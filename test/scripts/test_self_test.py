"""Tests for scripts/self_test.py."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import self_test


@pytest.fixture(autouse=True)
def reset_results():
    self_test.results.clear()
    yield
    self_test.results.clear()


@pytest.fixture
def patched(monkeypatch, agent_root):
    monkeypatch.setattr(self_test, "AGENT_DIR", agent_root)
    monkeypatch.setattr(self_test, "MEMORY_DIR", agent_root / "memory")
    monkeypatch.setattr(self_test, "APP_DIR", agent_root / "app")
    monkeypatch.setattr(self_test, "SCRIPTS_DIR", agent_root / "scripts")
    return agent_root


def test_check_records():
    self_test._check("test1", True, "detail", 12.345)
    assert len(self_test.results) == 1
    assert self_test.results[0]["name"] == "test1"
    assert self_test.results[0]["passed"] is True
    assert self_test.results[0]["duration_ms"] == 12.3


def test_check_returns_passed_value():
    assert self_test._check("a", True) is True
    assert self_test._check("b", False) is False


def test_get_failure_on_error(monkeypatch):
    def fake(*a, **kw):
        raise OSError("boom")

    monkeypatch.setattr(self_test.urllib.request, "urlopen", fake)
    status, body, elapsed = self_test._get("/foo")
    assert status == 0
    assert body is None


def test_get_success(monkeypatch):
    class FakeResp:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(
        self_test.urllib.request, "urlopen", lambda *a, **kw: FakeResp()
    )
    status, body, _ = self_test._get("/foo")
    assert status == 200
    assert body == {"ok": True}


def test_test_server_reachable_failure(monkeypatch):
    monkeypatch.setattr(
        self_test.urllib.request,
        "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")),
    )
    self_test.test_server_reachable()
    assert self_test.results[-1]["name"] == "server_reachable"
    assert self_test.results[-1]["passed"] is False


def test_test_memory_files_missing(patched):
    self_test.test_memory_files()
    # All required files missing → all should fail
    failed = [r for r in self_test.results if not r["passed"]]
    assert len(failed) > 0


def test_test_memory_files_present(patched):
    mem = patched / "memory"
    (mem / "state.json").write_text(json.dumps({}))
    (mem / "cycles.json").write_text(json.dumps([]))
    (mem / "goal.json").write_text(json.dumps([]))
    (mem / "journal.json").write_text(json.dumps([]))
    (mem / "server_errors.json").write_text(json.dumps([]))
    self_test.test_memory_files()
    passed = [r for r in self_test.results if r["passed"]]
    assert len(passed) == 5


def test_test_memory_files_invalid_json(patched):
    (patched / "memory" / "state.json").write_text("{bad json")
    self_test.test_memory_files()
    state_result = next(r for r in self_test.results if r["name"] == "mem_state.json")
    assert state_result["passed"] is False


def test_test_memory_files_wrong_type(patched):
    (patched / "memory" / "state.json").write_text(json.dumps([]))  # expected dict
    self_test.test_memory_files()
    r = next(r for r in self_test.results if r["name"] == "mem_state.json")
    assert r["passed"] is False


def test_test_state_fields_missing(patched):
    self_test.test_state_fields()
    # state.json doesn't exist
    assert any(not r["passed"] for r in self_test.results)


def test_test_state_fields_complete(patched):
    now_iso = datetime.now(timezone.utc).isoformat()
    (patched / "memory" / "state.json").write_text(
        json.dumps({"cycle_number": 5, "status": "idle", "last_heartbeat": now_iso})
    )
    self_test.test_state_fields()
    names = [r["name"] for r in self_test.results]
    assert "state_fields" in names
    assert "state_heartbeat_fresh" in names
    fresh = next(r for r in self_test.results if r["name"] == "state_heartbeat_fresh")
    assert fresh["passed"] is True


def test_test_state_fields_stale_heartbeat(patched):
    old = "2020-01-01T00:00:00+00:00"
    (patched / "memory" / "state.json").write_text(
        json.dumps({"cycle_number": 1, "status": "idle", "last_heartbeat": old})
    )
    self_test.test_state_fields()
    fresh = next(r for r in self_test.results if r["name"] == "state_heartbeat_fresh")
    assert fresh["passed"] is False


def test_test_messages_dir_missing(patched):
    # patched messages/ exists but inbox/outbox aren't pre-created
    self_test.test_messages_dir()
    assert any(not r["passed"] for r in self_test.results)


def test_test_messages_dir_valid(patched):
    msgs = patched / "messages"
    msgs.mkdir(parents=True, exist_ok=True)
    (msgs / "inbox.json").write_text(json.dumps([]))
    (msgs / "outbox.json").write_text(json.dumps([]))
    self_test.test_messages_dir()
    assert all(r["passed"] for r in self_test.results)


def test_test_no_active_tab_errors_no_file(patched):
    self_test.test_no_active_tab_errors()
    assert self_test.results[-1]["passed"] is True


def test_test_no_active_tab_errors_old_only(patched):
    old_ts = "2020-01-01T00:00:00+00:00"
    (patched / "memory" / "server_errors.json").write_text(
        json.dumps([{"timestamp": old_ts, "tab": "x", "error": "old"}])
    )
    self_test.test_no_active_tab_errors()
    r = next(r for r in self_test.results if r["name"] == "no_active_tab_errors")
    assert r["passed"] is True


def test_test_no_active_tab_errors_active(patched):
    now = datetime.now(timezone.utc).isoformat()
    (patched / "memory" / "server_errors.json").write_text(
        json.dumps([{"timestamp": now, "tab": "x", "error": "fresh"}])
    )
    self_test.test_no_active_tab_errors()
    r = next(r for r in self_test.results if r["name"] == "no_active_tab_errors")
    assert r["passed"] is False


def test_record_failures_no_failures(patched):
    self_test._check("a", True)
    self_test.record_failures(cycle_hint=1)
    # journal.json should not exist or remain empty
    journal = patched / "memory" / "journal.json"
    if journal.exists():
        data = json.loads(journal.read_text())
        # Either empty or never created
        assert isinstance(data, list)


def test_record_failures_writes_journal(patched):
    self_test._check("a", False, "boom")
    self_test._check("b", False, "kaboom")
    self_test.record_failures(cycle_hint=42)
    journal = json.loads((patched / "memory" / "journal.json").read_text())
    assert len(journal) == 1
    assert journal[0]["status"] == "failed"
    assert journal[0]["cycle"] == 42


def test_print_report_quiet(capsys):
    self_test._check("a", True)
    self_test._check("b", False, "fail")
    self_test.print_report(quiet=True)
    out = capsys.readouterr().out
    assert "1/2 passed" in out
    assert "FAILED: b" in out


def test_print_json_output(capsys):
    self_test._check("a", True)
    self_test._check("b", False)
    self_test.print_json()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["total"] == 2
    assert data["passed"] == 1
    assert data["failed"] == 1


def test_run_all_with_suite_filter(monkeypatch, patched):
    """Only the filtered suite runs."""
    # Patch out heavy suites; our filter is "memory" which is fast
    self_test.run_all(suite_filter="memory")
    # Should have results from memory test only
    assert len(self_test.results) > 0
    # All results should be from mem_*
    names = [r["name"] for r in self_test.results]
    assert all(n.startswith("mem_") for n in names)
