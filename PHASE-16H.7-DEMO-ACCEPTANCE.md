# PHASE-16H.7 — DEMO EXECUTION ACCEPTANCE

**Repository:** `tex-node/smc_engine` · **Branch:** `feature/gui-workstation` · **HEAD:** `af5c4fe`
**Status:** no order placed. `LIVE_EXECUTION_ENABLED=False` throughout.

## 1. Baseline (re-verified)
Prod DB `C:\smc_engine\smc_engine_gui.sqlite3`: `notification_outbox` absent, quarantine **1 row**, 193 opportunities, `integrity_check=ok`. Server PID 21812, `loaded_git_commit=af5c4fe`, `stale_code=true` (working-tree edits), `live_execution_enabled=false`, account `…@Exness-MT5Trial9 DEMO`, `account_identity=AUTHORIZED`. `/api/live/x` → **403**. MT5: 4× `terminal64.exe`.

## 2. Step 1.1 — Demo test configuration

**No documented, approved demo test configuration exists.** The repository's `RiskEngine`/`ExecutionPolicy` provide per-trade, broker-aware sizing/validation (volume_min/max/step, stop levels, a caller-supplied `risk_percent`) but do **not** define an approved demo test volume, order count, exposure, or daily loss cap. Per the phase rules I must not invent approved values, so the following is a **PROPOSAL for owner review — not approved**:

| parameter | proposed value (conservative) | source |
|---|---|---|
| terminal/account | `…@Exness-MT5Trial9` DEMO, login `477217728` | `/api/runtime` (verified) |
| broker server | `Exness-MT5Trial9` | verified |
| symbol / setup | one FX major with a valid, non-quarantined setup produced by the causal pipeline | existing eligibility |
| test volume | broker `volume_min` (the smallest broker-valid volume) | broker spec (not invented) |
| risk_percent | ≤ 0.10 % of balance | **proposed** |
| max order count | 1 | **proposed** |
| max concurrent positions | 1 | **proposed** |
| per-order risk | ≤ 0.10 % of balance | **proposed** |
| max total loss | ≤ 0.10 % of balance | **proposed** |
| daily loss limit | ≤ 0.20 % of balance | **proposed** |
| stop conditions | any deviation from the above, or any order path other than the approved demo harness → abort | — |
| cleanup | close the position immediately; verify flat; verify no residual order | — |
| rejection/timeout/duplicate/ambiguous | must fail closed; ambiguous outcome is never treated as success | existing policy |

**Gate:** every proposed value must be **explicitly approved** before an order is placed.

## 3. Step 1.2 — Execution path verification (read-only)
* `/api/live/{setup_id}` → **403** ("LIVE EXECUTION IS DISABLED"). `LIVE_EXECUTION_ENABLED=False` (compile-time).
* The only order-producing demo paths are the harness endpoints `/api/paper/{setup_id}` (+ `/cancel`) and `/api/dry-run/{setup_id}`.
* Risk/policy/guard/validation modules present: `risk.py` (`RiskEngine`), `execution_policy.py` (`ExecutionPolicy`), `order_guard.py` (`MT5OrderGuard`), `execution.py` (`MT5ExecutionAdapter`).
* Fail-closed behaviour is covered by the existing suites (see §4).

## 4. Step 1.3 — Non-order tests (run)
`pytest tests/test_dry_run.py tests/test_execution.py tests/test_execution_structure.py tests/test_order_guard.py tests/test_risk.py tests/test_risk_api.py -q` → **44 passed** (exit 0). No order placed.

## 5. Acceptance criteria
| criterion | result |
|---|---|
| Correct demo account identity | **PASS** (`…@Exness-MT5Trial9 DEMO`, AUTHORIZED) |
| Live execution remains disabled | **PASS** (`/api/live` 403; flag False) |
| Non-order risk/policy/guard tests | **PASS** (44) |
| Approved demo test configuration | **BLOCKED** (none documented; proposal pending owner approval) |
| Order checks / submission / reconciliation / stops / exit / restart recovery | **BLOCKED** (not executed — no approved configuration) |
| No order placed | **PASS** (no order submitted) |

**Disposition: `DEMO EXECUTION ACCEPTANCE BLOCKED`** — exact blocker: **no explicitly approved demo test configuration** (test volume + per-trade/aggregate/daily limits). No order was placed; none will be without that approval.
