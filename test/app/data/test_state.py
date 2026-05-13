"""Unit tests for app.data.state."""

from __future__ import annotations

import json
import os

import pytest

from app.data import state


@pytest.fixture
def memory_dir(tmp_path, monkeypatch):
    """Point state.MEMORY_DIR at a writable tmp_path/memory directory."""
    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setattr("app.data.state.MEMORY_DIR", str(mem))
    # Clear caches between tests so mtimes from previous tests don't interfere
    from app.data import _cache as cache_mod

    cache_mod._cache_clear_all()
    yield mem
    cache_mod._cache_clear_all()


def _write_json(path, obj):
    path.write_text(json.dumps(obj))


# ---------- load_state ----------


def test_load_state_returns_default_when_missing(memory_dir):
    result = state.load_state()
    assert result == {"agent_status": "awaiting_first_heartbeat", "cycle_number": 0}


def test_load_state_reads_existing_json(memory_dir):
    _write_json(
        memory_dir / "state.json",
        {"agent_status": "running", "cycle_number": 7, "services": {}},
    )
    result = state.load_state()
    assert result["agent_status"] == "running"
    assert result["cycle_number"] == 7


def test_load_state_uses_cache_on_unchanged_mtime(memory_dir):
    state_path = memory_dir / "state.json"
    _write_json(state_path, {"agent_status": "a", "cycle_number": 1})
    first = state.load_state()
    # Mutate file content but DO NOT change mtime (cache should be served)
    mtime = os.path.getmtime(state_path)
    _write_json(state_path, {"agent_status": "different", "cycle_number": 99})
    os.utime(state_path, (mtime, mtime))
    second = state.load_state()
    assert second == first


def test_load_state_refreshes_when_mtime_changes(memory_dir):
    state_path = memory_dir / "state.json"
    _write_json(state_path, {"status": "a", "cycle_number": 1})
    state.load_state()
    _write_json(state_path, {"status": "b", "cycle_number": 2})
    # Bump mtime forward
    new_mtime = os.path.getmtime(state_path) + 10
    os.utime(state_path, (new_mtime, new_mtime))
    result = state.load_state()
    assert result["cycle_number"] == 2


# ---------- load_services ----------


def test_load_services_empty_when_no_state_file(memory_dir):
    assert state.load_services() == {}


def test_load_services_returns_alive_flag(memory_dir, monkeypatch):
    _write_json(
        memory_dir / "state.json",
        {
            "status": "ok",
            "cycle_number": 1,
            "services": {
                "svc-a": {"pid": 12345, "port": 9000},
                "svc-b": {"pid": 99999, "port": 9001},
            },
        },
    )
    monkeypatch.setattr("app.data.state._pid_alive", lambda pid: pid == 12345)
    result = state.load_services()
    assert result["svc-a"]["alive"] is True
    assert result["svc-b"]["alive"] is False
    assert result["svc-a"]["port"] == 9000


def test_load_services_handles_no_services_key(memory_dir):
    _write_json(memory_dir / "state.json", {"status": "ok"})
    assert state.load_services() == {}


# ---------- load_services_full ----------


def test_load_services_full_empty_when_missing(memory_dir):
    assert state.load_services_full() == {}


def test_load_services_full_includes_command(memory_dir, monkeypatch):
    _write_json(
        memory_dir / "services.json",
        {
            "worker": {
                "pid": 111,
                "command": ["python", "worker.py"],
                "port": 8080,
            }
        },
    )
    monkeypatch.setattr("app.data.state._pid_alive", lambda pid: False)
    result = state.load_services_full()
    assert "worker" in result
    assert result["worker"]["command"] == ["python", "worker.py"]
    assert result["worker"]["alive"] is False


# ---------- load_service_logs ----------


def test_load_service_logs_returns_none_for_missing_service(memory_dir):
    _write_json(memory_dir / "services.json", {})
    assert state.load_service_logs("nope") is None


def test_load_service_logs_reads_stdout_stderr(memory_dir, tmp_path):
    stdout = tmp_path / "out.log"
    stderr = tmp_path / "err.log"
    stdout.write_text("hello stdout")
    stderr.write_text("hello stderr")
    _write_json(
        memory_dir / "services.json",
        {
            "svc": {
                "pid": 1,
                "stdout_log": str(stdout),
                "stderr_log": str(stderr),
            }
        },
    )
    result = state.load_service_logs("svc")
    assert result["stdout"] == "hello stdout"
    assert result["stderr"] == "hello stderr"
    assert result["stdout_path"] == str(stdout)
    assert result["stderr_path"] == str(stderr)


def test_load_service_logs_handles_missing_log_files(memory_dir):
    _write_json(
        memory_dir / "services.json",
        {"svc": {"pid": 1, "stdout_log": "", "stderr_log": ""}},
    )
    result = state.load_service_logs("svc")
    assert result["stdout"] == ""
    assert result["stderr"] == ""


def test_load_service_logs_truncates_large_output(memory_dir, tmp_path):
    big_log = tmp_path / "big.log"
    big_log.write_text("x" * 25000)
    _write_json(
        memory_dir / "services.json",
        {"svc": {"pid": 1, "stdout_log": str(big_log), "stderr_log": ""}},
    )
    result = state.load_service_logs("svc")
    assert "truncated" in result["stdout"]
    # Marker is prepended; content body is at most `cap` bytes of tail.
    assert result["stdout"].startswith("... (truncated")


# ---------- _read_log_capped tail behavior ----------


def test_read_log_capped_returns_empty_for_no_path(tmp_path):
    assert state._read_log_capped("") == ""
    assert state._read_log_capped(None) == ""


def test_read_log_capped_returns_empty_for_missing_file(tmp_path):
    assert state._read_log_capped(str(tmp_path / "does_not_exist.log")) == ""


def test_read_log_capped_returns_full_content_when_under_cap(tmp_path):
    log = tmp_path / "small.log"
    log.write_text("line1\nline2\nline3\n")
    result = state._read_log_capped(str(log), cap=20000)
    assert result == "line1\nline2\nline3\n"
    assert "truncated" not in result


def test_read_log_capped_returns_tail_not_head_when_over_cap(tmp_path):
    """Regression: previously read first `cap` bytes; must now read the tail."""
    log = tmp_path / "big.log"
    head = "HEAD_LINE\n" + ("filler line\n" * 5000)  # well over 20KB
    tail = "TAIL_LINE_FIRST\nTAIL_LINE_LAST\n"
    log.write_text(head + tail)
    result = state._read_log_capped(str(log), cap=20000)
    assert "TAIL_LINE_LAST" in result
    assert "HEAD_LINE" not in result
    # Marker is prepended, not appended — the freshest output sits at the bottom.
    assert result.startswith("... (truncated")
    assert result.rstrip().endswith("TAIL_LINE_LAST")


def test_read_log_capped_drops_partial_first_line_on_truncation(tmp_path):
    log = tmp_path / "big.log"
    # Build a file where the byte at (size - cap) lands mid-line. Each line
    # is 100 bytes; cap of 250 makes the seek land inside a line.
    lines = [f"line-{i:03d}-" + "x" * 90 + "\n" for i in range(50)]
    log.write_text("".join(lines))
    result = state._read_log_capped(str(log), cap=250)
    # Body (after marker) must not contain a leading partial line.
    body = result.split("\n", 1)[1]  # drop the marker line
    for raw_line in body.split("\n"):
        if not raw_line:
            continue
        assert raw_line.startswith("line-"), f"partial line leaked: {raw_line!r}"


def test_read_log_capped_handles_non_utf8_bytes(tmp_path):
    log = tmp_path / "binary.log"
    log.write_bytes(b"hello \xff\xfe world\n")
    result = state._read_log_capped(str(log))
    assert "hello" in result
    assert "world" in result
