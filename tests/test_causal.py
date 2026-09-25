
import pandas as pd

from src.smc_engine.causal import CausalMTFAnalyzer, _latest_index_at_or_before
from src.smc_engine.strategy import MultiTimeframeConfig


def make(times):
    return pd.DataFrame({
        "time": pd.to_datetime(times, utc=True),
        "open": [100.0] * len(times),
        "high": [101.0] * len(times),
        "low": [99.0] * len(times),
        "close": [100.0] * len(times),
    })


def test_latest_index_never_uses_future_candle():
    x = make(["2026-01-01", "2026-01-02", "2026-01-03"])
    assert _latest_index_at_or_before(x, "2026-01-02T12:00:00Z") == 1


def test_causal_analyzer_does_not_use_future_data():
    times = pd.date_range("2026-01-01", periods=40, freq="D", tz="UTC")
    d1 = make(times)
    h4_times = pd.date_range("2026-01-01", periods=240, freq="4h", tz="UTC")
    h4 = make(h4_times)
    m15_times = pd.date_range("2026-01-01", periods=960, freq="15min", tz="UTC")
    m15 = make(m15_times)

    analyzer = CausalMTFAnalyzer("TEST", MultiTimeframeConfig())
    early = analyzer.analyze_at(d1, h4, m15, as_of=m15_times[400])
    late = analyzer.analyze_at(d1, h4, m15, as_of=m15_times[800])
    assert isinstance(early, list)
    assert isinstance(late, list)
