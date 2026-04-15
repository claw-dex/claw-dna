---
name: whatsapp-bridge-setup
description: Set up and manage the WhatsApp bridge service that connects the agent's inbox/outbox to WhatsApp Business Cloud API via webhook. Use this skill when setting up WhatsApp messaging for the first time, storing or updating Meta API credentials in KeePass, starting or restarting the bridge, configuring the webhook URL in the Meta Developer Portal, or troubleshooting a non-responsive WhatsApp bot or missing chat ID.
---

# whatsapp-bridge-setup

**Service:** `services/whatsapp_bridge.py`

Long-running bridge that runs an HTTP webhook server on port 8083 to receive incoming WhatsApp messages (saved to `/agent/messages/inbox.json`) and forwards outbox messages to WhatsApp every 60s. The Caddy reverse proxy route is registered/deregistered automatically by the bridge. Phone numbers and owner name are auto-discovered when the first message is received.

## Prerequisites

### 1. Create a Meta Developer app with WhatsApp Business Cloud API

If no Meta app exists yet:

1. Go to [developers.facebook.com](https://developers.facebook.com) and create a new app (type: **Business**)
2. Add the **WhatsApp** product to the app
3. From the WhatsApp → API Setup page, collect:
   - **Access Token** — a temporary or permanent token for the Graph API
   - **Phone Number ID** — the numeric ID of the business phone number (not the actual phone number)
4. Choose a **Verify Token** — any secret string you pick; you will enter it in the Meta portal when configuring the webhook

### 2. Store credentials in KeePass

```bash
uv run python scripts/keepass.py store \
  --title "WHATSAPP_ACCESS_TOKEN" \
  --username "whatsapp" \
  --password "<your-access-token>" \
  --group "API Keys"

uv run python scripts/keepass.py store \
  --title "WHATSAPP_PHONE_NUMBER_ID" \
  --username "whatsapp" \
  --password "<your-phone-number-id>" \
  --group "API Keys"

uv run python scripts/keepass.py store \
  --title "WHATSAPP_VERIFY_TOKEN" \
  --username "whatsapp" \
  --password "<your-verify-token>" \
  --group "API Keys"
```

> `WHATSAPP_CHAT_ID` and `WHATSAPP_OWNER_USERNAME` are auto-populated by the bridge when you send your first message — do not store them manually unless migrating an existing setup.

## Setup Steps

### 1. Verify credentials are stored

```bash
uv run python scripts/keepass.py get "WHATSAPP_ACCESS_TOKEN"
uv run python scripts/keepass.py get "WHATSAPP_PHONE_NUMBER_ID"
uv run python scripts/keepass.py get "WHATSAPP_VERIFY_TOKEN"
```

### 2. Start the service

```bash
uv run python scripts/service_manager.py start whatsapp_bridge 8083 -- uv run python services/whatsapp_bridge.py
```

To have the bridge auto-restart on every heartbeat cycle, add `--auto-start`:

```bash
uv run python scripts/service_manager.py start whatsapp_bridge 8083 --auto-start -- uv run python services/whatsapp_bridge.py
```

The bridge automatically registers a Caddy reverse proxy route for the webhook path on startup.

### 3. Configure the webhook in Meta Developer Portal

In the Meta Developer Portal, go to **WhatsApp → Configuration → Webhook** and set:

- **Callback URL:** `https://<your-public-domain>/system/whatsapp-bridge/webhook`
- **Verify Token:** the value you stored as `WHATSAPP_VERIFY_TOKEN`

Subscribe to the **messages** field under Webhook Fields.

### 4. Send the first WhatsApp message to auto-discover your phone number

Send any message to your business number from your personal WhatsApp. The bridge detects the incoming message, extracts your phone number and name, and writes them to KeePass automatically. Confirm they were saved:

```bash
uv run python scripts/keepass.py get "WHATSAPP_CHAT_ID"
```

## Verification

```bash
# Check the service is running
uv run python scripts/service_manager.py status whatsapp_bridge

# Confirm the inbox received the first message
cat /agent/messages/inbox.json

# Watch live logs
tail -f /agent/memory/logs/service-whatsapp_bridge.stdout.log
tail -f /agent/memory/logs/service-whatsapp_bridge.stderr.log
```

Send `/heartbeat` to the business number from your personal WhatsApp — it should reply with the last heartbeat timestamp.

## Management

```bash
# Start (port 8083 is required)
uv run python scripts/service_manager.py start whatsapp_bridge 8083 -- uv run python services/whatsapp_bridge.py

# Stop (also deregisters the Caddy webhook route)
uv run python scripts/service_manager.py stop whatsapp_bridge

# Restart
uv run python scripts/service_manager.py restart whatsapp_bridge

# Status
uv run python scripts/service_manager.py status whatsapp_bridge

# Logs
tail -f /agent/memory/logs/service-whatsapp_bridge.stdout.log
tail -f /agent/memory/logs/service-whatsapp_bridge.stderr.log

# Bridge's own combined log
tail -f /agent/memory/logs/whatsapp_bridge.log
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/heartbeat` | Reply with last heartbeat timestamp |

## Troubleshooting

**Bot does not reply to `/heartbeat`:**
- Check the service is running: `uv run python scripts/service_manager.py status whatsapp_bridge`
- Check for errors: `tail -50 /agent/memory/logs/service-whatsapp_bridge.stderr.log`
- Confirm credentials are valid: `uv run python scripts/keepass.py get "WHATSAPP_ACCESS_TOKEN"`
- Verify the Caddy route was registered: `curl -s http://localhost:2019/config/apps/http/servers/srv0/routes | python3 -m json.tool | grep whatsapp`

**Webhook verification fails in Meta Developer Portal:**
- Confirm the service is running and reachable at `https://<domain>/system/whatsapp-bridge/webhook`
- Confirm the verify token in KeePass matches exactly what you entered in the portal
- Check Caddy is proxying correctly: `curl -s https://<domain>/system/whatsapp-bridge/webhook?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=test`

**Chat ID was not auto-discovered:**
- Confirm you messaged the correct business number
- Check logs for `"New chat_id discovered"` or any KeePass write error
- Manually store the phone number if needed (E.164 format, e.g. `15551234567`):
  ```bash
  uv run python scripts/keepass.py store \
    --title "WHATSAPP_CHAT_ID" \
    --username "whatsapp" \
    --password "<phone_number>" \
    --group "System"
  ```
  Then restart: `uv run python scripts/service_manager.py restart whatsapp_bridge`

**Multiple phone numbers:**
- The bridge supports comma-separated phone numbers in `WHATSAPP_CHAT_ID`
- Additional numbers are appended automatically when new users message the bot

**Service crashes on startup:**
- Confirm all three KeePass credentials exist before starting
- Confirm Caddy is running (the bridge registers a route on port 2019)
- Review startup errors: `tail -100 /agent/memory/logs/service-whatsapp_bridge.stderr.log`

**Outbox messages not being sent:**
- Verify `WHATSAPP_CHAT_ID` is populated — the bridge needs a known phone number to send
- Outbox is checked every 60s; wait one full cycle after the phone number is discovered
- Inspect outbox contents: `cat /agent/messages/outbox.json`
