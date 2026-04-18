#!/usr/bin/env python3
"""
Telegram Bridge Service
=======================
Bridges the agent's inbox/outbox with a Telegram bot.

- Polls Telegram every ~5s for incoming messages → saves to /agent/messages/inbox.json
- Polls outbox.json every 60s and sends unsent messages to Telegram
- Tracks sent message hashes to prevent duplicates (survives restarts)
- Auto-discovers chat_id from the user's first message to the bot

Setup:
  1. Create a bot via @BotFather on Telegram → get token
  2. Store token: uv run python scripts/keepass.py store --title "TELEGRAM_BOT_TOKEN" --username bot --password "<token>"
  3. Start via service manager:
       uv run python scripts/service_manager.py start telegram_bridge -- uv run python services/telegram_bridge.py
  4. Message the bot on Telegram — it will auto-discover your chat ID
  5. All future outbox messages will be forwarded to you on Telegram

Management:
  uv run python scripts/service_manager.py status telegram_bridge   # check status
  uv run python scripts/service_manager.py stop telegram_bridge     # stop service
  uv run python scripts/service_manager.py list                     # list all services
"""

import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from shared import atomic_write_json, write_to_inbox, append_to_history

# --- Paths ---
BASE = Path("/agent")
STATE_FILE = BASE / "memory" / "telegram_state.json"
INBOX_FILE = BASE / "messages" / "inbox.json"
OUTBOX_FILE = BASE / "messages" / "outbox.json"
INBOX_HISTORY_FILE = BASE / "memory" / "telegram_inbox_history.json"
OUTBOX_HISTORY_FILE = BASE / "memory" / "outbox_history.json"
CHAT_HISTORY_FILE = BASE / "memory" / "telegram_chat_history.json"
LOG_DIR = BASE / "memory" / "logs"
LOG_FILE = LOG_DIR / "telegram_bridge.log"
HEARTBEAT_DIR = BASE / "memory" / "heartbeats"
HEARTBEAT_FILE = HEARTBEAT_DIR / "telegram_bridge.heartbeat"
LOCK_FILE = BASE / "memory" / "telegram_bridge.lock"

LOG_DIR.mkdir(parents=True, exist_ok=True)
HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("telegram_bridge")


def _write_heartbeat():
    """Write current epoch timestamp to heartbeat file for liveness detection."""
    try:
        HEARTBEAT_FILE.write_text(str(time.time()))
    except OSError:
        pass  # non-critical


# _atomic_write_json → imported from shared module as atomic_write_json


# --- Constants ---
KEEPASS_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
KEEPASS_TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"
KEEPASS_TELEGRAM_OWNER_USERNAME = "TELEGRAM_OWNER_USERNAME"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 25  # Telegram long-poll timeout (seconds)
SEND_TIMEOUT = 10  # HTTP timeout for non-polling API calls (sendMessage etc.)
OUTBOX_INTERVAL = 60  # How often to check outbox (seconds)
MAX_PASSCODE_ATTEMPTS = 5  # Max wrong passcode tries before permanent block
STATE_JSON = BASE / "memory" / "state.json"
MEDIA_DIR = BASE / "workspace" / "telegram"
TELEGRAM_FILE_API = "https://api.telegram.org/file/bot{token}/{file_path}"


# ---------------------------------------------------------------------------
# Heartbeat helpers
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
    pattern = r"(?<!\w)@?" + re.escape(username) + r"\b"
    return bool(re.search(pattern, text, re.IGNORECASE))


def is_group_chat(chat_type: str) -> bool:
    """Return True for group and supergroup chats."""
    return chat_type in ("group", "supergroup")


def strip_bot_suffix(command: str, bot_username: str = "") -> str:
    """Strip the ``@BotName`` suffix Telegram appends to commands in groups.

    Only strips the suffix when it targets *this* bot (or when *bot_username*
    is empty — e.g. during startup before ``getMe`` has returned).  For
    commands addressed to other bots (``/goals@OtherBot``), the original
    string is returned unchanged so the dispatcher won't match it.
    """
    if "@" not in command:
        return command
    base, _, suffix = command.partition("@")
    if not bot_username or suffix.lower() == bot_username.lower():
        return base
    return command


def bot_is_addressed(msg: dict, bot_username: str, check_text: str) -> bool:
    """Return True if the bot should respond to this message in a group.

    In groups Telegram's privacy mode means the bot only receives:
      • Commands  (/something or /something@BotName)
      • Messages that @mention the bot
      • Replies to one of the bot's own messages
    This helper mirrors that logic so we don't respond to every message
    even when privacy mode is disabled.
    """
    text = check_text or ""
    # Any slash-command
    if text.lstrip().startswith("/"):
        return True
    # @mention of this bot — word-boundary match so @foo doesn't match @foobar
    if bot_username and contains_username(text, bot_username):
        return True
    # Reply to the bot's own message
    reply_to = msg.get("reply_to_message") or {}
    reply_from = reply_to.get("from") or {}
    if bot_username and reply_from.get("username", "").lower() == bot_username.lower():
        return True
    return False


def generate_passcode() -> str:
    """Generate a cryptographically secure random 4-digit passcode (zero-padded)."""
    return f"{secrets.randbelow(10000):04d}"


# ---------------------------------------------------------------------------
# Block-list helpers
# ---------------------------------------------------------------------------
# blocked_chat_ids entries are dicts: {"chat_id": "...", "username": "..."}
# Legacy string entries (plain chat_id) are migrated in _validate_state.


def is_blocked(state: dict, chat_id: str) -> bool:
    """Return True if chat_id is in the blocked list."""
    for entry in state.get("blocked_chat_ids", []):
        if isinstance(entry, dict):
            if entry.get("chat_id") == chat_id:
                return True
        elif entry == chat_id:  # legacy string format
            return True
    return False


def find_blocked_entry(state: dict, identifier: str) -> dict | None:
    """Find a blocked entry by chat_id or username (leading @ stripped).

    Returns the entry dict on match, or None if not found.
    """
    needle = identifier.lstrip("@").lower()
    for entry in state.get("blocked_chat_ids", []):
        if isinstance(entry, dict):
            if entry.get("chat_id") == needle:
                return entry
            if entry.get("username", "").lower() == needle:
                return entry
        elif entry == needle:  # legacy string
            return {"chat_id": entry, "username": ""}
    return None


def add_block(state: dict, chat_id: str, username: str = "") -> None:
    """Add chat_id to the blocked list, storing username alongside it.

    Removes any existing entry for that chat_id first (no duplicates).
    """
    _remove_block_by_chat_id(state, chat_id)
    state["blocked_chat_ids"].append(
        {"chat_id": chat_id, "username": username.lstrip("@")}
    )


def _remove_block_by_chat_id(state: dict, chat_id: str) -> bool:
    """Internal: remove by exact chat_id only."""
    before = len(state.get("blocked_chat_ids", []))
    state["blocked_chat_ids"] = [
        e
        for e in state.get("blocked_chat_ids", [])
        if not (isinstance(e, dict) and e.get("chat_id") == chat_id)
        and not (isinstance(e, str) and e == chat_id)
    ]
    return len(state["blocked_chat_ids"]) < before


def remove_block(state: dict, identifier: str) -> dict | None:
    """Remove a blocked entry by chat_id or username (leading @ stripped).

    Returns the removed entry dict on success, or None if not found.
    """
    needle = identifier.lstrip("@").lower()
    found = None
    kept = []
    for entry in state.get("blocked_chat_ids", []):
        matched = False
        if isinstance(entry, dict):
            if (
                entry.get("chat_id") == needle
                or entry.get("username", "").lower() == needle
            ):
                matched = True
                found = entry
        elif isinstance(entry, str) and entry == needle:
            matched = True
            found = {"chat_id": entry, "username": ""}
        if not matched:
            kept.append(entry)
    state["blocked_chat_ids"] = kept
    return found


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _validate_state(data) -> dict:
    """Ensure state has the expected structure, repairing wrong types."""
    default = {
        "last_update_id": 0,
        "sent_hashes": [],
        "pending_authorizations": {},
        "blocked_chat_ids": [],
    }
    if not isinstance(data, dict):
        log.warning(
            f"Telegram state has unexpected type {type(data).__name__}, resetting"
        )
        return dict(default)
    # Validate last_update_id — must be int
    uid = data.get("last_update_id")
    if not isinstance(uid, int):
        log.warning(
            f"Telegram state last_update_id has wrong type {type(uid).__name__}, resetting to 0"
        )
        data["last_update_id"] = 0
    # Validate sent_hashes — must be list of strings
    hashes = data.get("sent_hashes")
    if not isinstance(hashes, list):
        log.warning(
            f"Telegram state sent_hashes has wrong type {type(hashes).__name__}, resetting to []"
        )
        data["sent_hashes"] = []
    else:
        # Filter out any non-string entries
        cleaned = [h for h in hashes if isinstance(h, str)]
        if len(cleaned) != len(hashes):
            log.warning(
                f"Removed {len(hashes) - len(cleaned)} non-string entries from sent_hashes"
            )
            data["sent_hashes"] = cleaned
    # Validate pending_authorizations — must be dict
    pending = data.get("pending_authorizations")
    if not isinstance(pending, dict):
        log.warning(
            f"Telegram state pending_authorizations has wrong type {type(pending).__name__}, resetting to {{}}"
        )
        data["pending_authorizations"] = {}
    # Validate blocked_chat_ids — must be list of dicts; migrate legacy string entries
    blocked = data.get("blocked_chat_ids")
    if not isinstance(blocked, list):
        log.warning(
            f"Telegram state blocked_chat_ids has wrong type {type(blocked).__name__}, resetting to []"
        )
        data["blocked_chat_ids"] = []
    else:
        migrated = []
        for entry in blocked:
            if isinstance(entry, str):
                # Legacy format: plain chat_id string → upgrade to dict
                migrated.append({"chat_id": entry, "username": ""})
                log.info(
                    f"Migrated legacy blocked_chat_ids entry '{entry}' to dict format"
                )
            elif isinstance(entry, dict) and entry.get("chat_id"):
                migrated.append(entry)
            # else: malformed entry, drop it
        data["blocked_chat_ids"] = migrated
    return data


def load_state() -> dict:
    default = {
        "last_update_id": 0,
        "sent_hashes": [],
        "pending_authorizations": {},
        "blocked_chat_ids": [],
    }
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return _validate_state(data)
        except Exception as e:
            log.warning(f"Corrupt telegram state file, using defaults: {e}")
    return dict(default)


def save_state(state: dict):
    atomic_write_json(STATE_FILE, state, indent=2)


def msg_hash(msg: dict) -> str:
    """Stable 16-char hash of an outbox message to detect duplicates."""
    key = json.dumps(msg, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def protect_urls_in_markdown(text: str) -> str:
    """Wrap URLs in backticks to protect them from Markdown parsing.

    Telegram's Markdown mode treats underscores and other special chars
    as formatting markers. Wrapping URLs in backticks creates inline code
    blocks that preserve URLs exactly as-is.
    """
    # Match URLs: http(s)://... until whitespace or end of string
    # Lookahead/lookbehind ensure we don't wrap already-wrapped URLs
    url_pattern = r"(?<!`)(?<!`)\b(https?://[^\s`]+)(?!`)(?!`)"

    def wrap_url(match):
        url = match.group(1)
        if url.endswith("`"):
            return url
        return f"`{url}`"

    return re.sub(url_pattern, wrap_url, text)


def _strip_markdown_preserve_code(text: str) -> str:
    """Strip Markdown formatting but preserve backtick-wrapped content.

    Used as fallback when Telegram's Markdown parser fails. Removes
    bold (*) and italic (_) markers but keeps backtick-wrapped URLs intact.
    """
    # Extract backtick-wrapped content (URLs, code blocks)
    backtick_pattern = r"`([^`]+)`"
    placeholders = {}
    counter = 0

    def replace_with_placeholder(match):
        nonlocal counter
        # Use placeholder without underscores to avoid stripping issues
        placeholder = f"{{{{PRESERVED{counter}}}}}"
        placeholders[placeholder] = match.group(1)  # Store without backticks
        counter += 1
        return placeholder

    # Replace backtick blocks with placeholders
    text = re.sub(backtick_pattern, replace_with_placeholder, text)

    # Strip markdown formatting chars
    text = text.replace("*", "").replace("_", "")

    # Restore preserved content
    for placeholder, content in placeholders.items():
        text = text.replace(placeholder, content)

    return text


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
        lines.append(f"{label}: {m['text']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------


def tg(token: str, method: str, _http_timeout: float | None = None, **params):
    """Call the Telegram Bot API. Returns result on success, None on failure.

    *_http_timeout* sets the HTTP request timeout in seconds.  Defaults to
    SEND_TIMEOUT (10s) for all methods except getUpdates, which uses
    POLL_TIMEOUT + 5 (30s) to accommodate the long-poll window.
    Pass an explicit value to override (e.g. file downloads need more time).
    """
    is_long_poll = method == "getUpdates"
    http_timeout = (
        _http_timeout
        if _http_timeout is not None
        else (POLL_TIMEOUT + 5 if is_long_poll else SEND_TIMEOUT)
    )
    url = TELEGRAM_API.format(token=token, method=method)
    try:
        resp = requests.post(url, json=params, timeout=http_timeout)
        try:
            data = resp.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            log.warning(
                f"Telegram API non-JSON response ({method}, HTTP {resp.status_code}): {resp.text[:200]}"
            )
            return None
        if data.get("ok"):
            return data.get("result")
        log.warning(f"Telegram API error ({method}): {data.get('description')}")
    except requests.exceptions.Timeout:
        if not is_long_poll:
            log.warning(
                f"Telegram request timed out ({method}, timeout={http_timeout}s)"
            )
        # else: normal for long-poll — no log noise
    except Exception as e:
        log.error(f"Telegram request failed ({method}): {e}")
    return None


# ---------------------------------------------------------------------------
# Media download helpers
# ---------------------------------------------------------------------------


def extract_media(msg: dict) -> list[tuple[str, str, str]]:
    """Extract downloadable media from a Telegram message.

    Returns list of (file_id, media_type, filename_hint) tuples.
    Supports: photo, document (PDF, DOCX, images-as-file, etc.), audio.
    """
    media = []

    # Photos — Telegram sends multiple sizes; pick the largest (last)
    if msg.get("photo") and isinstance(msg["photo"], list) and len(msg["photo"]) > 0:
        largest = msg["photo"][-1]
        fid = largest.get("file_id") if isinstance(largest, dict) else None
        if fid:
            media.append((fid, "image", "photo.jpg"))

    # Document — covers PDF, DOCX, TXT, CSV, XLSX, images sent as files, etc.
    if msg.get("document"):
        doc = msg["document"]
        fid = doc.get("file_id")
        if fid:
            fname = doc.get("file_name") or f"document_{fid[:8]}"
            media.append((fid, "document", fname))

    # Audio files
    if msg.get("audio"):
        aud = msg["audio"]
        fid = aud.get("file_id")
        if fid:
            fname = aud.get("file_name") or f"audio_{fid[:8]}.mp3"
            media.append((fid, "audio", fname))

    return media


def download_telegram_file(
    token: str, file_id: str, chat_id: str, filename_hint: str
) -> str | None:
    """Download a file from Telegram and save to /agent/workspace/telegram/{chat_id}/.

    Returns the local file path on success, None on failure.
    """
    # Step 1: get the file path from Telegram
    result = tg(token, "getFile", file_id=file_id)
    if not result or not result.get("file_path"):
        log.warning(f"Failed to getFile for file_id={file_id[:16]}")
        return None

    remote_path = result["file_path"]
    url = TELEGRAM_FILE_API.format(token=token, file_path=remote_path)

    # Step 2: download the file and stream to disk
    dest_dir = MEDIA_DIR / chat_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(filename_hint).name  # strip any path components
    unique_name = f"{uuid.uuid4().hex[:12]}_{safe_name}"
    dest_path = dest_dir / unique_name

    max_size = 50 * 1024 * 1024  # 50 MB safety limit
    try:
        with requests.get(url, timeout=(30, 120), stream=True) as resp:
            resp.raise_for_status()
            # Check Content-Length header if available for early rejection
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > max_size:
                log.warning(
                    f"File {remote_path} too large ({content_length} bytes), skipping"
                )
                return None
            size = 0
            exceeded = False
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    size += len(chunk)
                    if size > max_size:
                        log.warning(
                            f"File {remote_path} exceeded {max_size} byte limit at {size} bytes, aborting"
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
        log.error(f"Failed to download file {remote_path}: {e}")
        dest_path.unlink(missing_ok=True)
        return None

    log.info(f"Saved media: {dest_path} ({size} bytes)")
    return str(dest_path)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def handle_heartbeat_command(
    token: str,
    from_chat: str,
    from_user: str,
    chat_history: dict,
    extra_args: list[str] = None,
) -> bool:
    """Handle the /heartbeat command by triggering an immediate heartbeat.

    Args:
        token: Telegram bot token
        from_chat: Chat ID where command was received
        from_user: Username who sent the command
        chat_history: Chat history dict
        extra_args: Additional arguments to pass to heartbeat.sh (e.g., ["--agent-sleep"])

    Returns True if command was handled, False otherwise.
    """
    try:
        cmd_args = extra_args or []
        args_str = " ".join(cmd_args) if cmd_args else "(no flags)"
        log.info(f"Heartbeat command received from @{from_user} with args: {args_str}")

        # Build command with any additional arguments
        cmd = ["bash", "/agent/heartbeat.sh"] + cmd_args

        # Trigger heartbeat by running the heartbeat script
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            cwd="/agent",
        )

        if result.returncode == 0:
            response = "✅ Heartbeat triggered successfully!\n\nThe agent cycle is now running."
            log.info(f"Heartbeat completed successfully for @{from_user}")
        else:
            response = f"⚠️ Heartbeat triggered but returned non-zero exit code: {result.returncode}\n\nCheck logs for details."
            log.warning(
                f"Heartbeat failed with code {result.returncode}: {result.stderr}"
            )

        tg(token, "sendMessage", chat_id=from_chat, text=response)
        append_chat_message(chat_history, from_chat, "bot", response)
        return True

    except subprocess.TimeoutExpired:
        response = (
            "⏱️ Heartbeat timed out after 5 minutes. The cycle may still be running."
        )
        log.error(f"Heartbeat timeout for @{from_user}")
        tg(token, "sendMessage", chat_id=from_chat, text=response)
        append_chat_message(chat_history, from_chat, "bot", response)
        return True

    except Exception as e:
        response = f"❌ Failed to trigger heartbeat: {str(e)}"
        log.error(f"Heartbeat command failed for @{from_user}: {e}", exc_info=True)
        tg(token, "sendMessage", chat_id=from_chat, text=response)
        append_chat_message(chat_history, from_chat, "bot", response)
        return True


# ---------------------------------------------------------------------------
# Quick-reply commands (no heartbeat needed)
# ---------------------------------------------------------------------------


def handle_status_command(
    token: str, from_chat: str, from_user: str, chat_history: dict
) -> None:
    """Handle /status — return live agent status from memory files instantly."""
    try:
        state_path = BASE / "memory" / "state.json"
        goal_path = BASE / "messages" / "inbox.json"
        cycles_path = BASE / "memory" / "cycles.json"
        goals_path = BASE / "memory" / "goal.json"

        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        goals = json.loads(goals_path.read_text()) if goals_path.exists() else []
        cycles = json.loads(cycles_path.read_text()) if cycles_path.exists() else []

        status = state.get("status", "unknown")
        cycle_num = state.get("cycle_number", "?")
        last_hb = state.get("last_heartbeat", "unknown")
        summary = state.get("last_cycle_summary", "")

        # Count goals by status
        if isinstance(goals, list):
            active = sum(
                1 for g in goals if g.get("status") in ("pending", "in_progress")
            )
            completed = sum(1 for g in goals if g.get("status") == "completed")
            failed = sum(1 for g in goals if g.get("status") == "failed")
        else:
            active = completed = failed = 0

        # Count cycles
        total_cycles = len(cycles) if isinstance(cycles, list) else 0

        status_emoji = {"running": "⚙️", "idle": "✅", "waiting_for_human": "⏳"}.get(
            status, "❓"
        )

        lines = [
            f"{status_emoji} *Agent Status*",
            f"Cycle: #{cycle_num}  |  Status: `{status}`",
            f"Last heartbeat: {last_hb[:19] if last_hb and last_hb != 'unknown' else 'unknown'}",
            f"Goals: {active} active, {completed} done, {failed} failed",
            f"Total cycles: {total_cycles}",
        ]
        if summary:
            lines.append(f"\n_Last: {summary[:200]}_")

        response = "\n".join(lines)
        tg(
            token,
            "sendMessage",
            chat_id=from_chat,
            text=response,
            parse_mode="Markdown",
        )
        append_chat_message(chat_history, from_chat, "bot", response)
        log.info(f"/status command served to @{from_user}")
    except Exception as e:
        err = f"❌ Failed to read status: {e}"
        tg(token, "sendMessage", chat_id=from_chat, text=err)
        log.error(f"/status error for @{from_user}: {e}", exc_info=True)


def handle_goals_command(
    token: str, from_chat: str, from_user: str, chat_history: dict, limit: int = 5
) -> None:
    """Handle /goals [N] — show the N most recent goals and their status."""
    try:
        goals_path = BASE / "memory" / "goal.json"
        goals = json.loads(goals_path.read_text()) if goals_path.exists() else []

        if not isinstance(goals, list) or not goals:
            response = "📭 No goals found."
            tg(token, "sendMessage", chat_id=from_chat, text=response)
            append_chat_message(chat_history, from_chat, "bot", response)
            return

        # Sort by updated_at desc, take limit
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
            # Truncate long goal text
            if len(goal_text) > 80:
                goal_text = goal_text[:77] + "..."
            lines.append(f"{emoji} `{st}` — {goal_text}")

        response = "\n".join(lines)
        tg(
            token,
            "sendMessage",
            chat_id=from_chat,
            text=response,
            parse_mode="Markdown",
        )
        append_chat_message(chat_history, from_chat, "bot", response)
        log.info(f"/goals command served to @{from_user} (limit={limit})")
    except Exception as e:
        err = f"❌ Failed to read goals: {e}"
        tg(token, "sendMessage", chat_id=from_chat, text=err)
        log.error(f"/goals error for @{from_user}: {e}", exc_info=True)


def handle_journal_command(
    token: str, from_chat: str, from_user: str, chat_history: dict, limit: int = 3
) -> None:
    """Handle /journal [N] — show the N most recent journal entries with summaries."""
    try:
        journal_path = BASE / "memory" / "journal.json"
        entries = json.loads(journal_path.read_text()) if journal_path.exists() else []

        if not isinstance(entries, list) or not entries:
            response = "📭 No journal entries found."
            tg(token, "sendMessage", chat_id=from_chat, text=response)
            append_chat_message(chat_history, from_chat, "bot", response)
            return

        # Sort by timestamp desc and take limit
        sorted_entries = sorted(
            entries, key=lambda e: e.get("timestamp", ""), reverse=True
        )
        recent = sorted_entries[:limit]

        type_emoji = {
            "evolve": "⚙️",
            "goal": "🎯",
            "self-heal": "🔧",
            "self_heal": "🔧",
        }

        lines = [
            f"📓 *Last {len(recent)} journal entr{'y' if len(recent) == 1 else 'ies'}*"
        ]
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
            lines.append(
                f"{emoji} *#{cycle_num}*{cat_label} — {summary or '(no summary)'} _{ts}_"
            )

        response = "\n".join(lines)
        tg(
            token,
            "sendMessage",
            chat_id=from_chat,
            text=response,
            parse_mode="Markdown",
        )
        append_chat_message(chat_history, from_chat, "bot", response)
        log.info(f"/journal command served to @{from_user} (limit={limit})")
    except Exception as e:
        err = f"❌ Failed to read journal: {e}"
        tg(token, "sendMessage", chat_id=from_chat, text=err)
        log.error(f"/journal error for @{from_user}: {e}", exc_info=True)


def handle_today_command(
    token: str, from_chat: str, from_user: str, chat_history: dict
) -> None:
    """Handle /today — show today's activity summary: completed goals, cycle count."""
    try:
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        cutoff_str = cutoff.isoformat()

        # --- Goals completed today ---
        goals_path = BASE / "memory" / "goal.json"
        goals_data = json.loads(goals_path.read_text()) if goals_path.exists() else {}
        all_goals = []
        for key in ("active", "archived"):
            bucket = goals_data.get(key, [])
            if isinstance(bucket, list):
                all_goals.extend(bucket)

        completed_today = [
            g
            for g in all_goals
            if g.get("status") == "completed"
            and (g.get("updated_at") or "") >= cutoff_str
        ]

        # --- Cycles run today ---
        cycles_path = BASE / "memory" / "cycles.json"
        cycles = json.loads(cycles_path.read_text()) if cycles_path.exists() else []
        cycles_today = [
            c
            for c in cycles
            if c.get("status") == "completed"
            and (c.get("end_time") or c.get("start_time") or "") >= cutoff_str
        ]
        evolve_today = [c for c in cycles_today if c.get("type") == "evolve"]
        goal_today = [c for c in cycles_today if c.get("type") == "goal"]

        lines = [
            f"📅 *Today's Summary* _(last 24h as of {now.strftime('%H:%M')} UTC)_\n"
        ]

        # Goals completed today
        if completed_today:
            lines.append(f"*Goals Completed* — {len(completed_today)}")
            for g in completed_today[-5:]:
                goal_text = g.get("goal", "")[:70]
                lines.append(f"  🎯 {goal_text}")

        # Cycles summary
        lines.append(f"\n*Cycles* — {len(cycles_today)} total")
        if evolve_today:
            cats = [c.get("category", "?") for c in evolve_today]
            lines.append(f"  ⚙️ {len(evolve_today)} evolve: {', '.join(cats[:5])}")
        if goal_today:
            lines.append(f"  🎯 {len(goal_today)} goal cycles")

        # Needs-human check
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
                    f"\n⚠️ *Blocked* — {len(blocked)} item(s) need human attention (`/outbox` for details)"
                )

        response = "\n".join(lines)
        tg(
            token,
            "sendMessage",
            chat_id=from_chat,
            text=response,
            parse_mode="Markdown",
        )
        append_chat_message(chat_history, from_chat, "bot", response)
        log.info(
            f"/today command served to @{from_user} ({len(completed_today)} goals, {len(cycles_today)} cycles)"
        )
    except Exception as e:
        err = f"❌ Failed to generate today summary: {e}"
        tg(token, "sendMessage", chat_id=from_chat, text=err)
        log.error(f"/today error for @{from_user}: {e}", exc_info=True)


def handle_outbox_command(
    token: str, from_chat: str, from_user: str, chat_history: dict, limit: int = 5
) -> None:
    """Handle /outbox [N] — show the N most recent outbox messages, highlighting needs_human items."""
    try:
        outbox_path = BASE / "messages" / "outbox.json"
        messages = json.loads(outbox_path.read_text()) if outbox_path.exists() else []

        if not isinstance(messages, list) or not messages:
            response = "📭 Outbox is empty."
            tg(token, "sendMessage", chat_id=from_chat, text=response)
            append_chat_message(chat_history, from_chat, "bot", response)
            return

        # Sort by timestamp desc and take limit
        sorted_msgs = sorted(
            messages, key=lambda m: m.get("timestamp", ""), reverse=True
        )
        recent = sorted_msgs[:limit]

        # Count needs_human
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
            # Use subject if available, otherwise truncate content
            display = subject if subject else content
            if len(display) > 100:
                display = display[:97] + "..."
            lines.append(f"{emoji} `{mtype}` — {display} _{ts}_")

        response = "\n".join(lines)
        tg(
            token,
            "sendMessage",
            chat_id=from_chat,
            text=response,
            parse_mode="Markdown",
        )
        append_chat_message(chat_history, from_chat, "bot", response)
        log.info(
            f"/outbox command served to @{from_user} (limit={limit}, needs_human={needs_human_count})"
        )
    except Exception as e:
        err = f"❌ Failed to read outbox: {e}"
        tg(token, "sendMessage", chat_id=from_chat, text=err)
        log.error(f"/outbox error for @{from_user}: {e}", exc_info=True)


def handle_cycles_command(
    token: str, from_chat: str, from_user: str, chat_history: dict, limit: int = 5
) -> None:
    """Handle /cycles [N] — show the N most recent completed cycles with type, category, duration, and summary."""
    try:
        cycles_path = BASE / "memory" / "cycles.json"
        cycles = json.loads(cycles_path.read_text()) if cycles_path.exists() else []

        if not isinstance(cycles, list) or not cycles:
            response = "📊 No cycle history found."
            tg(token, "sendMessage", chat_id=from_chat, text=response)
            append_chat_message(chat_history, from_chat, "bot", response)
            return

        # Filter to completed cycles only, most recent first
        completed = [c for c in cycles if c.get("status") == "completed"]
        recent = completed[-limit:][::-1]  # last N, reversed to newest-first

        total = len(completed)
        type_emoji = {"evolve": "🔧", "goal": "🎯", "self-heal": "🩺", "unknown": "❓"}

        header = f"📊 *Recent Cycles* (last {len(recent)} of {total} completed)"
        lines = [header]

        for c in recent:
            num = c.get("cycle", "?")
            ctype = c.get("type", "unknown")
            cat = c.get("category", "")
            dur = c.get("duration_seconds")
            summary = (c.get("summary", "") or "")[:120]
            if len(c.get("summary", "") or "") > 120:
                summary += "..."

            emoji = type_emoji.get(ctype, "❓")
            dur_str = f"{dur}s" if dur is not None else "?"
            label = f"{ctype}/{cat}" if cat else ctype

            lines.append(f"{emoji} *#{num}* `{label}` — {dur_str}")
            if summary:
                lines.append(f"   _{summary}_")

        response = "\n".join(lines)
        tg(
            token,
            "sendMessage",
            chat_id=from_chat,
            text=response,
            parse_mode="Markdown",
        )
        append_chat_message(chat_history, from_chat, "bot", response)
        log.info(f"/cycles command served to @{from_user} (limit={limit})")
    except Exception as e:
        err = f"❌ Failed to read cycles: {e}"
        tg(token, "sendMessage", chat_id=from_chat, text=err)
        log.error(f"/cycles error for @{from_user}: {e}", exc_info=True)


def handle_services_command(
    token: str, from_chat: str, from_user: str, chat_history: dict
) -> None:
    """Handle /services — show background service status from services.json."""
    try:
        import os

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

                # Verify process is actually alive
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
                meta_str = f" _({', '.join(meta)})_" if meta else ""

                lines.append(f"{status_icon} `{name}` — {status_label}{meta_str}")

            response = "\n".join(lines)

        tg(
            token,
            "sendMessage",
            chat_id=from_chat,
            text=response,
            parse_mode="Markdown",
        )
        append_chat_message(chat_history, from_chat, "bot", response)
        log.info(f"/services command served to @{from_user}")
    except Exception as e:
        err = f"❌ Failed to read services: {e}"
        tg(token, "sendMessage", chat_id=from_chat, text=err)
        log.error(f"/services error for @{from_user}: {e}", exc_info=True)


def handle_remind_command(
    token: str, from_chat: str, from_user: str, chat_history: dict, args: list[str]
) -> None:
    """Handle /remind <duration> <text> — create a one-time reminder.

    Duration formats: 30m, 2h, 1d (minutes, hours, days).
    Example: /remind 30m check deployment
    """
    if len(args) < 2:
        usage = (
            "Usage: `/remind <duration> <message>`\n"
            "Duration: `30m`, `2h`, `1d`\n"
            "Example: `/remind 30m check deployment`"
        )
        tg(token, "sendMessage", chat_id=from_chat, text=usage, parse_mode="Markdown")
        append_chat_message(chat_history, from_chat, "bot", usage)
        return

    duration_str = args[0].lower()
    reminder_text = " ".join(args[1:])

    # Parse duration into minutes
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
        err = f"Invalid duration `{duration_str}`. Use formats like `30m`, `2h`, `1d`."
        tg(token, "sendMessage", chat_id=from_chat, text=err, parse_mode="Markdown")
        append_chat_message(chat_history, from_chat, "bot", err)
        return

    # Compute fire-at time
    fire_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M"
    )

    # Create reminder via direct import
    from scripts.reminder import add_reminder

    rid = add_reminder(reminder_text, at=fire_at)

    if rid:
        # Human-friendly duration label
        if minutes >= 1440 and minutes % 1440 == 0:
            label = f"{minutes // 1440}d"
        elif minutes >= 60 and minutes % 60 == 0:
            label = f"{minutes // 60}h"
        else:
            label = f"{minutes}m"
        response = f"Reminder set for *{label}* from now: _{reminder_text}_"
        log.info(f"/remind command: '{reminder_text}' in {label} by @{from_user}")
    else:
        response = f"Failed to create reminder."
        log.error(f"/remind error for @{from_user}: add_reminder returned None")

    tg(token, "sendMessage", chat_id=from_chat, text=response, parse_mode="Markdown")
    append_chat_message(chat_history, from_chat, "bot", response)


def handle_note_command(
    token: str, from_chat: str, from_user: str, chat_history: dict, args: list[str]
) -> None:
    """Handle /note <text> — save a quick note via Telegram.

    Example: /note check the deploy logs tomorrow
    """
    if not args:
        usage = "Usage: `/note <text>`\n" "Example: `/note review PR 125 tomorrow`"
        tg(token, "sendMessage", chat_id=from_chat, text=usage, parse_mode="Markdown")
        append_chat_message(chat_history, from_chat, "bot", usage)
        return

    content = " ".join(args)
    # Use first 60 chars as title
    title = content[:60].rstrip() + ("..." if len(content) > 60 else "")

    from scripts.notes import add_note

    note_id = add_note(title, content, tags=["telegram"])

    if note_id:
        response = f"Note saved: _{title}_"
        log.info(f"/note command: '{title}' by @{from_user}")
    else:
        response = f"Failed to save note."
        log.error(f"/note error for @{from_user}: add_note returned None")

    tg(token, "sendMessage", chat_id=from_chat, text=response, parse_mode="Markdown")
    append_chat_message(chat_history, from_chat, "bot", response)


def handle_notes_command(
    token: str, from_chat: str, from_user: str, chat_history: dict, args: list[str]
) -> None:
    """Handle /notes [query] — list recent notes or search by keyword.

    Examples:
      /notes          → list 5 most recent notes
      /notes deploy   → search notes for "deploy"
    """
    from scripts.notes import list_notes, search_notes

    if args:
        query = " ".join(args)
        notes = search_notes(query)
        header = f"Notes matching _{query}_:"
    else:
        notes = list_notes()
        header = "Recent notes:"

    if not notes:
        response = (
            "No notes found." if not args else f"No notes matching _{' '.join(args)}_."
        )
        tg(
            token,
            "sendMessage",
            chat_id=from_chat,
            text=response,
            parse_mode="Markdown",
        )
        append_chat_message(chat_history, from_chat, "bot", response)
        return

    # Show up to 5 most recent
    shown = notes[:5]
    lines = [header, ""]
    for n in shown:
        title = n.get("title", "Untitled")
        content = n.get("content", "")
        tags = n.get("tags", [])
        # Truncate content preview
        preview = content[:80].replace("\n", " ")
        if len(content) > 80:
            preview += "..."
        tag_str = f" _#{' #'.join(tags)}_" if tags else ""
        lines.append(f"• *{title}*{tag_str}")
        if preview and preview != title:
            lines.append(f"  {preview}")

    if len(notes) > 5:
        lines.append(f"\n_…and {len(notes) - 5} more_")

    response = "\n".join(lines)
    tg(token, "sendMessage", chat_id=from_chat, text=response, parse_mode="Markdown")
    append_chat_message(chat_history, from_chat, "bot", response)
    log.info(
        f"/notes command served to @{from_user} (query={' '.join(args) if args else None}, count={len(shown)})"
    )


def handle_unblock_command(
    token: str,
    from_chat: str,
    from_user: str,
    chat_history: dict,
    state: dict,
    args: list[str],
) -> None:
    """Handle /unblock <@username|chat_id> — remove a user from the permanent block list."""
    if not args:
        usage = (
            "Usage: `/unblock <@username or chat_id>`\n"
            "Examples:\n"
            "  `/unblock @vinhbachsy`\n"
            "  `/unblock 98313829`"
        )
        tg(token, "sendMessage", chat_id=from_chat, text=usage, parse_mode="Markdown")
        append_chat_message(chat_history, from_chat, "bot", usage)
        return

    identifier = args[0]
    entry = remove_block(state, identifier)

    if entry:
        display = f"@{entry['username']}" if entry.get("username") else entry["chat_id"]
        chat_id_label = (
            f" (chat ID: `{entry['chat_id']}`)" if entry.get("username") else ""
        )
        response = (
            f"✅ *{display}*{chat_id_label} has been unblocked.\n"
            "They can now message the bot again and will be prompted for a new passcode challenge."
        )
        log.info(
            f"/unblock: removed {display} (chat_id={entry['chat_id']}) from block list by @{from_user}"
        )
        save_state(state)
    else:
        response = (
            f"⚠️ `{identifier}` was not found in the block list.\n"
            "Check the identifier and try again."
        )
        log.info(
            f"/unblock: '{identifier}' not found in block list (requested by @{from_user})"
        )

    tg(token, "sendMessage", chat_id=from_chat, text=response, parse_mode="Markdown")
    append_chat_message(chat_history, from_chat, "bot", response)


def handle_help_command(
    token: str, from_chat: str, from_user: str, chat_history: dict
) -> None:
    """Handle /help — list available bot commands."""
    response = (
        "🤖 *Agent — Available Commands*\n\n"
        "/status — Live agent status (cycle, goals, last heartbeat)\n"
        "/goals [N] — Show last N goals (default 5, max 20)\n"
        "/cycles [N] — Show last N completed cycles with type/category/duration (default 5, max 20)\n"
        "/journal [N] — Show last N journal entries (default 3, max 10)\n"
        "/outbox [N] — Show last N outbox messages, highlights needs\\_human (default 5, max 20)\n"
        "/services — Show background service status (running/dead, PID, port)\n"
        "/today — Summary of last 24h: goals, cycles, blockers\n"
        "/remind <duration> <text> — Set a reminder (e.g. `/remind 30m check deploy`)\n"
        "/note <text> — Save a quick note (e.g. `/note review PR 125 tomorrow`)\n"
        "/notes [query] — List recent notes or search (e.g. `/notes deploy`)\n"
        "/heartbeat — Trigger an immediate agent cycle\n"
        "/unblock <@username|chat\\_id> — Remove a user from the permanent block list\n"
        "/help — Show this help message\n\n"
        "_Any other message is queued as a goal for the next heartbeat._"
    )
    tg(token, "sendMessage", chat_id=from_chat, text=response, parse_mode="Markdown")
    append_chat_message(chat_history, from_chat, "bot", response)
    log.info(f"/help command served to @{from_user}")


# ---------------------------------------------------------------------------
# Incoming messages (Telegram → inbox.json)
# ---------------------------------------------------------------------------


def _process_authorized_message(
    token: str,
    msg: dict,
    text: str,
    from_user: str,
    from_chat: str,
    chat_history: dict,
    inbox_items: list,
    state: dict | None = None,
    history_key: str | None = None,
    bot_username: str = "",
):
    """Process an authorized incoming message (text and/or media) into an inbox item.

    *history_key* is the key used for chat-history lookups.  For private chats it
    equals *from_chat*; for group chats it is ``"<group_chat_id>:<user_id>"`` so
    each group member gets their own conversation context.  Falls back to
    *from_chat* when not supplied.
    """
    hkey = history_key if history_key is not None else from_chat
    chat_type = msg.get("chat", {}).get("type", "private")
    group_chat = is_group_chat(chat_type)

    # Check for commands first
    if text and text.startswith("/"):
        parts = text.split()
        # Strip @BotName suffix (only when it targets *this* bot — commands
        # addressed to other bots in a group will not match any handler).
        command = strip_bot_suffix(parts[0].lower(), bot_username)

        if command == "/heartbeat":
            # Whitelist allowed flags to prevent arbitrary arg injection
            ALLOWED_HEARTBEAT_ARGS = {"--agent-sleep"}
            raw_args = parts[1:] if len(parts) > 1 else []
            extra_args = [a for a in raw_args if a in ALLOWED_HEARTBEAT_ARGS]
            rejected = [a for a in raw_args if a not in ALLOWED_HEARTBEAT_ARGS]
            if rejected:
                log.warning(f"Rejected heartbeat args from @{from_user}: {rejected}")
            handle_heartbeat_command(
                token, from_chat, from_user, chat_history, extra_args
            )
            return  # Don't process as regular message

        if command == "/status":
            handle_status_command(token, from_chat, from_user, chat_history)
            return

        if command == "/goals":
            limit = 5
            if len(parts) > 1 and parts[1].isdigit():
                limit = max(1, min(int(parts[1]), 20))
            handle_goals_command(token, from_chat, from_user, chat_history, limit)
            return

        if command == "/journal":
            limit = 3
            if len(parts) > 1 and parts[1].isdigit():
                limit = max(1, min(int(parts[1]), 10))
            handle_journal_command(token, from_chat, from_user, chat_history, limit)
            return

        if command == "/outbox":
            limit = 5
            if len(parts) > 1 and parts[1].isdigit():
                limit = max(1, min(int(parts[1]), 20))
            handle_outbox_command(token, from_chat, from_user, chat_history, limit)
            return

        if command == "/cycles":
            limit = 5
            if len(parts) > 1 and parts[1].isdigit():
                limit = max(1, min(int(parts[1]), 20))
            handle_cycles_command(token, from_chat, from_user, chat_history, limit)
            return

        if command == "/services":
            handle_services_command(token, from_chat, from_user, chat_history)
            return

        if command == "/today":
            handle_today_command(token, from_chat, from_user, chat_history)
            return

        if command == "/remind":
            handle_remind_command(token, from_chat, from_user, chat_history, parts[1:])
            return

        if command == "/note":
            handle_note_command(token, from_chat, from_user, chat_history, parts[1:])
            return

        if command == "/notes":
            handle_notes_command(token, from_chat, from_user, chat_history, parts[1:])
            return

        if command == "/unblock":
            handle_unblock_command(
                token, from_chat, from_user, chat_history, state, parts[1:]
            )
            return

        if command == "/start":
            # Authorized user — /start is equivalent to /help
            handle_help_command(token, from_chat, from_user, chat_history)
            return

        if command == "/help":
            handle_help_command(token, from_chat, from_user, chat_history)
            return

    # Extract media attachments (photo, document, audio)
    media_list = extract_media(msg)

    # Use caption as fallback text for media messages
    caption = msg.get("caption", "").strip()
    effective_text = text or caption

    if not effective_text and not media_list:
        return  # nothing to process

    # Download media files
    attachments = []
    for file_id, media_type, filename_hint in media_list:
        local_path = download_telegram_file(token, file_id, from_chat, filename_hint)
        if local_path:
            attachments.append(
                {
                    "type": media_type,
                    "path": local_path,
                    "filename": Path(local_path).name,
                }
            )

    # Build content string
    context = build_chat_context(chat_history, hkey)
    base_content = (
        f"[Telegram @{from_user}]: {effective_text}"
        if effective_text
        else f"[Telegram @{from_user}]:"
    )

    if attachments:
        attachment_lines = "\n".join(f"- {a['type']}: {a['path']}" for a in attachments)
        base_content += f"\n[Attachments]\n{attachment_lines}"

    if context:
        content = f"[Previous conversation context (last 24h)]\n{context}\n[End of context]\n\n[New message]\n{base_content}"
    else:
        content = base_content

    chat_text = effective_text or "(media)"
    append_chat_message(chat_history, hkey, "user", chat_text)

    inbox_item = {
        "type": "message",
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "source": "telegram",
    }
    if attachments:
        inbox_item["attachments"] = attachments

    inbox_items.append(inbox_item)
    log.info(
        f"Received from @{from_user}: {chat_text[:100]}"
        + (f" (+{len(attachments)} attachment(s))" if attachments else "")
    )

    # Suppress the public ack in groups — the agent's actual reply will be
    # delivered via the outbox, and acking every message publicly is noisy.
    if not group_chat:
        ack = build_ack_message()
        tg(token, "sendMessage", chat_id=from_chat, text=ack)
        append_chat_message(chat_history, hkey, "bot", ack)
        log.info(f"Sent ack reply to {from_chat}")


def poll_updates(
    token: str,
    state: dict,
    owner_username: str | None,
    chat_ids: list[str],
    chat_history: dict,
    bot_username: str = "",
) -> tuple[str | None, list[str], list]:  # noqa: E501
    """
    Long-poll Telegram for new updates.
    Returns (updated_owner_username, updated_chat_ids, list_of_inbox_items).
    """
    updates = tg(
        token,
        "getUpdates",
        offset=state["last_update_id"] + 1,
        timeout=POLL_TIMEOUT,
        limit=100,
    )
    if not updates:
        return owner_username, chat_ids, []

    inbox_items = []
    for update in updates:
        uid = update.get("update_id")
        if uid is None:
            log.warning(
                f"Skipping malformed update (no update_id): {str(update)[:200]}"
            )
            continue
        state["last_update_id"] = max(state["last_update_id"], uid)

        try:
            msg = update.get("message") or update.get("channel_post")
            if not msg:
                continue

            from_chat = str(msg.get("chat", {}).get("id", ""))
            from_info = msg.get("from", {})
            from_user = from_info.get("username") or from_info.get("first_name", "user")
            text = msg.get("text", "").strip()
            caption = msg.get("caption", "").strip()
            check_text = text or caption  # for auth checks on media-with-caption

            # --- Chat-type metadata ---
            chat_type = msg.get("chat", {}).get("type", "private")
            group_chat = is_group_chat(chat_type)
            from_user_id = str(from_info.get("id", ""))

            # --- Channel posts are out of scope ---
            # Channels don't fit the private-chat or group-chat auth model and
            # have no per-user `from` metadata to gate on.  Ignore entirely.
            if chat_type == "channel":
                log.debug(
                    f"Ignoring channel_post from chat {from_chat} (channels not supported)"
                )
                continue

            # Per-user history key in groups; per-chat in private chats.
            # This prevents different group members' contexts from bleeding together.
            history_key = f"{from_chat}:{from_user_id}" if group_chat else from_chat

            # --- Privacy-mode guard for groups ---
            # In groups, only respond to commands, @bot mentions, or replies to the bot.
            # Ignore all other messages (mirrors Telegram's default bot privacy mode).
            if group_chat and not bot_is_addressed(msg, bot_username, check_text):
                continue

            # --- Session authorization logic ---
            state.setdefault("pending_authorizations", {})
            state.setdefault("blocked_chat_ids", [])

            # ── BLOCKED ──────────────────────────────────────────────────────────────
            if is_blocked(state, from_chat):
                # Permanently blocked — silent reject for both private and group
                log.info(
                    f"Silently rejected message from permanently blocked "
                    f"@{from_user} (chat_id: {from_chat})"
                )

            # ── KNOWN / AUTHORIZED SESSION ───────────────────────────────────────────
            elif from_chat in chat_ids:
                # Per-user gate in groups: authorization is granted to individual
                # users (identified by their Telegram user-id == private chat-id).
                # A member of an authorized group who is NOT themselves authorized
                # should not be able to run commands or forward messages to the
                # agent inbox — silently drop their messages.
                if group_chat and from_user_id not in chat_ids:
                    log.info(
                        f"Ignoring message from non-authorized user @{from_user} "
                        f"(user_id={from_user_id}) in authorized group {from_chat}"
                    )
                    continue

                _process_authorized_message(
                    token,
                    msg,
                    text,
                    from_user,
                    from_chat,
                    chat_history,
                    inbox_items,
                    state,
                    history_key,
                    bot_username,
                )

            # ── GROUP CHAT — special auth path ───────────────────────────────────────
            elif group_chat:
                # Groups are never auto-discovered and never get passcode challenges.
                # A group becomes authorized when an already-authorized user (identified
                # by their Telegram user-ID, which equals their private chat-ID) sends
                # a message in it.  That user's private chat-ID must already be in
                # chat_ids for the check to succeed.
                if from_user_id and from_user_id in chat_ids:
                    # Sender is an authorized user → auto-authorize this group
                    chat_ids.append(from_chat)
                    keepass_store(
                        KEEPASS_TELEGRAM_CHAT_ID,
                        "telegram",
                        serialize_chat_ids(chat_ids),
                        group="System",
                    )
                    log.info(
                        f"Auto-authorized group {from_chat} because member "
                        f"@{from_user} (user_id={from_user_id}) is an authorized user"
                    )
                    _process_authorized_message(
                        token,
                        msg,
                        text,
                        from_user,
                        from_chat,
                        chat_history,
                        inbox_items,
                        state,
                        history_key,
                        bot_username,
                    )
                else:
                    # Group not authorized — tell them how to add it
                    log.info(
                        f"Rejected group message from @{from_user} "
                        f"in unauthorized group {from_chat}"
                    )
                    tg(
                        token,
                        "sendMessage",
                        chat_id=from_chat,
                        text=(
                            "🔒 *This group is not authorized.*\n\n"
                            "An already-authorized user needs to send any message here "
                            "to grant this group access to the bot."
                        ),
                        parse_mode="Markdown",
                    )

            # ── PRIVATE CHAT — owner auto-discovery (first-ever DM) ─────────────────
            elif not chat_ids and from_chat:
                # First-ever private message: auto-accept as owner (no passcode)
                log.info(f"Auto-discovered owner: @{from_user} (chat_id: {from_chat})")
                owner_username = from_user
                chat_ids.append(from_chat)
                keepass_store(
                    KEEPASS_TELEGRAM_OWNER_USERNAME,
                    "telegram",
                    from_user,
                    group="System",
                )
                keepass_store(
                    KEEPASS_TELEGRAM_CHAT_ID,
                    "telegram",
                    serialize_chat_ids(chat_ids),
                    group="System",
                )
                log.info("Owner username and chat ID saved to KeePass.")
                _process_authorized_message(
                    token,
                    msg,
                    text,
                    from_user,
                    from_chat,
                    chat_history,
                    inbox_items,
                    state,
                    history_key,
                    bot_username,
                )

            # ── PRIVATE CHAT — active passcode challenge ─────────────────────────────
            elif from_chat in state["pending_authorizations"]:
                pending = state["pending_authorizations"][from_chat]

                if check_text and strip_bot_suffix(
                    check_text.strip().lower().split()[0], bot_username
                ) in ("/start", "/help"):
                    # /start or /help during challenge — remind them
                    remaining = MAX_PASSCODE_ATTEMPTS - pending.get("attempts", 0)
                    tg(
                        token,
                        "sendMessage",
                        chat_id=from_chat,
                        parse_mode="Markdown",
                        text=(
                            "🔐 *Access pending*\n\n"
                            "A 4-digit passcode was sent to the bot owner.\n"
                            "Please enter that passcode here to gain access.\n\n"
                            f"_{remaining} attempt{'s' if remaining != 1 else ''} remaining_"
                        ),
                    )
                elif check_text and check_text.strip() == pending["passcode"]:
                    # ✅ Correct passcode — authorize
                    del state["pending_authorizations"][from_chat]
                    chat_ids.append(from_chat)
                    keepass_store(
                        KEEPASS_TELEGRAM_CHAT_ID,
                        "telegram",
                        serialize_chat_ids(chat_ids),
                        group="System",
                    )
                    log.info(
                        f"Passcode accepted — authorized new session: "
                        f"@{from_user} (chat_id: {from_chat})"
                    )
                    welcome = "✅ Correct! You've been authorized. I'll forward messages to you from now on."
                    tg(token, "sendMessage", chat_id=from_chat, text=welcome)
                    append_chat_message(chat_history, history_key, "bot", welcome)
                    # Passcode text itself is NOT forwarded to the agent inbox.
                else:
                    # ❌ Wrong passcode
                    pending["attempts"] = pending.get("attempts", 0) + 1
                    remaining = MAX_PASSCODE_ATTEMPTS - pending["attempts"]

                    if pending["attempts"] >= MAX_PASSCODE_ATTEMPTS:
                        # 5th wrong attempt → permanently block
                        blocked_user = pending.get("from_user", from_user)
                        del state["pending_authorizations"][from_chat]
                        add_block(state, from_chat, blocked_user)
                        log.warning(
                            f"Permanently blocked @{blocked_user} (chat_id: {from_chat}) "
                            f"after {MAX_PASSCODE_ATTEMPTS} failed passcode attempts"
                        )
                        tg(
                            token,
                            "sendMessage",
                            chat_id=from_chat,
                            text="🚫 Too many wrong attempts. You have been permanently blocked.",
                        )
                        if chat_ids:
                            tg(
                                token,
                                "sendMessage",
                                chat_id=chat_ids[0],
                                text=(
                                    f"🚫 @{blocked_user} has been permanently blocked "
                                    f"after {MAX_PASSCODE_ATTEMPTS} failed passcode attempts."
                                ),
                            )
                    else:
                        log.info(
                            f"Wrong passcode from @{from_user} "
                            f"(attempt {pending['attempts']}/{MAX_PASSCODE_ATTEMPTS})"
                        )
                        tg(
                            token,
                            "sendMessage",
                            chat_id=from_chat,
                            text=(
                                f"❌ Wrong passcode. "
                                f"{remaining} attempt{'s' if remaining != 1 else ''} remaining."
                            ),
                        )

            # ── PRIVATE CHAT — new user mentions owner username → start challenge ────
            elif (
                from_chat
                and owner_username
                and check_text
                and contains_username(check_text, owner_username)
            ):
                if not chat_ids:
                    log.info(
                        f"Rejected @{from_user}: owner username mentioned but no owner chat_id stored"
                    )
                    tg(
                        token,
                        "sendMessage",
                        chat_id=from_chat,
                        text="Sorry, I can't verify you right now. Please try again later.",
                    )
                else:
                    passcode = generate_passcode()
                    state["pending_authorizations"][from_chat] = {
                        "passcode": passcode,
                        "attempts": 0,
                        "from_user": from_user,
                    }
                    owner_chat_id = chat_ids[0]
                    log.info(
                        f"Passcode challenge started for @{from_user} (chat_id: {from_chat}), "
                        f"passcode sent to owner (chat_id: {owner_chat_id})"
                    )
                    tg(
                        token,
                        "sendMessage",
                        chat_id=owner_chat_id,
                        parse_mode="Markdown",
                        text=(
                            f"🔐 *New access request*\n"
                            f"User @{from_user} (chat ID: `{from_chat}`) wants to connect.\n\n"
                            f"Passcode: *{passcode}*\n\n"
                            f"_(Max {MAX_PASSCODE_ATTEMPTS} attempts)_"
                        ),
                    )
                    tg(
                        token,
                        "sendMessage",
                        chat_id=from_chat,
                        text=(
                            "🔐 A 4-digit passcode has been sent to the owner.\n"
                            "Please enter it here to gain access:"
                        ),
                    )

            # ── PRIVATE CHAT — unknown user, no owner-username mention ───────────────
            else:
                is_start_cmd = check_text and strip_bot_suffix(
                    check_text.strip().lower().split()[0], bot_username
                ) in ("/start",)
                if is_start_cmd:
                    log.info(
                        f"/start from unauthorized @{from_user} (chat_id: {from_chat})"
                    )
                    tg(
                        token,
                        "sendMessage",
                        chat_id=from_chat,
                        text="Enter the bot owner username to start.",
                    )
                else:
                    log.info(
                        f"Rejected message from @{from_user} (chat_id: {from_chat})"
                    )
                    tg(
                        token,
                        "sendMessage",
                        chat_id=from_chat,
                        text="Sorry, I don't know you. Please include my owner's username in your message to get access.",
                    )

        except Exception as e:
            log.error(f"Error processing update {uid}: {e}", exc_info=True)
            continue

    return owner_username, chat_ids, inbox_items


def poll_updates_and_persist(
    token: str,
    state: dict,
    owner_username: str | None,
    chat_ids: list[str],
    chat_history: dict,
    bot_username: str = "",
):
    """Poll for updates, write to inbox AND persist to history."""
    owner_username, chat_ids, items = poll_updates(
        token, state, owner_username, chat_ids, chat_history, bot_username
    )
    if items:
        if write_to_inbox(items):
            append_to_inbox_history(items)
            save_chat_history(chat_history)
        else:
            log.error(
                f"Inbox write failed for {len(items)} item(s) — skipping history/chat save to allow retry"
            )
    return owner_username, chat_ids


# write_to_inbox → imported from shared module
# append_to_inbox_history → uses shared.append_to_history


def append_to_inbox_history(items: list):
    """Persist received Telegram messages to a history file (never cleared)."""
    append_to_history(items, INBOX_HISTORY_FILE)


# ---------------------------------------------------------------------------
# Outgoing messages (outbox.json → Telegram)
# ---------------------------------------------------------------------------


def send_outbox_messages(
    token: str, chat_ids: list[str], state: dict, chat_history: dict
):
    """Forward unsent outbox messages to all authorized Telegram chats."""
    from services.shared import read_outbox_locked

    outbox = read_outbox_locked()
    if not outbox:
        return

    sent_count = 0
    sent_history_batch: list[dict] = []
    for msg in outbox:
        h = msg_hash(msg)
        if h in state["sent_hashes"]:
            continue  # already sent

        text = _format_outbox_msg(msg)

        # Telegram max message length is 4096 chars
        if len(text) > 4000:
            text = text[:3997] + "..."

        succeeded_cids = []
        for cid in chat_ids:
            result = tg(
                token, "sendMessage", chat_id=cid, text=text, parse_mode="Markdown"
            )
            if result:
                succeeded_cids.append(cid)
            else:
                # Try without Markdown (preserve backtick-wrapped content as-is)
                fallback_text = _strip_markdown_preserve_code(text)
                result2 = tg(token, "sendMessage", chat_id=cid, text=fallback_text)
                if result2:
                    succeeded_cids.append(cid)

        if succeeded_cids:
            state["sent_hashes"].append(h)
            sent_count += 1
            log.info(
                f"Sent to Telegram ({len(succeeded_cids)} chat(s)): {msg.get('subject', text[:60])!r}"
            )
            sent_history_batch.append(msg)
            for cid in succeeded_cids:
                append_chat_message(chat_history, cid, "bot", text)

    # Cap hash list to last 1000 to prevent unbounded growth
    state["sent_hashes"] = state["sent_hashes"][-1000:]

    # Batch-write all sent messages to outbox history in one operation
    if sent_history_batch:
        append_to_history(sent_history_batch, OUTBOX_HISTORY_FILE)

    if sent_count:
        save_chat_history(chat_history)
        log.info(
            f"Forwarded {sent_count} outbox message(s) to {len(chat_ids)} Telegram chat(s)."
        )


def _format_outbox_msg(msg: dict) -> str:
    """Convert an outbox dict to a readable Telegram message."""
    msg_type = msg.get("type", "")
    subject = msg.get("subject", "")
    content = msg.get("content", "")

    lines = []

    # Type badge
    type_badges = {
        "needs_human": "🚨 *ACTION REQUIRED*",
        "goal_complete": "✅ *Goal Completed*",
        "goal_failed": "❌ *Goal Failed*",
        "status": "📊 *Status Report*",
    }
    if msg_type in type_badges:
        lines.append(type_badges[msg_type])

    if subject:
        lines.append(f"*{subject}*")

    if content:
        lines.append(content)

    formatted = "\n".join(lines) if lines else json.dumps(msg, indent=2)
    return protect_urls_in_markdown(formatted)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    # --- Singleton lock: prevent multiple instances from running simultaneously ---
    # Open in append mode so existing content (PID) is not truncated before we read it
    lock_fh = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fh.seek(0)
        existing_pid = lock_fh.read().strip() or "unknown"
        log.error(
            f"Another telegram_bridge instance is already running (lock held by PID {existing_pid}). "
            "Exiting to prevent Telegram API conflicts."
        )
        lock_fh.close()
        sys.exit(1)
    # Lock acquired — overwrite file with our PID for diagnostics
    lock_fh.seek(0)
    lock_fh.truncate()
    lock_fh.write(str(os.getpid()))
    lock_fh.flush()

    log.info("=" * 60)
    log.info("Telegram Bridge starting up")
    log.info("=" * 60)

    token = keepass_get(KEEPASS_TELEGRAM_BOT_TOKEN)
    if not token:
        log.error(
            f"TELEGRAM_BOT_TOKEN not found in KeePass. Setup instructions:\n"
            "1. Open Telegram and message @BotFather\n"
            "2. Send /newbot and follow the prompts to create your bot\n"
            "3. Copy the bot token BotFather gives you\n"
            "4. Store it in KeePass via the Credentials tab "
            "(Title: TELEGRAM_BOT_TOKEN, Username: bot, Password: <your_token>)\n"
            "   Or via CLI: uv run python scripts/keepass.py store "
            "--title 'TELEGRAM_BOT_TOKEN' --username bot --password '<your_token>'\n"
            "5. Start the telegram_bridge service again\n"
            "6. Message the bot on Telegram — it will auto-discover your chat ID"
        )
        sys.exit(1)

    # Verify token by calling getMe
    me = tg(token, "getMe")
    if me:
        log.info(f"Bot authenticated: @{me.get('username')} ({me.get('first_name')})")
    else:
        log.error("Failed to authenticate with Telegram. Check your bot token.")
        sys.exit(1)
    bot_username: str = (me or {}).get("username", "") if me else ""
    # A missing bot_username would make strip_bot_suffix() unconditionally strip
    # any @suffix — allowing this bot to handle commands addressed to OTHER bots
    # in a shared group (e.g. `/goals@OtherBot`).  Refuse to start in that case.
    if not bot_username:
        log.error(
            "getMe returned no username — cannot safely run in groups without knowing "
            "our own bot handle. Check the bot token / BotFather setup."
        )
        sys.exit(1)

    owner_username = keepass_get(KEEPASS_TELEGRAM_OWNER_USERNAME)
    if owner_username:
        log.info(f"Owner username: @{owner_username}")

    chat_ids_raw = keepass_get(KEEPASS_TELEGRAM_CHAT_ID)
    chat_ids = parse_chat_ids(chat_ids_raw)
    if chat_ids:
        log.info(f"Using saved chat_ids: {chat_ids}")
    else:
        log.info("No chat_ids saved. Message the bot on Telegram to auto-discover it.")

    state = load_state()
    chat_history = load_chat_history()
    last_outbox_check = 0.0

    # Graceful shutdown on SIGTERM (sent by service-manager stop)
    shutdown_requested = False

    def _handle_sigterm(signum, frame):
        nonlocal shutdown_requested
        log.info(f"Received signal {signum}, requesting graceful shutdown...")
        shutdown_requested = True

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    log.info(f"Loaded chat history for {len(chat_history)} chat(s)")
    log.info("Entering main loop (Ctrl+C or SIGTERM to stop)...")
    consecutive_errors = 0
    MAX_BACKOFF = 300  # 5 minutes cap
    try:
        while not shutdown_requested:
            try:
                # 1. Poll Telegram for incoming messages
                owner_username, chat_ids = poll_updates_and_persist(
                    token, state, owner_username, chat_ids, chat_history, bot_username
                )
                try:
                    save_state(state)
                except Exception as e:
                    log.error(f"Failed to save state after polling: {e}", exc_info=True)

                # 2. Forward outbox messages periodically
                now = time.time()
                if chat_ids and (now - last_outbox_check >= OUTBOX_INTERVAL):
                    send_outbox_messages(token, chat_ids, state, chat_history)
                    try:
                        save_state(state)
                    except Exception as e:
                        log.error(
                            f"Failed to save state after outbox send: {e}",
                            exc_info=True,
                        )
                    last_outbox_check = now

                # Reset backoff on successful iteration
                consecutive_errors = 0
                _write_heartbeat()

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
            save_state(state)
            save_chat_history(chat_history)
            log.info("State saved successfully. Goodbye.")
        except Exception as e:
            log.error(f"Failed to save state on shutdown: {e}", exc_info=True)
        # Release singleton lock
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
