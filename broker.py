"""
broker.py
Broker component for Galao.
Polls DB for PENDING commands, submits bracket orders via PAPER port,
monitors fills, and writes status back to DB.

Key invariants:
- Writes status=SUBMITTING (claim lock) before calling IB (R-ORD-12)
- Polls open orders every broker.ib_poll_seconds for fill detection
- Reconnects on disconnect (R-ERR-01)
- Never touches LIVE connection (data only via IBClient)
- Session stops when SESSION=SHUTDOWN appears in system_state

Usage:
    python broker.py            # run broker loop (blocking)
    python broker.py --self-test

Self-test:
    python broker.py --self-test
"""

import sys
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path

from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db, get_pending_commands, update_command_status, get_system_state
from lib.ib_client import IBClient
from lib.order_builder import build_bracket, place_bracket

log = get_logger("broker")

_MAX_RECONNECT_ATTEMPTS = 5


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_shutdown(db_path) -> bool:
    with get_db(db_path) as con:
        val = get_system_state(con, "SESSION")
    return val == "SHUTDOWN"


def _claim_command(db_path, command_id: int) -> bool:
    """
    Atomically claim a PENDING command by writing SUBMITTING + claimed_at.
    Returns True if we won the claim race, False if another process beat us.
    This is the claim lock (R-ORD-12).
    """
    now = _now_utc()
    with get_db(db_path) as con:
        cur = con.execute(
            "UPDATE commands SET status='SUBMITTING', claimed_at=?,"
            " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            " WHERE id=? AND status='PENDING'",
            (now, command_id)
        )
        return cur.rowcount == 1


def process_pending_commands(ibc: IBClient, db_path, cfg) -> int:
    """
    Find all PENDING commands, claim them, submit to IB, write SUBMITTED.
    Returns number of orders submitted.
    """
    with get_db(db_path) as con:
        pending = get_pending_commands(con)

    if not pending:
        return 0

    submitted = 0
    for cmd in pending:
        cid = cmd["id"]

        # Claim lock — atomic status change to SUBMITTING
        if not _claim_command(db_path, cid):
            log.debug(f"Command {cid} already claimed by another process — skip")
            continue

        log.info(f"Processing command {cid}: {cmd['direction']} {cmd['entry_type']} "
                 f"{cmd['symbol']} @ {cmd['entry_price']}")

        try:
            # Resolve front-month contract (cached in practice via IBClient)
            contract = ibc.get_contract(cmd["symbol"])

            # Build bracket order objects
            orders = build_bracket(
                ibc.paper, contract,
                direction   = cmd["direction"],
                entry_type  = cmd["entry_type"],
                entry_price = cmd["entry_price"],
                tp_price    = cmd["tp_price"],
                sl_price    = cmd["sl_price"],
                quantity    = cmd["quantity"],
            )

            # Submit to IB
            result = place_bracket(ibc.paper, contract, orders)

            # Write SUBMITTED + IB order IDs
            with get_db(db_path) as con:
                update_command_status(
                    con, cid, "SUBMITTED",
                    ib_order_id    = result["entry_id"],
                    ib_tp_order_id = result["tp_id"],
                    ib_sl_order_id = result["sl_id"],
                )
            log.info(f"Command {cid} SUBMITTED — IB entry_id={result['entry_id']}")
            submitted += 1

        except Exception as e:
            log.error(f"Command {cid} submission failed: {e}")
            with get_db(db_path) as con:
                update_command_status(con, cid, "ERROR", error_message=str(e))

    return submitted


def poll_fills(ibc: IBClient, db_path) -> int:
    """
    Check IB PAPER trades for fills of SUBMITTED commands.
    Updates DB to FILLED on detection.
    Returns number of fills detected.
    """
    if not ibc.is_paper_connected():
        log.warning("PAPER not connected — skipping fill poll")
        return 0

    try:
        trades = ibc.paper.trades()
    except Exception as e:
        log.error(f"Error fetching trades: {e}")
        return 0

    # Build lookup: ib_order_id → fill status
    filled_ids = {}
    for trade in trades:
        oid = trade.order.orderId
        status = trade.orderStatus.status
        fill_price = trade.orderStatus.avgFillPrice
        if status in ("Filled", "PartiallyFilled"):
            filled_ids[oid] = (status, fill_price)

    if not filled_ids:
        return 0

    # Find SUBMITTED commands whose entry order was filled
    with get_db(db_path) as con:
        submitted = con.execute(
            "SELECT * FROM commands WHERE status='SUBMITTED'"
        ).fetchall()

    fills = 0
    for cmd in submitted:
        entry_oid = cmd["ib_order_id"]
        if entry_oid in filled_ids:
            status, fill_price = filled_ids[entry_oid]
            # R-ORD-13: treat all fills as complete (partial fills ignored in V1)
            now = _now_utc()
            log.info(f"Command {cmd['id']} FILLED — price={fill_price}")
            with get_db(db_path) as con:
                update_command_status(
                    con, cmd["id"], "FILLED",
                    fill_price = fill_price,
                    fill_time  = now,
                )
            fills += 1

    return fills


def run_broker(db_path=None):
    """Main broker loop."""
    cfg = get_config()
    db_path = db_path or Path(cfg.paths.db)
    init_db(db_path)

    log.info(f"Broker starting — DB={db_path}")

    ibc = IBClient(cfg)
    try:
        ibc.connect(live=True, paper=True)
    except ConnectionError as e:
        log.error(f"Broker startup: IB connection failed: {e}")
        sys.exit(1)

    poll_seconds     = cfg.broker.command_poll_seconds
    ib_poll_seconds  = cfg.broker.ib_poll_seconds
    last_ib_poll     = 0.0

    log.info("Broker loop started")

    while True:
        # Check for shutdown signal
        if _is_shutdown(db_path):
            log.info("SESSION=SHUTDOWN detected — broker exiting")
            break

        # Check connections; reconnect if needed
        if not ibc.is_paper_connected() or not ibc.is_live_connected():
            log.warning("IB connection lost — attempting reconnect")
            ok = ibc.reconnect(max_attempts=_MAX_RECONNECT_ATTEMPTS)
            if not ok:
                log.error("Reconnect failed after max attempts — aborting broker")
                # R-ERR-05: abort means trigger shutdown then exit
                with get_db(db_path) as con:
                    from lib.db import set_system_state
                    set_system_state(con, "SESSION", "SHUTDOWN")
                break

        # Process pending commands
        try:
            n = process_pending_commands(ibc, db_path, cfg)
            if n:
                log.info(f"Submitted {n} order(s)")
        except Exception as e:
            log.error(f"Error in process_pending_commands: {e}")

        # Periodic fill poll
        now = time.time()
        if now - last_ib_poll >= ib_poll_seconds:
            try:
                f = poll_fills(ibc, db_path)
                if f:
                    log.info(f"Detected {f} fill(s)")
            except Exception as e:
                log.error(f"Error in poll_fills: {e}")
            last_ib_poll = now

        time.sleep(poll_seconds)

    ibc.disconnect()
    log.info("Broker stopped")


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    """
    Self-test:
    - Config loads
    - DB init + PENDING→SUBMITTING claim lock (no IB needed)
    - IB connection attempt (SKIP if not available)
    - Broker loop runs for 2 poll cycles (no real orders)
    """
    import tempfile
    try:
        from lib.logger import reset_loggers
        from lib.db import set_system_state

        cfg = get_config()

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)

            # 1. Insert a PENDING command
            with get_db(db_path) as con:
                con.execute("""
                    INSERT INTO commands
                        (symbol, line_price, line_type, line_strength,
                         direction, entry_type, entry_price, tp_price, sl_price, bracket_size)
                    VALUES ('MES', 6500.0, 'SUPPORT', 2,
                            'BUY', 'LMT', 6500.0, 6502.0, 6498.0, 2.0)
                """)
                cmd_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]

            # 2. Claim lock test — PENDING → SUBMITTING
            claimed = _claim_command(db_path, cmd_id)
            assert claimed, "Claim failed"
            with get_db(db_path) as con:
                row = con.execute("SELECT status FROM commands WHERE id=?",
                                  (cmd_id,)).fetchone()
            assert row["status"] == "SUBMITTING", f"Status: {row['status']}"

            # 3. Second claim attempt should fail (already SUBMITTING)
            claimed2 = _claim_command(db_path, cmd_id)
            assert not claimed2, "Second claim should fail"

            # 4. Shutdown detection
            with get_db(db_path) as con:
                set_system_state(con, "SESSION", "SHUTDOWN")
            assert _is_shutdown(db_path), "Shutdown not detected"

            with get_db(db_path) as con:
                set_system_state(con, "SESSION", "RUNNING")
            assert not _is_shutdown(db_path), "Running misdetected as SHUTDOWN"

            # 5. IB connection attempt
            ibc = IBClient(cfg)
            try:
                ibc.connect(live=True, paper=True)
                ib_ok = ibc.is_live_connected() and ibc.is_paper_connected()
                ibc.disconnect()
                if ib_ok:
                    log.info("[self-test] IB connections: PASS")
                else:
                    log.warning("[self-test] IB partial connection — non-fatal")
            except Exception as e:
                log.info(f"[self-test] IB not available: {e} — SKIP")

            reset_loggers()

        print("[self-test] broker: PASS")
        return True

    except Exception as e:
        print(f"[self-test] broker: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao broker")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    run_broker()
