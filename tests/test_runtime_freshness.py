"""Phase 14D — stale-process detection diagnostics.

The runtime fingerprint must report the revision a process ACTUALLY LOADED
(captured at startup), not the current on-disk HEAD, so an obsolete running
process can be identified. Regression for the incident where the running server
reported the current commit while executing old code.
"""
from __future__ import annotations

from src.smc_engine.web import runtime


def _reset():
    runtime._SERVER_STARTED_AT = None
    runtime._STARTUP_COMMIT = None
    runtime._STARTUP_HASHES = {}


def test_fingerprint_reports_loaded_revision(monkeypatch):
    _reset()
    monkeypatch.setattr(runtime, "git_info",
                        lambda repo=None: {"git_commit": "deadbee", "git_dirty": False,
                                           "modified_files": []})
    monkeypatch.setattr(runtime, "source_fingerprints", lambda: {"hub": "aaa"})
    runtime.record_startup()
    fp = runtime.build_fingerprint(pid=1)
    assert fp["loaded_git_commit"] == "deadbee"
    assert fp["git_commit"] == "deadbee"
    assert fp["stale_code"] is False
    _reset()


def test_stale_process_detected_when_disk_changes(monkeypatch):
    _reset()
    monkeypatch.setattr(runtime, "git_info",
                        lambda repo=None: {"git_commit": "oldcode", "git_dirty": False,
                                           "modified_files": []})
    monkeypatch.setattr(runtime, "source_fingerprints", lambda: {"hub": "aaa"})
    runtime.record_startup()                       # loaded: oldcode, hub=aaa

    # disk changes after startup (new checkout)
    monkeypatch.setattr(runtime, "git_info",
                        lambda repo=None: {"git_commit": "newcode", "git_dirty": True,
                                           "modified_files": ["x"]})
    monkeypatch.setattr(runtime, "source_fingerprints", lambda: {"hub": "bbb"})

    fp = runtime.build_fingerprint(pid=1)
    assert fp["loaded_git_commit"] == "oldcode"    # what this process loaded
    assert fp["git_commit"] == "newcode"           # what is on disk now
    assert fp["server_version"] == "oldcode"       # reports LOADED, not disk HEAD
    assert fp["stale_code"] is True
    assert "web/hub.py" in fp["files_modified_after_startup"]
    _reset()


def test_fingerprint_reports_runtime_identity_fields():
    _reset()
    runtime.record_startup()
    fp = runtime.build_fingerprint(pid=4242)
    for field in ("pid", "server_started_at", "python_executable", "module_paths",
                  "source_hashes", "loaded_git_commit", "stale_code",
                  "files_modified_after_startup", "live_execution_enabled"):
        assert field in fp, field
    assert fp["live_execution_enabled"] is False
    _reset()
