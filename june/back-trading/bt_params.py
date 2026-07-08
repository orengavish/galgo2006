"""
back-trading/bt_params.py
Cartesian product parameter set generator for the backtrader.

Defines all axes of the parameter space, generates all combinations,
seeds them into bt_param_sets, and provides neighbor lookup for the
Stability Zone anti-overfitting test.

Usage:
    from bt_params import seed_param_sets, get_active_param_sets, get_neighbors
    python back-trading/bt_params.py --self-test
    python back-trading/bt_params.py --seed         # seed DB (safe to re-run)
    python back-trading/bt_params.py --count        # print combination count
"""

import itertools
import sys
import argparse
from pathlib import Path
from datetime import time as dtime

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── Cartesian axes (Phase 1: 10,800 combinations) ────────────────────────────

AXES = {
    "tp_ticks":       [2, 4, 6, 8, 10, 12],
    "sl_ticks":       [2, 4, 6, 8, 10, 12],
    "entry_delay_s":  [0, 5, 15, 30, 60],
    "entry_offset_t": [-2, -1, 0, 1, 2],
    "tp_confirm_t":   [1, 2, 3],
    "session_window": ["ALL", "MORNING", "MIDDAY", "AFTERNOON"],
}

AXIS_KEYS = list(AXES.keys())

# Session window boundaries in CT (hour, minute)
SESSION_WINDOWS = {
    "ALL":        (dtime(8, 30),  dtime(15, 15)),
    "MORNING":    (dtime(8, 30),  dtime(11,  0)),
    "MIDDAY":     (dtime(11,  0), dtime(13, 30)),
    "AFTERNOON":  (dtime(13, 30), dtime(15, 15)),
    # PRE_MARKET: not yet in AXES — 69% of verified trades fall here (00:00–08:29 CT).
    # Add "PRE_MARKET" to AXES["session_window"] when ready to expand the matrix.
    "PRE_MARKET": (dtime(0,  0),  dtime(8, 30)),
}


def generate_param_sets() -> list[dict]:
    """Return list of all Cartesian product parameter dicts."""
    value_lists = [AXES[k] for k in AXIS_KEYS]
    return [
        dict(zip(AXIS_KEYS, combo))
        for combo in itertools.product(*value_lists)
    ]


def seed_param_sets(conn) -> int:
    """
    Insert all combinations into bt_param_sets (INSERT OR IGNORE — safe to re-run).
    Returns number of newly inserted rows.
    """
    before = conn.execute("SELECT COUNT(*) FROM bt_param_sets").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO bt_param_sets "
        "(tp_ticks, sl_ticks, entry_delay_s, entry_offset_t, tp_confirm_t, session_window) "
        "VALUES (:tp_ticks, :sl_ticks, :entry_delay_s, :entry_offset_t, :tp_confirm_t, :session_window)",
        generate_param_sets(),
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM bt_param_sets").fetchone()[0]
    return after - before


def get_active_param_sets(conn) -> list:
    """Return all rows from bt_param_sets as list of sqlite3.Row objects."""
    return conn.execute(
        "SELECT * FROM bt_param_sets ORDER BY id"
    ).fetchall()


def get_neighbors(conn, param_set_id: int) -> list[int]:
    """
    Return IDs of the nearest neighbors (±1 step on each axis independently).
    Used by the Stability Zone anti-overfitting test.
    Each axis contributes at most 2 neighbors (one step up, one step down).
    """
    row = conn.execute(
        "SELECT tp_ticks, sl_ticks, entry_delay_s, entry_offset_t, "
        "tp_confirm_t, session_window FROM bt_param_sets WHERE id=?",
        (param_set_id,)
    ).fetchone()
    if row is None:
        return []

    current = dict(zip(
        ["tp_ticks", "sl_ticks", "entry_delay_s", "entry_offset_t",
         "tp_confirm_t", "session_window"],
        tuple(row)
    ))

    neighbor_ids = set()
    for axis in AXIS_KEYS:
        values = AXES[axis]
        cur_val = current[axis]
        try:
            idx = values.index(cur_val)
        except ValueError:
            continue
        for delta in (-1, +1):
            ni = idx + delta
            if 0 <= ni < len(values):
                candidate = dict(current)
                candidate[axis] = values[ni]
                hit = conn.execute(
                    "SELECT id FROM bt_param_sets "
                    "WHERE tp_ticks=? AND sl_ticks=? AND entry_delay_s=? "
                    "AND entry_offset_t=? AND tp_confirm_t=? AND session_window=?",
                    (candidate["tp_ticks"], candidate["sl_ticks"],
                     candidate["entry_delay_s"], candidate["entry_offset_t"],
                     candidate["tp_confirm_t"], candidate["session_window"])
                ).fetchone()
                if hit:
                    neighbor_ids.add(hit[0])

    neighbor_ids.discard(param_set_id)
    return sorted(neighbor_ids)


def total_combinations() -> int:
    n = 1
    for v in AXES.values():
        n *= len(v)
    return n


# ── Self-test ─────────────────────────────────────────────────────────────────

def _self_test():
    import sqlite3, tempfile, os

    expected = total_combinations()
    assert expected == 10800, f"Expected 10800 combinations, got {expected}"

    # Use temp DB
    tmp = tempfile.mktemp(suffix=".db")
    try:
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row
        # Create table
        conn.execute("""
            CREATE TABLE bt_param_sets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tp_ticks INTEGER NOT NULL, sl_ticks INTEGER NOT NULL,
                entry_delay_s INTEGER NOT NULL, entry_offset_t INTEGER NOT NULL,
                tp_confirm_t INTEGER NOT NULL, session_window TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                UNIQUE(tp_ticks,sl_ticks,entry_delay_s,entry_offset_t,tp_confirm_t,session_window)
            )
        """)
        conn.commit()

        # Seed
        inserted = seed_param_sets(conn)
        assert inserted == expected, f"Expected {expected} inserts, got {inserted}"

        # Re-seed is idempotent
        inserted2 = seed_param_sets(conn)
        assert inserted2 == 0, f"Re-seed should insert 0, got {inserted2}"

        # Count in DB
        count = conn.execute("SELECT COUNT(*) FROM bt_param_sets").fetchone()[0]
        assert count == expected, f"DB count {count} != {expected}"

        # get_active_param_sets
        rows = get_active_param_sets(conn)
        assert len(rows) == expected

        # Neighbor test: find the row with tp=4, sl=4, delay=5, offset=0, confirm=2, window=ALL
        center = conn.execute(
            "SELECT id FROM bt_param_sets WHERE tp_ticks=4 AND sl_ticks=4 "
            "AND entry_delay_s=5 AND entry_offset_t=0 AND tp_confirm_t=2 AND session_window='ALL'"
        ).fetchone()
        assert center is not None, "Center param set not found"
        neighbors = get_neighbors(conn, center[0])
        # Each axis: 2 neighbors each; center is not on any boundary for tp=4,sl=4,delay=5,offset=0,confirm=2,window=ALL
        # tp: [2,4,6,8,10,12] → idx=1, neighbors: 2 and 6 ✓
        # sl: [2,4,6,8,10,12] → idx=1, neighbors: 2 and 6 ✓
        # delay: [0,5,15,30,60] → idx=1, neighbors: 0 and 15 ✓
        # offset: [-2,-1,0,1,2] → idx=2, neighbors: -1 and 1 ✓
        # confirm: [1,2,3] → idx=1, neighbors: 1 and 3 ✓
        # window: [ALL,MORNING,MIDDAY,AFTERNOON] → idx=0, neighbors: MORNING only (boundary)
        # Total: 2+2+2+2+2+1 = 11 neighbors
        assert len(neighbors) == 11, f"Expected 11 neighbors, got {len(neighbors)}: {neighbors}"
        assert center[0] not in neighbors, "Center ID should not be in its own neighbors"

        conn.close()
        print(f"PASS -- {expected} param sets generated and seeded, neighbor test OK")
        return True
    except Exception as e:
        print(f"FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cartesian parameter set manager")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--seed",  action="store_true", help="Seed bt.db with all param sets")
    parser.add_argument("--count", action="store_true", help="Print total combinations")
    args = parser.parse_args()

    if args.count:
        print(f"Total combinations: {total_combinations():,}")

    elif args.seed:
        import importlib.util
        spec = importlib.util.spec_from_file_location("bt_db", Path(__file__).parent / "bt_db.py")
        bt_db = importlib.util.module_from_spec(spec); spec.loader.exec_module(bt_db)
        db_path = _ROOT / "trader" / "data" / "bt.db"
        conn = bt_db.init_bt_db(db_path)
        n = seed_param_sets(conn)
        total = conn.execute("SELECT COUNT(*) FROM bt_param_sets").fetchone()[0]
        conn.close()
        print(f"Inserted {n:,} new rows. Total in DB: {total:,}")

    elif args.self_test:
        sys.exit(0 if _self_test() else 1)

    else:
        parser.print_help()
