#!/usr/bin/env python3
"""
skill_lifecycle.py — Pure-Python auto-transitions for agent-created skills.

Reads `skills/.usage.json` and computes lifecycle transitions purely from
timestamps. Phase 1 ships in --report-only mode (no writes). --apply lands
in Phase 2 once the curator cycle is in place.

Rules (only skills with created_by == "agent" and pinned == false):
    active   → stale     when last activity >= 30 days
    stale    → archived  when last activity >= 90 days  (moves dir to .archive/)
    stale    → active    on observed use (handled by skill_manage bump-usage)

Last activity = max(last_used_at, last_patched_at, created_at).
"""

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILLS_DIR = Path("/agent/skills")
ARCHIVE_DIR = SKILLS_DIR / ".archive"
USAGE_FILE = SKILLS_DIR / ".usage.json"

STALE_AFTER_DAYS = 30
ARCHIVE_AFTER_DAYS = 90


def _parse_iso(ts):
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _last_activity(entry: dict):
    candidates = [
        _parse_iso(entry.get("last_used_at")),
        _parse_iso(entry.get("last_patched_at")),
        _parse_iso(entry.get("created_at")),
    ]
    real = [c for c in candidates if c is not None]
    return max(real) if real else None


@dataclass
class Diff:
    active_to_stale: list
    stale_to_archived: list
    skipped_pinned: list
    skipped_seed: list


def compute(now=None) -> Diff:
    now = now or datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=STALE_AFTER_DAYS)
    archive_cutoff = now - timedelta(days=ARCHIVE_AFTER_DAYS)
    if not USAGE_FILE.exists():
        return Diff([], [], [], [])
    try:
        usage = json.loads(USAGE_FILE.read_text())
    except Exception:
        return Diff([], [], [], [])

    active_to_stale, stale_to_archived = [], []
    skipped_pinned, skipped_seed = [], []
    for name, entry in (usage.get("skills") or {}).items():
        if entry.get("created_by") != "agent":
            skipped_seed.append(name)
            continue
        if entry.get("pinned"):
            skipped_pinned.append(name)
            continue
        state = entry.get("state", "active")
        if state == "archived":
            continue
        last = _last_activity(entry)
        if last is None:
            continue
        if last <= archive_cutoff:
            stale_to_archived.append((name, last.isoformat()))
        elif last <= stale_cutoff and state == "active":
            active_to_stale.append((name, last.isoformat()))
    return Diff(active_to_stale, stale_to_archived, skipped_pinned, skipped_seed)


def report(diff: Diff) -> str:
    lines = []
    if diff.active_to_stale:
        lines.append(f"  active→stale ({len(diff.active_to_stale)}):")
        for name, ts in diff.active_to_stale:
            lines.append(f"    - {name}  (last activity {ts})")
    if diff.stale_to_archived:
        lines.append(f"  stale→archived ({len(diff.stale_to_archived)}):")
        for name, ts in diff.stale_to_archived:
            lines.append(f"    - {name}  (last activity {ts})")
    if not lines:
        return "  (no transitions due)"
    if diff.skipped_pinned or diff.skipped_seed:
        lines.append(
            f"  skipped: pinned={len(diff.skipped_pinned)}, "
            f"seed/non-agent={len(diff.skipped_seed)}"
        )
    return "\n".join(lines)


def apply(diff: Diff) -> dict:
    """Write the computed transitions to .usage.json and move archived dirs.

    Holds an exclusive flock on `.usage.json.lock` for the duration so
    concurrent `skill_manage` writes cannot clobber our snapshot.
    """
    if not USAGE_FILE.exists():
        return {"applied": False, "reason": "no .usage.json"}

    import fcntl  # POSIX-only; container is Linux.

    lock_path = USAGE_FILE.with_suffix(USAGE_FILE.suffix + ".lock")
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            usage = json.loads(USAGE_FILE.read_text())
        except Exception as e:
            return {"applied": False, "reason": f"read failed: {e}"}

        now = datetime.now(timezone.utc).isoformat()
        moved, marked_stale, archived = [], [], []
        for name, _ts in diff.active_to_stale:
            entry = usage["skills"].get(name)
            if entry:
                entry["state"] = "stale"
                marked_stale.append(name)
        for name, _ts in diff.stale_to_archived:
            entry = usage["skills"].get(name)
            if not entry:
                continue
            src = SKILLS_DIR / name
            if src.exists():
                ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
                dest = ARCHIVE_DIR / f"{name}-{stamp}"
                if dest.exists():
                    dest = ARCHIVE_DIR / f"{name}-{stamp}-{src.stat().st_ino}"
                try:
                    shutil.move(str(src), str(dest))
                    moved.append(str(dest))
                except OSError:
                    continue
            entry["state"] = "archived"
            entry["archived_at"] = now
            archived.append(name)

        tmp = USAGE_FILE.with_suffix(USAGE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(usage, indent=2))
        tmp.rename(USAGE_FILE)
        return {
            "applied": True,
            "marked_stale": marked_stale,
            "archived": archived,
            "moved_dirs": moved,
        }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--report-only", action="store_true", default=True)
    grp.add_argument("--apply", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    diff = compute()
    if args.json:
        payload = {
            "active_to_stale": diff.active_to_stale,
            "stale_to_archived": diff.stale_to_archived,
            "skipped_pinned": diff.skipped_pinned,
            "skipped_seed": diff.skipped_seed,
        }
        if args.apply:
            payload["applied"] = apply(diff)
        print(json.dumps(payload, indent=2))
        return 0

    print("[SKILL LIFECYCLE]")
    print(report(diff))
    if args.apply:
        result = apply(diff)
        print(
            f"\napplied: marked_stale={len(result.get('marked_stale', []))}, "
            f"archived={len(result.get('archived', []))}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
