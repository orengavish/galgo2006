# Choices Done — Autonomous Decision Log
**Append-only audit trail of all autonomous decisions made during implementation.**  
Format per entry: date | decision | why | design doc section affected

---

| Date | Decision | Why | Design Doc Section |
|---|---|---|---|
| 2026-04-27 | Created `algo_implementation_rules.md` as the governing doc for the 5×5×5 implementation plan | Needed a rules layer on top of existing `rules_book.md` specific to the algo implementation scopes A–D | `system_design.md` § Implementation Sequence |
| 2026-04-27 | Defined "verified trade" as: valid entry + valid non-shutdown exit + no tracing errors + saved in DB with P&L and timestamps | User definition — precise enough to be measurable in Scope B performance check | `system_design.md` § 7 (Implementation Sequence — Phase B) |
| 2026-04-27 | A/B harness: manually triggered from browser, designed for future automation | User decision — avoids over-engineering for automation now while keeping the path open | `system_design.md` § 6 (A/B Harness) |
| 2026-04-27 | Claude may update `system_design.md` autonomously; every update requires one `choices_done.md` entry in the same step | User approved autonomous updates to avoid bottlenecks; audit trail compensates for reduced oversight | `system_design.md` (all sections) |
