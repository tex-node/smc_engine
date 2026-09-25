from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

from src.smc_engine.lifecycle import SetupRegistry, SetupState
from src.smc_engine.persistent_lifecycle import PersistentLifecycleCoordinator, ReconciliationKind
from src.smc_engine.reconcile import MT5LifecycleReconciler
from src.smc_engine.store import SetupStore
from src.smc_engine.models import Direction
from src.smc_engine.setup import TradeSetup
from src.smc_engine.execution import MT5ExecutionAdapter
from src.smc_engine.market import SymbolSpec
from src.smc_engine.risk import RiskEngine


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

    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    TRADE_ACTION_PENDING = 5
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_PLACED = 10008

    def order_check(self, request):
        return {"retcode": 0}

    def order_send(self, request):
        self.orders.append(SimpleNamespace(ticket=123, magic=request["magic"], comment=request["comment"]))
        return {"retcode": self.TRADE_RETCODE_PLACED, "order": 123}


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


def test_submit_pending_persists_intent_before_broker_send(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    mt5 = FakeMT5()
    registry = SetupRegistry()
    lifecycle = __import__("src.smc_engine.lifecycle", fromlist=["SetupLifecycle"]).SetupLifecycle(make_setup())
    registry.add(lifecycle)
    coordinator = PersistentLifecycleCoordinator(store, MT5LifecycleReconciler(mt5, 202609, registry), registry)
    spec = SymbolSpec("EURAUD", 5, 0.00001, 0.00001, 1.0, 0.01, 100, 0.01, 10, 0, 0)
    lifecycle.setup = replace(lifecycle.setup, stop_loss=1.099)
    execution = MT5ExecutionAdapter(mt5, RiskEngine(spec))

    result = coordinator.submit_pending(lifecycle, execution, 1000, 1.099, 1.1003, "2026-01-01T00:03:00Z")
    assert result.state is SetupState.ORDER_PLACED
    assert result.ticket == 123
    assert lifecycle.state is SetupState.ORDER_PLACED
    assert store.get(lifecycle.setup.id)["ticket"] == 123
    assert mt5.orders
    store.close()


def test_submit_pending_leaves_submitting_on_ambiguous_send(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    mt5 = FakeMT5()
    registry = SetupRegistry()
    lifecycle = __import__("src.smc_engine.lifecycle", fromlist=["SetupLifecycle"]).SetupLifecycle(make_setup())
    registry.add(lifecycle)
    coordinator = PersistentLifecycleCoordinator(store, MT5LifecycleReconciler(mt5, 202609, registry), registry)

    class AmbiguousExecution:
        def __init__(self, mt5_module): self.mt5 = mt5_module
        def build_limit_request(self, *args): return object()
        def to_mt5_request(self, request): return {}
        def preflight(self, payload): return {"retcode": 0}
        def send(self, payload): raise RuntimeError("transport ambiguity")

    result = coordinator.submit_pending(lifecycle, AmbiguousExecution(mt5), 1000, 1.099, 1.1003, "2026-01-01T00:04:00Z")
    assert result.ambiguous is True
    assert result.state is SetupState.ORDER_SUBMITTING
    assert store.get(lifecycle.setup.id)["state"] is SetupState.ORDER_SUBMITTING
    store.close()


def test_duplicate_broker_identity_is_reconciliation_conflict(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    setup = make_setup()
    store.upsert_setup(setup, SetupState.ORDER_PLACED, "2026-01-01T00:01:00Z", ticket=11)
    mt5 = FakeMT5()
    mt5.orders = [SimpleNamespace(ticket=11, magic=202609, comment="SMC SETUP-EURAUD-1-OB")]
    mt5.positions_ = [SimpleNamespace(ticket=12, magic=202609, comment="SMC SETUP-EURAUD-1-OB")]
    registry = SetupRegistry()
    coordinator = PersistentLifecycleCoordinator(
        store, MT5LifecycleReconciler(mt5, 202609, registry), registry
    )

    results = coordinator.startup_reconcile("EURAUD")
    assert len(results) == 1
    assert results[0].kind == ReconciliationKind.BROKER_IDENTITY_CONFLICT
    assert results[0].setup_id == setup.id
    store.close()


def test_success_without_broker_ticket_stays_submitting(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    mt5 = FakeMT5()
    registry = SetupRegistry()
    lifecycle = __import__("src.smc_engine.lifecycle", fromlist=["SetupLifecycle"]).SetupLifecycle(make_setup())
    registry.add(lifecycle)
    coordinator = PersistentLifecycleCoordinator(store, MT5LifecycleReconciler(mt5, 202609, registry), registry)
    spec = SymbolSpec("EURAUD", 5, 0.00001, 0.00001, 1.0, 0.01, 100, 0.01, 10, 0, 0)
    lifecycle.setup = replace(lifecycle.setup, stop_loss=1.099)

    class SuccessWithoutTicketExecution(MT5ExecutionAdapter):
        def send(self, payload):
            return {"retcode": self.mt5.TRADE_RETCODE_PLACED}

    execution = SuccessWithoutTicketExecution(mt5, RiskEngine(spec))
    result = coordinator.submit_pending(lifecycle, execution, 1000, 1.099, 1.1003, "2026-01-01T00:05:00Z")
    assert result.state is SetupState.ORDER_SUBMITTING
    assert result.ambiguous is True
    assert lifecycle.state is SetupState.ORDER_SUBMITTING
    assert store.get(lifecycle.setup.id)["state"] is SetupState.ORDER_SUBMITTING
    store.close()

def test_resumed_preflighted_order_is_repreflighted_before_send(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    mt5 = FakeMT5()
    registry = SetupRegistry()
    lifecycle = __import__("src.smc_engine.lifecycle", fromlist=["SetupLifecycle"]).SetupLifecycle(
        make_setup(), SetupState.ORDER_PREFLIGHTED, "broker preflight passed"
    )
    registry.add(lifecycle)
    coordinator = PersistentLifecycleCoordinator(store, MT5LifecycleReconciler(mt5, 202609, registry), registry)
    spec = SymbolSpec("EURAUD", 5, 0.00001, 0.00001, 1.0, 0.01, 100, 0.01, 10, 0, 0)
    lifecycle.setup = replace(lifecycle.setup, stop_loss=1.099)

    class CountingExecution(MT5ExecutionAdapter):
        def __init__(self, mt5_module, risk):
            super().__init__(mt5_module, risk)
            self.preflight_calls = 0
        def preflight(self, payload):
            self.preflight_calls += 1
            return super().preflight(payload)

    execution = CountingExecution(mt5, RiskEngine(spec))
    result = coordinator.submit_pending(lifecycle, execution, 1000, 1.099, 1.1003, "2026-01-01T00:06:00Z")
    assert result.state is SetupState.ORDER_PLACED
    assert execution.preflight_calls == 1
    assert mt5.orders
    store.close()

def test_ambiguous_submitting_state_blocks_new_setup_after_restart(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    setup = make_setup()
    store.upsert_setup(setup, SetupState.ORDER_SUBMITTING, "2026-01-01T00:07:00Z")
    mt5 = FakeMT5()
    registry = SetupRegistry()
    coordinator = PersistentLifecycleCoordinator(
        store, MT5LifecycleReconciler(mt5, 202609, registry), registry
    )

    results = coordinator.startup_reconcile("EURAUD")
    assert len(results) == 1
    assert results[0].kind == ReconciliationKind.PERSISTED_MISSING_BROKER
    assert results[0].persisted_state is SetupState.ORDER_SUBMITTING
    assert not coordinator.can_accept_new_setup("EURAUD")
    assert registry.get(setup.id).state is SetupState.ORDER_SUBMITTING
    store.close()

def test_reconcile_detects_broker_ticket_mismatch(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    setup = make_setup()
    store.upsert_setup(setup, SetupState.ORDER_PLACED, "2026-01-01T00:01:00Z", ticket=11)
    mt5 = FakeMT5()
    mt5.orders = [SimpleNamespace(ticket=12, magic=202609, comment="SMC SETUP-EURAUD-1-OB")]
    registry = SetupRegistry()
    coordinator = PersistentLifecycleCoordinator(
        store, MT5LifecycleReconciler(mt5, 202609, registry), registry
    )

    results = coordinator.startup_reconcile("EURAUD")
    assert len(results) == 1
    assert results[0].kind == ReconciliationKind.BROKER_TICKET_MISMATCH
    assert results[0].persisted_state is SetupState.ORDER_PLACED
    assert results[0].ticket == 12
    assert not coordinator.can_accept_new_setup("EURAUD")
    store.close()
