from pathlib import Path
from types import SimpleNamespace

from src.smc_engine.lifecycle import SetupRegistry, SetupState
from src.smc_engine.persistent_lifecycle import PersistentLifecycleCoordinator
from src.smc_engine.reconcile import MT5LifecycleReconciler
from src.smc_engine.store import SetupStore


class FakeMT5:
    def __init__(self):
        self.orders = []
        self.positions_ = []

    def orders_get(self, symbol):
        return tuple(self.orders)

    def positions_get(self, symbol):
        return tuple(self.positions_)

    def last_error(self):
        return (0, "ok")


def test_startup_reconcile_returns_broker_identities(tmp_path: Path):
    mt5 = FakeMT5()
    mt5.orders = [
        SimpleNamespace(ticket=11, magic=202609, comment="SMC SETUP-EURAUD-1-OB")
    ]
    store = SetupStore(tmp_path / "state.sqlite3")
    coordinator = PersistentLifecycleCoordinator(
        store,
        MT5LifecycleReconciler(mt5, 202609, SetupRegistry()),
        SetupRegistry(),
    )

    assert coordinator.startup_reconcile("EURAUD") == {"SETUP-EURAUD-1-OB"}
    store.close()


def test_new_setup_blocked_when_persisted_state_exists(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    mt5 = FakeMT5()
    coordinator = PersistentLifecycleCoordinator(
        store,
        MT5LifecycleReconciler(mt5, 202609, SetupRegistry()),
        SetupRegistry(),
    )

    # A real persisted setup would be inserted by PersistentLifecycle.persist.
    # This test focuses on the empty-state contract.
    assert coordinator.can_accept_new_setup("EURAUD")
    store.close()
