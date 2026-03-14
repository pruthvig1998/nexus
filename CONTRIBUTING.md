# Contributing to NEXUS

## Setup

```bash
git clone https://github.com/pruthvig1998/nexus.git
cd nexus
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # add your Alpaca keys
```

## Development Workflow

```bash
# Run tests
pytest tests/ -v

# Lint
ruff check nexus/ tests/

# Backtest to verify changes
python -m nexus backtest -t AAPL -t MSFT --years 1
```

## Long/Short Correctness Rules

When touching execution or P&L logic, verify:

1. **Short P&L** = `(entry - exit) × shares` — never `(exit - entry) × shares`
2. **Short stop** triggers when price **rises** above stop (loss)
3. **Short target** triggers when price **falls** below target (profit)
4. **`broker.open_short()`** = SELL unowned stock (Alpaca auto-handles)
5. **`broker.close_short()`** = BUY to cover (not `place_order(SELL)`)

## Pull Request Process

1. Branch from `main`
2. Write tests for any new long/short logic
3. Run `pytest tests/ -v && ruff check nexus/ tests/`
4. Fill out the PR template (both long and short paths tested)
5. Request review
