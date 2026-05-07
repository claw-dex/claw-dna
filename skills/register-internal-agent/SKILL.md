---
name: register-internal-agent
description: Register, update, list, or deactivate internal agents via `scripts/register_internal_agent.py`. Use to add a new internal agent, update an existing internal agent's model / system-prompt / responsibilities / outbox routing rules (re-registration upserts), list registered internal agents, or deactivate one. Triggers include "register internal agent", "add internal agent", "update agent <name>", "change model for <agent>", "deactivate <agent>", "list internal agents", "agents.json", "outbox routing rules", "system prompt for <agent>".
---

# register-internal-agent

**Path:** `scripts/register_internal_agent.py`

Adds (or updates) an entry in `memory/agents.json` and creates the per-agent files under `messages/internal/<name>/` and `memory/chat/<name>/`. The `internal_agent_chat` daemon hot-reloads `agents.json` every sweep tick (~10s), so new or modified agents are picked up without a restart.

All SDK options (allowed_tools, permission_mode, cwd, add_dirs, ...) are fixed and identical to `app/chat.py`. Per-agent customization knobs: `responsibilities`, `system_prompt` (appended to the shared pre-built prompt), `outbox_routing_rules`, `model`.

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `--name N` (with other args) | Register a new internal agent or upsert an existing one. Re-registering with the same `--name` clears `deactivated`. |
| `--list` | List all internal agents (name, status, rule count, responsibilities). |
| `--deactivate N` | Set `status=deactivated`. Re-register with the same `--name` to re-activate. |

## Flags

| Flag | Description |
|------|-------------|
| `--name N` | Agent name. Must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$`. |
| `--responsibilities T` | Free-text duties. **Defaults to empty and is written on every call** — always re-pass on upsert (see "Updating" below). |
| `--system-prompt-file PATH` | Read system prompt from file. Appended to the shared system prompt; omit on upsert to preserve existing. |
| `--system-prompt-inline T` | Inline system prompt. Same semantics as `--system-prompt-file`. |
| `--outbox-routing-rules-file PATH` | JSON list of `{"description": "...", "agent": "<name>"}`. Each rule contributes one bullet to the LLM-visible description of the per-session `send_reply` tool. Omit on upsert to preserve existing. |
| `--outbox-routing-rules-inline JSON` | Inline JSON list. Same semantics as `--outbox-routing-rules-file`. |
| `--model M` | Short alias (`haiku`, `sonnet`, `opus`) or a full model id. Omit on upsert to preserve / use SDK default. |
| `--list` | List internal agents (subcommand mode). |
| `--deactivate N` | Mark an internal agent as deactivated (subcommand mode). |

The reserved name `main` is always available as an outbox routing target even with no rules.

**Exit codes:** `0` = success, non-zero = invalid args or write failed.

## Examples

```bash
# Register a new internal agent
uv run python scripts/register_internal_agent.py \
    --name planner \
    --responsibilities "Plan multi-step tasks for the main agent" \
    --system-prompt-file prompts/planner.md \
    --outbox-routing-rules-file prompts/planner_rules.json \
    --model sonnet

# Update model only (preserves system-prompt and routing rules)
uv run python scripts/register_internal_agent.py \
    --name myspec-reviewer \
    --responsibilities "<copy current value>" \
    --model opus
uv run python scripts/interact_with_agent.py clear-session --name myspec-reviewer

# Update system prompt only
uv run python scripts/register_internal_agent.py \
    --name planner \
    --responsibilities "<copy current value>" \
    --system-prompt-file prompts/planner_v2.md
uv run python scripts/interact_with_agent.py clear-session --name planner

# List / deactivate
uv run python scripts/register_internal_agent.py --list
uv run python scripts/register_internal_agent.py --deactivate planner
```

## Updating an existing agent

There is no `--update` flag — updates are done by re-running the script with the same `--name`. The script upserts: it merges new fields over the existing entry (`{**existing, **new}`).

**Field preservation rule:** any field you do *not* pass is **kept**, because the script only includes a field in the merge dict when its argument was provided.

**Exception — `--responsibilities`:** has `default=""` and is written on every call. If you are not changing it, copy the current value from `agents.json` into your command, otherwise it gets blanked.

**Recommended upsert workflow:**

1. Read the current entry so you can copy fields you are preserving:

   ```bash
   uv run python -c "import json; print(json.dumps(next(a for a in json.load(open('memory/agents.json')) if a.get('name')=='<agent>'), indent=2))"
   ```

2. Re-register with the change. Always re-pass `--responsibilities` (see exception above).

3. **For changes that need a fresh SDK session** (model swap, system-prompt change), queue `clear_session` via the `interact-with-agent` skill so the daemon drops the current SDK thread and reconnects with the new config on its next sweep:

   ```bash
   uv run python scripts/interact_with_agent.py clear-session --name <agent>
   ```

   Without this, `agents.json` carries the new value but the live session keeps the old model/prompt until the daemon restarts. Updates to `responsibilities` or `outbox_routing_rules` alone do not need `clear-session` — those are read each sweep.

## How it works

`agents.json` updates go through `services.shared.locked_json_rw`, which takes an exclusive flock on `agents.json.lock` and serializes concurrent writers (other CLI invocations, the daemon's strip-after-apply path on `control` flags). The daemon polls `agents.json` every ~10s and applies new config between turns.

## Don't

- Don't hand-edit `agents.json`. Always go through this script so the locked read-modify-write path is used.
- Don't forget to re-pass `--responsibilities` on upsert. It defaults to empty and overwrites on every call.
- Don't expect a model / system-prompt change to take effect on the live session without `clear-session` — see "Updating" step 3.

## Related

- `interact-with-agent` — send messages and queue `clear-chat` / `clear-session` control flags via `scripts/interact_with_agent.py`.
