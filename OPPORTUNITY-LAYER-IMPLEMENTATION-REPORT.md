# OPPORTUNITY-LAYER-IMPLEMENTATION-REPORT

## 1. Architecture change

```
BEFORE:  MARKET DATA → CAUSAL SMC ENGINE → TRADE SETUP → RISK → EXECUTION
AFTER:   MARKET DATA → CAUSAL SMC ENGINE → OPPORTUNITY ENGINE → TRADE SETUP → RISK → EXECUTION
```

The causal engine remains the sole authority for structural truth. The new
opportunity layer sits strictly between causal analysis and setup generation:

* **CAUSAL TRUTH** ("what structurally happened") — unchanged, engine-owned.
* **OPPORTUNITY STATE** ("what is currently developing") — new, persisted,
  tracked across polling cycles.

An opportunity can exist, persist and progress while no execution-ready
`TradeSetup` exists yet. TradeSetup creation is still owned exclusively by the
existing causal path; the opportunity engine only **links** to setups it
observes (`CONVERTED_TO_SETUP`).

## 2. Files changed

New package `src/smc_engine/opportunity/`:
* `models.py` — enums (`OpportunityState`, `OpportunityType`, `EntryPathway`,
  `BlockReason`, `OpportunityEventKind`), `Opportunity`, `POICandidate`,
  `OpportunityWindows`, timestamp-anchored identity helpers.
* `state_machine.py` — explicit transition table (terminal states never
  transition back).
* `repository.py` — SQLite persistence (`opportunities`,
  `opportunity_state_history`, `opportunity_events`) in the existing GUI
  database; additive `CREATE TABLE IF NOT EXISTS` migration; restart recovery;
  alert idempotency via `UNIQUE(opportunity_id, transition, evidence_key)`.
* `evaluator.py` — pure, `as_of`-truncated causal view + POI candidate
  discovery/ranking with recorded rejection reasons + audit classification.
* `engine.py` — `OpportunityEngine` (discover / advance / expire / promote /
  funnel / diagnostics / audit). Non-executing by construction.

Modified:
* `src/smc_engine/web/hub.py` — engine instance, UI-independent
  `scan_universe_once()`, per-symbol isolation, alert dispatch, diagnostics,
  additive fields on `/api/analysis` and `/api/readiness`.
* `src/smc_engine/web/api.py` — `/api/opportunities`,
  `/api/opportunities/diagnostics`, `/api/opportunities/{id}`.
* `src/smc_engine/fvg.py` — FVG ids are now timestamp-derived
  (`FVG-B-<ns>`) instead of index-derived (rolling-window identity safety;
  semantics unchanged).
* `src/smc_engine/web/static/{index.html,app.js,styles.css}` — OPPORTUNITIES
  panel + SSE wiring (view-only).
* `tests/test_opportunity_engine.py`, `tests/test_opportunity_cases.py`,
  `tests/test_opportunity_watcher.py` — 32 new tests.

## 3. Schema / migrations

```sql
CREATE TABLE IF NOT EXISTS opportunities(
  opportunity_id TEXT PRIMARY KEY, canonical_key TEXT NOT NULL UNIQUE,
  symbol, symbol_norm, direction, opportunity_type, state,
  created_at, updated_at, expires_at, first_seen, last_seen,
  setup_id, risk_status, reason, blocker, next_expected, payload_json);
CREATE TABLE IF NOT EXISTS opportunity_state_history(...);
CREATE TABLE IF NOT EXISTS opportunity_events(... UNIQUE(opportunity_id, transition, evidence_key));
```

Additive only; existing setup / history / lifecycle tables untouched; no
destructive changes; restart recovery = load active rows.

## 4. State machine

`IDLE → STRUCTURAL_CONTEXT → OPPORTUNITY_ARMED → WAITING_FOR_CONFIRMATION →
WAITING_FOR_POI → WAITING_FOR_IDM → READY_FOR_MITIGATION → ENTRY_TRIGGERED`,
with `INVALIDATED` / `EXPIRED` / `TERMINAL` terminals. Transitions are declared
in one table; illegal transitions raise; terminal states cannot resurrect (a
new canonical identity is required for a new setup).

## 5. Opportunity classes & entry pathways

* Classes: **REVERSAL** (sweep-anchored), **CONTINUATION** (BOS-anchored);
  PULLBACK mechanics are expressed as an entry pathway on the reversal stream.
* Pathways (distinct attributes/states, not UI labels):
  * `PULLBACK` — waits for the selected POI to be mitigated; tracks IDM.
  * `AGGRESSIVE` — READY at CSD + POI (documented exception to the IDM wait;
    still passes all risk/execution gates).
  * `SMART` — READY only when POI + IDM exist **and** price is within a
    configurable ATR distance of the POI.
  * `CONTINUATION` — independent BOS-anchored stream requiring a new
    directionally valid POI formed after the BOS.

## 6. Temporal windows (centralized, observable)

`OpportunityWindows`: `sweep_to_csd_bars=24`, `csd_to_poi_bars=96`,
`poi_to_mitigation_bars=96`, `continuation_bos_to_poi_bars=96`,
`ready_ttl_bars=96`, `poi_max_age_bars=192`, `idm_window_bars=24`,
`max_price_run_atr=6.0`, `poi_min_height_atr=0.25`. Windows are exposed in
`/api/opportunities` (`windows`) and in diagnostics. The watcher's TTL sweep is
anchored to the **latest closed market-data time** (closed-M15 cadence is
authoritative), not the wall clock.

## 7. API changes

* `GET /api/opportunities?symbol=&state=&type=&active_only=` → opportunities +
  funnel + windows.
* `GET /api/opportunities/diagnostics` → watcher + engine counters + per-symbol
  diagnostics (`last_analysis_time`, `last_data_time`, `last_error`,
  `current_opportunity_count`).
* `GET /api/opportunities/{id}` → opportunity + state history + audit.
* `/api/analysis` and `/api/readiness` gained additive `opportunities` and
  `opportunity_audit` fields (all existing fields unchanged).

## 8. UI changes

New **OPPORTUNITIES** panel (right column): SYMBOL, DIRECTION, TYPE, STATE
label (WATCHING/ARMED/DEVELOPING/READY/EXECUTION READY/INVALIDATED/EXPIRED),
causal progress chain (SWEEP → BOS → CSD → POI → IDM → READY → ENTRY), POI,
entry pathway, age, expiry, blocker + next expected event, risk/setup status.
Clicking a row **views** that symbol — it never causes discovery. SSE
`OPPORTUNITY_CREATED / OPPORTUNITY_ADVANCED / POI_FOUND / IDM_CONFIRMED /
READY / INVALIDATED / EXPIRED / CONVERTED_TO_SETUP` produce toasts and refresh.
The existing setup UI is untouched.

## 9. Tests (32 new; suite 460 passed / 1 skipped)

State transitions, sweep-arms, duplicate-sweep dedup, sweep expiry, CSD
progression, opposing-CSD cancellation, POI discovery/expiry/consumption,
IDM progression, READY persistence/expiry/invalidation, execution-ready
progression, all four pathways, continuation stream + independent TTL, restart
persistence, polling idempotency, rolling-window identity stability, replay
determinism, no-look-ahead, risk/portfolio rejection non-destruction, terminal
non-resurrection, background discovery without UI selection, symbol-failure
isolation, zero broker primitives, and the before/after funnel.

## 10. Safety verification

* `order_send` / `order_check` / `TRADE_ACTION*` / `MetaTrader5` imports:
  **absent** from the entire opportunity package (static test).
* Opportunity discovery never submits orders; live verification showed
  `POST /api/live/*` → **403**, `live_execution_enabled=false`.
* Gate A / Gate B / demo-only boundaries untouched; no second execution path.

## 11. Newly discoverable opportunities (evidence)

Same historical replay set (101 samples):

```
OLD ENGINE
  structural candidates:        0
  execution-ready observations: 0
  samples with any setup:       0 / 101

NEW ENGINE
  opportunities armed:          4 (across the replay)
  CSD progressions:             reached
  POI candidates:               selected (OB-M15-…)
  IDM progressions:             confirmed
  READY opportunities:          1 (18 ready-observations)
  execution-ready:              entry-triggered on touch
  expired:                      3 (stale arms)
  invalidated:                  0
  cycles with a developing opportunity but NO old setup: 57
```

Live (Exness demo, current market): watcher running over a **24-symbol
universe** (8 succeeded, 16 isolated per-symbol failures), producing **47
opportunities** (24 READY, 23 entry-triggered, 2 converted to setups) while the
causal engine alone still reported `candidates=0`. The causal engine remains
the gate for execution-ready setups (readiness still
`WAITING_FOR_CAUSAL_SETUP`).

## 12. Reference EA comparison

| Reference behavior | Decision | Notes |
|---|---|---|
| Persistent WAIT state | **ADOPT** | `OPPORTUNITY_ARMED`/`WAITING_*` persist across cycles |
| WAIT TTL | **ADOPT** | centralized `OpportunityWindows`, observable |
| CSOD promotes WAIT→READY | **ADAPT** | uses the authoritative CSD (`confirm_csd`), not the EA's current-TF CSOD |
| Persistent READY | **ADOPT** | `READY_FOR_MITIGATION` survives until entry/expiry/invalidation |
| Multiple entry pathways | **ADOPT/ADAPT** | PULLBACK/AGGRESSIVE/SMART distinct states; execution still gated |
| Continuation stream after BOS | **ADOPT** | independent anchor + TTL; requires a new POI |
| POI from OB **or** FVG | **ADAPT** | tracked candidates ranked deterministically; rejections recorded |
| READY expiry/runaway/invalidation/touch | **ADAPT** | TTL, structural invalidation, ENTRY_PASSED; runaway guard config present |
| EA SL/TP/RR logic | **REJECT** | authoritative Python setup/risk rules unchanged |
| EA POI selection semantics | **REJECT** | deterministic ranking with recorded rejections instead |
| EA displacement/CSOD definitions | **REJECT** | causal engine definitions unchanged |
| EA auto-trading behavior | **REJECT** | opportunity layer is non-executing; live disabled |

## 13. Known limitations

* Opportunity discovery runs on the existing 15 s watcher with a closed-M15
  gate; heavy cold fetches across 24 symbols can take minutes on first pass
  (bounded by bars cache and per-symbol isolation).
* `AGGRESSIVE` intentionally skips the IDM wait (documented pathway policy) —
  it still passes every risk/execution gate; the causal engine remains the
  source of any resulting setup.
* `max_price_run_atr` (runaway guard) is configured but only partially applied
  (documented; no structural weakening).
* 16/24 live symbols currently fail data build (thin history on some CFDs) —
  isolated and reported per-symbol, never starving the scan.

## 14. Recommended next validation

1. Replay a longer real-data window (e.g., 60 days of M15 per symbol) and
   compare old/new funnels per symbol to quantify recovered opportunities.
2. Add the runaway guard as a formal transition (`READY → EXPIRED`,
   `PRICE_RAN_AWAY`) with tests.
3. Operator workflow: acknowledge opportunities in the UI (persisted
   acknowledgement) without implying trade authorization.
4. Keep Gate B blocked until the causal engine produces an unresolved
   execution-ready setup; the opportunity layer must never promote one itself.
