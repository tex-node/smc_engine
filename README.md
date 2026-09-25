# SMC Engine — Varis The Trader 3-Layer Automated Trading Pipeline

A Python-based automated trading system implementing Smart Money Concepts (SMC)
market structure methodology (per Varis The Trader) on MetaTrader 5, using the
official `MetaTrader5` Python library.

> **Risk warning:** This bot places real pending orders on a live MT5 account.
> Test on a demo account first. Trading CFDs/forex carries significant risk of loss.

## Architecture

The pipeline is a strict 3-layer confluence model. No layer is traded in isolation:

| Layer | Timeframe | Purpose |
|-------|-----------|---------|
| 1. Macro Context | Daily (60–90 day window) | Unmitigated Demand/Supply zones created by displacement candles (body > 1.5x ATR-14). Price must be touching/sweeping an HTF POI. |
| 2. Reversal & CSD | 4-Hour | Liquidity sweep detection (wick through a prior swing, close back inside) + Change in State of Delivery (full body close beyond the sweep impulse point). Sweep extreme = Protected High/Low. |
| 3. Execution | 15-Minute | Inducement (IDM) at/near the 15m Order Block. Lot size computed from 1% account risk using broker tick value/size and volume steps; limit order at OB mitigation; SL beyond Protected High/Low; TP at Internal Range Liquidity (IRL). |

Position sizing formula (in `calculate_dynamic_lot_size()`):
`Lots = (Balance × Risk%) ÷ (|Entry − SL| / TickSize × TickValue)`, floored to the broker
`volume_step` and clamped between `volume_min` and `volume_max`. A tight SL therefore
scales volume up and a wide SL scales it down — monetary risk stays fixed at 1%.

Execution rules:
- Pending limit orders only — never market orders.
- SL, TP, and dynamic lot size are attached at order placement time.
- Deduplication: no new order while a bot-owned (magic 202609) pending order or
  position exists on the symbol.
- Structural invalidation: each poll checks the 15m close against pending orders' SL;
  if price breaches the Protected High/Low before the limit is filled, the order is
  canceled (`manage_pending_order_invalidation` / `cancel_order`).
- Continuous monitoring loop on closed 15m candles (poll interval configurable).

## Repository Layout

```
smc_engine/
├── varis_smc_bot.py    # Full implementation: MT5Connector, VarisSMCEngine, main loop
├── SYSTEM_PROMPT.md    # Agent scope / operating specification
├── requirements.txt    # Python dependencies
└── README.md
```

## Setup & Execution

### 1. Environment requirements
- **Windows OS** (Windows 10/11 or Windows VPS) — required for native MT5 IPC.
- **Python 3.10+** — check "Add Python to PATH" during installation.
- **MetaTrader 5 terminal** — downloaded from your broker (e.g., IC Markets, Exness, Deriv).

### 2. Configure MT5 Terminal
1. Open MetaTrader 5 and log into your broker account.
2. Go to **Tools → Options → Expert Advisors**.
3. Check **Allow Algo Trading**.
4. Ensure the traded pair (e.g., `EURAUD`) is added to Market Watch (`Ctrl + M`).

### 3. Install Python dependencies
```powershell
pip install -r requirements.txt
```
(or directly: `pip install MetaTrader5 pandas numpy`)

### 4. Run the bot
```powershell
python varis_smc_bot.py
```

## Configuration

Defaults are set at the top of `main()` in `varis_smc_bot.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SYMBOL` | `EURAUD` | Traded instrument |
| `RISK_PERCENT` | `1.0` | Percent of account balance risked per trade (drives dynamic lot size) |
| `POLL_INTERVAL_SECONDS` | `60` | Scanner loop interval |
| `atr_period` | `14` | ATR period for displacement filter |
| `displacement_mult` | `1.5` | Candle body multiple of ATR to qualify as displacement |
| `magic` | `202609` | Order magic number for tracking bot orders |


## Refactor status — market structure foundation

The branch refactor/market-structure-foundation introduces the first production-oriented foundation without changing the existing live bot:

- closed-candle MT5 market-data adapter (copy_rates_from_pos starting at position 1)
- broker symbol metadata model
- confirmed pivot swing detection
- explicit buy-side/sell-side liquidity pools
- wick-through/close-back liquidity sweep events
- structural break events based on actual level crossing
- bounded post-sweep CSD confirmation
- typed domain models for swings, liquidity, POIs and structure state
- pytest fixtures for swing/sweep/CSD behavior
- package metadata for a src/ layout

This is deliberately additive. The existing varis_smc_bot.py remains untouched until the new primitives are validated against historical replay and then wired into the strategy state machine.

### Next implementation sequence

1. Validate swing/liquidity/CSD primitives against historical candles.
2. Add D1 POI lifecycle: displacement, active/unmitigated, touch, mitigation and invalidation.
3. Add M15 order-block and inducement detection.
4. Add structural IRL targeting.
5. Replace the current risk/execution layer with broker-aware validation using MT5 symbol properties, order_check() and order_calc_profit().
6. Introduce the setup state machine and only then connect the new engine to live/demo execution.


### Phase 3 — D1 POI lifecycle

Added `src/smc_engine/poi.py` and `tests/test_poi.py`.

The D1 layer now has explicit primitives for:

- ATR-based displacement (body >= 1.5 ATR by default)
- directional displacement / close-location filtering
- D1 demand and supply POIs
- POI lifecycle state: ACTIVE, TOUCHED, MITIGATED, INVALIDATED
- filtering for currently active/unmitigated POIs
- explicit price-in-POI checks

The strategy still does not place orders from these new primitives. The old live bot remains isolated until the complete setup state machine exists.


### Phase 4 — M15 execution structure

Added `src/smc_engine/execution_structure.py` and `tests/test_execution_structure.py`.

The execution layer now has explicit primitives for:

- M15 Order Blocks derived from the last opposite-direction candle before displacement
- OB high/low zone boundaries
- directional OB mitigation price
- M15 inducement candidates derived from confirmed directional swings after the OB
- joining D1 POI + M15 OB + IDM into an execution context

These are detection primitives only. They do not place orders and are not yet connected to the live bot.


### Phase 5 — IRL and TradeSetup contract

Added `src/smc_engine/setup.py` and `tests/test_setup.py`.

The strategy boundary now includes:

- structural IRL targeting from the nearest unmitigated swing beyond entry
- protected level derived from the H4 sweep extreme
- deterministic entry from M15 OB mitigation
- stop-loss geometry beyond the protected level
- setup invalidation level
- typed `TradeSetup` contract linking D1 POI, H4 sweep/CSD, M15 OB/IDM and IRL
- reward/risk distance and R:R calculations

The setup contract is still analysis-only. MT5 risk sizing and order validation remain a separate execution concern.


### Phase 6 — Risk and MT5 execution boundary

Added `src/smc_engine/risk.py`, `src/smc_engine/execution.py`, and tests.

The new execution boundary now provides:

- broker-aware risk sizing from tick size/value
- volume-step flooring
- rejection when broker minimum volume would exceed requested risk
- stop/freeze distance validation
- pending BUY_LIMIT / SELL_LIMIT validation against live bid/ask
- typed pending-order request
- MT5 request translation
- explicit `order_check()` preflight
- separate `order_send()` execution call
- preserved magic number 202609

The legacy live bot is still isolated. The new adapter is intentionally not wired into live execution until replay/integration validation is complete.
