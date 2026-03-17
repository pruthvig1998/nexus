"""Unit tests for NEXUSConfig.validate()."""

from __future__ import annotations

import pytest

from nexus.config import AlpacaConfig, NEXUSConfig, RiskConfig


def _valid_config(**overrides) -> NEXUSConfig:
    """Create a valid config with optional overrides."""
    defaults = dict(
        active_broker="alpaca",
        watchlist=["AAPL", "MSFT"],
        scan_interval=60,
        alpaca=AlpacaConfig(api_key="pk_test", secret_key="sk_test"),
    )
    defaults.update(overrides)
    return NEXUSConfig(**defaults)


class TestConfigValidation:
    def test_valid_config_passes(self):
        cfg = _valid_config()
        cfg.validate()  # should not raise

    def test_missing_alpaca_api_key(self):
        cfg = _valid_config(alpaca=AlpacaConfig(api_key="", secret_key="sk_test"))
        with pytest.raises(ValueError, match="ALPACA_API_KEY"):
            cfg.validate()

    def test_missing_alpaca_secret_key(self):
        cfg = _valid_config(alpaca=AlpacaConfig(api_key="pk_test", secret_key=""))
        with pytest.raises(ValueError, match="ALPACA_SECRET_KEY"):
            cfg.validate()

    def test_missing_both_alpaca_keys(self):
        cfg = _valid_config(alpaca=AlpacaConfig(api_key="", secret_key=""))
        with pytest.raises(ValueError) as exc_info:
            cfg.validate()
        msg = str(exc_info.value)
        assert "ALPACA_API_KEY" in msg
        assert "ALPACA_SECRET_KEY" in msg

    def test_moomoo_no_api_keys_needed(self):
        cfg = _valid_config(
            active_broker="moomoo",
            alpaca=AlpacaConfig(api_key="", secret_key=""),
        )
        cfg.validate()  # should not raise

    def test_ibkr_no_api_keys_needed(self):
        cfg = _valid_config(
            active_broker="ibkr",
            alpaca=AlpacaConfig(api_key="", secret_key=""),
        )
        cfg.validate()  # should not raise

    def test_webull_no_api_keys_needed(self):
        cfg = _valid_config(
            active_broker="webull",
            alpaca=AlpacaConfig(api_key="", secret_key=""),
        )
        cfg.validate()  # should not raise

    def test_scan_interval_zero(self):
        cfg = _valid_config(scan_interval=0)
        with pytest.raises(ValueError, match="scan_interval"):
            cfg.validate()

    def test_scan_interval_negative(self):
        cfg = _valid_config(scan_interval=-10)
        with pytest.raises(ValueError, match="scan_interval"):
            cfg.validate()

    def test_empty_watchlist(self):
        cfg = _valid_config(watchlist=[])
        with pytest.raises(ValueError, match="watchlist"):
            cfg.validate()

    def test_max_position_pct_zero(self):
        cfg = _valid_config(risk=RiskConfig(max_position_pct=0.0))
        with pytest.raises(ValueError, match="max_position_pct"):
            cfg.validate()

    def test_max_position_pct_above_one(self):
        cfg = _valid_config(risk=RiskConfig(max_position_pct=1.5))
        with pytest.raises(ValueError, match="max_position_pct"):
            cfg.validate()

    def test_daily_loss_halt_pct_zero(self):
        cfg = _valid_config(risk=RiskConfig(daily_loss_halt_pct=0.0))
        with pytest.raises(ValueError, match="daily_loss_halt_pct"):
            cfg.validate()

    def test_daily_loss_halt_pct_negative(self):
        cfg = _valid_config(risk=RiskConfig(daily_loss_halt_pct=-0.05))
        with pytest.raises(ValueError, match="daily_loss_halt_pct"):
            cfg.validate()

    def test_max_open_positions_zero(self):
        cfg = _valid_config(risk=RiskConfig(max_open_positions=0))
        with pytest.raises(ValueError, match="max_open_positions"):
            cfg.validate()

    def test_max_open_positions_negative(self):
        cfg = _valid_config(risk=RiskConfig(max_open_positions=-1))
        with pytest.raises(ValueError, match="max_open_positions"):
            cfg.validate()

    def test_multiple_errors_collected(self):
        cfg = _valid_config(
            active_broker="alpaca",
            alpaca=AlpacaConfig(api_key="", secret_key=""),
            scan_interval=-1,
            watchlist=[],
            risk=RiskConfig(max_position_pct=0.0, max_open_positions=0),
        )
        with pytest.raises(ValueError) as exc_info:
            cfg.validate()
        msg = str(exc_info.value)
        # All errors should be present, not just the first
        assert "ALPACA_API_KEY" in msg
        assert "ALPACA_SECRET_KEY" in msg
        assert "scan_interval" in msg
        assert "watchlist" in msg
        assert "max_position_pct" in msg
        assert "max_open_positions" in msg

    def test_valid_edge_case_max_position_pct_one(self):
        cfg = _valid_config(risk=RiskConfig(max_position_pct=1.0))
        cfg.validate()  # should not raise

    def test_valid_edge_case_daily_loss_halt_pct_one(self):
        cfg = _valid_config(risk=RiskConfig(daily_loss_halt_pct=1.0))
        cfg.validate()  # should not raise

    def test_alpaca_keys_contain_helpful_link(self):
        cfg = _valid_config(alpaca=AlpacaConfig(api_key="", secret_key="sk"))
        with pytest.raises(ValueError, match="https://alpaca.markets"):
            cfg.validate()
