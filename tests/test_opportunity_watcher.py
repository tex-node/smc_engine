"""Background discovery / watcher independence tests — HARD REQUIREMENT.

An opportunity must be discovered, persisted and alerted while the UI is
viewing a DIFFERENT symbol (or no UI at all). Also: the before/after
opportunity funnel over the same historical replay set.

TEST FIXTURES ONLY. Discovery is 100% non-executing; broker sends are asserted
to be exactly zero.
"""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.smc_engine.causal import CausalMTFAnalyzer
from src.smc_engine.opportunity import (OpportunityState as St, OpportunityType as Ty,
                                        OpportunityWindows)
from src.smc_engine.opportunity import evaluator
from src.smc_engine.strategy import MultiTimeframeConfig
from src.smc_engine.web.api import create_app
from src.smc_engine.web.hub import DictSource, EngineHub, TIMEFRAMES
from tests.test_api import FakeDemoSource
from tests.test_opportunity_engine import BASE, CFG, timeline


def _flat(n=200, start="2026-01-01", freq="15min"):
    times = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"time": times, "open": 1.10, "high": 1.101,
                         "low": 1.099, "close": 1.10})


def prime_timeline(src, symbol="GBPUSD", cut=None):
    d1, h4, m15 = timeline()
    if cut is not None:
        cut = pd.Timestamp(cut, tz="UTC")
        m15 = m15[m15["time"] <= cut]
        h4 = h4[h4["time"] <= cut]
        d1 = d1[d1["time"] <= cut]
    src.prime(symbol, TIMEFRAMES["D1"], d1)
    src.prime(symbol, TIMEFRAMES["H4"], h4)
    src.prime(symbol, TIMEFRAMES["M15"], m15)


def prime_flat(src, symbol="EURAUD"):
    for code, freq in ((TIMEFRAMES["D1"], "1D"), (TIMEFRAMES["H4"], "4h"),
                       (TIMEFRAMES["M15"], "15min")):
        src.prime(symbol, code, _flat(300, freq=freq))


@pytest.fixture()
def hub(tmp_path):
    src = FakeDemoSource()
    prime_timeline(src, "GBPUSD", cut="2026-01-26 01:00")   # READY, TTL still live
    prime_flat(src, "EURAUD")
    h = EngineHub(src, db_path=str(tmp_path / "gui.db"),
                  setup_store_path=str(tmp_path / "state.db"))
    yield h
    h.stop_poller()


def test_discovery_without_ui_selection(hub):
    """UI views EURAUD; GBPUSD opportunity must still be discovered + alerted."""
    client = TestClient(create_app(hub))
    client.get("/api/analysis/EURAUD/M15")          # the user's selected symbol
    assert [s for s in hub.watchlist] == ["EURAUD"]  # selection state

    stats = hub.scan_universe_once(force=True)       # background watcher

    assert stats["symbols_scanned"] >= 2, "universe must be scanned, not the watchlist"
    assert stats["symbols_succeeded"] >= 2
    opps = client.get("/api/opportunities?symbol=GBPUSD").json()["opportunities"]
    assert opps, "GBPUSD opportunity must be discovered while viewing EURAUD"
    assert all(o["symbol"] == "GBPUSD" for o in opps)
    # alert dispatched by the backend, no UI involved
    alert_kinds = {a["kind"] for a in client.get("/api/alerts").json()["alerts"]}
    assert "OPPORTUNITY_CREATED" in alert_kinds
    # UI selection unchanged by discovery
    assert hub.watchlist == ["EURAUD"]
    assert hub.source.sent == [], "discovery must never send broker orders"


def test_api_opportunity_surfaces_and_history(hub):
    client = TestClient(create_app(hub))
    hub.scan_universe_once(force=True)
    row = client.get("/api/opportunities").json()
    assert row["opportunities"] and row["funnel"]["total"] >= 1
    one = row["opportunities"][0]
    detail = client.get(f"/api/opportunities/{one['opportunity_id']}").json()
    assert detail["opportunity"]["opportunity_id"] == one["opportunity_id"]
    assert detail["history"], "state history must be exposed"
    assert client.get("/api/opportunities/diagnostics").status_code == 200
    assert client.get("/api/opportunities/NOPE").status_code == 404
    # additive fields on existing endpoints (compatibility intact)
    analysis = client.get("/api/analysis/GBPUSD/M15").json()
    assert "opportunities" in analysis and "opportunity_audit" in analysis
    readiness = client.get("/api/readiness?symbol=GBPUSD").json()
    assert "opportunities" in readiness and "status" in readiness


def test_repeated_scans_are_idempotent_and_alert_once(hub):
    hub.scan_universe_once(force=True)
    gbp_after_1 = hub.opportunities.repo.query(symbol="GBPUSD")
    alerts_after_1 = len(hub.web.alerts())
    emitted_after_1 = hub.opportunities.diagnostics()["alerts_emitted"]

    for _ in range(4):
        hub.scan_universe_once(force=True)

    gbp = hub.opportunities.repo.query(symbol="GBPUSD")
    assert len(gbp) == len({o.opportunity_id for o in gbp}) == len(gbp_after_1)
    assert len(hub.web.alerts()) == alerts_after_1, "no duplicate alerts across scans"
    assert hub.opportunities.diagnostics()["alerts_emitted"] == emitted_after_1
    ready_alerts = [a for a in hub.web.alerts() if a["kind"] == "READY"]
    assert len(ready_alerts) <= len(gbp)
    assert len(ready_alerts) == len({(a["symbol"], a["message"]) for a in ready_alerts})


def test_symbol_failure_does_not_starve_scan(tmp_path):
    class FlakySource(DictSource):
        name = "flaky"

        def bars(self, symbol, tf_code, count):
            if symbol == "BROKEN":
                raise RuntimeError("simulated market-data failure")
            return super().bars(symbol, tf_code, count)

    src = FlakySource()
    prime_timeline(src, "GBPUSD", cut="2026-01-26 01:00")
    prime_flat(src, "BROKEN")
    hub = EngineHub(src, db_path=str(tmp_path / "g.db"),
                    setup_store_path=str(tmp_path / "s.db"))
    stats = hub.scan_universe_once(force=True)
    assert stats["symbols_failed"] >= 1
    assert stats["symbols_succeeded"] >= 1
    assert hub.opportunities.repo.query(symbol="GBPUSD")
    assert hub.opportunity_diagnostics()["symbols"]["BROKEN"]["last_error"]
    hub.stop_poller()


def test_restart_recovery_through_hub(tmp_path):
    src = FakeDemoSource()
    prime_timeline(src, "GBPUSD", cut="2026-01-26 01:00")
    h1 = EngineHub(src, db_path=str(tmp_path / "g.db"),
                   setup_store_path=str(tmp_path / "s.db"))
    h1.scan_universe_once(force=True)
    before = h1.opportunities.repo.query(symbol="GBPUSD")
    h1.stop_poller()
    h1.opportunities.repo.close()

    src2 = FakeDemoSource()
    prime_timeline(src2, "GBPUSD", cut="2026-01-26 01:00")
    h2 = EngineHub(src2, db_path=str(tmp_path / "g.db"),
                   setup_store_path=str(tmp_path / "s.db"))
    after = h2.opportunities.repo.query(symbol="GBPUSD")
    assert {o.opportunity_id for o in after} == {o.opportunity_id for o in before}
    assert after[0].expires_at == before[0].expires_at
    h2.scan_universe_once(force=True)                 # no duplicates after restart
    assert len(h2.opportunities.repo.query(symbol="GBPUSD")) == len(before)
    h2.stop_poller()


# ------------------------------------------------------- before/after funnel

def test_funnel_before_after_same_replay(tmp_path):
    """Same historical replay: the OLD architecture only sees a setup when the
    full causal chain resolves inside a single scan; the NEW opportunity layer
    keeps the developing opportunity alive across cycles."""
    d1, h4, m15 = timeline()
    analyzer = CausalMTFAnalyzer("GBPUSD", CFG)
    samples = list(m15["time"].iloc[::8]) + list(m15["time"].iloc[-4:])

    old_structural_candidates = 0
    old_execution_ready_samples = 0
    old_samples_with_candidate = 0
    samples_without_old = 0

    from src.smc_engine.opportunity import OpportunityEngine, OpportunityRepository
    eng = OpportunityEngine(OpportunityRepository(str(tmp_path / "funnel.db")), config=CFG,
                            windows=OpportunityWindows(sweep_to_csd_bars=96))
    new_ready_seen = 0
    new_active_without_old_candidate = 0

    for t in samples:
        view = evaluator.build_view("GBPUSD", d1, h4, m15, t, CFG)
        old_cands = len(view.candidates)
        old_structural_candidates += old_cands
        if old_cands:
            old_samples_with_candidate += 1
            old_execution_ready_samples += old_cands
        else:
            samples_without_old += 1
        eng.observe("GBPUSD", view)
        actives = eng.repo.query(symbol="GBPUSD", active_only=True)
        if any(o.state == St.READY_FOR_MITIGATION.value for o in actives):
            new_ready_seen += 1
        if old_cands == 0 and actives:
            new_active_without_old_candidate += 1

    new_funnel = eng.funnel()
    print("\n=== OPPORTUNITY FUNNEL (same replay set) ===")
    print(f"OLD: samples={len(samples)} structural_candidates={old_structural_candidates} "
          f"execution_ready_observations={old_execution_ready_samples} "
          f"samples_without_any_setup={samples_without_old}")
    print(f"NEW: {new_funnel}")
    print(f"NEW: ready_observations={new_ready_seen} "
          f"samples_with_active_opportunity_but_no_old_setup={new_active_without_old_candidate}")

    assert new_funnel["total"] >= 1
    assert new_ready_seen >= 1, "the opportunity must reach READY during the replay"
    assert new_active_without_old_candidate >= 1, (
        "there must be at least one cycle where a developing opportunity existed "
        "while the old single-scan architecture saw no setup at all")
    assert old_execution_ready_samples <= new_ready_seen


def test_opportunity_layer_has_no_broker_primitives():
    from pathlib import Path
    for f in Path('src/smc_engine/opportunity').glob('*.py'):
        txt = f.read_text(encoding='utf-8')
        for token in ('order_send', 'order_check', 'TRADE_ACTION', 'MetaTrader5', 'positions_get'):
            assert token not in txt, f'{f.name} contains {token}'
    hub_src = Path('src/smc_engine/web/hub.py').read_text(encoding='utf-8')
    i = hub_src.find('def scan_universe_once')
    j = hub_src.find('def opportunity_diagnostics')
    seg = hub_src[i:j]
    for token in ('order_send', 'order_check', 'TRADE_ACTION'):
        assert token not in seg

