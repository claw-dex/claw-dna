#!/usr/bin/env python3
"""
memory_ingest.py — Ingest agent memory into long-term semantic store (memvid SDK).

Parses journal.json, cycles.json, and goal.json, chunks them into semantically
meaningful pieces, and ingests into a .mv2 index via the `memvid_sdk` Python
package (hybrid lexical + semantic search with bge-base embeddings).

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
EMBED_MODEL = "bge-base"

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
    return {
        "embedding_model": EMBED_MODEL,
        "enable_embedding": True,
        "vector_compression": compress,
    }


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
    """Compose readable text from a journal entry for semantic embedding."""
    parts = []
    goal = entry.get("goal", "")
    if goal:
        parts.append(f"Goal: {goal}")
    summary = entry.get("summary", "")
    if summary and summary != goal:
        parts.append(f"Summary: {summary}")
    actions = entry.get("actions", [])
    if actions:
        parts.append("Actions: " + "; ".join(actions))
    category = entry.get("category", "")
    if category:
        parts.append(f"Category: {category}")
    outcome = entry.get("outcome", "")
    if outcome:
        parts.append(f"Outcome: {outcome}")
    learnings = entry.get("learnings", {})
    if isinstance(learnings, dict) and learnings:
        for key in ("approach", "key_decisions", "reusable_patterns", "pitfalls"):
            val = learnings.get(key)
            if isinstance(val, list):
                val = "; ".join(str(v) for v in val)
            if val:
                parts.append(f"{key.replace('_', ' ').title()}: {val}")
    return "\n".join(parts)


def chunk_journal(journal: list) -> list:
    """Convert journal entries into ingestible chunks."""
    chunks = []
    for entry in journal:
        if not isinstance(entry, dict):
            continue
        cycle = entry.get("cycle", 0)
        summary = entry.get("summary", "")
        ctype = entry.get("type", "")
        status = entry.get("status", "")
        category = entry.get("category", "")
        timestamp = entry.get("timestamp", "")

        text = compose_journal_text(entry)
        if not text or len(text.strip()) < 10:
            continue

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

        chunks.append(
            {
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
        )
    return chunks


def chunk_cycles(cycles: list) -> list:
    """Convert cycle records into ingestible chunks."""
    chunks = []
    for entry in cycles:
        if not isinstance(entry, dict):
            continue
        cycle = entry.get("cycle", 0)
        summary = entry.get("summary", "")
        ctype = entry.get("type", "")
        status = entry.get("status", "")
        category = entry.get("category", "")
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
        if summary:
            parts.append(f"Summary: {summary}")
        if duration is not None:
            m, s = divmod(int(duration), 60)
            parts.append(f"Duration: {m}m {s}s")
        if start:
            parts.append(f"Started: {start[:19]}")
        if end:
            parts.append(f"Ended: {end[:19]}")

        text = "\n".join(parts)
        if len(text.strip()) < 10:
            continue

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

        chunks.append(
            {
                "title": f"Cycle {cycle}: {summary[:80] or ctype}",
                "label": "cycle",
                "text": text,
                "tags": tags,
                "metadata": {
                    "source": "cycle",
                    "cycle": str(cycle),
                    "type": ctype,
                    "status": status,
                    "category": category,
                    "date": start[:10] if start else "",
                },
            }
        )
    return chunks


def chunk_goals(goals: list) -> list:
    """Convert goal records into ingestible chunks."""
    chunks = []
    for entry in goals:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content") or entry.get("goal") or ""
        status = entry.get("status", "unknown")
        goal_id = entry.get("id", "")

        if not content or len(content.strip()) < 5:
            continue

        text = f"Goal: {content}\nStatus: {status}"
        tags = ["goal", f"status:{status}"]
        if goal_id:
            tags.append(f"id:{goal_id}")

        chunks.append(
            {
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
        )
    return chunks


def gather_all_chunks(memory_dir: Path) -> list:
    """Load all memory files and produce a combined list of chunks.

    Ordering is intentional: richest semantic sources first, with cycle
    records (largely redundant with journal data) added last.

      1. journal.json       — current window (richest, most recent)
      2. journal-archive.json — historical journal (rich, ordered newest-first)
      3. goal.json          — active/recent goals
      4. cycles.json        — structured cycle records (lower priority; largely
                               redundant with journal data and shorter text)
    """
    all_chunks = []

    # 1. Current journal window — most recent, richest semantic content
    journal = load_json(memory_dir / "journal.json")
    if isinstance(journal, list):
        all_chunks.extend(chunk_journal(journal))

    # 2. Journal archive — historical entries, newest-first ordering preserved
    archive = load_json(memory_dir / "journal-archive.json")
    if isinstance(archive, list):
        all_chunks.extend(chunk_journal(archive))

    # 3. Goals
    goals_raw = load_json(memory_dir / "goal.json")
    if isinstance(goals_raw, list):
        all_chunks.extend(chunk_goals(goals_raw))
    elif isinstance(goals_raw, dict):
        goals_list = goals_raw.get("goals", [])
        if isinstance(goals_list, list):
            all_chunks.extend(chunk_goals(goals_list))

    # 4. Cycle records — lower priority; added last because cycle metadata
    #    largely duplicates what journal entries already contain.
    cycles = load_json(memory_dir / "cycles.json")
    if isinstance(cycles, list):
        all_chunks.extend(chunk_cycles(cycles))

    return all_chunks


# ---------------------------------------------------------------------------
# Build — ingest chunks via memvid SDK
# ---------------------------------------------------------------------------


def build(memory_dir, mv2_path, dry_run=False, quiet=False, json_mode=False):
    """Full rebuild: parse all memory files and ingest into .mv2."""
    if not dry_run:
        _require_sdk()
    mem_dir = Path(memory_dir)
    mv2 = Path(mv2_path)

    chunks = gather_all_chunks(mem_dir)
    if not quiet:
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

    # Backup existing .mv2 before full rebuild
    if mv2.exists():
        backup = mv2.with_suffix(mv2.suffix + ".backup")
        shutil.copy2(mv2, backup)
        if not quiet:
            print(f"[INGEST] Backed up {mv2.name} → {backup.name}")
        mv2.unlink()

    mv2.parent.mkdir(parents=True, exist_ok=True)
    mem = memvid_sdk.create(str(mv2), enable_vec=True, enable_lex=True)
    if not quiet:
        print(f"[INGEST] Created {mv2}")

    # Compression threshold is checked per-chunk because the file grows
    # during the build loop.
    put_base_kwargs = {"embedding_model": EMBED_MODEL, "enable_embedding": True}
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


def _detect_and_chunk(entry: dict) -> list:
    """Auto-detect entry type and route to the correct chunker.

    Detection heuristic:
      - Has "summary" or "goal" + "actions"/"learnings" → journal entry
      - Has "start" or "end" or "duration_seconds" → cycle record
      - Has "content" and "status" (without cycle fields) → goal record
    """
    if (
        "actions" in entry
        or "learnings" in entry
        or ("goal" in entry and "summary" in entry)
    ):
        return chunk_journal([entry])
    if "start" in entry or "end" in entry or "duration_seconds" in entry:
        return chunk_cycles([entry])
    if "content" in entry or (
        "goal" in entry and "status" in entry and "summary" not in entry
    ):
        return chunk_goals([entry])
    # Fallback: treat as journal entry
    return chunk_journal([entry])


def append_json(mv2_path, entry_source, quiet=False, json_mode=False):
    """Append a single JSON entry to the existing .mv2 index (journal, cycle, or goal)."""
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

    chunks = _detect_and_chunk(entry)
    if not chunks:
        print("ERROR: Entry produced no ingestible chunks.", file=sys.stderr)
        sys.exit(1)

    ch = chunks[0]
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

    cycle = entry.get("cycle", "?")
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
# Append Text — ingest raw text directly
# ---------------------------------------------------------------------------


def append_text(mv2_path, text, title=None, tags=None, quiet=False, json_mode=False):
    """Ingest raw text directly into the .mv2 index."""
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
    mv2_path, filepath, title=None, tags=None, quiet=False, json_mode=False
):
    """Ingest a file directly into the .mv2 index."""
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
