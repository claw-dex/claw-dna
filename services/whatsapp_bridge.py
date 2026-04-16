#!/usr/bin/env python3
"""
WhatsApp Bridge Service
=======================
Bridges the agent's inbox/outbox with WhatsApp via the Business Cloud API.

- Runs a webhook HTTP server on port 8083 to receive incoming WhatsApp messages
- Polls outbox.json every 60s and sends unsent messages to WhatsApp
- Tracks sent message hashes to prevent duplicates (survives restarts)
- Auto-discovers owner from the first message received

Setup:
  1. Create a Meta Developer account and set up a WhatsApp Business app
     → Get your Access Token, Phone Number ID, and choose a Verify Token
  2. Store credentials in KeePass:
       uv run python scripts/keepass.py store --title "WHATSAPP_ACCESS_TOKEN" --username whatsapp --password "<token>"
       uv run python scripts/keepass.py store --title "WHATSAPP_PHONE_NUMBER_ID" --username whatsapp --password "<phone_number_id>"
       uv run python scripts/keepass.py store --title "WHATSAPP_VERIFY_TOKEN" --username whatsapp --password "<your_secret>"
  3. Start via service manager:
       uv run python scripts/service_manager.py start whatsapp_bridge 8083 -- uv run python services/whatsapp_bridge.py
  4. Configure the webhook URL in Meta Developer Portal:
       https://<public-url>/system/whatsapp-bridge/webhook
     with your chosen verify token
  5. Send a WhatsApp message to the business number — it will auto-discover your phone

Management:
  uv run python scripts/service_manager.py status whatsapp_bridge   # check status
  uv run python scripts/service_manager.py stop whatsapp_bridge     # stop service
  uv run python scripts/service_manager.py list                     # list all services
"""

import json
import hashlib
import os
import re
import signal
import tempfile
import time
import subprocess
import sys
import logging
import uuid
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

from shared import atomic_write_json, write_to_inbox, append_to_history

# --- Paths ---
BASE = Path("/agent")
STATE_FILE = BASE / "memory" / "whatsapp_state.json"
INBOX_FILE = BASE / "messages" / "inbox.json"
OUTBOX_FILE = BASE / "messages" / "outbox.json"
INBOX_HISTORY_FILE = BASE / "memory" / "whatsapp_inbox_history.json"
OUTBOX_HISTORY_FILE = BASE / "memory" / "whatsapp_outbox_history.json"
CHAT_HISTORY_FILE = BASE / "memory" / "whatsapp_chat_history.json"
LOG_DIR = BASE / "memory" / "logs"
LOG_FILE = LOG_DIR / "whatsapp_bridge.log"
HEARTBEAT_DIR = BASE / "memory" / "heartbeats"
HEARTBEAT_FILE = HEARTBEAT_DIR / "whatsapp_bridge.heartbeat"

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
log = logging.getLogger("whatsapp_bridge")


def _write_heartbeat():
    """Write current epoch timestamp to heartbeat file for liveness detection."""
    try:
        HEARTBEAT_FILE.write_text(str(time.time()))
    except OSError:
        pass  # non-critical


# --- Constants ---
KEEPASS_WHATSAPP_ACCESS_TOKEN = "WHATSAPP_ACCESS_TOKEN"
KEEPASS_WHATSAPP_PHONE_NUMBER_ID = "WHATSAPP_PHONE_NUMBER_ID"
KEEPASS_WHATSAPP_VERIFY_TOKEN = "WHATSAPP_VERIFY_TOKEN"
KEEPASS_WHATSAPP_CHAT_ID = "WHATSAPP_CHAT_ID"
KEEPASS_WHATSAPP_OWNER_USERNAME = "WHATSAPP_OWNER_USERNAME"

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"
WEBHOOK_PORT = 8083
WEBHOOK_PATH = "/system/whatsapp-bridge/webhook"
OUTBOX_INTERVAL = 60  # How often to check outbox (seconds)
STATE_JSON = BASE / "memory" / "state.json"
MEDIA_DIR = BASE / "workspace" / "whatsapp"

# Thread lock for shared state
_lock = threading.RLock()


# _atomic_write_json → imported from shared module as atomic_write_json


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
    """Parse a comma-separated string of phone numbers into a list."""
    if not csv_string:
        return []
    return [cid.strip() for cid in csv_string.split(",") if cid.strip()]


def serialize_chat_ids(chat_ids: list[str]) -> str:
    """Serialize a list of phone numbers into a comma-separated string."""
    return ",".join(chat_ids)


def contains_username(text: str, username: str) -> bool:
    """Check if text contains the username as a whole word, case-insensitive."""
    pattern = r"(?<!\w)@?" + re.escape(username) + r"\b"
    return bool(re.search(pattern, text, re.IGNORECASE))


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _validate_state(data: dict) -> dict:
    """Ensure state has the expected structure, repairing wrong types."""
    default = {"last_message_ts": "", "sent_hashes": []}
    if not isinstance(data, dict):
        log.warning(
            "WhatsApp state has unexpected type %s, resetting", type(data).__name__
        )
        return dict(default)
    # Validate last_message_ts — must be str
    ts = data.get("last_message_ts")
    if not isinstance(ts, str):
        log.warning(
            "WhatsApp state last_message_ts has wrong type %s, resetting to ''",
            type(ts).__name__,
        )
        data["last_message_ts"] = ""
    # Validate sent_hashes — must be list of strings
    hashes = data.get("sent_hashes")
    if not isinstance(hashes, list):
        log.warning(
            "WhatsApp state sent_hashes has wrong type %s, resetting to []",
            type(hashes).__name__,
        )
        data["sent_hashes"] = []
    else:
        cleaned = [h for h in hashes if isinstance(h, str)]
        if len(cleaned) != len(hashes):
            log.warning(
                "Removed %d non-string entries from sent_hashes",
                len(hashes) - len(cleaned),
            )
            data["sent_hashes"] = cleaned
    return data


def load_state() -> dict:
    default = {"last_message_ts": "", "sent_hashes": []}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return _validate_state(data)
        except Exception as e:
            log.warning("Corrupt WhatsApp state file, using defaults: %s", e)
    return dict(default)


def save_state(state: dict):
    atomic_write_json(STATE_FILE, state, indent=2)


def msg_hash(msg: dict) -> str:
    """Stable 16-char hash of an outbox message to detect duplicates."""
    key = json.dumps(msg, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# WhatsApp formatting helpers
# ---------------------------------------------------------------------------


def _strip_wa_formatting(text: str) -> str:
    """Strip WhatsApp formatting characters as a plain-text fallback."""
    # Remove bold, italic, strikethrough markers
    text = text.replace("*", "").replace("_", "").replace("~", "")
    # Remove monospace triple backticks
    text = re.sub(r"```", "", text)
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
# WhatsApp API
# ---------------------------------------------------------------------------


def wa_send_message(
    token: str, phone_number_id: str, to: str, text: str
) -> dict | None:
    """Send a text message via WhatsApp Cloud API. Returns API response or None."""
    url = f"{GRAPH_API_BASE}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        try:
            data = resp.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            log.warning(
                f"WhatsApp send non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"
            )
            return None
        if resp.ok:
            return data
        log.warning(
            f"WhatsApp send failed ({resp.status_code}): {data.get('error', {}).get('message', data)}"
        )
    except Exception as e:
        log.error(f"WhatsApp send request failed: {e}")
    return None


def wa_send_read_receipt(token: str, phone_number_id: str, message_id: str):
    """Mark a message as read (blue ticks)."""
    url = f"{GRAPH_API_BASE}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        log.warning(f"Failed to send read receipt: {e}")


# ---------------------------------------------------------------------------
# Media download helpers
# ---------------------------------------------------------------------------


def extract_wa_media(msg: dict) -> list[tuple[str, str, str]]:
    """Extract downloadable media from a WhatsApp webhook message.

    Returns list of (media_id, media_type, filename_hint) tuples.
    Supports: image, document, audio, video.
    """
    media = []

    img = msg.get("image")
    if isinstance(img, dict):
        mid = img.get("id")
        mime = img.get("mime_type", "image/jpeg")
        ext = mime.split("/")[-1].split(";")[0]
        if mid:
            media.append((mid, "image", f"photo.{ext}"))

    doc = msg.get("document")
    if isinstance(doc, dict):
        mid = doc.get("id")
        if mid:
            fname = doc.get("filename") or f"document_{mid[:8]}"
            media.append((mid, "document", fname))

    aud = msg.get("audio")
    if isinstance(aud, dict):
        mid = aud.get("id")
        mime = aud.get("mime_type", "audio/ogg")
        ext = mime.split("/")[-1].split(";")[0]
        if mid:
            media.append((mid, "audio", f"audio.{ext}"))

    vid = msg.get("video")
    if isinstance(vid, dict):
        mid = vid.get("id")
        mime = vid.get("mime_type", "video/mp4")
        ext = mime.split("/")[-1].split(";")[0]
        if mid:
            media.append((mid, "video", f"video.{ext}"))

    return media


def download_wa_media(
    token: str, media_id: str, chat_id: str, filename_hint: str
) -> str | None:
    """Download a file from WhatsApp and save to /agent/workspace/whatsapp/{chat_id}/.

    Returns the local file path on success, None on failure.
    Two-step process: GET /{media_id} for URL, then download with Bearer auth.
    """
    headers = {"Authorization": f"Bearer {token}"}

    # Step 1: get the media URL
    try:
        resp = requests.get(f"{GRAPH_API_BASE}/{media_id}", headers=headers, timeout=30)
        resp.raise_for_status()
        try:
            media_data = resp.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            log.warning(
                f"Non-JSON media response for media_id={media_id[:16]} (HTTP {resp.status_code}): {resp.text[:200]}"
            )
            return None
        media_url = media_data.get("url")
        if not media_url:
            log.warning(f"No URL in media response for media_id={media_id[:16]}")
            return None
    except Exception as e:
        log.error(f"Failed to get media URL for media_id={media_id[:16]}: {e}")
        return None

    # Step 2: download the actual file (also needs Bearer auth)
    dest_dir = MEDIA_DIR / chat_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(filename_hint).name
    unique_name = f"{uuid.uuid4().hex[:12]}_{safe_name}"
    dest_path = dest_dir / unique_name

    max_size = 50 * 1024 * 1024  # 50 MB safety limit
    try:
        with requests.get(
            media_url, headers=headers, timeout=(30, 120), stream=True
        ) as resp:
            resp.raise_for_status()
            # Check Content-Length header if available for early rejection
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > max_size:
                log.warning(
                    f"Media {media_id[:16]} too large ({content_length} bytes), skipping"
                )
                return None
            size = 0
            exceeded = False
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    size += len(chunk)
                    if size > max_size:
                        log.warning(
                            f"Media {media_id[:16]} exceeded {max_size} byte limit at {size} bytes, aborting"
                        )
                        exceeded = True
                        break
                    f.write(chunk)
                else:
                    # Only reach here if loop completed without break (successful download)
                    f.flush()
                    os.fsync(f.fileno())
        if exceeded:
            dest_path.unlink(missing_ok=True)
            return None
    except Exception as e:
        log.error(f"Failed to download media {media_id[:16]}: {e}")
        dest_path.unlink(missing_ok=True)
        return None

    log.info(f"Saved media: {dest_path} ({size} bytes)")
    return str(dest_path)


# ---------------------------------------------------------------------------
# Caddy integration
# ---------------------------------------------------------------------------

CADDY_ADMIN = "http://localhost:2019"
CADDY_ROUTE_ID = "whatsapp-bridge-webhook"


def register_caddy_route():
    """Register the webhook route with the Caddy admin API."""
    route = {
        "@id": CADDY_ROUTE_ID,
        "match": [{"path": ["/system/whatsapp-bridge/webhook*"]}],
        "handle": [
            {
                "handler": "reverse_proxy",
                "upstreams": [{"dial": f"localhost:{WEBHOOK_PORT}"}],
            }
        ],
    }
    try:
        # First, remove any stale route with the same ID (ignore errors)
        requests.delete(f"{CADDY_ADMIN}/id/{CADDY_ROUTE_ID}", timeout=5)
        # Insert route at position 0 so it takes priority over catch-all groups
        resp = requests.put(
            f"{CADDY_ADMIN}/config/apps/http/servers/gateway/routes/0",
            json=route,
            timeout=10,
        )
        if resp.ok:
            log.info(
                f"Registered Caddy route: {WEBHOOK_PATH}* → localhost:{WEBHOOK_PORT}"
            )
        else:
            log.warning(
                f"Failed to register Caddy route ({resp.status_code}): {resp.text}"
            )
    except Exception as e:
        log.warning(f"Could not register Caddy route (Caddy may not be running): {e}")


def deregister_caddy_route():
    """Remove the webhook route from Caddy."""
    try:
        resp = requests.delete(
            f"{CADDY_ADMIN}/id/{CADDY_ROUTE_ID}",
            timeout=10,
        )
        if resp.ok:
            log.info("Deregistered Caddy route.")
        else:
            log.warning(
                f"Failed to deregister Caddy route ({resp.status_code}): {resp.text}"
            )
    except Exception as e:
        log.warning(f"Could not deregister Caddy route: {e}")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def handle_heartbeat_command(
    token: str,
    phone_number_id: str,
    from_phone: str,
    from_name: str,
    chat_history: dict,
    extra_args: list[str] = None,
) -> bool:
    """Handle the /heartbeat command by triggering an immediate heartbeat.

    Returns True if command was handled, False otherwise.
    """
    try:
        cmd_args = extra_args or []
        args_str = " ".join(cmd_args) if cmd_args else "(no flags)"
        log.info(f"Heartbeat command received from {from_name} with args: {args_str}")

        cmd = ["bash", "/agent/heartbeat.sh"] + cmd_args

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd="/agent",
        )

        if result.returncode == 0:
            response = "✅ Heartbeat triggered successfully!\n\nThe agent cycle is now running."
            log.info(f"Heartbeat completed successfully for {from_name}")
        else:
            response = f"⚠️ Heartbeat triggered but returned non-zero exit code: {result.returncode}\n\nCheck logs for details."
            log.warning(
                f"Heartbeat failed with code {result.returncode}: {result.stderr}"
            )

        wa_send_message(token, phone_number_id, from_phone, response)
        with _lock:
            append_chat_message(chat_history, from_phone, "bot", response)
        return True

    except subprocess.TimeoutExpired:
        response = (
            "⏱️ Heartbeat timed out after 5 minutes. The cycle may still be running."
        )
        log.error(f"Heartbeat timeout for {from_name}")
        wa_send_message(token, phone_number_id, from_phone, response)
        with _lock:
            append_chat_message(chat_history, from_phone, "bot", response)
        return True

    except Exception as e:
        response = f"❌ Failed to trigger heartbeat: {str(e)}"
        log.error(f"Heartbeat command failed for {from_name}: {e}", exc_info=True)
        wa_send_message(token, phone_number_id, from_phone, response)
        with _lock:
            append_chat_message(chat_history, from_phone, "bot", response)
        return True


# ---------------------------------------------------------------------------
# Incoming messages (WhatsApp webhook → inbox.json)
# ---------------------------------------------------------------------------


def _process_authorized_message(
    token: str,
    phone_number_id: str,
    msg: dict,
    text: str,
    from_name: str,
    from_phone: str,
    message_id: str,
    chat_history: dict,
    inbox_items: list,
):
    """Process an authorized incoming message (text and/or media) into an inbox item."""
    # Check for commands first
    if text and text.startswith("/"):
        parts = text.split()
        command = parts[0].lower()

        if command == "/heartbeat":
            extra_args = parts[1:] if len(parts) > 1 else []
            handle_heartbeat_command(
                token,
                phone_number_id,
                from_phone,
                from_name,
                chat_history,
                extra_args,
            )
            return

    # Extract media attachments
    media_list = extract_wa_media(msg)

    # Use caption as fallback text for media messages
    # WhatsApp puts captions in image/video/document caption field
    caption = ""
    for media_key in ("image", "document", "video"):
        media_val = msg.get(media_key)
        if isinstance(media_val, dict) and media_val.get("caption"):
            caption = str(media_val["caption"]).strip()
            break

    effective_text = text or caption

    if not effective_text and not media_list:
        return

    # Download media files
    attachments = []
    for media_id, media_type, filename_hint in media_list:
        local_path = download_wa_media(token, media_id, from_phone, filename_hint)
        if local_path:
            attachments.append(
                {
                    "type": media_type,
                    "path": local_path,
                    "filename": Path(local_path).name,
                }
            )

    # Build content string
    context = build_chat_context(chat_history, from_phone)
    base_content = (
        f"[WhatsApp {from_name}]: {effective_text}"
        if effective_text
        else f"[WhatsApp {from_name}]:"
    )

    if attachments:
        attachment_lines = "\n".join(f"- {a['type']}: {a['path']}" for a in attachments)
        base_content += f"\n[Attachments]\n{attachment_lines}"

    if context:
        content = f"[Previous conversation context (last 24h)]\n{context}\n[End of context]\n\n[New message]\n{base_content}"
    else:
        content = base_content

    chat_text = effective_text or "(media)"
    append_chat_message(chat_history, from_phone, "user", chat_text)

    inbox_item = {
        "type": "message",
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "source": "whatsapp",
    }
    if attachments:
        inbox_item["attachments"] = attachments

    inbox_items.append(inbox_item)
    log.info(
        f"Received from {from_name} ({from_phone}): {chat_text[:100]}"
        + (f" (+{len(attachments)} attachment(s))" if attachments else "")
    )

    # Send read receipt
    wa_send_read_receipt(token, phone_number_id, message_id)

    # Send ack
    ack = build_ack_message()
    wa_send_message(token, phone_number_id, from_phone, ack)
    append_chat_message(chat_history, from_phone, "bot", ack)
    log.info(f"Sent ack reply to {from_phone}")


def process_webhook_messages(
    token: str,
    phone_number_id: str,
    webhook_data: dict,
    state: dict,
    owner_username: str | None,
    chat_ids: list[str],
    chat_history: dict,
) -> tuple[str | None, list[str], list]:
    """Process incoming webhook messages.

    Returns (updated_owner_username, updated_chat_ids, list_of_inbox_items).
    """
    inbox_items = []

    for entry in webhook_data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # Build a map of wa_id → profile name from contacts
            contacts = {
                c.get("wa_id", ""): c.get("profile", {}).get("name", "Unknown")
                for c in value.get("contacts", [])
            }

            for msg in value.get("messages", []):
                from_phone = msg.get("from", "")
                from_name = contacts.get(from_phone, from_phone)
                message_id = msg.get("id", "")
                msg_type = msg.get("type", "")

                # Extract text content
                text = ""
                if msg_type == "text":
                    text = msg.get("text", {}).get("body", "").strip()

                # --- Session authorization logic ---

                if not chat_ids and from_phone:
                    # First-ever message: auto-accept as owner
                    log.info(
                        f"Auto-discovered owner: {from_name} (phone: {from_phone})"
                    )
                    owner_username = from_name
                    chat_ids.append(from_phone)
                    keepass_store(
                        KEEPASS_WHATSAPP_OWNER_USERNAME,
                        "whatsapp",
                        from_name,
                        group="System",
                    )
                    keepass_store(
                        KEEPASS_WHATSAPP_CHAT_ID,
                        "whatsapp",
                        serialize_chat_ids(chat_ids),
                        group="System",
                    )
                    log.info("Owner name and phone saved to KeePass.")
                    _process_authorized_message(
                        token,
                        phone_number_id,
                        msg,
                        text,
                        from_name,
                        from_phone,
                        message_id,
                        chat_history,
                        inbox_items,
                    )

                elif from_phone in chat_ids:
                    # Known session — process normally
                    _process_authorized_message(
                        token,
                        phone_number_id,
                        msg,
                        text,
                        from_name,
                        from_phone,
                        message_id,
                        chat_history,
                        inbox_items,
                    )

                elif (
                    from_phone
                    and owner_username
                    and text
                    and contains_username(text, owner_username)
                ):
                    # New session authorized — message contains owner's name
                    log.info(
                        f"Authorized new session: {from_name} (phone: {from_phone})"
                    )
                    chat_ids.append(from_phone)
                    keepass_store(
                        KEEPASS_WHATSAPP_CHAT_ID,
                        "whatsapp",
                        serialize_chat_ids(chat_ids),
                        group="System",
                    )

                    welcome = "Welcome! You've been authorized. I'll forward messages to you from now on."
                    wa_send_message(token, phone_number_id, from_phone, welcome)
                    append_chat_message(chat_history, from_phone, "bot", welcome)
                    _process_authorized_message(
                        token,
                        phone_number_id,
                        msg,
                        text,
                        from_name,
                        from_phone,
                        message_id,
                        chat_history,
                        inbox_items,
                    )

                else:
                    # Unauthorized — reject
                    log.info(f"Rejected message from {from_name} (phone: {from_phone})")
                    wa_send_message(
                        token,
                        phone_number_id,
                        from_phone,
                        "Sorry, I don't know you. Please include my owner's name in your message to get access.",
                    )

    return owner_username, chat_ids, inbox_items


def process_webhook_and_persist(
    token: str,
    phone_number_id: str,
    webhook_data: dict,
    state: dict,
    owner_username: str | None,
    chat_ids: list[str],
    chat_history: dict,
) -> tuple[str | None, list[str]]:
    """Process webhook messages, write to inbox AND persist to history."""
    owner_username, chat_ids, items = process_webhook_messages(
        token,
        phone_number_id,
        webhook_data,
        state,
        owner_username,
        chat_ids,
        chat_history,
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


# write_to_inbox → imported from shared module (FIXED: was missing file locking!)
# append_to_inbox_history → uses shared.append_to_history


def append_to_inbox_history(items: list):
    """Persist received WhatsApp messages to a history file (never cleared)."""
    append_to_history(items, INBOX_HISTORY_FILE)


# ---------------------------------------------------------------------------
# Outgoing messages (outbox.json → WhatsApp)
# ---------------------------------------------------------------------------


def send_outbox_messages(
    token: str,
    phone_number_id: str,
    chat_ids: list[str],
    state: dict,
    chat_history: dict,
):
    """Forward unsent outbox messages to all authorized WhatsApp numbers."""
    from services.shared import read_outbox_locked

    outbox = read_outbox_locked()
    if not outbox:
        return

    sent_count = 0
    for msg in outbox:
        h = msg_hash(msg)
        if h in state["sent_hashes"]:
            continue

        text = _format_outbox_msg(msg)

        # WhatsApp max message length is 4096 chars
        if len(text) > 4000:
            text = text[:3997] + "..."

        succeeded_phones = []
        for phone in chat_ids:
            result = wa_send_message(token, phone_number_id, phone, text)
            if result:
                succeeded_phones.append(phone)
            else:
                # Try without formatting as fallback
                fallback_text = _strip_wa_formatting(text)
                result2 = wa_send_message(token, phone_number_id, phone, fallback_text)
                if result2:
                    succeeded_phones.append(phone)

        if succeeded_phones:
            state["sent_hashes"].append(h)
            sent_count += 1
            log.info(
                f"Sent to WhatsApp ({len(succeeded_phones)} chat(s)): {msg.get('subject', text[:60])!r}"
            )
            _append_outbox_history(msg)
            for phone in succeeded_phones:
                append_chat_message(chat_history, phone, "bot", text)

    # Cap hash list to last 1000
    state["sent_hashes"] = state["sent_hashes"][-1000:]

    if sent_count:
        save_chat_history(chat_history)
        log.info(
            f"Forwarded {sent_count} outbox message(s) to {len(chat_ids)} WhatsApp chat(s)."
        )


def _append_outbox_history(msg: dict):
    """Append a sent outbox message to the persistent history (if not duplicate)."""
    try:
        history = []
        if OUTBOX_HISTORY_FILE.exists():
            raw = OUTBOX_HISTORY_FILE.read_text().strip()
            history = json.loads(raw) if raw else []
        existing_subjects = {m.get("subject") for m in history}
        if msg.get("subject") and msg["subject"] in existing_subjects:
            return
        history.append(msg)
        history = history[-500:]
        atomic_write_json(OUTBOX_HISTORY_FILE, history, indent=2)
    except Exception as e:
        log.warning(f"Failed to append outbox history: {e}")


def _format_outbox_msg(msg: dict) -> str:
    """Convert an outbox dict to a readable WhatsApp message."""
    msg_type = msg.get("type", "")
    subject = msg.get("subject", "")
    content = msg.get("content", "")

    lines = []

    # Type badge (WhatsApp supports *bold*)
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

    return "\n".join(lines) if lines else json.dumps(msg, indent=2)


# ---------------------------------------------------------------------------
# Webhook HTTP server
# ---------------------------------------------------------------------------


class WhatsAppWebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler for WhatsApp webhook verification and incoming messages."""

    def log_message(self, format, *args):
        """Route HTTP server logs through our logger."""
        log.debug(f"HTTP: {format % args}")

    def do_GET(self):
        """Handle webhook verification from Meta."""
        parsed = urlparse(self.path)
        if parsed.path != WEBHOOK_PATH:
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        mode = params.get("hub.mode", [None])[0]
        verify_token = params.get("hub.verify_token", [None])[0]
        challenge = params.get("hub.challenge", [None])[0]

        if (
            mode == "subscribe"
            and verify_token == self.server.wa_verify_token
            and challenge is not None
        ):
            log.info("Webhook verification successful.")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(challenge.encode())
        else:
            log.warning(
                f"Webhook verification failed. mode={mode}, challenge_present={challenge is not None}, token_match={verify_token == self.server.wa_verify_token}"
            )
            self.send_response(403)
            self.end_headers()

    def do_POST(self):
        """Handle incoming webhook messages from Meta."""
        parsed = urlparse(self.path)
        if parsed.path != WEBHOOK_PATH:
            self.send_response(404)
            self.end_headers()
            return

        # Read and parse body BEFORE sending response
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except Exception as e:
            log.warning(f"Failed to parse webhook body: {e}")
            self.send_response(400)
            self.end_headers()
            return

        # Respond 200 to Meta (they retry on non-200)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

        # Ignore non-message webhook events (status updates, etc.)
        is_message = False
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("value", {}).get("messages"):
                    is_message = True
                    break

        if not is_message:
            return

        # Process messages with thread safety
        srv = self.server
        with _lock:
            srv.wa_owner_username, srv.wa_chat_ids = process_webhook_and_persist(
                srv.wa_token,
                srv.wa_phone_number_id,
                data,
                srv.wa_state,
                srv.wa_owner_username,
                srv.wa_chat_ids,
                srv.wa_chat_history,
            )
            try:
                save_state(srv.wa_state)
            except Exception as e:
                log.error(
                    f"Failed to save state after webhook processing: {e}", exc_info=True
                )


def start_webhook_server(
    verify_token: str,
    token: str,
    phone_number_id: str,
    state: dict,
    owner_username: str | None,
    chat_ids: list[str],
    chat_history: dict,
) -> HTTPServer:
    """Start the webhook HTTP server in a daemon thread.

    Returns the server instance (shared state is accessible via server attributes).
    """
    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WhatsAppWebhookHandler)

    # Attach shared state to the server so the handler can access it
    server.wa_verify_token = verify_token
    server.wa_token = token
    server.wa_phone_number_id = phone_number_id
    server.wa_state = state
    server.wa_owner_username = owner_username
    server.wa_chat_ids = chat_ids
    server.wa_chat_history = chat_history

    thread = threading.Thread(target=server.serve_forever, daemon=False)
    thread.start()
    server._serve_thread = thread  # expose for graceful shutdown in main()
    log.info(f"Webhook server started on port {WEBHOOK_PORT} (path: {WEBHOOK_PATH})")
    return server


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    log.info("=" * 60)
    log.info("WhatsApp Bridge starting up")
    log.info("=" * 60)

    # Load credentials — check all before exiting so user sees everything needed
    token = keepass_get(KEEPASS_WHATSAPP_ACCESS_TOKEN)
    phone_number_id = keepass_get(KEEPASS_WHATSAPP_PHONE_NUMBER_ID)
    verify_token = keepass_get(KEEPASS_WHATSAPP_VERIFY_TOKEN)

    missing = []
    if not token:
        missing.append("WHATSAPP_ACCESS_TOKEN")
    if not phone_number_id:
        missing.append("WHATSAPP_PHONE_NUMBER_ID")
    if not verify_token:
        missing.append("WHATSAPP_VERIFY_TOKEN")

    if missing:
        log.error(
            f"Missing credentials in KeePass: {', '.join(missing)}\n"
            "\n"
            "=== WhatsApp Bridge Setup ===\n"
            "\n"
            "Step 1: Create a Meta Developer account & WhatsApp Business app\n"
            "  → Go to https://developers.facebook.com\n"
            "  → Create or select an app with WhatsApp product enabled\n"
            "  → In the app dashboard, go to WhatsApp → API Setup\n"
            "\n"
            "Step 2: Get your credentials from the API Setup page\n"
            "  → Access Token: generate a permanent token (or use the temporary one for testing)\n"
            "  → Phone Number ID: shown under the 'From' phone number (not the phone number itself)\n"
            "  → Verify Token: choose any secret string you like\n"
            "\n"
            "Step 3: Store credentials in KeePass\n"
            "  uv run python scripts/keepass.py store --title 'WHATSAPP_ACCESS_TOKEN' --username whatsapp --password '<your_token>'\n"
            "  uv run python scripts/keepass.py store --title 'WHATSAPP_PHONE_NUMBER_ID' --username whatsapp --password '<phone_number_id>'\n"
            "  uv run python scripts/keepass.py store --title 'WHATSAPP_VERIFY_TOKEN' --username whatsapp --password '<your_secret>'\n"
            "\n"
            "Step 4: Start the bridge\n"
            "  uv run python scripts/service_manager.py start whatsapp_bridge 8083 -- uv run python services/whatsapp_bridge.py\n"
            "\n"
            "Step 5: Configure the webhook in Meta Developer Portal\n"
            "  → Webhook URL: https://<public-url>/system/whatsapp-bridge/webhook\n"
            "  → Verify Token: the same secret string from Step 2\n"
            "  → Subscribe to: messages\n"
            "\n"
            "Step 6: Send a WhatsApp message to the business number\n"
            "  → The bridge will auto-discover your phone as the owner\n"
            "  → All future outbox messages will be forwarded to you on WhatsApp\n"
        )
        sys.exit(1)

    owner_username = keepass_get(KEEPASS_WHATSAPP_OWNER_USERNAME)
    if owner_username:
        log.info(f"Owner name: {owner_username}")

    chat_ids_raw = keepass_get(KEEPASS_WHATSAPP_CHAT_ID)
    chat_ids = parse_chat_ids(chat_ids_raw)
    if chat_ids:
        log.info(f"Using saved phone numbers: {chat_ids}")
    else:
        log.info("No phone numbers saved. Send a WhatsApp message to auto-discover.")

    state = load_state()
    chat_history = load_chat_history()

    log.info(f"Loaded chat history for {len(chat_history)} chat(s)")

    # Start webhook server
    server = start_webhook_server(
        verify_token,
        token,
        phone_number_id,
        state,
        owner_username,
        chat_ids,
        chat_history,
    )

    # Register Caddy route
    register_caddy_route()

    last_outbox_check = 0.0

    # Graceful shutdown via SIGTERM/SIGINT (matches telegram_bridge & github_watcher)
    shutdown_requested = False

    def _handle_shutdown(signum, frame):
        nonlocal shutdown_requested
        log.info(f"Received signal {signum}, requesting graceful shutdown...")
        shutdown_requested = True

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    log.info("Entering main loop (Ctrl+C or SIGTERM to stop)...")
    try:
        while not shutdown_requested:
            try:
                # Forward outbox messages periodically
                now = time.time()
                if now - last_outbox_check >= OUTBOX_INTERVAL:
                    with _lock:
                        # Re-read from server in case webhook thread updated them
                        chat_ids = server.wa_chat_ids
                        owner_username = server.wa_owner_username
                        if chat_ids:
                            send_outbox_messages(
                                token, phone_number_id, chat_ids, state, chat_history
                            )
                            try:
                                save_state(state)
                            except Exception as e:
                                log.error(
                                    f"Failed to save state after outbox send: {e}",
                                    exc_info=True,
                                )
                    last_outbox_check = now

                _write_heartbeat()
                # Sleep briefly to avoid busy-waiting
                time.sleep(1)

            except Exception as e:
                if shutdown_requested:
                    break
                log.error(f"Unexpected error in main loop: {e}", exc_info=True)
                time.sleep(10)

    except KeyboardInterrupt:
        log.info("Shutdown requested via KeyboardInterrupt.")
    finally:
        log.info("Cleaning up...")
        deregister_caddy_route()
        try:
            server.shutdown()
            if hasattr(server, "_serve_thread"):
                server._serve_thread.join(timeout=5)
        except Exception as e:
            log.warning(f"Error during server shutdown: {e}")
        log.info("WhatsApp Bridge stopped. Goodbye.")


if __name__ == "__main__":
    main()
