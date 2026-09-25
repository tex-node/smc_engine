import pytest

from src.smc_engine.market import SymbolSpec
from src.smc_engine.models import Direction
from src.smc_engine.risk import RiskEngine, order_side, pending_price_is_valid
from src.smc_engine.setup import TradeSetup


SPEC = SymbolSpec(
    symbol="TEST",
    digits=5,
    point=0.00001,
    tick_size=0.00001,
    tick_value=1.0,
    volume_min=0.01,
    volume_max=100.0,
    volume_step=0.01,
    trade_stops_level=10,
    trade_freeze_level=0,
    filling_mode=0,
)


def setup(direction=Direction.BULLISH):
    return TradeSetup(
        id="S", symbol="TEST", direction=direction, created_time=1,
        poi_id="P", sweep_id="SW", csd_id="CSD", protected_level=1.0,
        order_block_id="OB", inducement_id="IDM",
        entry=1.1, stop_loss=1.0, take_profit=1.2,
        irl_swing_id="IRL", invalidation_level=1.0, risk_percent=1.0,
    )


def test_risk_sizing_floors_to_volume_step():
    q = RiskEngine(SPEC).volume_for_risk(1000, 1.0, 1.10000, 1.09900)
    assert q.volume == 0.1
    assert q.estimated_loss == pytest.approx(10.0)


def test_minimum_volume_does_not_silently_increase_risk():
    with pytest.raises(ValueError):
        RiskEngine(SPEC).volume_for_risk(1, 1.0, 1.10000, 1.09900)


def test_stop_distance_and_geometry_validation():
    RiskEngine(SPEC).validate_setup(setup())
    with pytest.raises(ValueError):
        RiskEngine(SPEC).validate_setup(
            TradeSetup(**{**setup().__dict__, "stop_loss": 1.09999})
        )


def test_pending_price_uses_live_bid_ask():
    assert pending_price_is_valid(Direction.BULLISH, 1.0990, 1.1000, 1.1002)
    assert not pending_price_is_valid(Direction.BULLISH, 1.1003, 1.1000, 1.1002)
    assert pending_price_is_valid(Direction.BEARISH, 1.1010, 1.1000, 1.1002)
    assert not pending_price_is_valid(Direction.BEARISH, 1.0990, 1.1000, 1.1002)


def test_order_side():
    assert order_side(Direction.BULLISH) == "BUY_LIMIT"
    assert order_side(Direction.BEARISH) == "SELL_LIMIT"
