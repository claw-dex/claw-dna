"""Unit tests for app.data._helpers."""

from __future__ import annotations

import json
import os

import pytest

from app.data import _helpers

# ---------- _read_json_safe ----------


def test_read_json_safe_returns_parsed_dict(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"a": 1, "b": [2, 3]}))
    assert _helpers._read_json_safe(str(p)) == {"a": 1, "b": [2, 3]}


def test_read_json_safe_returns_default_on_missing(tmp_path):
    missing = tmp_path / "nope.json"
    assert _helpers._read_json_safe(str(missing), default=[]) == []
    assert _helpers._read_json_safe(str(missing)) is None


def test_read_json_safe_returns_default_on_malformed(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json}")
    assert _helpers._read_json_safe(str(p), default={"fallback": True}) == {
        "fallback": True
    }


def test_read_json_safe_returns_default_on_empty_file(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text("")
    assert _helpers._read_json_safe(str(p), default=42) == 42


def test_read_json_safe_handles_oserror(tmp_path, mocker):
    mocker.patch("builtins.open", side_effect=OSError("boom"))
    assert _helpers._read_json_safe("/whatever", default="fb") == "fb"


# ---------- _read_text_safe ----------


def test_read_text_safe_returns_text(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("hello\nworld")
    assert _helpers._read_text_safe(str(p)) == "hello\nworld"


def test_read_text_safe_missing_returns_none(tmp_path):
    assert _helpers._read_text_safe(str(tmp_path / "nope.txt")) is None


def test_read_text_safe_oserror_returns_none(mocker):
    mocker.patch("builtins.open", side_effect=OSError("perm"))
    assert _helpers._read_text_safe("/anything") is None


# ---------- _pid_alive ----------


def test_pid_alive_zero_returns_false():
    assert _helpers._pid_alive(0) is False
    assert _helpers._pid_alive(None) is False


def test_pid_alive_true_when_kill_succeeds(mocker):
    mocker.patch("os.kill", return_value=None)
    assert _helpers._pid_alive(12345) is True


def test_pid_alive_false_on_oserror(mocker):
    mocker.patch("os.kill", side_effect=OSError("no such process"))
    assert _helpers._pid_alive(99999) is False


def test_pid_alive_false_on_process_lookup_error(mocker):
    mocker.patch("os.kill", side_effect=ProcessLookupError())
    assert _helpers._pid_alive(99999) is False


def test_pid_alive_self_pid_is_true():
    # Sending signal 0 to ourselves always works.
    assert _helpers._pid_alive(os.getpid()) is True
