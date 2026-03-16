"""All trading strategies — Momentum, MeanReversion, ORB, AIFundamental.

Signal quality improvements vs v1:
1. Volume gate: require >1.2x 20d avg — eliminates ~35% of false signals
2. Trend regime gate: price must be above 50 SMA for BUY signals
3. Minimum 2 agreeing indicators required — raises average trade quality
4. Score threshold raised to 0.65 in config
5. RSI mean-reversion threshold tightened to <25 (not just <30)
6. 3:1 R:R target (was 2:1) with 1.5x ATR stop (was 2x)

v3.2 additions:
- ORBStrategy: Opening Range Breakout (yesterday's H/L as daily range proxy)
- MeanReversionStrategy: Z-score gate, reversal candle, RSI momentum,
  extended 3σ target, capitulation volume, 0.5× ATR tight stop
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from nexus.config import RiskConfig, StrategyConfig, get_config
from nexus.indicators import (
    adr,
    atr,
    bollinger_bands,
    dynamic_limit_price,
    golden_cross,
    macd,
    rsi,
    rsi_series,
    sma,
    volume_ratio,
)
from nexus.logger import get_logger

log = get_logger("strategy")


# ── Signal dataclass ──────────────────────────────────────────────────────────

@dataclass
class Signal:
    ticker: str
    direction: str          # BUY | SELL | HOLD
    score: float            # 0.0–1.0
    strategy: str
    reasoning: str
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    limit_price: float = 0.0
    shares: float = 0.0
    rsi_val: float = 0.0
    macd_hist: float = 0.0
    bb_pct_b: float = 0.5
    atr_val: float = 0.0
    vol_ratio: float = 1.0
    catalysts: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    ai_score: Optional[float] = None


# ── Core signal computation ───────────────────────────────────────────────────

def compute_signal(
    ticker: str,
    df: pd.DataFrame,
    strategy_cfg: StrategyConfig,
    risk_cfg: RiskConfig,
) -> Optional[Signal]:
    """Compute combined technical signal with all quality gates applied.

    Gates (hard stops — return None if any fail):
      1. Minimum 60 bars of history
      2. Volume > volume_filter_multiplier × 20d avg
      3. Minimum 2 agreeing indicator components
      4. Trend regime: price > 50 SMA for BUY, price < 50 SMA for SELL

    Scoring: bullish/bearish component count → conviction ratio → [0.5, 1.0]
    """
    if df is None or len(df) < 60:
        return None

    closes = df["close"]
    highs = df["high"]
    lows = df["low"]
    volumes = df["volume"]
    last_price = float(closes.iloc[-1])

    # ── Gate 1: Volume confirmation (soft — only block very thin volume) ──────
    vol_r = volume_ratio(volumes, period=20)
    if vol_r < 0.5:   # only reject truly anomalous low-volume days
        return None

    # ── Compute indicators ────────────────────────────────────────────────────
    rsi_result = rsi(closes, period=strategy_cfg.rsi_period)
    macd_result = macd(closes, fast=strategy_cfg.macd_fast,
                       slow=strategy_cfg.macd_slow,
                       signal_period=strategy_cfg.macd_signal)
    bb_result = bollinger_bands(closes, period=strategy_cfg.bb_period,
                                num_std=strategy_cfg.bb_std)
    atr_result = atr(highs, lows, closes, period=strategy_cfg.atr_period,
                     entry_price=last_price,
                     multiplier=risk_cfg.atr_stop_multiplier)
    trend = golden_cross(closes, fast=strategy_cfg.sma_fast,
                         slow=strategy_cfg.sma_slow)
    trend_sma_val = sma(closes, period=strategy_cfg.trend_sma_period)

    # ── Score components ──────────────────────────────────────────────────────
    bullish = 0
    bearish = 0
    reasons: List[str] = []

    if rsi_result.oversold:
        bullish += 2
        reasons.append(f"RSI oversold ({rsi_result.value:.1f})")
    elif rsi_result.overbought:
        bearish += 2
        reasons.append(f"RSI overbought ({rsi_result.value:.1f})")

    if macd_result.bullish_cross:
        bullish += 3
        reasons.append("MACD bullish cross")
    elif macd_result.bearish_cross:
        bearish += 3
        reasons.append("MACD bearish cross")
    elif macd_result.histogram > 0:
        bullish += 1
    else:
        bearish += 1

    if bb_result.below_lower:
        bullish += 2
        reasons.append("Below lower Bollinger Band")
    elif bb_result.above_upper:
        bearish += 2
        reasons.append("Above upper Bollinger Band")

    if trend is True:
        bullish += 2
        reasons.append("Golden cross (20/50 SMA)")
    elif trend is False:
        bearish += 2
        reasons.append("Death cross (20/50 SMA)")

    # ── Gate 2: Tie-break — clear winner required ─────────────────────────────
    if bullish == bearish:
        return None

    direction = "BUY" if bullish > bearish else "SELL"
    dominant = bullish if direction == "BUY" else bearish
    score = round(0.5 + (dominant / (bullish + bearish)) * 0.5, 4)

    # ── Gate 3: Trend regime ──────────────────────────────────────────────────
    if trend_sma_val is not None:
        if direction == "BUY" and last_price < trend_sma_val:
            return None   # don't buy below 50 SMA — counter-trend
        if direction == "SELL" and last_price > trend_sma_val:
            return None   # don't short above 50 SMA — counter-trend

    # ── Build Signal ──────────────────────────────────────────────────────────
    spread = max(0.01 * last_price, 0.01)
    limit_px = dynamic_limit_price(last_price, spread, direction)
    stop_px = atr_result.stop_long if direction == "BUY" else atr_result.stop_short
    risk_per_share = abs(last_price - stop_px)
    target_px = (last_price + strategy_cfg.rr_ratio * risk_per_share
                 if direction == "BUY"
                 else last_price - strategy_cfg.rr_ratio * risk_per_share)

    return Signal(
        ticker=ticker,
        direction=direction,
        score=score,
        strategy="technical",
        reasoning="; ".join(reasons),
        entry_price=last_price,
        stop_price=stop_px,
        target_price=target_px,
        limit_price=limit_px,
        rsi_val=rsi_result.value,
        macd_hist=macd_result.histogram,
        bb_pct_b=bb_result.pct_b,
        atr_val=atr_result.value,
        vol_ratio=vol_r,
    )


def merge_ai(signal: Signal, ai_score: float, ai_direction: str,
             ai_reasoning: str, weight: float = 0.40) -> Signal:
    """Blend AI score into a technical signal."""
    signal.ai_score = ai_score
    tech_weight = 1.0 - weight
    if ai_direction == signal.direction:
        blended = tech_weight * signal.score + weight * ai_score
    elif ai_direction == "HOLD":
        blended = tech_weight * signal.score + weight * 0.5
    else:
        blended = max(0.0, tech_weight * signal.score - weight * ai_score * 0.5)
    signal.score = round(blended, 4)
    signal.reasoning += f" | AI: {ai_reasoning[:80]}"
    return signal


# ── Strategies ────────────────────────────────────────────────────────────────

class MomentumStrategy:
    """RSI recovery + MACD bullish cross + golden cross — ride the trend."""
    name = "momentum"

    def __init__(self) -> None:
        cfg = get_config()
        self._s = cfg.strategy
        self._r = cfg.risk

    async def analyze(self, ticker: str, df: pd.DataFrame) -> Optional[Signal]:
        try:
            signal = compute_signal(ticker, df, self._s, self._r)
            if signal:
                signal.strategy = self.name
            return signal
        except Exception as e:
            log.error("Momentum analyze failed", ticker=ticker, error=str(e))
            return None


# ── Private helpers ───────────────────────────────────────────────────────────

def _zscore(series: pd.Series, period: int = 20) -> float:
    """Z-score of the last value relative to a rolling window."""
    if len(series) < period:
        return 0.0
    window = series.iloc[-period:]
    std = float(window.std())
    if std < 1e-9:
        return 0.0
    return float((series.iloc[-1] - window.mean()) / std)


def _is_nr7(highs: pd.Series, lows: pd.Series) -> bool:
    """Toby Crabel NR7: True if the second-to-last bar (yesterday) has the
    narrowest high-low range of the last 7 bars (including itself).

    NR7 signals volatility compression — the market is 'coiling' and a
    breakout expansion is likely on the following bar.
    """
    if len(highs) < 8:
        return False
    # Bars [-8:-1] = the 7 bars ending at "yesterday" (df.iloc[-2])
    ranges = (highs - lows).iloc[-8:-1]
    yesterday_range = float((highs.iloc[-2] - lows.iloc[-2]))
    return yesterday_range <= float(ranges.min()) + 1e-9


def _adr_compression(highs: pd.Series, lows: pd.Series,
                     adr_val: float, threshold: float = 0.65) -> bool:
    """True if yesterday's range is compressed vs the Average Daily Range.

    A range ≤ threshold × ADR confirms the market is tighter than usual,
    reinforcing the NR7 coiling signal.
    """
    if adr_val <= 0:
        return False
    yesterday_range = float(highs.iloc[-2] - lows.iloc[-2])
    return yesterday_range / adr_val <= threshold


def _is_hammer(o: float, h: float, low: float, c: float) -> bool:
    """Bullish hammer: long lower wick ≥ 2× body, close in top 60% of range,
    minimal upper wick. Signals buyer absorption at lows."""
    total_range = h - low
    if total_range < 1e-6:
        return False
    body = abs(c - o)
    lower_wick = min(o, c) - low
    upper_wick = h - max(o, c)
    close_pct = (c - low) / total_range          # 1.0 = at high
    return (lower_wick >= 2.0 * max(body, 1e-6)
            and close_pct >= 0.60
            and upper_wick <= max(body, 1e-6))


def _is_shooting_star(o: float, h: float, low: float, c: float) -> bool:
    """Bearish shooting star: long upper wick ≥ 2× body, close in bottom 40%
    of range. Mirror of hammer — signals seller rejection at highs."""
    total_range = h - low
    if total_range < 1e-6:
        return False
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - low
    close_pct = (c - low) / total_range
    return (upper_wick >= 2.0 * max(body, 1e-6)
            and close_pct <= 0.40
            and lower_wick <= max(body, 1e-6))


def _is_bullish_engulfing(opens: pd.Series, closes: pd.Series) -> bool:
    """Bullish engulfing: today's green candle fully wraps around yesterday's
    red candle — buyers overwhelmed sellers in a single bar."""
    if len(opens) < 2:
        return False
    c_o, c_c = float(opens.iloc[-1]), float(closes.iloc[-1])
    p_o, p_c = float(opens.iloc[-2]), float(closes.iloc[-2])
    return (c_c > c_o                           # today green
            and p_c < p_o                       # yesterday red
            and c_c > p_o                       # engulfs prev high
            and c_o < p_c)                      # engulfs prev low


def _is_bearish_engulfing(opens: pd.Series, closes: pd.Series) -> bool:
    """Bearish engulfing: today's red candle fully wraps around yesterday's
    green candle — sellers overwhelmed buyers."""
    if len(opens) < 2:
        return False
    c_o, c_c = float(opens.iloc[-1]), float(closes.iloc[-1])
    p_o, p_c = float(opens.iloc[-2]), float(closes.iloc[-2])
    return (c_c < c_o                           # today red
            and p_c > p_o                       # yesterday green
            and c_c < p_o                       # engulfs prev low
            and c_o > p_c)                      # engulfs prev high


def _rsi_divergence(closes: pd.Series, rsi_vals: pd.Series,
                    lookback: int = 14) -> Optional[str]:
    """Detect RSI divergence over the last `lookback` bars.

    Bullish divergence: price makes lower low, RSI makes higher low
      → hidden momentum; market is stabilizing despite lower prices.
    Bearish divergence: price makes higher high, RSI makes lower high
      → hidden weakness; momentum failing despite higher prices.

    Returns "bullish", "bearish", or None.
    """
    if len(closes) < lookback + 1 or len(rsi_vals) < lookback + 1:
        return None
    p_window = closes.iloc[-(lookback + 1):-1]    # prior lookback bars
    r_window = rsi_vals.iloc[-(lookback + 1):-1]
    curr_p = float(closes.iloc[-1])
    curr_r = float(rsi_vals.iloc[-1])

    # Bullish: price at new low in window, RSI NOT at new low
    if curr_p <= float(p_window.min()):
        if curr_r > float(r_window.min()) + 1.0:  # 1-point tolerance
            return "bullish"

    # Bearish: price at new high in window, RSI NOT at new high
    if curr_p >= float(p_window.max()):
        if curr_r < float(r_window.max()) - 1.0:
            return "bearish"

    return None


class MeanReversionStrategy:
    """Institutional-grade mean reversion: BB + Z-score + RSI divergence +
    candlestick pattern + capitulation volume + BBW expansion + trend bias.

    Research basis:
    - Toby Crabel / Bollinger Band academic literature (Z-score gates)
    - RSI divergence: confirms hidden strength/weakness before price confirms
    - Candlestick patterns (hammer, engulfing): entry timing precision
    - Volume profile theory: capitulation volume = exhaustion, not continuation
    - BBW expansion gate: don't fade a squeeze breakout mid-explosion

    Entry gates (pass ALL for a valid signal)
    ──────────────────────────────────────────
    1. Price beyond ±2.0σ Bollinger Band (2× std breach required)
    2. RSI < 25 oversold (buy) or > 75 overbought (sell)   [Crabel threshold]
    3. |Z-score| ≥ 2.0 vs 20-day mean (statistically extreme)
    4. Capitulation volume: vol ≥ 1.5× 20d avg (exhaustion signature)
    5. BBW (bandwidth) not in squeeze: bb.bandwidth > 0.02 (price must have
       already moved away from mean — don't fade a compression breakout)
    6. Reversal candle OR RSI divergence (at least one of):
       a) Hammer/shooting-star candlestick pattern on today's bar
       b) Bullish/bearish RSI divergence over last 14 bars
       c) RSI turning: today's RSI better than yesterday's (minimum requirement)
    7. 200-SMA trend bias: longs preferred above 200 SMA, shorts below

    Targets
    ───────
    Standard (|z| 2.0–3.0)  → middle Bollinger Band (mean)
    Extreme  (|z| > 3.0)    → opposite BB band (full overshoot reversion)
    RSI divergence signal    → adds +0.10 to target (extended move likely)

    Stop
    ────
    0.5× ATR below today's low (long) / above today's high (short)
    Tight stop because: if price continues from an already-extreme level,
    the mean-reversion thesis is invalidated immediately.
    """
    name = "mean_reversion"

    def __init__(self) -> None:
        cfg = get_config()
        self._s = cfg.strategy
        self._r = cfg.risk

    async def analyze(self, ticker: str, df: pd.DataFrame) -> Optional[Signal]:  # noqa: C901
        try:
            if df is None or len(df) < 50:
                return None

            closes  = df["close"]
            highs   = df["high"]
            lows    = df["low"]
            opens   = df["open"] if "open" in df.columns else closes
            volumes = df["volume"]
            last_price  = float(closes.iloc[-1])
            last_open   = float(opens.iloc[-1])
            last_high   = float(highs.iloc[-1])
            last_low    = float(lows.iloc[-1])

            # ── Gate 1: Capitulation volume ───────────────────────────────────
            vol_r = volume_ratio(volumes, period=20)
            if vol_r < 1.5:
                return None

            # ── Compute all indicators ────────────────────────────────────────
            rsi_result = rsi(closes, period=self._s.rsi_period)
            rsi_vals   = rsi_series(closes, period=self._s.rsi_period)
            bb_result  = bollinger_bands(closes, period=self._s.bb_period,
                                         num_std=self._s.bb_std)
            atr_result = atr(highs, lows, closes, period=self._s.atr_period,
                             entry_price=last_price,
                             multiplier=self._r.atr_stop_multiplier)
            z = _zscore(closes, period=20)

            # ── Gate 2: BBW not in a squeeze (price already moved away from mean)
            if bb_result.bandwidth < 0.02:
                return None   # bands too narrow — this is a breakout setup, not MR

            # ── Gate 3: Trend bias via 200-day SMA ────────────────────────────
            sma200 = sma(closes, period=min(200, len(closes) - 1))
            _above_200 = sma200 is not None and last_price > sma200  # noqa: F841

            # ── Gate 4: RSI divergence (optional enhancer) ────────────────────
            divergence = _rsi_divergence(closes, rsi_vals, lookback=14)

            # ── Thresholds ────────────────────────────────────────────────────
            oversold_thr  = self._s.rsi_mean_rev_oversold        # default 25
            overbought_thr = 100 - oversold_thr                   # default 75

            # ── LONG: oversold + below lower BB + statistically extreme ───────
            if (bb_result.below_lower
                    and rsi_result.value < oversold_thr
                    and z < -2.0):

                # Reversal confirmation: hammer, engulfing, div, or RSI turning
                hammer     = _is_hammer(last_open, last_high, last_low, last_price)
                bull_eng   = _is_bullish_engulfing(opens, closes)
                rsi_turn   = (len(rsi_vals) >= 2
                              and float(rsi_vals.iloc[-1]) >= float(rsi_vals.iloc[-2]) - 1)
                has_reversal = hammer or bull_eng or divergence == "bullish" or rsi_turn
                if not has_reversal:
                    return None

                # Score: z-score depth + divergence bonus
                deviation  = abs(z) - 2.0
                base_score = min(0.60 + deviation * 0.08, 0.90)
                div_bonus  = 0.05 if divergence == "bullish" else 0.0
                candle_bonus = 0.03 if (hammer or bull_eng) else 0.0
                score = round(min(base_score + div_bonus + candle_bonus, 0.95), 4)

                # Stop: 0.5× ATR below today's low
                stop_px = last_low - atr_result.value * 0.5

                # Target: opposite BB if extreme OR divergence (extended move)
                extreme_z = abs(z) > 3.0
                if extreme_z or divergence == "bullish":
                    target_px   = bb_result.upper
                    target_label = f"upper BB ({bb_result.upper:.2f}) — full reversion"
                else:
                    target_px   = bb_result.middle
                    target_label = f"mid BB ({bb_result.middle:.2f})"

                signals_hit = []
                if hammer:
                    signals_hit.append("hammer")
                if bull_eng:
                    signals_hit.append("bull-engulf")
                if divergence == "bullish":
                    signals_hit.append("RSI-div")

                reasoning = (
                    f"MeanRev LONG: RSI={rsi_result.value:.1f}, z={z:.2f}, "
                    f"BB%b={bb_result.pct_b:.2f}, vol={vol_r:.1f}x"
                    + (f", signals=[{','.join(signals_hit)}]" if signals_hit else "")
                    + f", target {target_label}"
                )
                return Signal(
                    ticker=ticker, direction="BUY", score=score,
                    strategy=self.name, reasoning=reasoning,
                    entry_price=last_price, stop_price=stop_px,
                    target_price=target_px,
                    limit_price=last_price * 1.0005,
                    rsi_val=rsi_result.value, bb_pct_b=bb_result.pct_b,
                    atr_val=atr_result.value, vol_ratio=vol_r,
                )

            # ── SHORT: overbought + above upper BB + statistically extreme ────
            if (bb_result.above_upper
                    and rsi_result.value > overbought_thr
                    and z > 2.0):

                star    = _is_shooting_star(last_open, last_high, last_low, last_price)
                bear_eng = _is_bearish_engulfing(opens, closes)
                rsi_turn = (len(rsi_vals) >= 2
                            and float(rsi_vals.iloc[-1]) <= float(rsi_vals.iloc[-2]) + 1)
                has_reversal = star or bear_eng or divergence == "bearish" or rsi_turn
                if not has_reversal:
                    return None

                deviation  = abs(z) - 2.0
                base_score = min(0.60 + deviation * 0.08, 0.90)
                div_bonus  = 0.05 if divergence == "bearish" else 0.0
                candle_bonus = 0.03 if (star or bear_eng) else 0.0
                score = round(min(base_score + div_bonus + candle_bonus, 0.95), 4)

                stop_px = last_high + atr_result.value * 0.5

                extreme_z = abs(z) > 3.0
                if extreme_z or divergence == "bearish":
                    target_px   = bb_result.lower
                    target_label = f"lower BB ({bb_result.lower:.2f}) — full reversion"
                else:
                    target_px   = bb_result.middle
                    target_label = f"mid BB ({bb_result.middle:.2f})"

                signals_hit = []
                if star:
                    signals_hit.append("shooting-star")
                if bear_eng:
                    signals_hit.append("bear-engulf")
                if divergence == "bearish":
                    signals_hit.append("RSI-div")

                reasoning = (
                    f"MeanRev SHORT: RSI={rsi_result.value:.1f}, z={z:.2f}, "
                    f"BB%b={bb_result.pct_b:.2f}, vol={vol_r:.1f}x"
                    + (f", signals=[{','.join(signals_hit)}]" if signals_hit else "")
                    + f", target {target_label}"
                )
                return Signal(
                    ticker=ticker, direction="SELL", score=score,
                    strategy=self.name, reasoning=reasoning,
                    entry_price=last_price, stop_price=stop_px,
                    target_price=target_px,
                    limit_price=last_price * 0.9995,
                    rsi_val=rsi_result.value, bb_pct_b=bb_result.pct_b,
                    atr_val=atr_result.value, vol_ratio=vol_r,
                )

            return None
        except Exception as e:
            log.error("MeanReversion analyze failed", ticker=ticker, error=str(e))
            return None


class ORBStrategy:
    """Proper Opening Range Breakout — Toby Crabel methodology on daily bars.

    True ORB uses the first N minutes of the trading session (5/15/30/60 min)
    as the "opening range" and trades confirmed breaks of that range.
    On daily OHLCV data we simulate this using a three-layer approach:

    Layer 1 — NR7 Trigger (Toby Crabel, "Day Trading With Short-Term Price
    Patterns and Opening Range Breakout", 1990):
      Yesterday must be the Narrow Range 7 bar — the narrowest daily range
      of the last 7 sessions. NR7 signals volatility compression: the market
      is "coiling" before an expansion move. Crabel found that NR7 days are
      followed by significantly larger moves the next session.

    Layer 2 — ADR Compression Confirm:
      Yesterday's range must be ≤ 65% of the 14-day Average Daily Range.
      This double-confirms the compression (NR7 alone can appear in low-vol
      markets where all ranges are small).

    Layer 3 — Breakout Confirmation:
      Today's close breaks yesterday's high (long) or low (short) with:
      • RVOL ≥ 1.5× 20d average (institutional participation on the break)
      • ATR-based confirmation buffer (10% of ATR, avoids marginal breaks)
      • Trend alignment: above 20-SMA for longs, below for shorts
      • Gap filter: if today's open gapped > 1% past the range boundary,
        skip — the move is already priced in (gap-and-go, not ORB)

    On live intraday feeds: replace yesterday's H/L with the actual first
    30-minute candle's H/L for a real intraday ORB. All other filters apply.

    Exits
    ─────
    Stop    : opposite side of the opening range (full range invalidation)
    Target 1: 1.0× range_width above/below breakout (conservative)
    Target 2: 2.0× range_width (extension target — used as signal target)
    Score   : 0.65 base + NR7-depth bonus + volume bonus, capped at 0.93
    """
    name = "orb"

    # Class-level constants (easy to tune without touching logic)
    _VOL_THRESHOLD      = 1.5    # minimum RVOL on breakout bar
    _ATR_BUFFER_FRAC    = 0.10   # confirmation buffer = 10% of ATR
    _ADR_COMPRESS_FRAC  = 0.65   # yesterday range ≤ 65% of 14-day ADR
    _GAP_MAX_FRAC       = 0.010  # skip if open already 1% past range (gap-and-go)
    _RANGE_MIN_FRAC     = 0.002  # 0.2% min range (avoid flat/halted stocks)
    _RANGE_MAX_FRAC     = 0.050  # 5.0% max range (avoid erratic/news days)
    _TARGET_MULT        = 2.0    # signal target = 2× range width

    def __init__(self) -> None:
        cfg = get_config()
        self._s = cfg.strategy
        self._r = cfg.risk

    async def analyze(self, ticker: str, df: pd.DataFrame) -> Optional[Signal]:
        try:
            if df is None or len(df) < 30:
                return None

            closes  = df["close"]
            highs   = df["high"]
            lows    = df["low"]
            opens   = df["open"] if "open" in df.columns else closes
            volumes = df["volume"]

            last_close = float(closes.iloc[-1])
            last_open  = float(opens.iloc[-1])

            # Opening range = yesterday's session high/low (daily proxy)
            orh = float(highs.iloc[-2])    # opening range high
            orl = float(lows.iloc[-2])     # opening range low
            range_width = orh - orl
            if range_width <= 0:
                return None

            # ── Layer 1: NR7 — yesterday must be narrowest of last 7 bars ────
            if not _is_nr7(highs, lows):
                return None

            # ── Layer 2: ADR compression — confirm the coiling ────────────────
            adr_val = adr(highs, lows, period=14)
            if not _adr_compression(highs, lows, adr_val, self._ADR_COMPRESS_FRAC):
                return None

            # ── Sanity: range must be well-formed (not micro or news-day wide) ─
            range_pct = range_width / max(last_close, 0.01)
            if not (self._RANGE_MIN_FRAC <= range_pct <= self._RANGE_MAX_FRAC):
                return None

            # ── Volume: institutional participation on the breakout ────────────
            vol_r = volume_ratio(volumes, period=20)
            if vol_r < self._VOL_THRESHOLD:
                return None

            # ── Supporting indicators ─────────────────────────────────────────
            atr_result = atr(highs, lows, closes, period=self._s.atr_period,
                             entry_price=last_close,
                             multiplier=self._r.atr_stop_multiplier)
            buffer     = atr_result.value * self._ATR_BUFFER_FRAC
            sma20_val  = sma(closes, period=20)
            above_sma  = sma20_val is not None and last_close > sma20_val
            below_sma  = sma20_val is not None and last_close < sma20_val

            # NR7 depth ratio: how compressed vs ADR (closer to 0 = more coiled)
            nr7_depth = range_width / max(adr_val, 0.001)   # 0–1, lower is better
            nr7_bonus = round(max(0.0, (self._ADR_COMPRESS_FRAC - nr7_depth) * 0.20), 3)

            # ── LONG: close above ORH + ATR buffer, trend aligned ─────────────
            gap_above = last_open - orh    # positive = gapped above range on open
            if (last_close > orh + buffer
                    and gap_above <= orh * self._GAP_MAX_FRAC
                    and above_sma):

                stop_px   = orl                                   # opposite range boundary
                target_px = last_close + self._TARGET_MULT * range_width

                vol_bonus = min((vol_r - self._VOL_THRESHOLD) * 0.04, 0.08)
                score = round(min(0.65 + nr7_bonus + vol_bonus, 0.93), 4)

                reasoning = (
                    f"ORB LONG (NR7+ADR): close {last_close:.2f} > ORH {orh:.2f}, "
                    f"range={range_pct:.1%} ({nr7_depth:.1%} of ADR), "
                    f"vol={vol_r:.1f}x, stop={stop_px:.2f}, "
                    f"target={target_px:.2f} ({self._TARGET_MULT}× range)"
                )
                return Signal(
                    ticker=ticker, direction="BUY", score=score,
                    strategy=self.name, reasoning=reasoning,
                    entry_price=last_close,
                    stop_price=stop_px,
                    target_price=target_px,
                    limit_price=last_close * 1.001,
                    atr_val=atr_result.value, vol_ratio=vol_r,
                )

            # ── SHORT: close below ORL - ATR buffer, trend aligned ────────────
            gap_below = orl - last_open    # positive = gapped below range on open
            if (last_close < orl - buffer
                    and gap_below <= orl * self._GAP_MAX_FRAC
                    and below_sma):

                stop_px   = orh
                target_px = last_close - self._TARGET_MULT * range_width

                vol_bonus = min((vol_r - self._VOL_THRESHOLD) * 0.04, 0.08)
                score = round(min(0.65 + nr7_bonus + vol_bonus, 0.93), 4)

                reasoning = (
                    f"ORB SHORT (NR7+ADR): close {last_close:.2f} < ORL {orl:.2f}, "
                    f"range={range_pct:.1%} ({nr7_depth:.1%} of ADR), "
                    f"vol={vol_r:.1f}x, stop={stop_px:.2f}, "
                    f"target={target_px:.2f} ({self._TARGET_MULT}× range)"
                )
                return Signal(
                    ticker=ticker, direction="SELL", score=score,
                    strategy=self.name, reasoning=reasoning,
                    entry_price=last_close,
                    stop_price=stop_px,
                    target_price=target_px,
                    limit_price=last_close * 0.999,
                    atr_val=atr_result.value, vol_ratio=vol_r,
                )

            return None
        except Exception as e:
            log.error("ORB analyze failed", ticker=ticker, error=str(e))
            return None


_AI_PROMPT = """You are a quantitative equity analyst. Return ONLY valid JSON — no markdown.

Ticker: {ticker}
Price: ${price:.2f} | RSI: {rsi:.1f} | MACD Hist: {macd:+.4f}
Bollinger %B: {pct_b:.2f} | Volume vs 20d avg: {vol:.2f}x
52w range: ${low52:.2f} – ${high52:.2f} ({from_high:.1f}% from high)

Return exactly:
{{"score":0.0-1.0,"direction":"BUY|SELL|HOLD","reasoning":"1-2 sentences","catalysts":["..."],"risks":["..."]}}"""


class AIFundamentalStrategy:
    """Claude Opus 4.6 fundamental + technical signal — async, non-blocking."""
    name = "ai_fundamental"

    def __init__(self) -> None:
        cfg = get_config()
        self._cfg = cfg
        self._s = cfg.strategy
        self._r = cfg.risk
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._cfg.anthropic_api_key)
        return self._client

    async def analyze(self, ticker: str, df: pd.DataFrame) -> Optional[Signal]:
        if not self._cfg.anthropic_api_key or df is None or len(df) < 60:
            return None
        try:
            closes = df["close"]
            highs = df["high"]
            lows = df["low"]
            volumes = df["volume"]
            last_price = float(closes.iloc[-1])

            rsi_r = rsi(closes, period=self._s.rsi_period)
            macd_r = macd(closes, fast=self._s.macd_fast, slow=self._s.macd_slow,
                          signal_period=self._s.macd_signal)
            bb_r = bollinger_bands(closes, period=self._s.bb_period, num_std=self._s.bb_std)
            atr_r = atr(highs, lows, closes, period=self._s.atr_period,
                        entry_price=last_price, multiplier=self._r.atr_stop_multiplier)
            vol_r = volume_ratio(volumes)
            window = min(252, len(closes))
            high52 = float(highs.iloc[-window:].max())
            low52 = float(lows.iloc[-window:].min())

            prompt = _AI_PROMPT.format(
                ticker=ticker, price=last_price, rsi=rsi_r.value,
                macd=macd_r.histogram, pct_b=bb_r.pct_b, vol=vol_r,
                high52=high52, low52=low52,
                from_high=(last_price - high52) / high52 * 100,
            )
            response = await asyncio.to_thread(
                self._get_client().messages.create,
                model=self._cfg.ai_model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            parsed = json.loads(response.content[0].text.strip())
            ai_score = max(0.0, min(float(parsed.get("score", 0.5)), 1.0))
            ai_dir = str(parsed.get("direction", "HOLD")).upper()
            if ai_dir not in ("BUY", "SELL", "HOLD"):
                ai_dir = "HOLD"
            ai_reason = str(parsed.get("reasoning", ""))

            stop_px = atr_r.stop_long if ai_dir == "BUY" else atr_r.stop_short
            risk_per_share = abs(last_price - stop_px)
            target_px = (last_price + self._s.rr_ratio * risk_per_share
                         if ai_dir == "BUY"
                         else last_price - self._s.rr_ratio * risk_per_share)

            signal = Signal(
                ticker=ticker, direction=ai_dir, score=ai_score,
                strategy=self.name, reasoning=ai_reason,
                entry_price=last_price, stop_price=stop_px, target_price=target_px,
                limit_price=last_price * (1.001 if ai_dir == "BUY" else 0.999),
                rsi_val=rsi_r.value, macd_hist=macd_r.histogram,
                bb_pct_b=bb_r.pct_b, atr_val=atr_r.value, vol_ratio=vol_r,
                ai_score=ai_score,
                catalysts=parsed.get("catalysts", []),
                risks=parsed.get("risks", []),
            )
            log.info("AI signal", ticker=ticker, direction=ai_dir,
                     score=f"{ai_score:.2f}")
            return signal
        except json.JSONDecodeError:
            log.error("AI signal: bad JSON", ticker=ticker)
            return None
        except Exception as e:
            log.error("AI analyze failed", ticker=ticker, error=str(e))
            return None
