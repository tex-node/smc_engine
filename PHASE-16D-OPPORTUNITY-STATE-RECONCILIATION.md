# PHASE-16D — OPPORTUNITY STATE RECONCILIATION (read-only audit)

**Repository:** `C:\smc_engine` · **Branch:** `main` (per `session.md`) · **HEAD:** `09c7ce2de21d3c78fb066917e25637bdfe04fdfc` (`09c7ce2`)
**Canonical DB:** `C:\smc_engine\smc_engine_gui.sqlite3` (WAL active)
**Audit type:** read-only. No application code changed, nothing committed/pushed, no production SQL writes, no restart.

---

## 0. TL;DR

The reported change (**EXPIRED 114→113, WAITING_FOR_CONFIRMATION 0→1**, total 185 unchanged) is **not** a reporting/query discrepancy and **not** a concurrent update. It is a **real, single-row state mutation** performed by the **newly deployed instance** at **`2026-10-11T13:21:30.5Z`** (≈56 s after it started at `13:20:34.5Z`):

> `OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH` was re-armed `EXPIRED → OPPORTUNITY_ARMED → WAITING_FOR_CONFIRMATION`.

Two code facts enable it and one deployment fact explains why it *sticks* now:

1. **Identity collision (pre-existing defect).** `opportunity_id_from_key()` **drops `anchor_level`** from the id, while `canonical_opportunity_key()` **includes it**. Two sweeps at the same time with slightly different swept levels therefore share an **opportunity_id** but have **different canonical_keys**.
2. **Guard bypass + non-updating column.** `_discover_reversal()` guards duplicates via `repo.get_by_key(canonical_key)`; a re-observed sweep with a drifted level (`…1.89628` vs stored `…1.89623`) is **not found**, so `_arm()` runs. `upsert()`'s `ON CONFLICT(opportunity_id) DO UPDATE` **does not update the `canonical_key` column**, so it silently overwrites the existing (terminal) row's `state`/`payload` while leaving the stale column key.
3. **Deployment effect.** The old revision (`46b671a`, stale `hub.py`) re-armed the same zombie at `05:54:06Z` and then **re-expired it by wall-clock at `05:54:28Z`**. The new revision (`09c7ce2`) uses **per-symbol, market-anchored expiry**; GBPAUD's market time is `2026-10-09T20:30Z`, which is **before** its `expires_at` (`2026-10-10T04:00Z`), so the re-armed row is **no longer re-expired** and persists as `WAITING_FOR_CONFIRMATION`.

The change emitted **no new event row** (durable event dedup by `(opportunity_id, transition, evidence_key)`), which is why the deployment scan was described as producing "no events". The count delta is therefore **real and introduced by the deployment**, contradicting `PHASE-16-RECOVERY-AND-SIGNAL-QUALITY-AUDIT.md` §14's "read-timing ambiguity / current revision cannot resurrect it" explanation.

**No production records were modified by this audit; the server was not restarted.**

---

## 1. Runtime identity / process state (evidence)

Read-only `GET /api/runtime` (live), plus OS process inspection:

| Field | Value |
|---|---|
| Listener | **PID 37656** on `127.0.0.1:8765` (only owner of the port) |
| Process tree | launcher `34052` → `.venv` python **20812** → system python **37656** (listener) — the documented launcher chain; a single logical instance |
| `git_commit` | `09c7ce2` |
| `loaded_git_commit` | `09c7ce2` |
| `stale_code` | **`false`** |
| `files_modified_after_startup` | `[]` (fingerprint files: setup/causal/poi/lifecycle/structure/execution_structure/hub/history) |
| `server_started_at` | **`2026-10-11T13:20:34.5Z`** |
| `python_executable` | `C:\smc_engine\.venv\Scripts\python.exe` |
| `account` | `477217728@Exness-MT5Trial9` DEMO, `account_identity=AUTHORIZED` |
| `live_execution_enabled` | `false` |
| `recovery.counts_at_startup` | `{ENTRY_TRIGGERED:51, EXPIRED:114, INVALIDATED:12, SUPERSEDED:8}` |
| `recovery.counts_current` | `{ENTRY_TRIGGERED:51, EXPIRED:113, INVALIDATED:12, SUPERSEDED:8, WAITING_FOR_CONFIRMATION:1}` |

`git_dirty=true` is only from untracked/modified **report** docs (e.g. `PHASE-16-…AUDIT.md`, unstaged `PHASE-10/13/14` reports); no fingerprint file differs from startup → `stale_code=false`.

---

## 2. Count semantics — exactly which query

The "185 / 114 / 113 / 0 / 1" figures are **current opportunity rows** aggregated by the indexed `opportunities.state` column, not events and not a derived funnel-only view.

* Schema (`src/smc_engine/opportunity/repository.py:31-68`): `opportunities(opportunity_id PK, canonical_key UNIQUE, symbol, symbol_norm, direction, opportunity_type, state, created_at, updated_at, expires_at, first_seen, last_seen, setup_id, risk_status, reason, blocker, next_expected, payload_json)`. Events are a **separate** table `opportunity_events(id, opportunity_id, at, transition, evidence_key, kind, message, UNIQUE(opportunity_id,transition,evidence_key))`. State transitions are a third table `opportunity_state_history`.
* State count query (`repository.py:207-210`):
  ```sql
  SELECT state, COUNT(*) FROM opportunities GROUP BY state
  ```
* `EngineHub.funnel()` (`engine.py:693-710`) maps `EXPIRED → expired_count`, `WAITING_FOR_CONFIRMATION → waiting_for_confirmation`, etc.; `total = sum(counts)`.
* Row state is written only by `upsert()` (`repository.py:74-99`) from `Opportunity.state`; the same value is serialized into `payload_json`.

So the reported numbers are `state_counts()` on the `opportunities` table. Confirmed identical semantics on both snapshots (row-state column **and** `payload_json.state` agree in every row: 0 mismatches).

---

## 3. Before / after snapshots (identical query semantics)

All detailed SQL was run against **copies**.

| Snapshot | When | How obtained |
|---|---|---|
| Pre-deployment backup | `2026-10-11T13:19:37Z` (file `smc_engine_gui_20261011_141937.sqlite3`) | provided; opened read-only |
| Current | `2026-10-11T13:44:19Z` | fresh **SQLite backup-API** snapshot of the live DB → `%TEMP%\opencode\smc16\smc_current_snapshot.sqlite3` |

`SELECT state, COUNT(*) FROM opportunities GROUP BY state`:

| State | BACKUP 13:19:37Z | CURRENT 13:44:19Z | Δ |
|---|---|---|---|
| ENTRY_TRIGGERED | 51 | 51 | 0 |
| EXPIRED | **114** | **113** | **−1** |
| INVALIDATED | 12 | 12 | 0 |
| SUPERSEDED | 8 | 8 | 0 |
| WAITING_FOR_CONFIRMATION | **0** | **1** | **+1** |
| **Total** | **185** | **185** | **0** |

`SELECT COUNT(*) FROM opportunity_events`: **1541 → 1541 (unchanged)** — the mutation emitted **no** new event.
Per-row consistency (both snapshots): `row.state != payload.state` = **0**; `row.state != latest state_history.to_state` = **0**.
Lifecycle duplication (both snapshots, unchanged): **6** opportunities have >1 `EXPIRED` event (max **123** on one), **25** duplicate `(opportunity_id,to_state,reason)` state-history groups — the historical pre-Phase-14C duplicate-emission artifact.

**The single differing row is the GBPAUD opportunity** (the only non-terminal row carrying an `EXPIRED` event: `non-terminal rows WITH an EXPIRED event = [OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH, WAITING_FOR_CONFIRMATION]`).

---

## 4. GBPAUD opportunity — authoritative row + full timeline

**Authoritative persisted row** (`opportunity_id` = `OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH`):

| Field | BACKUP 13:19:37Z | CURRENT 13:44:19Z |
|---|---|---|
| `state` (column) | EXPIRED | **WAITING_FOR_CONFIRMATION** |
| `payload_json.state` | EXPIRED | **WAITING_FOR_CONFIRMATION** |
| `canonical_key` (**column**) | `REVERSAL:GBPAUD:BULLISH:SWEEP:1791518400000000000:**1.89623**` | same (`…**1.89623**`) |
| `payload_json.canonical_key` | `…SWEEP:1791518400000000000:**1.89628**` | same (`…**1.89628**`) |
| `created_at` | `2026-10-09T07:45:00Z` | unchanged |
| `updated_at` | `2026-10-11T05:30:00Z` | `2026-10-09T20:30:00Z` |
| `expires_at` | `2026-10-10T04:00:00Z` | unchanged |
| `last_seen` | `2026-10-09T20:30:00Z` | unchanged |

Note the **column/payload key divergence** (`…1.89623` vs `…1.89628`) — the fingerprint of the collision in §6.

**Events (all 31, unchanged between snapshots; timestamps UTC):**
```
2026-10-09T08:00:22.545Z OPPORTUNITY_CREATED  SWEEP:1791518400000000000
2026-10-09T08:00:22.551Z OPPORTUNITY_ADVANCED WAITING_FOR_CSD:1791531900000000000
… (25 more OPPORTUNITY_ADVANCED, WAITING_FOR_CSD:<advancing_ns>) …
2026-10-09T14:45:47.604Z OPPORTUNITY_ADVANCED WAITING_FOR_CSD:1791556200000000000
2026-10-10T11:23:06.106Z EXPIRED              EXPIRED_TTL:1791518400000000000
2026-10-10T11:23:30.613Z OPPORTUNITY_ADVANCED WAITING_FOR_CSD:1791518400000000000
```
(No event after `10-10T11:23:30Z`; the `13:21:30Z` transition was deduplicated.)

**State history tail (CURRENT; the backup ends at the `05:54:28Z` EXPIRED line):**
```
2026-10-11T05:54:06.455Z IDLE → OPPORTUNITY_ARMED                  armed        (old instance, 46b671a)
2026-10-11T05:54:06.459Z OPPORTUNITY_ARMED → WAITING_FOR_CONFIRMATION WAITING_FOR_CSD
2026-10-11T05:54:28.487Z WAITING_FOR_CONFIRMATION → EXPIRED        EXPIRED_TTL  (old: wall-clock expiry)
2026-10-11T13:21:30.515Z IDLE → OPPORTUNITY_ARMED                  armed        (NEW instance, 09c7ce2)
2026-10-11T13:21:30.518Z OPPORTUNITY_ARMED → WAITING_FOR_CONFIRMATION WAITING_FOR_CSD
```

The transition at **`13:21:30.5Z`** is **after** the new server's start (`13:20:34.5Z`) and after its first per-symbol analysis (`GBPAUD.last_analysis_time = 13:20:49Z`) → produced by the **new revision**.

---

## 5. Code path that performed the mutation

1. `EngineHub.scan_universe_once` calls `OpportunityEngine.observe(symbol, view)`; the first scan after startup analyzed all FX symbols against the stale `2026-10-09 20:30Z` market data and re-discovered the `10-09` GBPAUD sweep.
2. `_discover_reversal()` (`engine.py:150-179`) computes `canonical_opportunity_key(...)` and does `if self.repo.get_by_key(key) is not None: continue`. The key **includes** `round(anchor_level,8)`; the stored **column** key is `…1.89623`, the recomputed key is `…1.89628` → **not found** → guard bypassed.
3. `_arm()` (`engine.py:106-139`) constructs a fresh `Opportunity` and `repo.upsert()`. Because `opportunity_id_from_key()` **drops the level**, the new id equals the existing id → `INSERT … ON CONFLICT(opportunity_id) DO UPDATE` overwrites `state`/`payload_json` but **not** `canonical_key` (`repository.py:84-90`). `record_state_change(IDLE → OPPORTUNITY_ARMED)` is written.
4. `_advance_reversal()` (`engine.py:341-344`) transitions `OPPORTUNITY_ARMED → WAITING_FOR_CONFIRMATION`.
5. `_expire_symbol()` uses **this symbol's market time** (`2026-10-09 20:30Z`); `expires_at = 2026-10-10T04:00Z` is **in the future** relative to market time → **no expiry**.
6. `_emit()` → `record_event_once()` deduplicates (`OPPORTUNITY_CREATED` evidence `SWEEP:1791518400000000000` already exists) → **no new event**.

`diag.opportunities_created = 11` (11 `_arm` calls this process) with only **one** visible funnel change: the other ~10 re-armed rows had `expires_at < market time` and were immediately re-expired (net EXPIRED→ARMED→EXPIRED = unchanged).

---

## 6. Concurrency check (not a concurrent writer)

* Only **one** process binds `127.0.0.1:8765` (PID 37656); `/api/runtime` reports a single `server_started_at`. The `20812` process is the launcher parent (documented chain), not a second listener.
* The mutation timestamp `13:21:30.5Z` sits inside the **new** process's lifetime and after its first symbol analysis (`13:20:49Z`), i.e. it is the new instance's own scan — not an old process, not a second writer, not a read at two different times.
* `db_write_failures=0`, `db_lock_retries=0`, `db_lock_failures=0`; SQLite WAL, single-writer.
* `last_scan` (latest, `13:45:44Z`): `symbols_skipped=24`, `events=[]` — the closed-market gate skips FX *now*, but the **first** post-start scan (`~13:20:49Z`) analyzed them and performed the re-arm.

**Conclusion: no concurrent update. The delta is deterministic re-arm behaviour on startup.**

---

## 7. Finding

* **Unexpected state mutation after deployment: YES — exactly one row** (`OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH`), `EXPIRED → WAITING_FOR_CONFIRMATION`, at `13:21:30.5Z`, by revision `09c7ce2`.
* **Root cause (confirmed):** `opportunity_id_from_key()` omits `anchor_level` while `canonical_opportunity_key()` includes it (identity collision), combined with `get_by_key()` duplicate-guard bypass and an `upsert` whose `ON CONFLICT` **does not update `canonical_key`**. On every restart the drifting-level sweep re-arms the same row; the new revision's **per-symbol market-anchored expiry** (no wall-clock) then fails to re-expire it during the closed market, so it persists as `WAITING_FOR_CONFIRMATION` (the old revision re-expired it immediately).
* **Corrects the prior record:** `PHASE-16-…AUDIT.md` §14 ("read-timing ambiguity"; "`09c7ce2` … neither expires nor reactivates it") is **not supported** by the data — the row *was* reactivated by `09c7ce2` at `13:21:30Z`.
* **Not a reporting/aggregation discrepancy:** both snapshots use identical semantics; the row genuinely changed state.
* **Residual:** the same collision could affect the ~11 re-armed rows on each startup (they re-expire, so the funnel is unchanged, but the underlying identity/key inconsistency remains). The `opportunity_events` table is unchanged (1541), so nothing was re-notified.

---

## 8. Compliance / safety confirmations

* **Production records were NOT modified.** All investigative SQL ran against read-only copies: the supplied backup and a fresh SQLite **backup-API** copy written to `%TEMP%\opencode\smc16\`. No `INSERT/UPDATE/DELETE/CREATE/DROP`, no migrations, no repairs against `smc_engine_gui.sqlite3`.
* **The server was NOT restarted** and no process was signalled. Only read-only HTTP `GET`s (`/api/runtime`, `/api/opportunities/diagnostics`) were issued.
* `LIVE_EXECUTION_ENABLED=False` confirmed; `Varis_SMC` untouched; no broker primitive; no Telegram send.
* No application code changed; nothing committed, staged, or pushed.

## 9. Recommended follow-up (no change made here)

1. Make the opportunity id **level-inclusive** (or drop the level from the canonical key) so id and key are 1:1.
2. Have `upsert()` update `canonical_key` (or resolve by `canonical_key` before insert).
3. Ignore/short-circuit **terminal** rows in `_discover_*` (`get_by_key` is not sufficient when the key can drift) so a restart cannot re-arm a terminal opportunity.
4. Read-only re-check after a fix: `state_counts()` must remain `{EXPIRED:113+1, WAITING:0}` (i.e. no re-arm) across a restart, and the row/payload `canonical_key` must agree.

## 10. Resolution — Phase 16E (local, uncommitted)

The four recommended follow-ups in §9 were implemented and regression-tested:

1. **Identity enforcement at the mutation boundary** (`repository.upsert` +
   `apply_transition`): an `opportunity_id` may bind to exactly one `canonical_key`;
   a write whose incoming key differs from the persisted key is refused
   (`OpportunityIdentityConflict`) with a safe diagnostic.
2. **Terminal immutability**: a terminal row can never be written to a non-terminal
   state (`TerminalStateImmutable`); terminal→terminal reclassification is allowed.
3. **Identity-aware discovery guard**: `_discover_reversal`/`_discover_continuation`
   now check the derived `opportunity_id` (not just the level-inclusive key), so the
   drifted-level re-observation is skipped with a diagnostic instead of re-arming.
4. **Atomic transition + event consistency**: `apply_transition()` commits state +
   history + event in one transaction; the dedup key includes the concrete `from->to`
   transition, so a genuine new transition is not suppressed by a reused evidence key.

**Reproduction (proof the test catches the defect):** the minimal reproduction
(`OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH`, levels 1.89623/1.89628)
returns `RESULT_AFTER_COLLIDING_UPSERT: OPPORTUNITY_ARMED` on the pre-fix code and
`BLOCKED: OpportunityIdentityConflict` / `FINAL_STATE: EXPIRED` on the fix.
`tests/test_identity_integrity.py` (16 tests) covers identity collision, terminal
immutability, event/history atomicity, dedup semantics, and concurrency.

**Migration assessment** (`tools/phase16e_identity_assessment.py`, read-only snapshot,
recomputed `2026-10-11T19:13Z`): `total=186 · key_mismatch=10 · id_drift=0 ·
reactivation=10 · history_gt_events=11 · potential_duplicate_groups=0`. Of the 10
key-mismatch rows, 8 are terminal and need no action (the indexed column key is
authoritative; the payload key is the drifted duplicate); 1 is the live GBPAUD
resurrection; and 1 further row (`OPP-REVERSAL-GBPUSD-SWEEP-1791374400000000000-BULLISH`)
shows terminal→active with no key drift. The last two are classified
**QUARANTINE / MANUAL REVIEW REQUIRED**. `history_gt_events=11` is the pre-fix
re-arm/dedup artifact (history row written, event suppressed by the old
`(opportunity_id, transition=kind, evidence_key)` key) — not a lost transition.
**No production repair was executed**; every repair is proposed only and must run in a
disposable copy under a separately approved migration. The row-by-row table and the
four acceptance gates are in `PHASE-16-RECOVERY-AND-SIGNAL-QUALITY-AUDIT.md` §16.

**Status: the defect is fixed locally and regression-proven; the reconciliation
finding is confirmed.** No production data was modified, no service was restarted,
nothing was committed or pushed.
