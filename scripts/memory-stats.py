#!/usr/bin/env python3
"""
memory-stats.py — Fast cycle-start status summary for the agent.

Reads all key memory files and prints a concise, structured report
so future cycles don't have to open multiple files manually.

Usage:
    python3 /agent/scripts/memory-stats.py
    python3 /agent/scripts/memory-stats.py --json      # machine-readable output
    python3 /agent/scripts/memory-stats.py --short     # one-liner summary only
"""

import json
import sys
import datetime
from pathlib import Path
from collections import Counter

MEMORY = Path("/agent/memory")
SCRIPTS = Path("/agent/scripts")


def load_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds/60)}m {int(seconds%60)}s"
    else:
        return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"


def ago(ts_str: str) -> str:
    """Return human-readable time since timestamp."""
    try:
        ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        delta = now - ts
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        elif secs < 3600:
            return f"{secs//60}m ago"
        elif secs < 86400:
            return f"{secs//3600}h ago"
        else:
            return f"{secs//86400}d ago"
    except Exception:
        return ts_str


def summarize_state(state: dict) -> dict:
    return {
        "cycle": state.get("cycle_number"),
        "status": state.get("status"),
        "goal_status": state.get("goal_status"),
        "last_heartbeat": state.get("last_heartbeat"),
        "last_cycle_summary": state.get("last_cycle_summary", "")[:120],
    }


def summarize_goals(goals_data: list) -> dict:
    if not goals_data:
        return {"total": 0, "pending": 0, "in_progress": 0, "completed": 0, "failed": 0}
    by_status = Counter(g.get("status", "unknown") for g in goals_data)
    return {
        "total": len(goals_data),
        "pending": by_status.get("pending", 0),
        "in_progress": by_status.get("in-progress", 0),
        "completed": by_status.get("completed", 0),
        "failed": by_status.get("failed", 0),
        "latest": goals_data[-1] if goals_data else None,
    }


def summarize_cycles(cycles: list) -> dict:
    if not cycles:
        return {}
    by_type = Counter(c.get("type", "unknown") for c in cycles)
    evolve_cycles = [c for c in cycles if c.get("type") == "evolve"]
    by_category = Counter(c.get("category", "unknown") for c in evolve_cycles)

    completed = [c for c in cycles if c.get("status") == "completed" and "duration_seconds" in c]
    avg_dur = sum(c["duration_seconds"] for c in completed) / len(completed) if completed else 0

    return {
        "total": len(cycles),
        "by_type": dict(by_type),
        "evolve_by_category": dict(by_category),
        "avg_duration_seconds": round(avg_dur, 1),
        "last_cycle": cycles[-1] if cycles else None,
    }


def summarize_failures(failures_data: dict) -> dict:
    failures = failures_data.get("failures", []) if isinstance(failures_data, dict) else []
    recent = failures[-3:] if failures else []
    return {
        "total": len(failures),
        "recent": [f.get("summary", str(f))[:80] for f in recent],
    }


def summarize_inbox(inbox_path: Path) -> dict:
    data = load_json(inbox_path)
    if data is None:
        return {"status": "missing"}
    if isinstance(data, list):
        return {"messages": len(data), "types": Counter(m.get("type") for m in data)}
    return {"status": "empty" if not data else "unknown_format"}


def check_portal_health() -> str:
    import subprocess
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "3", "http://localhost:8081/app/_stcore/health"],
            capture_output=True, text=True, timeout=5
        )
        return "ok" if "ok" in r.stdout.lower() else f"UNHEALTHY: {r.stdout[:50]}"
    except Exception as e:
        return f"ERROR: {e}"


def main():
    args = sys.argv[1:]
    json_mode = "--json" in args
    short_mode = "--short" in args

    # Load all memory files
    state = load_json(MEMORY / "state.json") or {}
    goals_raw = load_json(MEMORY / "goal.json")
    goals_list = goals_raw if isinstance(goals_raw, list) else (goals_raw.get("goals", []) if isinstance(goals_raw, dict) else [])
    cycles = load_json(MEMORY / "cycles.json") or []
    journal = load_json(MEMORY / "journal.json") or []
    failures_raw = {"failures": [e for e in journal if isinstance(e, dict) and e.get("status") == "failed"]}
    capabilities_raw = load_json(MEMORY / "capabilities.json")
    capabilities = capabilities_raw if isinstance(capabilities_raw, list) else []

    # Summarize
    s = summarize_state(state)
    g = summarize_goals(goals_list)
    c = summarize_cycles(cycles)
    f = summarize_failures(failures_raw)
    portal = check_portal_health()

    if json_mode:
        print(json.dumps({
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "state": s,
            "goals": g,
            "cycles": c,
            "failures": f,
            "portal": portal,
            "capabilities_count": len(capabilities),
        }, indent=2))
        return

    if short_mode:
        hb = ago(s.get("last_heartbeat", "")) if s.get("last_heartbeat") else "never"
        print(f"Cycle {s.get('cycle')} | {s.get('status')} | Portal: {portal} | Last HB: {hb}")
        if g.get("in_progress"):
            print(f"  Active goals: {g['in_progress']}")
        if f.get("total"):
            print(f"  Failures: {f['total']}")
        return

    # Full human-readable report
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"{'='*60}")
    print(f"  AGENT MEMORY STATS  —  {now_str}")
    print(f"{'='*60}")

    # State
    hb = ago(s.get("last_heartbeat", "")) if s.get("last_heartbeat") else "never"
    print(f"\n[STATE]")
    print(f"  Cycle:         {s.get('cycle', '?')}")
    print(f"  Status:        {s.get('status', '?')}")
    print(f"  Goal status:   {s.get('goal_status', '?')}")
    print(f"  Last HB:       {hb}")
    print(f"  Last summary:  {s.get('last_cycle_summary', '')[:100]}")

    # Portal
    portal_icon = "✓" if portal == "ok" else "✗"
    print(f"\n[PORTAL]  {portal_icon} {portal}")

    # Goals
    print(f"\n[GOALS]  total={g['total']}  pending={g['pending']}  in-progress={g['in_progress']}  completed={g['completed']}  failed={g['failed']}")
    if g.get("latest"):
        lg = g["latest"]
        print(f"  Latest: [{lg.get('status')}] {str(lg.get('content') or lg.get('goal', ''))[:80]}")

    # Cycles
    print(f"\n[CYCLES]  total={c.get('total', 0)}  avg_dur={fmt_duration(c.get('avg_duration_seconds', 0))}")
    if c.get("by_type"):
        for k, v in sorted(c["by_type"].items()):
            print(f"  {k}: {v}")
    if c.get("evolve_by_category"):
        cats = c["evolve_by_category"]
        all_cats = ["capability", "observability", "reliability", "efficiency", "prompt_evolution"]
        print(f"  Evolve breakdown:")
        for cat in all_cats:
            count = cats.get(cat, 0)
            bar = "█" * count + "░" * max(0, 3 - count)
            underserved = " ← underserved" if count == 0 else ""
            print(f"    {cat:<20} {bar} {count}{underserved}")
        other = {k: v for k, v in cats.items() if k not in all_cats}
        for k, v in other.items():
            print(f"    {k:<20} {'█'*v} {v}")

    # Failures
    if f["total"] == 0:
        print(f"\n[FAILURES]  none ✓")
    else:
        print(f"\n[FAILURES]  {f['total']} total")
        for r in f["recent"]:
            print(f"  - {r}")

    # Capabilities
    n_caps = len(capabilities)
    by_cat = Counter(cap.get("category", "unknown") for cap in capabilities)
    print(f"\n[CAPABILITIES]  {n_caps} registered  |  categories: {dict(by_cat)}")

    # Evolve recommendation
    cats = c.get("evolve_by_category", {})
    all_cats = ["capability", "observability", "reliability", "efficiency", "prompt_evolution"]
    zero_cats = [cat for cat in all_cats if cats.get(cat, 0) == 0]
    min_count = min((cats.get(cat, 0) for cat in all_cats), default=0)
    min_cats = [cat for cat in all_cats if cats.get(cat, 0) == min_count]

    print(f"\n[EVOLVE RECOMMENDATION]")
    if zero_cats:
        print(f"  Unstarted categories: {', '.join(zero_cats)}")
        print(f"  → Suggest: {zero_cats[0]}")
    else:
        print(f"  All categories started. Least-done: {', '.join(min_cats)} ({min_count})")
        print(f"  → Suggest: {min_cats[0]}")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
