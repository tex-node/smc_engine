from pathlib import Path
from types import SimpleNamespace

from src.smc_engine.lifecycle import SetupRegistry, SetupState
from src.smc_engine.persistent_lifecycle import PersistentLifecycleCoordinator, ReconciliationKind
from src.smc_engine.reconcile import MT5LifecycleReconciler
from src.smc_engine.store import SetupStore
from src.smc_engine.models import Direction
from src.smc_engine.setup import TradeSetup


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


def make_setup():
    return TradeSetup(
        id="SETUP-EURAUD-1-OB", symbol="EURAUD", direction=Direction.BULLISH,
        created_time="2026-01-01T00:00:00Z", poi_id="P", sweep_id="S", csd_id="C",
        protected_level=1.0, order_block_id="OB", inducement_id="IDM",
        entry=1.1, stop_loss=0.99, take_profit=1.2, irl_swing_id="IRL",
        invalidation_level=1.0, risk_percent=1.0,
    )


def test_new_setup_blocked_when_persisted_state_exists(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    mt5 = FakeMT5()
    coordinator = PersistentLifecycleCoordinator(
        store,
        MT5LifecycleReconciler(mt5, 202609, SetupRegistry()),
        SetupRegistry(),
    )

    store.upsert_setup(make_setup(), SetupState.ORDER_PLACED, "2026-01-01T00:01:00Z", ticket=11)
    assert not coordinator.can_accept_new_setup("EURAUD")
    store.close()


def test_reconcile_classifies_match_mismatch_and_missing(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    setup = make_setup()
    store.upsert_setup(setup, SetupState.ORDER_PLACED, "2026-01-01T00:01:00Z", ticket=11)
    mt5 = FakeMT5()
    mt5.orders = [SimpleNamespace(ticket=11, magic=202609, comment="SETUP-EURAUD-1-OB")]
    coordinator = PersistentLifecycleCoordinator(
        store, MT5LifecycleReconciler(mt5, 202609, SetupRegistry()), SetupRegistry()
    )
    result = coordinator.startup_reconcile("EURAUD")
    assert result[0].kind == ReconciliationKind.BROKER_ACTIVE_MATCH

    mt5.orders[0].ticket = 12
    result = coordinator.startup_reconcile("EURAUD")
    assert result[0].kind == ReconciliationKind.BROKER_ACTIVE_MATCH

    store.upsert_setup(setup, SetupState.FILLED, "2026-01-01T00:02:00Z", ticket=12)
    result = coordinator.startup_reconcile("EURAUD")
    assert result[0].kind == ReconciliationKind.STATE_MISMATCH

    mt5.orders = []
    result = coordinator.startup_reconcile("EURAUD")
    assert result[0].kind == ReconciliationKind.PERSISTED_MISSING_BROKER
    store.close()


def test_startup_restores_persisted_lifecycle_into_registry(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    setup = make_setup()
    store.upsert_setup(setup, SetupState.ORDER_PLACED, "2026-01-01T00:01:00Z", ticket=11)
    mt5 = FakeMT5()
    registry = SetupRegistry()
    coordinator = PersistentLifecycleCoordinator(
        store, MT5LifecycleReconciler(mt5, 202609, registry), registry
    )
    coordinator.startup_reconcile("EURAUD")
    restored = registry.get(setup.id)
    assert restored is not None
    assert restored.state is SetupState.ORDER_PLACED
    assert restored.setup == setup
    store.close()


def test_unknown_magic_owned_broker_object_blocks_new_setup(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    mt5 = FakeMT5()
    mt5.orders = [
        SimpleNamespace(ticket=99, magic=202609, comment="SMC PENDING")
    ]
    registry = SetupRegistry()
    coordinator = PersistentLifecycleCoordinator(
        store, MT5LifecycleReconciler(mt5, 202609, registry), registry
    )

    results = coordinator.startup_reconcile("EURAUD")
    assert results[0].kind == ReconciliationKind.BROKER_ACTIVE_UNKNOWN
    assert results[0].setup_id == "BROKER-UNKNOWN-ORDER-99"
    assert not coordinator.can_accept_new_setup("EURAUD")
    store.close()


def test_restart_reconciles_submitting_order_to_placed(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    setup = make_setup()
    store.upsert_setup(setup, SetupState.ORDER_SUBMITTING, "2026-01-01T00:01:00Z")
    mt5 = FakeMT5()
    mt5.orders = [SimpleNamespace(ticket=77, magic=202609, comment="SMC SETUP-EURAUD-1-OB")]
    registry = SetupRegistry()
    coordinator = PersistentLifecycleCoordinator(
        store, MT5LifecycleReconciler(mt5, 202609, registry), registry
    )

    results = coordinator.startup_reconcile("EURAUD")
    assert results[0].kind == ReconciliationKind.SUBMISSION_CONFIRMED
    assert registry.get(setup.id).state is SetupState.ORDER_PLACED
    row = store.get(setup.id)
    assert row["state"] is SetupState.ORDER_PLACED
    assert row["ticket"] == 77
    store.close()
