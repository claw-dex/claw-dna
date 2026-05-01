"""Unit tests for app.data.cycle."""

from __future__ import annotations

import json

import pytest

from app.data import _cache as cache_mod
from app.data import cycle as cycle_mod


@pytest.fixture
def patch_cycle_paths(monkeypatch, tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir(parents=True)
    logs = memory / "logs"
    logs.mkdir()
    history = memory / "command_history.json"

    monkeypatch.setattr("app.shared.MEMORY_DIR", str(memory))
    monkeypatch.setattr("app.shared.LOGS_DIR", str(logs))
    monkeypatch.setattr("app.shared.HISTORY_PATH", str(history))
    monkeypatch.setattr(cycle_mod, "MEMORY_DIR", str(memory))
    monkeypatch.setattr(cycle_mod, "LOGS_DIR", str(logs))
    monkeypatch.setattr(cycle_mod, "HISTORY_PATH", str(history))

    cache_mod._cache_clear_all()
    return {"memory": memory, "logs": logs, "history": history}


def _write(p, obj):
    p.write_text(json.dumps(obj))


# ---------- load_cycles ----------


def test_load_cycles_returns_list(patch_cycle_paths):
    _write(
        patch_cycle_paths["memory"] / "cycles.json",
        [{"cycle_number": 1, "cycle_status": "completed"}],
    )
    cache_mod._cache_clear_all()
    assert cycle_mod.load_cycles() == [{"cycle_number": 1, "cycle_status": "completed"}]


def test_load_cycles_missing_returns_empty(patch_cycle_paths):
    cache_mod._cache_clear_all()
    assert cycle_mod.load_cycles() == []


def test_load_cycles_non_list_normalised(patch_cycle_paths):
    _write(patch_cycle_paths["memory"] / "cycles.json", {"unexpected": "dict"})
    cache_mod._cache_clear_all()
    assert cycle_mod.load_cycles() == []


# ---------- load_cycle_velocity ----------


def test_velocity_none_when_too_few(patch_cycle_paths):
    _write(
        patch_cycle_paths["memory"] / "cycles.json",
        [{"cycle": 1, "status": "completed", "start": "2026-01-01T00:00:00+00:00"}],
    )
    cache_mod._cache_clear_all()
    assert cycle_mod.load_cycle_velocity() is None


def test_velocity_computed_for_recent(patch_cycle_paths):
    cycles = [
        {
            "cycle": i,
            "status": "completed",
            "start": f"2026-01-01T0{i}:00:00+00:00",
        }
        for i in range(2, 8)  # 6 cycles, 1h apart -> 5h span -> 6/5 = 1.2/hr
    ]
    _write(patch_cycle_paths["memory"] / "cycles.json", cycles)
    cache_mod._cache_clear_all()
    v = cycle_mod.load_cycle_velocity()
    assert v == 1.2


def test_velocity_handles_bad_timestamp(patch_cycle_paths):
    _write(
        patch_cycle_paths["memory"] / "cycles.json",
        [
            {"cycle": 1, "status": "completed", "start": "garbage"},
            {"cycle": 2, "status": "completed", "start": "also-bad"},
        ],
    )
    cache_mod._cache_clear_all()
    assert cycle_mod.load_cycle_velocity() is None


# ---------- load_cycle_logs ----------


def test_load_cycle_logs_sorted_newest_first(patch_cycle_paths):
    for n in (1, 5, 3, 10):
        (patch_cycle_paths["logs"] / f"cycle-{n}.log").write_text(f"log {n}")
    # noise files should be ignored
    (patch_cycle_paths["logs"] / "other.log").write_text("noise")
    (patch_cycle_paths["logs"] / "cycle-bad.log").write_text("noise")

    cache_mod._cache_clear_all()
    logs = cycle_mod.load_cycle_logs()
    nums = [e["cycle"] for e in logs]
    assert nums == [10, 5, 3, 1]
    for entry in logs:
        assert entry["size"] > 0
        assert "mtime" in entry


def test_load_cycle_logs_empty_dir(patch_cycle_paths):
    cache_mod._cache_clear_all()
    assert cycle_mod.load_cycle_logs() == []


# ---------- load_cycle_log_content ----------


def test_load_cycle_log_content_strips_json_warnings(patch_cycle_paths):
    log = patch_cycle_paths["logs"] / "cycle-7.log"
    log.write_text(
        '{"level":"warn","msg":"junk"}\n'
        "real line one\n"
        "real line two\n"
        '{"level":"info","msg":"more junk"}\n'
    )
    cache_mod._cache_clear_all()
    content = cycle_mod.load_cycle_log_content(7)
    assert "real line one" in content
    assert "real line two" in content
    assert "junk" not in content


def test_load_cycle_log_content_missing_returns_empty(patch_cycle_paths):
    cache_mod._cache_clear_all()
    assert cycle_mod.load_cycle_log_content(999) == ""


def test_load_cycle_log_content_keeps_brace_text_that_is_not_json(patch_cycle_paths):
    log = patch_cycle_paths["logs"] / "cycle-3.log"
    log.write_text("{this is not json}\nplain line\n")
    cache_mod._cache_clear_all()
    out = cycle_mod.load_cycle_log_content(3)
    assert "{this is not json}" in out
    assert "plain line" in out


# ---------- load_balance ----------


def test_load_balance_empty(patch_cycle_paths):
    cache_mod._cache_clear_all()
    res = cycle_mod.load_balance()
    assert res["total_evolve_cycles"] == 0
    assert res["all_time"] == {}
    assert res["recent_10"] == {}
    # Suggestion is the first all_cats fallback when nothing has been done.
    assert res["suggestion"] == "reliability"


def test_load_balance_counts_categories(patch_cycle_paths):
    cycles = [
        {"type": "evolve", "category": "reliability"},
        {"type": "evolve", "category": "reliability"},
        {"type": "evolve", "category": "capability"},
        {"type": "goal", "category": "ignored"},  # filtered
        {"type": "evolve"},  # missing category — filtered
    ]
    _write(patch_cycle_paths["memory"] / "cycles.json", cycles)
    cache_mod._cache_clear_all()
    res = cycle_mod.load_balance()
    assert res["all_time"] == {"reliability": 2, "capability": 1}
    assert res["total_evolve_cycles"] == 3
    # "observability" never appears -> tied for least-done -> suggested.
    assert res["suggestion"] == "observability"


def test_load_balance_uses_weights_suggestion(patch_cycle_paths):
    _write(patch_cycle_paths["memory"] / "cycles.json", [])
    _write(
        patch_cycle_paths["memory"] / "evolution_weights.json",
        {
            "weights": {"capability": 2.0},
            "suggestion": "capability",
            "goal_signals": ["sig"],
            "maturity_signals": {"x": 1},
        },
    )
    cache_mod._cache_clear_all()
    res = cycle_mod.load_balance()
    assert res["suggestion"] == "capability"
    assert res["weights"] == {"capability": 2.0}
    assert res["goal_signals"] == ["sig"]
    assert res["maturity_signals"] == {"x": 1}


# ---------- load_activity ----------


def test_load_activity_merges_history_and_cycles(patch_cycle_paths, monkeypatch):
    # Stub load_history at the import site (cycle.load_activity imports inline).
    fake_history = [
        {
            "timestamp": "2026-04-01T00:00:00+00:00",
            "type": "bash",
            "content": "ls",
            "result": "files",
        },
        {
            "timestamp": "2026-04-01T01:00:00+00:00",
            "type": "goal",
            "content": "build",
            "result": "ok",
        },
        {  # missing timestamp -> skipped
            "type": "message",
            "content": "noop",
        },
    ]
    import app.data.message as message_mod

    monkeypatch.setattr(message_mod, "load_history", lambda: fake_history)

    cycles = [
        {
            "cycle": 1,
            "start": "2026-04-02T00:00:00+00:00",
            "end": "2026-04-02T00:05:00+00:00",
            "duration_seconds": 300,
            "goal": "thing",
        }
    ]
    _write(patch_cycle_paths["memory"] / "cycles.json", cycles)
    cache_mod._cache_clear_all()

    events = cycle_mod.load_activity()
    types = [e["type"] for e in events]
    assert "cycle_start" in types
    assert "cycle_end" in types
    assert "goal" in types
    assert "bash_cmd" in types
    # Newest first
    times = [e["time"] for e in events]
    assert times == sorted(times, reverse=True)
    # Cap at 50
    assert len(events) <= 50


def test_load_activity_empty_when_no_data(patch_cycle_paths, monkeypatch):
    import app.data.message as message_mod

    monkeypatch.setattr(message_mod, "load_history", lambda: [])
    cache_mod._cache_clear_all()
    assert cycle_mod.load_activity() == []
