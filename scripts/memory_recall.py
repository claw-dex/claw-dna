#!/usr/bin/env python3
"""
memory_recall.py — Query long-term semantic memory (memvid CLI).

Searches the agent's long-term memory store for entries matching a
natural-language question using the `memvid` CLI (hybrid lexical + semantic).

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

Exit codes: 0 = success, 1 = error (missing args, file not found, CLI error)
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MEMORY = Path("/agent/memory")
MV2_PATH = MEMORY / "long_term_memory.mv2"
MEMVID_BIN = "memvid"


def _check_memvid():
    """Ensure the memvid CLI is available."""
    if not shutil.which(MEMVID_BIN):
        print("ERROR: memvid CLI not found. Install with:", file=sys.stderr)
        print("  curl -fsSL https://raw.githubusercontent.com/memvid/preflight-installer/main/install.sh | bash", file=sys.stderr)
        sys.exit(1)


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
                print(f"ERROR: --k must be an integer, got: {args[i]!r}", file=sys.stderr)
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


def _run_cmd(cmd):
    """Run a memvid CLI command, return parsed JSON output."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"ERROR: {' '.join(cmd[:3])} failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def _parse_date_to_unix(value):
    """Convert a date string (ISO format) or integer to a unix timestamp string.

    Returns the string representation of the unix timestamp, or exits on error.
    """
    try:
        return str(int(value))
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return str(int(dt.timestamp()))
    except ValueError:
        print(f"ERROR: Invalid date format: {value!r}. Use ISO format (2026-03-25) or unix timestamp.", file=sys.stderr)
        sys.exit(1)


def _clean_snippet(text):
    """Strip internal memvid metadata lines from snippet text."""
    lines = []
    for line in text.splitlines():
        if line.startswith(("uri: mv2://", "tags: ", "labels: ",
                            "category: ", "extractous_metadata:", "memvid.",
                            "metadata: {")):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def main():
    opts = parse_args(sys.argv)

    if opts["help"]:
        print(__doc__)
        sys.exit(0)

    if not opts["timeline"] and not opts["question"]:
        print("ERROR: QUESTION is required (first positional argument)", file=sys.stderr)
        print("Usage: uv run python scripts/memory_recall.py \"your question here\"", file=sys.stderr)
        sys.exit(1)

    mv2 = Path(opts["mv2"]) if opts["mv2"] else MV2_PATH
    if not mv2.exists():
        print(f"ERROR: {mv2} not found. Run at least one cycle-close to create it.", file=sys.stderr)
        sys.exit(1)

    _check_memvid()

    if opts["timeline"]:
        _run_timeline(opts, mv2)
    else:
        _run_query(opts, mv2)


def _run_query(opts, mv2):
    """Run hybrid search via memvid CLI."""
    question = opts["question"]
    k = opts["k"]

    cmd = [
        MEMVID_BIN, "find", str(mv2),
        "--query", question,
        "--top-k", str(k),
        "--json",
    ]
    if opts.get("since"):
        cmd.extend(["--since", _parse_date_to_unix(opts["since"])])
    if opts.get("until"):
        cmd.extend(["--until", _parse_date_to_unix(opts["until"])])
    data = _run_cmd(cmd)
    hits = data.get("hits", [])
    total = data.get("metadata", {}).get("total_hits", len(hits))

    # Sort by score descending
    hits.sort(key=lambda h: h.get("score", 0), reverse=True)

    if opts["json_mode"]:
        items = []
        for i, h in enumerate(hits, 1):
            items.append({
                "rank": i,
                "score": h.get("score"),
                "title": h.get("title", ""),
                "snippet": _clean_snippet(h.get("text", "")),
                "tags": h.get("metadata", {}).get("tags", []),
                "frame_id": h.get("frame_id"),
            })
        print(json.dumps({
            "query": question,
            "k": k,
            "total_hits": total,
            "results": items,
        }, indent=2))
    else:
        print(f'[MEMORY RECALL] "{question}" (k={k})\n')
        if not hits:
            print("No matching memories found.")
        else:
            for i, h in enumerate(hits, 1):
                score = h.get("score", 0)
                title = h.get("title", "untitled")
                snippet = _clean_snippet(h.get("text", ""))
                tags = h.get("metadata", {}).get("tags", [])

                cycle_tag = next((t for t in tags if t.startswith("cycle:")), "")
                date_tag = next((t for t in tags if t.startswith("date:")), "")

                header = f"── Result {i}/{len(hits)} (score: {score:.4f})"
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
        print(f"[MEMORY RECALL] {len(hits)} result(s) returned (total matches: {total}).")


def _run_timeline(opts, mv2):
    """Show timeline entries via memvid CLI."""
    k = opts["k"]
    since = opts["since"]

    cmd = [MEMVID_BIN, "timeline", str(mv2), "--json", "--limit", str(k)]
    if since:
        cmd.extend(["--since", _parse_date_to_unix(since)])
    if opts.get("until"):
        cmd.extend(["--until", _parse_date_to_unix(opts["until"])])

    items = _run_cmd(cmd)
    # CLI returns a JSON array for timeline
    if isinstance(items, dict):
        items = items.get("entries", [])

    if opts["json_mode"]:
        print(json.dumps({
            "mode": "timeline",
            "count": len(items),
            "since": since,
            "entries": items,
        }, indent=2, default=str))
    else:
        print(f"[MEMORY TIMELINE] {len(items)} entries" +
              (f" (since {since})" if since else ""))
        print()
        for entry in items:
            ts = entry.get("timestamp", "")
            frame_id = entry.get("frame_id", "?")
            preview = entry.get("preview", "")[:80]
            uri = entry.get("uri", "")
            print(f"  [ts={ts}] Frame {frame_id}: {preview}")
        print(f"\n[MEMORY TIMELINE] Done.")


def recall(query: str, k: int = 5, until=None, json_mode: bool = False) -> list:
    """Query long-term memory and return results as a list of dicts.

    Returns a list of result dicts (rank, score, title, snippet, tags, frame_id).
    Returns empty list on any error (file not found, memvid unavailable, etc.).
    Does not print or call sys.exit().
    """
    if not MV2_PATH.exists():
        return []
    if not shutil.which(MEMVID_BIN):
        return []
    cmd = [
        MEMVID_BIN, "find", str(MV2_PATH),
        "--query", query,
        "--top-k", str(k),
        "--json",
    ]
    if until:
        cmd.extend(["--until", _parse_date_to_unix(until)])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
    except Exception:
        return []
    hits = data.get("hits", [])
    hits.sort(key=lambda h: h.get("score", 0), reverse=True)
    return [
        {
            "rank": i,
            "score": h.get("score"),
            "title": h.get("title", ""),
            "snippet": _clean_snippet(h.get("text", "")),
            "tags": h.get("metadata", {}).get("tags", []),
            "frame_id": h.get("frame_id"),
        }
        for i, h in enumerate(hits, 1)
    ]


if __name__ == "__main__":
    main()