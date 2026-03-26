"""ReACT market analysis agent — tool-using LLM for pre-trade analysis.

Inspired by MiroFish's ReportAgent: uses Thought → Action → Observation
loops to gather data and produce structured market analysis reports.

Runs async after signal execution (non-blocking). Reports are stored
and served via the dashboard API.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from nexus.logger import get_logger

log = get_logger("react_agent")


# ── Output dataclass ─────────────────────────────────────────────────────────


@dataclass
class MarketAnalysis:
    ticker: str
    thesis: str
    confidence: float
    key_factors: List[str]
    risk_factors: List[str]
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "thesis": self.thesis,
            "confidence": self.confidence,
            "key_factors": self.key_factors,
            "risk_factors": self.risk_factors,
            "tool_calls": self.tool_calls,
            "generated_at": self.generated_at,
        }


# ── Tool Registry ────────────────────────────────────────────────────────────


class ToolRegistry:
    """Registry of tools the ReACT agent can call."""

    def __init__(self) -> None:
        self._tools: Dict[str, Callable] = {}
        self._descriptions: Dict[str, str] = {}

    def register(self, name: str, fn: Callable, description: str) -> None:
        self._tools[name] = fn
        self._descriptions[name] = description

    async def call(self, name: str, **kwargs) -> str:
        """Call a tool by name. Returns stringified result."""
        fn = self._tools.get(name)
        if fn is None:
            return f"Unknown tool: {name}"
        try:
            result = fn(**kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, (dict, list)):
                return json.dumps(result, default=str, indent=2)
            return str(result)
        except Exception as e:
            return f"Tool error: {e}"

    def get_descriptions(self) -> str:
        """Format tool descriptions for the LLM prompt."""
        parts = []
        for name, desc in self._descriptions.items():
            parts.append(f"- {name}: {desc}")
        return "\n".join(parts)

    @property
    def tool_names(self) -> List[str]:
        return list(self._tools.keys())


# ── Built-in tools ───────────────────────────────────────────────────────────


def create_default_tools(broker: Any = None, tracker: Any = None) -> ToolRegistry:
    """Create a ToolRegistry with default market analysis tools."""
    registry = ToolRegistry()

    def get_technicals(ticker: str) -> dict:
        """Compute technical indicators for a ticker using cached price data."""
        from nexus.indicators import atr, bollinger_bands, macd, rsi, sma, volume_ratio

        # Try to get data from broker
        # This is a sync wrapper — the agent calls it within asyncio.to_thread
        import yfinance as yf

        df = yf.download(ticker, period="6mo", interval="1d", progress=False)
        if df is None or len(df) < 60:
            return {"error": f"Insufficient data for {ticker}"}

        closes = df["Close"].squeeze()
        highs = df["High"].squeeze()
        lows = df["Low"].squeeze()
        volumes = df["Volume"].squeeze()

        rsi_r = rsi(closes)
        macd_r = macd(closes)
        bb_r = bollinger_bands(closes)
        atr_r = atr(highs, lows, closes, entry_price=float(closes.iloc[-1]))
        sma_20 = sma(closes, 20)
        sma_50 = sma(closes, 50)
        vol_r = volume_ratio(volumes)

        return {
            "ticker": ticker,
            "price": round(float(closes.iloc[-1]), 2),
            "rsi": round(rsi_r.value, 1),
            "rsi_oversold": rsi_r.oversold,
            "rsi_overbought": rsi_r.overbought,
            "macd_histogram": round(macd_r.histogram, 4),
            "macd_bullish_cross": macd_r.bullish_cross,
            "bb_pct_b": round(bb_r.pct_b, 2),
            "bb_below_lower": bb_r.below_lower,
            "bb_above_upper": bb_r.above_upper,
            "atr": round(atr_r.value, 2),
            "sma_20": round(sma_20, 2) if sma_20 else None,
            "sma_50": round(sma_50, 2) if sma_50 else None,
            "volume_ratio": round(vol_r, 2),
        }

    def get_trade_history(ticker: str) -> dict:
        """Get past trade performance for a ticker from the tracker."""
        if tracker is None:
            return {"error": "No tracker available"}
        trades = tracker.get_closed_trades(500)
        ticker_trades = [t for t in trades if t["ticker"] == ticker]
        if not ticker_trades:
            return {"ticker": ticker, "total_trades": 0}
        wins = sum(1 for t in ticker_trades if (t.get("pnl") or 0) > 0)
        total_pnl = sum(t.get("pnl") or 0 for t in ticker_trades)
        return {
            "ticker": ticker,
            "total_trades": len(ticker_trades),
            "wins": wins,
            "losses": len(ticker_trades) - wins,
            "win_rate": round(wins / len(ticker_trades), 2) if ticker_trades else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(ticker_trades), 2) if ticker_trades else 0,
        }

    def get_sector_context(ticker: str) -> dict:
        """Get sector info for a ticker."""
        # Simplified sector mapping
        SECTORS = {
            "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
            "GOOGL": "Technology", "META": "Technology", "AMD": "Technology",
            "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
            "CRM": "Technology", "NFLX": "Communication Services",
            "JPM": "Financials", "BAC": "Financials", "GS": "Financials",
            "XOM": "Energy", "CVX": "Energy", "JNJ": "Healthcare",
            "UNH": "Healthcare", "PFE": "Healthcare",
        }
        sector = SECTORS.get(ticker, "Unknown")
        peers = [t for t, s in SECTORS.items() if s == sector and t != ticker]
        return {"ticker": ticker, "sector": sector, "peers": peers[:5]}

    registry.register("get_technicals", get_technicals, "Get RSI, MACD, BB, ATR, SMA, volume for a ticker")
    registry.register("get_trade_history", get_trade_history, "Get past trade win rate and P&L for a ticker")
    registry.register("get_sector_context", get_sector_context, "Get sector and peer tickers")

    return registry


# ── ReACT Agent ──────────────────────────────────────────────────────────────

_REACT_SYSTEM = """\
You are a market analysis agent. You analyze trading signals using available tools.

Use Thought → Action → Observation loops:
1. Think about what data you need
2. Call ONE tool per turn
3. Analyze the result
4. Repeat (max {max_calls} tool calls total)
5. When done, output your final analysis

Available tools:
{tools}

To call a tool, output EXACTLY:
<tool_call>{{"name": "tool_name", "params": {{"param": "value"}}}}</tool_call>

When you have enough information, output:
<final_analysis>
{{"thesis": "1-2 sentence summary", "confidence": 0.0-1.0, "key_factors": ["..."], "risk_factors": ["..."]}}
</final_analysis>"""

_REACT_USER = """\
Analyze this signal:
Ticker: {ticker} | Direction: {direction} | Score: {score:.2f}
Strategy: {strategy}
Reasoning: {reasoning}
Entry: ${entry:.2f} | Stop: ${stop:.2f} | Target: ${target:.2f}

Start by calling a tool to gather data."""


class ReACTAgent:
    """Tool-using LLM agent for pre-trade market analysis."""

    MAX_TOOL_CALLS = 3
    MAX_ITERATIONS = 6  # safety cap on total LLM turns

    def __init__(
        self,
        anthropic_api_key: str,
        model: str = "claude-sonnet-4-20250514",
        tools: ToolRegistry | None = None,
    ) -> None:
        self._api_key = anthropic_api_key
        self._model = model
        self._tools = tools or ToolRegistry()
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    async def analyze(self, signal: Any) -> Optional[MarketAnalysis]:
        """Run ReACT loop to produce a MarketAnalysis for a signal."""
        if not self._api_key:
            return None

        from datetime import datetime, timezone

        from nexus.strategy import Signal

        sig: Signal = signal

        system = _REACT_SYSTEM.format(
            max_calls=self.MAX_TOOL_CALLS,
            tools=self._tools.get_descriptions(),
        )
        user = _REACT_USER.format(
            ticker=sig.ticker,
            direction=sig.direction,
            score=sig.score,
            strategy=sig.strategy,
            reasoning=sig.reasoning[:200],
            entry=sig.entry_price,
            stop=sig.stop_price,
            target=sig.target_price,
        )

        messages = [{"role": "user", "content": user}]
        tool_calls_log: List[Dict[str, Any]] = []
        tool_call_count = 0

        for iteration in range(self.MAX_ITERATIONS):
            try:
                client = self._get_client()
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.messages.create,
                        model=self._model,
                        max_tokens=1024,
                        system=system,
                        messages=messages,
                    ),
                    timeout=15.0,
                )
            except Exception as e:
                log.warning("ReACT LLM call failed", ticker=sig.ticker, error=str(e))
                return None

            text = response.content[0].text.strip()

            # Check for final analysis
            if "<final_analysis>" in text:
                return self._parse_final(text, sig.ticker, tool_calls_log)

            # Check for tool call
            import re

            tc_match = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.DOTALL)
            if tc_match and tool_call_count < self.MAX_TOOL_CALLS:
                try:
                    call_data = json.loads(tc_match.group(1))
                    tool_name = call_data.get("name", "")
                    params = call_data.get("params", {})

                    result = await self._tools.call(tool_name, **params)
                    tool_calls_log.append({"tool": tool_name, "params": params, "result_length": len(result)})
                    tool_call_count += 1

                    messages.append({"role": "assistant", "content": text})
                    messages.append({
                        "role": "user",
                        "content": f"Observation ({tool_call_count}/{self.MAX_TOOL_CALLS} calls used):\n{result}\n\n"
                        + ("Now output <final_analysis> with your conclusion." if tool_call_count >= self.MAX_TOOL_CALLS else "Continue analysis or output <final_analysis> when ready."),
                    })
                except (json.JSONDecodeError, Exception) as e:
                    log.debug("ReACT tool parse failed", error=str(e))
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": f"Tool call failed: {e}. Try again or output <final_analysis>."})
            else:
                # No tool call and no final — force conclusion
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": "Please output your <final_analysis> now."})

        log.warning("ReACT agent hit max iterations", ticker=sig.ticker)
        return None

    def _parse_final(
        self, text: str, ticker: str, tool_calls: List[Dict]
    ) -> Optional[MarketAnalysis]:
        """Extract MarketAnalysis from <final_analysis> block."""
        import re
        from datetime import datetime, timezone

        match = re.search(r"<final_analysis>\s*(\{.*?\})\s*</final_analysis>", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(1))
            return MarketAnalysis(
                ticker=ticker,
                thesis=str(data.get("thesis", ""))[:500],
                confidence=max(0.0, min(1.0, float(data.get("confidence", 0.5)))),
                key_factors=data.get("key_factors", [])[:10],
                risk_factors=data.get("risk_factors", [])[:10],
                tool_calls=tool_calls,
                generated_at=datetime.now(timezone.utc).isoformat(),
            )
        except (json.JSONDecodeError, ValueError):
            return None
