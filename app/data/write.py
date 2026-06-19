"""Write operations — inbox, scripts, goals, services, scheduled tasks."""

import contextlib
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from app.data._cache import _cache_clear_all
from app.data._helpers import _read_json_safe
from app.shared import (
    AGENT_DIR,
    MEMORY_DIR,
    LOGS_DIR,
    MESSAGES_DIR,
    SCRIPTS_DIR,
    GOALS_PATH,
    PORTAL_AUDIT_LOG_PATH,
    PORTAL_CONFIG_PATH,
    SCHEDULED_TASKS_PATH,
    _write_json_atomic,
    AtomicJSON,
)


def _append_portal_audit(entry: dict) -> None:
    """Append one JSON line to the portal audit log. Best-effort; never raises."""
    try:
        os.makedirs(os.path.dirname(PORTAL_AUDIT_LOG_PATH), exist_ok=True)
        with open(PORTAL_AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass


def save_portal_config(key: str, value):
    """Update a single key in portal_config.json, preserving other keys."""
    with AtomicJSON(PORTAL_CONFIG_PATH, default={}) as config:
        config[key] = value
    _cache_clear_all()


def queue_to_inbox(content, cmd_type, timestamp, priority=3):
    """Append a command to inbox.json and write an audit-log line.

    Uses AtomicJSON for exclusive file locking to prevent race conditions
    when both the Streamlit UI and a heartbeat cycle access inbox.json.
    Priority: 1 (highest) to 5 (lowest), default 3.
    """
    inbox_path = f"{MESSAGES_DIR}/inbox.json"
    body = {
        "type": cmd_type,
        "content": content,
        "timestamp": timestamp,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "priority": max(1, min(5, int(priority))),
        # Structured origin (== envelope.make_from("portal", role="owner")).
        # The portal operator is the owner; inlined to avoid a cross-root
        # import from app/ into services/.
        "from": {"transport": "portal", "role": "owner"},
    }
    with AtomicJSON(inbox_path, default=[]) as inbox:
        inbox.append(body)
    _append_portal_audit(
        {
            "timestamp": timestamp,
            "type": cmd_type,
            "priority": body["priority"],
            "content": content,
        }
    )
    _cache_clear_all()


def run_script(script_name, args=None):
    """Run a whitelisted utility script synchronously (30s timeout)."""
    if (
        not script_name
        or "/" in script_name
        or ".." in script_name
        or script_name.startswith(".")
    ):
        return {"ok": False, "error": "Invalid script name"}
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    if not os.path.isfile(script_path):
        return {"ok": False, "error": f"Script not found: {script_name}"}
    if script_name.endswith(".py"):
        cmd = ["uv", "run", "python", script_path]
    elif script_name.endswith(".sh"):
        cmd = ["bash", script_path]
    else:
        return {"ok": False, "error": "Unsupported script type"}
    if args:

        def _strip_quotes(v):
            s = str(v)
            if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
                return s[1:-1]
            return s

        cmd.extend(_strip_quotes(a) for a in args[:30])
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, cwd=AGENT_DIR
        )
        _cache_clear_all()
        return {
            "ok": True,
            "script": script_name,
            "exit_code": result.returncode,
            "stdout": result.stdout[:20000],
            "stderr": result.stderr[:5000],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Script timed out (30s limit)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def write_first_goal(content, timestamp):
    """Write the user's first goal directly to goal.json with status 'pending'.

    No-ops if goal.json already has entries (prevents double-submit race across sessions).
    """
    existing = _read_json_safe(GOALS_PATH, [])
    if existing:
        return  # already set — ignore duplicate submission
    entry = {
        "id": "goal-1",
        "goal": content,
        "status": "pending",
        "created_at": timestamp,
        "source": "user",
    }
    _write_json_atomic(GOALS_PATH, [entry], indent=2)
    _cache_clear_all()


def trigger_bootstrap_heartbeat():
    """Fire /agent/heartbeat.sh in the background to run the bootstrap cycle."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    try:
        with open(f"{LOGS_DIR}/bootstrap.log", "w") as lf:
            proc = subprocess.Popen(
                ["bash", "/agent/heartbeat.sh"],
                stdout=lf,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
        return {"ok": True, "pid": proc.pid}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def update_goal_status(goal_index: int, new_status: str):
    """Update the status of a goal by index."""
    with AtomicJSON(GOALS_PATH, default=[]) as goals:
        if 0 <= goal_index < len(goals):
            goals[goal_index]["status"] = new_status
    _cache_clear_all()


def is_archivable_goal(g: dict) -> bool:
    """True when a goal is a short-term completed/failed entry safe to archive.

    Long-term goals use ids prefixed with 'goal' and are preserved regardless
    of status so the agent keeps re-reading them as durable context.
    """
    return g.get("status") in ("completed", "failed") and not str(
        g.get("id", "")
    ).startswith("goal")


def archive_goals():
    """Archive completed/failed short-term goals to goal_history.json.

    Preserves long-term goals (id prefixed with 'goal') regardless of status.
    Returns the number of goals archived.
    """
    history_path = f"{MEMORY_DIR}/goal_history.json"
    archived_count = 0

    with AtomicJSON(GOALS_PATH, default=[]) as goals:
        to_archive = [g for g in goals if is_archivable_goal(g)]
        if to_archive:
            with AtomicJSON(history_path, default=[]) as history:
                existing_keys = {
                    (e.get("id"), e.get("created_at"))
                    for e in history
                    if isinstance(e, dict) and e.get("id")
                }
                for g in to_archive:
                    key = (g.get("id"), g.get("created_at"))
                    if g.get("id") and key in existing_keys:
                        continue
                    history.append(g)
                    if g.get("id"):
                        existing_keys.add(key)

            goals[:] = [g for g in goals if not is_archivable_goal(g)]
            archived_count = len(to_archive)

    _cache_clear_all()
    return archived_count


def delete_inbox_item(item_index: int):
    """Delete a single inbox item by index."""
    inbox_path = f"{MESSAGES_DIR}/inbox.json"
    with AtomicJSON(inbox_path, default=[]) as inbox:
        if 0 <= item_index < len(inbox):
            inbox.pop(item_index)
    _cache_clear_all()


def clear_outbox():
    """Archive outbox messages to outbox_history.json, then clear outbox."""
    outbox_path = f"{MESSAGES_DIR}/outbox.json"
    # outbox_history lives alongside the other messaging artifacts under
    # /agent/messages/ (inbox.json, inbox_history.json, outbox.json).
    history_path = f"{MESSAGES_DIR}/outbox_history.json"

    # Read current outbox
    outbox_data = _read_json_safe(outbox_path, [])
    if isinstance(outbox_data, dict):
        outbox_data = [outbox_data]
    elif not isinstance(outbox_data, list):
        outbox_data = []

    # Archive to history (dedup by timestamp)
    if outbox_data:
        history = _read_json_safe(history_path, [])
        existing_ts = {e.get("timestamp") for e in history if e.get("timestamp")}
        for msg in outbox_data:
            ts = msg.get("timestamp")
            if ts and ts in existing_ts:
                continue
            history.append(msg)
            if ts:
                existing_ts.add(ts)
        _write_json_atomic(history_path, history, indent=2)

    # Clear outbox
    with AtomicJSON(outbox_path, default=[]) as outbox:
        outbox.clear()
    _cache_clear_all()


def _call_svc(fn, *args):
    """Call a service_manager function, capturing stdout for error messages."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(*args)
        return {"ok": True, "output": buf.getvalue().strip()}
    except SystemExit as e:
        return {"ok": False, "error": buf.getvalue().strip() or f"exit {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def remove_service(name):
    """Stop (if running) and remove a service via service_manager."""
    if not _valid_service_name(name):
        return {"ok": False, "error": "Invalid service name"}
    try:
        from scripts.service_manager import cmd_remove

        result = _call_svc(cmd_remove, name)
        _cache_clear_all()
        if result["ok"]:
            return {"ok": True, "removed": name}
        return {"ok": False, "error": result["error"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _valid_service_name(name):
    return (
        name
        and "/" not in name
        and ".." not in name
        and not name.startswith("-")
        and "\x00" not in name
    )


def stop_service(name):
    """Stop a running service via service_manager."""
    if not _valid_service_name(name):
        return {"ok": False, "error": "Invalid service name"}
    try:
        from scripts.service_manager import cmd_stop

        result = _call_svc(cmd_stop, name)
        _cache_clear_all()
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def start_service(name):
    """Restart a dead service using its saved command from services.json."""
    if not _valid_service_name(name):
        return {"ok": False, "error": "Invalid service name"}
    services_path = f"{MEMORY_DIR}/services.json"
    services = _read_json_safe(services_path, {}) or {}
    svc = services.get(name)
    if not svc:
        return {"ok": False, "error": f"Service '{name}' not found in services.json"}
    command = svc.get("command")
    if not command or not isinstance(command, list):
        return {"ok": False, "error": f"No valid command for '{name}'"}
    port = svc.get("port")
    if port is not None:
        try:
            port = int(port)
        except (ValueError, TypeError):
            return {"ok": False, "error": f"Invalid port value for '{name}'"}

    try:
        from scripts.service_manager import cmd_start

        result = _call_svc(cmd_start, name, port, command)
        _cache_clear_all()
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def create_scheduled_task(task_data: dict):
    """Add a new scheduled task to scheduled_tasks.json."""
    tid = task_data.get("id", "").strip()
    if not tid:
        return {"ok": False, "error": "Task ID is required"}
    duplicate = False
    with AtomicJSON(SCHEDULED_TASKS_PATH, default=[]) as tasks:
        if any(t.get("id") == tid for t in tasks):
            duplicate = True
        else:
            tasks.append(task_data)
    if duplicate:
        return {"ok": False, "error": f"Task '{tid}' already exists"}
    _cache_clear_all()
    return {"ok": True, "created": tid}


def update_scheduled_task(task_id: str, updates: dict):
    """Update fields of an existing scheduled task."""
    found = False
    with AtomicJSON(SCHEDULED_TASKS_PATH, default=[]) as tasks:
        for t in tasks:
            if t.get("id") == task_id:
                t.update(updates)
                found = True
                break
    if found:
        _cache_clear_all()
        return {"ok": True, "updated": task_id}
    return {"ok": False, "error": f"Task '{task_id}' not found"}


def delete_scheduled_task(task_id: str):
    """Delete a scheduled task by ID."""
    found = False
    with AtomicJSON(SCHEDULED_TASKS_PATH, default=[]) as tasks:
        for i, t in enumerate(tasks):
            if t.get("id") == task_id:
                tasks.pop(i)
                found = True
                break
    if found:
        _cache_clear_all()
        return {"ok": True, "deleted": task_id}
    return {"ok": False, "error": f"Task '{task_id}' not found"}
