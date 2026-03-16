"""Event Calendar Strategy — proactive event-driven trading signals.

Checks upcoming events (earnings, product launches, FDA decisions, analyst actions)
for each ticker BEFORE the market moves, using three data layers:

1. Earnings Calendar — yfinance Ticker.calendar for upcoming earnings dates
2. News Headlines — yfinance Ticker.news for recent headlines (cached 30 min)
3. Claude AI Research — significant events are sent to Claude for directional analysis

The strategy pre-filters headlines with high-impact keyword matching to avoid
unnecessary Claude API calls, then uses AI to form a directional thesis with
conviction scoring.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd

from nexus.config import get_config
from nexus.indicators import atr
from nexus.logger import get_logger
from nexus.strategy import Signal

log = get_logger("strategy.events")


# ── News cache (module-level, shared across instances) ────────────────────────

_news_cache: Dict[str, tuple[list, float]] = {}  # {ticker: (news, timestamp)}


# ── High-impact keyword set ──────────────────────────────────────────────────

_HIGH_IMPACT_KEYWORDS = frozenset({
    "earnings", "revenue", "guidance", "forecast", "profit", "loss",
    "acquisition", "merger", "buyback", "dividend", "split",
    "fda", "approval", "clinical", "trial",
    "keynote", "conference", "gtc", "wwdc", "launch", "unveil", "announce",
    "tariff", "sanction", "ban", "regulation",
    "upgrade", "downgrade", "price target", "initiate",
    "layoff", "restructuring", "ceo", "resign",
    "beat", "miss", "surprise", "record",
})


# ── Claude AI prompt ─────────────────────────────────────────────────────────

_EVENT_PROMPT = """You are a quantitative equity analyst. Analyze these events for {ticker} \
(current price: ${price:.2f}) and provide a trading signal.

Recent headlines:
{headlines}

Respond in JSON only — no markdown, no code fences:
{{"direction": "BUY" or "SELL" or "HOLD", "score": 0.0 to 1.0, \
"reasoning": "one sentence explaining the thesis", \
"event_type": "earnings" or "product_launch" or "macro" or "analyst" or "corporate_action" or "other", \
"time_horizon": "intraday" or "swing" or "position"}}

Rules:
- Score > 0.7 = high conviction, 0.5-0.7 = moderate, < 0.5 = low
- HOLD if the event is already priced in or ambiguous
- Consider both the immediate reaction and the 1-5 day follow-through
- Be conservative — only BUY/SELL if the event clearly shifts fundamentals or sentiment"""


# ── EventCalendarStrategy ────────────────────────────────────────────────────

class EventCalendarStrategy:
    """Proactive event-driven strategy: detect upcoming catalysts and use
    Claude AI to research them and form trading theses before the market moves.
    """

    name = "events"

    _NEWS_TTL = 1800  # 30-minute cache for news headlines
    _MAX_CLAUDE_CALLS = 5  # max Claude calls per scan cycle (across all tickers)

    def __init__(self) -> None:
        cfg = get_config()
        self._cfg = cfg
        self._s = cfg.strategy
        self._r = cfg.risk
        self._client: Any = None
        self._claude_calls_this_cycle = 0
        self._cycle_reset_ts = 0.0

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._cfg.anthropic_api_key)
        return self._client

    # ── Main analyze entry point ──────────────────────────────────────────────

    async def analyze(self, ticker: str, df: pd.DataFrame) -> Optional[Signal]:
        """Analyze upcoming events for a ticker and generate a signal if warranted."""
        try:
            if df is None or len(df) < 20:
                return None

            closes = df["close"]
            highs = df["high"]
            lows = df["low"]
            last_price = float(closes.iloc[-1])

            # Reset Claude call counter every 5 minutes (approximate scan cycle)
            now = time.time()
            if now - self._cycle_reset_ts > 300:
                self._claude_calls_this_cycle = 0
                self._cycle_reset_ts = now

            # 1. Check earnings calendar
            earnings = await self._check_earnings(ticker)

            # 2. Fetch and filter news
            news = await self._fetch_news(ticker)
            significant = self._is_significant(news)

            # 3. If earnings within 3 days, add as a synthetic headline
            if earnings:
                significant.insert(0, {
                    "title": f"Earnings report expected {earnings['date']}",
                    "publisher": "calendar",
                })

            if not significant:
                return None

            # 4. Research with Claude (if API key available and budget remaining)
            research = await self._research_event(ticker, significant, last_price)
            if not research or research["direction"] == "HOLD":
                return None
            if research["score"] < self._s.min_signal_score:
                return None

            # 5. Compute ATR-based stops and targets
            atr_result = atr(highs, lows, closes, period=self._s.atr_period,
                             entry_price=last_price,
                             multiplier=self._r.atr_stop_multiplier)

            if research["direction"] == "BUY":
                stop_px = last_price - atr_result.value * 1.5
                target_px = last_price + atr_result.value * 4.5
                limit_px = last_price * 1.001
            else:
                stop_px = last_price + atr_result.value * 1.5
                target_px = last_price - atr_result.value * 4.5
                limit_px = last_price * 0.999

            return Signal(
                ticker=ticker,
                direction=research["direction"],
                score=research["score"],
                strategy=self.name,
                reasoning=f"Event: {research['reasoning']}",
                entry_price=last_price,
                stop_price=stop_px,
                target_price=target_px,
                limit_price=limit_px,
                atr_val=atr_result.value,
                catalysts=[
                    f"event_type: {research['event_type']}",
                    f"time_horizon: {research['time_horizon']}",
                ],
                risks=["event_driven: higher volatility expected"],
            )

        except Exception as e:
            log.error("Events analyze failed", ticker=ticker, error=str(e))
            return None

    # ── Earnings calendar check ──────────────────────────────────────────────

    async def _check_earnings(self, ticker: str) -> Optional[dict]:
        """Check if earnings are within the next 3 trading days."""
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            cal = await asyncio.to_thread(lambda: t.calendar)
            if cal is None:
                return None

            # yfinance calendar can be a DataFrame or a dict depending on version
            earnings_date = None
            if isinstance(cal, pd.DataFrame) and not cal.empty:
                # Look for 'Earnings Date' row or first date-like value
                if "Earnings Date" in cal.index:
                    val = cal.loc["Earnings Date"].iloc[0]
                    if hasattr(val, "date"):
                        earnings_date = val.date()
                    elif isinstance(val, str):
                        try:
                            earnings_date = datetime.fromisoformat(val).date()
                        except ValueError:
                            pass
            elif isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if ed:
                    if isinstance(ed, list) and len(ed) > 0:
                        ed = ed[0]
                    if hasattr(ed, "date"):
                        earnings_date = ed.date()
                    elif isinstance(ed, str):
                        try:
                            earnings_date = datetime.fromisoformat(ed).date()
                        except ValueError:
                            pass

            if earnings_date is None:
                return None

            today = datetime.now(timezone.utc).date()
            days_until = (earnings_date - today).days
            if 0 <= days_until <= 3:
                return {
                    "event": "earnings",
                    "date": earnings_date.isoformat(),
                    "days_until": days_until,
                }
        except Exception as e:
            log.debug("Earnings calendar check failed", ticker=ticker, error=str(e))
        return None

    # ── News headline fetch (cached) ─────────────────────────────────────────

    async def _fetch_news(self, ticker: str) -> list[dict]:
        """Fetch recent news via yfinance. Cached for 30 minutes."""
        now = time.time()
        cached = _news_cache.get(ticker)
        if cached and (now - cached[1]) < self._NEWS_TTL:
            return cached[0]

        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            news = await asyncio.to_thread(lambda: t.news)

            # Normalize: yfinance .news returns list of dicts with 'title', 'publisher', etc.
            normalized: list[dict] = []
            if news:
                for item in news:
                    if isinstance(item, dict):
                        normalized.append(item)

            _news_cache[ticker] = (normalized, now)
            return normalized
        except Exception as e:
            log.debug("News fetch failed", ticker=ticker, error=str(e))
            _news_cache[ticker] = ([], now)
            return []

    # ── Significance filter ──────────────────────────────────────────────────

    def _is_significant(self, headlines: list[dict]) -> list[dict]:
        """Filter headlines that contain high-impact keywords.

        Returns at most 5 significant headlines to limit Claude API costs.
        """
        significant: list[dict] = []
        for h in headlines:
            title = h.get("title", "").lower()
            if any(kw in title for kw in _HIGH_IMPACT_KEYWORDS):
                significant.append(h)
        return significant[:5]

    # ── Claude AI research ───────────────────────────────────────────────────

    async def _research_event(
        self,
        ticker: str,
        headlines: list[dict],
        current_price: float,
    ) -> Optional[dict]:
        """Send significant events to Claude for directional analysis.

        Returns a dict with keys: direction, score, reasoning, event_type, time_horizon.
        Returns None if no API key or budget exhausted.
        """
        if not self._cfg.anthropic_api_key:
            return None

        if self._claude_calls_this_cycle >= self._MAX_CLAUDE_CALLS:
            log.debug("Claude call budget exhausted for this cycle", ticker=ticker)
            return None

        headline_text = "\n".join(
            f"- {h.get('title', '(no title)')} ({h.get('publisher', 'unknown')})"
            for h in headlines
        )

        prompt = _EVENT_PROMPT.format(
            ticker=ticker,
            price=current_price,
            headlines=headline_text,
        )

        try:
            client = self._get_client()
            response = await asyncio.to_thread(
                client.messages.create,
                model=self._cfg.ai_model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            self._claude_calls_this_cycle += 1

            raw = response.content[0].text.strip()
            parsed = json.loads(raw)

            # Validate and clamp
            direction = str(parsed.get("direction", "HOLD")).upper()
            if direction not in ("BUY", "SELL", "HOLD"):
                direction = "HOLD"

            score = max(0.0, min(float(parsed.get("score", 0.5)), 1.0))
            reasoning = str(parsed.get("reasoning", ""))[:200]

            event_type = str(parsed.get("event_type", "other"))
            if event_type not in (
                "earnings", "product_launch", "macro",
                "analyst", "corporate_action", "other",
            ):
                event_type = "other"

            time_horizon = str(parsed.get("time_horizon", "swing"))
            if time_horizon not in ("intraday", "swing", "position"):
                time_horizon = "swing"

            log.info("Event research complete", ticker=ticker,
                     direction=direction, score=f"{score:.2f}",
                     event_type=event_type)

            return {
                "direction": direction,
                "score": score,
                "reasoning": reasoning,
                "event_type": event_type,
                "time_horizon": time_horizon,
            }

        except json.JSONDecodeError:
            log.error("Event research: bad JSON from Claude", ticker=ticker)
            return None
        except Exception as e:
            log.error("Event research failed", ticker=ticker, error=str(e))
            return None
