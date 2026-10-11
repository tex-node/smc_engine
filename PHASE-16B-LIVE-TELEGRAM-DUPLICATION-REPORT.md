# PHASE-16B — LIVE TELEGRAM DUPLICATION REPORT

Repository: `tex-node/smc_engine` · Branch: `feature/gui-workstation`
HEAD: `46b671a` (pushed; includes Telegram hardening `dc65e06`). Working tree:
uncommitted (Phase 16 + 16A + 16B). No restart performed during evidence capture.

**Conclusions are kept separate (§12):** (1) ROOT CAUSE CONFIRMED — *for the
recovery/expiry defect only*; (2) DUPLICATE DELIVERY — **not confirmed** for the
reported Telegram repeat; (3) RECOVERY RELIABILITY VERIFIED; (4) SIGNAL QUALITY
INSUFFICIENT DATA.

---

## 1. Timeline and observed references

Reported messages (from the user): GBPUSD READY `1ce1ceddc0` (**repeated, same
reference**); XAGUSD ENTRY_TRIGGERED `5a28cca68f`; EURCHF READY `4db75aeb0b`;
EURCHF ENTRY_TRIGGERED `84e4c9880d`; GBPJPY READY `810c185cf6` (~6 h old);
GBPUSD ENTRY_TRIGGERED `c7eb05552f`. GBPUSD notifications reportedly resumed
~1 h before the report.

Reference format: `_event_ref(opportunity_id, kind, event_id)` =
`SHA-256("<opp>|<kind>|<event_id>")[:10]` (revision `dc65e06`+, present in the
running `46b671a`). All reported references are 10-hex — consistent with that
scheme.

---

## 2. Running processes and loaded revisions (read-only capture)

* **One** smc_engine instance: PID 37192 (child of `.venv` python 31556),
  started `2026-10-11T05:53:27Z`, listening on `127.0.0.1:8765`. Launched via the
  project launcher (`cmd /K … uvicorn smc_engine.web.api:create_app`).
* **`/api/runtime`:** `git_commit=46b671a`, `loaded_git_commit=46b671a`,
  **`stale_code=True`** (on-disk `hub.py` differs from the loaded code — the
  uncommitted Phase 16 change), `pid=37192`.
* **No second smc_engine process; no other DB.** Full Python enumeration shows
  only codex-router, chartasst (`main.py --dry-run`), and the smc_engine server.
  Only one `smc_engine_gui.sqlite3` exists on disk. `SMC_GUI_DB_DIR` unset.
* **Notifier counters (running server):** `telegram_send_count=1`,
  `failure=0`, `deduplicated=0`, `uncertain=0`, `dropped=0`,
  `telegram_last_success=2026-10-11T05:54:15Z`, `telegram_by_symbol={GBPJPY}`.
  → the running server has sent **exactly one** message (GBPJPY) since startup;
  **no GBPUSD**.

---

## 3. Database identity and event records

* Canonical DB: `C:\smc_engine\smc_engine_gui.sqlite3` (read-only copy taken for
  analysis; the operational DB was **not** mutated).
* GBPUSD event kinds present: `OPPORTUNITY_ADVANCED 67`, `OPPORTUNITY_CREATED 9`,
  `EXPIRED 9`, `EXECUTION_READY 7`, `CONVERTED_TO_SETUP 7`,
  `OPPORTUNITY_SUPERSEDED 1`. **There is no GBPUSD `READY` event.**
* Searching **all** events, no `(opportunity_id, kind, evidence_key)` hashes to
  `1ce1ceddc0` under either the current scheme (`opp|kind|eid`) **or** the earlier
  `opp|eid` scheme.

---

## 4. Evidence establishing the origin of repeat deliveries

**No repeat delivery is attributable to the running system.**

* The running revision sent 1 message (GBPJPY); it did **not** send GBPUSD.
* The canonical DB contains **no GBPUSD READY** and **no `1ce1ceddc0`**.
* The reference `5a28cca68f` (XAGUSD ENTRY_TRIGGERED) matches the **Phase 14E
  controlled verification run** (`run_server_verbose.py`, PID 26512/14716) — a
  real Telegram send to the shared chat from a verification instance that was
  then stopped. This indicates the reported "incident" set includes earlier
  **verification/test deliveries**, not (necessarily) production repeats.
* Same-reference repeat is **structurally impossible** in the current revision:
  `notify()` deduplicates on `(opportunity_id, kind, event_id)` in-process
  (`_sent_keys`), and the engine only dispatches **fresh** events
  (`record_event_once`, `UNIQUE(opportunity_id, transition, evidence_key)`); a
  re-emission would change `event_id` and therefore the reference. The same
  reference could only repeat across **independent notifier instances with
  separate databases** (none exist) or a **different build** (not running).
* The specific reference `1ce1ceddc0` could **not** be reproduced or attributed
  from any available process, log, or database. **No root cause for that repeat
  is claimed** (per the brief: do not invent a cause).

---

## 5. GBPJPY apparent ~6-hour age

The running server delivered GBPJPY READY at `05:54:15Z` (its only send). "~6 h
old" corresponds to the **delivery time** being ~6 h before the report, not to
the market event: the notification was emitted at **startup** for a persisted
GBPJPY opportunity (a startup-triggered lifecycle transition), so the message
reflects the *delivery* time, not the *causal anchor* time. The current template
omits event age, so receipt time must not be read as the event time. This is a
**newly emitted transition for a previously persisted opportunity**, not a queue
delay and not a replay of a delivered event.

---

## 6. Root cause and narrow fix

**Recovery/expiry (confirmed, fixed).** The two Phase 16 defects stand: a missing
market timestamp fell back to the **wall clock**, and a **global** `data_now`
could expire other instruments' opportunities. Phase 16 removed the global sweep
(expiry is now per-symbol, market-anchored `_expire_symbol` in `observe`).
Phase 16B closed two residual wall-clock fallbacks:

* `OpportunityEngine.expire_cycle` now treats `now=None` as a **safe no-op**
  (never the wall clock) and requires an explicit market-time reference; it is no
  longer on the scan path. `test_ready_expires_after_ttl` was updated to pass an
  explicit market time (intent preserved).
* **Phase 16C:** `observe()` previously computed `now = view.m15_last_time or
  _now()` and passed that to `_expire_symbol` — so a symbol whose view had **no
  market time** (empty frame / feed outage) would still expire by wall clock.
  `observe` now passes `view.m15_last_time` directly and `_expire_symbol` returns
  early when it is `None`. **No production path now falls back to wall-clock
  expiry.**

Expiry is thus: per-symbol, market-anchored; deferred when market time is
unavailable; still applied when fresh data advances a symbol's own market time
past its TTL.

**Telegram duplication.** No defect in the current revision reproduces the
reported same-reference repeat; no code change is made to the notification path
on this evidence. The deduplication is **two-layered**: an in-process
`_sent_keys` guard and the engine's **durable lifecycle-event** dedup
(`record_event_once`). There is **no durable notification-delivery claim** — this
is a precise gap, not a claim of exactly-once (see §6b). Correctness of
no-repeat therefore depends on the engine not re-emitting a logical event, which
its stable event identity enforces.

---

## 6b. Notification-deduplication guarantees and limitations (Phase 16C)

Code-level answers (files: `opportunity/repository.py`, `telegram.py`,
`web/hub.py`):

1. **What does `record_event_once` persist?** A **lifecycle event**, not a
   delivery claim. `repository.py:52-61` — table `opportunity_events` with
   `UNIQUE(opportunity_id, transition, evidence_key)`; `repository.py:113-127` —
   `INSERT OR IGNORE … commit(); return rowcount > 0`. It records that the
   *event* happened once; it says nothing about Telegram.
2. **Durable delivery-attempt/outcome record?** **None.** The notifier's counters
   (`self._diag`) and `_sent_keys` are **in-memory**; `telegram_send` output goes
   to logging only. No table records a send attempt, its outcome, or a delivery
   claim.
3. **Atomic across two notifier instances sharing a DB?** The **event** claim is
   atomic: the DB `INSERT OR IGNORE` + `UNIQUE` lets exactly one instance's
   `record_event_once` return `True`, so only that instance dispatches
   (`hub._dispatch_opportunity_events` iterates `res["events"]`, which is
   fresh-only). The **delivery** claim (`_sent_keys`) is per-instance and **not**
   shared/durable. Net effect: one instance delivers, but this is a consequence
   of the event claim, not an atomic delivery claim.
4. **Same notification twice after restart?** **Not for stable-identity events:**
   `_sent_keys` resets on restart, but the engine does not re-emit recorded
   events (`record_event_once` returns `False`), so nothing is dispatched.
   **Unproven** if a logical event were re-emitted under a *different*
   `evidence_key` (which would also change the reference).
5. **Ambiguous outcome retried?** **No.** `telegram.py._send_with_retry`:
   timeout / connection-reset / unknown → `_record_uncertain`, **no retry**;
   post-send body-parse error → treated as success (no retry); HTTP 429 →
   bounded retry honouring `retry_after`; `socket.gaierror` / `ConnectionRefusedError`
   (definitive pre-delivery) → bounded retry. No retry can re-send an
   already-accepted message.
6. **What did the 16-event restart test prove?** *Observed, in one controlled
   single-instance run:* 16 distinct events → 16 distinct references, one
   delivery each; repeat scans (~2.5 min) produced no re-send; a restart produced
   **0** re-deliveries. It **did not prove** cross-instance concurrency,
   durability of any delivery claim (there is none), crash-window behaviour, or
   exactly-once under ambiguous network outcomes.

**Distinctions the guarantees rest on:**
* *Process-local deduplication* — `_sent_keys` on `(opportunity_id, kind, event_id)`
  (in-memory, per instance).
* *Durable lifecycle-event deduplication* — `record_event_once`
  (`opportunity_events` UNIQUE).
* *Durable notification-delivery claims* — **absent** (precise gap).
* *Observed no-resend* — demonstrated only in the specific controlled test above.
* *Unproven* — across crashes, concurrent instances with separate DBs, and
  ambiguous delivery outcomes.

**Exactly-once is NOT claimed** (Telegram supplies no remote idempotency key).
Recommended separately-scoped improvement: a durable notification-delivery ledger
(table keyed by the stable `(opportunity_id, kind, event_id)`, written **before**
the external send with an outcome update after), so delivery-claim state survives
restarts and is shared across instances. Not implemented here (out of the
recovery commit's scope).

---

## 7. Tests and full-suite counts

Added (Phase 16B, `tests/test_recovery.py`):
`test_expire_cycle_without_market_time_is_noop`,
`test_per_symbol_expire_cycle_does_not_touch_other_instruments`,
`test_expire_cycle_expires_only_when_own_market_time_advances`.

```
focused (recovery + opportunity engine/cases + telegram delivery)  →  65 passed
pytest -q (full suite)                                             →  608 passed, 1 skipped,
                                                                       0 failed, 0 errored
```
Baseline `46b671a`: 594 passed / 1 skipped. Cumulative Phase 16/16A/16B/16C delta
**+14** (all in `tests/test_recovery.py`; 14 tests). Existing suites cover repeated
scans, concurrent claim, distinct lifecycle delivery, restart no-resend, and WAL
durability; all fixtures use disposable DBs with mocked transport. The operational
DB was not modified by tests (they use per-test temp DBs).

---

## 8. Containment actions and verification

**No containment performed.** No active duplicate sender was found: the running
smc_engine sent one message and is not repeating; `Varis_SMC` is untouched and
was not the source of the reported text (the payloads are the smc_engine
template). No notifier-specific disable control was needed; the user's running
server was **not** stopped (no authorization, and it is not repeating). Evidence
(process list, `/api/runtime`, counters, DB copy) was captured before any change.

---

## 9. Recovery findings kept separate from notification findings

* **Recovery:** confirmed defect (wall-clock fallback + global clock), corrected;
  per-symbol market-anchored expiry; `expire_cycle(now=None)` no-op; restart/
  closed-market causes no artificial expiry; DB identity surfaced. **VERIFIED.**
* **Notification:** no reproducible duplication in the current revision; the
  reported repeat is unattributable. Kept distinct from the recovery fix — no
  causal link asserted.

---

## 10. Safety constraints

`LIVE_EXECUTION_ENABLED=False`. `Varis_SMC` not modified/disabled/removed; no MT5
reconfiguration; no event history deleted; operational DB neither cleared nor
recreated (analysis used a copy); SMC rules unchanged; no synthetic live
Telegram message sent; no commit/push.

---

## 11. Remaining uncertainties

* The origin of the repeated `1ce1ceddc0` is **unresolved** — no process/DB/log
  on the host contains it. Possible but unproven: a transient instance with a
  separate database (none exists now), an earlier build, or an external sender.
* The notification layer has no durable delivery record (in-process claim only);
  a durable delivery ledger would harden cross-instance/cross-restart exactly-once
  beyond the engine's event dedup (recommended follow-up, not implemented here).
* `stale_code=True` on the running server: it loaded `46b671a`; the uncommitted
  Phase 16 changes are not deployed (as the brief notes).

---

## 12. Final decisions (kept separate)

1. **ROOT CAUSE CONFIRMED** — for the recovery/expiry defect (wall-clock
   fallback; global cross-instrument clock). **Not confirmed** for the Telegram
   duplication: the reported repeat is not attributable to the current system.
2. **DUPLICATE DELIVERY CORRECTED** — **cannot be claimed.** No reproducible
   same-reference repeat exists in the current revision; no notification-path
   change was made on this evidence. Reported as an unresolved incident rather
   than a corrected one.
3. **RECOVERY RELIABILITY VERIFIED** — PASS: per-symbol market-anchored expiry;
   `expire_cycle(now=None)` safe no-op; **`observe`/`_expire_symbol` no longer
   fall back to wall clock when market time is absent** (Phase 16C); genuine
   expiry still applies when a symbol's own market time advances; restart/
   closed-market causes no artificial expiry; persisted state survives restart
   and abrupt termination; idempotent reconciliation; DB identity visible.
   `tests/test_recovery.py` **14 tests**; full suite **608 passed / 1 skipped**.
4. **SIGNAL QUALITY: INSUFFICIENT DATA** — no historical OHLC dataset exists;
   unchanged.

**Phase 16C review outcome.** The notification guarantees are now stated
precisely (§6b): process-local dedup (`_sent_keys`), durable lifecycle-event dedup
(`record_event_once`), **no durable delivery claim**, observed no-resend only in
the specific controlled test, and unproven behaviour across crashes / concurrent
instances / ambiguous outcomes. No exactly-once claim is made. The residual
wall-clock expiry fallback in `observe`/`_expire_symbol` was found and closed.

**Review decision: APPROVE WITH LIMITATIONS.** The recovery correction is
code-level justified and regression-tested (all wall-clock fallbacks removed;
per-symbol market anchoring; 14 recovery tests). Limitations: (a) the unresolved
origin of the reported Telegram repeat (unattributable, not a demonstrated defect
in this revision); (b) the absent durable notification-delivery claim — a
separately scoped follow-up (durable delivery ledger) is recommended, not
implemented here; (c) the DB-path resolution still warns rather than hard-fails.
Stopped before committing or pushing.
