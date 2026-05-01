"""Tests for scripts/app_check.py."""

from __future__ import annotations

import json
import sys
import types

import pytest

import app_check


@pytest.fixture
def redirect_result(monkeypatch, tmp_path):
    # Pre-create the sandbox layout main() expects.
    (tmp_path / "memory").mkdir(exist_ok=True)
    (tmp_path / "messages").mkdir(exist_ok=True)
    result_path = tmp_path / "memory" / "app_check_result.json"
    inbox_path = tmp_path / "messages" / "inbox.json"

    # For tests that invoke write_result() directly (no main()).
    monkeypatch.setattr(app_check, "RESULT_PATH", result_path)
    monkeypatch.setattr(app_check, "AGENT_DIR", tmp_path)
    monkeypatch.setattr(app_check, "INBOX_PATH", inbox_path)

    # For tests that go through main(): redirect the sandbox to tmp_path and
    # neutralise the app.shared monkey-patching so it doesn't touch real state.
    monkeypatch.setattr(app_check, "_make_sandbox", lambda: tmp_path)
    monkeypatch.setattr(app_check, "_patch_agent_paths", lambda s: None)

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

    class _Widget:
        def __init__(self, **kwargs):
            self._value = None
            self._clicked = False
            for k, v in kwargs.items():
                setattr(self, k, v)

        def set_value(self, v):
            self._value = v
            return self

        def click(self):
            self._clicked = True
            return self

    class FakeAppTest:
        def __init__(self):
            self.exception = []
            self.text_area = [_Widget()]
            self.button = [_Widget(label="Send")]

        @classmethod
        def from_file(cls, path, default_timeout=30):
            return cls()

        @classmethod
        def from_string(cls, source, default_timeout=30):
            return cls()

        def run(self):
            behavior(self)
            # If the commands_tab form was filled and submitted, mirror the
            # production behavior of writing the message to inbox.json so the
            # post-submit verification step in check_commands_tab_form passes.
            if (
                self.text_area
                and self.text_area[0]._value
                and self.button
                and self.button[0]._clicked
            ):
                inbox_path = app_check.INBOX_PATH
                inbox_path.parent.mkdir(parents=True, exist_ok=True)
                existing = (
                    json.loads(inbox_path.read_text()) if inbox_path.exists() else []
                )
                existing.append({"content": self.text_area[0]._value})
                inbox_path.write_text(json.dumps(existing))

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
