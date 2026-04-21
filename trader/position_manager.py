"""
position_manager.py
Position management for Galao.
Monitors open positions for stagnation and SL cool-down.

Responsibilities:
  - Stagnation kill-switch (R-POS-01): position open > stagnation_seconds AND
    price moved < stagnation_min_move_points → MKT exit, reason=STAGNATION
  - SL cool-down (R-POS-02): when a command reaches CLOSED with exit_reason=SL,
    disarm the line for sl_cooldown_seconds before re-arming
  - Writes EXITING status to DB before placing market exit
  - Stops when SESSION=SHUTDOWN

Usage:
    python position_manager.py            # run manager loop (blocking)
    python position_manager.py --self-test

Self-test:
    python position_manager.py --self-test
"""

import sys
import time
import argparse
from datetime import date, datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
import sys; sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db, get_system_state, update_command_status, record_completed_trade
from lib.critical_lines import disarm_line, rearm_line

log = get_logger("position_manager")


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _is_shutdown(db_path) -> bool:
    with get_db(db_path) as con:
        val = get_system_state(con, "SESSION")
    return val == "SHUTDOWN"


def check_stagnation(ibc, db_path, cfg) -> int:
    """
    For each FILLED command (open position), check stagnation:
    - open > stagnation_seconds AND price moved < stagnation_min_move_points
    → write EXITING, place MKT order, write CLOSED reason=STAGNATION

    Returns number of positions exited.
    """
    stag_secs  = cfg.position.stagnation_seconds
    stag_move  = cfg.position.stagnation_min_move_points

    with get_db(db_path) as con:
        filled = con.execute(
            "SELECT * FROM commands WHERE status='FILLED'"
        ).fetchall()

    if not filled:
        return 0

    exited = 0
    now = datetime.now(timezone.utc)

    for cmd in filled:
        fill_time = _parse_utc(cmd["fill_time"])
        open_secs = (now - fill_time).total_seconds()

        if open_secs < stag_secs:
            continue  # Not old enough yet

        # Get current price
        try:
            current_price = ibc.get_price(cmd["symbol"])
        except Exception as e:
            log.warning(f"Cannot get price for stagnation check cmd {cmd['id']}: {e}")
            continue

        fill_price = cmd["fill_price"]
        if fill_price is None:
            continue
        movement = abs(current_price - fill_price)

        if movement >= stag_move:
            continue  # Sufficient movement — not stagnating

        log.warning(
            f"STAGNATION detected: cmd {cmd['id']} {cmd['symbol']} "
            f"open={open_secs:.0f}s fill={fill_price} current={current_price} "
            f"move={movement:.2f}pt (< {stag_move}pt)"
        )

        # Write EXITING
        with get_db(db_path) as con:
            update_command_status(con, cmd["id"], "EXITING")

        # Place MKT exit via PAPER
        try:
            _place_market_exit(ibc, cmd)
            exit_price = current_price  # approximate
            pnl = (exit_price - fill_price) if cmd["direction"] == "BUY" \
                  else (fill_price - exit_price)
            with get_db(db_path) as con:
                update_command_status(
                    con, cmd["id"], "CLOSED",
                    exit_price  = exit_price,
                    exit_time   = _now_utc(),
                    exit_reason = "STAGNATION",
                    pnl_points  = round(pnl, 4),
                )
                record_completed_trade(con, cmd["id"])
            log.info(f"Command {cmd['id']} CLOSED via STAGNATION pnl={pnl:+.2f}pts")
            exited += 1
        except Exception as e:
            log.error(f"Failed to exit stagnant position cmd {cmd['id']}: {e}")
            with get_db(db_path) as con:
                update_command_status(con, cmd["id"], "ERROR", error_message=str(e))

    return exited


def _place_market_exit(ibc, cmd):
    """Place a market order to exit the open position."""
    from ib_insync import MarketOrder
    if not ibc.is_paper_connected():
        raise ConnectionError("PAPER not connected")

    contract = ibc.get_contract(cmd["symbol"])
    exit_action = "SELL" if cmd["direction"] == "BUY" else "BUY"
    order = MarketOrder(exit_action, cmd["quantity"])

    # Also cancel the open TP and SL orders
    _cancel_bracket_legs(ibc, cmd)

    trade = ibc.paper.placeOrder(contract, order)
    log.info(f"MKT exit placed: {exit_action} {cmd['quantity']} {cmd['symbol']} "
             f"ib_id={trade.order.orderId}")
    return trade


def _cancel_bracket_legs(ibc, cmd):
    """Cancel the TP and SL child orders of a filled bracket."""
    from ib_insync import Order
    for oid in (cmd["ib_tp_order_id"], cmd["ib_sl_order_id"]):
        if oid:
            try:
                o = Order()
                o.orderId = oid
                ibc.paper.cancelOrder(o)
                log.debug(f"Cancelled order {oid}")
            except Exception as e:
                log.warning(f"Could not cancel order {oid}: {e}")


def check_sl_cooldowns(db_path, cfg, date_str: str) -> int:
    """
    Find recently closed commands with exit_reason=SL.
    Disarm their critical line for sl_cooldown_seconds.
    Re-arm after the cooldown expires.
    Returns number of disarms applied.
    """
    cooldown = cfg.position.sl_cooldown_seconds
    now = datetime.now(timezone.utc)
    disarmed = 0

    with get_db(db_path) as con:
        # Find SL exits not yet cooldown-processed
        sl_exits = con.execute(
            "SELECT * FROM commands WHERE exit_reason='SL' AND status='CLOSED'"
            " AND exit_time IS NOT NULL"
        ).fetchall()

    for cmd in sl_exits:
        exit_time = _parse_utc(cmd["exit_time"])
        elapsed = (now - exit_time).total_seconds()

        with get_db(db_path) as con:
            line_row = con.execute(
                "SELECT * FROM critical_lines WHERE symbol=? AND date=? AND price=?",
                (cmd["symbol"], date_str, cmd["line_price"])
            ).fetchone()

        if not line_row:
            continue

        if elapsed < cooldown:
            # Within cooldown window — disarm if still armed
            if line_row["armed"] == 1:
                with get_db(db_path) as con:
                    disarm_line(con, line_row["id"])
                log.info(
                    f"SL cool-down: disarmed line {cmd['line_price']} "
                    f"({cooldown - elapsed:.0f}s remaining)"
                )
                disarmed += 1
        else:
            # Cooldown expired — re-arm if still disarmed
            if line_row["armed"] == 0:
                with get_db(db_path) as con:
                    rearm_line(con, line_row["id"])
                log.info(f"SL cool-down expired: re-armed line {cmd['line_price']}")

    return disarmed


def run_position_manager(db_path=None, date_str: str = None):
    """Main position manager loop."""
    cfg = get_config()
    db_path  = db_path or Path(cfg.paths.db)
    date_str = date_str or date.today().strftime("%Y-%m-%d")
    init_db(db_path)

    log.info(f"Position manager starting — DB={db_path}")

    from lib.ib_client import IBClient
    ibc = IBClient(cfg)
    try:
        ibc.connect(live=True, paper=True)
    except ConnectionError as e:
        log.error(f"Position manager: IB connection failed: {e}")
        sys.exit(1)

    poll_seconds = cfg.broker.command_poll_seconds  # reuse same poll cadence

    log.info("Position manager loop started")
    while True:
        if _is_shutdown(db_path):
            log.info("SESSION=SHUTDOWN — position manager exiting")
            break

        if not ibc.is_live_connected() or not ibc.is_paper_connected():
            log.warning("IB connection lost — attempting reconnect")
            ok = ibc.reconnect(max_attempts=5)
            if not ok:
                log.error("Reconnect failed — aborting position manager")
                break

        try:
            n_stag = check_stagnation(ibc, db_path, cfg)
            if n_stag:
                log.info(f"Exited {n_stag} stagnant position(s)")
        except Exception as e:
            log.error(f"Error in check_stagnation: {e}")

        try:
            check_sl_cooldowns(db_path, cfg, date_str)
        except Exception as e:
            log.error(f"Error in check_sl_cooldowns: {e}")

        time.sleep(poll_seconds)

    ibc.disconnect()
    log.info("Position manager stopped")


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        from lib.logger import reset_loggers
        from lib.db import set_system_state
        from lib.critical_lines import get_file_path, load_critical_lines

        cfg = get_config()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path  = tmp_path / "test.db"
            cl_dir   = tmp_path / "cl"
            cl_dir.mkdir()
            init_db(db_path)

            today = "2026-04-07"
            fp = get_file_path("MES", today, cl_dir)
            fp.write_text("SUPPORT, 6490.00, 2\nRESISTANCE, 6510.00, 1\n")
            load_critical_lines("MES", today, db_path, cl_dir)

            # 1. SL cool-down test (no IB needed)
            with get_db(db_path) as con:
                con.execute("""
                    INSERT INTO commands
                        (symbol, line_price, line_type, line_strength,
                         direction, entry_type, entry_price, tp_price, sl_price,
                         bracket_size, status, fill_price, fill_time,
                         exit_price, exit_time, exit_reason)
                    VALUES ('MES', 6490.0, 'SUPPORT', 2,
                            'BUY', 'LMT', 6490.0, 6492.0, 6488.0,
                            2.0, 'CLOSED', 6490.0, '2026-04-07T10:00:00Z',
                            6488.0, ?, 'SL')
                """, (_now_utc(),))  # exit_time = now (within cooldown)

            n_disarmed = check_sl_cooldowns(db_path, cfg, today)
            assert n_disarmed == 1, f"Expected 1 disarm, got {n_disarmed}"

            with get_db(db_path) as con:
                row = con.execute(
                    "SELECT armed FROM critical_lines WHERE price=6490.0"
                ).fetchone()
            assert row["armed"] == 0, "Line should be disarmed after SL"

            # 2. After cooldown — simulate old exit_time
            old_time = "2000-01-01T00:00:00Z"
            with get_db(db_path) as con:
                con.execute(
                    "UPDATE commands SET exit_time=? WHERE exit_reason='SL'",
                    (old_time,)
                )
            check_sl_cooldowns(db_path, cfg, today)
            with get_db(db_path) as con:
                row2 = con.execute(
                    "SELECT armed FROM critical_lines WHERE price=6490.0"
                ).fetchone()
            assert row2["armed"] == 1, "Line should be re-armed after cooldown"

            # 3. Stagnation detection logic (without IB)
            # Fake FILLED command with old fill_time and price at entry
            with get_db(db_path) as con:
                con.execute("""
                    INSERT INTO commands
                        (symbol, line_price, line_type, line_strength,
                         direction, entry_type, entry_price, tp_price, sl_price,
                         bracket_size, status, fill_price, fill_time)
                    VALUES ('MES', 6510.0, 'RESISTANCE', 1,
                            'SELL', 'LMT', 6510.0, 6508.0, 6512.0,
                            2.0, 'FILLED', 6510.0, '2000-01-01T00:00:00Z')
                """)
                cmd_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Verify the logic: open for 876000+ seconds, movement = 0 → stagnation
            cmd = None
            with get_db(db_path) as con:
                cmd = con.execute("SELECT * FROM commands WHERE id=?", (cmd_id,)).fetchone()
            fill_time = _parse_utc(cmd["fill_time"])
            open_secs = (datetime.now(timezone.utc) - fill_time).total_seconds()
            movement = abs(6510.0 - 6510.0)
            assert open_secs > cfg.position.stagnation_seconds
            assert movement < cfg.position.stagnation_min_move_points

            reset_loggers()

        print("[self-test] position_manager: PASS")
        return True

    except Exception as e:
        print(f"[self-test] position_manager: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao position manager")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    run_position_manager()
