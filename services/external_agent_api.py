#!/usr/bin/env python3
"""
External Agent API Service
==========================
HTTP service that lets remote/external agents talk to the main agent via a
file-backed inbox/outbox per agent.

- Listens on port 8083
- Caddy routes /external-agent/* to this service (prefix is stripped, basic
  auth applied at the gateway)
- Per-agent files live at /agent/messages/external/<name>/{inbox,outbox}.json
- Registry lives at /agent/memory/agents.json

Routes (agent identified via X-Agent-Name header):
  POST /read-inbox    -> body: {"ids": ["<id1>","<id2>",...]}. Returns the
                         matching messages from the agent's inbox and flips
                         each to read=true atomically. Every requested id
                         must exist in the inbox AND still be unread, else
                         400 (no partial reads). Updates last_ping_at.
  POST /write-outbox  -> appends a message (or list) to the agent's outbox.
                         Each entry MUST be:
                           {"type": <response|needs_human|error|info>,
                            "subject": <non-empty str>,
                            "content": <non-empty str>,
                            "timestamp": <ISO8601 UTC, optional>}
                         Server stamps id (uuid4) and fills timestamp if
                         missing. Updates last_ping_at.
  GET  /ping          -> updates last_ping_at + status="online"; returns
                         {unread} (and unread_ids when unread > 0).
                         If the agent is currently "deactivated", returns
                         only a "warning" message telling the external agent
                         to halt its loop (no last_ping_at update in that case).
  POST /upload        -> upload a file artifact to the main agent workspace.
                         Requires X-Agent-Name (like the other routes) plus
                         X-Filename: <basename> (must match
                         [A-Za-z0-9][A-Za-z0-9._-]+, no path separators).
                         Body: raw file bytes (binary OK), max 25 MB else 413.
                         Saved to
                         /agent/workspace/upload/<agent-name>/<YYYYMMDD_HHMMSS>_<X-Filename>.
                         Same-second name collisions get a -2/-3/... suffix.
                         Returns {"path": <absolute path>, "size": N}.
                         Updates last_ping_at.
  POST /update        -> patch the agent's entry in agents.json. Body is a JSON
                         object; only these fields may be set:
                           status          one of "online"|"offline"|"deactivated"
                           capabilities    list of capability objects
                           responsibilities free-text string
                         Setting status="deactivated" stops the sweeper from
                         forwarding this agent's outbox; the agent can later
                         POST /update with status="online" to reactivate.
  GET  /health        -> liveness probe (no agent header required).

Background sweeper:
  Every SWEEP_SECONDS (10s, hardcoded):
    - Lazily flips agents to status=offline if last_ping_at is older than
      their per-agent timeout_seconds (default 1800s).
    - Forwards each non-deactivated agent's outbox into the main inbox with
      source="external_agent" + reply_to. The forwarded type is prefixed
      with "agent_" (e.g. response → agent_response, needs_human →
      agent_needs_human) so the main agent can tell forwarded entries apart
      from native inbox types. Entries whose original type is "needs_human"
      are ALSO mirrored into /agent/messages/outbox.json (kept as type
      "needs_human" so existing Telegram / WhatsApp / etc. channels pick
      them up). Forwarded entries are then cleared and archived to
      messages/external/<name>/outbox_history.json.
    - Archives read inbox entries whose read_at is older than 10 minutes
      from messages/external/<name>/inbox.json into inbox_history.json
      (gives the external agent a small replay window after acknowledging
      a message before it is moved out of the live file). Runs for every
      registered external agent, including deactivated ones.

Setup:
  uv run python scripts/service_manager.py start external_agent_api 8083 \
      -- uv run python services/external_agent_api.py
"""

from __future__ import annotations

import json
import logging
import re
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from shared import (
    append_to_history,
    locked_json_rw,
    read_json_file,
    surface_error,
    write_to_inbox,
    write_to_outbox,
)

# --- Paths ---
BASE = Path("/agent")
AGENTS_FILE = BASE / "memory" / "agents.json"
EXTERNAL_DIR = BASE / "messages" / "external"
LOG_DIR = BASE / "memory" / "logs"
LOG_FILE = LOG_DIR / "external_agent_api.log"
HEARTBEAT_DIR = BASE / "memory" / "heartbeats"
HEARTBEAT_FILE = HEARTBEAT_DIR / "external_agent_api.heartbeat"
WORKSPACE_UPLOAD_DIR = BASE / "workspace" / "upload"

# --- Config ---
PORT = 8083
MAX_BODY_SIZE = 1_048_576  # 1 MB — applies to JSON routes
MAX_UPLOAD_SIZE = 25 * 1_048_576  # 25 MB — applies to /upload only
DEFAULT_TIMEOUT_SECONDS = 1800
SWEEP_SECONDS = 10
# Filenames may only contain these chars; rejects path separators, control
# bytes, and ".." traversal attempts. The external agent should send a clean
# basename (e.g. "report.pdf"), not a path.
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
# A read inbox message stays in the live inbox for this long after the
# external agent acknowledges it (so the agent has a window to re-fetch the
# content if it crashed mid-processing) before it is archived.
INBOX_ARCHIVE_AFTER_SECONDS = 900  # 15 minutes

log = logging.getLogger("external_agent_api")


_README = """\
External Agent API — spec.

Auth: HTTP Basic at the gateway. All routes except GET /health require
header `X-Agent-Name: <name>`. Timestamps are ISO8601 UTC.
Field type defaults to string; non-string types are annotated as
(int) / (bool) / (array) / (object) / (array of <type>).

GET /health
  -> 200 {"status":"ok"}

GET /ping
  -> 200 {"unread" (int),
          "unread_ids" (array of str, only when unread > 0)}
  -> 200 {"status":"deactivated",
          "unread" (int),
          "unread_ids" (array of str),
          "warning"}  (when agent is deactivated)

POST /read-inbox
  body: {"ids" (array of str): non-empty, unique, all currently unread}
  -> 200 {"status":"ok",
          "messages" (array of object),
          "count" (int)}
  -> 400 {"detail",
          "unknown_ids" (array of str),
          "already_read_ids" (array of str)}

POST /write-outbox
  body: object OR non-empty (array of object), each object:
    {"type": "response"|"needs_human"|"error"|"info",
     "subject":   non-empty,
     "content":   non-empty,
     "timestamp": ISO8601 UTC (optional),
     "reply_to_id": str (optional) — id of the original inbox message this
                    reply addresses; the sweeper copies it onto the
                    forwarded main-inbox entry as `reply_to_id` so the
                    main agent can correlate the reply to a delegated goal}
  -> 200 {"status":"ok",
          "appended" (int)}

POST /upload
  headers: X-Filename: <basename matching [A-Za-z0-9][A-Za-z0-9._-]{0,254}>
  body: raw bytes, max 25 MB
  -> 200 {"status":"ok",
          "path",
          "size" (int)}

POST /update
  body: object with any subset of:
    {"status":           "online"|"offline"|"deactivated",
     "capabilities"      (array of object: {id, name, description, category,
                                            enabled (bool)}),
     "responsibilities"}
  Only route allowed while status=="deactivated".
  -> 200 {"status":"ok",
          "agent" (object: {name, status, capabilities (array), responsibilities})}

Errors (JSON, include this readme on 4xx):
  400 bad input | 403 deactivated | 404 unknown agent or route |
  413 body too large (JSON: 1 MB; /upload: 25 MB)
"""


class _BodyTooLarge(Exception):
    def __init__(self, length: int):
        super().__init__(f"body too large: {length} > {MAX_BODY_SIZE}")
        self.length = length


def _configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    EXTERNAL_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_agents() -> list:
    return read_json_file(AGENTS_FILE, default=[])


def _find_agent(agents: list, name: str) -> dict | None:
    for a in agents:
        if isinstance(a, dict) and a.get("name") == name:
            return a
    return None


def _agent_inbox(name: str) -> Path:
    return EXTERNAL_DIR / name / "inbox.json"


def _agent_outbox(name: str) -> Path:
    return EXTERNAL_DIR / name / "outbox.json"


def _is_offline(agent: dict, *, now: datetime | None = None) -> bool:
    """True if agent's last_ping_at is older than its timeout_seconds."""
    timeout = int(agent.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    last = agent.get("last_ping_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True
    now = now or datetime.now(timezone.utc)
    return (now - last_dt) > timedelta(seconds=timeout)


def _update_agent(name: str, updater) -> dict | None:
    """Apply *updater(agent_dict)* to the named agent under lock.

    Returns the updated agent (post-mutation) or None if not found.
    """
    result: dict[str, dict | None] = {"agent": None}

    def _rw(agents):
        if not isinstance(agents, list):
            return agents
        for a in agents:
            if isinstance(a, dict) and a.get("name") == name:
                updater(a)
                result["agent"] = a
                break
        return agents

    locked_json_rw(_rw, json_file=AGENTS_FILE, default=[])
    return result["agent"]


def _touch_ping(name: str) -> None:
    def _do(a):
        a["last_ping_at"] = _now_iso()
        if a.get("status") != "deactivated":
            a["status"] = "online"

    _update_agent(name, _do)


# ---------------------------------------------------------------------------
# Inbox / outbox operations
# ---------------------------------------------------------------------------


class _ReadInboxError(ValueError):
    """Raised when a /read-inbox request specifies invalid ids."""

    def __init__(
        self,
        detail: str,
        *,
        unknown_ids: list | None = None,
        already_read_ids: list | None = None,
    ):
        super().__init__(detail)
        self.detail = detail
        self.unknown_ids = unknown_ids or []
        self.already_read_ids = already_read_ids or []


def _read_inbox_by_ids(name: str, ids: list) -> list:
    """Return inbox entries matching *ids* (in request order) and flip them
    to read=true atomically. Every id in *ids* must exist in the inbox and
    still be unread, else _ReadInboxError is raised and the file is not
    mutated.
    """
    if not isinstance(ids, list) or not ids:
        raise _ReadInboxError("'ids' must be a non-empty list of strings")
    if not all(isinstance(x, str) and x for x in ids):
        raise _ReadInboxError("'ids' must contain only non-empty strings")
    if len(set(ids)) != len(ids):
        raise _ReadInboxError("'ids' contains duplicates")

    target = _agent_inbox(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    requested = set(ids)
    selected: dict[str, dict] = {}
    unknown: list[str] = []
    already_read: list[str] = []

    def _rw(messages):
        if not isinstance(messages, list):
            messages = []
        by_id: dict[str, dict] = {}
        for m in messages:
            if isinstance(m, dict) and m.get("id") in requested:
                by_id[m["id"]] = m

        for mid in ids:
            m = by_id.get(mid)
            if m is None:
                unknown.append(mid)
            elif m.get("read", False):
                already_read.append(mid)

        if unknown or already_read:
            return messages  # abort — no mutation

        now = _now_iso()
        for mid in ids:
            m = by_id[mid]
            selected[mid] = dict(m)  # snapshot before flipping
            m["read"] = True
            m["read_at"] = now
        return messages

    locked_json_rw(_rw, json_file=target, default=[])

    if unknown or already_read:
        parts = []
        if unknown:
            parts.append(f"unknown_ids={unknown}")
        if already_read:
            parts.append(f"already_read_ids={already_read}")
        raise _ReadInboxError(
            "one or more ids could not be read: " + "; ".join(parts),
            unknown_ids=unknown,
            already_read_ids=already_read,
        )
    return [selected[i] for i in ids]


def _unread_ids(name: str) -> list[str]:
    target = _agent_inbox(name)
    if not target.exists():
        return []
    messages = read_json_file(target, default=[])
    return [
        m.get("id")
        for m in messages
        if isinstance(m, dict) and not m.get("read", False) and m.get("id")
    ]


_ALLOWED_UPDATE_FIELDS = ("status", "capabilities", "responsibilities")
_ALLOWED_STATUS_VALUES = ("online", "offline", "deactivated")


def _validate_update(patch: dict) -> dict:
    """Whitelist + type-check fields an external agent may set on its registry entry."""
    cleaned: dict = {}
    unknown = [k for k in patch if k not in _ALLOWED_UPDATE_FIELDS]
    if unknown:
        raise ValueError(
            f"unsupported field(s): {unknown}; allowed: {list(_ALLOWED_UPDATE_FIELDS)}"
        )
    if "status" in patch:
        s = patch["status"]
        if s not in _ALLOWED_STATUS_VALUES:
            raise ValueError(
                f"invalid status {s!r}; must be one of {list(_ALLOWED_STATUS_VALUES)}"
            )
        cleaned["status"] = s
    if "responsibilities" in patch:
        r = patch["responsibilities"]
        if not isinstance(r, str):
            raise ValueError("responsibilities must be a string")
        cleaned["responsibilities"] = r
    if "capabilities" in patch:
        c = patch["capabilities"]
        if not isinstance(c, list) or not all(isinstance(x, dict) for x in c):
            raise ValueError("capabilities must be a list of objects")
        cleaned["capabilities"] = c
    return cleaned


def _apply_update(name: str, patch: dict) -> dict:
    """Apply a whitelisted patch to the named agent under lock. Returns updated dict."""
    cleaned = _validate_update(patch)
    if not cleaned:
        raise ValueError("no fields to update")

    def _do(a):
        a.update(cleaned)
        if cleaned.get("status") == "deactivated":
            a["deactivated_at"] = _now_iso()

    updated = _update_agent(name, _do)
    if updated is None:
        raise ValueError(f"unknown agent: {name}")
    return updated


_ALLOWED_OUTBOX_TYPES = ("response", "needs_human", "error", "info")

# Priority assigned on the forwarded inbox entry, derived from the external
# agent's outbox type. Lower number = higher priority. Priority 1 is reserved
# for native goals from the user / scheduler.
_FORWARD_PRIORITY = {
    "needs_human": 2,
    "error": 3,
    "response": 4,
    "info": 5,
}
_FORWARD_PRIORITY_DEFAULT = 5


def _save_upload(agent_name: str, filename: str, body: bytes) -> Path:
    """Save *body* to /agent/workspace/upload/<agent_name>/<YYYYMMDD_HHMMSS>_<filename>.

    All files for one agent live flat under that agent's directory, with the
    UTC upload timestamp prefixed onto the filename. Raises ValueError if
    the filename fails the safe-basename check (no path separators, no '..',
    no control bytes). If the same name lands twice in the same second, a
    `-2`, `-3`, … suffix is appended to keep both files.
    """
    if not filename:
        raise ValueError("missing X-Filename header")
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise ValueError("filename must be a basename, not a path")
    if not _SAFE_FILENAME_RE.match(filename):
        raise ValueError(
            "filename must match [A-Za-z0-9][A-Za-z0-9._-]{0,254} "
            "(start with alphanumeric, then alphanumeric/dot/underscore/hyphen)"
        )

    target_dir = WORKSPACE_UPLOAD_DIR / agent_name
    target_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stamped_name = f"{ts}_{filename}"
    target = target_dir / stamped_name
    if target.exists():
        stem = target.stem
        suffix = target.suffix
        n = 2
        while True:
            candidate = target_dir / f"{stem}-{n}{suffix}"
            if not candidate.exists():
                target = candidate
                break
            n += 1

    target.write_bytes(body)
    return target


def _append_outbox(name: str, payload) -> int:
    """Append one or more messages to the agent's outbox. Returns count appended.

    Each entry must conform to the shared outbox schema:
      {"type": <response|needs_human|error|info>,
       "subject": <str>,
       "content": <str>,
       "timestamp": <ISO8601 UTC, optional — server fills in if absent>,
       "reply_to_id": <str, optional — id of the original inbox message this
                       reply addresses; preserved through the sweeper onto the
                       forwarded main-inbox entry so the main agent can
                       correlate the reply to a delegated goal>}
    """
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or not payload:
        raise ValueError("body must be a JSON object or non-empty list of objects")

    now = _now_iso()
    stamped: list[dict] = []
    for idx, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"entry [{idx}] must be a JSON object")
        t = item.get("type")
        if t not in _ALLOWED_OUTBOX_TYPES:
            raise ValueError(
                f"entry [{idx}] 'type' must be one of "
                f"{list(_ALLOWED_OUTBOX_TYPES)}; got {t!r}"
            )
        subject = item.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError(f"entry [{idx}] 'subject' must be a non-empty string")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"entry [{idx}] 'content' must be a non-empty string")
        ts = item.get("timestamp", now)
        if not isinstance(ts, str):
            raise ValueError(f"entry [{idx}] 'timestamp' must be an ISO8601 string")

        reply_to_id = item.get("reply_to_id")
        if reply_to_id is not None:
            if not isinstance(reply_to_id, str) or not reply_to_id.strip():
                raise ValueError(
                    f"entry [{idx}] 'reply_to_id' must be a non-empty string when provided"
                )
            reply_to_id = reply_to_id.strip()

        entry = {
            "id": item.get("id") or str(uuid.uuid4()),
            "type": t,
            "subject": subject,
            "content": content,
            "timestamp": ts,
        }
        if reply_to_id:
            entry["reply_to_id"] = reply_to_id
        stamped.append(entry)

    target = _agent_outbox(name)
    target.parent.mkdir(parents=True, exist_ok=True)

    def _rw(existing):
        if not isinstance(existing, list):
            existing = []
        existing.extend(stamped)
        return existing

    locked_json_rw(_rw, json_file=target, default=[])
    return len(stamped)


# ---------------------------------------------------------------------------
# Sweeper — drain external outboxes into the main inbox
# ---------------------------------------------------------------------------


def _agent_outbox_history(name: str) -> Path:
    return EXTERNAL_DIR / name / "outbox_history.json"


def _agent_inbox_history(name: str) -> Path:
    return EXTERNAL_DIR / name / "inbox_history.json"


def _archive_old_read_inbox(name: str, *, now: datetime) -> int:
    """Move read inbox entries whose read_at is older than the archive window
    out of the live inbox into the per-agent inbox_history.json. Returns the
    number of entries archived.
    """
    target = _agent_inbox(name)
    if not target.exists():
        return 0
    threshold = now - timedelta(seconds=INBOX_ARCHIVE_AFTER_SECONDS)
    archived: list[dict] = []

    def _rw(messages):
        if not isinstance(messages, list):
            return []
        kept: list = []
        for m in messages:
            if not isinstance(m, dict):
                kept.append(m)
                continue
            if not m.get("read") or not m.get("read_at"):
                kept.append(m)
                continue
            try:
                read_dt = datetime.fromisoformat(m["read_at"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                kept.append(m)
                continue
            if read_dt <= threshold:
                archived.append(m)
            else:
                kept.append(m)
        return kept

    if not locked_json_rw(_rw, json_file=target, default=[]):
        return 0
    if archived:
        try:
            append_to_history(archived, _agent_inbox_history(name))
        except Exception as e:
            log.warning(f"Failed to archive inbox history for {name}: {e}")
    return len(archived)


def _sweep_once() -> None:
    agents = _load_agents()
    if not agents:
        return

    now = datetime.now(timezone.utc)

    # Lazy offline detection — re-evaluate under lock to avoid TOCTOU on agents.json
    needs_flip = any(
        isinstance(a, dict)
        and a.get("type") == "external"
        and a.get("status") not in ("deactivated", "offline")
        and _is_offline(a, now=now)
        for a in agents
    )
    if needs_flip:

        def _flip(items):
            if not isinstance(items, list):
                return items
            for it in items:
                if (
                    isinstance(it, dict)
                    and it.get("type") == "external"
                    and it.get("status") not in ("deactivated", "offline")
                    and _is_offline(it, now=now)
                ):
                    it["status"] = "offline"
            return items

        locked_json_rw(_flip, json_file=AGENTS_FILE, default=[])

    # Forward outboxes — peek → forward → clear → archive.
    # Crash semantics:
    #   * crash between forward and clear   → re-forward those entries once on
    #     next sweep (acceptable: at-most-once duplicate per crash).
    #   * crash between clear and archive   → entries gone from outbox; the
    #     archive just misses one batch (informational, not load-bearing).
    #   * crash before forward succeeds     → entries still in outbox; next
    #     sweep retries safely.
    for a in agents:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "external":
            continue
        name = a.get("name")
        if not name:
            continue

        # Inbox archive runs for every registered external agent, even
        # deactivated ones — keeping their inbox.json from growing unbounded
        # is hygiene, not delivery, so it shouldn't depend on status.
        archived = _archive_old_read_inbox(name, now=now)
        if archived:
            log.info(
                f"Archived {archived} read inbox entry(s) for agent={name} "
                f"to inbox_history.json"
            )

        # Outbox forwarding only runs for active agents.
        if a.get("status") == "deactivated":
            continue
        outbox = _agent_outbox(name)
        if not outbox.exists():
            continue

        # Step 1: peek — read under the outbox lock without mutating.
        peeked: list[dict] = []

        def _peek(items):
            if isinstance(items, list):
                peeked.extend(it for it in items if isinstance(it, dict))
            return items

        if not locked_json_rw(_peek, json_file=outbox, default=[]):
            continue
        fresh = [d for d in peeked if d.get("id")]
        if not fresh:
            continue

        # Step 2: forward to main inbox.
        forwarded_at = _now_iso()
        inbox_items = []
        for entry in fresh:
            subject = (entry.get("subject") or "").strip()
            body = (entry.get("content") or "").strip()
            content_parts = [f"[from external agent {name}]"]
            if subject:
                content_parts.append(subject)
            if body:
                content_parts.append("")
                content_parts.append(body)
            ext_type = entry.get("type") or "info"
            forwarded = {
                # Prefix the external agent's type with "agent_" so the main
                # agent's inbox triage can distinguish forwarded entries
                # from the base inbox types (goal/message/event) at a glance.
                "type": f"agent_{ext_type}",
                "content": "\n".join(content_parts),
                "received_at": forwarded_at,
                "source": "external_agent",
                "from": f"messages/external/{name}/outbox.json",
                "reply_to": f"messages/external/{name}/inbox.json",
                "priority": _FORWARD_PRIORITY.get(ext_type, _FORWARD_PRIORITY_DEFAULT),
            }
            # Preserve the original delegated-inbox message id so the main
            # agent can correlate this reply to a goal's `delegated_message_id`.
            if isinstance(entry.get("reply_to_id"), str) and entry["reply_to_id"]:
                forwarded["reply_to_id"] = entry["reply_to_id"]
            inbox_items.append(forwarded)
        if not write_to_inbox(inbox_items, dedup=False):
            log.warning(
                f"write_to_inbox failed for agent={name}; leaving outbox intact for retry"
            )
            continue

        # Mirror needs_human entries into the main outbox so the existing
        # human-notification channels (Telegram / WhatsApp / etc.) pick them
        # up. Best-effort: if the outbox write fails, the message is still
        # in the main inbox and will be re-tried by the agent on next cycle.
        needs_human_items = [
            {
                "type": "needs_human",
                "subject": entry.get("subject", ""),
                "content": (f"[from external agent {name}] {entry.get('content', '')}"),
                "timestamp": entry.get("timestamp") or forwarded_at,
            }
            for entry in fresh
            if entry.get("type") == "needs_human"
        ]
        if needs_human_items and not write_to_outbox(needs_human_items):
            log.warning(
                f"write_to_outbox failed for {len(needs_human_items)} needs_human "
                f"item(s) from agent={name}; main inbox already has them"
            )
            surface_error(
                "external_agent_api",
                "write_to_outbox failed for needs_human mirror",
                context=f"agent={name}",
            )

        forwarded_ids = {d["id"] for d in fresh}
        # Step 3: clear forwarded entries from the outbox (preserves any items
        # appended between peek and now).
        _clear_outbox_ids(outbox, forwarded_ids)
        # Step 4: archive the forwarded entries to the per-agent history file.
        archive_entries = [{**d, "forwarded_at": forwarded_at} for d in fresh]
        try:
            append_to_history(archive_entries, _agent_outbox_history(name))
        except Exception as e:  # best-effort; do not retry the forward
            log.warning(f"Failed to archive outbox history for {name}: {e}")
        log.info(f"Forwarded {len(fresh)} message(s) from agent={name} into main inbox")


def _clear_outbox_ids(outbox: Path, ids_to_remove: set) -> None:
    if not ids_to_remove:
        return

    def _rw(items):
        if not isinstance(items, list):
            return []
        return [
            it
            for it in items
            if not (isinstance(it, dict) and it.get("id") in ids_to_remove)
        ]

    locked_json_rw(_rw, json_file=outbox, default=[])


def _sweeper_loop(stop_event: threading.Event) -> None:
    log.info(f"Sweeper thread started (interval={SWEEP_SECONDS}s)")
    while not stop_event.is_set():
        try:
            _sweep_once()
        except Exception as e:
            log.error(f"Sweep error: {e}", exc_info=True)
            surface_error("external_agent_api", e, context="sweep")
        try:
            HEARTBEAT_FILE.write_text(str(time.time()))
        except OSError:
            pass
        stop_event.wait(SWEEP_SECONDS)
    log.info("Sweeper thread exiting")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log.debug(f"HTTP: {format % args}")

    def _send_json(self, status: int, data: dict):
        # Attach the README to every 4xx so the caller can self-correct.
        if 400 <= status < 500 and "readme" not in data:
            data = {**data, "readme": _README}
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (ValueError, TypeError):
            length = 0
        if length <= 0:
            return None
        if length > MAX_BODY_SIZE:
            raise _BodyTooLarge(length)
        raw = self.rfile.read(length)
        text = raw.decode("utf-8", errors="replace")
        if not text.strip():
            return None
        return json.loads(text)

    def _read_raw_body(self, max_size: int) -> bytes | None:
        """Read the raw request body up to *max_size* bytes (no JSON parse)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (ValueError, TypeError):
            length = 0
        if length <= 0:
            return None
        if length > max_size:
            raise _BodyTooLarge(length)
        return self.rfile.read(length)

    def _resolve_agent(self, *, allow_deactivated: bool = False):
        """Return (agent_dict, None) on success, or (None, (status, body))."""
        name = self.headers.get("X-Agent-Name") or ""
        name = name.strip()
        if not name:
            return None, (400, {"status": "error", "detail": "missing X-Agent-Name"})
        agents = _load_agents()
        agent = _find_agent(agents, name)
        if agent is None:
            return None, (404, {"status": "error", "detail": f"unknown agent: {name}"})
        if not allow_deactivated and agent.get("status") == "deactivated":
            return None, (403, {"status": "error", "detail": "agent is deactivated"})
        return agent, None

    def _route(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        method = self.command

        if path == "/health" and method in ("GET", "HEAD"):
            return self._send_json(200, {"status": "ok"})

        # /update is the only way to bring an agent back from "deactivated",
        # so it is allowed to act on a deactivated agent. /ping is also
        # allowed through so the external agent gets a clear "stop looping"
        # signal instead of a bare 403 it has to interpret.
        allow_deactivated = (path == "/update" and method == "POST") or (
            path == "/ping" and method == "GET"
        )
        agent, err = self._resolve_agent(allow_deactivated=allow_deactivated)
        if err:
            status, body = err
            return self._send_json(status, body)
        name = agent["name"]

        if path == "/read-inbox" and method == "POST":
            try:
                payload = self._read_body()
            except _BodyTooLarge as e:
                return self._send_json(413, {"status": "error", "detail": str(e)})
            except json.JSONDecodeError as e:
                return self._send_json(
                    400, {"status": "error", "detail": f"invalid JSON: {e}"}
                )
            if not isinstance(payload, dict):
                return self._send_json(
                    400, {"status": "error", "detail": "body must be a JSON object"}
                )
            ids = payload.get("ids")
            try:
                messages = _read_inbox_by_ids(name, ids)
            except _ReadInboxError as e:
                return self._send_json(
                    400,
                    {
                        "status": "error",
                        "detail": e.detail,
                        "unknown_ids": e.unknown_ids,
                        "already_read_ids": e.already_read_ids,
                    },
                )
            _touch_ping(name)
            return self._send_json(
                200, {"status": "ok", "messages": messages, "count": len(messages)}
            )

        if path == "/write-outbox" and method == "POST":
            try:
                payload = self._read_body()
            except _BodyTooLarge as e:
                return self._send_json(413, {"status": "error", "detail": str(e)})
            except json.JSONDecodeError as e:
                return self._send_json(
                    400, {"status": "error", "detail": f"invalid JSON: {e}"}
                )
            if payload is None:
                return self._send_json(400, {"status": "error", "detail": "empty body"})
            try:
                count = _append_outbox(name, payload)
            except ValueError as e:
                return self._send_json(400, {"status": "error", "detail": str(e)})
            _touch_ping(name)
            return self._send_json(200, {"status": "ok", "appended": count})

        if path == "/upload" and method == "POST":
            filename = (self.headers.get("X-Filename") or "").strip()
            try:
                body = self._read_raw_body(MAX_UPLOAD_SIZE)
            except _BodyTooLarge as e:
                return self._send_json(413, {"status": "error", "detail": str(e)})
            if not body:
                return self._send_json(400, {"status": "error", "detail": "empty body"})
            try:
                target = _save_upload(name, filename, body)
            except ValueError as e:
                return self._send_json(400, {"status": "error", "detail": str(e)})
            except OSError as e:
                log.error(f"Upload save failed for agent={name}: {e}", exc_info=True)
                surface_error("external_agent_api", e, context=f"upload agent={name}")
                return self._send_json(500, {"status": "error"})
            _touch_ping(name)
            log.info(f"agent={name} uploaded {len(body)} bytes -> {target}")
            return self._send_json(
                200,
                {"status": "ok", "path": str(target), "size": len(body)},
            )

        if path == "/ping" and method == "GET":
            ids = _unread_ids(name)
            if agent.get("status") == "deactivated":
                # Surface a clear stop-looping signal instead of bouncing
                # the deactivated agent off a bare 403 (which /ping is
                # explicitly allowed through to deliver). Include any
                # pending unread ids so the agent can drain them before
                # halting its loop.
                return self._send_json(
                    200,
                    {
                        "status": "deactivated",
                        "unread_ids": ids,
                        "unread": len(ids),
                        "warning": (
                            "Deactivated. Before stopping your /loop, you "
                            "must read all unread messages (POST /read-inbox) "
                            "and finish pending tasks (POST /write-outbox). "
                            'May reactivate via POST /update {"status":"online"}.'
                        ),
                    },
                )
            _touch_ping(name)
            body: dict = {"unread": len(ids)}
            if ids:
                body["unread_ids"] = ids
            return self._send_json(200, body)

        if path == "/update" and method == "POST":
            try:
                payload = self._read_body()
            except _BodyTooLarge as e:
                return self._send_json(413, {"status": "error", "detail": str(e)})
            except json.JSONDecodeError as e:
                return self._send_json(
                    400, {"status": "error", "detail": f"invalid JSON: {e}"}
                )
            if not isinstance(payload, dict):
                return self._send_json(
                    400, {"status": "error", "detail": "body must be a JSON object"}
                )
            try:
                updated = _apply_update(name, payload)
            except ValueError as e:
                return self._send_json(400, {"status": "error", "detail": str(e)})
            if updated.get("status") != "deactivated":
                _touch_ping(name)
            return self._send_json(
                200,
                {
                    "status": "ok",
                    "agent": {
                        "name": updated.get("name"),
                        "status": updated.get("status"),
                        "responsibilities": updated.get("responsibilities"),
                        "capabilities": updated.get("capabilities"),
                    },
                },
            )

        return self._send_json(404, {"status": "error", "detail": "no such route"})

    def _safe_route(self):
        try:
            self._route()
        except Exception as e:
            log.error(f"Error handling {self.command} {self.path}: {e}", exc_info=True)
            surface_error(
                "external_agent_api", e, context=f"{self.command} {self.path}"
            )
            try:
                self._send_json(500, {"status": "error"})
            except Exception:
                pass

    do_GET = _safe_route
    do_HEAD = _safe_route
    do_POST = _safe_route


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    _configure_logging()
    log.info("=" * 60)
    log.info("External Agent API starting up")
    log.info("=" * 60)

    server = HTTPServer(("0.0.0.0", PORT), _Handler)
    serve_thread = threading.Thread(
        target=server.serve_forever, name="external-api-http", daemon=True
    )
    serve_thread.start()
    log.info(f"HTTP server listening on port {PORT}")

    stop_event = threading.Event()
    sweeper = threading.Thread(
        target=_sweeper_loop,
        name="external-api-sweeper",
        args=(stop_event,),
        daemon=True,
    )
    sweeper.start()

    shutdown_requested = False

    def _handle_shutdown(signum, frame):
        nonlocal shutdown_requested
        log.info(f"Received signal {signum}, shutting down...")
        shutdown_requested = True

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    try:
        while not shutdown_requested:
            try:
                HEARTBEAT_FILE.write_text(str(time.time()))
            except OSError:
                pass
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutdown via KeyboardInterrupt")
    finally:
        log.info("Cleaning up...")
        stop_event.set()
        server.shutdown()
        serve_thread.join(timeout=5)
        sweeper.join(timeout=5)
        log.info("External Agent API stopped.")


if __name__ == "__main__":
    main()
