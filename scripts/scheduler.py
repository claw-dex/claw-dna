#!/usr/bin/env python3
"""
scheduler.py — Evaluate and inject due scheduled tasks into inbox.json.

Reads /agent/memory/scheduled_tasks.json, checks which tasks are due,
and writes them to /agent/messages/inbox.json as inbox entries.

Supports three schedule types:
  - "interval": fires every N minutes (based on last_run)
  - "once": fires once at a specific datetime, then auto-disables
  - "cron": simple cron-like patterns (minute hour day_of_month month day_of_week)
             Supports '*' wildcards and integer matches only (no ranges/lists).

Usage:
    uv run python scripts/scheduler.py --check     # evaluate and inject due tasks
    uv run python scripts/scheduler.py --list       # list all scheduled tasks
    uv run python scripts/scheduler.py --help       # show this help

Schema for scheduled_tasks.json:
    [
      {
        "id": "unique-id",
        "schedule_type": "interval",   # interval | once | cron
        "interval_minutes": 60,        # for schedule_type=interval
        "schedule": "0 9 * * *",       # for schedule_type=cron (min hour dom mon dow)
        "run_at": "2026-03-01T09:00",  # for schedule_type=once
        "type": "goal",                # goal | message (matches inbox type)
        "content": "Description of what the agent should do",
        "enabled": true,
        "last_run": null,
        "priority": 3                  # optional, 1-5 (default 3)
      }
    ]

Exit codes: 0 = success, 1 = error
"""

import fcntl
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

MEMORY = Path("/agent/memory")
MESSAGES = Path("/agent/messages")
TASKS_PATH = MEMORY / "scheduled_tasks.json"
INBOX_PATH = MESSAGES / "inbox.json"


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else []


def write_atomic(path: Path, data):
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _cron_matches(pattern: str, now: datetime) -> bool:
    """Check if a simple cron pattern matches the current time.

    Pattern format: "minute hour day_of_month month day_of_week"
    Supports '*' (any) and integer values only.
    """
    parts = pattern.strip().split()
    if len(parts) != 5:
        return False

    # Convert Python weekday (0=Mon) to cron weekday (0=Sun)
    cron_dow = (now.weekday() + 1) % 7
    fields = [
        (parts[0], now.minute),
        (parts[1], now.hour),
        (parts[2], now.day),
        (parts[3], now.month),
        (parts[4], cron_dow),  # 0=Sunday in cron convention
    ]
    for pat, val in fields:
        if pat == "*":
            continue
        try:
            if int(pat) != val:
                return False
        except ValueError:
            return False
    return True


def _has_cron_match_since(pattern: str, since: datetime, now: datetime) -> bool:
    """Check if any minute between (since, now] matches the cron pattern.

    Scans backward from now for efficiency. Caps at 7 days.
    """
    max_minutes = 10080  # 7 days
    check = now.replace(second=0, microsecond=0)
    cutoff = since.replace(second=0, microsecond=0)

    for _ in range(max_minutes):
        if check <= cutoff:
            return False
        if _cron_matches(pattern, check):
            return True
        check -= timedelta(minutes=1)
    return False


def _is_due(task: dict, now: datetime) -> bool:
    """Check if a scheduled task is due to run."""
    if not task.get("enabled", True):
        return False

    task_type = task.get("schedule_type", "")
    last_run_str = task.get("last_run")
    last_run = None
    if last_run_str:
        try:
            last_run = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
        except Exception:
            pass

    if task_type == "interval":
        interval_min = task.get("interval_minutes", 0)
        if interval_min <= 0:
            return False
        if last_run is None:
            return True  # never run before
        elapsed = (now - last_run).total_seconds() / 60
        return elapsed >= interval_min

    elif task_type == "once":
        if last_run is not None:
            return False  # already ran
        run_at_str = task.get("run_at", "")
        if not run_at_str:
            return False
        try:
            run_at = datetime.fromisoformat(run_at_str.replace("Z", "+00:00"))
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)
            return now >= run_at
        except Exception:
            return False

    elif task_type == "cron":
        schedule = task.get("schedule", "")
        if not schedule:
            return False
        if last_run is not None:
            elapsed = (now - last_run).total_seconds()
            if elapsed < 60:
                return False  # debounce
            return _has_cron_match_since(schedule, last_run, now)
        else:
            # Never run before — check if pattern matched recently (last 24h)
            return _has_cron_match_since(
                schedule, now - timedelta(hours=24), now
            )

    return False


def check_and_inject():
    """Evaluate scheduled tasks and inject due ones into inbox."""
    tasks = load_json(TASKS_PATH, [])
    if not isinstance(tasks, list) or not tasks:
        return 0

    now = datetime.now(timezone.utc)
    injected = 0
    due_entries = []

    for task in tasks:
        if not _is_due(task, now):
            continue

        task_id = task.get("id", "unknown")
        content_text = task.get("content", "")
        if not content_text:
            continue

        entry = {
            "type": task.get("type", "goal"),
            "content": f"[Scheduled: {task_id}] {content_text}",
            "timestamp": now.isoformat(),
            "received_at": now.isoformat(),
            "source": "scheduler",
            "task_id": task_id,
        }
        priority = task.get("priority")
        if priority is not None:
            entry["priority"] = max(1, min(5, int(priority)))
        due_entries.append(entry)

        # Update task state
        task["last_run"] = now.isoformat()
        if task.get("schedule_type") == "once":
            task["enabled"] = False
        injected += 1
        print(f"  [scheduler] injected: {task_id} — {content_text[:80]}")

    if due_entries:
        # Use file locking to match AtomicJSON protocol in write.py
        lock_path = str(INBOX_PATH) + ".lock"
        with open(lock_path, "a+") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            inbox = load_json(INBOX_PATH, [])
            inbox.extend(due_entries)
            write_atomic(INBOX_PATH, inbox)
        write_atomic(TASKS_PATH, tasks)

    return injected


def list_tasks():
    """Print all scheduled tasks."""
    tasks = load_json(TASKS_PATH, [])
    if not tasks:
        print("No scheduled tasks.")
        return
    print(f"{'ID':<20} {'Type':<10} {'Enabled':<8} {'Last Run':<22} {'Schedule/Interval'}")
    print("-" * 85)
    for t in tasks:
        tid = t.get("id", "?")[:20]
        ttype = t.get("schedule_type", "?")
        enabled = "yes" if t.get("enabled", True) else "no"
        last = (t.get("last_run") or "never")[:22]
        if ttype == "interval":
            sched = f"every {t.get('interval_minutes', '?')}m"
        elif ttype == "cron":
            sched = t.get("schedule", "?")
        elif ttype == "once":
            sched = t.get("run_at", "?")
        else:
            sched = "?"
        print(f"{tid:<20} {ttype:<10} {enabled:<8} {last:<22} {sched}")


def main():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)
    elif "--list" in args:
        list_tasks()
    elif "--check" in args:
        n = check_and_inject()
        if n > 0:
            print(f"  [scheduler] {n} task(s) injected into inbox")
    else:
        print("Usage: scheduler.py --check | --list | --help")
        sys.exit(1)


if __name__ == "__main__":
    main()
