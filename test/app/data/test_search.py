"""Tests for app.data.search."""

from __future__ import annotations

import json

import pytest

import sys

import app.data.search  # noqa: F401  ensure submodule loaded

search = sys.modules["app.data.search"]
from app.data import journal as journal_mod
from app.data import _cache as data_cache


@pytest.fixture(autouse=True)
def _clean_caches():
    search._SEARCH_CACHE.clear()
    journal_mod._JOURNAL_CACHE.clear()
    for c in data_cache._MFILE_CACHES:
        c.clear()
    yield
    search._SEARCH_CACHE.clear()
    journal_mod._JOURNAL_CACHE.clear()
    for c in data_cache._MFILE_CACHES:
        c.clear()


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    """Wire up tmp paths across all modules search() pulls from."""
    mem = tmp_path / "memory"
    msgs = tmp_path / "messages"
    mem.mkdir()
    msgs.mkdir()

    goals_path = mem / "goal.json"
    history_path = mem / "command_history.json"

    # search.py module globals (use module obj directly because the package
    # re-exports `search` as a function, shadowing the submodule attribute).
    monkeypatch.setattr(search, "MEMORY_DIR", str(mem))
    monkeypatch.setattr(search, "GOALS_PATH", str(goals_path))
    monkeypatch.setattr(search, "HISTORY_PATH", str(history_path))

    # downstream loaders
    monkeypatch.setattr("app.data.journal.MEMORY_DIR", str(mem))
    monkeypatch.setattr("app.data.goal.GOALS_PATH", str(goals_path))
    monkeypatch.setattr("app.data.goal.MEMORY_DIR", str(mem))
    monkeypatch.setattr("app.data.cycle.MEMORY_DIR", str(mem))
    monkeypatch.setattr("app.data.cycle.HISTORY_PATH", str(history_path))
    monkeypatch.setattr("app.data.cycle.LOGS_DIR", str(mem / "logs"))
    monkeypatch.setattr("app.data.message.MEMORY_DIR", str(mem))
    monkeypatch.setattr("app.data.message.MESSAGES_DIR", str(msgs))
    monkeypatch.setattr("app.data.message.HISTORY_PATH", str(history_path))

    return {
        "memory": mem,
        "messages": msgs,
        "goals_path": goals_path,
        "history_path": history_path,
    }


def _w(p, obj):
    p.write_text(json.dumps(obj))


def test_search_query_too_short(stubbed):
    out = search.search("a")
    assert out["error"] == "Query must be at least 2 characters"
    assert out["results"] == []


def test_search_empty_query(stubbed):
    out = search.search("")
    assert "error" in out
    assert out["results"] == []


def test_search_no_data_no_results(stubbed):
    out = search.search("hello")
    assert out["query"] == "hello"
    assert out["count"] == 0
    assert out["results"] == []


def test_search_journal_match(stubbed):
    _w(
        stubbed["memory"] / "journal.json",
        [
            {
                "cycle": 5,
                "goal": "fix the foo bug",
                "summary": "patched logic",
                "actions": ["edit", "test"],
            }
        ],
    )
    out = search.search("foo")
    assert out["count"] == 1
    r = out["results"][0]
    assert r["source"] == "journal"
    assert r["cycle"] == 5
    assert "foo" in r["snippet"].lower()


def test_search_journal_uses_outcome_for_legacy(stubbed):
    _w(
        stubbed["memory"] / "journal-archive.json",
        [{"cycle": 1, "goal": "g", "outcome": "old MARKER text", "actions": []}],
    )
    out = search.search("marker")
    assert out["count"] == 1
    assert out["results"][0]["source"] == "journal"


def test_search_goal_match(stubbed):
    _w(
        stubbed["goals_path"],
        [{"content": "Build a SEARCH index", "status": "active"}],
    )
    out = search.search("search")
    sources = [r["source"] for r in out["results"]]
    assert "goal" in sources
    g = next(r for r in out["results"] if r["source"] == "goal")
    assert g["status"] == "active"


def test_search_cycle_match(stubbed):
    _w(
        stubbed["memory"] / "cycles.json",
        [{"cycle": 7, "goal": "explore widget", "status": "completed"}],
    )
    out = search.search("widget")
    sources = [r["source"] for r in out["results"]]
    assert "cycle" in sources
    c = next(r for r in out["results"] if r["source"] == "cycle")
    assert c["cycle"] == 7
    assert c["status"] == "completed"


def test_search_cycles_non_list_safe(stubbed):
    _w(stubbed["memory"] / "cycles.json", {"oops": True})
    out = search.search("anything")
    # should not crash; no cycle results
    assert all(r["source"] != "cycle" for r in out["results"])


def test_search_history_match_in_content(stubbed):
    _w(
        stubbed["history_path"],
        [{"type": "bash", "content": "ls foobar", "timestamp": "t1"}],
    )
    out = search.search("foobar")
    sources = [r["source"] for r in out["results"]]
    assert "history" in sources


def test_search_history_match_in_type(stubbed):
    _w(
        stubbed["history_path"],
        [{"type": "specialcmd", "content": "x", "timestamp": "t1"}],
    )
    out = search.search("specialcmd")
    sources = [r["source"] for r in out["results"]]
    assert "history" in sources


def test_search_case_insensitive(stubbed):
    _w(
        stubbed["memory"] / "journal.json",
        [{"cycle": 1, "goal": "FixBug", "summary": "", "actions": []}],
    )
    out = search.search("fixbug")
    assert out["count"] == 1


def test_search_combines_multiple_sources(stubbed):
    _w(
        stubbed["memory"] / "journal.json",
        [{"cycle": 1, "goal": "alpha task", "summary": "", "actions": []}],
    )
    _w(stubbed["goals_path"], [{"content": "alpha goal", "status": "open"}])
    _w(
        stubbed["memory"] / "cycles.json",
        [{"cycle": 2, "goal": "alpha cycle", "status": "completed"}],
    )
    _w(stubbed["history_path"], [{"type": "cmd", "content": "alpha hist"}])
    out = search.search("alpha")
    sources = sorted({r["source"] for r in out["results"]})
    assert sources == ["cycle", "goal", "history", "journal"]
    assert out["count"] == 4


def test_search_snippet_window_around_match(stubbed):
    long_goal = "x" * 200 + " NEEDLE " + "y" * 200
    _w(
        stubbed["memory"] / "journal.json",
        [{"cycle": 1, "goal": long_goal, "summary": "", "actions": []}],
    )
    out = search.search("needle")
    assert out["count"] == 1
    snip = out["results"][0]["snippet"]
    assert "NEEDLE" in snip
    # snippet trims to a window with ellipses
    assert snip.startswith("...") or len(long_goal) <= 120


def test_search_caches_result(stubbed):
    _w(stubbed["goals_path"], [{"content": "cache me", "status": "x"}])
    a = search.search("cache")
    b = search.search("cache")
    assert a is b  # cached identical object reference
