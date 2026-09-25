from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

try:\n    import MetaTrader5 as mt5\nexcept ImportError:  # pure-analysis/CI environments\n    class _MT5Constants:\n        TIMEFRAME_D1 = 16408\n        TIMEFRAME_H4 = 16388\n        TIMEFRAME_M15 = 15\n    mt5 = _MT5Constants()

from .causal import CausalMTFAnalyzer
from .lifecycle import SetupLifecycle, SetupRegistry
from .market import MT5MarketData
from .strategy import MultiTimeframeConfig


@dataclass(frozen=True)
class LiveAnalysis:
    symbol: str
    candidate: Optional[object]
    d1_bars: int
    h4_bars: int
    m15_bars: int


class MT5Analyzer:
    """Read-only live bridge using the causal strategy engine."""

    def __init__(
        self,
        market: MT5MarketData,
        strategy: CausalMTFAnalyzer,
        registry: Optional[SetupRegistry] = None,
        d1_count: int = 120,
        h4_count: int = 250,
        m15_count: int = 500,
    ):
        self.market = market
        self.strategy = strategy
        self.registry = registry or SetupRegistry()
        self.d1_count = d1_count
        self.h4_count = h4_count
        self.m15_count = m15_count

    def analyze_once(self) -> LiveAnalysis:
        d1 = self.market.closed_bars(mt5.TIMEFRAME_D1, self.d1_count)
        h4 = self.market.closed_bars(mt5.TIMEFRAME_H4, self.h4_count)
        m15 = self.market.closed_bars(mt5.TIMEFRAME_M15, self.m15_count)

        candidates = self.strategy.analyze_at(d1, h4, m15)
        selected = max(candidates, key=lambda x: x.setup.created_time) if candidates else None

        if selected is not None and not self.registry.has_active_for_symbol(self.strategy.symbol):
            self.registry.add(SetupLifecycle(selected.setup))

        return LiveAnalysis(
            symbol=self.strategy.symbol,
            candidate=selected,
            d1_bars=len(d1),
            h4_bars=len(h4),
            m15_bars=len(m15),
        )
