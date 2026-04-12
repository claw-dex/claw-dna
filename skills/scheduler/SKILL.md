---
name: scheduler
description: Manage and evaluate scheduled tasks — add, remove, enable, disable, run now, test cron patterns, forecast upcoming fires, and inject due tasks into inbox. Supports interval (every N minutes), once (fire once then disable), and cron-like schedule types. Use --add to create tasks from CLI, --run-now to trigger immediately, --test to preview cron fire times, --forecast to see upcoming timeline, --check at cycle start, or --list to see all tasks.
---

# scheduler

**Path:** `scripts/scheduler.py`

Full CRUD management for scheduled tasks in `/agent/memory/scheduled_tasks.json`. Check which tasks are due and inject them into `inbox.json`. Supports three schedule types: `interval`, `once`, `cron`.

## Arguments / Subcommands

| Flag | Description |
|------|-------------|
| `--check` | Evaluate all tasks and inject any that are due into inbox.json |
| `--list` | List all scheduled tasks with schedule, status, and content |
| `--json` | List all tasks as JSON |
| `--add` | Add a new scheduled task (requires `--id` and `--goal`/`--message` and a schedule) |
| `--remove` | Remove a task by ID (requires `--id`) |
| `--enable` | Enable a disabled task (requires `--id`) |
| `--disable` | Disable a task without removing it (requires `--id`) |
| `--run-now` | Immediately inject a task into inbox, bypassing schedule (requires `--id`) |
| `--test` | Test a cron pattern — shows next 5 fire times (requires `--cron`) |
| `--forecast` | Show timeline of all upcoming task fires (default: next 48h) |
| `--history` | Show execution history for a specific task (requires `--id`) |
| `--stats` | Show aggregate execution stats (total, injected, errors) for all tasks |

### Forecast Options

| Flag | Description |
|------|-------------|
| `--hours N` | Forecast horizon in hours (default: 48) |

### Add Options

| Flag | Description |
|------|-------------|
| `--id ID` | Unique task identifier |
| `--goal TEXT` | Goal content (sets type=goal) |
| `--message TEXT` | Message content (sets type=message) |
| `--every Nm\|Nh\|Nd` | Interval schedule (e.g., `30m`, `2h`, `1d`) |
| `--cron PATTERN` | Cron pattern: `minute hour dom month dow` |
| `--once DATETIME` | One-time ISO 8601 datetime |
| `--priority N` | Priority 1-5 (default: 3) |

## Examples

```bash
# Check for due tasks and inject them (run at cycle start)
uv run python scripts/scheduler.py --check

# List all tasks
uv run python scripts/scheduler.py --list

# Add a daily briefing at 9am UTC
uv run python scripts/scheduler.py --add --id daily-briefing --goal "Generate daily briefing" --cron "0 9 * * *"

# Add a health check every 30 minutes
uv run python scripts/scheduler.py --add --id health-30m --goal "Run health check" --every 30m

# Add a one-time reminder
uv run python scripts/scheduler.py --add --id deploy-check --goal "Verify deployment" --once "2026-04-01T14:00+08:00"

# Test a cron pattern (shows next 5 fire times)
uv run python scripts/scheduler.py --test --cron "0 9 * * 1"

# Cron with ranges, lists, and steps
uv run python scripts/scheduler.py --add --id biz-hours-check --goal "Check status" --cron "*/15 9-17 * * 1-5"
uv run python scripts/scheduler.py --test --cron "0,30 8-18 * * 1-5"

# Disable a task temporarily
uv run python scripts/scheduler.py --disable --id morning-briefing

# Re-enable a task
uv run python scripts/scheduler.py --enable --id morning-briefing

# Trigger a task immediately (bypass schedule)
uv run python scripts/scheduler.py --run-now --id morning-briefing

# Remove a task permanently
uv run python scripts/scheduler.py --remove --id old-task

# Show upcoming task fires (next 48h default)
uv run python scripts/scheduler.py --forecast

# Show upcoming task fires for next 7 days
uv run python scripts/scheduler.py --forecast --hours 168

# Export as JSON
uv run python scripts/scheduler.py --json
```

## Schedule Types

| Type       | Key Fields                       | Behavior                                                    |
|------------|----------------------------------|-------------------------------------------------------------|
| `interval` | `interval_minutes`               | Fires every N minutes since `last_run`                      |
| `once`     | `run_at` (ISO 8601)              | Fires once at the specified time, then auto-disables        |
| `cron`     | `schedule` (min hr dom mon dow)  | Full cron syntax: `*`, `N`, `N-M`, `N,M`, `*/S`, `N-M/S`   |

## Schema (`scheduled_tasks.json`)

Fields: `id` (required), `schedule_type` (interval/once/cron), `type` (goal/message, default goal),
`content` (task description), `enabled` (default true), `priority` (1-5, optional, default 3),
`last_run` (set automatically), `created_at` (set on add).
Schedule-specific: `interval_minutes` for interval, `run_at` for once, `schedule` for cron.