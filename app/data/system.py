"""System loaders — errors, system_info, validate, plugins."""

import os
import shutil
import time
from datetime import datetime, timezone

from app.data._cache import _mfile_cache, _cache, _register_cache
from app.data._helpers import _read_json_safe
from app.shared import AGENT_DIR, MEMORY_DIR, MESSAGES_DIR, GOALS_PATH, ERROR_LOG_PATH, SCHEDULED_TASKS_PATH


@_mfile_cache(lambda: SCHEDULED_TASKS_PATH, list)
def load_scheduled_tasks(data):
    """Load scheduled_tasks.json — mtime-cached."""
    return data if isinstance(data, list) else []


@_mfile_cache(lambda: ERROR_LOG_PATH, list)
def load_errors(data):
    """Load server_errors.json — mtime-cached; called on every render, changes only on tab crash."""
    return data if isinstance(data, list) else []



@_mfile_cache(lambda: f"{AGENT_DIR}/.claude/settings.json", dict)
def load_plugins(data):
    """Load enabled Claude plugins from .claude/settings.json.

    mtime-based — re-reads only when .claude/settings.json changes (agent installs a plugin).
    Returns dict of {plugin_id: enabled}.
    """
    return data.get("enabledPlugins", {}) if isinstance(data, dict) else {}


@_cache(ttl=60)
def load_system_info():
    info = {"timestamp": datetime.now(timezone.utc).isoformat()}

    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])
        total_kb = meminfo.get("MemTotal", 0)
        avail_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        used_kb = total_kb - avail_kb
        info["memory"] = {
            "total_mb": round(total_kb / 1024),
            "used_mb": round(used_kb / 1024),
            "available_mb": round(avail_kb / 1024),
            "percent": round(used_kb / total_kb * 100, 1) if total_kb else 0,
        }
    except Exception:
        info["memory"] = None

    try:
        usage = shutil.disk_usage("/agent")
        info["disk"] = {
            "total_gb": round(usage.total / (1024**3), 1),
            "used_gb": round(usage.used / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
            "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        }
    except Exception:
        info["disk"] = None

    try:
        load1, load5, load15 = os.getloadavg()
        info["load"] = {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)}
    except Exception:
        info["load"] = None

    try:
        with open("/proc/uptime") as f:
            uptime_secs = float(f.read().split()[0])
        hours = int(uptime_secs // 3600)
        mins = int((uptime_secs % 3600) // 60)
        info["uptime"] = {"seconds": int(uptime_secs), "human": f"{hours}h {mins}m"}
    except Exception:
        info["uptime"] = None

    try:
        total_bytes = 0
        for dirpath, dirs, filenames in os.walk("/agent/workspace"):
            # Skip hidden dirs (e.g. .agent-browser-profile with thousands of files)
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in filenames:
                if not fname.startswith("."):
                    try:
                        total_bytes += os.path.getsize(os.path.join(dirpath, fname))
                    except OSError:
                        pass
        info["workspace_mb"] = round(total_bytes / (1024 ** 2), 1)
    except Exception:
        info["workspace_mb"] = None

    return info


_VALIDATE_CACHE = _register_cache()


def load_validate():
    """Validate memory file integrity for the System tab.

    Uses mtime-based compound caching keyed on 6 source files + a 30-second
    heartbeat bucket. Previously TTL=15s caused full re-validation (8 JSON reads +
    cycle continuity scan) every 15s even when no files had changed. Now:
    - File integrity checks re-run only when a source file is modified (at cycle-close)
    - Heartbeat freshness check re-evaluates every 30s (bucket change) to stay current
    This reduces I/O from ~8 reads/15s to ~2 mtime stats/render between cycle boundaries.
    """
    state_path = os.path.join(MEMORY_DIR, "state.json")
    cyc_path   = os.path.join(MEMORY_DIR, "cycles.json")
    goal_path  = GOALS_PATH
    jour_path  = os.path.join(MEMORY_DIR, "journal.json")

    def _mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    state_m = _mtime(state_path)
    cyc_m   = _mtime(cyc_path)
    goal_m  = _mtime(goal_path)
    jour_m  = _mtime(jour_path)
    # Bucket time into 30s windows so heartbeat freshness updates every 30s
    hb_bucket = int(time.monotonic() // 30)

    cached = _VALIDATE_CACHE.get("data")
    if cached is not None:
        result, c_sm, c_cy, c_go, c_jo, c_hb = cached
        if (c_sm == state_m and c_cy == cyc_m and
                c_go == goal_m and c_jo == jour_m and
                c_hb == hb_bucket):
            return result

    checks = []
    ok_count = warn_count = fail_count = 0

    def check(name, passed, severity="error", detail=""):
        nonlocal ok_count, warn_count, fail_count
        if passed:
            ok_count += 1
        elif severity == "warning":
            warn_count += 1
        else:
            fail_count += 1
        checks.append({"name": name, "passed": passed,
                        "severity": severity if not passed else "ok", "detail": detail})

    json_files = {
        "state.json": ["cycle_number", "status", "last_heartbeat"],
        "cycles.json": None,
        "goal.json": None,
    }
    parsed = {}
    for fname, required_keys in json_files.items():
        path = os.path.join(MEMORY_DIR, fname)
        if not os.path.isfile(path):
            check(f"{fname} exists", False, detail="File missing")
            continue
        data = _read_json_safe(path)
        if data is None:
            check(f"{fname} valid JSON", False, detail="Failed to parse")
            continue
        parsed[fname] = data
        check(f"{fname} valid JSON", True)
        if required_keys and isinstance(data, dict):
            missing = [k for k in required_keys if k not in data]
            check(f"{fname} required fields", len(missing) == 0, "warning",
                  f"Missing: {', '.join(missing)}" if missing else "")

    jpath = os.path.join(MEMORY_DIR, "journal.json")
    if os.path.isfile(jpath):
        journal_data = _read_json_safe(jpath, None)
        check("journal.json exists", True)
        check("journal.json valid JSON", journal_data is not None, "warning",
              "Failed to parse" if journal_data is None else f"{len(journal_data) if isinstance(journal_data, list) else '?'} entries")
        if journal_data is not None:
            check("journal.json is a list", isinstance(journal_data, list), "warning",
                  f"Expected list, got {type(journal_data).__name__}")
    else:
        check("journal.json exists", False)

    state = parsed.get("state.json")
    cycles = parsed.get("cycles.json")
    if state and cycles and isinstance(cycles, list):
        state_cycle = state.get("cycle_number", 0)
        max_cycle = max((c.get("cycle", 0) for c in cycles), default=0)
        check("cycle_number consistent", state_cycle >= max_cycle, "warning",
              f"state.json says {state_cycle}, cycles.json max is {max_cycle}")

    if cycles and isinstance(cycles, list) and len(cycles) > 1:
        nums = sorted(c.get("cycle", 0) for c in cycles)
        gaps = [nums[i+1] for i in range(len(nums)-1) if nums[i+1] != nums[i]+1]
        check("cycle continuity", len(gaps) == 0, "warning",
              f"Gaps before: {gaps}" if gaps else f"Continuous 1-{nums[-1]}")

    if state and state.get("last_heartbeat"):
        try:
            hb = datetime.fromisoformat(state["last_heartbeat"])
            now = datetime.now(timezone.utc)
            delta = (now - hb).total_seconds()
            stale = delta > 600
            check("heartbeat fresh (<10min)", not stale, "warning", f"{int(delta)}s ago")
        except (ValueError, TypeError):
            check("heartbeat parseable", False, "warning", "Invalid timestamp format")

    for mf in ["inbox.json", "outbox.json"]:
        mp = os.path.join(MESSAGES_DIR, mf)
        check(f"messages/{mf} exists", os.path.isfile(mp))

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {"ok": ok_count, "warnings": warn_count, "errors": fail_count},
        "healthy": fail_count == 0,
        "checks": checks,
    }
    _VALIDATE_CACHE["data"] = (result, state_m, cyc_m, goal_m, jour_m, hb_bucket)
    return result


load_validate.clear = _VALIDATE_CACHE.clear
