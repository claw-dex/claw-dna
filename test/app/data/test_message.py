"""Tests for app.data.message loaders.

The decorated loaders (``load_inbox``, ``load_outbox``, etc.) take no arguments
externally; they read JSON from disk via ``@_mfile_cache``. The wrapped function
body receives the parsed ``data`` for post-processing.

Tests exercise both the wrapped function directly (passing crafted dicts) and
the public mtime-cached entrypoints (writing real JSON files).
"""

from __future__ import annotations

import json

import pytest

from app.data import message
from app.data import _cache as data_cache


@pytest.fixture(autouse=True)
def _clean_mfile_caches():
    for c in data_cache._MFILE_CACHES:
        c.clear()
    yield
    for c in data_cache._MFILE_CACHES:
        c.clear()


@pytest.fixture
def patched_paths(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    msgs = tmp_path / "messages"
    mem.mkdir()
    msgs.mkdir()
    monkeypatch.setattr("app.data.message.MESSAGES_DIR", str(msgs))
    monkeypatch.setattr(
        "app.data.message.HISTORY_PATH", str(mem / "command_history.json")
    )
    return {"memory": mem, "messages": msgs}


# ── wrapped-function unit tests (call .__wrapped__ to bypass decorator) ────


def test_load_outbox_normalises_dict_to_list():
    # wrapped function: dict input becomes single-element list
    inner = message.load_outbox.__wrapped__
    assert inner({"id": 1}) == [{"id": 1}]


def test_load_outbox_passes_through_list():
    inner = message.load_outbox.__wrapped__
    assert inner([{"a": 1}]) == [{"a": 1}]


def test_load_outbox_non_list_non_dict_returns_empty():
    inner = message.load_outbox.__wrapped__
    assert inner("garbage") == []
    assert inner(None) == []


def test_load_inbox_pass_through():
    inner = message.load_inbox.__wrapped__
    assert inner([{"x": 1}]) == [{"x": 1}]


# ── end-to-end through cache + JSON file ──────────────────────────────────


def test_load_inbox_reads_file(patched_paths):
    p = patched_paths["messages"] / "inbox.json"
    p.write_text(json.dumps([{"id": "m1"}]))
    assert message.load_inbox() == [{"id": "m1"}]


def test_load_inbox_missing_file_default_list(patched_paths):
    assert message.load_inbox() == []


def test_load_inbox_history(patched_paths):
    p = patched_paths["messages"] / "inbox_history.json"
    p.write_text(json.dumps([{"h": 1}]))
    assert message.load_inbox_history() == [{"h": 1}]


def test_load_outbox_dict_normalised_via_file(patched_paths):
    p = patched_paths["messages"] / "outbox.json"
    p.write_text(json.dumps({"id": "single"}))
    assert message.load_outbox() == [{"id": "single"}]


def test_load_outbox_list_via_file(patched_paths):
    p = patched_paths["messages"] / "outbox.json"
    p.write_text(json.dumps([{"a": 1}, {"b": 2}]))
    assert message.load_outbox() == [{"a": 1}, {"b": 2}]


def test_load_outbox_history(patched_paths):
    p = patched_paths["messages"] / "outbox_history.json"
    p.write_text(json.dumps([{"sent": True}]))
    assert message.load_outbox_history() == [{"sent": True}]


def test_load_history_reads_file(patched_paths):
    p = patched_paths["memory"] / "command_history.json"
    p.write_text(json.dumps([{"type": "bash", "content": "ls"}]))
    out = message.load_history()
    assert out == [{"type": "bash", "content": "ls"}]


def test_load_history_missing_file_default(patched_paths):
    assert message.load_history() == []


def test_loaders_cache_between_calls(patched_paths):
    p = patched_paths["messages"] / "inbox.json"
    p.write_text(json.dumps([{"v": 1}]))
    a = message.load_inbox()
    b = message.load_inbox()
    assert a is b  # cached → same object
