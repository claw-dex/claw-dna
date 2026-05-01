#!/usr/bin/env python3
"""cycle_report.py — Generate a summary report of agent activity.

Usage:
    python3 cycle_report.py                 # Full summary
    python3 cycle_report.py --last N        # Last N cycles only
    python3 cycle_report.py --format md     # Markdown output (default)
    python3 cycle_report.py --format json   # JSON output
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIR = Path("/agent/memory")


def load_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def format_duration(seconds):
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s}s"
    h, m = divmod(m, 60)
    return f"{h}h{m}m"


def main():
    last_n = None
    fmt = "md"

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--last" and i + 1 < len(args):
            last_n = int(args[i + 1])
            i += 2
        elif args[i] == "--format" and i + 1 < len(args):
            fmt = args[i + 1]
            i += 2
        elif args[i] in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            print(f"Unknown argument: {args[i]}")
            sys.exit(1)

    from scripts.memory_repair import (
        migrate_cycles_list,
        migrate_journal_list,
        migrate_state_dict,
    )

    state = load_json(MEMORY_DIR / "state.json") or {}
    migrate_state_dict(state)
    cycles = load_json(MEMORY_DIR / "cycles.json") or []
    migrate_cycles_list(cycles)
    goals = load_json(MEMORY_DIR / "goal.json") or []
    journal = load_json(MEMORY_DIR / "journal.json") or []
    migrate_journal_list(journal)
    failures = {
        "failures": [
            e
            for e in journal
            if isinstance(e, dict) and e.get("cycle_status") == "failed"
        ]
    }

    if last_n:
        cycles = cycles[-last_n:]

    completed = [c for c in cycles if c.get("cycle_status") == "completed"]
    durations = [c["duration_seconds"] for c in completed if c.get("duration_seconds")]

    now = datetime.now(timezone.utc)
    first_start = parse_iso(cycles[0].get("start", "")) if cycles else None
    session_elapsed = (now - first_start).total_seconds() if first_start else 0
    total_active = sum(durations)

    report = {
        "generated_at": now.isoformat(),
        "current_cycle": state.get("cycle_number", "?"),
        "agent_status": state.get("agent_status", "?"),
        "session_duration": format_duration(session_elapsed),
        "total_cycles": len(cycles),
        "completed_cycles": len(completed),
        "avg_cycle_duration": (
            format_duration(sum(durations) / len(durations)) if durations else "N/A"
        ),
        "min_cycle_duration": format_duration(min(durations)) if durations else "N/A",
        "max_cycle_duration": format_duration(max(durations)) if durations else "N/A",
        "utilization": (
            f"{(total_active / session_elapsed * 100):.1f}%"
            if session_elapsed > 0
            else "N/A"
        ),
        "capabilities_count": 0,
        "tools_count": 0,
        "total_goals": len(goals) if isinstance(goals, list) else 0,
        "failure_count": len(failures.get("failures", [])),
        "cycles": [
            {
                "num": c.get("cycle_number"),
                "summary": (c.get("summary") or c.get("cycle_goal") or "?"),
                "status": c.get("cycle_status", "?"),
                "duration": format_duration(c.get("duration_seconds")),
            }
            for c in cycles
        ],
    }

    if fmt == "json":
        print(json.dumps(report, indent=2))
        return

    # Markdown format
    print(f"# Agent Activity Report")
    print(f"Generated: {report['generated_at']}")
    print()
    print(f"## Summary")
    print(f"- **Current cycle:** {report['current_cycle']}")
    print(f"- **Status:** {report['agent_status']}")
    print(f"- **Session duration:** {report['session_duration']}")
    print(f"- **Utilization:** {report['utilization']}")
    print(
        f"- **Cycles:** {report['completed_cycles']} completed / {report['total_cycles']} total"
    )
    print(
        f"- **Avg cycle:** {report['avg_cycle_duration']} (min: {report['min_cycle_duration']}, max: {report['max_cycle_duration']})"
    )
    print(
        f"- **Capabilities:** {report['capabilities_count']} | Tools: {report['tools_count']}"
    )
    print(
        f"- **Goals tracked:** {report['total_goals']} | Failures: {report['failure_count']}"
    )
    print()
    print(f"## Cycle History")
    print(f"| # | Duration | Status | Goal |")
    print(f"|---|----------|--------|------|")
    for c in report["cycles"]:
        print(f"| {c['num']} | {c['duration']} | {c['status']} | {c['summary'][:60]} |")


if __name__ == "__main__":
    main()
