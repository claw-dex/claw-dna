"""Tests for scripts/maintain.py."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import maintain


@pytest.fixture
def patched(monkeypatch, agent_root):
    memory = agent_root / "memory"
    workspace = agent_root / "workspace"
    scripts = agent_root / "scripts"
    logs = memory / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    scripts.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(maintain, "MEMORY_DIR", memory)
    monkeypatch.setattr(maintain, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(maintain, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(maintain, "JOURNAL_PATH", memory / "journal.json")
    monkeypatch.setattr(maintain, "JOURNAL_ARCHIVE", memory / "journal_archive.json")
    monkeypatch.setattr(maintain, "LOGS_DIR", logs)
    return agent_root


def test_check_memory_integrity_missing_files(patched):
    out = maintain.check_memory_integrity()
    statuses = {r["file"]: r["status"] for r in out["results"]}
    assert statuses.get("state.json") == "error"


def test_check_memory_integrity_valid(patched):
    (patched / "memory" / "state.json").write_text(
        json.dumps(
            {
                "cycle_number": 1,
                "status": "idle",
                "last_heartbeat": "2026-04-30T12:00:00+00:00",
            }
        )
    )
    (patched / "memory" / "cycles.json").write_text(json.dumps([]))
    (patched / "memory" / "goal.json").write_text(json.dumps([]))
    (patched / "memory" / "journal.json").write_text(json.dumps([]))
    out = maintain.check_memory_integrity()
    statuses = [r["status"] for r in out["results"]]
    assert "ok" in statuses


def test_check_memory_integrity_invalid_json(patched):
    (patched / "memory" / "state.json").write_text("{not json")
    (patched / "memory" / "cycles.json").write_text(json.dumps([]))
    (patched / "memory" / "goal.json").write_text(json.dumps([]))
    out = maintain.check_memory_integrity()
    err_results = [r for r in out["results"] if r["file"] == "state.json"]
    assert err_results[0]["status"] == "error"


def test_check_journal_size_missing(patched):
    out = maintain.check_journal_size()
    assert out["results"][0]["status"] == "error"


def test_check_journal_size_under_threshold(patched):
    (patched / "memory" / "journal.json").write_text(json.dumps([{"x": 1}] * 5))
    out = maintain.check_journal_size()
    statuses = [r["status"] for r in out["results"]]
    assert "ok" in statuses


def test_check_journal_size_action(patched):
    big_journal = [{"summary": "x" * 100} for _ in range(50)]
    (patched / "memory" / "journal.json").write_text(json.dumps(big_journal))
    out = maintain.check_journal_size()
    statuses = [r["status"] for r in out["results"]]
    assert "action" in statuses


def test_check_journal_size_invalid_json(patched):
    (patched / "memory" / "journal.json").write_text("{bad json")
    out = maintain.check_journal_size()
    assert out["results"][0]["status"] == "error"


def test_check_log_files_no_dir(patched, monkeypatch):
    monkeypatch.setattr(maintain, "LOGS_DIR", patched / "nonexistent_logs")
    out = maintain.check_log_files()
    assert out["results"][0]["status"] == "ok"


def test_check_log_files_empty(patched):
    out = maintain.check_log_files()
    statuses = [r["status"] for r in out["results"]]
    assert "ok" in statuses


def test_check_log_files_overflow(patched):
    for i in range(maintain.LOG_KEEP_CYCLES + 5):
        (patched / "memory" / "logs" / f"bash-{i}.json").write_text("{}")
    out = maintain.check_log_files()
    statuses = [r["status"] for r in out["results"]]
    assert "action" in statuses


def test_check_cycle_prompt_logs_no_dir(monkeypatch, patched):
    monkeypatch.setattr(maintain, "LOGS_DIR", patched / "no_such")
    out = maintain.check_cycle_prompt_logs()
    assert out["results"][0]["status"] == "ok"


def test_check_cycle_prompt_logs_no_logs(patched):
    out = maintain.check_cycle_prompt_logs()
    assert out["results"][0]["status"] == "ok"


def test_check_cycle_prompt_logs_with_old(patched):
    logs = patched / "memory" / "logs"
    # create cycle .log files (so max_cycle is computed)
    for i in range(1, 200):
        (logs / f"cycle-{i}.log").write_text("x")
    # create some old prompt/system files
    (logs / "cycle-10-prompt.md").write_text("x")
    (logs / "cycle-10-system.md").write_text("x")
    out = maintain.check_cycle_prompt_logs()
    statuses = [r["status"] for r in out["results"]]
    assert "action" in statuses


def test_check_stale_processes_handles_subprocess_error(patched, monkeypatch):
    def fake_run(*a, **kw):
        raise OSError("boom")

    monkeypatch.setattr(maintain.subprocess, "run", fake_run)
    out = maintain.check_stale_processes()
    assert out["results"][0]["status"] == "error"


def test_check_stale_processes_one(patched, monkeypatch):
    class FakeResult:
        stdout = "1234 python3 server.py\n"

    monkeypatch.setattr(maintain.subprocess, "run", lambda *a, **kw: FakeResult())
    out = maintain.check_stale_processes()
    assert out["results"][0]["status"] == "ok"


def test_check_stale_processes_multiple(patched, monkeypatch):
    class FakeResult:
        stdout = "1234 python3 server.py\n5678 python3 server.py\n"

    monkeypatch.setattr(maintain.subprocess, "run", lambda *a, **kw: FakeResult())
    out = maintain.check_stale_processes()
    assert out["results"][0]["status"] == "warning"


def test_apply_fix_unknown(patched):
    assert "unknown fix" in maintain.apply_fix("nonexistent")


def test_apply_fix_cycle_prompt_cleanup(patched):
    logs = patched / "memory" / "logs"
    for i in range(1, 200):
        (logs / f"cycle-{i}.log").write_text("x")
    (logs / "cycle-10-prompt.md").write_text("x")
    result = maintain.apply_fix("cycle_prompt_cleanup")
    assert "deleted" in result


def test_main_runs_all(monkeypatch, patched, capsys):
    (patched / "memory" / "state.json").write_text(
        json.dumps(
            {
                "cycle_number": 1,
                "status": "idle",
                "last_heartbeat": "2026-04-30T12:00:00+00:00",
            }
        )
    )
    (patched / "memory" / "cycles.json").write_text(json.dumps([]))
    (patched / "memory" / "goal.json").write_text(json.dumps([]))
    (patched / "memory" / "journal.json").write_text(json.dumps([]))

    # avoid real subprocess calls
    class FakeR:
        stdout = ""

    monkeypatch.setattr(maintain.subprocess, "run", lambda *a, **kw: FakeR())
    monkeypatch.setattr("sys.argv", ["maintain.py", "--json"])
    maintain.main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "checks" in data


def test_main_specific_check(monkeypatch, patched, capsys):
    (patched / "memory" / "journal.json").write_text(json.dumps([]))
    monkeypatch.setattr("sys.argv", ["maintain.py", "--check", "journal_size"])
    maintain.main()
    out = capsys.readouterr().out
    assert "journal_size" in out


def test_main_unknown_check(monkeypatch, patched):
    monkeypatch.setattr("sys.argv", ["maintain.py", "--check", "bogus"])
    with pytest.raises(SystemExit):
        maintain.main()
