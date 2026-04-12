"""
back-trading/db.py
SQLite schema for backtest runs, orders, fills, and grades.

Tables:
  runs        — one row per engine invocation (sim or reality-model)
  sim_orders  — generated synthetic brackets
  sim_fills   — simulated fill results
  paper_fills — actual IB paper fill results (reality-model mode only)
  grades      — accuracy scores comparing sim to paper

Self-test:
  python back-trading/db.py --self-test
"""

import sys
import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            mode        TEXT NOT NULL,        -- 'sim' | 'reality'
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sim_orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER NOT NULL REFERENCES runs(id),
            ts_placed       TEXT NOT NULL,    -- ISO UTC, when order was placed
            direction       TEXT NOT NULL,    -- BUY | SELL
            entry_type      TEXT NOT NULL,    -- LMT | STP
            entry_price     REAL NOT NULL,
            tp_price        REAL NOT NULL,
            sl_price        REAL NOT NULL,
            bracket_size    REAL NOT NULL,
            market_price    REAL NOT NULL,    -- actual price at ts_placed
            entry_offset    REAL NOT NULL     -- distance placed from market
        );

        CREATE TABLE IF NOT EXISTS sim_fills (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id         INTEGER NOT NULL REFERENCES sim_orders(id),
            entry_fill_price REAL,
            entry_fill_time  TEXT,
            exit_type        TEXT,            -- TP | SL | EXPIRED
            exit_fill_price  REAL,
            exit_fill_time   TEXT,
            pnl              REAL,
            slippage_ticks   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS paper_fills (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id         INTEGER NOT NULL REFERENCES sim_orders(id),
            ib_entry_id      INTEGER,         -- IB orderId for the entry leg
            entry_fill_price REAL,
            entry_fill_time  TEXT,
            exit_type        TEXT,            -- TP | SL | EXPIRED
            exit_fill_price  REAL,
            exit_fill_time   TEXT,
            pnl              REAL
        );

        CREATE TABLE IF NOT EXISTS grades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER NOT NULL REFERENCES runs(id),
            bracket_size    REAL NOT NULL,
            total_trades    INTEGER NOT NULL,
            matched_1tick   INTEGER NOT NULL,
            matched_2tick   INTEGER NOT NULL,
            grade_pct       REAL NOT NULL,
            sim_pnl         REAL,
            paper_pnl       REAL,
            pnl_diff        REAL,
            created_at      TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


# ── Self-test ──────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile, os
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = Path(f.name)
        try:
            conn = init_db(tmp)

            # Insert a run
            run_id = conn.execute(
                "INSERT INTO runs (date, symbol, mode, created_at) VALUES (?,?,?,?)",
                ("2026-04-09", "MES", "sim", datetime.now(timezone.utc).isoformat())
            ).lastrowid
            conn.commit()
            assert run_id == 1

            # Insert an order
            order_id = conn.execute("""
                INSERT INTO sim_orders
                (run_id, ts_placed, direction, entry_type, entry_price,
                 tp_price, sl_price, bracket_size, market_price, entry_offset)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (run_id, "2026-04-09T14:00:00Z", "BUY", "LMT",
                  6500.0, 6502.0, 6498.0, 2.0, 6500.5, 0.5)).lastrowid
            conn.commit()
            assert order_id == 1

            # Insert a fill
            conn.execute("""
                INSERT INTO sim_fills
                (order_id, entry_fill_price, entry_fill_time,
                 exit_type, exit_fill_price, exit_fill_time, pnl)
                VALUES (?,?,?,?,?,?,?)
            """, (order_id, 6500.0, "2026-04-09T14:00:05Z",
                  "TP", 6502.0, "2026-04-09T14:05:00Z", 10.0))
            conn.commit()

            # Verify
            row = conn.execute(
                "SELECT pnl FROM sim_fills WHERE order_id=?", (order_id,)
            ).fetchone()
            assert row[0] == 10.0, f"pnl wrong: {row[0]}"

            conn.close()
        finally:
            os.unlink(tmp)

        print("[self-test] db: PASS")
        return True

    except Exception as e:
        print(f"[self-test] db: FAIL — {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("db.py — run --self-test to verify")
