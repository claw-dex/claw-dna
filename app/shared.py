"""Shared constants and utility functions for the agent Streamlit app."""

import copy
import fcntl
import glob
import html as _html
import json
import os
import shutil
import time
from datetime import datetime, timezone

# ── Path constants ────────────────────────────────────────────
AGENT_DIR = "/agent"
MEMORY_DIR = f"{AGENT_DIR}/memory"
LOGS_DIR = f"{MEMORY_DIR}/logs"
MESSAGES_DIR = f"{AGENT_DIR}/messages"
SCRIPTS_DIR = f"{AGENT_DIR}/scripts"
HISTORY_PATH = f"{MEMORY_DIR}/command_history.json"
GOALS_PATH = f"{MEMORY_DIR}/goal.json"
ERROR_LOG_PATH = f"{MEMORY_DIR}/server_errors.json"
CHAT_HISTORY_PATH = f"{MEMORY_DIR}/chat_history.json"
CHAT_META_PATH = f"{MEMORY_DIR}/chat_meta.json"
PORTAL_CONFIG_PATH = f"{MEMORY_DIR}/portal_config.json"
AGENT_CREDENTIALS_PATH = "/home/agent/.claude/.credentials.json"
SCHEDULED_TASKS_PATH = os.path.join(MEMORY_DIR, "scheduled_tasks.json")

# ── Status / type styling ─────────────────────────────────────
# See prompts/enum.md for complete enum definitions
_STATUS_COLORS = {
    "completed": "#4CAF50",
    "failed": "#F44336",
    "in_progress": "#2196F3",
    "pending": "#FF9800",
}
_TYPE_COLORS = {
    # Inbox types
    "goal": "#2196F3",
    "message": "#9C27B0",
    "bash": "#FF9800",
    # Outbox types
    "response": "#4CAF50",
    "needs_human": "#F44336",
    "goal_complete": "#4CAF50",
    "goal_failed": "#F44336",
}


def _badge(text, color):
    """Colored pill badge (HTML-escaped)."""
    return (
        f'<span style="background:{_html.escape(str(color))};color:#fff;padding:1px 8px;'
        f'border-radius:10px;font-size:11px;font-weight:600">{_html.escape(str(text))}</span>'
    )


MAX_HISTORY = 50  # keep last 50 commands


def parse_dt(s):
    """Parse an ISO timestamp string to a timezone-aware datetime, or None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def heartbeat_freshness(hb_str):
    """Return (display_str, icon) for a heartbeat timestamp.

    Icons: 🟢 < 5 min | 🟡 5-30 min | 🔴 > 30 min | ⚪ unknown
    """
    if not hb_str or hb_str == "—":
        return "—", "⚪"
    try:
        hb_dt = datetime.fromisoformat(str(hb_str))
        secs = (datetime.now(timezone.utc) - hb_dt).total_seconds()
        if secs < 0:
            age_str = "just now"
        elif secs < 60:
            age_str = f"{int(secs)}s ago"
        elif secs < 3600:
            age_str = f"{int(secs // 60)}m ago"
        elif secs < 86400:
            age_str = f"{int(secs // 3600)}h ago"
        else:
            age_str = f"{int(secs // 86400)}d ago"
        if secs < 300:
            icon = "🟢"
        elif secs < 1800:
            icon = "🟡"
        else:
            icon = "🔴"
        return age_str, icon
    except (ValueError, TypeError):
        return str(hb_str)[:16].replace("T", " "), "⚪"


# Critical directories and files with sensible defaults
_CRITICAL_DIRS = [
    MEMORY_DIR,
    LOGS_DIR,
    MESSAGES_DIR,
    f"{AGENT_DIR}/web",
    f"{AGENT_DIR}/workspace",
]
_CRITICAL_FILES = {
    f"{MEMORY_DIR}/state.json": {
        "cycle_number": 0,
        "status": "idle",
        "current_goal": None,
        "last_cycle_summary": None,
        "created_at": None,
        "last_heartbeat": None,
        "last_cycle_run": None,
        "last_cycle_end": None,
        "services": {},
    },
    f"{MEMORY_DIR}/cycles.json": [],
    GOALS_PATH: [],
    HISTORY_PATH: [],
    ERROR_LOG_PATH: [],
    f"{MESSAGES_DIR}/inbox.json": [],
    f"{MESSAGES_DIR}/outbox.json": [],
    f"{MEMORY_DIR}/journal.json": [],
}


def _startup_check():
    """Ensure all critical dirs/files exist and JSON files are valid.
    Repairs corrupted files by backing up and resetting to defaults."""
    issues = []
    # 1. Create missing directories
    for d in _CRITICAL_DIRS:
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            issues.append(f"Created missing dir: {d}")

    # 2. Create or repair critical JSON files
    for path, default in _CRITICAL_FILES.items():
        if not os.path.exists(path):
            _write_json_atomic(path, default, indent=2)
            issues.append(f"Created missing file: {path}")
        else:
            try:
                with open(path) as f:
                    json.load(f)
            except (json.JSONDecodeError, ValueError):
                backup = path + ".corrupt"
                shutil.copy2(path, backup)
                _write_json_atomic(path, default, indent=2)
                issues.append(f"Repaired corrupt file: {path} (backup: {backup})")

    # 3. Clean up stale .tmp files from interrupted atomic writes
    for d in _CRITICAL_DIRS:
        for tmp in glob.glob(os.path.join(d, "*.tmp")):
            try:
                age = time.time() - os.path.getmtime(tmp)
                if age > 30:  # older than 30 seconds = stale
                    os.unlink(tmp)
                    issues.append(f"Removed stale temp file: {tmp}")
            except OSError:
                pass

    if issues:
        print(f"[Agent] Startup check: fixed {len(issues)} issue(s):", flush=True)
        for issue in issues:
            print(f"  - {issue}", flush=True)
    else:
        print("[Agent] Startup check: all critical files OK", flush=True)
    return issues


def _write_json_atomic(path, data, indent=None):
    """Write JSON to a file atomically: write to temp, then os.replace().
    Prevents corruption if the process is killed mid-write."""
    import tempfile

    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _safe_int(value, default=0):
    """Parse an integer from a value, returning default on failure."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _append_history(entry):
    """Append a command entry to the history file, keeping last MAX_HISTORY."""
    try:
        with open(HISTORY_PATH) as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = []
    history.append(entry)
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    _write_json_atomic(HISTORY_PATH, history)


def _truncate_history(history, max_content=500):
    """Return history with content fields truncated for dashboard use."""
    result = []
    for entry in history:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("content"), str)
            and len(entry["content"]) > max_content
        ):
            entry = {
                **entry,
                "content": entry["content"][:max_content] + "...(truncated)",
            }
        result.append(entry)
    return result


class AtomicJSON:
    """Context manager for safe read-modify-write on a JSON file.

    Uses fcntl.flock for exclusive file locking and _write_json_atomic for
    crash-safe writes. Prevents race conditions when multiple processes
    (e.g., Streamlit UI and heartbeat) access the same file.

    Usage:
        with AtomicJSON(path, default=[]) as data:
            data.append(new_item)
        # data is written back atomically on context exit
    """

    def __init__(self, path: str, default=None):
        self.path = path
        self.default = default if default is not None else []
        self._lock_path = path + ".lock"
        self._lock_fd = None
        self._data = None

    def __enter__(self):
        self._lock_fd = open(self._lock_path, "a+")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        except Exception:
            self._lock_fd.close()
            raise
        try:
            with open(self.path) as f:
                self._data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = copy.deepcopy(self.default)
        return self._data

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                _write_json_atomic(self.path, self._data, indent=2)
        finally:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            self._lock_fd.close()
        return False  # don't suppress exceptions
