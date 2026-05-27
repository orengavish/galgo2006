"""
trader/verify_data.py
Data correctness checker for fetched tick files.

Checks:
  1. File exists and is non-empty
  2. Row count is plausible (> MIN_ROWS for RTH)
  3. Price range is within historical norms for the symbol
  4. No RTH gaps > MAX_GAP_MINUTES with zero ticks
  5. Bid > ask rate < MAX_INVERSION_RATE (for bidask files)
  6. First/last tick within expected session window

Saves verified=1 to fetch_progress.db on pass.

Usage:
  python trader/verify_data.py --symbol MES --date 2026-06-02
  python trader/verify_data.py --symbol MES --date 2026-06-02 --dtype bidask
  python trader/verify_data.py --all-pending   # check all unverified finished files
"""

import csv
import sys
import argparse
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from zoneinfo import ZoneInfo
from lib.config_loader import get_config
from lib.logger import get_logger

log = get_logger("verify_data")

CT  = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

# RTH: 08:30 – 15:15 CT
_RTH_START = (8, 30)
_RTH_END   = (15, 15)

_MAX_GAP_MINUTES   = 5     # longest acceptable silence during RTH
_MAX_INVERSION_RATE = 0.01  # max fraction of rows where bid > ask

# Minimum tick counts per full session (rough lower bound)
_MIN_ROWS = {
    "MES": 50_000,
    "MNQ": 30_000,
    "MYM": 10_000,
    "M2K": 10_000,
}

# Expected price ranges (rough bounds — update if market moves a lot)
_PRICE_RANGE = {
    "MES": (3000, 8000),
    "MNQ": (10000, 30000),
    "MYM": (25000, 50000),
    "M2K": (1000, 4000),
}


def _open_progress_db(cfg) -> sqlite3.Connection:
    try:
        prog_db = Path(cfg.paths.db).parent / "fetch_progress.db"
    except Exception:
        prog_db = Path("data/fetch_progress.db")
    conn = sqlite3.connect(str(prog_db), timeout=30)
    conn.row_factory = sqlite3.Row
    # Add verified column if not present
    try:
        conn.execute("ALTER TABLE fetch_progress ADD COLUMN verified INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    return conn


def _mark_verified(conn, symbol: str, date_str: str, dtype: str):
    conn.execute(
        "UPDATE fetch_progress SET verified=1 WHERE symbol=? AND date=? AND data_type=?",
        (symbol, date_str, dtype)
    )
    conn.commit()


def check_file(symbol: str, target_date: date, dtype: str = "trades",
               output_dir: Path = None, verbose: bool = True) -> bool:
    """
    Run all correctness checks on a tick file.
    Returns True if all checks pass.
    """
    date_compact = target_date.strftime("%Y%m%d")
    date_str     = target_date.strftime("%Y-%m-%d")

    if output_dir is None:
        try:
            output_dir = Path(get_config().paths.history)
        except Exception:
            output_dir = Path("data/history")

    path = output_dir / f"{symbol}_{dtype}_{date_compact}.csv"

    def _p(msg): verbose and print(msg)
    def _fail(reason):
        _p(f"  FAIL  {symbol} {dtype} {date_str}: {reason}")
        return False

    _p(f"\n--- Verify {symbol} {dtype} {date_str} ---")

    if not path.exists():
        return _fail("file not found")
    if path.stat().st_size < 200:
        return _fail("file too small (< 200 bytes)")

    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    _p(f"  Rows: {len(rows):,}")

    if len(rows) < 10:
        return _fail(f"only {len(rows)} rows")

    min_rows = _MIN_ROWS.get(symbol, 5000)
    if len(rows) < min_rows:
        _p(f"  WARN  row count {len(rows):,} < expected {min_rows:,} — may be a short session or holiday")

    # Parse timestamps
    try:
        times = [datetime.fromisoformat(r["time_utc"]) for r in rows]
    except Exception as e:
        return _fail(f"timestamp parse error: {e}")

    _p(f"  First: {times[0].astimezone(CT).strftime('%H:%M:%S CT')}")
    _p(f"  Last : {times[-1].astimezone(CT).strftime('%H:%M:%S CT')}")

    # Session window check: first tick should be near 17:00 CT prev day
    first_ct = times[0].astimezone(CT)
    if first_ct.hour not in (16, 17, 18):
        _p(f"  WARN  first tick at {first_ct.strftime('%H:%M CT')} — expected ~17:00 CT")

    # Price range check
    lo, hi = _PRICE_RANGE.get(symbol, (0, 999999))
    if dtype == "trades":
        try:
            prices = [float(r["price"]) for r in rows if r.get("price")]
        except Exception:
            return _fail("price column parse error")
        if not prices:
            return _fail("no valid price values")
        pmin, pmax = min(prices), max(prices)
        _p(f"  Price: {pmin:.2f} – {pmax:.2f}")
        if pmin < lo or pmax > hi:
            return _fail(f"price out of expected range [{lo}, {hi}]")
        zeros = sum(1 for p in prices if p <= 0)
        if zeros:
            return _fail(f"{zeros} rows with price <= 0")

    # Bid/ask inversion check
    if dtype == "bidask":
        try:
            bids = [float(r["bid_p"]) for r in rows if r.get("bid_p")]
            asks = [float(r["ask_p"]) for r in rows if r.get("ask_p")]
        except Exception:
            return _fail("bid/ask column parse error")
        if not bids or not asks:
            return _fail("no valid bid/ask values")
        inv_count = sum(1 for b, a in zip(bids, asks) if b > a and b > 0 and a > 0)
        inv_rate  = inv_count / len(bids)
        _p(f"  Bid/ask inversions: {inv_count} ({inv_rate:.2%})")
        if inv_rate > _MAX_INVERSION_RATE:
            return _fail(f"inversion rate {inv_rate:.2%} > {_MAX_INVERSION_RATE:.2%}")
        # Price range via bids
        pmin, pmax = min(bids), max(bids)
        _p(f"  Bid range: {pmin:.2f} – {pmax:.2f}")
        if pmin < lo or pmax > hi:
            return _fail(f"bid price out of range [{lo}, {hi}]")

    # RTH gap check
    rth_times = [t for t in times
                 if (t.astimezone(CT).hour, t.astimezone(CT).minute) >= _RTH_START
                 and (t.astimezone(CT).hour, t.astimezone(CT).minute) < _RTH_END]
    if rth_times:
        max_gap = timedelta(0)
        for i in range(1, len(rth_times)):
            gap = rth_times[i] - rth_times[i - 1]
            if gap > max_gap:
                max_gap = gap
        gap_min = max_gap.total_seconds() / 60
        _p(f"  RTH max gap: {gap_min:.1f} min")
        if gap_min > _MAX_GAP_MINUTES:
            return _fail(f"RTH gap of {gap_min:.1f} min > {_MAX_GAP_MINUTES} min limit")
    else:
        _p("  WARN  no RTH ticks found — holiday or session issue?")

    _p(f"  PASS  {symbol} {dtype} {date_str}")
    return True


def check_and_mark(symbol: str, target_date: date, dtype: str,
                   output_dir: Path = None) -> bool:
    """Run check_file and update fetch_progress.db if passing."""
    ok = check_file(symbol, target_date, dtype, output_dir)
    if ok:
        try:
            cfg  = get_config()
            conn = _open_progress_db(cfg)
            _mark_verified(conn, symbol, target_date.strftime("%Y-%m-%d"), dtype)
            conn.close()
        except Exception as e:
            log.warning(f"Could not mark verified in DB: {e}")
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galgo data correctness checker")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--date",   default=None, help="YYYY-MM-DD")
    parser.add_argument("--dtype",  default="trades", choices=["trades", "bidask"])
    parser.add_argument("--all-pending", action="store_true",
                        help="Check all finished-but-unverified files in fetch_progress.db")
    args = parser.parse_args()

    cfg = get_config()
    output_dir = Path(cfg.paths.history)

    if args.all_pending:
        try:
            conn = _open_progress_db(cfg)
            rows = conn.execute(
                "SELECT symbol, date, data_type FROM fetch_progress "
                "WHERE finished=1 AND (verified IS NULL OR verified=0)"
            ).fetchall()
            conn.close()
        except Exception as e:
            print(f"DB error: {e}")
            sys.exit(1)
        if not rows:
            print("No unverified finished files.")
            sys.exit(0)
        all_ok = True
        for r in rows:
            sym, date_s, dtype = r["symbol"], r["date"], r["data_type"].lower()
            d = date.fromisoformat(date_s)
            if not check_and_mark(sym, d, dtype, output_dir):
                all_ok = False
        sys.exit(0 if all_ok else 1)

    if not args.symbol or not args.date:
        parser.error("--symbol and --date required (or use --all-pending)")

    target = date.fromisoformat(args.date)
    ok = check_and_mark(args.symbol.upper(), target, args.dtype, output_dir)
    sys.exit(0 if ok else 1)
