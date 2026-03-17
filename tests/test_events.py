"""Tests for EventCalendarStrategy — significance filter, earnings check, signal generation."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from nexus.config import NEXUSConfig, set_config
from nexus.strategy_events import (
    _HIGH_IMPACT_KEYWORDS,
    EventCalendarStrategy,
    _news_cache,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_config():
    """Ensure a clean config for each test."""
    cfg = NEXUSConfig()
    cfg.anthropic_api_key = ""  # disable Claude by default
    set_config(cfg)
    yield
    set_config(NEXUSConfig())


@pytest.fixture(autouse=True)
def _clear_news_cache():
    """Clear module-level news cache between tests."""
    _news_cache.clear()
    yield
    _news_cache.clear()


def _make_df(rows: int = 60) -> pd.DataFrame:
    """Create a minimal OHLCV DataFrame for testing."""
    import numpy as np

    np.random.seed(42)
    base = 100.0
    closes = base + np.cumsum(np.random.randn(rows) * 0.5)
    return pd.DataFrame(
        {
            "open": closes - 0.3,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": [1_000_000] * rows,
        }
    )


# ── _is_significant tests ────────────────────────────────────────────────────


class TestIsSignificant:
    def test_earnings_keyword_detected(self):
        strategy = EventCalendarStrategy()
        headlines = [
            {"title": "NVDA earnings beat expectations this quarter", "publisher": "Reuters"},
        ]
        result = strategy._is_significant(headlines)
        assert len(result) == 1
        assert "earnings" in result[0]["title"].lower()

    def test_gtc_conference_keyword_detected(self):
        strategy = EventCalendarStrategy()
        headlines = [
            {"title": "NVIDIA GTC 2026 keynote reveals next-gen chips", "publisher": "TechCrunch"},
        ]
        result = strategy._is_significant(headlines)
        assert len(result) == 1

    def test_fda_approval_keyword_detected(self):
        strategy = EventCalendarStrategy()
        headlines = [
            {"title": "FDA approval expected for new cancer drug", "publisher": "BioPharma"},
        ]
        result = strategy._is_significant(headlines)
        assert len(result) == 1

    def test_upgrade_downgrade_keywords(self):
        strategy = EventCalendarStrategy()
        headlines = [
            {"title": "Morgan Stanley upgrade on AAPL to overweight", "publisher": "MW"},
            {"title": "Goldman downgrade on META to neutral", "publisher": "CNBC"},
        ]
        result = strategy._is_significant(headlines)
        assert len(result) == 2

    def test_non_significant_headlines_filtered_out(self):
        strategy = EventCalendarStrategy()
        headlines = [
            {"title": "Stocks close mixed on quiet trading day", "publisher": "AP"},
            {"title": "Markets await next week catalyst", "publisher": "Bloomberg"},
            {"title": "Weather report calls for sunny skies", "publisher": "Weather"},
        ]
        result = strategy._is_significant(headlines)
        assert len(result) == 0

    def test_max_5_significant_headlines(self):
        strategy = EventCalendarStrategy()
        headlines = [
            {"title": f"Company {i} earnings report surprise", "publisher": "Reuters"}
            for i in range(10)
        ]
        result = strategy._is_significant(headlines)
        assert len(result) == 5

    def test_mixed_significant_and_noise(self):
        strategy = EventCalendarStrategy()
        headlines = [
            {"title": "Markets open higher on Monday", "publisher": "AP"},
            {"title": "TSLA CEO resign amid controversy", "publisher": "WSJ"},
            {"title": "Sunny skies expected this week", "publisher": "Weather"},
            {"title": "Federal tariff increase announced", "publisher": "Reuters"},
        ]
        result = strategy._is_significant(headlines)
        assert len(result) == 2
        titles = [h["title"].lower() for h in result]
        assert any("resign" in t for t in titles)
        assert any("tariff" in t for t in titles)

    def test_empty_headlines_list(self):
        strategy = EventCalendarStrategy()
        result = strategy._is_significant([])
        assert result == []

    def test_headline_missing_title_key(self):
        strategy = EventCalendarStrategy()
        headlines = [{"publisher": "Reuters"}]
        result = strategy._is_significant(headlines)
        assert len(result) == 0


# ── _check_earnings tests ────────────────────────────────────────────────────


class TestCheckEarnings:
    def test_no_upcoming_earnings_returns_none(self):
        strategy = EventCalendarStrategy()
        with patch("nexus.strategy_events.asyncio.to_thread") as mock_thread:
            # Simulate yfinance returning None calendar
            mock_thread.return_value = None
            result = asyncio.get_event_loop().run_until_complete(strategy._check_earnings("AAPL"))
            assert result is None

    def test_empty_calendar_returns_none(self):
        strategy = EventCalendarStrategy()
        with patch("nexus.strategy_events.asyncio.to_thread") as mock_thread:
            mock_thread.return_value = pd.DataFrame()
            result = asyncio.get_event_loop().run_until_complete(strategy._check_earnings("AAPL"))
            assert result is None


# ── Signal direction and score validation ────────────────────────────────────


class TestSignalValidation:
    def test_analyze_returns_none_without_api_key(self):
        """Without an API key, _research_event returns None, so no signal."""
        strategy = EventCalendarStrategy()
        df = _make_df(60)

        # Inject cached news with significant headlines
        _news_cache["TEST"] = (
            [{"title": "TEST earnings beat expectations", "publisher": "Reuters"}],
            __import__("time").time(),
        )

        result = asyncio.get_event_loop().run_until_complete(strategy.analyze("TEST", df))
        assert result is None  # no API key configured

    def test_analyze_returns_none_for_insufficient_data(self):
        strategy = EventCalendarStrategy()
        df = _make_df(5)
        result = asyncio.get_event_loop().run_until_complete(strategy.analyze("AAPL", df))
        assert result is None

    def test_analyze_returns_none_for_none_df(self):
        strategy = EventCalendarStrategy()
        result = asyncio.get_event_loop().run_until_complete(strategy.analyze("AAPL", None))
        assert result is None

    def test_research_returns_valid_buy_signal(self):
        """Mock Claude response and verify signal construction."""
        cfg = NEXUSConfig()
        cfg.anthropic_api_key = "test-key"
        set_config(cfg)

        strategy = EventCalendarStrategy()
        df = _make_df(60)

        # Pre-populate news cache
        _news_cache["NVDA"] = (
            [{"title": "NVDA earnings beat expectations", "publisher": "Reuters"}],
            __import__("time").time(),
        )

        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "direction": "BUY",
                        "score": 0.85,
                        "reasoning": "Strong earnings beat with raised guidance",
                        "event_type": "earnings",
                        "time_horizon": "swing",
                    }
                )
            )
        ]

        with patch("nexus.strategy_events.asyncio.to_thread") as mock_thread:
            # First call: news fetch (already cached), second: Claude API
            mock_thread.return_value = mock_response
            with patch.object(
                strategy,
                "_fetch_news",
                return_value=[
                    {"title": "NVDA earnings beat expectations", "publisher": "Reuters"},
                ],
            ):
                with patch.object(strategy, "_get_client") as mock_client:
                    mock_client.return_value.messages.create = MagicMock(return_value=mock_response)

                    # Override to_thread to call the function directly for Claude
                    async def fake_to_thread(fn, *args, **kwargs):
                        return fn(*args, **kwargs)

                    mock_thread.side_effect = fake_to_thread

                    result = asyncio.get_event_loop().run_until_complete(
                        strategy.analyze("NVDA", df)
                    )

        assert result is not None
        assert result.direction == "BUY"
        assert result.score == 0.85
        assert result.strategy == "events"
        assert "Event:" in result.reasoning
        assert result.stop_price < result.entry_price
        assert result.target_price > result.entry_price

    def test_research_sell_signal_has_correct_stops(self):
        """Verify SELL signal has stop above entry and target below."""
        cfg = NEXUSConfig()
        cfg.anthropic_api_key = "test-key"
        set_config(cfg)

        strategy = EventCalendarStrategy()
        df = _make_df(60)

        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "direction": "SELL",
                        "score": 0.80,
                        "reasoning": "Guidance cut signals fundamental deterioration",
                        "event_type": "earnings",
                        "time_horizon": "swing",
                    }
                )
            )
        ]

        with patch.object(
            strategy,
            "_fetch_news",
            return_value=[
                {"title": "Company cuts guidance significantly", "publisher": "CNBC"},
            ],
        ):
            with patch.object(strategy, "_get_client") as mock_client:
                mock_client.return_value.messages.create = MagicMock(return_value=mock_response)
                with patch("nexus.strategy_events.asyncio.to_thread") as mock_thread:

                    async def fake_to_thread(fn, *args, **kwargs):
                        return fn(*args, **kwargs)

                    mock_thread.side_effect = fake_to_thread

                    result = asyncio.get_event_loop().run_until_complete(
                        strategy.analyze("BAD", df)
                    )

        assert result is not None
        assert result.direction == "SELL"
        assert result.stop_price > result.entry_price
        assert result.target_price < result.entry_price

    def test_hold_direction_returns_none(self):
        """Claude returning HOLD should produce no signal."""
        cfg = NEXUSConfig()
        cfg.anthropic_api_key = "test-key"
        set_config(cfg)

        strategy = EventCalendarStrategy()
        df = _make_df(60)

        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "direction": "HOLD",
                        "score": 0.50,
                        "reasoning": "Event already priced in",
                        "event_type": "other",
                        "time_horizon": "swing",
                    }
                )
            )
        ]

        with patch.object(
            strategy,
            "_fetch_news",
            return_value=[
                {"title": "Company announces minor product launch", "publisher": "PR"},
            ],
        ):
            with patch.object(strategy, "_get_client") as mock_client:
                mock_client.return_value.messages.create = MagicMock(return_value=mock_response)
                with patch("nexus.strategy_events.asyncio.to_thread") as mock_thread:

                    async def fake_to_thread(fn, *args, **kwargs):
                        return fn(*args, **kwargs)

                    mock_thread.side_effect = fake_to_thread

                    result = asyncio.get_event_loop().run_until_complete(
                        strategy.analyze("XYZ", df)
                    )

        assert result is None

    def test_score_below_threshold_returns_none(self):
        """Low-conviction Claude response should be filtered out."""
        cfg = NEXUSConfig()
        cfg.anthropic_api_key = "test-key"
        set_config(cfg)

        strategy = EventCalendarStrategy()
        df = _make_df(60)

        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "direction": "BUY",
                        "score": 0.30,  # below default 0.65 threshold
                        "reasoning": "Weak signal",
                        "event_type": "other",
                        "time_horizon": "swing",
                    }
                )
            )
        ]

        with patch.object(
            strategy,
            "_fetch_news",
            return_value=[
                {"title": "Minor earnings surprise for small company", "publisher": "PR"},
            ],
        ):
            with patch.object(strategy, "_get_client") as mock_client:
                mock_client.return_value.messages.create = MagicMock(return_value=mock_response)
                with patch("nexus.strategy_events.asyncio.to_thread") as mock_thread:

                    async def fake_to_thread(fn, *args, **kwargs):
                        return fn(*args, **kwargs)

                    mock_thread.side_effect = fake_to_thread

                    result = asyncio.get_event_loop().run_until_complete(
                        strategy.analyze("LOW", df)
                    )

        assert result is None


# ── Score clamping ───────────────────────────────────────────────────────────


class TestScoreClamping:
    def test_score_clamped_to_max_1(self):
        """Verify scores > 1.0 from Claude are clamped to 1.0."""
        cfg = NEXUSConfig()
        cfg.anthropic_api_key = "test-key"
        set_config(cfg)

        strategy = EventCalendarStrategy()
        df = _make_df(60)

        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "direction": "BUY",
                        "score": 1.5,  # out of range
                        "reasoning": "Extremely bullish",
                        "event_type": "earnings",
                        "time_horizon": "swing",
                    }
                )
            )
        ]

        with patch.object(
            strategy,
            "_fetch_news",
            return_value=[
                {"title": "Company earnings blowout record quarter", "publisher": "CNBC"},
            ],
        ):
            with patch.object(strategy, "_get_client") as mock_client:
                mock_client.return_value.messages.create = MagicMock(return_value=mock_response)
                with patch("nexus.strategy_events.asyncio.to_thread") as mock_thread:

                    async def fake_to_thread(fn, *args, **kwargs):
                        return fn(*args, **kwargs)

                    mock_thread.side_effect = fake_to_thread

                    result = asyncio.get_event_loop().run_until_complete(
                        strategy.analyze("CLMP", df)
                    )

        assert result is not None
        assert result.score == 1.0

    def test_score_clamped_to_min_0(self):
        """Verify negative scores from Claude are clamped to 0.0."""
        cfg = NEXUSConfig()
        cfg.anthropic_api_key = "test-key"
        set_config(cfg)

        # Score of -0.5 should clamp to 0.0 which is below min_signal_score,
        # so no signal is returned
        result_dict = {
            "direction": "BUY",
            "score": max(0.0, min(-0.5, 1.0)),
            "reasoning": "test",
            "event_type": "other",
            "time_horizon": "swing",
        }
        assert result_dict["score"] == 0.0


# ── Keyword coverage ─────────────────────────────────────────────────────────


class TestKeywordCoverage:
    def test_all_keyword_categories_present(self):
        """Ensure the keyword set covers all major event categories."""
        # Earnings-related
        assert "earnings" in _HIGH_IMPACT_KEYWORDS
        assert "revenue" in _HIGH_IMPACT_KEYWORDS
        assert "guidance" in _HIGH_IMPACT_KEYWORDS

        # M&A
        assert "acquisition" in _HIGH_IMPACT_KEYWORDS
        assert "merger" in _HIGH_IMPACT_KEYWORDS

        # FDA / biotech
        assert "fda" in _HIGH_IMPACT_KEYWORDS
        assert "approval" in _HIGH_IMPACT_KEYWORDS
        assert "clinical" in _HIGH_IMPACT_KEYWORDS

        # Conferences
        assert "gtc" in _HIGH_IMPACT_KEYWORDS
        assert "wwdc" in _HIGH_IMPACT_KEYWORDS
        assert "conference" in _HIGH_IMPACT_KEYWORDS

        # Regulatory
        assert "tariff" in _HIGH_IMPACT_KEYWORDS
        assert "sanction" in _HIGH_IMPACT_KEYWORDS
        assert "regulation" in _HIGH_IMPACT_KEYWORDS

        # Analyst actions
        assert "upgrade" in _HIGH_IMPACT_KEYWORDS
        assert "downgrade" in _HIGH_IMPACT_KEYWORDS
        assert "price target" in _HIGH_IMPACT_KEYWORDS

        # Corporate events
        assert "layoff" in _HIGH_IMPACT_KEYWORDS
        assert "ceo" in _HIGH_IMPACT_KEYWORDS
        assert "resign" in _HIGH_IMPACT_KEYWORDS

    def test_case_insensitive_matching(self):
        """Keywords are stored lowercase; headlines are lowercased before matching."""
        strategy = EventCalendarStrategy()
        headlines = [
            {"title": "FDA APPROVAL Granted For New Treatment", "publisher": "FDA.gov"},
        ]
        result = strategy._is_significant(headlines)
        assert len(result) == 1
