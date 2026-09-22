"""Tests for the cycle_close + cycle_start additions that wire the learning loop.

Covers:
  - cycle_close._tick_nudge_counters (locked + idempotent shape)
  - cycle_close._dispatch_skill_bump_bg (command structure + 30s delay)
  - cycle_start._skill_keywords (directory-name only)
  - cycle_start._compute_nudge_signals (failures / hot categories / orphans)
  - cycle_start._render_skill_nudges (mode gating + content)
  - cycle_start._render_skill_lifecycle_report
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import cycle_close as cc
import cycle_start as cs
import skill_manage as sm

# ── cycle_close._tick_nudge_counters ──────────────────────────────────────


@pytest.fixture
def nudges_sandbox(tmp_path, monkeypatch):
    nudges = tmp_path / "memory" / "nudges.json"
    monkeypatch.setattr(sm, "NUDGES_PATH", nudges)
    return nudges


def test_tick_nudge_counters_creates_file(nudges_sandbox):
    cc._tick_nudge_counters()
    data = json.loads(nudges_sandbox.read_text())
    assert data["cycles_since_skill_review"] == 1
    assert data["cycles_since_skill_create"] == 1


def test_tick_nudge_counters_increments(nudges_sandbox):
    nudges_sandbox.parent.mkdir(parents=True, exist_ok=True)
    nudges_sandbox.write_text(
        json.dumps({"cycles_since_skill_review": 4, "cycles_since_skill_create": 9})
    )
    cc._tick_nudge_counters()
    data = json.loads(nudges_sandbox.read_text())
    assert data["cycles_since_skill_review"] == 5
    assert data["cycles_since_skill_create"] == 10


def test_tick_nudge_counters_swallows_errors(nudges_sandbox, monkeypatch):
    """Even if the locked write blows up, the call must not raise."""

    def boom(*a, **kw):
        raise RuntimeError("simulated")

    monkeypatch.setattr(sm, "_mutate_nudges", boom)
    cc._tick_nudge_counters()  # must not raise


# ── cycle_close._dispatch_skill_bump_bg ───────────────────────────────────


def test_dispatch_skill_bump_bg_includes_sleep_and_script(monkeypatch, tmp_path):
    """The detached child must shell out as `sleep 30 && python skill_manage.py ...`."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "skill_manage.py"
    script.write_text("# stub\n")
    monkeypatch.setattr(cc, "SCRIPTS", scripts_dir)
    monkeypatch.setattr(cc, "MEMORY", tmp_path / "memory")
    (tmp_path / "memory").mkdir(exist_ok=True)

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(cc.subprocess, "Popen", fake_popen)
    cc._dispatch_skill_bump_bg(42)
    assert captured["cmd"][0] == "/bin/sh"
    assert captured["cmd"][1] == "-c"
    shell = captured["cmd"][2]
    assert shell.startswith(f"sleep {cc._SKILL_BUMP_DELAY_SECS} &&")
    assert "skill_manage.py" in shell
    assert "bump-usage" in shell
    assert "--cycle 42" in shell
    assert captured["kwargs"]["start_new_session"] is True


def test_dispatch_skill_bump_bg_quiet_when_script_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "SCRIPTS", tmp_path / "nonexistent")
    monkeypatch.setattr(cc, "MEMORY", tmp_path / "memory")
    # No Popen should be called; we just verify no exception escapes.
    called = []

    def fake_popen(*a, **kw):
        called.append(True)
        raise AssertionError("should not be called")

    monkeypatch.setattr(cc.subprocess, "Popen", fake_popen)
    cc._dispatch_skill_bump_bg(99)
    assert called == []


def test_dispatch_skill_bump_bg_delay_constant():
    """The delay constant must be at least 1s (regression guard)."""
    assert cc._SKILL_BUMP_DELAY_SECS >= 1


# ── cycle_start._skill_keywords ───────────────────────────────────────────


def test_skill_keywords_returns_dir_names_only(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    skills.mkdir()
    for name in ("change-portal-theme", "search", "memory-recall"):
        (skills / name).mkdir()
        (skills / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: triggers include with this and your\n---\n"
        )
    monkeypatch.setattr(cs, "SKILLS_DIR_CYS", skills)
    tokens = cs._skill_keywords()
    assert tokens == {"change-portal-theme", "search", "memory-recall"}
    # Common english words harvested from descriptions are NOT in the set.
    for stop in ("with", "this", "your", "triggers", "include"):
        assert stop not in tokens


def test_skill_keywords_skips_dot_dirs(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / ".archive").mkdir()
    (skills / ".archive" / "old").mkdir()
    (skills / ".archive" / "old" / "SKILL.md").write_text(
        "---\nname: old\ndescription: x\n---\n"
    )
    (skills / "real").mkdir()
    (skills / "real" / "SKILL.md").write_text("---\nname: real\ndescription: x\n---\n")
    monkeypatch.setattr(cs, "SKILLS_DIR_CYS", skills)
    tokens = cs._skill_keywords()
    assert tokens == {"real"}


# ── cycle_start._compute_nudge_signals ────────────────────────────────────


def test_compute_nudge_signals_detects_repeated_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "SKILLS_DIR_CYS", tmp_path / "skills_empty")
    journal = [
        {"cycle_status": "failed", "error": "timeout connecting to db"}
        for _ in range(4)
    ]
    out = cs._compute_nudge_signals(journal, goals=[], recent_dream_files=[])
    assert out["repeated_failures"]
    assert out["repeated_failures"][0]["count"] >= 3


def test_compute_nudge_signals_detects_hot_goal_categories(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "SKILLS_DIR_CYS", tmp_path / "skills_empty")
    goals = [{"category": "reporting"} for _ in range(5)]
    out = cs._compute_nudge_signals(journal=[], goals=goals, recent_dream_files=[])
    assert out["hot_goal_categories"]
    assert out["hot_goal_categories"][0]["category"] == "reporting"


def test_compute_nudge_signals_skips_hot_categories_with_matching_skill(
    monkeypatch, tmp_path
):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "reporting").mkdir()
    (skills / "reporting" / "SKILL.md").write_text(
        "---\nname: reporting\ndescription: x\n---\n"
    )
    monkeypatch.setattr(cs, "SKILLS_DIR_CYS", skills)
    goals = [{"category": "reporting"} for _ in range(5)]
    out = cs._compute_nudge_signals(journal=[], goals=goals, recent_dream_files=[])
    assert out["hot_goal_categories"] == []


def test_compute_nudge_signals_detects_orphan_learnings(monkeypatch, tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "search").mkdir()
    (skills / "search" / "SKILL.md").write_text(
        "---\nname: search\ndescription: x\n---\n"
    )
    monkeypatch.setattr(cs, "SKILLS_DIR_CYS", skills)
    learning_file = tmp_path / "learning_xyz.md"
    learning_file.write_text("This learning covers prometheus alerting tactics.\n")
    recent = [
        {
            "path": str(learning_file),
            "kind": "learning",
            "mtime": "2026-04-30T00:00:00+00:00",
        }
    ]
    out = cs._compute_nudge_signals(journal=[], goals=[], recent_dream_files=recent)
    assert len(out["orphan_learnings"]) == 1


def test_compute_nudge_signals_skips_learnings_that_mention_a_skill(
    monkeypatch, tmp_path
):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "search").mkdir()
    (skills / "search" / "SKILL.md").write_text(
        "---\nname: search\ndescription: x\n---\n"
    )
    monkeypatch.setattr(cs, "SKILLS_DIR_CYS", skills)
    learning_file = tmp_path / "learning_xyz.md"
    learning_file.write_text("Use the search skill to find old cycles.\n")
    recent = [
        {
            "path": str(learning_file),
            "kind": "learning",
            "mtime": "2026-04-30T00:00:00+00:00",
        }
    ]
    out = cs._compute_nudge_signals(journal=[], goals=[], recent_dream_files=recent)
    assert out["orphan_learnings"] == []


# ── cycle_start._render_skill_nudges ──────────────────────────────────────


def test_render_skill_nudges_returns_none_for_non_goal_or_evolve_mode():
    state = {"thresholds": {}}
    signals = {
        "repeated_failures": [{"pattern": "x", "count": 5}],
        "hot_goal_categories": [],
        "orphan_learnings": [],
    }
    assert cs._render_skill_nudges(state, signals, "dream") is None
    assert cs._render_skill_nudges(state, signals, None) is None


def test_render_skill_nudges_emits_block_for_failures():
    state = {
        "thresholds": {"review_every": 10, "create_every": 25},
        "last_nudge_cycle": 0,
    }
    signals = {
        "repeated_failures": [{"pattern": "db timeout", "count": 4}],
        "hot_goal_categories": [],
        "orphan_learnings": [],
    }
    out = cs._render_skill_nudges(state, signals, "goal")
    assert out is not None
    assert "[SKILL NUDGE]" in out
    assert "db timeout" in out


def test_render_skill_nudges_silent_when_no_signals_and_thresholds_not_hit():
    state = {
        "thresholds": {"review_every": 10, "create_every": 25, "consolidate_every": 50},
        "cycles_since_skill_create": 0,
        "cycles_since_skill_review": 0,
        "last_nudge_cycle": 0,
    }
    signals = {
        "repeated_failures": [],
        "hot_goal_categories": [],
        "orphan_learnings": [],
    }
    assert cs._render_skill_nudges(state, signals, "evolve") is None


def test_render_skill_nudges_threshold_only_block():
    state = {
        "thresholds": {"review_every": 10, "create_every": 25, "consolidate_every": 50},
        "cycles_since_skill_create": 30,
        "cycles_since_skill_review": 0,
        "last_nudge_cycle": 0,
    }
    signals = {
        "repeated_failures": [],
        "hot_goal_categories": [],
        "orphan_learnings": [],
    }
    out = cs._render_skill_nudges(state, signals, "evolve")
    assert out is not None
    assert "skill create" in out


# ── cycle_start._render_skill_lifecycle_report ────────────────────────────


def test_render_skill_lifecycle_report_none_when_no_transitions(monkeypatch):
    fake = type("D", (), {"active_to_stale": [], "stale_to_archived": []})()

    class FakeMod:
        @staticmethod
        def compute():
            return fake

        @staticmethod
        def report(d):
            return "ignored"

    monkeypatch.setitem(__import__("sys").modules, "scripts.skill_lifecycle", FakeMod)
    out = cs._render_skill_lifecycle_report()
    assert out is None


def test_render_skill_lifecycle_report_returns_block_when_transitions(monkeypatch):
    fake = type(
        "D",
        (),
        {
            "active_to_stale": [("foo", "ts")],
            "stale_to_archived": [],
        },
    )()

    class FakeMod:
        @staticmethod
        def compute():
            return fake

        @staticmethod
        def report(d):
            return "  active→stale (1):\n    - foo (last activity ts)"

    monkeypatch.setitem(__import__("sys").modules, "scripts.skill_lifecycle", FakeMod)
    out = cs._render_skill_lifecycle_report()
    assert out is not None
    assert "[SKILL LIFECYCLE]" in out
    assert "foo" in out


# ── cycle_start._load_nudge_state ─────────────────────────────────────────


def test_load_nudge_state_returns_defaults_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "NUDGES_PATH", tmp_path / "nudges.json")
    state = cs._load_nudge_state()
    assert state["cycles_since_skill_review"] == 0
    assert "review_every" in state["thresholds"]


def test_load_nudge_state_merges_overrides(monkeypatch, tmp_path):
    p = tmp_path / "nudges.json"
    p.write_text(
        json.dumps(
            {
                "cycles_since_skill_create": 17,
                "thresholds": {"review_every": 3},
            }
        )
    )
    monkeypatch.setattr(cs, "NUDGES_PATH", p)
    state = cs._load_nudge_state()
    assert state["cycles_since_skill_create"] == 17
    assert state["thresholds"]["review_every"] == 3
    # Defaults preserved for unspecified threshold fields.
    assert state["thresholds"]["create_every"] == 25
