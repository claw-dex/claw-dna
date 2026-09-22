"""Unit tests for app.data.suggest."""

from __future__ import annotations

import pytest

from app.data import suggest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Repoint suggest module path constants to tmp_path & clear caches."""
    monkeypatch.setattr("app.data.suggest.AGENT_DIR", str(tmp_path))
    monkeypatch.setattr("app.data.suggest.MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setattr("app.data.suggest.MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setattr(
        "app.data.suggest.GOALS_PATH", str(tmp_path / "memory" / "goal.json")
    )
    monkeypatch.setattr(
        "app.data.suggest.ERROR_LOG_PATH",
        str(tmp_path / "memory" / "server_errors.json"),
    )
    (tmp_path / "memory").mkdir()
    (tmp_path / "messages").mkdir()
    (tmp_path / "workspace").mkdir()
    from app.data import _cache as cache_mod

    cache_mod._cache_clear_all()
    yield tmp_path
    cache_mod._cache_clear_all()


@pytest.fixture
def patched_loaders(monkeypatch):
    """Patch the data loaders that suggest.load_suggest pulls in lazily."""
    state = {
        "goals": [],
        "inbox": [],
        "journal": {"total": 0, "entries": []},
        "errors": [],
        "cycles": [],
        "system_info": {"workspace_mb": 0},
    }

    monkeypatch.setattr("app.data.goal.load_goals", lambda: state["goals"])
    monkeypatch.setattr("app.data.message.load_inbox", lambda: state["inbox"])
    monkeypatch.setattr(
        "app.data.journal.load_journal", lambda limit=0: state["journal"]
    )
    monkeypatch.setattr("app.data.system.load_errors", lambda: state["errors"])
    monkeypatch.setattr(
        "app.data.system.load_system_info", lambda: state["system_info"]
    )
    monkeypatch.setattr("app.data.cycle.load_cycles", lambda: state["cycles"])
    return state


def _priorities(suggestions):
    return [s["priority"] for s in suggestions]


def test_no_signals_returns_empty_or_low(isolated, patched_loaders):
    result = suggest.load_suggest()
    # No high-priority signals — may include a low evolve suggestion
    assert all(s["priority"] in ("low", "medium") for s in result)


def test_in_progress_goal_yields_high(isolated, patched_loaders):
    patched_loaders["goals"] = [
        {"status": "in_progress", "content": "Finish the thing"}
    ]
    result = suggest.load_suggest()
    assert any(s["priority"] == "high" and "Continue" in s["action"] for s in result)


def test_pending_goal_yields_high(isolated, patched_loaders):
    patched_loaders["goals"] = [{"status": "pending", "content": "New work"}]
    result = suggest.load_suggest()
    assert any(
        s["priority"] == "high" and "Start pending" in s["action"] for s in result
    )


def test_inbox_messages_yield_high(isolated, patched_loaders):
    patched_loaders["inbox"] = [{"type": "goal", "content": "x"}]
    result = suggest.load_suggest()
    assert any("Process inbox" in s["action"] for s in result)


def test_large_journal_yields_archive_suggestion(isolated, patched_loaders):
    patched_loaders["journal"] = {"total": 50}
    result = suggest.load_suggest()
    assert any("journal_archive.py" in s["action"] for s in result)


def test_small_journal_no_archive_suggestion(isolated, patched_loaders):
    patched_loaders["journal"] = {"total": 5}
    result = suggest.load_suggest()
    assert not any("journal_archive.py" in s["action"] for s in result)


def test_recent_errors_yield_reliability_suggestion(isolated, patched_loaders):
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    patched_loaders["errors"] = [{"timestamp": f"{today}T10:00:00Z"}]
    result = suggest.load_suggest()
    assert any(s["category"] == "reliability" for s in result)


def test_old_errors_dont_yield_suggestion(isolated, patched_loaders):
    patched_loaders["errors"] = [{"timestamp": "2000-01-01T00:00:00Z"}]
    result = suggest.load_suggest()
    assert not any(s["category"] == "reliability" for s in result)


def test_evolve_suggestion_when_no_high_priority(isolated, patched_loaders):
    patched_loaders["cycles"] = [
        {"cycle_type": "evolve", "cycle_category": "reliability"},
        {"cycle_type": "evolve", "cycle_category": "reliability"},
    ]
    result = suggest.load_suggest()
    evolve = [s for s in result if s["category"] == "evolve"]
    assert evolve, "expected evolve suggestion when no high-priority items"
    # Least-served category should NOT be reliability since it has 2
    assert "reliability" not in evolve[0]["action"]


def test_no_evolve_suggestion_when_high_priority_exists(isolated, patched_loaders):
    patched_loaders["goals"] = [{"status": "pending", "content": "Go"}]
    patched_loaders["cycles"] = [
        {"cycle_type": "evolve", "cycle_category": "reliability"}
    ]
    result = suggest.load_suggest()
    assert not any(s["category"] == "evolve" for s in result)


def test_workspace_over_threshold_yields_cleanup(isolated, patched_loaders):
    patched_loaders["system_info"] = {"workspace_mb": 900}
    result = suggest.load_suggest()
    assert any("workspace" in s["action"].lower() for s in result)


def test_workspace_under_threshold_no_cleanup(isolated, patched_loaders):
    patched_loaders["system_info"] = {"workspace_mb": 100}
    result = suggest.load_suggest()
    assert not any("approaching 1GB" in s.get("action", "") for s in result)


def test_results_sorted_by_priority(isolated, patched_loaders):
    patched_loaders["goals"] = [{"status": "pending", "content": "A"}]
    patched_loaders["system_info"] = {"workspace_mb": 900}
    result = suggest.load_suggest()
    pri_indices = {"high": 0, "medium": 1, "low": 2}
    nums = [pri_indices[s["priority"]] for s in result]
    assert nums == sorted(nums)


def test_long_goal_content_truncated_in_action(isolated, patched_loaders):
    long_text = "x" * 500
    patched_loaders["goals"] = [{"status": "in_progress", "content": long_text}]
    result = suggest.load_suggest()
    high = [s for s in result if s["priority"] == "high"]
    # 80-char truncation in action prefix
    assert all(len(s["action"]) < 200 for s in high)
