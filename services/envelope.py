"""Unified email-like message envelope for the agent inbox/outbox system.

This module is the single home for the structured ``from`` identity, stable
message ``id`` minting, outbox de-duplication keys, and the bridge-owned
id -> origin resolution map that powers per-user reply routing.

Import precedent
----------------
``services`` is on the pythonpath (``pyproject.toml`` ``pythonpath`` setting),
so services and tests import this as ``from envelope import ...``. The portal
reaches it through the same ``sys.path.insert(.../services)`` bootstrap used by
``app/chat.py`` so there is a single shared module instance (not two divergent
caches).

Design
------
* Sender identity is a **structured** ``from`` object that splits *routing*
  (``transport`` + ``channel``) from *identity* (``user_id`` + ``handle`` +
  ``role``). Chat is not email: a group chat has one delivery ``channel`` but
  many distinct ``user_id`` speakers.
* The agent never writes raw platform ids. It replies with ``in_reply_to:
  <inbox id>`` (or an optional friendly ``to`` handle); the bridge resolves
  that back to ``from`` and delivers to exactly one user.
* Everything is additive and backward-compatible: every field is optional and
  legacy values (e.g. a string ``from``) degrade gracefully.
"""

from __future__ import annotations

import hashlib
import json
import uuid

# ``source`` = where the message ORIGINATED (the meaningful "who/what sent it").
# ``transport`` = the technical middleware that DELIVERED it into the inbox,
# recorded only when it is distinct from the source. They must NOT duplicate
# each other: e.g. a GitHub or WhatsApp message arriving via the webhook
# receiver is {source: "github"|"whatsapp", transport: "webhook"}, whereas a
# Slack DM or a portal write needs no transport (the source says it all).
# Both are documentation aids; unknown values still pass through unchanged.
SOURCES = (
    "slack",
    "telegram",
    "whatsapp",
    "github",
    "portal",
    "scheduler",
    "internal_agent",
    "external_agent",
)
TRANSPORTS = ("webhook", "polling_script", "portal")

# Bound on the per-bridge id -> origin resolution map so it never grows without
# limit. Matches the history cap used elsewhere (append_to_history default).
ORIGIN_MAP_MAX = 500


# ---------------------------------------------------------------------------
# Structured ``from`` identity
# ---------------------------------------------------------------------------


def make_from(
    source: str | None = None,
    *,
    transport: str | None = None,
    channel: str | None = None,
    user_id: str | None = None,
    handle: str | None = None,
    role: str | None = None,
) -> dict:
    """Build a structured ``from`` object, dropping empty values.

    ``source``     — where the message ORIGINATED (slack, telegram, whatsapp,
                     github, portal, scheduler, external_agent, …). The primary
                     identity field.
    ``transport``  — the delivery middleware, set ONLY when distinct from the
                     source (e.g. ``"webhook"`` for github/whatsapp arriving via
                     the webhook receiver). Omit it when the source already says
                     how the message arrived — the two must not duplicate.
    ``channel``    — delivery target (chat/DM id). Bridge-internal; never shown
                     to the agent.
    ``user_id``    — platform user id (identity, not routing).
    ``handle``     — human-readable @display handle.
    ``role``       — ``"owner"`` or ``"member"`` (identity label only; not a
                     permission gate).
    """
    out: dict = {}
    if source:
        out["source"] = str(source)
    if transport:
        out["transport"] = str(transport)
    if channel:
        out["channel"] = str(channel)
    if user_id:
        out["user_id"] = str(user_id)
    if handle:
        out["handle"] = str(handle)
    if role:
        out["role"] = str(role)
    return out


def message_source(msg: dict) -> str | None:
    """Return a message's source, preferring the new ``from.source`` location.

    Falls back to the legacy top-level ``source`` for messages written before
    the relocation (and for non-message objects like goals/reminders/tasks,
    which keep their own top-level ``source``). Returns ``None`` for non-dict
    input. Mirrors ``app.shared.message_source``.
    """
    if not isinstance(msg, dict):
        return None
    frm = msg.get("from")
    if isinstance(frm, dict) and frm.get("source"):
        return frm["source"]
    return msg.get("source")


def parse_from(value) -> dict:
    """Normalize a ``from`` value to a dict.

    Accepts the structured dict form unchanged. A legacy **string** ``from``
    (the old ``messages/external/<name>/outbox.json`` path, or an operator
    ``--from`` string) is wrapped as ``{"raw": value}`` so it carries no
    identity but never crashes a reader. Anything else returns ``{}``.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        return {"raw": value}
    return {}


# ---------------------------------------------------------------------------
# Stable message id
# ---------------------------------------------------------------------------


def new_id() -> str:
    """Mint a new unique message id (uuid4). Single source for all bridges."""
    return str(uuid.uuid4())


def ensure_id(envelope: dict) -> str:
    """Ensure *envelope* has a stable ``id``; return it.

    Stamps a fresh uuid only when one is absent so re-processing the same dict
    is idempotent.
    """
    mid = envelope.get("id")
    if not mid:
        mid = new_id()
        envelope["id"] = mid
    return str(mid)


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------


def msg_hash(msg: dict) -> str:
    """Stable 16-char hash of a message dict (full-dict content hash).

    Canonical implementation shared by every bridge (previously duplicated in
    slack_bridge / telegram_bridge / whatsapp_bridge_handler).
    """
    key = json.dumps(msg, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def dedup_key(msg: dict) -> str:
    """Return a stable de-dup key for an outbox message.

    Prefers the message ``id`` (immutable across field mutations such as
    ``sent: true``); falls back to the full-dict ``msg_hash`` for legacy
    messages and non-bridge writers that never stamped an id.
    """
    mid = msg.get("id")
    if mid:
        return f"id:{mid}"
    return msg_hash(msg)


# ---------------------------------------------------------------------------
# Bridge-owned id -> origin resolution map
# ---------------------------------------------------------------------------
#
# Each bridge persists this map in its own state file (e.g. slack_state.json)
# under the ``origin_map`` key. It is independent of inbox_history (which
# cycle_close drains and append_to_history caps), so a reply to an
# already-archived message still resolves.
#
# Shape: ``{ "<msg_id>": {"from": <from dict>, "origin_ref": <str>} }``
# ``origin_ref`` is the platform-native message handle (Slack ts, Telegram
# message_id, WhatsApp message id) used for threaded replies.


def record_origin(store: dict, *, msg_id: str, from_obj: dict, origin_ref=None) -> None:
    """Record an incoming message's origin and trim the map to ``ORIGIN_MAP_MAX``.

    Re-recording an existing id refreshes it to most-recent (dict insertion
    order), so the trim keeps the freshest entries.
    """
    if not msg_id:
        return
    key = str(msg_id)
    # Refresh recency: drop then re-insert so this id is last in iteration order.
    store.pop(key, None)
    store[key] = {"from": from_obj or {}, "origin_ref": origin_ref}
    # Trim oldest entries (front of insertion order) beyond the cap.
    excess = len(store) - ORIGIN_MAP_MAX
    if excess > 0:
        for old_key in list(store.keys())[:excess]:
            store.pop(old_key, None)


def resolve_origin(store: dict, msg_id: str):
    """Resolve ``msg_id`` -> ``(from_obj, origin_ref)`` or ``None`` if unknown."""
    if not msg_id:
        return None
    entry = store.get(str(msg_id))
    if not isinstance(entry, dict):
        return None
    return entry.get("from") or {}, entry.get("origin_ref")


def resolve_handle(store: dict, handle: str):
    """Resolve a friendly ``handle`` (or user_id) -> most-recent ``from_obj``.

    Powers the optional ``to: "@vincent"`` unprompted-send path. Matches on
    ``handle`` first, then ``user_id``. Returns the most recently recorded
    match, or ``None``.
    """
    if not handle:
        return None
    needle = str(handle).lstrip("@").strip().lower()
    if not needle:
        return None
    # Iterate most-recent first (dict preserves insertion order).
    for entry in reversed(list(store.values())):
        if not isinstance(entry, dict):
            continue
        frm = entry.get("from") or {}
        h = str(frm.get("handle") or "").lstrip("@").strip().lower()
        uid = str(frm.get("user_id") or "").strip().lower()
        if needle in (h, uid):
            return frm
    return None


def sanitize_origin_map(value) -> dict:
    """Return a well-formed ``origin_map`` for state validation.

    Drops malformed entries (mirrors the ``outbox_threads`` sanitization in the
    bridges). Bounds the result to ``ORIGIN_MAP_MAX`` keeping the newest.
    """
    if not isinstance(value, dict):
        return {}
    clean: dict = {}
    for k, v in value.items():
        if not isinstance(k, str):
            continue
        if not isinstance(v, dict):
            continue
        frm = v.get("from")
        if frm is not None and not isinstance(frm, dict):
            continue
        clean[k] = {"from": frm or {}, "origin_ref": v.get("origin_ref")}
    excess = len(clean) - ORIGIN_MAP_MAX
    if excess > 0:
        for old_key in list(clean.keys())[:excess]:
            clean.pop(old_key, None)
    return clean
