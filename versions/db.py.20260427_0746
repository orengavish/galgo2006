"""
lib/db.py
SQLite database setup and helpers for Galao.
Initializes schema, enables WAL mode, and provides CRUD helpers.

Tables: commands, positions, ib_events, system_state, critical_lines, release_notes

Usage:
    from lib.db import get_db, init_db
    with get_db() as con:
        con.execute("SELECT * FROM commands WHERE status='PENDING'")

Self-test:
    python -m lib.db --self-test
"""

import sys
import sqlite3
import argparse
from pathlib import Path
from contextlib import contextmanager

_db_path: Path = None


def set_db_path(path):
    global _db_path
    _db_path = Path(path)


def _resolve_path() -> Path:
    if _db_path:
        return _db_path
    from lib.config_loader import get_config
    try:
        cfg = get_config()
        return Path(cfg.paths.db)
    except Exception:
        return Path("data/galao.db")


@contextmanager
def get_db(path: Path = None):
    """Context manager — yields a sqlite3 connection with WAL mode and row_factory."""
    db_path = path or _resolve_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT    NOT NULL,
    line_price          REAL    NOT NULL,
    line_type           TEXT    NOT NULL,    -- SUPPORT | RESISTANCE
    line_strength       INTEGER NOT NULL,    -- 1=strong 2=medium 3=low
    direction           TEXT    NOT NULL,    -- BUY | SELL
    entry_type          TEXT    NOT NULL,    -- LMT | STP
    entry_price         REAL    NOT NULL,
    tp_price            REAL    NOT NULL,
    sl_price            REAL    NOT NULL,
    bracket_size        REAL    NOT NULL,
    quantity            INTEGER NOT NULL DEFAULT 1,
    status              TEXT    NOT NULL DEFAULT 'PENDING',
    -- PENDING | SUBMITTING | SUBMITTED | FILLED | EXITING | CLOSED
    -- CANCELLED | ERROR | RECONCILE_REQUIRED
    ib_order_id         INTEGER,
    ib_tp_order_id      INTEGER,
    ib_sl_order_id      INTEGER,
    claimed_at          TEXT,               -- ISO UTC timestamp, set when status→SUBMITTING
    replenishment_issued INTEGER NOT NULL DEFAULT 0,  -- 1 after Decider spawned replacement
    fill_price          REAL,
    fill_time           TEXT,               -- ISO UTC
    exit_price          REAL,
    exit_time           TEXT,               -- ISO UTC
    exit_reason         TEXT,               -- TP | SL | STAGNATION | SHUTDOWN | MANUAL
    pnl_points          REAL,
    error_message       TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id      INTEGER NOT NULL REFERENCES commands(id),
    symbol          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,       -- BUY | SELL
    quantity        INTEGER NOT NULL,
    entry_price     REAL    NOT NULL,
    entry_time      TEXT    NOT NULL,
    price_at_check  REAL,
    last_checked_at TEXT,
    status          TEXT    NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS ib_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,      -- ERROR | WARNING | INFO | RECONNECT | DISCONNECT
    component   TEXT NOT NULL,
    code        INTEGER,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS system_state (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS critical_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    date        TEXT    NOT NULL,           -- YYYY-MM-DD
    line_type   TEXT    NOT NULL,           -- SUPPORT | RESISTANCE
    price       REAL    NOT NULL,
    strength    INTEGER NOT NULL,           -- 1=strong 2=medium 3=low
    armed       INTEGER NOT NULL DEFAULT 1, -- 0 after SL cool-down disarms
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS release_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    program     TEXT NOT NULL,
    version     TEXT NOT NULL,
    summary     TEXT NOT NULL,
    details     TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_commands_status   ON commands(status);
CREATE INDEX IF NOT EXISTS idx_commands_symbol   ON commands(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_status  ON positions(status);
CREATE INDEX IF NOT EXISTS idx_critical_lines_sd ON critical_lines(symbol, date);
"""


def init_db(path: Path = None):
    """Create all tables and indexes if they don't exist."""
    with get_db(path) as con:
        con.executescript(_SCHEMA)


# ── CRUD helpers ──────────────────────────────────────────────────────────────

def get_system_state(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_system_state(con, key: str, value: str):
    con.execute(
        "INSERT INTO system_state(key, value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')",
        (key, value)
    )


def update_command_status(con, command_id: int, status: str, **kwargs):
    """Update command status plus any optional fields (ib_order_id, fill_price, etc.)."""
    fields = {"status": status, **kwargs}
    sets = ", ".join(f"{k}=?" for k in fields)
    sets += ", updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    values = list(fields.values()) + [command_id]
    con.execute(f"UPDATE commands SET {sets} WHERE id=?", values)


def get_pending_commands(con, symbol: str = None) -> list:
    if symbol:
        return con.execute(
            "SELECT * FROM commands WHERE status='PENDING' AND symbol=?", (symbol,)
        ).fetchall()
    return con.execute("SELECT * FROM commands WHERE status='PENDING'").fetchall()


def get_filled_commands(con, symbol: str = None) -> list:
    """Return FILLED commands where replenishment has not yet been issued."""
    if symbol:
        return con.execute(
            "SELECT * FROM commands WHERE status='FILLED'"
            " AND replenishment_issued=0 AND symbol=?", (symbol,)
        ).fetchall()
    return con.execute(
        "SELECT * FROM commands WHERE status='FILLED' AND replenishment_issued=0"
    ).fetchall()


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile, os
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            # 1. Init creates all tables
            init_db(db_path)
            with get_db(db_path) as con:
                tables = {r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
            expected = {"commands", "positions", "ib_events", "system_state",
                        "critical_lines", "release_notes"}
            assert expected <= tables, f"Missing tables: {expected - tables}"

            # 2. WAL mode is active
            with get_db(db_path) as con:
                mode = con.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal", f"Expected WAL, got {mode}"

            # 3. system_state round-trip
            with get_db(db_path) as con:
                set_system_state(con, "SESSION", "RUNNING")
                val = get_system_state(con, "SESSION")
            assert val == "RUNNING", f"system_state read: {val}"

            # 4. Upsert updates, not duplicates
            with get_db(db_path) as con:
                set_system_state(con, "SESSION", "SHUTDOWN")
                count = con.execute(
                    "SELECT COUNT(*) FROM system_state WHERE key='SESSION'"
                ).fetchone()[0]
                val2 = get_system_state(con, "SESSION")
            assert count == 1,          "Upsert created duplicate row"
            assert val2 == "SHUTDOWN",  f"Upsert didn't update value: {val2}"

            # 5. Insert a command and update its status
            with get_db(db_path) as con:
                con.execute("""
                    INSERT INTO commands
                        (symbol, line_price, line_type, line_strength,
                         direction, entry_type, entry_price, tp_price, sl_price, bracket_size)
                    VALUES ('MES', 6500.0, 'SUPPORT', 2,
                            'BUY', 'STP', 6500.25, 6502.25, 6498.25, 2.0)
                """)
                cmd_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]

            with get_db(db_path) as con:
                update_command_status(con, cmd_id, "SUBMITTING", claimed_at="2026-04-07T10:00:00Z")
                row = con.execute("SELECT status, claimed_at FROM commands WHERE id=?",
                                  (cmd_id,)).fetchone()
            assert row["status"] == "SUBMITTING",             f"Status: {row['status']}"
            assert row["claimed_at"] == "2026-04-07T10:00:00Z", f"claimed_at: {row['claimed_at']}"

            # 6. get_pending_commands (should be empty now)
            with get_db(db_path) as con:
                pending = get_pending_commands(con)
            assert len(pending) == 0, f"Expected 0 pending, got {len(pending)}"

            # 7. Rollback on error — no partial writes
            try:
                with get_db(db_path) as con:
                    con.execute("INSERT INTO system_state(key,value) VALUES('ROLLBACK_TEST','yes')")
                    raise RuntimeError("forced rollback")
            except RuntimeError:
                pass
            with get_db(db_path) as con:
                v = get_system_state(con, "ROLLBACK_TEST")
            assert v is None, "Rollback failed — value persisted"

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
    # Default: init production DB
    init_db()
    print("DB initialized.")
