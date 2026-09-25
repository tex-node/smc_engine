from dataclasses import dataclass
from types import SimpleNamespace

from src.smc_engine.models import Direction
from src.smc_engine.order_guard import MT5OrderGuard
from src.smc_engine.setup import TradeSetup


def setup(direction=Direction.BULLISH):
    return TradeSetup(
        id="SETUP-X",
        symbol="EURUSD",
        direction=direction,
        created_time="2026-01-01T00:00:00Z",
        poi_id="P",
        sweep_id="S",
        csd_id="C",
        protected_level=1.0,
        order_block_id="OB",
        inducement_id="IDM",
        entry=1.1 if direction is Direction.BULLISH else 0.9,
        stop_loss=0.99 if direction is Direction.BULLISH else 1.01,
        take_profit=1.2 if direction is Direction.BULLISH else 0.8,
        irl_swing_id="IRL",
        invalidation_level=1.0,
        risk_percent=1,
    )


class FakeMT5:
    def __init__(self):
        self.orders = []
        self.positions_ = []

    def orders_get(self, symbol):
        return tuple(self.orders)

    def positions_get(self, symbol):
        return tuple(self.positions_)

    def last_error(self):
        return (0, "ok")

    def order_send(self, request):
        return request


def test_duplicate_setup_identity_is_detected():
    mt5 = FakeMT5()
    mt5.orders = [SimpleNamespace(magic=202609, comment="SMC SETUP-X")]
    guard = MT5OrderGuard(mt5, 202609)
    assert guard.has_active_identity(setup())


def test_live_bullish_invalidation():
    guard = MT5OrderGuard(FakeMT5(), 202609)
    assert guard.setup_is_still_valid(setup(), bid=1.01, ask=1.011)
    assert not guard.setup_is_still_valid(setup(), bid=0.99, ask=0.991)


def test_live_bearish_invalidation():
    s = setup(Direction.BEARISH)
    guard = MT5OrderGuard(FakeMT5(), 202609)
    assert guard.setup_is_still_valid(s, bid=0.89, ask=0.90)
    assert not guard.setup_is_still_valid(s, bid=1.01, ask=1.02)


def test_setup_identity_does_not_match_prefix_collision():
    mt5 = FakeMT5()
    mt5.orders = [SimpleNamespace(magic=202609, comment="SMC SETUP-X2")]
    guard = MT5OrderGuard(mt5, 202609)
    assert not guard.has_active_identity(setup())
