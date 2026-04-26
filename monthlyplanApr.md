# Monthly Technical Plan — Late April → End of May 2026

## Long-Term Vision (Yearly)

### Generator
- Generate critical lines for 5+ futures (MES, ES, NQ, …)
- Multiple algos: random brackets, manual critical lines, ML-derived lines
- Full self-improving mechanism: algo updates its own parameters based on graded results

### Trader
- Send trades according to generated critical lines
- Replenish filled brackets automatically
- Trace all trades at all times
- Persist everything to DB

### Back-Trader
- Mirror the live paper session in simulation
- Grade sim accuracy vs real paper fills
- Drive continuous improvement of both the sim algo and the order generation strategy

---

## Current State (as of late April)

| Component | Status | Notes |
|-----------|--------|-------|
| a1: Send random bracket orders to IB paper daily | ✅ Working | Hundreds of orders per day, random timestamps in RTH |
| a2: Fetch previous day tick CSV (TRADES + BID_ASK) | ✅ Working | Saved to `data/bars/` |
| b1: Feed yesterday's paper results into back-trader | ❌ Not built | Next priority |
| b2: Iterative back-trader improvement loop | ❌ Not built | Follows b1 |

---

## Key Decision: Eligibility for Back-Trading Stats

**Problem discovered:** Orders canceled mid-market-day (e.g. stagnation exits, manual stops)
are impossible to back-trade reliably — the simulator cannot reproduce the cancellation trigger.

**Rules going forward:**

1. **No mid-day order cancellations.** Once an entry is submitted, it runs until TP, SL, or market close.
2. **Shutdown cancellations are allowed** but marked `eligible_for_bt = False` in the DB.
3. Only `eligible_for_bt = True` rows are used in grader comparisons and iteration metrics.
4. This applies retroactively: any existing paper fills with `exit_type = CANCELLED_SHUTDOWN`
   are excluded from grading.

---

## Monthly Goals (Apr 26 → May 31)

### Week 1 — Apr 26–May 2: Pipeline: Paper → Back-Trader (b1)

**Goal:** Every morning, yesterday's IB paper fills are automatically loaded into the back-trader
and compared against what the simulator would have predicted.

**Tasks:**
- [ ] Add `paper_fills` table to back-trading DB (if not already present)
- [ ] Write `import_paper_fills.py`: reads yesterday's IB execution report (or queries `trader` DB)
      and inserts rows into `back-trading/data/backtest.db`
- [ ] Mark `eligible_for_bt` flag on each fill (False if CANCELLED_SHUTDOWN, True otherwise)
- [ ] Wire into daily cron / startup script so it runs automatically each morning

**Output:** Back-trading DB has a growing table of real paper fills, eligibility-flagged.

---

### Week 2 — May 3–9: Grader v1 + First Comparison (b2 iteration 1–2)

**Goal:** Run the back-trader simulator on the same day's data and produce a grade report.

**Tasks:**
- [ ] Ensure `engine.py` can run a single day and produce `sim_fills` for that date
- [ ] Run grader: compare `sim_fills` vs `paper_fills` (eligible only)
      - Match by: same direction, same bracket size, same entry timestamp window
      - Grade metric: |sim_exit_price − paper_exit_price| ≤ 1 tick → match
- [ ] Output grade report to `data/results/grade_YYYYMMDD.json`
- [ ] Log: total eligible trades, % matched within 1 tick, avg price deviation
- [ ] **Iteration 1:** Inspect first results manually. Identify top mismatch patterns.
- [ ] **Iteration 2:** Tune fill model (entry fill logic: bid/ask thresholds) based on findings.

---

### Week 3 — May 10–16: Automated Improvement Loop (b2 iterations 3–5)

**Goal:** The improvement loop runs automatically, proposes parameter changes, re-grades.

**Tasks:**
- [ ] Write `improver.py`: reads last N grade reports, identifies worst-performing parameter
      (bracket size, offset range, fill model tolerance) and proposes a delta
- [ ] Auto-apply delta, re-run simulator on last 5 days, compare grade before/after
- [ ] Accept change if avg grade improves; revert if not
- [ ] Save improvement history to `data/results/improvement_log.json`
- [ ] **Iteration 3:** Entry offset range tuning
- [ ] **Iteration 4:** SL slippage model tuning
- [ ] **Iteration 5:** Bracket size eligibility filtering (e.g. small brackets harder to sim)

---

### Week 4 — May 17–23: Order Generation Adjustment (b2 iterations 6–8)

**Goal:** Use grader findings to adjust what orders we *send* — not just how we simulate.
The generator should favor trade setups that are easier to back-trade (more predictable fills).

**Tasks:**
- [ ] Add `backtradeability_score` per bracket size based on historical grade %
- [ ] Adjust `send_weights` in generator config: higher weight → sent more often
- [ ] Example: if 16pt brackets grade at 90% accuracy and 2pt at 55%, bias toward 16pt
- [ ] **Iteration 6:** Weight update based on bracket size accuracy
- [ ] **Iteration 7:** Offset range adjustment (avoid offsets where fills are unpredictable)
- [ ] **Iteration 8:** Timestamp distribution adjustment (avoid first/last 30min of RTH if grades are poor)

---

### Week 5 — May 24–31: Stabilize + Assessment

**Goal:** All daily automation is solid. Review one month of data.

**Tasks:**
- [ ] **Iteration 9:** Full review — re-grade all of May with updated sim params
- [ ] **Iteration 10:** Final parameter set locked in as baseline for June
- [ ] Write summary report: grade % trend over May, PnL sim vs paper deviation
- [ ] Confirm daily pipeline runs without manual intervention:
      `a1 (send) → a2 (fetch) → b1 (import paper) → b2 (grade + improve)`
- [ ] Document any remaining eligibility edge cases

---

## Daily Automation Target (end state)

```
06:00 CT  fetch_yesterday.py     → data/bars/MES_trades_YYYYMMDD.csv
06:05 CT  import_paper_fills.py  → backtest.db paper_fills (eligible-flagged)
06:10 CT  engine.py --date yesterday → backtest.db sim_fills
06:15 CT  grader.py              → data/results/grade_YYYYMMDD.json
06:20 CT  improver.py            → (if iteration pending) update params
08:25 CT  visualizer/app.py      → Generate + Send to IB paper (RTH)
```

---

## Constraints

- Market open: Mon–Fri, RTH 08:30–14:30 CT
- IB paper: no real money, but API rate limits apply
- Back-trader must never see future data (no look-ahead in fill model)
- Eligibility rule: `eligible_for_bt = False` rows never counted in grade %
- All iteration changes are logged and reversible (JSON history)

---

## Out of Scope This Month

- Multi-symbol support (MES only for now)
- Manual critical lines (random offsets only)
- ML-based generator
- Live trading (paper only)
