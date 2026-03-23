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
       uv run python scripts/service-manager.py start telegram_bridge -- uv run python services/telegram_bridge.py
  4. Message the bot on Telegram — it will auto-discover your chat ID
  5. All future outbox messages will be forwarded to you on Telegram

Management:
  uv run python scripts/service-manager.py status telegram_bridge   # check status
  uv run python scripts/service-manager.py stop telegram_bridge     # stop service
  uv run python scripts/service-manager.py list                     # list all services
"""

import json
import hashlib
import re
import time
import subprocess
import sys
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# --- Paths ---
BASE = Path("/agent")
STATE_FILE         = BASE / "memory" / "telegram_state.json"
INBOX_FILE         = BASE / "messages" / "inbox.json"
OUTBOX_FILE        = BASE / "messages" / "outbox.json"
INBOX_HISTORY_FILE  = BASE / "memory" / "telegram_inbox_history.json"
OUTBOX_HISTORY_FILE = BASE / "memory" / "outbox_history.json"
CHAT_HISTORY_FILE   = BASE / "memory" / "telegram_chat_history.json"
LOG_DIR             = BASE / "memory" / "logs"
LOG_FILE            = LOG_DIR / "telegram_bridge.log"

LOG_DIR.mkdir(parents=True, exist_ok=True)

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

# --- Constants ---
KEEPASS_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
KEEPASS_TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"
KEEPASS_TELEGRAM_OWNER_USERNAME = "TELEGRAM_OWNER_USERNAME"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT  = 25   # Telegram long-poll timeout (seconds)
OUTBOX_INTERVAL = 60  # How often to check outbox (seconds)
STATE_JSON = BASE / "memory" / "state.json"
MEDIA_DIR = BASE / "workspace" / "telegram"
TELEGRAM_FILE_API = "https://api.telegram.org/file/bot{token}/{file_path}"


# ---------------------------------------------------------------------------
# Heartbeat helpers
# ---------------------------------------------------------------------------

def get_last_heartbeat() -> datetime | None:
    """Return the last_heartbeat timestamp from state.json, or None."""
    try:
        state = json.loads(STATE_JSON.read_text())
        raw = state.get("last_heartbeat")
        if raw:
            return datetime.fromisoformat(raw)
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
        r = subprocess.run(
            ["uv", "run", "python", "/agent/scripts/keepass.py", "--json", "get", title],
            capture_output=True, text=True, cwd="/agent", timeout=15,
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            return data.get("password") or data.get("Password")
    except Exception as e:
        log.warning(f"KeePass get({title!r}) failed: {e}")
    return None


def keepass_store(title: str, username: str, value: str, group: str = "API Keys") -> bool:
    try:
        r = subprocess.run(
            ["uv", "run", "python", "/agent/scripts/keepass.py", "store",
             "--title", title, "--username", username, "--password", value, "--group", group],
            capture_output=True, text=True, cwd="/agent", timeout=15,
        )
        return r.returncode == 0
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
    pattern = r'(?<!\w)@?' + re.escape(username) + r'\b'
    return bool(re.search(pattern, text, re.IGNORECASE))


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    default = {"last_update_id": 0, "sent_hashes": []}
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return default


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


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
    url_pattern = r'(?<!`)(?<!`)\b(https?://[^\s`]+)(?!`)(?!`)'

    def wrap_url(match):
        url = match.group(1)
        if url.endswith('`'):
            return url
        return f'`{url}`'

    return re.sub(url_pattern, wrap_url, text)


def _strip_markdown_preserve_code(text: str) -> str:
    """Strip Markdown formatting but preserve backtick-wrapped content.

    Used as fallback when Telegram's Markdown parser fails. Removes
    bold (*) and italic (_) markers but keeps backtick-wrapped URLs intact.
    """
    # Extract backtick-wrapped content (URLs, code blocks)
    backtick_pattern = r'`([^`]+)`'
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
        CHAT_HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    except Exception as e:
        log.warning(f"Failed to save chat history: {e}")


def append_chat_message(history: dict, chat_id: str, role: str, text: str):
    """Append a message to a chat's history, keeping last 50 per chat."""
    if chat_id not in history:
        history[chat_id] = []
    history[chat_id].append({
        "role": role,
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
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

def tg(token: str, method: str, **params):
    """Call the Telegram Bot API. Returns result on success, None on failure."""
    url = TELEGRAM_API.format(token=token, method=method)
    try:
        resp = requests.post(url, json=params, timeout=POLL_TIMEOUT + 5)
        data = resp.json()
        if data.get("ok"):
            return data["result"]
        log.warning(f"Telegram API error ({method}): {data.get('description')}")
    except requests.exceptions.Timeout:
        pass  # normal for long-poll
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
    if msg.get("photo"):
        largest = msg["photo"][-1]
        fid = largest.get("file_id")
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


def download_telegram_file(token: str, file_id: str, chat_id: str, filename_hint: str) -> str | None:
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

    try:
        resp = requests.get(url, timeout=60, stream=True)
        resp.raise_for_status()
        size = 0
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                size += len(chunk)
    except Exception as e:
        log.error(f"Failed to download file {remote_path}: {e}")
        dest_path.unlink(missing_ok=True)
        return None

    log.info(f"Saved media: {dest_path} ({size} bytes)")
    return str(dest_path)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def handle_heartbeat_command(token: str, from_chat: str, from_user: str, chat_history: dict, extra_args: list[str] = None) -> bool:
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
            cwd="/agent"
        )

        if result.returncode == 0:
            response = "✅ Heartbeat triggered successfully!\n\nThe agent cycle is now running."
            log.info(f"Heartbeat completed successfully for @{from_user}")
        else:
            response = f"⚠️ Heartbeat triggered but returned non-zero exit code: {result.returncode}\n\nCheck logs for details."
            log.warning(f"Heartbeat failed with code {result.returncode}: {result.stderr}")

        tg(token, "sendMessage", chat_id=from_chat, text=response)
        append_chat_message(chat_history, from_chat, "bot", response)
        return True

    except subprocess.TimeoutExpired:
        response = "⏱️ Heartbeat timed out after 5 minutes. The cycle may still be running."
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
# Incoming messages (Telegram → inbox.json)
# ---------------------------------------------------------------------------

def _process_authorized_message(
    token: str, msg: dict, text: str, from_user: str, from_chat: str,
    chat_history: dict, inbox_items: list,
):
    """Process an authorized incoming message (text and/or media) into an inbox item."""
    # Check for commands first
    if text and text.startswith("/"):
        parts = text.split()
        command = parts[0].lower()

        if command == "/heartbeat":
            # Extract any additional arguments (e.g., /heartbeat --agent-sleep)
            extra_args = parts[1:] if len(parts) > 1 else []
            handle_heartbeat_command(token, from_chat, from_user, chat_history, extra_args)
            return  # Don't process as regular message

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
            attachments.append({
                "type": media_type,
                "path": local_path,
                "filename": Path(local_path).name,
            })

    # Build content string
    context = build_chat_context(chat_history, from_chat)
    base_content = f"[Telegram @{from_user}]: {effective_text}" if effective_text else f"[Telegram @{from_user}]:"

    if attachments:
        attachment_lines = "\n".join(
            f"- {a['type']}: {a['path']}" for a in attachments
        )
        base_content += f"\n[Attachments]\n{attachment_lines}"

    if context:
        content = f"[Previous conversation context (last 24h)]\n{context}\n[End of context]\n\n[New message]\n{base_content}"
    else:
        content = base_content

    chat_text = effective_text or "(media)"
    append_chat_message(chat_history, from_chat, "user", chat_text)

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
    log.info(f"Received from @{from_user}: {chat_text[:100]}" + (f" (+{len(attachments)} attachment(s))" if attachments else ""))

    ack = build_ack_message()
    tg(token, "sendMessage", chat_id=from_chat, text=ack)
    append_chat_message(chat_history, from_chat, "bot", ack)
    log.info(f"Sent ack reply to {from_chat}")


def poll_updates(token: str, state: dict, owner_username: str | None, chat_ids: list[str], chat_history: dict) -> tuple[str | None, list[str], list]:  # noqa: E501
    """
    Long-poll Telegram for new updates.
    Returns (updated_owner_username, updated_chat_ids, list_of_inbox_items).
    """
    updates = tg(
        token, "getUpdates",
        offset=state["last_update_id"] + 1,
        timeout=POLL_TIMEOUT,
        limit=100,
    )
    if not updates:
        return owner_username, chat_ids, []

    inbox_items = []
    for update in updates:
        uid = update["update_id"]
        state["last_update_id"] = max(state["last_update_id"], uid)

        msg = update.get("message") or update.get("channel_post")
        if not msg:
            continue

        from_chat = str(msg.get("chat", {}).get("id", ""))
        from_info = msg.get("from", {})
        from_user = (
            from_info.get("username")
            or from_info.get("first_name", "user")
        )
        text = msg.get("text", "").strip()
        caption = msg.get("caption", "").strip()
        check_text = text or caption  # for auth checks on media-with-caption

        # --- Session authorization logic ---

        if not chat_ids and from_chat:
            # First-ever message: auto-accept as owner
            log.info(f"Auto-discovered owner: @{from_user} (chat_id: {from_chat})")
            owner_username = from_user
            chat_ids.append(from_chat)
            keepass_store(KEEPASS_TELEGRAM_OWNER_USERNAME, "telegram", from_user, group="System")
            keepass_store(KEEPASS_TELEGRAM_CHAT_ID, "telegram", serialize_chat_ids(chat_ids), group="System")
            log.info("Owner username and chat ID saved to KeePass.")
            _process_authorized_message(token, msg, text, from_user, from_chat, chat_history, inbox_items)

        elif from_chat in chat_ids:
            # Known session — process normally
            _process_authorized_message(token, msg, text, from_user, from_chat, chat_history, inbox_items)

        elif from_chat and owner_username and check_text and contains_username(check_text, owner_username):
            # New session authorized — message contains owner's username
            log.info(f"Authorized new session: @{from_user} (chat_id: {from_chat})")
            chat_ids.append(from_chat)
            keepass_store(KEEPASS_TELEGRAM_CHAT_ID, "telegram", serialize_chat_ids(chat_ids), group="System")

            tg(token, "sendMessage", chat_id=from_chat,
               text="Welcome! You've been authorized. I'll forward messages to you from now on.")
            append_chat_message(chat_history, from_chat, "bot", "Welcome! You've been authorized. I'll forward messages to you from now on.")
            _process_authorized_message(token, msg, text, from_user, from_chat, chat_history, inbox_items)

        else:
            # Unauthorized new session — reject
            log.info(f"Rejected message from @{from_user} (chat_id: {from_chat})")
            tg(token, "sendMessage", chat_id=from_chat,
               text="Sorry, I don't know you. Please include my owner's username in your message to get access.")

    return owner_username, chat_ids, inbox_items


def poll_updates_and_persist(token: str, state: dict, owner_username: str | None, chat_ids: list[str], chat_history: dict):
    """Poll for updates, write to inbox AND persist to history."""
    owner_username, chat_ids, items = poll_updates(token, state, owner_username, chat_ids, chat_history)
    if items:
        write_to_inbox(items)
        append_to_inbox_history(items)
        save_chat_history(chat_history)
    return owner_username, chat_ids


def write_to_inbox(items: list):
    if not items:
        return
    try:
        existing = []
        if INBOX_FILE.exists():
            raw = INBOX_FILE.read_text().strip()
            existing = json.loads(raw) if raw else []
        existing.extend(items)
        INBOX_FILE.write_text(json.dumps(existing, indent=2))
        log.info(f"Added {len(items)} message(s) to inbox.json")
    except Exception as e:
        log.error(f"Failed to write inbox: {e}")


def append_to_inbox_history(items: list):
    """Persist received Telegram messages to a history file (never cleared)."""
    if not items:
        return
    try:
        history = []
        if INBOX_HISTORY_FILE.exists():
            raw = INBOX_HISTORY_FILE.read_text().strip()
            history = json.loads(raw) if raw else []
        history.extend(items)
        # Keep last 500 messages to prevent unbounded growth
        history = history[-500:]
        INBOX_HISTORY_FILE.write_text(json.dumps(history, indent=2))
    except Exception as e:
        log.warning(f"Failed to write inbox history: {e}")


# ---------------------------------------------------------------------------
# Outgoing messages (outbox.json → Telegram)
# ---------------------------------------------------------------------------

def send_outbox_messages(token: str, chat_ids: list[str], state: dict, chat_history: dict):
    """Forward unsent outbox messages to all authorized Telegram chats."""
    if not OUTBOX_FILE.exists():
        return
    try:
        raw = OUTBOX_FILE.read_text().strip()
        outbox = json.loads(raw) if raw else []
    except Exception as e:
        log.warning(f"Failed to read outbox: {e}")
        return

    if not isinstance(outbox, list) or not outbox:
        return

    sent_count = 0
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
            result = tg(token, "sendMessage",
                        chat_id=cid,
                        text=text,
                        parse_mode="Markdown")
            if result:
                succeeded_cids.append(cid)
            else:
                # Try without Markdown (preserve backtick-wrapped content as-is)
                fallback_text = _strip_markdown_preserve_code(text)
                result2 = tg(token, "sendMessage",
                             chat_id=cid,
                             text=fallback_text)
                if result2:
                    succeeded_cids.append(cid)

        if succeeded_cids:
            state["sent_hashes"].append(h)
            sent_count += 1
            log.info(f"Sent to Telegram ({len(succeeded_cids)} chat(s)): {msg.get('subject', text[:60])!r}")
            _append_outbox_history(msg)
            for cid in succeeded_cids:
                append_chat_message(chat_history, cid, "bot", text)

    # Cap hash list to last 1000 to prevent unbounded growth
    state["sent_hashes"] = state["sent_hashes"][-1000:]

    if sent_count:
        save_chat_history(chat_history)
        log.info(f"Forwarded {sent_count} outbox message(s) to {len(chat_ids)} Telegram chat(s).")


def _append_outbox_history(msg: dict):
    """Append a sent outbox message to the persistent history (if not duplicate)."""
    try:
        history = []
        if OUTBOX_HISTORY_FILE.exists():
            raw = OUTBOX_HISTORY_FILE.read_text().strip()
            history = json.loads(raw) if raw else []
        # Avoid duplicating messages already in history
        existing_subjects = {m.get("subject") for m in history}
        if msg.get("subject") and msg["subject"] in existing_subjects:
            return
        history.append(msg)
        history = history[-500:]
        OUTBOX_HISTORY_FILE.write_text(json.dumps(history, indent=2))
    except Exception as e:
        log.warning(f"Failed to append outbox history: {e}")


def _format_outbox_msg(msg: dict) -> str:
    """Convert an outbox dict to a readable Telegram message."""
    msg_type = msg.get("type", "")
    subject  = msg.get("subject", "")
    content  = msg.get("content", "")

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

    log.info(f"Loaded chat history for {len(chat_history)} chat(s)")
    log.info("Entering main loop (Ctrl+C to stop)...")
    while True:
        try:
            # 1. Poll Telegram for incoming messages
            owner_username, chat_ids = poll_updates_and_persist(token, state, owner_username, chat_ids, chat_history)
            save_state(state)

            # 2. Forward outbox messages periodically
            now = time.time()
            if chat_ids and (now - last_outbox_check >= OUTBOX_INTERVAL):
                send_outbox_messages(token, chat_ids, state, chat_history)
                save_state(state)
                last_outbox_check = now

        except KeyboardInterrupt:
            log.info("Shutdown requested. Goodbye.")
            break
        except Exception as e:
            log.error(f"Unexpected error in main loop: {e}", exc_info=True)
            time.sleep(10)  # back off before retrying


if __name__ == "__main__":
    main()