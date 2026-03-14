"""Backtesting engine — symmetric long/short simulation + HTML report.

v3 changes vs v2:
  - Simulates BOTH BUY (long entry) and SELL (short entry) signals
  - Correct short P&L: (entry - exit) × shares
  - Short exit logic: target = price drops to target, stop = price rises to stop
  - Trade records include side: "LONG" | "SHORT"
  - HTML report breaks out long vs short trade counts

Key correctness guarantees (preserved from v2):
  - Sequential yfinance downloads (avoids SQLite cache locking)
  - yfinance v2 MultiIndex column handling
  - Sharpe computed on active-trading days only (excludes flat/cash periods)
  - Same signal gates as live trading (volume filter, trend regime, min 2 signals)
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import asyncio
import numpy as np
import pandas as pd

from nexus.config import NEXUSConfig, RiskConfig, StrategyConfig, get_config
from nexus.strategy import compute_signal
from nexus.logger import get_logger

log = get_logger("backtest")


@dataclass
class TickerResult:
    ticker: str
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    cagr_pct: float
    win_rate: float
    profit_factor: float
    total_trades: int
    long_trades: int
    short_trades: int
    total_pnl: float
    final_equity: float


@dataclass
class BacktestSummary:
    tickers: List[str]
    years: float
    results: List[TickerResult]
    portfolio_sharpe: float
    portfolio_cagr: float
    portfolio_max_dd: float
    portfolio_win_rate: float
    total_trades: int
    long_trades: int
    short_trades: int
    generated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


# ── Metric helpers ────────────────────────────────────────────────────────────

def _sharpe(returns: pd.Series) -> float:
    active = returns[returns != 0.0]
    if len(active) < 5 or active.std() == 0:
        return 0.0
    return float(active.mean() / active.std() * math.sqrt(252))


def _sortino(returns: pd.Series) -> float:
    active = returns[returns != 0.0]
    down = active[active < 0]
    if len(down) == 0 or down.std() == 0:
        return 0.0
    return float(active.mean() / down.std() * math.sqrt(252))


def _max_dd(equity: pd.Series) -> float:
    roll_max = equity.cummax()
    dd = (equity - roll_max) / roll_max
    return float(dd.min() * -100)


def _cagr(equity: pd.Series, years: float) -> float:
    if years <= 0 or equity.iloc[0] <= 0:
        return 0.0
    return float(((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1) * 100)


# ── Per-ticker simulation ─────────────────────────────────────────────────────

def _simulate(ticker: str, df: pd.DataFrame,
              strategy_cfg: StrategyConfig, risk_cfg: RiskConfig,
              initial_capital: float = 100_000.0,
              commission_pct: float = 0.001) -> TickerResult:
    """Walk-forward simulation — each bar sees only past data.

    Tracks long and short positions simultaneously on the same ticker.
    """
    capital = initial_capital
    equity_curve: List[float] = []
    daily_returns: List[float] = []
    trades: List[dict] = []

    open_long: Optional[dict] = None   # {entry, stop, target, shares}
    open_short: Optional[dict] = None  # {entry, stop, target, shares}

    warmup = max(
        strategy_cfg.sma_slow + 1,
        strategy_cfg.macd_slow + strategy_cfg.macd_signal,
        strategy_cfg.bb_period,
        strategy_cfg.trend_sma_period,
        60,
    )

    for i in range(warmup, len(df)):
        window = df.iloc[: i + 1]
        row = df.iloc[i]
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])

        # ── Exit long ──────────────────────────────────────────────────────
        if open_long:
            pos = open_long
            if high >= pos["target"]:
                exit_price = pos["target"]
                pnl = (exit_price - pos["entry"]) * pos["shares"]
                capital += pos["shares"] * exit_price * (1 - commission_pct)
                trades.append({"pnl": pnl, "reason": "target", "side": "LONG"})
                open_long = None
            elif low <= pos["stop"]:
                exit_price = pos["stop"]
                pnl = (exit_price - pos["entry"]) * pos["shares"]
                capital += pos["shares"] * exit_price * (1 - commission_pct)
                trades.append({"pnl": pnl, "reason": "stop", "side": "LONG"})
                open_long = None

        # ── Exit short ─────────────────────────────────────────────────────
        if open_short:
            pos = open_short
            if low <= pos["target"]:
                # Price dropped to target — profit for short
                exit_price = pos["target"]
                pnl = (pos["entry"] - exit_price) * pos["shares"]
                capital += pnl - pos["shares"] * exit_price * commission_pct
                trades.append({"pnl": pnl, "reason": "target", "side": "SHORT"})
                open_short = None
            elif high >= pos["stop"]:
                # Price rose to stop — loss for short
                exit_price = pos["stop"]
                pnl = (pos["entry"] - exit_price) * pos["shares"]
                capital += pnl - pos["shares"] * exit_price * commission_pct
                trades.append({"pnl": pnl, "reason": "stop", "side": "SHORT"})
                open_short = None

        # ── Mark-to-market equity ──────────────────────────────────────────
        long_value = open_long["shares"] * close if open_long else 0.0
        short_pnl = ((open_short["entry"] - close) * open_short["shares"]
                     if open_short else 0.0)
        curr_equity = capital + long_value + short_pnl
        equity_curve.append(curr_equity)
        if len(equity_curve) > 1:
            prev = equity_curve[-2]
            daily_returns.append((curr_equity - prev) / prev if prev > 0 else 0.0)

        # ── New entries ────────────────────────────────────────────────────
        sig = compute_signal(ticker, window, strategy_cfg, risk_cfg)
        if sig and sig.score >= strategy_cfg.min_signal_score:
            risk_per_share = abs(sig.entry_price - sig.stop_price)
            if risk_per_share > 0:
                shares = min(
                    int(capital * 0.01 / risk_per_share),
                    int(capital * risk_cfg.max_position_pct / max(sig.entry_price, 0.01)),
                )

                if sig.direction == "BUY" and open_long is None and shares >= 1:
                    cost = shares * sig.entry_price * (1 + commission_pct)
                    if cost <= capital:
                        capital -= cost
                        open_long = {
                            "entry": sig.entry_price,
                            "shares": shares,
                            "stop": sig.stop_price,
                            "target": sig.target_price,
                        }

                elif sig.direction == "SELL" and open_short is None and shares >= 1:
                    # Short sale proceeds credited to capital
                    proceeds = shares * sig.entry_price * (1 - commission_pct)
                    capital += proceeds
                    open_short = {
                        "entry": sig.entry_price,
                        "shares": shares,
                        "stop": sig.stop_price,
                        "target": sig.target_price,
                    }

    # ── Close any open positions at period end ─────────────────────────────
    if len(df) > 0:
        final_price = float(df.iloc[-1]["close"])
        if open_long:
            pnl = (final_price - open_long["entry"]) * open_long["shares"]
            capital += open_long["shares"] * final_price * (1 - commission_pct)
            trades.append({"pnl": pnl, "reason": "period_end", "side": "LONG"})
        if open_short:
            pnl = (open_short["entry"] - final_price) * open_short["shares"]
            capital += pnl - open_short["shares"] * final_price * commission_pct
            trades.append({"pnl": pnl, "reason": "period_end", "side": "SHORT"})

    eq = pd.Series(equity_curve) if equity_curve else pd.Series([initial_capital])
    ret = pd.Series(daily_returns) if daily_returns else pd.Series([0.0])
    years = len(df) / 252
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [abs(t["pnl"]) for t in trades if t["pnl"] < 0]
    long_trades = [t for t in trades if t["side"] == "LONG"]
    short_trades = [t for t in trades if t["side"] == "SHORT"]

    return TickerResult(
        ticker=ticker,
        sharpe=_sharpe(ret),
        sortino=_sortino(ret),
        max_drawdown_pct=_max_dd(eq),
        cagr_pct=_cagr(eq, years),
        win_rate=len(wins) / max(len(trades), 1),
        profit_factor=sum(wins) / max(sum(losses), 0.01),
        total_trades=len(trades),
        long_trades=len(long_trades),
        short_trades=len(short_trades),
        total_pnl=capital - initial_capital,
        final_equity=capital,
    )


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_backtest(
    tickers: List[str],
    years: float = 2.0,
    initial_capital: float = 100_000.0,
    config: Optional[NEXUSConfig] = None,
) -> BacktestSummary:
    cfg = config or get_config()
    period = f"{int(years * 365)}d"
    log.info("Backtest starting", tickers=tickers, years=years,
             capital=f"${initial_capital:,.0f}", mode="long/short")

    async def _fetch(ticker: str) -> Tuple[str, Optional[pd.DataFrame]]:
        try:
            import yfinance as yf
            df = await asyncio.to_thread(
                lambda: yf.download(ticker, period=period, interval="1d",
                                    auto_adjust=True, progress=False)
            )
            if df is not None and len(df) > 60:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [c[0].lower() for c in df.columns]
                else:
                    df.columns = [c.lower() for c in df.columns]
                return ticker, df
            log.warning("Insufficient data", ticker=ticker,
                        bars=len(df) if df is not None else 0)
            return ticker, None
        except Exception as e:
            log.error("Download failed", ticker=ticker, error=str(e))
            return ticker, None

    price_data: Dict[str, pd.DataFrame] = {}
    for t in tickers:
        name, df = await _fetch(t)
        if df is not None:
            price_data[name] = df

    log.info("Data ready", downloaded=len(price_data),
             skipped=len(tickers) - len(price_data))

    results: List[TickerResult] = []
    for ticker, df in price_data.items():
        result = await asyncio.to_thread(
            _simulate, ticker, df, cfg.strategy, cfg.risk, initial_capital
        )
        results.append(result)
        log.info("Done", ticker=ticker,
                 sharpe=f"{result.sharpe:.2f}",
                 cagr=f"{result.cagr_pct:.1f}%",
                 max_dd=f"{result.max_drawdown_pct:.1f}%",
                 win_rate=f"{result.win_rate:.0%}",
                 trades=f"{result.total_trades} ({result.long_trades}L/{result.short_trades}S)")

    if not results:
        return BacktestSummary(tickers, years, [], 0, 0, 0, 0, 0, 0, 0)

    return BacktestSummary(
        tickers=list(price_data.keys()),
        years=years,
        results=results,
        portfolio_sharpe=round(float(np.mean([r.sharpe for r in results])), 2),
        portfolio_cagr=round(float(np.mean([r.cagr_pct for r in results])), 2),
        portfolio_max_dd=round(float(np.max([r.max_drawdown_pct for r in results])), 2),
        portfolio_win_rate=round(float(np.mean([r.win_rate for r in results])), 3),
        total_trades=sum(r.total_trades for r in results),
        long_trades=sum(r.long_trades for r in results),
        short_trades=sum(r.short_trades for r in results),
    )


# ── HTML Report ───────────────────────────────────────────────────────────────

def generate_report(summary: BacktestSummary, output_path: str) -> str:
    rows = "".join(
        f"<tr><td>{r.ticker}</td>"
        f"<td class='{'pos' if r.sharpe>=1 else 'neg'}'>{r.sharpe:.2f}</td>"
        f"<td class='{'pos' if r.sortino>=1 else 'neg'}'>{r.sortino:.2f}</td>"
        f"<td class='{'pos' if r.cagr_pct>=0 else 'neg'}'>{r.cagr_pct:.1f}%</td>"
        f"<td class='neg'>{r.max_drawdown_pct:.1f}%</td>"
        f"<td>{r.win_rate:.0%}</td>"
        f"<td class='{'pos' if r.profit_factor>=1 else 'neg'}'>{r.profit_factor:.2f}x</td>"
        f"<td><span class='long-badge'>▲{r.long_trades}L</span> / "
        f"<span class='short-badge'>▼{r.short_trades}S</span></td>"
        f"<td class='{'pos' if r.total_pnl>=0 else 'neg'}'>${r.total_pnl:+,.0f}</td></tr>"
        for r in summary.results
    )

    def badge(ok: bool, val: str) -> str:
        cls = "pass" if ok else "fail"
        mark = "✓ PASS" if ok else "✗ FAIL"
        return f"<span class='{cls}'>{mark}</span> <span class='val'>({val})</span>"

    long_pct = summary.long_trades / max(summary.total_trades, 1) * 100
    short_pct = summary.short_trades / max(summary.total_trades, 1) * 100

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8">
<title>NEXUS v3 Backtest — {summary.generated_at[:10]}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Courier New',monospace;background:#0d1b2a;color:#cdd6f4;padding:2rem;line-height:1.6}}
h1{{color:#C5A55A;font-size:2rem;margin-bottom:.25rem}}
.sub{{color:#6c7086;font-size:.85rem;margin-bottom:2rem}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin:1.5rem 0}}
.card{{background:#1B2A4A;border:1px solid #4A6FA5;border-radius:10px;padding:1.25rem;text-align:center}}
.card .val{{font-size:2.2rem;font-weight:700;color:#C5A55A;display:block}}
.card .lbl{{font-size:.75rem;color:#6c7086;margin-top:.3rem}}
.card .sub-stat{{font-size:.8rem;color:#a6e3a1;margin-top:.2rem}}
h2{{color:#4A6FA5;margin:2rem 0 .75rem;font-size:1.1rem;text-transform:uppercase;letter-spacing:.05em}}
table{{width:100%;border-collapse:collapse}}
th{{background:#1B2A4A;color:#C5A55A;padding:.5rem .75rem;text-align:left;border-bottom:2px solid #4A6FA5;font-size:.8rem;text-transform:uppercase}}
td{{padding:.45rem .75rem;border-bottom:1px solid #1e2d42;font-size:.85rem}}
tr:hover{{background:#1a2b3c}}
.pos{{color:#a6e3a1}}.neg{{color:#f38ba8}}
.long-badge{{color:#a6e3a1;font-weight:bold}}.short-badge{{color:#f38ba8;font-weight:bold}}
.targets{{background:#1B2A4A;border:1px solid #4A6FA5;border-radius:10px;padding:1.5rem;margin-top:2rem}}
.target-row{{display:flex;gap:2rem;flex-wrap:wrap;margin-top:.75rem}}
.target-item{{flex:1;min-width:200px}}
.target-item .label{{font-size:.75rem;color:#6c7086;text-transform:uppercase}}
.pass{{color:#a6e3a1;font-weight:700}}.fail{{color:#f38ba8;font-weight:700}}
.val{{color:#cdd6f4}}
footer{{margin-top:2.5rem;color:#6c7086;font-size:.75rem;text-align:center}}
</style>
</head>
<body>
<h1>⚡ NEXUS v3 Backtest Report</h1>
<p class="sub">Generated {summary.generated_at[:19].replace('T', ' ')} UTC &nbsp;·&nbsp;
{summary.years:.1f}-year simulation &nbsp;·&nbsp; Long/Short &nbsp;·&nbsp;
{', '.join(summary.tickers)}</p>

<div class="grid">
  <div class="card"><span class="val {'pos' if summary.portfolio_sharpe>=1 else 'neg'}">{summary.portfolio_sharpe:.2f}</span><div class="lbl">Portfolio Sharpe</div></div>
  <div class="card"><span class="val {'pos' if summary.portfolio_cagr>=0 else 'neg'}">{summary.portfolio_cagr:.1f}%</span><div class="lbl">Portfolio CAGR</div></div>
  <div class="card"><span class="val neg">{summary.portfolio_max_dd:.1f}%</span><div class="lbl">Max Drawdown</div></div>
  <div class="card">
    <span class="val">{summary.portfolio_win_rate:.0%}</span>
    <div class="lbl">Win Rate · {summary.total_trades} trades</div>
    <div class="sub-stat">▲ {long_pct:.0f}% Long &nbsp; ▼ {short_pct:.0f}% Short</div>
  </div>
</div>

<h2>Per-Ticker Results</h2>
<table>
<thead><tr><th>Ticker</th><th>Sharpe</th><th>Sortino</th><th>CAGR</th>
<th>Max DD</th><th>Win Rate</th><th>Profit Factor</th><th>Trades (L/S)</th>
<th>Total P&L</th></tr></thead>
<tbody>{rows}</tbody>
</table>

<div class="targets">
<h2>Targets</h2>
<div class="target-row">
  <div class="target-item"><div class="label">Sharpe &gt; 1.0</div>{badge(summary.portfolio_sharpe>=1.0, f'{summary.portfolio_sharpe:.2f}')}</div>
  <div class="target-item"><div class="label">Max Drawdown &lt; 20%</div>{badge(summary.portfolio_max_dd<20, f'{summary.portfolio_max_dd:.1f}%')}</div>
  <div class="target-item"><div class="label">Positive CAGR</div>{badge(summary.portfolio_cagr>0, f'{summary.portfolio_cagr:.1f}%')}</div>
  <div class="target-item"><div class="label">Short Trades &gt; 0</div>{badge(summary.short_trades>0, f'{summary.short_trades} trades')}</div>
</div>
</div>

<footer>NEXUS v3 &nbsp;·&nbsp; Long/Short &nbsp;·&nbsp; Built by Pruthvi Garlapati &nbsp;·&nbsp; github.com/pruthvig1998/nexus</footer>
</body></html>"""

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)
    log.info("Report saved", path=output_path)
    return output_path
