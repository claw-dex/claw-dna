"""Unit tests for scripts/metrics_db.py — the DuckDB metrics collector."""

from __future__ import annotations

import json

import pytest

import metrics_db as mdb

duckdb = pytest.importorskip("duckdb")


# ---------- fixtures ----------

CYCLES = [
    {
        "cycle_number": 1,
        "cycle_type": "evolve",
        "cycle_category": "reliability",
        "cycle_status": "completed",
        "start": "2026-08-12T10:00:00+00:00",
        "end": "2026-08-12T10:05:00+00:00",
        "duration_seconds": 300,
        "summary": "cycle one",
    },
    {
        "cycle_number": 2,
        "cycle_type": "goal",
        "cycle_category": "capability",
        "cycle_status": "completed",
        "start": "2026-08-13T11:00:00+00:00",
        "duration_seconds": 120,
        "summary": "cycle two",
        "actions": ["from cycles"],
    },
    {
        "cycle_number": 3,
        "cycle_type": "evolve",
        "cycle_category": "reliability",
        "cycle_status": "failed",
        "start": "2026-08-13T12:00:00+00:00",
        "duration_seconds": 60,
    },
    {
        "cycle_number": 4,
        "cycle_type": "goal",
        "cycle_status": "completed",
        "start": "2026-08-13T13:00:00+00:00",
        "duration_seconds": 90,
    },
]

JOURNAL = [
    {
        "cycle_number": 2,
        "summary": "journal summary for two",
        "actions": ["from journal"],
        "timestamp": "2026-08-13T11:05:00+00:00",
    }
]

GOALS = [
    {"content": "g1", "status": "completed", "created_at": "2026-08-12T09:00:00+00:00"},
    {"content": "g2", "status": "pending", "created_at": "2026-08-13T09:00:00+00:00"},
    {
        "content": "g3",
        "status": "failed",
        "created_at": "2026-08-11T09:00:00+00:00",
        "delegated_to": {"name": "bob"},
    },
]

STATE = {
    "cycle_number": 4,
    "agent_status": "idle",
    "last_heartbeat": "2026-08-13T13:05:00+00:00",
}


@pytest.fixture
def agent_dir(monkeypatch, tmp_path):
    """Redirect every metrics_db path onto a tmp_path sandbox."""
    memory = tmp_path / "memory"
    messages = tmp_path / "messages"
    workspace = tmp_path / "workspace"
    for d in (memory, messages, workspace):
        d.mkdir(parents=True)

    monkeypatch.setattr(mdb, "AGENT_DIR", tmp_path)
    monkeypatch.setattr(mdb, "MEMORY_DIR", memory)
    monkeypatch.setattr(mdb, "MESSAGES_DIR", messages)
    monkeypatch.setattr(mdb, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(mdb, "DB_PATH", memory / "metrics.duckdb")

    (memory / "cycles.json").write_text(json.dumps(CYCLES))
    (memory / "journal.json").write_text(json.dumps(JOURNAL))
    (memory / "goal.json").write_text(json.dumps(GOALS))
    (memory / "state.json").write_text(json.dumps(STATE))
    (memory / "server_errors.json").write_text("[]")
    (messages / "inbox.json").write_text("[]")
    return tmp_path


def _rows(path, sql):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _meta(path):
    return {k: json.loads(v) for k, v in _rows(path, "SELECT key, value FROM meta")}


def _stray_tmp_files():
    """Any leftover build temp files beside the database (pid-suffixed)."""
    db = mdb.db_path()
    return list(db.parent.glob(f"{db.name}.*.tmp"))


# ---------- build ----------


def test_build_creates_every_table(agent_dir):
    mdb.refresh(force=True)
    db = mdb.db_path()
    assert db.exists()
    for table in mdb.TABLES:
        # A missing table raises; a present one returns a count.
        assert _rows(db, f"SELECT count(*) FROM {table}")[0][0] >= 0


def test_raw_tables_hold_merged_sources(agent_dir):
    mdb.refresh(force=True)
    db = mdb.db_path()
    assert _rows(db, "SELECT count(*) FROM cycles")[0][0] == len(CYCLES)
    assert _rows(db, "SELECT count(*) FROM journal")[0][0] == len(JOURNAL)
    assert _rows(db, "SELECT count(*) FROM goals")[0][0] == len(GOALS)


def test_archive_is_merged_and_deduped(agent_dir):
    archive = [
        # cycle_number 1 is already active — the active copy must win.
        {"cycle_number": 1, "cycle_type": "bootstrap", "cycle_status": "completed"},
        {
            "cycle_number": 0,
            "cycle_type": "bootstrap",
            "cycle_status": "completed",
            "start": "2026-08-01T00:00:00+00:00",
            "duration_seconds": 10,
        },
    ]
    (agent_dir / "memory" / "cycles_archive.json").write_text(json.dumps(archive))
    mdb.refresh(force=True)
    db = mdb.db_path()
    assert _rows(db, "SELECT count(*) FROM cycles")[0][0] == len(CYCLES) + 1
    assert (
        _rows(db, "SELECT cycle_type FROM cycles WHERE cycle_number = 1")[0][0]
        == "evolve"
    )


# ---------- metric tables ----------


def test_metric_health(agent_dir):
    mdb.refresh(force=True)
    row = _rows(mdb.db_path(), "SELECT * FROM metric_health")[0]
    status, heartbeat, cycle_number, active_goals, errors_24h, tabs, inbox = row
    assert status == "idle"
    assert heartbeat == STATE["last_heartbeat"]
    assert cycle_number == 4
    assert active_goals == 1  # only g2 is pending
    assert errors_24h == 0
    assert json.loads(tabs) == []
    assert inbox == 0


def test_metric_health_counts_only_errors_within_24h(agent_dir):
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 8, 13, 13, 0, tzinfo=timezone.utc)
    (agent_dir / "memory" / "server_errors.json").write_text(
        json.dumps(
            [
                {
                    "timestamp": (now - timedelta(hours=1)).isoformat(),
                    "tab": "Overview",
                },
                {"timestamp": (now - timedelta(hours=48)).isoformat(), "tab": "System"},
                {"timestamp": "not-a-timestamp", "tab": "Broken"},
            ]
        )
    )
    mdb.build(mdb.db_path(), now=now)
    row = _rows(mdb.db_path(), "SELECT errors_24h, error_tabs FROM metric_health")[0]
    assert row[0] == 1
    assert json.loads(row[1]) == ["Overview"]


def test_metric_daily_rolls_up_per_day(agent_dir):
    mdb.refresh(force=True)
    rows = {
        r[0]: r
        for r in _rows(
            mdb.db_path(),
            "SELECT day, cycles_total, cycles_completed, cycles_failed, "
            "active_seconds, goal_cycles, evolve_cycles, category_count, "
            "categories, types FROM metric_daily",
        )
    }
    today = rows["2026-08-13"]
    assert today[1:8] == (3, 2, 1, 270.0, 2, 1, 2)
    assert json.loads(today[8]) == {"capability": 1, "reliability": 1}
    assert json.loads(today[9]) == {"goal": 2, "evolve": 1}

    yesterday = rows["2026-08-12"]
    assert yesterday[1:8] == (1, 1, 0, 300.0, 0, 1, 1)


def test_metric_timeline_prefers_journal_summary(agent_dir):
    mdb.refresh(force=True)
    rows = _rows(
        mdb.db_path(),
        "SELECT cycle_number, summary FROM metric_day_timeline ORDER BY cycle_number",
    )
    summaries = dict(rows)
    assert summaries[2] == "journal summary for two"  # journal wins
    assert summaries[1] == "cycle one"  # falls back to the cycle's own
    assert summaries[3] == ""  # neither present


def test_metric_goal_stats(agent_dir):
    mdb.refresh(force=True)
    row = _rows(mdb.db_path(), "SELECT * FROM metric_goal_stats")[0]
    (
        total,
        completed,
        failed,
        rate,
        avg_dur,
        deleg_total,
        deleg_awaiting,
        deleg_completed,
        deleg_failed,
        by_agent,
    ) = row
    assert (total, completed, failed) == (3, 1, 1)
    assert rate == pytest.approx(1 / 3)
    assert avg_dur == pytest.approx(105.0)  # goal cycles 2 (120s) and 4 (90s)
    assert (deleg_total, deleg_awaiting, deleg_completed, deleg_failed) == (1, 0, 0, 1)
    assert json.loads(by_agent) == {"bob": 1}


def test_metric_goal_cycle_durations(agent_dir):
    mdb.refresh(force=True)
    rows = _rows(
        mdb.db_path(),
        "SELECT cycle_number, duration_seconds FROM metric_goal_cycle_durations "
        "ORDER BY rank",
    )
    assert rows == [(2, 120.0), (4, 90.0)]


def test_metric_goal_cycle_durations_are_capped(agent_dir, monkeypatch):
    many = [
        {
            "cycle_number": i,
            "cycle_type": "goal",
            "cycle_status": "completed",
            "start": f"2026-08-13T{i // 60:02d}:{i % 60:02d}:00+00:00",
            "duration_seconds": i,
        }
        for i in range(1, 60)
    ]
    (agent_dir / "memory" / "cycles.json").write_text(json.dumps(many))
    mdb.refresh(force=True)
    rows = _rows(mdb.db_path(), "SELECT count(*) FROM metric_goal_cycle_durations")
    assert rows[0][0] == mdb.GOAL_DURATION_LIMIT


def test_metric_recent_goals_newest_first(agent_dir):
    mdb.refresh(force=True)
    rows = _rows(mdb.db_path(), "SELECT content FROM metric_recent_goals ORDER BY rank")
    assert [r[0] for r in rows] == ["g2", "g1", "g3"]


def test_metric_balance_without_weights(agent_dir):
    mdb.refresh(force=True)
    rows = {
        r[0]: r
        for r in _rows(
            mdb.db_path(),
            "SELECT category, all_time_count, recent10_count, score, is_suggested "
            "FROM metric_balance",
        )
    }
    assert set(rows) == set(mdb.ALL_CATEGORIES)
    assert rows["reliability"][1] == 2
    assert rows["capability"][1] == 0  # capability cycle 2 is a goal, not an evolve
    # Fallback suggestion: first category with the lowest count.
    assert rows["observability"][4] is True

    meta = _meta(mdb.db_path())
    assert meta["balance_total_evolve_cycles"] == 2
    assert meta["balance_has_weights"] is False
    assert meta["balance_suggestion"] == "observability"


def test_metric_balance_with_weights(agent_dir):
    (agent_dir / "memory" / "evolution_weights.json").write_text(
        json.dumps(
            {
                "suggestion": "efficiency",
                "weights": {
                    "efficiency": {"score": 12, "base_need": 5, "roi_bonus": 2},
                    "reliability": {"score": 3, "maturity_penalty": 1},
                },
                "goal_signals": [
                    {"goal": "ship it", "aligned_categories": ["capability"]}
                ],
            }
        )
    )
    mdb.refresh(force=True)
    rows = {
        r[0]: r
        for r in _rows(
            mdb.db_path(),
            "SELECT category, score, base_need, roi_bonus, maturity_penalty, "
            "is_suggested FROM metric_balance",
        )
    }
    assert rows["efficiency"][1:5] == (12.0, 5.0, 2.0, 0.0)
    assert rows["efficiency"][5] is True
    assert rows["reliability"][4] == 1.0

    meta = _meta(mdb.db_path())
    assert meta["balance_has_weights"] is True
    assert meta["balance_max_score"] == 12.0
    assert meta["balance_goal_signals"][0]["goal"] == "ship it"


def test_metric_velocity_is_chronological_with_averages(agent_dir):
    mdb.refresh(force=True)
    rows = _rows(
        mdb.db_path(),
        "SELECT cycle_number, cycle_type, duration_seconds FROM metric_velocity "
        "ORDER BY rank",
    )
    # Cycle 3 failed, so it is excluded; the rest run oldest → newest.
    assert rows == [(1, "evolve", 300.0), (2, "goal", 120.0), (4, "goal", 90.0)]

    meta = _meta(mdb.db_path())
    assert meta["velocity_count"] == 3
    assert meta["velocity_avg_all"] == 170
    assert meta["velocity_avg_evolve"] == 300
    assert meta["velocity_avg_goal"] == 105


def test_metric_velocity_is_capped(agent_dir):
    many = [
        {
            "cycle_number": i,
            "cycle_type": "evolve",
            "cycle_status": "completed",
            "start": f"2026-08-13T{i // 60:02d}:{i % 60:02d}:00+00:00",
            "duration_seconds": i,
        }
        for i in range(1, 60)
    ]
    (agent_dir / "memory" / "cycles.json").write_text(json.dumps(many))
    mdb.refresh(force=True)
    count = _rows(mdb.db_path(), "SELECT count(*) FROM metric_velocity")[0][0]
    assert count == mdb.VELOCITY_LIMIT


def test_metric_improvements_newest_first_with_journal_actions(agent_dir):
    mdb.refresh(force=True)
    rows = _rows(
        mdb.db_path(),
        "SELECT cycle_number, cycle_type, summary, actions FROM metric_improvements "
        "ORDER BY rank",
    )
    # Only completed evolve/goal cycles; cycle 3 failed.
    assert [r[0] for r in rows] == [4, 2, 1]
    by_cycle = {r[0]: r for r in rows}
    assert by_cycle[2][2] == "journal summary for two"
    assert json.loads(by_cycle[2][3]) == ["from journal"]  # journal beats cycles.json


def test_metric_suggestions_ranked(agent_dir):
    mdb.refresh(force=True)
    rows = _rows(
        mdb.db_path(),
        "SELECT priority, action FROM metric_suggestions ORDER BY rank",
    )
    assert rows[0][0] == "high"
    assert "pending goal" in rows[0][1]


def test_metric_suggestions_falls_back_to_evolve_when_no_high(agent_dir):
    (agent_dir / "memory" / "goal.json").write_text("[]")
    mdb.refresh(force=True)
    rows = _rows(
        mdb.db_path(),
        "SELECT priority, category, action FROM metric_suggestions ORDER BY rank",
    )
    assert rows[-1][0] == "low"
    assert rows[-1][1] == "evolve"
    assert "observability" in rows[-1][2]


def test_metric_memory_overview(agent_dir):
    (agent_dir / "memory" / "journal_archive.json").write_text(json.dumps([{}, {}]))
    (agent_dir / "memory" / "cycles_archive.json").write_text(json.dumps([{}]))
    (agent_dir / "messages" / "inbox_history.json").write_text(json.dumps([{}, {}, {}]))
    (agent_dir / "messages" / "outbox_history.json").write_text(json.dumps([{}]))
    mdb.refresh(force=True)
    row = _rows(mdb.db_path(), "SELECT * FROM metric_memory_overview")[0]
    (
        ltm_bytes,
        journal_active,
        journal_archived,
        cycles_active,
        cycles_archived,
        inbox_history,
        outbox_history,
        inbox_bytes,
        outbox_bytes,
    ) = row
    # Raw per-file counts, not the merged/deduped totals the other tables use.
    assert (journal_active, journal_archived) == (len(JOURNAL), 2)
    assert (cycles_active, cycles_archived) == (len(CYCLES), 1)
    assert (inbox_history, outbox_history) == (3, 1)
    assert inbox_bytes > 0 and outbox_bytes > 0
    assert ltm_bytes == 0  # no LanceDB store in the sandbox


def test_metric_memory_files_grades_size_and_age(agent_dir, monkeypatch):
    # Thresholds scaled down to the fixture sizes: cycles.json is ~0.7 KB,
    # server_errors.json is "[]".
    monkeypatch.setattr(mdb, "MEMORY_SIZE_WARN_KB", 0.3)
    monkeypatch.setattr(mdb, "MEMORY_SIZE_CRIT_KB", 0.5)
    mdb.refresh(force=True)
    rows = {
        r[0]: r
        for r in _rows(
            mdb.db_path(),
            "SELECT fname, size_kb, entry_count, entry_kind, health, "
            "size_health, age_health, age_exempt FROM metric_memory_files",
        )
    }
    # cycles.json is the biggest fixture; server_errors.json is nearly empty.
    assert rows["cycles.json"][4] == "crit"
    assert rows["cycles.json"][5] == "crit"
    assert rows["server_errors.json"][4] == "ok"
    # A list file reports entries, a dict file reports keys.
    assert rows["cycles.json"][3] == "entries"
    assert rows["state.json"][3] == "keys"
    assert rows["state.json"][2] == len(STATE)
    # Just-written fixtures are never stale.
    assert all(r[6] == "ok" for r in rows.values())

    meta = _meta(mdb.db_path())
    assert meta["memory_files_total"] == len(rows)
    assert meta["memory_files_crit"] >= 1
    assert meta["memory_files_ok"] + meta["memory_files_warn"] + meta[
        "memory_files_crit"
    ] == len(rows)


def test_metric_memory_files_grades_stale_files(agent_dir):
    """Age thresholds moved out of system_tab.py — pin them here."""
    import os
    import time

    now = time.time()
    # 30h old → warn; 100h old → crit; both against 24h/72h thresholds.
    (agent_dir / "memory" / "stale_warn.json").write_text("[]")
    (agent_dir / "memory" / "stale_crit.json").write_text("[]")
    os.utime(agent_dir / "memory" / "stale_warn.json", (now - 30 * 3600,) * 2)
    os.utime(agent_dir / "memory" / "stale_crit.json", (now - 100 * 3600,) * 2)

    mdb.refresh(force=True)
    rows = {
        r[0]: r
        for r in _rows(
            mdb.db_path(),
            "SELECT fname, age_health, health, size_health FROM metric_memory_files",
        )
    }
    assert rows["stale_warn.json"][1:] == ("warn", "warn", "ok")
    assert rows["stale_crit.json"][1:] == ("crit", "crit", "ok")
    assert rows["cycles.json"][1] == "ok"  # just written

    meta = _meta(mdb.db_path())
    assert meta["memory_files_warn"] >= 1
    assert meta["memory_files_crit"] >= 1


def test_metric_memory_files_marks_exempt_files(agent_dir):
    (agent_dir / "memory" / "bootstrap.json").write_text("{}")
    mdb.refresh(force=True)
    row = _rows(
        mdb.db_path(),
        "SELECT age_exempt FROM metric_memory_files WHERE fname = 'bootstrap.json'",
    )[0]
    assert row[0] is True


def test_exempt_files_never_grade_stale(agent_dir):
    import os
    import time

    path = agent_dir / "memory" / "bootstrap.json"
    path.write_text("{}")
    os.utime(path, (time.time() - 500 * 3600,) * 2)
    mdb.refresh(force=True)
    row = _rows(
        mdb.db_path(),
        "SELECT age_health, health FROM metric_memory_files "
        "WHERE fname = 'bootstrap.json'",
    )[0]
    assert row == ("ok", "ok")


def test_metric_memory_files_handles_unparseable_json(agent_dir):
    (agent_dir / "memory" / "broken.json").write_text("{not json")
    mdb.refresh(force=True)
    row = _rows(
        mdb.db_path(),
        "SELECT entry_count, entry_kind FROM metric_memory_files "
        "WHERE fname = 'broken.json'",
    )[0]
    assert row == (None, "parse err")


def test_metric_agent_errors(agent_dir):
    (agent_dir / "memory" / "agents.json").write_text(
        json.dumps(
            [
                {"name": "alice", "type": "internal"},
                {"name": "bob", "type": "external"},
                {"type": "internal"},  # no name — skipped
            ]
        )
    )
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    (agent_dir / "memory" / "server_errors.json").write_text(
        json.dumps(
            [
                # Matched by the "<name>:" context prefix.
                {
                    "timestamp": (now - timedelta(hours=1)).isoformat(),
                    "context": "alice: turn timeout",
                },
                # Matched by tab name, but outside the recent window.
                {"timestamp": (now - timedelta(hours=48)).isoformat(), "tab": "alice"},
                # Belongs to nobody.
                {"timestamp": now.isoformat(), "tab": "Overview"},
            ]
        )
    )
    mdb.build(mdb.db_path(), now=now, fingerprint=mdb.source_fingerprint(now))
    rows = {
        r[0]: r
        for r in _rows(
            mdb.db_path(),
            "SELECT agent, total, recent_count, last_error_ts FROM metric_agent_errors",
        )
    }
    assert set(rows) == {"alice", "bob"}
    assert rows["alice"][1] == 2
    assert rows["alice"][2] == 1
    assert rows["alice"][3] == (now - timedelta(hours=1)).isoformat()  # newest first
    assert rows["bob"][1:] == (0, 0, "")


def test_cycle_velocity_is_stored(agent_dir):
    mdb.refresh(force=True)
    # Cycles 1, 2, 4 completed; 3 failed but still has a start, so the
    # velocity window (which ignores status only for the span) uses the
    # completed ones: 3 cycles spanning 2026-08-12T10:00 → 2026-08-13T13:00.
    velocity = _meta(mdb.db_path())["cycle_velocity_per_hour"]
    assert velocity == pytest.approx(3 / 27, abs=0.05)


def test_cycle_velocity_is_none_below_two_cycles(agent_dir):
    (agent_dir / "memory" / "cycles.json").write_text(
        json.dumps([{"cycle_number": 1, "cycle_status": "completed", "start": "x"}])
    )
    mdb.refresh(force=True)
    assert _meta(mdb.db_path())["cycle_velocity_per_hour"] is None


def test_directory_walks_are_reused_between_builds(agent_dir, monkeypatch):
    """The two os.walk inputs must not re-run on every 30s daemon poll."""
    calls = {"workspace": 0, "ltm": 0}
    real_ws, real_ltm = mdb._workspace_mb, mdb._ltm_bytes

    def _count_workspace():
        calls["workspace"] += 1
        return real_ws()

    def _count_ltm():
        calls["ltm"] += 1
        return real_ltm()

    monkeypatch.setattr(mdb, "_workspace_mb", _count_workspace)
    monkeypatch.setattr(mdb, "_ltm_bytes", _count_ltm)
    mdb.refresh(force=True)
    mdb.refresh(force=True)
    mdb.refresh(force=True)
    assert calls == {"workspace": 1, "ltm": 1}
    assert mdb.stats()["meta"]["workspace_mb"] is not None


def test_directory_walks_expire_after_the_throttle_interval(agent_dir, monkeypatch):
    """The carried-forward measurement must age out, not stick forever."""
    from datetime import datetime, timedelta, timezone

    calls = {"workspace": 0}
    real_ws = mdb._workspace_mb

    def _count_workspace():
        calls["workspace"] += 1
        return real_ws()

    monkeypatch.setattr(mdb, "_workspace_mb", _count_workspace)

    t0 = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    mdb.build(mdb.db_path(), now=t0, fingerprint="a")
    assert calls["workspace"] == 1

    # Inside the window — reuse.
    inside = t0 + timedelta(minutes=mdb.WORKSPACE_WALK_INTERVAL_MINUTES - 5)
    mdb.build(mdb.db_path(), now=inside, fingerprint="b")
    assert calls["workspace"] == 1

    # Past the window — re-measure.
    outside = t0 + timedelta(minutes=mdb.WORKSPACE_WALK_INTERVAL_MINUTES + 1)
    mdb.build(mdb.db_path(), now=outside, fingerprint="c")
    assert calls["workspace"] == 2


# ---------- pluggable handlers ----------


def _fake_handler(name="fake", rows=None, tables=None, raises=None, schema=None):
    """A minimal in-memory handler module satisfying services/metrics/base."""
    import types

    from services.metrics.base import HandlerResult

    mod = types.ModuleType(f"fake_{name}")
    mod.NAME = name
    mod.TABLES = tables or [f"metric_{name}"]
    mod.SCHEMA = schema or [f"CREATE TABLE metric_{name} (a INTEGER, b VARCHAR)"]

    def collect(ctx):
        if raises:
            raise raises
        return HandlerResult(
            tables={mod.TABLES[0]: rows if rows is not None else [(1, "x")]},
            meta={"count": len(rows) if rows is not None else 1},
        )

    mod.collect = collect
    return mod


def test_handler_tables_and_meta_are_written(agent_dir, monkeypatch):
    handler = _fake_handler(rows=[(1, "x"), (2, "y")])
    monkeypatch.setattr(mdb, "_handlers", lambda: ([handler], []))
    mdb.refresh(force=True)

    assert _rows(mdb.db_path(), "SELECT * FROM metric_fake") == [(1, "x"), (2, "y")]
    # Handler meta is namespaced so two handlers can both publish a "count".
    assert _meta(mdb.db_path())["fake.count"] == 2
    status = _rows(
        mdb.db_path(), "SELECT name, ok, rows, error FROM metric_handler_status"
    )
    assert status == [("fake", True, 2, "")]


def test_a_raising_handler_is_isolated(agent_dir, monkeypatch):
    good = _fake_handler("good")
    bad = _fake_handler("bad", raises=RuntimeError("boom"))
    monkeypatch.setattr(mdb, "_handlers", lambda: ([good, bad], []))
    mdb.refresh(force=True)

    db = mdb.db_path()
    # Core metrics are unaffected...
    assert _rows(db, "SELECT count(*) FROM metric_health")[0][0] == 1
    # ...the healthy handler still wrote...
    assert _rows(db, "SELECT count(*) FROM metric_good")[0][0] == 1
    # ...the broken one's table exists but is empty (readers never 404)...
    assert _rows(db, "SELECT count(*) FROM metric_bad")[0][0] == 0
    # ...and the failure is recorded rather than silent.
    status = {
        r[0]: r for r in _rows(db, "SELECT name, ok, error FROM metric_handler_status")
    }
    assert status["good"][1] is True
    assert status["bad"][1] is False
    assert "RuntimeError: boom" in status["bad"][2]
    meta = _meta(db)
    assert meta["handlers_ok"] == 1 and meta["handlers_failed"] == 1


@pytest.mark.parametrize(
    "bad_rows",
    [
        pytest.param([("not an int", "x")], id="wrong-type"),
        pytest.param([(1,)], id="too-few-columns"),
        pytest.param([(1, "x", "extra")], id="too-many-columns"),
        pytest.param([({"a": 1}, "x")], id="unsupported-value"),
    ],
)
def test_a_handler_returning_bad_rows_cannot_destroy_the_core_tables(
    agent_dir, monkeypatch, bad_rows
):
    """Regression: a DuckDB runtime error aborts the WHOLE transaction.

    A ConversionException from a handler's insert used to poison the build's
    single transaction — every later statement failed, COMMIT degraded to a
    rollback, and the database came out with no tables at all. Handlers now
    each get their own transaction.
    """
    good = _fake_handler("good")
    bad = _fake_handler("bad", rows=bad_rows)
    monkeypatch.setattr(mdb, "_handlers", lambda: ([good, bad], []))
    mdb.refresh(force=True)

    db = mdb.db_path()
    # Core metrics survived intact.
    assert _rows(db, "SELECT count(*) FROM cycles")[0][0] == len(CYCLES)
    assert _rows(db, "SELECT count(*) FROM metric_health")[0][0] == 1
    assert _meta(db)["schema_version"] == mdb.SCHEMA_VERSION
    assert mdb.is_ready() is True
    # The healthy handler still wrote, the bad one wrote nothing.
    assert _rows(db, "SELECT count(*) FROM metric_good")[0][0] == 1
    assert _rows(db, "SELECT count(*) FROM metric_bad")[0][0] == 0
    status = {r[0]: r for r in _rows(db, "SELECT name, ok FROM metric_handler_status")}
    assert status["good"][1] is True
    assert status["bad"][1] is False


def test_a_handler_writing_an_undeclared_table_is_rejected(agent_dir, monkeypatch):
    import types

    from services.metrics.base import HandlerResult

    mod = types.ModuleType("fake_sneaky")
    mod.NAME = "sneaky"
    mod.TABLES = ["metric_sneaky"]
    mod.SCHEMA = ["CREATE TABLE metric_sneaky (a INTEGER)"]
    mod.collect = lambda ctx: HandlerResult(tables={"cycles": [(999,) * 11]})

    monkeypatch.setattr(mdb, "_handlers", lambda: ([mod], []))
    mdb.refresh(force=True)

    status = _rows(
        mdb.db_path(), "SELECT ok, error FROM metric_handler_status WHERE name='sneaky'"
    )[0]
    assert status[0] is False
    assert "undeclared table" in status[1]
    # The core table it tried to write is untouched.
    assert _rows(mdb.db_path(), "SELECT count(*) FROM cycles")[0][0] == len(CYCLES)


def test_a_handler_with_broken_schema_is_reported(agent_dir, monkeypatch):
    handler = _fake_handler("brokenddl", schema=["CREATE TABLE ((("])
    monkeypatch.setattr(mdb, "_handlers", lambda: ([handler], []))
    mdb.refresh(force=True)

    status = _rows(
        mdb.db_path(),
        "SELECT ok, error FROM metric_handler_status WHERE name='brokenddl'",
    )[0]
    assert status[0] is False
    assert "schema failed" in status[1]


def test_discovery_errors_are_recorded(agent_dir, monkeypatch):
    monkeypatch.setattr(mdb, "_handlers", lambda: ([], [("oops", "ImportError: nope")]))
    mdb.refresh(force=True)
    status = _rows(mdb.db_path(), "SELECT name, ok, error FROM metric_handler_status")
    assert status == [("oops", False, "ImportError: nope")]


def test_handlers_receive_previous_rows_for_carry_forward(agent_dir, monkeypatch):
    seen = {}

    import types

    from services.metrics.base import HandlerResult

    mod = types.ModuleType("fake_carry")
    mod.NAME = "carry"
    mod.TABLES = ["metric_carry"]
    mod.SCHEMA = ["CREATE TABLE metric_carry (n INTEGER)"]

    def collect(ctx):
        seen["previous"] = ctx.previous_rows("metric_carry")
        seen["now"] = ctx.now
        seen["sources"] = sorted(ctx.sources)
        return HandlerResult(tables={"metric_carry": [(len(seen["previous"]) + 1,)]})

    mod.collect = collect
    monkeypatch.setattr(mdb, "_handlers", lambda: ([mod], []))

    mdb.refresh(force=True)
    assert seen["previous"] == []
    assert "cycles" in seen["sources"]  # core payloads are handed through
    assert seen["now"] is not None

    mdb.refresh(force=True)
    assert seen["previous"] == [(1,)]
    assert _rows(mdb.db_path(), "SELECT n FROM metric_carry") == [(2,)]


def test_handler_fingerprint_participates_in_the_refresh_check(agent_dir, monkeypatch):
    state = {"value": "a"}
    handler = _fake_handler("fp")
    handler.fingerprint = lambda ctx: state["value"]
    monkeypatch.setattr(mdb, "_handlers", lambda: ([handler], []))

    mdb.refresh(force=True)
    assert mdb.refresh() is False  # nothing changed anywhere

    state["value"] = "b"  # only the handler's source moved
    assert mdb.refresh() is True


def test_a_handler_fingerprint_that_raises_forces_a_rebuild(agent_dir, monkeypatch):
    handler = _fake_handler("fpboom")

    def _boom(ctx):
        raise RuntimeError("cannot stat")

    handler.fingerprint = _boom
    monkeypatch.setattr(mdb, "_handlers", lambda: ([handler], []))
    # Must not raise out of the fingerprint, and must not pin the store.
    assert mdb.refresh(force=True) is True
    assert "cannot stat" in mdb.source_fingerprint()


def test_all_tables_includes_handler_tables(agent_dir, monkeypatch):
    handler = _fake_handler("extra", tables=["metric_extra"])
    monkeypatch.setattr(mdb, "_handlers", lambda: ([handler], []))
    tables = mdb.all_tables()
    assert "metric_extra" in tables
    assert "metric_health" in tables  # core tables still present
    mdb.refresh(force=True)
    assert mdb.stats()["tables"]["metric_extra"] == 1


def test_the_real_usage_handler_is_discovered(agent_dir):
    handlers, errors = mdb._handlers()
    assert errors == []
    assert "usage" in [h.NAME for h in handlers]


def test_usage_handler_runs_end_to_end(agent_dir):
    """The shipped handler collects from transcripts into the store."""
    import json as _json

    transcripts = agent_dir / "memory" / "transcripts"
    transcripts.mkdir(parents=True)
    record = {
        "type": "assistant",
        "uuid": "u1",
        "requestId": "req_1",
        "timestamp": "2026-08-13T10:00:00Z",
        "effort": "medium",
        "message": {
            "id": "msg_1",
            "model": "claude-sonnet-5",
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 515,
                "cache_read_input_tokens": 104810,
                "output_tokens": 77,
                "service_tier": "standard",
                "speed": "standard",
            },
        },
    }
    # Two records, one API response — the store must count one request.
    (transcripts / "cycle-1.jsonl").write_text(
        _json.dumps(record) + "\n" + _json.dumps({**record, "uuid": "u2"}) + "\n"
    )

    mdb.refresh(force=True)
    db = mdb.db_path()
    assert _rows(db, "SELECT count(*) FROM metric_usage_files")[0][0] == 1
    assert _rows(db, "SELECT requests FROM metric_usage_cycles")[0][0] == 1
    assert _rows(db, "SELECT output_tokens FROM metric_usage_daily")[0][0] == 77
    meta = _meta(db)
    assert meta["usage.requests"] == 1
    assert meta["usage.total_tokens"] == 2 + 515 + 104810 + 77
    assert _rows(db, "SELECT ok FROM metric_handler_status WHERE name='usage'")[0][0]


# ---------- refresh / fingerprint / atomicity ----------


def test_refresh_is_a_noop_when_sources_are_unchanged(agent_dir):
    assert mdb.refresh(force=True) is True
    assert mdb.refresh() is False


def test_refresh_rebuilds_when_a_source_changes(agent_dir):
    mdb.refresh(force=True)
    assert mdb.refresh() is False
    (agent_dir / "memory" / "goal.json").write_text(json.dumps(GOALS + GOALS))
    assert mdb.refresh() is True
    total = _rows(mdb.db_path(), "SELECT total FROM metric_goal_stats")[0][0]
    assert total == len(GOALS) * 2


def test_refresh_rebuilds_when_the_schema_version_moves(agent_dir, monkeypatch):
    mdb.refresh(force=True)
    monkeypatch.setattr(mdb, "SCHEMA_VERSION", mdb.SCHEMA_VERSION + 1)
    assert mdb.refresh() is True


def test_refresh_leaves_no_tmp_file_behind(agent_dir):
    mdb.refresh(force=True)
    assert not _stray_tmp_files()


def test_failed_build_leaves_the_previous_database_intact(agent_dir, monkeypatch):
    mdb.refresh(force=True)
    before = mdb.db_path().read_bytes()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("build exploded")

    monkeypatch.setattr(mdb, "build", _boom)
    with pytest.raises(RuntimeError):
        mdb.refresh(force=True)

    assert mdb.db_path().read_bytes() == before
    assert not _stray_tmp_files()


def test_missing_sources_still_build(agent_dir):
    for name in ("cycles.json", "journal.json", "goal.json", "state.json"):
        (agent_dir / "memory" / name).unlink()
    mdb.refresh(force=True)
    assert _rows(mdb.db_path(), "SELECT count(*) FROM cycles")[0][0] == 0
    assert _rows(mdb.db_path(), "SELECT count(*) FROM metric_health")[0][0] == 1


def test_corrupt_source_is_treated_as_empty(agent_dir):
    (agent_dir / "memory" / "cycles.json").write_text("{not json")
    mdb.refresh(force=True)
    assert _rows(mdb.db_path(), "SELECT count(*) FROM cycles")[0][0] == 0


def test_non_dict_elements_are_dropped(agent_dir):
    """A hand-edited or half-written file must not take the collector down."""
    (agent_dir / "memory" / "goal.json").write_text('["oops", null, 7]')
    (agent_dir / "memory" / "server_errors.json").write_text('["nope", null]')
    (agent_dir / "memory" / "metrics.json").write_text('["nope"]')
    mdb.refresh(force=True)
    db = mdb.db_path()
    assert _rows(db, "SELECT count(*) FROM goals")[0][0] == 0
    assert _rows(db, "SELECT count(*) FROM errors")[0][0] == 0
    assert _rows(db, "SELECT count(*) FROM sys_snapshots")[0][0] == 0
    assert _rows(db, "SELECT total FROM metric_goal_stats")[0][0] == 0


def test_string_cycle_numbers_do_not_break_the_sort(agent_dir):
    (agent_dir / "memory" / "cycles.json").write_text(
        json.dumps(
            [
                {
                    "cycle_number": "3",
                    "cycle_type": "goal",
                    "cycle_status": "completed",
                },
                {"cycle_number": 1, "cycle_type": "goal", "cycle_status": "completed"},
            ]
        )
    )
    mdb.refresh(force=True)
    rows = _rows(mdb.db_path(), "SELECT cycle_number FROM cycles")
    assert [r[0] for r in rows] == [1, 3]


def test_null_and_non_numeric_durations_are_tolerated(agent_dir):
    (agent_dir / "memory" / "cycles.json").write_text(
        json.dumps(
            [
                {
                    "cycle_number": 1,
                    "cycle_type": "goal",
                    "cycle_status": "completed",
                    "start": "2026-08-13T10:00:00+00:00",
                    "duration_seconds": None,
                },
                {
                    "cycle_number": 2,
                    "cycle_type": "goal",
                    "cycle_status": "completed",
                    "start": "2026-08-13T11:00:00+00:00",
                    "duration_seconds": "120",
                },
                {
                    "cycle_number": 3,
                    "cycle_type": "goal",
                    "cycle_status": "completed",
                    "start": "2026-08-13T12:00:00+00:00",
                    "duration_seconds": 90,
                },
            ]
        )
    )
    mdb.refresh(force=True)
    db = mdb.db_path()
    # Only cycle 3 has a usable duration; the other two are excluded from the
    # duration-based metrics but still counted as cycles.
    assert _rows(db, "SELECT cycles_total, active_seconds FROM metric_daily")[0] == (
        3,
        90.0,
    )
    assert _rows(db, "SELECT avg_goal_duration_seconds FROM metric_goal_stats")[0][
        0
    ] == pytest.approx(90.0)
    assert [
        r[0]
        for r in _rows(db, "SELECT cycle_number FROM metric_velocity ORDER BY rank")
    ] == [3]


def test_fingerprint_is_stamped_before_sources_are_read(agent_dir, monkeypatch):
    """A write landing mid-build must not be marked as already collected."""
    mdb.refresh(force=True)
    assert mdb.refresh() is False

    real_load = mdb.load_sources

    def _load_then_write():
        data = real_load()
        # Simulate cycle_close writing a second file while the build is running.
        (agent_dir / "memory" / "goal.json").write_text(json.dumps(GOALS + GOALS))
        return data

    monkeypatch.setattr(mdb, "load_sources", _load_then_write)
    assert mdb.refresh(force=True) is True
    monkeypatch.setattr(mdb, "load_sources", real_load)

    # The mid-build write is not reflected in the database, so the next refresh
    # must still rebuild rather than trust a fingerprint taken after the read.
    assert mdb.refresh() is True
    assert (
        _rows(mdb.db_path(), "SELECT total FROM metric_goal_stats")[0][0]
        == len(GOALS) * 2
    )


def test_time_relative_metrics_expire_without_a_source_change(agent_dir):
    """errors_24h must decay even when no file changes (hour bucket in the key)."""
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    (agent_dir / "memory" / "server_errors.json").write_text(
        json.dumps([{"timestamp": (t0 - timedelta(hours=1)).isoformat(), "tab": "X"}])
    )
    mdb.build(mdb.db_path(), now=t0, fingerprint=mdb.source_fingerprint(t0))
    assert _rows(mdb.db_path(), "SELECT errors_24h FROM metric_health")[0][0] == 1

    # 30 hours later, with the sources untouched, the fingerprint must differ.
    later = t0 + timedelta(hours=30)
    assert mdb.source_fingerprint(later) != mdb.source_fingerprint(t0)
    mdb.build(mdb.db_path(), now=later, fingerprint=mdb.source_fingerprint(later))
    assert _rows(mdb.db_path(), "SELECT errors_24h FROM metric_health")[0][0] == 0


# ---------- readiness / stats ----------


def test_is_ready(agent_dir):
    assert mdb.is_ready() is False
    mdb.refresh(force=True)
    assert mdb.is_ready() is True


def test_is_ready_false_for_a_stale_schema(agent_dir, monkeypatch):
    mdb.refresh(force=True)
    monkeypatch.setattr(mdb, "SCHEMA_VERSION", mdb.SCHEMA_VERSION + 1)
    assert mdb.is_ready() is False


def test_is_ready_false_for_an_empty_file(agent_dir):
    mdb.db_path().write_bytes(b"")
    assert mdb.is_ready() is False


def test_stats_reports_row_counts(agent_dir):
    assert mdb.stats()["exists"] is False
    mdb.refresh(force=True)
    data = mdb.stats()
    assert data["exists"] is True
    assert data["tables"]["cycles"] == len(CYCLES)
    assert data["meta"]["schema_version"] == mdb.SCHEMA_VERSION


def test_sys_snapshots_are_ingested(agent_dir):
    (agent_dir / "memory" / "metrics.json").write_text(
        json.dumps(
            [
                {
                    "ts": "2026-08-13T12:00:00+00:00",
                    "api": {"_stcore_health": {"ms": 12.5, "status": 200, "ok": True}},
                    "sys": {"mem_used_mb": 100.0, "load_1m": 0.5},
                }
            ]
        )
    )
    mdb.refresh(force=True)
    row = _rows(
        mdb.db_path(),
        "SELECT ts, portal_ms, portal_status, portal_ok, mem_used_mb, load_1m "
        "FROM sys_snapshots",
    )[0]
    assert row == ("2026-08-13T12:00:00+00:00", 12.5, 200, True, 100.0, 0.5)


def test_a_handler_declaring_a_core_table_is_rejected(agent_dir, monkeypatch):
    """Nothing in the DDL or the undeclared-table guard stops a handler naming
    a core table in TABLES — its rows would append straight into `cycles`."""
    from services.metrics import discover as real_discover

    handler = _fake_handler("greedy", tables=["cycles"])
    handler.SCHEMA = ["CREATE TABLE metric_greedy (a INTEGER)"]
    monkeypatch.setattr(
        "services.metrics.discover", lambda: ([handler], []), raising=False
    )
    handlers, errors = mdb._handlers()
    assert handlers == []
    assert "declares core table(s): cycles" in errors[0][1]

    mdb.refresh(force=True)
    # The core table is untouched and the rejection is visible.
    assert _rows(mdb.db_path(), "SELECT count(*) FROM cycles")[0][0] == len(CYCLES)
    status = _rows(
        mdb.db_path(), "SELECT ok, error FROM metric_handler_status WHERE name='greedy'"
    )[0]
    assert status[0] is False
    assert "core table" in status[1]
    monkeypatch.setattr("services.metrics.discover", real_discover, raising=False)


def test_handler_tables_are_carried_forward_in_one_connection(agent_dir, monkeypatch):
    """_previous_rows takes every table at once — one open, not one per table."""
    opens = {"n": 0}
    real_connect = duckdb.connect

    def _counting_connect(*args, **kwargs):
        opens["n"] += 1
        return real_connect(*args, **kwargs)

    handler = _fake_handler("multi", tables=["metric_a", "metric_b", "metric_c"])
    handler.SCHEMA = [
        "CREATE TABLE metric_a (x INTEGER)",
        "CREATE TABLE metric_b (x INTEGER)",
        "CREATE TABLE metric_c (x INTEGER)",
    ]
    handler.collect = lambda ctx: {"tables": {"metric_a": [(1,)]}, "meta": {}}
    monkeypatch.setattr(mdb, "_handlers", lambda: ([handler], []))
    mdb.refresh(force=True)

    monkeypatch.setattr(mdb.duckdb, "connect", _counting_connect)
    mdb._previous_rows(["metric_a", "metric_b", "metric_c"])
    assert opens["n"] == 1
