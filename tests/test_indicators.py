"""Unit tests for technical indicators."""
import numpy as np
import pandas as pd
import pytest

from nexus.indicators import atr, bollinger_bands, macd, rsi, volume_ratio


def _closes(vals):
    return pd.Series(vals, dtype=float)


def _ohlcv(n=50, trend="up"):
    base = 100.0
    prices = []
    for i in range(n):
        if trend == "up":
            prices.append(base + i * 0.5 + np.random.normal(0, 0.1))
        elif trend == "down":
            prices.append(base + (n - i) * 0.5 + np.random.normal(0, 0.1))
        else:
            prices.append(base + np.random.normal(0, 1))
    closes = pd.Series(prices)
    highs = closes * 1.01
    lows = closes * 0.99
    vols = pd.Series([1_000_000] * n, dtype=float)
    return highs, lows, closes, vols


class TestRSI:
    def test_rsi_range(self):
        _, _, closes, _ = _ohlcv(50)
        result = rsi(closes, period=14)
        assert 0 <= result.value <= 100

    def test_rsi_overbought(self):
        # Strongly uptrending series should produce high RSI
        closes = _closes([100 + i * 2 for i in range(50)])
        result = rsi(closes, period=14)
        assert result.overbought or result.value > 60

    def test_rsi_oversold(self):
        closes = _closes([100 - i * 2 for i in range(50)])
        result = rsi(closes, period=14)
        assert result.oversold or result.value < 40

    def test_rsi_requires_enough_data(self):
        closes = _closes([100.0] * 5)
        result = rsi(closes, period=14)
        assert result.value == 50.0  # default/neutral on insufficient data


class TestMACD:
    def test_macd_returns_fields(self):
        _, _, closes, _ = _ohlcv(60)
        result = macd(closes, fast=12, slow=26, signal_period=9)
        assert hasattr(result, "macd")
        assert hasattr(result, "signal")
        assert hasattr(result, "histogram")
        assert hasattr(result, "bullish_cross")
        assert hasattr(result, "bearish_cross")

    def test_macd_not_both_cross(self):
        _, _, closes, _ = _ohlcv(60)
        result = macd(closes, fast=12, slow=26, signal_period=9)
        assert not (result.bullish_cross and result.bearish_cross)


class TestBollingerBands:
    def test_bands_ordered(self):
        _, _, closes, _ = _ohlcv(30)
        result = bollinger_bands(closes, period=20, num_std=2.0)
        assert result.lower <= result.middle <= result.upper

    def test_pct_b_range(self):
        _, _, closes, _ = _ohlcv(30)
        result = bollinger_bands(closes, period=20, num_std=2.0)
        # pct_b can briefly exceed [0,1] during strong trends — just check it's finite
        assert np.isfinite(result.pct_b)

    def test_bandwidth_positive(self):
        _, _, closes, _ = _ohlcv(30)
        result = bollinger_bands(closes, period=20, num_std=2.0)
        assert result.bandwidth >= 0


class TestATR:
    def test_atr_positive(self):
        highs, lows, closes, _ = _ohlcv(30)
        result = atr(highs, lows, closes, period=14, entry_price=100.0, multiplier=1.5)
        assert result.value > 0

    def test_stop_long_below_entry(self):
        highs, lows, closes, _ = _ohlcv(30)
        result = atr(highs, lows, closes, period=14, entry_price=100.0, multiplier=1.5)
        assert result.stop_long < 100.0

    def test_stop_short_above_entry(self):
        highs, lows, closes, _ = _ohlcv(30)
        result = atr(highs, lows, closes, period=14, entry_price=100.0, multiplier=1.5)
        assert result.stop_short > 100.0


class TestVolumeRatio:
    def test_normal_volume(self):
        _, _, _, vols = _ohlcv(30)
        ratio = volume_ratio(vols, period=20)
        assert 0.5 < ratio < 2.0

    def test_spike_volume(self):
        vols = pd.Series([1_000_000] * 29 + [5_000_000], dtype=float)
        ratio = volume_ratio(vols, period=20)
        assert ratio > 1.0
