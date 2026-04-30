"""Unit tests for app.data.write."""

from __future__ import annotations

import json
import subprocess
import sys
import types

import pytest

from app.data import write


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Redirect every path constant write.py imported to tmp_path subdirs."""
    mem = tmp_path / "memory"
    msg = tmp_path / "messages"
    scripts = tmp_path / "scripts"
    logs = mem / "logs"
    for d in (mem, msg, scripts, logs):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("app.data.write.AGENT_DIR", str(tmp_path))
    monkeypatch.setattr("app.data.write.MEMORY_DIR", str(mem))
    monkeypatch.setattr("app.data.write.LOGS_DIR", str(logs))
    monkeypatch.setattr("app.data.write.MESSAGES_DIR", str(msg))
    monkeypatch.setattr("app.data.write.SCRIPTS_DIR", str(scripts))
    monkeypatch.setattr("app.data.write.GOALS_PATH", str(mem / "goal.json"))
    monkeypatch.setattr(
        "app.data.write.PORTAL_CONFIG_PATH", str(mem / "portal_config.json")
    )
    monkeypatch.setattr(
        "app.data.write.SCHEDULED_TASKS_PATH",
        str(mem / "scheduled_tasks.json"),
    )
    # The shared module's helpers (_write_json_atomic, AtomicJSON, _append_history)
    # use module-global HISTORY_PATH from app.shared. Patch that too so writes
    # don't leak to /agent/memory/.
    monkeypatch.setattr("app.shared.HISTORY_PATH", str(mem / "command_history.json"))
    from app.data import _cache as cache_mod

    cache_mod._cache_clear_all()
    return types.SimpleNamespace(
        root=tmp_path,
        memory=mem,
        messages=msg,
        scripts=scripts,
        logs=logs,
        goals=mem / "goal.json",
        portal_config=mem / "portal_config.json",
        scheduled_tasks=mem / "scheduled_tasks.json",
        history=mem / "command_history.json",
        inbox=msg / "inbox.json",
        outbox=msg / "outbox.json",
        outbox_history=mem / "outbox_history.json",
        services=mem / "services.json",
        goal_history=mem / "goal_history.json",
    )


# ---------- save_portal_config ----------


def test_save_portal_config_creates_file(paths):
    write.save_portal_config("theme", "dark")
    data = json.loads(paths.portal_config.read_text())
    assert data == {"theme": "dark"}


def test_save_portal_config_preserves_other_keys(paths):
    paths.portal_config.write_text(json.dumps({"theme": "light", "lang": "en"}))
    write.save_portal_config("theme", "dark")
    data = json.loads(paths.portal_config.read_text())
    assert data == {"theme": "dark", "lang": "en"}


# ---------- queue_to_inbox ----------


def test_queue_to_inbox_appends_entry(paths):
    write.queue_to_inbox("hello", "message", "2026-01-01T00:00:00Z", priority=2)
    data = json.loads(paths.inbox.read_text())
    assert len(data) == 1
    assert data[0]["content"] == "hello"
    assert data[0]["type"] == "message"
    assert data[0]["priority"] == 2


def test_queue_to_inbox_clamps_priority(paths):
    write.queue_to_inbox("a", "goal", "t", priority=99)
    write.queue_to_inbox("b", "goal", "t", priority=-5)
    data = json.loads(paths.inbox.read_text())
    assert data[0]["priority"] == 5
    assert data[1]["priority"] == 1


def test_queue_to_inbox_writes_history(paths):
    write.queue_to_inbox("hi", "message", "2026-01-01T00:00:00Z")
    hist = json.loads(paths.history.read_text())
    assert hist[-1]["content"] == "hi"
    assert hist[-1]["result"] == "queued"


# ---------- run_script ----------


@pytest.mark.parametrize(
    "bad", ["", "../escape.py", "/abs.py", ".hidden.py", "with/slash.py"]
)
def test_run_script_rejects_invalid_names(paths, bad):
    result = write.run_script(bad)
    assert result["ok"] is False
    assert "Invalid" in result["error"]


def test_run_script_missing_file(paths):
    result = write.run_script("nope.py")
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_run_script_unsupported_extension(paths):
    (paths.scripts / "tool.exe").write_text("")
    result = write.run_script("tool.exe")
    assert result["ok"] is False
    assert "Unsupported" in result["error"]


def test_run_script_python_invokes_subprocess(paths, mocker):
    (paths.scripts / "ok.py").write_text("print('hi')")
    fake = mocker.Mock(returncode=0, stdout="out", stderr="")
    run = mocker.patch("app.data.write.subprocess.run", return_value=fake)
    result = write.run_script("ok.py", args=["--flag", '"quoted"'])
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "out"
    cmd = run.call_args.args[0]
    assert cmd[:3] == ["uv", "run", "python"]
    # Quotes should be stripped from the args
    assert "quoted" in cmd
    assert '"quoted"' not in cmd


def test_run_script_shell_uses_bash(paths, mocker):
    (paths.scripts / "ok.sh").write_text("echo hi")
    fake = mocker.Mock(returncode=0, stdout="x", stderr="")
    run = mocker.patch("app.data.write.subprocess.run", return_value=fake)
    write.run_script("ok.sh")
    cmd = run.call_args.args[0]
    assert cmd[0] == "bash"


def test_run_script_handles_timeout(paths, mocker):
    (paths.scripts / "ok.py").write_text("print('hi')")
    mocker.patch(
        "app.data.write.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=30),
    )
    result = write.run_script("ok.py")
    assert result["ok"] is False
    assert "timed out" in result["error"]


def test_run_script_handles_generic_exception(paths, mocker):
    (paths.scripts / "ok.py").write_text("print('hi')")
    mocker.patch("app.data.write.subprocess.run", side_effect=RuntimeError("boom"))
    result = write.run_script("ok.py")
    assert result["ok"] is False
    assert "boom" in result["error"]


# ---------- write_first_goal ----------


def test_write_first_goal_creates(paths):
    write.write_first_goal("kick off", "2026-01-01T00:00:00Z")
    data = json.loads(paths.goals.read_text())
    assert len(data) == 1
    assert data[0]["goal"] == "kick off"
    assert data[0]["status"] == "pending"


def test_write_first_goal_no_op_when_existing(paths):
    paths.goals.write_text(json.dumps([{"id": "goal-1", "goal": "old"}]))
    write.write_first_goal("new", "2026-01-01T00:00:00Z")
    data = json.loads(paths.goals.read_text())
    assert data[0]["goal"] == "old"


# ---------- trigger_bootstrap_heartbeat ----------


def test_trigger_bootstrap_heartbeat_returns_pid(paths, mocker):
    proc = mocker.Mock()
    proc.pid = 12345
    mocker.patch("app.data.write.subprocess.Popen", return_value=proc)
    result = write.trigger_bootstrap_heartbeat()
    assert result["ok"] is True
    assert result["pid"] == 12345


def test_trigger_bootstrap_heartbeat_handles_exception(paths, mocker):
    mocker.patch("app.data.write.subprocess.Popen", side_effect=OSError("no bash"))
    result = write.trigger_bootstrap_heartbeat()
    assert result["ok"] is False
    assert "no bash" in result["error"]


# ---------- update_goal_status ----------


def test_update_goal_status_updates_index(paths):
    paths.goals.write_text(
        json.dumps(
            [
                {"id": "g1", "status": "pending"},
                {"id": "g2", "status": "pending"},
            ]
        )
    )
    write.update_goal_status(1, "completed")
    data = json.loads(paths.goals.read_text())
    assert data[1]["status"] == "completed"
    assert data[0]["status"] == "pending"


def test_update_goal_status_out_of_range_noop(paths):
    paths.goals.write_text(json.dumps([{"id": "g1", "status": "pending"}]))
    write.update_goal_status(99, "completed")
    data = json.loads(paths.goals.read_text())
    assert data[0]["status"] == "pending"


# ---------- is_archivable_goal & archive_goals ----------


def test_is_archivable_goal_short_term_completed():
    assert write.is_archivable_goal({"id": "abc-1", "status": "completed"})


def test_is_archivable_goal_long_term_preserved():
    assert not write.is_archivable_goal({"id": "goal-1", "status": "completed"})


def test_is_archivable_goal_in_progress_not_archivable():
    assert not write.is_archivable_goal({"id": "x-1", "status": "in_progress"})


def test_archive_goals_moves_completed(paths):
    paths.goals.write_text(
        json.dumps(
            [
                {
                    "id": "task-1",
                    "status": "completed",
                    "created_at": "2026-01-01",
                },
                {"id": "task-2", "status": "pending"},
                {
                    "id": "goal-1",
                    "status": "completed",
                    "created_at": "2026-01-01",
                },
            ]
        )
    )
    n = write.archive_goals()
    assert n == 1
    remaining = json.loads(paths.goals.read_text())
    ids = [g["id"] for g in remaining]
    assert "task-1" not in ids
    assert "task-2" in ids
    assert "goal-1" in ids
    history = json.loads(paths.goal_history.read_text())
    assert history[0]["id"] == "task-1"


def test_archive_goals_empty_when_nothing_archivable(paths):
    paths.goals.write_text(json.dumps([{"id": "x-1", "status": "pending"}]))
    assert write.archive_goals() == 0


# ---------- delete_inbox_item ----------


def test_delete_inbox_item_removes(paths):
    paths.inbox.write_text(json.dumps([{"a": 1}, {"a": 2}, {"a": 3}]))
    write.delete_inbox_item(1)
    data = json.loads(paths.inbox.read_text())
    assert data == [{"a": 1}, {"a": 3}]


def test_delete_inbox_item_out_of_range(paths):
    paths.inbox.write_text(json.dumps([{"a": 1}]))
    write.delete_inbox_item(50)
    data = json.loads(paths.inbox.read_text())
    assert data == [{"a": 1}]


# ---------- clear_outbox ----------


def test_clear_outbox_archives_to_history(paths):
    paths.outbox.write_text(
        json.dumps([{"timestamp": "2026-01-01", "msg": "hi"}, {"msg": "no_ts"}])
    )
    write.clear_outbox()
    assert json.loads(paths.outbox.read_text()) == []
    history = json.loads(paths.outbox_history.read_text())
    assert any(m.get("msg") == "hi" for m in history)


def test_clear_outbox_dedupes_by_timestamp(paths):
    paths.outbox.write_text(json.dumps([{"timestamp": "T1", "msg": "a"}]))
    paths.outbox_history.write_text(json.dumps([{"timestamp": "T1", "msg": "a"}]))
    write.clear_outbox()
    history = json.loads(paths.outbox_history.read_text())
    assert len(history) == 1


# ---------- service ops (validation only — wraps service_manager) ----------


@pytest.mark.parametrize("bad", ["", "/abs", "../x", "-flag", "name\x00null", None])
def test_remove_service_rejects_bad_names(paths, bad):
    result = write.remove_service(bad)
    assert result["ok"] is False


def test_remove_service_calls_cmd_remove(paths, mocker):
    fake_mod = types.ModuleType("scripts.service_manager")
    fake_mod.cmd_remove = mocker.Mock()
    mocker.patch.dict(sys.modules, {"scripts.service_manager": fake_mod})
    result = write.remove_service("svc")
    assert result["ok"] is True
    assert result["removed"] == "svc"
    fake_mod.cmd_remove.assert_called_once_with("svc")


def test_stop_service_calls_cmd_stop(paths, mocker):
    fake_mod = types.ModuleType("scripts.service_manager")
    fake_mod.cmd_stop = mocker.Mock()
    mocker.patch.dict(sys.modules, {"scripts.service_manager": fake_mod})
    result = write.stop_service("svc")
    assert result["ok"] is True
    fake_mod.cmd_stop.assert_called_once_with("svc")


def test_start_service_missing_in_services_json(paths):
    paths.services.write_text(json.dumps({}))
    result = write.start_service("svc")
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_start_service_no_command(paths):
    paths.services.write_text(json.dumps({"svc": {"command": None}}))
    result = write.start_service("svc")
    assert result["ok"] is False
    assert "No valid command" in result["error"]


def test_start_service_invalid_port(paths):
    paths.services.write_text(json.dumps({"svc": {"command": ["x"], "port": "abc"}}))
    result = write.start_service("svc")
    assert result["ok"] is False
    assert "Invalid port" in result["error"]


def test_start_service_calls_cmd_start(paths, mocker):
    paths.services.write_text(
        json.dumps({"svc": {"command": ["python", "x.py"], "port": "8080"}})
    )
    fake_mod = types.ModuleType("scripts.service_manager")
    fake_mod.cmd_start = mocker.Mock()
    mocker.patch.dict(sys.modules, {"scripts.service_manager": fake_mod})
    result = write.start_service("svc")
    assert result["ok"] is True
    fake_mod.cmd_start.assert_called_once_with("svc", 8080, ["python", "x.py"])


# ---------- scheduled tasks ----------


def test_create_scheduled_task_requires_id(paths):
    result = write.create_scheduled_task({"id": "  "})
    assert result["ok"] is False


def test_create_scheduled_task_appends(paths):
    result = write.create_scheduled_task({"id": "t1", "cron": "* * * * *"})
    assert result["ok"] is True
    data = json.loads(paths.scheduled_tasks.read_text())
    assert data[0]["id"] == "t1"


def test_create_scheduled_task_duplicate(paths):
    paths.scheduled_tasks.write_text(json.dumps([{"id": "t1"}]))
    result = write.create_scheduled_task({"id": "t1"})
    assert result["ok"] is False
    assert "already exists" in result["error"]


def test_update_scheduled_task_modifies(paths):
    paths.scheduled_tasks.write_text(json.dumps([{"id": "t1", "cron": "old"}]))
    result = write.update_scheduled_task("t1", {"cron": "new"})
    assert result["ok"] is True
    data = json.loads(paths.scheduled_tasks.read_text())
    assert data[0]["cron"] == "new"


def test_update_scheduled_task_not_found(paths):
    paths.scheduled_tasks.write_text(json.dumps([]))
    result = write.update_scheduled_task("missing", {"cron": "x"})
    assert result["ok"] is False


def test_delete_scheduled_task(paths):
    paths.scheduled_tasks.write_text(
        json.dumps([{"id": "a"}, {"id": "b"}, {"id": "c"}])
    )
    result = write.delete_scheduled_task("b")
    assert result["ok"] is True
    data = json.loads(paths.scheduled_tasks.read_text())
    assert [t["id"] for t in data] == ["a", "c"]


def test_delete_scheduled_task_not_found(paths):
    paths.scheduled_tasks.write_text(json.dumps([{"id": "a"}]))
    result = write.delete_scheduled_task("zz")
    assert result["ok"] is False
