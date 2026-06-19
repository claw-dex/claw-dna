"""Unit tests for services/envelope.py — the unified message envelope helpers."""

from __future__ import annotations

import envelope as e

# ---------------------------------------------------------------------------
# make_from / parse_from
# ---------------------------------------------------------------------------


def test_make_from_source_primary_drops_empty():
    # First positional is now `source`; transport omitted when not passed.
    out = e.make_from("slack", channel="D1", user_id="", handle=None, role="owner")
    assert out == {"source": "slack", "channel": "D1", "role": "owner"}


def test_make_from_with_distinct_transport():
    out = e.make_from(
        "whatsapp",
        transport="webhook",
        channel="123",
        user_id="456",
        handle="vincent",
        role="member",
    )
    assert out == {
        "source": "whatsapp",
        "transport": "webhook",
        "channel": "123",
        "user_id": "456",
        "handle": "vincent",
        "role": "member",
    }


def test_parse_from_dict_passthrough():
    d = {"transport": "slack", "handle": "x"}
    assert e.parse_from(d) is d


def test_parse_from_legacy_string_wraps_raw():
    assert e.parse_from("messages/external/bot/outbox.json") == {
        "raw": "messages/external/bot/outbox.json"
    }


def test_parse_from_empty_and_other():
    assert e.parse_from("") == {}
    assert e.parse_from(None) == {}
    assert e.parse_from(123) == {}


# ---------------------------------------------------------------------------
# id minting
# ---------------------------------------------------------------------------


def test_new_id_unique():
    assert e.new_id() != e.new_id()


def test_ensure_id_idempotent():
    env = {"type": "message"}
    first = e.ensure_id(env)
    assert env["id"] == first
    # Calling again does not change the id.
    assert e.ensure_id(env) == first


# ---------------------------------------------------------------------------
# dedup_key / msg_hash
# ---------------------------------------------------------------------------


def test_msg_hash_order_independent():
    assert e.msg_hash({"a": 1, "b": 2}) == e.msg_hash({"b": 2, "a": 1})
    assert len(e.msg_hash({"a": 1})) == 16


def test_dedup_key_prefers_id():
    msg = {"type": "info", "content": "x", "id": "u-1"}
    assert e.dedup_key(msg) == "id:u-1"
    # Mutating another field leaves the key stable.
    assert e.dedup_key(dict(msg, sent=True)) == "id:u-1"


def test_dedup_key_falls_back_to_hash():
    msg = {"type": "info", "content": "x"}
    assert e.dedup_key(msg) == e.msg_hash(msg)


# ---------------------------------------------------------------------------
# message_source
# ---------------------------------------------------------------------------


def test_message_source_prefers_from_source():
    msg = {"from": {"transport": "slack", "source": "slack"}, "source": "legacy"}
    assert e.message_source(msg) == "slack"


def test_message_source_falls_back_to_top_level():
    # Legacy message (string/absent from) → top-level source.
    assert e.message_source({"source": "webhook"}) == "webhook"
    assert e.message_source({"from": "legacy-string", "source": "portal"}) == "portal"


def test_message_source_none_when_absent_or_non_dict():
    assert e.message_source({}) is None
    assert e.message_source({"from": {"transport": "slack"}}) is None
    assert e.message_source("not-a-dict") is None
    assert e.message_source(None) is None


# ---------------------------------------------------------------------------
# make_to / reply_target (structured outbox `to`)
# ---------------------------------------------------------------------------


def test_make_to_drops_empty():
    assert e.make_to(in_reply_to="m1") == {"in_reply_to": "m1"}
    assert e.make_to(handle="@v") == {"handle": "@v"}
    assert e.make_to(in_reply_to="m1", handle="@v") == {
        "in_reply_to": "m1",
        "handle": "@v",
    }
    assert e.make_to() == {}


def test_reply_target_structured_to():
    assert e.reply_target({"to": {"in_reply_to": "m1"}}) == "m1"
    assert e.reply_target({"to": {"handle": "@v"}}) == "@v"
    # in_reply_to wins over handle within `to`.
    assert e.reply_target({"to": {"in_reply_to": "m1", "handle": "@v"}}) == "m1"


def test_reply_target_legacy_fallback():
    assert e.reply_target({"in_reply_to": "old"}) == "old"  # legacy top-level
    assert e.reply_target({"to": "@legacy"}) == "@legacy"  # legacy string `to`
    # legacy top-level in_reply_to still wins when `to` lacks one.
    assert e.reply_target({"to": {"handle": "@v"}, "in_reply_to": "x"}) == "x"


def test_reply_target_none_when_unaddressed():
    assert e.reply_target({"type": "info"}) is None
    assert e.reply_target({"to": {}}) is None
    assert e.reply_target("not-a-dict") is None


# ---------------------------------------------------------------------------
# origin map: record / resolve / handle / sanitize
# ---------------------------------------------------------------------------


def _frm(handle, uid="U1", channel="D1"):
    return e.make_from(
        "slack", channel=channel, user_id=uid, handle=handle, role="member"
    )


def test_record_and_resolve_origin():
    store: dict = {}
    e.record_origin(store, msg_id="m1", from_obj=_frm("alice"), origin_ref="ts-1")
    frm, ref = e.resolve_origin(store, "m1")
    assert frm["handle"] == "alice"
    assert ref == "ts-1"
    assert e.resolve_origin(store, "nope") is None
    assert e.resolve_origin(store, "") is None


def test_record_origin_trims_to_cap():
    store: dict = {}
    total = e.ORIGIN_MAP_MAX + 50
    for i in range(total):
        e.record_origin(store, msg_id=f"m{i}", from_obj=_frm(f"u{i}"), origin_ref=i)
    assert len(store) == e.ORIGIN_MAP_MAX
    # Oldest were dropped; newest retained.
    assert "m0" not in store
    assert f"m{total - 1}" in store


def test_resolve_handle_matches_handle_and_user_id():
    store: dict = {}
    e.record_origin(
        store, msg_id="m1", from_obj=_frm("Vincent", uid="U9"), origin_ref="t"
    )
    assert e.resolve_handle(store, "@vincent")["user_id"] == "U9"
    assert e.resolve_handle(store, "vincent")["user_id"] == "U9"
    assert e.resolve_handle(store, "U9")["handle"] == "Vincent"
    assert e.resolve_handle(store, "ghost") is None


def test_resolve_handle_returns_most_recent():
    store: dict = {}
    e.record_origin(
        store, msg_id="m1", from_obj=_frm("dup", channel="DA"), origin_ref=1
    )
    e.record_origin(
        store, msg_id="m2", from_obj=_frm("dup", channel="DB"), origin_ref=2
    )
    assert e.resolve_handle(store, "dup")["channel"] == "DB"


def test_sanitize_origin_map_drops_malformed():
    out = e.sanitize_origin_map(
        {
            "ok": {"from": {"transport": "slack"}, "origin_ref": "t"},
            "bad_value": "not-a-dict",
            7: {"from": {}},  # non-string key
            "bad_from": {"from": "should-be-dict"},
        }
    )
    assert set(out) == {"ok"}


def test_sanitize_origin_map_non_dict():
    assert e.sanitize_origin_map("nope") == {}
    assert e.sanitize_origin_map(None) == {}
