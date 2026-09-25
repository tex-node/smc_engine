
import pytest

from src.smc_engine.lifecycle import SetupLifecycle, SetupRegistry, SetupState
from src.smc_engine.models import Direction
from src.smc_engine.setup import TradeSetup


def make_setup(id="S-1"):
    return TradeSetup(
        id=id, symbol="EURAUD", direction=Direction.BULLISH, created_time=1,
        poi_id="P", sweep_id="SW", csd_id="CSD", protected_level=1.0,
        order_block_id="OB", inducement_id="IDM", entry=1.1,
        stop_loss=0.99, take_profit=1.2, irl_swing_id="IRL",
        invalidation_level=1.0, risk_percent=1.0,
    )


def test_valid_order_lifecycle():
    x = SetupLifecycle(make_setup())
    x.transition(SetupState.ORDER_PREPARED)
    x.transition(SetupState.ORDER_PREFLIGHTED)
    x.transition(SetupState.ORDER_SUBMITTING)
    x.transition(SetupState.ORDER_PLACED)
    x.transition(SetupState.FILLED)
    x.transition(SetupState.POSITION_MANAGED)
    x.transition(SetupState.CLOSED)
    assert x.state is SetupState.CLOSED


def test_invalid_transition_is_rejected():
    x = SetupLifecycle(make_setup())
    with pytest.raises(ValueError):
        x.transition(SetupState.FILLED)


def test_registry_deduplicates_and_tracks_active_symbol():
    registry = SetupRegistry()
    x = SetupLifecycle(make_setup())
    registry.add(x)
    assert registry.has_active_for_symbol("EURAUD")
    with pytest.raises(ValueError):
        registry.add(SetupLifecycle(make_setup()))
    x.transition(SetupState.ORDER_PREPARED)
    x.transition(SetupState.ENTRY_NO_LONGER_VALID)
    assert not registry.has_active_for_symbol("EURAUD")


def test_market_invalidation():
    x = SetupLifecycle(make_setup())
    x.invalidate_from_market(protected_level_breached=True)
    assert x.state is SetupState.PROTECTED_LEVEL_BREACHED
