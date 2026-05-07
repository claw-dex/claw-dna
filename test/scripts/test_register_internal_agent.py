"""Tests for scripts/register_internal_agent.py."""

from __future__ import annotations

import argparse
import json

import pytest

import register_internal_agent as ria


@pytest.fixture
def redirect_paths(monkeypatch, tmp_path):
    agents_file = tmp_path / "memory" / "agents.json"
    internal_dir = tmp_path / "messages" / "internal"
    chat_dir = tmp_path / "memory" / "chat"
    agents_file.parent.mkdir(parents=True, exist_ok=True)
    internal_dir.mkdir(parents=True, exist_ok=True)
    chat_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ria, "AGENTS_FILE", agents_file)
    monkeypatch.setattr(ria, "INTERNAL_DIR", internal_dir)
    # Chat trio (chat_history.json, archive, .session) lives under the
    # unified /agent/memory/chat/<name>/ tree — redirect that root too.
    import shared as _services_shared

    monkeypatch.setattr(_services_shared, "CHAT_DIR", chat_dir)
    monkeypatch.setattr(
        _services_shared,
        "CHAT_MIGRATION_SENTINEL",
        chat_dir / ".migration_done",
    )
    return {"agents": agents_file, "internal": internal_dir, "chat": chat_dir}


def _args(**overrides):
    base = dict(
        name="planner",
        responsibilities="r",
        system_prompt_file=None,
        system_prompt_inline=None,
        outbox_routing_rules_file=None,
        outbox_routing_rules_inline=None,
        model=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# _ensure_files
# ---------------------------------------------------------------------------


def test_ensure_files_creates_three_empty_lists(redirect_paths):
    inbox, inbox_history, chat_history = ria._ensure_files("planner")
    for f in (inbox, inbox_history, chat_history):
        assert f.exists()
        assert json.loads(f.read_text()) == []


def test_ensure_files_idempotent_does_not_clobber(redirect_paths):
    inbox, *_ = ria._ensure_files("planner")
    inbox.write_text('[{"x":1}]')
    ria._ensure_files("planner")
    assert json.loads(inbox.read_text()) == [{"x": 1}]


# ---------------------------------------------------------------------------
# _load_json_arg / _load_text_arg
# ---------------------------------------------------------------------------


def test_load_json_arg_inline():
    assert ria._load_json_arg(None, '[{"x":1}]') == [{"x": 1}]


def test_load_json_arg_file(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text('[{"description":"d","agent":"main"}]')
    assert ria._load_json_arg(str(p), None) == [{"description": "d", "agent": "main"}]


def test_load_json_arg_neither_returns_none():
    assert ria._load_json_arg(None, None) is None


def test_load_text_arg_inline_empty_string_kept():
    # Inline empty string is meaningful (explicit empty appendix); preserved
    assert ria._load_text_arg(None, "") == ""


def test_load_text_arg_neither_returns_none():
    assert ria._load_text_arg(None, None) is None


# ---------------------------------------------------------------------------
# cmd_register
# ---------------------------------------------------------------------------


def test_cmd_register_invalid_name(redirect_paths):
    with pytest.raises(SystemExit):
        ria.cmd_register(_args(name="bad name!"))


def test_cmd_register_writes_minimal_entry(redirect_paths):
    rc = ria.cmd_register(_args())
    assert rc == 0
    agents = json.loads(redirect_paths["agents"].read_text())
    assert len(agents) == 1
    a = agents[0]
    assert a["type"] == "internal"
    assert a["name"] == "planner"
    assert a["status"] == "online"
    assert a["responsibilities"] == "r"
    # `inbox` MUST be present and point at the per-agent inbox file
    assert a["inbox"].endswith("/messages/internal/planner/inbox.json")
    # No spurious customization fields
    assert "allowed_tools" not in a
    assert "permission_mode" not in a
    assert "cwd" not in a
    # Inbox files are still under messages/internal/<name>/...
    assert (redirect_paths["internal"] / "planner" / "inbox.json").exists()
    assert (redirect_paths["internal"] / "planner" / "inbox_history.json").exists()
    # ...but chat-side files now live under memory/chat/<name>/.
    assert (redirect_paths["chat"] / "planner" / "chat_history.json").exists()
    assert (redirect_paths["chat"] / "planner" / "chat_history_archive.json").exists()
    assert (redirect_paths["chat"] / "planner" / "planner.session").exists()


def test_cmd_register_with_system_prompt_and_rules(redirect_paths, tmp_path):
    rules_file = tmp_path / "r.json"
    rules_file.write_text(
        json.dumps(
            [
                {"description": "back to main", "agent": "main"},
                {"description": "research handoff", "agent": "research"},
            ]
        )
    )
    args = _args(
        system_prompt_inline="be terse",
        outbox_routing_rules_file=str(rules_file),
    )
    assert ria.cmd_register(args) == 0
    agents = json.loads(redirect_paths["agents"].read_text())
    assert agents[0]["system_prompt"] == "be terse"
    assert agents[0]["outbox_routing_rules"] == [
        {"description": "back to main", "agent": "main"},
        {"description": "research handoff", "agent": "research"},
    ]


def test_cmd_register_rejects_non_list_rules(redirect_paths):
    with pytest.raises(SystemExit):
        ria.cmd_register(_args(outbox_routing_rules_inline='{"not":"list"}'))


def test_cmd_register_rejects_rule_without_agent(redirect_paths):
    with pytest.raises(SystemExit):
        ria.cmd_register(_args(outbox_routing_rules_inline='[{"description":"d"}]'))


def test_cmd_register_rejects_rule_without_description(redirect_paths):
    with pytest.raises(SystemExit):
        ria.cmd_register(_args(outbox_routing_rules_inline='[{"agent":"main"}]'))


def test_cmd_register_rejects_non_object_rule(redirect_paths):
    with pytest.raises(SystemExit):
        ria.cmd_register(_args(outbox_routing_rules_inline='["string"]'))


def test_cmd_register_merges_existing_entry(redirect_paths):
    # First registration
    ria.cmd_register(_args(responsibilities="v1"))
    # Re-register with new responsibilities + system_prompt
    ria.cmd_register(_args(responsibilities="v2", system_prompt_inline="sp"))
    agents = json.loads(redirect_paths["agents"].read_text())
    assert len(agents) == 1
    assert agents[0]["responsibilities"] == "v2"
    assert agents[0]["system_prompt"] == "sp"


def test_cmd_register_writes_model_when_provided(redirect_paths):
    rc = ria.cmd_register(_args(model="haiku"))
    assert rc == 0
    agents = json.loads(redirect_paths["agents"].read_text())
    assert agents[0]["model"] == "haiku"


def test_cmd_register_omits_model_when_absent(redirect_paths):
    ria.cmd_register(_args())  # model defaults to None
    agents = json.loads(redirect_paths["agents"].read_text())
    assert "model" not in agents[0]


def test_cmd_register_blank_model_treated_as_absent(redirect_paths):
    ria.cmd_register(_args(model="   "))
    agents = json.loads(redirect_paths["agents"].read_text())
    assert "model" not in agents[0]


def test_cmd_register_strips_whitespace_around_model(redirect_paths):
    ria.cmd_register(_args(model="  sonnet  "))
    agents = json.loads(redirect_paths["agents"].read_text())
    assert agents[0]["model"] == "sonnet"


def test_cmd_register_merge_replaces_model(redirect_paths):
    ria.cmd_register(_args(model="haiku"))
    ria.cmd_register(_args(model="opus"))
    agents = json.loads(redirect_paths["agents"].read_text())
    assert len(agents) == 1
    assert agents[0]["model"] == "opus"


def test_cmd_register_revives_deactivated(redirect_paths):
    # Seed an existing deactivated entry
    redirect_paths["agents"].write_text(
        json.dumps(
            [
                {
                    "type": "internal",
                    "name": "planner",
                    "status": "deactivated",
                }
            ]
        )
    )
    ria.cmd_register(_args())
    agents = json.loads(redirect_paths["agents"].read_text())
    assert agents[0]["status"] == "online"


# ---------------------------------------------------------------------------
# cmd_deactivate
# ---------------------------------------------------------------------------


def test_cmd_deactivate_marks_status(redirect_paths):
    ria.cmd_register(_args())
    rc = ria.cmd_deactivate(argparse.Namespace(deactivate="planner"))
    assert rc == 0
    agents = json.loads(redirect_paths["agents"].read_text())
    assert agents[0]["status"] == "deactivated"


def test_cmd_deactivate_unknown_returns_1(redirect_paths, capsys):
    rc = ria.cmd_deactivate(argparse.Namespace(deactivate="ghost"))
    assert rc == 1


def test_cmd_deactivate_only_touches_internal_type(redirect_paths):
    redirect_paths["agents"].write_text(
        json.dumps(
            [
                {"type": "external", "name": "planner", "status": "online"},
            ]
        )
    )
    rc = ria.cmd_deactivate(argparse.Namespace(deactivate="planner"))
    # External agent with same name must NOT be deactivated by this script
    assert rc == 1
    agents = json.loads(redirect_paths["agents"].read_text())
    assert agents[0]["status"] == "online"


# ---------------------------------------------------------------------------
# cmd_list
# ---------------------------------------------------------------------------


def test_cmd_list_empty(redirect_paths, capsys):
    ria.cmd_list(argparse.Namespace())
    out = capsys.readouterr().out
    assert "no internal agents" in out


def test_cmd_list_shows_internal_only(redirect_paths, capsys):
    redirect_paths["agents"].write_text(
        json.dumps(
            [
                {"type": "internal", "name": "planner", "status": "online"},
                {"type": "external", "name": "research", "status": "online"},
            ]
        )
    )
    ria.cmd_list(argparse.Namespace())
    out = capsys.readouterr().out
    assert "planner" in out
    assert "research" not in out
