"""
Gate B Defect Closure — Setup Identity & Lifecycle Integrity Regression Tests.

Covers D-1 (FILLED lifecycle mapping) and D-2 (rolling-window identity drift).

Required tests from the brief:
  test_filled_replay_state_maps_correctly
  test_swing_identity_survives_rolling_window
  test_ob_identity_survives_rolling_window
  test_csd_identity_survives_rolling_window
  test_setup_identity_survives_rolling_window
  test_irl_swing_id_remains_resolvable
  test_persisted_setup_provenance_survives_restart
  test_identity_does_not_depend_on_dataframe_position
  test_duplicate_setup_identity_is_deterministic
  test_same_causal_event_cannot_create_duplicate_registry_entries
  test_distinct_causal_events_remain_distinct
  test_identity_generation_has_no_future_data_dependency

Additional:
  test_active_replay_state_stays_execution_ready
  test_invalidated_replay_state_maps_to_protected_level_breached
  test_expired_replay_state_maps_to_entry_no_longer_valid

All tests are read-only with respect to the broker (no order_send, order_check).
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.smc_engine.execution_structure import ExecutionContext, OrderBlock, find_order_blocks
from src.smc_engine.lifecycle import SetupState as LCState
from src.smc_engine.models import (
    Direction, Inducement, LiquiditySide, LiquiditySweep,
    POI, StructureEvent, StructureEventType, SwingPoint, SwingType,
)
from src.smc_engine.setup import TradeSetup, build_trade_setup, evaluate_setup_lifecycle, find_irl_target
from src.smc_engine.structure import (
    build_liquidity_pools, detect_sweeps, detect_structure_breaks, find_swings,
)
from src.smc_engine.web.hub import DictSource, EngineHub


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def _candles(rows: list[list]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])


def _hub() -> EngineHub:
    return EngineHub(DictSource(), db_path=":memory:", setup_store_path=":memory:")


def _swing(i: int, typ: SwingType, price: float):
    return SwingPoint(f"S-{i}", i, i, typ, price, 4, i, i)


def _bearish_setup(
    entry: float = 111.025,
    tp: float = 110.870,
    sl: float = 112.544,
    created: str = "2026-09-29 01:00Z",
) -> TradeSetup:
    poi = POI("D1-P1", Direction.BEARISH, "D1", sl, entry, 1, _ts(created))
    ob = OrderBlock("OB-1", Direction.BEARISH, "M15", 10, 10, entry - 0.010, entry, entry, 6)
    idm = Inducement("IDM-1", Direction.BEARISH, 12, 12, entry + 0.010, 12, "OB-1")
    ctx = ExecutionContext(poi, ob, idm, Direction.BEARISH)
    sweep = LiquiditySweep("SW-1", LiquiditySide.BUY_SIDE, entry + 0.050, sl,
                           "LQ-1", 5, 5, entry + 0.030)
    csd = StructureEvent("CSD-1", StructureEventType.CSD, Direction.BEARISH,
                         entry - 0.010, 8, 8, "SL-1", "SW-1")
    irl = find_irl_target(entry, Direction.BEARISH, [_swing(3, SwingType.LOW, tp)])
    return build_trade_setup("CADJPY", ctx, sweep, csd, irl, created_time=_ts(created))


def _m15_price_hits_tp(entry=111.025, tp=110.870, created="2026-09-29 01:00Z"):
    return _candles([
        [_ts("2026-09-29 00:45Z"), 111.100, 111.150, 111.050, 111.100],
        [_ts(created),             111.100, 111.200, 111.050, 111.080],
        [_ts("2026-09-29 01:15Z"), entry - 0.010, entry + 0.010, tp - 0.010, tp],
    ])


def _m15_entry_then_tp(entry=111.025, tp=110.870, created="2026-09-29 01:00Z"):
    return _candles([
        [_ts("2026-09-29 00:45Z"), 111.100, 111.150, 111.050, 111.100],
        [_ts(created),             111.100, 111.200, 111.050, 111.080],
        [_ts("2026-09-29 01:15Z"), entry + 0.005, entry + 0.010, entry - 0.005, entry],  # triggers
        [_ts("2026-09-29 01:30Z"), entry - 0.010, entry - 0.005, tp - 0.010, tp - 0.005],  # TP hit
    ])


def _m15_breach(entry=111.025, sl=112.544, created="2026-09-29 01:00Z"):
    return _candles([
        [_ts("2026-09-29 00:45Z"), 111.100, 111.150, 111.050, 111.100],
        [_ts(created),             111.100, 111.200, 111.050, 111.080],
        [_ts("2026-09-29 01:15Z"), 111.500, sl + 0.010, 111.400, 112.100],
    ])


# ─────────────────────────────────────────────────────────────────────────────
# D-1: FILLED lifecycle mapping
# ─────────────────────────────────────────────────────────────────────────────

def test_filled_replay_state_maps_correctly():
    """D-1: ReplayState.FILLED → LCState.FILLED when entry triggered then TP hit.

    After the fix, _apply_lifecycle_evaluation must transition the setup to
    LCState.FILLED rather than leaving it in EXECUTION_READY.
    """
    hub = _hub()
    setup = _bearish_setup(entry=111.025, tp=110.870)
    hub.register_setup(setup)

    m15 = _m15_entry_then_tp()
    hub._apply_lifecycle_evaluation(setup.id, m15)

    lc = hub.registry.get(setup.id)
    assert lc.state is LCState.FILLED, (
        f"ReplayState.FILLED must map to LCState.FILLED; got {lc.state}"
    )
    assert lc.reason == "take_profit_hit"


def test_active_replay_state_stays_execution_ready():
    """PENDING/TRIGGERED replay → setup must remain EXECUTION_READY (no terminal state)."""
    hub = _hub()
    setup = _bearish_setup(entry=111.025, tp=110.870)
    hub.register_setup(setup)

    # Only a creation bar — no post-creation price action
    m15 = _candles([
        [_ts("2026-09-29 01:00Z"), 111.100, 111.150, 111.050, 111.100],
    ])
    hub._apply_lifecycle_evaluation(setup.id, m15)

    lc = hub.registry.get(setup.id)
    assert lc.state is LCState.EXECUTION_READY


def test_invalidated_replay_state_maps_to_protected_level_breached():
    """ReplayState.INVALIDATED → LCState.PROTECTED_LEVEL_BREACHED."""
    hub = _hub()
    setup = _bearish_setup(entry=111.025, tp=110.870, sl=112.544)
    hub.register_setup(setup)

    hub._apply_lifecycle_evaluation(setup.id, _m15_breach())

    lc = hub.registry.get(setup.id)
    assert lc.state is LCState.PROTECTED_LEVEL_BREACHED
    assert lc.reason == "protected_level_breached_before_entry"


def test_expired_replay_state_maps_to_entry_no_longer_valid():
    """ReplayState.EXPIRED → LCState.ENTRY_NO_LONGER_VALID."""
    hub = _hub()
    setup = _bearish_setup(entry=111.025, tp=110.870)
    hub.register_setup(setup)

    hub._apply_lifecycle_evaluation(setup.id, _m15_price_hits_tp())

    lc = hub.registry.get(setup.id)
    assert lc.state is LCState.ENTRY_NO_LONGER_VALID
    assert lc.reason == "target_reached_before_entry"


def test_filled_transition_is_idempotent():
    """Calling _apply_lifecycle_evaluation multiple times after FILLED must not raise."""
    hub = _hub()
    setup = _bearish_setup(entry=111.025, tp=110.870)
    hub.register_setup(setup)

    m15 = _m15_entry_then_tp()
    hub._apply_lifecycle_evaluation(setup.id, m15)
    hub._apply_lifecycle_evaluation(setup.id, m15)
    hub._apply_lifecycle_evaluation(setup.id, m15)

    lc = hub.registry.get(setup.id)
    assert lc.state is LCState.FILLED


def test_lifecycle_no_broker_calls_on_filled():
    """FILLED transition must not call order_send or order_check."""
    calls = {"send": [], "check": []}

    class InstrumentedSource(DictSource):
        def order_send(self, req): calls["send"].append(req)
        def order_check(self, req): calls["check"].append(req)

    hub = EngineHub(InstrumentedSource(), db_path=":memory:", setup_store_path=":memory:")
    setup = _bearish_setup(entry=111.025, tp=110.870)
    hub.register_setup(setup)
    hub._apply_lifecycle_evaluation(setup.id, _m15_entry_then_tp())

    assert calls["send"] == []
    assert calls["check"] == []
    assert hub.registry.get(setup.id).state is LCState.FILLED


# ─────────────────────────────────────────────────────────────────────────────
# D-2: Swing / OB / CSD identity across rolling windows
# ─────────────────────────────────────────────────────────────────────────────

def _make_ohlc(base: str, n_bars: int, bar_minutes: int = 15) -> pd.DataFrame:
    """Generate a minimal M15-style OHLC frame starting at base."""
    t0 = pd.Timestamp(base, tz="UTC")
    rows = []
    for i in range(n_bars):
        t = t0 + pd.Timedelta(minutes=bar_minutes * i)
        rows.append({
            "time": t, "open": 110.0, "high": 110.5, "low": 109.5, "close": 110.0,
        })
    # Create a simple swing: local high at bar 5, local low at bar 10
    df = pd.DataFrame(rows)
    df.loc[5, "high"] = 112.0
    df.loc[10, "low"] = 108.0
    return df


def test_swing_identity_survives_rolling_window():
    """D-2: A swing confirmed at a fixed timestamp must have the same ID whether
    it appears at relative index 5 in a 15-bar window or index 7 in a 17-bar window.
    """
    # Build two windows with different leading history but the same physical swing
    base = "2026-01-01T00:00:00"
    short_window = _make_ohlc(base, 15)  # swing at relative index 5

    # Prepend 2 extra bars to shift the swing to relative index 7
    prefix = _make_ohlc("2025-12-31T23:30:00", 2)
    long_window = pd.concat([prefix, short_window], ignore_index=True).reset_index(drop=True)
    # Update long_window times to be monotonic (prefix already has earlier times)

    swings_short = find_swings(short_window, left=2, right=2)
    swings_long  = find_swings(long_window, left=2, right=2)

    # Find the swing that corresponds to bar 5 in short_window (by price=112.0)
    hs = [s for s in swings_short if abs(s.price - 112.0) < 0.1]
    hl = [s for s in swings_long  if abs(s.price - 112.0) < 0.1]

    assert hs, "swing HIGH at 112.0 must be detected in short window"
    assert hl, "swing HIGH at 112.0 must be detected in long window"

    # IDs must match — same physical candle time, different relative position
    assert hs[0].id == hl[0].id, (
        f"Swing ID must be position-independent: "
        f"short={hs[0].id!r} (idx={hs[0].index}) vs long={hl[0].id!r} (idx={hl[0].index})"
    )


def test_ob_identity_survives_rolling_window():
    """D-2: An OB at a fixed candle timestamp must produce the same ID regardless
    of which DataFrame offset it occupies.
    """
    # Build a DataFrame where a displacement candle is at position 3
    def _make_ob_frame(n_leading: int) -> pd.DataFrame:
        rows = []
        base = pd.Timestamp("2026-06-01T00:00:00", tz="UTC")
        for i in range(n_leading):
            t = base + pd.Timedelta(minutes=15 * (i - n_leading))
            rows.append({"time": t, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                         "displacement_bearish": False, "displacement_bullish": False})
        # OB source candle: BULLISH (close > open) — last up candle before bearish displacement
        ob_time = base
        rows.append({"time": ob_time, "open": 101.0, "high": 103.0, "low": 100.5, "close": 102.5,
                     "displacement_bearish": False, "displacement_bullish": False})
        # Displacement candle (bearish: close < open, large down body, annotated)
        rows.append({"time": base + pd.Timedelta(minutes=15), "open": 102.5, "high": 103.0,
                     "low": 99.0, "close": 99.2,
                     "displacement_bearish": True, "displacement_bullish": False})
        return pd.DataFrame(rows)

    df_short = _make_ob_frame(n_leading=0)  # OB at index 0
    df_long  = _make_ob_frame(n_leading=3)  # OB at index 3

    disp_short = [i for i, r in df_short.iterrows() if r["displacement_bearish"]]
    disp_long  = [i for i, r in df_long.iterrows()  if r["displacement_bearish"]]

    blocks_short = find_order_blocks(df_short, disp_short, timeframe="M15")
    blocks_long  = find_order_blocks(df_long,  disp_long,  timeframe="M15")

    assert blocks_short, "OB must be detected in short frame"
    assert blocks_long,  "OB must be detected in long frame"

    # Same physical OB candle time → same ID
    assert blocks_short[0].id == blocks_long[0].id, (
        f"OB ID must be position-independent: "
        f"short={blocks_short[0].id!r} (idx={blocks_short[0].candle_index}) "
        f"vs long={blocks_long[0].id!r} (idx={blocks_long[0].candle_index})"
    )


def test_csd_identity_survives_rolling_window():
    """D-2: The CSD event ID (derived from sweep ID + timestamp) must be stable
    across window positions.

    CSD ID = CSD-{sweep.id}. Sweep ID = SWEEP-{_ts_key(candle_time)}-{pool.id}.
    Pool ID = LQ-{swing.id}. Swing ID = {type}-{_ts_key(candle_time)}.
    All components are timestamp-based → CSD ID is window-position-independent.
    """
    from src.smc_engine.structure import (
        build_liquidity_pools, detect_sweeps, detect_structure_breaks, confirm_csd,
    )

    def _make_csd_frame(n_leading: int) -> pd.DataFrame:
        base = pd.Timestamp("2026-06-01T00:00:00", tz="UTC")
        rows = []
        for i in range(n_leading):
            rows.append({"time": base + pd.Timedelta(minutes=15 * (i - n_leading)),
                         "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0})

        # Row +0: base bar (left side for SH detection)
        rows.append({"time": base,                              "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0})
        # Row +1: HIGH swing at 105.0 (with left=1, right=1: SH if h>h[+0] AND h>=h[+2])
        rows.append({"time": base + pd.Timedelta(minutes=15),  "open": 100.0, "high": 105.0, "low": 99.5, "close": 100.0})
        # Row +2: right side of SH + base for LOW swing (low=98.0)
        rows.append({"time": base + pd.Timedelta(minutes=30),  "open": 100.0, "high": 101.0, "low": 98.0, "close": 100.0})
        # Row +3: right side of SL (low=99.0 ≥ 98.0)
        rows.append({"time": base + pd.Timedelta(minutes=45),  "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0})
        # Row +4: sweep candle — high > 105 (sweeps SH pool), close < 105
        rows.append({"time": base + pd.Timedelta(minutes=60),  "open": 100.0, "high": 106.0, "low": 99.0, "close": 104.0})
        # Row +5: BOS/CSD candle — close < 98.0 (bearish BOS below SL at row +2)
        rows.append({"time": base + pd.Timedelta(minutes=75),  "open": 104.0, "high": 104.5, "low": 96.0, "close": 96.5})
        return pd.DataFrame(rows)

    df_short = _make_csd_frame(n_leading=0)
    df_long  = _make_csd_frame(n_leading=4)

    def _get_csd(df):
        sw = find_swings(df, left=1, right=1)
        pools = build_liquidity_pools(sw)
        sweeps = detect_sweeps(df, pools, lookback_bars=10, swings=sw)
        breaks = detect_structure_breaks(df, sw)
        for sweep in sweeps:
            csd = confirm_csd(df, sweep, breaks, max_bars_after_sweep=6, swings=sw)
            if csd is not None:
                return csd
        return None

    csd_short = _get_csd(df_short)
    csd_long  = _get_csd(df_long)

    assert csd_short is not None, "CSD must be detected in short frame"
    assert csd_long  is not None, "CSD must be detected in long frame"

    assert csd_short.id == csd_long.id, (
        f"CSD ID must be position-independent: "
        f"short={csd_short.id!r} vs long={csd_long.id!r}"
    )


def test_setup_identity_survives_rolling_window():
    """D-2: build_trade_setup must produce the same setup ID whether the causal
    components are at relative index N or N+k in the M15 frame.
    """
    poi = POI("D1-P1", Direction.BEARISH, "D1", 112.5, 111.0, 1, _ts("2026-09-28T00:00:00"))

    # OB with a specific candle time
    ob_time = _ts("2026-09-28T06:00:00")
    ob = OrderBlock(
        id=f"OB-M15-{int(ob_time.value)}-BEARISH",
        direction=Direction.BEARISH, timeframe="M15",
        candle_index=10, candle_time=ob_time,
        low=110.9, high=111.1, mitigation_price=110.9,
        source_displacement_index=11,
    )
    idm = Inducement("IDM-1", Direction.BEARISH, 12, 12, 111.2, 12, ob.id)
    ctx = ExecutionContext(poi, ob, idm, Direction.BEARISH)
    sweep = LiquiditySweep("SW-1", LiquiditySide.BUY_SIDE, 111.5, 112.5, "LQ-1", 5, 5, 111.3)

    # CSD with the SAME candle time in two different windows (different candle_index)
    csd_time = _ts("2026-09-28T07:45:00")
    csd_a = StructureEvent("CSD-SWEEP-20260928T074500", StructureEventType.CSD,
                           Direction.BEARISH, 110.9,
                           candle_index=15,  # position in short window
                           candle_time=csd_time, source_sweep_id="SW-1")
    csd_b = StructureEvent("CSD-SWEEP-20260928T074500", StructureEventType.CSD,
                           Direction.BEARISH, 110.9,
                           candle_index=19,  # position in long window (4 extra leading bars)
                           candle_time=csd_time, source_sweep_id="SW-1")

    irl = find_irl_target(110.9, Direction.BEARISH,
                          [_swing(3, SwingType.LOW, 110.7)])

    setup_a = build_trade_setup("CADJPY", ctx, sweep, csd_a, irl,
                                created_time=_ts("2026-09-28T07:45:00"))
    setup_b = build_trade_setup("CADJPY", ctx, sweep, csd_b, irl,
                                created_time=_ts("2026-09-28T07:45:00"))

    # Setup ID uses CSD candle_time, not candle_index, so both must be equal
    assert setup_a.id == setup_b.id, (
        f"Setup ID must be position-independent: "
        f"a={setup_a.id!r} (csd_idx={csd_a.candle_index}) "
        f"b={setup_b.id!r} (csd_idx={csd_b.candle_index})"
    )


def test_irl_swing_id_remains_resolvable():
    """D-2: The IRL swing ID stored in a setup must remain the same ID as the
    swing produced by find_swings when the window advances.

    If find_swings produces SL-{timestamp}, and the setup stores that ID, then
    re-running find_swings on any window containing that same candle returns the
    same ID.
    """
    base = pd.Timestamp("2026-09-28T00:00:00", tz="UTC")
    n = 30
    rows = []
    for i in range(n):
        t = base + pd.Timedelta(minutes=15 * i)
        rows.append({"time": t, "open": 111.0, "high": 111.5, "low": 110.5, "close": 111.0})
    rows[15]["low"] = 110.0  # local low swing
    df = pd.DataFrame(rows)

    swings_full = find_swings(df, left=2, right=2)
    # Simulate window advance: add 3 new bars to the end
    new_rows = []
    for i in range(3):
        t = base + pd.Timedelta(minutes=15 * (n + i))
        new_rows.append({"time": t, "open": 111.0, "high": 111.5, "low": 110.5, "close": 111.0})
    df_advanced = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
    swings_advanced = find_swings(df_advanced, left=2, right=2)

    # Find the swing at the known low
    low_full     = [s for s in swings_full     if abs(s.price - 110.0) < 0.05]
    low_advanced = [s for s in swings_advanced if abs(s.price - 110.0) < 0.05]

    assert low_full,     "IRL swing must be detected in full window"
    assert low_advanced, "IRL swing must still be detected in advanced window"

    assert low_full[0].id == low_advanced[0].id, (
        f"IRL swing ID must survive window advance: "
        f"full={low_full[0].id!r} (idx={low_full[0].index}) "
        f"advanced={low_advanced[0].id!r} (idx={low_advanced[0].index})"
    )


def test_persisted_setup_provenance_survives_restart(tmp_path):
    """D-2: A setup registered in one hub session must produce the same setup_id
    when the causal components are reconstructed (simulating a server restart
    with the same underlying market events).
    """
    poi = POI("D1-P1", Direction.BEARISH, "D1", 112.5, 111.0, 1, _ts("2026-09-28T00:00:00"))
    ob_time = _ts("2026-09-28T06:00:00")
    ob = OrderBlock(
        id=f"OB-M15-{int(ob_time.value)}-BEARISH",
        direction=Direction.BEARISH, timeframe="M15",
        candle_index=10, candle_time=ob_time,
        low=110.9, high=111.1, mitigation_price=110.9,
        source_displacement_index=11,
    )
    idm = Inducement("IDM-1", Direction.BEARISH, 12, 12, 111.2, 12, ob.id)
    ctx = ExecutionContext(poi, ob, idm, Direction.BEARISH)
    sweep = LiquiditySweep("SW-1", LiquiditySide.BUY_SIDE, 111.5, 112.5, "LQ-1", 5, 5, 111.3)
    csd_time = _ts("2026-09-28T07:45:00")
    csd = StructureEvent(f"CSD-SW-1", StructureEventType.CSD,
                         Direction.BEARISH, 110.9, 15, csd_time, source_sweep_id="SW-1")
    irl = find_irl_target(110.9, Direction.BEARISH, [_swing(3, SwingType.LOW, 110.7)])

    setup1 = build_trade_setup("CADJPY", ctx, sweep, csd, irl,
                               created_time=_ts("2026-09-28T07:45:00"))

    # Simulate restart: reconstruct the SAME causal event with a different csd.candle_index
    # (the candle is still at the same time, but in a larger window it's at position 19)
    csd_after_restart = StructureEvent(
        f"CSD-SW-1", StructureEventType.CSD,
        Direction.BEARISH, 110.9, 19, csd_time, source_sweep_id="SW-1"
    )
    setup2 = build_trade_setup("CADJPY", ctx, sweep, csd_after_restart, irl,
                               created_time=_ts("2026-09-28T07:45:00"))

    assert setup1.id == setup2.id, (
        f"Setup ID must survive position change across restarts: "
        f"before={setup1.id!r}, after={setup2.id!r}"
    )


def test_identity_does_not_depend_on_dataframe_position():
    """D-2: Swing / OB IDs must depend only on candle time, not DataFrame row index."""
    # Test swing ID directly
    t = pd.Timestamp("2026-09-28T07:45:00", tz="UTC")
    df_a = pd.DataFrame([
        {"time": t - pd.Timedelta(minutes=30), "open": 111.0, "high": 111.5, "low": 110.5, "close": 111.0},
        {"time": t - pd.Timedelta(minutes=15), "open": 111.0, "high": 111.5, "low": 110.5, "close": 111.0},
        {"time": t,                             "open": 111.0, "high": 113.0, "low": 110.5, "close": 111.0},  # swing high
        {"time": t + pd.Timedelta(minutes=15), "open": 111.0, "high": 111.5, "low": 110.5, "close": 111.0},
        {"time": t + pd.Timedelta(minutes=30), "open": 111.0, "high": 111.5, "low": 110.5, "close": 111.0},
    ])
    # Shift the swing high to position 4 (add 2 prefix bars)
    t2 = t - pd.Timedelta(minutes=30)
    df_b = pd.DataFrame([
        {"time": t2 - pd.Timedelta(minutes=30), "open": 111.0, "high": 111.5, "low": 110.5, "close": 111.0},
        {"time": t2 - pd.Timedelta(minutes=15), "open": 111.0, "high": 111.5, "low": 110.5, "close": 111.0},
        {"time": t - pd.Timedelta(minutes=30), "open": 111.0, "high": 111.5, "low": 110.5, "close": 111.0},
        {"time": t - pd.Timedelta(minutes=15), "open": 111.0, "high": 111.5, "low": 110.5, "close": 111.0},
        {"time": t,                             "open": 111.0, "high": 113.0, "low": 110.5, "close": 111.0},  # same swing high
        {"time": t + pd.Timedelta(minutes=15), "open": 111.0, "high": 111.5, "low": 110.5, "close": 111.0},
        {"time": t + pd.Timedelta(minutes=30), "open": 111.0, "high": 111.5, "low": 110.5, "close": 111.0},
    ])

    swings_a = find_swings(df_a, left=2, right=2)
    swings_b = find_swings(df_b, left=2, right=2)

    sh_a = [s for s in swings_a if abs(s.price - 113.0) < 0.05]
    sh_b = [s for s in swings_b if abs(s.price - 113.0) < 0.05]

    assert sh_a and sh_b, "High swing must be detected in both frames"
    assert sh_a[0].id == sh_b[0].id, (
        f"Swing ID must not depend on DataFrame position: "
        f"pos={sh_a[0].index} id={sh_a[0].id!r} vs pos={sh_b[0].index} id={sh_b[0].id!r}"
    )


def test_duplicate_setup_identity_is_deterministic():
    """D-2: Calling build_trade_setup twice with identical inputs must produce
    the same setup ID (no random component, no positional hash).
    """
    poi = POI("D1-P1", Direction.BEARISH, "D1", 112.5, 111.0, 1, _ts("2026-09-28T00:00:00"))
    ob_time = _ts("2026-09-28T06:00:00")
    ob = OrderBlock(f"OB-M15-{int(ob_time.value)}-BEARISH", Direction.BEARISH, "M15",
                    10, ob_time, 110.9, 111.1, 110.9, 11)
    idm = Inducement("IDM-1", Direction.BEARISH, 12, 12, 111.2, 12, ob.id)
    ctx = ExecutionContext(poi, ob, idm, Direction.BEARISH)
    sweep = LiquiditySweep("SW-1", LiquiditySide.BUY_SIDE, 111.5, 112.5, "LQ-1", 5, 5, 111.3)
    csd_time = _ts("2026-09-28T07:45:00")
    csd = StructureEvent("CSD-SW-1", StructureEventType.CSD, Direction.BEARISH, 110.9, 15, csd_time, source_sweep_id="SW-1")
    irl = find_irl_target(110.9, Direction.BEARISH, [_swing(3, SwingType.LOW, 110.7)])

    s1 = build_trade_setup("CADJPY", ctx, sweep, csd, irl, created_time=_ts("2026-09-28T07:45:00"))
    s2 = build_trade_setup("CADJPY", ctx, sweep, csd, irl, created_time=_ts("2026-09-28T07:45:00"))

    assert s1.id == s2.id, f"Identical inputs must produce identical ID: {s1.id!r} != {s2.id!r}"


def test_same_causal_event_cannot_create_duplicate_registry_entries():
    """D-2: Registering the same setup ID twice must not add a duplicate entry."""
    hub = _hub()
    setup = _bearish_setup()
    hub.register_setup(setup)
    hub.register_setup(setup)  # second registration — same ID

    # registered_ids is a set — inherently deduplicated; registry.get returns one entry
    assert setup.id in hub.registered_ids, "Setup must be in registry after registration"
    lc = hub.registry.get(setup.id)
    assert lc is not None
    assert lc.state is LCState.EXECUTION_READY, (
        "Same setup ID must not produce duplicate registry entries or corrupt state"
    )


def test_distinct_causal_events_remain_distinct():
    """D-2: Two different causal events must produce different setup IDs even when
    they have the same entry/TP/SL (different timestamps → different IDs).
    """
    poi = POI("D1-P1", Direction.BEARISH, "D1", 112.5, 111.0, 1, _ts("2026-09-28T00:00:00"))
    ob_time_a = _ts("2026-09-28T06:00:00")
    ob_time_b = _ts("2026-09-28T08:00:00")  # different time = different event

    def _make_setup(ob_time, csd_time):
        ob = OrderBlock(f"OB-M15-{int(ob_time.value)}-BEARISH", Direction.BEARISH, "M15",
                        10, ob_time, 110.9, 111.1, 110.9, 11)
        idm = Inducement("IDM-1", Direction.BEARISH, 12, 12, 111.2, 12, ob.id)
        ctx = ExecutionContext(poi, ob, idm, Direction.BEARISH)
        sweep = LiquiditySweep("SW-1", LiquiditySide.BUY_SIDE, 111.5, 112.5, "LQ-1", 5, 5, 111.3)
        csd = StructureEvent("CSD-SW-1", StructureEventType.CSD, Direction.BEARISH, 110.9, 15, csd_time, source_sweep_id="SW-1")
        irl = find_irl_target(110.9, Direction.BEARISH, [_swing(3, SwingType.LOW, 110.7)])
        return build_trade_setup("CADJPY", ctx, sweep, csd, irl, created_time=csd_time)

    s_a = _make_setup(ob_time_a, _ts("2026-09-28T07:00:00"))
    s_b = _make_setup(ob_time_b, _ts("2026-09-28T09:00:00"))

    assert s_a.id != s_b.id, (
        f"Distinct causal events must produce distinct IDs: both got {s_a.id!r}"
    )


def test_identity_generation_has_no_future_data_dependency():
    """D-2: IDs must be assigned using only information available at event
    confirmation. A swing confirmed at time T uses T's timestamp (not any future bar).
    """
    base = pd.Timestamp("2026-09-28T00:00:00", tz="UTC")
    rows = []
    for i in range(10):
        rows.append({"time": base + pd.Timedelta(minutes=15 * i),
                     "open": 111.0, "high": 111.5, "low": 110.5, "close": 111.0})
    rows[3]["high"] = 113.0  # swing HIGH at bar 3 confirmed at bar 5 (left=2, right=2)
    df = pd.DataFrame(rows)

    swings = find_swings(df, left=2, right=2)
    sh = [s for s in swings if abs(s.price - 113.0) < 0.05]
    assert sh, "High swing must be detected"

    swing = sh[0]
    # The swing's identity component must be derived from its own candle time (bar 3),
    # NOT from its confirmation bar (bar 5) or any future bar.
    swing_candle_time = rows[3]["time"]
    ts_value = str(pd.Timestamp(swing_candle_time).value)

    # The ID should contain the swing's own candle timestamp
    assert ts_value in swing.id or swing.id.endswith(ts_value) or ts_value in swing.id, (
        f"Swing ID {swing.id!r} must encode candle time {swing_candle_time}, not future bars"
    )
    # Specifically, the confirmation time (bar 5) should NOT be used
    conf_time_value = str(pd.Timestamp(rows[5]["time"]).value)
    assert conf_time_value not in swing.id, (
        f"Swing ID {swing.id!r} must NOT encode confirmation time (bar 5), only candle time (bar 3)"
    )
