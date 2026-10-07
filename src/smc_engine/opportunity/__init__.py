"""Opportunity layer: tracks developing trade opportunities between causal
truth and execution-ready setup generation. Non-executing by construction."""
from .engine import OpportunityEngine
from .models import (BlockReason, EntryPathway, Opportunity, OpportunityEventKind,
                     OpportunityState, OpportunityType, OpportunityWindows,
                     canonical_opportunity_key)
from .repository import OpportunityRepository

__all__ = ["OpportunityEngine", "OpportunityRepository", "Opportunity",
           "OpportunityState", "OpportunityType", "EntryPathway", "BlockReason",
           "OpportunityEventKind", "OpportunityWindows", "canonical_opportunity_key"]
