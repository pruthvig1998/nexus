## Summary

<!-- Describe your changes in 2-3 bullet points -->

## Type of change

- [ ] Bug fix
- [ ] New feature (long/short trading, risk, strategy, broker)
- [ ] Refactor
- [ ] Documentation
- [ ] Tests

## Testing

- [ ] `pytest tests/ -v` passes
- [ ] `ruff check nexus/ tests/` passes
- [ ] Backtest run: `python -m nexus backtest -t AAPL -t MSFT --years 1`
- [ ] Smoke test: `python -m nexus run --paper --no-dashboard` (Ctrl+C after connect)

## Checklist

- [ ] No secrets or API keys committed
- [ ] Both long AND short paths tested if touching execution logic
- [ ] P&L math direction-aware (short = entry - exit, not exit - entry)
