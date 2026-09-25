
import pandas as pd

from src.smc_engine.execution_structure import find_inducements, find_order_blocks
from src.smc_engine.models import Direction, SwingPoint, SwingType


def candles(rows):
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])


def test_bullish_order_block_is_last_bearish_candle():
    x = candles([
        [1, 100, 102, 99, 101],
        [2, 101, 102, 98, 99],
        [3, 99, 100, 97, 98],
        [4, 98, 105, 97, 104],
        [5, 104, 106, 103, 105],
    ])
    blocks = find_order_blocks(x, [3])
    assert len(blocks) == 1
    assert blocks[0].direction is Direction.BULLISH
    assert blocks[0].candle_index == 2
    assert blocks[0].low == 97
    assert blocks[0].high == 100
    assert blocks[0].mitigation_price == 100


def test_bearish_order_block_is_last_bullish_candle():
    x = candles([
        [1, 100, 103, 99, 102],
        [2, 102, 104, 101, 103],
        [3, 103, 104, 100, 99],
        [4, 99, 100, 96, 97],
    ])
    blocks = find_order_blocks(x, [2])
    assert blocks[0].direction is Direction.BEARISH
    assert blocks[0].candle_index == 1
    assert blocks[0].mitigation_price == 101


def test_inducement_uses_directional_swing_after_order_block():
    x = candles([
        [1, 100, 101, 99, 100],
        [2, 100, 102, 98, 99],
        [3, 99, 103, 98, 102],
        [4, 102, 104, 101, 103],
        [5, 103, 105, 100, 104],
    ])
    blocks = find_order_blocks(x, [2])
    swings = [
        SwingPoint("SL-3", 3, 4, SwingType.LOW, 101, 6, 3, 4),
        SwingPoint("SH-3", 3, 4, SwingType.HIGH, 104, 6, 3, 4),
    ]
    idms = find_inducements(x, blocks, swings)
    assert len(idms) == 1
    assert idms[0].direction is Direction.BULLISH
    assert idms[0].level == 101



def test_inducement_requires_swing_confirmation_by_as_of():
    x = candles([
        [1, 100, 101, 99, 100],
        [2, 100, 102, 98, 99],
        [3, 99, 103, 98, 102],
        [4, 102, 104, 101, 103],
        [5, 103, 105, 100, 104],
    ])
    blocks = find_order_blocks(x, [2])
    late = SwingPoint("SL-3", 3, 4, SwingType.LOW, 101, 6, 4, 5)
    early = SwingPoint("SL-3E", 3, 4, SwingType.LOW, 101, 6, 3, 4)

    assert find_inducements(x, blocks, [late], as_of=4) == []
    assert len(find_inducements(x, blocks, [early], as_of=4)) == 1


def test_inducement_window_is_anchored_to_order_block():
    x = candles([[i, 100, 101, 99, 100] for i in range(10)])
    # Displacement is intentionally much later than the OB. A swing after the
    # OB window but within displacement+window must not qualify as an IDM.
    blocks = [
        __import__("src.smc_engine.execution_structure", fromlist=["OrderBlock"]).OrderBlock(
            id="OB-M15-1-BULLISH",
            direction=Direction.BULLISH,
            timeframe="M15",
            candle_index=1,
            candle_time=2,
            low=98,
            high=100,
            mitigation_price=100,
            source_displacement_index=8,
        )
    ]
    late_swing = SwingPoint("SL-LATE", 7, 8, SwingType.LOW, 99, 5, 7, 8)

    assert find_inducements(x, blocks, [late_swing], max_bars_after_ob=3) == []
