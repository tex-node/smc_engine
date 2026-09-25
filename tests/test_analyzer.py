
import pandas as pd

from src.smc_engine.analyzer import MT5Analyzer
from src.smc_engine.lifecycle import SetupRegistry
from src.smc_engine.strategy import MultiTimeframeAnalyzer


class FakeMarket:
    def __init__(self):
        self.calls = []

    def closed_bars(self, timeframe, count):
        self.calls.append((timeframe, count))
        return pd.DataFrame({
            "time": range(20),
            "open": [100.0] * 20,
            "high": [101.0] * 20,
            "low": [99.0] * 20,
            "close": [100.0] * 20,
            "tick_volume": [1] * 20,
        })


def test_read_only_analyzer_fetches_closed_multi_timeframe_data():
    market = FakeMarket()
    analyzer = MT5Analyzer(
        market,
        MultiTimeframeAnalyzer("TEST"),
        SetupRegistry(),
        d1_count=20,
        h4_count=20,
        m15_count=20,
    )
    result = analyzer.analyze_once()
    assert result.candidate is None
    assert len(market.calls) == 3
    assert result.d1_bars == result.h4_bars == result.m15_bars == 20
