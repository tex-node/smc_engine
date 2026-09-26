
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .models import POIState
from .setup import TradeSetup


class SetupState(str, Enum):
    IDLE = "IDLE"
    POI_ACTIVE = "POI_ACTIVE"
    LIQUIDITY_SWEPT = "LIQUIDITY_SWEPT"
    CSD_CONFIRMED = "CSD_CONFIRMED"
    EXECUTION_READY = "EXECUTION_READY"
    ORDER_PREPARED = "ORDER_PREPARED"
    ORDER_PREFLIGHTED = "ORDER_PREFLIGHTED"
    ORDER_SUBMITTING = "ORDER_SUBMITTING"
    ORDER_PLACED = "ORDER_PLACED"
    FILLED = "FILLED"
    POSITION_MANAGED = "POSITION_MANAGED"
    CLOSED = "CLOSED"
    POI_INVALIDATED = "POI_INVALIDATED"
    CSD_EXPIRED = "CSD_EXPIRED"
    OB_INVALIDATED = "OB_INVALIDATED"
    PROTECTED_LEVEL_BREACHED = "PROTECTED_LEVEL_BREACHED"
    ENTRY_NO_LONGER_VALID = "ENTRY_NO_LONGER_VALID"
    RISK_REJECTED = "RISK_REJECTED"
    BROKER_REJECTED = "BROKER_REJECTED"


@dataclass
class SetupLifecycle:
    setup: TradeSetup
    state: SetupState = SetupState.EXECUTION_READY
    reason: Optional[str] = None

    def transition(self, new_state: SetupState, reason: Optional[str] = None) -> None:
        allowed = {
            SetupState.EXECUTION_READY: {
                SetupState.ORDER_PREPARED,
                SetupState.POI_INVALIDATED,
                SetupState.OB_INVALIDATED,
                SetupState.PROTECTED_LEVEL_BREACHED,
                SetupState.ENTRY_NO_LONGER_VALID,
                SetupState.RISK_REJECTED,
            },
            SetupState.ORDER_PREPARED: {
                SetupState.ORDER_PREFLIGHTED,
                SetupState.BROKER_REJECTED,
                SetupState.ENTRY_NO_LONGER_VALID,
            },
            SetupState.ORDER_PREFLIGHTED: {
                SetupState.ORDER_SUBMITTING,
                SetupState.BROKER_REJECTED,
            },
            SetupState.ORDER_SUBMITTING: {
                SetupState.ORDER_PLACED,
                SetupState.BROKER_REJECTED,
            },
            SetupState.ORDER_PLACED: {
                SetupState.FILLED,
                SetupState.POI_INVALIDATED,
                SetupState.OB_INVALIDATED,
                SetupState.PROTECTED_LEVEL_BREACHED,
                SetupState.CLOSED,
            },
            SetupState.FILLED: {
                SetupState.POSITION_MANAGED,
                SetupState.CLOSED,
            },
            SetupState.POSITION_MANAGED: {SetupState.CLOSED},
        }
        if new_state not in allowed.get(self.state, set()):
            raise ValueError(f"Invalid transition {self.state.value} -> {new_state.value}")
        self.state = new_state
        self.reason = reason

    def invalidate_from_market(self, protected_level_breached: bool = False, poi_invalidated: bool = False, ob_invalidated: bool = False) -> None:
        if protected_level_breached:
            target = SetupState.PROTECTED_LEVEL_BREACHED
        elif poi_invalidated:
            target = SetupState.POI_INVALIDATED
        elif ob_invalidated:
            target = SetupState.OB_INVALIDATED
        else:
            target = SetupState.ENTRY_NO_LONGER_VALID
        if self.state in {SetupState.ORDER_PLACED, SetupState.EXECUTION_READY}:
            self.transition(target, "market invalidation")


@dataclass(frozen=True)
class LifecycleEvent:
    setup_id: str
    from_state: SetupState
    to_state: SetupState
    reason: str


class SetupRegistry:
    """In-memory deduplication/state registry.

    Persistence can replace this later without changing the state-machine API.
    """

    def __init__(self):
        self._items: dict[str, SetupLifecycle] = {}

    def add(self, lifecycle: SetupLifecycle) -> None:
        if lifecycle.setup.id in self._items:
            raise ValueError(f"Setup already registered: {lifecycle.setup.id}")
        self._items[lifecycle.setup.id] = lifecycle

    def get(self, setup_id: str) -> Optional[SetupLifecycle]:
        return self._items.get(setup_id)

    def active_for_symbol(self, symbol: str) -> list[SetupLifecycle]:
        terminal = {
            SetupState.CLOSED, SetupState.POI_INVALIDATED, SetupState.CSD_EXPIRED,
            SetupState.OB_INVALIDATED, SetupState.PROTECTED_LEVEL_BREACHED,
            SetupState.ENTRY_NO_LONGER_VALID, SetupState.RISK_REJECTED,
            SetupState.BROKER_REJECTED,
        }
        return [
            x for x in self._items.values()
            if x.setup.symbol == symbol and x.state not in terminal
        ]

    def has_active_for_symbol(self, symbol: str) -> bool:
        return bool(self.active_for_symbol(symbol))
