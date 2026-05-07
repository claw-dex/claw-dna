"""Tests for services/internal_agent_chat.py — pure helpers + send_reply tool."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed_agents(iac, agents: list) -> None:
    iac.AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    iac.AGENTS_FILE.write_text(json.dumps(agents))


def _seed_inbox(iac, name: str, items: list) -> Path:
    p = iac._inbox_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items))
    return p


def _seed_inbox_history(iac, name: str, items: list) -> Path:
    p = iac._inbox_history_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items))
    return p


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _internal_agents
# ---------------------------------------------------------------------------


def test_internal_agents_filters_by_type(patch_iac_paths):
    iac = patch_iac_paths
    out = iac._internal_agents(
        [
            {"type": "internal", "name": "a", "status": "online"},
            {"type": "external", "name": "b"},
            {"type": "internal", "name": "c", "status": "deactivated"},
            "not a dict",
            {"type": "internal", "name": ""},  # empty name skipped
        ]
    )
    assert list(out.keys()) == ["a"]


def test_internal_agents_includes_status_online_and_default(patch_iac_paths):
    iac = patch_iac_paths
    out = iac._internal_agents(
        [
            {"type": "internal", "name": "a"},  # no status — kept
            {"type": "internal", "name": "b", "status": "offline"},
        ]
    )
    assert set(out.keys()) == {"a", "b"}


# ---------------------------------------------------------------------------
# _routing_rules / _allowed_agent_names
# ---------------------------------------------------------------------------


def test_routing_rules_missing_returns_empty(patch_iac_paths):
    assert patch_iac_paths._routing_rules({}) == []


def test_routing_rules_non_list_returns_empty(patch_iac_paths):
    assert patch_iac_paths._routing_rules({"outbox_routing_rules": "x"}) == []


def test_allowed_agent_names_always_includes_main(patch_iac_paths):
    names = patch_iac_paths._allowed_agent_names({})
    assert names == ["main"]


def test_allowed_agent_names_dedups_and_preserves_order(patch_iac_paths):
    cfg = {
        "outbox_routing_rules": [
            {"description": "d1", "agent": "main"},  # already present
            {"description": "d2", "agent": "research"},
            {"description": "d3", "agent": "research"},  # dup
            {"agent": ""},  # empty skipped
            "not a dict",
            {"description": "d4", "agent": "summarizer"},
        ]
    }
    assert patch_iac_paths._allowed_agent_names(cfg) == [
        "main",
        "research",
        "summarizer",
    ]


# ---------------------------------------------------------------------------
# _build_tool_description
# ---------------------------------------------------------------------------


def test_build_tool_description_lists_rules(patch_iac_paths):
    cfg = {
        "outbox_routing_rules": [
            {"description": "Send numbered plans back to main.", "agent": "main"},
            {"description": "Hand off web research.", "agent": "research-bot"},
        ]
    }
    desc = patch_iac_paths._build_tool_description(cfg)
    assert "agent=main — Send numbered plans back to main." in desc
    assert "agent=research-bot — Hand off web research." in desc
    # Allowed type list is mentioned
    assert "agent_response" in desc
    assert "agent_needs_human" in desc
    assert "agent_error" in desc
    assert "agent_info" in desc
    # Disallowed types must not appear in the description
    assert "'goal'" not in desc
    assert "'message'" not in desc
    assert "'event'" not in desc


def test_build_tool_description_no_rules(patch_iac_paths):
    desc = patch_iac_paths._build_tool_description({})
    assert "main" in desc.lower()
    assert "No routing rules" in desc


# ---------------------------------------------------------------------------
# _safe_target_path
# ---------------------------------------------------------------------------


def test_safe_target_path_accepts_inbox_under_messages(patch_iac_paths):
    iac = patch_iac_paths
    p = iac.MESSAGES_DIR / "internal" / "x" / "inbox.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[]")
    assert iac._safe_target_path(p) is True


def test_safe_target_path_rejects_outside_messages(patch_iac_paths, agent_root):
    p = agent_root / "memory" / "inbox.json"  # outside messages
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[]")
    assert patch_iac_paths._safe_target_path(p) is False


def test_safe_target_path_rejects_non_inbox_filename(patch_iac_paths):
    iac = patch_iac_paths
    p = iac.MESSAGES_DIR / "internal" / "x" / "chat_history.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[]")
    assert iac._safe_target_path(p) is False


# ---------------------------------------------------------------------------
# _resolve_agent_inbox
# ---------------------------------------------------------------------------


def test_resolve_agent_inbox_main_uses_inbox_file(patch_iac_paths):
    assert patch_iac_paths._resolve_agent_inbox("main") == patch_iac_paths.INBOX_FILE


def test_resolve_agent_inbox_known_agent(patch_iac_paths, agent_root):
    iac = patch_iac_paths
    inbox = agent_root / "messages" / "external" / "bot" / "inbox.json"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text("[]")
    _seed_agents(iac, [{"type": "external", "name": "bot", "inbox": str(inbox)}])
    assert iac._resolve_agent_inbox("bot") == inbox


def test_resolve_agent_inbox_unknown(patch_iac_paths):
    _seed_agents(patch_iac_paths, [])
    assert patch_iac_paths._resolve_agent_inbox("ghost") is None


def test_resolve_agent_inbox_known_but_no_inbox_field(patch_iac_paths):
    _seed_agents(
        patch_iac_paths,
        [{"type": "internal", "name": "no-inbox"}],
    )
    assert patch_iac_paths._resolve_agent_inbox("no-inbox") is None


# ---------------------------------------------------------------------------
# _lookup_inbox_history_reply_to
# ---------------------------------------------------------------------------


def test_lookup_history_reply_to_found(patch_iac_paths):
    iac = patch_iac_paths
    iac._agent_dir("planner").mkdir(parents=True, exist_ok=True)
    _seed_inbox_history(
        iac,
        "planner",
        [
            {"id": "abc", "reply_to": "messages/inbox.json"},
            {"id": "def", "reply_to": "messages/external/x/inbox.json"},
        ],
    )
    assert iac._lookup_inbox_history_reply_to("planner", "def") == (
        "messages/external/x/inbox.json"
    )


def test_lookup_history_reply_to_missing_id(patch_iac_paths):
    iac = patch_iac_paths
    iac._agent_dir("planner").mkdir(parents=True, exist_ok=True)
    _seed_inbox_history(
        iac, "planner", [{"id": "abc", "reply_to": "messages/inbox.json"}]
    )
    assert iac._lookup_inbox_history_reply_to("planner", "nope") is None


def test_lookup_history_reply_to_empty_reply_to(patch_iac_paths):
    iac = patch_iac_paths
    iac._agent_dir("planner").mkdir(parents=True, exist_ok=True)
    _seed_inbox_history(iac, "planner", [{"id": "abc", "reply_to": ""}])
    assert iac._lookup_inbox_history_reply_to("planner", "abc") is None


# ---------------------------------------------------------------------------
# send_reply tool — validation
# ---------------------------------------------------------------------------


def _make_handler(iac, name="planner", cfg=None):
    cfg = cfg if cfg is not None else {}
    iac._ensure_agent_files(name)
    return iac._build_send_reply_handler(name, cfg)


def test_send_reply_requires_exactly_one_addressee(patch_iac_paths):
    h = _make_handler(patch_iac_paths)
    out = _run(h({"type": "agent_response", "content": "hi"}))
    assert out["is_error"] is True
    out = _run(
        h(
            {
                "agent": "main",
                "message_id": "x",
                "type": "agent_response",
                "content": "hi",
            }
        )
    )
    assert out["is_error"] is True


def test_send_reply_requires_type(patch_iac_paths):
    h = _make_handler(patch_iac_paths)
    out = _run(h({"agent": "main", "content": "hi"}))
    assert out["is_error"] is True


def test_send_reply_rejects_disallowed_types(patch_iac_paths):
    h = _make_handler(patch_iac_paths)
    for bad in ("goal", "message", "event", "response", "info"):
        out = _run(h({"agent": "main", "type": bad, "content": "hi"}))
        assert out["is_error"] is True, "type=" + bad + " should have been rejected"


def test_send_reply_requires_content(patch_iac_paths):
    h = _make_handler(patch_iac_paths)
    out = _run(h({"agent": "main", "type": "agent_response", "content": "  "}))
    assert out["is_error"] is True


def test_send_reply_priority_validation(patch_iac_paths):
    h = _make_handler(patch_iac_paths)
    for bad in (0, 6, "1", True):
        out = _run(
            h(
                {
                    "agent": "main",
                    "type": "agent_info",
                    "content": "x",
                    "priority": bad,
                }
            )
        )
        assert out["is_error"] is True, "priority=" + repr(bad) + " should fail"


def test_send_reply_rejects_unknown_agent(patch_iac_paths):
    iac = patch_iac_paths
    cfg = {"outbox_routing_rules": [{"description": "d", "agent": "research"}]}
    h = _make_handler(iac, cfg=cfg)
    # 'research' is allowed (in rules) but not present in agents.json → tool fails
    _seed_agents(iac, [])
    out = _run(h({"agent": "research", "type": "agent_response", "content": "x"}))
    assert out["is_error"] is True


def test_send_reply_blocks_agent_not_in_rules(patch_iac_paths):
    iac = patch_iac_paths
    h = _make_handler(iac, cfg={})
    out = _run(h({"agent": "stranger", "type": "agent_response", "content": "x"}))
    assert out["is_error"] is True
    assert "not allowed" in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# send_reply tool — successful delivery paths
# ---------------------------------------------------------------------------


def test_send_reply_to_main_writes_to_inbox_file(patch_iac_paths):
    iac = patch_iac_paths
    h = _make_handler(iac)
    out = _run(h({"agent": "main", "type": "agent_response", "content": "done"}))
    assert out.get("is_error") is not True
    items = json.loads(iac.INBOX_FILE.read_text())
    assert len(items) == 1
    env = items[0]
    assert env["type"] == "agent_response"
    assert env["content"] == "done"
    assert env["source"] == "internal_agent"
    assert env["reply_to"] == "messages/internal/planner/inbox.json"
    assert env["timestamp"]
    # write_to_inbox stamps received_at for the main inbox
    assert env["received_at"]


def test_send_reply_to_external_agent_stamps_received_at(patch_iac_paths, agent_root):
    iac = patch_iac_paths
    bot_inbox = agent_root / "messages" / "external" / "research" / "inbox.json"
    bot_inbox.parent.mkdir(parents=True, exist_ok=True)
    bot_inbox.write_text("[]")
    _seed_agents(
        iac,
        [
            {
                "type": "external",
                "name": "research",
                "inbox": str(bot_inbox),
            }
        ],
    )
    cfg = {
        "outbox_routing_rules": [
            {"description": "delegate web research", "agent": "research"}
        ]
    }
    h = _make_handler(iac, cfg=cfg)
    out = _run(
        h(
            {
                "agent": "research",
                "type": "agent_info",
                "content": "fyi",
                "priority": 2,
            }
        )
    )
    assert out.get("is_error") is not True
    items = json.loads(bot_inbox.read_text())
    assert len(items) == 1
    env = items[0]
    assert env["received_at"]  # stamped at write time
    assert env["priority"] == 2
    assert env["source"] == "internal_agent"
    assert env["reply_to"] == "messages/internal/planner/inbox.json"


def test_send_reply_agent_needs_human_mirrors_to_main_outbox(
    patch_iac_paths, agent_root
):
    """agent_needs_human → primary delivery + main outbox mirror.

    Mirrors the behaviour of external_agent_api.py:708-731 — the mirrored
    entry has type='needs_human' (no agent_ prefix) and content prefixed
    with `[from internal agent <name>] ...` so existing Telegram /
    WhatsApp channels surface it without main-agent intervention.
    """
    iac = patch_iac_paths
    h = _make_handler(iac)
    out = _run(
        h(
            {
                "agent": "main",
                "type": "agent_needs_human",
                "content": "API key rotation required",
            }
        )
    )
    assert out.get("is_error") is not True
    # Primary delivery — main inbox got the agent_needs_human envelope
    inbox_items = json.loads(iac.INBOX_FILE.read_text())
    assert len(inbox_items) == 1
    assert inbox_items[0]["type"] == "agent_needs_human"
    assert inbox_items[0]["content"] == "API key rotation required"
    # Mirror — main outbox got a needs_human envelope with the prefix
    outbox_path = agent_root / "messages" / "outbox.json"
    assert outbox_path.exists()
    outbox_items = json.loads(outbox_path.read_text())
    assert len(outbox_items) == 1
    mirror = outbox_items[0]
    assert mirror["type"] == "needs_human"  # prefix stripped
    assert mirror["content"] == (
        "[from internal agent planner] API key rotation required"
    )
    assert mirror["subject"] == "[from internal agent planner]"
    assert mirror["timestamp"]
    # Tool output text mentions the mirror so the LLM knows it happened
    assert "mirrored" in out["content"][0]["text"]


def test_send_reply_non_needs_human_does_not_mirror(patch_iac_paths, agent_root):
    """Only agent_needs_human triggers the outbox mirror — other types must not."""
    iac = patch_iac_paths
    outbox_path = agent_root / "messages" / "outbox.json"
    h = _make_handler(iac)
    for t in ("agent_response", "agent_error", "agent_info"):
        out = _run(h({"agent": "main", "type": t, "content": "x"}))
        assert out.get("is_error") is not True
    # Outbox file must not exist or be empty
    if outbox_path.exists():
        assert json.loads(outbox_path.read_text()) == []
    # And no "mirrored" mention in the success messages
    # (re-run one to grab the text)
    out = _run(h({"agent": "main", "type": "agent_response", "content": "z"}))
    assert "mirrored" not in out["content"][0]["text"]


def test_send_reply_needs_human_mirror_works_when_target_is_external(
    patch_iac_paths, agent_root
):
    """Mirror must fire even when the primary target is NOT the main inbox.

    A user-blocking question can come up while the agent is chatting with
    another peer; the operator still needs to see it. The peer gets the
    full agent_needs_human envelope; main outbox gets the stripped mirror.
    """
    iac = patch_iac_paths
    bot_inbox = agent_root / "messages" / "external" / "research" / "inbox.json"
    bot_inbox.parent.mkdir(parents=True, exist_ok=True)
    bot_inbox.write_text("[]")
    _seed_agents(
        iac,
        [{"type": "external", "name": "research", "inbox": str(bot_inbox)}],
    )
    cfg = {
        "outbox_routing_rules": [
            {"description": "delegate research", "agent": "research"}
        ]
    }
    h = _make_handler(iac, cfg=cfg)
    out = _run(
        h(
            {
                "agent": "research",
                "type": "agent_needs_human",
                "content": "Need a credential to continue",
            }
        )
    )
    assert out.get("is_error") is not True
    # Primary: peer inbox got the full envelope
    assert json.loads(bot_inbox.read_text())[0]["type"] == "agent_needs_human"
    # Mirror: main outbox still got the human-notification entry
    outbox_items = json.loads((agent_root / "messages" / "outbox.json").read_text())
    assert len(outbox_items) == 1
    assert outbox_items[0]["type"] == "needs_human"
    assert outbox_items[0]["content"].startswith("[from internal agent planner] ")


def test_send_reply_via_message_id_uses_reply_to(patch_iac_paths, agent_root):
    iac = patch_iac_paths
    iac._ensure_agent_files("planner")
    main_inbox = iac.INBOX_FILE
    _seed_inbox_history(
        iac,
        "planner",
        [{"id": "the-id", "reply_to": "messages/inbox.json"}],
    )
    h = _make_handler(iac)
    out = _run(
        h(
            {
                "message_id": "the-id",
                "type": "agent_response",
                "content": "answer",
            }
        )
    )
    assert out.get("is_error") is not True
    items = json.loads(main_inbox.read_text())
    assert len(items) == 1
    assert items[0]["content"] == "answer"


def test_send_reply_message_id_unknown_fails(patch_iac_paths):
    iac = patch_iac_paths
    iac._ensure_agent_files("planner")
    _seed_inbox_history(iac, "planner", [])
    h = _make_handler(iac)
    out = _run(h({"message_id": "ghost", "type": "agent_response", "content": "x"}))
    assert out["is_error"] is True


def test_send_reply_self_talk_blocked(patch_iac_paths, agent_root):
    iac = patch_iac_paths
    iac._ensure_agent_files("planner")
    _seed_agents(
        iac,
        [
            {
                "type": "internal",
                "name": "planner",
                "inbox": str(iac._inbox_path("planner")),
            }
        ],
    )
    cfg = {"outbox_routing_rules": [{"description": "loop", "agent": "planner"}]}
    h = _make_handler(iac, cfg=cfg)
    out = _run(h({"agent": "planner", "type": "agent_response", "content": "x"}))
    assert out["is_error"] is True
    assert "own inbox" in out["content"][0]["text"]


def test_send_reply_rejects_non_inbox_filename(patch_iac_paths, agent_root):
    iac = patch_iac_paths
    bad_target = agent_root / "messages" / "external" / "x" / "chat_history.json"
    bad_target.parent.mkdir(parents=True, exist_ok=True)
    bad_target.write_text("[]")
    iac._ensure_agent_files("planner")
    _seed_inbox_history(
        iac,
        "planner",
        [{"id": "id1", "reply_to": "messages/external/x/chat_history.json"}],
    )
    h = _make_handler(iac)
    out = _run(h({"message_id": "id1", "type": "agent_response", "content": "x"}))
    assert out["is_error"] is True


# ---------------------------------------------------------------------------
# inbox drain + grouping (synchronous helper inside InternalAgentSession)
# ---------------------------------------------------------------------------


def _make_dummy_session(iac, name="planner", cfg=None):
    """Build a session shell with just enough state to call _pop_and_group_inbox.

    We bypass __init__ to avoid spinning up the SDK thread.
    """
    cfg = cfg if cfg is not None else {}
    iac._ensure_agent_files(name)
    sess = iac.InternalAgentSession.__new__(iac.InternalAgentSession)
    sess.name = name
    sess.cfg = cfg
    return sess


def test_pop_and_group_inbox_atomically_empties_and_archives(patch_iac_paths):
    iac = patch_iac_paths
    sess = _make_dummy_session(iac)
    _seed_inbox(
        iac,
        "planner",
        [
            {
                "type": "message",
                "subject": "s1",
                "content": "c1",
                "reply_to": "messages/inbox.json",
                "from": "main",
                "timestamp": "2026-05-03T12:00:00+00:00",
            },
            {
                "type": "message",
                "subject": "s2",
                "content": "c2",
                "reply_to": "messages/inbox.json",
                "from": "main",
                "timestamp": "2026-05-03T12:00:01+00:00",
            },
            {
                "type": "message",
                "subject": "s3",
                "content": "c3",
                "reply_to": "messages/external/x/inbox.json",
                "from": "external:x",
                "timestamp": "2026-05-03T12:00:02+00:00",
            },
        ],
    )

    groups = sess._pop_and_group_inbox()

    # inbox.json fully drained
    assert json.loads(iac._inbox_path("planner").read_text()) == []

    # 2 groups by reply_to
    assert set(groups.keys()) == {
        "messages/inbox.json",
        "messages/external/x/inbox.json",
    }
    assert len(groups["messages/inbox.json"]) == 2
    assert len(groups["messages/external/x/inbox.json"]) == 1

    # All popped items got daemon-stamped id + processed_at
    for items in groups.values():
        for m in items:
            assert m["id"]  # uuid4 stamped
            assert m["processed_at"]

    # And the same items are archived to inbox_history.json
    archived = json.loads(iac._inbox_history_path("planner").read_text())
    assert len(archived) == 3
    for m in archived:
        assert m["id"]
        assert m["processed_at"]


def test_pop_and_group_inbox_skips_non_message_types(patch_iac_paths):
    iac = patch_iac_paths
    sess = _make_dummy_session(iac)
    _seed_inbox(
        iac,
        "planner",
        [
            {"type": "goal", "content": "x", "timestamp": "2026-05-03T12:00:00+00:00"},
            {
                "type": "message",
                "content": "y",
                "reply_to": "messages/inbox.json",
                "from": "main",
                "timestamp": "2026-05-03T12:00:01+00:00",
            },
        ],
    )
    groups = sess._pop_and_group_inbox()
    # The "goal" entry is rejected; only the "message" entry is grouped.
    keys = list(groups.keys())
    assert keys == ["messages/inbox.json"]
    assert len(groups["messages/inbox.json"]) == 1


def test_pop_and_group_inbox_groups_missing_reply_to_under_none_key(patch_iac_paths):
    iac = patch_iac_paths
    sess = _make_dummy_session(iac)
    _seed_inbox(
        iac,
        "planner",
        [
            {
                "type": "message",
                "content": "no reply_to",
                "from": "main",
                "timestamp": "2026-05-03T12:00:00+00:00",
            },
        ],
    )
    groups = sess._pop_and_group_inbox()
    assert "__none__" in groups
    assert len(groups["__none__"]) == 1


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal stand-in for InternalAgentSession used in shutdown tests.

    The real session starts an SDK subprocess on a daemon thread; we can't
    afford to do that in unit tests. The shutdown contract we care about
    is just (a) `stop()` is called once per session and (b) the saved
    session_id file on disk is preserved so the next start can resume.
    """

    def __init__(self, name: str, session_id: str | None = None):
        self.name = name
        self.stopped = False
        self.session_id = session_id

    def stop(self) -> None:
        self.stopped = True


def test_mark_internal_agents_offline_skips_deactivated(patch_iac_paths):
    iac = patch_iac_paths
    _seed_agents(
        iac,
        [
            {"type": "internal", "name": "a", "status": "online"},
            {"type": "internal", "name": "b", "status": "deactivated"},
            {"type": "external", "name": "c", "status": "online"},
        ],
    )
    iac.mark_internal_agents_offline(["a", "b", "c"])
    agents = json.loads(iac.AGENTS_FILE.read_text())
    by_name = {a["name"]: a for a in agents}
    # Online internal → offline
    assert by_name["a"]["status"] == "offline"
    # Deactivated internal → preserved
    assert by_name["b"]["status"] == "deactivated"
    # External agent is not internal — must NOT be touched even though
    # it appeared in the names list (defensive: the daemon never asks
    # for an external name, but the helper must enforce type=internal).
    assert by_name["c"]["status"] == "online"


def test_mark_internal_agents_offline_noop_on_empty(patch_iac_paths):
    iac = patch_iac_paths
    _seed_agents(iac, [{"type": "internal", "name": "a", "status": "online"}])
    iac.mark_internal_agents_offline([])
    agents = json.loads(iac.AGENTS_FILE.read_text())
    assert agents[0]["status"] == "online"


def test_fleet_shutdown_marks_offline_and_preserves_session_id(patch_iac_paths):
    iac = patch_iac_paths
    _seed_agents(
        iac,
        [
            {"type": "internal", "name": "planner", "status": "online"},
            {"type": "internal", "name": "summarizer", "status": "online"},
        ],
    )
    # Pre-seed each session_id file on disk — `_save_session_id` is called
    # progressively by the worker after every turn; the shutdown path must
    # not delete or overwrite these files. The session sidecars now live
    # under /agent/memory/chat/<name>/ — `ensure_chat_dir` creates the
    # surrounding directory + an empty placeholder.
    iac.ensure_chat_dir("planner")
    iac.ensure_chat_dir("summarizer")
    iac._session_path("planner").write_text("sess-planner-42")
    iac._session_path("summarizer").write_text("sess-summarizer-7")

    fleet = iac.Fleet()
    s1 = _FakeSession("planner")
    s2 = _FakeSession("summarizer")
    fleet._sessions = {"planner": s1, "summarizer": s2}

    fleet.shutdown()

    # Every session was stopped exactly once
    assert s1.stopped is True
    assert s2.stopped is True
    # The fleet's live registry is cleared
    assert fleet._sessions == {}

    # agents.json was flipped to offline for every internal agent that
    # had a live session
    agents = json.loads(iac.AGENTS_FILE.read_text())
    by_name = {a["name"]: a for a in agents}
    assert by_name["planner"]["status"] == "offline"
    assert by_name["summarizer"]["status"] == "offline"

    # Session id files are preserved verbatim — next daemon start can
    # resume the conversation.
    assert iac._session_path("planner").read_text() == "sess-planner-42"
    assert iac._session_path("summarizer").read_text() == "sess-summarizer-7"


def test_fleet_shutdown_with_no_sessions_is_a_noop(patch_iac_paths):
    iac = patch_iac_paths
    _seed_agents(iac, [{"type": "internal", "name": "planner", "status": "online"}])
    fleet = iac.Fleet()
    # Empty fleet — shutdown must NOT touch agents.json.
    fleet.shutdown()
    agents = json.loads(iac.AGENTS_FILE.read_text())
    assert agents[0]["status"] == "online"


def test_fleet_shutdown_continues_when_one_session_stop_raises(patch_iac_paths):
    iac = patch_iac_paths
    _seed_agents(
        iac,
        [
            {"type": "internal", "name": "a", "status": "online"},
            {"type": "internal", "name": "b", "status": "online"},
        ],
    )

    class _ExplodingSession(_FakeSession):
        def stop(self) -> None:  # noqa: D401
            raise RuntimeError("boom")

    bad = _ExplodingSession("a")
    good = _FakeSession("b")
    fleet = iac.Fleet()
    fleet._sessions = {"a": bad, "b": good}

    fleet.shutdown()

    # The other session still ran to completion …
    assert good.stopped is True
    # … and BOTH agents flipped to offline (the offline-flip must happen
    # before any stop() runs, so a single noisy session can't strand
    # peers as 'online' in agents.json).
    agents = json.loads(iac.AGENTS_FILE.read_text())
    by_name = {a["name"]: a for a in agents}
    assert by_name["a"]["status"] == "offline"
    assert by_name["b"]["status"] == "offline"


# ---------------------------------------------------------------------------
# chat_history persistence — must NEVER truncate
# ---------------------------------------------------------------------------


def _make_session_skeleton(iac, name: str = "x"):
    """Build an InternalAgentSession-like object stubbed enough to call
    `_append_chat_records` directly, without going through SDK / threads.
    """
    sess = iac.InternalAgentSession.__new__(iac.InternalAgentSession)
    sess.name = name
    sess._chat_history = []
    iac._ensure_agent_files(name)
    return sess


def test_append_chat_records_never_truncates_disk_or_memory(patch_iac_paths):
    iac = patch_iac_paths
    sess = _make_session_skeleton(iac, "x")
    # Write far more than the old 1000-record cap to prove no truncation.
    n_turns = 1500
    for i in range(n_turns):
        sess._append_chat_records(
            ids=[f"id-{i}"],
            merged_reply_to=None,
            user_text=f"u{i}",
            assistant_text=f"a{i}",
            session_id=None,
            cost_usd=None,
            duration_ms=None,
            is_error=False,
        )
    # Each turn appends one user + one assistant record.
    on_disk = json.loads(iac._chat_history_path("x").read_text())
    assert len(on_disk) == n_turns * 2
    assert len(sess._chat_history) == n_turns * 2
    # First record must still be the very first turn (no head trimming).
    assert on_disk[0]["content"] == "u0"
    assert on_disk[-1]["content"] == f"a{n_turns - 1}"


# ---------------------------------------------------------------------------
# _select_history_for_prompt — 24h window with 20-record fallback,
# extended at boundaries to keep user/assistant pairs together.
# ---------------------------------------------------------------------------


def _ts(offset_minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)).isoformat()


def _pair(i: int, offset_minutes: int) -> list[dict]:
    sid = [f"id-{i}"]
    return [
        {
            "role": "user",
            "ts": _ts(offset_minutes),
            "content": f"u{i}",
            "source_ids": sid,
        },
        {
            "role": "assistant",
            "ts": _ts(offset_minutes),
            "content": f"a{i}",
            "source_ids": sid,
        },
    ]


def test_select_history_empty(patch_iac_paths):
    assert patch_iac_paths._select_history_for_prompt([]) == []


def test_select_history_includes_all_records_within_24h(patch_iac_paths):
    iac = patch_iac_paths
    history: list[dict] = []
    # 30 turns (60 records), all within last 24h, well above the 20 soft limit.
    for i in range(30):
        history.extend(_pair(i, offset_minutes=-(60 + i)))
    selected = iac._select_history_for_prompt(history)
    # All 60 must be included since they fall inside the 24h window.
    assert len(selected) == 60
    assert selected[0]["content"] == "u0"
    assert selected[-1]["content"] == "a29"


def test_select_history_falls_back_to_soft_limit_when_all_old(patch_iac_paths):
    iac = patch_iac_paths
    history: list[dict] = []
    # 30 turns, all older than 24h.
    for i in range(30):
        history.extend(_pair(i, offset_minutes=-(60 * 24 + 60 + i)))
    selected = iac._select_history_for_prompt(history)
    # Soft limit 20 records → trailing 20 of the 60 records.
    # Last record is assistant; preceding 20 happens to start on a user → no
    # boundary extension needed.
    assert len(selected) == 20
    assert selected[-1]["content"] == "a29"
    assert selected[0]["role"] == "user"


def test_select_history_extends_to_keep_pair_at_tail(patch_iac_paths):
    """If the soft-limit window ends on a `user` record whose `assistant`
    reply lives one slot later in history, the assistant must be pulled in."""
    iac = patch_iac_paths
    # Build 11 old turns (22 records). Soft limit is 20 → window is records
    # [2 .. 21]. Index 2 is `assistant` of turn 1, index 21 is `assistant`
    # of turn 10.  To force the tail extension, append a single trailing
    # `user` whose matching `assistant` is past the 20-record cut: drop the
    # last assistant out of the slice by appending a 21st record (orphan
    # user) then its assistant.
    #
    # Simplest construction: 10 old pairs (20 records), then a final pair —
    # window of last 20 lands on records [2..21]. Record 21 is the final
    # assistant; pair stays intact. To exercise extension, push the cut so
    # the last selected record is a user: insert an extra orphan record
    # before the final pair.
    history: list[dict] = []
    for i in range(10):
        history.extend(_pair(i, offset_minutes=-(60 * 24 + 60 + i)))  # all old
    # One stray old assistant with no matching user — shifts parity by one.
    history.append(
        {
            "role": "assistant",
            "ts": _ts(-(60 * 24 + 30)),
            "content": "stray",
            "source_ids": ["stray-id"],
        }
    )
    # Final pair (still old).
    history.extend(_pair(99, offset_minutes=-(60 * 24 + 10)))
    # 22 records total, all old → fallback. Trailing 20 = indices [2..21].
    # Index 21 == final assistant (a99); index 2 == assistant of turn 1.
    # No tail extension needed (already assistant), but HEAD extension
    # should pull in the matching user of turn 1.
    selected = iac._select_history_for_prompt(history)
    assert selected[0]["content"] == "u1"  # extended back from assistant a1
    assert selected[-1]["content"] == "a99"
    assert len(selected) == 21


def test_select_history_extends_tail_when_user_orphan_at_end(patch_iac_paths):
    """When the trailing-20 window ends on a user whose assistant is the
    next record, the assistant gets pulled in (so the LLM sees the reply).
    """
    iac = patch_iac_paths
    history: list[dict] = []
    # 10 old pairs.
    for i in range(10):
        history.extend(_pair(i, offset_minutes=-(60 * 24 + 60 + i)))
    # Insert a single user-only record near the end of the trailing-20
    # boundary, then a follow-up pair to ensure the orphan ends the window.
    #
    # Construction: prepend two extra orphan-user records so the
    # trailing-20 ends on a user record whose assistant exists at index+1.
    history.insert(
        10,
        {
            "role": "user",
            "ts": _ts(-(60 * 24 + 50)),
            "content": "orphan-u",
            "source_ids": ["orphan"],
        },
    )
    history.insert(
        11,
        {
            "role": "assistant",
            "ts": _ts(-(60 * 24 + 49)),
            "content": "orphan-a",
            "source_ids": ["orphan"],
        },
    )
    # 22 records total, all old. Trailing 20 = indices [2..21].
    # Confirm this hits the tail-user-extension branch by truncating the
    # tail one record earlier: pop the last record so the window ends on a
    # user with its assistant just past the cut.
    history.pop()  # drop final assistant → end window on its `user`
    # Now 21 records; trailing 20 = indices [1..20]. index 20 must be a user.
    assert history[20]["role"] == "user"
    selected = iac._select_history_for_prompt(history)
    # Tail extension can't fire (no record at index 21 anymore) — verify
    # the function does NOT crash and stays within bounds.
    assert selected[-1] is history[20]


def test_select_history_skips_records_with_bad_ts(patch_iac_paths):
    iac = patch_iac_paths
    history = [
        {"role": "user", "ts": "not-a-date", "content": "u0", "source_ids": ["x"]},
        {"role": "assistant", "ts": "not-a-date", "content": "a0", "source_ids": ["x"]},
    ]
    history.extend(_pair(1, offset_minutes=-30))  # within 24h
    selected = iac._select_history_for_prompt(history)
    # Only the well-timestamped pair anchors the recent window; bad-ts
    # records sit outside it and aren't selected.
    assert [m["content"] for m in selected] == ["u1", "a1"]


def test_select_history_naive_ts_treated_as_utc(patch_iac_paths):
    iac = patch_iac_paths
    naive_now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    history = [
        {"role": "user", "ts": naive_now, "content": "u", "source_ids": ["x"]},
        {"role": "assistant", "ts": naive_now, "content": "a", "source_ids": ["x"]},
    ]
    selected = iac._select_history_for_prompt(history)
    assert len(selected) == 2


# ---------------------------------------------------------------------------
# _build_system_prompt — only the chat-history block changed.
# ---------------------------------------------------------------------------


def test_build_system_prompt_omits_history_block_when_no_records(patch_iac_paths):
    out = patch_iac_paths._build_system_prompt(chat_history=[])
    assert "<previous_chat_history>" not in out


def test_build_system_prompt_includes_selected_records(patch_iac_paths):
    iac = patch_iac_paths
    history = []
    for i in range(3):
        history.extend(_pair(i, offset_minutes=-(60 + i)))
    out = iac._build_system_prompt(chat_history=history)
    assert "<previous_chat_history>" in out
    # Every selected record (all within 24h) must appear, in order.
    for i in range(3):
        assert f"**user**: u{i}" in out
        assert f"**assistant**: a{i}" in out


# ---------------------------------------------------------------------------
# _build_options — per-agent `model` override flows through to the SDK.
# ---------------------------------------------------------------------------


def _make_options_session(iac, cfg: dict, name: str = "x"):
    sess = iac.InternalAgentSession.__new__(iac.InternalAgentSession)
    sess.name = name
    sess.cfg = cfg
    sess._chat_history = []
    sess._session_id = None
    iac._ensure_agent_files(name)
    return sess


def test_build_options_passes_model_alias_through(patch_iac_paths):
    iac = patch_iac_paths
    sess = _make_options_session(iac, cfg={"model": "haiku"})
    options = sess._build_options()
    assert options.model == "haiku"


def test_build_options_strips_whitespace_around_model(patch_iac_paths):
    iac = patch_iac_paths
    sess = _make_options_session(iac, cfg={"model": "  sonnet  "})
    options = sess._build_options()
    assert options.model == "sonnet"


def test_build_options_passes_full_model_id_through(patch_iac_paths):
    iac = patch_iac_paths
    sess = _make_options_session(iac, cfg={"model": "claude-opus-4-7"})
    options = sess._build_options()
    assert options.model == "claude-opus-4-7"


@pytest.mark.parametrize("bad", [None, "", "   ", 42, ["sonnet"], {"x": 1}])
def test_build_options_omits_model_when_absent_or_invalid(patch_iac_paths, bad):
    iac = patch_iac_paths
    cfg: dict = {} if bad is None else {"model": bad}
    sess = _make_options_session(iac, cfg=cfg)
    options = sess._build_options()
    assert options.model is None


def test_model_round_trip_register_to_build_options(patch_iac_paths, monkeypatch):
    """End-to-end contract: a value persisted by the registration script
    must be picked up verbatim by the daemon's `_build_options`.
    """
    import argparse

    import register_internal_agent as ria

    iac = patch_iac_paths
    monkeypatch.setattr(ria, "AGENTS_FILE", iac.AGENTS_FILE)
    monkeypatch.setattr(ria, "INTERNAL_DIR", iac.INTERNAL_DIR)

    args = argparse.Namespace(
        name="planner",
        responsibilities="r",
        system_prompt_file=None,
        system_prompt_inline=None,
        outbox_routing_rules_file=None,
        outbox_routing_rules_inline=None,
        model="  haiku  ",  # whitespace must survive normalization on both sides
    )
    assert ria.cmd_register(args) == 0

    agents = json.loads(iac.AGENTS_FILE.read_text())
    cfg = next(a for a in agents if a.get("name") == "planner")
    assert cfg["model"] == "haiku"  # script stripped + persisted

    sess = _make_options_session(iac, cfg=cfg, name="planner")
    options = sess._build_options()
    assert options.model == "haiku"  # daemon read it back verbatim


# ---------------------------------------------------------------------------
# _consume_control_flags — operator-driven clear_chat / clear_session
# ---------------------------------------------------------------------------


def _make_control_session(iac, name: str, cfg: dict):
    """Build a session skeleton wired up enough to exercise the
    control-flag pipeline without a real SDK or thread.
    """
    sess = iac.InternalAgentSession.__new__(iac.InternalAgentSession)
    sess.name = name
    sess.cfg = cfg
    sess._sdk = None
    sess._session_id = None
    sess._chat_history = []
    iac._ensure_agent_files(name)
    return sess


@pytest.mark.parametrize(
    "control_value",
    [
        None,  # key absent
        {},  # empty dict
        "not-a-dict",  # wrong type — must hit the isinstance(ctl, dict) guard
        [],  # wrong type (list)
    ],
    ids=["missing", "empty_dict", "string", "list"],
)
def test_consume_control_flags_noop_when_no_control(patch_iac_paths, control_value):
    iac = patch_iac_paths
    cfg = {"type": "internal", "name": "p", "status": "online"}
    if control_value is not None:
        cfg[iac.CONTROL_FIELD] = control_value
    _seed_agents(iac, [cfg])
    sess = _make_control_session(iac, "p", cfg=cfg)
    sess._chat_history = [{"role": "user", "content": "keep me"}]
    iac._chat_history_path("p").write_text(json.dumps(sess._chat_history))

    _run(sess._consume_control_flags())

    # Nothing was changed on disk or in memory.
    assert json.loads(iac._chat_history_path("p").read_text()) == [
        {"role": "user", "content": "keep me"}
    ]
    assert sess._chat_history == [{"role": "user", "content": "keep me"}]


def test_consume_control_flags_clear_chat_wipes_disk_memory_and_strips_flag(
    patch_iac_paths,
):
    iac = patch_iac_paths
    cfg = {
        "type": "internal",
        "name": "p",
        "status": "online",
        iac.CONTROL_FIELD: {iac.CONTROL_CLEAR_CHAT: True},
    }
    _seed_agents(iac, [cfg])
    sess = _make_control_session(iac, "p", cfg=cfg)
    sess._chat_history = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    iac._chat_history_path("p").write_text(json.dumps(sess._chat_history))

    _run(sess._consume_control_flags())

    # On-disk and in-memory chat are both wiped.
    assert json.loads(iac._chat_history_path("p").read_text()) == []
    assert sess._chat_history == []
    # Flag (and the empty control dict) was stripped from agents.json.
    agents_after = json.loads(iac.AGENTS_FILE.read_text())
    assert iac.CONTROL_FIELD not in agents_after[0]
    # Mirrored on cfg too.
    assert iac.CONTROL_FIELD not in sess.cfg


def test_consume_control_flags_clear_session_resets_sdk_and_strips_flag(
    patch_iac_paths, monkeypatch
):
    iac = patch_iac_paths
    cfg = {
        "type": "internal",
        "name": "p",
        "status": "online",
        iac.CONTROL_FIELD: {iac.CONTROL_CLEAR_SESSION: True},
    }
    _seed_agents(iac, [cfg])
    sess = _make_control_session(iac, "p", cfg=cfg)
    sess._session_id = "old-session-id"

    # Seed the on-disk session file the daemon should delete.
    session_file = iac._session_path("p")
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("old-session-id")

    # Single ordered event log — pins down unlink → disconnect → connect.
    # Asserting on three separate boolean lists would let a future refactor
    # that reconnects before unlinking still pass.
    events: list[str] = []

    # Wrap unlink so we can record the order. (We can't easily intercept
    # Path.unlink globally; rely on file presence + the events list below.)
    real_unlink = type(session_file).unlink

    def _tracking_unlink(self, *a, **kw):
        if self == session_file:
            events.append("unlink")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(type(session_file), "unlink", _tracking_unlink, raising=True)

    class _FakeSDK:
        async def disconnect(self):
            events.append("disconnect")

    sess._sdk = _FakeSDK()

    async def _fake_connect(self):
        events.append("connect")
        self._sdk = _FakeSDK()

    monkeypatch.setattr(
        iac.InternalAgentSession, "_connect_sdk", _fake_connect, raising=True
    )

    _run(sess._consume_control_flags())

    # Order matters: file gone before disconnect, disconnect before reconnect.
    assert events == ["unlink", "disconnect", "connect"]
    assert sess._session_id is None
    assert not session_file.exists()
    # Flag stripped after a successful reconnect.
    agents_after = json.loads(iac.AGENTS_FILE.read_text())
    assert iac.CONTROL_FIELD not in agents_after[0]


def test_consume_control_flags_clear_session_keeps_flag_on_reconnect_failure(
    patch_iac_paths, monkeypatch
):
    iac = patch_iac_paths
    cfg = {
        "type": "internal",
        "name": "p",
        "status": "online",
        iac.CONTROL_FIELD: {iac.CONTROL_CLEAR_SESSION: True},
    }
    _seed_agents(iac, [cfg])
    sess = _make_control_session(iac, "p", cfg=cfg)
    sess._session_id = "old-session-id"

    # Seed an existing SDK + session file so the disconnect branch runs
    # before the failing reconnect.
    session_file = iac._session_path("p")
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("old-session-id")

    disconnect_calls: list[bool] = []

    class _FakeSDK:
        async def disconnect(self):
            disconnect_calls.append(True)

    sess._sdk = _FakeSDK()

    async def _failing_connect(self):
        raise RuntimeError("no SDK for you")

    monkeypatch.setattr(
        iac.InternalAgentSession, "_connect_sdk", _failing_connect, raising=True
    )

    _run(sess._consume_control_flags())

    # Partial side effects of clear_session that happened *before* the
    # failing reconnect — pin them down so a future refactor can't
    # silently re-order them:
    #   - session file deleted
    #   - in-memory session id cleared
    #   - existing SDK was disconnected and dropped
    assert disconnect_calls == [True]
    assert not session_file.exists()
    assert sess._session_id is None
    assert sess._sdk is None
    # Reconnect failed — agents.json must still carry the flag so the
    # next sweep retries.
    agents_after = json.loads(iac.AGENTS_FILE.read_text())
    assert (
        agents_after[0].get(iac.CONTROL_FIELD, {}).get(iac.CONTROL_CLEAR_SESSION)
        is True
    )


def test_clear_control_keys_removes_empty_control_dict(patch_iac_paths):
    iac = patch_iac_paths
    cfg = {
        "type": "internal",
        "name": "p",
        "status": "online",
        iac.CONTROL_FIELD: {
            iac.CONTROL_CLEAR_CHAT: True,
            iac.CONTROL_CLEAR_SESSION: True,
        },
    }
    _seed_agents(iac, [cfg])
    sess = _make_control_session(iac, "p", cfg=cfg)

    sess._clear_control_keys([iac.CONTROL_CLEAR_CHAT, iac.CONTROL_CLEAR_SESSION])

    agents_after = json.loads(iac.AGENTS_FILE.read_text())
    # Both flags removed AND the empty control dict pruned entirely.
    assert iac.CONTROL_FIELD not in agents_after[0]
    assert iac.CONTROL_FIELD not in sess.cfg


def test_clear_control_keys_preserves_unrelated_control_keys(patch_iac_paths):
    iac = patch_iac_paths
    cfg = {
        "type": "internal",
        "name": "p",
        "status": "online",
        iac.CONTROL_FIELD: {
            iac.CONTROL_CLEAR_CHAT: True,
            "future_flag": "keep-me",
        },
    }
    _seed_agents(iac, [cfg])
    sess = _make_control_session(iac, "p", cfg=cfg)

    sess._clear_control_keys([iac.CONTROL_CLEAR_CHAT])

    agents_after = json.loads(iac.AGENTS_FILE.read_text())
    # Targeted key gone; unrelated key preserved; control dict intact.
    assert iac.CONTROL_CLEAR_CHAT not in agents_after[0][iac.CONTROL_FIELD]
    assert agents_after[0][iac.CONTROL_FIELD].get("future_flag") == "keep-me"


# ---------------------------------------------------------------------------
# clear_chat archive + control_audit log
# ---------------------------------------------------------------------------


def _read_audit_log(iac, agent_name: str | None = None) -> list[dict]:
    """Parse the JSONL global audit log into a list of dicts, optionally
    filtered to a single agent name.
    """
    path = iac.CONTROL_AUDIT_LOG
    if not path.exists():
        return []
    entries: list[dict] = []
    for raw in path.read_text().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        rec = json.loads(raw)
        if agent_name is None or rec.get("agent") == agent_name:
            entries.append(rec)
    return entries


def test_clear_chat_archives_records_before_truncation(patch_iac_paths):
    iac = patch_iac_paths
    cfg = {
        "type": "internal",
        "name": "p",
        "status": "online",
        iac.CONTROL_FIELD: {iac.CONTROL_CLEAR_CHAT: True},
    }
    _seed_agents(iac, [cfg])
    sess = _make_control_session(iac, "p", cfg=cfg)
    seed = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    sess._chat_history = list(seed)
    iac._chat_history_path("p").write_text(json.dumps(seed))

    _run(sess._consume_control_flags())

    # Live history wiped
    assert json.loads(iac._chat_history_path("p").read_text()) == []
    assert sess._chat_history == []
    # Archive contains every prior record, in order
    archived = json.loads(iac._chat_archive_path("p").read_text())
    assert archived == seed


def test_clear_chat_archive_appends_across_multiple_clears(patch_iac_paths):
    iac = patch_iac_paths
    sess = _make_control_session(
        iac,
        "p",
        cfg={"type": "internal", "name": "p", "status": "online"},
    )

    # First clear: 2 records.
    iac._chat_history_path("p").write_text(
        json.dumps([{"role": "user", "content": "first-batch"}])
    )
    sess._do_clear_chat([])

    # Second clear: 1 record.
    iac._chat_history_path("p").write_text(
        json.dumps([{"role": "assistant", "content": "second-batch"}])
    )
    sess._do_clear_chat([])

    archived = json.loads(iac._chat_archive_path("p").read_text())
    # Both batches in the archive, in order — archive is append-only.
    assert [m["content"] for m in archived] == ["first-batch", "second-batch"]


def test_clear_chat_with_empty_history_still_writes_audit(patch_iac_paths):
    iac = patch_iac_paths
    sess = _make_control_session(
        iac,
        "p",
        cfg={"type": "internal", "name": "p", "status": "online"},
    )
    iac._chat_history_path("p").write_text("[]")

    sess._do_clear_chat([])

    # Archive remains empty (nothing to copy).
    assert json.loads(iac._chat_archive_path("p").read_text()) == []
    # Audit still records the action with archived_count=0.
    audit = _read_audit_log(iac, "p")
    assert len(audit) == 1
    assert audit[0]["action"] == iac.CONTROL_CLEAR_CHAT
    assert audit[0]["ok"] is True
    assert audit[0]["archived_count"] == 0
    assert "ts" in audit[0]


def test_clear_chat_aborts_when_archive_fails(patch_iac_paths, monkeypatch):
    """If the archive step fails, the live chat_history.json must NOT be
    truncated and the control flag must stay set so the next sweep retries.
    """
    iac = patch_iac_paths
    cfg = {
        "type": "internal",
        "name": "p",
        "status": "online",
        iac.CONTROL_FIELD: {iac.CONTROL_CLEAR_CHAT: True},
    }
    _seed_agents(iac, [cfg])
    sess = _make_control_session(iac, "p", cfg=cfg)
    seed = [{"role": "user", "content": "must-not-be-lost"}]
    sess._chat_history = list(seed)
    iac._chat_history_path("p").write_text(json.dumps(seed))

    def _failing_archive(name):
        return False, 0, "simulated disk full"

    monkeypatch.setattr(iac, "_archive_chat_history", _failing_archive)

    _run(sess._consume_control_flags())

    # Live history preserved.
    assert json.loads(iac._chat_history_path("p").read_text()) == seed
    assert sess._chat_history == seed
    # Flag still set so next sweep retries.
    agents_after = json.loads(iac.AGENTS_FILE.read_text())
    assert (
        agents_after[0].get(iac.CONTROL_FIELD, {}).get(iac.CONTROL_CLEAR_CHAT) is True
    )
    # Audit captured the failure.
    audit = _read_audit_log(iac, "p")
    assert len(audit) == 1
    assert audit[0]["action"] == iac.CONTROL_CLEAR_CHAT
    assert audit[0]["ok"] is False
    assert audit[0]["error"] == "simulated disk full"


def test_clear_chat_refuses_when_chat_history_is_not_a_list(patch_iac_paths):
    """If `chat_history.json` is corrupt or hand-edited into a non-list
    shape, the archive helper must REFUSE to truncate so the operator
    can recover the original bytes manually.
    """
    iac = patch_iac_paths
    cfg = {
        "type": "internal",
        "name": "p",
        "status": "online",
        iac.CONTROL_FIELD: {iac.CONTROL_CLEAR_CHAT: True},
    }
    _seed_agents(iac, [cfg])
    sess = _make_control_session(iac, "p", cfg=cfg)
    # Corrupt content — a JSON object instead of a list.
    iac._chat_history_path("p").write_text(json.dumps({"oops": "wrong shape"}))

    _run(sess._consume_control_flags())

    # Original bytes preserved verbatim.
    assert json.loads(iac._chat_history_path("p").read_text()) == {
        "oops": "wrong shape"
    }
    # Flag still set so the next sweep retries (or, in practice, an
    # operator notices and fixes the file).
    agents_after = json.loads(iac.AGENTS_FILE.read_text())
    assert (
        agents_after[0].get(iac.CONTROL_FIELD, {}).get(iac.CONTROL_CLEAR_CHAT) is True
    )
    # Audit captured the failure with a descriptive error.
    audit = _read_audit_log(iac, "p")
    assert len(audit) == 1
    assert audit[0]["ok"] is False
    assert "not a JSON list" in audit[0]["error"]


def test_clear_chat_audit_on_success_includes_archive_metadata(patch_iac_paths):
    iac = patch_iac_paths
    sess = _make_control_session(
        iac,
        "p",
        cfg={"type": "internal", "name": "p", "status": "online"},
    )
    iac._chat_history_path("p").write_text(
        json.dumps([{"role": "user", "content": "x"}])
    )

    sess._do_clear_chat([])

    audit = _read_audit_log(iac, "p")
    assert len(audit) == 1
    e = audit[0]
    assert e["action"] == iac.CONTROL_CLEAR_CHAT
    assert e["ok"] is True
    assert e["archived_count"] == 1
    assert e["archive_path"] == str(iac._chat_archive_path("p"))


# ---------------------------------------------------------------------------
# clear_session control_audit log
# ---------------------------------------------------------------------------


def test_clear_session_audit_on_success(patch_iac_paths, monkeypatch):
    iac = patch_iac_paths
    cfg = {
        "type": "internal",
        "name": "p",
        "status": "online",
        iac.CONTROL_FIELD: {iac.CONTROL_CLEAR_SESSION: True},
    }
    _seed_agents(iac, [cfg])
    sess = _make_control_session(iac, "p", cfg=cfg)
    sess._session_id = "old-session-id"

    async def _fake_connect(self):
        self._session_id = "new-session-id"

    monkeypatch.setattr(
        iac.InternalAgentSession, "_connect_sdk", _fake_connect, raising=True
    )

    _run(sess._consume_control_flags())

    audit = _read_audit_log(iac, "p")
    assert len(audit) == 1
    e = audit[0]
    assert e["action"] == iac.CONTROL_CLEAR_SESSION
    assert e["ok"] is True
    assert e["prior_session_id"] == "old-session-id"
    assert e["new_session_id"] == "new-session-id"


def test_clear_session_audit_on_reconnect_failure(patch_iac_paths, monkeypatch):
    iac = patch_iac_paths
    cfg = {
        "type": "internal",
        "name": "p",
        "status": "online",
        iac.CONTROL_FIELD: {iac.CONTROL_CLEAR_SESSION: True},
    }
    _seed_agents(iac, [cfg])
    sess = _make_control_session(iac, "p", cfg=cfg)
    sess._session_id = "old-session-id"

    async def _failing_connect(self):
        raise RuntimeError("no SDK for you")

    monkeypatch.setattr(
        iac.InternalAgentSession, "_connect_sdk", _failing_connect, raising=True
    )

    _run(sess._consume_control_flags())

    audit = _read_audit_log(iac, "p")
    assert len(audit) == 1
    e = audit[0]
    assert e["action"] == iac.CONTROL_CLEAR_SESSION
    assert e["ok"] is False
    assert e["error"] == "no SDK for you"
    assert e["prior_session_id"] == "old-session-id"
    # New id is None because the failed reconnect never set one.
    assert e["new_session_id"] is None


def test_audit_log_appends_across_multiple_actions(patch_iac_paths, monkeypatch):
    """Two sequential clear_chat ops produce two audit entries in order."""
    iac = patch_iac_paths
    sess = _make_control_session(
        iac,
        "p",
        cfg={"type": "internal", "name": "p", "status": "online"},
    )

    iac._chat_history_path("p").write_text(
        json.dumps([{"role": "user", "content": "round-1"}])
    )
    sess._do_clear_chat([])
    iac._chat_history_path("p").write_text(
        json.dumps([{"role": "user", "content": "round-2"}])
    )
    sess._do_clear_chat([])

    audit = _read_audit_log(iac, "p")
    assert len(audit) == 2
    assert all(e["action"] == iac.CONTROL_CLEAR_CHAT for e in audit)
    assert [e["archived_count"] for e in audit] == [1, 1]


def test_audit_log_is_global_and_carries_agent_field(patch_iac_paths):
    """Two different agents writing audit entries land in the SAME global
    JSONL file and each line carries an `agent` field for filtering.
    """
    iac = patch_iac_paths
    sess_a = _make_control_session(
        iac,
        "alpha",
        cfg={"type": "internal", "name": "alpha", "status": "online"},
    )
    sess_b = _make_control_session(
        iac,
        "beta",
        cfg={"type": "internal", "name": "beta", "status": "online"},
    )

    iac._chat_history_path("alpha").write_text(
        json.dumps([{"role": "user", "content": "from-alpha"}])
    )
    iac._chat_history_path("beta").write_text(
        json.dumps([{"role": "user", "content": "from-beta"}])
    )
    sess_a._do_clear_chat([])
    sess_b._do_clear_chat([])

    # Single global file, JSONL-encoded.
    raw_lines = [
        ln for ln in iac.CONTROL_AUDIT_LOG.read_text().splitlines() if ln.strip()
    ]
    assert len(raw_lines) == 2
    parsed = [json.loads(ln) for ln in raw_lines]
    # Each line has an `agent` field; ordering reflects write order.
    assert [e["agent"] for e in parsed] == ["alpha", "beta"]
    # Helper-level filter pulls just one agent's entries.
    assert [e["agent"] for e in _read_audit_log(iac, "alpha")] == ["alpha"]
    assert [e["agent"] for e in _read_audit_log(iac, "beta")] == ["beta"]
    # Helper without filter returns both.
    assert len(_read_audit_log(iac)) == 2
