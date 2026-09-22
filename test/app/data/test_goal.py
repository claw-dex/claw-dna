"""Unit tests for app.data.goal."""

from __future__ import annotations

import json

import pytest

from app.data import _cache as cache_mod
from app.data import goal as goal_mod


@pytest.fixture
def patch_goal_paths(monkeypatch, tmp_path):
    """Redirect GOALS_PATH and MEMORY_DIR for both goal and shared modules."""
    memory = tmp_path / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    goals_path = memory / "goal.json"

    monkeypatch.setattr("app.shared.MEMORY_DIR", str(memory))
    monkeypatch.setattr("app.shared.GOALS_PATH", str(goals_path))
    monkeypatch.setattr(goal_mod, "MEMORY_DIR", str(memory))
    monkeypatch.setattr(goal_mod, "GOALS_PATH", str(goals_path))

    # Also redirect cycle module's MEMORY_DIR (load_goal_stats uses cycles.json).
    from app.data import cycle as cycle_mod

    monkeypatch.setattr(cycle_mod, "MEMORY_DIR", str(memory))

    # Clear all caches so each test starts fresh.
    cache_mod._cache_clear_all()

    return {"memory": memory, "goals": goals_path, "cycles": memory / "cycles.json"}


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


# ---------- load_goals ----------


def test_load_goals_returns_list(patch_goal_paths):
    _write(patch_goal_paths["goals"], [{"id": "g1", "status": "completed"}])
    cache_mod._cache_clear_all()
    assert goal_mod.load_goals() == [{"id": "g1", "status": "completed"}]


def test_load_goals_missing_returns_empty_list(patch_goal_paths):
    cache_mod._cache_clear_all()
    assert goal_mod.load_goals() == []


def test_load_goals_malformed_returns_default(patch_goal_paths):
    patch_goal_paths["goals"].write_text("{not json}")
    cache_mod._cache_clear_all()
    assert goal_mod.load_goals() == []


# ---------- load_goal_stats ----------


def test_load_goal_stats_empty(patch_goal_paths):
    cache_mod._cache_clear_all()
    stats = goal_mod.load_goal_stats()
    assert stats["total"] == 0
    assert stats["completed"] == 0
    assert stats["failed"] == 0
    assert stats["completion_rate"] == 0.0
    assert stats["avg_goal_duration_seconds"] is None
    assert stats["recent_goals"] == []
    assert stats["goal_cycle_durations"] == []


def test_load_goal_stats_counts_statuses(patch_goal_paths):
    _write(
        patch_goal_paths["goals"],
        [
            {"id": "g1", "status": "completed", "created_at": "2026-01-01"},
            {"id": "g2", "status": "completed", "created_at": "2026-01-02"},
            {"id": "g3", "status": "failed", "created_at": "2026-01-03"},
            {"id": "g4", "status": "in_progress", "created_at": "2026-01-04"},
        ],
    )
    cache_mod._cache_clear_all()
    stats = goal_mod.load_goal_stats()
    assert stats["total"] == 4
    assert stats["completed"] == 2
    assert stats["failed"] == 1
    assert stats["completion_rate"] == 0.5


def test_load_goal_stats_recent_goals_sorted_newest_first(patch_goal_paths):
    _write(
        patch_goal_paths["goals"],
        [
            {"id": f"g{i}", "status": "completed", "created_at": f"2026-01-{i:02d}"}
            for i in range(1, 8)
        ],
    )
    cache_mod._cache_clear_all()
    stats = goal_mod.load_goal_stats()
    assert len(stats["recent_goals"]) == 5
    assert stats["recent_goals"][0]["id"] == "g7"
    assert stats["recent_goals"][-1]["id"] == "g3"


def test_load_goal_stats_uses_source_timestamp_fallback(patch_goal_paths):
    _write(
        patch_goal_paths["goals"],
        [
            {"id": "a", "status": "completed", "source_timestamp": "2026-01-01"},
            {"id": "b", "status": "completed", "source_timestamp": "2026-02-01"},
        ],
    )
    cache_mod._cache_clear_all()
    stats = goal_mod.load_goal_stats()
    assert stats["recent_goals"][0]["id"] == "b"


def test_load_goal_stats_computes_avg_from_cycles(patch_goal_paths):
    _write(patch_goal_paths["goals"], [{"id": "a", "status": "completed"}])
    _write(
        patch_goal_paths["cycles"],
        [
            {"cycle": 1, "type": "goal", "duration_seconds": 100},
            {"cycle": 2, "type": "goal", "duration_seconds": 200},
            {"cycle": 3, "type": "evolve", "duration_seconds": 5},  # excluded
            {"cycle": 4, "type": "goal", "duration_seconds": None},  # excluded
        ],
    )
    cache_mod._cache_clear_all()
    stats = goal_mod.load_goal_stats()
    assert stats["avg_goal_duration_seconds"] == 150
    assert stats["goal_cycle_durations"] == [(1, 100), (2, 200)]
