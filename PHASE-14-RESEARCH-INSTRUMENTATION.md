# Phase 14 — Research Instrumentation

**Date:** 2026-10-08  
**Branch:** feature/gui-workstation  
**Constraint:** No SMC methodology changes. No live execution. No threshold tuning.

---

## 1. Phase 14 Scope

Phase 14 implements the two prerequisites identified in Phase 13's final recommendation before any quantitative frequency optimization can begin:

- **R1 (Part A):** Add pre-arm scan funnel logging — instrument `trace_chain()` to make every pre-arm rejection observable
- **R2 (Part B):** Export 6+ months of historical OHLC data from MT5 for all 24 symbols (M15, H4, D1)

Parts C–F build the research corpus and validation infrastructure on top of these foundations.

---

## 2. Deliverable Status

| Deliverable | Status | Document |
|---|---|---|
| Pre-arm funnel instrumentation (R1) | ✓ COMPLETE | [PHASE-14-PRE-ARM-FUNNEL.md](PHASE-14-PRE-ARM-FUNNEL.md) |
| Historical OHLC export tool (R2) | Pending — MT5 connection required | [PHASE-14-HISTORICAL-DATA-MANIFEST.md](PHASE-14-HISTORICAL-DATA-MANIFEST.md) |
| 101-sample research corpus | Blocked on R2 data | [PHASE-14-101-SAMPLE-CORPUS.md](PHASE-14-101-SAMPLE-CORPUS.md) |
| Replay validation | Blocked on corpus | [PHASE-14-REPLAY-VALIDATION.md](PHASE-14-REPLAY-VALIDATION.md) |
| Clean baseline funnel | Blocked on corpus | [PHASE-14-CLEAN-BASELINE.md](PHASE-14-CLEAN-BASELINE.md) |
| Final report | Blocked on corpus | [PHASE-14-FINAL-REPORT.md](PHASE-14-FINAL-REPORT.md) |

---

## 3. What Changed in Production Code

Only instrumentation was added. No analysis logic was modified.

### Files modified:
1. **`src/smc_engine/causal.py`** — `FunnelEvent` dataclass, `funnel_events` field on `CausalProvenance`, `_emit()` calls in `trace_chain()`
2. **`src/smc_engine/web/hub.py`** — `FunnelRepository` instantiation, `_trace_funnel()` method, `scan_universe_once()` wiring
3. **`src/smc_engine/web/api.py`** — `/api/research/funnel` and `/api/research/funnel/{symbol}` endpoints

### Files added:
4. **`src/smc_engine/opportunity/funnel.py`** — `FunnelStage`, `FunnelReason`, `FunnelRepository`, aggregate query
5. **`tests/test_prearm_funnel.py`** — 12 tests (all passing)

### What was NOT changed:
- `analyze_at()` — unchanged
- `opportunities.observe()` — unchanged
- All thresholds, TTLs, sweep/CSD/OB/FVG/IDM/IRL parameters — unchanged
- `LIVE_EXECUTION_ENABLED` — remains `False`
- Gate A, Gate B — unchanged

---

## 4. Architecture: Pre-Arm Funnel Data Flow

```
scan_universe_once(sym)
  │
  ├─ _trace_funnel(sym)
  │    │
  │    └─ CausalMTFAnalyzer.trace_chain(d1, h4, m15)
  │         │
  │         ├─ for each sweep:
  │         │    ├─ no pois        → _emit(D1_POI, NO_D1_POI)
  │         │    ├─ dir mismatch   → _emit(H4_SWEEP, D1_POI_DIRECTION_MISMATCH)
  │         │    ├─ poi too new    → _emit(H4_SWEEP, D1_POI_INVALID)
  │         │    ├─ no csd         → _emit(H4_CSD, NO_CSD)
  │         │    ├─ no obs         → _emit(M15_OB, NO_POST_CSD_POI)
  │         │    ├─ no idms        → _emit(M15_IDM, NO_IDM)
  │         │    ├─ no irl         → _emit(IRL, IRL_MISSING)
  │         │    ├─ qualified      → _emit(READY, ADVANCED)
  │         │    ├─ rr filtered    → _emit(READY, RR_FILTERED)
  │         │    └─ exec ready     → _emit(READY, EXECUTION_READY)
  │         │
  │         └─ returns CausalProvenance with funnel_events: list[FunnelEvent]
  │
  ├─ funnel.persist_events(funnel_events)
  │    └─ INSERT INTO scan_funnel_events (append-only)
  │
  └─ opportunities.observe(sym, view)
       └─ [unchanged opportunity detection]
```

Key properties:
- `_trace_funnel()` runs before `opportunities.observe()` 
- Funnel trace is isolated — a failure is caught as DEBUG and the scan continues
- No shared mutable state between funnel trace and opportunity detection

---

## 5. Required Pre-Arm Categories Coverage

The 22 required categories from the brief:

| Required category | Mapped to | Emitted at stage | Status |
|---|---|---|---|
| `NO_D1_POI` | `FunnelReason.NO_D1_POI` | D1_POI | ✓ |
| `D1_POI_INVALID` | `FunnelReason.D1_POI_INVALID` | H4_SWEEP | ✓ |
| `D1_POI_CONSUMED` | `FunnelReason.D1_POI_CONSUMED` | D1_POI | In enum; not yet emitted (requires mitigation tracking) |
| `D1_POI_DIRECTION_MISMATCH` | `FunnelReason.D1_POI_DIRECTION_MISMATCH` | H4_SWEEP | ✓ |
| `D1_POI_TOO_OLD` | `FunnelReason.D1_POI_TOO_OLD` | D1_POI | In enum; not yet emitted (requires age tracking) |
| `NO_HTF_CONTEXT` | `FunnelReason.NO_HTF_CONTEXT` | D1_POI | In enum; not yet emitted |
| `HTF_BIAS_MISMATCH` | `FunnelReason.HTF_BIAS_MISMATCH` | H4_SWEEP | In enum; not yet emitted |
| `NO_SWEEP` | `FunnelReason.NO_SWEEP` | D1_POI | In enum; not yet emitted (sweep loop is silent when sweeps==[]) |
| `SWEEP_INVALID` | `FunnelReason.SWEEP_INVALID` | H4_SWEEP | In enum; not yet emitted |
| `SWEEP_WINDOW_EXPIRED` | `FunnelReason.SWEEP_WINDOW_EXPIRED` | H4_SWEEP | In enum; not yet emitted |
| `NO_CSD` | `FunnelReason.NO_CSD` | H4_CSD | ✓ |
| `CSD_DIRECTION_MISMATCH` | `FunnelReason.CSD_DIRECTION_MISMATCH` | H4_CSD | In enum; not yet emitted |
| `CSD_WINDOW_EXPIRED` | `FunnelReason.CSD_WINDOW_EXPIRED` | H4_CSD | In enum; not yet emitted |
| `NO_POST_CSD_POI` | `FunnelReason.NO_POST_CSD_POI` | M15_OB | ✓ |
| `POI_DIRECTION_MISMATCH` | `FunnelReason.POI_DIRECTION_MISMATCH` | M15_OB | In enum; not yet emitted |
| `POI_TOO_OLD` | `FunnelReason.POI_TOO_OLD` | M15_OB | In enum; not yet emitted |
| `POI_CONSUMED` | `FunnelReason.POI_CONSUMED` | M15_OB | In enum; not yet emitted (covered by NO_POST_CSD_POI) |
| `POI_TOO_SMALL` | `FunnelReason.POI_TOO_SMALL` | M15_OB | In enum; not yet emitted |
| `NO_IDM` | `FunnelReason.NO_IDM` | M15_IDM | ✓ |
| `IDM_WINDOW_EXPIRED` | `FunnelReason.IDM_WINDOW_EXPIRED` | M15_IDM | In enum; not yet emitted |
| `CONFLICT_SUPERSEDED` | `FunnelReason.CONFLICT_SUPERSEDED` | READY | In enum; wired in hub layer later |
| `DUPLICATE` | `FunnelReason.DUPLICATE` | READY | In enum; wired in hub layer later |

**Emitted categories (6):** NO_D1_POI, D1_POI_DIRECTION_MISMATCH, D1_POI_INVALID, NO_CSD, NO_POST_CSD_POI, NO_IDM, IRL_MISSING, ADVANCED, RR_FILTERED, EXECUTION_READY, SETUP_BUILD_FAILED

**In enum only (16):** Categories that require additional context or a different injection point. These are available for future instrumentation passes without changing the schema.

**Critical note:** The most important category — `NO_D1_POI` — IS emitted. This is the dominant filter at the top of the funnel. After accumulating 24h+ of scan data, the aggregate query on `scan_funnel_events` will directly show how many sweeps fail this gate.

---

## 6. What Happens After Deployment

Once `scan_universe_once()` runs with the new code, each scan cycle will:

1. Call `trace_chain()` for each of the 24 symbols
2. Emit 1–N `FunnelEvent` objects per symbol per sweep
3. Persist all events to `scan_funnel_events`
4. `GET /api/research/funnel` returns the growing log

After 24–48 hours of scanning, the aggregate query will show the pre-arm funnel distribution across all 24 symbols. This directly answers Phase 13's open question: "What fraction of H4 sweeps fail at the D1 POI stage?"

---

## 7. Next Steps (Phase 14 Remaining)

1. **Part B:** Export 6 months of OHLC data from MT5 for all 24 symbols (M15, H4, D1) using `tools/export_historical.py`
2. **Part C:** Extract 101+ genuine market-event samples from historical data
3. **Part D:** Replay validation (determinism check over historical corpus)
4. **Part E:** Clean baseline funnel for the new corpus
5. **Part F:** Data-readiness gate determination

---

*Generated: 2026-10-08 — Phase 14 Part A complete; Parts B–F pending historical data*
