"""GUI PIPELINE TEST — Gate B readiness (observation only).

These tests validate the DETECTION/NOTIFICATION plumbing against a fake
in-process broker source. They are NOT a Gate B pass: a Gate B pass requires
a real causal SETUP-* from the production pipeline driven manually through
execution, which remains a separate authorization.

Zero broker calls (order_send/order_check) may occur in any of these tests.
"""
from dataclasses import replace

import pandas as pd
import pytest

from src.smc_engine.models import Direction
from tests.test_api import FakeDemoSource, build_client, make_setup


def _readiness(client, symbol=None):
    url = "/api/readiness" + (f"?symbol={symbol}" if symbol else "")
    r = client.get(url)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def src_client(tmp_path):
    src = FakeDemoSource()
    client, hub = build_client(tmp_path, src)
    yield client, hub, src
    hub.stop_poller()


def test_gui_pipeline_setup_detection_identity(src_client):
    """GUI PIPELINE TEST (not GATE B PASS): genuine-shaped SETUP-* is detected
    by the read-only readiness layer with byte-identical backend identity."""
    client, hub, src = src_client
    setup = make_setup("SETUP-EURAUD-149-OB-M15-391-BULLISH")
    setup = replace(setup, symbol="EURAUD")
    hub.register_setup(setup)

    r = _readiness(client, "EURAUD")
    assert r["status"] == "READY_FOR_MANUAL_VALIDATION"
    assert r["setup"]["setup_id"] == "SETUP-EURAUD-149-OB-M15-391-BULLISH"
    assert r["detected_ids"] == [r["setup"]["setup_id"]]
    assert r["mode"].startswith("GATE B WAITING FOR MANUAL VALIDATION")
    # review path reads the SAME backend row (identity stable across surfaces)
    via_setups = client.get("/api/setups?symbol=EURAUD").json()["setups"][0]
    assert via_setups["setup_id"] == r["setup"]["setup_id"]
    assert via_setups["entry"] == r["setup"]["entry"]
    assert via_setups["sl"] == r["setup"]["sl"] and via_setups["tp"] == r["setup"]["tp"]
    assert src.sent == []            # zero broker sends — detection never executes


def test_no_setup_state_is_calm_valid_not_error(src_client):
    client, hub, src = src_client
    r = _readiness(client, "EURAUD")
    assert r["status"] == "WAITING_FOR_CAUSAL_SETUP"
    assert r["setup"] is None
    assert r["mode"].startswith("OBSERVATION")
    assert src.sent == []


def test_duplicate_polling_does_not_duplicate_identity_or_alerts(src_client):
    client, hub, src = src_client
    hub.register_setup(make_setup("SETUP-DUP-1"))
    for _ in range(5):
        r = _readiness(client, "TEST")
        assert r["detected_ids"] == ["SETUP-DUP-1"]
    setups = client.get("/api/setups").json()["setups"]
    assert sum(1 for x in setups if x["setup_id"] == "SETUP-DUP-1") == 1
    alerts = client.get("/api/alerts").json()["alerts"]
    created = [a for a in alerts if a["kind"] == "SETUP_CREATED" and "SETUP-DUP-1" in a["message"]]
    assert len(created) == 1          # one notification per backend identity
    assert src.sent == []


def test_symbol_isolation_in_readiness(src_client):
    client, hub, src = src_client
    aud = replace(make_setup("SETUP-EURAUD-ONLY"), symbol="EURAUD")
    hub.register_setup(aud)
    assert _readiness(client, "EURAUD")["status"] == "READY_FOR_MANUAL_VALIDATION"
    xau = _readiness(client, "XAUUSD")
    assert xau["status"] == "WAITING_FOR_CAUSAL_SETUP"   # EURAUD never bleeds into XAUUSD view
    assert xau["detected_ids"] == []
    # normalized suffix from backend filtering still isolates other symbols
    assert _readiness(client, "euraud")["status"] == "READY_FOR_MANUAL_VALIDATION"
    assert src.sent == []


def test_malformed_setup_rejected_without_crash(src_client):
    client, hub, src = src_client
    broken = make_setup("SETUP-BROKEN")
    broken = replace(broken, entry=None, stop_loss=None, take_profit=None)
    hub.register_setup(broken)                    # orchestration boundary: reject, do not crash
    r = _readiness(client, "TEST")
    assert r["status"] == "WAITING_FOR_CAUSAL_SETUP"      # never presented as executable
    assert r["setup"] is None
    assert "SETUP-BROKEN" not in [x["setup_id"] for x in client.get("/api/setups").json()["setups"]]
    assert client.get("/api/setups/SETUP-BROKEN").status_code == 404
    # non-causal id shape (e.g. a Gate A geometry label) is never "detected" even when complete
    hub.register_setup(replace(make_setup("GATEA-SYNTHETIC-9")))
    r2 = _readiness(client, "TEST")
    assert "GATEA-SYNTHETIC-9" in [x["setup_id"] for x in client.get("/api/setups").json()["setups"]]
    assert "GATEA-SYNTHETIC-9" not in r2["detected_ids"]  # lifecycle shows it; readiness rejects it
    assert src.sent == []


def test_detection_readiness_never_crosses_execution_boundary(src_client):
    """§24 mandatory: while a genuine-shaped setup exists, detection/readiness
    polling must cause zero order_send and zero order_check calls."""
    client, hub, src = src_client
    hub.register_setup(make_setup("SETUP-NOEXEC-1"))
    for _ in range(8):
        _readiness(client)
        client.get("/api/setups")
        client.get("/api/lifecycle")
        client.get("/api/execution/SETUP-NOEXEC-1")
    assert src.sent == []
    # and the setup remains executable ONLY via the explicit paper route
    state = client.get("/api/setups/SETUP-NOEXEC-1").json()
    assert state["state"] == "EXECUTION_READY"


def test_readiness_endpoint_is_readonly_and_api_has_no_broker_primitives():
    """AST-lite static guard: /api/readiness introduces no broker primitives and
    api.py exposes none; GUI assets show no trade-prompt language."""
    import inspect
    import src.smc_engine.web.api as api_mod
    src_txt = inspect.getsource(api_mod)
    for forbidden in ("order_send", "order_check", "TRADE_ACTION"):
        assert forbidden not in src_txt
    app_js = open("src/smc_engine/web/static/app.js", encoding="utf-8").read()
    index_html = open("src/smc_engine/web/static/index.html", encoding="utf-8").read()
    for forbidden in ("order_send", "TRADE_ACTION"):
        assert forbidden not in app_js
        assert forbidden not in index_html
    for prompt in ("BUY NOW", "SELL NOW", "TRADE NOW"):
        assert prompt not in index_html.upper()
    assert "GATE B" in index_html
    assert "REVIEW SETUP" in index_html
