# PHASE-16H.7 — SIGNAL QUALITY & RISK REVIEW

**Status:** analytical only. `LIVE_EXECUTION_ENABLED=False` throughout. No metric invented.

## Data available (observed)
* Live MT5 market data (M15/H4/D1) is read on demand by the scan pipeline; it is **not persisted** as a historical OHLC dataset.
* GUI DB `smc_engine_gui.sqlite3`: **193 opportunities**, `opportunity_events`, `opportunity_state_history`, ≈45k+ `scan_funnel_events` (rejection reasons per causal stage), `setup_events` (105), `alerts` (1661).
* Pipeline stages observed in the funnel: `H4_SWEEP`, `H4_CSD`, `D1_POI`, `IRL`, `M15_IDM`, `READY`, with explicit rejection reasons (`D1_POI_DIRECTION_MISMATCH`, `NO_CSD`, `NO_D1_POI`, `IRL_MISSING`, `NO_IDM`, …).

## What cannot be established reproducibly
* **Outcome evidence** (win/loss, expectancy, drawdown) requires entry, invalidation, exit/target rules evaluated against **historical price series**, plus spread/slippage/commission. The system is observation-only: it records opportunity lifecycle, not trade outcomes. There is **no stored historical OHLC dataset** and **no outcome-evaluation harness**.
* Therefore event/opportunity/alert counts are **not** a proxy for strategy quality and no reproducible outcome set can be produced.

## Explicit risk controls (proposed for approval — not yet enabled)
Maximum per-trade risk cap; aggregate exposure cap; daily loss cap; drawdown stop; stale-data denial; duplicate-order prevention; broker/account identity verification before every order; manual emergency shutdown. (The `RiskEngine`/`ExecutionPolicy`/`MT5OrderGuard` modules implement per-trade sizing/validation; the aggregate/daily/drawdown caps are not yet configured.)

## Minimum additional evidence required
1. A persisted historical OHLC dataset (per instrument/timeframe) with documented freshness/missing-bar handling.
2. A reproducible outcome-evaluation harness: defined entry, invalidation, exit/target rules + spread/slippage/commission.
3. In-sample/out-of-sample split and a sample-size/uncertainty analysis.
4. Documented aggregate/daily/drawdown risk limits approved by the owner.

## Decision
**`INSUFFICIENT DATA`** — signal quality cannot be established; live trading remains disabled. No performance figure is claimed.
