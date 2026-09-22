"""Unit tests for app.data.system."""

from __future__ import annotations

import json
import os

import pytest

from app.data import _cache as cache_mod
from app.data import system as sys_mod


@pytest.fixture
def patch_system_paths(monkeypatch, tmp_path):
    agent = tmp_path
    memory = agent / "memory"
    messages = agent / "messages"
    memory.mkdir(parents=True)
    messages.mkdir(parents=True)
    (agent / ".claude").mkdir()

    goals = memory / "goal.json"
    errors = memory / "server_errors.json"
    sched = memory / "scheduled_tasks.json"

    monkeypatch.setattr("app.shared.AGENT_DIR", str(agent))
    monkeypatch.setattr("app.shared.MEMORY_DIR", str(memory))
    monkeypatch.setattr("app.shared.MESSAGES_DIR", str(messages))
    monkeypatch.setattr("app.shared.GOALS_PATH", str(goals))
    monkeypatch.setattr("app.shared.ERROR_LOG_PATH", str(errors))
    monkeypatch.setattr("app.shared.SCHEDULED_TASKS_PATH", str(sched))

    monkeypatch.setattr(sys_mod, "AGENT_DIR", str(agent))
    monkeypatch.setattr(sys_mod, "MEMORY_DIR", str(memory))
    monkeypatch.setattr(sys_mod, "MESSAGES_DIR", str(messages))
    monkeypatch.setattr(sys_mod, "GOALS_PATH", str(goals))
    monkeypatch.setattr(sys_mod, "ERROR_LOG_PATH", str(errors))
    monkeypatch.setattr(sys_mod, "SCHEDULED_TASKS_PATH", str(sched))

    cache_mod._cache_clear_all()
    return {
        "agent": agent,
        "memory": memory,
        "messages": messages,
        "goals": goals,
        "errors": errors,
        "sched": sched,
    }


def _write(p, obj):
    p.write_text(json.dumps(obj))


# ---------- load_scheduled_tasks ----------


def test_load_scheduled_tasks_returns_list(patch_system_paths):
    _write(patch_system_paths["sched"], [{"id": "t1"}, {"id": "t2"}])
    cache_mod._cache_clear_all()
    assert sys_mod.load_scheduled_tasks() == [{"id": "t1"}, {"id": "t2"}]


def test_load_scheduled_tasks_missing_returns_empty(patch_system_paths):
    cache_mod._cache_clear_all()
    assert sys_mod.load_scheduled_tasks() == []


def test_load_scheduled_tasks_non_list_normalised(patch_system_paths):
    _write(patch_system_paths["sched"], {"oops": True})
    cache_mod._cache_clear_all()
    assert sys_mod.load_scheduled_tasks() == []


# ---------- load_errors ----------


def test_load_errors_returns_list(patch_system_paths):
    _write(patch_system_paths["errors"], [{"err": "boom"}])
    cache_mod._cache_clear_all()
    assert sys_mod.load_errors() == [{"err": "boom"}]


def test_load_errors_missing_returns_empty(patch_system_paths):
    cache_mod._cache_clear_all()
    assert sys_mod.load_errors() == []


# ---------- load_plugins ----------


def test_load_plugins_extracts_enabled(patch_system_paths):
    settings = patch_system_paths["agent"] / ".claude" / "settings.json"
    _write(settings, {"enabledPlugins": {"foo": True, "bar": False}})
    cache_mod._cache_clear_all()
    assert sys_mod.load_plugins() == {"foo": True, "bar": False}


def test_load_plugins_missing_returns_empty(patch_system_paths):
    cache_mod._cache_clear_all()
    assert sys_mod.load_plugins() == {}


def test_load_plugins_no_enabled_key(patch_system_paths):
    settings = patch_system_paths["agent"] / ".claude" / "settings.json"
    _write(settings, {"other": "data"})
    cache_mod._cache_clear_all()
    assert sys_mod.load_plugins() == {}


# ---------- load_system_info ----------


def test_load_system_info_handles_missing_proc(monkeypatch, patch_system_paths):
    # All proc reads / shutil calls fail -> all None except timestamp.
    monkeypatch.setattr(
        "builtins.open", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError())
    )
    monkeypatch.setattr(
        sys_mod.shutil, "disk_usage", lambda p: (_ for _ in ()).throw(OSError())
    )
    monkeypatch.setattr(
        sys_mod.os, "getloadavg", lambda: (_ for _ in ()).throw(OSError())
    )
    monkeypatch.setattr(sys_mod.os, "walk", lambda p: (_ for _ in ()).throw(OSError()))

    sys_mod.load_system_info.clear()
    info = sys_mod.load_system_info()
    assert info["memory"] is None
    assert info["disk"] is None
    assert info["load"] is None
    assert info["uptime"] is None
    # workspace_mb may be 0 (empty walk caught) or None — just assert key exists.
    assert "workspace_mb" in info
    assert "timestamp" in info


def test_load_system_info_caches_within_ttl(patch_system_paths):
    sys_mod.load_system_info.clear()
    a = sys_mod.load_system_info()
    b = sys_mod.load_system_info()
    # Same cached instance (timestamp identical) within ttl=60.
    assert a["timestamp"] == b["timestamp"]


# ---------- load_validate ----------


def test_load_validate_all_missing(patch_system_paths):
    cache_mod._cache_clear_all()
    sys_mod.load_validate.clear()
    res = sys_mod.load_validate()
    assert "summary" in res
    assert res["summary"]["errors"] >= 1  # missing state.json etc.
    assert isinstance(res["checks"], list)
    assert res["healthy"] is False


def test_load_validate_healthy_with_complete_files(patch_system_paths):
    mem = patch_system_paths["memory"]
    msgs = patch_system_paths["messages"]
    _write(
        mem / "state.json",
        {
            "cycle_number": 5,
            "status": "ok",
            "last_heartbeat": "2026-04-30T11:59:00+00:00",
        },
    )
    _write(
        mem / "cycles.json",
        [{"cycle": i} for i in range(1, 6)],
    )
    _write(patch_system_paths["goals"], [{"id": "g"}])
    _write(mem / "journal.json", [])
    _write(msgs / "inbox.json", [])
    _write(msgs / "outbox.json", [])

    cache_mod._cache_clear_all()
    sys_mod.load_validate.clear()
    res = sys_mod.load_validate()
    assert res["summary"]["errors"] == 0


def test_load_validate_detects_cycle_continuity_gap(patch_system_paths):
    mem = patch_system_paths["memory"]
    _write(
        mem / "state.json",
        {
            "cycle_number": 5,
            "status": "ok",
            "last_heartbeat": "2026-04-30T11:59:00+00:00",
        },
    )
    _write(
        mem / "cycles.json",
        [{"cycle": 1}, {"cycle": 2}, {"cycle": 4}, {"cycle": 5}],  # gap at 3
    )
    _write(patch_system_paths["goals"], [])
    _write(mem / "journal.json", [])

    cache_mod._cache_clear_all()
    sys_mod.load_validate.clear()
    res = sys_mod.load_validate()
    cont = next(c for c in res["checks"] if c["name"] == "cycle continuity")
    assert cont["passed"] is False
    assert cont["severity"] == "warning"


def test_load_validate_detects_stale_heartbeat(patch_system_paths):
    mem = patch_system_paths["memory"]
    _write(
        mem / "state.json",
        {
            "cycle_number": 1,
            "status": "ok",
            "last_heartbeat": "2020-01-01T00:00:00+00:00",
        },
    )
    _write(mem / "cycles.json", [])
    _write(patch_system_paths["goals"], [])
    _write(mem / "journal.json", [])

    cache_mod._cache_clear_all()
    sys_mod.load_validate.clear()
    res = sys_mod.load_validate()
    hb = next(c for c in res["checks"] if "heartbeat" in c["name"])
    assert hb["passed"] is False


def test_load_validate_detects_malformed_json(patch_system_paths):
    mem = patch_system_paths["memory"]
    (mem / "state.json").write_text("{not valid")
    _write(mem / "cycles.json", [])
    _write(patch_system_paths["goals"], [])
    _write(mem / "journal.json", [])

    cache_mod._cache_clear_all()
    sys_mod.load_validate.clear()
    res = sys_mod.load_validate()
    bad = next(c for c in res["checks"] if c["name"] == "state.json valid JSON")
    assert bad["passed"] is False


def test_load_validate_clear_resets_cache(patch_system_paths):
    cache_mod._cache_clear_all()
    sys_mod.load_validate.clear()
    a = sys_mod.load_validate()
    sys_mod.load_validate.clear()
    # After clear, internal _VALIDATE_CACHE has no "data" key -> recomputed.
    b = sys_mod.load_validate()
    # Both runs produce the same shape; just confirm no crash.
    assert set(a) == set(b)
