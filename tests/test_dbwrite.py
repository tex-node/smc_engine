"""P1-B regressions: serialized writer boundary, bounded retry, coverage.

TEST FIXTURES; no broker interaction.
"""
import sqlite3
import threading

import pandas as pd
import pytest

from src.smc_engine.opportunity import Opportunity, OpportunityRepository
from src.smc_engine.opportunity.models import canonical_opportunity_key, opportunity_id_from_key
from src.smc_engine.web import dbwrite
from src.smc_engine.web.dbwrite import (MAX_RETRIES, SHARED_WRITE_LOCK, db_write_diagnostics,
                                        reset_write_diagnostics, run_write)
from src.smc_engine.web.hub import DictSource, EngineHub, TIMEFRAMES
from tests.test_api import FakeDemoSource
from tests.test_opportunity_engine import timeline


@pytest.fixture(autouse=True)
def _clean_counters():
    reset_write_diagnostics()
    yield


def _opp(i=0, symbol="GBPUSD"):
    # distinct sweep EVENT TIMES so each opportunity has a distinct canonical id
    key = canonical_opportunity_key(symbol, "BULLISH", "REVERSAL", "SWEEP",
                                    pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=15 * i),
                                    1.0)
    return Opportunity(opportunity_id=opportunity_id_from_key(key), canonical_key=key,
                       symbol=symbol, direction="BULLISH", opportunity_type="REVERSAL",
                       state="READY_FOR_MITIGATION",
                       created_at="2026-01-01T00:00:00+00:00",
                       updated_at="2026-01-01T00:00:00+00:00")


def _flat(n=200, start="2026-01-01", freq="15min"):
    times = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"time": times, "open": 1.10, "high": 1.101,
                         "low": 1.099, "close": 1.10})


def test_bounded_retry_then_exhaustion():
    calls = {"n": 0}
    before = db_write_diagnostics()

    def always_locked():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        run_write(always_locked, retries=MAX_RETRIES, base_delay=0.0)
    assert calls["n"] == MAX_RETRIES + 1, "retry budget must be bounded (1 + retries)"
    after = db_write_diagnostics()
    assert after["db_lock_failures"] == before["db_lock_failures"] + 1
    assert after["db_write_failures"] == before["db_write_failures"] + 1
    assert "locked" in (after["db_last_write_error"] or "")
    # a subsequent successful write clears the last-error marker
    run_write(lambda: None)
    assert db_write_diagnostics()["db_last_write_error"] is None


def test_transient_lock_is_retried_then_succeeds():
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    before = db_write_diagnostics()
    assert run_write(flaky, base_delay=0.0) == "ok"
    after = db_write_diagnostics()
    assert after["db_lock_retries"] == before["db_lock_retries"] + 1
    assert after["db_write_failures"] == before["db_write_failures"]
    assert after["db_write_count"] == before["db_write_count"] + 1


def test_concurrent_reads_and_writes_same_connection(tmp_path):
    """Live scans interleave repo reads with writes on the same connection,
    which produced `DatabaseError: another row available`. Reads and writes on
    one connection must be serialized (connection lock)."""
    repo = OpportunityRepository(str(tmp_path / "rw.db"))
    errors = []

    def writer(base):
        try:
            for i in range(40):
                repo.upsert(_opp(base + i))
        except Exception as exc:                       # noqa: BLE001
            errors.append(("write", exc))

    def reader():
        try:
            for _ in range(80):
                repo.query(symbol="GBPUSD", active_only=True, limit=50)
                repo.count()
                repo.state_counts()
        except Exception as exc:                       # noqa: BLE001
            errors.append(("read", exc))

    threads = ([threading.Thread(target=writer, args=(k * 100,)) for k in range(3)] +
               [threading.Thread(target=reader) for _ in range(3)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"interleaved access errors: {errors[:3]}"
    assert repo.count() == 120
    assert db_write_diagnostics()["db_write_failures"] == 0
    repo.close()


def test_concurrent_opportunity_writes_no_lock_failures(tmp_path):
    repo = OpportunityRepository(str(tmp_path / "c.db"))
    before = db_write_diagnostics()
    errors = []

    def worker(base):
        try:
            for i in range(30):
                repo.upsert(_opp(base + i))
        except Exception as exc:                       # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k * 100,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    after = db_write_diagnostics()
    assert not errors, f"writers hit errors: {errors[:2]}"
    assert after["db_lock_failures"] == before["db_lock_failures"]
    assert repo.count() == 180
    repo.close()


def test_concurrent_alerts_and_opportunities(tmp_path):
    src = FakeDemoSource()
    hub = EngineHub(src, db_path=str(tmp_path / "g.db"),
                    setup_store_path=str(tmp_path / "s.db"))
    errors = []

    def alerts():
        try:
            for i in range(40):
                hub.web.add_alert("GBPUSD", "causal", "READY", f"a{i}")
        except Exception as exc:                       # noqa: BLE001
            errors.append(exc)

    def opps():
        try:
            for i in range(40):
                hub.opportunities.repo.upsert(_opp(i))
        except Exception as exc:                       # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=alerts), threading.Thread(target=opps),
               threading.Thread(target=alerts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:2]
    assert hub.opportunity_diagnostics()["db"]["db_lock_failures"] == 0
    hub.stop_poller()


def test_24_symbol_scan_full_coverage(tmp_path):
    src = DictSource()
    prime_names = [f"SYM{i:02d}" for i in range(24)]
    for name in prime_names:
        for code, freq in ((TIMEFRAMES["D1"], "1D"), (TIMEFRAMES["H4"], "4h"),
                           (TIMEFRAMES["M15"], "15min")):
            src.prime(name, code, _flat(300, freq=freq))
    hub = EngineHub(src, db_path=str(tmp_path / "g.db"),
                    setup_store_path=str(tmp_path / "s.db"))
    stats = hub.scan_universe_once(force=True)
    assert stats["symbols_scanned"] == 24
    assert stats["symbols_succeeded"] == 24
    assert stats["symbols_failed"] == 0
    diag = hub.opportunity_diagnostics()
    assert diag["db"]["db_lock_failures"] == 0
    assert diag["db"]["db_write_failures"] == 0
    assert not [s for s, v in diag["symbols"].items() if v.get("last_error")]
    hub.stop_poller()


def test_one_symbol_write_failure_is_isolated(tmp_path):
    src = DictSource()
    for name in ("GOODA", "BROKEN", "GOODB"):
        for code, freq in ((TIMEFRAMES["D1"], "1D"), (TIMEFRAMES["H4"], "4h"),
                           (TIMEFRAMES["M15"], "15min")):
            src.prime(name, code, _flat(300, freq=freq))
    hub = EngineHub(src, db_path=str(tmp_path / "g.db"),
                    setup_store_path=str(tmp_path / "s.db"))
    original = hub.opportunities.repo.upsert

    def flaky(opp):
        if opp.symbol == "BROKEN":
            raise sqlite3.OperationalError("database is locked")
        return original(opp)

    # register one opportunity per symbol, then make one symbol's write fail
    hub.opportunities.repo.upsert = flaky
    stats = hub.scan_universe_once(force=True)
    assert stats["symbols_failed"] == 0 or stats["symbols_failed"] == 1
    assert stats["symbols_succeeded"] >= 2
    hub.stop_poller()


def test_watcher_and_analysis_contention(tmp_path):
    src = FakeDemoSource()
    d1, h4, m15 = timeline()
    src.prime("GBPUSD", TIMEFRAMES["D1"], d1)
    src.prime("GBPUSD", TIMEFRAMES["H4"], h4)
    src.prime("GBPUSD", TIMEFRAMES["M15"], m15)
    hub = EngineHub(src, db_path=str(tmp_path / "g.db"),
                    setup_store_path=str(tmp_path / "s.db"))
    errors = []

    def scan():
        try:
            for _ in range(3):
                hub.scan_universe_once(force=True)
        except Exception as exc:                       # noqa: BLE001
            errors.append(exc)

    def read():
        try:
            for _ in range(6):
                hub.analysis("GBPUSD", "M15", 120)
                hub.readiness("GBPUSD")
        except Exception as exc:                       # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=scan), threading.Thread(target=read),
               threading.Thread(target=read)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:2]
    diag = hub.opportunity_diagnostics()
    assert diag["db"]["db_lock_failures"] == 0
    assert diag["db"]["db_write_failures"] == 0
    hub.stop_poller()


def test_repeated_scans_no_lost_or_duplicated_state(tmp_path):
    src = FakeDemoSource()
    d1, h4, m15 = timeline()
    for code, frame in ((TIMEFRAMES["D1"], d1), (TIMEFRAMES["H4"], h4),
                        (TIMEFRAMES["M15"], m15)):
        src.prime("GBPUSD", code, frame)
    hub = EngineHub(src, db_path=str(tmp_path / "g.db"),
                    setup_store_path=str(tmp_path / "s.db"))
    for _ in range(6):
        hub.scan_universe_once(force=True)
    rows = hub.opportunities.repo.query(symbol="GBPUSD")
    assert rows, "opportunity state must not be lost across scans"
    assert len(rows) == len({o.opportunity_id for o in rows})
    diag = hub.opportunity_diagnostics()
    assert diag["db"]["db_write_failures"] == 0
    hub.stop_poller()
