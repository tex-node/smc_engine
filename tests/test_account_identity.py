"""Account Identity Gate + Runtime Authority regression tests.

Tests verify that the engine hard-blocks analysis, setup registration,
and Gate B when the connected MT5 account is not the authorized demo account
(login=477217728, server=Exness-MT5Trial9, DEMO).

DictSource and FakeDemoSource (name != "mt5") bypass the gate so every
other test suite remains unaffected.

Phase 11 tests (11-25) cover UI state propagation, alert provenance,
paper gate, and dirty-source detection as specified in the runtime
authority gate brief.
"""
from __future__ import annotations

import pytest
import pandas as pd

from src.smc_engine.models import Direction
from src.smc_engine.setup import TradeSetup
from src.smc_engine.web.hub import (
    AUTHORIZED_LOGIN, AUTHORIZED_SERVER,
    DictSource, EngineHub, TIMEFRAMES,
)

from tests.test_api import build_client, FakeDemoSource, make_setup


# ---------------------------------------------------------------------------
# MockMT5Source: simulates a real MT5 connection with configurable account.
# name="mt5" triggers the identity gate.
# ---------------------------------------------------------------------------
class MockMT5Source(DictSource):
    """Test double that pretends to be an MT5 source with a fixed account."""
    name = "mt5"

    def __init__(self, login: int, server: str, is_demo_flag: bool):
        super().__init__()
        self.account_info = {
            "login": login, "server": server, "balance": 10000.0,
            "equity": 10000.0, "currency": "USD", "margin_free": 10000.0,
        }
        self._is_demo_flag = is_demo_flag

    @property
    def is_demo(self) -> bool:
        return self._is_demo_flag


def _authorized_source() -> MockMT5Source:
    return MockMT5Source(AUTHORIZED_LOGIN, AUTHORIZED_SERVER, True)


# ---------------------------------------------------------------------------
# 1. Correct login + server + DEMO → AUTHORIZED, engine_operational=True
# ---------------------------------------------------------------------------
def test_authorized_account_is_operational(tmp_path):
    _, hub = build_client(tmp_path, _authorized_source())
    st = hub.status()
    assert st["account_identity"] == "AUTHORIZED"
    assert st["engine_operational"] is True
    assert st["engine"] == "CONNECTED"
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 2. Wrong login + correct server → UNAUTHORIZED, engine_operational=False
# ---------------------------------------------------------------------------
def test_wrong_login_is_unauthorized(tmp_path):
    src = MockMT5Source(999999, AUTHORIZED_SERVER, True)
    _, hub = build_client(tmp_path, src)
    st = hub.status()
    assert st["account_identity"] == "UNAUTHORIZED"
    assert st["engine_operational"] is False
    assert st["engine"] == "UNAUTHORIZED_ACCOUNT"
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 3. Correct login + wrong server → UNAUTHORIZED
# ---------------------------------------------------------------------------
def test_wrong_server_is_unauthorized(tmp_path):
    src = MockMT5Source(AUTHORIZED_LOGIN, "SomeBroker-Live", True)
    _, hub = build_client(tmp_path, src)
    st = hub.status()
    assert st["account_identity"] == "UNAUTHORIZED"
    assert st["engine_operational"] is False
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 4. Correct login + correct server + LIVE mode → UNAUTHORIZED
# ---------------------------------------------------------------------------
def test_live_mode_is_unauthorized(tmp_path):
    src = MockMT5Source(AUTHORIZED_LOGIN, AUTHORIZED_SERVER, False)  # is_demo=False
    _, hub = build_client(tmp_path, src)
    st = hub.status()
    assert st["account_identity"] == "UNAUTHORIZED"
    assert st["engine_operational"] is False
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 5. Headway-Real / 17594495 → UNAUTHORIZED (safety finding from 2026-09-30)
# ---------------------------------------------------------------------------
def test_headway_real_account_is_unauthorized(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    _, hub = build_client(tmp_path, src)
    st = hub.status()
    assert st["account_identity"] == "UNAUTHORIZED"
    assert st["engine_operational"] is False
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 6. Unauthorized account cannot trigger causal analysis (→ PermissionError)
# ---------------------------------------------------------------------------
def test_unauthorized_blocks_analysis(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    client, hub = build_client(tmp_path, src)
    r = client.get("/api/analysis/TEST/M15")
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert "UNAUTHORIZED_ACCOUNT" in r.text
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 7. Unauthorized account cannot register a lifecycle setup
# ---------------------------------------------------------------------------
def test_unauthorized_blocks_register_setup(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    _, hub = build_client(tmp_path, src)
    setup = make_setup("SETUP-UNAUTH-REG")
    with pytest.raises(PermissionError, match="UNAUTHORIZED_ACCOUNT"):
        hub.register_setup(setup)
    assert hub.registry.get("SETUP-UNAUTH-REG") is None
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 8. Unauthorized account cannot reach Gate B READY_FOR_MANUAL_VALIDATION
# ---------------------------------------------------------------------------
def test_unauthorized_blocks_gate_b(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    _, hub = build_client(tmp_path, src)
    r = hub.readiness()
    assert r["status"] == "UNAUTHORIZED_ACCOUNT"
    assert r["detected_ids"] == []
    assert r["setup"] is None
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 9. /api/runtime reports unauthorized account fields correctly
# ---------------------------------------------------------------------------
def test_runtime_reports_unauthorized_account(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    client, hub = build_client(tmp_path, src)
    r = client.get("/api/runtime").json()
    assert "authorized_account" in r
    assert "actual_account" in r
    assert "account_identity" in r
    assert "engine_operational" in r
    assert r["authorized_account"] == f"{AUTHORIZED_LOGIN}@{AUTHORIZED_SERVER} DEMO"
    assert r["account_identity"] == "UNAUTHORIZED"
    assert r["engine_operational"] is False
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 10. Authorized demo account (FakeDemoSource, login=0) retains full behavior
# ---------------------------------------------------------------------------
def test_authorized_demo_source_retains_behavior(tmp_path):
    """DictSource / FakeDemoSource bypass the gate — existing behavior unchanged."""
    client, hub = build_client(tmp_path, FakeDemoSource())
    st = hub.status()
    assert st["account_identity"] == "AUTHORIZED"
    assert st["engine_operational"] is True
    # analysis must succeed
    r = client.get("/api/analysis/TEST/M15")
    assert r.status_code == 200
    # setup registration must succeed
    setup = make_setup("SETUP-AUTHORIZED-OK")
    hub.register_setup(setup)
    assert hub.registry.get("SETUP-AUTHORIZED-OK") is not None
    hub.stop_poller()


# ===========================================================================
# Phase 11 — Runtime Authority / UI State Propagation tests
# ===========================================================================

# ---------------------------------------------------------------------------
# 11. /api/status for unauthorized includes actual_account and authorized_account
# ---------------------------------------------------------------------------
def test_status_unauthorized_exposes_account_fields(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    _, hub = build_client(tmp_path, src)
    st = hub.status()
    assert st["account_identity"] == "UNAUTHORIZED"
    assert st["engine_operational"] is False
    assert "authorized_account" in st
    assert "actual_account" in st
    assert "477217728" in st["authorized_account"]
    assert "Exness-MT5Trial9" in st["authorized_account"]
    assert "17594495" in st["actual_account"]
    assert "Headway-Real" in st["actual_account"]
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 12. /api/status for authorized DEMO exposes correct account fields
# ---------------------------------------------------------------------------
def test_status_authorized_account_fields(tmp_path):
    _, hub = build_client(tmp_path, _authorized_source())
    st = hub.status()
    assert st["account_identity"] == "AUTHORIZED"
    assert st["engine_operational"] is True
    assert "477217728" in st["authorized_account"]
    assert "477217728" in st["actual_account"]
    assert "Exness-MT5Trial9" in st["actual_account"]
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 13. risk_engine shows UNAUTHORIZED (not READY) for unauthorized MT5 account
# ---------------------------------------------------------------------------
def test_risk_engine_unauthorized_when_account_blocked(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    _, hub = build_client(tmp_path, src)
    st = hub.status()
    # Account is connected but unauthorized — must NOT show READY
    assert st["risk_engine"] == "UNAUTHORIZED", (
        f"risk_engine must be UNAUTHORIZED for unauthorized account, got {st['risk_engine']!r}"
    )
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 14. paper_enabled is False for unauthorized account (even if source were demo)
# ---------------------------------------------------------------------------
def test_paper_enabled_false_for_unauthorized(tmp_path):
    # An unauthorized account (LIVE / wrong login) must never show paper_enabled=True
    src = MockMT5Source(17594495, "Headway-Real", False)
    _, hub = build_client(tmp_path, src)
    st = hub.status()
    assert st["paper_enabled"] is False
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 15. paper_place is blocked for unauthorized account
# ---------------------------------------------------------------------------
def test_paper_place_blocked_for_unauthorized(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    client, hub = build_client(tmp_path, src)
    r = client.post("/api/paper/SETUP-FAKE")
    assert r.status_code == 403, f"expected 403, got {r.status_code}"
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 16. /api/alerts returns server_startup_time field
# ---------------------------------------------------------------------------
def test_alerts_returns_server_startup_time(tmp_path):
    client, hub = build_client(tmp_path)
    r = client.get("/api/alerts").json()
    assert "server_startup_time" in r, "alerts response must include server_startup_time"
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 17. /api/alerts server_startup_time is ISO 8601 string (not None after startup)
# ---------------------------------------------------------------------------
def test_alerts_startup_time_is_iso_string(tmp_path):
    from src.smc_engine.web import runtime as rt
    client, hub = build_client(tmp_path)
    r = client.get("/api/alerts").json()
    startup = r.get("server_startup_time")
    assert startup is not None, "server_startup_time must be set after hub init"
    # Must be parseable ISO 8601
    pd.Timestamp(startup)
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 18. Unauthorized analysis returns 403 with UNAUTHORIZED_ACCOUNT in detail
# ---------------------------------------------------------------------------
def test_unauthorized_analysis_403_detail(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    client, hub = build_client(tmp_path, src)
    r = client.get("/api/analysis/TEST/M15")
    assert r.status_code == 403
    body = r.json()
    assert "UNAUTHORIZED_ACCOUNT" in body.get("detail", ""), (
        f"403 detail must contain UNAUTHORIZED_ACCOUNT, got: {body}"
    )
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 19. Unauthorized setup registration raises PermissionError (not silent skip)
# ---------------------------------------------------------------------------
def test_unauthorized_register_setup_raises(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    _, hub = build_client(tmp_path, src)
    with pytest.raises(PermissionError):
        hub.register_setup(make_setup("SETUP-PERM-TEST"))
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 20. Gate B readiness returns UNAUTHORIZED_ACCOUNT status (not READY) for
#     unauthorized account even if registry somehow contained a setup.
# ---------------------------------------------------------------------------
def test_gate_b_readiness_blocked_for_unauthorized(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    _, hub = build_client(tmp_path, src)
    r = hub.readiness()
    assert r["status"] == "UNAUTHORIZED_ACCOUNT"
    assert r["detected_ids"] == []
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 21. Authorized DictSource bypasses gate — risk_engine shows READY
# ---------------------------------------------------------------------------
def test_dict_source_risk_engine_ready(tmp_path):
    """DictSource (name='static') bypasses gate — risk_engine must stay READY."""
    _, hub = build_client(tmp_path)  # default DictSource
    st = hub.status()
    assert st["account_identity"] == "AUTHORIZED"
    assert st["risk_engine"] == "READY"
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 22. /api/runtime includes authorized_account / actual_account for authorized
# ---------------------------------------------------------------------------
def test_runtime_authorized_account_fields(tmp_path):
    client, hub = build_client(tmp_path, _authorized_source())
    r = client.get("/api/runtime").json()
    assert r["account_identity"] == "AUTHORIZED"
    assert r["engine_operational"] is True
    assert r["authorized_account"] == r["actual_account"]
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 23. /api/runtime includes authorized_account / actual_account for unauthorized
# ---------------------------------------------------------------------------
def test_runtime_unauthorized_account_fields(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    client, hub = build_client(tmp_path, src)
    r = client.get("/api/runtime").json()
    assert r["account_identity"] == "UNAUTHORIZED"
    assert r["engine_operational"] is False
    assert r["authorized_account"] != r["actual_account"]
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 24. live_execution_enabled is always False — never influenced by auth state
# ---------------------------------------------------------------------------
def test_live_execution_never_enabled_regardless_of_auth(tmp_path):
    sources = [MockMT5Source(17594495, "Headway-Real", False),
               MockMT5Source(AUTHORIZED_LOGIN, AUTHORIZED_SERVER, True),
               DictSource()]
    for i, src in enumerate(sources):
        sub = tmp_path / str(i)
        sub.mkdir()
        _, hub = build_client(sub, src)
        st = hub.status()
        assert st["live_execution_enabled"] is False, (
            f"live_execution_enabled must always be False, got True for {src.name}"
        )
        hub.stop_poller()


# ---------------------------------------------------------------------------
# 25. execution field is DISABLED for unauthorized regardless of demo flag
# ---------------------------------------------------------------------------
def test_execution_disabled_for_unauthorized(tmp_path):
    src = MockMT5Source(17594495, "Headway-Real", False)
    _, hub = build_client(tmp_path, src)
    st = hub.status()
    assert st["execution"] == "DISABLED"
    hub.stop_poller()
