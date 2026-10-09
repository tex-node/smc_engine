# PHASE-14D — STALE TELEGRAM INCIDENT REPORT

Repository: `tex-node/smc_engine` · Branch: `feature/gui-workstation`
HEAD: `23f4649` (UI fix on top of `b1196bd` Phase 14C and `ffe4e18` Phase 14B).
Working tree: uncommitted (this phase). No secrets printed (token and chat ID redacted).

> **CORRECTION (Phase 14E).** The original conclusion below — that the stale
> messages were not produced by the smc_engine Python app — is **superseded**.
> Phase 14E fingerprinted a received payload and proved it is emitted **only** by
> the smc_engine Python READY formatter plus the uncommitted `_SOURCE_TAG` (the
> MQL5 EA cannot produce those strings). **The sender is the smc_engine Python
> pipeline.** The pre-Telegram server observed here (PID 34248) could not send,
> which is why no Python sender was visible in the earlier (stale) snapshot; a
> later, Telegram-enabled Python process — since lost to an environment reset —
> produced the message. See `PHASE-14E-TELEGRAM-SENDER-ATTRIBUTION-REPORT.md`.

**Bottom line (as originally written, now corrected above):** the stale Telegram
messages are NOT produced by the smc_engine Python application.** They are
produced by a **separate MQL5 Expert Advisor, `Varis_SMC`**, running inside a
*different* MetaTrader 5 terminal (`C:\Program Files\MetaTrader 5 - Copy`, PID
30344), which posts to the Telegram Bot API using the **same chat** as
`smc_engine/telegram.txt`. This was the Phase-14D misattribution; Phase 14E
supersedes it (the EA shares the bot/chat but does not emit the received text).


---

## 1. Immediate containment actions and verification

Chosen action (confirmed with the operator): **do not terminate the MetaTrader
terminal; report only.**

* **smc_engine side:** no containment required — the running server does not
  instantiate a Telegram notifier (§3). Verified via `/api/opportunities/diagnostics`
  (no `telegram_*` keys) and process inspection (no Python process holds a
  Telegram connection).
* **Actual sender:** the MQL5 EA `Varis_SMC` in `MetaTrader 5 - Copy`
  (PID 30344). It was **left running** per the operator's decision. Exact
  one-step disable (operator action): open that terminal's chart, open the
  `Varis_SMC` EA settings and set `InpUseTelegram = false` (or remove the EA),
  or close that terminal. The EA input is `input bool InpUseTelegram = true;`.
* **Not touched:** the persistent database, opportunity/event history, Git
  history, Phase 14C/UI work, `telegram.txt`, and all execution gates. No
  process was terminated. `LIVE_EXECUTION_ENABLED` remains `False`.

**Containment verification:** because the sender was intentionally left running,
ongoing outbound notifications are **not stopped** at the time of writing — this
is the operator's explicit choice (report-only). The smc_engine application is
verified not to be a sender.

---

## 2. Process-identification evidence (no secrets)

| PID | Name | Path | Started | Role |
|---|---|---|---|---|
| 34248 | python.exe (system 3.12) | `C:\Users\XPS\AppData\Local\Programs\Python\Python312` | 2026-10-08T05:08:06 (+01) = 04:08:09Z | **Only** listener on `127.0.0.1:8765`; runs `smc_engine.web.api`; **pre-Telegram code** |
| 2312 | python.exe (`.venv`) | `C:\smc_engine\.venv\Scripts\python.exe` | same | venv launcher/child (not a separate listener) |
| 30896 | cmd.exe | — | same | console that launched 2312/34248 |
| 25196, 23648, 2628, 35320 | cmd.exe | — | 10/4, 10/6, 10/7, 10/8 | stale "SMC Engine Workstation Server" launcher consoles (no live child python) |
| **30344** | **terminal64.exe** | `C:\Program Files\MetaTrader 5 - Copy` | 2026-10-05 | **Source of outbound Telegram traffic** (holds an established HTTPS connection to Telegram) |
| 17716 | terminal64.exe | `C:\Program Files\MetaTrader 5 - Copy (2)` | 10/4 | other MT5 (no Telegram EA) |
| 18508 | terminal64.exe | `C:\Program Files\Headway MT5 Terminal` | 10/4 | other MT5 |
| 23276 | terminal64.exe | `C:\Program Files\MetaTrader 5` | 10/4 | main MT5 (used by smc_engine via MT5Source) |

* Only one process listens on `127.0.0.1:8765`: PID 34248.
* The only established outbound connection to a Telegram address
  (`149.154.166.110:443`, Telegram's Bot API range) is owned by **PID 30344**.
* No Python process holds a Telegram connection. `git grep` over the whole tree
  shows `api.telegram.org` only in `src/smc_engine/telegram.py`.
* No scheduled task sends Telegram (the only smc task, `smc_gate_a_paper_test`,
  last ran 2026-09-28 and points at a temp `gate_a_launcher.cmd`).

---

## 3. Confirmed root cause (code path + runtime evidence)

### Proven
1. **The 8765 server is NOT sending Telegram.** Its `/api/opportunities/diagnostics`
   contains **no `telegram_*` fields** ⇒ the loaded `hub.py` has no
   `TelegramNotifier`. Its `/api/runtime` shows `server_started_at =
   2026-10-08T04:08:09Z` and `files_modified_after_startup = [causal.py,
   web/hub.py]` ⇒ it loaded code from **before** the Telegram commit
   (`ffe4e18`, authored 2026-10-08 19:53Z). It cannot send.
2. **The actual sender is the MQL5 EA `Varis_SMC`.**
   * Source: `…\Terminal\E67EB8BA6BADCDA78B527BBA0B498F08\MQL5\Experts\Varis_SMC.mq5`
     (`origin.txt` → `C:\Program Files\MetaTrader 5 - Copy`, PID 30344).
   * It contains `input bool InpUseTelegram = true;`, hardcoded `InpBotToken`
     (length 46) / `InpChatID`, and `SendTelegram()` which `WebRequest`s
     `https://api.telegram.org/bot<token>/sendMessage`.
   * Its MQL5 logs record `Telegram configured - bot token length=46 | chat
     ID=<redacted>` and repeated `Telegram OK` entries on **2026-10-07, 10-08 and
     10-09** across many instruments (USDJPYm, XAUUSDm, EURUSDm, GBPCHFm, …).
   * **The EA's chat ID equals the ChatID in `smc_engine/telegram.txt`**
     (compared programmatically — equal). Both systems post to the same chat.
3. **Only that one terminal runs the EA** (checked every MT5 data instance).
4. `Varis_SMC` is a distinct program from the Python `smc_engine`; it is not
   part of this repository.

### Mechanism
`Varis_SMC` is an MQL5 port of the same "Varis SMC" methodology. On each
`OnTick`/timer it runs its own SMC analysis and, when a condition holds
(throttled but repeating while the signal persists), calls `SendTelegram`. This
is why the operator sees "a GBPUSD signal from yesterday still being posted":
it is the **EA re-emitting its own signal**, not smc_engine replaying an event.

### Hypotheses (not proven)
* The exact GBPUSD message text/timestamp cannot be recovered: the EA does not
  log message bodies (`Notify`→`SendTelegram` logs only "Telegram OK"). The
  precise GBPUSD event identity/timestamp therefore remains **unknown** from
  available evidence.
* Whether the EA has an additional re-send defect is plausible but unverified
  without its full logic trace.

---

## 4. Identity/timestamp of the stale GBPUSD event

**Unknown / not recoverable.** The EA does not log message content, and the
smc_engine database/media do not contain the sent message. What is known:
* Delivery times are on 2026-10-07 … 2026-10-09 (EA MQL5 logs), i.e. after the
  market anchor the EA was analysing.
* The message is delivered by `Varis_SMC`, not by smc_engine (any smc_engine
  GBPUSD opportunity records are unrelated to the delivered Telegram text).

---

## 5. Categorisation of the mechanism

| Candidate | Verdict |
|---|---|
| Stale smc_engine process sending | **No** — the 8765 server has no notifier; no Python holds a Telegram connection |
| smc_engine persistent event replay | **Not the cause** — the sender is not smc_engine |
| Duplicate smc_engine dispatcher | **No** — single hub instance |
| smc_engine identity instability | **Not the cause here**; Phase 14C already stabilised identity (see §6) |
| Stale eligibility in smc_engine | **Not the cause here** |
| **External MQL5 EA (`Varis_SMC`) re-emitting its own signal** | **CONFIRMED (proven)** |
| Misattribution (shared name + shared chat) | **CONFIRMED (proven)** |

---

## 6. Exact files changed (this phase)

Narrow, evidence-backed changes only — no SMC methodology, causal, risk,
lifecycle, Telegram-transport or gate changes. Changes are **uncommitted**.

| File | Change | Why |
|---|---|---|
| `src/smc_engine/web/runtime.py` | `record_startup()` now also captures the **loaded** git revision and startup file hashes; `build_fingerprint()` adds `loaded_git_commit`, `startup_source_hashes`, `stale_code`, and makes `files_modified_after_startup` a definitive startup-hash vs disk-hash comparison; `server_version` now reports the **loaded** revision. | The incident showed `/api/runtime` reported the **current on-disk HEAD** (`23f4649`) for a process actually running old code — actively misleading stale-process detection. |
| `tests/test_runtime_freshness.py` (new) | Proves the fingerprint reports the loaded revision, detects a stale process when disk changes, and exposes the identity fields. | Required §5.10. |
| `tests/test_telegram_delivery.py` | Added `test_two_instances_shared_db_do_not_double_send` and `test_expired_opportunity_not_notified_as_current`; made the hub fixture's send interval 0 for determinism. | Required §5.5/§5.6/§5.4. |
| `PHASE-14D-STALE-TELEGRAM-INCIDENT-REPORT.md` (new) | This report. | Deliverable. |

Phase 14C (`b1196bd`) and the UI fix (`23f4649`) are **preserved, unmodified**.

---

## 7. Regression-test results and full-suite counts

```
focused (runtime_freshness + telegram_delivery)  →  24 passed
pytest -q (full suite)                           →  589 passed, 1 skipped, 0 failed, 0 errored
```

Previous baseline: 584 passed, 1 skipped. Delta **+5** (3 runtime + 2 delivery
tests). No unrelated regressions. Coverage against §5: repeated scan → one
delivery; restart no resend; two instances shared DB → one delivery; expired
not re-sent; concurrent claim once; distinct lifecycle events delivered; distinct
symbols no collision; ambiguous timeout not retried; obsolete-process detection;
no live Telegram / fabricated setup (all mocked).

---

## 8. Runtime verification results

* Exactly one process listens on `8765` (PID 34248); there is **no** second
  smc_engine server and **no** smc_engine Telegram dispatcher.
* The running 8765 server is **stale code** (pre-Telegram), proven by:
  `server_started_at` (2026-10-08T04:08:09Z) < Telegram commit time;
  `files_modified_after_startup = [causal.py, web/hub.py]`; and no `telegram_*`
  diagnostics.
* No Python process has a Telegram connection; the only Telegram connection is
  MT5 PID 30344 (the EA).
* A live Telegram delivery of a *new* eligible smc_engine event was **not**
  attempted (the running server has no notifier, and I did not restart it). Per
  the task, a mocked test is not proof of the production process; here the
  production process is simply not a sender, which is the key result.
* I did **not** start a replacement instance (would require restarting the
  operator's server) and did **not** manufacture a live setup.

---

## 9. Remaining limitations

* **The real sender remains active** (operator chose report-only). The stale
  notifications continue until the operator disables `Varis_SMC`'s Telegram
  (`InpUseTelegram=false`) or closes that terminal.
* **Exact GBPUSD event identity/time is not recoverable** (the EA logs no
  message body).
* The **stale smc_engine server** (PID 34248, pre-Telegram) is still running from
  an obsolete revision and should be restarted from the intended revision; I did
  not restart it (no authorization, and it is not the Telegram sender).
* The MQL5 EA is outside this repository, so its repeat/eligibility behaviour is
  not covered by the smc_engine test suite or by this fix.
* The `Varis_SMC.mq5` file hardcodes bot token/chat inputs (its own credential
  hygiene issue); not modified here.

---

## 10. Final decision

# SUPERSEDED BY PHASE 14E

**Correction:** Phase 14E fingerprinted a received payload and proved it is
emitted **only** by the smc_engine Python pipeline (its READY formatter plus the
uncommitted `_SOURCE_TAG`); the MQL5 EA cannot produce those strings. The
original conclusion — that smc_engine was not the sender — is therefore wrong:
the pre-Telegram server observed in the (stale) snapshot was not the sender, but
a later Telegram-enabled Python process was. See
`PHASE-14E-TELEGRAM-SENDER-ATTRIBUTION-REPORT.md`.

The retainable findings from Phase 14D: the runtime endpoint misreported the
loaded revision (fixed), and `Varis_SMC` shares the bot/chat but was never the
source of the received text.

**Phase 14D verdict (as originally recorded): CONDITIONAL PASS** — with the
attribution portion now superseded.


`LIVE_EXECUTION_ENABLED = False`; no execution primitives; no secrets exposed;
nothing staged, committed, or pushed.
