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
class DiscordConfig:
    bot_token: str = field(default_factory=lambda: os.getenv("DISCORD_BOT_TOKEN", ""))
    channel_ids: List[int] = field(
        default_factory=lambda: [
            int(x) for x in os.getenv("DISCORD_CHANNEL_IDS", "").split(",") if x.strip().isdigit()
        ]
    )
    min_message_score: float = 0.55
    use_llm_parsing: bool = False
    history_limit: int = 50


@dataclass
class TwitterConfig:
    accounts: List[str] = field(
        default_factory=lambda: [
            x.strip() for x in os.getenv("TWITTER_ACCOUNTS", "").split(",") if x.strip()
        ]
    )
    poll_interval: int = field(
        default_factory=lambda: int(os.getenv("TWITTER_POLL_INTERVAL", "20"))
    )
    nitter_instances: List[str] = field(
        default_factory=lambda: [
            x.strip()
            for x in os.getenv(
                "NITTER_INSTANCES",
                "nitter.poast.org,nitter.privacydev.net,nitter.cz,"
                "nitter.net,nitter.1d4.us,nitter.kavin.rocks,"
                "nitter.unixfox.eu,nitter.domain.glass",
            ).split(",")
            if x.strip()
        ]
    )
    min_score: float = 0.55
    use_llm_parsing: bool = False


@dataclass
class AlpacaConfig:
    api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    paper: bool = field(
        default_factory=lambda: os.getenv("ALPACA_PAPER", "true").lower() != "false"
    )


@dataclass
class MoomooConfig:
    host: str = field(default_factory=lambda: os.getenv("MOOMOO_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("MOOMOO_PORT", "11111")))
    trade_env: str = field(default_factory=lambda: os.getenv("MOOMOO_TRADE_ENV", "SIMULATE"))


@dataclass
class RiskConfig:
    max_position_pct: float = 0.05
    max_portfolio_exposure: float = 0.90
    max_short_exposure_pct: float = 0.50  # short book capped at 50% of portfolio
    daily_loss_halt_pct: float = 0.02
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

    # IronGrid rules
    vix_max_entry: float = 24.5  # don't buy options when VIX above this
    first_30min_wait: bool = True  # wait 30 min after market open
    profit_trim_25: float = 0.25  # trim 25% at +25%
    profit_trim_50: float = 0.50  # trim 50% at +50%
    profit_recover_100: float = 1.00  # recover capital at +100%
    max_swing_positions: int = 3
    max_leap_positions: int = 5
    stop_loss_pct: float = 0.25  # 25% stop loss on options
    trailing_stop_pct: float = 0.12  # 12% trail once up 20%+

    # Event Calendar strategy
    event_news_cache_ttl: int = 1800  # 30 min cache for news headlines
    event_max_claude_calls: int = 5  # max Claude calls per scan cycle


@dataclass
class TelegramConfig:
    bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    enabled: bool = field(
        default_factory=lambda: os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
    )


@dataclass
class OptionsConfig:
    enabled: bool = field(
        default_factory=lambda: os.getenv("NEXUS_OPTIONS_ENABLED", "false").lower() == "true"
    )
    min_dte: int = 0  # minimum days to expiration (0 = allow 0DTE)
    max_dte: int = 730  # maximum days to expiration (supports LEAPS)
    target_dte: int = 0  # preferred DTE (0 = auto-select via DTE engine)
    auto_dte: bool = True  # use DTE engine for intelligent DTE selection
    strike_offset: int = 1  # 0=ATM, 1=1 strike OTM, 2=2 strikes OTM
    max_premium: float = 0.0  # max premium per contract (0 = no limit)
    max_premium_pct: float = 0.02  # max 2% of portfolio per option trade
    min_open_interest: int = 100
    min_volume: int = 10
    min_signal_score: float = 0.70  # higher threshold for options (leveraged)
    profit_target_pct: float = 0.50  # take profit at 50% gain (fallback if grid disabled)
    stop_loss_pct: float = 0.50  # stop at 50% loss (fallback if grid disabled)
    min_dte_exit: int = 7  # close if < 7 DTE remaining
    use_irongrid_exits: bool = True  # use IronGrid profit ladder for exits


@dataclass
class ScannerConfig:
    enabled: bool = field(
        default_factory=lambda: os.getenv("NEXUS_SCANNER_ENABLED", "false").lower() == "true"
    )
    max_tickers: int = 20  # max extra tickers to add per scan
    scan_interval: int = 300  # seconds between universe scans (5 min)


@dataclass
class NEXUSConfig:
    active_broker: str = "alpaca"
    watchlist: List[str] = field(
        default_factory=lambda: [
            "AAPL",
            "MSFT",
            "NVDA",
            "GOOGL",
            "AMZN",
            "META",
            "TSLA",
            "AMD",
            "CRM",
            "NFLX",
        ]
    )
    scan_interval: int = 60
    paper: bool = True
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    ai_model: str = "claude-opus-4-6"
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)
    moomoo: MoomooConfig = field(default_factory=MoomooConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    twitter: TwitterConfig = field(default_factory=TwitterConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    options: OptionsConfig = field(default_factory=OptionsConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    db_path: str = "nexus.db"
    log_level: str = "INFO"

    def validate(self) -> None:
        """Validate configuration. Raises ValueError with all errors found."""
        errors: List[str] = []

        # Broker-specific API key checks
        if self.active_broker == "alpaca":
            if not self.alpaca.api_key:
                errors.append(
                    "ALPACA_API_KEY is required when using Alpaca broker. "
                    "Get one at https://alpaca.markets"
                )
            if not self.alpaca.secret_key:
                errors.append(
                    "ALPACA_SECRET_KEY is required when using Alpaca broker. "
                    "Get one at https://alpaca.markets"
                )

        # Scan interval
        if self.scan_interval <= 0:
            errors.append(f"scan_interval must be > 0 (got {self.scan_interval})")

        # Risk params
        if not (0 < self.risk.max_position_pct <= 1.0):
            errors.append(
                f"risk.max_position_pct must be in (0, 1.0] (got {self.risk.max_position_pct})"
            )
        if not (0 < self.risk.daily_loss_halt_pct <= 1.0):
            errors.append(
                f"risk.daily_loss_halt_pct must be in (0, 1.0] (got {self.risk.daily_loss_halt_pct})"
            )
        if self.risk.max_open_positions <= 0:
            errors.append(
                f"risk.max_open_positions must be > 0 (got {self.risk.max_open_positions})"
            )

        # Watchlist
        if not self.watchlist:
            errors.append("watchlist must not be empty")

        if errors:
            raise ValueError("Configuration validation failed:\n  - " + "\n  - ".join(errors))


_config: Optional[NEXUSConfig] = None


def get_config() -> NEXUSConfig:
    global _config
    if _config is None:
        _config = NEXUSConfig()
    return _config


def set_config(cfg: NEXUSConfig) -> None:
    global _config
    _config = cfg
