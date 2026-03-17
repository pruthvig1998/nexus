"""Technical indicators — RSI, MACD, Bollinger, ATR, ADR, volume.

Fixed vs original:
1. RSI: Wilder smoothing via ewm(alpha=1/period, adjust=False) — no off-by-one
   RSI = 100 when avg_loss == 0 (pure uptrend), not 50
2. MA crossover: event-based cross detection (prev vs curr bar) — no continuous firing
3. MACD cross: histogram sign change detection — event, not state
4. Limit price: dynamic min(0.3%, 0.5×spread) slippage — not flat 0.1%

v3.2 additions:
5. adr(): Average Daily Range — used by ORB for compression filtering
6. rsi_series(): Full RSI series — used by mean reversion for divergence detection
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class RSIResult:
    value: float
    oversold: bool
    overbought: bool


@dataclass
class MACDResult:
    macd: float
    signal: float
    histogram: float
    bullish_cross: bool
    bearish_cross: bool


@dataclass
class BollingerResult:
    upper: float
    middle: float
    lower: float
    bandwidth: float
    pct_b: float
    above_upper: bool
    below_lower: bool


@dataclass
class ATRResult:
    value: float
    stop_long: float
    stop_short: float


def rsi(prices: pd.Series, period: int = 14) -> RSIResult:
    if len(prices) < period + 1:
        return RSIResult(50.0, False, False)
    delta = prices.diff()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False).mean()
    last_gain = float(avg_gain.iloc[-1])
    last_loss = float(avg_loss.iloc[-1])
    if np.isnan(last_gain) or np.isnan(last_loss):
        value = 50.0
    elif last_loss == 0:
        value = 100.0 if last_gain > 0 else 50.0  # pure uptrend → fully overbought
    else:
        value = 100.0 - (100.0 / (1.0 + last_gain / last_loss))
    return RSIResult(value=value, oversold=value < 30, overbought=value > 70)


def sma(prices: pd.Series, period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return float(prices.rolling(period).mean().iloc[-1])


def ema(prices: pd.Series, period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return float(prices.ewm(span=period, adjust=False).mean().iloc[-1])


def golden_cross(prices: pd.Series, fast: int = 20, slow: int = 50) -> Optional[bool]:
    """True = golden cross event, False = death cross event, None = no cross this bar."""
    if len(prices) < slow + 1:
        return None
    fast_ma = prices.rolling(fast).mean()
    slow_ma = prices.rolling(slow).mean()
    prev_above = fast_ma.iloc[-2] > slow_ma.iloc[-2]
    curr_above = fast_ma.iloc[-1] > slow_ma.iloc[-1]
    if not prev_above and curr_above:
        return True
    if prev_above and not curr_above:
        return False
    return None


def macd(prices: pd.Series, fast: int = 12, slow: int = 26, signal_period: int = 9) -> MACDResult:
    if len(prices) < slow + signal_period:
        return MACDResult(0, 0, 0, False, False)
    ema_fast = prices.ewm(span=fast, adjust=False).mean()
    ema_slow = prices.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line
    curr = float(histogram.iloc[-1])
    prev = float(histogram.iloc[-2]) if len(histogram) > 1 else 0.0
    return MACDResult(
        macd=float(macd_line.iloc[-1]),
        signal=float(signal_line.iloc[-1]),
        histogram=curr,
        bullish_cross=prev < 0 < curr,
        bearish_cross=prev > 0 > curr,
    )


def bollinger_bands(prices: pd.Series, period: int = 20, num_std: float = 2.0) -> BollingerResult:
    if len(prices) < period:
        p = float(prices.iloc[-1]) if len(prices) > 0 else 0.0
        return BollingerResult(p, p, p, 0, 0.5, False, False)
    rolling = prices.rolling(period)
    middle = float(rolling.mean().iloc[-1])
    std = float(rolling.std().iloc[-1])
    upper = middle + num_std * std
    lower = middle - num_std * std
    last = float(prices.iloc[-1])
    bandwidth = (upper - lower) / middle if middle != 0 else 0
    pct_b = (last - lower) / (upper - lower) if upper != lower else 0.5
    return BollingerResult(
        upper=upper,
        middle=middle,
        lower=lower,
        bandwidth=bandwidth,
        pct_b=pct_b,
        above_upper=last > upper,
        below_lower=last < lower,
    )


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
    entry_price: float = 0.0,
    multiplier: float = 1.5,
) -> ATRResult:
    if len(close) < period + 1:
        return ATRResult(0.0, entry_price, entry_price)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_val = float(tr.ewm(span=period, adjust=False).mean().iloc[-1])
    return ATRResult(
        value=atr_val,
        stop_long=entry_price - multiplier * atr_val,
        stop_short=entry_price + multiplier * atr_val,
    )


def volume_ratio(volume: pd.Series, period: int = 20) -> float:
    """Current bar volume as a multiple of the rolling average."""
    if len(volume) < period:
        return 1.0
    avg = float(volume.rolling(period).mean().iloc[-1])
    return float(volume.iloc[-1]) / max(avg, 1)


def adr(high: pd.Series, low: pd.Series, period: int = 14) -> float:
    """Average Daily Range — mean of (high - low) over period bars.

    Used by ORB to identify compressed ranges relative to normal volatility.
    A range < 50% of ADR signals a 'coiling' day (Toby Crabel NR7 companion).
    """
    if len(high) < 2:
        return 0.0
    ranges = (high - low).iloc[-period:]
    return float(ranges.mean())


def rsi_series(prices: pd.Series, period: int = 14) -> pd.Series:
    """Return the full RSI series (not just the last value).

    Used by mean reversion to detect RSI divergence across a lookback window:
    price makes a new low but RSI makes a higher low → bullish divergence.
    """
    if len(prices) < period + 1:
        return pd.Series([50.0] * len(prices), index=prices.index)
    delta = prices.diff()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False).mean()
    # Handle avg_loss == 0 correctly (pure uptrend → RSI = 100)
    last_loss = avg_loss.copy()
    rs = avg_gain / last_loss.where(last_loss != 0, other=np.nan)
    result = 100.0 - (100.0 / (1.0 + rs))
    result = result.where(~np.isnan(result), other=50.0)
    result = result.where(last_loss != 0, other=100.0)
    return result


def dynamic_limit_price(mid: float, spread: float, side: str) -> float:
    """Slippage buffer: min(0.3%, 0.5×spread). Better than flat 0.1%."""
    slippage = min(0.003 * mid, 0.5 * spread)
    return mid + slippage if side == "BUY" else mid - slippage
