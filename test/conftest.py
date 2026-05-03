"""Shared fixtures for the services test suite.

Every fixture here aims at the same goal: redirect the production-only
``/agent/...`` paths the services hard-code at module import time onto a
``tmp_path``-rooted sandbox, so tests run on any host without touching real
state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Filesystem sandbox
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_root(tmp_path: Path) -> Path:
    """A tmp dir mirroring the /agent layout the services expect."""
    for sub in (
        "messages",
        "messages/external",
        "messages/bridge/telegram",
        "messages/bridge/whatsapp",
        "memory",
        "memory/heartbeats",
        "memory/logs",
        "workspace/upload",
        "workspace/telegram",
        "workspace/whatsapp",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Time freezing
# ---------------------------------------------------------------------------


class FrozenDatetime(datetime):
    """A datetime subclass whose ``now()`` returns a fixed instant.

    ``datetime`` is a C type, so ``monkeypatch.setattr(datetime, "now", ...)``
    raises ``TypeError``. The working pattern is to swap the *module-level
    alias* each service imports (``from datetime import datetime``) with this
    subclass.
    """

    _frozen: datetime = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        if tz is None:
            return cls._frozen.replace(tzinfo=None)
        return cls._frozen.astimezone(tz)

    @classmethod
    def utcnow(cls):  # type: ignore[override]
        return cls._frozen.replace(tzinfo=None)


def make_frozen_at(instant: datetime) -> type:
    """Build a FrozenDatetime variant pinned to *instant*."""

    class _Pinned(FrozenDatetime):
        _frozen = instant

    return _Pinned


@pytest.fixture
def frozen_now(monkeypatch):
    """Freeze ``datetime.now`` in every service module that imports it.

    Returns a callable: ``freeze(instant)`` rebinds the module-level
    ``datetime`` alias on every loaded service module to a subclass whose
    ``.now()`` returns *instant*.
    """
    target_modules = (
        "shared",
        "external_agent_api",
        "scheduler_daemon",
        "telegram_bridge",
        "webhook.whatsapp_bridge_handler",
        "webhook_receiver",
    )

    def freeze(instant: datetime):
        import importlib
        import sys

        pinned = make_frozen_at(instant)
        for modname in target_modules:
            mod = sys.modules.get(modname)
            if mod is None:
                try:
                    mod = importlib.import_module(modname)
                except ImportError:
                    continue
            if hasattr(mod, "datetime"):
                monkeypatch.setattr(f"{modname}.datetime", pinned, raising=False)
        return pinned

    return freeze


# ---------------------------------------------------------------------------
# Path patchers — one per service module
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_shared_paths(monkeypatch, agent_root):
    import shared

    monkeypatch.setattr(shared, "MESSAGES_DIR", agent_root / "messages")
    monkeypatch.setattr(shared, "INBOX_FILE", agent_root / "messages" / "inbox.json")
    monkeypatch.setattr(
        shared,
        "SERVER_ERRORS_FILE",
        agent_root / "memory" / "server_errors.json",
    )
    return shared


@pytest.fixture
def patch_eaa_paths(monkeypatch, agent_root, patch_shared_paths):
    import external_agent_api as eaa

    monkeypatch.setattr(eaa, "BASE", agent_root)
    monkeypatch.setattr(eaa, "AGENTS_FILE", agent_root / "memory" / "agents.json")
    monkeypatch.setattr(eaa, "EXTERNAL_DIR", agent_root / "messages" / "external")
    monkeypatch.setattr(eaa, "LOG_DIR", agent_root / "memory" / "logs")
    monkeypatch.setattr(
        eaa, "LOG_FILE", agent_root / "memory" / "logs" / "external_agent_api.log"
    )
    monkeypatch.setattr(eaa, "HEARTBEAT_DIR", agent_root / "memory" / "heartbeats")
    monkeypatch.setattr(
        eaa,
        "HEARTBEAT_FILE",
        agent_root / "memory" / "heartbeats" / "external_agent_api.heartbeat",
    )
    monkeypatch.setattr(
        eaa, "WORKSPACE_UPLOAD_DIR", agent_root / "workspace" / "upload"
    )
    return eaa


@pytest.fixture
def patch_scheduler_daemon_paths(monkeypatch, agent_root):
    import scheduler_daemon as sd

    monkeypatch.setattr(sd, "BASE", agent_root)
    monkeypatch.setattr(sd, "LOG_DIR", agent_root / "memory" / "logs")
    monkeypatch.setattr(
        sd, "LOG_FILE", agent_root / "memory" / "logs" / "scheduler_daemon.log"
    )
    monkeypatch.setattr(sd, "HEARTBEAT_DIR", agent_root / "memory" / "heartbeats")
    monkeypatch.setattr(
        sd,
        "HEARTBEAT_FILE",
        agent_root / "memory" / "heartbeats" / "scheduler_daemon.heartbeat",
    )
    return sd


@pytest.fixture
def patch_telegram_paths(monkeypatch, agent_root, patch_shared_paths):
    import telegram_bridge as tb

    monkeypatch.setattr(tb, "BASE", agent_root)
    monkeypatch.setattr(tb, "STATE_FILE", agent_root / "memory" / "telegram_state.json")
    monkeypatch.setattr(tb, "INBOX_FILE", agent_root / "messages" / "inbox.json")
    monkeypatch.setattr(tb, "OUTBOX_FILE", agent_root / "messages" / "outbox.json")
    bridge_dir = agent_root / "messages" / "bridge" / "telegram"
    monkeypatch.setattr(tb, "BRIDGE_DIR", bridge_dir)
    monkeypatch.setattr(tb, "INBOX_HISTORY_FILE", bridge_dir / "inbox_history.json")
    monkeypatch.setattr(tb, "OUTBOX_HISTORY_FILE", bridge_dir / "outbox_history.json")
    monkeypatch.setattr(tb, "CHAT_HISTORY_FILE", bridge_dir / "chat_history.json")
    monkeypatch.setattr(tb, "LOG_DIR", agent_root / "memory" / "logs")
    monkeypatch.setattr(
        tb, "LOG_FILE", agent_root / "memory" / "logs" / "telegram_bridge.log"
    )
    monkeypatch.setattr(tb, "HEARTBEAT_DIR", agent_root / "memory" / "heartbeats")
    monkeypatch.setattr(
        tb,
        "HEARTBEAT_FILE",
        agent_root / "memory" / "heartbeats" / "telegram_bridge.heartbeat",
    )
    monkeypatch.setattr(tb, "LOCK_FILE", agent_root / "memory" / "telegram_bridge.lock")
    monkeypatch.setattr(tb, "STATE_JSON", agent_root / "memory" / "state.json")
    monkeypatch.setattr(tb, "MEDIA_DIR", agent_root / "workspace" / "telegram")
    # Reset the cached heartbeat so tests get a clean read.
    monkeypatch.setattr(tb, "_heartbeat_cache", {"mtime": 0.0, "value": None})
    return tb


@pytest.fixture
def patch_whatsapp_paths(monkeypatch, agent_root, patch_shared_paths):
    from webhook import whatsapp_bridge_handler as wa

    monkeypatch.setattr(wa, "BASE", agent_root)
    monkeypatch.setattr(wa, "STATE_FILE", agent_root / "memory" / "whatsapp_state.json")
    monkeypatch.setattr(wa, "INBOX_FILE", agent_root / "messages" / "inbox.json")
    monkeypatch.setattr(wa, "OUTBOX_FILE", agent_root / "messages" / "outbox.json")
    bridge_dir = agent_root / "messages" / "bridge" / "whatsapp"
    monkeypatch.setattr(wa, "BRIDGE_DIR", bridge_dir)
    monkeypatch.setattr(wa, "INBOX_HISTORY_FILE", bridge_dir / "inbox_history.json")
    monkeypatch.setattr(wa, "OUTBOX_HISTORY_FILE", bridge_dir / "outbox_history.json")
    monkeypatch.setattr(wa, "CHAT_HISTORY_FILE", bridge_dir / "chat_history.json")
    monkeypatch.setattr(wa, "STATE_JSON", agent_root / "memory" / "state.json")
    monkeypatch.setattr(wa, "MEDIA_DIR", agent_root / "workspace" / "whatsapp")
    return wa


@pytest.fixture
def patch_iac_paths(monkeypatch, agent_root, patch_shared_paths):
    """Redirect internal_agent_chat's hardcoded /agent paths into agent_root."""
    import internal_agent_chat as iac

    monkeypatch.setattr(iac, "BASE", agent_root)
    # internal_agent_chat does `from shared import INBOX_FILE, MESSAGES_DIR`,
    # so the rebound names in `iac`'s namespace must be patched alongside
    # the originals on `shared` — `patch_shared_paths` only catches the
    # latter.
    monkeypatch.setattr(iac, "INBOX_FILE", agent_root / "messages" / "inbox.json")
    monkeypatch.setattr(iac, "MESSAGES_DIR", agent_root / "messages")
    monkeypatch.setattr(iac, "AGENTS_FILE", agent_root / "memory" / "agents.json")
    monkeypatch.setattr(iac, "INTERNAL_DIR", agent_root / "messages" / "internal")
    monkeypatch.setattr(
        iac, "SESSIONS_DIR", agent_root / "memory" / "sessions" / "internal"
    )
    monkeypatch.setattr(iac, "LOG_DIR", agent_root / "memory" / "logs")
    monkeypatch.setattr(
        iac, "LOG_FILE", agent_root / "memory" / "logs" / "internal_agent_chat.log"
    )
    monkeypatch.setattr(iac, "HEARTBEAT_DIR", agent_root / "memory" / "heartbeats")
    monkeypatch.setattr(
        iac,
        "HEARTBEAT_FILE",
        agent_root / "memory" / "heartbeats" / "internal_agent_chat.heartbeat",
    )
    # System prompt source files — point them at non-existing paths so the
    # builder produces an empty prompt unless the test seeds them.
    monkeypatch.setattr(iac, "SYSTEM_MD", agent_root / "system.md")
    monkeypatch.setattr(iac, "CONSTITUTION_MD", agent_root / "constitution.md")
    monkeypatch.setattr(iac, "PORTAL_CONFIG", agent_root / "portal_config.json")
    monkeypatch.setattr(
        iac, "CLAUDE_SYSTEM_PROMPT_MD", agent_root / "claude-system-prompt.md"
    )
    (agent_root / "messages" / "internal").mkdir(parents=True, exist_ok=True)
    return iac


@pytest.fixture
def patch_webhook_receiver_paths(monkeypatch, agent_root, patch_shared_paths):
    import webhook_receiver as wr

    monkeypatch.setattr(wr, "BASE", agent_root)
    monkeypatch.setattr(wr, "LOG_DIR", agent_root / "memory" / "logs")
    monkeypatch.setattr(
        wr, "LOG_FILE", agent_root / "memory" / "logs" / "webhook_receiver.log"
    )
    monkeypatch.setattr(wr, "HEARTBEAT_DIR", agent_root / "memory" / "heartbeats")
    monkeypatch.setattr(
        wr,
        "HEARTBEAT_FILE",
        agent_root / "memory" / "heartbeats" / "webhook_receiver.heartbeat",
    )
    return wr
