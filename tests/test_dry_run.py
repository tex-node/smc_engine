
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
