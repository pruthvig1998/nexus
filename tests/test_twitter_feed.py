"""Unit tests for Twitter/Nitter RSS feed integration.

Tests cover RSS XML parsing, tweet-to-signal conversion, dedup logic,
instance health fallback, and aiohttp fetch with mocking.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

from nexus.config import TwitterConfig
from nexus.twitter_feed import TwitterFeed, _parse_rss, _parse_tweet

# ── Sample RSS XML ───────────────────────────────────────────────────────────

VALID_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>@DeItaone</title>
<item>
  <title>*AAPL BEATS Q3 EPS ESTIMATES</title>
  <description><![CDATA[<p>*AAPL BEATS Q3 EPS ESTIMATES, reports $1.40 vs $1.35 expected. Stock up 3% AH. Buy calls.</p>]]></description>
  <link>https://nitter.poast.org/DeItaone/status/123</link>
  <guid>https://nitter.poast.org/DeItaone/status/123</guid>
  <pubDate>Mon, 15 Mar 2026 16:30:00 GMT</pubDate>
</item>
<item>
  <title>$TSLA DOWNGRADED by Morgan Stanley</title>
  <description><![CDATA[<p>$TSLA DOWNGRADED by Morgan Stanley to sell. PT lowered to $150 from $200.</p>]]></description>
  <link>https://nitter.poast.org/DeItaone/status/456</link>
  <guid>https://nitter.poast.org/DeItaone/status/456</guid>
  <pubDate>Mon, 15 Mar 2026 16:35:00 GMT</pubDate>
</item>
</channel>
</rss>
"""

# ── 1. RSS XML Parsing (_parse_rss) ─────────────────────────────────────────


class TestParseRss:
    def test_parse_rss_valid(self):
        """Valid RSS XML with 2 items returns list of 2 dicts with correct fields."""
        items = _parse_rss(VALID_RSS)
        assert len(items) == 2

        first = items[0]
        assert first["title"] == "*AAPL BEATS Q3 EPS ESTIMATES"
        assert "nitter.poast.org/DeItaone/status/123" in first["link"]
        assert first["guid"] == "https://nitter.poast.org/DeItaone/status/123"
        assert "Mon, 15 Mar 2026" in first["pubdate"]
        assert "$1.40" in first["text"]

        second = items[1]
        assert second["title"] == "$TSLA DOWNGRADED by Morgan Stanley"
        assert "status/456" in second["guid"]

    def test_parse_rss_html_stripping(self):
        """Description containing <p> and <a> tags produces text with no HTML."""
        rss_with_html = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<item>
  <title>Test</title>
  <description><![CDATA[<p>Hello <a href="https://example.com">world</a> and <b>bold</b> text</p>]]></description>
  <link>https://example.com/1</link>
  <guid>guid-html-1</guid>
  <pubDate>Mon, 15 Mar 2026 12:00:00 GMT</pubDate>
</item>
</channel>
</rss>
"""
        items = _parse_rss(rss_with_html)
        assert len(items) == 1
        text = items[0]["text"]
        assert "<p>" not in text
        assert "<a " not in text
        assert "</a>" not in text
        assert "<b>" not in text
        assert "Hello" in text
        assert "world" in text

    def test_parse_rss_malformed_xml(self):
        """Invalid XML string returns empty list without crashing."""
        result = _parse_rss("this is not valid xml <<>><<<")
        assert result == []

    def test_parse_rss_missing_fields(self):
        """Item with missing description/link returns dict with empty strings."""
        rss_missing = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<item>
  <title>Headline only</title>
  <guid>guid-missing-1</guid>
</item>
</channel>
</rss>
"""
        items = _parse_rss(rss_missing)
        assert len(items) == 1
        item = items[0]
        assert item["title"] == "Headline only"
        assert item["text"] == ""
        assert item["link"] == ""


# ── 2. Tweet -> Signal Parsing (_parse_tweet) ───────────────────────────────


class TestParseTweet:
    def test_parse_tweet_buy_signal(self):
        """'$AAPL looking bullish, buying calls' produces BUY signal for AAPL."""
        signals = _parse_tweet("$AAPL looking bullish, buying calls", "DeItaone")
        assert len(signals) >= 1
        aapl = [s for s in signals if s.ticker == "AAPL"]
        assert len(aapl) == 1
        assert aapl[0].direction == "BUY"
        assert aapl[0].strategy == "twitter"
        assert aapl[0].reasoning.startswith("Twitter: @DeItaone")

    def test_parse_tweet_sell_signal(self):
        """'$TSLA short, puts loaded' produces SELL signal for TSLA."""
        signals = _parse_tweet("$TSLA short, puts loaded", "DeItaone")
        assert len(signals) >= 1
        tsla = [s for s in signals if s.ticker == "TSLA"]
        assert len(tsla) == 1
        assert tsla[0].direction == "SELL"

    def test_parse_tweet_no_direction(self):
        """'$NVDA reported earnings' with no direction keywords yields no signals."""
        signals = _parse_tweet("$NVDA reported earnings", "DeItaone")
        assert signals == []

    def test_parse_tweet_common_words_filtered(self):
        """Common words like BIG, FOR, ALL are not extracted as tickers."""
        signals = _parse_tweet("BIG news FOR ALL", "DeItaone")
        assert signals == []
        # Verify none of the common words appear as tickers
        tickers = [s.ticker for s in signals]
        assert "BIG" not in tickers
        assert "FOR" not in tickers
        assert "ALL" not in tickers

    def test_parse_tweet_multi_ticker(self):
        """Tweet with two tickers and clear directional keywords produces 2 signals."""
        # Separate tickers by >120 chars so 60-char context windows don't overlap
        text = (
            "$AAPL looking bullish, buying calls on the breakout here. "
            "This is a great long opportunity with strong momentum and earnings beat. "
            "On the other hand $MSFT is tanking hard, selling and going short on weakness."
        )
        signals = _parse_tweet(text, "DeItaone")
        assert len(signals) == 2
        by_ticker = {s.ticker: s.direction for s in signals}
        assert by_ticker["AAPL"] == "BUY"
        assert by_ticker["MSFT"] == "SELL"

    def test_parse_tweet_explicit_ticker_score_boost(self):
        """Explicit $TICKER gets +0.05 score boost over bare ticker."""
        explicit = _parse_tweet("$AAPL buy", "DeItaone")
        bare = _parse_tweet("AAPL buy", "DeItaone")
        assert len(explicit) == 1
        assert len(bare) == 1
        assert explicit[0].score > bare[0].score

    def test_parse_tweet_empty_text(self):
        """Empty string produces no signals."""
        signals = _parse_tweet("", "DeItaone")
        assert signals == []

    def test_parse_tweet_short_text(self):
        """Very short text 'hi' produces no signals."""
        signals = _parse_tweet("hi", "DeItaone")
        assert signals == []


# ── 3. Dedup (TwitterFeed._process_item) ────────────────────────────────────


class TestDedup:
    def _make_feed(self):
        cfg = TwitterConfig(accounts=["test"], nitter_instances=["nitter.test.org"])
        queue = asyncio.Queue()
        feed = TwitterFeed(cfg, queue)
        return feed, queue

    async def test_dedup_same_guid(self):
        """Processing same item twice emits signals only once."""
        feed, queue = self._make_feed()
        item = {
            "title": "$AAPL buy",
            "text": "$AAPL buy calls looking bullish",
            "link": "https://nitter.test.org/test/status/999",
            "guid": "guid-dedup-1",
            "pubdate": "Mon, 15 Mar 2026 12:00:00 GMT",
        }

        await feed._process_item(item, "test")
        first_size = queue.qsize()

        await feed._process_item(item, "test")
        second_size = queue.qsize()

        # Second call should not add more signals
        assert second_size == first_size

    async def test_dedup_lru_eviction(self):
        """Adding >10000 unique guids does not grow the seen set unbounded."""
        feed, queue = self._make_feed()

        for i in range(10_001):
            item = {
                "title": "neutral headline",
                "text": "neutral headline",
                "link": f"https://nitter.test.org/test/status/{i}",
                "guid": f"guid-eviction-{i}",
                "pubdate": "Mon, 15 Mar 2026 12:00:00 GMT",
            }
            await feed._process_item(item, "test")

        # Internal seen set should be bounded (<=10000)
        assert len(feed._seen_guids) <= 10_000


# ── 4. Instance Fallback (_sorted_instances, _mark_unhealthy) ───────────────


class TestInstanceFallback:
    def _make_feed(self, instances=None):
        instances = instances or ["a.nitter.org", "b.nitter.org", "c.nitter.org"]
        cfg = TwitterConfig(accounts=["test"], nitter_instances=instances)
        queue = asyncio.Queue()
        feed = TwitterFeed(cfg, queue)
        return feed

    def test_sorted_instances_healthy_first(self):
        """All healthy instances are returned."""
        feed = self._make_feed()
        result = feed._sorted_instances()
        assert len(result) == 3
        assert set(result) == {"a.nitter.org", "b.nitter.org", "c.nitter.org"}

    def test_sorted_instances_unhealthy_excluded(self):
        """Recently marked unhealthy instance is excluded."""
        feed = self._make_feed()
        feed._mark_unhealthy("b.nitter.org")
        result = feed._sorted_instances()
        assert "b.nitter.org" not in result
        assert len(result) == 2

    def test_sorted_instances_cooldown_recovery(self):
        """Instance marked unhealthy >300s ago is included again."""
        feed = self._make_feed()
        feed._mark_unhealthy("b.nitter.org")
        # Backdate the failure timestamp to >300s ago
        feed._instance_last_fail["b.nitter.org"] = time.monotonic() - 400
        result = feed._sorted_instances()
        assert "b.nitter.org" in result
        assert len(result) == 3


# ── 5. Fetch Feed (mock aiohttp) ────────────────────────────────────────────


class _MockResponse:
    """Lightweight mock for aiohttp response used in async context manager."""

    def __init__(self, status: int, text: str):
        self.status = status
        self._text = text

    async def text(self):
        return self._text


class _MockContextManager:
    """Mock async context manager for session.get()."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        pass


class TestFetchFeed:
    def _make_feed(self, instances=None):
        instances = instances or ["first.nitter.org", "second.nitter.org"]
        cfg = TwitterConfig(accounts=["test"], nitter_instances=instances)
        queue = asyncio.Queue()
        feed = TwitterFeed(cfg, queue)
        return feed

    async def test_fetch_feed_first_succeeds(self):
        """First instance returns 200 with XML content."""
        feed = self._make_feed()
        resp = _MockResponse(200, VALID_RSS)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=_MockContextManager(resp))

        feed._session = mock_session
        result = await feed._fetch_feed("test")

        assert result is not None
        assert "AAPL" in result

    async def test_fetch_feed_fallback(self):
        """First instance returns 500, second returns 200 with XML."""
        feed = self._make_feed()

        responses = [_MockResponse(500, "error"), _MockResponse(200, VALID_RSS)]
        call_idx = 0

        def mock_get(url, **kwargs):
            nonlocal call_idx
            resp = responses[call_idx]
            call_idx += 1
            return _MockContextManager(resp)

        mock_session = MagicMock()
        mock_session.get = mock_get

        feed._session = mock_session
        result = await feed._fetch_feed("test")

        assert result is not None
        assert "AAPL" in result

    async def test_fetch_feed_all_fail(self):
        """All instances fail; returns None and increments _fetch_errors."""
        feed = self._make_feed()
        initial_errors = feed._fetch_errors

        resp = _MockResponse(500, "error")
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=_MockContextManager(resp))

        feed._session = mock_session
        result = await feed._fetch_feed("test")

        assert result is None
        assert feed._fetch_errors > initial_errors
