
import pandas as pd

from src.smc_engine.models import Direction
from src.smc_engine.replay import ReplayBroker, ReplayOrderState, summarize
from src.smc_engine.setup import TradeSetup


def setup(direction=Direction.BULLISH):
    return TradeSetup(
        id="R1", symbol="TEST", direction=direction, created_time=1,
        poi_id="P", sweep_id="SW", csd_id="CSD", protected_level=99,
        order_block_id="OB", inducement_id="IDM", entry=100,
        stop_loss=99, take_profit=102, irl_swing_id="IRL",
        invalidation_level=99, risk_percent=1,
    )


def candles(rows):
    return pd.DataFrame(rows, columns=["time","open","high","low","close"])


def test_bullish_pending_limit_fills_then_hits_target():
    x = candles([
        [1, 105, 106, 103, 104],
        [2, 104, 104, 100, 101],
        [3, 101, 103, 100, 102],
    ])
    result = ReplayBroker(x).run(setup())
    assert result.order_state is ReplayOrderState.TARGETED
    assert result.entry == 100
    assert result.exit == 102
    assert result.pnl_price == 2


def test_pending_order_invalidates_before_fill():
    x = candles([
        [1, 105, 106, 103, 104],
        [2, 104, 100, 98, 99],
        [3, 99, 102, 99, 101],
    ])
    result = ReplayBroker(x).run(setup())
    assert result.order_state is ReplayOrderState.INVALIDATED
    assert result.entry is None


def test_same_candle_invalidation_wins_before_fill():
    x = candles([[1, 101, 103, 98, 101]])
    result = ReplayBroker(x).run(setup())
    assert result.order_state is ReplayOrderState.INVALIDATED


def test_summary():
    x = candles([[1, 105, 106, 103, 104], [2, 104, 104, 100, 101], [3, 101, 103, 100, 102]])
    results = [ReplayBroker(x).run(setup())]
    metrics = summarize(results)
    assert metrics.setups == 1
    assert metrics.targets == 1
