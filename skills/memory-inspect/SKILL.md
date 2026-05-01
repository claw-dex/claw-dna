---
name: memory-inspect
description: Inspect the long-term semantic memory `.mv2` file (memvid SDK) for size, frame counts, segment catalog, payload-vs-on-disk efficiency, and per-frame breakdown. Use when the `.mv2` file is growing unexpectedly fast, when diagnosing storage bloat or commit/segment inflation, when comparing the indexed entry count against the source JSON files (journal, cycles, inbox_history), when sampling frame contents to find auto-chunked records, or when deciding whether to rebuild via `memory-ingest`. Triggers on "inspect mv2", "why is long_term_memory.mv2 so big", "memvid file growth", "memvid stats", "check memory file size", "memvid storage utilisation", or any disk-usage investigation of the agent's long-term memory store.
---

# memory-inspect

**Path:** `scripts/memory_inspect.py`

Read-only diagnostic tool for the agent's long-term semantic memory `.mv2`
file (memvid SDK). Reports on-disk footprint, frame counts, segment catalog,
storage utilisation, source/label/tag distribution, and timestamp span — and
compares the indexed counts against the source JSON files (`journal.json`,
`journal_archive.json`, `cycles.json`, `cycles_archive.json`,
`messages/inbox_history.json`, `messages/inbox.json`).

The tool exists because the `.mv2` can grow much faster than the underlying
content would suggest: each `mem.commit()` rewrites the segment catalog and
reserves significant on-disk space, so per-message commits inflate the file
~37× compared to a fresh `memory-ingest --build`. `memory-inspect` makes
that inflation visible.

## Arguments

| Flag | Description |
|------|-------------|
| `--mv2 PATH` | Path to the `.mv2` file (default: `/agent/memory/long_term_memory.mv2`). Pass alone to inspect any `.mv2` — `--memory` auto-derives from its parent. |
| `--memory PATH` | Path to the memory directory holding the source JSON files. Defaults to the parent directory of `--mv2`; only set this when the source JSON lives elsewhere. |
| `--limit N` | Max entries to iterate via `timeline()` (default: 200000) |
| `--top-tags N` | How many top tags to print (default: 20) |
| `--sample N` | Print N raw timeline entries (default: 0) |
| `--deep` | Call SDK introspection (`stats`, `memories_stats`, `state`, `get_capacity`, `doctor`, `verify`, `list_tables`) and fetch full frames for the largest fan-out records — outputs JSON |
| `--frame N` | Fetch a single frame by id via `mem.frame()` and print full content — outputs JSON |
| `--api` | Print `dir(mem)` for the opened handle and exit (lists all SDK methods) |
| `--json` | Output the default report as JSON |

**Exit codes:** `0` = success, `1` = error (file not found, SDK error)

## Examples

```bash
# Default human-readable report
uv run python scripts/memory_inspect.py

# Machine-readable
uv run python scripts/memory_inspect.py --json

# Print 5 raw timeline entries (preview text + frame_id + child_frames)
uv run python scripts/memory_inspect.py --sample 5

# Show top 30 tags
uv run python scripts/memory_inspect.py --top-tags 30

# Deep introspection — what the SDK itself reports about segments / index sizes
uv run python scripts/memory_inspect.py --deep > /tmp/mv2_deep.json
jq '.deep.stats' /tmp/mv2_deep.json
jq '.deep.fanout_samples[] | {frame_id, n_children, parent_blob_len}' /tmp/mv2_deep.json

# Fetch one specific frame
uv run python scripts/memory_inspect.py --frame 1234

# List every method exposed by memvid_sdk on the opened handle
uv run python scripts/memory_inspect.py --api

# Inspect a non-default .mv2 file — --memory auto-derives from its parent
uv run python scripts/memory_inspect.py --mv2 /agent/memory/project_notes.mv2

# Inspect a .mv2 whose source JSON files live in a different directory
uv run python scripts/memory_inspect.py \
    --mv2 /tmp/standalone.mv2 --memory /agent/memory
```

## What the report tells you

### Default report

- **File** — `.mv2` size + every sibling artifact (`.backup`, `.manifest.wal`,
  hidden `.RAND` tmp copies the SDK creates during atomic commits). Sibling
  size + mtime is shown so you can spot stale backups or tmp files left over
  from interrupted commits.
- **Source JSON counts** — how many entries each source file would contribute
  to a fresh `memory-ingest --build`. Compare to `entries_seen` to detect
  drift / auto-chunking.
- **Index** — `entries_seen`, `content_total_bytes` (sum of preview content
  before the SDK's appended ` title: / tags: / labels: ` metadata block),
  **`on-disk per entry`** (`file_size / entries_seen` — the headline number
  for bloat investigation), `inflation vs sources` ratio, content-size
  buckets, `child_frame_counts` (how many sub-frames each entry expanded
  into), source/label/tag distribution, timestamp span.

### `--deep` (most useful for bloat investigation)

Calls SDK introspection methods directly. Key fields in `deep.stats`:

| Field | What it means |
|---|---|
| `frame_count` | Total frames (top-level + auto-chunked children) |
| `payload_bytes` | Actual stored content (compressed) |
| `logical_bytes` | Uncompressed equivalent of payload |
| `lex_index_bytes` / `vec_index_bytes` / `time_index_bytes` | Index segment sizes |
| `wal_bytes` | Write-ahead log reservation |
| `size_bytes` | On-disk file size |
| `storage_utilisation_percent` | `payload / capacity` — **near-zero means massive sparse/dead space** |
| `segment_catalog` (from `doctor` probe lines) | Number of vec/lex/time segments — one new vec segment per `mem.put` is the typical bloat signature |

Sum the four `*_bytes` values + WAL — if that's ≪ `size_bytes`, the file is
mostly dead space from per-call commits, and a `memory-ingest --build` is
the cheapest fix.

`fanout_samples` shows the records with the most child frames (these are
typically large journal entries that the SDK auto-split into multiple
sub-frames). `flat_frame_samples` shows zero-child entries for comparison.

### Note on stderr noise

The memvid SDK is implemented in Rust and writes diagnostic lines (e.g.
`doctor: probe start`) directly to file-descriptor 2. The script wraps SDK
calls in an `_silenced_stderr()` context that dups FD 2 to `/dev/null` so
`--json` / `--deep` output stays parseable. Don't be surprised if the file
descriptor briefly redirects during deep mode.

## Integration with other tools

- **memory-ingest** (`scripts/memory_ingest.py`) — populates the `.mv2`. If
  `memory-inspect --deep` shows `storage_utilisation_percent` near zero,
  run `memory-ingest --build` to compact the file from source JSON.
- **memory-recall** / **memory-ask** — query the same `.mv2`. If a query
  returns unexpectedly few results, `memory-inspect` will tell you whether
  the entries are actually present (compare `entries_seen` to source totals).
- **cycle_close.py** — batches all per-cycle writes into a single
  `memvid_sdk` open + N puts + ONE commit via `memory_ingest.append_many`,
  which keeps file growth bounded between rebuilds. `memory-inspect` is
  the tool to verify this batching is actually working (look for one
  vec segment per cycle, not one per inbox message).
