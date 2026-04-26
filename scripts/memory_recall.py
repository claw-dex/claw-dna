#!/usr/bin/env python3
"""
memory_recall.py — Query long-term semantic memory (memvid SDK).

Searches the agent's long-term memory store for entries matching a
natural-language question using the `memvid_sdk` Python package (hybrid
lexical + semantic search).

Usage:
    uv run python scripts/memory_recall.py "What did I work on last week?"
    uv run python scripts/memory_recall.py "portal reliability fixes" --k 10
    uv run python scripts/memory_recall.py "efficiency improvements" --json
    uv run python scripts/memory_recall.py --timeline
    uv run python scripts/memory_recall.py --timeline --since 2026-03-01

Required:
    QUESTION          Natural-language query (first positional argument)

Optional:
    --mv2 PATH        Path to the .mv2 file (default: /agent/memory/long_term_memory.mv2)
    --k N             Number of results to return (default: 5)
    --json            Output as JSON instead of formatted text
    --timeline        Show timeline entries instead of semantic search
    --since DATE      Filter entries since DATE (ISO format or unix timestamp)
    --until DATE      Filter entries until DATE (ISO format or unix timestamp)

Exit codes: 0 = success, 1 = error (missing args, file not found, SDK error)
"""

import json
import sys
from dataclasses import asdict, is_dataclass
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
MV2_PATH = MEMORY / "long_term_memory.mv2"


def parse_args(argv):
    args = argv[1:]
    result = {
        "question": None,
        "k": 5,
        "mv2": None,
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
        elif a == "--mv2" and i + 1 < len(args):
            i += 1
            result["mv2"] = args[i]
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


def _clean_snippet(text):
    """Strip internal memvid metadata from snippet text.

    The SDK appends frame metadata **inline** (no newlines) after the actual
    content text, using a pattern like:
        <content> title: <title> tags: <tags> labels: <labels> category: "..." ...

    We truncate at the first inline metadata marker (`` title: `` with a
    leading space) to remove the appended metadata block.  A newline-based
    fallback handles cases where the SDK does use line breaks.
    """
    if not text:
        return ""
    # Inline separator (SDK appends metadata as " title: ... tags: ...")
    for sep in (" title: ", " tags: ", " labels: ", " category: "):
        idx = text.find(sep)
        if idx != -1:
            text = text[:idx]
            break
    # Newline-based fallback
    for sep in ("\ntitle: ", "\ntags: ", "\nlabels: ", "\ncategory: "):
        idx = text.find(sep)
        if idx != -1:
            text = text[:idx]
            break
    _METADATA_PREFIXES = (
        "uri: mv2://",
        "tags: ",
        "labels: ",
        "category: ",
        "title: ",
        "extractous_metadata:",
        "memvid.",
        "metadata: {",
        "source: ",
        "status: ",
        "type: ",
        "cycle: ",
        "date: ",
        "id: ",
    )
    lines = [
        line for line in text.splitlines() if not line.startswith(_METADATA_PREFIXES)
    ]
    return "\n".join(lines).strip()


def _hit_to_dict(hit, rank: int) -> dict:
    """Normalize an SDK Hit dict (or dataclass) into the dict shape used by this script."""
    # SDK returns plain dicts, not dataclasses — use .get() with getattr fallback
    _g = (
        (lambda k, d=None: hit.get(k, d))
        if isinstance(hit, dict)
        else (lambda k, d=None: getattr(hit, k, d))
    )
    snippet = _clean_snippet(_g("snippet") or "")
    return {
        "rank": rank,
        "score": _g("score", 0.0),
        "title": _g("title", "") or "",
        "snippet": snippet,
        "tags": list(_g("tags", []) or []),
        "frame_id": _g("frame_id"),
    }


def _open_readonly(mv2: Path):
    """Open an existing .mv2 read-only via the SDK."""
    _require_sdk()
    return memvid_sdk.use(
        "basic",
        str(mv2),
        mode="open",
        enable_vec=True,
        enable_lex=True,
        read_only=True,
    )


def _ask_normalized(mv2: Path, query: str, k: int, since=None, until=None):
    """Run mem.ask and return (items, total_hits).

    Items are score-sorted dicts produced by `_hit_to_dict`. MV004 / "lex not
    enabled" is treated as zero results; other SDK errors propagate.
    """
    mem = _open_readonly(mv2)
    try:
        # search up to k results with minimum score of 0.1
        # this will filtering out noise hits with low score
        result = mem.ask(
            query,
            k=k,
            context_only=True,
            since=since,
            until=until,
            show_chunks=True,
            adaptive=True,
            max_k=k,
            min_relevancy=0.1,
            adaptive_strategy="absolute",
        )
    except Exception as e:
        err = str(e)
        if "MV004" in err or "Lexical index is not enabled" in err:
            result = {}
        else:
            raise

    # `chunks` (show_chunks=True) returns all k retrieved results.
    # `hits` only returns the single top-ranked result — always 1 regardless of k.
    if isinstance(result, dict):
        raw_hits = list(result.get("chunks") or result.get("hits") or [])
        stats = result.get("stats")
    else:
        raw_hits = list(
            getattr(result, "chunks", None) or getattr(result, "hits", None) or []
        )
        stats = getattr(result, "stats", None)

    raw_hits.sort(
        key=lambda h: (
            h.get("score", 0.0) if isinstance(h, dict) else getattr(h, "score", 0.0)
        ),
        reverse=True,
    )
    items = [_hit_to_dict(h, i) for i, h in enumerate(raw_hits, 1)]
    total = (
        stats.get("total_hits", len(items)) if isinstance(stats, dict) else len(items)
    )
    return items, total


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

    mv2 = Path(opts["mv2"]) if opts["mv2"] else MV2_PATH
    if not mv2.exists():
        print(
            f"ERROR: {mv2} not found. Run at least one cycle-close to create it.",
            file=sys.stderr,
        )
        sys.exit(1)

    if opts["timeline"]:
        _run_timeline(opts, mv2)
    else:
        _run_query(opts, mv2)


def _run_query(opts, mv2):
    """Run hybrid search via the memvid SDK.

    Uses `Memvid.ask(context_only=True)` rather than `find()` because only
    `ask()` supports the `since`/`until` date filters we expose here.
    """
    question = opts["question"]
    k = opts["k"]
    since = _parse_date_to_unix(opts.get("since"))
    until = _parse_date_to_unix(opts.get("until"))

    try:
        items, total = _ask_normalized(mv2, question, k, since=since, until=until)
    except Exception as e:
        print(f"ERROR: memvid ask failed: {e}", file=sys.stderr)
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


def _entry_to_dict(entry) -> dict:
    """Normalize an SDK TimelineEntry dataclass into a dict for JSON output."""
    if is_dataclass(entry):
        return asdict(entry)
    if isinstance(entry, dict):
        return entry
    # Fallback: pull known attributes
    return {
        "frame_id": getattr(entry, "frame_id", None),
        "timestamp": getattr(entry, "timestamp", None),
        "preview": getattr(entry, "preview", ""),
        "uri": getattr(entry, "uri", None),
        "child_frames": list(getattr(entry, "child_frames", []) or []),
    }


def _run_timeline(opts, mv2):
    """Show timeline entries via the memvid SDK."""
    k = opts["k"]
    since = opts["since"]
    since_unix = _parse_date_to_unix(since)
    until_unix = _parse_date_to_unix(opts.get("until"))

    try:
        mem = _open_readonly(mv2)
        entries = mem.timeline(
            limit=k,
            since=since_unix,
            until=until_unix,
        )
    except Exception as e:
        print(f"ERROR: memvid timeline failed: {e}", file=sys.stderr)
        sys.exit(1)

    items = [_entry_to_dict(e) for e in (entries or [])]

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
            frame_id = entry.get("frame_id", "?")
            preview = (entry.get("preview") or "")[:80]
            print(f"  [ts={ts}] Frame {frame_id}: {preview}")
        print(f"\n[MEMORY TIMELINE] Done.")


def recall(query: str, k: int = 5, until=None, json_mode: bool = False) -> list:
    """Query long-term memory and return results as a list of dicts.

    Returns a list of result dicts (rank, score, title, snippet, tags, frame_id).
    Returns empty list on any error (file not found, SDK unavailable, etc.).
    Does not print or call sys.exit().
    """
    if memvid_sdk is None or not MV2_PATH.exists():
        return []
    try:
        until_unix = _parse_date_to_unix(until, strict=False)
        items, _ = _ask_normalized(MV2_PATH, query, k, until=until_unix)
        return items
    except Exception:
        return []


if __name__ == "__main__":
    main()
