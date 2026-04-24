#!/usr/bin/env python3
"""
memory_backup.py — Timestamped snapshot backups for all critical memory files.

Creates atomic, versioned backups in /agent/memory/backups/<timestamp>/.
Supports listing, restoring, and pruning old backups.

Usage:
    python3 memory_backup.py                  # create backup (default)
    python3 memory_backup.py --list           # list all backups
    python3 memory_backup.py --restore <ts>   # restore a specific backup
    python3 memory_backup.py --restore latest # restore most recent backup
    python3 memory_backup.py --prune N        # keep only N most recent backups
    python3 memory_backup.py --check          # show age of most recent backup
    python3 memory_backup.py --json           # output JSON (for scripting)

Exit codes:
    0 = success
    1 = error (see stderr)
    2 = no backups found (for --list, --restore)
"""

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIR = Path("/agent/memory")
BACKUP_ROOT = MEMORY_DIR / "backups"

# Files to include in each backup (critical memory files only)
BACKUP_FILES = [
    "state.json",
    "cycles.json",
    "goal.json",
    "notes.json",
    "journal.json",
    "server_errors.json",
    "command_history.json",
    "bootstrap.json",
]

OPTIONAL_FILES = []

MAX_BACKUPS_DEFAULT = 20  # prune if over this count


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ts_str() -> str:
    """ISO timestamp suitable for directory names (no colons)."""
    return now_utc().strftime("%Y%m%dT%H%M%SZ")


def parse_backup_ts(name: str) -> datetime | None:
    """Parse a backup directory name like 20260218T123045Z → datetime."""
    try:
        return datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def list_backups() -> list[Path]:
    """Return all backup dirs sorted newest-first."""
    if not BACKUP_ROOT.exists():
        return []
    dirs = [d for d in BACKUP_ROOT.iterdir() if d.is_dir() and parse_backup_ts(d.name)]
    return sorted(dirs, key=lambda d: d.name, reverse=True)


def backup_age_str(backup_dir: Path) -> str:
    """Return human-readable age string for a backup dir."""
    ts = parse_backup_ts(backup_dir.name)
    if not ts:
        return "unknown age"
    age_s = (now_utc() - ts).total_seconds()
    if age_s < 60:
        return f"{age_s:.0f}s ago"
    if age_s < 3600:
        return f"{age_s/60:.0f}m ago"
    if age_s < 86400:
        return f"{age_s/3600:.1f}h ago"
    return f"{age_s/86400:.1f}d ago"


def backup_summary(backup_dir: Path) -> dict:
    """Return metadata dict for a backup dir."""
    ts = parse_backup_ts(backup_dir.name)
    files = list(backup_dir.glob("**/*.json"))
    total_bytes = sum(f.stat().st_size for f in files)
    meta_path = backup_dir / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass
    return {
        "timestamp": ts.isoformat() if ts else backup_dir.name,
        "name": backup_dir.name,
        "path": str(backup_dir),
        "files": len(files) - (1 if meta_path.exists() else 0),  # exclude meta.json
        "size_bytes": total_bytes,
        "age": backup_age_str(backup_dir),
        "cycle": meta.get("cycle"),
        "label": meta.get("label", ""),
    }


def cmd_create(label: str = "", quiet: bool = False, json_mode: bool = False) -> int:
    """Create a new timestamped backup."""
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    ts = ts_str()
    dest = BACKUP_ROOT / ts
    dest.mkdir(exist_ok=True)

    copied = []
    skipped = []
    errors = []

    # Copy each file
    for rel in BACKUP_FILES + OPTIONAL_FILES:
        src = MEMORY_DIR / rel
        if not src.exists():
            skipped.append(rel)
            continue
        dst = dest / Path(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            copied.append(rel)
        except Exception as e:
            errors.append(f"{rel}: {e}")

    # Write metadata
    try:
        state = json.loads((MEMORY_DIR / "state.json").read_text())
        cycle = state.get("cycle_number", 0)
    except Exception:
        cycle = 0

    meta = {
        "created": now_utc().isoformat(),
        "cycle": cycle,
        "label": label,
        "files_copied": copied,
        "files_skipped": skipped,
        "errors": errors,
    }
    (dest / "meta.json").write_text(json.dumps(meta, indent=2))

    # Auto-prune if over limit
    pruned = _auto_prune(MAX_BACKUPS_DEFAULT)

    if json_mode:
        result = {
            "action": "create",
            "backup": ts,
            "path": str(dest),
            "files_copied": len(copied),
            "files_skipped": len(skipped),
            "errors": errors,
            "pruned": pruned,
        }
        print(json.dumps(result, indent=2))
    elif not quiet:
        print(f"  Backup created: {ts}")
        print(f"  Files: {len(copied)} copied, {len(skipped)} skipped")
        if errors:
            print(f"  Errors: {', '.join(errors)}", file=sys.stderr)
        if pruned:
            print(f"  Auto-pruned {pruned} old backup(s)")

    return 1 if errors else 0


def cmd_list(json_mode: bool = False) -> int:
    """List all available backups."""
    backups = list_backups()
    if not backups:
        if json_mode:
            print(json.dumps({"backups": []}))
        else:
            print("  No backups found.")
        return 2

    summaries = [backup_summary(b) for b in backups]

    if json_mode:
        print(json.dumps({"backups": summaries}, indent=2))
    else:
        print(f"\n  {'BACKUP':<20}  {'AGE':<12}  {'FILES':>5}  {'CYCLE':>5}  LABEL")
        print(f"  {'-'*20}  {'-'*12}  {'-'*5}  {'-'*5}  {'-'*20}")
        for s in summaries:
            cycle_str = str(s["cycle"]) if s["cycle"] is not None else "?"
            label = s["label"] or ""
            print(
                f"  {s['name']:<20}  {s['age']:<12}  {s['files']:>5}  {cycle_str:>5}  {label}"
            )
        print()

    return 0


def cmd_restore(target: str, dry_run: bool = False, json_mode: bool = False) -> int:
    """Restore a backup by name or 'latest'."""
    backups = list_backups()
    if not backups:
        print("  No backups found.", file=sys.stderr)
        return 2

    if target == "latest":
        src_dir = backups[0]
    else:
        # Find by name (partial match allowed)
        matches = [b for b in backups if b.name == target or b.name.startswith(target)]
        if not matches:
            print(f"  No backup matching '{target}' found.", file=sys.stderr)
            return 1
        src_dir = matches[0]

    meta_path = src_dir / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass

    # List files to restore (exclude meta.json)
    files_to_restore = [f for f in src_dir.rglob("*.json") if f.name != "meta.json"]

    if dry_run:
        if json_mode:
            result = {
                "action": "restore_dry_run",
                "source": src_dir.name,
                "files": [str(f.relative_to(src_dir)) for f in files_to_restore],
            }
            print(json.dumps(result, indent=2))
        else:
            print(f"\n  DRY RUN — would restore from: {src_dir.name}")
            print(f"  Backup created: {meta.get('created', 'unknown')}")
            print(f"  Files to restore: {len(files_to_restore)}")
            for f in files_to_restore:
                rel = f.relative_to(src_dir)
                print(f"    {rel}")
            print()
        return 0

    # Create a safety backup before restoring
    print(f"  Creating pre-restore safety backup...")
    cmd_create(label=f"pre-restore-from-{src_dir.name}", quiet=True)

    restored = []
    errors = []
    for src_file in files_to_restore:
        rel = src_file.relative_to(src_dir)
        dst = MEMORY_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Validate JSON before overwriting
            json.loads(src_file.read_text())
            shutil.copy2(src_file, dst)
            restored.append(str(rel))
        except json.JSONDecodeError:
            errors.append(f"{rel}: invalid JSON in backup")
        except Exception as e:
            errors.append(f"{rel}: {e}")

    if json_mode:
        result = {
            "action": "restore",
            "source": src_dir.name,
            "restored": restored,
            "errors": errors,
        }
        print(json.dumps(result, indent=2))
    else:
        print(f"  Restored from: {src_dir.name}")
        print(f"  Files restored: {len(restored)}")
        if errors:
            print(f"  Errors ({len(errors)}):")
            for e in errors:
                print(f"    {e}", file=sys.stderr)

    return 1 if errors else 0


def _auto_prune(keep: int) -> int:
    """Prune old backups, keeping the `keep` most recent. Returns count pruned."""
    backups = list_backups()
    to_delete = backups[keep:]
    for b in to_delete:
        try:
            shutil.rmtree(b)
        except Exception:
            pass
    return len(to_delete)


def cmd_prune(keep: int, json_mode: bool = False) -> int:
    """Keep only the N most recent backups."""
    backups = list_backups()
    to_delete = backups[keep:]

    deleted = []
    errors = []
    for b in to_delete:
        try:
            shutil.rmtree(b)
            deleted.append(b.name)
        except Exception as e:
            errors.append(f"{b.name}: {e}")

    if json_mode:
        result = {"action": "prune", "kept": keep, "deleted": deleted, "errors": errors}
        print(json.dumps(result, indent=2))
    else:
        print(f"  Pruned {len(deleted)} backup(s), kept {min(keep, len(backups))}")
        if errors:
            for e in errors:
                print(f"  Error: {e}", file=sys.stderr)

    return 1 if errors else 0


def cmd_check(json_mode: bool = False) -> int:
    """Show age and status of most recent backup."""
    backups = list_backups()

    if not backups:
        if json_mode:
            print(json.dumps({"status": "no_backups", "count": 0}))
        else:
            print("  No backups found. Run memory_backup.py to create one.")
        return 2

    latest = backups[0]
    s = backup_summary(latest)
    ts = parse_backup_ts(latest.name)
    age_s = (now_utc() - ts).total_seconds() if ts else None

    # Warn if backup is older than 1 hour
    stale = age_s is not None and age_s > 3600

    if json_mode:
        result = {
            "status": "stale" if stale else "ok",
            "latest": s,
            "count": len(backups),
            "age_seconds": round(age_s) if age_s else None,
        }
        print(json.dumps(result, indent=2))
    else:
        status_str = "STALE" if stale else "OK"
        color = "\033[33m" if stale else "\033[32m"
        reset = "\033[0m"
        print(f"  Latest backup: {latest.name}  ({s['age']})")
        print(
            f"  Status:        {color}{status_str}{reset}  ({len(backups)} total backups)"
        )
        if stale:
            print(f"  Consider running: python3 /agent/scripts/memory_backup.py")

    return 0


# ── Entry point ─────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Memory backup and restore utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list", action="store_true", help="List all backups")
    parser.add_argument(
        "--restore", metavar="TS", help="Restore a backup (name or 'latest')"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what --restore would do without doing it",
    )
    parser.add_argument(
        "--prune", metavar="N", type=int, help="Keep only N most recent backups"
    )
    parser.add_argument(
        "--check", action="store_true", help="Show age of most recent backup"
    )
    parser.add_argument("--label", default="", help="Label for this backup (optional)")
    parser.add_argument("--quiet", action="store_true", help="Suppress output")
    parser.add_argument(
        "--json", action="store_true", dest="json_mode", help="Output JSON"
    )

    args = parser.parse_args()

    if args.list:
        return cmd_list(json_mode=args.json_mode)
    elif args.restore:
        return cmd_restore(args.restore, dry_run=args.dry_run, json_mode=args.json_mode)
    elif args.prune is not None:
        return cmd_prune(args.prune, json_mode=args.json_mode)
    elif args.check:
        return cmd_check(json_mode=args.json_mode)
    else:
        return cmd_create(label=args.label, quiet=args.quiet, json_mode=args.json_mode)


if __name__ == "__main__":
    sys.exit(main())
