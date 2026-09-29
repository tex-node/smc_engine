from __future__ import annotations

import json
import os
import queue
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .hub import LIVE_EXECUTION_ENABLED, TIMEFRAMES, MT5Source, EngineHub
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
    source = MT5Source(login=login, password=password, server=server, path=path)
    source.connect()
    db_dir = os.environ.get("SMC_GUI_DB_DIR", ".")
    hub = EngineHub(
        source,
        db_path=os.path.join(db_dir, "smc_engine_gui.sqlite3"),
        setup_store_path=os.path.join(db_dir, "smc_engine_state.sqlite3"),
    )
    hub.start_poller()
    return hub


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
        except Exception as exc:
            raise HTTPException(503, str(exc))

    # ---------- setups / lifecycle ----------
    @app.get("/api/setups")
    def setups():
        rows = H().lifecycle()
        return {"setups": _jsonable(rows)}

    @app.get("/api/setups/{setup_id}")
    def setup_detail(setup_id: str):
        H()
        rows = [r for r in H().lifecycle() if r["setup_id"] == setup_id]
        if not rows:
            raise HTTPException(404, "unknown setup")
        return _jsonable(rows[0])

    @app.get("/api/lifecycle")
    def lifecycle():
        return {"states": _jsonable(H().lifecycle())}

    # ---------- risk ----------
    @app.get("/api/risk")
    def risk(symbol: str = "EURAUD", setup_id: Optional[str] = None):
        return _jsonable(H().risk_snapshot(symbol, setup_id))

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
        return {"alerts": _jsonable(H().web.alerts(limit))}

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
                    if hub and request is not None and asyncio_disconnected(request):
                        break
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


def asyncio_disconnected(request: Request) -> bool:
    try:
        return request.client is None and False
    except Exception:
        return False


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
