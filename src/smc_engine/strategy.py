
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .execution_structure import execution_context, find_inducements, find_order_blocks
from .models import Direction, LiquiditySweep, StructureEvent
from .poi import DisplacementConfig, active_unmitigated_pois, detect_displacement
from .setup import IRLTarget, TradeSetup, build_trade_setup, find_irl_target
from .structure import build_liquidity_pools, confirm_csd, detect_structure_breaks, detect_sweeps, find_swings


@dataclass(frozen=True)
class MultiTimeframeConfig:
    d1_lookback: int = 120
    d1_poi_lookback: int = 90
    h4_swing_left: int = 3
    h4_swing_right: int = 3
    h4_sweep_lookback: int = 20
    h4_csd_window: int = 6
    m15_swing_left: int = 2
    m15_swing_right: int = 2
    m15_displacement_atr: float = 1.5
    m15_atr_period: int = 14
    m15_ob_search_back: int = 5
    m15_idm_window: int = 8
    risk_percent: float = 1.0


@dataclass(frozen=True)
class CandidateSetup:
    setup: TradeSetup
    sweep: LiquiditySweep
    csd: StructureEvent


class MultiTimeframeAnalyzer:
    """Pure, MT5-independent D1/H4/M15 strategy orchestrator.

    DataFrames must contain completed candles sorted oldest -> newest.
    """

    def __init__(self, symbol: str, config: MultiTimeframeConfig = MultiTimeframeConfig()):
        self.symbol = symbol
        self.config = config

    def analyze(
        self,
        d1: pd.DataFrame,
        h4: pd.DataFrame,
        m15: pd.DataFrame,
    ) -> list[CandidateSetup]:
        """Compatibility wrapper for the authoritative causal analyzer.

        The former implementation performed an independent orchestration path and
        could combine future H4/M15 information with an earlier setup. Keep this
        public API for compatibility, but route analysis through the causal engine.
        """
        # Lazy import avoids the causal.py -> strategy.py type dependency.
        from .causal import CausalMTFAnalyzer

        analyzer = CausalMTFAnalyzer(self.symbol, self.config)
        return [
            CandidateSetup(setup=candidate.setup, sweep=candidate.sweep, csd=candidate.csd)
            for candidate in analyzer.analyze_at(d1, h4, m15)
        ]


def latest_candidate(
    analyzer: MultiTimeframeAnalyzer,
    d1: pd.DataFrame,
    h4: pd.DataFrame,
    m15: pd.DataFrame,
) -> Optional[CandidateSetup]:
    candidates = analyzer.analyze(d1, h4, m15)
    if not candidates:
        return None
    return max(candidates, key=lambda x: x.setup.created_time)
