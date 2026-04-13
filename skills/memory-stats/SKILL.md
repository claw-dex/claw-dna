---
name: memory-stats
description: Fast structured summary of all key memory files: state, goals, cycles, journal, inbox, and failures. Use when you need a quick snapshot of agent status without running a full cycle-start, when debugging state inconsistencies, or when you want machine-readable output with --json. Use --short for a single-line summary suitable for quick checks.
---

# memory-stats

**Path:** `scripts/memory_stats.py`

Reads all key memory files and prints a concise structured report. Faster than `cycle_start.py` — no memory repair, no inbox processing.

## Arguments

| Flag | Description |
|------|-------------|
| _(none)_ | Full structured summary (state, goals, cycles, journal, inbox, failures) |
| `--short` | Single-line summary (cycle number, status, counts) |
| `--json` | Machine-readable JSON output |

## Examples

```bash
# Full status report
uv run python scripts/memory_stats.py

# One-liner for a quick check
uv run python scripts/memory_stats.py --short

# JSON for scripting or automation
uv run python scripts/memory_stats.py --json
```
