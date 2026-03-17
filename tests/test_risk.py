"""Unit tests for position sizing and risk limit checks."""

from nexus.broker import Position
from nexus.config import RiskConfig
from nexus.risk import RiskLimits, kelly_fraction, size_position


class TestKellyFraction:
    def test_positive_edge(self):
        k = kelly_fraction(win_rate=0.6, win_loss_ratio=1.5)
        assert k > 0

    def test_zero_edge(self):
        k = kelly_fraction(win_rate=0.4, win_loss_ratio=1.0)
        assert k == 0.0

    def test_capped_at_20pct(self):
        k = kelly_fraction(win_rate=0.9, win_loss_ratio=10.0, fraction=1.0)
        assert k <= 0.20

    def test_invalid_ratio(self):
        k = kelly_fraction(win_rate=0.6, win_loss_ratio=0.0)
        assert k == 0.0


class TestSizePosition:
    def test_basic_sizing(self):
        shares = size_position(
            portfolio_value=100_000,
            cash=50_000,
            entry_price=100.0,
            stop_price=95.0,
            signal_score=0.75,
        )
        assert shares >= 0

    def test_zero_risk_returns_zero(self):
        shares = size_position(
            portfolio_value=100_000,
            cash=50_000,
            entry_price=100.0,
            stop_price=100.0,  # no risk
            signal_score=0.75,
        )
        assert shares == 0

    def test_insufficient_cash(self):
        shares = size_position(
            portfolio_value=100_000,
            cash=100,  # almost no cash
            entry_price=100.0,
            stop_price=95.0,
            signal_score=0.75,
        )
        assert shares == 0 or shares == 1  # limited by cash

    def test_max_position_cap(self):
        shares = size_position(
            portfolio_value=100_000,
            cash=100_000,
            entry_price=100.0,
            stop_price=95.0,
            signal_score=1.0,
            max_position_pct=0.05,
        )
        max_value = 100_000 * 0.05
        assert shares * 100.0 <= max_value * 1.01  # allow small float error


class TestRiskLimits:
    def _make_positions(self, n_long=0, n_short=0, price=100.0, shares=10.0):
        positions = []
        for _ in range(n_long):
            positions.append(
                Position(
                    ticker="AAPL",
                    shares=shares,
                    avg_cost=price,
                    current_price=price,
                    broker="alpaca",
                    side="LONG",
                )
            )
        for _ in range(n_short):
            positions.append(
                Position(
                    ticker="TSLA",
                    shares=shares,
                    avg_cost=price,
                    current_price=price,
                    broker="alpaca",
                    side="SHORT",
                )
            )
        return positions

    def test_basic_approval(self):
        cfg = RiskConfig()
        rl = RiskLimits(cfg)
        result = rl.check(
            signal_score=0.75,
            portfolio_value=100_000,
            cash=50_000,
            open_positions=[],
            proposed_shares=10,
            entry_price=100.0,
            signal_direction="BUY",
        )
        assert result.approved

    def test_halted_blocks_all(self):
        cfg = RiskConfig()
        rl = RiskLimits(cfg)
        rl.update_daily_pnl(-5_000, portfolio_value=100_000)  # -5% > 3% halt
        assert rl.is_halted
        result = rl.check(
            signal_score=0.9,
            portfolio_value=100_000,
            cash=50_000,
            open_positions=[],
            proposed_shares=10,
            entry_price=100.0,
        )
        assert not result.approved

    def test_max_positions_blocks(self):
        cfg = RiskConfig(max_open_positions=3)
        rl = RiskLimits(cfg)
        positions = self._make_positions(n_long=3)
        result = rl.check(
            signal_score=0.8,
            portfolio_value=100_000,
            cash=50_000,
            open_positions=positions,
            proposed_shares=5,
            entry_price=100.0,
        )
        assert not result.approved
        assert "Max positions" in result.reason

    def test_short_exposure_cap(self):
        """SELL signals blocked when short book exceeds 50%."""
        cfg = RiskConfig(max_short_exposure_pct=0.50)
        rl = RiskLimits(cfg)
        # 55k of shorts on 100k portfolio = 55% → over limit
        positions = self._make_positions(n_short=5, shares=110.0, price=100.0)
        result = rl.check(
            signal_score=0.8,
            portfolio_value=100_000,
            cash=50_000,
            open_positions=positions,
            proposed_shares=10,
            entry_price=100.0,
            signal_direction="SELL",
        )
        assert not result.approved
        assert "Short exposure" in result.reason

    def test_long_exposure_cap(self):
        """BUY signals blocked when long book exceeds 90%."""
        cfg = RiskConfig(max_portfolio_exposure=0.90)
        rl = RiskLimits(cfg)
        # 10 positions × 100 shares × $100 = $100k long exposure on $100k portfolio = 100%
        positions = self._make_positions(n_long=10, shares=100.0, price=100.0)
        # Use 30 shares (3% of portfolio) so per-position cap (5%) doesn't fire first
        result = rl.check(
            signal_score=0.8,
            portfolio_value=100_000,
            cash=50_000,
            open_positions=positions,
            proposed_shares=30,
            entry_price=100.0,
            signal_direction="BUY",
        )
        assert not result.approved
        assert "Long exposure" in result.reason

    def test_reset_daily_clears_halt(self):
        cfg = RiskConfig()
        rl = RiskLimits(cfg)
        rl.update_daily_pnl(-10_000, portfolio_value=100_000)
        assert rl.is_halted
        rl.reset_daily()
        assert not rl.is_halted

    def test_size_cap_adjusts_shares(self):
        cfg = RiskConfig(max_position_pct=0.05)
        rl = RiskLimits(cfg)
        result = rl.check(
            signal_score=0.8,
            portfolio_value=100_000,
            cash=100_000,
            open_positions=[],
            proposed_shares=1000,
            entry_price=100.0,
        )
        assert result.approved
        assert result.adjusted_shares is not None
        assert result.adjusted_shares * 100.0 <= 100_000 * 0.05 + 1
