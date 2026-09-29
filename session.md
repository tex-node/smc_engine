# Session Summary

## Project
`tex-node/smc_engine` — Smart Money Concepts (Varis methodology) 3-layer engine on MT5, plus a browser workstation GUI. GitHub is the source of truth; local `C:\smc_engine` mirrors it.

## Where we are
- **Branch `feature/gui-workstation`** (from verified commit `d569692` of `refactor/market-structure-foundation`). `main` and the refactor branch untouched.
- Baseline commits: `81e1bda` GUI → `d9256d7` CI → `f55426d` hub orchestration slice (COMPLETE, reviewed) → `b57dfbe` CI skips.

## Session arc
1. **Engine lineage** (main): legacy `varis_smc_bot.py` v1 → v2 dynamic 1% risk sizing → dedup guard → pending-order invalidation/cancel.
2. **Deployment**: mirrored `d569692`, `.venv` isolated, 102/102 tests, 8-point offline smoke PASS.
3. **DryRunEngine demo validation** (Exness-MT5Trial9): real metadata, real bid/ask, `order_check retcode=0`, zero `order_send`. Hard account gate (demo/trial only) later caught an accidental Headway-Real attach and refused.
4. **Gate A harness** (first paper round-trip): allow-list gate (PENDING + demo + server + symbol + session token; REMOVE only own tickets), 6/6 denial probes. Scheduled via Task Scheduler Mon 13:00 UTC with DPAPI single-use credentials. **Result at scheduled time: DENIED by account gate** (`unauthorized account/server` — terminal attached elsewhere; blob consumed by test runs). Zero orders placed; **Gate A has NOT yet passed — must re-provision and re-run with the demo credentials attached.**
5. **GUI workstation** (FastAPI + SSE + static dark canvas frontend over the engine; engine stays authoritative; FVG added as an engine primitive).
6. **Hub orchestration slice** (reviewed & complete): canonical enum gateway, broker-authoritative idempotent ticket accounting, symbol isolation + suffix normalization, dead-code removal. 134 green.
7. **GUI → real engine integration** (this slice, see below).

## Current slice: real-data GUI integration
- `candidates_for()` now passes the engine's evidence chain through verbatim (sweep/CSD/POI/OB/IDM/IRL ids); UI renders only engine values; absent evidence shown as unavailable.
- Live quote block per symbol (bid/ask/spread/tick source); `MARKET DATA UNAVAILABLE` states instead of stale data; "NO CAUSAL SETUP DETECTED" treated as a valid rendered result.
- Frontend no longer infers setup evidence from chart overlays; chart timeframe never substituted silently (503 on missing TF).
- Tests: 9 new in `tests/test_real_integration.py` (exact value passthrough, symbol isolation, TF integrity, risk-tracking, canonical lifecycle, paper-gate flow, static no-`order_send` guarantee). Full suite **143 passed**; Linux-sim **27 passed / 9 skipped**.
- Live check on Exness demo: EURAUD+XAUUSD real quotes, isolation confirmed, `/api/live/*` 403, `live_execution_enabled=false`.

## Standing constraints
- LIVE EXECUTION disabled (compile-time + backend gate). Paper orders only via Gate A-approved flow; first real demo order still pending Gate A re-run.
- Hub stays an orchestrator; no SMC math in frontend.
- Monday Gate A: needs fresh DPAPI blob + terminal actually on Exness demo; re-run before any paper claim.

## Run
```powershell
cd C:\smc_engine
.venv\Scripts\python.exe -m uvicorn smc_engine.web.api:create_app --factory --host 127.0.0.1 --port 8765
# http://127.0.0.1:8765  (env MT5_LOGIN/PASSWORD/SERVER for explicit demo attach)
python -m pytest -q      # 143 tests
```
