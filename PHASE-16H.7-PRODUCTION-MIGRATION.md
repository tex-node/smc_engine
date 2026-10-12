# PHASE-16H.7 — PRODUCTION MIGRATION (notification_outbox)

**Repository:** `tex-node/smc_engine` · **HEAD:** `826e466693c3ed3b3cb34e846bba19d7e17fef5d` · **Branch:** `feature/gui-workstation`
**Operation:** additive production schema migration. `LIVE_EXECUTION_ENABLED=False` throughout.

## Migration identity
* **Mechanism:** the repository has **no migration framework** (`migrations/` absent; only `CREATE TABLE IF NOT EXISTS` at store init). The migration is the additive table creation in `src/smc_engine/notification_ledger.py::NotificationLedger.__init__`.
* **Identifier:** `notification_outbox` (additive `CREATE TABLE IF NOT EXISTS` + `idx_outbox_status` index).
* **Additive / non-destructive:** yes — creates one new table + one index; touches no existing row/table.

## 3.1 Pre-migration gate
| check | result |
|---|---|
| Exact schema diff | additive `notification_outbox` (16 columns, PK `notification_id`, `UNIQUE(opportunity_id,kind,event_id)`) + index |
| Additive, no delete/rewrite | **PASS** |
| Locking/compatibility | shares the GUI DB writer lock (`web.dbwrite`); WAL; single additive statement |
| Index/table name collisions | none (`notification_outbox` new; no prior occurrence) |
| Migration-history discrepancy | N/A (no framework) |
| Recent verified backup | `…pre16h7_20261012T051508Z.sqlite3`, **SHA-256 `1cdc7e7eb1d9f65f92aa34311e955636cecec92812027422e307e23c35d625ea`**, `integrity_check=ok`, `foreign_key_check=[]` |
| Service/DB health | server healthy; `integrity_check=ok` |
| Baseline quarantine | **1 row** (`OPP-REVERSAL-GBPAUD-…`) |
| Telegram transport won't send test notifications | **PASS** (migration performs no notify) |
| `LIVE_EXECUTION_ENABLED=False` | **PASS** |
| Deployment required? | No — migration is schema-only; the running app (`af5c4fe`) is unaffected (it never uses the table) |

## 3.2 Apply
* Command: `python …\smc16h7_migrate.py` → `NotificationLedger(<prod DB>)` (store-init `CREATE TABLE IF NOT EXISTS`).
* Start `2026-10-12T05:15:08Z` → end `05:15:2x Z`; **exit code 0**; resulting schema: `notification_outbox` present, `counts={}`, `pending=0`.
* No destructive reset or broad sync command was used.

## 3.3 Verify
| check | result |
|---|---|
| New outbox table/schema exists | **PASS** |
| Migration history consistent | N/A (no framework) |
| Database integrity | **PASS** (`integrity_check=ok`) |
| Quarantine record + history intact | **PASS** (1 row unchanged) |
| No unintended outbox rows | **PASS** (0 rows) |
| No real Telegram send | **PASS** |
| Live execution disabled | **PASS** (`/api/live` → 403; flag False) |
| Read/write via the app path | **PASS** — `record_intent` → `PENDING`; then cleaned up → 0 rows |
| No order placed | **PASS** |

## Rollback / recovery
The migration is additive and inert to the running app. **Rollback** = `DROP TABLE notification_outbox` (and its index) — no other object depends on it. **Recovery** = restore `…pre16h7_…sqlite3` (verified backup) if any unexpected change is detected. **No rollback was required.**

**Disposition: PRODUCTION MIGRATION PASS.** No deployment or service restart was performed (schema-only, additive). No order placed; no Telegram send.
