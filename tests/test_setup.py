import pytest
import pandas as pd

from src.smc_engine.execution_structure import ExecutionContext, Inducement, OrderBlock
from src.smc_engine.models import Direction, LiquiditySide, LiquiditySweep, POI, SetupState, StructureEvent, StructureEventType, SwingPoint, SwingType
from src.smc_engine.setup import build_trade_setup, evaluate_setup_lifecycle, find_irl_target


def swing(i, typ, price):
    return SwingPoint(f"S-{i}", i, i, typ, price, 6, i, i)


def candles(rows):
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])


def make_setup():
    poi = POI("D1-1", Direction.BULLISH, "D1", 95, 100, 1, pd.Timestamp("2026-01-01 00:00Z"))
    ob = OrderBlock("OB-1", Direction.BULLISH, "M15", 5, 5, 98, 100, 100, 6)
    idm = Inducement("IDM-1", Direction.BULLISH, 7, 7, 97, 7, "OB-1")
    ctx = ExecutionContext(poi, ob, idm, Direction.BULLISH)
    sweep = LiquiditySweep("SW-1", LiquiditySide.SELL_SIDE, 96, 94, "LQ-1", 10, 10, 97)
    csd = StructureEvent("CSD-1", StructureEventType.CSD, Direction.BULLISH, 101, 12, 12, "SH-1", "SW-1")
    irl = find_irl_target(100, Direction.BULLISH, [swing(13, SwingType.HIGH, 110)])
    return build_trade_setup("EURAUD", ctx, sweep, csd, irl, created_time=pd.Timestamp("2026-01-01 00:00Z"))


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
    setup = make_setup()
    assert setup.entry == 100
    assert setup.stop_loss == 94
    assert setup.take_profit == 110
    assert setup.invalidation_level == 94
    assert setup.risk_reward == pytest.approx(10 / 6)


def test_setup_lifecycle_triggers_then_fills():
    setup = make_setup()
    df = candles([
        ["2026-01-01 00:15Z", 101, 103, 101, 102],
        ["2026-01-01 00:30Z", 102, 105, 99, 101],
        ["2026-01-01 00:45Z", 101, 111, 100, 110],
    ])
    result = evaluate_setup_lifecycle(setup, df)
    assert result.state is SetupState.FILLED
    assert result.event_index == 2


def test_setup_lifecycle_invalidates_before_entry():
    setup = make_setup()
    df = candles([["2026-01-01 00:15Z", 101, 99, 93, 94]])
    result = evaluate_setup_lifecycle(setup, df)
    assert result.state is SetupState.INVALIDATED
    assert result.reason == "protected_level_breached_before_entry"


def test_setup_lifecycle_expires_pending_entry():
    setup = make_setup()
    df = candles([[f"2026-01-01 00:{15*i:02d}Z", 101, 102, 101, 101] for i in range(1, 4)])
    result = evaluate_setup_lifecycle(setup, df, expiry_bars=3)
    assert result.state is SetupState.EXPIRED


def test_setup_lifecycle_marks_same_candle_entry_stop_ambiguity():
    setup = make_setup()
    df = candles([["2026-01-01 00:15Z", 101, 101, 93, 100]])
    result = evaluate_setup_lifecycle(setup, df)
    assert result.state is SetupState.AMBIGUOUS


def test_setup_lifecycle_does_not_use_creation_candle():
    setup = make_setup()
    df = candles([["2026-01-01 00:00Z", 100, 111, 93, 100]])
    result = evaluate_setup_lifecycle(setup, df)
    assert result.state is SetupState.PENDING


def test_setup_lifecycle_rejects_duplicate_timestamps():
    setup = make_setup()
    df = candles([
        ["2026-01-01 00:15Z", 101, 103, 101, 102],
        ["2026-01-01 00:15Z", 102, 105, 99, 101],
    ])
    with pytest.raises(ValueError, match="timestamps must be unique"):
        evaluate_setup_lifecycle(setup, df)


def test_setup_lifecycle_rejects_out_of_order_timestamps():
    setup = make_setup()
    df = candles([
        ["2026-01-01 00:30Z", 101, 103, 101, 102],
        ["2026-01-01 00:15Z", 102, 105, 99, 101],
    ])
    with pytest.raises(ValueError, match="strictly chronological"):
        evaluate_setup_lifecycle(setup, df)
