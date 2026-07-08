"""
back-trading/bt_scorer.py
Cartesian backtrader scoring engine.

Computes 12 metrics per param_set from bt_matrix_results,
runs 4 anti-overfitting guards, writes bt_scores + bt_score_history.

Metrics (all configurable weights in config.yaml):
  1  win_rate         wins / n_exits
  2  profit_factor    sum(+pnl) / abs(sum(-pnl))
  3  expectancy       mean(pnl_ticks)
  4  sharpe           mean/std * sqrt(N)
  5  sortino          mean / downside_std * sqrt(N)
  6  calmar           total_pnl / max_drawdown
  7  max_drawdown_t   maximum cumulative trough (ticks)
  8  avg_win_loss     mean(wins) / abs(mean(losses))
  9  sqn              System Quality Number (Van Tharp)
  10 fill_rate        non-EXPIRED / total
  11 max_consec_loss  longest losing streak
  12 mc_pvalue        Monte Carlo permutation p-value (lower = better)

Anti-overfitting guards:
  A  Insufficient data  n_trades < MIN_TRADES_FOR_SCORE → status=insufficient_data
  B  Monte Carlo        mc_pvalue > 0.05 → status=low_confidence
  C  Stability Zone     mean(neighbor_scores) / own_score < 0.70 → stability_zone < 0.70
  D  LOOCV              loocv_score < 0.80 * in_sample composite → flag in status

Usage:
    python back-trading/bt_scorer.py --run          # score all param_sets
    python back-trading/bt_scorer.py --top 20       # print top 20
    python back-trading/bt_scorer.py --snapshot     # write today's history
    python back-trading/bt_scorer.py --self-test
"""

import sys
import math
import sqlite3
import random
import argparse
from datetime import date, datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── Thresholds ────────────────────────────────────────────────────────────────

MIN_TRADES_FOR_SCORE = 5       # absolute floor; below this = insufficient_data
MIN_TRADES_FOR_FULL  = 20      # below this: metrics computed but flagged
MC_PERMUTATIONS      = 1000
MC_PVALUE_THRESHOLD  = 0.05    # > 0.05 = low_confidence
STABILITY_THRESHOLD  = 0.70    # < 0.70 = spiky optimum
LOOCV_THRESHOLD      = 0.80    # loocv_score / in_sample must be >= 0.80

# Default weights (sum = 1.0); override in config.yaml
DEFAULT_WEIGHTS = {
    "win_rate":      0.08,
    "profit_factor": 0.15,
    "expectancy":    0.12,
    "sharpe":        0.12,
    "sortino":       0.10,
    "calmar":        0.08,
    "max_drawdown_t": 0.10,   # inverted: lower drawdown = higher score
    "avg_win_loss":  0.08,
    "sqn":           0.07,
    "fill_rate":     0.05,
    "max_consec_loss": 0.03,  # inverted
    "mc_pvalue":     0.02,    # inverted: lower p = better
}


def _load_weights() -> dict:
    """Load score weights from config.yaml if present, else use defaults."""
    try:
        cfg_path = _ROOT / "trader" / "config.yaml"
        if not cfg_path.exists():
            return DEFAULT_WEIGHTS
        import importlib.util
        spec = importlib.util.spec_from_file_location("config_loader",
                                                       _ROOT / "lib" / "config_loader.py")
        cl = importlib.util.module_from_spec(spec); spec.loader.exec_module(cl)
        cfg = cl.get_config(cfg_path)
        w = getattr(getattr(cfg, "backtest", None), "score_weights", None)
        if w and isinstance(w, dict):
            return w
    except Exception:
        pass
    return DEFAULT_WEIGHTS


# ── Metric helpers ─────────────────────────────────────────────────────────────

def _mean(vals): return sum(vals) / len(vals)
def _std(vals, mean=None):
    if len(vals) < 2: return 0.0
    m = mean if mean is not None else _mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))

def _max_drawdown(pnl_seq):
    peak = cur = 0.0
    dd = 0.0
    for p in pnl_seq:
        cur += p
        if cur > peak:
            peak = cur
        dd = max(dd, peak - cur)
    return dd

def _max_consec_loss(pnl_seq):
    best = cur = 0
    for p in pnl_seq:
        cur = cur + 1 if p <= 0 else 0
        best = max(best, cur)
    return best


def compute_metrics(pnl_list: list[float], total_trades: int) -> dict:
    """
    Compute all 12 metrics for a list of non-EXPIRED pnl values.
    total_trades includes EXPIRED (used for fill_rate).
    """
    n = len(pnl_list)
    fill_rate = n / total_trades if total_trades > 0 else 0.0

    if n == 0:
        return {"status": "insufficient_data", "n_trades": 0, "fill_rate": fill_rate}

    wins   = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p <= 0]

    mean_pnl = _mean(pnl_list)
    std_pnl  = _std(pnl_list, mean_pnl)
    sqrt_n   = math.sqrt(n)

    # Profit factor
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    # Sharpe
    sharpe = (mean_pnl / std_pnl * sqrt_n) if std_pnl > 0 else 0.0

    # Sortino (downside std only)
    loss_std = _std(losses) if losses else 0.0
    sortino = (mean_pnl / loss_std * sqrt_n) if loss_std > 0 else (sharpe if mean_pnl > 0 else 0.0)

    # Max drawdown
    mdd = _max_drawdown(pnl_list)

    # Calmar
    calmar = (sum(pnl_list) / mdd) if mdd > 0 else (float("inf") if sum(pnl_list) > 0 else 0.0)

    # SQN
    sqn = sharpe  # same formula: mean/std * sqrt(N)

    # Avg win / avg loss
    avg_wl = ((_mean(wins) / abs(_mean(losses))) if wins and losses else
              (float("inf") if wins else 0.0))

    # Max consec loss
    mcl = _max_consec_loss(pnl_list)

    # Monte Carlo p-value (computed separately — expensive)
    mc_pvalue = None

    status = "ok" if n >= MIN_TRADES_FOR_FULL else "insufficient_data"

    return {
        "n_trades":       n,
        "fill_rate":      round(fill_rate, 4),
        "win_rate":       round(len(wins) / n, 4),
        "profit_factor":  round(min(pf, 99.0), 4),
        "expectancy":     round(mean_pnl, 4),
        "sharpe":         round(sharpe, 4),
        "sortino":        round(sortino, 4),
        "calmar":         round(min(calmar, 99.0), 4),
        "max_drawdown_t": round(mdd, 4),
        "avg_win_loss":   round(min(avg_wl, 99.0), 4),
        "sqn":            round(sqn, 4),
        "max_consec_loss": mcl,
        "mc_pvalue":      mc_pvalue,
        "status":         status,
    }


def monte_carlo_pvalue(pnl_list: list[float], n_perm: int = MC_PERMUTATIONS,
                       seed: int | None = None) -> float:
    """
    Shuffle pnl order N times, compute Sharpe each time.
    p-value = fraction of shuffles with Sharpe >= real Sharpe.
    Lower p = more likely to be a real edge.
    """
    if len(pnl_list) < 10:
        return 1.0  # not enough data
    rng = random.Random(seed)
    mean_p = _mean(pnl_list)
    std_p  = _std(pnl_list, mean_p)
    if std_p == 0:
        return 0.0 if mean_p > 0 else 1.0
    real_sharpe = mean_p / std_p * math.sqrt(len(pnl_list))
    count_gte = 0
    pool = list(pnl_list)
    for _ in range(n_perm):
        rng.shuffle(pool)
        m = _mean(pool)
        s = _std(pool, m)
        sh = m / s * math.sqrt(len(pool)) if s > 0 else 0.0
        if sh >= real_sharpe:
            count_gte += 1
    return round(count_gte / n_perm, 4)


def loocv_score(pnl_list: list[float]) -> float:
    """
    Leave-One-Out Cross-Validation: fit on N-1 trades, measure on held-out.
    Returns ratio of mean LOOCV expectancy to full-sample expectancy.
    """
    n = len(pnl_list)
    if n < 5:
        return 0.0
    full_mean = _mean(pnl_list)
    if full_mean == 0:
        return 1.0
    loo_means = []
    for i in range(n):
        train = pnl_list[:i] + pnl_list[i+1:]
        loo_means.append(_mean(train))
    loo_avg = _mean(loo_means)
    return round(loo_avg / full_mean, 4) if full_mean != 0 else 1.0


# ── Normalization + composite ──────────────────────────────────────────────────

def _normalize_scores(all_metrics: dict[int, dict], weights: dict) -> dict[int, float]:
    """
    Min-max normalize each metric across all param_sets, then compute composite.
    Inverted metrics (max_drawdown_t, max_consec_loss, mc_pvalue) use (max - val).
    Returns {param_set_id: composite_score}.
    """
    INVERTED = {"max_drawdown_t", "max_consec_loss", "mc_pvalue"}

    # Collect values per metric (only 'ok' rows with that metric)
    metric_vals: dict[str, list[float]] = {m: [] for m in weights}
    for m_dict in all_metrics.values():
        if m_dict.get("status") == "ok":
            for metric in weights:
                v = m_dict.get(metric)
                if v is not None and math.isfinite(v):
                    metric_vals[metric].append(v)

    # Min/max per metric
    ranges = {}
    for metric, vals in metric_vals.items():
        if not vals:
            ranges[metric] = (0.0, 1.0)
        else:
            mn, mx = min(vals), max(vals)
            ranges[metric] = (mn, mx if mx > mn else mn + 1e-9)

    composites = {}
    for ps_id, m_dict in all_metrics.items():
        if m_dict.get("status") != "ok":
            composites[ps_id] = 0.0
            continue
        score = 0.0
        for metric, weight in weights.items():
            v = m_dict.get(metric)
            if v is None or not math.isfinite(v):
                continue
            mn, mx = ranges[metric]
            norm = (v - mn) / (mx - mn)
            if metric in INVERTED:
                norm = 1.0 - norm
            score += norm * weight
        composites[ps_id] = round(score, 6)

    return composites


# ── Main scoring run ──────────────────────────────────────────────────────────

def run_scoring(bt_db_path: Path, run_mc: bool = True,
                run_loocv: bool = True, n_mc_perm: int = MC_PERMUTATIONS,
                mc_seed: int | None = None) -> dict:
    """
    Score all param_sets that have results in bt_matrix_results.
    Returns summary dict.
    """
    weights = _load_weights()

    bt_db_mod = _load_bt_db()
    conn = bt_db_mod.init_bt_db(bt_db_path)

    # Load all results grouped by param_set_id
    rows = conn.execute(
        "SELECT param_set_id, trade_id, exit_reason, pnl_ticks "
        "FROM bt_matrix_results WHERE exit_reason != 'NO_DATA'"
    ).fetchall()

    # Group: {ps_id: {trade_ids: set, pnl_list: [], total_count}}
    from collections import defaultdict
    groups = defaultdict(lambda: {"pnl": [], "total": 0})
    for r in rows:
        ps_id = r[0]
        exit_r = r[2]
        pnl    = r[3]
        groups[ps_id]["total"] += 1
        if exit_r not in ("EXPIRED", "ERROR", "NO_DATA") and pnl is not None:
            groups[ps_id]["pnl"].append(float(pnl))

    if not groups:
        conn.close()
        return {"scored": 0, "ok": 0, "insufficient": 0}

    # Compute metrics for all param_sets
    all_metrics: dict[int, dict] = {}
    for ps_id, g in groups.items():
        pnl = g["pnl"]
        total = g["total"]
        m = compute_metrics(pnl, total)

        # Monte Carlo (only for 'ok' status and enough trades)
        if run_mc and m["status"] == "ok" and len(pnl) >= 10:
            m["mc_pvalue"] = monte_carlo_pvalue(pnl, n_mc_perm, seed=mc_seed)
            if m["mc_pvalue"] > MC_PVALUE_THRESHOLD:
                m["status"] = "low_confidence"

        # LOOCV
        if run_loocv and m["status"] == "ok" and len(pnl) >= 5:
            m["loocv_score"] = loocv_score(pnl)
        else:
            m["loocv_score"] = None

        all_metrics[ps_id] = m

    # Normalize + composite
    composites = _normalize_scores(all_metrics, weights)

    # Stability zone (requires all composites computed first)
    bt_params_mod = _load_bt_params()
    for ps_id, m in all_metrics.items():
        if m.get("status") == "ok":
            neighbors = bt_params_mod.get_neighbors(conn, ps_id)
            nb_scores = [composites.get(nb, 0.0) for nb in neighbors]
            own = composites[ps_id]
            if nb_scores and own > 0:
                m["stability_zone"] = round(_mean(nb_scores) / own, 4)
                if m["stability_zone"] < STABILITY_THRESHOLD:
                    m["status"] = "unstable"
            else:
                m["stability_zone"] = None
        else:
            m["stability_zone"] = None

    # Write to bt_scores
    now_iso = datetime.now(timezone.utc).isoformat()
    for ps_id, m in all_metrics.items():
        conn.execute(
            "INSERT OR REPLACE INTO bt_scores "
            "(param_set_id, n_trades, win_rate, profit_factor, expectancy, "
            "sharpe, sortino, calmar, max_drawdown_t, avg_win_loss, sqn, "
            "fill_rate, max_consec_loss, mc_pvalue, composite_score, "
            "loocv_score, stability_zone, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ps_id,
             m.get("n_trades", 0),
             m.get("win_rate"), m.get("profit_factor"), m.get("expectancy"),
             m.get("sharpe"), m.get("sortino"), m.get("calmar"),
             m.get("max_drawdown_t"), m.get("avg_win_loss"), m.get("sqn"),
             m.get("fill_rate"), m.get("max_consec_loss"), m.get("mc_pvalue"),
             composites.get(ps_id, 0.0),
             m.get("loocv_score"), m.get("stability_zone"),
             m.get("status", "ok"),
             now_iso)
        )
    conn.commit()

    ok_count = sum(1 for m in all_metrics.values() if m.get("status") == "ok")
    insuf    = sum(1 for m in all_metrics.values() if m.get("status") == "insufficient_data")
    return {"scored": len(all_metrics), "ok": ok_count, "insufficient": insuf}


def write_snapshot(bt_db_path: Path, snapshot_date: str | None = None):
    """Write today's top param_set scores to bt_score_history."""
    bt_db_mod = _load_bt_db()
    conn = bt_db_mod.init_bt_db(bt_db_path)
    d = snapshot_date or date.today().isoformat()
    rows = conn.execute(
        "SELECT param_set_id, composite_score, n_trades, win_rate, expectancy, sqn, "
        "ROW_NUMBER() OVER (ORDER BY composite_score DESC) AS rnk "
        "FROM bt_scores WHERE status='ok' ORDER BY composite_score DESC LIMIT 1000"
    ).fetchall()
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO bt_score_history "
        "(snapshot_date, param_set_id, rank, composite_score, n_trades, "
        "win_rate, expectancy, sqn, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [(d, r[0], r[6], r[1], r[2], r[3], r[4], r[5], now_iso) for r in rows]
    )
    conn.commit()
    conn.close()
    return len(rows)


def print_top(bt_db_path: Path, n: int = 20):
    """Print top N param_sets ranked by composite_score."""
    bt_db_mod = _load_bt_db()
    conn = bt_db_mod.init_bt_db(bt_db_path)
    rows = conn.execute("""
        SELECT s.composite_score, s.win_rate, s.expectancy, s.sqn,
               s.profit_factor, s.n_trades, s.status,
               p.tp_ticks, p.sl_ticks, p.entry_delay_s,
               p.entry_offset_t, p.tp_confirm_t, p.session_window
        FROM bt_scores s JOIN bt_param_sets p ON p.id = s.param_set_id
        WHERE s.status IN ('ok','low_confidence')
        ORDER BY s.composite_score DESC LIMIT ?
    """, (n,)).fetchall()
    conn.close()
    if not rows:
        print("No scored param_sets yet.")
        return
    print(f"{'Rank':>4}  {'Score':>6}  {'WinR%':>5}  {'EV':>5}  {'SQN':>5}  "
          f"{'PF':>5}  {'N':>4}  {'TP':>2}  {'SL':>2}  "
          f"{'Dly':>3}  {'Off':>3}  {'Cnf':>3}  Window")
    print("-" * 85)
    for i, r in enumerate(rows, 1):
        print(f"{i:>4}  {r[0]:>6.4f}  {(r[1] or 0)*100:>4.1f}%  "
              f"{r[2] or 0:>5.2f}  {r[3] or 0:>5.2f}  {r[4] or 0:>5.2f}  "
              f"{r[5]:>4}  {r[7]:>2}  {r[8]:>2}  "
              f"{r[9]:>3}  {r[10]:>3}  {r[11]:>3}  {r[12]}")


def _load_bt_db():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bt_db", Path(__file__).parent / "bt_db.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_bt_params():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bt_params", Path(__file__).parent / "bt_params.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Self-test ──────────────────────────────────────────────────────────────────

def _self_test() -> bool:
    import os, tempfile, shutil

    print("Running bt_scorer self-test...")
    bt_db_mod  = _load_bt_db()
    bt_params_mod = _load_bt_params()

    tmp_bt = tempfile.mktemp(suffix="_bt.db")
    try:
        conn = bt_db_mod.init_bt_db(Path(tmp_bt))

        # Seed param_sets
        bt_params_mod.seed_param_sets(conn)

        # Insert synthetic bt_matrix_results for 3 param_sets
        # PS 1 (id=1): 25 trades, all TP (+4 ticks each) → should score 'ok'
        # PS 2 (id=2): 25 trades, mixed +4/-4 → moderate score
        # PS 3 (id=3): 3 trades only → insufficient_data
        import random as _rand
        _rand.seed(42)

        ps1_id = conn.execute(
            "SELECT id FROM bt_param_sets WHERE tp_ticks=4 AND sl_ticks=4 "
            "AND entry_delay_s=0 AND entry_offset_t=0 AND tp_confirm_t=2 AND session_window='ALL'"
        ).fetchone()[0]
        ps2_id = conn.execute(
            "SELECT id FROM bt_param_sets WHERE tp_ticks=6 AND sl_ticks=6 "
            "AND entry_delay_s=0 AND entry_offset_t=0 AND tp_confirm_t=2 AND session_window='ALL'"
        ).fetchone()[0]
        ps3_id = conn.execute(
            "SELECT id FROM bt_param_sets WHERE tp_ticks=8 AND sl_ticks=8 "
            "AND entry_delay_s=0 AND entry_offset_t=0 AND tp_confirm_t=2 AND session_window='ALL'"
        ).fetchone()[0]

        rows = []
        for trade_id in range(1, 26):
            rows.append((trade_id, ps1_id, "MES", "2026-06-30", "BUY", "TP",  4.0, 10, 50))
        for trade_id in range(1, 26):
            pnl = 6.0 if trade_id % 2 == 0 else -4.0
            reason = "TP" if pnl > 0 else "SL"
            rows.append((trade_id, ps2_id, "MES", "2026-06-30", "BUY", reason, pnl, 8, 60))
        for trade_id in range(1, 4):
            rows.append((trade_id, ps3_id, "MES", "2026-06-30", "BUY", "TP", 4.0, 5, 40))

        conn.executemany(
            "INSERT OR IGNORE INTO bt_matrix_results "
            "(trade_id, param_set_id, symbol, trade_date, direction, exit_reason, "
            "pnl_ticks, ticks_to_exit, ms_to_exit) VALUES (?,?,?,?,?,?,?,?,?)",
            rows
        )
        conn.commit()
        conn.close()

        # Run scorer (use fixed MC seed for reproducibility, fewer permutations)
        summary = run_scoring(
            bt_db_path=Path(tmp_bt),
            run_mc=True, run_loocv=True,
            n_mc_perm=200, mc_seed=99
        )

        assert summary["scored"] >= 3, f"Expected >=3 scored, got {summary}"

        # Verify results
        conn2 = sqlite3.connect(tmp_bt)
        conn2.row_factory = sqlite3.Row

        ps1_row = conn2.execute("SELECT * FROM bt_scores WHERE param_set_id=?", (ps1_id,)).fetchone()
        ps3_row = conn2.execute("SELECT * FROM bt_scores WHERE param_set_id=?", (ps3_id,)).fetchone()

        assert ps1_row is not None, "PS1 not in bt_scores"
        assert ps1_row["win_rate"] == 1.0, f"PS1 win_rate should be 1.0, got {ps1_row['win_rate']}"
        assert ps1_row["status"] in ("ok", "low_confidence"), \
            f"PS1 status unexpected: {ps1_row['status']}"

        assert ps3_row is not None, "PS3 not in bt_scores"
        assert ps3_row["status"] == "insufficient_data", \
            f"PS3 with 3 trades should be insufficient_data, got {ps3_row['status']}"

        # PS1 should rank above PS2 (all wins vs mixed)
        ps1_score = ps1_row["composite_score"]
        ps2_row   = conn2.execute("SELECT * FROM bt_scores WHERE param_set_id=?", (ps2_id,)).fetchone()
        ps2_score = ps2_row["composite_score"] if ps2_row else 0.0
        assert ps1_score >= ps2_score, \
            f"PS1 (all wins) score {ps1_score} should >= PS2 mixed {ps2_score}"

        # Snapshot
        write_snapshot(Path(tmp_bt), "2026-07-01")
        snap_count = conn2.execute(
            "SELECT COUNT(*) FROM bt_score_history WHERE snapshot_date='2026-07-01'"
        ).fetchone()[0]
        assert snap_count > 0, "No snapshot rows written"

        conn2.close()

        print(f"PASS -- scored {summary['scored']} param_sets "
              f"({summary['ok']} ok, {summary['insufficient']} insufficient), "
              f"snapshot={snap_count} rows")
        return True

    except Exception as e:
        print(f"FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False
    finally:
        try: os.unlink(tmp_bt)
        except Exception: pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtrader scoring engine")
    parser.add_argument("--run",      action="store_true", help="Score all param_sets")
    parser.add_argument("--top",      type=int, default=0, metavar="N")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--no-mc",    action="store_true", help="Skip Monte Carlo (faster)")
    args = parser.parse_args()

    bt_db_path = _ROOT / "trader" / "data" / "bt.db"

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    if args.run:
        summary = run_scoring(bt_db_path, run_mc=not args.no_mc)
        print(f"Scored: {summary}")

    if args.top > 0:
        print_top(bt_db_path, args.top)

    if args.snapshot:
        n = write_snapshot(bt_db_path)
        print(f"Snapshot written: {n} rows for {date.today()}")
