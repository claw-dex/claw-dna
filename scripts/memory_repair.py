#!/usr/bin/env python3
"""
memory_repair.py — Detect and auto-repair corrupted agent memory files.

Scans all critical JSON memory files, attempts repair from .backup copies
or safe defaults, and creates fresh .backup copies after successful repair.

Usage:
    python3 memory_repair.py              # scan + repair + report
    python3 memory_repair.py --dry-run    # scan only, no writes
    python3 memory_repair.py --backup     # only create backups (no repair)
    python3 memory_repair.py --quiet      # only print summary line
    python3 memory_repair.py --json       # output machine-readable JSON

Exit code: 0 = all ok (or repaired), 1 = unrecoverable issues remain.

Enum Reference: See prompts/enum.md for default status values used in repairs.
"""

import json
import shutil
import sys
import datetime
from pathlib import Path

MEMORY_DIR = Path("/agent/memory")
AGENT_DIR = Path("/agent")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ── Defaults for reconstruction ────────────────────────────────────────────────
#
# state.json defaults are duplicated from app/shared.py:STATE_DEFAULTS so that
# memory_repair can run when the app/ package itself is unimportable (the
# whole point of a recovery tool). Keep these two in sync — fields here that
# diverge from STATE_DEFAULTS are scoped to repair-time only:
#   - last_cycle_summary  → marker so post-repair journals identify the source
#   - last_heartbeat / last_cycle_run → timestamped to "now" instead of None,
#     because a freshly-repaired state implies the agent just ran.

DEFAULTS = {
    "state.json": {
        "cycle_number": 0,
        "agent_status": "idle",
        "current_goal": None,
        "last_cycle_summary": "Reconstructed by memory_repair.py",
        "last_heartbeat": _now(),
        "last_cycle_run": _now(),
        "services": {},
    },
    "cycles.json": [],
    "goal.json": [],
    "journal.json": [],
}

# ── Status constants ───────────────────────────────────────────────────────────

STATUS_OK = "ok"
STATUS_REPAIRED = "repaired"
STATUS_FAILED = "failed"
STATUS_BACKUP = "backed-up"


# ── Schema migration helpers ───────────────────────────────────────────────────
#
# Field renames to keep names consistent and disambiguated across the three
# memory files (state.json / cycles.json / journal.json). Every loader should
# call the appropriate migrate_* function on each loaded record so readers
# only ever see the new names — writers also emit only new names. When data
# gets rewritten to disk the legacy keys are dropped permanently.
#
# Old → new key maps:
_STATE_KEY_RENAMES = {"status": "agent_status"}
_CYCLE_KEY_RENAMES = {
    "cycle": "cycle_number",
    "status": "cycle_status",
    "type": "cycle_type",
    "category": "cycle_category",
    "goal": "cycle_goal",  # carried forward from the prior round
}
_JOURNAL_KEY_RENAMES = {
    "cycle": "cycle_number",
    "status": "cycle_status",
    "type": "cycle_type",
    "category": "cycle_category",
    "goal": "cycle_goal",
}


def _apply_renames(entry: dict, mapping: dict) -> bool:
    """Rename legacy keys on ``entry`` in place. Returns True if any change.

    If both old and new keys are present, prefer the new value and drop the
    legacy one (idempotent — safe to call repeatedly).
    """
    changed = False
    for old, new in mapping.items():
        if old in entry:
            if new not in entry:
                entry[new] = entry.pop(old)
            else:
                del entry[old]
            changed = True
    return changed


def migrate_state_dict(state: dict) -> dict:
    """Migrate state.json field names in place. Returns the same dict."""
    if isinstance(state, dict):
        _apply_renames(state, _STATE_KEY_RENAMES)
    return state


def migrate_cycle_entry(entry: dict) -> dict:
    """Migrate a single cycles.json entry in place. Returns the same dict."""
    if isinstance(entry, dict):
        _apply_renames(entry, _CYCLE_KEY_RENAMES)
    return entry


def migrate_journal_entry(entry: dict) -> dict:
    """Migrate a single journal.json entry in place. Returns the same dict."""
    if isinstance(entry, dict):
        _apply_renames(entry, _JOURNAL_KEY_RENAMES)
    return entry


def migrate_cycles_list(cycles: list) -> list:
    """Migrate every entry in a cycles.json list. Mutates and returns it."""
    if isinstance(cycles, list):
        for entry in cycles:
            migrate_cycle_entry(entry)
    return cycles


def migrate_journal_list(journal: list) -> list:
    """Migrate every entry in a journal.json list. Mutates and returns it."""
    if isinstance(journal, list):
        for entry in journal:
            migrate_journal_entry(entry)
    return journal


# ── Core helpers (stateless) ───────────────────────────────────────────────────


def _is_valid_json(path: Path) -> tuple[bool, object]:
    """Returns (valid, parsed_value). parsed_value is error string on failure."""
    try:
        data = json.loads(path.read_text())
        return True, data
    except Exception as e:
        return False, str(e)


def _write_safe(path: Path, data: object) -> bool:
    """Write JSON atomically via a temp file, then rename."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(path)
        return True
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _backup(path: Path) -> bool:
    """Copy path → path.backup. Returns True on success."""
    bak = path.with_suffix(path.suffix + ".backup")
    try:
        shutil.copy2(path, bak)
        return True
    except Exception:
        return False


def _restore_from_backup(path: Path) -> tuple[bool, object]:
    """Try to load a .backup file. Returns (success, data)."""
    bak = path.with_suffix(path.suffix + ".backup")
    if not bak.exists():
        return False, None
    valid, data = _is_valid_json(bak)
    return valid, data


def _salvage_json_array(text: str) -> list | None:
    """
    Try to recover a truncated JSON array by finding the last valid
    complete entry and closing the array. Returns list or None.
    """
    stripped = text.strip()
    if not stripped.startswith("["):
        return None

    last_close = stripped.rfind("}")
    if last_close == -1:
        return None

    for candidate in [
        stripped[: last_close + 1] + "]",
        stripped[: last_close + 1].rstrip().rstrip(",") + "]",
    ]:
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                return data
        except Exception:
            pass

    return None


# ── Per-file processing ────────────────────────────────────────────────────────


def _process_file(filename: str, results: list, dry_run: bool, backup_only: bool):
    path = MEMORY_DIR / filename
    default = DEFAULTS.get(filename)

    # Missing file
    if not path.exists():
        if dry_run or backup_only:
            results.append(
                {
                    "file": filename,
                    "status": STATUS_FAILED,
                    "action": "would-create",
                    "detail": "file missing",
                }
            )
            return
        if default is not None:
            if _write_safe(path, default):
                results.append(
                    {
                        "file": filename,
                        "status": STATUS_REPAIRED,
                        "action": "created-default",
                        "detail": "file was missing",
                    }
                )
            else:
                results.append(
                    {
                        "file": filename,
                        "status": STATUS_FAILED,
                        "action": "create-failed",
                        "detail": "could not write default",
                    }
                )
        else:
            results.append(
                {
                    "file": filename,
                    "status": STATUS_FAILED,
                    "action": "no-default",
                    "detail": "file missing, no default known",
                }
            )
        return

    # BACKUP_ONLY mode
    if backup_only:
        valid, _ = _is_valid_json(path)
        if valid:
            _backup(path)
            results.append(
                {
                    "file": filename,
                    "status": STATUS_BACKUP,
                    "action": "backup-created",
                    "detail": "",
                }
            )
        else:
            results.append(
                {
                    "file": filename,
                    "status": STATUS_FAILED,
                    "action": "skip-backup",
                    "detail": "invalid JSON, backup skipped",
                }
            )
        return

    # Validate JSON
    valid, data = _is_valid_json(path)
    if valid:
        if not dry_run:
            _backup(path)
        results.append(
            {
                "file": filename,
                "status": STATUS_OK,
                "action": "backup-updated" if not dry_run else "valid",
                "detail": "",
            }
        )
        return

    # Invalid JSON — attempt repair
    if dry_run:
        results.append(
            {
                "file": filename,
                "status": STATUS_FAILED,
                "action": "would-repair",
                "detail": f"invalid JSON: {data}",
            }
        )
        return

    # Step 1: try .backup
    restored, bak_data = _restore_from_backup(path)
    if restored:
        if _write_safe(path, bak_data):
            results.append(
                {
                    "file": filename,
                    "status": STATUS_REPAIRED,
                    "action": "restored-from-backup",
                    "detail": f"original error: {data}",
                }
            )
            return
        results.append(
            {
                "file": filename,
                "status": STATUS_FAILED,
                "action": "backup-write-failed",
                "detail": f"backup read ok but write failed: {data}",
            }
        )
        return

    # Step 2: try default
    if default is not None:
        corrupt_path = path.with_suffix(path.suffix + ".corrupt")
        try:
            shutil.copy2(path, corrupt_path)
        except Exception:
            pass

        if _write_safe(path, default):
            results.append(
                {
                    "file": filename,
                    "status": STATUS_REPAIRED,
                    "action": "reconstructed-default",
                    "detail": f"original error: {data}; corrupt saved to {corrupt_path.name}",
                }
            )
            return

    results.append(
        {
            "file": filename,
            "status": STATUS_FAILED,
            "action": "unrecoverable",
            "detail": f"no backup, no default, error: {data}",
        }
    )


def _check_journal(results: list, dry_run: bool):
    """Journal gets large — validate it separately with truncation detection."""
    path = MEMORY_DIR / "journal.json"
    filename = "journal.json"

    if not path.exists():
        results.append(
            {
                "file": filename,
                "status": STATUS_FAILED,
                "action": "missing",
                "detail": "journal.json not found",
            }
        )
        return

    text = path.read_text()
    if not text.strip():
        if not dry_run:
            _write_safe(path, [])
        results.append(
            {
                "file": filename,
                "status": STATUS_REPAIRED if not dry_run else STATUS_FAILED,
                "action": "empty-reset" if not dry_run else "would-reset",
                "detail": "journal was empty",
            }
        )
        return

    try:
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("journal must be a list")
        if not dry_run:
            _backup(path)
        results.append(
            {
                "file": filename,
                "status": STATUS_OK,
                "action": "backup-updated" if not dry_run else "valid",
                "detail": f"{len(data)} entries",
            }
        )
    except Exception as e:
        if dry_run:
            results.append(
                {
                    "file": filename,
                    "status": STATUS_FAILED,
                    "action": "would-repair",
                    "detail": f"invalid: {e}",
                }
            )
            return

        salvaged = _salvage_json_array(text)
        if salvaged is not None and len(salvaged) > 0:
            corrupt_path = path.with_suffix(".json.corrupt")
            try:
                shutil.copy2(path, corrupt_path)
            except Exception:
                pass
            if _write_safe(path, salvaged):
                results.append(
                    {
                        "file": filename,
                        "status": STATUS_REPAIRED,
                        "action": "truncation-salvaged",
                        "detail": f"kept {len(salvaged)} entries; corrupt saved",
                    }
                )
                return

        bak = path.with_suffix(".json.backup")
        try:
            shutil.copy2(path, bak)
        except Exception:
            pass
        _write_safe(path, [])
        results.append(
            {
                "file": filename,
                "status": STATUS_REPAIRED,
                "action": "reset-to-empty",
                "detail": f"corrupt saved to journal.json.backup; error: {e}",
            }
        )


def _normalize_statuses(results: list, dry_run: bool):
    """Normalize legacy 'in-progress' (hyphen) → 'in_progress' (underscore) and
    apply schema-key renames (cycles → cycle_status etc.). No-op once
    everything is already normalized.

    Acts on goal.json (status field), cycles.json (cycle_status after
    rename), and journal.json (cycle_status after rename).
    """
    targets = [
        ("goal.json", None, "status"),
        ("cycles.json", migrate_cycles_list, "cycle_status"),
        ("journal.json", migrate_journal_list, "cycle_status"),
    ]
    for filename, migrator, status_key in targets:
        path = MEMORY_DIR / filename
        if not path.exists():
            continue
        valid, data = _is_valid_json(path)
        if not valid or not isinstance(data, list):
            continue
        changed = False
        if migrator is not None:
            before = json.dumps(data, sort_keys=True)
            migrator(data)
            if json.dumps(data, sort_keys=True) != before:
                changed = True
        for entry in data:
            if isinstance(entry, dict) and entry.get(status_key) == "in-progress":
                entry[status_key] = "in_progress"
                changed = True
        if changed and not dry_run:
            _write_safe(path, data)
            results.append(
                {
                    "file": filename,
                    "status": STATUS_REPAIRED,
                    "action": "status-normalized",
                    "detail": "migrated keys/values to current schema",
                }
            )


# ── Public API ─────────────────────────────────────────────────────────────────


def run_repair(dry_run: bool = False, backup_only: bool = False) -> dict:
    """Run memory repair and return a summary dict.

    Safe to import and call programmatically — no sys.exit, no CLI output.

    Returns:
        {"ok": int, "repaired": int, "failed": int, "issues": list[str], "_results": list[dict]}
    """
    results: list = []

    _normalize_statuses(results, dry_run)
    for filename in DEFAULTS:
        _process_file(filename, results, dry_run, backup_only)
    _check_journal(results, dry_run)

    ok_count = sum(1 for r in results if r["status"] in (STATUS_OK, STATUS_BACKUP))
    repaired_count = sum(1 for r in results if r["status"] == STATUS_REPAIRED)
    failed_count = sum(1 for r in results if r["status"] == STATUS_FAILED)
    issues = [
        f"{r['action']}: {r['file']}" + (f" ({r['detail']})" if r.get("detail") else "")
        for r in results
        if r["status"] not in (STATUS_OK, STATUS_BACKUP)
    ]
    return {
        "ok": ok_count,
        "repaired": repaired_count,
        "failed": failed_count,
        "issues": issues,
        "_results": results,
    }


# ── CLI entry point ────────────────────────────────────────────────────────────


def main():
    dry_run = "--dry-run" in sys.argv
    backup_only = "--backup" in sys.argv
    quiet = "--quiet" in sys.argv
    json_output = "--json" in sys.argv

    summary = run_repair(dry_run=dry_run, backup_only=backup_only)
    results = summary["_results"]
    ok_count = summary["ok"]
    repaired_count = summary["repaired"]
    failed_count = summary["failed"]

    if json_output:
        print(
            json.dumps(
                {
                    "timestamp": _now(),
                    "dry_run": dry_run,
                    "backup_only": backup_only,
                    "ok": ok_count,
                    "repaired": repaired_count,
                    "failed": failed_count,
                    "results": results,
                },
                indent=2,
            )
        )
        sys.exit(1 if failed_count > 0 else 0)

    if quiet:
        parts = [f"{ok_count} ok"]
        if repaired_count:
            parts.append(f"{repaired_count} repaired")
        if failed_count:
            parts.append(f"{failed_count} FAILED")
        mode = " [dry-run]" if dry_run else (" [backup-only]" if backup_only else "")
        print(f"memory-repair{mode}: {', '.join(parts)}")
        sys.exit(1 if failed_count > 0 else 0)

    # Full output
    print("=== Memory Repair Check ===")
    print(f"Timestamp: {_now()}")
    if dry_run:
        print("Mode: DRY RUN (no writes)")
    elif backup_only:
        print("Mode: BACKUP ONLY")
    print()

    col_w = 30
    print(f"  {'FILE':<{col_w}} {'STATUS':<12} {'ACTION':<25} DETAIL")
    print(f"  {'-'*col_w} {'-'*12} {'-'*25} ------")
    for r in results:
        print(
            f"  {r['file']:<{col_w}} {r['status'].upper():<12} {r['action']:<25} {r['detail']}"
        )

    print()
    print(f"  OK: {ok_count}  |  Repaired: {repaired_count}  |  Failed: {failed_count}")
    print()
    if failed_count == 0:
        print("Result: ALL FILES HEALTHY")
    else:
        print(
            f"Result: {failed_count} FILE(S) UNRECOVERABLE — manual intervention needed"
        )

    sys.exit(1 if failed_count > 0 else 0)


if __name__ == "__main__":
    main()
