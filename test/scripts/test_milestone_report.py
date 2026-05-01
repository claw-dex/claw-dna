"""Tests for scripts/milestone_report.py."""

from __future__ import annotations

import json

import pytest

import milestone_report as mr


@pytest.fixture
def patched(monkeypatch, agent_root):
    monkeypatch.setattr(mr, "AGENT_DIR", agent_root)
    monkeypatch.setattr(mr, "MEMORY_DIR", agent_root / "memory")
    monkeypatch.setattr(mr, "WORKSPACE_DIR", agent_root / "workspace")
    monkeypatch.setattr(mr, "SCRIPTS_DIR", agent_root / "scripts")
    (agent_root / "scripts").mkdir(parents=True, exist_ok=True)
    return agent_root


def test_load_json_missing(tmp_path):
    assert mr._load_json(tmp_path / "missing.json", default=[]) == []


def test_load_json_valid(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"x": 1}))
    assert mr._load_json(p, default={}) == {"x": 1}


def test_milestone_cycles():
    assert mr._milestone_cycles(10) == []
    assert mr._milestone_cycles(25) == [25]
    assert mr._milestone_cycles(100) == [25, 50, 75, 100]


def test_cycles_up_to():
    cycles = [
        {"cycle_number": 1},
        {"cycle_number": 5},
        {"cycle_number": 10},
        {"cycle_number": "bad"},
    ]
    out = mr._cycles_up_to(cycles, 5)
    assert len(out) == 2
    assert all(c.get("cycle_number", 0) <= 5 for c in out)


def test_format_duration():
    assert mr._format_duration(None) == "—"
    assert mr._format_duration(45) == "45s"
    assert mr._format_duration(125).startswith("2m")
    assert mr._format_duration(3700).startswith("1h")


def test_summarize_notes_empty():
    s = mr._summarize_notes([])
    assert s["total"] == 0
    assert s["pinned"] == 0
    assert s["latest"] is None


def test_summarize_notes_basic():
    notes = [
        {
            "id": "n1",
            "title": "first",
            "tags": ["a", "b"],
            "pinned": True,
            "updated_at": "2026-01-01",
        },
        {"id": "n2", "title": "second", "tags": ["a"], "updated_at": "2026-02-01"},
    ]
    s = mr._summarize_notes(notes)
    assert s["total"] == 2
    assert s["pinned"] == 1
    assert s["unique_tags"] == 2
    assert s["latest"]["id"] == "n2"


def test_summarize_notes_invalid_input():
    s = mr._summarize_notes("not-a-list")
    assert s["total"] == 0


def test_build_report_basic():
    cycles = [
        {
            "cycle_number": 1,
            "cycle_status": "completed",
            "cycle_type": "evolve",
            "cycle_category": "efficiency",
            "duration_seconds": 60,
            "start": "2026-04-01T00:00:00",
            "end": "2026-04-01T01:00:00",
        },
        {
            "cycle_number": 2,
            "cycle_status": "completed",
            "cycle_type": "goal",
            "duration_seconds": 120,
            "start": "2026-04-01T01:00:00",
            "end": "2026-04-01T03:00:00",
        },
        {
            "cycle_number": 3,
            "cycle_status": "in_progress",
            "cycle_type": "evolve",
        },
    ]
    goals = [{"status": "completed"}, {"status": "failed"}, {"status": "pending"}]
    caps = {"utility_scripts": ["a.py", "b.py"]}
    journal = [{"cycle_number": 2, "summary": "did stuff"}]

    rep = mr.build_report(
        2, cycles, goals, caps, journal, notes=[], command_history=[], server_errors=[]
    )
    assert rep["milestone_cycle"] == 2
    assert rep["total_completed_cycles"] == 2
    assert rep["type_breakdown"]["evolve"] == 1
    assert rep["type_breakdown"]["goal"] == 1
    assert rep["category_breakdown"]["efficiency"] == 1
    assert rep["goal_total"] == 3
    assert rep["goal_completed"] == 1
    assert rep["goal_failed"] == 1
    assert rep["scripts_count"] == 2


def test_build_report_no_completed():
    rep = mr.build_report(5, [], [], {}, [], notes=[])
    assert rep["total_completed_cycles"] == 0
    assert rep["avg_duration_seconds"] is None
    assert rep["goal_completion_rate"] is None


def test_render_markdown_basic():
    cycles = [
        {
            "cycle": 1,
            "status": "completed",
            "type": "evolve",
            "category": "efficiency",
            "duration_seconds": 60,
        },
    ]
    rep = mr.build_report(1, cycles, [], {}, [], notes=[])
    md = mr.render_markdown(rep)
    assert "Milestone Report" in md
    assert "Cycle 1" in md


def test_main_list_mode(monkeypatch, capsys, patched):
    (patched / "memory" / "state.json").write_text(json.dumps({"cycle_number": 50}))
    (patched / "memory" / "cycles.json").write_text(
        json.dumps([{"cycle": i, "status": "completed"} for i in range(1, 51)])
    )
    (patched / "memory" / "goal.json").write_text(json.dumps([]))
    (patched / "memory" / "journal.json").write_text(json.dumps([]))
    monkeypatch.setattr("sys.argv", ["milestone_report.py", "--list"])
    rc = mr.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cycle" in out


def test_main_json_mode(monkeypatch, capsys, patched):
    (patched / "memory" / "state.json").write_text(json.dumps({"cycle_number": 25}))
    (patched / "memory" / "cycles.json").write_text(
        json.dumps([{"cycle": i, "status": "completed"} for i in range(1, 26)])
    )
    (patched / "memory" / "goal.json").write_text(json.dumps([]))
    (patched / "memory" / "journal.json").write_text(json.dumps([]))
    monkeypatch.setattr("sys.argv", ["milestone_report.py", "--cycle", "25", "--json"])
    rc = mr.main()
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["milestone_cycle"] == 25


def test_main_save_writes_file(monkeypatch, capsys, patched):
    (patched / "memory" / "state.json").write_text(json.dumps({"cycle_number": 25}))
    (patched / "memory" / "cycles.json").write_text(json.dumps([]))
    (patched / "memory" / "goal.json").write_text(json.dumps([]))
    (patched / "memory" / "journal.json").write_text(json.dumps([]))
    monkeypatch.setattr("sys.argv", ["milestone_report.py", "--cycle", "25", "--save"])
    mr.main()
    out_path = patched / "workspace" / "milestone_25.md"
    assert out_path.exists()
