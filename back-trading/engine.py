"""
back-trading/engine.py
Backtest engine for Galao.

Runs the full trading strategy against historical IB data.
Uses the same rules as trader/ (lib/order_builder, lib/critical_lines).
Simulates order fills from OHLC bars via sim_broker.py.

Usage:
    python engine.py --date 2026-04-09
    python engine.py --from 2026-04-01 --to 2026-04-09
    python engine.py --self-test

Dashboard: http://127.0.0.1:5001  (after engine.py run, open visualizer)
"""

import sys
import argparse
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.config_loader import get_config
from lib.logger import get_logger

log = get_logger("engine")


def run(date_from: str, date_to: str):
    raise NotImplementedError("back-trading engine — coming soon")


def self_test() -> bool:
    try:
        cfg = get_config()
        assert hasattr(cfg, "backtest"), "Config missing backtest section"
        print("[self-test] engine: PASS")
        return True
    except Exception as e:
        print(f"[self-test] engine: FAIL — {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao back-trading engine")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--date",  help="Single date YYYY-MM-DD")
    parser.add_argument("--from",  dest="date_from", help="Start date YYYY-MM-DD")
    parser.add_argument("--to",    dest="date_to",   help="End date YYYY-MM-DD")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    date_from = args.date_from or args.date or "2026-04-09"
    date_to   = args.date_to   or args.date or date_from
    run(date_from, date_to)
