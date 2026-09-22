#!/usr/bin/env python3
"""
memory_ingest.py — Ingest agent memory into long-term semantic store (LanceDB).

Parses journal.json, journal_archive.json, and messages/inbox_history.json
(sibling of the memory dir), chunks them into semantically meaningful pieces,
and ingests them into a LanceDB table via scripts/memory_store.py (hybrid
BM25 + semantic search over bge-small embeddings). cycles.json is no longer
ingested — cycle metadata is redundant with journal entries.

Usage:
    uv run python scripts/memory_ingest.py --build                          # Full rebuild
    uv run python scripts/memory_ingest.py --build --dry-run                # Preview chunks
    uv run python scripts/memory_ingest.py --append-json CYCLE_JSON         # Append one JSON entry
    uv run python scripts/memory_ingest.py --append-text "Some note to remember"  # Ingest raw text
    uv run python scripts/memory_ingest.py --append-file /path/to/notes.md  # Ingest a text file

Modes:
    --build           Parse all memory files and rebuild the index from scratch
    --append-json JSON   Append a single entry — journal, cycle, or goal (JSON string or @file.json path)
    --append-text TEXT   Ingest raw text directly (with optional --title and --tags)
    --append-file PATH   Ingest a text file directly (.txt, .md, .json, …)

Optional:
    --db PATH         Path to the LanceDB store (default: /agent/memory/long_term_memory.lancedb)
    --memory PATH     Path to the memory directory (default: /agent/memory)
    --title TEXT      Title for --append-text / --append-file entries (default: auto-generated)
    --tags TAG…       Tags for --append-text / --append-file (space-separated, each in quotes)
    --dry-run         Show what would be ingested without writing
    --json            Output results as JSON
    --quiet           Suppress progress output

Ingest is idempotent: each chunk gets a deterministic id derived from its
source and content, so re-appending an unchanged record replaces it instead of
creating a duplicate.

Exit codes: 0 = success, 1 = error
"""

import itertools
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import memory_store as store
from scripts.memory_store import BUILD_BATCH_SIZE, DEFAULT_DB, MEMORY

# Supported extensions for file ingestion (used by --append-file).
# Text-like formats only: extraction of PDF/DOCX/XLSX/PPTX and media is not
# available in-process. Convert those to text first, then --append-file the
# result (or pipe it through --append-text).
INGESTIBLE_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".html",
        ".htm",
        ".json",
        ".jsonl",
        ".csv",
        ".tsv",
        ".log",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".py",
        ".sh",
        ".sql",
    }
)

# Refuse to slurp an arbitrarily large file into one chunk.
MAX_FILE_BYTES = 5 * 1024 * 1024


def load_json(path: Path):
    """Load a JSON file, return None if missing or invalid."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"  WARN: failed to load {path.name}: {e}", file=sys.stderr)
        return None


def parse_args(argv):
    args = argv[1:]
    result = {
        "build": False,
        "append_json": None,
        "append_text": None,
        "append_file": None,
        "db": str(DEFAULT_DB),
        "memory": str(MEMORY),
        "title": None,
        "tags": [],
        "dry_run": False,
        "json_mode": False,
        "quiet": False,
        "help": False,
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            result["help"] = True
        elif a in (
            "--append-json",
            "--append-text",
            "--append-file",
            "--db",
            "--memory",
            "--title",
        ) and i + 1 >= len(args):
            print(f"ERROR: {a} requires a value", file=sys.stderr)
            sys.exit(1)
        elif a == "--build":
            result["build"] = True
        elif a == "--append-json":
            i += 1
            result["append_json"] = args[i]
        elif a == "--append-text":
            i += 1
            result["append_text"] = args[i]
        elif a == "--append-file":
            i += 1
            result["append_file"] = args[i]
        elif a == "--db":
            i += 1
            result["db"] = args[i]
        elif a == "--memory":
            i += 1
            result["memory"] = args[i]
        elif a == "--title":
            i += 1
            result["title"] = args[i]
        elif a == "--tags":
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                result["tags"].append(args[i])
                i += 1
            i -= 1  # outer loop will i += 1, so back up to re-process the -- flag
        elif a == "--dry-run":
            result["dry_run"] = True
        elif a == "--json":
            result["json_mode"] = True
        elif a == "--quiet":
            result["quiet"] = True
        i += 1
    return result


# ---------------------------------------------------------------------------
# Chunking — turn memory JSON files into semantically meaningful chunks
# ---------------------------------------------------------------------------


def compose_journal_text(entry: dict) -> str:
    """Compose readable text from a journal entry for semantic embedding.

    Caller is expected to have already migrated the entry to the current
    schema (e.g. via repair_memory_files.migrate_journal_entry); reads only new
    field names.
    """
    parts = []
    goal = entry.get("cycle_goal", "")
    if goal:
        parts.append(f"Goal: {goal}")
    summary = entry.get("summary", "")
    if summary and summary != goal:
        parts.append(f"Summary: {summary}")
    actions = entry.get("actions", [])
    if actions:
        parts.append("Actions: " + "; ".join(actions))
    category = entry.get("cycle_category", "")
    if category:
        parts.append(f"Category: {category}")
    return "\n".join(parts)


def transform_journal_entry(entry: dict) -> dict | None:
    """Convert a single journal entry into an ingest chunk, or None if unusable."""
    from scripts.repair_memory_files import migrate_journal_entry

    if not isinstance(entry, dict):
        return None
    migrate_journal_entry(entry)
    cycle = entry.get("cycle_number", 0)
    summary = entry.get("summary", "")
    ctype = entry.get("cycle_type", "")
    status = entry.get("cycle_status", "")
    category = entry.get("cycle_category", "")
    timestamp = entry.get("timestamp", "")

    text = compose_journal_text(entry)
    if not text or len(text.strip()) < 10:
        return None

    tags = ["journal"]
    if ctype:
        tags.append(f"type:{ctype}")
    if category:
        tags.append(f"category:{category}")
    if status:
        tags.append(f"status:{status}")
    tags.append(f"cycle:{cycle}")
    if timestamp:
        date_part = timestamp[:10]
        tags.append(f"date:{date_part}")

    return {
        "title": f"Cycle {cycle}: {summary[:100]}",
        "label": ctype or "journal",
        "text": text,
        "tags": tags,
        "metadata": {
            "source": "journal",
            "cycle": str(cycle),
            "type": ctype,
            "status": status,
            "category": category,
            "date": timestamp,
        },
    }


def transform_journal(journal: list) -> list:
    """Batch-transform journal entries into ingestible chunks."""
    return [c for c in (transform_journal_entry(e) for e in journal) if c is not None]


def transform_cycle_entry(entry: dict) -> dict | None:
    """Convert a single cycle record into an ingest chunk, or None if unusable."""
    from scripts.repair_memory_files import migrate_cycle_entry

    if not isinstance(entry, dict):
        return None
    migrate_cycle_entry(entry)
    cycle = entry.get("cycle_number", 0)
    # Cycle records no longer carry `summary`; surface the planned cycle
    # goal instead (with a final fallback to a stray `summary` field on
    # very old completed records).
    cycle_goal = entry.get("cycle_goal") or entry.get("summary", "")
    ctype = entry.get("cycle_type", "")
    status = entry.get("cycle_status", "")
    category = entry.get("cycle_category", "")
    start = entry.get("start", "")
    end = entry.get("end", "")
    duration = entry.get("duration_seconds")

    parts = [f"Cycle {cycle}"]
    if ctype:
        parts.append(f"Type: {ctype}")
    if category:
        parts.append(f"Category: {category}")
    if status:
        parts.append(f"Status: {status}")
    if cycle_goal:
        parts.append(f"Goal: {cycle_goal}")
    if duration is not None:
        m, s = divmod(int(duration), 60)
        parts.append(f"Duration: {m}m {s}s")
    if start:
        parts.append(f"Started: {start[:19]}")
    if end:
        parts.append(f"Ended: {end[:19]}")

    text = "\n".join(parts)
    if len(text.strip()) < 10:
        return None

    tags = ["cycle"]
    if ctype:
        tags.append(f"type:{ctype}")
    if category:
        tags.append(f"category:{category}")
    if status:
        tags.append(f"status:{status}")
    tags.append(f"cycle:{cycle}")
    if start:
        tags.append(f"date:{start[:10]}")

    return {
        "title": f"Cycle {cycle}: {cycle_goal[:80] or ctype}",
        "label": "cycle",
        "text": text,
        "tags": tags,
        "metadata": {
            "source": "cycle",
            "cycle": str(cycle),
            "type": ctype,
            "status": status,
            "category": category,
            "date": start,
        },
    }


def transform_cycles(cycles: list) -> list:
    """Batch-transform cycle records into ingestible chunks."""
    return [c for c in (transform_cycle_entry(e) for e in cycles) if c is not None]


def transform_goal_entry(entry: dict) -> dict | None:
    """Convert a single goal record into an ingest chunk, or None if unusable."""
    if not isinstance(entry, dict):
        return None
    content = entry.get("content") or entry.get("goal") or ""
    status = entry.get("status", "unknown")
    goal_id = entry.get("id", "")

    if not content or len(content.strip()) < 5:
        return None

    text = f"Goal: {content}\nStatus: {status}"
    tags = ["goal", f"status:{status}"]
    if goal_id:
        tags.append(f"id:{goal_id}")

    return {
        "title": content[:100],
        "label": "goal",
        "text": text,
        "tags": tags,
        "metadata": {
            "source": "goal",
            "status": status,
            "id": goal_id,
        },
    }


def transform_goals(goals: list) -> list:
    """Batch-transform goal records into ingestible chunks."""
    return [c for c in (transform_goal_entry(e) for e in goals) if c is not None]


def transform_inbox_entry(entry: dict) -> dict | None:
    """Convert a single inbox message dict into an ingest chunk.

    Shared by rebuild (transform_inbox) and live ingestion
    (cycle_close._inbox_chunks_for_ltm → append_many) so both paths
    produce identical records. The timestamp always comes from the entry —
    never datetime.now() — so rebuilds preserve original message times.

    Returns None if the entry is unusable (non-dict or content too short).
    """
    if not isinstance(entry, dict):
        return None
    content = str(entry.get("content") or "").strip()
    if len(content) < 5:
        return None
    msg_type = str(entry.get("type") or "message")
    # source now lives in from.source; fall back to legacy top-level source for
    # entries written before the relocation. Inlined to keep this standalone
    # script import-free.
    _frm = entry.get("from")
    source = str(
        (_frm.get("source") if isinstance(_frm, dict) else None)
        or entry.get("source")
        or ""
    )
    ts = str(
        entry.get("timestamp") or entry.get("received_at") or entry.get("date") or ""
    )
    date_part = ts[:10] if ts else ""
    msg_id = entry.get("id")

    cycle_raw = entry.get("cycle_number")
    cycle_int: int | None = None
    if isinstance(cycle_raw, bool):
        cycle_int = None
    elif isinstance(cycle_raw, int):
        cycle_int = cycle_raw
    elif isinstance(cycle_raw, str) and cycle_raw.strip().isdigit():
        try:
            cycle_int = int(cycle_raw)
        except ValueError:
            cycle_int = None

    tags = ["inbox", f"type:{msg_type}"]
    if source:
        tags.append(f"inbox_source:{source}")
    if date_part:
        tags.append(f"date:{date_part}")
    if msg_id:
        tags.append(f"id:{msg_id}")
    if cycle_int is not None:
        tags.append(f"cycle:{cycle_int}")

    metadata = {
        "source": "inbox",
        "message_type": msg_type,
        "inbox_source": source,
        "id": str(msg_id) if msg_id else "",
        "date": ts,
    }
    if cycle_int is not None:
        metadata["cycle"] = str(cycle_int)

    return {
        "title": f"Inbox {msg_type}: {content[:80]}",
        "label": "inbox",
        "text": content,
        "tags": tags,
        "metadata": metadata,
    }


def transform_inbox(messages: list) -> list:
    """Batch-transform archived inbox messages into ingestible chunks (rebuild path)."""
    return [c for c in (transform_inbox_entry(m) for m in messages) if c is not None]


def iter_all_chunks(memory_dir: Path):
    """Yield ingest chunks one at a time, freeing each source JSON before
    moving to the next file. Used by :func:`build` so peak memory during a
    rebuild is bounded by max(individual JSON file size) + one batch worth
    of transformed chunks, instead of holding every source list + every
    transformed list + the request list all at once.

      1. journal.json         — current window (richest, most recent)
      2. journal_archive.json — historical journal (rich, ordered newest-first)
      3. messages/inbox_history.json — archived inbox messages (sibling dir)

    All transformers preserve the source-recorded timestamp in metadata["date"];
    rebuild never substitutes datetime.now().

    cycles.json / cycles_archive.json are intentionally excluded — cycle
    metadata is largely redundant with journal entries which already carry
    richer semantic content. Goals (goal.json, goal_history.json) are also
    excluded from long-term memory.
    """
    # 1. Current journal window — most recent, richest semantic content
    journal = load_json(memory_dir / "journal.json")
    if isinstance(journal, list):
        for entry in journal:
            ch = transform_journal_entry(entry)
            if ch is not None:
                yield ch
    journal = None  # drop the source list before loading the next file

    # 2. Journal archive — historical entries, newest-first ordering preserved
    archive = load_json(memory_dir / "journal_archive.json")
    if archive is None:
        archive = load_json(memory_dir / "journal-archive.json")
    if isinstance(archive, list):
        for entry in archive:
            ch = transform_journal_entry(entry)
            if ch is not None:
                yield ch
    archive = None

    # 3. Inbox history — archived messages live in the sibling messages/ dir.
    #    Each message was also live-ingested at arrival; rebuild reconstructs
    #    them from JSON with original timestamps.
    inbox_history = load_json(
        Path(memory_dir).resolve().parent / "messages" / "inbox_history.json"
    )
    if isinstance(inbox_history, list):
        for entry in inbox_history:
            ch = transform_inbox_entry(entry)
            if ch is not None:
                yield ch
    inbox_history = None


def gather_all_chunks(memory_dir: Path) -> list:
    """Materialize all chunks into a single list.

    Kept for tests and callers that need a list. The streaming
    :func:`iter_all_chunks` is preferred for the rebuild path because it
    avoids holding every source JSON + every transformed chunk in memory
    simultaneously.
    """
    return list(iter_all_chunks(memory_dir))


# ---------------------------------------------------------------------------
# Build — ingest chunks into LanceDB
# ---------------------------------------------------------------------------


def _cleanup_staging(
    staging: Path, err: BaseException, quiet: bool, db_file: Path | None = None
) -> None:
    """Remove the partial staging database after a failed/aborted build.

    The rebuild writes only into the staging directory, so in the normal
    failure case the canonical store is untouched and only the partial staging
    directory needs removing.

    The exception is an interruption *during* the two-rename swap: the
    canonical name can be gone while its contents sit under ``.backup``. Blindly
    deleting staging then would leave the backup as the only copy — and the next
    rebuild starts by deleting the backup. So restore it first.

    Best-effort: cleanup failures are reported to stderr but never mask the
    original exception (the caller re-raises).
    """
    err_label = f"{type(err).__name__}: {err}"
    if db_file is not None:
        _restore_backup_if_orphaned(db_file, quiet)
    store.clear_compact_stamp(staging)
    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if not quiet:
            print(
                f"[INGEST] Rebuild failed ({err_label}) — removed partial "
                f"{staging.name}; canonical index left untouched",
                file=sys.stderr,
            )
    except Exception as rb_err:
        print(
            f"[INGEST] Staging cleanup ALSO failed: "
            f"{type(rb_err).__name__}: {rb_err} (original error: {err_label})",
            file=sys.stderr,
        )


def _restore_backup_if_orphaned(db_file: Path, quiet: bool) -> None:
    """Put the previous store back if a swap left the canonical name missing."""
    _staging, backup = store.staging_paths(db_file)
    if db_file.exists() or not backup.exists():
        return
    try:
        os.replace(backup, db_file)
        if not quiet:
            print(
                f"[INGEST] Swap was interrupted — restored {backup.name} → "
                f"{db_file.name}",
                file=sys.stderr,
            )
    except OSError as e:
        print(
            f"[INGEST] CRITICAL: swap was interrupted and restoring "
            f"{backup.name} failed ({e}). The previous index is intact at "
            f"{backup}; move it back manually.",
            file=sys.stderr,
        )


def build(memory_dir, db_path, dry_run=False, quiet=False, json_mode=False):
    """Full rebuild: stream chunks from memory files and ingest into LanceDB.

    Uses :func:`iter_all_chunks` as a generator so peak memory is bounded by
    one source JSON file + one BUILD_BATCH_SIZE batch of transformed chunks
    plus their embeddings, rather than materializing every source list + every
    transformed chunk + the full row list simultaneously.
    """
    mem_dir = Path(memory_dir)
    db_file = Path(db_path)

    if dry_run:
        # Stream-print as the generator yields. No full chunk list is ever
        # materialized — dry-run has the same memory profile as the real
        # build.
        count = 0
        if json_mode:
            print('{\n  "mode": "dry_run",\n  "chunks": [')
            first = True
            for ch in iter_all_chunks(mem_dir):
                count += 1
                prefix = "" if first else ",\n"
                first = False
                sys.stdout.write(
                    prefix
                    + json.dumps(
                        {
                            "title": ch["title"],
                            "label": ch["label"],
                            "tags": ch["tags"],
                            "text_len": len(ch["text"]),
                        }
                    )
                )
            print(f'\n  ],\n  "total_chunks": {count}\n}}')
        else:
            print("\n[DRY RUN] Streaming chunks:")
            for i, ch in enumerate(iter_all_chunks(mem_dir), start=1):
                count = i
                print(
                    f"  {i:3d}. [{ch['label']}] {ch['title'][:70]} "
                    f"({len(ch['text'])} chars, {len(ch['tags'])} tags)"
                )
            print(f"[DRY RUN] Total: {count} chunks")
        if count == 0:
            print("ERROR: No chunks to ingest. Check memory files.", file=sys.stderr)
            sys.exit(1)
        return

    # Real build. Peek the first chunk to detect "no chunks to ingest" before
    # we touch the staging directory — keeping the original error path intact.
    chunk_iter = iter_all_chunks(mem_dir)
    try:
        first = next(chunk_iter)
    except StopIteration:
        print("ERROR: No chunks to ingest. Check memory files.", file=sys.stderr)
        sys.exit(1)
    chunk_iter = itertools.chain([first], chunk_iter)

    # Build into a staging directory so the canonical store stays openable by
    # other processes (live inbox ingest, recall, append-*) for the full
    # duration of the rebuild. Only after the new index is finalized do we swap.
    staging, backup = store.staging_paths(db_file)

    db_file.parent.mkdir(parents=True, exist_ok=True)

    # Stale staging dir from a previously crashed/killed rebuild — drop it.
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    store.clear_compact_stamp(staging)

    ok = 0
    try:
        tbl = store.create_table(staging)
        if not quiet:
            print(f"[INGEST] Building into staging store {staging}")

        # Stream the generator in BUILD_BATCH_SIZE slices. Only one batch of
        # rows (and their embeddings) exists at a time. The staging table
        # starts empty and `seen` tracks ids across batches, so add_chunks can
        # skip its dedup scan and the rebuild stays linear.
        seen: set = set()
        batch_n = 0
        while True:
            batch_chunks = list(itertools.islice(chunk_iter, BUILD_BATCH_SIZE))
            if not batch_chunks:
                break
            batch_n += 1
            fresh = []
            for ch in batch_chunks:
                cid = store.row_id(ch)
                if cid in seen:
                    continue
                seen.add(cid)
                fresh.append(ch)
            batch_chunks = None
            if not fresh:
                continue
            added, failed = store.add_chunks(tbl, fresh, quiet=quiet, dedup=False)
            if failed:
                raise RuntimeError(
                    f"batch {batch_n}: {failed} chunk(s) failed to write"
                )
            ok += added
            fresh = None
            if not quiet and not json_mode:
                print(
                    f"[INGEST] Committed batch {batch_n} "
                    f"(running total: {ok} chunks)"
                )

        # Terminal finalize before swap: build the search indexes and compact
        # the many small write batches into fewer fragments. If this raises,
        # the staging index isn't trustworthy — propagate so cleanup runs and
        # the canonical store stays untouched.
        store.ensure_indexes(tbl, quiet=quiet)
        # Nothing else can hold this staging store, and it has no history worth
        # keeping, so prune every superseded version rather than the default
        # 15-minute window. This is what turns 40 append batches into a handful
        # of files.
        store.compact(tbl, retention=timedelta(0), quiet=quiet)
        if not quiet:
            print(f"[INGEST] Wrote {ok} rows to staging index")

        # Atomic swap: rename the canonical store to .backup (if it exists),
        # then rename the freshly-built staging directory into its place. Both
        # renames are atomic on POSIX; the canonical name is briefly absent
        # between the two calls but never half-written.
        store.promote_staging(db_file)
        # The promoted store was just fully compacted, so start its clock now
        # rather than letting the next write compact a store that has nothing
        # to reclaim.
        store.mark_compacted(db_file)
        if not quiet:
            print(f"[INGEST] Promoted staging → {db_file.name}")
    except BaseException as e:
        # Catch BaseException so KeyboardInterrupt / SystemExit also trigger
        # cleanup before propagating. The canonical store was never opened
        # by the rebuild, so we only need to drop the partial staging dir.
        _cleanup_staging(staging, e, quiet, db_file)
        raise

    size_kb = store.dir_size(db_file) / 1024

    if json_mode:
        print(
            json.dumps(
                {
                    "mode": "build",
                    "db": str(db_file),
                    "total_chunks": ok,
                    "ingested": ok,
                    "failed": 0,
                    "size_kb": round(size_kb, 1),
                },
                indent=2,
            )
        )
    elif not quiet:
        print(f"[INGEST] Done — {ok} ingested ({size_kb:.1f} KB)")
        print("[INGEST] Query with:")
        print('  uv run python scripts/memory_recall.py "your question here"')


# ---------------------------------------------------------------------------
# Append JSON — ingest a single journal/cycle/goal entry
# ---------------------------------------------------------------------------


def _detect_and_transform(entry: dict) -> dict | None:
    """Auto-detect entry type and route to the correct transformer.

    Detection heuristic (applies to current schema; legacy keys are mapped
    by the transformers themselves via repair_memory_files.migrate_*):
      - Has "actions" or ("cycle_goal" + "summary") → journal entry
      - Has "start" or "end" or "duration_seconds" → cycle record
      - Has "content" and "status" (no cycle fields) → goal record

    Returns a single chunk dict or None if the entry produced nothing
    ingestible (e.g. text too short).
    """
    if "actions" in entry or ("cycle_goal" in entry and "summary" in entry):
        return transform_journal_entry(entry)
    if "start" in entry or "end" in entry or "duration_seconds" in entry:
        return transform_cycle_entry(entry)
    if "content" in entry or (
        "goal" in entry and "status" in entry and "summary" not in entry
    ):
        return transform_goal_entry(entry)
    # Fallback: treat as journal entry
    return transform_journal_entry(entry)


def _open_for_append(db_path: Path):
    """Open the store for writing, or exit 1 telling the user to build first."""
    tbl = store.open_table(db_path)
    if tbl is None:
        print(f"ERROR: {db_path} not found. Run --build first.", file=sys.stderr)
        sys.exit(1)
    return tbl


def _write_one(tbl, chunk: dict, what: str) -> None:
    """Write a single chunk, exiting 1 with a uniform message on failure."""
    ok, fail = store.add_chunks(tbl, [chunk], quiet=False)
    if ok != 1 or fail:
        print(f"ERROR: {what} write failed", file=sys.stderr)
        sys.exit(1)
    store.maintain(tbl)


def _write_split(tbl, chunk: dict, what: str) -> int:
    """Write a chunk, splitting text too long for the embedding model.

    The embedder truncates at 512 tokens, so a long document stored as one row
    would only be semantically searchable by its opening paragraphs. Each piece
    becomes its own row, tagged with its position so the pieces stay traceable
    back to the source. Returns the number of rows written.
    """
    pieces = store.split_text(chunk["text"])
    if len(pieces) <= 1:
        _write_one(tbl, chunk, what)
        return 1

    total = len(pieces)
    rows = []
    for i, piece in enumerate(pieces, 1):
        part = dict(chunk)
        part["text"] = piece
        part["title"] = f"{chunk['title']} [{i}/{total}]"
        part["tags"] = list(chunk.get("tags") or []) + [f"part:{i}/{total}"]
        meta = dict(chunk.get("metadata") or {})
        meta["part"] = f"{i}/{total}"
        part["metadata"] = meta
        rows.append(part)

    ok, fail = store.add_chunks(tbl, rows, quiet=False)
    if ok != total or fail:
        print(
            f"ERROR: {what} write failed ({ok}/{total} pieces written)", file=sys.stderr
        )
        sys.exit(1)
    store.maintain(tbl)
    return ok


def append_json(db_path, entry_source, quiet=False, json_mode=False):
    """Append a single JSON entry to the existing store (journal, cycle, or goal).

    Writes are idempotent: an unchanged entry replaces its previous row rather
    than adding a duplicate.
    """
    db_file = Path(db_path)
    # Open first: a missing store is the actionable root cause and should be
    # reported ahead of any complaint about the entry itself.
    tbl = _open_for_append(db_file)

    # Parse the entry from JSON string or @file
    if entry_source.startswith("@"):
        file_path = Path(entry_source[1:])
        if not file_path.exists():
            print(f"ERROR: File not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        try:
            entry = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON in {file_path}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            entry = json.loads(entry_source)
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
            sys.exit(1)

    ch = _detect_and_transform(entry)
    if ch is None:
        print("ERROR: Entry produced no ingestible chunk.", file=sys.stderr)
        sys.exit(1)

    _write_one(tbl, ch, "append")

    cycle = entry.get("cycle_number", "?")
    if json_mode:
        print(
            json.dumps(
                {
                    "mode": "append",
                    "cycle": cycle,
                    "title": ch["title"],
                    "db": str(db_file),
                },
                indent=2,
            )
        )
    elif not quiet:
        print(f"[INGEST] Appended cycle {cycle} to {db_file.name}")


# ---------------------------------------------------------------------------
# Append Many — batched ingest: one open, one embed pass, one write
# ---------------------------------------------------------------------------


def store_ready(db_path) -> bool:
    """True when the store exists and holds a usable memories table.

    Directory existence alone isn't enough: an empty or half-removed
    `.lancedb/` would otherwise look built forever and every append would
    silently no-op.
    """
    return store.open_table(db_path) is not None


def append_many(db_path, chunks: list, *, quiet: bool = True) -> tuple:
    """Ingest multiple chunks in a single batched write.

    Embeds the whole batch in one fastembed call and writes it in one
    transaction, which is far cheaper than a per-chunk loop.

    The caller is responsible for ensuring the store exists — a missing store
    is treated as a no-op so we don't trigger a hidden full rebuild inside an
    unrelated code path.

    Returns ``(ok, fail)`` counts.
    """
    if not chunks:
        return (0, 0)
    tbl = store.open_table(db_path)
    if tbl is None:
        return (0, 0)
    result = store.add_chunks(tbl, chunks, quiet=quiet)
    # Amortised upkeep: this is the batched end-of-cycle path, so it's the
    # natural place to absorb the unindexed tail and reclaim the disk left
    # behind by previous cycles' writes.
    store.maintain(tbl, quiet=quiet)
    return result


# ---------------------------------------------------------------------------
# Append Inbox — ingest one inbox message using shared chunk schema
# ---------------------------------------------------------------------------


def append_inbox_message(db_path, message: dict, quiet: bool = True) -> bool:
    """Ingest a single inbox message into the store using the shared chunk
    schema. The message's own timestamp field is used (never datetime.now()),
    so live ingestion and rebuild produce identical records.

    Returns True on success, False if the message was skipped (too short /
    not a dict), the store is missing, or the write failed.
    """
    chunk = transform_inbox_entry(message)
    if chunk is None:
        return False
    tbl = store.open_table(db_path)
    if tbl is None:
        return False
    ok, _fail = store.add_chunks(tbl, [chunk], quiet=quiet)
    store.maintain(tbl, quiet=quiet)
    return ok == 1


# ---------------------------------------------------------------------------
# Append Text — ingest raw text directly
# ---------------------------------------------------------------------------


def append_text(db_path, text, title=None, tags=None, quiet=False, json_mode=False):
    """Ingest raw text directly into the store."""
    db_file = Path(db_path)
    tbl = _open_for_append(db_file)

    if not text or len(text.strip()) < 5:
        print("ERROR: Text is too short (min 5 characters).", file=sys.stderr)
        sys.exit(1)

    title = title or text[:100]
    tags = tags or []
    all_tags = ["manual-ingest", "text"] + list(tags)
    metadata = {
        "source": "append-text",
        "date": datetime.now(timezone.utc).isoformat(),
    }

    parts = _write_split(
        tbl,
        {
            "title": title,
            "label": "text",
            "text": text,
            "tags": all_tags,
            "metadata": metadata,
        },
        "append-text",
    )

    if json_mode:
        print(
            json.dumps(
                {
                    "mode": "append-text",
                    "title": title,
                    "text_len": len(text),
                    "parts": parts,
                    "tags": all_tags,
                    "db": str(db_file),
                },
                indent=2,
            )
        )
    elif not quiet:
        suffix = f" as {parts} pieces" if parts > 1 else ""
        print(f"[INGEST] Appended text ({len(text)} chars){suffix} to {db_file.name}")


# ---------------------------------------------------------------------------
# Append File — ingest a text file directly
# ---------------------------------------------------------------------------


def read_text_file(fpath: Path) -> str:
    """Read a text file for ingestion, or exit 1 with a clear reason.

    Binary formats (PDF, DOCX, XLSX, PPTX, images, audio, video) are not
    supported: extracting them would need a document-conversion dependency the
    agent does not carry. Convert to text first, then ingest the result.
    """
    ext = fpath.suffix.lower()
    if ext not in INGESTIBLE_EXTENSIONS:
        print(f"ERROR: Unsupported file type: {ext or '(none)'}", file=sys.stderr)
        print(
            f"  Supported: {', '.join(sorted(INGESTIBLE_EXTENSIONS))}",
            file=sys.stderr,
        )
        print(
            "  For PDF/DOCX/XLSX/PPTX or media, convert to text first "
            "and ingest the result.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        size = fpath.stat().st_size
    except OSError as e:
        print(f"ERROR: Cannot stat {fpath}: {e}", file=sys.stderr)
        sys.exit(1)
    if size > MAX_FILE_BYTES:
        print(
            f"ERROR: File too large ({size / 1024 / 1024:.1f} MB, "
            f"limit {MAX_FILE_BYTES // 1024 // 1024} MB): {fpath}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        text = fpath.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"ERROR: Cannot read {fpath}: {e}", file=sys.stderr)
        sys.exit(1)

    if len(text.strip()) < 5:
        print(f"ERROR: {fpath.name} has no ingestible text.", file=sys.stderr)
        sys.exit(1)
    return text


def append_file(db_path, filepath, title=None, tags=None, quiet=False, json_mode=False):
    """Ingest a text file directly into the store."""
    db_file = Path(db_path)
    tbl = _open_for_append(db_file)

    fpath = Path(filepath)
    if not fpath.exists():
        print(f"ERROR: File not found: {fpath}", file=sys.stderr)
        sys.exit(1)

    text = read_text_file(fpath)
    ext = fpath.suffix.lower()

    title = title or fpath.name
    tags = tags or []
    all_tags = ["manual-ingest", "file", f"ext:{ext}"] + list(tags)
    metadata = {
        "source": "append-file",
        "filepath": str(fpath),
        "date": datetime.now(timezone.utc).isoformat(),
    }

    parts = _write_split(
        tbl,
        {
            "title": title,
            "label": "file",
            "text": text,
            "tags": all_tags,
            "metadata": metadata,
        },
        "append-file",
    )

    try:
        size_kb = fpath.stat().st_size / 1024
    except OSError:
        size_kb = 0

    if json_mode:
        print(
            json.dumps(
                {
                    "mode": "append-file",
                    "filepath": str(fpath),
                    "title": title,
                    "size_kb": round(size_kb, 1),
                    "parts": parts,
                    "tags": all_tags,
                    "db": str(db_file),
                },
                indent=2,
            )
        )
    elif not quiet:
        suffix = f" as {parts} pieces" if parts > 1 else ""
        print(
            f"[INGEST] Ingested {fpath.name} ({size_kb:.1f} KB){suffix} "
            f"into {db_file.name}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    opts = parse_args(sys.argv)

    if opts["help"]:
        print(__doc__)
        sys.exit(0)

    if opts["build"]:
        build(
            opts["memory"],
            opts["db"],
            dry_run=opts["dry_run"],
            quiet=opts["quiet"],
            json_mode=opts["json_mode"],
        )
    elif opts["append_json"]:
        append_json(
            opts["db"],
            opts["append_json"],
            quiet=opts["quiet"],
            json_mode=opts["json_mode"],
        )
    elif opts["append_text"]:
        append_text(
            opts["db"],
            opts["append_text"],
            title=opts["title"],
            tags=opts["tags"],
            quiet=opts["quiet"],
            json_mode=opts["json_mode"],
        )
    elif opts["append_file"]:
        append_file(
            opts["db"],
            opts["append_file"],
            title=opts["title"],
            tags=opts["tags"],
            quiet=opts["quiet"],
            json_mode=opts["json_mode"],
        )
    else:
        print(
            "ERROR: Provide --build, --append-json, --append-text, or --append-file",
            file=sys.stderr,
        )
        print("Usage: uv run python scripts/memory_ingest.py --build", file=sys.stderr)
        print(
            "       uv run python scripts/memory_ingest.py --append-json '{...}'",
            file=sys.stderr,
        )
        print(
            "       uv run python scripts/memory_ingest.py --append-text 'some text'",
            file=sys.stderr,
        )
        print(
            "       uv run python scripts/memory_ingest.py --append-file /path/to/notes.md",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
