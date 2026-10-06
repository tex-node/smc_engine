"""Launcher slice static tests.

Asserts security invariants across start_workstation.bat and the two
PowerShell helpers (provision + load).  The bat is now a thin wrapper;
the PS scripts carry all logic.
"""
from pathlib import Path

import pytest

START     = Path("start_workstation.bat")
STOP      = Path("stop_workstation.bat")
PROVISION = Path("tools/provision_mt5_credentials.ps1")
LOAD      = Path("tools/load_mt5_credentials.ps1")


@pytest.fixture()
def start_txt():
    return START.read_text(encoding="utf-8")


@pytest.fixture()
def provision_txt():
    return PROVISION.read_text(encoding="utf-8")


@pytest.fixture()
def load_txt():
    return LOAD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# File existence and encoding
# ---------------------------------------------------------------------------

def test_bat_files_exist_and_decode_as_plain_ascii():
    for p in (START, STOP):
        assert p.exists(), p
        p.read_bytes().decode("ascii")          # double-click safe


def test_ps_scripts_exist_and_are_readable():
    for p in (PROVISION, LOAD):
        assert p.exists(), p
        txt = p.read_text(encoding="utf-8")
        assert len(txt) > 50, f"{p} appears empty"


# ---------------------------------------------------------------------------
# Credential safety — nothing sensitive in any launcher file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token", [
    "Manager",          # fragment of the actual credential value
    "70159",            # second fragment of the actual credential value
    "api_key", "token=", "gate_a_cred",
])
def test_no_credential_values_in_bat(start_txt, token):
    assert token not in start_txt


@pytest.mark.parametrize("token", [
    "Manager",          # credential value fragment must never appear
    "70159",
    "api_key", "token=", "gate_a_cred",
])
def test_no_credential_values_in_provision_script(provision_txt, token):
    assert token not in provision_txt


@pytest.mark.parametrize("token", [
    "Manager",
    "70159",
    "api_key", "token=", "gate_a_cred",
])
def test_no_credential_values_in_load_script(load_txt, token):
    assert token not in load_txt


# ---------------------------------------------------------------------------
# Bat file is a thin wrapper — no embedded logic or credentials
# ---------------------------------------------------------------------------

def test_bat_calls_load_ps_script(start_txt):
    assert "load_mt5_credentials.ps1" in start_txt


def test_bat_uses_executionpolicy_bypass(start_txt):
    assert "-ExecutionPolicy Bypass" in start_txt


def test_bat_has_no_plaintext_credential_vars(start_txt):
    for tok in ("MT5_PASSWORD", "MT5_LOGIN=", "MT5_SERVER=",
                "password", "Password", "PASSWORD"):
        assert tok not in start_txt, f"bat must not contain {tok!r}"


# ---------------------------------------------------------------------------
# Provision script — DPAPI encryption, no plaintext storage
# ---------------------------------------------------------------------------

def test_provision_uses_dpapi_protect(provision_txt):
    assert "ProtectedData" in provision_txt
    assert "Protect(" in provision_txt


def test_provision_stores_binary_not_text(provision_txt):
    # WriteAllBytes = binary blob; WriteAllText = plaintext — we want bytes
    assert "WriteAllBytes" in provision_txt
    assert "WriteAllText" not in provision_txt


def test_provision_wipes_plaintext_before_write(provision_txt):
    # The script must clear the plain variable and call GC before write
    assert "$plain = $null" in provision_txt
    assert "GC]::Collect" in provision_txt


def test_provision_uses_secure_read_host(provision_txt):
    assert "AsSecureString" in provision_txt


def test_provision_stores_in_appdata(provision_txt):
    assert "SMC_ENGINE" in provision_txt
    assert ".dpapi" in provision_txt


# ---------------------------------------------------------------------------
# Load script — fail-closed semantics, identity gate, canonical server
# ---------------------------------------------------------------------------

def test_load_uses_dpapi_unprotect(load_txt):
    assert "ProtectedData" in load_txt
    assert "Unprotect(" in load_txt


def test_load_fails_closed_if_store_missing(load_txt):
    # Must check that the DPAPI store file exists before proceeding
    assert "STORE_PATH" in load_txt
    assert "Test-Path" in load_txt


def test_load_validates_authorized_login(load_txt):
    # Must reject accounts whose login != the authorized login
    assert "477217728" in load_txt


def test_load_validates_authorized_server(load_txt):
    assert "Exness-MT5Trial9" in load_txt


def test_load_fails_closed_on_unauthorized_account(load_txt):
    assert "exit 1" in load_txt
    assert "unauthorized" in load_txt.lower()


def test_load_uses_canonical_server_command(load_txt):
    assert "smc_engine.web.api:create_app" in load_txt
    assert "--factory" in load_txt
    assert r".venv\Scripts\python.exe" in load_txt
    assert "8765" in load_txt
    assert "pip install" not in load_txt.lower()


def test_load_passes_secret_via_env_not_args(load_txt):
    # Secret must be set as an env var, never spliced into the argument list
    assert "MT5_PASSWORD" in load_txt
    assert "$env:MT5_PASSWORD = $creds.secret" in load_txt


def test_load_clears_secret_after_launch(load_txt):
    # Must wipe the password env var from the launcher PS session
    assert "$env:MT5_PASSWORD = $null" in load_txt
    assert "$creds.secret     = $null" in load_txt or "$creds.secret = $null" in load_txt


def test_load_server_window_title_matches_stop_script(load_txt):
    # The window title must match what stop_workstation.bat searches for
    assert "SMC Engine Workstation Server" in load_txt


def test_load_polls_readiness_before_browser(load_txt):
    status_idx  = load_txt.index("api/status")
    browser_idx = load_txt.index("Start-Process $URL")
    assert status_idx < browser_idx


def test_load_checks_already_running(load_txt):
    assert "already running" in load_txt.lower()


def test_load_shows_self_diagnostic_table(load_txt):
    for field in ("Credentials", "MT5 Account", "Identity Gate", "Live Execution"):
        assert field in load_txt, f"Self-diagnostic must include: {field!r}"


# ---------------------------------------------------------------------------
# Execution endpoint safety — neither script touches trade endpoints
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tok", ["/api/paper", "/api/live", "order_send", "TRADE_ACTION"])
def test_no_execution_endpoints_in_bat(start_txt, tok):
    assert tok not in start_txt


@pytest.mark.parametrize("tok", ["/api/paper", "/api/live", "order_send", "TRADE_ACTION"])
def test_no_execution_endpoints_in_load_script(load_txt, tok):
    assert tok not in load_txt


@pytest.mark.parametrize("tok", ["/api/paper", "/api/live", "order_send", "TRADE_ACTION"])
def test_no_execution_endpoints_in_provision_script(provision_txt, tok):
    assert tok not in provision_txt


# ---------------------------------------------------------------------------
# Stop script safety (unchanged)
# ---------------------------------------------------------------------------

def test_stop_script_is_targeted_not_nuclear(start_txt):
    stop = STOP.read_text(encoding="utf-8")
    assert "/IM python" not in stop
    assert "/IM python.exe" not in stop
    assert "WINDOWTITLE eq SMC Engine Workstation Server" in stop
