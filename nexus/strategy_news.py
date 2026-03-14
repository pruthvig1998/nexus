"""News Sentiment Strategy — generate trading signals from headline analysis.

Processes news headlines from Discord embed feeds (ZeroHedge, FinancialJuice, etc.)
and detects:
  1. Macro events (CPI, Fed, NFP, tariffs, geopolitics)
  2. Company-specific news (earnings beats/misses, upgrades/downgrades)
  3. Sector rotation signals (growth↔value, flight-to-safety)

Headlines are queued via add_headline() and checked against tickers when analyze()
is called by the engine.
"""
from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from nexus.logger import get_logger
from nexus.strategy import Signal

log = get_logger("strategy.news")


# ── Sector / ticker mappings ────────────────────────────────────────────────

SECTOR_MAP: Dict[str, List[str]] = {
    "tech": ["AAPL", "MSFT", "GOOGL", "META", "NVDA", "AMD", "AVGO", "TSM", "CRM", "ORCL"],
    "ai_infra": ["NVDA", "AMD", "TSM", "ASML", "AVGO", "CRDO", "ANET", "MU"],
    "fintech": ["SOFI", "HOOD", "OPEN", "PYPL", "GRAB"],
    "defense": ["LMT", "RTX", "NOC", "GD", "KTOS"],
    "energy": ["XOM", "CVX", "COP", "SLB", "OXY"],
    "nuclear": ["OKLO", "LEU", "SMR", "NEE", "UUUU"],
    "crypto": ["COIN", "MARA", "RIOT", "BITF", "CIFR"],
    "healthcare": ["UNH", "OSCR", "HIMS", "BSX", "NVO"],
    "cybersecurity": ["PANW", "CRWD", "FTNT"],
    "space": ["RKLB", "PL", "ASTS"],
    "ev_autonomy": ["TSLA", "PONY", "JOBY", "ACHR"],
}

# Reverse lookup: ticker -> set of sectors it belongs to
_TICKER_SECTORS: Dict[str, Set[str]] = {}
for _sector, _tickers in SECTOR_MAP.items():
    for _t in _tickers:
        _TICKER_SECTORS.setdefault(_t, set()).add(_sector)

# All known tickers across every sector (for headline extraction)
_ALL_TICKERS: Set[str] = set()
for _tickers in SECTOR_MAP.values():
    _ALL_TICKERS.update(_tickers)

# ETFs and indices commonly referenced in macro headlines
_INDEX_TICKERS = {"SPY", "QQQ", "DIA", "IWM", "XLF", "XLE", "XLK", "XLV",
                  "GLD", "SLV", "TLT", "VIX", "USO", "UNG"}
_ALL_TICKERS.update(_INDEX_TICKERS)


# ── Macro event rules ───────────────────────────────────────────────────────

# Each rule: (pattern, affected_tickers, direction, score, event_type)
MacroRule = Tuple[re.Pattern, List[str], str, float, str]

_MACRO_RULES: List[MacroRule] = [
    # CPI / Inflation
    (re.compile(r"cpi\s+(below|under|lower|miss|soft|cool)", re.I),
     ["SPY", "QQQ"], "BUY", 0.70, "cpi_soft"),
    (re.compile(r"inflation\s+(under\s+target|cool|deceler|fall)", re.I),
     ["SPY", "QQQ"], "BUY", 0.70, "cpi_soft"),
    (re.compile(r"cpi\s+(above|over|higher|beat|hot|surge)", re.I),
     ["SPY", "QQQ"], "SELL", 0.70, "cpi_hot"),
    (re.compile(r"inflation\s+(hot|accelerat|surge|spike|above)", re.I),
     ["SPY", "QQQ"], "SELL", 0.70, "cpi_hot"),

    # Fed
    (re.compile(r"(fed|fomc).{0,30}(rate\s+cut|cut\s+rate|dovish|easing)", re.I),
     ["SOFI", "OPEN", "XLF", "SPY"], "BUY", 0.68, "fed_dovish"),
    (re.compile(r"(fed|fomc).{0,30}(rate\s+hike|hike\s+rate|hawkish|tighten)", re.I),
     ["SOFI", "OPEN", "XLF", "TLT"], "SELL", 0.68, "fed_hawkish"),

    # NFP / Jobs
    (re.compile(r"(nfp|non.?farm|payroll|jobs).{0,30}(beat|strong|surge|blowout)", re.I),
     ["SPY"], "BUY", 0.65, "nfp_strong"),
    (re.compile(r"(nfp|non.?farm|payroll|jobs).{0,30}(miss|weak|disappoint|soft)", re.I),
     ["SPY"], "SELL", 0.65, "nfp_weak"),

    # Trade / tariffs
    (re.compile(r"(china|chinese).{0,40}(tariff|trade\s+war|retaliat|sanction|ban)", re.I),
     ["AAPL", "NVDA", "TSM", "AVGO", "AMD", "QQQ"], "SELL", 0.65, "china_tariff"),
    (re.compile(r"tariff.{0,30}(escalat|increas|new|raise|impose)", re.I),
     ["SPY", "QQQ"], "SELL", 0.65, "tariff_escalation"),

    # Oil / OPEC
    (re.compile(r"(opec|saudi).{0,30}(cut|reduc|curb|slash)\s*(production|output|supply)", re.I),
     ["XLE", "XOM", "CVX", "COP", "SLB", "OXY", "USO"], "BUY", 0.65, "opec_cut"),
    (re.compile(r"oil.{0,20}(spike|surge|rally|soar)", re.I),
     ["XLE", "XOM", "CVX", "COP", "SLB", "OXY"], "BUY", 0.63, "oil_rally"),

    # Geopolitical
    (re.compile(r"(war|invasion|missile|strike|bomb|escalat).{0,30}(ukraine|russia|iran|israel|gaza|taiwan)", re.I),
     ["LMT", "RTX", "NOC", "GD", "KTOS", "GLD"], "BUY", 0.60, "geopolitical"),
    (re.compile(r"(ukraine|russia|iran|israel|gaza|taiwan).{0,30}(war|invasion|missile|strike|bomb|escalat)", re.I),
     ["LMT", "RTX", "NOC", "GD", "KTOS", "GLD"], "BUY", 0.60, "geopolitical"),
    (re.compile(r"(sanction|embargo).{0,30}(russia|iran|china)", re.I),
     ["SPY"], "SELL", 0.60, "sanctions"),
]


# ── Company-specific sentiment patterns ──────────────────────────────────────

_BULLISH_PATTERNS = [
    re.compile(r"beat[s]?\s+(estimate|expect|consensus|forecast)", re.I),
    re.compile(r"raise[sd]?\s+guidance", re.I),
    re.compile(r"record\s+(revenue|earnings|profit|quarter)", re.I),
    re.compile(r"(upgrade[sd]?|price\s+target\s+raise[sd]?)", re.I),
    re.compile(r"(strong|blow.?out|stellar|robust)\s+(quarter|earnings|results)", re.I),
    re.compile(r"(initiate|start)[sd]?\s+(with\s+)?buy", re.I),
    re.compile(r"(buyback|repurchas|share\s+repurchas)", re.I),
    re.compile(r"(dividend\s+(hike|increase|raise)|special\s+dividend)", re.I),
    re.compile(r"(FDA\s+approv|breakthrough\s+designation)", re.I),
    re.compile(r"(contract\s+win|awarded\s+contract)", re.I),
]

_BEARISH_PATTERNS = [
    re.compile(r"miss(es|ed)?\s+(estimate|expect|consensus|forecast)", re.I),
    re.compile(r"(cut|lower|slash|reduce)[sd]?\s+guidance", re.I),
    re.compile(r"(investigation|probe|lawsuit|sued|SEC\s+charge)", re.I),
    re.compile(r"(downgrade[sd]?|price\s+target\s+(cut|lower|reduce))", re.I),
    re.compile(r"(weak|disappointing|soft|poor)\s+(quarter|earnings|results)", re.I),
    re.compile(r"(initiate|start)[sd]?\s+(with\s+)?sell", re.I),
    re.compile(r"(layoff|restructur|headcount\s+cut|workforce\s+reduction)", re.I),
    re.compile(r"(recall|safety\s+concern|warning\s+letter)", re.I),
    re.compile(r"(data\s+breach|hack|cyber\s+attack)", re.I),
    re.compile(r"(delisted|delist|going\s+concern)", re.I),
]


# ── Sector rotation patterns ────────────────────────────────────────────────

_ROTATION_RULES = [
    # (pattern, buy_sectors, sell_sectors)
    (re.compile(r"rotation\s+(into|toward)\s+(value|defensive|safe)", re.I),
     ["healthcare", "energy"], ["tech", "ai_infra"]),
    (re.compile(r"rotation\s+(into|toward)\s+(growth|tech|risk)", re.I),
     ["tech", "ai_infra"], ["healthcare", "energy"]),
    (re.compile(r"flight\s+to\s+(safety|quality)", re.I),
     [], []),  # special case: buy GLD/TLT, sell equities
    (re.compile(r"risk[\s-]?off", re.I),
     [], []),  # special case: buy GLD/TLT, sell equities
    (re.compile(r"risk[\s-]?on", re.I),
     ["tech", "ai_infra", "fintech"], []),
]


# ── Headline parser ─────────────────────────────────────────────────────────

# Pattern to find tickers in headlines: $AAPL or (AAPL) or standalone AAPL
_TICKER_RE = re.compile(r'(?:\$([A-Z]{1,5})|\b([A-Z]{2,5})\b)')

# Common English words that look like tickers but are not
_TICKER_BLACKLIST = {
    "A", "I", "AM", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "HE", "IF",
    "IN", "IS", "IT", "ME", "MY", "NO", "OF", "OK", "ON", "OR", "SO", "TO",
    "UP", "US", "WE", "CEO", "CFO", "CPI", "GDP", "IPO", "NFP", "SEC", "ETF",
    "THE", "FOR", "AND", "BUT", "NOT", "ARE", "WAS", "HAS", "HAD", "NEW",
    "NOW", "ALL", "CAN", "MAY", "SAY", "ITS", "OUT", "OUR", "WHO", "HOW",
    "BIG", "OLD", "TOP", "LOW", "OIL", "CUT", "SET", "HIT", "WAR", "FED",
    "GDP", "RED", "RUN", "PUT", "GET", "SAW", "TWO", "DAY", "KEY", "HIGH",
    "JUST", "FROM", "THIS", "THAT", "WITH", "WILL", "HAVE", "BEEN", "SAYS",
    "OVER", "INTO", "ALSO", "MORE", "MOST", "SOME", "THAN", "LIKE", "BACK",
    "VERY", "MUCH", "NEAR", "NEXT", "LAST", "FULL", "DOWN", "RATE", "OPEC",
    "FOMC", "DATA", "MISS", "BEAT", "LONG", "CALL", "SELL",
}


def _extract_tickers(text: str) -> List[str]:
    """Extract plausible stock tickers from headline text."""
    found: List[str] = []
    for dollar_match, bare_match in _TICKER_RE.findall(text):
        candidate = dollar_match or bare_match
        if candidate in _TICKER_BLACKLIST:
            continue
        if candidate in _ALL_TICKERS:
            found.append(candidate)
        elif dollar_match:
            # $-prefixed tickers are very likely real even if not in our map
            found.append(candidate)
    return list(dict.fromkeys(found))  # deduplicate preserving order


# ── NewsSentimentStrategy ────────────────────────────────────────────────────

class NewsSentimentStrategy:
    """Analyze queued news headlines and generate signals for affected tickers.

    Usage:
        strategy = NewsSentimentStrategy()
        strategy.add_headline("CPI comes in below estimates", source="zerohedge")
        signal = await strategy.analyze("SPY", df)
    """
    name = "news_sentiment"

    def __init__(self) -> None:
        self._headlines: deque[dict] = deque(maxlen=500)
        self._headline_expiry = timedelta(hours=2)

    # ── Public API ───────────────────────────────────────────────────────────

    def add_headline(self, text: str, source: str = "", timestamp: str = "") -> None:
        """Called by Discord feed when embed headlines are received.

        Args:
            text: The headline text.
            source: Feed source (e.g. "zerohedge", "financialjuice").
            timestamp: ISO-format timestamp; defaults to now (UTC).
        """
        if not text or not text.strip():
            return

        if timestamp:
            try:
                ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        parsed = self.parse_headline(text)
        entry = {
            "text": text.strip(),
            "source": source,
            "timestamp": ts,
            "parsed": parsed,
        }
        self._headlines.append(entry)
        log.debug("headline queued", source=source, tickers=parsed["tickers"],
                  sentiment=f"{parsed['sentiment']:+.2f}",
                  event_type=parsed["event_type"])

    async def analyze(self, ticker: str, df: pd.DataFrame) -> Optional[Signal]:
        """Check if any recent headlines affect this ticker and produce a signal.

        Scans the headline queue for:
          1. Direct ticker mentions
          2. Sector-level macro events that affect this ticker
          3. Sector rotation signals

        If multiple headlines match, the strongest signal wins.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - self._headline_expiry

        # Determine which sectors this ticker belongs to
        ticker_sectors = _TICKER_SECTORS.get(ticker, set())

        best_signal: Optional[Signal] = None
        best_score: float = 0.0

        for entry in self._headlines:
            if entry["timestamp"] < cutoff:
                continue

            parsed = entry["parsed"]
            text = entry["text"]

            signal = self._evaluate_headline(
                ticker, ticker_sectors, text, parsed, df,
            )
            if signal and signal.score > best_score:
                best_signal = signal
                best_score = signal.score

        return best_signal

    def parse_headline(self, text: str) -> dict:
        """Parse a news headline into structured components.

        Returns:
            dict with keys:
                tickers: List[str]       — extracted ticker symbols
                sentiment: float         — -1.0 (bearish) to +1.0 (bullish)
                event_type: str          — category of event
                sector: str              — primary sector affected (or "")
        """
        tickers = _extract_tickers(text)

        # Check macro events first (highest priority)
        for pattern, _affected, direction, score, event_type in _MACRO_RULES:
            if pattern.search(text):
                sentiment = score if direction == "BUY" else -score
                # Add affected tickers that were not already extracted
                combined = list(dict.fromkeys(tickers + _affected))
                sector = self._infer_sector(combined)
                return {
                    "tickers": combined,
                    "sentiment": sentiment,
                    "event_type": event_type,
                    "sector": sector,
                }

        # Check sector rotation
        for pattern, buy_sectors, sell_sectors in _ROTATION_RULES:
            if pattern.search(text):
                rotation_tickers = list(tickers)
                for s in buy_sectors:
                    rotation_tickers.extend(SECTOR_MAP.get(s, []))
                for s in sell_sectors:
                    rotation_tickers.extend(SECTOR_MAP.get(s, []))
                rotation_tickers = list(dict.fromkeys(rotation_tickers))
                sector = buy_sectors[0] if buy_sectors else (sell_sectors[0] if sell_sectors else "")
                # Net sentiment depends on whether it is risk-on or risk-off
                sentiment = 0.5 if buy_sectors else -0.5
                return {
                    "tickers": rotation_tickers,
                    "sentiment": sentiment,
                    "event_type": "sector_rotation",
                    "sector": sector,
                }

        # Check company-specific sentiment
        bullish_hits = sum(1 for p in _BULLISH_PATTERNS if p.search(text))
        bearish_hits = sum(1 for p in _BEARISH_PATTERNS if p.search(text))

        if bullish_hits or bearish_hits:
            net = bullish_hits - bearish_hits
            sentiment = max(-1.0, min(1.0, net * 0.4))
            event_type = "company_bullish" if net > 0 else ("company_bearish" if net < 0 else "company_mixed")
        else:
            sentiment = 0.0
            event_type = "neutral"

        sector = self._infer_sector(tickers)
        return {
            "tickers": tickers,
            "sentiment": sentiment,
            "event_type": event_type,
            "sector": sector,
        }

    # ── Private helpers ──────────────────────────────────────────────────────

    def _evaluate_headline(
        self,
        ticker: str,
        ticker_sectors: Set[str],
        text: str,
        parsed: dict,
        df: Optional[pd.DataFrame],
    ) -> Optional[Signal]:
        """Evaluate a single parsed headline against a specific ticker."""

        mentioned_tickers: List[str] = parsed["tickers"]
        sentiment: float = parsed["sentiment"]
        event_type: str = parsed["event_type"]

        # ── Direct ticker mention ────────────────────────────────────────────
        if ticker in mentioned_tickers:
            return self._build_signal(ticker, sentiment, event_type, text, df)

        # ── Macro event: ticker is in the affected list of a macro rule ──────
        for pattern, affected, direction, score, evt in _MACRO_RULES:
            if pattern.search(text) and ticker in affected:
                s = score if direction == "BUY" else -score
                return self._build_signal(ticker, s, evt, text, df)

        # ── Sector-level match: headline mentions a sector this ticker is in ─
        if ticker_sectors and parsed["sector"]:
            if parsed["sector"] in ticker_sectors:
                # Dampen sector-level signals (less direct)
                dampened = sentiment * 0.7
                return self._build_signal(ticker, dampened, event_type, text, df)

        # ── Sector rotation: check if ticker is in a buy/sell sector ─────────
        if event_type == "sector_rotation":
            return self._check_rotation(ticker, ticker_sectors, text, df)

        # ── Flight to safety special case ────────────────────────────────────
        if event_type == "sector_rotation" or re.search(r"flight\s+to\s+(safety|quality)|risk[\s-]?off", text, re.I):
            if ticker in ("GLD", "TLT", "SLV"):
                return self._build_signal(ticker, 0.60, "flight_to_safety", text, df)
            if ticker in ("SPY", "QQQ", "IWM"):
                return self._build_signal(ticker, -0.55, "flight_to_safety", text, df)

        return None

    def _check_rotation(
        self,
        ticker: str,
        ticker_sectors: Set[str],
        text: str,
        df: Optional[pd.DataFrame],
    ) -> Optional[Signal]:
        """Check sector rotation rules against a specific ticker."""
        for pattern, buy_sectors, sell_sectors in _ROTATION_RULES:
            if not pattern.search(text):
                continue

            # Flight-to-safety / risk-off special handling
            if not buy_sectors and not sell_sectors:
                if ticker in ("GLD", "TLT", "SLV"):
                    return self._build_signal(ticker, 0.60, "flight_to_safety", text, df)
                if ticker_sectors & {"tech", "ai_infra", "fintech", "crypto"}:
                    return self._build_signal(ticker, -0.55, "risk_off", text, df)
                continue

            for s in buy_sectors:
                if s in ticker_sectors:
                    return self._build_signal(ticker, 0.60, "rotation_buy", text, df)
            for s in sell_sectors:
                if s in ticker_sectors:
                    return self._build_signal(ticker, -0.55, "rotation_sell", text, df)

        return None

    def _build_signal(
        self,
        ticker: str,
        sentiment: float,
        event_type: str,
        headline: str,
        df: Optional[pd.DataFrame],
    ) -> Optional[Signal]:
        """Convert sentiment + event_type into a Signal with price levels."""

        # Determine direction
        if abs(sentiment) < 0.10:
            return None  # too weak to act on

        direction = "BUY" if sentiment > 0 else "SELL"
        score = min(abs(sentiment), 1.0)

        # Price levels from df if available
        entry_price = 0.0
        stop_price = 0.0
        target_price = 0.0
        limit_price = 0.0

        if df is not None and len(df) >= 20:
            closes = df["close"]
            highs = df["high"]
            lows = df["low"]
            entry_price = float(closes.iloc[-1])

            # Simple ATR-based stop/target (14-period)
            period = min(14, len(df) - 1)
            tr_vals = []
            for i in range(-period, 0):
                h = float(highs.iloc[i])
                l = float(lows.iloc[i])
                prev_c = float(closes.iloc[i - 1]) if i - 1 >= -len(closes) else l
                tr_vals.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
            atr_val = sum(tr_vals) / len(tr_vals) if tr_vals else entry_price * 0.02

            if direction == "BUY":
                stop_price = entry_price - 1.5 * atr_val
                target_price = entry_price + 3.0 * atr_val
                limit_price = entry_price * 1.001
            else:
                stop_price = entry_price + 1.5 * atr_val
                target_price = entry_price - 3.0 * atr_val
                limit_price = entry_price * 0.999

        # Truncate headline for reasoning
        short_headline = headline[:120] + ("..." if len(headline) > 120 else "")
        reasoning = f"News [{event_type}]: {short_headline}"

        catalysts = [f"Headline: {short_headline}"]
        risks = []
        if event_type.startswith("company_"):
            risks.append("Single-headline signal; verify with fundamentals")
        elif event_type in ("geopolitical", "sanctions", "flight_to_safety"):
            risks.append("Geopolitical headlines can reverse quickly")
        elif event_type.startswith("cpi_") or event_type.startswith("fed_"):
            risks.append("Macro data interpretation may shift intraday")

        return Signal(
            ticker=ticker,
            direction=direction,
            score=round(score, 4),
            strategy=self.name,
            reasoning=reasoning,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            limit_price=limit_price,
            catalysts=catalysts,
            risks=risks,
        )

    @staticmethod
    def _infer_sector(tickers: List[str]) -> str:
        """Return the most likely sector for a list of tickers."""
        sector_counts: Dict[str, int] = {}
        for t in tickers:
            for s in _TICKER_SECTORS.get(t, set()):
                sector_counts[s] = sector_counts.get(s, 0) + 1
        if not sector_counts:
            return ""
        return max(sector_counts, key=sector_counts.get)  # type: ignore[arg-type]

    def _prune_expired(self) -> None:
        """Remove headlines older than the expiry window."""
        cutoff = datetime.now(timezone.utc) - self._headline_expiry
        while self._headlines and self._headlines[0]["timestamp"] < cutoff:
            self._headlines.popleft()

    @property
    def headline_count(self) -> int:
        """Number of headlines currently in the queue."""
        return len(self._headlines)

    @property
    def active_headline_count(self) -> int:
        """Number of non-expired headlines."""
        cutoff = datetime.now(timezone.utc) - self._headline_expiry
        return sum(1 for h in self._headlines if h["timestamp"] >= cutoff)
