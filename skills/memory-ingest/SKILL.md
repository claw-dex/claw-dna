---
name: memory-ingest
description: Ingest agent memory into long-term semantic store (memvid CLI). Use when the agent needs to rebuild the .mv2 memory index from scratch, bulk-ingest all journal/cycle/goal data into semantic memory, append a single journal entry, ingest raw text or files directly into the memory store. Triggers on "rebuild memory index", "ingest memory", "reindex long-term memory", "rebuild mv2", "bulk import memory", "remember this text", "ingest this file", or when the .mv2 file is missing/corrupt and needs to be recreated. Also use after manual journal edits to re-sync the semantic index.
---

# memory-ingest

**Path:** `scripts/memory-ingest.py`

Parses `journal.json`, `cycles.json`, and `goal.json`, chunks them into semantically meaningful pieces, and ingests into a `.mv2` index via the `memvid` CLI with `bge-base` embeddings for high-quality hybrid lexical + semantic search.

The `.mv2` file is used by `memory-recall.py` for on-demand queries and by `cycle-start.py` for automatic long-term memory briefing.

## Arguments

| Flag | Description |
|------|-------------|
| `--build` | Full rebuild: backs up the existing `.mv2` (as `.mv2.backup`), then recreates the index from scratch |
| `--append-json JSON` | Append a single entry — journal, cycle, or goal (inline JSON string or `@file.json` path) |
| `--append-text TEXT` | Ingest raw text directly (with optional `--title` and `--tags`) |
| `--append-file PATH` | Ingest a file directly — PDF, DOCX, TXT, MD, etc. (with optional `--title` and `--tags`) |
| `--mv2 PATH` | Path to the `.mv2` file (default: `/agent/memory/long_term_memory.mv2`) |
| `--memory PATH` | Path to the memory directory (default: `/agent/memory`) |
| `--title TEXT` | Title for `--append-text` / `--append-file` entries (default: auto-generated) |
| `--tags TAG…` | Tags for `--append-text` / `--append-file` (space-separated, each in quotes) |
| `--dry-run` | Preview chunks without writing |
| `--json` | Output results as JSON |
| `--quiet` | Suppress progress output |

**Exit codes:** `0` = success, `1` = error

## Examples

```bash
# Full rebuild of the semantic memory index
uv run python scripts/memory-ingest.py --build

# Preview what would be ingested
uv run python scripts/memory-ingest.py --build --dry-run

# Append a single journal entry (JSON) after cycle close
uv run python scripts/memory-ingest.py --append-json '{"cycle": 42, "summary": "Fixed portal auth", "type": "goal", "status": "completed", "actions": ["Patched /agent/workspace/auth_fix.pdf"]}'

# Append from a JSON file
uv run python scripts/memory-ingest.py --append-json @/tmp/journal_entry.json

# Ingest raw text directly
uv run python scripts/memory-ingest.py --append-text "The deploy pipeline requires approval from two reviewers before merging to main"

# Ingest text with a custom title and tags
uv run python scripts/memory-ingest.py --append-text "Auth tokens expire after 24h in production" --title "Auth token TTL" --tags "auth" "production"

# Ingest a file directly
uv run python scripts/memory-ingest.py --append-file /agent/workspace/architecture-overview.pdf

# Ingest a file with tags
uv run python scripts/memory-ingest.py --append-file /agent/workspace/meeting-notes.md --tags "meeting" "planning"

# JSON output for scripting
uv run python scripts/memory-ingest.py --build --json --quiet
```

## How it works

### Data sources

The script reads three memory files and chunks each into semantically meaningful pieces:

- **journal.json** — Richest source: goal, summary, actions, category, outcome, and learnings per cycle. Each entry becomes one chunk.
- **cycles.json** — Structured cycle records with type, category, status, duration, and timestamps. Each cycle becomes one chunk.
- **goal.json** — Goal records with content and status. Each goal becomes one chunk.

### Chunking

Each chunk includes:
- **title** — Human-readable identifier (e.g., "Cycle 42: Fixed portal auth")
- **label** — Category label for grouping (e.g., "goal", "cycle", "evolve")
- **text** — Full semantic content for embedding
- **tags** — Structured tags for filtering (e.g., `type:evolve`, `category:efficiency`, `cycle:42`, `date:2026-03-25`)
- **metadata** — JSON metadata for retrieval context

### Embedding model

Uses `bge-base` via the `memvid` CLI for higher quality embeddings compared to `bge-small`. No Python SDK or fastembed dependency required — runs entirely through the CLI.

### Modes

- **`--build`** deletes the existing `.mv2` and recreates it from all memory files. Use after manual edits, corruption, or when the index needs a full refresh.
- **`--append-json`** adds a single structured entry (journal, cycle, or goal) to the existing `.mv2` without rebuilding. Use for incremental updates (e.g., at end of each cycle). Auto-detects entry type.
- **`--append-text`** ingests raw text directly. Use for ad-hoc notes, facts, or context that doesn't fit the journal/cycle/goal schema.
- **`--append-file`** ingests a file directly. Use for documents, reports, or other files the agent should be able to recall.

## Integration with cycle scripts

- **cycle-close.py** calls `memory-ingest.py --append-json` at the end of each cycle to store the journal entry into long-term memory.
- **memory-recall.py** queries the same `.mv2` file produced by this script.
- **cycle-start.py** reads the `.mv2` file for the `[LONG-TERM MEMORY]` briefing section.
