---
name: webhook-clickup-setup
description: >
  Set up, verify, and manage the ClickUp webhook integration that routes task
  events from ClickUp → the agent's webhook_receiver service → clickup_task_handler.
  Supports multiple workspaces — each workspace gets its own endpoint and secret.
  Use this skill when setting up ClickUp webhooks for the first time, adding a new
  workspace, re-creating a webhook after deletion, updating the endpoint after a
  hostname change, rotating a webhook secret, checking webhook health, or tearing
  down the integration.
  Triggers on: "setup clickup webhook", "create clickup webhook", "clickup webhook
  integration", "setup clickup task handler", "clickup events", "add clickup workspace",
  "reconnect clickup webhook", "update clickup webhook", "rotate webhook secret", or
  any request to wire ClickUp task events into the agent.
---

# webhook-clickup-setup

**Handler:** `services/webhook/clickup_task_handler.py`  
**Receiver:** `services/webhook_receiver.py` (port 8082)  
**Path pattern:** `/clickup/<workspace_id>/task` (internal) → `/webhook/clickup/<workspace_id>/task` (external)  
**Secret KeePass key pattern:** `CLICKUP_WEBHOOK_SECRET_<workspace_id>`

Incoming ClickUp task events flow through this pipeline:

```
ClickUp → HTTPS POST → Caddy (:8080/webhook/*) → webhook_receiver (:8082)
       → ClickUpTaskHandler (/clickup/<workspace_id>/task) → inbox.json → agent goal
```

Each workspace has its own URL, HMAC secret, and trigger configuration. The handler
acts only on `taskStatusUpdated` events matching a configured trigger transition.
All other events are acknowledged (HTTP 200) and logged but do not queue a goal.

---

## Prerequisites

| Requirement | Check |
|---|---|
| `clickup` CLI installed & authenticated | `clickup auth whoami` |
| Public hostname configured | `uv run python scripts/portal_config.py hostname --show` |
| `webhook_receiver` service running | `uv run python scripts/service_manager.py status webhook_receiver` |

---

## Setup — New Workspace

Run this for each workspace you want to integrate. Repeat from Step 1 for each.

### Step 1 — Get the workspace ID

```bash
clickup workspace list --output json
# Note the "id" field — e.g. "90182624126"
# Use the ID (not the name — names can change)
WORKSPACE_ID="90182624126"
```

### Step 2 — Derive the webhook endpoint URL

```bash
PUBLIC_URL=$(python3 -c "
import json, pathlib
cfg = pathlib.Path('/agent/memory/portal_config.json')
print(json.loads(cfg.read_text()).get('public_url', '').rstrip('/'))
")
WEBHOOK_ENDPOINT="${PUBLIC_URL}/webhook/clickup/${WORKSPACE_ID}/task"
echo "Endpoint: $WEBHOOK_ENDPOINT"
```

### Step 3 — Check for an existing webhook (avoid duplicates)

```bash
clickup webhook list --output json
```

Look for an entry whose `endpoint` matches `$WEBHOOK_ENDPOINT`. If active, skip to
Step 6. If unhealthy or stale, delete it first:

```bash
clickup webhook delete <WEBHOOK_ID>
```

### Step 4 — Create the webhook

```bash
RESULT=$(clickup webhook create \
  --endpoint "$WEBHOOK_ENDPOINT" \
  --event taskStatusUpdated \
  --event taskCreated \
  --event taskUpdated \
  --event taskDeleted \
  --event taskCommentPosted \
  --event taskCommentUpdated \
  --output json)
echo "$RESULT" | python3 -m json.tool
```

> **Note on scoping**: ClickUp supports `--folder ID`, `--list ID`, `--space ID` to
> scope webhooks to a subset of the workspace — but these require a Business plan or
> higher. On Free/Unlimited plans, omit scope flags for workspace-level delivery.

Extract the secret:
```bash
SECRET=$(echo "$RESULT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
entry = d[0] if isinstance(d, list) else d
print(entry.get('secret') or entry.get('webhook', {}).get('secret', ''))
")
echo "Secret: $SECRET"
```

### Step 5 — Store the secret in KeePass

Each workspace gets its own KeePass entry named `CLICKUP_WEBHOOK_SECRET_<workspace_id>`:

```bash
uv run python scripts/keepass.py store \
  --title "CLICKUP_WEBHOOK_SECRET_${WORKSPACE_ID}" \
  --username "clickup" \
  --password "$SECRET" \
  --group "System"
```

### Step 6 — Add workspace config to state file

Edit `/agent/memory/clickup_task_handler_state.json` to add an entry for the workspace.
The file is a JSON object keyed by workspace_id:

```json
{
  "90182624126": {
    "trigger_transitions": [
      {"from": "in progress", "to": "in review"},
      {"from": "in progress", "to": "complete"}
    ],
    "review_prompt_path": null
  }
}
```

To add a second workspace, simply add another key:

```json
{
  "90182624126": { "trigger_transitions": [...], "review_prompt_path": null },
  "99999999999": { "trigger_transitions": [...], "review_prompt_path": null }
}
```

> Trigger rules are re-read on every incoming request — no restart needed after
> editing the state file.

### Step 7 — Restart webhook_receiver to load the new secret

Secrets are loaded from KeePass once at startup (then cached in memory). Restart to
pick up a new secret for a newly added workspace:

```bash
uv run python scripts/service_manager.py restart webhook_receiver
```

Confirm the workspace was picked up (look for signature verification enabled):

```bash
tail -20 /agent/memory/logs/service-webhook_receiver.stdout.log
```

Expected log lines:
```
[INFO] Starting ClickUp task handler (multi-workspace)...
[INFO]   Workspace 90182624126: 2 trigger(s), signature verification enabled
[INFO] ClickUp task handler ready — path pattern: /webhook/clickup/<workspace_id>/task
[INFO] Registered sub-handler ClickUpTaskHandler on /clickup
[INFO] Ready. Listening for webhooks on port 8082...
```

---

## Verification

```bash
# Health check
curl -s http://localhost:8082/health

# List webhooks in ClickUp — confirm status=active, fail_count=0
clickup webhook list --output json

# Test: send a mock event (no HMAC — accepted for unknown/unsigned workspaces)
curl -s -X POST http://localhost:8082/clickup/90182624126/task \
  -H "Content-Type: application/json" \
  -d '{"event":"taskCreated","task_id":"test123"}'
# Expected: {"status":"ok"}

# Test: path mismatch → 404
curl -s http://localhost:8082/clickup/task
# Expected: {"status":"not_found"}

# Confirm KeePass entry
uv run python scripts/keepass.py get "CLICKUP_WEBHOOK_SECRET_90182624126"
```

---

## Service Management

```bash
# Start if not running
uv run python scripts/service_manager.py start webhook_receiver \
  --auto-start -- uv run python services/webhook_receiver.py

# Restart (e.g. after adding a new workspace/secret)
uv run python scripts/service_manager.py restart webhook_receiver

# Live logs
tail -f /agent/memory/logs/service-webhook_receiver.stdout.log
```

---

## Trigger Configuration

Per-workspace triggers live in `/agent/memory/clickup_task_handler_state.json`.
Re-read on every request — no restart required after edits.

Default transitions for every workspace:

| From | To |
|---|---|
| `in progress` | `in review` |
| `in progress` | `complete` |

To add a transition for workspace `90182624126`:

```json
{
  "90182624126": {
    "trigger_transitions": [
      {"from": "in progress", "to": "in review"},
      {"from": "in progress", "to": "complete"},
      {"from": "in review",   "to": "complete"}
    ],
    "review_prompt_path": null
  }
}
```

Set `review_prompt_path` to a file path (e.g. `"/agent/prompts/review_guide.md"`) to
inject custom review instructions into the queued goal.

---

## Updating After a Hostname Change

When the public hostname changes (e.g. new Cloudflare Tunnel URL), each workspace
webhook must be recreated:

```bash
# For each workspace:
clickup webhook list -q                            # get webhook IDs
clickup webhook delete <OLD_ID>                    # delete old
# Then re-run Steps 2–7 with the new hostname
```

The new webhook generates a new secret — always store it in KeePass and restart the receiver.

---

## Rotating a Webhook Secret

ClickUp does not support in-place secret rotation. To rotate for a workspace:

```bash
WORKSPACE_ID="90182624126"
# 1. Find and delete the existing webhook
clickup webhook list --output json      # find the ID for this workspace endpoint
clickup webhook delete <ID>
# 2. Re-create (generates new secret) — Steps 4–5 above
# 3. Restart receiver
uv run python scripts/service_manager.py restart webhook_receiver
```

---

## Teardown (Single Workspace)

```bash
WORKSPACE_ID="90182624126"
# 1. Delete ClickUp webhook
clickup webhook list --output json      # find ID
clickup webhook delete <ID>
# 2. Remove secret from KeePass
uv run python scripts/keepass.py delete "CLICKUP_WEBHOOK_SECRET_${WORKSPACE_ID}"
# 3. Remove entry from state file
#    Edit /agent/memory/clickup_task_handler_state.json — delete the workspace key
# 4. Restart receiver
uv run python scripts/service_manager.py restart webhook_receiver
```

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `401 Team(s) not authorized` on `--folder` flag | Plan doesn't support scoped webhooks | Remove `--folder`; use workspace-level |
| `{"status":"not_found"}` on POST | Path doesn't match `/clickup/<digits>/task` | Check URL — workspace_id must be numeric |
| `signature verification DISABLED` in logs | Secret not in KeePass for that workspace | Run Step 5 for the workspace |
| `401 unauthorized` responses | Wrong secret stored | Delete webhook, re-create, store new secret |
| No events arriving | Wrong endpoint URL in ClickUp | `clickup webhook list` to check endpoint |
| `fail_count > 0` in webhook health | Handler returned non-2xx | Check `webhook_receiver` stdout logs |
| Handler running but no goals queued | Transition not configured | Check state file for workspace entry |
| New workspace secret not loaded | Receiver not restarted after adding secret | Restart `webhook_receiver` |

---

## Quick Reference — Full Setup for One Workspace

```bash
WORKSPACE_ID="90182624126"
PUBLIC_URL=$(python3 -c "import json,pathlib; print(json.loads(pathlib.Path('/agent/memory/portal_config.json').read_text()).get('public_url','').rstrip('/'))")

# Create webhook
RESULT=$(clickup webhook create \
  --endpoint "${PUBLIC_URL}/webhook/clickup/${WORKSPACE_ID}/task" \
  --event taskStatusUpdated --event taskCreated --event taskUpdated \
  --event taskDeleted --event taskCommentPosted --event taskCommentUpdated \
  --output json)
echo "$RESULT" | python3 -m json.tool

# Extract + store secret
SECRET=$(echo "$RESULT" | python3 -c "
import sys, json; d=json.load(sys.stdin)
e = d[0] if isinstance(d,list) else d
print(e.get('secret') or e.get('webhook',{}).get('secret',''))
")
uv run python scripts/keepass.py store \
  --title "CLICKUP_WEBHOOK_SECRET_${WORKSPACE_ID}" \
  --username "clickup" --password "$SECRET" --group "System"

# Restart receiver
uv run python scripts/service_manager.py restart webhook_receiver
sleep 8 && tail -10 /agent/memory/logs/service-webhook_receiver.stdout.log
clickup webhook list
```
