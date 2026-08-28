---
name: telegram-bridge-setup
description: Set up and manage the Telegram bridge service that connects the agent's inbox/outbox to a Telegram bot. Use this skill when setting up the Telegram bot for the first time, storing or updating the bot token or owner username in KeePass, starting or restarting the bridge after it stopped, or troubleshooting a non-responsive Telegram bot or missing chat ID.
---

# telegram-bridge-setup

**Service:** `services/telegram_bridge.py`

Long-running bridge that polls Telegram every ~5s for incoming messages (saved to `/agent/messages/inbox.json`) and forwards outbox messages to Telegram every 60s. Chat IDs are auto-discovered when the user first messages the bot.

## Prerequisites

### 1. Create a Telegram bot via @BotFather

If no bot token exists yet:

1. Open Telegram and start a conversation with [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts (choose a display name and a `@username`)
3. Copy the token BotFather provides — it looks like `123456789:ABCdef...`

### 2. Store the bot token in KeePass

```bash
uv run python scripts/keepass.py store \
  --title "TELEGRAM_BOT_TOKEN" \
  --username "bot" \
  --password "<your-bot-token>" \
  --group "API Keys"
```

> `TELEGRAM_CHAT_ID` and `TELEGRAM_OWNER_USERNAME` are auto-populated by the bridge when you send your first message — do not store them manually unless migrating an existing setup.

## Setup Steps

### 1. Verify the token is stored

```bash
uv run python scripts/keepass.py get "TELEGRAM_BOT_TOKEN"
```

### 2. Start the service

```bash
uv run python scripts/service_manager.py start telegram_bridge -- uv run python services/telegram_bridge.py
```

To have the bridge auto-restart on every heartbeat cycle, add `--auto-start`:

```bash
uv run python scripts/service_manager.py start telegram_bridge --auto-start -- uv run python services/telegram_bridge.py
```

### 3. Send the first message to auto-discover your chat ID

Open Telegram, find your bot by its `@username`, and send any message (e.g., `/heartbeat`).

The bridge detects the incoming message, extracts your chat ID, and writes it to KeePass automatically. Confirm it was saved:

```bash
uv run python scripts/keepass.py get "TELEGRAM_CHAT_ID"
```

## Verification

```bash
# Check the service is running
uv run python scripts/service_manager.py status telegram_bridge

# Confirm the inbox received the first message
cat /agent/messages/inbox.json

# Watch live logs
tail -f /agent/memory/logs/service-telegram_bridge.stdout.log
tail -f /agent/memory/logs/service-telegram_bridge.stderr.log
```

Send `/heartbeat` to the bot — it should reply with the last heartbeat timestamp. Send `/status` to get a full service status report.

## Management

```bash
# Start
uv run python scripts/service_manager.py start telegram_bridge -- uv run python services/telegram_bridge.py

# Stop
uv run python scripts/service_manager.py stop telegram_bridge

# Restart
uv run python scripts/service_manager.py restart telegram_bridge

# Status
uv run python scripts/service_manager.py status telegram_bridge

# Logs
tail -f /agent/memory/logs/service-telegram_bridge.stdout.log
tail -f /agent/memory/logs/service-telegram_bridge.stderr.log

# Bridge's own combined log
tail -f /agent/memory/logs/telegram_bridge.log
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/heartbeat` | Reply with last heartbeat timestamp |
| `/status` | Show all service statuses |
| `/goals` | Show current goals |
| `/cycles` | Show recent cycle summaries |
| `/journal` | Show recent journal entries |
| `/today` | Show today's summary |
| `/outbox` | Show pending outbox messages |
| `/services` | List managed services |
| `/remind <text>` | Create a reminder |
| `/note <text>` | Save a quick note |
| `/notes` | List saved notes |
| `/help` | Show available commands |

## Troubleshooting

**Bot does not reply to `/heartbeat`:**
- Check the service is running: `uv run python scripts/service_manager.py status telegram_bridge`
- Check for errors: `tail -50 /agent/memory/logs/service-telegram_bridge.stderr.log`
- Confirm the token is valid: `uv run python scripts/keepass.py get "TELEGRAM_BOT_TOKEN"`
- Ensure no other process is consuming the same bot's `getUpdates` long-poll (only one bridge per token)

**Chat ID was not auto-discovered:**
- Confirm you messaged the correct bot (its `@username` must match the one BotFather assigned)
- Check the logs for `"New chat_id discovered"` or any KeePass write error
- Manually store the chat ID if needed:
  ```bash
  uv run python scripts/keepass.py store \
    --title "TELEGRAM_CHAT_ID" \
    --username "chat_id" \
    --password "<chat_id>" \
    --group "API Keys"
  ```
  Then restart: `uv run python scripts/service_manager.py restart telegram_bridge`

**Multiple users or group chats:**
- The bridge supports comma-separated chat IDs in `TELEGRAM_CHAT_ID`
- Additional IDs are appended automatically when new users message the bot
- To add one manually, update the KeePass entry with a comma-separated list: `"123456789,987654321"`

**Service crashes on startup:**
- Confirm `TELEGRAM_BOT_TOKEN` exists in KeePass before starting the service
- Check Python dependencies: `uv run python -c "import requests"`
- Review full startup errors: `tail -100 /agent/memory/logs/service-telegram_bridge.stderr.log`

**Outbox messages not being sent:**
- Verify `TELEGRAM_CHAT_ID` is populated — the bridge needs a known chat ID to send
- Outbox is checked every 60s; wait one full cycle after the chat ID is discovered
- Inspect outbox contents: `cat /agent/messages/outbox.json`
