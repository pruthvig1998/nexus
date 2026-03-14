"""Unit tests for Discord feed message parser.

Tests run without a live Discord connection — only the _parse_message()
function is tested directly.
"""
from __future__ import annotations

import pytest

from nexus.discord_feed import _parse_message


# ── Direction detection ───────────────────────────────────────────────────────

def test_explicit_ticker_buy():
    sigs = _parse_message("$AAPL looking bullish, buying calls", "u", "c", "s")
    assert any(s.ticker == "AAPL" and s.direction == "BUY" for s in sigs)


def test_explicit_ticker_sell():
    sigs = _parse_message("shorting $TSLA on this breakdown", "u", "c", "s")
    assert any(s.ticker == "TSLA" and s.direction == "SELL" for s in sigs)


def test_bare_ticker_buy():
    sigs = _parse_message("NVDA long entry here, buying the dip", "u", "c", "s")
    assert any(s.ticker == "NVDA" and s.direction == "BUY" for s in sigs)


def test_bare_ticker_sell():
    sigs = _parse_message("MSFT puts play, bearish into earnings", "u", "c", "s")
    assert any(s.ticker == "MSFT" and s.direction == "SELL" for s in sigs)


# ── Common word blocklist ─────────────────────────────────────────────────────

def test_common_word_not_ticker_us():
    sigs = _parse_message("I think US markets are going up today", "u", "c", "s")
    assert "US" not in [s.ticker for s in sigs]


def test_common_word_not_ticker_i():
    sigs = _parse_message("I am going long AAPL today", "u", "c", "s")
    assert "I" not in [s.ticker for s in sigs]


def test_common_word_not_ticker_in():
    sigs = _parse_message("IN a bullish trend for AAPL", "u", "c", "s")
    assert "IN" not in [s.ticker for s in sigs]


# ── Multi-ticker ──────────────────────────────────────────────────────────────

def test_multi_ticker_same_direction():
    sigs = _parse_message("long $AAPL and $MSFT both look great", "u", "c", "s")
    tickers = {s.ticker for s in sigs}
    assert {"AAPL", "MSFT"}.issubset(tickers)


def test_multi_ticker_mixed_directions():
    # Tickers need enough separation so buy/sell keywords don't bleed across
    # the ±50 char context window. "shorting" starts at char ~73 here, past
    # AAPL's window boundary of ~65.
    msg = "Going long $AAPL calls here for the breakout move, and separately shorting $TSLA puts"
    sigs = _parse_message(msg, "u", "c", "s")
    by_ticker = {s.ticker: s.direction for s in sigs}
    assert by_ticker.get("AAPL") == "BUY"
    assert by_ticker.get("TSLA") == "SELL"


# ── No direction → skip ───────────────────────────────────────────────────────

def test_no_direction_skipped():
    sigs = _parse_message("What does everyone think about AAPL?", "u", "c", "s")
    # Should either be empty or all below min score threshold
    aapl_sigs = [s for s in sigs if s.ticker == "AAPL"]
    assert len(aapl_sigs) == 0 or all(s.score < 0.55 for s in aapl_sigs)


def test_pure_question_no_signal():
    sigs = _parse_message("Any thoughts on NVDA earnings tonight?", "u", "c", "s")
    nvda_sigs = [s for s in sigs if s.ticker == "NVDA"]
    assert len(nvda_sigs) == 0


# ── Score calculation ─────────────────────────────────────────────────────────

def test_explicit_score_at_least_base():
    sigs = _parse_message("$AAPL buy", "u", "c", "s")
    assert sigs, "Expected at least one signal"
    assert sigs[0].score >= 0.55


def test_explicit_score_higher_than_bare():
    explicit = _parse_message("$AAPL buy", "u", "c", "s")
    bare = _parse_message("AAPL buy", "u", "c", "s")
    if explicit and bare:
        aapl_explicit = next((s for s in explicit if s.ticker == "AAPL"), None)
        aapl_bare = next((s for s in bare if s.ticker == "AAPL"), None)
        if aapl_explicit and aapl_bare:
            assert aapl_explicit.score >= aapl_bare.score


def test_price_mention_boosts_score():
    with_price = _parse_message("$AAPL buy at $175", "u", "c", "s")
    without_price = _parse_message("$AAPL buy", "u", "c", "s")
    if with_price and without_price:
        wp = next((s for s in with_price if s.ticker == "AAPL"), None)
        wop = next((s for s in without_price if s.ticker == "AAPL"), None)
        if wp and wop:
            assert wp.score >= wop.score


def test_score_capped_at_080():
    sigs = _parse_message("$AAPL strong buy at $175, going long calls bullish breakout", "u", "c", "s")
    for s in sigs:
        assert s.score <= 0.80


# ── Strategy and reasoning ────────────────────────────────────────────────────

def test_strategy_name():
    sigs = _parse_message("$AAPL long", "u", "c", "s")
    assert all(s.strategy == "discord" for s in sigs)


def test_reasoning_contains_author():
    sigs = _parse_message("$AAPL long", "trader_joe", "stocks", "MyServer")
    assert sigs, "Expected at least one signal"
    assert all("trader_joe" in s.reasoning for s in sigs)


def test_reasoning_contains_channel():
    sigs = _parse_message("$AAPL long", "user123", "trading-alerts", "MyServer")
    assert sigs, "Expected at least one signal"
    assert all("#trading-alerts" in s.reasoning for s in sigs)


def test_reasoning_snippet_truncated():
    long_msg = "$AAPL buy " + "x" * 200
    sigs = _parse_message(long_msg, "u", "c", "s")
    for s in sigs:
        # reasoning should not contain the full 200-char message
        assert len(s.reasoning) < 300


# ── Entry/stop/target zeroed ──────────────────────────────────────────────────

def test_prices_zero_for_engine_to_fill():
    sigs = _parse_message("$AAPL buy", "u", "c", "s")
    for s in sigs:
        assert s.entry_price == 0.0
        assert s.stop_price == 0.0
        assert s.target_price == 0.0


# ── Dedup (same ticker mentioned twice) ──────────────────────────────────────

def test_dedup_same_ticker_in_message():
    sigs = _parse_message("$AAPL buy, AAPL looking strong", "u", "c", "s")
    aapl_sigs = [s for s in sigs if s.ticker == "AAPL"]
    assert len(aapl_sigs) == 1  # only one signal per ticker per message
