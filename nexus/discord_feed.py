"""NEXUS Discord Feed — monitors Discord channels for trading signals.

Runs as an asyncio task alongside the engine. Parses messages for ticker
mentions + direction keywords and injects Signal objects into the engine's
queue.

Setup:
  1. discord.com/developers/applications → New App → Bot → copy Token
  2. Bot → Privileged Gateway Intents → enable Message Content Intent
  3. OAuth2 → URL Generator → bot + Read Messages → invite to servers
  4. Set DISCORD_BOT_TOKEN and DISCORD_CHANNEL_IDS in .env
"""
from __future__ import annotations

import asyncio
import re
from typing import List, Optional

from nexus.config import DiscordConfig
from nexus.logger import get_logger
from nexus.strategy import Signal

log = get_logger("discord_feed")

# ── Constants ─────────────────────────────────────────────────────────────────

COMMON_WORDS: set[str] = {
    "I", "A", "AT", "BE", "DO", "GO", "IT", "MY", "OR", "SO", "TO",
    "US", "AN", "AS", "IF", "IS", "IN", "ON", "UP", "BY",
}

_TICKER_EXPLICIT = re.compile(r"\$([A-Z]{1,5})\b")
_TICKER_BARE = re.compile(r"\b([A-Z]{2,5})\b")
_PRICE_NEAR = re.compile(r"\$\d+(?:\.\d+)?")

_BUY_KEYWORDS = {"buy", "long", "calls", "bullish", "moon", "breakout", "entry", "dip"}
_SELL_KEYWORDS = {"sell", "short", "puts", "bearish", "dump", "breakdown", "exit"}

_CONTEXT_WINDOW = 50   # characters around a ticker mention to search for direction


# ── Parser (module-level for easy unit testing) ───────────────────────────────

def _parse_message(
    content: str,
    author: str,
    channel: str,
    guild: str,
) -> List[Signal]:
    """Parse a Discord message and return a list of Signal objects.

    Args:
        content:  Raw message text.
        author:   Username of the message sender.
        channel:  Channel name (without #).
        guild:    Server/guild name.

    Returns:
        List of Signal objects with direction BUY or SELL. Messages with no
        discernible direction are skipped (empty list returned).
    """
    upper = content.upper()
    results: List[Signal] = []
    seen: set[str] = set()

    # Collect tickers with metadata (ticker, position_in_text, explicit)
    mentions: list[tuple[str, int, bool]] = []

    for m in _TICKER_EXPLICIT.finditer(content):
        ticker = m.group(1)
        mentions.append((ticker, m.start(), True))

    for m in _TICKER_BARE.finditer(content):
        ticker = m.group(1)
        if ticker in COMMON_WORDS:
            continue
        if not any(t == ticker for t, _, _ in mentions):
            mentions.append((ticker, m.start(), False))

    for ticker, pos, explicit in mentions:
        if ticker in seen:
            continue
        seen.add(ticker)

        # Extract context window around the ticker mention
        lo = max(0, pos - _CONTEXT_WINDOW)
        hi = min(len(content), pos + len(ticker) + _CONTEXT_WINDOW)
        ctx = content[lo:hi].lower()

        # Determine direction
        is_buy = any(kw in ctx for kw in _BUY_KEYWORDS)
        is_sell = any(kw in ctx for kw in _SELL_KEYWORDS)

        if not is_buy and not is_sell:
            continue  # no direction → skip

        # Ambiguous → use whichever side has more keyword hits (tie → skip)
        if is_buy and is_sell:
            buy_count = sum(1 for kw in _BUY_KEYWORDS if kw in ctx)
            sell_count = sum(1 for kw in _SELL_KEYWORDS if kw in ctx)
            if buy_count == sell_count:
                continue
            direction = "BUY" if buy_count > sell_count else "SELL"
        else:
            direction = "BUY" if is_buy else "SELL"

        # Score
        score = 0.55
        if explicit:
            score += 0.05
        if _PRICE_NEAR.search(content[lo:hi]):
            score += 0.05
        score = min(score, 0.80)

        snippet = content[:120].replace("\n", " ")
        reasoning = f"Discord: {author} in #{channel}: {snippet}"

        sig = Signal(
            ticker=ticker,
            direction=direction,
            score=score,
            entry_price=0.0,
            stop_price=0.0,
            target_price=0.0,
            limit_price=0.0,
            strategy="discord",
            reasoning=reasoning,
        )
        results.append(sig)

    return results


# ── DiscordFeed class ─────────────────────────────────────────────────────────

class DiscordFeed:
    """Connects to Discord as a bot, monitors channels, and emits Signal objects.

    Usage:
        feed = DiscordFeed(cfg.discord, engine.get_signal_queue())
        await feed.start()    # runs until stopped or Ctrl+C
        await feed.stop()
    """

    def __init__(self, config: DiscordConfig, signal_queue: asyncio.Queue) -> None:
        try:
            import discord  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "discord.py not installed. Run: pip install 'nexus-trader[discord]'"
            ) from exc

        intents = discord.Intents.default()
        intents.message_content = True

        self._client = discord.Client(intents=intents)
        self._cfg = config
        self._queue = signal_queue
        self._seen_ids: set[int] = set()
        self._discord = discord

        # Wire event handlers
        self._client.event(self._on_ready)
        self._client.event(self._on_message)

    # ── Discord event handlers ────────────────────────────────────────────────

    async def _on_ready(self) -> None:
        log.info("Discord bot connected",
                 user=str(self._client.user),
                 guilds=len(self._client.guilds))
        await self._fetch_history()

    async def _on_message(self, message) -> None:
        # Ignore own messages
        if message.author == self._client.user:
            return
        await self._process(message)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _fetch_history(self) -> None:
        """On startup, backfill recent messages from configured channels."""
        for guild in self._client.guilds:
            for channel in guild.text_channels:
                if (self._cfg.channel_ids and
                        channel.id not in self._cfg.channel_ids):
                    continue
                try:
                    count = 0
                    async for msg in channel.history(limit=self._cfg.history_limit):
                        await self._process(msg)
                        count += 1
                    log.info("Backfilled channel history",
                             channel=channel.name, guild=guild.name,
                             messages=count)
                except Exception as e:
                    log.warning("Could not read channel history",
                                channel=channel.name, error=str(e))

    async def _process(self, message) -> None:
        """Deduplicate and parse a single Discord message."""
        if message.id in self._seen_ids:
            return
        self._seen_ids.add(message.id)

        # Channel filter
        if (self._cfg.channel_ids and
                message.channel.id not in self._cfg.channel_ids):
            return

        content = getattr(message, "content", "") or ""
        if not content.strip():
            return

        author = str(message.author.display_name)
        channel = str(message.channel.name)
        guild = str(message.guild.name) if message.guild else "DM"

        signals = _parse_message(content, author, channel, guild)

        # Optional LLM confirmation
        if self._cfg.use_llm_parsing and signals:
            signals = await self._llm_confirm(content, signals)

        for sig in signals:
            if sig.score >= self._cfg.min_message_score:
                self._queue.put_nowait(sig)
                log.info("Discord signal",
                         ticker=sig.ticker, direction=sig.direction,
                         score=f"{sig.score:.2f}", author=author,
                         channel=channel)

    async def _llm_confirm(self, content: str, signals: List[Signal]) -> List[Signal]:
        """Optional: use Claude to confirm/boost signal confidence."""
        try:
            import anthropic  # type: ignore[import]
            import json

            client = anthropic.AsyncAnthropic()
            tickers = [s.ticker for s in signals]
            prompt = (
                f"Analyze this Discord trading message and return JSON array of "
                f"{{ticker, direction (BUY/SELL), confidence (0.0-1.0)}} for these tickers: "
                f"{tickers}.\nMessage: {content[:500]}\n"
                "Return ONLY valid JSON array, no explanation."
            )
            resp = await client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            parsed = json.loads(text)
            llm_map = {item["ticker"]: item for item in parsed}

            boosted: List[Signal] = []
            for sig in signals:
                llm = llm_map.get(sig.ticker)
                if llm and llm.get("direction") == sig.direction:
                    sig.score = min(sig.score + 0.10, 0.90)
                boosted.append(sig)
            return boosted
        except Exception as e:
            log.debug("LLM parsing failed, using regex result", error=str(e))
            return signals

    # ── Public interface ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the Discord bot. Runs until stop() is called."""
        if not self._cfg.bot_token:
            log.warning("DISCORD_BOT_TOKEN not set — Discord feed disabled")
            return
        log.info("Starting Discord feed",
                 channel_ids=self._cfg.channel_ids or "all",
                 history_limit=self._cfg.history_limit)
        async with self._client:
            await self._client.start(self._cfg.bot_token)

    async def stop(self) -> None:
        """Disconnect the Discord bot gracefully."""
        if not self._client.is_closed():
            await self._client.close()
            log.info("Discord feed stopped")
