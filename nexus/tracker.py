"""SQLite portfolio tracker — trades, signals, daily P&L.

v3 changes:
  - trades.side stores "LONG" | "SHORT" (was "BUY" | "SELL")
  - close_trade() correctly computes P&L for both directions:
      LONG  P&L = (exit - entry) × shares
      SHORT P&L = (entry - exit) × shares
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

from nexus.logger import get_logger

log = get_logger("tracker")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    broker TEXT, ticker TEXT, side TEXT,
    shares REAL, entry_price REAL, exit_price REAL,
    stop_price REAL, target_price REAL,
    strategy TEXT, signal_score REAL,
    pnl REAL, exit_reason TEXT,
    opened_at TEXT, closed_at TEXT,
    paper INTEGER,
    instrument_type TEXT DEFAULT 'EQUITY',
    option_strike REAL DEFAULT 0,
    option_expiration TEXT DEFAULT '',
    option_code TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS daily_pnl (
    date TEXT PRIMARY KEY, pnl REAL, trades INTEGER
);
CREATE TABLE IF NOT EXISTS signals (
    id TEXT, ticker TEXT, strategy TEXT,
    score REAL, direction TEXT, reasoning TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
"""


class PortfolioTracker:
    def __init__(self, db_path: str = "nexus.db") -> None:
        self._db_path = db_path
        # For :memory: databases, keep a persistent connection so the schema
        # survives across calls (each sqlite3.connect(":memory:") creates a
        # new, empty database).
        if db_path == ":memory:":
            self._persistent_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._persistent_conn.execute("PRAGMA journal_mode=WAL")
            self._persistent_conn.row_factory = sqlite3.Row
            self._persistent_conn.executescript(SCHEMA)
            # Migrate: add options columns if missing
            try:
                self._persistent_conn.execute("SELECT instrument_type FROM trades LIMIT 1")
            except sqlite3.OperationalError:
                self._persistent_conn.execute("ALTER TABLE trades ADD COLUMN instrument_type TEXT DEFAULT 'EQUITY'")
                self._persistent_conn.execute("ALTER TABLE trades ADD COLUMN option_strike REAL DEFAULT 0")
                self._persistent_conn.execute("ALTER TABLE trades ADD COLUMN option_expiration TEXT DEFAULT ''")
                self._persistent_conn.execute("ALTER TABLE trades ADD COLUMN option_code TEXT DEFAULT ''")
            self._persistent_conn.commit()
        else:
            self._persistent_conn = None
            self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Migrate: add options columns if missing
            try:
                conn.execute("SELECT instrument_type FROM trades LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE trades ADD COLUMN instrument_type TEXT DEFAULT 'EQUITY'")
                conn.execute("ALTER TABLE trades ADD COLUMN option_strike REAL DEFAULT 0")
                conn.execute("ALTER TABLE trades ADD COLUMN option_expiration TEXT DEFAULT ''")
                conn.execute("ALTER TABLE trades ADD COLUMN option_code TEXT DEFAULT ''")
                conn.commit()

    @contextmanager
    def _conn(self):
        if self._persistent_conn is not None:
            # Reuse persistent connection for :memory: databases
            try:
                yield self._persistent_conn
                self._persistent_conn.commit()
            except Exception:
                self._persistent_conn.rollback()
                raise
        else:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ── Trades ───────────────────────────────────────────────────────────────

    def open_trade(
        self,
        broker: str,
        ticker: str,
        side: str,
        shares: float,
        entry_price: float,
        stop_price: float,
        target_price: float,
        strategy: str,
        signal_score: float,
        paper: bool = True,
        instrument_type: str = "EQUITY",
        option_strike: float = 0.0,
        option_expiration: str = "",
        option_code: str = "",
    ) -> str:
        """Open a new trade. side should be "LONG" or "SHORT"."""
        trade_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO trades
                   (id,broker,ticker,side,shares,entry_price,stop_price,
                    target_price,strategy,signal_score,opened_at,paper,
                    instrument_type,option_strike,option_expiration,option_code)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade_id,
                    broker,
                    ticker,
                    side,
                    shares,
                    entry_price,
                    stop_price,
                    target_price,
                    strategy,
                    signal_score,
                    datetime.now(timezone.utc).isoformat(),
                    int(paper),
                    instrument_type,
                    option_strike,
                    option_expiration,
                    option_code,
                ),
            )
        log.info(
            "Trade opened",
            id=trade_id[:8],
            ticker=ticker,
            side=side,
            shares=shares,
            price=f"${entry_price:.2f}",
        )
        return trade_id

    def close_trade(
        self, trade_id: str, exit_price: float, exit_reason: str = "manual"
    ) -> Optional[float]:
        """Close a trade. P&L is direction-aware."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
            if not row:
                return None
            trade = dict(row)
            side = trade.get("side", "LONG")
            inst_type = trade.get("instrument_type", "EQUITY")
            multiplier = 100 if inst_type in ("CALL", "PUT") else 1
            if side == "SHORT":
                pnl = (trade["entry_price"] - exit_price) * trade["shares"] * multiplier
            else:
                pnl = (exit_price - trade["entry_price"]) * trade["shares"] * multiplier
            conn.execute(
                "UPDATE trades SET exit_price=?,pnl=?,exit_reason=?,closed_at=? WHERE id=?",
                (exit_price, pnl, exit_reason, datetime.now(timezone.utc).isoformat(), trade_id),
            )
            today = date.today().isoformat()
            conn.execute(
                """INSERT INTO daily_pnl (date,pnl,trades) VALUES (?,?,1)
                   ON CONFLICT(date) DO UPDATE SET pnl=pnl+excluded.pnl,
                   trades=trades+1""",
                (today, pnl),
            )
        log.info("Trade closed", id=trade_id[:8], side=side, pnl=f"${pnl:+.2f}", reason=exit_reason)
        return pnl

    def get_open_trades(self, broker: Optional[str] = None) -> List[dict]:
        with self._conn() as conn:
            q = (
                "SELECT * FROM trades WHERE closed_at IS NULL AND broker=?"
                if broker
                else "SELECT * FROM trades WHERE closed_at IS NULL"
            )
            rows = conn.execute(q, (broker,) if broker else ()).fetchall()
        return [dict(r) for r in rows]

    def get_closed_trades(self, limit: int = 100) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Daily P&L ─────────────────────────────────────────────────────────

    def get_today_pnl(self) -> Tuple[float, int]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT pnl,trades FROM daily_pnl WHERE date=?", (date.today().isoformat(),)
            ).fetchone()
        return (float(row["pnl"]), int(row["trades"])) if row else (0.0, 0)

    def get_pnl_history(self, days: int = 30) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT ?", (days,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Signals ───────────────────────────────────────────────────────────

    def log_signal(
        self, ticker: str, strategy: str, score: float, direction: str, reasoning: str
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO signals (id,ticker,strategy,score,direction,reasoning,ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    ticker,
                    strategy,
                    score,
                    direction,
                    reasoning,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_recent_signals(self, limit: int = 50) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Reconciliation ────────────────────────────────────────────────────

    def sync_position(
        self,
        broker: str,
        ticker: str,
        side: str,
        shares: float,
        entry_price: float,
        paper: bool = True,
    ) -> str:
        """Sync a broker position into the tracker (reconciliation)."""
        return self.open_trade(
            broker=broker,
            ticker=ticker,
            side=side,
            shares=shares,
            entry_price=entry_price,
            stop_price=0.0,
            target_price=0.0,
            strategy="reconciled",
            signal_score=0.0,
            paper=paper,
        )

    # ── Meta key-value store ──────────────────────────────────────────────

    def save_meta(self, key: str, value: str) -> None:
        """Save a key-value pair to the meta table."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> Optional[str]:
        """Get a value from the meta table."""
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # ── Stats ─────────────────────────────────────────────────────────────

    def compute_stats(self) -> dict:
        defaults = {
            "win_rate": 0.5,
            "profit_factor": 1.0,
            "avg_win": 1.5,
            "avg_loss": 1.0,
            "total_trades": 0,
            "total_pnl": 0.0,
        }
        trades = self.get_closed_trades(limit=500)
        if not trades:
            return defaults
        wins = [t["pnl"] for t in trades if (t["pnl"] or 0) > 0]
        losses = [abs(t["pnl"]) for t in trades if (t["pnl"] or 0) < 0]
        return {
            "win_rate": round(len(wins) / max(len(trades), 1), 3),
            "profit_factor": round(sum(wins) / max(sum(losses), 0.01), 2),
            "avg_win": round(sum(wins) / max(len(wins), 1), 2),
            "avg_loss": round(sum(losses) / max(len(losses), 1), 2),
            "total_trades": len(trades),
            "total_pnl": round(sum(t.get("pnl") or 0 for t in trades), 2),
        }
