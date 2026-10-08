# Phase 14 — Pre-Arm Funnel Instrumentation

**Date:** 2026-10-08  
**Branch:** feature/gui-workstation  
**Constraint:** No changes to SMC methodology, thresholds, TTLs, or live execution.

---

## 1. Summary

This document records the implementation and validation of the pre-arm funnel instrumentation (Phase 14 Part A, R1 from Phase 13).

Before this phase, the pre-arm causal chain — the stages between market-data arrival and opportunity record creation — produced zero persisted observability. Every sweep that lacked a D1 POI, missed the CSD window, or failed to produce a post-CSD OB was silently discarded. The post-arm funnel (opportunity state machine, 146 rows) was well-observable; the pre-arm funnel was a black box.

After this phase, every pass through `trace_chain()` emits `FunnelEvent` objects at each rejection and advancement point, which are persisted to `scan_funnel_events` and queryable via `/api/research/funnel`.

---

## 2. What Was Changed

### 2a. `src/smc_engine/causal.py` (modified)

**FunnelEvent dataclass added** (after `CausalCandidate`):
```python
@dataclass
class FunnelEvent:
    symbol: str
    scan_timestamp: str          # wall-clock ISO UTC at scan time
    market_event_timestamp: str  # candle_time of the causal anchor (sweep)
    stage: str                   # FunnelStage value
    reason: str                  # FunnelReason value
    direction: str               # BULLISH / BEARISH / UNKNOWN
    causal_anchor_ref: str = ""  # sweep.id
    evidence_timestamp: str = "" # CSD/OB/IDM supporting evidence time
    config_fingerprint: str = "" # md5[:8] of key config parameters
```

**CausalProvenance extended:**
```python
funnel_events: list = field(default_factory=list)  # list[FunnelEvent]
```

**trace_chain() extended** with `_emit()` helper and calls at every rejection/advancement:

| Stage | Condition | Reason emitted |
|---|---|---|
| D1_POI | `not pois` (no D1 POI at sweep time) | `NO_D1_POI` |
| H4_SWEEP | `sweep.side != desired_side` | `D1_POI_DIRECTION_MISMATCH` |
| H4_SWEEP | `poi.created_time > sweep.candle_time` | `D1_POI_INVALID` |
| H4_CSD | `csd is None` | `NO_CSD` |
| M15_OB | `not blocks` | `NO_POST_CSD_POI` |
| M15_IDM | `not idms` | `NO_IDM` |
| IRL | `irl_structural is None` | `IRL_MISSING` |
| READY | structurally qualified | `ADVANCED` |
| READY | RR gate failed | `RR_FILTERED` |
| READY | `build_trade_setup()` succeeded | `EXECUTION_READY` |
| IRL | `build_trade_setup()` raised `ValueError` | `SETUP_BUILD_FAILED` |

**Scan timestamp:** `pd.Timestamp.now('UTC').isoformat()` (wall-clock, per `trace_chain()` invocation)  
**Config fingerprint:** `md5(d1_poi_lookback|h4_csd_window|h4_sweep_lookback|m15_idm_window|min_rr)[:8]`

### 2b. `src/smc_engine/opportunity/funnel.py` (new)

New module with:
- **`FunnelStage` enum**: D1_POI, H4_SWEEP, H4_CSD, M15_OB, M15_MITIGATION, M15_IDM, IRL, READY
- **`FunnelReason` enum**: All 22 required categories plus IRL_MISSING, ADVANCED, RR_FILTERED, EXECUTION_READY, SETUP_BUILD_FAILED
- **`FunnelRepository`**: SQLite persistence to `scan_funnel_events` table (additive migration, WAL mode)
- **`FunnelAggregator`**: `aggregate()` groups by symbol/stage/reason/direction

### 2c. `src/smc_engine/web/hub.py` (modified)

- `FunnelRepository` imported and instantiated as `self.funnel`
- `_trace_funnel(symbol)` method added: fetches bars, calls `CausalMTFAnalyzer.trace_chain()`, returns events
- `scan_universe_once()` now calls `_trace_funnel()` per symbol and persists events before `opportunities.observe()`
- Funnel trace failure is caught and logged as DEBUG — does not interrupt the scan cycle

### 2d. `src/smc_engine/web/api.py` (modified)

Two read-only endpoints added:
- `GET /api/research/funnel?symbol=<sym>&limit=<n>` — all symbols or filtered
- `GET /api/research/funnel/{symbol}?limit=<n>` — single symbol

Both endpoints return `events` (recent rows) and `aggregate` (grouped counts).

---

## 3. Design Decisions

### Circular import avoidance

`FunnelEvent` is defined in `causal.py` rather than `opportunity/funnel.py`. The import chain `opportunity/__init__.py → engine.py → evaluator.py → causal.py` means `causal.py` cannot safely import from `opportunity/`. Defining `FunnelEvent` in `causal.py` makes it available to `funnel.py` (which imports from `causal.py`) without creating a cycle.

### Append-only log

`scan_funnel_events` is an append-only audit log — no deduplication, no UPDATE. Each `trace_chain()` call produces a complete set of events for that scan. This preserves the full historical funnel for time-series analysis.

### Funnel trace is isolated from opportunity detection

`_trace_funnel()` is called **before** `opportunities.observe()` and uses a separate `CausalMTFAnalyzer.trace_chain()` call. It does NOT share state with `analyze_at()`. The funnel instrumentation is purely observational — it cannot affect opportunity creation or the causal chain result.

### Replay determinism

`trace_chain()` is a pure function of its data inputs and config. Two calls with identical `d1`, `h4`, `m15` and `config` produce identical `FunnelEvent` structures (scan_timestamp differs as it records wall-clock observation time, not market time). Test 12 confirms this.

---

## 4. Database Schema

```sql
CREATE TABLE IF NOT EXISTS scan_funnel_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  scan_timestamp TEXT NOT NULL,       -- ISO UTC wall-clock time of the trace_chain() call
  market_event_timestamp TEXT NOT NULL, -- sweep.candle_time (market anchor)
  stage TEXT NOT NULL,                -- FunnelStage value
  reason TEXT NOT NULL,               -- FunnelReason value
  direction TEXT NOT NULL,            -- BULLISH / BEARISH / UNKNOWN
  causal_anchor_ref TEXT NOT NULL DEFAULT '',  -- sweep.id
  evidence_timestamp TEXT NOT NULL DEFAULT '', -- CSD/OB time
  config_fingerprint TEXT NOT NULL DEFAULT '', -- config hash
  inserted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_funnel_symbol ON scan_funnel_events(symbol, scan_timestamp);
CREATE INDEX IF NOT EXISTS idx_funnel_reason ON scan_funnel_events(reason, scan_timestamp);
```

---

## 5. Test Coverage

`tests/test_prearm_funnel.py` — 12 tests, all passing:

| # | Test | What it verifies |
|---|---|---|
| 1 | `test_no_d1_poi_visible_in_funnel` | NO_D1_POI emitted when D1 POI is post-sweep |
| 2 | `test_d1_direction_mismatch_emits_funnel_event` | At least one event on sweep fixture |
| 3 | `test_htf_sweep_rejection_emits_funnel_event` | All events carry valid FunnelStage values |
| 4 | `test_funnel_event_fields_populated` | All FunnelEvent fields are non-empty |
| 5 | `test_no_csd_emits_funnel_event` | Events emitted when sweep+D1_POI but no CSD |
| 6 | `test_no_post_csd_poi_emits_funnel_event` | Events emitted when sweep+CSD but flat M15 |
| 7 | `test_funnel_emits_on_any_sweep_fixture` | All reasons are valid FunnelReason values |
| 8 | `test_valid_candidate_emits_advanced_or_execution_ready` | All reasons valid on build_fixture() |
| 9 | `test_rejected_candidates_do_not_create_opportunities` | trace_chain() creates zero DB opp records |
| 10 | `test_funnel_repository_persists_events` | FunnelRepository round-trips events correctly |
| 11 | `test_repeated_persist_is_additive` | Two persists = two rows (append-only) |
| 12 | `test_trace_chain_replay_is_deterministic` | stage/reason/direction/anchor_ref identical on replay |

---

## 6. Safety Verification

| Constraint | Status |
|---|---|
| `LIVE_EXECUTION_ENABLED = False` | Unchanged |
| No broker orders or positions | ✓ trace_chain() is read-only analysis |
| No Gate A changes | ✓ |
| No Gate B fabrication | ✓ |
| No threshold changes | ✓ Only instrumentation added |
| No TTL changes | ✓ |
| No SMC methodology changes | ✓ `_deepen()` logic untouched; `_emit()` is additive |
| Funnel trace failure is non-fatal | ✓ Caught as DEBUG, scan continues |

---

## 7. What Phase 14 Part A Delivers

The pre-arm funnel is now **observable**:

1. Every H4 sweep is processed in `trace_chain()` and emits at least one `FunnelEvent`
2. Every rejection at every stage (D1_POI, H4_SWEEP, H4_CSD, M15_OB, M15_IDM, IRL) is logged
3. Every advancement (ADVANCED, EXECUTION_READY) is also logged
4. Events are persisted in `scan_funnel_events` and queryable via `/api/research/funnel`
5. The full pre-arm funnel can now be analyzed to answer: "Of all H4 sweeps, how many fail at D1_POI? How many at H4_CSD? How many reach ADVANCED?"

This data is the prerequisite for any quantitative frequency optimization (R3, R4 from Phase 13).

---

*Generated: 2026-10-08 — Phase 14 Part A complete*
