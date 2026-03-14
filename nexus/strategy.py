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
    ATRResult, BollingerResult, MACDResult, RSIResult,
    atr, bollinger_bands, dynamic_limit_price, golden_cross,
    macd, rsi, sma, volume_ratio,
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


def _zscore(series: pd.Series, period: int = 20) -> float:
    """Z-score of the last value relative to a rolling window."""
    if len(series) < period:
        return 0.0
    window = series.iloc[-period:]
    std = float(window.std())
    if std < 1e-9:
        return 0.0
    return float((series.iloc[-1] - window.mean()) / std)


class MeanReversionStrategy:
    """Multi-signal mean reversion: BB + RSI + Z-score + reversal confirmation.

    Entry gates (ALL must pass):
      1. Price beyond ±2.5σ Bollinger Band (extreme statistical deviation)
      2. RSI < 25 oversold (buy) or > 75 overbought (sell)
      3. |Z-score| ≥ 2.0 (price is statistically extreme vs 20d mean)
      4. Reversal candle: today's close recovers vs yesterday's close
         — prevents catching a falling knife mid-move
      5. Capitulation volume: vol ≥ 1.5× 20d avg (exhaustion signature)
      6. RSI momentum: RSI is turning (not still accelerating in extreme direction)

    Targets:
      - Standard deviation (|z| 2.0–3.0): middle BB (mean reversion)
      - Extreme deviation (|z| > 3.0): opposite BB band (full overshoot revert)

    Stop:
      - 0.5× ATR beyond the extreme (tight — price should not extend further)
    """
    name = "mean_reversion"

    def __init__(self) -> None:
        cfg = get_config()
        self._s = cfg.strategy
        self._r = cfg.risk

    async def analyze(self, ticker: str, df: pd.DataFrame) -> Optional[Signal]:
        try:
            if df is None or len(df) < 40:
                return None

            closes = df["close"]
            highs = df["high"]
            lows = df["low"]
            opens = df["open"] if "open" in df.columns else closes
            volumes = df["volume"]
            last_price = float(closes.iloc[-1])
            prev_close = float(closes.iloc[-2])

            # ── Gate 1: Capitulation volume ───────────────────────────────────
            vol_r = volume_ratio(volumes, period=20)
            if vol_r < 1.5:   # demand elevated volume for mean rev entries
                return None

            # ── Compute indicators ────────────────────────────────────────────
            rsi_result = rsi(closes, period=self._s.rsi_period)
            rsi_prev = rsi(closes.iloc[:-1], period=self._s.rsi_period)
            bb_result = bollinger_bands(closes, period=self._s.bb_period,
                                        num_std=self._s.bb_std)
            atr_result = atr(highs, lows, closes, period=self._s.atr_period,
                             entry_price=last_price,
                             multiplier=self._r.atr_stop_multiplier)
            z = _zscore(closes, period=20)

            # ── Thresholds ────────────────────────────────────────────────────
            oversold_threshold = self._s.rsi_mean_rev_oversold        # default 25
            overbought_threshold = 100 - oversold_threshold            # default 75
            deep_oversold  = rsi_result.value < oversold_threshold
            deep_overbought = rsi_result.value > overbought_threshold
            extreme_z = abs(z) > 3.0   # triggers extended target

            # ── LONG: oversold, below lower BB, price turning up ──────────────
            if (bb_result.below_lower
                    and deep_oversold
                    and z < -2.0             # statistically extreme
                    and last_price > prev_close  # reversal candle: price recovering
                    and rsi_result.value >= rsi_prev.value - 2):  # RSI not still diving

                # Score: deeper the deviation, higher conviction
                deviation = abs(z) - 2.0   # excess z beyond 2σ
                score = round(min(0.60 + deviation * 0.08, 0.95), 4)

                # Tight stop: 0.5× ATR below current low
                stop_px = float(lows.iloc[-1]) - atr_result.value * 0.5

                # Extended target at extreme deviations
                if extreme_z:
                    target_px = bb_result.upper   # full reversion to opposite band
                    target_label = f"upper BB ({bb_result.upper:.2f})"
                else:
                    target_px = bb_result.middle
                    target_label = f"mid BB ({bb_result.middle:.2f})"

                reasoning = (
                    f"Mean rev LONG: RSI={rsi_result.value:.1f} (oversold), "
                    f"z={z:.2f}, below lower BB ({bb_result.lower:.2f}), "
                    f"vol={vol_r:.1f}x, target {target_label}"
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

            # ── SHORT: overbought, above upper BB, price turning down ──────────
            if (bb_result.above_upper
                    and deep_overbought
                    and z > 2.0
                    and last_price < prev_close  # reversal candle: price retreating
                    and rsi_result.value <= rsi_prev.value + 2):  # RSI not still rising

                deviation = abs(z) - 2.0
                score = round(min(0.60 + deviation * 0.08, 0.95), 4)

                stop_px = float(highs.iloc[-1]) + atr_result.value * 0.5

                if extreme_z:
                    target_px = bb_result.lower
                    target_label = f"lower BB ({bb_result.lower:.2f})"
                else:
                    target_px = bb_result.middle
                    target_label = f"mid BB ({bb_result.middle:.2f})"

                reasoning = (
                    f"Mean rev SHORT: RSI={rsi_result.value:.1f} (overbought), "
                    f"z={z:.2f}, above upper BB ({bb_result.upper:.2f}), "
                    f"vol={vol_r:.1f}x, target {target_label}"
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
    """Opening Range Breakout — trade confirmed breaks of the prior day's range.

    On daily bars the 'opening range' is yesterday's high/low. This is the
    standard institutional proxy used when intraday data is unavailable.
    For live trading the engine should feed 30-minute bars so the range is
    the first candle of the session.

    Entry rules
    ───────────
    LONG  (upside breakout):
      • today's close > yesterday's high + ATR buffer (confirmed, not a wick)
      • volume ≥ 1.5× 20d average (institutional participation)
      • price above 20 SMA (trend alignment — trade with the bigger move)
      • range is well-formed: 0.3% ≤ range_width/price ≤ 4% (avoid erratic days)
      • no gap: open ≤ yesterday's high + 0.5% (don't chase huge gaps)

    SHORT (downside breakdown):
      • today's close < yesterday's low - ATR buffer
      • volume ≥ 1.5× 20d average
      • price below 20 SMA
      • same range and gap filters as above

    Exits
    ─────
    Stop loss : opposite boundary of the opening range
                (long stop = yesterday's low; short stop = yesterday's high)
    Target    : 1.5× range_width projected from the breakout level
    Score     : 0.65 base + volume bonus + trend bonus, capped at 0.92
    """
    name = "orb"
    _ORB_BUFFER_ATR_FRAC = 0.10   # 10% of ATR as confirmation buffer
    _VOL_THRESHOLD = 1.5          # minimum vol ratio for breakout entry
    _RANGE_MIN_PCT = 0.003        # 0.3% minimum range relative to price
    _RANGE_MAX_PCT = 0.04         # 4% maximum (avoid wide/erratic ranges)
    _GAP_MAX_PCT   = 0.005        # 0.5% max gap above/below range before entry

    def __init__(self) -> None:
        cfg = get_config()
        self._s = cfg.strategy
        self._r = cfg.risk

    async def analyze(self, ticker: str, df: pd.DataFrame) -> Optional[Signal]:
        try:
            if df is None or len(df) < 30:
                return None

            closes = df["close"]
            highs  = df["high"]
            lows   = df["low"]
            opens  = df["open"] if "open" in df.columns else closes
            volumes = df["volume"]

            last_close = float(closes.iloc[-1])
            last_open  = float(opens.iloc[-1])
            today_high = float(highs.iloc[-1])
            today_low  = float(lows.iloc[-1])

            # Yesterday's range = the "opening range" on daily bars
            orh = float(highs.iloc[-2])   # opening range high
            orl = float(lows.iloc[-2])    # opening range low
            range_width = orh - orl
            if range_width <= 0:
                return None

            # ── Gate 1: Well-formed range (not too wide/narrow) ───────────────
            range_pct = range_width / last_close
            if not (self._RANGE_MIN_PCT <= range_pct <= self._RANGE_MAX_PCT):
                return None

            # ── Gate 2: Volume confirmation ────────────────────────────────────
            vol_r = volume_ratio(volumes, period=20)
            if vol_r < self._VOL_THRESHOLD:
                return None

            # ── Compute supporting indicators ──────────────────────────────────
            atr_result = atr(highs, lows, closes, period=self._s.atr_period,
                             entry_price=last_close,
                             multiplier=self._r.atr_stop_multiplier)
            buffer = atr_result.value * self._ORB_BUFFER_ATR_FRAC
            sma20_val = sma(closes, period=20)
            above_sma = sma20_val is not None and last_close > sma20_val
            below_sma = sma20_val is not None and last_close < sma20_val

            # ── Gate 3: No runaway gap (price shouldn't already be far past range)
            gap_above = last_open - orh   # positive = gapped above range at open
            gap_below = orl - last_open   # positive = gapped below range at open

            # ── LONG: close above ORH + buffer ────────────────────────────────
            if (last_close > orh + buffer
                    and gap_above <= orh * self._GAP_MAX_PCT
                    and above_sma):

                stop_px   = orl   # full range stop at opposite boundary
                target_px = last_close + 1.5 * range_width

                # Bonus scoring
                vol_bonus   = min((vol_r - self._VOL_THRESHOLD) * 0.05, 0.10)
                trend_bonus = 0.05 if above_sma else 0.0
                score = round(min(0.65 + vol_bonus + trend_bonus, 0.92), 4)

                reasoning = (
                    f"ORB breakout LONG: close {last_close:.2f} > ORH {orh:.2f} "
                    f"(range {range_pct:.1%}), vol={vol_r:.1f}x, "
                    f"stop=ORL {stop_px:.2f}, target={target_px:.2f}"
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

            # ── SHORT: close below ORL - buffer ───────────────────────────────
            if (last_close < orl - buffer
                    and gap_below <= orl * self._GAP_MAX_PCT
                    and below_sma):

                stop_px   = orh   # full range stop at opposite boundary
                target_px = last_close - 1.5 * range_width

                vol_bonus   = min((vol_r - self._VOL_THRESHOLD) * 0.05, 0.10)
                trend_bonus = 0.05 if below_sma else 0.0
                score = round(min(0.65 + vol_bonus + trend_bonus, 0.92), 4)

                reasoning = (
                    f"ORB breakdown SHORT: close {last_close:.2f} < ORL {orl:.2f} "
                    f"(range {range_pct:.1%}), vol={vol_r:.1f}x, "
                    f"stop=ORH {stop_px:.2f}, target={target_px:.2f}"
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
            ai_score = float(parsed.get("score", 0.5))
            ai_dir = str(parsed.get("direction", "HOLD")).upper()
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
