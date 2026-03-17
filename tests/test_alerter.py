"""Unit tests for the Telegram alerter.

All Telegram API calls are mocked — no real HTTP requests are made.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.alerter import _EMOJI, TelegramAlerter
from nexus.config import TelegramConfig
from nexus.engine import EventType

# ── Fixtures ──────────────────────────────────────────────────────────────────


class FakeEventBus:
    """Minimal event bus for testing — mirrors _EventBus.subscribe()."""

    def __init__(self) -> None:
        self._handlers: Dict[EventType, List[Callable]] = defaultdict(list)

    def subscribe(self, event_type: EventType, handler: Callable) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event_type: EventType, data: Any = None) -> None:
        for handler in self._handlers.get(event_type, []):
            result = handler(event_type, data)
            if asyncio.iscoroutine(result):
                await result


@pytest.fixture
def telegram_config() -> TelegramConfig:
    return TelegramConfig(
        bot_token="test-token-123",
        chat_id="12345",
        enabled=True,
    )


@pytest.fixture
def disabled_config() -> TelegramConfig:
    return TelegramConfig(
        bot_token="test-token-123",
        chat_id="12345",
        enabled=False,
    )


@pytest.fixture
def bus() -> FakeEventBus:
    return FakeEventBus()


@pytest.fixture
def alerter(telegram_config: TelegramConfig, bus: FakeEventBus) -> TelegramAlerter:
    return TelegramAlerter(telegram_config, bus)


# ── Message formatting tests ─────────────────────────────────────────────────


class TestFormatOrderFilled:
    def test_format_with_object_data(self, alerter: TelegramAlerter) -> None:
        @dataclass
        class FakeOrder:
            ticker: str = "AAPL"
            filled_qty: float = 100
            avg_fill_price: float = 185.50
            side: str = "BUY"

        msg = alerter._format_event(EventType.ORDER_FILLED, FakeOrder())
        assert "Order Filled" in msg
        assert "AAPL" in msg
        assert "185.5" in msg
        assert "BUY" in msg
        assert _EMOJI["ORDER_FILLED"] in msg

    def test_format_with_dict_data(self, alerter: TelegramAlerter) -> None:
        data = {"ticker": "NVDA", "side": "SELL", "qty": 50, "avg_fill_price": 920.00}
        msg = alerter._format_event(EventType.ORDER_FILLED, data)
        assert "NVDA" in msg
        assert "SELL" in msg
        assert "920" in msg

    def test_format_with_string_fallback(self, alerter: TelegramAlerter) -> None:
        msg = alerter._format_event(EventType.ORDER_FILLED, "raw string")
        assert "raw string" in msg
        assert "Order Filled" in msg


class TestFormatPositionOpened:
    def test_format_with_dict(self, alerter: TelegramAlerter) -> None:
        data = {"ticker": "TSLA", "side": "LONG", "trade_id": "abcdef1234567890"}
        msg = alerter._format_event(EventType.POSITION_OPENED, data)
        assert "Position Opened" in msg
        assert "TSLA" in msg
        assert "LONG" in msg
        assert "abcdef12" in msg  # truncated to 8 chars
        assert _EMOJI["POSITION_OPENED"] in msg

    def test_format_with_non_dict_fallback(self, alerter: TelegramAlerter) -> None:
        msg = alerter._format_event(EventType.POSITION_OPENED, "some data")
        assert "Position Opened" in msg
        assert "some data" in msg


class TestFormatPositionClosed:
    def test_format_with_profit(self, alerter: TelegramAlerter) -> None:
        data = {"ticker": "AMD", "side": "LONG", "pnl": 523.45, "reason": "target_hit"}
        msg = alerter._format_event(EventType.POSITION_CLOSED, data)
        assert "Position Closed" in msg
        assert "AMD" in msg
        assert "+$523.45" in msg or "$+523.45" in msg or "+523.45" in msg
        assert "target_hit" in msg

    def test_format_with_loss(self, alerter: TelegramAlerter) -> None:
        data = {"ticker": "META", "side": "SHORT", "pnl": -150.00, "reason": "stop_hit"}
        msg = alerter._format_event(EventType.POSITION_CLOSED, data)
        assert "META" in msg
        assert "SHORT" in msg
        assert "-150" in msg
        assert "stop_hit" in msg

    def test_format_with_none_pnl(self, alerter: TelegramAlerter) -> None:
        data = {"ticker": "GOOGL", "side": "CLOSE", "pnl": None, "reason": "flip"}
        msg = alerter._format_event(EventType.POSITION_CLOSED, data)
        assert "pending" in msg


class TestFormatDailyHalt:
    def test_format(self, alerter: TelegramAlerter) -> None:
        msg = alerter._format_event(EventType.DAILY_HALT, None)
        assert "Daily Loss Halt" in msg
        assert _EMOJI["DAILY_HALT"] in msg


class TestFormatBrokerConnected:
    def test_format_string(self, alerter: TelegramAlerter) -> None:
        msg = alerter._format_event(EventType.BROKER_CONNECTED, "alpaca")
        assert "Broker Connected" in msg
        assert "alpaca" in msg
        assert _EMOJI["BROKER_CONNECTED"] in msg

    def test_format_non_string(self, alerter: TelegramAlerter) -> None:
        msg = alerter._format_event(EventType.BROKER_CONNECTED, 42)
        assert "42" in msg


class TestFormatUnhandled:
    def test_unsubscribed_event_returns_none(self, alerter: TelegramAlerter) -> None:
        # SCAN_COMPLETE is not handled by the alerter
        msg = alerter._format_event(EventType.SCAN_COMPLETE, {})
        assert msg is None


# ── Event subscription tests ─────────────────────────────────────────────────


class TestSubscriptions:
    async def test_subscribes_to_correct_events(
        self, telegram_config: TelegramConfig, bus: FakeEventBus
    ) -> None:
        alerter = TelegramAlerter(telegram_config, bus)
        # Patch _send_loop to return immediately so start() doesn't block
        alerter._send_loop = AsyncMock()
        with patch("aiohttp.ClientSession"):
            await alerter.start()

        expected = {
            EventType.ORDER_FILLED,
            EventType.POSITION_OPENED,
            EventType.POSITION_CLOSED,
            EventType.DAILY_HALT,
            EventType.BROKER_CONNECTED,
        }
        subscribed = set(bus._handlers.keys())
        assert expected == subscribed

    async def test_does_not_subscribe_to_signal_or_scan(
        self, telegram_config: TelegramConfig, bus: FakeEventBus
    ) -> None:
        alerter = TelegramAlerter(telegram_config, bus)
        alerter._send_loop = AsyncMock()
        with patch("aiohttp.ClientSession"):
            await alerter.start()

        assert EventType.SIGNAL_GENERATED not in bus._handlers
        assert EventType.SCAN_COMPLETE not in bus._handlers
        assert EventType.ORDER_SUBMITTED not in bus._handlers


# ── Event handler integration ────────────────────────────────────────────────


class TestOnEvent:
    async def test_event_queues_message(self, alerter: TelegramAlerter) -> None:
        data = {"ticker": "AAPL", "side": "LONG", "trade_id": "abc123"}
        await alerter._on_event(EventType.POSITION_OPENED, data)
        assert len(alerter._queue) == 1
        assert "AAPL" in alerter._queue[0]

    async def test_multiple_events_queue_in_order(self, alerter: TelegramAlerter) -> None:
        await alerter._on_event(EventType.DAILY_HALT, None)
        await alerter._on_event(EventType.BROKER_CONNECTED, "alpaca")
        assert len(alerter._queue) == 2
        assert "Halt" in alerter._queue[0]
        assert "alpaca" in alerter._queue[1]


# ── Rate limiting ────────────────────────────────────────────────────────────


class TestRateLimiting:
    async def test_messages_are_queued_not_dropped(self, alerter: TelegramAlerter) -> None:
        """Enqueue many messages — all should remain in the queue."""
        for i in range(10):
            alerter._enqueue(f"Message {i}")
        assert len(alerter._queue) == 10
        # Verify ordering preserved
        assert alerter._queue[0] == "Message 0"
        assert alerter._queue[9] == "Message 9"

    async def test_send_loop_processes_all_messages(
        self, telegram_config: TelegramConfig, bus: FakeEventBus
    ) -> None:
        """Verify the send loop drains the queue completely."""
        alerter = TelegramAlerter(telegram_config, bus)
        send_calls: list[str] = []

        async def mock_send(text: str) -> None:
            send_calls.append(text)

        alerter._send_telegram = mock_send  # type: ignore[assignment]

        # Enqueue 3 messages
        alerter._enqueue("msg1")
        alerter._enqueue("msg2")
        alerter._enqueue("msg3")
        alerter._running = True

        # Run the send loop briefly — it should process all messages
        async def stop_after_drain() -> None:
            while alerter._queue:
                await asyncio.sleep(0.05)
            alerter._running = False
            alerter._send_event.set()

        await asyncio.gather(
            alerter._send_loop(),
            stop_after_drain(),
        )

        assert send_calls == ["msg1", "msg2", "msg3"]
        assert len(alerter._queue) == 0


# ── send_daily_summary ───────────────────────────────────────────────────────


class TestDailySummary:
    async def test_basic_summary(self, alerter: TelegramAlerter) -> None:
        stats = {"pnl": 1234.56, "trades": 7, "win_rate": 0.714, "open_positions": 3}
        await alerter.send_daily_summary(stats)
        assert len(alerter._queue) == 1
        msg = alerter._queue[0]
        assert "Daily Summary" in msg
        assert "+$1,234.56" in msg
        assert "7" in msg
        assert "71%" in msg

    async def test_negative_pnl(self, alerter: TelegramAlerter) -> None:
        stats = {"pnl": -500.00, "trades": 3, "win_rate": 0.333, "open_positions": 0}
        await alerter.send_daily_summary(stats)
        msg = alerter._queue[0]
        assert "-$500.00" in msg

    async def test_with_ticker_breakdown(self, alerter: TelegramAlerter) -> None:
        stats = {
            "pnl": 200.00,
            "trades": 4,
            "win_rate": 0.75,
            "open_positions": 2,
            "tickers": [
                {"ticker": "AAPL", "pnl": 300.00},
                {"ticker": "TSLA", "pnl": -100.00},
            ],
        }
        await alerter.send_daily_summary(stats)
        msg = alerter._queue[0]
        assert "AAPL" in msg
        assert "TSLA" in msg
        assert "Breakdown" in msg


# ── send_error ───────────────────────────────────────────────────────────────


class TestSendError:
    async def test_error_message(self, alerter: TelegramAlerter) -> None:
        await alerter.send_error("Connection lost to Alpaca API")
        assert len(alerter._queue) == 1
        msg = alerter._queue[0]
        assert "Engine Error" in msg
        assert "Connection lost to Alpaca API" in msg
        assert _EMOJI["ERROR"] in msg

    async def test_error_includes_timestamp(self, alerter: TelegramAlerter) -> None:
        await alerter.send_error("crash")
        msg = alerter._queue[0]
        assert "UTC" in msg


# ── Telegram API call (mocked) ───────────────────────────────────────────────


class TestSendTelegram:
    async def test_sends_correct_payload(
        self, telegram_config: TelegramConfig, bus: FakeEventBus
    ) -> None:
        alerter = TelegramAlerter(telegram_config, bus)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.closed = False
        alerter._session = mock_session

        await alerter._send_telegram("Hello test")

        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert "bot" + telegram_config.bot_token in call_args[0][0]
        payload = call_args[1]["json"]
        assert payload["chat_id"] == "12345"
        assert payload["text"] == "Hello test"
        assert payload["parse_mode"] == "Markdown"

    async def test_handles_api_error_gracefully(
        self, telegram_config: TelegramConfig, bus: FakeEventBus
    ) -> None:
        alerter = TelegramAlerter(telegram_config, bus)

        mock_response = AsyncMock()
        mock_response.status = 429
        mock_response.text = AsyncMock(return_value="Too Many Requests")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.closed = False
        alerter._session = mock_session

        # Should not raise
        await alerter._send_telegram("test msg")

    async def test_handles_network_error_gracefully(
        self, telegram_config: TelegramConfig, bus: FakeEventBus
    ) -> None:
        alerter = TelegramAlerter(telegram_config, bus)

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=Exception("Network down"))
        mock_session.closed = False
        alerter._session = mock_session

        # Should not raise
        await alerter._send_telegram("test msg")

    async def test_no_send_when_session_closed(
        self, telegram_config: TelegramConfig, bus: FakeEventBus
    ) -> None:
        alerter = TelegramAlerter(telegram_config, bus)

        mock_session = MagicMock()
        mock_session.closed = True
        alerter._session = mock_session

        # Should silently return
        await alerter._send_telegram("test msg")
        mock_session.post.assert_not_called()


# ── Disabled alerter ─────────────────────────────────────────────────────────


class TestDisabledAlerter:
    async def test_start_returns_immediately_when_disabled(
        self, disabled_config: TelegramConfig, bus: FakeEventBus
    ) -> None:
        alerter = TelegramAlerter(disabled_config, bus)
        await alerter.start()  # should return immediately, not block
        assert not alerter._running
        assert len(bus._handlers) == 0

    async def test_start_returns_when_missing_token(self, bus: FakeEventBus) -> None:
        cfg = TelegramConfig(bot_token="", chat_id="12345", enabled=True)
        alerter = TelegramAlerter(cfg, bus)
        await alerter.start()
        assert not alerter._running


# ── Stop / cleanup ───────────────────────────────────────────────────────────


class TestStop:
    async def test_stop_drains_queue(
        self, telegram_config: TelegramConfig, bus: FakeEventBus
    ) -> None:
        alerter = TelegramAlerter(telegram_config, bus)
        alerter._running = True
        sent: list[str] = []

        async def mock_send(text: str) -> None:
            sent.append(text)

        alerter._send_telegram = mock_send  # type: ignore[assignment]
        alerter._enqueue("final msg")

        await alerter.stop()
        assert "final msg" in sent
        assert len(alerter._queue) == 0
        assert not alerter._running
