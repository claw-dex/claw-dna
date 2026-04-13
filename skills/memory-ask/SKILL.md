---
name: memory-ask
description: RAG-powered question answering over memvid .mv2 files. Retrieves adaptive context via memvid CLI, then synthesizes answers using Claude (claude-agent-sdk). Use for natural-language Q&A over agent long-term memory or any ingested knowledge base.
---

# memory-ask

**Path:** `scripts/memory_ask.py`

RAG pipeline that combines memvid CLI retrieval (hybrid lexical + semantic) with Claude synthesis via claude-agent-sdk. Retrieves relevant context from a `.mv2` file, then sends it to Claude for a natural-language answer.

## Arguments

| Flag | Description |
|------|-------------|
| `QUESTION` | Natural-language question (first positional argument, required) |
| `--mv2 PATH` | Path to .mv2 file (default: `/agent/memory/long_term_memory.mv2`) |
| `--k N` | Max results for retrieval (default: 20) |
| `--context-only` | Show retrieved context without Claude synthesis |
| `--json` | Output as JSON |
| `--system PROMPT` | Custom system prompt for Claude |

**Exit codes:** `0` = success, `1` = error

## Examples

```bash
# Ask about agent's past work (uses long_term_memory.mv2 by default)
uv run python scripts/memory_ask.py "What portal work was done recently?"

# Ask using a specific .mv2 file (e.g. an ITEZ product knowledge base)
uv run python scripts/memory_ask.py "What iPhones are available?" --mv2 /agent/workspace/itez_sg.mv2

# Get just the retrieved context (no Claude call)
uv run python scripts/memory_ask.py "corporate leasing" --mv2 /agent/workspace/itez_sg.mv2 --context-only

# JSON output for programmatic use
uv run python scripts/memory_ask.py "MacBook Pro M5 price" --mv2 /agent/workspace/itez_sg.mv2 --json

# Custom system prompt
uv run python scripts/memory_ask.py "warranty policy" --mv2 /agent/workspace/itez_sg.mv2 --system "Answer in bullet points only"
```

## How it works

1. **Retrieve**: Runs `memvid ask --context-only --json` for hybrid (lexical + semantic) retrieval with adaptive scoring
2. **Clean**: Strips internal memvid metadata from retrieved text
3. **Synthesize**: Sends question + cleaned context to Claude via `claude-agent-sdk` with streaming output
4. **Output**: Prints the synthesized answer (text or JSON)

## Dependencies

- `memvid` CLI (installed via `curl -fsSL https://raw.githubusercontent.com/memvid/preflight-installer/main/install.sh | bash`)
- `claude-agent-sdk` (Python package, already in pyproject.toml)