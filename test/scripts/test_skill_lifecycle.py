"""Tests for scripts/skill_lifecycle.py — pure-Python auto-transitions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import skill_lifecycle as sl


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    skills.mkdir()
    archive = skills / ".archive"
    usage = skills / ".usage.json"
    monkeypatch.setattr(sl, "SKILLS_DIR", skills)
    monkeypatch.setattr(sl, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(sl, "USAGE_FILE", usage)
    return tmp_path


def _write_usage(sandbox, skills_dict):
    payload = {"version": 1, "skills": skills_dict}
    (sandbox / "skills" / ".usage.json").write_text(json.dumps(payload))


def _entry(
    *, created_by="agent", pinned=False, state="active", last_used=None, created_at=None
):
    return {
        "created_by": created_by,
        "pinned": pinned,
        "state": state,
        "use_count": 0,
        "patch_count": 0,
        "created_at": created_at
        or _iso(datetime.now(timezone.utc) - timedelta(days=200)),
        "last_used_at": last_used,
        "last_patched_at": None,
        "absorbed_into": None,
        "source": "skill_manage",
    }


# ── compute ───────────────────────────────────────────────────────────────


def test_compute_missing_usage_returns_empty(sandbox):
    diff = sl.compute()
    assert diff.active_to_stale == []
    assert diff.stale_to_archived == []


def test_compute_active_to_stale_at_30_days(sandbox):
    now = datetime.now(timezone.utc)
    _write_usage(
        sandbox,
        {
            "fresh": _entry(last_used=_iso(now - timedelta(days=5))),
            "rotted": _entry(last_used=_iso(now - timedelta(days=40))),
        },
    )
    diff = sl.compute()
    names = [n for n, _ in diff.active_to_stale]
    assert "rotted" in names
    assert "fresh" not in names


def test_compute_stale_to_archived_at_90_days(sandbox):
    now = datetime.now(timezone.utc)
    _write_usage(
        sandbox,
        {
            "old": _entry(state="stale", last_used=_iso(now - timedelta(days=120))),
        },
    )
    diff = sl.compute()
    names = [n for n, _ in diff.stale_to_archived]
    assert "old" in names


def test_compute_skips_pinned(sandbox):
    now = datetime.now(timezone.utc)
    _write_usage(
        sandbox,
        {
            "pinned-old": _entry(
                pinned=True, last_used=_iso(now - timedelta(days=120))
            ),
        },
    )
    diff = sl.compute()
    assert diff.active_to_stale == []
    assert diff.stale_to_archived == []
    assert "pinned-old" in diff.skipped_pinned


def test_compute_skips_seed_skills(sandbox):
    now = datetime.now(timezone.utc)
    _write_usage(
        sandbox,
        {
            "seedy": _entry(
                created_by="seed", last_used=_iso(now - timedelta(days=120))
            ),
        },
    )
    diff = sl.compute()
    assert diff.active_to_stale == []
    assert diff.stale_to_archived == []
    assert "seedy" in diff.skipped_seed


def test_compute_uses_max_of_timestamps(sandbox):
    """Recent patch should keep skill active even if last_used is old."""
    now = datetime.now(timezone.utc)
    entry = _entry(
        last_used=_iso(now - timedelta(days=200)),
        created_at=_iso(now - timedelta(days=200)),
    )
    entry["last_patched_at"] = _iso(now - timedelta(days=2))
    _write_usage(sandbox, {"alive": entry})
    diff = sl.compute()
    assert diff.active_to_stale == []
    assert diff.stale_to_archived == []


def test_compute_ignores_archived(sandbox):
    now = datetime.now(timezone.utc)
    _write_usage(
        sandbox,
        {"done": _entry(state="archived", last_used=_iso(now - timedelta(days=400)))},
    )
    diff = sl.compute()
    assert diff.stale_to_archived == []


# ── apply ─────────────────────────────────────────────────────────────────


def test_apply_marks_stale(sandbox):
    now = datetime.now(timezone.utc)
    _write_usage(
        sandbox,
        {"rotted": _entry(last_used=_iso(now - timedelta(days=40)))},
    )
    diff = sl.compute()
    result = sl.apply(diff)
    assert "rotted" in result["marked_stale"]
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert data["skills"]["rotted"]["state"] == "stale"


def test_apply_archives_and_moves_dir(sandbox):
    now = datetime.now(timezone.utc)
    skill_dir = sandbox / "skills" / "oldy"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: oldy\ndescription: x\n---\n")
    _write_usage(
        sandbox,
        {
            "oldy": _entry(state="stale", last_used=_iso(now - timedelta(days=120))),
        },
    )
    diff = sl.compute()
    result = sl.apply(diff)
    assert "oldy" in result["archived"]
    assert not skill_dir.exists()
    archive = sandbox / "skills" / ".archive"
    assert archive.exists()
    archived_dirs = [p for p in archive.iterdir() if p.name.startswith("oldy-")]
    assert archived_dirs, "skill dir should be moved into .archive/"


def test_apply_handles_missing_dir_gracefully(sandbox):
    """An entry exists in .usage.json but the dir is already gone."""
    now = datetime.now(timezone.utc)
    _write_usage(
        sandbox,
        {
            "ghost": _entry(state="stale", last_used=_iso(now - timedelta(days=120))),
        },
    )
    diff = sl.compute()
    result = sl.apply(diff)
    # Should still mark the entry as archived in the sidecar.
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert data["skills"]["ghost"]["state"] == "archived"


def test_apply_missing_usage_returns_skipped(sandbox):
    result = sl.apply(sl.Diff([], [], [], []))
    assert result["applied"] is False


# ── report (rendering) ────────────────────────────────────────────────────


def test_report_no_transitions(sandbox):
    out = sl.report(sl.Diff([], [], [], []))
    assert "no transitions" in out


def test_report_lists_transitions(sandbox):
    diff = sl.Diff(
        active_to_stale=[("a", "2026-01-01T00:00:00+00:00")],
        stale_to_archived=[("b", "2025-12-01T00:00:00+00:00")],
        skipped_pinned=["c"],
        skipped_seed=["d"],
    )
    out = sl.report(diff)
    assert "active→stale" in out
    assert "stale→archived" in out
    assert "a" in out and "b" in out
