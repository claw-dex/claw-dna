#!/usr/bin/env python3
"""
Internal Agent Chat Service
===========================
Headless daemon that hosts a fleet of long-lived `claude_agent_sdk`
sessions, one per registered *internal* agent in
`/agent/memory/agents.json`.

No HTTP surface. Inputs come from the filesystem:

  /agent/messages/internal/<name>/inbox.json              # senders drop envelopes
  /agent/messages/internal/<name>/inbox_history.json      # daemon archives drained
  /agent/memory/chat/<name>/chat_history.json             # turn-by-turn transcript
  /agent/memory/chat/<name>/chat_history_archive.json     # records preserved across clear_chat
  /agent/memory/chat/<name>/<name>.session                # SDK resume id (bare string)

Per-message flow:

  1. Sender appends a `{type:"message", subject, content, reply_to, from,
     timestamp}` envelope to the agent's `inbox.json`.
  2. Sweeper (10 s) sees unread items and pokes the session.
  3. After the current turn (if any) finishes, the session atomically pops
     the inbox, stamps each envelope with a fresh uuid4 `id` +
     `processed_at`, archives them to `inbox_history.json`, and groups
     them by `reply_to`.
  4. Each group becomes one merged user turn into the SDK session.
  5. The assistant's response is persisted to `chat_history.json` (both
     the `user` and `assistant` records carry the same `source_ids`).
  6. Outbound delivery is driven by the LLM itself via the per-session
     `mcp__internal_agent_routing__send_reply` MCP tool. The tool's
     description is built from the agent's `outbox_routing_rules` so the
     LLM sees, in-context, which named recipients are valid and what
     each routing scenario means. Two calling modes: `agent=<name>`
     (deliver to that agent's `inbox` from agents.json — `main` is
     reserved and always available) or `message_id=<inbox-id>` (use the
     original message's `reply_to` — fails if absent).

Setup (no port — heartbeat-only liveness):
  uv run python scripts/service_manager.py start internal_agent_chat \\
      -- uv run python services/internal_agent_chat.py
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import queue
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import json

from shared import (
    INBOX_FILE,
    MESSAGES_DIR,
    append_to_history,
    chat_archive_path,
    chat_history_path,
    ensure_chat_dir,
    load_session_id,
    locked_json_rw,
    migrate_chat_layout,
    read_json_file,
    save_session_id,
    session_path,
    surface_error,
    write_to_inbox,
    write_to_outbox,
)

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

# --- Paths ---
BASE = Path("/agent")
AGENTS_FILE = BASE / "memory" / "agents.json"
INTERNAL_DIR = MESSAGES_DIR / "internal"
LOG_DIR = BASE / "memory" / "logs"
LOG_FILE = LOG_DIR / "internal_agent_chat.log"
# Global JSONL audit log for operator-driven control-flag actions across
# every internal agent. One JSON object per line; each line carries an
# `agent` field so a single tail/grep can reconstruct the clear-history
# of any agent.
CONTROL_AUDIT_LOG = LOG_DIR / "internal-agent-control-audit.log"
HEARTBEAT_DIR = BASE / "memory" / "heartbeats"
HEARTBEAT_FILE = HEARTBEAT_DIR / "internal_agent_chat.heartbeat"

# --- System prompt sources (mirrors app/chat.py exactly) ---
SYSTEM_MD = Path("/agent/system.md")
CONSTITUTION_MD = Path("/agent/constitution.md")
PORTAL_CONFIG = Path("/agent/memory/portal_config.json")
CLAUDE_SYSTEM_PROMPT_MD = Path("/home/agent/claude-system-prompt.md")

# --- Config ---
SWEEP_SECONDS = 10
TURN_TIMEOUT_SECONDS = 600  # safety cap on a single turn
INBOX_HISTORY_MAX = 500
SDK_CONNECT_TIMEOUT = 30
# System-prompt history selection: include every chat record from the last
# CHAT_HISTORY_RECENT_HOURS regardless of count; if none qualify, fall back
# to the most recent CHAT_HISTORY_SOFT_LIMIT records, extending the window
# by one entry at either end so a user/assistant pair is never split.
CHAT_HISTORY_RECENT_HOURS = 24
CHAT_HISTORY_SOFT_LIMIT = 20

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agent_dir(name: str) -> Path:
    return INTERNAL_DIR / name


def _inbox_path(name: str) -> Path:
    return _agent_dir(name) / "inbox.json"


def _inbox_history_path(name: str) -> Path:
    return _agent_dir(name) / "inbox_history.json"


# Chat-side helpers (history / archive / session sidecar) are thin
# wrappers around the canonical `services.shared` paths so tests can keep
# using `iac._chat_history_path(name)` style access. The actual files
# live under `/agent/memory/chat/<name>/` — see CHAT_DIR in shared.py.
def _chat_history_path(name: str) -> Path:
    return chat_history_path(name)


def _chat_archive_path(name: str) -> Path:
    """Append-only archive of chat records preserved across `clear_chat`.

    Every `clear_chat` op moves the live `chat_history.json` contents into
    this file *before* truncation, so cleared turns are recoverable. Like
    `chat_history.json`, this file is never truncated by the daemon.
    """
    return chat_archive_path(name)


def _session_path(name: str) -> Path:
    return session_path(name)


# Operator-driven clears are encoded as boolean flags inside the agent's
# entry in agents.json under the `control` key, e.g.:
#     {"name": "planner", "type": "internal", ...,
#      "control": {"clear_chat": true, "clear_session": true}}
# The daemon consumes a flag by performing the requested op and then
# removing that key (and the empty `control` dict) under the
# agents.json file lock. This avoids a parallel sentinel-file channel
# and reuses the registry the daemon already polls every sweep tick.
CONTROL_FIELD = "control"
CONTROL_CLEAR_CHAT = "clear_chat"
CONTROL_CLEAR_SESSION = "clear_session"


def _ensure_agent_files(name: str) -> None:
    # Inbox files stay under /agent/messages/internal/<name>/.
    d = _agent_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    for f in (_inbox_path(name), _inbox_history_path(name)):
        if not f.exists():
            f.write_text("[]")
    # Chat files (history, archive, .session) live under
    # /agent/memory/chat/<name>/ — see CHAT_DIR in shared.py. A failure
    # here would leave the agent half-registered (inbox present, chat
    # missing); surface it loudly and re-raise so the caller can decide
    # whether to retry or roll back.
    try:
        ensure_chat_dir(name)
    except OSError as exc:
        log.error(
            "[%s] could not create chat dir %s: %s",
            name,
            chat_history_path(name).parent,
            exc,
        )
        surface_error(
            "internal_agent_chat",
            "ensure_chat_dir failed for " + name + ": " + str(exc),
            context="ensure_agent_files:" + name,
        )
        raise


def _archive_chat_history(name: str) -> tuple[bool, int, str | None]:
    """Atomically move the live chat_history.json contents into the
    archive.

    Steps, in order:
      1. Under the source file lock: capture the records and truncate
         `chat_history.json` to ``[]`` in a single read-modify-write so
         no record can be appended between read and clear (TOCTOU-safe).
         If the on-disk content is not a JSON list (corrupted /
         hand-edited), the truncation is REFUSED and the original bytes
         are preserved so the operator can intervene.
      2. Under the destination file lock: append the captured records
         to `chat_history_archive.json` (append-only, unbounded).
      3. On archive-write failure, restore the captured records to the
         source file under its lock so the live state is recovered.

    Returns ``(ok, archived_count, error_message)``. Caller truncates
    nothing else: the truncate is part of the swap.
    """
    src = _chat_history_path(name)
    dst = _chat_archive_path(name)

    captured: dict = {"records": None, "error": None}

    def _swap(items):
        if not isinstance(items, list):
            captured["error"] = "chat_history.json is not a JSON list"
            # Refuse the truncate — leave the file untouched.
            return items
        captured["records"] = list(items)
        return []

    if not locked_json_rw(_swap, json_file=src, default=[]):
        return False, 0, "locked_json_rw on chat_history failed"
    if captured["error"] is not None:
        return False, 0, captured["error"]

    records = captured["records"] or []
    if not records:
        return True, 0, None

    def _append(items):
        if not isinstance(items, list):
            items = []
        items.extend(records)
        return items

    if locked_json_rw(_append, json_file=dst, default=[]):
        return True, len(records), None

    # Archive write failed — put the captured records back in the live
    # file so we don't leave the operator with truncated, unarchived data.
    def _restore(items):
        if not isinstance(items, list):
            items = []
        # Prepend the captured records ahead of anything that landed
        # between the swap and the rollback (defensive — same-task usage
        # makes this case unlikely).
        return list(records) + items

    locked_json_rw(_restore, json_file=src, default=[])
    return False, 0, "locked_json_rw on archive failed (records restored to live file)"


def _append_audit_entry(name: str, entry: dict) -> None:
    """Append one JSONL line to the global control-audit log.

    Each line is a JSON object with the agent name merged in as the
    leading `agent` field, so a single tail/grep over
    ``CONTROL_AUDIT_LOG`` reconstructs every clear action across every
    agent. The append is serialized via a sidecar `.lock` flock and the
    file is opened in O_APPEND mode so concurrent writers stay
    line-aligned.

    Best-effort: a failure is logged but never raised, so an audit-write
    glitch cannot abort the operator action it is recording.
    """
    record = {"agent": name, **entry}
    line = json.dumps(record, separators=(",", ":")) + "\n"
    try:
        CONTROL_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(CONTROL_AUDIT_LOG) + ".lock"
        with open(lock_path, "a+") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                with open(CONTROL_AUDIT_LOG, "a", encoding="utf-8") as f:
                    f.write(line)
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except Exception as exc:
        log.warning("[%s] control-audit append raised: %s", name, exc)


def _load_agents() -> list:
    return read_json_file(AGENTS_FILE, default=[])


def _internal_agents(agents: list) -> dict:
    """Return {name: cfg} for every active type=internal entry."""
    out: dict = {}
    for a in agents:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "internal":
            continue
        name = a.get("name")
        if not isinstance(name, str) or not name:
            continue
        if a.get("status") == "deactivated":
            continue
        out[name] = a
    return out


def _parse_chat_ts(msg: dict) -> datetime | None:
    raw = msg.get("ts")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _select_history_for_prompt(chat_history: list) -> list:
    """Pick which chat records to inline into the system prompt.

    Rule: include every record from the last CHAT_HISTORY_RECENT_HOURS,
    regardless of count. If none qualify, fall back to the trailing
    CHAT_HISTORY_SOFT_LIMIT records. Then extend the window by one entry
    at either end so a user/assistant turn pair (same `source_ids`) is
    never split — keeps assistant replies attached to their prompts even
    if doing so exceeds the soft limit.
    """
    n = len(chat_history)
    if n == 0:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CHAT_HISTORY_RECENT_HOURS)
    recent_idx = []
    for i, m in enumerate(chat_history):
        if not isinstance(m, dict):
            continue
        ts = _parse_chat_ts(m)
        if ts is not None and ts >= cutoff:
            recent_idx.append(i)
    if recent_idx:
        start, end = recent_idx[0], recent_idx[-1]
    else:
        start = max(0, n - CHAT_HISTORY_SOFT_LIMIT)
        end = n - 1

    first = chat_history[start] if isinstance(chat_history[start], dict) else {}
    if (
        first.get("role") == "assistant"
        and start > 0
        and isinstance(chat_history[start - 1], dict)
        and chat_history[start - 1].get("role") == "user"
        and chat_history[start - 1].get("source_ids") == first.get("source_ids")
    ):
        start -= 1

    last = chat_history[end] if isinstance(chat_history[end], dict) else {}
    if (
        last.get("role") == "user"
        and end + 1 < n
        and isinstance(chat_history[end + 1], dict)
        and chat_history[end + 1].get("role") == "assistant"
        and chat_history[end + 1].get("source_ids") == last.get("source_ids")
    ):
        end += 1

    return [m for m in chat_history[start : end + 1] if isinstance(m, dict)]


def _build_system_prompt(
    chat_history: list | None = None, custom_appendix: str = ""
) -> str:
    """Build system prompt — kept in lock-step with `app/chat.py:_build_system_prompt`.

    The first three sections (`agent_system_prompt`, `agent_constitution`,
    `public_url`) match `heartbeat.sh`'s `build_system_prompt` byte-for-byte
    in tag names and order, so all three entry points (heartbeat shell agent,
    chat tab, internal-agent daemon) hand the SDK an identically-shaped
    header. After those, the chat tab and the internal-agent daemon append
    `<previous_chat_history>` and `<claude_system_prompt>`. The internal-
    agent daemon additionally appends a final `<internal_agent_system_prompt>`
    section carrying the agent-specific text from agents.json — the only
    customization we expose for internal agents.
    """
    parts: list[str] = []
    if SYSTEM_MD.exists():
        parts.append("<agent_system_prompt>")
        parts.append(SYSTEM_MD.read_text())
        parts.append("</agent_system_prompt>")
    if CONSTITUTION_MD.exists():
        parts.append("<agent_constitution>")
        parts.append(CONSTITUTION_MD.read_text())
        parts.append("</agent_constitution>")
    if PORTAL_CONFIG.exists():
        try:
            cfg = json.loads(PORTAL_CONFIG.read_text())
            public_url = cfg.get("public_url", "")
            if public_url:
                parts.append("<public_url>")
                parts.append("This agent is accessible at: " + public_url)
                parts.append(
                    "When sharing links with the user (portal, file explorer, "
                    "workspace files, generated reports), use this public URL "
                    "as the base instead of localhost:8080. For example:\n"
                    "- Portal: " + public_url + "/app/\n"
                    "- Static Web: "
                    + public_url
                    + "/web/ (static files from /agent/web/)\n"
                    "- File Explorer: " + public_url + "/_/\n"
                    "- Workspace files: "
                    + public_url
                    + "/_/agent/workspace/path/to/<filename>"
                )
                parts.append(
                    "Note: For internal operations (curl, health checks, Caddy "
                    "admin API), continue using localhost."
                )
                parts.append("</public_url>")
        except (json.JSONDecodeError, OSError):
            pass
    if CLAUDE_SYSTEM_PROMPT_MD.exists():
        parts.append("<claude_system_prompt>")
        parts.append(CLAUDE_SYSTEM_PROMPT_MD.read_text())
        parts.append("</claude_system_prompt>")
    if custom_appendix:
        parts.append("<internal_agent_system_prompt>")
        parts.append(custom_appendix)
        parts.append("</internal_agent_system_prompt>")
    if chat_history:
        selected = _select_history_for_prompt(chat_history)
        if selected:
            parts.append("<previous_chat_history>")
            parts.append(
                "Below is the conversation history from the previous session. "
                "Use it for context."
            )
            for msg in selected:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                parts.append("**" + str(role) + "**: " + str(content))
            parts.append("</previous_chat_history>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# send_reply tool — the only outbound channel for an internal agent.
# ---------------------------------------------------------------------------

# Reserved target name; never a registered agent.
MAIN_AGENT = "main"

# MCP server / tool naming. The LLM-visible tool name is
# `mcp__<server>__<tool>`.
ROUTING_MCP_SERVER = "internal_agent_routing"
SEND_REPLY_TOOL = "send_reply"
SEND_REPLY_TOOL_FQN = "mcp__" + ROUTING_MCP_SERVER + "__" + SEND_REPLY_TOOL

# Allowed envelope types — only the agent-originated reply types from
# the inbox.json schema. Internal agents may not synthesize `goal` /
# `message` / `event` entries; those are reserved for users / schedulers
# / webhooks respectively.
_ALLOWED_REPLY_TYPES = (
    "agent_response",
    "agent_needs_human",
    "agent_error",
    "agent_info",
)
# Source value the daemon stamps on every outbound reply. Matches the
# `source` enum from the inbox.json schema.
_REPLY_SOURCE = "internal_agent"


def _routing_rules(cfg: dict) -> list:
    rules = cfg.get("outbox_routing_rules")
    return rules if isinstance(rules, list) else []


def _allowed_agent_names(cfg: dict) -> list[str]:
    """Names the LLM may pass as `agent=...` to send_reply.

    Always includes MAIN_AGENT plus any `agent` listed in the rules
    (deduped, order-preserving).
    """
    seen: dict[str, None] = {MAIN_AGENT: None}
    for rule in _routing_rules(cfg):
        if not isinstance(rule, dict):
            continue
        nm = rule.get("agent")
        if isinstance(nm, str) and nm and nm not in seen:
            seen[nm] = None
    return list(seen.keys())


def _build_tool_description(cfg: dict) -> str:
    """Compose the per-agent description shown to the LLM for send_reply.

    The skeleton explains the two calling modes; then we append one bullet
    per routing rule (description + agent) so the LLM knows which named
    agent to choose for each scenario.
    """
    lines = [
        "Send a reply message into another agent's inbox.json. Use this "
        "tool to hand work off, return results, ask for help, or report "
        "errors — it is the ONLY way for you to deliver outbound messages.",
        "",
        "Two ways to address the recipient — exactly ONE of `agent` or "
        "`message_id` must be provided:",
        "",
        "1. agent=<name> — deliver directly to that agent's inbox. Valid "
        "names are 'main' (the main agent) plus the names listed below.",
        "2. message_id=<inbox-id> — reply to a specific inbound message "
        "(the id you saw in its [from=... id=<id>] header). The reply is "
        "sent to that message's `reply_to` path. Fails if the original "
        "message had no reply_to.",
        "",
        "Required arguments:",
        "- type: one of "
        + ", ".join(repr(t) for t in _ALLOWED_REPLY_TYPES)
        + ". Use 'agent_response' for normal results, 'agent_needs_human' "
        "when blocked and a human must intervene, 'agent_error' for an "
        "unrecoverable failure, 'agent_info' for unsolicited status.",
        "- content: the full message body (non-empty string). Include any "
        "subject/headline as the first line of `content` — there is no "
        "separate subject field.",
        "",
        "Optional argument:",
        "- priority: integer 1-5 (lower = more urgent). Omit if unsure.",
        "",
        "The daemon stamps `timestamp`, `source` ('internal_agent'), and "
        "`reply_to` (your own inbox) on every delivered envelope. "
        "`received_at` is stamped at the moment the envelope is written "
        "into the target inbox.json.",
    ]
    rules = _routing_rules(cfg)
    if rules:
        lines.append("")
        lines.append("Routing scenarios for `agent=<name>`:")
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            nm = str(rule.get("agent") or "").strip()
            desc = str(rule.get("description") or "").strip()
            if not nm:
                continue
            if desc:
                lines.append("- agent=" + nm + " — " + desc)
            else:
                lines.append("- agent=" + nm)
    else:
        lines.append("")
        lines.append(
            "(No routing rules configured for this agent; `agent=main` is "
            "always available, otherwise prefer `message_id`.)"
        )
    return "\n".join(lines)


def _resolve_agent_inbox(name: str) -> Path | None:
    """Map an agent name to its inbox path via agents.json.

    `main` always resolves to `shared.INBOX_FILE`. For any other name,
    look up the agents.json entry and use its `inbox` field. Returns
    None if the name is unknown or the entry has no `inbox`.
    """
    if name == MAIN_AGENT:
        return INBOX_FILE
    for a in _load_agents():
        if not isinstance(a, dict):
            continue
        if a.get("name") != name:
            continue
        inbox = a.get("inbox")
        if isinstance(inbox, str) and inbox:
            return Path(inbox)
        return None
    return None


def _safe_target_path(target: Path) -> bool:
    """True iff *target* is inside MESSAGES_DIR and named inbox.json."""
    try:
        target.resolve().relative_to(MESSAGES_DIR.resolve())
    except ValueError:
        return False
    return target.name == "inbox.json"


def _lookup_inbox_history_reply_to(self_name: str, message_id: str) -> str | None:
    history = read_json_file(_inbox_history_path(self_name), default=[])
    for m in history:
        if isinstance(m, dict) and m.get("id") == message_id:
            rt = m.get("reply_to")
            if isinstance(rt, str) and rt.strip():
                return rt.strip()
            return None
    return None


def _build_send_reply_handler(session_name: str, cfg: dict):
    """Build the bare async send_reply closure for a session.

    Returned separately from the MCP-server wrapper so unit tests can
    invoke it directly without spinning up MCP plumbing.
    """
    allowed_names = set(_allowed_agent_names(cfg))

    async def send_reply(args):  # noqa: ANN001  (SDK signature)
        args = args or {}
        agent_name = (args.get("agent") or "").strip()
        message_id = (args.get("message_id") or "").strip()
        msg_type = (args.get("type") or "").strip()
        content = args.get("content") or ""
        priority = args.get("priority")

        def _err(msg: str):
            return {
                "content": [{"type": "text", "text": "send_reply error: " + msg}],
                "is_error": True,
            }

        if bool(agent_name) == bool(message_id):
            return _err("provide exactly one of `agent` or `message_id`")
        if not msg_type:
            return _err("`type` is required")
        if msg_type not in _ALLOWED_REPLY_TYPES:
            return _err(
                "invalid type "
                + repr(msg_type)
                + "; must be one of "
                + str(list(_ALLOWED_REPLY_TYPES))
            )
        if not isinstance(content, str) or not content.strip():
            return _err("`content` must be a non-empty string")
        if priority is not None:
            if not isinstance(priority, int) or isinstance(priority, bool):
                return _err("`priority` must be an integer if provided")
            if priority < 1 or priority > 5:
                return _err("`priority` must be between 1 and 5")

        # Resolve target inbox
        if agent_name:
            if agent_name not in allowed_names:
                return _err(
                    "agent "
                    + repr(agent_name)
                    + " is not allowed; valid names: "
                    + str(sorted(allowed_names))
                )
            target = _resolve_agent_inbox(agent_name)
            if target is None:
                return _err(
                    "agent "
                    + repr(agent_name)
                    + " is not registered or has no `inbox` in agents.json"
                )
        else:
            reply_to = _lookup_inbox_history_reply_to(session_name, message_id)
            if reply_to is None:
                return _err(
                    "message_id "
                    + repr(message_id)
                    + " not found in inbox_history, or its `reply_to` is empty"
                )
            target = (BASE / reply_to.lstrip("/")).resolve()

        if not _safe_target_path(target):
            return _err(
                "delivery target must live under "
                + str(MESSAGES_DIR)
                + " and be named inbox.json: got "
                + str(target)
            )

        # Self-talk guard
        if target.resolve() == _inbox_path(session_name).resolve():
            return _err("cannot send a reply to your own inbox")

        # Schema per /agent/messages inbox.json spec: required fields are
        # type, content, timestamp, received_at, source. reply_to is
        # always set to THIS internal agent's own inbox so the recipient
        # can reply back via its own send_reply (or write_to_inbox).
        # `received_at` is intentionally NOT set here — it is stamped at
        # the moment the envelope is appended to the target file (see
        # below), so the timestamp reflects when the entry actually
        # landed in the recipient's inbox, not when send_reply was
        # invoked.
        envelope = {
            "type": msg_type,
            "content": content,
            "timestamp": _now_iso(),
            "source": _REPLY_SOURCE,
            "reply_to": "messages/internal/" + session_name + "/inbox.json",
        }
        # When the reply is addressed to a specific inbound message, stamp
        # `reply_to_id` so the recipient (e.g. the main agent's polling step)
        # can correlate this reply back to the originating delegated message.
        if message_id:
            envelope["reply_to_id"] = message_id
        if priority is not None:
            envelope["priority"] = priority

        # Main inbox uses shared.write_to_inbox so the heartbeat agent
        # picks the entry up exactly like every other source.
        # write_to_inbox stamps `received_at` itself at the moment of
        # write (services/shared.py:142-144).
        if target.resolve() == INBOX_FILE.resolve():
            ok = write_to_inbox([envelope], inbox_file=INBOX_FILE, dedup=False)
            if not ok:
                return _err("write_to_inbox failed; see service logs")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)

            def _rw(items):
                if not isinstance(items, list):
                    items = []
                # Stamp received_at at the moment of the actual write,
                # mirroring shared.write_to_inbox's behaviour for the
                # main inbox.
                envelope["received_at"] = _now_iso()
                items.append(envelope)
                return items

            if not locked_json_rw(_rw, json_file=target, default=[]):
                return _err("locked_json_rw failed; see service logs")

        # Mirror agent_needs_human into the main outbox so the existing
        # human-notification channels (Telegram / WhatsApp / etc.) pick
        # them up — same behaviour as external_agent_api.py:708-731. The
        # primary delivery has already succeeded; a failed mirror is
        # surfaced but does NOT fail the tool call.
        mirrored = False
        if msg_type == "agent_needs_human":
            mirror_env = {
                "type": "needs_human",
                "subject": "[from internal agent " + session_name + "]",
                "content": ("[from internal agent " + session_name + "] " + content),
                "timestamp": envelope["timestamp"],
            }
            if write_to_outbox([mirror_env]):
                mirrored = True
            else:
                log.warning(
                    "[%s] write_to_outbox failed for needs_human mirror",
                    session_name,
                )
                surface_error(
                    "internal_agent_chat",
                    "write_to_outbox failed for needs_human mirror",
                    context=session_name,
                )

        text = "Delivered " + msg_type + " to " + str(target)
        if mirrored:
            text += " (and mirrored to main outbox for human notification)"
        return {"content": [{"type": "text", "text": text}]}

    return send_reply


def _build_send_reply_server(session_name: str, cfg: dict):
    """Create an in-process MCP server exposing `send_reply` for one session.

    The tool's description is built from the agent's outbox_routing_rules
    so the LLM sees, in-context, which named agents it may target and
    when each is appropriate. The handler closes over `session_name` so
    self-talk and message_id lookups resolve against the right agent.
    """
    description = _build_tool_description(cfg)
    handler = _build_send_reply_handler(session_name, cfg)
    decorated = tool(
        SEND_REPLY_TOOL,
        description,
        {
            "agent": str,
            "message_id": str,
            "type": str,
            "content": str,
            "priority": int,
        },
    )(handler)
    return create_sdk_mcp_server(ROUTING_MCP_SERVER, tools=[decorated])


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class InternalAgentSession:
    """Long-lived ClaudeSDKClient bound to one internal agent.

    Lifecycle is single-threaded inside a dedicated asyncio loop running
    on its own daemon thread. Public methods are thread-safe and just
    schedule coroutines onto that loop.
    """

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg

        self._loop: asyncio.AbstractEventLoop | None = None
        self._sdk: ClaudeSDKClient | None = None
        self._thread = threading.Thread(
            target=self._run_loop, name="iac-" + name, daemon=True
        )
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._stop_event = threading.Event()

        self._lock = threading.Lock()
        self._busy = False
        self._pending_drain = threading.Event()

        self._session_id: str | None = self._load_session_id()
        self._chat_history: list = read_json_file(_chat_history_path(name), default=[])

    # ── boot / shutdown ────────────────────────────────────────

    def start(self) -> None:
        _ensure_agent_files(self.name)
        self._thread.start()
        if not self._ready.wait(timeout=SDK_CONNECT_TIMEOUT):
            raise TimeoutError(
                "SDK for internal agent '" + self.name + "' did not connect in time"
            )
        if self._error is not None:
            raise self._error

    def stop(self) -> None:
        # Set the stop flag, wake the worker, and let _run_loop exit
        # naturally — it will await sdk.disconnect() and finalize the
        # loop. Only force-stop the loop if the worker fails to exit in
        # time.
        self._stop_event.set()
        self._pending_drain.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)
        if self._thread.is_alive():
            log.warning("[%s] worker did not exit in 10s, forcing loop stop", self.name)
            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
            self._thread.join(timeout=5)

    def notify_inbox(self) -> None:
        """Mark that drain is pending. Worker picks it up between turns."""
        self._pending_drain.set()

    # ── thread entry ───────────────────────────────────────────

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
        except Exception as exc:
            self._error = exc
            self._ready.set()
            self._loop.close()
            return
        try:
            # Trigger one drain at startup in case messages piled up while down.
            self._pending_drain.set()
            self._loop.run_until_complete(self._worker())
        except Exception as exc:
            log.error("[%s] worker crashed: %s", self.name, exc, exc_info=True)
            surface_error("internal_agent_chat", exc, context="worker:" + self.name)
        finally:
            # Disconnect the SDK on the same loop while it is still running
            # so any in-flight subprocess gets reaped cleanly.
            if self._sdk is not None:
                try:
                    self._loop.run_until_complete(self._sdk.disconnect())
                except Exception:
                    pass
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()

    def _build_options(self) -> ClaudeAgentOptions:
        # Internal-agent SDK options are intentionally identical to
        # app/chat.py — keep these two call sites in lock-step. The only
        # per-agent customizations are:
        #   1. an optional `system_prompt` appended to the shared prompt;
        #   2. the per-session `send_reply` MCP tool, whose description
        #      is built from this agent's outbox_routing_rules;
        #   3. an optional `model` override (haiku/sonnet/opus, or a full
        #      model id) — passed through to the SDK, which handles alias
        #      → id resolution. Absent or blank → SDK default.
        custom = self.cfg.get("system_prompt") or ""
        if not isinstance(custom, str):
            custom = ""
        raw_model = self.cfg.get("model")
        model = (
            raw_model.strip()
            if isinstance(raw_model, str) and raw_model.strip()
            else None
        )
        routing_server = _build_send_reply_server(self.name, self.cfg)
        return ClaudeAgentOptions(
            system_prompt=_build_system_prompt(self._chat_history, custom),
            model=model,
            permission_mode="bypassPermissions",
            include_partial_messages=False,
            cwd="/agent",
            add_dirs=["/home/agent", "/home/agent/.claude", "/agent/.claude"],
            setting_sources=["user", "project"],
            mcp_servers={ROUTING_MCP_SERVER: routing_server},
            allowed_tools=[
                "Skill",
                "Bash",
                "Glob",
                "Grep",
                "Read",
                "Edit",
                "Write",
                "TodoWrite",
                "WebFetch",
                "WebSearch",
                "BashOutput",
                "KillShell",
                "ListMcpResourcesTool",
                "ReadMcpResourceTool",
                SEND_REPLY_TOOL_FQN,
            ],
            disallowed_tools=["AskUserQuestion"],
            resume=self._session_id,
        )

    async def _connect_sdk(self) -> None:
        options = self._build_options()
        self._sdk = ClaudeSDKClient(options)
        await self._sdk.connect()
        log.info("[%s] connected (resume=%s)", self.name, self._session_id or "<new>")

    async def _connect(self) -> None:
        await self._connect_sdk()
        self._ready.set()

    # ── worker loop ────────────────────────────────────────────

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            # Wait until the sweeper notifies us (or we self-trigger).
            await loop.run_in_executor(None, self._pending_drain.wait)
            if self._stop_event.is_set():
                break
            self._pending_drain.clear()
            try:
                await self._consume_control_flags()
            except Exception as exc:
                log.error(
                    "[%s] control-flag consume failed: %s",
                    self.name,
                    exc,
                    exc_info=True,
                )
                surface_error(
                    "internal_agent_chat",
                    exc,
                    context="control:" + self.name,
                )
            if self._sdk is None:
                # A previous clear_session reconnect failed and left us
                # without an SDK. Skip the drain — the next sweep will
                # re-trigger _consume_control_flags and retry the
                # reconnect (the clear_session flag is still set in
                # agents.json).
                continue
            try:
                await self._drain_and_process_all()
            except Exception as exc:
                log.error("[%s] drain failed: %s", self.name, exc, exc_info=True)
                surface_error(
                    "internal_agent_chat",
                    exc,
                    context="drain:" + self.name,
                )

    async def _drain_and_process_all(self) -> None:
        """Pop the inbox, group by reply_to, run a turn per group.

        Loops until the inbox is empty so messages that arrive while we
        are mid-turn are processed in the same drain pass — but checks
        the stop flag between turns so shutdown is prompt.
        """
        with self._lock:
            self._busy = True
        try:
            while not self._stop_event.is_set():
                groups = self._pop_and_group_inbox()
                if not groups:
                    return
                # Deterministic order: earliest timestamp first.
                ordered = sorted(
                    groups.items(),
                    key=lambda kv: min(str(m.get("timestamp") or "") for m in kv[1])
                    or "",
                )
                for reply_to, msgs in ordered:
                    if self._stop_event.is_set():
                        return
                    await self._run_turn_for_group(reply_to, msgs)
        finally:
            with self._lock:
                self._busy = False

    def _pop_and_group_inbox(self) -> dict:
        """Atomically empty inbox.json; archive every popped item; return
        {reply_to_key: [stamped_msgs]} for processing.
        """
        popped_holder: dict = {"items": []}

        def _rw(items):
            if not isinstance(items, list):
                items = []
            popped_holder["items"] = items
            return []

        if not locked_json_rw(_rw, json_file=_inbox_path(self.name), default=[]):
            return {}

        popped = popped_holder["items"]
        if not popped:
            return {}

        valid: list[dict] = []
        seen_ids: set[str] = set()
        for raw in popped:
            if not isinstance(raw, dict):
                surface_error(
                    "internal_agent_chat",
                    "non-dict inbox entry",
                    context=self.name + ":" + repr(raw)[:200],
                )
                continue
            if raw.get("type") != "message":
                surface_error(
                    "internal_agent_chat",
                    "unsupported inbox type=" + str(raw.get("type")),
                    context=self.name,
                )
                continue
            stamped = dict(raw)
            # Preserve the sender-supplied id when present (e.g. a delegating
            # main agent's `delegated_message_id`); only mint a fresh uuid4
            # when no id was provided. This keeps end-to-end reply
            # correlation possible — a downstream `send_reply(message_id=…)`
            # stamps `reply_to_id` with this exact id on the reply envelope.
            # Defensive: if the same id appears twice in the same pop batch
            # (sender bug or replay), fall back to uuid4 for the duplicate to
            # preserve per-batch id uniqueness.
            existing_id = raw.get("id")
            candidate = (
                existing_id.strip()
                if isinstance(existing_id, str) and existing_id.strip()
                else None
            )
            if candidate and candidate in seen_ids:
                surface_error(
                    "internal_agent_chat",
                    "duplicate inbox id in pop batch — minting fresh uuid",
                    context=f"{self.name}: duplicate id={candidate}",
                )
                candidate = None
            stamped["id"] = candidate or str(uuid.uuid4())
            seen_ids.add(stamped["id"])
            stamped["processed_at"] = _now_iso()
            valid.append(stamped)

        # Archive every popped+stamped item before processing so a crash
        # mid-turn does not lose the audit trail.
        if valid:
            try:
                append_to_history(
                    valid,
                    _inbox_history_path(self.name),
                    max_entries=INBOX_HISTORY_MAX,
                )
            except Exception as exc:
                log.warning("[%s] failed to archive inbox history: %s", self.name, exc)

        groups: dict = {}
        for m in valid:
            key = m.get("reply_to") or "__none__"
            groups.setdefault(key, []).append(m)
        return groups

    # ── one turn ───────────────────────────────────────────────

    async def _run_turn_for_group(self, reply_to_key: str, msgs: list) -> None:
        prompt = self._build_user_prompt(msgs)
        ids = [m["id"] for m in msgs]

        chunk_q: queue.Queue = queue.Queue()
        done = asyncio.Event()
        text_parts: list[str] = []
        cost = None
        duration_ms = None
        is_error = False

        sdk = self._sdk
        assert sdk is not None

        async def _runner():
            try:
                await sdk.query(prompt)
                async for msg in sdk.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text:
                                text_parts.append(block.text)
                    elif isinstance(msg, ResultMessage):
                        sid = getattr(msg, "session_id", None)
                        if sid:
                            self._session_id = sid
                            self._save_session_id(sid)
                        chunk_q.put(
                            {
                                "cost": msg.total_cost_usd,
                                "duration_ms": msg.duration_ms,
                                "is_error": msg.is_error,
                            }
                        )
                        break
            except Exception as exc:
                chunk_q.put({"error": str(exc)})
            finally:
                done.set()

        task = asyncio.create_task(_runner())
        try:
            await asyncio.wait_for(done.wait(), timeout=TURN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            task.cancel()
            log.error("[%s] turn timed out after %ds", self.name, TURN_TIMEOUT_SECONDS)
            surface_error(
                "internal_agent_chat",
                "turn timeout",
                context=self.name + ":ids=" + ",".join(ids),
            )
            return

        # Drain queue
        while not chunk_q.empty():
            ev = chunk_q.get_nowait()
            if "error" in ev:
                is_error = True
                text_parts.append("[error] " + str(ev["error"]))
            else:
                cost = ev.get("cost")
                duration_ms = ev.get("duration_ms")
                is_error = bool(ev.get("is_error")) or is_error

        response_text = "".join(text_parts).strip()
        if not response_text:
            response_text = "(no response)"

        merged_reply_to = reply_to_key if reply_to_key != "__none__" else None
        self._append_chat_records(
            ids=ids,
            merged_reply_to=merged_reply_to,
            user_text=self._readable_user_record(msgs),
            assistant_text=response_text,
            session_id=self._session_id,
            cost_usd=cost,
            duration_ms=duration_ms,
            is_error=is_error,
        )

        # Outbound delivery is the LLM's responsibility — it must call
        # `mcp__internal_agent_routing__send_reply` during the turn. If
        # it forgets, the response is captured in chat_history.json but
        # not delivered anywhere; that is intentional so the LLM is in
        # full control of routing per the agent's rules.

    def _build_user_prompt(self, msgs: list) -> str:
        ordered = sorted(msgs, key=lambda m: str(m.get("timestamp") or ""))
        parts: list[str] = []
        for i, m in enumerate(ordered):
            header = (
                "[from="
                + str(m.get("from") or "?")
                + " reply_to="
                + str(m.get("reply_to") or "")
                + " id="
                + str(m.get("id"))
                + "]"
            )
            parts.append(header)
            subj = str(m.get("subject") or "")
            if subj:
                parts.append("Subject: " + subj)
            parts.append(str(m.get("content") or ""))
            if i < len(ordered) - 1:
                parts.append("---")
        return "\n".join(parts)

    def _readable_user_record(self, msgs: list) -> str:
        ordered = sorted(msgs, key=lambda m: str(m.get("timestamp") or ""))
        chunks = []
        for m in ordered:
            subj = str(m.get("subject") or "")
            content = str(m.get("content") or "")
            if subj:
                chunks.append(subj + "\n" + content)
            else:
                chunks.append(content)
        return "\n\n---\n\n".join(chunks)

    # ── persistence ────────────────────────────────────────────

    def _append_chat_records(
        self,
        *,
        ids: list,
        merged_reply_to: str | None,
        user_text: str,
        assistant_text: str,
        session_id: str | None,
        cost_usd,
        duration_ms,
        is_error: bool,
    ) -> None:
        ts = _now_iso()
        user_rec = {
            "role": "user",
            "ts": ts,
            "content": user_text,
            "source_ids": list(ids),
            "merged_reply_to": merged_reply_to,
        }
        asst_rec = {
            "role": "assistant",
            "ts": _now_iso(),
            "content": assistant_text,
            "source_ids": list(ids),
            "merged_reply_to": merged_reply_to,
            "session_id": session_id,
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
            "is_error": is_error,
        }

        def _rw(items):
            if not isinstance(items, list):
                items = []
            items.append(user_rec)
            items.append(asst_rec)
            return items

        locked_json_rw(_rw, json_file=_chat_history_path(self.name), default=[])
        # keep in-memory copy in sync (used by reconnect/system prompt);
        # never truncated — the system-prompt builder picks its own window.
        self._chat_history.append(user_rec)
        self._chat_history.append(asst_rec)

    def _load_session_id(self) -> str | None:
        return load_session_id(self.name)

    def _save_session_id(self, sid: str) -> None:
        try:
            save_session_id(self.name, sid)
        except OSError as exc:
            log.warning("[%s] could not persist session id: %s", self.name, exc)

    # ── operator-driven clears ────────────────────────────────
    #
    # Operators set a flag inside the agent's `control` dict in
    # agents.json (see CONTROL_FIELD docstring above). `Fleet.reconcile`
    # surfaces the latest agents.json into `self.cfg` every sweep, so we
    # just read from cfg here. After applying each op we strip that
    # specific key from agents.json (and from `self.cfg`) under the
    # registry's file lock. A failed op leaves the flag in place so the
    # next sweep retries — that is critical for clear_session, where a
    # failed reconnect would otherwise leave us with no SDK and no
    # recovery signal.
    def _clear_control_keys(self, keys: list[str]) -> None:
        if not keys:
            return

        def _rw(items):
            if not isinstance(items, list):
                return items
            for a in items:
                if not isinstance(a, dict) or a.get("name") != self.name:
                    continue
                ctl = a.get(CONTROL_FIELD)
                if not isinstance(ctl, dict):
                    continue
                for k in keys:
                    ctl.pop(k, None)
                if not ctl:
                    a.pop(CONTROL_FIELD, None)
            return items

        locked_json_rw(_rw, json_file=AGENTS_FILE, default=[])
        ctl = self.cfg.get(CONTROL_FIELD)
        if isinstance(ctl, dict):
            for k in keys:
                ctl.pop(k, None)
            if not ctl:
                self.cfg.pop(CONTROL_FIELD, None)

    async def _consume_control_flags(self) -> None:
        ctl = self.cfg.get(CONTROL_FIELD)
        if not isinstance(ctl, dict) or not ctl:
            return
        processed: list[str] = []

        if ctl.get(CONTROL_CLEAR_CHAT):
            self._do_clear_chat(processed)

        if ctl.get(CONTROL_CLEAR_SESSION):
            await self._do_clear_session(processed)

        if processed:
            try:
                self._clear_control_keys(processed)
            except Exception as exc:
                log.error(
                    "[%s] failed to strip processed control keys %s: %s",
                    self.name,
                    processed,
                    exc,
                )

    def _do_clear_chat(self, processed: list[str]) -> None:
        """Archive then truncate `chat_history.json` atomically. Always
        emits an audit entry. If the archive step fails, the live file
        is left intact (or restored) and the control flag stays set so
        the next sweep retries — the user's data is never destroyed
        without a successful archive.
        """
        ok_archive, archived_count, archive_err = _archive_chat_history(self.name)
        archive_path_str = str(_chat_archive_path(self.name))
        if not ok_archive:
            log.error(
                "[%s] clear_chat ABORTED — archive failed: %s",
                self.name,
                archive_err,
            )
            surface_error(
                "internal_agent_chat",
                "clear_chat archive failed: " + str(archive_err),
                context="clear_chat:" + self.name,
            )
            _append_audit_entry(
                self.name,
                {
                    "action": CONTROL_CLEAR_CHAT,
                    "ts": _now_iso(),
                    "ok": False,
                    "error": archive_err,
                    "archived_count": 0,
                    "archive_path": archive_path_str,
                },
            )
            # Flag stays set in agents.json so the next sweep retries.
            return

        # Archive succeeded — chat_history.json is already truncated as
        # part of the atomic swap inside _archive_chat_history. Sync the
        # in-memory copy so the next turn rebuilds the system prompt
        # without the cleared history.
        self._chat_history = []
        log.info(
            "[%s] cleared chat history (operator request); archived %d records",
            self.name,
            archived_count,
        )
        _append_audit_entry(
            self.name,
            {
                "action": CONTROL_CLEAR_CHAT,
                "ts": _now_iso(),
                "ok": True,
                "archived_count": archived_count,
                "archive_path": archive_path_str,
            },
        )
        processed.append(CONTROL_CLEAR_CHAT)

    async def _do_clear_session(self, processed: list[str]) -> None:
        """Drop the SDK resume id, disconnect, and reconnect. Always emits
        an audit entry capturing the prior session id (so an operator can
        still grep SDK logs for it) and the new id observed at audit-time.

        Note on `new_session_id` in the audit entry: a fresh session id
        is only known after the SDK emits its first ResultMessage, which
        happens on the agent's first turn — not at connect time. So the
        success-path audit entry typically records `new_session_id: None`
        and the id appears in `chat_history.json` for the next turn. The
        prior id remains the meaningful field for cross-referencing SDK
        logs after a clear.
        """
        prior_session_id = self._session_id
        try:
            try:
                _session_path(self.name).unlink()
            except FileNotFoundError:
                pass
            self._session_id = None
            if self._sdk is not None:
                try:
                    await self._sdk.disconnect()
                except Exception as exc:
                    log.warning(
                        "[%s] disconnect during clear_session failed: %s",
                        self.name,
                        exc,
                    )
                self._sdk = None
            await self._connect_sdk()
            log.info(
                "[%s] cleared SDK session and reconnected (operator request)",
                self.name,
            )
            _append_audit_entry(
                self.name,
                {
                    "action": CONTROL_CLEAR_SESSION,
                    "ts": _now_iso(),
                    "ok": True,
                    "prior_session_id": prior_session_id,
                    "new_session_id": self._session_id,
                },
            )
            processed.append(CONTROL_CLEAR_SESSION)
        except Exception as exc:
            log.error("[%s] clear_session failed: %s", self.name, exc, exc_info=True)
            surface_error(
                "internal_agent_chat",
                exc,
                context="clear_session:" + self.name,
            )
            _append_audit_entry(
                self.name,
                {
                    "action": CONTROL_CLEAR_SESSION,
                    "ts": _now_iso(),
                    "ok": False,
                    "error": str(exc),
                    "prior_session_id": prior_session_id,
                    "new_session_id": self._session_id,
                },
            )
            # Flag stays set in agents.json so the next sweep retries.


# ---------------------------------------------------------------------------
# Fleet manager
# ---------------------------------------------------------------------------


class Fleet:
    """Owns the live `InternalAgentSession` instances and reconciles them
    with the current contents of `agents.json` on each sweep tick.
    """

    def __init__(self):
        self._sessions: dict = {}

    def reconcile(self) -> None:
        agents = _load_agents()
        wanted = _internal_agents(agents)

        # Stop sessions that vanished or got deactivated
        for name in list(self._sessions.keys()):
            if name not in wanted:
                log.info("Stopping session for %s (removed/deactivated)", name)
                try:
                    self._sessions[name].stop()
                except Exception as exc:
                    log.warning("Stop %s failed: %s", name, exc)
                self._sessions.pop(name, None)

        # Start new sessions
        for name, cfg in wanted.items():
            if name in self._sessions:
                # Refresh cfg in-place so routing rule edits take effect.
                self._sessions[name].cfg = cfg
                continue
            log.info("Starting session for %s", name)
            try:
                sess = InternalAgentSession(name, cfg)
                sess.start()
                self._sessions[name] = sess
            except Exception as exc:
                log.error("Failed to start %s: %s", name, exc, exc_info=True)
                surface_error("internal_agent_chat", exc, context="start:" + name)

    def sweep_inboxes(self) -> None:
        for name, sess in self._sessions.items():
            try:
                ctl = sess.cfg.get(CONTROL_FIELD)
                if isinstance(ctl, dict) and (
                    ctl.get(CONTROL_CLEAR_CHAT) or ctl.get(CONTROL_CLEAR_SESSION)
                ):
                    sess.notify_inbox()
                    continue
                inbox = _inbox_path(name)
                if not inbox.exists():
                    continue
                items = read_json_file(inbox, default=[])
                if isinstance(items, list) and items:
                    sess.notify_inbox()
            except Exception as exc:
                log.warning("[%s] sweep error: %s", name, exc)

    def shutdown(self) -> None:
        """Graceful shutdown.

        1. Mark every internal agent as `status: "offline"` in agents.json
           BEFORE tearing down sessions, so any concurrent reader (the
           main agent's `[AGENTS]` panel, register-internal-agent --list,
           etc.) sees the right state immediately. The deactivated state
           is preserved so operators can still see explicit deactivation.
        2. Stop each session. Each session has been persisting its SDK
           `session_id` after every turn (see _save_session_id), so the
           saved state on disk is enough to resume the conversation
           verbatim on the next daemon start.
        """
        names = list(self._sessions.keys())
        if names:
            mark_internal_agents_offline(names)
        for name in names:
            try:
                self._sessions[name].stop()
            except Exception as exc:
                log.warning("Shutdown %s failed: %s", name, exc)
        self._sessions.clear()


def mark_internal_agents_offline(names: list[str]) -> None:
    """Flip every named internal agent in agents.json to status='offline'.

    Used by the daemon's graceful shutdown. Agents already in
    `deactivated` state are left alone so an operator's deactivation
    decision is not silently overwritten on restart.
    """
    if not names:
        return
    target = set(names)

    def _rw(items):
        if not isinstance(items, list):
            return items
        for a in items:
            if not isinstance(a, dict):
                continue
            if a.get("type") != "internal":
                continue
            if a.get("name") not in target:
                continue
            if a.get("status") == "deactivated":
                continue
            a["status"] = "offline"
        return items

    locked_json_rw(_rw, json_file=AGENTS_FILE, default=[])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    INTERNAL_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> int:
    _configure_logging()
    log.info("=" * 60)
    log.info("Internal Agent Chat daemon starting")
    log.info("=" * 60)

    # Move any pre-existing chat files into /agent/memory/chat/<name>/.
    # Idempotent (sentinel file inside CHAT_DIR), so cheap to call on every
    # start. Without this, an upgrade from the legacy layout would leave
    # the daemon reading empty new files while the real history sits at
    # the old paths.
    try:
        report = migrate_chat_layout()
        if not report.get("skipped"):
            log.info("chat-layout migration: %s", report)
    except Exception as exc:
        log.error("chat-layout migration failed: %s", exc, exc_info=True)
        surface_error("internal_agent_chat", exc, context="migrate_chat_layout")

    fleet = Fleet()
    stop_requested = {"v": False}

    def _handle_shutdown(signum, frame):
        log.info("Received signal %s, shutting down...", signum)
        stop_requested["v"] = True

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    try:
        while not stop_requested["v"]:
            try:
                fleet.reconcile()
                fleet.sweep_inboxes()
            except Exception as exc:
                log.error("Sweep error: %s", exc, exc_info=True)
                surface_error("internal_agent_chat", exc, context="sweep")
            try:
                HEARTBEAT_FILE.write_text(str(time.time()))
            except OSError:
                pass
            # Sleep in 1s slices so SIGTERM lands quickly.
            for _ in range(SWEEP_SECONDS):
                if stop_requested["v"]:
                    break
                time.sleep(1)
    finally:
        log.info("Shutting down sessions...")
        fleet.shutdown()
        log.info("Internal Agent Chat daemon stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
