# PHASE-14E — TELEGRAM SENDER ATTRIBUTION REPORT

Repository: `tex-node/smc_engine` · Branch: `feature/gui-workstation`
HEAD: `23f4649` · Working tree: uncommitted (this phase). No secrets printed.

**Conclusion: the duplicated message was produced by the smc_engine Python
pipeline — not by `Varis_SMC`.** The exact payload contains strings that exist
only in `src/smc_engine/telegram.py` + `engine.py` and the **uncommitted**
`_SOURCE_TAG`, which no MQL5/other component can emit. This corrects the Phase 14D
attribution. The exact repeat instance could not be reconstructed because the
environment was reset mid-investigation (see §4/§10).

---

## 1. Exact message fingerprint

Received payload (body):

```
SMC OPPORTUNITY READY

GBPUSD
BULLISH · REVERSAL

State:
READY FOR MITIGATION

Next:
POI mitigation / entry

Gate:
OBSERVATION ONLY

— source: smc_engine (python pipeline)
```

String-provenance (repo-wide `git grep`, working tree + all history + the Varis
EA source):

| Fragment | Produced by | Introduced |
|---|---|---|
| `SMC OPPORTUNITY READY` | `src/smc_engine/telegram.py:90` (`_format_message`, READY branch) | `ffe4e18` (Phase 14B) |
| `READY FOR MITIGATION` | same READY branch | `ffe4e18` |
| `POI mitigation / entry` | `src/smc_engine/opportunity/engine.py:471` (`_to_ready` → `next_expected`) | Phase 8/9 |
| `— source: smc_engine (python pipeline)` | `src/smc_engine/telegram.py:45` (`_SOURCE_TAG`) | **uncommitted working tree only** |

`_format_message("READY", "GBPUSD", opp)` renders the body **byte-for-byte** as
received (verified this phase). `Varis_SMC.mq5` contains **none** of these
strings (`git grep`/source scan). Message fingerprint (SHA-256[:10]) of the body
above = stable; the full received payload also carried the source tag.

**Proven:** the payload is emitted by the smc_engine Python READY formatter plus
the uncommitted source tag. No other sender can produce it.

---

## 2. Confirmed sender

**The smc_engine Python pipeline** (`src/smc_engine/telegram.py` loaded by a
workstation server process). The footer `_SOURCE_TAG` exists only in the
uncommitted working tree, so the sending process loaded the working-tree code
(after Phase 14D added the marker).

**Not** `Varis_SMC` (MT5) — it cannot produce any of the four fragments.

---

## 3. Process / runtime evidence

* **Current state:** the environment was reset (all PIDs and start times changed
  at 2026-10-09 ~15:51–15:52). At investigation time there is **no** smc_engine
  server running: port `8765` has **no** listener and no `uvicorn`/`smc_engine`
  Python process exists. Only `codex-router` and `chartasst` (MT5 disconnected,
  idle) are running.
* **Prior (pre-reset) state, captured earlier this session:** the single
  `127.0.0.1:8765` listener was PID 34248, which had **loaded obsolete,
  pre-Telegram code** (started `2026-10-08T04:08:09Z`; `/api/opportunities/
  diagnostics` had **no** `telegram_*` keys). That instance could **not** send.
  The payload proves a **different, later** process — one that loaded the
  working-tree Telegram code — produced the message. That process is no longer
  alive (reset).
* **DB:** `db_dir = os.environ["SMC_GUI_DB_DIR"]` (default `.` = cwd); only one
  `smc_engine_gui.sqlite3` exists (the repo DB, mtime 2026-10-09 16:39). It
  contains **no GBPUSD `READY` event** (GBPUSD kinds: 67 ADVANCED, 9 CREATED, 8
  EXPIRED, 7 EXECUTION_READY, 7 CONVERTED_TO_SETUP, 1 SUPERSEDED). So the
  sending server either used a fresh/other DB or the event was not persisted
  there → the exact opportunity/event id is **not recoverable**.
* **Other senders:** `Varis_SMC` (MT5 "Copy", PID 30344 in the prior session)
  uses the **same bot token and chat** as `smc_engine/telegram.txt` (verified)
  and was the only Telegram TCP peer ever observed. `ChartAsst`
  (`C:\chartasst\app\notifier.py`) targets the **same chat** with a **different**
  bot token but its MT5 is disconnected (idle). `_SOURCE_TAG` is unique to the
  repo, so neither produced the received payload.

---

## 4. Explanation of the two identical messages

The READY message body contains **no unique identifier** (no opportunity id, no
event id, no anchor). Therefore **any two distinct `GBPUSD` BULLISH REVERSAL
READY events render byte-identical text** — a documented hazard in §4 of the
brief ("separate opportunity transitions produce indistinguishable payloads").

Two candidate mechanisms, and their code-level status:

| Mechanism | Code status |
|---|---|
| Two **distinct** opportunities (two GBPUSD sweeps) both READY → identical text | **Possible** — the formatter omits any id; the message is genuinely identical for distinct events |
| True **re-delivery of one event** by one instance | **Ruled out in normal flow** — Phase 14C gives READY a stable identity (`_event_identity`); durable `record_event_once` + notifier `(opp_id, kind, event_id)` dedup ⇒ one send |
| Re-delivery via **two instances** | Durable dedup should prevent it (shared DB → only one `INSERT` wins). Only possible with **separate DBs** |
| Re-delivery via **ambiguous retry** | **Was possible**: a connection reset raised `URLError`, which the pre-Phase-14E policy retried — a duplicate-delivery path. **Fixed this phase** |

Because the sender's process/DB were gone (reset) and the payload carried no id,
**which mechanism occurred is NOT conclusively established.** The two messages
are either (a) two distinct events (identical text), or (b) a true duplicate via
the ambiguous-retry path (now closed) or a second instance.

---

## 5. Root cause and correction

Two evidence-backed defects, both addressed:

1. **No source attribution / no structured send diagnostics.** The pipeline could
   not be told apart from another sender sharing the bot/chat, and there was no
   per-send record (pid, event id, outcome) to distinguish a true duplicate from
   distinct events. → **Fix:** `_SOURCE_TAG` (kept) + structured
   `telegram_send pid=… ref=… fp=… symbol=… kind=… event_id=… outcome=…` logging
   before and after every send (secret-safe).
2. **Indistinguishable payloads.** The message omitted any event identifier, so
   distinct events were byte-identical and a genuine re-delivery was undetectable.
   → **Fix:** every message now ends with a short, stable **event ref**
   (`… · ref <10-hex>` = SHA-256 of `opportunity_id|kind|event_id`), which mirrors
   the notifier's deduplication identity: identical for a true duplicate, distinct
   for any different legitimate event (including two kinds of the same
   opportunity, e.g. CONVERTED_TO_SETUP vs EXECUTION_READY). No private data shown.
3. **Ambiguous-outcome duplicate path (hardening).** A `URLError` (e.g. connection
   reset after the request may have been sent) was retried. → **Fix:** only
   **definitive pre-delivery** failures (`socket.gaierror`, `ConnectionRefusedError`)
   are retried; resets/timeouts/unknown are `uncertain` and **not** retried
   (at-most-once preserved).

Stable event identities, persistent deduplication, legitimate new-lifecycle
notifications, bounded 429 retry, and ambiguous-outcome handling are all retained.
No SMC rule, eligibility threshold, or GBPUSD-specific suppression was added.

---

## 6. Focused and full-suite test results

```
focused (test_telegram.py + test_telegram_delivery.py + test_runtime_freshness.py)
                                                         →  52 passed
pytest -q (full suite)                                   →  594 passed, 1 skipped,
                                                             0 failed, 0 errored
```

Baseline when the first Phase 14E report was written: 593 passed, 1 skipped.
Delta **+1** in the final pre-commit review — the added
`test_event_ref_distinguishes_kinds_of_same_opportunity` test (the ref now
encodes `kind`, so two kinds of one opportunity get distinct refs). Prior to that,
Phase 14D→14E added +3 (ambiguous-URLError, event-ref, structured-logging) over
the 590 baseline; one existing test
(`test_definitive_urlerror_retries_bounded`) was updated to raise a genuine
`ConnectionRefusedError` (its "definitive failure retries" intent preserved; the
policy now distinguishes it from an ambiguous reset). No assertion was weakened.

---

## 7. Containment

**Not performed / not required at this time.** No smc_engine Python sender is
running (port 8765 free, no `smc_engine` process) — there is nothing to stop. No
process was terminated and no configuration was changed. Per §6, if a Python
sender is re-started and evidence identifies it, containment should stop **only**
that exact process (or use the notifier diagnostics), leaving MT5 and
`Varis_SMC` operational. No authorization was requested because no live sender
was found to isolate.

---

## 8. Varis_SMC untouched

No change to `Varis_SMC` (MQ5 identity, `InpUseTelegram`, chart attachment), its
MT5 terminal, or Telegram WebRequests. It was only read for attribution. Its
file/token were never modified; no process of it was started/stopped.

---

## 9. Live execution disabled

`LIVE_EXECUTION_ENABLED = False` (unchanged). No broker primitive touched; no
order sent; no SMC causal/risk/gate change. Nothing staged, committed, or pushed.

---

## 10. Remaining uncertainties and release status

* **Exact repeat instance unestablished:** the sender's process and DB were lost
  to the environment reset, and the payload had no event id, so it cannot be
  proven whether the two messages were two distinct events or one re-delivered
  event. The ref + structured logs added this phase make future occurrences
  provable.
* **Sender not identified to a PID/DB:** only the code identity (smc_engine
  Python) is proven via the unique footer; the specific process/DB is unknown.
* **Marker not yet loaded by a live process:** the `_SOURCE_TAG`/ref live in the
  working tree; a running worker must be restarted to load them.
* **CI:** HEAD `23f4649` is the pushed baseline; this phase's changes are
  uncommitted and unverified by CI.

**Release status: CONDITIONAL PASS.** Sender attribution is supported by
evidence (the payload is uniquely smc_engine's Python output, including the
uncommitted marker). The unwanted-repeat mechanism is addressed where evidence
supports it (indistinguishable payloads → event ref; attribution/logging added;
ambiguous-retry duplicate path closed), but the specific repeat instance could
not be reproduced because the live evidence was destroyed by the environment
reset. It should move to PASS once (a) this revision is committed + CI-green, and
(b) after restarting one workstation from this revision, a re-scan produces a
single message per event with a distinct `ref` and a `telegram_send … delivered`
log line per event.

---

## 11. Confirmed facts vs unresolved history

**Confirmed (evidence-backed):**
* The received payload is emitted **only** by the smc_engine Python READY
  formatter (`telegram.py:90`) + `_to_ready`'s `next_expected` (`engine.py:471`)
  + the uncommitted `_SOURCE_TAG` (`telegram.py:45`). `Varis_SMC` contains none of
  these strings. → **sender class = smc_engine Python pipeline.**
* `Varis_SMC` and `smc_engine` share the **same bot token and chat**; `ChartAsst`
  shares the chat with a different bot.
* The earlier-observed 8765 server had loaded **pre-Telegram** code (no notifier).
* Confirmed code defects (now fixed): id-less payloads (event ref added), absent
  structured send logging (added), and an ambiguous-`URLError` retry path that
  could duplicate a delivery (closed).

**Plausible but UNPROVEN (the historical pair):**
* Whether the two messages were two **distinct** opportunities (identical text) or
  one **re-delivered** event (via a second instance or the old ambiguous-retry
  path). The sender's process/DB were destroyed by the environment reset.

**Tests demonstrate future behaviour**, not the historical mechanism.

**Still requires live verification:**
* A live workstation run from this revision emitting exactly one message per
  event with a distinct `ref` and a `telegram_send … delivered` log per event.

---

## 12. Pre-commit review outcome (Phase 14E review)

* Exact working-tree changes reviewed: `src/smc_engine/telegram.py`,
  `src/smc_engine/web/runtime.py`, `tests/test_telegram_delivery.py` (modified);
  `tests/test_runtime_freshness.py`, `PHASE-14D-…md`, `PHASE-14E-…md` (new).
  `git diff --check` clean; nothing staged.
* Invariants 1–12 of the review brief all hold. One refinement was made during
  review: the event ref now hashes `(opportunity_id, kind, event_id)` to mirror
  the dedup identity exactly (invariant 1).
* No secrets in code/messages/logs/reports (the only scan hits are a synthetic
  test canary). No credential file staged. Unrelated untracked docs untouched.
* `Varis_SMC` unmodified (EA file mtime `2026-10-08 20:04`); no MT5 process
  closed/reconfigured; no service restarted; no real Telegram message sent.

**Recommended files for the eventual Phase 14D/E fix commit:**
`src/smc_engine/telegram.py` · `src/smc_engine/web/runtime.py` ·
`tests/test_telegram_delivery.py` · `tests/test_runtime_freshness.py` ·
`PHASE-14D-STALE-TELEGRAM-INCIDENT-REPORT.md` ·
`PHASE-14E-TELEGRAM-SENDER-ATTRIBUTION-REPORT.md`

**Recommendation: APPROVE WITH LIMITATIONS.** Sender attribution is
evidence-backed and the repeat-class mitigations are correct and tested
(594 passed / 1 skipped); the limitation is that the historical two-message
instance cannot be reproduced (its runtime evidence was lost), so the incident
closes only after a live re-run confirms one message per event with distinct
`ref`s and delivery logs.

---

## 13. Controlled runtime verification (post-commit, revision dc65e06)

Performed after `dc65e06` was pushed and CI passed. One intended instance only.

* **Launch:** exactly one server, from `dc65e06`, via the `.venv` interpreter with
  a verbose launcher (root logger at INFO so `telegram_send` is visible); MT5
  credentials injected from the DPAPI store; working dir `C:\smc_engine` (repo DB).
  Process tree: `.venv` python → system python (single server, PID 26512).
* **Loaded-revision diagnostics (`/api/runtime`):** `git_commit=dc65e06`,
  **`loaded_git_commit=dc65e06`**, **`stale_code=False`**,
  `server_version=dc65e06`, `files_modified_after_startup={}` → the new
  stale-process diagnostic correctly reports the loaded revision.
* **No historical replay:** **0 GBPUSD notifications** delivered. Startup produced
  16 genuinely-new transitions (8 `OPPORTUNITY_SUPERSEDED` from startup
  reconciliation of pre-existing conflicts, 4 `READY`, 4 `ENTRY_TRIGGERED`) across
  XAUUSD247/XAUUSD/CADJPY/XAGUSD/USDJPY/GBPCAD — not a replay of delivered events.
* **Attribution + delivery logs:** every event logged
  `telegram_send pid=… ref=… fp=… symbol=… kind=… event_id=… outcome=attempt` then
  `… outcome=delivered`. 16 events → **16 distinct refs**, `send_count=16`,
  `failure_count=0`, `deduplicated_count=0`, `uncertain=0`, `dropped=0`; **max
  deliveries per ref = 1** (no duplicates).
* **Repeat scans:** after ~2.5 min of continued polling (`last_scan_time`
  advanced), the delivered set was **unchanged** (16 refs, no new/duplicate) →
  re-scan does not re-send.
* **Restart:** a fresh instance on the same DB delivered **0** and re-sent **none**
  of the 16 pre-restart events → durable dedup survives restart.
* **Cleanup:** the verification server was stopped; no smc_engine server/notifier
  is left running. `LIVE_EXECUTION_ENABLED=False`; `Varis_SMC` untouched.

**Runtime-verification outcome: PASSED for revision `dc65e06`** — one instance,
correct loaded-revision diagnostics, no historical replay, one delivery per event
with a stable ref and `delivered` log, and no re-send across scans or restart.

**Overall incident status remains CONDITIONAL PASS:** the historical duplicate's
exact mechanism (which process/DB, and which of the two candidate mechanisms)
remains unresolved because the original process and database evidence were lost to
the environment reset. What is now proven is that the current revision does not
exhibit the repeat behaviour under scan and restart.
