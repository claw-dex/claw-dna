"""Tests for scripts/get_current_date_time.py."""

from __future__ import annotations

import json
import sys

import pytest

import get_current_date_time as gcdt


@pytest.fixture
def patched_cfg(monkeypatch, tmp_path):
    cfg = tmp_path / "portal_config.json"
    # Patch the local Path constructor reference inside get_user_timezone via
    # monkeypatching pathlib.Path? Easier: just monkeypatch the function itself
    # by re-pointing the path it reads. The function hardcodes the path, so we
    # patch the function instead.
    return cfg


def test_get_user_timezone_default_when_missing(monkeypatch, tmp_path):
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(gcdt.pathlib, "Path", lambda *a, **k: missing)
    assert gcdt.get_user_timezone() == "UTC"


def test_get_user_timezone_reads_value(monkeypatch, tmp_path):
    cfg = tmp_path / "portal_config.json"
    cfg.write_text(json.dumps({"timezone": "America/New_York"}))
    monkeypatch.setattr(gcdt.pathlib, "Path", lambda *a, **k: cfg)
    assert gcdt.get_user_timezone() == "America/New_York"


def test_get_user_timezone_default_for_missing_key(monkeypatch, tmp_path):
    cfg = tmp_path / "portal_config.json"
    cfg.write_text(json.dumps({"other": "x"}))
    monkeypatch.setattr(gcdt.pathlib, "Path", lambda *a, **k: cfg)
    assert gcdt.get_user_timezone() == "UTC"


def test_get_user_timezone_invalid_json(monkeypatch, tmp_path):
    cfg = tmp_path / "portal_config.json"
    cfg.write_text("{not json")
    monkeypatch.setattr(gcdt.pathlib, "Path", lambda *a, **k: cfg)
    assert gcdt.get_user_timezone() == "UTC"


def test_main_text_output(monkeypatch, capsys):
    monkeypatch.setattr(gcdt, "get_user_timezone", lambda: "UTC")
    monkeypatch.setattr(sys, "argv", ["get_current_date_time.py"])
    gcdt.main()
    out = capsys.readouterr().out
    assert "Timezone : UTC" in out
    assert "Date" in out
    assert "Time" in out


def test_main_json_output(monkeypatch, capsys):
    monkeypatch.setattr(gcdt, "get_user_timezone", lambda: "UTC")
    monkeypatch.setattr(sys, "argv", ["get_current_date_time.py", "--json"])
    gcdt.main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["timezone"] == "UTC"
    assert "datetime" in data
    assert "date" in data
    assert "weekday" in data
