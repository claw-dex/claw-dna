"""Tests for services/telegram_bridge.py — pure helpers + mocked HTTP."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

# ---------------------------------------------------------------------------
# heartbeat helpers
# ---------------------------------------------------------------------------


def test_write_heartbeat_writes_epoch_string(patch_telegram_paths):
    tb = patch_telegram_paths
    tb._write_heartbeat()
    float(tb.HEARTBEAT_FILE.read_text())


def test_write_heartbeat_swallows_oserror(monkeypatch, patch_telegram_paths, tmp_path):
    tb = patch_telegram_paths
    monkeypatch.setattr(tb, "HEARTBEAT_FILE", tmp_path / "missing" / "hb")
    tb._write_heartbeat()  # no raise


def test_get_last_heartbeat_returns_none_when_state_missing(patch_telegram_paths):
    assert patch_telegram_paths.get_last_heartbeat() is None


def test_get_last_heartbeat_returns_iso_value(patch_telegram_paths):
    tb = patch_telegram_paths
    iso = "2026-04-30T12:00:00+00:00"
    tb.STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    tb.STATE_JSON.write_text(json.dumps({"last_heartbeat": iso}))
    assert tb.get_last_heartbeat() == datetime.fromisoformat(iso)


# ---------------------------------------------------------------------------
# relative_time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delta, expected",
    [
        (timedelta(seconds=1), "1 second ago"),
        (timedelta(seconds=2), "2 seconds ago"),
        (timedelta(seconds=59), "59 seconds ago"),
        (timedelta(seconds=60), "1 minute ago"),
        (timedelta(minutes=2), "2 minutes ago"),
        (timedelta(hours=1), "1 hour ago"),
        (timedelta(hours=5), "5 hours ago"),
        (timedelta(days=1), "1 day ago"),
        (timedelta(days=3), "3 days ago"),
    ],
)
def test_relative_time_units(patch_telegram_paths, frozen_now, delta, expected):
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    frozen_now(now)
    assert patch_telegram_paths.relative_time(now - delta) == expected


def test_relative_time_handles_naive_datetime(patch_telegram_paths, frozen_now):
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    frozen_now(now)
    naive = datetime(2026, 4, 30, 11, 59, 0)  # tz-naive, treated as UTC
    assert patch_telegram_paths.relative_time(naive) == "1 minute ago"


# ---------------------------------------------------------------------------
# build_ack_message
# ---------------------------------------------------------------------------


def test_build_ack_message_without_heartbeat(patch_telegram_paths, frozen_now):
    frozen_now(datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc))
    msg = patch_telegram_paths.build_ack_message()
    assert msg == "Got it! I'll get back to you in the next cycle."


def test_build_ack_message_with_heartbeat(patch_telegram_paths, frozen_now):
    tb = patch_telegram_paths
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    frozen_now(now)
    tb.STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    hb = (now - timedelta(minutes=2)).isoformat()
    tb.STATE_JSON.write_text(json.dumps({"last_heartbeat": hb}))

    msg = tb.build_ack_message()
    assert "Last heartbeat" in msg
    assert "2 minutes ago" in msg


# ---------------------------------------------------------------------------
# keepass helpers — mocked subprocess via the lazy import
# ---------------------------------------------------------------------------


def test_keepass_get_returns_value(monkeypatch, patch_telegram_paths):
    import sys
    import types

    fake = types.ModuleType("scripts.keepass")
    fake.get_credential = lambda title: "secret-for-" + title
    fake.store_credential = lambda **kw: True

    pkg = types.ModuleType("scripts")
    pkg.keepass = fake
    monkeypatch.setitem(sys.modules, "scripts", pkg)
    monkeypatch.setitem(sys.modules, "scripts.keepass", fake)

    assert patch_telegram_paths.keepass_get("FOO") == "secret-for-FOO"


def test_keepass_get_returns_none_on_error(monkeypatch, patch_telegram_paths):
    import sys
    import types

    fake = types.ModuleType("scripts.keepass")

    def boom(title):
        raise RuntimeError("locked")

    fake.get_credential = boom
    pkg = types.ModuleType("scripts")
    pkg.keepass = fake
    monkeypatch.setitem(sys.modules, "scripts", pkg)
    monkeypatch.setitem(sys.modules, "scripts.keepass", fake)

    assert patch_telegram_paths.keepass_get("FOO") is None


def test_keepass_store_returns_true_on_success(monkeypatch, patch_telegram_paths):
    import sys
    import types

    captured = {}
    fake = types.ModuleType("scripts.keepass")

    def store(**kw):
        captured.update(kw)
        return True

    fake.store_credential = store
    pkg = types.ModuleType("scripts")
    pkg.keepass = fake
    monkeypatch.setitem(sys.modules, "scripts", pkg)
    monkeypatch.setitem(sys.modules, "scripts.keepass", fake)

    assert patch_telegram_paths.keepass_store("T", "u", "v", group="Bots") is True
    assert captured == {
        "title": "T",
        "username": "u",
        "password": "v",
        "group": "Bots",
    }


# ---------------------------------------------------------------------------
# chat-id parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, []),
        ("", []),
        ("  ", []),
        ("123", ["123"]),
        (" 1, 2 , 3", ["1", "2", "3"]),
        (",,,", []),
    ],
)
def test_parse_chat_ids(patch_telegram_paths, raw, expected):
    assert patch_telegram_paths.parse_chat_ids(raw) == expected


def test_serialize_chat_ids(patch_telegram_paths):
    assert patch_telegram_paths.serialize_chat_ids(["1", "2", "3"]) == "1,2,3"
    assert patch_telegram_paths.serialize_chat_ids([]) == ""


# ---------------------------------------------------------------------------
# string predicates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, username, expected",
    [
        ("hello @bob world", "bob", True),
        ("hello bob world", "bob", True),
        ("hello @robert world", "bob", False),
        ("hello @bobby world", "bob", False),
        ("BOB!", "bob", True),
        ("", "bob", False),
    ],
)
def test_contains_username(patch_telegram_paths, text, username, expected):
    assert patch_telegram_paths.contains_username(text, username) is expected


@pytest.mark.parametrize(
    "ct, expected",
    [
        ("group", True),
        ("supergroup", True),
        ("private", False),
        ("channel", False),
        ("", False),
    ],
)
def test_is_group_chat(patch_telegram_paths, ct, expected):
    assert patch_telegram_paths.is_group_chat(ct) is expected


@pytest.mark.parametrize(
    "command, bot, expected",
    [
        ("/goals", "mybot", "/goals"),
        ("/goals@mybot", "mybot", "/goals"),
        ("/goals@MYBOT", "mybot", "/goals"),
        ("/goals@otherbot", "mybot", "/goals@otherbot"),
        ("/goals@mybot", "", "/goals"),  # empty bot username still strips
    ],
)
def test_strip_bot_suffix(patch_telegram_paths, command, bot, expected):
    assert patch_telegram_paths.strip_bot_suffix(command, bot) == expected


def test_bot_is_addressed_slash_command(patch_telegram_paths):
    assert patch_telegram_paths.bot_is_addressed({}, "mybot", "/goals") is True


def test_bot_is_addressed_mention(patch_telegram_paths):
    assert patch_telegram_paths.bot_is_addressed({}, "mybot", "hi @mybot help") is True


def test_bot_is_addressed_reply(patch_telegram_paths):
    msg = {"reply_to_message": {"from": {"username": "mybot"}}}
    assert patch_telegram_paths.bot_is_addressed(msg, "mybot", "thanks") is True


def test_bot_is_addressed_negative(patch_telegram_paths):
    assert patch_telegram_paths.bot_is_addressed({}, "mybot", "random chatter") is False


# ---------------------------------------------------------------------------
# generate_passcode
# ---------------------------------------------------------------------------


def test_generate_passcode_format(patch_telegram_paths):
    for _ in range(20):
        code = patch_telegram_paths.generate_passcode()
        assert len(code) == 4
        assert code.isdigit()


def test_generate_passcode_zero_pads(monkeypatch, patch_telegram_paths):
    monkeypatch.setattr(patch_telegram_paths.secrets, "randbelow", lambda _n: 7)
    assert patch_telegram_paths.generate_passcode() == "0007"


# ---------------------------------------------------------------------------
# block-list helpers
# ---------------------------------------------------------------------------


def test_is_blocked_dict_format(patch_telegram_paths):
    state = {"blocked_chat_ids": [{"chat_id": "42", "username": "bob"}]}
    assert patch_telegram_paths.is_blocked(state, "42") is True
    assert patch_telegram_paths.is_blocked(state, "43") is False


def test_is_blocked_legacy_string(patch_telegram_paths):
    state = {"blocked_chat_ids": ["42"]}
    assert patch_telegram_paths.is_blocked(state, "42") is True


def test_find_blocked_entry_by_chat_id(patch_telegram_paths):
    state = {"blocked_chat_ids": [{"chat_id": "42", "username": "bob"}]}
    assert patch_telegram_paths.find_blocked_entry(state, "42") == {
        "chat_id": "42",
        "username": "bob",
    }


def test_find_blocked_entry_by_username_with_at(patch_telegram_paths):
    state = {"blocked_chat_ids": [{"chat_id": "42", "username": "bob"}]}
    assert patch_telegram_paths.find_blocked_entry(state, "@BOB")["chat_id"] == "42"


def test_find_blocked_entry_returns_none(patch_telegram_paths):
    assert patch_telegram_paths.find_blocked_entry({}, "anything") is None


def test_add_block_replaces_duplicate(patch_telegram_paths):
    state = {"blocked_chat_ids": []}
    patch_telegram_paths.add_block(state, "42", "@bob")
    patch_telegram_paths.add_block(state, "42", "@robert")
    assert state["blocked_chat_ids"] == [{"chat_id": "42", "username": "robert"}]


def test_remove_block_by_chat_id(patch_telegram_paths):
    state = {
        "blocked_chat_ids": [
            {"chat_id": "1", "username": "a"},
            {"chat_id": "2", "username": "b"},
        ]
    }
    removed = patch_telegram_paths.remove_block(state, "1")
    assert removed == {"chat_id": "1", "username": "a"}
    assert state["blocked_chat_ids"] == [{"chat_id": "2", "username": "b"}]


def test_remove_block_unknown_returns_none(patch_telegram_paths):
    state = {"blocked_chat_ids": []}
    assert patch_telegram_paths.remove_block(state, "ghost") is None


# ---------------------------------------------------------------------------
# state validation / persistence
# ---------------------------------------------------------------------------


def test_validate_state_repairs_wrong_types(patch_telegram_paths):
    out = patch_telegram_paths._validate_state(
        {
            "last_update_id": "not-an-int",
            "sent_hashes": "not-a-list",
            "pending_authorizations": [],
            "blocked_chat_ids": [
                "legacy_chat_id",
                {"chat_id": "valid", "username": "u"},
                {"username": "missing-chat-id"},
            ],
        }
    )
    assert out["last_update_id"] == 0
    assert out["sent_hashes"] == []
    assert out["pending_authorizations"] == {}
    assert {b["chat_id"] for b in out["blocked_chat_ids"]} == {
        "legacy_chat_id",
        "valid",
    }


def test_validate_state_resets_when_not_dict(patch_telegram_paths):
    out = patch_telegram_paths._validate_state(["junk"])
    assert out["last_update_id"] == 0
    assert out["sent_hashes"] == []


def test_validate_state_filters_non_string_hashes(patch_telegram_paths):
    out = patch_telegram_paths._validate_state(
        {"sent_hashes": ["good", 1, None, "also-good"]}
    )
    assert out["sent_hashes"] == ["good", "also-good"]


def test_load_state_returns_default_when_missing(patch_telegram_paths):
    s = patch_telegram_paths.load_state()
    assert s == {
        "last_update_id": 0,
        "sent_hashes": [],
        "pending_authorizations": {},
        "blocked_chat_ids": [],
    }


def test_save_then_load_state_round_trip(patch_telegram_paths):
    tb = patch_telegram_paths
    state = {
        "last_update_id": 42,
        "sent_hashes": ["abc"],
        "pending_authorizations": {"u": "data"},
        "blocked_chat_ids": [{"chat_id": "1", "username": "x"}],
    }
    tb.save_state(state)
    assert tb.load_state() == state


def test_load_state_returns_default_on_corrupt(patch_telegram_paths):
    tb = patch_telegram_paths
    tb.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tb.STATE_FILE.write_text("not json{")
    assert tb.load_state()["last_update_id"] == 0


# ---------------------------------------------------------------------------
# msg_hash
# ---------------------------------------------------------------------------


def test_msg_hash_deterministic(patch_telegram_paths):
    a = {"x": 1, "y": [1, 2]}
    b = {"y": [1, 2], "x": 1}
    assert patch_telegram_paths.msg_hash(a) == patch_telegram_paths.msg_hash(b)


def test_msg_hash_distinguishes_distinct(patch_telegram_paths):
    assert patch_telegram_paths.msg_hash({"x": 1}) != patch_telegram_paths.msg_hash(
        {"x": 2}
    )


def test_msg_hash_length(patch_telegram_paths):
    assert len(patch_telegram_paths.msg_hash({"x": 1})) == 16


# ---------------------------------------------------------------------------
# markdown helpers
# ---------------------------------------------------------------------------


def test_protect_urls_in_markdown_wraps_url(patch_telegram_paths):
    out = patch_telegram_paths.protect_urls_in_markdown(
        "see https://example.com/x_y now"
    )
    assert "`https://example.com/x_y`" in out


def test_protect_urls_in_markdown_does_not_double_wrap(patch_telegram_paths):
    text = "see `https://example.com/x` now"
    assert patch_telegram_paths.protect_urls_in_markdown(text) == text


def test_strip_markdown_preserve_code(patch_telegram_paths):
    text = "*hello* `https://x.com/a_b` _world_"
    out = patch_telegram_paths._strip_markdown_preserve_code(text)
    assert "*" not in out and "_world_" not in out
    # URL preserved without backticks but with original content.
    assert "https://x.com/a_b" in out


def test_escape_markdown_v1(patch_telegram_paths):
    assert (
        patch_telegram_paths.escape_markdown("*bold* _italic_ [link]", version=1)
        == r"\*bold\* \_italic\_ \[link]"
    )


def test_escape_markdown_v2_full(patch_telegram_paths):
    s = patch_telegram_paths.escape_markdown("a.b!", version=2)
    assert s == r"a\.b\!"


def test_escape_markdown_v2_pre_only_escapes_backslash_and_backtick(
    patch_telegram_paths,
):
    s = patch_telegram_paths.escape_markdown("a*b`c\\d", version=2, entity_type="pre")
    assert s == "a*b\\`c\\\\d"


def test_escape_markdown_invalid_version(patch_telegram_paths):
    with pytest.raises(ValueError):
        patch_telegram_paths.escape_markdown("x", version=3)


def test_mention_markdown_v1(patch_telegram_paths):
    assert (
        patch_telegram_paths.mention_markdown(42, "Bob", version=1)
        == "[Bob](tg://user?id=42)"
    )


def test_mention_markdown_v2_escapes_name(patch_telegram_paths):
    assert (
        patch_telegram_paths.mention_markdown(42, "B.ob", version=2)
        == r"[B\.ob](tg://user?id=42)"
    )


# ---------------------------------------------------------------------------
# chat history
# ---------------------------------------------------------------------------


def test_load_chat_history_empty_when_missing(patch_telegram_paths):
    assert patch_telegram_paths.load_chat_history() == {}


def test_save_then_load_chat_history(patch_telegram_paths):
    tb = patch_telegram_paths
    h = {
        "42": [{"role": "user", "text": "hi", "timestamp": "2026-04-30T12:00:00+00:00"}]
    }
    tb.save_chat_history(h)
    assert tb.load_chat_history() == h


def test_append_chat_message_truncates_to_50(patch_telegram_paths):
    h: dict = {}
    for i in range(60):
        patch_telegram_paths.append_chat_message(h, "42", "user", f"m{i}")
    assert len(h["42"]) == 50
    assert h["42"][0]["text"] == "m10"
    assert h["42"][-1]["text"] == "m59"


def test_build_chat_context_empty_when_no_history(patch_telegram_paths):
    assert patch_telegram_paths.build_chat_context({}, "42") == ""


def test_build_chat_context_filters_old_but_keeps_last_bot(
    patch_telegram_paths, frozen_now
):
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    frozen_now(now)
    history = {
        "c": [
            # Old user message — outside 24h, should drop.
            {
                "role": "user",
                "text": "old",
                "timestamp": (now - timedelta(days=3)).isoformat(),
            },
            # Old bot reply — outside 24h, MUST be retained.
            {
                "role": "bot",
                "text": "old-reply",
                "timestamp": (now - timedelta(days=3)).isoformat(),
            },
            # Fresh user message.
            {
                "role": "user",
                "text": "fresh",
                "timestamp": (now - timedelta(minutes=5)).isoformat(),
            },
        ]
    }
    out = patch_telegram_paths.build_chat_context(history, "c")
    assert "old-reply" in out
    assert "fresh" in out
    assert "old" not in out.replace("old-reply", "")  # no other "old" tokens


# ---------------------------------------------------------------------------
# tg() — Telegram API wrapper
# ---------------------------------------------------------------------------


def test_tg_returns_result_on_ok(mocker, patch_telegram_paths):
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"ok": True, "result": {"id": 1}}
    post = mocker.patch.object(patch_telegram_paths.requests, "post", return_value=resp)

    out = patch_telegram_paths.tg("TOK", "sendMessage", chat_id=42, text="hi")

    assert out == {"id": 1}
    args, kwargs = post.call_args
    assert "/botTOK/sendMessage" in args[0]
    assert kwargs["json"] == {"chat_id": 42, "text": "hi"}


def test_tg_returns_none_when_api_reports_error(mocker, patch_telegram_paths):
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"ok": False, "description": "bad"}
    mocker.patch.object(patch_telegram_paths.requests, "post", return_value=resp)
    assert patch_telegram_paths.tg("TOK", "sendMessage") is None


def test_tg_returns_none_on_non_json_response(mocker, patch_telegram_paths):
    resp = mocker.Mock(status_code=500, text="<html>")
    resp.json.side_effect = ValueError("not json")
    mocker.patch.object(patch_telegram_paths.requests, "post", return_value=resp)
    assert patch_telegram_paths.tg("TOK", "sendMessage") is None


def test_tg_uses_long_poll_timeout_for_get_updates(mocker, patch_telegram_paths):
    resp = mocker.Mock(status_code=200)
    resp.json.return_value = {"ok": True, "result": []}
    post = mocker.patch.object(patch_telegram_paths.requests, "post", return_value=resp)

    patch_telegram_paths.tg("TOK", "getUpdates")

    _, kwargs = post.call_args
    assert kwargs["timeout"] == patch_telegram_paths.POLL_TIMEOUT + 5


def test_tg_swallows_request_exceptions(mocker, patch_telegram_paths):
    mocker.patch.object(
        patch_telegram_paths.requests,
        "post",
        side_effect=patch_telegram_paths.requests.exceptions.ConnectionError("down"),
    )
    assert patch_telegram_paths.tg("TOK", "sendMessage") is None


# ---------------------------------------------------------------------------
# extract_media
# ---------------------------------------------------------------------------


def test_extract_media_picks_largest_photo(patch_telegram_paths):
    msg = {"photo": [{"file_id": "small"}, {"file_id": "big"}]}
    assert patch_telegram_paths.extract_media(msg) == [("big", "image", "photo.jpg")]


def test_extract_media_document_uses_filename(patch_telegram_paths):
    msg = {"document": {"file_id": "fid", "file_name": "report.pdf"}}
    assert patch_telegram_paths.extract_media(msg) == [
        ("fid", "document", "report.pdf")
    ]


def test_extract_media_audio_default_filename(patch_telegram_paths):
    msg = {"audio": {"file_id": "abcdefghij"}}
    out = patch_telegram_paths.extract_media(msg)
    assert out[0][0] == "abcdefghij"
    assert out[0][1] == "audio"
    assert out[0][2].endswith(".mp3")


def test_extract_media_empty(patch_telegram_paths):
    assert patch_telegram_paths.extract_media({"text": "hi"}) == []


def test_extract_media_handles_multiple_kinds(patch_telegram_paths):
    msg = {
        "photo": [{"file_id": "p1"}],
        "document": {"file_id": "d1", "file_name": "x.csv"},
    }
    types = {kind for _, kind, _ in patch_telegram_paths.extract_media(msg)}
    assert types == {"image", "document"}


# ---------------------------------------------------------------------------
# _format_outbox_msg
# ---------------------------------------------------------------------------


def test_format_outbox_msg_needs_human_badge(patch_telegram_paths):
    out = patch_telegram_paths._format_outbox_msg(
        {"type": "needs_human", "subject": "halp", "content": "stuck on X"}
    )
    assert "ACTION REQUIRED" in out
    assert "halp" in out
    assert "stuck on X" in out


def test_format_outbox_msg_falls_back_to_json(patch_telegram_paths):
    # Empty type+subject+content: dumps the whole payload (escaped).
    out = patch_telegram_paths._format_outbox_msg(
        {"type": "", "subject": "", "content": ""}
    )
    assert "type" in out


def test_format_outbox_msg_status_badge(patch_telegram_paths):
    out = patch_telegram_paths._format_outbox_msg(
        {"type": "status", "subject": "s", "content": "c"}
    )
    assert "Status Report" in out
