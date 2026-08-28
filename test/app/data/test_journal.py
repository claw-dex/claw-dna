"""Tests for app.data.journal loaders."""

from __future__ import annotations

import json

import pytest

from app.data import journal


@pytest.fixture(autouse=True)
def _clean_cache():
    journal._JOURNAL_CACHE.clear()
    yield
    journal._JOURNAL_CACHE.clear()


@pytest.fixture
def mem_dir(tmp_path, monkeypatch):
    d = tmp_path / "memory"
    d.mkdir()
    monkeypatch.setattr("app.data.journal.MEMORY_DIR", str(d))
    return d


def _write(path, obj):
    path.write_text(json.dumps(obj))


def test_parse_no_files(mem_dir):
    assert journal._parse_journal_entries() == []


def test_parse_active_only(mem_dir):
    _write(
        mem_dir / "journal.json",
        [
            {"cycle_number": 1, "cycle_goal": "g1"},
            {"cycle_number": 2, "cycle_goal": "g2"},
        ],
    )
    out = journal._parse_journal_entries()
    assert [e["cycle_number"] for e in out] == [2, 1]


def test_parse_merges_archive(mem_dir):
    _write(mem_dir / "journal.json", [{"cycle_number": 5, "cycle_goal": "new"}])
    _write(
        mem_dir / "journal_archive.json",
        [
            {"cycle_number": 1, "cycle_goal": "old"},
            {"cycle_number": 2, "cycle_goal": "older"},
        ],
    )
    out = journal._parse_journal_entries()
    assert [e["cycle_number"] for e in out] == [5, 2, 1]


def test_parse_active_takes_precedence(mem_dir):
    _write(mem_dir / "journal.json", [{"cycle_number": 1, "cycle_goal": "ACTIVE"}])
    _write(
        mem_dir / "journal_archive.json",
        [{"cycle_number": 1, "cycle_goal": "ARCHIVED"}],
    )
    out = journal._parse_journal_entries()
    assert len(out) == 1
    assert out[0]["cycle_goal"] == "ACTIVE"


def test_parse_handles_non_list_active(mem_dir):
    _write(mem_dir / "journal.json", {"oops": "not a list"})
    _write(mem_dir / "journal_archive.json", [{"cycle_number": 1, "cycle_goal": "g"}])
    out = journal._parse_journal_entries()
    assert [e["cycle_number"] for e in out] == [1]


def test_parse_handles_non_list_archive(mem_dir):
    _write(mem_dir / "journal.json", [{"cycle_number": 1, "cycle_goal": "g"}])
    _write(mem_dir / "journal_archive.json", {"oops": True})
    out = journal._parse_journal_entries()
    assert [e["cycle_number"] for e in out] == [1]


def test_parse_corrupt_json(mem_dir):
    (mem_dir / "journal.json").write_text("{not json")
    assert journal._parse_journal_entries() == []


def test_load_journal_pagination(mem_dir):
    entries = [{"cycle_number": i, "cycle_goal": f"g{i}"} for i in range(1, 6)]
    _write(mem_dir / "journal.json", entries)
    out = journal.load_journal(limit=2, offset=0)
    assert out["total"] == 5
    assert out["offset"] == 0
    assert out["has_more"] is True
    assert [e["cycle_number"] for e in out["entries"]] == [5, 4]


def test_load_journal_offset(mem_dir):
    entries = [{"cycle_number": i} for i in range(1, 6)]
    _write(mem_dir / "journal.json", entries)
    out = journal.load_journal(limit=2, offset=4)
    assert out["entries"] == [{"cycle_number": 1}]
    assert out["has_more"] is False


def test_load_journal_zero_limit_returns_all_from_offset(mem_dir):
    entries = [{"cycle_number": i} for i in range(1, 4)]
    _write(mem_dir / "journal.json", entries)
    out = journal.load_journal(limit=0, offset=1)
    assert [e["cycle_number"] for e in out["entries"]] == [2, 1]
    assert out["total"] == 3
    assert out["has_more"] is False


def test_parse_caches_until_mtime_changes(mem_dir):
    p = mem_dir / "journal.json"
    _write(p, [{"cycle_number": 1}])
    first = journal._parse_journal_entries()
    # change file content but reset mtime to match cached → still stale
    import os

    mtime = os.path.getmtime(p)
    _write(p, [{"cycle_number": 99}])
    os.utime(p, (mtime, mtime))
    assert journal._parse_journal_entries() == first
    # bump mtime → invalidate
    os.utime(p, (mtime + 100, mtime + 100))
    out = journal._parse_journal_entries()
    assert out[0]["cycle_number"] == 99
