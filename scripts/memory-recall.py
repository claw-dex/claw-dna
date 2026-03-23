#!/usr/bin/env python3
"""
memory-recall.py — Query long-term semantic memory (memvid).

Searches the agent's long-term memory store for entries matching a
natural-language question. Returns relevant context without invoking an LLM.

Usage:
    uv run python scripts/memory-recall.py "What did I work on last week?"
    uv run python scripts/memory-recall.py "portal reliability fixes" --k 10
    uv run python scripts/memory-recall.py "efficiency improvements" --json
    uv run python scripts/memory-recall.py --timeline
    uv run python scripts/memory-recall.py --timeline --since 2026-03-01

Required:
    QUESTION          Natural-language query (first positional argument)

Optional:
    --k N             Number of results to return (default: 5)
    --json            Output as JSON instead of formatted text
    --timeline        Show timeline entries instead of semantic search
    --since DATE      Filter timeline entries since DATE (ISO format)

Exit codes: 0 = success, 1 = error (missing args, file not found, import error)
"""

import json
import sys
from pathlib import Path

MEMORY = Path("/agent/memory")
MV2_PATH = MEMORY / "long_term_memory.mv2"


def parse_args(argv):
    args = argv[1:]
    result = {
        "question": None,
        "k": 5,
        "json_mode": False,
        "timeline": False,
        "since": None,
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
        elif a == "--json":
            result["json_mode"] = True
        elif a == "--timeline":
            result["timeline"] = True
        elif a == "--since" and i + 1 < len(args):
            i += 1
            result["since"] = args[i]
        elif not a.startswith("--") and result["question"] is None:
            result["question"] = a
        i += 1
    return result


def main():
    opts = parse_args(sys.argv)

    if opts["help"]:
        print(__doc__)
        sys.exit(0)

    if not opts["timeline"] and not opts["question"]:
        print("ERROR: QUESTION is required (first positional argument)", file=sys.stderr)
        print("Usage: uv run python scripts/memory-recall.py \"your question here\"", file=sys.stderr)
        sys.exit(1)

    if not MV2_PATH.exists():
        print(f"ERROR: {MV2_PATH} not found. Run at least one cycle-close to create it.", file=sys.stderr)
        sys.exit(1)

    try:
        import memvid_sdk
    except ImportError:
        print("ERROR: memvid-sdk not installed. Run: uv add memvid-sdk", file=sys.stderr)
        sys.exit(1)

    try:
        mem = memvid_sdk.use('basic', str(MV2_PATH), mode='open')
    except Exception as e:
        print(f"ERROR: Failed to open {MV2_PATH}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if opts["timeline"]:
            _run_timeline(mem, opts)
        else:
            _run_query(mem, opts)
    finally:
        mem.seal()


def _run_query(mem, opts):
    """Run semantic search query."""
    question = opts["question"]
    k = opts["k"]

    result = mem.ask(question, context_only=True, k=k, mode="sem")
    context = result.get("context", "") if isinstance(result, dict) else str(result)

    if opts["json_mode"]:
        print(json.dumps({
            "query": question,
            "k": k,
            "context": context,
        }, indent=2))
    else:
        print(f'[MEMORY RECALL] "{question}" (k={k})\n')
        if context:
            print(context)
        else:
            print("No matching memories found.")
        print(f"\n[MEMORY RECALL] Done.")


def _run_timeline(mem, opts):
    """Show timeline entries."""
    k = opts["k"]
    since = opts["since"]

    kwargs = {"limit": k}
    if since:
        kwargs["since"] = since

    entries = mem.timeline(**kwargs)
    items = entries if isinstance(entries, list) else entries.get("entries", [])

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
            meta = entry.get("metadata", {})
            cycle = meta.get("cycle", "?")
            date = (meta.get("date", "") or "")[:16]
            title = entry.get("title", "")[:80]
            label = entry.get("label", "")
            print(f"  [{date}] Cycle {cycle} ({label}): {title}")
        print(f"\n[MEMORY TIMELINE] Done.")


if __name__ == "__main__":
    main()
