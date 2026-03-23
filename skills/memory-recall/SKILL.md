---
name: memory-recall
description: Query the agent's long-term semantic memory (memvid). Use when the agent needs to recall past cycle experiences, search for what was done previously, find related past work, or answer questions about historical agent activity. Triggers on questions like "what did I do about X?", "have I worked on X before?", "recall past work on X", "search memory for X", or when the agent needs historical context to inform a current decision. Also use for browsing memory timeline chronologically.
---

# memory-recall

**Path:** `scripts/memory-recall.py`

Queries the agent's long-term semantic memory stored in `/agent/memory/long_term_memory.mv2`. Uses local embeddings (bge-small) for semantic search — no API keys needed.

The `.mv2` file is populated automatically by `cycle-close.py` at the end of every cycle. Each entry contains the cycle's goal, summary, actions, category, and learnings.

## Arguments

| Flag | Description |
|------|-------------|
| `QUESTION` | Natural-language query (first positional argument) |
| `--k N` | Number of results to return (default: 5) |
| `--json` | Output as JSON instead of formatted text |
| `--timeline` | Show timeline entries instead of semantic search |
| `--since DATE` | Filter timeline entries since DATE (ISO format) |

**Exit codes:** `0` = success, `1` = error (missing args, file not found, import error)

## Examples

```bash
# Semantic search — find past work related to a topic
uv run python scripts/memory-recall.py "portal reliability fixes"

# Get more results
uv run python scripts/memory-recall.py "efficiency improvements" --k 10

# JSON output for programmatic use
uv run python scripts/memory-recall.py "what scheduler changes were made?" --json

# Browse recent timeline
uv run python scripts/memory-recall.py --timeline

# Timeline since a specific date
uv run python scripts/memory-recall.py --timeline --since 2026-03-01
```

## How it works

- **Semantic search** (default): Uses `mem.ask(question, context_only=True, k=N)` to find entries whose embedded text is semantically similar to the question. Returns context snippets without invoking an LLM.
- **Timeline mode** (`--timeline`): Uses `mem.timeline(limit=N)` to list entries chronologically. Useful for browsing recent history or filtering by date range.

## Integration with cycle scripts

- **cycle-close.py** stores each journal entry into the `.mv2` file after closing a cycle (step 9).
- **cycle-start.py** automatically recalls up to 10 memories older than 24h and includes them in the `[LONG-TERM MEMORY]` briefing section.
- This script provides on-demand access to the same memory store.
