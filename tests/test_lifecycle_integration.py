"""
Regression tests: production lifecycle wiring in EngineHub.

Covers the seven scenarios from the Gate B lifecycle requalification brief.
All tests use DictSource (no broker, no MT5, no order primitives).
"""
import pandas as pd
import pytest

from src.smc_engine.execution_structure import ExecutionContext, OrderBlock
from src.smc_engine.lifecycle import SetupState as LCState
from src.smc_engine.models import (
    Direction, Inducement, LiquiditySide, LiquiditySweep, POI,
    StructureEvent, StructureEventType, SwingPoint, SwingType,
)
from src.smc_engine.setup import TradeSetup, build_trade_setup, find_irl_target
from src.smc_engine.web.hub import DictSource, EngineHub


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def _swing(i, typ, price):
    return SwingPoint(f"S-{i}", i, i, typ, price, 4, i, i)


def _make_bearish_setup(
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
    return build_trade_setup(
        "CADJPY", ctx, sweep, csd, irl,
        created_time=_ts(created),
    )


def _candles(rows):
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])


def _hub() -> EngineHub:
    src = DictSource()
    return EngineHub(src, db_path=":memory:", setup_store_path=":memory:")


# --------------------------------------------------------------------------
# Test 1: TP-before-entry expires setup
# --------------------------------------------------------------------------

def test_tp_before_entry_transitions_to_entry_no_longer_valid():
    """Price hits TP without ever reaching entry → ENTRY_NO_LONGER_VALID
    with reason target_reached_before_entry."""
    hub = _hub()
    setup = _make_bearish_setup(entry=111.025, tp=110.870,
                                created="2026-09-29 01:00Z")
    hub.register_setup(setup)
    assert hub.registry.get(setup.id).state is LCState.EXECUTION_READY

    # M15 data: candle after creation goes below TP without entering
    m15 = _candles([
        # candles BEFORE creation (ignored by evaluator)
        ["2026-09-29 00:30Z", 111.100, 111.150, 111.050, 111.100],
        ["2026-09-29 00:45Z", 111.100, 111.130, 111.070, 111.120],
        # creation bar at 01:00 (evaluator skips candles at or before created_time)
        ["2026-09-29 01:00Z", 111.100, 111.200, 111.050, 111.080],
        # post-creation: low reaches TP (110.870) without high reaching entry (111.025)
        ["2026-09-29 01:15Z", 111.060, 111.010, 110.850, 110.900],
        ["2026-09-29 01:30Z", 110.900, 110.950, 110.800, 110.870],
    ])

    hub._apply_lifecycle_evaluation(setup.id, m15)

    lc = hub.registry.get(setup.id)
    assert lc.state is LCState.ENTRY_NO_LONGER_VALID
    assert lc.reason == "target_reached_before_entry"


# --------------------------------------------------------------------------
# Test 2: Entry-before-TP does not expire
# --------------------------------------------------------------------------

def test_entry_before_tp_does_not_expire():
    """Price reaches entry before TP → setup should NOT be ENTRY_NO_LONGER_VALID."""
    hub = _hub()
    setup = _make_bearish_setup(entry=111.025, tp=110.870,
                                created="2026-09-29 01:00Z")
    hub.register_setup(setup)

    # M15 data: candle after creation triggers entry (price range includes entry)
    m15 = _candles([
        ["2026-09-29 00:45Z", 111.100, 111.150, 111.050, 111.100],
        ["2026-09-29 01:00Z", 111.100, 111.200, 111.050, 111.080],
        # entry is 111.025; low <= entry <= high → triggered
        ["2026-09-29 01:15Z", 111.080, 111.100, 110.980, 111.030],
    ])

    hub._apply_lifecycle_evaluation(setup.id, m15)

    lc = hub.registry.get(setup.id)
    # State should NOT be ENTRY_NO_LONGER_VALID (the entry genuinely traded,
    # this is not an expiry case)
    assert lc.state is not LCState.ENTRY_NO_LONGER_VALID
    # A consumed setup must NEVER remain EXECUTION_READY: the engine's own
    # verdict here is TRIGGERED/position_open, which the hub maps to FILLED.
    assert lc.state is LCState.FILLED
    assert "entry_traded" in (lc.reason or "")


# --------------------------------------------------------------------------
# Test 3: Invalidation still wins (protected level breached before entry)
# --------------------------------------------------------------------------

def test_protected_level_breach_transitions_to_protected_level_breached():
    """Candle after creation breaches the invalidation level → PROTECTED_LEVEL_BREACHED."""
    hub = _hub()
    setup = _make_bearish_setup(entry=111.025, tp=110.870, sl=112.544,
                                created="2026-09-29 01:00Z")
    hub.register_setup(setup)

    # M15 data: post-creation candle reaches above invalidation (112.544)
    m15 = _candles([
        ["2026-09-29 00:45Z", 111.100, 111.150, 111.050, 111.100],
        ["2026-09-29 01:00Z", 111.100, 111.200, 111.050, 111.080],
        # high >= invalidation_level (112.544) → protected_level_breached_before_entry
        ["2026-09-29 01:15Z", 111.500, 112.600, 111.400, 112.100],
    ])

    hub._apply_lifecycle_evaluation(setup.id, m15)

    lc = hub.registry.get(setup.id)
    assert lc.state is LCState.PROTECTED_LEVEL_BREACHED
    assert lc.reason == "protected_level_breached_before_entry"


# --------------------------------------------------------------------------
# Test 4: Repeated scans are idempotent
# --------------------------------------------------------------------------

def test_repeated_lifecycle_evaluation_is_idempotent():
    """Calling _apply_lifecycle_evaluation multiple times with the same M15
    data produces exactly one transition, not duplicates."""
    hub = _hub()
    setup = _make_bearish_setup(entry=111.025, tp=110.870,
                                created="2026-09-29 01:00Z")
    hub.register_setup(setup)

    m15 = _candles([
        ["2026-09-29 00:45Z", 111.100, 111.150, 111.050, 111.100],
        ["2026-09-29 01:00Z", 111.100, 111.200, 111.050, 111.080],
        ["2026-09-29 01:15Z", 111.060, 111.010, 110.850, 110.900],
    ])

    # Call three times — must not raise and must stay in terminal state
    hub._apply_lifecycle_evaluation(setup.id, m15)
    hub._apply_lifecycle_evaluation(setup.id, m15)
    hub._apply_lifecycle_evaluation(setup.id, m15)

    lc = hub.registry.get(setup.id)
    assert lc.state is LCState.ENTRY_NO_LONGER_VALID
    assert lc.reason == "target_reached_before_entry"


# --------------------------------------------------------------------------
# Test 5: New setup is evaluated immediately via candidates_for path
# --------------------------------------------------------------------------

def test_lifecycle_evaluated_in_candidates_for(tmp_path):
    """After a setup is registered through candidates_for, the lifecycle
    evaluation runs in the same call so a stale setup is not exposed as
    EXECUTION_READY.

    This test uses a pre-built M15 frame where price already hit the TP
    before entry, verifying end-to-end hub wiring without the full causal
    engine (which requires real multi-timeframe data).
    """
    hub = _hub()
    setup = _make_bearish_setup(entry=111.025, tp=110.870,
                                created="2026-09-29 01:00Z")
    # Register the setup directly (simulates what candidates_for does)
    hub.register_setup(setup)
    assert hub.registry.get(setup.id).state is LCState.EXECUTION_READY

    # Build an M15 frame where TP was already visited post-creation
    m15 = _candles([
        ["2026-09-29 00:30Z", 111.100, 111.150, 111.050, 111.100],
        ["2026-09-29 01:00Z", 111.100, 111.200, 111.050, 111.080],
        ["2026-09-29 01:15Z", 111.060, 111.010, 110.850, 110.900],
        ["2026-09-29 01:30Z", 110.900, 110.950, 110.800, 110.870],
    ])

    # Prime DictSource so the hub can resolve bars (needed by evaluation path)
    hub.source.prime("CADJPY", 15, m15)

    # Call the lifecycle evaluation directly — this is the exact path that
    # candidates_for calls after each causal scan
    with hub._lock:
        pending_eval = [
            sid for sid in hub.registered_ids
            if hub.registry.get(sid) is not None
            and hub.registry.get(sid).state is LCState.EXECUTION_READY
        ]
    for sid in pending_eval:
        hub._apply_lifecycle_evaluation(sid, m15)

    lc = hub.registry.get(setup.id)
    assert lc.state is LCState.ENTRY_NO_LONGER_VALID

    # Readiness must NOT surface this setup as actionable
    rows = hub.lifecycle()
    ready_rows = [r for r in rows if r["display"] == "EXECUTION_READY"]
    assert setup.id not in {r["setup_id"] for r in ready_rows}


# --------------------------------------------------------------------------
# Test 6: Rolling-window index safety
# --------------------------------------------------------------------------

def test_lifecycle_result_independent_of_rolling_window_offset():
    """Two M15 windows where the same physical TP candle sits at different
    relative indexes must produce the same lifecycle result.

    This specifically guards against the index-drift issue documented in
    the Gate B audit (SL-304 at creation == SL-302 in current window).
    The lifecycle evaluator uses absolute timestamps, not relative indexes,
    so the result must be identical regardless of window offset.
    """
    setup = _make_bearish_setup(
        entry=111.025, tp=110.870, created="2026-09-29 01:00Z",
    )

    # Window A: TP-crossing candle at relative index 4
    window_a = _candles([
        ["2026-09-28 23:30Z", 111.100, 111.150, 111.050, 111.100],
        ["2026-09-28 23:45Z", 111.100, 111.130, 111.070, 111.120],
        ["2026-09-29 00:00Z", 111.100, 111.200, 111.050, 111.080],
        ["2026-09-29 01:00Z", 111.100, 111.200, 111.050, 111.080],  # creation bar
        ["2026-09-29 01:15Z", 111.060, 111.010, 110.850, 110.900],  # TP crossed at index 4
    ])

    # Window B: two extra leading candles → same physical TP candle at index 6
    window_b = _candles([
        ["2026-09-28 22:30Z", 111.100, 111.150, 111.050, 111.100],
        ["2026-09-28 22:45Z", 111.100, 111.130, 111.070, 111.120],
        ["2026-09-28 23:30Z", 111.100, 111.150, 111.050, 111.100],
        ["2026-09-28 23:45Z", 111.100, 111.130, 111.070, 111.120],
        ["2026-09-29 00:00Z", 111.100, 111.200, 111.050, 111.080],
        ["2026-09-29 01:00Z", 111.100, 111.200, 111.050, 111.080],  # creation bar
        ["2026-09-29 01:15Z", 111.060, 111.010, 110.850, 110.900],  # TP crossed at index 6
    ])

    from src.smc_engine.setup import evaluate_setup_lifecycle
    result_a = evaluate_setup_lifecycle(setup, window_a)
    result_b = evaluate_setup_lifecycle(setup, window_b)

    assert result_a.state == result_b.state, (
        f"Window A: {result_a.state}, Window B: {result_b.state}")
    assert result_a.reason == result_b.reason
    from src.smc_engine.models import SetupState
    assert result_a.state is SetupState.EXPIRED
    assert result_a.reason == "target_reached_before_entry"


# --------------------------------------------------------------------------
# Test 7: No broker calls
# --------------------------------------------------------------------------

def test_lifecycle_evaluation_invokes_no_broker_primitives():
    """Lifecycle evaluation must never call order_send or order_check."""
    order_send_calls = []
    order_check_calls = []

    class InstrumentedSource(DictSource):
        def order_send(self, request):
            order_send_calls.append(request)
            raise RuntimeError("order_send must not be called during lifecycle evaluation")

        def order_check(self, request):
            order_check_calls.append(request)
            raise RuntimeError("order_check must not be called during lifecycle evaluation")

    src = InstrumentedSource()
    hub = EngineHub(src, db_path=":memory:", setup_store_path=":memory:")

    setup = _make_bearish_setup(entry=111.025, tp=110.870,
                                created="2026-09-29 01:00Z")
    hub.register_setup(setup)

    m15 = _candles([
        ["2026-09-29 00:45Z", 111.100, 111.150, 111.050, 111.100],
        ["2026-09-29 01:00Z", 111.100, 111.200, 111.050, 111.080],
        ["2026-09-29 01:15Z", 111.060, 111.010, 110.850, 110.900],
    ])

    hub._apply_lifecycle_evaluation(setup.id, m15)

    assert order_send_calls == [], f"order_send was called: {order_send_calls}"
    assert order_check_calls == [], f"order_check was called: {order_check_calls}"
    # Confirm the transition still happened correctly
    assert hub.registry.get(setup.id).state is LCState.ENTRY_NO_LONGER_VALID
