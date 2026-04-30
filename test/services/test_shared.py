"""Tests for services/shared.py — atomic JSON + file-locked queue helpers."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# atomic_write_json
# ---------------------------------------------------------------------------


def test_atomic_write_json_writes_json(tmp_path: Path, patch_shared_paths):
    target = tmp_path / "out.json"
    patch_shared_paths.atomic_write_json(target, {"a": 1, "b": [2, 3]}, indent=2)

    assert json.loads(target.read_text()) == {"a": 1, "b": [2, 3]}


def test_atomic_write_json_does_not_leave_tempfiles_on_success(
    tmp_path: Path, patch_shared_paths
):
    sub = tmp_path / "iso"
    sub.mkdir()
    target = sub / "out.json"
    patch_shared_paths.atomic_write_json(target, [1, 2, 3])

    siblings = sorted(p.name for p in sub.iterdir())
    assert siblings == ["out.json"]


def test_atomic_write_json_cleans_tempfile_on_serialization_error(
    tmp_path: Path, patch_shared_paths
):
    sub = tmp_path / "iso"
    sub.mkdir()
    target = sub / "out.json"

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        patch_shared_paths.atomic_write_json(target, Unserializable())

    # No leftover *.tmp file.
    assert list(sub.glob("*.tmp")) == []
    assert not target.exists()


# ---------------------------------------------------------------------------
# read_json_file
# ---------------------------------------------------------------------------


def test_read_json_file_returns_default_for_missing(tmp_path, patch_shared_paths):
    assert patch_shared_paths.read_json_file(tmp_path / "missing.json") == []
    assert patch_shared_paths.read_json_file(
        tmp_path / "missing.json", default={"x": 1}
    ) == {"x": 1}


def test_read_json_file_returns_default_for_empty(tmp_path, patch_shared_paths):
    p = tmp_path / "empty.json"
    p.write_text("   \n  ")
    assert patch_shared_paths.read_json_file(p, default=[]) == []


def test_read_json_file_returns_default_for_invalid_json(tmp_path, patch_shared_paths):
    p = tmp_path / "bad.json"
    p.write_text("{this is not json")
    assert patch_shared_paths.read_json_file(p, default=[]) == []


def test_read_json_file_returns_default_on_type_mismatch(tmp_path, patch_shared_paths):
    p = tmp_path / "wrong.json"
    p.write_text(json.dumps({"hello": "world"}))
    # Caller wants a list, file has a dict → return default.
    assert patch_shared_paths.read_json_file(p, default=[]) == []


def test_read_json_file_returns_parsed_value_for_matching_type(
    tmp_path, patch_shared_paths
):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps([1, 2, 3]))
    assert patch_shared_paths.read_json_file(p, default=[]) == [1, 2, 3]


# ---------------------------------------------------------------------------
# write_to_inbox / dedup
# ---------------------------------------------------------------------------


def test_write_to_inbox_appends_to_existing_list(tmp_path, patch_shared_paths):
    inbox = tmp_path / "inbox.json"
    inbox.write_text(json.dumps([{"type": "goal", "content": "first"}]))

    ok = patch_shared_paths.write_to_inbox(
        [{"type": "goal", "content": "second"}],
        inbox_file=inbox,
    )
    assert ok is True

    items = json.loads(inbox.read_text())
    assert [i["content"] for i in items] == ["first", "second"]


def test_write_to_inbox_dedup_skips_same_type_and_content(
    tmp_path, patch_shared_paths
):
    inbox = tmp_path / "inbox.json"
    patch_shared_paths.write_to_inbox(
        [{"type": "goal", "content": "Hello"}], inbox_file=inbox
    )
    patch_shared_paths.write_to_inbox(
        [
            {"type": "goal", "content": "  hello "},  # case+ws variant — duplicate
            {"type": "goal", "content": "different"},
        ],
        inbox_file=inbox,
    )
    items = json.loads(inbox.read_text())
    assert [i["content"] for i in items] == ["Hello", "different"]


def test_write_to_inbox_dedup_off_keeps_duplicates(tmp_path, patch_shared_paths):
    inbox = tmp_path / "inbox.json"
    patch_shared_paths.write_to_inbox(
        [{"type": "goal", "content": "x"}], inbox_file=inbox
    )
    patch_shared_paths.write_to_inbox(
        [{"type": "goal", "content": "x"}], inbox_file=inbox, dedup=False
    )
    items = json.loads(inbox.read_text())
    assert len(items) == 2


def test_write_to_inbox_stamps_received_at(tmp_path, patch_shared_paths):
    inbox = tmp_path / "inbox.json"
    patch_shared_paths.write_to_inbox(
        [{"type": "goal", "content": "x"}], inbox_file=inbox
    )
    item = json.loads(inbox.read_text())[0]
    assert "received_at" in item
    # Parses as ISO timestamp without raising.
    datetime.fromisoformat(item["received_at"])


def test_write_to_inbox_preserves_existing_received_at(tmp_path, patch_shared_paths):
    inbox = tmp_path / "inbox.json"
    patch_shared_paths.write_to_inbox(
        [{"type": "goal", "content": "x", "received_at": "2024-01-01T00:00:00+00:00"}],
        inbox_file=inbox,
    )
    item = json.loads(inbox.read_text())[0]
    assert item["received_at"] == "2024-01-01T00:00:00+00:00"


def test_write_to_inbox_empty_list_is_noop(tmp_path, patch_shared_paths):
    inbox = tmp_path / "inbox.json"
    assert patch_shared_paths.write_to_inbox([], inbox_file=inbox) is True
    assert not inbox.exists()


def test_write_to_inbox_recovers_from_corrupt_file(tmp_path, patch_shared_paths):
    inbox = tmp_path / "inbox.json"
    inbox.write_text("not json{")
    ok = patch_shared_paths.write_to_inbox(
        [{"type": "goal", "content": "x"}], inbox_file=inbox
    )
    assert ok is True
    items = json.loads(inbox.read_text())
    assert len(items) == 1


# ---------------------------------------------------------------------------
# write_to_outbox / read_outbox_locked
# ---------------------------------------------------------------------------


def test_write_to_outbox_appends_and_reads_back(tmp_path, patch_shared_paths):
    outbox = tmp_path / "outbox.json"
    patch_shared_paths.write_to_outbox(
        [{"type": "info", "subject": "s", "content": "c"}], outbox_file=outbox
    )
    patch_shared_paths.write_to_outbox(
        [{"type": "info", "subject": "s2", "content": "c2"}], outbox_file=outbox
    )
    assert (
        len(patch_shared_paths.read_outbox_locked(outbox_file=outbox)) == 2
    )


def test_read_outbox_locked_returns_empty_for_missing(tmp_path, patch_shared_paths):
    assert (
        patch_shared_paths.read_outbox_locked(
            outbox_file=tmp_path / "nope.json"
        )
        == []
    )


def test_read_outbox_locked_returns_empty_for_corrupt(tmp_path, patch_shared_paths):
    outbox = tmp_path / "outbox.json"
    outbox.write_text("nope{")
    assert (
        patch_shared_paths.read_outbox_locked(outbox_file=outbox) == []
    )


# ---------------------------------------------------------------------------
# locked_outbox_rw / locked_json_rw
# ---------------------------------------------------------------------------


def test_locked_outbox_rw_applies_callback(tmp_path, patch_shared_paths):
    outbox = tmp_path / "outbox.json"
    outbox.write_text(json.dumps([{"id": "a"}, {"id": "b"}]))

    patch_shared_paths.locked_outbox_rw(
        lambda items: [it for it in items if it["id"] != "a"],
        outbox_file=outbox,
    )
    assert json.loads(outbox.read_text()) == [{"id": "b"}]


def test_locked_outbox_rw_handles_callback_returning_none(
    tmp_path, patch_shared_paths
):
    outbox = tmp_path / "outbox.json"
    outbox.write_text(json.dumps([{"id": "a"}]))

    # None contract: write empty list, do not raise.
    ok = patch_shared_paths.locked_outbox_rw(lambda _items: None, outbox_file=outbox)
    assert ok is True
    assert json.loads(outbox.read_text()) == []


def test_locked_json_rw_preserves_data_when_callback_returns_none(
    tmp_path, patch_shared_paths
):
    target = tmp_path / "j.json"
    target.write_text(json.dumps({"k": "v"}))

    ok = patch_shared_paths.locked_json_rw(
        lambda _existing: None, json_file=target, default={}
    )
    assert ok is True
    # locked_json_rw preserves *existing* on None (different from outbox variant).
    assert json.loads(target.read_text()) == {"k": "v"}


def test_locked_json_rw_uses_default_when_file_missing(tmp_path, patch_shared_paths):
    target = tmp_path / "j.json"

    seen = []

    def _rw(existing):
        seen.append(dict(existing))  # snapshot before mutation
        existing["count"] = 1
        return existing

    patch_shared_paths.locked_json_rw(_rw, json_file=target, default={"count": 0})
    assert seen == [{"count": 0}]
    assert json.loads(target.read_text()) == {"count": 1}


# ---------------------------------------------------------------------------
# surface_error
# ---------------------------------------------------------------------------


def test_surface_error_appends_structured_entry(tmp_path, patch_shared_paths):
    err_file = tmp_path / "errors.json"
    patch_shared_paths.SERVER_ERRORS_FILE = err_file  # already redirected, but explicit

    import shared

    shared.SERVER_ERRORS_FILE = err_file

    shared.surface_error("svc", ValueError("boom"), context="test ctx")
    entries = json.loads(err_file.read_text())

    assert len(entries) == 1
    e = entries[0]
    assert e["tab"] == "svc"
    assert e["source_type"] == "service"
    assert e["error_type"] == "ValueError"
    assert e["context"] == "test ctx"
    datetime.fromisoformat(e["timestamp"])


def test_surface_error_trims_to_max(tmp_path, patch_shared_paths):
    import shared

    err_file = tmp_path / "errors.json"
    shared.SERVER_ERRORS_FILE = err_file

    for i in range(7):
        shared.surface_error("svc", f"e{i}", max_errors=3)

    entries = json.loads(err_file.read_text())
    assert len(entries) == 3
    assert [e["error"] for e in entries] == ["e4", "e5", "e6"]


def test_surface_error_swallows_exceptions(monkeypatch, patch_shared_paths):
    """surface_error must never raise — make locked_json_rw blow up."""
    import shared

    def _boom(*_a, **_kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(shared, "locked_json_rw", _boom)
    # No raise expected.
    shared.surface_error("svc", "oops")


# ---------------------------------------------------------------------------
# append_to_history
# ---------------------------------------------------------------------------


def test_append_to_history_caps_size(tmp_path, patch_shared_paths):
    hist = tmp_path / "h.json"
    patch_shared_paths.append_to_history(
        [{"i": i} for i in range(10)], hist, max_entries=4
    )
    items = json.loads(hist.read_text())
    assert items == [{"i": 6}, {"i": 7}, {"i": 8}, {"i": 9}]


def test_append_to_history_empty_is_noop(tmp_path, patch_shared_paths):
    hist = tmp_path / "h.json"
    patch_shared_paths.append_to_history([], hist)
    assert not hist.exists()


# ---------------------------------------------------------------------------
# _timed_flock
# ---------------------------------------------------------------------------


def test_timed_flock_times_out(tmp_path, patch_shared_paths):
    """Acquire a lock from a background thread, then assert the foreground
    times out within the requested window."""
    import shared

    lock_path = tmp_path / "x.lock"

    holder_acquired = threading.Event()
    holder_release = threading.Event()

    def _holder():
        with open(lock_path, "a+") as fh:
            shared._timed_flock(fh, timeout=5.0)
            holder_acquired.set()
            holder_release.wait(5.0)

    th = threading.Thread(target=_holder, daemon=True)
    th.start()
    assert holder_acquired.wait(2.0), "holder failed to acquire lock"

    try:
        with open(lock_path, "a+") as fh:
            with pytest.raises(TimeoutError):
                shared._timed_flock(fh, timeout=0.2)
    finally:
        holder_release.set()
        th.join(timeout=2.0)
