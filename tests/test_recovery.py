"""Phase 16 — Workstream A: opportunity recovery reliability.

Root-cause regression: the poller's TTL sweep (`expire_cycle`) must be anchored
to market-event time and must NEVER fall back to the wall clock. Previously,
`scan_universe_once` passed `now=data_now`, and when no symbol produced data
(closed market / closed-M15 gate / MT5 not ready) `data_now` was None, so
`expire_cycle` used `_now()` — silently expiring persisted, still-monitored
opportunities by wall-clock time. This made "restart / closed market" an
opportunity-expiry reason.

Also covers: restart rehydration, DB-identity diagnostics, and idempotent
startup reconciliation.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.smc_engine.opportunity.models import (Opportunity, OpportunityState as St,
                                               OpportunityType as Ty,
                                               canonical_opportunity_key,
                                               opportunity_id_from_key)
from src.smc_engine.web.hub import EngineHub
from tests.test_api import FakeDemoSource


def _hub(tmp_path, name="g.db", source=None):
    h = EngineHub(source or FakeDemoSource(), db_path=str(tmp_path / name),
                  setup_store_path=str(tmp_path / ("s_" + name)))
    h.stop_poller()
    return h


def _ready_opp(symbol="GBPUSD", anchor="2026-10-09 12:00", expires="2026-10-10 12:00"):
    key = canonical_opportunity_key(symbol, "BULLISH", Ty.REVERSAL.value, "SWEEP",
                                    pd.Timestamp(anchor, tz="UTC"), 1.0)
    return Opportunity(
        opportunity_id=opportunity_id_from_key(key), canonical_key=key,
        symbol=symbol, direction="BULLISH", opportunity_type=Ty.REVERSAL.value,
        state=St.READY_FOR_MITIGATION.value,
        created_at=f"{anchor}+00:00", updated_at=f"{anchor}+00:00",
        expires_at=f"{expires}+00:00",
        sweep_time=f"{anchor}+00:00", sweep_evidence={"id": "SW-X", "time": f"{anchor}+00:00"},
    )


def test_closed_market_does_not_wall_clock_expire(tmp_path):
    """No market data this cycle -> the sweep must NOT run at all.

    The persisted opportunity's market-anchored expiry is in the past relative
    to the wall clock but the market has not advanced (closed market), so it
    must remain active.
    """
    hub = _hub(tmp_path)                       # source exposes no symbols -> data_now None
    opp = _ready_opp()                         # expires 2026-10-10 (< wall clock today)
    hub.opportunities.repo.upsert(opp)
    hub.scan_universe_once(force=True)         # no market data -> must not sweep
    row = hub.opportunities.repo.get(opp.opportunity_id)
    assert row.state == St.READY_FOR_MITIGATION.value, \
        "a closed market / no-data cycle must not expire an opportunity by wall clock"
    hub.stop_poller()


def test_market_time_anchors_expiry_not_wall_clock(tmp_path):
    """An explicit market timestamp earlier than expiry keeps the opp active."""
    hub = _hub(tmp_path)
    opp = _ready_opp(expires="2026-10-10 12:00")
    hub.opportunities.repo.upsert(opp)
    # market data time 2026-10-09 12:00 < expires_at -> not expired
    hub.opportunities.expire_cycle(now=pd.Timestamp("2026-10-09 12:00", tz="UTC"))
    assert hub.opportunities.repo.get(opp.opportunity_id).state == St.READY_FOR_MITIGATION.value
    # market data time past expiry -> expired (legitimate)
    hub.opportunities.expire_cycle(now=pd.Timestamp("2026-10-10 12:01", tz="UTC"))
    assert hub.opportunities.repo.get(opp.opportunity_id).state == St.EXPIRED.value
    hub.stop_poller()


def test_restart_recovers_active_opportunity(tmp_path):
    """Persisted active state is authoritative across a restart (same DB)."""
    hub1 = _hub(tmp_path, name="g.db")
    opp = _ready_opp()
    hub1.opportunities.repo.upsert(opp)
    hub1.stop_poller()
    hub2 = _hub(tmp_path, name="g.db")          # fresh instance, same DB
    row = hub2.opportunities.repo.get(opp.opportunity_id)
    assert row is not None and row.state == St.READY_FOR_MITIGATION.value
    assert hub2._recovery_counts_at_startup.get(St.READY_FOR_MITIGATION.value, 0) >= 1
    hub2.stop_poller()


def test_recovery_from_other_working_directory(tmp_path):
    """A different cwd is irrelevant when the DB path is canonical/absolute."""
    import os
    db = str(tmp_path / "g.db")
    hub1 = EngineHub(FakeDemoSource(), db_path=db, setup_store_path=str(tmp_path / "s.db"))
    hub1.stop_poller()
    opp = _ready_opp()
    hub1.opportunities.repo.upsert(opp)
    cwd = os.getcwd()
    other = tmp_path / "othercwd"
    other.mkdir()
    os.chdir(str(other))
    try:
        hub2 = EngineHub(FakeDemoSource(), db_path=db, setup_store_path=str(tmp_path / "s.db"))
        hub2.stop_poller()
        assert hub2.opportunities.repo.get(opp.opportunity_id).state == St.READY_FOR_MITIGATION.value
    finally:
        os.chdir(cwd)
    hub1.stop_poller()


def test_missing_db_flags_empty_initialization(tmp_path):
    """A non-existent DB is surfaced, not silently healthy."""
    hub = _hub(tmp_path, name="fresh.db")
    rec = hub.runtime_info()["recovery"]
    assert rec["db_existed_at_startup"] is False
    assert rec["db_initialized_empty"] is True
    assert rec["db_path_basename"] == "fresh.db"
    hub.stop_poller()


def test_existing_db_reported_as_recovered(tmp_path):
    db = str(tmp_path / "g.db")
    h1 = EngineHub(FakeDemoSource(), db_path=db, setup_store_path=str(tmp_path / "s.db"))
    h1.stop_poller()
    h2 = EngineHub(FakeDemoSource(), db_path=db, setup_store_path=str(tmp_path / "s.db"))
    h2.stop_poller()
    rec = h2.runtime_info()["recovery"]
    assert rec["db_existed_at_startup"] is True
    assert rec["db_initialized_empty"] is False


def test_startup_reconciliation_is_idempotent(tmp_path):
    """Repeated restarts do not create endless supersession/expiry events."""
    db = str(tmp_path / "g.db")
    counts = []
    for _ in range(3):
        h = EngineHub(FakeDemoSource(), db_path=db, setup_store_path=str(tmp_path / "s.db"))
        h.stop_poller()
        counts.append(h.opportunities.repo.state_counts().get(St.SUPERSEDED.value, 0))
    assert counts[0] == counts[-1], "supersession count must be stable across restarts"


def test_no_global_sweep_expires_a_closed_instrument(tmp_path):
    """A trading instrument must not advance the expiry clock of a closed one.

    GBPUSD has fresh data; XAUUSD's opportunity has a TTL that expired before
    GBPUSD's last closed M15, but XAUUSD is not being scanned (its market is
    closed). It must remain active — expiry is per-symbol, not global.
    """
    from tests.test_opportunity_watcher import prime_timeline
    src = FakeDemoSource()
    prime_timeline(src, "GBPUSD", cut="2026-01-26 01:00")   # last M15 = 2026-01-26 01:00
    hub = EngineHub(src, db_path=str(tmp_path / "g.db"),
                    setup_store_path=str(tmp_path / "s.db"))
    hub.stop_poller()
    opp = _ready_opp(symbol="XAUUSD", anchor="2026-01-20 00:00", expires="2026-01-21 00:00")
    hub.opportunities.repo.upsert(opp)
    hub.scan_universe_once(force=True)
    row = hub.opportunities.repo.get(opp.opportunity_id)
    assert row.state == St.READY_FOR_MITIGATION.value, \
        "a trading instrument must not expire a closed instrument's opportunity"
    hub.stop_poller()


def test_per_symbol_expiry_uses_that_symbols_market_time(tmp_path):
    """Expiry compares a symbol's TTL against that symbol's own market time."""
    from src.smc_engine.opportunity import OpportunityEngine
    from src.smc_engine.opportunity.repository import OpportunityRepository
    repo = OpportunityRepository(str(tmp_path / "r.db"))
    eng = OpportunityEngine(repo)
    opp = _ready_opp(symbol="GBPUSD", expires="2026-10-10 12:00")
    repo.upsert(opp)
    res = {"expired": [], "events": [], "created": [], "advanced": [],
           "invalidated": [], "converted": [], "blocked": [], "superseded": []}
    # market time before expiry -> not expired
    eng._expire_symbol("GBPUSD", pd.Timestamp("2026-10-10 11:00", tz="UTC"), res)
    assert repo.get(opp.opportunity_id).state == St.READY_FOR_MITIGATION.value
    # fresh valid data past the TTL -> expired (legitimate)
    eng._expire_symbol("GBPUSD", pd.Timestamp("2026-10-10 13:00", tz="UTC"), res)
    assert repo.get(opp.opportunity_id).state == St.EXPIRED.value


def test_abrupt_termination_preserves_opportunity_state(tmp_path):
    """A killed (not cleanly shut down) process must not lose committed state."""
    import subprocess
    import sys
    import textwrap
    db = str(tmp_path / "crash.db")
    script = textwrap.dedent(r'''
        import sys, time
        sys.path.insert(0, r"C:\smc_engine")
        import pandas as pd
        from src.smc_engine.opportunity.repository import OpportunityRepository
        from src.smc_engine.opportunity.models import (Opportunity, OpportunityState as St,
            OpportunityType as Ty, canonical_opportunity_key, opportunity_id_from_key)
        key = canonical_opportunity_key("GBPUSD", "BULLISH", Ty.REVERSAL.value, "SWEEP",
                                        pd.Timestamp("2026-10-09 12:00", tz="UTC"), 1.0)
        opp = Opportunity(opportunity_id=opportunity_id_from_key(key), canonical_key=key,
            symbol="GBPUSD", direction="BULLISH", opportunity_type=Ty.REVERSAL.value,
            state=St.READY_FOR_MITIGATION.value, created_at="2026-10-09T12:00:00+00:00",
            updated_at="2026-10-09T12:00:00+00:00", expires_at="2026-10-10T12:00:00+00:00")
        r = OpportunityRepository(sys.argv[1]); r.upsert(opp)
        print("WRITTEN", flush=True)
        time.sleep(120)
    ''')
    p = subprocess.Popen([sys.executable, "-c", script, db],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "WRITTEN", p.stderr.read()
    finally:
        p.kill()                      # abrupt termination, no clean shutdown
        p.wait(timeout=10)
    from src.smc_engine.opportunity.repository import OpportunityRepository
    repo = OpportunityRepository(db)
    try:
        assert repo.state_counts().get(St.READY_FOR_MITIGATION.value, 0) == 1, \
            "committed opportunity state must survive abrupt termination"
    finally:
        repo.close()


# ── Phase 16B: explicit market-time expiry contract (no wall-clock fallback) ──

def _repo_engine(tmp_path):
    from src.smc_engine.opportunity import OpportunityEngine
    from src.smc_engine.opportunity.repository import OpportunityRepository
    repo = OpportunityRepository(str(tmp_path / "r.db"))
    return OpportunityEngine(repo), repo


def test_expire_cycle_without_market_time_is_noop(tmp_path):
    """expire_cycle(now=None) must never expire anything (no wall clock)."""
    eng, repo = _repo_engine(tmp_path)
    opp = _ready_opp(symbol="GBPUSD", expires="2026-10-10 12:00")   # past vs wall clock
    repo.upsert(opp)
    out = eng.expire_cycle()                                        # now=None
    assert out["expired"] == []
    assert repo.get(opp.opportunity_id).state == St.READY_FOR_MITIGATION.value


def test_per_symbol_expire_cycle_does_not_touch_other_instruments(tmp_path):
    eng, repo = _repo_engine(tmp_path)
    a = _ready_opp(symbol="GBPUSD", expires="2026-10-10 12:00")
    b = _ready_opp(symbol="XAUUSD", expires="2026-10-10 12:00")
    repo.upsert(a)
    repo.upsert(b)
    eng.expire_cycle(symbol="GBPUSD", now=pd.Timestamp("2026-10-11 00:00", tz="UTC"))
    assert repo.get(a.opportunity_id).state == St.EXPIRED.value
    assert repo.get(b.opportunity_id).state == St.READY_FOR_MITIGATION.value, \
        "a per-symbol sweep must not expire another instrument"


def test_expire_cycle_expires_only_when_own_market_time_advances(tmp_path):
    eng, repo = _repo_engine(tmp_path)
    opp = _ready_opp(symbol="GBPUSD", expires="2026-10-10 12:00")
    repo.upsert(opp)
    eng.expire_cycle(now=pd.Timestamp("2026-10-10 11:59", tz="UTC"))
    assert repo.get(opp.opportunity_id).state == St.READY_FOR_MITIGATION.value
    eng.expire_cycle(now=pd.Timestamp("2026-10-10 12:01", tz="UTC"))
    assert repo.get(opp.opportunity_id).state == St.EXPIRED.value


def test_observe_with_no_market_time_does_not_expire(tmp_path):
    """observe() with a view that has no market time must not wall-clock expire."""
    from src.smc_engine.opportunity import OpportunityEngine, evaluator
    from src.smc_engine.opportunity.repository import OpportunityRepository
    repo = OpportunityRepository(str(tmp_path / "r.db"))
    eng = OpportunityEngine(repo)
    opp = _ready_opp(symbol="GBPUSD", expires="2026-10-10 12:00")   # past vs wall clock
    repo.upsert(opp)
    view = evaluator.CausalView(symbol="GBPUSD", as_of=None, m15_last_time=None)
    eng.observe("GBPUSD", view)
    assert repo.get(opp.opportunity_id).state == St.READY_FOR_MITIGATION.value, \
        "no market time must not expire by wall clock"
