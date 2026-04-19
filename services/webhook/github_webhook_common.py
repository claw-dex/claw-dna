#!/usr/bin/env python3
"""Shared infrastructure for GitHub webhook sub-handlers.

All per-event GitHub handlers live under the same URL prefix
(``/github``) and share:
  - One HMAC secret per org, stored in KeePass under
    ``GITHUB_WEBHOOK_SECRET_<org>``.
  - HMAC-SHA256 signature verification.
  - ``ping`` handling.
  - The inbox "informative event" envelope so operators always see the
    delivery even when no goal is queued.
  - A single cached lookup of the GitHub username the agent acts as
    (from ``GITHUB_TOKEN_1``) for SELF-origin detection.

Concrete handlers subclass :class:`GithubWebhookHandlerBase` (or
:class:`InformativeGithubWebhookHandler` when the event only needs to
produce inbox events and never queue goals).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from shared import write_to_inbox

# --- Constants ---
WEBHOOK_PATH_PREFIX = "/github"

# KeePass key pattern — replace {org} before lookup.
KEEPASS_SECRET_KEY_TEMPLATE = "GITHUB_WEBHOOK_SECRET_{org}"

# KeePass title for the GitHub credential this agent acts as. The stored
# username is a real GitHub user (not a bot account).
KEEPASS_AGENT_TOKEN_TITLE = "GITHUB_TOKEN_1"

MAX_BODY_SIZE = 1_048_576  # 1 MB
# Inbox body cap — full payload is logged separately to
# webhook_receiver.log, so the inbox snippet stays scannable.
INBOX_BODY_MAX = 4_096

# GitHub org names: alphanumeric and hyphens, must start alphanumeric.
_ORG_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9-]*"


def compile_event_path_re(event: str) -> re.Pattern:
    """Return a compiled regex matching ``/github/<org>/<event>`` exactly."""
    return re.compile(rf"^/github/({_ORG_SEGMENT})/{re.escape(event)}$")


# ---------------------------------------------------------------------------
# KeePass helpers
# ---------------------------------------------------------------------------


def keepass_get(title: str, log: logging.Logger | None = None) -> str | None:
    """Return the password for a KeePass entry, or None if unavailable."""
    try:
        from scripts.keepass import get_credential

        return get_credential(title)
    except Exception as e:
        (log or logging.getLogger(__name__)).warning(
            f"KeePass get({title!r}) failed: {e}"
        )
    return None


def keepass_get_username(
    title: str, log: logging.Logger | None = None
) -> str | None:
    """Return the username field of a KeePass entry, or None if unavailable."""
    try:
        from scripts.keepass import get_credential_entry

        entry = get_credential_entry(title)
        if not entry:
            return None
        username = entry.get("username") or ""
        return username or None
    except Exception as e:
        (log or logging.getLogger(__name__)).warning(
            f"KeePass get_credential_entry({title!r}) failed: {e}"
        )
    return None


# Module-level cache for the agent's GitHub username. Resolved once per
# process; operators rotating ``GITHUB_TOKEN_1`` should restart
# webhook_receiver. ``_agent_user_loaded`` distinguishes "not yet looked
# up" from "looked up but absent" (stored as "").
_agent_user_loaded: bool = False
_agent_user: str = ""


def get_agent_github_user(log: logging.Logger | None = None) -> str | None:
    """Return the GitHub username from ``GITHUB_TOKEN_1``, cached module-wide.

    Handlers use this to decide whether an event is self-authored (e.g.
    review comments the agent posted) or whether a review request is
    directed at the agent's own login.

    Returns ``None`` when the credential is missing or has no username;
    callers should decide how to degrade (typically: skip SELF flagging).
    Logs a single warning on the first lookup that fails.
    """
    global _agent_user_loaded, _agent_user
    if not _agent_user_loaded:
        _agent_user_loaded = True
        _agent_user = keepass_get_username(KEEPASS_AGENT_TOKEN_TITLE, log) or ""
        if not _agent_user and log is not None:
            log.warning(
                f"{KEEPASS_AGENT_TOKEN_TITLE} not found in KeePass or has no "
                "username — SELF-origin and review-target detection will be "
                "disabled for all GitHub handlers."
            )
    return _agent_user or None


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Return True iff ``header`` is a valid GitHub sha256 HMAC of ``body``."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    provided = header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def send_json(req: BaseHTTPRequestHandler, status: int, data: dict) -> None:
    body = json.dumps(data).encode()
    req.send_response(status)
    req.send_header("Content-Type", "application/json")
    req.send_header("Content-Length", str(len(body)))
    req.end_headers()
    if req.command != "HEAD":
        req.wfile.write(body)


# ---------------------------------------------------------------------------
# Inbox helpers
# ---------------------------------------------------------------------------


def write_informative_event(
    *,
    log: logging.Logger,
    source: str,
    org: str,
    event: str,
    action: str,
    repo_name: str,
    pr_number,
    payload: dict,
    note: str = "",
) -> None:
    """Write a normalized informative inbox item for a non-actionable event.

    Inbox entry format: ``type: "event"`` (never ``"goal"``), a
    human-readable ``content`` summary, and the caller-supplied
    ``source`` tag. The full payload is always in
    ``webhook_receiver.log``; the inbox body is truncated at
    :data:`INBOX_BODY_MAX`.
    """
    body_str = json.dumps(payload)
    if len(body_str) > INBOX_BODY_MAX:
        body_str = (
            body_str[:INBOX_BODY_MAX]
            + f"\n[truncated {len(body_str) - INBOX_BODY_MAX} bytes"
            " — full payload in webhook_receiver.log]"
        )

    pr_label = f"#{pr_number}" if pr_number is not None else "?"
    repo_label = repo_name or "?"
    parts = [
        f"GitHub event={event!r} action={action!r} "
        f"org={org} repo={repo_label} pr={pr_label}",
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
        "source": source,
        "org": org,
        "priority": 3,
    }

    if write_to_inbox([item]):
        log.info(
            f"Org {org}: wrote informative event {event!r}/{action!r} "
            f"for {repo_label}{pr_label}" + (f" — {note}" if note else "")
        )
    else:
        log.error(
            f"Org {org}: failed to write informative event "
            f"{event!r}/{action!r} for {repo_label}{pr_label}"
        )


# ---------------------------------------------------------------------------
# Base class — all GitHub webhook sub-handlers
# ---------------------------------------------------------------------------


class GithubWebhookHandlerBase:
    """Shared HTTP/HMAC/ping plumbing for per-event GitHub handlers.

    Subclasses declare :attr:`EVENT_NAME` (matches ``X-GitHub-Event`` and
    the URL segment under ``/github/<org>/<event>``) and :attr:`SOURCE`
    (inbox ``source`` tag), and implement :meth:`_process_event` to act
    on validated payloads.

    The default :meth:`_extract_summary` pulls ``repository.name`` and
    ``pull_request.number`` out of the payload for the inbox summary
    line — subclasses handling non-PR events can override it.
    """

    path_prefix = WEBHOOK_PATH_PREFIX

    #: GitHub event name; also the last URL segment.
    EVENT_NAME: str = ""
    #: Value used as the inbox item's ``source`` field.
    SOURCE: str = ""

    def __init__(self) -> None:
        if not self.EVENT_NAME or not self.SOURCE:
            raise ValueError(
                f"{type(self).__name__} must define EVENT_NAME and SOURCE"
            )
        self._secrets: dict[str, str | None] = {}
        self._warned_no_secret: set[str] = set()
        self._path_re = compile_event_path_re(self.EVENT_NAME)
        self.log = logging.getLogger(
            f"webhook_receiver.github_{self.EVENT_NAME}"
        )

    # -- Lifecycle ------------------------------------------------------

    def start(self) -> None:
        self.log.info(f"Starting GitHub {self.EVENT_NAME} handler...")
        self.log.info(
            f"GitHub {self.EVENT_NAME} handler ready — "
            f"path pattern: /webhook/github/<org>/{self.EVENT_NAME}"
        )

    def shutdown(self) -> None:
        self.log.info(f"GitHub {self.EVENT_NAME} handler stopped.")

    # -- Dispatcher hook ------------------------------------------------

    def matches(self, path: str) -> bool:
        """Return True iff ``path`` targets this handler's event URL."""
        return self._path_re.match(path) is not None

    # -- Secret management ---------------------------------------------

    def _load_secret(self, org: str) -> str | None:
        key = KEEPASS_SECRET_KEY_TEMPLATE.format(org=org)
        secret = keepass_get(key, self.log)
        self._secrets[org] = secret
        return secret

    def _get_secret(self, org: str) -> str | None:
        if org not in self._secrets:
            self._load_secret(org)
            if not self._secrets.get(org) and org not in self._warned_no_secret:
                self.log.warning(
                    f"Org {org}: GITHUB_WEBHOOK_SECRET_{org} not found in "
                    "KeePass — signature verification DISABLED for this org."
                )
                self._warned_no_secret.add(org)
        return self._secrets.get(org)

    # -- Request handling -----------------------------------------------

    def handle(self, req: BaseHTTPRequestHandler) -> None:
        parsed_path = urlparse(req.path).path
        m = self._path_re.match(parsed_path)
        if not m:
            # _find_handler should gate by matches(); defense in depth if
            # the dispatcher ever bypasses it.
            send_json(req, 404, {"status": "not_found"})
            return

        url_org = m.group(1)

        if req.command != "POST":
            req.send_response(405)
            req.send_header("Allow", "POST")
            req.send_header("Content-Length", "0")
            req.end_headers()
            return

        try:
            content_length = int(req.headers.get("Content-Length") or 0)
        except (ValueError, TypeError):
            content_length = 0
        body = (
            req.rfile.read(min(content_length, MAX_BODY_SIZE))
            if content_length
            else b""
        )

        secret = self._get_secret(url_org)
        if secret:
            sig = req.headers.get("X-Hub-Signature-256")
            if not verify_signature(secret, body, sig):
                self.log.warning(
                    f"Org {url_org}: rejected webhook — "
                    "invalid or missing X-Hub-Signature-256"
                )
                send_json(req, 401, {"status": "unauthorized"})
                return

        event = req.headers.get("X-GitHub-Event", "")
        if event == "ping":
            send_json(req, 200, {"status": "pong"})
            return

        try:
            payload = json.loads(body) if body else {}
        except (ValueError, json.JSONDecodeError) as e:
            self.log.warning(f"Org {url_org}: invalid JSON body — {e}")
            send_json(req, 400, {"status": "error", "detail": "invalid json"})
            return

        # Ack quickly — GitHub retries on non-2xx.
        send_json(req, 200, {"status": "ok"})

        try:
            self._process_event(url_org, event, payload)
        except Exception:
            # Already acked; logging here prevents webhook_receiver's
            # outer handler from trying to send a second response.
            self.log.exception(f"Org {url_org}: _process_event failed")

    # -- Subclass hooks -------------------------------------------------

    def _process_event(self, url_org: str, event: str, payload: dict) -> None:
        """Handle a validated, non-ping delivery. Subclasses must implement."""
        raise NotImplementedError

    def _extract_summary(self, payload: dict) -> tuple[str, object]:
        """Return ``(repo_name, pr_number)`` for the inbox summary line.

        Default extracts PR-ish fields from a standard GitHub payload.
        Subclasses handling non-PR events (e.g. ``push``, ``check_run``)
        should override to surface the most relevant identifier.
        """
        pr = payload.get("pull_request") or {}
        repo = payload.get("repository") or {}
        return (repo.get("name") or "", pr.get("number"))

    # -- Inbox helper ---------------------------------------------------

    def _write_informative_event(
        self,
        url_org: str,
        event: str,
        action: str,
        payload: dict,
        note: str = "",
    ) -> None:
        repo_name, pr_number = self._extract_summary(payload)
        write_informative_event(
            log=self.log,
            source=self.SOURCE,
            org=url_org,
            event=event or self.EVENT_NAME,
            action=action,
            repo_name=repo_name,
            pr_number=pr_number,
            payload=payload,
            note=note,
        )


# ---------------------------------------------------------------------------
# Informative-only subclass
# ---------------------------------------------------------------------------


class InformativeGithubWebhookHandler(GithubWebhookHandlerBase):
    """Base class for per-event handlers that only produce inbox events.

    Every non-ping delivery is acknowledged with 200 and written to the
    inbox as ``type: "event"``; these handlers never queue goals.
    Subclasses may override :meth:`_note_for` to inject a one-line
    event-specific summary into the inbox entry.
    """

    def start(self) -> None:
        self.log.info(
            f"Starting GitHub {self.EVENT_NAME} handler (informative-only)..."
        )
        self.log.info(
            f"GitHub {self.EVENT_NAME} handler ready — "
            f"path pattern: /webhook/github/<org>/{self.EVENT_NAME}"
        )

    def _process_event(self, url_org: str, event: str, payload: dict) -> None:
        action = str(payload.get("action") or "")
        repo = payload.get("repository") or {}
        payload_org = (repo.get("owner") or {}).get("login") or ""

        notes: list[str] = []
        if event and event != self.EVENT_NAME:
            notes.append(
                f"X-GitHub-Event {event!r} does not match handler event "
                f"{self.EVENT_NAME!r}"
            )
        if payload_org and payload_org.lower() != url_org.lower():
            notes.append(
                f"payload org {payload_org!r} does not match URL org "
                f"{url_org!r}"
            )

        summary = self._note_for(payload)
        if summary:
            notes.append(summary)

        self._write_informative_event(
            url_org,
            event or self.EVENT_NAME,
            action,
            payload,
            note="; ".join(notes),
        )

    # -- Subclass hook --------------------------------------------------

    def _note_for(self, payload: dict) -> str:
        """Return an event-specific one-line summary for the inbox note.

        Default is empty. Subclasses override to surface the fields that
        matter most for their event (e.g. comment author and file:line).
        """
        return ""
