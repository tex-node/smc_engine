
import pandas as pd

from src.smc_engine.models import Direction, POIState
from src.smc_engine.poi import DisplacementConfig, active_unmitigated_pois, build_d1_pois, detect_displacement, price_is_at_poi, update_poi_lifecycle

def df(rows):
    return pd.DataFrame(rows, columns=["time","open","high","low","close"])

def test_displacement_requires_body_and_close_location():
    x = df([
        [1, 100, 101, 99, 100.5],
        [2, 100.5, 101, 100, 100.8],
        [3, 100.8, 108, 100, 107.8],
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


def test_d1_poi_identity_is_stable_when_lookback_window_moves():
    times = pd.date_range("2026-01-01", periods=20, freq="D", tz="UTC")
    rows = []
    for i, t in enumerate(times):
        rows.append([t, 100.0, 101.0, 99.0, 100.0])
    rows[14] = [times[14], 100.0, 108.0, 99.0, 107.8]
    x = df(rows)

    config = DisplacementConfig(atr_period=2, body_atr_multiple=1.0)
    wide = next(p for p in build_d1_pois(x, lookback_bars=20, config=config) if p.created_time == times[14])
    narrow = next(p for p in build_d1_pois(x, lookback_bars=6, config=config) if p.created_time == times[14])

    assert wide.id == narrow.id

# ---------------------------------------------------------------------------
# Regression tests for the created_index coordinate-frame bug
# (production: 150 D1 bars, lookback=90 -> tail-slice index != absolute index)
# ---------------------------------------------------------------------------

def _make_df(rows):
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])


# TEST 1 - Tail index translation
def test_poi_created_index_is_absolute_not_tail_relative():
    """created_index must equal the displacement bar's absolute position in df."""
    times = pd.date_range("2026-01-01", periods=20, freq="D", tz="UTC")
    rows = [[t, 100.0, 100.2, 99.8, 100.0] for t in times]
    # bearish displacement at absolute index 14 (tight pre-creation ATR -> large ratio)
    rows[14] = [times[14], 109.0, 110.5, 101.0, 102.0]
    x = _make_df(rows)
    config = DisplacementConfig(atr_period=2, body_atr_multiple=1.0)
    # lookback=6 => tail covers bars 14-19; bar 14 is work[0], buggy index would be 0
    pois = build_d1_pois(x, lookback_bars=6, config=config)
    poi = next((p for p in pois if p.created_time == times[14]), None)
    assert poi is not None, "displacement at bar 14 must be detected"
    assert poi.created_index == 14, (
        f"created_index={poi.created_index}; must be 14 (absolute), "
        "not 0 (tail-slice index)"
    )


# TEST 2 - Pre-creation price must not mitigate
def test_precreation_zone_touch_does_not_mitigate_poi():
    """Reproduces the production failure: bars BEFORE a POI is created must not
    affect its lifecycle state."""
    times = pd.date_range("2026-01-01", periods=20, freq="D", tz="UTC")
    # Bars 0-13: above the future zone; tight range so ATR stays small
    rows = [[t, 110.0, 111.0, 109.0, 110.0] for t in times]
    # Bar 14: bearish displacement, zone = [101.0, 110.5]
    # Pre-creation close=110 >= zone_low=101 AND pre-creation high=111 >= zone_high=110.5
    # => WOULD be MITIGATED at bar 0 under the old bug
    rows[14] = [times[14], 109.0, 110.5, 101.0, 102.0]
    # Bars 15-19: well below zone.low=101; no post-creation mitigation
    for i in range(15, 20):
        rows[i] = [times[i], 100.0, 100.5, 99.0, 99.5]
    x = _make_df(rows)
    config = DisplacementConfig(atr_period=2, body_atr_multiple=1.0)
    pois = active_unmitigated_pois(x, lookback_bars=6, config=config)
    assert pois, (
        "POI must not be mitigated by pre-creation price action — "
        "this directly reproduces the production bug"
    )


# TEST 3 - Post-creation mitigation still works
def test_postcreation_mitigation_still_triggers():
    """The fix must not disable lifecycle evaluation after creation."""
    times = pd.date_range("2026-01-01", periods=20, freq="D", tz="UTC")
    rows = [[t, 110.0, 111.0, 109.0, 110.0] for t in times]
    rows[14] = [times[14], 109.0, 110.5, 101.0, 102.0]  # bearish zone=[101, 110.5]
    # Bar 15: close=108 >= zone.low=101 AND high=111.0 >= zone.high=110.5 -> MITIGATED
    rows[15] = [times[15], 105.0, 111.0, 100.0, 108.0]
    for i in range(16, 20):
        rows[i] = [times[i], 100.0, 100.5, 99.0, 99.5]
    x = _make_df(rows)
    config = DisplacementConfig(atr_period=2, body_atr_multiple=1.0)
    pois = build_d1_pois(x, lookback_bars=6, config=config)
    poi = next((p for p in pois if p.created_time == times[14]), None)
    assert poi is not None
    state = update_poi_lifecycle(poi, x)
    assert state.state is POIState.MITIGATED, "post-creation mitigation must still work"


# TEST 4 - Full-length lookback regression (offset == 0, existing behavior unchanged)
def test_full_length_lookback_offset_is_zero():
    """When lookback_bars == len(df) the offset is 0 and created_index is unchanged."""
    times = pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC")
    rows = [[t, 100.0, 100.2, 99.8, 100.0] for t in times]
    rows[2] = [times[2], 100.0, 108.0, 99.0, 107.8]  # bullish displacement at absolute 2
    x = _make_df(rows)
    config = DisplacementConfig(atr_period=2, body_atr_multiple=1.0)
    pois = build_d1_pois(x, lookback_bars=5, config=config)
    poi = next((p for p in pois if p.created_time == times[2]), None)
    assert poi is not None
    assert poi.created_index == 2  # absolute == tail-slice index when lookback == len(df)


# TEST 5 - Short dataframe (lookback > len(df) -> tail returns full df, offset=0)
def test_short_df_lookback_larger_than_length():
    """When lookback_bars > len(df), tail returns the full df and offset is 0."""
    times = pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC")
    rows = [[t, 100.0, 100.2, 99.8, 100.0] for t in times]
    rows[3] = [times[3], 100.0, 108.0, 99.0, 107.8]  # displacement at absolute 3
    x = _make_df(rows)
    config = DisplacementConfig(atr_period=2, body_atr_multiple=1.0)
    pois = build_d1_pois(x, lookback_bars=100, config=config)
    poi = next((p for p in pois if p.created_time == times[3]), None)
    assert poi is not None
    assert poi.created_index == 3  # absolute; no slicing so offset is 0


# TEST 6 - Multiple POIs all have absolute indices
def test_multiple_pois_all_have_absolute_indices():
    """Every POI in the tail window must carry its absolute df index."""
    times = pd.date_range("2026-01-01", periods=20, freq="D", tz="UTC")
    # Tight base bars (range=0.2) keep ATR small so both displacement candles clear ratio>=1.0
    rows = [[t, 100.0, 100.1, 99.9, 100.0] for t in times]
    # Bar 12 (absolute): bearish displacement inside tail(10) window (bars 10-19)
    # TR[11]=0.2, TR[12]=11, ATR[12]=5.6; body=8, ratio=1.43 OK; cl=0.9 OK
    rows[12] = [times[12], 110.0, 111.0, 101.0, 102.0]
    # Bar 16 (absolute): bullish displacement
    # After bar 12 the series returns to tight: TR[15]=0.2, TR[16]=8.4, ATR[16]=4.3
    # body=8.0, ratio=1.86 OK; close_location~1.0 OK
    rows[16] = [times[16], 100.0, 108.2, 99.8, 108.0]
    x = _make_df(rows)
    config = DisplacementConfig(atr_period=2, body_atr_multiple=1.0)
    pois = build_d1_pois(x, lookback_bars=10, config=config)
    bear = next((p for p in pois if p.created_time == times[12]), None)
    bull = next((p for p in pois if p.created_time == times[16]), None)
    assert bear is not None, "bearish displacement at bar 12 must be detected"
    assert bull is not None, "bullish displacement at bar 16 must be detected"
    assert bear.created_index == 12
    assert bull.created_index == 16


# TEST 7 - Directional lifecycle semantics preserved
def test_bearish_poi_invalidated_when_close_above_high():
    """Bearish POI invalidation rule must be unchanged by the index fix."""
    times = pd.date_range("2026-01-01", periods=8, freq="D", tz="UTC")
    rows = [[t, 100.0, 100.2, 99.8, 100.0] for t in times]
    rows[2] = [times[2], 108.0, 109.0, 99.0, 100.5]  # bearish displacement, zone=[99, 109]
    rows[3] = [times[3], 100.5, 110.0, 99.5, 110.0]  # close=110 > zone.high=109 -> INVALIDATED
    x = _make_df(rows)
    config = DisplacementConfig(atr_period=2, body_atr_multiple=1.0)
    pois = build_d1_pois(x, lookback_bars=8, config=config)
    bearish = next((p for p in pois if p.direction is Direction.BEARISH), None)
    assert bearish is not None
    state = update_poi_lifecycle(bearish, x)
    assert state.state is POIState.INVALIDATED


# TEST 8 - Production-shaped regression: 150 bars, lookback=90
def test_production_shaped_150bar_lookback90_regression():
    """Exact production geometry: 150 bars, lookback=90, displacement near bar 144.

    With the bug:  created_index=84, lifecycle starts at bar 85 (a high-price bar),
                   POI is immediately INVALIDATED by pre-creation data.
    With the fix:  created_index=144, lifecycle starts at bar 145 (low-price bar),
                   POI survives and is ACTIVE or TOUCHED.
    """
    N = 150
    lookback = 90
    disp_idx = 144  # near the end of the dataset

    times = pd.date_range("2026-04-01", periods=N, freq="B", tz="UTC")
    rows = []
    # Bars 0-59: NOT in the tail(90) window
    for i in range(60):
        rows.append([times[i], 103.0, 103.2, 102.8, 103.0])
    # Bars 60-84: in the tail window, price at HIGH level (110) -> tight ATR
    for i in range(60, 85):
        rows.append([times[i], 110.0, 110.2, 109.8, 110.0])
    # Bars 85-143: in the tail window, price at LOW level (103)
    for i in range(85, 144):
        rows.append([times[i], 103.0, 103.2, 102.8, 103.0])
    # Bar 144: bearish displacement, zone = [98, 104.6]
    # Close=99 well below zone; previous tight bars keep ATR small -> large ratio
    rows.append([times[144], 104.5, 104.6, 98.0, 99.0])
    # Bars 145-149: post-creation, close=103, which is in zone [98, 104.6] -> TOUCHED
    # but not MITIGATED (high=103.2 < zone.high=104.6) and not INVALIDATED
    for i in range(145, 150):
        rows.append([times[i], 103.0, 103.2, 102.8, 103.0])

    x = _make_df(rows)
    config = DisplacementConfig(atr_period=2, body_atr_multiple=1.0)

    pois = build_d1_pois(x, lookback_bars=lookback, config=config)
    poi = next((p for p in pois if p.created_time == times[disp_idx]), None)
    assert poi is not None, f"displacement at bar {disp_idx} must be detected"

    buggy_index = disp_idx - (N - lookback)  # = 84 under the old bug
    assert poi.created_index == disp_idx, (
        f"created_index={poi.created_index}; "
        f"must be {disp_idx} (absolute), not {buggy_index} (tail-slice)"
    )

    # With the old bug: lifecycle would start at bar 85 (close=110.0 > zone.high=104.6)
    # -> INVALIDATED immediately. With the fix: starts at bar 145 -> ACTIVE or TOUCHED.
    state = update_poi_lifecycle(poi, x)
    assert state.state in {POIState.ACTIVE, POIState.TOUCHED}, (
        f"POI state={state.state.value}; must be ACTIVE or TOUCHED after fix "
        "(was INVALIDATED under the old bug because bar 85 close=110.0 > zone.high=104.6)"
    )

    # active_unmitigated_pois must also return it
    active = active_unmitigated_pois(x, lookback_bars=lookback, config=config)
    assert any(p.created_time == times[disp_idx] for p in active), (
        "active_unmitigated_pois must return the POI after the index fix"
    )
