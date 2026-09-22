---
name: register-external-agent
description: Register, onboard, list, deactivate, and re-issue connection prompts for **external agents** — separate, out-of-process LLM sessions (e.g. another Claude Code or Codex instance running elsewhere) that talk to the main agent over the external_agent_api HTTP service at /external-agent/*. Use when the user asks to "register an external agent", "delegate work to an external agent / research-bot / external coding agent", "connect another Claude Code / Codex agent", "list registered external agents" (e.g. before delegating a goal, to pick one whose responsibilities match), "deactivate an external agent" (kill switch — stops the sweeper from forwarding their outbox), or "re-issue / regenerate connection instructions" after a crash, restart, or for a second instance. NOT for in-process subagents spawned via the Agent / Task tool, NOT for sending one-off messages to a registered external agent (write to `messages/external/<name>/inbox.json` or use the `reply_to` on a forwarded item), NOT for authenticating the API (Caddy already applies basic auth at `/external-agent/*`), NOT for entries in `memory/capabilities.json` or `memory/services.json` — those are all internal to this agent and unrelated to the external-agent registry.
---

# register-external-agent

**Path:** `scripts/register_external_agent.py`

CLI for managing the external-agent registry at `/agent/memory/agents.json` and the per-agent message directories at `/agent/messages/external/<name>/`. Backs the [`external_agent_api`](../../services/external_agent_api.py) service.

## Subcommands

| Action | Flag | Effect |
|--------|------|--------|
| Register / update | `--name <agent-name> --responsibilities "<text>" [--capabilities-file F \| --capabilities-inline JSON] [--timeout-seconds N]` | Adds (or upserts) the agent in `agents.json`, creates empty `messages/external/<agent-name>/{inbox,outbox}.json`, and clears any prior `deactivated` status. |
| List | `--list` | One line per agent: name, type, status, timeout, last_ping_at. |
| Deactivate | `--deactivate <agent-name>` | Sets `status="deactivated"` so the API rejects all routes except `POST /update` and the sweeper skips that agent. Exits non-zero if `<agent-name>` isn't registered. |
| Connection prompt | `--setup <agent-name> [--client monitor\|loop] [--poll-seconds N]` | Prints the prompt the operator pastes into the external agent's session. Default `--client monitor` is for Claude Code: the agent starts one persistent background `Monitor` watch that polls `/ping` every N seconds (default 30) and wakes the agent only when something changes. `--client loop` prints the older `/loop 10m ...` prompt for Codex or any client without a `Monitor` tool. Show this prompt to the user to help set up the external agent. |

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

# Codex / other clients without a Monitor tool:
uv run python scripts/register_external_agent.py --setup research-bot --client loop
```

The default output tells the remote Claude Code session to start one persistent `Monitor` watch running a small `curl` + `sh` script. The script polls `GET /ping` every 30s in the background and prints a line only when something changes. Each line becomes one notification for the agent:

| Event line | Meaning | What the external agent does |
|------------|---------|------------------------------|
| `INBOX {"unread": N, "unread_ids": [...]}` | New unread items | Fetches the new ids via `POST /read-inbox`, does the work, replies via `POST /write-outbox` |
| `DEACTIVATED {...}` | Operator ran `--deactivate` | Stops; unread items stay queued until it is reactivated (the script has already exited) |
| `PING_FAILED http=...` | 3 failed polls in a row | Tells its user; the script keeps retrying and prints `PING_RECOVERED` when the API works again |

**Why Monitor instead of `/loop`:** every `/loop` tick is a full model turn that re-reads the whole context, even when the inbox is empty. The Monitor script runs in a shell, so the agent spends no tokens while idle. It also picks up work within ~30s instead of up to 10 minutes, and stays `online` because every poll updates `last_ping_at`.

Replies are surfaced into our main inbox by the sweeper with `source: "external_agent"` and types prefixed `agent_*` (see `prompts/enum.md`).

### 2. Check who can take work

```bash
uv run python scripts/register_external_agent.py --list
```

Look at each row's `status` and `last_ping_at`. Only delegate to `online` agents whose `responsibilities` match the task. Agents whose `last_ping_at` is older than `timeout_seconds` will be flipped to `offline` by the sweeper on its next pass. Agents set up with `--client monitor` ping every 30s while their session is alive, so a stale `last_ping_at` usually means the session (and its Monitor watch) died. Agents on `--client loop` ping only once per tick.

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

### 4. Stop forwarding from a misbehaving agent

```bash
uv run python scripts/register_external_agent.py --deactivate research-bot
```

The agent's outbox stops draining into the main inbox, and any incoming `read-inbox` / `write-outbox` calls return 403. A Monitor-based agent sees the change on its next poll: its script prints `DEACTIVATED` and exits. The agent itself can come back by calling `POST /update` with `{"status":"online"}` (operator can also re-run `--name research-bot ...` to reactivate from this side).

### 5. Re-issue the connection prompt

If the remote session was lost (a crashed or restarted Claude Code session also loses its Monitor watch), just rerun `--setup` — it reads live config + credentials, so the printed instruction always reflects the current host and password.

## Inputs and validation

- `--name`: must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$`. The name becomes a directory segment under `messages/external/`, so unsafe characters are rejected.
- `--timeout-seconds`: must be `> 0`. Default is 1800s (30 minutes). Set lower for chatty agents that should turn over fast; higher for slow research agents that legitimately go quiet between tasks.
- `--capabilities-file` and `--capabilities-inline` are mutually exclusive. Each entry should follow the same shape as a row in `memory/capabilities.json` (`id`, `name`, `description`, `category`, `enabled`).
- `--client`: `monitor` (default, Claude Code) or `loop` (Codex and other clients without a `Monitor` tool). Only affects `--setup` output.
- `--poll-seconds`: must be `> 0`. Default is 30. Each poll is one small HTTP request through Caddy and costs the agent no tokens; there is little reason to go below ~10s.
- `--responsibilities` is free text — write it from the perspective of the **main agent deciding whether to delegate** ("Run web searches and summarise findings"; "Open and review PRs in repo X"). Keep it specific enough that a triage step can match a task to an agent.

## Where the data lives

- Registry: `/agent/memory/agents.json` (list of agent dicts).
- Per-agent files: `/agent/messages/external/<name>/{inbox,outbox,inbox_history,outbox_history}.json`. The sweeper moves read inbox entries (read_at older than 10 minutes) from `inbox.json` into `inbox_history.json`, and forwarded outbox entries from `outbox.json` into `outbox_history.json`.
- Portal config the script reads for `--setup`: `/agent/memory/portal_config.json` and KeePass entry `System/PORTAL_BASIC_AUTH`.
- Service that consumes all of the above: `services/external_agent_api.py` (port 8083, routed via Caddy at `/external-agent/*`).

## Heuristics for the main agent

- Use the default `--client monitor` for Claude Code agents. Use `--client loop` only for clients that have no `Monitor` tool (e.g. Codex).

- Before delegating a goal: `--list` first. If no online agent's `responsibilities` cover the goal, do not invent a delegation — handle the goal locally or ask the user whether to register a new agent.
- After registering a new agent, the operator still has to paste the `--setup` output into the remote session; the agent will be `offline` until its first ping.
- Forwarded entries already include `reply_to`. To answer a delegated agent, append a JSON object to that path — do **not** call this script for replies.
