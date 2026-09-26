
from src.smc_engine.dry_run import DryRunEngine
from src.smc_engine.lifecycle import SetupState
from src.smc_engine.market import SymbolSpec
from src.smc_engine.risk import RiskEngine


class FakeMarket:
    def tick(self):
        return {"bid": 1.1, "ask": 1.1002}


class FakeAnalyzer:
    def __init__(self, candidate=None):
        self.candidate = candidate


class FakeGuard:
    def __init__(self, active=False, valid=True):
        self.active = active
        self.valid = valid

    def has_active_identity(self, setup):
        return self.active

    def setup_is_still_valid(self, setup, bid, ask):
        return self.valid


from types import SimpleNamespace
from src.smc_engine.analyzer import LiveAnalysis
from src.smc_engine.setup import TradeSetup
from src.smc_engine.models import Direction


def make_setup():
    return TradeSetup(
        id="S", symbol="TEST", direction=Direction.BULLISH, created_time=1,
        poi_id="P", sweep_id="SW", csd_id="CSD", protected_level=1.099,
        order_block_id="OB", inducement_id="IDM", entry=1.1,
        stop_loss=1.099, take_profit=1.2, irl_swing_id="IRL",
        invalidation_level=1.099, risk_percent=1.0,
    )


def make_engine(guard):
    setup = make_setup()
    analyzer = FakeAnalyzer(SimpleNamespace(setup=setup))
    analyzer.analyze_once = lambda: LiveAnalysis("TEST", analyzer.candidate, 1, 1, 1)
    spec = SymbolSpec("TEST", 5, 0.00001, 0.00001, 1.0, 0.01, 100, 0.01, 10, 0, 0)
    return DryRunEngine(FakeMarket(), analyzer, RiskEngine(spec), order_guard=guard)


def test_dry_run_blocks_duplicate_broker_identity():
    report = make_engine(FakeGuard(active=True)).run_once(1000)
    assert report.state is SetupState.ORDER_PLACED
    assert report.volume is None


def test_dry_run_blocks_structurally_invalid_setup():
    report = make_engine(FakeGuard(valid=False)).run_once(1000)
    assert report.state is SetupState.PROTECTED_LEVEL_BREACHED
    assert report.volume is None
