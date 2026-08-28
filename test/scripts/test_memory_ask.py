"""Tests for scripts/memory_ask.py."""

from __future__ import annotations

import json
import sys

import pytest

# These suites exercise a real LanceDB store. uv only resolves lancedb for
# the Linux container, so on a dev machine the dependency is simply absent.
pytest.importorskip("lancedb")

import memory_ask as ma
import memory_store as store


def test_parse_args_question_only():
    a = ma.parse_args(["script", "what is X?"])
    assert a["question"] == "what is X?"
    assert a["k"] == 20
    assert a["min_score"] == ma.DEFAULT_MIN_SCORE


def test_parse_args_overrides():
    a = ma.parse_args(
        [
            "script",
            "Q",
            "--k",
            "5",
            "--db",
            "/tmp/x.lancedb",
            "--context-only",
            "--json",
        ]
    )
    assert a["k"] == 5
    assert a["db"] == "/tmp/x.lancedb"
    assert a["context_only"] is True
    assert a["json_mode"] is True


def test_parse_args_help():
    a = ma.parse_args(["script", "--help"])
    assert a["help"] is True


def test_parse_args_bad_k(capsys):
    with pytest.raises(SystemExit):
        ma.parse_args(["script", "Q", "--k", "notanumber"])


def test_parse_args_min_score():
    a = ma.parse_args(["script", "Q", "--min-score", "0.25"])
    assert a["min_score"] == pytest.approx(0.25)


def test_parse_args_bad_min_score(capsys):
    with pytest.raises(SystemExit):
        ma.parse_args(["script", "Q", "--min-score", "nope"])


def test_parse_args_custom_system():
    a = ma.parse_args(["script", "Q", "--system", "be brief"])
    assert a["system_prompt"] == "be brief"


# ---------------------------------------------------------------------------
# Retrieval against a real store
# ---------------------------------------------------------------------------

_CHUNKS = [
    {
        "title": "Cycle 1: fixed the scheduler",
        "label": "evolve",
        "text": "Goal: repair the cron parser\nSummary: scheduler now handles */5",
        "tags": ["journal", "cycle:1"],
        "metadata": {"source": "journal", "cycle": "1", "date": "2026-01-01T10:00:00Z"},
    },
    {
        "title": "Inbox message: restart the portal",
        "label": "inbox",
        "text": "please restart the portal when you get a chance",
        "tags": ["inbox"],
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


def test_retrieve_context_missing_store(tmp_path, capsys, stub_embeddings):
    with pytest.raises(SystemExit):
        ma.retrieve_context(str(tmp_path / "no.lancedb"), "q", 5)
    assert "not found" in capsys.readouterr().err


def test_retrieve_context_returns_hits(populated_db, stub_embeddings):
    out = ma.retrieve_context(
        str(populated_db), "scheduler cron parser", 5, min_score=0
    )
    assert len(out["results"]) == 2
    assert out["total_hits"] == 2
    assert out["stats"]["retrieval_ms"] >= 0
    assert "scheduler" in out["context"].lower()
    # Context is assembled from the stored text verbatim, one block per hit.
    assert out["context"].count("---") == 1


def test_retrieve_context_min_score_prunes(populated_db, stub_embeddings):
    strict = ma.retrieve_context(
        str(populated_db), "scheduler cron parser", 5, min_score=1.0
    )
    assert len(strict["results"]) == 1
    assert strict["results"][0]["score"] == pytest.approx(1.0)


def test_retrieve_context_no_match_returns_empty(populated_db, stub_embeddings, mocker):
    mocker.patch.object(store, "search", return_value=[])
    out = ma.retrieve_context(str(populated_db), "q", 5)
    assert out["results"] == []
    assert out["context"] == ""


def test_retrieve_context_error_exits(populated_db, mocker, capsys, stub_embeddings):
    mocker.patch.object(store, "search", side_effect=RuntimeError("disk on fire"))
    with pytest.raises(SystemExit):
        ma.retrieve_context(str(populated_db), "q", 5)
    assert "memory search failed" in capsys.readouterr().err


def test_main_no_question(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["memory_ask.py"])
    with pytest.raises(SystemExit):
        ma.main()
    assert "QUESTION is required" in capsys.readouterr().err


def test_main_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["memory_ask.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        ma.main()
    assert exc.value.code == 0


def test_main_no_results_json(monkeypatch, mocker, capsys, tmp_path):
    db = tmp_path / "x.lancedb"
    monkeypatch.setattr(sys, "argv", ["memory_ask.py", "Q", "--db", str(db), "--json"])
    mocker.patch.object(
        ma,
        "retrieve_context",
        return_value={"results": [], "context": "", "total_hits": 0, "stats": {}},
    )
    with pytest.raises(SystemExit) as exc:
        ma.main()
    assert exc.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["context_hits"] == 0
    assert out["db"] == str(db)


def test_main_context_only_json(monkeypatch, mocker, capsys, tmp_path):
    db = tmp_path / "x.lancedb"
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_ask.py", "Q", "--db", str(db), "--context-only", "--json"],
    )
    mocker.patch.object(
        ma,
        "retrieve_context",
        return_value={
            "results": [{"title": "t", "score": 0.9}],
            "context": "ctx",
            "total_hits": 1,
            "stats": {"retrieval_ms": 5},
        },
    )
    with pytest.raises(SystemExit) as exc:
        ma.main()
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["context"] == "ctx"
    assert data["context_hits"] == 1


def test_main_context_only_end_to_end(
    monkeypatch, populated_db, capsys, stub_embeddings
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory_ask.py",
            "restart the portal",
            "--db",
            str(populated_db),
            "--context-only",
            "--json",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        ma.main()
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["context_hits"] >= 1
    assert "portal" in data["context"].lower()
