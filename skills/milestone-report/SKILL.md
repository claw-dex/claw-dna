---
name: milestone-report
description: Generate a narrative milestone summary at significant cycle numbers (every 25 cycles). Reports scripts added, capabilities gained, cycle throughput, goal success rate, and category balance. Use when the agent reaches a milestone cycle, to produce a progress report to save to the workspace (--save), or to list all previous milestones (--list). Example: --cycle 100 --save to generate and persist the cycle-100 report.
---

# milestone-report

**Path:** `scripts/milestone-report.py`

Generates a narrative progress report covering scripts added, capabilities gained, throughput, goal success rate, and category balance. Designed for milestone cycles (every 25 cycles).

## Arguments

| Flag | Description |
|------|-------------|
| _(none)_ | Generate report for the current cycle number |
| `--cycle N` | Generate report for a specific cycle number |
| `--list` | List all previous milestone cycles with dates |
| `--save` | Write the report to `workspace/milestone-<N>.md` |
| `--json` | Output raw data as JSON |

## Examples

```bash
# Generate report for current cycle
uv run python scripts/milestone-report.py

# Generate the cycle-100 milestone report
uv run python scripts/milestone-report.py --cycle 100

# Generate and save it to workspace/
uv run python scripts/milestone-report.py --cycle 100 --save

# List all past milestones
uv run python scripts/milestone-report.py --list

# Raw JSON data
uv run python scripts/milestone-report.py --json
```
