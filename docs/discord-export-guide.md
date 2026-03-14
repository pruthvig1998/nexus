# Discord Export Guide — Loading Channel Data into NEXUS

This guide walks you through exporting Discord channel history using
[DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter) and
loading it into NEXUS for signal extraction and backtesting.

---

## Prerequisites

- You are a member of the Discord servers/channels you want to export
- macOS, Windows, or Linux
- NEXUS installed (`pip install -e .` from the repo root)

---

## Step 1 — Get Your Discord User Token

> ⚠️ Your user token is like a password. Never share it with anyone.
> Never commit it to git. Store it only in your `.env` file.

1. Open Discord at **discord.com** in Chrome or Firefox (use the browser, not the app)
2. Press **F12** on Windows/Linux, or **Cmd + Option + J** on Mac to open DevTools
3. Click the **Network** tab
4. Press **Ctrl+R** (or **Cmd+R** on Mac) to reload the page
5. In the filter box, type `science`
6. Click any request that appears → **Headers** tab → scroll to **Request Headers**
7. Find `Authorization:` — copy that value (it starts with your user ID)

> If no `science` requests appear, type `messages` in the filter instead —
> any Discord API request will contain the Authorization header.

---

## Step 2 — Download DiscordChatExporter CLI

Go to the releases page:
**https://github.com/Tyrrrz/DiscordChatExporter/releases/latest**

Download the correct version for your OS:

| OS | File to download |
|----|-----------------|
| Mac (M1/M2/M3) | `DiscordChatExporter.Cli.osx-arm64.zip` |
| Mac (Intel) | `DiscordChatExporter.Cli.osx-x64.zip` |
| Windows 64-bit | `DiscordChatExporter.Cli.win-x64.zip` |
| Linux x64 | `DiscordChatExporter.Cli.linux-x64.zip` |

**Not sure which Mac chip you have?** Click the Apple menu → About This Mac:
- "Apple M1 / M2 / M3" → download `osx-arm64`
- "Intel" → download `osx-x64`

---

## Step 3 — Set Up the CLI (Mac/Linux)

```bash
# Unzip into your Downloads folder
cd ~/Downloads
unzip DiscordChatExporter.Cli.osx-arm64.zip -d DiscordChatExporter
cd DiscordChatExporter

# Remove macOS quarantine flag and make executable
xattr -cr ./DiscordChatExporter.Cli
chmod +x ./DiscordChatExporter.Cli
```

---

## Step 4 — Find Your Server and Channel IDs

**Enable Developer Mode in Discord:**
1. Discord → Settings (gear icon) → Advanced
2. Toggle on **Developer Mode**
3. Now right-click any server icon → **Copy Server ID**
4. Right-click any channel → **Copy Channel ID**

**Or list them via CLI:**

```bash
# List all servers you're in
./DiscordChatExporter.Cli guilds -t YOUR_USER_TOKEN

# List all channels in a specific server
./DiscordChatExporter.Cli channels -t YOUR_USER_TOKEN -g SERVER_ID
```

---

## Step 5 — Export Channels

**Export a single channel:**
```bash
./DiscordChatExporter.Cli export \
  -t YOUR_USER_TOKEN \
  -c CHANNEL_ID \
  -f Json \
  -o ~/Desktop/discord-exports/
```

**Export an entire server at once:**
```bash
./DiscordChatExporter.Cli exportguild \
  -t YOUR_USER_TOKEN \
  -g SERVER_ID \
  -f Json \
  -o ~/Desktop/discord-exports/
```

**Export multiple specific channels:**
```bash
./DiscordChatExporter.Cli export \
  -t YOUR_USER_TOKEN \
  -c CHANNEL_ID_1 \
  -c CHANNEL_ID_2 \
  -c CHANNEL_ID_3 \
  -f Json \
  -o ~/Desktop/discord-exports/
```

**Export only recent messages (last 90 days):**
```bash
./DiscordChatExporter.Cli export \
  -t YOUR_USER_TOKEN \
  -c CHANNEL_ID \
  -f Json \
  --after 2024-01-01 \
  -o ~/Desktop/discord-exports/
```

Each channel is saved as a separate `.json` file in your output folder.

---

## Step 6 — Load Into NEXUS

Once you have your export files, load them into NEXUS:

```bash
# Load all exports from a directory (logs signals to nexus.db)
nexus load-discord ~/Desktop/discord-exports/

# Load a single file
nexus load-discord ~/Desktop/discord-exports/MyServer - trading-signals.json

# Preview signals without writing to DB
nexus load-discord ~/Desktop/discord-exports/ --no-db

# Lower the signal score threshold to catch more mentions
nexus load-discord ~/Desktop/discord-exports/ --min-score 0.55

# Show individual signal details
nexus load-discord ~/Desktop/discord-exports/ --show-signals

# Show up to 100 signals
nexus load-discord ~/Desktop/discord-exports/ --show-signals --limit 100
```

**Example output:**
```
══════════════════════════════════════════════════════════════
  NEXUS — Discord Export Signal Summary
══════════════════════════════════════════════════════════════
  Files processed:   8
  Messages scanned:  12,847
  Signals found:     234
  Signals logged:    234  → nexus.db

  Direction:  189 BUY  /  45 SELL

  Top tickers by signal count:
    AAPL      42  ██████████████████████
    NVDA      38  ████████████████████
    TSLA      31  █████████████████
    MSFT      24  █████████████
    AMD       19  ██████████

  Top signal authors:
    trader_joe           67 signals
    options_hawk         45 signals
    bull_market_mike     31 signals
══════════════════════════════════════════════════════════════
```

---

## Step 7 — View Signals in NEXUS

```bash
# See recently loaded Discord signals
nexus signals --limit 50
```

Signals loaded from Discord exports appear with `strategy = discord` and
include the author name and channel in the reasoning field.

---

## Tips

- **Export JSON format** (not HTML or CSV) — NEXUS only parses JSON exports
- **Channel names** with trading-related names (`signals`, `alerts`, `picks`,
  `calls`, `flow`) tend to have the highest signal density
- **Lower `--min-score`** to 0.55 if you want to catch more mentions;
  raise to 0.65+ for higher-confidence signals only
- Run `nexus load-discord` periodically after re-exporting to keep your
  signal history up to date
- Combine with the live `--discord` flag to get both historical context
  and real-time signals simultaneously

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `"is damaged and can't be opened"` | Run `xattr -cr ./DiscordChatExporter.Cli` in Terminal |
| `Unauthorized` from CLI | User token expired — re-fetch it from browser DevTools |
| `No JSON files found` | Check the path; make sure you exported with `-f Json` |
| `No trading signals found` | Lower `--min-score` or check that channels contain trading discussion |
| `Missing fields` parse errors | File may be from an older DiscordChatExporter version; try re-exporting |
