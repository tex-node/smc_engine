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
import os
import queue
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from typing import Any, Optional

import pandas as pd

from ..causal import CausalMTFAnalyzer, CausalProvenance
from ..execution_policy import ExecutionPolicy
from ..fvg import detect_fvgs
from ..lifecycle import SetupLifecycle, SetupRegistry, SetupState as LCState
from ..models import SetupState as ReplayState
from ..poi import build_d1_pois, detect_displacement, update_poi_lifecycle
from ..risk import RiskEngine, allocate_portfolio_risk_budget, pending_price_is_valid
from ..setup import TradeSetup, evaluate_setup_lifecycle as _eval_lifecycle
from ..store import SetupStore
from ..strategy import MultiTimeframeConfig
from ..execution_structure import find_inducements, find_order_blocks
from ..structure import (
    build_liquidity_pools,
    detect_structure_breaks,
    detect_sweeps,
    find_swings,
)
from .history import SetupEventHistory, timeframe_from_id
from .runtime import build_fingerprint, record_startup
from .dbwrite import run_write, serialized_read, serialized_write, db_write_diagnostics
from ..opportunity import OpportunityEngine, OpportunityRepository
from ..opportunity import evaluator as opp_evaluator

TIMEFRAMES = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 16385, "H4": 16388, "D1": 16408,
}

LIVE_EXECUTION_ENABLED = False  # hard compile-time gate for this phase
AUTHORIZED_LOGIN = 477217728
AUTHORIZED_SERVER = "Exness-MT5Trial9"
MAX_TOTAL_RISK_PERCENT = 3.0
DEMO_MARKERS = ("demo", "trial")

# Display states that indicate a setup has left EXECUTION_READY and is now terminal.
# Terminal setups remain idempotent in the registry; they are never resurrected.
_TERMINAL_DISPLAY_STATES = frozenset({"INVALIDATED", "COMPLETED", "ORDER_PLACED"})

# Bounded zero-tick retry for freshly activated symbols (feed subscription
# latency). Strictly read-only: symbol_info_tick only — no select, no order
# primitives, finite attempts, small delay, never a fabricated price.
QUOTE_ATTEMPTS = 5
QUOTE_RETRY_DELAY = 0.4


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

    @property
    def mt5_module(self):
        """The underlying MetaTrader5 module (constants + order_calc_profit).

        RiskEngine math requires the real module's ORDER_TYPE_* constants and
        order_calc_profit(); passing the adapter object instead raised
        AttributeError('MT5Source' object has no attribute 'ORDER_TYPE_SELL')
        which surfaced as an HTTP 500 on /api/risk.
        """
        return self._mt5

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
        self._lock = threading.RLock()
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
        self._lock = threading.RLock()
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

    @serialized_write
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

    @serialized_read
    def hypotheses(self) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM hypotheses ORDER BY created_time DESC")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    @serialized_write
    def set_hypothesis_state(self, hid: str, state: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE hypotheses SET state=? WHERE id=?", (state, hid))

    @serialized_write
    def add_alert(self, symbol, timeframe, kind, message, level="INFO") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO alerts(time,symbol,timeframe,kind,message,level) VALUES(?,?,?,?,?,?)",
                (pd.Timestamp.now(tz="UTC").isoformat(), symbol, timeframe, kind, message, level))

    @serialized_read
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
        record_startup()
        self.source = source
        self.events = EventBus()
        self.web = WebStore(db_path)
        self.event_history = SetupEventHistory(db_path)  # GUI-owned audit tables
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
        self._history_failures = 0
        # ---------- opportunity layer (non-executing; UI-independent) ----------
        self.opportunities = OpportunityEngine(OpportunityRepository(db_path),
                                               config=MultiTimeframeConfig())
        self._opp_last_m15: dict[str, object] = {}
        self._symbol_diag: dict[str, dict] = {}
        self._last_scan: dict = {}
        self._last_scan_time: Optional[str] = None

    # ---------- account identity gate ----------
    def _account_identity(self) -> dict:
        """Return identity check result for the current source.

        Gate applies only to MT5Source (name='mt5'). DictSource, FakeDemoSource,
        and any non-MT5 source bypass it so tests and offline mode are unaffected.
        When not connected, treats as authorized (connection not yet established).
        """
        if self.source.name != "mt5":
            return {"authorized": True, "login": None, "server": None,
                    "mode": None, "reason": "non-mt5-source"}
        acct = self.source.account() if self.source.connected else None
        if not acct:
            return {"authorized": True, "login": None, "server": None,
                    "mode": None, "reason": "not-connected"}
        login = acct.get("login")
        server = str(acct.get("server", ""))
        mode = "DEMO" if self.source.is_demo else "LIVE"
        if login != AUTHORIZED_LOGIN:
            return {"authorized": False, "login": login, "server": server,
                    "mode": mode, "reason": f"login {login} != {AUTHORIZED_LOGIN}"}
        if AUTHORIZED_SERVER not in server:
            return {"authorized": False, "login": login, "server": server,
                    "mode": mode, "reason": f"server '{server}' != '{AUTHORIZED_SERVER}'"}
        if mode != "DEMO":
            return {"authorized": False, "login": login, "server": server,
                    "mode": mode, "reason": f"account mode is {mode}, must be DEMO"}
        return {"authorized": True, "login": login, "server": server,
                "mode": mode, "reason": "authorized"}

    def _require_authorized(self) -> None:
        """Raise PermissionError if connected to an unauthorized MT5 account."""
        ident = self._account_identity()
        if not ident["authorized"]:
            raise PermissionError(
                f"UNAUTHORIZED_ACCOUNT: {ident['login']}@{ident['server']} ({ident['mode']}). "
                f"Authorized: {AUTHORIZED_LOGIN}@{AUTHORIZED_SERVER} DEMO. "
                f"Reason: {ident['reason']}"
            )

    # ---------- setup-event history (observational; never affects analysis) ----------
    def _safe_history(self, fn, *args, **kwargs) -> None:
        """§23: history persistence failure must NEVER break market analysis."""
        try:
            fn(*args, **kwargs)
        except Exception:
            import logging
            self._history_failures += 1
            logging.getLogger(__name__).exception("setup-history persistence failed "
                                                  "(market analysis unaffected)")

    # ---------- readiness (derived, read path + history observation) ----------
    _READINESS_REQUIRED = ("setup_id", "symbol", "direction", "entry", "sl", "tp",
                           "risk_percent", "state")

    def readiness(self, symbol: Optional[str] = None) -> dict:
        """Gate B readiness view derived from the engine lifecycle.

        Presentation state only — deliberately NOT a lifecycle state.
        A setup counts only when it is a genuine engine-causal id with
        complete fields. Incomplete or foreign-id rows are rejected here
        (and counted), never presented as executable. Discovery of a
        genuine setup is mirrored into the setup-event history as a pure
        side observation; it never triggers execution.
        """
        ident = self._account_identity()
        if not ident["authorized"]:
            return {
                "status": "UNAUTHORIZED_ACCOUNT",
                "setup": None,
                "detected_ids": [],
                "rejected_incomplete": 0,
                "terminal_historical_count": 0,
                "mode": f"UNAUTHORIZED: {ident['reason']}",
            }
        rows = self.lifecycle(symbol)
        complete = [r for r in rows
                    if str(r.get("setup_id", "")).startswith("SETUP-")
                    and all(r.get(k) is not None and r.get(k) != "" for k in self._READINESS_REQUIRED)]
        valid = [r for r in complete if r.get("display") == "EXECUTION_READY"]
        # Semantic breakdown: incomplete (missing fields) vs terminal (complete but consumed)
        rejected_incomplete = len(rows) - len(complete)
        terminal_historical_count = len(complete) - len(valid)
        for r in valid:
            self._safe_history(self.event_history.observe, {
                "setup_id": r["setup_id"], "symbol": r["symbol"],
                "timeframe": timeframe_from_id(r["setup_id"]),
                "direction": r["direction"], "entry": r["entry"],
                "stop_loss": r["sl"], "take_profit": r["tp"],
                "risk_percent": r["risk_percent"], "rr": r.get("rr"),
                "as_of": r["as_of"], "evidence": r["evidence"],
            })
        if valid:
            return {
                "status": "READY_FOR_MANUAL_VALIDATION",
                "setup": valid[0],
                "detected_ids": [r["setup_id"] for r in valid],
                "rejected_incomplete": rejected_incomplete,
                "terminal_historical_count": terminal_historical_count,
                "opportunities": self.active_opportunities(symbol),
                "opportunity_audit": self.opportunity_audit(symbol) if symbol else None,
                "mode": "GATE B WAITING FOR MANUAL VALIDATION — NO AUTO-EXECUTION",
            }
        return {
            "status": "WAITING_FOR_CAUSAL_SETUP",
            "setup": None,
            "detected_ids": [],
            "rejected_incomplete": rejected_incomplete,
            "terminal_historical_count": terminal_historical_count,
            "opportunities": self.active_opportunities(symbol),
            "opportunity_audit": self.opportunity_audit(symbol) if symbol else None,
            "mode": "OBSERVATION — VALID MARKET STATE, NOT AN ERROR",
        }

    # ---------- status ----------
    def status(self) -> dict:
        acct = self.source.account() if self.source.connected else None
        mode = "DEMO" if acct and self.source.is_demo else ("LIVE" if acct else "DISCONNECTED")
        ident = self._account_identity()
        authorized = ident["authorized"]
        actual_login = ident.get("login")
        actual_server = ident.get("server")
        actual_mode = ident.get("mode")
        return {
            "engine": "CONNECTED" if authorized else "UNAUTHORIZED_ACCOUNT",
            "mt5": "CONNECTED" if self.source.connected else "DISCONNECTED",
            "source": self.source.name,
            "account": acct,
            "account_mode": mode,
            "market_data": "LIVE" if self.source.connected else "DOWN",
            "risk_engine": ("READY" if authorized else "UNAUTHORIZED") if acct else "STANDBY",
            "execution": "READY" if (acct and self.source.is_demo and authorized) else "DISABLED",
            "live_execution_enabled": LIVE_EXECUTION_ENABLED,
            "paper_enabled": self.source.is_demo and authorized,
            "account_identity": "AUTHORIZED" if authorized else "UNAUTHORIZED",
            "engine_operational": authorized,
            "authorized_account": f"{AUTHORIZED_LOGIN}@{AUTHORIZED_SERVER} DEMO",
            "actual_account": (
                f"{actual_login}@{actual_server} {actual_mode}"
                if actual_login is not None else "not-connected"
            ),
        }

    def symbols(self) -> list[str]:
        return self.source.symbols()

    # ---------- runtime fingerprint (read-only) ----------
    def runtime_info(self) -> dict:
        """Source/process identity with no broker or execution calls."""
        st = self.status()
        acct = st.get("account") or {}
        ident = self._account_identity()
        fp = build_fingerprint(
            pid=os.getpid(),
            account_mode=st.get("account_mode", "DEMO"),
            account_login=acct.get("login"),
        )
        fp["authorized_account"] = f"{AUTHORIZED_LOGIN}@{AUTHORIZED_SERVER} DEMO"
        actual_login = ident.get("login")
        actual_server = ident.get("server")
        actual_mode = ident.get("mode")
        fp["actual_account"] = (
            f"{actual_login}@{actual_server} {actual_mode}"
            if actual_login is not None else "not-connected"
        )
        fp["account_identity"] = "AUTHORIZED" if ident["authorized"] else "UNAUTHORIZED"
        fp["engine_operational"] = ident["authorized"]
        return fp

    # ---------- market-data diagnostic (read-only) ----------
    def market_diagnostic(self, symbol: str, tf: str, count: int = 250) -> dict:
        """Structured market-data availability report. Never executes."""
        code = TIMEFRAMES.get(tf)
        if code is None:
            return {"error": f"unknown timeframe {tf}", "symbol": symbol, "tf": tf,
                    "resolved_broker_symbol": None, "mt5_connected": self.source.connected,
                    "returned_bar_count": 0, "latest_candle_time": None}
        mt5_connected = self.source.connected
        try:
            spec = self.source.spec(symbol) or {}
            resolved = spec.get("symbol", symbol)
        except Exception as exc:
            resolved = f"error:{exc}"
            spec = {}
        bar_count = 0
        latest_candle_time = None
        error: Optional[str] = None
        try:
            df = self.bars(symbol, tf, count)
            bar_count = len(df)
            if bar_count:
                latest_candle_time = str(df.iloc[-1]["time"])
        except Exception as exc:
            error = str(exc)
        return {
            "symbol": symbol,
            "requested_tf": tf,
            "requested_bar_count": count,
            "resolved_broker_symbol": resolved,
            "mt5_connected": mt5_connected,
            "returned_bar_count": bar_count,
            "latest_candle_time": latest_candle_time,
            "error": error,
        }

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
        self._require_authorized()
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
        # Opportunity layer observation for the actively-viewed symbol.
        # Discovery itself never depends on this path (see scan_universe_once).
        try:
            self._dispatch_opportunity_events(
                self.opportunities.observe(symbol, self._causal_view(symbol)))
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "opportunity observation failed for %s (analysis unaffected)", symbol)
        last = df.iloc[-1]
        quote = self._quote_block(symbol)
        snapshot = {
            "symbol": symbol, "timeframe": tf,
            "identity": self._identity(),
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
            "candidate_summary": {
                "structurally_qualified_count": len(candidates),
                "new_admissible_count": sum(
                    1 for c in candidates if c.get("admission") == "NEW_ADMISSIBLE"
                ),
                "terminal_existing_count": sum(
                    1 for c in candidates if c.get("admission") == "TERMINAL_HISTORICAL"
                ),
                "active_registered_count": sum(
                    1 for c in candidates if c.get("admission") == "ACTIVE_REGISTERED"
                ),
            },
            "quote": quote,
            "opportunities": self.active_opportunities(symbol),
            "opportunity_audit": self.opportunity_audit(symbol),
            "last_closed_candle_time": str(last["time"]),
            "candles": [[str(r.time), float(r.open), float(r.high), float(r.low), float(r.close)]
                        for r in df.itertuples()],
        }
        self.events.publish("MARKET_UPDATE", {"symbol": symbol, "tf": tf,
                                              "price": snapshot["last_closed_close"]})
        return snapshot

    # ---------- market-data quote + identity (read-only) ----------
    def _quote_block(self, symbol: str) -> dict:
        """Bounded zero-tick retry immediately after symbol activation.

        States: AVAILABLE / WAITING_FOR_LIVE_TICK (tick object exists but
        bid/ask are still zero while history is fine) / UNAVAILABLE (no tick
        object at all). Never substitutes candle closes or last prices.
        """
        saw_tick_without_price = False
        for attempt in range(QUOTE_ATTEMPTS):
            try:
                t = self.source.tick(symbol)
            except Exception:
                t = None
            if t is not None and t.get("bid") and t.get("ask"):
                return {"market_data": "AVAILABLE", "bid": float(t["bid"]),
                        "ask": float(t["ask"]),
                        "spread": round(float(t["ask"]) - float(t["bid"]), 8),
                        "tick_time": str(t.get("time")), "source": self.source.name}
            if t is not None:
                saw_tick_without_price = True
            if attempt < QUOTE_ATTEMPTS - 1:
                time.sleep(QUOTE_RETRY_DELAY)
        state = "WAITING_FOR_LIVE_TICK" if saw_tick_without_price else "UNAVAILABLE"
        return {"market_data": state, "bid": None, "ask": None, "spread": None,
                "tick_time": None, "source": self.source.name}

    def _identity(self) -> dict:
        """Authoritative server/account identity from the live source.

        Rendered by the GUI so a stale tab can never masquerade as a
        different broker instance. Derived, never hard-coded.
        """
        acct = self.source.account() if self.source.connected else None
        if acct is None:
            account_class = "DISCONNECTED"
        elif self.source.is_demo:
            account_class = "DEMO"
        else:
            account_class = "LIVE"
        return {"login": acct and acct.get("login"), "server": acct and acct.get("server"),
                "account_class": account_class, "source": self.source.name,
                "connected": bool(self.source.connected)}

    def _registry_admission_summary(self, symbol: str) -> dict:
        """Read-only registry snapshot for a symbol.

        Returns counts that distinguish structurally qualified setups from
        newly admissible ones, without running the causal engine again.
        Never registers, never modifies state.
        """
        execution_ready = 0
        terminal = 0
        active_non_ready = 0
        want = norm_symbol(symbol)
        with self._lock:
            for sid in self.registered_ids:
                lc = self.registry.get(sid)
                if lc is None:
                    continue
                if norm_symbol(lc.setup.symbol) != want:
                    continue
                display = self.LADDER.get(canon_state(lc.state).name, "")
                if display == "EXECUTION_READY":
                    execution_ready += 1
                elif display in _TERMINAL_DISPLAY_STATES:
                    terminal += 1
                else:
                    active_non_ready += 1
        return {
            "execution_ready_count": execution_ready,
            "terminal_existing_count": terminal,
            "active_non_ready_count": active_non_ready,
            "total_registered_count": execution_ready + terminal + active_non_ready,
        }

    def candidates_for(self, symbol: str) -> list[dict]:
        """Causal engine output with its original evidence chain attached.

        Only fields produced by the engine are serialized here. The GUI must
        not infer, add, or substitute evidence; absent evidence is absent.
        """
        out = []
        try:
            d1 = self.bars(symbol, "D1", 150)
            h4 = self.bars(symbol, "H4", 400)
            m15 = self.bars(symbol, "M15", 500)
        except Exception:
            return out
        analyzer = CausalMTFAnalyzer(symbol, MultiTimeframeConfig())
        all_cands = analyzer.analyze_at(d1, h4, m15)
        market_as_of = str(m15["time"].iloc[-1])
        observed_ids = {c.setup.id for c in all_cands}

        # Classify each candidate BEFORE registration so we can report the
        # admission distinction: new (not yet in registry) vs terminal
        # (already in registry but in a consumed/invalidated state).
        new_admissible = 0
        terminal_existing = 0
        for cand in all_cands[-3:]:
            pre_reg_lc = self.registry.get(cand.setup.id)
            if pre_reg_lc is None:
                admission = "NEW_ADMISSIBLE"
                new_admissible += 1
            else:
                display = self.LADDER.get(canon_state(pre_reg_lc.state).name, "")
                if display in _TERMINAL_DISPLAY_STATES:
                    admission = "TERMINAL_HISTORICAL"
                    terminal_existing += 1
                else:
                    admission = "ACTIVE_REGISTERED"

            self.register_setup(cand.setup)
            row = _setup_dict(cand.setup)
            row["timeframe"] = cand.setup.order_block_id.split("-")[1] \
                if cand.setup.order_block_id.count("-") >= 2 else "M15"
            row["as_of"] = market_as_of
            row["admission"] = admission
            row["evidence"] = {
                "poi_id": cand.setup.poi_id,
                "sweep": {"id": cand.sweep.id, "side": cand.sweep.side.value,
                          "swept_level": cand.sweep.swept_level,
                          "sweep_extreme": cand.sweep.sweep_extreme,
                          "time": str(cand.sweep.candle_time)},
                "csd": {"id": cand.csd.id, "type": cand.csd.type.value,
                        "direction": cand.csd.direction.value, "level": cand.csd.level,
                        "time": str(cand.csd.candle_time)},
                "order_block_id": cand.setup.order_block_id,
                "inducement_id": cand.setup.inducement_id,
                "irl_swing_id": cand.setup.irl_swing_id,
                "irl_target_type": cand.setup.irl_target_type,
                "irl_qualification_reason": cand.setup.irl_qualification_reason,
            }
            self._safe_history(self.event_history.observe, self._history_payload(row))
            out.append(row)
        # full-set sweep: ids the production pipeline no longer produces are
        # EXPIRED in the audit trail (never deleted, never fake-inserted)
        self._safe_history(self.event_history.expire_absent, symbol, observed_ids)
        # Evaluate every EXECUTION_READY setup for this symbol against the
        # current closed M15 bar sequence. This is the single production call
        # site for evaluate_setup_lifecycle; it runs on every causal scan
        # (poller + on-demand analysis) and is idempotent across repeated ticks.
        with self._lock:
            pending_eval = [
                sid for sid in self.registered_ids
                if self.registry.get(sid) is not None
                and norm_symbol(self.registry.get(sid).setup.symbol) == norm_symbol(symbol)
                and self.registry.get(sid).state is LCState.EXECUTION_READY
            ]
        for sid in pending_eval:
            self._apply_lifecycle_evaluation(sid, m15)
        return out

    def causal_scan_provenance(self, symbol: str) -> dict:
        """Trace the causal chain for one symbol. Returns rejection stage + counts."""
        self._require_authorized()
        try:
            d1 = self.bars(symbol, "D1", 150)
            h4 = self.bars(symbol, "H4", 400)
            m15 = self.bars(symbol, "M15", 500)
        except Exception as exc:
            return {
                "symbol": symbol, "as_of": None,
                "d1_poi_count": 0, "matching_sweep_count": 0, "csd_count": 0,
                "post_csd_ob_count": 0, "unmitigated_ob_count": 0, "idm_count": 0,
                "structurally_qualified_count": 0,
                "rr_filtered_count": 0, "rr_gate": "",
                "new_admissible_count": 0,
                "execution_ready_count": 0,
                "engine_execution_ready_count": 0,
                "qualified_candidate_count": 0,
                "rejection_stage": "D1_POI", "rejection_reason": f"data unavailable: {exc}",
            }
        prov = CausalMTFAnalyzer(symbol, MultiTimeframeConfig()).trace_chain(d1, h4, m15)
        reg = self._registry_admission_summary(symbol)
        # new_admissible = structurally qualified setups that are also execution-ready
        # and not already in a terminal lifecycle state.
        new_admissible = max(0, prov.execution_ready_count - reg["terminal_existing_count"])
        return {
            "symbol": prov.symbol, "as_of": str(prov.as_of),
            "d1_poi_count": prov.d1_poi_count,
            "matching_sweep_count": prov.matching_sweep_count,
            "csd_count": prov.csd_count,
            "post_csd_ob_count": prov.post_csd_ob_count,
            "unmitigated_ob_count": prov.unmitigated_ob_count,
            "idm_count": prov.idm_count,
            "structurally_qualified_count": prov.structurally_qualified_count,
            "rr_filtered_count": prov.rr_filtered_count,
            "rr_gate": prov.rr_gate,
            "new_admissible_count": new_admissible,
            # execution_ready_count: registry-based (how many setups are in EXECUTION_READY
            # lifecycle state); kept for backward compatibility with existing tests/consumers.
            # engine_execution_ready_count: engine-based (how many pass all gates in this scan).
            "execution_ready_count": reg["execution_ready_count"],
            "engine_execution_ready_count": prov.execution_ready_count,
            "qualified_candidate_count": prov.qualified_candidate_count,
            # Registry admission breakdown (derived from current lifecycle state):
            "terminal_existing_count": reg["terminal_existing_count"],
            "total_registered_count": reg["total_registered_count"],
            "rejection_stage": prov.rejection_stage,
            "rejection_reason": prov.rejection_reason,
        }

    def causal_scan_universe(self) -> dict:
        """Trace causal chain for all broker symbols. Read-only observability scan."""
        self._require_authorized()
        syms = self.symbols()
        results = []
        for sym in syms:
            try:
                results.append(self.causal_scan_provenance(sym))
            except PermissionError:
                raise
            except Exception as exc:
                results.append({
                    "symbol": sym, "as_of": None,
                    "d1_poi_count": 0, "matching_sweep_count": 0, "csd_count": 0,
                    "post_csd_ob_count": 0, "unmitigated_ob_count": 0, "idm_count": 0,
                    "qualified_candidate_count": 0,
                    "rejection_stage": "D1_POI", "rejection_reason": f"scan error: {exc}",
                })
        return {
            "symbols_scanned": len(results),
            "candidates_found": sum(r["qualified_candidate_count"] for r in results),
            "scan": results,
        }

    @staticmethod
    def _history_payload(cand_row: dict) -> dict:
        """Map a candidate row to the history payload shape."""
        return {
            "setup_id": cand_row["id"], "symbol": cand_row["symbol"],
            "timeframe": cand_row.get("timeframe"), "direction": cand_row["direction"],
            "entry": cand_row["entry"], "stop_loss": cand_row["stop_loss"],
            "take_profit": cand_row["take_profit"], "risk_percent": cand_row["risk_percent"],
            "rr": cand_row.get("reward_risk"), "as_of": cand_row.get("as_of"),
            "evidence": cand_row.get("evidence"),
        }

    # Orchestration-boundary completeness guard. The engine's own
    # build_trade_setup validates geometry; this protects the hub from
    # manually injected/incomplete objects (readiness §5: reject, never crash).
    _COMPLETE_FIELDS = ("id", "symbol", "direction", "entry", "stop_loss",
                        "take_profit", "risk_percent", "protected_level")

    def register_setup(self, setup: TradeSetup) -> None:
        self._require_authorized()
        with self._lock:
            if any(getattr(setup, f, None) is None
                   or (isinstance(getattr(setup, f, None), float) and
                       getattr(setup, f) != getattr(setup, f))  # NaN guard
                   for f in self._COMPLETE_FIELDS):
                import logging
                logging.getLogger(__name__).warning(
                    "rejected incomplete setup at orchestration boundary: %s", setup.id)
                return
            if self.registry.get(setup.id) is not None:
                return
            self.registry.add(SetupLifecycle(setup))
            self.registered_ids.add(setup.id)
            self.watch(setup.symbol)
            try:
                run_write(lambda: self.store.upsert_setup(
                    setup, LCState.EXECUTION_READY, pd.Timestamp.now(tz="UTC")))
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
                    # causal provenance carried from the engine's own setup
                    "as_of": str(lc.setup.created_time),
                    "rr": (round(lc.setup.reward_distance / lc.setup.risk_distance, 3)
                           if lc.setup.risk_distance else None),
                    "evidence": {
                        "poi_id": lc.setup.poi_id, "sweep_id": lc.setup.sweep_id,
                        "csd_id": lc.setup.csd_id, "order_block_id": lc.setup.order_block_id,
                        "inducement_id": lc.setup.inducement_id,
                        "irl_swing_id": lc.setup.irl_swing_id,
                        "irl_target_type": lc.setup.irl_target_type,
                        "irl_qualification_reason": lc.setup.irl_qualification_reason,
                        "protected_level": lc.setup.protected_level,
                        "invalidation_level": lc.setup.invalidation_level,
                    },
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
        previous = lc.state.value
        lc.transition(target, reason)
        self._safe_history(self.event_history.record_lifecycle, sid, previous, target.value, reason)
        event_map = {
            LCState.ORDER_PREPARED: "ORDER_PREPARED", LCState.ORDER_PLACED: "ORDER_PLACED",
            LCState.FILLED: "SETUP_FILLED",
            LCState.PROTECTED_LEVEL_BREACHED: "SETUP_INVALIDATED",
            LCState.POI_INVALIDATED: "SETUP_INVALIDATED", LCState.OB_INVALIDATED: "SETUP_INVALIDATED",
            LCState.RISK_REJECTED: "SETUP_INVALIDATED", LCState.BROKER_REJECTED: "SETUP_INVALIDATED",
            LCState.ENTRY_NO_LONGER_VALID: "SETUP_INVALIDATED",
        }
        if target.name in ("PROTECTED_LEVEL_BREACHED", "POI_INVALIDATED", "OB_INVALIDATED",
                           "RISK_REJECTED", "BROKER_REJECTED", "ENTRY_NO_LONGER_VALID"):
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
            # RiskEngine expects the MetaTrader5 MODULE (ORDER_TYPE_* +
            # order_calc_profit), not the adapter. Test/offline sources
            # expose no module -> RiskEngine uses its spec-arithmetic fallback.
            mt5mod = getattr(self.source, "mt5_module", None)
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
        """Deterministic structured risk result — never a bare 500.

        status: RISK_OK | RISK_REJECTED | RISK_PORTFOLIO_REJECTED
                RISK_UNAVAILABLE | RISK_NOT_REQUESTED
        Legacy keys (equity/committed_risk_percent/new_setup/portfolio_allocation)
        are preserved for existing consumers; the structured keys follow the
        agreed schema. No sizing math lives here: all values come from
        RiskEngine/ExecutionPolicy.
        """
        try:
            acct = self.source.account() or {}
        except Exception:
            acct = {}
        try:
            identity = self._identity()
        except Exception:
            identity = {"login": None, "server": None, "account_class": "DISCONNECTED",
                        "source": getattr(self.source, "name", "unknown"), "connected": False}
        equity = float(acct.get("equity") or acct.get("balance") or 0)
        re_, meta_error = None, None
        try:
            re_ = self.risk_engine(symbol)
        except Exception as exc:
            meta_error = f"symbol metadata failed: {exc}"
        committed = self._committed_percent()
        out = {
            "symbol": symbol, "setup_id": setup_id, "account_identity": identity,
            "equity": equity, "currency": acct.get("currency"),
            "max_total_risk_percent": MAX_TOTAL_RISK_PERCENT,
            "committed_risk_percent": round(committed, 2),
            "available_risk_percent": round(MAX_TOTAL_RISK_PERCENT - committed, 2),
            "committed_portfolio_risk": round(committed, 2),
            "available_portfolio_risk": round(MAX_TOTAL_RISK_PERCENT - committed, 2),
            "requested_risk": None, "stop_distance": None, "computed_volume": None,
            "estimated_loss": None, "portfolio_gate": None, "validation": None,
            "status": "RISK_NOT_REQUESTED", "compute_error": meta_error,
            "new_setup": None, "broker_view": None,
        }
        if not setup_id:
            out["status"] = "RISK_UNAVAILABLE" if meta_error else "RISK_NOT_REQUESTED"
            return out
        lc = self.registry.get(setup_id)
        if lc is None:
            out["status"] = "RISK_UNAVAILABLE"
            out["compute_error"] = f"unknown setup {setup_id}"
            return out
        if norm_symbol(lc.setup.symbol) != norm_symbol(symbol):
            raise ValueError(f"setup {setup_id} belongs to {lc.setup.symbol}, not {symbol}")
        out["requested_risk"] = lc.setup.risk_percent
        out["stop_distance"] = (round(abs(lc.setup.entry - lc.setup.stop_loss), 8)
                                if lc.setup.entry is not None and lc.setup.stop_loss is not None
                                else None)
        if re_ is None:
            out["status"] = "RISK_UNAVAILABLE"
            out["compute_error"] = meta_error or (
                "no broker symbol metadata (MT5 source unavailable)")
            out["new_setup"] = {"setup_id": setup_id, "error": out["compute_error"]}
            return out
        try:
            re_.validate_setup(lc.setup)
            order = ExecutionPolicy(re_).build_order(lc.setup, equity or 1.0)
            loss = (equity or 0) * lc.setup.risk_percent / 100.0
            out["computed_volume"] = order.volume
            out["estimated_loss"] = round(loss, 2)
            out["validation"] = {"ok": True}
            out["new_setup"] = {
                "setup_id": setup_id, "risk_percent": lc.setup.risk_percent,
                "volume": order.volume, "estimated_loss": round(loss, 2),
                "reward_risk": round(lc.setup.reward_distance / lc.setup.risk_distance, 3)
                if lc.setup.risk_distance else None,
            }
            budget = allocate_portfolio_risk_budget(
                [lc.setup], committed, MAX_TOTAL_RISK_PERCENT)
            gate = "FIT" if budget else "EXCEEDS_BUDGET"
            out["portfolio_gate"] = gate
            out["portfolio_allocation"] = gate          # legacy key
            out["status"] = "RISK_OK" if budget else "RISK_PORTFOLIO_REJECTED"
            if not budget:
                out["validation"] = {"ok": False,
                                     "error": "portfolio risk budget exhausted"}
        except ValueError as exc:
            out["status"] = "RISK_REJECTED"
            out["validation"] = {"ok": False, "error": str(exc)}
            out["new_setup"] = {"setup_id": setup_id, "error": str(exc)}
        except Exception as exc:  # adapter/broker failures surface structured, never 500
            import logging
            logging.getLogger(__name__).exception("risk computation failed for %s", setup_id)
            out["status"] = "RISK_UNAVAILABLE"
            out["compute_error"] = f"{type(exc).__name__}: {exc}"
            out["validation"] = {"ok": False, "error": out["compute_error"]}
            out["new_setup"] = {"setup_id": setup_id, "error": out["compute_error"]}
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
        self._require_authorized()
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
            self._safe_history(self.event_history.mark_paper_execution, setup_id,
                               "REQUESTED (pending limit)")
            check = self.source.order_check(self._real_request(virtual))
            retcode = getattr(check, "retcode", None)
            if retcode not in (0, 10004):
                self._safe_history(self.event_history.mark_paper_execution, setup_id,
                                   f"ORDER_CHECK_FAILED retcode={retcode}", requested=False)
                raise ValueError(f"order_check retcode={retcode}")
            self._transition(setup_id, LCState.ORDER_PREFLIGHTED, "order_check ok")
            self._transition(setup_id, LCState.ORDER_SUBMITTING, "paper submit")
            sent = self.source.order_send(self._real_request(virtual))
            code = getattr(sent, "retcode", None)
            if code != 10009:
                self._safe_history(self.event_history.mark_paper_execution, setup_id,
                                   f"BROKER_REJECTED retcode={code}", requested=False)
                raise RuntimeError(f"broker rejected placement retcode={code} {getattr(sent,'comment','')}")
            ticket = sent.order
            self.paper_tickets[setup_id] = ticket
            self._transition(setup_id, LCState.ORDER_PLACED, f"ticket {ticket}")
            self._safe_history(self.event_history.mark_paper_execution, setup_id,
                               f"ORDER_PLACED ticket={ticket}", requested=False)
            try:
                run_write(lambda: self.store.upsert_setup(
                    lc.setup, LCState.ORDER_PLACED, pd.Timestamp.now(tz="UTC"),
                    ticket=ticket))
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

    # ---------- lifecycle evaluation ----------
    def _apply_lifecycle_evaluation(self, setup_id: str, m15: pd.DataFrame) -> None:
        """Evaluate one EXECUTION_READY setup against closed M15 bars and apply
        the appropriate lifecycle transition.

        Uses the last closed bar (len(m15)-1) as current_index so that an
        unfinished live bar never influences terminal-state decisions.

        Idempotent: skips setups already in a terminal state. Concurrent
        transitions that race ahead of this call are silently absorbed.
        """
        lc = self.registry.get(setup_id)
        if lc is None or lc.state is not LCState.EXECUTION_READY:
            return
        current_index = len(m15) - 1
        try:
            result = _eval_lifecycle(lc.setup, m15, current_index=current_index)
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "lifecycle eval failed for %s", setup_id)
            return
        # Map models.SetupState → lifecycle.SetupState and apply exactly once.
        if result.state is ReplayState.EXPIRED:
            try:
                self._transition(setup_id, LCState.ENTRY_NO_LONGER_VALID, result.reason)
            except ValueError:
                pass  # already transitioned by a concurrent path
        elif result.state is ReplayState.INVALIDATED:
            try:
                self._transition(setup_id, LCState.PROTECTED_LEVEL_BREACHED, result.reason)
            except ValueError:
                pass
        elif result.state is ReplayState.FILLED:
            try:
                self._transition(setup_id, LCState.FILLED, result.reason)
            except ValueError:
                pass
        elif result.state is ReplayState.TRIGGERED:
            # Engine verdict: the limit entry has already traded
            # ("position_open"). A consumed setup must NEVER remain
            # EXECUTION_READY — map it to the lifecycle state the machine
            # already permits (EXECUTION_READY -> FILLED).
            try:
                self._transition(setup_id, LCState.FILLED, f"entry_traded: {result.reason}")
            except ValueError:
                pass
        elif result.state is ReplayState.AMBIGUOUS:
            # Entry+stop (or stop+target) touched in one candle: conservative
            # admission failure, never left executable.
            try:
                self._transition(setup_id, LCState.ENTRY_NO_LONGER_VALID,
                                 f"ambiguous: {result.reason}")
            except ValueError:
                pass

    # ---------- opportunity layer / background watcher (UI-independent) ----------
    def eligible_symbols(self) -> list[str]:
        """Authoritative broker universe (never the UI-selected symbol list)."""
        try:
            return [s for s in (self.source.symbols() or []) if s]
        except Exception:
            return []

    def _last_closed_m15(self, symbol: str):
        try:
            df = self.source.bars(symbol, TIMEFRAMES["M15"], 1)
            return df["time"].iloc[-1] if len(df) else None
        except Exception:
            return None

    def _causal_view(self, symbol: str):
        d1 = self.bars(symbol, "D1", 150)
        h4 = self.bars(symbol, "H4", 400)
        m15 = self.bars(symbol, "M15", 500)
        return opp_evaluator.build_view(symbol, d1, h4, m15, None, MultiTimeframeConfig())

    def _dispatch_opportunity_events(self, res: dict) -> None:
        """Backend alert dispatch — runs regardless of any UI connection."""
        for ev in res.get("events", []):
            try:
                self.web.add_alert(ev.get("symbol", "?"), "causal", ev.get("kind", "OPPORTUNITY"),
                                   ev.get("message", ""), level="OPPORTUNITY")
                self.events.publish(ev.get("kind", "OPPORTUNITY_ADVANCED"), ev)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("opportunity alert dispatch failed")

    def scan_universe_once(self, force: bool = False) -> dict:
        """One authoritative background cycle.

        Enumerates the broker universe, applies the closed-M15 gate per symbol,
        runs the causal view, advances the opportunity engine, persists and
        dispatches alerts. Requires no browser, no selection, no HTTP request.
        Per-symbol isolation: one failure cannot starve the scan.
        """
        now = pd.Timestamp.now(tz="UTC").isoformat()
        stats = {"symbols_scanned": 0, "symbols_succeeded": 0, "symbols_failed": 0,
                 "symbols_skipped": 0, "opportunities_created": 0,
                 "opportunities_advanced": 0, "opportunities_invalidated": 0,
                 "opportunities_expired": 0, "converted_to_setup": 0,
                 "alerts_emitted": 0, "alerts_deduplicated": 0, "events": []}
        data_now = None
        for sym in self.eligible_symbols():
            try:
                last = self._last_closed_m15(sym)
                if not force and last is not None and self._opp_last_m15.get(sym) == last:
                    stats["symbols_skipped"] += 1
                    continue
                self._opp_last_m15[sym] = last
                if last is not None:
                    data_now = last if data_now is None else max(data_now, last)
                stats["symbols_scanned"] += 1
                view = self._causal_view(sym)
                res = self.opportunities.observe(sym, view)
                self._dispatch_opportunity_events(res)
                stats["opportunities_created"] += len(res["created"])
                stats["opportunities_advanced"] += len(res["advanced"])
                stats["opportunities_invalidated"] += len(res["invalidated"])
                stats["opportunities_expired"] += len(res["expired"])
                stats["converted_to_setup"] += len(res["converted"])
                stats["events"].extend(res["events"])
                d = self._symbol_diag.setdefault(sym, {})
                d.update({"last_analysis_time": now,
                          "last_data_time": str(last) if last is not None else None,
                          "last_error": None,
                          "current_opportunity_count":
                              len(self.opportunities.repo.query(symbol=sym, active_only=True, limit=100))})
                if res["events"]:
                    d["last_opportunity_time"] = now
                stats["symbols_succeeded"] += 1
            except Exception as exc:
                stats["symbols_failed"] += 1
                self._symbol_diag.setdefault(sym, {})["last_error"] = f"{type(exc).__name__}: {exc}"
                continue
        try:
            # TTL sweep is anchored to the latest CLOSED market data time, not
            # the wall clock: the closed-M15 cadence is authoritative.
            self.opportunities.expire_cycle(now=data_now)
        except Exception:
            pass
        diag = self.opportunities.diagnostics()
        stats["alerts_emitted"] = diag.get("alerts_emitted", 0)
        stats["alerts_deduplicated"] = diag.get("alerts_deduplicated", 0)
        stats["last_scan_time"] = now
        self._last_scan = stats
        self._last_scan_time = now
        return stats

    def opportunity_diagnostics(self) -> dict:
        eng = self.opportunities.diagnostics()
        out = dict(eng)
        out.update({
            "watcher_running": bool(self._poller and self._poller.is_alive()),
            "last_scan_time": self._last_scan_time,
            "last_scan": dict(self._last_scan),
            "symbols": dict(self._symbol_diag),
            "universe_size": len(self.eligible_symbols()),
            "db": db_write_diagnostics(),
        })
        return out

    def opportunity_audit(self, symbol: str) -> dict:
        return self.opportunities.audit(symbol)

    def active_opportunities(self, symbol: Optional[str] = None) -> list[dict]:
        rows = []
        for opp in self.opportunities.repo.query(symbol=symbol, active_only=True, limit=200):
            rows.append(self._opportunity_view(opp))
        return rows

    @staticmethod
    def _opportunity_view(opp) -> dict:
        from ..opportunity.models import STATE_LABEL
        return {
            "opportunity_id": opp.opportunity_id, "symbol": opp.symbol,
            "direction": opp.direction, "type": opp.opportunity_type,
            "state": opp.state, "label": STATE_LABEL.get(
                opp.state, opp.state),
            "created_at": str(opp.created_at), "updated_at": str(opp.updated_at),
            "expires_at": str(opp.expires_at) if opp.expires_at else None,
            "first_seen": str(opp.first_seen), "last_seen": str(opp.last_seen),
            "sweep": opp.sweep_evidence, "csd": opp.csd_evidence,
            "bos": opp.bos_evidence, "selected_poi": opp.selected_poi,
            "poi_candidates": opp.poi_candidates, "idm_reference": opp.idm_reference,
            "entry_pathway": opp.entry_pathway, "blocker": opp.blocker,
            "next_expected": opp.next_expected, "reason": opp.reason,
            "setup_id": opp.setup_id, "risk_status": opp.risk_status,
            "lifecycle_status": opp.lifecycle_status,
        }

    # ---------- poller ----------
    def start_poller(self, interval: float = 15.0) -> None:
        if self._poller and self._poller.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(interval):
                try:
                    self.scan_universe_once()
                except Exception:
                    continue

        self._poller = threading.Thread(target=loop, daemon=True)
        self._poller.start()

    def stop_poller(self) -> None:
        self._stop.set()
