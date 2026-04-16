#!/usr/bin/env python3
"""Auto-maintenance script for the agent.

Combines multiple housekeeping tasks into one command:
1. Memory validation — checks all JSON files for integrity
2. Journal archival — keeps journal.json lean by archiving old entries
3. Log cleanup — archives old bash log files
4. Cycle prompt/system log cleanup — deletes old cycle-N-system.md / cycle-N-prompt.md
5. Stale process detection — finds zombie server.py processes
6. Disk usage summary — reports workspace and memory sizes

Usage:
    python3 maintain.py              # Run all checks (report only)
    python3 maintain.py --fix        # Run all checks and apply fixes
    python3 maintain.py --json       # Output results as JSON
    python3 maintain.py --check X    # Run specific check only
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIR = Path("/agent/memory")
WORKSPACE_DIR = Path("/agent/workspace")
SCRIPTS_DIR = Path("/agent/scripts")
JOURNAL_PATH = MEMORY_DIR / "journal.json"
JOURNAL_ARCHIVE = MEMORY_DIR / "journal-archive.json"
LOGS_DIR = MEMORY_DIR / "logs"

# Thresholds
JOURNAL_KEEP_ENTRIES = 20
LOG_KEEP_CYCLES = 50
JOURNAL_SIZE_WARN_KB = 30
MEMORY_SIZE_WARN_KB = 100
HEARTBEAT_STALE_MINUTES = 15


def check_memory_integrity():
    """Validate all memory JSON files."""
    results = []
    json_files = {
        "state.json": {"required": ["cycle_number", "status", "last_heartbeat"]},
        "cycles.json": {"required": None},  # array
        "goal.json": {"required": None},  # array
    }

    for fname, spec in json_files.items():
        fpath = MEMORY_DIR / fname
        if not fpath.exists():
            results.append({"file": fname, "status": "error", "msg": "missing"})
            continue
        try:
            data = json.loads(fpath.read_text())
            if spec["required"] and isinstance(data, dict):
                missing = [k for k in spec["required"] if k not in data]
                if missing:
                    results.append(
                        {
                            "file": fname,
                            "status": "warning",
                            "msg": f"missing keys: {missing}",
                        }
                    )
                    continue
            results.append({"file": fname, "status": "ok", "msg": "valid"})
        except json.JSONDecodeError as e:
            results.append(
                {"file": fname, "status": "error", "msg": f"invalid JSON: {e}"}
            )

    # Check journal exists
    if JOURNAL_PATH.exists():
        results.append({"file": "journal.json", "status": "ok", "msg": "exists"})
    else:
        results.append({"file": "journal.json", "status": "error", "msg": "missing"})

    # Cross-check: cycle_number vs cycles.json
    try:
        state = json.loads((MEMORY_DIR / "state.json").read_text())
        cycles = json.loads((MEMORY_DIR / "cycles.json").read_text())
        if cycles:
            max_cycle = max(c["cycle"] for c in cycles)
            if state.get("cycle_number") != max_cycle:
                results.append(
                    {
                        "file": "cross-check",
                        "status": "warning",
                        "msg": f"state.cycle_number={state.get('cycle_number')} != max(cycles)={max_cycle}",
                    }
                )
    except Exception:
        pass

    # Heartbeat staleness
    try:
        state = json.loads((MEMORY_DIR / "state.json").read_text())
        hb = state.get("last_heartbeat", "")
        if hb:
            hb_dt = datetime.fromisoformat(hb)
            age_min = (datetime.now(timezone.utc) - hb_dt).total_seconds() / 60
            if age_min > HEARTBEAT_STALE_MINUTES:
                results.append(
                    {
                        "file": "heartbeat",
                        "status": "warning",
                        "msg": f"stale: {age_min:.0f}m ago (threshold: {HEARTBEAT_STALE_MINUTES}m)",
                    }
                )
    except Exception:
        pass

    return {"name": "memory_integrity", "results": results}


def check_journal_size():
    """Check if journal needs archival."""
    if not JOURNAL_PATH.exists():
        return {
            "name": "journal_size",
            "results": [{"status": "error", "msg": "journal.json missing"}],
        }

    try:
        content = JOURNAL_PATH.read_text()
        data = json.loads(content)
    except (json.JSONDecodeError, OSError) as e:
        return {
            "name": "journal_size",
            "results": [{"status": "error", "msg": f"invalid JSON: {e}"}],
        }

    if not isinstance(data, list):
        return {
            "name": "journal_size",
            "results": [{"status": "error", "msg": "journal.json is not a list"}],
        }

    size_kb = len(content.encode()) / 1024
    entry_count = len(data)

    results = []
    results.append({"status": "info", "msg": f"{size_kb:.1f}KB, {entry_count} entries"})

    if entry_count > JOURNAL_KEEP_ENTRIES + 5:
        results.append(
            {
                "status": "action",
                "msg": f"{entry_count - JOURNAL_KEEP_ENTRIES} entries can be archived (keeping {JOURNAL_KEEP_ENTRIES})",
                "fix": "journal_archive",
            }
        )
    elif size_kb > JOURNAL_SIZE_WARN_KB:
        results.append(
            {
                "status": "warning",
                "msg": f"journal is {size_kb:.0f}KB (threshold: {JOURNAL_SIZE_WARN_KB}KB)",
            }
        )
    else:
        results.append({"status": "ok", "msg": "size within limits"})

    return {"name": "journal_size", "results": results}


def check_log_files():
    """Check bash log files for cleanup opportunities."""
    if not LOGS_DIR.exists():
        return {
            "name": "log_files",
            "results": [{"status": "ok", "msg": "no logs directory"}],
        }

    log_files = list(LOGS_DIR.glob("bash-*.json"))
    total_size = sum(f.stat().st_size for f in log_files)

    results = []
    results.append(
        {
            "status": "info",
            "msg": f"{len(log_files)} log files, {total_size / 1024:.1f}KB total",
        }
    )

    if len(log_files) > LOG_KEEP_CYCLES:
        excess = len(log_files) - LOG_KEEP_CYCLES
        results.append(
            {
                "status": "action",
                "msg": f"{excess} log files can be archived",
                "fix": "log_cleanup",
            }
        )
    else:
        results.append({"status": "ok", "msg": "within limits"})

    return {"name": "log_files", "results": results}


def check_cycle_prompt_logs():
    """Check for old cycle-N-system.md and cycle-N-prompt.md files.

    These files are written by heartbeat.sh on every cycle for debugging purposes.
    After 50 cycles they are no longer needed (constitution allows compression/deletion
    of logs older than 50 cycles). At 136 cycles, ~172 files (1MB) accumulate.
    Keeping only the last 50 reduces the logs directory file count and speeds up
    glob operations in load_cycle_logs().
    """
    if not LOGS_DIR.exists():
        return {
            "name": "cycle_prompt_logs",
            "results": [{"status": "ok", "msg": "no logs directory"}],
        }

    # Find max cycle number from .log files
    cycle_logs = list(LOGS_DIR.glob("cycle-*.log"))
    if not cycle_logs:
        return {
            "name": "cycle_prompt_logs",
            "results": [{"status": "ok", "msg": "no cycle logs yet"}],
        }

    max_cycle = 0
    for f in cycle_logs:
        m = re.search(r"cycle-(\d+)\.log$", f.name)
        if m:
            max_cycle = max(max_cycle, int(m.group(1)))

    cutoff = max_cycle - LOG_KEEP_CYCLES  # delete anything older than this cycle number

    old_files = []
    for pattern in ("cycle-*-system.md", "cycle-*-prompt.md"):
        for f in LOGS_DIR.glob(pattern):
            m = re.search(r"cycle-(\d+)-(?:system|prompt)", f.name)
            if m and int(m.group(1)) < cutoff:
                old_files.append(f)

    total_size = sum(f.stat().st_size for f in old_files)
    results = []
    results.append(
        {
            "status": "info",
            "msg": f"{len(old_files)} old prompt/system log files, {total_size / 1024:.1f}KB",
        }
    )

    if old_files:
        results.append(
            {
                "status": "action",
                "msg": f"{len(old_files)} files from cycles <{cutoff} can be deleted (keeping last {LOG_KEEP_CYCLES})",
                "fix": "cycle_prompt_cleanup",
            }
        )
    else:
        results.append({"status": "ok", "msg": "within limits"})

    return {"name": "cycle_prompt_logs", "results": results}


def check_stale_processes():
    """Find zombie or duplicate server.py processes."""
    results = []
    try:
        out = subprocess.run(
            ["pgrep", "-af", "python3.*server.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = [
            l
            for l in out.stdout.strip().split("\n")
            if l and "pgrep" not in l and not l.startswith("1 ")
        ]
        if len(lines) > 1:
            pids = [l.split()[0] for l in lines]
            results.append(
                {
                    "status": "warning",
                    "msg": f"multiple server.py processes: PIDs {pids}",
                    "fix": "kill_stale",
                }
            )
        elif len(lines) == 1:
            pid = lines[0].split()[0]
            results.append(
                {"status": "ok", "msg": f"single server process (PID {pid})"}
            )
        else:
            results.append({"status": "warning", "msg": "no server.py process found"})
    except Exception as e:
        results.append({"status": "error", "msg": f"process check failed: {e}"})

    return {"name": "stale_processes", "results": results}


def check_disk_usage():
    """Report disk usage for key directories."""
    results = []
    for label, path in [
        ("memory", MEMORY_DIR),
        ("workspace", WORKSPACE_DIR),
        ("web", Path("/agent/web")),
    ]:
        if path.exists():
            try:
                out = subprocess.run(
                    ["du", "-sb", str(path)], capture_output=True, text=True, timeout=5
                )
                size_bytes = int(out.stdout.split()[0])
                size_mb = size_bytes / (1024 * 1024)
                # Warn at 800MB for workspace (approaching 1GB limit), 50MB for memory/web
                warn_mb = 800 if label == "workspace" else 50
                status = "warning" if size_mb > warn_mb else "ok"
                results.append({"status": status, "msg": f"{label}: {size_mb:.1f}MB"})
            except Exception:
                results.append(
                    {"status": "error", "msg": f"{label}: could not measure"}
                )

    return {"name": "disk_usage", "results": results}


def apply_fix(fix_name):
    """Apply a specific fix."""
    if fix_name == "journal_archive":
        try:
            from scripts.journal_archive import cmd_archive

            cmd_archive(keep=JOURNAL_KEEP_ENTRIES)
            return "archived"
        except Exception as e:
            return f"journal_archive failed: {e}"

    elif fix_name == "log_cleanup":
        script = SCRIPTS_DIR / "log_cleanup.sh"
        if script.exists():
            result = subprocess.run(
                ["bash", str(script), "--keep", str(LOG_KEEP_CYCLES)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.stdout.strip() or "cleaned"
        return "log_cleanup.sh not found"

    elif fix_name == "cycle_prompt_cleanup":
        if not LOGS_DIR.exists():
            return "no logs directory"
        cycle_logs = list(LOGS_DIR.glob("cycle-*.log"))
        if not cycle_logs:
            return "no cycle logs found"
        max_cycle = max(
            int(m.group(1))
            for f in cycle_logs
            if (m := re.search(r"cycle-(\d+)\.log$", f.name))
        )
        cutoff = max_cycle - LOG_KEEP_CYCLES
        deleted = 0
        freed_bytes = 0
        for pattern in ("cycle-*-system.md", "cycle-*-prompt.md"):
            for f in LOGS_DIR.glob(pattern):
                m = re.search(r"cycle-(\d+)-(?:system|prompt)", f.name)
                if m and int(m.group(1)) < cutoff:
                    try:
                        freed_bytes += f.stat().st_size
                        f.unlink()
                        deleted += 1
                    except OSError:
                        pass
        return f"deleted {deleted} files, freed {freed_bytes / 1024:.1f}KB"

    elif fix_name == "kill_stale":
        out = subprocess.run(
            ["pgrep", "-af", "python3.*server.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = [
            l
            for l in out.stdout.strip().split("\n")
            if l and "pgrep" not in l and not l.startswith("1 ")
        ]
        if len(lines) > 1:
            # Keep the newest PID, kill the rest
            pids = [int(l.split()[0]) for l in lines]
            pids.sort()
            to_kill = pids[:-1]
            for pid in to_kill:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
            return f"killed stale PIDs: {to_kill}"
        return "no stale processes to kill"

    return f"unknown fix: {fix_name}"


def main():
    args = sys.argv[1:]
    do_fix = "--fix" in args
    as_json = "--json" in args
    specific = None
    if "--check" in args:
        idx = args.index("--check")
        if idx + 1 < len(args):
            specific = args[idx + 1]

    checks = [
        ("memory_integrity", check_memory_integrity),
        ("journal_size", check_journal_size),
        ("log_files", check_log_files),
        ("cycle_prompt_logs", check_cycle_prompt_logs),
        ("stale_processes", check_stale_processes),
        ("disk_usage", check_disk_usage),
    ]

    if specific:
        checks = [(n, f) for n, f in checks if n == specific]
        if not checks:
            print(f"Unknown check: {specific}")
            print(
                f"Available: memory_integrity, journal_size, log_files, cycle_prompt_logs, stale_processes, disk_usage"
            )
            sys.exit(1)

    all_results = []
    fixes_applied = []

    for name, check_fn in checks:
        result = check_fn()
        all_results.append(result)

        if do_fix:
            for r in result.get("results", []):
                if r.get("fix"):
                    fix_result = apply_fix(r["fix"])
                    fixes_applied.append({"fix": r["fix"], "result": fix_result})

    if as_json:
        output = {
            "checks": all_results,
            "fixes_applied": fixes_applied,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        print(json.dumps(output, indent=2))
    else:
        # Human-readable output
        status_icons = {
            "ok": "+",
            "warning": "!",
            "error": "X",
            "info": "~",
            "action": ">",
        }
        for check in all_results:
            print(f"\n=== {check['name']} ===")
            for r in check.get("results", []):
                icon = status_icons.get(r["status"], "?")
                print(f"  [{icon}] {r['msg']}")

        if fixes_applied:
            print(f"\n=== Fixes Applied ===")
            for f in fixes_applied:
                print(f"  [{f['fix']}] {f['result']}")

        # Summary
        all_statuses = [r["status"] for c in all_results for r in c.get("results", [])]
        errors = all_statuses.count("error")
        warnings = all_statuses.count("warning")
        actions = all_statuses.count("action")
        print(
            f"\nSummary: {errors} errors, {warnings} warnings, {actions} actions available"
        )
        if actions and not do_fix:
            print("Run with --fix to apply available fixes")


if __name__ == "__main__":
    main()
