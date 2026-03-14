"""Filter Discord export JSON to keep only trading-relevant messages.

Removes jokes, casual chat, greetings, and off-topic conversation.
Keeps: trading discussion, ticker mentions, market analysis, geopolitics
affecting markets, technical analysis, options flow, earnings, macro events.

Usage:
    python scripts/filter_discord_export.py INPUT.json OUTPUT.json
    python scripts/filter_discord_export.py INPUT.json  # overwrites in-place
    python scripts/filter_discord_export.py ~/Desktop/discord-exports/ --all  # filter all files
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


# ── Relevance detection ───────────────────────────────────────────────────────

# Ticker pattern: $AAPL or standalone 2-5 letter uppercase
_TICKER_EXPLICIT = re.compile(r"\$[A-Z]{1,5}\b")
_TICKER_BARE = re.compile(r"\b[A-Z]{2,5}\b")
_PRICE_PATTERN = re.compile(r"\$\d+(?:\.\d+)?")
_PERCENT_PATTERN = re.compile(r"\d+(?:\.\d+)?%")

# Trading keywords (lowercase for matching)
TRADING_KEYWORDS = {
    # Actions
    "buy", "sell", "long", "short", "calls", "puts", "entry", "exit",
    "stop", "target", "tp", "sl", "dca", "average", "averaging",
    "cover", "hedge", "scalp", "swing", "daytrade", "position",
    # Market terms
    "bullish", "bearish", "breakout", "breakdown", "reversal", "pullback",
    "support", "resistance", "consolidation", "accumulation", "distribution",
    "squeeze", "gamma", "delta", "theta", "vega", "iv", "implied volatility",
    "premium", "strike", "expiry", "expiration", "dte", "itm", "otm", "atm",
    # Technical analysis
    "rsi", "macd", "ema", "sma", "vwap", "fibonacci", "fib", "bollinger",
    "moving average", "trend", "trendline", "channel", "wedge", "flag",
    "pennant", "head and shoulders", "double top", "double bottom",
    "golden cross", "death cross", "divergence", "overbought", "oversold",
    "volume", "candle", "doji", "hammer", "engulfing", "gap",
    # Options
    "call", "put", "spread", "straddle", "strangle", "condor", "butterfly",
    "iron condor", "credit spread", "debit spread", "vertical", "calendar",
    "leaps", "leap", "weeklies", "weekly", "monthlies", "monthly",
    "roll", "assignment", "exercise", "open interest", "oi",
    # Fundamentals / earnings
    "earnings", "er", "eps", "revenue", "guidance", "beat", "miss",
    "dividend", "buyback", "split", "ipo", "offering", "dilution",
    "pe ratio", "p/e", "forward pe", "market cap", "valuation",
    "free cash flow", "fcf", "ebitda", "margin", "growth",
    # Market structure
    "fed", "fomc", "cpi", "ppi", "gdp", "nfp", "jobs report",
    "interest rate", "rate cut", "rate hike", "inflation", "recession",
    "yield", "treasury", "bond", "dxy", "dollar", "vix",
    "spy", "qqq", "iwm", "dia", "spx", "ndx",
    # Geopolitical
    "tariff", "sanction", "war", "conflict", "trade war", "embargo",
    "geopolitical", "election", "policy", "regulation", "sec",
    "china", "russia", "ukraine", "taiwan", "opec", "oil", "crude",
    "nato", "middle east", "iran", "israel",
    # Crypto (if relevant)
    "bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain",
    # Flow / institutional
    "flow", "unusual", "dark pool", "sweep", "block trade", "whale",
    "insider", "13f", "institutional", "hedge fund", "fund",
    # General market
    "market", "rally", "sell-off", "selloff", "crash", "correction",
    "dip", "rip", "pump", "dump", "moon", "tank", "drill",
    "green", "red", "gap up", "gap down", "premarket", "afterhours",
    "futures", "opening", "closing", "bell",
    # Portfolio / risk
    "portfolio", "risk", "reward", "r/r", "risk reward", "profit",
    "loss", "pnl", "p&l", "roi", "return", "drawdown",
    "winner", "loser", "win rate", "batting average",
}

# Noise patterns — messages matching these are likely NOT trading-relevant
NOISE_PATTERNS = [
    re.compile(r"^(lol|lmao|haha|😂|🤣|💀|😭|🔥|💪|👀|🚀|gm|gn|yo|sup|hey|hi|hello|bruh|bro|dude|man|damn|wtf|smh|nah|yea|yeah|yep|nope|true|fr|ong|bet|cap|no cap|fax|w$|l$|gg|rip|oof|brb|ttyl|lmk|idk|idc|ngl|tbh|imo|imho|fwiw|btw|jk|np|ty|thx|thanks|thank you|welcome|congrats|nice|sick|fire|goat|king|legend|mood|vibe|vibes|chill|relax|same|facts|word|period|periodt|slay|sus|sheesh|bussin|lowkey|highkey|deadass|no way|omg|wth|smh|pfft|meh|eh|hmm|huh|ok|okay|alright|aight|cool|dope|lit|gas|cap|ratio|lfg)\s*$", re.IGNORECASE),
    re.compile(r"^.{0,3}$"),  # Very short messages (1-3 chars)
    re.compile(r"^[\U00010000-\U0010FFFF\u2600-\u27BF\u2700-\u27BF\uFE00-\uFE0F\u200D\s]+$"),  # Emoji-only
    re.compile(r"^(good morning|good night|gm everyone|happy friday|happy monday|have a good|enjoy your|see you|later guys|peace out|going to bed|heading out|back later)", re.IGNORECASE),
    re.compile(r"^(what are you guys|anyone here|who's online|what's everyone|how's everyone|how is everyone|what's up|wassup|wasaup|wya)", re.IGNORECASE),
]


def is_trading_relevant(content: str) -> bool:
    """Check if a message is related to trading, markets, or geopolitics."""
    if not content or not content.strip():
        return False

    text = content.strip()

    # Skip very short messages with no substance
    if len(text) < 5:
        return False

    # Check noise patterns first (fast rejection)
    for pattern in NOISE_PATTERNS:
        if pattern.match(text):
            return False

    lower = text.lower()

    # Explicit ticker mention ($AAPL) → almost certainly relevant
    if _TICKER_EXPLICIT.search(text):
        return True

    # Price or percentage → likely relevant
    if _PRICE_PATTERN.search(text) and len(text) > 15:
        return True
    if _PERCENT_PATTERN.search(text) and len(text) > 15:
        return True

    # URL with financial domains → relevant
    financial_domains = ["tradingview", "finviz", "stocktwits", "seekingalpha",
                         "cnbc", "bloomberg", "reuters", "wsj", "marketwatch",
                         "barchart", "yahoo.com/finance", "investing.com",
                         "zerohedge", "unusual-whales"]
    if any(domain in lower for domain in financial_domains):
        return True

    # Check for trading keywords
    words = set(re.findall(r"[a-z]+", lower))
    # Also check bigrams
    word_list = re.findall(r"[a-z]+", lower)
    bigrams = {f"{word_list[i]} {word_list[i+1]}" for i in range(len(word_list) - 1)}

    keyword_hits = (words | bigrams) & TRADING_KEYWORDS
    if keyword_hits:
        # Require at least some substance (not just "market" in a casual sentence)
        if len(text) > 20 or len(keyword_hits) >= 2:
            return True
        # Short message but has a bare ticker nearby
        if _TICKER_BARE.search(text):
            return True

    # Bare uppercase tickers (2-5 chars) with surrounding context
    bare_tickers = _TICKER_BARE.findall(text)
    common_words = {"I", "A", "AT", "BE", "DO", "GO", "IT", "MY", "OR", "SO",
                    "TO", "US", "AN", "AS", "IF", "IS", "IN", "ON", "UP", "BY",
                    "AM", "NO", "OK", "OH", "HE", "WE", "ME", "OF", "THE",
                    "AND", "BUT", "NOT", "ALL", "HAS", "HAD", "WAS", "HIS",
                    "HER", "ARE", "FOR", "CAN", "DID", "GET", "GOT", "HAS",
                    "HOW", "ITS", "LET", "MAY", "NEW", "NOW", "OLD", "OUR",
                    "OUT", "OWN", "SAY", "SHE", "TOO", "USE", "WHO", "WHY",
                    "YES", "YET", "YOU", "JUST", "LIKE", "ALSO", "BACK",
                    "BEEN", "COME", "EACH", "EVEN", "GIVE", "GOOD", "HAVE",
                    "HERE", "HIGH", "KEEP", "KNOW", "LAST", "LONG", "LOOK",
                    "MADE", "MAKE", "MORE", "MOST", "MUCH", "MUST", "NAME",
                    "ONLY", "OVER", "PART", "REAL", "SAID", "SAME", "SOME",
                    "TAKE", "TELL", "THAN", "THAT", "THEM", "THEN", "THEY",
                    "THIS", "TIME", "VERY", "WANT", "WELL", "WENT", "WERE",
                    "WHAT", "WHEN", "WILL", "WITH", "WORK", "YEAR", "YOUR",
                    "FROM", "WILL", "BEEN", "INTO", "MANY", "NEXT", "SUCH",
                    "STILL", "THINK", "GOING", "COULD", "WOULD", "SHOULD",
                    "WHERE", "WHICH", "THEIR", "THERE", "THESE", "THOSE",
                    "ABOUT", "AFTER", "BEING", "COULD", "EVERY", "FIRST",
                    "OTHER", "NEVER", "RIGHT", "SINCE", "THING", "UNDER",
                    "WORLD", "AGAIN", "MIGHT", "GREAT", "GONNA", "YEAH",
                    "SURE", "MAYBE", "CAUSE", "DAMN", "SHIT", "FUCK",
                    "LMAO", "LMFAO", "BRUH", "GUYS", "DUDE", "MANS",
                    "NEED", "DONT", "DONT", "WONT", "CANT", "ISNT",
                    "ARENT", "WASNT", "DIDNT", "HASNT", "HADNT",
                    "LOL", "OMG", "WTF", "SMH", "TBH", "IMO", "IDK",
                    "NGL", "FYI", "FAQ", "RIP", "GG", "GL", "HF",
                    "AMA", "TIL"}
    real_tickers = [t for t in bare_tickers if t not in common_words]
    if real_tickers and len(text) > 15:
        return True

    return False


def filter_export(input_path: str, output_path: str | None = None) -> dict:
    """Filter a Discord export JSON file, keeping only trading-relevant messages.

    Returns dict with stats: {total, kept, removed, pct_kept}.
    """
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "messages" not in data:
        print(f"  Skipping (not a Discord export): {input_path}")
        return {"total": 0, "kept": 0, "removed": 0, "pct_kept": 0}

    original = data["messages"]
    filtered = [msg for msg in original if is_trading_relevant(msg.get("content", ""))]

    data["messages"] = filtered
    data["messageCount"] = len(filtered)

    out = output_path or input_path
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    stats = {
        "total": len(original),
        "kept": len(filtered),
        "removed": len(original) - len(filtered),
        "pct_kept": round(100 * len(filtered) / max(len(original), 1), 1),
    }
    return stats


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python filter_discord_export.py INPUT.json [OUTPUT.json]")
        print("  python filter_discord_export.py DIRECTORY/ --all")
        sys.exit(1)

    path = Path(sys.argv[1]).expanduser()
    filter_all = "--all" in sys.argv

    if path.is_dir() and filter_all:
        files = sorted(path.glob("*.json"))
        print(f"\nFiltering {len(files)} files in {path}\n")
        total_orig = 0
        total_kept = 0
        for f in files:
            stats = filter_export(str(f))
            total_orig += stats["total"]
            total_kept += stats["kept"]
            if stats["total"] > 0:
                name = f.stem[:60]
                print(f"  {name:<62} {stats['total']:>6} → {stats['kept']:>6}  "
                      f"({stats['pct_kept']:>5.1f}% kept)")
        print(f"\n  {'TOTAL':<62} {total_orig:>6} → {total_kept:>6}  "
              f"({round(100*total_kept/max(total_orig,1),1):>5.1f}% kept)")
        print(f"  Removed {total_orig - total_kept:,} non-trading messages.\n")

    elif path.is_file():
        output = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else None
        stats = filter_export(str(path), output)
        print(f"\n  {path.name}")
        print(f"  {stats['total']:,} messages → {stats['kept']:,} kept "
              f"({stats['pct_kept']}%), {stats['removed']:,} removed")
        print(f"  Output: {output or path}\n")

    else:
        print(f"Error: {path} not found or use --all for directories")
        sys.exit(1)


if __name__ == "__main__":
    main()
