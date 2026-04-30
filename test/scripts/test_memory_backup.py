"""Tests for scripts/memory_backup.py."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import memory_backup as mb


@pytest.fixture
def patch_paths(monkeypatch, agent_root):
    mem = agent_root / "memory"
    backups = mem / "backups"
    monkeypatch.setattr(mb, "MEMORY_DIR", mem)
    monkeypatch.setattr(mb, "BACKUP_ROOT", backups)
    return mem, backups


def _seed_state(mem):
    (mem / "state.json").write_text(json.dumps({"cycle_number": 7}))
    (mem / "cycles.json").write_text(json.dumps([{"cycle": 1}]))
    (mem / "goal.json").write_text(json.dumps([]))


def test_ts_str_format():
    s = mb.ts_str()
    assert len(s) == 16  # YYYYMMDDTHHMMSSZ
    assert s.endswith("Z")
    assert "T" in s


def test_parse_backup_ts_valid():
    dt = mb.parse_backup_ts("20260218T123045Z")
    assert dt is not None
    assert dt.year == 2026
    assert dt.tzinfo is timezone.utc


def test_parse_backup_ts_invalid():
    assert mb.parse_backup_ts("not-a-timestamp") is None
    assert mb.parse_backup_ts("foo") is None


def test_list_backups_empty(patch_paths):
    assert mb.list_backups() == []


def test_list_backups_sorted_newest_first(patch_paths):
    _, root = patch_paths
    root.mkdir(parents=True, exist_ok=True)
    (root / "20260101T000000Z").mkdir()
    (root / "20260301T000000Z").mkdir()
    (root / "20260201T000000Z").mkdir()
    (root / "not-a-backup-dir").mkdir()
    backups = mb.list_backups()
    names = [b.name for b in backups]
    assert names == ["20260301T000000Z", "20260201T000000Z", "20260101T000000Z"]


def test_backup_age_str_seconds(patch_paths, monkeypatch):
    _, root = patch_paths
    root.mkdir(parents=True, exist_ok=True)
    name = mb.ts_str()
    (root / name).mkdir()
    s = mb.backup_age_str(root / name)
    assert "ago" in s


def test_backup_age_str_unknown(patch_paths):
    assert "unknown" in mb.backup_age_str(Path("/tmp/abc"))


def test_cmd_create(patch_paths, capsys):
    mem, root = patch_paths
    _seed_state(mem)
    rv = mb.cmd_create(label="my-label")
    assert rv == 0
    backups = mb.list_backups()
    assert len(backups) == 1
    meta = json.loads((backups[0] / "meta.json").read_text())
    assert meta["cycle"] == 7
    assert meta["label"] == "my-label"
    assert "state.json" in meta["files_copied"]


def test_cmd_create_json_mode(patch_paths, capsys):
    mem, _ = patch_paths
    _seed_state(mem)
    rv = mb.cmd_create(json_mode=True)
    assert rv == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["action"] == "create"
    assert data["files_copied"] >= 1


def test_cmd_list_no_backups(patch_paths, capsys):
    rv = mb.cmd_list()
    assert rv == 2
    assert "No backups" in capsys.readouterr().out


def test_cmd_list_json_empty(patch_paths, capsys):
    rv = mb.cmd_list(json_mode=True)
    assert rv == 2
    data = json.loads(capsys.readouterr().out)
    assert data == {"backups": []}


def test_cmd_list_with_backups(patch_paths, capsys):
    mem, _ = patch_paths
    _seed_state(mem)
    mb.cmd_create()
    rv = mb.cmd_list()
    assert rv == 0


def test_cmd_check_no_backups(patch_paths, capsys):
    rv = mb.cmd_check()
    assert rv == 2


def test_cmd_check_ok(patch_paths, capsys):
    mem, _ = patch_paths
    _seed_state(mem)
    mb.cmd_create()
    capsys.readouterr()  # discard create output
    rv = mb.cmd_check(json_mode=True)
    assert rv == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "ok"


def test_cmd_check_stale(patch_paths, monkeypatch, capsys):
    _, root = patch_paths
    root.mkdir(parents=True, exist_ok=True)
    # Old backup, 2 hours ago
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    (root / old_ts).mkdir()
    rv = mb.cmd_check(json_mode=True)
    assert rv == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "stale"


def test_cmd_prune(patch_paths, capsys):
    _, root = patch_paths
    root.mkdir(parents=True, exist_ok=True)
    for ts in ("20260101T000000Z", "20260201T000000Z", "20260301T000000Z"):
        (root / ts).mkdir()
    rv = mb.cmd_prune(keep=1)
    assert rv == 0
    remaining = mb.list_backups()
    assert len(remaining) == 1
    assert remaining[0].name == "20260301T000000Z"


def test_cmd_restore_no_backups(patch_paths, capsys):
    rv = mb.cmd_restore("latest")
    assert rv == 2


def test_cmd_restore_no_match(patch_paths, capsys):
    _, root = patch_paths
    root.mkdir(parents=True, exist_ok=True)
    (root / "20260101T000000Z").mkdir()
    rv = mb.cmd_restore("nope")
    assert rv == 1


def test_cmd_restore_dry_run(patch_paths, capsys):
    mem, _ = patch_paths
    _seed_state(mem)
    mb.cmd_create()
    capsys.readouterr()  # discard create output
    rv = mb.cmd_restore("latest", dry_run=True, json_mode=True)
    assert rv == 0
    data = json.loads(capsys.readouterr().out)
    assert data["action"] == "restore_dry_run"


def test_cmd_restore_actual(patch_paths, capsys, monkeypatch):
    mem, root = patch_paths
    _seed_state(mem)
    # Manually create a backup with a fixed older timestamp so the pre-restore
    # safety backup (created at "now") doesn't collide and overwrite it.
    import shutil as _sh

    backup_name = "20250101T000000Z"
    dest = root / backup_name
    dest.mkdir(parents=True)
    for f in ("state.json", "cycles.json", "goal.json"):
        _sh.copy2(mem / f, dest / f)
    (dest / "meta.json").write_text(json.dumps({"cycle": 7, "label": ""}))

    # Mutate state
    (mem / "state.json").write_text(json.dumps({"cycle_number": 999}))
    rv = mb.cmd_restore(backup_name)
    assert rv == 0
    state = json.loads((mem / "state.json").read_text())
    assert state["cycle_number"] == 7


def test_main_default_creates(patch_paths, monkeypatch, capsys):
    mem, _ = patch_paths
    _seed_state(mem)
    monkeypatch.setattr(sys, "argv", ["memory_backup.py"])
    rv = mb.main()
    assert rv == 0
    assert len(mb.list_backups()) == 1


def test_main_list(patch_paths, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["memory_backup.py", "--list"])
    rv = mb.main()
    assert rv == 2
