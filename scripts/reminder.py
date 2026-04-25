#!/usr/bin/env python3
"""
reminder.py — Personal reminder manager for the agent.

Creates, lists, and manages reminders by writing entries into
scheduled_tasks.json. Reminders are scheduled tasks with id prefix
"reminder-" and source field "reminder" for easy filtering.

Supports one-time reminders (fire once then disable) and recurring
reminders (interval-based or cron-based).

Usage:
    uv run python scripts/reminder.py add --text "Call dentist" --at "2026-03-27T15:00"
    uv run python scripts/reminder.py add --text "Stretch" --in 30m       # one-shot, 30 min from now
    uv run python scripts/reminder.py add --text "Tea" --in 2h            # one-shot, 2 hours from now
    uv run python scripts/reminder.py add --text "Stand up" --every 60
    uv run python scripts/reminder.py add --text "Weekly review" --cron "0 9 * * 1"
    uv run python scripts/reminder.py list
    uv run python scripts/reminder.py list --json
    uv run python scripts/reminder.py delete --id reminder-abc123
    uv run python scripts/reminder.py clear              # remove all fired/disabled reminders

Options for 'add':
    --text TEXT        Reminder message (required)
    --at DATETIME      Fire once at this time (ISO 8601, e.g. 2026-03-27T15:00)
                       Interpreted in agent's configured timezone if no offset given
    --in DURATION      Fire once N from now. Format: Nm | Nh | Nd (minutes/hours/days)
    --every MINUTES    Fire every N minutes (recurring)
    --cron PATTERN     Fire on cron schedule (min hour dom mon dow)
    --priority N       Priority 1-5 (default 1 = highest)

Exit codes: 0 = success, 1 = error
"""

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from scheduler import TASKS_PATH, _load_tasks, timed_flock, write_atomic


def _parse_duration_to_minutes(value: str) -> int:
    """Parse '30m' / '2h' / '1d' (or bare integer = minutes) into minutes.

    Raises ValueError on bad input or non-positive durations.
    """
    if not value:
        raise ValueError("empty duration")
    v = value.strip().lower()
    if v.endswith("m"):
        n = int(v[:-1])
    elif v.endswith("h"):
        n = int(v[:-1]) * 60
    elif v.endswith("d"):
        n = int(v[:-1]) * 1440
    else:
        n = int(v)
    if n <= 0:
        raise ValueError(f"duration must be positive: {value!r}")
    return n


def _resolve_in_to_isoformat(duration: str) -> str:
    """Convert a relative duration (e.g. '30m', '2h') to an absolute ISO datetime.

    Anchored to now() in the agent's configured timezone when available, else UTC.
    """
    minutes = _parse_duration_to_minutes(duration)
    try:
        import zoneinfo

        tz = zoneinfo.ZoneInfo(os.environ.get("TZ", "UTC"))
    except Exception:
        tz = timezone.utc
    return (datetime.now(tz) + timedelta(minutes=minutes)).isoformat()


@contextmanager
def _tasks_lock():
    """Acquire the scheduled_tasks.json flock using scheduler's timed_flock."""
    lock_path = str(TASKS_PATH) + ".lock"
    with open(lock_path, "a+") as lock_f:
        with timed_flock(lock_f):
            yield


def generate_id(text: str) -> str:
    """Generate a short unique reminder ID from text + timestamp."""
    h = hashlib.sha256(f"{text}{datetime.now().isoformat()}".encode()).hexdigest()[:8]
    return f"reminder-{h}"


def parse_datetime(s: str) -> str:
    """Parse a datetime string, adding UTC offset if missing."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        # Try to use the agent's configured timezone
        try:
            import zoneinfo

            tz_name = os.environ.get("TZ", "UTC")
            tz = zoneinfo.ZoneInfo(tz_name)
            dt = dt.replace(tzinfo=tz)
        except Exception:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def cmd_add(args: list) -> int:
    text = None
    at_time = None
    in_duration = None
    every_min = None
    cron_pat = None
    priority = 1

    i = 0
    while i < len(args):
        if args[i] == "--text" and i + 1 < len(args):
            text = args[i + 1]
            i += 2
        elif args[i] == "--at" and i + 1 < len(args):
            at_time = args[i + 1]
            i += 2
        elif args[i] == "--in" and i + 1 < len(args):
            in_duration = args[i + 1]
            i += 2
        elif args[i] == "--every" and i + 1 < len(args):
            every_min = int(args[i + 1])
            i += 2
        elif args[i] == "--cron" and i + 1 < len(args):
            cron_pat = args[i + 1]
            i += 2
        elif args[i] == "--priority" and i + 1 < len(args):
            priority = max(1, min(5, int(args[i + 1])))
            i += 2
        else:
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            return 1

    if not text:
        print("Error: --text is required", file=sys.stderr)
        return 1

    # --in is just a relative spelling of --at; resolve it now.
    if in_duration is not None:
        if at_time is not None:
            print("Error: --in and --at are mutually exclusive", file=sys.stderr)
            return 1
        try:
            at_time = _resolve_in_to_isoformat(in_duration)
        except ValueError as e:
            print(
                f"Error: invalid --in value {in_duration!r} ({e}). "
                "Use Nm, Nh, or Nd (e.g. 30m, 2h, 1d).",
                file=sys.stderr,
            )
            return 1

    # Exactly one schedule type required
    schedule_count = sum(1 for x in [at_time, every_min, cron_pat] if x is not None)
    if schedule_count == 0:
        print("Error: specify --at, --in, --every, or --cron", file=sys.stderr)
        return 1
    if schedule_count > 1:
        print("Error: specify only one of --at, --every, --cron", file=sys.stderr)
        return 1

    rid = generate_id(text)
    task = {
        "id": rid,
        "source": "reminder",
        "type": "message",
        "content": f"Reminder: {text}",
        "enabled": True,
        "last_run": None,
        "priority": priority,
    }

    if at_time:
        task["schedule_type"] = "once"
        task["run_at"] = parse_datetime(at_time)
    elif every_min:
        task["schedule_type"] = "interval"
        task["interval_minutes"] = every_min
    elif cron_pat:
        task["schedule_type"] = "cron"
        task["schedule"] = cron_pat

    with _tasks_lock():
        tasks = _load_tasks()
        tasks.append(task)
        write_atomic(TASKS_PATH, tasks)

    print(f"Created reminder: {rid}")
    print(f"  Text: {text}")
    if at_time:
        print(f"  When: {task['run_at']} (once)")
    elif every_min:
        print(f"  When: every {every_min} minutes")
    elif cron_pat:
        print(f"  When: cron {cron_pat}")
    return 0


def cmd_list(args: list) -> int:
    as_json = "--json" in args
    tasks = _load_tasks()
    reminders = [
        t
        for t in tasks
        if t.get("source") == "reminder" or t.get("id", "").startswith("reminder-")
    ]

    if not reminders:
        if as_json:
            print("[]")
        else:
            print("No reminders set.")
        return 0

    if as_json:
        print(json.dumps(reminders, indent=2))
        return 0

    print(f"{'ID':<22} {'Enabled':<8} {'Type':<10} {'Schedule':<24} Message")
    print("-" * 100)
    for r in reminders:
        rid = r.get("id", "?")[:22]
        enabled = "yes" if r.get("enabled", True) else "fired"
        stype = r.get("schedule_type", "?")
        if stype == "once":
            sched = (r.get("run_at") or "?")[:24]
        elif stype == "interval":
            sched = f"every {r.get('interval_minutes', '?')}m"
        elif stype == "cron":
            sched = r.get("schedule", "?")[:24]
        else:
            sched = "?"
        content = r.get("content", "")[:40]
        print(f"{rid:<22} {enabled:<8} {stype:<10} {sched:<24} {content}")
    return 0


def cmd_delete(args: list) -> int:
    rid = None
    i = 0
    while i < len(args):
        if args[i] == "--id" and i + 1 < len(args):
            rid = args[i + 1]
            i += 2
        else:
            i += 1

    if not rid:
        print("Error: --id is required", file=sys.stderr)
        return 1

    with _tasks_lock():
        tasks = _load_tasks()
        original_len = len(tasks)
        tasks = [t for t in tasks if t.get("id") != rid]

        if len(tasks) == original_len:
            print(f"Reminder not found: {rid}", file=sys.stderr)
            return 1

        write_atomic(TASKS_PATH, tasks)
    print(f"Deleted: {rid}")
    return 0


def cmd_clear(args: list) -> int:
    """Remove all fired/disabled reminders."""
    with _tasks_lock():
        tasks = _load_tasks()
        before = len(tasks)
        tasks = [
            t
            for t in tasks
            if not (
                (
                    t.get("source") == "reminder"
                    or t.get("id", "").startswith("reminder-")
                )
                and not t.get("enabled", True)
            )
        ]
        removed = before - len(tasks)
        write_atomic(TASKS_PATH, tasks)
    print(f"Cleared {removed} fired reminder(s)")
    return 0


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("--help", "-h"):
        print(__doc__)
        return 0

    cmd = args[0]
    rest = args[1:]

    if cmd == "add":
        return cmd_add(rest)
    elif cmd == "list":
        return cmd_list(rest)
    elif cmd == "delete":
        return cmd_delete(rest)
    elif cmd == "clear":
        return cmd_clear(rest)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Commands: add, list, delete, clear")
        return 1


# --- Public API (for direct import by services) ---


def add_reminder(
    text: str,
    *,
    at: str | None = None,
    in_: str | None = None,
    every: int | None = None,
    cron: str | None = None,
    priority: int = 1,
) -> str | None:
    """Add a reminder programmatically. Returns the reminder ID on success, None on error.

    Exactly one of *at* (ISO datetime string), *in_* (relative duration like '30m',
    '2h', '1d'), *every* (minutes), or *cron* (pattern) must be provided. *in_* is
    a one-shot reminder resolved to an absolute datetime relative to now.
    """
    try:
        if in_ is not None:
            if at is not None:
                return None
            try:
                at = _resolve_in_to_isoformat(in_)
            except ValueError:
                return None
        schedule_count = sum(1 for x in [at, every, cron] if x is not None)
        if not text or schedule_count != 1:
            return None

        rid = generate_id(text)
        task: dict = {
            "id": rid,
            "source": "reminder",
            "type": "message",
            "content": f"Reminder: {text}",
            "enabled": True,
            "last_run": None,
            "priority": max(1, min(5, priority)),
        }
        if at is not None:
            task["schedule_type"] = "once"
            task["run_at"] = parse_datetime(at)
        elif every is not None:
            task["schedule_type"] = "interval"
            task["interval_minutes"] = every
        else:
            task["schedule_type"] = "cron"
            task["schedule"] = cron

        with _tasks_lock():
            tasks = _load_tasks()
            tasks.append(task)
            write_atomic(TASKS_PATH, tasks)
        return rid
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
