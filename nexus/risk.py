"""Risk management — Kelly+ATR position sizing and limit checks.

v3 changes:
  - RiskConfig.max_short_exposure_pct: float = 0.50 (short book capped at 50%)
  - RiskLimits.check() tracks long vs short exposure separately
  - signal_direction parameter routes the correct exposure check
  - Removed var_confidence dead code
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from nexus.config import RiskConfig, get_config
from nexus.logger import get_logger

log = get_logger("risk")


# ── Position Sizing ───────────────────────────────────────────────────────────

def kelly_fraction(win_rate: float, win_loss_ratio: float,
                   fraction: float = 0.25) -> float:
    if win_loss_ratio <= 0:
        return 0.0
    loss_rate = 1.0 - win_rate
    kelly = (win_rate * win_loss_ratio - loss_rate) / win_loss_ratio
    return max(0.0, min(kelly * fraction, 0.20))


def size_position(portfolio_value: float, cash: float, entry_price: float,
                  stop_price: float, signal_score: float,
                  win_rate: float = 0.55, avg_win: float = 1.5,
                  avg_loss: float = 1.0, kelly_frac: float = 0.25,
                  max_position_pct: float = 0.05) -> int:
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share < 0.001 or entry_price <= 0:
        return 0

    # ATR-based size: risk 1% of portfolio per trade
    atr_shares = int((portfolio_value * 0.01) / risk_per_share)
    atr_shares = min(atr_shares, int(portfolio_value * max_position_pct / entry_price))

    # Kelly size scaled by signal conviction
    wl_ratio = avg_win / max(avg_loss, 0.001)
    k = kelly_fraction(win_rate, wl_ratio, kelly_frac)
    kelly_shares = int(portfolio_value * k * signal_score / entry_price)

    shares = min(atr_shares, kelly_shares) if kelly_shares > 0 else atr_shares
    shares = min(shares, int(cash * 0.95 / entry_price))
    return max(shares, 0)


# ── Risk Limits ───────────────────────────────────────────────────────────────

@dataclass
class RiskCheckResult:
    approved: bool
    reason: str
    adjusted_shares: Optional[int] = None


class RiskLimits:
    def __init__(self, config: Optional[RiskConfig] = None) -> None:
        self._cfg = config or get_config().risk
        self._halted = False
        self._daily_pnl = 0.0

    def update_daily_pnl(self, pnl: float, portfolio_value: float = 0.0) -> None:
        self._daily_pnl = pnl
        if portfolio_value > 0 and pnl < 0:
            loss_pct = abs(pnl) / portfolio_value
            if loss_pct > self._cfg.daily_loss_halt_pct:
                self._halted = True
                log.warning("Daily loss halt triggered",
                            loss_pct=f"{loss_pct:.1%}",
                            threshold=f"{self._cfg.daily_loss_halt_pct:.1%}")

    def reset_daily(self) -> None:
        self._halted = False
        self._daily_pnl = 0.0

    @property
    def is_halted(self) -> bool:
        return self._halted

    def check(self, signal_score: float, portfolio_value: float, cash: float,
              open_positions: List, proposed_shares: int,
              entry_price: float, signal_direction: str = "BUY") -> RiskCheckResult:
        """Check risk limits before opening a position.

        Args:
            signal_direction: "BUY" (open long) or "SELL" (open short)
        """
        cfg = self._cfg

        if self._halted:
            return RiskCheckResult(False, "Daily loss halt active")

        if signal_score < get_config().strategy.min_signal_score:
            return RiskCheckResult(False,
                f"Score {signal_score:.2f} < min {get_config().strategy.min_signal_score:.2f}")

        if len(open_positions) >= cfg.max_open_positions:
            return RiskCheckResult(False,
                f"Max positions ({cfg.max_open_positions}) reached")

        proposed_value = proposed_shares * entry_price

        # Per-position size cap
        if portfolio_value > 0 and proposed_value / portfolio_value > cfg.max_position_pct:
            max_shares = int(portfolio_value * cfg.max_position_pct / entry_price)
            if max_shares < 1:
                return RiskCheckResult(False, "Position too small after cap")
            return RiskCheckResult(True, "Approved (size-capped)",
                                   adjusted_shares=max_shares)

        # Separate long/short exposure tracking
        longs = [p for p in open_positions if getattr(p, "side", "LONG") == "LONG"]
        shorts = [p for p in open_positions if getattr(p, "side", "LONG") != "LONG"]
        long_exposure = sum(p.shares * p.current_price for p in longs)
        short_exposure = sum(p.shares * p.current_price for p in shorts)

        if portfolio_value > 0:
            if signal_direction == "BUY":
                new_long_exp = (long_exposure + proposed_value) / portfolio_value
                if new_long_exp > cfg.max_portfolio_exposure:
                    return RiskCheckResult(False,
                        f"Long exposure {new_long_exp:.1%} > limit {cfg.max_portfolio_exposure:.1%}")
            else:  # SELL = open short
                new_short_exp = (short_exposure + proposed_value) / portfolio_value
                if new_short_exp > cfg.max_short_exposure_pct:
                    return RiskCheckResult(False,
                        f"Short exposure {new_short_exp:.1%} > limit {cfg.max_short_exposure_pct:.1%}")

        if proposed_value > cash * 0.95:
            max_affordable = int(cash * 0.95 / max(entry_price, 0.01))
            if max_affordable < 1:
                return RiskCheckResult(False, "Insufficient cash")
            return RiskCheckResult(True, "Approved (cash-limited)",
                                   adjusted_shares=max_affordable)

        return RiskCheckResult(True, "Approved", adjusted_shares=proposed_shares)
