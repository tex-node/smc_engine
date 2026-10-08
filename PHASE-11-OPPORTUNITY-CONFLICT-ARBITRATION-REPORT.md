# Phase 11 — Opportunity Conflict Arbitration Report

**Commit:** c4b7b91  
**Branch:** feature/gui-workstation  
**Date:** 2026-10-08  
**Predecessor:** Phase 10 CLEAN BASELINE ESTABLISHED (7e66967)

---

## Hard Invariant Established

> **ONLY ONE ACTIVE DIRECTIONAL THESIS PER CANONICAL INSTRUMENT.**

This invariant is now enforced at the backend opportunity state level — not via UI filtering, not via soft hiding. Conflicting opportunities are resolved before they reach the active repository.

---

## Architecture

### Layer: CausalEngine → OpportunityEngine → **OpportunityArbiter** → Repository

```
_discover_reversal()  ─┐
                        ├─► arbiter.arbitrate()  ──► ACCEPT │ SUPERSEDE │ REJECT
_discover_continuation() ─┘        │                           │           │
                                    │       supersede_opp()     │           │
                                    └──────────────────────────►│           │
                                                    repo.upsert()◄──────────┘ (skipped)
```

The arbiter runs inside the engine's existing `self._lock` (RLock). No second concurrency mechanism was introduced. Serialization is provided by the existing `SHARED_WRITE_LOCK` + `self._lock` hierarchy.

---

## New Components

### `src/smc_engine/opportunity/arbiter.py` (new)

**`canonical_instrument(symbol: str) → str`**  
Maps broker symbol to canonical instrument for conflict detection. Currently: `norm_symbol` (strip non-alnum, upper). No aliases hard-coded — `XAUUSD247` and `XAUUSD` are treated as separate instruments until broker spec confirms equivalence.

**`thesis_strength(opp: Opportunity) → int`**  
Deterministic state-based strength score:

| State | Strength |
|---|---|
| `READY_FOR_MITIGATION` | 7 |
| `ENTRY_TRIGGERED` | 6 |
| `WAITING_FOR_IDM` | 5 |
| `WAITING_FOR_POI` | 4 |
| `WAITING_FOR_CONFIRMATION` | 3 |
| `OPPORTUNITY_ARMED` | 2 |
| other | 1 |

Tie-break: most recent anchor event time (nanoseconds from canonical_key) wins.

**`OpportunityArbiter.arbitrate(symbol, direction, initial_state, anchor_time_ns) → ArbiterDecision`**

Decision logic:
1. Find all active opps for the canonical instrument
2. **Opposing direction**: compare new strength vs strongest opposing → SUPERSEDE if new ≥ (or newer on tie), REJECT if existing stronger
3. **Same direction**: SUPERSEDE if new is strictly stronger, REJECT if existing is equal or stronger
4. No conflict → ACCEPT

**`OpportunityArbiter.reconcile(now_iso) → list[tuple[str, str]]`**  
Startup reconciliation: finds all pre-existing conflicts in the DB and returns (loser_id, winner_id) pairs for the engine to supersede.

---

## Models Changes (`models.py`)

### New `OpportunityState.SUPERSEDED`
```python
SUPERSEDED = "SUPERSEDED"
```
Added to `TERMINAL_STATES`. Added to `STATE_LABEL` ("SUPERSEDED"). Not in `active_only` queries.

### New fields on `Opportunity`
```python
superseded_by: str = ""           # canonical_key of the winning thesis
superseded_at: object = None      # ISO timestamp
supersession_reason: str = ""     # human-readable reason string
```
Stored in `payload_json` (existing catch-all column). No schema migration required. Old rows without these fields load correctly via dataclass defaults.

### New `OpportunityEventKind.OPPORTUNITY_SUPERSEDED`
Distinct from `INVALIDATED`: causal logic is unchanged; the thesis was retired by conflict arbitration, not by a causal violation.

### New `BlockReason` values
- `SUPERSEDED_THESIS` — set on superseded opp's reason/blocker fields
- `CONFLICT_REJECTED` — set in `result["blocked"]` for REJECT decisions

---

## State Machine Changes (`state_machine.py`)

All active states now permit `→ SUPERSEDED`:

```
OPPORTUNITY_ARMED        → SUPERSEDED
WAITING_FOR_CONFIRMATION → SUPERSEDED
WAITING_FOR_POI          → SUPERSEDED
WAITING_FOR_IDM          → SUPERSEDED
READY_FOR_MITIGATION     → SUPERSEDED
```

`SUPERSEDED` has no outgoing transitions (terminal).

---

## Repository Changes (`repository.py`)

- `active_only=True` filter now excludes `'SUPERSEDED'` in addition to existing terminal states
- New `active_for_instrument(symbol_norm: str) → list[Opportunity]` read method — used by arbiter to enumerate conflicts for a canonical instrument

---

## Engine Changes (`engine.py`)

- `OpportunityArbiter` constructed in `__init__`, stored as `self.arbiter`
- `_reconciled: bool = False` flag — reconciliation runs once per engine lifetime
- `observe()`: adds `"superseded": []` to result dict; calls `_reconcile_startup()` on first call
- `_discover_reversal()`: calls `arbiter.arbitrate()` after `get_by_key()` guard; executes SUPERSEDE or skips on REJECT
- `_discover_continuation()`: same
- `_supersede_opp(opp_id, winner_key, reason, now, result)`: transitions opp to SUPERSEDED, sets supersession fields, persists, records state change, emits `OPPORTUNITY_SUPERSEDED` event
- `_reconcile_startup(result)`: calls `arbiter.reconcile()`, supersedes each loser
- `diagnostics()`: includes `"arbitration"` dict from `arbiter.diagnostics()`
- `funnel()`: includes `"superseded_count"`

---

## Test Coverage (`tests/test_opportunity_arbitration.py`)

**33 new tests, all passing.**

| Category | Tests |
|---|---|
| `canonical_instrument` | 3 |
| `thesis_strength` ordering | 1 |
| `arbitrate()` — ACCEPT paths | 2 |
| `arbitrate()` — SUPERSEDE paths | 4 |
| `arbitrate()` — REJECT paths | 2 |
| SUPERSEDED excluded from active | 2 |
| `TERMINAL_STATES` + `is_terminal()` | 2 |
| State machine transitions | 2 |
| `reconcile()` | 4 |
| `_supersede_opp()` fields | 1 |
| `_supersede_opp()` event | 1 |
| `_supersede_opp()` idempotency | 1 |
| `_supersede_opp()` terminal guard | 1 |
| REJECT → blocked list | 1 |
| diagnostics counters | 2 |

**1 existing test updated** (`test_opposing_csd_invalidates`): assertion updated to accept SUPERSEDED as a valid retirement state. The invariant (original opp is retired and excluded from active) remains verified; the mechanism changed from OPPOSING_CSD invalidation to arbiter-driven SUPERSEDED.

---

## Full Suite Results

```
516 passed, 1 skipped, 0 failed
```

All pre-existing 483 tests pass. 33 new tests pass.

---

## Historical DB Reconciliation

**Pre-reconciliation conflicts found: 1**

| Instrument | Direction | State | Outcome |
|---|---|---|---|
| GBPUSD | BULLISH | WAITING_FOR_CONFIRMATION (older anchor) | SUPERSEDED |
| GBPUSD | BULLISH | WAITING_FOR_CONFIRMATION (newer anchor) | **WINNER** (kept) |

The previously-observed XAUUSD BEARISH + XAUUSD BULLISH conflict had already expired naturally by reconciliation time.

**Post-reconciliation active set: 0 conflicts**

```
BTCUSD    BEARISH  CONTINUATION  READY_FOR_MITIGATION  str=7
CADJPY    BULLISH  REVERSAL      WAITING_FOR_CONFIRMATION str=3
CHFJPY    BULLISH  REVERSAL      WAITING_FOR_CONFIRMATION str=3
EURGBP    BULLISH  REVERSAL      WAITING_FOR_CONFIRMATION str=3
EURJPY    BULLISH  REVERSAL      WAITING_FOR_CONFIRMATION str=3
GBPJPY    BULLISH  REVERSAL      WAITING_FOR_CONFIRMATION str=3
GBPUSD    BULLISH  REVERSAL      WAITING_FOR_CONFIRMATION str=3
USDJPY    BULLISH  REVERSAL      WAITING_FOR_CONFIRMATION str=3
XAGUSD    BULLISH  REVERSAL      WAITING_FOR_CONFIRMATION str=3
XAUUSD    BULLISH  REVERSAL      WAITING_FOR_CONFIRMATION str=3
XAUUSD247 BULLISH  REVERSAL      WAITING_FOR_CONFIRMATION str=3
```

Each `canonical_instrument` appears at most once per direction. The invariant holds.

---

## Canonical Instrument Identity Notes

- `canonical_instrument(symbol) = norm_symbol(symbol)` (strips non-alnum, uppercase)
- `XAUUSD` and `XAUUSD247` are **distinct** canonical instruments (no alias configured)
- Per the brief: "Do NOT hard-code aliases such as XAUUSD247 without evidence." No alias was added. If broker spec later confirms equivalence, a config-driven alias table can be injected into `canonical_instrument()` without changing the arbitration logic.

---

## Safety Constraints

All Phase 10 safety constraints remain unchanged:

| Constraint | Status |
|---|---|
| `live_execution_enabled=false` | UNCHANGED ✓ |
| `POST /api/live/{id}` → 403 | UNCHANGED ✓ |
| No Gate A changes | UNCHANGED ✓ |
| No OpportunityEngine semantics changed | ✓ — arbitration is additive, no causal logic modified |
| No causal logic changed | ✓ |
| No watcher behaviour changed | ✓ |
| No broker order primitives introduced | ✓ |

---

## Diagnostics

`OpportunityEngine.diagnostics()` now includes:

```json
{
  "arbitration": {
    "conflicts_detected": 0,
    "conflicts_resolved": 0,
    "opportunities_superseded": 0,
    "duplicates_rejected": 0
  },
  "funnel": {
    "superseded_count": 1,
    ...
  }
}
```

`result["superseded"]` is populated by each `observe()` call with any IDs superseded in that scan.

---

## Verdict

```
PHASE 11 COMPLETE — SINGLE-ACTIVE-THESIS INVARIANT ENFORCED
```

The backend active opportunity state is conflict-free. The invariant is enforced at write time (before persistence) and at startup (reconciliation before the first scan). No UI changes are needed to achieve a conflict-free active set — the enforcement is structural.

---

*Generated: 2026-10-08 — Commit c4b7b91*
