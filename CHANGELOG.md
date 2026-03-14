# Changelog

## [3.0.0] — 2025-03-13

### Added
- **Long/short support** — NEXUS can now profit in both up and down markets
- `broker.open_short()` / `broker.close_short()` — explicit short sell and cover methods
- `Position.side` field — "LONG" | "SHORT" on all position objects
- `RiskConfig.max_short_exposure_pct` — short book capped at 50% of portfolio value
- Symmetric backtest simulation — both BUY (long) and SELL (short) entries simulated
- Dashboard SIDE column — ▲ LONG (green) / ▼ SHORT (red) badges on positions
- Dashboard shows long exposure % and short exposure % separately in risk panel
- `backtest.TickerResult` includes `long_trades` / `short_trades` counts
- GitHub CI workflow (pytest + ruff on push/PR)
- GitHub weekly backtest workflow (Monday 6 AM UTC → HTML artifact)
- `docs/architecture.md` — component diagram and data flow
- `docs/strategies.md` — full strategy spec with long/short entry/exit rules
- `docs/risk-management.md` — position sizing math and halt conditions
- Unit test suite: `tests/test_indicators.py`, `test_strategy.py`, `test_risk.py`, `test_backtest.py`

### Fixed
- `backtest.py`: was long-only (only processed BUY signals); now processes SELL signals too
- `tracker.close_trade()`: P&L was always `(exit - entry) × shares`; short P&L is now `(entry - exit) × shares`
- `engine._check_exits()`: short stops/targets were swapped (stop should trigger on price **rise** for shorts)
- `engine._execute()`: SELL signals were ignored unless closing an existing long; now routes to `open_short()`
- `dashboard._positions_table()`: showed no side information; now shows LONG/SHORT badge

### Removed
- `RiskConfig.var_confidence` — dead code, never referenced anywhere in execution path

### Changed
- `engine._PendingOrder` now stores `side: str` for correct trade recording
- `risk.check()` signature adds `signal_direction: str = "BUY"` parameter
- CLI `backtest` output now shows `(long / short)` trade split per ticker

---

## [2.0.0] — 2025-02-15

- Consolidated v1 multi-file structure into 12 flat files
- Added signal quality gates (volume filter, trend regime, min 2 indicators)
- Raised R:R ratio to 3:1, tightened RSI mean-reversion threshold to 25
- AlpacaBroker: official alpaca-py SDK, paper trading out of the box

## [1.0.0] — 2025-01-20

- Initial release: Momentum + MeanReversion strategies
- Long-only execution via Alpaca paper trading
- Rich terminal dashboard
- SQLite trade tracking
