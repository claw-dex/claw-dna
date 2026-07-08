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
    monkeypatch.setattr(
        at, "_SERVER_ERRORS_FILE", agent_root / "memory" / "server_errors.json"
    )
    monkeypatch.setattr(
        at, "GOALS_PATH", str(agent_root / "memory" / "goal.json")
    )
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
    # `from` is a structured identity: source="portal", no transport (the portal
    # writes directly, so source already says how it arrived — no duplication).
    assert env["from"] == {"source": "portal", "role": "owner"}
    assert "source" not in env  # no top-level source
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


# ── _ts ─────────────────────────────────────────────────────────────────


def test_ts_prefers_ts_key(patch_agents_tab):
    at = patch_agents_tab
    item = {"ts": "2026-01-01", "timestamp": "2025-01-01"}
    assert at._ts(item) == "2026-01-01"


def test_ts_falls_through_to_timestamp(patch_agents_tab):
    at = patch_agents_tab
    assert at._ts({"timestamp": "2026-02-02"}) == "2026-02-02"


def test_ts_falls_through_to_received_at(patch_agents_tab):
    at = patch_agents_tab
    assert at._ts({"received_at": "2026-03-03"}) == "2026-03-03"


def test_ts_falls_through_to_processed_at(patch_agents_tab):
    at = patch_agents_tab
    assert at._ts({"processed_at": "2026-04-04"}) == "2026-04-04"


def test_ts_returns_empty_when_no_keys_present(patch_agents_tab):
    assert patch_agents_tab._ts({}) == ""


def test_ts_skips_non_string_values(patch_agents_tab):
    at = patch_agents_tab
    # int value for `ts` should be skipped, fallback to `timestamp`.
    assert at._ts({"ts": 12345, "timestamp": "2026-05-05"}) == "2026-05-05"


# ── _caddy_url ──────────────────────────────────────────────────────────


def test_caddy_url_maps_agent_path(patch_agents_tab):
    at = patch_agents_tab
    assert at._caddy_url("/agent/memory/chat/planner/chat_history.json") == (
        "/_/agent/memory/chat/planner/chat_history.json"
    )


def test_caddy_url_percent_encodes_special_chars(patch_agents_tab):
    at = patch_agents_tab
    url = at._caddy_url("/agent/memory/chat/my agent (test)/history.json")
    assert "%20" in url or "+" in url  # space encoded
    assert "%28" in url  # open paren encoded
    assert "%29" in url  # close paren encoded
    assert url.startswith("/_")


def test_caddy_url_passes_through_non_agent_paths(patch_agents_tab):
    at = patch_agents_tab
    assert at._caddy_url("/tmp/some/path") == "/tmp/some/path"


# ── _from_label ─────────────────────────────────────────────────────────


def test_from_label_dict_prefers_handle(patch_agents_tab):
    at = patch_agents_tab
    env = {"from": {"handle": "@user", "source": "portal", "transport": "ws"}}
    assert at._from_label(env) == "@user"


def test_from_label_dict_falls_to_source(patch_agents_tab):
    at = patch_agents_tab
    env = {"from": {"source": "portal", "transport": "ws"}}
    assert at._from_label(env) == "portal"


def test_from_label_dict_falls_to_transport(patch_agents_tab):
    at = patch_agents_tab
    env = {"from": {"transport": "telegram"}}
    assert at._from_label(env) == "telegram"


def test_from_label_dict_falls_to_raw(patch_agents_tab):
    at = patch_agents_tab
    env = {"from": {"raw": "unknown-sender"}}
    assert at._from_label(env) == "unknown-sender"


def test_from_label_string(patch_agents_tab):
    at = patch_agents_tab
    assert at._from_label({"from": "alice"}) == "alice"


def test_from_label_missing_returns_none(patch_agents_tab):
    at = patch_agents_tab
    assert at._from_label({}) is None


def test_from_label_empty_string_returns_none(patch_agents_tab):
    at = patch_agents_tab
    assert at._from_label({"from": ""}) is None


# ── _load_agent_goals ───────────────────────────────────────────────────


def _seed_goals(at_mod, goals):
    """Write a goal.json file in the patched GOALS_PATH location."""
    goal_path = Path(at_mod.GOALS_PATH)
    goal_path.parent.mkdir(parents=True, exist_ok=True)
    goal_path.write_text(json.dumps(goals))


def test_load_agent_goals_filters_by_delegated_to_name(patch_agents_tab):
    at = patch_agents_tab
    _seed_goals(at, [
        {
            "id": "g1",
            "goal": "do A",
            "status": "pending",
            "delegated_to": {"name": "planner", "type": "internal"},
        },
        {
            "id": "g2",
            "goal": "do B",
            "status": "pending",
            "delegated_to": {"name": "other-agent", "type": "internal"},
        },
        {
            "id": "g3",
            "goal": "do C",
            "status": "pending",
            "delegated_to": {"name": "planner", "type": "internal"},
        },
    ])
    out = at._load_agent_goals("planner")
    assert [g["id"] for g in out] == ["g1", "g3"]


def test_load_agent_goals_ignores_non_dict_delegated_to(patch_agents_tab):
    at = patch_agents_tab
    _seed_goals(at, [
        {
            "id": "g1",
            "goal": "do A",
            "status": "pending",
            "delegated_to": "planner",  # string, not dict
        },
        {
            "id": "g2",
            "goal": "do B",
            "status": "pending",
            "delegated_to": {"name": "planner", "type": "internal"},
        },
    ])
    out = at._load_agent_goals("planner")
    assert [g["id"] for g in out] == ["g2"]


def test_load_agent_goals_sorts_by_status_then_created_at(patch_agents_tab):
    at = patch_agents_tab
    _seed_goals(at, [
        {
            "id": "old-pending",
            "goal": "old",
            "status": "pending",
            "created_at": "2026-01-01T00:00:00Z",
            "delegated_to": {"name": "bot"},
        },
        {
            "id": "new-pending",
            "goal": "new",
            "status": "pending",
            "created_at": "2026-06-01T00:00:00Z",
            "delegated_to": {"name": "bot"},
        },
        {
            "id": "in-progress",
            "goal": "active",
            "status": "in_progress",
            "created_at": "2026-03-01T00:00:00Z",
            "delegated_to": {"name": "bot"},
        },
        {
            "id": "completed",
            "goal": "done",
            "status": "completed",
            "created_at": "2026-05-01T00:00:00Z",
            "delegated_to": {"name": "bot"},
        },
    ])
    out = at._load_agent_goals("bot")
    ids = [g["id"] for g in out]
    # in_progress (0) → pending (1, newest first) → completed (2)
    assert ids == ["in-progress", "new-pending", "old-pending", "completed"]


def test_load_agent_goals_returns_empty_for_empty_name(patch_agents_tab):
    at = patch_agents_tab
    _seed_goals(at, [
        {"id": "g1", "status": "pending", "delegated_to": {"name": "bot"}},
    ])
    assert at._load_agent_goals("") == []


def test_load_agent_goals_handles_missing_goal_file(patch_agents_tab):
    assert patch_agents_tab._load_agent_goals("bot") == []


def test_load_agent_goals_skips_non_dict_entries(patch_agents_tab):
    at = patch_agents_tab
    _seed_goals(at, [
        "not a dict",
        42,
        {"id": "g1", "status": "pending", "delegated_to": {"name": "bot"}},
    ])
    out = at._load_agent_goals("bot")
    assert [g["id"] for g in out] == ["g1"]


# ── _load_agent_errors ──────────────────────────────────────────────────


def _seed_errors(at_mod, errors):
    """Write a server_errors.json file in the patched path."""
    path = at_mod._SERVER_ERRORS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(errors))


def test_load_agent_errors_matches_by_context_prefix(patch_agents_tab):
    at = patch_agents_tab
    _seed_errors(at, [
        {"context": "planner: turn timeout", "timestamp": "2026-06-01T10:00:00Z"},
        {"context": "other: crash", "timestamp": "2026-06-01T09:00:00Z"},
        {"context": "planner: SDK error", "timestamp": "2026-06-01T11:00:00Z"},
    ])
    out = at._load_agent_errors("planner")
    assert len(out) == 2
    assert all("planner" in e["context"] for e in out)


def test_load_agent_errors_matches_by_tab_field(patch_agents_tab):
    at = patch_agents_tab
    _seed_errors(at, [
        {"tab": "planner", "error": "render crash", "timestamp": "2026-06-01T10:00:00Z"},
        {"tab": "other", "error": "unrelated", "timestamp": "2026-06-01T09:00:00Z"},
    ])
    out = at._load_agent_errors("planner")
    assert len(out) == 1
    assert out[0]["error"] == "render crash"


def test_load_agent_errors_combines_context_and_tab_matches(patch_agents_tab):
    at = patch_agents_tab
    _seed_errors(at, [
        {"context": "planner: timeout", "timestamp": "2026-06-01T10:00:00Z"},
        {"tab": "planner", "error": "render", "timestamp": "2026-06-01T11:00:00Z"},
        {"context": "other: x", "tab": "other", "timestamp": "2026-06-01T09:00:00Z"},
    ])
    out = at._load_agent_errors("planner")
    assert len(out) == 2


def test_load_agent_errors_sorted_newest_first(patch_agents_tab):
    at = patch_agents_tab
    _seed_errors(at, [
        {"context": "bot: a", "timestamp": "2026-01-01T00:00:00Z"},
        {"context": "bot: c", "timestamp": "2026-06-01T00:00:00Z"},
        {"context": "bot: b", "timestamp": "2026-03-01T00:00:00Z"},
    ])
    out = at._load_agent_errors("bot")
    timestamps = [e["timestamp"] for e in out]
    assert timestamps == sorted(timestamps, reverse=True)


def test_load_agent_errors_returns_empty_for_empty_name(patch_agents_tab):
    at = patch_agents_tab
    _seed_errors(at, [
        {"context": "bot: err", "timestamp": "2026-06-01T00:00:00Z"},
    ])
    assert at._load_agent_errors("") == []


def test_load_agent_errors_handles_missing_file(patch_agents_tab):
    assert patch_agents_tab._load_agent_errors("bot") == []


def test_load_agent_errors_skips_non_dict_entries(patch_agents_tab):
    at = patch_agents_tab
    _seed_errors(at, [
        "not a dict",
        {"context": "bot: err", "timestamp": "2026-06-01T00:00:00Z"},
    ])
    out = at._load_agent_errors("bot")
    assert len(out) == 1
