"""Unit tests for services/slack_bridge.py.

Mirrors test_telegram_bridge.py: the module is reached through the
``patch_slack_paths`` fixture (returns the patched module as ``sb``) so every
hard-coded ``/agent`` path lands in a tmp sandbox. Network calls go through a
``FakeSlackClient``; KeePass is faked by injecting ``scripts.keepass`` into
``sys.modules``.
"""

from __future__ import annotations

import json
import sys
import threading
import types
from datetime import datetime, timedelta, timezone

import pytest

FROZEN = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeApp:
    """Captures Bolt event/command listeners registered by register_handlers."""

    def __init__(self):
        self.events: dict = {}
        self.commands: dict = {}

    def event(self, name):
        def deco(fn):
            self.events[name] = fn
            return fn

        return deco

    def command(self, name):
        def deco(fn):
            self.commands[name] = fn
            return fn

        return deco


class _FakeResp:
    """Minimal context-manager stand-in for requests.get(stream=True)."""

    def __init__(self, chunks, headers=None):
        self._chunks = chunks
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        for c in self._chunks:
            yield c


class FakeSay:
    """Stand-in for the Assistant ``say`` helper (auto-threaded in real Slack)."""

    def __init__(self):
        self.said: list = []

    def __call__(self, text=None, **kw):
        self.said.append(text if text is not None else kw.get("text"))
        return {"ok": True, "ts": "1700000000.000200"}


class Recorder:
    """Captures a single positional value per call (set_status / set_title)."""

    def __init__(self):
        self.calls: list = []

    def __call__(self, value=None, **kw):
        self.calls.append(value if value is not None else kw)


class SuggestRecorder:
    def __init__(self):
        self.prompts = None

    def __call__(self, prompts=None):
        self.prompts = prompts


class FakeSlackClient:
    """Records chat_postMessage calls and answers users_info lookups."""

    def __init__(self, names=None, fail=False):
        self.sent: list[dict] = []
        self.names = names or {}
        self.fail = fail

    def chat_postMessage(self, channel, text, mrkdwn=True, thread_ts=None):
        if self.fail:
            raise RuntimeError("boom")
        self._counter = getattr(self, "_counter", 0) + 1
        ts = f"1700000000.{self._counter:06d}"
        self.sent.append(
            {
                "channel": channel,
                "text": text,
                "mrkdwn": mrkdwn,
                "thread_ts": thread_ts,
                "ts": ts,
            }
        )
        return {"ok": True, "ts": ts}

    def users_info(self, user):
        if user == "RAISE":
            raise RuntimeError("no such user")
        name = self.names.get(user, user)
        return {"ok": True, "user": {"id": user, "name": name, "profile": {}}}


@pytest.fixture
def fake_keepass(monkeypatch):
    """Inject a fake scripts.keepass that records stored credentials."""
    stored: dict[str, str] = {}

    fake = types.ModuleType("scripts.keepass")

    def _get(title):
        return stored.get(title)

    def _store(title, username, password, url="", notes="", group=""):
        stored[title] = password
        return True

    fake.get_credential = _get
    fake.store_credential = _store

    pkg = sys.modules.get("scripts")
    if pkg is None:
        pkg = types.ModuleType("scripts")
        monkeypatch.setitem(sys.modules, "scripts", pkg)
    pkg.keepass = fake
    monkeypatch.setitem(sys.modules, "scripts.keepass", fake)
    return stored


def make_ctx(sb, **over):
    ctx = {
        "bot_token": "xoxb-test",
        "bot_user_id": "BOT1",
        "owner_user_id": None,
        "owner_username": None,
        "chat_ids": [],
        "state": {"sent_hashes": [], "processed_keys": []},
        "chat_history": {},
        "user_name_cache": {},
        "lock": threading.Lock(),
    }
    ctx.update(over)
    return ctx


# ---------------------------------------------------------------------------
# Chat-id helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, []),
        ("", []),
        ("C1", ["C1"]),
        ("C1,C2 , C3", ["C1", "C2", "C3"]),
        (" , ,", []),
    ],
)
def test_parse_chat_ids(patch_slack_paths, raw, expected):
    assert patch_slack_paths.parse_chat_ids(raw) == expected


def test_serialize_round_trip(patch_slack_paths):
    sb = patch_slack_paths
    ids = ["D1", "C2", "G3"]
    assert sb.parse_chat_ids(sb.serialize_chat_ids(ids)) == ids


@pytest.mark.parametrize(
    "text,user,expected",
    [
        ("hey gosu can you help", "gosu", True),
        ("ping @gosu now", "gosu", True),
        ("GOSU loud", "gosu", True),
        ("gosulike not a match", "gosu", False),
        ("nothing here", "gosu", False),
        ("anything", "", False),
    ],
)
def test_contains_username(patch_slack_paths, text, user, expected):
    assert patch_slack_paths.contains_username(text, user) is expected


# ---------------------------------------------------------------------------
# mrkdwn helpers
# ---------------------------------------------------------------------------


def test_escape_slack(patch_slack_paths):
    sb = patch_slack_paths
    assert sb.escape_slack("a & b < c > d") == "a &amp; b &lt; c &gt; d"
    # formatting markers are intentionally NOT escaped
    assert sb.escape_slack("*bold* _it_ `code`") == "*bold* _it_ `code`"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("<@BOT1> status", "status"),
        ("<@BOT1|claw> hello", "hello"),
        ("no mention here", "no mention here"),
        ("  <@BOT1>   goals 5 ", "goals 5"),
    ],
)
def test_strip_bot_mention(patch_slack_paths, text, expected):
    assert patch_slack_paths.strip_bot_mention(text, "BOT1") == expected


@pytest.mark.parametrize(
    "text,cmd,args",
    [
        ("status", "status", []),
        ("/status", "status", []),
        ("!status", "status", []),
        ("goals 10", "goals", ["10"]),
        ("STATUS", "status", []),
        ("remind 30m do thing", "remind", ["30m", "do", "thing"]),
        ("hello there", None, []),
        ("", None, []),
    ],
)
def test_parse_command(patch_slack_paths, text, cmd, args):
    assert patch_slack_paths.parse_command(text) == (cmd, args)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def test_msg_hash_stable_and_distinct(patch_slack_paths):
    sb = patch_slack_paths
    a = {"type": "info", "content": "x"}
    b = {"content": "x", "type": "info"}  # key order should not matter
    c = {"type": "info", "content": "y"}
    assert sb.msg_hash(a) == sb.msg_hash(b)
    assert sb.msg_hash(a) != sb.msg_hash(c)
    assert len(sb.msg_hash(a)) == 16


def test_validate_state_good(patch_slack_paths):
    sb = patch_slack_paths
    data = {"sent_hashes": ["a"], "processed_keys": ["C1:1"]}
    assert sb._validate_state(data) == data


def test_validate_state_wrong_types(patch_slack_paths):
    sb = patch_slack_paths
    out = sb._validate_state({"sent_hashes": "nope", "processed_keys": 5})
    assert out["sent_hashes"] == []
    assert out["processed_keys"] == []


def test_validate_state_non_dict(patch_slack_paths):
    sb = patch_slack_paths
    out = sb._validate_state(["not", "a", "dict"])
    assert out == {"sent_hashes": [], "processed_keys": [], "daily_threads": {}}


def test_validate_state_daily_threads_wrong_type(patch_slack_paths):
    sb = patch_slack_paths
    out = sb._validate_state(
        {"sent_hashes": [], "processed_keys": [], "daily_threads": "nope"}
    )
    assert out["daily_threads"] == {}


def test_validate_state_drops_malformed_daily_thread_entries(patch_slack_paths):
    sb = patch_slack_paths
    out = sb._validate_state(
        {
            "sent_hashes": [],
            "processed_keys": [],
            "daily_threads": {
                "D1": {"date": "2026-04-30", "thread_ts": "1.0"},  # valid
                "D2": "garbage",  # not a dict
                "D3": {"date": 5, "thread_ts": "2.0"},  # wrong inner type
                "D4": {"date": "2026-04-30"},  # missing thread_ts
            },
        }
    )
    assert out["daily_threads"] == {"D1": {"date": "2026-04-30", "thread_ts": "1.0"}}


def test_send_outbox_tolerates_corrupt_daily_thread(
    patch_slack_paths, agent_root, frozen_now
):
    sb = patch_slack_paths
    frozen_now(FROZEN)
    (agent_root / "messages" / "outbox.json").write_text(
        json.dumps([{"type": "info", "content": "hi"}])
    )
    client = FakeSlackClient()
    ctx = make_ctx(sb, chat_ids=["D1"])
    ctx["state"]["daily_threads"] = {"D1": "garbage"}  # corrupt inner entry
    # must not raise (would otherwise wedge the main loop) and still deliver
    sb.send_outbox_messages(client, ctx)
    assert len(client.sent) == 1
    assert client.sent[0]["thread_ts"] is None


def test_validate_state_filters_non_strings(patch_slack_paths):
    sb = patch_slack_paths
    out = sb._validate_state({"sent_hashes": ["a", 1, None, "b"], "processed_keys": []})
    assert out["sent_hashes"] == ["a", "b"]


def test_state_round_trip(patch_slack_paths):
    sb = patch_slack_paths
    state = {
        "sent_hashes": ["h1"],
        "processed_keys": ["C1:1.0"],
        "daily_threads": {"D1": {"date": "2026-04-30", "thread_ts": "1.0"}},
    }
    sb.save_state(state)
    assert sb.load_state() == state


def test_load_state_missing_returns_default(patch_slack_paths):
    sb = patch_slack_paths
    assert sb.load_state() == {
        "sent_hashes": [],
        "processed_keys": [],
        "daily_threads": {},
    }


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------


def test_append_chat_message_caps_at_50(patch_slack_paths, frozen_now):
    sb = patch_slack_paths
    frozen_now(FROZEN)
    hist: dict = {}
    for i in range(60):
        sb.append_chat_message(hist, "C1", "user", f"m{i}")
    assert len(hist["C1"]) == 50
    assert hist["C1"][0]["text"] == "m10"
    assert hist["C1"][-1]["text"] == "m59"


def test_build_chat_context_empty(patch_slack_paths):
    assert patch_slack_paths.build_chat_context({}, "C1") == ""


def test_build_chat_context_includes_recent(patch_slack_paths, frozen_now):
    sb = patch_slack_paths
    frozen_now(FROZEN)
    hist = {
        "C1": [
            {"role": "user", "text": "hi", "timestamp": FROZEN.isoformat()},
            {"role": "bot", "text": "hello", "timestamp": FROZEN.isoformat()},
        ]
    }
    ctx = sb.build_chat_context(hist, "C1")
    assert "<message>User: hi</message>" in ctx
    assert "<message>Agent: hello</message>" in ctx


def test_build_chat_context_drops_stale_user_keeps_last_bot(
    patch_slack_paths, frozen_now
):
    sb = patch_slack_paths
    frozen_now(FROZEN)
    old = (FROZEN - timedelta(hours=48)).isoformat()
    hist = {
        "C1": [
            {"role": "user", "text": "ancient", "timestamp": old},
            {"role": "bot", "text": "old-reply", "timestamp": old},
        ]
    }
    ctx = sb.build_chat_context(hist, "C1")
    # stale user message excluded, but last bot reply always retained
    assert "ancient" not in ctx
    assert "old-reply" in ctx


def test_chat_history_round_trip(patch_slack_paths, frozen_now):
    sb = patch_slack_paths
    frozen_now(FROZEN)
    hist: dict = {}
    sb.append_chat_message(hist, "C1", "user", "hi")
    sb.save_chat_history(hist)
    assert sb.load_chat_history() == hist


# ---------------------------------------------------------------------------
# Heartbeat / ack
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=5), "5 seconds ago"),
        (timedelta(minutes=1), "1 minute ago"),
        (timedelta(hours=2), "2 hours ago"),
        (timedelta(days=3), "3 days ago"),
    ],
)
def test_relative_time(patch_slack_paths, frozen_now, delta, expected):
    sb = patch_slack_paths
    frozen_now(FROZEN)
    assert sb.relative_time(FROZEN - delta) == expected


def test_build_ack_with_heartbeat(patch_slack_paths, frozen_now, agent_root):
    sb = patch_slack_paths
    frozen_now(FROZEN)
    hb = FROZEN - timedelta(minutes=10)
    (agent_root / "memory" / "state.json").write_text(
        json.dumps({"last_heartbeat": hb.isoformat()})
    )
    msg = sb.build_ack_message()
    assert "10 minutes ago" in msg


def test_build_ack_without_heartbeat(patch_slack_paths):
    msg = patch_slack_paths.build_ack_message()
    assert msg == "Got it! I'll get back to you in the next cycle."


# ---------------------------------------------------------------------------
# Outbox formatting + sending
# ---------------------------------------------------------------------------


def test_format_outbox_badges(patch_slack_paths):
    sb = patch_slack_paths
    out = sb._format_outbox_msg(
        {"type": "needs_human", "subject": "Help <me>", "content": "do & think"}
    )
    assert "🚨 *ACTION REQUIRED*" in out
    assert "*Help &lt;me&gt;*" in out
    assert "do &amp; think" in out


def test_format_outbox_fallback_to_json(patch_slack_paths):
    sb = patch_slack_paths
    out = sb._format_outbox_msg({"type": "weird"})
    assert "weird" in out


def test_slack_send_records_and_truncates(patch_slack_paths):
    sb = patch_slack_paths
    client = FakeSlackClient()
    long_text = "x" * (sb.SLACK_MAX_LEN + 100)
    sb.slack_send(client, "C1", long_text)
    assert len(client.sent) == 1
    assert len(client.sent[0]["text"]) <= sb.SLACK_MAX_LEN
    assert client.sent[0]["text"].endswith("...")


def test_slack_send_empty_channel(patch_slack_paths):
    client = FakeSlackClient()
    assert patch_slack_paths.slack_send(client, "", "hi") is None
    assert client.sent == []


def test_slack_send_swallows_errors(patch_slack_paths):
    client = FakeSlackClient(fail=True)
    assert patch_slack_paths.slack_send(client, "C1", "hi") is None


def test_send_outbox_broadcasts_and_dedups(patch_slack_paths, agent_root):
    sb = patch_slack_paths
    outbox = [
        {"type": "needs_human", "subject": "Blocked", "content": "need key"},
        {"type": "info", "content": "fyi"},
    ]
    (agent_root / "messages" / "outbox.json").write_text(json.dumps(outbox))

    client = FakeSlackClient()
    ctx = make_ctx(sb, chat_ids=["D1", "C2"])
    sb.send_outbox_messages(client, ctx)

    # 2 messages * 2 channels = 4 sends
    assert len(client.sent) == 4
    assert len(ctx["state"]["sent_hashes"]) == 2

    # second run is a no-op (already-sent hashes)
    client.sent.clear()
    sb.send_outbox_messages(client, ctx)
    assert client.sent == []


def test_send_outbox_no_channels(patch_slack_paths, agent_root):
    sb = patch_slack_paths
    (agent_root / "messages" / "outbox.json").write_text(
        json.dumps([{"type": "info", "content": "x"}])
    )
    client = FakeSlackClient()
    ctx = make_ctx(sb, chat_ids=[])
    sb.send_outbox_messages(client, ctx)
    assert client.sent == []


def test_send_outbox_threads_per_day(patch_slack_paths, agent_root, frozen_now):
    sb = patch_slack_paths
    frozen_now(FROZEN)
    outbox = [
        {"type": "info", "content": "first of the day"},
        {"type": "info", "content": "second of the day"},
    ]
    (agent_root / "messages" / "outbox.json").write_text(json.dumps(outbox))
    client = FakeSlackClient()
    ctx = make_ctx(sb, chat_ids=["D1"])

    sb.send_outbox_messages(client, ctx)

    assert len(client.sent) == 2
    # first message roots the day's thread (no thread_ts)
    assert client.sent[0]["thread_ts"] is None
    # second nests under the first message's ts → one conversation
    assert client.sent[1]["thread_ts"] == client.sent[0]["ts"]
    # the day's thread root is persisted in state
    entry = ctx["state"]["daily_threads"]["D1"]
    assert entry["date"] == "2026-04-30"
    assert entry["thread_ts"] == client.sent[0]["ts"]


def test_send_outbox_new_day_starts_new_thread(
    patch_slack_paths, agent_root, frozen_now
):
    sb = patch_slack_paths
    frozen_now(FROZEN)  # 2026-04-30
    (agent_root / "messages" / "outbox.json").write_text(
        json.dumps([{"type": "info", "content": "today's message"}])
    )
    client = FakeSlackClient()
    # yesterday's thread root is stale → today's first message must NOT nest
    ctx = make_ctx(sb, chat_ids=["D1"])
    ctx["state"]["daily_threads"] = {
        "D1": {"date": "2026-04-29", "thread_ts": "1600000000.000001"}
    }

    sb.send_outbox_messages(client, ctx)

    assert client.sent[0]["thread_ts"] is None  # new top-level root for the new day
    assert ctx["state"]["daily_threads"]["D1"]["date"] == "2026-04-30"


def test_send_outbox_per_channel_threads(patch_slack_paths, agent_root, frozen_now):
    sb = patch_slack_paths
    frozen_now(FROZEN)
    outbox = [
        {"type": "info", "content": "m1"},
        {"type": "info", "content": "m2"},
    ]
    (agent_root / "messages" / "outbox.json").write_text(json.dumps(outbox))
    client = FakeSlackClient()
    ctx = make_ctx(sb, chat_ids=["D1", "D2"])

    sb.send_outbox_messages(client, ctx)

    # each channel keeps its own independent thread root + nesting
    by_channel: dict = {}
    for s in client.sent:
        by_channel.setdefault(s["channel"], []).append(s)
    assert set(by_channel) == {"D1", "D2"}
    for cid, msgs in by_channel.items():
        assert len(msgs) == 2
        assert msgs[0]["thread_ts"] is None
        assert msgs[1]["thread_ts"] == msgs[0]["ts"]
        assert ctx["state"]["daily_threads"][cid]["thread_ts"] == msgs[0]["ts"]


def test_send_outbox_no_ts_response_falls_back_top_level(
    patch_slack_paths, agent_root, frozen_now
):
    sb = patch_slack_paths
    frozen_now(FROZEN)

    class NoTsClient(FakeSlackClient):
        def chat_postMessage(self, channel, text, mrkdwn=True, thread_ts=None):
            self.sent.append({"channel": channel, "text": text, "thread_ts": thread_ts})
            return {"ok": True}  # success but no 'ts'

    outbox = [
        {"type": "info", "content": "m1"},
        {"type": "info", "content": "m2"},
    ]
    (agent_root / "messages" / "outbox.json").write_text(json.dumps(outbox))
    client = NoTsClient()
    ctx = make_ctx(sb, chat_ids=["D1"])

    sb.send_outbox_messages(client, ctx)

    # no root could be recorded → both posts stay top-level, nothing persisted
    assert [s["thread_ts"] for s in client.sent] == [None, None]
    assert ctx["state"]["daily_threads"] == {}


def test_send_outbox_prunes_stale_other_channel(
    patch_slack_paths, agent_root, frozen_now
):
    sb = patch_slack_paths
    frozen_now(FROZEN)  # 2026-04-30
    (agent_root / "messages" / "outbox.json").write_text(
        json.dumps([{"type": "info", "content": "to D1"}])
    )
    client = FakeSlackClient()
    ctx = make_ctx(sb, chat_ids=["D1"])
    # D2 has a stale (yesterday) root and gets no message today → must be pruned
    ctx["state"]["daily_threads"] = {
        "D2": {"date": "2026-04-29", "thread_ts": "1600000000.000009"}
    }

    sb.send_outbox_messages(client, ctx)

    assert "D2" not in ctx["state"]["daily_threads"]
    assert ctx["state"]["daily_threads"]["D1"]["date"] == "2026-04-30"


# ---------------------------------------------------------------------------
# User name resolution
# ---------------------------------------------------------------------------


def test_get_user_name_caches(patch_slack_paths):
    sb = patch_slack_paths
    client = FakeSlackClient(names={"U1": "alice"})
    cache: dict = {}
    assert sb.get_user_name(client, "U1", cache) == "alice"
    assert cache["U1"] == "alice"


def test_get_user_name_fallback(patch_slack_paths):
    sb = patch_slack_paths
    client = FakeSlackClient()
    assert sb.get_user_name(client, "RAISE", {}) == "RAISE"


# ---------------------------------------------------------------------------
# Media / files
# ---------------------------------------------------------------------------


def test_extract_files(patch_slack_paths):
    sb = patch_slack_paths
    event = {"files": [{"id": "F1", "name": "a.pdf"}, {"no_id": True}, "junk"]}
    files = sb.extract_files(event)
    assert len(files) == 1
    assert files[0]["id"] == "F1"


def test_extract_files_none(patch_slack_paths):
    assert patch_slack_paths.extract_files({}) == []


@pytest.mark.parametrize(
    "mimetype,expected",
    [
        ("image/png", "image"),
        ("audio/mpeg", "audio"),
        ("application/pdf", "document"),
        ("", "document"),
    ],
)
def test_media_type_for(patch_slack_paths, mimetype, expected):
    assert patch_slack_paths._media_type_for({"mimetype": mimetype}) == expected


# ---------------------------------------------------------------------------
# Inbox content + event predicates
# ---------------------------------------------------------------------------


def test_build_inbox_content_plain(patch_slack_paths):
    sb = patch_slack_paths
    assert sb.build_inbox_content("alice", "hello", [], "") == "[Slack @alice]: hello"


def test_build_inbox_content_with_attachments_and_context(patch_slack_paths):
    sb = patch_slack_paths
    content = sb.build_inbox_content(
        "alice",
        "see file",
        [{"type": "document", "path": "/p/a.pdf", "filename": "a.pdf"}],
        "<message>User: earlier</message>",
    )
    assert "[Attachments]" in content
    assert "- document: /p/a.pdf" in content
    assert "[Previous conversation context (last 24h)]" in content
    assert "[New message]" in content


@pytest.mark.parametrize(
    "event,expected",
    [
        ({"bot_id": "B1"}, True),
        ({"user": "BOT1"}, True),
        ({"subtype": "message_changed"}, True),
        ({"subtype": "file_share", "user": "U1"}, False),
        ({"user": "U1"}, False),
    ],
)
def test_is_skippable_message(patch_slack_paths, event, expected):
    assert patch_slack_paths._is_skippable_message(event, "BOT1") is expected


def test_event_key(patch_slack_paths):
    sb = patch_slack_paths
    assert sb._event_key("C1", {"ts": "1.5"}) == "C1:1.5"
    assert sb._event_key("C1", {"event_ts": "2.0"}) == "C1:2.0"


@pytest.mark.parametrize(
    "raw,ctx_over,expected",
    [
        ("hey <@OWN1> help", {"owner_user_id": "OWN1"}, True),
        ("hey gosu help", {"owner_username": "gosu"}, True),
        ("unrelated", {"owner_user_id": "OWN1", "owner_username": "gosu"}, False),
        ("", {"owner_user_id": "OWN1"}, False),
    ],
)
def test_mentions_owner(patch_slack_paths, raw, ctx_over, expected):
    sb = patch_slack_paths
    ctx = make_ctx(sb, **ctx_over)
    assert sb._mentions_owner(raw, ctx) is expected


# ---------------------------------------------------------------------------
# Incoming processing (integration with fakes)
# ---------------------------------------------------------------------------


def test_process_incoming_owner_discovery(
    patch_slack_paths, fake_keepass, frozen_now, agent_root
):
    sb = patch_slack_paths
    frozen_now(FROZEN)
    client = FakeSlackClient(names={"U1": "alice"})
    ctx = make_ctx(sb)
    event = {
        "channel": "D1",
        "user": "U1",
        "text": "remember to ship",
        "channel_type": "im",
        "ts": "1.0",
    }
    sb._process_incoming(client, event, ctx)

    # owner auto-discovered + channel authorized + persisted to keepass
    assert ctx["owner_user_id"] == "U1"
    assert "D1" in ctx["chat_ids"]
    assert fake_keepass[sb.KEEPASS_SLACK_OWNER_USERID] == "U1"

    # message written to inbox with source=slack
    inbox = json.loads((agent_root / "messages" / "inbox.json").read_text())
    assert inbox[-1]["source"] == "slack"
    assert "[Slack @alice]: remember to ship" in inbox[-1]["content"]

    # DM gets an ack reply
    assert any("Got it!" in s["text"] for s in client.sent)


def test_process_incoming_rejects_unknown_in_channel(
    patch_slack_paths, fake_keepass, agent_root
):
    sb = patch_slack_paths
    client = FakeSlackClient(names={"U9": "stranger"})
    # owner exists; bot is @mentioned (passes the channel gate) but channel is
    # not authorized and the owner is not referenced → rejected.
    ctx = make_ctx(sb, owner_user_id="OWN1", owner_username="gosu", chat_ids=["D1"])
    event = {
        "channel": "C9",
        "user": "U9",
        "text": "<@BOT1> let me in",
        "channel_type": "channel",
        "ts": "2.0",
    }
    sb._process_incoming(client, event, ctx, is_mention=True)

    assert "C9" not in ctx["chat_ids"]
    assert any("don't know you" in s["text"] for s in client.sent)
    assert not (agent_root / "messages" / "inbox.json").exists()


def test_process_incoming_ignores_unaddressed_channel_message(
    patch_slack_paths, fake_keepass, agent_root
):
    sb = patch_slack_paths
    client = FakeSlackClient(names={"U1": "alice"})
    # Authorized channel, but the bot is not addressed and the owner is not
    # referenced → silently ignored (no flooding the inbox with channel chatter).
    ctx = make_ctx(sb, owner_user_id="OWN1", owner_username="gosu", chat_ids=["C5"])
    event = {
        "channel": "C5",
        "user": "U1",
        "text": "just chatting with the team",
        "channel_type": "channel",
        "ts": "9.0",
    }
    sb._process_incoming(client, event, ctx, is_mention=False)

    assert client.sent == []
    assert not (agent_root / "messages" / "inbox.json").exists()
    # gate returns before recording the dedup key
    assert ctx["state"]["processed_keys"] == []


def test_process_incoming_command_routed(patch_slack_paths, fake_keepass, agent_root):
    sb = patch_slack_paths
    client = FakeSlackClient(names={"U1": "alice"})
    ctx = make_ctx(sb, chat_ids=["D1"])
    event = {
        "channel": "D1",
        "user": "U1",
        "text": "help",
        "channel_type": "im",
        "ts": "3.0",
    }
    sb._process_incoming(client, event, ctx)

    # command answered, nothing queued to the inbox
    assert any("Available Commands" in s["text"] for s in client.sent)
    assert not (agent_root / "messages" / "inbox.json").exists()


def test_process_incoming_dedup(patch_slack_paths, fake_keepass, agent_root):
    sb = patch_slack_paths
    client = FakeSlackClient(names={"U1": "alice"})
    ctx = make_ctx(sb, chat_ids=["D1"])
    event = {
        "channel": "D1",
        "user": "U1",
        "text": "ship it",
        "channel_type": "im",
        "ts": "4.0",
    }
    sb._process_incoming(client, event, ctx)
    sb._process_incoming(client, event, ctx)  # redelivery → ignored

    inbox = json.loads((agent_root / "messages" / "inbox.json").read_text())
    assert len(inbox) == 1


def test_process_incoming_skips_bot_message(patch_slack_paths, agent_root):
    sb = patch_slack_paths
    client = FakeSlackClient()
    ctx = make_ctx(sb, chat_ids=["D1"])
    event = {"channel": "D1", "user": "BOT1", "bot_id": "B1", "text": "hi", "ts": "5.0"}
    sb._process_incoming(client, event, ctx)
    assert client.sent == []
    assert not (agent_root / "messages" / "inbox.json").exists()


# ---------------------------------------------------------------------------
# Command dispatch (memory-file readers)
# ---------------------------------------------------------------------------


def test_dispatch_status(patch_slack_paths, agent_root):
    sb = patch_slack_paths
    (agent_root / "memory" / "state.json").write_text(
        json.dumps(
            {
                "agent_status": "idle",
                "cycle_number": 7,
                "last_heartbeat": "2026-04-30T11:00:00+00:00",
            }
        )
    )
    (agent_root / "memory" / "goal.json").write_text(json.dumps([]))
    client = FakeSlackClient()
    sb.dispatch_command(client, "D1", "status", [], {})
    assert any("Agent Status" in s["text"] for s in client.sent)


def test_dispatch_goals_with_limit(patch_slack_paths, agent_root):
    sb = patch_slack_paths
    goals = [
        {"goal": f"g{i}", "status": "completed", "updated_at": f"2026-04-{i+1:02d}"}
        for i in range(8)
    ]
    (agent_root / "memory" / "goal.json").write_text(json.dumps(goals))
    client = FakeSlackClient()
    sb.dispatch_command(client, "D1", "goals", ["3"], {})
    assert len(client.sent) == 1
    # header should reflect the capped count
    assert "Last 3 goal(s)" in client.sent[0]["text"]


def test_dispatch_heartbeat_rejects_bad_args(patch_slack_paths, mocker):
    sb = patch_slack_paths
    captured = {}

    def fake_handle(client, channel, chat_history, extra):
        captured["extra"] = extra

    mocker.patch.object(sb, "handle_heartbeat_command", side_effect=fake_handle)
    sb.dispatch_command(
        FakeSlackClient(), "D1", "heartbeat", ["--agent-sleep", "--evil"], {}
    )
    assert captured["extra"] == ["--agent-sleep"]


# ---------------------------------------------------------------------------
# DM detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "channel,channel_type,expected",
    [
        ("D123", "", True),  # app_mention in a DM carries no channel_type
        ("C123", "im", True),  # fallback on channel_type
        ("C123", "channel", False),
        ("G123", "group", False),
    ],
)
def test_is_dm_channel(patch_slack_paths, channel, channel_type, expected):
    assert patch_slack_paths._is_dm_channel(channel, channel_type) is expected


# ---------------------------------------------------------------------------
# Inbox write failure → event left for retry
# ---------------------------------------------------------------------------


def test_process_incoming_write_failure_not_marked(
    patch_slack_paths, fake_keepass, mocker
):
    sb = patch_slack_paths
    mocker.patch.object(sb, "write_to_inbox", return_value=False)
    client = FakeSlackClient(names={"U1": "alice"})
    ctx = make_ctx(sb, chat_ids=["D1"])
    event = {
        "channel": "D1",
        "user": "U1",
        "text": "do the thing",
        "channel_type": "im",
        "ts": "7.0",
    }
    sb._process_incoming(client, event, ctx)
    # not marked processed → eligible for redelivery/retry; no ack sent
    assert ctx["state"]["processed_keys"] == []
    assert not any("Got it!" in s["text"] for s in client.sent)


# ---------------------------------------------------------------------------
# Slack file download bounds
# ---------------------------------------------------------------------------


def test_download_slack_file_ok(patch_slack_paths, mocker):
    sb = patch_slack_paths
    mocker.patch.object(sb.requests, "get", return_value=_FakeResp([b"hello world"]))
    file_obj = {"id": "F1", "name": "note.txt", "url_private_download": "https://x/f"}
    path = sb.download_slack_file("xoxb", file_obj, "C1")
    assert path is not None
    from pathlib import Path

    assert Path(path).read_bytes() == b"hello world"


def test_download_slack_file_rejects_oversized_header(patch_slack_paths, mocker):
    sb = patch_slack_paths
    mocker.patch.object(
        sb.requests,
        "get",
        return_value=_FakeResp(
            [b"x"], headers={"Content-Length": str(sb.MAX_MEDIA_BYTES + 1)}
        ),
    )
    file_obj = {"id": "F2", "name": "big.bin", "url_private_download": "https://x/f"}
    assert sb.download_slack_file("xoxb", file_obj, "C1") is None


def test_download_slack_file_aborts_oversized_stream(
    patch_slack_paths, mocker, monkeypatch
):
    sb = patch_slack_paths
    monkeypatch.setattr(sb, "MAX_MEDIA_BYTES", 10)
    mocker.patch.object(sb.requests, "get", return_value=_FakeResp([b"x" * 50]))
    file_obj = {"id": "F3", "name": "big.bin", "url_private_download": "https://x/f"}
    assert sb.download_slack_file("xoxb", file_obj, "C1") is None
    # partial file is cleaned up
    assert list((sb.MEDIA_DIR / "C1").glob("*")) == []


def test_download_slack_file_no_url(patch_slack_paths):
    assert patch_slack_paths.download_slack_file("xoxb", {"id": "F4"}, "C1") is None


# ---------------------------------------------------------------------------
# Slash-command authorization (register_handlers)
# ---------------------------------------------------------------------------


def test_slash_command_unauthorized_does_not_add_channel(
    patch_slack_paths, fake_keepass
):
    sb = patch_slack_paths
    app = FakeApp()
    ctx = make_ctx(sb)  # no owner, no chat_ids
    sb.register_handlers(app, ctx)

    client = FakeSlackClient()
    app.commands["/status"](
        ack=lambda *a, **k: None,
        command={"channel_id": "C9", "user_id": "U9", "text": ""},
        client=client,
    )
    # rejected, and the channel is NOT silently authorized for outbox broadcast
    assert "C9" not in ctx["chat_ids"]
    assert any("DM me first" in s["text"] for s in client.sent)


def test_slash_command_owner_allowed(patch_slack_paths, fake_keepass, agent_root):
    sb = patch_slack_paths
    (agent_root / "memory" / "state.json").write_text(
        json.dumps({"agent_status": "idle", "cycle_number": 1})
    )
    (agent_root / "memory" / "goal.json").write_text(json.dumps([]))
    app = FakeApp()
    ctx = make_ctx(sb, owner_user_id="OWN1")
    sb.register_handlers(app, ctx)

    client = FakeSlackClient()
    app.commands["/status"](
        ack=lambda *a, **k: None,
        command={"channel_id": "C1", "user_id": "OWN1", "text": ""},
        client=client,
    )
    assert any("Agent Status" in s["text"] for s in client.sent)
    # owner running a command elsewhere must not turn that channel into a target
    assert "C1" not in ctx["chat_ids"]


def test_slash_start_not_registered(patch_slack_paths):
    sb = patch_slack_paths
    app = FakeApp()
    sb.register_handlers(app, make_ctx(sb))
    assert "/start" not in app.commands
    assert "/status" in app.commands
    assert "message" in app.events
    assert "app_mention" in app.events


# ---------------------------------------------------------------------------
# Assistant (Agents & AI Apps) — AI assistant container
# ---------------------------------------------------------------------------


def test_assistant_owner_discovery_and_ack(
    patch_slack_paths, fake_keepass, frozen_now, agent_root
):
    sb = patch_slack_paths
    frozen_now(FROZEN)
    client = FakeSlackClient(names={"U1": "alice"})
    ctx = make_ctx(sb)  # fresh — first assistant DM becomes owner
    say, set_status, set_title = FakeSay(), Recorder(), Recorder()
    payload = {"channel": "D1", "user": "U1", "text": "ship the release", "ts": "1.0"}

    sb._handle_assistant_message(client, payload, ctx, say, set_status, set_title)

    # owner discovered + persisted
    assert ctx["owner_user_id"] == "U1"
    assert "D1" in ctx["chat_ids"]
    assert fake_keepass[sb.KEEPASS_SLACK_OWNER_USERID] == "U1"
    # free text queued as a goal → inbox (source slack)
    inbox = json.loads((agent_root / "messages" / "inbox.json").read_text())
    assert inbox[-1]["source"] == "slack"
    assert "[Slack @alice]: ship the release" in inbox[-1]["content"]
    # thinking status + title set, and an immediate ack posted in-thread
    assert "is thinking…" in set_status.calls
    assert set_title.calls == ["ship the release"]
    assert any("Got it!" in s for s in say.said)


def test_assistant_command_answers_in_thread(
    patch_slack_paths, fake_keepass, agent_root
):
    sb = patch_slack_paths
    (agent_root / "memory" / "state.json").write_text(
        json.dumps({"agent_status": "idle", "cycle_number": 3})
    )
    (agent_root / "memory" / "goal.json").write_text(json.dumps([]))
    client = FakeSlackClient(names={"U1": "alice"})
    ctx = make_ctx(sb, chat_ids=["D1"])
    say, set_status, set_title = FakeSay(), Recorder(), Recorder()
    payload = {"channel": "D1", "user": "U1", "text": "status", "ts": "2.0"}

    sb._handle_assistant_message(client, payload, ctx, say, set_status, set_title)

    # command answered via say() (threaded), nothing queued to the inbox
    assert any("Agent Status" in s for s in say.said)
    assert not (agent_root / "messages" / "inbox.json").exists()


def test_assistant_rejects_unknown_user(patch_slack_paths, fake_keepass, agent_root):
    sb = patch_slack_paths
    client = FakeSlackClient(names={"U9": "stranger"})
    # owner already set; a different user's DM channel is not authorized
    ctx = make_ctx(sb, owner_user_id="OWN1", owner_username="gosu", chat_ids=["D1"])
    say, set_status, set_title = FakeSay(), Recorder(), Recorder()
    payload = {"channel": "D2", "user": "U9", "text": "let me in", "ts": "3.0"}

    sb._handle_assistant_message(client, payload, ctx, say, set_status, set_title)

    assert "D2" not in ctx["chat_ids"]
    assert any("don't know you" in s for s in say.said)
    assert not (agent_root / "messages" / "inbox.json").exists()


def test_assistant_dedup(patch_slack_paths, fake_keepass, agent_root):
    sb = patch_slack_paths
    client = FakeSlackClient(names={"U1": "alice"})
    ctx = make_ctx(sb, chat_ids=["D1"])
    say, set_status, set_title = FakeSay(), Recorder(), Recorder()
    payload = {"channel": "D1", "user": "U1", "text": "ship it", "ts": "4.0"}

    sb._handle_assistant_message(client, payload, ctx, say, set_status, set_title)
    sb._handle_assistant_message(client, payload, ctx, say, set_status, set_title)

    inbox = json.loads((agent_root / "messages" / "inbox.json").read_text())
    assert len(inbox) == 1


def test_assistant_write_failure_not_marked(patch_slack_paths, fake_keepass, mocker):
    sb = patch_slack_paths
    mocker.patch.object(sb, "write_to_inbox", return_value=False)
    client = FakeSlackClient(names={"U1": "alice"})
    ctx = make_ctx(sb, chat_ids=["D1"])
    say, set_status, set_title = FakeSay(), Recorder(), Recorder()
    payload = {"channel": "D1", "user": "U1", "text": "do the thing", "ts": "6.0"}

    sb._handle_assistant_message(client, payload, ctx, say, set_status, set_title)

    # not marked → eligible for redelivery; warned, and NOT acked as queued
    assert ctx["state"]["processed_keys"] == []
    assert any("couldn't queue" in s for s in say.said)
    assert not any("Got it!" in s for s in say.said)


def test_assistant_empty_input_after_mention_strip(
    patch_slack_paths, fake_keepass, agent_root
):
    sb = patch_slack_paths
    client = FakeSlackClient(names={"U1": "alice"})
    ctx = make_ctx(sb, chat_ids=["D1"])
    say, set_status, set_title = FakeSay(), Recorder(), Recorder()
    # text is only a bot mention → strips to empty, no files
    payload = {"channel": "D1", "user": "U1", "text": "<@BOT1>", "ts": "8.0"}

    sb._handle_assistant_message(client, payload, ctx, say, set_status, set_title)

    assert any("didn't catch" in s for s in say.said)
    assert not (agent_root / "messages" / "inbox.json").exists()
    # marked processed (nothing to retry)
    assert ctx["state"]["processed_keys"] == ["D1:8.0"]


def test_assistant_welcome_on_owner_reference(patch_slack_paths, fake_keepass):
    sb = patch_slack_paths
    client = FakeSlackClient(names={"U2": "bob"})
    # owner known; a new DM channel that references the owner → authorized + welcomed
    ctx = make_ctx(sb, owner_user_id="OWN1", owner_username="gosu", chat_ids=["D1"])
    say, set_status, set_title = FakeSay(), Recorder(), Recorder()
    payload = {"channel": "D2", "user": "U2", "text": "help me <@OWN1>", "ts": "9.5"}

    sb._handle_assistant_message(client, payload, ctx, say, set_status, set_title)

    # new channel authorized (now an outbox broadcast target) + welcome sent
    assert "D2" in ctx["chat_ids"]
    assert any("Welcome!" in s for s in say.said)


def test_build_assistant_wires_handlers(patch_slack_paths, monkeypatch):
    sb = patch_slack_paths

    class FakeAssistant:
        def __init__(self):
            self.started = None
            self.user_msg = None

        def thread_started(self, fn):
            self.started = fn
            return fn

        def user_message(self, fn):
            self.user_msg = fn
            return fn

    fake_mod = types.ModuleType("slack_bolt")
    fake_mod.Assistant = FakeAssistant
    monkeypatch.setitem(sys.modules, "slack_bolt", fake_mod)

    assistant = sb.build_assistant(make_ctx(sb))
    assert assistant.started is not None
    assert assistant.user_msg is not None

    # thread_started greets and publishes the suggested prompts
    say = FakeSay()
    suggest = SuggestRecorder()
    assistant.started(say=say, set_suggested_prompts=suggest)
    assert say.said and "agent" in say.said[0].lower()
    assert suggest.prompts == sb.ASSISTANT_SUGGESTED_PROMPTS
