---
name: reminder
description: Create, list, and manage personal reminders. Reminders fire as inbox messages via the scheduler. Supports one-time, recurring (interval), and cron-based reminders.
---

# Reminder

**Script:** `uv run python scripts/reminder.py <command> [options]`

## Commands

### add — Create a reminder

```bash
# One-time reminder at a specific time
uv run python scripts/reminder.py add --text "Call dentist" --at "2026-03-27T15:00"

# One-time reminder N from now (Nm | Nh | Nd)
uv run python scripts/reminder.py add --text "Stretch" --in 30m
uv run python scripts/reminder.py add --text "Tea break" --in 2h
uv run python scripts/reminder.py add --text "Follow up" --in 1d

# Recurring reminder every N minutes
uv run python scripts/reminder.py add --text "Stand up and stretch" --every 60

# Cron-based reminder (e.g., every Monday at 9am)
uv run python scripts/reminder.py add --text "Weekly review" --cron "0 9 * * 1"

# With priority (1=highest, 5=lowest, default 1)
uv run python scripts/reminder.py add --text "Important meeting" --at "2026-03-27T14:00" --priority 1
```

**Schedule flags (exactly one required):**

- `--at <ISO datetime>` — fire once at an absolute time
- `--in <Nm|Nh|Nd>` — fire once N minutes / hours / days from now (resolved to an absolute datetime in the agent's TZ)
- `--every <minutes>` — recurring interval
- `--cron "<min hour dom mon dow>"` — cron pattern

### list — Show all reminders

```bash
uv run python scripts/reminder.py list          # human-readable table
uv run python scripts/reminder.py list --json   # JSON output
```

### delete — Remove a reminder by ID

```bash
uv run python scripts/reminder.py delete --id reminder-abc12345
```

### clear — Remove all fired/disabled reminders

```bash
uv run python scripts/reminder.py clear
```

## How It Works

Reminders are stored as entries in `/agent/memory/scheduled_tasks.json` with:

- ID prefix `reminder-` and `"source": "reminder"` for filtering
- `"type": "message"` so they appear as inbox messages when fired

When a reminder fires, it injects a message into `inbox.json` like:

```
[Scheduled: reminder-abc12345] Reminder: Call dentist
```

One-time reminders auto-disable after firing. Recurring reminders keep firing on schedule.

## Datetime Handling

- ISO 8601 format: `2026-03-27T15:00` or `2026-03-27T15:00:00+08:00`
- If no timezone offset given, uses the agent's TZ environment variable (default UTC)
