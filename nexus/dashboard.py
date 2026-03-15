"""Rich live terminal dashboard — Bloomberg Terminal aesthetic.

v3.1 redesign:
  - Dark navy background with steel blue borders and gold accents
  - Professional header bar with mode badge, portfolio stats
  - Visual exposure bars, score gauges, trend arrows
  - Status bar with broker connection, scan info, counts
  - Dense, information-rich layout matching institutional terminals
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import List, Optional

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from nexus.broker import AccountInfo, Position
from nexus.logger import get_logger
from nexus.tracker import PortfolioTracker

log = get_logger("dashboard")

# ── Theme ─────────────────────────────────────────────────────────────────────

BG_NAVY    = "#0F172A"
BORDER     = "#4A6FA5"
GOLD       = "#C5A55A"
GREEN      = "#22C55E"
RED        = "#EF4444"
DIM_TEXT   = "#64748B"
BRIGHT     = "#E2E8F0"
MID_TEXT   = "#94A3B8"

STYLE_BG       = Style(bgcolor=BG_NAVY)
STYLE_BORDER   = Style(color=BORDER)
STYLE_GOLD     = Style(color=GOLD, bold=True)
STYLE_GREEN    = Style(color=GREEN)
STYLE_RED      = Style(color=RED)
STYLE_DIM      = Style(color=DIM_TEXT)
STYLE_BRIGHT   = Style(color=BRIGHT)
STYLE_MID      = Style(color=MID_TEXT)

# Shorthand for inline markup
G = GREEN
R = RED


def _pnl_color(val: float) -> str:
    return G if val >= 0 else R


def _pnl_arrow(val: float) -> str:
    return "\u25b2" if val > 0 else ("\u25bc" if val < 0 else "\u25c6")


def _fmt_money(val: float, decimals: int = 0) -> str:
    if decimals == 0:
        return f"${val:,.0f}"
    return f"${val:,.{decimals}f}"


def _fmt_pnl(val: float, decimals: int = 0) -> str:
    if decimals == 0:
        return f"${val:+,.0f}"
    return f"${val:+,.{decimals}f}"


def _bar_string(pct: float, width: int = 20, filled_char: str = "\u2588",
                empty_char: str = "\u2591") -> str:
    """Build a text-based percentage bar."""
    pct = max(0.0, min(pct, 100.0))
    filled = int(round(pct / 100 * width))
    return filled_char * filled + empty_char * (width - filled)


def _score_gauge(score: float, max_score: float = 1.0, width: int = 10) -> Text:
    """Mini gauge bar for signal scores."""
    pct = max(0.0, min(score / max_score, 1.0))
    filled = int(round(pct * width))
    bar_text = Text()
    if pct >= 0.7:
        color = G
    elif pct >= 0.4:
        color = GOLD
    else:
        color = DIM_TEXT
    bar_text.append("\u2588" * filled, style=Style(color=color))
    bar_text.append("\u2591" * (width - filled), style=STYLE_DIM)
    bar_text.append(f" {score:.2f}", style=Style(color=color))
    return bar_text


class NEXUSDashboard:
    """
    Professional trading terminal dashboard with Bloomberg-style layout.

    +-[ NEXUS v3 ]--------[ 14:32:05 ]--------[ PAPER ]-+
    | Portfolio: $102,340  Day P&L: +$2,340  Cash: $48k  |
    +-------------------+----------------+---------------+
    |  POSITIONS        |  SIGNALS       |  RISK         |
    +-------------------+----------------+---------------+
    |  RECENT TRADES                                      |
    +-[ Broker: MOOMOO ]--[ Scan #42 ]--[ Pos: 5 ]------+
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
        self._scan_count: int = 0
        self._next_scan_seconds: int = 0

        if self._bus:
            from nexus.engine import EventType
            self._bus.subscribe(EventType.POSITION_OPENED, self._on_event)
            self._bus.subscribe(EventType.POSITION_CLOSED, self._on_event)
            self._bus.subscribe(EventType.ORDER_FILLED, self._on_event)

    async def _on_event(self, *_) -> None:
        pass  # re-render on next tick

    def update(self, account: Optional[AccountInfo] = None,
               positions: Optional[List[Position]] = None,
               risk_level: str = "LOW", sharpe: float = 0.0,
               scan_count: int = 0, next_scan_seconds: int = 0) -> None:
        if account:
            self._account = account
        if positions is not None:
            self._positions = positions
        self._risk_level = risk_level
        self._sharpe = sharpe
        self._scan_count = scan_count
        self._next_scan_seconds = next_scan_seconds

    # ── Header ────────────────────────────────────────────────────────────

    def _header(self) -> Panel:
        """Two-row Bloomberg-style header bar."""
        # Row 1: branding | time | mode badge
        row1 = Text()
        row1.append("  NEXUS", style=Style(color=GOLD, bold=True))
        row1.append(" v3 ", style=Style(color=GOLD))
        row1.append("  \u2502  ", style=STYLE_DIM)

        ts = datetime.now().strftime("%H:%M:%S")
        row1.append(ts, style=Style(color=BRIGHT, bold=True))
        row1.append("  \u2502  ", style=STYLE_DIM)

        if self._paper:
            row1.append(" PAPER ", style=Style(color="#000000", bgcolor="#EAB308", bold=True))
        else:
            row1.append(" LIVE ", style=Style(color="#FFFFFF", bgcolor="#EF4444", bold=True))

        # Row 2: portfolio metrics
        row2 = Text()
        acct = self._account
        if acct:
            row2.append("  Portfolio ", style=STYLE_MID)
            row2.append(_fmt_money(acct.portfolio_value), style=Style(color=BRIGHT, bold=True))

            row2.append("  \u2502  ", style=STYLE_DIM)
            arrow = _pnl_arrow(acct.day_pnl)
            pc = _pnl_color(acct.day_pnl)
            pct = acct.day_pnl / max(acct.portfolio_value, 1) * 100
            row2.append("Day P&L ", style=STYLE_MID)
            row2.append(f"{arrow} {_fmt_pnl(acct.day_pnl)} ({pct:+.2f}%)",
                        style=Style(color=pc, bold=True))

            row2.append("  \u2502  ", style=STYLE_DIM)
            row2.append("Cash ", style=STYLE_MID)
            row2.append(_fmt_money(acct.cash), style=Style(color=BRIGHT))

            row2.append("  \u2502  ", style=STYLE_DIM)
            row2.append("Buying Power ", style=STYLE_MID)
            row2.append(_fmt_money(acct.buying_power), style=Style(color=BRIGHT))
        else:
            row2.append("  Connecting...", style=STYLE_DIM)

        content = Group(Align.center(row1), Align.center(row2))
        return Panel(
            content,
            box=box.HEAVY,
            border_style=Style(color=BORDER),
            style=Style(bgcolor=BG_NAVY),
            height=4,
        )

    # ── Positions Panel ───────────────────────────────────────────────────

    def _positions_panel(self) -> Panel:
        t = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style=Style(color=MID_TEXT, bold=True),
            row_styles=[Style(bgcolor=BG_NAVY)],
            padding=(0, 1),
            expand=True,
        )
        t.add_column("TICKER", style=Style(color=BRIGHT, bold=True), width=7)
        t.add_column("SIDE", width=8)
        t.add_column("QTY", justify="right", width=6, style=Style(color=MID_TEXT))
        t.add_column("ENTRY", justify="right", width=9, style=Style(color=MID_TEXT))
        t.add_column("LAST", justify="right", width=9, style=Style(color=BRIGHT))
        t.add_column("MKT VAL", justify="right", width=10, style=Style(color=MID_TEXT))
        t.add_column("P&L", justify="right", width=11)
        t.add_column("P&L %", justify="right", width=8)

        for p in (self._positions or [])[:12]:
            if p.side == "SHORT":
                side_badge = Text(" SHORT ", style=Style(color="#FFFFFF", bgcolor=RED, bold=True))
            else:
                side_badge = Text(" LONG  ", style=Style(color="#FFFFFF", bgcolor=GREEN, bold=True))

            pnl = p.unrealized_pnl
            cost_basis = p.avg_cost * p.shares
            pnl_pct = (pnl / cost_basis * 100) if cost_basis != 0 else 0.0
            pc = _pnl_color(pnl)
            arrow = _pnl_arrow(pnl)

            pnl_text = Text(f"{arrow} {_fmt_pnl(pnl, 2)}", style=Style(color=pc, bold=True))
            pnl_pct_text = Text(f"{pnl_pct:+.2f}%", style=Style(color=pc))

            t.add_row(
                p.ticker,
                side_badge,
                f"{p.shares:.0f}",
                f"${p.avg_cost:.2f}",
                f"${p.current_price:.2f}",
                _fmt_money(p.market_value),
                pnl_text,
                pnl_pct_text,
            )

        if not self._positions:
            t.add_row(
                Text("No open positions", style=STYLE_DIM),
                "", "", "", "", "", "", "",
            )

        title = Text()
        title.append("\u2588 ", style=Style(color=GOLD))
        title.append("POSITIONS", style=Style(color=GOLD, bold=True))
        count = len(self._positions or [])
        title.append(f" ({count})", style=STYLE_DIM)

        return Panel(
            t,
            title=title,
            title_align="left",
            box=box.HEAVY,
            border_style=STYLE_BORDER,
            style=Style(bgcolor=BG_NAVY),
        )

    # ── Signals Panel ─────────────────────────────────────────────────────

    def _signals_panel(self) -> Panel:
        t = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style=Style(color=MID_TEXT, bold=True),
            padding=(0, 1),
            expand=True,
        )
        t.add_column("TIME", width=5, style=STYLE_DIM)
        t.add_column("TICKER", width=6, style=Style(color=BRIGHT, bold=True))
        t.add_column("DIR", width=5)
        t.add_column("SCORE", width=16)
        t.add_column("STRATEGY", width=12, style=STYLE_MID)
        t.add_column("REASON", style=STYLE_DIM, no_wrap=True)

        sigs = self._tracker.get_recent_signals(limit=10)
        for s in sigs:
            d = s.get("direction", "")
            if d == "BUY":
                dir_text = Text("BUY", style=Style(color=GREEN, bold=True))
            elif d == "SELL":
                dir_text = Text("SELL", style=Style(color=RED, bold=True))
            else:
                dir_text = Text(d, style=STYLE_DIM)

            ts = (s.get("ts") or "")[:16].replace("T", " ")[-5:]
            score = s.get("score", 0)
            gauge = _score_gauge(score)
            strategy = (s.get("strategy") or "")[:12]
            reasoning = (s.get("reasoning") or "")[:28]

            t.add_row(ts, s.get("ticker", ""), dir_text, gauge,
                      strategy, reasoning)

        if not sigs:
            t.add_row(
                Text("No signals yet", style=STYLE_DIM),
                "", "", "", "", "",
            )

        title = Text()
        title.append("\u2588 ", style=Style(color=GOLD))
        title.append("SIGNALS", style=Style(color=GOLD, bold=True))
        title.append(f" ({len(sigs)})", style=STYLE_DIM)

        return Panel(
            t,
            title=title,
            title_align="left",
            box=box.HEAVY,
            border_style=STYLE_BORDER,
            style=Style(bgcolor=BG_NAVY),
        )

    # ── Risk Panel ────────────────────────────────────────────────────────

    def _risk_panel(self) -> Panel:
        t = Table(
            box=box.SIMPLE,
            show_header=False,
            padding=(0, 1),
            expand=True,
        )
        t.add_column("Metric", width=15, style=Style(color=MID_TEXT))
        t.add_column("Value", justify="right", style=Style(color=BRIGHT))

        pnl, trades_today = self._tracker.get_today_pnl()
        stats = self._tracker.compute_stats()

        # Compute exposures
        longs = [p for p in self._positions if p.side == "LONG"]
        shorts = [p for p in self._positions if p.side == "SHORT"]
        long_val = sum(p.market_value for p in longs)
        short_val = sum(p.market_value for p in shorts)
        port_val = self._account.portfolio_value if self._account else 1
        long_pct = long_val / max(port_val, 1) * 100
        short_pct = short_val / max(port_val, 1) * 100
        net_pct = long_pct - short_pct

        # Peak drawdown (estimate from total PnL vs best)
        total_pnl = stats.get("total_pnl", 0.0)
        drawdown = min(0.0, total_pnl)  # simplified

        pc = _pnl_color(pnl)
        arrow = _pnl_arrow(pnl)

        # Daily P&L
        pnl_text = Text()
        pnl_text.append(f"{arrow} {_fmt_pnl(pnl)}", style=Style(color=pc, bold=True))
        t.add_row("Daily P&L", pnl_text)

        t.add_row("Trades Today", Text(str(trades_today), style=Style(color=BRIGHT)))

        # Separator
        t.add_row(
            Text("\u2500" * 15, style=STYLE_DIM),
            Text("\u2500" * 12, style=STYLE_DIM),
        )

        # Long exposure with bar
        long_bar = Text()
        long_bar.append(_bar_string(long_pct, width=10), style=Style(color=GREEN))
        long_bar.append(f" {long_pct:.1f}%", style=Style(color=GREEN))
        t.add_row("Long Exp", long_bar)

        # Short exposure with bar
        short_bar = Text()
        short_bar.append(_bar_string(short_pct, width=10), style=Style(color=RED))
        short_bar.append(f" {short_pct:.1f}%", style=Style(color=RED))
        t.add_row("Short Exp", short_bar)

        # Net exposure
        net_color = G if net_pct >= 0 else R
        t.add_row("Net Exp", Text(f"{net_pct:+.1f}%", style=Style(color=net_color, bold=True)))

        # Separator
        t.add_row(
            Text("\u2500" * 15, style=STYLE_DIM),
            Text("\u2500" * 12, style=STYLE_DIM),
        )

        # Performance metrics
        wr = stats["win_rate"]
        wr_color = G if wr >= 0.5 else R
        t.add_row("Win Rate", Text(f"{wr:.0%}", style=Style(color=wr_color, bold=True)))

        pf = stats["profit_factor"]
        pf_color = G if pf >= 1.0 else R
        t.add_row("Profit Factor", Text(f"{pf:.2f}x", style=Style(color=pf_color)))

        t.add_row("Sharpe", Text(f"{self._sharpe:.2f}", style=Style(color=BRIGHT)))

        # Drawdown
        dd_text = Text(f"{_fmt_pnl(drawdown)}", style=Style(color=RED if drawdown < 0 else MID_TEXT))
        t.add_row("Drawdown", dd_text)

        # Risk level with colored dot
        rc = {
            "LOW": GREEN, "MEDIUM": "#EAB308", "HIGH": RED, "CRITICAL": RED,
        }.get(self._risk_level, MID_TEXT)
        risk_text = Text()
        risk_text.append("\u25cf ", style=Style(color=rc))
        risk_text.append(self._risk_level, style=Style(color=rc, bold=True))
        t.add_row("Risk Level", risk_text)

        title = Text()
        title.append("\u2588 ", style=Style(color=GOLD))
        title.append("RISK", style=Style(color=GOLD, bold=True))

        return Panel(
            t,
            title=title,
            title_align="left",
            box=box.HEAVY,
            border_style=STYLE_BORDER,
            style=Style(bgcolor=BG_NAVY),
        )

    # ── Trades Panel ──────────────────────────────────────────────────────

    def _trades_panel(self) -> Panel:
        t = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style=Style(color=MID_TEXT, bold=True),
            padding=(0, 1),
            expand=True,
        )
        t.add_column("OPENED", width=11, style=STYLE_DIM)
        t.add_column("SIDE", width=8)
        t.add_column("TICKER", width=7, style=Style(color=BRIGHT, bold=True))
        t.add_column("QTY", justify="right", width=6, style=STYLE_MID)
        t.add_column("ENTRY", justify="right", width=9, style=STYLE_MID)
        t.add_column("EXIT", justify="right", width=9, style=Style(color=BRIGHT))
        t.add_column("P&L", justify="right", width=11)
        t.add_column("P&L %", justify="right", width=8)
        t.add_column("HELD", justify="right", width=7, style=STYLE_DIM)
        t.add_column("STRATEGY", width=12, style=STYLE_MID)

        closed = self._tracker.get_closed_trades(limit=8)
        for tr in closed:
            pnl = tr.get("pnl") or 0
            pc = _pnl_color(pnl)
            arrow = _pnl_arrow(pnl)
            side = tr.get("side", "LONG")
            if side == "SHORT":
                side_badge = Text(" SHORT ", style=Style(color="#FFFFFF", bgcolor=RED, bold=True))
            else:
                side_badge = Text(" LONG  ", style=Style(color="#FFFFFF", bgcolor=GREEN, bold=True))

            # Compute P&L percentage
            entry_price = tr.get("entry_price", 0)
            shares = tr.get("shares", 0)
            cost_basis = entry_price * shares
            pnl_pct = (pnl / cost_basis * 100) if cost_basis != 0 else 0.0

            # Duration
            opened_at = tr.get("opened_at") or ""
            closed_at = tr.get("closed_at") or ""
            duration_str = ""
            if opened_at and closed_at:
                try:
                    o = datetime.fromisoformat(opened_at)
                    c = datetime.fromisoformat(closed_at)
                    delta = c - o
                    hours = delta.total_seconds() / 3600
                    if hours < 1:
                        duration_str = f"{delta.total_seconds() / 60:.0f}m"
                    elif hours < 24:
                        duration_str = f"{hours:.1f}h"
                    else:
                        duration_str = f"{delta.days}d"
                except (ValueError, TypeError):
                    duration_str = "-"

            ts_display = opened_at[:16].replace("T", " ") if opened_at else "-"

            exit_price = tr.get("exit_price")
            exit_str = f"${exit_price:.2f}" if exit_price else "-"

            pnl_text = Text(f"{arrow} {_fmt_pnl(pnl, 2)}", style=Style(color=pc, bold=True))
            pnl_pct_text = Text(f"{pnl_pct:+.2f}%", style=Style(color=pc))

            strategy = (tr.get("strategy") or "-")[:12]

            t.add_row(
                ts_display[-11:],
                side_badge,
                tr.get("ticker", ""),
                f"{shares:.0f}",
                f"${entry_price:.2f}",
                exit_str,
                pnl_text,
                pnl_pct_text,
                duration_str,
                strategy,
            )

        if not closed:
            t.add_row(
                Text("No closed trades", style=STYLE_DIM),
                "", "", "", "", "", "", "", "", "",
            )

        title = Text()
        title.append("\u2588 ", style=Style(color=GOLD))
        title.append("RECENT TRADES", style=Style(color=GOLD, bold=True))
        title.append(f" ({len(closed)})", style=STYLE_DIM)

        return Panel(
            t,
            title=title,
            title_align="left",
            box=box.HEAVY,
            border_style=STYLE_BORDER,
            style=Style(bgcolor=BG_NAVY),
        )

    # ── Status Bar ────────────────────────────────────────────────────────

    def _status_bar(self) -> Panel:
        bar = Text()

        # Left: broker info
        broker_name = "---"
        if self._account:
            broker_name = self._account.broker.upper()
        bar.append("  \u25cf ", style=Style(color=GREEN if self._account else RED))
        bar.append(f"Broker: {broker_name}", style=Style(color=BRIGHT))
        bar.append(" | ", style=STYLE_DIM)
        bar.append("Connected" if self._account else "Disconnected",
                   style=Style(color=GREEN if self._account else RED))

        # Center padding
        bar.append("    \u2502    ", style=STYLE_DIM)

        # Center: scan info
        bar.append(f"Scan #{self._scan_count}", style=Style(color=BRIGHT))
        bar.append(" | ", style=STYLE_DIM)
        bar.append(f"Next in {self._next_scan_seconds}s", style=Style(color=MID_TEXT))

        bar.append("    \u2502    ", style=STYLE_DIM)

        # Right: counts
        pos_count = len(self._positions or [])
        sig_count = len(self._tracker.get_recent_signals(limit=500))
        bar.append(f"Positions: {pos_count}", style=Style(color=BRIGHT))
        bar.append(" | ", style=STYLE_DIM)
        bar.append(f"Signals today: {sig_count}", style=Style(color=MID_TEXT))
        bar.append("  ", style=STYLE_DIM)

        return Panel(
            Align.center(bar),
            box=box.HEAVY,
            border_style=Style(color=DIM_TEXT),
            style=Style(bgcolor=BG_NAVY),
            height=3,
        )

    # ── Layout ────────────────────────────────────────────────────────────

    def _layout(self) -> Layout:
        lo = Layout()
        lo.split_column(
            Layout(name="header", size=4),
            Layout(name="middle", ratio=3),
            Layout(name="trades", ratio=2),
            Layout(name="status", size=3),
        )
        lo["middle"].split_row(
            Layout(name="positions", ratio=3),
            Layout(name="signals", ratio=3),
            Layout(name="risk", ratio=2),
        )
        lo["header"].update(self._header())
        lo["positions"].update(self._positions_panel())
        lo["signals"].update(self._signals_panel())
        lo["risk"].update(self._risk_panel())
        lo["trades"].update(self._trades_panel())
        lo["status"].update(self._status_bar())
        return lo

    # ── Run / Stop ────────────────────────────────────────────────────────

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
