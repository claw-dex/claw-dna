# AGENTS.md

Agent-specific instructions loaded by the AI coding agent at session start.

## Directory Structure

```
/agent/                          ← WORKDIR, your home base
├── agent.sh                     ← AI coding agent CLI wrapper (supports multiple backends, immutable)
├── AGENTS.md                    ← This file (agent instructions, directory tree, skills, credentials)
├── bootstrap.sh                 ← PID 1 process manager (entrypoint, immutable)
├── heartbeat.sh                 ← Heartbeat loop invoked on this script in intervals (immutable)
├── server.py                    ← Streamlit portal entry point (hot-reloads on edit, invalid python code will break the portal)
├── Caddyfile                    ← Caddy gateway initial config (DO NOT EDIT — use Caddy API instead)
├── system.md                    ← Agent system prompt (read-only, immutable)
├── constitution.md              ← Immutable rules (read-only, chmod 444)
├── pyproject.toml               ← Python dependencies (if edit then `uv sync`)
│
├── app/                         ← Streamlit portal pages & modules
│
├── prompts/                     ← Prompt files to drive agent behaviors
│
├── scripts/                     ← Utility/maintenance & skill scripts
│
├── memory/                      ← Persistent agent memory (cycle/journal/goal/dream/memories)
│
├── messages/                    ← Message queues (inbox/outbox)
│
├── services/                    ← Long-running background services (managed by service_manager.py)
│
├── web/                         ← Static files served by Caddy at / (PUBLIC — exposed to user browser)
│
├── workspace/                   ← Scratch space for agent work (PUBLIC — browsable at /_/agent/workspace/)
│
├── skills/                      ← Agent skills (skill docs and instructions)
│
├── .claude/                     ← Claude Code CLI configuration (contain its settings)
│
└── .streamlit/                  ← Streamlit configuration
```

## Searching `/agent/`

Use the `search` skill to find file and content (text based) anywhere under `/agent/` — journals, inbox/outbox message, goals, prompts, scripts, notes, code. It runs a `ripgrep` exact-match pre-filter, then BM25S re-ranks the matches and returns relevance-scored snippets. Prefer it over ad-hoc `grep`/`rg` when you need ranked/relevant results across entire `/agent/` folder.

## Telegram Bridge

A background service that bridges Telegram messages to the agent's inbox/outbox queue.
For setup instructions, configuration, and usage details, read the source file directly:
`services/telegram_bridge.py` — it contains inline documentation covering bot token setup,
chat ID configuration, environment variables, and the message flow.

## Internal Agent Chat

Headless daemon hosting one long-lived `claude_agent_sdk` session per registered internal agent in `/agent/memory/agents.json`. No HTTP surface — inbound messages are file-backed at `/agent/messages/internal/<name>/inbox.json`; transcripts live under `/agent/memory/chat/<name>/`. Outbound delivery is driven by the LLM via the per-session `mcp__internal_agent_routing__send_reply` MCP tool. See `services/internal_agent_chat.py` for the full per-message flow and setup command.

## External Agent API

HTTP service on port **8083** (Caddy proxies `/external-agent/*` with basic auth) that lets remote agents exchange messages with the main agent via per-agent files at `/agent/messages/external/<name>/{inbox,outbox}.json`. Routes: `POST /read-inbox`, `POST /write-outbox`, `GET /ping`, `POST /upload` (≤25 MB, lands in `/agent/workspace/upload/<name>/`), `POST /update` (status/capabilities/responsibilities). Agent identity is supplied via the `X-Agent-Name` header. Registry: `/agent/memory/agents.json`. See `services/external_agent_api.py` for full route semantics.

## Scheduler Daemon

Long-running service that polls `/agent/memory/scheduled_tasks.json` every `SCHEDULER_DAEMON_POLL_SECONDS` (default 30s) and injects due tasks into `/agent/messages/inbox.json` — decoupled from heartbeat cycle frequency. Reuses the same atomic `check_and_inject()` used by `scripts/scheduler.py --check`. Auto-started by `service_manager.py`; writes `/agent/memory/heartbeats/scheduler_daemon.heartbeat` each tick. See `services/scheduler_daemon.py`.

## Metrics Daemon

Long-running service that keeps the DuckDB metrics store at `/agent/memory/metrics.duckdb` in sync with the JSON files it derives from, polling every `METRICS_DAEMON_POLL_SECONDS` (default 300s, floor 60s). Each tick calls `metrics_db.refresh()`, a no-op unless a `(mtime, size)` fingerprint of the sources changed; rebuilds go to a temp file and are atomically swapped in, so the portal's read-only connections never see a partial database. `scripts/cycle_close.py` also dispatches a one-shot refresh so the Overview tab is current the moment a cycle lands.

The portal reads this store through `app/data/metrics.py` — read-only `SELECT`s against pre-computed `metric_*` tables. **No metric is derived at render time anywhere in the portal.** State and status still come straight from JSON: agent status, heartbeat, cycle number, current goal, service liveness, queue depths, and raw log/error content are read live and are not metrics.

Collection is pluggable — any module under `services/metrics/` contributes its own tables to the same build. **→ See the `metrics-daemon-handler` skill** to add one.

**Shipped handler — `services/metrics/usage.py`**: token spend (requests, input / output / cache tokens, by model and day) parsed from the cycle transcripts under `/agent/memory/transcripts/`, surfaced as "Token Usage" on the System tab.

## Webhook Receiver

Generic HTTP webhook handler on port **8082** (Caddy proxies `/webhook/*`). POST/PUT/DELETE/PATCH payloads are recorded to `/agent/messages/inbox.json` as `type="event"`, `source="webhook"` and logged for audit; GET/HEAD/OPTIONS return 200 without recording. Path-prefix sub-handlers (registered in `HANDLERS`, e.g. `services/webhook/whatsapp_bridge_handler.py`) can take over all methods on a prefix and bypass the default inbox-writing behavior. See `services/webhook_receiver.py`.
