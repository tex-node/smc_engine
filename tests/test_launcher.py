"""Launcher slice static tests — the launcher must be convenience only.

Asserts: no credentials anywhere, canonical server entry point preserved,
never triggers execution endpoints, never mass-kills python, and the health
check precedes browser launch.
"""
from pathlib import Path

import pytest

START = Path("start_workstation.bat")
STOP = Path("stop_workstation.bat")


@pytest.fixture()
def start_txt():
    return START.read_text(encoding="utf-8")


def test_files_exist_and_decode_as_plain_ascii():
    for p in (START, STOP):
        assert p.exists(), p
        raw = p.read_bytes()
        raw.decode("ascii")          # double-click safe: no exotic encodings


@pytest.mark.parametrize("token", [
    "MT5_PASSWORD", "MT5_LOGIN", "password", "Password", "PASSWORD",
    "Manager", "477217728", "ProtectedData", "gate_a_cred", ".bin",
    "api_key", "token=",
])
def test_launcher_contains_no_credentials(start_txt, token):
    assert token not in start_txt


@pytest.mark.parametrize("token", ["/api/paper", "/api/live", "order_send",
                                   "TRADE_ACTION"])
def test_launcher_never_touches_execution_or_state_endpoints(start_txt, token):
    assert token not in start_txt


def test_launcher_uses_canonical_validated_server_command(start_txt):
    assert "smc_engine.web.api:create_app" in start_txt
    assert "--factory" in start_txt
    assert r".venv\Scripts\python.exe" in start_txt
    assert "8765" in start_txt                      # default matches app config
    assert "pip install" not in start_txt.lower()   # never installs silently
    assert "python -m venv" not in start_txt.lower()
    assert 'cd /d "C:\\smc_engine"' in start_txt or "cd /d \"%REPO%\"" in start_txt


def test_health_check_before_browser(start_txt):
    curl = start_txt.index("curl")
    browser_open = start_txt.index('start "" "%URL%"')
    assert curl < browser_open                      # readiness polled first
    assert "--max-time" in start_txt                # bounded polling
    assert "goto :err_timeout" in start_txt         # failure path exists
    assert "exit /b 2" in start_txt


def test_already_running_protection(start_txt):
    assert "already running" in start_txt.lower()
    assert ":already" in start_txt


def test_stop_script_is_targeted_not_nuclear(start_txt):
    stop = STOP.read_text(encoding="utf-8")
    assert "/IM python" not in stop                 # never mass-kill python
    assert "/IM python.exe" not in stop
    assert "WINDOWTITLE eq SMC Engine Workstation Server" in stop
