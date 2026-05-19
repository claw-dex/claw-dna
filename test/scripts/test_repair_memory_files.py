"""Tests for scripts/repair_memory_files.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import repair_memory_files as mr


@pytest.fixture
def patch_paths(monkeypatch, agent_root):
    mem = agent_root / "memory"
    monkeypatch.setattr(mr, "MEMORY_DIR", mem)
    monkeypatch.setattr(mr, "AGENT_DIR", agent_root)
    return mem


def test_is_valid_json_ok(tmp_path):
    p = tmp_path / "f.json"
    p.write_text('{"a": 1}')
    valid, data = mr._is_valid_json(p)
    assert valid is True
    assert data == {"a": 1}


def test_is_valid_json_bad(tmp_path):
    p = tmp_path / "f.json"
    p.write_text("{not json")
    valid, err = mr._is_valid_json(p)
    assert valid is False
    assert isinstance(err, str)


def test_write_safe(tmp_path):
    p = tmp_path / "out.json"
    assert mr._write_safe(p, [1, 2, 3]) is True
    assert json.loads(p.read_text()) == [1, 2, 3]


def test_backup_creates_copy(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("[1]")
    assert mr._backup(p) is True
    assert (tmp_path / "x.json.backup").exists()


def test_restore_from_backup_missing(tmp_path):
    ok, _ = mr._restore_from_backup(tmp_path / "no.json")
    assert ok is False


def test_restore_from_backup_present(tmp_path):
    p = tmp_path / "x.json"
    bak = tmp_path / "x.json.backup"
    bak.write_text("[42]")
    ok, data = mr._restore_from_backup(p)
    assert ok is True
    assert data == [42]


def test_salvage_json_array():
    text = '[{"a":1},{"b":2},{"c":3'  # truncated
    salvaged = mr._salvage_json_array(text)
    assert salvaged == [{"a": 1}, {"b": 2}]


def test_salvage_json_array_not_an_array():
    assert mr._salvage_json_array('{"a":1}') is None


def test_run_repair_all_missing_creates_defaults(patch_paths):
    summary = mr.run_repair()
    assert summary["failed"] == 0
    # all defaults files exist
    for name in mr.DEFAULTS:
        assert (patch_paths / name).exists()


def test_run_repair_dry_run_does_not_write(patch_paths):
    summary = mr.run_repair(dry_run=True)
    # Files don't get created
    for name in mr.DEFAULTS:
        assert not (patch_paths / name).exists()
    # All marked as failed (would-create)
    assert summary["failed"] >= len(mr.DEFAULTS)


def test_run_repair_invalid_json_repaired_from_backup(patch_paths):
    p = patch_paths / "state.json"
    p.write_text("{not json")
    bak = patch_paths / "state.json.backup"
    bak.write_text(json.dumps({"cycle_number": 5, "status": "ok"}))
    summary = mr.run_repair()
    state = json.loads(p.read_text())
    assert state["cycle_number"] == 5
    assert summary["repaired"] >= 1


def test_run_repair_invalid_json_falls_back_to_default(patch_paths):
    p = patch_paths / "cycles.json"
    p.write_text("{garbage")
    summary = mr.run_repair()
    # Default for cycles.json is []
    assert json.loads(p.read_text()) == []
    assert summary["repaired"] >= 1
    # corrupt sibling created
    assert (patch_paths / "cycles.json.corrupt").exists()


def test_run_repair_journal_truncated_salvages(patch_paths, monkeypatch):
    # Remove journal.json from DEFAULTS so _process_file doesn't reset it
    # to []; that lets _check_journal's salvage path run on the truncated text.
    defaults = {k: v for k, v in mr.DEFAULTS.items() if k != "journal.json"}
    monkeypatch.setattr(mr, "DEFAULTS", defaults)
    for name, default in defaults.items():
        (patch_paths / name).write_text(json.dumps(default))
    journal = patch_paths / "journal.json"
    journal.write_text('[{"cycle":1,"summary":"a"},{"cycle":2,"summary":"b"')
    mr.run_repair()
    data = json.loads(journal.read_text())
    assert isinstance(data, list)
    assert len(data) >= 1


def test_run_repair_journal_empty_resets(patch_paths):
    for name, default in mr.DEFAULTS.items():
        if name != "journal.json":
            (patch_paths / name).write_text(json.dumps(default))
    journal = patch_paths / "journal.json"
    journal.write_text("   ")
    mr.run_repair()
    assert json.loads(journal.read_text()) == []


def test_normalize_statuses_migrates(patch_paths):
    for name, default in mr.DEFAULTS.items():
        (patch_paths / name).write_text(json.dumps(default))
    goal = patch_paths / "goal.json"
    goal.write_text(json.dumps([{"id": 1, "status": "in-progress"}]))
    mr.run_repair()
    data = json.loads(goal.read_text())
    assert data[0]["status"] == "in_progress"


def test_run_repair_backup_only(patch_paths):
    p = patch_paths / "state.json"
    p.write_text(json.dumps({"cycle_number": 1}))
    mr.run_repair(backup_only=True)
    assert (patch_paths / "state.json.backup").exists()


def test_main_quiet_exits_zero_on_healthy(patch_paths, monkeypatch, capsys):
    for name, default in mr.DEFAULTS.items():
        (patch_paths / name).write_text(json.dumps(default))
    monkeypatch.setattr(sys, "argv", ["repair_memory_files.py", "--quiet"])
    with pytest.raises(SystemExit) as exc:
        mr.main()
    assert exc.value.code == 0


def test_main_json_output(patch_paths, monkeypatch, capsys):
    for name, default in mr.DEFAULTS.items():
        (patch_paths / name).write_text(json.dumps(default))
    monkeypatch.setattr(sys, "argv", ["repair_memory_files.py", "--json"])
    with pytest.raises(SystemExit):
        mr.main()
    data = json.loads(capsys.readouterr().out)
    assert "ok" in data and "results" in data
