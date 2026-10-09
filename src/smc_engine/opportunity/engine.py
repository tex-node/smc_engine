"""OpportunityEngine — the opportunity layer between causal truth and setup
generation.

Non-executing by construction: no broker primitives exist in this module.
It observes the authoritative causal view, tracks developing opportunities
across polling cycles, persists state, and emits deduplicated transition
events. TradeSetup creation remains owned by the existing causal path.
"""
from __future__ import annotations

import threading
from typing import Optional

import pandas as pd

from ..models import Direction, StructureEventType
from ..strategy import MultiTimeframeConfig
from . import evaluator
from .arbiter import ArbiterDecision, OpportunityArbiter, thesis_strength
from .models import (TERMINAL_STATES, BlockReason, EntryPathway, Opportunity,
                     OpportunityEventKind as Ev, OpportunityState as St,
                     OpportunityType as Ty, OpportunityWindows,
                     canonical_opportunity_key, opportunity_id_from_key,
                     _ns)
from .repository import OpportunityRepository
from .state_machine import assert_transition, is_terminal


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _iso(ts) -> str:
    return pd.Timestamp(ts).isoformat()


class OpportunityEngine:
    def __init__(self, repository: OpportunityRepository,
                 windows: Optional[OpportunityWindows] = None,
                 config: Optional[MultiTimeframeConfig] = None,
                 pathways: Optional[list[str]] = None):
        self.repo = repository
        self.windows = windows or OpportunityWindows()
        self.config = config or MultiTimeframeConfig()
        # Enabled entry pathways (opportunity-generation policy, NOT an
        # execution authorization). All four are distinct attributes/states.
        self.pathways = pathways or [EntryPathway.PULLBACK.value,
                                     EntryPathway.AGGRESSIVE.value,
                                     EntryPathway.SMART.value,
                                     EntryPathway.CONTINUATION.value]
        self.smart_max_distance_atr = 1.5
        self.arbiter = OpportunityArbiter(repository)
        self._lock = threading.RLock()
        self._last_view: dict = {}
        self._reconciled = False
        self._diag: dict = {"last_scan_time": None, "opportunities_created": 0,
                            "opportunities_advanced": 0, "alerts_emitted": 0,
                            "alerts_deduplicated": 0}

    # ------------------------------------------------------------------ public
    def observe(self, symbol: str, view: evaluator.CausalView) -> dict:
        """Advance the opportunity state machine for one symbol from one
        authoritative causal view. Idempotent per (view, as_of)."""
        result = {"created": [], "advanced": [], "invalidated": [], "expired": [],
                  "converted": [], "events": [], "blocked": [], "superseded": []}
        with self._lock:
            if not self._reconciled:
                self._reconcile_startup(result)
                self._reconciled = True
            self._last_view[symbol] = view
            now = view.m15_last_time or _now()
            self._discover_reversal(symbol, view, now, result)
            self._discover_continuation(symbol, view, now, result)
            self._advance_symbol(symbol, view, now, result)
            self._promote(symbol, view, result)
            self._expire_symbol(symbol, now, result)
            self._diag["last_scan_time"] = _iso(now)
        return result

    def expire_cycle(self, symbol: Optional[str] = None,
                     now: Optional[pd.Timestamp] = None) -> dict:
        """Cheap TTL sweep used by the watcher between data-driven scans.

        `now` may be supplied for deterministic replay/tests.
        """
        out = {"expired": [], "events": []}
        with self._lock:
            now = pd.Timestamp(now) if now is not None else _now()
            for opp in self.repo.query(symbol=symbol, active_only=True, limit=1000):
                if opp.expires_at and pd.Timestamp(opp.expires_at) < now:
                    self._terminate(opp, St.EXPIRED, BlockReason.EXPIRED_TTL.value,
                                    "expires_at reached", now, out)
        return out

    # ------------------------------------------------------------- discovery
    def _arm(self, key: str, symbol: str, direction: str, otype: str,
             anchor_kind: str, anchor_time, anchor_level: float,
             evidence: dict, pathway: str, now, result: dict,
             initial_state: str = St.OPPORTUNITY_ARMED.value) -> Opportunity:
        opp = Opportunity(
            opportunity_id=opportunity_id_from_key(key), canonical_key=key,
            symbol=symbol, direction=direction, opportunity_type=otype,
            state=initial_state, created_at=_iso(now), updated_at=_iso(now),
            # ARMED/WAITING lifetime is anchored to the MARKET EVENT, never to
            # the wall clock: repeated polling/rehydration must not rejuvenate.
            expires_at=_iso(pd.Timestamp(anchor_time) + pd.Timedelta(
                minutes=15 * self.windows.sweep_to_csd_bars)),
            first_seen=_iso(now), last_seen=_iso(now),
            observed_time=_iso(now),
            entry_pathway=pathway, reason="sweep_armed",
            blocker=BlockReason.WAITING_FOR_CSD.value,
            next_expected="CSD confirmation",
            source_event_ids=[evidence.get("id", "")])
        if otype == Ty.REVERSAL.value:
            opp.sweep_evidence = evidence
            opp.sweep_time = _iso(anchor_time)
        else:
            opp.bos_evidence = evidence
            opp.bos_time = _iso(anchor_time)
            opp.expires_at = _iso(pd.Timestamp(anchor_time) + pd.Timedelta(
                minutes=15 * self.windows.continuation_bos_to_poi_bars))
        self.repo.upsert(opp)
        self.repo.record_state_change(opp.opportunity_id, St.IDLE.value,
                                      opp.state, "armed", anchor_kind)
        self._emit(opp, Ev.OPPORTUNITY_CREATED.value, f"{anchor_kind}:{_ns(anchor_time)}",
                   f"{symbol} {direction} {otype} armed ({anchor_kind})", result)
        result["created"].append(opp.opportunity_id)
        self._diag["opportunities_created"] += 1
        return opp

    def _reversal_pathway(self) -> str:
        """Entry pathway policy for the reversal stream (first enabled of
        PULLBACK/AGGRESSIVE/SMART; default PULLBACK)."""
        for p in (EntryPathway.PULLBACK.value, EntryPathway.AGGRESSIVE.value,
                  EntryPathway.SMART.value):
            if p in self.pathways:
                return p
        return EntryPathway.PULLBACK.value

    def _discover_reversal(self, symbol: str, view: evaluator.CausalView,
                           now, result: dict) -> None:
        for sweep in view.sweeps:
            age = evaluator._bars_since(sweep.candle_time, view.m15_last_time)
            csd = view.csd_by_sweep.get(sweep.id)
            if csd is None and age > self.windows.sweep_to_csd_bars:
                continue          # stale sweep: the causal window has passed
            if csd is None and age > self.windows.sweep_to_csd_bars + self.windows.csd_to_poi_bars:
                continue
            direction = (Direction.BULLISH if sweep.side.value == "SELL_SIDE"
                         else Direction.BEARISH)
            key = canonical_opportunity_key(symbol, direction.value, Ty.REVERSAL.value,
                                            "SWEEP", sweep.candle_time, sweep.swept_level)
            if self.repo.get_by_key(key) is not None:
                continue                          # duplicate sweep: one opportunity
            evidence = {"id": sweep.id, "side": sweep.side.value,
                        "swept_level": float(sweep.swept_level),
                        "sweep_extreme": float(sweep.sweep_extreme),
                        "time": _iso(sweep.candle_time)}
            # Late observation (CSD already confirmed): arm directly at the
            # post-CSD stage so an opportunity whose components unfolded
            # across time is not lost.
            if csd is not None:
                # P1-A: the CSD must lie within the sweep->CSD causal window of
                # the sweep EVENT (event-time based, not observation-time).
                if (pd.Timestamp(csd.candle_time) - pd.Timestamp(sweep.candle_time)) > \
                        pd.Timedelta(minutes=15 * self.windows.sweep_to_csd_bars):
                    continue          # stale CSD for this sweep: never arm
                initial = St.WAITING_FOR_POI.value
            else:
                initial = St.OPPORTUNITY_ARMED.value
            decision = self.arbiter.arbitrate(
                symbol, direction.value, initial, int(_ns(sweep.candle_time)))
            if decision.outcome == "SUPERSEDE":
                for sid in decision.superseded_ids:
                    self._supersede_opp(sid, key, decision.reason, now, result)
            elif decision.outcome == "REJECT":
                result["blocked"].append(
                    {"key": key, "reason": decision.reason,
                     "block": BlockReason.CONFLICT_REJECTED.value})
                continue
            if csd is not None:
                opp = self._arm(key, symbol, direction.value, Ty.REVERSAL.value, "SWEEP",
                                sweep.candle_time, sweep.swept_level, evidence,
                                self._reversal_pathway(), now, result,
                                initial_state=initial)
                opp.csd_evidence = {"id": csd.id, "direction": csd.direction.value,
                                    "level": float(csd.level), "time": _iso(csd.candle_time)}
                opp.csd_time = _iso(csd.candle_time)
                opp.h4_context = csd.id
                opp.expires_at = _iso(pd.Timestamp(csd.candle_time) + pd.Timedelta(
                    minutes=15 * self.windows.csd_to_poi_bars))
                self.repo.upsert(opp)
            else:
                self._arm(key, symbol, direction.value, Ty.REVERSAL.value, "SWEEP",
                          sweep.candle_time, sweep.swept_level, evidence,
                          self._reversal_pathway(), now, result)

    def _discover_continuation(self, symbol: str, view: evaluator.CausalView,
                               now, result: dict) -> None:
        if EntryPathway.CONTINUATION.value not in self.pathways:
            return
        for bos in view.breaks:
            if bos.type is not StructureEventType.BOS:
                continue
            age = evaluator._bars_since(bos.candle_time, view.m15_last_time)
            if age > self.windows.continuation_bos_to_poi_bars:
                continue
            key = canonical_opportunity_key(symbol, bos.direction.value,
                                            Ty.CONTINUATION.value, "BOS",
                                            bos.candle_time, bos.level)
            if self.repo.get_by_key(key) is not None:
                continue
            evidence = {"id": bos.id, "type": bos.type.value,
                        "direction": bos.direction.value, "level": float(bos.level),
                        "time": _iso(bos.candle_time)}
            decision = self.arbiter.arbitrate(
                symbol, bos.direction.value, St.OPPORTUNITY_ARMED.value,
                int(_ns(bos.candle_time)))
            if decision.outcome == "SUPERSEDE":
                for sid in decision.superseded_ids:
                    self._supersede_opp(sid, key, decision.reason, now, result)
            elif decision.outcome == "REJECT":
                result["blocked"].append(
                    {"key": key, "reason": decision.reason,
                     "block": BlockReason.CONFLICT_REJECTED.value})
                continue
            self._arm(key, symbol, bos.direction.value, Ty.CONTINUATION.value, "BOS",
                      bos.candle_time, bos.level, evidence,
                      EntryPathway.CONTINUATION.value, now, result)

    # -------------------------------------------------------------- advancing
    def _advance_symbol(self, symbol: str, view: evaluator.CausalView,
                        now, result: dict) -> None:
        for opp in self.repo.query(symbol=symbol, active_only=True, limit=1000):
            if is_terminal(opp.state):
                continue
            try:
                self._advance_one(opp, view, now, result)
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "opportunity advance failed for %s", opp.opportunity_id)

    def _poi_anchor_time(self, opp: Opportunity):
        """Formation time of the selected POI, for causal-window auditing.

        `opp.poi_time` is the canonical source, but rows persisted before the
        market-event timestamps existed (or any chain whose poi_time was not
        populated) still carry the selected candidate's `created_time` inside
        `poi_candidates`. Falling back to it keeps the invariant auditable
        across restarts and legacy payloads — a stale pairing must never escape
        termination merely because a convenience field is missing.
        """
        if opp.poi_time:
            return opp.poi_time
        sel = opp.selected_poi
        if sel:
            for c in opp.poi_candidates or []:
                if c.get("poi_id") == sel and c.get("created_time"):
                    return c.get("created_time")
        return None

    def _stale_anchor_reason(self, opp: Opportunity) -> Optional[str]:
        """P1-A: causal timing invariant for the persisted chain.

        Evaluated on MARKET-EVENT timestamps only (never observation time):
          sweep -> CSD within sweep_to_csd_bars
          CSD   -> POI within csd_to_poi_bars   (continuation: BOS -> POI)
        """
        if opp.opportunity_type == Ty.REVERSAL.value:
            sweep_t = opp.sweep_time or opp.sweep_evidence.get("time")
            csd_t = opp.csd_time or opp.csd_evidence.get("time")
            if sweep_t and csd_t:
                gap = pd.Timestamp(csd_t) - pd.Timestamp(sweep_t)
                if gap > pd.Timedelta(minutes=15 * self.windows.sweep_to_csd_bars):
                    return f"CSD {csd_t} beyond sweep->CSD window from {sweep_t}"
            poi_t = self._poi_anchor_time(opp)
            if csd_t and poi_t:
                gap = pd.Timestamp(poi_t) - pd.Timestamp(csd_t)
                if gap > pd.Timedelta(minutes=15 * self.windows.csd_to_poi_bars):
                    return f"POI {poi_t} beyond CSD->POI window from {csd_t}"
        else:
            bos_t = opp.bos_time or opp.bos_evidence.get("time")
            poi_t = self._poi_anchor_time(opp)
            if bos_t and poi_t:
                gap = pd.Timestamp(poi_t) - pd.Timestamp(bos_t)
                if gap > pd.Timedelta(minutes=15 * self.windows.continuation_bos_to_poi_bars):
                    return f"POI {poi_t} beyond BOS->POI window from {bos_t}"
        return None

    def _advance_one(self, opp: Opportunity, view: evaluator.CausalView,
                     now, result: dict) -> None:
        # P1-A: a persisted chain that violates the causal windows is terminated
        # as STALE_ANCHOR (auditable; never silently deleted, never mislabelled
        # as POI_NOT_FOUND/EXPIRED/RISK_REJECTED/INVALIDATED).
        violation = self._stale_anchor_reason(opp)
        if violation:
            self._terminate(opp, St.INVALIDATED, BlockReason.STALE_ANCHOR.value,
                            violation, now, result)
            return
        # observation metadata advances on every cycle; TTL/expiry do NOT.
        opp.last_seen = _iso(now)
        opp.observed_time = _iso(now)
        direction = Direction(opp.direction)
        if opp.opportunity_type == Ty.REVERSAL.value:
            self._advance_reversal(opp, view, direction, now, result)
        else:
            self._advance_continuation(opp, view, direction, now, result)

    def _csd_for(self, opp: Opportunity, view: evaluator.CausalView):
        sid = opp.sweep_evidence.get("id")
        return view.csd_by_sweep.get(sid)

    def _advance_reversal(self, opp: Opportunity, view: evaluator.CausalView,
                          direction: Direction, now, result: dict) -> None:
        csd = self._csd_for(opp, view)
        # opposing CSD (same sweep, opposite direction) cancels
        for other in view.csd_by_sweep.values():
            if (other.direction is not direction
                    and other.candle_time > pd.Timestamp(opp.created_at)):
                if pd.Timestamp(other.candle_time) > pd.Timestamp(opp.sweep_evidence["time"]):
                    self._terminate(opp, St.INVALIDATED, BlockReason.OPPOSING_CSD.value,
                                    "opposing CSD after sweep", now, result)
                    return
        stored_csd_time = opp.csd_evidence.get("time")
        if csd is None and not stored_csd_time:
            # still pre-CSD: only arming/waiting/expiry apply
            if opp.state not in (St.OPPORTUNITY_ARMED.value,
                                 St.WAITING_FOR_CONFIRMATION.value):
                return
            if opp.state == St.OPPORTUNITY_ARMED.value:
                self._transition(opp, St.WAITING_FOR_CONFIRMATION,
                                 BlockReason.WAITING_FOR_CSD.value,
                                 "awaiting CSD confirmation", now, result)
            if opp.expires_at and pd.Timestamp(opp.expires_at) < pd.Timestamp(view.m15_last_time):
                self._terminate(opp, St.EXPIRED, BlockReason.SWEEP_EXPIRED.value,
                                "sweep->CSD window elapsed", now, result)
            return

        # CSD confirmed (freshly detected or previously persisted — progression
        # must survive the event leaving the rolling window)
        if csd is not None and not opp.csd_evidence:
            opp.csd_evidence = {"id": csd.id, "direction": csd.direction.value,
                                "level": float(csd.level), "time": _iso(csd.candle_time)}
            opp.csd_time = _iso(csd.candle_time)
            opp.h4_context = csd.id
            opp.expires_at = _iso(pd.Timestamp(csd.candle_time) + pd.Timedelta(
                minutes=15 * self.windows.csd_to_poi_bars))
            self._transition(opp, St.WAITING_FOR_POI, BlockReason.WAITING_FOR_POI.value,
                             "CSD confirmed; awaiting POI", now, result,
                             event_kind=Ev.OPPORTUNITY_ADVANCED.value,
                             evidence_time=csd.candle_time)
            stored_csd_time = _iso(csd.candle_time)

        # POI discovery (tracked, ranked, rejections preserved)
        if not opp.selected_poi:
            window_end = (pd.Timestamp(stored_csd_time) + pd.Timedelta(
                minutes=15 * self.windows.csd_to_poi_bars)) if stored_csd_time else None
            cands = evaluator.poi_candidates(view, direction, self.windows,
                                             created_after=stored_csd_time,
                                             created_before=window_end)
            opp.poi_candidates = [c.__dict__ for c in cands]
            valid = [c for c in cands if c.rejected_reason is None]
            if not valid:
                opp.blocker = BlockReason.POI_NOT_FOUND.value
                opp.next_expected = "qualifying POI (OB/FVG/D1) within CSD window"
                self.repo.upsert(opp)
                return
            best = valid[0]
            opp.selected_poi = best.poi_id
            opp.poi_time = _iso(best.created_time)
            self.repo.upsert(opp)
            self._emit(opp, Ev.POI_FOUND.value, f"POI:{best.poi_id}",
                       f"{opp.symbol} POI selected {best.poi_id} ({best.kind})", result)

        pathway = opp.entry_pathway
        if pathway == EntryPathway.AGGRESSIVE.value:
            self._to_ready(opp, view, now, result, "aggressive: CSD+POI")
            self._check_entry(opp, view, direction, now, result)
            return

        # IDM requirement (authoritative causal spec requires IDM for the
        # reversal path; aggressive/continuation pathways are documented
        # exceptions). Once confirmed, the reference is persisted so the
        # opportunity keeps progressing after the IDM leaves the rolling window.
        if not opp.idm_reference:
            idm = next((i for i in view.idms if i.order_block_id == opp.selected_poi), None)
            if idm is None:
                if opp.state == St.WAITING_FOR_POI.value:
                    self._transition(opp, St.WAITING_FOR_IDM,
                                     BlockReason.IDM_NOT_CONFIRMED.value,
                                     "awaiting IDM confirmation", now, result)
                opp.blocker = BlockReason.IDM_NOT_CONFIRMED.value
                opp.next_expected = "IDM confirmation"
                self.repo.upsert(opp)
                return
            opp.idm_reference = idm.id
            opp.idm_time = _iso(getattr(idm, "confirmation_time", None)
                                or getattr(idm, "candle_time", None) or now)
            self._emit(opp, Ev.IDM_CONFIRMED.value, f"IDM:{idm.id}",
                       f"{opp.symbol} IDM confirmed {idm.id}", result)

        if pathway == EntryPathway.SMART.value:
            poi = next((c for c in opp.poi_candidates
                        if c.get("poi_id") == opp.selected_poi), None)
            if poi is None:
                return
            # SMART policy: require proximity to the selected POI before READY
            last = view.m15_frame["close"].iloc[-1] if view.m15_frame is not None \
                and len(view.m15_frame) else None
            atr = view.m15_atr or 0.0
            if last is None or not atr:
                return
            edge = float(poi["high"]) if direction is Direction.BULLISH else float(poi["low"])
            if abs(float(last) - edge) > self.smart_max_distance_atr * atr:
                opp.blocker = BlockReason.WAITING_FOR_POI.value
                opp.next_expected = "price approaching selected POI (smart pathway)"
                self.repo.upsert(opp)
                return
            self._to_ready(opp, view, now, result, "smart: POI+IDM+near")
        else:
            self._to_ready(opp, view, now, result, "pullback: POI+IDM")
        self._check_entry(opp, view, direction, now, result)

    def _advance_continuation(self, opp: Opportunity, view: evaluator.CausalView,
                              direction: Direction, now, result: dict) -> None:
        bos_time = opp.bos_evidence.get("time")
        if not opp.selected_poi:
            window_end = (pd.Timestamp(bos_time) + pd.Timedelta(
                minutes=15 * self.windows.continuation_bos_to_poi_bars)) if bos_time else None
            cands = evaluator.poi_candidates(view, direction, self.windows,
                                             created_after=bos_time,
                                             created_before=window_end)
            opp.poi_candidates = [c.__dict__ for c in cands]
            valid = [c for c in cands if c.rejected_reason is None]
            if not valid:
                if opp.state == St.OPPORTUNITY_ARMED.value:
                    self._transition(opp, St.WAITING_FOR_POI,
                                     BlockReason.POI_NOT_FOUND.value,
                                     "BOS confirmed; awaiting new POI", now, result)
                opp.blocker = BlockReason.POI_NOT_FOUND.value
                opp.next_expected = "new directionally valid POI after BOS"
                self.repo.upsert(opp)
                return
            best = valid[0]
            opp.selected_poi = best.poi_id
            opp.poi_time = _iso(best.created_time)
            self.repo.upsert(opp)
            self._emit(opp, Ev.POI_FOUND.value, f"POI:{best.poi_id}",
                       f"{opp.symbol} continuation POI {best.poi_id}", result)
        self._to_ready(opp, view, now, result, "continuation: BOS+POI")
        self._check_entry(opp, view, direction, now, result)
    def _to_ready(self, opp: Opportunity, view: evaluator.CausalView, now,
                  result: dict, reason: str) -> None:
        if opp.state != St.READY_FOR_MITIGATION.value:
            self._transition(opp, St.READY_FOR_MITIGATION,
                             BlockReason.READY_FOR_MITIGATION.value, reason, now, result,
                             event_kind=Ev.READY.value,
                             evidence_time=(opp.poi_time
                                            or opp.csd_evidence.get("time")
                                            or opp.bos_evidence.get("time")))
            # P1-A: READY lifetime is anchored to the market event that produced
            # readiness (POI/IDM/CSD/BOS), NOT to the observation time. Repeated
            # polling or a restart must not rejuvenate an old opportunity.
            anchor = max(filter(None, [opp.idm_time, opp.poi_time, opp.csd_time,
                                       opp.bos_time]),
                         default=pd.Timestamp(now))
            opp.expires_at = _iso(pd.Timestamp(anchor) + pd.Timedelta(
                minutes=15 * self.windows.ready_ttl_bars))
        opp.blocker = ""
        opp.next_expected = "POI mitigation / entry"
        self.repo.upsert(opp)

    def _check_entry(self, opp: Opportunity, view: evaluator.CausalView,
                     direction: Direction, now, result: dict) -> None:
        poi = next((c for c in opp.poi_candidates
                    if c.get("poi_id") == opp.selected_poi), None)
        if not poi:
            return
        lo, hi = float(poi["low"]), float(poi["high"])
        # Only bars strictly AFTER the confirmation event AND after the POI's
        # formation window count as mitigation. The candle(s) that created the
        # POI — including the displacement that spans the zone — are not entry
        # touches (opportunity-layer policy, applied on top of causal truth).
        anchor_time = (opp.csd_evidence.get("time") or opp.bos_evidence.get("time")
                       or opp.created_at)
        base = max(pd.Timestamp(anchor_time), pd.Timestamp(poi["created_time"]))
        if poi.get("kind") in ("OB", "FVG"):
            base = base + pd.Timedelta(minutes=15)
        df = self._bars_after(view, base)
        if df is None or not len(df):
            self.repo.upsert(opp)
            return
        touched = bool(((df["low"] <= hi) & (df["high"] >= lo)).any())
        if touched:
            self._terminate(opp, St.ENTRY_TRIGGERED, BlockReason.ENTRY_PASSED.value,
                            "entry mitigation reached", now, result,
                            event_kind=Ev.ENTRY_TRIGGERED.value)
        else:
            self.repo.upsert(opp)

    @staticmethod
    def _bars_after(view: evaluator.CausalView, time_value):
        df = view.m15_frame
        if df is None or not len(df):
            return None
        return df[pd.to_datetime(df["time"], utc=True) > pd.Timestamp(time_value)]

    # ------------------------------------------------------------ promotion
    def _promote(self, symbol: str, view: evaluator.CausalView, result: dict) -> None:
        """Link opportunities to setups produced by the authoritative causal
        engine. Never creates a TradeSetup here."""
        for cand in view.candidates:
            setup = cand.setup
            for opp in self.repo.query(symbol=symbol, active_only=True, limit=1000):
                if opp.setup_id == setup.id:
                    continue
                matches = ((opp.sweep_evidence.get("id") == setup.sweep_id)
                           or (opp.selected_poi == setup.order_block_id))
                if not matches:
                    continue
                opp.setup_id = setup.id
                opp.lifecycle_status = "SETUP_REGISTERED"
                self.repo.upsert(opp)
                if self.repo.record_event_once(opp.opportunity_id,
                                               Ev.CONVERTED_TO_SETUP.value,
                                               f"SETUP:{setup.id}",
                                               Ev.CONVERTED_TO_SETUP.value,
                                               f"{symbol} opportunity -> setup {setup.id}"):
                    result["converted"].append(opp.opportunity_id)
                    result["events"].append({"opportunity_id": opp.opportunity_id,
                                             "kind": Ev.CONVERTED_TO_SETUP.value,
                                             "message": f"opportunity converted to setup {setup.id}",
                                             "symbol": symbol, "state": opp.state,
                                             "event_id": f"SETUP:{setup.id}"})
                    self._diag["alerts_emitted"] += 1
                # EXECUTION_READY is emitted ONLY now — i.e. only once a real
                # TradeSetup promotion has occurred (P1-C).
                if self.repo.record_event_once(opp.opportunity_id,
                                               Ev.EXECUTION_READY.value,
                                               f"SETUP:{setup.id}",
                                               Ev.EXECUTION_READY.value,
                                               f"{symbol} setup {setup.id} execution-ready"):
                    result["events"].append({"opportunity_id": opp.opportunity_id,
                                             "kind": Ev.EXECUTION_READY.value,
                                             "message": f"setup {setup.id} execution-ready",
                                             "symbol": symbol, "state": opp.state,
                                             "event_id": f"SETUP:{setup.id}"})
                    self._diag["alerts_emitted"] += 1
                break

    # ------------------------------------------------------- arbitration helpers
    def _supersede_opp(self, opp_id: str, winner_key: str, reason: str,
                       now, result: dict) -> None:
        """Transition an existing opportunity to SUPERSEDED state."""
        opp = self.repo.get(opp_id)
        if opp is None or opp.state == St.SUPERSEDED.value:
            return
        try:
            assert_transition(opp.state, St.SUPERSEDED.value)
        except Exception:
            return
        previous = opp.state
        opp.state = St.SUPERSEDED.value
        opp.superseded_by = winner_key
        opp.superseded_at = _iso(now)
        opp.supersession_reason = reason
        opp.reason = BlockReason.SUPERSEDED_THESIS.value
        opp.blocker = BlockReason.SUPERSEDED_THESIS.value
        opp.updated_at = _iso(now)
        self.repo.upsert(opp)
        self.repo.record_state_change(opp.opportunity_id, previous,
                                      St.SUPERSEDED.value,
                                      BlockReason.SUPERSEDED_THESIS.value, reason)
        self._emit(opp, Ev.OPPORTUNITY_SUPERSEDED.value,
                   f"superseded:{winner_key}",
                   f"{opp.symbol} {opp.direction} {opp.opportunity_type} superseded by "
                   f"{winner_key}", result)
        result["superseded"].append(opp_id)
        self.arbiter.opportunities_superseded += 1

    def _reconcile_startup(self, result: dict) -> None:
        """Reconcile pre-existing conflicts from previous sessions."""
        now = _now()
        pairs = self.arbiter.reconcile(_iso(now))
        for loser_id, winner_id in pairs:
            self._supersede_opp(loser_id, winner_id,
                                "startup reconciliation — conflict pre-dates this session",
                                now, result)

    # ------------------------------------------------------------- termination
    def _expire_symbol(self, symbol: str, now, result: dict) -> None:
        for opp in self.repo.query(symbol=symbol, active_only=True, limit=1000):
            if opp.expires_at and pd.Timestamp(opp.expires_at) < pd.Timestamp(now):
                self._terminate(opp, St.EXPIRED, BlockReason.EXPIRED_TTL.value,
                                "TTL elapsed", now, result)

    def _terminate(self, opp: Opportunity, state: St, reason: str, detail: str,
                   now, result: dict, event_kind: Optional[str] = None) -> None:
        if opp.state == state.value:
            return
        try:
            assert_transition(opp.state, state.value)
        except Exception:
            return
        previous = opp.state
        opp.state = state.value
        opp.reason = reason
        opp.blocker = reason
        opp.updated_at = _iso(now)
        opp.observed_time = _iso(now)
        self.repo.upsert(opp)
        self.repo.record_state_change(opp.opportunity_id, previous, state.value,
                                      reason, detail)
        kind = event_kind or (Ev.INVALIDATED.value if state is St.INVALIDATED
                              else Ev.EXPIRED.value if state is St.EXPIRED
                              else Ev.OPPORTUNITY_ADVANCED.value)
        self._emit(opp, kind, self._event_identity(opp, reason),
                   f"{opp.symbol} {opp.direction} {opp.opportunity_type} -> "
                   f"{state.value} ({reason})", result)
        bucket = ("invalidated" if state is St.INVALIDATED
                  else "expired" if state is St.EXPIRED else "advanced")
        result[bucket].append(opp.opportunity_id)

    def _transition(self, opp: Opportunity, target: St, reason: str, detail: str,
                    now, result: dict, event_kind: Optional[str] = None,
                    evidence_time=None) -> None:
        try:
            assert_transition(opp.state, target.value)
        except Exception:
            return
        previous = opp.state
        opp.state = target.value
        opp.reason = reason
        opp.updated_at = _iso(now)
        opp.last_seen = _iso(now)
        opp.observed_time = _iso(now)
        self.repo.upsert(opp)
        self.repo.record_state_change(opp.opportunity_id, previous, target.value,
                                      reason, detail)
        self._emit(opp, event_kind or Ev.OPPORTUNITY_ADVANCED.value,
                   f"{reason}:{_ns(evidence_time or self._event_anchor(opp))}",
                   f"{opp.symbol} {target.value} ({detail})", result)
        result["advanced"].append(opp.opportunity_id)
        self._diag["opportunities_advanced"] += 1

    # ------------------------------------------------------------------ events
    @staticmethod
    def _event_anchor(opp: Opportunity):
        """Stable market-event anchor for a logical event (never wall-clock)."""
        return (opp.poi_time or opp.idm_time or opp.csd_time or opp.bos_time
                or opp.sweep_time or opp.created_at)

    @classmethod
    def _event_identity(cls, opp: Opportunity, reason: str) -> str:
        """Stable logical-event identity.

        Repeated scans must yield the SAME identity for the same logical event
        so that ``record_event_once`` deduplicates durably across restarts. The
        identity is derived from the market-event anchor, never the observation
        clock — otherwise every scan mints a new identity and the same event is
        recorded (and therefore re-notified) again.
        """
        return f"{reason}:{_ns(cls._event_anchor(opp))}"

    def _emit(self, opp: Opportunity, kind: str, evidence_key: str, message: str,
              result: dict) -> None:
        """Persist-once event; only genuinely new transitions surface."""
        fresh = self.repo.record_event_once(opp.opportunity_id, kind, evidence_key,
                                            kind, message)
        if fresh:
            self._diag["alerts_emitted"] += 1
            result["events"].append({"opportunity_id": opp.opportunity_id,
                                     "kind": kind, "message": message,
                                     "symbol": opp.symbol, "state": opp.state,
                                     "event_id": evidence_key})
        else:
            self._diag["alerts_deduplicated"] += 1

    # ------------------------------------------------------------- diagnostics
    def funnel(self) -> dict:
        counts = self.repo.state_counts()
        all_opps = self.repo.query(limit=1000)
        converted = sum(1 for o in all_opps if o.setup_id)
        return {
            "opportunities_armed": counts.get(St.OPPORTUNITY_ARMED.value, 0),
            "waiting_for_confirmation": counts.get(St.WAITING_FOR_CONFIRMATION.value, 0),
            "waiting_for_poi": counts.get(St.WAITING_FOR_POI.value, 0),
            "waiting_for_idm": counts.get(St.WAITING_FOR_IDM.value, 0),
            "ready_opportunity_count": counts.get(St.READY_FOR_MITIGATION.value, 0),
            "execution_ready_count": counts.get(St.ENTRY_TRIGGERED.value, 0),
            "invalidated_count": counts.get(St.INVALIDATED.value, 0),
            "expired_count": counts.get(St.EXPIRED.value, 0),
            "superseded_count": counts.get(St.SUPERSEDED.value, 0),
            "terminal_count": counts.get(St.TERMINAL.value, 0),
            "converted_to_setup": converted,
            "total": sum(counts.values()),
        }

    def diagnostics(self) -> dict:
        d = dict(self._diag)
        d["funnel"] = self.funnel()
        d["windows"] = self.windows.as_dict()
        d["pathways"] = list(self.pathways)
        d["arbitration"] = self.arbiter.diagnostics()
        return d

    def audit(self, symbol: str) -> dict:
        """Why is this symbol currently showing no execution-ready setup?"""
        view = self._last_view.get(symbol)
        active = self.repo.query(symbol=symbol, active_only=True, limit=100)
        states = [o.state for o in active]
        if view is None:
            return {"symbol": symbol, "classification": "NOT_SCANNED",
                    "active_opportunities": len(active)}
        return {
            "symbol": symbol,
            "classification": evaluator.audit_classification(view, states),
            "active_opportunities": len(active),
            "states": states,
            "sweeps": len(view.sweeps),
            "csd_confirmed": len(view.csd_by_sweep),
            "poi_candidates": len(view.m15_obs) + len(view.m15_fvgs) + len(view.d1_pois),
            "idms": len(view.idms),
            "candidates": len(view.candidates),
            "as_of": _iso(view.as_of) if view.as_of is not None else None,
        }
