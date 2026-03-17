# NEXUS v3 — Claude Code Project Instructions

## What This Is

NEXUS is a production-grade long/short algorithmic trading system. 22 Python modules, ~8,100 LOC, 162 tests. It trades equities across 4 brokers using 7 strategy engines with AI-powered signal generation and institutional-quality risk management.

## Architecture

```
CLI (main.py) → NEXUSEngine (engine.py) → asyncio event loop
                    ├── Strategies (7 engines) → Signal objects
                    ├── Risk Manager (risk.py) → position sizing + limits
                    ├── Broker Adapters (4) → order execution
                    ├── Discord Feed (discord_feed.py) → signal ingestion
                    ├── Twitter Feed (twitter_feed.py) → Nitter RSS signals
                    ├── Dashboard (dashboard.py) → Rich TUI
                    └── Tracker (tracker.py) → SQLite audit trail
```

### Signal Flow

External feeds (Discord, Twitter) inject `Signal` objects into `asyncio.Queue`. The engine's `_scan_cycle()` drains this queue alongside internally-generated signals. All signals pass through the same risk/sizing pipeline before execution.

### Key Abstractions

- **`Signal`** (`strategy.py`): Core data object — ticker, direction, score, strategy, reasoning, prices
- **`BaseBroker`** (`broker.py`): ABC for all broker adapters — connect, quote, order, positions
- **`NEXUSConfig`** (`config.py`): Global singleton via `get_config()` / `set_config()`, all env-driven
- **`EventType`** (`engine.py`): Internal pub/sub event bus for cross-component communication

## Module Map

| Module | Purpose |
|--------|---------|
| `main.py` | CLI entrypoint (click): run, backtest, status, signals, load-discord |
| `engine.py` | Async trading loop, signal aggregation, order routing, fill polling |
| `config.py` | Dataclass config hierarchy, env loading via python-dotenv |
| `strategy.py` | Momentum, Mean Reversion, ORB, AI Fundamental strategies |
| `strategy_irongrid.py` | IronGrid community playbook (cup-and-handle, reversal) |
| `strategy_events.py` | Event calendar: earnings/catalyst detection with Claude AI |
| `strategy_news.py` | Headline sentiment: macro rules (65+ regex) + sector rotation |
| `indicators.py` | RSI, MACD, BB, ATR, SMA, EMA, ADR, volume ratio, golden cross |
| `risk.py` | Kelly+ATR sizing, 4-layer risk framework, circuit breakers |
| `tracker.py` | SQLite: trades, signals, daily P&L (WAL mode) |
| `broker.py` | BaseBroker ABC + AlpacaBroker |
| `broker_moomoo.py` | Moomoo/Futu adapter |
| `broker_ibkr.py` | Interactive Brokers TWS adapter |
| `broker_webull.py` | Webull adapter (unofficial) |
| `discord_feed.py` | Live Discord bot: message parsing → Signal objects |
| `discord_loader.py` | Historical DiscordChatExporter JSON → signals |
| `twitter_feed.py` | Nitter RSS polling → tweet parsing → Signal objects |
| `dashboard.py` | Rich terminal UI: positions, signals, exposure, P&L |
| `backtest.py` | Historical simulation with slippage model, HTML reports |
| `logger.py` | structlog setup (console renderer, timestamps) |

## Conventions

### Code Style
- `from __future__ import annotations` in every module
- Type hints on all function signatures
- structlog for logging: `log = get_logger("module_name")`
- ruff for linting (E, F, I rules, line length 100)
- pytest + pytest-asyncio (asyncio_mode = "auto")

### Signal Parsing (Discord + Twitter)
Both feeds share the same parsing infrastructure from `discord_feed.py`:
- `COMMON_WORDS` set — filters false-positive ticker matches
- `_TICKER_EXPLICIT` regex — `$AAPL` explicit mentions
- `_TICKER_BARE` regex — `AAPL` bare uppercase words
- `_BUY_KEYWORDS` / `_SELL_KEYWORDS` — strength-tiered (2.0/1.5/1.0)
- `_compute_direction_score()` — proximity-weighted scoring
- Ambiguity: require 65% relative advantage to resolve
- Score: base 0.55, +0.05 explicit ticker, +0.05 price nearby, max 0.80

### Broker Interface
All brokers implement `BaseBroker` with async methods:
- `connect()`, `disconnect()`, `is_connected`
- `get_quote()`, `get_batch_quotes()`, `get_positions()`, `get_account_info()`
- `place_order()`, `cancel_order()`, `get_order_status()`

### Adding a New Strategy
1. Create `strategy_xxx.py` implementing the strategy
2. Return `Signal` objects with score 0.0-1.0
3. Register in `engine.py`'s strategy list
4. Add tests in `tests/test_xxx.py`

### Adding a New Feed
1. Create `xxx_feed.py` with a class matching `TwitterFeed` / `DiscordFeed` pattern
2. Accept `(config, signal_queue, news_strategy)` in `__init__`
3. Implement `start()` (blocking async loop) and `stop()`
4. Wire into `main.py`'s `_run()` function with a CLI flag
5. Add config dataclass in `config.py`

## Running

```bash
pip install -e ".[dev]"         # install with dev deps
pytest tests/ -v                # run tests (162 tests)
nexus run --paper --broker alpaca  # paper trading
nexus run --paper --discord --twitter  # with social feeds
nexus backtest -t AAPL -t MSFT --years 2  # backtest
nexus status                    # portfolio status
```

## Environment

All config via `.env` file (see `.env.example`). Key vars:
- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` — primary broker
- `ANTHROPIC_API_KEY` — AI strategies (event calendar, AI fundamental)
- `DISCORD_BOT_TOKEN` / `DISCORD_CHANNEL_IDS` — Discord feed
- `TWITTER_ACCOUNTS` / `TWITTER_POLL_INTERVAL` / `NITTER_INSTANCES` — Twitter feed

## Testing

- 162 tests across 8 test files
- Tests use real indicator math, no mocking of core logic
- Broker/network calls are mocked
- `asyncio_mode = "auto"` — async tests just work
- Always run full suite after changes: `pytest tests/ -v`

## Important Patterns

- **Never mock indicators or strategy math in tests** — test with real data
- **Signal dedup**: Both feeds use LRU set (10k cap, evict to 5k)
- **Instance health**: Twitter feed rotates 8 Nitter instances with 5-min cooldown
- **Risk layers**: Signal quality → Kelly+ATR sizing → exposure caps → circuit breakers
- **Short safety**: Separate exposure cap (50%), direction-aware stops, margin-based sizing
