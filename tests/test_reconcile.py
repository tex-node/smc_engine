from types import SimpleNamespace

from src.smc_engine.lifecycle import SetupRegistry, SetupState
from src.smc_engine.reconcile import MT5LifecycleReconciler


class FakeMT5:
    def __init__(self):
        self.orders = [
            SimpleNamespace(ticket=101, magic=202609, comment="SMC SETUP-EURAUD-1-OB")
        ]
        self.positions_ = [
            SimpleNamespace(ticket=202, magic=202609, comment="SMC SETUP-EURAUD-2-OB")
        ]

    def orders_get(self, symbol):
        return tuple(self.orders)

    def positions_get(self, symbol):
        return tuple(self.positions_)

    def last_error(self):
        return (0, "ok")


def test_reconciler_recovers_broker_truth():
    reconciler = MT5LifecycleReconciler(FakeMT5(), 202609, SetupRegistry())
    records = reconciler.reconcile("EURAUD")

    assert {r.setup_id for r in records} == {
        "SETUP-EURAUD-1-OB",
        "SETUP-EURAUD-2-OB",
    }
    assert {r.state for r in records} == {
        SetupState.ORDER_PLACED,
        SetupState.POSITION_MANAGED,
    }


def test_reconciler_filters_other_magic_numbers():
    mt5 = FakeMT5()
    mt5.orders.append(
        SimpleNamespace(ticket=303, magic=7, comment="SMC SETUP-EURAUD-3-OB")
    )
    reconciler = MT5LifecycleReconciler(mt5, 202609, SetupRegistry())

    assert reconciler.active_setup_ids("EURAUD") == {
        "SETUP-EURAUD-1-OB",
        "SETUP-EURAUD-2-OB",
    }
