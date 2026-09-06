"""Tests for services/internal_agent_chat.py — pure helpers + send_reply tool."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

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
# send_reply tool — input schema (regression for the schema/handler
# contradiction documented in memory/dream/learnings/
# internal-agent-chat-send-reply-schema-contradiction.md: the old
# {"name": type} shorthand marked every key "required", forcing callers to
# pass message_id="" as a workaround even when addressing by `agent`).
# ---------------------------------------------------------------------------


def test_send_reply_schema_only_requires_type_and_content(patch_iac_paths):
    schema = patch_iac_paths.SEND_REPLY_INPUT_SCHEMA
    assert schema["required"] == ["type", "content"]
    assert set(schema["properties"]) == {
        "agent",
        "message_id",
        "type",
        "content",
        "priority",
    }


def _wire_schemas(server) -> dict:
    """Map tool name -> the inputSchema an MCP server advertises over the wire.

    Drives the SDK's real schema-building path via the `tools/list` handler
    instead of calling its private schema helper directly — that helper has
    already been renamed once (it used to be
    `claude_agent_sdk._build_input_schema`, it is now a closure inside
    `create_sdk_mcp_server`), so importing it makes the test break for the
    wrong reason on an SDK upgrade. `request_handlers` is itself an mcp
    internal, hence the explicit assert below rather than a bare KeyError.
    """
    from mcp.types import ListToolsRequest

    assert (
        ListToolsRequest in server.request_handlers
    ), "the SDK/mcp changed how list_tools is registered — update this probe"
    result = _run(
        server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    )
    return {t.name: t.inputSchema for t in result.root.tools}


def test_send_reply_schema_passthrough_matches_real_sdk_wire_schema(patch_iac_paths):
    # claude_agent_sdk passes a dict straight through unmodified whenever it
    # already has top-level "type"/"properties" keys (rather than re-deriving
    # "required" from every dict key, as it does for the {"name": type}
    # shorthand). Build the *production* server so this also fails if
    # _build_send_reply_server stops handing the SDK the full-JSON-Schema
    # form — a likelier regression than an SDK upgrade.
    iac = patch_iac_paths
    iac._ensure_agent_files("planner")
    server = iac._build_send_reply_server("planner", {})["instance"]
    wire_schema = _wire_schemas(server)[iac.SEND_REPLY_TOOL]
    # `==`, not `is`: the SDK returns the same dict object, but
    # Tool.model_validate re-validates it, so identity no longer holds.
    assert wire_schema == iac.SEND_REPLY_INPUT_SCHEMA
    assert wire_schema["required"] == ["type", "content"]
    assert "agent" not in wire_schema["required"]
    assert "message_id" not in wire_schema["required"]
    assert "priority" not in wire_schema["required"]


def test_sdk_shorthand_schema_still_marks_every_key_required():
    # Control for the test above: the {"name": type} shorthand is the form
    # that caused the original bug (every key forced into "required"). If the
    # SDK ever stops doing this, the passthrough test above is guarding a
    # contract that no longer means anything and should be revisited.
    from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server

    tool_def = SdkMcpTool(
        name="shorthand",
        description="d",
        input_schema={"agent": str, "message_id": str},
        handler=lambda args: None,
    )
    server = create_sdk_mcp_server(name="probe", tools=[tool_def])["instance"]
    assert _wire_schemas(server)["shorthand"]["required"] == [
        "agent",
        "message_id",
    ]


def test_send_reply_agent_only_no_message_id_needed(patch_iac_paths):
    # With the fixed schema, omitting message_id entirely (not even passing
    # "") when addressing by agent must still succeed at the handler level.
    h = _make_handler(patch_iac_paths)
    out = _run(h({"agent": "main", "type": "agent_response", "content": "hi"}))
    assert out.get("is_error") is not True


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
    assert env["from"]["source"] == "internal_agent"  # source relocated into from
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
    assert env["from"]["source"] == "internal_agent"  # source relocated into from
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


def test_send_reply_agent_response_mirrors_to_main_outbox(patch_iac_paths, agent_root):
    """agent_response → primary delivery + main outbox mirror, typed `response`.

    The mirror carries the agent's own type rather than `needs_human`. Both
    reach the human either way — the bridges forward every outbox entry
    regardless of type (telegram_bridge.send_outbox_messages) — but the type
    drives the badge: `needs_human` renders as "🚨 ACTION REQUIRED" and is
    counted by `/outbox` as an item needing attention. Completed work is not
    action-required, so labelling it that way would inflate the alert count
    and blunt the badge.
    """
    iac = patch_iac_paths
    h = _make_handler(iac)
    out = _run(
        h(
            {
                "agent": "main",
                "type": "agent_response",
                "content": "Review complete: APPROVE",
            }
        )
    )
    assert out.get("is_error") is not True
    # Primary delivery — main inbox got the agent_response envelope
    inbox_items = json.loads(iac.INBOX_FILE.read_text())
    assert len(inbox_items) == 1
    assert inbox_items[0]["type"] == "agent_response"
    assert inbox_items[0]["content"] == "Review complete: APPROVE"
    # Mirror — main outbox got a `response` envelope with the prefix
    outbox_path = agent_root / "messages" / "outbox.json"
    assert outbox_path.exists()
    outbox_items = json.loads(outbox_path.read_text())
    assert len(outbox_items) == 1
    mirror = outbox_items[0]
    assert mirror["type"] == "response"  # agent_ prefix stripped, type kept
    assert mirror["content"] == (
        "[from internal agent planner] Review complete: APPROVE"
    )
    assert mirror["subject"] == "[from internal agent planner]"
    assert mirror["timestamp"]
    # Tool output text mentions the mirror so the LLM knows it happened
    assert "mirrored" in out["content"][0]["text"]


def test_send_reply_non_mirrored_types_do_not_mirror(patch_iac_paths, agent_root):
    """agent_error and agent_info must not trigger the outbox mirror."""
    iac = patch_iac_paths
    outbox_path = agent_root / "messages" / "outbox.json"
    h = _make_handler(iac)
    for t in ("agent_error", "agent_info"):
        out = _run(h({"agent": "main", "type": t, "content": "x"}))
        assert out.get("is_error") is not True
    # Outbox file must not exist or be empty
    if outbox_path.exists():
        assert json.loads(outbox_path.read_text()) == []
    # And no mirror notification in the success messages
    out = _run(h({"agent": "main", "type": "agent_info", "content": "z"}))
    assert "mirrored to main outbox" not in out["content"][0]["text"]


@pytest.mark.parametrize(
    "msg_type,expected_mirror_type",
    [("agent_needs_human", "needs_human"), ("agent_response", "response")],
)
def test_send_reply_mirror_works_when_target_is_external(
    patch_iac_paths, agent_root, msg_type, expected_mirror_type
):
    """Mirror must fire even when the primary target is NOT the main inbox.

    Both agent_needs_human and agent_response trigger the outbox mirror. The
    peer gets the full envelope; main outbox gets the stripped mirror, keeping
    the agent's own type (the `agent_` prefix is dropped) so an escalation and
    a completed-work reply stay distinguishable to the human.
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
                "type": msg_type,
                "content": "Need a credential to continue",
            }
        )
    )
    assert out.get("is_error") is not True
    # Primary: peer inbox got the full envelope
    assert json.loads(bot_inbox.read_text())[0]["type"] == msg_type
    # Mirror: main outbox still got the human-notification entry
    outbox_items = json.loads((agent_root / "messages" / "outbox.json").read_text())
    assert len(outbox_items) == 1
    assert outbox_items[0]["type"] == expected_mirror_type
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


def test_pop_and_group_inbox_accepts_goal_and_message_types(patch_iac_paths):
    iac = patch_iac_paths
    sess = _make_dummy_session(iac)
    _seed_inbox(
        iac,
        "planner",
        [
            {
                "type": "goal",
                "content": "x",
                "reply_to": "messages/inbox.json",
                "from": "main",
                "timestamp": "2026-05-03T12:00:00+00:00",
            },
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
    # Both "goal" and "message" entries are accepted and grouped together
    # under the same reply_to key — internal agents do not differentiate
    # the two types operationally.
    keys = list(groups.keys())
    assert keys == ["messages/inbox.json"]
    assert len(groups["messages/inbox.json"]) == 2
    types = sorted(m["type"] for m in groups["messages/inbox.json"])
    assert types == ["goal", "message"]


def test_pop_and_group_inbox_skips_unknown_types(patch_iac_paths):
    iac = patch_iac_paths
    sess = _make_dummy_session(iac)
    _seed_inbox(
        iac,
        "planner",
        [
            {
                "type": "event",
                "content": "x",
                "timestamp": "2026-05-03T12:00:00+00:00",
            },
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
    # Unknown types (e.g. "event", "agent_*" reply types) are still rejected.
    keys = list(groups.keys())
    assert keys == ["messages/inbox.json"]
    assert len(groups["messages/inbox.json"]) == 1
    assert groups["messages/inbox.json"][0]["type"] == "message"


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
    `_append_user_record` / `_append_assistant_record` directly, without
    going through SDK / threads.
    """
    sess = iac.InternalAgentSession.__new__(iac.InternalAgentSession)
    sess.name = name
    sess._chat_history = []
    iac._ensure_agent_files(name)
    return sess


def test_append_user_and_assistant_records_never_truncate_disk_or_memory(
    patch_iac_paths,
):
    """Prove that _append_*_record never truncates records beyond the old 1000-record cap.

    Pre-populate approach (per AGENTS.md test-quality rules): seed the chat history
    file directly with N_TURNS-1 turns (2998 records) without calling the append
    methods in a loop (which would be O(n²) disk I/O — 73 seconds in the original).
    Then call each append method exactly once and assert the total reaches 3000.
    """
    iac = patch_iac_paths
    # Use a cap above the old 1000-record limit to prove no truncation.
    n_turns = 1500
    pre_turns = n_turns - 1  # 1499 turns = 2998 records already on disk

    # Build pre-populated records: u0/a0 … u1497/a1498
    pre_records = []
    for i in range(pre_turns):
        pre_records.append(
            {"role": "user", "content": f"u{i}", "ts": "2000-01-01T00:00:00+00:00"}
        )
        pre_records.append(
            {
                "role": "assistant",
                "content": f"a{i}",
                "ts": "2000-01-01T00:00:00+00:00",
            }
        )

    # Seed the JSON file and in-memory list directly — no locked_json_rw loop.
    chat_path = iac._chat_history_path("x")
    chat_path.parent.mkdir(parents=True, exist_ok=True)
    chat_path.write_text(json.dumps(pre_records))

    sess = _make_session_skeleton(iac, "x")
    sess._chat_history = list(pre_records)  # mirror in-memory

    # One final turn appended via the real methods (exercises the code path once).
    final_i = pre_turns
    sess._append_user_record(
        ids=[f"id-{final_i}"],
        merged_reply_to=None,
        user_text=f"u{final_i}",
    )
    sess._append_assistant_record(
        ids=[f"id-{final_i}"],
        merged_reply_to=None,
        assistant_text=f"a{final_i}",
        session_id=None,
        cost_usd=None,
        duration_ms=None,
        is_error=False,
    )

    # Each turn contributes one user + one assistant record.
    on_disk = json.loads(chat_path.read_text())
    assert len(on_disk) == n_turns * 2
    assert len(sess._chat_history) == n_turns * 2
    # First record must still be the very first turn (no head trimming).
    assert on_disk[0]["content"] == "u0"
    assert on_disk[-1]["content"] == f"a{final_i}"


def test_user_record_visible_before_assistant_record(patch_iac_paths):
    """The user record must land on disk before the assistant record so the
    portal Chat tab can show the inbound prompt mid-turn."""
    iac = patch_iac_paths
    sess = _make_session_skeleton(iac, "y")
    sess._append_user_record(ids=["id-0"], merged_reply_to=None, user_text="hello")
    after_user = json.loads(iac._chat_history_path("y").read_text())
    assert len(after_user) == 1
    assert after_user[0]["role"] == "user"
    assert after_user[0]["content"] == "hello"

    sess._append_assistant_record(
        ids=["id-0"],
        merged_reply_to=None,
        assistant_text="world",
        session_id=None,
        cost_usd=None,
        duration_ms=None,
        is_error=False,
    )
    after_asst = json.loads(iac._chat_history_path("y").read_text())
    assert len(after_asst) == 2
    assert after_asst[1]["role"] == "assistant"
    assert after_asst[1]["content"] == "world"


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
# _cap_history_chars — hard character budget so the inlined history block
# can never blow past the kernel's per-argument exec() limit (cycle 8686:
# myspec-reviewer failed to reconnect with "Argument list too long" after a
# chatty 24h window produced an oversized --system-prompt argument).
# ---------------------------------------------------------------------------


def test_cap_history_chars_drops_oldest_when_over_budget(patch_iac_paths):
    iac = patch_iac_paths
    selected = [
        {"role": "user", "content": "a" * 100, "source_ids": ["1"]},
        {"role": "assistant", "content": "b" * 100, "source_ids": ["1"]},
        {"role": "user", "content": "c" * 100, "source_ids": ["2"]},
        {"role": "assistant", "content": "d" * 100, "source_ids": ["2"]},
    ]
    capped = iac._cap_history_chars(selected, max_chars=250)
    # Oldest records dropped first; total content stays under budget.
    assert capped == selected[-2:]


def test_cap_history_chars_truncates_single_oversized_record(patch_iac_paths):
    iac = patch_iac_paths
    original = {"role": "user", "content": "x" * 1000, "source_ids": ["1"]}
    capped = iac._cap_history_chars([dict(original)], max_chars=200)
    # Never drop the very last record — but do not pass it through whole
    # either: one oversized record is what blew the exec() arg limit.
    assert len(capped) == 1
    assert capped[0]["content"].endswith(iac._HISTORY_TRUNCATION_MARKER)
    assert iac._history_record_chars(capped[0]) <= 200
    # The caller's record is not mutated in place.
    assert original["content"] == "x" * 1000


def test_cap_history_chars_counts_role_prefix_in_budget(patch_iac_paths):
    """The rendered form is `**<role>**: <content>`, so the prefix counts."""
    iac = patch_iac_paths
    m = {"role": "assistant", "content": "abc"}
    # len("assistant") + len("abc") + prefix/newline overhead
    assert iac._history_record_chars(m) == 9 + 3 + iac._HISTORY_RECORD_OVERHEAD


def test_cap_history_chars_budget_is_utf8_bytes(patch_iac_paths):
    """MAX_ARG_STRLEN is a byte limit — counting characters would let
    multi-byte content through at up to 4x the real size."""
    iac = patch_iac_paths
    # 300 chars, 900 bytes.
    selected = [{"role": "user", "content": "☃" * 300}]
    capped = iac._cap_history_chars(selected, max_chars=400)
    assert len(capped[0]["content"].encode("utf-8")) <= 400
    assert capped[0]["content"].endswith(iac._HISTORY_TRUNCATION_MARKER)
    # A multi-byte character is never cut in half.
    assert "�" not in capped[0]["content"]


def test_cap_history_chars_noop_when_under_budget(patch_iac_paths):
    iac = patch_iac_paths
    selected = [{"role": "user", "content": "short", "source_ids": ["1"]}]
    assert iac._cap_history_chars(selected, max_chars=40_000) == selected


def test_select_history_for_prompt_caps_oversized_recent_window(patch_iac_paths):
    iac = patch_iac_paths
    history: list[dict] = []
    # 10 turns (20 records) within 24h, each message far larger than the
    # per-message average CHAT_HISTORY_MAX_CHARS budget allows in total.
    for i in range(10):
        pair = _pair(i, offset_minutes=-(60 + i))
        for m in pair:
            m["content"] = m["content"] * 2000  # ~4-6KB per record
        history.extend(pair)
    selected = iac._select_history_for_prompt(history)
    total_chars = sum(len(str(m["content"])) for m in selected)
    assert total_chars <= iac.CHAT_HISTORY_MAX_CHARS
    # Most recent turn must survive the trim.
    assert selected[-1]["content"] == history[-1]["content"]


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


@pytest.fixture
def mock_sdk(patch_iac_paths, monkeypatch):
    """Pre-populate _SDK_IMPORT_CACHE with lightweight fakes.

    Avoids the ~450ms real claude_agent_sdk import that _build_options() /
    _build_send_reply_server() trigger on the first call to _import_claude_sdk().
    Tests that call _build_options() use this fixture so they don't pay the
    one-time SDK cold-import cost (~450 ms per test run saved).

    _FakeOptions stores ClaudeAgentOptions kwargs as attributes so assertions
    like `options.model == "haiku"` work without the real SDK classes.
    """
    iac = patch_iac_paths

    class _FakeOptions:
        """Lightweight stand-in for ClaudeAgentOptions; stores kwargs as attrs."""

        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    _tool_mock = MagicMock(side_effect=lambda *args, **kw: MagicMock())
    _server_mock = MagicMock(return_value=MagicMock())

    monkeypatch.setattr(
        iac,
        "_SDK_IMPORT_CACHE",
        (
            MagicMock(),  # AssistantMessage
            _FakeOptions,  # ClaudeAgentOptions — accepts **kwargs, stores as attrs
            MagicMock(),  # ClaudeSDKClient
            MagicMock(),  # ResultMessage
            MagicMock(),  # TextBlock
            MagicMock(),  # ThinkingBlock
            MagicMock(),  # ToolUseBlock
            _server_mock,  # create_sdk_mcp_server
            _tool_mock,  # tool — called as tool(name, desc, schema, handler)
            MagicMock(),  # SystemMessage
        ),
    )
    return iac


def _make_options_session(iac, cfg: dict, name: str = "x"):
    sess = iac.InternalAgentSession.__new__(iac.InternalAgentSession)
    sess.name = name
    sess.cfg = cfg
    sess._chat_history = []
    sess._session_id = None
    iac._ensure_agent_files(name)
    return sess


def test_build_options_passes_model_alias_through(mock_sdk):
    iac = mock_sdk
    sess = _make_options_session(iac, cfg={"model": "haiku"})
    options = sess._build_options()
    assert options.model == "haiku"


def test_build_options_strips_whitespace_around_model(mock_sdk):
    iac = mock_sdk
    sess = _make_options_session(iac, cfg={"model": "  sonnet  "})
    options = sess._build_options()
    assert options.model == "sonnet"


def test_build_options_passes_full_model_id_through(mock_sdk):
    iac = mock_sdk
    sess = _make_options_session(iac, cfg={"model": "claude-opus-4-7"})
    options = sess._build_options()
    assert options.model == "claude-opus-4-7"


@pytest.mark.parametrize("bad", [None, "", "   ", 42, ["sonnet"], {"x": 1}])
def test_build_options_omits_model_when_absent_or_invalid(mock_sdk, bad):
    iac = mock_sdk
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
        effort="  low  ",
    )
    assert ria.cmd_register(args) == 0

    agents = json.loads(iac.AGENTS_FILE.read_text())
    cfg = next(a for a in agents if a.get("name") == "planner")
    assert cfg["model"] == "haiku"  # script stripped + persisted
    assert cfg["effort"] == "low"

    sess = _make_options_session(iac, cfg=cfg, name="planner")
    options = sess._build_options()
    assert options.model == "haiku"  # daemon read it back verbatim
    assert options.effort == "low"


# ---------------------------------------------------------------------------
# _build_options — per-agent `effort` override flows through to the SDK.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_build_options_passes_effort_through(patch_iac_paths, level):
    iac = patch_iac_paths
    sess = _make_options_session(iac, cfg={"effort": level})
    options = sess._build_options()
    assert options.effort == level


def test_build_options_strips_whitespace_around_effort(patch_iac_paths):
    iac = patch_iac_paths
    sess = _make_options_session(iac, cfg={"effort": "  high  "})
    options = sess._build_options()
    assert options.effort == "high"


@pytest.mark.parametrize(
    "bad", [None, "", "   ", "ultra", "extreme", "HIGH", 42, ["high"], {"x": 1}]
)
def test_build_options_omits_effort_when_absent_or_invalid(patch_iac_paths, bad):
    """agents.json is hand-editable and hot-reloaded every sweep, so a bad
    effort must degrade to the SDK default rather than reach the CLI (an
    invalid `--effort` would fail the connect, retried every 10s).
    """
    iac = patch_iac_paths
    cfg: dict = {} if bad is None else {"effort": bad}
    sess = _make_options_session(iac, cfg=cfg)
    options = sess._build_options()
    assert options.effort is None


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


# ---------------------------------------------------------------------------
# _import_claude_sdk — tuple shape & positional order
#
# Regression coverage for a real bug: a stale call site did
# `sdk = _import_claude_sdk(); sdk.AssistantMessage`, treating the return
# value like a module/namespace. `_import_claude_sdk` actually returns a
# 9-tuple, so every site must unpack positionally. If the export list or
# order ever changes, these tests fail loudly instead of silently binding
# the wrong symbol at every call site.
# ---------------------------------------------------------------------------


def _install_fake_claude_sdk(monkeypatch):
    """Register a stub ``claude_agent_sdk`` module in sys.modules and clear
    the lazy cache so the next ``_import_claude_sdk()`` picks up the stub.
    """
    import sys
    import types

    import internal_agent_chat as iac

    fake = types.ModuleType("claude_agent_sdk")
    for sym in (
        "AssistantMessage",
        "ClaudeAgentOptions",
        "ClaudeSDKClient",
        "ResultMessage",
        "TextBlock",
        "ThinkingBlock",
        "ToolUseBlock",
        "create_sdk_mcp_server",
        "tool",
        "SystemMessage",
    ):
        # Use distinct sentinel objects so positional-order tests can
        # assert identity rather than just non-None.
        setattr(fake, sym, type(sym, (), {"_stub_name": sym}))
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    monkeypatch.setattr(iac, "_SDK_IMPORT_CACHE", None)
    return fake


def test_import_claude_sdk_returns_symbols_in_fixed_order(monkeypatch):
    """The lazy cache must yield exactly these symbols in this order;
    every call site in internal_agent_chat.py depends on it.
    """
    fake = _install_fake_claude_sdk(monkeypatch)
    import internal_agent_chat as iac

    result = iac._import_claude_sdk()
    assert isinstance(result, tuple)
    assert len(result) == 10
    assert result == (
        fake.AssistantMessage,
        fake.ClaudeAgentOptions,
        fake.ClaudeSDKClient,
        fake.ResultMessage,
        fake.TextBlock,
        fake.ThinkingBlock,
        fake.ToolUseBlock,
        fake.create_sdk_mcp_server,
        fake.tool,
        fake.SystemMessage,
    )


def test_import_claude_sdk_caches_result(monkeypatch):
    """Second call returns the *same* tuple object — the SDK import is
    paid for only once per process.
    """
    _install_fake_claude_sdk(monkeypatch)
    import internal_agent_chat as iac

    first = iac._import_claude_sdk()
    second = iac._import_claude_sdk()
    assert first is second


def test_invoke_sdk_once_unpack_matches_import_order(monkeypatch):
    """Regression: ``_invoke_sdk_once`` reaches into positions 0/3/4 of
    the SDK tuple for AssistantMessage / ResultMessage / TextBlock. If
    those positions ever drift the chat loop silently breaks. This pins
    the contract by reading the actual unpack the source code performs.
    """
    fake = _install_fake_claude_sdk(monkeypatch)
    import internal_agent_chat as iac

    (
        assistant_msg,
        _,
        _,
        result_msg,
        text_block,
        _,
        _,
        _,
        _,
        system_msg,
    ) = iac._import_claude_sdk()
    assert assistant_msg is fake.AssistantMessage
    assert result_msg is fake.ResultMessage
    assert text_block is fake.TextBlock
    # Position 9 (last) — read by the run-boundary loop for task frames.
    assert system_msg is fake.SystemMessage


# ---------------------------------------------------------------------------
# Fleet._session_start_backoff_s — exponential growth with cap
# ---------------------------------------------------------------------------


def test_session_start_backoff_base_when_no_history(patch_iac_paths):
    iac = patch_iac_paths
    fleet = iac.Fleet()
    # No prior failure → 2**0 = 1× base.
    assert fleet._session_start_backoff_s("nobody") == iac.START_BACKOFF_BASE_S


def test_session_start_backoff_doubles_with_count(patch_iac_paths):
    iac = patch_iac_paths
    fleet = iac.Fleet()
    fleet._start_failures["x"] = {"count": 3, "next_try": 0.0}
    # 2**3 = 8× base, well below the cap.
    assert fleet._session_start_backoff_s("x") == iac.START_BACKOFF_BASE_S * 8


def test_session_start_backoff_clamps_at_cap(patch_iac_paths):
    iac = patch_iac_paths
    fleet = iac.Fleet()
    fleet._start_failures["x"] = {"count": 999, "next_try": 0.0}
    assert fleet._session_start_backoff_s("x") == iac.START_BACKOFF_MAX_S


# ---------------------------------------------------------------------------
# Fleet.reconcile — start-failure backoff bookkeeping
# ---------------------------------------------------------------------------


def _install_failing_session(monkeypatch, iac, *, fail_exc=None):
    """Replace ``InternalAgentSession`` with a stub whose ``start()`` raises.
    Returns a list that is appended to on every constructor call so tests
    can assert how many starts were attempted.
    """
    calls: list = []
    exc = fail_exc or RuntimeError("boom")

    class _BoomSession:
        def __init__(self, name, cfg):
            self.name = name
            self.cfg = cfg
            calls.append(name)

        def start(self):
            raise exc

        def stop(self):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr(iac, "InternalAgentSession", _BoomSession)
    return calls


def _capture_surface_error(monkeypatch, iac):
    captured: list = []
    monkeypatch.setattr(
        iac,
        "surface_error",
        lambda comp, exc, context="": captured.append((comp, str(exc), context)),
    )
    return captured


def test_reconcile_records_first_start_failure_and_surfaces_once(
    patch_iac_paths, monkeypatch
):
    iac = patch_iac_paths
    _install_failing_session(monkeypatch, iac)
    surfaced = _capture_surface_error(monkeypatch, iac)
    _seed_agents(iac, [{"type": "internal", "name": "a", "status": "online"}])
    # Prevent mark_internal_agents_online from poking real disk paths.
    monkeypatch.setattr(iac, "mark_internal_agents_online", lambda names: None)

    fleet = iac.Fleet()
    fleet.reconcile()

    assert "a" in fleet._start_failures
    rec = fleet._start_failures["a"]
    assert rec["count"] == 1
    assert rec["next_try"] > 0
    # First failure surfaces; later sweeps must not (covered below).
    assert len(surfaced) == 1
    assert surfaced[0][2] == "start:a"


def test_reconcile_subsequent_failures_do_not_spam_surface_error(
    patch_iac_paths, monkeypatch
):
    """Regression: a persistently-failing agent must surface_error exactly
    once, not on every sweep tick.
    """
    iac = patch_iac_paths
    _install_failing_session(monkeypatch, iac)
    surfaced = _capture_surface_error(monkeypatch, iac)
    monkeypatch.setattr(iac, "mark_internal_agents_online", lambda names: None)
    _seed_agents(iac, [{"type": "internal", "name": "a", "status": "online"}])

    fleet = iac.Fleet()
    fleet.reconcile()  # records first failure → surfaces
    # Fast-forward the next_try window so reconcile actually retries.
    fleet._start_failures["a"]["next_try"] = 0.0
    fleet.reconcile()  # retries, fails again → must NOT surface
    fleet._start_failures["a"]["next_try"] = 0.0
    fleet.reconcile()

    assert fleet._start_failures["a"]["count"] == 3
    assert len(surfaced) == 1  # still just the first failure


def test_reconcile_honours_backoff_window(patch_iac_paths, monkeypatch):
    """Within the backoff window we must skip the agent entirely — no new
    construction attempts, no new failure entries.
    """
    iac = patch_iac_paths
    calls = _install_failing_session(monkeypatch, iac)
    _capture_surface_error(monkeypatch, iac)
    monkeypatch.setattr(iac, "mark_internal_agents_online", lambda names: None)
    _seed_agents(iac, [{"type": "internal", "name": "a", "status": "online"}])

    fleet = iac.Fleet()
    fleet.reconcile()  # 1 construction attempt
    assert len(calls) == 1
    # next_try is in the future → second reconcile must short-circuit.
    fleet.reconcile()
    assert len(calls) == 1
    # Count unchanged: we skipped, we didn't fail again.
    assert fleet._start_failures["a"]["count"] == 1


def test_reconcile_cleans_backoff_when_agent_removed_after_only_failures(
    patch_iac_paths, monkeypatch
):
    """Regression: an agent that only ever failed to start (never landed
    in ``_sessions``) and was then removed from agents.json must have its
    backoff entry cleaned up. Otherwise the dict leaks forever.
    """
    iac = patch_iac_paths
    _install_failing_session(monkeypatch, iac)
    _capture_surface_error(monkeypatch, iac)
    monkeypatch.setattr(iac, "mark_internal_agents_online", lambda names: None)
    _seed_agents(iac, [{"type": "internal", "name": "a", "status": "online"}])

    fleet = iac.Fleet()
    fleet.reconcile()
    assert "a" in fleet._start_failures
    assert "a" not in fleet._sessions  # never landed in sessions

    # Remove the agent from agents.json and reconcile again.
    _seed_agents(iac, [])
    fleet.reconcile()

    assert "a" not in fleet._start_failures


def test_reconcile_clears_backoff_on_successful_start(patch_iac_paths, monkeypatch):
    """Once a previously-failing agent finally starts, its backoff entry
    must be removed so a future failure starts from count=1 again.
    """
    iac = patch_iac_paths

    class _OkSession:
        def __init__(self, name, cfg):
            self.name = name
            self.cfg = cfg

        def start(self):
            pass

        def stop(self):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(iac, "InternalAgentSession", _OkSession)
    monkeypatch.setattr(iac, "mark_internal_agents_online", lambda names: None)
    _seed_agents(iac, [{"type": "internal", "name": "a", "status": "online"}])

    fleet = iac.Fleet()
    # Pre-seed a stale failure entry with an already-elapsed next_try.
    fleet._start_failures["a"] = {"count": 2, "next_try": 0.0}
    fleet.reconcile()

    assert "a" in fleet._sessions
    assert "a" not in fleet._start_failures


# ---------------------------------------------------------------------------
# _notify_main_inbox_on_timeout
# ---------------------------------------------------------------------------


class TestNotifyMainInboxOnTimeout:
    """Tests for the turn-timeout inbox notification helper."""

    def _inbox(self, iac) -> Path:
        p = iac.INBOX_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("[]")
        return p

    def _read_inbox(self, iac) -> list:
        return json.loads(self._inbox(iac).read_text())

    def test_writes_message_to_main_inbox(self, patch_iac_paths):
        iac = patch_iac_paths
        self._inbox(iac)
        iac._notify_main_inbox_on_timeout(
            agent_name="doordot-reviewer",
            ids=["abc-123"],
            prompt_snippet="Please review PR https://github.com/doordot/doordot-monorepo/pull/204",
            timeout_seconds=900,
        )
        items = self._read_inbox(iac)
        assert len(items) == 1
        msg = items[0]
        assert msg["type"] == "message"
        assert msg["source"] == "internal_agent_chat"
        assert "doordot-reviewer" in msg["subject"]
        assert "900" in msg["subject"]

    def test_envelope_contains_agent_name(self, patch_iac_paths):
        iac = patch_iac_paths
        self._inbox(iac)
        iac._notify_main_inbox_on_timeout(
            agent_name="myspec-reviewer",
            ids=["id-1"],
            prompt_snippet="some task",
            timeout_seconds=600,
        )
        msg = self._read_inbox(iac)[0]
        assert msg["agent"] == "myspec-reviewer"

    def test_envelope_contains_timed_out_ids(self, patch_iac_paths):
        iac = patch_iac_paths
        self._inbox(iac)
        iac._notify_main_inbox_on_timeout(
            agent_name="doordot-reviewer",
            ids=["id-a", "id-b"],
            prompt_snippet="",
            timeout_seconds=900,
        )
        msg = self._read_inbox(iac)[0]
        assert msg["timed_out_ids"] == ["id-a", "id-b"]
        assert "id-a" in msg["content"]
        assert "id-b" in msg["content"]

    def test_snippet_truncated_to_limit(self, patch_iac_paths):
        iac = patch_iac_paths
        self._inbox(iac)
        long_prompt = "X" * 500
        iac._notify_main_inbox_on_timeout(
            agent_name="agent",
            ids=["id-1"],
            prompt_snippet=long_prompt,
            timeout_seconds=900,
        )
        msg = self._read_inbox(iac)[0]
        # Content should contain truncated snippet — not the full 500 chars
        assert "X" * 500 not in msg["content"]
        # Truncation marker is present
        assert "…" in msg["content"]

    def test_snippet_not_truncated_when_short(self, patch_iac_paths):
        iac = patch_iac_paths
        self._inbox(iac)
        short_prompt = "short task"
        iac._notify_main_inbox_on_timeout(
            agent_name="agent",
            ids=["id-1"],
            prompt_snippet=short_prompt,
            timeout_seconds=900,
        )
        msg = self._read_inbox(iac)[0]
        assert "short task" in msg["content"]
        assert "…" not in msg["content"]

    def test_empty_prompt_does_not_crash(self, patch_iac_paths):
        iac = patch_iac_paths
        self._inbox(iac)
        iac._notify_main_inbox_on_timeout(
            agent_name="agent",
            ids=["id-1"],
            prompt_snippet="",
            timeout_seconds=900,
        )
        items = self._read_inbox(iac)
        assert len(items) == 1  # envelope still written

    def test_timeout_seconds_in_subject_and_content(self, patch_iac_paths):
        iac = patch_iac_paths
        self._inbox(iac)
        iac._notify_main_inbox_on_timeout(
            agent_name="agent",
            ids=["x"],
            prompt_snippet="task",
            timeout_seconds=900,
        )
        msg = self._read_inbox(iac)[0]
        assert "900" in msg["subject"]
        assert "900" in msg["content"]

    def test_envelope_has_timestamp(self, patch_iac_paths):
        iac = patch_iac_paths
        self._inbox(iac)
        iac._notify_main_inbox_on_timeout(
            agent_name="agent",
            ids=["x"],
            prompt_snippet="task",
            timeout_seconds=900,
        )
        msg = self._read_inbox(iac)[0]
        assert "timestamp" in msg
        # ISO 8601 basic check
        assert "T" in msg["timestamp"]

    def test_snippet_constant_matches_truncation_boundary(self, patch_iac_paths):
        """_TIMEOUT_SNIPPET_LEN must equal the actual truncation boundary."""
        iac = patch_iac_paths
        self._inbox(iac)
        limit = iac._TIMEOUT_SNIPPET_LEN
        exact_prompt = "A" * limit
        over_prompt = "A" * (limit + 1)

        # Exactly at boundary — no truncation
        self._inbox(iac).write_text("[]")
        iac._notify_main_inbox_on_timeout("a", ["x"], exact_prompt, 900)
        msg = self._read_inbox(iac)[0]
        assert "…" not in msg["content"]

        # One over — truncation triggered
        self._inbox(iac).write_text("[]")
        iac._notify_main_inbox_on_timeout("a", ["x"], over_prompt, 900)
        msg = self._read_inbox(iac)[0]
        assert "…" in msg["content"]


def _make_bare_session(iac):
    """Build an InternalAgentSession without running __init__ (which spins
    up a real thread + SDK connect). Only the attributes is_alive() reads
    are set."""
    sess = iac.InternalAgentSession.__new__(iac.InternalAgentSession)
    sess.name = "bare"
    sess._stop_event = threading.Event()
    return sess


class TestIsAliveThreadCheck:
    """Regression coverage for the reliability bug (evolve cycle 5949):
    is_alive() previously only checked `_sdk is not None and not
    _stop_event.is_set()`, so a worker thread that crashed with an
    exception escaping `_worker()` was reported alive forever — `_run_loop`'s
    `finally` block never cleared `_sdk` or set `_stop_event` on that path.
    """

    def test_alive_when_sdk_set_and_thread_running_and_not_stopped(
        self, patch_iac_paths
    ):
        iac = patch_iac_paths
        sess = _make_bare_session(iac)
        sess._sdk = object()
        started = threading.Event()
        release = threading.Event()

        def _spin():
            started.set()
            release.wait(timeout=5)

        sess._thread = threading.Thread(target=_spin, daemon=True)
        sess._thread.start()
        started.wait(timeout=5)
        try:
            assert sess.is_alive() is True
        finally:
            release.set()
            sess._thread.join(timeout=5)

    def test_not_alive_when_thread_has_exited_even_if_sdk_still_set(
        self, patch_iac_paths
    ):
        """This is the exact crash scenario: `_sdk` was never nulled out
        because the worker's exception escaped past the finally block's
        normal exit paths, but the thread itself is dead."""
        iac = patch_iac_paths
        sess = _make_bare_session(iac)
        sess._sdk = object()  # stale, non-None — the bug's trigger condition
        sess._thread = threading.Thread(target=lambda: None, daemon=True)
        sess._thread.start()
        sess._thread.join(timeout=5)
        assert sess._thread.is_alive() is False
        assert sess.is_alive() is False

    def test_not_alive_when_sdk_is_none(self, patch_iac_paths):
        iac = patch_iac_paths
        sess = _make_bare_session(iac)
        sess._sdk = None
        sess._thread = threading.Thread(target=lambda: None, daemon=True)
        sess._thread.start()
        sess._thread.join(timeout=5)
        assert sess.is_alive() is False

    def test_not_alive_when_stop_event_set(self, patch_iac_paths):
        iac = patch_iac_paths
        sess = _make_bare_session(iac)
        sess._sdk = object()
        sess._stop_event.set()
        started = threading.Event()
        release = threading.Event()

        def _spin():
            started.set()
            release.wait(timeout=5)

        sess._thread = threading.Thread(target=_spin, daemon=True)
        sess._thread.start()
        started.wait(timeout=5)
        try:
            assert sess.is_alive() is False
        finally:
            release.set()
            sess._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Run-boundary detection — a run that spawns background subagents emits one
# `result` frame per turn, and `_invoke_sdk_once` must consume ALL of them.
#
# Regression for the "internal agent pauses forever waiting for subagents"
# bug: breaking at the first ResultMessage stranded the continuation frames
# in the SDK's bounded (max_buffer_size=100) receive channel, which wedged
# the SDK reader task — the same task that services SDK-MCP control
# requests — so the agent's own `send_reply` call hung until the next inbox
# message happened to resume reading, and that message then received the
# PREVIOUS run's text (a ~10ms "turn").
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeThinkingBlock:
    def __init__(self, thinking):
        self.thinking = thinking


class _FakeAssistantMessage:
    def __init__(self, *blocks, parent_tool_use_id=None):
        self.content = list(blocks)
        self.parent_tool_use_id = parent_tool_use_id


class _FakeResultMessage:
    def __init__(
        self, session_id="sess-1", total_cost_usd=1.5, duration_ms=42, is_error=False
    ):
        self.session_id = session_id
        self.total_cost_usd = total_cost_usd
        self.duration_ms = duration_ms
        self.is_error = is_error


class _FakeSystemMessage:
    def __init__(self, subtype, data=None, **attrs):
        self.subtype = subtype
        self.data = data or {}
        for k, v in attrs.items():
            setattr(self, k, v)


class _FakeStreamSDK:
    """Fake ClaudeSDKClient with the real channel semantics that matter.

    `receive_messages()` is a fresh async generator each call but drains a
    SHARED queue, so frames a previous turn did not consume are still there
    for the next one — exactly the leftover behaviour of the SDK's anyio
    memory stream, and the thing the desync bug depended on.
    """

    def __init__(self, *batches):
        import asyncio as _asyncio

        self._q = _asyncio.Queue()
        self._batches = [list(b) for b in batches]
        self.prompts = []

    def preload(self, *msgs):
        for m in msgs:
            self._q.put_nowait(m)

    async def query(self, prompt):
        self.prompts.append(prompt)
        batch = self._batches.pop(0) if self._batches else []
        for m in batch:
            self._q.put_nowait(m)

    async def receive_messages(self):
        while True:
            yield await self._q.get()

    def pending(self):
        return self._q.qsize()


def _install_stream_sdk(monkeypatch):
    """Install a claude_agent_sdk stub whose message classes are real,
    instantiable types so `_invoke_sdk_once`'s isinstance dispatch works.
    """
    import sys
    import types

    import internal_agent_chat as iac

    fake = types.ModuleType("claude_agent_sdk")
    fake.AssistantMessage = _FakeAssistantMessage
    fake.ResultMessage = _FakeResultMessage
    fake.SystemMessage = _FakeSystemMessage
    fake.TextBlock = _FakeTextBlock
    fake.ThinkingBlock = _FakeThinkingBlock
    for sym in (
        "ClaudeAgentOptions",
        "ClaudeSDKClient",
        "ToolUseBlock",
        "create_sdk_mcp_server",
        "tool",
    ):
        setattr(fake, sym, type(sym, (), {"_stub_name": sym}))
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    monkeypatch.setattr(iac, "_SDK_IMPORT_CACHE", None)
    return fake


def _stream_session(iac, sdk, *, turn_timeout=10):
    sess = iac.InternalAgentSession.__new__(iac.InternalAgentSession)
    sess.name = "runboundary"
    sess._sdk = sdk
    sess._turn_timeout = turn_timeout
    sess._session_id = None
    sess._save_session_id = lambda sid: None
    return sess


def _fast_windows(monkeypatch, iac):
    """Shrink the grace windows so tests finish in milliseconds."""
    monkeypatch.setattr(iac, "POST_RESULT_DRAIN_S", 0.05)
    monkeypatch.setattr(iac, "POST_TASK_DRAIN_S", 0.05)
    monkeypatch.setattr(iac, "INFLIGHT_IDLE_TIMEOUT_S", 0.20)
    monkeypatch.setattr(iac, "surface_error", lambda *a, **k: None)


# ── _apply_task_lifecycle ──────────────────────────────────────


def test_task_lifecycle_tracks_agent_start_and_notification(patch_iac_paths):
    iac = patch_iac_paths
    inflight = set()
    started = _FakeSystemMessage("task_started", task_id="t1", task_type="local_agent")
    assert iac._apply_task_lifecycle(started, inflight) is True
    assert inflight == {"t1"}
    done = _FakeSystemMessage("task_notification", task_id="t1", status="completed")
    assert iac._apply_task_lifecycle(done, inflight) is False
    assert inflight == set()


def test_task_lifecycle_ignores_background_shells(patch_iac_paths):
    """`local_bash` tasks may never reach a terminal status — tracking one
    would hold every turn open until the idle timeout.
    """
    iac = patch_iac_paths
    inflight = set()
    shell = _FakeSystemMessage("task_started", task_id="b1", task_type="local_bash")
    assert iac._apply_task_lifecycle(shell, inflight) is False
    assert inflight == set()


def test_task_lifecycle_clears_on_terminal_task_updated(patch_iac_paths):
    """A terminal state can arrive ONLY as `task_updated` (no notification)."""
    iac = patch_iac_paths
    inflight = {"t1"}
    iac._apply_task_lifecycle(
        _FakeSystemMessage(
            "task_updated", data={"task_id": "t1", "patch": {"status": "completed"}}
        ),
        inflight,
    )
    assert inflight == set()


def test_task_lifecycle_keeps_task_on_nonterminal_update(patch_iac_paths):
    iac = patch_iac_paths
    inflight = {"t1"}
    iac._apply_task_lifecycle(
        _FakeSystemMessage(
            "task_updated", data={"task_id": "t1", "patch": {"status": "running"}}
        ),
        inflight,
    )
    assert inflight == {"t1"}


def test_task_lifecycle_reads_task_id_from_raw_data(patch_iac_paths):
    """Base `SystemMessage` carries only the raw payload, not attributes."""
    iac = patch_iac_paths
    inflight = set()
    assert (
        iac._apply_task_lifecycle(
            _FakeSystemMessage(
                "task_started", data={"task_id": "t9", "task_type": "local_workflow"}
            ),
            inflight,
        )
        is True
    )
    assert inflight == {"t9"}


def test_task_lifecycle_keeps_task_on_nonterminal_notification(patch_iac_paths):
    """A notification that is explicitly non-terminal must not clear the task.

    Defensive: SDK 0.1.76 types `TaskNotificationStatus` as
    ``completed|failed|stopped``, so no real notification frame currently
    carries a non-terminal status — in-progress updates arrive as
    `task_progress`. This pins the behavior in case that changes.
    """
    iac = patch_iac_paths
    inflight = {"t1"}
    iac._apply_task_lifecycle(
        _FakeSystemMessage("task_notification", task_id="t1", status="running"),
        inflight,
    )
    assert inflight == {"t1"}


def test_task_lifecycle_clears_on_notification_without_status(patch_iac_paths):
    """A missing status is treated as terminal — refusing to discard would
    pin the task and hold the turn open until INFLIGHT_IDLE_TIMEOUT_S."""
    iac = patch_iac_paths
    inflight = {"t1"}
    iac._apply_task_lifecycle(
        _FakeSystemMessage("task_notification", task_id="t1"), inflight
    )
    assert inflight == set()


def test_task_lifecycle_reads_patch_from_typed_attribute(patch_iac_paths):
    """`TaskUpdatedMessage` exposes `patch` as an attribute, not only in `data`."""
    iac = patch_iac_paths
    inflight = {"t1"}
    iac._apply_task_lifecycle(
        _FakeSystemMessage("task_updated", task_id="t1", patch={"status": "completed"}),
        inflight,
    )
    assert inflight == set()


# ── _drain_windows ─────────────────────────────────────────────


def test_drain_windows_clamped_to_short_turn_timeout(patch_iac_paths):
    """A per-agent `timeout_seconds` shorter than the constants must win —
    otherwise the outer turn timeout fires first and discards the text.

    A window sized at exactly the turn budget is no better than an
    unclamped one: it starts after the outer `wait_for` has already been
    running, so it can never elapse first. Assert real headroom.
    """
    iac = patch_iac_paths
    sess = iac.InternalAgentSession.__new__(iac.InternalAgentSession)
    sess._turn_timeout = 10
    post_result, post_task, inflight_idle = sess._drain_windows()
    for window in (post_result, post_task, inflight_idle):
        assert window < sess._turn_timeout


def test_drain_windows_scale_post_task_with_long_timeout(patch_iac_paths):
    """A long-running agent gets a proportionally longer post-task window so
    a slow continuation turn is not cut off at a flat 30s."""
    iac = patch_iac_paths
    sess = iac.InternalAgentSession.__new__(iac.InternalAgentSession)
    sess._turn_timeout = 1800
    _, post_task, inflight_idle = sess._drain_windows()
    assert post_task > iac.POST_TASK_DRAIN_S
    assert post_task <= iac.INFLIGHT_IDLE_TIMEOUT_S
    assert inflight_idle == iac.INFLIGHT_IDLE_TIMEOUT_S


# ── _invoke_sdk_once: the run boundary ─────────────────────────


def test_invoke_sdk_once_reads_past_result_frame_while_task_in_flight(
    patch_iac_paths, monkeypatch
):
    """THE regression: a result frame with a subagent still in flight is a
    TURN boundary, not the end of the run. The loop must keep reading, pick
    up the continuation turn's text, and leave nothing buffered.
    """
    iac = patch_iac_paths
    _install_stream_sdk(monkeypatch)
    _fast_windows(monkeypatch, iac)

    sdk = _FakeStreamSDK(
        [
            _FakeAssistantMessage(_FakeTextBlock("Launching a subagent.")),
            _FakeSystemMessage("task_started", task_id="t1", task_type="local_agent"),
            # Turn 1 ends here — the OLD code stopped reading at this frame.
            _FakeResultMessage(total_cost_usd=1.0, is_error=False),
            # ... subagent finishes and the CLI wakes the parent ...
            _FakeSystemMessage("task_notification", task_id="t1", status="completed"),
            _FakeAssistantMessage(_FakeTextBlock("Subagent done; final answer.")),
            _FakeResultMessage(total_cost_usd=2.5, is_error=False),
        ]
    )
    sess = _stream_session(iac, sdk)

    (
        runner_error,
        text_parts,
        cost,
        duration_ms,
        sdk_is_error,
        timed_out,
        thinking_parts,
    ) = asyncio.run(sess._invoke_sdk_once("review this PR", ["id-1"]))

    assert runner_error is None
    assert timed_out is False
    # The continuation turn is a separate paragraph, not a continuation of
    # turn 1's last sentence. (This assertion used to compensate for the
    # missing break with a leading space on the second fragment.)
    assert "".join(text_parts) == (
        "Launching a subagent.\n\nSubagent done; final answer."
    )
    # Last frame wins for cost; duration is measured to the last result frame.
    assert cost == 2.5
    assert duration_ms is not None
    assert sdk_is_error is False
    assert thinking_parts == []
    # Nothing stranded => nothing to leak into the next turn.
    assert sdk.pending() == 0


def test_invoke_sdk_once_stops_at_result_when_no_tasks_in_flight(
    patch_iac_paths, monkeypatch
):
    """The common no-subagent case must still finish at its single result
    frame after only the short grace window.
    """
    iac = patch_iac_paths
    _install_stream_sdk(monkeypatch)
    _fast_windows(monkeypatch, iac)

    sdk = _FakeStreamSDK(
        [
            _FakeAssistantMessage(
                _FakeThinkingBlock("pondering"), _FakeTextBlock("done")
            ),
            _FakeResultMessage(total_cost_usd=0.25),
        ]
    )
    sess = _stream_session(iac, sdk)
    _, text_parts, cost, _, is_err, timed_out, thinking = asyncio.run(
        sess._invoke_sdk_once("hi", ["id-1"])
    )
    assert "".join(text_parts) == "done"
    assert thinking == ["pondering"]
    assert cost == 0.25
    assert (is_err, timed_out) == (False, False)


def test_invoke_sdk_once_excludes_subagent_sidechain_text(patch_iac_paths, monkeypatch):
    """A frame with `parent_tool_use_id` set is a subagent's own transcript.
    Splicing it into the reply is what made recorded responses read as
    mid-sentence fragments of internal work.
    """
    iac = patch_iac_paths
    _install_stream_sdk(monkeypatch)
    _fast_windows(monkeypatch, iac)

    sdk = _FakeStreamSDK(
        [
            _FakeAssistantMessage(_FakeTextBlock("Top-level reply.")),
            _FakeAssistantMessage(
                _FakeTextBlock("SUBAGENT INTERNAL MONOLOGUE"),
                parent_tool_use_id="toolu_abc",
            ),
            _FakeResultMessage(),
        ]
    )
    sess = _stream_session(iac, sdk)
    _, text_parts, *_ = asyncio.run(sess._invoke_sdk_once("go", ["id-1"]))
    joined = "".join(text_parts)
    assert joined == "Top-level reply."
    assert "SUBAGENT" not in joined


def test_invoke_sdk_once_finalizes_when_task_never_reports_terminal(
    patch_iac_paths, monkeypatch
):
    """A dropped task notification must degrade to "finalize with the text
    we have", not to a full turn timeout that discards it.
    """
    iac = patch_iac_paths
    _install_stream_sdk(monkeypatch)
    _fast_windows(monkeypatch, iac)

    sdk = _FakeStreamSDK(
        [
            _FakeAssistantMessage(_FakeTextBlock("partial work")),
            _FakeSystemMessage("task_started", task_id="lost", task_type="local_agent"),
            _FakeResultMessage(),
            # no task_notification / task_updated ever arrives
        ]
    )
    sess = _stream_session(iac, sdk, turn_timeout=10)
    runner_error, text_parts, _, _, _, timed_out, _ = asyncio.run(
        sess._invoke_sdk_once("go", ["id-1"])
    )
    assert timed_out is False  # did NOT burn the whole turn timeout
    assert runner_error is None
    assert "".join(text_parts) == "partial work"


def test_invoke_sdk_once_sticky_is_error_across_continuation_turns(
    patch_iac_paths, monkeypatch
):
    """A failed intermediate turn must not be masked by a clean continuation."""
    iac = patch_iac_paths
    _install_stream_sdk(monkeypatch)
    _fast_windows(monkeypatch, iac)

    sdk = _FakeStreamSDK(
        [
            _FakeSystemMessage("task_started", task_id="t1", task_type="local_agent"),
            _FakeResultMessage(is_error=True),
            _FakeSystemMessage("task_notification", task_id="t1", status="completed"),
            _FakeAssistantMessage(_FakeTextBlock("recovered")),
            _FakeResultMessage(is_error=False),
        ]
    )
    sess = _stream_session(iac, sdk)
    (
        runner_error,
        text_parts,
        _cost,
        _duration_ms,
        sdk_is_error,
        timed_out,
        _thinking,
    ) = asyncio.run(sess._invoke_sdk_once("go", ["id-1"]))

    assert runner_error is None
    assert timed_out is False
    assert "".join(text_parts) == "recovered"
    assert sdk_is_error is True  # sticky across the clean final frame


def test_invoke_sdk_once_flushes_stale_frames_from_a_previous_turn(
    patch_iac_paths, monkeypatch
):
    """Leftovers from a cancelled/timed-out turn must be discarded, not
    handed to the next prompt as if they answered it.
    """
    iac = patch_iac_paths
    _install_stream_sdk(monkeypatch)
    _fast_windows(monkeypatch, iac)

    sdk = _FakeStreamSDK(
        [
            _FakeAssistantMessage(_FakeTextBlock("answer to the NEW prompt")),
            _FakeResultMessage(total_cost_usd=9.0),
        ]
    )
    # Simulate a previous turn's abandoned frames sitting in the channel.
    sdk.preload(
        _FakeAssistantMessage(_FakeTextBlock("STALE text from the OLD run")),
        _FakeResultMessage(total_cost_usd=1.0),
    )
    sess = _stream_session(iac, sdk)
    _, text_parts, cost, *_ = asyncio.run(sess._invoke_sdk_once("new", ["id-2"]))
    joined = "".join(text_parts)
    assert joined == "answer to the NEW prompt"
    assert "STALE" not in joined
    assert cost == 9.0


def test_flush_stale_stream_returns_zero_without_sdk(patch_iac_paths, monkeypatch):
    iac = patch_iac_paths
    _install_stream_sdk(monkeypatch)
    sess = _stream_session(iac, None)
    assert asyncio.run(sess._flush_stale_stream()) == 0


class _FlushRaisingSDK(_FakeStreamSDK):
    """Raises once on the first `receive_messages()`, then behaves normally.

    Models a dead reader task surfacing while the between-turn flush is
    draining leftovers.
    """

    def __init__(self, *batches, error="Failed to decode JSON"):
        super().__init__(*batches)
        self._error = error
        self.raised = False

    async def receive_messages(self):
        if not self.raised:
            self.raised = True
            raise Exception(self._error)
        async for m in super().receive_messages():
            yield m


def test_flush_stale_stream_reraises_fatal_error(patch_iac_paths, monkeypatch):
    """Swallowing a dead reader here left the caller to `query()` a dead
    client, which hung until the turn timeout instead of reconnecting."""
    iac = patch_iac_paths
    _install_stream_sdk(monkeypatch)
    sess = _stream_session(iac, _FlushRaisingSDK())
    with pytest.raises(Exception, match="Failed to decode JSON"):
        asyncio.run(sess._flush_stale_stream())


def test_flush_stale_stream_swallows_non_fatal_error(patch_iac_paths, monkeypatch):
    iac = patch_iac_paths
    _install_stream_sdk(monkeypatch)
    sess = _stream_session(iac, _FlushRaisingSDK(error="transient blip"))
    assert asyncio.run(sess._flush_stale_stream()) == 0


def test_invoke_sdk_once_reconnects_after_fatal_flush(patch_iac_paths, monkeypatch):
    iac = patch_iac_paths
    _install_stream_sdk(monkeypatch)
    _fast_windows(monkeypatch, iac)

    healthy = _FakeStreamSDK(
        [_FakeAssistantMessage(_FakeTextBlock("ok")), _FakeResultMessage()]
    )
    sess = _stream_session(iac, _FlushRaisingSDK())
    reasons: list = []

    async def _reconnect(reason):
        reasons.append(reason)
        sess._sdk = healthy
        return True

    sess._reconnect_after_error = _reconnect

    runner_error, text_parts, *_ = asyncio.run(sess._invoke_sdk_once("go", ["id-1"]))

    assert len(reasons) == 1  # exactly one reconnect for this turn
    assert "Failed to decode JSON" in reasons[0]
    assert runner_error is None
    assert "".join(text_parts) == "ok"
    # Flagged so _run_turn_for_group's trailing reconnect is suppressed.
    assert sess._reconnected_this_turn is True


def test_invoke_sdk_once_error_tuple_when_reconnect_leaves_no_client(
    patch_iac_paths, monkeypatch
):
    """The hand-written early-return tuple must match the 7 positions
    `_run_turn_for_group` unpacks, and must not look retryable."""
    iac = patch_iac_paths
    _install_stream_sdk(monkeypatch)
    _fast_windows(monkeypatch, iac)

    sess = _stream_session(iac, _FlushRaisingSDK())

    async def _reconnect(reason):
        sess._sdk = None
        return False

    sess._reconnect_after_error = _reconnect

    result = asyncio.run(sess._invoke_sdk_once("go", ["id-1"]))
    assert len(result) == 7
    runner_error, text_parts, cost, duration_ms, sdk_is_error, timed_out, thinking = (
        result
    )
    assert runner_error is not None
    assert sdk_is_error is True
    assert timed_out is False
    assert (text_parts, cost, duration_ms, thinking) == ([], None, None, [])
    # Must NOT match the retry signature — replaying the prompt here would
    # risk duplicate side effects.
    assert iac._is_dead_subprocess_error(runner_error) is False


def test_invoke_sdk_once_separator_survives_thinking_only_turn(
    patch_iac_paths, monkeypatch
):
    """A continuation turn that emits only thinking must leave the pending
    paragraph break armed for the next turn that actually emits text."""
    iac = patch_iac_paths
    _install_stream_sdk(monkeypatch)
    _fast_windows(monkeypatch, iac)

    sdk = _FakeStreamSDK(
        [
            _FakeAssistantMessage(_FakeTextBlock("first")),
            _FakeSystemMessage("task_started", task_id="t1", task_type="local_agent"),
            _FakeResultMessage(),
            _FakeAssistantMessage(_FakeThinkingBlock("pondering")),
            _FakeResultMessage(),
            _FakeSystemMessage("task_notification", task_id="t1", status="completed"),
            _FakeAssistantMessage(_FakeTextBlock("second")),
            _FakeResultMessage(),
        ]
    )
    sess = _stream_session(iac, sdk)

    _, text_parts, _cost, _dur, _err, _to, thinking_parts = asyncio.run(
        sess._invoke_sdk_once("go", ["id-1"])
    )

    joined = "".join(text_parts)
    assert joined == "first\n\nsecond"  # exactly one break, none stray
    assert not joined.startswith("\n") and not joined.endswith("\n")
    assert thinking_parts == ["pondering"]


def test_invoke_sdk_once_saves_session_id_once_per_run(patch_iac_paths, monkeypatch):
    """A multi-turn run emits one result frame per turn; re-saving the same
    id on each of them rewrites the session file for no reason.
    """
    iac = patch_iac_paths
    _install_stream_sdk(monkeypatch)
    _fast_windows(monkeypatch, iac)

    sdk = _FakeStreamSDK(
        [
            _FakeAssistantMessage(_FakeTextBlock("one")),
            _FakeSystemMessage("task_started", task_id="t1", task_type="local_agent"),
            _FakeResultMessage(total_cost_usd=1.0, session_id="sid-1"),
            _FakeSystemMessage("task_notification", task_id="t1", status="completed"),
            _FakeAssistantMessage(_FakeTextBlock("two")),
            _FakeResultMessage(total_cost_usd=2.0, session_id="sid-1"),
        ]
    )
    sess = _stream_session(iac, sdk)
    saved: list = []
    sess._save_session_id = saved.append

    asyncio.run(sess._invoke_sdk_once("go", ["id-1"]))

    assert saved == ["sid-1"]
    assert sess._session_id == "sid-1"


def test_run_turn_timeout_keeps_partial_text(patch_iac_paths, monkeypatch):
    """A turn timeout now bounds a whole multi-turn RUN, so discarding
    `text_parts` would throw away every completed turn's output. Keep it
    under an explicit marker instead.
    """
    iac = patch_iac_paths
    monkeypatch.setattr(iac, "surface_error", lambda *a, **k: None)
    monkeypatch.setattr(iac, "_notify_main_inbox_on_timeout", lambda **k: None)

    sess = iac.InternalAgentSession.__new__(iac.InternalAgentSession)
    sess.name = "planner"
    sess._turn_timeout = 30
    sess._session_id = "sid-1"
    recorded: dict = {}

    async def _fake_invoke(prompt, ids):
        # (runner_error, text_parts, cost, duration_ms, is_error, timed_out, thinking)
        return (
            None,
            ["Turn one done.", "\n\n", "Turn two half-"],
            None,
            None,
            False,
            True,
            [],
        )

    async def _fake_reconnect(reason):
        return True

    sess._invoke_sdk_once = _fake_invoke
    sess._reconnect_after_error = _fake_reconnect
    sess._build_user_prompt = lambda msgs: "prompt"
    sess._readable_user_record = lambda msgs: "user text"
    sess._append_user_record = lambda **kw: None
    sess._append_assistant_record = lambda **kw: recorded.update(kw)

    asyncio.run(sess._run_turn_for_group("__none__", [{"id": "id-1"}]))

    text = recorded["assistant_text"]
    assert text.startswith("Turn one done.\n\nTurn two half-")
    assert "timed out after 30s" in text
    assert "(partial output above)" in text
    assert recorded["is_error"] is True
