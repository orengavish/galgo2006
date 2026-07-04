"""
back-trading/cl_algo_worker.py
Per-symbol worker: runs backtester → scorer → learner for one symbol.

Designed for parallel execution: each Claude instance owns one symbol via lock file.
Two instances running different symbols will share the same galao.db safely (WAL mode).

Lock file: data/cl_algo_{SYMBOL}.lock — prevents duplicate workers on same symbol.

Usage:
    python back-trading/cl_algo_worker.py --symbol MES
    python back-trading/cl_algo_worker.py --symbol MNQ
    python back-trading/cl_algo_worker.py --symbol MES --dry-run
    python back-trading/cl_algo_worker.py --self-test
"""

import sys
import os
import time
import json
import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.db import get_db, init_db


def _lock_path(symbol: str, data_dir: Path) -> Path:
    return data_dir / f"cl_algo_{symbol}.lock"


def _acquire_lock(symbol: str, data_dir: Path) -> bool:
    """Write PID to lock file. Return False if another live process holds it."""
    lock = _lock_path(symbol, data_dir)
    if lock.exists():
        try:
            existing_pid = int(lock.read_text().strip())
            import psutil
            if psutil.pid_exists(existing_pid):
                return False  # live process holds lock
            # Stale lock — clear it
            lock.unlink(missing_ok=True)
        except Exception:
            lock.unlink(missing_ok=True)
    lock.write_text(str(os.getpid()))
    return True


def _release_lock(symbol: str, data_dir: Path):
    _lock_path(symbol, data_dir).unlink(missing_ok=True)


def _get_learner_recommendation(db_path: Path, symbol: str) -> dict | None:
    """Load the most recent learner run recommendation for a symbol."""
    try:
        with get_db(db_path) as con:
            row = con.execute("""
                SELECT recommended_tp_ticks, recommended_sl_ticks, convergence_status
                FROM cl_algo_learner_runs
                WHERE symbol=?
                ORDER BY id DESC LIMIT 1
            """, (symbol,)).fetchone()
        if row:
            return {
                "tp_ticks": json.loads(row[0] or "[]"),
                "sl_ticks": json.loads(row[1] or "[]"),
                "convergence_status": row[2],
            }
    except Exception:
        pass
    return None


def run_worker(symbol: str, db_path: Path, history_dir: Path,
               dry_run: bool = False, verbose: bool = False) -> dict:
    """
    Full pipeline for one symbol:
      1. Load learner recommendation (if any)
      2. Run backtester with recommended params (or coarse default)
      3. Run scorer
      4. Run learner → write next recommendation
    Returns summary dict.
    """
    from back_trading_imports import _import_backtester, _import_scorer, _import_learner
    backtester = _import_backtester()
    scorer     = _import_scorer()
    learner    = _import_learner()

    init_db(db_path)

    # Step 1: get learner recommendation
    rec = _get_learner_recommendation(db_path, symbol)
    combos = None
    if rec and rec["tp_ticks"] and rec["sl_ticks"]:
        combos = backtester.build_combos(
            tp_ticks=rec["tp_ticks"],
            sl_ticks=rec["sl_ticks"],
        )
        if verbose:
            print(f"[{symbol}] Using learner recommendation: "
                  f"tp={rec['tp_ticks']} sl={rec['sl_ticks']}")
    else:
        if verbose:
            print(f"[{symbol}] No learner recommendation — using coarse default grid")

    # Step 2: backtest
    t0 = time.monotonic()
    bt_result = backtester.run(
        db_path=db_path,
        history_dir=history_dir,
        symbols=[symbol],
        combos=combos,
        dry_run=dry_run,
        verbose=verbose,
    )
    bt_elapsed = round(time.monotonic() - t0, 1)

    # Step 3: score
    score_result = scorer.score(db_path, symbol, verbose=verbose)

    # Step 4: learner recommendation for next run
    learn_result = learner.recommend(db_path, symbol, dry_run=dry_run)

    summary = {
        "symbol":          symbol,
        "bt_written":      bt_result.get("written", 0),
        "bt_skipped":      bt_result.get("skipped", 0),
        "bt_elapsed_s":    bt_elapsed,
        "n_combos_scored": score_result.get("n_ranked", 0),
        "top_combo":       score_result.get("top"),
        "convergence":     learn_result.get("convergence_status"),
        "next_tp_ticks":   learn_result.get("recommended_tp_ticks"),
        "next_sl_ticks":   learn_result.get("recommended_sl_ticks"),
        "dry_run":         dry_run,
    }
    return summary


# ── Dynamic importer (avoids top-level circular deps) ─────────────────────────

class _Imports:
    pass

def _import_module(name: str, path: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Self-test ─────────────────────────────────────────────────────────────────

def _self_test() -> bool:
    """Run worker self-test: lock acquisition + release, no real data needed."""
    print("Running cl_algo_worker self-test...")
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            # Lock acquisition
            ok = _acquire_lock("MES", data_dir)
            assert ok, "First lock acquire should succeed"

            # Second acquire should fail (same PID, but lock exists)
            # Simulate another process by writing a valid PID to the lock
            import os, psutil
            lock = _lock_path("MES", data_dir)
            # Write current PID as the holder
            lock.write_text(str(os.getpid()))
            ok2 = _acquire_lock("MES", data_dir)
            assert not ok2, "Second acquire should fail while first holds lock"

            # Release and retry
            _release_lock("MES", data_dir)
            ok3 = _acquire_lock("MES", data_dir)
            assert ok3, "Acquire after release should succeed"
            _release_lock("MES", data_dir)

            # Stale lock (dead PID) → auto-cleared
            lock.write_text("9999999")  # almost certainly not a live PID
            ok4 = _acquire_lock("MES", data_dir)
            assert ok4, "Stale lock should be auto-cleared"
            _release_lock("MES", data_dir)

        # Test that MNQ lock is independent from MES
        with tempfile.TemporaryDirectory() as tmp2:
            data_dir2 = Path(tmp2)
            ok_mes = _acquire_lock("MES", data_dir2)
            ok_mnq = _acquire_lock("MNQ", data_dir2)
            assert ok_mes and ok_mnq, "Different symbols should lock independently"
            _release_lock("MES", data_dir2)
            _release_lock("MNQ", data_dir2)

        print("PASS -- worker: lock acquire/release, stale cleanup, symbol independence")
        return True

    except Exception as e:
        import traceback
        print(f"FAIL -- {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CL Algo Worker (per-symbol)")
    parser.add_argument("--symbol",   required=False, default="MES")
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--verbose",  action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    from lib.config_loader import get_config
    cfg      = get_config()
    db_path  = Path(cfg.paths.db)
    hist_dir = db_path.parent / "history"
    data_dir = db_path.parent

    sym = args.symbol.upper()
    if not _acquire_lock(sym, data_dir):
        print(f"[{sym}] Another worker is already running for this symbol. Exiting.")
        sys.exit(0)

    try:
        # Dynamic imports to avoid circular dep
        bt  = _import_module("cl_algo_backtester", Path(__file__).parent / "cl_algo_backtester.py")
        sc  = _import_module("cl_algo_scorer",    Path(__file__).parent / "cl_algo_scorer.py")
        lr  = _import_module("cl_algo_learner",   Path(__file__).parent / "cl_algo_learner.py")

        init_db(db_path)
        rec = _get_learner_recommendation(db_path, sym)
        combos = None
        if rec and rec["tp_ticks"] and rec["sl_ticks"]:
            combos = bt.build_combos(tp_ticks=rec["tp_ticks"], sl_ticks=rec["sl_ticks"])

        bt_result    = bt.run(db_path, hist_dir, symbols=[sym], combos=combos,
                              dry_run=args.dry_run, verbose=args.verbose)
        score_result = sc.score(db_path, sym, verbose=args.verbose)
        learn_result = lr.recommend(db_path, sym, dry_run=args.dry_run)

        top = score_result.get("top")
        print(f"[{sym}] done  written={bt_result.get('written',0)}"
              f"  top={top['algo_type']} tp={top['tp_ticks']} sl={top['sl_ticks']}"
              f" score={top['composite_score']:.4f}" if top else f"[{sym}] no ranked combos yet")
        print(f"[{sym}] next: tp={learn_result['recommended_tp_ticks']}"
              f" sl={learn_result['recommended_sl_ticks']}"
              f" status={learn_result['convergence_status']}")
    finally:
        _release_lock(sym, data_dir)
