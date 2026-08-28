#!/usr/bin/env python3
"""Archive old cycle records to keep cycles.json lean.

Moves entries older than --keep N cycles into cycles_archive.json.
Keeps the active cycles.json small for fast loading by cycle_start.py and the portal.

Usage:
  uv run python scripts/cycles_archive.py              # archive, keep 100 most recent
  uv run python scripts/cycles_archive.py --keep 200   # keep 200 most recent
  uv run python scripts/cycles_archive.py --dry-run    # preview without modifying
  uv run python scripts/cycles_archive.py --list       # show counts + archive stats
  uv run python scripts/cycles_archive.py --search Q   # search active + archive
  uv run python scripts/cycles_archive.py --json       # output stats as JSON

Archive: /agent/memory/cycles_archive.json (sorted by cycle_number ascending)

Safety: cycles with cycle_status == "in_progress" are never archived.
"""

import argparse
import json
import sys
from pathlib import Path

MEMORY_DIR = Path("/agent/memory")
CYCLES = MEMORY_DIR / "cycles.json"
ARCHIVE = MEMORY_DIR / "cycles_archive.json"
DEFAULT_KEEP = 100


def _load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json_atomic(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _is_in_progress(entry: dict) -> bool:
    return entry.get("cycle_status") == "in_progress"


def cmd_archive(keep=DEFAULT_KEEP, dry_run=False):
    from scripts.repair_memory_files import migrate_cycles_list

    entries = _load_json(CYCLES, [])
    if not isinstance(entries, list):
        print("cycles.json is not a list — aborting", file=sys.stderr)
        sys.exit(1)
    if not entries:
        print("cycles.json is empty — nothing to archive.")
        return 0

    migrate_cycles_list(entries)
    entries.sort(key=lambda e: e.get("cycle_number", 0))
    total = len(entries)

    if total <= keep:
        print(f"cycles.json has {total} entries (≤ keep={keep}) — nothing to archive.")
        return 0

    # Candidate slice: oldest entries beyond the keep window.
    # Filter out any in_progress cycles (safety: never orphan a live cycle).
    candidates = entries[: total - keep]
    to_archive = [e for e in candidates if not _is_in_progress(e)]
    skipped_in_progress = len(candidates) - len(to_archive)
    archive_cycles = {e.get("cycle_number") for e in to_archive}
    to_keep = [e for e in entries if e.get("cycle_number") not in archive_cycles]

    if not to_archive:
        print(
            f"cycles.json has {total} entries, but all archivable candidates are "
            f"in_progress — nothing to archive."
        )
        return 0

    print(f"cycles.json: {total} entries total")
    print(
        f"  Archive: {len(to_archive)} entries  "
        f"(cycles {to_archive[0].get('cycle_number')}–{to_archive[-1].get('cycle_number')})"
    )
    print(
        f"  Keep:    {len(to_keep)} entries  "
        f"(cycles {to_keep[0].get('cycle_number')}–{to_keep[-1].get('cycle_number')})"
    )
    if skipped_in_progress:
        print(f"  Skipped: {skipped_in_progress} in_progress entries")

    if dry_run:
        size_before = CYCLES.stat().st_size if CYCLES.exists() else 0
        approx_size_after = int(size_before * len(to_keep) / total)
        print(
            f"\n  cycles.json size: {size_before // 1024} KB → ~{approx_size_after // 1024} KB"
        )
        print("\n[DRY RUN] No files were modified.")
        return 0

    existing_archive = _load_json(ARCHIVE, [])
    if not isinstance(existing_archive, list):
        existing_archive = []
    migrate_cycles_list(existing_archive)

    existing_cycles = {e.get("cycle_number") for e in existing_archive}
    new_entries = [
        e for e in to_archive if e.get("cycle_number") not in existing_cycles
    ]
    new_archive = sorted(
        existing_archive + new_entries, key=lambda e: e.get("cycle_number", 0)
    )

    _write_json_atomic(ARCHIVE, new_archive)
    _write_json_atomic(CYCLES, to_keep)

    print(f"\n✓ Archived {len(new_entries)} entries → {ARCHIVE}")
    print(f"  cycles.json now has {len(to_keep)} entries (was {total})")
    print(f"  cycles_archive.json now has {len(new_archive)} entries total")
    return len(new_entries)


def cmd_list():
    active = _load_json(CYCLES, [])
    archive = _load_json(ARCHIVE, [])

    active_count = len(active) if isinstance(active, list) else 0
    archive_count = len(archive) if isinstance(archive, list) else 0

    cycles_size = CYCLES.stat().st_size if CYCLES.exists() else 0
    archive_size = ARCHIVE.stat().st_size if ARCHIVE.exists() else 0

    print(f"{'File':<35} {'Entries':>8}  {'Size':>8}")
    print("-" * 55)
    print(f"{'cycles.json':<35} {active_count:>8}  {cycles_size // 1024:>7} KB")
    if ARCHIVE.exists():
        print(
            f"{'cycles_archive.json':<35} {archive_count:>8}  {archive_size // 1024:>7} KB"
        )
    else:
        print(f"{'cycles_archive.json':<35} {'—':>8}  {'(not created)':>12}")
    print("-" * 55)
    total = active_count + archive_count
    total_size = cycles_size + archive_size
    print(f"{'TOTAL':<35} {total:>8}  {total_size // 1024:>7} KB")

    if active_count > DEFAULT_KEEP:
        excess = active_count - DEFAULT_KEEP
        print(
            f"\n  ⚠  cycles.json has {active_count} entries — {excess} can be archived"
        )
        print(f"     Run: uv run python scripts/cycles_archive.py")
    else:
        print(
            f"\n  ✓  cycles.json is healthy ({active_count}/{DEFAULT_KEEP} threshold)"
        )


def cmd_search(query):
    from scripts.repair_memory_files import migrate_cycles_list

    q = query.lower()
    results = []

    def matches(entry):
        text = " ".join(
            [
                str(entry.get("cycle_number", "")),
                str(entry.get("cycle_goal", "")),
                str(entry.get("cycle_status", "")),
                str(entry.get("cycle_type", "")),
                str(entry.get("cycle_category", "")),
            ]
        ).lower()
        return q in text

    active = _load_json(CYCLES, [])
    if isinstance(active, list):
        migrate_cycles_list(active)
        for e in active:
            if matches(e):
                results.append(("active", e))

    archived = _load_json(ARCHIVE, [])
    if isinstance(archived, list):
        migrate_cycles_list(archived)
        for e in archived:
            if matches(e):
                results.append(("archive", e))

    results.sort(key=lambda r: r[1].get("cycle_number", 0))

    if not results:
        print(f"No results for '{query}' in active cycles or archive.")
        return

    print(f"Found {len(results)} result(s) for '{query}':\n")
    for source, e in results:
        tag = "[archived]" if source == "archive" else "[active]  "
        cycle = e.get("cycle_number", "?")
        status = e.get("cycle_status", "?")
        goal = (e.get("cycle_goal") or "")[:80]
        print(f"  {tag}  Cycle {str(cycle):>4}  [{status:<11}]  {goal}")


def cmd_json(keep=DEFAULT_KEEP):
    active = _load_json(CYCLES, [])
    archive = _load_json(ARCHIVE, [])
    active_count = len(active) if isinstance(active, list) else 0
    archive_count = len(archive) if isinstance(archive, list) else 0
    cycles_size = CYCLES.stat().st_size if CYCLES.exists() else 0
    archive_size = ARCHIVE.stat().st_size if ARCHIVE.exists() else 0
    print(
        json.dumps(
            {
                "active_entries": active_count,
                "archived_entries": archive_count,
                "total_entries": active_count + archive_count,
                "cycles_size_kb": round(cycles_size / 1024, 1),
                "archive_size_kb": round(archive_size / 1024, 1),
                "would_archive": max(0, active_count - keep),
                "keep_setting": keep,
                "needs_archive": active_count > keep,
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description="Archive old cycle records to keep cycles.json small."
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        metavar="N",
        help=f"Keep N most recent entries in cycles.json (default: {DEFAULT_KEEP})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be archived without modifying files",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Show entry counts and file sizes for active cycles + archive",
    )
    parser.add_argument(
        "--search",
        metavar="QUERY",
        help="Search across active cycles and archive by keyword",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output stats as JSON without archiving"
    )
    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif args.search:
        cmd_search(args.search)
    elif args.json:
        cmd_json(keep=args.keep)
    else:
        cmd_archive(keep=args.keep, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
