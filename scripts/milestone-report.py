#!/usr/bin/env python3
"""
milestone-report.py — Generate a milestone summary at significant cycle numbers.

Prints a narrative snapshot of the agent's growth: scripts added, capabilities
gained, cycle throughput, goal success rate, and category balance.

Usage:
    uv run python scripts/milestone-report.py              # auto-detect current cycle
    uv run python scripts/milestone-report.py --cycle 50  # report for cycle 50
    uv run python scripts/milestone-report.py --save      # write to workspace/
    uv run python scripts/milestone-report.py --json      # machine-readable output
    uv run python scripts/milestone-report.py --list      # show all milestone cycles
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

AGENT_DIR = Path("/agent")
MEMORY_DIR = AGENT_DIR / "memory"
WORKSPACE_DIR = AGENT_DIR / "workspace"
SCRIPTS_DIR = AGENT_DIR / "scripts"

MILESTONE_INTERVAL = 25  # Report at 25, 50, 75, 100, ...

_CATEGORY_ICONS = {
    "capability":       "⚡",
    "observability":    "👁️",
    "reliability":      "🛡️",
    "efficiency":       "⚙️",
    "prompt_evolution": "📝",
}


def _load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _load_state():
    return _load_json(MEMORY_DIR / "state.json", {})


def _load_cycles():
    return _load_json(MEMORY_DIR / "cycles.json", [])


def _load_goals():
    return _load_json(MEMORY_DIR / "goal.json", [])


def _load_capabilities():
    """Compute capabilities on-the-fly from filesystem."""
    import glob as _glob
    scripts_dir = MEMORY_DIR.parent / "scripts"
    return {
        "utility_scripts": sorted(f.name for f in scripts_dir.glob("*.py")) if scripts_dir.exists() else [],
        "portal_modules": 0,
        "capabilities": [],
    }


def _load_journal():
    return _load_json(MEMORY_DIR / "journal.json", [])


def _list_scripts():
    try:
        return sorted(
            f.name for f in SCRIPTS_DIR.iterdir()
            if f.suffix in (".py", ".sh") and not f.name.endswith(".backup")
        )
    except Exception:
        return []


def _milestone_cycles(total):
    """Return list of milestone cycle numbers up to total."""
    milestones = []
    n = MILESTONE_INTERVAL
    while n <= total:
        milestones.append(n)
        n += MILESTONE_INTERVAL
    return milestones


def _cycles_up_to(cycles, cycle_num):
    """Return cycles with cycle number <= cycle_num."""
    result = []
    for c in cycles:
        try:
            if int(c.get("cycle", 0)) <= cycle_num:
                result.append(c)
        except (ValueError, TypeError):
            pass
    return result


def _format_duration(seconds):
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds)//60}m {int(seconds)%60:02d}s"
    return f"{int(seconds)//3600}h {(int(seconds)%3600)//60}m"


def build_report(target_cycle, all_cycles, all_goals, caps, journal_entries):
    """Build milestone report data for a given target cycle number."""
    subset = _cycles_up_to(all_cycles, target_cycle)
    completed = [c for c in subset if c.get("status") == "completed"]

    # Type breakdown
    type_counts = {}
    for c in completed:
        t = c.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    # Category breakdown (evolve cycles only)
    cat_counts = {}
    for c in completed:
        if c.get("type") == "evolve":
            cat = c.get("category", "")
            if cat:
                cat_counts[cat] = cat_counts.get(cat, 0) + 1

    # Duration stats
    durations = [c.get("duration_seconds") for c in completed if c.get("duration_seconds")]
    avg_dur = sum(durations) / len(durations) if durations else None
    min_dur = min(durations) if durations else None
    max_dur = max(durations) if durations else None

    # Goal stats
    goals_up_to = all_goals  # goals don't have cycle numbers attached easily
    goal_completed = [g for g in goals_up_to if g.get("status") == "completed"]
    goal_failed = [g for g in goals_up_to if g.get("status") == "failed"]
    goal_total = len(goals_up_to)

    # Time span
    started_at = None
    ended_at = None
    starts = [c.get("start") for c in completed if c.get("start")]
    ends = [c.get("end") for c in completed if c.get("end")]
    if starts:
        started_at = min(starts)
    if ends:
        ended_at = max(ends)

    # Elapsed time
    elapsed_str = "—"
    if started_at and ended_at:
        try:
            t0 = datetime.fromisoformat(started_at)
            t1 = datetime.fromisoformat(ended_at)
            secs = (t1 - t0).total_seconds()
            h = int(secs // 3600)
            m = int((secs % 3600) // 60)
            elapsed_str = f"{h}h {m}m"
        except Exception:
            pass

    # Capability counts (snapshot from current caps.json — best approximation)
    scripts_count = len(caps.get("utility_scripts", []))
    portal_tabs = caps.get("portal_tabs", 0)
    skills = caps.get("agent_skills", caps.get("claude_skills", []))
    commands = caps.get("agent_commands", caps.get("claude_commands", []))
    core_caps = caps.get("capabilities", [])

    # Recent highlights from journal (entries around milestone)
    highlights = []
    window_low = max(1, target_cycle - 9)
    for je in journal_entries:
        try:
            cn = int(je.get("cycle", 0))
        except (ValueError, TypeError):
            continue
        if window_low <= cn <= target_cycle:
            summary = je.get("summary") or ""
            if summary:
                highlights.append({
                    "cycle": cn,
                    "type": je.get("type", ""),
                    "category": je.get("category", ""),
                    "summary": summary[:120],
                })
    highlights.sort(key=lambda x: x["cycle"])

    return {
        "milestone_cycle": target_cycle,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_completed_cycles": len(completed),
        "elapsed_time": elapsed_str,
        "started_at": started_at,
        "ended_at": ended_at,
        "type_breakdown": type_counts,
        "category_breakdown": cat_counts,
        "avg_duration_seconds": round(avg_dur, 1) if avg_dur else None,
        "min_duration_seconds": min_dur,
        "max_duration_seconds": max_dur,
        "goal_total": goal_total,
        "goal_completed": len(goal_completed),
        "goal_failed": len(goal_failed),
        "goal_completion_rate": round(len(goal_completed) / goal_total, 3) if goal_total else None,
        "scripts_count": scripts_count,
        "portal_tabs": portal_tabs,
        "skills_count": len(skills),
        "commands_count": len(commands),
        "core_capabilities_count": len(core_caps),
        "last_10_highlights": highlights[-10:],
    }


def render_markdown(report):
    """Render milestone report as a human-readable markdown string."""
    mc = report["milestone_cycle"]
    gen = report["generated_at"][:16].replace("T", " ")
    elapsed = report["elapsed_time"]
    total_c = report["total_completed_cycles"]

    lines = [
        f"# 🏆 Milestone Report — Cycle {mc}",
        f"",
        f"> Generated: {gen} UTC  |  Elapsed since bootstrap: **{elapsed}**",
        f"",
        f"---",
        f"",
        f"## Summary",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Milestone cycle | **{mc}** |",
        f"| Completed cycles | {total_c} |",
        f"| Time active | {elapsed} |",
        f"| Avg cycle duration | {_format_duration(report['avg_duration_seconds'])} |",
        f"| Min cycle | {_format_duration(report['min_duration_seconds'])} |",
        f"| Max cycle | {_format_duration(report['max_duration_seconds'])} |",
        f"",
        f"## Agent Capabilities at Cycle {mc}",
        f"",
        f"| Capability | Count |",
        f"|-----------|-------|",
        f"| Utility scripts | {report['scripts_count']} |",
        f"| Portal tabs | {report['portal_tabs']} |",
        f"| Agent skills | {report['skills_count']} |",
        f"| Slash commands | {report['commands_count']} |",
        f"| Core capabilities | {report['core_capabilities_count']} |",
        f"",
        f"## Goal Performance",
        f"",
    ]

    gt = report["goal_total"]
    gc = report["goal_completed"]
    gf = report["goal_failed"]
    rate = report["goal_completion_rate"]
    rate_str = f"{round(rate * 100)}%" if rate is not None else "—"
    lines += [
        f"- **Total goals:** {gt}",
        f"- **Completed:** {gc}",
        f"- **Failed:** {gf}",
        f"- **Success rate:** {rate_str}",
        f"",
        f"## Cycle Distribution",
        f"",
    ]

    type_b = report["type_breakdown"]
    for t, count in sorted(type_b.items(), key=lambda x: -x[1]):
        type_icons = {"evolve": "🧬", "goal": "🎯", "bootstrap": "🌱", "self-heal": "🔧"}
        icon = type_icons.get(t, "•")
        lines.append(f"- {icon} **{t}**: {count}")

    lines += ["", "## Evolution Category Balance", ""]
    cat_b = report["category_breakdown"]
    total_evolve = sum(cat_b.values())
    for cat, count in sorted(cat_b.items(), key=lambda x: -x[1]):
        icon = _CATEGORY_ICONS.get(cat, "•")
        pct = f"{round(count/total_evolve*100)}%" if total_evolve else "—"
        lines.append(f"- {icon} **{cat.replace('_', ' ')}**: {count} ({pct})")

    highlights = report["last_10_highlights"]
    if highlights:
        lines += ["", f"## Last 10 Cycles Before Milestone", ""]
        for h in highlights:
            cn = h["cycle"]
            cat = h["category"]
            ctype = h["type"]
            summary = h["summary"]
            icon = _CATEGORY_ICONS.get(cat, "•") if cat else ("🎯" if ctype == "goal" else "🧬")
            lines.append(f"**#{cn}** {icon} {summary}")

    lines += ["", "---", f"_Report generated by milestone-report.py_", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate milestone report for significant cycle numbers"
    )
    parser.add_argument("--cycle", type=int, default=None,
                        help="Target cycle number (default: current cycle)")
    parser.add_argument("--save", action="store_true",
                        help="Save markdown to workspace/milestone_<N>.md")
    parser.add_argument("--json", action="store_true",
                        help="Output raw JSON instead of markdown")
    parser.add_argument("--list", action="store_true",
                        help="List all milestone cycles in cycles.json")
    args = parser.parse_args()

    state = _load_state()
    all_cycles = _load_cycles()
    all_goals = _load_goals()
    caps = _load_capabilities()
    journal_entries = _load_journal()

    current_cycle = state.get("cycle_number") or len(all_cycles)

    if args.list:
        milestones = _milestone_cycles(current_cycle)
        print(f"Milestone cycles (every {MILESTONE_INTERVAL}):")
        for m in milestones:
            subset = _cycles_up_to(all_cycles, m)
            completed = len([c for c in subset if c.get("status") == "completed"])
            print(f"  Cycle {m:4d} — {completed} completed cycles in range")
        return 0

    target = args.cycle if args.cycle is not None else current_cycle

    report = build_report(target, all_cycles, all_goals, caps, journal_entries)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    md = render_markdown(report)
    print(md)

    if args.save:
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        out_path = WORKSPACE_DIR / f"milestone_{target}.md"
        out_path.write_text(md)
        print(f"\n✅ Saved to {out_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
