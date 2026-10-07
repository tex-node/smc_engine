"""Explicit opportunity state machine.

Deliberately separate from the TradeSetup lifecycle machine: an opportunity is
not a setup. Terminal opportunity states never transition back — a consumed or
invalidated opportunity must not resurrect (the causal engine may still emit a
fresh setup under a NEW canonical identity).
"""
from __future__ import annotations

from .models import OpportunityState as S

TERMINAL = {S.INVALIDATED, S.EXPIRED, S.TERMINAL, S.ENTRY_TRIGGERED}

ALLOWED: dict[S, set[S]] = {
    S.IDLE: {S.STRUCTURAL_CONTEXT, S.OPPORTUNITY_ARMED, S.EXPIRED},
    S.STRUCTURAL_CONTEXT: {S.OPPORTUNITY_ARMED, S.WAITING_FOR_CONFIRMATION,
                           S.INVALIDATED, S.EXPIRED},
    S.OPPORTUNITY_ARMED: {S.WAITING_FOR_CONFIRMATION, S.WAITING_FOR_POI,
                          S.READY_FOR_MITIGATION, S.INVALIDATED, S.EXPIRED},
    S.WAITING_FOR_CONFIRMATION: {S.WAITING_FOR_POI, S.WAITING_FOR_IDM,
                                 S.READY_FOR_MITIGATION, S.INVALIDATED, S.EXPIRED},
    S.WAITING_FOR_POI: {S.WAITING_FOR_IDM, S.READY_FOR_MITIGATION,
                        S.INVALIDATED, S.EXPIRED},
    S.WAITING_FOR_IDM: {S.READY_FOR_MITIGATION, S.INVALIDATED, S.EXPIRED},
    S.READY_FOR_MITIGATION: {S.ENTRY_TRIGGERED, S.WAITING_FOR_POI, S.WAITING_FOR_IDM,
                             S.INVALIDATED, S.EXPIRED},
    S.ENTRY_TRIGGERED: set(),
    S.INVALIDATED: set(),
    S.EXPIRED: set(),
    S.TERMINAL: set(),
}


class IllegalTransition(ValueError):
    pass


def can_transition(current: str, target: str) -> bool:
    try:
        return S(target) in ALLOWED[S(current)]
    except (KeyError, ValueError):
        return False


def assert_transition(current: str, target: str) -> None:
    if current == target:
        return
    if not can_transition(current, target):
        raise IllegalTransition(f"opportunity transition {current} -> {target} is not allowed")


def is_terminal(state: str) -> bool:
    try:
        return S(state) in TERMINAL
    except ValueError:
        return True
