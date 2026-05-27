"""
back-trading/simulator.py
Tick-by-tick OCO bracket simulator.  Realism is the primary goal.

Fill model (1-lot MES):
┌──────────────────┬──────────────┬──────────────────────────────────────────────┐
│ Leg              │ Tick source  │ Condition                                    │
├──────────────────┼──────────────┼──────────────────────────────────────────────┤
│ Entry LMT BUY    │ BID_ASK      │ ask_p  <= entry_price  (someone sells to us) │
│ Entry LMT SELL   │ BID_ASK      │ bid_p  >= entry_price  (someone buys from us)│
│ Long  TP  (sell) │ TRADES       │ price  >= tp_price  (conservative: trade-at) │
│ Long  SL  (sell) │ TRADES       │ price  <= sl_price  + 1-tick slippage        │
│ Short TP  (buy)  │ TRADES       │ price  <= tp_price  (conservative: trade-at) │
│ Short SL  (buy)  │ TRADES       │ price  >= sl_price  + 1-tick slippage        │
└──────────────────┴──────────────┴──────────────────────────────────────────────┘

Fallback: if BID_ASK data is unavailable, entry uses TRADES (price touch).

OCO priority: SL checked before TP on the same tick (conservative).

Self-test:
  python back-trading/simulator.py --self-test
"""

import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TICK          = 0.25
_SL_SLIP_TICKS = 0       # SL market fill: data shows 64% have 0 slippage (was 1)
_MES_MULT      = 5.0     # MES: $5 per point = $1.25 per tick


# ── Public API ────────────────────────────────────────────────────────────────

def _confirmed_hit(df: pd.DataFrame, first_idx: int, min_ticks: int = 2) -> bool:
    """True if there are >=min_ticks rows at/past the level starting from first_idx."""
    return len(df) - first_idx >= min_ticks


def simulate_exit(fill_price: float,
                  fill_time: datetime,
                  tp_price: float,
                  sl_price: float,
                  direction: str,
                  trades_df: pd.DataFrame,
                  session_end_utc: datetime,
                  stag_seconds: float | None = None,
                  stag_move: float | None = None,
                  tp_confirm_ticks: int = 2) -> dict:
    """
    Calibration entry point: IB already filled the entry.
    Skip phase 1 — run only phase 2 (TP/SL OCO) from the known fill.

    stag_seconds / stag_move: when both provided, add STAGNATION detection.
    tp_confirm_ticks: minimum consecutive at/past-TP ticks to confirm a TP fill.
      Filters brief "touch and bounce" that IB paper doesn't fill. Default 2.

    Returns dict with keys: exit_type, exit_fill_price, exit_fill_time, pnl.
    """
    from datetime import timedelta

    is_buy = direction == "BUY"
    trades_after = trades_df[trades_df["time_utc"] > fill_time]

    # TP: first hit only confirmed if >=tp_confirm_ticks ticks stay at/past tp
    if is_buy:
        tp_cands = trades_after[trades_after["price"] >= tp_price]
        sl_cands = trades_after[trades_after["price"] <= sl_price]
        sl_exit  = sl_price - _TICK * _SL_SLIP_TICKS
    else:
        tp_cands = trades_after[trades_after["price"] <= tp_price]
        sl_cands = trades_after[trades_after["price"] >= sl_price]
        sl_exit  = sl_price + _TICK * _SL_SLIP_TICKS

    # Find confirmed TP: scan each candidate row, check if >=tp_confirm_ticks follow.
    # Use actual tick price (not tp_price) to model price improvement on gaps:
    #   SELL TP (BUY limit): fills at ask, which may be < tp on a fast drop.
    #   BUY  TP (SELL limit): fills at tp (data shows delta=0 for all BUY TPs).
    tp_hit = None
    for iloc_i in range(len(tp_cands)):
        if _confirmed_hit(tp_cands, iloc_i, tp_confirm_ticks):
            row = tp_cands.iloc[iloc_i]
            tick_p = row["price"]
            # For SELL TP: actual fill = tick price (could be < tp on gap)
            # For BUY  TP: actual fill = tp_price (limit order, no price improvement in data)
            fill_p = tick_p if not is_buy else tp_price
            tp_hit = (row["time_utc"], fill_p)
            break

    sl_hit = (sl_cands.iloc[0]["time_utc"], sl_cands.iloc[0]["price"]) if not sl_cands.empty else None

    # Stagnation: model what the live position_manager sees (last-known trade price).
    # At fill+stag_seconds, check the last trade price up to that moment.
    # If within stag_move → stagnation fires at cutoff.
    # Otherwise, wait for the next trade tick within stag_move.
    stag_hit = None
    if stag_seconds is not None and stag_move is not None:
        stag_cutoff = fill_time + timedelta(seconds=stag_seconds)
        prior = trades_df[trades_df["time_utc"] <= stag_cutoff]
        if not prior.empty:
            last_p = prior.iloc[-1]["price"]
            if abs(last_p - fill_price) < stag_move * 2:
                stag_hit = (stag_cutoff, last_p)
            else:
                # Check each new trade tick after cutoff (price changes on each tick)
                cands = trades_after[
                    (trades_after["time_utc"] > stag_cutoff) &
                    (trades_after["price"].sub(fill_price).abs() < stag_move * 2)
                ]
                if not cands.empty:
                    stag_hit = (cands.iloc[0]["time_utc"], cands.iloc[0]["price"])

    # OCO priority: SL > TP > STAG (take earliest; SL wins ties)
    candidates = [(t, kind) for t, kind in [
        (sl_hit[0]   if sl_hit   else None, "SL"),
        (tp_hit[0]   if tp_hit   else None, "TP"),
        (stag_hit[0] if stag_hit else None, "STAGNATION"),
    ] if t is not None]

    if not candidates:
        exit_type       = "EXPIRED"
        exit_time       = session_end_utc
        exit_fill_price = None
    else:
        # Earliest time wins; on tie SL > TP > STAG (order already sorted above)
        earliest_time = min(t for t, _ in candidates)
        for t, kind in candidates:       # first match in SL>TP>STAG order
            if t == earliest_time:
                winner = kind
                break

        if winner == "SL":
            exit_type       = "SL"
            exit_time, _    = sl_hit
            exit_fill_price = round(sl_exit, 10)
        elif winner == "TP":
            exit_type           = "TP"
            exit_time, tp_fill  = tp_hit
            exit_fill_price     = tp_fill
        else:
            exit_type       = "STAGNATION"
            exit_time, stag_price = stag_hit
            exit_fill_price = stag_price   # MKT exit at current price

    pnl = None
    if exit_fill_price is not None:
        diff = (exit_fill_price - fill_price) if is_buy else (fill_price - exit_fill_price)
        pnl  = round(diff * _MES_MULT, 2)

    return {
        "exit_type":        exit_type,
        "exit_fill_price":  exit_fill_price,
        "exit_fill_time":   exit_time,
        "pnl":              pnl,
    }


def simulate(orders: list[dict],
             trades_df: pd.DataFrame,
             bidask_df: Optional[pd.DataFrame],
             session_end_utc: datetime) -> list[dict]:
    """
    Simulate fills for all orders.

    orders          : list of order dicts (from generator.generate)
    trades_df       : TRADES ticks — columns [time_utc, price, size]
    bidask_df       : BID_ASK ticks — columns [time_utc, bid_p, ask_p, ...]
                      Pass None to fall back to TRADES for entry fills.
    session_end_utc : used as exit_fill_time for EXPIRED orders

    Returns list of fill result dicts, one per input order (same index).
    """
    return [
        _sim_one(order, i, trades_df, bidask_df, session_end_utc)
        for i, order in enumerate(orders)
    ]


# ── Core simulation ───────────────────────────────────────────────────────────

def _sim_one(order: dict, idx: int,
             trades_df: pd.DataFrame,
             bidask_df: Optional[pd.DataFrame],
             session_end_utc: datetime) -> dict:

    ts    = order["ts_placed"]
    ep    = order["entry_price"]
    tp    = order["tp_price"]
    sl    = order["sl_price"]
    is_buy = order["direction"] == "BUY"

    # Slice from placement time onward
    trades = trades_df[trades_df["time_utc"] >= ts]
    ba     = bidask_df[bidask_df["time_utc"] >= ts] if bidask_df is not None else None

    # ── Phase 1: entry fill ───────────────────────────────────────────────────
    entry = _find_entry(is_buy, ep, trades, ba)
    if entry is None:
        return _make_result(order, idx,
                            entry_fill_price=None, entry_fill_time=None,
                            exit_type="EXPIRED", exit_fill_price=None,
                            exit_fill_time=session_end_utc, pnl=None)

    entry_time, entry_price = entry

    # ── Phase 2: TP / SL after entry ─────────────────────────────────────────
    trades_after = trades[trades["time_utc"] > entry_time]

    if is_buy:
        tp_hit = _first_gte(tp, trades_after)   # trade at or above TP
        sl_hit = _first_lte(sl, trades_after)   # trade at or below SL
        sl_exit = sl - _TICK * _SL_SLIP_TICKS   # worse than stop (slip)
    else:
        tp_hit = _first_lte(tp, trades_after)   # trade at or below TP
        sl_hit = _first_gte(sl, trades_after)   # trade at or above SL
        sl_exit = sl + _TICK * _SL_SLIP_TICKS

    # OCO: SL priority on tie
    if tp_hit is None and sl_hit is None:
        exit_type       = "EXPIRED"
        exit_time       = session_end_utc
        exit_fill_price = None

    elif sl_hit is not None and (tp_hit is None or sl_hit[0] <= tp_hit[0]):
        exit_type       = "SL"
        exit_time, _    = sl_hit
        exit_fill_price = round(sl_exit, 10)

    else:
        exit_type       = "TP"
        exit_time, _    = tp_hit
        exit_fill_price = tp   # limit order: our price

    # ── P&L ──────────────────────────────────────────────────────────────────
    pnl = None
    if exit_fill_price is not None:
        diff = (exit_fill_price - entry_price) if is_buy else (entry_price - exit_fill_price)
        pnl  = round(diff * _MES_MULT, 2)

    return _make_result(order, idx,
                        entry_fill_price=entry_price, entry_fill_time=entry_time,
                        exit_type=exit_type, exit_fill_price=exit_fill_price,
                        exit_fill_time=exit_time, pnl=pnl)


# ── Fill finders ─────────────────────────────────────────────────────────────

def _find_entry(is_buy: bool, ep: float,
                trades: pd.DataFrame,
                ba: Optional[pd.DataFrame]):
    """Return (time, price) of first entry fill, or None."""
    if ba is not None and not ba.empty:
        if is_buy:
            # LMT BUY: ASK drops to or below entry price
            hit = ba[ba["ask_p"] <= ep]
        else:
            # LMT SELL: BID rises to or above entry price
            hit = ba[ba["bid_p"] >= ep]
        if not hit.empty:
            row = hit.iloc[0]
            return row["time_utc"], ep   # limit fill: always at our price
    else:
        # Fallback: TRADES touch
        if is_buy:
            hit = trades[trades["price"] <= ep]
        else:
            hit = trades[trades["price"] >= ep]
        if not hit.empty:
            return hit.iloc[0]["time_utc"], ep
    return None


def _first_gte(price: float, df: pd.DataFrame):
    """First trade at or above price. Returns (time, price) or None."""
    hit = df[df["price"] >= price]
    return (hit.iloc[0]["time_utc"], hit.iloc[0]["price"]) if not hit.empty else None


def _first_lte(price: float, df: pd.DataFrame):
    """First trade at or below price. Returns (time, price) or None."""
    hit = df[df["price"] <= price]
    return (hit.iloc[0]["time_utc"], hit.iloc[0]["price"]) if not hit.empty else None


# ── Result builder ────────────────────────────────────────────────────────────

def _make_result(order, idx, entry_fill_price, entry_fill_time,
                 exit_type, exit_fill_price, exit_fill_time, pnl) -> dict:
    return {
        "order_idx":        idx,
        "direction":        order["direction"],
        "entry_type":       order["entry_type"],
        "entry_price":      order["entry_price"],
        "tp_price":         order["tp_price"],
        "sl_price":         order["sl_price"],
        "bracket_size":     order["bracket_size"],
        "market_price":     order["market_price"],
        "ts_placed":        order["ts_placed"],
        "entry_fill_price": entry_fill_price,
        "entry_fill_time":  entry_fill_time,
        "exit_type":        exit_type,
        "exit_fill_price":  exit_fill_price,
        "exit_fill_time":   exit_fill_time,
        "pnl":              pnl,
    }


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    UTC = ZoneInfo("UTC")

    try:
        base = datetime(2026, 4, 9, 14, 0, 0, tzinfo=UTC)  # 09:00 CT

        def make_ts(s): return base + timedelta(seconds=s)

        # TRADES: price goes: 6500 → 6499 → 6501 → 6498 → 6503
        trades = pd.DataFrame([
            {"time_utc": make_ts(0),  "price": 6500.00, "size": 5},
            {"time_utc": make_ts(10), "price": 6499.00, "size": 3},
            {"time_utc": make_ts(20), "price": 6501.00, "size": 2},
            {"time_utc": make_ts(30), "price": 6498.00, "size": 4},
            {"time_utc": make_ts(40), "price": 6503.00, "size": 1},
        ])
        # BID_ASK: ASK mirrors price - 0.25
        bidask = pd.DataFrame([
            {"time_utc": make_ts(0),  "bid_p": 6499.75, "ask_p": 6500.25},
            {"time_utc": make_ts(5),  "bid_p": 6498.75, "ask_p": 6499.00},
            {"time_utc": make_ts(15), "bid_p": 6500.75, "ask_p": 6501.00},
            {"time_utc": make_ts(25), "bid_p": 6497.75, "ask_p": 6498.00},
            {"time_utc": make_ts(35), "bid_p": 6502.75, "ask_p": 6503.00},
        ])

        session_end = make_ts(3600)

        # Test 1: LMT BUY at 6499, bracket 2 → tp=6501, sl=6497
        # Entry: ASK=6499.00 at t=5 → fill at 6499
        # TP: trade >= 6501 at t=20 → TP hit
        orders = [{
            "ts_placed": make_ts(0), "direction": "BUY", "entry_type": "LMT",
            "entry_price": 6499.0, "tp_price": 6501.0, "sl_price": 6497.0,
            "bracket_size": 2.0, "market_price": 6500.0, "entry_offset": 1.0,
        }]
        results = simulate(orders, trades, bidask, session_end)
        r = results[0]
        assert r["exit_type"] == "TP", f"Expected TP, got {r['exit_type']}"
        assert r["exit_fill_price"] == 6501.0
        assert r["pnl"] == (6501.0 - 6499.0) * 5.0  # $10

        # Test 2: LMT BUY at 6499, bracket 1 → tp=6500, sl=6498
        # Entry: ASK=6499.00 at t=5
        # SL: trade <= 6498 at t=30 comes before TP (6500 hit at t=15)
        # Wait - trade=6501 at t=20 > 6500, so TP at t=20 first
        # Let me recalculate: tp=6500, first trade>=6500 is t=0 (6500.00)
        # But ts_placed=t=0, trades AFTER entry fill (t=5):
        # t=10: 6499 < 6500, t=20: 6501 >= 6500 → TP at t=20
        orders2 = [{
            "ts_placed": make_ts(0), "direction": "BUY", "entry_type": "LMT",
            "entry_price": 6499.0, "tp_price": 6500.0, "sl_price": 6498.0,
            "bracket_size": 1.0, "market_price": 6500.0, "entry_offset": 1.0,
        }]
        results2 = simulate(orders2, trades, bidask, session_end)
        r2 = results2[0]
        assert r2["entry_fill_time"] == make_ts(5), f"Entry time wrong: {r2['entry_fill_time']}"
        assert r2["exit_type"] == "TP", f"Expected TP, got {r2['exit_type']}"

        # Test 3: EXPIRED (entry placed too late, no fill)
        orders3 = [{
            "ts_placed": make_ts(45), "direction": "BUY", "entry_type": "LMT",
            "entry_price": 6490.0, "tp_price": 6492.0, "sl_price": 6488.0,
            "bracket_size": 2.0, "market_price": 6503.0, "entry_offset": 13.0,
        }]
        results3 = simulate(orders3, trades, bidask, session_end)
        assert results3[0]["exit_type"] == "EXPIRED"
        assert results3[0]["pnl"] is None

        # Test 4: SL hit (no BID_ASK fallback test)
        trades4 = pd.DataFrame([
            {"time_utc": make_ts(0),  "price": 6500.0, "size": 2},
            {"time_utc": make_ts(5),  "price": 6499.0, "size": 2},
            {"time_utc": make_ts(10), "price": 6496.0, "size": 2},
        ])
        orders4 = [{
            "ts_placed": make_ts(0), "direction": "BUY", "entry_type": "LMT",
            "entry_price": 6499.0, "tp_price": 6501.0, "sl_price": 6497.0,
            "bracket_size": 2.0, "market_price": 6500.0, "entry_offset": 1.0,
        }]
        results4 = simulate(orders4, trades4, None, session_end)
        r4 = results4[0]
        assert r4["exit_type"] == "SL"
        # SL at 6497, 0 slippage ticks → fill at 6497.0
        assert r4["exit_fill_price"] == 6497.0, f"SL fill: {r4['exit_fill_price']}"
        assert r4["pnl"] == (6497.0 - 6499.0) * 5.0

        print("[self-test] simulator: PASS")
        return True

    except Exception as e:
        print(f"[self-test] simulator: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("simulator.py — run --self-test to verify")
