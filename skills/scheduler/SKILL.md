---
name: scheduler
description: Evaluate and inject due scheduled tasks (cron) from scheduled_tasks.json into inbox.json. Supports interval (every N minutes), once (fire once then disable), and cron-like schedule types. Use at cycle start to check for due tasks (--check), or to list all scheduled tasks and their next run times (--list). Typically invoked automatically by cycle-start.py.
---

# scheduler

**Path:** `scripts/scheduler.py`

Reads `/agent/memory/scheduled_tasks.json`, checks which tasks are due, and injects them into `inbox.json`. Each task's `type` field (goal/message) is passed through to the inbox entry. Supports three schedule types: `interval` (every N minutes), `once` (fire once then auto-disable), `cron` (simple cron-like patterns).

## Arguments / Subcommands

| Flag | Description |
|------|-------------|
| `--check` | Evaluate all tasks and inject any that are due into inbox.json |
| `--list` | List all scheduled tasks with their schedule and next run time |

## Examples

```bash
# Check for due tasks and inject them (run at cycle start)
uv run python scripts/scheduler.py --check

# See all scheduled tasks and when they next fire
uv run python scripts/scheduler.py --list
```

## Schedule Types

| Type       | Key Fields                       | Behavior                                                    |
|------------|----------------------------------|-------------------------------------------------------------|
| `interval` | `interval_minutes`               | Fires every N minutes since `last_run`                      |
| `once`     | `run_at` (ISO 8601)              | Fires once at the specified time, then auto-disables        |
| `cron`     | `schedule` (min hr dom mon dow)  | Simple cron patterns (`*` and integers only)                |

## Schema (`scheduled_tasks.json`)

Fields: `id` (required), `schedule_type` (interval/once/cron), `type` (goal/message, default goal),
`content` (task description), `enabled` (default true), `priority` (1–5, optional, default 3),
`last_run` (set automatically).
Schedule-specific: `interval_minutes` for interval, `run_at` for once, `schedule` for cron.

```json
[
  {
    "id": "unique-id",
    "schedule_type": "interval",
    "interval_minutes": 60,
    "type": "goal",
    "content": "Description of what the agent should do",
    "enabled": true,
    "last_run": null,
    "priority": 3
  }
]
```

## Creating a Scheduled Task

Append an entry to `/agent/memory/scheduled_tasks.json`. The scheduler picks it up on
the next `--check` run. When a task fires, it appears in the inbox as
`[Scheduled: <id>] <content>` with source `"scheduler"` and the task's `type` (goal/message).
