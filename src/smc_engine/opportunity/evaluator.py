"""Pure opportunity evidence evaluation.

Reuses the authoritative engine primitives unchanged. Everything is computed
from data available at `as_of` only — no look-ahead. This module TRACKS
candidates; it never manufactures evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from ..causal import CausalMTFAnalyzer
from ..execution_structure import find_inducements, find_order_blocks
from ..fvg import detect_fvgs
from ..models import Direction, LiquiditySide, StructureEventType
from ..poi import DisplacementConfig, active_unmitigated_pois, detect_displacement
from ..structure import (build_liquidity_pools, confirm_csd, detect_structure_breaks,
                         detect_sweeps, find_swings)
from ..strategy import MultiTimeframeConfig
from .models import BlockReason, OpportunityWindows, POICandidate


@dataclass
class CausalView:
    symbol: str
    as_of: object
    m15_last_time: object = None
    m15_atr: float = 0.0
    h4_swings: list = field(default_factory=list)
    sweeps: list = field(default_factory=list)
    breaks: list = field(default_factory=list)
    csd_by_sweep: dict = field(default_factory=dict)   # sweep.id -> StructureEvent
    d1_pois: list = field(default_factory=list)
    m15_obs: list = field(default_factory=list)
    m15_fvgs: list = field(default_factory=list)
    m15_swings: list = field(default_factory=list)
    idms: list = field(default_factory=list)
    candidates: list = field(default_factory=list)      # raw CausalCandidate
    m15_frame: object = None                            # truncated M15 candles
    d1_last_time: object = None
    h4_last_time: object = None


def _truncate(df: pd.DataFrame, as_of) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return df
    times = pd.to_datetime(df["time"], utc=True)
    if as_of is None:
        return df.reset_index(drop=True)
    ts = pd.Timestamp(as_of)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return df[times <= ts].reset_index(drop=True)


def _m15_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.0
    work = detect_displacement(df, DisplacementConfig(atr_period=period))
    atr = work["atr"].dropna()
    return float(atr.iloc[-1]) if len(atr) else 0.0


def build_view(symbol: str, d1: pd.DataFrame, h4: pd.DataFrame, m15: pd.DataFrame,
               as_of=None, config: Optional[MultiTimeframeConfig] = None) -> CausalView:
    """Causal evidence at `as_of`, computed strictly from candles <= as_of."""
    config = config or MultiTimeframeConfig()
    d1v = _truncate(d1, as_of)
    h4v = _truncate(h4, as_of)
    m15v = _truncate(m15, as_of)
    view = CausalView(symbol=symbol, as_of=(pd.Timestamp(as_of) if as_of is not None
                                            else (m15v["time"].iloc[-1] if len(m15v) else None)),
                      m15_frame=m15v)
    if len(m15v):
        view.m15_last_time = m15v["time"].iloc[-1]
        view.m15_atr = _m15_atr(m15v, config.m15_atr_period)
    if len(h4v):
        view.h4_last_time = h4v["time"].iloc[-1]
    if len(d1v):
        view.d1_last_time = d1v["time"].iloc[-1]
    if len(h4v) < 10 or len(m15v) < 10 or len(d1v) < 5:
        return view

    swings = find_swings(h4v, config.h4_swing_left, config.h4_swing_right)
    pools = build_liquidity_pools(swings)
    sweeps = detect_sweeps(h4v, pools, config.h4_sweep_lookback, swings=swings)
    breaks = detect_structure_breaks(h4v, swings)
    swing_by_id = {s.id: s for s in swings}

    csd_by_sweep = {}
    for sweep in sweeps:
        pre = [b for b in breaks
               if b.source_swing_id in swing_by_id
               and swing_by_id[b.source_swing_id].index < sweep.candle_index
               and swing_by_id[b.source_swing_id].confirmation_index <= sweep.candle_index]
        csd = confirm_csd(h4v, sweep, pre, config.h4_csd_window, swings=swings)
        if csd is not None:
            csd_by_sweep[sweep.id] = csd

    disp = detect_displacement(m15v, DisplacementConfig(
        atr_period=config.m15_atr_period, body_atr_multiple=config.m15_displacement_atr))
    disp_idx = [i for i, r in disp.iterrows()
                if bool(r["displacement_bullish"] or r["displacement_bearish"])]
    obs = find_order_blocks(disp, disp_idx, "M15", config.m15_ob_search_back)
    m15_swings = find_swings(m15v, config.m15_swing_left, config.m15_swing_right)
    idms = find_inducements(m15v, obs, m15_swings, config.m15_idm_window,
                            as_of=view.as_of)
    fvgs = detect_fvgs(m15v)
    d1_pois = active_unmitigated_pois(
        d1v, lookback_bars=min(config.d1_poi_lookback, len(d1v)))

    view.h4_swings, view.sweeps, view.breaks = swings, sweeps, breaks
    view.csd_by_sweep = csd_by_sweep
    view.m15_obs, view.m15_fvgs, view.m15_swings, view.idms = obs, fvgs, m15_swings, idms
    view.d1_pois = d1_pois
    view.candidates = CausalMTFAnalyzer(symbol, config).analyze_at(
        d1v, h4v, m15v, as_of=view.as_of)
    return view


def _bars_since(time_value, last_time, minutes: int = 15) -> int:
    if time_value is None or last_time is None:
        return 10 ** 6
    try:
        return int((pd.Timestamp(last_time) - pd.Timestamp(time_value))
                   / pd.Timedelta(minutes=minutes))
    except Exception:
        return 10 ** 6


def poi_candidates(view: CausalView, direction: Direction,
                   windows: OpportunityWindows,
                   created_after=None, created_before=None) -> list[POICandidate]:
    """Tracked POI candidates (D1 POI / M15 OB / FVG), ranked deterministically.

    `created_after`/`created_before` enforce the causal window between the
    confirming event and the POI (event-time based, never wall-clock).
    Rejections are preserved with machine-readable reasons (never discarded
    silently). No candidate is manufactured.
    """
    want = "BULLISH" if direction is Direction.BULLISH else "BEARISH"
    out: list[POICandidate] = []
    min_height = windows.poi_min_height_atr * (view.m15_atr or 0.0)

    def add(c: POICandidate):
        if c.direction != want:
            c.rejected_reason = BlockReason.POI_DIRECTION_INVALID.value
        elif created_after is not None and pd.Timestamp(c.created_time) < pd.Timestamp(created_after):
            c.rejected_reason = BlockReason.POI_STRUCTURALLY_INVALID.value
        elif created_before is not None and pd.Timestamp(c.created_time) > pd.Timestamp(created_before):
            # POI formed outside the causal window after the confirming event
            c.rejected_reason = BlockReason.STALE_ANCHOR.value
        elif c.mitigated:
            c.rejected_reason = BlockReason.POI_CONSUMED.value
        elif min_height and (c.high - c.low) < min_height:
            c.rejected_reason = BlockReason.POI_TOO_SMALL.value
        elif _bars_since(c.created_time, view.m15_last_time) > windows.poi_max_age_bars:
            c.rejected_reason = BlockReason.POI_EXPIRED.value
        out.append(c)

    for p in view.d1_pois:
        add(POICandidate(poi_id=p.id, kind="D1_POI", direction=p.direction.value,
                         low=float(p.low), high=float(p.high),
                         created_time=p.created_time,
                         mitigated=getattr(p, "state", None) is not None
                         and str(getattr(p.state, "value", p.state)) == "MITIGATED"))
    for b in view.m15_obs:
        add(POICandidate(poi_id=b.id, kind="OB", direction=b.direction.value,
                         low=float(b.low), high=float(b.high),
                         created_time=b.candle_time))
    for g in view.m15_fvgs:
        add(POICandidate(poi_id=g.id, kind="FVG", direction=g.direction.value,
                         low=float(g.bottom), high=float(g.top),
                         created_time=g.created_time,
                         mitigated=g.state == "MITIGATED"))

    valid = [c for c in out if c.rejected_reason is None]
    for c in valid:
        causal_bonus = 0.0
        if created_after is not None and pd.Timestamp(c.created_time) >= pd.Timestamp(created_after):
            causal_bonus = -1.0          # created after the confirming event = causal
        recency = _bars_since(c.created_time, view.m15_last_time)
        kind_bonus = {"D1_POI": -0.5, "OB": -0.25, "FVG": 0.0}[c.kind]
        c.rank = round(causal_bonus + kind_bonus + recency / 1000.0, 6)
        c.rank_reason = ("causal-after-event" if causal_bonus else "context")
    valid.sort(key=lambda c: (c.rank, c.poi_id))
    for i, c in enumerate(valid):
        c.rank_reason = f"{c.rank_reason};rank={i}"
    # deterministic ordering for consumers: valid (ranked) then rejected
    rejected = [c for c in out if c.rejected_reason is not None]
    return valid + rejected


def audit_classification(view: CausalView, active_states: list[str],
                         risk_status: str = "") -> str:
    """Machine-readable answer to 'why is this symbol not execution-ready?'."""
    if not view.sweeps and not view.breaks and not view.d1_pois:
        return BlockReason.NO_STRUCTURAL_CONTEXT.value
    if active_states:
        top = active_states[0]
        mapping = {
            "OPPORTUNITY_ARMED": BlockReason.SWEEP_ARMED.value,
            "WAITING_FOR_CONFIRMATION": BlockReason.WAITING_FOR_CSD.value,
            "WAITING_FOR_POI": BlockReason.WAITING_FOR_POI.value,
            "WAITING_FOR_IDM": BlockReason.WAITING_FOR_IDM.value,
            "READY_FOR_MITIGATION": BlockReason.READY_FOR_MITIGATION.value,
        }
        if top in mapping:
            return mapping[top]
    if view.csd_by_sweep:
        return BlockReason.CSD_CONFIRMED.value
    if view.sweeps:
        return BlockReason.SWEEP_ARMED.value
    if view.d1_pois:
        return BlockReason.STRUCTURAL_CONTEXT_ONLY.value
    if risk_status in ("RISK_REJECTED", "RISK_PORTFOLIO_REJECTED"):
        return BlockReason.RISK_BLOCKED.value
    return BlockReason.STRUCTURAL_CONTEXT_ONLY.value
