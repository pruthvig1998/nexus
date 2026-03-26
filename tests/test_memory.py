"""Tests for the market memory system."""

from __future__ import annotations

from nexus.memory import MarketMemory
from nexus.strategy import Signal
from nexus.swarm import AgentVote, SwarmDebateResult


def _make_debate_result(ticker: str = "NVDA", vetoed: bool = False) -> SwarmDebateResult:
    sig = Signal(
        ticker=ticker, direction="BUY", score=0.80, strategy="momentum",
        reasoning="RSI oversold", entry_price=120.0, stop_price=115.0, target_price=135.0,
    )
    votes = [
        AgentVote(agent_name="momentum", direction="BUY", conviction=0.85, reasoning="Strong trend"),
        AgentVote(agent_name="contrarian", direction="HOLD", conviction=0.50, reasoning="Neutral"),
        AgentVote(agent_name="macro", direction="BUY", conviction=0.70, reasoning="Sector ok"),
        AgentVote(agent_name="risk_manager", direction="BUY", conviction=0.75, reasoning="Acceptable",
                  veto=vetoed),
        AgentVote(agent_name="quant", direction="BUY", conviction=0.65, reasoning="Some edge"),
    ]
    return SwarmDebateResult(
        original_signal=sig, votes=votes,
        consensus_direction="BUY", consensus_score=0.72,
        debate_summary="Swarm 4/5 BUY (score=0.72)", vetoed=vetoed,
    )


class TestMarketMemory:
    def test_create_in_memory(self):
        mem = MarketMemory(":memory:")
        assert mem is not None

    def test_record_debate(self):
        mem = MarketMemory(":memory:")
        result = _make_debate_result()
        debate_id = mem.record_debate(result)
        assert isinstance(debate_id, str)
        assert len(debate_id) == 8

    def test_get_recent_debates(self):
        mem = MarketMemory(":memory:")
        mem.record_debate(_make_debate_result("NVDA"))
        mem.record_debate(_make_debate_result("AAPL"))

        debates = mem.get_recent_debates(10)
        assert len(debates) == 2
        # Most recent first
        assert debates[0]["ticker"] == "AAPL"
        assert debates[1]["ticker"] == "NVDA"

    def test_debate_has_votes(self):
        mem = MarketMemory(":memory:")
        mem.record_debate(_make_debate_result())
        debates = mem.get_recent_debates(1)
        assert len(debates[0]["votes"]) == 5
        assert debates[0]["votes"][0]["agent"] == "momentum"

    def test_link_trade_and_outcome(self):
        mem = MarketMemory(":memory:")
        debate_id = mem.record_debate(_make_debate_result())
        mem.link_trade(debate_id, "trade-123")
        mem.record_outcome("trade-123", 150.0)

        record = mem.get_agent_track_record("momentum")
        assert record["wins"] == 1
        assert record["losses"] == 0
        assert record["total_pnl"] == 150.0
        assert record["win_rate"] == 1.0

    def test_agent_track_record_loss(self):
        mem = MarketMemory(":memory:")
        debate_id = mem.record_debate(_make_debate_result())
        mem.link_trade(debate_id, "trade-456")
        mem.record_outcome("trade-456", -50.0)

        record = mem.get_agent_track_record("momentum")
        assert record["wins"] == 0
        assert record["losses"] == 1
        assert record["total_pnl"] == -50.0

    def test_agent_track_record_by_ticker(self):
        mem = MarketMemory(":memory:")
        d1 = mem.record_debate(_make_debate_result("NVDA"))
        mem.link_trade(d1, "t1")
        mem.record_outcome("t1", 100.0)

        d2 = mem.record_debate(_make_debate_result("AAPL"))
        mem.link_trade(d2, "t2")
        mem.record_outcome("t2", -30.0)

        nvda_record = mem.get_agent_track_record("momentum", ticker="NVDA")
        assert nvda_record["wins"] == 1
        assert nvda_record["losses"] == 0

        aapl_record = mem.get_agent_track_record("momentum", ticker="AAPL")
        assert aapl_record["wins"] == 0
        assert aapl_record["losses"] == 1

    def test_agent_track_record_empty(self):
        mem = MarketMemory(":memory:")
        record = mem.get_agent_track_record("momentum")
        assert record["total"] == 0
        assert record["win_rate"] == 0.0


class TestNarratives:
    def test_create_narrative(self):
        mem = MarketMemory(":memory:")
        nid = mem.update_narrative("Tech rotation into value", 0.70)
        assert isinstance(nid, str)

    def test_get_active_narratives(self):
        mem = MarketMemory(":memory:")
        mem.update_narrative("Tech sell-off", 0.80)
        mem.update_narrative("Flight to safety", 0.60)
        narratives = mem.get_active_narratives()
        assert len(narratives) == 2

    def test_update_existing_narrative(self):
        mem = MarketMemory(":memory:")
        mem.update_narrative("Rate cut expectations", 0.50)
        mem.update_narrative("Rate cut expectations", 0.80)
        narratives = mem.get_active_narratives()
        assert len(narratives) == 1
        assert narratives[0]["confidence"] == 0.80
        assert narratives[0]["supporting_signals"] == 2

    def test_deactivate_narrative(self):
        mem = MarketMemory(":memory:")
        nid = mem.update_narrative("Temporary panic", 0.50)
        mem.deactivate_narrative(nid)
        narratives = mem.get_active_narratives()
        assert len(narratives) == 0
