"""Tests for scripts/portal_config.py."""

from __future__ import annotations

import argparse
import json
import sys

import pytest

import portal_config as pc


@pytest.fixture
def redirect_paths(monkeypatch, tmp_path):
    cfg = tmp_path / "portal_config.json"
    monkeypatch.setattr(pc, "MEMORY", tmp_path)
    monkeypatch.setattr(pc, "CONFIG_PATH", cfg)
    return cfg


# ─── Helpers ────────────────────────────────────────────────────────────────


def test_read_json_safe_missing(tmp_path):
    p = tmp_path / "nope.json"
    assert pc._read_json_safe(p, {"a": 1}) == {"a": 1}


def test_read_json_safe_corrupt(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json")
    assert pc._read_json_safe(p, {"d": 1}) == {"d": 1}


def test_read_json_safe_ok(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"k": "v"}))
    assert pc._read_json_safe(p, {}) == {"k": "v"}


def test_write_json_atomic_creates(tmp_path):
    p = tmp_path / "sub" / "f.json"
    pc._write_json_atomic(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}


def test_save_portal_config_preserves(redirect_paths):
    pc._save_portal_config("a", "1")
    pc._save_portal_config("b", "2")
    data = json.loads(redirect_paths.read_text())
    assert data == {"a": "1", "b": "2"}


def test_detect_legacy_mode(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["portal-hostname.py"])
    assert pc._detect_legacy_mode() == "hostname"
    monkeypatch.setattr(sys, "argv", ["portal-auth.py"])
    assert pc._detect_legacy_mode() == "auth"
    monkeypatch.setattr(sys, "argv", ["portal_config.py"])
    assert pc._detect_legacy_mode() is None


# ─── Hostname ───────────────────────────────────────────────────────────────


def test_set_hostname_invalid_url(redirect_paths, capsys):
    with pytest.raises(SystemExit):
        pc._set_hostname("not-a-url")


def test_set_hostname_with_whitespace(redirect_paths, capsys):
    with pytest.raises(SystemExit):
        pc._set_hostname("https://exam ple.com")


def test_set_hostname_strips_trailing_slash(redirect_paths, capsys):
    pc._set_hostname("https://example.com/")
    data = json.loads(redirect_paths.read_text())
    assert data["public_url"] == "https://example.com"


def test_show_hostname_unset(redirect_paths, capsys):
    pc._show_hostname()
    out = capsys.readouterr().out
    assert "not configured" in out


def test_show_hostname_set(redirect_paths, capsys):
    pc._save_portal_config("public_url", "https://x")
    pc._show_hostname()
    assert "https://x" in capsys.readouterr().out


def test_clear_hostname_no_file(redirect_paths, capsys):
    pc._clear_hostname()
    assert "Nothing to clear" in capsys.readouterr().out


def test_clear_hostname_removes_key(redirect_paths, capsys):
    pc._save_portal_config("public_url", "https://x")
    pc._save_portal_config("timezone", "UTC")
    pc._clear_hostname()
    data = json.loads(redirect_paths.read_text())
    assert "public_url" not in data
    assert data["timezone"] == "UTC"


def test_cmd_hostname_dispatch_show(redirect_paths, capsys):
    pc.cmd_hostname(argparse.Namespace(set=None, show=True, clear=False))
    assert "not configured" in capsys.readouterr().out


def test_cmd_hostname_dispatch_set(redirect_paths, capsys):
    pc.cmd_hostname(
        argparse.Namespace(set="https://example.com", show=False, clear=False)
    )
    assert "Public hostname set" in capsys.readouterr().out


# ─── Timezone ───────────────────────────────────────────────────────────────


def test_set_timezone_invalid(redirect_paths, capsys):
    with pytest.raises(SystemExit):
        pc._set_timezone("Mars/Phobos")


def test_set_timezone_valid(redirect_paths, capsys):
    pc._set_timezone("America/New_York")
    data = json.loads(redirect_paths.read_text())
    assert data["timezone"] == "America/New_York"


def test_show_timezone_set(redirect_paths, capsys):
    pc._save_portal_config("timezone", "UTC")
    pc._show_timezone()
    assert "UTC" in capsys.readouterr().out


def test_show_timezone_unset(redirect_paths, capsys):
    pc._show_timezone()
    assert "not configured" in capsys.readouterr().out


def test_clear_timezone_removes_key(redirect_paths, capsys):
    pc._save_portal_config("timezone", "UTC")
    pc._save_portal_config("public_url", "https://x")
    pc._clear_timezone()
    data = json.loads(redirect_paths.read_text())
    assert "timezone" not in data
    assert data["public_url"] == "https://x"


# ─── Auth helpers ───────────────────────────────────────────────────────────


def test_build_auth_route_shape():
    route = pc._build_auth_route("user", "$2b$hash")
    assert route["@id"] == "portal_auth"
    accounts = route["handle"][0]["providers"]["http_basic"]["accounts"]
    assert accounts[0]["username"] == "user"
    assert accounts[0]["password"] == "$2b$hash"


def test_caddy_api_url_error(monkeypatch):
    import urllib.error

    def fail(req, *a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(pc.urllib.request, "urlopen", fail)
    status, body = pc._caddy_api("GET", "/x")
    assert status == 0
    assert "refused" in body


def test_caddy_api_http_error(monkeypatch):
    import urllib.error
    import io

    def fail(req, *a, **k):
        raise urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO(b"server failed"))

    monkeypatch.setattr(pc.urllib.request, "urlopen", fail)
    status, body = pc._caddy_api("GET", "/x")
    assert status == 500
    assert "server failed" in body


def test_get_server_name_finds(monkeypatch):
    payload = json.dumps({"srv1": {"listen": [":8080"]}, "srv2": {"listen": [":443"]}})
    monkeypatch.setattr(pc, "_caddy_api", lambda m, p: (200, payload))
    assert pc._get_server_name() == "srv1"


def test_get_server_name_none(monkeypatch):
    monkeypatch.setattr(pc, "_caddy_api", lambda m, p: (404, ""))
    assert pc._get_server_name() is None


def test_auth_exists_true(monkeypatch):
    monkeypatch.setattr(pc, "_caddy_api", lambda m, p: (200, ""))
    assert pc._auth_exists() is True


def test_auth_exists_false(monkeypatch):
    monkeypatch.setattr(pc, "_caddy_api", lambda m, p: (404, ""))
    assert pc._auth_exists() is False


def test_enable_auth_bad_format(redirect_paths, capsys):
    with pytest.raises(SystemExit):
        pc._enable_auth("nocolon")


def test_enable_auth_blank_parts(redirect_paths, capsys):
    with pytest.raises(SystemExit):
        pc._enable_auth(":pw")
    with pytest.raises(SystemExit):
        pc._enable_auth("user:")


def test_enable_auth_already_enabled(redirect_paths, monkeypatch, capsys):
    monkeypatch.setattr(pc, "_auth_exists", lambda: True)
    with pytest.raises(SystemExit) as exc:
        pc._enable_auth("u:p")
    assert exc.value.code == 0


def test_disable_auth_not_enabled(monkeypatch, capsys):
    monkeypatch.setattr(pc, "_auth_exists", lambda: False)
    pc._disable_auth()
    assert "not enabled" in capsys.readouterr().out


def test_disable_auth_success(monkeypatch, capsys):
    monkeypatch.setattr(pc, "_auth_exists", lambda: True)
    monkeypatch.setattr(pc, "_caddy_api", lambda m, p: (200, ""))
    monkeypatch.setattr(pc, "_delete_from_keepass", lambda: None)
    pc._disable_auth()
    assert "Auth disabled" in capsys.readouterr().out


def test_show_creds_none(monkeypatch, capsys):
    monkeypatch.setattr(pc, "_load_from_keepass", lambda: None)
    pc._show_creds()
    assert "No credentials" in capsys.readouterr().out


def test_show_creds_set(monkeypatch, capsys):
    monkeypatch.setattr(
        pc,
        "_load_from_keepass",
        lambda: {"username": "u", "password": "p", "password_hash": "h"},
    )
    pc._show_creds()
    out = capsys.readouterr().out
    assert "Username: u" in out


def test_rollback_not_enabled(monkeypatch, capsys):
    monkeypatch.setattr(pc, "_auth_exists", lambda: False)
    pc._rollback_auth()
    assert "nothing to rollback" in capsys.readouterr().out
