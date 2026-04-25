#!/usr/bin/env python3
"""
memory_ask.py — RAG-powered question answering over memvid memory files.

Retrieves adaptive context from a .mv2 file via the `memvid_sdk` Python
package (`Memvid.ask(context_only=True)`), then synthesizes an answer using
Claude via claude-agent-sdk.

Usage:
    uv run python scripts/memory_ask.py "What is the MacBook Pro M5 price?"
    uv run python scripts/memory_ask.py "What portal work was done?" --mv2 /agent/memory/long_term_memory.mv2
    uv run python scripts/memory_ask.py "corporate leasing options" --mv2 /agent/workspace/itez_sg.mv2 --k 10
    uv run python scripts/memory_ask.py "iPhone models" --context-only
    uv run python scripts/memory_ask.py "warranty info" --json

Required:
    QUESTION          Natural-language question (first positional argument)

Optional:
    --mv2 PATH        Path to .mv2 file (default: /agent/memory/long_term_memory.mv2)
    --k N             Max results for retrieval (default: 20)
    --context-only    Show retrieved context without Claude synthesis
    --json            Output as JSON
    --system PROMPT   Custom system prompt for Claude

Exit codes: 0 = success, 1 = error
"""

import asyncio
import json
import sys
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


DEFAULT_MV2 = Path("/agent/memory/long_term_memory.mv2")

SYSTEM_PROMPT = (
    "You are a helpful assistant answering questions based on retrieved context. "
    "Use ONLY the provided context to answer. If the context doesn't contain "
    "enough information, say so clearly. Be concise and direct. "
    "Cite specific details (score, date, name) from the context when available."
)


def parse_args(argv):
    args = argv[1:]
    result = {
        "question": None,
        "mv2": str(DEFAULT_MV2),
        "k": 20,
        "context_only": False,
        "json_mode": False,
        "system_prompt": SYSTEM_PROMPT,
        "help": False,
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            result["help"] = True
        elif a == "--mv2" and i + 1 < len(args):
            i += 1
            result["mv2"] = args[i]
        elif a == "--k" and i + 1 < len(args):
            i += 1
            try:
                result["k"] = int(args[i])
            except ValueError:
                print(
                    f"ERROR: --k must be an integer, got: {args[i]!r}", file=sys.stderr
                )
                sys.exit(1)
        elif a == "--context-only":
            result["context_only"] = True
        elif a == "--json":
            result["json_mode"] = True
        elif a == "--system" and i + 1 < len(args):
            i += 1
            result["system_prompt"] = args[i]
        elif not a.startswith("--") and result["question"] is None:
            result["question"] = a
        i += 1
    return result


def _clean_text(text: str) -> str:
    """Strip internal memvid metadata from a snippet.

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


def retrieve_context(mv2_path, question, k):
    """Retrieve context from a .mv2 file via the memvid SDK."""
    _require_sdk()
    mv2 = Path(mv2_path)
    if not mv2.exists():
        print(f"ERROR: {mv2} not found.", file=sys.stderr)
        sys.exit(1)

    try:
        mem = memvid_sdk.use(
            "basic",
            str(mv2),
            mode="open",
            enable_vec=True,
            enable_lex=True,
            read_only=True,
        )
        result = mem.ask(question, k=k, context_only=True)
    except Exception as e:
        err = str(e)
        # MV004: no lex hits → SDK tried vec fallback → fastembed not compiled in.
        # Treat as zero results rather than a hard failure.
        if "MV004" in err or "Lexical index is not enabled" in err:
            result = {}
        else:
            print(f"ERROR: memvid ask failed: {e}", file=sys.stderr)
            sys.exit(1)

    # SDK returns plain dicts, not dataclasses — use .get() not getattr()
    _rget = (
        (lambda k_, d=None: result.get(k_, d))
        if isinstance(result, dict)
        else (lambda k_, d=None: getattr(result, k_, d))
    )
    hits = list(_rget("hits") or [])
    clean_parts = []
    results = []
    for h in hits:
        _hget = (
            (lambda k_, d=None: h.get(k_, d))
            if isinstance(h, dict)
            else (lambda k_, d=None: getattr(h, k_, d))
        )
        title = _hget("title", "") or ""
        snippet = _clean_text(_hget("snippet", "") or "")
        score = _hget("score", 0.0)
        frame_id = _hget("frame_id")
        if snippet:
            clean_parts.append(f"[{title}]\n{snippet}")
        results.append(
            {
                "title": title,
                "score": score,
                "snippet": snippet,
                "frame_id": frame_id,
            }
        )

    sdk_context = _rget("context", "") or ""
    stats = _rget("stats", {}) or {}
    total_hits = (
        stats.get("total_hits", len(hits)) if isinstance(stats, dict) else len(hits)
    )
    retrieval_ms = stats.get("took_ms") if isinstance(stats, dict) else None

    return {
        "results": results,
        "context": "\n\n---\n\n".join(clean_parts) or sdk_context,
        "total_hits": total_hits,
        "stats": {"retrieval_ms": retrieval_ms} if retrieval_ms is not None else {},
    }


async def ask_claude(question, context, system_prompt):
    """Send question + context to Claude via claude-agent-sdk and return the answer."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )
    from claude_agent_sdk.types import StreamEvent

    prompt = (
        f"<context>\n\n{context}\n\n"
        f"</context>\n\n---\n\n"
        f"<question>\n\n{question}\n\n"
        f"</question>\n\n---\n\n"
        f"Answer the question using only the context above."
    )

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        cwd="/agent",
        allowed_tools=[
            "Glob",
            "Grep",
            "Read",
            "WebFetch",
            "WebSearch",
        ],  # Readonly tool
        disallowed_tools=["AskUserQuestion"],
    )

    sdk = ClaudeSDKClient(options)
    await sdk.connect()

    try:
        await sdk.query(prompt)
        parts = []
        async for msg in sdk.receive_response():
            if isinstance(msg, StreamEvent):
                event = msg.event
                if event.get("type") == "content_block_delta":
                    text = (event.get("delta") or {}).get("text", "")
                    if text:
                        # Stream to stderr for real-time output
                        print(text, end="", file=sys.stderr, flush=True)
                        parts.append(text)
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        if not parts:
                            parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                break
        print("", file=sys.stderr)  # newline after streaming
        return "".join(parts)
    finally:
        await sdk.disconnect()


def main():
    opts = parse_args(sys.argv)

    if opts["help"]:
        print(__doc__)
        sys.exit(0)

    if not opts["question"]:
        print(
            "ERROR: QUESTION is required (first positional argument)", file=sys.stderr
        )
        print(
            'Usage: uv run python scripts/memory_ask.py "your question here"',
            file=sys.stderr,
        )
        sys.exit(1)

    question = opts["question"]
    mv2_path = opts["mv2"]

    # Step 1: Retrieve context
    retrieval = retrieve_context(mv2_path, question, opts["k"])

    if not retrieval["results"]:
        if opts["json_mode"]:
            print(
                json.dumps(
                    {
                        "question": question,
                        "mv2": mv2_path,
                        "answer": None,
                        "context_hits": 0,
                        "message": "No relevant context found.",
                    },
                    indent=2,
                )
            )
        else:
            print(f'[MEMORY ASK] "{question}"\n')
            print("No relevant context found in the memory store.")
        sys.exit(0)

    # Step 2: Context-only mode — just show what was retrieved
    if opts["context_only"]:
        if opts["json_mode"]:
            print(
                json.dumps(
                    {
                        "question": question,
                        "mv2": mv2_path,
                        "context_hits": retrieval["total_hits"],
                        "retrieval_ms": retrieval["stats"].get("retrieval_ms"),
                        "context": retrieval["context"],
                        "results": [
                            {"title": r.get("title", ""), "score": r.get("score", 0)}
                            for r in retrieval["results"]
                        ],
                    },
                    indent=2,
                )
            )
        else:
            print(
                f'[MEMORY ASK] Context for "{question}" '
                f'({retrieval["total_hits"]} hits, '
                f'{retrieval["stats"].get("retrieval_ms", "?")}ms)\n'
            )
            print(retrieval["context"])
        sys.exit(0)

    # Step 3: Synthesize answer with Claude
    if not opts["json_mode"]:
        print(
            f'[MEMORY ASK] "{question}" '
            f'({retrieval["total_hits"]} context hits from {Path(mv2_path).name})\n',
            file=sys.stderr,
        )

    answer = asyncio.run(
        ask_claude(question, retrieval["context"], opts["system_prompt"])
    )

    if opts["json_mode"]:
        print(
            json.dumps(
                {
                    "question": question,
                    "mv2": mv2_path,
                    "answer": answer,
                    "context_hits": retrieval["total_hits"],
                    "retrieval_ms": retrieval["stats"].get("retrieval_ms"),
                },
                indent=2,
            )
        )
    else:
        print(answer)


if __name__ == "__main__":
    main()
