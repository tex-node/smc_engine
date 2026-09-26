from pathlib import Path

from src.smc_engine.lifecycle import SetupState
from src.smc_engine.store import SetupStore
from src.smc_engine.models import Direction
from src.smc_engine.setup import TradeSetup


def make_setup():
    return TradeSetup(
        id="SETUP-EURAUD-1-OB",
        symbol="EURAUD",
        direction=Direction.BULLISH,
        created_time="2026-01-01T00:00:00Z",
        poi_id="P", sweep_id="S", csd_id="C",
        protected_level=1.0, order_block_id="OB",
        inducement_id="IDM", entry=1.1, stop_loss=0.99,
        take_profit=1.2, irl_swing_id="IRL",
        invalidation_level=1.0, risk_percent=1.0,
    )


def test_setup_store_round_trip(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    setup = make_setup()

    store.upsert_setup(
        setup,
        SetupState.ORDER_PLACED,
        "2026-01-01T00:01:00Z",
        ticket=123,
        reason="submitted",
    )
    store.record_transition(
        setup.id,
        SetupState.ORDER_PREFLIGHTED,
        SetupState.ORDER_PLACED,
        "2026-01-01T00:01:00Z",
        "submitted",
    )

    saved = store.get(setup.id)
    assert saved["state"] is SetupState.ORDER_PLACED
    assert saved["ticket"] == 123
    assert saved["setup_json"]["symbol"] == "EURAUD"
    assert len(store.active("EURAUD")) == 1

    store.close()


def test_persist_transition_is_atomic_and_enum_safe(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    setup = make_setup()
    store.persist_transition(
        setup,
        SetupState.EXECUTION_READY,
        SetupState.ORDER_PREFLIGHTED,
        "2026-01-01T00:02:00Z",
        "preflight ok",
        ticket=123,
    )
    saved = store.get(setup.id)
    assert saved["state"] is SetupState.ORDER_PREFLIGHTED
    assert saved["setup_json"]["direction"] == "BULLISH"
    assert saved["ticket"] == 123
    store.close()


def test_persisted_setup_can_be_reconstructed(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    setup = make_setup()
    store.upsert_setup(setup, SetupState.ORDER_PLACED, "2026-01-01T00:01:00Z", ticket=123)
    restored = store.load_setup(setup.id)
    assert restored == setup
    assert restored.direction is Direction.BULLISH
    store.close()


def test_position_ticket_is_persisted_separately(tmp_path: Path):
    store = SetupStore(tmp_path / "state.sqlite3")
    setup = make_setup()
    store.upsert_setup(setup, SetupState.ORDER_PLACED, "2026-01-01T00:03:00Z", ticket=123)
    store.set_position_ticket(setup.id, 456)
    saved = store.get(setup.id)
    assert saved["ticket"] == 123
    assert saved["position_ticket"] == 456
    store.close()
