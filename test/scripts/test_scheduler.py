"""Tests for scripts/scheduler.py — focus on pure helpers + CRUD."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import scheduler


@pytest.fixture
def patched(monkeypatch, agent_root):
    tasks = agent_root / "memory" / "scheduled_tasks.json"
    inbox = agent_root / "messages" / "inbox.json"
    monkeypatch.setattr(scheduler, "MEMORY", agent_root / "memory")
    monkeypatch.setattr(scheduler, "MESSAGES", agent_root / "messages")
    monkeypatch.setattr(scheduler, "TASKS_PATH", tasks)
    monkeypatch.setattr(scheduler, "INBOX_PATH", inbox)
    return agent_root


# ── Cron field parsing ──────────────────────────────────────────────────────


def test_parse_cron_field_wildcard():
    assert scheduler._parse_cron_field("*", 0, 59) is None


def test_parse_cron_field_int():
    assert scheduler._parse_cron_field("5", 0, 59) == 5


def test_parse_cron_field_out_of_range():
    assert scheduler._parse_cron_field("60", 0, 59) == "ERROR"


def test_parse_cron_field_range():
    result = scheduler._parse_cron_field("1-3", 0, 59)
    assert result == frozenset({1, 2, 3})


def test_parse_cron_field_list():
    result = scheduler._parse_cron_field("1,3,5", 0, 59)
    assert result == frozenset({1, 3, 5})


def test_parse_cron_field_step():
    result = scheduler._parse_cron_field("*/15", 0, 59)
    assert result == frozenset({0, 15, 30, 45})


def test_parse_cron_field_range_step():
    result = scheduler._parse_cron_field("1-10/2", 0, 59)
    assert result == frozenset({1, 3, 5, 7, 9})


def test_parse_cron_field_invalid():
    assert scheduler._parse_cron_field("abc", 0, 59) == "ERROR"
    assert scheduler._parse_cron_field("*/0", 0, 59) == "ERROR"


def test_parse_cron_pattern_valid():
    parsed = scheduler._parse_cron_pattern("0 9 * * *")
    assert parsed is not None
    assert parsed[0] == 0
    assert parsed[1] == 9
    assert parsed[2] is None


def test_parse_cron_pattern_wrong_field_count():
    assert scheduler._parse_cron_pattern("0 9 *") is None
    assert scheduler._parse_cron_pattern("0 9 * * * *") is None


def test_parse_cron_pattern_invalid_field():
    assert scheduler._parse_cron_pattern("99 * * * *") is None


def test_cron_matches_exact():
    now = datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc)
    assert scheduler._cron_matches("0 9 * * *", now) is True


def test_cron_matches_no():
    now = datetime(2026, 4, 30, 9, 5, 0, tzinfo=timezone.utc)
    assert scheduler._cron_matches("0 9 * * *", now) is False


def test_cron_matches_invalid_returns_false():
    now = datetime.now(timezone.utc)
    assert scheduler._cron_matches("invalid", now) is False


# ── _parse_every ────────────────────────────────────────────────────────────


def test_parse_every_minutes():
    assert scheduler._parse_every("30m") == 30


def test_parse_every_hours():
    assert scheduler._parse_every("2h") == 120


def test_parse_every_days():
    assert scheduler._parse_every("1d") == 1440


def test_parse_every_bare_int():
    assert scheduler._parse_every("45") == 45


# ── _is_due ─────────────────────────────────────────────────────────────────


def test_is_due_disabled_task():
    task = {"enabled": False, "schedule_type": "interval", "interval_minutes": 5}
    now = datetime.now(timezone.utc)
    assert scheduler._is_due(task, now) is False


def test_is_due_interval_first_run():
    task = {"enabled": True, "schedule_type": "interval", "interval_minutes": 30}
    now = datetime.now(timezone.utc)
    assert scheduler._is_due(task, now) is True


def test_is_due_interval_too_soon():
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    task = {
        "enabled": True,
        "schedule_type": "interval",
        "interval_minutes": 30,
        "last_run": "2026-04-30T11:50:00+00:00",
    }
    assert scheduler._is_due(task, now) is False


def test_is_due_once_in_past():
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    task = {
        "enabled": True,
        "schedule_type": "once",
        "run_at": "2026-04-30T11:00:00+00:00",
    }
    assert scheduler._is_due(task, now) is True


def test_is_due_once_already_ran():
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    task = {
        "enabled": True,
        "schedule_type": "once",
        "run_at": "2026-04-30T11:00:00+00:00",
        "last_run": "2026-04-30T11:00:00+00:00",
    }
    assert scheduler._is_due(task, now) is False


# ── load/write JSON, _record_execution ──────────────────────────────────────


def test_load_json_missing(tmp_path):
    assert scheduler.load_json(tmp_path / "missing.json") == []
    assert scheduler.load_json(tmp_path / "missing.json", default={"x": 1}) == {"x": 1}


def test_write_atomic_success(tmp_path):
    target = tmp_path / "out.json"
    scheduler.write_atomic(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}


def test_record_execution_appends():
    task = {}
    scheduler._record_execution(task, "injected", "note text")
    assert len(task["execution_history"]) == 1
    assert task["execution_history"][0]["status"] == "injected"


def test_record_execution_truncates_to_max():
    task = {
        "execution_history": [{"status": "x", "timestamp": "t", "note": ""}]
        * (scheduler.EXEC_HISTORY_MAX + 5)
    }
    scheduler._record_execution(task, "injected", "")
    assert len(task["execution_history"]) == scheduler.EXEC_HISTORY_MAX


# ── CRUD via patched paths ──────────────────────────────────────────────────


def test_load_tasks_invalid_returns_empty(patched):
    scheduler.TASKS_PATH.write_text("{not json")
    assert scheduler._load_tasks() == []


def test_load_tasks_filters_non_dict(patched):
    scheduler.TASKS_PATH.write_text(json.dumps([{"id": "a"}, "junk", 5, {"id": "b"}]))
    out = scheduler._load_tasks()
    assert len(out) == 2


def test_check_and_inject_no_tasks(patched, capsys):
    n = scheduler.check_and_inject()
    assert n == 0


def test_check_and_inject_due_task(patched):
    scheduler.TASKS_PATH.write_text(
        json.dumps(
            [
                {
                    "id": "t1",
                    "schedule_type": "interval",
                    "interval_minutes": 30,
                    "content": "do thing",
                    "enabled": True,
                    "type": "goal",
                }
            ]
        )
    )
    n = scheduler.check_and_inject()
    assert n == 1
    inbox = json.loads(scheduler.INBOX_PATH.read_text())
    assert len(inbox) == 1
    assert inbox[0]["task_id"] == "t1"


def test_remove_task(patched):
    scheduler.TASKS_PATH.write_text(
        json.dumps(
            [
                {"id": "t1", "schedule_type": "interval"},
                {"id": "t2", "schedule_type": "interval"},
            ]
        )
    )
    scheduler.remove_task("t1")
    tasks = scheduler._load_tasks()
    assert [t["id"] for t in tasks] == ["t2"]


def test_remove_task_not_found(patched):
    scheduler.TASKS_PATH.write_text(json.dumps([]))
    with pytest.raises(SystemExit):
        scheduler.remove_task("nope")


def test_toggle_task(patched):
    scheduler.TASKS_PATH.write_text(json.dumps([{"id": "t1", "enabled": True}]))
    scheduler.toggle_task("t1", False)
    tasks = scheduler._load_tasks()
    assert tasks[0]["enabled"] is False


def test_add_task(patched):
    scheduler.TASKS_PATH.write_text(json.dumps([]))
    scheduler.add_task("new", "do it", "interval", 30, task_type="goal", priority=2)
    tasks = scheduler._load_tasks()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "new"
    assert tasks[0]["interval_minutes"] == 30


def test_next_cron_fire_basic():
    after = datetime(2026, 4, 30, 8, 30, 0, tzinfo=timezone.utc)
    nxt = scheduler._next_cron_fire("0 9 * * *", after)
    assert nxt is not None
    assert nxt.hour == 9 and nxt.minute == 0


def test_next_interval_fire_no_last_run():
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    nxt = scheduler._next_interval_fire({"interval_minutes": 30}, now)
    assert nxt == now


def test_next_once_fire_already_ran():
    now = datetime.now(timezone.utc)
    assert scheduler._next_once_fire({"last_run": "x", "run_at": "x"}, now) is None
