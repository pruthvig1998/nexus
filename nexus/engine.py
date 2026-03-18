"""NEXUS async engine — scan loop, execution, order tracking.

v3 changes:
  - _execute(): routes SELL signals to open_short() when no existing position
  - _execute(): covers short before going long on BUY signal
  - _check_exits(): correct stop/target logic for SHORT positions
  - _poll_fills(): records LONG/SHORT side in tracker
  - _scan_cycle(): auto-resets daily halt at midnight
  - _execute(): tracks close/flip orders in _pending with side="CLOSE"
  - _execute(): scales position size down during portfolio drawdown
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from nexus.broker import (
    AlpacaBroker,
    BaseBroker,
    OptionsContract,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from nexus.config import NEXUSConfig, get_config
from nexus.logger import get_logger
from nexus.risk import RiskLimits, size_position
from nexus.strategy import (
    AIFundamentalStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    ORBStrategy,
    Signal,
)
from nexus.strategy_events import EventCalendarStrategy
from nexus.strategy_irongrid import IronGridStrategy
from nexus.strategy_news import NewsSentimentStrategy
from nexus.tracker import PortfolioTracker

log = get_logger("engine")


# ── EventBus (internal) ───────────────────────────────────────────────────────


class EventType(Enum):
    SIGNAL_GENERATED = auto()
    ORDER_SUBMITTED = auto()
    ORDER_FILLED = auto()
    ORDER_PARTIAL = auto()
    ORDER_CANCELLED = auto()
    POSITION_OPENED = auto()
    POSITION_CLOSED = auto()
    SCAN_COMPLETE = auto()
    BROKER_CONNECTED = auto()
    DAILY_HALT = auto()


class _EventBus:
    def __init__(self) -> None:
        self._handlers: Dict[EventType, List[Callable]] = defaultdict(list)

    def subscribe(self, event_type: EventType, handler: Callable) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event_type: EventType, data: Any = None) -> None:
        for handler in self._handlers.get(event_type, []):
            try:
                result = handler(event_type, data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                log.error("Event handler error", event_type=event_type.name, error=str(e))


# ── Pending order tracking ────────────────────────────────────────────────────


class _PendingOrder:
    __slots__ = ("order_id", "trade_id", "ticker", "shares", "side", "checks", "max_checks")

    def __init__(
        self,
        order_id: str,
        trade_id: str,
        ticker: str,
        shares: float,
        side: str,
        max_checks: int = 20,
    ) -> None:
        self.order_id = order_id
        self.trade_id = trade_id
        self.ticker = ticker
        self.shares = shares
        self.side = side  # "LONG" | "SHORT"
        self.checks = 0
        self.max_checks = max_checks


# ── Main engine ───────────────────────────────────────────────────────────────


class NEXUSEngine:
    """Async scan-signal-execute loop with long/short support.

    Usage:
        engine = NEXUSEngine()
        await engine.start()          # connects broker, runs forever
        await engine.stop()           # graceful shutdown
    """

    def __init__(
        self,
        config: Optional[NEXUSConfig] = None,
        broker: Optional[BaseBroker] = None,
        flatten_on_exit: bool = False,
    ) -> None:
        self._cfg = config or get_config()
        self._broker = broker or AlpacaBroker(self._cfg.alpaca)
        self._tracker = PortfolioTracker(self._cfg.db_path)
        self._risk = RiskLimits(self._cfg.risk)
        self._bus = _EventBus()
        self._running = False
        self._scan_count = 0
        self._flatten_on_exit = flatten_on_exit

        self._strategies = [
            MomentumStrategy(),
            MeanReversionStrategy(),
            ORBStrategy(),
            IronGridStrategy(),
            NewsSentimentStrategy(),
            EventCalendarStrategy(),
        ]
        if self._cfg.anthropic_api_key:
            self._strategies.append(AIFundamentalStrategy())

        self._price_cache: Dict[str, pd.DataFrame] = {}
        self._cache_ts: Dict[str, datetime] = {}
        self._pending: Dict[str, _PendingOrder] = {}

        # Local position cache: ticker → Position (for routing decisions)
        self._positions: Dict[str, Position] = {}

        # Daily reset tracking
        self._last_reset_date: Optional[date] = None

        # Drawdown tracking for position size scaling — restore from DB
        saved_peak = self._tracker.get_meta("peak_equity")
        self._peak_equity: float = float(saved_peak) if saved_peak else 0.0

        # External signal queue (e.g. Discord feed)
        self._signal_queue: asyncio.Queue[Signal] = asyncio.Queue()

    # ── Public interface ──────────────────────────────────────────────────────

    @property
    def tracker(self) -> PortfolioTracker:
        return self._tracker

    @property
    def risk(self) -> RiskLimits:
        return self._risk

    @property
    def broker(self) -> BaseBroker:
        return self._broker

    @property
    def event_bus(self) -> _EventBus:
        return self._bus

    @property
    def price_cache(self) -> Dict[str, pd.DataFrame]:
        return self._price_cache

    async def start(self) -> None:
        log.info(
            "NEXUS starting",
            paper=self._cfg.paper,
            broker=self._cfg.active_broker,
            watchlist=len(self._cfg.watchlist),
        )

        connected = await self._broker.connect()
        if connected:
            await self._bus.publish(EventType.BROKER_CONNECTED, self._broker.name)
            await self._reconcile_positions()
        else:
            log.warning("Broker offline — running price-data-only mode")

        self._running = True
        await self._scan_loop()

    async def stop(self) -> None:
        self._running = False

        # Cancel all pending orders
        for order_id, pending in list(self._pending.items()):
            try:
                await self._broker.cancel_order(order_id)
                log.info("Cancelled pending order", ticker=pending.ticker, order_id=order_id[:8])
            except Exception as e:
                log.warning("Failed to cancel order", order_id=order_id[:8], error=str(e))
        self._pending.clear()

        # Log open positions with current P&L
        if self._broker.is_connected:
            try:
                positions = await self._broker.get_positions()
                for pos in positions:
                    log.info(
                        "Open position at shutdown",
                        ticker=pos.ticker,
                        side=pos.side,
                        shares=pos.shares,
                        unrealized_pnl=f"${pos.unrealized_pnl:+.2f}",
                    )

                # Flatten all positions if requested
                if self._flatten_on_exit and positions:
                    log.info("Flattening all positions on exit", count=len(positions))
                    for pos in positions:
                        try:
                            if pos.side == "SHORT":
                                await self._broker.close_short(
                                    pos.ticker,
                                    pos.shares,
                                    limit_price=pos.current_price * 1.001,
                                )
                            else:
                                await self._broker.place_order(
                                    ticker=pos.ticker,
                                    side=OrderSide.SELL,
                                    qty=pos.shares,
                                    order_type=OrderType.MARKET,
                                )
                            log.info("Flattened position", ticker=pos.ticker, side=pos.side)
                        except Exception as e:
                            log.error("Failed to flatten position", ticker=pos.ticker, error=str(e))
            except Exception as e:
                log.warning("Could not fetch positions at shutdown", error=str(e))

        # Persist peak equity to SQLite
        if self._peak_equity > 0:
            self._tracker.save_meta("peak_equity", str(self._peak_equity))

        await self._broker.disconnect()
        log.info("NEXUS stopped", scans=self._scan_count)

    def inject_signal(self, signal: Signal) -> None:
        """Inject an external signal directly into the scan queue."""
        self._signal_queue.put_nowait(signal)

    def get_signal_queue(self) -> asyncio.Queue:
        """Return the queue for external signal sources (e.g. DiscordFeed)."""
        return self._signal_queue

    @property
    def news_strategy(self) -> Optional[object]:
        """Return the NewsSentimentStrategy instance for headline injection."""
        for s in self._strategies:
            if hasattr(s, "name") and s.name == "news_sentiment":
                return s
        return None

    # ── Position reconciliation ─────────────────────────────────────────────

    async def _reconcile_positions(self) -> None:
        """Reconcile broker positions against tracker state on startup."""
        try:
            broker_positions = await self._broker.get_positions()
            tracker_trades = self._tracker.get_open_trades(broker=self._broker.name)

            broker_by_ticker = {p.ticker: p for p in broker_positions}
            tracker_by_ticker = {t["ticker"]: t for t in tracker_trades}

            # Positions in broker but not in tracker — sync them
            for ticker, pos in broker_by_ticker.items():
                if ticker not in tracker_by_ticker:
                    log.warning(
                        "Broker has position not in tracker — syncing",
                        ticker=ticker,
                        side=pos.side,
                        shares=pos.shares,
                        avg_cost=f"${pos.avg_cost:.2f}",
                    )
                    self._tracker.sync_position(
                        broker=self._broker.name,
                        ticker=ticker,
                        side=pos.side,
                        shares=pos.shares,
                        entry_price=pos.avg_cost,
                        paper=self._cfg.paper,
                    )

            # Positions in tracker but not in broker — warn
            for ticker, trade in tracker_by_ticker.items():
                if ticker not in broker_by_ticker:
                    log.warning(
                        "Tracker has position not found at broker",
                        ticker=ticker,
                        side=trade.get("side"),
                        shares=trade.get("shares"),
                        trade_id=trade["id"][:8],
                    )

            synced = len([t for t in broker_by_ticker if t not in tracker_by_ticker])
            missing = len([t for t in tracker_by_ticker if t not in broker_by_ticker])
            if synced or missing:
                log.info(
                    "Position reconciliation complete", synced=synced, missing_at_broker=missing
                )
            else:
                log.info("Position reconciliation complete — all in sync")

        except Exception as e:
            log.error("Position reconciliation failed", error=str(e))

    # ── Scan loop ─────────────────────────────────────────────────────────────

    async def _scan_loop(self) -> None:
        while self._running:
            try:
                await self._scan_cycle()
                self._scan_count += 1
                await self._bus.publish(EventType.SCAN_COMPLETE, self._scan_count)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Scan cycle error", error=str(e))
            await asyncio.sleep(self._cfg.scan_interval)

    async def _scan_cycle(self) -> None:
        await self._refresh_prices()

        if self._broker.is_connected:
            try:
                acct = await self._broker.get_account_info()

                # Auto-reset daily halt at midnight
                today = date.today()
                if self._last_reset_date != today:
                    self._risk.reset_daily()
                    self._last_reset_date = today
                    log.info("Daily risk counters reset", date=today.isoformat())

                self._risk.update_daily_pnl(acct.day_pnl, acct.portfolio_value)

                # Track peak equity for drawdown scaling
                if acct.portfolio_value > self._peak_equity:
                    self._peak_equity = acct.portfolio_value

                # Refresh local position cache
                positions_list = await self._broker.get_positions()
                self._positions = {p.ticker: p for p in positions_list}
            except Exception as e:
                log.debug("Account refresh failed", error=str(e))

        tasks = [
            strategy.analyze(ticker, self._price_cache.get(ticker))
            for ticker in self._cfg.watchlist
            for strategy in self._strategies
            if self._price_cache.get(ticker) is not None
        ]
        raw = await asyncio.gather(*tasks, return_exceptions=True)

        best: Dict[str, Signal] = {}
        for sig in raw:
            if isinstance(sig, Signal) and sig.direction != "HOLD":
                if sig.ticker not in best or sig.score > best[sig.ticker].score:
                    best[sig.ticker] = sig

        # Drain external signal queue (Discord feed, etc.)
        while not self._signal_queue.empty():
            try:
                ext_sig = self._signal_queue.get_nowait()
                if ext_sig.score >= self._cfg.strategy.min_signal_score:
                    if ext_sig.ticker not in best or ext_sig.score > best[ext_sig.ticker].score:
                        best[ext_sig.ticker] = ext_sig
            except asyncio.QueueEmpty:
                break

        if self._broker.is_connected and not self._risk.is_halted:
            market_open = await self._broker.is_market_open()
            if market_open:
                for sig in best.values():
                    if sig.score >= self._cfg.strategy.min_signal_score:
                        # Convert to options signal if options enabled
                        if self._cfg.options.enabled and sig.instrument_type == "EQUITY":
                            from nexus.strategy_options import convert_signal_to_option

                            try:
                                acct = await self._broker.get_account_info()
                                opt_sig = await convert_signal_to_option(
                                    sig, self._broker, acct.portfolio_value
                                )
                                if opt_sig:
                                    await self._execute_options(opt_sig)
                                    continue
                            except Exception as e:
                                log.debug("Options conversion failed", ticker=sig.ticker, error=str(e))
                        await self._execute(sig)

        if self._broker.is_connected:
            await self._check_exits()

        await self._poll_fills()

        log.debug(
            "Scan complete", cycle=self._scan_count, signals=len(best), pending=len(self._pending)
        )

    # ── Price cache ───────────────────────────────────────────────────────────

    async def _refresh_prices(self) -> None:
        now = datetime.now(timezone.utc)
        stale = [
            t
            for t in self._cfg.watchlist
            if t not in self._cache_ts or (now - self._cache_ts[t]).total_seconds() > 300
        ]
        # Evict tickers no longer on watchlist to prevent unbounded cache growth
        stale_keys = [t for t in self._price_cache if t not in self._cfg.watchlist]
        for t in stale_keys:
            del self._price_cache[t]
            self._cache_ts.pop(t, None)
        for ticker in stale:
            await self._fetch_price(ticker, now)

    async def _fetch_price(self, ticker: str, now: datetime) -> None:
        try:
            import yfinance as yf

            df = await asyncio.to_thread(
                lambda: yf.download(
                    ticker, period="1y", interval="1d", auto_adjust=True, progress=False
                )
            )
            if df is not None and len(df) > 60:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [c[0].lower() for c in df.columns]
                else:
                    df.columns = [c.lower() for c in df.columns]
                self._price_cache[ticker] = df
                self._cache_ts[ticker] = now
        except Exception as e:
            log.error("Price fetch failed", ticker=ticker, error=str(e))

    # ── Execution ─────────────────────────────────────────────────────────────

    async def _execute(self, signal: Signal) -> None:
        """Route signal to correct broker action based on existing positions."""
        try:
            acct = await self._broker.get_account_info()
            positions_list = await self._broker.get_positions()
            existing = self._positions.get(signal.ticker)

            stats = self._tracker.compute_stats()
            shares = size_position(
                portfolio_value=acct.portfolio_value,
                cash=acct.cash,
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                signal_score=signal.score,
                win_rate=stats["win_rate"],
                avg_win=stats["avg_win"],
                avg_loss=stats["avg_loss"],
                kelly_frac=self._cfg.risk.kelly_fraction,
                max_position_pct=self._cfg.risk.max_position_pct,
                signal_direction=signal.direction,
            )
            if shares < 1:
                return

            check = self._risk.check(
                signal_score=signal.score,
                portfolio_value=acct.portfolio_value,
                cash=acct.cash,
                open_positions=positions_list,
                proposed_shares=shares,
                entry_price=signal.entry_price,
                signal_direction=signal.direction,
            )
            if not check.approved:
                log.debug("Signal blocked", ticker=signal.ticker, reason=check.reason)
                return

            final_shares = check.adjusted_shares or shares
            signal.shares = float(final_shares)

            self._tracker.log_signal(
                ticker=signal.ticker,
                strategy=signal.strategy,
                score=signal.score,
                direction=signal.direction,
                reasoning=signal.reasoning,
            )

            # Drawdown-based position size scaling: reduce size linearly as
            # portfolio falls from peak. At 5% DD → 50% size, at 10%+ DD → 25%.
            if self._peak_equity > 0 and acct.portfolio_value < self._peak_equity:
                dd_pct = 1.0 - (acct.portfolio_value / self._peak_equity)
                scale = max(0.25, 1.0 - dd_pct * 10)  # 10% DD → scale=0.0 floored at 0.25
                final_shares = max(1, int(final_shares * scale))
                if dd_pct > 0.01:
                    log.debug("Drawdown size scaling", dd=f"{dd_pct:.1%}", scale=f"{scale:.2f}")

            if signal.direction == "BUY":
                if existing and existing.side == "SHORT":
                    # Cover the short before going long — track in pending so
                    # we know the fill happened before re-evaluating
                    log.info("Covering short before going long", ticker=signal.ticker)
                    close_result = await self._broker.close_short(
                        signal.ticker,
                        existing.shares,
                        signal.limit_price or signal.entry_price,
                    )
                    self._positions.pop(signal.ticker, None)
                    if close_result and close_result.order_id:
                        self._pending[close_result.order_id] = _PendingOrder(
                            order_id=close_result.order_id,
                            trade_id="",  # close — no new trade_id
                            ticker=signal.ticker,
                            shares=existing.shares,
                            side="CLOSE",
                        )
                    return  # Re-evaluate next cycle to open the long

                elif existing and existing.side == "LONG":
                    return  # Already long, skip

                # Open long
                result = await self._broker.place_order(
                    ticker=signal.ticker,
                    side=OrderSide.BUY,
                    qty=float(final_shares),
                    order_type=OrderType.LIMIT,
                    limit_price=signal.limit_price or signal.entry_price,
                )
                trade_side = "LONG"

            else:  # SELL signal
                if existing and existing.side == "LONG":
                    # Close long before going short — track the close order
                    log.info("Closing long before going short", ticker=signal.ticker)
                    close_result = await self._broker.place_order(
                        ticker=signal.ticker,
                        side=OrderSide.SELL,
                        qty=existing.shares,
                        order_type=OrderType.MARKET,
                    )
                    self._positions.pop(signal.ticker, None)
                    if close_result and close_result.order_id:
                        self._pending[close_result.order_id] = _PendingOrder(
                            order_id=close_result.order_id,
                            trade_id="",  # close — no new trade_id
                            ticker=signal.ticker,
                            shares=existing.shares,
                            side="CLOSE",
                        )
                    return  # Re-evaluate next cycle to open the short

                elif existing and existing.side == "SHORT":
                    return  # Already short, skip

                # Open short
                result = await self._broker.open_short(
                    ticker=signal.ticker,
                    shares=float(final_shares),
                    limit_price=signal.limit_price or signal.entry_price,
                )
                trade_side = "SHORT"

            if result.status == OrderStatus.REJECTED:
                log.warning("Order rejected", ticker=signal.ticker, msg=result.message)
                return

            trade_id = self._tracker.open_trade(
                broker=self._broker.name,
                ticker=signal.ticker,
                side=trade_side,
                shares=float(final_shares),
                entry_price=result.avg_fill_price or signal.entry_price,
                stop_price=signal.stop_price,
                target_price=signal.target_price,
                strategy=signal.strategy,
                signal_score=signal.score,
                paper=self._cfg.paper,
            )
            self._pending[result.order_id] = _PendingOrder(
                order_id=result.order_id,
                trade_id=trade_id,
                ticker=signal.ticker,
                shares=float(final_shares),
                side=trade_side,
            )
            await self._bus.publish(EventType.ORDER_SUBMITTED, result)
            log.info(
                "Order submitted",
                ticker=signal.ticker,
                side=trade_side,
                shares=final_shares,
                score=f"{signal.score:.2f}",
                strategy=signal.strategy,
            )

        except Exception as e:
            log.error("Execute failed", ticker=signal.ticker, error=str(e))

    # ── Options execution ──────────────────────────────────────────────────────

    async def _execute_options(self, signal: Signal) -> None:
        """Execute an options signal — buy calls or puts."""
        try:
            contract = OptionsContract(
                ticker=signal.ticker,
                strike=signal.option_strike,
                expiration=signal.option_expiration,
                right=signal.instrument_type,
                code=signal.option_code,
            )
            qty = signal.contracts
            if qty < 1:
                return

            self._tracker.log_signal(
                ticker=signal.ticker,
                strategy=signal.strategy,
                score=signal.score,
                direction=f"BUY_{signal.instrument_type}",
                reasoning=signal.reasoning,
            )

            result = await self._broker.place_options_order(
                contract=contract,
                side=OrderSide.BUY,
                qty=qty,
                order_type=OrderType.LIMIT,
                limit_price=signal.limit_price or signal.entry_price,
            )

            if result.status == OrderStatus.REJECTED:
                log.warning(
                    "Options order rejected",
                    ticker=signal.ticker,
                    right=signal.instrument_type,
                    msg=result.message,
                )
                return

            trade_id = self._tracker.open_trade(
                broker=self._broker.name,
                ticker=signal.ticker,
                side="LONG",
                shares=float(qty),
                entry_price=result.avg_fill_price or signal.entry_price,
                stop_price=signal.stop_price,
                target_price=signal.target_price,
                strategy=signal.strategy,
                signal_score=signal.score,
                paper=self._cfg.paper,
                instrument_type=signal.instrument_type,
                option_strike=signal.option_strike,
                option_expiration=signal.option_expiration,
                option_code=signal.option_code,
            )
            self._pending[result.order_id] = _PendingOrder(
                order_id=result.order_id,
                trade_id=trade_id,
                ticker=signal.ticker,
                shares=float(qty),
                side=signal.instrument_type,  # "CALL" or "PUT"
            )
            await self._bus.publish(EventType.ORDER_SUBMITTED, result)
            log.info(
                "Options order submitted",
                ticker=signal.ticker,
                type=signal.instrument_type,
                strike=signal.option_strike,
                exp=signal.option_expiration,
                contracts=qty,
                premium=f"${signal.entry_price:.2f}",
                strategy=signal.strategy,
            )
        except Exception as e:
            log.error("Options execute failed", ticker=signal.ticker, error=str(e))

    # ── Fill polling ──────────────────────────────────────────────────────────

    async def _poll_fills(self) -> None:
        for order_id in list(self._pending.keys()):
            pending = self._pending.get(order_id)
            if not pending:
                continue
            try:
                status = await self._broker.get_order_status(order_id)
                pending.checks += 1

                if status.status == OrderStatus.FILLED:
                    await self._bus.publish(EventType.ORDER_FILLED, status)
                    if pending.side != "CLOSE":
                        await self._bus.publish(
                            EventType.POSITION_OPENED,
                            {
                                "trade_id": pending.trade_id,
                                "ticker": pending.ticker,
                                "side": pending.side,
                            },
                        )
                    else:
                        await self._bus.publish(
                            EventType.POSITION_CLOSED,
                            {
                                "trade_id": pending.trade_id,
                                "ticker": pending.ticker,
                                "side": "CLOSE",
                                "pnl": None,
                                "reason": "flip",
                            },
                        )
                    del self._pending[order_id]

                elif status.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                    del self._pending[order_id]

                elif pending.checks >= pending.max_checks:
                    await self._broker.cancel_order(order_id)
                    del self._pending[order_id]
                    log.warning("Order timeout cancelled", ticker=pending.ticker)

            except Exception as e:
                log.error("Fill poll error", order_id=order_id[:8], error=str(e))

    # ── Stop/target exits ─────────────────────────────────────────────────────

    async def _check_exits(self) -> None:
        """Check open trades for stop/target hits. Logic is direction-aware."""
        open_trades = self._tracker.get_open_trades(broker=self._broker.name)
        if not open_trades:
            return

        # Separate options and equity trades
        equity_trades = [t for t in open_trades if t.get("instrument_type", "EQUITY") == "EQUITY"]
        option_trades = [t for t in open_trades if t.get("instrument_type", "EQUITY") in ("CALL", "PUT")]

        if option_trades:
            await self._check_options_exits(option_trades)

        tickers = list({t["ticker"] for t in equity_trades})
        if not tickers:
            return
        quotes = await self._broker.get_batch_quotes(tickers)

        for trade in equity_trades:
            quote = quotes.get(trade["ticker"])
            if not quote:
                continue
            price = quote.last
            stop = trade.get("stop_price") or 0
            target = trade.get("target_price") or 0
            side = trade.get("side", "LONG")

            if side == "LONG":
                hit_stop = stop > 0 and price <= stop
                hit_target = target > 0 and price >= target
                exit_order_side = OrderSide.SELL

                def exit_price_adj(p: float) -> float:
                    return p * 0.999  # slightly below for limit
            else:  # SHORT
                # Stop: price rose above stop (loss)
                hit_stop = stop > 0 and price >= stop
                # Target: price fell below target (profit)
                hit_target = target > 0 and price <= target
                exit_order_side = OrderSide.BUY  # buy to cover

                def exit_price_adj(p: float) -> float:
                    return p * 1.001  # slightly above for limit

            if hit_stop or hit_target:
                reason = "target_hit" if hit_target else "stop_hit"
                exit_price = exit_price_adj(price)

                if side == "SHORT":
                    await self._broker.close_short(
                        trade["ticker"],
                        trade["shares"],
                        limit_price=round(exit_price, 2),
                    )
                else:
                    await self._broker.place_order(
                        ticker=trade["ticker"],
                        side=exit_order_side,
                        qty=trade["shares"],
                        order_type=OrderType.MARKET,
                    )

                pnl = self._tracker.close_trade(trade["id"], price, reason)
                await self._bus.publish(
                    EventType.POSITION_CLOSED,
                    {
                        "trade_id": trade["id"],
                        "ticker": trade["ticker"],
                        "side": side,
                        "pnl": pnl,
                        "reason": reason,
                    },
                )
                log.info(
                    "Position closed",
                    ticker=trade["ticker"],
                    side=side,
                    pnl=f"${pnl:+.2f}" if pnl else "?",
                    reason=reason,
                )

    async def _check_options_exits(self, trades: List[dict]) -> None:
        """Check options trades for exit conditions (P&L targets, time decay)."""
        from datetime import datetime as _dt

        opts_cfg = self._cfg.options
        today = _dt.now().date()

        for trade in trades:
            opt_code = trade.get("option_code", "")
            if not opt_code:
                continue

            # Get current option price via quote
            try:
                from nexus.broker import OptionsContract

                contract = OptionsContract(
                    ticker=trade["ticker"],
                    strike=trade.get("option_strike", 0),
                    expiration=trade.get("option_expiration", ""),
                    right=trade.get("instrument_type", "CALL"),
                    code=opt_code,
                )
                # Use batch quotes with the option code for current price
                quotes = await self._broker.get_batch_quotes([opt_code])
                quote = quotes.get(opt_code)
                if not quote:
                    continue

                current_price = quote.last
                entry_price = trade["entry_price"]
                if entry_price <= 0:
                    continue

                pnl_pct = (current_price - entry_price) / entry_price

                # Check DTE
                reason = None
                exp_str = trade.get("option_expiration", "")
                if exp_str:
                    try:
                        exp_date = _dt.strptime(exp_str[:10], "%Y-%m-%d").date()
                        dte = (exp_date - today).days
                        if dte <= opts_cfg.min_dte_exit:
                            reason = f"dte_exit ({dte}d remaining)"
                    except ValueError:
                        pass

                # Check profit target
                if pnl_pct >= opts_cfg.profit_target_pct:
                    reason = f"profit_target ({pnl_pct:.0%})"

                # Check stop loss
                if pnl_pct <= -opts_cfg.stop_loss_pct:
                    reason = f"stop_loss ({pnl_pct:.0%})"

                if reason:
                    # Sell the option
                    await self._broker.place_options_order(
                        contract=contract,
                        side=OrderSide.SELL,
                        qty=int(trade["shares"]),
                        order_type=OrderType.LIMIT,
                        limit_price=round(current_price * 0.98, 2),
                    )
                    pnl = self._tracker.close_trade(trade["id"], current_price, reason)
                    await self._bus.publish(
                        EventType.POSITION_CLOSED,
                        {
                            "trade_id": trade["id"],
                            "ticker": trade["ticker"],
                            "side": trade.get("instrument_type", "CALL"),
                            "pnl": pnl,
                            "reason": reason,
                        },
                    )
                    log.info(
                        "Options position closed",
                        ticker=trade["ticker"],
                        type=trade.get("instrument_type"),
                        pnl=f"${pnl:+.2f}" if pnl else "?",
                        reason=reason,
                    )
            except Exception as e:
                log.error("Options exit check failed", ticker=trade["ticker"], error=str(e))
