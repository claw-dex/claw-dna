"""Tests for app/agents_tab.py — pure helpers + action handlers.

Streamlit-rendering helpers (`_render_chat`, `_render_inbox`, ...) are
not exercised here; they call into Streamlit primitives that require
a running session. We unit-test the data loaders and the side-effecting
action handlers (`_handle_send_message`, `_handle_clear_flag`) through
their non-UI surface, and rely on `scripts/app_check.py` for full-render
smoke testing.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

# ── fixture ──────────────────────────────────────────────────────────────


@pytest.fixture
def patch_agents_tab(monkeypatch, agent_root: Path, patch_shared_paths):
    """Redirect agents_tab's hardcoded paths into agent_root."""
    import app.agents_tab as at

    monkeypatch.setattr(at, "_BASE", agent_root)
    monkeypatch.setattr(at, "_AGENTS_FILE", agent_root / "memory" / "agents.json")
    monkeypatch.setattr(at, "_EXTERNAL_DIR", agent_root / "messages" / "external")
    # Chat helpers come from `services.shared` — redirect CHAT_DIR there
    # so the unified /agent/memory/chat/<name>/ layout points at agent_root.
    monkeypatch.setattr(patch_shared_paths, "CHAT_DIR", agent_root / "memory" / "chat")
    monkeypatch.setattr(
        patch_shared_paths,
        "CHAT_MIGRATION_SENTINEL",
        agent_root / "memory" / "chat" / ".migration_done",
    )
    return at


def _seed_agents(at_mod, agents):
    at_mod._AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    at_mod._AGENTS_FILE.write_text(json.dumps(agents))


# ── _load_agents ────────────────────────────────────────────────────────


def test_load_agents_filters_to_internal_and_external_only(patch_agents_tab):
    at = patch_agents_tab
    _seed_agents(
        at,
        [
            {"type": "internal", "name": "planner", "status": "online"},
            {"type": "external", "name": "bot", "status": "online"},
            {"type": "system", "name": "scheduler"},  # filtered out
            "not a dict",  # filtered out
            {"type": "internal", "name": ""},  # empty name filtered out
        ],
    )
    out = at._load_agents()
    assert [a["name"] for a in out] == ["bot", "planner"]


def test_load_agents_sorted_by_name(patch_agents_tab):
    at = patch_agents_tab
    _seed_agents(
        at,
        [
            {"type": "internal", "name": "zebra"},
            {"type": "internal", "name": "alpha"},
            {"type": "external", "name": "monkey"},
        ],
    )
    out = at._load_agents()
    assert [a["name"] for a in out] == ["alpha", "monkey", "zebra"]


def test_load_agents_handles_missing_file(patch_agents_tab):
    # No agents.json on disk → empty list, no crash.
    assert patch_agents_tab._load_agents() == []


# ── _load_internal_chat ─────────────────────────────────────────────────


def test_load_internal_chat_reads_unified_layout(patch_agents_tab):
    at = patch_agents_tab
    from shared import chat_history_path, ensure_chat_dir

    ensure_chat_dir("planner")
    records = [
        {"role": "user", "ts": "2026-05-08T10:00:00+00:00", "content": "hi"},
        {"role": "assistant", "ts": "2026-05-08T10:00:01+00:00", "content": "hello"},
    ]
    chat_history_path("planner").write_text(json.dumps(records))
    assert at._load_internal_chat("planner") == records


def test_load_internal_chat_returns_empty_for_missing_agent(patch_agents_tab):
    assert patch_agents_tab._load_internal_chat("ghost") == []


# ── _synthesize_external_chat ───────────────────────────────────────────


def test_synthesize_external_chat_merges_inbox_and_outbox_by_timestamp(
    patch_agents_tab,
):
    at = patch_agents_tab
    name = "bot"
    base = at._EXTERNAL_DIR / name
    base.mkdir(parents=True, exist_ok=True)
    # Out-of-order timestamps to prove sorting.
    (base / "inbox.json").write_text(
        json.dumps(
            [
                {
                    "id": "i2",
                    "type": "message",
                    "content": "2nd",
                    "timestamp": "2026-05-08T10:02:00+00:00",
                }
            ]
        )
    )
    (base / "inbox_history.json").write_text(
        json.dumps(
            [
                {
                    "id": "i1",
                    "type": "message",
                    "content": "1st",
                    "timestamp": "2026-05-08T10:00:00+00:00",
                }
            ]
        )
    )
    (base / "outbox.json").write_text(
        json.dumps(
            [
                {
                    "id": "o1",
                    "type": "response",
                    "content": "ack",
                    "timestamp": "2026-05-08T10:01:00+00:00",
                }
            ]
        )
    )
    (base / "outbox_history.json").write_text(json.dumps([]))

    out = at._synthesize_external_chat({"type": "external", "name": name})

    # Sorted by timestamp; alternates user (inbox) / assistant (outbox).
    assert [(r["role"], r["content"]) for r in out] == [
        ("user", "1st"),
        ("assistant", "ack"),
        ("user", "2nd"),
    ]


def test_synthesize_external_chat_with_no_files_returns_empty(patch_agents_tab):
    out = patch_agents_tab._synthesize_external_chat(
        {"type": "external", "name": "ghost"}
    )
    assert out == []


def test_synthesize_external_chat_concatenates_subject_into_content(
    patch_agents_tab,
):
    at = patch_agents_tab
    name = "bot"
    base = at._EXTERNAL_DIR / name
    base.mkdir(parents=True, exist_ok=True)
    (base / "inbox.json").write_text(
        json.dumps(
            [
                {
                    "id": "i1",
                    "type": "message",
                    "subject": "Re: ping",
                    "content": "still here",
                    "timestamp": "2026-05-08T10:00:00+00:00",
                }
            ]
        )
    )
    out = at._synthesize_external_chat({"type": "external", "name": name})
    assert "Re: ping" in out[0]["content"]
    assert "still here" in out[0]["content"]


# ── _handle_send_message ────────────────────────────────────────────────


def _stub_st(monkeypatch, at):
    """Replace the Streamlit primitives the action handlers call so the
    unit test doesn't need a live Streamlit session.
    """
    fake_st = mock.MagicMock()
    monkeypatch.setattr(at, "st", fake_st)
    return fake_st


def test_send_message_writes_envelope_with_correct_shape(patch_agents_tab, monkeypatch):
    at = patch_agents_tab
    fake_st = _stub_st(monkeypatch, at)

    inbox = at._BASE / "messages" / "internal" / "planner" / "inbox.json"
    agent = {"type": "internal", "name": "planner", "inbox": str(inbox)}

    at._handle_send_message(agent, subject="Hi", content="Hello there")

    assert inbox.exists()
    items = json.loads(inbox.read_text())
    assert len(items) == 1
    env = items[0]
    assert env["type"] == "message"
    assert env["from"] == "portal"
    assert env["source"] == "portal"
    assert env["subject"] == "Hi"
    assert env["content"] == "Hello there"
    assert env["reply_to"] == "messages/inbox.json"
    assert env["priority"] == 3
    assert env["id"]  # non-empty uuid
    assert env["timestamp"]
    assert env["received_at"]  # stamped by write_to_inbox
    fake_st.success.assert_called_once()
    fake_st.error.assert_not_called()


def test_send_message_rejects_empty_content(patch_agents_tab, monkeypatch):
    at = patch_agents_tab
    fake_st = _stub_st(monkeypatch, at)
    agent = {"type": "internal", "name": "planner", "inbox": "/tmp/no.json"}
    at._handle_send_message(agent, subject="", content="   ")
    fake_st.warning.assert_called_once()


def test_send_message_handles_missing_inbox_field(patch_agents_tab, monkeypatch):
    at = patch_agents_tab
    fake_st = _stub_st(monkeypatch, at)
    at._handle_send_message(
        {"type": "internal", "name": "planner"}, subject="", content="x"
    )
    fake_st.error.assert_called_once()


# ── _handle_clear_flag ──────────────────────────────────────────────────


def test_clear_flag_sets_control_in_agents_json(patch_agents_tab, monkeypatch):
    at = patch_agents_tab
    fake_st = _stub_st(monkeypatch, at)
    _seed_agents(at, [{"type": "internal", "name": "planner", "status": "online"}])

    at._handle_clear_flag("planner", at.AGENT_CONTROL_CLEAR_CHAT, "chat")

    agents = json.loads(at._AGENTS_FILE.read_text())
    assert agents[0]["control"][at.AGENT_CONTROL_CLEAR_CHAT] is True
    fake_st.success.assert_called_once()


def test_clear_flag_unknown_agent_warns(patch_agents_tab, monkeypatch):
    at = patch_agents_tab
    fake_st = _stub_st(monkeypatch, at)
    _seed_agents(at, [{"type": "internal", "name": "planner", "status": "online"}])
    at._handle_clear_flag("ghost", at.AGENT_CONTROL_CLEAR_SESSION, "session")
    fake_st.warning.assert_called_once()
    # Real agent untouched.
    agents = json.loads(at._AGENTS_FILE.read_text())
    assert "control" not in agents[0]


def test_clear_flag_preserves_unrelated_control_keys(patch_agents_tab, monkeypatch):
    at = patch_agents_tab
    _stub_st(monkeypatch, at)
    _seed_agents(
        at,
        [
            {
                "type": "internal",
                "name": "planner",
                "status": "online",
                "control": {"future_flag": "keep-me"},
            }
        ],
    )

    at._handle_clear_flag("planner", at.AGENT_CONTROL_CLEAR_SESSION, "session")

    agents = json.loads(at._AGENTS_FILE.read_text())
    ctl = agents[0]["control"]
    assert ctl["future_flag"] == "keep-me"
    assert ctl[at.AGENT_CONTROL_CLEAR_SESSION] is True
