"""Read-only runtime fingerprint for the SMC Engine workstation.

Exposes source identity (git commit, dirty status, file hashes), process
metadata (PID, Python executable, startup time), and loaded module paths.

No broker primitives. No execution. No credentials.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Repo root: four levels up from this file (web/runtime.py → web → smc_engine → src → repo)
_REPO_ROOT = Path(__file__).parent.parent.parent.parent.resolve()
# Engine package root: smc_engine/
_ENGINE_PKG = Path(__file__).parent.parent.resolve()

# Files to fingerprint, relative to _ENGINE_PKG
FINGERPRINT_FILES = {
    "setup": "setup.py",
    "causal": "causal.py",
    "poi": "poi.py",
    "lifecycle": "lifecycle.py",
    "structure": "structure.py",
    "execution_structure": "execution_structure.py",
    "hub": "web/hub.py",
    "history": "web/history.py",
}

# Module names for loaded-module path lookup.
# Both bare (production: python -m smc_engine.web) and src-prefixed
# (development: python -m pytest from repo root) names are checked.
_MODULE_NAMES = {
    "setup": ("smc_engine.setup", "src.smc_engine.setup"),
    "causal": ("smc_engine.causal", "src.smc_engine.causal"),
    "poi": ("smc_engine.poi", "src.smc_engine.poi"),
    "lifecycle": ("smc_engine.lifecycle", "src.smc_engine.lifecycle"),
    "structure": ("smc_engine.structure", "src.smc_engine.structure"),
    "execution_structure": ("smc_engine.execution_structure", "src.smc_engine.execution_structure"),
    "hub": ("smc_engine.web.hub", "src.smc_engine.web.hub"),
    "history": ("smc_engine.web.history", "src.smc_engine.web.history"),
}

# Set once when the first EngineHub is created (call record_startup() from hub.__init__)
_SERVER_STARTED_AT: Optional[float] = None
# Revision ACTUALLY loaded by this process, captured at startup. Unlike reading
# git HEAD at request time, this cannot be changed by later checkouts/pulls, so
# an obsolete running process can be detected (loaded != current).
_STARTUP_COMMIT: Optional[str] = None
_STARTUP_HASHES: dict = {}


def record_startup() -> float:
    """Record process start time and the loaded source revision.

    Idempotent — only the first call has effect. Captures the revision and file
    hashes at startup so diagnostics can detect an OBSOLETE running process
    (loaded code != code on disk) instead of reporting the current HEAD.
    """
    global _SERVER_STARTED_AT, _STARTUP_COMMIT, _STARTUP_HASHES
    if _SERVER_STARTED_AT is None:
        try:
            _STARTUP_COMMIT = git_info()["git_commit"]
        except Exception:
            _STARTUP_COMMIT = None
        try:
            _STARTUP_HASHES = source_fingerprints()
        except Exception:
            _STARTUP_HASHES = {}
        _SERVER_STARTED_AT = time.time()
    return _SERVER_STARTED_AT


def file_sha256(path: Path) -> str:
    """First 12 hex characters of SHA-256 of a file, or 'error:<reason>'."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except Exception as exc:
        return f"error:{exc}"


def git_info(repo: Optional[Path] = None) -> dict:
    """Git commit and dirty-file list. Never modifies the repo."""
    root = repo or _REPO_ROOT

    def _run(*args: str) -> str:
        return subprocess.check_output(
            list(args), cwd=root, text=True,
            stderr=subprocess.DEVNULL, timeout=5,
        ).strip()

    try:
        commit = _run("git", "rev-parse", "HEAD")[:7]
    except Exception:
        commit = "unknown"

    try:
        lines = _run("git", "status", "--porcelain").splitlines()
        dirty = bool(lines)
        modified = [ln[3:].strip() for ln in lines if ln.strip()]
    except Exception:
        dirty, modified = False, []

    return {"git_commit": commit, "git_dirty": dirty, "modified_files": modified}


def source_fingerprints() -> dict[str, str]:
    """Current SHA-256[:12] of each critical file on disk."""
    return {
        name: file_sha256(_ENGINE_PKG / rel)
        for name, rel in FINGERPRINT_FILES.items()
    }


def loaded_module_paths() -> dict[str, str]:
    """Filesystem path of each loaded module (or 'not_loaded' if absent from sys.modules).

    Checks both bare names (production: ``python -m smc_engine.web``) and
    src-prefixed names (development: ``python -m pytest`` from repo root).
    """
    result: dict[str, str] = {}
    for key, candidates in _MODULE_NAMES.items():
        mod = None
        for mod_name in candidates:
            mod = sys.modules.get(mod_name)
            if mod is not None:
                break
        result[key] = getattr(mod, "__file__", "not_loaded") if mod else "not_loaded"
    return result


def build_fingerprint(pid: Optional[int] = None, account_mode: str = "DEMO",
                      account_login: Optional[int] = None) -> dict:
    """Complete runtime fingerprint. Read-only — no broker or execution calls."""
    try:
        import pandas as pd
        started_iso: Optional[str] = (
            pd.Timestamp(_SERVER_STARTED_AT, unit="s", tz="UTC").isoformat()
            if _SERVER_STARTED_AT else None
        )
    except Exception:
        started_iso = None

    gi = git_info()
    disk = source_fingerprints()
    loaded = loaded_module_paths()

    # Detect staleness two ways: (a) mtime after startup (heuristic O/S-level),
    # and (b) startup hash != current disk hash (definitive for fingerprinted
    # files). A stale process = code on disk changed since it started.
    stale: set = set()
    if _SERVER_STARTED_AT:
        for name, rel in FINGERPRINT_FILES.items():
            path = _ENGINE_PKG / rel
            try:
                if path.stat().st_mtime > _SERVER_STARTED_AT:
                    stale.add(rel)
            except Exception:
                pass
    for name, rel in FINGERPRINT_FILES.items():
        sh = (_STARTUP_HASHES or {}).get(name)
        if sh and disk.get(name) != sh:
            stale.add(rel)

    loaded_commit = _STARTUP_COMMIT
    stale_code = bool(stale) or (loaded_commit is not None and loaded_commit != gi["git_commit"])

    return {
        "git_commit": gi["git_commit"],              # current on-disk HEAD
        "loaded_git_commit": loaded_commit,          # revision loaded at startup
        "git_dirty": gi["git_dirty"],
        "modified_files": gi["modified_files"],
        "server_started_at": started_iso,
        "python_executable": sys.executable,
        "pid": pid if pid is not None else os.getpid(),
        "module_paths": loaded,
        "source_hashes": disk,
        "startup_source_hashes": dict(_STARTUP_HASHES or {}),
        "files_modified_after_startup": sorted(stale),
        "stale_code": stale_code,
        "live_execution_enabled": False,
        "account_mode": account_mode,
        "account_login": account_login,
        # server_version = the LOADED revision, so a stale process never reports
        # the current HEAD as the version it is running.
        "server_version": loaded_commit or gi["git_commit"],
    }
