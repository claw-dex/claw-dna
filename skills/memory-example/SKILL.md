---
name: memory-example
description: Centralized reference for the expected JSON structure of every agent memory and message file. Use when creating, updating, or repairing memory files to know sensible defaults and field conventions. Not an enforced schema — files may contain additional fields added over time.
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
  "status": "idle",
  "current_goal": null,
  "last_cycle_summary": null,
  "last_cycle_type": null,
  "last_cycle_category": null,
  "created_at": null,
  "last_heartbeat": null,
  "last_cycle_run": null,
  "last_cycle_end": null,
  "services": {}
}
```

- `status`: `idle` (between cycles) | `running` (during a cycle)
- `last_cycle_type`: `evolve` | `goal` | `self-heal` | `null`
- `last_cycle_category`: evolve category (when applicable) | `null`
- `last_heartbeat`: set by heartbeat.sh on every invocation (even during sleep mode)
- `last_cycle_run`: set by heartbeat.sh only when a cycle actually executes (after sleep check passes)
- `last_cycle_end`: set by cycle_close.py when a cycle completes
- `services`: map of `{name: {port, pid, started}}`

### cycles.json

```json
[]
```

Entry appended by cycle-close:

```json
{
  "cycle": 1,
  "type": "evolve",
  "category": "capability",
  "status": "completed",
  "start": "2026-03-05T10:00:00+00:00",
  "end": "2026-03-05T10:15:30+00:00",
  "duration_seconds": 930,
  "summary": "Built feature X",
  "actions": ["Created file X", "Updated module Y"]
}
```

- `type`: `evolve` | `goal` | `self-heal`
- `category` (evolve only): `reliability` | `observability` | `capability` | `efficiency` | `prompt_evolution`
- `status`: `completed` | `failed` | `interrupted` | `in-progress`

### journal.json

```json
[]
```

Entry appended by cycle-close:

```json
{
  "cycle": 1,
  "timestamp": "2026-03-05T10:15:30+00:00",
  "type": "evolve",
  "status": "completed",
  "goal": "Brief description of what was worked on",
  "summary": "Longer summary of what was done and why it matters",
  "actions": ["Action 1", "Action 2"],
  "category": "capability"
}
```

Comprehensive examples:

```json
[
  {
    "cycle": 1,
    "timestamp": "2026-03-19T04:07:52.382690+00:00",
    "status": "completed",
    "type": "evolve",
    "goal": "Bootstrap: redesigned portal as Personal Assistant hub with Tasks management (priorities, due dates, categories, filtering) and Contacts CRM (relationship types, interaction logging, search). Reorganized tabs into Personal Assistant, Agent Console, and Files & Security groups.",
    "actions": [
      "Created app/tasks.py with full task management UI",
      "Created app/contacts.py with CRM and interaction logging",
      "Reorganized TAB_REGISTRY into Personal Assistant-focused layout",
      "Rebranded portal as Personal Assistant Agent",
      "Created tasks.json and contacts.json data stores"
    ],
    "summary": "Bootstrap: redesigned portal as Personal Assistant hub with Tasks management (priorities, due dates, categories, filtering) and Contacts CRM (relationship types, interaction logging, search). Reorganized tabs into Personal Assistant, Agent Console, and Files & Security groups.",
    "category": "capability"
  },
  {
    "cycle": 2,
    "timestamp": "2026-03-19T04:14:00+00:00",
    "status": "completed",
    "type": "evolve",
    "goal": "Improve observability: replace SVG bar charts with interactive Altair charts in Agent Overview dashboard",
    "actions": [
      "Replaced SVG goal duration sparkline with Altair bar chart + regression trend line",
      "Replaced SVG cycle velocity chart with Altair colored bar chart (by cycle type)",
      "Added rolling average line overlay to velocity chart (adaptive window 3 or 5)",
      "Added interactive tooltips with cycle number, type, category, and duration",
      "Lowered velocity chart threshold from 3 to 2 cycles, increased window from 20 to 30"
    ],
    "summary": "Replaced raw SVG bar charts in Agent Overview with interactive Altair charts \u2014 goal durations with trend line, cycle velocity colored by type with rolling average. Zero new dependencies.",
    "category": "observability"
  },
  {
    "cycle": 2,
    "timestamp": "2026-03-19T04:14:07.318734+00:00",
    "status": "completed",
    "type": "evolve",
    "goal": "Replaced raw SVG bar charts in Agent Overview with interactive Altair charts \u2014 goal durations with trend line, cycle velocity colored by type with rolling average. Zero new dependencies.",
    "actions": [],
    "summary": "Replaced raw SVG bar charts in Agent Overview with interactive Altair charts \u2014 goal durations with trend line, cycle velocity colored by type with rolling average. Zero new dependencies.",
    "category": "observability"
  },
  {
    "cycle": 3,
    "timestamp": "2026-03-19T04:20:47.399188+00:00",
    "status": "completed",
    "type": "evolve",
    "goal": "Updated stale prompts: goal.md routing table (+6 PA domain entries), server.md TAB_REGISTRY (4\u219210 tabs), research.md (+5 script refs), AGENTS.md (+4 missing scripts). Prompts now match actual portal state after rapid expansion in cycles 0-2.",
    "actions": [
      "Added email/calendar/contacts/tasks/credentials/scheduler routing to goal.md",
      "Updated server.md TAB_REGISTRY from 4 to 10 tabs across 3 groups with correct modules",
      "Added 5 missing script references to research.md local resources table",
      "Updated AGENTS.md directory tree with 4 missing scripts and services_tab.py"
    ],
    "summary": "Updated stale prompts: goal.md routing table (+6 PA domain entries), server.md TAB_REGISTRY (4\u219210 tabs), research.md (+5 script refs), AGENTS.md (+4 missing scripts). Prompts now match actual portal state after rapid expansion in cycles 0-2.",
    "category": "prompt_evolution"
  },
  {
    "cycle": 4,
    "timestamp": "2026-03-19T04:22:40.401795+00:00",
    "status": "completed",
    "type": "goal",
    "goal": "Process user message: add post-goal learning instruction to AGENTS.md",
    "actions": [
      "Added 'Post-Goal Learning (Mandatory)' section to AGENTS.md",
      "Section requires documenting: approach taken, key decisions, reusable patterns, pitfalls",
      "Responded to user via outbox confirming the change"
    ],
    "summary": "Added mandatory post-goal learning section to AGENTS.md per user request. Agent will now document approach, decisions, patterns, and pitfalls after every completed goal to build institutional knowledge.",
    "category": "message"
  },
  {
    "cycle": 5,
    "timestamp": "2026-03-19T04:22:56.875917+00:00",
    "status": "completed",
    "type": "goal",
    "goal": "Added mandatory post-goal learning section to AGENTS.md per user request. Agent will now document approach, decisions, patterns, and pitfalls after every completed goal.",
    "actions": [],
    "summary": "Added mandatory post-goal learning section to AGENTS.md per user request. Agent will now document approach, decisions, patterns, and pitfalls after every completed goal."
  },
  {
    "cycle": 6,
    "timestamp": "2026-03-19T04:31:00+00:00",
    "status": "completed",
    "type": "evolve",
    "goal": "Efficiency: optimize load_system_info and fix deprecation warnings across portal",
    "actions": [
      "Replaced os.walk workspace size calc with du -sb subprocess (10-100x faster)",
      "Converted last TTL @_cache(ttl=60) to time-bucketed cache (10s windows) in load_system_info",
      "Removed unused _cache import from system.py (dead code cleanup)",
      "Fixed 3x deprecated use_container_width=True -> width='stretch' in overview.py and emails.py",
      "Fixed pandas FutureWarning: added utc=True to pd.to_datetime in emails.py"
    ],
    "summary": "Optimized load_system_info() \u2014 replaced os.walk with du -sb for workspace size (10-100x faster), completed mtime conversion by eliminating the last TTL cache decorator, fixed 3 Streamlit deprecation warnings and 1 pandas FutureWarning.",
    "category": "efficiency"
  },
  {
    "cycle": 6,
    "timestamp": "2026-03-19T04:32:27.895961+00:00",
    "status": "completed",
    "type": "evolve",
    "goal": "Optimized load_system_info() \u2014 replaced os.walk with du -sb for workspace size (10-100x faster), completed mtime conversion by eliminating last TTL cache decorator, fixed 3 Streamlit deprecation warnings and 1 pandas FutureWarning.",
    "actions": [],
    "summary": "Optimized load_system_info() \u2014 replaced os.walk with du -sb for workspace size (10-100x faster), completed mtime conversion by eliminating last TTL cache decorator, fixed 3 Streamlit deprecation warnings and 1 pandas FutureWarning.",
    "category": "efficiency"
  },
  {
    "cycle": 8,
    "timestamp": "2026-03-19T04:36:56.357864+00:00",
    "status": "completed",
    "type": "evolve",
    "goal": "Reliability hardening: fixed unsafe bracket indexing in tasks.py, overview.py, and goal.py; added type validation to JSON loaders; safe .index() fallbacks; write error handling for tasks and contacts save operations.",
    "actions": [],
    "summary": "Reliability hardening: fixed unsafe bracket indexing in tasks.py, overview.py, and goal.py; added type validation to JSON loaders; safe .index() fallbacks; write error handling for tasks and contacts save operations.",
    "category": "reliability"
  },
  {
    "cycle": 9,
    "timestamp": "2026-03-19T04:43:44.823632+00:00",
    "status": "completed",
    "type": "evolve",
    "category": "observability",
    "summary": "Added Cycle Timeline (Gantt chart) and Evolution Trend (cumulative stacked area chart) to Agent Overview. Timeline shows when each cycle ran with category-colored bars; trend chart shows cumulative category balance over time.",
    "actions": [
      "Added _render_cycle_timeline() \u2014 Gantt-style horizontal bar chart showing cycle start/end times, colored by category with tooltips",
      "Added _render_evolution_trend() \u2014 cumulative stacked area chart tracking evolution category balance across cycles",
      "Both charts use consistent _CATEGORY_COLORS, handle edge cases (< 2 cycles), and show last 30 cycles max",
      "Trend chart includes percentage summary line below the chart"
    ],
    "outcome": "Two new interactive Altair visualizations in Agent Overview providing temporal insight into agent activity and evolution balance",
    "learnings": {
      "approach": "Added two focused Altair charts to existing overview.py using helper functions called from render()",
      "key_decisions": "Used Gantt bars (mark_bar with x/x2 temporal encoding) for timeline instead of scatter; used stacked area for trend to show both individual and total",
      "reusable_patterns": "Altair x/x2 temporal encoding for Gantt charts; cumulative counting with step-after interpolation for trend visualization",
      "pitfalls": "None \u2014 straightforward addition building on existing Altair patterns from cycle 2"
    }
  },
  {
    "cycle": 9,
    "timestamp": "2026-03-19T04:43:48.988218+00:00",
    "status": "completed",
    "type": "evolve",
    "goal": "Added Cycle Timeline and Evolution Trend charts to Agent Overview",
    "actions": [],
    "summary": "Added Cycle Timeline and Evolution Trend charts to Agent Overview",
    "category": "observability"
  },
  {
    "cycle": 10,
    "timestamp": "2026-03-19T04:57:06.196967+00:00",
    "status": "completed",
    "type": "evolve",
    "goal": "Added daily-briefing.py script that aggregates tasks, goals, cycles, scheduled items, inbox, and system health into actionable briefings. Supports text, JSON, HTML, and outbox output modes. Added skill docs and goal routing entry.",
    "actions": [
      "Created scripts/daily-briefing.py with 4 output modes (text/json/html/outbox)",
      "Created .claude/skills/daily-briefing/SKILL.md",
      "Added daily briefing routing to prompts/goal.md",
      "Registered Daily Briefing Generator capability"
    ],
    "summary": "Added daily-briefing.py script that aggregates tasks, goals, cycles, scheduled items, inbox, and system health into actionable briefings. Supports text, JSON, HTML, and outbox output modes. Added skill docs and goal routing entry.",
    "category": "capability"
  }
]
```

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

### chat_history.json

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

---

## `/agent/messages/`

### inbox.json

```json
[]
```

Entry:

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

- `type`: `goal` | `message` | `event`
- `source`: `user` | `scheduler` | `telegram` | `whatsapp` | `webhook` | other
- Scheduler-injected entries may include `task_id`.
- `event` entries (source `webhook`) carry the sanitized HTTP payload in `content`: method, path, filtered headers, and body (truncated at 4 KB). Full payload is in `webhook_receiver.log`.

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
| **Cycle type** | `evolve` / `goal` / `self-heal` |
| **Category** | `reliability` / `observability` / `capability` / `efficiency` / `prompt_evolution` |
| **Null fields** | Use `null`, not empty string, for absent optional values |
| **Arrays** | Default to `[]`; objects default to `{}` |
| **Atomic writes** | All files use write-to-temp-then-rename to prevent corruption |
| **File locking** | inbox/outbox use `fcntl.flock()` for exclusive access |
