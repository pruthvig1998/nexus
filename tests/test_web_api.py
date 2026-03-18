"""Tests for the NEXUS web API and WebSocket."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List
from unittest.mock import AsyncMock

import pytest

from nexus.broker import AccountInfo, Position
from nexus.engine import EventType
from nexus.tracker import PortfolioTracker
from nexus.web.ws import WebSocketManager, _serialize

# ── Fixtures ──────────────────────────────────────────────────────────────────


class FakeEventBus:
    """Minimal event bus for testing."""

    def __init__(self) -> None:
        self._handlers: Dict[EventType, List[Callable]] = defaultdict(list)

    def subscribe(self, event_type: EventType, handler: Callable) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event_type: EventType, data: Any = None) -> None:
        for handler in self._handlers.get(event_type, []):
            result = handler(event_type, data)
            if asyncio.iscoroutine(result):
                await result


class _FakeOptionsConfig:
    enabled = False


class FakeConfig:
    """Minimal config for engine mock."""

    active_broker = "alpaca"
    paper = True
    watchlist = ["AAPL", "MSFT", "NVDA"]  # noqa: RUF012
    scan_interval = 60
    log_level = "WARNING"
    db_path = ":memory:"
    options = _FakeOptionsConfig()


class FakeEngine:
    """Minimal engine mock for API tests."""

    def __init__(self) -> None:
        self._cfg = FakeConfig()
        self._tracker = PortfolioTracker(":memory:")
        self._bus = FakeEventBus()
        self._broker = AsyncMock()
        self._running = True
        self._scan_count = 42

    @property
    def tracker(self):
        return self._tracker

    @property
    def event_bus(self):
        return self._bus

    @property
    def broker(self):
        return self._broker


@pytest.fixture
def engine():
    return FakeEngine()


@pytest.fixture
def app(engine):
    from nexus.web.api import create_app

    return create_app(engine)


@pytest.fixture
async def client(app):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── Serialization Tests ──────────────────────────────────────────────────────


def test_serialize_none():
    assert _serialize(None) is None


def test_serialize_primitives():
    assert _serialize(42) == 42
    assert _serialize("hello") == "hello"
    assert _serialize(3.14) == 3.14
    assert _serialize(True) is True


def test_serialize_dict():
    assert _serialize({"a": 1, "b": "x"}) == {"a": 1, "b": "x"}


def test_serialize_list():
    assert _serialize([1, 2, 3]) == [1, 2, 3]


def test_serialize_dataclass():
    info = AccountInfo(
        broker="alpaca",
        cash=50000.0,
        portfolio_value=100000.0,
        buying_power=150000.0,
        day_pnl=250.0,
        total_pnl=1500.0,
        paper=True,
    )
    result = _serialize(info)
    assert isinstance(result, dict)
    assert result["broker"] == "alpaca"
    assert result["cash"] == 50000.0
    assert result["paper"] is True


def test_serialize_datetime():
    dt = datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
    assert _serialize(dt) == "2025-01-15T10:30:00+00:00"


def test_serialize_nested():
    data = {"positions": [{"ticker": "AAPL", "pnl": 100.0}], "count": 1}
    result = _serialize(data)
    assert result["positions"][0]["ticker"] == "AAPL"


# ── REST Endpoint Tests ──────────────────────────────────────────────────────


async def test_index_returns_html(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_get_account_success(client, engine):
    engine.broker.get_account_info.return_value = AccountInfo(
        broker="alpaca",
        cash=50000.0,
        portfolio_value=100000.0,
        buying_power=150000.0,
        day_pnl=250.0,
        total_pnl=1500.0,
        paper=True,
    )
    resp = await client.get("/api/account")
    assert resp.status_code == 200
    data = resp.json()
    assert data["broker"] == "alpaca"
    assert data["portfolio_value"] == 100000.0
    assert data["broker_connected"] is True


async def test_get_account_broker_disconnected(client, engine):
    engine.broker.get_account_info.side_effect = Exception("Connection refused")
    resp = await client.get("/api/account")
    assert resp.status_code == 200
    data = resp.json()
    assert data["broker_connected"] is False
    assert data["portfolio_value"] == 0


async def test_get_positions_success(client, engine):
    engine.broker.get_positions.return_value = [
        Position(
            ticker="AAPL",
            shares=100,
            avg_cost=150.0,
            current_price=155.0,
            broker="alpaca",
            side="LONG",
        )
    ]
    resp = await client.get("/api/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["ticker"] == "AAPL"
    assert data[0]["market_value"] == 15500.0
    assert data[0]["unrealized_pnl"] == 500.0


async def test_get_positions_broker_error(client, engine):
    engine.broker.get_positions.side_effect = Exception("Timeout")
    resp = await client.get("/api/positions")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_signals(client, engine):
    engine.tracker.log_signal("AAPL", "momentum", 0.85, "BUY", "Strong RSI breakout")
    engine.tracker.log_signal("MSFT", "mean_reversion", 0.72, "SELL", "Overbought")
    resp = await client.get("/api/signals?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


async def test_get_trades_open(client, engine):
    engine.tracker.open_trade(
        broker="alpaca",
        ticker="NVDA",
        side="LONG",
        shares=50,
        entry_price=200.0,
        stop_price=190.0,
        target_price=220.0,
        strategy="momentum",
        signal_score=0.80,
        paper=True,
    )
    resp = await client.get("/api/trades?status=open")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["ticker"] == "NVDA"


async def test_get_trades_closed(client, engine):
    tid = engine.tracker.open_trade(
        broker="alpaca",
        ticker="GOOGL",
        side="SHORT",
        shares=30,
        entry_price=150.0,
        stop_price=160.0,
        target_price=130.0,
        strategy="mean_reversion",
        signal_score=0.75,
        paper=True,
    )
    engine.tracker.close_trade(tid, 135.0, "target")
    resp = await client.get("/api/trades?status=closed&limit=50")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["pnl"] == 450.0  # SHORT: (150-135)*30


async def test_get_stats(client, engine):
    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "win_rate" in data
    assert "profit_factor" in data
    assert "total_trades" in data


async def test_get_pnl_history(client, engine):
    resp = await client.get("/api/pnl-history?days=7")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_get_status(client, engine):
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is True
    assert data["scan_count"] == 42
    assert data["paper"] is True
    assert data["broker"] == "alpaca"
    assert "AAPL" in data["watchlist"]
    assert data["options_enabled"] is False


async def test_get_option_chain_expirations(client, engine):
    engine.broker.get_option_expirations = AsyncMock(return_value=["2025-04-17", "2025-05-16"])
    resp = await client.get("/api/option-chain/AAPL")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ticker"] == "AAPL"
    assert len(data["expirations"]) == 2


# ── Options Data Model Tests ─────────────────────────────────────────────────


def test_options_contract():
    from nexus.broker import OptionsContract

    c = OptionsContract(ticker="AAPL", strike=150.0, expiration="2025-04-17", right="CALL")
    assert c.ticker == "AAPL"
    assert c.right == "CALL"


def test_position_option_pnl():
    from nexus.broker import Position

    pos = Position(
        ticker="AAPL",
        shares=5,
        avg_cost=3.50,
        current_price=5.00,
        broker="moomoo",
        side="LONG",
        instrument_type="CALL",
        strike=150.0,
        expiration="2025-04-17",
    )
    assert pos.is_option is True
    assert pos.market_value == 5 * 5.00 * 100  # 2500
    assert pos.unrealized_pnl == (5.00 - 3.50) * 5 * 100  # 750


def test_position_equity_unchanged():
    from nexus.broker import Position

    pos = Position(
        ticker="AAPL", shares=100, avg_cost=150.0, current_price=155.0, broker="alpaca"
    )
    assert pos.is_option is False
    assert pos.market_value == 15500.0
    assert pos.unrealized_pnl == 500.0


def test_options_config():
    from nexus.config import NEXUSConfig

    cfg = NEXUSConfig()
    assert cfg.options.enabled is False
    assert cfg.options.target_dte == 30
    assert cfg.options.min_signal_score == 0.70


def test_signal_options_fields():
    from nexus.strategy import Signal

    sig = Signal(
        ticker="AAPL",
        direction="BUY",
        score=0.85,
        strategy="options_momentum",
        reasoning="Strong RSI",
        instrument_type="CALL",
        option_strike=150.0,
        option_expiration="2025-04-17",
        option_code="US.AAPL250417C00150000",
        contracts=3,
    )
    assert sig.instrument_type == "CALL"
    assert sig.contracts == 3
    assert sig.option_code == "US.AAPL250417C00150000"


def test_tracker_options_trade():
    from nexus.tracker import PortfolioTracker

    t = PortfolioTracker(":memory:")
    tid = t.open_trade(
        broker="moomoo",
        ticker="AAPL",
        side="LONG",
        shares=5,
        entry_price=3.50,
        stop_price=1.75,
        target_price=5.25,
        strategy="options_momentum",
        signal_score=0.85,
        paper=True,
        instrument_type="CALL",
        option_strike=150.0,
        option_expiration="2025-04-17",
        option_code="US.AAPL250417C00150000",
    )
    trades = t.get_open_trades()
    assert len(trades) == 1
    assert trades[0]["instrument_type"] == "CALL"
    assert trades[0]["option_strike"] == 150.0

    pnl = t.close_trade(tid, 5.25, "profit_target")
    # CALL P&L: (5.25 - 3.50) * 5 * 100 = 875
    assert pnl == 875.0


def test_select_expiration():
    from datetime import datetime, timedelta

    from nexus.config import OptionsConfig
    from nexus.strategy_options import select_expiration

    cfg = OptionsConfig(min_dte=21, max_dte=45, target_dte=30)
    today = datetime.now().date()
    # Generate future expirations relative to today
    exp1 = (today + timedelta(days=10)).isoformat()  # too soon
    exp2 = (today + timedelta(days=28)).isoformat()  # in range, close to target
    exp3 = (today + timedelta(days=60)).isoformat()  # too far
    exp4 = (today + timedelta(days=35)).isoformat()  # in range
    result = select_expiration([exp1, exp2, exp3, exp4], cfg)
    assert result is not None
    assert result == exp2  # closest to target_dte=30


# ── WebSocket Tests ──────────────────────────────────────────────────────────


async def test_websocket_manager_connect_disconnect():
    mgr = WebSocketManager()
    ws = AsyncMock()
    await mgr.connect(ws)
    assert mgr.client_count == 1
    mgr.disconnect(ws)
    assert mgr.client_count == 0


async def test_websocket_manager_broadcast():
    mgr = WebSocketManager()
    ws = AsyncMock()
    await mgr.connect(ws)
    await mgr.broadcast(EventType.SCAN_COMPLETE, {"scan_count": 5})
    ws.send_text.assert_called_once()
    import json

    msg = json.loads(ws.send_text.call_args[0][0])
    assert msg["event"] == "SCAN_COMPLETE"
    assert msg["data"]["scan_count"] == 5


async def test_websocket_manager_removes_dead_clients():
    mgr = WebSocketManager()
    ws_good = AsyncMock()
    ws_dead = AsyncMock()
    ws_dead.send_text.side_effect = Exception("Connection closed")
    await mgr.connect(ws_good)
    await mgr.connect(ws_dead)
    assert mgr.client_count == 2
    await mgr.broadcast(EventType.SCAN_COMPLETE, None)
    assert mgr.client_count == 1


async def test_websocket_manager_no_clients():
    mgr = WebSocketManager()
    # Should not raise
    await mgr.broadcast(EventType.SCAN_COMPLETE, None)


# ── Import Tests ─────────────────────────────────────────────────────────────


def test_import_web_server():
    from nexus.web import WebServer

    assert WebServer is not None


def test_import_create_app():
    from nexus.web.api import create_app

    assert create_app is not None
