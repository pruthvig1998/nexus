"""Options strategy — converts equity signals to options trades (calls/puts).

When enabled, strong BUY signals → buy CALL, strong SELL signals → buy PUT.
Selects contracts near target DTE with appropriate strike selection.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List, Optional

from nexus.broker import OptionsQuote
from nexus.config import OptionsConfig, get_config
from nexus.logger import get_logger
from nexus.strategy import Signal

if TYPE_CHECKING:
    from nexus.broker import BaseBroker

log = get_logger("strategy.options")


def select_expiration(expirations: List[str], cfg: OptionsConfig) -> Optional[str]:
    """Pick the expiration closest to target_dte within [min_dte, max_dte]."""
    today = datetime.now().date()
    target = today + timedelta(days=cfg.target_dte)
    best: Optional[str] = None
    best_dist = float("inf")

    for exp_str in expirations:
        try:
            exp_date = datetime.strptime(exp_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (exp_date - today).days
        if dte < cfg.min_dte or dte > cfg.max_dte:
            continue
        dist = abs((exp_date - target).days)
        if dist < best_dist:
            best_dist = dist
            best = exp_str[:10]

    return best


def select_strike(
    chain: List[OptionsQuote],
    right: str,
    underlying_price: float,
    cfg: OptionsConfig,
) -> Optional[OptionsQuote]:
    """Select a strike from the chain.

    For CALL: ATM or slightly OTM (strike >= underlying).
    For PUT: ATM or slightly OTM (strike <= underlying).
    Filters by min open interest and volume.
    """
    candidates = [
        q
        for q in chain
        if q.contract.right == right
        and q.open_interest >= cfg.min_open_interest
        and q.volume >= cfg.min_volume
        and q.ask > 0
    ]

    if not candidates:
        # Relax OI/volume filters if no candidates
        candidates = [q for q in chain if q.contract.right == right and q.ask > 0]

    if not candidates:
        return None

    # Sort by distance from underlying price
    if right == "CALL":
        # OTM calls: strike >= price, sorted ascending
        otm = sorted(
            [q for q in candidates if q.contract.strike >= underlying_price],
            key=lambda q: q.contract.strike,
        )
        if len(otm) > cfg.strike_offset:
            return otm[cfg.strike_offset]
        return otm[0] if otm else min(candidates, key=lambda q: abs(q.contract.strike - underlying_price))
    else:
        # OTM puts: strike <= price, sorted descending
        otm = sorted(
            [q for q in candidates if q.contract.strike <= underlying_price],
            key=lambda q: q.contract.strike,
            reverse=True,
        )
        if len(otm) > cfg.strike_offset:
            return otm[cfg.strike_offset]
        return otm[0] if otm else min(candidates, key=lambda q: abs(q.contract.strike - underlying_price))


async def convert_signal_to_option(
    signal: Signal,
    broker: BaseBroker,
    portfolio_value: float,
) -> Optional[Signal]:
    """Convert an equity signal to an options signal.

    BUY → buy CALL, SELL → buy PUT.
    Returns None if no suitable contract found.
    """
    cfg = get_config().options
    if not cfg.enabled:
        return None

    if signal.score < cfg.min_signal_score:
        return None

    right = "CALL" if signal.direction == "BUY" else "PUT"
    ticker = signal.ticker

    # Get expirations
    expirations = await broker.get_option_expirations(ticker)
    if not expirations:
        log.debug("No option expirations available", ticker=ticker)
        return None

    # Select expiration
    exp = select_expiration(expirations, cfg)
    if not exp:
        log.debug("No suitable expiration found", ticker=ticker, min_dte=cfg.min_dte, max_dte=cfg.max_dte)
        return None

    # Get chain
    chain = await broker.get_option_chain(ticker, exp)
    if not chain:
        log.debug("Option chain empty", ticker=ticker, expiration=exp)
        return None

    # Select strike
    quote = select_strike(chain, right, signal.entry_price, cfg)
    if not quote:
        log.debug("No suitable strike found", ticker=ticker, right=right)
        return None

    # Size: max_premium_pct of portfolio, each contract = 100 shares
    premium_per_contract = quote.ask * 100  # cost per contract
    if premium_per_contract <= 0:
        return None

    max_spend = portfolio_value * cfg.max_premium_pct
    contracts = max(1, int(max_spend / premium_per_contract))

    # Build options signal
    opt_signal = Signal(
        ticker=ticker,
        direction="BUY",  # always buying options (calls or puts)
        score=signal.score,
        strategy=f"options_{signal.strategy}",
        reasoning=f"{right} {quote.contract.strike:.0f} {exp} | {signal.reasoning}",
        entry_price=quote.ask,  # premium price
        stop_price=quote.ask * (1 - cfg.stop_loss_pct),
        target_price=quote.ask * (1 + cfg.profit_target_pct),
        limit_price=quote.ask,
        shares=0,
        rsi_val=signal.rsi_val,
        macd_hist=signal.macd_hist,
        bb_pct_b=signal.bb_pct_b,
        atr_val=signal.atr_val,
        vol_ratio=signal.vol_ratio,
        instrument_type=right,
        option_strike=quote.contract.strike,
        option_expiration=exp,
        option_code=quote.contract.code,
        contracts=contracts,
    )

    log.info(
        "Options signal",
        ticker=ticker,
        right=right,
        strike=quote.contract.strike,
        exp=exp,
        premium=f"${quote.ask:.2f}",
        contracts=contracts,
        score=f"{signal.score:.2f}",
    )
    return opt_signal
