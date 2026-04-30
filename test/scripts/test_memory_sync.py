"""Tests for scripts/memory_sync.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import memory_sync as msync


@pytest.fixture
def patch_paths(monkeypatch, agent_root):
    mem = agent_root / "memory"
    monkeypatch.setattr(msync, "MEMORY", mem)
    return mem


def test_load_json_missing(tmp_path):
    assert msync.load_json(tmp_path / "x.json") is None


def test_load_json_invalid(tmp_path, capsys):
    p = tmp_path / "x.json"
    p.write_text("not json")
    assert msync.load_json(p) is None


def test_write_md_unchanged(tmp_path):
    p = tmp_path / "out.md"
    p.write_text("hello")
    assert msync.write_md(p, "hello") is False


def test_write_md_writes(tmp_path):
    p = tmp_path / "out.md"
    assert msync.write_md(p, "fresh content") is True
    assert p.read_text() == "fresh content"


def test_write_md_dry_run(tmp_path, capsys):
    p = tmp_path / "out.md"
    assert msync.write_md(p, "fresh", dry_run=True) is True
    assert not p.exists()


def test_frontmatter_format():
    s = msync.frontmatter("Foo", "bar")
    assert s.startswith("---")
    assert "name: Foo" in s
    assert "description: bar" in s


def test_render_capabilities_empty(patch_paths):
    assert msync.render_capabilities() == ""


def test_render_capabilities(patch_paths):
    (patch_paths / "capabilities.json").write_text(
        json.dumps(
            [
                {"category": "core", "name": "X", "enabled": True, "description": "d"},
                {"category": "ext", "name": "Y", "enabled": False, "description": "e"},
            ]
        )
    )
    md = msync.render_capabilities()
    assert "Core" in md
    assert "**X**" in md
    assert "enabled" in md


def test_render_services(patch_paths):
    (patch_paths / "services.json").write_text(
        json.dumps({"svc1": {"pid": 123, "port": 8080, "command": ["python", "x.py"]}})
    )
    md = msync.render_services()
    assert "svc1" in md
    assert "running" in md


def test_render_state(patch_paths):
    (patch_paths / "state.json").write_text(
        json.dumps(
            {"cycle_number": 5, "status": "running", "last_heartbeat": "2026-01-01"}
        )
    )
    md = msync.render_state()
    assert "Cycle**: 5" in md


def test_render_state_missing(patch_paths):
    assert msync.render_state() == ""


def test_render_cycles(patch_paths):
    (patch_paths / "cycles.json").write_text(
        json.dumps(
            [
                {"cycle": 1, "type": "explore", "status": "ok", "duration_seconds": 30},
                {"cycle": 2, "type": "evolve", "status": "ok"},
            ]
        )
    )
    md = msync.render_cycles()
    assert "Cycle 1" in md
    assert "Cycle 2" in md


def test_render_cycles_empty(patch_paths):
    (patch_paths / "cycles.json").write_text("[]")
    md = msync.render_cycles()
    assert "No cycles" in md


def test_render_journal(patch_paths):
    (patch_paths / "journal.json").write_text(
        json.dumps(
            [
                {
                    "cycle": 1,
                    "type": "evolve",
                    "status": "ok",
                    "goal": "g",
                    "summary": "s",
                    "actions": ["a1", "a2"],
                    "outcome": "good",
                    "learnings": {"approach": "appr"},
                }
            ]
        )
    )
    md = msync.render_journal()
    assert "Cycle 1" in md
    assert "**Goal**" in md
    assert "a1" in md
    assert "Approach" in md


def test_render_goals_grouped(patch_paths):
    (patch_paths / "goal.json").write_text(
        json.dumps(
            [
                {"content": "g1", "status": "in_progress"},
                {"content": "g2", "status": "completed"},
            ]
        )
    )
    md = msync.render_goals()
    assert "In_Progress" in md
    assert "g1" in md


def test_render_goals_dict_form(patch_paths):
    (patch_paths / "goal.json").write_text(
        json.dumps({"goals": [{"content": "x", "status": "pending"}]})
    )
    md = msync.render_goals()
    assert "x" in md


def test_render_notes(patch_paths):
    (patch_paths / "notes.json").write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "title": "T",
                    "tags": ["a"],
                    "pinned": True,
                    "updated_at": "2026-01-01T00:00",
                    "content": "body",
                }
            ]
        )
    )
    md = msync.render_notes()
    assert "PINNED" in md
    assert "body" in md


def test_fmt_dur():
    assert msync._fmt_dur(None) == ""
    assert msync._fmt_dur(45) == "45s"
    assert msync._fmt_dur(125) == "2m 5s"
    assert "h" in msync._fmt_dur(3700)


def test_sync_all_only_filter(patch_paths):
    (patch_paths / "state.json").write_text(json.dumps({"cycle_number": 1}))
    synced, errors = msync.sync_all(only={"state"})
    assert synced == 1
    assert errors == 0
    assert (patch_paths / "state_memory.md").exists()


def test_sync_all_dry_run(patch_paths, capsys):
    (patch_paths / "state.json").write_text(json.dumps({"cycle_number": 1}))
    synced, _ = msync.sync_all(dry_run=True, only={"state"})
    assert synced == 1
    assert not (patch_paths / "state_memory.md").exists()


def test_main_runs(patch_paths, monkeypatch, capsys):
    (patch_paths / "state.json").write_text(json.dumps({"cycle_number": 1}))
    monkeypatch.setattr(sys, "argv", ["memory_sync.py", "--only", "state"])
    msync.main()
    assert (
        "synced" in capsys.readouterr().out.lower()
        or (patch_paths / "state_memory.md").exists()
    )
