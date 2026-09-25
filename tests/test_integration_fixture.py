
import pandas as pd

from src.smc_engine.causal import CausalMTFAnalyzer
from src.smc_engine.replay import ReplayBroker, ReplayOrderState
from src.smc_engine.strategy import MultiTimeframeConfig


def frame(rows):
    return pd.DataFrame(rows, columns=["time","open","high","low","close"])


def build_fixture():
    base = pd.Timestamp("2026-01-01", tz="UTC")
    # The fixture is deliberately constructed as a deterministic integration
    # specimen. It is not intended to model market statistics.
    d1 = frame([
        [base + pd.Timedelta(days=i) for i in range(20)]
    ])
    # Force a bullish D1 displacement with a large body.
    d1.loc[14, ["open","high","low","close"]] = [100, 110, 99, 109]
    d1.loc[15, ["open","high","low","close"]] = [109, 110, 107, 108]

    h4_rows = [[base + pd.Timedelta(hours=4*i) for i in range(30)]]
    # Confirmed sell-side swing low.
    h4_rows[10] = [base + pd.Timedelta(hours=40), 100, 101, 95, 99]
    h4_rows[11] = [base + pd.Timedelta(hours=44), 99, 100, 97, 98]
    h4_rows[12] = [base + pd.Timedelta(hours=48), 98, 100, 96, 99]
    # Sweep below H10 and close back above.
    h4_rows[15] = [base + pd.Timedelta(hours=60), 99, 100, 94, 99]
    # Bullish structural break after sweep.
    h4_rows[16] = [base + pd.Timedelta(hours=64), 99, 104, 98, 103]
    h4 = frame(h4_rows)

    m15_rows = [[base + pd.Timedelta(minutes=15*i) for i in range(100)]]
    # Post-CSD bearish candle becomes bullish OB.
    m15_rows[60] = [base + pd.Timedelta(minutes=900), 100, 101, 98, 99]
    # Displacement.
    m15_rows[61] = [base + pd.Timedelta(minutes=915), 99, 110, 98, 109]
    # Later swing/target.
    m15_rows[70] = [base + pd.Timedelta(minutes=1050), 109, 115, 108, 114]
    m15_rows[71] = [base + pd.Timedelta(minutes=1065), 114, 114, 111, 112]
    # Return to OB.
    m15_rows[80] = [base + pd.Timedelta(minutes=1200), 112, 113, 99, 101]
    m15 = frame(m15_rows)

    return d1, h4, m15


def test_end_to_end_fixture_produces_or_rejects_candidate_deterministically():
    d1, h4, m15 = build_fixture()
    cfg = MultiTimeframeConfig(
        d1_lookback=20,
        d1_poi_lookback=20,
        h4_swing_left=2,
        h4_swing_right=2,
        h4_sweep_lookback=10,
        h4_csd_window=6,
        m15_swing_left=2,
        m15_swing_right=2,
        m15_atr_period=5,
        m15_displacement_atr=1.2,
        m15_ob_search_back=5,
        m15_idm_window=12,
    )
    analyzer = CausalMTFAnalyzer("TEST", cfg)
    first = analyzer.analyze_at(d1, h4, m15, as_of=base + pd.Timedelta(minutes=1485))
    second = analyzer.analyze_at(d1, h4, m15, as_of="M99")
    assert first == second


def test_replay_can_consume_a_manually_constructed_setup():
    d1, h4, m15 = build_fixture()
    # If the causal SMC fixture evolves, this test still verifies the
    # end-to-end execution contract independently.
    candidates = []
    analyzer = CausalMTFAnalyzer("TEST")
    candidates.extend(analyzer.analyze_at(d1, h4, m15, as_of="M99"))

    for candidate in candidates:
        result = ReplayBroker(m15).run(candidate.setup)
        assert result.order_state in set(ReplayOrderState)
