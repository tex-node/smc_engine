from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .execution_structure import execution_context, find_inducements, find_order_blocks
from .models import Direction, LiquiditySide
from .poi import DisplacementConfig, active_unmitigated_pois, detect_displacement
from .setup import build_trade_setup, find_irl_target
from .structure import build_liquidity_pools, confirm_csd, detect_structure_breaks, detect_sweeps, find_swings
from .strategy import CandidateSetup, MultiTimeframeConfig


def _latest_index_at_or_before(df: pd.DataFrame, timestamp) -> int:
    times = pd.to_datetime(df['time'], utc=True)
    target = pd.Timestamp(timestamp)
    target = target.tz_localize('UTC') if target.tzinfo is None else target.tz_convert('UTC')
    eligible = times <= target
    if not eligible.any():
        return -1
    return int(eligible[eligible].index[-1])


@dataclass(frozen=True)
class CausalCandidate:
    setup: object
    setup_time: object


class CausalMTFAnalyzer:
    """Causally aligned D1/H4/M15 analyzer."""

    def __init__(self, symbol: str, config: MultiTimeframeConfig = MultiTimeframeConfig()):
        self.symbol = symbol
        self.config = config

    def analyze_at(self, d1: pd.DataFrame, h4: pd.DataFrame, m15: pd.DataFrame, as_of=None) -> list[CausalCandidate]:
        d1 = d1.sort_values('time').reset_index(drop=True)
        h4 = h4.sort_values('time').reset_index(drop=True)
        m15 = m15.sort_values('time').reset_index(drop=True)
        if as_of is None:
            as_of = m15.iloc[-1]['time']

        d1_end = _latest_index_at_or_before(d1, as_of)
        h4_end = _latest_index_at_or_before(h4, as_of)
        m15_end = _latest_index_at_or_before(m15, as_of)
        if min(d1_end, h4_end, m15_end) < 0:
            return []

        d1_view = d1.iloc[:d1_end + 1].tail(self.config.d1_lookback).reset_index(drop=True)
        h4_view = h4.iloc[:h4_end + 1].reset_index(drop=True)
        m15_view = m15.iloc[:m15_end + 1].reset_index(drop=True)

        h4_swings = find_swings(h4_view, self.config.h4_swing_left, self.config.h4_swing_right)
        swing_by_id = {s.id: s for s in h4_swings}
        liquidity = build_liquidity_pools(h4_swings)
        sweeps = detect_sweeps(h4_view, liquidity, self.config.h4_sweep_lookback)
        # A sweep cannot use a swing before that swing's right-side confirmation candle.
        sweeps = [
            s for s in sweeps
            if swing_by_id.get(next((p.source_swing_id for p in liquidity if p.id == s.source_liquidity_id), ''), None) is not None
            and swing_by_id[next(p.source_swing_id for p in liquidity if p.id == s.source_liquidity_id)].confirmation_index <= s.candle_index
        ]
        breaks = detect_structure_breaks(h4_view, h4_swings)

        m15_work = detect_displacement(
            m15_view,
            DisplacementConfig(atr_period=self.config.m15_atr_period, body_atr_multiple=self.config.m15_displacement_atr),
        )
        m15_displacement_indices = [
            i for i, row in m15_work.iterrows()
            if bool(row['displacement_bullish'] or row['displacement_bearish'])
        ]
        m15_swings = find_swings(m15_view, self.config.m15_swing_left, self.config.m15_swing_right)

        candidates: list[CausalCandidate] = []
        for sweep in sweeps:
            d1_sweep_end = _latest_index_at_or_before(d1_view, sweep.candle_time)
            if d1_sweep_end < 0:
                continue
            d1_at_sweep = d1_view.iloc[:d1_sweep_end + 1].reset_index(drop=True)
            pois = active_unmitigated_pois(
                d1_at_sweep,
                lookback_bars=min(self.config.d1_poi_lookback, len(d1_at_sweep)),
                config=DisplacementConfig(),
            )
            if not pois:
                continue

            csd = None
            for poi in pois:
                desired_side = LiquiditySide.SELL_SIDE if poi.direction is Direction.BULLISH else LiquiditySide.BUY_SIDE
                if sweep.side is not desired_side:
                    continue
                if not (poi.created_time <= sweep.candle_time):
                    continue
                pre_sweep_breaks = [
                    b for b in breaks
                    if b.source_swing_id in swing_by_id
                    and swing_by_id[b.source_swing_id].index < sweep.candle_index
                    and swing_by_id[b.source_swing_id].confirmation_index <= sweep.candle_index
                ]
                csd = confirm_csd(h4_view, sweep, pre_sweep_breaks, self.config.h4_csd_window)
                if csd is None:
                    continue

                m15_end_for_csd = _latest_index_at_or_before(m15_view, csd.candle_time)
                if m15_end_for_csd < 0:
                    continue
                m15_after_indices = [i for i in m15_displacement_indices if i > m15_end_for_csd]
                blocks = find_order_blocks(m15_work, m15_after_indices, 'M15', self.config.m15_ob_search_back)
                blocks = [b for b in blocks if b.candle_index > m15_end_for_csd and b.source_displacement_index > m15_end_for_csd]
                if not blocks:
                    continue

                swings_after = [
                    s for s in m15_swings
                    if s.index > m15_end_for_csd and s.confirmation_index <= m15_end
                ]
                if not swings_after:
                    continue

                for block in blocks:
                    idms = find_inducements(
                        m15_view, [block], swings_after, self.config.m15_idm_window, as_of=as_of
                    )
                    # An order block must already exist when its IDM is confirmed.
                    idms = [
                        x for x in idms
                        if block.source_displacement_index >= 0
                        and pd.Timestamp(m15_view.iloc[block.source_displacement_index]["time"]) <= pd.Timestamp(x.confirmation_time)
                    ]
                    contexts = execution_context(poi, [block], idms, poi.direction)
                    for context in contexts:
                        event_time = context.inducement.confirmation_time or context.inducement.candle_time
                        irl = find_irl_target(
                            context.order_block.mitigation_price, context.direction, swings_after,
                            as_of=event_time,
                        )
                        if irl is None:
                            continue
                        try:
                            setup = build_trade_setup(
                                self.symbol, context, sweep, csd, irl,
                                risk_percent=self.config.risk_percent, created_time=event_time,
                            )
                        except ValueError:
                            continue
                        candidates.append(CausalCandidate(setup, event_time))

        return candidates