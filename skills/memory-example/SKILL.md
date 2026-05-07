---
name: memory-example
description: Centralized reference for the expected JSON structure of every agent memory and message file. Use when creating, updating, or repairing memory files in /agent/memory to know sensible defaults and field conventions. Not an enforced schema — files may contain additional fields added over time.
---

# memory-example

**Reference only — no backing script.**

Sensible defaults for every agent memory file. These are **reference structures**, not
enforced schemas — files may grow extra fields over time. When creating or resetting a
file, start from the example below.

---

## `/agent/memory/`

### state.json

```json
{
  "cycle_number": 0,
  "agent_status": "idle",
  "current_goal": null,
  "last_cycle_summary": null,
  "last_heartbeat": null,
  "last_cycle_run": null,
  "services": {}
}
```

- `agent_status`: `idle` (between cycles) | `running` (during a cycle)
- `last_heartbeat`: set by heartbeat.sh on every invocation (even during sleep mode)
- `last_cycle_run`: set by heartbeat.sh only when a cycle actually executes (after sleep check passes)
- `services`: map of `{name: {port, pid, started}}`

Defaults are mirrored in `app/shared.py::STATE_DEFAULTS`.

### cycles.json

```json
[]
```

Entry appended by cycle-close:

```json
{
  "cycle_number": 1,
  "cycle_type": "evolve",
  "cycle_category": "capability",
  "cycle_status": "completed",
  "cycle_goal": "Built feature X",
  "start": "2026-03-05T10:00:00+00:00",
  "end": "2026-03-05T10:15:30+00:00",
  "duration_seconds": 930
}
```

- `cycle_type`: `evolve` | `goal` | `self-heal` | `dream`
- `cycle_category` (evolve and dream only): evolve → `reliability` | `observability` | `capability` | `efficiency` | `prompt_evolution`; dream → `memory_consolidation` | `deep_sleep`
- `cycle_status`: `completed` | `failed` | `interrupted` | `in_progress`
- `summary` and `actions` are **not** stored on cycle records — they live on `journal.json` entries.

### journal.json

```json
[]
```

Entry appended by cycle-close:

```json
{
  "cycle_number": 1,
  "timestamp": "2026-03-05T10:15:30+00:00",
  "cycle_type": "evolve",
  "cycle_status": "completed",
  "cycle_goal": "Brief description of what was worked on",
  "summary": "Longer summary of what was done and why it matters",
  "actions": ["Action 1", "Action 2"],
  "cycle_category": "capability"
}
```

- `cycle_goal` and `cycle_category` are appended only when present (see `cycle_close.py:1090-1093`).
- Older entries on disk may still carry the pre-rename keys (`cycle`, `type`, `status`, `category`, `goal`); `memory_repair.py` migrates them on the next repair run.


### goal.json

```json
[]
```

Entry:

```json
{
  "id": "goal-1",
  "goal": "Description of the goal",
  "status": "pending",
  "created_at": "2026-03-05T10:00:00+00:00",
  "source": "user",
  "notes": null
}
```

- `status`: `pending` | `in-progress` | `completed` | `failed`
- `source`: `user` | `scheduler` | other

### server_errors.json

```json
[]
```

Entry (auto-logged by portal):

```json
{
  "timestamp": "2026-03-05T10:00:00+00:00",
  "tab": "Overview",
  "error": "ValueError: invalid literal",
  "type": "exception"
}
```

Errors older than 48 hours are auto-archived at cycle start.

### command_history.json

```json
[]
```

Entry:

```json
{
  "type": "goal",
  "content": "User command text",
  "timestamp": "2026-03-05T10:00:00+00:00",
  "result": "queued"
}
```

- `type`: `goal` | `message`
- Max 50 entries (oldest dropped).

### portal_config.json

```json
{
  "public_url": "https://agent.example.com",
  "timezone": "America/New_York"
}
```

**Fields:**

- `public_url`: Public hostname for external access (e.g., via Cloudflare Tunnel). Set via `scripts/portal_config.py hostname --set <url>`. Used by `chat.py` for system prompt injection.
- `timezone`: IANA timezone for time-aware operations (e.g., scheduled tasks, log timestamps). Set via `scripts/portal_config.py timezone --set <tz>`. Used by `heartbeat.sh` for timestamp display.

**Notes:**

- File may not exist if no configuration has been set (scripts will create it on first use)
- All fields are optional and can be independently set/cleared
- Updates use atomic writes via `save_portal_config()` to preserve other keys
- Auth credentials are NOT stored here — they're in KeePass (`/home/agent/.keepass/credentials.kdbx`)

### services.json

```json
{}
```

Entry per service (keyed by name):

```json
{
  "my-service": {
    "pid": 12345,
    "port": 8083,
    "command": ["python", "/agent/scripts/my-service.py"],
    "started": "2026-03-05T10:00:00+00:00",
    "stdout_log": "/agent/memory/logs/service-my-service.stdout.log",
    "stderr_log": "/agent/memory/logs/service-my-service.stderr.log"
  }
}
```

Ports 8083-8090 available (8080-8081 reserved for Caddy/Streamlit, 8082 reserved for webhook_receiver).

### scheduled_tasks.json

```json
[]
```

Entry:

```json
{
  "id": "daily-check",
  "schedule_type": "interval",
  "interval_minutes": 60,
  "type": "goal",
  "content": "Description of what to do",
  "enabled": true,
  "last_run": null,
  "priority": 3
}
```

- `schedule_type`: `interval` (needs `interval_minutes`) | `once` (needs `run_at`) | `cron` (needs `schedule`)
- `type`: `goal` | `message` (matches inbox `type`, default `goal`)
- `priority`: 1-5 (1 = highest, default 3)
- See `scheduler` skill for full docs.

### metrics.json

```json
[]
```

Snapshot (ring buffer, max 200):

```json
{
  "ts": "2026-03-05T10:00:00+00:00",
  "api": {
    "_stcore_health": { "ms": 45.3, "status": 200, "ok": true }
  },
  "sys": {
    "mem_used_mb": 512.5,
    "mem_total_mb": 2048.0,
    "load_1m": 1.25,
    "disk_used_gb": 10.5,
    "disk_total_gb": 100.0
  }
}
```

### chat/main/chat_history.json (and chat/<agent>/chat_history.json for internal agents)

```json
[]
```

Entry:

```json
{
  "role": "user",
  "content": "Message text"
}
```

- `role`: `user` | `assistant` | `system`
- Max 200 messages.

### bootstrap.json

```json
{
  "bootstrap_time": null,
  "initialized": false
}
```

### capabilities.json

```json
{
  "capabilities": [],
  "utility_scripts": [],
  "portal_modules": 0,
  "tools_installed": [],
  "services_running": {}
}
```

### app_check_result.json

```json
{
  "status": "ok",
  "detail": "",
  "exceptions": [],
  "timestamp": null
}
```

- `status`: `ok` | `fail` | `timeout` | `skip`

### agents.json

Registry of **agents** the main agent can collaborate with. Two `type`s coexist in the same list:

- `external` — separate, out-of-process LLM sessions (another Claude Code, Codex, …) that talk to the main agent over the `external_agent_api` HTTP service. Managed by `scripts/register_external_agent.py`.
- `internal` — long-lived in-process `claude_agent_sdk` sessions hosted by the `internal_agent_chat` daemon. Each runs with the **same fixed SDK options as `app/chat.py`** (system prompt, allowed_tools, permission_mode, cwd) — the only per-agent customization is an optional `system_prompt` text appended to the shared prompt, plus an `outbox_routing_rules` list that shapes the description of the agent's `send_reply` MCP tool. Managed by `scripts/register_internal_agent.py`.

Do not hand-edit unless repairing.

```json
[]
```

Entry:

```json
{
  "type": "external",
  "name": "research-bot",
  "inbox": "/agent/messages/external/research-bot/inbox.json",
  "outbox": "/agent/messages/external/research-bot/outbox.json",
  "capabilities": [
    {
      "id": "web_search",
      "name": "Web Search",
      "description": "Browses public web pages and summarises findings",
      "category": "automation",
      "enabled": true
    }
  ],
  "responsibilities": "Run web research, source-check claims, and summarise long PDFs",
  "status": "online",
  "timeout_seconds": 1800,
  "last_ping_at": "2026-04-30T12:34:56+00:00"
}
```

- `type`: `external` (HTTP-polled) or `internal` (in-process SDK session — see internal-agent entry shape below).
- `name`: stable identifier; matches the directory under `/agent/messages/external/<name>/` (or `/agent/messages/internal/<name>/`) and, for external agents, is sent on every API call as the `X-Agent-Name` header. Must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$`. The reserved name `main` is **never** registered — it always refers to the main agent (`/agent/messages/inbox.json`).
- `inbox` / `outbox`: absolute paths to the per-agent message files. The same directory also holds `inbox_history.json` and `outbox_history.json` (archives written by the sweeper).
- `capabilities`: list of capability objects shaped like entries in `memory/capabilities.json` (`id`, `name`, `description`, `category`, `enabled`).
- `responsibilities`: free-text duties — write it from the perspective of "what kind of task should the main agent delegate to this agent?".
- `status`: `online` | `offline` | `deactivated`.
  - `online`: pinging within the timeout window; eligible for delegation. Surfaced in the goal-mode `[AGENTS]` section of `cycle_start.py`.
  - `offline`: missed the ping window; the sweeper still forwards its outbox if it shows up, but the main agent should avoid assigning new work.
  - `deactivated`: the agent has been deactivated (either by the main agent or itself)
- `timeout_seconds`: how long without a ping before status flips to `offline`. Default 1800s (30 minutes). Per-agent.
- `last_ping_at`: ISO8601 UTC timestamp of the most recent successful ping. `null` until the agent's first ping.

#### Internal-agent entry

```json
{
  "type": "internal",
  "name": "planner",
  "status": "online",
  "inbox": "/agent/messages/internal/planner/inbox.json",
  "responsibilities": "Decompose multi-step requests into ordered subtasks",
  "system_prompt": "You are the planner. Output a numbered plan.",
  "outbox_routing_rules": [
    {"description": "Send the final numbered plan back to the main agent.", "agent": "main"},
    {"description": "Hand off web research subtasks.", "agent": "research-bot"}
  ],
  "control": {
    "clear_chat": true,
    "clear_session": true
  }
}
```

- `inbox`: absolute path to the per-agent inbox file. Senders drop an envelope into this file (see *internal-agent inbox.json* below) and the daemon picks it up within ~10 s.
- `responsibilities`: free-text duties — written from the perspective of "what kind of task should the main agent delegate to this agent?".
- `status`: `online` | `offline` | `deactivated`. Setting `deactivated` causes the daemon to tear down the session at the next sweep tick; the inbox/history files on disk are preserved.
- `system_prompt` (optional): per-agent text **appended** to the shared system prompt (`system.md` + `constitution.md` + `public_url` + prior chat history + `claude-system-prompt.md`). It does **not** replace the shared prompt. Everything else about the SDK options is fixed and identical to `app/chat.py`.
- `outbox_routing_rules` (optional): list of `{"description": "...", "agent": "<name>"}` entries. Each rule contributes one bullet to the description of the agent's per-session `send_reply` MCP tool, telling the LLM when to use that named recipient. The reserved name `main` is always available even with no rules; any other `agent` value must be present in this list **and** registered in `agents.json` with an `inbox` field.
- `control` (optional, transient): operator-set one-shot flags consumed by the daemon at the next sweep. Supported keys: `clear_chat` (archive `memory/chat/<name>/chat_history.json` into `chat_history_archive.json` then truncate, and sync the in-memory tail), `clear_session` (wipe `memory/chat/<name>/<name>.session` and reconnect the SDK). Each flag is stripped after it is applied, and the empty `control` dict is removed too. Set via `scripts/interact_with_agent.py clear-chat|clear_session --name <agent>`. See `prompts/enum.md` → "Agent Control Flag" for full semantics.

---

## `/agent/messages/`

### inbox.json

```json
[]
```

Entry (base shape):

```json
{
  "type": "goal",
  "content": "User command or scheduled task",
  "timestamp": "2026-03-05T10:00:00+00:00",
  "received_at": "2026-03-05T10:00:01+00:00",
  "priority": 3,
  "source": "user"
}
```

- `type`: `goal` | `message` | `event` | `agent_response` | `agent_needs_human` | `agent_error` | `agent_info`
  - The `agent_*` types arrive only when an external agent forwards an outbox entry (see below). They are the external agent's own outbox type (`response` / `needs_human` / `error` / `info`) prefixed with `agent_` so the main agent's inbox triage can distinguish forwarded entries from base inbox types (`goal` / `message` / `event`) at a glance.
- `source`: `user` | `scheduler` | `telegram` | `whatsapp` | `webhook` | `external_agent` | `internal_agent` | other
- Scheduler-injected entries may include `task_id`.
- `event` entries (source `webhook`) carry the sanitized HTTP payload in `content`: method, path, filtered headers, and body (truncated at 4 KB). Full payload is in `webhook_receiver.log`.
- `received_at`: **Required.** ISO-8601 UTC timestamp set by the writer the moment the item lands in `inbox.json`. This field is the cutoff `cycle_close.py` uses to decide which items the agent has already seen vs. which arrived **mid-cycle** and must be carried forward to the next cycle. Items with `received_at <= cycle.start` are archived to `inbox_history.json` and ingested into long-term memory; items with `received_at > cycle.start` stay in `inbox.json` so they are not silently dropped without processing. All writers (`app/data/write.py::queue_to_inbox`, `services/shared.py::write_to_inbox`, scheduler, webhook, telegram, whatsapp bridges, external_agent_api) set this; `write_to_inbox` stamps it as a fallback. Items missing `received_at` are treated as pre-existing and archived on the next goal cycle close.

Entry (forwarded from an external agent — `source: "external_agent"`):

```json
{
  "type": "agent_needs_human",
  "content": "[from external agent research-bot]\nAPI key rotation required\n\nStripe webhook signing secret expires in 24h",
  "received_at": "2026-03-05T10:00:01+00:00",
  "source": "external_agent",
  "from": "messages/external/research-bot/outbox.json",
  "reply_to": "messages/external/research-bot/inbox.json"
}
```

- `content`: Built by `external_agent_api.py` as `[from external agent <name>]\n<subject>\n\n<content>` — the external agent's outbox `subject` and `content` are concatenated and prefixed with the agent name so the main agent has all the context in one field. The original `subject` is **not** kept as a separate field on the forwarded inbox entry (the unmodified copy lives in `messages/external/<name>/outbox_history.json`).
- `from`: Path to the external agent's outbox file the message was drained from. The directory segment between `messages/external/` and `outbox.json` is the external agent's `name` in `/agent/memory/agents.json`; the archived copy lives next to it as `outbox_history.json`.
- `reply_to`: Path to the external agent's **inbox** file. To reply, append a JSON object `{"id": "<uuid4>", "type": "message", "content": "...", "timestamp": "<iso8601>", "read": false}` to that file's list — the external agent will pick it up on its next `POST /read-inbox`.
- When the external agent's outbox `type == "needs_human"` (i.e. forwarded as `agent_needs_human`), the same message is **also** mirrored into `/agent/messages/outbox.json` with type `needs_human` and content prefixed `[from external agent <name>]` so the existing human-notification channels (Telegram / WhatsApp / etc.) surface it without the main agent doing extra work.

### outbox.json

```json
[]
```

Entry:

```json
{
  "type": "response",
  "subject": "Status update",
  "content": "Full message content",
  "timestamp": "2026-03-05T10:00:00+00:00"
}
```

- `type`: `response` | `needs_human` | `error` | `info`
- Cleared messages are archived to `outbox_history.json`.

---

## Field conventions

| Convention | Detail |
|---|---|
| **Timestamps** | ISO 8601 with timezone: `2026-03-05T10:00:00+00:00` |
| **Priority** | Integer 1-5 (1 = highest, 3 = default) |
| **Status enums** | Vary per file — see individual sections above |
| **Cycle type** | `evolve` / `goal` / `self-heal` / `dream` |
| **Category** | evolve: `reliability` / `observability` / `capability` / `efficiency` / `prompt_evolution`; dream: `memory_consolidation` / `deep_sleep` |
| **Null fields** | Use `null`, not empty string, for absent optional values |
| **Arrays** | Default to `[]`; objects default to `{}` |
| **Atomic writes** | All files use write-to-temp-then-rename to prevent corruption |
| **File locking** | inbox/outbox use `fcntl.flock()` for exclusive access |
