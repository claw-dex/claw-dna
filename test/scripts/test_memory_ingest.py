"""Tests for scripts/memory_ingest.py — chunking helpers and arg parsing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import memory_ingest as mi


def test_should_compress_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(mi, "COMPRESSION_THRESHOLD_MB", 0)  # any size triggers
    p = tmp_path / "x.mv2"
    p.write_text("data")
    assert mi._should_compress(p) is True


def test_should_compress_missing(tmp_path):
    assert mi._should_compress(tmp_path / "absent.mv2") is False


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
        mi.parse_args(["script", "--mv2"])


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
    mv2 = tmp_path / "x.mv2"
    # No source files exist — gather returns []
    with pytest.raises(SystemExit):
        mi.build(str(mem), str(mv2), dry_run=True)
    err = capsys.readouterr().err
    assert "No chunks" in err


def test_build_dry_run_emits_json(tmp_path, monkeypatch, capsys):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "journal.json").write_text(
        json.dumps([{"cycle": 1, "summary": "ok work done", "actions": ["a"]}])
    )
    mv2 = tmp_path / "x.mv2"
    mi.build(str(mem), str(mv2), dry_run=True, json_mode=True, quiet=True)
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["total_chunks"] >= 1


def test_append_json_missing_mv2(tmp_path, capsys):
    mv2 = tmp_path / "no.mv2"
    with pytest.raises(SystemExit):
        mi.append_json(str(mv2), '{"cycle":1}')
    assert "not found" in capsys.readouterr().err


def test_append_json_bad_json(tmp_path, capsys):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("placeholder")
    with pytest.raises(SystemExit):
        mi.append_json(str(mv2), "{not json")
    assert "Invalid JSON" in capsys.readouterr().err


def test_append_text_short_text(tmp_path, capsys):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("placeholder")
    with pytest.raises(SystemExit):
        mi.append_text(str(mv2), "")
    assert "short" in capsys.readouterr().err.lower()


def test_append_file_missing_mv2(tmp_path, capsys):
    mv2 = tmp_path / "no.mv2"
    f = tmp_path / "doc.pdf"
    f.write_text("x")
    with pytest.raises(SystemExit):
        mi.append_file(str(mv2), str(f))


def test_append_file_unsupported_ext(tmp_path, capsys):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("p")
    f = tmp_path / "doc.xyz"
    f.write_text("x")
    with pytest.raises(SystemExit):
        mi.append_file(str(mv2), str(f))
    assert "Unsupported" in capsys.readouterr().err


def test_append_many_no_chunks_returns_zero(tmp_path):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("p")
    # memvid_sdk may be None on this runtime; without chunks, returns (0,0)
    if mi.memvid_sdk is None:
        # _require_sdk would exit; use empty chunks to avoid going further
        ok, fail = mi.append_many(str(mv2), [])
        assert (ok, fail) == (0, 0)


def test_append_inbox_message_missing_mv2(tmp_path):
    if mi.memvid_sdk is None:
        # _require_sdk exits before reaching the file check
        with pytest.raises(SystemExit):
            mi.append_inbox_message(str(tmp_path / "no.mv2"), {"content": "hi"})
    else:
        assert (
            mi.append_inbox_message(str(tmp_path / "no.mv2"), {"content": "hi"})
            is False
        )


# ---------------------------------------------------------------------------
# Build staging-file rebuild — verify the canonical .mv2 stays untouched
# during the build and is only swapped after seal() succeeds.
# ---------------------------------------------------------------------------


class _FakeMemvid:
    """Minimal memvid stand-in that writes a marker to its target path."""

    def __init__(self, path: str, marker: str = "fresh") -> None:
        self.path = Path(path)
        self.path.write_text(marker)
        self.put_many_calls: list = []
        self.sealed = False

    def put_many(self, requests, opts=None):
        self.put_many_calls.append((list(requests), dict(opts or {})))
        return [f"frame-{i}" for i in range(len(requests))]

    def seal(self) -> None:
        self.sealed = True


class _FakeSdk:
    """memvid_sdk stand-in that records every create() call."""

    def __init__(self, *, fail_put_many: bool = False) -> None:
        self.created: list[_FakeMemvid] = []
        self.fail_put_many = fail_put_many

    def create(self, path, **_kwargs):
        mem = _FakeMemvid(path)
        if self.fail_put_many:

            def _boom(_requests, opts=None):
                raise RuntimeError("simulated put_many failure")

            mem.put_many = _boom  # type: ignore[assignment]
        self.created.append(mem)
        return mem

    def use(self, *_a, **_kw):  # not exercised by build()
        raise AssertionError("use() should not be called during --build")


def _seed_memory(mem_dir: Path) -> None:
    mem_dir.mkdir()
    (mem_dir / "journal.json").write_text(
        json.dumps(
            [{"cycle_number": 1, "summary": "did the thing", "actions": ["did it"]}]
        )
    )


def test_build_writes_to_staging_and_swaps(tmp_path, monkeypatch):
    """Successful build: staging path is used, then swapped in atomically."""
    mem_dir = tmp_path / "memory"
    _seed_memory(mem_dir)
    mv2 = tmp_path / "long_term_memory.mv2"
    mv2.write_text("OLD INDEX")  # pretend a previous index exists

    fake = _FakeSdk()
    monkeypatch.setattr(mi, "memvid_sdk", fake)

    mi.build(str(mem_dir), str(mv2), quiet=True)

    # Exactly one create() call, and it targeted the staging path.
    assert len(fake.created) == 1
    assert fake.created[0].path == mv2.with_suffix(mv2.suffix + ".rebuild")
    assert fake.created[0].sealed is True
    assert fake.created[0].put_many_calls, "put_many was not invoked"

    # After swap: canonical holds the freshly-built content; previous
    # canonical is preserved as .backup; staging file is gone.
    assert mv2.exists()
    assert mv2.read_text() == "fresh"
    backup = mv2.with_suffix(mv2.suffix + ".backup")
    assert backup.exists() and backup.read_text() == "OLD INDEX"
    assert not mv2.with_suffix(mv2.suffix + ".rebuild").exists()


def test_build_first_run_no_backup(tmp_path, monkeypatch):
    """First build (no pre-existing canonical) — no backup is created."""
    mem_dir = tmp_path / "memory"
    _seed_memory(mem_dir)
    mv2 = tmp_path / "long_term_memory.mv2"  # does not exist

    fake = _FakeSdk()
    monkeypatch.setattr(mi, "memvid_sdk", fake)

    mi.build(str(mem_dir), str(mv2), quiet=True)

    assert mv2.exists() and mv2.read_text() == "fresh"
    assert not mv2.with_suffix(mv2.suffix + ".backup").exists()
    assert not mv2.with_suffix(mv2.suffix + ".rebuild").exists()


def test_build_swap_failure_leaves_canonical_untouched(tmp_path, monkeypatch):
    """If the rename swap fails, canonical is NOT replaced and staging is removed."""
    mem_dir = tmp_path / "memory"
    _seed_memory(mem_dir)
    mv2 = tmp_path / "long_term_memory.mv2"
    mv2.write_text("OLD INDEX")

    fake = _FakeSdk()
    monkeypatch.setattr(mi, "memvid_sdk", fake)

    def _replace_boom(_a, _b):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(mi.os, "replace", _replace_boom)

    with pytest.raises(OSError):
        mi.build(str(mem_dir), str(mv2), quiet=True)

    # Canonical .mv2 must be unchanged; staging must be cleaned up.
    assert mv2.read_text() == "OLD INDEX"
    assert not mv2.with_suffix(mv2.suffix + ".rebuild").exists()


def test_build_put_many_failure_leaves_canonical_untouched(tmp_path, monkeypatch):
    """If put_many raises, the swap is skipped and canonical stays intact."""
    mem_dir = tmp_path / "memory"
    _seed_memory(mem_dir)
    mv2 = tmp_path / "long_term_memory.mv2"
    mv2.write_text("OLD INDEX")

    fake = _FakeSdk(fail_put_many=True)
    monkeypatch.setattr(mi, "memvid_sdk", fake)

    with pytest.raises(RuntimeError, match="simulated put_many failure"):
        mi.build(str(mem_dir), str(mv2), quiet=True)

    # Canonical untouched; no backup created (swap never happened); staging gone.
    assert mv2.read_text() == "OLD INDEX"
    assert not mv2.with_suffix(mv2.suffix + ".backup").exists()
    assert not mv2.with_suffix(mv2.suffix + ".rebuild").exists()


def test_build_removes_stale_staging_before_start(tmp_path, monkeypatch):
    """A leftover .rebuild file from a prior crash is cleared at the start."""
    mem_dir = tmp_path / "memory"
    _seed_memory(mem_dir)
    mv2 = tmp_path / "long_term_memory.mv2"
    stale = mv2.with_suffix(mv2.suffix + ".rebuild")
    stale.write_text("STALE PARTIAL")

    fake = _FakeSdk()
    monkeypatch.setattr(mi, "memvid_sdk", fake)

    mi.build(str(mem_dir), str(mv2), quiet=True)

    # The stale partial was overwritten by a fresh build (then renamed away).
    assert mv2.exists() and mv2.read_text() == "fresh"
    assert not stale.exists()
