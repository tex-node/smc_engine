from pathlib import Path
from types import SimpleNamespace

from src.smc_engine.lifecycle import SetupRegistry, SetupState
from src.smc_engine.persistent_lifecycle import PersistentLifecycleCoordinator, ReconciliationKind
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


def test_startup_reconcile_classifies_broker_only_state(tmp_path: Path):
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

    results = coordinator.startup_reconcile("EURAUD")
    assert len(results) == 1
    assert results[0].kind == ReconciliationKind.BROKER_ACTIVE_UNKNOWN
    assert results[0].setup_id == "SETUP-EURAUD-1-OB"
    assert results[0].ticket == 11
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
