
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
        d1 = d1.tail(self.config.d1_lookback).reset_index(drop=True)
        h4 = h4.reset_index(drop=True)
        m15 = m15.reset_index(drop=True)

        pois = active_unmitigated_pois(
            d1,
            lookback_bars=min(self.config.d1_poi_lookback, len(d1)),
            config=DisplacementConfig(),
        )
        if not pois or len(h4) < 10 or len(m15) < 10:
            return []

        h4_swings = find_swings(h4, self.config.h4_swing_left, self.config.h4_swing_right)
        liquidity = build_liquidity_pools(h4_swings)
        sweeps = detect_sweeps(h4, liquidity, self.config.h4_sweep_lookback)
        breaks = detect_structure_breaks(h4, h4_swings)

        m15_displacement = detect_displacement(
            m15,
            DisplacementConfig(
                atr_period=self.config.m15_atr_period,
                body_atr_multiple=self.config.m15_displacement_atr,
            ),
        )
        displacement_indices = [
            i for i, row in m15_displacement.iterrows()
            if bool(row["displacement_bullish"] or row["displacement_bearish"])
        ]
        m15_blocks = find_order_blocks(
            m15_displacement,
            displacement_indices,
            timeframe="M15",
            search_back=self.config.m15_ob_search_back,
        )
        m15_swings = find_swings(m15, self.config.m15_swing_left, self.config.m15_swing_right)
        idms = find_inducements(m15, m15_blocks, m15_swings, self.config.m15_idm_window)

        candidates: list[CandidateSetup] = []
        for poi in pois:
            direction = poi.direction
            relevant_sweeps = [
                s for s in sweeps
                if (
                    direction is Direction.BULLISH
                    and s.side.value == "SELL_SIDE"
                ) or (
                    direction is Direction.BEARISH
                    and s.side.value == "BUY_SIDE"
                )
            ]
            contexts = execution_context(poi, m15_blocks, idms, direction)

            for sweep in relevant_sweeps:
                csd = confirm_csd(h4, sweep, breaks, self.config.h4_csd_window)
                if csd is None:
                    continue
                for context in contexts:
                    if context.order_block.source_displacement_index < 0:
                        continue
                    entry = context.order_block.mitigation_price
                    irl = find_irl_target(entry, direction, m15_swings)
                    if irl is None:
                        continue
                    try:
                        setup = build_trade_setup(
                            self.symbol,
                            context,
                            sweep,
                            csd,
                            irl,
                            risk_percent=self.config.risk_percent,
                        )
                    except ValueError:
                        continue
                    candidates.append(CandidateSetup(setup, sweep, csd))

        return candidates


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
