"""
back-trading/algo_dashboard.py
Algo Dashboard — Flask on port 5002.

Generates best-100 trade candidates from:
  - Full-duplex structural exits (critical-line TP/SL)
  - Half-duplex top-combo matrix (backtest-scored params)

Candidates are scored by: 60% historical backtest score + 40% current-price proximity.
Submit button inserts all 100 into commands table → existing broker on port 5001 picks them up.

Usage:
    python back-trading/algo_dashboard.py
    python back-trading/algo_dashboard.py --port 5002
    python back-trading/algo_dashboard.py --self-test
"""

import sys
import json
import uuid
import argparse
import threading
import time
import collections
from itertools import zip_longest
from pathlib import Path
from datetime import datetime, timezone

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
for _p in (str(_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import requests
from flask import Flask, jsonify, request, render_template

from lib.db          import get_db, init_db
from lib.day_params  import get_day_params, _MIN_EXIT_TICKS, _TICK as _DAY_TICK
from lib.algo_engine import AlgoParams, _build_cmds

_TICK       = 0.25
_MAX_CMDS   = 400
_TOP_N      = 100        # candidates shown in UI
_BATCH_SIZE = 200        # commands submitted per button press
_MAX_ACTIVE = 500        # gate: refuse submit if PENDING+SUBMITTING+SUBMITTED >= this
_HD_TOP_N   = 5          # top N combos from scorer to apply as HD candidates

# Round-number interval (pts) used when auto-generating lines from current price
_AUTO_LINE_INTERVALS = {"MES": 10, "MNQ": 50, "MYM": 250, "M2K": 10}


def _auto_generate_lines(symbol: str, price: float, date_str: str) -> list[dict]:
    """Generate 2 SUPPORT + 2 RESISTANCE lines at round-number intervals near price."""
    interval = _AUTO_LINE_INTERVALS.get(symbol, 10)
    base = round(price / interval) * interval
    lines = []
    for i in (2, 1):  # resistance: closer = stronger
        lines.append({"symbol": symbol, "date": date_str,
                      "line_type": "RESISTANCE",
                      "price": float(base + interval * i),
                      "strength": 3 if i == 1 else 2})
    for i in (1, 2):  # support: closer = stronger
        lines.append({"symbol": symbol, "date": date_str,
                      "line_type": "SUPPORT",
                      "price": float(base - interval * i),
                      "strength": 3 if i == 1 else 2})
    return lines

# Default HD combo grid — full Cartesian product used when no backtest scores exist.
# Intentionally broad ("thousands in memory, top 100 shown"); pruned at scoring stage.
_HD_DEFAULT_COMBOS = [
    {"algo_type": at, "tp_ticks": tp, "sl_ticks": sl,
     "direction_filter": df, "strength_max": st,
     "composite_score": 0.0, "n_fills": 0}
    for at in ["BOUNCE", "BREAKOUT", "FADE", "DIRECTIONAL", "BOTH"]
    for tp in [4, 8, 16, 32]
    for sl in [2, 4, 8]
    for df in ["BUY", "SELL", "BOTH"]
    for st in [1, 2, 3]
]
_TRADER_URL = "http://127.0.0.1:5001"

ALL_SYMBOLS = ["MES", "MNQ", "MYM", "M2K"]

# Minimum bracket (TP distance in points) — filters out too-tight combos that stop out immediately
_MIN_BRACKET = {"MES": 4.0, "MNQ": 4.0, "MYM": 4.0, "M2K": 2.0}

# Per-symbol minimum SL distance in points — prevents near-zero stops that guarantee immediate exit
_MIN_SL_PTS = {"MES": 1.0, "MNQ": 4.0, "MYM": 10.0, "M2K": 0.5}

# Per-symbol minimum avg_move used for distance gate and FD TP/SL/proximity scaling.
# day_params computes from prior-day CSV but may return a small default (10.0) when no data.
# These floors ensure reasonable behaviour even with missing history.
_MIN_AVG_MOVE = {"MES": 16.0, "MNQ": 150.0, "M2K": 20.0, "MYM": 200.0}


# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder="algo_templates")

_candidates: list[dict] = []
_candidates_lock = threading.Lock()
_candidates_meta: dict = {}

# Overrideable for testing
_DB_PATH_OVERRIDE:   Path | None = None
_HIST_PATH_OVERRIDE: Path | None = None


def _resolve_db() -> Path:
    if _DB_PATH_OVERRIDE:
        return _DB_PATH_OVERRIDE
    cfg_path = _ROOT / "trader" / "config.yaml"
    if cfg_path.exists():
        try:
            import yaml
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f)
            db_rel = cfg.get("paths", {}).get("db", "data/galao.db")
            return (cfg_path.parent / db_rel).resolve()
        except Exception:
            pass
    return (_ROOT / "trader" / "data" / "galao.db").resolve()


def _ensure_tables(db_path: Path) -> None:
    """Create algo_candidates table if it doesn't exist (safe on existing DBs)."""
    with get_db(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS algo_candidates (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id          TEXT    NOT NULL,
                rank                INTEGER NOT NULL,
                symbol              TEXT    NOT NULL,
                command_class       TEXT    NOT NULL,
                algo_type           TEXT    NOT NULL,
                direction           TEXT    NOT NULL,
                entry_type          TEXT    NOT NULL,
                entry_price         REAL    NOT NULL,
                tp_price            REAL    NOT NULL,
                sl_price            REAL    NOT NULL,
                bracket             REAL    NOT NULL,
                entry_line_price    REAL,
                entry_line_type     TEXT,
                entry_line_strength INTEGER,
                entry_line_id       INTEGER,
                combined_score      REAL,
                queued_status       TEXT    NOT NULL DEFAULT 'QUEUED',
                command_ids         TEXT,
                created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_algo_cand_sess"
            " ON algo_candidates(session_id, queued_status, rank)"
        )


def _resolve_history() -> Path:
    if _HIST_PATH_OVERRIDE:
        return _HIST_PATH_OVERRIDE
    return _resolve_db().parent / "history"


# ── Price helpers ─────────────────────────────────────────────────────────────

def _fetch_price(symbol: str = "MES") -> dict:
    """Try live price from port 5001; fall back to price_cache in DB."""
    try:
        r = requests.get(f"{_TRADER_URL}/api/price", params={"symbol": symbol}, timeout=1.5)
        if r.ok:
            data = r.json()
            if data.get("price"):
                return {"price": data["price"], "source": "live", "age_s": data.get("age_s", 0)}
    except Exception:
        pass
    # DB fallback
    try:
        db_path = _resolve_db()
        with get_db(db_path) as con:
            row = con.execute(
                "SELECT last_price, updated_at FROM price_cache WHERE symbol=?", (symbol,)
            ).fetchone()
            if row:
                return {"price": row[0], "source": "delayed", "age_s": None}
    except Exception:
        pass
    return {"price": None, "source": "unavailable", "age_s": None}


# ── Candidate generation ───────────────────────────────────────────────────────

def _rt(p: float) -> float:
    return round(round(p / _TICK) * _TICK, 10)


def _find_tp_line(entry_price: float, direction: str,
                  lines: list[dict], avg_move: float) -> dict | None:
    min_dist = _MIN_EXIT_TICKS * _TICK
    if direction == "BUY":
        cands = [l for l in lines
                 if l["line_type"] == "RESISTANCE"
                 and l["price"] >= entry_price + min_dist
                 and l["price"] <= entry_price + avg_move]
        return min(cands, key=lambda l: l["price"]) if cands else None
    else:
        cands = [l for l in lines
                 if l["line_type"] == "SUPPORT"
                 and l["price"] <= entry_price - min_dist
                 and l["price"] >= entry_price - avg_move]
        return max(cands, key=lambda l: l["price"]) if cands else None


def _find_sl_line(line_price: float, direction: str, lines: list[dict]) -> dict | None:
    if direction == "BUY":
        cands = [l for l in lines if l["line_type"] == "SUPPORT" and l["price"] < line_price]
        return max(cands, key=lambda l: l["price"]) if cands else None
    else:
        cands = [l for l in lines if l["line_type"] == "RESISTANCE" and l["price"] > line_price]
        return min(cands, key=lambda l: l["price"]) if cands else None


def _get_armed_symbols(db_path: Path) -> list[str]:
    """Return symbols that have at least one armed critical line, ordered."""
    try:
        with get_db(db_path) as con:
            rows = con.execute(
                "SELECT DISTINCT symbol FROM critical_lines WHERE armed=1 ORDER BY symbol"
            ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def _generate_candidates(symbols: list[str], current_prices: dict) -> tuple[list[dict], dict]:
    """
    Build and score trade candidates in memory.
    Returns (candidates_list, meta_dict).
    """
    db_path    = _resolve_db()
    hist_dir   = _resolve_history()

    with get_db(db_path) as con:
        # Most recent armed date per symbol independently
        sym_dates: dict[str, str] = {}
        for sym in symbols:
            row = con.execute(
                "SELECT MAX(date) FROM critical_lines WHERE armed=1 AND symbol=?", (sym,)
            ).fetchone()
            if row and row[0]:
                sym_dates[sym] = row[0]

        if not sym_dates:
            return [], {"error": "No armed critical lines found for selected symbols",
                        "n_returned": 0, "n_generated": 0, "n_lines": 0}

        # Armed lines — each symbol at its own most-recent date
        lines_all = []
        for sym, d in sym_dates.items():
            rows = con.execute(
                "SELECT * FROM critical_lines WHERE armed=1 AND date=? AND symbol=?", (d, sym)
            ).fetchall()
            lines_all.extend(dict(r) for r in rows)

        date_str = max(sym_dates.values())

        # HD: top combos per symbol (table may not exist on fresh DB)
        hd_combos: dict[str, list] = {}
        for sym in symbols:
            try:
                ts_row = con.execute(
                    "SELECT MAX(scored_at) FROM cl_algo_combo_scores WHERE symbol=?", (sym,)
                ).fetchone()
                if ts_row and ts_row[0]:
                    rows = con.execute("""
                        SELECT * FROM cl_algo_combo_scores
                        WHERE symbol=? AND scored_at=? AND data_status='ok'
                        ORDER BY rank ASC LIMIT ?
                    """, (sym, ts_row[0], _HD_TOP_N)).fetchall()
                    hd_combos[sym] = [dict(r) for r in rows]
                else:
                    hd_combos[sym] = []
            except Exception:
                hd_combos[sym] = []

        # FD historical P&L (table may not exist on fresh DB)
        fd_hist: dict = {}
        try:
            rows = con.execute("""
                SELECT symbol, entry_line_type, direction, tp_source,
                       AVG(pnl_ticks) as avg_pnl, COUNT(*) as n
                FROM cl_algo_fd_results
                WHERE entry_fill_price IS NOT NULL
                GROUP BY symbol, entry_line_type, direction, tp_source
            """).fetchall()
            for r in rows:
                fd_hist[(r[0], r[1], r[2], r[3])] = {"avg_pnl": r[4] or 0.0, "n": r[5]}
        except Exception:
            pass

    candidates = []

    for sym in symbols:
        if sym not in current_prices:
            continue  # no live price — skip entirely to avoid stale-level trades
        sym_lines = [l for l in lines_all if l["symbol"] == sym]
        if not sym_lines:
            continue
        cur_price = current_prices.get(sym)

        params = get_day_params(db_path, sym, date_str, hist_dir)
        avg_move  = max(params["two_hour_avg_move"], _MIN_AVG_MOVE.get(sym, 16.0))
        tick_buf  = params["tick_buffer"]
        buf_price = _rt(tick_buf * _TICK)

        # ── Full-duplex candidates ─────────────────────────────────────────────
        for line in sym_lines:
            lp        = line["price"]
            ltype     = line["line_type"]
            direction = "BUY" if ltype == "SUPPORT" else "SELL"
            entry_p   = _rt(lp + buf_price) if direction == "BUY" else _rt(lp - buf_price)

            tp_line = _find_tp_line(entry_p, direction, sym_lines, avg_move)
            if tp_line:
                tp_price  = (_rt(tp_line["price"] - buf_price) if direction == "BUY"
                             else _rt(tp_line["price"] + buf_price))
                tp_source = "critical_line"
                tp_line_p = tp_line["price"]
            else:
                tp_price  = (_rt(entry_p + avg_move) if direction == "BUY"
                             else _rt(entry_p - avg_move))
                tp_source = "2hr_avg_fallback"
                tp_line_p = None

            sl_line = _find_sl_line(lp, direction, sym_lines)
            if sl_line:
                sl_price  = (_rt(sl_line["price"] - buf_price) if direction == "BUY"
                             else _rt(sl_line["price"] + buf_price))
                sl_source = "critical_line"
                sl_line_p = sl_line["price"]
            else:
                sl_price  = (_rt(entry_p - avg_move) if direction == "BUY"
                             else _rt(entry_p + avg_move))
                sl_source = "2hr_avg_fallback"
                sl_line_p = None

            hist = fd_hist.get((sym, ltype, direction, tp_source), {})
            hist_raw = hist.get("avg_pnl", 0.0)

            prox = 0.0
            if cur_price is not None:
                prox = max(0.0, 1.0 - abs(cur_price - entry_p) / max(avg_move, 1.0))

            # One candidate per line-direction; submit will create both LMT and STP orders
            candidates.append({
                "symbol":             sym,
                "date":               date_str,
                "command_class":      "FD",
                "algo_type":          "FULL_DUPLEX",
                "entry_line_price":   lp,
                "entry_line_type":    ltype,
                "entry_line_strength": line["strength"],
                "entry_line_id":      line.get("id"),
                "direction":          direction,
                "entry_type":         "LMT+STP",  # both orders created on submit
                "entry_price":        entry_p,
                "tp_price":           tp_price,
                "tp_source":          tp_source,
                "tp_line_price":      tp_line_p,
                "sl_price":           sl_price,
                "sl_source":          sl_source,
                "sl_line_price":      sl_line_p,
                "bracket":            round(abs(tp_price - entry_p), 4),
                "avg_move":           avg_move,
                "hist_raw":           hist_raw,
                "hist_n":             hist.get("n", 0),
                "proximity":          round(prox, 4),
            })

        # ── Half-duplex candidates ─────────────────────────────────────────────
        # Fall back to default grid when no backtest scores exist yet
        active_combos = hd_combos.get(sym) or [
            {**d, "composite_score": 0.0, "n_fills": 0} for d in _HD_DEFAULT_COMBOS
        ]
        for combo in active_combos:
            ap = AlgoParams(
                algo_type=combo["algo_type"],
                tp_ticks=combo["tp_ticks"],
                sl_ticks=combo["sl_ticks"],
                direction_filter=combo["direction_filter"],
                strength_max=combo["strength_max"],
            )
            approx_price = cur_price or (sym_lines[0]["price"] if sym_lines else 0)
            for line in sym_lines:
                cmds = _build_cmds(line, ap, approx_price)
                for cmd in cmds:
                    ep  = cmd["entry_price"]
                    tp  = cmd["tp_price"]
                    sl  = cmd["sl_price"]
                    prox = 0.0
                    if cur_price is not None:
                        prox = max(0.0, 1.0 - abs(cur_price - ep) / max(avg_move, 1.0))
                    candidates.append({
                        "symbol":             sym,
                        "date":               date_str,
                        "command_class":      "HD",
                        "algo_type":          combo["algo_type"],
                        "tp_ticks":           combo["tp_ticks"],
                        "sl_ticks":           combo["sl_ticks"],
                        "direction_filter":   combo["direction_filter"],
                        "strength_max":       combo["strength_max"],
                        "entry_line_price":   line["price"],
                        "entry_line_type":    line["line_type"],
                        "entry_line_strength": line["strength"],
                        "entry_line_id":      line.get("id"),
                        "direction":          cmd["direction"],
                        "entry_type":         cmd["entry_type"],
                        "entry_price":        ep,
                        "tp_price":           tp,
                        "tp_source":          "bracket",
                        "tp_line_price":      None,
                        "sl_price":           sl,
                        "sl_source":          "bracket",
                        "sl_line_price":      None,
                        "bracket":            round(abs(tp - ep), 4),
                        "avg_move":           avg_move,
                        "hist_raw":           combo.get("composite_score") or 0.0,
                        "hist_n":             combo.get("n_fills", 0),
                        "proximity":          round(prox, 4),
                    })

    if not candidates:
        return [], {"date": date_str, "n_lines": len(lines_all),
                    "n_generated": 0, "n_returned": 0,
                    "error": f"Lines found ({len(lines_all)}) but no candidates generated — "
                             "need at least 2 lines (one support + one resistance) for FD, "
                             "or backtest combo scores for HD."}

    # Normalize hist_raw across all candidates → [0,1]
    hist_vals = [c["hist_raw"] for c in candidates]
    mn, mx    = min(hist_vals), max(hist_vals)
    hist_rng  = mx - mn if mx != mn else 1.0
    for c in candidates:
        c["hist_score"] = round((c["hist_raw"] - mn) / hist_rng, 4)
        c["combined_score"] = round(0.6 * c["hist_score"] + 0.4 * c["proximity"], 4)

    # Round-robin across symbols first, then algo_types within each symbol —
    # ensures no single symbol dominates the top-N when scores are flat or scored.
    def _interleave_by_symbol(cands: list) -> list:
        sym_groups: dict = collections.defaultdict(list)
        for c in cands:
            sym_groups[c["symbol"]].append(c)
        # Within each symbol, round-robin across algo_types
        sym_tops: dict = {}
        for sym, sc in sym_groups.items():
            at_groups: dict = collections.defaultdict(list)
            for c in sc:
                at_groups[c["algo_type"]].append(c)
            for g in at_groups.values():
                g.sort(key=lambda c: (-c.get("entry_line_strength", 0),
                                      c.get("tp_ticks", 0), c.get("sl_ticks", 0)))
            sym_tops[sym] = []
            for batch in zip_longest(*[at_groups[a] for a in sorted(at_groups)]):
                for c in batch:
                    if c is not None:
                        sym_tops[sym].append(c)
        # Round-robin across symbols
        result = []
        for batch in zip_longest(*[sym_tops[s] for s in sorted(sym_tops)]):
            for c in batch:
                if c is not None:
                    result.append(c)
        return result

    score_spread = max(c["combined_score"] for c in candidates) - \
                   min(c["combined_score"] for c in candidates)
    if score_spread < 0.001:
        top = _interleave_by_symbol(candidates)
    else:
        candidates.sort(key=lambda c: c["combined_score"], reverse=True)
        top = _interleave_by_symbol(candidates)

    for i, c in enumerate(top):
        c["rank"] = i + 1
    meta = {
        "date":         date_str,
        "n_lines":      len(lines_all),
        "n_generated":  len(candidates),
        "n_returned":   len(top),
        "symbols":      symbols,
        "price_source": {sym: current_prices.get(sym) for sym in symbols},
    }
    return top, meta


# ── Routes: pages ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html", active="dashboard", symbols=ALL_SYMBOLS)


@app.route("/trades")
def trades_page():
    return render_template("trades.html", active="trades", symbols=ALL_SYMBOLS)


@app.route("/lines")
def lines_page():
    return render_template("lines.html", active="lines", symbols=ALL_SYMBOLS)


# ── Routes: API ───────────────────────────────────────────────────────────────

@app.route("/api/price")
def api_price():
    sym = request.args.get("symbol", "MES")
    return jsonify(_fetch_price(sym))


@app.route("/api/algo/create", methods=["POST"])
def api_create():
    body    = request.get_json(silent=True) or {}
    symbols = body.get("symbols", ["MES"])
    symbols = [s for s in symbols if s in ALL_SYMBOLS] or ["MES"]

    # Gather prices (one per symbol, best-effort)
    prices = {}
    for sym in symbols:
        info = _fetch_price(sym)
        if info["price"] is not None:
            prices[sym] = info["price"]

    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db_path = _resolve_db()
    _ensure_tables(db_path)

    # Auto-generate lines at current price, replacing stale armed lines.
    # Disarm lines for any checked symbol that has no live price (stale/wrong levels).
    lines_created = []
    with get_db(db_path) as con:
        for sym in symbols:
            px = prices.get(sym)
            if px is None:
                # No current price — disarm any leftover lines to avoid stale trades
                con.execute("UPDATE critical_lines SET armed=0 WHERE symbol=? AND armed=1", (sym,))
                continue
            con.execute("UPDATE critical_lines SET armed=0 WHERE symbol=? AND armed=1", (sym,))
            for ln in _auto_generate_lines(sym, px, today):
                con.execute(
                    "INSERT INTO critical_lines(symbol,date,line_type,price,strength,armed)"
                    " VALUES(?,?,?,?,?,1)",
                    (ln["symbol"], ln["date"], ln["line_type"], ln["price"], ln["strength"])
                )
                lines_created.append(ln)

    # Generate ALL candidates (no display cap)
    cands, meta = _generate_candidates(symbols, prices)

    # Drop candidates with too-tight TP bracket or too-tight SL (near-instant stop-out)
    cands = [c for c in cands if c["bracket"] >= _MIN_BRACKET.get(c["symbol"], 4.0)]
    cands = [c for c in cands
             if abs(c["sl_price"] - c["entry_price"]) >= _MIN_SL_PTS.get(c["symbol"], 1.0)]

    # Deduplicate: one entry per (symbol, direction, entry_type, entry_price, bracket)
    # Multiple lines can produce identical candidates — keep highest-ranked (first in list)
    _seen_keys: set = set()
    deduped = []
    for c in cands:
        k = (c["symbol"], c["direction"], c["entry_type"],
             round(c["entry_price"], 4), c["bracket"])
        if k not in _seen_keys:
            _seen_keys.add(k)
            deduped.append(c)
    cands = deduped

    # Persist to algo_candidates queue
    session_id = str(uuid.uuid4())
    if cands:
        with get_db(db_path) as con:
            con.execute(
                "UPDATE algo_candidates SET queued_status='SKIPPED'"
                " WHERE queued_status='QUEUED'"
            )
            con.executemany(
                "INSERT INTO algo_candidates"
                "(session_id,rank,symbol,command_class,algo_type,direction,entry_type,"
                " entry_price,tp_price,sl_price,bracket,"
                " entry_line_price,entry_line_type,entry_line_strength,entry_line_id,combined_score)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(session_id, c["rank"], c["symbol"], c["command_class"], c["algo_type"],
                  c["direction"], c["entry_type"],
                  c["entry_price"], c["tp_price"], c["sl_price"], c["bracket"],
                  c.get("entry_line_price"), c.get("entry_line_type"),
                  c.get("entry_line_strength"), c.get("entry_line_id"),
                  c.get("combined_score"))
                 for c in cands]
            )
            con.execute(
                "INSERT OR REPLACE INTO system_state(key,value,updated_at)"
                " VALUES('ALGO_SESSION_ID',?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (session_id,)
            )

    # Store top 100 for UI display only
    display = cands[:_TOP_N]
    with _candidates_lock:
        _candidates.clear()
        _candidates.extend(display)
        _candidates_meta.clear()
        _candidates_meta.update(meta)

    meta["lines_created"]      = len(lines_created)
    meta["total_queued"]       = len(cands)
    meta["price_source_label"] = (
        "live" if any(_fetch_price(s)["source"] == "live" for s in symbols) else "delayed"
    )
    return jsonify({
        "candidates":   display,
        "meta":         meta,
        "session_id":   session_id,
        "total_queued": len(cands),
    })


@app.route("/api/algo/candidates")
def api_candidates():
    with _candidates_lock:
        return jsonify({"candidates": _candidates, "meta": _candidates_meta})


@app.route("/api/algo/queue")
def api_queue_status():
    db_path = _resolve_db()
    try:
        _ensure_tables(db_path)
        with get_db(db_path) as con:
            sess = con.execute(
                "SELECT value FROM system_state WHERE key='ALGO_SESSION_ID'"
            ).fetchone()
            if not sess:
                return jsonify({"session_id": None, "queued": 0, "submitted_cands": 0,
                                "total": 0, "active_commands": 0,
                                "max_active": _MAX_ACTIVE, "batch_size": _BATCH_SIZE,
                                "batches_remaining": 0, "next_batch_start": 0, "next_batch_end": 0})
            session_id = sess["value"]
            rows = con.execute(
                "SELECT queued_status, COUNT(*) n FROM algo_candidates WHERE session_id=?"
                " GROUP BY queued_status",
                (session_id,)
            ).fetchall()
            counts = {r["queued_status"]: r["n"] for r in rows}
            active = con.execute(
                "SELECT COUNT(*) FROM commands WHERE source='algo_dashboard'"
                " AND status IN ('PENDING','SUBMITTING','SUBMITTED')"
            ).fetchone()[0]
        queued     = counts.get("QUEUED", 0)
        submitted  = counts.get("SUBMITTED", 0)
        total      = queued + submitted + counts.get("SKIPPED", 0)
        n_batches  = (queued + _BATCH_SIZE - 1) // _BATCH_SIZE if queued else 0
        next_start = submitted + 1
        next_end   = submitted + min(_BATCH_SIZE, queued)
        return jsonify({
            "session_id":       session_id,
            "queued":           queued,
            "submitted_cands":  submitted,
            "total":            total,
            "active_commands":  active,
            "max_active":       _MAX_ACTIVE,
            "batch_size":       _BATCH_SIZE,
            "batches_remaining":n_batches,
            "next_batch_start": next_start,
            "next_batch_end":   next_end,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _sanity_check(cand: dict, current_prices: dict) -> tuple[bool, str]:
    """Return (ok, reason). Fails if order would fill immediately or entry is unreachable."""
    sym   = cand["symbol"]
    price = current_prices.get(sym)
    if price is None:
        return False, "no live price"

    entry      = cand["entry_price"]
    direction  = cand["direction"]
    entry_type = cand["entry_type"]
    avg_move   = max(cand.get("avg_move") or 0, _MIN_AVG_MOVE.get(sym, 16.0))

    # Distance gate: entry must be within 2 avg_moves of current price
    if abs(price - entry) > 2.0 * avg_move:
        return False, f"entry {entry} too far from market {price:.2f} (>{2*avg_move:.1f})"

    # Direction-type check: will this fill immediately or is it pending correctly?
    if entry_type in ("LMT", "LMT+STP"):
        if direction == "BUY" and price <= entry:
            return False, f"BUY LMT {entry} but market {price:.2f} already at/below entry"
        if direction == "SELL" and price >= entry:
            return False, f"SELL LMT {entry} but market {price:.2f} already at/above entry"
    elif entry_type == "STP":
        if direction == "BUY" and price >= entry:
            return False, f"BUY STP {entry} but market {price:.2f} already at/above stop"
        if direction == "SELL" and price <= entry:
            return False, f"SELL STP {entry} but market {price:.2f} already at/below stop"

    return True, "ok"


@app.route("/api/algo/submit", methods=["POST"])
def api_submit():
    db_path = _resolve_db()

    with get_db(db_path) as con:
        sess = con.execute(
            "SELECT value FROM system_state WHERE key='ALGO_SESSION_ID'"
        ).fetchone()
    if not sess:
        return jsonify({"error": "No active queue — run Create Trades first."}), 400
    session_id = sess["value"]

    with get_db(db_path) as con:
        active = con.execute(
            "SELECT COUNT(*) FROM commands WHERE source='algo_dashboard'"
            " AND status IN ('PENDING','SUBMITTING','SUBMITTED')"
        ).fetchone()[0]
    if active >= _MAX_ACTIVE:
        return jsonify({
            "error": f"Too many active orders ({active}/{_MAX_ACTIVE}). Cancel some first."
        }), 400

    slots = min(_BATCH_SIZE, _MAX_ACTIVE - active)

    # Fetch more than slots to have candidates to scan through after demotions
    with get_db(db_path) as con:
        scan_pool = [dict(r) for r in con.execute(
            "SELECT * FROM algo_candidates WHERE session_id=? AND queued_status='QUEUED'"
            " ORDER BY rank ASC LIMIT ?",
            (session_id, slots * 5)
        ).fetchall()]
        max_rank = con.execute(
            "SELECT MAX(rank) FROM algo_candidates WHERE session_id=?", (session_id,)
        ).fetchone()[0] or 0

    if not scan_pool:
        return jsonify({"error": "Queue empty — run Create Trades to generate a new queue."}), 400

    # Fetch live prices once
    current_prices = {}
    for sym in ALL_SYMBOLS:
        info = _fetch_price(sym)
        if info["price"] is not None:
            current_prices[sym] = info["price"]

    # Build set of entry keys already active in commands (avoid duplicate positions)
    with get_db(db_path) as con:
        active_cmds = con.execute(
            "SELECT symbol, direction, entry_type, entry_price FROM commands"
            " WHERE source='algo_dashboard'"
            " AND status IN ('PENDING','SUBMITTING','SUBMITTED','FILLED')"
        ).fetchall()
    active_entry_keys = {
        (r["symbol"], r["direction"], r["entry_type"], round(r["entry_price"], 4))
        for r in active_cmds
    }

    # Walk ranked candidates: pass → submit, fail → demote to bottom
    to_submit     = []
    to_demote     = []
    demote_ctr    = 0
    transient_reasons: set = set()   # non-duplicate sanity failures (market-distance, direction)
    for c in scan_pool:
        if len(to_submit) >= slots:
            break
        # Skip if an active command already holds this entry price.
        # LMT+STP candidates expand to one leg — check both LMT and STP keys.
        _etypes_check = ["LMT", "STP"] if c["entry_type"] == "LMT+STP" else [c["entry_type"]]
        entry_keys = {
            (c["symbol"], c["direction"], et, round(c["entry_price"], 4))
            for et in _etypes_check
        }
        if entry_keys & active_entry_keys:
            demote_ctr += 1
            to_demote.append((c["id"], max_rank + demote_ctr, "duplicate entry"))
            continue
        ok, reason = _sanity_check(c, current_prices)
        if ok:
            to_submit.append(c)
            active_entry_keys.update(entry_keys)  # prevent same key twice in this batch
        else:
            # Duplicates get demoted — they resolve only when the active position clears.
            # Market-distance and direction failures are transient: leave rank unchanged
            # so they stay in queue order and pass on the next submit when price has moved.
            transient_reasons.add(reason)

    if not to_submit:
        with get_db(db_path) as con:
            for (cid, new_rank, _) in to_demote:
                con.execute("UPDATE algo_candidates SET rank=? WHERE id=?", (new_rank, cid))
        all_reasons = list({"duplicate entry"} & {r for _, _, r in to_demote} | transient_reasons)
        return jsonify({
            "submitted": 0,
            "demoted":   len(to_demote),
            "reasons":   all_reasons[:5],
            "error":     f"All {len(scan_pool)} candidates failed sanity. Demoted to end of queue.",
        }), 200

    # Build command rows for passing candidates
    rows = []
    for c in to_submit:
        if c["entry_type"] == "LMT+STP":
            # Only submit the leg that is valid for current market position.
            # LMT needs market on the far side (BUY: price>entry, SELL: price<entry).
            # STP needs market on the near side (BUY: price<entry, SELL: price>entry).
            price = current_prices.get(c["symbol"])
            entry = c["entry_price"]
            direction = c["direction"]
            valid = []
            if price is None:
                valid = ["LMT"]  # no price → conservative: LMT only
            else:
                if direction == "BUY":
                    if price > entry:
                        valid.append("LMT")
                    if price < entry:
                        valid.append("STP")
                else:  # SELL
                    if price < entry:
                        valid.append("LMT")
                    if price > entry:
                        valid.append("STP")
            if not valid:
                continue  # price == entry exactly: neither leg is safe, skip
            etypes = valid
        else:
            etypes = [c["entry_type"]]
        for etype in etypes:
            rows.append((
                c["symbol"],
                c.get("entry_line_price") or 0,
                c.get("entry_line_type") or "",
                c.get("entry_line_strength") or 1,
                c["direction"], etype,
                c["entry_price"], c["tp_price"], c["sl_price"],
                c["bracket"], "algo_dashboard",
                c.get("entry_line_id"),
                1,
            ))

    submit_ids = [c["id"] for c in to_submit]
    demote_ids = [(cid, new_rank) for cid, new_rank, _ in to_demote]

    with get_db(db_path) as con:
        con.executemany("""
            INSERT INTO commands
                (symbol, line_price, line_type, line_strength,
                 direction, entry_type, entry_price, tp_price, sl_price,
                 bracket_size, source, critical_line_id, quantity, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING')
        """, rows)
        con.execute(
            f"UPDATE algo_candidates SET queued_status='SUBMITTED'"
            f" WHERE id IN ({','.join('?'*len(submit_ids))})",
            submit_ids
        )
        for (cid, new_rank) in demote_ids:
            con.execute("UPDATE algo_candidates SET rank=? WHERE id=?", (new_rank, cid))

    with get_db(db_path) as con:
        remaining = con.execute(
            "SELECT COUNT(*) FROM algo_candidates WHERE session_id=? AND queued_status='QUEUED'",
            (session_id,)
        ).fetchone()[0]

    return jsonify({
        "submitted":           len(rows),
        "candidates_consumed": len(to_submit),
        "demoted":             len(to_demote),
        "active_after":        active + len(rows),
        "remaining_queued":    remaining,
    })


@app.route("/api/algo/clear-errors", methods=["POST"])
def api_clear_errors():
    """Flip all ERROR algo_dashboard commands to CANCELLED and remove from display."""
    db_path = _resolve_db()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db(db_path) as con:
        n = con.execute(
            "UPDATE commands SET status='CANCELLED', updated_at=?"
            " WHERE source='algo_dashboard' AND status='ERROR'",
            (now_str,)
        ).rowcount
    return jsonify({"cleared": n})


@app.route("/api/algo/purge", methods=["POST"])
def api_purge():
    """Cancel all open algo_dashboard commands (PENDING/SUBMITTED/FILLED/CLOSED/ERROR).

    PENDING/CLOSED/ERROR: pure DB flip, no open IB orders.
    SUBMITTED: cancel all 3 bracket legs in IB, then flip DB.
    FILLED: cancel TP+SL child orders in IB (entry already done), then flip DB.
    """
    db_path = _resolve_db()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 0 — DB-only statuses: PENDING, CLOSED, ERROR
    with get_db(db_path) as con:
        n_db_only = con.execute(
            "UPDATE commands SET status='CANCELLED', updated_at=?"
            " WHERE source='algo_dashboard' AND status IN ('PENDING','CLOSED','ERROR')",
            (now_str,)
        ).rowcount

    # Fetch SUBMITTED and FILLED rows — both need IB cancels
    with get_db(db_path) as con:
        sub_rows = [dict(r) for r in con.execute(
            "SELECT id, status, ib_order_id, ib_tp_order_id, ib_sl_order_id"
            " FROM commands WHERE source='algo_dashboard' AND status='SUBMITTED'"
        ).fetchall()]
        fil_rows = [dict(r) for r in con.execute(
            "SELECT id, status, ib_order_id, ib_tp_order_id, ib_sl_order_id"
            " FROM commands WHERE source='algo_dashboard' AND status='FILLED'"
        ).fetchall()]

    n_submitted = len(sub_rows)
    n_filled    = len(fil_rows)
    n_ib_errors = 0

    ib_rows = sub_rows + fil_rows
    if ib_rows:
        try:
            from lib.ib_client import IBClient
            from lib.config_loader import get_config
            from ib_insync import Order as IBOrder
            cfg    = get_config(_ROOT / "trader" / "config.yaml")
            client = IBClient(cfg)
            client.connect(live=False, paper=True)
            try:
                for row in ib_rows:
                    # SUBMITTED: cancel entry + TP + SL
                    # FILLED: entry is done — cancel TP + SL only (entry cancel would error harmlessly)
                    for oid in (row["ib_order_id"], row["ib_tp_order_id"], row["ib_sl_order_id"]):
                        if oid is None:
                            continue
                        try:
                            o = IBOrder()
                            o.orderId = int(oid)
                            client.cancel_order(o)
                        except Exception:
                            n_ib_errors += 1
                import time; time.sleep(1)
            finally:
                client.disconnect()
        except Exception as e:
            n_ib_errors += len(ib_rows)

        # Always flip DB regardless of IB result
        cmd_ids = [r["id"] for r in ib_rows]
        with get_db(db_path) as con:
            con.execute(
                f"UPDATE commands SET status='CANCELLED', updated_at=?"
                f" WHERE id IN ({','.join('?'*len(cmd_ids))})",
                [now_str] + cmd_ids
            )

    total = n_db_only + n_submitted + n_filled
    return jsonify({"cancelled": total, "db_only": n_db_only,
                    "submitted": n_submitted, "filled": n_filled,
                    "ib_errors": n_ib_errors})


@app.route("/api/algo/trades")
def api_algo_trades():
    sym            = request.args.get("symbol")
    status         = request.args.get("status")
    exclude_status = request.args.get("exclude_status", "")
    algo           = request.args.get("algo_type")
    limit          = min(int(request.args.get("limit", 500)), 2000)

    db_path = _resolve_db()
    filters = ["source='algo_dashboard'"]
    params  = []
    if sym:
        filters.append("symbol=?"); params.append(sym)
    if status:
        filters.append("status=?"); params.append(status)
    if exclude_status and not status:
        ex_list = [s.strip() for s in exclude_status.split(",") if s.strip()]
        if ex_list:
            filters.append(f"status NOT IN ({','.join('?'*len(ex_list))})")
            params.extend(ex_list)

    where = " AND ".join(filters)
    with get_db(db_path) as con:
        rows = con.execute(
            f"SELECT * FROM commands WHERE {where} ORDER BY id DESC LIMIT ?",
            params + [limit]
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/algo/stats")
def api_algo_stats():
    db_path = _resolve_db()
    with get_db(db_path) as con:
        by_sym_rows = con.execute("""
            SELECT symbol,
                   SUM(CASE WHEN status IN ('PENDING','SUBMITTING','SUBMITTED') THEN 1 ELSE 0 END) as active,
                   SUM(CASE WHEN status='FILLED'    THEN 1 ELSE 0 END) as filled,
                   SUM(CASE WHEN status='CLOSED'    THEN 1 ELSE 0 END) as closed
            FROM commands WHERE source='algo_dashboard'
            GROUP BY symbol
        """).fetchall()

        sc = con.execute("""
            SELECT status, COUNT(*) as n FROM commands
            WHERE source='algo_dashboard'
            GROUP BY status
        """).fetchall()
        status_counts = {r["status"]: r["n"] for r in sc}

        total_active = (status_counts.get("PENDING",0) + status_counts.get("SUBMITTING",0)
                        + status_counts.get("SUBMITTED",0) + status_counts.get("FILLED",0))

        # P&L from closed algo_dashboard trades today
        pnl_row = con.execute("""
            SELECT
                SUM(CASE WHEN pnl_points > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_points < 0 THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN pnl_points > 0 THEN pnl_points ELSE 0 END) as gains,
                SUM(CASE WHEN pnl_points < 0 THEN pnl_points ELSE 0 END) as loss_total,
                SUM(pnl_points) as net
            FROM commands
            WHERE source='algo_dashboard' AND status='CLOSED'
              AND date(updated_at) = date('now')
        """).fetchone()

    by_sym = {r["symbol"]: dict(r) for r in by_sym_rows}
    pnl = dict(pnl_row) if pnl_row else {}

    return jsonify({
        "by_symbol":     by_sym,
        "total_active":  total_active,
        "max":           _MAX_CMDS,
        "status_counts": status_counts,
        "pnl_today":     pnl,
    })


@app.route("/api/algo/trade/<int:cmd_id>")
def api_algo_trade_detail(cmd_id):
    db_path = _resolve_db()
    with get_db(db_path) as con:
        cmd = con.execute("SELECT * FROM commands WHERE id=?", (cmd_id,)).fetchone()
        if not cmd:
            return jsonify({"error": "Not found"}), 404
        line = None
        if cmd["critical_line_id"]:
            line = con.execute(
                "SELECT * FROM critical_lines WHERE id=?", (cmd["critical_line_id"],)
            ).fetchone()
    result = dict(cmd)
    if line:
        result["critical_line"] = dict(line)
    return jsonify(result)


@app.route("/api/algo/lines", methods=["GET"])
def api_algo_lines_list():
    db_path = _resolve_db()
    symbol  = request.args.get("symbol")
    armed   = request.args.get("armed")
    filters = []
    params  = []
    if symbol:
        filters.append("symbol=?"); params.append(symbol)
    if armed is not None:
        filters.append("armed=?"); params.append(1 if armed == "1" else 0)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    with get_db(db_path) as con:
        rows = con.execute(
            f"SELECT * FROM critical_lines {where} ORDER BY date DESC, symbol, price",
            params
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/algo/lines", methods=["POST"])
def api_algo_lines_add():
    body     = request.get_json(silent=True) or {}
    symbol   = body.get("symbol", "").upper().strip()
    date_str = body.get("date", "").strip()
    line_type= body.get("line_type", "").upper().strip()
    price    = body.get("price")
    strength = int(body.get("strength", 2))

    if symbol not in ALL_SYMBOLS:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 400
    if line_type not in ("SUPPORT", "RESISTANCE"):
        return jsonify({"error": "line_type must be SUPPORT or RESISTANCE"}), 400
    if price is None:
        return jsonify({"error": "price required"}), 400
    if not date_str:
        return jsonify({"error": "date required"}), 400
    if strength not in (1, 2, 3):
        strength = 2

    db_path = _resolve_db()
    with get_db(db_path) as con:
        cur = con.execute(
            "INSERT INTO critical_lines(symbol,date,line_type,price,strength,armed)"
            " VALUES(?,?,?,?,?,1)",
            (symbol, date_str, line_type, float(price), strength)
        )
        new_id = cur.lastrowid
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/algo/lines/<int:line_id>", methods=["DELETE"])
def api_algo_lines_delete(line_id: int):
    db_path = _resolve_db()
    with get_db(db_path) as con:
        n = con.execute("DELETE FROM critical_lines WHERE id=?", (line_id,)).rowcount
    return jsonify({"ok": True, "deleted": n})


@app.route("/api/algo/lines/<int:line_id>/arm", methods=["POST"])
def api_algo_lines_arm(line_id: int):
    body    = request.get_json(silent=True) or {}
    armed   = 1 if body.get("armed", True) else 0
    db_path = _resolve_db()
    with get_db(db_path) as con:
        n = con.execute("UPDATE critical_lines SET armed=? WHERE id=?", (armed, line_id)).rowcount
    return jsonify({"ok": True, "updated": n})


# ── Self-test ─────────────────────────────────────────────────────────────────

def _self_test() -> bool:
    import tempfile, csv, math
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    _UTC = ZoneInfo("UTC")

    print("Running algo_dashboard self-test...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p    = Path(tmp)
            hist_dir = tmp_p / "history"
            hist_dir.mkdir()
            db_path  = tmp_p / "galao.db"
            init_db(db_path)

            # Seed critical lines
            with get_db(db_path) as con:
                con.executemany(
                    "INSERT INTO critical_lines(symbol,date,line_type,price,strength,armed)"
                    " VALUES(?,?,?,?,?,?)", [
                        ("MES", "2026-07-05", "SUPPORT",    5490.0, 2, 1),
                        ("MES", "2026-07-05", "SUPPORT",    5500.0, 1, 1),
                        ("MES", "2026-07-05", "RESISTANCE", 5510.0, 1, 1),
                    ]
                )

            # Prior-day trades CSV for avg_move computation
            base   = datetime(2026, 7, 4, 13, 30, 0, tzinfo=_UTC)
            prices = [round(5500.0 + 15.0 * math.sin(i / 30.0), 2) for i in range(200)]
            with open(hist_dir / "MES_trades_20260704.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time_utc", "price", "size"])
                for i, p in enumerate(prices):
                    w.writerow([(base + timedelta(seconds=i * 60)).isoformat(), p, 100])

            # Override module-level path resolvers (works for __main__ and imported)
            g = globals()
            g["_DB_PATH_OVERRIDE"]   = db_path
            g["_HIST_PATH_OVERRIDE"] = hist_dir

            cands, meta = _generate_candidates(["MES"], {"MES": 5503.0})

            g["_DB_PATH_OVERRIDE"]   = None
            g["_HIST_PATH_OVERRIDE"] = None

        assert len(cands) > 0,                               "Should generate candidates"
        assert all("combined_score" in c for c in cands),    "All candidates need combined_score"
        assert all("rank" in c for c in cands),              "All candidates need rank"
        assert cands[0]["rank"] == 1,                        "Top candidate rank should be 1"
        assert cands[0]["combined_score"] >= cands[-1]["combined_score"], "Should be sorted"

        # Verify U-shape entry: support 5500 → entry 5500.25
        fd = [c for c in cands if c["command_class"] == "FD"]
        assert fd, "Should have FD candidates"
        sup5500 = [c for c in fd if c["entry_line_price"] == 5500.0]
        assert sup5500, "Should have FD candidates from 5500 support"
        assert abs(sup5500[0]["entry_price"] - 5500.25) < 0.01, \
            f"Support 5500 FD entry should be 5500.25, got {sup5500[0]['entry_price']}"

        # Verify TP is resistance - 1 tick for CL-sourced TP
        cl_tp = [c for c in sup5500 if c["tp_source"] == "critical_line"]
        if cl_tp:
            assert abs(cl_tp[0]["tp_price"] - 5509.75) < 0.01, \
                f"TP at 5510 resistance should be 5509.75, got {cl_tp[0]['tp_price']}"

        print(f"PASS -- algo_dashboard: {len(cands)} candidates, "
              f"rank-1 score={cands[0]['combined_score']:.3f}, "
              f"FD={sum(1 for c in cands if c['command_class']=='FD')}, "
              f"HD={sum(1 for c in cands if c['command_class']=='HD')}")
        return True

    except Exception as e:
        import traceback
        print(f"FAIL -- {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Algo Dashboard")
    parser.add_argument("--port",      type=int, default=5002)
    parser.add_argument("--host",      default="0.0.0.0")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    db_path = _resolve_db()
    init_db(db_path)
    _ensure_tables(db_path)
    print(f"Algo Dashboard starting on http://{args.host}:{args.port}")
    print(f"DB: {db_path}")
    app.run(host=args.host, port=args.port, debug=False)
