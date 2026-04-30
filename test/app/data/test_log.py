"""Tests for app.data.log loaders."""

from __future__ import annotations

import json

import pytest

from app.data import log


@pytest.fixture(autouse=True)
def _clean_caches():
    log._LOGS_CACHE.clear()
    log._LOG_DETAIL_CACHE.clear()
    yield
    log._LOGS_CACHE.clear()
    log._LOG_DETAIL_CACHE.clear()


@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setattr("app.data.log.LOGS_DIR", str(d))
    return d


def _write_meta(d, num, **fields):
    meta = {"id": num}
    meta.update(fields)
    p = d / f"bash-{num}.json"
    p.write_text(json.dumps(meta))
    return p


def test_load_logs_empty_dir(logs_dir):
    assert log.load_logs() == []


def test_load_logs_missing_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("app.data.log.LOGS_DIR", str(tmp_path / "ghost"))
    assert log.load_logs() == []


def test_load_logs_skips_corrupt(logs_dir):
    (logs_dir / "bash-1.json").write_text("not json")
    _write_meta(logs_dir, 2, status="completed")
    out = log.load_logs()
    assert len(out) == 1
    assert out[0]["id"] == 2


def test_load_logs_running_with_dead_pid_marked_exited(logs_dir, monkeypatch):
    _write_meta(logs_dir, 1, status="running", pid=999999)
    monkeypatch.setattr("app.data.log._pid_alive", lambda pid: False)
    out = log.load_logs()
    assert out[0]["status"] == "exited"


def test_load_logs_running_with_alive_pid_kept(logs_dir, monkeypatch):
    _write_meta(logs_dir, 1, status="running", pid=1234)
    monkeypatch.setattr("app.data.log._pid_alive", lambda pid: True)
    out = log.load_logs()
    assert out[0]["status"] == "running"


def test_load_logs_reads_exitcode_log(logs_dir, tmp_path):
    ec_file = tmp_path / "ec.txt"
    ec_file.write_text("42\n")
    _write_meta(
        logs_dir,
        1,
        status="completed",
        exitcode_log=str(ec_file),
        exit_code=None,
    )
    out = log.load_logs()
    assert out[0]["exit_code"] == 42


def test_load_logs_computes_duration(logs_dir):
    _write_meta(
        logs_dir,
        1,
        status="completed",
        started_at="2026-01-01T00:00:00",
        ended_at="2026-01-01T00:00:12.500000",
    )
    out = log.load_logs()
    assert out[0]["duration_seconds"] == 12.5


def test_load_logs_bad_timestamps_no_crash(logs_dir):
    _write_meta(
        logs_dir, 1, status="completed", started_at="bogus", ended_at="alsobogus"
    )
    out = log.load_logs()
    assert "duration_seconds" not in out[0]


def test_load_log_detail_missing(logs_dir):
    assert log.load_log_detail(99) is None


def test_load_log_detail_reads_streams(logs_dir, tmp_path):
    out_file = tmp_path / "out.log"
    err_file = tmp_path / "err.log"
    out_file.write_text("hello stdout")
    err_file.write_text("oops stderr")
    _write_meta(
        logs_dir,
        7,
        status="completed",
        stdout_log=str(out_file),
        stderr_log=str(err_file),
    )
    detail = log.load_log_detail(7)
    assert detail["stdout"] == "hello stdout"
    assert detail["stderr"] == "oops stderr"
    assert detail["status"] == "completed"


def test_load_log_detail_truncates_long_output(logs_dir, tmp_path):
    out_file = tmp_path / "out.log"
    out_file.write_text("x" * 20000)
    _write_meta(logs_dir, 1, stdout_log=str(out_file))
    detail = log.load_log_detail(1)
    assert detail["stdout"].endswith("(truncated)")
    assert len(detail["stdout"]) <= 10000 + len("\n... (truncated)")


def test_load_log_detail_running_dead_pid(logs_dir, monkeypatch):
    _write_meta(logs_dir, 1, status="running", pid=999999)
    monkeypatch.setattr("app.data.log._pid_alive", lambda pid: False)
    detail = log.load_log_detail(1)
    assert detail["status"] == "exited"


def test_load_log_detail_clear_attribute(logs_dir):
    # clear should be exposed for cache invalidation
    assert callable(log.load_log_detail.clear)
    log._LOG_DETAIL_CACHE["sentinel"] = 1
    log.load_log_detail.clear()
    assert "sentinel" not in log._LOG_DETAIL_CACHE


def test_load_log_detail_caches(logs_dir, tmp_path):
    out_file = tmp_path / "out.log"
    out_file.write_text("first")
    _write_meta(logs_dir, 3, stdout_log=str(out_file))
    a = log.load_log_detail(3)
    # Mutate file content but freeze its mtime so cache returns stale
    import os

    om = os.path.getmtime(out_file)
    out_file.write_text("second")
    os.utime(out_file, (om, om))
    # also pin meta's mtime
    meta_path = logs_dir / "bash-3.json"
    mm = os.path.getmtime(meta_path)
    os.utime(meta_path, (mm, mm))
    b = log.load_log_detail(3)
    assert a is b  # same object — cache hit
