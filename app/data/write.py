"""Write operations — inbox, scripts, goals, services, scheduled tasks."""

import json
import os
import subprocess
from datetime import datetime, timezone

from app.data._cache import _cache_clear_all
from app.data._helpers import _read_json_safe
from app.shared import (
    AGENT_DIR, MEMORY_DIR, LOGS_DIR, MESSAGES_DIR, SCRIPTS_DIR,
    GOALS_PATH, PORTAL_CONFIG_PATH, _write_json_atomic, _append_history, AtomicJSON,
)

SCHEDULED_TASKS_PATH = f"{MEMORY_DIR}/scheduled_tasks.json"


def save_portal_config(key: str, value):
    """Update a single key in portal_config.json, preserving other keys."""
    with AtomicJSON(PORTAL_CONFIG_PATH, default={}) as config:
        config[key] = value
    _cache_clear_all()


def queue_to_inbox(content, cmd_type, timestamp, priority=3):
    """Append a command to inbox.json and record in history.

    Uses AtomicJSON for exclusive file locking to prevent race conditions
    when both the Streamlit UI and a heartbeat cycle access inbox.json.
    Priority: 1 (highest) to 5 (lowest), default 3.
    """
    inbox_path = f"{MESSAGES_DIR}/inbox.json"
    body = {"type": cmd_type, "content": content, "timestamp": timestamp,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "priority": max(1, min(5, int(priority)))}
    with AtomicJSON(inbox_path, default=[]) as inbox:
        inbox.append(body)
    _append_history({"type": cmd_type, "content": content, "timestamp": timestamp, "result": "queued"})
    _cache_clear_all()



def run_script(script_name, args=None):
    """Run a whitelisted utility script synchronously (30s timeout)."""
    if not script_name or '/' in script_name or '..' in script_name or script_name.startswith('.'):
        return {"ok": False, "error": "Invalid script name"}
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    if not os.path.isfile(script_path):
        return {"ok": False, "error": f"Script not found: {script_name}"}
    if script_name.endswith('.py'):
        cmd = ["uv", "run", "python", script_path]
    elif script_name.endswith('.sh'):
        cmd = ["bash", script_path]
    else:
        return {"ok": False, "error": "Unsupported script type"}
    if args:
        cmd.extend(str(a) for a in args[:30])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=AGENT_DIR)
        _cache_clear_all()
        return {
            "ok": True, "script": script_name,
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
    history_path = f"{MEMORY_DIR}/outbox_history.json"

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


def remove_service(name):
    """Remove a registered service from both state.json and services.json."""
    removed_any = False

    # Remove from state.json
    state_path = f"{MEMORY_DIR}/state.json"
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    if name in state.get("services", {}):
        del state["services"][name]
        _write_json_atomic(state_path, state, indent=2)
        removed_any = True

    # Remove from services.json
    services_path = f"{MEMORY_DIR}/services.json"
    svc_data = _read_json_safe(services_path, {}) or {}
    if name in svc_data:
        del svc_data[name]
        _write_json_atomic(services_path, svc_data, indent=2)
        removed_any = True

    _cache_clear_all()
    if not removed_any:
        return {"ok": False, "error": f"Service '{name}' not found"}
    return {"ok": True, "removed": name}


def _valid_service_name(name):
    return name and '/' not in name and '..' not in name and not name.startswith('-') and '\x00' not in name


def stop_service(name):
    """Stop a running service via service-manager.py."""
    if not _valid_service_name(name):
        return {"ok": False, "error": "Invalid service name"}
    script_path = os.path.join(SCRIPTS_DIR, "service-manager.py")
    try:
        result = subprocess.run(
            ["uv", "run", "python", script_path, "stop", name],
            capture_output=True, text=True, timeout=15, cwd=AGENT_DIR,
        )
        _cache_clear_all()
        if result.returncode == 0:
            return {"ok": True, "output": result.stdout.strip()}
        return {"ok": False, "error": result.stdout.strip() or result.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Stop timed out (15s)"}
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

    script_path = os.path.join(SCRIPTS_DIR, "service-manager.py")
    cmd = ["uv", "run", "python", script_path, "start", name]
    port = svc.get("port")
    if port is not None:
        try:
            cmd.append(str(int(port)))
        except (ValueError, TypeError):
            return {"ok": False, "error": f"Invalid port value for '{name}'"}
    cmd.append("--")
    cmd.extend(command)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15, cwd=AGENT_DIR,
        )
        _cache_clear_all()
        if result.returncode == 0:
            return {"ok": True, "output": result.stdout.strip()}
        return {"ok": False, "error": result.stdout.strip() or result.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Start timed out (15s)"}
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
