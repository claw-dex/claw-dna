---
name: memory-recall
description: Query the agent's long-term semantic memory (LanceDB). Use when the agent needs to recall past cycle experiences, search for what was done previously, find related past work, or answer questions about historical agent activity. Triggers on questions like "what did I do about X?", "have I worked on X before?", "recall past work on X", "search memory for X", or when the agent needs historical context to inform a current decision. Also use for browsing memory timeline chronologically.
---

# memory-recall

**Path:** `scripts/memory_recall.py`

Queries the long-term memory store with hybrid retrieval: BM25 full-text search fused with bge-small vector similarity, merged by reciprocal-rank fusion. Defaults to `/agent/memory/long_term_memory.lancedb` but can query any LanceDB store built by `memory_ingest.py` via `--db`.

The default store is populated automatically by `cycle_close.py` at the end of every cycle. Each entry contains the cycle's goal, summary, actions, and category.

## Arguments

| Flag | Description |
|------|-------------|
| `QUESTION` | Natural-language query (first positional argument) |
| `--db PATH` | Path to the LanceDB store (default: `/agent/memory/long_term_memory.lancedb`) |
| `--k N` | Number of results to return (default: 5) |
| `--min-score F` | Drop results scoring below F of the top hit, 0–1 (default: 0 = keep all) |
| `--json` | Output as JSON instead of formatted text |
| `--timeline` | Show timeline entries instead of semantic search |
| `--since DATE` | Filter entries since DATE (ISO format or unix timestamp) |
| `--until DATE` | Filter entries until DATE (ISO format or unix timestamp) |

**Exit codes:** `0` = success, `1` = error (missing args, store not found, query error)

## Examples

```bash
# Semantic search — find past work related to a topic
uv run python scripts/memory_recall.py "portal reliability fixes"

# Get more results
uv run python scripts/memory_recall.py "efficiency improvements" --k 10

# Only strong matches (at least half the top hit's score)
uv run python scripts/memory_recall.py "auth changes" --k 10 --min-score 0.5

# JSON output for programmatic use
uv run python scripts/memory_recall.py "what scheduler changes were made?" --json

# Query a different store
uv run python scripts/memory_recall.py "auth changes" --db /agent/memory/project_notes.lancedb

# Only entries older than a specific date
uv run python scripts/memory_recall.py "reliability" --until 2026-03-20

# Only entries within a date range
uv run python scripts/memory_recall.py "efficiency" --since 2026-03-01 --until 2026-03-15

# Browse recent timeline
uv run python scripts/memory_recall.py --timeline

# Timeline since a specific date
uv run python scripts/memory_recall.py --timeline --since 2026-03-01
```

## How it works

- **Semantic search** (default): embeds the query with `BAAI/bge-small-en-v1.5`, runs a hybrid LanceDB query (vector + BM25) and fuses the two rankings. `--since` / `--until` become a SQL predicate on the row's timestamp, applied *before* retrieval so filtering never silently shrinks the result set below `k`. If the full-text index hasn't been built yet, the query degrades to vector-only instead of failing.
- **Scoring**: hybrid fusion scores have no absolute meaning — their magnitude depends on how many candidates were merged. Results are therefore normalised against the top hit, so `score` is always 1.0 for the best match and `--min-score` reads as "at least this fraction of the best match".
- **Timeline mode** (`--timeline`): one scan ordered newest-first, filtered by `--since` / `--until`. Rows already carry title, tags, and text, so no per-entry lookups are needed.

## Integration with cycle scripts

- **cycle_close.py** buffers each cycle's journal entry and inbox messages and writes them to the store in one batch at the end of the cycle.
- **cycle_start.py** imports `recall()` from this script directly (one query per inbox message plus the active goal, run concurrently), dedupes by row id, and includes the merged results in the `[LONG-TERM MEMORY]` briefing section.
- This script provides on-demand access to the same memory store (or any other store via `--db`).
