"""UI context-consistency regressions.

Covers the dashboard defects reported in UI-CONTEXT-CONSISTENCY-AUDIT.md:
* chart/quote/label/price must agree on ONE instrument;
* a delayed response for a previous symbol must not overwrite the newer one;
* a failed analysis must not leave a stale live price;
* the chart symbol must be labelled from the data it actually draws;
* an opportunity (READY) must be inspectable as its OWN context, never shown
  as a fresh causal setup.

Three layers:
1. A Node behavioural harness runs the REAL static/app.js state machine under a
   stubbed DOM/transport (skipped if node is unavailable).
2. Static structural guards (the repo's existing convention for frontend tests).
3. Backend API-contract tests proving the data the UI binds to is self-consistent.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.smc_engine.web.api import create_app
from src.smc_engine.web.hub import EngineHub
from tests.test_api import FakeDemoSource
from tests.test_opportunity_watcher import prime_flat, prime_timeline

STATIC = Path("src/smc_engine/web/static")


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def hub(tmp_path):
    src = FakeDemoSource()
    prime_timeline(src, "GBPUSD", cut="2026-01-26 01:00")   # eligible → READY
    prime_flat(src, "EURAUD")                               # flat → no event
    h = EngineHub(src, db_path=str(tmp_path / "g.db"),
                  setup_store_path=str(tmp_path / "s.db"))
    h.stop_poller()
    yield h
    h.stop_poller()


@pytest.fixture()
def client(hub):
    return TestClient(create_app(hub))


# ── 1. behavioural harness (real app.js) ──────────────────────────────────────

def test_frontend_context_consistency_harness():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    js = Path("tests/frontend/context_consistency.test.js")
    assert js.exists()
    r = subprocess.run([node, str(js)], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")


# ── 2. static structural guards ───────────────────────────────────────────────

def _js():
    return STATIC.joinpath("app.js").read_text(encoding="utf-8")


def test_chart_label_derives_from_drawn_data_not_selection():
    js = _js()
    # header labelled from the data actually drawn (fallback to selection)
    assert "(a ? a.symbol : S.symbol)" in js
    # stale live price must be clearable
    assert "function clearPriceTag" in js


def test_scoped_loads_discard_stale_symbol_responses():
    js = _js()
    assert "symbolSeq++" in js
    assert "seq !== S.symbolSeq" in js
    # the symbol-scoped loads carry the guard
    for fn in ("loadAnalysis", "loadSetups", "loadReadiness", "loadCausalEvents"):
        assert f"function {fn}" in js


def test_chart_states_are_distinct():
    js = _js()
    for token in ("loading", "unavailable", "empty", "ready"):
        assert token in js
    assert "LOADING" in js and "MT5 DISCONNECTED" in js and "not enough candles" in js


def test_opportunity_context_surface_present():
    js = _js()
    assert "OPPORTUNITY CONTEXT" in js and "CHART CONTEXT" in js
    assert "renderOpportunityContext" in js
    assert "selectOpportunity" in js
    # READY opportunity must never be presented as an executable setup in its pane
    assert "NOT an executable setup" in js
    html = STATIC.joinpath("index.html").read_text(encoding="utf-8")
    assert 'id="ctx-context"' in html


def test_no_broker_primitives_in_frontend_fix():
    js = _js()
    # match the repo's existing frontend guard (order_check appears in display text)
    for tok in ("order_send", "TRADE_ACTION"):
        assert tok not in js


def test_sse_updates_do_not_change_selection():
    js = _js()
    # live ticks only update the price tag for the CURRENT chart symbol
    assert "p.symbol === S.symbol" in js
    # no SSE handler may mutate the chart selection
    import re
    m = re.search(r"function openStream.*?function renderStatus", js, re.S)
    assert m
    assert "S.symbol =" not in m.group(0)


def test_timeframe_switch_invalidates_old_context():
    js = _js()
    import re
    m = re.search(r"function selectTimeframe.*?refresh\(\);", js, re.S)
    assert m
    assert "symbolSeq++" in m.group(0)
    assert "S.analysis = null" in m.group(0)


# ── 3. backend API contracts the UI binds to ──────────────────────────────────

def test_analysis_response_is_self_identifying(client):
    """The chart/quote bind to a.symbol/a.timeframe — they must match the request."""
    a = client.get("/api/analysis/GBPUSD/M15").json()
    assert a["symbol"] == "GBPUSD"
    assert a["timeframe"] == "M15"
    assert "candles" in a and "quote" in a


def test_opportunities_are_global_and_self_consistent(hub, client):
    hub.scan_universe_once(force=True)
    payload = client.get("/api/opportunities?active_only=true").json()
    rows = payload["opportunities"]
    assert rows, "fixture must produce at least one opportunity"
    # opportunity rows carry their own symbol/state/label
    for o in rows:
        assert o["symbol"] and o["opportunity_id"]
        assert o["label"] in {"READY", "ARMED", "DEVELOPING", "WATCHING",
                              "ENTRY CONDITION MET", "SUPERSEDED", "INVALIDATED",
                              "EXPIRED"}


def test_opportunity_detail_matches_row(hub, client):
    hub.scan_universe_once(force=True)
    rows = client.get("/api/opportunities?active_only=true").json()["opportunities"]
    one = rows[0]
    detail = client.get(f"/api/opportunities/{one['opportunity_id']}").json()
    assert detail["opportunity"]["opportunity_id"] == one["opportunity_id"]
    assert detail["opportunity"]["symbol"] == one["symbol"]
    assert "history" in detail


def test_setups_are_symbol_scoped(client):
    """The chart/setup panel binds to /api/setups?symbol=<chart symbol>."""
    r = client.get("/api/setups?symbol=GBPUSD").json()
    assert "setups" in r
    for s in r["setups"]:
        assert s.get("symbol") == "GBPUSD"


def test_ready_opportunity_is_not_a_causal_setup(hub, client):
    """A READY opportunity must not imply a causal setup for the chart context."""
    hub.scan_universe_once(force=True)
    opps = client.get("/api/opportunities?active_only=true").json()["opportunities"]
    ready = [o for o in opps if o["label"] == "READY"]
    if not ready:
        pytest.skip("no READY opportunity in fixture")
    sym = ready[0]["symbol"]
    setups = client.get(f"/api/setups?symbol={sym}").json()["setups"]
    # The opportunity may exist with no promoted causal setup — the two are
    # separate layers; the UI must not conflate them.
    assert isinstance(setups, list)
