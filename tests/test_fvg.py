import pandas as pd
import pytest

from src.smc_engine.fvg import detect_fvgs
from src.smc_engine.models import Direction


def frame(rows):
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])


def make_df(candles):
    base = pd.Timestamp("2026-01-01", tz="UTC")
    return frame([[base + pd.Timedelta(hours=i), o, h, l, c] for i, (o, h, l, c) in enumerate(candles)])


def test_bullish_gap_detected_between_first_and_third_candle():
    # candle 2 low (104) > candle 0 high (102) => bullish FVG [102,104]
    df = make_df([(100, 102, 99, 101), (101, 105, 101, 104), (104, 108, 104, 107)])
    gaps = detect_fvgs(df)
    assert len(gaps) == 1
    g = gaps[0]
    assert g.direction is Direction.BULLISH
    assert g.bottom == 102 and g.top == 104
    assert g.state == "ACTIVE"


def test_bearish_gap_detected():
    # candle 2 high (98) < candle 0 low (100) => bearish FVG [98,100]
    df = make_df([(101, 102, 100, 100.5), (100, 100.5, 96, 97), (97, 98, 95, 96)])
    gaps = detect_fvgs(df)
    assert len(gaps) == 1
    g = gaps[0]
    assert g.direction is Direction.BEARISH
    assert g.bottom == 98 and g.top == 100


def test_bullish_gap_fully_mitigated_when_price_trades_below_bottom():
    df = make_df([
        (100, 102, 99, 101),
        (101, 105, 101, 104),
        (104, 108, 104, 107),   # gap [102,104] created
        (107, 108, 103, 104),   # touches into gap
        (104, 104.5, 101.5, 102),  # trades below bottom -> mitigated
    ])
    gaps = detect_fvgs(df)
    g = gaps[0]
    assert g.state == "MITIGATED"
    assert g.mitigation_index == 4


def test_partial_touch_is_marked_touched_not_mitigated():
    df = make_df([
        (100, 102, 99, 101),
        (101, 105, 101, 104),
        (104, 108, 104, 107),
        (107, 108, 102.5, 103),  # dips into gap but stays above bottom
    ])
    gaps = detect_fvgs(df)
    assert gaps[0].state == "TOUCHED"


def test_no_gap_when_candles_overlap():
    df = make_df([(100, 103, 99, 102), (102, 105, 101, 104), (104, 106, 100, 102)])
    assert detect_fvgs(df) == []
