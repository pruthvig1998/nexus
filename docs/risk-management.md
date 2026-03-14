# Risk Management

NEXUS v3 uses a four-layer risk system with symmetric long/short limits.

## Layer 1: Position Sizing (Kelly + ATR)

```python
# ATR-based: risk 1% of portfolio per trade
atr_shares = (portfolio_value × 0.01) / risk_per_share

# Kelly sizing: scaled by signal conviction
kelly_shares = portfolio_value × kelly_fraction × signal_score / entry_price

# Final: minimum of the two methods
shares = min(atr_shares, kelly_shares)
shares = min(shares, cash × 0.95 / entry_price)   # cash check
```

**Kelly formula (25% fraction):**
```
Kelly = (win_rate × W/L_ratio - loss_rate) / W/L_ratio × 0.25
```

Capped at 20% of portfolio per position maximum.

---

## Layer 2: Per-Position Limits

| Limit | Value |
|-------|-------|
| Max position size | 5% of portfolio |
| Max open positions | 20 |
| Min signal score | 0.65 |

---

## Layer 3: Portfolio Exposure Caps

| Direction | Limit | Notes |
|-----------|-------|-------|
| Long exposure | 90% | Sum of all long positions / portfolio value |
| Short exposure | 50% | Sum of all short positions / portfolio value |

Long and short books are tracked **separately**. A 90% long + 50% short portfolio is theoretically possible (net long 40%) but requires sufficient buying power / margin.

---

## Layer 4: Daily Loss Halt

If intraday P&L drops below **−3%** of portfolio value:
- `RiskLimits.is_halted = True`
- All new order execution blocked for the remainder of the day
- Reset at midnight via `reset_daily()`

---

## Stop / Target Math

```
Stop distance = 1.5 × ATR(14)
Target        = entry ± 3 × stop_distance   (3:1 R:R)

LONG:  stop = entry - 1.5×ATR   target = entry + 4.5×ATR
SHORT: stop = entry + 1.5×ATR   target = entry - 4.5×ATR
```

Stop/target hits are checked on every scan cycle (default: 60s).

---

## Short Exposure Example

```
Portfolio: $100,000
Longs:  AAPL $20k + MSFT $15k + NVDA $10k = $45k (45% long)
Shorts: TSLA $12k + META $8k              = $20k (20% short)

→ Long check:  45% < 90% ✓
→ Short check: 20% < 50% ✓
→ Net exposure: 25% long
```
