# Week Plan — May 27 → June 6, 2026

## Status Reset
May goals failed: zero progress on data files, failed to build askbid/trades pipeline, failed on broker/backtrader.
This week is a clean reboot.

---

## Priority 0 — Permissions (DONE)
`settings.local.json` updated to `"Bash(*)"` / `"PowerShell(*)"` / `"Write(*)"` / `"Edit(*)"` — no more approval prompts.

---

## Goal 1 — June Subfolder (Day 1: May 27)

**What:**  
Create `C:\Projects\galgo2026\june\` as a standalone Claude Code project.  
Contains only what is verified working; ships with its own `CLAUDE.md` that fully describes the system so any session can start cold.

**Tasks:**
- [ ] Create `june/` folder with subfolders: `lib/`, `trader/`, `back-trading/`, `broker/`, `data/`, `docs/`, `.claude/`
- [ ] Copy to june: `lib/*.py`, `trader/fetcher.py`, `trader/fetch_priority.py`, `trader/config.yaml`, `lib/config_loader.py`, `lib/logger.py`, `lib/db.py`, `lib/ib_client.py`, `lib/ibc_launcher.py`
- [ ] Write `june/CLAUDE.md` — full system description (symbols, ports, DB path, what each module does, June goals)
- [ ] Copy + update `docs/system_design.md` → `june/docs/`
- [ ] Create `june/.claude/settings.json` with same broad permissions

**Estimate:** 2–3 hours

---

## Goal 2 — Fetcher Overhaul (Days 1–3: May 27–29)

### 2a — Remove old schedulers
- [ ] Remove all Windows Task Scheduler tasks on THIS computer: `schtasks /delete /tn "galgo*" /f`
- [ ] Document what tasks existed on the other computer (ask user) — remove them too
- [ ] Confirm no python fetcher processes running

### 2b — New scheduled fetcher on this computer only
**Reuse:** `trader/fetcher.py` (8-worker paginator, all 4 symbols, resume support)  
**Symbols:** MES, MNQ, MYM, M2K (already in `config.yaml` under `fetcher.symbols_override`)

New script: `june/trader/fetch_scheduler.py` (replaces `may_scheduler.py`)
- Runs daily at 17:30 CT (after CME close)
- Calls `fetcher.fetch_day()` for all 4 symbols with `--bid-ask`
- Uses smart priority: skip if file already verified; fetch missing-bidask days first
- IBC: launch gateway at start if not up, stop after all fetches done
- Writes summary log

Windows Task Scheduler setup:
- `.bat` file: `june/scripts/run_fetcher.bat`
- Schedule: daily, 17:30 CT = 23:30 UTC (or 00:30 next day depending on DST)

### 2c — IBC on demand (paper only)
**Reuse:** `lib/ibc_launcher.py`  
- Start gateway before fetch, stop after (or leave up if paper session follows)
- Only paper port 4002
- Add `--ibc-start` / `--ibc-stop` flags to `fetch_scheduler.py`

### 2d — Google Drive upload module (NEW)
New file: `june/lib/gdrive.py`
```python
# Interface:
upload_file(local_path: Path, drive_folder_id: str) -> str  # returns drive file id
file_exists_on_drive(filename: str, folder_id: str) -> bool
```
- Uses `google-api-python-client` + service account (or OAuth)
- Called after `_mark_finished()` + `verify_csv()` passes
- Retry on failure (3 attempts, 5s backoff)

Integration in `fetcher.py`: after `_mark_finished()`, call `gdrive.upload_file()`

### 2e — Data correctness review
Goal: confirm our fetched files match IB's own data (source of truth).

Two approaches:
1. **Cross-symbol sanity:** on any given minute, MES and ES prices should move together. Flag days where they diverge >5 points.
2. **IB TWS market scanner / flex report:** download IB's own trade summary for a day, compare total volume against our tick count. ±10% = suspicious.
3. **Intraday gap check:** if there's a gap > 5 minutes with zero ticks during RTH, flag it.

New script: `june/trader/verify_data.py`
- `--date`, `--symbol` args
- Checks: row count, price range, RTH gap detection, bid/ask inversion rate
- Prints pass/fail + saves to `fetch_progress.db` field `verified=1`

**Estimate for 2a–2e:** 3 days

---

## Goal 3 — Backtrader Total Rewrite (Days 3–6: May 29 → June 1)

### Architecture change: CSV → DB first

**Current:** engine.py reads CSV files, simulates, writes to backtest.db  
**New:** all tick data is also written into DB (new table `tick_data`). Control table `data_files` tracks completion per (symbol, date, dtype). Multiple processes can each work on different days safely (SQLite WAL mode).

### Input contract (single command)
```python
@dataclass
class BacktradeCommand:
    ts: datetime          # entry timestamp (UTC)
    direction: str        # "BUY" or "SELL"
    entry_type: str       # "MKT" or "LMT"
    price: float          # entry price
    tp_ticks: int         # take-profit ticks
    sl_ticks: int         # stop-loss ticks
    symbol: str
    quantity: int
```

### New files (total rewrite)
1. `june/back-trading/bt_command.py` — dataclass + DB serialization
2. `june/back-trading/bt_db.py` — new DB schema (see below)
3. `june/back-trading/bt_fetcher.py` — incremental 1000-tick fetcher
4. `june/back-trading/bt_simulator.py` — reuse core of existing `simulator.py`
5. `june/back-trading/bt_engine.py` — orchestrator (replaces engine.py)
6. `june/back-trading/bt_worker.py` — background worker (one command at a time)

### New DB tables (`bt_db.py`)
```sql
-- tick data stored in DB (also written by fetcher when complete)
CREATE TABLE tick_data (
    id INTEGER PRIMARY KEY,
    symbol TEXT, date TEXT, dtype TEXT,   -- dtype: trades | bidask
    ts_utc TEXT, price REAL, size INTEGER,
    bid_p REAL, bid_s INTEGER, ask_p REAL, ask_s INTEGER
);

-- completion control table
CREATE TABLE data_files (
    symbol TEXT, date TEXT, dtype TEXT,
    status TEXT DEFAULT 'missing',   -- missing | fetching | complete
    tick_count INTEGER,
    updated_at TEXT,
    drive_file_id TEXT,
    PRIMARY KEY (symbol, date, dtype)
);

-- backtest results
CREATE TABLE bt_runs (
    id INTEGER PRIMARY KEY,
    command_id INTEGER,
    symbol TEXT, date TEXT,
    entry_ts TEXT, direction TEXT,
    entry_price REAL, exit_price REAL,
    exit_reason TEXT,   -- TP | SL | EOD | TIMEOUT
    pnl_ticks INTEGER,
    ticks_consumed INTEGER,
    runtime_ms INTEGER,
    created_at TEXT
);

-- commands queue
CREATE TABLE bt_commands (
    id INTEGER PRIMARY KEY,
    ts TEXT, direction TEXT, entry_type TEXT,
    price REAL, tp_ticks INTEGER, sl_ticks INTEGER,
    symbol TEXT, quantity INTEGER,
    status TEXT DEFAULT 'pending',  -- pending | running | done | failed
    result_id INTEGER,
    created_at TEXT
);
```

### Incremental fetch + simulate loop (bt_engine.py)
```
for each command:
  1. check data_files table: if status=complete → use tick_data from DB
  2. if missing: start incremental fetch loop:
     a. fetch 1000 ticks from IB (start_ts = command.ts)
     b. try to simulate: if position opened AND closed → done
     c. if not closed: fetch next 1000 ticks from cursor
     d. repeat until exit or EOD
  3. write result to bt_runs
  4. if full day now complete in DB: mark data_files.status=complete, upload to Drive
```

### Bracket size recommendation for fast exit
For `--build-trades` with backtrader simulation:
- **TP: 4 ticks (1.0 point = $5/contract MES)**
- **SL: 4 ticks (1.0 point = $5/contract MES)**
- Symmetric 1:1 ratio → high fill probability within 1-2 batches of 1000 ticks
- For MNQ: 4 ticks = 0.5 NQ points ($5 micro)
- Avoids long-running positions that require many 1000-tick batches
- Can be configured per command in `BacktradeCommand`

**Estimate:** 3 days (biggest chunk)

---

## Goal 4 — Broker Rewrite (Days 6–9: June 1–5)

### Guiding constraints
- Paper only, scheduled
- `--build-trades` sends as many commands as possible at once
- Brackets: small/fast (same 4/4 ticks as backtrader default)
- Price source: save from fills → DB (bypass IB's 15min paper delay)

### Contract counts (recommendation)
| Symbol | Contracts | Rationale |
|--------|-----------|-----------|
| MES    | 2         | Main instrument, most liquid micro |
| MNQ    | 1         | Nasdaq micro, correlated to MES |
| MYM    | 1         | Dow micro |
| M2K    | 1         | Russell micro, most divergent |
Total: 5 micro contracts. At 4-tick SL, max loss per full sweep = $25. Safe for paper.

### New/modified files
1. **`june/trader/broker.py`** — keep core, add:
   - `--build-trades` mode: read all PENDING commands, submit all at once
   - Traceability: link parent_command_id → (entry_order_id, tp_order_id, sl_order_id) in DB
   - Price cache: on every fill event, save `(symbol, price, ts)` to `price_cache` table

2. **`june/lib/db.py`** — add new tables:
   ```sql
   CREATE TABLE price_cache (
       symbol TEXT PRIMARY KEY,
       last_price REAL,
       last_fill_ts TEXT,
       source TEXT   -- 'fill' | 'paper_delayed'
   );
   CREATE TABLE bracket_map (
       command_id INTEGER,
       entry_order_id INTEGER,
       tp_order_id INTEGER,
       sl_order_id INTEGER,
       PRIMARY KEY (command_id)
   );
   ```

3. **`june/trader/decider.py`** — add multi-symbol support (currently MES only):
   - Generate commands for all trading symbols in config
   - Limit: max N active commands per symbol at once

### Traceability solution for bracket splits
IB brackets submit as 3 orders (parent + 2 children). Problem: if parent fills, IB assigns new order IDs to TP/SL children.

Solution:
- On `place_bracket()`, record all 3 order IDs into `bracket_map`
- On every `execDetails` event, check if order_id matches any `tp_order_id` or `sl_order_id`
- This keeps the parent `command_id` as the single key across all 3 legs

### Replenish
Reuse existing `spawn_replenishment()` from `lib/db.py`. Extend: after replenishment command inserted, if `--build-trades` mode → submit immediately without waiting for next poll cycle.

### `--build-trades` flow
```
1. fetch current price for each symbol from price_cache (fallback: IB reqMktData with timeout)
2. for each symbol × direction (buy/sell based on critical lines):
   generate BacktradeCommand with price = cached_price ± entry_offset
3. insert all as PENDING into commands table
4. broker poll loop picks them up immediately (poll_seconds=1 in build-trades mode)
5. all submitted in one IB batch
```

**Estimate:** 3 days

---

## Summary Schedule

| Days | Goal | Key Deliverable |
|------|------|-----------------|
| May 27 | june/ folder + permissions | Standalone project, clean slate |
| May 27–29 | Fetcher overhaul | Scheduled, 4 symbols, Google Drive, correctness check |
| May 29–Jun 1 | Backtrader rewrite | DB-first, incremental 1000-tick, results in DB |
| Jun 1–5 | Broker | --build-trades, traceability, price cache |
| Jun 5–6 | Integration test | End-to-end: schedule → fetch → backtest → broker |

**Total estimate:** ~50 hours. Aggressive but doable.

---

## What We Reuse vs Rewrite

| Module | Reuse | Change |
|--------|-------|--------|
| `trader/fetcher.py` | Core paginator, session bounds, progress DB | Add Google Drive call after finish |
| `trader/fetch_priority.py` | All of it | Minor: also check `data_files` table |
| `lib/ibc_launcher.py` | All of it | None |
| `lib/config_loader.py` | All of it | None |
| `lib/logger.py` | All of it | None |
| `lib/ib_client.py` | All of it | None |
| `back-trading/simulator.py` | Core tick-replay logic | Wrap in incremental API |
| `lib/order_builder.py` | All of it | None |
| `trader/broker.py` | Fill detection, reconnect, IB event wiring | Add bracket_map, price_cache, --build-trades |
| `trader/decider.py` | Replenish logic | Add multi-symbol, multi-contract |
| `back-trading/engine.py` | NONE | Total rewrite as bt_engine.py |
| `back-trading/generator.py` | NONE | Replaced by BacktradeCommand input |
| `back-trading/grader.py` | Keep as-is | Not in scope this week |
