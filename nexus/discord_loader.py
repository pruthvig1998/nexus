"""NEXUS Discord Export Loader — parses DiscordChatExporter JSON files into signals.

Supports:
  - Single file or entire directory of exports
  - DiscordChatExporter v2 and v3 JSON formats
  - Thread messages nested under channels
  - Deduplication of signals across overlapping export files
  - Date-range filtering via --since / --until
  - Replay mode: runs messages through _parse_message() and logs to tracker DB
  - Progress reporting for large exports

DiscordChatExporter v2 format:
{
  "guild":   {"id": "...", "name": "Server Name"},
  "channel": {"id": "...", "name": "channel-name", "type": "GuildTextChannel"},
  "messages": [...]
}

DiscordChatExporter v3 format (some exports nest differently):
{
  "guild":   {"id": "...", "name": "Server Name"},
  "channel": {"id": "...", "name": "channel-name", "categoryName": "..."},
  "dateRange": {"after": "...", "before": "..."},
  "messages": [...]
}

Thread exports may appear as:
  - channel.type == "GuildPublicThread" / "GuildPrivateThread"
  - or messages with a "thread" key containing nested messages
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from nexus.discord_feed import _parse_message
from nexus.logger import get_logger
from nexus.strategy import Signal
from nexus.tracker import PortfolioTracker

log = get_logger("discord_loader")

# Message types considered valid content (DiscordChatExporter uses both string
# labels and integer codes depending on version).
_VALID_MSG_TYPES = {"Default", "Reply", 0, 19}

# Progress dot interval — print a dot every N messages to show progress.
_PROGRESS_INTERVAL = 500


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class MessageResult:
    message_id: str
    timestamp: str
    author: str
    channel: str
    guild: str
    content: str
    signals: List[Signal]
    is_thread: bool = False


@dataclass
class FileResult:
    path: str
    guild: str
    channel: str
    messages_scanned: int
    signals_found: int
    skipped_duplicate: int = 0
    signal_details: List[MessageResult] = field(default_factory=list)


@dataclass
class LoadSummary:
    files_processed: int
    messages_scanned: int
    signals_found: int
    signals_logged: int
    duplicates_skipped: int = 0
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

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the summary to a JSON-compatible dict."""
        return {
            "files_processed": self.files_processed,
            "messages_scanned": self.messages_scanned,
            "signals_found": self.signals_found,
            "signals_logged": self.signals_logged,
            "duplicates_skipped": self.duplicates_skipped,
            "top_tickers": self.top_tickers,
            "direction_breakdown": self.direction_breakdown,
            "top_authors": self.top_authors,
            "results": [
                {
                    "path": r.path,
                    "guild": r.guild,
                    "channel": r.channel,
                    "messages_scanned": r.messages_scanned,
                    "signals_found": r.signals_found,
                    "skipped_duplicate": r.skipped_duplicate,
                    "signal_details": [
                        {
                            "message_id": m.message_id,
                            "timestamp": m.timestamp,
                            "author": m.author,
                            "channel": m.channel,
                            "guild": m.guild,
                            "content": m.content,
                            "is_thread": m.is_thread,
                            "signals": [asdict(s) for s in m.signals],
                        }
                        for m in r.signal_details
                    ],
                }
                for r in self.results
            ],
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_author(author_obj: dict) -> str:
    """Extract the best display name from a message author object.

    Priority: nickname > name > discriminator-based > 'unknown'.
    """
    if not author_obj:
        return "unknown"

    nickname = (author_obj.get("nickname") or "").strip()
    if nickname:
        return nickname

    name = (author_obj.get("name") or "").strip()
    if name:
        return name

    # Some older exports only have id + discriminator
    discriminator = author_obj.get("discriminator", "")
    user_id = author_obj.get("id", "")
    if discriminator and discriminator != "0000":
        return f"user#{discriminator}"
    if user_id:
        return f"user_{user_id[:8]}"

    return "unknown"


def _parse_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp string into a timezone-aware datetime.

    Handles both offset-aware (e.g. +00:00) and naive timestamps (treated as UTC).
    Returns None if parsing fails.
    """
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _extract_guild_name(data: dict) -> str:
    """Extract guild/server name, handling v2 and v3 format differences."""
    guild = data.get("guild")
    if isinstance(guild, dict):
        return guild.get("name") or guild.get("id") or "Unknown Server"
    # Some exports put guild name as a top-level string
    if isinstance(guild, str):
        return guild
    return "Unknown Server"


def _extract_channel_info(data: dict) -> tuple[str, str]:
    """Extract channel name and type from the export data.

    Returns (channel_name, channel_type).
    """
    channel = data.get("channel", {})
    if not isinstance(channel, dict):
        return ("unknown-channel", "GuildTextChannel")

    name = channel.get("name") or channel.get("id") or "unknown-channel"
    ch_type = channel.get("type") or "GuildTextChannel"
    return (name, str(ch_type))


def _is_discord_export(data: dict) -> bool:
    """Detect whether a JSON dict looks like a DiscordChatExporter file.

    Accepts both v2 and v3 formats. The key indicator is the presence of
    a 'messages' key (list) and typically a 'channel' key.
    """
    if not isinstance(data, dict):
        return False
    # Must have a messages key that is a list
    messages = data.get("messages")
    if not isinstance(messages, list):
        return False
    # Should have at least a channel or guild key
    if "channel" in data or "guild" in data:
        return True
    # Some minimal exports only have messages + messageCount
    if "messageCount" in data:
        return True
    return False


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
        # Track seen message IDs globally across all files for deduplication.
        self._seen_ids: set[str] = set()

    def load(
        self,
        path: str,
        *,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> LoadSummary:
        """Load a single JSON file or all JSON files in a directory.

        Args:
            path: File or directory path (supports ~ expansion).
            since: Optional ISO date string — only include messages on or after
                   this timestamp (e.g. "2024-06-01" or "2024-06-01T00:00:00Z").
            until: Optional ISO date string — only include messages before this
                   timestamp.

        Returns:
            LoadSummary with all extracted signals.
        """
        p = Path(path).expanduser()
        if p.is_file():
            files = [p]
        elif p.is_dir():
            files = sorted(p.rglob("*.json"))
        else:
            raise FileNotFoundError(f"Path not found: {path}")

        if not files:
            raise FileNotFoundError(f"No JSON files found in: {path}")

        # Parse date filters
        since_dt = _parse_timestamp(since) if since else None
        until_dt = _parse_timestamp(until) if until else None

        log.info(
            "Loading Discord exports",
            path=str(p),
            files=len(files),
            since=since or "none",
            until=until or "none",
        )

        # Reset dedup set for each load() call
        self._seen_ids = set()

        summary = LoadSummary(
            files_processed=0,
            messages_scanned=0,
            signals_found=0,
            signals_logged=0,
        )

        for i, f in enumerate(files, 1):
            result = self._load_file(f, since_dt=since_dt, until_dt=until_dt)
            if result is not None:
                summary.files_processed += 1
                summary.messages_scanned += result.messages_scanned
                summary.signals_found += result.signals_found
                summary.duplicates_skipped += result.skipped_duplicate
                summary.results.append(result)

            # File-level progress for multi-file loads
            if len(files) > 1 and i % 10 == 0:
                log.info("File progress", processed=i, total=len(files))

        return summary

    def _load_file(
        self,
        path: Path,
        *,
        since_dt: Optional[datetime] = None,
        until_dt: Optional[datetime] = None,
    ) -> Optional[FileResult]:
        """Parse a single DiscordChatExporter JSON file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Skipping file", path=str(path), error=str(e))
            return None

        if not _is_discord_export(data):
            log.debug("Not a DiscordChatExporter file, skipping", path=str(path))
            return None

        guild = _extract_guild_name(data)
        channel_name, channel_type = _extract_channel_info(data)
        messages = data.get("messages", [])

        # Handle empty messages list gracefully
        if not messages:
            log.debug(
                "Export file has no messages, skipping",
                path=str(path),
                guild=guild,
                channel=channel_name,
            )
            return FileResult(
                path=str(path),
                guild=guild,
                channel=channel_name,
                messages_scanned=0,
                signals_found=0,
            )

        is_thread_channel = channel_type in (
            "GuildPublicThread",
            "GuildPrivateThread",
            "PublicThread",
            "PrivateThread",
        )

        result = FileResult(
            path=str(path),
            guild=guild,
            channel=channel_name,
            messages_scanned=0,
            signals_found=0,
        )

        # Collect all processable messages, including thread sub-messages
        flat_messages = self._flatten_messages(messages, is_thread_channel)

        total = len(flat_messages)
        for idx, (msg, is_thread) in enumerate(flat_messages, 1):
            self._process_message(
                msg,
                channel=channel_name,
                guild=guild,
                result=result,
                is_thread=is_thread,
                since_dt=since_dt,
                until_dt=until_dt,
            )

            # Progress dots for large files
            if total >= _PROGRESS_INTERVAL and idx % _PROGRESS_INTERVAL == 0:
                sys.stderr.write(f"\r  {path.name}: {idx:,}/{total:,} messages")
                sys.stderr.flush()

        # Clear progress line if we printed any
        if total >= _PROGRESS_INTERVAL:
            sys.stderr.write(f"\r  {path.name}: {total:,}/{total:,} messages (done)\n")
            sys.stderr.flush()

        log.info(
            "Processed export file",
            guild=guild,
            channel=channel_name,
            messages=result.messages_scanned,
            signals=result.signals_found,
            duplicates=result.skipped_duplicate,
        )

        return result

    @staticmethod
    def _flatten_messages(
        messages: list[dict],
        parent_is_thread: bool,
    ) -> list[tuple[dict, bool]]:
        """Flatten messages and any nested thread messages into a single list.

        Returns a list of (message_dict, is_thread) tuples.
        """
        flat: list[tuple[dict, bool]] = []
        for msg in messages:
            flat.append((msg, parent_is_thread))

            # DiscordChatExporter sometimes nests thread messages under a
            # "thread" key on the parent message.
            thread_data = msg.get("thread")
            if isinstance(thread_data, dict):
                thread_msgs = thread_data.get("messages", [])
                for tmsg in thread_msgs:
                    flat.append((tmsg, True))
            elif isinstance(thread_data, list):
                # Alternate format: thread is directly a list of messages
                for tmsg in thread_data:
                    if isinstance(tmsg, dict):
                        flat.append((tmsg, True))

        return flat

    def _process_message(
        self,
        msg: dict,
        *,
        channel: str,
        guild: str,
        result: FileResult,
        is_thread: bool,
        since_dt: Optional[datetime],
        until_dt: Optional[datetime],
    ) -> None:
        """Process a single message dict, extracting signals into *result*."""
        # Skip non-content message types (pins, joins, boosts, etc.)
        msg_type = msg.get("type")
        if msg_type is not None and msg_type not in _VALID_MSG_TYPES:
            return

        content = msg.get("content", "") or ""
        if not content.strip():
            return

        # Deduplication by message ID
        msg_id = str(msg.get("id", ""))
        if msg_id and msg_id in self._seen_ids:
            result.skipped_duplicate += 1
            return
        if msg_id:
            self._seen_ids.add(msg_id)

        # Date filtering
        timestamp = msg.get("timestamp", "")
        if since_dt or until_dt:
            msg_dt = _parse_timestamp(timestamp)
            if msg_dt:
                if since_dt and msg_dt < since_dt:
                    return
                if until_dt and msg_dt >= until_dt:
                    return

        author = _resolve_author(msg.get("author", {}))

        signals = _parse_message(content, author, channel, guild)
        signals = [s for s in signals if s.score >= self.min_score]

        result.messages_scanned += 1

        if signals:
            result.signals_found += len(signals)
            result.signal_details.append(MessageResult(
                message_id=msg_id,
                timestamp=timestamp,
                author=author,
                channel=channel,
                guild=guild,
                content=content,
                signals=signals,
                is_thread=is_thread,
            ))

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
                        log.warning(
                            "Failed to log signal",
                            ticker=sig.ticker,
                            error=str(e),
                        )
        summary.signals_logged = logged
        log.info("Signals logged to DB", count=logged, db=db_path)
        return logged

    def print_summary(self, summary: LoadSummary) -> None:
        """Print a formatted summary to stdout."""
        w = 62
        print("\n" + "=" * w)
        print("  NEXUS -- Discord Export Signal Summary")
        print("=" * w)
        print(f"  Files processed:   {summary.files_processed}")
        print(f"  Messages scanned:  {summary.messages_scanned:,}")
        print(f"  Signals found:     {summary.signals_found:,}")
        if summary.duplicates_skipped:
            print(f"  Duplicates skipped:{summary.duplicates_skipped:,}")
        if summary.signals_logged:
            print(f"  Signals logged:    {summary.signals_logged:,}  -> nexus.db")

        # Direction breakdown
        dirs = summary.direction_breakdown
        if dirs.get("BUY") or dirs.get("SELL"):
            print(f"\n  Direction:  "
                  f"{dirs.get('BUY', 0)} BUY  /  {dirs.get('SELL', 0)} SELL")

        # Top tickers
        top = summary.top_tickers
        if top:
            print("\n  Top tickers by signal count:")
            for ticker, count in top[:10]:
                bar = "#" * min(count, 20)
                print(f"    {ticker:<7} {count:>4}  {bar}")

        # Top authors
        authors = summary.top_authors
        if authors:
            print("\n  Top signal authors:")
            for author, count in authors:
                print(f"    {author:<20} {count:>4} signals")

        # Per-file breakdown
        if summary.results:
            print(f"\n  {'Guild':<25} {'Channel':<20} {'Msgs':>6} {'Sigs':>5}")
            print(f"  {'-' * (w - 2)}")
            for r in summary.results:
                g = r.guild[:24]
                c = r.channel[:19]
                print(f"  {g:<25} {c:<20} {r.messages_scanned:>6,} "
                      f"{r.signals_found:>5}")

        print("=" * w + "\n")

    def print_signals(self, summary: LoadSummary, limit: int = 50) -> None:
        """Print individual signals with timestamp and context."""
        all_msgs: list[tuple[str, MessageResult]] = []
        for r in summary.results:
            for msg in r.signal_details:
                all_msgs.append((r.channel, msg))

        if not all_msgs:
            print("\n  No signals found.\n")
            return

        # Sort by timestamp
        all_msgs.sort(key=lambda x: x[1].timestamp)

        thread_marker = " [T]"
        print(f"\n  {'Time':<20} {'Author':<18} {'Ticker':<7} {'Dir':<5} "
              f"{'Score':>5}  Content")
        print("  " + "-" * 80)

        shown = 0
        for channel, msg in all_msgs:
            for sig in msg.signals:
                ts = (msg.timestamp or "")[:16].replace("T", " ")
                snippet = msg.content[:45].replace("\n", " ")
                suffix = thread_marker if msg.is_thread else ""
                print(f"  {ts:<20} {msg.author:<18} {sig.ticker:<7} "
                      f"{sig.direction:<5} {sig.score:>5.2f}  {snippet}{suffix}")
                shown += 1
                if shown >= limit:
                    remaining = sum(
                        len(m.signals) for _, m in all_msgs
                    ) - shown
                    if remaining > 0:
                        print(f"\n  ... and {remaining} more signals")
                    return
        print()
