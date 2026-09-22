"""Tests for scripts/service_manager.py."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

import service_manager as sm


@pytest.fixture
def redirect_paths(monkeypatch, tmp_path):
    services_file = tmp_path / "services.json"
    state_file = tmp_path / "state.json"
    hb_dir = tmp_path / "heartbeats"
    hb_dir.mkdir()
    state_file.write_text("{}")
    monkeypatch.setattr(sm, "SERVICES_FILE", services_file)
    monkeypatch.setattr(sm, "STATE_FILE", state_file)
    monkeypatch.setattr(sm, "HEARTBEAT_DIR", hb_dir)
    monkeypatch.setattr(sm, "_LOCK_PATH", str(services_file) + ".lock")
    return tmp_path


# ─── Helpers ────────────────────────────────────────────────────────────────


def test_load_missing(redirect_paths):
    assert sm._load() == {}


def test_load_corrupt(redirect_paths):
    sm.SERVICES_FILE.write_text("not json")
    assert sm._load() == {}


def test_save_and_load(redirect_paths):
    sm._save({"a": {"pid": 1}})
    assert sm._load() == {"a": {"pid": 1}}


def test_pid_alive_self():
    assert sm._pid_alive(os.getpid())


def test_pid_alive_dead(monkeypatch):
    def fake_kill(pid, sig):
        raise OSError("dead")

    monkeypatch.setattr(sm.os, "kill", fake_kill)
    assert sm._pid_alive(99999) is False


def test_kill_pid_terminates(monkeypatch, capsys):
    calls = []

    def fake_kill(pid, sig):
        calls.append((pid, sig))

    alive_seq = iter([True, False, False])
    monkeypatch.setattr(sm.os, "kill", fake_kill)
    monkeypatch.setattr(sm, "_pid_alive", lambda p: next(alive_seq))
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    assert sm._kill_pid("svc", 1234)
    assert calls[0] == (1234, signal.SIGTERM)


def test_kill_pid_force_kills(monkeypatch):
    calls = []

    def fake_kill(pid, sig):
        calls.append(sig)

    monkeypatch.setattr(sm.os, "kill", fake_kill)
    monkeypatch.setattr(sm, "_pid_alive", lambda p: True)  # never dies
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    assert sm._kill_pid("svc", 1234)
    assert signal.SIGKILL in calls


def test_kill_pid_handles_exception(monkeypatch, capsys):
    def fake_kill(pid, sig):
        raise PermissionError("nope")

    monkeypatch.setattr(sm.os, "kill", fake_kill)
    assert sm._kill_pid("svc", 1234) is False


def test_find_orphan_pids_no_command():
    assert sm._find_orphan_pids([]) == []


def test_find_orphan_pids_with_match(monkeypatch):
    # Build a fake psutil.process_iter
    class FakeProc:
        def __init__(self, pid, cmdline):
            self.info = {"pid": pid, "cmdline": cmdline}

    fake_procs = [
        FakeProc(2, ["python", "/agent/scripts/foo.py", "--flag"]),
        FakeProc(3, ["python", "/usr/bin/other.py"]),
        FakeProc(4, ["python", "service_manager.py", "list"]),
    ]
    monkeypatch.setattr(sm.psutil, "process_iter", lambda fields: fake_procs)
    monkeypatch.setattr(sm.os, "getpid", lambda: 999)
    orphans = sm._find_orphan_pids(["python", "scripts/foo.py"])
    pids = [pid for pid, _ in orphans]
    assert 2 in pids
    assert 4 not in pids  # service_manager filtered


def test_port_in_use_false(monkeypatch):
    import socket

    class FakeSock:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            return 1  # error

    monkeypatch.setattr(socket, "socket", FakeSock)
    assert sm._port_in_use(8082) is False


def test_port_in_use_true(monkeypatch):
    import socket

    class FakeSock:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            return 0  # ok

    monkeypatch.setattr(socket, "socket", FakeSock)
    assert sm._port_in_use(8082) is True


def test_read_heartbeat_missing(redirect_paths):
    assert sm._read_heartbeat("nope") is None


def test_read_heartbeat_valid(redirect_paths):
    hb = sm.HEARTBEAT_DIR / "svc.heartbeat"
    hb.write_text("1234567890.5")
    assert sm._read_heartbeat("svc") == 1234567890.5


def test_read_heartbeat_invalid(redirect_paths):
    hb = sm.HEARTBEAT_DIR / "svc.heartbeat"
    hb.write_text("not a number")
    assert sm._read_heartbeat("svc") is None


# ─── Subcommands (no actual subprocess) ────────────────────────────────────


def test_cmd_list_empty(redirect_paths, capsys):
    sm.cmd_list()
    assert "No managed services" in capsys.readouterr().out


def test_cmd_list_with_services(redirect_paths, monkeypatch, capsys):
    sm._save(
        {
            "svc1": {"pid": 1234, "port": 8082, "started": "2024-01-01"},
            "svc2": {"pid": None, "port": None, "started": None},
        }
    )
    monkeypatch.setattr(sm, "_pid_alive", lambda p: p == 1234)
    sm.cmd_list()
    out = capsys.readouterr().out
    assert "svc1" in out and "running" in out
    assert "svc2" in out and "stopped" in out


def test_cmd_status_unknown(redirect_paths, capsys):
    with pytest.raises(SystemExit):
        sm.cmd_status("ghost")


def test_cmd_status_known(redirect_paths, monkeypatch, capsys):
    sm._save(
        {
            "svc": {
                "pid": 1234,
                "port": 8082,
                "command": ["python", "x.py"],
                "started": "2024",
                "stdout_log": "/tmp/x",
            }
        }
    )
    monkeypatch.setattr(sm, "_pid_alive", lambda p: True)
    monkeypatch.setattr(sm, "_port_in_use", lambda p: True)
    sm.cmd_status("svc")
    out = capsys.readouterr().out
    assert "alive" in out
    assert "responding" in out


def test_cmd_stop_unknown(redirect_paths):
    with pytest.raises(SystemExit):
        sm.cmd_stop("ghost")


def test_cmd_stop_not_running(redirect_paths, monkeypatch, capsys):
    sm._save({"svc": {"pid": None}})
    sm.cmd_stop("svc")
    out = capsys.readouterr().out
    assert "not running" in out
    services = sm._load()
    assert services["svc"]["pid"] is None


def test_cmd_stop_alive(redirect_paths, monkeypatch, capsys):
    sm._save({"svc": {"pid": 1234}})
    monkeypatch.setattr(sm, "_pid_alive", lambda p: True)
    monkeypatch.setattr(sm, "_kill_pid", lambda n, p: True)
    sm.cmd_stop("svc")
    services = sm._load()
    assert services["svc"]["pid"] is None


def test_cmd_remove_unknown(redirect_paths):
    with pytest.raises(SystemExit):
        sm.cmd_remove("ghost")


def test_cmd_remove_success(redirect_paths, monkeypatch, capsys):
    sm._save({"svc": {"pid": None}})
    sm.cmd_remove("svc")
    assert sm._load() == {}


def test_cmd_cleanup_no_dead(redirect_paths, monkeypatch, capsys):
    sm._save({"svc": {"pid": 1234}})
    monkeypatch.setattr(sm, "_pid_alive", lambda p: True)
    sm.cmd_cleanup()
    assert "No dead entries" in capsys.readouterr().out


def test_cmd_cleanup_removes_dead(redirect_paths, monkeypatch, capsys):
    sm._save({"alive": {"pid": 1}, "dead": {"pid": 2}})
    monkeypatch.setattr(sm, "_pid_alive", lambda p: p == 1)
    sm.cmd_cleanup()
    out = capsys.readouterr().out
    assert "dead" in out
    assert sm._load() == {"alive": {"pid": 1}}


def test_cmd_health_empty(redirect_paths, capsys):
    sm.cmd_health()
    assert "No managed services" in capsys.readouterr().out


def test_cmd_health_ok(redirect_paths, monkeypatch, capsys):
    sm._save({"svc": {"pid": 1, "port": 8082}})
    monkeypatch.setattr(sm, "_pid_alive", lambda p: True)
    monkeypatch.setattr(sm, "_port_in_use", lambda p: True)
    monkeypatch.setattr(sm, "_read_heartbeat", lambda n: None)
    sm.cmd_health()
    out = capsys.readouterr().out
    assert "OK" in out
    assert "HEALTHY" in out


def test_cmd_health_stuck_heartbeat(redirect_paths, monkeypatch, capsys):
    sm._save({"svc": {"pid": 1, "port": 8082}})
    monkeypatch.setattr(sm, "_pid_alive", lambda p: True)
    monkeypatch.setattr(sm, "_port_in_use", lambda p: True)
    # heartbeat way in the past
    monkeypatch.setattr(sm, "_read_heartbeat", lambda n: time.time() - 99999)
    sm.cmd_health()
    out = capsys.readouterr().out
    assert "STUCK" in out
    assert "ISSUES" in out


def test_cmd_start_no_command(redirect_paths, capsys):
    with pytest.raises(SystemExit):
        sm.cmd_start("svc", None, [])


def test_cmd_start_bad_port(redirect_paths, capsys):
    with pytest.raises(SystemExit):
        sm.cmd_start("svc", 9999, ["python", "x.py"])


def test_cmd_start_already_running(redirect_paths, monkeypatch, capsys):
    sm._save({"svc": {"pid": 1234}})
    monkeypatch.setattr(sm, "_pid_alive", lambda p: True)
    with pytest.raises(SystemExit):
        sm.cmd_start("svc", None, ["python", "x.py"])


def test_cmd_start_orphan_blocks(redirect_paths, monkeypatch, capsys):
    monkeypatch.setattr(sm, "_pid_alive", lambda p: False)
    monkeypatch.setattr(
        sm, "_find_orphan_pids", lambda c, exclude_pids=None: [(99, "x")]
    )
    with pytest.raises(SystemExit):
        sm.cmd_start("svc", None, ["python", "x.py"])
