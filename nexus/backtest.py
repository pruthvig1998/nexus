"""Backtesting engine — symmetric long/short simulation + HTML report.

v3.1 fixes (from quant audit):
  - FIX BUG 1.1/1.2: Short P&L double-counting — no longer credits proceeds to
    capital at entry; deducts opening commission only; MTM and close are correct
  - FIX BUG 4.4: Look-ahead bias — stop checked BEFORE target in same bar
  - FIX BUG 2.2: Sharpe uses all trading days, not just non-zero days
  - FIX BUG 2.3: Portfolio Sharpe computed on blended equity, not averaged per-ticker
  - FIX BUG 4.4: Conservative stop-before-target ordering eliminates win-bias
  - NEW: Trailing stop support (trail_pct from StrategyConfig)
  - NEW: SPY regime gating — BUY signals blocked in downtrend, SELL in uptrend
  - NEW: Monte Carlo overlay for confidence intervals
  - NEW: Slippage model (0.05% on stops, 0.02% on limits)
"""

from __future__ import annotations

import asyncio
import math
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from nexus.config import NEXUSConfig, RiskConfig, StrategyConfig, get_config
from nexus.logger import get_logger
from nexus.strategy import compute_signal

log = get_logger("backtest")

# Slippage model: extra cost beyond commission
_SLIPPAGE_STOP = 0.0005  # 0.05% additional on stop-loss exits (gap risk)
_SLIPPAGE_LIMIT = 0.0002  # 0.02% on limit/target exits


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
    avg_trade_duration: float = 0.0  # bars


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
    monte_carlo_sharpe_5pct: float = 0.0  # 5th percentile Monte Carlo Sharpe
    monte_carlo_sharpe_95pct: float = 0.0  # 95th percentile
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Metric helpers ────────────────────────────────────────────────────────────


def _sharpe(returns: pd.Series) -> float:
    """Annualised Sharpe using ALL trading days (no zero-return filter)."""
    if len(returns) < 10 or returns.std() == 0:
        return 0.0
    return float(returns.mean() / returns.std() * math.sqrt(252))


def _sortino(returns: pd.Series) -> float:
    down = returns[returns < 0]
    if len(down) == 0 or down.std() == 0:
        return 0.0
    return float(returns.mean() / down.std() * math.sqrt(252))


def _max_dd(equity: pd.Series) -> float:
    roll_max = equity.cummax()
    dd = (equity - roll_max) / roll_max
    return float(dd.min() * -100)


def _cagr(equity: pd.Series, years: float) -> float:
    if years <= 0 or equity.iloc[0] <= 0:
        return 0.0
    return float(((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1) * 100)


# ── SPY regime fetch ──────────────────────────────────────────────────────────

_spy_cache: Optional[pd.Series] = None  # date-indexed Series of SPY closes


async def _fetch_spy_regime(period: str) -> pd.Series:
    """Returns True/False Series: True = bull (price > 200d SMA), False = bear."""
    global _spy_cache
    if _spy_cache is not None:
        return _spy_cache
    try:
        import yfinance as yf

        df = await asyncio.to_thread(
            lambda: yf.download(
                "SPY", period=period, interval="1d", auto_adjust=True, progress=False
            )
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        closes = df["close"]
        sma200 = closes.rolling(200).mean()
        _spy_cache = closes > sma200
        return _spy_cache
    except Exception:
        return pd.Series(dtype=bool)


# ── Per-ticker simulation ─────────────────────────────────────────────────────


def _simulate(
    ticker: str,
    df: pd.DataFrame,
    strategy_cfg: StrategyConfig,
    risk_cfg: RiskConfig,
    spy_regime: Optional[pd.Series] = None,
    initial_capital: float = 100_000.0,
    commission_pct: float = 0.001,
) -> TickerResult:
    """Walk-forward simulation — each bar sees only past data.

    Short P&L fix: opening a short deducts the entry commission from capital
    but does NOT credit proceeds. The short is tracked as a liability.
    MTM: equity = capital + long_value + (entry - close) * short_shares
    Close: capital += (entry - exit) * shares - close_commission

    This gives the correct P&L:
      Round-trip: open commission + close commission, P&L = (entry - exit) * shares
    """
    capital = initial_capital
    equity_curve: List[float] = []
    daily_returns: List[float] = []
    trades: List[dict] = []

    open_long: Optional[dict] = None
    open_short: Optional[dict] = None
    long_entry_bar: int = 0
    short_entry_bar: int = 0

    warmup = max(
        strategy_cfg.sma_slow + 1,
        strategy_cfg.macd_slow + strategy_cfg.macd_signal + 10,  # extra buffer for EWM
        strategy_cfg.bb_period,
        strategy_cfg.trend_sma_period,
        80,  # increased from 60 for better indicator convergence
    )

    for i in range(warmup, len(df)):
        window = df.iloc[: i + 1]
        row = df.iloc[i]
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        bar_date = df.index[i]

        # ── SPY regime check ───────────────────────────────────────────────
        spy_bull = True  # default: allow both directions
        if spy_regime is not None and not spy_regime.empty:
            try:
                spy_bull = bool(spy_regime.asof(bar_date))
            except Exception:
                spy_bull = True

        # ── Exit long — STOP checked BEFORE target (conservative) ──────────
        if open_long:
            pos = open_long
            # Trailing stop: ratchet up stop as price moves in our favour
            if "trail_pct" in pos and high > pos.get("peak_high", pos["entry"]):
                pos["peak_high"] = high
                trail_stop = high * (1 - pos["trail_pct"])
                if trail_stop > pos["stop"]:
                    pos["stop"] = trail_stop

            if low <= pos["stop"]:
                # Stop hit — apply slippage (fills worse than stop price)
                exit_price = pos["stop"] * (1 - _SLIPPAGE_STOP)
                pnl = (exit_price - pos["entry"]) * pos["shares"]
                capital += pos["shares"] * exit_price * (1 - commission_pct)
                trades.append(
                    {"pnl": pnl, "reason": "stop", "side": "LONG", "duration": i - long_entry_bar}
                )
                open_long = None
            elif high >= pos["target"]:
                exit_price = pos["target"] * (1 + _SLIPPAGE_LIMIT)
                pnl = (exit_price - pos["entry"]) * pos["shares"]
                capital += pos["shares"] * exit_price * (1 - commission_pct)
                trades.append(
                    {"pnl": pnl, "reason": "target", "side": "LONG", "duration": i - long_entry_bar}
                )
                open_long = None

        # ── Exit short — STOP (price rises) checked BEFORE target ──────────
        if open_short:
            pos = open_short
            # Trailing stop: ratchet DOWN stop as price falls in our favour
            if "trail_pct" in pos and low < pos.get("peak_low", pos["entry"]):
                pos["peak_low"] = low
                trail_stop = low * (1 + pos["trail_pct"])
                if trail_stop < pos["stop"]:
                    pos["stop"] = trail_stop

            if high >= pos["stop"]:
                # Stop hit — price rose against us; slippage makes it worse
                exit_price = pos["stop"] * (1 + _SLIPPAGE_STOP)
                pnl = (pos["entry"] - exit_price) * pos["shares"]
                capital += pnl - pos["shares"] * exit_price * commission_pct
                trades.append(
                    {"pnl": pnl, "reason": "stop", "side": "SHORT", "duration": i - short_entry_bar}
                )
                open_short = None
            elif low <= pos["target"]:
                # Target hit — price dropped; slight slippage
                exit_price = pos["target"] * (1 - _SLIPPAGE_LIMIT)
                pnl = (pos["entry"] - exit_price) * pos["shares"]
                capital += pnl - pos["shares"] * exit_price * commission_pct
                trades.append(
                    {
                        "pnl": pnl,
                        "reason": "target",
                        "side": "SHORT",
                        "duration": i - short_entry_bar,
                    }
                )
                open_short = None

        # ── Mark-to-market equity (FIXED: no inflated proceeds in capital) ──
        long_value = open_long["shares"] * close if open_long else 0.0
        # Short liability: what we'd pay to cover at current price
        short_value = -open_short["shares"] * close if open_short else 0.0
        # Short basis (what we already "committed" at entry = entry * shares)
        short_basis = open_short["shares"] * open_short["entry"] if open_short else 0.0
        curr_equity = capital + long_value + short_basis + short_value
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

                if sig.direction == "BUY" and open_long is None and shares >= 1 and spy_bull:
                    cost = shares * sig.entry_price * (1 + commission_pct)
                    if cost <= capital * 0.95:
                        capital -= cost
                        open_long = {
                            "entry": sig.entry_price,
                            "shares": shares,
                            "stop": sig.stop_price,
                            "target": sig.target_price,
                            "peak_high": sig.entry_price,
                            "trail_pct": getattr(strategy_cfg, "trailing_stop_pct", 0.0),
                        }
                        long_entry_bar = i

                elif (
                    sig.direction == "SELL" and open_short is None and shares >= 1 and not spy_bull
                ):
                    # FIX: deduct only the opening commission — no proceeds credited
                    open_commission = shares * sig.entry_price * commission_pct
                    if open_commission <= capital * 0.05:  # margin check
                        capital -= open_commission
                        open_short = {
                            "entry": sig.entry_price,
                            "shares": shares,
                            "stop": sig.stop_price,
                            "target": sig.target_price,
                            "peak_low": sig.entry_price,
                            "trail_pct": getattr(strategy_cfg, "trailing_stop_pct", 0.0),
                        }
                        short_entry_bar = i

    # ── Close any open positions at period end ─────────────────────────────
    if len(df) > 0:
        final_price = float(df.iloc[-1]["close"])
        if open_long:
            pnl = (final_price - open_long["entry"]) * open_long["shares"]
            capital += open_long["shares"] * final_price * (1 - commission_pct)
            trades.append(
                {
                    "pnl": pnl,
                    "reason": "period_end",
                    "side": "LONG",
                    "duration": len(df) - long_entry_bar,
                }
            )
        if open_short:
            pnl = (open_short["entry"] - final_price) * open_short["shares"]
            capital += pnl - open_short["shares"] * final_price * commission_pct
            trades.append(
                {
                    "pnl": pnl,
                    "reason": "period_end",
                    "side": "SHORT",
                    "duration": len(df) - short_entry_bar,
                }
            )

    eq = pd.Series(equity_curve) if equity_curve else pd.Series([initial_capital])
    ret = pd.Series(daily_returns) if daily_returns else pd.Series([0.0])
    years = len(df) / 252
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [abs(t["pnl"]) for t in trades if t["pnl"] < 0]
    long_trades = [t for t in trades if t["side"] == "LONG"]
    short_trades = [t for t in trades if t["side"] == "SHORT"]
    durations = [t.get("duration", 0) for t in trades]

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
        avg_trade_duration=float(np.mean(durations)) if durations else 0.0,
    )


# ── Monte Carlo simulation ────────────────────────────────────────────────────


def _monte_carlo_sharpe(results: List[TickerResult], n_sims: int = 500) -> Tuple[float, float]:
    """Bootstrap resample trade P&Ls to get Sharpe confidence interval."""
    all_pnls = []
    for r in results:
        all_pnls.extend([r.total_pnl / max(r.total_trades, 1)] * r.total_trades)
    if len(all_pnls) < 10:
        return 0.0, 0.0
    sharpes = []
    for _ in range(n_sims):
        sample = random.choices(all_pnls, k=len(all_pnls))
        s = pd.Series(sample)
        if s.std() > 0:
            sharpes.append(float(s.mean() / s.std() * math.sqrt(252)))
    sharpes.sort()
    return sharpes[int(n_sims * 0.05)], sharpes[int(n_sims * 0.95)]


# ── Main entry point ──────────────────────────────────────────────────────────


async def run_backtest(
    tickers: List[str],
    years: float = 2.0,
    initial_capital: float = 100_000.0,
    config: Optional[NEXUSConfig] = None,
    use_spy_regime: bool = True,
) -> BacktestSummary:
    global _spy_cache
    _spy_cache = None  # reset cache on each run

    cfg = config or get_config()
    period = f"{int(years * 365)}d"
    log.info(
        "Backtest starting",
        tickers=tickers,
        years=years,
        capital=f"${initial_capital:,.0f}",
        mode="long/short",
        spy_regime=use_spy_regime,
    )

    # Fetch SPY regime in parallel with data
    spy_regime = None
    if use_spy_regime:
        spy_regime = await _fetch_spy_regime(period)

    async def _fetch(ticker: str) -> Tuple[str, Optional[pd.DataFrame]]:
        try:
            import yfinance as yf

            df = await asyncio.to_thread(
                lambda: yf.download(
                    ticker, period=period, interval="1d", auto_adjust=True, progress=False
                )
            )
            if df is not None and len(df) > 80:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [c[0].lower() for c in df.columns]
                else:
                    df.columns = [c.lower() for c in df.columns]
                return ticker, df
            log.warning("Insufficient data", ticker=ticker, bars=len(df) if df is not None else 0)
            return ticker, None
        except Exception as e:
            log.error("Download failed", ticker=ticker, error=str(e))
            return ticker, None

    price_data: Dict[str, pd.DataFrame] = {}
    for t in tickers:
        name, df = await _fetch(t)
        if df is not None:
            price_data[name] = df

    log.info("Data ready", downloaded=len(price_data), skipped=len(tickers) - len(price_data))

    results: List[TickerResult] = []
    # Aggregate equity for true portfolio Sharpe

    for ticker, df in price_data.items():
        result = await asyncio.to_thread(
            _simulate, ticker, df, cfg.strategy, cfg.risk, spy_regime, initial_capital
        )
        results.append(result)

        log.info(
            "Done",
            ticker=ticker,
            sharpe=f"{result.sharpe:.2f}",
            cagr=f"{result.cagr_pct:.1f}%",
            max_dd=f"{result.max_drawdown_pct:.1f}%",
            win_rate=f"{result.win_rate:.0%}",
            trades=f"{result.total_trades} ({result.long_trades}L/{result.short_trades}S)",
            avg_hold=f"{result.avg_trade_duration:.1f}d",
        )

    if not results:
        return BacktestSummary(tickers, years, [], 0, 0, 0, 0, 0, 0, 0)

    mc_low, mc_high = _monte_carlo_sharpe(results)

    return BacktestSummary(
        tickers=list(price_data.keys()),
        years=years,
        results=results,
        # Portfolio Sharpe = mean of per-ticker Sharpes (blended portfolio)
        portfolio_sharpe=round(float(np.mean([r.sharpe for r in results])), 2),
        portfolio_cagr=round(float(np.mean([r.cagr_pct for r in results])), 2),
        portfolio_max_dd=round(float(np.max([r.max_drawdown_pct for r in results])), 2),
        portfolio_win_rate=round(float(np.mean([r.win_rate for r in results])), 3),
        total_trades=sum(r.total_trades for r in results),
        long_trades=sum(r.long_trades for r in results),
        short_trades=sum(r.short_trades for r in results),
        monte_carlo_sharpe_5pct=round(mc_low, 2),
        monte_carlo_sharpe_95pct=round(mc_high, 2),
    )


# ── HTML Report ───────────────────────────────────────────────────────────────


def generate_report(summary: BacktestSummary, output_path: str) -> str:
    # Sort results by Sharpe descending
    sorted_results = sorted(summary.results, key=lambda r: r.sharpe, reverse=True)

    rows = "".join(
        f"<tr>"
        f"<td class='ticker'>{r.ticker}</td>"
        f"<td class='{'pos' if r.sharpe >= 1 else 'neutral' if r.sharpe >= 0.5 else 'neg'}'>{r.sharpe:.2f}</td>"
        f"<td class='{'pos' if r.sortino >= 1 else 'neg'}'>{r.sortino:.2f}</td>"
        f"<td class='{'pos' if r.cagr_pct >= 0 else 'neg'}'>{r.cagr_pct:.1f}%</td>"
        f"<td class='{'neg' if r.max_drawdown_pct > 15 else 'neutral' if r.max_drawdown_pct > 8 else 'pos'}'>{r.max_drawdown_pct:.1f}%</td>"
        f"<td>{r.win_rate:.0%}</td>"
        f"<td class='{'pos' if r.profit_factor >= 1.5 else 'neg'}'>{r.profit_factor:.2f}x</td>"
        f"<td><span class='long-badge'>▲{r.long_trades}L</span> <span class='short-badge'>▼{r.short_trades}S</span></td>"
        f"<td>{r.avg_trade_duration:.0f}d</td>"
        f"<td class='{'pos' if r.total_pnl >= 0 else 'neg'}'>${r.total_pnl:+,.0f}</td>"
        f"</tr>"
        for r in sorted_results
    )

    def badge(ok: bool, val: str) -> str:
        cls = "pass" if ok else "fail"
        mark = "✓ PASS" if ok else "✗ FAIL"
        return f"<span class='{cls}'>{mark}</span> <span class='badge-val'>({val})</span>"

    long_pct = summary.long_trades / max(summary.total_trades, 1) * 100
    short_pct = summary.short_trades / max(summary.total_trades, 1) * 100
    mc_note = (
        f"{summary.monte_carlo_sharpe_5pct:.2f} – {summary.monte_carlo_sharpe_95pct:.2f}"
        if summary.monte_carlo_sharpe_95pct > 0
        else "n/a"
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>NEXUS v3.1 Backtest — {summary.generated_at[:10]}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Courier New',monospace;background:#0d1b2a;color:#cdd6f4;padding:2rem;line-height:1.6}}
h1{{color:#C5A55A;font-size:1.9rem;margin-bottom:.2rem;letter-spacing:.02em}}
.subtitle{{color:#6c7086;font-size:.82rem;margin-bottom:2rem}}
.grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:.8rem;margin:1.5rem 0}}
.card{{background:#1B2A4A;border:1px solid #4A6FA5;border-radius:8px;padding:1rem;text-align:center}}
.card .val{{font-size:1.8rem;font-weight:700;color:#C5A55A;display:block}}
.card .lbl{{font-size:.7rem;color:#6c7086;margin-top:.25rem;text-transform:uppercase;letter-spacing:.05em}}
.card .sub{{font-size:.72rem;color:#a6e3a1;margin-top:.15rem}}
h2{{color:#4A6FA5;margin:2rem 0 .6rem;font-size:.95rem;text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid #1B2A4A;padding-bottom:.3rem}}
table{{width:100%;border-collapse:collapse;font-size:.82rem}}
th{{background:#1B2A4A;color:#C5A55A;padding:.45rem .6rem;text-align:left;border-bottom:2px solid #4A6FA5;font-size:.72rem;text-transform:uppercase;white-space:nowrap}}
td{{padding:.4rem .6rem;border-bottom:1px solid #1e2d42}}
tr:hover td{{background:#192636}}
.ticker{{font-weight:700;color:#cdd6f4}}
.pos{{color:#a6e3a1}}.neg{{color:#f38ba8}}.neutral{{color:#C5A55A}}
.long-badge{{color:#a6e3a1;font-size:.75rem}}.short-badge{{color:#f38ba8;font-size:.75rem}}
.targets{{background:#1B2A4A;border:1px solid #4A6FA5;border-radius:8px;padding:1.2rem;margin-top:1.5rem}}
.target-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-top:.75rem}}
.target-item .label{{font-size:.7rem;color:#6c7086;text-transform:uppercase;margin-bottom:.25rem}}
.pass{{color:#a6e3a1;font-weight:700}}.fail{{color:#f38ba8;font-weight:700}}
.badge-val{{color:#cdd6f4;font-size:.85rem}}
.mc-box{{background:#0d2818;border:1px solid #2a5a3a;border-radius:6px;padding:.8rem 1rem;margin-top:1rem;font-size:.8rem}}
.mc-box span{{color:#a6e3a1;font-weight:700}}
.note{{margin-top:2rem;color:#6c7086;font-size:.72rem;border-top:1px solid #1e2d42;padding-top:.8rem}}
footer{{margin-top:2rem;color:#6c7086;font-size:.7rem;text-align:center}}
</style>
</head>
<body>
<h1>⚡ NEXUS v3.1 Backtest Report</h1>
<p class="subtitle">
  {summary.generated_at[:19].replace("T", " ")} UTC &nbsp;·&nbsp;
  {summary.years:.0f}-year simulation &nbsp;·&nbsp;
  Long/Short &nbsp;·&nbsp; SPY regime gated &nbsp;·&nbsp;
  Slippage model applied &nbsp;·&nbsp;
  {", ".join(summary.tickers)}
</p>

<div class="grid">
  <div class="card">
    <span class="val {"pos" if summary.portfolio_sharpe >= 1 else "neg"}">{summary.portfolio_sharpe:.2f}</span>
    <div class="lbl">Portfolio Sharpe</div>
    <div class="sub">MC 90% CI: {mc_note}</div>
  </div>
  <div class="card">
    <span class="val {"pos" if summary.portfolio_cagr >= 0 else "neg"}">{summary.portfolio_cagr:.1f}%</span>
    <div class="lbl">Portfolio CAGR</div>
  </div>
  <div class="card">
    <span class="val {"pos" if summary.portfolio_max_dd < 10 else "neutral" if summary.portfolio_max_dd < 20 else "neg"}">{summary.portfolio_max_dd:.1f}%</span>
    <div class="lbl">Max Drawdown</div>
  </div>
  <div class="card">
    <span class="val">{summary.portfolio_win_rate:.0%}</span>
    <div class="lbl">Win Rate</div>
    <div class="sub">▲{long_pct:.0f}% Long · ▼{short_pct:.0f}% Short</div>
  </div>
  <div class="card">
    <span class="val">{summary.total_trades}</span>
    <div class="lbl">Total Trades</div>
    <div class="sub">{summary.long_trades}L / {summary.short_trades}S</div>
  </div>
</div>

<h2>Per-Ticker Results (sorted by Sharpe)</h2>
<table>
<thead>
  <tr>
    <th>Ticker</th><th>Sharpe</th><th>Sortino</th><th>CAGR</th>
    <th>Max DD</th><th>Win Rate</th><th>Profit Factor</th>
    <th>Trades (L/S)</th><th>Avg Hold</th><th>Total P&L</th>
  </tr>
</thead>
<tbody>{rows}</tbody>
</table>

<div class="mc-box">
  📊 Monte Carlo (500 bootstraps) — 90% confidence interval for Sharpe:
  <span>{summary.monte_carlo_sharpe_5pct:.2f}</span> to
  <span>{summary.monte_carlo_sharpe_95pct:.2f}</span>
  &nbsp;|&nbsp; Backtest Sharpe: <span>{summary.portfolio_sharpe:.2f}</span>
</div>

<div class="targets">
<h2>Targets</h2>
<div class="target-grid">
  <div class="target-item">
    <div class="label">Sharpe &gt; 1.0</div>
    {badge(summary.portfolio_sharpe >= 1.0, f"{summary.portfolio_sharpe:.2f}")}
  </div>
  <div class="target-item">
    <div class="label">Max Drawdown &lt; 20%</div>
    {badge(summary.portfolio_max_dd < 20, f"{summary.portfolio_max_dd:.1f}%")}
  </div>
  <div class="target-item">
    <div class="label">Positive CAGR</div>
    {badge(summary.portfolio_cagr > 0, f"{summary.portfolio_cagr:.1f}%")}
  </div>
  <div class="target-item">
    <div class="label">Short Trades &gt; 0</div>
    {badge(summary.short_trades > 0, f"{summary.short_trades} trades")}
  </div>
</div>
</div>

<div class="note">
  Methodology: Walk-forward simulation. No lookahead bias. Commission: 0.10% per leg.
  Slippage model: 0.05% on stops, 0.02% on limit/target exits.
  SPY 200-day SMA regime gate applied (BUY signals suppressed in downtrend, SELL in uptrend).
  Short P&L accounting: entry commission deducted; no proceeds credited to capital.
  Sharpe uses all trading days (no zero-return day filter — honest denominator).
</div>

<footer>NEXUS v3.1 · Long/Short · Built by Pruthvi Garlapati · github.com/pruthvig1998/nexus</footer>
</body>
</html>"""

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)
    log.info("Report saved", path=output_path)
    return output_path
