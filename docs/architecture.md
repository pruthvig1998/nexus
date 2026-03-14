# NEXUS v3 Architecture

## Component Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         NEXUS v3                                │
│                  Long/Short Trading System                      │
└─────────────────────────────────────────────────────────────────┘

CLI (main.py / __main__.py)
    │
    ├── backtest  ──→  backtest.py  ──→  strategy.py + indicators.py
    │
    └── run  ──→  engine.py (scan loop)
                     │
                     ├── strategy.py  ──→  indicators.py
                     │       └── compute_signal() → Signal(BUY|SELL|HOLD)
                     │
                     ├── risk.py  ──→  config.py
                     │       └── check() → approved/blocked, long/short exposure caps
                     │
                     ├── broker.py  ──→  Alpaca API
                     │       ├── place_order() / open_short() / close_short()
                     │       └── get_positions() → side: LONG|SHORT
                     │
                     ├── tracker.py  ──→  nexus.db (SQLite)
                     │       └── open_trade() / close_trade() → P&L (direction-aware)
                     │
                     └── dashboard.py  ──→  Rich terminal UI
                             └── shows LONG ▲ / SHORT ▼ badges, exposure split
```

## Data Flow

```
Market Data (yfinance)
    │
    ▼
indicators.py
  RSI, MACD, Bollinger Bands, ATR, Golden Cross, Volume Ratio
    │
    ▼
strategy.py  →  Signal(direction="BUY"|"SELL", score, entry, stop, target)
    │
    ▼
engine._execute()
    │
    ├── direction=="BUY" + no position   →  broker.place_order(BUY)  →  open LONG
    ├── direction=="BUY" + SHORT open    →  broker.close_short()     →  cover
    ├── direction=="SELL" + no position  →  broker.open_short()      →  open SHORT
    └── direction=="SELL" + LONG open    →  broker.place_order(SELL) →  close long
    │
    ▼
tracker.open_trade(side="LONG"|"SHORT")
    │
    ▼
engine._check_exits()
    ├── LONG: stop=price≤stop, target=price≥target  →  broker.place_order(SELL)
    └── SHORT: stop=price≥stop, target=price≤target →  broker.close_short()
    │
    ▼
tracker.close_trade()
  LONG P&L  = (exit - entry) × shares
  SHORT P&L = (entry - exit) × shares
```

## File Map

| File | Purpose | v3 Changes |
|------|---------|-----------|
| `config.py` | All settings in one dataclass | Added `max_short_exposure_pct` |
| `indicators.py` | RSI, MACD, BB, ATR, volume | No changes |
| `strategy.py` | Generates BUY/SELL/HOLD signals | No changes |
| `risk.py` | Kelly sizing + limit checks | Short exposure cap, direction-aware check |
| `broker.py` | BaseBroker + AlpacaBroker | `open_short()`, `close_short()`, side detection |
| `tracker.py` | SQLite trade/signal log | Direction-aware P&L in `close_trade()` |
| `engine.py` | Async scan-execute loop | Routes SELL→short, BUY→cover logic |
| `dashboard.py` | Rich terminal UI | SIDE badges, long/short exposure panel |
| `backtest.py` | Walk-forward simulation | Symmetric long+short simulation |
| `logger.py` | structlog setup | No changes |
| `main.py` | CLI (click) | Backtest output shows L/S split |

## Database Schema

```sql
trades (
  id TEXT PRIMARY KEY,
  broker TEXT, ticker TEXT,
  side TEXT,          -- "LONG" | "SHORT"
  shares REAL, entry_price REAL, exit_price REAL,
  stop_price REAL, target_price REAL,
  strategy TEXT, signal_score REAL,
  pnl REAL,           -- direction-aware: short = (entry-exit)*shares
  exit_reason TEXT,
  opened_at TEXT, closed_at TEXT, paper INTEGER
)
```
