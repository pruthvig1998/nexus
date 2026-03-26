"""Tests for the ReACT market analysis agent."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

from nexus.react_agent import MarketAnalysis, ReACTAgent, ToolRegistry
from nexus.strategy import Signal


def _make_signal() -> Signal:
    return Signal(
        ticker="NVDA", direction="BUY", score=0.80, strategy="momentum",
        reasoning="RSI oversold; MACD bullish cross",
        entry_price=120.0, stop_price=115.0, target_price=135.0,
    )


class TestToolRegistry:
    def test_register_and_call(self):
        reg = ToolRegistry()
        reg.register("test_tool", lambda x: f"result:{x}", "A test tool")
        result = asyncio.get_event_loop().run_until_complete(reg.call("test_tool", x="hello"))
        assert result == "result:hello"

    def test_unknown_tool(self):
        reg = ToolRegistry()
        result = asyncio.get_event_loop().run_until_complete(reg.call("nonexistent"))
        assert "Unknown tool" in result

    def test_tool_error_handled(self):
        reg = ToolRegistry()

        def bad_tool():
            raise ValueError("broken")

        reg.register("bad", bad_tool, "A broken tool")
        result = asyncio.get_event_loop().run_until_complete(reg.call("bad"))
        assert "Tool error" in result

    def test_descriptions(self):
        reg = ToolRegistry()
        reg.register("t1", lambda: None, "Tool one")
        reg.register("t2", lambda: None, "Tool two")
        desc = reg.get_descriptions()
        assert "t1" in desc
        assert "Tool one" in desc

    def test_tool_names(self):
        reg = ToolRegistry()
        reg.register("alpha", lambda: None, "A")
        reg.register("beta", lambda: None, "B")
        assert reg.tool_names == ["alpha", "beta"]


class TestMarketAnalysis:
    def test_to_dict(self):
        analysis = MarketAnalysis(
            ticker="NVDA", thesis="Strong momentum setup",
            confidence=0.85, key_factors=["RSI oversold"],
            risk_factors=["Earnings in 5 days"],
            generated_at="2026-03-26T12:00:00Z",
        )
        d = analysis.to_dict()
        assert d["ticker"] == "NVDA"
        assert d["confidence"] == 0.85
        assert len(d["key_factors"]) == 1


class TestReACTAgent:
    def test_no_api_key_returns_none(self):
        agent = ReACTAgent(anthropic_api_key="")
        sig = _make_signal()
        result = asyncio.get_event_loop().run_until_complete(agent.analyze(sig))
        assert result is None

    def test_successful_analysis(self):
        agent = ReACTAgent(anthropic_api_key="test-key")

        # First response: tool call
        tool_response = MagicMock()
        tool_response.content = [MagicMock(text='Thinking about NVDA...\n<tool_call>{"name": "get_sector_context", "params": {"ticker": "NVDA"}}</tool_call>')]

        # Second response: final analysis
        final_response = MagicMock()
        final_response.content = [MagicMock(text='''Based on the data:
<final_analysis>
{"thesis": "Strong momentum with sector tailwinds", "confidence": 0.82, "key_factors": ["Oversold RSI", "Bullish MACD"], "risk_factors": ["High VIX"]}
</final_analysis>''')]

        call_count = [0]

        def mock_create(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return tool_response
            return final_response

        # Register a simple tool
        agent._tools.register("get_sector_context", lambda ticker: {"sector": "Technology", "peers": ["AMD", "INTC"]}, "Get sector")

        with patch("nexus.react_agent.asyncio.to_thread") as mock_thread:
            async def fake_thread(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_thread.side_effect = fake_thread

            with patch.object(agent, "_get_client") as mock_client:
                mock_client.return_value.messages.create = mock_create

                sig = _make_signal()
                result = asyncio.get_event_loop().run_until_complete(agent.analyze(sig))

        assert result is not None
        assert result.ticker == "NVDA"
        assert result.confidence == 0.82
        assert len(result.key_factors) == 2
        assert len(result.tool_calls) == 1

    def test_parse_final(self):
        agent = ReACTAgent(anthropic_api_key="test-key")
        text = '''Here is my analysis:
<final_analysis>
{"thesis": "Bearish reversal likely", "confidence": 0.65, "key_factors": ["Overbought RSI"], "risk_factors": ["Strong trend"]}
</final_analysis>'''
        result = agent._parse_final(text, "AAPL", [])
        assert result is not None
        assert result.ticker == "AAPL"
        assert result.thesis == "Bearish reversal likely"
        assert result.confidence == 0.65

    def test_parse_final_invalid_json(self):
        agent = ReACTAgent(anthropic_api_key="test-key")
        text = "<final_analysis>not json</final_analysis>"
        result = agent._parse_final(text, "AAPL", [])
        assert result is None
