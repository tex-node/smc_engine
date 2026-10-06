"""Replay tooling: candle-level order simulation and historical R:R accumulation."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd

from .lifecycle import SetupLifecycle, SetupState
from .models import Direction
from .risk import RiskEngine
from .setup import TradeSetup


class ReplayOrderState(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    STOPPED = "STOPPED"
    TARGETED = "TARGETED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True)
class ReplayFill:
    index: int
    price: float


@dataclass(frozen=True)
class ReplayResult:
    setup_id: str
    order_state: ReplayOrderState
    entry: Optional[float]
    exit: Optional[float]
    exit_index: Optional[int]
    pnl_price: float
    bars_observed: int


class ReplayBroker:
    """Deterministic candle-level pending-limit simulator.

    This intentionally models conservative OHLC ambiguity: if a single candle
    touches both entry and SL/TP, the stop is resolved first. This avoids
    optimistic fills and can be replaced by tick replay later.
    """

    def __init__(self, candles: pd.DataFrame):
        self.candles = candles.reset_index(drop=True)

    def run(self, setup: TradeSetup) -> ReplayResult:
        pending = True
        filled_at: Optional[float] = None
        exit_at: Optional[float] = None
        exit_index: Optional[int] = None
        state = ReplayOrderState.PENDING

        for i, row in self.candles.iterrows():
            high, low = float(row["high"]), float(row["low"])

            if pending:
                if setup.direction is Direction.BULLISH:
                    if low <= setup.invalidation_level:
                        state = ReplayOrderState.INVALIDATED
                        break
                    if low <= setup.entry <= high:
                        filled_at = setup.entry
                        pending = False
                        state = ReplayOrderState.FILLED
                else:
                    if high >= setup.invalidation_level:
                        state = ReplayOrderState.INVALIDATED
                        break
                    if low <= setup.entry <= high:
                        filled_at = setup.entry
                        pending = False
                        state = ReplayOrderState.FILLED
                continue

            if setup.direction is Direction.BULLISH:
                if low <= setup.stop_loss:
                    exit_at, exit_index, state = setup.stop_loss, i, ReplayOrderState.STOPPED
                    break
                if high >= setup.take_profit:
                    exit_at, exit_index, state = setup.take_profit, i, ReplayOrderState.TARGETED
                    break
            else:
                if high >= setup.stop_loss:
                    exit_at, exit_index, state = setup.stop_loss, i, ReplayOrderState.STOPPED
                    break
                if low <= setup.take_profit:
                    exit_at, exit_index, state = setup.take_profit, i, ReplayOrderState.TARGETED
                    break

        pnl = 0.0 if filled_at is None or exit_at is None else (
            exit_at - filled_at if setup.direction is Direction.BULLISH
            else filled_at - exit_at
        )
        return ReplayResult(
            setup.id, state, filled_at, exit_at, exit_index, pnl, len(self.candles)
        )


@dataclass(frozen=True)
class ReplayMetrics:
    setups: int
    filled: int
    targets: int
    stops: int
    invalidated: int
    pending: int
    gross_price_pnl: float


def summarize(results: list[ReplayResult]) -> ReplayMetrics:
    return ReplayMetrics(
        setups=len(results),
        filled=sum(r.order_state in {ReplayOrderState.FILLED, ReplayOrderState.STOPPED, ReplayOrderState.TARGETED} for r in results),
        targets=sum(r.order_state is ReplayOrderState.TARGETED for r in results),
        stops=sum(r.order_state is ReplayOrderState.STOPPED for r in results),
        invalidated=sum(r.order_state is ReplayOrderState.INVALIDATED for r in results),
        pending=sum(r.order_state is ReplayOrderState.PENDING for r in results),
        gross_price_pnl=sum(r.pnl_price for r in results),
    )


# ---------------------------------------------------------------------------
# Historical R:R accumulation — uses the exact production causal pipeline
# ---------------------------------------------------------------------------

from .causal import CausalMTFAnalyzer, _compute_atr  # noqa: E402
from .strategy import MultiTimeframeConfig  # noqa: E402


@dataclass
class ReplayRecord:
    """Full provenance for one structurally qualified setup."""
    symbol: str
    direction: str
    created_time: object
    setup_id: str
    sweep_id: str
    csd_id: str
    ob_id: str
    entry: float
    stop: float
    target: float
    stop_distance_pips: float
    target_distance_pips: float
    risk_reward: float
    irl_strength: int
    irl_dist_atrs: float
    m15_atr: float


@dataclass
class RRBucket:
    label: str
    lo: float
    hi: float
    count: int = 0
    records: list = None  # list[ReplayRecord]

    def __post_init__(self):
        if self.records is None:
            self.records = []

    def contains(self, rr: float) -> bool:
        return self.lo <= rr < self.hi


_BUCKETS = [
    ("<0.5",    0.0,   0.5),
    ("0.5-1.0", 0.5,   1.0),
    ("1.0-1.5", 1.0,   1.5),
    ("1.5-2.0", 1.5,   2.0),
    (">=2.0",   2.0,   float("inf")),
]


def run_replay(
    symbol: str,
    d1: pd.DataFrame,
    h4: pd.DataFrame,
    m15: pd.DataFrame,
    config: Optional[MultiTimeframeConfig] = None,
) -> list[ReplayRecord]:
    """Return one ReplayRecord per structurally qualified setup in the data.

    Always uses min_rr=0.0 regardless of what is passed in config — the RR
    gate must never suppress setups from the historical record.  R:R is
    stored as metadata for later distribution analysis.
    """
    if config is None:
        config = MultiTimeframeConfig()
    gate_free_config = MultiTimeframeConfig(
        d1_lookback=config.d1_lookback,
        d1_poi_lookback=config.d1_poi_lookback,
        h4_swing_left=config.h4_swing_left,
        h4_swing_right=config.h4_swing_right,
        h4_sweep_lookback=config.h4_sweep_lookback,
        h4_csd_window=config.h4_csd_window,
        m15_swing_left=config.m15_swing_left,
        m15_swing_right=config.m15_swing_right,
        m15_displacement_atr=config.m15_displacement_atr,
        m15_atr_period=config.m15_atr_period,
        m15_ob_search_back=config.m15_ob_search_back,
        m15_idm_window=config.m15_idm_window,
        m15_irl_ob_proximity=config.m15_irl_ob_proximity,
        risk_percent=config.risk_percent,
        min_rr=0.0,
    )

    analyzer = CausalMTFAnalyzer(symbol, gate_free_config)
    candidates = analyzer.analyze_at(d1, h4, m15)

    m15_sorted = m15.sort_values("time").reset_index(drop=True)
    m15_atr = _compute_atr(m15_sorted, gate_free_config.m15_atr_period)

    records: list[ReplayRecord] = []
    for c in candidates:
        s = c.setup
        stop_dist = abs(s.stop_loss - s.entry)
        tgt_dist  = abs(s.entry - s.take_profit)
        rr = tgt_dist / stop_dist if stop_dist > 0 else 0.0
        irl_dist_atrs = (tgt_dist / (m15_atr or 1.0))

        records.append(ReplayRecord(
            symbol=symbol,
            direction=s.direction.value,
            created_time=s.created_time,
            setup_id=s.id,
            sweep_id=c.sweep.id,
            csd_id=c.csd.id,
            ob_id=_ob_id_from_setup(s),
            entry=round(float(s.entry), 5),
            stop=round(float(s.stop_loss), 5),
            target=round(float(s.take_profit), 5),
            stop_distance_pips=round(stop_dist * 10_000, 1),
            target_distance_pips=round(tgt_dist * 10_000, 1),
            risk_reward=round(rr, 3),
            irl_strength=s.irl_target_strength,
            irl_dist_atrs=round(irl_dist_atrs, 2),
            m15_atr=round(m15_atr * 10_000, 1),
        ))

    return records


def _ob_id_from_setup(setup) -> str:
    parts = setup.id.split("-OB-", 1)
    return "OB-" + parts[1] if len(parts) == 2 else ""


def distribution_report(records: list[ReplayRecord]) -> str:
    """Human-readable R:R distribution report."""
    buckets = [RRBucket(lbl, lo, hi) for lbl, lo, hi in _BUCKETS]
    for r in records:
        for b in buckets:
            if b.contains(r.risk_reward):
                b.count += 1
                b.records.append(r)
                break

    total = len(records)
    lines = [
        f"R:R Distribution -- {total} structurally qualified setup(s)",
        "-" * 60,
    ]
    for b in buckets:
        pct = b.count / total * 100 if total else 0
        bar = "#" * b.count
        lines.append(f"  {b.label:10s}: {b.count:3d}  {bar:<30s} {pct:5.1f}%")

    lines.append("-" * 60)
    if records:
        avg_rr = sum(r.risk_reward for r in records) / total
        med = sorted(r.risk_reward for r in records)[total // 2]
        lines.append(f"  mean R:R: {avg_rr:.3f}   median R:R: {med:.3f}")

    lines.append("")
    lines.append("Per-setup provenance:")
    lines.append(f"  {'symbol':<8} {'dir':<8} {'R:R':>6} {'stop_pip':>9} "
                 f"{'tgt_pip':>8} {'irl_str':>7} {'irl_atr':>7}  created")
    lines.append("  " + "-" * 82)
    for r in sorted(records, key=lambda x: x.risk_reward):
        lines.append(
            f"  {r.symbol:<8} {r.direction:<8} {r.risk_reward:>6.3f} "
            f"{r.stop_distance_pips:>9.1f} {r.target_distance_pips:>8.1f} "
            f"{r.irl_strength:>7} {r.irl_dist_atrs:>7.2f}  {r.created_time}"
        )
    return "\n".join(lines)
