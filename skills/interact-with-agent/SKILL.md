---
name: interact-with-agent
description: Operator CLI for runtime maintenance of registered agents. Use to send a message to a registered internal/external agent inbox (including the reserved `main` inbox), clear an internal agent's chat history, or reset an internal agent's SDK session. Triggers include "send message to <agent>", "clear chat for <agent>", "clear session for <agent>", "reset agent session", "queue control flag", "operator CLI", "interact_with_agent", "agent inbox", "agents.json control flag".
---

# interact-with-agent

**Path:** `scripts/interact_with_agent.py`

Operator CLI for runtime maintenance of agents registered in `memory/agents.json`. Wraps inbox writes (`services.shared.write_to_inbox`, atomic + flock + dedup) and queues control flags consumed by the `internal_agent_chat` daemon (~10s sweep). External agents are skipped with exit 0 on `clear-chat` / `clear-session`. Unknown agent names exit 1.

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `send-message` | Append a message envelope to a registered agent's `inbox.json` (or `main` for the main inbox). Works for internal and external agents. |
| `clear-chat` | Internal agents only. Archive `memory/chat/<name>/chat_history.json` to `chat_history_archive.json`, then wipe the live file and the in-memory tail. Queued via `control.clear_chat=true` in `agents.json`. |
| `clear-session` | Internal agents only. Wipe `memory/chat/<name>/<name>.session` so the next turn starts a fresh SDK thread (no `resume=`). Queued via `control.clear_session=true`. Use after a `--model` or system-prompt change. |

## Flags

| Flag | Description |
|------|-------------|
| `--name N` | Target agent name. Required for `send-message` (use `main` for the main inbox). For `clear-chat` / `clear-session`, mutually exclusive with `--all`. |
| `--all` | `clear-chat` / `clear-session` only. Apply to every active internal agent. |
| `--content T` | `send-message` body text. Mutually exclusive with `--content-file`. |
| `--content-file PATH` | `send-message` body read from file. |
| `--type T` | `send-message` envelope type (default: `message`). Internal agents only process `type=message`. |
| `--source S` | `send-message` source label (default: `operator-cli`). |
| `--priority N` | `send-message` priority 1–5 (default: `3`). |
| `--reply-to PATH` | `send-message` reply-to path (default: `messages/inbox.json`). Path string, not a message id. |
| `--id ID` | `send-message` override the generated uuid4 message id. |
| `--subject S` | `send-message` optional subject (used by internal-agent prompts). |
| `--from F` | `send-message` optional `from` field (used by internal-agent prompts). |
| `--no-dedup` | `send-message` disable type+content dedup in `write_to_inbox`. |

**Exit codes:** `0` = success / queued (or no-op for external on clear-*), `1` = invalid args, unknown name, or write failed.

## Examples

```bash
# Send a message to a registered internal agent
uv run python scripts/interact_with_agent.py send-message \
    --name planner --content "Please summarize today's cycle"

# Send to the main agent inbox
uv run python scripts/interact_with_agent.py send-message \
    --name main --content "status?" --priority 2

# Body from file, with subject and from
uv run python scripts/interact_with_agent.py send-message \
    --name planner --content-file /tmp/body.md \
    --subject "weekly plan" --from operator

# Disable dedup (allow identical repeats)
uv run python scripts/interact_with_agent.py send-message \
    --name planner --content "ping" --no-dedup

# Clear chat history (one agent / all active internal agents)
uv run python scripts/interact_with_agent.py clear-chat --name planner
uv run python scripts/interact_with_agent.py clear-chat --all

# Reset SDK session — use after model / system-prompt changes
uv run python scripts/interact_with_agent.py clear-session --name planner
uv run python scripts/interact_with_agent.py clear-session --all
```

## How it works

1. **`send-message`** builds a JSON envelope (`id`, `type`, `content`, `source`, `priority`, `timestamp`, `reply_to`, optional `subject` / `from`) and appends it via `services.shared.write_to_inbox`. The writer takes an exclusive flock on the inbox, dedups on `type+content` unless `--no-dedup` is set, and writes atomically.
2. **`clear-chat` / `clear-session`** set a flag in the target agent's `control` dict in `agents.json` using `locked_json_rw` (flock on `agents.json.lock`). The `internal_agent_chat` daemon reads `agents.json` on its sweep tick (~10s), applies the flag between turns, archives/wipes the relevant file, then strips the flag. Both writers go through the same lock so concurrent updates do not interleave.

## Don't

- Don't hand-edit `agents.json` to set `control` flags — always go through this CLI for the locked read-modify-write path.
- Don't write directly to an inbox file — use `send-message` so dedup and locking are honored.

## Related

- `register-internal-agent` — register / upsert / list / deactivate internal agents in `agents.json`.
