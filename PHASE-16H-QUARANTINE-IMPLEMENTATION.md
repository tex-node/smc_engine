# PHASE-16H — OPPORTUNITY QUARANTINE: IMPLEMENTATION AND REGRESSION TESTS

**Repository:** `C:\smc_engine` · **Branch:** `feature/gui-workstation` · **HEAD:** `4682f63`
**Status:** implementation + tests only. **No production migration, no quarantine applied to production, no restart, no commit/push, no Telegram send.**
`LIVE_EXECUTION_ENABLED=False`; MT5 and `Varis_SMC` untouched.

Implements the mechanism designed in `PHASE-16F-PRODUCTION-MIGRATION-PLAN.md` §3.

---

## 1. What was implemented

### `src/smc_engine/opportunity/repository.py`
* **Additive table** `opportunity_quarantine(opportunity_id PK, reason, at, evidence_json)` created with `CREATE TABLE IF NOT EXISTS` (no `ALTER`; existing DBs gain an empty table).
* **Idempotent operations:** `quarantine(id, reason, evidence) -> bool` (`INSERT OR IGNORE`), `unquarantine(id) -> bool`, `is_quarantined(id)`, `quarantined_ids()`, `quarantine_diagnostics()`, `quarantined_count()`.
* **Active-scan exclusion:** `query(active_only=True)` and `active_for_instrument()` append `AND opportunity_id NOT IN (SELECT opportunity_id FROM opportunity_quarantine)`.
* **Authoritative write guard:** `_assert_identity_invariants` now raises `QuarantinedOpportunity` (a new `OpportunityInvariantError` subclass) *before* any write, so `upsert`/`apply_transition` can never re-arm, promote, or transition a quarantined row.
* The inconsistent row, its `canonical_key`, `state_history`, and `events` are **never touched** by quarantine.

### `src/smc_engine/opportunity/engine.py`
* `_arm` now catches the base `OpportunityInvariantError` (covers identity, terminal, and quarantine) → a refused arm is surfaced in `result["blocked"]`, not raised.
* **`_expire_symbol` isolates per-opportunity invariant failures** (try/except around `_terminate`): an inconsistent/quarantined row can no longer abort expiry of the other opportunities for the same symbol (requirement 3).
* `diagnostics()` exposes `quarantined_count` and `quarantine`.

### `src/smc_engine/web/hub.py`
* `_dispatch_opportunity_events` skips any event whose `opportunity_id` is quarantined → **no SSE alert and no Telegram notification** for a quarantined opportunity (requirement 2). Unrelated events/symbols are unaffected.

---

## 2. Trace: how the conflict behaved before/after

| path | before | after |
|---|---|---|
| `_advance_symbol → _advance_one → _terminate` | per-opp `try/except` (caught, logged) | unchanged |
| `_expire_symbol → _terminate` | **unguarded** → `OpportunityIdentityConflict` propagated out of `observe` → whole-symbol cycle result discarded (events persisted but never dispatched, then deduped) | **guarded per opp** → isolated (logged), other opportunities continue |
| active reads | included the inconsistent row | **excluded** (quarantined) |
| notification dispatch | could emit for the row | **skipped** |

---

## 3. Tests (`tests/test_opportunity_quarantine.py`, 10 tests)

Disposable SQLite databases only; mocked notification transport (`FakeTelegram`), no network.

1. `test_quarantine_is_additive_idempotent_and_auditable` — second quarantine returns `False`; diagnostics carry reason+evidence; `unquarantine` works.
2. `test_quarantined_excluded_from_active_scans` — removed from `query(active_only)` and `active_for_instrument`; still returned by diagnostic (non-active) reads.
3. `test_write_guard_refuses_quarantined_writes` — `upsert` and `apply_transition` raise `QuarantinedOpportunity`; state unchanged.
4. `test_identity_conflict_is_isolated_without_quarantine` — a poisoned row (col `1.89623` / payload `1.89628`, one id) makes `observe` complete **without raising**; the unrelated valid opportunity is still processed; the poisoned row is untouched and emits no events.
5. `test_quarantine_isolates_and_processes_other_opportunities` — same with the poisoned row quarantined.
6. `test_quarantine_persists_across_restart` — a new repository on the same file still sees the quarantine and excludes the row.
7. `test_quarantined_event_is_not_dispatched` — `EngineHub._dispatch_opportunity_events` with a mocked telegram: quarantined event not delivered, unrelated event delivered.
8. `test_quarantine_preserves_row_history_and_events` — `(canonical_key, state, payload_json)` and `(history_count, event_count)` are byte-identical before/after quarantine.
9. `test_other_symbols_unaffected` — quarantining a GBPAUD row does not affect EURUSD; `observe("EURUSD", …)` still works.
10. `test_engine_diagnostics_expose_quarantine` — `diagnostics()["quarantined_count"] == 1`.

---

## 4. Test results

| run | result |
|---|---|
| `tests/test_opportunity_quarantine.py` | **10 passed** |
| focused (`identity_integrity` + `recovery` + `quarantine` + `opportunity_engine` + `opportunity_cases` + `telegram_delivery`) | **91 passed** |
| **full suite** (`pytest -q`) | **634 passed, 1 skipped, 0 failed, 0 errored** |

`git diff --check` clean.

---

## 5. Safety / non-mutation

* **Production DB not migrated and not quarantined.** No `opportunity_quarantine` row exists in `smc_engine_gui.sqlite3`; the GBPAUD and GBPUSD rows are unchanged **by this work** (GBPAUD `…1.89623` ≠ payload `…1.89628`; it was `WAITING_FOR_CONFIRMATION` at 16H time and the running server later expired it to `EXPIRED`, 32 events / 67 history — see §8.6; GBPUSD `EXPIRED`).
* Server **not** restarted (PID 37656); `LIVE_EXECUTION_ENABLED=False`; MT5 / `Varis_SMC` untouched; no Telegram messages; nothing staged/committed/pushed.

---

## 6. Working-tree scope (uncommitted)

```
 M src/smc_engine/opportunity/engine.py     (+23/−10 net)
 M src/smc_engine/opportunity/repository.py (+86/−? net)
 M src/smc_engine/web/hub.py                (+7)
?? tests/test_opportunity_quarantine.py     (new, 10 tests)
?? PHASE-16F-PRODUCTION-MIGRATION-PLAN.md   (revised, 16G)
?? PHASE-16H-QUARANTINE-IMPLEMENTATION.md   (this report)
```
(Plus pre-existing untracked `PHASE-9/10/12/13/14B` docs — unrelated.)

---

## 7. Not done (requires separate approval)

* No production quarantine application, no identity reconciliation, no lifecycle-state change, no restart, no commit/push.
* The one-off **application tool** now exists (`tools/phase16g_quarantine_opportunities.py`, §8) but has **not** been run against production; the deployment/rollback sequence remains as specified in `PHASE-16F-PRODUCTION-MIGRATION-PLAN.md`.
* Notification-delivery reliability stays **CONDITIONAL**; signal quality stays **INSUFFICIENT DATA**.

---

## 8. Phase 16H.1 — acceptance gap closure

### 8.1 Dry-run-first quarantine utility (`tools/phase16g_quarantine_opportunities.py`)
Idempotent, fail-closed. **Dry-run is the default.** Write mode (`--apply`) refuses unless *all* confirmations are met:
* explicit `--db` and `--opportunity-id`; explicit non-empty `--reason` and `--evidence` (JSON object);
* the id resolves to exactly one existing row (missing/ambiguous → refuse);
* `--expect-db` canonicalizes to the **same file** as `--db` (path mismatch → refuse);
* `--backup` names an existing file that passes `PRAGMA integrity_check`;
* `--expect-count` equals the current opportunity count (pins the DB generation).

It only inserts into `opportunity_quarantine`; it never touches the row/payload/keys/history/events, is idempotent, and has **no unquarantine path**. It asserts the row is byte-identical after the write.

### 8.2 Send-boundary quarantine check (`telegram.py`)
`TelegramNotifier` now accepts `quarantine_check` (the repository's `is_quarantined`) and calls it **immediately before every outbound send** in `_send_with_retry` — a queued event is still suppressed if the opportunity was quarantined after enqueue. `_is_quarantined` is **fail-closed**: a storage error is treated as quarantined (`telegram_quarantined_count` incremented, send skipped), never as "not quarantined". The hub wires `quarantine_check=self.opportunities.repo.is_quarantined`.

### 8.3 Storage-failure behaviour
* Repository `is_quarantined` **raises** on a missing table or a closed connection (surfaced, never silently `False`).
* The hub dispatch is **fail-closed**: if `quarantined_ids()` fails it logs and dispatches nothing for the cycle (unknown ≠ not-quarantined).
* The notifier suppresses sends when the check cannot be established.

### 8.4 Tests added
`tests/test_quarantine_notifications.py` (9): deterministic queued-event race (enqueue not-quarantined → quarantine before send → suppressed; unrelated event delivered); notifier fail-closed; send suppressed on check error; hub dispatch fail-closed; missing-table and closed-connection detection.
`tests/test_quarantine_tool.py` (8): dry-run default writes nothing; missing/empty id refused; missing reason/evidence refused; expected-target/backup/count refusals; idempotent apply; row/history/event preservation; never unquarantines.

### 8.5 Results
| run | result |
|---|---|
| quarantine + notifications + tool | **27 passed** |
| focused (identity + recovery + telegram + telegram_delivery + quarantine×3 + engine + cases) | **131 passed** |
| **full suite** | **651 passed, 1 skipped, 0 failed, 0 errored** |

### 8.6 Operational note (production drift — external to this task)
During this session the **running server (PID 37656, revision `09c7ce2`) expired GBPAUD** once the market reopened (`updated_at 2026-10-11T21:30Z`), so the row is now `EXPIRED` (column==payload **state**), and new opportunities appeared (total **187**). The **identity mismatch persists** (indexed key `…1.89623` ≠ payload key `…1.89628`). This drift was produced by the server's own scanning — **not** by this task's tooling or tests: no test references the production path and every `EngineHub` in the suite passes an explicit `db_path`. Assessment categories are unchanged (`key_mismatch=10, id_drift=0, reactivation=10, history_gt_events=11`).

### 8.7 Working-tree scope (uncommitted)
```
 M src/smc_engine/opportunity/engine.py
 M src/smc_engine/opportunity/repository.py
 M src/smc_engine/telegram.py
 M src/smc_engine/web/hub.py
?? tests/test_opportunity_quarantine.py
?? tests/test_quarantine_notifications.py
?? tests/test_quarantine_tool.py
?? tools/phase16g_quarantine_opportunities.py
?? PHASE-16F-PRODUCTION-MIGRATION-PLAN.md
?? PHASE-16H-QUARANTINE-IMPLEMENTATION.md
```

---

## 9. Phase 16H.2 — controlled containment (server stopped, left down)

**Authorization:** controlled stop of the running `smc_engine` server, after a verified backup and process-identity check. Server left **stopped**; no restart, no deploy, no quarantine.

### 9.1 Process identity (verified before acting)
| PID | parent | image | command |
|---|---|---|---|
| 34052 | 26524 | `cmd.exe` | `/K title "SMC Engine Workstation Server" && .venv\Scripts\python.exe -m uvicorn smc_engine.web.api:create_app … --port 8765` |
| 38704 | 34052 | `conhost.exe` | console host for 34052 |
| 20812 | 34052 | `C:\smc_engine\.venv\Scripts\python.exe` | `-m uvicorn smc_engine.web.api:create_app … --port 8765` |
| 37656 | 20812 | `…\Python312\python.exe` | `-m uvicorn smc_engine.web.api:create_app … --port 8765` (**listener** on `127.0.0.1:8765`) |

`GET /api/runtime`: `pid=37656`, `server_started_at=2026-10-11T13:20:34Z`, `loaded_git_commit=09c7ce2`, `git_commit=4682f63`, **`stale_code=true`** (`files_modified_after_startup=["web/hub.py"]`), `live_execution_enabled=false`, account `AUTHORIZED`, `recovery.db_path_basename=smc_engine_gui.sqlite3`, `counts_current={ENTRY_TRIGGERED:52, EXPIRED:115, INVALIDATED:12, SUPERSEDED:8}`. All four processes belong **exclusively** to the smc_engine launcher tree.

### 9.2 Backup (pre-stop, verified)
* Source (canonical): `C:\smc_engine\smc_engine_gui.sqlite3` (`SMC_GUI_DB_DIR` unset → `./` relative to the launcher's `cd /d C:\smc_engine`).
* Backup: `C:\Users\XPS\AppData\Local\Temp\opencode\smc16h2\backups\smc_engine_gui_pre_20261011T220946Z.sqlite3` (outside the repo), created `2026-10-11T22:09:46Z`, 23,531,520 bytes.
* **SHA-256 `d81c27e16edd4cbefc75dff1bdb38090686f34dd73a89205647bbfd72f76fe3b`**, `PRAGMA integrity_check=ok`, `PRAGMA foreign_key_check=[]`, `user_version=0`, `schema_version=1`.
* Row counts: `opportunities=187`, `opportunity_events=1549`, `opportunity_state_history=3555`, `alerts=1647`, `setup_events=105`, `setup_event_timeline=13388`, `scan_funnel_events=45838`.
* GBPAUD: column `…:1.89623`, payload `…:1.89628`, state `EXPIRED`, 67 history / 32 events (last: `WAITING_FOR_CONFIRMATION→EXPIRED (SWEEP_EXPIRED)` at `21:46:15Z`). GBPUSD: column==payload `…:1.32011`, `EXPIRED`, 6 history / 5 events.

### 9.3 Stop
The documented `stop_workstation.bat` (taskkill by launcher window title) **matched nothing and did not stop the server**; a gentle `taskkill /PID 34052 /T` was refused (needs `/F`). Ownership was re-verified immediately, then `taskkill /PID 34052 /T /F` terminated exactly `{37656, 38704, 20812, 34052}`.

### 9.4 Post-stop verification
* Port `127.0.0.1:8765`: **no listener**. No `smc_engine` uvicorn process; no launcher console.
* **No respawn** (re-checked after 12 s).
* Post-stop snapshot `…\smc_engine_gui_post_20261011T221345Z.sqlite3`: `integrity_check=ok`, `foreign_key_check=[]`, and **identical SHA-256 to the pre-stop backup** → **no records changed immediately before shutdown** (last write was `~22:05Z`, before the kill). Counts unchanged (187 / 1549 / 3555 / 1647).

### 9.5 Safety confirmations
`LIVE_EXECUTION_ENABLED=False`; **no migration/quarantine run**; production DB **not modified** (read-only snapshots only; no `opportunity_quarantine` table); `Varis_SMC.mq5` untouched (`2026-10-09 20:02`); MT5 (`terminal64.exe`) still running (PIDs 11156/5220/29640/39868) and not reconfigured; codex-router and ChartAsst processes untouched; no Telegram messages; nothing staged/committed/pushed; all uncommitted Phase 16H changes and reports preserved.

### 9.6 Git
HEAD `4682f63`; nothing staged. Scope unchanged: modified `engine.py`, `repository.py`, `telegram.py`, `web/hub.py`; untracked `tests/test_opportunity_quarantine.py`, `tests/test_quarantine_notifications.py`, `tests/test_quarantine_tool.py`, `tools/phase16g_quarantine_opportunities.py`, `PHASE-16F-PRODUCTION-MIGRATION-PLAN.md`, `PHASE-16H-QUARANTINE-IMPLEMENTATION.md`.

**Server is left DOWN.** No deploy, no production quarantine. Awaiting explicit approval.

---

## 10. Phase 16H.2 — final pre-deployment validation

### 10.1 Requirement → implementation → test
| requirement | implementation | test |
|---|---|---|
| explicit, auditable, idempotent quarantine | `OpportunityRepository.quarantine` (`INSERT OR IGNORE`) + `quarantine_diagnostics` | `test_quarantine_is_additive_idempotent_and_auditable` |
| no change to row / keys / history / events | quarantine inserts only into `opportunity_quarantine`; the tool asserts the row is byte-identical | `test_quarantine_preserves_row_history_and_events`, `test_apply_is_idempotent_and_preserves_row_history_events` |
| quarantined IDs excluded from active processing | `query(active_only=True)` + `active_for_instrument` `NOT IN (…quarantine)` | `test_quarantined_excluded_from_active_scans` |
| upsert/transition reject quarantined IDs | `_assert_identity_invariants` → `QuarantinedOpportunity` | `test_write_guard_refuses_quarantined_writes` |
| identity conflict does not abort unrelated same-symbol observation | `_expire_symbol` per-opportunity guard; `_arm` catches `OpportunityInvariantError` | `test_identity_conflict_is_isolated_without_quarantine`, `test_quarantine_isolates_and_processes_other_opportunities` |
| final send-boundary check, incl. queued-before-quarantine | `TelegramNotifier._send_with_retry` → `_is_quarantined` | `test_queued_message_suppressed_when_quarantined_before_send` |
| fail closed when storage missing/unreadable | `_is_quarantined` raises → suppress; hub dispatch fail-closed | `test_notifier_fails_closed_when_check_raises`, `test_send_suppressed_when_check_raises`, `test_dispatch_fails_closed_when_state_unavailable`, `test_missing_quarantine_table_is_detected_not_false`, `test_closed_connection_error_is_surfaced` |
| other opportunities/symbols remain eligible | per-ID exclusion + per-symbol isolation | `test_other_symbols_unaffected` |
| dry-run tool requires explicit db / expected identity / id / reason / evidence / verified backup | `assess` + `apply_quarantine` preconditions | `tests/test_quarantine_tool.py` (8) |
| no accidental unquarantine / bulk suppression | tool has no unquarantine path; no bulk operation exists | `test_tool_never_unquarantines` |

### 10.2 Verification results (re-run, not carried over)
| run | passed | failed | skipped | errored |
|---|---|---|---|---|
| focused (quarantine×3 + identity_integrity + recovery + telegram + telegram_delivery + engine + cases) | **131** | 0 | 0 | 0 |
| **full suite** (`pytest -q`) | **651** | 0 | **1** | 0 |

`git diff --check` clean.

### 10.3 Deployment-safety confirmations
`LIVE_EXECUTION_ENABLED=False`; production server **stopped** (port `8765` free, no uvicorn); production DB **unchanged** (`mtime 2026-10-11T23:00:47` local, 187 rows, `alerts=1647`, `integrity_check=ok`) and **no `opportunity_quarantine` table**; the three containment backups are **unchanged** (SHA-256 `d81c27e1…`); MT5 running (4× `terminal64.exe`) and `Varis_SMC.mq5` untouched (`2026-10-09 20:02`); no Telegram messages; nothing staged; no credentials in the diff.

### 10.4 Remaining limitations
* No durable Telegram **delivery** ledger — exactly-once remote delivery is **not** claimed; the quarantine check guarantees suppression, not delivery.
* The production GBPAUD row remains **identity-inconsistent** (indexed `…1.89623` ≠ payload `…1.89628`) and is now `EXPIRED`; **quarantine has not been applied to production**.
* The fix + quarantine are **uncommitted**, not deployed, and have not passed CI.
* Signal quality remains **INSUFFICIENT DATA**.

### 10.5 Proposed commit contents (for review; nothing staged)
Modified: `src/smc_engine/opportunity/engine.py`, `src/smc_engine/opportunity/repository.py`, `src/smc_engine/telegram.py`, `src/smc_engine/web/hub.py`.
Added: `tests/test_opportunity_quarantine.py`, `tests/test_quarantine_notifications.py`, `tests/test_quarantine_tool.py`, `tools/phase16g_quarantine_opportunities.py`, `PHASE-16F-PRODUCTION-MIGRATION-PLAN.md`, `PHASE-16H-QUARANTINE-IMPLEMENTATION.md`.
Excluded (unrelated, preserved): the pre-existing untracked `PHASE-9/10/12/13/14B` reports.

### 10.6 Send-boundary concurrency guarantee (precise)
The quarantine check runs in `_send_with_retry` **immediately before** `_send_telegram_message`, reading the authoritative persistent `opportunity_quarantine` row for the item's `opportunity_id`. The exact guarantee:

* **Point-in-time suppression:** if the opportunity is quarantined at the instant of the check, the send is skipped (`telegram_quarantined_count` incremented) — **including items queued before the quarantine** (verified by the race test).
* **Fail-closed:** if the state cannot be read (missing table / storage error), the send is skipped.
* **Not atomic / not exactly-once:** the state read and the network send are **not** in a single transaction, so there is a small TOCTOU window between the check and the HTTP request; a quarantine committed inside that window does not suppress that one send. The hub dispatch filter is an additional per-cycle early filter, not a delivery guarantee. Telegram's Bot API offers no idempotency key, and the durable **delivery** ledger remains a separate follow-up — **exactly-once remote delivery is not claimed.**
