---
name: repair-memory-files
description: Detect and auto-repair corrupted agent memory files. Scans all critical JSON files, attempts repair from .backup copies or safe defaults, and creates fresh backups after successful repair. Use when cycle-start reports memory errors, when a JSON file is corrupted, or as a deep diagnostic with --dry-run to see what's wrong without making changes. Also run automatically by cycle_start.py on every startup.
---

# repair-memory-files

**Path:** `scripts/repair_memory_files.py`

Scans all critical JSON memory files for corruption and attempts repair from `.backup` copies or safe empty defaults.

## Arguments

| Flag | Description |
|------|-------------|
| _(none)_ | Scan and repair all memory files in place |
| `--dry-run` | Scan only — report issues without writing any fixes |
| `--backup` | Create `.backup` copies of all healthy files (no repair) |
| `--quiet` | Only print the summary line, suppress per-file output |
| `--json` | Machine-readable JSON output |

**Exit codes:** `0` = all OK, `1` = unrecoverable issues found

## Examples

```bash
# Scan and repair everything
uv run python scripts/repair_memory_files.py

# Diagnose without touching files
uv run python scripts/repair_memory_files.py --dry-run

# Create fresh .backup copies of all healthy files
uv run python scripts/repair_memory_files.py --backup

# Silent repair for scripting
uv run python scripts/repair_memory_files.py --quiet

# JSON output for automation
uv run python scripts/repair_memory_files.py --json
```
