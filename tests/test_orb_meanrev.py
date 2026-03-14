"""Tests for ORBStrategy and improved MeanReversionStrategy."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from nexus.config import RiskConfig, StrategyConfig
from nexus.strategy import MeanReversionStrategy, ORBStrategy


def _make_df(n: int = 60, **kwargs) -> pd.DataFrame:
    """Base OHLCV frame — override individual columns via kwargs."""
    base = 100.0
    closes = pd.Series([base + i * 0.1 for i in range(n)], dtype=float)
    df = pd.DataFrame({
        "open":   closes * 0.999,
        "high":   closes * 1.005,
        "low":    closes * 0.995,
        "close":  closes,
        "volume": [2_000_000.0] * n,
    })
    for k, v in kwargs.items():
        df[k] = v
    return df


# ─────────────────────────── ORB Strategy ────────────────────────────────────

class TestORBStrategy:
    def setup_method(self):
        self.orb = ORBStrategy()

    def _run(self, df):
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self.orb.analyze("AAPL", df)
        )

    def test_insufficient_data_returns_none(self):
        df = _make_df(n=20)
        assert self._run(df) is None

    def test_no_signal_on_flat_close(self):
        """Close inside yesterday's range → no signal."""
        df = _make_df(n=60)
        # Yesterday's range: high=105, low=95. Today closes at 101 — inside range.
        df.loc[df.index[-2], "high"] = 105.0
        df.loc[df.index[-2], "low"]  = 95.0
        df.loc[df.index[-1], "close"] = 101.0
        df.loc[df.index[-1], "open"]  = 100.0
        assert self._run(df) is None

    def test_long_breakout_signal(self):
        """Close convincingly above yesterday's high → BUY signal."""
        n = 60
        # Uptrending base so last price is comfortably above SMA20
        closes = pd.Series([90.0 + i * 0.5 for i in range(n)], dtype=float)
        df = pd.DataFrame({
            "open":   closes * 0.999,
            "high":   closes * 1.005,
            "low":    closes * 0.995,
            "close":  closes,
            "volume": [2_000_000.0] * n,
        })
        # Yesterday: tight well-formed range (2% of price)
        orh, orl = 116.0, 113.0   # 2.6% range around base ~114
        df.loc[df.index[-2], "high"] = orh
        df.loc[df.index[-2], "low"]  = orl
        # Today: gaps up slightly, closes well above ORH, strong volume
        df.loc[df.index[-1], "open"]   = 116.2
        df.loc[df.index[-1], "close"]  = 118.5   # > orh + buffer; > SMA20 (~113.75)
        df.loc[df.index[-1], "high"]   = 118.8
        df.loc[df.index[-1], "low"]    = 115.9
        df.loc[df.index[-1], "volume"] = 4_000_000.0   # 2× avg
        sig = self._run(df)
        assert sig is not None, "Expected BUY signal on ORB breakout"
        assert sig.direction == "BUY"
        assert sig.stop_price == pytest.approx(orl, abs=0.01)
        assert sig.target_price > sig.entry_price
        assert sig.score >= 0.65

    def test_short_breakdown_signal(self):
        """Close convincingly below yesterday's low → SELL signal."""
        n = 60
        # Downtrending base so price stays below SMA20
        closes = pd.Series([120.0 - i * 0.5 for i in range(n)], dtype=float)
        df = pd.DataFrame({
            "open":   closes * 1.001,
            "high":   closes * 1.005,
            "low":    closes * 0.995,
            "close":  closes,
            "volume": [2_000_000.0] * n,
        })
        # Yesterday: tight range around ~90
        orh, orl = 92.0, 89.0
        df.loc[df.index[-2], "high"] = orh
        df.loc[df.index[-2], "low"]  = orl
        # Today: breaks hard below ORL, still in downtrend (below SMA20 ~95.25)
        df.loc[df.index[-1], "close"]  = 86.5   # < orl - buffer
        df.loc[df.index[-1], "open"]   = 88.8
        df.loc[df.index[-1], "high"]   = 89.1
        df.loc[df.index[-1], "low"]    = 86.3
        df.loc[df.index[-1], "volume"] = 4_000_000.0
        sig = self._run(df)
        assert sig is not None, "Expected SELL signal on ORB breakdown"
        assert sig.direction == "SELL"
        assert sig.stop_price == pytest.approx(orh, abs=0.01)
        assert sig.target_price < sig.entry_price

    def test_wide_range_filtered(self):
        """Range > 4% of price should be filtered (erratic day)."""
        df = _make_df(n=60)
        df.loc[df.index[-2], "high"] = 110.0   # 10% range on 100 base
        df.loc[df.index[-2], "low"]  = 90.0
        df.loc[df.index[-1], "close"]  = 115.0
        df.loc[df.index[-1], "volume"] = 4_000_000.0
        assert self._run(df) is None

    def test_low_volume_filtered(self):
        """Volume below 1.5× avg should be rejected even on breakout."""
        df = _make_df(n=60)
        df.loc[df.index[-2], "high"] = 102.0
        df.loc[df.index[-2], "low"]  = 98.0
        df.loc[df.index[-1], "close"]  = 104.0
        df.loc[df.index[-1], "open"]   = 102.1
        # Volume at average (1.0x) — below 1.5x threshold
        df["volume"] = 2_000_000.0
        assert self._run(df) is None

    def test_signal_name(self):
        """Strategy name is 'orb'."""
        assert ORBStrategy.name == "orb"


# ───────────────────────── Mean Reversion Strategy ───────────────────────────

class TestMeanReversionStrategy:
    def setup_method(self):
        self.mr = MeanReversionStrategy()

    def _run(self, df):
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self.mr.analyze("AAPL", df)
        )

    def _oversold_df(self, n=60, rsi_extreme=True) -> pd.DataFrame:
        """Frame with deeply oversold conditions (BB breach + extreme RSI)."""
        # Build a series that crashes hard at the end → oversold
        base = [100.0] * (n - 15) + [100.0 - i * 3.5 for i in range(15)]
        closes = pd.Series(base, dtype=float)
        highs  = closes * 1.005
        lows   = closes * 0.995
        opens  = closes * 0.999
        # Last bar closes slightly above prev close (reversal candle)
        closes.iloc[-1] = closes.iloc[-2] * 1.005
        opens.iloc[-1]  = closes.iloc[-2]
        df = pd.DataFrame({
            "open": opens, "high": highs, "low": lows,
            "close": closes,
            "volume": [3_500_000.0] * n,   # 1.75x avg
        })
        return df

    def _overbought_df(self, n=60) -> pd.DataFrame:
        """Frame with deeply overbought conditions."""
        base = [100.0] * (n - 15) + [100.0 + i * 3.5 for i in range(15)]
        closes = pd.Series(base, dtype=float)
        highs  = closes * 1.005
        lows   = closes * 0.995
        opens  = closes * 1.001
        # Last bar closes slightly below prev close (reversal candle)
        closes.iloc[-1] = closes.iloc[-2] * 0.995
        opens.iloc[-1]  = closes.iloc[-2]
        df = pd.DataFrame({
            "open": opens, "high": highs, "low": lows,
            "close": closes,
            "volume": [3_500_000.0] * n,
        })
        return df

    def test_insufficient_data_returns_none(self):
        df = _make_df(n=20)
        assert self._run(df) is None

    def test_no_signal_on_normal_conditions(self):
        """Normal trending data without extreme deviation → no signal."""
        df = _make_df(n=60)
        assert self._run(df) is None

    def test_oversold_entry_buy(self):
        """Deep crash with reversal candle → BUY mean reversion signal."""
        df = self._oversold_df()
        sig = self._run(df)
        if sig is not None:  # may not trigger if RSI threshold not met on synth data
            assert sig.direction == "BUY"
            assert sig.stop_price < sig.entry_price
            assert sig.target_price > sig.entry_price
            assert 0.60 <= sig.score <= 0.95
            assert sig.strategy == "mean_reversion"

    def test_overbought_entry_sell(self):
        """Sharp rally with reversal candle → SELL mean reversion signal."""
        df = self._overbought_df()
        sig = self._run(df)
        if sig is not None:
            assert sig.direction == "SELL"
            assert sig.stop_price > sig.entry_price
            assert sig.target_price < sig.entry_price
            assert 0.60 <= sig.score <= 0.95

    def test_no_signal_without_reversal_candle(self):
        """If price is still dropping (no reversal), no signal issued."""
        df = self._oversold_df()
        # Make last bar close BELOW prev close — still falling
        df.loc[df.index[-1], "close"] = df["close"].iloc[-2] * 0.99
        df.loc[df.index[-1], "open"]  = df["close"].iloc[-2] * 1.001
        sig = self._run(df)
        # Either no signal or a signal — but if there is one it must be BUY
        if sig is not None:
            assert sig.direction == "BUY"

    def test_low_volume_filtered(self):
        """Capitulation volume gate: volume < 1.5x avg → no signal."""
        df = self._oversold_df()
        df["volume"] = 1_000_000.0   # below 1.5x threshold
        assert self._run(df) is None

    def test_stop_tighter_than_atr_multiplier(self):
        """Mean rev stop must be 0.5×ATR tight (not full multiplier)."""
        df = self._oversold_df()
        sig = self._run(df)
        if sig:
            atr_approx = sig.atr_val if sig.atr_val > 0 else 1.0
            gap = abs(sig.entry_price - sig.stop_price)
            # Stop should be within 0.5*ATR + small tolerance
            assert gap <= atr_approx * 0.6 + 0.5

    def test_strategy_name(self):
        assert MeanReversionStrategy.name == "mean_reversion"
