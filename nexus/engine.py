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
from datetime import date, datetime
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from nexus.broker import AlpacaBroker, BaseBroker, OrderSide, OrderStatus, OrderType, Position, Quote
from nexus.config import NEXUSConfig, get_config
from nexus.logger import get_logger
from nexus.risk import RiskLimits, size_position
from nexus.strategy import (
    AIFundamentalStrategy, MeanReversionStrategy, MomentumStrategy,
    ORBStrategy, Signal,
)
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
                log.error("Event handler error", event=event_type.name, error=str(e))


# ── Pending order tracking ────────────────────────────────────────────────────

class _PendingOrder:
    __slots__ = ("order_id", "trade_id", "ticker", "shares", "side", "checks", "max_checks")

    def __init__(self, order_id: str, trade_id: str, ticker: str,
                 shares: float, side: str, max_checks: int = 20) -> None:
        self.order_id = order_id
        self.trade_id = trade_id
        self.ticker = ticker
        self.shares = shares
        self.side = side          # "LONG" | "SHORT"
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
    ) -> None:
        self._cfg = config or get_config()
        self._broker = broker or AlpacaBroker(self._cfg.alpaca)
        self._tracker = PortfolioTracker(self._cfg.db_path)
        self._risk = RiskLimits(self._cfg.risk)
        self._bus = _EventBus()
        self._running = False
        self._scan_count = 0

        self._strategies = [MomentumStrategy(), MeanReversionStrategy(), ORBStrategy()]
        if self._cfg.anthropic_api_key:
            self._strategies.append(AIFundamentalStrategy())

        self._price_cache: Dict[str, pd.DataFrame] = {}
        self._cache_ts: Dict[str, datetime] = {}
        self._pending: Dict[str, _PendingOrder] = {}

        # Local position cache: ticker → Position (for routing decisions)
        self._positions: Dict[str, Position] = {}

        # Daily reset tracking
        self._last_reset_date: Optional[date] = None

        # Drawdown tracking for position size scaling
        self._peak_equity: float = 0.0

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
        log.info("NEXUS starting", paper=self._cfg.paper,
                 broker=self._cfg.active_broker,
                 watchlist=len(self._cfg.watchlist))

        connected = await self._broker.connect()
        if connected:
            await self._bus.publish(EventType.BROKER_CONNECTED, self._broker.name)
        else:
            log.warning("Broker offline — running price-data-only mode")

        self._running = True
        await self._scan_loop()

    async def stop(self) -> None:
        self._running = False
        await self._broker.disconnect()
        log.info("NEXUS stopped", scans=self._scan_count)

    def inject_signal(self, signal: Signal) -> None:
        """Inject an external signal directly into the scan queue."""
        self._signal_queue.put_nowait(signal)

    def get_signal_queue(self) -> asyncio.Queue:
        """Return the queue for external signal sources (e.g. DiscordFeed)."""
        return self._signal_queue

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
            except Exception:
                pass

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
                        await self._execute(sig)

        if self._broker.is_connected:
            await self._check_exits()

        await self._poll_fills()

        log.debug("Scan complete", cycle=self._scan_count,
                  signals=len(best), pending=len(self._pending))

    # ── Price cache ───────────────────────────────────────────────────────────

    async def _refresh_prices(self) -> None:
        now = datetime.utcnow()
        stale = [
            t for t in self._cfg.watchlist
            if t not in self._cache_ts or
            (now - self._cache_ts[t]).seconds > 300
        ]
        for ticker in stale:
            await self._fetch_price(ticker, now)

    async def _fetch_price(self, ticker: str, now: datetime) -> None:
        try:
            import yfinance as yf
            df = await asyncio.to_thread(
                lambda: yf.download(ticker, period="1y", interval="1d",
                                    auto_adjust=True, progress=False)
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
                ticker=signal.ticker, strategy=signal.strategy,
                score=signal.score, direction=signal.direction,
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
                        signal.ticker, existing.shares,
                        signal.limit_price or signal.entry_price,
                    )
                    del self._positions[signal.ticker]
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
                    ticker=signal.ticker, side=OrderSide.BUY, qty=float(final_shares),
                    order_type=OrderType.LIMIT,
                    limit_price=signal.limit_price or signal.entry_price,
                )
                trade_side = "LONG"

            else:  # SELL signal
                if existing and existing.side == "LONG":
                    # Close long before going short — track the close order
                    log.info("Closing long before going short", ticker=signal.ticker)
                    close_result = await self._broker.place_order(
                        ticker=signal.ticker, side=OrderSide.SELL, qty=existing.shares,
                        order_type=OrderType.MARKET,
                    )
                    del self._positions[signal.ticker]
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
                    ticker=signal.ticker, shares=float(final_shares),
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
            log.info("Order submitted", ticker=signal.ticker,
                     side=trade_side, shares=final_shares,
                     score=f"{signal.score:.2f}", strategy=signal.strategy)

        except Exception as e:
            log.error("Execute failed", ticker=signal.ticker, error=str(e))

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
                        await self._bus.publish(EventType.POSITION_OPENED, {
                            "trade_id": pending.trade_id,
                            "ticker": pending.ticker,
                            "side": pending.side,
                        })
                    else:
                        await self._bus.publish(EventType.POSITION_CLOSED, {
                            "trade_id": pending.trade_id,
                            "ticker": pending.ticker,
                            "side": "CLOSE",
                            "pnl": None,
                            "reason": "flip",
                        })
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
        tickers = list({t["ticker"] for t in open_trades})
        quotes = await self._broker.get_batch_quotes(tickers)

        for trade in open_trades:
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
                exit_price_adj = lambda p: p * 0.999  # slightly below for limit
            else:  # SHORT
                # Stop: price rose above stop (loss)
                hit_stop = stop > 0 and price >= stop
                # Target: price fell below target (profit)
                hit_target = target > 0 and price <= target
                exit_order_side = OrderSide.BUY  # buy to cover
                exit_price_adj = lambda p: p * 1.001  # slightly above for limit

            if hit_stop or hit_target:
                reason = "target_hit" if hit_target else "stop_hit"
                exit_price = exit_price_adj(price)

                if side == "SHORT":
                    await self._broker.close_short(
                        trade["ticker"], trade["shares"],
                        limit_price=round(exit_price, 2),
                    )
                else:
                    await self._broker.place_order(
                        ticker=trade["ticker"], side=exit_order_side,
                        qty=trade["shares"], order_type=OrderType.MARKET,
                    )

                pnl = self._tracker.close_trade(trade["id"], price, reason)
                await self._bus.publish(EventType.POSITION_CLOSED, {
                    "trade_id": trade["id"],
                    "ticker": trade["ticker"],
                    "side": side,
                    "pnl": pnl,
                    "reason": reason,
                })
                log.info("Position closed", ticker=trade["ticker"], side=side,
                         pnl=f"${pnl:+.2f}" if pnl else "?", reason=reason)
