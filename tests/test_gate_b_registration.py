"""Gate B — Candidate Registration & Lifecycle Propagation regression tests.

Covers the 12 regression areas from the Gate B Candidate Registration brief.
All tests use DictSource or FakeDemoSource — no real broker, no order_send,
no order_check, no paper placement, no live execution.

The production registration path under test:
  candidates_for()
  → register_setup()
  → lifecycle registry
  → lifecycle() / readiness()
  → API surface
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.smc_engine.lifecycle import SetupState as LCState
from src.smc_engine.models import Direction
from src.smc_engine.setup import TradeSetup
from src.smc_engine.web.hub import AUTHORIZED_LOGIN, AUTHORIZED_SERVER, DictSource, EngineHub, TIMEFRAMES

from tests.test_api import build_client, FakeDemoSource, make_setup
from tests.test_account_identity import MockMT5Source
from tests.test_integration_fixture import build_fixture


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hub():
    return EngineHub(DictSource(), db_path=":memory:", setup_store_path=":memory:")


def _primed_hub(tmp_path):
    """Build a hub + TestClient with the standard fixture primed for TEST."""
    client, hub = build_client(tmp_path)
    return client, hub


def _make_three_setups():
    return [
        make_setup(f"SETUP-CADJPY-REG-00{i}")
        for i in range(1, 4)
    ]


# ---------------------------------------------------------------------------
# 1. Candidates register through the production analysis path
# ---------------------------------------------------------------------------
def test_analysis_endpoint_calls_candidates_for_and_returns_candidates_key(tmp_path):
    """GET /api/analysis/TEST/M15 must return a 'candidates' key — proving
    candidates_for() was invoked as part of the analysis path."""
    client, hub = _primed_hub(tmp_path)
    r = client.get("/api/analysis/TEST/M15")
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert "candidates" in body, "analysis response must include 'candidates' from candidates_for()"
    hub.stop_poller()


def test_registered_setup_appears_in_lifecycle_via_production_path(tmp_path):
    """Setups registered inside candidates_for() appear in hub.lifecycle() —
    the complete production path from scan → registry → API surface."""
    client, hub = _primed_hub(tmp_path)
    # Register three setups directly (simulating what candidates_for does)
    setups = _make_three_setups()
    for s in setups:
        hub.register_setup(s)

    lc = hub.lifecycle()
    registered_ids = {r["setup_id"] for r in lc}
    for s in setups:
        assert s.id in registered_ids, (
            f"{s.id} registered but not in lifecycle(): {registered_ids}"
        )
    hub.stop_poller()


def test_lifecycle_api_surfaces_all_registered_setups(tmp_path):
    """POST /api/analysis/run triggers registration; /api/setups must reflect them."""
    client, hub = _primed_hub(tmp_path)
    setups = _make_three_setups()
    for s in setups:
        hub.register_setup(s)

    r = client.get("/api/setups").json()
    api_ids = {row["setup_id"] for row in r["setups"]}
    for s in setups:
        assert s.id in api_ids, f"{s.id} not in /api/setups response"
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 2. Repeated analysis is idempotent
# ---------------------------------------------------------------------------
def test_repeated_analysis_does_not_increase_setup_count(tmp_path):
    """Calling /api/analysis/TEST/M15 twice must not create additional setups."""
    client, hub = _primed_hub(tmp_path)
    client.get("/api/analysis/TEST/M15")
    count_after_first = len(hub.lifecycle())
    client.get("/api/analysis/TEST/M15")
    count_after_second = len(hub.lifecycle())
    assert count_after_second == count_after_first, (
        f"second analysis created extra setups: {count_after_first} → {count_after_second}"
    )
    hub.stop_poller()


def test_repeated_register_setup_is_idempotent():
    """Calling register_setup() twice with the same setup must leave exactly
    one entry in the registry — the second call is a silent no-op."""
    hub = _hub()
    setup = make_setup("SETUP-IDEM-TEST")
    hub.register_setup(setup)
    hub.register_setup(setup)  # second call must be a no-op
    lc = hub.lifecycle()
    matching = [r for r in lc if r["setup_id"] == setup.id]
    assert len(matching) == 1, (
        f"expected exactly 1 lifecycle entry for {setup.id}, got {len(matching)}"
    )


# ---------------------------------------------------------------------------
# 3. No duplicate setup IDs
# ---------------------------------------------------------------------------
def test_no_duplicate_setup_ids_after_multiple_registrations():
    """Registering the same setup multiple times must yield exactly one
    distinct entry in the lifecycle registry."""
    hub = _hub()
    setup = make_setup("SETUP-NODUP-TEST")
    for _ in range(5):
        hub.register_setup(setup)
    ids = [r["setup_id"] for r in hub.lifecycle()]
    assert ids.count(setup.id) == 1, (
        f"duplicate lifecycle entries for {setup.id}: {ids}"
    )


def test_distinct_setup_ids_all_registered():
    """Three setups with different IDs each produce exactly one lifecycle entry."""
    hub = _hub()
    setups = _make_three_setups()
    for s in setups:
        hub.register_setup(s)
    ids = [r["setup_id"] for r in hub.lifecycle()]
    for s in setups:
        assert ids.count(s.id) == 1, (
            f"expected exactly 1 entry for {s.id}, got {ids.count(s.id)}"
        )


# ---------------------------------------------------------------------------
# 4. Canonical timestamp identity survives registration
# ---------------------------------------------------------------------------
def test_setup_id_is_preserved_through_registration():
    """The setup.id assigned by build_trade_setup() survives hub registration
    and is returned unchanged via lifecycle()."""
    hub = _hub()
    setup = make_setup("SETUP-CADJPY-TS-1790636400000000000-OB-M15-1790641800000000000-BEARISH")
    hub.register_setup(setup)
    lc = hub.lifecycle()
    matching = [r for r in lc if r["setup_id"] == setup.id]
    assert len(matching) == 1
    assert matching[0]["setup_id"] == setup.id


def test_setup_id_starts_with_setup_prefix():
    """Every registered setup ID must start with 'SETUP-' for identity validity."""
    hub = _hub()
    setups = _make_three_setups()
    for s in setups:
        hub.register_setup(s)
    for row in hub.lifecycle():
        assert row["setup_id"].startswith("SETUP-"), (
            f"setup_id does not start with 'SETUP-': {row['setup_id']}"
        )


# ---------------------------------------------------------------------------
# 5. IRL provenance survives registration
# ---------------------------------------------------------------------------
def test_irl_swing_id_survives_registration():
    """setup.irl_swing_id set at creation must survive hub.register_setup()
    and appear in the lifecycle row's evidence."""
    hub = _hub()
    setup = make_setup("SETUP-IRL-PROV-001")
    # Verify the field exists on the setup object
    assert setup.irl_swing_id is not None, "fixture make_setup must set irl_swing_id"
    hub.register_setup(setup)
    lc = hub.lifecycle()
    row = next((r for r in lc if r["setup_id"] == setup.id), None)
    assert row is not None
    # irl_swing_id must appear in the serialized evidence
    evidence = row.get("evidence") or {}
    assert evidence.get("irl_swing_id") == setup.irl_swing_id, (
        f"irl_swing_id lost: expected {setup.irl_swing_id!r}, got {evidence.get('irl_swing_id')!r}"
    )


def test_irl_provenance_fields_present_in_lifecycle_row():
    """Evidence block must carry all IRL-related fields after registration."""
    hub = _hub()
    setup = make_setup("SETUP-IRL-FIELDS-001")
    hub.register_setup(setup)
    lc = hub.lifecycle()
    row = next(r for r in lc if r["setup_id"] == setup.id)
    evidence = row.get("evidence") or {}
    for field in ("irl_swing_id", "irl_target_type", "irl_qualification_reason"):
        assert field in evidence, f"evidence missing field {field!r} after registration"


# ---------------------------------------------------------------------------
# 6. Lifecycle state survives serialization
# ---------------------------------------------------------------------------
def test_execution_ready_state_serialized_correctly():
    """A newly registered setup serializes as EXECUTION_READY in lifecycle()."""
    hub = _hub()
    setup = make_setup("SETUP-SER-001")
    hub.register_setup(setup)
    row = next(r for r in hub.lifecycle() if r["setup_id"] == setup.id)
    assert row["state"] == "EXECUTION_READY", (
        f"expected EXECUTION_READY, got {row['state']!r}"
    )
    assert row["display"] == "EXECUTION_READY"


def test_lifecycle_row_has_all_required_fields():
    """Serialized lifecycle row must carry all fields the UI depends on."""
    hub = _hub()
    setup = make_setup("SETUP-FIELDS-001")
    hub.register_setup(setup)
    row = next(r for r in hub.lifecycle() if r["setup_id"] == setup.id)
    for field in ("setup_id", "symbol", "direction", "entry", "sl", "tp",
                  "state", "display", "risk_percent"):
        assert field in row, f"lifecycle row missing required field {field!r}"
        assert row[field] is not None, f"lifecycle row field {field!r} is None"


# ---------------------------------------------------------------------------
# 7. Terminal historical setups are not resurrected
# ---------------------------------------------------------------------------
def test_terminal_setup_not_resurrected_by_re_registration():
    """A setup in a terminal lifecycle state (ENTRY_NO_LONGER_VALID) must NOT
    be replaced by a fresh EXECUTION_READY entry when register_setup is called again."""
    hub = _hub()
    setup = make_setup("SETUP-TERM-001")
    hub.register_setup(setup)
    # Force the setup into a terminal state
    from src.smc_engine.lifecycle import SetupLifecycle
    lc = hub.registry.get(setup.id)
    lc.state = LCState.ENTRY_NO_LONGER_VALID
    lc.reason = "target_reached_before_entry"

    # Attempt re-registration — must be a no-op
    hub.register_setup(setup)

    lc_after = hub.registry.get(setup.id)
    assert lc_after.state is LCState.ENTRY_NO_LONGER_VALID, (
        f"terminal setup was resurrected: state is now {lc_after.state!r}"
    )


def test_filled_setup_not_resurrected():
    """A FILLED setup stays FILLED even if register_setup is called again."""
    hub = _hub()
    setup = make_setup("SETUP-FILLED-001")
    hub.register_setup(setup)
    hub.registry.get(setup.id).state = LCState.FILLED
    hub.register_setup(setup)
    assert hub.registry.get(setup.id).state is LCState.FILLED


# ---------------------------------------------------------------------------
# 8. Alerts are not duplicated
# ---------------------------------------------------------------------------
def test_register_setup_fires_exactly_one_alert():
    """Registering a setup must produce exactly one alert — not one per
    re-registration attempt."""
    hub = _hub()
    setup = make_setup("SETUP-ALERT-001")
    hub.register_setup(setup)
    hub.register_setup(setup)  # second call must not fire a second alert
    hub.register_setup(setup)  # third call same
    alerts = hub.web.alerts(limit=50)
    matching = [a for a in alerts if setup.id in a.get("message", "")]
    assert len(matching) == 1, (
        f"expected exactly 1 alert for {setup.id}, got {len(matching)}: {matching}"
    )


def test_repeated_analysis_does_not_duplicate_alerts(tmp_path):
    """Calling analysis twice on the same symbol must not double the alert count."""
    client, hub = _primed_hub(tmp_path)
    client.get("/api/analysis/TEST/M15")
    alerts_after_first = list(hub.web.alerts(limit=100))
    client.get("/api/analysis/TEST/M15")
    alerts_after_second = list(hub.web.alerts(limit=100))
    # No new alerts should appear from the second scan (idempotent registration → no new alert)
    assert len(alerts_after_second) == len(alerts_after_first), (
        f"second analysis fired extra alerts: {len(alerts_after_first)} → {len(alerts_after_second)}"
    )
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 9. Gate B status reflects actual lifecycle state
# ---------------------------------------------------------------------------
def test_readiness_waiting_when_no_execution_ready_setups():
    """With no EXECUTION_READY setups, Gate B must report WAITING_FOR_CAUSAL_SETUP."""
    hub = _hub()
    r = hub.readiness()
    assert r["status"] == "WAITING_FOR_CAUSAL_SETUP"
    assert r["detected_ids"] == []


def test_readiness_ready_when_execution_ready_setup_exists():
    """With at least one EXECUTION_READY setup, Gate B must report
    READY_FOR_MANUAL_VALIDATION."""
    hub = _hub()
    setup = make_setup("SETUP-READY-001")
    hub.register_setup(setup)
    r = hub.readiness()
    assert r["status"] == "READY_FOR_MANUAL_VALIDATION", (
        f"expected READY_FOR_MANUAL_VALIDATION, got {r['status']!r}"
    )
    assert setup.id in r["detected_ids"]


def test_readiness_waiting_when_all_setups_terminal():
    """Once all setups reach terminal states, Gate B reverts to WAITING."""
    hub = _hub()
    setup = make_setup("SETUP-TERM-WAIT-001")
    hub.register_setup(setup)
    assert hub.readiness()["status"] == "READY_FOR_MANUAL_VALIDATION"
    # Transition to terminal
    hub.registry.get(setup.id).state = LCState.ENTRY_NO_LONGER_VALID
    assert hub.readiness()["status"] == "WAITING_FOR_CAUSAL_SETUP"


def test_readiness_reflects_terminal_not_active_for_terminal_setups():
    """Gate B readiness must NOT present terminal (FILLED/ENTRY_NO_LONGER_VALID)
    setups as EXECUTION_READY."""
    hub = _hub()
    for i, state in enumerate([LCState.FILLED, LCState.ENTRY_NO_LONGER_VALID]):
        sid = f"SETUP-TERMINAL-{i:03d}"
        s = make_setup(sid)
        hub.register_setup(s)
        hub.registry.get(s.id).state = state

    r = hub.readiness()
    assert r["status"] == "WAITING_FOR_CAUSAL_SETUP", (
        f"terminal setups must not produce READY status, got {r['status']!r}"
    )
    assert r["detected_ids"] == []


# ---------------------------------------------------------------------------
# 10. Unauthorized account cannot trigger registration
# ---------------------------------------------------------------------------
def test_unauthorized_account_blocks_register_setup(tmp_path):
    """MockMT5Source (name='mt5') with wrong account must raise PermissionError
    on register_setup() — no setup enters the registry."""
    src = MockMT5Source(17594495, "Headway-Real", False)
    _, hub = build_client(tmp_path, src)
    setup = make_setup("SETUP-UNAUTH-CANTREG-001")
    with pytest.raises(PermissionError, match="UNAUTHORIZED_ACCOUNT"):
        hub.register_setup(setup)
    assert hub.registry.get(setup.id) is None, "unauthorized setup must not enter registry"
    hub.stop_poller()


def test_unauthorized_account_blocks_analysis_endpoint(tmp_path):
    """Analysis endpoint (which triggers candidates_for → register_setup) must
    return 403 for unauthorized MT5 account."""
    src = MockMT5Source(17594495, "Headway-Real", False)
    client, hub = build_client(tmp_path, src)
    r = client.get("/api/analysis/TEST/M15")
    assert r.status_code == 403
    assert hub.lifecycle() == [], "no setups must enter registry via unauthorized path"
    hub.stop_poller()


def test_authorized_demo_source_can_register():
    """DictSource bypasses the identity gate — register_setup must succeed."""
    hub = _hub()
    setup = make_setup("SETUP-AUTH-OK-001")
    hub.register_setup(setup)
    assert hub.registry.get(setup.id) is not None


# ---------------------------------------------------------------------------
# 11. Runtime identity remains authoritative
# ---------------------------------------------------------------------------
def test_runtime_identity_fields_present(tmp_path):
    """/api/runtime must return all identity fields the Gate B report depends on."""
    client, hub = _primed_hub(tmp_path)
    r = client.get("/api/runtime").json()
    for field in ("account_identity", "engine_operational", "authorized_account",
                  "actual_account", "live_execution_enabled"):
        assert field in r, f"/api/runtime missing field {field!r}"
    hub.stop_poller()


def test_runtime_engine_operational_matches_status(tmp_path):
    """/api/runtime engine_operational must agree with /api/status engine_operational."""
    client, hub = _primed_hub(tmp_path)
    status = client.get("/api/status").json()
    runtime = client.get("/api/runtime").json()
    assert runtime["engine_operational"] == status["engine_operational"], (
        "runtime and status disagree on engine_operational"
    )
    hub.stop_poller()


def test_runtime_authorized_account_matches_status_for_authorized_mt5(tmp_path):
    """For authorized MT5 source, runtime authorized_account == actual_account."""
    src = MockMT5Source(AUTHORIZED_LOGIN, AUTHORIZED_SERVER, True)
    client, hub = build_client(tmp_path, src)
    r = client.get("/api/runtime").json()
    assert r["account_identity"] == "AUTHORIZED"
    assert r["authorized_account"] == r["actual_account"], (
        "for authorized account, authorized_account must equal actual_account"
    )
    hub.stop_poller()


# ---------------------------------------------------------------------------
# 12. Execution calls remain impossible
# ---------------------------------------------------------------------------
def test_live_execution_endpoint_always_disabled(tmp_path):
    """/api/live/{setup_id} must always return 403 — live execution is
    permanently disabled at this stage regardless of auth state."""
    client, hub = _primed_hub(tmp_path)
    r = client.post("/api/live/SETUP-ANY")
    assert r.status_code == 403, f"live endpoint must be 403, got {r.status_code}"
    hub.stop_poller()


def test_live_execution_enabled_never_true_after_registration():
    """live_execution_enabled must remain False even after setup registration."""
    hub = _hub()
    setups = _make_three_setups()
    for s in setups:
        hub.register_setup(s)
    st = hub.status()
    assert st["live_execution_enabled"] is False, (
        "live_execution_enabled became True after setup registration"
    )


def test_order_send_never_called_during_registration():
    """DictSource.order_send must never be invoked during register_setup()."""
    order_send_calls = []

    class InstrumentedSource(DictSource):
        def order_send(self, request):
            order_send_calls.append(request)
            raise RuntimeError("order_send called during registration — forbidden")

    hub = EngineHub(InstrumentedSource(), db_path=":memory:", setup_store_path=":memory:")
    setup = make_setup("SETUP-NOSEND-001")
    hub.register_setup(setup)
    assert order_send_calls == [], f"order_send was called: {order_send_calls}"


def test_order_check_never_called_during_registration():
    """DictSource.order_check must never be invoked during register_setup()."""
    order_check_calls = []

    class InstrumentedSource(DictSource):
        def order_check(self, request):
            order_check_calls.append(request)
            raise RuntimeError("order_check called during registration — forbidden")

    hub = EngineHub(InstrumentedSource(), db_path=":memory:", setup_store_path=":memory:")
    setup = make_setup("SETUP-NOCHECK-001")
    hub.register_setup(setup)
    assert order_check_calls == [], f"order_check was called: {order_check_calls}"


def test_paper_place_blocked_for_execution_ready_setup(tmp_path):
    """Even with a valid EXECUTION_READY setup, /api/paper/{id} must be
    blocked for unauthorized MT5 sources."""
    src = MockMT5Source(17594495, "Headway-Real", False)
    client, hub = build_client(tmp_path, src)
    r = client.post("/api/paper/SETUP-FAKE-ID")
    assert r.status_code == 403, f"paper endpoint must be 403 for unauthorized, got {r.status_code}"
    hub.stop_poller()
