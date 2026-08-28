#!/usr/bin/env python3
"""
scheduler.py — Manage and evaluate scheduled tasks for the agent.

Reads /agent/memory/scheduled_tasks.json, checks which tasks are due,
and writes them to /agent/messages/inbox.json as inbox entries.

Supports three schedule types:
  - "interval": fires every N minutes (based on last_run)
  - "once": fires once at a specific datetime, then auto-disables
  - "cron": cron patterns (minute hour day_of_month month day_of_week)
             Supports: * (any), N (exact), N-M (range), N,M,O (list),
             */S (step), N-M/S (range+step), and combinations like 1-5,10,15-20.

Usage:
    uv run python scripts/scheduler.py --check                        # evaluate and inject due tasks
    uv run python scripts/scheduler.py --list                         # list all scheduled tasks
    uv run python scripts/scheduler.py --add --id ID --goal CONTENT   # add a new scheduled task
        [--every Nm | --cron "m h dom mon dow" | --once ISO_DATETIME]
        [--priority N] [--type goal|message]
    uv run python scripts/scheduler.py --remove --id ID               # remove a task by ID
    uv run python scripts/scheduler.py --enable --id ID               # enable a disabled task
    uv run python scripts/scheduler.py --disable --id ID              # disable a task
    uv run python scripts/scheduler.py --run-now --id ID              # immediately inject task into inbox
    uv run python scripts/scheduler.py --test --cron "0 9 * * *"      # test when a cron fires next
    uv run python scripts/scheduler.py --forecast                     # show next 48h timeline
    uv run python scripts/scheduler.py --forecast --hours 72          # show next 72h timeline
    uv run python scripts/scheduler.py --history --id ID              # show execution history for a task
    uv run python scripts/scheduler.py --stats                        # show aggregate execution stats
    uv run python scripts/scheduler.py --json                         # list tasks as JSON
    uv run python scripts/scheduler.py --help                         # show this help

Examples:
    # Run daily briefing at 9am UTC
    uv run python scripts/scheduler.py --add --id daily-briefing --goal "Generate daily briefing" --cron "0 9 * * *"

    # Run health check every 30 minutes
    uv run python scripts/scheduler.py --add --id health-30m --goal "Run health check" --every 30m

    # One-time task at specific time
    uv run python scripts/scheduler.py --add --id deploy-check --goal "Verify deployment" --once "2026-04-01T14:00+08:00"

    # Remove a task
    uv run python scripts/scheduler.py --remove --id health-30m

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
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

MEMORY = Path("/agent/memory")
MESSAGES = Path("/agent/messages")
TASKS_PATH = MEMORY / "scheduled_tasks.json"
INBOX_PATH = MESSAGES / "inbox.json"

LOCK_TIMEOUT_SECONDS = 10
EXEC_HISTORY_MAX = 20  # max entries per task in execution_history ring buffer
INBOX_DEDUP_WINDOW_SECONDS = (
    60  # suppress re-inject if task_id already in inbox within this window
)


@contextmanager
def timed_flock(fd, timeout=LOCK_TIMEOUT_SECONDS):
    """Acquire an exclusive flock with a timeout to prevent indefinite hangs.

    Uses LOCK_NB + busy-wait (safe in multi-threaded callers, unlike SIGALRM).
    """
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Could not acquire file lock within {timeout}s")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else []


def _load_tasks() -> list[dict]:
    """Load scheduled tasks with type safety: returns only valid dict items."""
    raw = load_json(TASKS_PATH, [])
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, dict)]


def write_atomic(path: Path, data):
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _record_execution(task: dict, status: str, note: str = ""):
    """Append an execution record to a task's history ring buffer."""
    history = task.get("execution_history", [])
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status,  # "injected", "skipped", "run_now", "error"
            "note": note[:200] if note else "",
        }
    )
    # Trim to ring buffer max
    task["execution_history"] = history[-EXEC_HISTORY_MAX:]


def _recent_inbox_task_ids(now: datetime, window_seconds: int) -> set[str]:
    """Return task_ids that already appear in inbox.json within *window_seconds*.

    Used as belt-and-suspenders dedup so a task can't be injected twice in quick
    succession even if some external caller (manual --check, stale daemon, etc.)
    bypasses or races the flock-based primary mechanism.

    Best-effort: read without a lock. A stale read just means we *might* fail to
    suppress a duplicate — the flock-protected last_run check remains primary.
    """
    try:
        inbox = load_json(INBOX_PATH, [])
    except Exception:
        return set()
    if not isinstance(inbox, list):
        return set()
    cutoff = now - timedelta(seconds=window_seconds)
    recent: set[str] = set()
    for it in inbox:
        if not isinstance(it, dict):
            continue
        tid = it.get("task_id")
        if not tid:
            continue
        ts_str = it.get("timestamp") or it.get("received_at") or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            recent.add(tid)
    return recent


def _parse_cron_field(field: str, min_val: int, max_val: int):
    """Parse a single cron field into None (wildcard), int, or frozenset[int].

    Supported syntax:
      *         → None (match any)
      5         → 5
      1,3,5     → frozenset({1, 3, 5})
      1-5       → frozenset({1, 2, 3, 4, 5})
      */15      → frozenset({0, 15, 30, 45})  (step from min_val)
      1-10/2    → frozenset({1, 3, 5, 7, 9})  (step within range)

    Returns None on parse error (caller treats as invalid pattern).
    """
    if field == "*":
        return None  # wildcard

    # Handle step: */N or M-N/S
    if "/" in field:
        base, step_str = field.split("/", 1)
        try:
            step = int(step_str)
        except ValueError:
            return "ERROR"
        if step <= 0:
            return "ERROR"
        if base == "*":
            return frozenset(range(min_val, max_val + 1, step))
        elif "-" in base:
            try:
                lo, hi = base.split("-", 1)
                lo, hi = int(lo), int(hi)
            except ValueError:
                return "ERROR"
            if lo < min_val or hi > max_val or lo > hi:
                return "ERROR"
            return frozenset(range(lo, hi + 1, step))
        else:
            return "ERROR"

    # Handle list: 1,3,5
    if "," in field:
        vals = set()
        for item in field.split(","):
            item = item.strip()
            if "-" in item:
                # range inside list: 1-3,7,10-12
                try:
                    lo, hi = item.split("-", 1)
                    lo, hi = int(lo), int(hi)
                except ValueError:
                    return "ERROR"
                if lo < min_val or hi > max_val or lo > hi:
                    return "ERROR"
                vals.update(range(lo, hi + 1))
            else:
                try:
                    v = int(item)
                except ValueError:
                    return "ERROR"
                if v < min_val or v > max_val:
                    return "ERROR"
                vals.add(v)
        return frozenset(vals)

    # Handle range: 1-5
    if "-" in field:
        try:
            lo, hi = field.split("-", 1)
            lo, hi = int(lo), int(hi)
        except ValueError:
            return "ERROR"
        if lo < min_val or hi > max_val or lo > hi:
            return "ERROR"
        return frozenset(range(lo, hi + 1))

    # Plain integer
    try:
        v = int(field)
        if v < min_val or v > max_val:
            return "ERROR"
        return v
    except ValueError:
        return "ERROR"


# Field bounds: (min, max) for minute, hour, day-of-month, month, day-of-week
_CRON_FIELD_BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]


def _parse_cron_pattern(pattern: str):
    """Parse a cron pattern into a list of 5 entries.

    Returns None if the pattern is invalid. Each entry is:
      - None for '*' (match any)
      - int for a single value
      - frozenset[int] for ranges, lists, or steps

    Supported syntax per field: *, N, N-M, N,M,O, */S, N-M/S, N-M,O,P-Q
    """
    parts = pattern.strip().split()
    if len(parts) != 5:
        return None
    parsed = []
    for p, (lo, hi) in zip(parts, _CRON_FIELD_BOUNDS):
        result = _parse_cron_field(p, lo, hi)
        if result == "ERROR":
            return None
        parsed.append(result)
    return parsed


def _cron_parsed_matches(parsed: list, now: datetime) -> bool:
    """Check if pre-parsed cron fields match the given time.

    Each parsed entry can be None (wildcard), int, or frozenset[int].
    """
    # Convert Python weekday (0=Mon) to cron weekday (0=Sun)
    cron_dow = (now.weekday() + 1) % 7
    values = [now.minute, now.hour, now.day, now.month, cron_dow]
    for expected, actual in zip(parsed, values):
        if expected is None:
            continue  # wildcard
        if isinstance(expected, frozenset):
            if actual not in expected:
                return False
        elif expected != actual:
            return False
    return True


def _cron_matches(pattern: str, now: datetime) -> bool:
    """Check if a simple cron pattern matches the current time.

    Pattern format: "minute hour day_of_month month day_of_week"
    Supports '*' (any) and integer values only.
    """
    parsed = _parse_cron_pattern(pattern)
    if parsed is None:
        return False
    return _cron_parsed_matches(parsed, now)


def _has_cron_match_since(pattern: str, since: datetime, now: datetime) -> bool:
    """Check if any minute between (since, now] matches the cron pattern.

    Scans backward from now for efficiency. Caps at 7 days.
    """
    parsed = _parse_cron_pattern(pattern)
    if parsed is None:
        return False

    max_minutes = 10080  # 7 days
    check = now.replace(second=0, microsecond=0)
    cutoff = since.replace(second=0, microsecond=0)

    for _ in range(max_minutes):
        if check <= cutoff:
            return False
        if _cron_parsed_matches(parsed, check):
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
        except (ValueError, TypeError) as e:
            print(
                f"  [scheduler] Warning: invalid last_run '{last_run_str}' for task '{task.get('id', '?')}': {e}",
                file=sys.stderr,
            )

    if task_type == "interval":
        try:
            interval_min = int(task.get("interval_minutes", 0))
        except (TypeError, ValueError):
            return False
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
        except (ValueError, TypeError) as e:
            print(
                f"  [scheduler] Warning: invalid run_at '{run_at_str}' for task '{task.get('id', '?')}': {e}",
                file=sys.stderr,
            )
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
            return _has_cron_match_since(schedule, now - timedelta(hours=24), now)

    return False


def check_and_inject():
    """Evaluate scheduled tasks and inject due ones into inbox.

    Uses TASKS_PATH lock (matching AtomicJSON protocol in write.py) to prevent
    race conditions with portal writes. The inbox lock is nested inside when
    due entries need injection.
    """
    # Quick pre-check without lock — if no tasks exist, nothing to do
    tasks_precheck = _load_tasks()
    if not tasks_precheck:
        return 0

    now = datetime.now(timezone.utc)
    injected = 0

    # Acquire tasks lock — matches AtomicJSON(SCHEDULED_TASKS_PATH) in write.py
    tasks_lock_path = str(TASKS_PATH) + ".lock"
    try:
        with open(tasks_lock_path, "a+") as tasks_lock_f:
            with timed_flock(tasks_lock_f):
                # Re-read under lock to see any concurrent portal writes
                tasks = _load_tasks()
                if not tasks:
                    return 0

                # Belt-and-suspenders dedup: any task_id already in inbox within
                # the dedup window is suppressed even if _is_due says it should
                # fire. Read inbox once here (no lock — best-effort).
                recent_inbox_ids = _recent_inbox_task_ids(
                    now, INBOX_DEDUP_WINDOW_SECONDS
                )

                due_entries = []
                tasks_dirty = False  # tracks whether we mutated any task state
                # Save original task state for rollback if inbox write fails
                original_state = {}
                for task in tasks:
                    if not _is_due(task, now):
                        continue

                    task_id = task.get("id", "unknown")
                    content_text = task.get("content", "")
                    if not content_text:
                        continue

                    # Save original state before modifying (for rollback or audit)
                    original_state[id(task)] = {
                        "last_run": task.get("last_run"),
                        "enabled": task.get("enabled", True),
                    }
                    # Always advance task state — the schedule has fired even if
                    # we suppress the inbox append below.
                    task["last_run"] = now.isoformat()
                    if task.get("schedule_type") == "once":
                        task["enabled"] = False
                    tasks_dirty = True

                    if task_id in recent_inbox_ids:
                        _record_execution(
                            task,
                            "skipped_dup",
                            f"task_id present in inbox within {INBOX_DEDUP_WINDOW_SECONDS}s",
                        )
                        print(
                            f"  [scheduler] suppressed duplicate: {task_id} "
                            f"(already in inbox within {INBOX_DEDUP_WINDOW_SECONDS}s)"
                        )
                        continue

                    entry = {
                        "type": task.get("type", "goal"),
                        "content": f"[Scheduled: {task_id}] {content_text}",
                        "timestamp": now.isoformat(),
                        "received_at": now.isoformat(),
                        # source="scheduler" (origin); transport="polling_script"
                        # — the scheduler polls its task list and fires due tasks
                        # into the inbox. Inlined to avoid a cross-root import in
                        # this standalone daemon. Scheduled tasks act for the owner.
                        "from": {
                            "source": "scheduler",
                            "transport": "polling_script",
                            "role": "owner",
                        },
                        "task_id": task_id,
                    }
                    priority = task.get("priority")
                    if priority is not None:
                        try:
                            entry["priority"] = max(1, min(5, int(priority)))
                        except (TypeError, ValueError):
                            pass  # skip invalid priority, use default
                    due_entries.append(entry)

                    _record_execution(task, "injected", content_text[:80])
                    injected += 1
                    print(f"  [scheduler] injected: {task_id} — {content_text[:80]}")

                # Write tasks FIRST (persist last_run/enabled), then inbox.
                # Order rationale: if tasks write succeeds but inbox fails,
                # the task won't re-fire next cycle (last_run is set) — at worst
                # a single injection is lost and fires next interval. The reverse
                # (inbox succeeds, tasks fails) causes duplicates every cycle.
                # Suppressed-as-duplicate tasks also need the tasks write so the
                # schedule advances; only the inbox append is skipped.
                if due_entries or tasks_dirty:
                    try:
                        write_atomic(TASKS_PATH, tasks)
                    except Exception as e:
                        # Tasks write failed — rollback state and skip inbox write
                        print(
                            f"  [scheduler] ERROR: tasks write failed: {e} — rolling back",
                            file=sys.stderr,
                        )
                        for task in tasks:
                            orig = original_state.get(id(task))
                            if orig is not None:
                                task["last_run"] = orig["last_run"]
                                task["enabled"] = orig["enabled"]
                                _record_execution(
                                    task, "error", f"tasks write failed: {e}"
                                )
                        injected = 0
                        due_entries = []

                    # Only write inbox if tasks were persisted successfully
                    if due_entries:
                        inbox_lock_path = str(INBOX_PATH) + ".lock"
                        try:
                            with open(inbox_lock_path, "a+") as inbox_lock_f:
                                with timed_flock(inbox_lock_f):
                                    inbox = load_json(INBOX_PATH, [])
                                    inbox.extend(due_entries)
                                    write_atomic(INBOX_PATH, inbox)
                        except Exception as e:
                            # Inbox write failed but tasks already persisted.
                            # Tasks have last_run set so they won't re-fire —
                            # the injected items are lost for this interval only.
                            print(
                                f"  [scheduler] ERROR: inbox write failed: {e} — tasks already committed, items lost this cycle",
                                file=sys.stderr,
                            )
                            for task in tasks:
                                orig = original_state.get(id(task))
                                if orig is not None:
                                    _record_execution(
                                        task, "error", f"inbox write failed: {e}"
                                    )
                            injected = 0
                else:
                    # No due entries but still persist any task state changes
                    write_atomic(TASKS_PATH, tasks)
    except (TimeoutError, OSError) as e:
        print(
            f"  [scheduler] WARNING: {e} — skipping injection this cycle",
            file=sys.stderr,
        )
        return 0

    return injected


def list_tasks():
    """Print all scheduled tasks."""
    tasks = _load_tasks()
    if not tasks:
        print("No scheduled tasks.")
        return
    print(
        f"{'ID':<20} {'Type':<10} {'Enabled':<8} {'Last Run':<22} {'Schedule/Interval':<20} {'Content'}"
    )
    print("-" * 110)
    for t in tasks:
        tid = t.get("id", "?")[:20]
        ttype = t.get("schedule_type", "?")
        enabled = "yes" if t.get("enabled", True) else "no"
        last = (t.get("last_run") or "never")[:22]
        content = t.get("content", "")[:40]
        if ttype == "interval":
            sched = f"every {t.get('interval_minutes', '?')}m"
        elif ttype == "cron":
            sched = t.get("schedule", "?")
        elif ttype == "once":
            sched = t.get("run_at", "?")
        else:
            sched = "?"
        print(f"{tid:<20} {ttype:<10} {enabled:<8} {last:<22} {sched:<20} {content}")


def list_tasks_json():
    """Print all scheduled tasks as JSON."""
    tasks = _load_tasks()
    print(json.dumps(tasks, indent=2))


def show_history(task_id: str):
    """Show execution history for a specific task."""
    tasks = _load_tasks()
    task = next((t for t in tasks if t.get("id") == task_id), None)
    if task is None:
        print(f"Error: no task with ID '{task_id}' found.", file=sys.stderr)
        sys.exit(1)

    history = task.get("execution_history", [])
    if not history:
        print(f"No execution history for task '{task_id}'.")
        return

    print(
        f"Execution history for '{task_id}' ({len(history)} entries, max {EXEC_HISTORY_MAX}):"
    )
    print(f"{'Timestamp':<28} {'Status':<10} {'Note'}")
    print("-" * 80)
    for h in reversed(history):
        ts = h.get("timestamp", "?")[:26]
        status = h.get("status", "?")
        note = h.get("note", "")[:50]
        print(f"{ts:<28} {status:<10} {note}")


def show_stats():
    """Show aggregate execution stats for all scheduled tasks."""
    tasks = _load_tasks()
    if not tasks:
        print("No scheduled tasks.")
        return

    print(
        f"{'Task ID':<20} {'Total':<7} {'Injected':<10} {'RunNow':<8} {'Errors':<8} {'Last Exec'}"
    )
    print("-" * 85)
    for t in tasks:
        tid = t.get("id", "?")[:20]
        history = t.get("execution_history", [])
        total = len(history)
        injected = sum(1 for h in history if h.get("status") == "injected")
        run_now = sum(1 for h in history if h.get("status") == "run_now")
        errors = sum(1 for h in history if h.get("status") == "error")
        last_exec = history[-1].get("timestamp", "?")[:22] if history else "never"
        print(
            f"{tid:<20} {total:<7} {injected:<10} {run_now:<8} {errors:<8} {last_exec}"
        )

    # Summary
    all_history = [h for t in tasks for h in t.get("execution_history", [])]
    total = len(all_history)
    errors = sum(1 for h in all_history if h.get("status") == "error")
    if total:
        print(
            f"\nTotal: {total} executions, {errors} errors ({errors*100//total}% error rate)"
        )


def add_task(
    task_id, content, schedule_type, schedule_value, task_type="goal", priority=3
):
    """Add a new scheduled task."""
    # Skip pre-lock read — the authoritative duplicate check happens under
    # lock below. The unlocked read was redundant I/O that could also give
    # stale results under concurrent writes.
    task = {
        "id": task_id,
        "schedule_type": schedule_type,
        "type": task_type,
        "content": content,
        "enabled": True,
        "last_run": None,
        "priority": max(1, min(5, priority)),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    if schedule_type == "interval":
        task["interval_minutes"] = schedule_value
    elif schedule_type == "cron":
        task["schedule"] = schedule_value
    elif schedule_type == "once":
        task["run_at"] = schedule_value

    # Acquire lock for safe write
    tasks_lock_path = str(TASKS_PATH) + ".lock"
    with open(tasks_lock_path, "a+") as lock_f:
        with timed_flock(lock_f):
            tasks = _load_tasks()
            # Re-check after lock
            if any(t.get("id") == task_id for t in tasks):
                print(
                    f"Error: task '{task_id}' already exists (race).", file=sys.stderr
                )
                sys.exit(1)
            tasks.append(task)
            write_atomic(TASKS_PATH, tasks)

    print(f"+ Added task '{task_id}'")
    print(f"  Schedule: {schedule_type} = {schedule_value}")
    print(f"  Content:  {content[:80]}")
    print(f"  Type:     {task_type}  Priority: {priority}")


def remove_task(task_id):
    """Remove a scheduled task by ID."""
    tasks_lock_path = str(TASKS_PATH) + ".lock"
    with open(tasks_lock_path, "a+") as lock_f:
        with timed_flock(lock_f):
            tasks = _load_tasks()
            if not isinstance(tasks, list):
                print("scheduled_tasks.json is not a list.", file=sys.stderr)
                sys.exit(1)

            original_len = len(tasks)
            tasks = [t for t in tasks if t.get("id") != task_id]

            if len(tasks) == original_len:
                print(f"Error: no task with ID '{task_id}' found.", file=sys.stderr)
                sys.exit(1)

            write_atomic(TASKS_PATH, tasks)

    print(f"- Removed task '{task_id}'")


def toggle_task(task_id, enabled):
    """Enable or disable a scheduled task."""
    action = "enable" if enabled else "disable"
    tasks_lock_path = str(TASKS_PATH) + ".lock"
    with open(tasks_lock_path, "a+") as lock_f:
        with timed_flock(lock_f):
            tasks = _load_tasks()
            if not isinstance(tasks, list):
                print("scheduled_tasks.json is not a list.", file=sys.stderr)
                sys.exit(1)

            found = False
            for t in tasks:
                if t.get("id") == task_id:
                    t["enabled"] = enabled
                    found = True
                    break

            if not found:
                print(f"Error: no task with ID '{task_id}' found.", file=sys.stderr)
                sys.exit(1)

            write_atomic(TASKS_PATH, tasks)

    state = "enabled" if enabled else "disabled"
    print(f"~ Task '{task_id}' is now {state}")


def run_now_task(task_id):
    """Immediately inject a scheduled task into inbox, bypassing its schedule.

    The task must exist and be enabled. Updates last_run to now.
    Does NOT disable 'once' tasks (use --check for normal schedule behavior).
    """
    now = datetime.now(timezone.utc)
    tasks_lock_path = str(TASKS_PATH) + ".lock"

    with open(tasks_lock_path, "a+") as lock_f:
        with timed_flock(lock_f):
            tasks = _load_tasks()
            if not isinstance(tasks, list):
                print("scheduled_tasks.json is not a list.", file=sys.stderr)
                sys.exit(1)

            task = None
            for t in tasks:
                if t.get("id") == task_id:
                    task = t
                    break

            if task is None:
                print(f"Error: no task with ID '{task_id}' found.", file=sys.stderr)
                sys.exit(1)

            if not task.get("enabled", True):
                print(
                    f"Error: task '{task_id}' is disabled. Enable it first with --enable.",
                    file=sys.stderr,
                )
                sys.exit(1)

            content_text = task.get("content", "")
            if not content_text:
                print(f"Error: task '{task_id}' has no content.", file=sys.stderr)
                sys.exit(1)

            entry = {
                "type": task.get("type", "goal"),
                "content": f"[Scheduled: {task_id}] {content_text}",
                "timestamp": now.isoformat(),
                "received_at": now.isoformat(),
                # source="scheduler" (origin); transport="polling_script" — the
                # scheduler polls its task list and fires due tasks into the
                # inbox. Inlined to avoid a cross-root import in this standalone
                # daemon. Scheduled tasks act for the owner.
                "from": {
                    "source": "scheduler",
                    "transport": "polling_script",
                    "role": "owner",
                },
                "task_id": task_id,
            }
            priority = task.get("priority")
            if priority is not None:
                try:
                    entry["priority"] = max(1, min(5, int(priority)))
                except (TypeError, ValueError):
                    pass

            # Inject into inbox
            inbox_lock_path = str(INBOX_PATH) + ".lock"
            try:
                with open(inbox_lock_path, "a+") as inbox_lock_f:
                    with timed_flock(inbox_lock_f):
                        inbox = load_json(INBOX_PATH, [])
                        inbox.append(entry)
                        write_atomic(INBOX_PATH, inbox)
            except Exception as e:
                print(f"Error: inbox write failed: {e}", file=sys.stderr)
                sys.exit(1)

            # Update last_run and record execution
            task["last_run"] = now.isoformat()
            _record_execution(task, "run_now", content_text[:80])
            write_atomic(TASKS_PATH, tasks)

    print(f">> Triggered task '{task_id}' — injected into inbox now")
    print(f"   Content: {content_text[:80]}")
    print(f"   Type:    {task.get('type', 'goal')}")


def _next_cron_fire(pattern: str, after: datetime, max_days: int = 31):
    """Find the next time a cron pattern fires after the given datetime."""
    parsed = _parse_cron_pattern(pattern)
    if parsed is None:
        return None
    check = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    max_scan = 60 * 24 * max_days
    for _ in range(max_scan):
        if _cron_parsed_matches(parsed, check):
            return check
        check += timedelta(minutes=1)
    return None


def _next_interval_fire(task: dict, now: datetime):
    """Find the next time an interval task fires."""
    try:
        interval_min = int(task.get("interval_minutes", 0))
    except (TypeError, ValueError):
        return None
    if interval_min <= 0:
        return None
    last_run_str = task.get("last_run")
    if not last_run_str:
        return now  # never run, due immediately
    try:
        last_run = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return now
    next_fire = last_run + timedelta(minutes=interval_min)
    return next_fire if next_fire > now else now


def _next_once_fire(task: dict, now: datetime):
    """Find the next time a once task fires (or None if already ran)."""
    if task.get("last_run"):
        return None  # already ran
    run_at_str = task.get("run_at", "")
    if not run_at_str:
        return None
    try:
        run_at = datetime.fromisoformat(run_at_str.replace("Z", "+00:00"))
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        return run_at if run_at > now else None
    except (ValueError, TypeError):
        return None


def forecast_tasks(hours: int = 48):
    """Show a timeline of upcoming task fires for the next N hours."""
    tasks = _load_tasks()
    if not tasks:
        print("No scheduled tasks.")
        return

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours)
    timeline = []  # list of (fire_time, task_id, content)

    for task in tasks:
        if not task.get("enabled", True):
            continue
        task_id = task.get("id", "?")
        content = task.get("content", "")[:60]
        stype = task.get("schedule_type", "")

        if stype == "cron":
            pattern = task.get("schedule", "")
            if not pattern:
                continue
            # Find all fires within the horizon
            check_from = now
            cron_count = 0
            for _ in range(5000):  # cap iterations
                nxt = _next_cron_fire(pattern, check_from)
                if nxt is None or nxt > horizon:
                    break
                timeline.append((nxt, task_id, content, stype))
                check_from = nxt
                cron_count += 1
                if cron_count >= 500:
                    print(f"  (forecast for '{task_id}' truncated at 500 entries)")
                    break
        elif stype == "interval":
            nxt = _next_interval_fire(task, now)
            if nxt is None:
                continue
            try:
                interval_min = int(task.get("interval_minutes", 0))
            except (TypeError, ValueError):
                continue
            while nxt <= horizon:
                if nxt >= now:
                    timeline.append((nxt, task_id, content, stype))
                nxt += timedelta(minutes=interval_min)
                if len(timeline) > 500:
                    print(f"  (forecast for '{task_id}' truncated at 500 entries)")
                    break
        elif stype == "once":
            nxt = _next_once_fire(task, now)
            if nxt and nxt <= horizon:
                timeline.append((nxt, task_id, content, stype))

    if not timeline:
        print(f"No tasks scheduled to fire in the next {hours}h.")
        return

    timeline.sort(key=lambda x: x[0])
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    print(f"Schedule forecast — next {hours}h ({len(timeline)} fires)")
    print(f"{'When (UTC)':<20} {'In':<10} {'Task ID':<22} {'Type':<8} {'Content'}")
    print("-" * 100)

    for fire_time, task_id, content, stype in timeline:
        delta = fire_time - now
        total_min = int(delta.total_seconds() / 60)
        if total_min < 60:
            in_str = f"{total_min}m"
        elif total_min < 1440:
            in_str = f"{total_min // 60}h {total_min % 60}m"
        else:
            in_str = f"{total_min // 1440}d {(total_min % 1440) // 60}h"

        dow = dow_names[fire_time.weekday()]
        time_str = f"{fire_time.strftime('%Y-%m-%d %H:%M')} {dow}"
        print(f"{time_str:<20} {in_str:<10} {task_id:<22} {stype:<8} {content}")


def test_cron(pattern):
    """Test a cron pattern: show next 5 fire times."""
    parts = pattern.strip().split()
    if len(parts) != 5:
        print(
            f"Error: cron pattern must have 5 fields (got {len(parts)}).",
            file=sys.stderr,
        )
        print("  Format: minute hour day_of_month month day_of_week", file=sys.stderr)
        print("  Example: 0 9 * * *  (every day at 09:00 UTC)", file=sys.stderr)
        sys.exit(1)

    print(f"Cron pattern: {pattern}")
    print(
        f"Fields: min={parts[0]} hour={parts[1]} dom={parts[2]} mon={parts[3]} dow={parts[4]}"
    )
    print()

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    check = now + timedelta(minutes=1)
    matches = []
    max_scan = 60 * 24 * 31  # scan up to 31 days

    # Parse once and reuse — avoids re-parsing the pattern string on each of
    # up to ~44k iterations (31 days * 24h * 60m).
    parsed = _parse_cron_pattern(pattern)
    if parsed is None:
        print("  Error: invalid cron pattern.", file=sys.stderr)
        sys.exit(1)
    for _ in range(max_scan):
        if _cron_parsed_matches(parsed, check):
            matches.append(check)
            if len(matches) >= 5:
                break
        check += timedelta(minutes=1)

    if not matches:
        print("  No matches found in next 31 days.")
    else:
        print("Next 5 fire times (UTC):")
        for m in matches:
            dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            dow = dow_names[m.weekday()]
            print(f"  {m.strftime('%Y-%m-%d %H:%M')} ({dow})")


def _parse_every(value):
    """Parse --every value like '30m', '2h', '1d' into minutes."""
    value = value.strip().lower()
    if value.endswith("m"):
        return int(value[:-1])
    elif value.endswith("h"):
        return int(value[:-1]) * 60
    elif value.endswith("d"):
        return int(value[:-1]) * 1440
    else:
        # Assume minutes if no suffix
        return int(value)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Manage and evaluate scheduled tasks for the agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  scheduler.py --check                                          # inject due tasks
  scheduler.py --list                                           # list all tasks
  scheduler.py --add --id myid --goal "Do X" --every 30m        # interval task
  scheduler.py --add --id myid --goal "Do X" --cron "0 9 * * *" # cron task
  scheduler.py --add --id myid --goal "Do X" --once "2026-04-01T14:00+08:00"
  scheduler.py --remove --id myid                               # remove task
  scheduler.py --enable --id myid                               # enable task
  scheduler.py --disable --id myid                              # disable task
  scheduler.py --run-now --id myid                              # trigger task immediately
  scheduler.py --test --cron "0 9 * * 1"                        # test cron pattern
  scheduler.py --forecast                                       # show next 48h timeline
  scheduler.py --forecast --hours 72                             # show next 72h timeline
        """,
    )

    # Mode flags
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check", action="store_true", help="Evaluate and inject due tasks into inbox"
    )
    mode.add_argument("--list", action="store_true", help="List all scheduled tasks")
    mode.add_argument("--add", action="store_true", help="Add a new scheduled task")
    mode.add_argument("--remove", action="store_true", help="Remove a task by ID")
    mode.add_argument("--enable", action="store_true", help="Enable a disabled task")
    mode.add_argument("--disable", action="store_true", help="Disable a task")
    mode.add_argument(
        "--test", action="store_true", help="Test a cron pattern (show next fire times)"
    )
    mode.add_argument("--json", action="store_true", help="List tasks as JSON")
    mode.add_argument(
        "--run-now",
        action="store_true",
        help="Immediately inject a task into inbox (requires --id)",
    )
    mode.add_argument(
        "--forecast", action="store_true", help="Show timeline of upcoming task fires"
    )
    mode.add_argument(
        "--history",
        action="store_true",
        help="Show execution history for a task (requires --id)",
    )
    mode.add_argument(
        "--stats",
        action="store_true",
        help="Show aggregate execution stats for all tasks",
    )

    # Shared options
    parser.add_argument(
        "--id", metavar="ID", help="Task ID (for add/remove/enable/disable)"
    )
    parser.add_argument(
        "--goal", metavar="TEXT", help="Goal content for the task (for --add)"
    )
    parser.add_argument(
        "--message",
        metavar="TEXT",
        help="Message content for the task (for --add, sets type=message)",
    )

    # Schedule type (for --add)
    schedule = parser.add_mutually_exclusive_group()
    schedule.add_argument(
        "--every", metavar="Nm|Nh|Nd", help="Interval schedule (e.g., 30m, 2h, 1d)"
    )
    schedule.add_argument(
        "--cron", metavar="PATTERN", help="Cron pattern (min hour dom mon dow)"
    )
    schedule.add_argument(
        "--once", metavar="ISO_DATETIME", help="One-time schedule (ISO 8601 datetime)"
    )

    parser.add_argument(
        "--hours",
        type=int,
        default=48,
        metavar="N",
        help="Forecast horizon in hours (default: 48, for --forecast)",
    )
    parser.add_argument(
        "--priority", type=int, default=3, metavar="N", help="Priority 1-5 (default: 3)"
    )
    parser.add_argument(
        "--type",
        dest="task_type",
        default=None,
        choices=["goal", "message"],
        help="Inbox type (default: goal)",
    )

    args = parser.parse_args()

    if args.check:
        n = check_and_inject()
        if n > 0:
            print(f"  [scheduler] {n} task(s) injected into inbox")
    elif args.list:
        list_tasks()
    elif args.json:
        list_tasks_json()
    elif args.add:
        if not args.id:
            parser.error("--add requires --id")

        # Determine content and type
        content = args.goal or args.message
        if not content:
            parser.error("--add requires --goal or --message")
        task_type = args.task_type or ("message" if args.message else "goal")

        # Determine schedule
        if args.every:
            try:
                minutes = _parse_every(args.every)
                if minutes <= 0:
                    parser.error("--every value must be positive")
            except ValueError:
                parser.error(
                    f"Invalid --every format: {args.every} (use Nm, Nh, or Nd)"
                )
            add_task(
                args.id,
                content,
                "interval",
                minutes,
                task_type=task_type,
                priority=args.priority,
            )
        elif args.cron:
            # Validate cron pattern
            parts = args.cron.strip().split()
            if len(parts) != 5:
                parser.error(f"Cron pattern must have 5 fields (got {len(parts)})")
            add_task(
                args.id,
                content,
                "cron",
                args.cron,
                task_type=task_type,
                priority=args.priority,
            )
        elif args.once:
            # Validate datetime
            try:
                dt = datetime.fromisoformat(args.once.replace("Z", "+00:00"))
                add_task(
                    args.id,
                    content,
                    "once",
                    dt.isoformat(),
                    task_type=task_type,
                    priority=args.priority,
                )
            except ValueError:
                parser.error(f"Invalid datetime: {args.once}")
        else:
            parser.error("--add requires a schedule: --every, --cron, or --once")
    elif args.remove:
        if not args.id:
            parser.error("--remove requires --id")
        remove_task(args.id)
    elif args.enable:
        if not args.id:
            parser.error("--enable requires --id")
        toggle_task(args.id, True)
    elif args.disable:
        if not args.id:
            parser.error("--disable requires --id")
        toggle_task(args.id, False)
    elif args.run_now:
        if not args.id:
            parser.error("--run-now requires --id")
        run_now_task(args.id)
    elif args.history:
        if not args.id:
            parser.error("--history requires --id")
        show_history(args.id)
    elif args.stats:
        show_stats()
    elif args.forecast:
        forecast_tasks(hours=args.hours)
    elif args.test:
        if not args.cron:
            parser.error("--test requires --cron PATTERN")
        test_cron(args.cron)


if __name__ == "__main__":
    main()
