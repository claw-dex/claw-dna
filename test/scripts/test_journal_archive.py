"""Tests for scripts/journal_archive.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import journal_archive as ja


@pytest.fixture
def patch_paths(monkeypatch, agent_root):
    journal = agent_root / "memory" / "journal.json"
    archive = agent_root / "memory" / "journal-archive.json"
    monkeypatch.setattr(ja, "MEMORY_DIR", agent_root / "memory")
    monkeypatch.setattr(ja, "JOURNAL", journal)
    monkeypatch.setattr(ja, "ARCHIVE", archive)
    return journal, archive


def _make_entries(n, start=1):
    return [
        {
            "cycle": i,
            "summary": f"summary-{i}",
            "goal": f"goal-{i}",
            "outcome": f"outcome-{i}",
            "actions": [f"action-{i}"],
            "timestamp": f"2026-04-{i:02d}T00:00:00Z",
        }
        for i in range(start, start + n)
    ]


def test_load_json_missing_returns_default():
    assert ja._load_json(Path("/no/such/path"), default=[]) == []


def test_load_json_invalid_returns_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert ja._load_json(p, default={"x": 1}) == {"x": 1}


def test_write_json_atomic(tmp_path):
    target = tmp_path / "x.json"
    ja._write_json_atomic(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    # No .tmp left behind
    assert not (tmp_path / "x.json.tmp").exists()


def test_cmd_archive_empty_journal(patch_paths, capsys):
    journal, _ = patch_paths
    journal.write_text("[]")
    rv = ja.cmd_archive(keep=20)
    assert rv == 0
    out = capsys.readouterr().out
    assert "empty" in out.lower()


def test_cmd_archive_under_threshold(patch_paths, capsys):
    journal, _ = patch_paths
    journal.write_text(json.dumps(_make_entries(5)))
    rv = ja.cmd_archive(keep=20)
    assert rv == 0
    assert "nothing to archive" in capsys.readouterr().out.lower()


def test_cmd_archive_dry_run(patch_paths, capsys):
    journal, archive = patch_paths
    journal.write_text(json.dumps(_make_entries(30)))
    rv = ja.cmd_archive(keep=10, dry_run=True)
    assert rv == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    # File untouched
    assert len(json.loads(journal.read_text())) == 30
    assert not archive.exists()


def test_cmd_archive_writes_files(patch_paths, capsys):
    journal, archive = patch_paths
    journal.write_text(json.dumps(_make_entries(30)))
    rv = ja.cmd_archive(keep=10)
    assert rv == 20
    active = json.loads(journal.read_text())
    assert len(active) == 10
    archived = json.loads(archive.read_text())
    assert len(archived) == 20
    # Archive sorted ascending by cycle
    cycles = [e["cycle"] for e in archived]
    assert cycles == sorted(cycles)


def test_cmd_archive_appends_to_existing_archive(patch_paths):
    journal, archive = patch_paths
    archive.write_text(json.dumps(_make_entries(5, start=100)))
    journal.write_text(json.dumps(_make_entries(15, start=1)))
    ja.cmd_archive(keep=5)
    archived = json.loads(archive.read_text())
    assert len(archived) == 5 + 10  # existing + newly archived


def test_cmd_archive_journal_not_list(patch_paths):
    journal, _ = patch_paths
    journal.write_text(json.dumps({"not": "list"}))
    with pytest.raises(SystemExit):
        ja.cmd_archive()


def test_cmd_list_no_files(patch_paths, capsys):
    ja.cmd_list()
    out = capsys.readouterr().out
    assert "journal.json" in out


def test_cmd_list_with_data(patch_paths, capsys):
    journal, archive = patch_paths
    journal.write_text(json.dumps(_make_entries(3)))
    archive.write_text(json.dumps(_make_entries(2, start=50)))
    ja.cmd_list()
    out = capsys.readouterr().out
    assert "journal.json" in out
    assert "TOTAL" in out


def test_cmd_search_no_results(patch_paths, capsys):
    journal, _ = patch_paths
    journal.write_text(json.dumps(_make_entries(3)))
    ja.cmd_search("nonexistent-term-xyz")
    assert "No results" in capsys.readouterr().out


def test_cmd_search_finds_in_active_and_archive(patch_paths, capsys):
    journal, archive = patch_paths
    journal.write_text(json.dumps(_make_entries(2, start=10)))
    archive.write_text(json.dumps(_make_entries(2, start=1)))
    ja.cmd_search("summary")
    out = capsys.readouterr().out
    assert "[active]" in out
    assert "[archived]" in out


def test_cmd_json(patch_paths, capsys):
    journal, archive = patch_paths
    journal.write_text(json.dumps(_make_entries(25)))
    archive.write_text(json.dumps(_make_entries(10, start=100)))
    ja.cmd_json(keep=20)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["active_entries"] == 25
    assert data["archived_entries"] == 10
    assert data["would_archive"] == 5
    assert data["needs_archive"] is True


def test_main_dispatches_to_list(patch_paths, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["journal_archive.py", "--list"])
    ja.main()
    assert "journal.json" in capsys.readouterr().out


def test_main_dispatches_to_search(patch_paths, monkeypatch, capsys):
    journal, _ = patch_paths
    journal.write_text(json.dumps(_make_entries(2)))
    monkeypatch.setattr(sys, "argv", ["journal_archive.py", "--search", "goal-1"])
    ja.main()
    assert "result" in capsys.readouterr().out.lower()


def test_main_dispatches_to_json(patch_paths, monkeypatch, capsys):
    journal, _ = patch_paths
    journal.write_text(json.dumps(_make_entries(2)))
    monkeypatch.setattr(sys, "argv", ["journal_archive.py", "--json"])
    ja.main()
    data = json.loads(capsys.readouterr().out)
    assert "active_entries" in data
