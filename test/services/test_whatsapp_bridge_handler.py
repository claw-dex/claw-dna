"""Tests for services/webhook/whatsapp_bridge_handler.py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

# ---------------------------------------------------------------------------
# heartbeat / time helpers
# ---------------------------------------------------------------------------


def test_get_last_heartbeat_returns_none_when_state_missing(patch_whatsapp_paths):
    assert patch_whatsapp_paths.get_last_heartbeat() is None


def test_get_last_heartbeat_returns_iso_value(patch_whatsapp_paths):
    wa = patch_whatsapp_paths
    iso = "2026-04-30T12:00:00+00:00"
    wa.STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    wa.STATE_JSON.write_text(json.dumps({"last_heartbeat": iso}))
    assert wa.get_last_heartbeat() == datetime.fromisoformat(iso)


@pytest.mark.parametrize(
    "delta, expected",
    [
        (timedelta(seconds=1), "1 second ago"),
        (timedelta(seconds=30), "30 seconds ago"),
        (timedelta(minutes=1), "1 minute ago"),
        (timedelta(minutes=10), "10 minutes ago"),
        (timedelta(hours=1), "1 hour ago"),
        (timedelta(days=1), "1 day ago"),
        (timedelta(days=7), "7 days ago"),
    ],
)
def test_relative_time(patch_whatsapp_paths, frozen_now, delta, expected):
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    frozen_now(now)
    assert patch_whatsapp_paths.relative_time(now - delta) == expected


def test_build_ack_message_without_heartbeat(patch_whatsapp_paths, frozen_now):
    frozen_now(datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc))
    assert (
        patch_whatsapp_paths.build_ack_message()
        == "Got it! I'll get back to you in the next cycle."
    )


def test_build_ack_message_with_heartbeat(patch_whatsapp_paths, frozen_now):
    wa = patch_whatsapp_paths
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    frozen_now(now)
    wa.STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    hb = (now - timedelta(hours=1)).isoformat()
    wa.STATE_JSON.write_text(json.dumps({"last_heartbeat": hb}))
    msg = wa.build_ack_message()
    assert "Last heartbeat" in msg
    assert "1 hour ago" in msg


# ---------------------------------------------------------------------------
# keepass helpers
# ---------------------------------------------------------------------------


def _install_fake_keepass(monkeypatch, *, get=None, store=None):
    import sys
    import types

    fake = types.ModuleType("scripts.keepass")
    fake.get_credential = get or (lambda title: None)
    fake.store_credential = store or (lambda **kw: True)
    pkg = types.ModuleType("scripts")
    pkg.keepass = fake
    monkeypatch.setitem(sys.modules, "scripts", pkg)
    monkeypatch.setitem(sys.modules, "scripts.keepass", fake)
    return fake


def test_keepass_get_returns_value(monkeypatch, patch_whatsapp_paths):
    _install_fake_keepass(monkeypatch, get=lambda title: f"value-of-{title}")
    assert patch_whatsapp_paths.keepass_get("WA") == "value-of-WA"


def test_keepass_get_returns_none_on_error(monkeypatch, patch_whatsapp_paths):
    def boom(_t):
        raise RuntimeError("locked")

    _install_fake_keepass(monkeypatch, get=boom)
    assert patch_whatsapp_paths.keepass_get("WA") is None


def test_keepass_store_returns_true_on_success(monkeypatch, patch_whatsapp_paths):
    captured = {}

    def store(**kw):
        captured.update(kw)
        return True

    _install_fake_keepass(monkeypatch, store=store)
    assert patch_whatsapp_paths.keepass_store("T", "u", "v") is True
    assert captured["title"] == "T"
    assert captured["password"] == "v"


# ---------------------------------------------------------------------------
# parse helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, []),
        ("", []),
        (" 1 , 2 ", ["1", "2"]),
    ],
)
def test_parse_chat_ids(patch_whatsapp_paths, raw, expected):
    assert patch_whatsapp_paths.parse_chat_ids(raw) == expected


def test_serialize_chat_ids(patch_whatsapp_paths):
    assert patch_whatsapp_paths.serialize_chat_ids(["a", "b"]) == "a,b"


def test_contains_username(patch_whatsapp_paths):
    assert patch_whatsapp_paths.contains_username("hello @bob world", "bob") is True
    assert patch_whatsapp_paths.contains_username("hello @bobby", "bob") is False


# ---------------------------------------------------------------------------
# state validation / persistence
# ---------------------------------------------------------------------------


def test_validate_state_resets_when_not_dict(patch_whatsapp_paths):
    out = patch_whatsapp_paths._validate_state(["junk"])
    assert out == {"last_message_ts": "", "sent_hashes": []}


def test_validate_state_repairs_wrong_types(patch_whatsapp_paths):
    out = patch_whatsapp_paths._validate_state(
        {"last_message_ts": 123, "sent_hashes": "nope"}
    )
    assert out["last_message_ts"] == ""
    assert out["sent_hashes"] == []


def test_validate_state_filters_non_string_hashes(patch_whatsapp_paths):
    out = patch_whatsapp_paths._validate_state(
        {"last_message_ts": "x", "sent_hashes": ["a", 1, "b"]}
    )
    assert out["sent_hashes"] == ["a", "b"]


def test_load_state_default_when_missing(patch_whatsapp_paths):
    assert patch_whatsapp_paths.load_state() == {
        "last_message_ts": "",
        "sent_hashes": [],
    }


def test_save_then_load_state(patch_whatsapp_paths):
    wa = patch_whatsapp_paths
    state = {"last_message_ts": "ts", "sent_hashes": ["h1"]}
    wa.save_state(state)
    assert wa.load_state() == state


def test_load_state_default_on_corrupt(patch_whatsapp_paths):
    wa = patch_whatsapp_paths
    wa.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    wa.STATE_FILE.write_text("not json{")
    assert wa.load_state()["last_message_ts"] == ""


# ---------------------------------------------------------------------------
# msg_hash + formatting
# ---------------------------------------------------------------------------


def test_msg_hash_deterministic(patch_whatsapp_paths):
    assert patch_whatsapp_paths.msg_hash(
        {"a": 1, "b": 2}
    ) == patch_whatsapp_paths.msg_hash({"b": 2, "a": 1})


def test_msg_hash_distinguishes(patch_whatsapp_paths):
    assert patch_whatsapp_paths.msg_hash({"a": 1}) != patch_whatsapp_paths.msg_hash(
        {"a": 2}
    )


def test_strip_wa_formatting(patch_whatsapp_paths):
    assert (
        patch_whatsapp_paths._strip_wa_formatting("*bold* _ital_ ~strike~ ```code```")
        == "bold ital strike code"
    )


# ---------------------------------------------------------------------------
# chat history
# ---------------------------------------------------------------------------


def test_load_chat_history_empty_when_missing(patch_whatsapp_paths):
    assert patch_whatsapp_paths.load_chat_history() == {}


def test_save_then_load_chat_history(patch_whatsapp_paths):
    wa = patch_whatsapp_paths
    h = {
        "42": [{"role": "user", "text": "hi", "timestamp": "2026-04-30T12:00:00+00:00"}]
    }
    wa.save_chat_history(h)
    assert wa.load_chat_history() == h


def test_append_chat_message_truncates_to_50(patch_whatsapp_paths):
    h: dict = {}
    for i in range(60):
        patch_whatsapp_paths.append_chat_message(h, "42", "user", f"m{i}")
    assert len(h["42"]) == 50
    assert h["42"][0]["text"] == "m10"


def test_build_chat_context_keeps_last_bot_outside_window(
    patch_whatsapp_paths, frozen_now
):
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    frozen_now(now)
    history = {
        "c": [
            {
                "role": "bot",
                "text": "ancient-reply",
                "timestamp": (now - timedelta(days=5)).isoformat(),
            },
            {
                "role": "user",
                "text": "today",
                "timestamp": (now - timedelta(minutes=1)).isoformat(),
            },
        ]
    }
    out = patch_whatsapp_paths.build_chat_context(history, "c")
    assert "ancient-reply" in out
    assert "today" in out


# ---------------------------------------------------------------------------
# wa_send_message / wa_send_read_receipt
# ---------------------------------------------------------------------------


def test_wa_send_message_posts_payload(mocker, patch_whatsapp_paths):
    resp = mocker.Mock(ok=True, status_code=200)
    resp.json.return_value = {"messages": [{"id": "x"}]}
    post = mocker.patch.object(patch_whatsapp_paths.requests, "post", return_value=resp)

    out = patch_whatsapp_paths.wa_send_message("TOK", "PHONE", "+1", "hello")

    assert out == {"messages": [{"id": "x"}]}
    args, kwargs = post.call_args
    assert args[0].endswith("/PHONE/messages")
    assert kwargs["headers"]["Authorization"] == "Bearer TOK"
    assert kwargs["json"]["to"] == "+1"
    assert kwargs["json"]["text"] == {"body": "hello"}


def test_wa_send_message_returns_none_on_http_error(mocker, patch_whatsapp_paths):
    resp = mocker.Mock(ok=False, status_code=400)
    resp.json.return_value = {"error": {"message": "bad"}}
    mocker.patch.object(patch_whatsapp_paths.requests, "post", return_value=resp)
    assert patch_whatsapp_paths.wa_send_message("T", "P", "+1", "h") is None


def test_wa_send_message_returns_none_on_non_json(mocker, patch_whatsapp_paths):
    resp = mocker.Mock(ok=False, status_code=500, text="<html>")
    resp.json.side_effect = ValueError("nope")
    mocker.patch.object(patch_whatsapp_paths.requests, "post", return_value=resp)
    assert patch_whatsapp_paths.wa_send_message("T", "P", "+1", "h") is None


def test_wa_send_message_swallows_request_exception(mocker, patch_whatsapp_paths):
    mocker.patch.object(
        patch_whatsapp_paths.requests, "post", side_effect=RuntimeError("net")
    )
    assert patch_whatsapp_paths.wa_send_message("T", "P", "+1", "h") is None


def test_wa_send_read_receipt_posts_status_read(mocker, patch_whatsapp_paths):
    post = mocker.patch.object(patch_whatsapp_paths.requests, "post")
    patch_whatsapp_paths.wa_send_read_receipt("TOK", "PHONE", "MID")
    _, kwargs = post.call_args
    assert kwargs["json"]["status"] == "read"
    assert kwargs["json"]["message_id"] == "MID"


# ---------------------------------------------------------------------------
# extract_wa_media
# ---------------------------------------------------------------------------


def test_extract_wa_media_image(patch_whatsapp_paths):
    out = patch_whatsapp_paths.extract_wa_media(
        {"image": {"id": "img1", "mime_type": "image/png"}}
    )
    assert out == [("img1", "image", "photo.png")]


def test_extract_wa_media_document_uses_filename(patch_whatsapp_paths):
    out = patch_whatsapp_paths.extract_wa_media(
        {"document": {"id": "doc1", "filename": "my.pdf"}}
    )
    assert out == [("doc1", "document", "my.pdf")]


def test_extract_wa_media_audio_default_extension(patch_whatsapp_paths):
    out = patch_whatsapp_paths.extract_wa_media({"audio": {"id": "aud1"}})
    assert out[0][2].endswith(".ogg")


def test_extract_wa_media_video(patch_whatsapp_paths):
    out = patch_whatsapp_paths.extract_wa_media(
        {"video": {"id": "v1", "mime_type": "video/mp4"}}
    )
    assert out == [("v1", "video", "video.mp4")]


def test_extract_wa_media_combines_kinds(patch_whatsapp_paths):
    msg = {
        "image": {"id": "i", "mime_type": "image/jpeg"},
        "document": {"id": "d", "filename": "x.csv"},
    }
    out = patch_whatsapp_paths.extract_wa_media(msg)
    kinds = {kind for _, kind, _ in out}
    assert kinds == {"image", "document"}


def test_extract_wa_media_empty(patch_whatsapp_paths):
    assert patch_whatsapp_paths.extract_wa_media({"text": {"body": "hi"}}) == []


# ---------------------------------------------------------------------------
# _format_outbox_msg / _append_outbox_history
# ---------------------------------------------------------------------------


def test_format_outbox_msg_needs_human_badge(patch_whatsapp_paths):
    out = patch_whatsapp_paths._format_outbox_msg(
        {"type": "needs_human", "subject": "halp", "content": "stuck"}
    )
    assert "ACTION REQUIRED" in out
    assert "halp" in out
    assert "stuck" in out


def test_format_outbox_msg_dumps_when_empty(patch_whatsapp_paths):
    out = patch_whatsapp_paths._format_outbox_msg(
        {"type": "", "subject": "", "content": ""}
    )
    assert "type" in out  # JSON dump fallback


def test_append_outbox_history_dedups_subject(patch_whatsapp_paths):
    wa = patch_whatsapp_paths
    msg = {"subject": "weekly", "content": "first"}
    wa._append_outbox_history(msg)
    wa._append_outbox_history({"subject": "weekly", "content": "again"})

    history = json.loads(wa.OUTBOX_HISTORY_FILE.read_text())
    assert len(history) == 1
    assert history[0]["content"] == "first"


def test_append_outbox_history_caps_size(patch_whatsapp_paths):
    wa = patch_whatsapp_paths
    for i in range(550):
        wa._append_outbox_history({"subject": f"s{i}", "content": "c"})
    history = json.loads(wa.OUTBOX_HISTORY_FILE.read_text())
    assert len(history) == 500
    assert history[0]["subject"] == "s50"
    assert history[-1]["subject"] == "s549"
