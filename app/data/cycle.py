"""
Cycle loaders — cycles, velocity, cycle logs, balance, activity.

Enum Reference: See prompts/enum.md → Cycle Status, Cycle Type, Evolution Category.
"""

import glob
import json
import os
import re
import time
from datetime import datetime, timezone

from app.data._cache import _mfile_cache, _mmfile_cache, _register_cache
from app.data._helpers import _read_json_safe
from app.shared import MEMORY_DIR, LOGS_DIR, HISTORY_PATH


@_mfile_cache(lambda: f"{MEMORY_DIR}/cycles.json", list)
def load_cycles(data):
    """Load cycles from cycles.json — mtime-cached."""
    return data if isinstance(data, list) else []


@_mmfile_cache([lambda: f"{MEMORY_DIR}/cycles.json"])
def load_cycle_velocity():
    """Compute cycles-per-hour from the last 10 completed cycles (rolling window).

    Uses @_mmfile_cache keyed on cycles.json mtime. load_cycle_velocity() is called
    in server.py on every render. Previously used a hand-rolled _CYCLE_VELOCITY_CACHE
    (identical pattern); now auto-registered in _MFILE_CACHES for auto-clear on writes.
    Returns float velocity (cycles/hr) or None if insufficient data.
    """
    try:
        cycles_data = load_cycles() or []
        completed = [c for c in cycles_data if c.get("status") == "completed" and c.get("start")]
        if len(completed) < 2:
            return None
        recent = sorted(completed, key=lambda c: c.get("start", ""), reverse=True)[:10]
        if len(recent) < 2:
            return None
        oldest = recent[-1].get("start", "")
        newest = recent[0].get("start", "")
        t_new = datetime.fromisoformat(newest)
        t_old = datetime.fromisoformat(oldest)
        span_hours = (t_new - t_old).total_seconds() / 3600
        return round(len(recent) / span_hours, 1) if span_hours > 0 else None
    except Exception:
        return None


_CYCLE_LOGS_CACHE = _register_cache()


def load_cycle_logs():
    """Return sorted list of cycle log metadata dicts (newest first).

    Each entry: {cycle: int, path: str, size: int, mtime: float}

    Uses mtime-based caching on the logs directory: the glob + stat pass is only
    re-run when the directory itself changes (i.e. a new .log file appears, which
    happens ~once per cycle / ~5 minutes). Previously @_cache(ttl=15) caused ~4
    glob passes/minute over 131 files; this reduces that to ~0 globs/minute when
    no new cycle has completed.
    """
    try:
        dir_mtime = os.path.getmtime(LOGS_DIR)
    except OSError:
        dir_mtime = 0.0

    cached = _CYCLE_LOGS_CACHE.get("data")
    if cached is not None:
        result, cached_mtime = cached
        if cached_mtime == dir_mtime:
            return result

    log_files = glob.glob(f"{LOGS_DIR}/cycle-*.log")
    cycle_logs = []
    for path in log_files:
        base = os.path.basename(path)
        m = re.match(r"cycle-(\d+)\.log$", base)
        if m:
            cycle_num = int(m.group(1))
            try:
                size_bytes = os.path.getsize(path)
                file_mtime = os.path.getmtime(path)
            except OSError:
                size_bytes = 0
                file_mtime = 0
            cycle_logs.append({"cycle": cycle_num, "path": path, "size": size_bytes, "mtime": file_mtime})
    cycle_logs.sort(key=lambda x: x["cycle"], reverse=True)

    _CYCLE_LOGS_CACHE["data"] = (cycle_logs, dir_mtime)
    return cycle_logs


_CYCLE_LOG_CONTENT_CACHE = _register_cache()


def load_cycle_log_content(cycle_num: int) -> str:
    """Read and return the text content of a cycle's .log file.

    Uses per-file mtime-based caching: re-reads only when the log file's mtime
    changes. Cycle logs are write-once after the cycle ends — previous @_cache(ttl=60)
    caused a full file read every 60s even for long-finished cycles. Mtime caching
    gives ~0 reads between cycle writes (~5 min apart) and is effectively permanent
    for archived cycle logs.
    """
    path = f"{LOGS_DIR}/cycle-{cycle_num}.log"
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    cached = _CYCLE_LOG_CONTENT_CACHE.get(cycle_num)
    if cached is not None:
        result, cached_mtime = cached
        if cached_mtime == mtime:
            return result
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return ""
    # Drop JSON-only lines (CLI pre-flight warnings like {"level":"warn",...})
    lines = raw.splitlines()
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                json.loads(stripped)
                continue  # skip JSON warning lines
            except (ValueError, json.JSONDecodeError):
                pass
        cleaned.append(line)
    result = "\n".join(cleaned).strip()
    _CYCLE_LOG_CONTENT_CACHE[cycle_num] = (result, mtime)
    return result


_BALANCE_CACHE = _register_cache()


def load_balance():
    """Compute evolution category balance with mtime-based derived caching.

    load_balance() is called in overview.py on every render.
    It derives from load_cycles(), journal data, and the dynamic weights file
    (written by cycle_start.py). Caches against cycles.json, journal.json,
    and evolution_weights.json mtimes.
    """
    cycles_path = f"{MEMORY_DIR}/cycles.json"
    journal_path = f"{MEMORY_DIR}/journal.json"
    weights_path = f"{MEMORY_DIR}/evolution_weights.json"
    try:
        c_mtime = os.path.getmtime(cycles_path)
    except OSError:
        c_mtime = 0.0
    try:
        j_mtime = os.path.getmtime(journal_path)
    except OSError:
        j_mtime = 0.0
    try:
        w_mtime = os.path.getmtime(weights_path)
    except OSError:
        w_mtime = 0.0

    cached = _BALANCE_CACHE.get("data")
    if cached is not None:
        result, cc, cj, cw = cached
        if cc == c_mtime and cj == j_mtime and cw == w_mtime:
            return result

    cycles = load_cycles()
    if not isinstance(cycles, list):
        cycles = []
    categories = {}
    recent_categories = {}
    evolve_cycles = [c for c in cycles if c.get("type") == "evolve" and c.get("category")]
    for c in evolve_cycles:
        cat = c["category"]
        categories[cat] = categories.get(cat, 0) + 1
    for c in evolve_cycles[-10:]:
        cat = c["category"]
        recent_categories[cat] = recent_categories.get(cat, 0) + 1
    total = sum(categories.values())
    all_cats = ["reliability", "observability", "capability", "efficiency", "prompt_evolution"]

    # ── Load dynamic weights from evolution_weights.json (written by cycle_start.py) ──
    weights_data = _read_json_safe(weights_path, {})
    weights = weights_data.get("weights", {}) if isinstance(weights_data, dict) else {}
    goal_signals = weights_data.get("goal_signals", []) if isinstance(weights_data, dict) else []
    maturity_signals = weights_data.get("maturity_signals", {}) if isinstance(weights_data, dict) else {}

    # Use weights-based suggestion if available, else fall back to least-done
    suggestion = None
    if weights:
        suggestion = weights_data.get("suggestion")
    if not suggestion:
        # Fallback: least-done category
        pool = all_cats
        if pool:
            min_count = min(categories.get(c, 0) for c in pool)
            candidates = [c for c in pool if categories.get(c, 0) == min_count]
            suggestion = candidates[0] if candidates else None

    result = {
        "total_evolve_cycles": total,
        "all_time": categories,
        "recent_10": recent_categories,
        "suggestion": suggestion,
        "all_cats": all_cats,
        # Dynamic weights data (empty dicts if weights file not yet created)
        "weights": weights,
        "goal_signals": goal_signals,
        "maturity_signals": maturity_signals,
    }
    _BALANCE_CACHE["data"] = (result, c_mtime, j_mtime, w_mtime)
    return result


@_mmfile_cache([lambda: HISTORY_PATH, lambda: f"{MEMORY_DIR}/cycles.json"])
def load_activity():
    """Merge command history + cycle events into a unified activity feed (newest 50).

    Uses @_mmfile_cache keyed on command_history.json + cycles.json mtimes. Previously
    used hand-rolled _ACTIVITY_CACHE; now auto-registered in _MFILE_CACHES for auto-clear.
    TTL=5s caused full re-merge every 5s — ~0 re-merges between cycle boundaries now.
    """
    from app.data.message import load_history

    events = []
    type_map = {"goal": "goal", "message": "message", "bash": "bash_cmd"}
    history = load_history()
    for h in history:
        ts = h.get("timestamp")
        if not ts:
            continue
        etype = type_map.get(h.get("type"), "bash_cmd")
        events.append({
            "time": ts,
            "type": etype,
            "summary": (h.get("content") or "")[:120],
            "detail": h.get("result", ""),
        })
    cycles = load_cycles()
    if isinstance(cycles, list):
        # Only scan the most recent 30 cycles — activity feed only shows 50 events total,
        # and each cycle contributes at most 2 events (start + end). Scanning all cycles
        # is O(N) in cycle count; this caps it at O(30) regardless of total cycle count.
        recent_cycles = cycles[-30:] if len(cycles) > 30 else cycles
        for c in recent_cycles:
            if c.get("start"):
                events.append({
                    "time": c["start"],
                    "type": "cycle_start",
                    "summary": f"Cycle {c.get('cycle', '')} started",
                    "detail": (c.get("goal") or "")[:120],
                })
            if c.get("end"):
                dur = c.get("duration_seconds", "?")
                events.append({
                    "time": c["end"],
                    "type": "cycle_end",
                    "summary": f"Cycle {c.get('cycle', '')} completed ({dur}s)",
                    "detail": (c.get("goal") or "")[:120],
                })
    events.sort(key=lambda e: e.get("time", ""), reverse=True)
    return events[:50]
