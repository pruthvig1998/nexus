"""Universe scanner — discovers trading opportunities beyond the watchlist.

Scans a broad base of liquid US equities and ETFs for momentum, volume
spikes, and technical setups, then returns the top candidates for the
engine's full strategy analysis.
"""

from __future__ import annotations

import asyncio
import time
from typing import List, Optional

from nexus.config import get_config
from nexus.logger import get_logger

log = get_logger("scanner")

# ── Base universe: most liquid US equities + key ETFs ────────────────────────

BASE_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "ORCL", "CRM",
    # Semiconductors
    "AMD", "INTC", "QCOM", "MU", "MRVL", "ARM", "SMCI", "ANET", "KLAC", "LRCX",
    # Finance
    "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "SCHW", "V", "MA",
    # Healthcare
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "BMY", "AMGN", "GILD", "ISRG",
    # Consumer
    "WMT", "COST", "HD", "NKE", "SBUX", "MCD", "DIS", "NFLX", "ABNB", "BKNG",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "DVN", "MPC", "VLO", "PSX",
    # Industrial
    "CAT", "DE", "BA", "RTX", "LMT", "GE", "HON", "UPS", "FDX", "UNP",
    # Other large-cap
    "COIN", "SNOW", "PLTR", "PANW", "CRWD", "ZS", "NET", "DDOG", "SQ", "SHOP",
    "UBER", "LYFT", "DASH", "RBLX", "RIVN", "LCID", "NIO", "XPEV", "LI", "F",
    # ETFs (sector/thematic — great for options)
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLV", "XLI", "XLY",
    "XLP", "XLU", "XLRE", "XLC", "XLB", "GLD", "SLV", "TLT", "HYG", "EEM",
    "ARKK", "SOXX", "SMH", "KWEB", "FXI",
]

# Cache for scanner results (avoid rescanning every cycle)
_scan_cache: dict[str, tuple[list[str], float]] = {}
_CACHE_TTL = 300  # 5 minutes


class UniverseScanner:
    """Discover high-opportunity tickers beyond the static watchlist.

    Scans the base universe for:
    1. Momentum: stocks with significant daily moves (>2%)
    2. Volume spikes: unusual volume vs 20-day average
    3. Gap plays: significant gap ups/downs at open
    """

    def __init__(self, max_tickers: int = 20) -> None:
        self._max_tickers = max_tickers
        cfg = get_config()
        self._watchlist = set(cfg.watchlist)

    async def scan(self) -> List[str]:
        """Return top tickers not already on the watchlist.

        Uses yfinance for batch data. Results cached for 5 minutes.
        """
        now = time.time()
        cached = _scan_cache.get("universe")
        if cached and (now - cached[1]) < _CACHE_TTL:
            return cached[0]

        try:
            tickers = await self._scan_movers()
            # Filter out watchlist tickers (already being scanned)
            tickers = [t for t in tickers if t not in self._watchlist]
            tickers = tickers[: self._max_tickers]
            _scan_cache["universe"] = (tickers, now)
            log.info("Universe scan complete", discovered=len(tickers))
            return tickers
        except Exception as e:
            log.warning("Universe scan failed", error=str(e))
            return []

    async def _scan_movers(self) -> List[str]:
        """Scan for top movers using yfinance batch download."""
        import yfinance as yf

        # Download 5-day data for all universe tickers in one batch
        # This is the most efficient way to scan a large universe
        all_tickers = [t for t in BASE_UNIVERSE if t not in self._watchlist]
        if not all_tickers:
            return []

        data = await asyncio.to_thread(
            yf.download,
            tickers=all_tickers,
            period="5d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )

        if data is None or data.empty:
            return []

        scores: list[tuple[str, float]] = []

        for ticker in all_tickers:
            try:
                score = self._score_ticker(data, ticker, len(all_tickers) > 1)
                if score is not None and score > 0:
                    scores.append((ticker, score))
            except Exception:  # noqa: S112
                continue

        # Sort by score descending, return tickers
        scores.sort(key=lambda x: x[1], reverse=True)
        return [t for t, _ in scores]

    def _score_ticker(
        self, data, ticker: str, multi_ticker: bool
    ) -> Optional[float]:
        """Score a single ticker based on momentum + volume.

        Returns a composite score (0-100) or None if data insufficient.
        """
        try:
            if multi_ticker:
                closes = data["Close"][ticker].dropna()
                volumes = data["Volume"][ticker].dropna()
            else:
                closes = data["Close"].dropna()
                volumes = data["Volume"].dropna()

            if len(closes) < 2:
                return None

            # Daily move: absolute percentage change
            daily_move = abs(
                (float(closes.iloc[-1]) - float(closes.iloc[-2]))
                / float(closes.iloc[-2])
            )

            # Volume spike: today vs average of prior days
            if len(volumes) >= 3:
                avg_vol = float(volumes.iloc[:-1].mean())
                vol_ratio = float(volumes.iloc[-1]) / max(avg_vol, 1)
            else:
                vol_ratio = 1.0

            # Multi-day momentum: 5-day move
            if len(closes) >= 5:
                five_day_move = abs(
                    (float(closes.iloc[-1]) - float(closes.iloc[0]))
                    / float(closes.iloc[0])
                )
            else:
                five_day_move = daily_move

            # Composite score: weighted combination
            # - Daily move > 2% is interesting, > 5% is very interesting
            # - Volume ratio > 1.5 is notable, > 3.0 is unusual
            # - 5-day move adds trend context
            score = 0.0
            score += min(daily_move * 1000, 40)  # 4% daily = 40 pts
            score += min((vol_ratio - 1.0) * 15, 30)  # 3x vol = 30 pts
            score += min(five_day_move * 600, 30)  # 5% 5d = 30 pts

            # Minimum threshold: skip boring tickers
            if daily_move < 0.015 and vol_ratio < 1.3:
                return None

            return score

        except (KeyError, IndexError, TypeError):
            return None
