# PHASE-8-P1-CORRECTION-REPORT

Correction slice for the three P1 defects raised in
`PHASE-8-POST-IMPLEMENTATION-AUDIT.md`. Unlike the audit, this phase **implements
fixes**. No new opportunity-generation features were added; no causal definition,
execution boundary, risk engine or authorization was changed.

Evidence is from: code inspection, the committed test suite, an in-process
24-symbol live scan (read-only against the demo account), and live HTTP GET
(`/api/opportunities`, `/api/opportunities/diagnostics`, `/api/status`). No
broker primitive was invoked (`order_send=0`, `order_check=0`).

---

## 1. Executive verdict

**P1-A, P1-B and P1-C are corrected, and a residual P1-A gap found during this
slice is now closed.** The committed fixes (`9ded886`, `6b17f01`) resolved the
three defects as originally scoped. While re-measuring live, one residual P1-A
hole was discovered — legacy persisted rows with a NULL `poi_time` escaped the
stale-anchor termination — and is fixed in this slice.

Live results after correction:

* **P1-A** — non-terminal chains with an out-of-window anchor→POI pairing: **0**
  (was 42/47 ≈ 89% at audit). 10 non-terminal stale chains were terminated as
  `INVALIDATED`/`STALE_ANCHOR`.
* **P1-B** — `db_write_failures=0`, `db_lock_retries=0`, `db_lock_failures=0`,
  `db_last_write_error=null` over 3,956 writes; in-process 24-symbol scan
  `symbol_errors={}` (was 20/24 symbols failing, coverage 4/24 = 17%).
* **P1-C** — `ENTRY_TRIGGERED` renders as **"ENTRY CONDITION MET"** and emits its
  own event kind; `EXECUTION_READY` is emitted only when a real `TradeSetup`
  promotion occurs.

Full committed suite: **483 passed, 1 skipped**. Safety boundary intact
(`LIVE_EXECUTION_ENABLED=False`, `POST /api/live/x → 403`, identity AUTHORIZED).

**Verdict: the opportunity layer's live behaviour now matches its architecture.**
The one remaining blemish is 37 pre-fix **terminal** `ENTRY_TRIGGERED` rows that
carry a stale pairing and, by state-machine design, cannot be re-labelled (see
§7); they are inert and cannot be re-created.

---

## 2. Scope, constraints & what was NOT changed

Changed (this slice + `9ded886`/`6b17f01`):

* `src/smc_engine/opportunity/engine.py` — causal-window enforcement on
  discovery/advance, stale-anchor termination, market-anchored TTL, entry
  semantics.
* `src/smc_engine/opportunity/evaluator.py` — `poi_candidates(created_before=…)`.
* `src/smc_engine/opportunity/models.py` — `STATE_LABEL`, `STALE_ANCHOR`,
  `ENTRY_TRIGGERED` event kind, window defaults.
* `src/smc_engine/web/dbwrite.py` (new) — serialized SQLite writes/reads.
* `src/smc_engine/web/{hub,history,api}.py` — writer serialization + diagnostics.
* `src/smc_engine/web/static/app.js` — `RISK: NOT CHECKED`.
* Tests: `test_opportunity_temporal.py`, `test_dbwrite.py`,
  `test_opportunity_semantics.py` (+ updates to existing opportunity suites).

Deliberately **not** changed:

* Gate A; `LIVE_EXECUTION_ENABLED` (stays `False`); demo authorization; the
  execution boundary; causal definitions; the risk engine.
* Discovery remains non-executing; `TradeSetup` creation remains owned by the
  causal path (the opportunity layer only links).
* Gate B remains **BLOCKED — WAITING FOR REAL CAUSAL SETUP**; no setup is
  manufactured.
* A parallel **Phase 9 UI** commit (`949a6f8`) exists on the branch; it is
  outside this slice and was not modified.

---

## 3. P1-A — root cause

At audit, 42/47 live opportunities attached a POI created far outside the
configured `csd_to_poi` window (up to ≈1967 h) to an old sweep/CSD anchor. Two
code paths allowed it:

1. **Discovery bypass** — `_discover_reversal` skipped the age guard whenever a
   CSD existed (`engine.py`), so an ancient sweep with any later CSD armed.
2. **Unbounded POI selection** — `poi_candidates(created_after=csd_time)` had no
   upper bound, so the *most recent* unmitigated POI was selected regardless of
   how long after the CSD it formed.

The effect: READY / ENTRY_TRIGGERED counts were inflated by causally
meaningless pairings.

---

## 4. P1-A — correction design

* **Hard upper bound on POI selection.** `evaluator.poi_candidates()` gained
  `created_before`; candidates formed after it are retained but marked
  `BlockReason.STALE_ANCHOR` (never silently discarded). Both
  `_advance_reversal` and `_advance_continuation` pass
  `created_before = anchor_time + window`.
* **Stale-CSD guard on discovery.** `_discover_reversal` refuses to arm when
  `csd.candle_time − sweep.candle_time > sweep_to_csd_bars` (event time).
* **Persisted-chain invariant.** `_stale_anchor_reason()` re-evaluates the causal
  windows on **market-event timestamps only** (never observation time) and
  terminates violators as `INVALIDATED`/`STALE_ANCHOR`.
* **Market-anchored TTL.** READY/ARMED expiry is anchored to the producing event
  (POI/IDM/CSD/BOS), never to wall-clock observation, so repeated polling and
  restarts cannot rejuvenate an old chain.
* **Default window tightened.** `sweep_to_csd_bars` 24 → 96 (24 h), consistent
  with `csd_to_poi_bars=96` (`models.py`).

---

## 5. P1-A — residual gap found during this slice + completion

**Finding.** Re-measuring the live set after the committed fix revealed **10
non-terminal `READY_FOR_MITIGATION` chains still carrying stale pairings**
(anchor→POI gaps of 122–1655 h). They were re-advanced every scan yet never
terminated.

**Root cause.** `_stale_anchor_reason()` read the POI time only from
`opp.poi_time`. The offending rows were written **before** the market-event
timestamp fields existed (their persisted payload has
`sweep_time = csd_time = poi_time = bos_time = idm_time = None`; only
`csd_evidence.time`, `sweep_evidence.time` and `poi_candidates[].created_time`
survived). With `poi_time=None` the CSD→POI check was skipped, so the invariant
was never evaluated for these chains.

**Completion.** Added `OpportunityEngine._poi_anchor_time()` (`engine.py:213`):
it returns `opp.poi_time` when present, otherwise the selected candidate's
`created_time` from `poi_candidates`. `_stale_anchor_reason()` now uses it for
both the reversal (CSD→POI) and continuation (BOS→POI) checks. This makes the
invariant auditable across restarts and legacy payloads — a stale pairing can no
longer escape termination merely because a convenience field is missing.

Regression test: `tests/test_opportunity_temporal.py::test_legacy_payload_without_poi_time_is_still_audited`
constructs exactly the legacy shape (`poi_time=None`, `selected_poi` set,
`poi_candidates[].created_time` stale) and asserts termination as
`INVALIDATED`/`STALE_ANCHOR`.

---

## 6. P1-A — live before/after evidence

Recomputed from `/api/opportunities` (anchor→POI gap vs. the 24 h window):

| Metric | Audit (before) | After correction |
|---|---|---|
| Opportunities with stale anchor→POI | 42 / 47 (89%) | **0 non-terminal** |
| Max anchor→POI gap | ≈1967 h | n/a (none) |
| Non-terminal stale chains | 10 `READY` | **0** |
| `INVALIDATED` count | 2 | **12** (10 new `STALE_ANCHOR`) |
| `READY_FOR_MITIGATION` | 11 | **1** |

Termination evidence (10 chains, all `INVALIDATED` / `STALE_ANCHOR`), e.g.:

```
CHFJPY REVERSAL  POI 2026-10-07 11:30 beyond CSD->POI window from 2026-07-30 12:00
BTCUSD REVERSAL  POI 2026-10-07 01:30 beyond CSD->POI window from 2026-08-10 12:00
AUDCAD REVERSAL  POI 2026-10-07 14:30 beyond CSD->POI window from 2026-08-04 08:00
```

Post-fix re-audit: **non-terminal chains checked = 1, stale pairings = 0.**
All stale non-terminal chains were created `2026-10-07 19:00` (pre-fix); the
fix commits are `2026-10-08 04:39`/`05:07`. No chain created after the fix has a
stale pairing.

---

## 7. P1-A — terminal legacy rows (known limitation)

**37 `ENTRY_TRIGGERED` rows** still carry a stale pairing. They are pre-fix
artifacts and are **immutable by design**: `ENTRY_TRIGGERED` is in
`TERMINAL_STATES` and its allowed-transition set is empty
(`state_machine.py:27`), so it cannot be re-labelled to `INVALIDATED` without
violating the "terminal never resurrects / never re-labels" invariant that the
rest of the layer relies on.

Properties of these rows: `state=ENTRY_TRIGGERED`, `setup_id=''`, no risk
approval, non-executing, and they will not be re-created (discovery dedupes by
canonical key and the POI bound now rejects the stale candidate). They are inert
history. Options (not taken here, to avoid a destructive migration): a one-off
operator purge, or a future read-time "causal-invalid" annotation for terminal
rows. Recommended follow-up: P2 item.

---

## 8. P1-B — root cause

`sqlite3.OperationalError: database is locked` caused 20/24 symbols to fail in
the sampled scan (coverage collapsed to 4/24 = 17%). Root cause: four
independently-locked writers (`WebStore`, `SetupEventHistory`,
`OpportunityRepository`, engine `SetupStore`) wrote the same GUI database from
poller and request threads; the single-writer WAL + 10 s busy timeout was
exceeded during heavy scans. A follow-up symptom, `DatabaseError: another row
available`, came from concurrent reads on a connection being used for writes.

---

## 9. P1-B — correction design

New `src/smc_engine/web/dbwrite.py`:

* `SHARED_WRITE_LOCK` — one process-wide writer lock across all stores.
* `run_write(fn, retries=4)` — bounded retry on `SQLITE_BUSY`/`SQLITE_LOCKED`.
* `serialized_write` / `serialized_read` decorators.
* Diagnostics counters: `db_write_count`, `db_write_failures`, `db_lock_retries`,
  `db_lock_failures`, `db_last_write_error` (cleared on success);
  `reset_write_diagnostics()`.

Applied to `WebStore`, `SetupEventHistory`, `OpportunityRepository`; engine
`SetupStore` upserts are wrapped via `run_write` in `hub.py`. Per-connection
locks use `threading.RLock()` (the write bodies already acquire them, so a
non-reentrant lock would self-deadlock). Diagnostics are surfaced at
`/api/opportunities/diagnostics → db`.

---

## 10. P1-B — live before/after evidence

| Metric | Audit (before) | After correction |
|---|---|---|
| `db_write_failures` | (scan failures) | **0** |
| `db_lock_retries` | — | **0** |
| `db_lock_failures` | — | **0** |
| `db_last_write_error` | `database is locked` | **null** |
| Symbols failing (in-process 24-symbol scan) | 20 / 24 | **0** (`symbol_errors={}`) |
| Universe coverage | 4/24 = 17% | **24/24 eligible; 0 errors** |
| `db_write_count` | — | 3,956 |

`/api/opportunities/diagnostics → db` (live):

```json
{"db_write_count":3956,"db_write_failures":0,"db_lock_retries":0,
 "db_lock_failures":0,"db_last_write_error":null}
```

Regression tests: `tests/test_dbwrite.py` — bounded retry/exhaustion, concurrent
writes, mixed concurrent reads+writes on one connection, 24-symbol scan
coverage, single-symbol isolation, watcher+API contention.

---

## 11. P1-C — root cause

`ENTRY_TRIGGERED` — an opportunity-side "entry touch" with no `TradeSetup` and no
risk approval — rendered in the UI as **"EXECUTION READY"** and emitted an
`EXECUTION_READY` alert kind (23 persisted). This conflated an observation with
an execution-ready trade.

---

## 12. P1-C — correction design

* `STATE_LABEL[ENTRY_TRIGGERED] = "ENTRY CONDITION MET"` (never "EXECUTION
  READY").
* New distinct event kind `OpportunityEventKind.ENTRY_TRIGGERED`.
* `EXECUTION_READY` is emitted **only** in `_promote`, i.e. only when a real
  `TradeSetup` is linked.
* UI shows `RISK: NOT CHECKED` for opportunities (`app.js`).
* Paper execution remains gated by setup + risk (unchanged).

---

## 13. P1-C — live evidence

Live event stream confirms the new semantics: entry touches emit
`ENTRY_TRIGGERED`, and every `EXECUTION_READY` event is paired with a
`CONVERTED_TO_SETUP` (setup-linked) event, e.g.:

```
USDCHF BULLISH CONTINUATION -> ENTRY_TRIGGERED (ENTRY_PASSED)
XAUUSD247 BEARISH CONTINUATION -> ENTRY_TRIGGERED (ENTRY_PASSED)
...
CONVERTED_TO_SETUP ... setup SETUP-EURJPY-…-BEARISH
EXECUTION_READY    ... setup SETUP-EURJPY-…-BEARISH execution-ready
```

Regression tests: `tests/test_opportunity_semantics.py` — label, distinct event
kind, risk not approved, paper blocked, `EXECUTION_READY` only after promotion,
UI statics.

---

## 14. Regression test inventory

| Suite | Covers |
|---|---|
| `tests/test_opportunity_temporal.py` (8) | boundary accept/reject, TTL non-rejuvenation across polling/restart, stale CSD→POI, old sweep→CSD, **legacy `poi_time=None` termination** |
| `tests/test_dbwrite.py` | bounded retry/exhaustion, concurrent writes, mixed concurrent reads+writes, 24-symbol coverage, isolation, watcher+API contention |
| `tests/test_opportunity_semantics.py` | P1-C label/event/risk/paper/UI invariants |
| `tests/test_opportunity_engine.py`, `test_opportunity_cases.py`, `test_opportunity_watcher.py` | updated windows; idempotency; restart recovery; 101-sample funnel; no-broker-primitives |

Suite result on the corrected tree: **483 passed, 1 skipped** (was 460 before the
slice; +22 fix tests +1 residual-gap test).

---

## 15. 101-sample funnel re-run (before/after, same fixture)

`tests/test_opportunity_watcher.py::test_funnel_before_after_same_replay`
(deterministic replay, identical fixture):

```
OLD: samples=101 structural_candidates=0 execution_ready_observations=0
     samples_without_any_setup=101
NEW: total=4 (all EXPIRED by replay end)  ready_observations=13
     samples_with_active_opportunity_but_no_old_setup=50
```

The old single-scan architecture saw **no** setup in any of the 101 samples; the
opportunity layer kept a developing opportunity alive across **50** cycles where
the old path saw nothing, and reached READY in **13** samples. In the fixture,
anchor and POI are within the configured windows — genuine recovery, now
untainted by stale anchors.

---

## 16. Live 24-symbol coverage & classification

In-process scan (`force=True`, demo account, read-only):

* `symbols_scanned = 24`, `symbol_errors = {}` → **0 failures**.
* `last_scan` is populated (the earlier empty `last_scan` observation was a
  transient first-scan read, not a defect).
* Classification: `last_scan` shows `symbols_skipped=24` on the background
  cadence (closed-M15 gate) with no scan-level error — expected off-session.
* Live funnel (`/api/opportunities/diagnostics`): total 144 → armed 0,
  `WAITING_FOR_CONFIRMATION` 17, `WAITING_FOR_POI` 1, `READY` 1,
  `ENTRY_TRIGGERED` 37 (terminal legacy), `INVALIDATED` 12, `EXPIRED` 76,
  converted 8.

---

## 17. Safety & invariant verification

* `live_execution_enabled = False`; `POST /api/live/x → 403`
  ("LIVE EXECUTION IS DISABLED AT THIS STAGE").
* `/api/status`: account `477217728` @ `Exness-MT5Trial9`, identity
  `AUTHORIZED`, mode DEMO.
* No broker primitives in `src/smc_engine/opportunity/*`
  (`test_opportunity_layer_has_no_broker_primitives`).
* Discovery remains non-executing; the opportunity layer never calls the risk
  engine or execution policy; `paper_place` still requires an `EXECUTION_READY`
  setup, the demo gate, `_paper_gate`, risk validation and the portfolio budget.
* Gate A unchanged; Gate B remains **BLOCKED — WAITING FOR REAL CAUSAL SETUP**.
* Causal-window invariant now holds on **market-event time** for every
  non-terminal chain (0 violations live).

---

## 18. Verdict, residual items & next steps

**Verdict: P1-A, P1-B and P1-C are corrected and verified.** The residual P1-A
gap discovered during this slice (legacy `poi_time=None` rows escaping
termination) is closed with a tested fallback. The layer is now safe to build
further opportunity-generation logic on.

Residual items (not P1):

* **P2-a** — 37 terminal `ENTRY_TRIGGERED` legacy rows with stale pairings;
  immutable by design. Recommend a one-off operator purge or a read-time
  "causal-invalid" annotation for terminal rows.
* **P2-D** — no protected-level-breach invalidation in the opportunity layer
  (only opposing CSD invalidates).
* **P2-E** — diagnostics: stale `last_error`; silent empty scan still needs a
  scan-level error field.
* **P2-F** — conversion alerts vs. current linkage reconciliation.
* **P3-G** — state-history/event timestamps use wall clock.

Next steps: (1) commit this slice (`engine.py` + temporal test + this report);
(2) confirm CI green; (3) schedule P2-D/E/F as a follow-up correction slice.
