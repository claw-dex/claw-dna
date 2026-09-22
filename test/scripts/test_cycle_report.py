"""Tests for scripts/cycle_report.py — cycle activity report generator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cycle_report


@pytest.fixture
def patched(monkeypatch, agent_root):
    monkeypatch.setattr(cycle_report, "MEMORY_DIR", agent_root / "memory")
    return agent_root / "memory"


def test_load_json_missing(tmp_path):
    assert cycle_report.load_json(tmp_path / "nope.json") is None


def test_load_json_invalid(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert cycle_report.load_json(p) is None


def test_load_json_valid(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text('{"a": 1}')
    assert cycle_report.load_json(p) == {"a": 1}


def test_parse_iso_valid():
    dt = cycle_report.parse_iso("2026-04-30T12:00:00+00:00")
    assert dt is not None
    assert dt.year == 2026


def test_parse_iso_invalid():
    assert cycle_report.parse_iso("garbage") is None
    assert cycle_report.parse_iso("") is None


def test_format_duration_none():
    assert cycle_report.format_duration(None) == "?"


def test_format_duration_seconds():
    assert cycle_report.format_duration(45) == "45s"


def test_format_duration_minutes():
    assert cycle_report.format_duration(125) == "2m5s"


def test_format_duration_hours():
    assert cycle_report.format_duration(3661) == "1h1m"


def test_main_json_output(monkeypatch, capsys, patched):
    (patched / "state.json").write_text(
        json.dumps({"cycle_number": 3, "status": "idle"})
    )
    (patched / "cycles.json").write_text(
        json.dumps(
            [
                {
                    "cycle": 1,
                    "status": "completed",
                    "duration_seconds": 60,
                    "summary": "first",
                    "start": "2026-04-30T10:00:00+00:00",
                },
                {"cycle": 2, "status": "completed", "duration_seconds": 120},
            ]
        )
    )
    (patched / "goal.json").write_text(json.dumps([{"id": 1}]))
    (patched / "journal.json").write_text(json.dumps([{"status": "failed"}]))

    monkeypatch.setattr("sys.argv", ["cycle_report.py", "--format", "json"])
    cycle_report.main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["current_cycle"] == 3
    assert data["total_cycles"] == 2
    assert data["completed_cycles"] == 2
    assert data["failure_count"] == 1
    assert data["total_goals"] == 1


def test_main_md_output(monkeypatch, capsys, patched):
    (patched / "cycles.json").write_text(json.dumps([]))
    monkeypatch.setattr("sys.argv", ["cycle_report.py"])
    cycle_report.main()
    out = capsys.readouterr().out
    assert "Agent Activity Report" in out
    assert "Cycle History" in out


def test_main_unknown_arg_exits(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cycle_report.py", "--bogus"])
    with pytest.raises(SystemExit) as exc:
        cycle_report.main()
    assert exc.value.code == 1


def test_main_help(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["cycle_report.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        cycle_report.main()
    assert exc.value.code == 0


def test_main_last_n(monkeypatch, capsys, patched):
    (patched / "cycles.json").write_text(
        json.dumps(
            [
                {"cycle": i, "status": "completed", "duration_seconds": 10}
                for i in range(1, 11)
            ]
        )
    )
    monkeypatch.setattr(
        "sys.argv", ["cycle_report.py", "--last", "3", "--format", "json"]
    )
    cycle_report.main()
    data = json.loads(capsys.readouterr().out)
    assert data["total_cycles"] == 3
