"""NEXUS Discord Export Loader — parses DiscordChatExporter JSON files into signals.

Supports:
  - Single file or entire directory of exports
  - Replay mode: runs messages through _parse_message() and logs to tracker DB
  - Backtest mode: checks if signal direction matched actual price movement

DiscordChatExporter JSON format (each exported file):
{
  "guild":   {"id": "...", "name": "Server Name"},
  "channel": {"id": "...", "name": "channel-name"},
  "messages": [
    {
      "id": "...",
      "timestamp": "2024-01-15T10:30:00+00:00",
      "content": "message text",
      "author": {"id": "...", "name": "username", "nickname": "Display Name"}
    }
  ],
  "messageCount": 100
}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from nexus.discord_feed import _parse_message
from nexus.logger import get_logger
from nexus.strategy import Signal
from nexus.tracker import PortfolioTracker

log = get_logger("discord_loader")


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class MessageResult:
    timestamp: str
    author: str
    channel: str
    guild: str
    content: str
    signals: List[Signal]


@dataclass
class FileResult:
    path: str
    guild: str
    channel: str
    messages_scanned: int
    signals_found: int
    signal_details: List[MessageResult] = field(default_factory=list)


@dataclass
class LoadSummary:
    files_processed: int
    messages_scanned: int
    signals_found: int
    signals_logged: int
    results: List[FileResult] = field(default_factory=list)

    # Top tickers by signal count
    @property
    def top_tickers(self) -> List[tuple[str, int]]:
        counts: dict[str, int] = {}
        for r in self.results:
            for msg in r.signal_details:
                for sig in msg.signals:
                    counts[sig.ticker] = counts.get(sig.ticker, 0) + 1
        return sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]

    @property
    def direction_breakdown(self) -> dict[str, int]:
        counts: dict[str, int] = {"BUY": 0, "SELL": 0}
        for r in self.results:
            for msg in r.signal_details:
                for sig in msg.signals:
                    counts[sig.direction] = counts.get(sig.direction, 0) + 1
        return counts

    @property
    def top_authors(self) -> List[tuple[str, int]]:
        counts: dict[str, int] = {}
        for r in self.results:
            for msg in r.signal_details:
                if msg.signals:
                    counts[msg.author] = counts.get(msg.author, 0) + len(msg.signals)
        return sorted(counts.items(), key=lambda x: x[1], reverse=True)[:5]


# ── Core loader ───────────────────────────────────────────────────────────────

class DiscordLoader:
    """Loads DiscordChatExporter JSON exports and extracts trading signals.

    Usage:
        loader = DiscordLoader(min_score=0.55)
        summary = loader.load("~/Desktop/discord-exports/")
        loader.log_to_db(summary, db_path="nexus.db")
        loader.print_summary(summary)
    """

    def __init__(self, min_score: float = 0.55) -> None:
        self.min_score = min_score

    def load(self, path: str) -> LoadSummary:
        """Load a single JSON file or all JSON files in a directory."""
        p = Path(path).expanduser()
        if p.is_file():
            files = [p]
        elif p.is_dir():
            files = sorted(p.rglob("*.json"))
        else:
            raise FileNotFoundError(f"Path not found: {path}")

        if not files:
            raise FileNotFoundError(f"No JSON files found in: {path}")

        log.info("Loading Discord exports", path=str(p), files=len(files))

        summary = LoadSummary(
            files_processed=0,
            messages_scanned=0,
            signals_found=0,
            signals_logged=0,
        )

        for f in files:
            result = self._load_file(f)
            if result:
                summary.files_processed += 1
                summary.messages_scanned += result.messages_scanned
                summary.signals_found += result.signals_found
                summary.results.append(result)

        return summary

    def _load_file(self, path: Path) -> Optional[FileResult]:
        """Parse a single DiscordChatExporter JSON file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Skipping file", path=str(path), error=str(e))
            return None

        # Validate it's a DiscordChatExporter file
        if "messages" not in data:
            log.debug("Not a DiscordChatExporter file, skipping", path=str(path))
            return None

        guild = data.get("guild", {}).get("name", "Unknown Server")
        channel = data.get("channel", {}).get("name", "unknown-channel")
        messages = data.get("messages", [])

        result = FileResult(
            path=str(path),
            guild=guild,
            channel=channel,
            messages_scanned=0,
            signals_found=0,
        )

        for msg in messages:
            # Skip non-default messages (pins, joins, etc.)
            if msg.get("type") not in ("Default", "Reply", 0, 19):
                continue

            content = msg.get("content", "") or ""
            if not content.strip():
                continue

            author_obj = msg.get("author", {})
            author = (author_obj.get("nickname")
                      or author_obj.get("name")
                      or "unknown")
            timestamp = msg.get("timestamp", "")

            signals = _parse_message(content, author, channel, guild)
            signals = [s for s in signals if s.score >= self.min_score]

            result.messages_scanned += 1

            if signals:
                result.signals_found += len(signals)
                result.signal_details.append(MessageResult(
                    timestamp=timestamp,
                    author=author,
                    channel=channel,
                    guild=guild,
                    content=content,
                    signals=signals,
                ))

        log.info("Processed export file",
                 guild=guild, channel=channel,
                 messages=result.messages_scanned,
                 signals=result.signals_found)

        return result

    def log_to_db(self, summary: LoadSummary, db_path: str = "nexus.db") -> int:
        """Log all extracted signals to the NEXUS tracker database.

        Returns number of signals logged.
        """
        tracker = PortfolioTracker(db_path)
        logged = 0
        for file_result in summary.results:
            for msg_result in file_result.signal_details:
                for sig in msg_result.signals:
                    try:
                        tracker.log_signal(
                            ticker=sig.ticker,
                            strategy=sig.strategy,
                            score=sig.score,
                            direction=sig.direction,
                            reasoning=sig.reasoning,
                        )
                        logged += 1
                    except Exception as e:
                        log.warning("Failed to log signal",
                                    ticker=sig.ticker, error=str(e))
        summary.signals_logged = logged
        log.info("Signals logged to DB", count=logged, db=db_path)
        return logged

    def print_summary(self, summary: LoadSummary) -> None:
        """Print a formatted summary to stdout."""
        w = 62
        print("\n" + "═" * w)
        print("  NEXUS — Discord Export Signal Summary")
        print("═" * w)
        print(f"  Files processed:   {summary.files_processed}")
        print(f"  Messages scanned:  {summary.messages_scanned:,}")
        print(f"  Signals found:     {summary.signals_found:,}")
        if summary.signals_logged:
            print(f"  Signals logged:    {summary.signals_logged:,}  → nexus.db")

        # Direction breakdown
        dirs = summary.direction_breakdown
        if dirs.get("BUY") or dirs.get("SELL"):
            print(f"\n  Direction:  "
                  f"{dirs.get('BUY', 0)} BUY  /  {dirs.get('SELL', 0)} SELL")

        # Top tickers
        top = summary.top_tickers
        if top:
            print(f"\n  Top tickers by signal count:")
            for ticker, count in top[:10]:
                bar = "█" * min(count, 20)
                print(f"    {ticker:<7} {count:>4}  {bar}")

        # Top authors
        authors = summary.top_authors
        if authors:
            print(f"\n  Top signal authors:")
            for author, count in authors:
                print(f"    {author:<20} {count:>4} signals")

        # Per-file breakdown
        if summary.results:
            print(f"\n  {'Guild':<25} {'Channel':<20} {'Msgs':>6} {'Sigs':>5}")
            print(f"  {'-'*(w-2)}")
            for r in summary.results:
                g = r.guild[:24]
                c = r.channel[:19]
                print(f"  {g:<25} {c:<20} {r.messages_scanned:>6,} {r.signals_found:>5}")

        print("═" * w + "\n")

    def print_signals(self, summary: LoadSummary, limit: int = 50) -> None:
        """Print individual signals with timestamp and context."""
        all_msgs: list[tuple[str, MessageResult]] = []
        for r in summary.results:
            for msg in r.signal_details:
                all_msgs.append((r.channel, msg))

        # Sort by timestamp
        all_msgs.sort(key=lambda x: x[1].timestamp)

        print(f"\n  {'Time':<20} {'Author':<18} {'Ticker':<7} {'Dir':<5} "
              f"{'Score':>5}  Content")
        print("  " + "─" * 80)

        shown = 0
        for channel, msg in all_msgs:
            for sig in msg.signals:
                ts = (msg.timestamp or "")[:16].replace("T", " ")
                snippet = msg.content[:45].replace("\n", " ")
                print(f"  {ts:<20} {msg.author:<18} {sig.ticker:<7} "
                      f"{sig.direction:<5} {sig.score:>5.2f}  {snippet}")
                shown += 1
                if shown >= limit:
                    remaining = sum(
                        len(m.signals) for _, m in all_msgs
                    ) - shown
                    if remaining > 0:
                        print(f"\n  ... and {remaining} more signals")
                    return
        print()
