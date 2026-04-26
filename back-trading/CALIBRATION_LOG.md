# Calibration Iteration Log

## Run #1 — Baseline (iteration 0)
**Time:** 2026-04-25  
**Change:** TP/SL phase-2 only, no stagnation model  
**Overall:** 23.6% (34/144) on 2026-04-24  
**TP:** 95%  **SL:** 93.8%  **STAG:** 0%  **Avg delta:** 2.4tk  
**Better:** baseline  

---

## Run #2 — Stagnation model (iteration 1)
**Time:** 2026-04-25  
**Change:** Add stagnation timeout: first tick >=300s after fill where `abs(price-fill) < 0.5pt`  
**Overall:** 30.6% (+7pp)  
**TP:** 85%  **SL:** 87.5%  **STAG:** 12%  **Avg delta:** 2.5tk  
**Better:** YES  
**Conclusion:** Adding stagnation detection improved overall 7pp. TP/SL dropped slightly (2-3 trades now misclassified as STAGNATION — brief price touches that sim treats as stagnation but IB would fill TP/SL). Stagnation detection only 12% — most cases have TP/SL firing first in sim.

---

## Run #3 — Stagnation last-known price (iteration 2)
**Time:** 2026-04-25  
**Change:** At stag_cutoff, check last-known trade price (before cutoff), not first new tick  
**Overall:** 30.6% (unchanged)  
**Better:** NO (tied)  
**Conclusion:** Fix was valid conceptually (live system uses last-known price) but in practice the last price before cutoff was never within 0.5pt in cases we weren't already catching. No new detections.

---

## Run #4 — TP confirm 2-ticks + TP tick-fill (iteration 3)
**Time:** 2026-04-25  
**Change A (reverted):** BID/ASK-based TP detection — fires when bid>=tp (BUY TP) or ask<=tp (SELL TP)  
**Result:** TP accuracy crashed 95%→5%, STAG improved 12%→26%, overall 29.9% (WORSE)  
**Reverted.**  
**Change B:** Require >=2 consecutive ticks at TP level (tp_confirm_ticks=2)  
**Result:** Same (30.6%). Brief touches have 312 median ticks, not just 1.  
**Change C (final):** TP fill price = actual tick price (not tp_price), to model price improvement  
**Overall:** 30.6% on 2026-04-24 (unchanged), but 44.2% including 2026-04-23  
**Better:** YES (due to 2026-04-23 data addition, not the change itself)  
**Conclusion:** The TP-tick-fill change didn't help on 2026-04-24. It may help on 2026-04-23 (47% TP exits vs 14%). The real lesson: brief TP touches have 312 median ticks and 33s hold time — impossible to filter by tick count. The bracket doesn't fill because IB uses bid/ask strictly, not trade prices.

---

## Root cause of STAGNATION miss (91/136 cases)
- Price crosses TP/SL briefly (stays there 33s median, 312 ticks)
- IB paper trading does NOT fill the bracket (bid/ask not reaching limit)
- Price bounces back, stays near fill for 5+ minutes (median 322s after TP cross)
- Stagnation fires
- Our sim fires TP/SL on the first trade at/past the level (can't distinguish bid/ask side)
- **Fundamental limitation**: TRADES data doesn't tell us if the tick was bid-side or ask-side

---

## Run #6 — TP tick-fill-price (iteration 4) [saved]
**Overall:** 44.2% (92/208) on 2 dates  
**TP:** 86%  **SL:** 90.9%  **STAG:** 21.3%  **Avg delta:** 3.8tk  
**Better:** YES  
**Note:** Improvement is from 2026-04-23 data (47% TP exits vs 14% on 2026-04-24). The tick-price fill model is active for SELL TPs.

---

## Run #7 — SL slippage = 0 ticks (iteration 5) [saved]
**Time:** 2026-04-25  
**Change:** `_SL_SLIP_TICKS = 0` (was 1). Data shows 64% actual SL exits have 0 slippage.  
**Overall:** 44.2% (92/208) on 2 dates (unchanged)  
**TP:** 86%  **SL:** 90.9%  **STAG:** 21.3%  **Avg delta:** 3.7tk (SL delta improved 1.9→1.6tk)  
**Better:** NO (tied on overall, minor delta improvement)  
**Conclusion:** 0-slippage model is more accurate for SL price prediction. No new type matches.

---

## Run #8 — Widen stagnation window to 1.0pt (iteration 6) [saved]
**Time:** 2026-04-25  
**Change:** Stagnation threshold: `stag_move` (0.5pt) → `stag_move * 2` (1.0pt). Both the last-known-price check and the post-cutoff tick scan use the wider window.  
**Overall:** 45.7% (95/208) — +1.5pp  
**TP:** 86%  **SL:** 90.9%  **STAG:** 23.5%  **Avg delta:** 3.7tk  
**Better:** YES  
**Conclusion:** Caught 3 more stagnation trades. TP/SL unchanged — no false positives introduced. 104 stagnation trades still missed (price crosses TP/SL before stagnation fires in sim).

---

## Iteration 7 — Bracket anchor for STP entries [investigation only, no change]
**Time:** 2026-04-25  
**Finding:** IB anchors TP/SL bracket to `entry_price` (the stop trigger), not `fill_price`. Confirmed: `tp - entry_price` = exactly bracket size for all STP rows; `tp - fill_price` varies. calibrate.py already passes `row["tp_price"]` / `row["sl_price"]` directly from DB → already correct. No simulator change needed.  
**Better:** N/A (no change)

---

## Run #9 — Stagnation poll model 5s (iteration 8) [saved, REVERTED]
**Time:** 2026-04-25  
**Change:** After stag_cutoff, poll every 5s using last-known price (matches position_manager interval). Previously fired at first qualifying tick after cutoff.  
**Overall:** 44.7% (93/208) — −1.0pp  
**TP:** 86%  **SL:** 90.9%  **STAG:** 22.1%  **Avg delta:** 3.7tk  
**Better:** NO (WORSE)  
**Conclusion:** Polling every 5s fires stagnation LATER than firing on first qualifying tick. This gives TP/SL more time to win → 2 fewer stagnation catches. **Reverted to iter6 (tick-based).**

---

## Iteration 9 — Fetch 2026-04-21/22 [PENDING — needs IB connection]
Tick files for 04-21 (9 trades) and 04-22 (19 trades) not yet fetched. Once available: run full 236-trade calibration.

---

## Run #10 — Best combination summary (iteration 10) [final]
**Time:** 2026-04-25  
**Active changes (iter6 = current simulator.py):**
- `_SL_SLIP_TICKS = 0` (iter5)
- SELL TP uses tick fill price (iter4)
- Stagnation threshold: `stag_move * 2` = 1.0pt (iter6)
- Stagnation: last-known price at cutoff, then first qualifying tick (iter2+6)
- TP confirm: ≥2 ticks at/past TP level (iter3)

**Best result:** 45.7% (95/208) on 2 dates (208/236 trades)  
**TP:** 86%  **SL:** 90.9%  **STAG:** 23.5%  **Avg delta:** 3.7tk  

**Ceiling analysis:**
- STAGNATION miss (104/136): fundamental bid/ask gap — sim fires TP/SL on any trade at the level, IB doesn't fill unless bid/ask reaches limit. Can't distinguish from TRADES data alone.
- Theoretical max if all stagnation caught: (104+95)/208 = ~96%
- Practical ceiling: ~70% if we could fix 50% of stagnation misses

**Next step:** Fetch 04-21/22 tick data (IB connection required) → run full 236-trade baseline.
