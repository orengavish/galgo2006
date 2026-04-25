"""
back-trading/engine.py
Backtest engine for Galao.

Modes:
  Historical simulation (uses cached or freshly-fetched tick data):
    python engine.py --date 2026-04-09
    python engine.py --from 2026-04-01 --to 2026-04-09

  Reality model (live paper trading + end-of-day grading):
    python engine.py --reality-model

  Fetch tick data only (no simulation):
    python engine.py --fetch --date 2026-04-09

  Self-test:
    python engine.py --self-test

How it works:
  1. generator.py  — picks N random timestamps in RTH, places LMT BUY/SELL
                     at market_price +/- small_offset with symmetric brackets
  2. simulator.py  — replays tick-by-tick, fills using realistic bid/ask model
  3. reality_model.py — (--reality-model) submits same orders to IB paper live
  4. grader.py     — compares sim fills to paper fills -> accuracy score
"""

import sys
import csv
import argparse
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

_ROOT   = Path(__file__).parent.parent          # galgo2026/
_TRADER = _ROOT / "trader"                      # galgo2026/trader/
for p in [str(_ROOT), str(_TRADER)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from lib.config_loader import get_config
from lib.logger import get_logger
import generator as gen
import simulator as sim
import grader as grd
from db import init_db

log = get_logger("engine")


# ── Tick data ─────────────────────────────────────────────────────────────────

def _tick_paths(cfg, symbol: str, target_date: date) -> tuple:
    bars = Path(cfg.paths.history)
    d    = target_date.strftime("%Y%m%d")
    return (bars / f"{symbol}_trades_{d}.csv",
            bars / f"{symbol}_bidask_{d}.csv")


def _fetch_ticks(cfg, symbol: str, target_date: date) -> None:
    """Fetch TRADES + BID_ASK ticks from IB and save to data/history/."""
    import random
    from ib_insync import IB
    from fetcher import (get_contract_for_date, get_session_bounds,
                         paginate_ticks, _init_progress_db)
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")

    bars = Path(cfg.paths.history)
    bars.mkdir(parents=True, exist_ok=True)
    prog_db = bars.parent / "fetch_progress.db"
    progress_conn = _init_progress_db(prog_db)

    ib  = IB()
    ids = list(getattr(cfg.ib, "fetcher_client_ids", cfg.ib.live_client_ids))
    random.shuffle(ids)
    for cid in ids:
        try:
            ib.connect(cfg.ib.live_host, cfg.ib.live_port,
                       clientId=cid, timeout=cfg.ib.connection_timeout)
            if ib.isConnected():
                log.info(f"IB connected for fetch: clientId={cid}")
                break
        except Exception as e:
            log.warning(f"clientId={cid} failed: {e}")

    if not ib.isConnected():
        progress_conn.close()
        raise ConnectionError("Cannot connect to IB for tick data fetch")

    try:
        contract  = get_contract_for_date(ib, symbol, target_date)
        start_utc, end_utc = get_session_bounds(target_date)
        date_str  = target_date.isoformat()
        d_compact = target_date.strftime("%Y%m%d")

        for dtype, suffix, headers in [
            ("TRADES",  "trades",
             ["time_ct", "time_utc", "price", "size", "symbol"]),
            ("BID_ASK", "bidask",
             ["time_ct", "time_utc", "bid_p", "bid_s", "ask_p", "ask_s", "symbol"]),
        ]:
            out = bars / f"{symbol}_{suffix}_{d_compact}.csv"
            if out.exists():
                log.info(f"[SKIP] {out.name} already cached")
                continue

            log.info(f"[FETCH] {symbol} {dtype} {target_date}")
            with open(out, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(headers)

                def _write(tick, t_u, _dtype=dtype):
                    t_c = t_u.astimezone(CT)
                    if _dtype == "TRADES":
                        w.writerow([t_c.isoformat(), t_u.isoformat(),
                                    tick.price, tick.size, contract.localSymbol])
                    else:
                        w.writerow([t_c.isoformat(), t_u.isoformat(),
                                    tick.priceBid, tick.sizeBid,
                                    tick.priceAsk, tick.sizeAsk,
                                    contract.localSymbol])

                count = paginate_ticks(ib, contract, start_utc, end_utc,
                                       dtype, _write, progress_conn, symbol, date_str)
            log.info(f"[DONE] {symbol} {dtype} {target_date}: {count:,} ticks")
    finally:
        progress_conn.close()
        ib.disconnect()


def _load_df(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    return df


def _ensure_ticks(cfg, symbol: str, target_date: date):
    """Return (trades_df, bidask_df). Fetches from IB if files are missing."""
    trades_path, bidask_path = _tick_paths(cfg, symbol, target_date)

    if not trades_path.exists() or not bidask_path.exists():
        log.info(f"Tick data missing for {symbol} {target_date} — fetching from IB")
        _fetch_ticks(cfg, symbol, target_date)

    trades_df = _load_df(trades_path)
    bidask_df = _load_df(bidask_path) if bidask_path.exists() else None

    if bidask_df is None:
        log.warning(f"BID_ASK not found — falling back to TRADES for entry fills")

    log.info(f"Loaded {len(trades_df):,} trade ticks"
             + (f", {len(bidask_df):,} bid/ask ticks" if bidask_df is not None else ""))
    return trades_df, bidask_df


# ── Single day simulation ─────────────────────────────────────────────────────

def run_day(cfg, symbol: str, target_date: date,
            db: sqlite3.Connection, mode: str = "sim"):
    """
    Run generation + simulation for one day.
    Returns (run_id, orders, order_db_ids, sim_results).
    All four are None/[] if no orders could be generated.
    """
    gcfg = cfg.generator
    trades_df, bidask_df = _ensure_ticks(cfg, symbol, target_date)

    orders = gen.generate(
        trades_df        = trades_df,
        target_date      = target_date,
        bracket_sizes    = list(gcfg.bracket_sizes),
        n_timestamps     = gcfg.n_timestamps,
        entry_offset_min = gcfg.entry_offset_min,
        entry_offset_max = gcfg.entry_offset_max,
        symbol           = symbol,
    )

    if not orders:
        log.warning(f"No orders generated for {symbol} {target_date}")
        return None, [], [], []

    n_ts = gcfg.n_timestamps
    log.info(f"Generated {len(orders)} orders "
             f"({n_ts} timestamps x {len(list(gcfg.bracket_sizes))} brackets x 2 directions)")

    # --- persist run + orders ---
    run_id = db.execute(
        "INSERT INTO runs (date, symbol, mode, created_at) VALUES (?,?,?,?)",
        (target_date.isoformat(), symbol, mode,
         datetime.now(timezone.utc).isoformat())
    ).lastrowid
    db.commit()

    order_db_ids = []
    for o in orders:
        oid = db.execute("""
            INSERT INTO sim_orders
            (run_id, ts_placed, direction, entry_type, entry_price,
             tp_price, sl_price, bracket_size, market_price, entry_offset)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            run_id,
            o["ts_placed"].isoformat(), o["direction"], o["entry_type"],
            o["entry_price"], o["tp_price"], o["sl_price"],
            o["bracket_size"], o["market_price"], o["entry_offset"],
        )).lastrowid
        order_db_ids.append(oid)
    db.commit()

    # --- simulate ---
    from fetcher import get_session_bounds
    _, session_end = get_session_bounds(target_date)
    sim_results = sim.simulate(orders, trades_df, bidask_df, session_end)

    # --- persist sim fills ---
    for i, r in enumerate(sim_results):
        db.execute("""
            INSERT INTO sim_fills
            (order_id, entry_fill_price, entry_fill_time,
             exit_type, exit_fill_price, exit_fill_time, pnl)
            VALUES (?,?,?,?,?,?,?)
        """, (
            order_db_ids[i],
            r.get("entry_fill_price"),
            r["entry_fill_time"].isoformat() if r.get("entry_fill_time") else None,
            r.get("exit_type"),
            r.get("exit_fill_price"),
            r["exit_fill_time"].isoformat() if r.get("exit_fill_time") else None,
            r.get("pnl"),
        ))
    db.commit()

    return run_id, orders, order_db_ids, sim_results


# ── Console output ────────────────────────────────────────────────────────────

def print_timeline(sim_results: list, target_date: date) -> None:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")

    filled    = [r for r in sim_results if r.get("exit_type") in ("TP", "SL")]
    expired   = [r for r in sim_results if r.get("exit_type") == "EXPIRED"]
    total_pnl = sum(r["pnl"] for r in filled if r.get("pnl") is not None)
    n_tp      = sum(1 for r in filled if r.get("exit_type") == "TP")
    n_sl      = sum(1 for r in filled if r.get("exit_type") == "SL")

    print(f"\n{'─'*78}")
    print(f"  {target_date}  |  {len(sim_results)} orders  "
          f"|  {len(filled)} filled  |  {len(expired)} expired")
    print(f"{'─'*78}")
    print(f"  {'Placed':>8}  {'D':>4}  {'BkSz':>4}  "
          f"{'Entry':>8}  {'Filled@':>8}  {'Exit':>4}  {'Exit@':>8}  {'P&L':>8}")
    print(f"{'─'*78}")

    for r in sorted(sim_results, key=lambda x: x.get("ts_placed") or datetime.max):
        ts    = r["ts_placed"].astimezone(CT).strftime("%H:%M:%S") if r.get("ts_placed") else "?"
        d     = r.get("direction", "?")[0]
        bksz  = r.get("bracket_size", "?")
        ep    = f"{r['entry_price']:.2f}"
        efp   = f"{r['entry_fill_price']:.2f}" if r.get("entry_fill_price") else "—"
        etype = r.get("exit_type", "?")
        exfp  = f"{r['exit_fill_price']:.2f}" if r.get("exit_fill_price") else "—"
        pnl   = f"${r['pnl']:+.2f}" if r.get("pnl") is not None else "—"
        print(f"  {ts:>8}  {d:>4}  {bksz:>4}  {ep:>8}  {efp:>8}  {etype:>4}  {exfp:>8}  {pnl:>8}")

    print(f"{'─'*78}")
    print(f"  Session P&L: ${total_pnl:+.2f}  "
          f"({len(filled)} trades: {n_tp} TP, {n_sl} SL, {len(expired)} expired)\n")


def print_grades(grades: dict, target_date: date) -> None:
    print(f"\n{'─'*68}")
    print(f"  Grade: {target_date}")
    print(f"{'─'*68}")
    for bs, g in sorted(grades.items()):
        print(f"  Bracket {bs:>4}pt  |  {g['grade_pct']:>5.1f}%  "
              f"({g['matched_1tick']}/{g['total_trades']} within 1 tick)  "
              f"|  sim ${g['sim_pnl']:+.2f}  paper ${g['paper_pnl']:+.2f}  "
              f"diff ${g['pnl_diff']:+.2f}")
    print(f"{'─'*68}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def run(date_from: str, date_to: str, symbol: str = "MES",
        reality_model: bool = False) -> None:
    cfg    = get_config()
    db     = init_db(Path(cfg.paths.db))
    d_from = date.fromisoformat(date_from)
    d_to   = date.fromisoformat(date_to)

    curr = d_from
    while curr <= d_to:
        log.info(f"=== {symbol} {curr} ===")

        if reality_model:
            run_id, orders, order_db_ids, sim_results = run_day(
                cfg, symbol, curr, db, mode="reality"
            )
            if run_id is None:
                curr += timedelta(days=1)
                continue

            from reality_model import RealityModel
            rm = RealityModel(cfg, db, run_id)
            paper_results = rm.run(orders, order_db_ids, symbol, curr)

            grades = grd.grade(sim_results, paper_results)
            for bs, g in grades.items():
                db.execute("""
                    INSERT INTO grades
                    (run_id, bracket_size, total_trades, matched_1tick, matched_2tick,
                     grade_pct, sim_pnl, paper_pnl, pnl_diff, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (
                    run_id, bs,
                    g["total_trades"], g["matched_1tick"], g["matched_2tick"],
                    g["grade_pct"], g["sim_pnl"], g["paper_pnl"], g["pnl_diff"],
                    datetime.now(timezone.utc).isoformat()
                ))
            db.commit()
            print_grades(grades, curr)

        else:
            _, _, _, sim_results = run_day(cfg, symbol, curr, db, mode="sim")
            if sim_results:
                print_timeline(sim_results, curr)

        curr += timedelta(days=1)
        while curr.weekday() >= 5:          # skip Sat/Sun
            curr += timedelta(days=1)

    db.close()


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    try:
        cfg = get_config()
        assert hasattr(cfg, "backtest"),  "Config missing backtest section"
        assert hasattr(cfg, "generator"), "Config missing generator section"
        assert hasattr(cfg, "grader"),    "Config missing grader section"
        assert cfg.generator.n_timestamps > 0
        assert len(list(cfg.generator.bracket_sizes)) > 0

        import db as _db, generator as _gen, simulator as _sim, grader as _grd
        ok = all([
            _db.self_test(),
            _gen.self_test(),
            _sim.self_test(),
            _grd.self_test(),
        ])
        if ok:
            print("[self-test] engine: PASS")
        return ok

    except Exception as e:
        print(f"[self-test] engine: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao back-trading engine")
    parser.add_argument("--self-test",     action="store_true")
    parser.add_argument("--date",          help="Single date YYYY-MM-DD")
    parser.add_argument("--from",          dest="date_from", help="Start date")
    parser.add_argument("--to",            dest="date_to",   help="End date")
    parser.add_argument("--symbol",        default="MES")
    parser.add_argument("--reality-model", action="store_true",
                        help="Submit to IB paper + grade at day-end")
    parser.add_argument("--fetch",         action="store_true",
                        help="Fetch tick data only, no simulation")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    if args.fetch:
        cfg = get_config()
        d   = date.fromisoformat(args.date) if args.date else date.today()
        _fetch_ticks(cfg, args.symbol, d)
        sys.exit(0)

    d_from = args.date_from or args.date or date.today().isoformat()
    d_to   = args.date_to   or args.date or d_from
    run(d_from, d_to, symbol=args.symbol, reality_model=args.reality_model)
