"""Tests for scripts/register_external_agent.py."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import register_external_agent as rea


@pytest.fixture
def redirect_paths(monkeypatch, tmp_path):
    agents_file = tmp_path / "memory" / "agents.json"
    external_dir = tmp_path / "messages" / "external"
    portal_cfg = tmp_path / "memory" / "portal_config.json"
    keepass_db = tmp_path / "keepass.kdbx"
    agents_file.parent.mkdir(parents=True, exist_ok=True)
    external_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rea, "AGENTS_FILE", agents_file)
    monkeypatch.setattr(rea, "EXTERNAL_DIR", external_dir)
    monkeypatch.setattr(rea, "PORTAL_CONFIG_PATH", portal_cfg)
    monkeypatch.setattr(rea, "KEEPASS_DB", keepass_db)
    return {
        "agents": agents_file,
        "external": external_dir,
        "portal": portal_cfg,
        "keepass": keepass_db,
    }


# ─── Helpers ────────────────────────────────────────────────────────────────


def test_ensure_files_creates_inbox_outbox(redirect_paths):
    inbox, outbox = rea._ensure_files("bot")
    assert inbox.exists()
    assert outbox.exists()
    assert json.loads(inbox.read_text()) == []


def test_ensure_files_idempotent(redirect_paths):
    inbox, _ = rea._ensure_files("bot")
    inbox.write_text('[{"x":1}]')
    rea._ensure_files("bot")
    # Existing content preserved
    assert json.loads(inbox.read_text()) == [{"x": 1}]


def test_load_capabilities_from_inline():
    args = argparse.Namespace(
        capabilities_file=None, capabilities_inline='[{"id":"a"}]'
    )
    assert rea._load_capabilities(args) == [{"id": "a"}]


def test_load_capabilities_empty():
    args = argparse.Namespace(capabilities_file=None, capabilities_inline=None)
    assert rea._load_capabilities(args) == []


def test_load_capabilities_from_file(tmp_path):
    f = tmp_path / "c.json"
    f.write_text('[{"id":"x"}]')
    args = argparse.Namespace(capabilities_file=str(f), capabilities_inline=None)
    assert rea._load_capabilities(args) == [{"id": "x"}]


def test_load_capabilities_not_a_list():
    args = argparse.Namespace(
        capabilities_file=None, capabilities_inline='{"not":"list"}'
    )
    with pytest.raises(SystemExit):
        rea._load_capabilities(args)


def test_load_public_url_missing(redirect_paths):
    assert rea._load_public_url() is None


def test_load_public_url_present(redirect_paths):
    redirect_paths["portal"].parent.mkdir(parents=True, exist_ok=True)
    redirect_paths["portal"].write_text(json.dumps({"public_url": "https://x.com/"}))
    assert rea._load_public_url() == "https://x.com"


def test_load_public_url_corrupt(redirect_paths):
    redirect_paths["portal"].parent.mkdir(parents=True, exist_ok=True)
    redirect_paths["portal"].write_text("nope")
    assert rea._load_public_url() is None


def test_load_basic_auth_no_db(redirect_paths):
    assert rea._load_basic_auth() == (None, None)


# ─── cmd_register ───────────────────────────────────────────────────────────


def test_cmd_register_invalid_name(redirect_paths):
    args = argparse.Namespace(
        name="bad name!",
        timeout_seconds=300,
        responsibilities="r",
        capabilities_file=None,
        capabilities_inline=None,
    )
    with pytest.raises(SystemExit):
        rea.cmd_register(args)


def test_cmd_register_bad_timeout(redirect_paths):
    args = argparse.Namespace(
        name="bot",
        timeout_seconds=0,
        responsibilities="r",
        capabilities_file=None,
        capabilities_inline=None,
    )
    with pytest.raises(SystemExit):
        rea.cmd_register(args)


def test_cmd_register_creates_entry(redirect_paths, capsys):
    args = argparse.Namespace(
        name="bot",
        timeout_seconds=300,
        responsibilities="do stuff",
        capabilities_file=None,
        capabilities_inline='[{"id":"x"}]',
    )
    rc = rea.cmd_register(args)
    assert rc == 0
    agents = json.loads(redirect_paths["agents"].read_text())
    assert len(agents) == 1
    assert agents[0]["name"] == "bot"
    assert agents[0]["status"] == "offline"
    assert agents[0]["timeout_seconds"] == 300


def test_cmd_register_updates_existing(redirect_paths):
    redirect_paths["agents"].write_text(
        json.dumps(
            [
                {
                    "type": "external",
                    "name": "bot",
                    "status": "online",
                    "last_ping_at": "2024-01-01T00:00:00",
                    "responsibilities": "old",
                    "timeout_seconds": 100,
                    "capabilities": [],
                    "inbox": "",
                    "outbox": "",
                }
            ]
        )
    )
    args = argparse.Namespace(
        name="bot",
        timeout_seconds=600,
        responsibilities="new",
        capabilities_file=None,
        capabilities_inline=None,
    )
    rc = rea.cmd_register(args)
    assert rc == 0
    agents = json.loads(redirect_paths["agents"].read_text())
    assert len(agents) == 1
    assert agents[0]["responsibilities"] == "new"
    assert agents[0]["timeout_seconds"] == 600
    assert agents[0]["last_ping_at"] == "2024-01-01T00:00:00"  # preserved


# ─── cmd_list ───────────────────────────────────────────────────────────────


def test_cmd_list_empty(redirect_paths, capsys):
    rc = rea.cmd_list(argparse.Namespace())
    assert rc == 0
    assert "no agents registered" in capsys.readouterr().out


def test_cmd_list_with_entries(redirect_paths, capsys):
    redirect_paths["agents"].write_text(
        json.dumps(
            [
                {
                    "name": "bot",
                    "type": "external",
                    "status": "online",
                    "timeout_seconds": 300,
                    "last_ping_at": "2024-01-01",
                }
            ]
        )
    )
    rc = rea.cmd_list(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "bot" in out


# ─── cmd_deactivate ────────────────────────────────────────────────────────


def test_cmd_deactivate_not_found(redirect_paths, capsys):
    redirect_paths["agents"].write_text("[]")
    args = argparse.Namespace(deactivate="ghost")
    rc = rea.cmd_deactivate(args)
    assert rc == 1


def test_cmd_deactivate_success(redirect_paths, capsys):
    redirect_paths["agents"].write_text(
        json.dumps([{"name": "bot", "status": "online"}])
    )
    args = argparse.Namespace(deactivate="bot")
    rc = rea.cmd_deactivate(args)
    assert rc == 0
    agents = json.loads(redirect_paths["agents"].read_text())
    assert agents[0]["status"] == "deactivated"


# ─── cmd_setup ──────────────────────────────────────────────────────────────


def test_cmd_setup_invalid_name(redirect_paths, capsys):
    args = argparse.Namespace(setup="bad name!")
    assert rea.cmd_setup(args) == 1


def test_cmd_setup_unknown_agent(redirect_paths, capsys):
    redirect_paths["agents"].write_text("[]")
    args = argparse.Namespace(setup="bot")
    assert rea.cmd_setup(args) == 1


def _setup_bot(redirect_paths, monkeypatch):
    redirect_paths["agents"].write_text(json.dumps([{"name": "bot"}]))
    monkeypatch.setattr(rea, "_load_public_url", lambda: "https://example.com")
    monkeypatch.setattr(rea, "_load_basic_auth", lambda: ("user", "pass"))


def test_cmd_setup_prints_instruction(redirect_paths, monkeypatch, capsys):
    _setup_bot(redirect_paths, monkeypatch)
    args = argparse.Namespace(setup="bot")
    assert rea.cmd_setup(args) == 0
    out = capsys.readouterr().out
    assert "persistent Monitor" in out
    assert "PING_FAILED http=<code>" not in out
    assert "https://example.com/external-agent/ping" in out
    assert "https://example.com/external-agent/read-inbox" in out
    assert f"sleep {rea.DEFAULT_POLL_SECONDS}" in out
    assert "user:pass" in out
    assert not out.lstrip().startswith("```/loop")


def test_cmd_setup_monitor_custom_poll_seconds(redirect_paths, monkeypatch, capsys):
    _setup_bot(redirect_paths, monkeypatch)
    args = argparse.Namespace(setup="bot", client="monitor", poll_seconds=45)
    assert rea.cmd_setup(args) == 0
    assert "sleep 45" in capsys.readouterr().out


def test_cmd_setup_loop_client(redirect_paths, monkeypatch, capsys):
    _setup_bot(redirect_paths, monkeypatch)
    args = argparse.Namespace(setup="bot", client="loop", poll_seconds=30)
    assert rea.cmd_setup(args) == 0
    out = capsys.readouterr().out
    assert "```/loop 10m" in out
    assert "https://example.com/external-agent/ping" in out
    assert "user:pass" in out
    assert "persistent Monitor" not in out


@pytest.mark.parametrize("poll_seconds", [0, -5])
def test_cmd_setup_rejects_nonpositive_poll_seconds(
    redirect_paths, monkeypatch, poll_seconds
):
    _setup_bot(redirect_paths, monkeypatch)
    args = argparse.Namespace(setup="bot", client="monitor", poll_seconds=poll_seconds)
    assert rea.cmd_setup(args) == 1


def test_cmd_setup_rejects_poll_seconds_at_or_above_timeout(
    redirect_paths, monkeypatch
):
    _setup_bot(redirect_paths, monkeypatch)
    args = argparse.Namespace(
        setup="bot", client="monitor", poll_seconds=rea.DEFAULT_TIMEOUT_SECONDS
    )
    assert rea.cmd_setup(args) == 1


def test_cmd_setup_loop_ignores_poll_seconds(redirect_paths, monkeypatch):
    _setup_bot(redirect_paths, monkeypatch)
    args = argparse.Namespace(setup="bot", client="loop", poll_seconds=0)
    assert rea.cmd_setup(args) == 0


def test_setup_prompts_quote_credentials():
    creds = "u:p'$w"
    quoted = "'u:p'\"'\"'$w'"
    base = "https://example.com/x"
    assert f"-u {quoted}" in rea._loop_prompt("bot", base, creds)
    assert f"-u {quoted}" in rea._monitor_prompt("bot", base, creds, 30)


# ─── Monitor watch script ───────────────────────────────────────────────────

_SHELLS = [s for s in ("sh", "bash", "zsh") if shutil.which(s)]
_FAIL = None  # fake curl exits non-zero, like a connection failure
B0 = '{"unread": 0}'
B1 = '{"unread": 1, "unread_ids": ["m1"]}'
B2 = '{"unread": 2, "unread_ids": ["m1", "m2"]}'


@pytest.mark.parametrize("shell", _SHELLS)
def test_monitor_command_is_valid_shell(shell):
    script = rea._monitor_command("bot", "https://example.com/x", "u:p'$w", 30)
    result = subprocess.run([shell, "-n", "-c", script], capture_output=True)
    assert result.returncode == 0, result.stderr


def _run_monitor(tmp_path, responses, *, polls=None, shell="sh") -> list[str]:
    """Run the watch script against a fake curl; return its output lines.

    The fake curl returns *responses* in order, one ``(body, http_code)`` per
    poll, repeating the last one; ``_FAIL`` simulates a connection failure.
    The fake sleep stops the loop after *polls* polls (default: one more than
    ``len(responses)``, so the last response is seen twice).
    """
    polls = polls or len(responses) + 1
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for i, resp in enumerate(responses, 1):
        text = "FAIL" if resp is _FAIL else f"{resp[0]} {resp[1]}"
        (tmp_path / f"resp{i}").write_text(text)
    counter = tmp_path / "n"
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        f"n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1))\n"
        f'[ "$n" -gt {len(responses)} ] && n={len(responses)}\n'
        f"c=$(cat {tmp_path}/resp$n)\n"
        '[ "$c" = FAIL ] && exit 7\n'
        "printf '%s' \"$c\"\n"
    )
    sleep = fake_bin / "sleep"
    sleep.write_text(
        "#!/bin/sh\n"
        f"n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}\n"
        f'[ "$n" -ge {polls} ] && kill $PPID\n'
        "exit 0\n"
    )
    for f in (curl, sleep):
        f.chmod(0o755)
    script = rea._monitor_command("bot", "https://example.com/x", "u:p", 30)
    result = subprocess.run(
        [shell, "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
        timeout=10,
    )
    return result.stdout.splitlines()


def test_monitor_emits_inbox_once_per_change(tmp_path):
    assert _run_monitor(tmp_path, [(B1, 200)]) == [f"INBOX {B1}"]


def test_monitor_reemits_when_ids_change_or_reappear(tmp_path):
    out = _run_monitor(tmp_path, [(B1, 200), (B2, 200), (B0, 200), (B1, 200)])
    assert out == [f"INBOX {B1}", f"INBOX {B2}", f"INBOX {B1}"]


def test_monitor_silent_when_empty(tmp_path):
    assert _run_monitor(tmp_path, [(B0, 200)]) == []


@pytest.mark.parametrize("shell", _SHELLS)
def test_monitor_deactivated_is_one_line_and_exits(tmp_path, shell):
    body = json.dumps(
        {
            "status": "deactivated",
            "unread_ids": ["m1"],
            "unread": 1,
            "warning": 'Stop.\nMay reactivate via POST /update {"status":"online"}.',
        }
    )
    out = _run_monitor(tmp_path, [(body, 200), (B1, 200)], shell=shell)
    assert out == [f"DEACTIVATED {body}"]


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(
    "body,code",
    [
        (json.dumps({"detail": "unknown agent", "readme": "line1\nline2"}), 404),
        ("<html>\n<body>Bad Gateway</body>\n</html>", 502),
    ],
    ids=["json-readme", "html"],
)
def test_monitor_ping_failed_is_one_line(tmp_path, shell, body, code):
    out = _run_monitor(tmp_path, [(body, code)], polls=5, shell=shell)
    assert out == [f"PING_FAILED http={code}"]


def test_monitor_reports_recovery_after_curl_failures(tmp_path):
    out = _run_monitor(tmp_path, [_FAIL, _FAIL, _FAIL, (B0, 200)])
    assert out == ["PING_FAILED http=000", "PING_RECOVERED"]


# ─── main / argparse ────────────────────────────────────────────────────────


def test_main_requires_name(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["register_external_agent.py"])
    with pytest.raises(SystemExit):
        rea.main()


def test_main_list_dispatch(redirect_paths, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["register_external_agent.py", "--list"])
    rc = rea.main()
    assert rc == 0
