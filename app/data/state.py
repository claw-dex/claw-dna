"""
State and services loaders.

Enum Reference: See prompts/enum.md → Agent Status for valid status values.
"""

import os
import time

from app.data._cache import _mfile_cache, _register_cache
from app.data._helpers import _read_json_safe, _pid_alive
from app.shared import MEMORY_DIR, PORTAL_CONFIG_PATH

# services/shared.py is the single definition of the SDK model/effort
# vocabulary, shared with the internal-agent daemon and its register CLI.
# app/chat.py already bootstraps sys.path to services/; do the same here so we
# get the same module instance rather than a second copy.
import sys as _sys
from pathlib import Path as _Path

_services_dir = str(_Path(__file__).resolve().parent.parent.parent / "services")
if _services_dir not in _sys.path:
    _sys.path.insert(0, _services_dir)
from shared import (  # noqa: E402
    PORTAL_MODEL_CHOICES,
    normalize_effort,
    normalize_model,
)


@_mfile_cache(
    lambda: f"{MEMORY_DIR}/state.json",
    lambda: {"agent_status": "awaiting_first_heartbeat", "cycle_number": 0},
)
def load_state(data):
    """Load state.json — mtime-cached, invalidates on every heartbeat write."""
    from scripts.repair_memory_files import migrate_state_dict

    if isinstance(data, dict):
        migrate_state_dict(data)
    return data


_SERVICES_CACHE = _register_cache()


def load_services():
    """Load services from state.json with mtime + 5s time-bucket caching.

    Cache key: (state_mtime, pid_bucket) where pid_bucket = int(monotonic // 5).
    - state.json mtime invalidates when a service is started/stopped (state write).
    - pid_bucket advances every 5s so _pid_alive() results refresh at most 5s stale.
    Previously @_cache(ttl=5) re-read state.json every 5s even when no services changed.
    """
    state_path = f"{MEMORY_DIR}/state.json"
    try:
        state_mtime = os.path.getmtime(state_path)
    except OSError:
        state_mtime = 0.0
    pid_bucket = int(time.monotonic() // 5)
    cache_key = (state_mtime, pid_bucket)

    cached = _SERVICES_CACHE.get("data")
    if cached is not None:
        result, ck = cached
        if ck == cache_key:
            return result

    state = _read_json_safe(state_path, {}) or {}
    result = {
        name: {**info, "alive": _pid_alive(info.get("pid"))}
        for name, info in state.get("services", {}).items()
    }
    _SERVICES_CACHE["data"] = (result, cache_key)
    return result


_SERVICES_FULL_CACHE = _register_cache()
_SERVICE_LOG_CACHE = _register_cache()


def load_services_full():
    """Load all services from services.json with alive status and full info (including command).

    Unlike load_services() which reads from state.json (alive services only),
    this reads from services.json which retains dead/crashed services and their
    original command for restarting.
    """
    services_path = f"{MEMORY_DIR}/services.json"
    try:
        svc_mtime = os.path.getmtime(services_path)
    except OSError:
        svc_mtime = 0.0
    pid_bucket = int(time.monotonic() // 5)
    cache_key = (svc_mtime, pid_bucket)

    cached = _SERVICES_FULL_CACHE.get("data")
    if cached is not None:
        result, ck = cached
        if ck == cache_key:
            return result

    services = _read_json_safe(services_path, {}) or {}
    result = {
        name: {**info, "alive": _pid_alive(info.get("pid"))}
        for name, info in services.items()
    }
    _SERVICES_FULL_CACHE["data"] = (result, cache_key)
    return result


def _read_log_capped(path, cap=20000):
    """Read the tail of a log file (last `cap` bytes), with a truncation marker if larger.

    Reads from the end so the caller (which typically tails the last N lines)
    sees the most recent output instead of the head of the file.
    """
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > cap:
                f.seek(size - cap)
            raw = f.read()
        content = raw.decode("utf-8", errors="replace")
        if size > cap:
            # Drop the partial first line so we start on a clean line boundary.
            nl = content.find("\n")
            if nl != -1:
                content = content[nl + 1 :]
            content = (
                f"... (truncated — showing last {cap:,} characters of "
                f"{size:,}-byte log)\n" + content
            )
        return content
    except (FileNotFoundError, OSError):
        return ""


def load_service_logs(name):
    """Load stdout and stderr log content for a service, with mtime-based caching.

    Returns {"stdout": str, "stderr": str, "stdout_path": str, "stderr_path": str}
    or None if the service is not found.
    """
    services_path = f"{MEMORY_DIR}/services.json"
    services = _read_json_safe(services_path, {}) or {}
    svc = services.get(name)
    if not svc:
        return None

    stdout_path = svc.get("stdout_log", "")
    stderr_path = svc.get("stderr_log", "")

    def _mtime(p):
        try:
            return os.path.getmtime(p) if p else 0.0
        except OSError:
            return 0.0

    stdout_mtime = _mtime(stdout_path)
    stderr_mtime = _mtime(stderr_path)
    cache_key = (stdout_mtime, stderr_mtime)

    cached = _SERVICE_LOG_CACHE.get(name)
    if cached is not None:
        result, ck = cached
        if ck == cache_key:
            return result

    result = {
        "stdout": _read_log_capped(stdout_path),
        "stderr": _read_log_capped(stderr_path),
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
    }
    _SERVICE_LOG_CACHE[name] = (result, cache_key)
    return result


@_mfile_cache(lambda: PORTAL_CONFIG_PATH, dict)
def load_chat_sdk_settings(data):
    """Portal-chat SDK overrides from portal_config.json — mtime-cached.

    Returns ``{"model": str|None, "effort": str|None}``. Both are normalized,
    so a hand-edited or stale value that is no longer valid degrades to
    ``None`` (= use the SDK default) instead of reaching the `claude` CLI.
    """
    if not isinstance(data, dict):
        data = {}
    return {
        "model": normalize_model(data.get("chat_model"), allowed=PORTAL_MODEL_CHOICES),
        "effort": normalize_effort(data.get("chat_effort")),
    }
