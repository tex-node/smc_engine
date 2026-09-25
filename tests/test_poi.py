
import pandas as pd

from src.smc_engine.models import Direction, POIState
from src.smc_engine.poi import DisplacementConfig, active_unmitigated_pois, build_d1_pois, detect_displacement, price_is_at_poi, update_poi_lifecycle

def df(rows):
    return pd.DataFrame(rows, columns=["time","open","high","low","close"])

def test_displacement_requires_body_and_close_location():
    x = df([
        [1, 100, 101, 99, 100.5],
        [2, 100.5, 101, 100, 100.8],
        [3, 100.8, 104, 100, 103.8],
        [4, 103.8, 104, 103, 103.5],
    ])
    out = detect_displacement(x, DisplacementConfig(atr_period=2, body_atr_multiple=1.5))
    assert bool(out.iloc[2]["displacement_bullish"])

def test_bullish_poi_can_be_touched_then_invalidated():
    x = df([
        [1,100,101,99,100],
        [2,100,105,99,104],
        [3,104,104.5,101,102],
        [4,102,103,98,98.5],
    ])
    pois = build_d1_pois(x, lookback_bars=4, config=DisplacementConfig(atr_period=2, body_atr_multiple=1.0))
    bullish = next(p for p in pois if p.direction is Direction.BULLISH)
    state = update_poi_lifecycle(bullish, x)
    assert state.state is POIState.INVALIDATED
    assert state.invalidated_index is not None

def test_active_unmitigated_poi_is_at_current_price():
    x = df([
        [1,100,101,99,100],
        [2,100,105,99,104],
        [3,104,104.5,102,103],
        [4,103,104,102.5,103.5],
    ])
    pois = active_unmitigated_pois(x, lookback_bars=4, config=DisplacementConfig(atr_period=2, body_atr_multiple=1.0))
    assert pois
    assert any(price_is_at_poi(103.5, p) for p in pois)
