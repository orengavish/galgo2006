"""
compare_fetch.py
Compare a newly fetched CSV against a V1 reference file.
Checks: row count, price range, first/last tick, volume, price distribution.

Usage:
    python compare_fetch.py --new data/history/MNQ_trades_20260224.csv
                            --ref "C:/path/to/V1/DATA2/MNQ_trades_20260224.csv"
"""

import csv
import argparse
import sys
from pathlib import Path
from datetime import datetime


def load(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def stats(rows: list[dict], label: str) -> dict:
    prices  = [float(r["price"]) for r in rows if r.get("price")]
    sizes   = [float(r["size"])  for r in rows if r.get("size")]
    times_ct = [r.get("time_ct", "") for r in rows]

    vol = sum(sizes)
    return {
        "label":      label,
        "rows":       len(rows),
        "price_min":  min(prices),
        "price_max":  max(prices),
        "price_range": max(prices) - min(prices),
        "vol_total":  vol,
        "vol_avg":    vol / len(sizes) if sizes else 0,
        "first_tick": times_ct[0]  if times_ct else "",
        "last_tick":  times_ct[-1] if times_ct else "",
        "symbols":    sorted({r.get("symbol","") for r in rows}),
    }


def pct_diff(a, b):
    if b == 0:
        return float("inf")
    return (a - b) / b * 100


def compare(new_path: Path, ref_path: Path, tolerance_pct: float = 2.0):
    print(f"\n{'='*62}")
    print(f"  FETCH COMPARISON")
    print(f"  NEW : {new_path.name}")
    print(f"  REF : {ref_path.name}")
    print(f"{'='*62}")

    new_rows = load(new_path)
    ref_rows = load(ref_path)

    n = stats(new_rows, "NEW")
    r = stats(ref_rows, "REF")

    issues = []

    def row(label, nv, rv, fmt="{}", unit="", check=True):
        nv_s = fmt.format(nv) if not isinstance(nv, str) else nv
        rv_s = fmt.format(rv) if not isinstance(rv, str) else rv
        diff = ""
        flag = ""
        if isinstance(nv, (int, float)) and isinstance(rv, (int, float)) and rv != 0:
            d = pct_diff(nv, rv)
            diff = f"  ({d:+.1f}%)"
            if check and abs(d) > tolerance_pct:
                flag = "  <-- DIFF"
                issues.append(f"{label}: new={nv_s} ref={rv_s} diff={d:+.1f}%")
        print(f"  {label:<22} NEW={nv_s+unit:<18} REF={rv_s+unit:<18}{diff}{flag}")

    row("Row count",   n["rows"],        r["rows"],        "{:,}")
    row("Price min",   n["price_min"],   r["price_min"],   "{:.2f}")
    row("Price max",   n["price_max"],   r["price_max"],   "{:.2f}")
    row("Price range", n["price_range"], r["price_range"], "{:.2f}", " pts")
    row("Total volume",n["vol_total"],   r["vol_total"],   "{:,.0f}", " contracts")
    row("Avg size",    n["vol_avg"],     r["vol_avg"],     "{:.2f}")
    print(f"  {'First tick':<22} NEW={n['first_tick']}")
    print(f"  {'':22} REF={r['first_tick']}")
    print(f"  {'Last tick':<22} NEW={n['last_tick']}")
    print(f"  {'':22} REF={r['last_tick']}")
    print(f"  {'Symbols':<22} NEW={n['symbols']}  REF={r['symbols']}")

    # Price bucket distribution (10 buckets)
    print(f"\n  Price distribution (10 buckets):")
    all_prices = [float(row["price"]) for row in new_rows + ref_rows]
    lo, hi = min(all_prices), max(all_prices)
    bsize = (hi - lo) / 10 if hi > lo else 1

    def bucket_counts(rows, lo, bsize):
        counts = [0] * 10
        for row in rows:
            p = float(row["price"])
            idx = min(int((p - lo) / bsize), 9)
            counts[idx] += 1
        return counts

    nb = bucket_counts(new_rows, lo, bsize)
    rb = bucket_counts(ref_rows, lo, bsize)
    total_n = sum(nb) or 1
    total_r = sum(rb) or 1

    for i in range(10):
        low_p  = lo + i * bsize
        high_p = lo + (i + 1) * bsize
        np_pct = nb[i] / total_n * 100
        rp_pct = rb[i] / total_r * 100
        bar_n  = "#" * int(np_pct / 2)
        bar_r  = "#" * int(rp_pct / 2)
        diff_flag = "  <-- DIFF" if abs(np_pct - rp_pct) > 5 else ""
        print(f"  {low_p:>8.1f}-{high_p:<8.1f}  "
              f"NEW {np_pct:5.1f}% {bar_n:<25}  "
              f"REF {rp_pct:5.1f}% {bar_r}{diff_flag}")

    print(f"\n{'='*62}")
    if issues:
        print(f"  ISSUES ({len(issues)}):")
        for iss in issues:
            print(f"    - {iss}")
        print(f"  RESULT: MISMATCH (tolerance={tolerance_pct}%)")
    else:
        print(f"  RESULT: OK (all diffs within {tolerance_pct}%)")
    print(f"{'='*62}\n")
    return len(issues) == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--new",       required=True,  help="New fetcher CSV path")
    parser.add_argument("--ref",       required=True,  help="V1 reference CSV path")
    parser.add_argument("--tolerance", type=float, default=2.0,
                        help="Acceptable % difference (default: 2.0)")
    args = parser.parse_args()

    new_path = Path(args.new)
    ref_path = Path(args.ref)

    if not new_path.exists():
        print(f"ERROR: new file not found: {new_path}")
        sys.exit(1)
    if not ref_path.exists():
        print(f"ERROR: ref file not found: {ref_path}")
        sys.exit(1)

    ok = compare(new_path, ref_path, args.tolerance)
    sys.exit(0 if ok else 1)
