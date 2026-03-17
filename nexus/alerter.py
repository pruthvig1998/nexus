"""Telegram alerter — real-time trade notifications via Telegram Bot API.

Subscribes to the NEXUS EventBus and sends formatted messages for
order fills, position opens/closes, daily halts, and broker connections.
Rate-limited to 1 message/second per Telegram API guidelines.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from nexus.config import TelegramConfig
from nexus.logger import get_logger

log = get_logger("alerter")

# Emoji mapping for each event type
_EMOJI = {
    "ORDER_FILLED": "\u2705",  # green check
    "POSITION_OPENED": "\U0001f4c8",  # chart increasing
    "POSITION_CLOSED": "\U0001f4c9",  # chart decreasing
    "DAILY_HALT": "\U0001f6d1",  # stop sign
    "BROKER_CONNECTED": "\U0001f517",  # link
    "ERROR": "\U0001f6a8",  # rotating light
    "DAILY_SUMMARY": "\U0001f4ca",  # bar chart
}


class TelegramAlerter:
    """Sends trade alerts to a Telegram chat via the Bot API.

    Usage:
        alerter = TelegramAlerter(cfg.telegram, engine.event_bus)
        await alerter.start()   # begins processing queued messages
        await alerter.stop()    # graceful shutdown
    """

    def __init__(self, config: TelegramConfig, event_bus: Any) -> None:
        self._config = config
        self._bus = event_bus
        self._queue: deque[str] = deque()
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._send_event = asyncio.Event()

    # ── Public interface ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Subscribe to events and begin the send loop."""
        if not self._config.enabled:
            log.info("Telegram alerter disabled")
            return

        if not self._config.bot_token or not self._config.chat_id:
            log.warning("Telegram alerter enabled but missing bot_token or chat_id")
            return

        self._session = aiohttp.ClientSession()
        self._running = True

        # Import here to avoid circular imports at module level
        from nexus.engine import EventType

        self._bus.subscribe(EventType.ORDER_FILLED, self._on_event)
        self._bus.subscribe(EventType.POSITION_OPENED, self._on_event)
        self._bus.subscribe(EventType.POSITION_CLOSED, self._on_event)
        self._bus.subscribe(EventType.DAILY_HALT, self._on_event)
        self._bus.subscribe(EventType.BROKER_CONNECTED, self._on_event)

        log.info("Telegram alerter started", chat_id=self._config.chat_id)
        await self._send_loop()

    async def stop(self) -> None:
        """Flush remaining messages and shut down."""
        self._running = False
        self._send_event.set()  # wake up the loop so it can exit
        # Drain remaining messages
        while self._queue:
            await self._send_telegram(self._queue.popleft())
        if self._session and not self._session.closed:
            await self._session.close()
        log.info("Telegram alerter stopped")

    async def send_daily_summary(self, stats: dict) -> None:
        """Send a daily P&L summary message."""
        emoji = _EMOJI["DAILY_SUMMARY"]
        pnl = stats.get("pnl", 0.0)
        trades = stats.get("trades", 0)
        win_rate = stats.get("win_rate", 0.0)
        open_positions = stats.get("open_positions", 0)

        if pnl >= 0:
            pnl_str = f"+${pnl:,.2f}"
        else:
            pnl_str = f"-${abs(pnl):,.2f}"
        lines = [
            f"{emoji} *Daily Summary*",
            "",
            f"P&L: `{pnl_str}`",
            f"Trades: `{trades}`",
            f"Win Rate: `{win_rate:.0%}`",
            f"Open Positions: `{open_positions}`",
        ]

        # Include per-ticker breakdown if provided
        if "tickers" in stats:
            lines.append("")
            lines.append("_Breakdown:_")
            for ticker_info in stats["tickers"]:
                t_pnl = ticker_info.get("pnl", 0.0)
                t_sign = "+" if t_pnl >= 0 else ""
                lines.append(f"  {ticker_info['ticker']}: `{t_sign}${t_pnl:,.2f}`")

        self._enqueue("\n".join(lines))

    async def send_error(self, error: str) -> None:
        """Send an error/crash alert."""
        emoji = _EMOJI["ERROR"]
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        msg = f"{emoji} *Engine Error*\n\n`{error}`\n\n_{ts}_"
        self._enqueue(msg)

    # ── Event handler ─────────────────────────────────────────────────────────

    async def _on_event(self, event_type: Any, data: Any) -> None:
        """Handle an event from the bus and queue a formatted message."""
        msg = self._format_event(event_type, data)
        if msg:
            self._enqueue(msg)

    # ── Message formatting ────────────────────────────────────────────────────

    def _format_event(self, event_type: Any, data: Any) -> Optional[str]:
        """Format an event into a Telegram-friendly message string."""
        name = event_type.name
        emoji = _EMOJI.get(name, "\U0001f514")  # bell fallback

        if name == "ORDER_FILLED":
            return self._format_order_filled(emoji, data)
        elif name == "POSITION_OPENED":
            return self._format_position_opened(emoji, data)
        elif name == "POSITION_CLOSED":
            return self._format_position_closed(emoji, data)
        elif name == "DAILY_HALT":
            return f"{emoji} *Daily Loss Halt Triggered*\n\nTrading paused for the day."
        elif name == "BROKER_CONNECTED":
            broker_name = data if isinstance(data, str) else str(data)
            return f"{emoji} *Broker Connected*\n\n`{broker_name}` is online."
        return None

    def _format_order_filled(self, emoji: str, data: Any) -> str:
        """Format an ORDER_FILLED event."""
        if hasattr(data, "ticker"):
            ticker = data.ticker
            qty = getattr(data, "filled_qty", getattr(data, "qty", "?"))
            price = getattr(data, "avg_fill_price", "?")
            side = getattr(data, "side", "?")
            return (
                f"{emoji} *Order Filled*\n\n"
                f"Ticker: `{ticker}`\n"
                f"Side: `{side}`\n"
                f"Qty: `{qty}`\n"
                f"Price: `${price}`"
            )
        # Fallback for dict-like data
        if isinstance(data, dict):
            return (
                f"{emoji} *Order Filled*\n\n"
                f"Ticker: `{data.get('ticker', '?')}`\n"
                f"Side: `{data.get('side', '?')}`\n"
                f"Qty: `{data.get('qty', '?')}`\n"
                f"Price: `${data.get('avg_fill_price', '?')}`"
            )
        return f"{emoji} *Order Filled*\n\n`{data}`"

    def _format_position_opened(self, emoji: str, data: Any) -> str:
        """Format a POSITION_OPENED event."""
        if isinstance(data, dict):
            ticker = data.get("ticker", "?")
            side = data.get("side", "?")
            trade_id = data.get("trade_id", "")[:8]
            return (
                f"{emoji} *Position Opened*\n\n"
                f"Ticker: `{ticker}`\n"
                f"Side: `{side}`\n"
                f"Trade: `{trade_id}`"
            )
        return f"{emoji} *Position Opened*\n\n`{data}`"

    def _format_position_closed(self, emoji: str, data: Any) -> str:
        """Format a POSITION_CLOSED event."""
        if isinstance(data, dict):
            ticker = data.get("ticker", "?")
            side = data.get("side", "?")
            pnl = data.get("pnl")
            reason = data.get("reason", "?")
            pnl_str = f"${pnl:+,.2f}" if pnl is not None else "pending"
            return (
                f"{emoji} *Position Closed*\n\n"
                f"Ticker: `{ticker}`\n"
                f"Side: `{side}`\n"
                f"P&L: `{pnl_str}`\n"
                f"Reason: `{reason}`"
            )
        return f"{emoji} *Position Closed*\n\n`{data}`"

    # ── Send loop (rate-limited) ──────────────────────────────────────────────

    def _enqueue(self, message: str) -> None:
        """Add a message to the send queue and wake the send loop."""
        self._queue.append(message)
        self._send_event.set()

    async def _send_loop(self) -> None:
        """Process queued messages at max 1 per second."""
        while self._running:
            await self._send_event.wait()
            self._send_event.clear()

            while self._queue and self._running:
                msg = self._queue.popleft()
                await self._send_telegram(msg)
                # Rate limit: 1 message/second
                if self._queue:
                    await asyncio.sleep(1.0)

    async def _send_telegram(self, text: str) -> None:
        """Send a message via the Telegram Bot API."""
        if not self._session or self._session.closed:
            return

        url = f"https://api.telegram.org/bot{self._config.bot_token}/sendMessage"
        payload = {
            "chat_id": self._config.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            async with self._session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("Telegram send failed", status=resp.status, body=body[:200])
                else:
                    log.debug("Telegram message sent")
        except Exception as e:
            log.error("Telegram send error", error=str(e))
