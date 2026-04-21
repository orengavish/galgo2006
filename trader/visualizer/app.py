"""
visualizer/app.py
Flask web app for Galao monitoring.

Routes:
  GET /            -> dashboard
  GET /active      -> active orders (PENDING/SUBMITTING/SUBMITTED/FILLED)
  GET /positions   -> open positions with unrealized P&L
  GET /orders      -> all commands
  GET /ib-trace    -> IB events log
  GET /logs        -> log file viewer
  GET /preflight   -> last preflight results
  GET /release-notes -> release notes

  API (JSON):
  GET /api/price           -> {price, age_s, stale}
  GET /api/session-state   -> {session}
  GET /api/stats           -> status counts + unrealized_pnl
  GET /api/nearby          -> {price, lines: [{line, commands}]}
  GET /api/commands        -> commands (?status_in=A,B&status=X&symbol=Y&limit=N)
  GET /api/positions       -> open positions
  GET /api/ib-events       -> IB events (?limit=N)
  GET /api/preflight       -> last preflight results from system_state
  GET /api/logs            -> log tail (?component=broker&level=ERROR&limit=N)
  GET /api/release-notes   -> release notes (?program=X)
  GET /api/state           -> system_state rows

Usage:
    python visualizer/app.py                # run server
    python visualizer/app.py --self-test    # headless test

Self-test:
    python visualizer/app.py --self-test
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

_HERE = Path(__file__).parent
_TRADER = _HERE.parent        # trader/visualizer -> trader
_ROOT = _TRADER.parent        # trader -> galgo2026
for _p in (str(_ROOT), str(_TRADER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import re
from flask import Flask, jsonify, request, render_template
from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db

log = get_logger("visualizer")

app = Flask(__name__, template_folder="templates", static_folder="static")
_cfg = None
_db_path = None


def _get_cfg():
    global _cfg
    if _cfg is None:
        _cfg = get_config()
    return _cfg


def _get_db_path():
    global _db_path
    if _db_path is None:
        _db_path = Path(_get_cfg().paths.db)
    return _db_path


def _rows_to_list(rows) -> list:
    return [dict(r) for r in rows]


# ── Price API ─────────────────────────────────────────────────────────────────

@app.route("/api/price")
def api_price():
    try:
        from visualizer.price_feed import get_latest
        price, ts = get_latest()
    except Exception:
        price, ts = None, None

    if price is None:
        return jsonify({"price": None, "age_s": None, "stale": True})

    now = datetime.now(timezone.utc)
    age_s = int((now - ts).total_seconds()) if ts else None
    stale = age_s is not None and age_s > 30
    return jsonify({"price": price, "age_s": age_s, "stale": stale})


@app.route("/api/session-state")
def api_session_state():
    try:
        with get_db(_get_db_path()) as con:
            row = con.execute(
                "SELECT value FROM system_state WHERE key='SESSION'"
            ).fetchone()
        return jsonify({"session": row["value"] if row else None})
    except Exception:
        return jsonify({"session": None})


# ── Stats API ─────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    with get_db(_get_db_path()) as con:
        rows = con.execute(
            "SELECT status, COUNT(*) as cnt FROM commands GROUP BY status"
        ).fetchall()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        closed_today = con.execute(
            "SELECT COUNT(*) FROM commands WHERE status='CLOSED'"
            " AND date(updated_at)=?", (today,)
        ).fetchone()[0]
        errors = con.execute(
            "SELECT COUNT(*) FROM commands WHERE status IN ('ERROR','RECONCILE_REQUIRED')"
        ).fetchone()[0]

        # Unrealized P&L from open positions
        positions = _rows_to_list(con.execute(
            "SELECT * FROM positions WHERE status='OPEN'"
        ).fetchall())

    counts = {r["status"]: r["cnt"] for r in rows}
    counts["CLOSED"] = closed_today
    counts["ERROR"]  = errors

    try:
        from visualizer.price_feed import get_latest
        price, _ = get_latest()
    except Exception:
        price = None

    pnl = None
    if price and positions:
        pnl = sum(
            (price - p["entry_price"]) * p["quantity"] if p["direction"] == "BUY"
            else (p["entry_price"] - price) * p["quantity"]
            for p in positions
        )
    counts["unrealized_pnl"] = round(pnl, 2) if pnl is not None else None
    return jsonify(counts)


# ── Nearby lines API ──────────────────────────────────────────────────────────

@app.route("/api/nearby")
def api_nearby():
    """Return the 8 critical lines nearest to current price, with attached commands."""
    try:
        from visualizer.price_feed import get_latest
        price, _ = get_latest()
    except Exception:
        price = None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db(_get_db_path()) as con:
        lines = _rows_to_list(con.execute(
            "SELECT * FROM critical_lines WHERE date=? AND armed=1 ORDER BY price",
            (today,)
        ).fetchall())

        active_cmds = _rows_to_list(con.execute(
            "SELECT * FROM commands WHERE status IN"
            " ('PENDING','SUBMITTING','SUBMITTED','FILLED')"
        ).fetchall())

    if not price or not lines:
        return jsonify({"price": price, "lines": []})

    # Sort by distance from price, take nearest 8
    lines.sort(key=lambda l: abs(l["price"] - price))
    nearest = lines[:8]

    # Attach commands to each line
    result = []
    for ln in nearest:
        cmds = [c for c in active_cmds if abs(c["line_price"] - ln["price"]) < 0.01]
        result.append({"line": ln, "commands": cmds})

    # Sort by price descending for display (resistance above, support below)
    result.sort(key=lambda x: x["line"]["price"], reverse=True)
    return jsonify({"price": price, "lines": result})


# ── Commands API ──────────────────────────────────────────────────────────────

@app.route("/api/commands")
def api_commands():
    status_in = request.args.get("status_in")   # "PENDING,SUBMITTED,FILLED"
    status    = request.args.get("status")       # single status (legacy)
    symbol    = request.args.get("symbol")
    limit     = int(request.args.get("limit", 200))

    query  = "SELECT * FROM commands WHERE 1=1"
    params = []
    if status_in:
        placeholders = ",".join("?" for _ in status_in.split(","))
        query += f" AND status IN ({placeholders})"
        params.extend(status_in.split(","))
    elif status:
        query += " AND status=?"
        params.append(status)
    if symbol:
        query += " AND symbol=?"
        params.append(symbol)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with get_db(_get_db_path()) as con:
        rows = con.execute(query, params).fetchall()
    return jsonify(_rows_to_list(rows))


# ── Positions API ─────────────────────────────────────────────────────────────

@app.route("/api/positions")
def api_positions():
    with get_db(_get_db_path()) as con:
        rows = con.execute(
            """SELECT p.*,
                      c.tp_price, c.sl_price
               FROM positions p
               LEFT JOIN commands c ON c.id = p.command_id
               WHERE p.status='OPEN'
               ORDER BY p.id DESC"""
        ).fetchall()
    return jsonify(_rows_to_list(rows))


# ── IB Events API ─────────────────────────────────────────────────────────────

@app.route("/api/ib-events")
def api_ib_events():
    limit = int(request.args.get("limit", 500))
    with get_db(_get_db_path()) as con:
        rows = con.execute(
            "SELECT * FROM ib_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return jsonify(_rows_to_list(rows))


# ── Preflight API ─────────────────────────────────────────────────────────────

@app.route("/api/preflight")
def api_preflight():
    with get_db(_get_db_path()) as con:
        rows = con.execute(
            "SELECT * FROM system_state WHERE key LIKE 'preflight_%' ORDER BY key"
        ).fetchall()
    results = {}
    for row in rows:
        parts = row["value"].split("|", 2)
        results[row["key"]] = {
            "status": parts[0] if len(parts) > 0 else "",
            "time":   parts[1] if len(parts) > 1 else "",
            "detail": parts[2] if len(parts) > 2 else "",
        }
    return jsonify(results)


# ── Logs API ──────────────────────────────────────────────────────────────────

@app.route("/api/logs")
def api_logs():
    component = request.args.get("component", "broker")
    level     = request.args.get("level", "").upper()
    limit     = int(request.args.get("limit", 500))

    cfg = _get_cfg()
    log_path = Path(cfg.paths.logs) / f"{component}.log"
    if not log_path.exists():
        return jsonify({"lines": [], "error": f"Not found: {log_path.name}"})

    with open(log_path, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    if level:
        all_lines = [l for l in all_lines if f"| {level}" in l]

    lines = all_lines[-limit:]
    return jsonify({"component": component, "level": level,
                    "count": len(lines), "lines": lines})


# ── Release Notes API ─────────────────────────────────────────────────────────

@app.route("/api/release-notes")
def api_release_notes():
    program = request.args.get("program")
    query   = "SELECT * FROM release_notes WHERE 1=1"
    params  = []
    if program:
        query += " AND program=?"
        params.append(program)
    query += " ORDER BY id DESC"
    with get_db(_get_db_path()) as con:
        rows = con.execute(query, params).fetchall()
    return jsonify(_rows_to_list(rows))


# ── Generate API ─────────────────────────────────────────────────────────────

@app.route("/api/generate", methods=["POST"])
def api_generate():
    """
    POST /api/generate
    Body JSON:
      bracket      float  — bracket size in points (default 4.0)
      types        list   — entry types to use, e.g. ["MKT","LMT","STP"]
      count        int    — number of trades to insert (default 10, max 200)
      max_offset   int    — max entry offset from live price in ticks (default 2)

    Returns {count, price, commands: [...]}
    """
    body       = request.get_json(force=True) or {}
    bracket    = float(body.get("bracket", 4.0))
    types      = [t.upper() for t in body.get("types", ["MKT", "LMT", "STP"])]
    count      = min(int(body.get("count", 10)), 200)
    max_offset = int(body.get("max_offset", 2))

    if not types:
        return jsonify({"error": "At least one entry type required"}), 400

    try:
        from visualizer.price_feed import get_latest
        price, _ = get_latest()
    except Exception:
        price = None

    if price is None:
        return jsonify({"error": "No price available — price feed not running"}), 400

    import random as _rnd
    from random_gen import _build_trade, _insert_pending, _pick_entry_type

    cfg    = _get_cfg()
    tick   = cfg.orders.tick_size

    def rt(p):
        return round(round(p / tick) * tick, 10)

    inserted = []
    with get_db(_get_db_path()) as _con:
        pass  # just ensure DB open; inserts happen inside _insert_pending

    for _ in range(count):
        # Force entry_type from allowed types only
        entry_type = _rnd.choice(types)
        direction  = _rnd.choice(["BUY", "SELL"])
        offset     = _rnd.randint(1, max(1, max_offset)) * tick

        if entry_type == "MKT":
            entry_price = rt(price)
        elif entry_type == "LMT":
            entry_price = rt(price - offset) if direction == "BUY" else rt(price + offset)
        else:  # STP
            entry_price = rt(price + offset) if direction == "BUY" else rt(price - offset)

        tp_price = rt(entry_price + bracket) if direction == "BUY" else rt(entry_price - bracket)
        sl_price = rt(entry_price - bracket) if direction == "BUY" else rt(entry_price + bracket)

        trade = {
            "symbol":        (cfg.symbols or ["MES"])[0],
            "line_price":    entry_price,
            "line_type":     "SUPPORT" if direction == "BUY" else "RESISTANCE",
            "line_strength": _rnd.randint(1, 3),
            "direction":     direction,
            "entry_type":    entry_type,
            "entry_price":   entry_price,
            "tp_price":      tp_price,
            "sl_price":      sl_price,
            "bracket_size":  bracket,
            "source":        f"random_{entry_type.lower()}",
            "quantity":      cfg.orders.quantity,
        }
        cmd_id = _insert_pending(_get_db_path(), trade)
        inserted.append({"id": cmd_id, "direction": direction, "entry_type": entry_type,
                          "entry_price": entry_price, "tp_price": tp_price, "sl_price": sl_price})

    log.info(f"[generate] Inserted {len(inserted)} commands near price={price} bracket={bracket}")
    return jsonify({"count": len(inserted), "price": price, "commands": inserted})


# ── Test Trades API ───────────────────────────────────────────────────────────

@app.route("/api/test-trades", methods=["POST"])
def api_test_trades():
    """Insert ~10 test PENDING commands near current price for end-to-end testing."""
    try:
        from visualizer.price_feed import get_latest
        price, _ = get_latest()
    except Exception:
        price = None

    if price is None:
        return jsonify({"error": "No price available — price feed not running"}), 400

    tick    = 0.25
    bracket = 2.0
    symbol  = _get_cfg().symbols[0] if _get_cfg().symbols else "MES"

    def rt(p):
        return round(round(p / tick) * tick, 10)

    # 10 test orders: MKT long+short, aggressive LMT, STP already-triggered, near LMT/STP
    specs = [
        # Market orders (fill immediately)
        ("BUY",  "MKT", rt(price),        rt(price + bracket),       rt(price - bracket)),
        ("SELL", "MKT", rt(price),        rt(price - bracket),       rt(price + bracket)),
        # Aggressive LMT (limit far past market → fills at market price)
        ("BUY",  "LMT", rt(price + 2.0),  rt(price + 2.0 + bracket), rt(price + 2.0 - bracket)),
        ("SELL", "LMT", rt(price - 2.0),  rt(price - 2.0 - bracket), rt(price - 2.0 + bracket)),
        # STP already past market (triggers immediately → MKT fill)
        ("BUY",  "STP", rt(price - tick), rt(price - tick + bracket), rt(price - tick - bracket)),
        ("SELL", "STP", rt(price + tick), rt(price + tick - bracket), rt(price + tick + bracket)),
        # Near LMT (just inside spread — fills quickly)
        ("BUY",  "LMT", rt(price + 0.5),  rt(price + 0.5 + bracket), rt(price + 0.5 - bracket)),
        ("SELL", "LMT", rt(price - 0.5),  rt(price - 0.5 - bracket), rt(price - 0.5 + bracket)),
        # Near STP (one tick move triggers)
        ("BUY",  "STP", rt(price + 0.5),  rt(price + 0.5 + bracket), rt(price + 0.5 - bracket)),
        ("SELL", "STP", rt(price - 0.5),  rt(price - 0.5 - bracket), rt(price - 0.5 + bracket)),
    ]

    inserted = []
    with get_db(_get_db_path()) as con:
        for direction, entry_type, entry_price, tp_price, sl_price in specs:
            line_type = "SUPPORT" if direction == "BUY" else "RESISTANCE"
            con.execute(
                "INSERT INTO commands"
                " (symbol, line_price, line_type, line_strength,"
                "  direction, entry_type, entry_price, tp_price, sl_price, bracket_size, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test')",
                (symbol, rt(price), line_type, 1,
                 direction, entry_type, entry_price, tp_price, sl_price, bracket)
            )
            cmd_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            inserted.append({
                "id": cmd_id, "symbol": symbol,
                "direction": direction, "entry_type": entry_type,
                "entry_price": entry_price, "tp_price": tp_price, "sl_price": sl_price,
            })

    log.info(f"[test-trades] Inserted {len(inserted)} test commands near price={price}")
    return jsonify({"count": len(inserted), "price": price, "commands": inserted})


# ── IB Gateway Log API ────────────────────────────────────────────────────────

@app.route("/api/ib-gateway-log")
def api_ib_gateway_log():
    """
    GET /api/ib-gateway-log?limit=500
    Reads the most recent IB Gateway system log file from ib.gateway_log_dir.
    Returns {lines, file, error}.
    """
    limit = int(request.args.get("limit", 500))
    cfg   = _get_cfg()
    log_dir = getattr(cfg.ib, "gateway_log_dir", "") if hasattr(cfg, "ib") else ""

    if not log_dir:
        return jsonify({"lines": [], "file": None,
                        "error": "ib.gateway_log_dir not set in config.yaml"})

    log_dir_path = Path(log_dir)
    if not log_dir_path.exists():
        return jsonify({"lines": [], "file": None,
                        "error": f"Directory not found: {log_dir}"})

    # Find the most recent *.log or any log file in the dir (or subdirs by date)
    candidates = sorted(log_dir_path.glob("**/*.log"), key=lambda p: p.stat().st_mtime,
                        reverse=True)
    if not candidates:
        # Also try .txt files (IB Gateway sometimes uses different extensions)
        candidates = sorted(log_dir_path.glob("**/*.txt"), key=lambda p: p.stat().st_mtime,
                            reverse=True)
    if not candidates:
        return jsonify({"lines": [], "file": None,
                        "error": f"No log files found in {log_dir}"})

    latest = candidates[0]
    try:
        with open(latest, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        lines = all_lines[-limit:]
        return jsonify({"lines": lines, "file": str(latest), "error": None})
    except Exception as e:
        return jsonify({"lines": [], "file": str(latest), "error": str(e)})


# ── System State API ──────────────────────────────────────────────────────────

@app.route("/api/state")
def api_state():
    with get_db(_get_db_path()) as con:
        rows = con.execute("SELECT * FROM system_state ORDER BY key").fetchall()
    return jsonify(_rows_to_list(rows))


# ── Lines input API ───────────────────────────────────────────────────────────

_STRENGTH_LABELS = {1: "strong (!)", 2: "medium (?)", 3: "weak (no suffix)"}

def _parse_lines_text(text: str) -> list[dict]:
    """
    Parse Hebrew/free-form critical lines input.

    Recognised section headers (case-insensitive, Hebrew or English):
      קווי תמיכה / support
      קווי התנגדות / resistance

    Per-price suffixes:
      !  → strength 1 (strong)
      ?  → strength 2 (medium)
      (none) → strength 3 (weak)

    Ranges  A - B  inside a comma-separated list add both endpoints separately.
    """
    results = []

    support_text    = ""
    resistance_text = ""

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if "תמיכה" in line or "support" in low:
            idx = line.find(":")
            if idx >= 0:
                support_text = line[idx + 1:].strip()
        elif "התנגדות" in line or "resistance" in low:
            idx = line.find(":")
            if idx >= 0:
                resistance_text = line[idx + 1:].strip()

    def _parse_section(raw: str, line_type: str):
        items = []
        for token in raw.split(","):
            for sub in token.split(" - "):
                sub = sub.strip()
                if not sub:
                    continue
                if sub.endswith("!"):
                    strength, price_str = 3, sub[:-1].strip()
                elif sub.endswith("?"):
                    strength, price_str = 2, sub[:-1].strip()
                else:
                    strength, price_str = 1, sub.strip()
                try:
                    price = float(price_str)
                    items.append({"line_type": line_type, "price": price, "strength": strength})
                except ValueError:
                    pass
        return items

    if support_text:
        results.extend(_parse_section(support_text, "SUPPORT"))
    if resistance_text:
        results.extend(_parse_section(resistance_text, "RESISTANCE"))

    return results


@app.route("/api/lines", methods=["GET", "POST"])
def api_lines():
    """
    GET  /api/lines?date=YYYY-MM-DD&symbol=MES
         Returns current critical_lines rows for that date+symbol.

    POST /api/lines
         Body JSON: {text, date, symbol}
         Parses the text and upserts into critical_lines.
         Returns {count, lines} on success or {error} on failure.
    """
    if request.method == "GET":
        date_str = request.args.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        symbol   = request.args.get("symbol", (_get_cfg().symbols or ["MES"])[0])
        with get_db(_get_db_path()) as con:
            rows = _rows_to_list(con.execute(
                "SELECT * FROM critical_lines WHERE symbol=? AND date=? ORDER BY price",
                (symbol, date_str)
            ).fetchall())
        return jsonify(rows)

    # POST
    body   = request.get_json(force=True) or {}
    text   = body.get("text", "").strip()
    date_str = body.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    symbol   = body.get("symbol", (_get_cfg().symbols or ["MES"])[0])

    if not text:
        return jsonify({"error": "No text provided"}), 400

    lines = _parse_lines_text(text)
    if not lines:
        return jsonify({"error": "No valid lines found in input"}), 400
    if len(lines) > 20:
        return jsonify({"error": f"Too many lines ({len(lines)}), max is 20"}), 400

    with get_db(_get_db_path()) as con:
        con.execute(
            "DELETE FROM critical_lines WHERE symbol=? AND date=?",
            (symbol, date_str)
        )
        for ln in lines:
            con.execute(
                "INSERT INTO critical_lines (symbol, date, line_type, price, strength, armed)"
                " VALUES (?, ?, ?, ?, ?, 1)",
                (symbol, date_str, ln["line_type"], ln["price"], ln["strength"])
            )

    log.info(f"Lines input: saved {len(lines)} lines for {symbol} {date_str}")
    return jsonify({"count": len(lines), "lines": lines})


# ── Reset API ─────────────────────────────────────────────────────────────────

_WIPE_TABLES = ("commands", "positions", "ib_events", "system_state")


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """
    POST /api/reset
    Body JSON: {cancel_ib: bool, wipe_db: bool}
    Defaults: both true.

    1. Optionally call reqGlobalCancel on LIVE and PAPER.
    2. Optionally wipe operational DB tables (keeps critical_lines, release_notes).
    Returns {ib_cancel: "ok"|"failed"|"skipped", wipe: {table: count}, errors: [...]}
    """
    body      = request.get_json(force=True) or {}
    do_ib     = body.get("cancel_ib", True)
    do_wipe   = body.get("wipe_db",   True)

    result = {"ib_cancel": "skipped", "wipe": {}, "errors": []}

    # ── 1. IB cancel ──────────────────────────────────────────────────────────
    if do_ib:
        try:
            from lib.ib_client import IBClient
            cfg = _get_cfg()
            ibc = IBClient(cfg)
            ibc.connect(live=True, paper=True)
            try:
                ibc.live.reqGlobalCancel()
                log.info("[reset] reqGlobalCancel sent to LIVE")
            except Exception as e:
                result["errors"].append(f"LIVE cancel: {e}")
            try:
                ibc.paper.reqGlobalCancel()
                log.info("[reset] reqGlobalCancel sent to PAPER")
            except Exception as e:
                result["errors"].append(f"PAPER cancel: {e}")
            ibc.disconnect()
            result["ib_cancel"] = "ok"
        except Exception as e:
            result["ib_cancel"] = "failed"
            result["errors"].append(f"IB connect: {e}")
            log.warning(f"[reset] IB cancel failed: {e}")

    # ── 2. DB wipe ────────────────────────────────────────────────────────────
    if do_wipe:
        try:
            with get_db(_get_db_path()) as con:
                for tbl in _WIPE_TABLES:
                    cnt = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                    con.execute(f"DELETE FROM {tbl}")
                    result["wipe"][tbl] = cnt
            log.info(f"[reset] DB wiped: {result['wipe']}")
        except Exception as e:
            result["errors"].append(f"DB wipe: {e}")
            log.error(f"[reset] DB wipe failed: {e}")

    return jsonify(result)


# ── Report API ────────────────────────────────────────────────────────────────

@app.route("/api/report")
def api_report():
    """
    GET /api/report?source=X&symbol=Y&limit=N
    Reads from completed_trades — guaranteed no duplicates, no missing data.
      trades       — list of completed trades (newest first)
      by_source    — per-source stats
      by_exit      — per-exit-reason stats
      summary      — overall totals
      equity_curve — [{time, pnl, cumulative}] sorted by exit_time
    """
    source  = request.args.get("source")
    symbol  = request.args.get("symbol")
    limit   = int(request.args.get("limit", 5000))

    query  = "SELECT * FROM completed_trades WHERE 1=1"
    params = []
    if source:
        query += " AND source=?"
        params.append(source)
    if symbol:
        query += " AND symbol=?"
        params.append(symbol)
    query += " ORDER BY exit_time ASC LIMIT ?"
    params.append(limit)

    with get_db(_get_db_path()) as con:
        rows = _rows_to_list(con.execute(query, params).fetchall())

    trades_with_pnl = rows  # completed_trades always has pnl_points (NOT NULL)

    # ── by_source ──
    src_map: dict = {}
    for r in rows:
        src = r["source"] or "unknown"
        if src not in src_map:
            src_map[src] = {"source": src, "count": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
        src_map[src]["count"] += 1
        pnl = r.get("pnl_points")
        if pnl is not None:
            src_map[src]["total_pnl"] += pnl
            if pnl > 0:
                src_map[src]["wins"] += 1
            else:
                src_map[src]["losses"] += 1
    for s in src_map.values():
        n = s["wins"] + s["losses"]
        s["win_rate"]  = round(100.0 * s["wins"] / n, 1) if n else None
        s["avg_pnl"]   = round(s["total_pnl"] / n, 4)    if n else None
        s["total_pnl"] = round(s["total_pnl"], 4)
    by_source = sorted(src_map.values(), key=lambda x: x["total_pnl"], reverse=True)

    # ── by_exit ──
    exit_map: dict = {}
    for r in rows:
        reason = r["exit_reason"] or "unknown"
        exit_map[reason] = exit_map.get(reason, 0) + 1
    by_exit = [{"reason": k, "count": v} for k, v in sorted(exit_map.items())]

    # ── summary ──
    pnl_vals = [r["pnl_points"] for r in trades_with_pnl]
    total_pnl = sum(pnl_vals) if pnl_vals else 0.0
    wins = sum(1 for p in pnl_vals if p > 0)
    summary = {
        "total_trades": len(rows),
        "trades_with_pnl": len(pnl_vals),
        "total_pnl":    round(total_pnl, 4),
        "win_rate":     round(100.0 * wins / len(pnl_vals), 1) if pnl_vals else None,
        "avg_pnl":      round(total_pnl / len(pnl_vals), 4)    if pnl_vals else None,
        "best_trade":   round(max(pnl_vals), 4)                 if pnl_vals else None,
        "worst_trade":  round(min(pnl_vals), 4)                 if pnl_vals else None,
    }

    # ── equity_curve ──
    equity_curve = []
    cumulative = 0.0
    for r in trades_with_pnl:
        cumulative += r["pnl_points"]
        equity_curve.append({
            "time":       r["exit_time"],
            "pnl":        round(r["pnl_points"], 4),
            "cumulative": round(cumulative, 4),
            "source":     r["source"] or "unknown",
        })

    return jsonify({
        "trades":       list(reversed(rows)),   # newest first for table display
        "by_source":    by_source,
        "by_exit":      by_exit,
        "summary":      summary,
        "equity_curve": equity_curve,
    })


# ── HTML routes ───────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("dashboard.html", active="dashboard")


@app.route("/active")
def active_page():
    return render_template("active.html", active="active")


@app.route("/positions")
def positions_page():
    return render_template("positions.html", active="positions")


@app.route("/orders")
def orders_page():
    return render_template("orders.html", active="orders")


@app.route("/ib-trace")
def ib_trace_page():
    return render_template("ib_trace.html", active="ib_trace")


@app.route("/logs")
def logs_page():
    cfg = _get_cfg()
    log_dir = Path(cfg.paths.logs)
    components = sorted([p.stem for p in log_dir.glob("*.log")]) if log_dir.exists() else ["broker"]
    return render_template("logs.html", active="logs", components=components)


@app.route("/preflight")
def preflight_page():
    return render_template("preflight.html", active="preflight")


@app.route("/release-notes")
def release_notes_page():
    return render_template("release_notes.html", active="release_notes")


@app.route("/report")
def report_page():
    return render_template("report.html", active="report")


@app.route("/lines")
def lines_page():
    cfg = _get_cfg()
    return render_template("lines.html", active="lines",
                           symbols=cfg.symbols or ["MES"])


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        from lib.logger import reset_loggers
        from lib.db import init_db, set_system_state

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            (log_dir / "broker.log").write_text(
                "2026-04-07T10:00:00 | INFO | broker | started\n"
                "2026-04-07T10:00:01 | ERROR | broker | test error\n"
            )

            init_db(db_path)

            with get_db(db_path) as con:
                set_system_state(con, "SESSION", "RUNNING")
                con.execute(
                    "INSERT INTO release_notes (program, version, summary)"
                    " VALUES ('test_prog', '0.1.0', 'Test note')"
                )
                con.execute(
                    "INSERT INTO system_state (key, value) VALUES"
                    " ('preflight_db', 'PASS|2026-04-07T10:00:00Z|')"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                )
                con.execute(
                    "INSERT INTO ib_events (event_type, component, message)"
                    " VALUES ('ERROR', 'broker', 'test IB error')"
                )
                con.execute(
                    "INSERT INTO critical_lines (symbol, date, line_type, price, strength)"
                    " VALUES ('MES', '2026-04-07', 'SUPPORT', 5500.0, 1)"
                )

            global _db_path, _cfg
            _db_path = db_path

            # Patch config paths for log dir
            class _FakePaths:
                db   = str(db_path)
                logs = str(log_dir)
            class _FakeCfg:
                paths = _FakePaths()
            _cfg = _FakeCfg()

            app.config["TESTING"] = True
            client = app.test_client()

            routes_200 = [
                "/",
                "/active",
                "/positions",
                "/orders",
                "/ib-trace",
                "/preflight",
                "/release-notes",
                "/api/price",
                "/api/session-state",
                "/api/stats",
                "/api/nearby",
                "/api/commands",
                "/api/commands?status_in=PENDING,SUBMITTED",
                "/api/positions",
                "/api/ib-events",
                "/api/preflight",
                "/api/logs?component=broker",
                "/api/logs?component=broker&level=ERROR",
                "/api/release-notes",
                "/api/state",
            ]

            for route in routes_200:
                resp = client.get(route)
                assert resp.status_code == 200, \
                    f"Route {route}: expected 200, got {resp.status_code}"

            # /logs page needs components list — test separately
            resp = client.get("/logs")
            assert resp.status_code == 200, f"/logs failed"

            # Validate shapes
            pf = client.get("/api/preflight").get_json()
            assert "preflight_db" in pf
            assert pf["preflight_db"]["status"] == "PASS"

            rn = client.get("/api/release-notes").get_json()
            assert isinstance(rn, list) and len(rn) >= 1
            assert rn[0]["program"] == "test_prog"

            stats = client.get("/api/stats").get_json()
            assert isinstance(stats, dict)

            cmds_all = client.get("/api/commands").get_json()
            assert isinstance(cmds_all, list)

            logs_data = client.get("/api/logs?component=broker").get_json()
            assert isinstance(logs_data["lines"], list)
            assert len(logs_data["lines"]) == 2

            logs_err = client.get("/api/logs?component=broker&level=ERROR").get_json()
            assert len(logs_err["lines"]) == 1, \
                f"Expected 1 ERROR line, got {len(logs_err['lines'])}"

            ib = client.get("/api/ib-events").get_json()
            assert isinstance(ib, list) and len(ib) >= 1
            assert ib[0]["event_type"] == "ERROR"

            _db_path = None
            _cfg = None
            reset_loggers()

        print("[self-test] visualizer: PASS")
        return True

    except Exception as e:
        print(f"[self-test] visualizer: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao visualizer")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--no-price-feed", action="store_true",
                        help="Skip IB price feed thread (useful for testing)")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    cfg = get_config()
    init_db(Path(cfg.paths.db))

    if not args.no_price_feed:
        from visualizer.price_feed import start as start_price_feed
        symbol = cfg.symbols[0] if cfg.symbols else "MES"
        start_price_feed(cfg, symbol=symbol, interval=5)
        log.info(f"Price feed started for {symbol}")

    log.info(f"Visualizer starting on {cfg.visualizer.host}:{cfg.visualizer.port}")
    app.run(host=cfg.visualizer.host, port=cfg.visualizer.port, debug=False)
