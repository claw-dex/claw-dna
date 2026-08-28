"""Tests for scripts/callmebot.py."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

import callmebot


@pytest.fixture
def redirect_state(monkeypatch, tmp_path):
    state_file = tmp_path / "callmebot_state.json"
    monkeypatch.setattr(callmebot, "STATE_FILE", state_file)
    return state_file


# ─── Helpers ────────────────────────────────────────────────────────────────


def test_normalize_username_adds_at():
    assert callmebot._normalize_username("user") == "@user"
    assert callmebot._normalize_username("@user") == "@user"
    assert callmebot._normalize_username("") == ""


def test_now_iso_returns_iso():
    s = callmebot._now_iso()
    # parses round-trip
    datetime.fromisoformat(s)


def test_load_state_missing(redirect_state):
    state = callmebot._load_state()
    assert state == {"last_call_timestamp": None, "total_calls": 0}


def test_load_state_corrupt(redirect_state):
    redirect_state.write_text("not json")
    state = callmebot._load_state()
    assert state == {"last_call_timestamp": None, "total_calls": 0}


def test_save_and_load_state(redirect_state):
    callmebot._save_state({"last_call_timestamp": "x", "total_calls": 5})
    loaded = callmebot._load_state()
    assert loaded == {"last_call_timestamp": "x", "total_calls": 5}


def test_cooldown_remaining_no_ts():
    assert callmebot._cooldown_remaining({}) == 0


def test_cooldown_remaining_recent():
    now = datetime.now(timezone.utc)
    state = {"last_call_timestamp": now.isoformat()}
    rem = callmebot._cooldown_remaining(state)
    # within cooldown window
    assert 0 < rem <= callmebot.COOLDOWN_SECONDS


def test_cooldown_remaining_expired():
    long_ago = datetime.now(timezone.utc) - timedelta(
        seconds=callmebot.COOLDOWN_SECONDS + 60
    )
    state = {"last_call_timestamp": long_ago.isoformat()}
    assert callmebot._cooldown_remaining(state) == 0


def test_cooldown_remaining_invalid_ts():
    assert callmebot._cooldown_remaining({"last_call_timestamp": "nope"}) == 0


def test_check_api_error_unauthorized():
    msg = callmebot._check_api_error(
        "Authorization for user @bob is not received.", "@bob"
    )
    assert msg is not None and "not authorized" in msg


def test_check_api_error_unauthorized_exact():
    msg = callmebot._check_api_error("Warning! User not authorized.", "@bob")
    assert msg is not None and "not authorized" in msg


def test_check_api_error_format_error():
    msg = callmebot._check_api_error("ERROR: User foo has wrong format", "foo")
    assert msg is not None and "format error" in msg


def test_check_api_error_other_error():
    msg = callmebot._check_api_error("ERROR: Something broke. continued", "@bob")
    assert msg is not None and "Something broke" in msg


def test_check_api_error_clean():
    assert callmebot._check_api_error("OK done", "@bob") is None


# ─── Subcommands ────────────────────────────────────────────────────────────


def test_cmd_status_unconfigured(monkeypatch, redirect_state, capsys):
    monkeypatch.setattr(callmebot, "keepass_get", lambda title: None)
    args = argparse.Namespace(json=False)
    rc = callmebot.cmd_status(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "not configured" in out


def test_cmd_status_configured_json(monkeypatch, redirect_state, capsys):
    answers = {
        "TELEGRAM_OWNER_USERNAME": "alice",
        "CALLMEBOT_LANG": "en-US-Standard-B",
    }
    monkeypatch.setattr(callmebot, "keepass_get", lambda t: answers.get(t))
    args = argparse.Namespace(json=True)
    rc = callmebot.cmd_status(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["configured"] is True
    assert data["user"] == "@alice"
    assert data["lang"] == "en-US-Standard-B"
    assert data["can_call_now"] is True


def test_cmd_call_no_user(monkeypatch, redirect_state, capsys):
    monkeypatch.setattr(callmebot, "keepass_get", lambda t: None)
    args = argparse.Namespace(text="hi", json=False)
    rc = callmebot.cmd_call(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "TELEGRAM_OWNER_USERNAME" in err


def test_cmd_call_rate_limited(monkeypatch, redirect_state, capsys):
    monkeypatch.setattr(
        callmebot,
        "keepass_get",
        lambda t: "alice" if t == "TELEGRAM_OWNER_USERNAME" else "en-US",
    )
    callmebot._save_state(
        {
            "last_call_timestamp": datetime.now(timezone.utc).isoformat(),
            "total_calls": 1,
        }
    )
    args = argparse.Namespace(text="hi", json=True)
    rc = callmebot.cmd_call(args)
    assert rc == 2
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "rate_limited"


def test_cmd_call_success(monkeypatch, redirect_state, capsys):
    monkeypatch.setattr(
        callmebot,
        "keepass_get",
        lambda t: "alice" if t == "TELEGRAM_OWNER_USERNAME" else "en-US",
    )

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"OK"

    monkeypatch.setattr(
        callmebot.urllib.request, "urlopen", lambda req, timeout=10: FakeResp()
    )
    args = argparse.Namespace(text="hello", json=True)
    rc = callmebot.cmd_call(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "called"
    assert data["user"] == "@alice"
    state = callmebot._load_state()
    assert state["total_calls"] == 1


def test_cmd_call_unauthorized_response(monkeypatch, redirect_state, capsys):
    monkeypatch.setattr(
        callmebot,
        "keepass_get",
        lambda t: "alice" if t == "TELEGRAM_OWNER_USERNAME" else "en-US",
    )

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"Warning! User not authorized."

    monkeypatch.setattr(
        callmebot.urllib.request, "urlopen", lambda req, timeout=10: FakeResp()
    )
    args = argparse.Namespace(text="hello", json=True)
    rc = callmebot.cmd_call(args)
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "unauthorized"


def test_cmd_setup_no_user(monkeypatch, redirect_state, capsys):
    monkeypatch.setattr(callmebot, "keepass_get", lambda t: None)
    args = argparse.Namespace(lang=None, json=False)
    rc = callmebot.cmd_setup(args)
    assert rc == 1
    assert "TELEGRAM_OWNER_USERNAME" in capsys.readouterr().err


def test_cmd_setup_success(monkeypatch, redirect_state, capsys):
    monkeypatch.setattr(callmebot, "keepass_get", lambda t: "alice")
    monkeypatch.setattr(callmebot, "keepass_store", lambda *a, **k: True)

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"OK"

    monkeypatch.setattr(
        callmebot.urllib.request, "urlopen", lambda req, timeout=10: FakeResp()
    )
    args = argparse.Namespace(lang=None, json=True)
    rc = callmebot.cmd_setup(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "configured"
