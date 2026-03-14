"""Unit tests for Discord feed message parser.

Tests run without a live Discord connection — only the _parse_message()
function is tested directly.
"""
from __future__ import annotations

import pytest

from nexus.discord_feed import _parse_message


# ── Parametrized BUY keyword detection ───────────────────────────────────────

@pytest.mark.parametrize("keyword", [
    "buy", "long", "calls", "bullish", "moon", "breakout", "entry", "dip",
])
def test_buy_keyword_detected(keyword):
    msg = f"$AAPL {keyword} here"
    sigs = _parse_message(msg, "u", "c", "s")
    assert len(sigs) == 1
    assert sigs[0].ticker == "AAPL"
    assert sigs[0].direction == "BUY"


# ── Parametrized SELL keyword detection ──────────────────────────────────────

@pytest.mark.parametrize("keyword", [
    "sell", "short", "puts", "bearish", "dump", "breakdown", "exit",
])
def test_sell_keyword_detected(keyword):
    msg = f"$TSLA {keyword} now"
    sigs = _parse_message(msg, "u", "c", "s")
    assert len(sigs) == 1
    assert sigs[0].ticker == "TSLA"
    assert sigs[0].direction == "SELL"


# ── Common word blocklist (exhaustive) ───────────────────────────────────────

@pytest.mark.parametrize("word", [
    "I", "A", "AT", "BE", "DO", "GO", "IT", "MY", "OR", "SO", "TO",
    "US", "AN", "AS", "IF", "IS", "IN", "ON", "UP", "BY",
])
def test_common_word_not_extracted_as_ticker(word):
    msg = f"{word} buy AAPL long"
    sigs = _parse_message(msg, "u", "c", "s")
    tickers = [s.ticker for s in sigs]
    assert word not in tickers, f"Common word '{word}' should be blocked"


# ── Precise score boundaries ────────────────────────────────────────────────

def test_score_bare_ticker_is_base():
    """Bare ticker + direction keyword = base score 0.55."""
    sigs = _parse_message("AAPL buy", "u", "c", "s")
    assert len(sigs) == 1
    assert sigs[0].score == 0.55


def test_score_explicit_ticker_no_price():
    """Explicit $TICKER + direction keyword = 0.55 + 0.05 = 0.60."""
    sigs = _parse_message("$AAPL buy", "u", "c", "s")
    assert len(sigs) == 1
    assert sigs[0].score == 0.60


def test_score_explicit_ticker_with_price():
    """Explicit $TICKER + direction + price nearby = 0.55 + 0.05 + 0.05 = 0.65."""
    sigs = _parse_message("$AAPL buy at $175", "u", "c", "s")
    assert len(sigs) == 1
    assert sigs[0].score == 0.65


def test_score_capped_at_080():
    """Score must never exceed the 0.80 cap."""
    sigs = _parse_message(
        "$AAPL strong buy at $175, going long calls bullish breakout", "u", "c", "s"
    )
    for s in sigs:
        assert s.score <= 0.80


def test_score_bare_with_price_is_060():
    """Bare ticker + direction + price nearby = 0.55 + 0.05 = 0.60 (price bonus only)."""
    sigs = _parse_message("AAPL buy at $175", "u", "c", "s")
    assert len(sigs) == 1
    assert sigs[0].score == 0.60


# ── Reasoning field format ───────────────────────────────────────────────────

def test_reasoning_exact_format():
    """Reasoning must be exactly: 'Discord: {author} in #{channel}: {snippet}'."""
    sigs = _parse_message("$AAPL long", "trader_joe", "stocks", "MyServer")
    assert len(sigs) == 1
    assert sigs[0].reasoning.startswith("Discord: trader_joe in #stocks: ")


def test_reasoning_snippet_matches_message_start():
    msg = "$AAPL long entry here"
    sigs = _parse_message(msg, "alice", "alerts", "srv")
    assert len(sigs) == 1
    expected = f"Discord: alice in #alerts: {msg}"
    assert sigs[0].reasoning == expected


def test_reasoning_snippet_truncated_at_120_chars():
    body = "x" * 200
    msg = f"$AAPL buy {body}"
    sigs = _parse_message(msg, "u", "c", "s")
    assert len(sigs) == 1
    snippet_part = sigs[0].reasoning.split(": ", 2)[-1]
    assert len(snippet_part) <= 120


# ── Edge cases ───────────────────────────────────────────────────────────────

def test_empty_string_returns_no_signals():
    sigs = _parse_message("", "u", "c", "s")
    assert sigs == []


def test_whitespace_only_returns_no_signals():
    sigs = _parse_message("   \n\t  ", "u", "c", "s")
    assert sigs == []


def test_numbers_only_returns_no_signals():
    sigs = _parse_message("12345 678 90.12", "u", "c", "s")
    assert sigs == []


def test_emoji_only_returns_no_signals():
    sigs = _parse_message("\U0001f680\U0001f4b0\U0001f4c8", "u", "c", "s")
    assert sigs == []


def test_very_long_message_does_not_crash():
    msg = "$AAPL buy " + "word " * 5000
    sigs = _parse_message(msg, "u", "c", "s")
    assert len(sigs) >= 1
    assert sigs[0].ticker == "AAPL"


# ── Entry/stop/target prices zeroed ─────────────────────────────────────────

def test_all_prices_exactly_zero():
    sigs = _parse_message("$AAPL buy at $175", "u", "c", "s")
    assert len(sigs) == 1
    assert sigs[0].entry_price == 0.0
    assert sigs[0].stop_price == 0.0
    assert sigs[0].target_price == 0.0


# ── Multi-ticker with context isolation ──────────────────────────────────────

def test_multi_ticker_mixed_directions_no_context_bleeding():
    # Tickers need enough separation so buy/sell keywords don't bleed across
    # the +/-50 char context window. "shorting" starts at char ~73 here, past
    # AAPL's window boundary of ~65.
    msg = "Going long $AAPL calls here for the breakout move, and separately shorting $TSLA puts"
    sigs = _parse_message(msg, "u", "c", "s")
    by_ticker = {s.ticker: s.direction for s in sigs}
    assert by_ticker.get("AAPL") == "BUY"
    assert by_ticker.get("TSLA") == "SELL"


# ── Direction: no direction → skip ───────────────────────────────────────────

def test_no_direction_keyword_yields_no_signal():
    sigs = _parse_message("What does everyone think about $AAPL?", "u", "c", "s")
    assert sigs == []


def test_pure_question_no_signal():
    sigs = _parse_message("Any thoughts on NVDA earnings tonight?", "u", "c", "s")
    assert sigs == []


# ── Strategy name ────────────────────────────────────────────────────────────

def test_strategy_is_discord():
    sigs = _parse_message("$AAPL long", "u", "c", "s")
    assert len(sigs) == 1
    assert sigs[0].strategy == "discord"


# ── Dedup (same ticker mentioned twice) ──────────────────────────────────────

def test_dedup_same_ticker_in_message():
    sigs = _parse_message("$AAPL buy, AAPL looking strong", "u", "c", "s")
    aapl_sigs = [s for s in sigs if s.ticker == "AAPL"]
    assert len(aapl_sigs) == 1
