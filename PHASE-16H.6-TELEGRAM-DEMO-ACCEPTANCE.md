# PHASE-16H.6 — DURABLE TELEGRAM DELIVERY & DEMO ACCEPTANCE

**Repository:** `tex-node/smc_engine` · **Branch:** `feature/gui-workstation`
**Baseline commit:** `af5c4fe109bd5d4170eb45ec379791e12b5c49f9` (deployed, in sync with origin)
**Status:** code + tests only. **No production deployment, migration, restart, Telegram send, or order.**
`LIVE_EXECUTION_ENABLED=False` throughout.

> Observed facts are labelled; assumptions are noted. No credentials are included.

---

## 1. Starting state
HEAD `af5c4fe` (in sync with origin); working tree clean except the preserved untracked `PHASE-9/10/12/13/14B` reports + `PHASE-16H.5-OPERATIONAL-CLOSURE.md`. Production: quarantine **1 row** present, `integrity_check=ok`, server listening (PID 21812), `LIVE_EXECUTION_ENABLED=False`.

## 2. Telegram architecture and original failure modes
Path: `opportunity event → hub._dispatch_opportunity_events → TelegramNotifier.notify → in-memory queue.Queue → worker → _send_with_retry → HTTP`.
Original memory-only state: `queue.Queue(maxsize=50)` and `_sent_keys` (in-process). Consequences: a restart **discards queued-but-unsent** notifications; ambiguous HTTP outcomes were recorded as `uncertain` with no durable record; no persisted attempts/outcomes. Durable *event* dedup exists at the DB layer (`record_event_once`), but there was **no durable delivery record**.

## 3. Durable outbox (new) — schema & migration
New module `src/smc_engine/notification_ledger.py`, class `NotificationLedger`, additive table `notification_outbox` (created `IF NOT EXISTS` by the ledger; shares the GUI DB + writer lock):
`notification_id PK, opportunity_id, kind, event_id, symbol, text, status, attempts, next_attempt_at, last_attempt_at, last_error, telegram_message_id, delivered_at, suppression_reason, created_at, updated_at, UNIQUE(opportunity_id, kind, event_id)`.
The notifier takes an optional `ledger`; when present the **outbox is the queue** (no in-memory queue). The hub wires `NotificationLedger(db_path)`. When absent, the legacy in-memory path is retained (backward compatible).

## 4. State machine
`PENDING → IN_FLIGHT → {DELIVERED, RETRY_WAIT, FAILED_PERMANENT, SUPPRESSED_QUARANTINE}`; `RETRY_WAIT → IN_FLIGHT` (when due); stale `IN_FLIGHT → PENDING` (crash recovery, `_STALE_INFLIGHT_S=300`). `DELIVERED` is set **only** on a confirmed 2xx response.

## 5. Retry, ambiguity, and recovery semantics
* Bounded exponential backoff with jitter (`_BASE_BACKOFF_S=2`, `_MAX_BACKOFF_S=300`, `_MAX_ATTEMPTS=5`).
* HTTP 429 → `RETRY_WAIT` honouring `retry_after`.
* Non-429 HTTP / auth failure → `FAILED_PERMANENT`.
* Connection refused / DNS → `RETRY_WAIT` (pre-delivery, safe to retry).
* Timeout / reset / unknown → **ambiguous** → `RETRY_WAIT`, **never `DELIVERED`** (no exactly-once claim).
* Restart: `recover_stale()` returns crashed claims to `PENDING`; pending/retry records survive.
* Guarantees: **durable creation + at-least-once attempts + confirmed acceptance recorded**; **exactly-once external delivery is NOT claimed** (a timeout after Telegram accepts is ambiguous).

## 6. Quarantine / send concurrency
The dispatch re-checks quarantine immediately before the HTTP call, **fail-closed** (unreadable state → `SUPPRESSED_QUARANTINE`). Covered cases: quarantined before enqueue (hub filter), after enqueue (dispatch check), after claim, and check-error. **Unavoidable race:** a TOCTOU window remains between the DB read and the HTTP request; an in-flight send accepted by Telegram cannot be retracted by quarantine. No cross-process lock/lease is implemented (a final DB read is not atomic) — documented, not claimed.

## 7. Failure-injection test results (`tests/test_notification_outbox.py`, 17)
Mocked transport + disposable DBs (never real recipients): successful delivery → `DELIVERED` (+ message_id); 429 `retry_after` → `RETRY_WAIT`; 5xx → `FAILED_PERMANENT`; 401 → `FAILED_PERMANENT`; timeout → ambiguous `RETRY_WAIT` (not delivered); connection refused → retryable; retry exhaustion → `FAILED_PERMANENT`; restart recovers pending; stale `IN_FLIGHT` → `PENDING`; delivered not re-claimed; duplicate event idempotent; quarantine-after-enqueue and fail-closed check → `SUPPRESSED_QUARANTINE`; ledger-unavailable enqueue fails closed; concurrent claim exclusive (1 winner); diagnostics expose no message text.

## 8. Demo execution audit + acceptance
* **Gate:** `/api/live/{setup_id}` → **403 "LIVE EXECUTION IS DISABLED"** (the only live order path); `LIVE_EXECUTION_ENABLED=False` (compile-time). Demo harness: `/api/paper/{setup_id}` (+ cancel) and `/api/dry-run/{setup_id}`.
* MT5: 4× `terminal64.exe` running; account `…@Exness-MT5Trial9 DEMO`, `account_identity=AUTHORIZED`.
* **`DEMO EXECUTION ACCEPTANCE BLOCKED`** — exact blocker: no documented **approved demo test configuration** (test volume + per-trade/aggregate risk limits). Placing and resolving a real demo position without those parameters would require inventing risk controls, which this phase forbids. **No order was placed.**

## 9. Signal quality
The scan pipeline is active (≈45k+ funnel events; 193 opportunities now) but there is **no reproducible strategy-outcome evidence** (no stored historical OHLC dataset; no entry/exit/stop/target outcome tracking; no cost/slippage accounting). **`INSUFFICIENT DATA`** — unchanged; no thresholds invented.

## 10. Risk controls and remaining limitations
Quarantine remains active; execution gate intact; outbox never marks delivered on a non-2xx/ambiguous outcome. Remaining: TOCTOU window; no exactly-once delivery; no cross-process send/claim coordination; demo acceptance blocked; signal quality insufficient; outbox **not deployed** (migration not applied to production).

## 11. Migration and rollback
Migration = the additive `notification_outbox` table (created on ledger init; no destructive change). Rollback = drop the table (or revert the code); no production data depends on it. **Not applied to production in this phase.**

## 12. Files changed (uncommitted)
```
 M src/smc_engine/telegram.py            (optional durable outbox integration)
 M src/smc_engine/web/hub.py             (wire NotificationLedger)
?? src/smc_engine/notification_ledger.py (new)
?? tests/test_notification_outbox.py     (new, 17 tests)
?? PHASE-16H.5-OPERATIONAL-CLOSURE.md    (prior phase report, preserved)
```

## 13. Production isolation
| check | result |
|---|---|
| `LIVE_EXECUTION_ENABLED=False` | **PASS** |
| Production DB unchanged by this phase (no `notification_outbox` table; quarantine 1 row; integrity ok) | **PASS** |
| No production migration applied | **PASS** |
| No deployment/restart by this phase (server still on `af5c4fe`) | **PASS** |
| No real Telegram message sent | **PASS** |
| No MT5 live/demo order placed; MT5 terminals untouched | **PASS** |
| No real secrets in new/changed files | **PASS** |

## 14. Gate table
| Gate | Result |
|---|---|
| Baseline verified | **PASS** |
| Telegram architecture audited | **PASS** |
| Durable outbox implemented (additive) | **PASS** |
| Outbox tests (failure injection) | **PASS** (17) |
| Focused regression | **PASS** (123) |
| Full regression | **PASS** (668 passed, 1 skipped) |
| Production isolation | **PASS** |
| Quarantine intact + preserved | **PASS** |
| Demo execution acceptance | **BLOCKED** (no approved test configuration) |
| Signal quality | **INSUFFICIENT DATA** |
| Live execution | **DISABLED / BLOCKED** |

**Disposition: `TELEGRAM RELIABILITY PASS — DEMO ACCEPTANCE BLOCKED`.**

## 15. Next authorization required
(a) Approve the exact demo **test configuration** (test volume + per-trade/aggregate risk limits) to run the demo lifecycle; (b) separately authorize committing/pushing the outbox change and applying its additive migration to production; (c) separately authorize live trading only after signal-quality evidence + documented risk limits. **No commit/push/deploy/migration/order was performed.**
