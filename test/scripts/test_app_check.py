"""Tests for scripts/app_check.py."""

from __future__ import annotations

import json
import sys
import types

import pytest

import app_check


@pytest.fixture
def redirect_result(monkeypatch, tmp_path):
    result_path = tmp_path / "app_check_result.json"
    monkeypatch.setattr(app_check, "RESULT_PATH", result_path)
    monkeypatch.setattr(app_check, "AGENT_DIR", tmp_path)
    return result_path


def test_write_result_skips_without_json_flag(monkeypatch, redirect_result):
    monkeypatch.setattr(sys, "argv", ["app_check.py"])
    app_check.write_result("ok")
    assert not redirect_result.exists()


def test_write_result_writes_with_json_flag(monkeypatch, redirect_result):
    monkeypatch.setattr(sys, "argv", ["app_check.py", "--json"])
    app_check.write_result("ok", "all good", ["e1"])
    data = json.loads(redirect_result.read_text())
    assert data["status"] == "ok"
    assert data["detail"] == "all good"
    assert data["exceptions"] == ["e1"]
    assert "timestamp" in data


def test_main_returns_3_when_apptest_unavailable(monkeypatch, redirect_result, capsys):
    # Force ImportError when importing AppTest by stubbing the module to
    # something that raises on attribute access.
    fake_st_testing = types.ModuleType("streamlit.testing.v1")

    def _fail():
        raise ImportError("boom")

    # Replace the import with a module that raises on attribute lookup
    class _Bad(types.ModuleType):
        def __getattr__(self, name):
            raise ImportError(f"no AppTest: {name}")

    bad = _Bad("streamlit.testing.v1")
    monkeypatch.setitem(sys.modules, "streamlit.testing.v1", bad)
    monkeypatch.setattr(sys, "argv", ["app_check.py"])
    rc = app_check.main()
    assert rc == 3
    assert "SKIP" in capsys.readouterr().out


def _install_fake_apptest(monkeypatch, behavior):
    """Install a fake streamlit.testing.v1 module with an AppTest stub."""

    class FakeAppTest:
        exception = []

        @classmethod
        def from_file(cls, path, default_timeout=30):
            return cls()

        def run(self):
            behavior(self)

    fake_mod = types.ModuleType("streamlit.testing.v1")
    fake_mod.AppTest = FakeAppTest
    monkeypatch.setitem(sys.modules, "streamlit.testing.v1", fake_mod)
    return FakeAppTest


def test_main_ok_path(monkeypatch, redirect_result, tmp_path, capsys):
    (tmp_path / "server.py").write_text("# stub")

    def ok(self):
        self.exception = []

    _install_fake_apptest(monkeypatch, ok)
    monkeypatch.setattr(sys, "argv", ["app_check.py", "--json"])
    rc = app_check.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out
    data = json.loads(redirect_result.read_text())
    assert data["status"] == "ok"


def test_main_timeout_path(monkeypatch, redirect_result, tmp_path, capsys):
    (tmp_path / "server.py").write_text("# stub")

    def timeout(self):
        raise TimeoutError("slow")

    _install_fake_apptest(monkeypatch, timeout)
    monkeypatch.setattr(sys, "argv", ["app_check.py", "--json"])
    rc = app_check.main()
    assert rc == 2
    assert "TIMEOUT" in capsys.readouterr().out
    data = json.loads(redirect_result.read_text())
    assert data["status"] == "timeout"


def test_main_run_raises(monkeypatch, redirect_result, tmp_path, capsys):
    (tmp_path / "server.py").write_text("# stub")

    def fail(self):
        raise RuntimeError("nope")

    _install_fake_apptest(monkeypatch, fail)
    monkeypatch.setattr(sys, "argv", ["app_check.py", "--json"])
    rc = app_check.main()
    assert rc == 1
    data = json.loads(redirect_result.read_text())
    assert data["status"] == "fail"


def test_main_render_captures_exceptions(
    monkeypatch, redirect_result, tmp_path, capsys
):
    (tmp_path / "server.py").write_text("# stub")

    class Exc:
        def __init__(self, msg):
            self.value = msg

    def with_excs(self):
        self.exception = [Exc("bang"), Exc("crash")]

    _install_fake_apptest(monkeypatch, with_excs)
    monkeypatch.setattr(sys, "argv", ["app_check.py", "--json"])
    rc = app_check.main()
    assert rc == 1
    data = json.loads(redirect_result.read_text())
    assert data["status"] == "fail"
    assert "bang" in data["exceptions"]
