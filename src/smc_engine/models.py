from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"

class SwingType(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"

class LiquiditySide(str, Enum):
    BUY_SIDE = "BUY_SIDE"
    SELL_SIDE = "SELL_SIDE"

class StructureEventType(str, Enum):
    BOS = "BOS"
    CHOCH = "CHOCH"
    CSD = "CSD"

class POIState(str, Enum):
    ACTIVE = "ACTIVE"
    TOUCHED = "TOUCHED"
    MITIGATED = "MITIGATED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"

class SetupState(str, Enum):
    PENDING = "PENDING"
    TRIGGERED = "TRIGGERED"
    FILLED = "FILLED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    AMBIGUOUS = "AMBIGUOUS"

@dataclass(frozen=True)
class SwingPoint:
    id: str
    index: int
    time: object
    type: SwingType
    price: float
    strength: int
    confirmation_index: int
    confirmation_time: object

@dataclass(frozen=True)
class LiquidityPool:
    id: str
    side: LiquiditySide
    price: float
    source_swing_id: str
    source_time: object

@dataclass(frozen=True)
class LiquiditySweep:
    id: str
    side: LiquiditySide
    swept_level: float
    sweep_extreme: float
    source_liquidity_id: str
    candle_index: int
    candle_time: object
    close: float

@dataclass(frozen=True)
class StructureEvent:
    id: str
    type: StructureEventType
    direction: Direction
    level: float
    candle_index: int
    candle_time: object
    source_swing_id: Optional[str] = None
    source_sweep_id: Optional[str] = None

@dataclass
class POI:
    id: str
    direction: Direction
    timeframe: str
    low: float
    high: float
    created_index: int
    created_time: object
    state: POIState = POIState.ACTIVE
    mitigated_index: Optional[int] = None
    invalidated_index: Optional[int] = None
    touches: int = 0

    def contains(self, price: float, tolerance: float = 0.0) -> bool:
        return (self.low - tolerance) <= price <= (self.high + tolerance)

@dataclass(frozen=True)
class Inducement:
    id: str
    direction: Direction
    candle_index: int
    candle_time: object
    level: float
    source_swing_index: int
    order_block_id: str
    confirmation_time: object = None

@dataclass
class StructureState:
    direction: Optional[Direction] = None
    swings: list[SwingPoint] = field(default_factory=list)
    events: list[StructureEvent] = field(default_factory=list)
    protected_high: Optional[float] = None
    protected_low: Optional[float] = None

@dataclass(frozen=True)
class AnalysisSnapshot:
    symbol: str
    timeframe: str
    last_closed_time: object
    last_closed_close: float
    swings: tuple[SwingPoint, ...]
    liquidity: tuple[LiquidityPool, ...]
    sweeps: tuple[LiquiditySweep, ...]
    structure_events: tuple[StructureEvent, ...]
