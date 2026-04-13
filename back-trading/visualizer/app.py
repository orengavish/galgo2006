"""
back-trading/visualizer/app.py
Dashboard for the back-trading engine.

Modes:
  SIM  — historical simulation (any past date, no IB needed)
  LIVE — reality model (today only, submits to IB paper, traces fills live)

Usage:
    cd back-trading
    python visualizer/app.py
    → http://127.0.0.1:5001

Self-test:
    python visualizer/app.py --self-test
"""

import sys
import argparse
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

_HERE = Path(__file__).parent                    # back-trading/visualizer/
_BT   = _HERE.parent                            # back-trading/
_ROOT = _BT.parent                              # galgo2026/
_TRADER = _ROOT / "trader"
for p in [str(_ROOT), str(_BT), str(_TRADER)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from flask import Flask, jsonify, request, render_template
from lib.config_loader import get_config
from lib.logger import get_logger

log = get_logger("bt_visualizer")

app = Flask(__name__, template_folder="templates")

# ── Shared session state ──────────────────────────────────────────────────────

_lock    = threading.Lock()
_session = {
    "mode":        "sim",       # "sim" | "live"
    "date":        None,        # date object
    "status":      "idle",      # idle | generating | simulating | scheduled | running | done | error
    "symbol":      "MES",
    "timestamps":  [],          # scheduled timestamps (live mode preview)
    "orders":      [],          # list of order dicts
    "fills":       [],          # list of fill result dicts (indexed by order position)
    "ib_events":   [],          # list of {time, type, msg}
    "error":       None,
    "pnl":         None,
}
_live_thread: threading.Thread | None = None


def _set(key, val):
    with _lock:
        _session[key] = val


def _get(key):
    with _lock:
        return _session[key]


def _append_event(etype: str, msg: str):
    with _lock:
        _session["ib_events"].append({
            "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "type": etype,
            "msg":  msg,
        })


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with _lock:
        s = dict(_session)
        s["date"]       = s["date"].isoformat() if s["date"] else None
        s["timestamps"] = [t.isoformat() for t in s["timestamps"]]
        # orders: convert datetime fields
        orders_out = []
        for o in s["orders"]:
            od = dict(o)
            if isinstance(od.get("ts_placed"), datetime):
                od["ts_placed"] = od["ts_placed"].isoformat()
            orders_out.append(od)
        s["orders"] = orders_out
        fills_out = []
        for f in s["fills"]:
            fd = dict(f) if f else {}
            for k in ("entry_fill_time", "exit_fill_time", "ts_placed"):
                if isinstance(fd.get(k), datetime):
                    fd[k] = fd[k].isoformat()
            fills_out.append(fd)
        s["fills"] = fills_out
    return jsonify(s)


@app.route("/api/generate", methods=["POST"])
def api_generate():
    """
    Generate orders for a date.
    SIM mode:  needs tick data (fetches from IB if missing), runs full simulation.
    LIVE mode: generates timestamps only (no tick data), prices are TBD until runtime.
    Body: { date: "YYYY-MM-DD", mode: "sim"|"live", symbol: "MES" }
    """
    body   = request.json or {}
    d_str  = body.get("date") or date.today().isoformat()
    mode   = body.get("mode", "sim")
    symbol = body.get("symbol", "MES")

    try:
        target_date = date.fromisoformat(d_str)
    except ValueError:
        return jsonify({"error": f"Invalid date: {d_str}"}), 400

    with _lock:
        if _session["status"] in ("running", "scheduled"):
            return jsonify({"error": "Session already running"}), 409
        _session.update({
            "mode": mode, "date": target_date, "symbol": symbol,
            "status": "generating", "orders": [], "fills": [],
            "timestamps": [], "ib_events": [], "error": None, "pnl": None,
        })

    if mode == "sim":
        threading.Thread(target=_run_sim, args=(target_date, symbol), daemon=True).start()
    else:
        threading.Thread(target=_run_live_generate, args=(target_date, symbol), daemon=True).start()

    return jsonify({"ok": True, "mode": mode, "date": d_str})


@app.route("/api/send", methods=["POST"])
def api_send():
    """Start the live reality model — submit scheduled orders to IB paper."""
    with _lock:
        if _session["status"] not in ("scheduled",):
            return jsonify({"error": "Must generate (live mode) first"}), 409
        if _session["mode"] != "live":
            return jsonify({"error": "Only available in live mode"}), 409
        _session["status"] = "running"

    global _live_thread
    _live_thread = threading.Thread(
        target=_run_live_session,
        args=(_get("date"), _get("symbol")),
        daemon=True,
    )
    _live_thread.start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Cancel all open paper orders and stop the live session."""
    _set("status", "idle")
    _append_event("STOP", "Stop requested by user")
    # Signal the thread to exit (it checks _session["status"])
    return jsonify({"ok": True})


@app.route("/api/ib-events")
def api_ib_events():
    with _lock:
        return jsonify(_session["ib_events"])


# ── Sim worker ────────────────────────────────────────────────────────────────

def _run_sim(target_date: date, symbol: str):
    """Background thread: load tick data, generate orders, simulate fills."""
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        cfg = get_config()
        gcfg = cfg.generator
        _set("status", "generating")

        # Import here to avoid circular imports at module load
        import generator as gen
        import simulator as sim
        from fetcher import get_session_bounds

        # Ensure tick data
        bars = Path(cfg.paths.bars)
        d    = target_date.strftime("%Y%m%d")
        trades_path = bars / f"{symbol}_trades_{d}.csv"
        bidask_path = bars / f"{symbol}_bidask_{d}.csv"

        if not trades_path.exists() or not bidask_path.exists():
            _append_event("INFO", f"Tick data not cached — fetching {symbol} {target_date}")
            _set("status", "fetching")
            _fetch_ticks_for_date(cfg, symbol, target_date)

        import pandas as pd
        trades_df = pd.read_csv(trades_path)
        trades_df["time_utc"] = pd.to_datetime(trades_df["time_utc"], utc=True)
        bidask_df = None
        if bidask_path.exists():
            bidask_df = pd.read_csv(bidask_path)
            bidask_df["time_utc"] = pd.to_datetime(bidask_df["time_utc"], utc=True)

        _append_event("INFO", f"Loaded {len(trades_df):,} trade ticks")
        _set("status", "generating")

        orders = gen.generate(
            trades_df        = trades_df,
            target_date      = target_date,
            bracket_sizes    = list(gcfg.bracket_sizes),
            n_timestamps     = gcfg.n_timestamps,
            entry_offset_min = gcfg.entry_offset_min,
            entry_offset_max = gcfg.entry_offset_max,
            symbol           = symbol,
        )
        _set("orders", orders)
        _append_event("INFO", f"Generated {len(orders)} orders")

        _set("status", "simulating")
        _, session_end = get_session_bounds(target_date)
        fills = sim.simulate(orders, trades_df, bidask_df, session_end)
        _set("fills", fills)

        total_pnl = sum(r["pnl"] for r in fills if r.get("pnl") is not None)
        n_tp  = sum(1 for r in fills if r.get("exit_type") == "TP")
        n_sl  = sum(1 for r in fills if r.get("exit_type") == "SL")
        n_exp = sum(1 for r in fills if r.get("exit_type") == "EXPIRED")
        _set("pnl", round(total_pnl, 2))
        _append_event("RESULT", f"P&L ${total_pnl:+.2f} | TP:{n_tp} SL:{n_sl} EXP:{n_exp}")
        _set("status", "done")

    except Exception as e:
        _set("error", str(e))
        _set("status", "error")
        _append_event("ERROR", str(e))
        log.error(f"Sim worker error: {e}", exc_info=True)


# ── Live generate worker ──────────────────────────────────────────────────────

def _run_live_generate(target_date: date, symbol: str):
    """Generate timestamps for a live session (no tick data needed)."""
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        cfg  = get_config()
        gcfg = cfg.generator
        import generator as gen

        timestamps = gen.generate_live_timestamps(
            target_date  = target_date,
            n_timestamps = gcfg.n_timestamps,
        )
        with _lock:
            _session["timestamps"] = timestamps
            _session["status"]     = "scheduled"
            # Build placeholder orders (prices TBD)
            placeholder_orders = []
            for ts in timestamps:
                for bs in list(gcfg.bracket_sizes):
                    for d in ("BUY", "SELL"):
                        placeholder_orders.append({
                            "ts_placed":    ts,
                            "direction":    d,
                            "entry_type":   "LMT",
                            "entry_price":  None,
                            "tp_price":     None,
                            "sl_price":     None,
                            "bracket_size": float(bs),
                            "market_price": None,
                            "entry_offset": None,
                            "symbol":       symbol,
                            "status":       "SCHEDULED",
                        })
            _session["orders"] = placeholder_orders
            _session["fills"]  = [None] * len(placeholder_orders)

        _append_event("INFO",
            f"{len(timestamps)} timestamps scheduled "
            f"{timestamps[0].strftime('%H:%M') if timestamps else '—'} → "
            f"{timestamps[-1].strftime('%H:%M') if timestamps else '—'} CT")

    except Exception as e:
        _set("error", str(e))
        _set("status", "error")
        _append_event("ERROR", str(e))


# ── Live session worker ───────────────────────────────────────────────────────

def _run_live_session(target_date: date, symbol: str):
    """
    Background thread for live reality model.
    asyncio event loop is created here because ib_insync requires one per thread.
    Submits orders to IB paper at their scheduled timestamps.
    Collects fills via execDetailsEvent.
    """
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        cfg  = get_config()
        gcfg = cfg.generator

        from lib.ib_client import IBClient
        from lib.order_builder import build_bracket, place_bracket
        import generator as gen

        _append_event("CONN", "Connecting to IB paper...")
        ibc = IBClient(cfg)
        ibc.connect(live=True, paper=True)
        contract = ibc.get_contract(symbol)
        _append_event("CONN", f"Connected — {symbol} contract resolved")

        # Fill event → update fills in session
        # Map: ib_order_id → (order_idx, leg: entry/tp/sl)
        _ib_map: dict = {}   # ib_id → dict with order info
        _pending: dict = {}  # order_idx → info dict

        def on_fill(trade, fill):
            ib_id = fill.execution.orderId
            price = fill.execution.price
            ts    = datetime.now(timezone.utc).isoformat()
            info  = _ib_map.get(ib_id)
            if not info:
                return
            idx = info["order_idx"]
            if ib_id == info["entry_id"]:
                info["entry_fill_price"] = price
                info["entry_fill_time"]  = ts
                info["entry_filled"]     = True
                with _lock:
                    if idx < len(_session["orders"]):
                        _session["orders"][idx]["status"] = "OPEN"
                _append_event("FILL", f"[{idx}] entry {info['direction']} @ {price:.2f}")
            elif ib_id in (info["tp_id"], info["sl_id"]):
                etype = "TP" if ib_id == info["tp_id"] else "SL"
                ep    = info.get("entry_fill_price")
                pnl   = None
                if ep:
                    diff = (price - ep) if info["direction"] == "BUY" else (ep - price)
                    pnl  = round(diff * 5.0, 2)
                fill_result = {
                    "order_idx":         idx,
                    "direction":         info["direction"],
                    "entry_price":       info.get("entry_price"),
                    "tp_price":          info.get("tp_price"),
                    "sl_price":          info.get("sl_price"),
                    "bracket_size":      info.get("bracket_size"),
                    "market_price":      info.get("market_price"),
                    "ts_placed":         info.get("ts_placed"),
                    "entry_fill_price":  ep,
                    "entry_fill_time":   info.get("entry_fill_time"),
                    "exit_type":         etype,
                    "exit_fill_price":   price,
                    "exit_fill_time":    ts,
                    "pnl":               pnl,
                }
                with _lock:
                    if idx < len(_session["fills"]):
                        _session["fills"][idx] = fill_result
                    if idx < len(_session["orders"]):
                        _session["orders"][idx]["status"] = etype
                _append_event("FILL",
                    f"[{idx}] {etype} @ {price:.2f}  P&L ${pnl:+.2f}" if pnl is not None
                    else f"[{idx}] {etype} @ {price:.2f}")
                _pending.pop(idx, None)

        ibc.paper.execDetailsEvent += on_fill

        # Walk through scheduled timestamps
        with _lock:
            timestamps = list(_session["timestamps"])
            bracket_sizes = list(gcfg.bracket_sizes)

        # Group by timestamp (each timestamp has 2*len(brackets) orders)
        orders_per_ts = len(bracket_sizes) * 2
        ts_idx = 0

        for ts_i, ts in enumerate(timestamps):
            # Wait for this timestamp (check status every second)
            while datetime.now(timezone.utc) < ts:
                if _get("status") != "running":
                    _append_event("STOP", "Session stopped by user")
                    return
                ibc.paper.sleep(1)

            if _get("status") != "running":
                break

            # Fetch live price
            try:
                price = ibc.get_price(symbol)
                _append_event("PRICE", f"Live price @ {ts.strftime('%H:%M:%S')}: {price:.2f}")
            except Exception as e:
                _append_event("WARN", f"Price fetch failed at {ts}: {e}")
                ts_idx += orders_per_ts
                continue

            # Generate actual orders for this timestamp
            new_orders = gen.make_orders_for_price(
                ts               = ts,
                market_price     = price,
                bracket_sizes    = bracket_sizes,
                entry_offset_min = gcfg.entry_offset_min,
                entry_offset_max = gcfg.entry_offset_max,
                symbol           = symbol,
            )

            # Submit each bracket
            for local_i, order in enumerate(new_orders):
                idx = ts_idx + local_i
                # Update placeholder in session with real prices
                with _lock:
                    if idx < len(_session["orders"]):
                        _session["orders"][idx].update({
                            "entry_price":  order["entry_price"],
                            "tp_price":     order["tp_price"],
                            "sl_price":     order["sl_price"],
                            "market_price": order["market_price"],
                            "entry_offset": order["entry_offset"],
                            "status":       "SUBMITTING",
                        })
                try:
                    ib_orders = build_bracket(
                        ibc.paper, contract,
                        order["direction"], order["entry_type"],
                        order["entry_price"], order["tp_price"], order["sl_price"],
                    )
                    placed = place_bracket(ibc.paper, contract, ib_orders)
                    info = {
                        "order_idx":   idx,
                        "direction":   order["direction"],
                        "entry_price": order["entry_price"],
                        "tp_price":    order["tp_price"],
                        "sl_price":    order["sl_price"],
                        "bracket_size": order["bracket_size"],
                        "market_price": order["market_price"],
                        "ts_placed":   ts,
                        "entry_id":    placed["entry_id"],
                        "tp_id":       placed["tp_id"],
                        "sl_id":       placed["sl_id"],
                        "entry_filled": False,
                        "entry_fill_price": None,
                        "entry_fill_time":  None,
                    }
                    for ib_id in (placed["entry_id"], placed["tp_id"], placed["sl_id"]):
                        _ib_map[ib_id] = info
                    _pending[idx] = info
                    with _lock:
                        _session["orders"][idx]["status"] = "SUBMITTED"
                    _append_event("ORDER",
                        f"[{idx}] {order['direction']} {order['entry_type']} "
                        f"@ {order['entry_price']} bracket={order['bracket_size']}")
                except Exception as e:
                    _append_event("ERROR", f"Submit [{idx}]: {e}")
                    with _lock:
                        _session["orders"][idx]["status"] = "ERROR"

            ts_idx += orders_per_ts

        # Session end: cancel unfilled entries
        _append_event("INFO", "Session end — cancelling unfilled entries")
        for idx, info in list(_pending.items()):
            if not info["entry_filled"]:
                try:
                    for t in ibc.paper.trades():
                        if t.order.orderId == info["entry_id"]:
                            ibc.paper.cancelOrder(t.order)
                            break
                except Exception:
                    pass
                with _lock:
                    if idx < len(_session["orders"]):
                        _session["orders"][idx]["status"] = "EXPIRED"
                    if idx < len(_session["fills"]) and _session["fills"][idx] is None:
                        _session["fills"][idx] = {"exit_type": "EXPIRED", "pnl": None}

        ibc.paper.execDetailsEvent -= on_fill
        ibc.disconnect()

        # Final P&L
        with _lock:
            fills = [f for f in _session["fills"] if f and f.get("pnl") is not None]
            total = sum(f["pnl"] for f in fills)
            _session["pnl"] = round(total, 2)
            _session["status"] = "done"
        _append_event("RESULT", f"Done. P&L ${total:+.2f} ({len(fills)} completed trades)")

    except Exception as e:
        _set("error", str(e))
        _set("status", "error")
        _append_event("ERROR", f"Live session error: {e}")
        log.error(f"Live session error: {e}", exc_info=True)


# ── Tick fetch helper ─────────────────────────────────────────────────────────

def _fetch_ticks_for_date(cfg, symbol: str, target_date: date):
    """Fetch TRADES + BID_ASK for a date using trader/fetcher.py."""
    import csv, random
    from ib_insync import IB
    from fetcher import (get_contract_for_date, get_session_bounds,
                         paginate_ticks, _init_progress_db)
    from zoneinfo import ZoneInfo
    CT_tz = ZoneInfo("America/Chicago")

    bars = Path(cfg.paths.bars)
    bars.mkdir(parents=True, exist_ok=True)
    prog_db = bars.parent / "fetch_progress.db"
    progress_conn = _init_progress_db(prog_db)

    ib = IB()
    ids = list(getattr(cfg.ib, "fetcher_client_ids", cfg.ib.live_client_ids))
    random.shuffle(ids)
    for cid in ids:
        try:
            ib.connect(cfg.ib.live_host, cfg.ib.live_port,
                       clientId=cid, timeout=cfg.ib.connection_timeout)
            if ib.isConnected():
                break
        except Exception:
            continue

    if not ib.isConnected():
        progress_conn.close()
        raise ConnectionError("Cannot connect to IB for tick data fetch")

    try:
        contract  = get_contract_for_date(ib, symbol, target_date)
        start_utc, end_utc = get_session_bounds(target_date)
        date_str  = target_date.isoformat()
        d_compact = target_date.strftime("%Y%m%d")

        for dtype, suffix, headers in [
            ("TRADES", "trades",
             ["time_ct", "time_utc", "price", "size", "symbol"]),
            ("BID_ASK", "bidask",
             ["time_ct", "time_utc", "bid_p", "bid_s", "ask_p", "ask_s", "symbol"]),
        ]:
            out = bars / f"{symbol}_{suffix}_{d_compact}.csv"
            if out.exists():
                continue
            with open(out, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(headers)
                def _write(tick, t_u, _dt=dtype):
                    t_c = t_u.astimezone(CT_tz)
                    if _dt == "TRADES":
                        w.writerow([t_c.isoformat(), t_u.isoformat(),
                                    tick.price, tick.size, contract.localSymbol])
                    else:
                        w.writerow([t_c.isoformat(), t_u.isoformat(),
                                    tick.priceBid, tick.sizeBid,
                                    tick.priceAsk, tick.sizeAsk,
                                    contract.localSymbol])
                count = paginate_ticks(ib, contract, start_utc, end_utc,
                                       dtype, _write, progress_conn, symbol, date_str)
            _append_event("FETCH", f"{symbol} {dtype} {target_date}: {count:,} ticks")
    finally:
        progress_conn.close()
        ib.disconnect()


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    try:
        cfg = get_config()
        assert hasattr(cfg, "visualizer"), "Config missing visualizer section"
        assert hasattr(cfg, "generator"),  "Config missing generator section"
        # Check Flask routes exist
        with app.test_client() as c:
            r = c.get("/api/status")
            assert r.status_code == 200
            data = r.get_json()
            assert "status" in data
            assert "orders" in data
        print("[self-test] bt_visualizer: PASS")
        return True
    except Exception as e:
        print(f"[self-test] bt_visualizer: FAIL — {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Back-trading dashboard")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    cfg = get_config()
    host = cfg.visualizer.host
    port = cfg.visualizer.port
    print(f"Back-trading dashboard → http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)
