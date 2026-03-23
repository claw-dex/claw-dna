---
name: memory-backup
description: Timestamped snapshot backups of all critical memory files. Creates atomic versioned backups in /agent/memory/backups/. Use before risky changes to memory files, to restore after corruption (--restore latest), to list available backups (--list), or to prune old snapshots (--prune 5). Also run automatically by cycle-close.py when the last backup is older than 1 hour.
---

# memory-backup

**Path:** `scripts/memory-backup.py`

Creates and manages timestamped snapshots of all critical memory files in `/agent/memory/backups/<timestamp>/`.

## Arguments

| Flag | Description |
|------|-------------|
| _(none)_ | Create a new backup snapshot now |
| `--list` | List all available backups with timestamps and sizes |
| `--check` | Show the age of the most recent backup |
| `--restore TS` | Restore from a specific backup (timestamp string or `latest`) |
| `--prune N` | Delete all but the N most recent backups (default: 5) |
| `--dry-run` | Preview what a restore would do without writing |
| `--label TEXT` | Attach a label to the new backup (e.g., `pre-deploy`) |
| `--json` | Output as JSON |
| `--quiet` | Suppress non-essential output |

**Exit codes:** `0` = success, `1` = error, `2` = no backups found

## Examples

```bash
# Create a snapshot right now
uv run python scripts/memory-backup.py

# Create a labeled snapshot before a risky change
uv run python scripts/memory-backup.py --label pre-refactor

# List all backups
uv run python scripts/memory-backup.py --list

# Check when the last backup was taken
uv run python scripts/memory-backup.py --check

# Restore the most recent backup
uv run python scripts/memory-backup.py --restore latest

# Restore a specific backup by timestamp
uv run python scripts/memory-backup.py --restore 2024-01-15T10-30-00

# Keep only the 5 most recent backups
uv run python scripts/memory-backup.py --prune 5
```
