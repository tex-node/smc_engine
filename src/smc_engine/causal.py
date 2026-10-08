from __future__ import annotations

from dataclasses import dataclass, field
import hashlib

import pandas as pd

from .execution_structure import execution_context, find_inducements, find_order_blocks
from .models import Direction, LiquiditySide
from .poi import DisplacementConfig, active_unmitigated_pois, detect_displacement
from .setup import build_trade_setup, find_irl_target
from .structure import build_liquidity_pools, confirm_csd, detect_structure_breaks, detect_sweeps, find_swings
from .strategy import CandidateSetup, MultiTimeframeConfig


def _compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """ATR over the last ``period`` bars using True Range."""
    if len(df) < 2:
        return 0.0
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    closes = df['close'].to_numpy()
    trs = []
    for i in range(1, len(df)):
        tr = max(
            float(highs[i]) - float(lows[i]),
            abs(float(highs[i]) - float(closes[i - 1])),
            abs(float(lows[i]) - float(closes[i - 1])),
        )
        trs.append(tr)
    window = trs[-period:]
    return sum(window) / len(window) if window else 0.0


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
    sweep: object
    csd: object


_CHAIN_STAGES = ("D1_POI", "H4_SWEEP", "H4_CSD", "M15_OB", "M15_MITIGATION", "M15_IDM", "IRL", "READY")


@dataclass
class FunnelEvent:
    """One pre-arm rejection or advancement emitted during trace_chain().

    Separates wall-clock observation time (scan_timestamp) from the market
    event anchor (market_event_timestamp). Only minimal scalar data is stored —
    no OHLC frames or raw bar sequences.
    """
    symbol: str
    scan_timestamp: str          # wall-clock ISO UTC at scan time
    market_event_timestamp: str  # candle_time of the causal anchor (sweep/BOS)
    stage: str                   # FunnelStage value
    reason: str                  # FunnelReason value
    direction: str               # BULLISH / BEARISH / UNKNOWN
    causal_anchor_ref: str = ""  # sweep.id, bos.id etc.
    evidence_timestamp: str = "" # time of supporting evidence (CSD, OB, IDM)
    config_fingerprint: str = "" # short stable hash of key config parameters


@dataclass
class CausalProvenance:
    """Per-symbol causal chain trace.  Reports deepest rejection stage reached.

    Counts are populated by trace_chain().  new_admissible_count is set externally
    by the hub/API layer which has registry access; all other counts come from the
    causal engine.

    Semantic hierarchy:
      structurally_qualified  — chain reached READY (any R:R, min_rr ignored)
      rr_filtered             — structurally qualified but R:R < min_rr
      execution_ready         — structurally qualified AND not RR-filtered
      new_admissible          — execution_ready AND not already in lifecycle registry
    """
    symbol: str
    as_of: object
    d1_poi_count: int = 0
    matching_sweep_count: int = 0
    csd_count: int = 0
    post_csd_ob_count: int = 0
    unmitigated_ob_count: int = 0
    idm_count: int = 0
    structurally_qualified_count: int = 0
    rr_filtered_count: int = 0
    new_admissible_count: int = 0
    execution_ready_count: int = 0
    qualified_candidate_count: int = 0   # alias kept for backward compatibility
    rejection_stage: str = "D1_POI"
    rejection_reason: str = "no active D1 POI at any sweep time"
    rr_gate: str = ""
    funnel_events: list = field(default_factory=list)  # list[FunnelEvent]


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
        sweeps = detect_sweeps(h4_view, liquidity, self.config.h4_sweep_lookback, swings=h4_swings)
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
        m15_atr = _compute_atr(m15_view, self.config.m15_atr_period)

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
                csd = confirm_csd(h4_view, sweep, pre_sweep_breaks, self.config.h4_csd_window, swings=h4_swings)
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
                        _stop_dist = abs(sweep.sweep_extreme - context.order_block.mitigation_price)
                        irl = find_irl_target(
                            context.order_block.mitigation_price,
                            context.direction,
                            m15_swings,
                            ohlc=m15_view,
                            as_of=event_time,
                            min_distance=m15_atr,
                            ob_candle_index=block.candle_index,
                            ob_proximity_bars=self.config.m15_irl_ob_proximity,
                            stop_distance=_stop_dist,
                            min_rr=self.config.min_rr,
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
                        candidates.append(CausalCandidate(setup, event_time, sweep, csd))

        return candidates

    def trace_chain(self, d1: pd.DataFrame, h4: pd.DataFrame, m15: pd.DataFrame, as_of=None) -> CausalProvenance:
        """Same traversal as analyze_at() but accumulates per-stage counts.

        Reports the deepest rejection stage reached across all sweep iterations.
        Does NOT alter candidate selection — purely observational.
        """
        d1 = d1.sort_values('time').reset_index(drop=True)
        h4 = h4.sort_values('time').reset_index(drop=True)
        m15 = m15.sort_values('time').reset_index(drop=True)
        if as_of is None:
            as_of = m15.iloc[-1]['time']

        prov = CausalProvenance(symbol=self.symbol, as_of=as_of,
                                rr_gate=f"min_rr={self.config.min_rr:.1f}" if self.config.min_rr > 0 else "")

        d1_end = _latest_index_at_or_before(d1, as_of)
        h4_end = _latest_index_at_or_before(h4, as_of)
        m15_end = _latest_index_at_or_before(m15, as_of)
        if min(d1_end, h4_end, m15_end) < 0:
            prov.rejection_reason = "insufficient data for one or more timeframes"
            return prov

        d1_view = d1.iloc[:d1_end + 1].tail(self.config.d1_lookback).reset_index(drop=True)
        h4_view = h4.iloc[:h4_end + 1].reset_index(drop=True)
        m15_view = m15.iloc[:m15_end + 1].reset_index(drop=True)

        h4_swings = find_swings(h4_view, self.config.h4_swing_left, self.config.h4_swing_right)
        swing_by_id = {s.id: s for s in h4_swings}
        liquidity = build_liquidity_pools(h4_swings)
        sweeps = detect_sweeps(h4_view, liquidity, self.config.h4_sweep_lookback, swings=h4_swings)
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
        m15_atr = _compute_atr(m15_view, self.config.m15_atr_period)

        def _deepen(stage: str, reason: str) -> None:
            if _CHAIN_STAGES.index(stage) > _CHAIN_STAGES.index(prov.rejection_stage):
                prov.rejection_stage = stage
                prov.rejection_reason = reason

        _scan_ts = pd.Timestamp.now('UTC').isoformat()
        _cfg_fp = hashlib.md5(
            f"{self.config.d1_poi_lookback}|{self.config.h4_csd_window}"
            f"|{self.config.h4_sweep_lookback}|{self.config.m15_idm_window}"
            f"|{self.config.min_rr}".encode()
        ).hexdigest()[:8]

        def _emit(stage: str, reason: str, sweep, direction: str = "UNKNOWN",
                  evidence_timestamp: str = "") -> None:
            prov.funnel_events.append(FunnelEvent(
                symbol=self.symbol,
                scan_timestamp=_scan_ts,
                market_event_timestamp=str(getattr(sweep, 'candle_time', '')),
                stage=stage,
                reason=reason,
                direction=direction,
                causal_anchor_ref=str(getattr(sweep, 'id', '')),
                evidence_timestamp=evidence_timestamp,
                config_fingerprint=_cfg_fp,
            ))

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
                _emit("D1_POI", "NO_D1_POI", sweep)
                continue

            for poi in pois:
                prov.d1_poi_count += 1
                desired_side = LiquiditySide.SELL_SIDE if poi.direction is Direction.BULLISH else LiquiditySide.BUY_SIDE
                poi_dir = poi.direction.value if hasattr(poi.direction, 'value') else str(poi.direction)
                if sweep.side is not desired_side:
                    _deepen("H4_SWEEP",
                            f"sweep {sweep.side.value} doesn't match {poi.direction.value} POI (needs {desired_side.value})")
                    _emit("H4_SWEEP", "D1_POI_DIRECTION_MISMATCH", sweep, direction=poi_dir)
                    continue
                if not (poi.created_time <= sweep.candle_time):
                    _deepen("H4_SWEEP", "POI created after sweep candle time")
                    _emit("H4_SWEEP", "D1_POI_INVALID", sweep, direction=poi_dir,
                          evidence_timestamp=str(poi.created_time))
                    continue
                prov.matching_sweep_count += 1

                pre_sweep_breaks = [
                    b for b in breaks
                    if b.source_swing_id in swing_by_id
                    and swing_by_id[b.source_swing_id].index < sweep.candle_index
                    and swing_by_id[b.source_swing_id].confirmation_index <= sweep.candle_index
                ]
                csd = confirm_csd(h4_view, sweep, pre_sweep_breaks, self.config.h4_csd_window, swings=h4_swings)
                if csd is None:
                    _deepen("H4_CSD",
                            f"no confirming CSD within {self.config.h4_csd_window} H4 bars after sweep")
                    _emit("H4_CSD", "NO_CSD", sweep, direction=poi_dir)
                    continue

                m15_end_for_csd = _latest_index_at_or_before(m15_view, csd.candle_time)
                if m15_end_for_csd < 0:
                    # CSD is older than the M15 data window; skip without counting.
                    # (Counting here would make csd_count misleadingly include setups
                    # for which no M15 analysis is possible.)
                    continue
                prov.csd_count += 1
                m15_after_indices = [i for i in m15_displacement_indices if i > m15_end_for_csd]
                blocks = find_order_blocks(m15_work, m15_after_indices, 'M15', self.config.m15_ob_search_back)
                blocks = [b for b in blocks
                          if b.candle_index > m15_end_for_csd and b.source_displacement_index > m15_end_for_csd]

                prov.post_csd_ob_count += len(blocks)
                if not blocks:
                    _deepen("M15_OB", "no M15 OB formed after CSD")
                    _emit("M15_OB", "NO_POST_CSD_POI", sweep, direction=poi_dir,
                          evidence_timestamp=str(csd.candle_time))
                    continue

                swings_after = [
                    s for s in m15_swings
                    if s.index > m15_end_for_csd and s.confirmation_index <= m15_end
                ]

                for block in blocks:
                    idms = find_inducements(
                        m15_view, [block], swings_after, self.config.m15_idm_window, as_of=as_of
                    )
                    idms = [
                        x for x in idms
                        if block.source_displacement_index >= 0
                        and pd.Timestamp(m15_view.iloc[block.source_displacement_index]["time"]) <= pd.Timestamp(x.confirmation_time)
                    ]
                    if not idms:
                        _deepen("M15_IDM", "no IDM/inducement confirmed for OB")
                        _emit("M15_IDM", "NO_IDM", sweep, direction=poi_dir,
                              evidence_timestamp=str(csd.candle_time))
                        continue

                    prov.idm_count += len(idms)
                    contexts = execution_context(poi, [block], idms, poi.direction)
                    if not contexts:
                        _deepen("M15_MITIGATION",
                                "OB+IDM found but execution context invalid (OB may be mitigated or direction mismatch)")
                        continue

                    prov.unmitigated_ob_count += 1
                    for context in contexts:
                        event_time = context.inducement.confirmation_time or context.inducement.candle_time
                        _stop_dist = abs(sweep.sweep_extreme - context.order_block.mitigation_price)
                        _irl_kwargs = dict(
                            ohlc=m15_view,
                            as_of=event_time,
                            min_distance=m15_atr,
                            ob_candle_index=block.candle_index,
                            ob_proximity_bars=self.config.m15_irl_ob_proximity,
                            stop_distance=_stop_dist,
                        )
                        # Structural check: is there any IRL target at all (ignoring RR gate)?
                        irl_structural = find_irl_target(
                            context.order_block.mitigation_price,
                            context.direction,
                            m15_swings,
                            min_rr=0.0,
                            **_irl_kwargs,
                        )
                        if irl_structural is None:
                            _deepen("IRL", "no structural IRL target in M15 swing pool")
                            _emit("IRL", "IRL_MISSING", sweep, direction=poi_dir,
                                  evidence_timestamp=str(csd.candle_time))
                            continue

                        prov.structurally_qualified_count += 1
                        _deepen("READY", f"structurally qualified: {prov.structurally_qualified_count}")
                        _emit("READY", "ADVANCED", sweep, direction=poi_dir,
                              evidence_timestamp=str(csd.candle_time))

                        # RR gate check (only when a threshold is configured).
                        if self.config.min_rr > 0.0:
                            irl = find_irl_target(
                                context.order_block.mitigation_price,
                                context.direction,
                                m15_swings,
                                min_rr=self.config.min_rr,
                                **_irl_kwargs,
                            )
                            if irl is None:
                                prov.rr_filtered_count += 1
                                _emit("READY", "RR_FILTERED", sweep, direction=poi_dir,
                                      evidence_timestamp=str(csd.candle_time))
                                continue
                        else:
                            irl = irl_structural

                        try:
                            build_trade_setup(
                                self.symbol, context, sweep, csd, irl,
                                risk_percent=self.config.risk_percent, created_time=event_time,
                            )
                            prov.execution_ready_count += 1
                            prov.qualified_candidate_count += 1
                            _deepen("READY", f"{prov.qualified_candidate_count} candidate(s) qualified")
                            _emit("READY", "EXECUTION_READY", sweep, direction=poi_dir,
                                  evidence_timestamp=str(csd.candle_time))
                        except ValueError as exc:
                            _deepen("IRL", f"setup build failed: {exc}")
                            _emit("IRL", "SETUP_BUILD_FAILED", sweep, direction=poi_dir,
                                  evidence_timestamp=str(csd.candle_time))

        return prov