# NEXUS v3 — Agent Guidelines

Instructions for AI agents (Claude Code, Copilot, Cursor, etc.) working on this codebase.

## Ground Rules

1. **Read before writing.** Always read a file before modifying it. Understand the existing patterns.
2. **Run tests after every change.** `pytest tests/ -v` must stay at 162+ passing. Never commit with failing tests.
3. **No over-engineering.** This is a trading system — simplicity reduces bugs. Don't add abstractions for hypothetical future needs.
4. **Match existing style.** Check adjacent code before writing. If the file uses structlog, use structlog. If it uses dataclasses, use dataclasses.
5. **Don't modify risk.py casually.** Risk management is safety-critical. Changes need explicit user approval and thorough testing.

## Code Conventions

### Imports
```python
from __future__ import annotations  # always first

# stdlib
import asyncio
import re

# third-party
import pandas as pd

# local
from nexus.config import NEXUSConfig
from nexus.logger import get_logger
from nexus.strategy import Signal
```

### Logging
```python
from nexus.logger import get_logger
log = get_logger("module_name")

log.info("Action completed", ticker="AAPL", score=0.75)
log.warning("Something off", error=str(e))
log.debug("Verbose detail", data=payload)
```

### Type Hints
- All function signatures must have type hints
- Use `Optional[X]` for nullable, `List[X]` for lists (from typing)
- `asyncio.Queue` gets `# type: ignore[type-arg]` comment

### Testing
- Test file: `tests/test_{module_name}.py`
- Use pytest, not unittest (except for `unittest.mock`)
- Async tests: just define `async def test_...()` — asyncio_mode is auto
- Mock network calls, never mock math/indicators
- Group related tests in classes: `class TestFeatureName:`

### Error Handling
- Use try/except around all broker calls and external I/O
- Log errors with structlog: `log.error("Action failed", error=str(e))`
- Never let exceptions crash the scan loop — catch and log in `_scan_cycle()`
- Validate inputs at boundaries (CLI args, config loading, external signals)

### Async / Concurrency
- All broker calls are async (wrapped with `asyncio.to_thread` for sync SDKs)
- Use `asyncio.gather(*tasks, return_exceptions=True)` for parallel strategy analysis
- External feeds (Discord, Twitter) run as separate tasks in the same event loop
- Signal queue (`asyncio.Queue`) is the interface between feeds and engine
- Never use `time.sleep()` — always `await asyncio.sleep()`

## Architecture Boundaries

### What NOT to change without asking
- `risk.py` — Risk management is load-bearing. One bug = real money lost.
- `engine.py` signal queue drain — External feeds depend on this interface.
- `broker.py` BaseBroker ABC — All 4 broker adapters implement this.
- `Signal` dataclass fields — Every strategy and feed produces these.

### Safe to modify freely
- Adding new strategies (`strategy_*.py`)
- Adding new feeds (`*_feed.py`)
- Tests
- Dashboard layout/formatting
- CLI commands in `main.py`
- Config dataclasses (additive only)

## Adding Features

### New Strategy
1. Create `nexus/strategy_xxx.py`
2. Implement `analyze(ticker, df) -> Optional[Signal]` or similar
3. Register in `engine.py`'s `_strategies` list
4. Add `tests/test_xxx.py` with edge cases
5. Update CLAUDE.md module table

### New Feed (Signal Source)
1. Create `nexus/xxx_feed.py`
2. Class with `__init__(config, signal_queue, news_strategy=None)`
3. `async start()` — blocking loop, `async stop()` — graceful shutdown
4. Parse input → `Signal` objects → `queue.put_nowait(sig)`
5. Add config dataclass to `config.py`
6. Add CLI flag to `main.py`'s `run` command
7. Add tests with mocked I/O
8. Update CLAUDE.md and AGENTS.md

### New Broker
1. Create `nexus/broker_xxx.py`
2. Subclass `BaseBroker` from `broker.py`
3. Implement all abstract methods (connect, quote, order, positions)
4. Add optional dependency in `pyproject.toml`
5. Add to `main.py`'s broker choice list
6. Add config dataclass to `config.py`

## Common Patterns

### Signal Creation
```python
sig = Signal(
    ticker="AAPL",
    direction="BUY",        # BUY | SELL | HOLD
    score=0.72,             # 0.0 to 1.0
    strategy="my_strategy",
    reasoning="Brief explanation of why",
    entry_price=0.0,        # 0 = let engine determine
    stop_price=0.0,
    target_price=0.0,
)
```

### Feed Dedup Pattern
```python
if guid in self._seen:
    return
if len(self._seen) >= 10_000:
    self._seen = set(list(self._seen)[-5000:])
self._seen.add(guid)
```

### Health Tracking Pattern (for external services)
```python
self._health: Dict[str, bool] = {inst: True for inst in instances}
self._last_fail: Dict[str, float] = {}

def _mark_unhealthy(self, inst):
    self._health[inst] = False
    self._last_fail[inst] = time.monotonic()

def _sorted_instances(self):
    now = time.monotonic()
    healthy = [i for i in self._instances if self._health.get(i, True)]
    recovered = [i for i in self._instances
                 if not self._health.get(i, True)
                 and now - self._last_fail.get(i, 0) > 300]
    return healthy + recovered
```

## Debugging

```bash
# Run with debug logging
nexus run --paper --log-level DEBUG

# Check database directly
sqlite3 nexus.db "SELECT * FROM trades ORDER BY created_at DESC LIMIT 10;"
sqlite3 nexus.db "SELECT * FROM signals ORDER BY ts DESC LIMIT 10;"

# Test a single module
pytest tests/test_twitter_feed.py -v -s

# Import check
python -c "from nexus.twitter_feed import TwitterFeed; print('OK')"
```

## Dependencies

Core deps are in `pyproject.toml`. Rules:
- Core deps must work without optional broker packages
- Broker-specific deps go in `[project.optional-dependencies]`
- `aiohttp` is core (used by Twitter feed, general async HTTP)
- `discord.py` is optional (`[discord]` extra)
- Dev deps (`pytest`, `ruff`) go in `[dev]` extra

### PR Workflow
- All tests must pass: `pytest tests/ -v`
- Lint must pass: `ruff check nexus/ tests/`
- Format must pass: `ruff format nexus/ tests/ --check`
- Update CLAUDE.md module table if adding new modules
- Update README.md feature counts if adding features

## Files That Should Stay Updated

When making significant changes, update:
- `CLAUDE.md` — Module table, conventions, running instructions
- `AGENTS.md` — This file, if new patterns emerge
- `README.md` — Feature counts, strategy list, CLI reference
- `.env.example` — New environment variables
- `pyproject.toml` — New dependencies
