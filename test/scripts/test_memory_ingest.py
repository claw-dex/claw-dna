"""Tests for scripts/memory_ingest.py — chunking helpers, arg parsing, and
the LanceDB rebuild/append paths."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# These suites exercise a real LanceDB store. uv only resolves lancedb for
# the Linux container, so on a dev machine the dependency is simply absent.
pytest.importorskip("lancedb")

import memory_ingest as mi
import memory_store as store


def test_load_json_missing(tmp_path):
    assert mi.load_json(tmp_path / "no.json") is None


def test_load_json_invalid(tmp_path, capsys):
    p = tmp_path / "x.json"
    p.write_text("{not")
    assert mi.load_json(p) is None


def test_parse_args_build():
    a = mi.parse_args(["script", "--build"])
    assert a["build"] is True


def test_parse_args_append_text():
    a = mi.parse_args(["script", "--append-text", "hello"])
    assert a["append_text"] == "hello"


def test_parse_args_tags_list():
    a = mi.parse_args(["script", "--tags", "t1", "t2", "--build"])
    assert a["tags"] == ["t1", "t2"]
    assert a["build"] is True


def test_parse_args_help():
    a = mi.parse_args(["script", "--help"])
    assert a["help"] is True


def test_parse_args_missing_value(capsys):
    with pytest.raises(SystemExit):
        mi.parse_args(["script", "--db"])


def test_compose_journal_text():
    text = mi.compose_journal_text(
        {
            "cycle_goal": "G",
            "summary": "S",
            "actions": ["a1", "a2"],
            "cycle_category": "explore",
        }
    )
    assert "Goal: G" in text
    assert "Summary: S" in text
    assert "Actions: a1; a2" in text
    assert "Category: explore" in text


def test_transform_journal_skips_short():
    chunks = mi.transform_journal(
        [{"cycle": 1}, "not a dict", {"cycle": 2, "summary": ""}]
    )
    # All are too short / non-dict
    assert chunks == []


def test_transform_journal_basic():
    chunks = mi.transform_journal(
        [
            {
                "cycle": 1,
                "summary": "S1",
                "type": "evolve",
                "status": "completed",
                "category": "core",
                "timestamp": "2026-04-01T00:00:00Z",
                "goal": "g1",
                "actions": ["do x"],
            }
        ]
    )
    assert len(chunks) == 1
    c = chunks[0]
    assert c["label"] == "evolve"
    assert "journal" in c["tags"]
    assert "type:evolve" in c["tags"]
    assert "cycle:1" in c["tags"]
    assert "date:2026-04-01" in c["tags"]


def test_transform_cycles_basic():
    chunks = mi.transform_cycles(
        [
            {
                "cycle": 5,
                "summary": "x",
                "type": "evolve",
                "status": "completed",
                "category": "ops",
                "start": "2026-04-01T00:00:00Z",
                "end": "2026-04-01T01:00:00Z",
                "duration_seconds": 3600,
            }
        ]
    )
    assert len(chunks) == 1
    assert "cycle" in chunks[0]["tags"]


def test_transform_cycles_skips_short():
    assert mi.transform_cycles([{"cycle": 1}]) == []


def test_transform_goals():
    chunks = mi.transform_goals(
        [{"content": "achieve X", "status": "pending", "id": "g1"}, {"content": ""}]
    )
    assert len(chunks) == 1
    assert chunks[0]["label"] == "goal"
    assert "id:g1" in chunks[0]["tags"]


def test_transform_inbox_entry_skip_short():
    assert mi.transform_inbox_entry({"content": "x"}) is None
    assert mi.transform_inbox_entry("not a dict") is None


def test_transform_inbox_entry_basic():
    c = mi.transform_inbox_entry(
        {
            "content": "Hello world from user",
            "type": "user",
            "source": "telegram",
            "timestamp": "2026-04-01T00:00:00Z",
            "id": "msg1",
        }
    )
    assert c is not None
    assert c["label"] == "inbox"
    assert "inbox_source:telegram" in c["tags"]
    assert "id:msg1" in c["tags"]


def test_transform_inbox_iterates():
    chunks = mi.transform_inbox(
        [
            {
                "content": "valid message here",
                "type": "user",
                "timestamp": "2026-01-01",
            },
            {"content": "x"},  # too short
        ]
    )
    assert len(chunks) == 1


def test_gather_all_chunks(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    msgs = tmp_path / "messages"
    msgs.mkdir()
    (mem / "journal.json").write_text(
        json.dumps([{"cycle": 1, "summary": "ok work done here", "actions": ["a"]}])
    )
    (mem / "journal_archive.json").write_text(
        json.dumps(
            [{"cycle": 0, "summary": "older entry text content", "actions": ["b"]}]
        )
    )
    # cycles.json is intentionally written but ignored — gather_all_chunks
    # no longer ingests cycle records.
    (mem / "cycles.json").write_text(
        json.dumps(
            [{"cycle": 1, "summary": "ok work done here", "start": "2026-01-01"}]
        )
    )
    (msgs / "inbox_history.json").write_text(
        json.dumps([{"content": "an inbox message body", "type": "user"}])
    )
    chunks = mi.gather_all_chunks(mem)
    sources = {c["metadata"]["source"] for c in chunks}
    assert "journal" in sources
    assert "inbox" in sources
    assert "cycle" not in sources


def test_detect_and_transform_journal_route():
    chunk = mi._detect_and_transform(
        {"cycle": 1, "summary": "ok cycle work", "actions": ["a"]}
    )
    assert chunk is not None and chunk["metadata"]["source"] == "journal"


def test_detect_and_transform_cycle_route():
    chunk = mi._detect_and_transform(
        {"cycle": 1, "summary": "x", "start": "2026-01-01", "type": "evolve"}
    )
    assert chunk is not None and chunk["metadata"]["source"] == "cycle"


def test_detect_and_transform_goal_route():
    chunk = mi._detect_and_transform({"content": "do this thing", "status": "pending"})
    assert chunk is not None and chunk["metadata"]["source"] == "goal"


def test_main_no_args_exits(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["memory_ingest.py"])
    with pytest.raises(SystemExit):
        mi.main()
    err = capsys.readouterr().err
    assert "ERROR" in err


def test_main_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["memory_ingest.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        mi.main()
    assert exc.value.code == 0


def test_build_no_chunks_exits(tmp_path, monkeypatch, capsys):
    mem = tmp_path / "memory"
    mem.mkdir()
    db = tmp_path / "x.lancedb"
    # No source files exist — gather returns []
    with pytest.raises(SystemExit):
        mi.build(str(mem), str(db), dry_run=True)
    err = capsys.readouterr().err
    assert "No chunks" in err


def test_build_dry_run_emits_json(tmp_path, monkeypatch, capsys):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "journal.json").write_text(
        json.dumps([{"cycle": 1, "summary": "ok work done", "actions": ["a"]}])
    )
    db = tmp_path / "x.lancedb"
    mi.build(str(mem), str(db), dry_run=True, json_mode=True, quiet=True)
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["total_chunks"] >= 1


# ---------------------------------------------------------------------------
# Write paths — real LanceDB round-trips against tmp_path, with embeddings
# stubbed so no test ever downloads a model.
# ---------------------------------------------------------------------------


def _seed_memory(mem_dir: Path) -> None:
    mem_dir.mkdir()
    (mem_dir / "journal.json").write_text(
        json.dumps(
            [
                {
                    "cycle_number": 1,
                    "summary": "did the thing",
                    "actions": ["did it"],
                    "timestamp": "2026-01-01T00:00:00Z",
                }
            ]
        )
    )


@pytest.fixture
def built_db(tmp_path, stub_embeddings):
    """A store with one journal row already ingested."""
    mem_dir = tmp_path / "memory"
    _seed_memory(mem_dir)
    db = tmp_path / "long_term_memory.lancedb"
    mi.build(str(mem_dir), str(db), quiet=True)
    return db


def test_append_json_missing_db(tmp_path, capsys, stub_embeddings):
    db = tmp_path / "no.lancedb"
    with pytest.raises(SystemExit):
        mi.append_json(str(db), '{"cycle":1}')
    assert "not found" in capsys.readouterr().err


def test_append_json_bad_json(built_db, capsys, stub_embeddings):
    with pytest.raises(SystemExit):
        mi.append_json(str(built_db), "{not json")
    assert "Invalid JSON" in capsys.readouterr().err


def test_append_json_writes_row(built_db, stub_embeddings):
    before = store.open_table(built_db).count_rows()
    mi.append_json(
        str(built_db),
        json.dumps(
            {
                "cycle_number": 7,
                "summary": "appended entry",
                "actions": ["a"],
                "timestamp": "2026-02-01T00:00:00Z",
            }
        ),
        quiet=True,
    )
    assert store.open_table(built_db).count_rows() == before + 1


def test_append_text_short_text(built_db, capsys, stub_embeddings):
    with pytest.raises(SystemExit):
        mi.append_text(str(built_db), "")
    assert "short" in capsys.readouterr().err.lower()


def test_append_text_is_idempotent(built_db, stub_embeddings):
    mi.append_text(str(built_db), "a memorable fact worth keeping", quiet=True)
    after_first = store.open_table(built_db).count_rows()
    mi.append_text(str(built_db), "a memorable fact worth keeping", quiet=True)
    assert store.open_table(built_db).count_rows() == after_first


def test_append_file_missing_db(tmp_path, stub_embeddings):
    db = tmp_path / "no.lancedb"
    f = tmp_path / "doc.md"
    f.write_text("some content here")
    with pytest.raises(SystemExit):
        mi.append_file(str(db), str(f))


def test_append_file_unsupported_ext(built_db, capsys, stub_embeddings):
    f = Path(built_db).parent / "doc.pdf"
    f.write_text("x")
    with pytest.raises(SystemExit):
        mi.append_file(str(built_db), str(f))
    err = capsys.readouterr().err
    assert "Unsupported" in err
    assert "convert to text first" in err.lower()


def test_append_file_too_large(built_db, capsys, monkeypatch, stub_embeddings):
    monkeypatch.setattr(mi, "MAX_FILE_BYTES", 10)
    f = Path(built_db).parent / "big.md"
    f.write_text("x" * 100)
    with pytest.raises(SystemExit):
        mi.append_file(str(built_db), str(f))
    assert "too large" in capsys.readouterr().err.lower()


def test_append_file_reads_text(built_db, stub_embeddings):
    before = store.open_table(built_db).count_rows()
    f = Path(built_db).parent / "notes.md"
    f.write_text("# Notes\n\nThe deploy needs two approvals.")
    mi.append_file(str(built_db), str(f), quiet=True)
    tbl = store.open_table(built_db)
    assert tbl.count_rows() == before + 1
    rows = tbl.search().where("source = 'append-file'").limit(5).to_list()
    assert any("two approvals" in r["text"] for r in rows)


def test_append_many_no_chunks_returns_zero(built_db, stub_embeddings):
    assert mi.append_many(str(built_db), []) == (0, 0)


def test_append_many_missing_db_is_noop(tmp_path, stub_embeddings):
    chunk = mi.transform_inbox_entry({"content": "a message body", "type": "user"})
    assert mi.append_many(str(tmp_path / "no.lancedb"), [chunk]) == (0, 0)


def test_append_many_writes_and_dedupes(built_db, stub_embeddings):
    chunks = [
        mi.transform_inbox_entry(
            {"content": "first message body", "type": "user", "id": "m1"}
        ),
        mi.transform_inbox_entry(
            {"content": "second message body", "type": "user", "id": "m2"}
        ),
    ]
    before = store.open_table(built_db).count_rows()
    assert mi.append_many(str(built_db), chunks) == (2, 0)
    assert store.open_table(built_db).count_rows() == before + 2
    # Re-flushing the same batch must not duplicate.
    assert mi.append_many(str(built_db), chunks) == (2, 0)
    assert store.open_table(built_db).count_rows() == before + 2


def test_append_inbox_message_missing_db(tmp_path, stub_embeddings):
    assert (
        mi.append_inbox_message(str(tmp_path / "no.lancedb"), {"content": "hi there"})
        is False
    )


def test_append_inbox_message_skips_short(built_db, stub_embeddings):
    assert mi.append_inbox_message(str(built_db), {"content": "x"}) is False


def test_append_inbox_message_writes(built_db, stub_embeddings):
    before = store.open_table(built_db).count_rows()
    assert (
        mi.append_inbox_message(str(built_db), {"content": "a real inbox message"})
        is True
    )
    assert store.open_table(built_db).count_rows() == before + 1


# ---------------------------------------------------------------------------
# Build staging rebuild — the canonical store must stay untouched during the
# build and only be swapped after the staging store is fully written.
# ---------------------------------------------------------------------------


def test_build_writes_to_staging_and_swaps(tmp_path, stub_embeddings):
    """Successful build: staging is used, then swapped in atomically."""
    mem_dir = tmp_path / "memory"
    _seed_memory(mem_dir)
    db = tmp_path / "long_term_memory.lancedb"
    staging, backup = store.staging_paths(db)

    # Pretend a previous index exists.
    mi.build(str(mem_dir), str(db), quiet=True)
    first_rows = store.open_table(db).count_rows()

    # Add a second journal entry, then rebuild.
    (mem_dir / "journal.json").write_text(
        json.dumps(
            [
                {
                    "cycle_number": 1,
                    "summary": "did the thing",
                    "actions": ["did it"],
                    "timestamp": "2026-01-01T00:00:00Z",
                },
                {
                    "cycle_number": 2,
                    "summary": "did another thing",
                    "actions": ["did it again"],
                    "timestamp": "2026-01-02T00:00:00Z",
                },
            ]
        )
    )
    mi.build(str(mem_dir), str(db), quiet=True)

    assert store.open_table(db).count_rows() == first_rows + 1
    assert backup.is_dir(), "previous store should be preserved as .backup"
    assert not staging.exists(), "staging directory should be gone after the swap"


def test_build_first_run_no_backup(tmp_path, stub_embeddings):
    """First build (no pre-existing canonical) — no backup is created."""
    mem_dir = tmp_path / "memory"
    _seed_memory(mem_dir)
    db = tmp_path / "long_term_memory.lancedb"
    staging, backup = store.staging_paths(db)

    mi.build(str(mem_dir), str(db), quiet=True)

    assert store.open_table(db).count_rows() == 1
    assert not backup.exists()
    assert not staging.exists()


def test_build_creates_fts_index(tmp_path, stub_embeddings):
    mem_dir = tmp_path / "memory"
    _seed_memory(mem_dir)
    db = tmp_path / "long_term_memory.lancedb"
    mi.build(str(mem_dir), str(db), quiet=True)
    assert store.has_fts_index(store.open_table(db)) is True


def test_build_swap_failure_leaves_canonical_untouched(
    tmp_path, monkeypatch, stub_embeddings
):
    """If the swap fails, canonical is NOT replaced and staging is removed."""
    mem_dir = tmp_path / "memory"
    _seed_memory(mem_dir)
    db = tmp_path / "long_term_memory.lancedb"
    mi.build(str(mem_dir), str(db), quiet=True)
    staging, _backup = store.staging_paths(db)
    original_rows = store.open_table(db).count_rows()

    def _promote_boom(_path):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(store, "promote_staging", _promote_boom)

    with pytest.raises(OSError):
        mi.build(str(mem_dir), str(db), quiet=True)

    assert store.open_table(db).count_rows() == original_rows
    assert not staging.exists()


def test_build_write_failure_leaves_canonical_untouched(
    tmp_path, monkeypatch, stub_embeddings
):
    """If a batch write fails, the swap is skipped and canonical stays intact."""
    mem_dir = tmp_path / "memory"
    _seed_memory(mem_dir)
    db = tmp_path / "long_term_memory.lancedb"
    mi.build(str(mem_dir), str(db), quiet=True)
    staging, backup = store.staging_paths(db)
    original_rows = store.open_table(db).count_rows()

    monkeypatch.setattr(store, "add_chunks", lambda *a, **kw: (0, 1))

    with pytest.raises(RuntimeError, match="failed to write"):
        mi.build(str(mem_dir), str(db), quiet=True)

    assert store.open_table(db).count_rows() == original_rows
    assert not backup.exists()
    assert not staging.exists()


def test_build_removes_stale_staging_before_start(tmp_path, stub_embeddings):
    """A leftover .rebuild directory from a prior crash is cleared at the start."""
    mem_dir = tmp_path / "memory"
    _seed_memory(mem_dir)
    db = tmp_path / "long_term_memory.lancedb"
    staging, _backup = store.staging_paths(db)
    staging.mkdir(parents=True)
    (staging / "STALE_PARTIAL").write_text("junk")

    mi.build(str(mem_dir), str(db), quiet=True)

    assert store.open_table(db).count_rows() == 1
    assert not staging.exists()
