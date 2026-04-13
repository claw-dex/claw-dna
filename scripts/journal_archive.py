#!/usr/bin/env python3
"""Archive old journal entries to keep journal.json lean.

Moves entries older than --keep N cycles into journal-archive.json.
Keeps the active journal.json small for fast loading by cycle_start.py and the portal.

Usage:
  uv run python scripts/journal_archive.py              # archive, keep 20 most recent
  uv run python scripts/journal_archive.py --keep 30   # keep 30 most recent
  uv run python scripts/journal_archive.py --dry-run   # preview without modifying
  uv run python scripts/journal_archive.py --list      # show counts + archive stats
  uv run python scripts/journal_archive.py --search Q  # search active + archive
  uv run python scripts/journal_archive.py --json      # output stats as JSON

Archive: /agent/memory/journal-archive.json (sorted by cycle ascending)
"""

import argparse
import json
import os
import sys
from pathlib import Path

MEMORY_DIR = Path("/agent/memory")
JOURNAL = MEMORY_DIR / "journal.json"
ARCHIVE = MEMORY_DIR / "journal-archive.json"
DEFAULT_KEEP = 20


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


def cmd_archive(keep=DEFAULT_KEEP, dry_run=False):
    entries = _load_json(JOURNAL, [])
    if not isinstance(entries, list):
        print("journal.json is not a list — aborting", file=sys.stderr)
        sys.exit(1)
    if not entries:
        print("Journal is empty — nothing to archive.")
        return 0

    entries.sort(key=lambda e: e.get("cycle", 0))
    total = len(entries)

    if total <= keep:
        print(f"Journal has {total} entries (≤ keep={keep}) — nothing to archive.")
        return 0

    to_archive = entries[: total - keep]
    to_keep = entries[total - keep :]

    print(f"Journal: {total} entries total")
    print(f"  Archive: {len(to_archive)} entries  "
          f"(cycles {to_archive[0].get('cycle')}–{to_archive[-1].get('cycle')})")
    print(f"  Keep:    {len(to_keep)} entries  "
          f"(cycles {to_keep[0].get('cycle')}–{to_keep[-1].get('cycle')})")

    if dry_run:
        size_before = JOURNAL.stat().st_size if JOURNAL.exists() else 0
        approx_size_after = int(size_before * len(to_keep) / total)
        print(f"\n  journal.json size: {size_before // 1024} KB → ~{approx_size_after // 1024} KB")
        print("\n[DRY RUN] No files were modified.")
        return 0

    existing_archive = _load_json(ARCHIVE, [])
    if not isinstance(existing_archive, list):
        existing_archive = []

    new_archive = sorted(
        existing_archive + to_archive,
        key=lambda e: e.get("cycle", 0)
    )

    _write_json_atomic(ARCHIVE, new_archive)
    _write_json_atomic(JOURNAL, to_keep)

    print(f"\n✓ Archived {len(to_archive)} entries → {ARCHIVE}")
    print(f"  journal.json now has {len(to_keep)} entries (was {total})")
    print(f"  journal-archive.json now has {len(new_archive)} entries total")
    return len(to_archive)


def cmd_list():
    active = _load_json(JOURNAL, [])
    archive = _load_json(ARCHIVE, [])

    active_count = len(active) if isinstance(active, list) else 0
    archive_count = len(archive) if isinstance(archive, list) else 0

    journal_size = JOURNAL.stat().st_size if JOURNAL.exists() else 0
    archive_size = ARCHIVE.stat().st_size if ARCHIVE.exists() else 0

    print(f"{'File':<35} {'Entries':>8}  {'Size':>8}")
    print("-" * 55)
    print(f"{'journal.json':<35} {active_count:>8}  {journal_size // 1024:>7} KB")
    if ARCHIVE.exists():
        print(f"{'journal-archive.json':<35} {archive_count:>8}  {archive_size // 1024:>7} KB")
    else:
        print(f"{'journal-archive.json':<35} {'—':>8}  {'(not created)':>12}")
    print("-" * 55)
    total = active_count + archive_count
    total_size = journal_size + archive_size
    print(f"{'TOTAL':<35} {total:>8}  {total_size // 1024:>7} KB")

    if active_count > DEFAULT_KEEP:
        excess = active_count - DEFAULT_KEEP
        print(f"\n  ⚠  Journal has {active_count} entries — {excess} can be archived")
        print(f"     Run: uv run python scripts/journal_archive.py")
    else:
        print(f"\n  ✓  Journal is healthy ({active_count}/{DEFAULT_KEEP} threshold)")


def cmd_search(query):
    q = query.lower()
    results = []

    def matches(entry):
        text = " ".join([
            str(entry.get("goal", "")),
            str(entry.get("summary", "")),
            str(entry.get("outcome", "")),
            " ".join(entry.get("actions", [])),
        ]).lower()
        return q in text

    active = _load_json(JOURNAL, [])
    if isinstance(active, list):
        for e in active:
            if matches(e):
                results.append(("active", e))

    archived = _load_json(ARCHIVE, [])
    if isinstance(archived, list):
        for e in archived:
            if matches(e):
                results.append(("archive", e))

    results.sort(key=lambda r: r[1].get("cycle", 0))

    if not results:
        print(f"No results for '{query}' in active journal or archive.")
        return

    print(f"Found {len(results)} result(s) for '{query}':\n")
    for source, e in results:
        tag = "[archived]" if source == "archive" else "[active]  "
        cycle = e.get("cycle", "?")
        ts = e.get("timestamp", "")[:10]
        goal = (e.get("goal", "") or e.get("summary", ""))[:80]
        print(f"  {tag}  Cycle {str(cycle):>3}  {ts}  {goal}")


def cmd_json(keep=DEFAULT_KEEP):
    active = _load_json(JOURNAL, [])
    archive = _load_json(ARCHIVE, [])
    active_count = len(active) if isinstance(active, list) else 0
    archive_count = len(archive) if isinstance(archive, list) else 0
    journal_size = JOURNAL.stat().st_size if JOURNAL.exists() else 0
    archive_size = ARCHIVE.stat().st_size if ARCHIVE.exists() else 0
    print(json.dumps({
        "active_entries": active_count,
        "archived_entries": archive_count,
        "total_entries": active_count + archive_count,
        "journal_size_kb": round(journal_size / 1024, 1),
        "archive_size_kb": round(archive_size / 1024, 1),
        "would_archive": max(0, active_count - keep),
        "keep_setting": keep,
        "needs_archive": active_count > keep,
    }, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Archive old journal entries to keep journal.json small."
    )
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, metavar="N",
                        help=f"Keep N most recent entries in journal.json (default: {DEFAULT_KEEP})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be archived without modifying files")
    parser.add_argument("--list", action="store_true",
                        help="Show entry counts and file sizes for active journal + archive")
    parser.add_argument("--search", metavar="QUERY",
                        help="Search across active journal and archive by keyword")
    parser.add_argument("--json", action="store_true",
                        help="Output stats as JSON without archiving")
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
