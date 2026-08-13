"""Unit tests for app.data.metrics — the read-only DuckDB accessors."""

from __future__ import annotations

import json

import pytest

from app.data import _cache as cache_mod
from app.data import metrics as metrics_mod

pytest.importorskip("duckdb")

# Import by bare name: pyproject puts `scripts/` directly on pythonpath, and the
# `from scripts import …` form would resolve to test/scripts/ during collection
# (that package shadows the repo's scripts/ until test/scripts/conftest.py runs).
import metrics_db as mdb  # noqa: E402 — needs the importorskip above

CYCLES = [
    {
        "cycle_number": 1,
        "cycle_type": "evolve",
        "cycle_category": "reliability",
        "cycle_status": "completed",
        "start": "2026-08-12T10:00:00+00:00",
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
    },
    {
        "cycle_number": 3,
        "cycle_type": "goal",
        "cycle_status": "completed",
        "start": "2026-08-13T13:00:00+00:00",
        "duration_seconds": 90,
        "summary": "cycle three",
    },
]

GOALS = [
    {"content": "g1", "status": "completed", "created_at": "2026-08-12T09:00:00+00:00"},
    {"content": "g2", "status": "pending", "created_at": "2026-08-13T09:00:00+00:00"},
]

STATE = {
    "cycle_number": 3,
    "agent_status": "running",
    "last_heartbeat": "2026-08-13T13:05:00+00:00",
}


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Point the collector at tmp_path and reset every portal-side cache."""
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
    (memory / "journal.json").write_text("[]")
    (memory / "goal.json").write_text(json.dumps(GOALS))
    (memory / "state.json").write_text(json.dumps(STATE))
    (memory / "server_errors.json").write_text("[]")
    (messages / "inbox.json").write_text(json.dumps([{"content": "hi"}]))

    # The connection cache and cold-start backoff deliberately survive
    # _cache_clear_all() in production, so reset them explicitly between tests.
    metrics_mod._close_conn()
    cache_mod._cache_clear_all()
    yield tmp_path
    metrics_mod._close_conn()
    cache_mod._cache_clear_all()


@pytest.fixture
def built(sandbox):
    mdb.refresh(force=True)
    cache_mod._cache_clear_all()
    return sandbox


# ---------- happy path ----------


def test_load_health(built):
    health = metrics_mod.load_health()
    assert health["agent_status"] == "running"
    assert health["cycle_number"] == 3
    assert health["active_goals"] == 1
    assert health["errors_24h"] == 0
    assert health["error_tabs"] == []
    assert health["inbox_count"] == 1


def test_load_day_glance(built):
    day = metrics_mod.load_day_glance("2026-08-13", "2026-08-12")
    assert day["day"] == "2026-08-13"
    assert day["cycles_completed"] == 2
    assert day["prev_completed"] == 1
    assert day["active_seconds"] == 210.0
    assert day["goal_cycles"] == 2
    assert day["categories"] == {"capability": 1}
    assert [t["cycle_number"] for t in day["timeline"]] == [2, 3]


def test_load_day_glance_unknown_day_is_empty(built):
    day = metrics_mod.load_day_glance("1999-01-01", "1999-01-02")
    assert day["cycles_total"] == 0
    assert day["categories"] == {}
    assert day["timeline"] == []


def test_load_goal_metrics(built):
    stats = metrics_mod.load_goal_metrics()
    assert stats["total"] == 2
    assert stats["completed"] == 1
    assert stats["completion_rate"] == pytest.approx(0.5)
    assert stats["goal_cycle_durations"] == [(2, 120.0), (3, 90.0)]
    assert [g["content"] for g in stats["recent_goals"]] == ["g2", "g1"]
    assert stats["delegated_by_agent"] == {}


def test_load_balance_metrics(built):
    balance = metrics_mod.load_balance_metrics()
    assert [r["category"] for r in balance["rows"]] == mdb.ALL_CATEGORIES
    assert balance["total_evolve_cycles"] == 1
    assert balance["has_weights"] is False
    assert balance["max_score"] == 1.0
    assert balance["suggestion"] == "observability"


def test_load_velocity_metrics(built):
    velocity = metrics_mod.load_velocity_metrics()
    assert [r["cycle_number"] for r in velocity["rows"]] == [1, 2, 3]
    assert velocity["count"] == 3
    assert velocity["avg_all"] == 170
    assert velocity["avg_goal"] == 105


def test_load_improvements_respects_the_limit(built):
    assert len(metrics_mod.load_improvements(10)) == 3
    assert len(metrics_mod.load_improvements(2)) == 2
    assert metrics_mod.load_improvements(0) == []
    # Newest first, with actions decoded from JSON.
    rows = metrics_mod.load_improvements(10)
    assert [r["cycle_number"] for r in rows] == [3, 2, 1]
    assert rows[0]["actions"] == []


def test_load_improvements_tolerates_a_bad_limit(built):
    assert len(metrics_mod.load_improvements("nope")) == 3


def test_load_suggestions(built):
    suggestions = metrics_mod.load_suggestions()
    assert suggestions
    assert suggestions[0]["priority"] == "high"
    assert {"rank", "priority", "category", "action", "reason"} <= set(suggestions[0])


def test_load_cycle_velocity_metric(built):
    # Cycles 1, 2, 3 all completed, spanning 2026-08-12T10:00 → 08-13T13:00.
    assert metrics_mod.load_cycle_velocity_metric() == pytest.approx(0.1, abs=0.05)


def test_load_workspace_mb(built):
    assert metrics_mod.load_workspace_mb() == 0.0


def test_load_memory_overview(built):
    overview = metrics_mod.load_memory_overview()
    assert overview["cycles_active"] == len(CYCLES)
    assert overview["cycles_archived"] == 0
    assert overview["journal_active"] == 0
    assert overview["ltm_bytes"] == 0


def test_load_memory_file_health(built):
    health = metrics_mod.load_memory_file_health()
    names = [r["fname"] for r in health["rows"]]
    assert "cycles.json" in names and "state.json" in names
    assert health["total"] == len(health["rows"])
    assert health["ok"] + health["warn"] + health["crit"] == health["total"]
    row = next(r for r in health["rows"] if r["fname"] == "cycles.json")
    # The grades the System tab colours its columns with.
    assert row["health"] in {"ok", "warn", "crit"}
    assert row["size_health"] in {"ok", "warn", "crit"}
    assert row["age_health"] in {"ok", "warn", "crit"}


def test_load_agent_error_metrics(sandbox):
    (sandbox / "memory" / "agents.json").write_text(
        json.dumps([{"name": "alice", "type": "internal"}])
    )
    (sandbox / "memory" / "server_errors.json").write_text(
        json.dumps([{"timestamp": "2026-08-13T12:00:00+00:00", "tab": "alice"}])
    )
    mdb.refresh(force=True)
    cache_mod._cache_clear_all()

    stats = metrics_mod.load_agent_error_metrics("alice")
    assert stats["total"] == 1
    assert stats["last_error_ts"] == "2026-08-13T12:00:00+00:00"
    assert stats["recent_hours"] == 24


def test_load_agent_error_metrics_for_an_unknown_agent(built):
    stats = metrics_mod.load_agent_error_metrics("nobody")
    assert stats["total"] == 0
    assert stats["recent_count"] == 0
    assert stats["last_error_ts"] == ""
    assert stats["recent_hours"] == 24


# ---------- caching / freshness ----------


def test_query_results_are_cached_against_the_database_mtime(built):
    first = metrics_mod.load_health()
    assert metrics_mod._QUERY_CACHE, "expected the query result to be cached"

    # A cached read must not touch DuckDB again — break the connection and
    # confirm the same answer still comes back.
    conn, _mtime = metrics_mod._CONN_CACHE["conn"]
    conn.close()
    assert metrics_mod.load_health() == first


def test_reads_stay_correct_while_the_collector_swaps_the_file(built):
    """Readers must never see defaults because a rebuild closed their connection."""
    import threading

    results = []
    errors = []
    stop = threading.Event()

    def _read():
        try:
            while not stop.is_set():
                results.append(metrics_mod.load_health())
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    readers = [threading.Thread(target=_read) for _ in range(3)]
    for t in readers:
        t.start()
    try:
        for _ in range(15):
            mdb.refresh(force=True)
    finally:
        stop.set()
        for t in readers:
            t.join(timeout=10)

    assert not errors
    assert results
    assert all(r["cycle_number"] == 3 for r in results), "a read returned defaults"


def test_a_rebuild_is_picked_up(built):
    assert metrics_mod.load_health()["cycle_number"] == 3
    state = built / "memory" / "state.json"
    state.write_text(json.dumps({**STATE, "cycle_number": 99}))
    mdb.refresh()
    assert metrics_mod.load_health()["cycle_number"] == 99


# ---------- cold start / failure modes ----------


def test_missing_database_is_built_on_first_read(sandbox):
    assert not mdb.db_path().exists()
    health = metrics_mod.load_health()
    assert mdb.db_path().exists()
    assert health["cycle_number"] == 3


def test_a_failing_build_is_attempted_only_once(sandbox, monkeypatch):
    calls = {"n": 0}

    def _boom(*_args, **_kwargs):
        calls["n"] += 1
        raise RuntimeError("nope")

    monkeypatch.setattr(mdb, "refresh", _boom)
    assert metrics_mod.load_health() == metrics_mod._HEALTH_DEFAULT
    metrics_mod.load_health()
    metrics_mod.load_health()
    assert calls["n"] == 1


def test_database_missing_the_newer_tables_yields_defaults(sandbox, monkeypatch):
    """A valid but older-schema database must not raise on the new loaders.

    This is the shape of an upgrade before the SCHEMA_VERSION rebuild lands.
    """
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(mdb.db_path()))
    con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
    con.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    con.close()
    monkeypatch.setattr(mdb, "refresh", lambda *a, **k: False)

    assert metrics_mod.load_memory_overview() == metrics_mod._MEMORY_OVERVIEW_DEFAULT
    assert metrics_mod.load_memory_file_health()["rows"] == []
    assert metrics_mod.load_memory_file_health()["available"] is False
    assert metrics_mod.load_agent_error_metrics("alice")["found"] is False
    assert metrics_mod.load_health() == metrics_mod._HEALTH_DEFAULT
    assert metrics_mod.load_cycle_velocity_metric() is None


def test_is_available_true_once_the_store_exists(built):
    assert metrics_mod.is_available() is True
    assert metrics_mod.built_at()


def test_is_available_false_when_the_build_cannot_run(sandbox, monkeypatch):
    monkeypatch.setattr(mdb, "refresh", lambda *a, **k: False)
    assert metrics_mod.is_available() is False
    assert metrics_mod.built_at() is None


def test_corrupt_database_yields_defaults(sandbox, monkeypatch):
    mdb.db_path().write_bytes(b"this is not a duckdb file")
    monkeypatch.setattr(mdb, "refresh", lambda *a, **k: False)

    assert metrics_mod.load_health() == metrics_mod._HEALTH_DEFAULT
    assert metrics_mod.load_suggestions() == []
    assert metrics_mod.load_improvements() == []
    assert metrics_mod.load_velocity_metrics()["rows"] == []
    assert metrics_mod.load_balance_metrics()["rows"] == []
    assert metrics_mod.load_goal_metrics()["total"] == 0
    assert metrics_mod.load_day_glance("2026-08-13", "2026-08-12")["cycles_total"] == 0
    assert metrics_mod.load_memory_overview() == metrics_mod._MEMORY_OVERVIEW_DEFAULT
    assert metrics_mod.load_memory_file_health()["rows"] == []
    assert metrics_mod.load_agent_error_metrics("alice")["total"] == 0
    assert metrics_mod.load_cycle_velocity_metric() is None
    assert metrics_mod.load_workspace_mb() is None
    assert metrics_mod.is_available() is False
