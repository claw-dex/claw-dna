"""Tests for scripts/memory_stats.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import memory_stats as ms


@pytest.fixture
def patch_paths(monkeypatch, agent_root):
    mem = agent_root / "memory"
    monkeypatch.setattr(ms, "MEMORY", mem)
    return mem


def test_load_json_missing(tmp_path):
    assert ms.load_json(tmp_path / "nope.json") is None


def test_load_json_ok(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"a":1}')
    assert ms.load_json(p) == {"a": 1}


def test_fmt_duration():
    assert ms.fmt_duration(30) == "30s"
    assert ms.fmt_duration(125) == "2m 5s"
    assert "h" in ms.fmt_duration(3700)


def test_ago_formats():
    # Bad input passes through
    assert ms.ago("garbage") == "garbage"


def test_ago_recent():
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    ts = (now - _dt.timedelta(seconds=10)).isoformat()
    s = ms.ago(ts)
    assert "ago" in s


def test_summarize_state():
    s = ms.summarize_state(
        {
            "cycle_number": 3,
            "agent_status": "running",
            "last_heartbeat": "2026-01-01T00:00:00Z",
            "last_cycle_summary": "x" * 200,
        }
    )
    assert s["cycle_number"] == 3
    assert s["agent_status"] == "running"
    assert len(s["last_cycle_summary"]) == 120


def test_summarize_goals_empty():
    s = ms.summarize_goals([])
    assert s["total"] == 0
    assert s["pending"] == 0


def test_summarize_goals_counts():
    g = ms.summarize_goals(
        [
            {"status": "pending"},
            {"status": "in_progress"},
            {"status": "in_progress"},
            {"status": "completed"},
            {"status": "failed"},
        ]
    )
    assert g["total"] == 5
    assert g["in_progress"] == 2
    assert g["latest"]["status"] == "failed"


def test_summarize_cycles_empty():
    assert ms.summarize_cycles([]) == {}


def test_summarize_cycles_full():
    s = ms.summarize_cycles(
        [
            {
                "cycle_type": "evolve",
                "cycle_status": "completed",
                "duration_seconds": 60,
                "cycle_category": "capability",
            },
            {
                "cycle_type": "evolve",
                "cycle_status": "completed",
                "duration_seconds": 30,
                "cycle_category": "reliability",
            },
            {"cycle_type": "explore", "cycle_status": "failed"},
        ]
    )
    assert s["total"] == 3
    assert s["by_type"]["evolve"] == 2
    assert s["avg_duration_seconds"] == 45.0
    assert "capability" in s["evolve_by_category"]


def test_summarize_failures():
    s = ms.summarize_failures({"failures": [{"summary": "a"}, {"summary": "b"}]})
    assert s["total"] == 2
    assert s["recent"] == ["a", "b"]


def test_summarize_failures_non_dict():
    s = ms.summarize_failures([])
    assert s["total"] == 0


def test_summarize_notes():
    s = ms.summarize_notes(
        [
            {
                "id": 1,
                "title": "t",
                "tags": ["a", "b"],
                "pinned": True,
                "updated_at": "2026-01-01",
            },
            {"id": 2, "title": "u", "tags": ["a"], "updated_at": "2026-02-01"},
        ]
    )
    assert s["total"] == 2
    assert s["pinned"] == 1
    assert s["unique_tags"] == 2
    assert s["latest"]["id"] == 2


def test_summarize_notes_non_list():
    s = ms.summarize_notes("not a list")
    assert s == {"total": 0, "pinned": 0, "tags": {}}


def test_summarize_server_errors():
    s = ms.summarize_server_errors([{"error": "boom"}])
    assert s["total"] == 1
    assert "boom" in s["recent"][0]


def test_summarize_command_history():
    s = ms.summarize_command_history(
        [
            {"type": "shell", "result": "ok", "command": "ls"},
            {"type": "shell", "result": "fail", "content": "rm -rf"},
        ]
    )
    assert s["total"] == 2
    assert s["by_type"]["shell"] == 2


def test_summarize_inbox_missing(patch_paths):
    s = ms.summarize_inbox(patch_paths / "no.json")
    assert s == {"status": "missing"}


def test_summarize_inbox_list(tmp_path):
    p = tmp_path / "inbox.json"
    p.write_text(json.dumps([{"type": "user"}, {"type": "system"}]))
    s = ms.summarize_inbox(p)
    assert s["messages"] == 2


def test_check_portal_health_mocked(mocker):
    fake = mocker.MagicMock(stdout="ok")
    mocker.patch("subprocess.run", return_value=fake)
    assert ms.check_portal_health() == "ok"


def test_check_portal_health_unhealthy(mocker):
    fake = mocker.MagicMock(stdout="something else")
    mocker.patch("subprocess.run", return_value=fake)
    assert "UNHEALTHY" in ms.check_portal_health()


def test_check_portal_health_error(mocker):
    mocker.patch("subprocess.run", side_effect=OSError("nope"))
    assert "ERROR" in ms.check_portal_health()


def test_main_json_mode(patch_paths, monkeypatch, mocker, capsys):
    (patch_paths / "state.json").write_text(json.dumps({"cycle_number": 1}))
    mocker.patch.object(ms, "check_portal_health", return_value="ok")
    monkeypatch.setattr(sys, "argv", ["memory_stats.py", "--json"])
    ms.main()
    data = json.loads(capsys.readouterr().out)
    assert "state" in data
    assert "goals" in data


def test_main_short_mode(patch_paths, monkeypatch, mocker, capsys):
    (patch_paths / "state.json").write_text(
        json.dumps({"cycle_number": 1, "status": "ok"})
    )
    mocker.patch.object(ms, "check_portal_health", return_value="ok")
    monkeypatch.setattr(sys, "argv", ["memory_stats.py", "--short"])
    ms.main()
    out = capsys.readouterr().out
    assert "Cycle" in out


def test_main_full_report(patch_paths, monkeypatch, mocker, capsys):
    (patch_paths / "state.json").write_text(json.dumps({"cycle_number": 1}))
    (patch_paths / "notes.json").write_text("[]")
    (patch_paths / "goal.json").write_text("[]")
    (patch_paths / "cycles.json").write_text("[]")
    (patch_paths / "journal.json").write_text("[]")
    mocker.patch.object(ms, "check_portal_health", return_value="ok")
    monkeypatch.setattr(sys, "argv", ["memory_stats.py"])
    ms.main()
    out = capsys.readouterr().out
    assert "AGENT MEMORY STATS" in out
