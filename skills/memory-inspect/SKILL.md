---
name: memory-inspect
description: Inspect the long-term semantic memory store (LanceDB) for on-disk size, row counts, index health, duplicate rows, and content distribution. Also verifies the stored embedding dimension matches the configured model. Use when the store is growing unexpectedly fast, when diagnosing storage bloat, when comparing the indexed row count against the source JSON files (journal, journal_archive, inbox_history), when verifying the vector index is aligned with the current embedding model, or when deciding whether to rebuild via `memory-ingest`. Triggers on "inspect the memory store", "why is long_term_memory.lancedb so big", "memory store growth", "memory stats", "check memory size", "check embedding dimension", "is the memory index healthy", or any disk-usage or index-health investigation of the agent's long-term memory.
---

# memory-inspect

**Path:** `scripts/memory_inspect.py`

Read-only diagnostic tool for the agent's long-term memory store
(`/agent/memory/long_term_memory.lancedb/`). Reports on-disk footprint, row
counts, index health, embedding-dimension alignment, source/label/tag
distribution, and timestamp span — and compares the indexed counts against the
source JSON files (`journal.json`, `journal_archive.json`,
`messages/inbox_history.json`, `messages/inbox.json`).

Two things this catches that nothing else does: a store built with a different
embedding model than the one queries now use (every semantic result is
meaningless until rebuilt), and a store whose disk footprint has drifted far
from its content size because many small appends left behind many small
fragments.

## Arguments

| Flag | Description |
|------|-------------|
| `--db PATH` | Path to the LanceDB store (default: `/agent/memory/long_term_memory.lancedb`). Pass alone to inspect any store — `--memory` auto-derives from its parent. |
| `--memory PATH` | Path to the memory directory holding the source JSON files. Defaults to the parent directory of `--db`; only set this when the source JSON lives elsewhere. |
| `--limit N` | Max rows to scan (default: 200000) |
| `--top-tags N` | How many top tags to print (default: 20) |
| `--sample N` | Print N raw rows (default: 0) |
| `--deep` | Add table statistics, version history, and the largest files on disk — outputs JSON |
| `--stats` | Report index health and compare the stored vector dimension against `EMBED_DIM` in `memory_store.py`. Prints alignment status (✅ ALIGNED / ❌ MISMATCH). Exit code 2 if mismatched — use as a scriptable health check. Supports `--json`. |
| `--row ID` | Fetch a single row by its id and print full content — outputs JSON |
| `--api` | Print `dir(table)` for the opened handle and exit |
| `--json` | Output the default report as JSON |

**Exit codes:** `0` = success, `1` = error (store not found, query error), `2` = embedding dimension mismatch

## Examples

```bash
# Default human-readable report
uv run python scripts/memory_inspect.py

# Machine-readable
uv run python scripts/memory_inspect.py --json

# Print 5 raw rows
uv run python scripts/memory_inspect.py --sample 5

# Show top 30 tags
uv run python scripts/memory_inspect.py --top-tags 30

# Deep introspection — fragments, versions, and where the bytes actually went
uv run python scripts/memory_inspect.py --deep > /tmp/ltm_deep.json
jq '.deep.stats' /tmp/ltm_deep.json
jq '.deep.largest_files[] | {path, size}' /tmp/ltm_deep.json

# Fetch one specific row (ids come from --json or memory_recall --json)
uv run python scripts/memory_inspect.py --row 0de41bc8e6483d2ed28751405379df89

# List every method exposed on the opened table handle
uv run python scripts/memory_inspect.py --api

# Inspect a non-default store — --memory auto-derives from its parent
uv run python scripts/memory_inspect.py --db /agent/memory/project_notes.lancedb

# Inspect a store whose source JSON files live in a different directory
uv run python scripts/memory_inspect.py \
    --db /tmp/standalone.lancedb --memory /agent/memory

# Embedding dimension health check (exit code 2 = mismatch → needs --build)
uv run python scripts/memory_inspect.py --stats

# Machine-readable stats check (for scripts / system-check)
uv run python scripts/memory_inspect.py --stats --json
```

## What the report tells you

### Default report

- **Store** — total size of the store directory plus any sibling
  `.rebuild` / `.backup` directories, with size and mtime. A lingering
  `.rebuild` means a previous `memory-ingest --build` was interrupted; a
  `.backup` is the previous store kept after the last successful rebuild and
  is safe to delete once the new one looks healthy.
- **Source JSON counts** — how many entries each source file would contribute
  to a fresh `memory-ingest --build`. Compare against `rows` to spot drift.
- **Index** — total and scanned row counts, `duplicate_ids` (should always be
  0 — ingest is idempotent, so anything else means rows were written outside
  the normal path), total/max/average text size, **`on-disk per row`** (the
  headline number for bloat investigation), the ratio against source totals,
  text-size buckets, source/label/tag distribution, and timestamp span.

### `--deep` (most useful for bloat investigation)

| Field | What it means |
|---|---|
| `stats.num_rows` | Rows in the table |
| `stats.total_bytes` | Size the table reports for its data |
| `stats.fragment_stats.num_fragments` | How many data fragments — many tiny fragments means many small appends |
| `stats.fragment_stats.num_small_fragments` | Fragments below the compaction threshold |
| `version_count` | Number of table versions; every write creates one |
| `largest_files` | The 25 biggest files on disk, relative to the store root |

If `total_bytes` is far below the store's actual directory size, the space is
going to superseded versions and unoptimised fragments. Both are reclaimed by
a `memory-ingest --build`.

### `--stats` (embedding / index health check)

Reads the actual width of the stored vector column and compares it against
`EMBED_DIM` in `scripts/memory_store.py` — a direct check on the data rather
than an inference from configuration.

| Output field | Meaning |
|---|---|
| `dimension_aligned` | `true` = healthy; `false` = MISMATCH, rebuild needed |
| `stored_dimension` | Vector width actually stored in the table |
| `expected_dimension` | Width the configured model produces |
| `expected_model` | Model name from `memory_store.EMBED_MODEL` |
| `has_fts_index` | Whether the BM25 full-text index exists (required for hybrid search) |
| `has_vec_index` | Whether an ANN index exists — absent is normal and correct below 5,000 rows, where an exact scan is faster |
| `indexes[].unindexed_rows` | Rows appended since the index was last built. These are still searchable (LanceDB scans the tail) but slow the query down as they accumulate; writes rebuild automatically past 200. |
| `retained_versions` | Un-reclaimed copy-on-write versions — the number that predicts storage bloat. Compaction triggers past 20, so a persistently high value means compaction is failing or rate-limited. |
| `last_compacted` | When the store was last compacted, from the `.long_term_memory.lancedb.compact` stamp file. `never` on a store that has only just been built. |

Exit code `2` signals a mismatch so shell scripts / `system-check.md` can act
on `$?` directly. Exit code `0` = aligned. Exit code `1` = store-not-found or
query error.

**Fix for dimension mismatch:** `uv run python scripts/memory_ingest.py --build`

## Integration with other tools

- **memory-ingest** (`scripts/memory_ingest.py`) — populates the store. If
  `memory-inspect --deep` shows many small fragments or a large version count,
  run `memory-ingest --build` to compact it from source JSON.
- **memory-recall** / **memory-ask** — query the same store. If a query
  returns unexpectedly few results, `memory-inspect` will tell you whether the
  rows are actually present (compare row counts to source totals) and whether
  the full-text index exists.
- **cycle_close.py** — batches all per-cycle writes into a single embed pass
  and one write via `memory_ingest.append_many`, which keeps the version count
  bounded between rebuilds. `memory-inspect --deep` is the tool to verify that
  batching is working: expect roughly one new version per cycle, not one per
  inbox message.
