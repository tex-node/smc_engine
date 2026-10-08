"""Serialized writer boundary for the shared GUI SQLite database.

The GUI database is written by WebStore, SetupEventHistory and
OpportunityRepository (and, via the hub, the engine SetupStore) from the
background watcher thread and request threads. SQLite/WAL permits a single
writer; unsynchronized writers produced `database is locked` failures that
collapsed symbol coverage (P1-B).

Design (smallest production-safe): one process-wide re-entrant write lock plus
bounded retry on transient SQLITE_BUSY/SQLITE_LOCKED, with observable counters.
Reads are unaffected and remain concurrent.
"""
from __future__ import annotations

import functools
import sqlite3
import threading
import time
from typing import Any, Callable

# One lock for every writer of the shared GUI database.
SHARED_WRITE_LOCK = threading.RLock()

MAX_RETRIES = 4            # bounded: 1 attempt + up to 4 retries
BASE_DELAY = 0.05          # seconds; deterministic exponential backoff
_LOCK_MARKERS = ("database is locked", "database table is locked", "busy")

_COUNTERS = {
    "db_write_count": 0,
    "db_write_failures": 0,
    "db_lock_retries": 0,
    "db_lock_failures": 0,
    "db_last_write_error": None,
}
_COUNTER_LOCK = threading.Lock()


def _is_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return any(m in msg for m in _LOCK_MARKERS)


def _bump(key: str, amount: int = 1) -> None:
    with _COUNTER_LOCK:
        _COUNTERS[key] += amount


def _set_last_error(value) -> None:
    with _COUNTER_LOCK:
        _COUNTERS["db_last_write_error"] = value


def run_write(fn: Callable[[], Any], *, retries: int = MAX_RETRIES,
              base_delay: float = BASE_DELAY) -> Any:
    """Execute a write under the shared lock with bounded retry.

    A transient lock failure is retried (observable via db_lock_retries); after
    the retry budget is exhausted the failure is recorded
    (db_lock_failures/db_write_failures/db_last_write_error) and re-raised so
    the caller can isolate it per symbol. No infinite loops.
    """
    attempt = 0
    while True:
        with SHARED_WRITE_LOCK:
            try:
                result = fn()
                _bump("db_write_count")
                _set_last_error(None)          # cleared on subsequent success
                return result
            except Exception as exc:           # noqa: BLE001 - classify below
                if _is_lock_error(exc) and attempt < retries:
                    attempt += 1
                    _bump("db_lock_retries")
                    _set_last_error(str(exc))
                else:
                    _bump("db_write_failures")
                    if _is_lock_error(exc):
                        _bump("db_lock_failures")
                    _set_last_error(str(exc))
                    raise
        time.sleep(base_delay * (2 ** (attempt - 1)))


def db_write_diagnostics() -> dict:
    with _COUNTER_LOCK:
        return dict(_COUNTERS)


def reset_write_diagnostics() -> None:
    """Test support: reset the observable counters (never used in production
    paths)."""
    with _COUNTER_LOCK:
        for k in _COUNTERS:
            _COUNTERS[k] = 0 if k != "db_last_write_error" else None


def serialized_write(method: Callable) -> Callable:
    """Decorate a store write method so its whole body runs under the shared
    writer lock with bounded retry + observable counters.

    The wrapped methods are idempotent SQL (UPSERT / INSERT OR IGNORE /
    UPDATE), so a retry after a transient lock error is safe.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        return run_write(lambda: method(self, *args, **kwargs))
    return wrapper
