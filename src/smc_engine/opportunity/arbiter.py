"""Opportunity conflict arbitration.

Enforces: ONLY ONE ACTIVE DIRECTIONAL THESIS PER CANONICAL INSTRUMENT.

The arbiter is injected between discovery (sweep/BOS detection) and persistence
(_arm). It runs inside the engine's existing SHARED_WRITE_LOCK; no second
concurrency mechanism is introduced.

Decisions:
  ACCEPT          — no conflict; create the new opportunity as normal
  SUPERSEDE       — new thesis is stronger; supersede the listed existing opp(s)
                    and then create the new one
  REJECT          — existing thesis is stronger; discard the new candidate silently

Canonical instrument identity uses norm_symbol (strips non-alnum, upper case).
No broker-symbol aliases are hard-coded without evidence from the broker spec.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import (Opportunity, OpportunityState as St, OpportunityEventKind as Ev,
                     BlockReason)
from .repository import OpportunityRepository

# ------------------------------------------------------------------- constants

_STATE_STRENGTH: dict[str, int] = {
    St.READY_FOR_MITIGATION.value: 7,
    St.ENTRY_TRIGGERED.value: 6,      # terminal but causal completion reached
    St.WAITING_FOR_IDM.value: 5,
    St.WAITING_FOR_POI.value: 4,
    St.WAITING_FOR_CONFIRMATION.value: 3,
    St.OPPORTUNITY_ARMED.value: 2,
}


def _norm(symbol: str) -> str:
    return "".join(c for c in symbol.upper() if c.isalnum())


def canonical_instrument(symbol: str) -> str:
    """Map broker symbol to canonical instrument for conflict detection.

    Currently: identity (norm_symbol). A config-driven alias table can be
    injected later once broker-spec confirms XAUUSD247 ≡ XAUUSD etc.
    """
    return _norm(symbol)


def thesis_strength(opp: Opportunity) -> int:
    """Deterministic strength score for a persisted opportunity.

    Higher = more causally developed / harder to displace.
    Terminal non-SUPERSEDED states count their score (e.g. ENTRY_TRIGGERED = 6)
    so a completed thesis can block re-discovery on restart.
    """
    return _STATE_STRENGTH.get(opp.state, 1)


def _anchor_ns(opp: Opportunity) -> int:
    """Extract the anchor-event nanosecond timestamp from the canonical_key."""
    parts = opp.canonical_key.split(":")
    if len(parts) >= 5:
        try:
            return int(parts[4])
        except ValueError:
            return 0
    return 0


# ------------------------------------------------------------------- decision

@dataclass
class ArbiterDecision:
    outcome: str                        # "ACCEPT" | "SUPERSEDE" | "REJECT"
    superseded_ids: list = field(default_factory=list)
    reason: str = ""


# ------------------------------------------------------------------- arbiter

class OpportunityArbiter:
    """Deterministic single-thesis-per-instrument enforcer.

    Called by OpportunityEngine BEFORE repo.upsert() for any new candidate.
    Also called once per engine lifetime at startup for reconciliation.
    """

    def __init__(self, repo: OpportunityRepository):
        self._repo = repo
        self.conflicts_detected: int = 0
        self.conflicts_resolved: int = 0
        self.opportunities_superseded: int = 0
        self.duplicates_rejected: int = 0

    # ------------------------------------------------------------------public

    def arbitrate(self, symbol: str, direction: str,
                  initial_state: str, anchor_time_ns: int) -> ArbiterDecision:
        """Determine whether a new opportunity may be created.

        :param symbol:        broker symbol (as received from the scanner)
        :param direction:     BULLISH | BEARISH
        :param initial_state: state the new opp would start in
        :param anchor_time_ns: anchor event time in nanoseconds (for tie-break)
        :returns: ArbiterDecision
        """
        inst = canonical_instrument(symbol)
        active = self._repo.active_for_instrument(inst)
        if not active:
            return ArbiterDecision(outcome="ACCEPT")

        new_strength = _STATE_STRENGTH.get(initial_state, 1)

        # Separate opponents and same-direction rivals
        opposing = [o for o in active if o.direction != direction]
        same_dir = [o for o in active if o.direction == direction]

        # ---- opposing-direction conflict ----
        if opposing:
            self.conflicts_detected += 1
            best = max(opposing, key=lambda o: (thesis_strength(o), _anchor_ns(o)))
            best_strength = thesis_strength(best)

            if new_strength > best_strength:
                # New thesis is stronger: supersede the existing one
                self.conflicts_resolved += 1
                return ArbiterDecision(
                    outcome="SUPERSEDE",
                    superseded_ids=[best.opportunity_id],
                    reason=(f"new {direction} thesis (strength {new_strength}) "
                            f"supersedes {best.direction} {best.opportunity_id} "
                            f"(strength {best_strength})"))

            if new_strength == best_strength:
                # Tie: most recent anchor event wins
                if anchor_time_ns > _anchor_ns(best):
                    self.conflicts_resolved += 1
                    return ArbiterDecision(
                        outcome="SUPERSEDE",
                        superseded_ids=[best.opportunity_id],
                        reason=(f"new {direction} thesis tie-breaks by anchor recency "
                                f"over {best.opportunity_id}"))
                # Existing is same-age or newer: reject new
            # Existing is stronger (or same age): reject the new candidate
            self.duplicates_rejected += 1
            return ArbiterDecision(
                outcome="REJECT",
                reason=(f"existing {best.direction} thesis {best.opportunity_id} "
                        f"(strength {best_strength}) blocks new {direction} "
                        f"(strength {new_strength})"))

        # ---- same-direction conflict ----
        if same_dir:
            best = max(same_dir, key=lambda o: (thesis_strength(o), _anchor_ns(o)))
            best_strength = thesis_strength(best)

            if new_strength > best_strength:
                # New same-direction thesis is more developed: supersede old
                self.conflicts_detected += 1
                self.conflicts_resolved += 1
                return ArbiterDecision(
                    outcome="SUPERSEDE",
                    superseded_ids=[best.opportunity_id],
                    reason=(f"stronger same-direction {direction} thesis "
                            f"(strength {new_strength}) supersedes {best.opportunity_id} "
                            f"(strength {best_strength})"))

            # Existing same-direction is equal or stronger: reject duplicate
            self.duplicates_rejected += 1
            return ArbiterDecision(
                outcome="REJECT",
                reason=(f"same-direction {direction} thesis already active: "
                        f"{best.opportunity_id} (strength {best_strength})"))

        return ArbiterDecision(outcome="ACCEPT")

    def reconcile(self, now_iso: str) -> list[tuple[str, str]]:
        """Reconcile pre-existing conflicts in the DB.

        Returns list of (superseded_opp_id, winner_opp_id) pairs.
        Called once at engine startup before the first scan.
        """
        all_active = self._repo.query(active_only=True, limit=2000)
        by_instrument: dict[str, list[Opportunity]] = {}
        for opp in all_active:
            inst = canonical_instrument(opp.symbol)
            by_instrument.setdefault(inst, []).append(opp)

        pairs: list[tuple[str, str]] = []
        for inst, opps in by_instrument.items():
            if len(opps) <= 1:
                continue
            # Find opposing-direction pairs; keep strongest per direction
            bull = [o for o in opps if o.direction == "BULLISH"]
            bear = [o for o in opps if o.direction == "BEARISH"]
            if bull and bear:
                best_bull = max(bull, key=lambda o: (thesis_strength(o), _anchor_ns(o)))
                best_bear = max(bear, key=lambda o: (thesis_strength(o), _anchor_ns(o)))
                bull_str = thesis_strength(best_bull)
                bear_str = thesis_strength(best_bear)
                if bull_str >= bear_str:
                    winner, losers = best_bull, bear
                else:
                    winner, losers = best_bear, bull
                for loser in losers:
                    pairs.append((loser.opportunity_id, winner.opportunity_id))
            # Also deduplicate same-direction if more than one active
            for direction_group in (bull, bear):
                if len(direction_group) <= 1:
                    continue
                best = max(direction_group,
                           key=lambda o: (thesis_strength(o), _anchor_ns(o)))
                for opp in direction_group:
                    if opp.opportunity_id != best.opportunity_id:
                        pairs.append((opp.opportunity_id, best.opportunity_id))
        return pairs

    def diagnostics(self) -> dict:
        return {
            "conflicts_detected": self.conflicts_detected,
            "conflicts_resolved": self.conflicts_resolved,
            "opportunities_superseded": self.opportunities_superseded,
            "duplicates_rejected": self.duplicates_rejected,
        }
