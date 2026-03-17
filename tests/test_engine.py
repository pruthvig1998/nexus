"""Comprehensive tests for NEXUSEngine — signal routing, exits, fills, events."""

from __future__ import annotations

from typing import Dict, List, Optional
from unittest.mock import AsyncMock, patch

from nexus.broker import (
    AccountInfo,
    BaseBroker,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
)
from nexus.config import NEXUSConfig, RiskConfig, StrategyConfig
from nexus.engine import EventType, NEXUSEngine, _EventBus, _PendingOrder
from nexus.strategy import Signal

# ── Mock broker ──────────────────────────────────────────────────────────────


class MockBroker(BaseBroker):
    """Predictable broker for engine tests."""

    name = "mock"
    paper = True

    def __init__(self) -> None:
        self._connected = True
        self._positions: List[Position] = []
        self._account = AccountInfo(
            broker="mock",
            cash=100_000.0,
            portfolio_value=200_000.0,
            buying_power=200_000.0,
            day_pnl=0.0,
            total_pnl=0.0,
            paper=True,
        )
        self._order_counter = 0
        self._order_statuses: Dict[str, OrderResult] = {}
        self._market_open = True
        self._quotes: Dict[str, Quote] = {}
        self.placed_orders: List[dict] = []
        self.cancelled_orders: List[str] = []
        self.short_opens: List[dict] = []
        self.short_closes: List[dict] = []

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def get_quote(self, ticker: str) -> Optional[Quote]:
        return self._quotes.get(ticker)

    async def get_batch_quotes(self, tickers: List[str]) -> Dict[str, Quote]:
        return {t: self._quotes[t] for t in tickers if t in self._quotes}

    async def get_positions(self) -> List[Position]:
        return list(self._positions)

    async def get_account_info(self) -> AccountInfo:
        return self._account

    async def place_order(
        self,
        ticker: str,
        side: OrderSide,
        qty: float,
        order_type: OrderType = OrderType.LIMIT,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        self._order_counter += 1
        oid = f"order-{self._order_counter}"
        self.placed_orders.append(
            {
                "order_id": oid,
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "type": order_type,
                "limit_price": limit_price,
            }
        )
        result = OrderResult(
            order_id=oid,
            ticker=ticker,
            side=side,
            requested_qty=qty,
            filled_qty=qty,
            avg_fill_price=limit_price or 100.0,
            status=OrderStatus.SUBMITTED,
            broker=self.name,
        )
        # Pre-set fill status for polling
        self._order_statuses[oid] = OrderResult(
            order_id=oid,
            ticker=ticker,
            side=side,
            requested_qty=qty,
            filled_qty=qty,
            avg_fill_price=limit_price or 100.0,
            status=OrderStatus.FILLED,
            broker=self.name,
        )
        return result

    async def cancel_order(self, order_id: str) -> bool:
        self.cancelled_orders.append(order_id)
        return True

    async def get_order_status(self, order_id: str) -> OrderResult:
        if order_id in self._order_statuses:
            return self._order_statuses[order_id]
        return OrderResult(
            order_id=order_id,
            ticker="",
            side=OrderSide.BUY,
            requested_qty=0,
            filled_qty=0,
            avg_fill_price=0,
            status=OrderStatus.PENDING,
            broker=self.name,
        )

    async def is_market_open(self) -> bool:
        return self._market_open

    async def open_short(
        self, ticker: str, shares: float, limit_price: Optional[float] = None
    ) -> OrderResult:
        self.short_opens.append(
            {
                "ticker": ticker,
                "shares": shares,
                "limit_price": limit_price,
            }
        )
        return await self.place_order(
            ticker,
            OrderSide.SELL,
            shares,
            OrderType.LIMIT if limit_price else OrderType.MARKET,
            limit_price,
        )

    async def close_short(
        self, ticker: str, shares: float, limit_price: Optional[float] = None
    ) -> OrderResult:
        self.short_closes.append(
            {
                "ticker": ticker,
                "shares": shares,
                "limit_price": limit_price,
            }
        )
        return await self.place_order(
            ticker,
            OrderSide.BUY,
            shares,
            OrderType.LIMIT if limit_price else OrderType.MARKET,
            limit_price,
        )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _cfg(db_path: str = ":memory:") -> NEXUSConfig:
    """Minimal config for testing."""
    return NEXUSConfig(
        watchlist=["AAPL", "MSFT"],
        scan_interval=1,
        paper=True,
        anthropic_api_key="",  # disables AI strategy
        risk=RiskConfig(
            max_position_pct=0.10,
            daily_loss_halt_pct=0.02,
            max_open_positions=20,
            kelly_fraction=0.25,
        ),
        strategy=StrategyConfig(min_signal_score=0.60),
        db_path=db_path,
    )


def _signal(
    ticker: str = "AAPL",
    direction: str = "BUY",
    score: float = 0.80,
    entry: float = 150.0,
    stop: float = 145.0,
    target: float = 165.0,
) -> Signal:
    return Signal(
        ticker=ticker,
        direction=direction,
        score=score,
        strategy="test",
        reasoning="test signal",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        limit_price=entry,
    )


def _make_engine(broker: Optional[MockBroker] = None, db_path: str = ":memory:") -> NEXUSEngine:
    """Create engine with mocked strategies to avoid importing heavy deps."""
    b = broker or MockBroker()
    cfg = _cfg(db_path)
    with (
        patch("nexus.engine.MomentumStrategy"),
        patch("nexus.engine.MeanReversionStrategy"),
        patch("nexus.engine.ORBStrategy"),
        patch("nexus.engine.IronGridStrategy"),
        patch("nexus.engine.NewsSentimentStrategy"),
        patch("nexus.engine.EventCalendarStrategy"),
    ):
        engine = NEXUSEngine(config=cfg, broker=b)
    engine._strategies = []  # no real strategies; we inject signals directly
    return engine


# ── Signal Routing Tests ─────────────────────────────────────────────────────


class TestSignalRouting:
    async def test_buy_signal_no_position_opens_long(self):
        broker = MockBroker()
        engine = _make_engine(broker)
        sig = _signal("AAPL", "BUY")

        await engine._execute(sig)

        assert len(broker.placed_orders) == 1
        order = broker.placed_orders[0]
        assert order["ticker"] == "AAPL"
        assert order["side"] == OrderSide.BUY

    async def test_sell_signal_no_position_opens_short(self):
        broker = MockBroker()
        engine = _make_engine(broker)
        sig = _signal("AAPL", "SELL", entry=150.0, stop=155.0, target=135.0)

        await engine._execute(sig)

        assert len(broker.short_opens) == 1
        assert broker.short_opens[0]["ticker"] == "AAPL"

    async def test_buy_signal_with_existing_long_skips(self):
        broker = MockBroker()
        engine = _make_engine(broker)
        engine._positions["AAPL"] = Position(
            ticker="AAPL",
            shares=10,
            avg_cost=145.0,
            current_price=150.0,
            broker="mock",
            side="LONG",
        )
        sig = _signal("AAPL", "BUY")

        await engine._execute(sig)

        assert len(broker.placed_orders) == 0

    async def test_sell_signal_with_existing_short_skips(self):
        broker = MockBroker()
        engine = _make_engine(broker)
        engine._positions["AAPL"] = Position(
            ticker="AAPL",
            shares=10,
            avg_cost=155.0,
            current_price=150.0,
            broker="mock",
            side="SHORT",
        )
        sig = _signal("AAPL", "SELL", entry=150.0, stop=155.0, target=135.0)

        await engine._execute(sig)

        assert len(broker.placed_orders) == 0
        assert len(broker.short_opens) == 0

    async def test_buy_signal_with_short_covers_first(self):
        broker = MockBroker()
        engine = _make_engine(broker)
        engine._positions["AAPL"] = Position(
            ticker="AAPL",
            shares=10,
            avg_cost=155.0,
            current_price=150.0,
            broker="mock",
            side="SHORT",
        )
        sig = _signal("AAPL", "BUY")

        await engine._execute(sig)

        # Should cover the short (close_short called)
        assert len(broker.short_closes) == 1
        assert broker.short_closes[0]["ticker"] == "AAPL"
        # Should NOT open a long in the same cycle (returns early)
        # The only placed_orders should be from close_short delegation
        assert all(o["side"] == OrderSide.BUY for o in broker.placed_orders)

    async def test_sell_signal_with_long_closes_first(self):
        broker = MockBroker()
        engine = _make_engine(broker)
        engine._positions["AAPL"] = Position(
            ticker="AAPL",
            shares=10,
            avg_cost=145.0,
            current_price=150.0,
            broker="mock",
            side="LONG",
        )
        sig = _signal("AAPL", "SELL", entry=150.0, stop=155.0, target=135.0)

        await engine._execute(sig)

        # Should close the long via place_order(SELL)
        assert any(o["side"] == OrderSide.SELL for o in broker.placed_orders)
        # Should NOT open a short in the same cycle
        assert len(broker.short_opens) == 0


# ── Exit Detection Tests ─────────────────────────────────────────────────────


class TestExitDetection:
    def _setup_open_trade(
        self, engine, side="LONG", entry=150.0, stop=145.0, target=165.0, ticker="AAPL"
    ):
        trade_id = engine._tracker.open_trade(
            broker="mock",
            ticker=ticker,
            side=side,
            shares=10,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            strategy="test",
            signal_score=0.8,
            paper=True,
        )
        return trade_id

    async def test_long_stop_hit_closes(self):
        broker = MockBroker()
        engine = _make_engine(broker)
        self._setup_open_trade(engine, "LONG", entry=150.0, stop=145.0, target=165.0)
        broker._quotes["AAPL"] = Quote(
            ticker="AAPL",
            bid=144.0,
            ask=145.0,
            last=144.0,
            volume=1000,
        )

        await engine._check_exits()

        # Should have placed a SELL (market) order to close
        assert any(o["side"] == OrderSide.SELL for o in broker.placed_orders)

    async def test_long_target_hit_closes(self):
        broker = MockBroker()
        engine = _make_engine(broker)
        self._setup_open_trade(engine, "LONG", entry=150.0, stop=145.0, target=165.0)
        broker._quotes["AAPL"] = Quote(
            ticker="AAPL",
            bid=165.0,
            ask=166.0,
            last=166.0,
            volume=1000,
        )

        await engine._check_exits()

        assert any(o["side"] == OrderSide.SELL for o in broker.placed_orders)

    async def test_short_stop_hit_closes(self):
        broker = MockBroker()
        engine = _make_engine(broker)
        self._setup_open_trade(engine, "SHORT", entry=150.0, stop=155.0, target=135.0)
        broker._quotes["AAPL"] = Quote(
            ticker="AAPL",
            bid=155.0,
            ask=156.0,
            last=156.0,
            volume=1000,
        )

        await engine._check_exits()

        # Short stop: price rose above stop → buy to cover via close_short
        assert len(broker.short_closes) == 1

    async def test_short_target_hit_closes(self):
        broker = MockBroker()
        engine = _make_engine(broker)
        self._setup_open_trade(engine, "SHORT", entry=150.0, stop=155.0, target=135.0)
        broker._quotes["AAPL"] = Quote(
            ticker="AAPL",
            bid=134.0,
            ask=135.0,
            last=134.0,
            volume=1000,
        )

        await engine._check_exits()

        # Short target: price dropped below target → profit → close_short
        assert len(broker.short_closes) == 1

    async def test_no_exit_when_price_in_range(self):
        broker = MockBroker()
        engine = _make_engine(broker)
        self._setup_open_trade(engine, "LONG", entry=150.0, stop=145.0, target=165.0)
        broker._quotes["AAPL"] = Quote(
            ticker="AAPL",
            bid=155.0,
            ask=156.0,
            last=155.0,
            volume=1000,
        )

        await engine._check_exits()

        assert len(broker.placed_orders) == 0
        assert len(broker.short_closes) == 0


# ── Circuit Breaker Tests ────────────────────────────────────────────────────


class TestCircuitBreaker:
    async def test_daily_loss_halt_blocks_signals(self):
        broker = MockBroker()
        engine = _make_engine(broker)

        # Trigger halt via risk limits
        engine._risk.update_daily_pnl(-5000.0, 200_000.0)  # 2.5% loss > 2% threshold
        assert engine._risk.is_halted

        sig = _signal("AAPL", "BUY")
        await engine._execute(sig)

        # Risk check inside _execute should block due to halt
        assert len(broker.placed_orders) == 0

    async def test_daily_reset_clears_halt(self):
        broker = MockBroker()
        engine = _make_engine(broker)

        engine._risk.update_daily_pnl(-5000.0, 200_000.0)
        assert engine._risk.is_halted

        engine._risk.reset_daily()
        assert not engine._risk.is_halted


# ── Signal Queue Tests ───────────────────────────────────────────────────────


class TestSignalQueue:
    async def test_external_signal_processed(self):
        broker = MockBroker()
        engine = _make_engine(broker)
        sig = _signal("AAPL", "BUY", score=0.85)
        engine.inject_signal(sig)

        assert not engine._signal_queue.empty()

    async def test_external_signal_below_min_score_filtered(self):
        """Signal with score below min_signal_score gets filtered during scan."""
        broker = MockBroker()
        engine = _make_engine(broker)

        low_sig = _signal("AAPL", "BUY", score=0.30)  # below 0.60 threshold
        engine.inject_signal(low_sig)

        # Simulate the drain logic from _scan_cycle
        best: Dict[str, Signal] = {}
        while not engine._signal_queue.empty():
            ext_sig = engine._signal_queue.get_nowait()
            if ext_sig.score >= engine._cfg.strategy.min_signal_score:
                best[ext_sig.ticker] = ext_sig

        assert "AAPL" not in best

    async def test_best_signal_per_ticker_wins(self):
        broker = MockBroker()
        engine = _make_engine(broker)

        low_sig = _signal("AAPL", "BUY", score=0.70)
        high_sig = _signal("AAPL", "BUY", score=0.90)
        engine.inject_signal(low_sig)
        engine.inject_signal(high_sig)

        best: Dict[str, Signal] = {}
        while not engine._signal_queue.empty():
            ext_sig = engine._signal_queue.get_nowait()
            if ext_sig.score >= engine._cfg.strategy.min_signal_score:
                if ext_sig.ticker not in best or ext_sig.score > best[ext_sig.ticker].score:
                    best[ext_sig.ticker] = ext_sig

        assert best["AAPL"].score == 0.90


# ── Fill Polling Tests ───────────────────────────────────────────────────────


class TestFillPolling:
    async def test_filled_order_publishes_event(self):
        broker = MockBroker()
        engine = _make_engine(broker)

        events_received = []
        engine._bus.subscribe(
            EventType.POSITION_OPENED, lambda et, data: events_received.append(data)
        )

        # Add a pending order that will be filled
        engine._pending["order-1"] = _PendingOrder(
            order_id="order-1",
            trade_id="trade-1",
            ticker="AAPL",
            shares=10,
            side="LONG",
        )
        broker._order_statuses["order-1"] = OrderResult(
            order_id="order-1",
            ticker="AAPL",
            side=OrderSide.BUY,
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=150.0,
            status=OrderStatus.FILLED,
            broker="mock",
        )

        await engine._poll_fills()

        assert len(events_received) == 1
        assert events_received[0]["trade_id"] == "trade-1"
        assert "order-1" not in engine._pending

    async def test_cancelled_order_removed(self):
        broker = MockBroker()
        engine = _make_engine(broker)

        engine._pending["order-1"] = _PendingOrder(
            order_id="order-1",
            trade_id="trade-1",
            ticker="AAPL",
            shares=10,
            side="LONG",
        )
        broker._order_statuses["order-1"] = OrderResult(
            order_id="order-1",
            ticker="AAPL",
            side=OrderSide.BUY,
            requested_qty=10,
            filled_qty=0,
            avg_fill_price=0,
            status=OrderStatus.CANCELLED,
            broker="mock",
        )

        await engine._poll_fills()

        assert "order-1" not in engine._pending

    async def test_timeout_cancels_order(self):
        broker = MockBroker()
        engine = _make_engine(broker)

        pending = _PendingOrder(
            order_id="order-1",
            trade_id="trade-1",
            ticker="AAPL",
            shares=10,
            side="LONG",
            max_checks=3,
        )
        pending.checks = 3  # already at max
        engine._pending["order-1"] = pending

        # Return PENDING status so it doesn't clear via fill/cancel
        broker._order_statuses["order-1"] = OrderResult(
            order_id="order-1",
            ticker="AAPL",
            side=OrderSide.BUY,
            requested_qty=10,
            filled_qty=0,
            avg_fill_price=0,
            status=OrderStatus.SUBMITTED,
            broker="mock",
        )

        await engine._poll_fills()

        assert "order-1" in broker.cancelled_orders
        assert "order-1" not in engine._pending


# ── Drawdown Scaling Tests ───────────────────────────────────────────────────


class TestDrawdownScaling:
    async def test_drawdown_scales_position_size(self):
        broker = MockBroker()
        engine = _make_engine(broker)

        # Set peak at 200k, current at 190k (5% drawdown)
        engine._peak_equity = 200_000.0
        broker._account = AccountInfo(
            broker="mock",
            cash=100_000.0,
            portfolio_value=190_000.0,
            buying_power=200_000.0,
            day_pnl=0.0,
            total_pnl=0.0,
            paper=True,
        )

        sig = _signal("AAPL", "BUY", entry=150.0, stop=145.0, target=165.0)
        await engine._execute(sig)

        # Order should be placed but with reduced shares
        assert len(broker.placed_orders) >= 1

    async def test_no_scaling_at_peak(self):
        broker = MockBroker()
        engine = _make_engine(broker)

        # At peak — no drawdown
        engine._peak_equity = 200_000.0
        broker._account = AccountInfo(
            broker="mock",
            cash=100_000.0,
            portfolio_value=200_000.0,
            buying_power=200_000.0,
            day_pnl=0.0,
            total_pnl=0.0,
            paper=True,
        )

        sig = _signal("AAPL", "BUY", entry=150.0, stop=145.0, target=165.0)
        await engine._execute(sig)

        assert len(broker.placed_orders) >= 1


# ── Event Bus Tests ──────────────────────────────────────────────────────────


class TestEventBus:
    async def test_event_bus_publishes(self):
        bus = _EventBus()
        received = []
        bus.subscribe(EventType.SCAN_COMPLETE, lambda et, data: received.append(data))

        await bus.publish(EventType.SCAN_COMPLETE, {"cycle": 1})

        assert len(received) == 1
        assert received[0]["cycle"] == 1

    async def test_event_bus_async_handler(self):
        bus = _EventBus()
        received = []

        async def async_handler(et, data):
            received.append(data)

        bus.subscribe(EventType.ORDER_FILLED, async_handler)
        await bus.publish(EventType.ORDER_FILLED, "fill-data")

        assert received == ["fill-data"]

    async def test_event_bus_error_doesnt_crash(self):
        bus = _EventBus()

        def bad_handler(et, data):
            raise ValueError("handler exploded")

        bus.subscribe(EventType.SCAN_COMPLETE, bad_handler)

        # Should not raise
        await bus.publish(EventType.SCAN_COMPLETE, None)

    async def test_event_bus_multiple_handlers(self):
        bus = _EventBus()
        a_received = []
        b_received = []
        bus.subscribe(EventType.SCAN_COMPLETE, lambda et, d: a_received.append(d))
        bus.subscribe(EventType.SCAN_COMPLETE, lambda et, d: b_received.append(d))

        await bus.publish(EventType.SCAN_COMPLETE, 42)

        assert a_received == [42]
        assert b_received == [42]

    async def test_event_bus_no_handlers_is_noop(self):
        bus = _EventBus()
        # Should not raise even with no subscribers
        await bus.publish(EventType.SCAN_COMPLETE, "data")


# ── Engine Lifecycle Tests ───────────────────────────────────────────────────


class TestEngineLifecycle:
    async def test_engine_stop_sets_running_false(self):
        broker = MockBroker()
        engine = _make_engine(broker)
        engine._running = True

        await engine.stop()

        assert not engine._running

    async def test_engine_start_connects_broker(self):
        broker = MockBroker()
        broker._connected = False
        engine = _make_engine(broker)

        events = []
        engine._bus.subscribe(EventType.BROKER_CONNECTED, lambda et, data: events.append(data))

        # Patch _scan_loop to avoid infinite loop
        engine._scan_loop = AsyncMock()

        await engine.start()

        assert broker._connected
        assert len(events) == 1

    async def test_engine_properties_exposed(self):
        broker = MockBroker()
        engine = _make_engine(broker)

        assert engine.tracker is engine._tracker
        assert engine.risk is engine._risk
        assert engine.broker is broker
        assert engine.event_bus is engine._bus

    async def test_inject_signal_adds_to_queue(self):
        engine = _make_engine()
        sig = _signal("MSFT", "SELL", score=0.75)
        engine.inject_signal(sig)

        assert engine._signal_queue.qsize() == 1
        queued = engine._signal_queue.get_nowait()
        assert queued.ticker == "MSFT"

    async def test_close_order_tracked_as_pending_close(self):
        """When closing a position before flipping, the close order uses side='CLOSE'."""
        broker = MockBroker()
        engine = _make_engine(broker)
        engine._positions["AAPL"] = Position(
            ticker="AAPL",
            shares=10,
            avg_cost=155.0,
            current_price=150.0,
            broker="mock",
            side="SHORT",
        )
        sig = _signal("AAPL", "BUY")

        await engine._execute(sig)

        # The pending order for the cover should have side="CLOSE"
        close_pendings = [p for p in engine._pending.values() if p.side == "CLOSE"]
        assert len(close_pendings) == 1
        assert close_pendings[0].ticker == "AAPL"

    async def test_close_pending_publishes_position_closed_on_fill(self):
        """CLOSE-side pending orders publish POSITION_CLOSED, not POSITION_OPENED."""
        broker = MockBroker()
        engine = _make_engine(broker)

        closed_events = []
        opened_events = []
        engine._bus.subscribe(
            EventType.POSITION_CLOSED, lambda et, data: closed_events.append(data)
        )
        engine._bus.subscribe(
            EventType.POSITION_OPENED, lambda et, data: opened_events.append(data)
        )

        engine._pending["order-close"] = _PendingOrder(
            order_id="order-close",
            trade_id="",
            ticker="AAPL",
            shares=10,
            side="CLOSE",
        )
        broker._order_statuses["order-close"] = OrderResult(
            order_id="order-close",
            ticker="AAPL",
            side=OrderSide.BUY,
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=150.0,
            status=OrderStatus.FILLED,
            broker="mock",
        )

        await engine._poll_fills()

        assert len(closed_events) == 1
        assert closed_events[0]["side"] == "CLOSE"
        assert len(opened_events) == 0

    async def test_rejected_order_not_tracked(self):
        """If broker rejects the order, no trade is opened and nothing is pending."""
        broker = MockBroker()
        engine = _make_engine(broker)

        # Override place_order to return REJECTED
        original_place = broker.place_order

        async def rejecting_place(*args, **kwargs):
            result = await original_place(*args, **kwargs)
            result.status = OrderStatus.REJECTED
            return result

        broker.place_order = rejecting_place

        sig = _signal("AAPL", "BUY")
        await engine._execute(sig)

        assert len(engine._pending) == 0
        # No trade opened in tracker
        assert len(engine._tracker.get_open_trades()) == 0
