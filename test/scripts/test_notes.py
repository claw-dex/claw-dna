"""Tests for scripts/notes.py."""

from __future__ import annotations

import json
import sys

import pytest

import notes


@pytest.fixture
def redirect_notes(monkeypatch, tmp_path):
    path = tmp_path / "notes.json"
    monkeypatch.setattr(notes, "NOTES_PATH", path)
    monkeypatch.setattr(notes, "_LOCK_PATH", str(path) + ".lock")
    return path


def test_load_notes_missing(redirect_notes):
    assert notes.load_notes() == []


def test_load_notes_corrupt(redirect_notes):
    redirect_notes.write_text("not json")
    assert notes.load_notes() == []


def test_save_and_load_notes(redirect_notes):
    notes.save_notes([{"id": "x", "title": "t"}])
    assert notes.load_notes() == [{"id": "x", "title": "t"}]


def test_get_flag_present_and_missing():
    assert notes._get_flag(["--title", "hi"], "--title") == "hi"
    assert notes._get_flag(["--other"], "--title") is None
    assert notes._get_flag(["--title"], "--title") is None


def test_gen_id_returns_8_hex():
    a = notes._gen_id("abc")
    b = notes._gen_id("abc")
    assert len(a) == 8
    # Different invocations give different ids (timestamp dependent)
    # they may collide rarely; we just check format
    int(a, 16)
    int(b, 16)


# ─── add ────────────────────────────────────────────────────────────────────


def test_cmd_add_requires_title_and_content(redirect_notes, capsys):
    rc = notes.cmd_add([])
    assert rc == 1
    assert "required" in capsys.readouterr().err


def test_cmd_add_creates_note(redirect_notes, capsys):
    rc = notes.cmd_add(["--title", "T", "--content", "C", "--tags", "a, b", "--pin"])
    assert rc == 0
    data = notes.load_notes()
    assert len(data) == 1
    assert data[0]["title"] == "T"
    assert data[0]["content"] == "C"
    assert data[0]["tags"] == ["a", "b"]
    assert data[0]["pinned"] is True


# ─── list ───────────────────────────────────────────────────────────────────


def test_cmd_list_empty(redirect_notes, capsys):
    rc = notes.cmd_list(["--json"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "[]"


def test_cmd_list_filters_by_tag(redirect_notes, capsys):
    notes.add_note("a", "x", tags=["t1"])
    notes.add_note("b", "y", tags=["t2"])
    rc = notes.cmd_list(["--tag", "t1", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["title"] == "a"


# ─── get ────────────────────────────────────────────────────────────────────


def test_cmd_get_missing_id(redirect_notes, capsys):
    rc = notes.cmd_get([])
    assert rc == 1


def test_cmd_get_not_found(redirect_notes, capsys):
    rc = notes.cmd_get(["--id", "deadbeef"])
    assert rc == 1


def test_cmd_get_found_json(redirect_notes, capsys):
    nid = notes.add_note("T", "C")
    rc = notes.cmd_get(["--id", nid, "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == nid


# ─── search ─────────────────────────────────────────────────────────────────


def test_cmd_search_requires_query(redirect_notes, capsys):
    rc = notes.cmd_search([])
    assert rc == 1


def test_cmd_search_finds(redirect_notes, capsys):
    notes.add_note("Roadmap Q2", "discussion", tags=[])
    rc = notes.cmd_search(["--query", "roadmap", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1


# ─── edit ───────────────────────────────────────────────────────────────────


def test_cmd_edit_missing_id(redirect_notes, capsys):
    rc = notes.cmd_edit([])
    assert rc == 1


def test_cmd_edit_no_changes(redirect_notes, capsys):
    nid = notes.add_note("T", "C")
    rc = notes.cmd_edit(["--id", nid])
    assert rc == 1


def test_cmd_edit_updates(redirect_notes, capsys):
    nid = notes.add_note("T", "C")
    rc = notes.cmd_edit(["--id", nid, "--title", "T2", "--pin"])
    assert rc == 0
    note = next(n for n in notes.load_notes() if n["id"] == nid)
    assert note["title"] == "T2"
    assert note["pinned"] is True


def test_cmd_edit_not_found(redirect_notes, capsys):
    rc = notes.cmd_edit(["--id", "deadbeef", "--title", "x"])
    assert rc == 1


# ─── delete ─────────────────────────────────────────────────────────────────


def test_cmd_delete_missing_id(redirect_notes):
    assert notes.cmd_delete([]) == 1


def test_cmd_delete_not_found(redirect_notes):
    assert notes.cmd_delete(["--id", "deadbeef"]) == 1


def test_cmd_delete_success(redirect_notes):
    nid = notes.add_note("T", "C")
    assert notes.cmd_delete(["--id", nid]) == 0
    assert notes.load_notes() == []


# ─── tags / export / stats ─────────────────────────────────────────────────


def test_cmd_tags(redirect_notes, capsys):
    notes.add_note("a", "x", tags=["t1", "t2"])
    notes.add_note("b", "y", tags=["t1"])
    rc = notes.cmd_tags(["--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"t1": 2, "t2": 1}


def test_cmd_export_md(redirect_notes, capsys):
    notes.add_note("Title", "Body content", tags=["x"])
    rc = notes.cmd_export([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Title" in out
    assert "Body content" in out


def test_cmd_export_unknown_format(redirect_notes, capsys):
    notes.add_note("T", "C")
    rc = notes.cmd_export(["--format", "xml"])
    assert rc == 1


def test_cmd_stats(redirect_notes, capsys):
    notes.add_note("a", "x", tags=["t1"], pinned=True)
    notes.add_note("b", "y", tags=["t2"])
    rc = notes.cmd_stats(["--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total"] == 2
    assert data["pinned"] == 1
    assert data["unique_tags"] == 2


# ─── public API ─────────────────────────────────────────────────────────────


def test_search_notes_api(redirect_notes):
    notes.add_note("Hello world", "body", tags=[])
    notes.add_note("other", "no match here", tags=["world"])
    res = notes.search_notes("world")
    assert len(res) == 2


def test_list_notes_api_filters_by_tag(redirect_notes):
    notes.add_note("a", "x", tags=["t1"])
    notes.add_note("b", "y", tags=["t2"])
    res = notes.list_notes(tag="t1")
    assert len(res) == 1
    assert res[0]["title"] == "a"


# ─── main dispatch ──────────────────────────────────────────────────────────


def test_main_unknown_command(redirect_notes, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["notes.py", "bogus"])
    assert notes.main() == 1


def test_main_help(redirect_notes, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["notes.py", "--help"])
    assert notes.main() == 0


def test_main_no_args(redirect_notes, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["notes.py"])
    assert notes.main() == 1
