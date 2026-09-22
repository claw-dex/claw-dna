"""Tests for scripts/reminder.py."""

from __future__ import annotations

import json

import pytest

import reminder
import scheduler


@pytest.fixture
def patched(monkeypatch, agent_root):
    tasks = agent_root / "memory" / "scheduled_tasks.json"
    inbox = agent_root / "messages" / "inbox.json"
    monkeypatch.setattr(scheduler, "TASKS_PATH", tasks)
    monkeypatch.setattr(scheduler, "INBOX_PATH", inbox)
    monkeypatch.setattr(reminder, "TASKS_PATH", tasks)
    return agent_root


def test_parse_duration_minutes():
    assert reminder._parse_duration_to_minutes("30m") == 30


def test_parse_duration_hours():
    assert reminder._parse_duration_to_minutes("2h") == 120


def test_parse_duration_days():
    assert reminder._parse_duration_to_minutes("1d") == 1440


def test_parse_duration_bare_int():
    assert reminder._parse_duration_to_minutes("45") == 45


def test_parse_duration_empty():
    with pytest.raises(ValueError):
        reminder._parse_duration_to_minutes("")


def test_parse_duration_negative():
    with pytest.raises(ValueError):
        reminder._parse_duration_to_minutes("-5m")


def test_parse_duration_invalid():
    with pytest.raises(ValueError):
        reminder._parse_duration_to_minutes("abc")


def test_resolve_in_to_isoformat():
    iso = reminder._resolve_in_to_isoformat("30m")
    # Should be parseable as ISO format
    from datetime import datetime

    dt = datetime.fromisoformat(iso)
    assert dt is not None


def test_generate_id_format():
    rid = reminder.generate_id("hello")
    assert rid.startswith("reminder-")
    assert len(rid) == len("reminder-") + 8


def test_parse_datetime_with_tz():
    iso = reminder.parse_datetime("2026-04-30T15:00+00:00")
    assert "2026-04-30" in iso


def test_parse_datetime_naive_gets_tz():
    iso = reminder.parse_datetime("2026-04-30T15:00")
    # Should add tz info
    assert "+" in iso or "Z" in iso or "-" in iso[10:]


def test_cmd_add_missing_text(patched, capsys):
    rc = reminder.cmd_add(["--at", "2026-04-30T15:00"])
    assert rc == 1


def test_cmd_add_missing_schedule(patched):
    rc = reminder.cmd_add(["--text", "hello"])
    assert rc == 1


def test_cmd_add_too_many_schedules(patched):
    rc = reminder.cmd_add(["--text", "hi", "--at", "2026-04-30T15:00", "--every", "30"])
    assert rc == 1


def test_cmd_add_at(patched, capsys):
    scheduler.TASKS_PATH.write_text(json.dumps([]))
    rc = reminder.cmd_add(["--text", "Call dentist", "--at", "2026-04-30T15:00"])
    assert rc == 0
    tasks = json.loads(scheduler.TASKS_PATH.read_text())
    assert len(tasks) == 1
    assert tasks[0]["source"] == "reminder"
    assert tasks[0]["schedule_type"] == "once"


def test_cmd_add_every(patched):
    scheduler.TASKS_PATH.write_text(json.dumps([]))
    rc = reminder.cmd_add(["--text", "Stretch", "--every", "60"])
    assert rc == 0
    tasks = json.loads(scheduler.TASKS_PATH.read_text())
    assert tasks[0]["schedule_type"] == "interval"
    assert tasks[0]["interval_minutes"] == 60


def test_cmd_add_in(patched):
    scheduler.TASKS_PATH.write_text(json.dumps([]))
    rc = reminder.cmd_add(["--text", "Tea", "--in", "30m"])
    assert rc == 0
    tasks = json.loads(scheduler.TASKS_PATH.read_text())
    assert tasks[0]["schedule_type"] == "once"


def test_cmd_add_in_invalid(patched, capsys):
    rc = reminder.cmd_add(["--text", "x", "--in", "garbage"])
    assert rc == 1


def test_cmd_add_in_and_at_mutually_exclusive(patched):
    rc = reminder.cmd_add(["--text", "x", "--in", "30m", "--at", "2026-04-30T15:00"])
    assert rc == 1


def test_cmd_list_empty(patched, capsys):
    scheduler.TASKS_PATH.write_text(json.dumps([]))
    rc = reminder.cmd_list([])
    assert rc == 0
    assert "No reminders" in capsys.readouterr().out


def test_cmd_list_json(patched, capsys):
    scheduler.TASKS_PATH.write_text(
        json.dumps(
            [
                {
                    "id": "reminder-abc",
                    "source": "reminder",
                    "schedule_type": "interval",
                    "interval_minutes": 60,
                }
            ]
        )
    )
    rc = reminder.cmd_list(["--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1


def test_cmd_delete_missing_id(patched):
    rc = reminder.cmd_delete([])
    assert rc == 1


def test_cmd_delete_not_found(patched):
    scheduler.TASKS_PATH.write_text(json.dumps([]))
    rc = reminder.cmd_delete(["--id", "x"])
    assert rc == 1


def test_cmd_delete_success(patched):
    scheduler.TASKS_PATH.write_text(
        json.dumps([{"id": "reminder-abc", "source": "reminder"}, {"id": "other"}])
    )
    rc = reminder.cmd_delete(["--id", "reminder-abc"])
    assert rc == 0
    tasks = json.loads(scheduler.TASKS_PATH.read_text())
    assert all(t.get("id") != "reminder-abc" for t in tasks)


def test_cmd_clear(patched, capsys):
    scheduler.TASKS_PATH.write_text(
        json.dumps(
            [
                {"id": "reminder-1", "source": "reminder", "enabled": False},
                {"id": "reminder-2", "source": "reminder", "enabled": True},
                {"id": "other-task"},
            ]
        )
    )
    rc = reminder.cmd_clear([])
    assert rc == 0
    tasks = json.loads(scheduler.TASKS_PATH.read_text())
    ids = [t.get("id") for t in tasks]
    assert "reminder-1" not in ids
    assert "reminder-2" in ids
    assert "other-task" in ids


def test_main_no_args_prints_help(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["reminder.py"])
    rc = reminder.main()
    assert rc == 0


def test_main_unknown_cmd(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["reminder.py", "bogus"])
    rc = reminder.main()
    assert rc == 1


def test_add_reminder_api_success(patched):
    scheduler.TASKS_PATH.write_text(json.dumps([]))
    rid = reminder.add_reminder("test", every=60)
    assert rid is not None
    assert rid.startswith("reminder-")


def test_add_reminder_api_no_text(patched):
    scheduler.TASKS_PATH.write_text(json.dumps([]))
    assert reminder.add_reminder("", every=60) is None


def test_add_reminder_api_no_schedule(patched):
    scheduler.TASKS_PATH.write_text(json.dumps([]))
    assert reminder.add_reminder("text") is None
