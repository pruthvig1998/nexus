"""Multi-agent swarm debate engine — MiroFish-inspired consensus for signals.

5 AI trader agents with distinct personas evaluate each high-conviction
signal via a single Claude API call.  Weighted voting produces a consensus
score; the Risk Manager agent has veto power.

Cost: ~1 Claude API call per debated signal (all 5 perspectives in one prompt).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from nexus.config import SwarmConfig
from nexus.logger import get_logger
from nexus.strategy import Signal

log = get_logger("swarm")


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class AgentPersona:
    name: str
    role: str
    system_prompt: str
    weight: float = 1.0


@dataclass
class AgentVote:
    agent_name: str
    direction: str  # BUY | SELL | HOLD
    conviction: float  # 0.0-1.0
    reasoning: str
    risk_flags: List[str] = field(default_factory=list)
    veto: bool = False


@dataclass
class SwarmDebateResult:
    original_signal: Signal
    votes: List[AgentVote]
    consensus_direction: str
    consensus_score: float
    debate_summary: str
    vetoed: bool = False


# ── Agent Personas ───────────────────────────────────────────────────────────

PERSONAS: List[AgentPersona] = [
    AgentPersona(
        name="momentum",
        role="Momentum Trader",
        weight=1.0,
        system_prompt=(
            "You are an aggressive momentum trader. You FAVOR signals with strong "
            "trend confirmation: rising volume, MACD bullish cross, price above 20 SMA, "
            "and breakout patterns. You are skeptical of low-volume signals and "
            "counter-trend entries. You judge trades on whether momentum is accelerating."
        ),
    ),
    AgentPersona(
        name="contrarian",
        role="Contrarian / Value Analyst",
        weight=1.0,
        system_prompt=(
            "You are a skeptical contrarian. You LOOK FOR reasons the trade will fail: "
            "overextension (RSI >75 for buys), exhaustion signals, crowded trades, and "
            "mean-reversion setups that conflict with the proposed direction. "
            "You push back on momentum trades near resistance and flag when the "
            "crowd is on one side. You only agree when the setup is genuinely under-loved."
        ),
    ),
    AgentPersona(
        name="macro",
        role="Macro Analyst",
        weight=0.8,
        system_prompt=(
            "You analyze the macro environment. Consider VIX level (>25 = elevated fear), "
            "sector rotation patterns, Fed policy expectations, and correlation with "
            "SPY/QQQ. Flag if the ticker's sector is out of favor or if broad market "
            "conditions are hostile to the proposed trade direction."
        ),
    ),
    AgentPersona(
        name="risk_manager",
        role="Risk Manager",
        weight=1.2,
        system_prompt=(
            "You are the portfolio risk manager with VETO POWER. Evaluate: "
            "1) Position sizing risk — is the stop wide relative to entry? "
            "2) Correlation with existing positions — would this concentrate risk? "
            "3) Time-of-day risk — late-day entries carry overnight gap risk. "
            "4) Earnings proximity — avoid new positions within 3 days of earnings. "
            "5) Volatility regime — is VIX suggesting caution? "
            "Set veto=true ONLY for clear risk violations (multiple flags). "
            "A single concern is a flag, not a veto."
        ),
    ),
    AgentPersona(
        name="quant",
        role="Quantitative Analyst",
        weight=0.8,
        system_prompt=(
            "You evaluate the statistical edge. Consider: "
            "1) Z-score of the current move — is it extreme enough to trade? "
            "2) Bollinger %B position — where is price relative to bands? "
            "3) Historical pattern: similar RSI + MACD + volume setups — what is "
            "the base-rate win probability? "
            "4) Risk-reward ratio — is the stop/target asymmetry sufficient (>2:1)? "
            "You need quantitative evidence, not narratives."
        ),
    ),
]


# ── Prompt Builder ───────────────────────────────────────────────────────────

_DEBATE_PROMPT = """\
You are simulating 5 trading agents who independently evaluate whether to execute this trade.
Each agent has a distinct perspective. Respond as each agent IN ORDER.

═══ SIGNAL ═══
Ticker: {ticker}
Direction: {direction}
Score: {score:.2f}
Strategy: {strategy}
Reasoning: {reasoning}

═══ TECHNICAL DATA ═══
Entry: ${entry:.2f} | Stop: ${stop:.2f} | Target: ${target:.2f}
RSI: {rsi:.1f} | MACD Hist: {macd:.4f} | BB %B: {bb:.2f} | Volume: {vol:.1f}x avg
Risk/Reward: {rr:.1f}:1

═══ MARKET CONTEXT ═══
VIX: {vix:.1f}
{positions_summary}

═══ AGENTS ═══
{agents_block}

{memory_block}═══ RESPONSE FORMAT ═══
Return ONLY a valid JSON array with exactly 5 objects (one per agent, in order).
Each object must have these fields:
- "agent": agent name (string, lowercase)
- "direction": "BUY" or "SELL" or "HOLD" (string)
- "conviction": 0.0 to 1.0 (number)
- "reasoning": one-sentence explanation (string)
- "risk_flags": list of risk flag strings, empty list if none (array)
- "veto": true or false (boolean, only risk_manager can set true)

No markdown. No explanation outside the JSON array. Start with [ and end with ]."""

_AGENT_BLOCK = """Agent {n} — {role}:
{prompt}
"""


def _build_prompt(
    signal: Signal,
    vix: float,
    positions_summary: str,
    agent_track_records: str = "",
) -> str:
    """Build the debate prompt for a single signal."""
    risk_per_share = abs(signal.entry_price - signal.stop_price) if signal.stop_price else 0.01
    rr = abs(signal.target_price - signal.entry_price) / risk_per_share if risk_per_share > 0 else 0

    agents_block = "\n".join(
        _AGENT_BLOCK.format(n=i + 1, role=p.role, prompt=p.system_prompt)
        for i, p in enumerate(PERSONAS)
    )

    memory_block = ""
    if agent_track_records:
        memory_block = f"═══ AGENT TRACK RECORDS ═══\n{agent_track_records}\nConsider each agent's historical accuracy when weighing your vote.\n\n"

    return _DEBATE_PROMPT.format(
        ticker=signal.ticker,
        direction=signal.direction,
        score=signal.score,
        strategy=signal.strategy,
        reasoning=signal.reasoning[:200],
        entry=signal.entry_price,
        stop=signal.stop_price,
        target=signal.target_price,
        rsi=signal.rsi_val,
        macd=signal.macd_hist,
        bb=signal.bb_pct_b,
        vol=signal.vol_ratio,
        rr=rr,
        vix=vix,
        positions_summary=positions_summary or "No open positions",
        agents_block=agents_block,
        memory_block=memory_block,
    )


# ── Consensus Computation ────────────────────────────────────────────────────


def compute_consensus(
    votes: List[AgentVote],
    signal_direction: str,
    threshold: float = 0.60,
) -> tuple[str, float, bool]:
    """Compute weighted consensus from agent votes.

    Returns (consensus_direction, consensus_score, vetoed).
    consensus_score is in [0, 1] — fraction of weighted conviction that
    agrees with the winning direction.
    """
    # Check for veto first
    for v in votes:
        if v.veto and v.agent_name == "risk_manager":
            return "HOLD", 0.0, True

    if not votes:
        return signal_direction, 0.5, False

    # Map agent name → persona weight
    weight_map = {p.name: p.weight for p in PERSONAS}

    # Accumulate weighted conviction per direction
    direction_scores: Dict[str, float] = {"BUY": 0.0, "SELL": 0.0, "HOLD": 0.0}
    total_weight = 0.0

    for v in votes:
        w = weight_map.get(v.agent_name, 1.0)
        direction_scores[v.direction] += v.conviction * w
        total_weight += w

    if total_weight == 0:
        return signal_direction, 0.5, False

    # Winner is the direction with highest weighted conviction
    winner = max(direction_scores, key=lambda d: direction_scores[d])
    consensus_score = direction_scores[winner] / total_weight

    return winner, round(consensus_score, 4), False


def _build_summary(votes: List[AgentVote], consensus_dir: str, score: float) -> str:
    """One-line debate summary for signal reasoning."""
    agree = sum(1 for v in votes if v.direction == consensus_dir)
    return f"Swarm {agree}/5 {consensus_dir} (score={score:.2f})"


# ── Main Debate Class ────────────────────────────────────────────────────────


class SwarmDebate:
    """Run multi-agent debates on trading signals using a single Claude call."""

    def __init__(
        self,
        config: SwarmConfig,
        anthropic_api_key: str,
        ai_model: str | None = None,
    ) -> None:
        self._cfg = config
        self._api_key = anthropic_api_key
        self._model = ai_model or config.swarm_model
        self._client: Any = None
        self._debate_calls_this_cycle = 0
        self._cycle_reset_ts = 0.0

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def reset_cycle(self) -> None:
        """Reset per-cycle budget counter. Call at start of each scan cycle."""
        self._debate_calls_this_cycle = 0
        self._cycle_reset_ts = time.monotonic()

    @property
    def budget_remaining(self) -> int:
        return max(0, self._cfg.max_debate_calls - self._debate_calls_this_cycle)

    async def debate(
        self,
        signal: Signal,
        vix: float = 20.0,
        positions_summary: str = "",
        agent_track_records: str = "",
    ) -> SwarmDebateResult:
        """Run multi-agent debate on a signal.

        Returns SwarmDebateResult. On failure (no API key, timeout, parse error),
        returns a passthrough result that preserves the original signal unchanged.
        """
        # Budget check
        if self._debate_calls_this_cycle >= self._cfg.max_debate_calls:
            log.debug("Swarm budget exhausted", ticker=signal.ticker)
            return self._passthrough(signal)

        # API key check
        if not self._api_key:
            log.debug("No API key for swarm debate")
            return self._passthrough(signal)

        prompt = _build_prompt(signal, vix, positions_summary, agent_track_records)

        try:
            client = self._get_client()
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.messages.create,
                    model=self._model,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=self._cfg.timeout_seconds,
            )
            self._debate_calls_this_cycle += 1

            raw_text = response.content[0].text.strip()
            votes = self._parse_votes(raw_text)

            if not votes:
                log.warning("Failed to parse swarm votes", ticker=signal.ticker)
                return self._passthrough(signal)

            consensus_dir, consensus_score, vetoed = compute_consensus(
                votes, signal.direction, self._cfg.consensus_threshold
            )
            summary = _build_summary(votes, consensus_dir, consensus_score)

            log.info(
                "Swarm debate complete",
                ticker=signal.ticker,
                consensus=consensus_dir,
                score=consensus_score,
                vetoed=vetoed,
                votes=len(votes),
            )

            return SwarmDebateResult(
                original_signal=signal,
                votes=votes,
                consensus_direction=consensus_dir,
                consensus_score=consensus_score,
                debate_summary=summary,
                vetoed=vetoed,
            )

        except asyncio.TimeoutError:
            log.warning("Swarm debate timed out", ticker=signal.ticker)
            return self._passthrough(signal)
        except Exception as e:
            log.warning("Swarm debate failed", ticker=signal.ticker, error=str(e))
            return self._passthrough(signal)

    def _parse_votes(self, raw: str) -> List[AgentVote]:
        """Parse JSON array of 5 agent votes from Claude response."""
        # Strip any markdown fences
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
            text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON array from response
            start = text.find("[")
            end = text.rfind("]")
            if start == -1 or end == -1:
                return []
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return []

        if not isinstance(data, list):
            return []

        valid_agents = {p.name for p in PERSONAS}
        votes: List[AgentVote] = []

        for item in data:
            if not isinstance(item, dict):
                continue
            agent = item.get("agent", "").lower().strip()
            if agent not in valid_agents:
                continue
            direction = item.get("direction", "HOLD").upper()
            if direction not in ("BUY", "SELL", "HOLD"):
                direction = "HOLD"
            conviction = max(0.0, min(1.0, float(item.get("conviction", 0.5))))
            reasoning = str(item.get("reasoning", ""))[:200]
            risk_flags = item.get("risk_flags", [])
            if not isinstance(risk_flags, list):
                risk_flags = []
            veto = bool(item.get("veto", False)) and agent == "risk_manager"

            votes.append(
                AgentVote(
                    agent_name=agent,
                    direction=direction,
                    conviction=conviction,
                    reasoning=reasoning,
                    risk_flags=[str(f) for f in risk_flags[:5]],
                    veto=veto,
                )
            )

        return votes

    @staticmethod
    def _passthrough(signal: Signal) -> SwarmDebateResult:
        """Return a result that preserves the original signal unchanged."""
        return SwarmDebateResult(
            original_signal=signal,
            votes=[],
            consensus_direction=signal.direction,
            consensus_score=signal.score,
            debate_summary="passthrough (no debate)",
            vetoed=False,
        )
