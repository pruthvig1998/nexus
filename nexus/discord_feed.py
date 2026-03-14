"""NEXUS Discord Feed — monitors Discord channels for trading signals.

Runs as an asyncio task alongside the engine. Parses messages for ticker
mentions + direction keywords and injects Signal objects into the engine's
queue.

Setup:
  1. discord.com/developers/applications -> New App -> Bot -> copy Token
  2. Bot -> Privileged Gateway Intents -> enable Message Content Intent
  3. OAuth2 -> URL Generator -> bot + Read Messages -> invite to servers
  4. Set DISCORD_BOT_TOKEN and DISCORD_CHANNEL_IDS in .env

v3.1 improvements:
  - Proximity-weighted ambiguity resolution (not just keyword count)
  - Expanded BUY/SELL keyword sets with strength tiers
  - Edge-case handling: empty, very short, all-caps messages
  - JSON-schema-validated LLM confirmation
  - Stats tracking (messages_processed, signals_emitted, dedup_skipped)
  - Structured logging with context fields throughout
  - Full type hints
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from nexus.config import DiscordConfig
from nexus.logger import get_logger
from nexus.strategy import Signal

log = get_logger("discord_feed")

# ── Constants ─────────────────────────────────────────────────────────────────

COMMON_WORDS: set[str] = {
    "A", "AM", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "HE", "HI",
    "I", "IF", "IN", "IS", "IT", "ME", "MY", "NO", "OF", "OK", "ON",
    "OR", "PM", "RE", "SO", "TO", "UP", "US", "WE",
    # Common 3-letter words that look like tickers
    "ALL", "AND", "ARE", "BIG", "BUT", "CAN", "CEO", "DAY", "DID",
    "EPS", "ETF", "FOR", "GET", "GOT", "HAS", "HIS", "HOW", "IMO",
    "ITS", "LET", "LOL", "MAY", "MOM", "NEW", "NOT", "NOW", "OLD",
    "ONE", "OUR", "OUT", "OWN", "RUN", "SAY", "SEC", "SET", "SHE",
    "THE", "TOP", "TRY", "TWO", "USE", "WAY", "WHO", "WHY", "WIN",
    "YES", "YET", "YOU",
    # Common 4-letter words
    "BEEN", "BEST", "CALL", "CASH", "DEBT", "DOES", "DONE", "DOWN",
    "DROP", "EACH", "EDIT", "EVEN", "ELON", "FAST", "FROM", "GAIN",
    "GOOD", "GROW", "HALF", "HAVE", "HERE", "HIGH", "HOLD", "HOPE",
    "INTO", "JUST", "KEEP", "KNOW", "LAST", "LIKE", "LONG", "LOOK",
    "LOSS", "LOTS", "LOVE", "MADE", "MAKE", "MANY", "MORE", "MUCH",
    "MUST", "NEAR", "NEED", "NEWS", "NEXT", "NICE", "ONCE", "ONLY",
    "OVER", "PAID", "PEAK", "PLAY", "PULL", "PUSH", "PUTS", "REAL",
    "RISK", "SAFE", "SAME", "SELL", "SEEN", "SEND", "SENT", "SOME",
    "STOP", "SURE", "TAKE", "TALK", "THAT", "THEM", "THEN", "THEY",
    "THIS", "TIME", "TOLD", "TOOK", "TURN", "VERY", "WAIT", "WANT",
    "WENT", "WERE", "WHAT", "WHEN", "WILL", "WITH", "WORK", "YEAH",
    "YEAR", "YOUR", "ZERO",
}

_TICKER_EXPLICIT = re.compile(r"\$([A-Z]{1,5})\b")
_TICKER_BARE = re.compile(r"\b([A-Z]{2,5})\b")
_PRICE_NEAR = re.compile(r"\$\d+(?:\.\d+)?")

# Direction keywords organized by strength tier.
# Strong keywords (weight 2.0) are unambiguous trading actions.
# Medium keywords (weight 1.5) are moderately directional.
# Weak keywords (weight 1.0) are suggestive but context-dependent.
_BUY_KEYWORDS: Dict[str, float] = {
    # Strong (2.0)
    "buy":        2.0,
    "bought":     2.0,
    "buying":     2.0,
    "long":       2.0,
    "calls":      2.0,
    "call":       1.5,
    "bid":        1.5,
    # Medium (1.5)
    "bullish":    1.5,
    "breakout":   1.5,
    "accumulate": 1.5,
    "loading":    1.5,
    "added":      1.5,
    "adding":     1.5,
    "entry":      1.5,
    "upgrade":    1.5,
    "upgraded":   1.5,
    "upside":     1.5,
    "rally":      1.5,
    # Weak (1.0)
    "moon":       1.0,
    "rocket":     1.0,
    "dip":        1.0,
    "oversold":   1.0,
    "undervalued": 1.0,
    "bottom":     1.0,
    "bounce":     1.0,
    "green":      1.0,
    "rip":        1.0,
    "squeeze":    1.0,
    "send":       1.0,
}

_SELL_KEYWORDS: Dict[str, float] = {
    # Strong (2.0)
    "sell":       2.0,
    "sold":       2.0,
    "selling":    2.0,
    "short":      2.0,
    "puts":       2.0,
    "put":        1.5,
    # Medium (1.5)
    "bearish":    1.5,
    "breakdown":  1.5,
    "dumping":    1.5,
    "dump":       1.5,
    "exit":       1.5,
    "exiting":    1.5,
    "downgrade":  1.5,
    "downgraded": 1.5,
    "downside":   1.5,
    "trimming":   1.5,
    "trim":       1.5,
    "cut":        1.5,
    # Weak (1.0)
    "crash":      1.0,
    "tank":       1.0,
    "tanking":    1.0,
    "overvalued": 1.0,
    "overbought": 1.0,
    "fade":       1.0,
    "fading":     1.0,
    "red":        1.0,
    "drop":       1.0,
    "falling":    1.0,
    "drill":      1.0,
    "rug":        1.0,
    "baghold":    1.0,
}

_CONTEXT_WINDOW: int = 60  # characters around a ticker mention to search

# Minimum message length to bother parsing (after stripping whitespace).
_MIN_MESSAGE_LENGTH: int = 3

# LLM confirmation JSON schema keys for validation.
_LLM_REQUIRED_KEYS: set[str] = {"ticker", "direction", "confidence"}
_LLM_VALID_DIRECTIONS: set[str] = {"BUY", "SELL", "HOLD"}


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

    Ambiguity resolution uses proximity-weighted scoring: each keyword's
    contribution is scaled by ``weight / (1 + distance_to_ticker)`` so
    keywords closer to the ticker dominate over distant ones.
    """
    # ── Edge cases ─────────────────────────────────────────────────────────
    if not content or not content.strip():
        return []

    stripped = content.strip()
    if len(stripped) < _MIN_MESSAGE_LENGTH:
        return []

    # Normalize all-caps messages: if >80% uppercase alpha chars, lowercase
    # the whole thing for keyword matching (but keep original for ticker regex).
    alpha_chars = [c for c in stripped if c.isalpha()]
    if alpha_chars and sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars) > 0.80:
        search_text = stripped.lower()
        # Re-uppercase potential tickers that were $-prefixed in original
        normalized = True
    else:
        search_text = content
        normalized = False

    results: List[Signal] = []
    seen: set[str] = set()

    # ── Collect ticker mentions with metadata ──────────────────────────────
    mentions: List[Tuple[str, int, bool]] = []  # (ticker, position, explicit)

    for m in _TICKER_EXPLICIT.finditer(content):
        ticker = m.group(1)
        if ticker not in COMMON_WORDS:
            mentions.append((ticker, m.start(), True))

    for m in _TICKER_BARE.finditer(content):
        ticker = m.group(1)
        if ticker in COMMON_WORDS:
            continue
        if not any(t == ticker for t, _, _ in mentions):
            mentions.append((ticker, m.start(), False))

    # ── Score each ticker mention ──────────────────────────────────────────
    for ticker, pos, explicit in mentions:
        if ticker in seen:
            continue
        seen.add(ticker)

        # Extract context window around the ticker mention.
        lo = max(0, pos - _CONTEXT_WINDOW)
        hi = min(len(content), pos + len(ticker) + _CONTEXT_WINDOW)
        ctx = content[lo:hi].lower()

        # Proximity-weighted direction scoring.
        # For each keyword found in the context window, compute:
        #   contribution = keyword_weight / (1 + char_distance_to_ticker)
        # where char_distance is the distance from the keyword start to
        # the ticker position (both relative to the context window).
        ticker_pos_in_ctx = pos - lo

        buy_score = _compute_direction_score(
            ctx, ticker_pos_in_ctx, _BUY_KEYWORDS,
        )
        sell_score = _compute_direction_score(
            ctx, ticker_pos_in_ctx, _SELL_KEYWORDS,
        )

        if buy_score == 0.0 and sell_score == 0.0:
            continue  # no directional keywords found

        # Ambiguous: use proximity-weighted scores (ties -> skip)
        if buy_score > 0.0 and sell_score > 0.0:
            # Require at least 30% relative advantage to resolve ambiguity
            total = buy_score + sell_score
            if buy_score / total < 0.65 and sell_score / total < 0.65:
                log.debug(
                    "Ambiguous signal skipped",
                    ticker=ticker,
                    buy_score=f"{buy_score:.3f}",
                    sell_score=f"{sell_score:.3f}",
                    author=author,
                    channel=channel,
                )
                continue
            direction = "BUY" if buy_score > sell_score else "SELL"
        else:
            direction = "BUY" if buy_score > 0.0 else "SELL"

        # ── Confidence score ───────────────────────────────────────────────
        score: float = 0.55
        if explicit:
            score += 0.05
        if _PRICE_NEAR.search(content[lo:hi]):
            score += 0.05
        score = round(min(score, 0.80), 2)

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


def _compute_direction_score(
    ctx: str,
    ticker_pos: int,
    keywords: Dict[str, float],
) -> float:
    """Compute proximity-weighted directional score for a set of keywords.

    Each keyword hit contributes: ``weight / (1 + distance)`` where distance
    is the character offset between the keyword and the ticker position within
    the context window. This ensures nearby keywords dominate.

    Args:
        ctx:        Lowercased context window text.
        ticker_pos: Position of the ticker within the context window.
        keywords:   Mapping of keyword -> base weight.

    Returns:
        Total proximity-weighted score (0.0 if no keywords found).
    """
    total: float = 0.0
    for kw, weight in keywords.items():
        # Use word-boundary-aware search to avoid partial matches
        # (e.g., "long" should not match "along")
        pattern = rf"\b{re.escape(kw)}\b"
        for m in re.finditer(pattern, ctx):
            distance = abs(m.start() - ticker_pos)
            total += weight / (1.0 + distance)
    return total


# ── DiscordFeed class ─────────────────────────────────────────────────────────

class DiscordFeed:
    """Connects to Discord as a bot, monitors channels, and emits Signal objects.

    Usage:
        feed = DiscordFeed(cfg.discord, engine.get_signal_queue())
        await feed.start()    # runs until stopped or Ctrl+C
        await feed.stop()

    Attributes:
        stats: Read-only dict of operational counters.
    """

    def __init__(self, config: DiscordConfig, signal_queue: asyncio.Queue) -> None:  # type: ignore[type-arg]
        try:
            import discord  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "discord.py not installed. Run: pip install 'nexus-trader[discord]'"
            ) from exc

        intents = discord.Intents.default()
        intents.message_content = True

        self._client: discord.Client = discord.Client(intents=intents)
        self._cfg: DiscordConfig = config
        self._queue: asyncio.Queue = signal_queue  # type: ignore[type-arg]
        self._seen_ids: set[int] = set()
        self._discord = discord

        # Operational counters
        self._messages_processed: int = 0
        self._signals_emitted: int = 0
        self._dedup_skipped: int = 0

        # Wire event handlers
        self._client.event(self._on_ready)
        self._client.event(self._on_message)

    # ── Stats property ─────────────────────────────────────────────────────

    @property
    def stats(self) -> Dict[str, int]:
        """Return operational counters as a dict."""
        return {
            "messages_processed": self._messages_processed,
            "signals_emitted": self._signals_emitted,
            "dedup_skipped": self._dedup_skipped,
        }

    # ── Discord event handlers ─────────────────────────────────────────────

    async def _on_ready(self) -> None:
        user_str = str(self._client.user)
        guild_count = len(self._client.guilds)
        guild_names = [g.name for g in self._client.guilds]
        log.info(
            "Discord bot connected",
            user=user_str,
            guilds=guild_count,
            guild_names=guild_names,
        )
        await self._fetch_history()

    async def _on_message(self, message: Any) -> None:
        # Ignore own messages
        if message.author == self._client.user:
            return
        await self._process(message)

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _fetch_history(self) -> None:
        """On startup, backfill recent messages from configured channels."""
        for guild in self._client.guilds:
            for channel in guild.text_channels:
                if (self._cfg.channel_ids
                        and channel.id not in self._cfg.channel_ids):
                    continue
                try:
                    count: int = 0
                    async for msg in channel.history(limit=self._cfg.history_limit):
                        await self._process(msg)
                        count += 1
                    log.info(
                        "Backfilled channel history",
                        channel=channel.name,
                        guild=guild.name,
                        messages=count,
                    )
                except Exception as e:
                    log.warning(
                        "Could not read channel history",
                        channel=channel.name,
                        guild=guild.name,
                        error=str(e),
                    )

    async def _process(self, message: Any) -> None:
        """Deduplicate and parse a single Discord message."""
        if message.id in self._seen_ids:
            self._dedup_skipped += 1
            return
        self._seen_ids.add(message.id)

        # Channel filter
        if (self._cfg.channel_ids
                and message.channel.id not in self._cfg.channel_ids):
            return

        content: str = getattr(message, "content", "") or ""
        if not content.strip():
            log.debug("Skipping empty message", message_id=message.id)
            return

        self._messages_processed += 1

        author: str = str(message.author.display_name)
        channel: str = str(message.channel.name)
        guild: str = str(message.guild.name) if message.guild else "DM"

        signals = _parse_message(content, author, channel, guild)

        log.debug(
            "Parsed message",
            author=author,
            channel=channel,
            guild=guild,
            content_len=len(content),
            signals_found=len(signals),
        )

        # Optional LLM confirmation
        if self._cfg.use_llm_parsing and signals:
            signals = await self._llm_confirm(content, signals)

        for sig in signals:
            if sig.score >= self._cfg.min_message_score:
                self._queue.put_nowait(sig)
                self._signals_emitted += 1
                log.info(
                    "Discord signal emitted",
                    ticker=sig.ticker,
                    direction=sig.direction,
                    score=f"{sig.score:.2f}",
                    author=author,
                    channel=channel,
                    guild=guild,
                )
            else:
                log.debug(
                    "Signal below threshold",
                    ticker=sig.ticker,
                    direction=sig.direction,
                    score=f"{sig.score:.2f}",
                    threshold=self._cfg.min_message_score,
                )

    async def _llm_confirm(
        self, content: str, signals: List[Signal],
    ) -> List[Signal]:
        """Use Claude to confirm/boost signal confidence.

        Sends the message to the LLM and validates the response against an
        expected JSON schema. Only boosts signals where the LLM agrees on
        both ticker and direction.

        Returns the original signals (possibly boosted) on success, or
        unmodified signals on any failure.
        """
        try:
            import anthropic  # type: ignore[import]

            client = anthropic.AsyncAnthropic()
            tickers = [s.ticker for s in signals]
            prompt = (
                "Analyze this Discord trading message. For each ticker listed, "
                "determine the trading direction and your confidence.\n\n"
                f"Tickers to evaluate: {tickers}\n"
                f"Message: {content[:500]}\n\n"
                "Return ONLY a valid JSON array. Each element MUST have exactly "
                "these fields:\n"
                '  - "ticker": string (uppercase, e.g. "AAPL")\n'
                '  - "direction": string, one of "BUY", "SELL", "HOLD"\n'
                '  - "confidence": number between 0.0 and 1.0\n\n'
                "Example: "
                '[{"ticker":"AAPL","direction":"BUY","confidence":0.85}]\n'
                "Return ONLY the JSON array, no markdown, no explanation."
            )
            resp = await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            text: str = resp.content[0].text.strip()

            # Strip markdown fences if LLM wrapped the response
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)

            parsed = json.loads(text)

            if not isinstance(parsed, list):
                log.warning(
                    "LLM returned non-array JSON, ignoring",
                    type=type(parsed).__name__,
                )
                return signals

            # Validate and build lookup map
            llm_map: Dict[str, Dict[str, Any]] = {}
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                # Validate required keys
                if not _LLM_REQUIRED_KEYS.issubset(item.keys()):
                    log.debug(
                        "LLM item missing required keys",
                        item=item,
                        missing=list(_LLM_REQUIRED_KEYS - item.keys()),
                    )
                    continue
                # Validate direction
                direction = str(item.get("direction", "")).upper()
                if direction not in _LLM_VALID_DIRECTIONS:
                    log.debug(
                        "LLM item has invalid direction",
                        direction=direction,
                    )
                    continue
                # Validate confidence range
                try:
                    confidence = float(item["confidence"])
                except (ValueError, TypeError):
                    continue
                if not 0.0 <= confidence <= 1.0:
                    continue

                ticker = str(item["ticker"]).upper()
                llm_map[ticker] = {
                    "direction": direction,
                    "confidence": confidence,
                }

            # Apply boosts
            boosted: List[Signal] = []
            for sig in signals:
                llm = llm_map.get(sig.ticker)
                if llm and llm["direction"] == sig.direction:
                    old_score = sig.score
                    sig.score = min(sig.score + 0.10, 0.90)
                    log.debug(
                        "LLM boosted signal",
                        ticker=sig.ticker,
                        direction=sig.direction,
                        old_score=f"{old_score:.2f}",
                        new_score=f"{sig.score:.2f}",
                        llm_confidence=f"{llm['confidence']:.2f}",
                    )
                elif llm:
                    log.debug(
                        "LLM disagreed on direction",
                        ticker=sig.ticker,
                        our_direction=sig.direction,
                        llm_direction=llm["direction"],
                    )
                boosted.append(sig)
            return boosted

        except json.JSONDecodeError as e:
            log.warning("LLM returned invalid JSON", error=str(e))
            return signals
        except Exception as e:
            log.debug("LLM parsing failed, using regex result", error=str(e))
            return signals

    # ── Public interface ───────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the Discord bot. Runs until stop() is called."""
        if not self._cfg.bot_token:
            log.warning("DISCORD_BOT_TOKEN not set -- Discord feed disabled")
            return
        log.info(
            "Starting Discord feed",
            channel_ids=self._cfg.channel_ids or "all",
            history_limit=self._cfg.history_limit,
            use_llm=self._cfg.use_llm_parsing,
            min_score=self._cfg.min_message_score,
        )
        async with self._client:
            await self._client.start(self._cfg.bot_token)

    async def stop(self) -> None:
        """Disconnect the Discord bot gracefully."""
        if not self._client.is_closed():
            await self._client.close()
            log.info("Discord feed stopped", **self.stats)
