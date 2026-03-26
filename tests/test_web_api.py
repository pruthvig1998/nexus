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


class _FakeScannerConfig:
    enabled = False
    max_tickers = 20
    scan_interval = 300


class _FakeSwarmConfig:
    enabled = False
    max_debate_calls = 3
    min_score_for_debate = 0.70
    consensus_threshold = 0.60
    timeout_seconds = 10.0
    swarm_model = "claude-sonnet-4-20250514"


class FakeConfig:
    """Minimal config for engine mock."""

    active_broker = "alpaca"
    paper = True
    watchlist = ["AAPL", "MSFT", "NVDA"]  # noqa: RUF012
    scan_interval = 60
    log_level = "WARNING"
    db_path = ":memory:"
    options = _FakeOptionsConfig()
    swarm = _FakeSwarmConfig()
    scanner = _FakeScannerConfig()


class FakeEngine:
    """Minimal engine mock for API tests."""

    def __init__(self) -> None:
        self._cfg = FakeConfig()
        self._tracker = PortfolioTracker(":memory:")
        self._bus = FakeEventBus()
        self._broker = AsyncMock()
        self._running = True
        self._scan_count = 42
        self._scanner_tickers = []
        self._vix = 20.0

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


def test_options_config(monkeypatch):
    monkeypatch.delenv("NEXUS_OPTIONS_ENABLED", raising=False)
    from nexus.config import NEXUSConfig

    cfg = NEXUSConfig()
    assert cfg.options.enabled is False
    assert cfg.options.target_dte == 0  # default: 0DTE
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


# ── DTE Engine Tests ────────────────────────────────────────────────────────


def test_dte_engine_momentum_scalp():
    from nexus.dte_engine import SCALP, select_dte_profile

    min_dte, max_dte = select_dte_profile("momentum", signal_score=0.75, vix=20.0)
    # Momentum at normal VIX → SCALP range
    assert min_dte == SCALP[0]
    assert max_dte == SCALP[1]


def test_dte_engine_fundamental_position():
    from nexus.dte_engine import select_dte_profile

    min_dte, max_dte = select_dte_profile("ai_fundamental", signal_score=0.75, vix=20.0)
    # AI fundamental at normal VIX → POSITION range
    assert min_dte >= 14
    assert max_dte <= 45


def test_dte_engine_high_vix_shifts_shorter():
    from nexus.dte_engine import select_dte_profile

    # Mean reversion at normal VIX
    normal = select_dte_profile("mean_reversion", signal_score=0.75, vix=20.0)
    # Same strategy at high VIX
    high_vix = select_dte_profile("mean_reversion", signal_score=0.75, vix=28.0)
    # High VIX should shift shorter
    assert high_vix[1] <= normal[1]


def test_dte_engine_low_vix_shifts_longer():
    from nexus.dte_engine import select_dte_profile

    # Momentum at normal VIX
    normal = select_dte_profile("momentum", signal_score=0.75, vix=20.0)
    # Same strategy at very low VIX
    low_vix = select_dte_profile("momentum", signal_score=0.75, vix=12.0)
    # Low VIX should shift longer (premium is cheap)
    assert low_vix[1] >= normal[1]


def test_dte_engine_high_conviction_shifts_longer():
    from nexus.dte_engine import select_dte_profile

    # Low conviction
    low = select_dte_profile("momentum", signal_score=0.65, vix=20.0)
    # High conviction
    high = select_dte_profile("momentum", signal_score=0.90, vix=20.0)
    # High conviction should shift longer
    assert high[1] >= low[1]


def test_dte_engine_recommend_target():
    from nexus.dte_engine import recommend_target_dte

    target = recommend_target_dte("momentum", signal_score=0.75, vix=20.0)
    assert 0 <= target <= 5  # momentum should be short DTE


def test_dte_engine_leaps_low_vix_fundamental():
    from nexus.dte_engine import select_dte_profile

    min_dte, max_dte = select_dte_profile("ai_fundamental", signal_score=0.90, vix=12.0)
    # High conviction + low VIX + fundamental → should reach LEAPS range
    assert max_dte >= 45  # at least multi-month


def test_dte_engine_extreme_vix():
    from nexus.dte_engine import select_dte_profile

    min_dte, max_dte = select_dte_profile("mean_reversion", signal_score=0.75, vix=35.0)
    # Extreme VIX: max_dte capped at 7
    assert max_dte <= 7


def test_select_expiration_with_overrides():
    from datetime import datetime, timedelta

    from nexus.config import OptionsConfig
    from nexus.strategy_options import select_expiration

    cfg = OptionsConfig(min_dte=0, max_dte=730)
    today = datetime.now().date()
    exp_short = (today + timedelta(days=1)).isoformat()
    exp_mid = (today + timedelta(days=30)).isoformat()
    exp_long = (today + timedelta(days=200)).isoformat()

    # Override to LEAPS range
    result = select_expiration(
        [exp_short, exp_mid, exp_long], cfg,
        target_dte_override=180,
        min_dte_override=150,
        max_dte_override=400,
    )
    assert result == exp_long[:10]


# ── IronGrid Grid Exit Tests ────────────────────────────────────────────────


def test_tracker_partial_close():
    from nexus.tracker import PortfolioTracker

    t = PortfolioTracker(":memory:")
    tid = t.open_trade(
        broker="moomoo",
        ticker="AAPL",
        side="LONG",
        shares=4,
        entry_price=3.50,
        stop_price=1.75,
        target_price=5.25,
        strategy="irongrid",
        signal_score=0.85,
        paper=True,
        instrument_type="CALL",
        option_strike=150.0,
        option_expiration="2025-04-17",
        option_code="US.AAPL250417C00150000",
    )

    # Partial close: sell 1 of 4 contracts at $4.50
    pnl = t.partial_close_trade(tid, 1, 4.50, "grid_L1_trim_25pct")
    assert pnl is not None
    # CALL P&L: (4.50 - 3.50) * 1 * 100 = 100
    assert pnl == 100.0

    # Check remaining shares
    trades = t.get_open_trades()
    assert len(trades) == 1
    assert trades[0]["shares"] == 3  # 4 - 1 = 3

    # Full close remaining
    final_pnl = t.close_trade(tid, 5.00, "profit_target")
    # Remaining P&L: (5.00 - 3.50) * 3 * 100 = 450, plus accumulated 100 = 550
    assert final_pnl is not None


def test_tracker_grid_level_update():
    from nexus.tracker import PortfolioTracker

    t = PortfolioTracker(":memory:")
    tid = t.open_trade(
        broker="moomoo",
        ticker="NVDA",
        side="LONG",
        shares=10,
        entry_price=2.00,
        stop_price=1.00,
        target_price=4.00,
        strategy="irongrid",
        signal_score=0.80,
        paper=True,
        instrument_type="CALL",
        option_strike=200.0,
        option_expiration="2025-05-16",
        option_code="US.NVDA250516C00200000",
    )

    # Update grid level
    t.update_grid_level(tid, 1, trailing_stop=2.00)
    trades = t.get_open_trades()
    assert trades[0]["grid_level"] == 1
    assert trades[0]["trailing_stop"] == 2.00


def test_tracker_original_shares_stored():
    from nexus.tracker import PortfolioTracker

    t = PortfolioTracker(":memory:")
    t.open_trade(
        broker="test",
        ticker="TSLA",
        side="LONG",
        shares=8,
        entry_price=5.00,
        stop_price=2.50,
        target_price=10.00,
        strategy="momentum",
        signal_score=0.85,
        paper=True,
        instrument_type="PUT",
    )
    trades = t.get_open_trades()
    assert trades[0]["original_shares"] == 8


def test_partial_close_clamps_to_available():
    from nexus.tracker import PortfolioTracker

    t = PortfolioTracker(":memory:")
    tid = t.open_trade(
        broker="test",
        ticker="MSFT",
        side="LONG",
        shares=2,
        entry_price=3.00,
        stop_price=1.50,
        target_price=6.00,
        strategy="irongrid",
        signal_score=0.80,
        paper=True,
        instrument_type="CALL",
    )
    # Try to close more than available — should clamp
    pnl = t.partial_close_trade(tid, 5, 4.00, "grid_trim")
    assert pnl is not None
    # Should close all 2 contracts, (4.00-3.00)*2*100 = 200
    assert pnl == 200.0
    # Trade should be fully closed
    trades = t.get_open_trades()
    assert len(trades) == 0


# ── Scanner Tests ───────────────────────────────────────────────────────────


def test_scanner_base_universe():
    from nexus.scanner import BASE_UNIVERSE

    assert len(BASE_UNIVERSE) > 50
    assert "AAPL" in BASE_UNIVERSE
    assert "SPY" in BASE_UNIVERSE
    assert "QQQ" in BASE_UNIVERSE


def test_scanner_instantiation():
    from nexus.scanner import UniverseScanner

    scanner = UniverseScanner(max_tickers=10)
    assert scanner._max_tickers == 10


# ── Config Tests ────────────────────────────────────────────────────────────


def test_options_config_leaps():
    from nexus.config import OptionsConfig

    cfg = OptionsConfig()
    assert cfg.max_dte == 730  # supports LEAPS
    assert cfg.auto_dte is True
    assert cfg.use_irongrid_exits is True


def test_scanner_config():
    from nexus.config import ScannerConfig

    cfg = ScannerConfig()
    assert cfg.enabled is False
    assert cfg.max_tickers == 20


def test_nexus_config_has_scanner():
    from nexus.config import NEXUSConfig

    cfg = NEXUSConfig()
    assert hasattr(cfg, "scanner")
    assert cfg.scanner.enabled is False
