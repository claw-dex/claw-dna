---
name: memory-recall
description: Query the agent's long-term semantic memory (memvid). Use when the agent needs to recall past cycle experiences, search for what was done previously, find related past work, or answer questions about historical agent activity. Triggers on questions like "what did I do about X?", "have I worked on X before?", "recall past work on X", "search memory for X", or when the agent needs historical context to inform a current decision. Also use for browsing memory timeline chronologically.
---

# memory-recall

**Path:** `scripts/memory-recall.py`

Queries long-term semantic memory stored in `.mv2` files using the `memvid` CLI for hybrid lexical + semantic search. Defaults to `/agent/memory/long_term_memory.mv2` but supports querying any `.mv2` file via `--mv2`.

The default `.mv2` file is populated automatically by `cycle-close.py` at the end of every cycle. Each entry contains the cycle's goal, summary, actions, category, and learnings.

## Arguments

| Flag | Description |
|------|-------------|
| `QUESTION` | Natural-language query (first positional argument) |
| `--mv2 PATH` | Path to the `.mv2` file (default: `/agent/memory/long_term_memory.mv2`) |
| `--k N` | Number of results to return (default: 5) |
| `--json` | Output as JSON instead of formatted text |
| `--timeline` | Show timeline entries instead of semantic search |
| `--since DATE` | Filter entries since DATE (ISO format or unix timestamp) |
| `--until DATE` | Filter entries until DATE (ISO format or unix timestamp) |

**Exit codes:** `0` = success, `1` = error (missing args, file not found, CLI error)

## Examples

```bash
# Semantic search — find past work related to a topic
uv run python scripts/memory-recall.py "portal reliability fixes"

# Get more results
uv run python scripts/memory-recall.py "efficiency improvements" --k 10

# JSON output for programmatic use
uv run python scripts/memory-recall.py "what scheduler changes were made?" --json

# Query a different .mv2 file
uv run python scripts/memory-recall.py "auth changes" --mv2 /agent/memory/project_notes.mv2

# Only entries older than a specific date
uv run python scripts/memory-recall.py "reliability" --until 2026-03-20

# Only entries within a date range
uv run python scripts/memory-recall.py "efficiency" --since 2026-03-01 --until 2026-03-15

# Browse recent timeline
uv run python scripts/memory-recall.py --timeline

# Timeline since a specific date
uv run python scripts/memory-recall.py --timeline --since 2026-03-01
```

## How it works

- **Semantic search** (default): Runs `memvid find` CLI with hybrid lexical + semantic search. Supports `--since` and `--until` for time-range filtering. Results are sorted by score descending. Internal memvid metadata lines are stripped from snippets.
- **Timeline mode** (`--timeline`): Runs `memvid timeline` CLI to list entries chronologically. Supports `--since` and `--until` for date range filtering.

## Integration with cycle scripts

- **cycle-close.py** stores each journal entry into the `.mv2` file via `memory-ingest.py --append-json` at the end of each cycle.
- **cycle-start.py** shells out to this script (`memory-recall.py --until <24h_ago> --k 50 --json`) to recall up to 50 memories older than 24h and includes them in the `[LONG-TERM MEMORY]` briefing section.
- This script provides on-demand access to the same memory store (or any other `.mv2` file via `--mv2`).
