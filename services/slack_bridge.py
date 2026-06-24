#!/usr/bin/env python3
"""
Slack Bridge Service
====================
Bridges the agent's inbox/outbox with a Slack bot using Bolt for Python
(https://docs.slack.dev/tools/bolt-python/) over **Socket Mode** — the bot
connects outbound over a WebSocket, so no public URL / inbound port / reverse
proxy is required.

- Incoming Slack messages (DMs, channel messages, @mentions) → /agent/messages/inbox.json
- Polls outbox.json every 60s and forwards unsent messages to Slack
- Tracks sent message hashes to prevent duplicates (survives restarts)
- Auto-discovers the owner from the user's first DM to the bot

It also enables Slack's **Agents & AI Apps** assistant container
(https://docs.slack.dev/ai/developing-agents/): a dedicated 1:1 assistant pane
with a greeting, suggested-prompt chips, a "thinking…" status, and a thread
title. Commands (status/today/help/…) are answered instantly in-thread; any
other message is queued as a goal and acknowledged immediately, with the
agent's real answer arriving on its next cycle via the shared outbox.

Slack app setup (one time):
  1. Create a Slack app at https://api.slack.com/apps (from scratch).
  2. Enable **Socket Mode** (Settings → Socket Mode). Generate an app-level
     token with the ``connections:write`` scope → ``xapp-...``.
  3. Enable **Agents & AI Apps** (App Home / "Agents & AI Apps") — this adds
     the ``assistant:write`` scope and the assistant container.
  4. OAuth & Permissions → add bot token scopes:
       assistant:write, chat:write, app_mentions:read, im:history, im:read,
       im:write, channels:history, groups:history, files:read, users:read
     (add ``commands`` too if you register slash commands).
  5. Event Subscriptions → subscribe to bot events:
       assistant_thread_started, assistant_thread_context_changed,
       message.im, message.channels, message.groups, app_mention
  6. (Optional) Slash Commands → add /status, /goals, /journal, /today,
     /outbox, /cycles, /services, /remind, /note, /notes, /heartbeat, /help.
     Commands also work as plain text ("status", "help", or "@bot status"),
     so registering them is optional.
  7. Install the app to your workspace → copy the Bot User OAuth Token
     (``xoxb-...``).
  8. Store both tokens in KeePass:
       uv run python scripts/keepass.py store --title "SLACK_BOT_TOKEN" \
         --username slack --password "xoxb-..." --group "API Keys"
       uv run python scripts/keepass.py store --title "SLACK_APP_TOKEN" \
         --username slack --password "xapp-..." --group "API Keys"
  9. Start the service (see below) and DM the bot — it auto-discovers you as
     the owner. Other users gain access by @mentioning you (the owner).

Management:
  uv run python scripts/service_manager.py start slack_bridge --auto-start -- \
      uv run python services/slack_bridge.py
  uv run python scripts/service_manager.py status slack_bridge   # check status
  uv run python scripts/service_manager.py stop slack_bridge     # stop service
  uv run python scripts/service_manager.py list                  # list services
"""

import fcntl
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from envelope import (
    dedup_key,
    ensure_id,
    make_from,
    msg_hash,
    record_origin,
    reply_target,
    resolve_handle,
    resolve_origin,
    sanitize_origin_map,
)
from shared import append_to_history, atomic_write_json, write_to_inbox

# --- Paths ---
BASE = Path("/agent")
STATE_FILE = BASE / "memory" / "slack_state.json"
INBOX_FILE = BASE / "messages" / "inbox.json"
OUTBOX_FILE = BASE / "messages" / "outbox.json"
BRIDGE_DIR = BASE / "messages" / "bridge" / "slack"
INBOX_HISTORY_FILE = BRIDGE_DIR / "inbox_history.json"
OUTBOX_HISTORY_FILE = BRIDGE_DIR / "outbox_history.json"
CHAT_HISTORY_FILE = BRIDGE_DIR / "chat_history.json"
LOG_DIR = BASE / "memory" / "logs"
LOG_FILE = LOG_DIR / "slack_bridge.log"
HEARTBEAT_DIR = BASE / "memory" / "heartbeats"
HEARTBEAT_FILE = HEARTBEAT_DIR / "slack_bridge.heartbeat"
LOCK_FILE = BASE / "memory" / "slack_bridge.lock"
STATE_JSON = BASE / "memory" / "state.json"
MEDIA_DIR = BASE / "workspace" / "slack"

# --- Logging ---
log = logging.getLogger("slack_bridge")


def _setup_logging():
    """Create log/heartbeat dirs and attach handlers.

    Deferred to main() so the module can be imported on hosts without /agent
    (e.g. tests, dev machines).
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _migrate_legacy_message_files():
    """Move pre-existing message records from /agent/memory/ to BRIDGE_DIR.

    Idempotent: skips when the new path already exists. Non-fatal on errors.
    """
    legacy_pairs = [
        (BASE / "memory" / "slack_inbox_history.json", INBOX_HISTORY_FILE),
        (BASE / "memory" / "slack_outbox_history.json", OUTBOX_HISTORY_FILE),
        (BASE / "memory" / "slack_chat_history.json", CHAT_HISTORY_FILE),
    ]
    for old, new in legacy_pairs:
        try:
            if old.exists() and not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(new))
                log.info(f"Migrated {old} -> {new}")
        except (OSError, shutil.Error) as exc:
            log.warning(f"Failed to migrate {old} -> {new}: {exc}")


def _write_heartbeat():
    """Write current epoch timestamp to heartbeat file for liveness detection."""
    try:
        HEARTBEAT_FILE.write_text(str(time.time()))
    except OSError:
        pass  # non-critical


# --- Constants ---
KEEPASS_SLACK_BOT_TOKEN = "SLACK_BOT_TOKEN"
KEEPASS_SLACK_APP_TOKEN = "SLACK_APP_TOKEN"
KEEPASS_SLACK_OWNER_USERID = "SLACK_OWNER_USERID"
KEEPASS_SLACK_OWNER_USERNAME = "SLACK_OWNER_USERNAME"
KEEPASS_SLACK_CHAT_ID = "SLACK_CHAT_ID"  # comma-separated authorized channel IDs
# Optional: channel id that unaddressed outbox messages (status/FYI with no
# in_reply_to/to) are redirected to instead of the owner's DM. Read at startup
# from env SLACK_UNADDRESSED_CHANNEL first, then this KeePass key.
KEEPASS_SLACK_UNADDRESSED_CHANNEL = "SLACK_UNADDRESSED_CHANNEL"

OUTBOX_INTERVAL = 60  # How often to check outbox (seconds)
LOOP_SLEEP = 5  # Main-loop tick (seconds); incoming runs on the Socket Mode thread
SLACK_MAX_LEN = 39000  # Slack chat.postMessage text limit is ~40k chars
SEND_TIMEOUT = 10  # HTTP timeout for file downloads (connect)
MAX_MEDIA_BYTES = 50 * 1024 * 1024  # 50 MB per-file safety limit
MAX_PROCESSED_KEYS = 500  # bounded inbound-dedup history

# Recognized bot commands (without the leading slash). Used by both the native
# slash-command handlers and the keyword/mention text path.
COMMANDS = {
    "status",
    "goals",
    "journal",
    "today",
    "outbox",
    "cycles",
    "services",
    "remind",
    "note",
    "notes",
    "heartbeat",
    "help",
    "start",
}


# ---------------------------------------------------------------------------
# Heartbeat helpers (agent cycle heartbeat, for ack messages)
# ---------------------------------------------------------------------------

_heartbeat_cache: dict = {"mtime": 0.0, "value": None}


def get_last_heartbeat() -> datetime | None:
    """Return the last_heartbeat timestamp from state.json, or None.

    Uses mtime-based caching to avoid re-reading on every incoming message.
    """
    try:
        mt = STATE_JSON.stat().st_mtime
        if mt == _heartbeat_cache["mtime"]:
            return _heartbeat_cache["value"]
        state = json.loads(STATE_JSON.read_text())
        raw = state.get("last_heartbeat")
        val = datetime.fromisoformat(raw) if raw else None
        _heartbeat_cache["mtime"] = mt
        _heartbeat_cache["value"] = val
        return val
    except Exception as e:
        log.warning(f"Could not read state.json: {e}")
    return None


def relative_time(dt: datetime) -> str:
    """Return a human-readable relative time string, e.g. '10 minutes ago'."""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = int((now - dt).total_seconds())
    if diff < 60:
        return f"{diff} second{'s' if diff != 1 else ''} ago"
    elif diff < 3600:
        m = diff // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    elif diff < 86400:
        h = diff // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    else:
        d = diff // 86400
        return f"{d} day{'s' if d != 1 else ''} ago"


def build_ack_message() -> str:
    """Build the auto-reply acknowledgement message."""
    hb_dt = get_last_heartbeat()
    if hb_dt:
        ts = hb_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        rel = relative_time(hb_dt)
        return (
            f"Got it! I'll get back to you in the next cycle.\n"
            f"Last heartbeat: {ts} ({rel})."
        )
    return "Got it! I'll get back to you in the next cycle."


# ---------------------------------------------------------------------------
# KeePass helpers
# ---------------------------------------------------------------------------


def keepass_get(title: str) -> str | None:
    try:
        from scripts.keepass import get_credential

        return get_credential(title)
    except Exception as e:
        log.warning(f"KeePass get({title!r}) failed: {e}")
    return None


def keepass_store(
    title: str, username: str, value: str, group: str = "API Keys"
) -> bool:
    try:
        from scripts.keepass import store_credential

        return store_credential(
            title=title, username=username, password=value, group=group
        )
    except Exception as e:
        log.warning(f"KeePass store({title!r}) failed: {e}")
    return False


# ---------------------------------------------------------------------------
# Chat ID helpers
# ---------------------------------------------------------------------------


def parse_chat_ids(csv_string: str | None) -> list[str]:
    """Parse a comma-separated string of chat IDs into a list."""
    if not csv_string:
        return []
    return [cid.strip() for cid in csv_string.split(",") if cid.strip()]


def serialize_chat_ids(chat_ids: list[str]) -> str:
    """Serialize a list of chat IDs into a comma-separated string."""
    return ",".join(chat_ids)


def contains_username(text: str, username: str) -> bool:
    """Check if text contains the username (or @username) as a whole word, case-insensitive."""
    if not username:
        return False
    pattern = r"(?<!\w)@?" + re.escape(username) + r"\b"
    return bool(re.search(pattern, text, re.IGNORECASE))


# ---------------------------------------------------------------------------
# Slack mrkdwn helpers
# ---------------------------------------------------------------------------


def escape_slack(text: str) -> str:
    """Escape the three characters Slack treats specially in message text.

    Slack mrkdwn only reserves ``&``, ``<`` and ``>`` (used for entities and
    links). Unlike Telegram's MarkdownV2, formatting markers (``*``/``_``/`` ` ``)
    do not need escaping in user content, so this is intentionally minimal.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def strip_bot_mention(text: str, bot_user_id: str) -> str:
    """Remove a leading ``<@BOTID>`` mention (and surrounding space) from text."""
    if not text:
        return text
    if bot_user_id:
        text = re.sub(r"<@" + re.escape(bot_user_id) + r"(\|[^>]*)?>", "", text)
    return text.strip()


def parse_command(text: str) -> tuple[str | None, list[str]]:
    """Parse free-text into a (command, args) pair.

    Accepts an optional leading ``/`` or ``!`` (e.g. ``/status``, ``!status``,
    ``status``). Returns ``(None, [])`` when the first token is not a known
    command so the message is treated as a regular goal instead.
    """
    if not text:
        return None, []
    parts = text.split()
    if not parts:
        return None, []
    head = parts[0].lstrip("/!").lower()
    if head in COMMANDS:
        return head, parts[1:]
    return None, []


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _default_state() -> dict:
    # outbox_threads: {channel_id: thread_ts} —
    # the Slack thread root for outbox messages, set when the owner sends a new
    # top-level DM. Rotates whenever the owner starts a new conversation.
    # origin_map: {msg_id: {"from": <from dict>, "origin_ref": <slack ts>}} —
    # the bridge-owned id -> origin resolution map that lets the agent reply to
    # a specific user via outbox `in_reply_to`. See services/envelope.py.
    return {
        "sent_hashes": [],
        "processed_keys": [],
        "outbox_threads": {},
        "origin_map": {},
    }


def _validate_state(data) -> dict:
    """Ensure state has the expected structure, repairing wrong types."""
    if not isinstance(data, dict):
        log.warning(f"Slack state has unexpected type {type(data).__name__}, resetting")
        return _default_state()
    for field in ("sent_hashes", "processed_keys"):
        vals = data.get(field)
        if not isinstance(vals, list):
            log.warning(
                f"Slack state {field} has wrong type "
                f"{type(vals).__name__}, resetting to []"
            )
            data[field] = []
        else:
            cleaned = [v for v in vals if isinstance(v, str)]
            if len(cleaned) != len(vals):
                log.warning(
                    f"Removed {len(vals) - len(cleaned)} non-string entries from {field}"
                )
                data[field] = cleaned
    # Migrate legacy daily_threads → outbox_threads (flat {cid: ts} map).
    if "daily_threads" in data and "outbox_threads" not in data:
        legacy = data.pop("daily_threads")
        migrated: dict = {}
        if isinstance(legacy, dict):
            for cid, ent in legacy.items():
                if isinstance(ent, dict) and isinstance(ent.get("thread_ts"), str):
                    migrated[cid] = ent["thread_ts"]
        data["outbox_threads"] = migrated
        if migrated:
            log.info(f"Migrated {len(migrated)} daily_threads entries → outbox_threads")
    elif "daily_threads" in data:
        data.pop("daily_threads")  # remove stale key if outbox_threads already present

    threads = data.get("outbox_threads")
    if not isinstance(threads, dict):
        if threads is not None:
            log.warning(
                f"Slack state outbox_threads has wrong type "
                f"{type(threads).__name__}, resetting to {{}}"
            )
        data["outbox_threads"] = {}
    else:
        # Drop malformed entries (values must be non-empty strings).
        cleaned = {cid: ts for cid, ts in threads.items() if isinstance(ts, str) and ts}
        if len(cleaned) != len(threads):
            log.warning(
                f"Dropped {len(threads) - len(cleaned)} malformed outbox_threads entries"
            )
        data["outbox_threads"] = cleaned

    # origin_map: sanitize via the shared helper (drops malformed entries,
    # bounds size). Missing/wrong-typed → {}.
    data["origin_map"] = sanitize_origin_map(data.get("origin_map"))
    return data


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return _validate_state(data)
        except Exception as e:
            log.warning(f"Corrupt slack state file, using defaults: {e}")
    return _default_state()


def save_state(state: dict):
    try:
        atomic_write_json(STATE_FILE, state, indent=2)
    except Exception as e:
        log.error(f"CRITICAL: Failed to save Slack state: {e}", exc_info=True)


def _recover_outbox_thread(client, cid: str, bot_user_id: str) -> str | None:
    """Find the outbox thread root by scanning the channel's recent history.

    Called on startup when the local state is missing for a channel.
    Queries Slack for recent top-level messages and returns the ``ts`` of
    the most recent top-level message sent by the owner (non-bot user) — which
    is the message the owner last used to start a conversation with the bot.

    Returns None on any API error or when no suitable message is found.
    """
    try:
        resp = client.conversations_history(channel=cid, limit=20)
        # conversations_history returns newest-first; find most recent
        # top-level owner message (no thread_ts = top-level DM).
        for msg in resp.get("messages") or []:
            # Skip bot messages and threaded replies
            if msg.get("bot_id") or msg.get("user") == bot_user_id:
                continue
            if msg.get("thread_ts"):
                continue  # skip replies — we want top-level messages only
            return msg["ts"]
    except Exception as e:
        log.debug(f"Outbox thread recovery for {cid} failed: {e}")
    return None


# msg_hash / dedup_key are imported from envelope (single canonical impl).


# ---------------------------------------------------------------------------
# Chat history (in-memory + disk-synced)
# ---------------------------------------------------------------------------


def load_chat_history() -> dict:
    """Load per-chat conversation history from disk."""
    if CHAT_HISTORY_FILE.exists():
        try:
            return json.loads(CHAT_HISTORY_FILE.read_text())
        except Exception as e:
            log.warning(f"Failed to load chat history: {e}")
    return {}


def save_chat_history(history: dict):
    """Sync in-memory chat history to disk."""
    try:
        atomic_write_json(CHAT_HISTORY_FILE, history, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f"Failed to save chat history: {e}")


def append_chat_message(history: dict, chat_id: str, role: str, text: str):
    """Append a message to a chat's history, keeping last 50 per chat."""
    if chat_id not in history:
        history[chat_id] = []
    history[chat_id].append(
        {
            "role": role,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    history[chat_id] = history[chat_id][-50:]


def build_chat_context(history: dict, chat_id: str) -> str:
    """Build a conversation context string from recent chat history.

    Rules:
    - Up to 10 messages
    - Up to 24 hours old
    - Always include the last bot message if within 24h
    """
    messages = history.get(chat_id, [])
    if not messages:
        return ""

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    # Find the last bot message (regardless of age)
    last_bot = None
    for m in reversed(messages):
        if m["role"] == "bot":
            last_bot = m
            break

    # Filter to last 24 hours
    recent = []
    for m in messages:
        try:
            ts = datetime.fromisoformat(m["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                recent.append(m)
        except (KeyError, ValueError):
            continue

    # Always include the last bot reply even if older than 24h
    if last_bot and last_bot not in recent:
        recent.insert(0, last_bot)

    if not recent:
        return ""

    # Take last 10
    selected = recent[-10:]

    # Ensure last bot message is included
    if last_bot and last_bot not in selected:
        selected = [last_bot] + selected[-9:]

    lines = []
    for m in selected:
        label = "User" if m["role"] == "user" else "Agent"
        lines.append(f"<message>{label}: {m['text']}</message>")

    return "\n".join(lines)


def fetch_slack_thread_context(
    client, channel: str, event: dict, bot_user_id: str, limit: int = 10
) -> str:
    """Fetch up to *limit* past messages from the Slack thread the user sent in.

    - If the event has a ``thread_ts`` it is a reply inside a thread;
      ``conversations.replies`` is used to fetch that thread's history.
    - Otherwise ``conversations.history`` is used to fetch recent messages in
      the channel/DM (the top-level conversation).

    The current message itself is excluded (it hasn't been written yet).
    Bot/app messages are labelled ``Agent``; human messages are labelled
    ``User``.  Returns an empty string on any API error or when there is no
    prior context.
    """
    thread_ts = event.get("thread_ts")
    current_ts = event.get("ts", "")

    try:
        if thread_ts:
            # Message is part of a thread — fetch the thread replies.
            resp = client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                limit=limit + 1,  # +1 to account for the root message
            )
            raw_messages = resp.get("messages") or []
        else:
            # Top-level DM or channel message — fetch channel history.
            resp = client.conversations_history(
                channel=channel,
                limit=limit + 1,  # +1 so we can drop the current one if present
            )
            raw_messages = resp.get("messages") or []
            # conversations.history returns newest-first; reverse to oldest-first.
            raw_messages = list(reversed(raw_messages))
    except Exception as e:
        log.debug(f"fetch_slack_thread_context API call failed: {e}")
        return ""

    lines = []
    for msg in raw_messages:
        # Skip the current message (it's the one the user just sent).
        if msg.get("ts") == current_ts:
            continue
        msg_text = (msg.get("text") or "").strip()
        if not msg_text:
            continue
        # Determine role: bot/app messages are "Agent", humans are "User".
        if msg.get("bot_id") or msg.get("user") == bot_user_id:
            label = "Agent"
        else:
            label = "User"
        lines.append(f"<message>{label}: {msg_text}</message>")

    # Keep only the last *limit* messages.
    selected = lines[-limit:]
    return "\n".join(selected)


# ---------------------------------------------------------------------------
# Slack API helpers
# ---------------------------------------------------------------------------


def slack_send(client, channel: str, text: str, thread_ts: str | None = None):
    """Send a message to a Slack channel. Returns the API response or None.

    Truncates to Slack's text limit and swallows API errors (logging them) so
    a single failed send never crashes the bridge. When *thread_ts* is given the
    message is posted as a reply in that thread (used to keep the agent's daily
    activity in a single conversation rather than one chat per message).
    """
    if not channel:
        return None
    if len(text) > SLACK_MAX_LEN:
        text = text[: SLACK_MAX_LEN - 3] + "..."
    kwargs = {"channel": channel, "text": text, "mrkdwn": True}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    try:
        return client.chat_postMessage(**kwargs)
    except Exception as e:
        log.warning(f"Slack chat_postMessage to {channel} failed: {e}")
        return None


def get_user_name(client, user_id: str, cache: dict) -> str:
    """Resolve a Slack user id to a handle/display name, cached per process.

    Falls back to the raw user id on any failure (e.g. missing users:read).
    """
    if not user_id:
        return "user"
    if user_id in cache:
        return cache[user_id]
    name = user_id
    try:
        resp = client.users_info(user=user_id)
        user = resp["user"] if isinstance(resp, dict) else resp.get("user", {})
        profile = user.get("profile", {}) if isinstance(user, dict) else {}
        name = (
            user.get("name")
            or profile.get("display_name")
            or profile.get("real_name")
            or user_id
        )
    except Exception as e:
        log.debug(f"users_info({user_id}) failed: {e}")
    cache[user_id] = name
    return name


# ---------------------------------------------------------------------------
# Media download helpers
# ---------------------------------------------------------------------------


def extract_files(event: dict) -> list[dict]:
    """Return the list of file objects attached to a Slack message event."""
    files = event.get("files")
    if isinstance(files, list):
        return [f for f in files if isinstance(f, dict) and f.get("id")]
    return []


def _media_type_for(file_obj: dict) -> str:
    """Map a Slack file's mimetype to our coarse media-type label."""
    mimetype = (file_obj.get("mimetype") or "").lower()
    if mimetype.startswith("image/"):
        return "image"
    if mimetype.startswith("audio/"):
        return "audio"
    return "document"


def download_slack_file(token: str, file_obj: dict, chat_id: str) -> str | None:
    """Download a Slack file to /agent/workspace/slack/{chat_id}/.

    Slack private files require the bot token as a bearer credential.
    Returns the local file path on success, None on failure.
    """
    url = file_obj.get("url_private_download") or file_obj.get("url_private")
    if not url:
        return None

    dest_dir = MEDIA_DIR / chat_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(file_obj.get("name") or f"file_{file_obj['id']}").name
    unique_name = f"{uuid.uuid4().hex[:12]}_{safe_name}"
    dest_path = dest_dir / unique_name

    headers = {"Authorization": f"Bearer {token}"}
    try:
        with requests.get(
            url, headers=headers, timeout=(SEND_TIMEOUT, 120), stream=True
        ) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_MEDIA_BYTES:
                log.warning(
                    f"Slack file {safe_name} too large ({content_length} bytes), skipping"
                )
                return None
            size = 0
            exceeded = False
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    size += len(chunk)
                    if size > MAX_MEDIA_BYTES:
                        log.warning(
                            f"Slack file {safe_name} exceeded "
                            f"{MAX_MEDIA_BYTES} byte limit, aborting"
                        )
                        exceeded = True
                        break
                    f.write(chunk)
                else:
                    f.flush()
                    os.fsync(f.fileno())
        if exceeded:
            dest_path.unlink(missing_ok=True)
            return None
    except Exception as e:
        log.error(f"Failed to download Slack file {safe_name}: {e}")
        dest_path.unlink(missing_ok=True)
        return None

    log.info(f"Saved media: {dest_path} ({size} bytes)")
    return str(dest_path)


# ---------------------------------------------------------------------------
# Command handlers (read-only / quick replies)
# ---------------------------------------------------------------------------


def handle_help_command(client, channel: str, chat_history: dict) -> None:
    """Handle help — list available bot commands."""
    response = (
        "🤖 *Agent — Available Commands*\n\n"
        "`status` — Live agent status (cycle, goals, last heartbeat)\n"
        "`goals [N]` — Show last N goals (default 5, max 20)\n"
        "`cycles [N]` — Show last N completed cycles (default 5, max 20)\n"
        "`journal [N]` — Show last N journal entries (default 3, max 10)\n"
        "`outbox [N]` — Show last N outbox messages, highlights needs_human\n"
        "`services` — Show background service status (running/dead, PID, port)\n"
        "`today` — Summary of last 24h: goals, cycles, blockers\n"
        "`remind <duration> <text>` — Set a reminder (e.g. `remind 30m check deploy`)\n"
        "`note <text>` — Save a quick note (e.g. `note review PR 125 tomorrow`)\n"
        "`notes [query]` — List recent notes or search (e.g. `notes deploy`)\n"
        "`heartbeat` — Trigger an immediate agent cycle\n"
        "`help` — Show this help message\n\n"
        "_Commands work as plain text, slash commands, or `@mention`. "
        "Any other message is queued as a goal for the next heartbeat._"
    )
    slack_send(client, channel, response)
    append_chat_message(chat_history, channel, "bot", response)


def handle_status_command(client, channel: str, chat_history: dict) -> None:
    """Handle status — return live agent status from memory files instantly."""
    try:
        state_path = BASE / "memory" / "state.json"
        cycles_path = BASE / "memory" / "cycles.json"
        goals_path = BASE / "memory" / "goal.json"

        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        goals = json.loads(goals_path.read_text()) if goals_path.exists() else []
        cycles = json.loads(cycles_path.read_text()) if cycles_path.exists() else []

        try:
            from scripts.repair_memory_files import migrate_state_dict

            if isinstance(state, dict):
                migrate_state_dict(state)
        except Exception:
            pass

        status = state.get("agent_status", "unknown")
        cycle_num = state.get("cycle_number", "?")
        last_hb = state.get("last_heartbeat", "unknown")
        summary = state.get("last_cycle_summary", "")

        if isinstance(goals, list):
            active = sum(
                1 for g in goals if g.get("status") in ("pending", "in_progress")
            )
            completed = sum(1 for g in goals if g.get("status") == "completed")
            failed = sum(1 for g in goals if g.get("status") == "failed")
        else:
            active = completed = failed = 0

        total_cycles = len(cycles) if isinstance(cycles, list) else 0

        status_emoji = {"running": "⚙️", "idle": "✅", "waiting_for_human": "⏳"}.get(
            status, "❓"
        )

        hb_display = last_hb[:19] if last_hb and last_hb != "unknown" else "unknown"
        lines = [
            f"{status_emoji} *Agent Status*",
            f"Cycle: #{cycle_num}  |  Status: `{status}`",
            f"Last heartbeat: {escape_slack(hb_display)}",
            f"Goals: {active} active, {completed} done, {failed} failed",
            f"Total cycles: {total_cycles}",
        ]
        if summary:
            lines.append(f"\n_Last: {escape_slack(summary[:200])}_")

        response = "\n".join(lines)
        slack_send(client, channel, response)
        append_chat_message(chat_history, channel, "bot", response)
        log.info("status command served")
    except Exception as e:
        err = f"❌ Failed to read status: {e}"
        slack_send(client, channel, err)
        log.error(f"status error: {e}", exc_info=True)


def handle_goals_command(
    client, channel: str, chat_history: dict, limit: int = 5
) -> None:
    """Handle goals [N] — show the N most recent goals and their status."""
    try:
        goals_path = BASE / "memory" / "goal.json"
        goals = json.loads(goals_path.read_text()) if goals_path.exists() else []

        # goal.json may be a list or an {"active": [...], "archived": [...]} dict
        if isinstance(goals, dict):
            merged = []
            for key in ("active", "archived"):
                bucket = goals.get(key, [])
                if isinstance(bucket, list):
                    merged.extend(bucket)
            goals = merged

        if not isinstance(goals, list) or not goals:
            response = "📭 No goals found."
            slack_send(client, channel, response)
            append_chat_message(chat_history, channel, "bot", response)
            return

        def _sort_key(g):
            return g.get("updated_at") or g.get("created_at") or ""

        recent = sorted(goals, key=_sort_key, reverse=True)[:limit]

        status_emoji = {
            "completed": "✅",
            "failed": "❌",
            "in_progress": "⚙️",
            "pending": "⏳",
        }

        lines = [f"📋 *Last {len(recent)} goal(s)*"]
        for g in recent:
            st = g.get("status", "?")
            emoji = status_emoji.get(st, "❓")
            goal_text = g.get("goal", "untitled")
            if len(goal_text) > 80:
                goal_text = goal_text[:77] + "..."
            lines.append(f"{emoji} `{st}` — {escape_slack(goal_text)}")

        response = "\n".join(lines)
        slack_send(client, channel, response)
        append_chat_message(chat_history, channel, "bot", response)
        log.info(f"goals command served (limit={limit})")
    except Exception as e:
        err = f"❌ Failed to read goals: {e}"
        slack_send(client, channel, err)
        log.error(f"goals error: {e}", exc_info=True)


def handle_journal_command(
    client, channel: str, chat_history: dict, limit: int = 3
) -> None:
    """Handle journal [N] — show the N most recent journal entries."""
    try:
        journal_path = BASE / "memory" / "journal.json"
        entries = json.loads(journal_path.read_text()) if journal_path.exists() else []

        if not isinstance(entries, list) or not entries:
            response = "📭 No journal entries found."
            slack_send(client, channel, response)
            append_chat_message(chat_history, channel, "bot", response)
            return

        sorted_entries = sorted(
            entries, key=lambda e: e.get("timestamp", ""), reverse=True
        )
        recent = sorted_entries[:limit]

        type_emoji = {
            "evolve": "⚙️",
            "goal": "🎯",
            "self-heal": "🔧",
            "self_heal": "🔧",
            "dream": "💤",
        }

        plural = "y" if len(recent) == 1 else "ies"
        lines = [f"📓 *Last {len(recent)} journal entr{plural}*"]
        for e in recent:
            cycle_num = e.get("cycle", "?")
            etype = e.get("type", "unknown")
            emoji = type_emoji.get(etype, "📌")
            category = e.get("category", "")
            cat_label = f" `{category}`" if category else ""
            summary = e.get("summary", "") or e.get("title", "") or ""
            if len(summary) > 120:
                summary = summary[:117] + "..."
            ts = (e.get("timestamp", "") or "")[:10]
            summary_display = escape_slack(summary) if summary else "(no summary)"
            lines.append(
                f"{emoji} *#{cycle_num}*{cat_label} — {summary_display} "
                f"_{escape_slack(ts)}_"
            )

        response = "\n".join(lines)
        slack_send(client, channel, response)
        append_chat_message(chat_history, channel, "bot", response)
        log.info(f"journal command served (limit={limit})")
    except Exception as e:
        err = f"❌ Failed to read journal: {e}"
        slack_send(client, channel, err)
        log.error(f"journal error: {e}", exc_info=True)


def handle_today_command(client, channel: str, chat_history: dict) -> None:
    """Handle today — summary of last 24h: completed goals, cycle count."""
    try:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        cutoff_str = cutoff.isoformat()

        goals_path = BASE / "memory" / "goal.json"
        goals_data = json.loads(goals_path.read_text()) if goals_path.exists() else {}
        all_goals = []
        if isinstance(goals_data, dict):
            for key in ("active", "archived"):
                bucket = goals_data.get(key, [])
                if isinstance(bucket, list):
                    all_goals.extend(bucket)
        elif isinstance(goals_data, list):
            all_goals = goals_data

        completed_today = [
            g
            for g in all_goals
            if g.get("status") == "completed"
            and (g.get("updated_at") or "") >= cutoff_str
        ]

        cycles_path = BASE / "memory" / "cycles.json"
        cycles = json.loads(cycles_path.read_text()) if cycles_path.exists() else []
        try:
            from scripts.repair_memory_files import migrate_cycles_list

            if isinstance(cycles, list):
                migrate_cycles_list(cycles)
        except Exception:
            pass
        cycles_today = [
            c
            for c in cycles
            if c.get("cycle_status") == "completed"
            and (c.get("end_time") or c.get("start_time") or "") >= cutoff_str
        ]
        evolve_today = [c for c in cycles_today if c.get("cycle_type") == "evolve"]
        goal_today = [c for c in cycles_today if c.get("cycle_type") == "goal"]

        lines = [
            f"📅 *Today's Summary* _(last 24h as of "
            f"{escape_slack(now.strftime('%H:%M'))} UTC)_\n"
        ]

        if completed_today:
            lines.append(f"*Goals Completed* — {len(completed_today)}")
            for g in completed_today[-5:]:
                lines.append(f"  🎯 {escape_slack(g.get('goal', '')[:70])}")

        lines.append(f"\n*Cycles* — {len(cycles_today)} total")
        if evolve_today:
            cats = [escape_slack(c.get("category", "?")) for c in evolve_today]
            lines.append(f"  ⚙️ {len(evolve_today)} evolve: {', '.join(cats[:5])}")
        if goal_today:
            lines.append(f"  🎯 {len(goal_today)} goal cycles")

        outbox_path = BASE / "messages" / "outbox.json"
        if outbox_path.exists():
            outbox_data = json.loads(outbox_path.read_text())
            msgs = (
                outbox_data
                if isinstance(outbox_data, list)
                else outbox_data.get("messages", [])
            )
            blocked = [m for m in msgs if m.get("type") == "needs_human"]
            if blocked:
                lines.append(
                    f"\n⚠️ *Blocked* — {len(blocked)} item(s) need human "
                    f"attention (`outbox` for details)"
                )

        response = "\n".join(lines)
        slack_send(client, channel, response)
        append_chat_message(chat_history, channel, "bot", response)
        log.info(
            f"today command served ({len(completed_today)} goals, "
            f"{len(cycles_today)} cycles)"
        )
    except Exception as e:
        err = f"❌ Failed to generate today summary: {e}"
        slack_send(client, channel, err)
        log.error(f"today error: {e}", exc_info=True)


def handle_outbox_command(
    client, channel: str, chat_history: dict, limit: int = 5
) -> None:
    """Handle outbox [N] — show recent outbox messages, highlight needs_human."""
    try:
        outbox_path = BASE / "messages" / "outbox.json"
        messages = json.loads(outbox_path.read_text()) if outbox_path.exists() else []

        if not isinstance(messages, list) or not messages:
            response = "📭 Outbox is empty."
            slack_send(client, channel, response)
            append_chat_message(chat_history, channel, "bot", response)
            return

        sorted_msgs = sorted(
            messages, key=lambda m: m.get("timestamp", ""), reverse=True
        )
        recent = sorted_msgs[:limit]
        needs_human_count = sum(1 for m in messages if m.get("type") == "needs_human")

        type_emoji = {
            "needs_human": "🚨",
            "info": "ℹ️",
            "error": "❌",
            "warning": "⚠️",
            "success": "✅",
        }

        header = f"📤 *Outbox* (last {len(recent)} of {len(messages)})"
        if needs_human_count > 0:
            header += f" — 🚨 *{needs_human_count} needs human*"
        lines = [header]

        for m in recent:
            mtype = m.get("type", "info")
            emoji = type_emoji.get(mtype, "📩")
            subject = m.get("subject", "") or ""
            content = m.get("content", "") or ""
            ts = (m.get("timestamp", "") or "")[:16].replace("T", " ")
            display = subject if subject else content
            if len(display) > 100:
                display = display[:97] + "..."
            lines.append(
                f"{emoji} `{mtype}` — {escape_slack(display)} _{escape_slack(ts)}_"
            )

        response = "\n".join(lines)
        slack_send(client, channel, response)
        append_chat_message(chat_history, channel, "bot", response)
        log.info(
            f"outbox command served (limit={limit}, needs_human={needs_human_count})"
        )
    except Exception as e:
        err = f"❌ Failed to read outbox: {e}"
        slack_send(client, channel, err)
        log.error(f"outbox error: {e}", exc_info=True)


def handle_cycles_command(
    client, channel: str, chat_history: dict, limit: int = 5
) -> None:
    """Handle cycles [N] — show recent completed cycles with type/duration."""
    try:
        cycles_path = BASE / "memory" / "cycles.json"
        cycles = json.loads(cycles_path.read_text()) if cycles_path.exists() else []

        if not isinstance(cycles, list) or not cycles:
            response = "📊 No cycle history found."
            slack_send(client, channel, response)
            append_chat_message(chat_history, channel, "bot", response)
            return

        try:
            from scripts.repair_memory_files import migrate_cycles_list

            migrate_cycles_list(cycles)
        except Exception:
            pass

        completed = [c for c in cycles if c.get("cycle_status") == "completed"]
        recent = completed[-limit:][::-1]
        total = len(completed)

        type_emoji = {
            "evolve": "🔧",
            "goal": "🎯",
            "self-heal": "🩺",
            "dream": "💤",
            "unknown": "❓",
        }

        lines = [f"📊 *Recent Cycles* (last {len(recent)} of {total} completed)"]
        for c in recent:
            num = c.get("cycle_number", "?")
            ctype = c.get("cycle_type", "unknown")
            cat = c.get("cycle_category", "")
            dur = c.get("duration_seconds")
            full_summary = c.get("summary", "") or ""
            summary = full_summary[:120]
            if len(full_summary) > 120:
                summary += "..."

            emoji = type_emoji.get(ctype, "❓")
            dur_str = f"{dur}s" if dur is not None else "?"
            label = f"{ctype}/{cat}" if cat else ctype

            lines.append(f"{emoji} *#{num}* `{escape_slack(label)}` — {dur_str}")
            if summary:
                lines.append(f"   _{escape_slack(summary)}_")

        response = "\n".join(lines)
        slack_send(client, channel, response)
        append_chat_message(chat_history, channel, "bot", response)
        log.info(f"cycles command served (limit={limit})")
    except Exception as e:
        err = f"❌ Failed to read cycles: {e}"
        slack_send(client, channel, err)
        log.error(f"cycles error: {e}", exc_info=True)


def handle_services_command(client, channel: str, chat_history: dict) -> None:
    """Handle services — show background service status from services.json."""
    try:
        services_path = BASE / "memory" / "services.json"
        services = (
            json.loads(services_path.read_text()) if services_path.exists() else {}
        )

        if not services:
            response = "⚙️ *Services*\n\nNo services registered."
        else:
            lines = ["⚙️ *Services*\n"]
            for name, svc in services.items():
                status = svc.get("status", "unknown")
                pid = svc.get("pid")
                port = svc.get("port")
                started = svc.get("started_at", "")

                if pid and status == "running":
                    try:
                        os.kill(int(pid), 0)
                        alive = True
                    except (ProcessLookupError, PermissionError):
                        alive = False
                    status_icon = "🟢" if alive else "🔴"
                    status_label = "running" if alive else "dead (stale PID)"
                else:
                    status_icon = "⚫"
                    status_label = status

                meta = []
                if port:
                    meta.append(f"port {port}")
                if pid:
                    meta.append(f"PID {pid}")
                if started:
                    meta.append(f"since {started[:10]}")
                meta_str = f" _({escape_slack(', '.join(meta))})_" if meta else ""

                lines.append(
                    f"{status_icon} `{name}` — {escape_slack(status_label)}{meta_str}"
                )

            response = "\n".join(lines)

        slack_send(client, channel, response)
        append_chat_message(chat_history, channel, "bot", response)
        log.info("services command served")
    except Exception as e:
        err = f"❌ Failed to read services: {e}"
        slack_send(client, channel, err)
        log.error(f"services error: {e}", exc_info=True)


def handle_remind_command(
    client, channel: str, chat_history: dict, args: list[str]
) -> None:
    """Handle remind <duration> <text> — create a one-time reminder."""
    if len(args) < 2:
        usage = (
            "Usage: `remind <duration> <message>`\n"
            "Duration: `30m`, `2h`, `1d`\n"
            "Example: `remind 30m check deployment`"
        )
        slack_send(client, channel, usage)
        append_chat_message(chat_history, channel, "bot", usage)
        return

    duration_str = args[0].lower()
    reminder_text = " ".join(args[1:])

    try:
        if duration_str.endswith("d"):
            minutes = int(duration_str[:-1]) * 1440
        elif duration_str.endswith("h"):
            minutes = int(duration_str[:-1]) * 60
        elif duration_str.endswith("m"):
            minutes = int(duration_str[:-1])
        else:
            raise ValueError("unknown unit")
        if minutes <= 0:
            raise ValueError("non-positive")
    except (ValueError, IndexError):
        err = (
            f"Invalid duration `{escape_slack(duration_str)}`. "
            "Use formats like `30m`, `2h`, `1d`."
        )
        slack_send(client, channel, err)
        append_chat_message(chat_history, channel, "bot", err)
        return

    fire_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M"
    )

    try:
        from scripts.reminder import add_reminder

        rid = add_reminder(reminder_text, at=fire_at)
    except Exception as e:
        rid = None
        log.error(f"remind error: add_reminder raised {e}")

    if rid:
        if minutes >= 1440 and minutes % 1440 == 0:
            label = f"{minutes // 1440}d"
        elif minutes >= 60 and minutes % 60 == 0:
            label = f"{minutes // 60}h"
        else:
            label = f"{minutes}m"
        response = (
            f"Reminder set for *{escape_slack(label)}* from now: "
            f"_{escape_slack(reminder_text)}_"
        )
        log.info(f"remind command: {reminder_text!r} in {label}")
    else:
        response = "Failed to create reminder."

    slack_send(client, channel, response)
    append_chat_message(chat_history, channel, "bot", response)


def handle_note_command(
    client, channel: str, chat_history: dict, args: list[str]
) -> None:
    """Handle note <text> — save a quick note."""
    if not args:
        usage = "Usage: `note <text>`\nExample: `note review PR 125 tomorrow`"
        slack_send(client, channel, usage)
        append_chat_message(chat_history, channel, "bot", usage)
        return

    content = " ".join(args)
    title = content[:60].rstrip() + ("..." if len(content) > 60 else "")

    try:
        from scripts.notes import add_note

        note_id = add_note(title, content, tags=["slack"])
    except Exception as e:
        note_id = None
        log.error(f"note error: add_note raised {e}")

    if note_id:
        response = f"Note saved: _{escape_slack(title)}_"
        log.info(f"note command: {title!r}")
    else:
        response = "Failed to save note."

    slack_send(client, channel, response)
    append_chat_message(chat_history, channel, "bot", response)


def handle_notes_command(
    client, channel: str, chat_history: dict, args: list[str]
) -> None:
    """Handle notes [query] — list recent notes or search by keyword."""
    try:
        from scripts.notes import list_notes, search_notes
    except Exception as e:
        err = f"❌ Failed to load notes: {e}"
        slack_send(client, channel, err)
        return

    if args:
        query = " ".join(args)
        notes = search_notes(query)
        header = f"Notes matching _{escape_slack(query)}_:"
    else:
        notes = list_notes()
        header = "Recent notes:"

    if not notes:
        response = (
            "No notes found."
            if not args
            else f"No notes matching _{escape_slack(' '.join(args))}_."
        )
        slack_send(client, channel, response)
        append_chat_message(chat_history, channel, "bot", response)
        return

    shown = notes[:5]
    lines = [header, ""]
    for n in shown:
        title = n.get("title", "Untitled")
        content = n.get("content", "")
        tags = n.get("tags", [])
        preview = content[:80].replace("\n", " ")
        if len(content) > 80:
            preview += "..."
        tag_str = ""
        if tags:
            tag_joined = " ".join(f"#{escape_slack(t)}" for t in tags)
            tag_str = f" _{tag_joined}_"
        lines.append(f"• *{escape_slack(title)}*{tag_str}")
        if preview and preview != title:
            lines.append(f"  {escape_slack(preview)}")

    if len(notes) > 5:
        lines.append(f"\n_…and {len(notes) - 5} more_")

    response = "\n".join(lines)
    slack_send(client, channel, response)
    append_chat_message(chat_history, channel, "bot", response)
    log.info(f"notes command served (count={len(shown)})")


def handle_heartbeat_command(
    client, channel: str, chat_history: dict, extra_args: list[str] = None
) -> None:
    """Handle heartbeat — trigger an immediate agent cycle."""
    try:
        cmd_args = extra_args or []
        cmd = ["bash", "/agent/heartbeat.sh"] + cmd_args
        log.info(f"Heartbeat command received with args: {cmd_args or '(none)'}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd="/agent",
        )

        if result.returncode == 0:
            response = (
                "✅ Heartbeat triggered successfully!\n\n"
                "The agent cycle is now running."
            )
            log.info("Heartbeat completed successfully")
        else:
            response = (
                f"⚠️ Heartbeat triggered but returned non-zero exit code: "
                f"{result.returncode}\n\nCheck logs for details."
            )
            log.warning(
                f"Heartbeat failed with code {result.returncode}: {result.stderr}"
            )

        slack_send(client, channel, response)
        append_chat_message(chat_history, channel, "bot", response)
    except subprocess.TimeoutExpired:
        response = (
            "⏱️ Heartbeat timed out after 5 minutes. The cycle may still be running."
        )
        log.error("Heartbeat timeout")
        slack_send(client, channel, response)
        append_chat_message(chat_history, channel, "bot", response)
    except Exception as e:
        response = f"❌ Failed to trigger heartbeat: {e}"
        log.error(f"Heartbeat command failed: {e}", exc_info=True)
        slack_send(client, channel, response)
        append_chat_message(chat_history, channel, "bot", response)


# Whitelist allowed heartbeat flags to prevent arbitrary arg injection.
ALLOWED_HEARTBEAT_ARGS = {"--agent-sleep"}


def dispatch_command(
    client, channel: str, command: str, args: list[str], chat_history: dict
) -> None:
    """Route a parsed command to its handler. *command* has no leading slash."""
    if command in ("help", "start"):
        handle_help_command(client, channel, chat_history)
    elif command == "status":
        handle_status_command(client, channel, chat_history)
    elif command == "goals":
        limit = 5
        if args and args[0].isdigit():
            limit = max(1, min(int(args[0]), 20))
        handle_goals_command(client, channel, chat_history, limit)
    elif command == "journal":
        limit = 3
        if args and args[0].isdigit():
            limit = max(1, min(int(args[0]), 10))
        handle_journal_command(client, channel, chat_history, limit)
    elif command == "outbox":
        limit = 5
        if args and args[0].isdigit():
            limit = max(1, min(int(args[0]), 20))
        handle_outbox_command(client, channel, chat_history, limit)
    elif command == "cycles":
        limit = 5
        if args and args[0].isdigit():
            limit = max(1, min(int(args[0]), 20))
        handle_cycles_command(client, channel, chat_history, limit)
    elif command == "services":
        handle_services_command(client, channel, chat_history)
    elif command == "today":
        handle_today_command(client, channel, chat_history)
    elif command == "remind":
        handle_remind_command(client, channel, chat_history, args)
    elif command == "note":
        handle_note_command(client, channel, chat_history, args)
    elif command == "notes":
        handle_notes_command(client, channel, chat_history, args)
    elif command == "heartbeat":
        extra = [a for a in args if a in ALLOWED_HEARTBEAT_ARGS]
        rejected = [a for a in args if a not in ALLOWED_HEARTBEAT_ARGS]
        if rejected:
            log.warning(f"Rejected heartbeat args: {rejected}")
        handle_heartbeat_command(client, channel, chat_history, extra)


# ---------------------------------------------------------------------------
# Incoming messages (Slack → inbox.json)
# ---------------------------------------------------------------------------


def build_inbox_content(
    username: str, text: str, attachments: list[dict], context: str
) -> str:
    """Assemble the inbox `content` string for an incoming Slack message."""
    base_content = f"[Slack @{username}]: {text}" if text else f"[Slack @{username}]:"
    if attachments:
        attachment_lines = "\n".join(f"- {a['type']}: {a['path']}" for a in attachments)
        base_content += f"\n[Attachments]\n{attachment_lines}"
    if context:
        return (
            "[Previous conversation context]\n"
            f"{context}\n[End of context]\n\n[New message]\n{base_content}"
        )
    return base_content


def _event_key(channel: str, event: dict) -> str:
    """Stable dedup key for an incoming Slack event (channel + ts)."""
    ts = event.get("ts") or event.get("event_ts") or event.get("client_msg_id") or ""
    return f"{channel}:{ts}"


def _is_skippable_message(event: dict, bot_user_id: str) -> bool:
    """Return True for events we must never treat as user input.

    Skips the bot's own messages, other bots, and message-edit/delete/system
    subtypes. ``file_share`` is allowed through (it carries user files + text).
    """
    if event.get("bot_id"):
        return True
    if bot_user_id and event.get("user") == bot_user_id:
        return True
    subtype = event.get("subtype")
    if subtype and subtype not in ("file_share",):
        return True
    return False


def _is_dm_channel(channel: str, channel_type: str) -> bool:
    """Return True for a Slack direct-message channel.

    ``app_mention`` events do not carry ``channel_type``, so the channel-id
    prefix (Slack DM channel ids start with ``D``) is the reliable signal and
    is checked first; ``channel_type == "im"`` is kept as a fallback.
    """
    return channel.startswith("D") or channel_type == "im"


def _mark_processed(state: dict, channel: str, event: dict) -> None:
    """Record an event's dedup key (bounded) so redelivery is ignored."""
    key = _event_key(channel, event)
    if key not in state["processed_keys"]:
        state["processed_keys"].append(key)
        state["processed_keys"] = state["processed_keys"][-MAX_PROCESSED_KEYS:]


def _decide_authorization(
    ctx: dict, channel: str, user_id: str, username: str, raw_text: str, is_dm: bool
) -> tuple[bool, str | None, list[tuple[str, str]]]:
    """Apply the owner auto-discovery auth model to an incoming message.

    Mutates ``ctx['chat_ids']`` / owner fields under the lock and returns
    ``(authorized, welcome_message, keepass_writes)``. KeePass persistence is
    returned (not performed) so the caller can run it outside the lock.

    Shared by the message/app_mention path and the Assistant path.
    """
    keepass_writes: list[tuple[str, str]] = []
    with ctx["lock"]:
        chat_ids = ctx["chat_ids"]
        if channel in chat_ids:
            return True, None, []
        if not chat_ids and is_dm:
            # First-ever DM: auto-accept the sender as the owner.
            ctx["owner_user_id"] = user_id
            ctx["owner_username"] = username
            chat_ids.append(channel)
            keepass_writes = [
                (KEEPASS_SLACK_OWNER_USERID, user_id),
                (KEEPASS_SLACK_OWNER_USERNAME, username),
                (KEEPASS_SLACK_CHAT_ID, serialize_chat_ids(chat_ids)),
            ]
            log.info(f"Auto-discovered owner: @{username} (user={user_id})")
            return True, None, keepass_writes
        if _mentions_owner(raw_text, ctx):
            # A new channel/DM where the owner is referenced → authorize it.
            chat_ids.append(channel)
            keepass_writes = [(KEEPASS_SLACK_CHAT_ID, serialize_chat_ids(chat_ids))]
            log.info(f"Authorized new chat {channel} (referenced owner)")
            welcome = (
                "Welcome! You've been authorized. "
                "I'll forward messages to you from now on."
            )
            return True, welcome, keepass_writes
    return False, None, []


def _ingest_message(
    ctx: dict,
    client,
    channel: str,
    username: str,
    user_id: str,
    text: str,
    attachments: list[dict],
    event: dict,
) -> bool:
    """Write an authorized incoming message to inbox.json (+ history/chat).

    Marks the event processed only on a successful inbox write, so a failed
    write stays eligible for Socket Mode redelivery. Returns True on success.
    Shared by the message/app_mention path and the Assistant path.

    Context is fetched live from the Slack thread the user sent their message
    in (up to 10 past messages) instead of from the local in-memory history.
    """
    # Fetch thread context from Slack API (outside the lock — network call).
    context = fetch_slack_thread_context(client, channel, event, ctx["bot_user_id"])

    now_iso = datetime.now(timezone.utc).isoformat()
    with ctx["lock"]:
        chat_history = ctx["chat_history"]
        content = build_inbox_content(username, text, attachments, context)
        # role is an identity label only (no permission gating): the owner is
        # whoever was auto-discovered first; everyone else is a member.
        role = "owner" if user_id and user_id == ctx.get("owner_user_id") else "member"
        # source="slack" (origin); transport="polling_script" — the slack bridge
        # polls Slack and writes the message into the inbox.
        from_obj = make_from(
            "slack",
            transport="polling_script",
            channel=channel,
            user_id=user_id,
            handle=username,
            role=role,
        )
        inbox_item = {
            "type": "message",
            "content": content,
            "timestamp": now_iso,
            "received_at": now_iso,
            # source now lives in from.source (envelope normalization).
            "from": from_obj,
        }
        # Stamp a stable id so the agent can reply to this exact message via
        # the outbox `in_reply_to` field and the bridge can route it back.
        msg_id = ensure_id(inbox_item)
        if attachments:
            inbox_item["attachments"] = attachments

        if write_to_inbox([inbox_item]):
            append_to_history([inbox_item], INBOX_HISTORY_FILE)
            append_chat_message(chat_history, channel, "user", text or "(media)")
            # Record id -> origin so an outbox `in_reply_to` resolves back to
            # this user's channel. origin_ref is used as thread_ts when sending
            # the agent's reply, so it must be the THREAD ROOT ts (thread_ts),
            # not the individual reply ts. For top-level messages thread_ts is
            # absent, so we fall back to ts (which IS the thread root).
            record_origin(
                ctx["state"].setdefault("origin_map", {}),
                msg_id=msg_id,
                from_obj=from_obj,
                origin_ref=event.get("thread_ts") or event.get("ts"),
            )
            _mark_processed(ctx["state"], channel, event)
            save_state(ctx["state"])
            save_chat_history(chat_history)
            log.info(
                f"Received from @{username}: {(text or '(media)')[:100]}"
                + (f" (+{len(attachments)} attachment(s))" if attachments else "")
            )
            return True
        # Do NOT mark processed — leave the event eligible for retry.
        log.error("Inbox write failed — not marking processed, will retry")
        return False


def _download_attachments(ctx: dict, channel: str, event: dict) -> list[dict]:
    """Download any files on *event* to local paths (network, no lock)."""
    attachments = []
    for file_obj in extract_files(event):
        local_path = download_slack_file(ctx["bot_token"], file_obj, channel)
        if local_path:
            attachments.append(
                {
                    "type": _media_type_for(file_obj),
                    "path": local_path,
                    "filename": Path(local_path).name,
                }
            )
    return attachments


def _process_incoming(client, event, ctx, is_mention=False) -> None:
    """Handle one incoming Slack event end-to-end (auth, commands, inbox write).

    The Socket Mode WebSocket runs this on its own thread while the main loop
    polls the outbox. ``ctx['lock']`` guards only the quick shared-state reads
    and mutations (dedup set, chat_ids, chat_history, persisted state); all
    blocking I/O — ``users_info``, KeePass writes, file downloads, command
    handlers (``/heartbeat`` shells out for up to 5 min) and ``slack_send`` —
    runs OUTSIDE the lock so a slow operation never stalls the main loop's
    heartbeat and trips the service watchdog.
    """
    lock = ctx["lock"]
    bot_user_id = ctx["bot_user_id"]

    if not is_mention and _is_skippable_message(event, bot_user_id):
        return

    channel = event.get("channel", "")
    user_id = event.get("user", "")
    if not channel or not user_id:
        return
    if user_id == bot_user_id:
        return

    raw_text = (event.get("text") or "").strip()
    text = strip_bot_mention(raw_text, bot_user_id)
    channel_type = event.get("channel_type", "")
    is_dm = _is_dm_channel(channel, channel_type)

    # --- Assistant-thread gate ---
    # Messages inside a DM thread (thread_ts is set) come from the Agents &
    # AI Apps assistant pane or are replies to an existing thread.  Both cases
    # are handled exclusively by the @assistant.user_message middleware, which
    # provides the `say` helper that replies *in-thread* so the ACK is visible
    # to the user.  If we let _process_incoming race ahead and mark the event
    # processed, the dedup check in _handle_assistant_message fires and skips
    # the in-thread reply — the ACK then lands as a top-level DM, invisible
    # inside the assistant pane.
    if is_dm and event.get("thread_ts"):
        return

    # --- Channel privacy gate ---
    # In channels/groups we only act when the bot is addressed: an explicit
    # @mention (app_mention, or the bot's id appearing in a message event) or
    # a reference to the owner (used to authorize a new channel). This mirrors
    # Telegram's group privacy mode and prevents every channel message from
    # being forwarded to the agent inbox. DMs are always processed.
    if not is_dm and not is_mention:
        bot_mentioned = bool(bot_user_id) and (f"<@{bot_user_id}>" in raw_text)
        if not bot_mentioned and not _mentions_owner(raw_text, ctx):
            return

    # Fast, network-free dedup pre-check.
    with lock:
        if _event_key(channel, event) in ctx["state"]["processed_keys"]:
            return

    # Resolve the display name (network) outside the lock.
    username = get_user_name(client, user_id, ctx["user_name_cache"])

    # --- Authorization decision (owner auto-discovery, WhatsApp-style) ---
    # Mutate chat_ids under the lock; collect slow side effects to run after.
    authorized, welcome, keepass_writes = _decide_authorization(
        ctx, channel, user_id, username, raw_text, is_dm
    )

    # Persist credentials (slow, best-effort) outside the lock.
    for title, value in keepass_writes:
        keepass_store(title, "slack", value, group="System")

    if not authorized:
        log.info(f"Rejected message from @{username} (channel={channel})")
        slack_send(
            client,
            channel,
            "Sorry, I don't know you. Please @mention my owner to get access.",
        )
        with lock:
            _mark_processed(ctx["state"], channel, event)
            save_state(ctx["state"])
        return

    if welcome:
        slack_send(client, channel, welcome)
        with lock:
            append_chat_message(ctx["chat_history"], channel, "bot", welcome)
            _mark_processed(ctx["state"], channel, event)
            save_state(ctx["state"])
        # The triggering message was the authorization handshake (@mention of the
        # owner), not a real request — skip forwarding it to inbox.json.
        return

    # --- Command handling (both slash and keyword/mention text) ---
    # Mark processed first (we have committed to running the command — a long
    # /heartbeat must not be re-triggered by a redelivery), then dispatch
    # OUTSIDE the lock so the command's network/subprocess I/O never blocks
    # the main loop.
    command, args = parse_command(text)
    if command:
        with lock:
            _mark_processed(ctx["state"], channel, event)
            save_state(ctx["state"])
        dispatch_command(client, channel, command, args, ctx["chat_history"])
        with lock:
            save_chat_history(ctx["chat_history"])
        return

    # --- Regular message → inbox.json ---
    attachments = _download_attachments(ctx, channel, event)

    if not text and not attachments:
        with lock:
            _mark_processed(ctx["state"], channel, event)
            save_state(ctx["state"])
        return

    wrote = _ingest_message(
        ctx, client, channel, username, user_id, text, attachments, event
    )

    # ACK the message to confirm receipt.
    # - DMs: thread under the owner's message ts; also track it in outbox_threads
    #   so future agent outbox replies appear as replies to the same conversation.
    # - Channels: thread under the existing thread root (if the @mention arrived
    #   inside a thread) or under the message itself (top-level @mention). This
    #   keeps the ACK scoped to the thread where the user tagged the bot, without
    #   cluttering the channel's main timeline.
    if wrote:
        msg_ts = event.get("ts")
        ack_thread_ts = event.get("thread_ts") or msg_ts
        ack = build_ack_message()
        slack_send(client, channel, ack, thread_ts=ack_thread_ts)
        with lock:
            if is_dm and msg_ts:
                # Only track the thread root for DMs — outbox_threads drives
                # owner-DM threading; channel replies use origin_map instead.
                ctx["state"]["outbox_threads"][channel] = msg_ts
            append_chat_message(ctx["chat_history"], channel, "bot", ack)
            save_chat_history(ctx["chat_history"])
            save_state(ctx["state"])


def _mentions_owner(raw_text: str, ctx: dict) -> bool:
    """True if *raw_text* references the owner by @mention or by username."""
    owner_user_id = ctx.get("owner_user_id")
    owner_username = ctx.get("owner_username")
    if not raw_text:
        return False
    if owner_user_id and f"<@{owner_user_id}>" in raw_text:
        return True
    if owner_username and contains_username(raw_text, owner_username):
        return True
    return False


# ---------------------------------------------------------------------------
# Outgoing messages (outbox.json → Slack)
# ---------------------------------------------------------------------------


def _format_outbox_msg(msg: dict) -> str:
    """Convert an outbox dict to a readable Slack mrkdwn message."""
    msg_type = msg.get("type", "")
    subject = msg.get("subject", "")
    content = msg.get("content", "")

    lines = []
    type_badges = {
        "needs_human": "🚨 *ACTION REQUIRED*",
        "goal_complete": "✅ *Goal Completed*",
        "goal_failed": "❌ *Goal Failed*",
    }
    if msg_type in type_badges:
        lines.append(type_badges[msg_type])
    if subject:
        lines.append(f"*{escape_slack(subject)}*")
    if content:
        lines.append(escape_slack(content))

    return "\n".join(lines) if lines else escape_slack(json.dumps(msg, indent=2))


def _owner_channel(origin_map: dict, chat_ids: list[str]) -> str | None:
    """Resolve the owner's delivery channel for unaddressed messages.

    Prefers a channel recorded with ``role == "owner"`` in the origin map
    (set once the owner has messaged since this feature shipped); falls back
    to the first authorized chat id (the owner's DM by auto-discovery order).

    *origin_map* must be a snapshot taken under ``ctx["lock"]`` — never the live
    ``ctx["state"]["origin_map"]`` dict, which the Socket Mode thread mutates
    concurrently (iterating it live races with ``record_origin``).
    """
    for entry in reversed(list(origin_map.values())):
        frm = (entry or {}).get("from") or {}
        if frm.get("role") == "owner" and frm.get("channel"):
            return frm["channel"]
    return chat_ids[0] if chat_ids else None


def _resolve_recipient(origin_map: dict, target: str):
    """Resolve an outbox target (``in_reply_to`` id or ``to`` handle).

    Returns ``(channel, thread_ts)`` for delivery, or ``None`` when this bridge
    cannot resolve it (the message belongs to another transport → caller skips
    without marking it sent).
    """
    resolved = resolve_origin(origin_map, target)
    if resolved is None:
        frm = resolve_handle(origin_map, target)
        if frm is None:
            return None
        resolved = (frm, None)
    from_obj, origin_ref = resolved
    channel = (from_obj or {}).get("channel")
    if not channel:
        return None
    return channel, origin_ref


def send_outbox_messages(client, ctx: dict) -> None:
    """Forward unsent outbox messages to Slack with per-recipient routing.

    Runs on the main loop. Snapshots the dedup set + routing state under the
    lock, performs the network sends UNLOCKED (so a slow Slack API call never
    blocks the incoming-event handler thread), then re-acquires the lock to
    record the sent keys and chat history.

    Each message follows the 3-way delivery rule:
      1. Addressed (``in_reply_to``/``to``) and resolvable here → deliver to
         that one user's channel, threaded under their original message.
      2. Addressed but NOT resolvable here → skip WITHOUT marking sent (it
         belongs to another transport; unique ids never false-match).
      3. Unaddressed (status/FYI) → deliver to the owner only, unless a
         configured redirect channel is set.
    """
    from shared import read_outbox_locked

    outbox = read_outbox_locked()
    if not outbox:
        return

    lock = ctx["lock"]
    with lock:
        chat_ids = list(ctx["chat_ids"])
        already_sent = set(ctx["state"]["sent_hashes"])
        # Copy the routing state so we can read it while unlocked.
        outbox_threads = dict(ctx["state"].get("outbox_threads", {}))
        origin_map = dict(ctx["state"].get("origin_map", {}))

    # Use the locked snapshot (above), never the live state dict.
    owner_channel = _owner_channel(origin_map, chat_ids)
    # Redirect target for unaddressed messages, resolved once at startup.
    unaddressed_channel = ctx.get("unaddressed_channel") or owner_channel

    # Send outside the lock.
    results: list[tuple[dict, str, list[str], str]] = []
    for msg in outbox:
        key = dedup_key(msg)
        if key in already_sent:
            continue

        # Recipient from the structured `to` (to.in_reply_to / to.handle), with
        # legacy fallback to top-level in_reply_to or a bare-string `to`.
        target = reply_target(msg)
        if target:
            recipient = _resolve_recipient(origin_map, str(target))
            if recipient is None:
                # Case 2: addressed to another transport — leave for that
                # bridge; do NOT mark sent.
                continue
            destination, thread_ts = recipient
        elif unaddressed_channel:
            # Case 3: unaddressed → owner (or configured redirect channel),
            # threaded under that channel's current outbox root if any.
            destination = unaddressed_channel
            thread_ts = outbox_threads.get(destination)
        else:
            continue  # no owner channel known yet

        text = _format_outbox_msg(msg)
        resp = slack_send(client, destination, text, thread_ts=thread_ts)
        if resp:
            results.append((msg, key, [destination], text))
            log.info(
                f"Sent to Slack channel {destination}: "
                f"{msg.get('subject', text[:60])!r}"
            )

    if not results:
        return

    with lock:
        state = ctx["state"]
        chat_history = ctx["chat_history"]
        for msg, key, succeeded, text in results:
            if key in state["sent_hashes"]:
                continue  # another path recorded it while we were sending
            state["sent_hashes"].append(key)
            for cid in succeeded:
                append_chat_message(chat_history, cid, "bot", text)
        state["sent_hashes"] = state["sent_hashes"][-1000:]
        append_to_history([r[0] for r in results], OUTBOX_HISTORY_FILE)
        save_chat_history(chat_history)
        save_state(state)

    log.info(f"Forwarded {len(results)} outbox message(s) to Slack.")


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------


def register_handlers(app, ctx) -> None:
    """Wire Slack Bolt event/command listeners onto *app*."""

    @app.event("message")
    def _on_message(event, client):
        try:
            _process_incoming(client, event, ctx, is_mention=False)
        except Exception as e:
            log.error(f"Error handling message event: {e}", exc_info=True)

    @app.event("app_mention")
    def _on_app_mention(event, client):
        try:
            _process_incoming(client, event, ctx, is_mention=True)
        except Exception as e:
            log.error(f"Error handling app_mention event: {e}", exc_info=True)

    def _make_slash_handler(command_name):
        def _handler(ack, command, client):
            ack()
            try:
                channel = command.get("channel_id", "")
                user_id = command.get("user_id", "")
                args = (command.get("text") or "").split()
                # Honor the same owner-discovery auth model as messages: only
                # the owner, or an already-authorized channel, may run commands.
                # We deliberately do NOT add the invoking channel to chat_ids —
                # that would silently turn it into an outbox broadcast target and
                # leak agent notifications (incl. needs_human) to arbitrary
                # channels where a workspace member typed a slash command.
                with ctx["lock"]:
                    owner_id = ctx.get("owner_user_id")
                    authorized = channel in ctx["chat_ids"] or (
                        bool(owner_id) and user_id == owner_id
                    )
                if not authorized:
                    slack_send(
                        client,
                        channel,
                        "Please DM me first to get set up before using "
                        "commands here.",
                    )
                    return
                dispatch_command(
                    client, channel, command_name, args, ctx["chat_history"]
                )
                with ctx["lock"]:
                    save_chat_history(ctx["chat_history"])
            except Exception as e:
                log.error(f"Error handling /{command_name}: {e}", exc_info=True)

        return _handler

    for name in COMMANDS:
        if name == "start":
            continue  # /start is not a Slack-registrable command name
        app.command(f"/{name}")(_make_slash_handler(name))


# ---------------------------------------------------------------------------
# Assistant (Agents & AI Apps) — the AI-assistant container UX
# ---------------------------------------------------------------------------

# Suggested-prompt chips shown when the assistant thread opens. Each maps to a
# command or a goal-style question the agent can act on.
ASSISTANT_SUGGESTED_PROMPTS = [
    "status",
    "today",
    "what are you working on right now?",
    "help",
]


class _AssistantSayClient:
    """Adapter so the existing command handlers reply inside the assistant thread.

    The handlers call ``slack_send(client, channel, text)`` →
    ``client.chat_postMessage(...)``. In an assistant thread replies must be
    threaded; the Assistant's ``say`` does that automatically, so we route
    ``chat_postMessage`` through ``say`` and reuse every handler unchanged.
    """

    def __init__(self, say):
        self._say = say

    def chat_postMessage(self, channel=None, text="", mrkdwn=True, thread_ts=None):
        # Assistant ``say`` is already bound to the user's thread; ignore any
        # thread_ts (command replies never pass one anyway).
        return self._say(text)


def _handle_assistant_message(client, payload, ctx, say, set_status, set_title) -> None:
    """Handle one message inside an assistant thread (always a 1:1 DM).

    Commands are answered instantly (threaded via ``say``); any other text is
    queued as a goal — we show a 'thinking' status and ack immediately, and the
    agent's real answer arrives later via the shared outbox (per the chosen
    immediate-ack model).

    Input is pre-filtered by Bolt's ``@assistant.user_message`` matcher
    (``is_user_message_event_in_assistant_thread``) to non-bot ``im`` thread
    messages with subtype None/``file_share`` — which is why, unlike
    ``_process_incoming``, this handler omits ``_is_skippable_message`` and only
    re-checks ``user_id == bot_user_id``. Do not remove that implicit dependency.
    """
    channel = payload.get("channel", "")
    user_id = payload.get("user", "")
    if not channel or not user_id or user_id == ctx["bot_user_id"]:
        return

    raw_text = (payload.get("text") or "").strip()
    text = strip_bot_mention(raw_text, ctx["bot_user_id"])

    with ctx["lock"]:
        if _event_key(channel, payload) in ctx["state"]["processed_keys"]:
            return

    username = get_user_name(client, user_id, ctx["user_name_cache"])

    # Assistant threads are 1:1 DMs → owner auto-discovery applies.
    authorized, welcome, keepass_writes = _decide_authorization(
        ctx, channel, user_id, username, raw_text, is_dm=True
    )
    for title, value in keepass_writes:
        keepass_store(title, "slack", value, group="System")

    if not authorized:
        say("Sorry, I don't know you. Please @mention my owner to get access.")
        with ctx["lock"]:
            _mark_processed(ctx["state"], channel, payload)
            save_state(ctx["state"])
        return

    if welcome:
        say(welcome)
        with ctx["lock"]:
            append_chat_message(ctx["chat_history"], channel, "bot", welcome)
            _mark_processed(ctx["state"], channel, payload)
            save_state(ctx["state"])
        # The triggering message was the authorization handshake (@mention of the
        # owner), not a real request — skip forwarding it to inbox.json.
        return

    # --- Commands answer instantly, in-thread ---
    command, args = parse_command(text)
    if command:
        with ctx["lock"]:
            _mark_processed(ctx["state"], channel, payload)
            save_state(ctx["state"])
        _safe_set_status(set_status, "on it…")
        dispatch_command(
            _AssistantSayClient(say), channel, command, args, ctx["chat_history"]
        )
        with ctx["lock"]:
            save_chat_history(ctx["chat_history"])
        return

    # --- Free text → goal for the agent ---
    if text:
        _safe_set_title(set_title, text[:80])
    _safe_set_status(set_status, "is thinking…")

    attachments = _download_attachments(ctx, channel, payload)
    if not text and not attachments:
        say("I didn't catch a message there — mind trying again?")
        with ctx["lock"]:
            _mark_processed(ctx["state"], channel, payload)
            save_state(ctx["state"])
        return

    if _ingest_message(
        ctx, client, channel, username, user_id, text, attachments, payload
    ):
        ack = build_ack_message()
        say(ack)  # also clears the 'thinking' status
        # Update outbox thread root so agent replies land in this assistant thread.
        # thread_ts is the root of the assistant thread; fall back to ts if absent.
        owner_thread_ts = payload.get("thread_ts") or payload.get("ts")
        with ctx["lock"]:
            if owner_thread_ts:
                ctx["state"]["outbox_threads"][channel] = owner_thread_ts
            append_chat_message(ctx["chat_history"], channel, "bot", ack)
            save_chat_history(ctx["chat_history"])
            save_state(ctx["state"])
    else:
        say(":warning: I couldn't queue that just now — please try again.")


def _safe_set_status(set_status, value: str) -> None:
    """Best-effort status update — never let a UI hint break message handling."""
    try:
        set_status(value)
    except Exception as e:
        log.debug(f"set_status failed: {e}")


def _safe_set_title(set_title, value: str) -> None:
    """Best-effort thread-title update."""
    try:
        set_title(value)
    except Exception as e:
        log.debug(f"set_title failed: {e}")


def build_assistant(ctx):
    """Build the Slack Assistant (Agents & AI Apps) middleware.

    Lazy-imports ``Assistant`` so the module still imports on older slack_bolt
    versions; the caller wraps this in try/except so the bridge keeps working
    via the message/command handlers even if the Assistant API is unavailable.
    """
    from slack_bolt import Assistant

    assistant = Assistant()

    @assistant.thread_started
    def _thread_started(say, set_suggested_prompts):
        try:
            say(
                "👋 Hi! I'm your agent. Ask me to do something and I'll pick it "
                "up on my next cycle, or try one of the prompts below."
            )
            set_suggested_prompts(prompts=ASSISTANT_SUGGESTED_PROMPTS)
        except Exception as e:
            log.error(f"assistant thread_started failed: {e}", exc_info=True)

    @assistant.user_message
    def _user_message(payload, client, say, set_status, set_title, logger):
        try:
            _handle_assistant_message(client, payload, ctx, say, set_status, set_title)
        except Exception as e:
            logger.exception(f"assistant user_message failed: {e}")
            try:
                say(":warning: Sorry, something went wrong handling that.")
            except Exception:
                pass

    return assistant


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    _setup_logging()
    _migrate_legacy_message_files()

    # --- Singleton lock: prevent multiple instances running simultaneously ---
    lock_fh = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fh.seek(0)
        existing_pid = lock_fh.read().strip() or "unknown"
        log.error(
            f"Another slack_bridge instance is already running "
            f"(lock held by PID {existing_pid}). Exiting."
        )
        lock_fh.close()
        sys.exit(1)
    lock_fh.seek(0)
    lock_fh.truncate()
    lock_fh.write(str(os.getpid()))
    lock_fh.flush()

    log.info("=" * 60)
    log.info("Slack Bridge starting up")
    log.info("=" * 60)

    bot_token = keepass_get(KEEPASS_SLACK_BOT_TOKEN)
    app_token = keepass_get(KEEPASS_SLACK_APP_TOKEN)
    if not bot_token or not app_token:
        log.error(
            "SLACK_BOT_TOKEN and/or SLACK_APP_TOKEN not found in KeePass. Setup:\n"
            "1. Create a Slack app and enable Socket Mode (see module docstring)\n"
            "2. Store the bot token (xoxb-...): uv run python scripts/keepass.py "
            "store --title 'SLACK_BOT_TOKEN' --username slack --password '<token>'\n"
            "3. Store the app-level token (xapp-...): uv run python scripts/keepass.py "
            "store --title 'SLACK_APP_TOKEN' --username slack --password '<token>'\n"
            "4. Restart the slack_bridge service"
        )
        _release_lock(lock_fh)
        sys.exit(1)

    # Lazy-import Bolt so the module stays importable (e.g. for unit tests)
    # on hosts where slack_bolt is not installed.
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError as e:
        log.error(
            f"slack_bolt is not installed ({e}). Add 'slack-bolt' to "
            "pyproject.toml dependencies and run `uv sync`."
        )
        _release_lock(lock_fh)
        sys.exit(1)

    app = App(token=bot_token, logger=logging.getLogger("slack_bolt"))

    # Verify auth and learn our own user id (to ignore self-messages / strip mentions).
    try:
        auth = app.client.auth_test()
        bot_user_id = auth.get("user_id", "")
        log.info(
            f"Bot authenticated: {auth.get('user')} "
            f"(team={auth.get('team')}, user_id={bot_user_id})"
        )
    except Exception as e:
        log.error(f"Slack auth_test failed — check SLACK_BOT_TOKEN: {e}")
        _release_lock(lock_fh)
        sys.exit(1)

    state = load_state()
    chat_history = load_chat_history()
    chat_ids = parse_chat_ids(keepass_get(KEEPASS_SLACK_CHAT_ID))
    owner_user_id = keepass_get(KEEPASS_SLACK_OWNER_USERID)
    owner_username = keepass_get(KEEPASS_SLACK_OWNER_USERNAME)
    if chat_ids:
        log.info(f"Using saved chat_ids: {chat_ids}")
    else:
        log.info("No chat_ids saved. DM the bot on Slack to auto-discover the owner.")

    # --- Startup: recover outbox thread roots that were lost across restarts ---
    # For every authorized channel whose outbox thread root is missing, query
    # Slack's channel history to find the most recent top-level owner message
    # and reuse its ts as the outbox thread root. This makes threading resilient
    # to process restarts without requiring state-file surgery.
    outbox_threads: dict = dict(state.get("outbox_threads", {}))
    changed = False
    for cid in chat_ids:
        if cid in outbox_threads:
            continue  # already have a root — keep it (owner may have set it)
        recovered_ts = _recover_outbox_thread(app.client, cid, bot_user_id)
        if recovered_ts:
            outbox_threads[cid] = recovered_ts
            log.info(
                f"Startup: recovered outbox thread root for {cid}: ts={recovered_ts}"
            )
            changed = True
        else:
            log.debug(
                f"Startup: no outbox thread root found for {cid} "
                "(no owner messages found, or API error)"
            )
    if changed:
        state["outbox_threads"] = outbox_threads
        save_state(state)
        log.info("Startup: flushed recovered thread roots to disk")

    # Optional redirect for unaddressed (status/FYI) outbox messages: env wins,
    # then KeePass. Resolved once at startup to avoid a per-poll credential read.
    unaddressed_channel = (
        os.environ.get(KEEPASS_SLACK_UNADDRESSED_CHANNEL)
        or keepass_get(KEEPASS_SLACK_UNADDRESSED_CHANNEL)
        or None
    )
    if unaddressed_channel:
        unaddressed_channel = unaddressed_channel.strip() or None
    if unaddressed_channel:
        log.info(f"Unaddressed outbox messages will route to {unaddressed_channel}")

    ctx = {
        "bot_token": bot_token,
        "bot_user_id": bot_user_id,
        "owner_user_id": owner_user_id,
        "owner_username": owner_username,
        "chat_ids": chat_ids,
        "unaddressed_channel": unaddressed_channel,
        "state": state,
        "chat_history": chat_history,
        "user_name_cache": {},
        "lock": threading.Lock(),
    }

    register_handlers(app, ctx)

    # Enable the Assistant (Agents & AI Apps) container for the 1:1 DM UX.
    # Best-effort: if the installed slack_bolt predates the Assistant API, keep
    # running with the message/command handlers above.
    try:
        app.use(build_assistant(ctx))
        log.info("Slack Assistant (Agents & AI Apps) enabled")
    except Exception as e:
        log.warning(
            f"Assistant features unavailable ({e}); continuing with "
            "message/command handlers only"
        )

    handler = SocketModeHandler(app, app_token)

    shutdown_requested = False

    def _handle_sigterm(signum, frame):
        nonlocal shutdown_requested
        log.info(f"Received signal {signum}, requesting graceful shutdown...")
        shutdown_requested = True

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    log.info(f"Loaded chat history for {len(chat_history)} chat(s)")
    log.info("Connecting to Slack via Socket Mode...")
    handler.connect()  # non-blocking — runs the WebSocket on a background thread
    log.info("Entering main loop (Ctrl+C or SIGTERM to stop)...")

    last_outbox_check = 0.0
    consecutive_errors = 0
    MAX_BACKOFF = 300
    try:
        while not shutdown_requested:
            try:
                now = time.time()
                if ctx["chat_ids"] and (now - last_outbox_check >= OUTBOX_INTERVAL):
                    send_outbox_messages(app.client, ctx)
                    last_outbox_check = now

                consecutive_errors = 0
                _write_heartbeat()
                time.sleep(LOOP_SLEEP)

            except KeyboardInterrupt:
                log.info("Shutdown requested via KeyboardInterrupt.")
                break
            except Exception as e:
                consecutive_errors += 1
                backoff = min(10 * (2 ** (consecutive_errors - 1)), MAX_BACKOFF)
                log.error(
                    f"Unexpected error in main loop (attempt {consecutive_errors}, "
                    f"backoff {backoff}s): {e}",
                    exc_info=True,
                )
                time.sleep(backoff)
    finally:
        log.info("Saving final state before exit...")
        try:
            handler.close()
        except Exception:
            pass
        try:
            with ctx["lock"]:
                save_state(ctx["state"])
                save_chat_history(ctx["chat_history"])
            log.info("State saved successfully. Goodbye.")
        except Exception as e:
            log.error(f"Failed to save state on shutdown: {e}", exc_info=True)
        _release_lock(lock_fh)


def _release_lock(lock_fh):
    """Release the singleton lock and remove the lock file."""
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
