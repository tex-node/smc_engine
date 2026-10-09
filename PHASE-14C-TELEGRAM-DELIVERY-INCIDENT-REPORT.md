# PHASE-14C — TELEGRAM DELIVERY INCIDENT REPORT

Repository: `tex-node/smc_engine` · Branch: `feature/gui-workstation`
Baseline commit: `ffe4e18` (CI run `37863201054` — PASS)
Fix revision: local working tree (uncommitted); report lives outside the commit range.

Clearly separated below: **PROVEN FACTS**, **HYPOTHESES**, and **NOT REPRODUCED**.

---

## 1. Executive summary

The reported incident had two symptoms: (a) only GBPUSD reaching Telegram, and
(b) repeated Telegram messages.

**Proven (code + live DB):** the underlying **duplication mechanism** is real and
was reproduced at the persistence layer. The engine derived the logical-event
identity (`evidence_key`) from the **observation clock** (`now`) for terminal and
arbitration events, so every scan minted a *new* identity for the *same* logical
event. The live database proves this: single opportunities carry **35
`ENTRY_TRIGGERED`, 67 `EXPIRED`, 50 `OPPORTUNITY_ADVANCED`** event rows, all with
distinct `…:<scan-time>` evidence keys. The Telegram layer had **no durable
delivery state** and deduplicated only in-process on a **coarse**
`(opportunity_id, kind)` key, so restart-safety depended entirely on that
unstable identity.

**Not reproduced (honest limitation):** end-to-end **duplicate Telegram delivery**
with the *current* committed code. The engine's terminal/state guards prevent the
duplicate re-emission, and restart does not resend. The persisted duplicate rows
are historical artifacts of the pre-Telegram server. No mechanism was found that
causes the *current* code to deliver the same message twice in the tested paths,
and the "GBPUSD-only" symptom is **not** reproduced — routing is symbol-agnostic.

**Fix (smallest, evidence-driven):** make the logical-event identity **stable**
(market-anchor, never wall-clock), expose it to sinks as `event_id`, deduplicate
in the notifier on that **stable identity**, add **per-symbol** scan→delivery
diagnostics, count dropped/uncertain outcomes, and stop retrying on **ambiguous**
outcomes (which could otherwise duplicate a delivery).

**Verdict: CONDITIONAL PASS** (§15).

---

## 2. Confirmed reproduction steps

### 2.1 Persisted duplication (reproduced)
Live DB (`smc_engine_gui.sqlite3`), grouped by `(opportunity_id, kind)`:

```
EXPIRED               rows=67  distinct_evidence=67   (one opportunity)
ENTRY_TRIGGERED       rows=35  distinct_evidence=35   (one opportunity)
OPPORTUNITY_ADVANCED  rows=50  distinct_evidence=50   (one opportunity)
CONVERTED_TO_SETUP    rows=8   distinct_evidence=8
READY                 rows=2   distinct_evidence=2
```

Evidence keys (pre-fix) were `ENTRY_PASSED:<scan_ns>` / `EXPIRED_TTL:<scan_ns>` —
the `<scan_ns>` advances with the market clock every scan, so each scan is a new
identity. The table's `UNIQUE(opportunity_id, transition, evidence_key)` therefore
never deduplicated. **This is the root duplication mechanism.**

### 2.2 Delivery duplication (NOT reproduced with current code)
Deterministic probes on the current committed code:
* `observe()` across 8 successive scans of an entry-touch fixture → exactly **1**
  `ENTRY_TRIGGERED` row (state guard prevents re-emission).
* Hub scan ×3 + restart on a copy of the live DB → scan 1: 5 dispatches, scans
  2–5: **0** dispatches.
* `test_repeated_scan_does_not_resend` / `test_restart_does_not_resend` → 0 extra
  deliveries.
No path produced two *deliveries* for one logical event.

### 2.3 Multi-symbol routing (NOT a defect)
* 3-symbol fixture (GBPUSD/EURUSD/XAUUSD) → 3 independent READY deliveries.
* Live DB copy (real MT5 data) → 5 `OPPORTUNITY_SUPERSEDED` across
  XAUUSD247/XAUUSD/AUDCAD/ETHUSD/AUDUSD in one scan.
* `git grep GBPUSD src/` → no routing/dedup dependency (one UI majors list only).

---

## 3. Root cause (code-level evidence)

`src/smc_engine/opportunity/engine.py`:

* `_terminate()` emitted with `evidence_key = f"{reason}:{_ns(now)}"` (line 616).
* `_supersede_opp()` emitted with `evidence_key = f"superseded:{_ns(now)}"` (line 574).
* `_transition()` used `f"{reason}:{_ns(evidence_time or now)}"` (line 640).

`now` is the scan/observation time (`view.m15_last_time` or `_now()`), which
advances every cycle. The persisted logical-event identity is
`(opportunity_id, transition, evidence_key)` (persistent `opportunity_events`
table, `UNIQUE` constraint, `record_event_once` → `INSERT OR IGNORE`). Because
`evidence_key` moved every scan, behavioural duplicates of the same logical event
were recorded as distinct events.

`src/smc_engine/telegram.py` compounded this:
* Deduplication key was `(opportunity_id, kind)` — **coarse** (suppresses distinct
  lifecycle events of the same kind) and **in-process only** (lost on restart).
* No durable notification-delivery state existed; restart-safety was delegated
  entirely to the engine's (unstable) event identity.
* A post-send body-parse error (`json.loads(resp.read())`) raised *after* the
  message was accepted, and was treated as a failure → **retry → duplicate**.

**HYPOTHESIS (not proven for the current revision):** in a run where the engine
re-emitted a terminal/arbitration event (as the pre-fix code did), a restart
(clearing `_sent_keys`) would have produced a genuine duplicate Telegram
message. The current state guards prevent the re-emission, so this is latent, not
observed.

---

## 4. End-to-end event-path trace

```
poller thread (15 s)                         [UI-independent]
 └─ EngineHub.scan_universe_once()
     └─ for sym in eligible_symbols():        (all 24; no selection/browser input)
         ├─ _causal_view(sym)                 (authoritative causal view)
         ├─ OpportunityEngine.observe(sym, view)
         │    ├─ _reconcile_startup()  (once per engine; arbitration)
         │    ├─ _discover_reversal/_continuation → arbiter → _arm
         │    ├─ _advance_symbol → _transition/_terminate/_check_entry/_promote
         │    └─ result["events"]  ← ONLY fresh events (record_event_once)
         └─ _dispatch_opportunity_events(res)
              ├─ web.add_alert(...)            (SQLite alerts)
              ├─ events.publish(kind, ev)      (SSE)
              └─ telegram.notify(ev, opp)      (opp re-read from repo)
                   └─ queue (50) → daemon worker → _send_with_retry → HTTPS
```

Both SSE and Telegram consume the **same** `result["events"]` objects; nothing
downstream recomputes sweep/CSD/POI/IDM/entry/risk/setup. `telegram.py` has no
engine imports. Instrumentation added: `event_id` is now carried on every
dispatched event and on every per-symbol pipeline counter.

---

## 5. Why GBPUSD appeared to be the only notifying symbol

**NOT REPRODUCED — no defect found.** Routing is symbol-agnostic:
`scan_universe_once` iterates `eligible_symbols()` and dispatches per symbol;
there is **no** GBPUSD-specific selection, cache, filter or fallback in the
opportunity→dispatch→notifier path (`git grep GBPUSD src/` shows only a UI
majors list). Multi-symbol delivery is demonstrated for 3 fixture symbols and 5
live-DB symbols.

Attribution of the *appearance* (HYPOTHESIS): the Phase 14B acceptance evidence
was GBPUSD-centric, and the old coarse notifier key `(opportunity_id, kind)`
suppressed all but the first event of a given kind for an opportunity — a busy
single chain can dominate a small sample and look like a single-symbol feed. This
is a masking artifact, not a routing bug, and is removed by the stable-identity
dedup.

---

## 6. Why repeated notifications occurred

The **mechanism** is the unstable event identity (§3): the same logical event was
persisted under a new `evidence_key` each scan, so the engine regarded it as new
every time. The Telegram layer's only dedup was the in-process coarse key, which
masked the duplication *within* a process but is reset on restart. Therefore any
re-emission of a logical event after a restart would be delivered again.

**PROVEN:** the persisted duplication (35/67/50 rows). **NOT PROVEN:** an actual
duplicate *delivery* under the current code (state guards suppress re-emission).

Distinction preserved: (1) same logical event → now one stable identity; (2)
multiple records for one logical event → eliminated at the source; (3) genuinely
distinct lifecycle events → still distinct and still delivered; (4) retries after
an ambiguous network result → now **not** retried (see §9).

---

## 7. Exact files and behavior changed

| File | Change |
|---|---|
| `src/smc_engine/opportunity/engine.py` | New `_event_anchor()` / `_event_identity()` — stable, market-anchor-derived identity (never wall-clock). `_terminate`, `_transition`, `_supersede_opp` now emit stable identities. Dispatched events now carry `event_id`. |
| `src/smc_engine/telegram.py` | Dedup key `(opportunity_id, kind, event_id)` (stable logical identity). Queue items carry `symbol/kind/event_id/text`. Per-symbol pipeline counters (`queued/delivered/failed/uncertain/deduplicated/dropped`). New `telegram_uncertain_count`, `telegram_dropped_count`, `telegram_by_symbol`. Defensive body parse (a 2xx with unparseable body is a success, not a retry). Ambiguous outcomes are **not** retried. |
| `tests/test_telegram_delivery.py` (new) | 19 deterministic tests (multi-symbol, dedup, restart, concurrency, failure/recovery, queue, ambiguity, credential safety, stable-identity regression). |
| `PHASE-14C-TELEGRAM-DELIVERY-INCIDENT-REPORT.md` (new) | this report. |

No SMC methodology, timeframe, confirmation rule, risk parameter, state-machine
semantics, gate, or execution path was changed.

---

## 8. Persistent deduplication and restart semantics

* **Durable record:** the persistent `opportunity_events` table with
  `UNIQUE(opportunity_id, transition, evidence_key)` is the durable dedup store.
  With a **stable** `evidence_key` it now deduplicates genuinely-repeated
  processing of the same logical event across restarts.
* **In-process guard:** notifier `_sent_keys` keyed by
  `(opportunity_id, kind, event_id)` under a lock — concurrent dispatchers cannot
  both claim the same event (`test_concurrent_consumers_claim_once`).
* **Restart:** verified 0 resent deliveries (`test_restart_does_not_resend`).
* **Scope:** key includes opportunity identity and the stable event identity, so
  distinct symbols and distinct lifecycle events never collide
  (`test_different_symbols_same_event_id_do_not_collide`,
  `test_distinct_lifecycle_events_same_opportunity_delivered`).

---

## 9. Network-timeout and retry trade-offs

Documented failure policy in `_send_with_retry`:

| Outcome | Class | Action |
|---|---|---|
| HTTP 429 | rate-limited | retry (bounded, honours `retry_after`, capped 60 s) |
| `URLError` (DNS / connection refused) | definitive pre-delivery | retry (≤ `_MAX_RETRIES`) |
| 2xx with unparseable body | delivered | **success** (no retry → no duplicate) |
| `TimeoutError` / `socket.timeout` | ambiguous | **no retry**; counted `uncertain` |
| other exceptions | ambiguous | **no retry**; counted `uncertain` |

**Trade-off (explicit):** Telegram's Bot API has **no idempotency key**; if the
server accepts a message but the client loses the response, exactly-once cannot
be guaranteed. This implementation prefers **at-most-once for ambiguous
outcomes** (no retry → possible drop, never a duplicate) and **bounded
at-least-once for definitive pre-delivery failures**. Uncertain events are
reported via `telegram_uncertain_count` and are **not** falsely counted as
confirmed delivery. Reconciliation path: uncertain events remain visible in
diagnostics and in the opportunity/alert history for manual follow-up.

---

## 10. Before/after diagnostic evidence

| Signal | Before | After |
|---|---|---|
| Terminal event identity | `ENTRY_PASSED:<scan_ns>` (changes each scan) | `ENTRY_PASSED:<anchor_ns>` (stable) |
| Rows for one logical terminal event (probe, 8 scans) | 35 in live history | **1** |
| Notifier dedup key | `(opportunity_id, kind)` | `(opportunity_id, kind, event_id)` |
| Durable delivery state | none (engine-dependent) | engine store + stable id |
| Per-symbol counters | none | `telegram_by_symbol[symbol]{queued,delivered,failed,uncertain,deduplicated,dropped}` |
| Ambiguous-outcome counter | none | `telegram_uncertain_count` |
| Queue-overflow counter | none | `telegram_dropped_count` |
| Post-send parse error | retried (duplicate risk) | success (no retry) |

No credentials appear in any diagnostic (verified).

---

## 11. Multi-symbol acceptance results

Deterministic experiment, 3 symbols (GBPUSD/EURUSD/XAUUSD), transport mocked:

| Stage | Result |
|---|---|
| Candidate events | 12 (4 per symbol) |
| Persisted events | 12 unique identities |
| Dispatch attempts | 12 (EURUSD 4, GBPUSD 4, XAUUSD 4) |
| Deduplicated | 0 |
| Successful mock deliveries | 3 (1 READY per symbol) |
| Replay of delivered events | 0 new deliveries |
| Restart (new notifier, same DB) | 0 new deliveries |
| Legitimate later lifecycle event | 1 delivery |
| Injected delivery failure (one symbol) | isolated: other symbols delivered; failed symbol counted (`uncertain`) |

Regression tests assert the same properties deterministically
(`tests/test_telegram_delivery.py`).

**Live application:** the running workstation on `:8765` predates Telegram (no
`telegram_*` diagnostics) and is stale code; the live DB's duplicate rows are
attributed to that pre-Telegram server. The current engine, run against a copy of
the live DB with real market data, produced only 5 fresh cross-symbol
supersessions then silence — no repeats. The original incident is therefore
**explained as legacy-data-driven**, not as a live defect in the current revision.

---

## 12. Regression-test results and full-suite counts

```
pytest tests/test_telegram.py                  ->  23 passed
pytest tests/test_telegram_delivery.py         ->  19 passed
pytest -q (full suite)                         ->  571 passed, 1 skipped, 0 failed, 0 errored
```

Baseline `ffe4e18`: **552 passed, 1 skipped**. Delta: **+19** (the new suite).
No unrelated regressions; existing `ENTRY_TRIGGERED`, supersession and
live-execution safety tests all pass. No pre-existing failures were observed.

No formatter/linter/type-checker is configured in the repository (no
ruff/flake8/mypy/black in `pyproject.toml` or the venv), so none was run.

---

## 13. Security and live-execution safety review

* `LIVE_EXECUTION_ENABLED = False`; `POST /api/live/x → 403`; DEMO account.
* No broker order / pending / position created by the notification path; tests
  assert zero broker sends.
* `git grep` over the changed source files for
  `OrderSend|order_send|order_check|TRADE_ACTION|CTrade|MetaTrader5` → **none**.
  Telegram remains outbound-only (no polling, no command handler).
* No credential (token/chat-id) appears in this report, code, tests, logs,
  diagnostics, or the diff (scanned). `telegram.txt` and `.venv/telegram.txt`
  remain gitignored and untracked; nothing was staged. Unrelated untracked audit
  documents were not modified.

---

## 14. Remaining limitations and follow-up actions

1. **Duplicate *delivery* was not reproduced** against the current code; only the
   persisted duplication was. The fix removes the mechanism, but the exact
   reported live symptom cannot be re-created from the current revision. **Do not
   treat the incident as fully closed on delivery evidence alone.**
2. **Legacy rows:** the live DB retains historical duplicate event rows and
   events with unstable keys. They are terminal/idempotent and are not
   re-dispatched; a one-off cleanup is optional (not performed here).
3. **Ambiguous outcomes** may drop a message (at-most-once preference); uncertain
   events are visible in diagnostics for reconciliation.
4. **`_sent_keys` is unbounded in-process** (bounded in practice by the
   opportunity count); a future cap/TTL is advisable.
5. **New-revision CI is unverified.** The fix is uncommitted and unpushed; CI
   `37863201054` certifies only `ffe4e18`. A new CI run is required for this
   revision.

---

## 15. Final acceptance decision

# CONDITIONAL PASS

**Justification.**
* **Fixed / proven:** the duplicated-event mechanism (unstable wall-clock event
  identity) is confirmed by code and by 35/67/50 live rows, and is corrected with
  a stable market-anchor identity; the notification layer is now keyed on a
  stable logical identity, restart-safe (0 resends), concurrency-safe, and
  exposes per-symbol scan→delivery diagnostics; ambiguous outcomes no longer
  risk duplicate delivery.
* **Multi-symbol delivery proven:** 3 fixture symbols and 5 live-DB symbols
  independently reach the notifier; the "GBPUSD-only" symptom is not a defect.
* **Conditions:** (a) the *delivery* duplication itself was **not reproduced**
  against the current revision — a documented limitation, not a claimed fix;
  (b) CI for this revision is unverified (uncommitted/unpushed; only `ffe4e18`
  is CI-certified). Push + green CI, plus retention of the DC-1 limitation in
  §14, are required to move to PASS.
