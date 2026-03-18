"""Intelligent DTE selection based on market conditions.

Maps signal characteristics (strategy type, conviction, VIX) to the optimal
options expiration window.  Five profiles:

    SCALP         0-2 DTE    Momentum/ORB, high VIX, quick gamma plays
    SWING         3-14 DTE   Mean reversion, IronGrid patterns, weekly options
    POSITION     14-45 DTE   High-conviction trend trades, earnings setups
    MULTI_MONTH  45-180 DTE  Sector rotation, fundamental thesis trades
    LEAPS       180-730 DTE  Deep-value / low-VIX environment, long thesis
"""

from __future__ import annotations

from typing import Tuple

from nexus.logger import get_logger

log = get_logger("dte_engine")

# ── DTE profiles ─────────────────────────────────────────────────────────────

SCALP = (0, 2)
SWING = (3, 14)
POSITION = (14, 45)
MULTI_MONTH = (45, 180)
LEAPS = (180, 730)

# Strategy → base DTE profile mapping
_STRATEGY_PROFILE: dict[str, tuple[int, int]] = {
    # Short-duration strategies
    "momentum": SCALP,
    "orb": SCALP,
    # Medium-duration strategies
    "mean_reversion": SWING,
    "irongrid": SWING,
    "news_sentiment": SWING,
    "event_calendar": SWING,
    # Longer-duration strategies
    "ai_fundamental": POSITION,
    # Options-prefixed versions (from convert_signal_to_option)
    "options_momentum": SCALP,
    "options_orb": SCALP,
    "options_mean_reversion": SWING,
    "options_irongrid": SWING,
    "options_news_sentiment": SWING,
    "options_event_calendar": SWING,
    "options_ai_fundamental": POSITION,
}


def select_dte_profile(
    strategy: str,
    signal_score: float,
    vix: float = 20.0,
) -> Tuple[int, int]:
    """Select optimal DTE range based on strategy, conviction, and VIX.

    Returns (min_dte, max_dte) tuple for the recommended expiration window.

    Logic:
    - Base profile from strategy type
    - VIX shift: high VIX (>25) → shift shorter (premium expensive, faster moves)
                 low VIX (<15) → shift longer (premium cheap, buy more time)
    - Conviction shift: score > 0.85 → shift one level longer (more time to be right)
                        score < 0.70 → stay at base or shift shorter
    """
    # Strip 'options_' prefix for lookup
    clean_strategy = strategy.removeprefix("options_")
    base = _STRATEGY_PROFILE.get(clean_strategy, SWING)

    # Start with base profile
    min_dte, max_dte = base

    # ── VIX adjustment ────────────────────────────────────────────────────
    if vix > 30:
        # Extreme fear: stay very short, premium is expensive
        min_dte = max(0, min_dte)
        max_dte = min(max_dte, 7)
    elif vix > 25:
        # Elevated: shift one level shorter
        min_dte, max_dte = _shift_shorter(min_dte, max_dte)
    elif vix < 12:
        # Very low VIX: premium cheap, LEAPS are attractive
        min_dte, max_dte = _shift_longer(min_dte, max_dte, steps=2)
    elif vix < 15:
        # Low VIX: shift one level longer
        min_dte, max_dte = _shift_longer(min_dte, max_dte)

    # ── Conviction adjustment ─────────────────────────────────────────────
    if signal_score >= 0.85:
        # High conviction: buy more time (shift longer)
        min_dte, max_dte = _shift_longer(min_dte, max_dte)
    elif signal_score < 0.70:
        # Low conviction: stay short (less capital at risk)
        min_dte, max_dte = _shift_shorter(min_dte, max_dte)

    # Ensure valid range
    min_dte = max(0, min_dte)
    max_dte = max(min_dte + 1, max_dte)

    log.debug(
        "DTE profile selected",
        strategy=strategy,
        score=f"{signal_score:.2f}",
        vix=f"{vix:.1f}",
        dte_range=f"{min_dte}-{max_dte}",
    )
    return (min_dte, max_dte)


def recommend_target_dte(
    strategy: str,
    signal_score: float,
    vix: float = 20.0,
) -> int:
    """Return a single target DTE (midpoint of the recommended range)."""
    min_dte, max_dte = select_dte_profile(strategy, signal_score, vix)
    # Weighted toward the lower end (time decay favors shorter)
    return min_dte + (max_dte - min_dte) // 3


# ── Profile shifting helpers ─────────────────────────────────────────────────

_ORDERED_PROFILES = [SCALP, SWING, POSITION, MULTI_MONTH, LEAPS]


def _find_profile_index(min_dte: int, max_dte: int) -> int:
    """Find closest matching profile index."""
    mid = (min_dte + max_dte) / 2
    best_idx = 0
    best_dist = float("inf")
    for i, (lo, hi) in enumerate(_ORDERED_PROFILES):
        profile_mid = (lo + hi) / 2
        dist = abs(mid - profile_mid)
        if dist < best_dist:
            best_dist = dist
            best_idx = i
    return best_idx


def _shift_shorter(min_dte: int, max_dte: int) -> Tuple[int, int]:
    """Shift one profile level shorter."""
    idx = _find_profile_index(min_dte, max_dte)
    new_idx = max(0, idx - 1)
    return _ORDERED_PROFILES[new_idx]


def _shift_longer(min_dte: int, max_dte: int, steps: int = 1) -> Tuple[int, int]:
    """Shift one or more profile levels longer."""
    idx = _find_profile_index(min_dte, max_dte)
    new_idx = min(len(_ORDERED_PROFILES) - 1, idx + steps)
    return _ORDERED_PROFILES[new_idx]
