"""Tests for scripts/cycle_start.py — focuses on pure helpers."""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone

import pytest

# Stub scripts.repair_memory_files before importing cycle_start
if "scripts" not in sys.modules:
    pkg = types.ModuleType("scripts")
    pkg.__path__ = []
    sys.modules["scripts"] = pkg
if "scripts.repair_memory_files" not in sys.modules:
    mr = types.ModuleType("scripts.repair_memory_files")
    mr.run_repair = lambda: {"ok": 0, "repaired": 0, "failed": 0, "issues": []}
    sys.modules["scripts.repair_memory_files"] = mr

import cycle_start as cs


@pytest.fixture
def patched(monkeypatch, agent_root):
    monkeypatch.setattr(cs, "MEMORY", agent_root / "memory")
    monkeypatch.setattr(cs, "MESSAGES", agent_root / "messages")
    monkeypatch.setattr(
        cs, "LONG_TERM_MEMORY_MV2_PATH", agent_root / "memory" / "long_term_memory.mv2"
    )
    monkeypatch.setattr(cs, "SCRIPTS", agent_root / "scripts")
    monkeypatch.setattr(cs, "DREAM_DIR", agent_root / "memory" / "dream")
    return agent_root


def test_now_iso():
    iso = cs.now_iso()
    # Should be parseable
    dt = datetime.fromisoformat(iso)
    assert dt.tzinfo is not None


def test_load_json_missing(tmp_path):
    assert cs.load_json(tmp_path / "missing.json") is None


def test_load_json_invalid(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert cs.load_json(p) is None


def test_load_json_valid(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text('{"a": 1}')
    assert cs.load_json(p) == {"a": 1}


def test_ago_recent():
    now = datetime.now(timezone.utc)
    ts = now.isoformat()
    assert "ago" in cs.ago(ts)


def test_ago_invalid():
    assert cs.ago("garbage") == "garbage"


def test_fmt_dur_seconds():
    assert cs.fmt_dur(45) == "45s"


def test_fmt_dur_minutes():
    assert cs.fmt_dur(125) == "2m 5s"


def test_fmt_dur_hours():
    assert cs.fmt_dur(3700).startswith("1h")


def test_write_safe_success(tmp_path):
    p = tmp_path / "out.json"
    assert cs._write_safe(p, {"a": 1}) is True
    assert json.loads(p.read_text()) == {"a": 1}


def test_check_orphaned_cycles_marks_stale(patched):
    old_start = "2020-01-01T00:00:00+00:00"
    cycles = [
        {"cycle_number": 1, "cycle_status": "in_progress", "start": old_start},
        {"cycle_number": 2, "cycle_status": "completed"},
    ]
    out, n = cs.check_orphaned_cycles(cycles, max_age_minutes=30)
    assert n == 1
    assert out[0]["cycle_status"] == "interrupted"
    assert "interrupted_at" in out[0]


def test_check_orphaned_cycles_keeps_recent(patched):
    recent = datetime.now(timezone.utc).isoformat()
    cycles = [{"cycle_number": 1, "cycle_status": "in_progress", "start": recent}]
    out, n = cs.check_orphaned_cycles(cycles, max_age_minutes=30)
    assert n == 0
    assert out[0]["cycle_status"] == "in_progress"


def test_error_age_hours_no_timestamp():
    assert cs._error_age_hours({}) == 9999.0


def test_error_age_hours_invalid():
    assert cs._error_age_hours({"timestamp": "garbage"}) == 9999.0


def test_error_age_hours_recent():
    ts = datetime.now(timezone.utc).isoformat()
    age = cs._error_age_hours({"timestamp": ts})
    assert age < 1.0


def test_auto_archive_old_errors_empty(patched):
    out, removed = cs.auto_archive_old_errors([])
    assert out == []
    assert removed == 0


def test_auto_archive_old_errors_filters(patched):
    # Need to ensure the memory directory exists
    (patched / "memory").mkdir(exist_ok=True)
    old = "2020-01-01T00:00:00+00:00"
    recent = datetime.now(timezone.utc).isoformat()
    errors = [{"timestamp": old}, {"timestamp": recent}]
    kept, removed = cs.auto_archive_old_errors(errors, max_age_hours=48)
    assert removed == 1
    assert len(kept) == 1


def test_summarize_cycles_empty():
    info = cs.summarize_cycles([])
    assert info["total"] == 0
    assert info["avg_dur"] == 0


def test_summarize_cycles_with_data():
    cycles = [
        {
            "cycle_type": "evolve",
            "cycle_status": "completed",
            "duration_seconds": 60,
            "cycle_category": "efficiency",
        },
        {"cycle_type": "goal", "cycle_status": "completed", "duration_seconds": 120},
        {
            "cycle_type": "evolve",
            "cycle_status": "completed",
            "duration_seconds": 90,
            "cycle_category": "reliability",
        },
    ]
    info = cs.summarize_cycles(cycles)
    assert info["total"] == 3
    assert info["by_type"]["evolve"] == 2
    assert info["by_cat"]["efficiency"] == 1
    assert info["avg_dur"] == 90.0


def test_compute_base_need_zero_evolve():
    # When no evolve cycles, need is max
    assert cs._compute_base_need("efficiency", {}, 0) == 30


def test_compute_base_need_proportional():
    by_cat = {"efficiency": 5, "reliability": 5}
    assert cs._compute_base_need("efficiency", by_cat, 10) >= 0


def test_compute_recency_boost_no_cycles():
    assert cs._compute_recency_boost("efficiency", []) == 25


def test_compute_recency_boost_recent_pick():
    cycles = [{"cycle_type": "evolve", "cycle_category": "efficiency"}]
    boost = cs._compute_recency_boost("efficiency", cycles)
    assert 0 <= boost <= 25


def test_compute_goal_alignment_no_unfinished():
    assert cs._compute_goal_alignment("efficiency", [{"status": "completed"}]) == 0


def test_compute_goal_alignment_match():
    goals = [{"status": "pending", "content": "fix the bug"}]
    score = cs._compute_goal_alignment("reliability", goals)
    assert score > 0


def test_compute_roi_bonus_no_history():
    assert cs._compute_roi_bonus("efficiency", []) == 5


def test_compute_roi_bonus_all_pass():
    cycles = [
        {
            "cycle_type": "evolve",
            "cycle_category": "efficiency",
            "cycle_status": "completed",
        },
        {
            "cycle_type": "evolve",
            "cycle_category": "efficiency",
            "cycle_status": "completed",
        },
    ]
    assert cs._compute_roi_bonus("efficiency", cycles) == 10


def test_compute_maturity_penalty_observability_high_tabs():
    caps = {"portal_tabs": 25}
    p, reason = cs._compute_maturity_penalty("observability", caps)
    assert p > 0


def test_compute_maturity_penalty_clean():
    p, reason = cs._compute_maturity_penalty("efficiency", {})
    assert p == 0


def test_evolve_recommendation_empty():
    text, suggested, _ = cs.evolve_recommendation({})
    assert suggested in cs.ALL_CATS


def test_evolve_recommendation_underserved(patched):
    # capability has 0, others have lots
    by_cat = {
        "capability": 0,
        "observability": 10,
        "reliability": 10,
        "efficiency": 10,
        "prompt_evolution": 10,
    }
    text, suggested, _ = cs.evolve_recommendation(by_cat)
    assert suggested == "capability"


def test_goal_signals_aligned():
    goals = [{"status": "pending", "content": "fix the broken thing"}]
    out = cs._goal_signals(goals)
    assert len(out) == 1
    assert "reliability" in out[0]["aligned_categories"]


def test_build_recall_queries_dedup():
    inbox = [{"content": "hello"}, {"content": "hello"}, {"content": "world"}]
    goals = [{"status": "in_progress", "content": "world"}]  # dup of inbox item
    out = cs._build_recall_queries(inbox, goals)
    assert len(out) == 2  # deduped


def test_build_recall_queries_caps_to_goal_in_progress():
    inbox = []
    goals = [
        {"status": "completed", "content": "old"},
        {"status": "in_progress", "content": "current"},
    ]
    out = cs._build_recall_queries(inbox, goals)
    assert "current" in out


def test_list_recent_dream_files_no_dir(patched):
    # DREAM_DIR doesn't exist
    assert cs._list_recent_dream_files() == []


def test_list_recent_dream_files_with_recent(patched):
    learnings_dir = patched / "memory" / "dream" / "learnings"
    learnings_dir.mkdir(parents=True)
    f = learnings_dir / "x.md"
    f.write_text("note")
    out = cs._list_recent_dream_files(hours=24)
    assert len(out) == 1
    assert out[0]["kind"] == "learning"


def test_auto_archive_journal_inlined(patched):
    import datetime as _dt

    (patched / "memory").mkdir(exist_ok=True)
    # Build a journal where entries 1..10 are >24h old AND >5 cycles before the
    # newest cycle (15), so they qualify for archival under the new rule.
    old_ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=48)).isoformat()
    fresh_ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
    journal = [
        {"cycle_number": i, "summary": f"c{i}", "timestamp": old_ts}
        for i in range(1, 11)
    ] + [
        {"cycle_number": i, "summary": f"c{i}", "timestamp": fresh_ts}
        for i in range(11, 16)
    ]
    kept, n_archived, total = cs._auto_archive_journal_inlined(
        journal, min_keep=5, min_cycle_age=0
    )
    assert len(kept) == 5
    assert n_archived == 10
