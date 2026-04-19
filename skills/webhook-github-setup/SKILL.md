---
name: webhook-github-setup
description: Set up and configure GitHub Organization-level webhooks that feed the agent's inbox. Built around a per-event handler pattern with three implementations today — `pull_request` (triggers automated PR reviews), `pull_request_review_comment` and `pull_request_review_thread` (both informative-only). Registers the org in the relevant handler state file, stores the HMAC secret in KeePass, creates any required review guideline prompt, and starts the webhook_receiver service. Use when wiring a new GitHub org to the agent, adding a new org to an existing setup, enabling an additional event type, or verifying an existing org is correctly configured. Triggers on "set up github webhook", "configure github org webhook", "add github org for PR review", "wire github org to agent", "add pull_request_review_comment webhook", "add pull_request_review_thread webhook".
---

# webhook-github-setup

Sets up GitHub Organization-level webhooks that drive agent behavior. The skill is
built around a **per-event handler** pattern: each GitHub event type is served by
its own handler module mounted at a predictable URL, sharing the same org config,
HMAC secret, and receiver service.

**Today's implementations:**

| GitHub event | URL path (after Caddy strips `/webhook`) | Handler module | Inbox output |
|---|---|---|---|
| `pull_request` | `/github/<org>/pull_request` | `github_pull_request_handler.py` | review goal → `/review-ghpr` (see gating rules below) |
| `pull_request_review_comment` | `/github/<org>/pull_request_review_comment` | `github_pull_request_review_comment_handler.py` | informative event only |
| `pull_request_review_thread` | `/github/<org>/pull_request_review_thread` | `github_pull_request_review_thread_handler.py` | informative event only |

Shared infrastructure (HMAC verification, KeePass secret lookup, ping response,
informative-event inbox envelope, agent-user lookup, and the
`GithubWebhookHandlerBase` / `InformativeGithubWebhookHandler` classes all
three concrete handlers build on) lives in
`services/webhook/github_webhook_common.py`.

**Planned / future event types** (same URL/handler pattern, not yet implemented):
`pull_request_review`, `issue_comment`, `check_run`, …
See *Extending to other GitHub event types* below.

---

## Architecture

```
GitHub Org → POST /webhook/github/<org>/<event>
              ↓  (Caddy strips /webhook prefix)
           webhook_receiver.py  (port 8082)
              ↓  (shared path prefix: /github)
           <Event>Handler  (one per GitHub event type)
              ↓  (HMAC-SHA256 verified against GITHUB_WEBHOOK_SECRET_<org>)
           inbox.json  ← goal or informative event
              ↓
           Agent → event-specific skill (e.g. /review-ghpr)
```

`<event>` matches the GitHub event name (the value GitHub sends in the
`X-GitHub-Event` header) so each handler's URL is self-describing and the HMAC
secret is shared across all event types for a given org.

### `pull_request` handler specifics

**Reviewable PR actions** (queue a goal): `opened`, `reopened`, `ready_for_review`, `review_requested`  
**Gated action**: `review_requested` only queues a goal when the agent's GitHub user (from `GITHUB_TOKEN_1`) appears in `pull_request.requested_reviewers`.  
**All other actions** (e.g. `synchronize`, `closed`, `labeled`): written to inbox as informative events only.  
**Draft PRs**: skipped unless the action is `ready_for_review`.

### `pull_request_review_comment` / `pull_request_review_thread` handler specifics

Both handlers are **informative-only**: every verified, non-ping delivery is
acknowledged `200 {"status":"ok"}` and written to the inbox as
`type: "event"`. No goals are queued.

- `pull_request_review_comment` covers actions `created`, `edited`, `deleted`
  — inline review comments tied to a specific file and line. The handler
  checks the comment's `user.login` against the agent's GitHub user (from
  `GITHUB_TOKEN_1`) and appends `[SELF — comment authored by this agent]`
  to the inbox note when they match, so self-authored review comments are
  distinguishable from human-authored ones at a glance.
- `pull_request_review_thread` covers actions `resolved`, `unresolved` — the
  lifecycle of an entire review thread. The handler checks `sender.login`
  against the agent's GitHub user and appends
  `[SELF — action performed by this agent]` when they match, symmetrical
  with the review-comment SELF flag.

Each handler surfaces a short one-line summary in the inbox entry's `Note:`
field (comment author + `path:line`, with a `SELF` tag when authored by
the agent; thread actor + comment count, also SELF-tagged when the agent
triggered it) to make audit log entries scannable without opening the
full payload.

---

## Step 0 — Prerequisites

### A. Public URL
The agent must be reachable from the internet (e.g. via Cloudflare Tunnel).
Check or set the public URL:

```bash
uv run python scripts/portal_config.py hostname --show
uv run python scripts/portal_config.py hostname --set https://your-public-hostname.example.com
```

Your webhook payload URL will be:
```
https://<public-hostname>/webhook/github/<org>/<event>
```

Where `<event>` matches the GitHub event name. For the PR review flow, use
`pull_request`. Additional event types register at their own path under the
same `/webhook/github/<org>/…` tree.

### B. GitHub Token
A GitHub Personal Access Token with at least `repo` scope must be stored in KeePass
so the `review-ghpr` skill can fetch PR details and submit reviews. The `username`
field must be the exact GitHub login of the user the agent acts as — the
`pull_request` handler uses it (for `review_requested` events) to decide whether
the request is aimed at this agent:

```bash
uv run python scripts/keepass.py store \
  --title "GITHUB_TOKEN_1" \
  --username "<github-username>" \
  --password "ghp_..." \
  --group "API Keys"
```

---

## Step 1 — Store the HMAC secret in KeePass

The secret must match exactly what you will enter on GitHub.
The KeePass title pattern is `GITHUB_WEBHOOK_SECRET_<org>` (case-sensitive).
One secret per org — it is shared across all event-type handlers for that org.

```bash
uv run python scripts/keepass.py store \
  --title "GITHUB_WEBHOOK_SECRET_<org>" \
  --username "github" \
  --password "<your-webhook-secret>" \
  --group "API Keys"
```

> If no secret is stored the handler accepts webhooks **unverified** — a warning is logged.
> Always store a secret in production.

To rotate the secret later, re-run the same command with the new value, then update the
webhook on GitHub. The handler reloads KeePass on each request — no restart needed.

---

## Step 2 — Register the org in the handler state file

> **Only required for handlers that consult per-org config.** The
> `pull_request` handler does — it needs a `guideline_prompt_path`
> per org. The informative-only handlers
> (`pull_request_review_comment`, `pull_request_review_thread`) do
> **not** use a state file; they write every verified delivery straight
> to the inbox, so you can skip this step for those event types.

The `pull_request` handler reads
`/agent/memory/github_pull_request_handler_state.json`. Future
config-driven handlers follow the same
`github_<event>_handler_state.json` convention.

Edit (or create) the state file for the event type you are enabling.
Add one key per org, keyed by the **exact** GitHub org login (case-insensitive matching
is used during validation, but use the canonical casing for clarity):

```json
{
  "<org>": {
    "guideline_prompt_path": "/agent/prompts/code_review_<org>.md"
  }
}
```

Multiple orgs are supported — add one entry per org:

```json
{
  "org-one": {
    "guideline_prompt_path": "/agent/prompts/code_review_org-one.md"
  },
  "org-two": {
    "guideline_prompt_path": "/agent/prompts/code_review_org-two.md"
  }
}
```

> This file is **re-read on every webhook delivery** — no service restart is needed
> after adding, removing, or editing orgs.

---

## Step 3 — Create a code review guideline prompt

> Applies to the `pull_request` handler. Other event-type handlers may define
> their own prompt fields (or none at all) — consult that handler's state
> schema before filling in extra keys.

Create the markdown file referenced by `guideline_prompt_path`.
This file is passed verbatim to the `/review-ghpr` skill and controls what the agent
focuses on when reviewing PRs for this org.

Starter template (save to `/agent/prompts/code_review_<org>.md`):

```markdown
# Code Review Guidelines — <org>

You are a senior engineer reviewing pull requests for the **<org>** GitHub organization.
Your goal is to provide thorough, constructive, and actionable feedback.

## Review Focus Areas

### 1. Correctness
- Does the code do what the PR description claims?
- Are there logic errors, off-by-one mistakes, or unhandled edge cases?
- Are error paths properly handled (null checks, exceptions, empty collections)?

### 2. Security
- Are there injection risks (SQL, command, XSS, SSRF)?
- Are secrets/credentials ever hardcoded or logged?
- Are inputs validated and sanitized before use?
- Are auth checks in place where needed?

### 3. Code Quality
- Is the code readable and self-documenting?
- Are functions focused and not doing too many things?
- Is duplication introduced that could be abstracted?
- Are names consistent with codebase conventions?

### 4. Tests
- Are new features or bug fixes covered by tests?
- Are edge cases tested?

### 5. Performance
- Are there N+1 queries, unnecessary loops, or blocking I/O in hot paths?

### 6. Documentation
- Are public APIs and non-obvious logic documented?
- Is the PR description clear about *what* changed and *why*?

## Review Tone

- Be direct but respectful. Assume good intent.
- Distinguish blocking issues from suggestions.
- Use prefixes: **[BLOCKING]**, **[SUGGESTION]**, **[QUESTION]**, **[NITPICK]**

## Output Format

Submit your review using the `review-ghpr` skill with inline comments on specific lines
where relevant. Provide a summary comment with an overall verdict: APPROVE / REQUEST_CHANGES / COMMENT.
```

---

## Step 4 — Start the webhook_receiver service

```bash
# Start (first time or after a container restart)
uv run python scripts/service_manager.py start webhook_receiver 8082 \
  -- uv run python /agent/services/webhook_receiver.py

# Check status
uv run python scripts/service_manager.py status webhook_receiver

# View live logs
tail -f /agent/memory/logs/service-webhook_receiver.stdout.log
```

Expected log output on a healthy start (one block per registered handler):

```
[INFO] Starting GitHub pull-request handler (multi-org)...
[INFO]   Org <org>: guideline=/agent/prompts/code_review_<org>.md, signature verification enabled
[INFO] GitHub pull_request handler ready — path pattern: /webhook/github/<org>/pull_request
[INFO] Starting GitHub pull_request_review_comment handler (informative-only)...
[INFO] GitHub pull_request_review_comment handler ready — path pattern: /webhook/github/<org>/pull_request_review_comment
[INFO] Starting GitHub pull_request_review_thread handler (informative-only)...
[INFO] GitHub pull_request_review_thread handler ready — path pattern: /webhook/github/<org>/pull_request_review_thread
[INFO] HTTP server started on port 8082
[INFO] Ready. Listening for webhooks on port 8082...
```

If `signature verification enabled` appears (logged lazily on the first
delivery per org for the informative-only handlers), the HMAC secret was
loaded correctly. If `NO secret — verification DISABLED` appears, check the
KeePass title matches exactly.

---

## Step 5 — Configure the webhook on GitHub (Org level)

Each GitHub event type is configured as its **own** webhook so GitHub delivers
it to the handler-specific URL. Repeat this step once per event type you want
the agent to receive from this org.

1. Go to: `https://github.com/organizations/<org>/settings/hooks`
2. Click **Add webhook**
3. Fill in the form (example shown for `pull_request`):

| Field | Value |
|-------|-------|
| **Payload URL** | `https://<public-hostname>/webhook/github/<org>/<event>` |
| **Content type** | `application/json` |
| **Secret** | *(same value stored in KeePass for this org — shared across event types)* |
| **Which events?** | **Let me select individual events** → check only the event matching `<event>` (e.g. **Pull requests** for `pull_request`, **Pull request review comments** for `pull_request_review_comment`, **Pull request review threads** for `pull_request_review_thread`) |
| **Active** | ✅ |

4. Click **Add webhook**

GitHub immediately sends a `ping` event. The handler responds `{"status": "pong"}`.

---

## Step 6 — Verify the ping

Check the logs to confirm the ping was received and verified:

```bash
tail -20 /agent/memory/logs/webhook_receiver.log
```

You should see a POST to `/github/<org>/<event>` with no signature error.

Test manually with a correctly signed ping (swap `<event>` for the event type you are
verifying, e.g. `pull_request`):

```bash
SECRET="<your-webhook-secret>"
BODY='{"zen":"test"}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -s -X POST http://localhost:8082/github/<org>/<event> \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: ping" \
  -H "X-Hub-Signature-256: sha256=$SIG" \
  -d "$BODY"
# Expected: {"status": "pong"}
```

---

## Adding a Second Org

1. Store its secret: `GITHUB_WEBHOOK_SECRET_<new-org>` in KeePass (shared across event types)
2. For **config-driven** handlers (today: `pull_request`), add the org block to the
   handler's state file — e.g. `/agent/memory/github_pull_request_handler_state.json`.
   Informative-only handlers (`pull_request_review_comment`,
   `pull_request_review_thread`) have no state file and need no per-org entry.
3. Create any event-specific config files the handler needs (e.g.
   `/agent/prompts/code_review_<new-org>.md` for `pull_request`).
4. Register one webhook per event type on GitHub for the new org (same steps as Step 5).

No service restart required — handlers re-read their state files on every delivery and
lazily load KeePass secrets on first use, so new orgs are picked up automatically.

---

## Extending to other GitHub event types

The receiver is designed so a new GitHub event type is a **drop-in sibling** of
the existing handlers — not a fork of this skill. Shared infrastructure lives in
`services/webhook/github_webhook_common.py`:

- `GithubWebhookHandlerBase` — parent class for every per-event handler. Owns
  `path_prefix = "/github"`, the `matches(path)` dispatcher hook, the HTTP
  request lifecycle (path match → 404, method check → 405, body read, HMAC
  verify, ping → pong, JSON parse, 200 ack, dispatch to
  `_process_event`), and a `_write_informative_event(url_org, event, action,
  payload, note)` helper for informative inbox entries.
- `InformativeGithubWebhookHandler` — subclass of the base; use when the
  event should only produce inbox events (no goal queueing). Overrides
  `_process_event` to accumulate mismatch notes + a subclass-supplied
  `_note_for(payload)` summary and write one informative entry.
- `get_agent_github_user(log)` — module-wide cached lookup of the
  `GITHUB_TOKEN_1` username; the single source of truth for SELF-origin
  and review-target checks across all handlers.
- Low-level helpers: `compile_event_path_re`, `verify_signature`,
  `keepass_get`, `keepass_get_username`, `send_json`,
  `write_informative_event`.

### Recipe A — Informative-only event type

Use when the agent should just log the event (the
`pull_request_review_comment` / `pull_request_review_thread` handlers are
reference implementations).

1. **Create** `v1/codasst/services/webhook/github_<event>_handler.py`:

    ```python
    from webhook.github_webhook_common import (
        InformativeGithubWebhookHandler,
        get_agent_github_user,  # only if you need SELF-origin detection
    )

    class Github<Event>Handler(InformativeGithubWebhookHandler):
        EVENT_NAME = "<event>"                              # e.g. "issue_comment"
        SOURCE = "github_<event>_webhook"

        def _note_for(self, payload: dict) -> str:
            # Optional: return a one-line summary for the inbox Note line.
            return ""
    ```

2. **Register** the class in `v1/codasst/services/webhook_receiver.py` — add
   one import line and append an instance to `HANDLERS`.
3. **Configure the GitHub webhook** at the new URL (Step 5). No state file
   or prompt is required; per-org HMAC secrets are shared with the other
   GitHub handlers.

### Recipe B — Actionable event type (queues a goal)

Use when the agent should act on the event (the `pull_request` handler is
the reference implementation — it reads a state file, looks up a guideline
prompt, and queues a `type: "goal"` item).

1. Subclass `GithubWebhookHandlerBase` directly (not
   `InformativeGithubWebhookHandler`). You inherit `path_prefix`,
   `matches(path)`, HMAC verification, ping handling, JSON parsing, and the
   `_write_informative_event` helper for free.
2. Declare `EVENT_NAME` and `SOURCE` class attributes — the base class will
   raise in `__init__` if either is missing.
3. Override `start()` only if you need per-org configuration logging
   (the `pull_request` handler iterates its state file to report
   guideline path + secret status per org; informative-only handlers skip
   this).
4. Implement `_process_event(url_org, event, payload)`:
   - Handle `event != self.EVENT_NAME` and payload-org mismatches by
     calling `self._write_informative_event(...)` and returning early.
   - Read `/agent/memory/github_<event>_handler_state.json` for any
     per-org config (see `load_org_state` in `github_pull_request_handler.py`
     as a template).
   - Use `get_agent_github_user(self.log)` when you need to check whether
     an event targets or was authored by the agent's GitHub user.
   - Queue actionable payloads to inbox as `type: "goal"`; everything else
     should be written via `self._write_informative_event` so operators
     retain visibility.
5. Register in `webhook_receiver.py` and configure the GitHub webhook
   (Step 5) — one webhook per event type.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `{"status": "unauthorized"}` (401) | HMAC signature mismatch | Verify secret in KeePass matches GitHub exactly; re-store if needed |
| `{"status": "not_found"}` (404) | URL path wrong | Must be `/webhook/github/<org>/<event>` where `<event>` matches a registered handler (e.g. `pull_request`) — check for typos |
| `405 Method Not Allowed` | Non-POST request | GitHub webhooks always POST; ignore GET probes |
| Informative event, no goal queued | Org not in state file | Add org entry to the relevant `github_<event>_handler_state.json` |
| Informative event, no goal queued | Org mismatch | Payload org (repo owner) must match the URL org |
| Informative event, no goal queued | Required prompt path missing (e.g. `guideline_prompt_path` for PR) | Set the path in the state file and create the file |
| Informative event on `review_requested` | Request not aimed at agent | The agent's GitHub user (`GITHUB_TOKEN_1` username) is not in `requested_reviewers` — expected behavior |
| `signature verification DISABLED` in logs | Secret not in KeePass | Check title is `GITHUB_WEBHOOK_SECRET_<org>` (exact, case-sensitive) |
| Port 8082 not responding | Service stopped | Run `service_manager.py start webhook_receiver ...` |
| GitHub shows delivery failed | Tunnel/public URL down | Confirm Cloudflare Tunnel is running and `portal_config.py hostname` is set |

---

## Key File Locations

Per-handler files follow the pattern `github_<event>_*`; the table lists the
three implemented handlers plus shared infrastructure.

| File | Purpose |
|------|---------|
| `/agent/memory/github_pull_request_handler_state.json` | Per-org config for the `pull_request` handler (re-read per request). Informative-only handlers have no state file. |
| `/agent/prompts/code_review_<org>.md` | Review guidelines passed to `/review-ghpr` (PR handler only) |
| `/agent/services/webhook/github_webhook_common.py` | Shared infrastructure: HMAC, KeePass, ping, informative-event writer, `InformativeGithubWebhookHandler` base class |
| `/agent/services/webhook/github_pull_request_handler.py` | `pull_request` handler (review goal queueing) |
| `/agent/services/webhook/github_pull_request_review_comment_handler.py` | `pull_request_review_comment` handler (informative-only) |
| `/agent/services/webhook/github_pull_request_review_thread_handler.py` | `pull_request_review_thread` handler (informative-only) |
| `/agent/services/webhook_receiver.py` | Generic receiver that hosts all sub-handlers and registers them in `HANDLERS` |
| `/agent/memory/logs/webhook_receiver.log` | Full audit log of every delivery (all event types) |
| `/agent/memory/logs/service-webhook_receiver.stdout.log` | Service stdout (startup, errors) |
