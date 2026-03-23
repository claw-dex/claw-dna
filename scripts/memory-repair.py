#!/usr/bin/env python3
"""
memory-repair.py — Detect and auto-repair corrupted agent memory files.

Scans all critical JSON memory files, attempts repair from .backup copies
or safe defaults, and creates fresh .backup copies after successful repair.

Usage:
    python3 memory-repair.py              # scan + repair + report
    python3 memory-repair.py --dry-run    # scan only, no writes
    python3 memory-repair.py --backup     # only create backups (no repair)
    python3 memory-repair.py --quiet      # only print summary line
    python3 memory-repair.py --json       # output machine-readable JSON

Exit code: 0 = all ok (or repaired), 1 = unrecoverable issues remain.
"""

import json
import shutil
import sys
import datetime
from pathlib import Path

MEMORY_DIR = Path("/agent/memory")
AGENT_DIR = Path("/agent")

DRY_RUN = "--dry-run" in sys.argv
BACKUP_ONLY = "--backup" in sys.argv
QUIET = "--quiet" in sys.argv
JSON_OUTPUT = "--json" in sys.argv

# ── Defaults for reconstruction ────────────────────────────────────────────────

def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

DEFAULTS = {
    "state.json": {
        "cycle_number": 1,
        "status": "idle",
        "current_goal": None,
        "last_cycle_summary": "Reconstructed by memory-repair.py",
        "created_at": _now(),
        "last_heartbeat": _now(),
        "last_cycle_run": _now(),
        "last_cycle_end": None,
        "services": {}
    },
    "cycles.json": [],
    "goal.json": [],
    "journal.json": [],
    "command_history.json": [],
}

# ── Result tracking ────────────────────────────────────────────────────────────

results = []   # list of {"file", "status", "action", "detail"}

STATUS_OK      = "ok"
STATUS_REPAIRED = "repaired"
STATUS_FAILED  = "failed"
STATUS_BACKUP  = "backed-up"

def _record(file: str, status: str, action: str = "", detail: str = ""):
    results.append({"file": file, "status": status, "action": action, "detail": detail})


# ── Core helpers ───────────────────────────────────────────────────────────────

def _is_valid_json(path: Path) -> tuple[bool, object]:
    """Returns (valid, parsed_value). parsed_value is None on failure."""
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


# ── Per-file processing ────────────────────────────────────────────────────────

def process_file(filename: str):
    path = MEMORY_DIR / filename
    default = DEFAULTS.get(filename)

    # Missing file
    if not path.exists():
        if DRY_RUN or BACKUP_ONLY:
            _record(filename, STATUS_FAILED, "would-create", "file missing")
            return
        if default is not None:
            if _write_safe(path, default):
                _record(filename, STATUS_REPAIRED, "created-default", "file was missing")
            else:
                _record(filename, STATUS_FAILED, "create-failed", "could not write default")
        else:
            _record(filename, STATUS_FAILED, "no-default", "file missing, no default known")
        return

    # BACKUP_ONLY mode — just create backups
    if BACKUP_ONLY:
        valid, _ = _is_valid_json(path)
        if valid:
            _backup(path)
            _record(filename, STATUS_BACKUP, "backup-created", "")
        else:
            _record(filename, STATUS_FAILED, "skip-backup", "invalid JSON, backup skipped")
        return

    # Validate JSON
    valid, data = _is_valid_json(path)
    if valid:
        # Good — create/refresh backup
        if not DRY_RUN:
            _backup(path)
        _record(filename, STATUS_OK, "backup-updated" if not DRY_RUN else "valid", "")
        return

    # Invalid JSON — attempt repair
    if DRY_RUN:
        _record(filename, STATUS_FAILED, "would-repair", f"invalid JSON: {data}")
        return

    # Step 1: try .backup
    restored, bak_data = _restore_from_backup(path)
    if restored:
        if _write_safe(path, bak_data):
            _record(filename, STATUS_REPAIRED, "restored-from-backup",
                    f"original error: {data}")
            return
        _record(filename, STATUS_FAILED, "backup-write-failed",
                f"backup read ok but write failed: {data}")
        return

    # Step 2: try default
    if default is not None:
        # Back up the corrupt file before overwriting
        corrupt_path = path.with_suffix(path.suffix + ".corrupt")
        try:
            shutil.copy2(path, corrupt_path)
        except Exception:
            pass

        if _write_safe(path, default):
            _record(filename, STATUS_REPAIRED, "reconstructed-default",
                    f"original error: {data}; corrupt saved to {corrupt_path.name}")
            return

    _record(filename, STATUS_FAILED, "unrecoverable",
            f"no backup, no default, error: {data}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    for filename in DEFAULTS:
        process_file(filename)

    # Also check journal.json (special: may be large, truncation is common)
    _check_journal()

    # Summary
    ok_count      = sum(1 for r in results if r["status"] in (STATUS_OK, STATUS_BACKUP))
    repaired_count = sum(1 for r in results if r["status"] == STATUS_REPAIRED)
    failed_count  = sum(1 for r in results if r["status"] == STATUS_FAILED)

    if JSON_OUTPUT:
        print(json.dumps({
            "timestamp": _now(),
            "dry_run": DRY_RUN,
            "backup_only": BACKUP_ONLY,
            "ok": ok_count,
            "repaired": repaired_count,
            "failed": failed_count,
            "results": results
        }, indent=2))
        sys.exit(1 if failed_count > 0 else 0)

    if QUIET:
        parts = [f"{ok_count} ok"]
        if repaired_count:
            parts.append(f"{repaired_count} repaired")
        if failed_count:
            parts.append(f"{failed_count} FAILED")
        mode = " [dry-run]" if DRY_RUN else (" [backup-only]" if BACKUP_ONLY else "")
        print(f"memory-repair{mode}: {', '.join(parts)}")
        sys.exit(1 if failed_count > 0 else 0)

    # Full output
    print("=== Memory Repair Check ===")
    print(f"Timestamp: {_now()}")
    if DRY_RUN:
        print("Mode: DRY RUN (no writes)")
    elif BACKUP_ONLY:
        print("Mode: BACKUP ONLY")
    print()

    col_w = 30
    print(f"  {'FILE':<{col_w}} {'STATUS':<12} {'ACTION':<25} DETAIL")
    print(f"  {'-'*col_w} {'-'*12} {'-'*25} ------")
    for r in results:
        status_colored = r["status"].upper()
        print(f"  {r['file']:<{col_w}} {status_colored:<12} {r['action']:<25} {r['detail']}")

    print()
    print(f"  OK: {ok_count}  |  Repaired: {repaired_count}  |  Failed: {failed_count}")
    print()
    if failed_count == 0:
        print("Result: ALL FILES HEALTHY")
    else:
        print(f"Result: {failed_count} FILE(S) UNRECOVERABLE — manual intervention needed")

    sys.exit(1 if failed_count > 0 else 0)


def _check_journal():
    """Journal gets large — validate it separately with truncation detection."""
    path = MEMORY_DIR / "journal.json"
    filename = "journal.json"

    if not path.exists():
        _record(filename, STATUS_FAILED, "missing", "journal.json not found")
        return

    text = path.read_text()
    if not text.strip():
        if not DRY_RUN:
            _write_safe(path, [])
        _record(filename, STATUS_REPAIRED if not DRY_RUN else STATUS_FAILED,
                "empty-reset" if not DRY_RUN else "would-reset", "journal was empty")
        return

    try:
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("journal must be a list")
        if not DRY_RUN and not BACKUP_ONLY:
            _backup(path)
        _record(filename, STATUS_OK, "backup-updated" if not DRY_RUN else "valid",
                f"{len(data)} entries")
    except Exception as e:
        if DRY_RUN:
            _record(filename, STATUS_FAILED, "would-repair", f"invalid: {e}")
            return

        # Try to salvage entries from truncated JSON
        salvaged = _salvage_json_array(text)
        if salvaged is not None and len(salvaged) > 0:
            corrupt_path = path.with_suffix(".json.corrupt")
            try:
                shutil.copy2(path, corrupt_path)
            except Exception:
                pass
            if _write_safe(path, salvaged):
                _record(filename, STATUS_REPAIRED, "truncation-salvaged",
                        f"kept {len(salvaged)} entries; corrupt saved")
                return

        # Last resort: backup + empty list
        bak = path.with_suffix(".json.backup")
        try:
            shutil.copy2(path, bak)
        except Exception:
            pass
        _write_safe(path, [])
        _record(filename, STATUS_REPAIRED, "reset-to-empty",
                f"corrupt saved to journal.json.backup; error: {e}")


def _salvage_json_array(text: str) -> list | None:
    """
    Try to recover a truncated JSON array by finding the last valid
    complete entry and closing the array. Returns list or None.
    """
    # Quick check: does it at least start with '['?
    stripped = text.strip()
    if not stripped.startswith("["):
        return None

    # Binary search for the largest valid prefix that is a complete JSON array
    # Strategy: find the last '}' and try closing the array there
    last_close = stripped.rfind("}")
    if last_close == -1:
        return None

    # Try appending ']' after the last '}'
    candidate = stripped[:last_close + 1] + "]"
    try:
        data = json.loads(candidate)
        if isinstance(data, list):
            return data
    except Exception:
        pass

    # Try stripping trailing comma before ']'
    candidate2 = stripped[:last_close + 1].rstrip().rstrip(",") + "]"
    try:
        data = json.loads(candidate2)
        if isinstance(data, list):
            return data
    except Exception:
        pass

    return None


if __name__ == "__main__":
    main()
