"""LLM-powered news headline parser — fallback for regex-unmatched headlines.

When strategy_news.py's 65+ regex rules return "neutral" for a headline that
contains financial keywords, this module sends it to Claude for structured
extraction: tickers, event_type, sentiment, magnitude, affected_sectors.

Rate-limited to max N calls per cycle to control API cost (~$0.004/call with Sonnet).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from nexus.logger import get_logger

log = get_logger("news_llm")


_LLM_SYSTEM = """\
You are a financial news analyst. Given a headline, extract structured information.

Return ONLY valid JSON with these fields:
- "tickers": list of stock ticker symbols mentioned or affected (e.g. ["NVDA", "AMD"])
- "event_type": one of: earnings_beat, earnings_miss, upgrade, downgrade, \
guidance_raise, guidance_cut, fda_approval, fda_rejection, contract_win, \
layoffs, buyback, dividend, merger, ipo, regulation, tariff, macro, \
sector_rotation, geopolitical, analyst_action, product_launch, legal, other
- "sentiment": float from -1.0 (very bearish) to +1.0 (very bullish)
- "magnitude": float from 0.0 (trivial) to 1.0 (market-moving)
- "sectors": list of affected sectors (e.g. ["tech", "ai_infra"])

Known sectors: tech, ai_infra, fintech, defense, energy, nuclear, crypto, \
healthcare, cybersecurity, space, ev_autonomy

No markdown fences. Start with { and end with }."""

_LLM_USER = "Headline: {headline}"

# Financial keywords that suggest a headline is worth LLM parsing
_FINANCIAL_KEYWORDS = {
    "earnings", "revenue", "profit", "loss", "guidance", "forecast",
    "upgrade", "downgrade", "target", "rating", "initiate", "coverage",
    "acquisition", "merger", "buyout", "takeover", "ipo", "spac",
    "fda", "approval", "trial", "patent", "lawsuit", "sec",
    "dividend", "buyback", "repurchase", "split", "offering",
    "tariff", "sanction", "regulation", "antitrust", "ban",
    "layoff", "restructur", "recall", "breach", "hack",
    "contract", "deal", "partnership", "launch", "ai ", "chip",
    "billion", "million", "surge", "crash", "plunge", "soar", "rally",
    "beat", "miss", "blow", "warn", "cut", "raise", "hike",
}


def headline_has_financial_keywords(text: str) -> bool:
    """Check if a headline contains financial keywords worth LLM parsing."""
    lower = text.lower()
    return any(kw in lower for kw in _FINANCIAL_KEYWORDS)


class NewsLLMParser:
    """LLM-powered headline parser with per-cycle rate limiting."""

    def __init__(
        self,
        anthropic_api_key: str,
        model: str = "claude-sonnet-4-20250514",
        max_calls_per_cycle: int = 3,
    ) -> None:
        self._api_key = anthropic_api_key
        self._model = model
        self._max_calls = max_calls_per_cycle
        self._calls_this_cycle = 0
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def reset_cycle(self) -> None:
        """Reset per-cycle call counter. Call at start of each scan cycle."""
        self._calls_this_cycle = 0

    @property
    def budget_remaining(self) -> int:
        return max(0, self._max_calls - self._calls_this_cycle)

    async def parse_headline(self, headline: str) -> Optional[Dict[str, Any]]:
        """Parse a headline using Claude. Returns structured dict or None.

        Returns None if:
        - No API key configured
        - Budget exhausted for this cycle
        - Headline lacks financial keywords
        - API call fails or returns invalid JSON
        """
        if not self._api_key:
            return None

        if self._calls_this_cycle >= self._max_calls:
            log.debug("LLM news budget exhausted")
            return None

        if not headline_has_financial_keywords(headline):
            return None

        try:
            client = self._get_client()
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.messages.create,
                    model=self._model,
                    max_tokens=256,
                    system=_LLM_SYSTEM,
                    messages=[{"role": "user", "content": _LLM_USER.format(headline=headline[:300])}],
                ),
                timeout=8.0,
            )
            self._calls_this_cycle += 1

            text = response.content[0].text.strip()
            data = self._parse_response(text)

            if data:
                log.debug(
                    "LLM headline parsed",
                    tickers=data.get("tickers", []),
                    sentiment=data.get("sentiment", 0),
                    event_type=data.get("event_type", ""),
                )

            return data

        except asyncio.TimeoutError:
            log.debug("LLM news parse timed out", headline=headline[:80])
            return None
        except Exception as e:
            log.debug("LLM news parse failed", error=str(e))
            return None

    @staticmethod
    def _parse_response(text: str) -> Optional[Dict[str, Any]]:
        """Parse Claude's JSON response into a validated dict."""
        # Strip markdown fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
            text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON object from response
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1:
                return None
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None

        if not isinstance(data, dict):
            return None

        # Validate and normalize fields
        tickers = data.get("tickers", [])
        if not isinstance(tickers, list):
            tickers = []
        tickers = [str(t).upper() for t in tickers if isinstance(t, str) and 1 <= len(t) <= 5]

        event_type = str(data.get("event_type", "other"))

        sentiment = data.get("sentiment", 0.0)
        try:
            sentiment = max(-1.0, min(1.0, float(sentiment)))
        except (ValueError, TypeError):
            sentiment = 0.0

        magnitude = data.get("magnitude", 0.5)
        try:
            magnitude = max(0.0, min(1.0, float(magnitude)))
        except (ValueError, TypeError):
            magnitude = 0.5

        sectors = data.get("sectors", [])
        if not isinstance(sectors, list):
            sectors = []
        sectors = [str(s) for s in sectors if isinstance(s, str)]

        return {
            "tickers": tickers,
            "event_type": event_type,
            "sentiment": sentiment,
            "magnitude": magnitude,
            "sectors": sectors,
        }
