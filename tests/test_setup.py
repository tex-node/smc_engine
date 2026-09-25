
import pytest

from src.smc_engine.execution_structure import ExecutionContext, Inducement, OrderBlock
from src.smc_engine.models import Direction, LiquiditySide, LiquiditySweep, POI, StructureEvent, StructureEventType, SwingPoint, SwingType
from src.smc_engine.setup import build_trade_setup, find_irl_target


def swing(i, typ, price):
    return SwingPoint(f"S-{i}", i, i, typ, price, 6, i, i)


def test_bullish_irl_is_nearest_unmitigated_high_above_entry():
    swings = [swing(1, SwingType.HIGH, 110), swing(2, SwingType.HIGH, 106), swing(3, SwingType.LOW, 95)]
    target = find_irl_target(100, Direction.BULLISH, swings)
    assert target is not None
    assert target.price == 106


def test_bearish_irl_is_nearest_unmitigated_low_below_entry():
    swings = [swing(1, SwingType.LOW, 90), swing(2, SwingType.LOW, 96), swing(3, SwingType.HIGH, 105)]
    target = find_irl_target(100, Direction.BEARISH, swings)
    assert target is not None
    assert target.price == 96


def test_irl_skips_mitigated_structural_liquidity():
    swings = [swing(1, SwingType.HIGH, 106), swing(2, SwingType.HIGH, 108)]
    target = find_irl_target(100, Direction.BULLISH, swings, {"S-1"})
    assert target is not None
    assert target.price == 108


def test_trade_setup_uses_sweep_extreme_as_protected_stop_reference():
    poi = POI("D1-1", Direction.BULLISH, "D1", 95, 100, 1, 1)
    ob = OrderBlock("OB-1", Direction.BULLISH, "M15", 5, 5, 98, 100, 100, 6)
    idm = Inducement("IDM-1", Direction.BULLISH, 7, 7, 97, 7, "OB-1")
    ctx = ExecutionContext(poi, ob, idm, Direction.BULLISH)
    sweep = LiquiditySweep("SW-1", LiquiditySide.SELL_SIDE, 96, 94, "LQ-1", 10, 10, 97)
    csd = StructureEvent("CSD-1", StructureEventType.CSD, Direction.BULLISH, 101, 12, 12, "SH-1", "SW-1")
    irl = find_irl_target(100, Direction.BULLISH, [swing(13, SwingType.HIGH, 110)])
    setup = build_trade_setup("EURAUD", ctx, sweep, csd, irl)
    assert setup.entry == 100
    assert setup.stop_loss == 94
    assert setup.take_profit == 110
    assert setup.invalidation_level == 94
    assert setup.risk_reward == pytest.approx(10 / 6)
