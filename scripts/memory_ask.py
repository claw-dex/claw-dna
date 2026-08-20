#!/usr/bin/env python3
"""
memory_ask.py — RAG-powered question answering over the long-term memory store.

Retrieves context from a LanceDB store via scripts/memory_store.py (hybrid
BM25 + vector search), then synthesizes an answer using Claude via
claude-agent-sdk.

Usage:
    uv run python scripts/memory_ask.py "What is the MacBook Pro M5 price?"
    uv run python scripts/memory_ask.py "What portal work was done?" --db /agent/memory/long_term_memory.lancedb
    uv run python scripts/memory_ask.py "corporate leasing options" --db /agent/workspace/itez_sg.lancedb --k 10
    uv run python scripts/memory_ask.py "iPhone models" --context-only
    uv run python scripts/memory_ask.py "warranty info" --json

Required:
    QUESTION          Natural-language question (first positional argument)

Optional:
    --db PATH         Path to the LanceDB store (default: /agent/memory/long_term_memory.lancedb)
    --k N             Max results for retrieval (default: 20)
    --min-score F     Drop results scoring below F of the top hit, 0..1 (default: 0.5)
    --context-only    Show retrieved context without Claude synthesis
    --json            Output as JSON
    --system PROMPT   Custom system prompt for Claude

Exit codes: 0 = success, 1 = error
"""

import asyncio
import json
import sys
import time
from pathlib import Path

from scripts import memory_store as store
from scripts.memory_store import DEFAULT_DB

# Same sys.path bootstrap as app/chat.py so the bare-name `import shared`
# style resolves to services/shared.py (the SDK transport tuning lives there
# so all three SDK call sites stay in lock-step).
_SERVICES_DIR = str(Path(__file__).resolve().parent.parent / "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)
from shared import sdk_buffer_size_kwargs  # noqa: E402

# Keep hits whose score is at least half the top hit's. Retrieval is scored
# relative to the best match rather than on an absolute scale, because hybrid
# fusion scores depend on how many candidates were merged.
DEFAULT_MIN_SCORE = 0.5

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
        "db": str(DEFAULT_DB),
        "k": 20,
        "min_score": DEFAULT_MIN_SCORE,
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
        elif a == "--db" and i + 1 < len(args):
            i += 1
            result["db"] = args[i]
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


def retrieve_context(db_path, question, k, min_score=DEFAULT_MIN_SCORE):
    """Retrieve context for a question from the LanceDB store.

    Over-fetches (2x k) and then trims to k after the relevance filter, so a
    long tail of weak matches cannot crowd out strong ones.
    """
    db_file = Path(db_path)
    try:
        tbl = store.open_table(db_file)
    except Exception as e:
        print(f"ERROR: cannot open {db_file}: {e}", file=sys.stderr)
        sys.exit(1)
    if tbl is None:
        print(f"ERROR: {db_file} not found.", file=sys.stderr)
        sys.exit(1)

    started = time.monotonic()
    try:
        rows = store.search(tbl, question, k=k * 2, min_score=min_score)[:k]
    except Exception as e:
        print(f"ERROR: memory search failed: {e}", file=sys.stderr)
        sys.exit(1)
    retrieval_ms = int((time.monotonic() - started) * 1000)

    context_parts = []
    results = []
    for row in rows:
        title = row.get("title") or ""
        snippet = row.get("text") or ""
        if snippet:
            context_parts.append(f"[{title}]\n{snippet}")
        results.append(
            {
                "title": title,
                "score": row.get("score", 0.0),
                "snippet": snippet,
                "id": row.get("id"),
            }
        )

    return {
        "results": results,
        "context": "\n\n---\n\n".join(context_parts),
        "total_hits": len(results),
        "stats": {"retrieval_ms": retrieval_ms},
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
        # Raise the stdout buffer ceiling: the SDK default (1 MiB) is smaller
        # than a single large tool result and fails the turn with "JSON
        # message exceeded maximum buffer size".
        **sdk_buffer_size_kwargs(ClaudeAgentOptions),
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
    db_path = opts["db"]

    # Step 1: Retrieve context
    retrieval = retrieve_context(
        db_path, question, opts["k"], min_score=opts["min_score"]
    )

    if not retrieval["results"]:
        if opts["json_mode"]:
            print(
                json.dumps(
                    {
                        "question": question,
                        "db": db_path,
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
                        "db": db_path,
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
            f'({retrieval["total_hits"]} context hits from {Path(db_path).name})\n',
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
                    "db": db_path,
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
