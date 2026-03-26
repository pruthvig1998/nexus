"""Tests for the multi-agent swarm debate engine."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from nexus.config import NEXUSConfig, SwarmConfig, set_config
from nexus.strategy import Signal
from nexus.swarm import (
    AgentVote,
    SwarmDebate,
    SwarmDebateResult,
    compute_consensus,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_signal(
    ticker: str = "NVDA",
    direction: str = "BUY",
    score: float = 0.80,
    strategy: str = "momentum",
) -> Signal:
    return Signal(
        ticker=ticker,
        direction=direction,
        score=score,
        strategy=strategy,
        reasoning="RSI oversold; MACD bullish cross",
        entry_price=120.0,
        stop_price=115.0,
        target_price=135.0,
        rsi_val=28.0,
        macd_hist=0.15,
        bb_pct_b=0.10,
        vol_ratio=1.5,
    )


def _make_votes_buy_majority() -> list[AgentVote]:
    """4/5 agents agree BUY."""
    return [
        AgentVote(agent_name="momentum", direction="BUY", conviction=0.85, reasoning="Strong trend"),
        AgentVote(agent_name="contrarian", direction="BUY", conviction=0.60, reasoning="Not overextended"),
        AgentVote(agent_name="macro", direction="BUY", conviction=0.70, reasoning="Sector in favor"),
        AgentVote(agent_name="risk_manager", direction="BUY", conviction=0.75, reasoning="Risk acceptable"),
        AgentVote(agent_name="quant", direction="HOLD", conviction=0.40, reasoning="Weak stats edge"),
    ]


def _make_votes_sell_majority() -> list[AgentVote]:
    """3/5 agents agree SELL."""
    return [
        AgentVote(agent_name="momentum", direction="SELL", conviction=0.80, reasoning="Downtrend"),
        AgentVote(agent_name="contrarian", direction="SELL", conviction=0.70, reasoning="Overextended"),
        AgentVote(agent_name="macro", direction="BUY", conviction=0.50, reasoning="Sector ok"),
        AgentVote(agent_name="risk_manager", direction="SELL", conviction=0.65, reasoning="Risk high"),
        AgentVote(agent_name="quant", direction="HOLD", conviction=0.30, reasoning="No edge"),
    ]


def _make_votes_veto() -> list[AgentVote]:
    """Risk manager vetoes despite 4/5 BUY."""
    return [
        AgentVote(agent_name="momentum", direction="BUY", conviction=0.90, reasoning="Great setup"),
        AgentVote(agent_name="contrarian", direction="BUY", conviction=0.70, reasoning="Looks good"),
        AgentVote(agent_name="macro", direction="BUY", conviction=0.80, reasoning="Bullish macro"),
        AgentVote(
            agent_name="risk_manager",
            direction="HOLD",
            conviction=0.90,
            reasoning="Earnings in 2 days + concentrated sector exposure",
            veto=True,
        ),
        AgentVote(agent_name="quant", direction="BUY", conviction=0.75, reasoning="Good stats"),
    ]


def _make_votes_tie() -> list[AgentVote]:
    """Split: 2 BUY, 2 SELL, 1 HOLD."""
    return [
        AgentVote(agent_name="momentum", direction="BUY", conviction=0.60, reasoning="Trend up"),
        AgentVote(agent_name="contrarian", direction="SELL", conviction=0.60, reasoning="Overextended"),
        AgentVote(agent_name="macro", direction="HOLD", conviction=0.50, reasoning="Neutral"),
        AgentVote(agent_name="risk_manager", direction="SELL", conviction=0.55, reasoning="Prefer caution"),
        AgentVote(agent_name="quant", direction="BUY", conviction=0.55, reasoning="Slight edge"),
    ]


def _mock_claude_response(votes_json: list[dict]) -> MagicMock:
    """Create a mock Claude API response with given vote JSON."""
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=json.dumps(votes_json))]
    return mock_resp


# ── Consensus computation tests ──────────────────────────────────────────────


class TestConsensus:
    def test_buy_majority(self):
        votes = _make_votes_buy_majority()
        direction, score, vetoed = compute_consensus(votes, "BUY")
        assert direction == "BUY"
        assert score > 0.5
        assert vetoed is False

    def test_sell_majority(self):
        votes = _make_votes_sell_majority()
        direction, score, vetoed = compute_consensus(votes, "SELL")
        assert direction == "SELL"
        assert score > 0.4
        assert vetoed is False

    def test_veto_overrides_buy_consensus(self):
        votes = _make_votes_veto()
        direction, score, vetoed = compute_consensus(votes, "BUY")
        assert vetoed is True
        assert direction == "HOLD"
        assert score == 0.0

    def test_tie_produces_low_score(self):
        votes = _make_votes_tie()
        direction, score, vetoed = compute_consensus(votes, "BUY")
        assert vetoed is False
        # Score should be relatively low with a split vote
        assert score < 0.70

    def test_empty_votes_passthrough(self):
        direction, score, vetoed = compute_consensus([], "BUY")
        assert direction == "BUY"
        assert score == 0.5
        assert vetoed is False

    def test_only_risk_manager_can_veto(self):
        """Non-risk-manager veto flag is ignored."""
        votes = [
            AgentVote(
                agent_name="momentum", direction="HOLD", conviction=0.9,
                reasoning="Bad trade", veto=True  # should be ignored
            ),
            AgentVote(agent_name="contrarian", direction="BUY", conviction=0.8, reasoning="ok"),
            AgentVote(agent_name="macro", direction="BUY", conviction=0.7, reasoning="ok"),
            AgentVote(agent_name="risk_manager", direction="BUY", conviction=0.6, reasoning="fine"),
            AgentVote(agent_name="quant", direction="BUY", conviction=0.7, reasoning="ok"),
        ]
        direction, score, vetoed = compute_consensus(votes, "BUY")
        assert vetoed is False
        assert direction == "BUY"

    def test_weighted_voting_risk_manager_counts_more(self):
        """Risk manager weight=1.2 vs quant weight=0.8."""
        votes_rm_heavy = [
            AgentVote(agent_name="risk_manager", direction="SELL", conviction=1.0, reasoning="danger"),
            AgentVote(agent_name="quant", direction="BUY", conviction=1.0, reasoning="edge"),
        ]
        direction, _, _ = compute_consensus(votes_rm_heavy, "BUY")
        # risk_manager: 1.0 * 1.2 = 1.2 for SELL
        # quant: 1.0 * 0.8 = 0.8 for BUY
        assert direction == "SELL"

    def test_score_blending(self):
        """Verify score is weighted conviction / total weight."""
        votes = [
            AgentVote(agent_name="momentum", direction="BUY", conviction=0.80, reasoning="ok"),
        ]
        _, score, _ = compute_consensus(votes, "BUY")
        # momentum weight=1.0, conviction=0.80
        # total_weight=1.0, BUY score = 0.80/1.0 = 0.80
        assert abs(score - 0.80) < 0.01


# ── SwarmDebate class tests ──────────────────────────────────────────────────


class TestSwarmDebate:
    def _make_debate(self, **overrides) -> SwarmDebate:
        cfg = SwarmConfig()
        cfg.enabled = True
        cfg.max_debate_calls = overrides.get("max_calls", 3)
        cfg.timeout_seconds = overrides.get("timeout", 10.0)
        return SwarmDebate(
            config=cfg,
            anthropic_api_key=overrides.get("api_key", "test-key"),
        )

    def test_no_api_key_passthrough(self):
        debate = self._make_debate(api_key="")
        sig = _make_signal()
        result = asyncio.get_event_loop().run_until_complete(debate.debate(sig))
        assert result.votes == []
        assert result.consensus_direction == "BUY"
        assert result.vetoed is False
        assert "passthrough" in result.debate_summary

    def test_budget_exhausted_passthrough(self):
        debate = self._make_debate(max_calls=0)
        sig = _make_signal()
        result = asyncio.get_event_loop().run_until_complete(debate.debate(sig))
        assert result.votes == []
        assert "passthrough" in result.debate_summary

    def test_budget_tracking(self):
        debate = self._make_debate(max_calls=2)
        assert debate.budget_remaining == 2

        votes_json = [
            {"agent": "momentum", "direction": "BUY", "conviction": 0.8, "reasoning": "ok", "risk_flags": [], "veto": False},
            {"agent": "contrarian", "direction": "BUY", "conviction": 0.7, "reasoning": "ok", "risk_flags": [], "veto": False},
            {"agent": "macro", "direction": "BUY", "conviction": 0.6, "reasoning": "ok", "risk_flags": [], "veto": False},
            {"agent": "risk_manager", "direction": "BUY", "conviction": 0.7, "reasoning": "ok", "risk_flags": [], "veto": False},
            {"agent": "quant", "direction": "BUY", "conviction": 0.7, "reasoning": "ok", "risk_flags": [], "veto": False},
        ]
        mock_response = _mock_claude_response(votes_json)

        with patch("nexus.swarm.asyncio.to_thread") as mock_thread:
            async def fake_thread(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_thread.side_effect = fake_thread

            with patch.object(debate, "_get_client") as mock_client:
                mock_client.return_value.messages.create = MagicMock(return_value=mock_response)

                sig = _make_signal()
                asyncio.get_event_loop().run_until_complete(debate.debate(sig))
                assert debate.budget_remaining == 1

                asyncio.get_event_loop().run_until_complete(debate.debate(sig))
                assert debate.budget_remaining == 0

                # Third call should passthrough
                result = asyncio.get_event_loop().run_until_complete(debate.debate(sig))
                assert "passthrough" in result.debate_summary

    def test_successful_debate(self):
        debate = self._make_debate()
        votes_json = [
            {"agent": "momentum", "direction": "BUY", "conviction": 0.85, "reasoning": "Strong trend", "risk_flags": [], "veto": False},
            {"agent": "contrarian", "direction": "BUY", "conviction": 0.60, "reasoning": "Not overextended", "risk_flags": [], "veto": False},
            {"agent": "macro", "direction": "BUY", "conviction": 0.70, "reasoning": "Sector favorable", "risk_flags": [], "veto": False},
            {"agent": "risk_manager", "direction": "BUY", "conviction": 0.75, "reasoning": "Acceptable", "risk_flags": [], "veto": False},
            {"agent": "quant", "direction": "HOLD", "conviction": 0.40, "reasoning": "Weak edge", "risk_flags": [], "veto": False},
        ]
        mock_response = _mock_claude_response(votes_json)

        with patch("nexus.swarm.asyncio.to_thread") as mock_thread:
            async def fake_thread(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_thread.side_effect = fake_thread

            with patch.object(debate, "_get_client") as mock_client:
                mock_client.return_value.messages.create = MagicMock(return_value=mock_response)

                sig = _make_signal()
                result = asyncio.get_event_loop().run_until_complete(debate.debate(sig))

        assert len(result.votes) == 5
        assert result.consensus_direction == "BUY"
        assert result.consensus_score > 0.5
        assert result.vetoed is False
        assert "Swarm" in result.debate_summary

    def test_veto_debate(self):
        debate = self._make_debate()
        votes_json = [
            {"agent": "momentum", "direction": "BUY", "conviction": 0.90, "reasoning": "Great", "risk_flags": [], "veto": False},
            {"agent": "contrarian", "direction": "BUY", "conviction": 0.70, "reasoning": "Ok", "risk_flags": [], "veto": False},
            {"agent": "macro", "direction": "BUY", "conviction": 0.80, "reasoning": "Bullish", "risk_flags": [], "veto": False},
            {"agent": "risk_manager", "direction": "HOLD", "conviction": 0.90, "reasoning": "Earnings in 2 days", "risk_flags": ["earnings_proximity"], "veto": True},
            {"agent": "quant", "direction": "BUY", "conviction": 0.75, "reasoning": "Edge ok", "risk_flags": [], "veto": False},
        ]
        mock_response = _mock_claude_response(votes_json)

        with patch("nexus.swarm.asyncio.to_thread") as mock_thread:
            async def fake_thread(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_thread.side_effect = fake_thread

            with patch.object(debate, "_get_client") as mock_client:
                mock_client.return_value.messages.create = MagicMock(return_value=mock_response)

                sig = _make_signal()
                result = asyncio.get_event_loop().run_until_complete(debate.debate(sig))

        assert result.vetoed is True

    def test_timeout_passthrough(self):
        debate = self._make_debate(timeout=0.001)

        with patch("nexus.swarm.asyncio.to_thread") as mock_thread:
            async def slow_thread(fn, *args, **kwargs):
                await asyncio.sleep(10)  # way too slow
                return fn(*args, **kwargs)

            mock_thread.side_effect = slow_thread

            with patch.object(debate, "_get_client") as mock_client:
                mock_client.return_value.messages.create = MagicMock()

                sig = _make_signal()
                result = asyncio.get_event_loop().run_until_complete(debate.debate(sig))

        assert result.votes == []
        assert "passthrough" in result.debate_summary

    def test_invalid_json_passthrough(self):
        debate = self._make_debate()

        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text="This is not JSON at all")]

        with patch("nexus.swarm.asyncio.to_thread") as mock_thread:
            async def fake_thread(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_thread.side_effect = fake_thread

            with patch.object(debate, "_get_client") as mock_client:
                mock_client.return_value.messages.create = MagicMock(return_value=mock_resp)

                sig = _make_signal()
                result = asyncio.get_event_loop().run_until_complete(debate.debate(sig))

        assert result.votes == []
        assert "passthrough" in result.debate_summary

    def test_reset_cycle(self):
        debate = self._make_debate(max_calls=2)
        debate._debate_calls_this_cycle = 2
        assert debate.budget_remaining == 0

        debate.reset_cycle()
        assert debate.budget_remaining == 2

    def test_parse_votes_with_markdown_fences(self):
        debate = self._make_debate()
        votes_json = [
            {"agent": "momentum", "direction": "BUY", "conviction": 0.8, "reasoning": "ok", "risk_flags": [], "veto": False},
        ]
        raw = f"```json\n{json.dumps(votes_json)}\n```"
        result = debate._parse_votes(raw)
        assert len(result) == 1
        assert result[0].agent_name == "momentum"

    def test_parse_votes_filters_invalid_agents(self):
        debate = self._make_debate()
        votes_json = [
            {"agent": "momentum", "direction": "BUY", "conviction": 0.8, "reasoning": "ok", "risk_flags": [], "veto": False},
            {"agent": "unknown_agent", "direction": "BUY", "conviction": 0.9, "reasoning": "skip me", "risk_flags": [], "veto": False},
        ]
        result = debate._parse_votes(json.dumps(votes_json))
        assert len(result) == 1

    def test_parse_votes_clamps_conviction(self):
        debate = self._make_debate()
        votes_json = [
            {"agent": "momentum", "direction": "BUY", "conviction": 1.5, "reasoning": "ok", "risk_flags": [], "veto": False},
            {"agent": "contrarian", "direction": "SELL", "conviction": -0.3, "reasoning": "ok", "risk_flags": [], "veto": False},
        ]
        result = debate._parse_votes(json.dumps(votes_json))
        assert result[0].conviction == 1.0
        assert result[1].conviction == 0.0


# ── SwarmConfig tests ────────────────────────────────────────────────────────


def test_swarm_config_defaults(monkeypatch):
    monkeypatch.delenv("NEXUS_SWARM_ENABLED", raising=False)
    monkeypatch.delenv("NEXUS_SWARM_MODEL", raising=False)
    cfg = SwarmConfig()
    assert cfg.enabled is False
    assert cfg.max_debate_calls == 3
    assert cfg.min_score_for_debate == 0.70
    assert cfg.consensus_threshold == 0.60
    assert cfg.timeout_seconds == 10.0
    assert "sonnet" in cfg.swarm_model.lower() or "claude" in cfg.swarm_model.lower()


def test_swarm_config_in_nexus_config(monkeypatch):
    monkeypatch.delenv("NEXUS_SWARM_ENABLED", raising=False)
    monkeypatch.delenv("NEXUS_OPTIONS_ENABLED", raising=False)
    cfg = NEXUSConfig()
    assert hasattr(cfg, "swarm")
    assert isinstance(cfg.swarm, SwarmConfig)
    assert cfg.swarm.enabled is False


# ── Memory feedback loop tests ──────────────────────────────────────────────


class TestMemoryFeedback:
    def test_prompt_includes_track_records(self):
        from nexus.swarm import _build_prompt

        sig = _make_signal()
        track_records = "- momentum: 10 trades, 70% win rate, P&L $+500.00\n- contrarian: 8 trades, 63% win rate, P&L $+120.00"
        prompt = _build_prompt(sig, vix=20.0, positions_summary="", agent_track_records=track_records)
        assert "AGENT TRACK RECORDS" in prompt
        assert "momentum: 10 trades" in prompt
        assert "contrarian: 8 trades" in prompt

    def test_prompt_without_track_records(self):
        from nexus.swarm import _build_prompt

        sig = _make_signal()
        prompt = _build_prompt(sig, vix=20.0, positions_summary="")
        assert "AGENT TRACK RECORDS" not in prompt

    def test_debate_passes_track_records(self):
        """Verify debate() forwards agent_track_records to the prompt."""
        cfg = SwarmConfig()
        cfg.enabled = True
        debate = SwarmDebate(config=cfg, anthropic_api_key="test-key")

        votes_json = [
            {"agent": "momentum", "direction": "BUY", "conviction": 0.8, "reasoning": "ok", "risk_flags": [], "veto": False},
            {"agent": "contrarian", "direction": "BUY", "conviction": 0.7, "reasoning": "ok", "risk_flags": [], "veto": False},
            {"agent": "macro", "direction": "BUY", "conviction": 0.6, "reasoning": "ok", "risk_flags": [], "veto": False},
            {"agent": "risk_manager", "direction": "BUY", "conviction": 0.7, "reasoning": "ok", "risk_flags": [], "veto": False},
            {"agent": "quant", "direction": "BUY", "conviction": 0.7, "reasoning": "ok", "risk_flags": [], "veto": False},
        ]
        mock_response = _mock_claude_response(votes_json)

        captured_prompts = []

        with patch("nexus.swarm.asyncio.to_thread") as mock_thread:
            async def fake_thread(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_thread.side_effect = fake_thread

            original_create = MagicMock(return_value=mock_response)

            def capturing_create(**kwargs):
                captured_prompts.append(kwargs.get("messages", [{}])[0].get("content", ""))
                return mock_response

            with patch.object(debate, "_get_client") as mock_client:
                mock_client.return_value.messages.create = capturing_create

                sig = _make_signal()
                result = asyncio.get_event_loop().run_until_complete(
                    debate.debate(sig, agent_track_records="- momentum: 5 trades, 80% win rate")
                )

        assert len(captured_prompts) == 1
        assert "momentum: 5 trades" in captured_prompts[0]
