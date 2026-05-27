"""
back-trading/grader.py
Compares simulated fills with IB paper fills → accuracy grade.

Grade (per bracket size):
  % of fully-filled trades where |sim_exit_price - paper_exit_price| <= 1 tick

Archived per run in the grades table.

Self-test:
  python back-trading/grader.py --self-test
"""

import sys
import argparse
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TICK = 0.25


def grade(sim_results: list[dict], paper_results: list[dict]) -> dict:
    """
    Compare sim_results to paper_results (indexed by order position).

    Both lists must be the same length.
    A trade is "graded" only if BOTH sim and paper show TP or SL exit.
    EXPIRED orders on either side are excluded from grading.

    Returns dict keyed by bracket_size:
      { bracket_size: {
          total_trades, matched_1tick, matched_2tick,
          grade_pct, sim_pnl, paper_pnl, pnl_diff
        } }
    """
    buckets: dict[float, dict] = {}

    for i, sim in enumerate(sim_results):
        paper = paper_results[i] if i < len(paper_results) else {}
        bs    = sim.get("bracket_size", 0.0)

        if bs not in buckets:
            buckets[bs] = {
                "total":    0,
                "match_1":  0,
                "match_2":  0,
                "sim_pnl":  0.0,
                "paper_pnl": 0.0,
            }
        b = buckets[bs]

        sim_exit   = sim.get("exit_type")
        paper_exit = paper.get("exit_type")

        # Only grade trades that fully completed on both sides
        if sim_exit not in ("TP", "SL") or paper_exit not in ("TP", "SL"):
            continue

        b["total"] += 1

        sp = sim.get("exit_fill_price")
        pp = paper.get("exit_fill_price")

        if sp is not None and pp is not None:
            diff_ticks = abs(sp - pp) / _TICK
            if diff_ticks <= 1:
                b["match_1"] += 1
            if diff_ticks <= 2:
                b["match_2"] += 1

        if sim.get("pnl") is not None:
            b["sim_pnl"] += sim["pnl"]
        if paper.get("pnl") is not None:
            b["paper_pnl"] += paper["pnl"]

    result = {}
    for bs, b in buckets.items():
        total     = b["total"]
        grade_pct = round(b["match_1"] / total * 100, 1) if total > 0 else 0.0
        result[bs] = {
            "bracket_size":  bs,
            "total_trades":  total,
            "matched_1tick": b["match_1"],
            "matched_2tick": b["match_2"],
            "grade_pct":     grade_pct,
            "sim_pnl":       round(b["sim_pnl"],   2),
            "paper_pnl":     round(b["paper_pnl"], 2),
            "pnl_diff":      round(b["paper_pnl"] - b["sim_pnl"], 2),
        }
    return result


# ── Self-test ──────────────────────────────────────────────────────────────────

def self_test() -> bool:
    try:
        sim = [
            {"bracket_size": 2.0, "exit_type": "TP",  "exit_fill_price": 6502.00, "pnl": 10.0},
            {"bracket_size": 2.0, "exit_type": "SL",  "exit_fill_price": 6497.75, "pnl": -6.25},
            {"bracket_size": 2.0, "exit_type": "EXPIRED", "exit_fill_price": None, "pnl": None},
            {"bracket_size": 16.0,"exit_type": "TP",  "exit_fill_price": 6516.00, "pnl": 80.0},
        ]
        paper = [
            {"bracket_size": 2.0, "exit_type": "TP",  "exit_fill_price": 6502.00, "pnl": 10.0},  # exact match
            {"bracket_size": 2.0, "exit_type": "SL",  "exit_fill_price": 6498.00, "pnl": -5.0},  # 1-tick diff
            {"bracket_size": 2.0, "exit_type": "TP",  "exit_fill_price": 6502.00, "pnl": 10.0},  # sim EXPIRED → skip
            {"bracket_size": 16.0,"exit_type": "TP",  "exit_fill_price": 6515.50, "pnl": 77.5},  # 2-tick diff
        ]

        g = grade(sim, paper)

        # Bracket 2: 2 graded trades, both within 1 tick → 100%
        assert g[2.0]["total_trades"]  == 2, f"total {g[2.0]}"
        assert g[2.0]["matched_1tick"] == 2, f"match1 {g[2.0]}"
        assert g[2.0]["grade_pct"]     == 100.0

        # Bracket 16: 1 graded trade, 2-tick diff → 0% within 1 tick
        assert g[16.0]["total_trades"]  == 1
        assert g[16.0]["matched_1tick"] == 0
        assert g[16.0]["grade_pct"]     == 0.0
        assert g[16.0]["matched_2tick"] == 1   # within 2 ticks

        print("[self-test] grader: PASS")
        return True

    except Exception as e:
        print(f"[self-test] grader: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    print("grader.py — run --self-test to verify")
