#!/usr/bin/env python3
"""Headless AppTest render check for server.py + commands_tab form.

Uses Streamlit's built-in AppTest framework to do a full headless render,
catching syntax errors, missing imports, and runtime exceptions that the
/_stcore/health endpoint misses.

Two checks are performed:
  1. server.py renders without exceptions.
  2. commands_tab "Queue Command For Next Cycle" form accepts a submission
     without exceptions, and the submitted content actually lands in
     messages/inbox.json. The check runs entirely against a tmp sandbox
     (app.shared paths are monkey-patched), so no cleanup is needed —
     the sandbox is discarded on exit.

Exit codes:
  0 = OK (both checks passed)
  1 = FAIL (a check raised an exception)
  2 = TIMEOUT (render did not complete in time)
  3 = AppTest unavailable (Streamlit version too old or import error)
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# The check always runs in a tmp sandbox — app.shared's "/agent"-rooted
# constants are monkey-patched at runtime (mirroring test/conftest.py).
REPO_ROOT = Path(__file__).resolve().parent.parent

# Populated by main() once the sandbox is created.
AGENT_DIR: Path = Path("/")
RESULT_PATH: Path = Path("/")
INBOX_PATH: Path = Path("/")


def write_result(
    status: str,
    detail: str = "",
    exceptions: list[str] | None = None,
    checks: dict | None = None,
):
    """Atomically write result JSON when --json flag is passed."""
    if "--json" not in sys.argv:
        return

    result = {
        "status": status,
        "detail": detail,
        "exceptions": exceptions or [],
        "checks": checks or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RESULT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2))
    tmp.rename(RESULT_PATH)


def _make_sandbox() -> Path:
    """Create a tmp dir mirroring the /agent layout app/shared expects."""
    sandbox = Path(tempfile.mkdtemp(prefix="app-check-sandbox-"))
    for sub in (
        "memory",
        "memory/logs",
        "memory/heartbeats",
        "messages",
        "messages/external",
        "scripts",
        "web",
        "workspace",
        "workspace/upload",
        "workspace/telegram",
        "workspace/whatsapp",
    ):
        (sandbox / sub).mkdir(parents=True, exist_ok=True)
    return sandbox


def _patch_agent_paths(sandbox: Path) -> None:
    """Rebind every "/agent"-rooted constant in app.shared to *sandbox*.

    This mirrors test/conftest.py's `patch_shared_paths` fixture: we mutate
    module attributes so that any later `from app.shared import X` reads the
    new value. It MUST run before app.data.* (which captures these names at
    import time) is imported. Streamlit's AppTest exec'ing server.py is what
    triggers those imports — so we patch right after importing app.shared.
    """
    import app.shared as shared

    base = str(sandbox)
    shared.AGENT_DIR = base
    shared.MEMORY_DIR = f"{base}/memory"
    shared.LOGS_DIR = f"{base}/memory/logs"
    shared.MESSAGES_DIR = f"{base}/messages"
    shared.SCRIPTS_DIR = f"{base}/scripts"
    shared.HISTORY_PATH = f"{base}/memory/command_history.json"
    shared.GOALS_PATH = f"{base}/memory/goal.json"
    shared.ERROR_LOG_PATH = f"{base}/memory/server_errors.json"
    shared.CHAT_HISTORY_PATH = f"{base}/memory/chat_history.json"
    shared.CHAT_META_PATH = f"{base}/memory/chat_meta.json"
    shared.PORTAL_CONFIG_PATH = f"{base}/memory/portal_config.json"
    shared.SCHEDULED_TASKS_PATH = os.path.join(
        shared.MEMORY_DIR, "scheduled_tasks.json"
    )

    # _startup_check() iterates these module-level structures — they were
    # snapshotted with the old "/agent" prefix at import time, so rebuild.
    shared._CRITICAL_DIRS = [
        shared.MEMORY_DIR,
        shared.LOGS_DIR,
        shared.MESSAGES_DIR,
        f"{base}/web",
        f"{base}/workspace",
    ]
    shared._CRITICAL_FILES = {
        f"{shared.MEMORY_DIR}/state.json": {
            "cycle_number": 0,
            "status": "idle",
            "current_goal": None,
            "last_cycle_summary": None,
            "created_at": None,
            "last_heartbeat": None,
            "last_cycle_run": None,
            "last_cycle_end": None,
            "services": {},
        },
        f"{shared.MEMORY_DIR}/cycles.json": [],
        shared.GOALS_PATH: [],
        shared.HISTORY_PATH: [],
        shared.ERROR_LOG_PATH: [],
        f"{shared.MESSAGES_DIR}/inbox.json": [],
        f"{shared.MESSAGES_DIR}/outbox.json": [],
        f"{shared.MEMORY_DIR}/journal.json": [],
    }


def _collect_exceptions(at) -> list[str]:
    out = []
    for exc in at.exception:
        out.append(str(exc.value) if hasattr(exc, "value") else str(exc))
    return out


def check_server_render(AppTest) -> tuple[str, str, list[str]]:
    """Render server.py headlessly. Returns (status, detail, exceptions)."""
    try:
        at = AppTest.from_file("server.py", default_timeout=30)
        at.run()
    except TimeoutError as e:
        return "timeout", f"server.py render timed out: {e}", []
    except Exception as e:
        return "fail", f"AppTest.run() raised: {type(e).__name__}: {e}", [str(e)]

    if at.exception:
        excs = _collect_exceptions(at)
        return "fail", "; ".join(excs[:3]), excs
    return "ok", "", []


def check_commands_tab_form(AppTest) -> tuple[str, str, list[str], str]:
    """Render commands_tab and submit the Queue Command form.

    Returns (status, detail, exceptions, test_content). test_content is the
    unique sentinel used so the caller can clean up the inbox afterwards.
    """
    test_content = (
        f"[app-check] form submission test {datetime.now(timezone.utc).isoformat()}"
    )
    # Minimal harness: render only the commands tab so the test isolates that
    # module from the rest of the portal (header, other tabs, autorefresh).
    harness = "from app import commands_tab\ncommands_tab.render()\n"

    try:
        at = AppTest.from_string(harness, default_timeout=30)
        at.run()
    except TimeoutError as e:
        return "timeout", f"commands_tab render timed out: {e}", [], test_content
    except Exception as e:
        return (
            "fail",
            f"commands_tab AppTest.run() raised: {type(e).__name__}: {e}",
            [str(e)],
            test_content,
        )

    if at.exception:
        excs = _collect_exceptions(at)
        return "fail", "initial render: " + "; ".join(excs[:3]), excs, test_content

    # AppTest (Streamlit 1.57) flattens form widgets into the top-level
    # collections — there is no `at.form()` accessor. Drive the form by
    # filling its sole text_area and clicking the "Send" submit button.
    if not at.text_area:
        return (
            "fail",
            "commands_tab: no text_area widget found (form missing?)",
            [],
            test_content,
        )
    try:
        at.text_area[0].set_value(test_content)
        send_btn = next((b for b in at.button if b.label == "Send"), None)
        if send_btn is None:
            return (
                "fail",
                "commands_tab: 'Send' submit button not found",
                [],
                test_content,
            )
        send_btn.click()
        at.run()
    except Exception as e:
        return (
            "fail",
            f"commands_tab submit raised: {type(e).__name__}: {e}",
            [str(e)],
            test_content,
        )

    if at.exception:
        excs = _collect_exceptions(at)
        return "fail", "after submit: " + "; ".join(excs[:3]), excs, test_content

    # Verify the message actually landed in inbox.json — proves the full
    # form → queue_to_inbox → disk write path works, not just rendering.
    try:
        if not INBOX_PATH.exists():
            return (
                "fail",
                f"commands_tab: {INBOX_PATH} was not created by submission",
                [],
                test_content,
            )
        inbox = json.loads(INBOX_PATH.read_text())
        if not any(m.get("content") == test_content for m in inbox):
            return (
                "fail",
                "commands_tab: submitted content not found in inbox.json",
                [],
                test_content,
            )
    except Exception as e:
        return (
            "fail",
            f"commands_tab: inbox.json verify failed: {e}",
            [str(e)],
            test_content,
        )

    return "ok", "", [], test_content


def main() -> int:
    try:
        from streamlit.testing.v1 import AppTest
    except ImportError as e:
        msg = f"AppTest unavailable: {e}"
        print(f"[app-check] SKIP — {msg}")
        write_result("skip", msg)
        return 3

    global AGENT_DIR, RESULT_PATH, INBOX_PATH

    # Always run against a tmp sandbox so the check never touches real agent
    # state. app.shared paths are monkey-patched onto the sandbox; chdir to
    # the repo root so AppTest.from_file("server.py") resolves. The sandbox
    # is discarded when this process exits — no cleanup needed.
    AGENT_DIR = _make_sandbox()
    _patch_agent_paths(AGENT_DIR)
    os.chdir(str(REPO_ROOT))
    print(f"[app-check] sandbox: {AGENT_DIR}")

    RESULT_PATH = AGENT_DIR / "memory" / "app_check_result.json"
    INBOX_PATH = AGENT_DIR / "messages" / "inbox.json"

    # ── Check 1: server.py full render ────────────────────────
    server_status, server_detail, server_excs = check_server_render(AppTest)

    # ── Check 2: commands_tab form submission (verifies the message
    #            lands in the sandbox's messages/inbox.json).
    form_status, form_detail, form_excs, _ = check_commands_tab_form(AppTest)

    checks = {
        "server_render": {"status": server_status, "detail": server_detail},
        "commands_tab_form": {"status": form_status, "detail": form_detail},
    }

    # Aggregate result: timeout > fail > ok.
    statuses = [server_status, form_status]
    if "timeout" in statuses:
        overall = "timeout"
        rc = 2
    elif "fail" in statuses:
        overall = "fail"
        rc = 1
    else:
        overall = "ok"
        rc = 0

    if overall == "ok":
        print("[app-check] OK — server render + commands_tab form")
        write_result("ok", checks=checks)
    else:
        merged = "; ".join(d for d in (server_detail, form_detail) if d)
        all_excs = server_excs + form_excs
        label = "TIMEOUT" if overall == "timeout" else "FAIL"
        print(f"[app-check] {label} — {merged}")
        write_result(overall, merged, all_excs, checks=checks)

    return rc


if __name__ == "__main__":
    sys.exit(main())
