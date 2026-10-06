
import pandas as pd
import pytest

from src.smc_engine.models import Direction
from src.smc_engine.replay import (
    ReplayBroker, ReplayOrderState, ReplayRecord, run_replay,
    distribution_report, summarize,
)
from src.smc_engine.setup import TradeSetup
from src.smc_engine.strategy import MultiTimeframeConfig


def setup(direction=Direction.BULLISH):
    return TradeSetup(
        id="R1", symbol="TEST", direction=direction, created_time=1,
        poi_id="P", sweep_id="SW", csd_id="CSD", protected_level=99,
        order_block_id="OB", inducement_id="IDM", entry=100,
        stop_loss=99, take_profit=102, irl_swing_id="IRL",
        invalidation_level=99, risk_percent=1,
    )


def candles(rows):
    return pd.DataFrame(rows, columns=["time","open","high","low","close"])


def test_bullish_pending_limit_fills_then_hits_target():
    x = candles([
        [1, 105, 106, 103, 104],
        [2, 104, 104, 100, 101],
        [3, 101, 103, 100, 102],
    ])
    result = ReplayBroker(x).run(setup())
    assert result.order_state is ReplayOrderState.TARGETED
    assert result.entry == 100
    assert result.exit == 102
    assert result.pnl_price == 2


def test_pending_order_invalidates_before_fill():
    x = candles([
        [1, 105, 106, 103, 104],
        [2, 104, 100, 98, 99],
        [3, 99, 102, 99, 101],
    ])
    result = ReplayBroker(x).run(setup())
    assert result.order_state is ReplayOrderState.INVALIDATED
    assert result.entry is None


def test_same_candle_invalidation_wins_before_fill():
    x = candles([[1, 101, 103, 98, 101]])
    result = ReplayBroker(x).run(setup())
    assert result.order_state is ReplayOrderState.INVALIDATED


def test_summary():
    x = candles([[1, 105, 106, 103, 104], [2, 104, 104, 100, 101], [3, 101, 103, 100, 102]])
    results = [ReplayBroker(x).run(setup())]
    metrics = summarize(results)
    assert metrics.setups == 1
    assert metrics.targets == 1


# ---------------------------------------------------------------------------
# run_replay / ReplayRecord / distribution_report
# ---------------------------------------------------------------------------

def test_run_replay_returns_list():
    """run_replay always returns a list (possibly empty)."""
    from tests.test_integration_fixture import build_fixture
    d1, h4, m15 = build_fixture()
    records = run_replay("TEST", d1, h4, m15)
    assert isinstance(records, list)


def test_run_replay_records_have_correct_fields():
    """Every ReplayRecord has the required provenance fields."""
    from tests.test_integration_fixture import build_fixture
    d1, h4, m15 = build_fixture()
    records = run_replay("TEST", d1, h4, m15)
    for r in records:
        assert isinstance(r, ReplayRecord)
        assert r.symbol == "TEST"
        assert r.direction in ("BEARISH", "BULLISH")
        assert r.risk_reward >= 0
        assert r.stop_distance_pips > 0
        assert r.target_distance_pips > 0
        assert r.irl_strength >= 0
        assert r.irl_dist_atrs >= 0
        assert r.m15_atr > 0


def test_run_replay_uses_min_rr_zero_regardless_of_config():
    """run_replay captures setups even when config has min_rr=99 (gate must be ignored)."""
    from tests.test_integration_fixture import build_fixture
    d1, h4, m15 = build_fixture()
    records_gate_off = run_replay("TEST", d1, h4, m15, MultiTimeframeConfig(min_rr=0.0))
    records_gate_on  = run_replay("TEST", d1, h4, m15, MultiTimeframeConfig(min_rr=99.0))
    assert len(records_gate_off) == len(records_gate_on), (
        "run_replay must ignore min_rr — gate-free and gate-on results must match"
    )


def test_distribution_report_returns_string():
    """distribution_report always returns a non-empty string."""
    from tests.test_integration_fixture import build_fixture
    d1, h4, m15 = build_fixture()
    records = run_replay("TEST", d1, h4, m15)
    report = distribution_report(records)
    assert isinstance(report, str) and len(report) > 0


def test_distribution_report_covers_all_buckets():
    """distribution_report output contains all five R:R bucket labels."""
    from tests.test_integration_fixture import build_fixture
    d1, h4, m15 = build_fixture()
    report = distribution_report(run_replay("TEST", d1, h4, m15))
    for label in ("<0.5", "0.5-1.0", "1.0-1.5", "1.5-2.0", ">=2.0"):
        assert label in report, f"distribution_report missing bucket {label!r}"


def test_replay_record_rr_consistent_with_geometry():
    """ReplayRecord.risk_reward = target_distance / stop_distance (within rounding)."""
    from tests.test_integration_fixture import build_fixture
    d1, h4, m15 = build_fixture()
    for r in run_replay("TEST", d1, h4, m15):
        if r.stop_distance_pips > 0:
            expected = r.target_distance_pips / r.stop_distance_pips
            assert abs(r.risk_reward - expected) < 0.002, (
                f"R:R {r.risk_reward} inconsistent with geometry "
                f"({r.target_distance_pips}/{r.stop_distance_pips}={expected:.3f})"
            )
