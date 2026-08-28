"""Tests for scripts/register_external_agent.py."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

import register_external_agent as rea


@pytest.fixture
def redirect_paths(monkeypatch, tmp_path):
    agents_file = tmp_path / "memory" / "agents.json"
    external_dir = tmp_path / "messages" / "external"
    portal_cfg = tmp_path / "memory" / "portal_config.json"
    keepass_db = tmp_path / "keepass.kdbx"
    agents_file.parent.mkdir(parents=True, exist_ok=True)
    external_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rea, "AGENTS_FILE", agents_file)
    monkeypatch.setattr(rea, "EXTERNAL_DIR", external_dir)
    monkeypatch.setattr(rea, "PORTAL_CONFIG_PATH", portal_cfg)
    monkeypatch.setattr(rea, "KEEPASS_DB", keepass_db)
    return {
        "agents": agents_file,
        "external": external_dir,
        "portal": portal_cfg,
        "keepass": keepass_db,
    }


# ─── Helpers ────────────────────────────────────────────────────────────────


def test_ensure_files_creates_inbox_outbox(redirect_paths):
    inbox, outbox = rea._ensure_files("bot")
    assert inbox.exists()
    assert outbox.exists()
    assert json.loads(inbox.read_text()) == []


def test_ensure_files_idempotent(redirect_paths):
    inbox, _ = rea._ensure_files("bot")
    inbox.write_text('[{"x":1}]')
    rea._ensure_files("bot")
    # Existing content preserved
    assert json.loads(inbox.read_text()) == [{"x": 1}]


def test_load_capabilities_from_inline():
    args = argparse.Namespace(
        capabilities_file=None, capabilities_inline='[{"id":"a"}]'
    )
    assert rea._load_capabilities(args) == [{"id": "a"}]


def test_load_capabilities_empty():
    args = argparse.Namespace(capabilities_file=None, capabilities_inline=None)
    assert rea._load_capabilities(args) == []


def test_load_capabilities_from_file(tmp_path):
    f = tmp_path / "c.json"
    f.write_text('[{"id":"x"}]')
    args = argparse.Namespace(capabilities_file=str(f), capabilities_inline=None)
    assert rea._load_capabilities(args) == [{"id": "x"}]


def test_load_capabilities_not_a_list():
    args = argparse.Namespace(
        capabilities_file=None, capabilities_inline='{"not":"list"}'
    )
    with pytest.raises(SystemExit):
        rea._load_capabilities(args)


def test_load_public_url_missing(redirect_paths):
    assert rea._load_public_url() is None


def test_load_public_url_present(redirect_paths):
    redirect_paths["portal"].parent.mkdir(parents=True, exist_ok=True)
    redirect_paths["portal"].write_text(json.dumps({"public_url": "https://x.com/"}))
    assert rea._load_public_url() == "https://x.com"


def test_load_public_url_corrupt(redirect_paths):
    redirect_paths["portal"].parent.mkdir(parents=True, exist_ok=True)
    redirect_paths["portal"].write_text("nope")
    assert rea._load_public_url() is None


def test_load_basic_auth_no_db(redirect_paths):
    assert rea._load_basic_auth() == (None, None)


# ─── cmd_register ───────────────────────────────────────────────────────────


def test_cmd_register_invalid_name(redirect_paths):
    args = argparse.Namespace(
        name="bad name!",
        timeout_seconds=300,
        responsibilities="r",
        capabilities_file=None,
        capabilities_inline=None,
    )
    with pytest.raises(SystemExit):
        rea.cmd_register(args)


def test_cmd_register_bad_timeout(redirect_paths):
    args = argparse.Namespace(
        name="bot",
        timeout_seconds=0,
        responsibilities="r",
        capabilities_file=None,
        capabilities_inline=None,
    )
    with pytest.raises(SystemExit):
        rea.cmd_register(args)


def test_cmd_register_creates_entry(redirect_paths, capsys):
    args = argparse.Namespace(
        name="bot",
        timeout_seconds=300,
        responsibilities="do stuff",
        capabilities_file=None,
        capabilities_inline='[{"id":"x"}]',
    )
    rc = rea.cmd_register(args)
    assert rc == 0
    agents = json.loads(redirect_paths["agents"].read_text())
    assert len(agents) == 1
    assert agents[0]["name"] == "bot"
    assert agents[0]["status"] == "offline"
    assert agents[0]["timeout_seconds"] == 300


def test_cmd_register_updates_existing(redirect_paths):
    redirect_paths["agents"].write_text(
        json.dumps(
            [
                {
                    "type": "external",
                    "name": "bot",
                    "status": "online",
                    "last_ping_at": "2024-01-01T00:00:00",
                    "responsibilities": "old",
                    "timeout_seconds": 100,
                    "capabilities": [],
                    "inbox": "",
                    "outbox": "",
                }
            ]
        )
    )
    args = argparse.Namespace(
        name="bot",
        timeout_seconds=600,
        responsibilities="new",
        capabilities_file=None,
        capabilities_inline=None,
    )
    rc = rea.cmd_register(args)
    assert rc == 0
    agents = json.loads(redirect_paths["agents"].read_text())
    assert len(agents) == 1
    assert agents[0]["responsibilities"] == "new"
    assert agents[0]["timeout_seconds"] == 600
    assert agents[0]["last_ping_at"] == "2024-01-01T00:00:00"  # preserved


# ─── cmd_list ───────────────────────────────────────────────────────────────


def test_cmd_list_empty(redirect_paths, capsys):
    rc = rea.cmd_list(argparse.Namespace())
    assert rc == 0
    assert "no agents registered" in capsys.readouterr().out


def test_cmd_list_with_entries(redirect_paths, capsys):
    redirect_paths["agents"].write_text(
        json.dumps(
            [
                {
                    "name": "bot",
                    "type": "external",
                    "status": "online",
                    "timeout_seconds": 300,
                    "last_ping_at": "2024-01-01",
                }
            ]
        )
    )
    rc = rea.cmd_list(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "bot" in out


# ─── cmd_deactivate ────────────────────────────────────────────────────────


def test_cmd_deactivate_not_found(redirect_paths, capsys):
    redirect_paths["agents"].write_text("[]")
    args = argparse.Namespace(deactivate="ghost")
    rc = rea.cmd_deactivate(args)
    assert rc == 1


def test_cmd_deactivate_success(redirect_paths, capsys):
    redirect_paths["agents"].write_text(
        json.dumps([{"name": "bot", "status": "online"}])
    )
    args = argparse.Namespace(deactivate="bot")
    rc = rea.cmd_deactivate(args)
    assert rc == 0
    agents = json.loads(redirect_paths["agents"].read_text())
    assert agents[0]["status"] == "deactivated"


# ─── cmd_setup ──────────────────────────────────────────────────────────────


def test_cmd_setup_invalid_name(redirect_paths, capsys):
    args = argparse.Namespace(setup="bad name!")
    assert rea.cmd_setup(args) == 1


def test_cmd_setup_unknown_agent(redirect_paths, capsys):
    redirect_paths["agents"].write_text("[]")
    args = argparse.Namespace(setup="bot")
    assert rea.cmd_setup(args) == 1


def test_cmd_setup_prints_instruction(redirect_paths, monkeypatch, capsys):
    redirect_paths["agents"].write_text(json.dumps([{"name": "bot"}]))
    monkeypatch.setattr(rea, "_load_public_url", lambda: "https://example.com")
    monkeypatch.setattr(rea, "_load_basic_auth", lambda: ("user", "pass"))
    args = argparse.Namespace(setup="bot")
    assert rea.cmd_setup(args) == 0
    out = capsys.readouterr().out
    assert "/loop" in out
    assert "https://example.com/external-agent" in out
    assert "user:pass" in out


# ─── main / argparse ────────────────────────────────────────────────────────


def test_main_requires_name(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["register_external_agent.py"])
    with pytest.raises(SystemExit):
        rea.main()


def test_main_list_dispatch(redirect_paths, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["register_external_agent.py", "--list"])
    rc = rea.main()
    assert rc == 0
