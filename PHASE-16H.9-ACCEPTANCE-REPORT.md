# PHASE-16H.9 — ACCEPTANCE REPORT

**Repository:** `tex-node/smc_engine` · **Branch:** `feature/gui-workstation`
**Starting commit:** `af5c4fe` → **deployed commit:** `826e466693c3ed3b3cb34e846bba19d7e17fef5d`
**Status:** redeployed + one Telegram test delivered; **demo order NOT submitted** (no valid stop-loss). `LIVE_EXECUTION_ENABLED=False` throughout. Secrets redacted.

## 1. Authorization scope and exclusions
Authorized: redeploy `826e466`; **one** real non-sensitive Telegram test; **one** demo market order (EURUSD BUY 0.01, mandatory SL, 1% per-trade risk, 10% daily cap, max 1 order). Excluded (and honored): live trading, `LIVE_EXECUTION_ENABLED=True`, live endpoint, real-money orders, volume/limit changes, second order, unrelated refactor, quarantine alteration, bypassing failed checks.

## 2. Commits
Starting HEAD `af5c4fe` → deployed/reviewed revision `826e466693c3ed3b3cb34e846bba19d7e17fef5d` (parent `af5c4fe`), in sync with `origin/feature/gui-workstation`. No new commit made this phase.

## 3. Deployment commands + outcomes
* Documented stop `stop_workstation.bat` matched nothing (window-title filter) → after re-verifying the owned tree (`18940 cmd → {31304 conhost, 29708 python → 34096 listener}`) escalated to `taskkill /PID 18940 /T /F`. Port 8765 freed.
* Start via documented `start_workstation.bat`: `Mode DEMO`, `Identity Gate AUTHORIZED`, `Live Execution DISABLED`; listener **PID 21692**, `server_started_at ≈ 2026-10-12T05:2xZ`.

## 4. Health checks + safety gates (all PASS)
`loaded_git_commit=826e466`, `git_commit=826e466`, **`stale_code=false`**, `files_modified_after_startup=[]`; account `…@Exness-MT5Trial9 DEMO`, `account_identity=AUTHORIZED`, `account_mode=DEMO`; `/api/live/x` → **403**; `/api/runtime` HTTP 200. Process tree `18940 → 29708 → 21692`.

## 5. Live execution
`LIVE_EXECUTION_ENABLED=False`; `/api/live` denied (403). **Confirmed throughout.**

## 6. Database / outbox / quarantine
`notification_outbox` present; **1 row** = the authorized test (`DELIVERED`); quarantine **1 row** preserved; `integrity_check=ok`. No production migration performed this phase (already applied in 16H.7).

## 7. Telegram test (PASS)
* Destination: the app's configured test chat (fingerprint `sha256[:8]=09cbbd44`; value never printed).
* Correlation ID: `phase16h9-telegram-20261012T053147Z` (unique; one event).
* Path: app's `TelegramNotifier` + durable `NotificationLedger` + quarantine check enabled (`OpportunityRepository.is_quarantined`).
* Outbox transition: `PENDING → IN_FLIGHT → DELIVERED`; attempts **1**; `last_error=None`.
* HTTP result: **confirmed success** — Telegram `message_id=1013`.
* `telegram_send_count=1`, `telegram_quarantined_count=0`; **no duplicate** submitted.

## 8. MT5 terminal / account identity (secrets redacted)
App MT5 integration: `source=mt5`, `connected=true`, `account_class=DEMO`, login `477217728`, server `Exness-MT5Trial9`. Verified via `/api/account` and `/api/risk`.

## 9. EURUSD symbol resolution (PASS)
`/api/market/EURUSD` returns current M15 bars (latest `2026-10-12 05:15Z`); EURUSD is present and tradable via the app's broker symbol resolution (no suffix substitution performed).

## 10. Demo equity + risk
Equity `786024.74` (currency **NGN**), `margin_free 786024.74`. App risk gate: `max_total_risk_percent=3.0`, `committed=0.0`, `available=3.0`. Requested 1% ceiling = 7860.25 NGN.

## 11. Stop-loss rationale — **BLOCKED**
A stop-loss must come from **an existing valid test setup or a specifically approved test price**. `/api/setups` → **empty** and the `setups` table → **0 rows**; no approved test price was supplied. A valid SL therefore **cannot be established**, and inventing one is prohibited. No SL price, no estimated loss, and no broker risk check were produced.

## 12. Broker `order_check` — not reached (BLOCKED at SL).

## 13. Order submission — **not submitted** (BLOCKED). No broker order/deal identifiers; no order placed.

## 14. Final position / cleanup
No order → no position, no exposure, no cleanup obligation. (No MT5 order was placed by this run.)

## 15. Tests executed
`pytest tests/test_risk.py tests/test_risk_api.py tests/test_execution.py tests/test_execution_structure.py tests/test_order_guard.py tests/test_dry_run.py tests/test_notification_outbox.py tests/test_telegram.py tests/test_telegram_delivery.py tests/test_quarantine_notifications.py tests/test_opportunity_quarantine.py tests/test_identity_integrity.py -q` → **144 passed, 0 failed, 0 skipped**.

## 16. Failures / blockers / limitations
* **Blocker:** no existing test setup and no approved test price → valid stop-loss unavailable → demo order blocked.
* Telegram: no exactly-once; TOCTOU window remains (not eliminated).
* The outbox now holds one `DELIVERED` test row (intended audit record).

## 17. Phase status
| phase | result |
|---|---|
| A — preflight / repo verification | **PASS** |
| B — redeploy + health/safety gates | **PASS** |
| C — one Telegram test | **PASS** (DELIVERED, message_id 1013) |
| D — demo order preflight | **BLOCKED** (no valid stop-loss; account + symbol verified) |
| E — submit ≤1 demo order | **BLOCKED** (not submitted) |
| F — position / cleanup | **N/A** (no position) |
| G — evidence report | **PASS** (this file) |

**Overall: `TELEGRAM ACCEPTANCE PASS — DEMO ORDER BLOCKED (NO VALID STOP-LOSS)`.** Live execution remains disabled; no live or demo order was placed.
