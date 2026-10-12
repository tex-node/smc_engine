# PHASE-16H.8 — DEPLOYMENT ACCEPTANCE

**Repository:** `tex-node/smc_engine` · **Branch:** `feature/gui-workstation`
**Deployed artifact SHA:** `826e466693c3ed3b3cb34e846bba19d7e17fef5d` (parent `af5c4fe`)
**Status:** deployed via the documented launcher. `LIVE_EXECUTION_ENABLED=False` throughout. No order placed; no real Telegram test message.

## Step 1 — Pre-deployment verification
| check | result |
|---|---|
| Repo/branch/HEAD | `C:/smc_engine`, `feature/gui-workstation`, `826e466` |
| Remote sync | `origin/feature/gui-workstation` = `826e466` (0/0) |
| CI run `38206895271` | **completed / success**, `headSha=826e466…` |
| Production migration present | `notification_outbox` present, 0 rows |
| Quarantine / integrity | 1 row; `integrity_check=ok` |
| Backup accessible | `…pre16h7_20261012T051508Z.sqlite3` (25,862,144 bytes) |
| `LIVE_EXECUTION_ENABLED=False` + `/api/live` denied | **PASS** (403) |
| Pre-deploy runtime | `loaded=af5c4fe`, `stale=true`, DEMO AUTHORIZED |

All prerequisites passed.

## Step 2 — Deploy
* **Mechanism:** documented launcher `start_workstation.bat` → `load_mt5_credentials.ps1`.
* **Stop:** `stop_workstation.bat` reported success but the window-title filter matched nothing; escalated to `taskkill /PID 31868 /T /F` after re-verifying the owned tree (`31868 cmd → {29452 conhost, 22512 python → 21812 listener}`). Port 8765 freed.
* **Start:** launcher reported `Mode DEMO`, `Identity Gate AUTHORIZED`, `Live Execution DISABLED`; listener **PID 34096**, `server_started_at=2026-10-12T05:20:31Z`.
* No production configuration was modified ad hoc; the working tree at `826e466` was loaded.

## Step 3 — Post-deployment verification
| check | result |
|---|---|
| Running commit | `loaded_git_commit=826e466`, `git_commit=826e466`, **`stale_code=false`**, `files_modified_after_startup=[]` |
| Process tree | `18940 → 29708 (.venv) → 34096 (listener)` |
| Account identity | `…@Exness-MT5Trial9 DEMO`, `account_identity=AUTHORIZED` |
| Outbox table + migration | present, 0 rows |
| Quarantine intact | 1 row (`OPP-REVERSAL-GBPAUD-…`) |
| DB integrity | `integrity_check=ok` |
| Live execution disabled | `/api/live` → **403**; `live_execution_enabled=false` |
| Telegram | `telegram_enabled=true`, `telegram_send_count=0`, `telegram_quarantined_count=0` |
| Outbox functional verification | non-delivery: 17 outbox tests (mock transport) + ledger read/write self-test with cleanup — **PASS**; the running app wires `NotificationLedger` (outbox is the queue) |
| No real Telegram test message | **PASS** (send_count 0; verification used mocked/self-test paths) |
| No MT5 order placed | **PASS** |
| Logs contain no notification text/credentials | **PASS** (structured `telegram_send` logs carry `ref`/`fp`/symbol/kind/event_id only) |

## Step 4 — Demo acceptance readiness
**BLOCKED.** No written test configuration exists (verified demo terminal/account/broker identity; symbol/setup eligibility; exact volume + max order count; per-trade risk; aggregate exposure; daily loss limit; max outstanding orders; cancellation/cleanup/stop conditions). A conservative proposal is recorded in `PHASE-16H.7-DEMO-ACCEPTANCE.md` **pending owner approval**. No order placed; no approved value invented.

## Step 5 — Signal quality
**`INSUFFICIENT DATA`** (unchanged) — no stored historical OHLC dataset and no reproducible outcome-evaluation harness; event/opportunity counts are not a proxy for strategy quality (see `PHASE-16H.7-SIGNAL-QUALITY.md`).

## Residual risks
* Telegram: durable enqueue + at-least-once attempts + confirmed acceptance; **no exactly-once**; TOCTOU window between the quarantine read and the HTTP send remains.
* The deployed app now sends through the durable outbox; genuine market events may produce real notifications (normal operation, not test traffic).
* GBPAUD identity conflict quarantined (not rewritten).
* Demo acceptance blocked; signal quality insufficient; live trading disabled.

## Gate table
| gate | result |
|---|---|
| Pre-deployment prerequisites | **PASS** |
| Deployment (artifact loaded) | **PASS** (`826e466`, not stale) |
| Post-deployment health | **PASS** |
| Outbox present + used | **PASS** |
| Quarantine enforcement | **PASS** |
| Execution gate | **PASS** (403; flag False) |
| No real Telegram test message | **PASS** |
| No MT5 order | **PASS** |
| Demo acceptance | **BLOCKED** |
| Signal quality | **INSUFFICIENT DATA** |

**Disposition: `DEPLOYMENT PASS — DEMO ACCEPTANCE BLOCKED — SIGNAL QUALITY INSUFFICIENT DATA`.** Live execution remains disabled. Awaiting separate authorization before live trading.
