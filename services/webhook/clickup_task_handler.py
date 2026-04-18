#!/usr/bin/env python3
"""ClickUp task-webhook handler for webhook_receiver.

Supports multiple ClickUp workspaces. Each workspace gets its own endpoint:

    https://<public-url>/webhook/clickup/<workspace_id>/task

The workspace_id is extracted from the URL path and used to look up
per-workspace configuration (trigger transitions, review prompt) and the
per-workspace HMAC secret from KeePass.

ClickUp task event reference:
    https://developer.clickup.com/docs/webhooktaskpayloads

Responsibilities:
- POST /clickup/<workspace_id>/task: receive a ClickUp webhook event.
  Only taskStatusUpdated is actionable — when a task's status transitions
  through a configured trigger pair, a review goal is queued in inbox.json.
  All other events are acknowledged (200) and logged but take no action.
- Paths that don't match /clickup/<workspace_id>/task → 404.
- GET/other methods → 405.

Configuration lives in
    /agent/memory/clickup_task_handler_state.json

Schema — keyed by workspace_id (string):
    {
      "90182624126": {
        "trigger_transitions": [
          {"from": "in progress", "to": "in review"},
          {"from": "in progress", "to": "complete"}
        ],
        "review_prompt_path": null
      }
    }

State is re-read on every request, so trigger rules can be tuned without
restarting webhook_receiver.

HMAC-SHA256 signature verification is enabled per workspace when the KeePass
credential CLICKUP_WEBHOOK_SECRET_<workspace_id> is present. If absent,
requests for that workspace are accepted unverified (warning logged at startup
and again on first unverified request).
"""

import hmac
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from shared import read_json_file, write_to_inbox

# --- Paths ---
BASE = Path("/agent")
STATE_FILE = BASE / "memory" / "clickup_task_handler_state.json"

# --- Constants ---
# Prefix registered with webhook_receiver.  Matches any path that equals
# "/clickup" or starts with "/clickup/" — the handle() method validates the
# full /clickup/<workspace_id>/task shape and returns 404 for anything else.
WEBHOOK_PATH_PREFIX = "/clickup"

# KeePass key pattern — replace {workspace_id} before lookup.
KEEPASS_SECRET_KEY_TEMPLATE = "CLICKUP_WEBHOOK_SECRET_{workspace_id}"

MAX_BODY_SIZE = 1_048_576  # 1 MB — ClickUp payloads are typically < 20 KB
# Inbox body cap — mirrors the default webhook_receiver handler (full payload
# already in webhook_receiver.log so the inbox item is kept compact).
_INBOX_BODY_MAX = 4_096  # 4 KB

# Regex that accepts /clickup/<workspace_id>/task (workspace_id: digits only)
_PATH_RE = re.compile(r"^/clickup/(\d+)/task$")

# Default trigger transitions applied when a workspace has no state entry.
DEFAULT_TRIGGER_TRANSITIONS = [
    {"from": "in progress", "to": "in review"},
    {"from": "in progress", "to": "complete"},
]

# Known ClickUp task events — used to label log lines for events we drop.
KNOWN_TASK_EVENTS = {
    "taskCreated",
    "taskUpdated",
    "taskDeleted",
    "taskPriorityUpdated",
    "taskStatusUpdated",
    "taskAssigneeUpdated",
    "taskDueDateUpdated",
    "taskTagUpdated",
    "taskMoved",
    "taskCommentPosted",
    "taskCommentUpdated",
    "taskTimeEstimateUpdated",
    "taskTimeTrackedUpdated",
}

log = logging.getLogger("webhook_receiver.clickup")


# ---------------------------------------------------------------------------
# KeePass helper
# ---------------------------------------------------------------------------


def _keepass_get(title: str) -> str | None:
    try:
        from scripts.keepass import get_credential

        return get_credential(title)
    except Exception as e:
        log.warning(f"KeePass get({title!r}) failed: {e}")
    return None


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _default_workspace_state() -> dict:
    return {
        "trigger_transitions": [
            (t["from"], t["to"]) for t in DEFAULT_TRIGGER_TRANSITIONS
        ],
        "review_prompt_path": None,
    }


def _parse_workspace_state(raw: dict) -> dict:
    """Normalise a single workspace config block from the state file."""
    transitions_raw = raw.get("trigger_transitions")
    if not isinstance(transitions_raw, list) or not transitions_raw:
        transitions_raw = DEFAULT_TRIGGER_TRANSITIONS

    normalised: list[tuple[str, str]] = []
    for t in transitions_raw:
        if not isinstance(t, dict):
            continue
        fr = str(t.get("from", "")).strip().lower()
        to = str(t.get("to", "")).strip().lower()
        if fr and to:
            normalised.append((fr, to))

    prompt_path = raw.get("review_prompt_path")
    if not isinstance(prompt_path, str) or not prompt_path:
        prompt_path = None

    return {
        "trigger_transitions": normalised
        or _default_workspace_state()["trigger_transitions"],
        "review_prompt_path": prompt_path,
    }


def load_workspace_state(workspace_id: str) -> dict:
    """Return parsed state for a specific workspace.

    Re-reads STATE_FILE on every call so operators can retune without
    restarting the receiver. Falls back to defaults for unknown workspaces.
    """
    data = read_json_file(STATE_FILE, default={})
    if not isinstance(data, dict):
        log.warning(f"{STATE_FILE.name}: expected a JSON object, using defaults")
        return _default_workspace_state()

    if workspace_id not in data:
        log.warning(
            f"Workspace {workspace_id!r} has no entry in {STATE_FILE.name} "
            "— using default trigger transitions"
        )
        return _default_workspace_state()

    raw = data[workspace_id]
    if not isinstance(raw, dict):
        log.warning(
            f"Workspace {workspace_id!r} state is not a JSON object, using defaults"
        )
        return _default_workspace_state()

    return _parse_workspace_state(raw)


def list_configured_workspaces() -> list[str]:
    """Return workspace IDs present in the state file."""
    data = read_json_file(STATE_FILE, default={})
    if not isinstance(data, dict):
        return []
    return [k for k in data if isinstance(data[k], dict)]


# ---------------------------------------------------------------------------
# Signature verification
#
# ClickUp signs the webhook body with HMAC-SHA256 using the webhook secret
# and delivers the hex digest in the ``X-Signature`` header (no prefix).
# ---------------------------------------------------------------------------


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    provided = header.strip()
    # Tolerate a "sha256=" prefix in case ClickUp ever changes format.
    if provided.startswith("sha256="):
        provided = provided.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def extract_status_transition(payload: dict) -> tuple[str, str] | None:
    """Return (before_status_lower, after_status_lower) for a status-change
    history item, or None if the payload does not describe a status change.
    """
    for item in payload.get("history_items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("field") != "status":
            continue
        before = item.get("before") or {}
        after = item.get("after") or {}
        before_status = str(before.get("status", "")).strip().lower()
        after_status = str(after.get("status", "")).strip().lower()
        if before_status and after_status:
            return before_status, after_status
    return None


# ---------------------------------------------------------------------------
# Handler class
# ---------------------------------------------------------------------------


class ClickUpTaskHandler:
    """Webhook sub-handler for ClickUp task events (multi-workspace).

    Registered at path_prefix = "/clickup". Handles requests matching:
        /clickup/<workspace_id>/task

    All other paths under /clickup/* return 404.
    """

    path_prefix = WEBHOOK_PATH_PREFIX

    def __init__(self):
        # workspace_id → secret string (or None if not configured)
        # Populated lazily: loaded from KeePass on first request for a
        # new workspace_id so operators can add workspaces without restart.
        self._secrets: dict[str, str | None] = {}
        # Track workspace_ids we've already warned about missing secrets.
        self._warned_no_secret: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        log.info("Starting ClickUp task handler (multi-workspace)...")
        workspaces = list_configured_workspaces()
        if workspaces:
            for ws_id in workspaces:
                self._load_secret(ws_id)
                secret_status = (
                    "signature verification enabled"
                    if self._secrets.get(ws_id)
                    else "NO secret — verification DISABLED"
                )
                state = load_workspace_state(ws_id)
                log.info(
                    f"  Workspace {ws_id}: "
                    f"{len(state['trigger_transitions'])} trigger(s), "
                    f"{secret_status}"
                )
        else:
            log.warning(
                f"No workspaces configured in {STATE_FILE.name}. "
                "Events will be accepted with default triggers. "
                "Add workspace config keyed by workspace_id to tune behaviour."
            )
        log.info(
            f"ClickUp task handler ready — "
            f"path pattern: /webhook/clickup/<workspace_id>/task"
        )

    def shutdown(self):
        log.info("ClickUp task handler stopped.")

    # ------------------------------------------------------------------
    # Secret management (lazy per-workspace KeePass load)
    # ------------------------------------------------------------------

    def _load_secret(self, workspace_id: str) -> str | None:
        """Load (or reload) the KeePass secret for workspace_id."""
        key = KEEPASS_SECRET_KEY_TEMPLATE.format(workspace_id=workspace_id)
        secret = _keepass_get(key)
        self._secrets[workspace_id] = secret
        return secret

    def _get_secret(self, workspace_id: str) -> str | None:
        """Return cached secret, loading from KeePass if not yet seen."""
        if workspace_id not in self._secrets:
            self._load_secret(workspace_id)
            if not self._secrets.get(workspace_id):
                if workspace_id not in self._warned_no_secret:
                    log.warning(
                        f"Workspace {workspace_id}: "
                        f"CLICKUP_WEBHOOK_SECRET_{workspace_id} not found in KeePass "
                        "— signature verification DISABLED for this workspace. "
                        f"Store it with: uv run python scripts/keepass.py store "
                        f"--title 'CLICKUP_WEBHOOK_SECRET_{workspace_id}' "
                        "--username clickup --password '<value>'"
                    )
                    self._warned_no_secret.add(workspace_id)
        return self._secrets.get(workspace_id)

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    def handle(self, req: BaseHTTPRequestHandler):
        parsed_path = urlparse(req.path).path

        # Validate path shape — must be /clickup/<workspace_id>/task
        m = _PATH_RE.match(parsed_path)
        if not m:
            self._send_json(req, 404, {"status": "not_found"})
            return

        workspace_id = m.group(1)

        if req.command != "POST":
            req.send_response(405)
            req.send_header("Allow", "POST")
            req.send_header("Content-Length", "0")
            req.end_headers()
            return

        # Read body
        try:
            content_length = int(req.headers.get("Content-Length") or 0)
        except (ValueError, TypeError):
            content_length = 0
        body = (
            req.rfile.read(min(content_length, MAX_BODY_SIZE))
            if content_length
            else b""
        )

        # Signature verification (per-workspace secret)
        secret = self._get_secret(workspace_id)
        if secret:
            sig = req.headers.get("X-Signature")
            if not verify_signature(secret, body, sig):
                log.warning(
                    f"Workspace {workspace_id}: rejected webhook — "
                    "invalid or missing X-Signature"
                )
                self._send_json(req, 401, {"status": "unauthorized"})
                return

        # Parse JSON
        try:
            payload = json.loads(body) if body else {}
        except (ValueError, json.JSONDecodeError) as e:
            log.warning(f"Workspace {workspace_id}: invalid JSON body — {e}")
            self._send_json(req, 400, {"status": "error", "detail": "invalid json"})
            return

        event = str(payload.get("event") or "")
        task_id = str(payload.get("task_id") or "")

        # Ack quickly — ClickUp retries on non-2xx.
        self._send_json(req, 200, {"status": "ok"})

        self._process_event(workspace_id, event, task_id, payload)

    # ------------------------------------------------------------------
    # Informative event helper
    # ------------------------------------------------------------------

    @staticmethod
    def _write_informative_event(
        workspace_id: str,
        event: str,
        task_id: str,
        payload: dict,
        note: str = "",
    ) -> None:
        """Write a normalized informative inbox item for a non-actionable event.

        Mirrors the default webhook_receiver handler format:
        - type: "event"  (never "goal")
        - content: human-readable summary of what arrived
        - source: "clickup_task_webhook"

        The full payload is always available in webhook_receiver.log; the inbox
        item is capped at _INBOX_BODY_MAX to keep it scannable.
        """
        body_str = json.dumps(payload)
        if len(body_str) > _INBOX_BODY_MAX:
            body_str = (
                body_str[:_INBOX_BODY_MAX]
                + f"\n[truncated {len(body_str) - _INBOX_BODY_MAX} bytes"
                " — full payload in webhook_receiver.log]"
            )

        parts = [
            f"ClickUp event={event!r} workspace={workspace_id}"
            + (f" task={task_id}" if task_id else ""),
        ]
        if note:
            parts.append(f"Note: {note}")
        parts.append(f"Payload: {body_str}")

        now = datetime.now(timezone.utc).isoformat()
        item = {
            "type": "event",
            "content": "\n".join(parts),
            "timestamp": now,
            "received_at": now,
            "source": "clickup_task_webhook",
            "workspace_id": workspace_id,
            "priority": 3,
        }

        if write_to_inbox([item]):
            log.info(
                f"Workspace {workspace_id}: wrote informative event "
                f"{event!r} for task {task_id or '?'}" + (f" — {note}" if note else "")
            )
        else:
            log.error(
                f"Workspace {workspace_id}: failed to write informative event "
                f"{event!r} for task {task_id or '?'}"
            )

    # ------------------------------------------------------------------
    # Event processing
    # ------------------------------------------------------------------

    def _process_event(
        self,
        workspace_id: str,
        event: str,
        task_id: str,
        payload: dict,
    ):
        if event not in KNOWN_TASK_EVENTS:
            log.info(
                f"Workspace {workspace_id}: unknown event {event!r} — writing informative"
            )
            self._write_informative_event(
                workspace_id,
                event,
                task_id,
                payload,
                note=f"unrecognised event type {event!r}",
            )
            return

        if event != "taskStatusUpdated":
            log.info(
                f"Workspace {workspace_id}: {event!r} for task {task_id or '?'}"
                " — writing informative"
            )
            self._write_informative_event(workspace_id, event, task_id, payload)
            return

        transition = extract_status_transition(payload)
        if not transition:
            log.info(
                f"Workspace {workspace_id}: taskStatusUpdated for task "
                f"{task_id or '?'} has no parseable status history — writing informative"
            )
            self._write_informative_event(
                workspace_id,
                event,
                task_id,
                payload,
                note="taskStatusUpdated with no parseable status history",
            )
            return
        before, after = transition

        state = load_workspace_state(workspace_id)
        triggers = state["trigger_transitions"]
        if (before, after) not in triggers:
            log.info(
                f"Workspace {workspace_id}: task {task_id or '?'} "
                f"status {before!r} → {after!r} "
                "— no matching trigger, writing informative"
            )
            self._write_informative_event(
                workspace_id,
                event,
                task_id,
                payload,
                note=f"status changed {before!r} → {after!r} (no matching trigger configured)",
            )
            return

        if not task_id:
            log.warning(
                f"Workspace {workspace_id}: matched transition "
                f"{before!r} → {after!r} but payload has no task_id — writing informative"
            )
            self._write_informative_event(
                workspace_id,
                event,
                task_id,
                payload,
                note=f"matched transition {before!r} → {after!r} but task_id is missing",
            )
            return

        guideline_clause = (
            f" Use the review guideline at {state['review_prompt_path']}."
            if state["review_prompt_path"]
            else ""
        )
        content = (
            f"Review completion of ClickUp task {task_id} in workspace {workspace_id} "
            f"(status transitioned {before!r} → {after!r}). "
            f"Use the clickup-cli skill to fetch task details, description, "
            f"checklist, comments, and linked deliverables; then verify that "
            f"the task's acceptance criteria are met and post a review comment "
            f"back to the task."
            + guideline_clause
            + f" (triggered by ClickUp webhook event=taskStatusUpdated "
            f"task_id={task_id} workspace_id={workspace_id})"
        )
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "type": "goal",
            "content": content,
            "timestamp": now,
            "received_at": now,
            "source": "clickup_task_webhook",
            "workspace_id": workspace_id,
            "priority": 3,
        }

        if write_to_inbox([item]):
            log.info(
                f"Workspace {workspace_id}: queued review goal for task {task_id} "
                f"({before!r} → {after!r})"
            )
        else:
            log.error(
                f"Workspace {workspace_id}: failed to queue review goal "
                f"for task {task_id}"
            )

    @staticmethod
    def _send_json(req: BaseHTTPRequestHandler, status: int, data: dict):
        body = json.dumps(data).encode()
        req.send_response(status)
        req.send_header("Content-Type", "application/json")
        req.send_header("Content-Length", str(len(body)))
        req.end_headers()
        if req.command != "HEAD":
            req.wfile.write(body)
