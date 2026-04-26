"""
back-trading/calibrate.py
Step B: replay verified IB paper trades through simulator to calibrate phase 2.

For each date in verified_trades that has a tick CSV, run simulate_exit() on
every trade and compare the simulated exit to the actual.
Results saved to the backtest DB (calib_runs / calib_details tables).

Usage:
    python back-trading/calibrate.py
    python back-trading/calibrate.py --save --iteration 0 --change-name baseline
    python back-trading/calibrate.py --save --iteration 1 --change-name stagnation --description "add stagnation timeout"
    python back-trading/calibrate.py --history   # print all saved runs
    python back-trading/calibrate.py --self-test
"""

import sys
import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime, date, timezone
from pathlib import Path

import pandas as pd

_ROOT   = Path(__file__).parent.parent
_TRADER = _ROOT / "trader"
for p in [str(_ROOT), str(_TRADER)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from lib.config_loader import get_config
import simulator as sim
from db import init_db


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_trades_df(history_dir: Path, symbol: str, d: date) -> pd.DataFrame | None:
    path = history_dir / f"{symbol}_trades_{d.strftime('%Y%m%d')}.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    return df


def _session_end(d: date) -> datetime:
    """17:00 CT = 22:00 UTC for most sessions."""
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
    naive = datetime(d.year, d.month, d.day, 17, 0, 0)
    return naive.replace(tzinfo=CT).astimezone(timezone.utc)


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ── Stats collector ───────────────────────────────────────────────────────────

class _Stats:
    def __init__(self):
        self.total        = 0
        self.type_match   = 0
        self.price_deltas = []   # ticks, type-matched TP/SL only

    def add(self, matched: bool, delta_ticks: float | None):
        self.total += 1
        if matched:
            self.type_match += 1
            if delta_ticks is not None:
                self.price_deltas.append(delta_ticks)

    def pct(self):
        return self.type_match / self.total * 100 if self.total else 0.0

    def avg_delta(self):
        return sum(self.price_deltas) / len(self.price_deltas) if self.price_deltas else None


# ── Core calibration ──────────────────────────────────────────────────────────

def calibrate(min_date: str | None = None, use_stagnation: bool = True) -> dict:
    cfg         = get_config()
    db_path     = Path(cfg.paths.live_db)
    history_dir = Path(cfg.paths.history)
    symbol      = cfg.backtest.symbol

    # Stagnation params — from position config, matching what the live system used
    if use_stagnation:
        stag_seconds = cfg.position.stagnation_seconds
        stag_move    = cfg.position.stagnation_min_move_points
    else:
        stag_seconds = None
        stag_move    = None

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    query  = "SELECT * FROM verified_trades WHERE symbol=?"
    params = [symbol]
    if min_date:
        query += " AND fill_time >= ?"
        params.append(min_date)
    rows = conn.execute(query, params).fetchall()
    conn.close()

    by_date: dict[date, list] = defaultdict(list)
    for row in rows:
        d = _parse_utc(row["fill_time"]).date()
        by_date[d].append(row)

    overall        = _Stats()
    by_bracket     = defaultdict(_Stats)
    by_exit_reason = defaultdict(_Stats)
    by_source      = defaultdict(_Stats)

    processed_dates = []
    skipped_dates   = []
    trade_count     = 0

    for d in sorted(by_date):
        trades_df = _load_trades_df(history_dir, symbol, d)
        if trades_df is None:
            skipped_dates.append(d)
            continue

        processed_dates.append(d)
        sess_end = _session_end(d)

        for row in by_date[d]:
            fill_time   = _parse_utc(row["fill_time"])
            actual_exit = row["exit_reason"]   # TP | SL | STAGNATION

            result = sim.simulate_exit(
                fill_price      = row["fill_price"],
                fill_time       = fill_time,
                tp_price        = row["tp_price"],
                sl_price        = row["sl_price"],
                direction       = row["direction"],
                trades_df       = trades_df,
                session_end_utc = sess_end,
                stag_seconds    = stag_seconds,
                stag_move       = stag_move,
            )

            sim_exit       = result["exit_type"]
            sim_exit_price = result["exit_fill_price"]
            matched        = (sim_exit == actual_exit)

            # Price delta only when both agree on TP/SL and we have both prices
            delta = None
            if matched and sim_exit in ("TP", "SL") and sim_exit_price is not None:
                delta = abs(sim_exit_price - row["exit_price"]) / 0.25

            bs  = row["bracket_size"]
            src = row["source"] or "unknown"

            overall.add(matched, delta)
            by_bracket[bs].add(matched, delta)
            by_exit_reason[actual_exit].add(matched, delta)
            by_source[src].add(matched, delta)
            trade_count += 1

    # Convenience: per-reason pct for DB saving
    tp_pct   = by_exit_reason["TP"].pct()   if "TP"          in by_exit_reason else None
    sl_pct   = by_exit_reason["SL"].pct()   if "SL"          in by_exit_reason else None
    stag_pct = by_exit_reason["STAGNATION"].pct() if "STAGNATION" in by_exit_reason else None

    return {
        "processed_dates": processed_dates,
        "skipped_dates":   skipped_dates,
        "total_in_db":     len(rows),
        "trade_count":     trade_count,
        "overall":         overall,
        "by_bracket":      dict(by_bracket),
        "by_exit_reason":  dict(by_exit_reason),
        "by_source":       dict(by_source),
        "tp_pct":          tp_pct,
        "sl_pct":          sl_pct,
        "stag_pct":        stag_pct,
    }


# ── DB persistence ────────────────────────────────────────────────────────────

def save_to_db(r: dict, iteration: int, change_name: str, description: str) -> int:
    """Save calibration results to backtest DB. Returns run_id."""
    cfg    = get_config()
    db     = init_db(Path(cfg.paths.db))
    overall = r["overall"]

    # Compare to previous best
    prev = db.execute(
        "SELECT overall_pct FROM calib_runs ORDER BY overall_pct DESC LIMIT 1"
    ).fetchone()
    is_better = None
    if prev is not None:
        is_better = 1 if overall.pct() > prev[0] else 0

    run_id = db.execute("""
        INSERT INTO calib_runs
        (ran_at, iteration, change_name, description,
         trades_analyzed, overall_pct, tp_pct, sl_pct, stag_pct,
         avg_delta_ticks, is_better)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        iteration, change_name, description,
        r["trade_count"],
        round(overall.pct(), 2),
        round(r["tp_pct"],   2) if r["tp_pct"]   is not None else None,
        round(r["sl_pct"],   2) if r["sl_pct"]   is not None else None,
        round(r["stag_pct"], 2) if r["stag_pct"] is not None else None,
        round(overall.avg_delta(), 2) if overall.avg_delta() is not None else None,
        is_better,
    )).lastrowid
    db.commit()

    def _save_dim(dimension: str, items: dict):
        for label, stats in items.items():
            db.execute("""
                INSERT INTO calib_details
                (run_id, dimension, label, total, matched, match_pct, avg_delta_ticks)
                VALUES (?,?,?,?,?,?,?)
            """, (
                run_id, dimension, str(label),
                stats.total, stats.type_match,
                round(stats.pct(), 2),
                round(stats.avg_delta(), 2) if stats.avg_delta() is not None else None,
            ))

    _save_dim("bracket",     r["by_bracket"])
    _save_dim("exit_reason", r["by_exit_reason"])
    _save_dim("source",      r["by_source"])
    db.commit()
    db.close()
    return run_id


def print_history():
    cfg = get_config()
    db  = init_db(Path(cfg.paths.db))
    rows = db.execute(
        "SELECT id, ran_at, iteration, change_name, overall_pct, tp_pct, sl_pct, "
        "stag_pct, avg_delta_ticks, is_better FROM calib_runs ORDER BY id"
    ).fetchall()
    db.close()

    if not rows:
        print("No calibration runs saved yet.")
        return

    print(f"\n{'='*88}")
    print(f"  {'ID':>3}  {'Iter':>4}  {'Change':<24}  {'Overall':>8}  "
          f"{'TP':>7}  {'SL':>7}  {'STAG':>7}  {'AvgDelta':>9}  Better")
    print(f"{'='*88}")
    for r in rows:
        better = {1: "YES", 0: "no", None: "base"}[r[9]]
        tp   = f"{r[5]:.1f}%" if r[5] is not None else "   n/a"
        sl   = f"{r[6]:.1f}%" if r[6] is not None else "   n/a"
        stag = f"{r[7]:.1f}%" if r[7] is not None else "   n/a"
        adelta = f"{r[8]:.1f}tk" if r[8] is not None else "    n/a"
        print(f"  {r[0]:>3}  {r[2]:>4}  {r[3]:<24}  {r[4]:>7.1f}%  "
              f"{tp:>7}  {sl:>7}  {stag:>7}  {adelta:>9}  {better}")
    print(f"{'='*88}\n")


# ── Console report ────────────────────────────────────────────────────────────

def print_report(r: dict, run_id: int | None = None) -> None:
    overall = r["overall"]
    print(f"\n{'='*64}")
    print(f"  Calibration Report — simulator.py phase-2 accuracy")
    if run_id is not None:
        print(f"  Saved as run #{run_id}")
    print(f"{'='*64}")
    print(f"  Dates processed : {len(r['processed_dates'])} "
          f"{[str(d) for d in r['processed_dates']]}")
    if r["skipped_dates"]:
        print(f"  Dates skipped   : {len(r['skipped_dates'])} (no tick file) "
              f"{[str(d) for d in r['skipped_dates']]}")
    print(f"  Trades analyzed : {r['trade_count']} / {r['total_in_db']} total in DB")

    if r["trade_count"] == 0:
        print("\n  No trades to analyze — fetch tick data first.\n")
        return

    def _fmt(stats: _Stats, label: str, width: int = 20) -> str:
        avg   = stats.avg_delta()
        avg_s = f"{avg:.1f}tk" if avg is not None else "  n/a"
        return (f"  {label:<{width}}  "
                f"{stats.type_match:>4}/{stats.total:<4}  "
                f"({stats.pct():>5.1f}%)  avg_delta={avg_s}")

    print(f"\n  OVERALL")
    print(_fmt(overall, "all"))

    print(f"\n  BY BRACKET SIZE")
    for bs in sorted(r["by_bracket"]):
        print(_fmt(r["by_bracket"][bs], f"bracket {bs:.0f}pt"))

    print(f"\n  BY ACTUAL EXIT REASON")
    for reason in sorted(r["by_exit_reason"]):
        print(_fmt(r["by_exit_reason"][reason], reason))

    print(f"\n  BY SOURCE")
    for src in sorted(r["by_source"]):
        print(_fmt(r["by_source"][src], src))

    print(f"\n{'='*64}")
    print(f"  Type match rate: {overall.pct():.1f}%  "
          f"({overall.type_match}/{overall.total})\n")


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    UTC = ZoneInfo("UTC")
    try:
        base = datetime(2026, 4, 24, 14, 0, 0, tzinfo=UTC)

        trades = pd.DataFrame([
            {"time_utc": base + timedelta(seconds=10), "price": 6500.0, "size": 2},
            {"time_utc": base + timedelta(seconds=20), "price": 6502.0, "size": 2},
            {"time_utc": base + timedelta(seconds=30), "price": 6501.0, "size": 2},
        ])
        sess_end = base + timedelta(hours=3)

        # BUY tp=6502 → TP at t+20
        r = sim.simulate_exit(
            fill_price=6500.0, fill_time=base,
            tp_price=6502.0, sl_price=6498.0,
            direction="BUY", trades_df=trades, session_end_utc=sess_end,
        )
        assert r["exit_type"] == "TP"
        assert r["exit_fill_price"] == 6502.0

        # SELL sl=6502 → SL at t+20 (slippage +1 tick)
        r2 = sim.simulate_exit(
            fill_price=6500.0, fill_time=base,
            tp_price=6498.0, sl_price=6502.0,
            direction="SELL", trades_df=trades, session_end_utc=sess_end,
        )
        assert r2["exit_type"] == "SL"
        assert r2["exit_fill_price"] == 6502.25

        # EXPIRED
        r3 = sim.simulate_exit(
            fill_price=6500.0, fill_time=base,
            tp_price=6510.0, sl_price=6490.0,
            direction="BUY", trades_df=trades, session_end_utc=sess_end,
        )
        assert r3["exit_type"] == "EXPIRED"

        print("[self-test] calibrate: PASS")
        return True

    except Exception as e:
        print(f"[self-test] calibrate: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Calibrate simulator against real paper trades")
    parser.add_argument("--self-test",   action="store_true")
    parser.add_argument("--history",     action="store_true",  help="Print saved run history")
    parser.add_argument("--save",        action="store_true",  help="Persist results to DB")
    parser.add_argument("--iteration",   type=int, default=0)
    parser.add_argument("--change-name", default="baseline")
    parser.add_argument("--description", default="")
    parser.add_argument("--min-date",       help="Only trades on or after YYYY-MM-DD")
    parser.add_argument("--no-stagnation", action="store_true",
                        help="Disable stagnation model (baseline mode)")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    if args.history:
        print_history()
        sys.exit(0)

    report = calibrate(min_date=args.min_date, use_stagnation=not args.no_stagnation)
    run_id = None
    if args.save:
        run_id = save_to_db(report, args.iteration, args.change_name, args.description)
    print_report(report, run_id)
