"""NEXUS v3 unified configuration — Alpaca-first, everything in one place.

v3 changes:
  - Removed var_confidence (dead code)
  - Added max_short_exposure_pct to RiskConfig (short book capped at 50%)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass
class AlpacaConfig:
    api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    paper: bool = field(default_factory=lambda: os.getenv("ALPACA_PAPER", "true").lower() != "false")


@dataclass
class RiskConfig:
    max_position_pct: float = 0.05
    max_portfolio_exposure: float = 0.90
    max_short_exposure_pct: float = 0.50    # short book capped at 50% of portfolio
    daily_loss_halt_pct: float = 0.03
    max_open_positions: int = 20
    kelly_fraction: float = 0.25
    atr_stop_multiplier: float = 1.5


@dataclass
class StrategyConfig:
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    rsi_mean_rev_oversold: float = 25.0
    sma_fast: int = 20
    sma_slow: int = 50
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bb_period: int = 20
    bb_std: float = 2.0
    atr_period: int = 14
    min_signal_score: float = 0.65
    ai_signal_weight: float = 0.40
    volume_filter_multiplier: float = 1.2
    trend_sma_period: int = 50
    rr_ratio: float = 3.0


@dataclass
class NEXUSConfig:
    active_broker: str = "alpaca"
    watchlist: List[str] = field(default_factory=lambda: [
        "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
        "META", "TSLA", "AMD", "CRM", "NFLX",
    ])
    scan_interval: int = 60
    paper: bool = True
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    ai_model: str = "claude-opus-4-6"
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    db_path: str = "nexus.db"
    log_level: str = "INFO"


_config: Optional[NEXUSConfig] = None


def get_config() -> NEXUSConfig:
    global _config
    if _config is None:
        _config = NEXUSConfig()
    return _config


def set_config(cfg: NEXUSConfig) -> None:
    global _config
    _config = cfg
