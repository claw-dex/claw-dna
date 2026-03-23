"""State and services loaders."""

import os
import time

from app.data._cache import _mfile_cache, _register_cache
from app.data._helpers import _read_json_safe, _pid_alive
from app.shared import MEMORY_DIR


@_mfile_cache(lambda: f"{MEMORY_DIR}/state.json",
              lambda: {"status": "awaiting_first_heartbeat", "cycle_number": 0})
def load_state(data):
    """Load state.json — mtime-cached, invalidates on every heartbeat write."""
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
