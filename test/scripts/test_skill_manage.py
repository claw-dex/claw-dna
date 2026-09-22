"""Tests for scripts/skill_manage.py — lifecycle metadata CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import skill_manage as sm


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect every /agent/... path skill_manage hard-codes onto tmp_path."""
    skills = tmp_path / "skills"
    skills.mkdir()
    archive = skills / ".archive"
    usage = skills / ".usage.json"
    transcripts = tmp_path / "memory" / "transcripts"
    transcripts.mkdir(parents=True)
    nudges = tmp_path / "memory" / "nudges.json"

    monkeypatch.setattr(sm, "SKILLS_DIR", skills)
    monkeypatch.setattr(sm, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(sm, "USAGE_FILE", usage)
    monkeypatch.setattr(sm, "TRANSCRIPTS_DIR", transcripts)
    monkeypatch.setattr(sm, "NUDGES_PATH", nudges)
    return tmp_path


def _seed_skill(sandbox, name, *, frontmatter_name=None, body="hello"):
    """Create a skills/<name>/SKILL.md on disk (no sidecar entry)."""
    fname = frontmatter_name or name
    p = sandbox / "skills" / name
    p.mkdir()
    (p / "SKILL.md").write_text(
        f"---\nname: {fname}\ndescription: test skill\n---\n\n{body}\n"
    )
    return p


def _run(args):
    """Run the CLI with the given argv list; return exit code."""
    parser = sm.build_parser()
    ns = parser.parse_args(args)
    return sm.HANDLERS[ns.cmd](ns)


# ── init ──────────────────────────────────────────────────────────────────


def test_init_seeds_existing_skills(sandbox):
    _seed_skill(sandbox, "alpha")
    _seed_skill(sandbox, "beta")
    rc = _run(["init"])
    assert rc == 0
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert set(data["skills"].keys()) == {"alpha", "beta"}
    for entry in data["skills"].values():
        assert entry["created_by"] == "seed"
        assert entry["pinned"] is True
        assert entry["state"] == "active"


def test_init_is_idempotent_and_preserves_existing(sandbox):
    _seed_skill(sandbox, "alpha")
    _run(["init"])
    # Tamper with the seeded entry.
    usage_file = sandbox / "skills" / ".usage.json"
    data = json.loads(usage_file.read_text())
    data["skills"]["alpha"]["use_count"] = 5
    usage_file.write_text(json.dumps(data))
    _seed_skill(sandbox, "beta")
    _run(["init"])
    data2 = json.loads(usage_file.read_text())
    assert data2["skills"]["alpha"]["use_count"] == 5  # preserved
    assert "beta" in data2["skills"]


def test_init_skips_dot_dirs_and_archive(sandbox):
    _seed_skill(sandbox, "real-one")
    (sandbox / "skills" / ".archive").mkdir(exist_ok=True)
    (sandbox / "skills" / ".archive" / "old-skill").mkdir()
    (sandbox / "skills" / ".archive" / "old-skill" / "SKILL.md").write_text(
        "---\nname: old\ndescription: x\n---\n"
    )
    _run(["init"])
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert "old-skill" not in data["skills"]
    assert "real-one" in data["skills"]


# ── create ────────────────────────────────────────────────────────────────


def test_create_writes_skill_and_sidecar_entry(sandbox, tmp_path):
    body = tmp_path / "body.md"
    body.write_text("# new skill body\n")
    rc = _run(
        [
            "create",
            "--name",
            "newby",
            "--description",
            "a brand new skill",
            "--body-file",
            str(body),
        ]
    )
    assert rc == 0
    skill_md = sandbox / "skills" / "newby" / "SKILL.md"
    text = skill_md.read_text()
    assert "name: newby" in text
    assert "description: a brand new skill" in text
    assert "# new skill body" in text
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert data["skills"]["newby"]["created_by"] == "agent"
    assert data["skills"]["newby"]["pinned"] is False


def test_create_rejects_duplicate(sandbox):
    _seed_skill(sandbox, "dupe")
    with pytest.raises(SystemExit):
        _run(["create", "--name", "dupe", "--description", "x"])


def test_create_rejects_invalid_name(sandbox):
    with pytest.raises(SystemExit):
        _run(["create", "--name", "Bad Name", "--description", "x"])


def test_create_resets_nudge_counters(sandbox):
    nudges = sandbox / "memory" / "nudges.json"
    nudges.parent.mkdir(parents=True, exist_ok=True)
    nudges.write_text(
        json.dumps({"cycles_since_skill_create": 42, "cycles_since_skill_review": 9})
    )
    _run(["create", "--name", "fresh", "--description", "x"])
    data = json.loads(nudges.read_text())
    assert data["cycles_since_skill_create"] == 0
    assert data["cycles_since_skill_review"] == 0


# ── patch / edit / write-file ─────────────────────────────────────────────


def test_patch_appends_section_and_bumps_patch_count(sandbox, tmp_path):
    _seed_skill(sandbox, "alpha")
    _run(["init"])
    body = tmp_path / "extra.md"
    body.write_text("new instructions go here\n")
    _run(
        [
            "patch",
            "--name",
            "alpha",
            "--section",
            "Edge cases",
            "--body-file",
            str(body),
        ]
    )
    text = (sandbox / "skills" / "alpha" / "SKILL.md").read_text()
    assert "## Edge cases" in text
    assert "new instructions go here" in text
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert data["skills"]["alpha"]["patch_count"] == 1
    assert data["skills"]["alpha"]["last_patched_at"] is not None


def test_edit_replaces_body_keeps_frontmatter(sandbox, tmp_path):
    _seed_skill(sandbox, "alpha", body="original\n")
    body = tmp_path / "new.md"
    body.write_text("totally new content\n")
    _run(["edit", "--name", "alpha", "--body-file", str(body)])
    text = (sandbox / "skills" / "alpha" / "SKILL.md").read_text()
    assert "name: alpha" in text
    assert "totally new content" in text
    assert "original" not in text


def test_write_file_rejects_path_traversal(sandbox, tmp_path):
    _seed_skill(sandbox, "alpha")
    body = tmp_path / "evil"
    body.write_text("x")
    with pytest.raises(SystemExit):
        _run(
            [
                "write-file",
                "--name",
                "alpha",
                "--rel-path",
                "../../etc/passwd",
                "--body-file",
                str(body),
            ]
        )


def test_write_file_creates_subdirs(sandbox, tmp_path):
    _seed_skill(sandbox, "alpha")
    body = tmp_path / "helper.py"
    body.write_text("print('x')\n")
    _run(
        [
            "write-file",
            "--name",
            "alpha",
            "--rel-path",
            "scripts/helper.py",
            "--body-file",
            str(body),
        ]
    )
    written = sandbox / "skills" / "alpha" / "scripts" / "helper.py"
    assert written.read_text() == "print('x')\n"


# ── delete ────────────────────────────────────────────────────────────────


def test_delete_requires_absorbed_or_prune(sandbox):
    _seed_skill(sandbox, "alpha")
    _run(["init"])
    # touch unpin so it's not pinned (init seeds pinned=True).
    _run(["touch", "--name", "alpha", "--unpin"])
    with pytest.raises(SystemExit):
        _run(["delete", "--name", "alpha"])


def test_delete_archives_with_absorbed_into(sandbox):
    _seed_skill(sandbox, "alpha")
    _seed_skill(sandbox, "umbrella")
    _run(["init"])
    _run(["touch", "--name", "alpha", "--unpin"])
    _run(["delete", "--name", "alpha", "--absorbed-into", "umbrella"])
    assert not (sandbox / "skills" / "alpha").exists()
    archived = list((sandbox / "skills" / ".archive").iterdir())
    assert any(p.name.startswith("alpha-") for p in archived)
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert data["skills"]["alpha"]["state"] == "archived"
    assert data["skills"]["alpha"]["absorbed_into"] == "umbrella"


def test_delete_with_prune_classifies_as_prune(sandbox):
    _seed_skill(sandbox, "alpha")
    _run(["init"])
    _run(["touch", "--name", "alpha", "--unpin"])
    _run(["delete", "--name", "alpha", "--prune"])
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert data["skills"]["alpha"]["absorbed_into"] is None
    assert data["skills"]["alpha"]["state"] == "archived"


def test_delete_refuses_pinned_without_force(sandbox):
    _seed_skill(sandbox, "alpha")
    _run(["init"])  # seeds pinned=True
    with pytest.raises(SystemExit):
        _run(["delete", "--name", "alpha", "--prune"])


def test_delete_allows_pinned_with_force(sandbox):
    _seed_skill(sandbox, "alpha")
    _run(["init"])
    _run(["delete", "--name", "alpha", "--prune", "--force"])
    assert not (sandbox / "skills" / "alpha").exists()


# ── use / touch / list ────────────────────────────────────────────────────


def test_use_bumps_counter_and_restores_stale(sandbox):
    _seed_skill(sandbox, "alpha")
    _run(["init"])
    # Mark as stale.
    usage_file = sandbox / "skills" / ".usage.json"
    data = json.loads(usage_file.read_text())
    data["skills"]["alpha"]["state"] = "stale"
    usage_file.write_text(json.dumps(data))
    _run(["use", "--name", "alpha"])
    data2 = json.loads(usage_file.read_text())
    assert data2["skills"]["alpha"]["use_count"] == 1
    assert data2["skills"]["alpha"]["state"] == "active"


def test_touch_toggles_pinned(sandbox):
    _seed_skill(sandbox, "alpha")
    _run(["init"])  # pinned=True
    _run(["touch", "--name", "alpha", "--unpin"])
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert data["skills"]["alpha"]["pinned"] is False
    _run(["touch", "--name", "alpha", "--pin"])
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert data["skills"]["alpha"]["pinned"] is True


def test_list_runs_without_error(sandbox, capsys):
    _seed_skill(sandbox, "alpha")
    _run(["init"])
    _run(["list"])
    out = capsys.readouterr().out
    assert "alpha" in out


# ── bump-usage ────────────────────────────────────────────────────────────


def _write_transcript(path: Path, blocks):
    """Write a JSONL transcript. `blocks` = list of (role, content)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for role, content in blocks:
        lines.append(
            json.dumps(
                {
                    "type": role,
                    "timestamp": "2026-04-30T12:00:00Z",
                    "message": {"content": content},
                }
            )
        )
    path.write_text("\n".join(lines) + "\n")


def test_bump_usage_directory_name_word_boundary(sandbox):
    """A tool_use_input that mentions `skills/alpha/SKILL.md` bumps `alpha`."""
    _seed_skill(sandbox, "alpha")
    _run(["init"])
    transcript = sandbox / "memory" / "transcripts" / "cycle-5.jsonl"
    _write_transcript(
        transcript,
        [
            (
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/agent/skills/alpha/SKILL.md"},
                    }
                ],
            )
        ],
    )
    _run(["bump-usage", "--cycle", "5", "--transcript", str(transcript)])
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert data["skills"]["alpha"]["use_count"] == 1
    assert data["last_scanned_cycle"] == 5


def test_bump_usage_is_idempotent(sandbox):
    _seed_skill(sandbox, "alpha")
    _run(["init"])
    transcript = sandbox / "memory" / "transcripts" / "cycle-1.jsonl"
    _write_transcript(
        transcript,
        [("assistant", [{"type": "text", "text": "called alpha here"}])],
    )
    _run(["bump-usage", "--cycle", "1", "--transcript", str(transcript)])
    _run(["bump-usage", "--cycle", "1", "--transcript", str(transcript)])
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert data["skills"]["alpha"]["use_count"] == 1  # second run skipped


def test_bump_usage_skips_unknown_names(sandbox):
    """Phantom names from a tool_use_input pattern must not auto-create entries."""
    _seed_skill(sandbox, "alpha")
    _run(["init"])
    transcript = sandbox / "memory" / "transcripts" / "cycle-7.jsonl"
    _write_transcript(
        transcript,
        [
            (
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "skill_manage.py use --name ghost-skill"},
                    }
                ],
            )
        ],
    )
    _run(["bump-usage", "--cycle", "7", "--transcript", str(transcript)])
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert "ghost-skill" not in data["skills"]


def test_bump_usage_missing_transcript_is_quiet(sandbox):
    """Missing transcript file must not crash."""
    _seed_skill(sandbox, "alpha")
    _run(["init"])
    rc = _run(["bump-usage", "--cycle", "99"])
    assert rc == 0


def test_bump_usage_no_false_positive_on_common_words(sandbox):
    """`change-portal-theme` must not match transcripts that say 'change' alone."""
    _seed_skill(sandbox, "change-portal-theme")
    _run(["init"])
    transcript = sandbox / "memory" / "transcripts" / "cycle-3.jsonl"
    _write_transcript(
        transcript,
        [("user", "please change the wallpaper color")],
    )
    _run(["bump-usage", "--cycle", "3", "--transcript", str(transcript)])
    data = json.loads((sandbox / "skills" / ".usage.json").read_text())
    assert data["skills"]["change-portal-theme"]["use_count"] == 0


def test_bump_usage_restores_from_stale(sandbox):
    _seed_skill(sandbox, "alpha")
    _run(["init"])
    usage_file = sandbox / "skills" / ".usage.json"
    data = json.loads(usage_file.read_text())
    data["skills"]["alpha"]["state"] = "stale"
    usage_file.write_text(json.dumps(data))
    transcript = sandbox / "memory" / "transcripts" / "cycle-2.jsonl"
    _write_transcript(
        transcript,
        [
            (
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/agent/skills/alpha/SKILL.md"},
                    }
                ],
            )
        ],
    )
    _run(["bump-usage", "--cycle", "2", "--transcript", str(transcript)])
    data = json.loads(usage_file.read_text())
    assert data["skills"]["alpha"]["state"] == "active"


# ── _mutate_nudges (file lock + atomic write) ─────────────────────────────


def test_mutate_nudges_creates_and_updates(sandbox):
    sm._mutate_nudges(lambda d: d.update({"foo": 1}))
    data = json.loads((sandbox / "memory" / "nudges.json").read_text())
    assert data["foo"] == 1
    sm._mutate_nudges(lambda d: d.update({"foo": 2, "bar": "x"}))
    data = json.loads((sandbox / "memory" / "nudges.json").read_text())
    assert data == {"foo": 2, "bar": "x"}


def test_mutate_nudges_handles_corrupt_file(sandbox):
    nudges = sandbox / "memory" / "nudges.json"
    nudges.parent.mkdir(parents=True, exist_ok=True)
    nudges.write_text("{ not json")
    sm._mutate_nudges(lambda d: d.update({"reset": True}))
    data = json.loads(nudges.read_text())
    assert data == {"reset": True}


def test_reset_nudge_counter(sandbox):
    nudges = sandbox / "memory" / "nudges.json"
    nudges.parent.mkdir(parents=True, exist_ok=True)
    nudges.write_text(json.dumps({"cycles_since_skill_create": 17}))
    sm._reset_nudge_counter("cycles_since_skill_create")
    data = json.loads(nudges.read_text())
    assert data["cycles_since_skill_create"] == 0
