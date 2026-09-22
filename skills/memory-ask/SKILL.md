---
name: memory-ask
description: RAG-powered question answering over a LanceDB memory store. Retrieves relevance-filtered context via hybrid BM25 + vector search, then synthesizes answers using Claude (claude-agent-sdk). Use for natural-language Q&A over agent long-term memory or any ingested knowledge base.
---

# memory-ask

**Path:** `scripts/memory_ask.py`

RAG pipeline that combines LanceDB hybrid retrieval (BM25 + bge-small vectors) with Claude synthesis via claude-agent-sdk. Retrieves relevant context from a memory store, then sends it to Claude for a natural-language answer.

## Arguments

| Flag | Description |
|------|-------------|
| `QUESTION` | Natural-language question (first positional argument, required) |
| `--db PATH` | Path to the LanceDB store (default: `/agent/memory/long_term_memory.lancedb`) |
| `--k N` | Max results for retrieval (default: 20) |
| `--min-score F` | Drop results scoring below F of the top hit, 0–1 (default: 0.5) |
| `--context-only` | Show retrieved context without Claude synthesis |
| `--json` | Output as JSON |
| `--system PROMPT` | Custom system prompt for Claude |

**Exit codes:** `0` = success, `1` = error

## Examples

```bash
# Ask about the agent's past work (uses long_term_memory.lancedb by default)
uv run python scripts/memory_ask.py "What portal work was done recently?"

# Ask using a specific store (e.g. an ITEZ product knowledge base)
uv run python scripts/memory_ask.py "What iPhones are available?" --db /agent/workspace/itez_sg.lancedb

# Get just the retrieved context (no Claude call)
uv run python scripts/memory_ask.py "corporate leasing" --db /agent/workspace/itez_sg.lancedb --context-only

# Widen retrieval by relaxing the relevance floor
uv run python scripts/memory_ask.py "warranty policy" --k 30 --min-score 0.2

# JSON output for programmatic use
uv run python scripts/memory_ask.py "MacBook Pro M5 price" --db /agent/workspace/itez_sg.lancedb --json

# Custom system prompt
uv run python scripts/memory_ask.py "warranty policy" --db /agent/workspace/itez_sg.lancedb --system "Answer in bullet points only"
```

## How it works

1. **Retrieve**: hybrid LanceDB query (BM25 + vector), over-fetching `2 × k` candidates so a long tail of weak matches can't crowd out strong ones.
2. **Filter**: drops hits scoring below `--min-score` of the top hit, then trims back to `k`. Scores are relative because hybrid fusion scores have no absolute scale.
3. **Synthesize**: sends question + assembled context to Claude via `claude-agent-sdk`, streaming output to stderr as it arrives.
4. **Output**: prints the synthesized answer (text or JSON).

If nothing clears the relevance floor, the script reports "No relevant context found" and exits `0` — it never invents an answer from an empty context.

## Dependencies

- `lancedb` and `fastembed` (Python packages, in pyproject.toml — install via `uv sync` or `seed/install_memory_deps.sh`)
- `claude-agent-sdk` (Python package, already in pyproject.toml)
