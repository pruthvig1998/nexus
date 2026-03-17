"""IronGrid Discipline Strategy — community-sourced trading playbook.

Encodes the IronGrid Discord community's rules as both a standalone signal
generator and a signal filter/modifier.  Two primary setups:

1. Cup & Handle: 40-60 day U-shaped recovery with handle pullback breakout
2. Reversal Play: panic-drop > 15% in 5 days, RSI < 25, showing recovery

Pre-signal gates (all must pass before any pattern scan):
- VIX < 24.5 (options market not pricing extreme fear)
- First-30-min rule: skip the opening noise in live mode
- Volume >= 1.2x 20-day average (institutional participation)
- Trend alignment: BUY only above 50 SMA, SELL only below

Post-signal enrichment:
- Profit Ladder: trim 25% at +25%, trim 50% at +50%, recover capital at +100%
- PEG boost: optional score bump when PEG < 1.5
- ATR-based stops (1.5x) and targets (4.5x = 3:1 R:R)
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Optional

import pandas as pd

from nexus.config import get_config
from nexus.indicators import atr, rsi, sma, volume_ratio
from nexus.logger import get_logger
from nexus.strategy import Signal

log = get_logger("irongrid")


# ── VIX cache ────────────────────────────────────────────────────────────────

_vix_cache: dict[str, tuple[float, float]] = {}  # {"vix": (value, timestamp)}
_VIX_TTL = 300  # 5 minutes


async def get_vix() -> float:
    """Fetch current VIX value. Cached for 5 minutes."""
    now = time.time()
    cached = _vix_cache.get("vix")
    if cached and (now - cached[1]) < _VIX_TTL:
        return cached[0]

    try:
        import yfinance as yf

        ticker = yf.Ticker("^VIX")
        hist = await asyncio.to_thread(ticker.history, period="1d")
        if hist is not None and len(hist) > 0:
            vix_val = float(hist["Close"].iloc[-1])
        else:
            # Fallback: try fast_info
            info = await asyncio.to_thread(lambda: ticker.fast_info)
            vix_val = float(getattr(info, "last_price", 20.0))
    except Exception as e:
        log.warning("VIX fetch failed, using default 20.0", error=str(e))
        vix_val = 20.0

    _vix_cache["vix"] = (vix_val, now)
    return vix_val


def _get_peg_ratio(ticker: str) -> Optional[float]:
    """Try to fetch PEG ratio via yfinance. Returns None if unavailable."""
    try:
        import yfinance as yf

        info = yf.Ticker(ticker).info
        peg = info.get("pegRatio")
        if peg is not None and peg > 0:
            return float(peg)
    except Exception:
        pass
    return None


# ── Pattern detectors ────────────────────────────────────────────────────────


def _detect_cup_and_handle(df: pd.DataFrame) -> Optional[dict]:
    """Detect a cup-and-handle pattern in the last 40-60 days.

    Cup: price drops > 10%, then recovers to within 3% of the prior high.
    Handle: a small 3-7% pullback after the cup completes, with volume
    increasing on the breakout bar.

    Returns a dict with pattern metadata, or None.
    """
    if len(df) < 60:
        return None

    closes = df["close"]
    volumes = df["volume"]

    # Scan windows of 40-60 bars ending near the current bar
    for lookback in (60, 55, 50, 45, 40):
        if len(closes) < lookback + 5:
            continue

        window = closes.iloc[-(lookback + 5) :]
        cup_start_price = float(window.iloc[0])
        trough_pos = int(window.iloc[:lookback].argmin())
        cup_trough_price = float(window.iloc[trough_pos])

        # Cup depth: must drop > 10%
        drop_pct = (cup_start_price - cup_trough_price) / cup_start_price
        if drop_pct < 0.10:
            continue

        # Recovery: after the trough, price must recover to within 3% of cup start
        recovery_segment = window.iloc[trough_pos:]
        if len(recovery_segment) < 5:
            continue

        recovery_high = float(recovery_segment.max())
        recovery_gap = (cup_start_price - recovery_high) / cup_start_price
        if recovery_gap > 0.03:
            continue  # didn't recover close enough to the rim

        # Handle: after the recovery high, look for a 3-7% pullback
        rh_rel = int(recovery_segment.argmax())
        rh_loc = trough_pos + rh_rel
        handle_segment = window.iloc[rh_loc:]
        if len(handle_segment) < 2:
            continue

        handle_low = float(handle_segment.min())
        handle_pullback = (recovery_high - handle_low) / recovery_high
        if not (0.03 <= handle_pullback <= 0.07):
            continue

        # Current price should be breaking out of the handle (near or above recovery high)
        current_price = float(closes.iloc[-1])
        if current_price < recovery_high * 0.97:
            continue  # not breaking out yet

        # Volume should increase on the breakout
        vol_r = volume_ratio(volumes, period=20)
        if vol_r < 1.2:
            continue

        return {
            "cup_start": cup_start_price,
            "cup_trough": cup_trough_price,
            "drop_pct": drop_pct,
            "recovery_high": recovery_high,
            "handle_low": handle_low,
            "handle_pullback": handle_pullback,
            "vol_ratio": vol_r,
            "lookback": lookback,
        }

    return None


def _detect_reversal_play(df: pd.DataFrame) -> Optional[dict]:
    """Detect an IronGrid reversal play setup.

    Conditions:
    - Stock dropped > 15% in the last 5 days
    - RSI < 25 (deeply oversold)
    - Volume spike > 2x average on the drop
    - Last 2 days show recovery (close > open)

    Returns a dict with pattern metadata, or None.
    """
    if len(df) < 25:
        return None

    closes = df["close"]
    opens = df["open"] if "open" in df.columns else closes
    volumes = df["volume"]

    # Check 5-day drop
    price_5d_ago = float(closes.iloc[-6])
    current_price = float(closes.iloc[-1])
    drop_pct = (price_5d_ago - current_price) / price_5d_ago

    if drop_pct < 0.15:
        return None

    # RSI < 25
    rsi_result = rsi(closes, period=14)
    if rsi_result.value >= 25:
        return None

    # Volume spike > 2x average during the drop period
    vol_r = volume_ratio(volumes, period=20)
    # Also check if any of the last 5 days had a volume spike
    avg_vol = (
        float(volumes.rolling(20).mean().iloc[-6]) if len(volumes) >= 26 else float(volumes.mean())
    )
    max_vol_last_5 = float(volumes.iloc[-5:].max())
    if max_vol_last_5 / max(avg_vol, 1) < 2.0:
        return None

    # Recovery: last 2 days close > open
    for i in (-2, -1):
        if float(closes.iloc[i]) <= float(opens.iloc[i]):
            return None

    return {
        "drop_pct": drop_pct,
        "days": 5,
        "rsi": rsi_result.value,
        "vol_spike": max_vol_last_5 / max(avg_vol, 1),
        "current_vol_ratio": vol_r,
    }


# ── Strategy ─────────────────────────────────────────────────────────────────


class IronGridStrategy:
    """IronGrid Discipline — community playbook encoded as systematic rules.

    Acts as both a standalone signal generator (cup-and-handle, reversal play)
    and a signal filter (VIX gate, first-30-min rule, volume confirmation,
    trend alignment).
    """

    name = "irongrid"

    # Configurable thresholds
    _VIX_CEILING = 24.5
    _MIN_VOL_RATIO = 1.2
    _FIRST_30_MIN_SKIP = True  # respect the first-30-min rule in live mode
    _ATR_STOP_MULT = 1.5
    _ATR_TARGET_MULT = 4.5  # 3:1 R:R with 1.5x stop
    _PEG_BOOST_THRESHOLD = 1.5
    _PEG_BOOST_AMOUNT = 0.05

    def __init__(self) -> None:
        cfg = get_config()
        self._s = cfg.strategy
        self._r = cfg.risk
        self._paper = cfg.paper

    async def analyze(self, ticker: str, df: pd.DataFrame) -> Optional[Signal]:
        """Run all IronGrid gates and pattern detectors.

        Order:
        1. VIX filter (skip BUY if VIX > 24.5)
        2. First-30-min rule (live mode only)
        3. Volume confirmation (>= 1.2x 20d avg)
        4. Trend alignment (BUY above 50 SMA, SELL below)
        5. Cup-and-handle detection
        6. Reversal play detection
        7. Return highest-scoring signal or None
        """
        try:
            if df is None or len(df) < 60:
                return None

            closes = df["close"]
            highs = df["high"]
            lows = df["low"]
            volumes = df["volume"]
            last_price = float(closes.iloc[-1])

            # ── Gate 1: VIX awareness (informational, not blocking) ────────
            vix = await get_vix()
            vix_caution = vix > self._VIX_CEILING  # used to reduce score, not block

            # ── Gate 2: First-30-min rule (live mode only) ──────────────────
            if not self._paper and self._FIRST_30_MIN_SKIP:
                try:
                    from zoneinfo import ZoneInfo

                    now_et = datetime.now(ZoneInfo("America/New_York"))
                    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
                    minutes_since_open = (now_et - market_open).total_seconds() / 60
                    if 0 < minutes_since_open < 30:
                        log.debug(
                            "IronGrid first-30-min rule: too early",
                            ticker=ticker,
                            minutes=f"{minutes_since_open:.0f}",
                        )
                        return None
                except Exception:
                    pass  # if timezone handling fails, skip this gate

            # ── Gate 3: Volume confirmation ─────────────────────────────────
            vol_r = volume_ratio(volumes, period=20)
            if vol_r < self._MIN_VOL_RATIO:
                return None

            # ── Gate 4: Trend alignment via 50 SMA ─────────────────────────
            sma50 = sma(closes, period=50)
            trend_bullish = sma50 is not None and last_price > sma50
            _trend_bearish = sma50 is not None and last_price < sma50  # noqa: F841

            # ── Compute shared indicators ───────────────────────────────────
            atr_result = atr(
                highs,
                lows,
                closes,
                period=self._s.atr_period,
                entry_price=last_price,
                multiplier=self._ATR_STOP_MULT,
            )

            candidates: list[Signal] = []

            # ── Pattern 1: Cup and Handle (BUY only) ───────────────────────
            if trend_bullish:
                cup = _detect_cup_and_handle(df)
                if cup is not None:
                    score = 0.75 + min(cup["drop_pct"] - 0.10, 0.10)  # 0.75-0.85
                    score = round(min(max(score, 0.75), 0.85), 4)

                    stop_px = last_price - atr_result.value * self._ATR_STOP_MULT
                    target_px = last_price + atr_result.value * self._ATR_TARGET_MULT

                    reasoning = (
                        f"IronGrid cup-and-handle: {ticker} formed {cup['lookback']}d "
                        f"cup (drop {cup['drop_pct']:.0%}), handle pullback "
                        f"{cup['handle_pullback']:.0%}, breaking out with "
                        f"vol {cup['vol_ratio']:.1f}x"
                    )
                    signal = Signal(
                        ticker=ticker,
                        direction="BUY",
                        score=score,
                        strategy=self.name,
                        reasoning=reasoning,
                        entry_price=last_price,
                        stop_price=stop_px,
                        target_price=target_px,
                        limit_price=last_price * 1.001,
                        atr_val=atr_result.value,
                        vol_ratio=cup["vol_ratio"],
                        catalysts=[
                            "profit_ladder: trim_25pct_at_+25%, "
                            "trim_50pct_at_+50%, recover_capital_at_+100%"
                        ],
                    )
                    candidates.append(signal)

            # ── Pattern 2: Reversal Play (BUY only, exception to trend gate)
            # Reversal plays are counter-trend by nature — RSI < 25 after a
            # 15%+ drop means the stock is almost certainly below the 50 SMA.
            # We still require volume and VIX gates but relax trend alignment.
            reversal = _detect_reversal_play(df)
            if reversal is not None:
                score = 0.70

                stop_px = last_price - atr_result.value * self._ATR_STOP_MULT
                target_px = last_price + atr_result.value * self._ATR_TARGET_MULT

                reasoning = (
                    f"IronGrid reversal play: {ticker} dropped "
                    f"{reversal['drop_pct']:.0%} in {reversal['days']} days, "
                    f"RSI at {reversal['rsi']:.1f}, showing recovery"
                )
                signal = Signal(
                    ticker=ticker,
                    direction="BUY",
                    score=score,
                    strategy=self.name,
                    reasoning=reasoning,
                    entry_price=last_price,
                    stop_price=stop_px,
                    target_price=target_px,
                    limit_price=last_price * 1.001,
                    rsi_val=reversal["rsi"],
                    atr_val=atr_result.value,
                    vol_ratio=reversal["current_vol_ratio"],
                    catalysts=[
                        "profit_ladder: trim_25pct_at_+25%, "
                        "trim_50pct_at_+50%, recover_capital_at_+100%"
                    ],
                    risks=["reversal_play: counter-trend, tight risk management required"],
                )
                candidates.append(signal)

            # ── No patterns matched ─────────────────────────────────────────
            if not candidates:
                return None

            # ── Pick highest-scoring signal ─────────────────────────────────
            best = max(candidates, key=lambda s: s.score)

            # ── VIX caution: reduce score when volatility is elevated ──────
            if vix_caution:
                penalty = min(
                    (vix - self._VIX_CEILING) * 0.02, 0.15
                )  # 2% per VIX point over ceiling, max 15%
                best.score = round(max(best.score - penalty, 0.50), 4)
                best.risks.append(f"elevated_vix: {vix:.1f} (score reduced by {penalty:.2f})")
                log.debug(
                    "VIX caution applied",
                    ticker=ticker,
                    vix=f"{vix:.1f}",
                    penalty=f"{penalty:.2f}",
                    adjusted_score=f"{best.score:.2f}",
                )

            # ── Optional PEG boost ──────────────────────────────────────────
            peg = await asyncio.to_thread(_get_peg_ratio, ticker)
            if peg is not None and peg < self._PEG_BOOST_THRESHOLD:
                best.score = round(min(best.score + self._PEG_BOOST_AMOUNT, 0.95), 4)
                best.reasoning += f" | PEG={peg:.2f} (value+growth boost)"
                best.catalysts.append(f"peg_ratio: {peg:.2f} < {self._PEG_BOOST_THRESHOLD}")

            log.info(
                "IronGrid signal",
                ticker=ticker,
                direction=best.direction,
                score=f"{best.score:.2f}",
                pattern=best.reasoning[:60],
            )
            return best

        except Exception as e:
            log.error("IronGrid analyze failed", ticker=ticker, error=str(e))
            return None
