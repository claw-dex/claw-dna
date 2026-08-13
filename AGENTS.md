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

Long-running service that keeps the DuckDB metrics store at `/agent/memory/metrics.duckdb` in sync with the JSON files it derives from, polling every `METRICS_DAEMON_POLL_SECONDS` (default 300s, floor 60s — sources change on the heartbeat cadence, so a tighter poll would only burn stat() calls). Each tick calls `metrics_db.refresh()`, which compares a `(mtime, size)` fingerprint of every source and is a no-op when nothing changed. Rebuilds go to `<db>.tmp` and are atomically `os.replace()`d in, so the portal's read-only connections never see a partial database. `scripts/cycle_close.py` also dispatches a one-shot `metrics_db.py --refresh` so the Overview tab is current the moment a cycle lands. Auto-started by `service_manager.py`; writes `/agent/memory/heartbeats/metrics_daemon.heartbeat` each tick. See `services/metrics_daemon.py` and `scripts/metrics_db.py`.

The portal reads this store through `app/data/metrics.py` (read-only `SELECT`s against pre-computed `metric_*` tables). No metric is derived at render time anywhere in the portal:

| Surface | Metrics served from DuckDB |
|---|---|
| `app/overview_tab.py` | health strip, daily glance, suggestions, evolution balance, goal performance, cycle velocity, improvements |
| `server.py` header | cycle velocity, portal health (24h errors) |
| `app/memory_tab.py` | Memory Overview counts + LanceDB store size |
| `app/system_tab.py` | memory-file size/age/health table, workspace size |
| `app/agents_tab.py` | per-agent error totals and recent-window counts |

State and status still come straight from JSON — agent status, heartbeat, cycle number, current goal, service liveness, queue depths, and raw log/error content are read live and are not metrics.

### Adding a metrics handler

Collection is pluggable. Any module under `services/metrics/` that satisfies the contract in `services/metrics/base.py` contributes its own tables to the same build — there is no registration list to edit:

```python
NAME   = "mymetric"
TABLES = ["metric_mymetric"]
SCHEMA = ["CREATE TABLE metric_mymetric (day VARCHAR, n BIGINT)"]

def fingerprint(ctx):          # optional — skip the rebuild when nothing moved
    return str(my_source_mtime)

def collect(ctx):              # ctx: agent dirs, build `now`, core sources,
    return HandlerResult(      #      previous meta + rows for carry-forward
        tables={"metric_mymetric": [("2026-08-13", 42)]},
        meta={"total": 42},    # stored namespaced as "mymetric.total"
    )
```

The collector owns the single write connection and the atomic swap, so a handler never touches DuckDB itself. Handler DDL runs with the core schema, so its tables exist even when `collect` raises; a failing handler is isolated and recorded in `metric_handler_status` (surfaced on the System tab) instead of taking the store down. `ctx.previous_rows(table)` returns the last build's rows, which is what makes incremental handlers possible.

**Shipped handler — `services/metrics/usage.py`**: token usage parsed from `/agent/memory/transcripts/cycle-*.jsonl` (and the `.jsonl.gz` form). It deduplicates on `message.id` at two levels, both of which matter:

- *Within* a transcript — one API response is written once per content block (thinking / text / tool_use), each repeating the same `message.usage`. Summing raw inflates output tokens by ~1.8x.
- *Across* transcripts — `heartbeat.sh:483` resumes an in-progress goal's session and `heartbeat.sh:561` copies the **whole** session file to `cycle-<N>.jsonl` every cycle, so a goal spanning k cycles produces k transcripts each a superset of the last. Requests are therefore attributed to the first cycle whose transcript contained them; per-file counting would report every early request k times.

Real transcripts hold the agent's reasoning, cwd, and branch names and must never be committed (`/examples/` and `*.jsonl.txt` are gitignored). The parser is tested against synthetic replicas in `test/fixtures/transcripts/`, which reproduce the CLI's record shape field for field — regenerate with `uv run python test/fixtures/make_transcripts.py`. The generator emits a `manifest.json` of the totals it *intended* to write, so the parser is checked against generator intent rather than against itself.

Finished transcripts never change, so per-file `(fname, size, mtime)` identity carries already-attributed rows forward — steady state re-reads only the cycle that just ran. `metric_usage_daily` rows for days whose cycles have aged out of the `METRICS_USAGE_CYCLES` window (default 200) are frozen rather than recomputed, so daily history outlives the window. Tables: `metric_usage_files`, `metric_usage_requests` (one row per distinct API response — the carry-forward grain), `metric_usage_cycles` (cycle × model × effort × tier × speed), `metric_usage_daily`. Surfaced as "Token Usage" on the System tab.

## Webhook Receiver

Generic HTTP webhook handler on port **8082** (Caddy proxies `/webhook/*`). POST/PUT/DELETE/PATCH payloads are recorded to `/agent/messages/inbox.json` as `type="event"`, `source="webhook"` and logged for audit; GET/HEAD/OPTIONS return 200 without recording. Path-prefix sub-handlers (registered in `HANDLERS`, e.g. `services/webhook/whatsapp_bridge_handler.py`) can take over all methods on a prefix and bypass the default inbox-writing behavior. See `services/webhook_receiver.py`.
