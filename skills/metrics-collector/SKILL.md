---
name: metrics-collector
description: Collect and store time-series performance metrics for the agent. Measures portal response time, CPU, memory, and disk usage, appending snapshots to metrics.json (ring buffer, max 200 entries). Use to capture a baseline snapshot (default), view a report of recent snapshots (--report), detect performance regressions (--regressions), or get a one-line average summary (--summary).
---

# metrics-collector

**Path:** `scripts/metrics_collector.py`

Collects time-series performance snapshots (portal latency, CPU, RAM, disk) and stores them in `/agent/memory/metrics.json` (ring buffer, max 200 entries).

## Arguments

| Flag | Description |
|------|-------------|
| _(none)_ | Capture a new metrics snapshot and append to metrics.json |
| `--report` | Print the last N snapshots as a table |
| `--json` | Print the last snapshot as JSON |
| `--summary` | One-line average summary (CPU%, RAM%, disk%, portal latency) |
| `--regressions` | Detect performance regressions vs. historical baseline |
| `--limit N` | Used with `--report`: show last N snapshots (default: 10) |
| `--max N` | Ring buffer size (default: 200) |

## Examples

```bash
# Capture a snapshot
uv run python scripts/metrics_collector.py

# View last 10 snapshots as a table
uv run python scripts/metrics_collector.py --report

# View last 25 snapshots
uv run python scripts/metrics_collector.py --report --limit 25

# One-line average summary
uv run python scripts/metrics_collector.py --summary

# Check for performance regressions
uv run python scripts/metrics_collector.py --regressions

# Latest snapshot as JSON
uv run python scripts/metrics_collector.py --json
```
