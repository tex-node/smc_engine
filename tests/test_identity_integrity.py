"""Phase 16E — canonical opportunity identity, terminal immutability, and
lifecycle-event consistency regressions.

These tests use disposable SQLite databases only. They reproduce the production
defect (GBPAUD `EXPIRED → OPPORTUNITY_ARMED → WAITING_FOR_CONFIRMATION` via a
canonical-key/opportunity-id collision) and assert the hardened invariants:

  * an opportunity_id refers to exactly ONE canonical identity;
  * a terminal row cannot be silently reactivated by an upsert / re-discovery;
  * a real transition records its history + event atomically; dedup only
    suppresses a replay of the SAME transition.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from src.smc_engine.opportunity.engine import OpportunityEngine
from src.smc_engine.opportunity.models import (Opportunity, OpportunityState as St,
                                               OpportunityType as Ty,
                                               canonical_opportunity_key,
                                               opportunity_id_from_key)
from src.smc_engine.opportunity.repository import (OpportunityIdentityConflict,
                                                   OpportunityRepository,
                                                   TerminalStateImmutable)

ANCHOR = "2026-10-09 04:00"           # = ns 1791518400000000000
GBPAUD_LEVEL = 1.89623
GBPAUD_DRIFT = 1.89628
GBPAUD_ID = "OPP-REVERSAL-GBPAUD-SWEEP-1791518400000000000-BULLISH"


def _res() -> dict:
    return {"created": [], "advanced": [], "invalidated": [], "expired": [],
            "converted": [], "events": [], "blocked": [], "superseded": []}


def _key(symbol="GBPAUD", level=GBPAUD_LEVEL, anchor=ANCHOR, otype=Ty.REVERSAL.value,
         direction="BULLISH", anchor_kind="SWEEP"):
    return canonical_opportunity_key(symbol, direction, otype, anchor_kind,
                                     pd.Timestamp(anchor, tz="UTC"), level)


def _mk(symbol="GBPAUD", level=GBPAUD_LEVEL, anchor=ANCHOR, state=St.OPPORTUNITY_ARMED.value,
        expires="2026-10-10 04:00", key=None, direction="BULLISH",
        otype=Ty.REVERSAL.value, anchor_kind="SWEEP"):
    k = key or _key(symbol=symbol, level=level, anchor=anchor, otype=otype,
                    direction=direction, anchor_kind=anchor_kind)
    return Opportunity(
        opportunity_id=opportunity_id_from_key(k), canonical_key=k, symbol=symbol,
        direction=direction, opportunity_type=otype, state=state,
        created_at=f"{anchor}+00:00", updated_at=f"{anchor}+00:00",
        expires_at=f"{expires}+00:00", sweep_time=f"{anchor}+00:00",
        sweep_evidence={"id": "SW-X", "time": f"{anchor}+00:00"})


def _repo(tmp_path, name="r.db") -> OpportunityRepository:
    return OpportunityRepository(str(tmp_path / name))


# ── 1/2/5. identity collision (level-only difference) is refused ─────────────

def test_identity_collision_is_refused_and_original_untouched(tmp_path):
    repo = _repo(tmp_path)
    a = _mk(level=GBPAUD_LEVEL, state=St.READY_FOR_MITIGATION.value)
    repo.upsert(a)
    before = repo.get(a.opportunity_id)

    b = _mk(level=GBPAUD_DRIFT, state=St.OPPORTUNITY_ARMED.value)   # same ns, drifted level
    assert b.opportunity_id == a.opportunity_id, "levels must derive the same legacy id"
    assert b.canonical_key != a.canonical_key

    with pytest.raises(OpportunityIdentityConflict):
        repo.upsert(b)

    after = repo.get(a.opportunity_id)                              # (#7) unchanged
    assert after.canonical_key == before.canonical_key
    assert after.state == before.state
    assert after.updated_at == before.updated_at
    assert after.observed_time == before.observed_time
    assert any(d["kind"] == "IDENTITY_COLLISION" for d in repo.identity_diagnostics())


def test_gbpaud_collision_scenario_fixture(tmp_path):
    """The exact production fixture: indexed key ends 1.89623, payload 1.89628."""
    repo = _repo(tmp_path)
    stored = _mk(level=GBPAUD_LEVEL, state=St.EXPIRED.value)
    assert stored.opportunity_id == GBPAUD_ID
    repo.upsert(stored)

    redisc = _mk(level=GBPAUD_DRIFT, state=St.OPPORTUNITY_ARMED.value)
    with pytest.raises(OpportunityIdentityConflict):
        repo.upsert(redisc)

    row = repo.get(GBPAUD_ID)
    assert row.state == St.EXPIRED.value, "the terminal GBPAUD row must not be resurrected"


# ── 3/4/11. terminal immutability ────────────────────────────────────────────

@pytest.mark.parametrize("terminal", [St.EXPIRED.value, St.INVALIDATED.value,
                                      St.SUPERSEDED.value, St.ENTRY_TRIGGERED.value,
                                      St.TERMINAL.value])
def test_terminal_rows_cannot_be_reactivated(tmp_path, terminal):
    repo = _repo(tmp_path, name=f"{terminal}.db")
    a = _mk(state=terminal)
    repo.upsert(a)
    active = _mk(state=St.WAITING_FOR_CONFIRMATION.value)
    with pytest.raises(TerminalStateImmutable):
        repo.upsert(active)
    assert repo.get(a.opportunity_id).state == terminal


def test_arm_cannot_reactivate_a_terminal_row(tmp_path):
    """The exact resurrection path: `_arm` must refuse and return None."""
    repo = _repo(tmp_path)
    a = _mk(state=St.EXPIRED.value)
    repo.upsert(a)
    eng = OpportunityEngine(repo)
    out = eng._arm(a.canonical_key, "GBPAUD", "BULLISH", Ty.REVERSAL.value, "SWEEP",
                   pd.Timestamp(ANCHOR, tz="UTC"), GBPAUD_LEVEL, {"id": "SW-Y"},
                   "PULLBACK", pd.Timestamp("2026-10-11 13:21:30", tz="UTC"), _res())
    assert out is None
    assert repo.get(a.opportunity_id).state == St.EXPIRED.value


def test_repeated_discovery_does_not_reactivate_via_id_collision(tmp_path):
    """A later scan that re-observes a drifted-level sweep must NOT re-arm the
    persisted terminal row (id collision path), and must surface a diagnostic."""
    repo = _repo(tmp_path)
    persisted = _mk(level=GBPAUD_LEVEL, state=St.EXPIRED.value)
    repo.upsert(persisted)
    eng = OpportunityEngine(repo)

    sweep = SimpleNamespace(id="SW-1", candle_time=pd.Timestamp(ANCHOR, tz="UTC"),
                            side=SimpleNamespace(value="SELL_SIDE"),
                            swept_level=GBPAUD_DRIFT, sweep_extreme=1.8)
    view = SimpleNamespace(sweeps=[sweep], m15_last_time=pd.Timestamp("2026-10-09 20:30", tz="UTC"),
                           csd_by_sweep={}, breaks=[])
    res = _res()
    eng._discover_reversal("GBPAUD", view, pd.Timestamp("2026-10-11 13:21:30", tz="UTC"), res)

    assert repo.get(GBPAUD_ID).state == St.EXPIRED.value
    assert res["created"] == []                       # nothing (re)armed
    assert any(b.get("reason") == "identity_collision" for b in res["blocked"])


# ── 6. row identity and payload identity stay consistent ─────────────────────

def test_row_key_and_payload_key_stay_consistent_after_transition(tmp_path):
    repo = _repo(tmp_path)
    a = _mk(state=St.OPPORTUNITY_ARMED.value)
    repo.upsert(a)
    OpportunityEngine(repo)._terminate(a, St.EXPIRED, "EXPIRED_TTL", "TTL elapsed",
                                       pd.Timestamp("2026-10-11 05:54", tz="UTC"), _res())
    col_key, payload = repo._conn.execute(
        "SELECT canonical_key, payload_json FROM opportunities WHERE opportunity_id=?",
        (a.opportunity_id,)).fetchone()
    assert col_key == json.loads(payload)["canonical_key"]


# ── 8/9/10. atomic transition + event consistency ────────────────────────────

def test_transition_records_history_and_event_atomically(tmp_path):
    repo = _repo(tmp_path)
    a = _mk(state=St.OPPORTUNITY_ARMED.value)
    repo.upsert(a)
    eng = OpportunityEngine(repo)
    eng._terminate(a, St.EXPIRED, "EXPIRED_TTL", "TTL elapsed",
                   pd.Timestamp("2026-10-10 12:00", tz="UTC"), _res())
    assert repo.get(a.opportunity_id).state == St.EXPIRED.value
    ev = repo._conn.execute("SELECT COUNT(*) FROM opportunity_events WHERE opportunity_id=? AND kind='EXPIRED'",
                            (a.opportunity_id,)).fetchone()[0]
    hist = repo._conn.execute("SELECT to_state FROM opportunity_state_history WHERE opportunity_id=? ORDER BY id",
                              (a.opportunity_id,)).fetchall()
    assert ev == 1
    assert hist and hist[-1][0] == St.EXPIRED.value


def test_duplicate_transition_event_is_suppressed(tmp_path):
    repo = _repo(tmp_path)
    a = _mk(state=St.OPPORTUNITY_ARMED.value)
    repo.upsert(a)
    opp = repo.get(a.opportunity_id)
    assert repo.apply_transition(opp, St.OPPORTUNITY_ARMED.value, St.EXPIRED.value,
                                 "EXPIRED_TTL", "d", "EXPIRED", "EXPIRED_TTL:1", "m") is True
    again = repo.get(a.opportunity_id)                       # reload; now EXPIRED
    fresh = repo.apply_transition(again, St.OPPORTUNITY_ARMED.value, St.EXPIRED.value,
                                  "EXPIRED_TTL", "d", "EXPIRED", "EXPIRED_TTL:1", "m")
    assert fresh is False
    count = repo._conn.execute("SELECT COUNT(*) FROM opportunity_events WHERE opportunity_id=?",
                               (a.opportunity_id,)).fetchone()[0]
    assert count == 1


def test_distinct_transition_not_suppressed_by_reused_evidence_key(tmp_path):
    repo = _repo(tmp_path)
    a = _mk(state=St.OPPORTUNITY_ARMED.value)
    repo.upsert(a)
    opp = repo.get(a.opportunity_id)
    assert repo.apply_transition(opp, St.OPPORTUNITY_ARMED.value, St.WAITING_FOR_CONFIRMATION.value,
                                 "WAITING_FOR_CSD", "d", "OPPORTUNITY_ADVANCED", "REUSED:1", "m") is True
    opp2 = repo.get(a.opportunity_id)
    # a genuinely DIFFERENT transition reusing the same evidence_key must record
    assert repo.apply_transition(opp2, St.WAITING_FOR_CONFIRMATION.value, St.WAITING_FOR_POI.value,
                                 "POI", "d", "OPPORTUNITY_ADVANCED", "REUSED:1", "m") is True
    count = repo._conn.execute("SELECT COUNT(*) FROM opportunity_events WHERE opportunity_id=?",
                               (a.opportunity_id,)).fetchone()[0]
    assert count == 2


def test_unchanged_state_records_nothing(tmp_path):
    repo = _repo(tmp_path)
    a = _mk(state=St.READY_FOR_MITIGATION.value)
    repo.upsert(a)
    opp = repo.get(a.opportunity_id)
    assert repo.apply_transition(opp, St.READY_FOR_MITIGATION.value, St.READY_FOR_MITIGATION.value,
                                 "noop", "d", "OPPORTUNITY_ADVANCED", "X", "m") is False
    assert repo._conn.execute("SELECT COUNT(*) FROM opportunity_state_history WHERE opportunity_id=?",
                              (a.opportunity_id,)).fetchone()[0] == 0


# ── 13. market-time expiry still correct (through the atomic boundary) ───────

def test_per_symbol_market_time_expiry_still_records_event(tmp_path):
    repo = _repo(tmp_path)
    a = _mk(state=St.READY_FOR_MITIGATION.value, expires="2026-10-10 12:00")
    repo.upsert(a)
    eng = OpportunityEngine(repo)
    res = _res()
    eng._expire_symbol("GBPAUD", pd.Timestamp("2026-10-10 11:00", tz="UTC"), res)
    assert repo.get(a.opportunity_id).state == St.READY_FOR_MITIGATION.value   # before TTL
    eng._expire_symbol("GBPAUD", pd.Timestamp("2026-10-10 13:00", tz="UTC"), res)
    assert repo.get(a.opportunity_id).state == St.EXPIRED.value               # genuine
    assert any(e["kind"] == "EXPIRED" for e in res["events"])


# ── 14. concurrency cannot bypass the invariants ─────────────────────────────

def test_concurrent_upserts_cannot_bypass_identity_or_terminal_protection(tmp_path):
    repo = _repo(tmp_path)
    a = _mk(state=St.EXPIRED.value)
    repo.upsert(a)

    outcomes = {"conflict": 0, "immutable": 0, "ok": 0}

    def worker(i):
        b = _mk(level=GBPAUD_DRIFT if i % 2 else GBPAUD_LEVEL,
                state=St.OPPORTUNITY_ARMED.value)
        try:
            repo.upsert(b)
            outcomes["ok"] += 1
        except OpportunityIdentityConflict:
            outcomes["conflict"] += 1
        except TerminalStateImmutable:
            outcomes["immutable"] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert repo.get(a.opportunity_id).state == St.EXPIRED.value
    assert outcomes["ok"] == 0
    assert outcomes["conflict"] + outcomes["immutable"] == 12
