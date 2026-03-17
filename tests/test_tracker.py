"""Comprehensive tests for PortfolioTracker — trades, signals, P&L, stats."""

from __future__ import annotations

from datetime import date

import pytest

from nexus.tracker import PortfolioTracker

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tracker():
    """Fresh in-memory tracker for each test."""
    return PortfolioTracker(":memory:")


def _open_trade(
    tracker: PortfolioTracker,
    ticker: str = "AAPL",
    side: str = "LONG",
    shares: float = 10,
    entry_price: float = 150.0,
    stop: float = 145.0,
    target: float = 165.0,
    broker: str = "mock",
    strategy: str = "test",
) -> str:
    return tracker.open_trade(
        broker=broker,
        ticker=ticker,
        side=side,
        shares=shares,
        entry_price=entry_price,
        stop_price=stop,
        target_price=target,
        strategy=strategy,
        signal_score=0.80,
        paper=True,
    )


# ── Trade Lifecycle ──────────────────────────────────────────────────────────


class TestTradeLifecycle:
    def test_open_trade_returns_id(self, tracker):
        trade_id = _open_trade(tracker)
        assert isinstance(trade_id, str)
        assert len(trade_id) > 0

    def test_open_trade_stored_in_db(self, tracker):
        trade_id = _open_trade(tracker, ticker="MSFT")
        open_trades = tracker.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0]["id"] == trade_id
        assert open_trades[0]["ticker"] == "MSFT"
        assert open_trades[0]["side"] == "LONG"
        assert open_trades[0]["shares"] == 10

    def test_close_trade_long_pnl(self, tracker):
        trade_id = _open_trade(tracker, side="LONG", entry_price=100.0)
        pnl = tracker.close_trade(trade_id, exit_price=110.0, exit_reason="target_hit")

        # LONG P&L = (exit - entry) * shares = (110 - 100) * 10 = 100
        assert pnl == pytest.approx(100.0)

    def test_close_trade_short_pnl(self, tracker):
        trade_id = _open_trade(tracker, side="SHORT", entry_price=100.0, stop=105.0, target=90.0)
        pnl = tracker.close_trade(trade_id, exit_price=90.0, exit_reason="target_hit")

        # SHORT P&L = (entry - exit) * shares = (100 - 90) * 10 = 100
        assert pnl == pytest.approx(100.0)

    def test_close_trade_short_loss(self, tracker):
        trade_id = _open_trade(tracker, side="SHORT", entry_price=100.0, stop=105.0, target=90.0)
        pnl = tracker.close_trade(trade_id, exit_price=105.0, exit_reason="stop_hit")

        # SHORT loss = (100 - 105) * 10 = -50
        assert pnl == pytest.approx(-50.0)

    def test_close_trade_updates_daily_pnl(self, tracker):
        trade_id = _open_trade(tracker, side="LONG", entry_price=100.0)
        tracker.close_trade(trade_id, exit_price=110.0)

        pnl, trades = tracker.get_today_pnl()
        assert pnl == pytest.approx(100.0)
        assert trades == 1

    def test_close_nonexistent_trade_returns_none(self, tracker):
        result = tracker.close_trade("nonexistent-id", exit_price=100.0)
        assert result is None


# ── Queries ──────────────────────────────────────────────────────────────────


class TestQueries:
    def test_get_open_trades_excludes_closed(self, tracker):
        t1 = _open_trade(tracker, ticker="AAPL")
        _open_trade(tracker, ticker="MSFT")
        tracker.close_trade(t1, exit_price=160.0)

        open_trades = tracker.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0]["ticker"] == "MSFT"

    def test_get_open_trades_by_broker(self, tracker):
        _open_trade(tracker, ticker="AAPL", broker="alpaca")
        _open_trade(tracker, ticker="MSFT", broker="ibkr")

        alpaca_trades = tracker.get_open_trades(broker="alpaca")
        assert len(alpaca_trades) == 1
        assert alpaca_trades[0]["ticker"] == "AAPL"

    def test_get_closed_trades_ordered_by_date(self, tracker):
        t1 = _open_trade(tracker, ticker="AAPL")
        t2 = _open_trade(tracker, ticker="MSFT")
        tracker.close_trade(t1, exit_price=160.0)
        tracker.close_trade(t2, exit_price=210.0)

        closed = tracker.get_closed_trades()
        assert len(closed) == 2
        # Ordered by closed_at DESC — t2 closed last, should be first
        assert closed[0]["ticker"] == "MSFT"
        assert closed[1]["ticker"] == "AAPL"

    def test_get_closed_trades_limit(self, tracker):
        for i in range(5):
            tid = _open_trade(tracker, ticker=f"T{i}")
            tracker.close_trade(tid, exit_price=160.0)

        closed = tracker.get_closed_trades(limit=3)
        assert len(closed) == 3


# ── Signal Logging ───────────────────────────────────────────────────────────


class TestSignalLogging:
    def test_log_signal_stores_record(self, tracker):
        tracker.log_signal(
            ticker="AAPL",
            strategy="momentum",
            score=0.85,
            direction="BUY",
            reasoning="RSI oversold",
        )

        signals = tracker.get_recent_signals(limit=10)
        assert len(signals) == 1
        assert signals[0]["ticker"] == "AAPL"
        assert signals[0]["strategy"] == "momentum"
        assert signals[0]["score"] == pytest.approx(0.85)
        assert signals[0]["direction"] == "BUY"

    def test_get_recent_signals_ordered(self, tracker):
        tracker.log_signal("AAPL", "momentum", 0.80, "BUY", "first")
        tracker.log_signal("MSFT", "mean_rev", 0.90, "SELL", "second")

        signals = tracker.get_recent_signals(limit=10)
        assert len(signals) == 2
        # Ordered by ts DESC — second logged is first returned
        assert signals[0]["ticker"] == "MSFT"
        assert signals[1]["ticker"] == "AAPL"

    def test_get_recent_signals_limit(self, tracker):
        for i in range(10):
            tracker.log_signal(f"T{i}", "test", 0.5, "BUY", f"reason {i}")

        signals = tracker.get_recent_signals(limit=3)
        assert len(signals) == 3


# ── Daily P&L ────────────────────────────────────────────────────────────────


class TestDailyPnL:
    def test_get_today_pnl_no_trades(self, tracker):
        pnl, trades = tracker.get_today_pnl()
        assert pnl == 0.0
        assert trades == 0

    def test_get_today_pnl_after_close(self, tracker):
        tid = _open_trade(tracker, entry_price=100.0)
        tracker.close_trade(tid, exit_price=110.0)

        pnl, trades = tracker.get_today_pnl()
        assert pnl == pytest.approx(100.0)
        assert trades == 1

    def test_pnl_accumulates_multiple_closes(self, tracker):
        t1 = _open_trade(tracker, ticker="AAPL", entry_price=100.0)
        t2 = _open_trade(tracker, ticker="MSFT", entry_price=200.0)

        tracker.close_trade(t1, exit_price=110.0)  # +100
        tracker.close_trade(t2, exit_price=190.0)  # -100

        pnl, trades = tracker.get_today_pnl()
        assert pnl == pytest.approx(0.0)
        assert trades == 2

    def test_pnl_history(self, tracker):
        tid = _open_trade(tracker, entry_price=100.0)
        tracker.close_trade(tid, exit_price=120.0)

        history = tracker.get_pnl_history(days=7)
        assert len(history) == 1
        assert history[0]["date"] == date.today().isoformat()


# ── Stats ────────────────────────────────────────────────────────────────────


class TestStats:
    def test_compute_stats_no_trades_returns_defaults(self, tracker):
        stats = tracker.compute_stats()
        assert stats["win_rate"] == 0.5
        assert stats["total_trades"] == 0
        assert stats["total_pnl"] == 0.0

    def test_compute_stats_with_wins_and_losses(self, tracker):
        # 2 wins, 1 loss
        t1 = _open_trade(tracker, ticker="W1", entry_price=100.0)
        tracker.close_trade(t1, exit_price=120.0)  # +200

        t2 = _open_trade(tracker, ticker="W2", entry_price=100.0)
        tracker.close_trade(t2, exit_price=115.0)  # +150

        t3 = _open_trade(tracker, ticker="L1", entry_price=100.0)
        tracker.close_trade(t3, exit_price=90.0)  # -100

        stats = tracker.compute_stats()
        assert stats["total_trades"] == 3
        assert stats["total_pnl"] == pytest.approx(250.0)

    def test_win_rate_calculation(self, tracker):
        # 3 wins, 1 loss
        for i in range(3):
            tid = _open_trade(tracker, ticker=f"W{i}", entry_price=100.0)
            tracker.close_trade(tid, exit_price=110.0)  # win
        tid = _open_trade(tracker, ticker="L0", entry_price=100.0)
        tracker.close_trade(tid, exit_price=90.0)  # loss

        stats = tracker.compute_stats()
        assert stats["win_rate"] == pytest.approx(0.75)

    def test_profit_factor_calculation(self, tracker):
        # Win: +200, Loss: -100 → profit_factor = 200/100 = 2.0
        t1 = _open_trade(tracker, ticker="W1", entry_price=100.0)
        tracker.close_trade(t1, exit_price=120.0)  # +200

        t2 = _open_trade(tracker, ticker="L1", entry_price=100.0)
        tracker.close_trade(t2, exit_price=90.0)  # -100

        stats = tracker.compute_stats()
        assert stats["profit_factor"] == pytest.approx(2.0)

    def test_avg_win_avg_loss(self, tracker):
        t1 = _open_trade(tracker, ticker="W1", entry_price=100.0, shares=10)
        tracker.close_trade(t1, exit_price=115.0)  # +150

        t2 = _open_trade(tracker, ticker="W2", entry_price=100.0, shares=10)
        tracker.close_trade(t2, exit_price=110.0)  # +100

        t3 = _open_trade(tracker, ticker="L1", entry_price=100.0, shares=10)
        tracker.close_trade(t3, exit_price=92.0)  # -80

        stats = tracker.compute_stats()
        assert stats["avg_win"] == pytest.approx(125.0)  # (150+100)/2
        assert stats["avg_loss"] == pytest.approx(80.0)


# ── Meta Store ───────────────────────────────────────────────────────────────


class TestMetaStore:
    def test_save_and_get_meta(self, tracker):
        tracker.save_meta("peak_equity", "250000.0")
        val = tracker.get_meta("peak_equity")
        assert val == "250000.0"

    def test_get_meta_missing_key(self, tracker):
        val = tracker.get_meta("nonexistent")
        assert val is None

    def test_meta_upsert(self, tracker):
        tracker.save_meta("key1", "val1")
        tracker.save_meta("key1", "val2")
        assert tracker.get_meta("key1") == "val2"


# ── Reconciliation ──────────────────────────────────────────────────────────


class TestReconciliation:
    def test_sync_position_creates_trade(self, tracker):
        trade_id = tracker.sync_position(
            broker="alpaca",
            ticker="NVDA",
            side="LONG",
            shares=50,
            entry_price=400.0,
            paper=True,
        )
        assert isinstance(trade_id, str)
        open_trades = tracker.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0]["ticker"] == "NVDA"
        assert open_trades[0]["strategy"] == "reconciled"
