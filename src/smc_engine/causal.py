
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .execution_structure import execution_context, find_inducements, find_order_blocks
from .models import Direction, LiquiditySide
from .poi import DisplacementConfig, active_unmitigated_pois, detect_displacement
from .setup import build_trade_setup, find_irl_target
from .structure import build_liquidity_pools, confirm_csd, detect_structure_breaks, detect_sweeps, find_swings
from .strategy import CandidateSetup, MultiTimeframeConfig


def _time(df: pd.DataFrame, index: int):
    return df.iloc[index]["time"]


def _latest_index_at_or_before(df: pd.DataFrame, timestamp) -> int:
    times = pd.to_datetime(df["time"], utc=True)
    target = pd.Timestamp(timestamp)
    if target.tzinfo is None:
        target = target.tz_localize("UTC")
    else:
        target = target.tz_convert("UTC")
    eligible = times <= target
    if not eligible.any():
        return -1
    return int(eligible[eligible].index[-1])


@dataclass(frozen=True)
class CausalCandidate:
    setup: object
    setup_time: object


class CausalMTFAnalyzer:
    """Causally aligned D1/H4/M15 analyzer.

    Every lower-timeframe decision is made only from candles whose close time
    is <= the setup candle timestamp. No future M15/H4 information is used.
    """

    def __init__(self, symbol: str, config: MultiTimeframeConfig = MultiTimeframeConfig()):
        self.symbol = symbol
        self.config = config

    def analyze_at(self, d1: pd.DataFrame, h4: pd.DataFrame, m15: pd.DataFrame, as_of=None) -> list[CausalCandidate]:
        d1 = d1.sort_values("time").reset_index(drop=True)
        h4 = h4.sort_values("time").reset_index(drop=True)
        m15 = m15.sort_values("time").reset_index(drop=True)

        if as_of is None:
            as_of = m15.iloc[-1]["time"]

        d1_end = _latest_index_at_or_before(d1, as_of)
        h4_end = _latest_index_at_or_before(h4, as_of)
        m15_end = _latest_index_at_or_before(m15, as_of)
        if min(d1_end, h4_end, m15_end) < 0:
            return []

        d1_view = d1.iloc[:d1_end + 1].tail(self.config.d1_lookback).reset_index(drop=True)
        h4_view = h4.iloc[:h4_end + 1].reset_index(drop=True)
        m15_view = m15.iloc[:m15_end + 1].reset_index(drop=True)

        pois = active_unmitigated_pois(
            d1_view,
            lookback_bars=min(self.config.d1_poi_lookback, len(d1_view)),
            config=DisplacementConfig(),
        )
        if not pois:
            return []

        h4_swings = find_swings(h4_view, self.config.h4_swing_left, self.config.h4_swing_right)
        liquidity = build_liquidity_pools(h4_swings)
        sweeps = detect_sweeps(h4_view, liquidity, self.config.h4_sweep_lookback)
        breaks = detect_structure_breaks(h4_view, h4_swings)

        m15_work = detect_displacement(
            m15_view,
            DisplacementConfig(
                atr_period=self.config.m15_atr_period,
                body_atr_multiple=self.config.m15_displacement_atr,
            ),
        )
        displacement_indices = [
            i for i, row in m15_work.iterrows()
            if bool(row["displacement_bullish"] or row["displacement_bearish"])
        ]
        blocks = find_order_blocks(m15_work, displacement_indices, "M15", self.config.m15_ob_search_back)
        swings15 = find_swings(m15_view, self.config.m15_swing_left, self.config.m15_swing_right)


        candidates = []
        for poi in pois:
            for sweep in sweeps:
                desired_side = LiquiditySide.SELL_SIDE if poi.direction is Direction.BULLISH else LiquiditySide.BUY_SIDE
                if sweep.side is not desired_side:
                    continue
                csd = confirm_csd(h4_view, sweep, breaks, self.config.h4_csd_window)
                if csd is None:
                    continue

                setup_time = csd.candle_time
                m15_end_for_csd = _latest_index_at_or_before(m15_view, setup_time)
                if m15_end_for_csd < 0:
                    continue

                # Execution structures must form after CSD and only use data
                # available after that event.
                m15_after = m15_view.iloc[m15_end_for_csd + 1:].reset_index(drop=True)
                if len(m15_after) < self.config.m15_swing_left + self.config.m15_swing_right + 2:
                    continue

                m15_after_work = detect_displacement(
                    m15_after,
                    DisplacementConfig(
                        atr_period=self.config.m15_atr_period,
                        body_atr_multiple=self.config.m15_displacement_atr,
                    ),
                )
                disp_after = [
                    i for i, row in m15_after_work.iterrows()
                    if bool(row["displacement_bullish"] or row["displacement_bearish"])
                ]
                blocks_after = find_order_blocks(
                    m15_after_work, disp_after, "M15", self.config.m15_ob_search_back
                )
                swings_after = find_swings(
                    m15_after, self.config.m15_swing_left, self.config.m15_swing_right
                )
                idms_after = find_inducements(
                    m15_after, blocks_after, swings_after, self.config.m15_idm_window
                )
                contexts = execution_context(poi, blocks_after, idms_after, poi.direction)

                for context in contexts:
                    # A setup can only be created after the OB/IDM exists.
                    event_time = context.order_block.candle_time
                    irl = find_irl_target(
                        context.order_block.mitigation_price,
                        context.direction,
                        swings_after,
                    )
                    if irl is None:
                        continue
                    if pd.Timestamp(irl.candle_time) > pd.Timestamp(event_time):
                        continue
                    try:
                        setup = build_trade_setup(
                            self.symbol, context, sweep, csd, irl,
                            risk_percent=self.config.risk_percent,
                            created_time=event_time,
                        )
                    except ValueError:
                        continue
                    candidates.append(CausalCandidate(setup, event_time))

        return candidates
