#!/usr/bin/env python3
"""
memory_sync.py — Generate agent auto-memory .md files from JSON memory data.

Reads JSON memory files in /agent/memory/ and generates corresponding .md files
with auto-memory frontmatter format, making them accessible to the agent's
memory system

Usage:
    uv run python scripts/memory_sync.py                          # sync all
    uv run python scripts/memory_sync.py --only capabilities,state  # sync specific
    uv run python scripts/memory_sync.py --dry-run                # preview only

Exit codes: 0 = success, 1 = errors encountered.
"""

import json
import sys
from pathlib import Path

MEMORY = Path("/agent/memory")


def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"  WARN: failed to load {path.name}: {e}", file=sys.stderr)
        return None


def write_md(path: Path, content: str, dry_run: bool = False) -> bool:
    """Atomic write, skip if content unchanged."""
    if path.exists() and path.read_text() == content:
        return False  # no change
    if dry_run:
        print(f"[DRY RUN] Would write {path.name} ({len(content)} bytes)")
        return True
    tmp = path.with_suffix(".md.tmp")
    try:
        tmp.write_text(content)
        tmp.rename(path)
        return True
    except Exception as e:
        tmp.unlink(missing_ok=True)
        print(f"  WARN: failed to write {path.name}: {e}", file=sys.stderr)
        return False


def frontmatter(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\ntype: user\n---\n\n"


# ── Renderers ──────────────────────────────────────────────────────────────────


def render_capabilities() -> str:
    data = load_json(MEMORY / "capabilities.json")
    if not isinstance(data, list):
        return ""
    md = frontmatter(
        "Agent Capabilities",
        "This assistant's capabilities grouped by category with enabled/disabled status",
    )
    by_cat = {}
    for cap in data:
        cat = cap.get("category", "unknown")
        by_cat.setdefault(cat, []).append(cap)
    for cat in sorted(by_cat):
        md += f"## {cat.title()}\n\n"
        for cap in by_cat[cat]:
            status = "enabled" if cap.get("enabled") else "disabled"
            md += f"- **{cap.get('name', cap.get('id', '?'))}** ({status}): {cap.get('description', '')}\n"
        md += "\n"
    return md


def render_services() -> str:
    data = load_json(MEMORY / "services.json")
    if not isinstance(data, dict):
        return ""
    md = frontmatter(
        "Background Services",
        "Background services this assistant manages and their runtime status",
    )
    if not data:
        md += "No services configured.\n"
        return md
    for name, info in data.items():
        pid = info.get("pid")
        port = info.get("port")
        status = "running" if pid else "stopped"
        cmd = " ".join(info.get("command", [])) if info.get("command") else "N/A"
        md += f"- **Name**: {name}\n"
        md += f"- **Status**: {status}\n"
        if pid:
            md += f"- **PID**: {pid}\n"
        if port:
            md += f"- **Port**: {port}\n"
        md += f"- **Command**: `{cmd}`\n\n"
    return md


def render_state() -> str:
    from scripts.memory_repair import migrate_state_dict

    data = load_json(MEMORY / "state.json")
    if not isinstance(data, dict):
        return ""
    migrate_state_dict(data)
    md = frontmatter(
        "Current State",
        "This assistant's current cycle number, agent status, and last heartbeat timestamp",
    )
    md += f"- **Cycle**: {data.get('cycle_number', '?')}\n"
    md += f"- **Agent status**: {data.get('agent_status', '?')}\n"
    md += f"- **Last heartbeat**: {data.get('last_heartbeat', 'N/A')}\n"
    md += f"- **Last cycle run**: {data.get('last_cycle_run', 'N/A')}\n"
    summary = data.get("last_cycle_summary", "")
    if summary:
        md += f"- **Last cycle summary**: {summary}\n"
    goal = data.get("current_goal")
    if goal:
        md += f"- **Current goal**: {goal}\n"
    return md


def render_cycles() -> str:
    from scripts.memory_repair import migrate_cycles_list

    data = load_json(MEMORY / "cycles.json")
    if not isinstance(data, list):
        return ""
    migrate_cycles_list(data)
    md = frontmatter(
        "Recent Cycles",
        "This assistant's recent cycle history with type, category, status, and duration",
    )
    recent = data[-10:]
    if not recent:
        md += "No cycles recorded yet.\n"
        return md
    for c in reversed(recent):
        cycle_num = c.get("cycle_number", "?")
        ctype = c.get("cycle_type", "?")
        status = c.get("cycle_status", "?")
        cat = c.get("cycle_category", "")
        dur = c.get("duration_seconds")
        summary = c.get("summary", "")
        dur_str = f" ({_fmt_dur(dur)})" if dur else ""
        cat_str = f" [{cat}]" if cat else ""
        md += f"- **Cycle {cycle_num}** ({ctype}{cat_str}): {status}{dur_str}\n"
        if summary:
            md += f"  {summary}\n"
    total = len(data)
    if total > 10:
        md += f"\n_{total - 10} earlier cycles omitted._\n"
    return md


def render_journal() -> str:
    from scripts.memory_repair import migrate_journal_list

    data = load_json(MEMORY / "journal.json")
    if not isinstance(data, list):
        return ""
    migrate_journal_list(data)
    md = frontmatter(
        "Journal",
        "This assistant's recent journal entries from completed cycles with full details",
    )
    recent = data[-25:]
    if not recent:
        md += "No journal entries yet.\n"
        return md
    for e in reversed(recent):
        cycle = e.get("cycle_number", "?")
        ts = (e.get("timestamp", "") or "")[:16]
        ctype = e.get("cycle_type", "")
        status = e.get("cycle_status", "")
        goal = e.get("cycle_goal", "")
        summary = e.get("summary", "")
        category = e.get("cycle_category", "")
        actions = e.get("actions", [])

        # Header with cycle, type, status, category
        header_parts = [f"**Cycle {cycle}**"]
        if ctype:
            header_parts.append(f"type={ctype}")
        if status:
            header_parts.append(f"status={status}")
        if category:
            header_parts.append(f"category={category}")
        header_parts.append(f"[{ts}]")
        md += f"### {' '.join(header_parts)}\n\n"

        # Goal
        if goal:
            md += f"**Goal**: {goal}\n\n"

        # Summary
        if summary:
            md += f"**Summary**: {summary}\n\n"

        # Actions
        if actions and isinstance(actions, list) and len(actions) > 0:
            md += "**Actions**:\n"
            for action in actions:
                md += f"- {action}\n"
            md += "\n"

        md += "---\n\n"

    total = len(data)
    if total > 25:
        md += f"\n_{total - 25} earlier entries omitted._\n"
    return md


def render_goals() -> str:
    data = load_json(MEMORY / "goal.json")
    if isinstance(data, dict):
        data = data.get("goals", [])
    if not isinstance(data, list):
        return ""
    md = frontmatter(
        "Goals",
        "Goals this assistant is tracking grouped by status",
    )
    if not data:
        md += "No goals recorded yet.\n"
        return md
    by_status = {}
    for g in data:
        s = g.get("status", "unknown")
        by_status.setdefault(s, []).append(g)
    order = ["in_progress", "pending", "completed", "failed"]
    for s in order:
        goals = by_status.pop(s, [])
        if not goals:
            continue
        md += f"## {s.title()}\n\n"
        for g in goals:
            text = g.get("content") or g.get("goal") or "?"
            md += f"- {text}\n"
        md += "\n"
    # Any remaining statuses
    for s, goals in by_status.items():
        md += f"## {s.title()}\n\n"
        for g in goals:
            text = g.get("content") or g.get("goal") or "?"
            md += f"- {text}\n"
        md += "\n"
    return md


def render_notes() -> str:
    data = load_json(MEMORY / "notes.json")
    if not isinstance(data, list):
        return ""
    md = frontmatter(
        "Notes",
        "This assistant's personal notes/scratchpad: titles, tags, and content (pinned first)",
    )
    if not data:
        md += "No notes recorded yet.\n"
        return md
    # Sort: pinned first, then most-recently-updated first within each group
    notes = sorted(data, key=lambda n: n.get("updated_at", ""), reverse=True)
    notes = sorted(notes, key=lambda n: not n.get("pinned", False))

    recent = notes[:25]
    for n in recent:
        pin = " [PINNED]" if n.get("pinned") else ""
        title = n.get("title", "(untitled)")
        nid = n.get("id", "?")
        tags = n.get("tags", []) or []
        updated = (n.get("updated_at", "") or "")[:16].replace("T", " ")
        md += f"- **Title**:{title}{pin}\n"
        md += f"- **ID**: {nid}\n"
        if tags:
            md += f"- **Tags**: {', '.join(tags)}\n"
        md += f"- **Updated**: {updated}\n\n"
        content = n.get("content", "") or ""
        if content:
            md += f"{content}\n\n"
        md += "---\n\n"
    total = len(data)
    if total > 25:
        md += f"\n_{total - 25} earlier notes omitted._\n"
    return md


def _fmt_dur(seconds) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


# ── Sync registry ─────────────────────────────────────────────────────────────

SYNC_MAP = {
    "capabilities": ("capabilities_memory.md", render_capabilities),
    "services": ("services_memory.md", render_services),
    "state": ("state_memory.md", render_state),
    "cycles": ("cycles_memory.md", render_cycles),
    "journal": ("journal_memory.md", render_journal),
    "goals": ("goals_memory.md", render_goals),
    "notes": ("notes_memory.md", render_notes),
}


def sync_all(dry_run: bool = False, only=None):
    """Run all memory syncs. Returns (synced_count, error_count)."""
    synced = 0
    errors = 0
    for key, (filename, renderer) in SYNC_MAP.items():
        if only and key not in only:
            continue
        try:
            content = renderer()
            if not content:
                continue
            path = MEMORY / filename
            if write_md(path, content, dry_run=dry_run):
                synced += 1
                if not dry_run:
                    print(f"  synced {filename}")
        except Exception as e:
            errors += 1
            print(f"  ERROR syncing {filename}: {e}", file=sys.stderr)
    return synced, errors


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    only = None
    if "--only" in args:
        idx = args.index("--only")
        if idx + 1 < len(args):
            only = set(args[idx + 1].split(","))

    print("[MEMORY SYNC] Generating .md files from JSON data...")
    synced, errors = sync_all(dry_run=dry_run, only=only)
    if synced == 0 and errors == 0:
        print("[MEMORY SYNC] All files up to date")
    elif errors:
        print(f"[MEMORY SYNC] Done: {synced} synced, {errors} error(s)")
        sys.exit(1)
    else:
        print(f"[MEMORY SYNC] Done: {synced} file(s) synced")


if __name__ == "__main__":
    main()
