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
            if engine._cfg.options.enabled:
                positions = [p for p in positions if getattr(p, "instrument_type", "EQUITY") != "EQUITY"]
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
            trades = engine.tracker.get_open_trades()
        else:
            trades = engine.tracker.get_closed_trades(limit)
        if engine._cfg.options.enabled:
            trades = [t for t in trades if t.get("instrument_type", "EQUITY") in ("CALL", "PUT")]
        return trades

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
            "scanner_enabled": engine._cfg.scanner.enabled,
            "scanner_tickers": getattr(engine, '_scanner_tickers', []),
            "total_tickers": len(engine._cfg.watchlist) + len(getattr(engine, '_scanner_tickers', [])),
            "vix": getattr(engine, '_vix', 20.0),
            "swarm_enabled": engine._cfg.swarm.enabled,
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

    @app.get("/api/scanner-tickers")
    async def get_scanner_tickers():
        return {
            "watchlist": engine._cfg.watchlist,
            "scanner": getattr(engine, '_scanner_tickers', []),
            "total": len(engine._cfg.watchlist) + len(getattr(engine, '_scanner_tickers', [])),
            "scanner_enabled": engine._cfg.scanner.enabled,
        }

    # ── Swarm Intelligence endpoints ──────────────────────────────────────

    @app.get("/api/swarm-debates")
    async def get_swarm_debates(limit: int = Query(default=10, ge=1, le=50)):
        memory = getattr(engine, '_memory', None)
        if memory:
            return memory.get_recent_debates(limit)
        return []

    @app.get("/api/agent-track-record")
    async def get_agent_track_record():
        memory = getattr(engine, '_memory', None)
        if not memory:
            return {}
        agents = ["momentum", "contrarian", "macro", "risk_manager", "quant"]
        return {name: memory.get_agent_track_record(name) for name in agents}

    @app.get("/api/market-narratives")
    async def get_market_narratives():
        memory = getattr(engine, '_memory', None)
        if memory:
            return memory.get_active_narratives()
        return []

    @app.get("/api/react-report/{ticker}")
    async def get_react_report(ticker: str):
        report = getattr(engine, '_react_reports', {}).get(ticker.upper())
        if report:
            return report.to_dict()
        return {"ticker": ticker.upper(), "error": "No report available"}

    @app.get("/api/broker-orders")
    async def get_broker_orders(limit: int = Query(default=50, ge=1, le=200)):
        try:
            orders = await engine.broker.get_order_history(limit)
            return orders
        except Exception as e:
            log.warning("Broker orders fetch failed", error=str(e))
            return []

    @app.get("/api/broker-deals")
    async def get_broker_deals(limit: int = Query(default=50, ge=1, le=200)):
        try:
            deals = await engine.broker.get_deal_history(limit)
            return deals
        except Exception as e:
            log.warning("Broker deals fetch failed", error=str(e))
            return []

    @app.post("/api/modify-trade")
    async def modify_trade(body: dict | None = None):
        """Modify stop_price or target_price on an open trade."""
        try:
            if not body:
                return {"error": "Missing request body"}
            trade_id = body.get("trade_id", "")
            if not trade_id:
                return {"error": "trade_id is required"}

            stop_price = body.get("stop_price")
            target_price = body.get("target_price")

            if stop_price is None and target_price is None:
                return {"error": "Must provide stop_price or target_price"}

            # Update in tracker
            trades = engine.tracker.get_open_trades()
            trade = next((t for t in trades if t["id"] == trade_id), None)
            if not trade:
                return {"error": f"Trade {trade_id} not found or already closed"}

            engine.tracker.update_trade_prices(trade_id, stop_price=stop_price, target_price=target_price)
            return {"success": True, "trade_id": trade_id}
        except Exception as e:
            log.warning("Modify trade failed", error=str(e))
            return {"error": str(e)}

    # ── Close position ────────────────────────────────────────────────────

    @app.post("/api/close-position")
    async def close_position(body: dict | None = None):
        """Close a position by ticker (and optional option_code)."""
        try:
            if not body:
                return {"error": "Missing request body"}

            ticker = body.get("ticker", "").upper()
            option_code = body.get("option_code", "")
            if not ticker:
                return {"error": "ticker is required"}

            # Find matching open trade in tracker
            trades = engine.tracker.get_open_trades()
            trade = None
            for t in trades:
                if t["ticker"] == ticker:
                    if option_code and t.get("option_code") == option_code:
                        trade = t
                        break
                    if not option_code and t.get("instrument_type", "EQUITY") == "EQUITY":
                        trade = t
                        break
            # Fallback: match any trade for this ticker
            if not trade:
                trade = next((t for t in trades if t["ticker"] == ticker), None)

            inst_type = trade.get("instrument_type", "EQUITY") if trade else "EQUITY"
            trade_id = trade["id"] if trade else None

            if inst_type in ("CALL", "PUT") and trade:
                from nexus.broker import OptionsContract, OrderSide, OrderType

                contract = OptionsContract(
                    ticker=ticker,
                    strike=trade.get("option_strike", 0),
                    expiration=trade.get("option_expiration", ""),
                    right=inst_type,
                    code=trade.get("option_code", ""),
                )
                quote = await engine.broker.get_quote(contract.code) if contract.code else None
                exit_price = quote.last if quote else trade["entry_price"]
                await engine.broker.place_options_order(
                    contract=contract,
                    side=OrderSide.SELL,
                    qty=int(trade["shares"]),
                    order_type=OrderType.MARKET,
                )
            else:
                from nexus.broker import OrderSide, OrderType

                # Close equity position directly at broker
                side = trade.get("side", "LONG") if trade else "LONG"
                # Get shares from broker position if no tracker trade
                pos_list = await engine.broker.get_positions()
                pos = next((p for p in pos_list if p.ticker == ticker), None)
                shares = pos.shares if pos else (trade["shares"] if trade else 0)
                if shares <= 0:
                    return {"error": f"No position found for {ticker}"}

                if side == "SHORT" or (pos and pos.side == "SHORT"):
                    await engine.broker.close_short(ticker, shares)
                else:
                    await engine.broker.place_order(
                        ticker=ticker,
                        side=OrderSide.SELL,
                        qty=shares,
                        order_type=OrderType.MARKET,
                    )
                quote = await engine.broker.get_quote(ticker)
                exit_price = quote.last if quote else (trade["entry_price"] if trade else 0)

            pnl = None
            if trade_id:
                pnl = engine.tracker.close_trade(trade_id, exit_price, "manual_close")

            return {"success": True, "ticker": ticker, "pnl": pnl, "exit_price": exit_price}
        except Exception as e:
            log.warning("Close position failed", ticker=body.get("ticker", "?"), error=str(e))
            return {"error": str(e)}

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
