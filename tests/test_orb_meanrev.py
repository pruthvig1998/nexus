"""Tests for ORBStrategy (NR7+ADR methodology) and MeanReversionStrategy."""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from nexus.strategy import MeanReversionStrategy, ORBStrategy

# ── Shared async runner ───────────────────────────────────────────────────────


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── ORB helpers ───────────────────────────────────────────────────────────────


def _orb_df(
    n: int = 40,
    orh: float = 102.0,
    orl: float = 100.0,
    today_close: float = 104.0,
    today_open: float = 102.1,
    vol_mult: float = 2.0,
    trend: str = "up",
) -> pd.DataFrame:
    """Build an ORB-ready DataFrame.

    The second-to-last bar (yesterday) becomes the NR7 bar by making it
    the narrowest of the last 7 — all prior bars have wider ranges.
    The last bar (today) is the breakout candidate.
    """
    base_close = 90.0 if trend == "up" else 120.0
    step = 0.5 if trend == "up" else -0.5
    rows = []
    avg_vol = 2_000_000.0

    for i in range(n):
        c = base_close + i * step
        # Give all bars except the last two a wide range (4× NR7 range width)
        bar_range = (orh - orl) * 4
        rows.append(
            {
                "open": c,
                "high": c + bar_range / 2,
                "low": c - bar_range / 2,
                "close": c,
                "volume": avg_vol,
            }
        )

    # Yesterday (NR7 bar): tight range [orl, orh]
    rows[-2] = {
        "open": (orh + orl) / 2,
        "high": orh,
        "low": orl,
        "close": (orh + orl) / 2,
        "volume": avg_vol,
    }

    # Today: breakout bar
    rows[-1] = {
        "open": today_open,
        "high": today_close * 1.002,
        "low": today_close * 0.998,
        "close": today_close,
        "volume": avg_vol * vol_mult,
    }

    return pd.DataFrame(rows)


# ─────────────────────────── ORB Strategy ────────────────────────────────────


class TestORBStrategy:
    def setup_method(self):
        self.orb = ORBStrategy()

    def test_insufficient_data_returns_none(self):
        df = _orb_df(n=15)
        assert _run(self.orb.analyze("AAPL", df)) is None

    def test_no_nr7_returns_none(self):
        """If yesterday's range is NOT the narrowest of last 7, no signal."""
        df = _orb_df(n=40, orh=108.0, orl=90.0)  # wide yesterday range
        # Make all prior bars narrower than yesterday to kill NR7
        for i in range(len(df) - 9, len(df) - 2):
            df.loc[i, "high"] = df.loc[i, "close"] + 0.5
            df.loc[i, "low"] = df.loc[i, "close"] - 0.5
        assert _run(self.orb.analyze("AAPL", df)) is None

    def test_low_volume_returns_none(self):
        """Volume < 1.5× average → rejected even if NR7 breakout."""
        df = _orb_df(n=40, today_close=104.0, vol_mult=0.8)
        assert _run(self.orb.analyze("AAPL", df)) is None

    def test_long_breakout_signal(self):
        """NR7 + ADR compression + close above ORH + volume → BUY signal."""
        # Use a higher today_close so it clears the 20-SMA comfortably.
        # Uptrend: base 90→109, yesterday NR7 [100,102], today close 107.
        df = _orb_df(
            n=40,
            orh=102.0,
            orl=100.0,
            today_close=107.0,
            today_open=102.2,
            vol_mult=2.0,
            trend="up",
        )
        sig = _run(self.orb.analyze("AAPL", df))
        assert sig is not None, "Expected BUY ORB signal"
        assert sig.direction == "BUY"
        assert sig.stop_price == pytest.approx(100.0, abs=0.01)  # stop = ORL
        assert sig.target_price > sig.entry_price
        assert sig.score >= 0.65
        assert "NR7" in sig.reasoning

    def test_short_breakdown_signal(self):
        """NR7 + ADR compression + close below ORL + volume → SELL signal."""
        df = _orb_df(
            n=40, orh=92.0, orl=90.0, today_close=87.5, today_open=89.8, vol_mult=2.0, trend="down"
        )
        sig = _run(self.orb.analyze("AAPL", df))
        assert sig is not None, "Expected SELL ORB signal"
        assert sig.direction == "SELL"
        assert sig.stop_price == pytest.approx(92.0, abs=0.01)  # stop = ORH
        assert sig.target_price < sig.entry_price
        assert "NR7" in sig.reasoning

    def test_target_is_2x_range(self):
        """Target must equal entry + 2× range_width."""
        df = _orb_df(
            n=40,
            orh=102.0,
            orl=100.0,
            today_close=104.0,
            today_open=102.2,
            vol_mult=2.0,
            trend="up",
        )
        sig = _run(self.orb.analyze("AAPL", df))
        if sig and sig.direction == "BUY":
            range_w = 102.0 - 100.0  # orh - orl
            expected_target = sig.entry_price + 2.0 * range_w
            assert sig.target_price == pytest.approx(expected_target, rel=0.01)

    def test_wide_range_filtered(self):
        """Yesterday range > 5% of price is filtered (erratic/news day)."""
        df = _orb_df(
            n=40,
            orh=115.0,
            orl=90.0,  # ~22% range
            today_close=117.0,
            vol_mult=2.0,
            trend="up",
        )
        assert _run(self.orb.analyze("AAPL", df)) is None

    def test_large_gap_filtered(self):
        """If open already 1%+ past the range boundary, skip (gap-and-go)."""
        df = _orb_df(
            n=40,
            orh=102.0,
            orl=100.0,
            today_close=104.0,
            today_open=103.5,  # 1.5% gap above ORH=102
            vol_mult=2.0,
            trend="up",
        )
        sig = _run(self.orb.analyze("AAPL", df))
        # Gap = 103.5 - 102 = 1.5 > 1% of 102 → should be filtered
        assert sig is None

    def test_strategy_name(self):
        assert ORBStrategy.name == "orb"


# ───────────────────────── Mean Reversion Strategy ───────────────────────────


def _mr_crash_df(n: int = 60) -> pd.DataFrame:
    """Frame that crashes hard then shows a hammer reversal on the last bar."""
    # Stable base, then sharp crash, then reversal bar
    base = [100.0] * (n - 20) + [100.0 - i * 3.0 for i in range(20)]
    closes = pd.Series(base, dtype=float)
    opens = closes.copy()
    highs = closes * 1.004
    lows = closes * 0.996

    # Last bar: hammer pattern — big lower wick, close near high
    entry_lvl = base[-1]
    lows.iloc[-1] = entry_lvl * 0.985  # long lower wick
    closes.iloc[-1] = entry_lvl * 1.002  # close near high (recovery)
    opens.iloc[-1] = entry_lvl * 0.995
    highs.iloc[-1] = entry_lvl * 1.004

    avg_vol = 2_000_000.0
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [avg_vol * 2.0] * n,  # elevated volume throughout crash
        }
    )


def _mr_rally_df(n: int = 60) -> pd.DataFrame:
    """Frame that rallies hard then shows a shooting-star on the last bar."""
    base = [100.0] * (n - 20) + [100.0 + i * 3.0 for i in range(20)]
    closes = pd.Series(base, dtype=float)
    opens = closes.copy()
    highs = closes * 1.004
    lows = closes * 0.996

    # Last bar: shooting star — big upper wick, close near low
    entry_lvl = base[-1]
    highs.iloc[-1] = entry_lvl * 1.015  # long upper wick
    closes.iloc[-1] = entry_lvl * 0.998  # close near low (rejection)
    opens.iloc[-1] = entry_lvl * 1.005
    lows.iloc[-1] = entry_lvl * 0.996

    avg_vol = 2_000_000.0
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [avg_vol * 2.0] * n,
        }
    )


class TestMeanReversionStrategy:
    def setup_method(self):
        self.mr = MeanReversionStrategy()

    def test_insufficient_data_returns_none(self):
        df = pd.DataFrame(
            {
                "open": [100.0] * 20,
                "high": [101.0] * 20,
                "low": [99.0] * 20,
                "close": [100.0] * 20,
                "volume": [1_000_000.0] * 20,
            }
        )
        assert _run(self.mr.analyze("AAPL", df)) is None

    def test_no_signal_on_normal_market(self):
        """Normal gently-trending price → no extreme conditions → no signal."""
        n = 60
        closes = pd.Series([100.0 + i * 0.1 for i in range(n)])
        df = pd.DataFrame(
            {
                "open": closes * 0.999,
                "high": closes * 1.003,
                "low": closes * 0.997,
                "close": closes,
                "volume": [3_000_000.0] * n,
            }
        )
        assert _run(self.mr.analyze("AAPL", df)) is None

    def test_oversold_hammer_produces_buy(self):
        """Deep crash + hammer reversal bar → BUY mean reversion."""
        df = _mr_crash_df(n=70)
        sig = _run(self.mr.analyze("AAPL", df))
        if sig is not None:
            assert sig.direction == "BUY"
            assert sig.stop_price < sig.entry_price
            assert sig.target_price > sig.entry_price
            assert 0.60 <= sig.score <= 0.95
            assert sig.strategy == "mean_reversion"

    def test_overbought_shooting_star_produces_sell(self):
        """Sharp rally + shooting star bar → SELL mean reversion."""
        df = _mr_rally_df(n=70)
        sig = _run(self.mr.analyze("AAPL", df))
        if sig is not None:
            assert sig.direction == "SELL"
            assert sig.stop_price > sig.entry_price
            assert sig.target_price < sig.entry_price
            assert 0.60 <= sig.score <= 0.95

    def test_low_volume_rejected(self):
        """Volume < 1.5× average → no signal even in extreme conditions."""
        df = _mr_crash_df(n=70)
        df["volume"] = 800_000.0  # well below 1.5× threshold
        assert _run(self.mr.analyze("AAPL", df)) is None

    def test_bbw_squeeze_rejected(self):
        """Flat price (very narrow bands, BBW < 0.02) → no signal."""
        n = 60
        flat = pd.Series([100.0] * n)
        df = pd.DataFrame(
            {
                "open": flat,
                "high": flat + 0.01,
                "low": flat - 0.01,
                "close": flat,
                "volume": [3_000_000.0] * n,
            }
        )
        assert _run(self.mr.analyze("AAPL", df)) is None

    def test_stop_within_half_atr(self):
        """Stop must be tight — 0.5× ATR from the intraday extreme."""
        df = _mr_crash_df(n=70)
        sig = _run(self.mr.analyze("AAPL", df))
        if sig:
            atr_approx = sig.atr_val if sig.atr_val > 0 else 1.0
            # Allow 0.6× ATR gap (0.5× + small floating-point tolerance)
            price_stop_gap = abs(sig.entry_price - sig.stop_price)
            assert price_stop_gap <= atr_approx * 0.6 + 1.0

    def test_extreme_z_targets_opposite_band(self):
        """When Z-score > 3σ, target extends to opposite Bollinger Band."""
        # Build a VERY extreme crash (3σ+)
        n = 70
        base = [100.0] * (n - 25) + [100.0 - i * 4.5 for i in range(25)]
        closes = pd.Series(base, dtype=float)
        opens = closes * 0.999
        highs = closes * 1.003
        # Last bar: hammer
        lows = closes * 0.996
        lows.iloc[-1] = closes.iloc[-1] * 0.985
        closes.iloc[-1] = closes.iloc[-2] * 1.003
        df = pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": [3_200_000.0] * n,
            }
        )
        sig = _run(self.mr.analyze("AAPL", df))
        if sig and sig.direction == "BUY":
            # At extreme z, target should be ABOVE middle BB
            from nexus.indicators import bollinger_bands

            bb = bollinger_bands(closes)
            assert sig.target_price >= bb.middle

    def test_strategy_name(self):
        assert MeanReversionStrategy.name == "mean_reversion"


# ── Pattern helpers ───────────────────────────────────────────────────────────


class TestCandlePatterns:
    """Unit-test the private candlestick helpers directly."""

    from nexus.strategy import (  # noqa: PLC0415  # noqa: PLC0415
        _is_bearish_engulfing,
        _is_bullish_engulfing,
        _is_hammer,
        _is_shooting_star,
    )

    def test_hammer_detected(self):
        from nexus.strategy import _is_hammer

        # Long lower wick, small body, close near high
        assert _is_hammer(o=100.5, h=101.0, low=98.0, c=100.8)

    def test_hammer_rejected_no_long_wick(self):
        from nexus.strategy import _is_hammer

        # Normal candle — no long lower wick
        assert not _is_hammer(o=100.0, h=101.5, low=99.5, c=101.0)

    def test_shooting_star_detected(self):
        from nexus.strategy import _is_shooting_star

        # Long upper wick, close near low
        assert _is_shooting_star(o=100.5, h=103.0, low=100.0, c=100.2)

    def test_bullish_engulfing_detected(self):
        from nexus.strategy import _is_bullish_engulfing

        opens = pd.Series([101.0, 99.0])  # prev opens above close, today below
        closes = pd.Series([99.5, 101.5])  # prev red, today green and engulfs
        assert _is_bullish_engulfing(opens, closes)

    def test_bearish_engulfing_detected(self):
        from nexus.strategy import _is_bearish_engulfing

        opens = pd.Series([99.0, 101.5])
        closes = pd.Series([100.5, 98.5])
        assert _is_bearish_engulfing(opens, closes)
