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
import argparse
import threading
import time
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

_TICK      = 0.25
_MAX_CMDS  = 400
_TOP_N     = 100
_HD_TOP_N  = 5           # top N combos from scorer to apply as HD candidates

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
    # Try trader/config.yaml — resolve db path relative to the config file's directory
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


def _resolve_history() -> Path:
    if _HIST_PATH_OVERRIDE:
        return _HIST_PATH_OVERRIDE
    return _resolve_db().parent / "history"


# ── Price helpers ─────────────────────────────────────────────────────────────

def _fetch_price(symbol: str = "MES") -> dict:
    """Try live price from port 5001; fall back to price_cache in DB."""
    try:
        r = requests.get(f"{_TRADER_URL}/api/price", timeout=1.5)
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


def _generate_candidates(symbols: list[str], current_prices: dict) -> tuple[list[dict], dict]:
    """
    Build and score trade candidates in memory.
    Returns (candidates_list, meta_dict).
    """
    db_path    = _resolve_db()
    hist_dir   = _resolve_history()

    with get_db(db_path) as con:
        # Most recent date with armed lines for these symbols
        ph = ",".join("?" * len(symbols))
        date_row = con.execute(
            f"SELECT MAX(date) FROM critical_lines WHERE armed=1 AND symbol IN ({ph})",
            symbols
        ).fetchone()
        date_str = date_row[0] if date_row and date_row[0] else None
        if not date_str:
            return [], {"error": "No armed critical lines found",
                        "n_returned": 0, "n_generated": 0, "n_lines": 0}

        # All armed lines for that date + symbols
        lines_all = [dict(r) for r in con.execute(
            f"SELECT * FROM critical_lines WHERE armed=1 AND date=? AND symbol IN ({ph})",
            [date_str] + list(symbols)
        ).fetchall()]

        # HD: top combos per symbol
        hd_combos: dict[str, list] = {}
        for sym in symbols:
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

        # FD historical P&L: avg by (symbol, entry_line_type, direction, tp_source)
        fd_hist: dict = {}
        rows = con.execute("""
            SELECT symbol, entry_line_type, direction, tp_source,
                   AVG(pnl_ticks) as avg_pnl, COUNT(*) as n
            FROM cl_algo_fd_results
            WHERE entry_fill_price IS NOT NULL
            GROUP BY symbol, entry_line_type, direction, tp_source
        """).fetchall()
        for r in rows:
            fd_hist[(r[0], r[1], r[2], r[3])] = {"avg_pnl": r[4] or 0.0, "n": r[5]}

    candidates = []

    for sym in symbols:
        sym_lines = [l for l in lines_all if l["symbol"] == sym]
        if not sym_lines:
            continue
        cur_price = current_prices.get(sym)

        params = get_day_params(db_path, sym, date_str, hist_dir)
        avg_move  = params["two_hour_avg_move"]
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

    # When all scores are flat (no history, price far from lines), do a round-robin
    # interleave across algo_types so top-100 has algo diversity rather than just
    # the first algo_type alphabetically from a stable sort.
    score_spread = max(c["combined_score"] for c in candidates) - \
                   min(c["combined_score"] for c in candidates)
    if score_spread < 0.001:
        # Sort within each algo_type by secondary keys (strength DESC, tp_ticks, sl_ticks)
        import collections
        groups: dict = collections.defaultdict(list)
        for c in candidates:
            groups[c["algo_type"]].append(c)
        for g in groups.values():
            g.sort(key=lambda c: (-c.get("entry_line_strength", 0),
                                  c.get("tp_ticks", 0), c.get("sl_ticks", 0)))
        # Round-robin across algo_type groups to build top
        from itertools import zip_longest
        top = []
        algo_order = sorted(groups.keys())  # deterministic order
        for batch in zip_longest(*[groups[a] for a in algo_order]):
            for c in batch:
                if c is not None:
                    top.append(c)
                    if len(top) == _TOP_N:
                        break
            if len(top) == _TOP_N:
                break
        # Re-sort the selected top-100 by (entry_line_strength DESC, algo_type) for display
        top.sort(key=lambda c: (-c.get("entry_line_strength", 0), c["algo_type"],
                                c.get("tp_ticks", 0), c.get("sl_ticks", 0)))
    else:
        candidates.sort(key=lambda c: c["combined_score"], reverse=True)
        top = candidates[:_TOP_N]

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

    cands, meta = _generate_candidates(symbols, prices)

    with _candidates_lock:
        _candidates.clear()
        _candidates.extend(cands)
        _candidates_meta.clear()
        _candidates_meta.update(meta)

    meta["price_source_label"] = (
        "live" if any(_fetch_price(s)["source"] == "live" for s in symbols) else "delayed"
    )
    return jsonify({"candidates": cands, "meta": meta})


@app.route("/api/algo/candidates")
def api_candidates():
    with _candidates_lock:
        return jsonify({"candidates": _candidates, "meta": _candidates_meta})


@app.route("/api/algo/submit", methods=["POST"])
def api_submit():
    with _candidates_lock:
        cands = list(_candidates)

    if not cands:
        return jsonify({"error": "No candidates — run Create Trades first."}), 400

    # Require price (any symbol)
    has_price = any(
        _fetch_price(s)["price"] is not None
        for s in {c["symbol"] for c in cands}
    )
    if not has_price:
        return jsonify({"error": "No price available. Start IBC Gateway or wait for delayed feed."}), 400

    db_path = _resolve_db()
    with get_db(db_path) as con:
        active = con.execute(
            "SELECT COUNT(*) FROM commands"
            " WHERE status IN ('PENDING','SUBMITTING','SUBMITTED','FILLED')"
        ).fetchone()[0]

    n_orders = sum(2 if c["entry_type"] == "LMT+STP" else 1 for c in cands)
    if active + n_orders > _MAX_CMDS:
        headroom = max(0, _MAX_CMDS - active)
        msg = (f"Limit already exceeded ({active}/{_MAX_CMDS} active)."
               if headroom == 0 else
               f"Would exceed {_MAX_CMDS}-command limit. "
               f"{active} active + {n_orders} new = {active+n_orders}. "
               f"Can submit at most {headroom} more — cancel some PENDING orders first.")
        return jsonify({"error": msg}), 400

    rows = []
    for c in cands:
        bracket = round(abs(c["tp_price"] - c["entry_price"]), 4)
        # FD candidates carry entry_type="LMT+STP" → expand into two separate orders
        etypes = ["LMT", "STP"] if c["entry_type"] == "LMT+STP" else [c["entry_type"]]
        for etype in etypes:
            rows.append((
                c["symbol"],
                c["entry_line_price"], c["entry_line_type"], c["entry_line_strength"],
                c["direction"], etype,
                c["entry_price"], c["tp_price"], c["sl_price"],
                bracket, "algo_dashboard",
                c.get("entry_line_id"),
                1,  # quantity
            ))

    with get_db(db_path) as con:
        con.executemany("""
            INSERT INTO commands
                (symbol, line_price, line_type, line_strength,
                 direction, entry_type, entry_price, tp_price, sl_price,
                 bracket_size, source, critical_line_id, quantity, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING')
        """, rows)

    # Clear candidates so double-submit is blocked
    with _candidates_lock:
        _candidates.clear()

    return jsonify({"submitted": len(rows), "active_after": active + len(rows)})


@app.route("/api/algo/purge", methods=["POST"])
def api_purge():
    """Cancel (set CANCELLED) all PENDING commands from source='algo_dashboard'."""
    db_path = _resolve_db()
    with get_db(db_path) as con:
        cur = con.execute(
            "UPDATE commands SET status='CANCELLED', updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            " WHERE source='algo_dashboard' AND status='PENDING'"
        )
        n = cur.rowcount
    return jsonify({"cancelled": n})


@app.route("/api/algo/trades")
def api_algo_trades():
    sym    = request.args.get("symbol")
    status = request.args.get("status")
    algo   = request.args.get("algo_type")
    limit  = min(int(request.args.get("limit", 500)), 2000)

    db_path = _resolve_db()
    filters = ["source='algo_dashboard'"]
    params  = []
    if sym:
        filters.append("symbol=?"); params.append(sym)
    if status:
        filters.append("status=?"); params.append(status)

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
        rows = con.execute("""
            SELECT symbol,
                   SUM(CASE WHEN status IN ('PENDING','SUBMITTING','SUBMITTED','FILLED') THEN 1 ELSE 0 END) as active,
                   SUM(CASE WHEN status='FILLED' THEN 1 ELSE 0 END) as filled,
                   SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) as closed
            FROM commands
            WHERE source='algo_dashboard'
            GROUP BY symbol
        """).fetchall()
        total_active = con.execute(
            "SELECT COUNT(*) FROM commands WHERE status IN ('PENDING','SUBMITTING','SUBMITTED','FILLED')"
        ).fetchone()[0]
    by_sym = {r["symbol"]: dict(r) for r in rows}
    return jsonify({"by_symbol": by_sym, "total_active": total_active, "max": _MAX_CMDS})


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
    print(f"Algo Dashboard starting on http://{args.host}:{args.port}")
    print(f"DB: {db_path}")
    app.run(host=args.host, port=args.port, debug=False)
