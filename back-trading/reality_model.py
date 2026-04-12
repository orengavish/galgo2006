"""
back-trading/reality_model.py
IB paper trading reality model — submits generated orders to IB paper
and collects actual fills for later comparison with the simulator.

Run via engine.py --reality-model (not directly).

Lifecycle:
  1. Connect to IB paper
  2. At each order's ts_placed, submit the bracket to paper
  3. Collect fills via execDetailsEvent (event-driven)
  4. At session end (15:00 CT), cancel all unfilled entries
  5. Write paper_fills rows to DB
  6. Return fills list (indexed by order position) for grader
"""

import sys
import time
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.ib_client import IBClient
from lib.order_builder import build_bracket, place_bracket
from lib.logger import get_logger

log = get_logger("reality_model")

CT  = ZoneInfo("America/Chicago")
_MES_MULT  = 5.0
_SESSION_END_H = 15
_SESSION_END_M = 0


class RealityModel:
    """
    Submits generated orders to IB paper in real time and collects fills.

    Usage:
        rm = RealityModel(cfg, db_conn, run_id)
        paper_fills = rm.run(orders, symbol="MES", target_date=date.today())
    """

    def __init__(self, cfg, db_conn: sqlite3.Connection, run_id: int):
        self._cfg    = cfg
        self._db     = db_conn
        self._run_id = run_id
        self._ibc    = IBClient(cfg)

        # ib_entry_id → bracket info dict
        # Also indexed by tp_id and sl_id for fast lookup in event handler
        self._id_map: dict[int, dict] = {}

        # order_idx → completed fill dict
        self._fills: dict[int, dict] = {}

        # order_idx → bracket info (for cancel-on-expiry)
        self._pending_entry: dict[int, dict] = {}

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self, orders: list[dict], order_db_ids: list[int],
            symbol: str, target_date: date) -> list[dict]:
        """
        Submit orders at their timestamps and collect fills.
        Returns list of fill dicts (same length as orders, indexed by position).
        """
        end_ct  = datetime(target_date.year, target_date.month, target_date.day,
                           _SESSION_END_H, _SESSION_END_M, 0, tzinfo=CT)
        end_utc = end_ct.astimezone(timezone.utc)

        log.info(f"Reality model connecting to IB paper for {target_date}")
        self._ibc.connect(live=False, paper=True)
        contract = self._ibc.get_contract(symbol)

        # Wire fill event
        self._ibc.paper.execDetailsEvent += self._on_fill

        sorted_orders = sorted(enumerate(orders), key=lambda x: x[1]["ts_placed"])
        idx = 0

        try:
            while True:
                now = datetime.now(timezone.utc)
                if now >= end_utc:
                    break

                # Submit any orders whose scheduled time has arrived
                while idx < len(sorted_orders):
                    order_pos, order = sorted_orders[idx]
                    if order["ts_placed"] <= now:
                        self._submit(order, order_pos, order_db_ids[order_pos], contract)
                        idx += 1
                    else:
                        break

                self._ibc.paper.sleep(1)   # process IB events for 1 second

        finally:
            self._ibc.paper.execDetailsEvent -= self._on_fill
            self._cancel_unfilled()
            self._write_db(order_db_ids)
            self._ibc.disconnect()

        # Return fills as a list indexed by order position
        n = len(orders)
        return [
            self._fills.get(i, {
                "order_idx": i, "exit_type": "EXPIRED",
                "entry_fill_price": None, "entry_fill_time": None,
                "exit_fill_price": None, "exit_fill_time": None, "pnl": None,
            })
            for i in range(n)
        ]

    # ── Order submission ──────────────────────────────────────────────────────

    def _submit(self, order: dict, order_pos: int,
                order_db_id: int, contract) -> None:
        log.info(f"Submit [{order_pos}] {order['direction']} {order['entry_type']} "
                 f"@ {order['entry_price']} bracket={order['bracket_size']}")
        try:
            ib_orders = build_bracket(
                self._ibc.paper, contract,
                order["direction"], order["entry_type"],
                order["entry_price"], order["tp_price"], order["sl_price"],
            )
            placed = place_bracket(self._ibc.paper, contract, ib_orders)

            info = {
                "order_pos":    order_pos,
                "order_db_id":  order_db_id,
                "direction":    order["direction"],
                "entry_price":  order["entry_price"],
                "tp_price":     order["tp_price"],
                "sl_price":     order["sl_price"],
                "bracket_size": order["bracket_size"],
                "ib_entry_id":  placed["entry_id"],
                "ib_tp_id":     placed["tp_id"],
                "ib_sl_id":     placed["sl_id"],
                "entry_filled": False,
                "entry_fill_price": None,
                "entry_fill_time":  None,
            }
            # Index by all three IB order IDs
            for ib_id in (placed["entry_id"], placed["tp_id"], placed["sl_id"]):
                self._id_map[ib_id] = info
            self._pending_entry[order_pos] = info

        except Exception as e:
            log.error(f"Submit failed for order {order_pos}: {e}")

    # ── Fill event ────────────────────────────────────────────────────────────

    def _on_fill(self, trade, fill):
        """Called by ib_insync when an execution is confirmed."""
        ib_id = fill.execution.orderId
        price = fill.execution.price
        ts    = datetime.now(timezone.utc).isoformat()

        info = self._id_map.get(ib_id)
        if info is None:
            return

        pos = info["order_pos"]

        if ib_id == info["ib_entry_id"]:
            info["entry_filled"]      = True
            info["entry_fill_price"]  = price
            info["entry_fill_time"]   = ts
            log.info(f"Entry fill [{pos}]: {info['direction']} @ {price}")

        elif ib_id == info["ib_tp_id"]:
            info["exit_type"]       = "TP"
            info["exit_fill_price"] = price
            info["exit_fill_time"]  = ts
            self._complete(pos, info)

        elif ib_id == info["ib_sl_id"]:
            info["exit_type"]       = "SL"
            info["exit_fill_price"] = price
            info["exit_fill_time"]  = ts
            self._complete(pos, info)

    def _complete(self, pos: int, info: dict) -> None:
        """Record a completed bracket in self._fills."""
        entry_p = info.get("entry_fill_price")
        exit_p  = info.get("exit_fill_price")
        direct  = info["direction"]

        pnl = None
        if entry_p and exit_p:
            diff = (exit_p - entry_p) if direct == "BUY" else (entry_p - exit_p)
            pnl  = round(diff * _MES_MULT, 2)

        self._fills[pos] = {
            "order_idx":        pos,
            "ib_entry_id":      info["ib_entry_id"],
            "entry_fill_price": entry_p,
            "entry_fill_time":  info.get("entry_fill_time"),
            "exit_type":        info.get("exit_type", "EXPIRED"),
            "exit_fill_price":  exit_p,
            "exit_fill_time":   info.get("exit_fill_time"),
            "pnl":              pnl,
        }
        self._pending_entry.pop(pos, None)
        log.info(f"Bracket complete [{pos}]: {info['exit_type']} @ {exit_p}  P&L={pnl}")

    # ── Session end ───────────────────────────────────────────────────────────

    def _cancel_unfilled(self) -> None:
        """Cancel entry orders that never filled."""
        for pos, info in list(self._pending_entry.items()):
            if not info["entry_filled"]:
                try:
                    # Cancel the entry order by IB ID
                    for t in self._ibc.paper.trades():
                        if t.order.orderId == info["ib_entry_id"]:
                            self._ibc.paper.cancelOrder(t.order)
                            break
                except Exception as e:
                    log.warning(f"Cancel failed for [{pos}]: {e}")
                self._fills[pos] = {
                    "order_idx": pos, "ib_entry_id": info["ib_entry_id"],
                    "entry_fill_price": None, "entry_fill_time": None,
                    "exit_type": "EXPIRED",
                    "exit_fill_price": None, "exit_fill_time": None, "pnl": None,
                }

    # ── DB persistence ────────────────────────────────────────────────────────

    def _write_db(self, order_db_ids: list[int]) -> None:
        for pos, fill in self._fills.items():
            db_order_id = order_db_ids[pos] if pos < len(order_db_ids) else None
            self._db.execute("""
                INSERT INTO paper_fills
                (order_id, ib_entry_id, entry_fill_price, entry_fill_time,
                 exit_type, exit_fill_price, exit_fill_time, pnl)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                db_order_id,
                fill.get("ib_entry_id"),
                fill.get("entry_fill_price"),
                fill.get("entry_fill_time"),
                fill.get("exit_type"),
                fill.get("exit_fill_price"),
                fill.get("exit_fill_time"),
                fill.get("pnl"),
            ))
        self._db.commit()
        log.info(f"Wrote {len(self._fills)} paper fills to DB")
