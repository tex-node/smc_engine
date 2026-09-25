
import pandas as pd

from src.smc_engine.strategy import MultiTimeframeAnalyzer, MultiTimeframeConfig


def make(rows):
    return pd.DataFrame(rows, columns=["time","open","high","low","close"])


def test_orchestrator_returns_empty_without_required_layers():
    analyzer = MultiTimeframeAnalyzer("TEST")
    empty = make([[1,1,2,0,1]] * 5)
    assert analyzer.analyze(empty, empty, empty) == []


def test_orchestrator_is_pure_and_deterministic():
    analyzer = MultiTimeframeAnalyzer(
        "TEST",
        MultiTimeframeConfig(
            d1_lookback=30,
            d1_poi_lookback=20,
            h4_swing_left=2,
            h4_swing_right=2,
            m15_swing_left=2,
            m15_swing_right=2,
        ),
    )
    d1 = make([[i,100,101,99,100] for i in range(30)])
    h4 = make([[i,100,101,99,100] for i in range(30)])
    m15 = make([[i,100,101,99,100] for i in range(30)])
    assert analyzer.analyze(d1, h4, m15) == analyzer.analyze(d1, h4, m15)
