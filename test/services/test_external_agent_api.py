"""Tests for services/external_agent_api.py — pure helpers + sweeper."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed_agents(eaa, agents: list) -> None:
    eaa.AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    eaa.AGENTS_FILE.write_text(json.dumps(agents))


# ---------------------------------------------------------------------------
# _is_offline
# ---------------------------------------------------------------------------


def test_is_offline_when_last_ping_missing(patch_eaa_paths):
    assert patch_eaa_paths._is_offline({}) is True


def test_is_offline_when_last_ping_recent(patch_eaa_paths):
    now = datetime.now(timezone.utc)
    agent = {"last_ping_at": now.isoformat(), "timeout_seconds": 60}
    assert patch_eaa_paths._is_offline(agent, now=now) is False


def test_is_offline_when_last_ping_stale(patch_eaa_paths):
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(seconds=400)).isoformat()
    agent = {"last_ping_at": stale, "timeout_seconds": 60}
    assert patch_eaa_paths._is_offline(agent, now=now) is True


def test_is_offline_treats_malformed_timestamp_as_offline(patch_eaa_paths):
    agent = {"last_ping_at": "not a date"}
    assert patch_eaa_paths._is_offline(agent) is True


def test_is_offline_uses_default_timeout(patch_eaa_paths):
    now = datetime.now(timezone.utc)
    edge = (now - timedelta(seconds=patch_eaa_paths.DEFAULT_TIMEOUT_SECONDS - 1)).isoformat()
    agent = {"last_ping_at": edge}
    assert patch_eaa_paths._is_offline(agent, now=now) is False


# ---------------------------------------------------------------------------
# _validate_update
# ---------------------------------------------------------------------------


def test_validate_update_rejects_unknown_field(patch_eaa_paths):
    with pytest.raises(ValueError, match="unsupported field"):
        patch_eaa_paths._validate_update({"name": "x"})


def test_validate_update_rejects_invalid_status(patch_eaa_paths):
    with pytest.raises(ValueError, match="invalid status"):
        patch_eaa_paths._validate_update({"status": "asleep"})


def test_validate_update_rejects_non_string_responsibilities(patch_eaa_paths):
    with pytest.raises(ValueError, match="responsibilities"):
        patch_eaa_paths._validate_update({"responsibilities": 42})


def test_validate_update_rejects_non_list_capabilities(patch_eaa_paths):
    with pytest.raises(ValueError, match="capabilities"):
        patch_eaa_paths._validate_update({"capabilities": "all of them"})


def test_validate_update_rejects_capabilities_with_non_dict(patch_eaa_paths):
    with pytest.raises(ValueError, match="capabilities"):
        patch_eaa_paths._validate_update({"capabilities": [{"a": 1}, "bad"]})


def test_validate_update_returns_clean_dict(patch_eaa_paths):
    cleaned = patch_eaa_paths._validate_update(
        {
            "status": "online",
            "responsibilities": "all",
            "capabilities": [{"id": "x"}],
        }
    )
    assert cleaned == {
        "status": "online",
        "responsibilities": "all",
        "capabilities": [{"id": "x"}],
    }


# ---------------------------------------------------------------------------
# _apply_update
# ---------------------------------------------------------------------------


def test_apply_update_writes_patch(patch_eaa_paths):
    _seed_agents(
        patch_eaa_paths,
        [{"name": "alpha", "status": "online", "type": "external"}],
    )
    out = patch_eaa_paths._apply_update("alpha", {"status": "offline"})
    assert out["status"] == "offline"

    on_disk = json.loads(patch_eaa_paths.AGENTS_FILE.read_text())
    assert on_disk[0]["status"] == "offline"


def test_apply_update_sets_deactivated_at(patch_eaa_paths):
    _seed_agents(
        patch_eaa_paths,
        [{"name": "alpha", "status": "online", "type": "external"}],
    )
    out = patch_eaa_paths._apply_update("alpha", {"status": "deactivated"})
    assert out["status"] == "deactivated"
    assert "deactivated_at" in out
    datetime.fromisoformat(out["deactivated_at"])


def test_apply_update_does_not_set_deactivated_at_for_other_status(patch_eaa_paths):
    _seed_agents(
        patch_eaa_paths,
        [{"name": "alpha", "status": "online", "type": "external"}],
    )
    out = patch_eaa_paths._apply_update("alpha", {"status": "offline"})
    assert "deactivated_at" not in out


def test_apply_update_unknown_agent_raises(patch_eaa_paths):
    _seed_agents(patch_eaa_paths, [])
    with pytest.raises(ValueError, match="unknown agent"):
        patch_eaa_paths._apply_update("ghost", {"status": "online"})


def test_apply_update_no_fields_raises(patch_eaa_paths):
    _seed_agents(patch_eaa_paths, [{"name": "alpha", "type": "external"}])
    with pytest.raises(ValueError, match="no fields to update"):
        patch_eaa_paths._apply_update("alpha", {})


# ---------------------------------------------------------------------------
# _save_upload
# ---------------------------------------------------------------------------


def test_save_upload_writes_with_timestamp_prefix(patch_eaa_paths, frozen_now):
    frozen_now(datetime(2026, 4, 30, 13, 14, 15, tzinfo=timezone.utc))
    p = patch_eaa_paths._save_upload("alpha", "report.pdf", b"PDFDATA")
    assert p.exists()
    assert p.name == "20260430_131415_report.pdf"
    assert p.read_bytes() == b"PDFDATA"


def test_save_upload_collision_appends_suffix(patch_eaa_paths, frozen_now):
    frozen_now(datetime(2026, 4, 30, 13, 14, 15, tzinfo=timezone.utc))
    p1 = patch_eaa_paths._save_upload("alpha", "x.txt", b"A")
    p2 = patch_eaa_paths._save_upload("alpha", "x.txt", b"B")
    p3 = patch_eaa_paths._save_upload("alpha", "x.txt", b"C")
    assert p1.name == "20260430_131415_x.txt"
    assert p2.name == "20260430_131415_x-2.txt"
    assert p3.name == "20260430_131415_x-3.txt"
    assert {p1.read_bytes(), p2.read_bytes(), p3.read_bytes()} == {b"A", b"B", b"C"}


@pytest.mark.parametrize(
    "bad",
    [
        "",
        ".",
        "..",
        "../etc/passwd",
        "sub/dir/file.txt",
        "back\\slash.txt",
        ".hidden",
        "name with space.txt",
        "naïve.txt",
    ],
)
def test_save_upload_rejects_unsafe_filenames(patch_eaa_paths, bad):
    with pytest.raises(ValueError):
        patch_eaa_paths._save_upload("alpha", bad, b"x")


# ---------------------------------------------------------------------------
# _append_outbox
# ---------------------------------------------------------------------------


def test_append_outbox_accepts_dict_payload(patch_eaa_paths):
    n = patch_eaa_paths._append_outbox(
        "alpha",
        {"type": "info", "subject": "s", "content": "c"},
    )
    assert n == 1
    items = json.loads(patch_eaa_paths._agent_outbox("alpha").read_text())
    assert len(items) == 1
    assert items[0]["type"] == "info"
    assert items[0]["id"]
    datetime.fromisoformat(items[0]["timestamp"])


def test_append_outbox_accepts_list_payload(patch_eaa_paths):
    n = patch_eaa_paths._append_outbox(
        "alpha",
        [
            {"type": "info", "subject": "s1", "content": "c1"},
            {"type": "needs_human", "subject": "s2", "content": "c2"},
        ],
    )
    assert n == 2


def test_append_outbox_rejects_bad_type(patch_eaa_paths):
    with pytest.raises(ValueError, match="'type'"):
        patch_eaa_paths._append_outbox(
            "alpha", {"type": "spam", "subject": "s", "content": "c"}
        )


def test_append_outbox_rejects_empty_subject(patch_eaa_paths):
    with pytest.raises(ValueError, match="subject"):
        patch_eaa_paths._append_outbox(
            "alpha", {"type": "info", "subject": "  ", "content": "c"}
        )


def test_append_outbox_rejects_empty_content(patch_eaa_paths):
    with pytest.raises(ValueError, match="content"):
        patch_eaa_paths._append_outbox(
            "alpha", {"type": "info", "subject": "s", "content": ""}
        )


def test_append_outbox_rejects_empty_list(patch_eaa_paths):
    with pytest.raises(ValueError):
        patch_eaa_paths._append_outbox("alpha", [])


def test_append_outbox_preserves_caller_supplied_id_and_timestamp(patch_eaa_paths):
    patch_eaa_paths._append_outbox(
        "alpha",
        {
            "id": "fixed-id-123",
            "type": "info",
            "subject": "s",
            "content": "c",
            "timestamp": "2024-01-01T00:00:00+00:00",
        },
    )
    item = json.loads(patch_eaa_paths._agent_outbox("alpha").read_text())[0]
    assert item["id"] == "fixed-id-123"
    assert item["timestamp"] == "2024-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# _read_inbox_by_ids
# ---------------------------------------------------------------------------


def _seed_inbox(eaa, name: str, messages: list) -> None:
    inbox = eaa._agent_inbox(name)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(json.dumps(messages))


def test_read_inbox_by_ids_flips_read_and_returns_in_order(patch_eaa_paths):
    _seed_inbox(
        patch_eaa_paths,
        "alpha",
        [
            {"id": "m1", "content": "first", "read": False},
            {"id": "m2", "content": "second", "read": False},
            {"id": "m3", "content": "third", "read": False},
        ],
    )
    out = patch_eaa_paths._read_inbox_by_ids("alpha", ["m3", "m1"])
    assert [m["content"] for m in out] == ["third", "first"]

    on_disk = json.loads(patch_eaa_paths._agent_inbox("alpha").read_text())
    by_id = {m["id"]: m for m in on_disk}
    assert by_id["m1"]["read"] is True and "read_at" in by_id["m1"]
    assert by_id["m2"]["read"] is False
    assert by_id["m3"]["read"] is True and "read_at" in by_id["m3"]


def test_read_inbox_by_ids_aborts_on_unknown(patch_eaa_paths):
    _seed_inbox(
        patch_eaa_paths,
        "alpha",
        [{"id": "m1", "content": "first", "read": False}],
    )
    with pytest.raises(patch_eaa_paths._ReadInboxError) as exc:
        patch_eaa_paths._read_inbox_by_ids("alpha", ["m1", "ghost"])
    assert exc.value.unknown_ids == ["ghost"]
    # Mutation must NOT happen on abort.
    on_disk = json.loads(patch_eaa_paths._agent_inbox("alpha").read_text())
    assert on_disk[0]["read"] is False


def test_read_inbox_by_ids_aborts_on_already_read(patch_eaa_paths):
    _seed_inbox(
        patch_eaa_paths,
        "alpha",
        [{"id": "m1", "content": "first", "read": True}],
    )
    with pytest.raises(patch_eaa_paths._ReadInboxError) as exc:
        patch_eaa_paths._read_inbox_by_ids("alpha", ["m1"])
    assert exc.value.already_read_ids == ["m1"]


@pytest.mark.parametrize(
    "ids, match",
    [
        ([], "non-empty"),
        ([""], "non-empty strings"),
        (["a", "a"], "duplicates"),
        ("not a list", "non-empty"),
    ],
)
def test_read_inbox_by_ids_rejects_invalid_input(patch_eaa_paths, ids, match):
    with pytest.raises(patch_eaa_paths._ReadInboxError, match=match):
        patch_eaa_paths._read_inbox_by_ids("alpha", ids)


# ---------------------------------------------------------------------------
# _unread_ids
# ---------------------------------------------------------------------------


def test_unread_ids_filters_out_read_and_id_less(patch_eaa_paths):
    _seed_inbox(
        patch_eaa_paths,
        "alpha",
        [
            {"id": "m1", "read": False},
            {"id": "m2", "read": True},
            {"read": False},  # missing id
            {"id": "m3", "read": False},
        ],
    )
    assert patch_eaa_paths._unread_ids("alpha") == ["m1", "m3"]


def test_unread_ids_returns_empty_for_missing_inbox(patch_eaa_paths):
    assert patch_eaa_paths._unread_ids("nobody") == []


# ---------------------------------------------------------------------------
# _archive_old_read_inbox
# ---------------------------------------------------------------------------


def test_archive_old_read_inbox_moves_old_read_entries(patch_eaa_paths):
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    archive_after = patch_eaa_paths.INBOX_ARCHIVE_AFTER_SECONDS
    old = (now - timedelta(seconds=archive_after + 60)).isoformat()
    fresh = (now - timedelta(seconds=archive_after - 60)).isoformat()

    _seed_inbox(
        patch_eaa_paths,
        "alpha",
        [
            {"id": "old", "read": True, "read_at": old},
            {"id": "fresh", "read": True, "read_at": fresh},
            {"id": "unread", "read": False},
        ],
    )
    n = patch_eaa_paths._archive_old_read_inbox("alpha", now=now)
    assert n == 1

    live = json.loads(patch_eaa_paths._agent_inbox("alpha").read_text())
    assert sorted(m["id"] for m in live) == ["fresh", "unread"]

    history = json.loads(patch_eaa_paths._agent_inbox_history("alpha").read_text())
    assert [m["id"] for m in history] == ["old"]


def test_archive_old_read_inbox_returns_zero_for_missing(patch_eaa_paths):
    n = patch_eaa_paths._archive_old_read_inbox(
        "nobody", now=datetime.now(timezone.utc)
    )
    assert n == 0


# ---------------------------------------------------------------------------
# _sweep_once
# ---------------------------------------------------------------------------


def test_sweep_forwards_outbox_with_priority_and_archives(
    patch_eaa_paths, frozen_now
):
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    frozen_now(now)

    _seed_agents(
        patch_eaa_paths,
        [
            {
                "name": "alpha",
                "type": "external",
                "status": "online",
                "last_ping_at": now.isoformat(),
            },
        ],
    )
    # Seed alpha's outbox with one of each forwardable type.
    patch_eaa_paths._append_outbox(
        "alpha",
        [
            {"type": "needs_human", "subject": "halp", "content": "stuck"},
            {"type": "response", "subject": "done", "content": "ok"},
        ],
    )

    patch_eaa_paths._sweep_once()

    # Main inbox now has two forwarded items (agent_*).
    main_inbox = json.loads(
        (patch_eaa_paths.BASE / "messages" / "inbox.json").read_text()
    )
    types = sorted(m["type"] for m in main_inbox)
    assert types == ["agent_needs_human", "agent_response"]
    by_type = {m["type"]: m for m in main_inbox}
    assert by_type["agent_needs_human"]["priority"] == 2
    assert by_type["agent_response"]["priority"] == 4
    assert all(m["source"] == "external_agent" for m in main_inbox)

    # needs_human mirrored to main outbox.
    main_outbox = json.loads(
        (patch_eaa_paths.BASE / "messages" / "outbox.json").read_text()
    )
    assert [m["type"] for m in main_outbox] == ["needs_human"]

    # Per-agent outbox cleared, history populated.
    assert json.loads(patch_eaa_paths._agent_outbox("alpha").read_text()) == []
    history = json.loads(patch_eaa_paths._agent_outbox_history("alpha").read_text())
    assert {h["type"] for h in history} == {"needs_human", "response"}
    assert all("forwarded_at" in h for h in history)


def test_sweep_skips_deactivated_agents_for_outbox_forwarding(
    patch_eaa_paths, frozen_now
):
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    frozen_now(now)

    _seed_agents(
        patch_eaa_paths,
        [
            {
                "name": "alpha",
                "type": "external",
                "status": "deactivated",
                "last_ping_at": now.isoformat(),
            },
        ],
    )
    patch_eaa_paths._append_outbox(
        "alpha", {"type": "info", "subject": "s", "content": "c"}
    )

    patch_eaa_paths._sweep_once()

    # Outbox still has the entry; main inbox not touched.
    items = json.loads(patch_eaa_paths._agent_outbox("alpha").read_text())
    assert len(items) == 1
    main_inbox = patch_eaa_paths.BASE / "messages" / "inbox.json"
    assert not main_inbox.exists() or json.loads(main_inbox.read_text()) == []


def test_sweep_flips_stale_agents_to_offline(patch_eaa_paths, frozen_now):
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    frozen_now(now)

    stale = (now - timedelta(seconds=999)).isoformat()
    _seed_agents(
        patch_eaa_paths,
        [
            {
                "name": "alpha",
                "type": "external",
                "status": "online",
                "last_ping_at": stale,
                "timeout_seconds": 60,
            }
        ],
    )

    patch_eaa_paths._sweep_once()

    on_disk = json.loads(patch_eaa_paths.AGENTS_FILE.read_text())
    assert on_disk[0]["status"] == "offline"
