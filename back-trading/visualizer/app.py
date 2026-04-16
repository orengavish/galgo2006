"""
back-trading/visualizer/app.py
Live paper-trading dashboard.

Flow:
  1. Generate  — pick N random RTH timestamps, show scheduled orders (prices TBD)
  2. Send      — connect IB paper; at each timestamp fetch live price, submit bracket
  3. Trace     — fills stream in via execDetailsEvent, P&L updates in real time

No historical tick data required.

Usage:
    cd back-trading
    python visualizer/app.py
    → http://127.0.0.1:5001

Self-test:
    python visualizer/app.py --self-test
"""

import os
import sys
import signal
import argparse
import threading
import time
import socket as _socket
from datetime import date, datetime, timezone
from pathlib import Path

_HERE   = Path(__file__).parent
_BT     = _HERE.parent
_ROOT   = _BT.parent
_TRADER = _ROOT / "trader"
for p in [str(_ROOT), str(_BT), str(_TRADER)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from flask import Flask, jsonify, request, render_template
from lib.config_loader import get_config
from lib.logger import get_logger

log = get_logger("bt_visualizer")
app = Flask(__name__, template_folder="templates")

# ── Clean Ctrl+C ──────────────────────────────────────────────────────────────

def _sigint(sig, frame):
    print("\n[back-trading] Ctrl+C — exiting")
    os._exit(0)

if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGINT, _sigint)

# ── Shared session state ──────────────────────────────────────────────────────

_lock    = threading.Lock()
_session = {
    "date":       None,
    "status":     "idle",     # idle | generating | scheduled | running | done | error
    "symbol":     "MES",
    "timestamps": [],         # scheduled UTC datetimes
    "orders":     [],         # list of order dicts
    "fills":      [],         # fill result per order (None until filled/expired)
    "ib_events":  [],
    "error":      None,
    "pnl":        None,
    # monitor state
    "ib_live":    False,
    "ib_paper":   False,
    "price":      None,
    # generator params (UI overrides)
    "_bracket_sizes": None,
    "_n_timestamps":  None,
    "_offset_min":    None,
    "_offset_max":    None,
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

def _gen_params():
    """Effective generator params: UI override → config default."""
    cfg  = get_config()
    gcfg = cfg.generator
    with _lock:
        return {
            "bracket_sizes": _session["_bracket_sizes"] or list(gcfg.bracket_sizes),
            "n_timestamps":  _session["_n_timestamps"]  or gcfg.n_timestamps,
            "offset_min":    _session["_offset_min"]    or gcfg.entry_offset_min,
            "offset_max":    _session["_offset_max"]    or gcfg.entry_offset_max,
        }


# ── IB connectivity + price monitor ──────────────────────────────────────────

def _reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        s = _socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def _connectivity_loop():
    """Ping IB ports every 5 s — no IB library needed."""
    while True:
        try:
            cfg = get_config()
            _set("ib_live",  _reachable(cfg.ib.live_host,  cfg.ib.live_port))
            _set("ib_paper", _reachable(cfg.ib.paper_host, cfg.ib.paper_port))
        except Exception:
            pass
        time.sleep(5)


def _price_loop():
    """Persistent read-only IB paper connection; update MES price every 5 s."""
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())

    while True:
        ib = None
        try:
            if not _get("ib_paper"):
                time.sleep(5)
                continue

            from ib_insync import IB, ContFuture
            cfg = get_config()
            ib  = IB()

            connected = False
            for cid in [351, 352, 353]:
                try:
                    ib.connect(cfg.ib.paper_host, cfg.ib.paper_port,
                               clientId=cid, timeout=5, readonly=True)
                    if ib.isConnected():
                        connected = True
                        break
                except Exception:
                    continue

            if not connected:
                time.sleep(20)
                continue

            contract = ContFuture(symbol="MES", exchange="CME", currency="USD")
            ib.qualifyContracts(contract)
            ticker = ib.reqMktData(contract, "", False, False)
            ib.sleep(2)

            while ib.isConnected() and _get("ib_paper"):
                ib.sleep(5)
                p = ticker.last
                if not (p and p > 0):
                    p = ticker.close
                if not (p and p > 0):
                    p = (ticker.bid or 0) or (ticker.ask or 0) or None
                if p and p > 0:
                    _set("price", round(float(p), 2))

        except Exception as e:
            log.debug(f"Price loop: {e}")
        finally:
            if ib:
                try:
                    ib.disconnect()
                except Exception:
                    pass
        time.sleep(20)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with _lock:
        s = dict(_session)
        s["date"]        = s["date"].isoformat() if s["date"] else None
        s["timestamps"]  = [t.isoformat() for t in s["timestamps"]]
        s["server_time"] = datetime.now(timezone.utc).strftime("%H:%M:%S")
        for k in ("_bracket_sizes", "_n_timestamps", "_offset_min", "_offset_max"):
            s.pop(k, None)
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
    Pre-generate scheduled timestamps and placeholder orders for today.
    No tick data. No IB connection required.
    Body: { date, symbol, bracket_sizes?, n_timestamps?,
            entry_offset_min?, entry_offset_max? }
    """
    body   = request.json or {}
    d_str  = body.get("date")  or date.today().isoformat()
    symbol = body.get("symbol", "MES")

    try:
        target_date = date.fromisoformat(d_str)
    except ValueError:
        return jsonify({"error": f"Invalid date: {d_str}"}), 400

    with _lock:
        if _session["status"] == "running":
            return jsonify({"error": "Session already running — stop first"}), 409
        _session.update({
            "date": target_date, "symbol": symbol,
            "status": "generating", "orders": [], "fills": [],
            "timestamps": [], "ib_events": [], "error": None, "pnl": None,
            "_bracket_sizes": body.get("bracket_sizes"),
            "_n_timestamps":  body.get("n_timestamps"),
            "_offset_min":    body.get("entry_offset_min"),
            "_offset_max":    body.get("entry_offset_max"),
        })

    threading.Thread(target=_run_generate, args=(target_date, symbol),
                     daemon=True).start()
    return jsonify({"ok": True, "date": d_str})


@app.route("/api/send", methods=["POST"])
def api_send():
    """Connect to IB paper and start executing the scheduled orders."""
    with _lock:
        status = _session["status"]
        orders = _session["orders"]

    if not orders:
        return jsonify({"error": "Generate first"}), 409
    if status == "running":
        return jsonify({"error": "Already running"}), 409
    if status not in ("scheduled", "done"):
        return jsonify({"error": f"Not ready (status={status})"}), 409

    _set("status", "running")
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
    _set("status", "idle")
    _append_event("STOP", "Stop requested")
    return jsonify({"ok": True})


# ── Generate worker ───────────────────────────────────────────────────────────

def _run_generate(target_date: date, symbol: str):
    """
    Build scheduled timestamps and placeholder order list.
    Pure math — no IB, no tick data.
    """
    try:
        import generator as gen
        p = _gen_params()

        timestamps = gen.generate_live_timestamps(
            target_date  = target_date,
            n_timestamps = p["n_timestamps"],
        )
        if not timestamps:
            raise RuntimeError(
                f"No RTH window available for {target_date} "
                "(market closed or date is in the past beyond RTH)."
            )

        orders = []
        for ts in timestamps:
            for bs in p["bracket_sizes"]:
                for direction in ("BUY", "SELL"):
                    orders.append({
                        "ts_placed":    ts,
                        "direction":    direction,
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

        with _lock:
            _session["timestamps"] = timestamps
            _session["orders"]     = orders
            _session["fills"]      = [None] * len(orders)
            _session["status"]     = "scheduled"

        first = timestamps[0].strftime("%H:%M")
        last  = timestamps[-1].strftime("%H:%M")
        _append_event("INFO",
            f"{len(timestamps)} timestamps  {first}–{last} CT  "
            f"→ {len(orders)} orders  "
            f"(brackets: {p['bracket_sizes']})")

    except Exception as e:
        _set("error", str(e))
        _set("status", "error")
        _append_event("ERROR", str(e))


# ── Live session worker ───────────────────────────────────────────────────────

def _run_live_session(target_date: date, symbol: str):
    """
    At each scheduled timestamp: fetch live MES price from IB paper,
    compute bracket levels, submit bracket orders, collect fills.
    """
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        from lib.ib_client import IBClient
        from lib.order_builder import build_bracket, place_bracket
        import generator as gen

        cfg = get_config()
        p   = _gen_params()

        _append_event("CONN", "Connecting to IB paper...")
        ibc      = IBClient(cfg)
        ibc.connect(live=True, paper=True)
        contract = ibc.get_contract(symbol)
        _append_event("CONN", f"Connected — {symbol} resolved")

        _ib_map:  dict = {}  # ib_order_id → order info
        _pending: dict = {}  # order_idx   → order info

        def on_fill(trade, fill):
            ib_id = fill.execution.orderId
            price = fill.execution.price
            info  = _ib_map.get(ib_id)
            if not info:
                return
            idx = info["order_idx"]
            ts  = datetime.now(timezone.utc).isoformat()

            if ib_id == info["entry_id"]:
                info["entry_fill_price"] = price
                info["entry_fill_time"]  = ts
                info["entry_filled"]     = True
                with _lock:
                    if idx < len(_session["orders"]):
                        _session["orders"][idx]["status"] = "OPEN"
                _append_event("FILL",
                    f"[{idx}] entry {info['direction']} @ {price:.2f}")

            elif ib_id in (info["tp_id"], info["sl_id"]):
                etype = "TP" if ib_id == info["tp_id"] else "SL"
                ep    = info.get("entry_fill_price")
                pnl   = None
                if ep:
                    diff = (price - ep) if info["direction"] == "BUY" else (ep - price)
                    pnl  = round(diff * 5.0, 2)
                result = {
                    "order_idx":        idx,
                    "direction":        info["direction"],
                    "entry_price":      info["entry_price"],
                    "tp_price":         info["tp_price"],
                    "sl_price":         info["sl_price"],
                    "bracket_size":     info["bracket_size"],
                    "ts_placed":        info["ts_placed"],
                    "entry_fill_price": ep,
                    "entry_fill_time":  info.get("entry_fill_time"),
                    "exit_type":        etype,
                    "exit_fill_price":  price,
                    "exit_fill_time":   ts,
                    "pnl":              pnl,
                }
                with _lock:
                    if idx < len(_session["fills"]):
                        _session["fills"][idx] = result
                    if idx < len(_session["orders"]):
                        _session["orders"][idx]["status"] = etype
                _append_event("FILL",
                    f"[{idx}] {etype} @ {price:.2f}"
                    + (f"  P&L ${pnl:+.2f}" if pnl is not None else ""))
                _pending.pop(idx, None)

        ibc.paper.execDetailsEvent += on_fill

        with _lock:
            timestamps    = list(_session["timestamps"])
            bracket_sizes = list(p["bracket_sizes"])

        orders_per_ts = len(bracket_sizes) * 2
        ts_idx        = 0

        for ts in timestamps:
            # Wait until this timestamp
            while datetime.now(timezone.utc) < ts:
                if _get("status") != "running":
                    _append_event("STOP", "Session stopped")
                    return
                ibc.paper.sleep(1)

            if _get("status") != "running":
                break

            # Fetch live price at this moment
            try:
                price = ibc.get_price(symbol)
                _append_event("PRICE",
                    f"@ {ts.strftime('%H:%M:%S')} CT  {symbol} {price:.2f}")
            except Exception as e:
                _append_event("WARN", f"Price fetch failed @ {ts}: {e}")
                ts_idx += orders_per_ts
                continue

            # Generate actual bracket levels for this timestamp
            new_orders = gen.make_orders_for_price(
                ts               = ts,
                market_price     = price,
                bracket_sizes    = bracket_sizes,
                entry_offset_min = p["offset_min"],
                entry_offset_max = p["offset_max"],
                symbol           = symbol,
            )

            for local_i, order in enumerate(new_orders):
                idx = ts_idx + local_i
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
                        "order_idx":        idx,
                        "direction":        order["direction"],
                        "entry_price":      order["entry_price"],
                        "tp_price":         order["tp_price"],
                        "sl_price":         order["sl_price"],
                        "bracket_size":     order["bracket_size"],
                        "ts_placed":        ts,
                        "entry_id":         placed["entry_id"],
                        "tp_id":            placed["tp_id"],
                        "sl_id":            placed["sl_id"],
                        "entry_filled":     False,
                        "entry_fill_price": None,
                        "entry_fill_time":  None,
                    }
                    for ib_id in (placed["entry_id"], placed["tp_id"], placed["sl_id"]):
                        _ib_map[ib_id] = info
                    _pending[idx] = info
                    with _lock:
                        if idx < len(_session["orders"]):
                            _session["orders"][idx]["status"] = "SUBMITTED"
                    _append_event("ORDER",
                        f"[{idx}] {order['direction']} @ {order['entry_price']:.2f} "
                        f"TP {order['tp_price']:.2f} SL {order['sl_price']:.2f} "
                        f"[{order['bracket_size']}pt]")
                except Exception as e:
                    _append_event("ERROR", f"Submit [{idx}]: {e}")
                    with _lock:
                        if idx < len(_session["orders"]):
                            _session["orders"][idx]["status"] = "ERROR"

            ts_idx += orders_per_ts

        # Session end — cancel unfilled entries
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
                    if idx < len(_session["fills"]) and not _session["fills"][idx]:
                        _session["fills"][idx] = {"exit_type": "EXPIRED", "pnl": None}

        ibc.paper.execDetailsEvent -= on_fill
        ibc.disconnect()

        with _lock:
            fills = [f for f in _session["fills"] if f and f.get("pnl") is not None]
            total = sum(f["pnl"] for f in fills)
            _session["pnl"]    = round(total, 2)
            _session["status"] = "done"
        _append_event("RESULT", f"Done. P&L ${total:+.2f} ({len(fills)} filled)")

    except Exception as e:
        _set("error", str(e))
        _set("status", "error")
        _append_event("ERROR", f"Session error: {e}")
        log.error(f"Live session error: {e}", exc_info=True)


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    try:
        cfg = get_config()
        assert hasattr(cfg, "visualizer")
        assert hasattr(cfg, "generator")

        # Routes
        with app.test_client() as c:
            r = c.get("/api/status")
            assert r.status_code == 200
            d = r.get_json()
            assert "status" in d and "orders" in d and "ib_live" in d

        # Generate worker (no tick data, no IB)
        with _lock:
            _session.update({
                "date": None, "status": "idle", "symbol": "MES",
                "timestamps": [], "orders": [], "fills": [],
                "ib_events": [], "error": None, "pnl": None,
                "_bracket_sizes": None, "_n_timestamps": None,
                "_offset_min": None, "_offset_max": None,
            })

        with app.test_client() as c:
            r = c.post("/api/generate",
                       json={"date": "2026-04-09", "symbol": "MES",
                             "bracket_sizes": [2.0], "n_timestamps": 5})
            assert r.status_code == 200

        deadline = time.time() + 10
        while time.time() < deadline:
            time.sleep(0.2)
            if _get("status") in ("scheduled", "error"):
                break

        with _lock:
            st     = _session["status"]
            orders = list(_session["orders"])

        assert st == "scheduled", f"status={st} error={_get('error')}"
        assert len(orders) == 5 * 1 * 2, f"expected 10 orders, got {len(orders)}"  # 5ts × 1bracket × 2dirs
        assert all(o["status"] == "SCHEDULED" for o in orders)

        print("[self-test] bt_visualizer: PASS")
        return True

    except Exception as e:
        print(f"[self-test] bt_visualizer: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Back-trading live paper dashboard")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    cfg  = get_config()
    host = cfg.visualizer.host
    port = cfg.visualizer.port

    threading.Thread(target=_connectivity_loop, daemon=True, name="ib-ping").start()
    threading.Thread(target=_price_loop,        daemon=True, name="price").start()

    print(f"Back-trading dashboard → http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)
