
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
