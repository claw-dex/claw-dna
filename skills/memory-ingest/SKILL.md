---
name: memory-ingest
description: Ingest agent memory into the long-term semantic store (LanceDB). Use when the agent needs to rebuild the memory index from scratch, bulk-ingest all journal and inbox history into semantic memory, append a single journal entry, or ingest raw text or files directly into the memory store. Triggers on "rebuild memory index", "ingest memory", "reindex long-term memory", "rebuild the lancedb store", "bulk import memory", "remember this text", "ingest this file", or when long_term_memory.lancedb is missing/corrupt and needs to be recreated. Also use after manual journal edits to re-sync the semantic index.
---

# memory-ingest

**Path:** `scripts/memory_ingest.py`

Parses `journal.json`, `journal_archive.json`, and `messages/inbox_history.json`, chunks them into semantically meaningful pieces, and writes them into a LanceDB store for hybrid BM25 + semantic search. Embeddings are produced locally by fastembed with `BAAI/bge-small-en-v1.5` (384-dim) — no API key and no network after the model is cached.

The store at `/agent/memory/long_term_memory.lancedb/` is queried by `memory_recall.py` on demand and by `cycle_start.py` for the automatic long-term memory briefing.

## Arguments

| Flag | Description |
|------|-------------|
| `--build` | Full rebuild into a `.rebuild` staging directory, then an atomic swap (the previous store is kept as `.backup`) |
| `--append-json JSON` | Append a single entry — journal, cycle, or goal (inline JSON string or `@file.json` path) |
| `--append-text TEXT` | Ingest raw text directly (with optional `--title` and `--tags`) |
| `--append-file PATH` | Ingest a text file directly (with optional `--title` and `--tags`) |
| `--db PATH` | Path to the LanceDB store (default: `/agent/memory/long_term_memory.lancedb`) |
| `--memory PATH` | Path to the memory directory (default: `/agent/memory`) |
| `--title TEXT` | Title for `--append-text` / `--append-file` entries (default: auto-generated) |
| `--tags TAG…` | Tags for `--append-text` / `--append-file` (space-separated, each in quotes) |
| `--dry-run` | Preview chunks without writing |
| `--json` | Output results as JSON |
| `--quiet` | Suppress progress output |

**Exit codes:** `0` = success, `1` = error

## Examples

```bash
# Full rebuild of the semantic memory store
uv run python scripts/memory_ingest.py --build

# Preview what would be ingested
uv run python scripts/memory_ingest.py --build --dry-run

# Append a single journal entry (JSON) after cycle close
uv run python scripts/memory_ingest.py --append-json '{"cycle_number": 42, "summary": "Fixed portal auth", "cycle_type": "goal", "cycle_status": "completed", "actions": ["Patched app/auth.py"]}'

# Append from a JSON file
uv run python scripts/memory_ingest.py --append-json @/tmp/journal_entry.json

# Ingest raw text directly
uv run python scripts/memory_ingest.py --append-text "The deploy pipeline requires approval from two reviewers before merging to main"

# Ingest text with a custom title and tags
uv run python scripts/memory_ingest.py --append-text "Auth tokens expire after 24h in production" --title "Auth token TTL" --tags "auth" "production"

# Ingest a text file directly
uv run python scripts/memory_ingest.py --append-file /agent/workspace/architecture-overview.md

# Ingest a file with tags
uv run python scripts/memory_ingest.py --append-file /agent/workspace/meeting-notes.md --tags "meeting" "planning"

# JSON output for scripting
uv run python scripts/memory_ingest.py --build --json --quiet
```

## How it works

### Data sources

`--build` reads three files, in this order:

- **journal.json** — Richest source: goal, summary, actions, category per cycle. Each entry becomes one chunk.
- **journal_archive.json** — Historical journal entries, newest-first ordering preserved.
- **messages/inbox_history.json** — Archived inbox messages (lives in the sibling `messages/` directory).

`cycles.json` and `goal.json` are deliberately **not** ingested — cycle metadata is redundant with journal entries, which already carry richer semantic content.

### Chunking

One source record becomes one chunk. Each chunk carries:
- **title** — Human-readable identifier (e.g., "Cycle 42: Fixed portal auth")
- **label** — Category label for grouping (e.g., "evolve", "inbox", "goal")
- **text** — Full semantic content, embedded and stored verbatim
- **tags** — Structured tags for filtering (e.g., `type:evolve`, `category:efficiency`, `cycle:42`, `date:2026-03-25`)
- **metadata** — Free-form dict preserved as JSON alongside the row

### Storage

`/agent/memory/long_term_memory.lancedb/` is a LanceDB database directory holding a single `memories` table. Filterable fields (`source`, `cycle`, `date`, `ts`, `label`) are real columns so time-range and source filters run as SQL; the rest of the metadata stays in a JSON column. A BM25 full-text index covers `text`; the vector index is only built once the table passes 5,000 rows, below which an exact brute-force scan is faster.

### Keeping the store small

LanceDB is copy-on-write: every write creates a new table version and new files, and the superseded files stay on disk until they are pruned. Unmanaged, that dominates the actual data — 300 cycles of small appends measured at 906 files / 6.6 MB for 600 rows.

Two things keep it in check:

- **Batching.** A cycle's inbox messages and journal entry are buffered and written as one batch, not one row at a time. Long documents are likewise split and written in a single call.
- **Compaction.** After roughly every 20 writes, the next write merges small fragments and prunes superseded versions (`table.optimize(cleanup_older_than=...)`). The same 300 cycles then measure 349 files / 2.8 MB, with bytes-per-row flat instead of climbing.

Compaction is also rate-limited to once per 30 minutes. That floor matters: `optimize()` can only prune files older than its retention window, so a pass that runs too soon rewrites every fragment and keeps *both* copies. Compacting on every write measured **10x worse than doing nothing** (11.3 MB vs 1.0 MB over 100 cycles). If you tune `COMPACT_RETENTION` or `COMPACT_MIN_INTERVAL` in `scripts/memory_store.py`, keep retention well below the interval.

`--build` sidesteps all of this: it writes a fresh store and prunes it completely, which is the cheapest way to reclaim a store that has drifted.

### Embedding model

`BAAI/bge-small-en-v1.5` (384-dim) via fastembed, running locally on ONNX — no PyTorch, no API key. The weights (~130 MB) download once and are cached under `/agent/.cache/fastembed`. `seed/install_memory_deps.sh` pre-warms that cache so the first ingest doesn't pay for it mid-cycle.

### Idempotency

Every chunk gets a deterministic id derived from its source, identifiers, and full text. Appending an unchanged record replaces its existing row instead of adding a duplicate, so re-running an ingest is safe.

### Modes

- **`--build`** rebuilds into `long_term_memory.lancedb.rebuild/`, then atomically swaps it in and keeps the old store as `.backup`. The existing store stays queryable for the whole rebuild, and a crash leaves it untouched. Use after manual edits, corruption, or when the index needs a full refresh.
- **`--append-json`** adds a single structured entry (journal, cycle, or goal) without rebuilding. Auto-detects entry type.
- **`--append-text`** ingests raw text. Use for ad-hoc notes or facts that don't fit the journal schema.
- **`--append-file`** ingests a **text** file: `.txt .md .markdown .rst .html .htm .json .jsonl .csv .tsv .log .yaml .yml .toml .ini .cfg .py .sh .sql`, up to 5 MB. Binary formats (PDF, DOCX, XLSX, PPTX, images, audio, video) are rejected with a clear error — convert them to text first, then ingest the result.

### Long documents

The embedding model truncates its input at 512 tokens (~2 KB of English), so a long document stored as a single row would only be semantically searchable by its opening paragraphs. `--append-text` and `--append-file` therefore split anything longer into ~1500-character overlapping pieces, one row each, titled `<title> [i/n]` and tagged `part:i/n`. Journal and inbox entries are far shorter than the threshold and are never split. The `--json` output reports the piece count as `parts`.

## Integration with cycle scripts

- **cycle_close.py** buffers this cycle's inbox messages and journal entry and flushes them in one batched write at the end of the cycle. On first run, when the store doesn't exist yet, it runs a one-shot `--build` instead.
- **memory_recall.py** queries the same store produced by this script.
- **cycle_start.py** reads the store for the `[LONG-TERM MEMORY]` briefing section.
- **memory_inspect.py** reports on the store's size, row counts, and index health.
