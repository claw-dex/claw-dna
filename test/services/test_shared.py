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


def test_write_to_inbox_dedup_skips_same_type_and_content(tmp_path, patch_shared_paths):
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
    assert len(patch_shared_paths.read_outbox_locked(outbox_file=outbox)) == 2


def test_read_outbox_locked_returns_empty_for_missing(tmp_path, patch_shared_paths):
    assert (
        patch_shared_paths.read_outbox_locked(outbox_file=tmp_path / "nope.json") == []
    )


def test_read_outbox_locked_returns_empty_for_corrupt(tmp_path, patch_shared_paths):
    outbox = tmp_path / "outbox.json"
    outbox.write_text("nope{")
    assert patch_shared_paths.read_outbox_locked(outbox_file=outbox) == []


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


def test_locked_outbox_rw_handles_callback_returning_none(tmp_path, patch_shared_paths):
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


# ---------------------------------------------------------------------------
# Per-surface chat path helpers + migrator
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_chat_dir(monkeypatch, tmp_path: Path, patch_shared_paths):
    """Redirect CHAT_DIR + sentinel into tmp_path so each test is isolated."""
    chat_root = tmp_path / "memory" / "chat"
    monkeypatch.setattr(patch_shared_paths, "CHAT_DIR", chat_root)
    monkeypatch.setattr(
        patch_shared_paths,
        "CHAT_MIGRATION_SENTINEL",
        chat_root / ".migration_done",
    )
    return patch_shared_paths


def test_chat_paths_resolve_under_chat_dir(patch_chat_dir, tmp_path):
    sh = patch_chat_dir
    expected_root = tmp_path / "memory" / "chat" / "planner"
    assert sh.chat_dir("planner") == expected_root
    assert sh.chat_history_path("planner") == expected_root / "chat_history.json"
    assert (
        sh.chat_archive_path("planner") == expected_root / "chat_history_archive.json"
    )
    assert sh.session_path("planner") == expected_root / "planner.session"


def test_ensure_chat_dir_creates_three_files(patch_chat_dir):
    sh = patch_chat_dir
    sh.ensure_chat_dir("planner")
    assert json.loads(sh.chat_history_path("planner").read_text()) == []
    assert json.loads(sh.chat_archive_path("planner").read_text()) == []
    assert sh.session_path("planner").read_text() == ""


def test_ensure_chat_dir_is_idempotent(patch_chat_dir):
    sh = patch_chat_dir
    sh.ensure_chat_dir("planner")
    sh.chat_history_path("planner").write_text(json.dumps([{"x": 1}]))
    sh.session_path("planner").write_text("sess-abc")
    sh.ensure_chat_dir("planner")
    assert json.loads(sh.chat_history_path("planner").read_text()) == [{"x": 1}]
    assert sh.session_path("planner").read_text() == "sess-abc"


def test_save_and_load_session_id_round_trip(patch_chat_dir):
    sh = patch_chat_dir
    assert sh.load_session_id("planner") is None
    sh.save_session_id("planner", "sess-42")
    assert sh.load_session_id("planner") == "sess-42"
    sh.save_session_id("planner", "   ")  # whitespace-only ignored
    assert sh.load_session_id("planner") == "sess-42"


def _make_legacy_layout(root: Path, agents=("planner",)):
    memory = root / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "chat_history.json").write_text(
        json.dumps([{"role": "user", "content": "from-portal"}])
    )
    (memory / "chat_meta.json").write_text(json.dumps({"session_id": "portal-42"}))
    sessions = memory / "sessions" / "internal"
    sessions.mkdir(parents=True, exist_ok=True)
    msgs = root / "messages" / "internal"
    msgs.mkdir(parents=True, exist_ok=True)
    for name in agents:
        ad = msgs / name
        ad.mkdir(parents=True, exist_ok=True)
        (ad / "chat_history.json").write_text(
            json.dumps([{"role": "user", "content": f"from-{name}"}])
        )
        (ad / "chat_history_archive.json").write_text(
            json.dumps([{"role": "user", "content": f"old-{name}"}])
        )
        (sessions / (name + ".session")).write_text(f"sess-{name}-99")


def test_migrate_chat_layout_moves_all_legacy_files(patch_chat_dir, tmp_path):
    sh = patch_chat_dir
    _make_legacy_layout(tmp_path, agents=("planner", "summarizer"))

    report = sh.migrate_chat_layout(
        legacy_memory_dir=tmp_path / "memory",
        legacy_messages_internal=tmp_path / "messages" / "internal",
        legacy_sessions_dir=tmp_path / "memory" / "sessions" / "internal",
    )

    assert report["skipped"] is False
    assert report["portal_history"] is True
    assert report["portal_session"] is True
    assert report["internal_history"] == 2
    assert report["internal_archive"] == 2
    assert report["internal_session"] == 2

    assert json.loads(sh.chat_history_path("main").read_text()) == [
        {"role": "user", "content": "from-portal"}
    ]
    assert sh.session_path("main").read_text() == "portal-42"
    assert not (tmp_path / "memory" / "chat_history.json").exists()
    assert not (tmp_path / "memory" / "chat_meta.json").exists()

    for name in ("planner", "summarizer"):
        assert json.loads(sh.chat_history_path(name).read_text()) == [
            {"role": "user", "content": f"from-{name}"}
        ]
        assert json.loads(sh.chat_archive_path(name).read_text()) == [
            {"role": "user", "content": f"old-{name}"}
        ]
        assert sh.session_path(name).read_text() == f"sess-{name}-99"
        assert not (
            tmp_path / "messages" / "internal" / name / "chat_history.json"
        ).exists()
        assert not (
            tmp_path / "memory" / "sessions" / "internal" / f"{name}.session"
        ).exists()


def test_migrate_chat_layout_is_idempotent(patch_chat_dir, tmp_path):
    sh = patch_chat_dir
    _make_legacy_layout(tmp_path, agents=("planner",))
    sh.migrate_chat_layout(
        legacy_memory_dir=tmp_path / "memory",
        legacy_messages_internal=tmp_path / "messages" / "internal",
        legacy_sessions_dir=tmp_path / "memory" / "sessions" / "internal",
    )
    second = sh.migrate_chat_layout(
        legacy_memory_dir=tmp_path / "memory",
        legacy_messages_internal=tmp_path / "messages" / "internal",
        legacy_sessions_dir=tmp_path / "memory" / "sessions" / "internal",
    )
    assert second["skipped"] is True
    assert sh.CHAT_MIGRATION_SENTINEL.exists()


def test_migrate_chat_layout_skips_when_destination_exists(patch_chat_dir, tmp_path):
    """If a destination file already holds data, migrator must not
    overwrite it — operator can merge manually.
    """
    sh = patch_chat_dir
    _make_legacy_layout(tmp_path, agents=("planner",))
    sh.ensure_chat_dir("planner")
    sh.chat_history_path("planner").write_text(
        json.dumps([{"role": "assistant", "content": "already-here"}])
    )

    sh.migrate_chat_layout(
        legacy_memory_dir=tmp_path / "memory",
        legacy_messages_internal=tmp_path / "messages" / "internal",
        legacy_sessions_dir=tmp_path / "memory" / "sessions" / "internal",
    )

    assert json.loads(sh.chat_history_path("planner").read_text()) == [
        {"role": "assistant", "content": "already-here"}
    ]
    # Legacy file kept on disk for operator inspection.
    assert (
        tmp_path / "messages" / "internal" / "planner" / "chat_history.json"
    ).exists()


def test_migrate_chat_layout_handles_meta_with_no_session_id(patch_chat_dir, tmp_path):
    sh = patch_chat_dir
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory" / "chat_meta.json").write_text(json.dumps({}))

    sh.migrate_chat_layout(
        legacy_memory_dir=tmp_path / "memory",
        legacy_messages_internal=tmp_path / "messages" / "internal",
        legacy_sessions_dir=tmp_path / "memory" / "sessions" / "internal",
    )

    assert not (tmp_path / "memory" / "chat_meta.json").exists()
    assert sh.session_path("main").exists()
    assert sh.session_path("main").read_text() == ""


# ---------------------------------------------------------------------------
# set_agent_control_flag
# ---------------------------------------------------------------------------


def test_set_agent_control_flag_sets_named_flag(patch_shared_paths, tmp_path):
    sh = patch_shared_paths
    agents_file = tmp_path / "agents.json"
    agents_file.write_text(
        json.dumps(
            [
                {"type": "internal", "name": "planner", "status": "online"},
                {"type": "internal", "name": "summarizer", "status": "online"},
            ]
        )
    )

    ok, matched = sh.set_agent_control_flag(
        ["planner"], sh.AGENT_CONTROL_CLEAR_CHAT, agents_file=agents_file
    )
    assert ok is True
    assert matched == ["planner"]

    agents = json.loads(agents_file.read_text())
    by_name = {a["name"]: a for a in agents}
    assert (
        by_name["planner"][sh.AGENT_CONTROL_FIELD][sh.AGENT_CONTROL_CLEAR_CHAT] is True
    )
    # Untargeted agent is left alone.
    assert sh.AGENT_CONTROL_FIELD not in by_name["summarizer"]


def test_set_agent_control_flag_preserves_unrelated_keys(patch_shared_paths, tmp_path):
    sh = patch_shared_paths
    agents_file = tmp_path / "agents.json"
    agents_file.write_text(
        json.dumps(
            [
                {
                    "type": "internal",
                    "name": "planner",
                    "control": {"future_flag": "keep"},
                }
            ]
        )
    )
    sh.set_agent_control_flag(
        ["planner"], sh.AGENT_CONTROL_CLEAR_SESSION, agents_file=agents_file
    )
    ctl = json.loads(agents_file.read_text())[0][sh.AGENT_CONTROL_FIELD]
    assert ctl["future_flag"] == "keep"
    assert ctl[sh.AGENT_CONTROL_CLEAR_SESSION] is True


def test_set_agent_control_flag_empty_names_is_noop(patch_shared_paths, tmp_path):
    sh = patch_shared_paths
    agents_file = tmp_path / "agents.json"
    ok, matched = sh.set_agent_control_flag(
        [], sh.AGENT_CONTROL_CLEAR_CHAT, agents_file=agents_file
    )
    assert ok is True
    assert matched == []
    # File was not touched / created.
    assert not agents_file.exists()


def test_set_agent_control_flag_unknown_name_returns_no_match(
    patch_shared_paths, tmp_path
):
    sh = patch_shared_paths
    agents_file = tmp_path / "agents.json"
    agents_file.write_text(json.dumps([{"type": "internal", "name": "planner"}]))
    ok, matched = sh.set_agent_control_flag(
        ["ghost"], sh.AGENT_CONTROL_CLEAR_CHAT, agents_file=agents_file
    )
    assert ok is True
    assert matched == []
    # Real agent untouched.
    agents = json.loads(agents_file.read_text())
    assert sh.AGENT_CONTROL_FIELD not in agents[0]
