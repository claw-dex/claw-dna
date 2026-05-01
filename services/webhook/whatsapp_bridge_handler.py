#!/usr/bin/env python3
"""WhatsApp Bridge handler for webhook_receiver.

Registered on path prefix /whatsapp-bridge. Since Caddy strips the /webhook
prefix, the external URL exposed to Meta is:
    https://<public-url>/webhook/whatsapp-bridge

Responsibilities:
- GET: Meta webhook verification (hub.challenge echo)
- POST: Incoming message dispatch — auth/session, media download, inbox write
- Background: polls /agent/messages/outbox.json every 60s and forwards unsent
  messages to all authorized WhatsApp chats

Credentials are read from KeePass at start(). If any are missing, the handler
logs a warning and self-disables; webhook_receiver keeps serving other paths.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from shared import (
    append_to_history,
    atomic_write_json,
    read_outbox_locked,
    write_to_inbox,
)

# --- Paths ---
BASE = Path("/agent")
STATE_FILE = BASE / "memory" / "whatsapp_state.json"
INBOX_FILE = BASE / "messages" / "inbox.json"
OUTBOX_FILE = BASE / "messages" / "outbox.json"
INBOX_HISTORY_FILE = BASE / "memory" / "whatsapp_inbox_history.json"
OUTBOX_HISTORY_FILE = BASE / "memory" / "whatsapp_outbox_history.json"
CHAT_HISTORY_FILE = BASE / "memory" / "whatsapp_chat_history.json"
STATE_JSON = BASE / "memory" / "state.json"
MEDIA_DIR = BASE / "workspace" / "whatsapp"

# Logger is a child of webhook_receiver — propagates to the receiver's handlers.
log = logging.getLogger("webhook_receiver.whatsapp")

# --- Constants ---
KEEPASS_WHATSAPP_ACCESS_TOKEN = "WHATSAPP_ACCESS_TOKEN"
KEEPASS_WHATSAPP_PHONE_NUMBER_ID = "WHATSAPP_PHONE_NUMBER_ID"
KEEPASS_WHATSAPP_VERIFY_TOKEN = "WHATSAPP_VERIFY_TOKEN"
KEEPASS_WHATSAPP_CHAT_ID = "WHATSAPP_CHAT_ID"
KEEPASS_WHATSAPP_OWNER_USERNAME = "WHATSAPP_OWNER_USERNAME"

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"
OUTBOX_INTERVAL = 60  # seconds
WEBHOOK_PATH = "/whatsapp-bridge"  # matched after Caddy strips /webhook

# Shared state lock (single-instance handler)
_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Heartbeat helpers (for ack messages)
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
    if not csv_string:
        return []
    return [cid.strip() for cid in csv_string.split(",") if cid.strip()]


def serialize_chat_ids(chat_ids: list[str]) -> str:
    return ",".join(chat_ids)


def contains_username(text: str, username: str) -> bool:
    pattern = r"(?<!\w)@?" + re.escape(username) + r"\b"
    return bool(re.search(pattern, text, re.IGNORECASE))


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _validate_state(data: dict) -> dict:
    default = {"last_message_ts": "", "sent_hashes": []}
    if not isinstance(data, dict):
        log.warning(
            "WhatsApp state has unexpected type %s, resetting", type(data).__name__
        )
        return dict(default)
    ts = data.get("last_message_ts")
    if not isinstance(ts, str):
        log.warning(
            "WhatsApp state last_message_ts has wrong type %s, resetting to ''",
            type(ts).__name__,
        )
        data["last_message_ts"] = ""
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
    key = json.dumps(msg, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# WhatsApp formatting helpers
# ---------------------------------------------------------------------------


def _strip_wa_formatting(text: str) -> str:
    text = text.replace("*", "").replace("_", "").replace("~", "")
    text = re.sub(r"```", "", text)
    return text


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------


def load_chat_history() -> dict:
    if CHAT_HISTORY_FILE.exists():
        try:
            return json.loads(CHAT_HISTORY_FILE.read_text())
        except Exception as e:
            log.warning(f"Failed to load chat history: {e}")
    return {}


def save_chat_history(history: dict):
    try:
        atomic_write_json(CHAT_HISTORY_FILE, history, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f"Failed to save chat history: {e}")


def append_chat_message(history: dict, chat_id: str, role: str, text: str):
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
    messages = history.get(chat_id, [])
    if not messages:
        return ""

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    last_bot = None
    for m in reversed(messages):
        if m["role"] == "bot":
            last_bot = m
            break

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

    if last_bot and last_bot not in recent:
        recent.insert(0, last_bot)

    if not recent:
        return ""

    selected = recent[-10:]

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
    headers = {"Authorization": f"Bearer {token}"}

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

    dest_dir = MEDIA_DIR / chat_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(filename_hint).name
    unique_name = f"{uuid.uuid4().hex[:12]}_{safe_name}"
    dest_path = dest_dir / unique_name

    max_size = 50 * 1024 * 1024
    try:
        with requests.get(
            media_url, headers=headers, timeout=(30, 120), stream=True
        ) as resp:
            resp.raise_for_status()
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
# Command handlers
# ---------------------------------------------------------------------------


def handle_heartbeat_command(
    token: str,
    phone_number_id: str,
    from_phone: str,
    from_name: str,
    chat_history: dict,
    extra_args: list[str] | None = None,
) -> bool:
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
# Incoming message processing
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

    media_list = extract_wa_media(msg)

    caption = ""
    for media_key in ("image", "document", "video"):
        media_val = msg.get(media_key)
        if isinstance(media_val, dict) and media_val.get("caption"):
            caption = str(media_val["caption"]).strip()
            break

    effective_text = text or caption

    if not effective_text and not media_list:
        return

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

    wa_send_read_receipt(token, phone_number_id, message_id)

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
    inbox_items = []

    for entry in webhook_data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            contacts = {
                c.get("wa_id", ""): c.get("profile", {}).get("name", "Unknown")
                for c in value.get("contacts", [])
            }

            for msg in value.get("messages", []):
                from_phone = msg.get("from", "")
                from_name = contacts.get(from_phone, from_phone)
                message_id = msg.get("id", "")
                msg_type = msg.get("type", "")

                text = ""
                if msg_type == "text":
                    text = msg.get("text", {}).get("body", "").strip()

                if not chat_ids and from_phone:
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


def append_to_inbox_history(items: list):
    append_to_history(items, INBOX_HISTORY_FILE)


# ---------------------------------------------------------------------------
# Outgoing message processing (outbox → WhatsApp)
# ---------------------------------------------------------------------------


def send_outbox_messages(
    token: str,
    phone_number_id: str,
    chat_ids: list[str],
    state: dict,
    chat_history: dict,
):
    outbox = read_outbox_locked()
    if not outbox:
        return

    sent_count = 0
    for msg in outbox:
        h = msg_hash(msg)
        if h in state["sent_hashes"]:
            continue

        text = _format_outbox_msg(msg)

        if len(text) > 4000:
            text = text[:3997] + "..."

        succeeded_phones = []
        for phone in chat_ids:
            result = wa_send_message(token, phone_number_id, phone, text)
            if result:
                succeeded_phones.append(phone)
            else:
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

    state["sent_hashes"] = state["sent_hashes"][-1000:]

    if sent_count:
        save_chat_history(chat_history)
        log.info(
            f"Forwarded {sent_count} outbox message(s) to {len(chat_ids)} WhatsApp chat(s)."
        )


def _append_outbox_history(msg: dict):
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
        lines.append(f"*{subject}*")

    if content:
        lines.append(content)

    return "\n".join(lines) if lines else json.dumps(msg, indent=2)


# ---------------------------------------------------------------------------
# Handler class — registered with webhook_receiver
# ---------------------------------------------------------------------------


class WhatsAppBridgeHandler:
    """Webhook sub-handler for WhatsApp Business Cloud API.

    Registered at path_prefix = "/whatsapp-bridge" (matched after Caddy strips
    the external "/webhook" prefix).
    """

    path_prefix = WEBHOOK_PATH

    def __init__(self):
        self._enabled = False
        self._shutdown = False
        self._token: str | None = None
        self._phone_number_id: str | None = None
        self._verify_token: str | None = None
        self._state: dict | None = None
        self._chat_history: dict | None = None
        self._owner_username: str | None = None
        self._chat_ids: list[str] | None = None
        self._outbox_thread: threading.Thread | None = None

    def start(self):
        """Load credentials + state; spawn outbox poller. Self-disables if creds missing."""
        log.info("Starting WhatsApp bridge handler...")

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
            log.warning(
                f"WhatsApp handler disabled — missing KeePass credentials: {', '.join(missing)}. "
                "Store them with: uv run python scripts/keepass.py store "
                "--title '<NAME>' --username whatsapp --password '<value>'"
            )
            return

        self._token = token
        self._phone_number_id = phone_number_id
        self._verify_token = verify_token
        self._owner_username = keepass_get(KEEPASS_WHATSAPP_OWNER_USERNAME)
        self._chat_ids = parse_chat_ids(keepass_get(KEEPASS_WHATSAPP_CHAT_ID))
        self._state = load_state()
        self._chat_history = load_chat_history()

        if self._owner_username:
            log.info(f"Owner name: {self._owner_username}")
        if self._chat_ids:
            log.info(f"Using saved phone numbers: {self._chat_ids}")
        else:
            log.info(
                "No phone numbers saved. Send a WhatsApp message to auto-discover."
            )
        log.info(f"Loaded chat history for {len(self._chat_history)} chat(s)")

        self._enabled = True
        self._outbox_thread = threading.Thread(
            target=self._outbox_loop, name="whatsapp-outbox", daemon=True
        )
        self._outbox_thread.start()
        log.info(
            f"WhatsApp bridge handler ready on {self.path_prefix} "
            f"(external: /webhook{self.path_prefix})"
        )

    def shutdown(self):
        self._shutdown = True
        if self._outbox_thread and self._outbox_thread.is_alive():
            self._outbox_thread.join(timeout=5)
        log.info("WhatsApp bridge handler stopped.")

    def handle(self, req: BaseHTTPRequestHandler):
        """Own the full HTTP request/response for this path."""
        if not self._enabled:
            body = b'{"status":"error","detail":"whatsapp bridge disabled: missing credentials"}'
            req.send_response(503)
            req.send_header("Content-Type", "application/json")
            req.send_header("Content-Length", str(len(body)))
            req.end_headers()
            if req.command != "HEAD":
                req.wfile.write(body)
            return

        method = req.command
        if method == "GET":
            self._handle_verification(req)
        elif method == "POST":
            self._handle_message(req)
        else:
            req.send_response(405)
            req.send_header("Allow", "GET, POST")
            req.send_header("Content-Length", "0")
            req.end_headers()

    def _handle_verification(self, req: BaseHTTPRequestHandler):
        """Meta webhook verification (challenge echo)."""
        parsed = urlparse(req.path)
        params = parse_qs(parsed.query)
        mode = params.get("hub.mode", [None])[0]
        verify_token = params.get("hub.verify_token", [None])[0]
        challenge = params.get("hub.challenge", [None])[0]

        if (
            mode == "subscribe"
            and verify_token == self._verify_token
            and challenge is not None
        ):
            log.info("Webhook verification successful.")
            body = challenge.encode()
            req.send_response(200)
            req.send_header("Content-Type", "text/plain")
            req.send_header("Content-Length", str(len(body)))
            req.end_headers()
            req.wfile.write(body)
        else:
            log.warning(
                f"Webhook verification failed. mode={mode}, "
                f"challenge_present={challenge is not None}, "
                f"token_match={verify_token == self._verify_token}"
            )
            req.send_response(403)
            req.send_header("Content-Length", "0")
            req.end_headers()

    def _handle_message(self, req: BaseHTTPRequestHandler):
        """Incoming message from Meta. Reply 200 first, then process."""
        try:
            content_length = int(req.headers.get("Content-Length", 0))
            body = req.rfile.read(content_length)
            data = json.loads(body)
        except Exception as e:
            log.warning(f"Failed to parse webhook body: {e}")
            req.send_response(400)
            req.send_header("Content-Length", "0")
            req.end_headers()
            return

        # Reply 200 before processing — Meta retries on non-200.
        resp_body = b'{"status": "ok"}'
        req.send_response(200)
        req.send_header("Content-Type", "application/json")
        req.send_header("Content-Length", str(len(resp_body)))
        req.end_headers()
        req.wfile.write(resp_body)

        # Ignore non-message events (status updates, etc.).
        is_message = False
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("value", {}).get("messages"):
                    is_message = True
                    break

        if not is_message:
            return

        with _lock:
            self._owner_username, self._chat_ids = process_webhook_and_persist(
                self._token,
                self._phone_number_id,
                data,
                self._state,
                self._owner_username,
                self._chat_ids,
                self._chat_history,
            )
            try:
                save_state(self._state)
            except Exception as e:
                log.error(
                    f"Failed to save state after webhook processing: {e}",
                    exc_info=True,
                )

    def _outbox_loop(self):
        """Poll outbox every OUTBOX_INTERVAL seconds; sleep in 1s steps for responsive shutdown."""
        log.info(f"Outbox polling started (interval: {OUTBOX_INTERVAL}s)")
        last_check = 0.0
        while not self._shutdown:
            try:
                now = time.time()
                if now - last_check >= OUTBOX_INTERVAL:
                    with _lock:
                        if self._chat_ids:
                            send_outbox_messages(
                                self._token,
                                self._phone_number_id,
                                self._chat_ids,
                                self._state,
                                self._chat_history,
                            )
                            try:
                                save_state(self._state)
                            except Exception as e:
                                log.error(
                                    f"Failed to save state after outbox send: {e}",
                                    exc_info=True,
                                )
                    last_check = now
                time.sleep(1)
            except Exception as e:
                if self._shutdown:
                    break
                log.error(f"Error in outbox loop: {e}", exc_info=True)
                for _ in range(10):
                    if self._shutdown:
                        break
                    time.sleep(1)
