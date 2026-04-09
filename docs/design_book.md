# Galao System — Design Book
Version: 0.5.0 | Date: 2026-04-07

---

## 1. System Purpose

Galao is an **intraday futures trading learning platform**. Primary goal: collect structured trade data across different bracket sizes and line strengths to enable A/B testing. P&L optimization comes later.

---

## 2. System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                          GALAO SYSTEM                               │
│                                                                     │
│  ┌──────────────┐    DB (SQLite)    ┌──────────────────────────┐   │
│  │   DECIDER    │ ◄──────────────► │        BROKER            │   │
│  │ (bg process) │                  │      (bg process)        │   │
│  └──────┬───────┘                  └────────────┬─────────────┘   │
│         │                                        │                  │
│  Reads critical                          ib_insync/ibapi            │
│  line files                                      │                  │
│         │                          ┌─────────────┴──────────┐      │
│  ┌──────┴───────┐                  │     IB GATEWAY         │      │
│  │   FETCHER    │                  │  PAPER port 4002       │      │
│  │ (bg process) │                  │  LIVE  port 4001       │      │
│  └──────┬───────┘                  └────────────────────────┘      │
│         │ LIVE port only                                            │
│         │ CSV history files                                         │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │        VISUALIZER  (browser GUI)                             │  │
│  │        DB viewer + live orders/prices/P&L/status             │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │        ANALYZER    (bg process)  [LATER]                     │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Components

### 3.1 Decider (`decider.py`)
**Role:** Brain. Generates all trading commands at day open and handles replenishment all day.

**Inputs:**
- `data/critical_lines/levels_daily_YYYYMMDD.txt`
- DB `commands` table (polls for fills to trigger replenishment)
- `config.yaml`

**Outputs:**
- DB `commands` table (writes PENDING rows)
- DB `system_state` table (writes SHUTDOWN trigger)

**Lifecycle:**
```
startup:
  load config
  load critical lines for today (all symbols)
  validate: all symbols have line files, prices are sane
  resolve active contract months
  generate all day-open commands → write to DB
  state = RUNNING

main loop (every replenishment_poll_seconds):
  query DB for commands with status = FILLED and no replenishment yet
  for each new fill:
    write new PENDING command: same line, same direction, same bracket
  check time → if T-60min: write SHUTDOWN to system_state, exit loop
```

**Command generation per critical line:**
```
for each line (price P, strength S):
  P = round(P, tick=0.25)              # always round to MES tick size
  for each bracket size B in config.active_brackets:
    if current_price > P:              # price is ABOVE line
      write: LMT BUY  at P, TP=round(P+B,0.25), SL=round(P-B,0.25)
      write: STP SELL at P, TP=round(P-B,0.25), SL=round(P+B,0.25)
    else:                              # price is BELOW line
      write: STP BUY  at P, TP=round(P+B,0.25), SL=round(P-B,0.25)
      write: LMT SELL at P, TP=round(P-B,0.25), SL=round(P+B,0.25)
    store: strength=S, bracket=B on each command
```

**Toggle logic summary:**
| Current price vs line | BUY entry type | SELL entry type |
|-----------------------|---------------|-----------------|
| Price ABOVE line | `LMT BUY` | `STP SELL` |
| Price BELOW line | `STP BUY` | `LMT SELL` |

Toggle is re-evaluated on every replenishment (price may have crossed the line).

---

### 3.2 Broker (`broker.py`)
**Role:** Executor. Reads commands from DB, sends to IB, updates status. No trading logic.

**Inputs:**
- DB `commands` table (polls for PENDING commands)
- IB PAPER port 4002 (order execution)
- IB LIVE port 4001 (price data, position sync)

**Outputs:**
- DB `commands` table (updates status, IB order IDs, fill prices)
- DB `positions` table
- DB `ib_events` table

**Lifecycle:**
```
startup:
  connect to IB PAPER port 4002 (client ID pool)
  connect to IB LIVE port 4001 (client ID pool)
  sync open orders and positions from IB → update DB
  state = RUNNING

main loop (every command_poll_seconds):
  query DB for PENDING commands
  for each:
    build OCO bracket (LMT entry + LMT TP + STP SL)
    submit to IB PAPER port
    write IB order IDs to DB, set status = SUBMITTED

IB callbacks (event-driven):
  orderStatus → update DB status
  fill → update DB status = FILLED, write fill price, timestamp
  error → log to ib_events, flag in DB

shutdown (detects SHUTDOWN in system_state):
  cancel all SUBMITTED/PENDING orders
  tighten all open position stops to 1 point
  orderly exit loop → panic exit if time critical
```

**OCO Bracket Order (ib_insync):**
```python
# Entry: LMT (bounce — fills when price reaches the line)
parent = LimitOrder(action, qty, entry_price)
parent.transmit = False

# Take Profit: LMT
take_profit = LimitOrder(opposite, qty, tp_price)
take_profit.parentId = parent.orderId
take_profit.transmit = False

# Stop Loss: STP
stop_loss = StopOrder(opposite, qty, sl_price)
stop_loss.parentId = parent.orderId
stop_loss.transmit = True   # sends all 3
```

---

### 3.3 Fetcher (`fetcher.py`)
**Role:** Downloads historical price data to CSV. Runs independently, LIVE port only.

**Inputs:** config (symbols, date range, bar size), IB LIVE port 4001

**Outputs:** `data/history/{SYMBOL}_{YYYY-MM-DD}.csv`

**CSV format:**
```
timestamp,open,high,low,close,volume
2026-04-06 09:30:00,6250.25,6251.00,6249.75,6250.50,1234
```

**Reuse:** Based on existing `fetch_full_day.py` and `fetch_mes_historic_data.py` from V1.

---

### 3.4 Visualizer (`visualizer/`)
**Role:** Browser-based dashboard. Priority feature — needed for monitoring and DB inspection.

**Views (in priority order):**

| View | Content |
|------|---------|
| DB Viewer | Raw tables: commands, positions, ib_events — sortable, filterable |
| Live Orders | All pending/open orders with status, entry price, bracket |
| Positions | Open positions, unrealized P&L |
| P&L | Daily P&L, per symbol, per bracket size |
| Prices | Live price per symbol (LIVE port feed) |
| Connection | IB LIVE + PAPER connection status |
| Shutdown | Status, countdown, panic mode indicator |

**Tech:** Flask or FastAPI backend + HTML/JS frontend (lightweight, no heavy framework)

**Reuse:** `interactive_dashboard_V2.py` from V1 as reference — modularize into web components.

---

### 3.5 Analyzer (`analyzer.py`) — LATER

**Planned:** A/B test results engine.
- P&L by bracket size
- P&L by line strength (1-3)
- Win rate by bracket size × strength combination
- Fill frequency per line

---

## 4. Critical Lines File Format

**File:** `data/critical_lines/levels_daily_YYYYMMDD.txt`

**Format:** `SYMBOL, PRICE, STRENGTH`
```
MES, 6250, 3
MES, 6300, 2
MES, 6180, 1
MES, 6400, 3
```

**Rules:**
- Strength: 1 (weak) to 3 (strong)
- One file per trading day
- Same file can be copied/reused for multiple days
- Up to 10 lines per symbol

---

## 5. Database Schema

**File:** `data/galao.db` (extends V1 `trading_data.db` schema)

### Table: `commands`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto increment |
| created_at | DATETIME | UTC |
| symbol | TEXT | e.g. MES |
| contract_month | TEXT | e.g. 202506 |
| direction | TEXT | BUY / SELL |
| entry_price | REAL | Critical line price |
| bracket_size | REAL | Points — A/B test variable |
| take_profit | REAL | entry ± bracket_size |
| stop_loss | REAL | entry ∓ bracket_size |
| line_strength | INTEGER | 1-3 — A/B test variable |
| status | TEXT | PENDING / SUBMITTING / SUBMITTED / FILLED / EXITING / CLOSED / CANCELLED / ERROR / RECONCILE_REQUIRED |
| ib_parent_order_id | INTEGER | IB order ID for entry |
| ib_tp_order_id | INTEGER | IB order ID for TP |
| ib_sl_order_id | INTEGER | IB order ID for SL |
| fill_price | REAL | Actual fill price |
| filled_at | DATETIME | UTC |
| exit_price | REAL | TP or SL hit price |
| exit_at | DATETIME | UTC |
| exit_reason | TEXT | TP / SL / STAGNATION / SHUTDOWN_ORDERLY / SHUTDOWN_PANIC |
| is_replenishment | BOOLEAN | True if auto-replenished |
| parent_command_id | INTEGER | FK to original command that was filled |
| replenishment_issued | BOOLEAN | True once Decider has written replenishment — prevents duplicate (I-03) |
| order_ref | TEXT | Standardized ref (from V1 pattern) — stored in IB orderRef field |
| claimed_at | DATETIME | When Broker set status=SUBMITTING (claim lock) |
| notes | TEXT | |

### Table: `positions`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| command_id | INTEGER | FK to commands |
| symbol | TEXT | |
| direction | TEXT | BUY / SELL |
| entry_price | REAL | |
| entry_time | DATETIME | UTC |
| exit_price | REAL | |
| exit_time | DATETIME | UTC |
| pnl_points | REAL | |
| exit_reason | TEXT | TP / SL / STAGNATION / SHUTDOWN_ORDERLY / SHUTDOWN_PANIC |

### Table: `ib_events`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| timestamp | DATETIME | UTC |
| event_type | TEXT | orderStatus / fill / error / connection |
| ib_order_id | INTEGER | |
| data | TEXT | JSON blob |

### Table: `system_state`
| Column | Type | Description |
|--------|------|-------------|
| key | TEXT PK | e.g. session_state, shutdown_triggered |
| value | TEXT | |
| updated_at | DATETIME | UTC |

### Table: `release_notes`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| timestamp | DATETIME | UTC — when the change was made |
| program | TEXT | e.g. `broker.py`, `lib/db.py` |
| version | TEXT | e.g. `0.4.0` |
| change_type | TEXT | FEATURE / FIX / REFACTOR / TEST |
| description | TEXT | What changed and why |

*Read via `release_notes.py --program <name>` — filters by program name, shows all entries for that file.*

---

### Table: `critical_lines`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| date | DATE | Trading date |
| symbol | TEXT | |
| price | REAL | |
| strength | INTEGER | 1-3 |
| source | TEXT | MANUAL / AUTO (future) |

---

## 6. Configuration (`config.yaml`)

```yaml
# IB Connection
ib:
  live_host: 127.0.0.1
  live_port: 4001
  live_client_ids: [101, 102, 103]
  paper_host: 127.0.0.1
  paper_port: 4002
  paper_client_ids: [201, 301, 401]
  reconnect_interval_seconds: 30

# Symbols
symbols:
  - MES

# Trading Session (CME offsets in minutes)
session:
  open_offset_minutes: 30
  shutdown_offset_minutes: 60

# Order Parameters
orders:
  active_brackets: [2, 4]     # A/B test variable — any positive values
  quantity: 1                  # max 1 contract during learning phase
  tick_size: 0.25              # MES tick size — all prices rounded to this

# Position Management
position:
  stagnation_seconds: 60           # exit if in trade this long...
  stagnation_min_move_points: 0.5  # ...and price hasn't moved this much
  sl_cooldown_seconds: 30          # wait after SL hit before re-arming line

# Shutdown
shutdown:
  exit_patience_seconds: 30
  panic_threshold_minutes: 10

# Decider
decider:
  replenishment_poll_seconds: 10

# Broker
broker:
  command_poll_seconds: 5

# Paths
paths:
  db: data/galao.db
  critical_lines: data/critical_lines/
  history: data/history/
  logs: logs/
```

---

## 7. File / Directory Structure

```
galao/
├── .cursorrules
├── config.yaml
├── decider.py
├── broker.py
├── fetcher.py
├── release_notes.py          ← CLI reader: --program <name> filter
├── preflight.py              ← startup checklist (also used by --self-test)
├── analyzer.py               (later)
├── visualizer/
│   ├── app.py
│   ├── templates/
│   └── static/
├── lib/
│   ├── db.py
│   ├── ib_client.py
│   ├── order_builder.py
│   ├── config_loader.py
│   └── logger.py             ← shared logging setup
├── data/
│   ├── galao.db
│   ├── critical_lines/
│   │   └── levels_daily_20260407.txt
│   └── history/
│       └── MES_2026-04-07.csv
├── logs/
│   ├── decider.log
│   ├── broker.log
│   ├── fetcher.log
│   └── visualizer.log
├── regression.py             ← on-demand regression test runner
├── versions/                 ← timestamped file snapshots before each edit
│   └── broker.py.20260407_1151
└── docs/
    ├── rules_book.md
    ├── design_book.md
    ├── tech_solutions_book.md
    └── release_notes.md
```

---

## 8. Inter-Component Data Flow

```
DAY START (T+30min after CME open):
  Decider reads levels_daily_YYYYMMDD.txt
  → generates PENDING commands (LMT BUY + LMT SELL per line per bracket)
  → writes to DB commands table

  Broker polls DB → finds PENDING
  → builds OCO bracket (LMT+LMT+STP)
  → submits to IB PAPER port 4002
  → writes IB order IDs back to DB, status = SUBMITTED

ALL DAY:
  IB fires callbacks → Broker updates DB status
  On FILL: Broker sets status = FILLED, records exit_reason (TP/SL)
  Decider polls DB → detects FILLED
  → if exit was SL: start cool-down timer for that line (sl_cooldown_seconds)
  → if exit was TP or stagnation: replenish immediately
  → after cool-down expires: replenish
  → new command re-evaluates LMT/STP toggle based on current price

  Stagnation monitor (Broker, per open position):
  → every few seconds: check time_in_trade and price_moved_from_entry
  → if time > stagnation_seconds AND movement < stagnation_min_move: market exit
  → log exit_reason = STAGNATION

T-60min (SHUTDOWN):
  Decider writes SHUTDOWN to system_state
  Broker detects SHUTDOWN:
    cancel all SUBMITTED orders
    tighten open stops to 1pt
    exit open positions (orderly → panic)
    log all to DB

END OF DAY:
  DB = complete record of every order, fill, exit, reason
  Ready for Analyzer (future A/B test analysis)
```

---

## 9. Regression Test Program (`regression.py`)

**Run on demand only.** Three layers, printed output, logs to `logs/regression.log`.

### Layers

**Layer 1 — Self-tests** (no IB, no test DB required)
```
Run --self-test on every component:
  decider.py, broker.py, fetcher.py, preflight.py,
  release_notes.py, visualizer/app.py, lib/db.py,
  lib/ib_client.py, lib/order_builder.py, lib/config_loader.py
```

**Layer 2 — Feature tests** (uses test_galao.db, no IB required)
```
Config:
  - config loads and validates all required fields
  - missing required field raises clear error

Critical lines:
  - valid file parses correctly
  - invalid strength value (e.g. 4) raises error
  - missing file raises error

Order generation:
  - tick rounding: 6250.1 → 6250.0, 6250.3 → 6250.25
  - toggle logic: price above line → LMT BUY + STP SELL
  - toggle logic: price below line → STP BUY + LMT SELL
  - bracket sizes applied correctly (TP/SL symmetric)

DB:
  - write command row → read back → values match
  - write release note → filter by program → appears in results
  - system_state read/write

Replenishment:
  - FILLED command → replenishment command generated correctly
  - SL cool-down: command with SL exit → next replenishment delayed

Shutdown:
  - SHUTDOWN in system_state → no new commands generated

Stagnation:
  - position age > threshold + movement < threshold → STAGNATION logged
```

**Layer 3 — IB Integration** (requires IB Gateway running, uses PAPER port)
```
Connection:
  - connect to LIVE port 4001
  - connect to PAPER port 4002

Price fetch:
  - fetch current price for MES via LIVE port → returns valid float

Order submission (no-fill test):
  - get current MES price
  - place LMT BUY at (current_price - 500.0) rounded to 0.25
  - verify IB returns valid order ID
  - cancel order immediately
  - verify order status = Cancelled
```

### Output Format
```
=== GALAO REGRESSION TEST ===
2026-04-07 11:51:00 UTC

[Layer 1: Self-tests]
[PASS] self-test: decider.py           (0.31s)
[PASS] self-test: broker.py            (0.28s)
[PASS] self-test: lib/order_builder.py (0.05s)
...

[Layer 2: Feature tests]
[PASS] feature: tick_rounding          (0.01s)
[PASS] feature: toggle_logic_above     (0.01s)
[FAIL] feature: critical_lines_parse   (0.02s) — missing strength column
...

[Layer 3: Integration]
[PASS] integration: live_connection    (1.20s)
[PASS] integration: paper_connection   (0.95s)
[PASS] integration: price_fetch        (0.43s)
[PASS] integration: order_no_fill      (1.10s)

==============================
Result: 18 passed, 1 failed, 0 skipped
```

### Flags
```bash
python regression.py                   # all 3 layers
python regression.py --quick           # layers 1+2 only (no IB needed)
python regression.py --layer3-only     # layer 3 only
python regression.py --program broker  # only tests related to broker
```

### Definition of Done for Regression
Every new feature must include a corresponding Layer 2 test in `regression.py`.
Regression must fully pass before any version bump.

---

## 10. V1 Reuse Map

| Galao Component | Reuse from V1 |
|----------------|---------------|
| `lib/ib_client.py` | `ib_connect.py` — retry logic, client ID pool |
| `lib/order_builder.py` | `ib_executor.py` — bracket order structure (change STP→LMT for entry) |
| `lib/db.py` | `trading_db.py` — schema, context manager pattern |
| `fetcher.py` | `fetch_full_day.py` + `fetch_mes_historic_data.py` |
| `visualizer/` | `interactive_dashboard_V2.py` — modularize into web app |
| Order ref system | `order_tracking_utils.py` — standardized ref format |
| Shutdown scripts | `abort_all_positions.py`, `cancel_all.py` — feeds shutdown sequence |
