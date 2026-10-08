"""P1-C regressions: ENTRY_TRIGGERED must never mean execution-ready.

TEST FIXTURES; no broker interaction.
"""
import re
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.smc_engine.opportunity import (BlockReason, OpportunityEventKind as Ev,
                                        OpportunityState as St, OpportunityType as Ty,
                                        OpportunityWindows)
from src.smc_engine.opportunity.models import STATE_LABEL
from src.smc_engine.web.api import create_app
from src.smc_engine.web.hub import DictSource, EngineHub, TIMEFRAMES
from tests.test_api import FakeDemoSource
from tests.test_opportunity_engine import CFG, _engine_named, timeline


def _entry_touched(tmp_path):
    eng, repo = _engine_named(tmp_path, "c.db",
                              windows=OpportunityWindows(sweep_to_csd_bars=96,
                                                         ready_ttl_bars=192))
    d1, h4, m15 = timeline(entry_touch=True)
    from src.smc_engine.opportunity import evaluator
    samples = list(m15["time"].iloc[::8]) + list(m15["time"].iloc[-4:])
    events = []
    for t in samples:
        res = eng.observe("GBPUSD", evaluator.build_view("GBPUSD", d1, h4, m15, t, CFG))
        events.extend(res["events"])
    row = [o for o in repo.query(symbol="GBPUSD")
           if o.opportunity_type == Ty.REVERSAL.value][0]
    return eng, repo, row, events


def test_entry_triggered_label_is_not_execution_ready():
    assert STATE_LABEL[St.ENTRY_TRIGGERED] == "ENTRY CONDITION MET"
    assert "EXECUTION READY" not in set(STATE_LABEL.values()), \
        "EXECUTION READY is reserved for setups, never opportunities"


def test_entry_triggered_emits_distinct_event_not_execution_ready(tmp_path):
    eng, repo, row, events = _entry_touched(tmp_path)
    assert row.state == St.ENTRY_TRIGGERED.value
    kinds = [e["kind"] for e in events]
    assert Ev.ENTRY_TRIGGERED.value in kinds
    assert Ev.EXECUTION_READY.value not in kinds, \
        "no EXECUTION_READY alert without an actual setup promotion"


def test_entry_triggered_does_not_imply_risk_approval(tmp_path):
    eng, repo, row, events = _entry_touched(tmp_path)
    assert row.setup_id == ""
    assert row.risk_status in ("", None), "risk is never consulted for an opportunity"


def test_entry_triggered_without_setup_is_not_paper_executable(tmp_path):
    src = FakeDemoSource()
    d1, h4, m15 = timeline(entry_touch=True)
    for code, frame in ((TIMEFRAMES["D1"], d1), (TIMEFRAMES["H4"], h4),
                        (TIMEFRAMES["M15"], m15)):
        src.prime("GBPUSD", code, frame)
    hub = EngineHub(src, db_path=str(tmp_path / "g.db"),
                    setup_store_path=str(tmp_path / "s.db"),
                    magic=202609)
    hub.scan_universe_once(force=True)
    opps = hub.opportunities.repo.query(symbol="GBPUSD", active_only=False)
    assert opps
    client = TestClient(create_app(hub))
    # the opportunity id is not a setup id: the backend paper gate refuses it
    r = client.post(f"/api/paper/{opps[0].opportunity_id}")
    assert r.status_code in (403, 404, 409, 502), r.text
    assert src.sent == [], "no broker order may be produced by opportunity state"
    hub.stop_poller()


def test_execution_ready_emitted_only_after_setup_promotion(tmp_path):
    from dataclasses import replace
    from src.smc_engine.causal import CausalCandidate
    from src.smc_engine.models import Direction
    from src.smc_engine.setup import TradeSetup
    from src.smc_engine.opportunity import evaluator
    from tests.test_api import make_setup

    eng, repo = _engine_named(tmp_path, "p.db",
                              windows=OpportunityWindows(sweep_to_csd_bars=96,
                                                         ready_ttl_bars=192))
    view = evaluator.build_view("GBPUSD", *timeline(), None, CFG)
    eng.observe("GBPUSD", view)
    row = [o for o in repo.query(symbol="GBPUSD")
           if o.opportunity_type == Ty.REVERSAL.value][0]
    assert row.setup_id == ""

    # a real causal candidate that matches this opportunity's sweep anchor
    setup = TradeSetup(
        id="SETUP-GBPUSD-149-OB-M15-391-BULLISH", symbol="GBPUSD",
        direction=Direction.BULLISH, created_time=pd.Timestamp("2026-01-25 22:00", tz="UTC"),
        poi_id="P", sweep_id=row.sweep_evidence["id"], csd_id="C", protected_level=1.0,
        order_block_id="OB-M15-391-BULLISH", inducement_id="I", entry=104.4,
        stop_loss=101.5, take_profit=108.0, irl_swing_id="R",
        invalidation_level=101.5, risk_percent=1.0)
    cand = CausalCandidate(setup=setup, setup_time=setup.created_time, sweep=object(), csd=object())
    view2 = evaluator.CausalView(symbol="GBPUSD", as_of=view.as_of,
                                 m15_frame=view.m15_frame, m15_last_time=view.m15_last_time,
                                 sweeps=view.sweeps, csd_by_sweep=view.csd_by_sweep,
                                 m15_obs=view.m15_obs, m15_fvgs=view.m15_fvgs,
                                 m15_swings=view.m15_swings, d1_pois=view.d1_pois,
                                 idms=view.idms, candidates=[cand])
    res = eng.observe("GBPUSD", view2)
    kinds = [e["kind"] for e in res["events"]]
    assert Ev.CONVERTED_TO_SETUP.value in kinds
    assert Ev.EXECUTION_READY.value in kinds, "EXECUTION_READY only after promotion"
    row2 = [o for o in repo.query(symbol="GBPUSD")
            if o.opportunity_type == Ty.REVERSAL.value][0]
    assert row2.setup_id == setup.id


def test_ui_renders_backend_label_and_marks_risk_not_checked():
    js = Path("src/smc_engine/web/static/app.js").read_text(encoding="utf-8")
    # opportunity rows render the backend label (never a local EXECUTION READY)
    assert "esc(o.label)" in js
    assert "NOT CHECKED" in js, "entry-triggered opportunities must show risk as NOT CHECKED"
    # the paper button is gated by setup rows + risk, never by opportunity state
    assert 'S.risk.state === "OK"' in js
    assert "st.display === \"EXECUTION_READY\"" in js
    assert "order_send" not in js and "TRADE_ACTION" not in js
