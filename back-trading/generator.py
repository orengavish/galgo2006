"""
back-trading/generator.py
Synthetic bracket order generator for back-trading simulations.

Strategy — "fake critical line" approach:
  At N random timestamps within RTH (08:30–14:30 CT):
    • Look up actual market price P from tick data
    • Create LMT BUY  at P - offset  (market ABOVE our "line" → LMT BUY)
    • Create LMT SELL at P + offset  (market BELOW our "line" → LMT SELL)
    • For each configured bracket size (e.g. [2, 16] points)

  offset is random in [entry_offset_min, entry_offset_max], tick-rounded.
  Close enough to fill quickly but not trivially at placement.

  Timestamps are drawn so no two are within MIN_GAP_SECONDS of each other
  (prevents clustering and gives fills time to complete).

Self-test:
  python back-trading/generator.py --self-test
"""

import sys
import random
import argparse
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

CT  = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

_TICK          = 0.25
_MIN_GAP_SEC   = 120    # minimum seconds between placement timestamps
_MES_MULT      = 5.0    # $5 per point on MES


def _round_tick(price: float, tick: float = _TICK) -> float:
    return round(round(price / tick) * tick, 10)


def _pick_spaced(pool: list, n: int, min_gap_s: int) -> list:
    """
    Pick up to n items from pool (list of datetime) so that no two
    are within min_gap_s seconds of each other.
    Shuffles the pool first for randomness, then greedily selects.
    """
    pool = list(pool)
    random.shuffle(pool)
    chosen = []
    for ts in pool:
        if len(chosen) >= n:
            break
        if all(abs((ts - c).total_seconds()) >= min_gap_s for c in chosen):
            chosen.append(ts)
    return sorted(chosen)


def generate(trades_df: pd.DataFrame,
             target_date: date,
             bracket_sizes: list,
             n_timestamps: int,
             entry_offset_min: float,
             entry_offset_max: float,
             symbol: str = "MES",
             seed: int = None) -> list[dict]:
    """
    Generate synthetic bracket orders from a day's TRADES tick data.

    trades_df        : DataFrame with columns [time_utc, price, size]
    target_date      : the trading date
    bracket_sizes    : e.g. [2, 16] — TP/SL distance in points
    n_timestamps     : how many placement times to sample
    entry_offset_min : min distance from market (points), tick-rounded
    entry_offset_max : max distance from market (points), tick-rounded
    seed             : optional random seed for reproducibility

    Returns list of order dicts:
      ts_placed, direction, entry_type, entry_price, tp_price, sl_price,
      bracket_size, market_price, entry_offset, symbol
    """
    if seed is not None:
        random.seed(seed)

    # RTH placement window
    rth_start = datetime(target_date.year, target_date.month, target_date.day,
                         8, 30, 0, tzinfo=CT).astimezone(UTC)
    rth_end   = datetime(target_date.year, target_date.month, target_date.day,
                         14, 30, 0, tzinfo=CT).astimezone(UTC)

    df = trades_df[
        (trades_df["time_utc"] >= rth_start) &
        (trades_df["time_utc"] <  rth_end)
    ].copy()

    if df.empty:
        return []

    # Unique timestamps only (many trades share the same second)
    unique_ts = sorted(df["time_utc"].unique())
    if len(unique_ts) < n_timestamps:
        return []

    timestamps = _pick_spaced(unique_ts, n_timestamps, _MIN_GAP_SEC)

    orders = []
    for ts in timestamps:
        # Use the first trade at this timestamp as the market price
        market_price = float(df[df["time_utc"] == ts].iloc[0]["price"])

        # Random tick-rounded offset
        raw = random.uniform(entry_offset_min, entry_offset_max)
        offset = _round_tick(raw)
        if offset < _TICK:
            offset = _TICK

        for bracket_size in bracket_sizes:
            bs = float(bracket_size)

            # --- LMT BUY: line below market, market is ABOVE → LMT BUY ---
            buy_line = _round_tick(market_price - offset)
            orders.append({
                "ts_placed":    ts,
                "direction":    "BUY",
                "entry_type":   "LMT",
                "entry_price":  buy_line,
                "tp_price":     _round_tick(buy_line + bs),
                "sl_price":     _round_tick(buy_line - bs),
                "bracket_size": bs,
                "market_price": market_price,
                "entry_offset": offset,
                "symbol":       symbol,
            })

            # --- LMT SELL: line above market, market is BELOW → LMT SELL ---
            sell_line = _round_tick(market_price + offset)
            orders.append({
                "ts_placed":    ts,
                "direction":    "SELL",
                "entry_type":   "LMT",
                "entry_price":  sell_line,
                "tp_price":     _round_tick(sell_line - bs),
                "sl_price":     _round_tick(sell_line + bs),
                "bracket_size": bs,
                "market_price": market_price,
                "entry_offset": offset,
                "symbol":       symbol,
            })

    return orders


# ── Self-test ──────────────────────────────────────────────────────────────────

def self_test() -> bool:
    from datetime import timedelta, timezone
    try:
        # Build a synthetic trades DataFrame: 1 tick per second, 8:30-14:30 CT
        base = datetime(2026, 4, 9, 13, 30, 0, tzinfo=UTC)  # 08:30 CT
        rows = []
        for i in range(21600):  # 6 hours of 1-tick-per-second data
            ts = base + timedelta(seconds=i)
            rows.append({"time_utc": ts, "price": 6500.0 + (i % 100) * 0.25, "size": 1})
        df = pd.DataFrame(rows)

        orders = generate(
            trades_df        = df,
            target_date      = date(2026, 4, 9),
            bracket_sizes    = [2.0, 16.0],
            n_timestamps     = 10,
            entry_offset_min = 0.25,
            entry_offset_max = 1.50,
            seed             = 42,
        )

        # 10 timestamps × 2 brackets × 2 directions = 40 orders
        assert len(orders) == 40, f"Expected 40, got {len(orders)}"

        # Check structure
        for o in orders:
            assert o["direction"] in ("BUY", "SELL")
            assert o["entry_type"] == "LMT"
            assert o["bracket_size"] in (2.0, 16.0)
            assert o["entry_offset"] >= 0.25

            # BUY: entry below market
            if o["direction"] == "BUY":
                assert o["entry_price"] < o["market_price"], \
                    f"BUY entry {o['entry_price']} should be < market {o['market_price']}"
                assert o["tp_price"] > o["entry_price"]
                assert o["sl_price"] < o["entry_price"]
            else:
                assert o["entry_price"] > o["market_price"], \
                    f"SELL entry {o['entry_price']} should be > market {o['market_price']}"
                assert o["tp_price"] < o["entry_price"]
                assert o["sl_price"] > o["entry_price"]

        # Timestamps are at least MIN_GAP_SEC apart
        ts_list = sorted(set(o["ts_placed"] for o in orders))
        for i in range(1, len(ts_list)):
            gap = (ts_list[i] - ts_list[i-1]).total_seconds()
            assert gap >= _MIN_GAP_SEC, f"Timestamps too close: {gap}s"

        print("[self-test] generator: PASS")
        return True

    except Exception as e:
        print(f"[self-test] generator: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("generator.py — run --self-test to verify")
