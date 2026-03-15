# Contributing to NEXUS

## Development Setup

```bash
git clone https://github.com/pruthvig1998/nexus.git
cd nexus
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # add broker API keys + ANTHROPIC_API_KEY
```

Required environment variables:
- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` -- paper trading (minimum)
- `ANTHROPIC_API_KEY` -- AI Fundamental and News Sentiment strategies
- Broker-specific keys for Moomoo, IBKR, or Webull (optional)

## Code Style

- **Formatter / linter:** ruff (configured in `pyproject.toml`)
- **Line length:** 100 characters
- **Type hints:** required on all public function signatures
- **Docstrings:** required on all modules, classes, and public methods

```bash
ruff check nexus/ tests/
ruff format nexus/ tests/ --check
```

## Testing

All tests must pass before submitting a PR. The test suite covers indicators, strategy signals, risk checks, and backtest simulation.

```bash
# Run full suite
pytest tests/ -v

# With coverage
pytest tests/ --cov=nexus --cov-report=term-missing

# Run a quick backtest to sanity-check changes
python -m nexus backtest -t AAPL -t MSFT --years 1
```

## Long/Short Correctness

When modifying execution, P&L, or risk logic, verify these invariants:

1. **Short P&L** = `(entry - exit) * shares` -- never `(exit - entry) * shares`
2. **Short stop** triggers when price **rises** above stop (loss direction)
3. **Short target** triggers when price **falls** below target (profit direction)
4. **`broker.open_short()`** = sell unowned stock to open a short position
5. **`broker.close_short()`** = buy to cover -- not `place_order(SELL)`

## Architecture Overview

NEXUS is a flat-module system with 12 core files in `nexus/`:

```
Market Data -> indicators.py -> strategy.py -> risk.py -> engine.py -> broker.py
                                                              |
                                              +-----------+---+---+-----------+
                                              |           |       |           |
                                          tracker.py  dashboard.py  discord_feed.py
```

**Strategies** (6 total):
- `strategy.py` -- Momentum, Mean Reversion, ORB
- `strategy_irongrid.py` -- IronGrid systematic grid entries
- `strategy_news.py` -- News Sentiment via Discord feeds
- AI Fundamental is integrated into the base strategy module

**Brokers** (4 total):
- `broker.py` -- BaseBroker interface + AlpacaBroker
- `broker_moomoo.py` -- Moomoo via OpenD
- `broker_ibkr.py` -- Interactive Brokers via TWS/Gateway
- `broker_webull.py` -- Webull REST API

## Pull Request Process

1. Create a feature branch from `main`
2. Make your changes with tests covering new behavior
3. Verify: `pytest tests/ -v && ruff check nexus/ tests/`
4. For any long/short logic changes, include backtest results showing both directions
5. Open a PR with a clear description of what changed and why
6. Request review -- all PRs require at least one approval

## Commit Messages

Use concise, descriptive messages. Prefix with a category when helpful:

```
fix: short P&L calculation for partial fills
feat: add Webull broker adapter
test: add IronGrid strategy edge case coverage
docs: update risk management thresholds
```

## Reporting Issues

When filing a bug report, include:
- Python version and OS
- Broker being used (Alpaca/Moomoo/IBKR/Webull)
- Mode (paper/live/backtest)
- Minimal reproduction steps
- Relevant log output from `logs/`
