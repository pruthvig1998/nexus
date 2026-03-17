"""NEXUS Twitter/Nitter Feed — monitors Twitter accounts via Nitter RSS for trading signals.

Polls public Nitter RSS feeds for configured Twitter accounts, parses tweets
for ticker mentions and directional keywords, and injects Signal objects into
the engine's queue.

Uses the same parsing algorithm as discord_feed for consistency, with
strategy="twitter" and Twitter-specific reasoning format.

Instance health tracking rotates through multiple Nitter instances with
automatic cooldown on failure/rate-limiting.
"""

from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from html import unescape
from typing import Dict, List, Optional, Tuple

import aiohttp

from nexus.discord_feed import (
    _BUY_KEYWORDS,
    _CONTEXT_WINDOW,
    _PRICE_NEAR,
    _SELL_KEYWORDS,
    _TICKER_BARE,
    _TICKER_EXPLICIT,
    COMMON_WORDS,
    _compute_direction_score,
)
from nexus.logger import get_logger
from nexus.strategy import Signal

log = get_logger("twitter_feed")

# Minimum message length to bother parsing.
_MIN_MESSAGE_LENGTH: int = 3

# HTML tag stripper for RSS description fields.
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Seen-guid LRU limits.
_SEEN_MAX: int = 10_000
_SEEN_KEEP: int = 5_000

# Unhealthy instance cooldown in seconds.
_COOLDOWN_SECS: float = 300.0


# ── RSS Parser ────────────────────────────────────────────────────────────────


def _parse_rss(xml_text: str) -> List[dict]:
    """Parse Nitter RSS XML into a list of item dicts.

    Each dict contains:
        - title: str
        - text: str (HTML-stripped description)
        - link: str
        - guid: str
        - pubdate: str

    Returns an empty list on malformed/unparseable XML.
    """
    if not xml_text or not xml_text.strip():
        return []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        log.debug("Malformed RSS XML, skipping")
        return []

    items: List[dict] = []
    # Nitter RSS uses standard <channel><item> structure.
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        raw_desc = item.findtext("description") or ""
        # Strip HTML tags and unescape entities.
        text = unescape(_HTML_TAG_RE.sub("", raw_desc)).strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        pubdate = (item.findtext("pubDate") or "").strip()

        items.append(
            {
                "title": title,
                "text": text,
                "link": link,
                "guid": guid,
                "pubdate": pubdate,
            }
        )

    return items


# ── Tweet Parser ──────────────────────────────────────────────────────────────


def _parse_tweet(text: str, account: str) -> List[Signal]:
    """Parse a tweet and return a list of Signal objects.

    Uses the same algorithm as discord_feed._parse_message but with
    strategy='twitter' and Twitter-specific reasoning format.

    Args:
        text:    Raw tweet text (HTML already stripped).
        account: Twitter handle (without @).

    Returns:
        List of Signal objects with direction BUY or SELL.
    """
    if not text or not text.strip():
        return []

    stripped = text.strip()
    if len(stripped) < _MIN_MESSAGE_LENGTH:
        return []

    # Normalize all-caps messages for keyword matching.
    alpha_chars = [c for c in stripped if c.isalpha()]
    if alpha_chars and sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars) > 0.80:
        pass  # normalization flag noted; keyword matching uses .lower() on context

    results: List[Signal] = []
    seen: set[str] = set()

    # ── Collect ticker mentions with metadata ──────────────────────────────
    mentions: List[Tuple[str, int, bool]] = []  # (ticker, position, explicit)

    for m in _TICKER_EXPLICIT.finditer(text):
        ticker = m.group(1)
        if ticker not in COMMON_WORDS:
            mentions.append((ticker, m.start(), True))

    for m in _TICKER_BARE.finditer(text):
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
        hi = min(len(text), pos + len(ticker) + _CONTEXT_WINDOW)
        ctx = text[lo:hi].lower()

        ticker_pos_in_ctx = pos - lo

        buy_score = _compute_direction_score(ctx, ticker_pos_in_ctx, _BUY_KEYWORDS)
        sell_score = _compute_direction_score(ctx, ticker_pos_in_ctx, _SELL_KEYWORDS)

        if buy_score == 0.0 and sell_score == 0.0:
            continue

        # Ambiguity resolution: require 65% relative advantage.
        if buy_score > 0.0 and sell_score > 0.0:
            total = buy_score + sell_score
            if buy_score / total < 0.65 and sell_score / total < 0.65:
                log.debug(
                    "Ambiguous tweet signal skipped",
                    ticker=ticker,
                    buy_score=f"{buy_score:.3f}",
                    sell_score=f"{sell_score:.3f}",
                    account=account,
                )
                continue
            direction = "BUY" if buy_score > sell_score else "SELL"
        else:
            direction = "BUY" if buy_score > 0.0 else "SELL"

        # ── Confidence score ───────────────────────────────────────────────
        score: float = 0.55
        if explicit:
            score += 0.05
        if _PRICE_NEAR.search(text[lo:hi]):
            score += 0.05
        score = round(min(score, 0.80), 2)

        snippet = text[:120].replace("\n", " ")
        reasoning = f"Twitter: @{account}: {snippet}"

        sig = Signal(
            ticker=ticker,
            direction=direction,
            score=score,
            entry_price=0.0,
            stop_price=0.0,
            target_price=0.0,
            limit_price=0.0,
            strategy="twitter",
            reasoning=reasoning,
        )
        results.append(sig)

    return results


# ── TwitterFeed class ─────────────────────────────────────────────────────────


class TwitterFeed:
    """Monitors Twitter accounts via Nitter RSS and emits trading signals.

    Usage:
        feed = TwitterFeed(cfg.twitter, engine.get_signal_queue())
        await feed.start()   # blocks until stop() called
        feed.stop()
    """

    def __init__(
        self,
        config,  # TwitterConfig
        signal_queue: asyncio.Queue,  # type: ignore[type-arg]
        news_strategy=None,
    ) -> None:
        self._cfg = config
        self._queue: asyncio.Queue = signal_queue  # type: ignore[type-arg]
        self._news_strategy = news_strategy

        self._seen_guids: set[str] = set()
        self._instance_health: Dict[str, bool] = {inst: True for inst in self._cfg.nitter_instances}
        self._instance_last_fail: Dict[str, float] = {}
        self._running: bool = False
        self._session: Optional[aiohttp.ClientSession] = None

        # Operational counters.
        self._tweets_processed: int = 0
        self._signals_emitted: int = 0
        self._fetch_errors: int = 0

    @property
    def stats(self) -> Dict[str, int]:
        """Return operational counters."""
        return {
            "tweets_processed": self._tweets_processed,
            "signals_emitted": self._signals_emitted,
            "fetch_errors": self._fetch_errors,
        }

    # ── Public interface ───────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the polling loop. Blocks until stop() is called."""
        if not self._cfg.accounts:
            log.warning("No Twitter accounts configured -- Twitter feed disabled")
            return

        self._running = True
        timeout = aiohttp.ClientTimeout(total=15)
        self._session = aiohttp.ClientSession(timeout=timeout)

        log.info(
            "Starting Twitter feed",
            accounts=len(self._cfg.accounts),
            instances=len(self._cfg.nitter_instances),
            poll_interval=self._cfg.poll_interval,
            min_score=self._cfg.min_score,
        )

        try:
            while self._running:
                await self._poll_cycle()
                await asyncio.sleep(self._cfg.poll_interval)
        finally:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None
            log.info("Twitter feed stopped", **self.stats)

    async def stop(self) -> None:
        """Signal the polling loop to exit."""
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
        log.info("Twitter feed stopped", **self.stats)

    # ── Polling ────────────────────────────────────────────────────────────

    async def _poll_cycle(self) -> None:
        """Poll RSS feeds for all configured accounts."""
        for account in self._cfg.accounts:
            xml_text = await self._fetch_feed(account)
            if xml_text is None:
                continue

            items = _parse_rss(xml_text)
            for item in items:
                await self._process_item(item, account)

    async def _fetch_feed(self, account: str) -> Optional[str]:
        """Fetch RSS feed for an account, trying instances in health-priority order.

        Returns XML text on success, None if all instances fail.
        """
        if self._session is None:
            return None

        for inst in self._sorted_instances():
            url = f"https://{inst}/{account}/rss"
            try:
                async with self._session.get(url) as resp:
                    if resp.status == 200:
                        # Mark healthy on success.
                        self._instance_health[inst] = True
                        text = await resp.text()
                        return text
                    elif resp.status == 429:
                        log.warning(
                            "Nitter rate limited",
                            instance=inst,
                            account=account,
                            status=resp.status,
                        )
                        self._mark_unhealthy(inst)
                    else:
                        log.debug(
                            "Nitter fetch failed",
                            instance=inst,
                            account=account,
                            status=resp.status,
                        )
                        self._mark_unhealthy(inst)
            except Exception as e:
                log.debug(
                    "Nitter fetch exception",
                    instance=inst,
                    account=account,
                    error=str(e),
                )
                self._mark_unhealthy(inst)

        log.warning(
            "All Nitter instances failed for account",
            account=account,
            instances=len(self._cfg.nitter_instances),
        )
        self._fetch_errors += 1
        return None

    async def _process_item(self, item: dict, account: str) -> None:
        """Process a single RSS item: dedup, parse, and emit signals."""
        guid = item.get("guid", "")
        if not guid:
            return

        # Dedup by guid.
        if guid in self._seen_guids:
            return

        # LRU eviction: if at capacity, keep the most recent half.
        if len(self._seen_guids) >= _SEEN_MAX:
            to_keep = list(self._seen_guids)[-_SEEN_KEEP:]
            self._seen_guids = set(to_keep)

        self._seen_guids.add(guid)
        self._tweets_processed += 1

        # Feed headline to news strategy if available.
        text = item.get("text", "") or item.get("title", "")
        if text and self._news_strategy and hasattr(self._news_strategy, "add_headline"):
            self._news_strategy.add_headline(
                text=text,
                source=f"twitter:@{account}",
                timestamp=item.get("pubdate", ""),
            )

        # Parse for trading signals.
        signals = _parse_tweet(text, account)

        for sig in signals:
            if sig.score >= self._cfg.min_score:
                self._queue.put_nowait(sig)
                self._signals_emitted += 1
                log.info(
                    "Twitter signal emitted",
                    ticker=sig.ticker,
                    direction=sig.direction,
                    score=f"{sig.score:.2f}",
                    account=account,
                )
            else:
                log.debug(
                    "Twitter signal below threshold",
                    ticker=sig.ticker,
                    direction=sig.direction,
                    score=f"{sig.score:.2f}",
                    threshold=self._cfg.min_score,
                )

    # ── Instance health management ─────────────────────────────────────────

    def _mark_unhealthy(self, inst: str) -> None:
        """Mark a Nitter instance as unhealthy and record the failure time."""
        self._instance_health[inst] = False
        self._instance_last_fail[inst] = time.monotonic()

    def _sorted_instances(self) -> List[str]:
        """Return instances sorted: healthy first, then unhealthy past cooldown.

        Unhealthy instances that failed less than 300s ago are excluded.
        """
        now = time.monotonic()
        healthy: List[str] = []
        recovered: List[str] = []

        for inst in self._cfg.nitter_instances:
            if self._instance_health.get(inst, True):
                healthy.append(inst)
            else:
                last_fail = self._instance_last_fail.get(inst, 0.0)
                if now - last_fail >= _COOLDOWN_SECS:
                    recovered.append(inst)

        return healthy + recovered
