"""NEXUS v3 CLI."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime

import click


@click.group()
@click.version_option("3.0.0")
def cli():
    """⚡ NEXUS v3 — long/short algorithmic trading with Alpaca paper trading."""
    pass


# ── backtest ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option("-t", "--ticker", "tickers", multiple=True,
              default=["AAPL", "MSFT", "NVDA", "GOOGL"], show_default=True)
@click.option("-y", "--years", default=2.0, show_default=True)
@click.option("--capital", default=100_000.0, show_default=True)
@click.option("-o", "--output", default=None)
@click.option("--log-level", default="INFO", show_default=True)
def backtest(tickers, years, capital, output, log_level):
    """Run long/short backtest on historical data and generate HTML report."""
    from nexus.logger import setup_logging
    setup_logging(log_level)
    from nexus.backtest import generate_report, run_backtest

    if not output:
        output = f"reports/nexus/backtest_{datetime.now():%Y%m%d}.html"

    click.echo(f"Backtesting {list(tickers)} over {years:.1f} years "
               f"(${capital:,.0f} starting capital) — LONG/SHORT\n")

    async def _run():
        summary = await run_backtest(list(tickers), years, capital)
        path = generate_report(summary, output)

        click.echo("\n" + "═" * 62)
        click.echo("  NEXUS v3 BACKTEST RESULTS  (Long/Short)")
        click.echo("═" * 62)
        click.echo(f"  Tickers:          {', '.join(summary.tickers)}")
        click.echo(f"  Period:           {years:.1f} years")
        click.echo(f"  Total Trades:     {summary.total_trades} "
                   f"({summary.long_trades} long / {summary.short_trades} short)")
        click.echo()
        s_pass = summary.portfolio_sharpe >= 1.0
        d_pass = summary.portfolio_max_dd < 20
        c_pass = summary.portfolio_cagr > 0
        sh_pass = summary.short_trades > 0
        click.echo(f"  Sharpe:    {summary.portfolio_sharpe:>6.2f}   {'✓ PASS' if s_pass else '✗ FAIL'}  (target >1.0)")
        click.echo(f"  CAGR:      {summary.portfolio_cagr:>5.1f}%   {'✓ PASS' if c_pass else '✗ FAIL'}  (target >0%)")
        click.echo(f"  Max DD:    {summary.portfolio_max_dd:>5.1f}%   {'✓ PASS' if d_pass else '✗ FAIL'}  (target <20%)")
        click.echo(f"  Win Rate:  {summary.portfolio_win_rate:>5.0%}")
        click.echo(f"  Shorts:    {summary.short_trades:>6}    {'✓ PASS' if sh_pass else '✗ FAIL'}  (target >0)")
        click.echo()
        click.echo(f"  {'Ticker':<7} {'Sharpe':>7} {'CAGR':>7} {'MaxDD':>6} {'WinRate':>8} {'L':>5} {'S':>5}")
        click.echo(f"  {'-'*55}")
        for r in summary.results:
            click.echo(f"  {r.ticker:<7} {r.sharpe:>7.2f} {r.cagr_pct:>6.1f}% "
                       f"{r.max_drawdown_pct:>5.1f}% {r.win_rate:>8.0%} "
                       f"{r.long_trades:>5} {r.short_trades:>5}")
        click.echo()
        click.echo(f"  Report → {path}")
        click.echo("═" * 62)
        sys.exit(0 if (s_pass and d_pass and c_pass) else 1)

    asyncio.run(_run())


# ── run ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--paper/--live", default=True, show_default=True)
@click.option("-b", "--broker", "broker_name", default="alpaca",
              type=click.Choice(["alpaca", "moomoo", "ibkr", "webull"], case_sensitive=False),
              show_default=True, help="Broker to trade with")
@click.option("-t", "--ticker", "tickers", multiple=True)
@click.option("--no-dashboard", is_flag=True)
@click.option("--scan-interval", default=60, show_default=True)
@click.option("--log-level", default="INFO", show_default=True)
@click.option("--discord", "use_discord", is_flag=True, default=False,
              help="Enable Discord channel monitoring for signals")
def run(paper, broker_name, tickers, no_dashboard, scan_interval, log_level, use_discord):
    """Start the live long/short trading engine with Rich dashboard."""
    from nexus.logger import setup_logging
    setup_logging(log_level)
    from nexus.config import NEXUSConfig, set_config

    if not paper:
        click.confirm(
            "⚠️  You are about to start LIVE trading with real money. Continue?",
            abort=True,
        )

    cfg = NEXUSConfig(paper=paper, active_broker=broker_name,
                      scan_interval=scan_interval, log_level=log_level)
    if tickers:
        cfg.watchlist = list(tickers)
    set_config(cfg)

    # Instantiate the selected broker
    def _make_broker():
        if broker_name == "moomoo":
            from nexus.broker_moomoo import MoomooBroker, MoomooTrdEnv
            trade_env = MoomooTrdEnv.SIMULATE if paper else MoomooTrdEnv.REAL
            return MoomooBroker(
                host=cfg.moomoo.host,
                port=cfg.moomoo.port,
                trade_env=trade_env,
            )
        elif broker_name == "ibkr":
            from nexus.broker_ibkr import IBKRBroker
            return IBKRBroker()
        elif broker_name == "webull":
            from nexus.broker_webull import WebullBroker
            return WebullBroker()
        else:
            from nexus.broker import AlpacaBroker
            return AlpacaBroker(cfg.alpaca)

    broker = _make_broker()
    mode = "PAPER" if paper else "LIVE"
    click.echo(f"Starting NEXUS v3 [{mode}] — broker: {broker_name.upper()} — Long/Short")
    click.echo(f"Watchlist: {', '.join(cfg.watchlist)}")
    click.echo(f"Scan interval: {scan_interval}s  |  Press Ctrl+C to stop\n")

    async def _run():
        from nexus.engine import NEXUSEngine
        from nexus.dashboard import NEXUSDashboard

        engine = NEXUSEngine(config=cfg, broker=broker)

        tasks = [engine.start()]
        if not no_dashboard:
            dash = NEXUSDashboard(engine.tracker, paper=paper,
                                  event_bus=engine.event_bus)
            tasks.append(dash.run())
        else:
            dash = None

        if use_discord:
            from nexus.discord_feed import DiscordFeed
            feed = DiscordFeed(cfg.discord, engine.get_signal_queue(),
                               news_strategy=engine.news_strategy)
            tasks.append(feed.start())
            click.echo("Discord feed enabled — monitoring configured channels")
        else:
            feed = None

        try:
            await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await engine.stop()
            if dash:
                await dash.stop()
            if feed:
                await feed.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("\nNEXUS stopped.")


# ── status ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--db", default="nexus.db", show_default=True)
def status(db):
    """Show portfolio status with long/short breakdown."""
    from nexus.tracker import PortfolioTracker
    t = PortfolioTracker(db)
    stats = t.compute_stats()
    pnl, trades = t.get_today_pnl()
    open_t = t.get_open_trades()
    closed = t.get_closed_trades(limit=5)

    longs = [tr for tr in open_t if tr.get("side") == "LONG"]
    shorts = [tr for tr in open_t if tr.get("side") == "SHORT"]

    click.echo("\n⚡ NEXUS v3 Portfolio Status  (Long/Short)")
    click.echo("─" * 44)
    click.echo(f"  Today's P&L:    ${pnl:+,.2f}  ({trades} trades)")
    click.echo(f"  Open Trades:    {len(open_t)}  ({len(longs)} long / {len(shorts)} short)")
    click.echo(f"  Win Rate:       {stats['win_rate']:.0%}  ({stats['total_trades']} total)")
    click.echo(f"  Profit Factor:  {stats['profit_factor']:.2f}x")
    click.echo(f"  Total P&L:      ${stats['total_pnl']:+,.2f}")

    if open_t:
        click.echo("\n  Open Positions:")
        for tr in open_t:
            side = tr.get("side", "LONG")
            arrow = "▲" if side == "LONG" else "▼"
            click.echo(f"    {arrow} {side:5} {tr['shares']:5.0f}×{tr['ticker']:<6} "
                       f"@ ${tr['entry_price']:.2f}  stop=${tr['stop_price']:.2f}")
    if closed:
        click.echo("\n  Recent Closed:")
        for tr in closed:
            p = tr.get("pnl") or 0
            side = tr.get("side", "LONG")
            click.echo(f"    {tr['ticker']:<6} {side:5}  ${p:+.2f}  ({tr.get('exit_reason', '-')})")


# ── signals ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--db", default="nexus.db", show_default=True)
@click.option("--limit", default=20, show_default=True)
def signals(db, limit):
    """Show recent trading signals."""
    from nexus.tracker import PortfolioTracker
    t = PortfolioTracker(db)
    sigs = t.get_recent_signals(limit)
    click.echo(f"\n  {'Time':<20} {'Ticker':<7} {'Dir':<6} {'Score':>6}  {'Strategy':<15}  Reasoning")
    click.echo("  " + "─" * 85)
    for s in sigs:
        ts = (s.get("ts") or "")[:19].replace("T", " ")
        click.echo(f"  {ts:<20} {s.get('ticker', ''):<7} {s.get('direction', ''):<6} "
                   f"{s.get('score', 0):>6.2f}  {s.get('strategy', ''):<15}  "
                   f"{(s.get('reasoning') or '')[:40]}")


# ── load-discord ───────────────────────────────────────────────────────────────

@cli.command("load-discord")
@click.argument("path", default="~/Desktop/discord-exports/")
@click.option("--min-score", default=0.55, show_default=True,
              help="Minimum signal score to include (0.0–1.0)")
@click.option("--db", default="nexus.db", show_default=True,
              help="SQLite database to log signals into")
@click.option("--no-db", is_flag=True, default=False,
              help="Parse and display signals without writing to DB")
@click.option("--show-signals", is_flag=True, default=False,
              help="Print individual signal details after summary")
@click.option("--limit", default=50, show_default=True,
              help="Max signals to show with --show-signals")
@click.option("--log-level", default="WARNING", show_default=True)
def load_discord(path, min_score, db, no_db, show_signals, limit, log_level):
    """Load DiscordChatExporter JSON exports and extract trading signals.

    PATH can be a single .json file or a directory of exported files.

    \b
    Examples:
      nexus load-discord ~/Desktop/discord-exports/
      nexus load-discord ~/Desktop/exports/server.json --show-signals
      nexus load-discord ~/Desktop/discord-exports/ --min-score 0.60 --no-db
    """
    from nexus.logger import setup_logging
    setup_logging(log_level)
    from nexus.discord_loader import DiscordLoader

    loader = DiscordLoader(min_score=min_score)

    try:
        summary = loader.load(path)
    except FileNotFoundError as e:
        click.echo(f"\n  Error: {e}", err=True)
        raise SystemExit(1)

    if not no_db and summary.signals_found > 0:
        loader.log_to_db(summary, db_path=db)

    loader.print_summary(summary)

    if show_signals:
        loader.print_signals(summary, limit=limit)

    if summary.signals_found == 0:
        click.echo("  No trading signals found. Try lowering --min-score or check "
                   "that the export contains trading-related messages.\n")


if __name__ == "__main__":
    cli()
