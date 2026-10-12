# PHASE-16F — PRODUCTION MIGRATION PLAN (revised, Phase 16G)

**Repository:** `C:\smc_engine` · **Branch:** `feature/gui-workstation` · **HEAD:** `4682f63`
(`fix(opportunities): enforce identity and terminal-state invariants`; parent `09c7ce2`)
**Canonical DB:** `C:\smc_engine\smc_engine_gui.sqlite3` (WAL) · **Running server:** PID 37656, loaded revision **`09c7ce2`** (pre-fix code)
**Status:** planning only. **No production DB change, no restart, no commit/push, no Telegram send, no trading-config change.**
**Gate:** every disposition below requires review and **explicit approval**. `LIVE_EXECUTION_ENABLED=False`; MT5 and `Varis_SMC` untouched.

> **Phase 16G revision.** The Phase 16F plan proposed re-terminating GBPAUD to `EXPIRED`. That is **withdrawn**: the prior `EXPIRED` transitions are not valid proof of expiry (see §1). The revised plan **quarantines** the row and **separates identity reconciliation from lifecycle-state disposition** (§4). Nothing is executed.

---

## 1. Why GBPAUD must NOT be auto-expired

| fact | value |
|---|---|
| indexed `canonical_key` (column) | `REVERSAL:GBPAUD:BULLISH:SWEEP:1791518400000000000:**1.89623**` |
| `payload_json.canonical_key` / `sweep_evidence.swept_level` | `…:**1.89628**` |
| state (column == payload) | **`EXPIRED`** — *the running pre-fix server expired it at `2026-10-11T21:30Z` once the market reopened (see `PHASE-16H-QUARANTINE-IMPLEMENTATION.md` §8.6); the identity mismatch persists* |
| last valid GBPAUD **M15** timestamp | `2026-10-09T20:30:00Z` (at the time of the 16G analysis) |
| stored `expires_at` | **`2026-10-10T04:00:00Z`** |
| events / state-history rows | 32 / 67 (live) |

* **The market-time deadline has not elapsed.** Expiry in the current (correct) semantics is anchored to the symbol's own market time. GBPAUD's market time is `2026-10-09T20:30:00Z`, which is **before** the stored deadline `2026-10-10T04:00:00Z`. By market time the opportunity is therefore **not due to expire**.
* **The historical `EXPIRED` events are not valid evidence.** The row shows `EXPIRED` at `2026-10-10T11:23:06Z`, `2026-10-10T11:23:34Z`, `2026-10-11T05:54:28Z` — all produced by the **pre-Phase-16 wall-clock fallback**, which expired on the observation clock, not the market clock. Those transitions cannot be used to infer a valid market-time expiry.
* **The correct current lifecycle state is unresolved.** The row is neither validly active (its last arming at `2026-10-11T13:21:30Z` was the identity-collision defect, not a genuine re-observation) nor validly terminal (it never expired by market time). Neither `EXPIRED` nor an active state can be asserted.

**Disposition: `QUARANTINE / MANUAL REVIEW`** until the identity and the lifecycle state are reconciled with independent evidence.

---

## 2. How the current code handles the identity conflict during GBPAUD observation

Trace (fixed revision `4682f63`):

1. `EngineHub` calls `self.opportunities.observe("GBPAUD", view)` inside a per-symbol `try/except` (`hub.py:698-704` → logs *"opportunity observation failed for … (analysis unaffected)"*).
2. `OpportunityEngine.observe` runs, in order: `_discover_reversal`, `_discover_continuation`, **`_advance_symbol`**, `_promote`, **`_expire_symbol`**.
3. `_advance_symbol` wraps each `_advance_one` in `try/except` (`engine.py:278-284`) → the conflict raised from `_advance_reversal → _terminate` is **caught and logged** (*"opportunity advance failed for …"*).
4. `_expire_symbol` (`engine.py:602-608`) is **not** wrapped → the same `OpportunityIdentityConflict` from `apply_transition → _assert_identity_invariants` **propagates out of `observe`**.
5. `observe` therefore **aborts**; the hub discards its `result`, so `_dispatch_opportunity_events` never runs for that symbol/cycle.

**Consequence (measured, §6-A):** the conflict fails the **entire GBPAUD symbol observation**, not merely the one opportunity. Any event already durably recorded earlier in that same cycle (`_discover_*`/`_advance_*`/`_promote`) is persisted in `opportunity_events` but **never dispatched** to SSE/Telegram; because `record_event_once` deduplicates it, it is **not re-dispatched on a later cycle** → a permanent notification loss for any other valid GBPAUD opportunity in that cycle. Other symbols are unaffected (per-symbol catch).

*Currently inert:* GBPAUD's market time (`2026-10-09T20:30Z`) is before `expires_at`, so `_expire_symbol` does not fire while the market is closed; the failure becomes live once GBPAUD's market time advances past the deadline (market reopen).

---

## 3. Quarantine mechanism (narrow, explicit, auditable)

> **Implemented in Phases 16H / 16H.1** (`src/smc_engine/opportunity/repository.py`, `engine.py`, `telegram.py`, `web/hub.py`; tests `tests/test_opportunity_quarantine.py`, `test_quarantine_notifications.py`; application tool `tools/phase16g_quarantine_opportunities.py`, dry-run-first). The code exists in the working tree (**uncommitted**) and is **not applied to production** — no `opportunity_quarantine` row exists in the operational DB. See `PHASE-16H-QUARANTINE-IMPLEMENTATION.md`.

**Additive table** (no `ALTER`, no row/event/history mutation):

```sql
CREATE TABLE IF NOT EXISTS opportunity_quarantine(
  opportunity_id TEXT PRIMARY KEY,
  reason        TEXT NOT NULL,
  at            TEXT NOT NULL,
  evidence_json TEXT
);
```

**Read exclusion** in `OpportunityRepository.query(active_only=True)` and `active_for_instrument()`:
`AND opportunity_id NOT IN (SELECT opportunity_id FROM opportunity_quarantine)`.

**Write guard** (defense in depth): `upsert`/`apply_transition` raise a distinct `QuarantinedOpportunity` error when the target id is quarantined, so a quarantined row can never be re-armed, promoted, or transitioned even if it is reached by another path.

Properties required by this phase — all satisfied:
* **Auditable:** the quarantine row records `reason`, `at`, and the evidence (indexed vs payload key).
* **No promotion / re-arm / READY / ENTRY_TRIGGERED:** excluded from active reads and refused at the write boundary; `_discover_*` already skips when the id/key is found.
* **Unrelated valid GBPAUD opportunities still observed:** exclusion is per-id, not per-symbol (§6-B).
* **No silent expiry/deletion:** the row, its 31 events and 66 history rows are untouched.
* **No global suppression:** other symbols and the notification system are unaffected.
* **History intact:** nothing is rewritten or invented.

**Funnel note:** `state_counts()` still counts the row (it is a real row). The plan recommends surfacing a separate `quarantined` count so operators do not read it as an active opportunity.

---

## 4. Identity and state are separate migration decisions

### 4a. Identity reconciliation — **NOT establishable beyond reasonable doubt → retain quarantine**
The row's own evidence points to `…1.89628` (`payload.canonical_key` and `sweep_evidence.swept_level` agree), while the frozen indexed column is `…1.89623` (the first-insert value). The two disagree, and there is **no independent record** to adjudicate the true swept level of the `2026-10-09T04:00Z` sweep (market data is live-only and not persisted; the payload was itself overwritten by the collision). Because the intended canonical identity cannot be established beyond reasonable doubt, the indexed key is **left unchanged** and the row stays quarantined. **No** `opportunity_id` change, **no** event deletion, **no** timestamp rewrite, **no** invented identity.

### 4b. Lifecycle-state disposition — **deferred**
No state change is applied. The state is resolved only after identity is established (or the row is permanently quarantined). This is the key change from Phase 16F.

---

## 5. Other rows

* **`OPP-REVERSAL-GBPUSD-SWEEP-1791374400000000000-BULLISH`** — terminal `EXPIRED`, indexed key == payload key (`1.32011`), inert. **Unchanged**, pending its own review (its `SUPERSEDED → re-arm` history is a historical artifact; history is not rewritten).
* **The other 8 key-mismatch rows** — terminal and inert. **Untouched** unless independent evidence establishes a different disposition.

---

## 6. Simulation on a disposable copy (`%TEMP%\smc16g\A.sqlite3`)

Production opened `mode=ro` and copied via the SQLite **backup API**; every write went to the copy. Script: `%TEMP%\opencode\smc16g_sim.py`.

* **A — defect reproduced:** `observe("GBPAUD", <future market time>)` **raises `OpportunityIdentityConflict`** without quarantine (whole-symbol abort, as in §2).
* **B — proposal works:** with the additive `opportunity_quarantine` table + read exclusion:
  * `query(symbol="GBPAUD", active_only=True)` returns the **other** valid opportunity and **excludes** the quarantined one;
  * `observe("GBPAUD", …)` **×5 with no raise** (repeated startup/scan idempotent);
  * GBPAUD **state unchanged** (`WAITING_FOR_CONFIRMATION`), **indexed key unchanged** (`…1.89623`);
  * GBPAUD **events/history unchanged: (31, 66) → (31, 66)** → **no new notifications**, history preserved;
  * the other valid GBPAUD opportunity **was processed** (`OPPORTUNITY_ARMED → WAITING_FOR_CONFIRMATION`).
* **C — rollback:** restoring the copy from the pre-migration snapshot reproduces the exact pre-state (`sha256[0:16]` `4c174fbc45f7e5ad` == pre).

**No production row or event was mutated; no production code was changed.**

---

## 7. Deployment sequence (executed only after approval)

1. **Pre-flight (read-only):** fresh backup-API snapshot; `PRAGMA integrity_check=ok`; record counts and the GBPAUD/GBPUSD rows.
2. **Quiesce:** stop the smc_engine instance (`34052 → 20812 → 37656`); no other writer touches the DB.
3. **Cold backup:** copy `smc_engine_gui.sqlite3` (+`-wal`/`-shm`) → `…\smc16g\backup\…`; `integrity_check` on the copy.
4. **Apply the additive quarantine** (idempotent tool, e.g. `tools/phase16g_quarantine_opportunities.py`): `CREATE TABLE IF NOT EXISTS opportunity_quarantine` + `INSERT OR REPLACE` for GBPAUD with reason `IDENTITY_AMBIGUOUS_LIFECYCLE_UNRESOLVED` and the key evidence. **No** change to `opportunities`, `opportunity_events`, or `opportunity_state_history`.
5. **Deploy the fix + mechanism:** start one instance loading `4682f63` (which must also contain the read-exclusion + write guard). *Order note:* the quarantine marker must be in place before the fixed revision runs, otherwise the fixed code raises on GBPAUD's `_expire_symbol` (it is inert while the market is closed, but this avoids a live window).
6. **Verify** (§8).

**Rollback:** stop → restore the cold backup (step 3) → restart the previous revision. Exact and does not rely on inverse logic. (An inverse SQL is not used: terminal→active is forbidden by design.)

---

## 8. Post-deployment verification

1. One instance; `loaded_git_commit == git_commit == 4682f63`; `stale_code=false`; `files_modified_after_startup=[]`; `live_execution_enabled=false`; identity `AUTHORIZED`.
2. `PRAGMA integrity_check=ok`; total rows unchanged (186); GBPAUD row byte-identical (state `WAITING_FOR_CONFIRMATION`, key `…1.89623`, events 31, history 66); GBPUSD byte-identical.
3. `observe("GBPAUD", …)` completes with **no raise**; GBPAUD produces **no** READY/ENTRY_TRIGGERED events; other GBPAUD opportunities continue to be processed.
4. Quarantine row present and auditable; `state_counts()` still contains the `WAITING_FOR_CONFIRMATION:1` row (reported separately as quarantined).
5. `telegram_send_count` does not increase from the migration; `db_write_failures=0`.
6. Re-run `tools/phase16e_identity_assessment.py`: `key_mismatch=10`, `reactivation=10`, `id_drift=0`, `duplicate_groups=0` (unchanged — no repair performed).

---

## 9. Constraints / open items

`LIVE_EXECUTION_ENABLED=False`; no broker primitive; MT5/`Varis_SMC` untouched; no push; no repair/restart before explicit approval; only additive, idempotent writes; no row/event/history mutation.

* **Identity unresolved** → quarantine is retained; aligning the indexed key is **not** authorized by current evidence.
* **Lifecycle state deferred** until identity is established.
* **Notification reliability** stays **CONDITIONAL** (the durable delivery ledger is a separate follow-up; the quarantine mechanism prevents this row from generating notifications but does not add delivery guarantees).
* **Signal quality** stays **INSUFFICIENT DATA**.

**Awaiting explicit approval. Nothing was executed.**

---

## 10. Final pre-deployment validation (Phase 16H.2)

The quarantine implementation in this plan has passed its final review: focused **131 passed**; full suite **651 passed, 1 skipped, 0 failed, 0 errored**; `git diff --check` clean. The production server is **stopped**, the production DB is **unchanged** (187 rows, no `opportunity_quarantine` table), the containment backups are unchanged, and `LIVE_EXECUTION_ENABLED=False`.

**Still not authorized / not done:** no production quarantine applied; no identity reconciliation; no lifecycle-state change; the fix is uncommitted and has not passed CI. The GBPAUD row remains identity-inconsistent (indexed `…1.89623` ≠ payload `…1.89628`) and is currently `EXPIRED`. The dry-run-first tool (`tools/phase16g_quarantine_opportunities.py`) and the §7 deployment/rollback sequence remain the operative procedure pending explicit approval. See `PHASE-16H-QUARANTINE-IMPLEMENTATION.md` §10.
