---
name: search
description: Hybrid ripgrep + BM25S full-text search that returns relevance-ranked snippets across agent-managed dirs (memory/, messages/, web/, workspace/, scripts/, prompts/) and any other repo path. Use to find free-text content in journals, inbox/outbox, goals, capabilities, notes, code, or markdown — any text, JSON, or JSONL file.
---

# search

**Path:** `scripts/search.py`

Two-stage search: `ripgrep` does a fast exact-match pre-filter to discover candidate files, then BM25S indexes each one and produces a globally re-ranked list of relevance-scored chunks. Large files (>100 KB) get a cached on-disk index that is rebuilt only when the source file's mtime changes.

## Arguments

| Flag | Description |
|------|-------------|
| `QUERY` | Search query string (positional, required for searches) |
| `--dir, -d DIR` | Directory for rg pre-filter (default: `/agent`) |
| `--top, -k N` | Number of top results to display (default: `10`) |
| `--no-rg` | Skip ripgrep pre-filter; search only existing catalog files |
| `--json` | Emit machine-readable JSON instead of the formatted output |
| `--rebuild-all` | Force-rebuild all cached BM25S indices then exit (no query needed) |
| `--list-catalog` | Print all files currently in the search catalog then exit |
| `--reset-catalog` | Reset catalog to default files only |

**Exit codes:** `0` = success, `1` = missing query (when not in utility mode)

## Examples

```bash
# Default search across /agent
uv run python scripts/search.py "myspec-coder"

# Scoped search inside one subtree
uv run python scripts/search.py "webhook" --dir /agent/memory/

# Show more results
uv run python scripts/search.py "evolution" --top 20

# Skip the rg pre-filter; only re-rank files already in the catalog
uv run python scripts/search.py "goal failed" --no-rg

# JSON output for programmatic consumers
uv run python scripts/search.py "myspec-coder" --json

# Search the project repo (any path works) — respects .gitignore
uv run python scripts/search.py "retry logic" --dir /Users/vincent/workspace/claw-dex/claw-dna

# Maintenance
uv run python scripts/search.py --list-catalog
uv run python scripts/search.py --reset-catalog
uv run python scripts/search.py --rebuild-all
```

## Output

### Default (formatted)

```
🔍  Query : "webhook"
    5 file(s) found by rg in /agent | 8 file(s) searched | 142 ms total
    Showing top 3 of 12 hit(s)

──────────────────────────────────────────────────────────────────────────────────
  # 1  score=4.7821  [doc 17]  /agent/memory/journal.json
       …configured the github webhook to POST to the agent's inbox endpoint…

  # 2  score=3.1204  [doc L42]  /agent/messages/inbox.jsonl
       …received webhook payload from telegram bridge…
──────────────────────────────────────────────────────────────────────────────────
```

`doc_id` format depends on the source file: array index (JSON list), key (JSON dict), `L<n>` (JSONL line), `P<n>` (paragraph), or `L<a>-<b>` (line chunk).

### JSON (`--json`)

```json
{
  "query": "webhook",
  "search_dir": "/agent",
  "no_rg": false,
  "rg_count": 5,
  "files_searched": 8,
  "total_hits": 12,
  "returned_hits": 3,
  "elapsed_ms": 142.32,
  "hits": [
    {
      "rank": 1,
      "score": 4.7821,
      "doc_id": "17",
      "file": "/agent/memory/journal.json",
      "snippet": "…configured the github webhook to POST…"
    }
  ]
}
```

## Supported file types

| Type | Chunking |
|------|----------|
| `.json` | one doc per top-level array item / dict value (string leaves recursively flattened) |
| `.jsonl` | one doc per non-empty line |
| Plain text / Markdown / code | paragraph split (≥3 paragraphs) or 30-line chunks |

Binary files (`*.npy`, `*.db`, `*.kdbx`, `*.lance`/`*.lancedb` stores, images, fonts, archives) are skipped automatically. `.gitignore` is respected everywhere.

## How `--dir` interacts with `.gitignore`

- rg always respects `.gitignore` and additionally skips hidden VCS dirs by default (`--hidden` is enabled, so non-ignored hidden dirs like `.github/` ARE searched).
- The 4 agent state directories — `/agent/memory`, `/agent/messages`, `/agent/web`, `/agent/workspace` — are listed in `.gitignore`, so they would normally be invisible to rg. The script passes them as **explicit search roots** when they fall inside the chosen `--dir`, so they are searched anyway.
- A narrow `--dir` is never widened: `--dir /home/agent/some-other/dir` will NOT pull in `/agent/memory` etc.

## Persistent state

| Path | Purpose |
|------|---------|
| `/home/agent/.bm25s/search_catalog.json` | List of file paths currently tracked by the catalog. Seeded with the default agent state files; rg-discovered files are auto-appended (capped at 20 per run). |
| `/home/agent/.bm25s/search_indices/` | Per-file cached BM25 indices for files >100 KB. Rebuilt on mtime change. |

## How it works

1. **Pre-filter** — `rg --files-with-matches --hidden` produces the candidate file list across `--dir` plus the 4 forced `/agent` roots.
2. **Catalog update** — new candidates are appended to the persistent catalog (capped per run).
3. **Per-file BM25S** — each catalog file is parsed into docs, then indexed (cached on disk if >100 KB, otherwise built on the fly).
4. **Retrieve** — top `k` docs per file (5/10/20 by file size) are pulled via BM25.
5. **Global re-rank** — all per-file hits are merged and sorted by BM25 score.
6. **Output** — top `--top` hits printed (formatted or JSON).

## Dependencies

- `rg` (ripgrep) on PATH
- `bm25s` Python package (already in `pyproject.toml`, Linux-only wheel)
