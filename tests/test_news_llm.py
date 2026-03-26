"""Tests for the LLM news headline parser."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

from nexus.news_llm import NewsLLMParser, headline_has_financial_keywords


class TestFinancialKeywords:
    def test_earnings_keyword(self):
        assert headline_has_financial_keywords("NVDA earnings beat estimates") is True

    def test_tariff_keyword(self):
        assert headline_has_financial_keywords("New tariff on Chinese chips") is True

    def test_no_keywords(self):
        assert headline_has_financial_keywords("Weather is nice today") is False

    def test_fda_keyword(self):
        assert headline_has_financial_keywords("FDA approval for new drug") is True

    def test_merger_keyword(self):
        assert headline_has_financial_keywords("Company announces merger deal") is True

    def test_case_insensitive(self):
        assert headline_has_financial_keywords("EARNINGS BEAT expectations") is True


class TestNewsLLMParser:
    def test_no_api_key(self):
        parser = NewsLLMParser(anthropic_api_key="")
        result = asyncio.get_event_loop().run_until_complete(
            parser.parse_headline("NVDA earnings beat")
        )
        assert result is None

    def test_budget_exhausted(self):
        parser = NewsLLMParser(anthropic_api_key="test-key", max_calls_per_cycle=0)
        result = asyncio.get_event_loop().run_until_complete(
            parser.parse_headline("NVDA earnings beat")
        )
        assert result is None

    def test_no_financial_keywords(self):
        parser = NewsLLMParser(anthropic_api_key="test-key")
        result = asyncio.get_event_loop().run_until_complete(
            parser.parse_headline("Weather is nice today")
        )
        assert result is None

    def test_budget_tracking(self):
        parser = NewsLLMParser(anthropic_api_key="test-key", max_calls_per_cycle=2)
        assert parser.budget_remaining == 2

        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text=json.dumps({
            "tickers": ["NVDA"],
            "event_type": "earnings_beat",
            "sentiment": 0.8,
            "magnitude": 0.7,
            "sectors": ["tech", "ai_infra"],
        }))]

        with patch("nexus.news_llm.asyncio.to_thread") as mock_thread:
            async def fake_thread(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_thread.side_effect = fake_thread

            with patch.object(parser, "_get_client") as mock_client:
                mock_client.return_value.messages.create = MagicMock(return_value=mock_resp)

                asyncio.get_event_loop().run_until_complete(
                    parser.parse_headline("NVDA earnings beat estimates")
                )
                assert parser.budget_remaining == 1

    def test_reset_cycle(self):
        parser = NewsLLMParser(anthropic_api_key="test-key", max_calls_per_cycle=2)
        parser._calls_this_cycle = 2
        assert parser.budget_remaining == 0
        parser.reset_cycle()
        assert parser.budget_remaining == 2

    def test_successful_parse(self):
        parser = NewsLLMParser(anthropic_api_key="test-key")

        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text=json.dumps({
            "tickers": ["NVDA", "AMD"],
            "event_type": "earnings_beat",
            "sentiment": 0.8,
            "magnitude": 0.7,
            "sectors": ["tech", "ai_infra"],
        }))]

        with patch("nexus.news_llm.asyncio.to_thread") as mock_thread:
            async def fake_thread(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_thread.side_effect = fake_thread

            with patch.object(parser, "_get_client") as mock_client:
                mock_client.return_value.messages.create = MagicMock(return_value=mock_resp)

                result = asyncio.get_event_loop().run_until_complete(
                    parser.parse_headline("NVDA earnings beat estimates massively")
                )

        assert result is not None
        assert "NVDA" in result["tickers"]
        assert "AMD" in result["tickers"]
        assert result["event_type"] == "earnings_beat"
        assert result["sentiment"] == 0.8
        assert result["magnitude"] == 0.7
        assert "tech" in result["sectors"]

    def test_parse_response_valid_json(self):
        result = NewsLLMParser._parse_response(json.dumps({
            "tickers": ["AAPL"],
            "event_type": "upgrade",
            "sentiment": 0.6,
            "magnitude": 0.5,
            "sectors": ["tech"],
        }))
        assert result is not None
        assert result["tickers"] == ["AAPL"]
        assert result["sentiment"] == 0.6

    def test_parse_response_markdown_fences(self):
        raw = "```json\n" + json.dumps({
            "tickers": ["TSLA"],
            "event_type": "product_launch",
            "sentiment": 0.7,
            "magnitude": 0.6,
            "sectors": ["ev_autonomy"],
        }) + "\n```"
        result = NewsLLMParser._parse_response(raw)
        assert result is not None
        assert result["tickers"] == ["TSLA"]

    def test_parse_response_invalid_json(self):
        result = NewsLLMParser._parse_response("This is not JSON")
        assert result is None

    def test_parse_response_clamps_sentiment(self):
        result = NewsLLMParser._parse_response(json.dumps({
            "tickers": ["NVDA"],
            "event_type": "other",
            "sentiment": 5.0,
            "magnitude": -2.0,
            "sectors": [],
        }))
        assert result is not None
        assert result["sentiment"] == 1.0
        assert result["magnitude"] == 0.0

    def test_parse_response_filters_invalid_tickers(self):
        result = NewsLLMParser._parse_response(json.dumps({
            "tickers": ["NVDA", "", "TOOLONG123", 123, "OK"],
            "event_type": "other",
            "sentiment": 0.0,
            "magnitude": 0.5,
            "sectors": [],
        }))
        assert result is not None
        assert result["tickers"] == ["NVDA", "OK"]

    def test_timeout_returns_none(self):
        parser = NewsLLMParser(anthropic_api_key="test-key")

        with patch("nexus.news_llm.asyncio.to_thread") as mock_thread:
            async def slow_thread(fn, *args, **kwargs):
                await asyncio.sleep(100)
                return fn(*args, **kwargs)

            mock_thread.side_effect = slow_thread

            with patch.object(parser, "_get_client") as mock_client:
                mock_client.return_value.messages.create = MagicMock()

                result = asyncio.get_event_loop().run_until_complete(
                    parser.parse_headline("NVDA earnings beat big time")
                )

        assert result is None


class TestNewsStrategyLLMIntegration:
    def test_set_llm_parser(self):
        from nexus.strategy_news import NewsSentimentStrategy

        strategy = NewsSentimentStrategy()
        assert strategy._llm_parser is None

        parser = NewsLLMParser(anthropic_api_key="test-key")
        strategy.set_llm_parser(parser)
        assert strategy._llm_parser is parser
