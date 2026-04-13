"""
Action suggestions for the Agent Overview tab.

Enum Reference: See prompts/enum.md → Suggestion Priority, Suggestion Category, Evolution Category.
"""

import os
from datetime import datetime, timezone

from app.data._cache import _register_cache
from app.shared import AGENT_DIR, MEMORY_DIR, MESSAGES_DIR, GOALS_PATH, ERROR_LOG_PATH


_SUGGEST_CACHE = _register_cache()


def load_suggest():
    """Build ranked action suggestions for the Agent Overview tab.

    Uses compound mtime-based caching keyed on all source files + workspace dir + date bucket.
    Previously @_cache(ttl=60) caused re-computation every 60 seconds even during idle cycles
    when goal.json, inbox.json, journal.json, errors, and cycles.json had not changed.
    Mtime caching gives ~0 re-computations between cycle boundaries (~5 min apart).

    Cache key: (goal_m, inbox_m, jour_m, arch_m, err_m, cyc_m, ws_m, date_bucket)
    where date_bucket = today's ISO date string (ensures midnight invalidation for error filter).
    """
    from app.data.goal import load_goals
    from app.data.message import load_inbox
    from app.data.journal import load_journal
    from app.data.system import load_errors, load_system_info
    from app.data.cycle import load_cycles

    goal_path  = GOALS_PATH
    inbox_path = os.path.join(MESSAGES_DIR, "inbox.json")
    jour_path  = os.path.join(MEMORY_DIR, "journal.json")
    arch_path  = os.path.join(MEMORY_DIR, "journal-archive.json")
    err_path   = ERROR_LOG_PATH
    cyc_path   = os.path.join(MEMORY_DIR, "cycles.json")
    ws_path    = os.path.join(AGENT_DIR, "workspace")

    def _m(p):
        try: return os.path.getmtime(p)
        except OSError: return 0.0

    goal_m  = _m(goal_path)
    inbox_m = _m(inbox_path)
    jour_m  = _m(jour_path)
    arch_m  = _m(arch_path)
    err_m   = _m(err_path)
    cyc_m   = _m(cyc_path)
    ws_m    = _m(ws_path)
    date_bucket = datetime.now(timezone.utc).date().isoformat()  # invalidate at midnight

    cached = _SUGGEST_CACHE.get("data")
    if cached is not None:
        result, c_go, c_in, c_jo, c_ar, c_er, c_cy, c_ws, c_dt = cached
        if (c_go == goal_m and c_in == inbox_m and c_jo == jour_m and c_ar == arch_m and
                c_er == err_m and c_cy == cyc_m and c_ws == ws_m and c_dt == date_bucket):
            return result

    suggestions = []
    goals = load_goals()
    in_progress = [g for g in goals if g.get("status") == "in_progress"]
    pending = [g for g in goals if g.get("status") == "pending"]
    if in_progress:
        suggestions.append({
            "priority": "high", "category": "goal",
            "action": f"Continue in-progress goal: {in_progress[0].get('content', '')[:80]}",
            "reason": f"{len(in_progress)} goal(s) in progress",
        })
    if pending:
        suggestions.append({
            "priority": "high", "category": "goal",
            "action": f"Start pending goal: {pending[0].get('content', '')[:80]}",
            "reason": f"{len(pending)} goal(s) pending",
        })
    inbox = load_inbox()
    if inbox:
        suggestions.append({
            "priority": "high", "category": "goal",
            "action": "Process inbox messages",
            "reason": f"{len(inbox)} unprocessed message(s) in inbox",
        })
    journal_data = load_journal(limit=0)
    journal_count = journal_data.get("total", 0)
    if journal_count > 30:
        suggestions.append({
            "priority": "medium", "category": "efficiency",
            "action": "Run: uv run python scripts/journal_archive.py",
            "reason": f"Journal has {journal_count} entries — archive old entries to speed up loading",
        })
    errors = load_errors()
    if isinstance(errors, list) and errors:
        today = date_bucket  # reuse the already-computed date string
        recent_errors = [e for e in errors if e.get("timestamp", "")[:10] >= today]
        if recent_errors:
            suggestions.append({
                "priority": "medium", "category": "reliability",
                "action": "Investigate recent server errors",
                "reason": f"{len(recent_errors)} error(s) logged today",
            })
    if not any(s["priority"] == "high" for s in suggestions):
        cycles = load_cycles()
        if isinstance(cycles, list):
            evolve_cycles = [c for c in cycles if c.get("type") == "evolve" and c.get("category")]
            categories = {}
            for c in evolve_cycles:
                cat = c["category"]
                categories[cat] = categories.get(cat, 0) + 1
            all_cats = ["reliability", "observability", "capability", "efficiency", "prompt_evolution"]
            underserved = sorted(all_cats, key=lambda c: categories.get(c, 0))
            if underserved:
                suggestions.append({
                    "priority": "low", "category": "evolve",
                    "action": f"Evolve: focus on {underserved[0].replace('_', ' ')} (least served category)",
                    "reason": f"{underserved[0]} has {categories.get(underserved[0], 0)} cycles",
                })
    # Use cached system info instead of spawning a du subprocess
    sysinfo = load_system_info()
    ws_mb = sysinfo.get("workspace_mb") or 0
    if ws_mb > 800:
        suggestions.append({
            "priority": "medium", "category": "efficiency",
            "action": "Clean up workspace — approaching 1GB limit",
            "reason": f"Workspace is {ws_mb}MB (limit: 1GB)",
        })
    priority_order = {"high": 0, "medium": 1, "low": 2}
    suggestions.sort(key=lambda s: priority_order.get(s["priority"], 3))
    result = suggestions
    _SUGGEST_CACHE["data"] = (result, goal_m, inbox_m, jour_m, arch_m, err_m, cyc_m, ws_m, date_bucket)
    return result
