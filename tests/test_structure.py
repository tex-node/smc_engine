
import pandas as pd
from src.smc_engine.structure import build_liquidity_pools, confirm_csd, detect_structure_breaks, detect_sweeps, find_swings

def candles(rows):
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])

def test_find_confirmed_swings():
    df = candles([
        [1,10,12,9,11], [2,11,13,10,12], [3,12,15,11,14],
        [4,14,14,10,11], [5,11,12,8,9], [6,9,11,7,10],
        [7,10,11,8,9], [8,9,10,8,9],
    ])
    swings = find_swings(df, left=2, right=2)
    assert any(s.type.value == "HIGH" and s.price == 15 for s in swings)
    assert any(s.type.value == "LOW" and s.price == 7 for s in swings)

def test_sell_side_sweep_and_bullish_csd():
    df = candles([
        [1,10,12,10,11], [2,11,13,10.5,12], [3,12,14,9,10],
        [4,10,14,11,13], [5,13,13,8,12], [6,12,16,11,15],
        [7,15,17,13,16], [8,16,18,14,17],
    ])
    swings = find_swings(df, left=1, right=1)
    pools = build_liquidity_pools(swings)
    sweeps = detect_sweeps(df, pools, lookback_bars=4)
    bullish_sweep = next(s for s in sweeps if s.side.value == "SELL_SIDE")
    breaks = detect_structure_breaks(df, swings)
    csd = confirm_csd(df, bullish_sweep, breaks, max_bars_after_sweep=4)
    assert csd is not None
    assert csd.direction.value == "BULLISH"
    assert csd.source_sweep_id == bullish_sweep.id
