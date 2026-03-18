"""FastAPI application — REST endpoints and WebSocket for NEXUS Web UI."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from nexus.engine import EventType
from nexus.logger import get_logger
from nexus.web.ws import WebSocketManager

if TYPE_CHECKING:
    from nexus.engine import NEXUSEngine

log = get_logger("web.api")

STATIC_DIR = Path(__file__).parent / "static"


def create_app(engine: NEXUSEngine) -> FastAPI:
    """Create and configure the FastAPI application."""
    ws_manager = WebSocketManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Subscribe WebSocketManager to all EventType values
        for event_type in EventType:
            engine.event_bus.subscribe(event_type, ws_manager.broadcast)
        log.info("WebSocket bridge subscribed to all events")
        yield

    app = FastAPI(title="NEXUS Trading Dashboard", version="3.0.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.ws_manager = ws_manager

    # ── Static files ─────────────────────────────────────────────────────────

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    # ── REST endpoints ───────────────────────────────────────────────────────

    @app.get("/api/account")
    async def get_account():
        try:
            info = await engine.broker.get_account_info()
            return _account_dict(info)
        except Exception as e:
            log.warning("Broker account fetch failed", error=str(e))
            return {
                "broker": engine._cfg.active_broker,
                "cash": 0,
                "portfolio_value": 0,
                "buying_power": 0,
                "day_pnl": 0,
                "total_pnl": 0,
                "paper": engine._cfg.paper,
                "broker_connected": False,
            }

    @app.get("/api/positions")
    async def get_positions():
        try:
            positions = await engine.broker.get_positions()
            return [_position_dict(p) for p in positions]
        except Exception as e:
            log.warning("Broker positions fetch failed", error=str(e))
            return []

    @app.get("/api/signals")
    async def get_signals(limit: int = Query(default=20, ge=1, le=200)):
        return engine.tracker.get_recent_signals(limit)

    @app.get("/api/trades")
    async def get_trades(
        status: str = Query(default="open", pattern="^(open|closed)$"),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        if status == "open":
            return engine.tracker.get_open_trades()
        return engine.tracker.get_closed_trades(limit)

    @app.get("/api/stats")
    async def get_stats():
        return engine.tracker.compute_stats()

    @app.get("/api/pnl-history")
    async def get_pnl_history(days: int = Query(default=30, ge=1, le=365)):
        return engine.tracker.get_pnl_history(days)

    @app.get("/api/status")
    async def get_status():
        return {
            "running": engine._running,
            "scan_count": engine._scan_count,
            "paper": engine._cfg.paper,
            "broker": engine._cfg.active_broker,
            "watchlist": engine._cfg.watchlist,
            "ws_clients": ws_manager.client_count,
            "options_enabled": engine._cfg.options.enabled,
        }

    @app.get("/api/option-chain/{ticker}")
    async def get_option_chain(ticker: str, expiration: str = Query(default="")):
        """Get option chain for a ticker. If no expiration, return expirations list."""
        try:
            if not expiration:
                expirations = await engine.broker.get_option_expirations(ticker.upper())
                return {"ticker": ticker.upper(), "expirations": expirations}
            chain = await engine.broker.get_option_chain(ticker.upper(), expiration)
            from nexus.web.ws import _serialize

            return {
                "ticker": ticker.upper(),
                "expiration": expiration,
                "chain": [_serialize(q) for q in chain],
            }
        except Exception as e:
            log.warning("Option chain fetch failed", ticker=ticker, error=str(e))
            return {"ticker": ticker.upper(), "error": str(e)}

    # ── WebSocket ────────────────────────────────────────────────────────────

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            while True:
                # Keep connection alive — client can send pings
                await websocket.receive_text()
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)

    return app


# ── Helpers ──────────────────────────────────────────────────────────────────


def _account_dict(info) -> dict:
    """Convert AccountInfo dataclass to dict with broker_connected flag."""
    import dataclasses

    d = dataclasses.asdict(info)
    d["broker_connected"] = True
    return d


def _position_dict(pos) -> dict:
    """Convert Position dataclass to dict with computed fields."""
    import dataclasses

    d = dataclasses.asdict(pos)
    d["market_value"] = pos.market_value
    d["unrealized_pnl"] = pos.unrealized_pnl
    d["unrealized_pnl_pct"] = pos.unrealized_pnl_pct
    return d
