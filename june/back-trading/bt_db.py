"""
back-trading/bt_db.py
Database schema for the June 2026 backtrader.

Separate DB from galao.db — backtrader is independent of live trading.
Uses WAL mode so multiple processes can read/write concurrently.

Tables:
  bt_commands  — queue of BacktradeCommands to simulate
  bt_runs      — simulation results (one row per command)
  tick_data    — raw ticks stored in DB (alternative to CSV files)
  data_files   — control table: (symbol, date, dtype) → completion status

Usage:
  from back_trading.bt_db import get_bt_db, init_bt_db
  with get_bt_db(path) as con:
      ...
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS bt_commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    ts          TEXT    NOT NULL,  -- ISO UTC, start of simulation window
    direction   TEXT    NOT NULL,  -- BUY | SELL
    entry_type  TEXT    NOT NULL,  -- MKT | LMT
    price       REAL    NOT NULL,
    tp_ticks    INTEGER NOT NULL,
    sl_ticks    INTEGER NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 1,
    status      TEXT    NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
    result_id   INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS bt_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id      INTEGER NOT NULL,
    symbol          TEXT    NOT NULL,
    date            TEXT    NOT NULL,  -- YYYY-MM-DD of simulated session
    entry_ts        TEXT,              -- actual fill timestamp (UTC ISO)
    direction       TEXT    NOT NULL,
    entry_price     REAL,
    exit_price      REAL,
    exit_reason     TEXT,   -- TP | SL | EOD | TIMEOUT | NO_FILL
    pnl_ticks       INTEGER,
    ticks_consumed  INTEGER,
    runtime_ms      INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    FOREIGN KEY (command_id) REFERENCES bt_commands(id)
);

CREATE TABLE IF NOT EXISTS data_files (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,   -- YYYY-MM-DD
    dtype       TEXT NOT NULL,   -- trades | bidask
    status      TEXT NOT NULL DEFAULT 'missing',  -- missing|fetching|complete
    tick_count  INTEGER,
    updated_at  TEXT,
    drive_file_id TEXT,
    PRIMARY KEY (symbol, date, dtype)
);

CREATE TABLE IF NOT EXISTS tick_data (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol  TEXT    NOT NULL,
    date    TEXT    NOT NULL,  -- YYYY-MM-DD
    dtype   TEXT    NOT NULL,  -- trades | bidask
    ts_utc  TEXT    NOT NULL,  -- ISO UTC
    price   REAL,
    size    INTEGER,
    bid_p   REAL,
    bid_s   INTEGER,
    ask_p   REAL,
    ask_s   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tick_data_lookup
    ON tick_data(symbol, date, dtype, ts_utc);

CREATE INDEX IF NOT EXISTS idx_bt_commands_status
    ON bt_commands(status);
"""


def init_bt_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()
    return conn


@contextmanager
def get_bt_db(path: Path):
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── bt_commands helpers ───────────────────────────────────────────────────────

def insert_command(conn, cmd) -> int:
    cur = conn.execute(
        "INSERT INTO bt_commands (symbol, ts, direction, entry_type, price, "
        "tp_ticks, sl_ticks, quantity) VALUES (?,?,?,?,?,?,?,?)",
        (cmd.symbol, cmd.ts.isoformat(), cmd.direction, cmd.entry_type,
         cmd.price, cmd.tp_ticks, cmd.sl_ticks, cmd.quantity)
    )
    conn.commit()
    return cur.lastrowid


def get_pending_commands(conn) -> list:
    return conn.execute(
        "SELECT * FROM bt_commands WHERE status='pending' ORDER BY id"
    ).fetchall()


def claim_command(conn, command_id: int) -> bool:
    """Mark a command as running (optimistic lock). Returns True if claimed."""
    cur = conn.execute(
        "UPDATE bt_commands SET status='running' "
        "WHERE id=? AND status='pending'",
        (command_id,)
    )
    conn.commit()
    return cur.rowcount == 1


def complete_command(conn, command_id: int, result_id: int):
    conn.execute(
        "UPDATE bt_commands SET status='done', result_id=? WHERE id=?",
        (result_id, command_id)
    )
    conn.commit()


def fail_command(conn, command_id: int):
    conn.execute(
        "UPDATE bt_commands SET status='failed' WHERE id=?",
        (command_id,)
    )
    conn.commit()


# ── bt_runs helpers ───────────────────────────────────────────────────────────

def insert_run(conn, command_id: int, symbol: str, date_str: str,
               direction: str, entry_ts=None, entry_price=None,
               exit_price=None, exit_reason=None,
               pnl_ticks=None, ticks_consumed=0, runtime_ms=0) -> int:
    cur = conn.execute(
        "INSERT INTO bt_runs (command_id, symbol, date, direction, "
        "entry_ts, entry_price, exit_price, exit_reason, "
        "pnl_ticks, ticks_consumed, runtime_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (command_id, symbol, date_str, direction,
         entry_ts.isoformat() if entry_ts else None,
         entry_price, exit_price, exit_reason,
         pnl_ticks, ticks_consumed, runtime_ms)
    )
    conn.commit()
    return cur.lastrowid


# ── data_files helpers ────────────────────────────────────────────────────────

def get_data_file_status(conn, symbol: str, date_str: str, dtype: str) -> str:
    row = conn.execute(
        "SELECT status FROM data_files WHERE symbol=? AND date=? AND dtype=?",
        (symbol, date_str, dtype)
    ).fetchone()
    return row["status"] if row else "missing"


def mark_data_file_fetching(conn, symbol: str, date_str: str, dtype: str):
    conn.execute(
        "INSERT INTO data_files (symbol, date, dtype, status, updated_at) VALUES (?,?,?,'fetching',?)"
        " ON CONFLICT(symbol,date,dtype) DO UPDATE SET status='fetching', updated_at=excluded.updated_at",
        (symbol, date_str, dtype, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()


def mark_data_file_complete(conn, symbol: str, date_str: str, dtype: str,
                            tick_count: int, drive_file_id: str = ""):
    conn.execute(
        "INSERT INTO data_files (symbol, date, dtype, status, tick_count, updated_at, drive_file_id) "
        "VALUES (?,?,?,'complete',?,?,?)"
        " ON CONFLICT(symbol,date,dtype) DO UPDATE SET "
        "status='complete', tick_count=excluded.tick_count, "
        "updated_at=excluded.updated_at, drive_file_id=excluded.drive_file_id",
        (symbol, date_str, dtype, tick_count,
         datetime.now(timezone.utc).isoformat(), drive_file_id)
    )
    conn.commit()
