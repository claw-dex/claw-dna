"""
Goal loaders and stats.

Enum Reference: See prompts/enum.md → Goal Status for valid status values.
"""

from app.data._cache import _mfile_cache, _mmfile_cache
from app.shared import GOALS_PATH, MEMORY_DIR


@_mfile_cache(lambda: GOALS_PATH, list)
def load_goals(data):
    """Load goal.json — mtime-cached, invalidates only when goals change."""
    return data


@_mmfile_cache([lambda: GOALS_PATH, lambda: f"{MEMORY_DIR}/cycles.json"])
def load_goal_stats():
    """Compute goal performance metrics for the Overview panel.

    Uses @_mmfile_cache keyed on goal.json + cycles.json mtimes. Goal stats change
    only when a goal is added/completed or a new cycle is recorded (both mtime changes).
    Previously used hand-rolled _GOAL_STATS_CACHE; now auto-registered in _MFILE_CACHES.

    Returns:
        completed: int
        failed: int
        total: int
        completion_rate: float (0-1)
        avg_goal_duration_seconds: float | None
        recent_goals: list of last 5 goal entries (newest first)
        goal_cycle_durations: list of (cycle_num, duration_seconds) for sparkline
    """
    from app.data.cycle import load_cycles

    goals = load_goals() or []
    total = len(goals)
    completed = sum(1 for g in goals if g.get("status") == "completed")
    failed = sum(1 for g in goals if g.get("status") == "failed")
    completion_rate = completed / total if total else 0.0

    # Delegation stats: count goals that have a `delegated_to` field.
    delegated = [g for g in goals if isinstance(g.get("delegated_to"), dict)]
    delegated_total = len(delegated)
    delegated_awaiting = sum(
        1 for g in delegated if g.get("status") in ("pending", "in_progress")
    )
    delegated_completed = sum(1 for g in delegated if g.get("status") == "completed")
    delegated_failed = sum(1 for g in delegated if g.get("status") == "failed")
    delegated_by_agent: dict[str, int] = {}
    for g in delegated:
        name = g["delegated_to"].get("name")
        if name:
            delegated_by_agent[name] = delegated_by_agent.get(name, 0) + 1

    # Recent goals (newest first by created_at)
    sorted_goals = sorted(
        goals,
        key=lambda g: g.get("created_at") or g.get("source_timestamp") or "",
        reverse=True,
    )
    recent_goals = sorted_goals[:5]

    # Goal cycle durations from cycles.json
    cycles = load_cycles() or []
    goal_cycles = [
        c
        for c in cycles
        if c.get("cycle_type") == "goal" and c.get("duration_seconds") is not None
    ]
    goal_cycle_durations = [
        (c.get("cycle_number", "?"), c["duration_seconds"]) for c in goal_cycles
    ]

    avg_dur = None
    if goal_cycles:
        avg_dur = sum(c["duration_seconds"] for c in goal_cycles) / len(goal_cycles)

    return {
        "completed": completed,
        "failed": failed,
        "total": total,
        "completion_rate": completion_rate,
        "avg_goal_duration_seconds": avg_dur,
        "recent_goals": recent_goals,
        "goal_cycle_durations": goal_cycle_durations,
        "delegated_total": delegated_total,
        "delegated_awaiting": delegated_awaiting,
        "delegated_completed": delegated_completed,
        "delegated_failed": delegated_failed,
        "delegated_by_agent": delegated_by_agent,
    }
