"""GUI orchestration hub.

The hub is a thin composition layer over the engine. It owns:
- a market-data source abstraction (MT5 in production, injected in tests)
- the lifecycle registry + setup store (engine-owned)
- the paper-execution authorization gate (backend-enforced, never frontend)
- the event/alert bus consumed by SSE clients

The hub NEVER computes SMC geometry itself; every analytical value comes from
engine primitives (structure/poi/fvg/execution_structure/setup/causal).
"""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from typing import Any, Optional

import pandas as pd

from ..causal import CausalMTFAnalyzer
from ..execution_policy import ExecutionPolicy
from ..fvg import detect_fvgs
from ..lifecycle import SetupLifecycle, SetupRegistry, SetupState as LCState
from ..models import SetupState as ReplayState
from ..poi import build_d1_pois, detect_displacement, update_poi_lifecycle
from ..risk import RiskEngine, allocate_portfolio_risk_budget, pending_price_is_valid
from ..setup import TradeSetup
from ..store import SetupStore
from ..strategy import MultiTimeframeConfig
from ..execution_structure import find_inducements, find_order_blocks
from ..structure import (
    build_liquidity_pools,
    detect_structure_breaks,
    detect_sweeps,
    find_swings,
)

TIMEFRAMES = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 16385, "H4": 16388, "D1": 16408,
}

LIVE_EXECUTION_ENABLED = False  # hard compile-time gate for this phase
MAX_TOTAL_RISK_PERCENT = 3.0
DEMO_MARKERS = ("demo", "trial")


def norm_symbol(symbol: str) -> str:
    """Canonical symbol key used ONLY for hub-side comparison/filtering.

    Uppercases and strips broker decoration (dots/whitespace) so an
    'EURAUD.'-style suffix still resolves to the same base identity.
    Broker-facing requests (bars/spec) keep the raw symbol unchanged.
    """
    return "".join(ch for ch in str(symbol).upper() if ch.isalnum())


def canon_state(state) -> LCState:
    """One canonical GUI lifecycle enum.

    The replay models.SetupState shares member names (FILLED) with the
    lifecycle machine, so cross-type values must be rejected explicitly
    instead of silently coercing to the wrong state space.
    """
    if isinstance(state, ReplayState):
        raise ValueError(f"replay state {state.value!r} is not GUI lifecycle state")
    if isinstance(state, LCState):
        return state
    return LCState(state)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "value"):  # str Enum
        return value.value
    return value


def _setup_dict(s: TradeSetup) -> dict:
    d = {
        "id": s.id, "symbol": s.symbol, "direction": s.direction.value,
        "created_time": str(s.created_time), "entry": s.entry,
        "stop_loss": s.stop_loss, "take_profit": s.take_profit,
        "protected_level": s.protected_level, "invalidation_level": s.invalidation_level,
        "risk_percent": s.risk_percent,
        "reward_risk": round(s.reward_distance / s.risk_distance, 3) if s.risk_distance else None,
    }
    return d


# --------------------------------------------------------------------------
# market data sources
# --------------------------------------------------------------------------
class MarketSource:
    name = "abstract"

    def connect(self) -> bool: raise NotImplementedError
    @property
    def connected(self) -> bool: raise NotImplementedError
    def account(self) -> Optional[dict]: raise NotImplementedError
    def symbols(self) -> list[str]: raise NotImplementedError
    def bars(self, symbol: str, tf_code: int, count: int) -> pd.DataFrame: raise NotImplementedError
    def tick(self, symbol: str) -> Optional[dict]: raise NotImplementedError
    def spec(self, symbol: str) -> Optional[dict]: raise NotImplementedError
    @property
    def is_demo(self) -> bool: return False
    def order_send(self, request: dict): raise RuntimeError("this source cannot send orders")
    def order_check(self, request: dict): raise RuntimeError("this source cannot preflight")
    def pending_orders(self, symbol: str) -> list[dict]: return []
    def positions(self, symbol: str) -> list[dict]: return []


class MT5Source(MarketSource):
    name = "mt5"

    def __init__(self, login: str | None = None, password: str | None = None,
                 server: str | None = None, path: str | None = None):
        self._connected = False
        self._mt5 = None
        self._creds = (login, password, server, path)

    def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            return False
        self._mt5 = mt5
        login, password, server, path = self._creds
        if login:
            ok = mt5.initialize(path=path or r"C:\Program Files\MetaTrader 5\terminal64.exe",
                                login=int(login), password=password or "",
                                server=server or "", timeout=60000)
        else:
            ok = mt5.initialize()
        self._connected = bool(ok)
        return self._connected

    @property
    def connected(self) -> bool:
        return self._connected

    def account(self):
        if not self._mt5:
            return None
        a = self._mt5.account_info()
        return None if a is None else {
            "login": a.login, "server": a.server, "balance": a.balance,
            "equity": a.equity, "currency": a.currency, "margin_free": a.margin_free,
        }

    @property
    def is_demo(self) -> bool:
        acct = self.account()
        return bool(acct) and any(t in str(acct["server"]).lower() for t in DEMO_MARKERS)

    def symbols(self):
        if not self._mt5:
            return []
        out = []
        for s in self._mt5.symbols_get() or ():
            if s.visible:
                out.append(s.name)
        return sorted(out)

    def bars(self, symbol, tf_code, count):
        self._mt5.symbol_select(symbol, True)
        rates = self._mt5.copy_rates_from_pos(symbol, tf_code, 1, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"no bars for {symbol}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df[["time", "open", "high", "low", "close", "tick_volume"]].reset_index(drop=True)

    def tick(self, symbol):
        t = self._mt5.symbol_info_tick(symbol) if self._mt5 else None
        return None if t is None else {"bid": t.bid, "ask": t.ask, "time": t.time}

    def spec(self, symbol):
        si = self._mt5.symbol_info(symbol) if self._mt5 else None
        if si is None:
            return None
        return {
            "symbol": symbol, "digits": si.digits, "point": si.point,
            "tick_size": si.trade_tick_size, "tick_value": si.trade_tick_value,
            "volume_min": si.volume_min, "volume_max": si.volume_max,
            "volume_step": si.volume_step, "trade_stops_level": si.trade_stops_level,
            "trade_freeze_level": si.trade_freeze_level, "filling_mode": si.filling_mode,
        }

    def order_send(self, request):
        return self._mt5.order_send(request)

    def order_check(self, request):
        return self._mt5.order_check(request)

    def pending_orders(self, symbol):
        return [
            {"ticket": o.ticket, "magic": o.magic, "type": o.type, "price_open": o.price_open,
             "sl": o.sl, "tp": o.tp, "volume": o.volume_current, "comment": o.comment}
            for o in (self._mt5.orders_get(symbol=symbol) or ())
        ]

    def positions(self, symbol):
        return [
            {"ticket": p.ticket, "magic": p.magic, "profit": p.profit, "comment": p.comment}
            for p in (self._mt5.positions_get(symbol=symbol) or ())
        ]


class DictSource(MarketSource):
    """Test/offline source: bars supplied by caller. Cannot connect to any broker."""
    name = "static"

    def __init__(self):
        self._bars: dict[tuple[str, int], pd.DataFrame] = {}
        self._connected = True
        self.account_info = {"login": 0, "server": "offline-static", "balance": 10000.0,
                             "equity": 10000.0, "currency": "USD", "margin_free": 10000.0}
        self.specs: dict[str, dict] = {}

    def prime(self, symbol, tf_code, df):
        self._bars[(symbol, tf_code)] = df

    def connect(self): return True

    @property
    def connected(self): return self._connected

    def account(self): return self.account_info

    def symbols(self): return sorted({k[0] for k in self._bars})

    def bars(self, symbol, tf_code, count):
        df = self._bars.get((symbol, tf_code))
        if df is None:
            raise RuntimeError(f"no data primed for {symbol} tf{tf_code}")
        return df.tail(count).reset_index(drop=True)

    def tick(self, symbol):
        df = self._bars.get((symbol, 15))
        if df is None:
            df = self._bars.get((symbol, 16408))
        if df is None or df.empty:
            return None
        last = df["close"].iloc[-1]
        return {"bid": float(last), "ask": float(last) * 1.0001, "time": 0}

    def spec(self, symbol):
        return self.specs.get(symbol) or {
            "symbol": symbol, "digits": 5, "point": 1e-5, "tick_size": 1e-5,
            "tick_value": 0.0, "volume_min": 0.01, "volume_max": 100.0,
            "volume_step": 0.01, "trade_stops_level": 0, "trade_freeze_level": 0,
            "filling_mode": 1,
        }

    @property
    def is_demo(self): return False


# --------------------------------------------------------------------------
# event bus + persistence
# --------------------------------------------------------------------------
class EventBus:
    def __init__(self, ring_size: int = 300):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.ring = deque(maxlen=ring_size)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, kind: str, payload: dict) -> None:
        event = {"id": uuid.uuid4().hex[:12], "time": pd.Timestamp.now(tz="UTC").isoformat(),
                 "kind": kind, "payload": _jsonable(payload)}
        self.ring.append(event)
        with self._lock:
            for q in self._subscribers:
                q.put(event)


class WebStore:
    """SQLite for GUI-owned records: hypotheses (weekly plan) + alerts.
    TradeSetup/lifecycle provenance stays in the engine SetupStore."""

    def __init__(self, path: str):
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS hypotheses(
          id TEXT PRIMARY KEY, symbol TEXT, timeframe TEXT, bias TEXT, thesis TEXT,
          trigger_note TEXT, confirmation TEXT, invalidation TEXT, target TEXT,
          state TEXT, created_time TEXT);
        CREATE TABLE IF NOT EXISTS alerts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, symbol TEXT, timeframe TEXT,
          kind TEXT, message TEXT, level TEXT);
        """)
        self._conn.commit()

    def add_hypothesis(self, h: dict) -> str:
        hid = h.get("id") or uuid.uuid4().hex[:10]
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO hypotheses VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (hid, h["symbol"], h["timeframe"], h["bias"], h.get("thesis", ""),
                 h.get("trigger", ""), h.get("confirmation", ""), h.get("invalidation", ""),
                 h.get("target", ""), h.get("state", "WATCHING"),
                 h.get("created_time", pd.Timestamp.now(tz="UTC").isoformat())))
        return hid

    def hypotheses(self) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM hypotheses ORDER BY created_time DESC")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def set_hypothesis_state(self, hid: str, state: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE hypotheses SET state=? WHERE id=?", (state, hid))

    def add_alert(self, symbol, timeframe, kind, message, level="INFO") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO alerts(time,symbol,timeframe,kind,message,level) VALUES(?,?,?,?,?,?)",
                (pd.Timestamp.now(tz="UTC").isoformat(), symbol, timeframe, kind, message, level))

    def alerts(self, limit: int = 100) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# hub
# --------------------------------------------------------------------------
class EngineHub:
    def __init__(self, source: MarketSource, db_path: str = "smc_engine_gui.sqlite3",
                 setup_store_path: str = "smc_engine_state.sqlite3", magic: int = 202609):
        self.source = source
        self.events = EventBus()
        self.web = WebStore(db_path)
        self.store = SetupStore(setup_store_path)
        self.registry = SetupRegistry()
        self.magic = magic
        self.bars_cache: dict[tuple[str, int], tuple[float, pd.DataFrame]] = {}
        self.bars_ttl = 10.0
        self.watchlist: list[str] = []
        self.registered_ids: set[str] = set()
        self.paper_tickets: dict[str, int] = {}  # setup id -> broker ticket
        self._lock = threading.RLock()
        self.risk_engines: dict[str, RiskEngine] = {}
        self._poller: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---------- status ----------
    def status(self) -> dict:
        acct = self.source.account() if self.source.connected else None
        mode = "DEMO" if acct and self.source.is_demo else ("LIVE" if acct else "DISCONNECTED")
        return {
            "engine": "CONNECTED",
            "mt5": "CONNECTED" if self.source.connected else "DISCONNECTED",
            "source": self.source.name,
            "account": acct,
            "account_mode": mode,
            "market_data": "LIVE" if self.source.connected else "DOWN",
            "risk_engine": "READY" if acct else "STANDBY",
            "execution": "READY" if (acct and self.source.is_demo) else "DISABLED",
            "live_execution_enabled": LIVE_EXECUTION_ENABLED,
            "paper_enabled": self.source.is_demo,
        }

    def symbols(self) -> list[str]:
        return self.source.symbols()

    # ---------- bars with cache ----------
    def bars(self, symbol: str, tf: str, count: int = 300) -> pd.DataFrame:
        code = TIMEFRAMES[tf]
        key = (symbol, code)
        now = time.time()
        with self._lock:
            hit = self.bars_cache.get(key)
            if hit and now - hit[0] < self.bars_ttl and len(hit[1]) >= min(count, 50):
                return hit[1].tail(count).reset_index(drop=True)
        df = self.source.bars(symbol, code, count)
        with self._lock:
            self.bars_cache[key] = (now, df)
        return df

    # ---------- analysis (engine primitives only) ----------
    def analysis(self, symbol: str, tf: str, count: int = 250) -> dict:
        self.watch(symbol)
        df = self.bars(symbol, tf, count)
        swings = find_swings(df)
        pools = build_liquidity_pools(swings)
        sweeps = detect_sweeps(df, pools, swings=swings)
        breaks = detect_structure_breaks(df, swings)
        fvgs = detect_fvgs(df)
        disp = detect_displacement(df)
        disp_idx = [i for i in disp.index
                    if bool(disp.at[i, "displacement_bullish"]) or bool(disp.at[i, "displacement_bearish"])]
        obs = find_order_blocks(df, disp_idx, timeframe=tf)
        idm = find_inducements(df, obs, swings)
        pois = [update_poi_lifecycle(p, df) for p in build_d1_pois(df)]
        candidates = self.candidates_for(symbol)
        last = df.iloc[-1]
        snapshot = {
            "symbol": symbol, "timeframe": tf,
            "last_closed_time": str(last["time"]), "last_closed_close": float(last["close"]),
            "swings": [{"id": s.id, "index": s.index, "time": str(s.time), "type": s.type.value,
                        "price": s.price, "confirmation_index": s.confirmation_index} for s in swings],
            "liquidity": [{"id": p.id, "side": p.side.value, "price": p.price,
                           "source_swing_id": p.source_swing_id} for p in pools],
            "sweeps": [{"id": w.id, "side": w.side.value, "swept_level": w.swept_level,
                        "sweep_extreme": w.sweep_extreme, "time": str(w.candle_time),
                        "close": w.close} for w in sweeps],
            "structure_events": [{"id": e.id, "type": e.type.value, "direction": e.direction.value,
                                  "level": e.level, "time": str(e.candle_time)} for e in breaks],
            "fvgs": [{"id": g.id, "direction": g.direction.value, "top": g.top, "bottom": g.bottom,
                      "time": str(g.created_time), "state": g.state} for g in fvgs],
            "order_blocks": [{"id": b.id, "direction": b.direction.value, "high": b.high,
                              "low": b.low, "time": str(b.candle_time) if b.candle_time else None}
                             for b in obs],
            "inducements": [{"id": i.id, "direction": i.direction.value, "level": i.level}
                            for i in idm],
            "pois": [{"id": p.id, "direction": p.direction.value, "low": p.low, "high": p.high,
                      "time": str(p.created_time), "state": p.state.value}
                     for p in pois if p.state.value in ("ACTIVE", "TOUCHED")],
            "candidates": candidates,
            "candles": [[str(r.time), float(r.open), float(r.high), float(r.low), float(r.close)]
                        for r in df.itertuples()],
        }
        self.events.publish("MARKET_UPDATE", {"symbol": symbol, "tf": tf,
                                              "price": snapshot["last_closed_close"]})
        return snapshot

    def candidates_for(self, symbol: str) -> list[dict]:
        out = []
        try:
            d1 = self.bars(symbol, "D1", 150)
            h4 = self.bars(symbol, "H4", 400)
            m15 = self.bars(symbol, "M15", 500)
        except Exception:
            return out
        analyzer = CausalMTFAnalyzer(symbol, MultiTimeframeConfig())
        for cand in analyzer.analyze_at(d1, h4, m15)[-3:]:
            self.register_setup(cand.setup)
            out.append(_setup_dict(cand.setup))
        return out

    def register_setup(self, setup: TradeSetup) -> None:
        with self._lock:
            if self.registry.get(setup.id) is not None:
                return
            self.registry.add(SetupLifecycle(setup))
            self.registered_ids.add(setup.id)
            self.watch(setup.symbol)
            try:
                self.store.upsert_setup(setup, LCState.EXECUTION_READY,
                                        pd.Timestamp.now(tz="UTC"))
            except Exception:
                import logging
                logging.getLogger(__name__).exception("failed to persist setup %s", setup.id)
            self.web.add_alert(setup.symbol, "causal", "SETUP_CREATED",
                               f"{setup.direction.value} setup {setup.id} became EXECUTION_READY")
            self.events.publish("SETUP_CREATED", {"setup": _setup_dict(setup)})

    def watch(self, symbol: str) -> None:
        with self._lock:
            if any(norm_symbol(w) == norm_symbol(symbol) for w in self.watchlist):
                return
            self.watchlist.insert(0, symbol)
            self.watchlist[:] = self.watchlist[:6]

    # ---------- lifecycle ----------
    LADDER = {
        "IDLE": "WATCHING", "POI_ACTIVE": "WATCHING", "LIQUIDITY_SWEPT": "DEVELOPING",
        "CSD_CONFIRMED": "DEVELOPING", "EXECUTION_READY": "EXECUTION_READY",
        "ORDER_PREPARED": "ORDER_PREPARED", "ORDER_PREFLIGHTED": "ORDER_PREPARED",
        "ORDER_SUBMITTING": "ORDER_PREPARED", "ORDER_PLACED": "ORDER_PLACED",
        "FILLED": "ORDER_PLACED", "POSITION_MANAGED": "ORDER_PLACED", "CLOSED": "COMPLETED",
        "POI_INVALIDATED": "INVALIDATED", "CSD_EXPIRED": "INVALIDATED",
        "OB_INVALIDATED": "INVALIDATED", "PROTECTED_LEVEL_BREACHED": "INVALIDATED",
        "ENTRY_NO_LONGER_VALID": "INVALIDATED", "RISK_REJECTED": "INVALIDATED",
        "BROKER_REJECTED": "INVALIDATED",
    }

    def lifecycle(self, symbol: Optional[str] = None) -> list[dict]:
        """Registered setups. `symbol` filters at this orchestration boundary:
        an EURUSD view can never surface an XAUUSD setup, and broker-suffixed
        names still match their base symbol."""
        want = norm_symbol(symbol) if symbol else None
        rows = []
        with self._lock:
            for sid in sorted(self.registered_ids):
                lc = self.registry.get(sid)
                if lc is None:
                    continue
                if want and norm_symbol(lc.setup.symbol) != want:
                    continue
                state = canon_state(lc.state)
                rows.append({
                    "setup_id": sid, "symbol": lc.setup.symbol,
                    "state": state.value, "display": self.LADDER[state.name],
                    "direction": lc.setup.direction.value,
                    "entry": lc.setup.entry, "sl": lc.setup.stop_loss, "tp": lc.setup.take_profit,
                    "risk_percent": lc.setup.risk_percent,
                    "ticket": self.paper_tickets.get(sid),
                    "reason": lc.reason,
                })
        return rows

    def _transition(self, sid: str, to, reason: str) -> bool:
        """Canonical, idempotent transition. Duplicate target states publish
        nothing and change no accounting. Returns whether it was applied."""
        lc = self.registry.get(sid)
        if lc is None:
            raise ValueError(f"unknown setup {sid}")
        target = canon_state(to)
        if lc.state is target:
            return False
        lc.transition(target, reason)
        event_map = {
            LCState.ORDER_PREPARED: "ORDER_PREPARED", LCState.ORDER_PLACED: "ORDER_PLACED",
            LCState.PROTECTED_LEVEL_BREACHED: "SETUP_INVALIDATED",
            LCState.POI_INVALIDATED: "SETUP_INVALIDATED", LCState.OB_INVALIDATED: "SETUP_INVALIDATED",
            LCState.RISK_REJECTED: "SETUP_INVALIDATED", LCState.BROKER_REJECTED: "SETUP_INVALIDATED",
        }
        if target.name in ("PROTECTED_LEVEL_BREACHED", "POI_INVALIDATED", "OB_INVALIDATED",
                           "RISK_REJECTED", "BROKER_REJECTED"):
            self.web.add_alert(lc.setup.symbol, "causal", "SETUP_INVALIDATED",
                               f"{sid} invalidated: {reason}", level="WARN")
        self.events.publish(event_map.get(target, "SETUP_UPDATED"),
                            {"setup_id": sid, "state": lc.state.value})
        return True

    # ---------- risk ----------
    def risk_engine(self, symbol: str) -> Optional[RiskEngine]:
        from ..market import SymbolSpec
        spec = self.source.spec(symbol)
        if not spec:
            return None
        if symbol not in self.risk_engines:
            mt5mod = self.source if isinstance(self.source, MT5Source) else None
            self.risk_engines[symbol] = RiskEngine(SymbolSpec(**spec), mt5_module=mt5mod)
        return self.risk_engines[symbol]

    def _committed_percent(self, exclude_setup_id: Optional[str] = None) -> float:
        total = 0.0
        for row in self.lifecycle():
            if exclude_setup_id and row["setup_id"] == exclude_setup_id:
                continue
            if row["display"] in ("EXECUTION_READY", "ORDER_PREPARED", "ORDER_PLACED"):
                total += row["risk_percent"]
        return total

    def risk_snapshot(self, symbol: str, setup_id: Optional[str] = None) -> dict:
        acct = self.source.account() or {}
        equity = float(acct.get("equity") or acct.get("balance") or 0)
        re_ = self.risk_engine(symbol)
        committed = self._committed_percent()
        out = {
            "symbol": symbol, "equity": equity, "currency": acct.get("currency"),
            "max_total_risk_percent": MAX_TOTAL_RISK_PERCENT,
            "committed_risk_percent": round(committed, 2),
            "available_risk_percent": round(MAX_TOTAL_RISK_PERCENT - committed, 2),
            "new_setup": None, "broker_view": None,
        }
        if setup_id and re_:
            lc = self.registry.get(setup_id)
            if lc and norm_symbol(lc.setup.symbol) != norm_symbol(symbol):
                raise ValueError(f"setup {setup_id} belongs to {lc.setup.symbol}, not {symbol}")
            if lc:
                try:
                    re_.validate_setup(lc.setup)
                    policy = ExecutionPolicy(re_)
                    order = policy.build_order(lc.setup, equity or 1.0)
                    loss = (equity or 0) * lc.setup.risk_percent / 100.0
                    out["new_setup"] = {
                        "setup_id": setup_id, "risk_percent": lc.setup.risk_percent,
                        "volume": order.volume, "estimated_loss": round(loss, 2),
                        "reward_risk": round(lc.setup.reward_distance / lc.setup.risk_distance, 3)
                        if lc.setup.risk_distance else None,
                    }
                    budget = allocate_portfolio_risk_budget(
                        [lc.setup], committed, MAX_TOTAL_RISK_PERCENT)
                    out["portfolio_allocation"] = "FIT" if budget else "EXCEEDS_BUDGET"
                except ValueError as exc:
                    out["new_setup"] = {"setup_id": setup_id, "error": str(exc)}
        return out

    # ---------- paper execution (backend-enforced) ----------
    def _paper_gate(self, request: dict) -> Optional[str]:
        """Allow-list gate. Anything not explicitly permitted is denied."""
        if not self.source.is_demo:
            return "account gate: non-demo broker"
        action = request.get("action")
        if action == "pending":
            symbol = request.get("symbol")
            if not symbol:
                return "no symbol"
            tracked = {norm_symbol(w) for w in self.watchlist}
            tracked |= {norm_symbol(self.registry.get(sid).setup.symbol)
                        for sid in self.registered_ids if self.registry.get(sid)}
            if norm_symbol(symbol) not in tracked:
                return f"symbol {symbol} not authorized for this session"
            if request.get("type") not in ("BUY_LIMIT", "SELL_LIMIT"):
                return "order type not allowed (market orders forbidden)"
            if not str(request.get("comment", "")).startswith("SMCGUI-"):
                return "foreign session comment"
            return None
        if action == "remove":
            if request.get("order") not in set(self.paper_tickets.values()):
                return "cancel of ticket not owned by this session"
            return None
        return f"action {action} not on allow-list"

    def dry_run(self, setup_id: str) -> dict:
        lc = self.registry.get(setup_id)
        if lc is None:
            raise ValueError("unknown setup")
        re_ = self.risk_engine(lc.setup.symbol)
        if re_ is None:
            raise ValueError("no broker symbol metadata (MT5 required)")
        acct = self.source.account() or {}
        try:
            re_.validate_setup(lc.setup)
            order = ExecutionPolicy(re_).build_order(lc.setup, float(acct.get("balance", 0) or 0))
            return {"status": "DRY_RUN_OK", "setup_id": setup_id, "order": asdict(order),
                    "note": "no order sent"}
        except ValueError as exc:
            return {"status": "DRY_RUN_REJECTED", "setup_id": setup_id, "reason": str(exc)}

    def paper_place(self, setup_id: str) -> dict:
        with self._lock:
            if not self.source.is_demo:
                raise PermissionError("PAPER EXECUTION REJECTED: not a demo account")
            lc = self.registry.get(setup_id)
            if lc is None:
                raise ValueError("unknown setup")
            if setup_id in self.paper_tickets:
                raise PermissionError("setup already has an active paper ticket")
            if lc.state is not LCState.EXECUTION_READY:
                raise PermissionError(f"setup state {lc.state.value} cannot be placed")
            re_ = self.risk_engine(lc.setup.symbol)
            if re_ is None:
                raise PermissionError("no broker metadata")
            re_.validate_setup(lc.setup)
            acct = self.source.account() or {}
            committed = self._committed_percent(exclude_setup_id=setup_id)
            budget = allocate_portfolio_risk_budget([lc.setup], committed, MAX_TOTAL_RISK_PERCENT)
            if not budget:
                raise PermissionError("portfolio risk budget exhausted")
            order = ExecutionPolicy(re_).build_order(lc.setup, float(acct.get("balance", 0) or 0))
            self._transition(setup_id, LCState.ORDER_PREPARED, "paper prepared")
            # live re-check of entry against current quote (engine helper, not hub math)
            tick = self.source.tick(lc.setup.symbol) or {}
            bid, ask = float(tick.get("bid", 0) or 0), float(tick.get("ask", 0) or 0)
            if bid and ask and not pending_price_is_valid(lc.setup.direction, order.entry, bid, ask):
                raise PermissionError("entry no longer valid at current market")
            virtual = {
                "action": "pending", "symbol": lc.setup.symbol, "volume": order.volume,
                "type": order.side, "price": order.entry, "sl": order.stop_loss,
                "tp": order.take_profit, "magic": self.magic,
                "comment": f"SMCGUI-{setup_id}"[:31],
            }
            reason = self._paper_gate(virtual)
            if reason:
                raise PermissionError(f"paper gate: {reason}")
            check = self.source.order_check(self._real_request(virtual))
            retcode = getattr(check, "retcode", None)
            if retcode not in (0, 10004):
                raise ValueError(f"order_check retcode={retcode}")
            self._transition(setup_id, LCState.ORDER_PREFLIGHTED, "order_check ok")
            self._transition(setup_id, LCState.ORDER_SUBMITTING, "paper submit")
            sent = self.source.order_send(self._real_request(virtual))
            code = getattr(sent, "retcode", None)
            if code != 10009:
                raise RuntimeError(f"broker rejected placement retcode={code} {getattr(sent,'comment','')}")
            ticket = sent.order
            self.paper_tickets[setup_id] = ticket
            self._transition(setup_id, LCState.ORDER_PLACED, f"ticket {ticket}")
            try:
                self.store.upsert_setup(lc.setup, LCState.ORDER_PLACED,
                                        pd.Timestamp.now(tz="UTC"), ticket=ticket)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("failed to persist placement %s", setup_id)
            self.web.add_alert(lc.setup.symbol, "causal", "ORDER_PLACED",
                               f"paper order placed ticket={ticket}", level="PAPER")
            return {"status": "ORDER_PLACED", "ticket": ticket, "order": asdict(order)}

    def _real_request(self, virtual: dict) -> dict:
        try:
            import MetaTrader5 as mt5
            order_type = mt5.ORDER_TYPE_BUY_LIMIT if virtual["type"] == "BUY_LIMIT" else mt5.ORDER_TYPE_SELL_LIMIT
            return {"action": mt5.TRADE_ACTION_PENDING, "symbol": virtual["symbol"],
                    "volume": virtual["volume"], "type": order_type, "price": virtual["price"],
                    "sl": virtual["sl"], "tp": virtual["tp"], "magic": virtual["magic"],
                    "comment": virtual["comment"], "type_time": mt5.ORDER_TIME_GTC,
                    "deviation": 0}
        except ImportError:
            raise RuntimeError("MT5 module unavailable; paper execution requires a broker terminal")

    def paper_cancel(self, setup_id: str) -> dict:
        with self._lock:
            ticket = self.paper_tickets.get(setup_id)
            lc = self.registry.get(setup_id)
            if ticket is None:
                if lc is not None and canon_state(lc.state) is LCState.CLOSED:
                    return {"status": "ALREADY_CANCELLED", "ticket": None}
                raise ValueError("no paper ticket for setup")
            symbol = lc.setup.symbol if lc else None
            # broker book is authoritative: if our ticket is gone, nothing to send
            live = {o["ticket"] for o in self.source.pending_orders(symbol)
                    if o.get("magic") == self.magic} if symbol else set()
            if ticket not in live:
                self.paper_tickets.pop(setup_id, None)
                if lc is not None:
                    try:
                        self._transition(setup_id, LCState.CLOSED, "ticket no longer at broker")
                    except Exception:
                        pass
                return {"status": "ALREADY_CANCELLED", "ticket": ticket}
            reason = self._paper_gate({"action": "remove", "order": ticket})
            if reason:
                raise PermissionError(f"paper gate: {reason}")
            import MetaTrader5 as mt5
            sent = self.source.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
            if getattr(sent, "retcode", None) != 10009:
                raise RuntimeError(f"cancel rejected retcode={getattr(sent,'retcode',None)}")
            self.paper_tickets.pop(setup_id, None)
            if lc is not None:
                try:
                    self._transition(setup_id, LCState.CLOSED, "pending cancelled")
                except Exception:
                    pass
            self.events.publish("ORDER_CANCELLED", {"setup_id": setup_id, "ticket": ticket})
            self.web.add_alert(symbol or "?", "causal", "ORDER_CANCELLED",
                               f"paper order cancelled ticket={ticket}", level="PAPER")
            return {"status": "CANCELLED", "ticket": ticket}

    def execution_state(self, setup_id: str) -> dict:
        lc = self.registry.get(setup_id)
        if lc is None:
            raise ValueError("unknown setup")
        try:
            dry = self.dry_run(setup_id)
        except Exception as exc:
            dry = {"status": "UNAVAILABLE", "reason": str(exc)}
        state = canon_state(lc.state)
        return {"setup_id": setup_id, "lifecycle": state.value,
                "display": self.LADDER[state.name],
                "ticket": self.paper_tickets.get(setup_id), "mode": "DRY RUN",
                "account_mode": "DEMO" if self.source.is_demo else "DISCONNECTED/LIVE",
                "dry_run": dry}

    # ---------- hypotheses ----------
    def add_hypothesis(self, data: dict) -> dict:
        hid = self.web.add_hypothesis(data)
        self.events.publish("ALERT_CREATED", {"kind": "HYPOTHESIS", "id": hid})
        return {"id": hid, **data}

    # ---------- history ----------
    def history(self) -> list[dict]:
        try:
            conn = sqlite3.connect(self.store.path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT setup_id,symbol,state,created_time,updated_time,ticket,setup_json,reason "
                "FROM setups ORDER BY updated_time DESC LIMIT 200").fetchall()
            conn.close()
        except Exception:
            return []
        out = []
        for r in rows:
            d = dict(r)
            try:
                payload = json.loads(d.pop("setup_json"))
                d.update({k: payload.get(k) for k in
                          ("direction", "entry", "stop_loss", "take_profit", "risk_percent")})
                e, s_, t_ = d.get("entry"), d.get("stop_loss"), d.get("take_profit")
                d["reward_risk"] = (round(abs(t_ - e) / abs(e - s_), 2)
                                    if None not in (e, s_, t_) and e != s_ else None)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                import logging
                logging.getLogger(__name__).warning("history row %s merge failed: %s", d.get("setup_id"), exc)
            out.append(d)
        return out

    # ---------- poller ----------
    def start_poller(self, interval: float = 15.0) -> None:
        if self._poller and self._poller.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(interval):
                for sym in list(self.watchlist):
                    try:
                        cands = self.candidates_for(sym)
                        self.events.publish("MARKET_UPDATE", {"symbol": sym,
                                                              "candidates": len(cands)})
                    except Exception:
                        continue

        self._poller = threading.Thread(target=loop, daemon=True)
        self._poller.start()

    def stop_poller(self) -> None:
        self._stop.set()
