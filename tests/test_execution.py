from src.smc_engine.execution import MT5ExecutionAdapter, PendingOrderRequest
from src.smc_engine.models import Direction
from src.smc_engine.risk import RiskEngine
from src.smc_engine.market import SymbolSpec
from src.smc_engine.setup import TradeSetup


class FakeMT5:
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    TRADE_ACTION_PENDING = 5
    ORDER_TIME_GTC = 0

    def __init__(self):
        self.checked = None
        self.sent = None

    def order_check(self, request):
        self.checked = request
        return {"retcode": 0}

    def order_send(self, request):
        self.sent = request
        return {"retcode": 0}


def make_setup():
    return TradeSetup(
        id="S", symbol="TEST", direction=Direction.BULLISH, created_time=1,
        poi_id="P", sweep_id="SW", csd_id="CSD", protected_level=1.099,
        order_block_id="OB", inducement_id="IDM", entry=1.1,
        stop_loss=1.099, take_profit=1.2, irl_swing_id="IRL",
        invalidation_level=1.099, risk_percent=1.0,
    )


def test_build_and_preflight_pending_request():
    spec = SymbolSpec("TEST", 5, 0.00001, 0.00001, 1.0, 0.01, 100, 0.01, 10, 0, 0)
    mt5 = FakeMT5()
    adapter = MT5ExecutionAdapter(mt5, RiskEngine(spec))
    req = adapter.build_limit_request(make_setup(), 1000, 1.1002, 1.1003)
    assert req.volume == 0.1
    assert req.comment == "SMC SETUP-S"
    payload = adapter.to_mt5_request(req)
    assert payload["type"] == mt5.ORDER_TYPE_BUY_LIMIT
    assert payload["comment"] == "SMC SETUP-S"
    assert adapter.preflight(payload)["retcode"] == 0
    assert mt5.checked == payload


def test_send_is_separate_from_preflight():
    spec = SymbolSpec("TEST", 5, 0.00001, 0.00001, 1.0, 0.01, 100, 0.01, 10, 0, 0)
    mt5 = FakeMT5()
    adapter = MT5ExecutionAdapter(mt5, RiskEngine(spec))
    payload = adapter.to_mt5_request(adapter.build_limit_request(make_setup(), 1000, 1.1002, 1.1003))
    adapter.send(payload)
    assert mt5.sent == payload


def test_preflight_rejects_nonzero_retcode():
    class RejectingMT5(FakeMT5):
        def order_check(self, request):
            return {"retcode": 10014}
    spec = SymbolSpec("TEST", 5, 0.00001, 0.00001, 1.0, 0.01, 100, 0.01, 10, 0, 0)
    adapter = MT5ExecutionAdapter(RejectingMT5(), RiskEngine(spec))
    payload = adapter.to_mt5_request(adapter.build_limit_request(make_setup(), 1000, 1.1002, 1.1003))
    try:
        adapter.preflight(payload)
    except ValueError as exc:
        assert "10014" in str(exc)
    else:
        raise AssertionError("expected preflight rejection")


def test_broker_comment_rejects_unbounded_setup_identity():
    spec = SymbolSpec("TEST", 5, 0.00001, 0.00001, 1.0, 0.01, 100, 0.01, 10, 0, 0)
    adapter = MT5ExecutionAdapter(FakeMT5(), RiskEngine(spec))
    setup = make_setup()
    setup = TradeSetup(**{**setup.__dict__, "id": "SETUP-" + "X" * 40})
    try:
        adapter.build_limit_request(setup, 1000, 1.1002, 1.1003)
    except ValueError as exc:
        assert "too long" in str(exc)
    else:
        raise AssertionError("expected comment length rejection")


def test_bullish_prices_are_normalized_conservatively():
    spec = SymbolSpec("TEST", 5, 0.00001, 0.00001, 1.0, 0.01, 100, 0.01, 10, 0, 0)
    adapter = MT5ExecutionAdapter(FakeMT5(), RiskEngine(spec))
    setup = TradeSetup(**{**make_setup().__dict__, "entry": 1.100006, "stop_loss": 1.099994, "take_profit": 1.200004})
    req = adapter.build_limit_request(setup, 1000, 1.1001, 1.1002)
    assert req.price == 1.1
    assert req.stop_loss == 1.09999
    assert req.take_profit == 1.20001


def test_normalized_entry_is_revalidated_against_market():
    spec = SymbolSpec("TEST", 5, 0.00001, 0.00001, 1.0, 0.01, 100, 0.01, 10, 0, 0)
    adapter = MT5ExecutionAdapter(FakeMT5(), RiskEngine(spec))
    setup = make_setup()
    try:
        adapter.build_limit_request(setup, 1000, 1.09999, 1.1)
    except ValueError as exc:
        assert "no longer valid" in str(exc)
    else:
        raise AssertionError("expected normalized marketability rejection")
