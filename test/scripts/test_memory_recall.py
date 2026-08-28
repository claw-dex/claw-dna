"""Tests for scripts/memory_recall.py."""

from __future__ import annotations

import json
import sys

import pytest

# These suites exercise a real LanceDB store. uv only resolves lancedb for
# the Linux container, so on a dev machine the dependency is simply absent.
pytest.importorskip("lancedb")

import memory_recall as mr
import memory_store as store


def test_parse_args_question():
    a = mr.parse_args(["script", "what?"])
    assert a["question"] == "what?"
    assert a["k"] == 5


def test_parse_args_options():
    a = mr.parse_args(
        ["script", "Q", "--k", "10", "--db", "/tmp/x.lancedb", "--json", "--timeline"]
    )
    assert a["k"] == 10
    assert a["db"] == "/tmp/x.lancedb"
    assert a["json_mode"] is True
    assert a["timeline"] is True


def test_parse_args_bad_k(capsys):
    with pytest.raises(SystemExit):
        mr.parse_args(["script", "Q", "--k", "x"])


def test_parse_args_min_score():
    a = mr.parse_args(["script", "Q", "--min-score", "0.4"])
    assert a["min_score"] == pytest.approx(0.4)


def test_parse_args_bad_min_score(capsys):
    with pytest.raises(SystemExit):
        mr.parse_args(["script", "Q", "--min-score", "x"])


def test_parse_args_help():
    a = mr.parse_args(["script", "--help"])
    assert a["help"] is True


def test_parse_args_since_until():
    a = mr.parse_args(["script", "Q", "--since", "2026-01-01", "--until", "2026-02-01"])
    assert a["since"] == "2026-01-01"
    assert a["until"] == "2026-02-01"


def test_parse_date_to_unix_int_string():
    assert mr._parse_date_to_unix("12345") == 12345


def test_parse_date_to_unix_iso():
    val = mr._parse_date_to_unix("2026-04-30T00:00:00")
    assert isinstance(val, int)
    assert val > 1_700_000_000


def test_parse_date_to_unix_invalid_strict(capsys):
    with pytest.raises(SystemExit):
        mr._parse_date_to_unix("garbage", strict=True)


def test_parse_date_to_unix_invalid_lenient():
    assert mr._parse_date_to_unix("garbage", strict=False) is None


def test_parse_date_to_unix_none():
    assert mr._parse_date_to_unix(None) is None


def test_result_to_dict():
    row = {
        "id": "abc",
        "score": 0.5,
        "title": "t",
        "text": "s",
        "tags": ["a"],
        "metadata": {"source": "journal"},
    }
    h = mr._result_to_dict(row, 1)
    assert h["rank"] == 1
    assert h["score"] == 0.5
    assert h["title"] == "t"
    assert h["snippet"] == "s"
    assert h["id"] == "abc"
    assert h["metadata"]["source"] == "journal"


def test_timeline_entry_extracts_cycle():
    entry = mr._timeline_entry(
        {
            "id": "abc",
            "ts": 1700000000,
            "date": "2026-01-01",
            "title": "t",
            "label": "evolve",
            "source": "journal",
            "tags": ["journal", "cycle:12"],
            "text": "body",
        }
    )
    assert entry["cycle"] == "12"
    assert entry["timestamp"] == 1700000000
    assert entry["preview"] == "body"


# ---------------------------------------------------------------------------
# Query paths against a real store
# ---------------------------------------------------------------------------

_CHUNKS = [
    {
        "title": "Cycle 1: fixed the scheduler",
        "label": "evolve",
        "text": "Goal: repair the cron parser\nSummary: scheduler now handles */5",
        "tags": ["journal", "cycle:1", "date:2026-01-01"],
        "metadata": {"source": "journal", "cycle": "1", "date": "2026-01-01T10:00:00Z"},
    },
    {
        "title": "Inbox message: restart the portal",
        "label": "inbox",
        "text": "please restart the portal when you get a chance",
        "tags": ["inbox", "date:2026-01-03"],
        "metadata": {"source": "inbox", "id": "m1", "date": "2026-01-03T09:00:00Z"},
    },
]


@pytest.fixture
def populated_db(tmp_path, stub_embeddings):
    db = tmp_path / "long_term_memory.lancedb"
    tbl = store.create_table(db)
    store.add_chunks(tbl, _CHUNKS)
    store.ensure_indexes(tbl)
    return db


def test_recall_no_store(monkeypatch, tmp_path, stub_embeddings):
    monkeypatch.setattr(mr, "DB_PATH", tmp_path / "missing.lancedb")
    assert mr.recall("q") == []


def test_recall_returns_items(monkeypatch, populated_db, stub_embeddings):
    monkeypatch.setattr(mr, "DB_PATH", populated_db)
    out = mr.recall("scheduler cron parser")
    assert out
    assert out[0]["rank"] == 1
    assert out[0]["score"] == pytest.approx(1.0)
    assert "scheduler" in out[0]["title"].lower()
    assert out[0]["id"]


def test_recall_swallows_errors(monkeypatch, populated_db, mocker, stub_embeddings):
    monkeypatch.setattr(mr, "DB_PATH", populated_db)
    mocker.patch.object(mr, "search_store", side_effect=RuntimeError("nope"))
    assert mr.recall("q") == []


def test_search_store_missing_raises(tmp_path, stub_embeddings):
    with pytest.raises(FileNotFoundError):
        mr.search_store(tmp_path / "no.lancedb", "q", 5)


def test_search_store_min_score_filters(populated_db, stub_embeddings):
    everything, _ = mr.search_store(populated_db, "portal restart scheduler", 5)
    filtered, _ = mr.search_store(
        populated_db, "portal restart scheduler", 5, min_score=1.0
    )
    assert len(filtered) < len(everything)
    assert all(item["score"] >= 1.0 for item in filtered)


def test_search_store_since_filters(populated_db, stub_embeddings):
    cutoff = store.to_unix("2026-01-02")
    items, _ = mr.search_store(populated_db, "portal scheduler", 5, since=cutoff)
    assert items
    assert all(item["metadata"].get("source") == "inbox" for item in items)


def test_search_falls_back_to_vector_without_fts(tmp_path, stub_embeddings):
    """A store built but never indexed still answers queries, vector-only."""
    db = tmp_path / "unindexed.lancedb"
    tbl = store.create_table(db)
    store.add_chunks(tbl, _CHUNKS)
    assert store.has_fts_index(tbl) is False
    items, _ = mr.search_store(db, "scheduler", 5)
    assert items


def test_main_no_question(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["memory_recall.py"])
    with pytest.raises(SystemExit):
        mr.main()


def test_main_help(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["memory_recall.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        mr.main()
    assert exc.value.code == 0


def test_main_missing_store(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mr, "DB_PATH", tmp_path / "no.lancedb")
    monkeypatch.setattr(sys, "argv", ["memory_recall.py", "what?"])
    with pytest.raises(SystemExit):
        mr.main()
    assert "not found" in capsys.readouterr().err


def test_main_query_json(monkeypatch, populated_db, capsys, stub_embeddings):
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_recall.py", "Q", "--db", str(populated_db), "--json"],
    )
    mr.main()
    data = json.loads(capsys.readouterr().out)
    assert data["query"] == "Q"
    assert data["total_hits"] == len(data["results"])


def test_main_timeline_json(monkeypatch, populated_db, capsys, stub_embeddings):
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_recall.py", "--timeline", "--db", str(populated_db), "--json"],
    )
    mr.main()
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "timeline"
    assert data["count"] == 2
    # Newest first.
    stamps = [e["timestamp"] for e in data["entries"]]
    assert stamps == sorted(stamps, reverse=True)


def test_recall_survives_missing_backend(monkeypatch, populated_db, stub_embeddings):
    """A missing lancedb/fastembed must yield [] rather than kill the caller.

    The dependency guards in memory_store call sys.exit, and recall() runs
    inside cycle_start's thread pool — an escaping SystemExit would abort the
    whole cycle-start briefing over an optional dependency.
    """
    monkeypatch.setattr(mr, "DB_PATH", populated_db)
    monkeypatch.setattr(mr, "search_store", lambda *a, **kw: sys.exit(1))
    assert mr.recall("q") == []


def test_recall_survives_missing_embedder(monkeypatch, populated_db, stub_embeddings):
    monkeypatch.setattr(mr, "DB_PATH", populated_db)

    def _exit(_text):
        sys.exit(1)

    monkeypatch.setattr(store, "embed_query", _exit)
    assert mr.recall("q") == []
