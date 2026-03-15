# Changelog

All notable changes to NEXUS are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). Versions use [Semantic Versioning](https://semver.org/).

---

## [3.0.0] -- 2026-03-14

Major rewrite: long/short support, multi-broker architecture, two new strategies, and real-time notifications.

### Added
- **Long/short support** -- NEXUS can now profit in both up and down markets
- **Multi-broker architecture** -- pluggable broker interface with 4 adapters:
  - Alpaca (paper + live), Moomoo (OpenD), IBKR (TWS/Gateway), Webull (REST)
- **IronGrid strategy** (`strategy_irongrid.py`) -- systematic grid-based entries with scaling logic
- **News Sentiment strategy** (`strategy_news.py`) -- NLP scoring from Discord feeds and news sources
- **Discord integration** (`discord_feed.py`, `discord_loader.py`) -- trade alerts, signal notifications, Discord channel ingestion
- **Risk tuning** -- separate long/short exposure caps, `max_short_exposure_pct` at 50%
- `broker.open_short()` / `broker.close_short()` -- explicit short sell and cover methods
- `Position.side` field -- "LONG" or "SHORT" on all position objects
- Symmetric backtest simulation -- both BUY (long) and SELL (short) entries
- Dashboard SIDE column -- LONG (green) / SHORT (red) badges on positions
- Dashboard shows long exposure % and short exposure % separately in risk panel
- `backtest.TickerResult` includes `long_trades` / `short_trades` counts
- GitHub CI workflow (pytest + ruff on push/PR)
- GitHub weekly backtest workflow (Monday 6 AM UTC, HTML artifact)
- Architecture diagram SVG (`docs/assets/architecture.svg`)
- Full documentation: `docs/architecture.md`, `docs/strategies.md`, `docs/risk-management.md`
- Unit test suite: `tests/test_indicators.py`, `test_strategy.py`, `test_risk.py`, `test_backtest.py`

### Fixed
- `backtest.py`: was long-only (only processed BUY signals); now processes SELL signals too
- `tracker.close_trade()`: P&L was always `(exit - entry) * shares`; short P&L is now `(entry - exit) * shares`
- `engine._check_exits()`: short stops/targets were swapped (stop should trigger on price rise for shorts)
- `engine._execute()`: SELL signals were ignored unless closing an existing long; now routes to `open_short()`
- `dashboard._positions_table()`: showed no side information; now shows LONG/SHORT badge

### Changed
- `engine._PendingOrder` now stores `side: str` for correct trade recording
- `risk.check()` signature adds `signal_direction: str = "BUY"` parameter
- CLI `backtest` output now shows `(long / short)` trade split per ticker

### Removed
- `RiskConfig.var_confidence` -- dead code, never referenced in execution path

---

## [2.0.0] -- 2025-02-15

Consolidated architecture, tighter risk parameters, and official Alpaca SDK integration.

### Added
- Signal quality gates: volume filter, trend regime check, minimum 2 confirming indicators
- Opening Range Breakout (ORB) strategy for intraday momentum
- Configurable watchlist via `NEXUS_TICKERS` environment variable

### Changed
- Consolidated v1 multi-file structure into 12 flat modules
- Raised reward-to-risk ratio to 3:1 (was 2:1)
- Tightened RSI mean-reversion threshold to 25 (was 30)
- AlpacaBroker: migrated to official `alpaca-py` SDK
- Paper trading enabled out of the box with no configuration

### Fixed
- ATR calculation used wrong lookback period on first N bars
- Dashboard refresh rate caused flickering on slower terminals

---

## [1.0.0] -- 2025-01-20

Initial release.

### Added
- **Momentum strategy** -- RSI + MACD + SMA crossover for long entries
- **Mean Reversion strategy** -- Bollinger Band + RSI extremes for long entries
- Long-only execution via Alpaca paper trading
- Rich terminal dashboard with live positions, signals, and P&L
- SQLite trade tracking with full audit trail
- Kelly criterion + ATR-based position sizing
- CLI: `nexus run`, `nexus backtest`, `nexus status`
- Structured JSON logging
- Backtest engine with walk-forward simulation
