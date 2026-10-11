# PHASE-16 — RECOVERY AND SIGNAL-QUALITY AUDIT

Repository: `tex-node/smc_engine` · Branch: `feature/gui-workstation`
HEAD: `46b671a` (pushed; CI `38047357773` success). Working tree: uncommitted (this phase).

Two independent gates: **Workstream A — recovery reliability** and
**Workstream B — signal quality**. A successful recovery fix does not imply
signal quality (and vice-versa). No live execution, no MT5/Varis change, no
artificial Telegram signals.

---

## 1. Incident timeline and confirmed root cause

**Incident:** the SMC Engine stopped over a weekend; after restart the monitored
opportunities appeared gone and the visible notifications were for expired
opportunities.

**Calendar (confirmed):** `2026-10-09` = Friday, `2026-10-10` = Saturday,
`2026-10-11` = Sunday. The FX market closed Friday ≈20:30 UTC and reopens Sunday
≈21:00 UTC. The last closed M15 the engine saw is `2026-10-09 20:30`.

**Confirmed root cause (code-level + reproduced):**
`EngineHub.scan_universe_once` runs a TTL sweep
`self.opportunities.expire_cycle(now=data_now)`, where `data_now` is the latest
closed market-data time — but it is **`None` whenever no symbol produced data**
(all symbols skipped by the closed-M15 gate, closed market, or MT5 not ready).
`OpportunityEngine.expire_cycle(now=None)` then falls back to the **wall clock**
(`_now()`), so the sweep compares each opportunity's market-anchored
`expires_at` against the wall clock and **expires still-monitored opportunities
by wall-clock time**. Over the Fri→Sun closed market, this expired Friday's
persisted opportunities — matching the reported symptom. This violates the
recovery rule "a restart / closed market is not an opportunity-expiry reason".

**Reproduction:** an opportunity with `expires_at = 2026-10-10` (Saturday) is
`EXPIRED` by `expire_cycle(now=None)` (wall clock) but remains
`READY_FOR_MITIGATION` under `expire_cycle(now = market time 2026-10-09 20:30)`.
Since 10/10 was a Saturday, market time could not have advanced past 10/09 20:30,
so wall-clock is the only mechanism that could have expired those rows.

**Unresolved alternatives (checked and not supported):** data was **not**
deleted; the DB is **not** corrupted; the running instance did **not** open the
wrong database (single repo DB, §2); the mass `EXPIRED` rows are historical
(§3), not a restart artifact.

---

## 2. Database identity / process state / restart reconciliation (evidence)

* **Running instance (read-only capture):** PID 37192 (child of `.venv` python
  31556), started `2026-10-11T05:53:27Z`, listening on `127.0.0.1:8765`.
* **`/api/runtime`:** `git_commit=46b671a`, **`loaded_git_commit=46b671a`**,
  **`stale_code=False`**, `files_modified_after_startup={}` → not stale code.
* **Canonical DB path:** `build_hub` → `db_dir = os.environ.get("SMC_GUI_DB_DIR",".")`
  → `smc_engine_gui.sqlite3`. `SMC_GUI_DB_DIR` is unset; the server runs from
  `C:\smc_engine`, so the canonical DB is `C:\smc_engine\smc_engine_gui.sqlite3`
  (17 MB, WAL 4.2 MB active). Only one such DB exists on disk → no
  dev/test/prod confusion, no wrong-DB read.
* **Config-fallback risk (now surfaced):** because `db_dir` defaults to `.`, a
  server started from a different working directory would silently use/create a
  different DB. This is now reported (§4) rather than silent.
* **Restart reconciliation:** startup produced **no burst** of expiry/
  supersession — only a handful of events at `10/11 05:xx` (1 `OPPORTUNITY_CREATED`,
  1 `POI_FOUND`, 1 `READY`, 1 `EXPIRED`). The bulk of `EXPIRED` rows are dated
  `10/08–10/09` (historical).

---

## 3. Opportunity counts and state transitions (before/after)

Snapshot of the canonical DB at investigation (copy):

| State | Count |
|---|---|
| EXPIRED | 114 |
| ENTRY_TRIGGERED (terminal) | 51 |
| INVALIDATED | 12 |
| SUPERSEDED | 8 |
| READY / WAITING_* / ARMED | **0** |
| **Total** | **185** |

Event totals: `EXPIRED 835`, `OPPORTUNITY_CREATED 185`, `READY 72`,
`POI_FOUND 72`, `ENTRY_TRIGGERED 65`, `EXECUTION_READY 64`, `IDM_CONFIRMED 44`,
`CONVERTED_TO_SETUP 42`, `INVALIDATED 12`, `OPPORTUNITY_SUPERSEDED 9`.

`EXPIRED` events by date: `10/08: 466`, `10/09: 357`, `10/10: 11`, `10/11: 1`.
The `10/08–10/09` bulk is the **historical pre-Phase-14C duplicate-emission**
artifact (multiple `EXPIRED` rows per opportunity with distinct wall-clock
`evidence_key`s), not a restart effect. The `10/10–10/11` rows (~12) are the
weekend **wall-clock sweep** expiring Friday's still-monitored opportunities
(the defect in §1).

**State changes explained:** (a) historical duplicate `EXPIRED` rows
(10/08–10/09); (b) ~12 weekend wall-clock expiries (the defect); (c) 51 terminal
`ENTRY_TRIGGERED` ("entry condition met"); (d) 12 `INVALIDATED` + 8 `SUPERSEDED`
from causal invalidation/startup arbitration. **The fix does not rewrite any
history** — it prevents *future* wall-clock expiry.

---

## 4. Code changes and crash-recovery guarantees

`src/smc_engine/web/hub.py`:
* **Global TTL sweep removed; expiry is per-symbol and market-anchored.** The
  Phase 16 fix first guarded `expire_cycle(now=data_now)` against the wall-clock
  fallback; the Phase 16A review showed `data_now` was a **global** max across
  scanned symbols (so one trading instrument could expire a closed instrument's
  opportunities). The sweep call and its `data_now` accumulator are therefore
  **removed**. Expiry now happens only in `OpportunityEngine._expire_symbol()`,
  which runs inside `observe()` for each scanned symbol using **that symbol's own
  last closed M15 time**. A symbol whose market has not advanced keeps its
  opportunities active; the next fresh scan reconciles them.
* **Recovery/DB-identity diagnostics.** `EngineHub` captures the canonical
  `db_path` (absolute) and `db_existed_at_startup` **before** the store opens the
  file, plus persisted counts by state at startup; `runtime_info()` exposes a
  `recovery` block (`db_path_basename`, `db_existed_at_startup`,
  `db_initialized_empty`, `counts_at_startup`, `counts_current`). A missing DB
  emits a prominent **WARNING** and is flagged `db_initialized_empty=True`.

Guarantees: persisted opportunity state/history are authoritative across
restarts; rehydration precedes dispatch; schema/DB identity is surfaced; startup
reconciliation is idempotent; durable event identity + Telegram dedup unchanged;
genuinely new events still deliver; no broad state-machine rewrite.

### Expiry semantics (Phase 16A review, code-level answers)

`data_now` was **global**: `hub.py` computed `max(last-closed-M15)` across scanned
symbols. Consequences:

1. **Q1 — per-symbol or global?** Global (`max` across symbols).
2. **Q2 — can a trading instrument advance a closed instrument's clock?** Yes;
   `expire_cycle(symbol=None)` iterated **all** active opportunities against the
   global time. **Confirmed defect** (reproduced: a `XAUUSD` opp with
   `expires_at=2026-01-21` is `EXPIRED` by a global sweep at `2026-01-26`).
3. **Q3 — stale candle as current time?** `data_now` was the last **closed** M15
   (never future). A delayed/stale candle makes the clock *older* → under-expiry,
   not over-expiry; no future-leak risk.
4. **Q4 — outage leaves stale opps healthy indefinitely?** With the sweep removed,
   during an MT5 outage no symbol is scanned and the market time does not advance,
   so opportunities correctly stay active. Their *health* is separate (§3/Q5).
5. **Q5 — degradation shown separately?** Market-data health is exposed by
   `/api/status.market_data` (and the UI health chip) independently of the
   opportunity lifecycle state.
6. **Q6 — post-outage reconciliation?** On the next fresh scan each symbol runs
   `_expire_symbol()` with the new market time, expiring genuinely-expired
   opportunities. Verified by `test_per_symbol_expiry_uses_that_symbols_market_time`.


---

## 5. Recovery regression tests (`tests/test_recovery.py`, 10 tests)

1. `test_closed_market_does_not_wall_clock_expire` — **the root-cause
   regression**: a no-data scan must not expire a persisted opp (fails on the
   pre-fix code; passes now).
2. `test_market_time_anchors_expiry_not_wall_clock` — expiry uses market time;
   past-expiry market time legitimately expires.
3. `test_restart_recovers_active_opportunity` — persisted active state survives a
   restart (same DB).
4. `test_recovery_from_other_working_directory` — canonical absolute DB path
   recovers regardless of cwd.
5. `test_missing_db_flags_empty_initialization` — a non-existent DB is surfaced
   (`db_initialized_empty=True`), not silently healthy.
6. `test_existing_db_reported_as_recovered` — an existing DB reports
   `db_existed_at_startup=True`.
7. `test_startup_reconciliation_is_idempotent` — supersession count stable across
   repeated restarts.
8. `test_no_global_sweep_expires_a_closed_instrument` — **Phase 16A Q2
   regression**: a trading instrument (GBPUSD) must not expire a closed
   instrument's (XAUUSD) opportunity (fails on the pre-review global-sweep code).
9. `test_per_symbol_expiry_uses_that_symbols_market_time` — expiry compares a
   symbol's TTL against that symbol's own market time (before → active, after →
   expired).
10. `test_abrupt_termination_preserves_opportunity_state` — a **killed** child
    process that committed an opportunity leaves it intact on reopen (WAL
    durability), not just a clean restart.

Existing suites already cover restart no-resend, concurrent claim, distinct
lifecycle delivery, and abort-safe SQLite (WAL) durability. All fixtures use
disposable DBs; the production DB was never crashed/corrupted.

---

## 5b. Startup reconciliation and notification burst (Phase 16A review)

The earlier controlled run produced **8 `OPPORTUNITY_SUPERSEDED`** transitions at
startup. Explanation:

* **Why:** the persisted DB was maintained by a **pre-Phase-11** server that did
  not enforce the single-active-thesis invariant, so it held **genuine
  pre-existing opposing theses** per instrument. `_reconcile_startup()` →
  `arbiter.reconcile()` resolves those conflicts once per engine lifetime.
* **Genuine, not rebuild artifacts:** the arbiter reads **persisted active
  opportunities**; it does not rebuild state, and it only supersedes real
  conflicts (strongest thesis per direction wins).
* **Idempotent across restarts:** `_reconcile_startup` runs once
  (`_reconciled` flag) and losers are already `SUPERSEDED`, so
  `_supersede_opp` early-returns → **no new transitions**. Verified by
  `test_startup_reconciliation_is_idempotent`.
* **Notification burst:** on the *first* startup that resolves N conflicts, N
  `OPPORTUNITY_SUPERSEDED` notifications are sent (a real lifecycle event, not
  suppressed); on every later restart, **0**. This is a one-time burst, not a
  recurring one. No restart-induced expiry/supersession was added.


---

## 6. Dataset inventory and causal replay methodology

**Inventory result: no historical OHLC dataset exists in the repository.** A
recursive scan for `*.csv/*.parquet/*.feather/*.h5/*.pkl` data files returns
nothing; there is no `data/` directory. The engine obtains OHLC **only** from the
live MT5 terminal (`MT5Source.bars` → `copy_rates_from_pos`) at request time.
Research surfaces exist (`scan_funnel_events`, `FunnelRepository`,
`/api/research/funnel`, `tools/historical_rr_replay.py`) but operate on live/
synthetic fixtures, not a stored multi-month dataset.

`PHASE-13-FINAL-RECOMMENDATION.md` (R2, HIGH CONFIDENCE) already states the
dataset does not exist and must be exported: "Use MT5 `copy_rates_from()` to
export 6 months of D1, H4, and M15 data for all 24 symbols … Without it, only
qualitative analysis is possible."

**Required before a causal replay can run:** 6 months of D1/H4/M15 OHLC for the
24-symbol universe, exported read-only from MT5, with timestamp convention/TZ,
per-symbol candle counts and gap list, provenance, and bid/ask or spread where
available — stored outside the production DB.

---

## 7. Signal-quality results

**Not measured — `INSUFFICIENT DATA`.** A causal historical replay/outcome
evaluator was **not** implemented, because the mandatory input dataset (§6) does
not exist; building one against live/synthetic fixtures would produce
non-reproducible, look-ahead-prone results. Per the brief, no setup thresholds
were tuned and the D1→H4→M15 methodology, risk, entry/stop/target and lifecycle
definitions are unchanged.

No sample sizes, expectancy, R-distributions, robustness or out-of-sample
figures are reported, because any such number from the available data would be
unfounded. (Delivery counts from Phase 14E are delivery-reliability evidence, not
signal quality.)

---

## 8. Limitations / insufficient evidence

* The historical incident's **exact** per-opportunity timeline (whether the
  server ran continuously over the weekend or was down and the first post-restart
  scan had no MT5 data) is reconstructed from DB/event evidence, not from a
  preserved crash log; the **mechanism** (wall-clock sweep) is confirmed.
* Signal quality is blocked on the absent historical dataset.
* **Defect (surfaced, not hard-failed) — DB-path resolution.** `db_dir` still
  defaults to `os.environ.get("SMC_GUI_DB_DIR", ".")`. A server started from the
  wrong working directory with an absent DB would **continue to monitor and
  notify using a fresh empty database**, emitting only a WARNING (now also
  exposed as `recovery.db_initialized_empty=True`). This is **not** a hard
  recovery/configuration error. Recommended follow-up (not done here, to avoid an
  architectural change without deployment/first-run requirements): require an
  explicit canonical DB path (or a sentinel) for production, while allowing
  first-run creation only under a documented, explicit condition.
* `expire_cycle()` (engine) still has a wall-clock fallback for direct callers;
  it is **no longer on the scan path**, so it cannot expire opportunities during
  scanning/restart. Retained for explicit/test use.

---

## 9. Full-suite results

```
pytest -q  →  604 passed, 1 skipped, 0 failed, 0 errored   (4m53s)
```
Baseline `46b671a`: 594 passed, 1 skipped. Delta **+10** (`tests/test_recovery.py`:
7 original + 3 added in the Phase 16A review). No unrelated regressions;
Telegram/UI/arbitration/safety suites all pass.

---

## 10. Safety

`LIVE_EXECUTION_ENABLED=False` (unchanged). No broker primitive touched; no order;
no SMC/risk/gate change; no MT5 terminal reconfigured; `Varis_SMC` untouched; no
artificial Telegram signal; the production DB was not crashed, corrupted, cleared
or recreated (analysis used a copy). The running server (46b671a) was left as-is.

---

## 11. Working-tree / commit status

* HEAD `46b671a` (pushed, in sync with `origin`). Nothing staged.
* Modified: `src/smc_engine/web/hub.py`.
* New: `tests/test_recovery.py`, `PHASE-16-RECOVERY-AND-SIGNAL-QUALITY-AUDIT.md`.
* Unrelated untracked audit reports untouched. Not committed / not pushed.

---

## 12. Decisions (separate gates)

**Workstream A — Recovery reliability: PASS.**
Two demonstrated defects were fixed: (1) the wall-clock TTL sweep when no market
data is available, and (2) the **global** sweep keyed to `max(last-closed-M15)`,
which let a trading instrument expire a closed instrument's opportunities. Both
are resolved by **removing the global sweep** — expiry is now solely the
per-symbol, market-anchored `_expire_symbol()` inside `observe()`. Regression
tests fail on the pre-fix code (closed-market no-expiry; no cross-instrument
expiry). Recovery guarantees are test-backed: persisted state survives restart
(same DB and different cwd) and **abrupt termination**; genuine expiry uses the
symbol's own market time; restart/closed-market causes no artificial expiry;
startup reconciliation is idempotent; DB identity is visible in diagnostics.
(Documented limitation: DB-path resolution still warns rather than hard-fails on
an unintended empty DB — §8.)

**Workstream B — Signal quality: INSUFFICIENT DATA.**
No stored historical OHLC dataset exists; a causal replay/outcome evaluation
cannot be produced or trusted until ~6 months of D1/H4/M15 data for the 24-symbol
universe is collected (§6). No quality PASS is claimed.

---

## 13. Phase 16A independent review outcome

* **Scope:** only `src/smc_engine/web/hub.py` (modified) and
  `tests/test_recovery.py` (new) plus this report. No unrelated files or
  pre-existing reports changed; the production DB was not mutated (analysis used
  a copy; tests use disposable DBs). `git diff --check` clean; nothing staged.
* **Expiry challenge (§2):** the review's concern was valid and stronger than the
  first fix — `data_now` was **global**, so the sweep could expire a closed
  instrument's opportunities. Corrected by removing the global sweep (per-symbol
  expiry only). The six review questions are answered in §4.
* **DB identity (§3):** `db_existed_at_startup` is captured before the store
  opens; a missing DB is surfaced (WARNING + `db_initialized_empty`). It still
  **warns rather than hard-fails** — reported as a defect/limitation (§8), not
  silently healthy.
* **Reconciliation (§4):** the 8 `OPPORTUNITY_SUPERSEDED` were genuine
  pre-existing conflicts; idempotent across restarts; one-time burst only (§5b).
* **Tests (§5):** added cross-instrument no-expiry (Q2), per-symbol market-time
  expiry, and abrupt-termination (killed process) durability. 10 recovery tests.
* **Results (§6):** focused `tests/test_recovery.py` **10 passed**; full suite
  **604 passed, 1 skipped, 0 failed, 0 errored**. `LIVE_EXECUTION_ENABLED=False`;
  `Varis_SMC` untouched; no artificial setups.

**Review decision: APPROVE WITH LIMITATIONS.** The recovery correction is
code-level justified, regression-tested (both defects fail pre-fix), and
strengthened beyond the initial fix. The limitation is the DB-path
resolution behaviour (§8): an unintended empty DB warns and is flagged but does
not hard-fail, which should be hardened (explicit canonical path) in a follow-up
once deployment/first-run requirements are confirmed. Signal quality remains
`INSUFFICIENT DATA` and is unaffected by this review.

---

## 14. Controlled deployment of `09c7ce2` (Phase 16D)

**Pre-deployment state.** Running instance PID 37192 (child of `.venv` python
31556 ← launcher cmd 39520), started 2026-10-11T05:53:27Z, listener on
`127.0.0.1:8765`; `/api/runtime` → `loaded_git_commit=46b671a`,
`git_commit=09c7ce2`, **`stale_code=True`**, `files_modified_after_startup=[web/hub.py]`
(the old instance had not loaded the recovery change). Canonical DB
`C:\smc_engine\smc_engine_gui.sqlite3` existed. Baseline funnel: total 185
(`ENTRY_TRIGGERED 51, EXPIRED 114, INVALIDATED 12, SUPERSEDED 8`); telegram
`send_count=1` (GBPJPY, 05:54).

**Backup (before restart).** Consistent copy via the SQLite **backup API**
(WAL-safe, not a file copy): `…\smc16\backups\smc_engine_gui_20261011_141937.sqlite3`;
`PRAGMA integrity_check = ok`; 185 opportunities / 1541 events; states matched.

**Restart (smc_engine only).** Stopped exactly 37192, 31556, 39520; port 8765
released and no smc process remained. Started one instance via the documented
launcher (`start_workstation.bat` → `load_mt5_credentials.ps1`, DPAPI creds):
new tree cmd 34052 → `.venv` python 20812 → system python **37656** (listener).
Identity gate AUTHORIZED, account `477217728 @ Exness-MT5Trial9` DEMO. MT5 and
`Varis_SMC` were not touched.

**Runtime verification (new instance).**
`loaded_git_commit=09c7ce2`, `git_commit=09c7ce2`, **`stale_code=False`**,
`files_modified_after_startup=[]`; `recovery.db_existed_at_startup=true`,
`db_initialized_empty=false`; canonical DB in use; **exactly one** instance.

**Before/after opportunity counts and transitions.** Total unchanged (185); no
new events emitted by the deployment scan (`last_scan.events=[]`,
`alerts_deduplicated=14`, `symbols_skipped=24` — market closed). Settled state:
`{ENTRY_TRIGGERED 51, EXPIRED 113, INVALIDATED 12, SUPERSEDED 8,
WAITING_FOR_CONFIRMATION 1}`. Exactly **one** non-terminal opportunity carries an
`EXPIRED` event — `OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH` — a
**historical resurrection artifact**: its event log shows `EXPIRED`
(2026-10-10T11:23:06) then `OPPORTUNITY_ADVANCED`→`WAITING_FOR_CONFIRMATION`
(2026-10-10T11:23:30), produced under the **pre-Phase-14C** code (its preceding
events use the old `WAITING_FOR_CSD:<advancing_ns>` identity). The current
revision **cannot** resurrect it: `repository.query(active_only=True)` excludes
`EXPIRED` (`repository.py:169-171`), and per-symbol expiry uses the symbol's own
market time (2026-10-09 20:30) which is **before** this opp's `expires_at`
(2026-10-10 04:00), so `09c7ce2` neither expires nor reactivates it. The
deployment therefore caused **no** opportunity-state change; the 1-count delta
versus the startup read is this pre-existing artifact surfacing on the settled
read (a read-timing ambiguity noted below).

**Recovery acceptance.** Confirmed by the deterministic regression tests
(`tests/test_recovery.py`, 14): no wall-clock expiry on missing market time;
per-symbol market time only; genuine expiry when the symbol's own time advances;
closed-market/feed-outage distinguishable; persisted state + history survive
restart; idempotent reconciliation; delivered events not replayed. Not reproduced
against production data (market closed).

**Notification observation.** New instance `telegram_send_count=0`; no re-delivery
of previously delivered events; **no new eligible event occurred** (market closed,
all symbols skipped) → **live new-event delivery remains unverified.** Because the
notification layer still lacks a durable delivery ledger, no exactly-once claim is
made across crashes/ambiguous outcomes. `Varis_SMC` was not touched.

**Rollback conditions:** none triggered (DB opened + backed up; intended DB; loaded
revision correct and not stale; history intact; one instance; live execution
disabled).

**Remaining risks / limitations.** (a) A **read-timing ambiguity**: the
`counts_at_startup`/backup reads showed 114 `EXPIRED` while the settled read shows
113 + 1; the single affected row is the historical artifact above, and no
transition event was emitted by the deployment. (b) The historical resurrection
artifact remains in the DB (terminal-immutability prevents re-labelling; it is
inert and cannot be advanced by the current code). (c) Live new-event Telegram
delivery unverified (no eligible event). (d) `LIVE_EXECUTION_ENABLED=False`
confirmed.

**Final recovery decision: PASS** (deployment loaded the intended revision against
the intended DB with no artificial expiry/supersession; recovery guarantees
test-backed). Live new-event notification delivery: **CONDITIONAL** (unverified —
no eligible event occurred). Signal quality: **INSUFFICIENT DATA**.

---

## 15. Phase 16E — CORRECTION: the deployment DID mutate opportunity state (§14 superseded)

§14 stated that `09c7ce2` "cannot resurrect" the GBPAUD row and attributed the
`EXPIRED 114→113 / WAITING_FOR_CONFIRMATION 0→1` delta to a "read-timing
ambiguity". **A read-only reconciliation (see `PHASE-16D-OPPORTUNITY-STATE-RECONCILIATION.md`)
disproved that.** The delta was a **real, single-row state mutation** performed by
the newly deployed process.

### Confirmed mutation
`OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH` changed
`EXPIRED → OPPORTUNITY_ARMED → WAITING_FOR_CONFIRMATION`, with state-history rows at
**`2026-10-11T13:21:30.515Z` / `.518Z`** — `~56 s` after `server_started_at=13:20:34.5Z`
and after that process's first per-symbol analysis (`GBPAUD last_analysis_time=13:20:49Z`).
The pre-deployment backup (13:19:37Z) had it `EXPIRED`. `opportunity_events` was
unchanged (1541) because the `_arm`-path `OPPORTUNITY_CREATED` event was
deduplicated — which is why the deployment scan was reported as producing "no events".

### Confirmed root cause
1. `canonical_opportunity_key()` includes `anchor_level`; `opportunity_id_from_key()`
   **drops** it → two canonical keys (same time, drifted level) share one `opportunity_id`.
2. `_discover_reversal()` guarded only by `get_by_key()` → a drifted-level re-observation
   was **not found** → `_arm()` ran.
3. `upsert()` `ON CONFLICT(opportunity_id) DO UPDATE` overwrote `state`/`payload` and
   **never updated/validated `canonical_key`**, and bypassed the state machine → a
   terminal row was re-armed.
4. The old revision (`46b671a`, stale `hub.py`) re-armed at `05:54:06Z` and
   **re-expired by wall-clock at `05:54:28Z`**; the new revision's **per-symbol
   market-anchored expiry** (GBPAUD market time `2026-10-09 20:30Z` < `expires_at
   2026-10-10 04:00Z`) no longer re-expires it → it stuck as `WAITING_FOR_CONFIRMATION`.

### Corrected guarantees (Phase 16E fix — local, uncommitted)
* **Canonical identity contract:** the indexed `canonical_key` column is the row's
  authoritative identity; `opportunity_id` is a legacy-derived handle. `upsert` and
  `apply_transition` now **refuse** (with a safe diagnostic) any write whose incoming
  `canonical_key` differs from the persisted one — an id can bind to exactly one key.
* **Terminal immutability at the mutation boundary:** a terminal row (`EXPIRED`,
  `INVALIDATED`, `SUPERSEDED`, `ENTRY_TRIGGERED`, `TERMINAL`) can never be written to a
  **non-terminal** state (terminal→terminal reclassification remains allowed).
* **Discovery guard by identity:** `_discover_*` now also checks the derived
  `opportunity_id`, so a drifted-level re-observation is skipped with an observable
  diagnostic rather than re-armed.
* **Atomic transitions + event consistency:** `apply_transition()` commits the row
  update + `opportunity_state_history` + `opportunity_events` in one transaction; the
  dedup key now includes the concrete `from->to` transition, so a genuine new
  transition is never suppressed by a reused evidence key; an unchanged state records
  nothing.
* **Regression proof:** `tests/test_identity_integrity.py` (16 tests) reproduces the
  GBPAUD collision; a minimal reproduction returns `OPPORTUNITY_ARMED` (resurrected)
  on the pre-fix code and `BLOCKED: OpportunityIdentityConflict` / `FINAL_STATE:
  EXPIRED` on the fix.

### Outstanding migration risks (read-only assessment)
`tools/phase16e_identity_assessment.py` over the live DB (via backup-API snapshot):
**10** rows where `column canonical_key` disagrees with `payload.canonical_key`,
**10** rows with a terminal→later-non-terminal history, **11** opportunities whose
history-transition count exceeds their event count, **0** potential duplicate groups
under a level-inclusive id. These are **pre-existing** inconsistencies (the GBPAUD row
is one). No production repair was performed; every repair is proposed only (§7 of
`PHASE-16D-…md`) and must run in a disposable copy pending an approved migration.

This correction does not change the recovery *fix* (per-symbol market-time expiry is
correct and retained); it corrects the **record** of what the deployment did and adds
the identity/terminal/event hardening.

---

## 16. Phase 16E — recomputed migration assessment and acceptance gates

**Method.** `tools/phase16e_identity_assessment.py` opens the operational DB
`mode=ro` and takes a consistent **SQLite backup-API** snapshot into `%TEMP%\smc16e\`;
it never writes the source. Recomputed `2026-10-11T19:13Z` on a fresh snapshot
(prior figures in parentheses). Counts are measured, not assumed.

| category | fresh (prior) |
|---|---|
| total_opportunities | **186** (185 — DB grew by one row) |
| key_mismatch (column key ≠ payload key) | **10** (10) |
| id_drift (id no longer derives from payload key) | **0** (0) |
| reactivation (terminal → later non-terminal history) | **10** (10) |
| history_gt_events (history rows > event rows) | **11** (11) |
| potential_duplicate_groups (level-inclusive id) | **0** (0) |

**Affected rows and proposed dispositions** (all read-only; **no production repair
executed**):

| opportunity_id | column → payload key | row state | terminal→active | disposition |
|---|---|---|---|---|
| OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH | 1.89623 → 1.89628 | WAITING_FOR_CONFIRMATION (live) | yes (EXPIRED→ARMED) | **QUARANTINE / MANUAL REVIEW** — only live resurrected row |
| OPP-REVERSAL-AUDUSD-SWEEP-1789992000000000000-BEARISH | 0.71396 → 0.7136 | EXPIRED | yes | NO ACTION (terminal; column key authoritative) |
| OPP-CONTINUATION-CHFJPY-BOS-1791345600000000000-BEARISH | 189.887 → 189.827 | ENTRY_TRIGGERED | yes | NO ACTION (terminal) |
| OPP-CONTINUATION-CHFJPY-BOS-1791374400000000000-BEARISH | 189.887 → 189.827 | ENTRY_TRIGGERED | yes | NO ACTION (terminal) |
| OPP-REVERSAL-ETHUSD-SWEEP-1786896000000000000-BEARISH | 1886.28 → 1884.33 | EXPIRED | yes | NO ACTION (terminal) |
| OPP-REVERSAL-EURGBP-SWEEP-1790064000000000000-BULLISH | 0.85695 → 0.85716 | EXPIRED | yes | NO ACTION (terminal) |
| OPP-REVERSAL-GBPCHF-SWEEP-1790827200000000000-BEARISH | 1.10454 → 1.10967 | EXPIRED | yes | NO ACTION (terminal) |
| OPP-REVERSAL-USDCAD-SWEEP-1786492800000000000-BULLISH | 1.39252 → 1.39262 | EXPIRED | yes | NO ACTION (terminal) |
| OPP-REVERSAL-XAUUSD247-SWEEP-1788840000000000000-BEARISH | 4437.056 → 4435.029 | EXPIRED | yes | NO ACTION (terminal) |
| OPP-REVERSAL-GBPUSD-SWEEP-1791432000000000000-BULLISH | 1.31898 → 1.31931 | EXPIRED | yes | NO ACTION (terminal) |
| OPP-REVERSAL-GBPUSD-SWEEP-1791374400000000000-BULLISH | (column == payload) | EXPIRED | yes | **QUARANTINE / MANUAL REVIEW** — reactivation with no key drift, not explained by the collision |

**`history_gt_events` reconciliation (11 rows).** Not a lost transition. The pre-fix
`_arm`/`_transition`/`_terminate` always inserted an `opportunity_state_history` row,
while `record_event_once` deduplicated the matching event by
`(opportunity_id, transition=kind, evidence_key)`. On a repeated re-arm of the same key
the `IDLE→OPPORTUNITY_ARMED(armed)` history row was written but the `OPPORTUNITY_CREATED`
event was suppressed (same evidence `SWEEP:<ns>`). So those specific history rows have
no event by design; **no genuine transition lacks an event**.

**One id ↔ multiple identities.** `id_drift=0`: every `opportunity_id` still derives
from its persisted key. The collision manifests *within* a single row (frozen column key
+ overwritten payload key), not as two distinct rows. No row was merged, re-keyed,
deleted, or invented.

### Acceptance gates (Phase 16E)

1. **Expiry & recovery reliability — PASS.** Per-symbol market-time expiry retained;
   `expire_cycle(None)` is a no-op; `tests/test_recovery.py` (14) +
   `test_per_symbol_market_time_expiry_still_records_event` pass; full suite **624
   passed / 1 skipped**.
2. **Canonical identity & terminal-state integrity — PASS (fix) / migration PENDING.**
   Identity collision refused at `upsert`/`apply_transition` with a safe diagnostic;
   terminal immutability enforced at the write boundary; discovery guards by identity;
   `tests/test_identity_integrity.py` (16) pass. The two quarantine rows require an
   approved, disposable-copy migration.
3. **Notification-delivery reliability — CONDITIONAL.** Lifecycle events now persist
   atomically with the transition, but the durable Telegram **delivery** ledger remains
   a separate follow-up; exactly-once remote delivery is **not** claimed.
4. **Signal quality — INSUFFICIENT DATA.** Unchanged: no adequate historical OHLC
   dataset.
