"""Rich live terminal dashboard — long/short aware.

v3 changes:
  - Positions table: SIDE column with ▲ LONG / ▼ SHORT badges
  - P&L coloring is direction-aware (uses Position.unrealized_pnl)
  - Risk panel shows long exposure and short exposure separately
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Dict, List, Optional

from rich import box
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from nexus.broker import AccountInfo, Position
from nexus.logger import get_logger
from nexus.tracker import PortfolioTracker

log = get_logger("dashboard")

NAVY  = "#1B2A4A"
STEEL = "#4A6FA5"
GOLD  = "#C5A55A"


class NEXUSDashboard:
    """
    ┌─ NEXUS ── HH:MM:SS ── PAPER MODE ──────────────────────────────────┐
    │ Portfolio: $102,340 (+$2,340 / +2.3%)  Cash: $48,200  Risk: LOW   │
    ├─────────────────────┬──────────────────────┬────────────────────── ┤
    │  POSITIONS          │  TODAY'S SIGNALS     │  RISK METRICS         │
    ├─────────────────────┴──────────────────────┴────────────────────── ┤
    │  RECENT TRADES                                                      │
    └─────────────────────────────────────────────────────────────────────┘
    """

    def __init__(self, tracker: PortfolioTracker, paper: bool = True,
                 event_bus=None) -> None:
        self._tracker = tracker
        self._paper = paper
        self._bus = event_bus
        self._console = Console()
        self._running = False

        self._account: Optional[AccountInfo] = None
        self._positions: List[Position] = []
        self._risk_level = "LOW"
        self._sharpe = 0.0

        if self._bus:
            from nexus.engine import EventType
            self._bus.subscribe(EventType.POSITION_OPENED, self._on_event)
            self._bus.subscribe(EventType.POSITION_CLOSED, self._on_event)
            self._bus.subscribe(EventType.ORDER_FILLED, self._on_event)

    async def _on_event(self, *_) -> None:
        pass  # re-render on next tick

    def update(self, account: Optional[AccountInfo] = None,
               positions: Optional[List[Position]] = None,
               risk_level: str = "LOW", sharpe: float = 0.0) -> None:
        if account:
            self._account = account
        if positions is not None:
            self._positions = positions
        self._risk_level = risk_level
        self._sharpe = sharpe

    def _header(self) -> Panel:
        mode = "[bold yellow]PAPER MODE[/]" if self._paper else "[bold red]LIVE TRADING[/]"
        ts = datetime.now().strftime("%H:%M:%S")
        acct = self._account
        if acct:
            pct = acct.day_pnl / max(acct.portfolio_value, 1) * 100
            col = "green" if acct.day_pnl >= 0 else "red"
            body = (f"Portfolio: [bold]${acct.portfolio_value:,.0f}[/]  "
                    f"([{col}]{acct.day_pnl:+,.0f} / {pct:+.1f}%[/])  "
                    f"Cash: ${acct.cash:,.0f}  Risk: [bold]{self._risk_level}[/]")
        else:
            body = "Connecting..."
        txt = Text.assemble(("NEXUS  ", "bold white"),
                            (f"{ts}  ", "dim"), (mode + "  ", ""), (body, ""))
        return Panel(Align.center(txt), style=f"on {NAVY}", height=3)

    def _positions_table(self) -> Table:
        t = Table(title="POSITIONS", box=box.SIMPLE,
                  title_style=f"bold {GOLD}", header_style="bold dim")
        t.add_column("Ticker", width=7)
        t.add_column("Side", width=8)
        t.add_column("Shares", justify="right", width=7)
        t.add_column("Entry", justify="right", width=9)
        t.add_column("Now", justify="right", width=9)
        t.add_column("P&L", justify="right", width=10)

        for p in (self._positions or [])[:10]:
            if p.side == "SHORT":
                side_badge = "[red]▼ SHORT[/red]"
            else:
                side_badge = "[green]▲ LONG[/green]"

            pnl = p.unrealized_pnl
            pnl_color = "green" if pnl >= 0 else "red"
            pnl_str = f"[{pnl_color}]{pnl:+.2f}[/{pnl_color}]"

            t.add_row(p.ticker, side_badge, f"{p.shares:.0f}",
                      f"${p.avg_cost:.2f}", f"${p.current_price:.2f}", pnl_str)

        if not self._positions:
            t.add_row("[dim]No positions[/]", "", "", "", "", "")
        return t

    def _signals_table(self) -> Table:
        t = Table(title="TODAY'S SIGNALS", box=box.SIMPLE,
                  title_style=f"bold {GOLD}", header_style="bold dim")
        t.add_column("Time", width=6)
        t.add_column("Ticker", width=7)
        t.add_column("Dir", width=5)
        t.add_column("Score", justify="right", width=6)
        t.add_column("Strategy", width=13)
        sigs = self._tracker.get_recent_signals(limit=10)
        for s in sigs:
            d = s.get("direction", "")
            c = "green" if d == "BUY" else "red" if d == "SELL" else "dim"
            ts = (s.get("ts") or "")[:16].replace("T", " ")[-5:]
            t.add_row(ts, s.get("ticker", ""), f"[{c}]{d}[/]",
                      f"{s.get('score', 0):.2f}",
                      (s.get("strategy") or "")[:13])
        if not sigs:
            t.add_row("[dim]No signals yet[/]", "", "", "", "")
        return t

    def _risk_table(self) -> Table:
        t = Table(title="RISK METRICS", box=box.SIMPLE,
                  title_style=f"bold {GOLD}", show_header=False)
        t.add_column("Metric", width=16)
        t.add_column("Value", justify="right", width=11)

        pnl, trades = self._tracker.get_today_pnl()
        stats = self._tracker.compute_stats()
        pc = "green" if pnl >= 0 else "red"
        rc = {"LOW": "green", "MEDIUM": "yellow", "HIGH": "red"}.get(self._risk_level, "white")

        # Compute long/short exposure from positions
        longs = [p for p in self._positions if p.side == "LONG"]
        shorts = [p for p in self._positions if p.side == "SHORT"]
        long_val = sum(p.market_value for p in longs)
        short_val = sum(p.market_value for p in shorts)
        port_val = self._account.portfolio_value if self._account else 1
        long_exp_pct = long_val / max(port_val, 1) * 100
        short_exp_pct = short_val / max(port_val, 1) * 100

        t.add_row("Daily P&L", f"[{pc}]${pnl:+,.0f}[/]")
        t.add_row("Trades Today", str(trades))
        t.add_row("Win Rate", f"{stats['win_rate']:.0%}")
        t.add_row("Profit Factor", f"{stats['profit_factor']:.2f}x")
        t.add_row("Long Exposure", f"[green]{long_exp_pct:.1f}%[/]")
        t.add_row("Short Exposure", f"[red]{short_exp_pct:.1f}%[/]")
        t.add_row("Risk Level", f"[{rc}]{self._risk_level}[/]")
        t.add_row("Sharpe", f"{self._sharpe:.2f}")
        return t

    def _trades_table(self) -> Table:
        t = Table(title="RECENT TRADES", box=box.SIMPLE,
                  title_style=f"bold {GOLD}", header_style="bold dim")
        for col, w in [("Time", 6), ("Side", 7), ("Ticker", 7), ("Shares", 7),
                       ("Entry", 9), ("Exit", 9), ("P&L", 10), ("Reason", 12)]:
            t.add_column(col, width=w,
                         justify="right" if col in ("Shares", "Entry", "Exit", "P&L") else "left")
        for tr in self._tracker.get_closed_trades(limit=8):
            pnl = tr.get("pnl") or 0
            c = "green" if pnl >= 0 else "red"
            side = tr.get("side", "LONG")
            side_str = "[green]▲ LONG[/]" if side == "LONG" else "[red]▼ SHORT[/]"
            ts = ((tr.get("opened_at") or "")[:16].replace("T", " "))[-5:]
            t.add_row(ts, side_str, tr.get("ticker", ""),
                      f"{tr.get('shares', 0):.0f}",
                      f"${tr.get('entry_price', 0):.2f}",
                      f"${tr.get('exit_price', 0):.2f}" if tr.get("exit_price") else "-",
                      f"[{c}]${pnl:+.2f}[/]",
                      (tr.get("exit_reason") or "-")[:12])
        if not self._tracker.get_closed_trades(limit=1):
            t.add_row("[dim]No closed trades[/]", "", "", "", "", "", "", "")
        return t

    def _layout(self) -> Layout:
        lo = Layout()
        lo.split_column(
            Layout(name="header", size=3),
            Layout(name="middle", ratio=2),
            Layout(name="footer", ratio=1),
        )
        lo["middle"].split_row(
            Layout(name="pos"), Layout(name="sig"), Layout(name="risk"))
        lo["header"].update(self._header())
        lo["pos"].update(Panel(self._positions_table(), border_style=STEEL))
        lo["sig"].update(Panel(self._signals_table(), border_style=STEEL))
        lo["risk"].update(Panel(self._risk_table(), border_style=STEEL))
        lo["footer"].update(Panel(self._trades_table(), border_style=STEEL))
        return lo

    async def run(self) -> None:
        self._running = True
        with Live(self._layout(), console=self._console,
                  refresh_per_second=2, screen=True) as live:
            while self._running:
                try:
                    live.update(self._layout())
                    await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.error("Dashboard render error", error=str(e))
                    await asyncio.sleep(1)

    async def stop(self) -> None:
        self._running = False
