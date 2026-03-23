"""Full-text search across journal, goals, cycles, and history."""

import os

from app.data._cache import _register_cache
from app.shared import MEMORY_DIR, GOALS_PATH, HISTORY_PATH


_SEARCH_CACHE = _register_cache()


def search(query):
    """Full-text search across journal, goals, cycles, and history.

    Cache key: (query, journal_mtime, archive_mtime, goals_mtime, cycles_mtime, history_mtime).
    Results are stable between file changes — no TTL decay needed. Previously @_cache(ttl=10)
    would re-run the search every 10s even when none of the source files had changed.
    """
    from app.data.journal import _parse_journal_entries
    from app.data.goal import load_goals
    from app.data.cycle import load_cycles
    from app.data.message import load_history

    _j_path  = f"{MEMORY_DIR}/journal.json"
    _a_path  = f"{MEMORY_DIR}/journal-archive.json"
    _c_path  = f"{MEMORY_DIR}/cycles.json"
    def _mt(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0
    cache_key = (query, _mt(_j_path), _mt(_a_path), _mt(GOALS_PATH), _mt(_c_path), _mt(HISTORY_PATH))
    cached = _SEARCH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if not query or len(query) < 2:
        result = {"results": [], "query": query, "error": "Query must be at least 2 characters"}
        _SEARCH_CACHE[cache_key] = result
        return result
    q = query.lower()
    results = []

    def snippet(text, qry, max_len=120):
        lower = text.lower()
        idx = lower.find(qry)
        if idx == -1:
            return text[:max_len]
        start = max(0, idx - 40)
        end = min(len(text), idx + len(qry) + 80)
        s = text[start:end].replace("\n", " ")
        if start > 0:
            s = "..." + s
        if end < len(text):
            s = s + "..."
        return s

    for entry in _parse_journal_entries():
        goal = entry.get("goal", "")
        # Support both "summary" (current schema) and "outcome" (archived/legacy entries)
        summary_text = entry.get("summary") or entry.get("outcome", "")
        actions = " ".join(entry.get("actions", []))
        searchable = f"{goal} {summary_text} {actions}"
        if q in searchable.lower():
            results.append({
                "source": "journal",
                "title": f"Cycle {entry.get('cycle')}: {goal[:60]}",
                "snippet": snippet(searchable, q),
                "cycle": entry.get("cycle"),
            })

    goals = load_goals()
    for goal in goals:
        content = goal.get("content", "")
        if q in content.lower():
            results.append({
                "source": "goal",
                "title": content[:80],
                "snippet": snippet(content, q),
                "status": goal.get("status"),
            })

    cycles = load_cycles()
    if isinstance(cycles, list):
        for cycle in cycles:
            goal_text = cycle.get("goal", "")
            if q in goal_text.lower():
                results.append({
                    "source": "cycle",
                    "title": f"Cycle {cycle.get('cycle')}: {goal_text[:60]}",
                    "snippet": goal_text,
                    "cycle": cycle.get("cycle"),
                    "status": cycle.get("status"),
                })

    history = load_history()
    for cmd in history:
        content = cmd.get("content", "")
        cmd_type = cmd.get("type", "")
        if q in content.lower() or q in cmd_type.lower():
            results.append({
                "source": "history",
                "title": f"[{cmd_type}] {content[:60]}",
                "snippet": snippet(content, q),
                "timestamp": cmd.get("timestamp"),
            })

    result = {"results": results, "query": query, "count": len(results)}
    _SEARCH_CACHE[cache_key] = result
    return result
