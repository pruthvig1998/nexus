"""Market memory — SQLite-based tracking of swarm debates, agent outcomes, and narratives.

Stores swarm debate results, links them to trade outcomes, and tracks
evolving market narratives. Enables adaptive agent confidence based on
historical accuracy.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from nexus.logger import get_logger

log = get_logger("memory")

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS swarm_debates (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    signal_direction TEXT,
    signal_score REAL,
    signal_strategy TEXT,
    consensus_direction TEXT,
    consensus_score REAL,
    vetoed INTEGER DEFAULT 0,
    debate_summary TEXT,
    votes_json TEXT,
    trade_id TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_memory (
    id TEXT PRIMARY KEY,
    debate_id TEXT,
    agent_name TEXT NOT NULL,
    ticker TEXT NOT NULL,
    direction TEXT,
    conviction REAL,
    reasoning TEXT,
    risk_flags TEXT,
    outcome TEXT DEFAULT 'PENDING',
    outcome_pnl REAL DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS market_narratives (
    id TEXT PRIMARY KEY,
    narrative TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    supporting_signals INTEGER DEFAULT 1,
    first_seen TEXT,
    last_seen TEXT,
    active INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_debates_ticker ON swarm_debates(ticker);
CREATE INDEX IF NOT EXISTS idx_debates_trade ON swarm_debates(trade_id);
CREATE INDEX IF NOT EXISTS idx_agent_mem_agent ON agent_memory(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_mem_ticker ON agent_memory(ticker);
CREATE INDEX IF NOT EXISTS idx_narratives_active ON market_narratives(active);
"""


class MarketMemory:
    """SQLite-backed market memory for swarm intelligence."""

    def __init__(self, db_path: str = "nexus.db") -> None:
        self._db_path = db_path
        if db_path == ":memory:":
            self._persistent_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._persistent_conn.execute("PRAGMA journal_mode=WAL")
            self._persistent_conn.row_factory = sqlite3.Row
            self._persistent_conn.executescript(MEMORY_SCHEMA)
            self._persistent_conn.commit()
        else:
            self._persistent_conn = None
            self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(MEMORY_SCHEMA)

    @contextmanager
    def _conn(self):
        if self._persistent_conn is not None:
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

    # ── Record debate ────────────────────────────────────────────────────────

    def record_debate(self, debate_result: Any) -> str:
        """Store a SwarmDebateResult. Returns the debate_id."""
        from nexus.swarm import SwarmDebateResult

        r: SwarmDebateResult = debate_result
        debate_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()

        votes_json = json.dumps(
            [
                {
                    "agent": v.agent_name,
                    "direction": v.direction,
                    "conviction": v.conviction,
                    "reasoning": v.reasoning,
                    "risk_flags": v.risk_flags,
                    "veto": v.veto,
                }
                for v in r.votes
            ],
            ensure_ascii=False,
        )

        with self._conn() as conn:
            conn.execute(
                """INSERT INTO swarm_debates
                   (id, ticker, signal_direction, signal_score, signal_strategy,
                    consensus_direction, consensus_score, vetoed, debate_summary,
                    votes_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    debate_id,
                    r.original_signal.ticker,
                    r.original_signal.direction,
                    r.original_signal.score,
                    r.original_signal.strategy,
                    r.consensus_direction,
                    r.consensus_score,
                    1 if r.vetoed else 0,
                    r.debate_summary,
                    votes_json,
                    now,
                ),
            )
            # Record individual agent votes
            for v in r.votes:
                conn.execute(
                    """INSERT INTO agent_memory
                       (id, debate_id, agent_name, ticker, direction, conviction,
                        reasoning, risk_flags, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4())[:8],
                        debate_id,
                        v.agent_name,
                        r.original_signal.ticker,
                        v.direction,
                        v.conviction,
                        v.reasoning,
                        json.dumps(v.risk_flags),
                        now,
                    ),
                )

        log.debug("Debate recorded", debate_id=debate_id, ticker=r.original_signal.ticker)
        return debate_id

    def link_trade(self, debate_id: str, trade_id: str) -> None:
        """Link a debate to a trade after execution."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE swarm_debates SET trade_id = ? WHERE id = ?",
                (trade_id, debate_id),
            )

    def record_outcome(self, trade_id: str, pnl: float) -> None:
        """Record trade outcome for all agents in the linked debate."""
        outcome = "WIN" if pnl > 0 else "LOSS"
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM swarm_debates WHERE trade_id = ?", (trade_id,)
            ).fetchone()
            if row:
                debate_id = row["id"]
                conn.execute(
                    "UPDATE agent_memory SET outcome = ?, outcome_pnl = ? WHERE debate_id = ?",
                    (outcome, pnl, debate_id),
                )

    # ── Queries ──────────────────────────────────────────────────────────────

    def get_recent_debates(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent swarm debates for dashboard."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, ticker, signal_direction, signal_score, signal_strategy,
                          consensus_direction, consensus_score, vetoed, debate_summary,
                          votes_json, trade_id, created_at
                   FROM swarm_debates ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "ticker": r["ticker"],
                    "signal_direction": r["signal_direction"],
                    "signal_score": r["signal_score"],
                    "signal_strategy": r["signal_strategy"],
                    "consensus_direction": r["consensus_direction"],
                    "consensus_score": r["consensus_score"],
                    "vetoed": bool(r["vetoed"]),
                    "debate_summary": r["debate_summary"],
                    "votes": json.loads(r["votes_json"]) if r["votes_json"] else [],
                    "trade_id": r["trade_id"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def get_agent_track_record(
        self, agent_name: str, ticker: str | None = None
    ) -> Dict[str, Any]:
        """Get win/loss record for a specific agent."""
        with self._conn() as conn:
            if ticker:
                rows = conn.execute(
                    """SELECT outcome, outcome_pnl FROM agent_memory
                       WHERE agent_name = ? AND ticker = ? AND outcome != 'PENDING'""",
                    (agent_name, ticker),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT outcome, outcome_pnl FROM agent_memory
                       WHERE agent_name = ? AND outcome != 'PENDING'""",
                    (agent_name,),
                ).fetchall()

        wins = sum(1 for r in rows if r["outcome"] == "WIN")
        losses = sum(1 for r in rows if r["outcome"] == "LOSS")
        total = wins + losses
        total_pnl = sum(r["outcome_pnl"] for r in rows)

        return {
            "agent": agent_name,
            "ticker": ticker,
            "wins": wins,
            "losses": losses,
            "total": total,
            "win_rate": round(wins / total, 4) if total > 0 else 0.0,
            "total_pnl": round(total_pnl, 2),
        }

    # ── Narratives ───────────────────────────────────────────────────────────

    def update_narrative(self, narrative: str, confidence: float = 0.5) -> str:
        """Create or update a market narrative."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            # Check if similar narrative exists
            existing = conn.execute(
                "SELECT id, supporting_signals FROM market_narratives WHERE narrative = ? AND active = 1",
                (narrative,),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE market_narratives
                       SET confidence = ?, supporting_signals = ?, last_seen = ?
                       WHERE id = ?""",
                    (confidence, existing["supporting_signals"] + 1, now, existing["id"]),
                )
                return existing["id"]
            else:
                nid = str(uuid.uuid4())[:8]
                conn.execute(
                    """INSERT INTO market_narratives
                       (id, narrative, confidence, supporting_signals, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?)""",
                    (nid, narrative, confidence, 1, now, now),
                )
                return nid

    def get_active_narratives(self) -> List[Dict[str, Any]]:
        """Get all active market narratives."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, narrative, confidence, supporting_signals, first_seen, last_seen
                   FROM market_narratives WHERE active = 1
                   ORDER BY last_seen DESC""",
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "narrative": r["narrative"],
                    "confidence": r["confidence"],
                    "supporting_signals": r["supporting_signals"],
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                }
                for r in rows
            ]

    def deactivate_narrative(self, narrative_id: str) -> None:
        """Mark a narrative as no longer active."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE market_narratives SET active = 0 WHERE id = ?",
                (narrative_id,),
            )
