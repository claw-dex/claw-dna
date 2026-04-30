---
name: register-external-agent
description: Onboard, list, deactivate, and generate connection prompts for **external agents** — separate, out-of-process LLM sessions (e.g. another Claude Code or Codex instance running elsewhere) that talk to the main agent over the external_agent_api HTTP service at /external-agent/*. Use when the user asks to "register an external agent", "delegate work to an external agent", "connect another Claude Code / Codex external agent", "list registered external agents", or to deactivate / re-onboard one. NOT for in-process subagents spawned via the Agent / Task tool, NOT for skills, and NOT for entries in memory/capabilities.json — those are all internal to this agent and unrelated to the external-agent registry.
---

# register-external-agent

**Path:** `scripts/register_external_agent.py`

CLI for managing the external-agent registry at `/agent/memory/agents.json` and the per-agent message directories at `/agent/messages/external/<name>/`. Backs the [`external_agent_api`](../../services/external_agent_api.py) service.

## When to use this

Reach for this skill when the user wants to:

- **Delegate work to a separate external agent** (e.g. "have a research-bot external agent handle web lookups", "register an external coding agent that I can hand PR reviews to"). Each external agent is a Claude Code / Codex / other LLM session running on another host or another terminal that polls our `/external-agent/*` API; it is **not** an in-process subagent.
- **List who is currently registered** — useful before assigning a goal so you only delegate to external agents whose `status != "deactivated"` and whose `responsibilities` match the task.
- **Deactivate an external agent** (operator-side kill switch — stops the sweeper from forwarding their outbox into the main inbox).
- **Re-issue connection instructions** to an existing external agent (e.g. they crashed, started fresh, or a teammate needs to connect a second instance).

Do **not** use this skill for:

- **In-process subagents** spawned via the Agent / Task tool — those have nothing to do with `agents.json` or the `/external-agent/*` API; they live entirely inside this session.
- Internal capabilities or services on this host — those go in `memory/capabilities.json` and `memory/services.json` directly.
- Sending one-off messages to a registered external agent — write to `messages/external/<name>/inbox.json` or use the `reply_to` field on a forwarded inbox item.
- Authenticating the API — Caddy already applies basic auth at `/external-agent/*`.

## Subcommands

| Action | Flag | Effect |
|--------|------|--------|
| Register / update | `--name <agent-name> --responsibilities "<text>" [--capabilities-file F \| --capabilities-inline JSON] [--timeout-seconds N]` | Adds (or upserts) the agent in `agents.json`, creates empty `messages/external/<agent-name>/{inbox,outbox}.json`, and clears any prior `deactivated` status. |
| List | `--list` | One line per agent: name, type, status, timeout, last_ping_at. |
| Deactivate | `--deactivate <agent-name>` | Sets `status="deactivated"` so the API rejects all routes except `POST /update` and the sweeper skips that agent. Exits non-zero if `<agent-name>` isn't registered. |
| Connection prompt | `--setup <agent-name>` | Prints a one-line `/loop 1m ...` system prompt that the operator pastes into the external agent's Claude Code / Codex session. Show this prompt to the user to help setup the external agent. |

## Typical flows

### 1. Onboard a new external agent

```bash
# (a) Operator picks a stable name, writes responsibilities + capabilities.
uv run python scripts/register_external_agent.py \
    --name research-bot \
    --responsibilities "Web research, source-checking, summarising long PDFs" \
    --capabilities-inline '[{"id":"web_search","name":"Web Search","description":"Browses public web pages","category":"automation","enabled":true}]'

# (b) Get the connection prompt to paste into the remote Claude Code session.
uv run python scripts/register_external_agent.py --setup research-bot
```

The `--setup` output is one block. For example:

Paste this to your Claude Code/Codex:

```
/loop 1m You are external agent 'research-bot'. Each tick: `curl -s -u <user>:<pass> ...`...
```

The operator copies that into the remote agent. From then on, the remote agent pings every minute, picks up unread items via `POST /read-inbox`, and replies via `POST /write-outbox` — all surfaced into our main inbox by the sweeper with `source: "external_agent"` and types prefixed `agent_*` (see `prompts/enum.md`).

### 2. Check who can take work

```bash
uv run python scripts/register_external_agent.py --list
```

Look at each row's `status` and `last_ping_at`. Only delegate to `online` agents whose `responsibilities` match the task. Agents whose `last_ping_at` is older than `timeout_seconds` will be flipped to `offline` by the sweeper on its next pass.

### 3. Stop forwarding from a misbehaving agent

```bash
uv run python scripts/register_external_agent.py --deactivate research-bot
```

The agent's outbox stops draining into the main inbox, and any incoming `read-inbox` / `write-outbox` calls return 403. The agent itself can come back by calling `POST /update` with `{"status":"online"}` (operator can also re-run `--name research-bot ...` to reactivate from this side).

### 4. Re-issue the connection prompt

If the remote session was lost, just rerun `--setup` — it reads live config + credentials, so the printed instruction always reflects the current host and password.

## Inputs and validation

- `--name`: must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$`. The name becomes a directory segment under `messages/external/`, so unsafe characters are rejected.
- `--timeout-seconds`: must be `> 0`. Default is 300s. Set lower for chatty agents that should turn over fast; higher for slow research agents that legitimately go quiet between tasks.
- `--capabilities-file` and `--capabilities-inline` are mutually exclusive. Each entry should follow the same shape as a row in `memory/capabilities.json` (`id`, `name`, `description`, `category`, `enabled`).
- `--responsibilities` is free text — write it from the perspective of the **main agent deciding whether to delegate** ("Run web searches and summarise findings"; "Open and review PRs in repo X"). Keep it specific enough that a triage step can match a task to an agent.

## Where the data lives

- Registry: `/agent/memory/agents.json` (list of agent dicts).
- Per-agent files: `/agent/messages/external/<name>/{inbox,outbox,inbox_history,outbox_history}.json`. The sweeper moves read inbox entries (read_at older than 10 minutes) from `inbox.json` into `inbox_history.json`, and forwarded outbox entries from `outbox.json` into `outbox_history.json`.
- Portal config the script reads for `--setup`: `/agent/memory/portal_config.json` and KeePass entry `System/PORTAL_BASIC_AUTH`.
- Service that consumes all of the above: `services/external_agent_api.py` (port 8083, routed via Caddy at `/external-agent/*`).

## Heuristics for the main agent

- Before delegating a goal: `--list` first. If no online agent's `responsibilities` cover the goal, do not invent a delegation — handle the goal locally or ask the user whether to register a new agent.
- After registering a new agent, the operator still has to paste the `--setup` output into the remote session; the agent will be `offline` until its first ping.
- Forwarded entries already include `reply_to`. To answer a delegated agent, append a JSON object to that path — do **not** call this script for replies.
