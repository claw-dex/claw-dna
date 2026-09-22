---
name: whatsapp-bridge-setup
description: Set up and manage the WhatsApp bridge that connects the agent's inbox/outbox to the WhatsApp Business Cloud API via webhook. Use this skill when setting up WhatsApp messaging for the first time, storing or updating Meta API credentials in KeePass, configuring the webhook URL in the Meta Developer Portal, or troubleshooting a non-responsive WhatsApp bot or missing chat ID.
---

# whatsapp-bridge-setup

**Handler:** `services/webhook/whatsapp_bridge_handler.py`
**Host service:** `services/webhook_receiver.py` (port 8082)
**Webhook path:** `/whatsapp-bridge` (internal) → `/webhook/whatsapp-bridge` (external, after Caddy's `/webhook/*` route strips the `/webhook` prefix)

The WhatsApp bridge runs as a sub-handler **inside** `webhook_receiver`. It owns both GET (Meta verification challenge) and POST (incoming messages) to its path, writes incoming messages to `/agent/messages/inbox.json`, and polls `/agent/messages/outbox.json` every 60s to forward unsent messages to all authorized WhatsApp chats. There is **no separate service, no separate port, and no dynamic Caddy registration** — the existing `/webhook/*` → `localhost:8082` route in the Caddyfile covers it. Phone numbers and owner name are auto-discovered on the first message.

If the three required KeePass credentials are missing, the handler logs a warning and self-disables, but `webhook_receiver` keeps serving all other webhook paths.

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

> `WHATSAPP_CHAT_ID` and `WHATSAPP_OWNER_USERNAME` are auto-populated on the first message — do not store them manually unless migrating an existing setup.

## Setup Steps

### 1. Verify credentials are stored

```bash
uv run python scripts/keepass.py get "WHATSAPP_ACCESS_TOKEN"
uv run python scripts/keepass.py get "WHATSAPP_PHONE_NUMBER_ID"
uv run python scripts/keepass.py get "WHATSAPP_VERIFY_TOKEN"
```

### 2. Ensure webhook_receiver is running and pick up the new handler

```bash
uv run python scripts/service_manager.py status webhook_receiver
# If not running:
uv run python scripts/service_manager.py start webhook_receiver 8082 -- uv run python services/webhook_receiver.py
# If already running, restart so the handler reloads credentials from KeePass:
uv run python scripts/service_manager.py restart webhook_receiver
```

On startup the receiver logs one of:
- `Registered sub-handler WhatsAppBridgeHandler on /whatsapp-bridge` — handler active
- `WhatsApp handler disabled — missing KeePass credentials: …` — store the missing credentials and restart

### 3. Configure the webhook in the Meta Developer Portal

In the Meta Developer Portal, go to **WhatsApp → Configuration → Webhook** and set:

- **Callback URL:** `https://<your-public-domain>/webhook/whatsapp-bridge`
- **Verify Token:** the value you stored as `WHATSAPP_VERIFY_TOKEN`

Subscribe to the **messages** field under Webhook Fields.

### 4. Send the first WhatsApp message to auto-discover your phone number

Send any message to your business number from your personal WhatsApp. The handler detects the incoming message, extracts your phone number and name, and writes them to KeePass automatically. Confirm they were saved:

```bash
uv run python scripts/keepass.py get "WHATSAPP_CHAT_ID"
```

## Verification

```bash
# Host service is running
uv run python scripts/service_manager.py status webhook_receiver

# Handler registered successfully (look for "Registered sub-handler WhatsAppBridgeHandler")
grep -i whatsapp /agent/memory/logs/webhook_receiver.log | tail -20

# Confirm the inbox received the first message
cat /agent/messages/inbox.json

# Smoke-test the verification endpoint locally (challenge echo)
curl -i "http://localhost:8082/whatsapp-bridge?hub.mode=subscribe&hub.verify_token=<VERIFY_TOKEN>&hub.challenge=abc123"
# Expect: 200 OK with body "abc123"
```

Send `/heartbeat` to the business number from your personal WhatsApp — it should reply with the last heartbeat timestamp.

## Management

The bridge is managed through the `webhook_receiver` host service, not as a separate service.

```bash
# Restart (reloads handler credentials from KeePass)
uv run python scripts/service_manager.py restart webhook_receiver

# Stop (also stops the handler's outbox polling thread)
uv run python scripts/service_manager.py stop webhook_receiver

# Status
uv run python scripts/service_manager.py status webhook_receiver

# Logs (handler shares webhook_receiver's log file)
tail -f /agent/memory/logs/webhook_receiver.log
tail -f /agent/memory/logs/service-webhook_receiver.stdout.log
tail -f /agent/memory/logs/service-webhook_receiver.stderr.log
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/heartbeat` | Reply with last heartbeat timestamp |

## Troubleshooting

**Bot does not reply to `/heartbeat`:**
- Confirm webhook_receiver is running: `uv run python scripts/service_manager.py status webhook_receiver`
- Confirm the handler registered on startup: `grep -i "sub-handler WhatsAppBridgeHandler" /agent/memory/logs/webhook_receiver.log`
- Check for handler errors: `grep -i whatsapp /agent/memory/logs/webhook_receiver.log | tail -50`
- Confirm credentials are valid: `uv run python scripts/keepass.py get "WHATSAPP_ACCESS_TOKEN"`

**Handler disabled at startup:**
- The log shows `WhatsApp handler disabled — missing KeePass credentials: …`. Store any missing credentials (see Prerequisites) and run `restart webhook_receiver`.

**Webhook verification fails in Meta Developer Portal:**
- Confirm the webhook URL is `https://<domain>/webhook/whatsapp-bridge` (not the old `/system/whatsapp-bridge/webhook`)
- Confirm the verify token in KeePass matches exactly what you entered in the portal
- Test Caddy → webhook_receiver end-to-end: `curl -s "https://<domain>/webhook/whatsapp-bridge?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=test"` (expect body `test`)

**Chat ID was not auto-discovered:**
- Confirm you messaged the correct business number
- Check logs for auto-discovery or any KeePass write error
- Manually store the phone number if needed (E.164 format, e.g. `15551234567`):
  ```bash
  uv run python scripts/keepass.py store \
    --title "WHATSAPP_CHAT_ID" \
    --username "whatsapp" \
    --password "<phone_number>" \
    --group "System"
  ```
  Then: `uv run python scripts/service_manager.py restart webhook_receiver`

**Multiple phone numbers:**
- The bridge supports comma-separated phone numbers in `WHATSAPP_CHAT_ID`
- Additional numbers are appended automatically when new users message the bot and mention the owner's name

**Outbox messages not being sent:**
- Verify `WHATSAPP_CHAT_ID` is populated — the handler needs a known phone number to send
- Outbox is polled every 60s from a daemon thread inside webhook_receiver; wait one full cycle after the phone number is discovered
- Inspect outbox contents: `cat /agent/messages/outbox.json`
