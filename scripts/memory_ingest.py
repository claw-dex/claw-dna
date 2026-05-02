#!/usr/bin/env python3
"""
memory_ingest.py — Ingest agent memory into long-term semantic store (memvid SDK).

Parses journal.json, journal_archive.json, and messages/inbox_history.json
(sibling of the memory dir), chunks them into semantically meaningful pieces,
and ingests into a .mv2 index via the `memvid_sdk` Python package (hybrid
lexical + semantic search with bge-base embeddings). cycles.json is no longer
ingested — cycle metadata is redundant with journal entries.

Usage:
    uv run python scripts/memory_ingest.py --build                          # Full rebuild
    uv run python scripts/memory_ingest.py --build --dry-run                # Preview chunks
    uv run python scripts/memory_ingest.py --append-json CYCLE_JSON         # Append one JSON entry
    uv run python scripts/memory_ingest.py --append-text "Some note to remember"  # Ingest raw text
    uv run python scripts/memory_ingest.py --append-file /path/to/doc.pdf   # Ingest a file

Modes:
    --build           Parse all memory files and rebuild the .mv2 index from scratch
    --append-json JSON   Append a single entry — journal, cycle, or goal (JSON string or @file.json path)
    --append-text TEXT   Ingest raw text directly (with optional --title and --tags)
    --append-file PATH   Ingest a file directly (PDF, DOCX, TXT, MD, etc.)

Optional:
    --mv2 PATH        Path to the .mv2 file (default: /agent/memory/long_term_memory.mv2)
    --memory PATH     Path to the memory directory (default: /agent/memory)
    --title TEXT      Title for --append-text / --append-file entries (default: auto-generated)
    --tags TAG…       Tags for --append-text / --append-file (space-separated, each in quotes)
    --dry-run         Show what would be ingested without writing
    --json            Output results as JSON
    --quiet           Suppress progress output

Exit codes: 0 = success, 1 = error
"""

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import memvid_sdk
except ImportError:
    memvid_sdk = None


def _require_sdk():
    """Fail fast with a clear error when memvid_sdk is unavailable."""
    if memvid_sdk is None:
        print(
            "ERROR: memvid_sdk not installed (not available on this runtime). "
            "Install from https://github.com/0xGosu/memvid-sdk",
            file=sys.stderr,
        )
        sys.exit(1)


MEMORY = Path("/agent/memory")
DEFAULT_MV2 = MEMORY / "long_term_memory.mv2"
# Local embedding model — uses fastembed (compiled into the Rust SDK binary).
# Requires the SDK to be built with `-F fastembed` (see seed/install_memvid.sh).
# Set to None to disable embedding and use lex-only indexing.
ENABLE_EMBEDDING = True
EMBED_MODEL = "bge-base"  # BAAI/bge-base-en-v1.5 via fastembed

# Enable vector compression only once the .mv2 grows past this size.
# Below the threshold, uncompressed vectors (~270 KB/doc) give the best
# search quality; above it, compression (~20 KB/doc, 16x savings) keeps
# the file from growing unbounded.
COMPRESSION_THRESHOLD_MB = 25


def _should_compress(mv2_path) -> bool:
    """Return True when the .mv2 is large enough to warrant compression."""
    try:
        p = Path(mv2_path)
        return p.exists() and p.stat().st_size > COMPRESSION_THRESHOLD_MB * 1024 * 1024
    except OSError:
        return False


# Supported extensions for file ingestion (used by --append-file)
INGESTIBLE_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".docx",
        ".xlsx",
        ".pptx",
        ".txt",
        ".md",
        ".html",
        ".jpg",
        ".jpeg",
        ".mp3",
        ".mp4",
    }
)


def _open_or_create(mv2: Path):
    """Open an existing .mv2 for write, or create a new one if missing."""
    _require_sdk()
    if mv2.exists():
        return memvid_sdk.use(
            "basic",
            str(mv2),
            mode="open",
            enable_vec=True,
            enable_lex=True,
        )
    mv2.parent.mkdir(parents=True, exist_ok=True)
    return memvid_sdk.create(str(mv2), enable_vec=True, enable_lex=True)


def _put_kwargs(compress: bool) -> dict:
    """Shared kwargs for every `Memvid.put` call."""
    kwargs: dict = {
        "enable_embedding": ENABLE_EMBEDDING,
        "vector_compression": compress,
    }
    if EMBED_MODEL is not None:
        kwargs["embedding_model"] = EMBED_MODEL
    return kwargs


_SMART_COMMIT_DEFAULT_THRESHOLD = 50


def _smart_commit_check(mv2: Path, puts_added: int) -> bool:
    """Increment the put counter for `mv2` by `puts_added` and decide whether
    to commit now.

    Counter state lives at ``<mv2>.put_counter`` as JSON:
    ``{"count": int, "threshold": int}``. Threshold defaults to
    ``_SMART_COMMIT_DEFAULT_THRESHOLD`` (50) on first use and is read back
    on every call, so the user can edit the file to tune the cadence
    without code changes.

    Returns True when the new count exceeds the threshold (counter is
    reset to 0 and persisted before returning). Otherwise persists the
    new count and returns False.
    """
    counter_path = mv2.parent / f"{mv2.name}.put_counter"
    count = 0
    threshold = _SMART_COMMIT_DEFAULT_THRESHOLD
    if counter_path.exists():
        try:
            data = json.loads(counter_path.read_text())
            if isinstance(data, dict):
                if isinstance(data.get("count"), int) and data["count"] >= 0:
                    count = data["count"]
                if isinstance(data.get("threshold"), int) and data["threshold"] > 0:
                    threshold = data["threshold"]
        except Exception:
            pass
    count += max(0, puts_added)
    fire = count > threshold
    if fire:
        count = 0
    try:
        counter_path.write_text(json.dumps({"count": count, "threshold": threshold}))
    except Exception:
        pass
    return fire


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
        "mv2": str(DEFAULT_MV2),
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
            "--mv2",
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
        elif a == "--mv2":
            i += 1
            result["mv2"] = args[i]
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
    schema (e.g. via memory_repair.migrate_journal_entry); reads only new
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
    from scripts.memory_repair import migrate_journal_entry

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
    from scripts.memory_repair import migrate_cycle_entry

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
    (cycle_close._inbox_chunks_for_memvid → append_many) so both paths
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
    source = str(entry.get("source") or "")
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


def gather_all_chunks(memory_dir: Path) -> list:
    """Load all memory files and produce a combined list of chunks.

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
    all_chunks = []

    # 1. Current journal window — most recent, richest semantic content
    journal = load_json(memory_dir / "journal.json")
    if isinstance(journal, list):
        all_chunks.extend(transform_journal(journal))

    # 2. Journal archive — historical entries, newest-first ordering preserved
    archive = load_json(memory_dir / "journal_archive.json")
    if archive is None:
        archive = load_json(memory_dir / "journal-archive.json")
    if isinstance(archive, list):
        all_chunks.extend(transform_journal(archive))

    # 3. Inbox history — archived messages live in the sibling messages/ dir.
    #    Each message was also live-ingested at arrival; rebuild reconstructs
    #    them from JSON with original timestamps.
    inbox_history = load_json(
        Path(memory_dir).resolve().parent / "messages" / "inbox_history.json"
    )
    if isinstance(inbox_history, list):
        all_chunks.extend(transform_inbox(inbox_history))

    return all_chunks


# ---------------------------------------------------------------------------
# Build — ingest chunks via memvid SDK
# ---------------------------------------------------------------------------


def _rollback_build(
    mv2: Path, backup: Path | None, err: BaseException, quiet: bool
) -> None:
    """Restore the pre-rebuild state after a failed/aborted build.

    If a backup was taken (an .mv2 existed before the rebuild started), copy it
    back over the partial .mv2. If no backup existed (fresh build), just remove
    the partial file. Best-effort — rollback failures are reported to stderr but
    do not mask the original exception (the caller re-raises ``err``).
    """
    err_label = f"{type(err).__name__}: {err}"
    try:
        if backup is not None and backup.exists():
            shutil.copy2(backup, mv2)
            if not quiet:
                print(
                    f"[INGEST] Rebuild failed ({err_label}) — restored "
                    f"{mv2.name} from {backup.name}",
                    file=sys.stderr,
                )
        else:
            if mv2.exists():
                mv2.unlink()
            if not quiet:
                print(
                    f"[INGEST] Rebuild failed ({err_label}) — no backup to "
                    f"restore; removed partial {mv2.name}",
                    file=sys.stderr,
                )
    except Exception as rb_err:
        print(
            f"[INGEST] Rollback ALSO failed: {type(rb_err).__name__}: {rb_err} "
            f"(original error: {err_label})",
            file=sys.stderr,
        )


def build(memory_dir, mv2_path, dry_run=False, quiet=False, json_mode=False):
    """Full rebuild: parse all memory files and ingest into .mv2."""
    if not dry_run:
        _require_sdk()
    mem_dir = Path(memory_dir)
    mv2 = Path(mv2_path)

    chunks = gather_all_chunks(mem_dir)
    if not quiet and not json_mode:
        print(f"[INGEST] Parsed {len(chunks)} chunks from memory files")

    if not chunks:
        print("ERROR: No chunks to ingest. Check memory files.", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        if json_mode:
            print(
                json.dumps(
                    {
                        "mode": "dry_run",
                        "total_chunks": len(chunks),
                        "chunks": [
                            {
                                "title": c["title"],
                                "label": c["label"],
                                "tags": c["tags"],
                                "text_len": len(c["text"]),
                            }
                            for c in chunks
                        ],
                    },
                    indent=2,
                )
            )
        else:
            print(f"\n[DRY RUN] Would ingest {len(chunks)} chunks:")
            for i, ch in enumerate(chunks):
                print(
                    f"  {i+1:3d}. [{ch['label']}] {ch['title'][:70]} "
                    f"({len(ch['text'])} chars, {len(ch['tags'])} tags)"
                )
        return

    # Backup existing .mv2 before full rebuild. The backup is also our
    # rollback source: if the rebuild raises (or a per-chunk loop is killed
    # mid-flight by Ctrl-C / OOM), we copy the backup back over the partial
    # .mv2 so the index is never left in a half-written state.
    backup: Path | None = None
    if mv2.exists():
        backup = mv2.with_suffix(mv2.suffix + ".backup")
        shutil.copy2(mv2, backup)
        if not quiet:
            print(f"[INGEST] Backed up {mv2.name} → {backup.name}")
        mv2.unlink()

    mv2.parent.mkdir(parents=True, exist_ok=True)

    try:
        mem = memvid_sdk.create(str(mv2), enable_vec=True, enable_lex=True)
        if not quiet:
            print(f"[INGEST] Created {mv2}")

        # Compression threshold is checked per-chunk because the file grows
        # during the build loop.
        put_base_kwargs: dict = {"enable_embedding": ENABLE_EMBEDDING}
        if EMBED_MODEL is not None:
            put_base_kwargs["embedding_model"] = EMBED_MODEL
        ok, fail = 0, 0
        for i, ch in enumerate(chunks):
            merged_meta = dict(ch.get("metadata") or {})
            # Fold tags into metadata so they're queryable (SDK accepts tags= too)
            try:
                mem.put(
                    title=ch["title"],
                    label=ch["label"],
                    text=ch["text"],
                    tags=ch["tags"],
                    metadata=merged_meta,
                    vector_compression=_should_compress(mv2),
                    **put_base_kwargs,
                )
                ok += 1
            except Exception as e:
                fail += 1
                if not quiet:
                    print(
                        f"  WARN: chunk {i} failed: {str(e)[:120]}",
                        file=sys.stderr,
                    )
            if not quiet and (i + 1) % 20 == 0:
                print(f"[INGEST] Ingested {i + 1}/{len(chunks)} chunks...")
            if (i + 1) % 1000 == 0:
                mem.commit()

        # Commit WAL → searchable index (REQUIRED — without this, put() calls are
        # buffered in the WAL and never appear in find()/ask() results).
        try:
            mem.seal()  # same as commit() in memvid_sdk
            if not quiet:
                print(f"[INGEST] Committed {ok} frames to index")
        except Exception as e:
            if not quiet:
                print(f"  WARN: commit failed: {e}", file=sys.stderr)
    except BaseException as e:
        # Catch BaseException so KeyboardInterrupt / SystemExit also trigger
        # rollback before propagating — a half-built .mv2 is worse than no
        # change at all when we have a known-good backup.
        _rollback_build(mv2, backup, e, quiet)
        raise

    size_kb = mv2.stat().st_size / 1024 if mv2.exists() else 0

    if json_mode:
        print(
            json.dumps(
                {
                    "mode": "build",
                    "mv2": str(mv2),
                    "total_chunks": len(chunks),
                    "ingested": ok,
                    "failed": fail,
                    "size_kb": round(size_kb, 1),
                },
                indent=2,
            )
        )
    elif not quiet:
        print(f"[INGEST] Done — {ok} ingested, {fail} failed ({size_kb:.1f} KB)")
        print(f"[INGEST] Query with:")
        print(f'  uv run python scripts/memory_recall.py "your question here"')


# ---------------------------------------------------------------------------
# Append JSON — ingest a single journal/cycle/goal entry
# ---------------------------------------------------------------------------


def _detect_and_transform(entry: dict) -> dict | None:
    """Auto-detect entry type and route to the correct transformer.

    Detection heuristic (applies to current schema; legacy keys are mapped
    by the transformers themselves via memory_repair.migrate_*):
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


def append_json(
    mv2_path, entry_source, quiet=False, json_mode=False, smart_commit: bool = False
):
    """Append a single JSON entry to the existing .mv2 index (journal, cycle, or goal).

    When ``smart_commit`` is True, the post-put commit is gated by
    :func:`_smart_commit_check` — commit only fires when the persisted
    put-counter exceeds its threshold. When False, commits unconditionally
    (existing behavior).
    """
    mv2 = Path(mv2_path)
    if not mv2.exists():
        print(f"ERROR: {mv2} not found. Run --build first.", file=sys.stderr)
        sys.exit(1)

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

    mem = _open_or_create(mv2)
    try:
        mem.put(
            title=ch["title"],
            label=ch["label"],
            text=ch["text"],
            tags=ch["tags"],
            metadata=dict(ch.get("metadata") or {}),
            **_put_kwargs(_should_compress(mv2)),
        )
    except Exception as e:
        print(f"ERROR: memvid put failed: {e}", file=sys.stderr)
        sys.exit(1)

    if not smart_commit or _smart_commit_check(mv2, 1):
        try:
            mem.commit()
        except Exception as e:
            print(f"WARN: commit failed: {e}", file=sys.stderr)

    cycle = entry.get("cycle_number", "?")
    if json_mode:
        print(
            json.dumps(
                {
                    "mode": "append",
                    "cycle": cycle,
                    "title": ch["title"],
                    "mv2": str(mv2),
                },
                indent=2,
            )
        )
    elif not quiet:
        print(f"[INGEST] Appended cycle {cycle} to {mv2.name}")


# ---------------------------------------------------------------------------
# Append Many — batched ingest with a single open + many puts + ONE commit
# ---------------------------------------------------------------------------


def append_many(
    mv2_path, chunks: list, *, quiet: bool = True, smart_commit: bool = False
) -> tuple:
    """Ingest multiple chunks under a single open + ONE final commit.

    Each per-call commit on the memvid `.mv2` rewrites the footer/segment
    catalog and reserves significant on-disk space, so callers that need to
    ingest several records at once should batch them through this function
    instead of looping on `append_*` (which commits per-record).

    The caller is responsible for ensuring the `.mv2` exists — a missing
    file is treated as a no-op so we don't trigger a hidden full rebuild
    inside an unrelated code path.

    When ``smart_commit`` is True, the final commit is gated by
    :func:`_smart_commit_check` — counter is incremented by the number of
    successful puts and the commit only fires when the new count exceeds
    the persisted threshold.

    Returns ``(ok, fail)`` counts.
    """
    _require_sdk()
    mv2 = Path(mv2_path)
    if not mv2.exists() or not chunks:
        return (0, 0)
    mem = _open_or_create(mv2)
    ok, fail = 0, 0
    for ch in chunks:
        try:
            mem.put(
                title=ch["title"],
                label=ch["label"],
                text=ch["text"],
                tags=ch["tags"],
                metadata=dict(ch.get("metadata") or {}),
                **_put_kwargs(_should_compress(mv2)),
            )
            ok += 1
        except Exception as e:
            fail += 1
            if not quiet:
                print(f"WARN: put failed: {e}", file=sys.stderr)
    if ok == 0:
        # Nothing landed — skip the commit so we don't reserve a footer
        # segment for an empty batch.
        return (ok, fail)
    if not smart_commit or _smart_commit_check(mv2, ok):
        try:
            mem.commit()
        except Exception as e:
            if not quiet:
                print(f"WARN: commit failed: {e}", file=sys.stderr)
    return (ok, fail)


# ---------------------------------------------------------------------------
# Append Inbox — ingest one inbox message using shared chunk schema
# ---------------------------------------------------------------------------


def append_inbox_message(
    mv2_path, message: dict, quiet: bool = True, smart_commit: bool = False
) -> bool:
    """Ingest a single inbox message into the .mv2 using the shared chunk
    schema. The message's own timestamp field is used (never datetime.now()),
    so live ingestion and rebuild produce identical records.

    When ``smart_commit`` is True, the post-put commit is gated by
    :func:`_smart_commit_check`; otherwise commit fires unconditionally.

    Returns True on success, False if the message was skipped (too short /
    not a dict) or the put failed.
    """
    _require_sdk()
    mv2 = Path(mv2_path)
    if not mv2.exists():
        return False
    chunk = transform_inbox_entry(message)
    if chunk is None:
        return False
    mem = _open_or_create(mv2)
    try:
        mem.put(
            title=chunk["title"],
            label=chunk["label"],
            text=chunk["text"],
            tags=chunk["tags"],
            metadata=chunk["metadata"],
            **_put_kwargs(_should_compress(mv2)),
        )
        if not smart_commit or _smart_commit_check(mv2, 1):
            mem.commit()
        return True
    except Exception as e:
        if not quiet:
            print(f"WARN: inbox put failed: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Append Text — ingest raw text directly
# ---------------------------------------------------------------------------


def append_text(
    mv2_path,
    text,
    title=None,
    tags=None,
    quiet=False,
    json_mode=False,
    smart_commit: bool = False,
):
    """Ingest raw text directly into the .mv2 index.

    When ``smart_commit`` is True, the post-put commit is gated by
    :func:`_smart_commit_check`; otherwise commit fires unconditionally.
    """
    mv2 = Path(mv2_path)
    if not mv2.exists():
        print(f"ERROR: {mv2} not found. Run --build first.", file=sys.stderr)
        sys.exit(1)

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

    mem = _open_or_create(mv2)
    try:
        mem.put(
            title=title,
            label="text",
            text=text,
            tags=all_tags,
            metadata=metadata,
            **_put_kwargs(_should_compress(mv2)),
        )
    except Exception as e:
        print(f"ERROR: memvid put failed: {e}", file=sys.stderr)
        sys.exit(1)

    if not smart_commit or _smart_commit_check(mv2, 1):
        try:
            mem.commit()
        except Exception as e:
            print(f"WARN: commit failed: {e}", file=sys.stderr)

    if json_mode:
        print(
            json.dumps(
                {
                    "mode": "append-text",
                    "title": title,
                    "text_len": len(text),
                    "tags": all_tags,
                    "mv2": str(mv2),
                },
                indent=2,
            )
        )
    elif not quiet:
        print(f"[INGEST] Appended text ({len(text)} chars) to {mv2.name}")


# ---------------------------------------------------------------------------
# Append File — ingest a file directly
# ---------------------------------------------------------------------------


def append_file(
    mv2_path,
    filepath,
    title=None,
    tags=None,
    quiet=False,
    json_mode=False,
    smart_commit: bool = False,
):
    """Ingest a file directly into the .mv2 index.

    When ``smart_commit`` is True, the post-put commit is gated by
    :func:`_smart_commit_check`; otherwise commit fires unconditionally.
    """
    mv2 = Path(mv2_path)
    if not mv2.exists():
        print(f"ERROR: {mv2} not found. Run --build first.", file=sys.stderr)
        sys.exit(1)

    fpath = Path(filepath)
    if not fpath.exists():
        print(f"ERROR: File not found: {fpath}", file=sys.stderr)
        sys.exit(1)

    ext = fpath.suffix.lower()
    if ext not in INGESTIBLE_EXTENSIONS:
        print(f"ERROR: Unsupported file type: {ext}", file=sys.stderr)
        print(
            f"  Supported: {', '.join(sorted(INGESTIBLE_EXTENSIONS))}", file=sys.stderr
        )
        sys.exit(1)

    title = title or fpath.name
    tags = tags or []
    all_tags = ["manual-ingest", "file", f"ext:{ext}"] + list(tags)
    metadata = {
        "source": "append-file",
        "filepath": str(fpath),
        "date": datetime.now(timezone.utc).isoformat(),
    }

    mem = _open_or_create(mv2)
    try:
        mem.put(
            title=title,
            file=str(fpath),
            tags=all_tags,
            metadata=metadata,
            **_put_kwargs(_should_compress(mv2)),
        )
    except Exception as e:
        print(f"ERROR: memvid put failed: {e}", file=sys.stderr)
        sys.exit(1)

    if not smart_commit or _smart_commit_check(mv2, 1):
        try:
            mem.commit()
        except Exception as e:
            print(f"WARN: commit failed: {e}", file=sys.stderr)

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
                    "tags": all_tags,
                    "mv2": str(mv2),
                },
                indent=2,
            )
        )
    elif not quiet:
        print(f"[INGEST] Ingested {fpath.name} ({size_kb:.1f} KB) into {mv2.name}")


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
            opts["mv2"],
            dry_run=opts["dry_run"],
            quiet=opts["quiet"],
            json_mode=opts["json_mode"],
        )
    elif opts["append_json"]:
        append_json(
            opts["mv2"],
            opts["append_json"],
            quiet=opts["quiet"],
            json_mode=opts["json_mode"],
        )
    elif opts["append_text"]:
        append_text(
            opts["mv2"],
            opts["append_text"],
            title=opts["title"],
            tags=opts["tags"],
            quiet=opts["quiet"],
            json_mode=opts["json_mode"],
        )
    elif opts["append_file"]:
        append_file(
            opts["mv2"],
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
            "       uv run python scripts/memory_ingest.py --append-file /path/to/file.pdf",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
