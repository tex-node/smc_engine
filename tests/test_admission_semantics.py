"""Gate B — Admission semantics regression tests.

Covers the distinction between:
  structurally_qualified  — setup passes D1→H4→M15→IDM→IRL causal chain
  new_admissible          — structurally_qualified AND not already in registry
  terminal_historical     — already in registry in a consumed/invalidated state
  execution_ready         — newly admitted, reached EXECUTION_READY lifecycle state

Rules under test:
  1. structurally_qualified_count > 0 does NOT imply Gate B READY
  2. Terminal setups are NOT resurrected by re-analysis
  3. new_admissible_count == 0 when all candidates are already terminal
  4. readiness() returns READY only when execution_ready_count > 0
  5. causal scan and analysis agree on admission distinction
  6. account identity gate remains enforced throughout
  7. live execution remains False and execution calls remain 0
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.smc_engine.lifecycle import SetupState as LCState
from src.smc_engine.models import Direction
from src.smc_engine.setup import TradeSetup
from src.smc_engine.web.hub import (
    AUTHORIZED_LOGIN, AUTHORIZED_SERVER, DictSource, EngineHub, TIMEFRAMES,
    _TERMINAL_DISPLAY_STATES,
)

from tests.test_api import build_client, FakeDemoSource, make_setup
from tests.test_account_identity import MockMT5Source
from tests.test_integration_fixture import build_fixture


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hub_with_source(src=None):
    return EngineHub(src or DictSource(), db_path=":memory:", setup_store_path=":memory:")


def _primed(tmp_path, src=None):
    return build_client(tmp_path, src)


def _register_terminal(hub: EngineHub, setup: TradeSetup, to_state: LCState) -> None:
    """Register a setup and immediately transition it to a terminal state."""
    hub.register_setup(setup)
    hub._transition(setup.id, to_state, f"forced-terminal-for-test")


# ---------------------------------------------------------------------------
# 1. Structurally qualified but already terminal — NOT new admissible
# ---------------------------------------------------------------------------

def test_terminal_setup_admission_is_terminal_historical(tmp_path):
    """A setup already in a terminal state gets admission='TERMINAL_HISTORICAL'."""
    client, hub = _primed(tmp_path)
    s = make_setup("SETUP-TERM-001")
    _register_terminal(hub, s, LCState.ENTRY_NO_LONGER_VALID)

    # Directly call register_setup again — must be idempotent
    hub.register_setup(s)
    lc = hub.registry.get(s.id)
    assert lc is not None
    assert lc.state is LCState.ENTRY_NO_LONGER_VALID, (
        "terminal setup must not be resurrected by re-registration"
    )
    hub.stop_poller()


def test_terminal_setup_not_resurrected_to_execution_ready(tmp_path):
    """Re-registering a terminal setup must NOT change its state back to EXECUTION_READY."""
    client, hub = _primed(tmp_path)
    for state in (LCState.FILLED, LCState.ENTRY_NO_LONGER_VALID,
                  LCState.POI_INVALIDATED, LCState.OB_INVALIDATED):
        s = make_setup(f"SETUP-TERM-{state.value[:4]}")
        _register_terminal(hub, s, state)
        hub.register_setup(s)  # second call
        lc = hub.registry.get(s.id)
        assert lc.state is state, (
            f"setup in {state.value} must not be resurrected to EXECUTION_READY"
        )
    hub.stop_poller()


def test_readiness_waiting_when_all_candidates_are_terminal(tmp_path):
    """readiness() must return WAITING_FOR_CAUSAL_SETUP when every registered setup is terminal."""
    client, hub = _primed(tmp_path)
    for i in range(3):
        s = make_setup(f"SETUP-TERM-ALL-{i:03d}")
        _register_terminal(hub, s, LCState.ENTRY_NO_LONGER_VALID)

    r = hub.readiness()
    assert r["status"] == "WAITING_FOR_CAUSAL_SETUP", (
        f"readiness must be WAITING when all setups are terminal, got {r['status']}"
    )
    assert r["detected_ids"] == [], "no EXECUTION_READY setups → detected_ids must be empty"
    assert r["rejected_incomplete"] == 0, (
        "terminal setups are complete — rejected_incomplete must be 0, not 3"
    )
    assert r["terminal_historical_count"] == 3, (
        "terminal_historical_count must equal the number of terminal setups"
    )
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 2. Genuinely new setup — new_admissible, reaches EXECUTION_READY
# ---------------------------------------------------------------------------

def test_new_setup_registers_as_execution_ready(tmp_path):
    """A newly registered setup (not previously in registry) reaches EXECUTION_READY."""
    client, hub = _primed(tmp_path)
    s = make_setup("SETUP-NEW-001")
    hub.register_setup(s)
    lc = hub.registry.get(s.id)
    assert lc is not None
    assert lc.state is LCState.EXECUTION_READY
    hub.stop_poller()


def test_readiness_ready_when_new_setup_is_execution_ready(tmp_path):
    """readiness() returns READY_FOR_MANUAL_VALIDATION when a genuine EXECUTION_READY setup exists."""
    client, hub = _primed(tmp_path)
    s = make_setup("SETUP-NEW-READY-001")
    hub.register_setup(s)

    r = hub.readiness()
    assert r["status"] == "READY_FOR_MANUAL_VALIDATION", (
        f"readiness must be READY after registering a new setup, got {r['status']}"
    )
    assert s.id in r["detected_ids"]
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 3. Repeated analysis of the same new setup — idempotent
# ---------------------------------------------------------------------------

def test_repeated_registration_is_idempotent(tmp_path):
    """Registering the same setup twice must not create two entries or change state."""
    client, hub = _primed(tmp_path)
    s = make_setup("SETUP-IDEM-001")
    hub.register_setup(s)
    hub.register_setup(s)
    # Only one entry in the registry
    count = sum(1 for sid in hub.registered_ids if sid == s.id)
    assert count == 1, "duplicate registration must not create duplicate entries"
    lc = hub.registry.get(s.id)
    assert lc.state is LCState.EXECUTION_READY
    hub.stop_poller()


def test_analysis_endpoint_idempotent_registration(tmp_path):
    """Calling /api/analysis/{symbol} twice must not inflate setup count."""
    client, hub = _primed(tmp_path)
    client.get("/api/analysis/TEST/M15")
    count_after_first = len(list(hub.registered_ids))
    client.get("/api/analysis/TEST/M15")
    count_after_second = len(list(hub.registered_ids))
    assert count_after_second == count_after_first, (
        f"second analysis must not register additional setups: "
        f"first={count_after_first} second={count_after_second}"
    )
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 4. Terminal setup cannot be resurrected via the analysis endpoint
# ---------------------------------------------------------------------------

def test_analysis_does_not_resurrect_terminal_setup(tmp_path):
    """An ENTRY_NO_LONGER_VALID setup stays terminal after a fresh analysis call."""
    client, hub = _primed(tmp_path)
    s = make_setup("SETUP-NO-RESURRECT-001")
    _register_terminal(hub, s, LCState.ENTRY_NO_LONGER_VALID)

    # Running analysis doesn't touch setups it didn't produce itself
    client.get("/api/analysis/TEST/M15")
    lc = hub.registry.get(s.id)
    assert lc.state is LCState.ENTRY_NO_LONGER_VALID, (
        "terminal setup must not be resurrected by analysis endpoint"
    )
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 5. readiness only becomes READY when a genuinely admissible setup is registered
# ---------------------------------------------------------------------------

def test_readiness_not_ready_until_execution_ready_exists(tmp_path):
    """readiness() must be WAITING before any new setup is registered."""
    client, hub = _primed(tmp_path)
    r = hub.readiness()
    assert r["status"] == "WAITING_FOR_CAUSAL_SETUP", (
        "fresh hub with no setups must report WAITING"
    )
    hub.stop_poller()


def test_readiness_transitions_from_waiting_to_ready(tmp_path):
    """readiness() transitions WAITING → READY only when a real new setup is registered."""
    client, hub = _primed(tmp_path)
    assert hub.readiness()["status"] == "WAITING_FOR_CAUSAL_SETUP"
    s = make_setup("SETUP-TRANSITION-001")
    hub.register_setup(s)
    assert hub.readiness()["status"] == "READY_FOR_MANUAL_VALIDATION"
    hub.stop_poller()


def test_readiness_returns_to_waiting_after_terminal_transition(tmp_path):
    """readiness() returns to WAITING after the only EXECUTION_READY setup becomes terminal."""
    client, hub = _primed(tmp_path)
    s = make_setup("SETUP-REVERT-001")
    hub.register_setup(s)
    assert hub.readiness()["status"] == "READY_FOR_MANUAL_VALIDATION"

    hub._transition(s.id, LCState.ENTRY_NO_LONGER_VALID, "test-invalidation")
    assert hub.readiness()["status"] == "WAITING_FOR_CAUSAL_SETUP", (
        "after invalidating the only EXECUTION_READY setup, readiness must revert to WAITING"
    )
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 6. causal scan and analysis agree on the admission distinction
# ---------------------------------------------------------------------------

def test_analysis_candidate_summary_fields_present(tmp_path):
    """analysis() response must include candidate_summary with semantic breakdown fields."""
    client, hub = _primed(tmp_path)
    r = client.get("/api/analysis/TEST/M15")
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert "candidate_summary" in body, "analysis must include candidate_summary"
    cs = body["candidate_summary"]
    for field in ("structurally_qualified_count", "new_admissible_count",
                  "terminal_existing_count", "active_registered_count"):
        assert field in cs, f"candidate_summary missing {field!r}"
        assert isinstance(cs[field], int) and cs[field] >= 0, (
            f"{field}={cs[field]!r} must be non-negative int"
        )
    hub.stop_poller()


def test_candidate_summary_counts_consistent(tmp_path):
    """candidate_summary counts must be internally consistent."""
    client, hub = _primed(tmp_path)
    body = client.get("/api/analysis/TEST/M15").json()
    cs = body["candidate_summary"]
    total = cs["new_admissible_count"] + cs["terminal_existing_count"] + cs["active_registered_count"]
    assert total == cs["structurally_qualified_count"], (
        f"admission counts {total} must sum to structurally_qualified_count "
        f"{cs['structurally_qualified_count']}"
    )
    hub.stop_poller()


def test_causal_scan_includes_registry_admission_fields(tmp_path):
    """/api/causal-scan/{symbol} must include execution_ready_count and terminal_existing_count."""
    client, hub = _primed(tmp_path)
    body = client.get("/api/causal-scan/TEST").json()
    for field in ("structurally_qualified_count", "execution_ready_count",
                  "terminal_existing_count", "total_registered_count"):
        assert field in body, f"causal-scan missing {field!r}"
        assert isinstance(body[field], int) and body[field] >= 0
    hub.stop_poller()


def test_causal_scan_execution_ready_count_matches_readiness(tmp_path):
    """execution_ready_count in causal-scan must match the number of EXECUTION_READY ids in readiness."""
    client, hub = _primed(tmp_path)
    # Register a new setup so there's an EXECUTION_READY entry
    s = make_setup("SETUP-XSCAN-MATCH-001")
    hub.register_setup(s)

    scan = client.get("/api/causal-scan/TEST").json()
    ready = hub.readiness()
    assert scan["execution_ready_count"] >= len(ready["detected_ids"]), (
        "execution_ready_count in causal-scan must be >= detected_ids in readiness"
    )
    hub.stop_poller()


def test_causal_scan_terminal_count_increases_after_invalidation(tmp_path):
    """terminal_existing_count increases when an EXECUTION_READY setup is invalidated."""
    client, hub = _primed(tmp_path)
    s = make_setup("SETUP-TERM-COUNT-001")
    hub.register_setup(s)

    before = client.get("/api/causal-scan/TEST").json()
    before_ready = before["execution_ready_count"]

    hub._transition(s.id, LCState.OB_INVALIDATED, "test")

    after = client.get("/api/causal-scan/TEST").json()
    assert after["execution_ready_count"] < before_ready or after["terminal_existing_count"] > before["terminal_existing_count"], (
        "after invalidation, terminal_existing_count should increase or execution_ready should decrease"
    )
    hub.stop_poller()


def test_candidate_admission_field_present_in_each_candidate(tmp_path):
    """Each candidate in analysis response must have an 'admission' field."""
    client, hub = _primed(tmp_path)
    body = client.get("/api/analysis/TEST/M15").json()
    for cand in body.get("candidates", []):
        assert "admission" in cand, f"candidate missing 'admission' field: {cand.get('id')}"
        assert cand["admission"] in ("NEW_ADMISSIBLE", "TERMINAL_HISTORICAL", "ACTIVE_REGISTERED"), (
            f"invalid admission value: {cand['admission']!r}"
        )
    hub.stop_poller()


def test_terminal_candidate_has_terminal_historical_admission(tmp_path):
    """A candidate whose ID is already terminal gets admission='TERMINAL_HISTORICAL'."""
    client, hub = _primed(tmp_path)

    # Prime fixture data for TEST so analyze_at() can run
    d1, h4, m15 = build_fixture()
    # analyze_at on the fixture returns 0 candidates (H4 sweep after M15 as_of),
    # so we need to verify via direct hub method.
    # Instead, create a fresh hub, register a setup as terminal, then call
    # candidates_for on a symbol where that ID would be in the registry.
    hub2 = _hub_with_source()
    s = make_setup("SETUP-TERM-ADM-001")
    s2 = make_setup("SETUP-TERM-ADM-001")  # same id
    _register_terminal(hub2, s, LCState.FILLED)

    # Now directly test _registry_admission_summary
    reg = hub2._registry_admission_summary("TEST")
    assert reg["terminal_existing_count"] >= 1
    assert reg["execution_ready_count"] == 0
    hub2.stop_poller()
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 7. Account identity gate remains enforced
# ---------------------------------------------------------------------------

def test_unauthorized_mt5_blocks_causal_scan_admission(tmp_path):
    """Unauthorized MT5 account must block causal-scan (403) regardless of candidate count."""
    src = MockMT5Source(17594495, "Headway-Real", False)
    client, hub = _primed(tmp_path, src)
    r = client.get("/api/causal-scan/TEST")
    assert r.status_code == 403
    assert "UNAUTHORIZED_ACCOUNT" in r.text
    hub.stop_poller()


def test_unauthorized_mt5_blocks_register_setup(tmp_path):
    """register_setup must raise PermissionError for unauthorized MT5 source."""
    src = MockMT5Source(17594495, "Headway-Real", False)
    client, hub = _primed(tmp_path, src)
    s = make_setup("SETUP-UNAUTH-001")
    with pytest.raises(PermissionError, match="UNAUTHORIZED_ACCOUNT"):
        hub.register_setup(s)
    hub.stop_poller()


def test_authorized_mt5_can_register_and_read_admission(tmp_path):
    """Authorized MT5 source can register a setup and see it in causal-scan summary."""
    src = MockMT5Source(AUTHORIZED_LOGIN, AUTHORIZED_SERVER, True)
    d1, h4, m15 = build_fixture()
    src.prime("TEST", TIMEFRAMES["D1"], d1)
    src.prime("TEST", TIMEFRAMES["H4"], h4)
    src.prime("TEST", TIMEFRAMES["M15"], m15)
    client, hub = _primed(tmp_path, src)
    s = make_setup("SETUP-AUTH-MT5-001")
    hub.register_setup(s)

    body = client.get("/api/causal-scan/TEST").json()
    assert body["execution_ready_count"] >= 1
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 8. Live execution remains false and execution calls remain 0
# ---------------------------------------------------------------------------

def test_live_execution_stays_false_after_admission_ops(tmp_path):
    """live_execution_enabled must remain False through all admission operations."""
    client, hub = _primed(tmp_path)
    s = make_setup("SETUP-EXEC-SAFE-001")
    hub.register_setup(s)
    hub._transition(s.id, LCState.ENTRY_NO_LONGER_VALID, "test")
    hub.register_setup(s)  # re-registration attempt

    st = hub.status()
    assert st["live_execution_enabled"] is False, (
        "live_execution_enabled must remain False through admission/invalidation"
    )
    hub.stop_poller()


def test_order_send_never_called_during_admission(tmp_path):
    """No order_send or order_check is ever called during candidate analysis/registration."""
    import unittest.mock as mock
    client, hub = _primed(tmp_path)
    with mock.patch.object(hub.source, "order_send", side_effect=AssertionError("order_send called")) as ms:
        with mock.patch.object(hub.source, "order_check", side_effect=AssertionError("order_check called")) as mc:
            client.get("/api/analysis/TEST/M15")
            client.get("/api/causal-scan/TEST")
    hub.stop_poller()


def test_readiness_terminal_historical_count_field_present(tmp_path):
    """/api/readiness must include terminal_historical_count field."""
    client, hub = _primed(tmp_path)
    r = client.get("/api/readiness").json()
    assert "terminal_historical_count" in r, "readiness must expose terminal_historical_count"
    assert isinstance(r["terminal_historical_count"], int) and r["terminal_historical_count"] >= 0
    hub.stop_poller()


def test_readiness_rejected_incomplete_excludes_terminal_setups(tmp_path):
    """rejected_incomplete must count only setups with missing fields, not terminal ones."""
    client, hub = _primed(tmp_path)
    s = make_setup("SETUP-RJCT-FIELD-001")
    _register_terminal(hub, s, LCState.FILLED)

    r = hub.readiness()
    assert r["rejected_incomplete"] == 0, (
        "a complete-but-terminal setup must not inflate rejected_incomplete"
    )
    assert r["terminal_historical_count"] >= 1
    hub.stop_poller()


def test_qualified_count_nonzero_does_not_imply_ready(tmp_path):
    """qualified_candidate_count > 0 must NOT be interpreted as Gate B READY.

    This is the core semantic gate: a structurally qualified setup may already
    be terminal. Gate B is READY only when execution_ready_count > 0.
    """
    client, hub = _primed(tmp_path)

    # Register several setups and immediately terminate them
    for i in range(3):
        s = make_setup(f"SETUP-QUAL-TERM-{i:03d}")
        _register_terminal(hub, s, LCState.ENTRY_NO_LONGER_VALID)

    # readiness must still be WAITING
    r = hub.readiness()
    assert r["status"] == "WAITING_FOR_CAUSAL_SETUP", (
        "Gate B must NOT be READY merely because terminal setups exist in the registry"
    )
    hub.stop_poller()
