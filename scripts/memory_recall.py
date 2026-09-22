#!/usr/bin/env python3
"""
memory_recall.py — Query long-term semantic memory (LanceDB).

Searches the agent's long-term memory store for entries matching a
natural-language question using hybrid retrieval: BM25 full-text search fused
with bge-small vector similarity (see scripts/memory_store.py).

Usage:
    uv run python scripts/memory_recall.py "What did I work on last week?"
    uv run python scripts/memory_recall.py "portal reliability fixes" --k 10
    uv run python scripts/memory_recall.py "efficiency improvements" --json
    uv run python scripts/memory_recall.py --timeline
    uv run python scripts/memory_recall.py --timeline --since 2026-03-01

Required:
    QUESTION          Natural-language query (first positional argument)

Optional:
    --db PATH         Path to the LanceDB store (default: /agent/memory/long_term_memory.lancedb)
    --k N             Number of results to return (default: 5)
    --min-score F     Drop results scoring below F of the top hit, 0..1 (default: 0 = keep all)
    --json            Output as JSON instead of formatted text
    --timeline        Show timeline entries instead of semantic search
    --since DATE      Filter entries since DATE (ISO format or unix timestamp)
    --until DATE      Filter entries until DATE (ISO format or unix timestamp)

Exit codes: 0 = success, 1 = error (missing args, store not found, query error)
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts import memory_store as store
from scripts.memory_store import DEFAULT_DB

# Kept as a module-level name so callers and tests can point the library
# `recall()` helper at a different store.
DB_PATH = DEFAULT_DB


def parse_args(argv):
    args = argv[1:]
    result = {
        "question": None,
        "k": 5,
        "db": None,
        "min_score": 0.0,
        "json_mode": False,
        "timeline": False,
        "since": None,
        "until": None,
        "help": False,
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            result["help"] = True
        elif a == "--k" and i + 1 < len(args):
            i += 1
            try:
                result["k"] = int(args[i])
            except ValueError:
                print(
                    f"ERROR: --k must be an integer, got: {args[i]!r}", file=sys.stderr
                )
                sys.exit(1)
        elif a == "--min-score" and i + 1 < len(args):
            i += 1
            try:
                result["min_score"] = float(args[i])
            except ValueError:
                print(
                    f"ERROR: --min-score must be a number, got: {args[i]!r}",
                    file=sys.stderr,
                )
                sys.exit(1)
        elif a == "--db" and i + 1 < len(args):
            i += 1
            result["db"] = args[i]
        elif a == "--json":
            result["json_mode"] = True
        elif a == "--timeline":
            result["timeline"] = True
        elif a == "--since" and i + 1 < len(args):
            i += 1
            result["since"] = args[i]
        elif a == "--until" and i + 1 < len(args):
            i += 1
            result["until"] = args[i]
        elif not a.startswith("--") and result["question"] is None:
            result["question"] = a
        i += 1
    return result


def _parse_date_to_unix(value, strict: bool = True):
    """Convert a date string (ISO format) or integer to a unix timestamp int.

    strict=True (CLI): exit on invalid input. strict=False (library): return None.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        if strict:
            print(
                f"ERROR: Invalid date format: {value!r}. Use ISO format (2026-03-25) or unix timestamp.",
                file=sys.stderr,
            )
            sys.exit(1)
        return None


def _result_to_dict(row: dict, rank: int) -> dict:
    """Shape a store result into the dict this script prints and returns.

    `snippet` is simply the stored text: LanceDB returns the document column
    verbatim, so there is no embedded metadata to strip.
    """
    return {
        "rank": rank,
        "score": row.get("score", 0.0),
        "title": row.get("title") or "",
        "snippet": row.get("text") or "",
        "tags": list(row.get("tags") or []),
        "id": row.get("id"),
        "metadata": dict(row.get("metadata") or {}),
    }


def search_store(db_path, query: str, k: int, since=None, until=None, min_score=0.0):
    """Run hybrid search and return (items, total_hits).

    Raises FileNotFoundError when the store is missing so callers can choose
    between a hard error (CLI) and an empty list (library).
    """
    tbl = store.open_table(db_path)
    if tbl is None:
        raise FileNotFoundError(str(db_path))
    rows = store.search(tbl, query, k=k, since=since, until=until, min_score=min_score)
    items = [_result_to_dict(r, i) for i, r in enumerate(rows, 1)]
    return items, len(items)


def main():
    opts = parse_args(sys.argv)

    if opts["help"]:
        print(__doc__)
        sys.exit(0)

    if not opts["timeline"] and not opts["question"]:
        print(
            "ERROR: QUESTION is required (first positional argument)", file=sys.stderr
        )
        print(
            'Usage: uv run python scripts/memory_recall.py "your question here"',
            file=sys.stderr,
        )
        sys.exit(1)

    db_path = Path(opts["db"]) if opts["db"] else DB_PATH
    if not db_path.exists():
        print(
            f"ERROR: {db_path} not found. Run at least one cycle-close to create it.",
            file=sys.stderr,
        )
        sys.exit(1)

    if opts["timeline"]:
        _run_timeline(opts, db_path)
    else:
        _run_query(opts, db_path)


def _run_query(opts, db_path):
    """Run hybrid search against the LanceDB store."""
    question = opts["question"]
    k = opts["k"]
    since = _parse_date_to_unix(opts.get("since"))
    until = _parse_date_to_unix(opts.get("until"))

    try:
        items, total = search_store(
            db_path,
            question,
            k,
            since=since,
            until=until,
            min_score=opts.get("min_score", 0.0),
        )
    except Exception as e:
        print(f"ERROR: memory search failed: {e}", file=sys.stderr)
        sys.exit(1)

    if opts["json_mode"]:
        print(
            json.dumps(
                {
                    "query": question,
                    "k": k,
                    "total_hits": total,
                    "results": items,
                },
                indent=2,
            )
        )
    else:
        print(f'[MEMORY RECALL] "{question}" (k={k})\n')
        if not items:
            print("No matching memories found.")
        else:
            for item in items:
                score = item["score"] or 0
                title = item["title"] or "untitled"
                snippet = item["snippet"]
                tags = item["tags"]

                cycle_tag = next((t for t in tags if t.startswith("cycle:")), "")
                date_tag = next((t for t in tags if t.startswith("date:")), "")

                header = f"── Result {item['rank']}/{len(items)} (score: {score:.4f})"
                if cycle_tag:
                    header += f" | {cycle_tag}"
                if date_tag:
                    header += f" | {date_tag}"
                print(f"{header} ──")
                print(f"  {title}")
                if snippet:
                    lines = snippet.splitlines()
                    preview = "\n    ".join(lines[:4])
                    print(f"    {preview}")
                    if len(lines) > 4:
                        print(f"    ... ({len(lines) - 4} more lines)")
                print()
        print(
            f"[MEMORY RECALL] {len(items)} result(s) returned (total matches: {total})."
        )


def _timeline_entry(row: dict) -> dict:
    """Shape a store timeline row for output.

    Unlike semantic search the store returns every column in one scan, so
    title and tags need no follow-up lookup per entry.
    """
    tags = list(row.get("tags") or [])
    entry = {
        "id": row.get("id"),
        "timestamp": row.get("ts"),
        "date": row.get("date") or "",
        "title": row.get("title") or "",
        "label": row.get("label") or "",
        "source": row.get("source") or "",
        "tags": tags,
        "preview": row.get("text") or "",
    }
    cycle_tag = next((t for t in tags if t.startswith("cycle:")), "")
    if cycle_tag:
        entry["cycle"] = cycle_tag.split(":", 1)[1]
    return entry


def _run_timeline(opts, db_path):
    """Show timeline entries newest-first."""
    k = opts["k"]
    since = opts["since"]
    since_unix = _parse_date_to_unix(since)
    until_unix = _parse_date_to_unix(opts.get("until"))

    try:
        tbl = store.open_table(db_path)
        if tbl is None:
            raise FileNotFoundError(str(db_path))
        rows = store.timeline(tbl, limit=k, since=since_unix, until=until_unix)
    except Exception as e:
        print(f"ERROR: memory timeline failed: {e}", file=sys.stderr)
        sys.exit(1)

    items = [_timeline_entry(r) for r in rows]

    if opts["json_mode"]:
        print(
            json.dumps(
                {
                    "mode": "timeline",
                    "count": len(items),
                    "since": since,
                    "entries": items,
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(
            f"[MEMORY TIMELINE] {len(items)} entries"
            + (f" (since {since})" if since else "")
        )
        print()
        for entry in items:
            ts = entry.get("timestamp", "")
            title = entry.get("title") or "untitled"
            tags = entry.get("tags") or []
            cycle_tag = next((t for t in tags if t.startswith("cycle:")), "")
            date_tag = next((t for t in tags if t.startswith("date:")), "")
            header = f"  [ts={ts}] {entry.get('label') or 'entry'}"
            if cycle_tag:
                header += f" | {cycle_tag}"
            if date_tag:
                header += f" | {date_tag}"
            print(f"{header}")
            print(f"    {title}")
            preview = " ".join((entry.get("preview") or "").split())[:160]
            if preview:
                print(f"    {preview}")
        print("\n[MEMORY TIMELINE] Done.")


def recall(query: str, k: int = 5, until=None, json_mode: bool = False) -> list:
    """Query long-term memory and return results as a list of dicts.

    Returns a list of result dicts (rank, score, title, snippet, tags, id,
    metadata). Returns an empty list on any error (store not found, backend
    unavailable, etc.). Does not print or call sys.exit().

    SystemExit is caught alongside Exception on purpose: the store's dependency
    guards exit rather than raise, and this helper runs inside cycle_start's
    thread pool, where an escaping SystemExit would abort the whole briefing
    over a missing optional dependency.
    """
    try:
        until_unix = _parse_date_to_unix(until, strict=False)
        items, _ = search_store(DB_PATH, query, k, until=until_unix)
        return items
    except (Exception, SystemExit):
        return []


if __name__ == "__main__":
    main()
