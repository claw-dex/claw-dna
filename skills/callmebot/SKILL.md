---
name: callmebot
description: Voice call escalation via CallMeBot Telegram API. Use this skill when the agent has sent a needs_human outbox message but the human hasn't responded — it makes a voice call to get their attention. Also use for initial setup (configuring the CallMeBot user credential) or checking if callmebot is ready. Trigger this skill whenever you see unanswered needs_human messages in the outbox, when the user asks about voice call escalation, or when setting up CallMeBot for the first time.
---

# callmebot

**Path:** `scripts/callmebot.py`

Makes voice calls to the agent owner via the free [CallMeBot Telegram Call API](https://www.callmebot.com/telegram-call-api/). The primary use case is escalating unanswered `needs_human` outbox messages — if the human hasn't responded via Telegram, a voice call gets their attention.

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `setup [--lang L]` | Verify config and optionally set voice language |
| `call --text "message"` | Make a voice call (rate-limited to 1 call per hour) |
| `status` | Check if CallMeBot is configured and ready to call |

## Flags

| Flag | Description |
|------|-------------|
| `--json` | Output as JSON |
| `--lang L` | Voice language (default: en-US-Standard-B), used with `setup` |
| `--text T` | Message to speak, max 256 chars, used with `call` |

## Setup Prerequisites

CallMeBot uses the same `TELEGRAM_OWNER_USERNAME` KeePass entry as `telegram_bridge.py` — no separate user credential is needed.

Before CallMeBot can reach a user, they must authorize it:

1. The user sends `/start` to [@CallMeBot_txtbot](https://t.me/CallMeBot_txtbot) on Telegram
   - Alternatively, visit: https://api2.callmebot.com/txt/login.php
2. Then verify the setup:
   ```bash
   uv run python scripts/callmebot.py setup
   ```

## Escalation Logic

Use this skill to escalate when a `needs_human` message hasn't been answered. The conditions for escalation:

1. `/agent/messages/outbox.json` contains at least one message with `"type": "needs_human"` from a previous cycle
2. `/agent/messages/inbox.json` is empty (the human hasn't responded)
3. `callmebot.py --json status` shows `"can_call_now": true`

When all conditions are met, make the call:

```bash
uv run python scripts/callmebot.py call --text "<summary of what's blocked>"
```

The call text should be derived from the `needs_human` message's `subject` field — keep it concise and actionable so the human knows what to do when they pick up. After calling, close the cycle normally — don't retry the blocked work.

If `can_call_now` is false (rate-limited), skip the call and close the cycle. The next cycle will try again once the cooldown expires.

## Examples

```bash
# Check if CallMeBot is configured
uv run python scripts/callmebot.py status

# Verify setup (uses TELEGRAM_OWNER_USERNAME from KeePass)
uv run python scripts/callmebot.py setup

# Set a custom voice language
uv run python scripts/callmebot.py setup --lang en-GB-Standard-B

# Make a voice call
uv run python scripts/callmebot.py call --text "I need your API key to continue the deployment"

# Check status as JSON (useful for programmatic checks)
uv run python scripts/callmebot.py --json status
```
