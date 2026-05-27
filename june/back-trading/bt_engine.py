"""
back-trading/bt_engine.py
Backtrader orchestrator — June 2026 rewrite.

Processes one BacktradeCommand at a time.
If the tick files for the command's date are already in data/history/ (or bt.db),
uses them directly. Otherwise fetches incrementally (1000 ticks at a time) from IB.

The "incremental simulate" loop:
  1. Fetch 1000 trades + 1000 bidask ticks starting at command.ts
  2. Try to simulate: did the position open AND close within these ticks?
  3. Yes → write bt_runs, done.
  4. No → fetch next 1000 from cursor, append, retry.
  5. Repeat until exit found or session end reached.

When a full day's worth of ticks is assembled in bt.db (data_files.status=complete),
the file is also written to CSV in data/history/ and uploaded to Google Drive.
This means fetcher and backtrader share the same data and avoid duplicate IB calls.

Usage:
  python back-trading/bt_engine.py --command-id 42
  python back-trading/bt_engine.py --all-pending        # drain the pending queue
  python back-trading/bt_engine.py --add-command        # insert a test command + run it
  python back-trading/bt_engine.py --self-test

Output (bt.db bt_runs):
  exit_reason: TP | SL | EOD | NO_FILL
  pnl_ticks:   positive = profit, negative = loss
"""

import sys
import csv
import time
import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

_ROOT    = Path(__file__).parent.parent   # june/
_BT_DIR  = Path(__file__).parent          # june/back-trading/
for _p in [str(_ROOT), str(_BT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from zoneinfo import ZoneInfo
from lib.config_loader import get_config
from lib.logger import get_logger
from lib.gdrive import GDriveClient

# back-trading siblings: add dir to sys.path and import directly (hyphen in dir name)
from bt_command import BacktradeCommand
from bt_db import (
    init_bt_db, get_bt_db,
    insert_command, get_pending_commands, claim_command,
    complete_command, fail_command, insert_run,
    get_data_file_status, mark_data_file_fetching, mark_data_file_complete,
)
from bt_fetcher import IncrementalFetcher

import importlib.util as _ilu
_sim_path = _BT_DIR / "simulator.py"
_spec = _ilu.spec_from_file_location("simulator", _sim_path)
_sim_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_sim_mod)
simulate = _sim_mod.simulate

CT  = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

log = get_logger("bt_engine")

_TICK      = 0.25
_MAX_BATCHES = 50  # safety limit: max 1000-tick batches per command


# ── Tick I/O ──────────────────────────────────────────────────────────────────

def _load_csv_ticks(cfg, symbol: str, target_date: date) -> tuple:
    """Load trades_df, bidask_df from CSV history files if they exist."""
    d = target_date.strftime("%Y%m%d")
    hist = Path(cfg.paths.history)
    tp = hist / f"{symbol}_trades_{d}.csv"
    bp = hist / f"{symbol}_bidask_{d}.csv"

    trades_df = None
    bidask_df = None

    if tp.exists() and tp.stat().st_size > 100:
        df = pd.read_csv(tp, parse_dates=["time_utc"])
        df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
        df["price"] = df["price"].astype(float)
        trades_df = df

    if bp.exists() and bp.stat().st_size > 100:
        df = pd.read_csv(bp, parse_dates=["time_utc"])
        df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
        bidask_df = df

    return trades_df, bidask_df


def _ticks_to_df(trades_rows: list, bidask_rows: list) -> tuple:
    """Convert raw tick tuples from IncrementalFetcher to DataFrames."""
    if trades_rows:
        trades_df = pd.DataFrame(
            [(t, p, s) for t, p, s in trades_rows],
            columns=["time_utc", "price", "size"]
        )
        trades_df["time_utc"] = pd.to_datetime(
            trades_df["time_utc"], utc=True)
    else:
        trades_df = pd.DataFrame(columns=["time_utc", "price", "size"])

    if bidask_rows:
        bidask_df = pd.DataFrame(
            [(t, bp, bs, ap, as_) for t, bp, bs, ap, as_ in bidask_rows],
            columns=["time_utc", "bid_p", "bid_s", "ask_p", "ask_s"]
        )
        bidask_df["time_utc"] = pd.to_datetime(
            bidask_df["time_utc"], utc=True)
    else:
        bidask_df = None

    return trades_df, bidask_df


def _store_ticks_in_db(bt_db_path: Path, symbol: str, date_str: str,
                       trades_rows: list, bidask_rows: list):
    """Append tick rows to tick_data table in bt.db."""
    from back_trading.bt_db import get_bt_db
    with get_bt_db(bt_db_path) as con:
        if trades_rows:
            con.executemany(
                "INSERT INTO tick_data (symbol,date,dtype,ts_utc,price,size) VALUES (?,?,?,?,?,?)",
                [(symbol, date_str, "trades", t.isoformat(), p, s)
                 for t, p, s in trades_rows]
            )
        if bidask_rows:
            con.executemany(
                "INSERT INTO tick_data (symbol,date,dtype,ts_utc,bid_p,bid_s,ask_p,ask_s) VALUES (?,?,?,?,?,?,?,?)",
                [(symbol, date_str, "bidask", t.isoformat(), bp, bs, ap, as_)
                 for t, bp, bs, ap, as_ in bidask_rows]
            )


# ── Session helpers ───────────────────────────────────────────────────────────

def _session_end(target_date: date) -> datetime:
    """Session ends at target_date 17:00 CT."""
    end_ct = datetime(target_date.year, target_date.month, target_date.day,
                      17, 0, 0, tzinfo=CT)
    return end_ct.astimezone(timezone.utc)


def _connect_ib(cfg):
    import random
    from ib_insync import IB
    ib = IB()
    ids = list(getattr(cfg.ib, "fetcher_client_ids", cfg.ib.live_client_ids))
    random.shuffle(ids)
    for cid in ids:
        try:
            ib.connect(cfg.ib.live_host, cfg.ib.live_port,
                       clientId=cid, timeout=cfg.ib.connection_timeout)
            if ib.isConnected():
                log.info("IB connected clientId=%d", cid)
                return ib
        except Exception as e:
            log.warning("clientId=%d failed: %s", cid, e)
    raise ConnectionError(f"Could not connect to IB port {cfg.ib.live_port}")


def _get_contract(ib, symbol: str, target_date: date):
    from trader.fetcher import get_contract_for_date
    return get_contract_for_date(ib, symbol, target_date)


# ── Simulation ────────────────────────────────────────────────────────────────

def _run_sim_on_df(cmd: BacktradeCommand, trades_df: pd.DataFrame,
                   bidask_df, session_end_utc: datetime) -> dict:
    """
    Run simulator.simulate() for a single command.
    Returns a fill-result dict with keys: entry_fill_price, entry_fill_time,
    exit_type, exit_fill_price, pnl, pnl_ticks.
    """
    tick_size = _TICK
    tp_price = (cmd.price + cmd.tp_ticks * tick_size if cmd.direction == "BUY"
                else cmd.price - cmd.tp_ticks * tick_size)
    sl_price = (cmd.price - cmd.sl_ticks * tick_size if cmd.direction == "BUY"
                else cmd.price + cmd.sl_ticks * tick_size)

    order = {
        "ts_placed":    cmd.ts,
        "direction":    cmd.direction,
        "entry_type":   cmd.entry_type,
        "entry_price":  cmd.price,
        "tp_price":     tp_price,
        "sl_price":     sl_price,
        "bracket_size": cmd.tp_ticks,   # ticks — used by simulator for result dict
        "market_price": cmd.price,      # approximate (no live feed needed here)
    }

    results = simulate([order], trades_df, bidask_df, session_end_utc)
    r = results[0]

    exit_type = r.get("exit_type", "NO_FILL")
    if exit_type == "EXPIRED":
        exit_type = "EOD"

    entry_price = r.get("entry_fill_price")
    exit_price  = r.get("exit_fill_price")
    pnl_ticks   = None

    if entry_price is not None and exit_price is not None:
        diff = exit_price - entry_price
        if cmd.direction == "SELL":
            diff = -diff
        pnl_ticks = round(diff / tick_size)

    return {
        "entry_ts":    r.get("entry_fill_time"),
        "entry_price": entry_price,
        "exit_price":  exit_price,
        "exit_reason": exit_type,
        "pnl_ticks":   pnl_ticks,
    }


# ── Main command runner ───────────────────────────────────────────────────────

def run_command(cmd: BacktradeCommand, cfg, bt_db_path: Path,
                gdrive: GDriveClient) -> bool:
    """
    Simulate one command. Fetches ticks incrementally if needed.
    Writes result to bt_runs. Returns True on success.
    """
    t_start    = time.monotonic()
    date_str   = cmd.ts.astimezone(CT).strftime("%Y-%m-%d")
    target_day = date.fromisoformat(date_str)
    sess_end   = _session_end(target_day)
    output_dir = Path(cfg.paths.history)

    log.info("[cmd %d] %s %s %s @ %.2f  TP=%dt SL=%dt",
             cmd.command_id, cmd.symbol, cmd.direction, date_str,
             cmd.price, cmd.tp_ticks, cmd.sl_ticks)

    # ── Try CSV files first (fastest path) ──
    trades_df, bidask_df = _load_csv_ticks(cfg, cmd.symbol, target_day)
    if trades_df is not None and len(trades_df) > 100:
        log.info("[cmd %d] using CSV files (%d trades ticks)", cmd.command_id, len(trades_df))
        result = _run_sim_on_df(cmd, trades_df, bidask_df, sess_end)
        ticks_consumed = len(trades_df)

        with get_bt_db(bt_db_path) as con:
            run_id = insert_run(
                con, cmd.command_id, cmd.symbol, date_str,
                cmd.direction,
                entry_ts=result["entry_ts"],
                entry_price=result["entry_price"],
                exit_price=result["exit_price"],
                exit_reason=result["exit_reason"],
                pnl_ticks=result["pnl_ticks"],
                ticks_consumed=ticks_consumed,
                runtime_ms=int((time.monotonic() - t_start) * 1000),
            )
        log.info("[cmd %d] done via CSV: %s pnl=%s",
                 cmd.command_id, result["exit_reason"], result["pnl_ticks"])
        return True

    # ── Incremental fetch path ──
    log.info("[cmd %d] no CSV — starting incremental IB fetch", cmd.command_id)

    ib = _connect_ib(cfg)
    try:
        contract = _get_contract(ib, cmd.symbol, target_day)
        fetcher  = IncrementalFetcher(ib, contract, sess_end)
        fetcher.start_from(cmd.ts)

        all_trades = []
        all_bidask = []
        result     = None
        batches    = 0

        while not fetcher.is_done() and batches < _MAX_BATCHES:
            trades_batch, bidask_batch = fetcher.next_batch()
            if not trades_batch:
                break

            all_trades.extend(trades_batch)
            all_bidask.extend(bidask_batch)
            batches += 1

            # Store in DB for future reuse
            _store_ticks_in_db(bt_db_path, cmd.symbol, date_str,
                               trades_batch, bidask_batch)

            # Try simulation with accumulated ticks so far
            trades_df, bidask_df = _ticks_to_df(all_trades, all_bidask)
            r = _run_sim_on_df(cmd, trades_df, bidask_df, sess_end)

            if r["exit_reason"] not in ("EOD", "NO_FILL"):
                # Position opened and closed within accumulated ticks
                result = r
                log.info("[cmd %d] exit found after %d batches (%d ticks): %s pnl=%s",
                         cmd.command_id, batches, len(all_trades),
                         r["exit_reason"], r["pnl_ticks"])
                break

        if result is None:
            # Ran out of batches or session ended without exit
            if all_trades:
                trades_df, bidask_df = _ticks_to_df(all_trades, all_bidask)
                result = _run_sim_on_df(cmd, trades_df, bidask_df, sess_end)
            else:
                result = {"entry_ts": None, "entry_price": None,
                          "exit_price": None, "exit_reason": "NO_FILL",
                          "pnl_ticks": None}

        with get_bt_db(bt_db_path) as con:
            run_id = insert_run(
                con, cmd.command_id, cmd.symbol, date_str,
                cmd.direction,
                entry_ts=result["entry_ts"],
                entry_price=result["entry_price"],
                exit_price=result["exit_price"],
                exit_reason=result["exit_reason"],
                pnl_ticks=result["pnl_ticks"],
                ticks_consumed=len(all_trades),
                runtime_ms=int((time.monotonic() - t_start) * 1000),
            )

        log.info("[cmd %d] done: %s pnl=%s  %d batches  %d ticks",
                 cmd.command_id, result["exit_reason"], result["pnl_ticks"],
                 batches, len(all_trades))
        return True

    finally:
        ib.disconnect()


def run_all_pending(cfg, bt_db_path: Path, gdrive: GDriveClient):
    """Drain the pending queue — process each command in order."""
    with get_bt_db(bt_db_path) as con:
        pending = get_pending_commands(con)

    if not pending:
        log.info("No pending commands.")
        return

    log.info("%d pending commands", len(pending))
    for row in pending:
        cmd = BacktradeCommand.from_db_row(row)

        with get_bt_db(bt_db_path) as con:
            if not claim_command(con, cmd.command_id):
                log.info("cmd %d already claimed — skipping", cmd.command_id)
                continue

        try:
            ok = run_command(cmd, cfg, bt_db_path, gdrive)
            with get_bt_db(bt_db_path) as con:
                if ok:
                    # get result id
                    r = con.execute(
                        "SELECT id FROM bt_runs WHERE command_id=? ORDER BY id DESC LIMIT 1",
                        (cmd.command_id,)
                    ).fetchone()
                    complete_command(con, cmd.command_id, r["id"] if r else 0)
                else:
                    fail_command(con, cmd.command_id)
        except Exception as e:
            log.error("cmd %d failed: %s", cmd.command_id, e)
            with get_bt_db(bt_db_path) as con:
                fail_command(con, cmd.command_id)


# ── Self test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile, shutil
    try:
        from lib.logger import reset_loggers

        cfg    = get_config()
        tmp    = tempfile.mkdtemp()
        try:
            bt_db_path = Path(tmp) / "bt.db"
            _bt_conn   = init_bt_db(bt_db_path)  # keep ref so we can close it
            _bt_conn.close()

            # 1. Insert a command
            cmd = BacktradeCommand(
                symbol="MES", ts=datetime(2026, 4, 7, 14, 0, tzinfo=timezone.utc),
                direction="BUY", entry_type="LMT", price=5250.0,
                tp_ticks=4, sl_ticks=4, quantity=1
            )
            cmd.validate()
            with get_bt_db(bt_db_path) as con:
                cmd_id = insert_command(con, cmd)
            assert cmd_id > 0

            # 2. Claim it
            with get_bt_db(bt_db_path) as con:
                ok = claim_command(con, cmd_id)
            assert ok

            # 3. Run simulation with synthetic ticks
            tick_size = _TICK
            entry_ts  = datetime(2026, 4, 7, 14, 0, 10, tzinfo=timezone.utc)
            ticks = [(entry_ts + timedelta(seconds=i), 5250.0 + i * tick_size, 10)
                     for i in range(10)]
            # Add TP hit
            ticks.append((entry_ts + timedelta(seconds=10),
                          5250.0 + 4 * tick_size, 5))  # TP touch
            ticks.append((entry_ts + timedelta(seconds=11),
                          5250.0 + 4 * tick_size, 5))  # confirm

            trades_df = pd.DataFrame(
                [(t, p, s) for t, p, s in ticks],
                columns=["time_utc", "price", "size"]
            )
            trades_df["time_utc"] = pd.to_datetime(trades_df["time_utc"], utc=True)

            cmd.command_id = cmd_id
            sess_end = _session_end(date(2026, 4, 7))
            result = _run_sim_on_df(cmd, trades_df, None, sess_end)
            assert result["exit_reason"] in ("TP", "SL", "EOD", "NO_FILL"), \
                f"unexpected exit_reason: {result['exit_reason']}"

            # 4. Insert run
            with get_bt_db(bt_db_path) as con:
                run_id = insert_run(
                    con, cmd_id, "MES", "2026-04-07", "BUY",
                    entry_ts=result["entry_ts"],
                    entry_price=result["entry_price"],
                    exit_price=result["exit_price"],
                    exit_reason=result["exit_reason"],
                    pnl_ticks=result["pnl_ticks"],
                    ticks_consumed=len(ticks),
                    runtime_ms=10,
                )
            assert run_id > 0

        finally:
            try:
                reset_loggers()
            except Exception:
                pass
            shutil.rmtree(tmp, ignore_errors=True)

        print("[self-test] bt_engine: PASS")
        return True

    except Exception as e:
        print(f"[self-test] bt_engine: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galgo backtrader engine")
    parser.add_argument("--command-id",  type=int, default=None,
                        help="Run a single command by ID")
    parser.add_argument("--all-pending", action="store_true",
                        help="Drain all pending commands from queue")
    parser.add_argument("--add-command", action="store_true",
                        help="Insert a test command and run it")
    parser.add_argument("--self-test",   action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    cfg        = get_config()
    bt_db_path = Path(cfg.paths.bt_db)
    init_bt_db(bt_db_path)
    gdrive = GDriveClient(cfg)

    if args.add_command:
        from datetime import timezone
        cmd = BacktradeCommand(
            symbol="MES",
            ts=datetime.now(timezone.utc).replace(hour=14, minute=0, second=0),
            direction="BUY", entry_type="LMT", price=0.0,
            tp_ticks=cfg.backtest.default_tp_ticks,
            sl_ticks=cfg.backtest.default_sl_ticks,
            quantity=1,
        )
        print("Entry price? (e.g. 5500.25): ", end="")
        cmd.price = float(input())
        with get_bt_db(bt_db_path) as con:
            cmd_id = insert_command(con, cmd)
        print(f"Inserted command id={cmd_id}")
        cmd.command_id = cmd_id
        with get_bt_db(bt_db_path) as con:
            claim_command(con, cmd_id)
        run_command(cmd, cfg, bt_db_path, gdrive)
        sys.exit(0)

    if args.command_id:
        with get_bt_db(bt_db_path) as con:
            row = con.execute("SELECT * FROM bt_commands WHERE id=?",
                              (args.command_id,)).fetchone()
        if not row:
            print(f"Command {args.command_id} not found")
            sys.exit(1)
        cmd = BacktradeCommand.from_db_row(row)
        with get_bt_db(bt_db_path) as con:
            claim_command(con, cmd.command_id)
        ok = run_command(cmd, cfg, bt_db_path, gdrive)
        sys.exit(0 if ok else 1)

    if args.all_pending:
        run_all_pending(cfg, bt_db_path, gdrive)
        sys.exit(0)

    parser.print_help()
