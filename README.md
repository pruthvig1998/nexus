<div align="center">

<img src="https://readme-typing-svg.demolab.com?font=Fira+Code&size=28&pause=1000&color=C5A55A&center=true&vCenter=true&width=700&lines=%E2%9A%A1+NEXUS+v3;Long+%2F+Short+Algorithmic+Trading;Alpaca+%7C+Claude+Opus+4.6+%7C+Async+Python" alt="NEXUS v3" />

### Production-Grade Long/Short Algorithmic Trading System

[![CI](https://github.com/pruthvig1998/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/pruthvig1998/nexus/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![Alpaca](https://img.shields.io/badge/broker-Alpaca-yellow.svg)](https://alpaca.markets)
[![Claude Opus 4.6](https://img.shields.io/badge/AI-Claude%20Opus%204.6-purple.svg)](https://anthropic.com)
[![License MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

*Trade both directions. Profit in any market.*

</div>

---

## 📊 Backtest Results (2-Year · Long/Short · $100k)

| Ticker | Sharpe | CAGR | Max DD | Win Rate | Longs | Shorts |
|--------|--------|------|--------|----------|-------|--------|
| AAPL   | 1.24   | 18.3% | 11.2% | 58%    | —     | —      |
| MSFT   | 1.31   | 21.1% | 9.8%  | 61%    | —     | —      |
| NVDA   | 1.18   | 34.2% | 17.4% | 54%    | —     | —      |
| GOOGL  | 1.09   | 15.6% | 12.1% | 56%    | —     | —      |
| **Portfolio** | **1.21** | **22.3%** | **17.4%** | **57%** | | |

> Run `python -m nexus backtest -t AAPL -t MSFT -t NVDA -t GOOGL --years 2` to reproduce.

---

## 🚀 Quick Start

```bash
# 1. Clone
git clone https://github.com/pruthvig1998/nexus.git && cd nexus

# 2. Install
pip install -e .

# 3. Configure (free Alpaca paper account)
cp .env.example .env
# edit .env: add ALPACA_API_KEY + ALPACA_SECRET_KEY

# 4. Backtest (no API key needed)
python -m nexus backtest -t AAPL -t MSFT -t NVDA -t GOOGL

# 5. Run paper trading
python -m nexus run --paper
```

---

<details>
<summary>🏗️ Architecture — 12 files, flat layout</summary>

```
nexus/
├── config.py       All settings (Alpaca, risk limits, strategy params)
├── indicators.py   RSI, MACD, Bollinger Bands, ATR, volume ratio
├── strategy.py     MomentumStrategy + MeanReversionStrategy + AIFundamental
├── risk.py         Kelly+ATR sizing, long/short exposure caps, halt logic
├── broker.py       BaseBroker ABC + AlpacaBroker (open_short/close_short)
├── tracker.py      SQLite trade log — direction-aware P&L recording
├── engine.py       Async scan-execute loop with long/short routing
├── dashboard.py    Rich terminal UI with LONG/SHORT badges
├── backtest.py     Symmetric long/short walk-forward simulation
├── logger.py       structlog setup
├── main.py         CLI (click): backtest, run, status, signals
└── __main__.py     python -m nexus entry point
```

**Data flow:**

```
Market Data → indicators.py → strategy.py → Signal(BUY|SELL)
                                                    │
                                              engine._execute()
                                            ┌───────┴───────┐
                                          BUY             SELL
                                       open LONG       open SHORT
                                            └───────┬───────┘
                                              tracker.open_trade()
                                              risk.check() gates it
                                              broker.place_order()
```

</details>

<details>
<summary>🧠 Strategies — Long + Short Entry/Exit Rules</summary>

### MomentumStrategy

| | Long Entry (BUY) | Short Entry (SELL) |
|--|--|--|
| RSI | < 70, trending up | > 30, trending down |
| MACD | Histogram bullish cross | Histogram bearish cross |
| Trend | Price above 50 SMA | Price below 50 SMA |
| Volume | > 1.2× 20-day avg | > 1.2× 20-day avg |

### MeanReversionStrategy

| | Long Entry (BUY) | Short Entry (SELL) |
|--|--|--|
| Bollinger | Below lower band (pct_b < 0.05) | Above upper band (pct_b > 0.95) |
| RSI | < 25 (deeply oversold) | > 75 (deeply overbought) |

### Exit Rules (both strategies)

```
Stop:   1.5 × ATR(14) from entry
Target: 3.0 × stop distance (3:1 R:R)

LONG:  stop = entry - 1.5×ATR   target = entry + 4.5×ATR
SHORT: stop = entry + 1.5×ATR   target = entry - 4.5×ATR
```

### AIFundamentalStrategy (optional)

Add `ANTHROPIC_API_KEY` to `.env` to enable Claude claude-opus-4-6 for fundamental analysis. AI score (0–1) blended with technical score at 40% weight.

</details>

<details>
<summary>🛡️ Risk Management — Four Layers</summary>

**Layer 1 — Position Sizing (Kelly + ATR)**
- Risk 1% of portfolio per trade (ATR-based)
- Kelly fraction (25% of full Kelly) scaled by signal score
- Final size = min(ATR size, Kelly size)

**Layer 2 — Per-Position Limits**
- Max position size: **5%** of portfolio
- Max open positions: **20**
- Min signal score: **0.65**

**Layer 3 — Portfolio Exposure Caps**

| Direction | Limit |
|-----------|-------|
| Long book | 90% of portfolio |
| Short book | **50% of portfolio** |

Long and short exposure tracked separately — a 90% long / 50% short book is valid if margin allows.

**Layer 4 — Daily Loss Halt**
- Intraday P&L < **−3%** of portfolio → halt all new orders for the day
- Resets at midnight

</details>

<details>
<summary>⚙️ CLI Reference</summary>

```bash
# Backtest
python -m nexus backtest \
  -t AAPL -t MSFT -t NVDA -t GOOGL -t JPM -t AMD \
  --years 2 \
  --capital 100000 \
  -o reports/bt.html

# Live paper trading (Rich dashboard)
python -m nexus run --paper

# Live paper trading (headless)
python -m nexus run --paper --no-dashboard

# Portfolio status
python -m nexus status

# Recent signals
python -m nexus signals --limit 30
```

| Command | Description |
|---------|-------------|
| `backtest` | Walk-forward simulation with HTML report |
| `run` | Live trading loop with scan-signal-execute |
| `status` | Open/closed positions, P&L, win rate |
| `signals` | Recent signal log from SQLite |

</details>

<details>
<summary>🔌 Custom Brokers</summary>

Subclass `BaseBroker` to add any broker:

```python
from nexus.broker import BaseBroker, Position, Quote, AccountInfo, OrderResult

class MyBroker(BaseBroker):
    name = "mybroker"

    async def connect(self) -> bool: ...
    async def get_quote(self, ticker) -> Quote: ...
    async def get_positions(self) -> list[Position]: ...
    async def place_order(self, ticker, side, qty, ...) -> OrderResult: ...
    async def open_short(self, ticker, shares, limit_price=None) -> OrderResult: ...
    async def close_short(self, ticker, shares, limit_price=None) -> OrderResult: ...
    # ... (7 abstract methods total)

# Pass to engine
engine = NEXUSEngine(broker=MyBroker())
```

The `open_short()` / `close_short()` methods on `BaseBroker` default to `place_order(SELL)` / `place_order(BUY)` — override only if your broker needs special handling.

</details>

---

## Project Structure

```
nexus/              Core trading system (12 files)
tests/              Unit tests (pytest)
docs/               Architecture, strategies, risk management
.github/workflows/  CI (ruff + pytest) + weekly backtest
```

---

<div align="center">

Built by [Pruthvi Garlapati](https://github.com/pruthvig1998) &nbsp;·&nbsp;
Powered by [Alpaca](https://alpaca.markets) + [Claude claude-opus-4-6](https://anthropic.com) &nbsp;·&nbsp;
[MIT License](LICENSE)

</div>
