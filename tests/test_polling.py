import pandas as pd

from src.smc_engine.persistent_lifecycle import PersistentLifecycleCoordinator

from src.smc_engine.polling import LivePollingCoordinator


class FakeMarket:
    def __init__(self):
        self.times = [
            pd.Timestamp("2026-01-01T10:00:00Z"),
            pd.Timestamp("2026-01-01T10:00:00Z"),
            pd.Timestamp("2026-01-01T10:15:00Z"),
        ]

    def closed_bars(self, timeframe, count=1):
        return pd.DataFrame({"time": [self.times.pop(0)]})


class FakeDryRun:
    def __init__(self):
        self.calls = 0

    def run_once(self, balance):
        self.calls += 1
        return self.calls


def test_poll_only_runs_for_new_closed_m15_candle():
    market = FakeMarket()
    dry_run = FakeDryRun()
    coordinator = LivePollingCoordinator(market, dry_run)

    assert coordinator.poll_once(1000) == 1
    assert coordinator.poll_once(1000) is None
    assert coordinator.poll_once(1000) == 2
    assert dry_run.calls == 2


class BlockingLifecycle:
    def __init__(self):
        self.calls = 0
    def can_accept_new_setup(self, symbol):
        self.calls += 1
        return False


def test_poll_blocks_replacement_when_active_lifecycle_exists():
    market = FakeMarket()
    dry_run = FakeDryRun()
    lifecycle = BlockingLifecycle()
    coordinator = LivePollingCoordinator(market, dry_run, lifecycle)
    report = coordinator.poll_once(1000)
    assert report.message.startswith("POLL: active")
    assert dry_run.calls == 0
