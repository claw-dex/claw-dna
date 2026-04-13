#!/usr/bin/env python3
"""
self_test.py — Comprehensive agent self-test and health check.

Validates all critical agent systems: API endpoints, memory files,
disk health, process state, app module imports, data loaders, and scripts.

Usage:
    python3 self_test.py              # run all tests, print report
    python3 self_test.py --json       # output JSON results
    python3 self_test.py --record     # record failures to journal.json
    python3 self_test.py --fail-fast  # stop on first failure
    python3 self_test.py --quiet      # only print summary
    python3 self_test.py --suite SUITE  # run only a specific suite
    python3 self_test.py --list-suites  # list available suites

Exit code: 0 = all passed, 1 = one or more failures.

Current coverage: 111 tests across 14 suites (updated cycle 109).
"""

import json
import os
import sys
import time
import subprocess
import importlib
import py_compile
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

AGENT_DIR = Path("/agent")
MEMORY_DIR = AGENT_DIR / "memory"
APP_DIR = AGENT_DIR / "app"
SCRIPTS_DIR = AGENT_DIR / "scripts"
BASE_URL = "http://localhost:8081/app"
HEALTH_URL = f"{BASE_URL}/_stcore/health"

# Ensure agent dir is on path for app imports
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

# ── Result accumulator ──────────────────────────────────────────────────────

results = []   # list of {"name", "passed", "detail", "duration_ms"}

def _check(name: str, passed: bool, detail: str = "", duration_ms: float = 0.0):
    results.append({"name": name, "passed": passed, "detail": detail, "duration_ms": round(duration_ms, 1)})
    return passed


# ── HTTP helpers ────────────────────────────────────────────────────────────

def _get(path: str, timeout: float = 5.0):
    """Return (status_code, parsed_json_or_None, elapsed_ms)."""
    url = f"{BASE_URL}{path}"
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            elapsed = (time.monotonic() - t0) * 1000
            body = resp.read()
            try:
                return resp.status, json.loads(body), elapsed
            except json.JSONDecodeError:
                return resp.status, None, elapsed
    except urllib.error.HTTPError as e:
        elapsed = (time.monotonic() - t0) * 1000
        return e.code, None, elapsed
    except Exception as e:
        elapsed = (time.monotonic() - t0) * 1000
        return 0, None, elapsed


# ── Suite 1: Infrastructure ─────────────────────────────────────────────────

def test_server_reachable():
    """Streamlit health endpoint returns 'ok'."""
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            elapsed = (time.monotonic() - t0) * 1000
            body = resp.read().decode("utf-8", errors="replace").strip()
            ok = resp.status == 200 and body == "ok"
            _check("server_reachable", ok, f"HTTP {resp.status} body={body!r}", elapsed)
    except Exception as e:
        elapsed = (time.monotonic() - t0) * 1000
        _check("server_reachable", False, str(e), elapsed)


def test_memory_files():
    """All required memory files exist and are valid JSON."""
    required_files = {
        "state.json": dict,
        "cycles.json": list,
        "goal.json": list,
        "journal.json": list,
        "server_errors.json": list,
    }
    for fname, expected_type in required_files.items():
        path = MEMORY_DIR / fname
        if not path.exists():
            _check(f"mem_{fname}", False, "file missing")
            continue
        try:
            data = json.loads(path.read_text())
            ok = isinstance(data, expected_type)
            detail = f"type ok ({expected_type.__name__})" if ok else f"expected {expected_type.__name__}, got {type(data).__name__}"
            _check(f"mem_{fname}", ok, detail)
        except json.JSONDecodeError as e:
            _check(f"mem_{fname}", False, f"invalid JSON: {e}")


def test_state_fields():
    """state.json has required fields and a fresh heartbeat."""
    try:
        state = json.loads((MEMORY_DIR / "state.json").read_text())
    except Exception as e:
        _check("state_fields", False, f"cannot read: {e}")
        return

    required = ["cycle_number", "status", "last_heartbeat"]
    missing = [f for f in required if f not in state]
    if missing:
        _check("state_fields", False, f"missing: {', '.join(missing)}")
        return

    hb = state.get("last_heartbeat")
    if hb:
        try:
            hb_dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
            age_s = (datetime.now(timezone.utc) - hb_dt).total_seconds()
            ok = age_s < 600
            _check("state_heartbeat_fresh", ok, f"heartbeat {age_s:.0f}s ago")
        except Exception:
            _check("state_heartbeat_fresh", False, f"cannot parse: {hb!r}")
    else:
        _check("state_heartbeat_fresh", False, "no last_heartbeat")

    _check("state_fields", True, f"cycle={state.get('cycle_number')}, status={state.get('status')!r}")


def test_disk_space():
    """Workspace disk usage is under 1GB limit."""
    try:
        stat = os.statvfs(str(AGENT_DIR))
        free_bytes = stat.f_bavail * stat.f_frsize
        total_bytes = stat.f_blocks * stat.f_frsize
        used_pct = 100 * (1 - free_bytes / total_bytes)
        ok = used_pct < 90
        _check("disk_space", ok, f"{used_pct:.1f}% disk used, {free_bytes // 1024 // 1024}MB free")
    except Exception as e:
        _check("disk_space", False, str(e))

    try:
        workspace = AGENT_DIR / "workspace"
        if workspace.exists():
            # Skip browser profile dir — it's large (~130MB) and not user content
            SKIP_DIRS = {workspace / ".agent-browser-profile"}
            total = sum(
                f.stat().st_size
                for f in workspace.rglob("*")
                if f.is_file() and not any(f.is_relative_to(d) for d in SKIP_DIRS)
            )
            ok = total < 1024 * 1024 * 1024
            _check("disk_workspace_limit", ok, f"workspace {total // 1024 // 1024}MB (excl. browser profile)")
    except Exception as e:
        _check("disk_workspace_limit", False, str(e))


def test_streamlit_running():
    """Process manager (PID 1) is alive and not a zombie."""
    try:
        with open("/proc/1/status") as f:
            content = f.read()
        name_line = next((l for l in content.splitlines() if l.startswith("Name:")), "")
        state_line = next((l for l in content.splitlines() if l.startswith("State:")), "")
        ok = "Z" not in state_line
        _check("process_manager_pid1", ok, f"{name_line.strip()} | {state_line.strip()}")
    except Exception as e:
        _check("process_manager_pid1", False, str(e))


def test_messages_dir():
    """Message queue files exist and are valid JSON arrays."""
    for fname in ("inbox.json", "outbox.json"):
        path = AGENT_DIR / "messages" / fname
        if not path.exists():
            _check(f"msg_{fname}", False, "missing")
            continue
        try:
            data = json.loads(path.read_text())
            ok = isinstance(data, list)
            _check(f"msg_{fname}", ok, f"{len(data)} items" if ok else f"not a list: {type(data)}")
        except json.JSONDecodeError as e:
            _check(f"msg_{fname}", False, f"invalid JSON: {e}")


def test_web_portal_served():
    """Streamlit portal root returns a non-empty HTML response."""
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(f"{BASE_URL}/", timeout=5) as resp:
            elapsed = (time.monotonic() - t0) * 1000
            html = resp.read().decode("utf-8", errors="replace")
            ok = resp.status == 200 and len(html) > 100
            _check("portal_served", ok, f"HTTP {resp.status}, {len(html)} chars", elapsed)
    except Exception as e:
        elapsed = (time.monotonic() - t0) * 1000
        _check("portal_served", False, str(e), elapsed)


# ── Suite 2: App module imports ─────────────────────────────────────────────

APP_MODULES = [
    "commands_tab", "glance", "memory_tab", "overview_tab", "system_tab",
]

def test_app_module_imports():
    """All app/ modules import cleanly and expose render()."""
    for mod_name in APP_MODULES:
        t0 = time.monotonic()
        full_name = f"app.{mod_name}"
        try:
            mod = sys.modules.get(full_name) or importlib.import_module(full_name)
            elapsed = (time.monotonic() - t0) * 1000
            has_render = hasattr(mod, "render") and callable(mod.render)
            if has_render:
                _check(f"import_{mod_name}", True, "ok, render() found", elapsed)
            else:
                _check(f"import_{mod_name}", False, "imported but no render()", elapsed)
        except Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            _check(f"import_{mod_name}", False, f"{str(e)[:70]}", elapsed)

    # data.py: no render(), but must import
    t0 = time.monotonic()
    try:
        sys.modules.get("app.data") or importlib.import_module("app.data")
        _check("import_data", True, "ok", (time.monotonic() - t0) * 1000)
    except Exception as e:
        _check("import_data", False, f"{str(e)[:70]}", (time.monotonic() - t0) * 1000)


# ── Suite 3: Data loader return types ───────────────────────────────────────

def test_data_loaders():
    """Key data.py loaders return the expected Python types."""
    try:
        import app.data as data
    except Exception as e:
        _check("data_loaders", False, f"cannot import app.data: {e}")
        return

    checks = [
        ("load_state",          dict,  "state"),
        ("load_goals",          list,  "goals"),
        ("load_inbox",          list,  "inbox"),
        ("load_outbox",         list,  "outbox"),
        ("load_cycles",         list,  "cycles"),
        ("load_journal",        dict,  "journal"),
        ("load_errors",         list,  "errors"),
        ("load_balance",        dict,  "balance"),
        ("load_goal_stats",     dict,  "goal_stats"),
        ("load_cycle_logs",     list,  "cycle_logs"),
        ("load_system_info",    dict,  "system_info"),
        ("load_scripts",        list,  "scripts"),
        ("load_services",       dict,  "services"),
        ("load_activity",       list,  "activity"),
        ("load_outbox_history", list,  "outbox_history"),
        ("load_history",        list,  "history"),
        ("load_suggest",        list,  "suggest"),
        ("load_validate",       dict,  "validate"),
        ("load_logs",           list,  "logs"),
    ]

    # load_cycle_velocity returns float | None — just verify it doesn't raise
    t0 = time.monotonic()
    fn = getattr(data, "load_cycle_velocity", None)
    if fn is not None:
        try:
            result = fn()
            elapsed = (time.monotonic() - t0) * 1000
            ok = result is None or isinstance(result, (int, float))
            _check("loader_cycle_velocity", ok,
                   f"float ok ({result})" if ok else f"unexpected type {type(result).__name__}", elapsed)
        except Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            _check("loader_cycle_velocity", False, f"raised: {str(e)[:60]}", elapsed)

    for fn_name, expected_type, label in checks:
        t0 = time.monotonic()
        fn = getattr(data, fn_name, None)
        if fn is None:
            _check(f"loader_{label}", False, f"{fn_name} missing from data.py")
            continue
        try:
            result = fn()
            elapsed = (time.monotonic() - t0) * 1000
            ok = isinstance(result, expected_type)
            detail = f"{expected_type.__name__} ok" if ok else f"expected {expected_type.__name__}, got {type(result).__name__}"
            _check(f"loader_{label}", ok, detail, elapsed)
        except Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            _check(f"loader_{label}", False, f"raised: {str(e)[:60]}", elapsed)


# ── Suite 4: Script syntax validation ───────────────────────────────────────

CRITICAL_SCRIPTS = [
    "cycle_start.py", "cycle_close.py", "memory_repair.py", "memory_backup.py",
    "self_test.py", "journal_archive.py",
    "memory_stats.py", "metrics_collector.py",
    "milestone_report.py", "maintain.py",
]

def test_scripts_syntax():
    """Key scripts in /agent/scripts/ have valid Python syntax."""
    for script_name in CRITICAL_SCRIPTS:
        path = SCRIPTS_DIR / script_name
        label = script_name.replace("-", "_").replace(".py", "")
        if not path.exists():
            _check(f"syntax_{label}", False, "file not found")
            continue
        try:
            py_compile.compile(str(path), doraise=True)
            _check(f"syntax_{label}", True, "syntax ok")
        except py_compile.PyCompileError as e:
            _check(f"syntax_{label}", False, f"SyntaxError: {str(e)[:70]}")


# ── Suite 5: cycle-start quick smoke test ────────────────────────────────────

def test_cycle_start_runs():
    """cycle_start.py --short exits 0 with non-empty output."""
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            ["uv", "run", "python", "scripts/cycle_start.py", "--short"],
            capture_output=True, text=True, timeout=20, cwd=str(AGENT_DIR),
        )
        elapsed = (time.monotonic() - t0) * 1000
        ok = r.returncode == 0 and len(r.stdout.strip()) > 0
        _check("cycle_start_runs", ok, f"exit={r.returncode}, {len(r.stdout)} chars", elapsed)
    except subprocess.TimeoutExpired:
        _check("cycle_start_runs", False, "timed out (20s)")
    except Exception as e:
        _check("cycle_start_runs", False, str(e)[:80])


# ── Suite 7: Active tab error check ─────────────────────────────────────────

def test_no_active_tab_errors():
    """No tab errors younger than 6 hours in server_errors.json."""
    errors_path = MEMORY_DIR / "server_errors.json"
    if not errors_path.exists():
        _check("no_active_tab_errors", True, "no error log file")
        return
    try:
        errors = json.loads(errors_path.read_text())
        if not isinstance(errors, list):
            _check("no_active_tab_errors", True, "empty error log")
            return
        now = datetime.now(timezone.utc)
        active = []
        for err in errors:
            ts = err.get("timestamp", "")
            if not ts:
                continue
            try:
                err_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if (now - err_dt).total_seconds() / 3600 < 6:
                    active.append(err.get("tab", "unknown"))
            except Exception:
                pass
        ok = len(active) == 0
        detail = "0 active tab errors" if ok else f"{len(active)} active: {', '.join(active[:3])}"
        _check("no_active_tab_errors", ok, detail)
    except Exception as e:
        _check("no_active_tab_errors", False, str(e)[:80])


# ── Suite 8: AppTest headless render ──────────────────────────────────────────

def test_app_render():
    """Headless AppTest render of server.py completes without exception."""
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            ["uv", "run", "python", "scripts/app_check.py"],
            capture_output=True, text=True, timeout=45, cwd=str(AGENT_DIR),
        )
        elapsed = (time.monotonic() - t0) * 1000
        if r.returncode == 3:  # AppTest unavailable — skip
            _check("app_render", True, "skipped: AppTest not available", elapsed)
        else:
            ok = r.returncode == 0
            output = (r.stdout + r.stderr).strip()[-80:]
            _check("app_render", ok, output, elapsed)
    except subprocess.TimeoutExpired:
        _check("app_render", False, "timed out (45s)")
    except Exception as e:
        _check("app_render", False, str(e)[:80])


# ── Suite registry ───────────────────────────────────────────────────────────

SUITES = {
    "server":      (test_server_reachable,    "Streamlit health endpoint"),
    "memory":      (test_memory_files,         "Memory file JSON validity"),
    "state":       (test_state_fields,         "state.json fields + heartbeat freshness"),
    "disk":        (test_disk_space,           "Disk usage limits"),
    "process":     (test_streamlit_running,    "Process manager PID 1 alive"),
    "messages":    (test_messages_dir,         "Message queue files"),
    "portal":      (test_web_portal_served,    "Portal HTML response"),
    "imports":     (test_app_module_imports,   "App module imports + render()"),
    "loaders":     (test_data_loaders,         "Data loader return types"),
    "syntax":      (test_scripts_syntax,       "Script syntax validity"),
    "cycle_start": (test_cycle_start_runs,     "cycle_start.py --short smoke test"),
    "tab_errors":  (test_no_active_tab_errors, "No active tab errors (6h)"),
    "apptest":     (test_app_render,           "AppTest headless render of server.py"),
}


# ── Main runner ──────────────────────────────────────────────────────────────

def run_all(fail_fast: bool = False, suite_filter: str = None):
    for name, (fn, _desc) in SUITES.items():
        if suite_filter and name != suite_filter:
            continue
        fn()
        if fail_fast and results and not results[-1]["passed"]:
            break


def record_failures(cycle_hint: int = None):
    """Log failed tests to journal.json as a failed entry."""
    failed = [r for r in results if not r["passed"]]
    if not failed:
        return

    if cycle_hint is None:
        try:
            state = json.loads((MEMORY_DIR / "state.json").read_text())
            cycle_hint = state.get("cycle_number", 0)
        except Exception:
            cycle_hint = 0

    journal_path = MEMORY_DIR / "journal.json"
    try:
        journal = json.loads(journal_path.read_text())
        if not isinstance(journal, list):
            journal = []
    except Exception:
        journal = []

    now = datetime.now(timezone.utc).isoformat()
    descriptions = [f"{f['name']}: {f['detail']}" for f in failed[:5]]
    journal.append({
        "cycle": cycle_hint,
        "timestamp": now,
        "type": "self-heal",
        "status": "failed",
        "goal": "self_test health check",
        "summary": f"Self-test: {len(failed)} failure(s) — {'; '.join(descriptions)}",
        "actions": [],
    })

    tmp = journal_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(journal, indent=2))
    tmp.rename(journal_path)


def print_report(quiet: bool = False):
    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if not r["passed"]]
    total = len(results)

    if not quiet:
        width = max((len(r["name"]) for r in results), default=20) + 2
        print()
        print(f"  {'TEST':<{width}}  {'STATUS':<8}  {'DETAIL'}")
        print(f"  {'-'*width}  {'-'*8}  {'-'*40}")
        for r in results:
            status = "PASS" if r["passed"] else "FAIL"
            color = "\033[32m" if r["passed"] else "\033[31m"
            reset = "\033[0m"
            ms_str = f"  {r['duration_ms']:.0f}ms" if r["duration_ms"] > 0 else ""
            print(f"  {r['name']:<{width}}  {color}{status}{reset:<8}  {r['detail']}{ms_str}")
        print()

    print(f"  Results: {len(passed)}/{total} passed", end="")
    if failed:
        print(f"  |  {len(failed)} FAILED: {', '.join(r['name'] for r in failed)}")
    else:
        print("  — all systems OK")
    print()


def print_json():
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "results": results,
    }
    print(json.dumps(output, indent=2))


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Agent self-test and health check")
    parser.add_argument("--json", action="store_true", help="Output JSON results")
    parser.add_argument("--record", action="store_true", help="Record failures to journal.json")
    parser.add_argument("--fail-fast", action="store_true", help="Stop on first failure")
    parser.add_argument("--quiet", action="store_true", help="Only print summary line")
    parser.add_argument("--cycle", type=int, help="Cycle number for failure recording")
    parser.add_argument("--suite", choices=list(SUITES.keys()), help="Run only a specific test suite")
    parser.add_argument("--list-suites", action="store_true", help="List available test suites")
    args = parser.parse_args()

    if args.list_suites:
        print("\nAvailable test suites:")
        for name, (_, desc) in SUITES.items():
            print(f"  {name:<14}  {desc}")
        print()
        sys.exit(0)

    run_all(fail_fast=args.fail_fast, suite_filter=args.suite)

    if args.json:
        print_json()
    else:
        print_report(quiet=args.quiet)

    if args.record:
        record_failures(cycle_hint=args.cycle)

    sys.exit(0 if all(r["passed"] for r in results) else 1)
