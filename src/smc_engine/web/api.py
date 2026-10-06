from __future__ import annotations

import json
import os
import queue
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .hub import AUTHORIZED_LOGIN, AUTHORIZED_SERVER, DEMO_MARKERS, TIMEFRAMES, EngineHub, MT5Source
from .hub import _jsonable  # view-model serialization helper

STATIC_DIR = Path(__file__).parent / "static"


class HypothesisIn(BaseModel):
    symbol: str
    timeframe: str
    bias: str
    thesis: str = ""
    trigger: str = ""
    confirmation: str = ""
    invalidation: str = ""
    target: str = ""


def build_hub() -> EngineHub:
    login = os.environ.get("MT5_LOGIN")
    password = os.environ.get("MT5_PASSWORD")
    server = os.environ.get("MT5_SERVER")
    path = os.environ.get("MT5_TERMINAL_PATH")

    if login is None or server is None or password is None:
        raise SystemExit(
            "STARTUP ERROR: MT5_LOGIN, MT5_PASSWORD, and MT5_SERVER must all be set "
            "in the environment before starting the workstation. "
            "The server will not start without explicit credentials."
        )

    source = MT5Source(login=login, password=password, server=server, path=path)
    source.connect()

    if source.name == "mt5":
        _verify_authorized_account(source)

    db_dir = os.environ.get("SMC_GUI_DB_DIR", ".")
    hub = EngineHub(
        source,
        db_path=os.path.join(db_dir, "smc_engine_gui.sqlite3"),
        setup_store_path=os.path.join(db_dir, "smc_engine_state.sqlite3"),
    )
    hub.start_poller()
    return hub


def _verify_authorized_account(source: MT5Source) -> None:
    """Fail startup if the connected MT5 account is not the authorized demo account.

    Mirrors _account_identity() in hub.py but raises SystemExit so the server
    process terminates before accepting any requests.
    """
    acct = source.account() if source.connected else None
    if not acct:
        raise SystemExit(
            "STARTUP ERROR: MT5 connected but account() returned no data. "
            "Cannot verify account identity. Server will not start."
        )

    login = acct.get("login")
    server = str(acct.get("server", ""))
    mode = "DEMO" if source.is_demo else "LIVE"

    errors = []
    if login != AUTHORIZED_LOGIN:
        errors.append(f"login mismatch: connected={login}, required={AUTHORIZED_LOGIN}")
    if AUTHORIZED_SERVER not in server:
        errors.append(f"server mismatch: connected={server!r}, required={AUTHORIZED_SERVER!r}")
    if mode != "DEMO":
        errors.append(f"account_mode is {mode}, must be DEMO")

    if errors:
        raise SystemExit(
            "STARTUP ERROR: Connected MT5 account is NOT the authorized account.\n"
            + "\n".join(f"  - {e}" for e in errors)
            + "\nThe server will not start. Set MT5_LOGIN=477217728 / "
            "MT5_SERVER=Exness-MT5Trial9 and connect to the authorized DEMO terminal."
        )


def create_app(hub: Optional[EngineHub] = None) -> FastAPI:
    app = FastAPI(title="SMC Engine Workstation", version="1.0")
    app.state.hub = hub or build_hub()

    def H() -> EngineHub:
        return app.state.hub

    # ---------- static ----------
    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/static/{fname:path}")
    def static(fname: str):
        target = (STATIC_DIR / fname).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target)

    # ---------- runtime fingerprint (read-only) ----------
    @app.get("/api/runtime")
    def runtime():
        """Source identity, git commit, file hashes, process metadata.
        No broker calls. No credentials. Purely observational."""
        return _jsonable(H().runtime_info())

    @app.get("/api/causal-scan")
    def causal_scan_universe():
        """Read-only causal chain provenance scan across all broker symbols."""
        try:
            return _jsonable(H().causal_scan_universe())
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except Exception as exc:
            raise HTTPException(503, str(exc))

    @app.get("/api/causal-scan/{symbol}")
    def causal_scan_symbol(symbol: str):
        """Causal chain provenance trace for a single symbol."""
        try:
            return _jsonable(H().causal_scan_provenance(symbol))
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except Exception as exc:
            raise HTTPException(503, str(exc))

    @app.get("/api/market-diagnostic/{symbol}")
    def market_diagnostic(symbol: str, tf: str = "M15", count: int = 250):
        """Structured market-data availability diagnostic. Read-only."""
        return _jsonable(H().market_diagnostic(symbol, tf, count))

    # ---------- status / account / symbols ----------
    @app.get("/api/status")
    def status():
        return _jsonable(H().status())

    @app.get("/api/account")
    def account():
        s = H().status()
        return _jsonable({"account": s["account"], "mode": s["account_mode"]})

    @app.get("/api/symbols")
    def symbols():
        try:
            return {"symbols": H().symbols()}
        except Exception as exc:
            raise HTTPException(503, f"symbols unavailable: {exc}")

    # ---------- market / analysis ----------
    @app.get("/api/market/{symbol}")
    def market(symbol: str, tf: str = "M15", count: int = 250):
        if tf not in TIMEFRAMES:
            raise HTTPException(400, f"unknown timeframe {tf}")
        try:
            df = H().bars(symbol, tf, count)
        except Exception as exc:
            raise HTTPException(503, str(exc))
        return {"symbol": symbol, "timeframe": tf,
                "candles": [[str(r.time), float(r.open), float(r.high),
                             float(r.low), float(r.close)] for r in df.itertuples()]}

    @app.get("/api/analysis/{symbol}/{tf}")
    def analysis(symbol: str, tf: str, count: int = 250):
        if tf not in TIMEFRAMES:
            raise HTTPException(400, f"unknown timeframe {tf}")
        try:
            return _jsonable(H().analysis(symbol, tf, count))
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except Exception as exc:
            raise HTTPException(503, str(exc))

    # ---------- setups / lifecycle ----------
    @app.get("/api/setups")
    def setups(symbol: Optional[str] = None):
        return {"setups": _jsonable(H().lifecycle(symbol))}

    @app.get("/api/setups/{setup_id}")
    def setup_detail(setup_id: str):
        rows = [r for r in H().lifecycle() if r["setup_id"] == setup_id]
        if not rows:
            raise HTTPException(404, "unknown setup")
        return _jsonable(rows[0])

    @app.get("/api/lifecycle")
    def lifecycle(symbol: Optional[str] = None):
        return {"states": _jsonable(H().lifecycle(symbol))}

    # ---------- readiness (OBSERVATION-ONLY; never executes) ----------
    @app.get("/api/readiness")
    def readiness(symbol: Optional[str] = None):
        """Gate B readiness view derived from the engine lifecycle.

        Presentation state only — deliberately NOT a lifecycle state.
        A setup counts only when it is a genuine engine-causal id with
        complete fields. Incomplete or foreign-id rows are rejected here
        (and counted), never presented as executable. Detection paths call
        no broker primitives; /api/setups stays read-only.
        """
        return _jsonable(H().readiness(symbol))

    # ---------- causal setup event history (read-only audit surface) ----------
    @app.get("/api/setup-history")
    def setup_history(symbol: Optional[str] = None, timeframe: Optional[str] = None,
                      status: Optional[str] = None, setup_id: Optional[str] = None,
                      limit: int = 50, before_id: Optional[int] = None):
        """Strictly read-only audit query. No writes, no broker primitives."""
        if status and status not in ("DETECTED", "ACTIVE", "EXPIRED", "CLOSED"):
            raise HTTPException(400, "unknown history status")
        return _jsonable(H().event_history.query(symbol=symbol, timeframe=timeframe,
                                           status=status, setup_id=setup_id,
                                           limit=limit, before_id=before_id))

    @app.get("/api/setup-history/{setup_id}")
    def setup_history_detail(setup_id: str):
        res = H().event_history.query(setup_id=setup_id, limit=1)
        if not res["events"]:
            raise HTTPException(404, "no history event for setup")
        event = res["events"][0]
        event["timeline"] = H().event_history.timeline(setup_id)
        return _jsonable({"event": event})

    @app.post("/api/setup-history/{setup_id}/review")
    def setup_history_review(setup_id: str):
        """Record a manual review observation. No user-account system exists;
        the action itself (manual, local session) is the identity. Never
        executes anything."""
        ok = H().event_history.mark_reviewed(setup_id)
        if not ok:
            raise HTTPException(404, "no history event for setup")
        return _jsonable(H().event_history.query(setup_id=setup_id, limit=1)["events"][0])

    # ---------- risk ----------
    @app.get("/api/risk")
    def risk(symbol: str = "EURAUD", setup_id: Optional[str] = None):
        try:
            return _jsonable(H().risk_snapshot(symbol, setup_id))
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    # ---------- execution ----------
    @app.get("/api/execution/{setup_id}")
    def execution(setup_id: str):
        try:
            return _jsonable(H().execution_state(setup_id))
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    # ---------- alerts / history ----------
    @app.get("/api/alerts")
    def alerts(limit: int = 100):
        from .runtime import _SERVER_STARTED_AT
        import pandas as pd
        startup_iso: Optional[str] = (
            pd.Timestamp(_SERVER_STARTED_AT, unit="s", tz="UTC").isoformat()
            if _SERVER_STARTED_AT else None
        )
        return {"alerts": _jsonable(H().web.alerts(limit)), "server_startup_time": startup_iso}

    @app.get("/api/history")
    def history():
        return {"rows": _jsonable(H().history())}

    # ---------- hypotheses ----------
    @app.get("/api/hypotheses")
    def hypotheses():
        return {"hypotheses": _jsonable(H().web.hypotheses())}

    @app.post("/api/hypotheses")
    def create_hypothesis(h: HypothesisIn):
        out = H().add_hypothesis(h.model_dump())
        return _jsonable(out)

    # ---------- analysis run trigger ----------
    @app.post("/api/analysis/run")
    def run_analysis(body: dict):
        symbol = body.get("symbol")
        tf = body.get("timeframe", "M15")
        if not symbol:
            raise HTTPException(400, "symbol required")
        try:
            return _jsonable(H().analysis(symbol, tf))
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except Exception as exc:
            raise HTTPException(503, str(exc))

    # ---------- dry run ----------
    @app.post("/api/dry-run/{setup_id}")
    def dry_run(setup_id: str):
        try:
            return _jsonable(H().dry_run(setup_id))
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    # ---------- paper ----------
    @app.post("/api/paper/{setup_id}")
    def paper_place(setup_id: str):
        try:
            return _jsonable(H().paper_place(setup_id))
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise HTTPException(502, str(exc))

    @app.post("/api/paper/{setup_id}/cancel")
    def paper_cancel(setup_id: str):
        try:
            return _jsonable(H().paper_cancel(setup_id))
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except Exception as exc:
            raise HTTPException(502, str(exc))

    # ---------- live (DISABLED) ----------
    @app.post("/api/live/{setup_id}")
    def live_disabled(setup_id: str):
        raise HTTPException(403, "LIVE EXECUTION IS DISABLED AT THIS STAGE")

    # ---------- SSE ----------
    @app.get("/api/events")
    def events(request: Request):
        hub = H()
        q = hub.events.subscribe()

        def gen():
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        ev = q.get(timeout=15)
                        yield f"event: {ev['kind']}\ndata: {json.dumps(ev)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                hub.events.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


app = None


def main() -> None:
    import uvicorn
    global app
    app = create_app()
    host = os.environ.get("SMC_GUI_HOST", "127.0.0.1")
    port = int(os.environ.get("SMC_GUI_PORT", "8765"))
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
