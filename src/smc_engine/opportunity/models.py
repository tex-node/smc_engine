"""Opportunity layer domain models.

Two separate concepts, deliberately not conflated:

  CAUSAL TRUTH     — what has structurally happened (engine-owned, unchanged)
  OPPORTUNITY STATE — what is currently developing as a potentially actionable
                      trade, tracked across polling cycles

An opportunity can exist (and persist) while there is no execution-ready
TradeSetup yet. This module owns only the opportunity-side vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pandas as pd

from ..models import Direction


class OpportunityState(str, Enum):
    IDLE = "IDLE"
    STRUCTURAL_CONTEXT = "STRUCTURAL_CONTEXT"
    OPPORTUNITY_ARMED = "OPPORTUNITY_ARMED"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    WAITING_FOR_POI = "WAITING_FOR_POI"
    WAITING_FOR_IDM = "WAITING_FOR_IDM"
    READY_FOR_MITIGATION = "READY_FOR_MITIGATION"
    ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    TERMINAL = "TERMINAL"


TERMINAL_STATES = {OpportunityState.INVALIDATED, OpportunityState.EXPIRED,
                   OpportunityState.TERMINAL, OpportunityState.ENTRY_TRIGGERED}

# UI-facing buckets (never implies a trade signal)
STATE_LABEL = {
    OpportunityState.IDLE: "WATCHING",
    OpportunityState.STRUCTURAL_CONTEXT: "WATCHING",
    OpportunityState.OPPORTUNITY_ARMED: "ARMED",
    OpportunityState.WAITING_FOR_CONFIRMATION: "DEVELOPING",
    OpportunityState.WAITING_FOR_POI: "DEVELOPING",
    OpportunityState.WAITING_FOR_IDM: "DEVELOPING",
    OpportunityState.READY_FOR_MITIGATION: "READY",
    OpportunityState.ENTRY_TRIGGERED: "EXECUTION READY",
    OpportunityState.INVALIDATED: "INVALIDATED",
    OpportunityState.EXPIRED: "EXPIRED",
    OpportunityState.TERMINAL: "EXPIRED",
}


class OpportunityType(str, Enum):
    REVERSAL = "REVERSAL"
    PULLBACK = "PULLBACK"
    CONTINUATION = "CONTINUATION"


class EntryPathway(str, Enum):
    PULLBACK = "PULLBACK"
    AGGRESSIVE = "AGGRESSIVE"
    SMART = "SMART"
    CONTINUATION = "CONTINUATION"


class BlockReason(str, Enum):
    NO_STRUCTURAL_CONTEXT = "NO_STRUCTURAL_CONTEXT"
    STRUCTURAL_CONTEXT_ONLY = "STRUCTURAL_CONTEXT_ONLY"
    SWEEP_ARMED = "SWEEP_ARMED"
    SWEEP_EXPIRED = "SWEEP_EXPIRED"
    WAITING_FOR_CSD = "WAITING_FOR_CSD"
    CSD_NOT_FOUND = "CSD_NOT_FOUND"
    CSD_CONFIRMED = "CSD_CONFIRMED"
    OPPOSING_CSD = "OPPOSING_CSD"
    WAITING_FOR_POI = "WAITING_FOR_POI"
    POI_NOT_FOUND = "POI_NOT_FOUND"
    POI_TOO_SMALL = "POI_TOO_SMALL"
    POI_CONSUMED = "POI_CONSUMED"
    POI_DIRECTION_INVALID = "POI_DIRECTION_INVALID"
    POI_STRUCTURALLY_INVALID = "POI_STRUCTURALLY_INVALID"
    POI_EXPIRED = "POI_EXPIRED"
    WAITING_FOR_IDM = "WAITING_FOR_IDM"
    IDM_NOT_CONFIRMED = "IDM_NOT_CONFIRMED"
    IDM_CONFIRMED = "IDM_CONFIRMED"
    READY_FOR_MITIGATION = "READY_FOR_MITIGATION"
    STRUCTURAL_INVALIDATION = "STRUCTURAL_INVALIDATION"
    PRICE_RAN_AWAY = "PRICE_RAN_AWAY"
    ENTRY_PASSED = "ENTRY_PASSED"
    RISK_REJECTED = "RISK_REJECTED"
    PORTFOLIO_RISK_REJECTED = "PORTFOLIO_RISK_REJECTED"
    TERMINAL_EXISTING = "TERMINAL_EXISTING"
    TERMINAL_HISTORICAL = "TERMINAL_HISTORICAL"
    RISK_BLOCKED = "RISK_BLOCKED"
    DUPLICATE = "DUPLICATE"
    EXPIRED_TTL = "EXPIRED_TTL"


class OpportunityEventKind(str, Enum):
    OPPORTUNITY_CREATED = "OPPORTUNITY_CREATED"
    OPPORTUNITY_ADVANCED = "OPPORTUNITY_ADVANCED"
    POI_FOUND = "POI_FOUND"
    IDM_CONFIRMED = "IDM_CONFIRMED"
    READY = "READY"
    EXECUTION_READY = "EXECUTION_READY"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    CONVERTED_TO_SETUP = "CONVERTED_TO_SETUP"


@dataclass(frozen=True)
class OpportunityWindows:
    """Centralized temporal windows (M15 bars unless noted). Observable in
    diagnostics; no scattered magic timestamps."""
    sweep_to_csd_bars: int = 24          # sweep -> CSD must confirm within
    csd_to_poi_bars: int = 96            # CSD -> qualifying POI discovery
    poi_to_mitigation_bars: int = 96     # POI discovery -> mitigation/entry
    continuation_bos_to_poi_bars: int = 96
    ready_ttl_bars: int = 96             # READY lifetime before expiry
    poi_max_age_bars: int = 192          # candidate POI usable age
    idm_window_bars: int = 24
    max_price_run_atr: float = 6.0       # runaway guard (ATR multiples)
    poi_min_height_atr: float = 0.25     # POI_TOO_SMALL guard

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class POICandidate:
    """A tracked (never manufactured) POI candidate for an opportunity."""
    poi_id: str
    kind: str                  # "OB" | "FVG" | "D1_POI"
    direction: str
    low: float
    high: float
    created_time: object
    mitigated: bool = False
    rank: Optional[float] = None
    rank_reason: str = ""
    rejected_reason: Optional[str] = None


@dataclass
class Opportunity:
    opportunity_id: str
    canonical_key: str
    symbol: str
    direction: str
    opportunity_type: str
    state: str
    created_at: object
    updated_at: object
    expires_at: object = None
    source_event_ids: list = field(default_factory=list)
    causal_chain_id: str = ""
    d1_context: str = ""
    h4_context: str = ""
    m15_context: str = ""
    sweep_evidence: dict = field(default_factory=dict)
    csd_evidence: dict = field(default_factory=dict)
    bos_evidence: dict = field(default_factory=dict)
    poi_candidates: list = field(default_factory=list)   # list[dict]
    selected_poi: str = ""
    idm_reference: str = ""
    entry_pathway: str = EntryPathway.PULLBACK.value
    invalidation_reference: str = ""
    readiness_status: str = ""
    lifecycle_status: str = ""
    first_seen: object = None
    last_seen: object = None
    reason: str = ""
    blocker: str = ""
    next_expected: str = ""
    setup_id: str = ""
    risk_status: str = ""
    state_history: list = field(default_factory=list)     # list[dict]

    @property
    def active(self) -> bool:
        return self.state not in {s.value for s in TERMINAL_STATES}


def _ns(value) -> int:
    """Canonical timestamp-derived identity component (never an index)."""
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return int(ts.value)


def canonical_opportunity_key(symbol: str, direction: str, opportunity_type: str,
                              anchor_kind: str, anchor_time, anchor_level: float) -> str:
    """Stable identity across polling cycles and rolling data windows.

    Built exclusively from timestamp/level components of the canonical engine
    event that anchors the opportunity — no indices, no object identity, no
    list positions.
    """
    return (f"{opportunity_type}:{symbol.upper()}:{direction}:{anchor_kind}:"
            f"{_ns(anchor_time)}:{round(float(anchor_level), 8)}")


def opportunity_id_from_key(canonical_key: str) -> str:
    parts = canonical_key.split(":")
    otype, symbol, direction, anchor_kind = parts[0], parts[1], parts[2], parts[3]
    return f"OPP-{otype}-{symbol}-{anchor_kind}-{parts[4]}-{direction}"
