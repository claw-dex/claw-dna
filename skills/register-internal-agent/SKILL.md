---
name: register-internal-agent
description: Register, onboard, list, and deactivate **internal agents** — long-lived in-process `claude_agent_sdk` sessions hosted by the `internal_agent_chat` daemon. Each internal agent has its own conversation history, its own `inbox.json`, and a per-session `send_reply` MCP tool for routing replies. Use when the user asks to "register an internal agent", "spin up a planner / summarizer / triage agent", "list registered internal agents", or to deactivate one. NOT for external agents (use `register-external-agent`), NOT for in-process subagents spawned via the Agent / Task tool, and NOT for entries in `memory/capabilities.json`.
---

# register-internal-agent

**Path:** `scripts/register_internal_agent.py`

CLI for managing the internal-agent registry at `/agent/memory/agents.json` and the per-agent message directories at `/agent/messages/internal/<name>/`. Backs the [`internal_agent_chat`](../../services/internal_agent_chat.py) daemon (registered in `memory/services.json` with `auto_start: true`, no port — heartbeat-only liveness).

## When to use this

Reach for this skill when the user wants to:

- **Spin up a long-lived specialist session** (e.g. "register a planner internal agent that decomposes multi-step tasks", "set up a summarizer that returns 5-bullet summaries"). Each internal agent is a `claude_agent_sdk.ClaudeSDKClient` running **in-process** inside the `internal_agent_chat` daemon, with its own persistent transcript and a stable `session_id` that survives daemon restarts.
- **List who is currently registered** — useful before delegating so the main agent only sends work to internal agents whose `status` is `online` and whose `responsibilities` match the task.
- **Deactivate an internal agent** (operator-side kill switch — the daemon tears down that agent's SDK session at the next sweep tick; inbox + history files on disk are preserved).
- **Re-register / revive** an agent that was previously deactivated (just run `--name` again — re-registration intentionally clears `deactivated`).

Do **not** use this skill for:

- **External agents** (separate processes polling `/external-agent/*` over HTTP) — use the `register-external-agent` skill instead.
- **In-process subagents** spawned via the Agent / Task tool — those have nothing to do with `agents.json` or the `internal_agent_chat` daemon; they live entirely inside the calling session.
- Sending one-off messages to a registered internal agent — write a `{"type":"message", ...}` envelope to `/agent/messages/internal/<name>/inbox.json` directly. The daemon picks it up within ~10 s.
- Customizing SDK options (allowed_tools, permission_mode, cwd, add_dirs, …) — those are intentionally **fixed** and identical to `app/chat.py`. The only per-agent customizations are: `--system-prompt-*` text (appended to the shared system prompt), `--outbox-routing-rules-*`, and `--model` (override the SDK's default model on a per-agent basis).

## Subcommands

| Action | Flag | Effect |
|--------|------|--------|
| Register / update | `--name <agent-name> --responsibilities "<text>" [--system-prompt-file F \| --system-prompt-inline TEXT] [--outbox-routing-rules-file F \| --outbox-routing-rules-inline JSON] [--model haiku\|sonnet\|opus\|<model-id>]` | Adds (or upserts) the agent in `agents.json` with `type: "internal"` and `inbox: <abs path>`. Creates empty `messages/internal/<agent-name>/{inbox,inbox_history}.json` for inbox traffic and `memory/chat/<agent-name>/{chat_history,chat_history_archive}.json` + `<agent-name>.session` for the SDK chat. Clears any prior `deactivated` status. The daemon hot-reloads `agents.json` on its next sweep tick (10 s) so no service restart is needed. |
| List | `--list` | One line per internal agent: name, status, number of routing rules, responsibilities. |
| Deactivate | `--deactivate <agent-name>` | Sets `status="deactivated"` so the daemon stops the SDK session at the next sweep tick. Exits non-zero if `<agent-name>` is not registered as an internal agent (external-agent matches are ignored). |

There is no `--setup` because internal agents are wired up entirely by the daemon — the operator does **not** paste any prompt anywhere.

## Typical flows

### 1. Onboard a new internal agent

```bash
uv run python scripts/register_internal_agent.py \
    --name planner \
    --responsibilities "Decompose multi-step requests into ordered, verifiable subtasks" \
    --system-prompt-inline "You are the planner. Output a numbered plan, one tool/skill per line. Do not perform the work yourself — only plan it." \
    --outbox-routing-rules-inline '[
      {"description": "Send the final numbered plan back to the main agent.", "agent": "main"},
      {"description": "Hand off web-research subtasks to the research bot.", "agent": "research-bot"}
    ]'
```

That's it. Within ~10 s the `internal_agent_chat` daemon will:

1. Notice the new entry on its next sweep tick.
2. Build a `ClaudeSDKClient` with the same SDK options as `app/chat.py`, plus your `--system-prompt-*` text appended to the shared prompt and a `send_reply` MCP tool whose description embeds the routing rules above.
3. Start polling `/agent/messages/internal/planner/inbox.json` — drop a `{"type":"message", ...}` envelope there to talk to it.

### 2. Check who can take work

```bash
uv run python scripts/register_internal_agent.py --list
```

Only delegate to agents whose `status` is `online` and whose `responsibilities` match the task. The daemon flips every internal agent to `offline` on graceful shutdown (and back to `online` when the matching session reconnects on next start).

### 3. Send the agent a task

There is no helper for this — write the envelope yourself follow sample below:

```json
{
  "type": "goal",
  "content": "User command or the entire description the task that the agent should take up",
  "timestamp": "2026-03-05T10:00:00+00:00",
  "received_at": "2026-03-05T10:00:01+00:00",
  "priority": 3,
  "source": "user"
}
```

(see `memory-example` skill for more samples).

### 4. Stop / pause an internal agent

```bash
uv run python scripts/register_internal_agent.py --deactivate planner
```

The daemon tears down the SDK session at the next sweep tick. The agent's inbox files (`messages/internal/<name>/{inbox,inbox_history}.json`) and chat files (`memory/chat/<name>/{chat_history,chat_history_archive}.json` + `<name>.session`) are all preserved — re-registering with `--name planner` revives the agent and the new SDK session resumes its prior conversation verbatim.

## Inputs and validation

- `--name`: must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$`. Becomes a directory segment under `messages/internal/`. The reserved name `main` must **never** be used — it always refers to the main agent's `/agent/messages/inbox.json`.
- `--system-prompt-file` / `--system-prompt-inline`: optional text **appended** to the shared pre-built system prompt (system.md + constitution.md + public_url + prior chat history + claude-system-prompt.md). The two flags are mutually exclusive.
- `--outbox-routing-rules-file` / `--outbox-routing-rules-inline`: optional JSON list. Each entry must be an object with both `description` (non-empty string explaining when this route applies) and `agent` (non-empty string — a name registered in `agents.json`, or the reserved `"main"`). Each rule contributes one bullet to the description of the agent's `send_reply` MCP tool, telling the LLM when to use that named recipient. Rules are **not** auto-evaluated by the daemon — the LLM picks where to deliver each reply.
- `--responsibilities` is free text — write it from the perspective of the **main agent deciding whether to delegate** ("Decompose multi-step plans"; "Summarise long emails into 5 bullets"). Keep it specific enough that a triage step can match a task to an agent.
- `--model`: optional override for the SDK model. Accepts the short aliases `haiku`, `sonnet`, `opus` (the SDK resolves these to the latest Claude 4.x ids itself) or a full model id (e.g. `claude-sonnet-4-6`). Omit to use the SDK default. Pick `haiku` for cheap/fast formatting or summarisation work, `opus` for heavy reasoning, `sonnet` as the balanced default. Re-registering with a different value replaces the prior selection; the change takes effect on the next SDK connect (restart the daemon, or trigger `clear_session` via the agent's `control` flag).

## Where the data lives

- Registry: `/agent/memory/agents.json` — internal entries are mixed in with external ones; filter by `type == "internal"`.
- Per-agent inbox files: `/agent/messages/internal/<name>/{inbox,inbox_history}.json`. The daemon atomically pops `inbox.json`, stamps each envelope with a daemon-generated `id` + `processed_at`, archives every popped envelope to `inbox_history.json`, then runs one SDK turn per group (messages sharing a `reply_to` are merged into a single user turn).
- Per-agent chat files: `/agent/memory/chat/<name>/{chat_history,chat_history_archive}.json` + `<name>.session` (bare-string SDK resume id). The portal chat (the "main" agent) follows the same layout under `chat/main/`. `chat_history.json` is **never truncated by the daemon**; the only path that empties it is the operator-triggered `clear_chat` flag (see "Operator-driven clears" below), which first appends every cleared record to `chat_history_archive.json`. The `<name>.session` sidecar is updated after every turn so daemon restarts resume the conversation verbatim.
- Control-action audit log (global, all internal agents): `/agent/memory/logs/internal-agent-control-audit.log` — one JSON object per line, append-only. Each line has a leading `agent` field so a single grep reconstructs any agent's clear-history.
- Daemon logs: `/agent/memory/logs/internal_agent_chat.log`.
- Daemon heartbeat: `/agent/memory/heartbeats/internal_agent_chat.heartbeat` (touched once per 10 s sweep tick).
- Daemon registration in `memory/services.json` is `auto_start: true` and portless.

## Operator-driven clears

Two control flags can be set on an agent's `control` dict in `agents.json` to ask the daemon to reset state at the next sweep tick:

```jsonc
{
  "name": "planner", "type": "internal", "status": "online", ...,
  "control": { "clear_chat": true, "clear_session": true }
}
```

- **`clear_chat`** — empties `chat_history.json` so the next SDK turn starts with no conversation context (the SDK resume id is left intact, so the model still picks up mid-session if the daemon kept it). **Before truncation**, every existing record is appended to `chat_history_archive.json` (append-only, unbounded). If the archive write fails, the truncation is **aborted** and the control flag stays set so the next sweep retries — chat data is never destroyed without a successful archive.
- **`clear_session`** — drops the SDK resume id (deletes `<name>.session`), disconnects the live `ClaudeSDKClient`, and reconnects with a fresh session. Use this after changing `--system-prompt-*` or `--model` to make the change take effect immediately. If the reconnect fails, the flag stays set and the next sweep retries.

Every action — success or failure — appends one JSON line to the global audit log at `/agent/memory/logs/internal-agent-control-audit.log` capturing `agent`, `action`, `ts`, `ok`, plus `archived_count` + `archive_path` for `clear_chat` and `prior_session_id` + `new_session_id` for `clear_session`. To reconstruct one agent's clear-history: `grep '"agent":"<name>"' /agent/memory/logs/internal-agent-control-audit.log | jq .`.

After a successful action the daemon strips that one key from the `control` dict (and removes the dict entirely if empty). Unrelated keys in `control` are preserved.

## Heuristics for the main agent

- Before delegating a goal: `--list` first. If no `online` internal agent's `responsibilities` cover the goal, do not invent a delegation — handle locally, register a new agent, or fall back to an external agent via `register-external-agent`.
- Internal agents reply via their `send_reply` tool — incoming entries on the main inbox will have `source: "internal_agent"` and `reply_to: "messages/internal/<name>/inbox.json"`. To answer back, append a `{"type":"message", ...}` envelope to that path; do **not** call this script for replies.
- After deactivating an internal agent, its files persist; do not delete them unless the operator explicitly asks. Re-registering with `--name` is a non-destructive revive.
- If the daemon's heartbeat is stale (`/agent/memory/heartbeats/internal_agent_chat.heartbeat` mtime > 60 s) **before** delegating, surface the issue to the operator instead of registering a new agent — a registered agent is useless if the daemon hosting it is down.
