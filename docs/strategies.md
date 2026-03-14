# Trading Strategies

NEXUS v3 uses three composable strategies that all generate both BUY (long entry) and SELL (short entry) signals.

## 1. MomentumStrategy

**Thesis:** Stocks with strong momentum continue in the same direction.

### Long Entry (BUY signal)
All of the following must agree:
- RSI < 70 (not overbought) and trending up
- MACD histogram bullish cross (hist flips positive)
- Price above 50-day SMA (trend regime)
- Volume > 1.2× 20-day average

### Short Entry (SELL signal)
All of the following must agree:
- RSI > 30 (not oversold) and trending down
- MACD histogram bearish cross (hist flips negative)
- Price below 50-day SMA (downtrend regime)
- Volume > 1.2× 20-day average

### Exit Rules
- **Stop**: 1.5× ATR from entry
- **Target**: 3× risk (3:1 R:R ratio)
- Stop/target checked every scan cycle (60s default)

---

## 2. MeanReversionStrategy

**Thesis:** Extreme deviations from the mean tend to revert.

### Long Entry (BUY signal)
- Price below Bollinger lower band (pct_b < 0.05)
- RSI < 25 (deeply oversold — stricter than momentum's 30)
- Volume elevated (confirms selling climax)

### Short Entry (SELL signal)
- Price above Bollinger upper band (pct_b > 0.95)
- RSI > 75 (deeply overbought)
- Volume elevated (confirms buying climax)

### Exit Rules
- Target: middle Bollinger band (mean reversion complete)
- Stop: 1.5× ATR (same as momentum)

---

## 3. AIFundamentalStrategy (optional)

**Thesis:** Fundamental catalysts + technical confirmation = high-conviction trades.

Requires `ANTHROPIC_API_KEY` in `.env`. Uses Claude claude-opus-4-6 to score recent news and fundamentals (0.0–1.0) and combines with technical signal for final score.

- Score weight: `ai_signal_weight = 0.40` (config)
- Falls back to technical-only if API unavailable

---

## Signal Quality Gates

All strategies must pass these gates before generating a signal:

1. **Minimum 60 bars** of history
2. **Volume filter**: volume > 0.5× 20-day avg (rejects truly anomalous days)
3. **Min 2 indicators agreeing**: momentum, mean reversion, or AI
4. **Score threshold**: `min_signal_score = 0.65` (configurable)

## Score Calculation

```
bullish_components / total_components → base score in [0.5, 1.0]
× ai_score_weight (if AI strategy)
```

Final score ≥ 0.65 required to submit an order.
