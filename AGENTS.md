# AGENTS.md

Agent-specific instructions loaded by the AI coding agent at session start.

## Directory Structure

```
/agent/                          ← WORKDIR, your home base
├── agent.sh                     ← AI coding agent CLI wrapper (supports multiple backends)
├── AGENTS.md                    ← This file (agent instructions, directory tree, skills, credentials)
├── bootstrap.sh                 ← PID 1 process manager (entrypoint)
├── heartbeat.sh                 ← Heartbeat loop invoked by bootstrap
├── server.py                    ← Streamlit entry point (hot-reloads on edit, invalid python code will break the portal)
├── Caddyfile                    ← Caddy gateway initial config (DO NOT EDIT — use Caddy API)
├── system.md                    ← Agent system prompt (read-only, immutable)
├── constitution.md              ← Immutable rules (read-only, chmod 444)
├── pyproject.toml               ← Python dependencies (edit, then `uv sync`)
│
├── app/                         ← Streamlit portal pages & modules
│   ├── __init__.py
│   ├── chat.py                  ← Chat UI page
│   ├── commands_tab.py           ← Command Center tab (goal/message form, run-script, goals, inbox/outbox)
│   ├── credential_tab.py        ← Credentials tab (KeePass UI)
│   ├── data/                    ← Data loading & write utilities (mtime-cached)
│   ├── emails_tab.py            ← Email tab (Gmail via Google Workspace CLI)
│   ├── glance.py                ← Quick-glance dashboard (Goals/Inbox/Outbox summary above chat)
│   ├── memory_tab.py            ← Memory tab (journal, logs, goals, memory files, search)
│   ├── overview_tab.py          ← Overview tab (activity, goal stats, evolution balance)
│   ├── services_tab.py          ← Services & Cron tab (service management, scheduled tasks)
│   ├── shared.py                ← Shared helpers across pages
│   ├── system_tab.py            ← System tab (health, diagnostics, scripts)
│   └── workspace_tab.py         ← Workspace tab (file upload, Caddy file browser)
│
├── prompts/                     ← Prompt templates for agent behaviors
│   ├── bootstrap.md             ← First-boot initialization prompt
│   ├── cycle-close.md           ← End-of-cycle close-out prompt
│   ├── goal.md                  ← Goal execution prompt
│   ├── post-goal-review.md      ← Post-goal review prompt
│   ├── evolve.md                ← Self-evolution prompt
│   ├── self-heal.md             ← Self-healing / recovery prompt
│   ├── error-triage.md          ← Error triage prompt
│   ├── research.md              ← Research task prompt
│   └── server.md                ← Portal architecture prompt
│
├── scripts/                     ← Utility & maintenance scripts
│   ├── cycle_start.py
│   ├── cycle_close.py
│   ├── cycle_report.py
│   ├── memory_backup.py
│   ├── memory_ingest.py
│   ├── memory_repair.py
│   ├── memory_stats.py
│   ├── memory_sync.py
│   ├── memory_recall.py
│   ├── memory_ask.py
│   ├── journal_archive.py
│   ├── maintain.py
│   ├── milestone_report.py
│   ├── metrics_collector.py
│   ├── portal_config.py
│   ├── keepass.py
│   ├── callmebot.py              ← CallMeBot voice call escalation
│   ├── email_imap.py
│   ├── scheduler.py
│   ├── self_test.py
│   ├── service_manager.py
│   ├── server_restart.sh
│   ├── log_cleanup.sh
│   └── health_check.sh
│
├── memory/                      ← Persistent agent memory (survives commits)
│   ├── state.json               ← Current cycle state & status
│   ├── journal.json             ← Cycle-by-cycle journal entries
│   ├── cycles.json              ← Complete cycle history with durations
│   ├── goal.json                ← Active goal tracking
│   ├── server_errors.json      ← Tab crash errors (auto-logged by portal)
│   └── logs/                    ← Cycle and service logs
│
├── messages/                    ← Message queues (inbox/outbox)
│   ├── inbox.json               ← Incoming commands (goal, message)
│   └── outbox.json              ← Outgoing messages to user
│
├── services/                    ← Long-running background services (managed by service_manager.py)
│   ├── shared.py                ← Shared utilities for services (atomic writes, locking, messaging)
│   ├── telegram_bridge.py       ← Telegram ↔ inbox/outbox bridge
│   ├── webhook_receiver.py      ← Incoming webhook handler (port 8082, auto-start)
│   └── whatsapp_bridge.py       ← WhatsApp ↔ inbox/outbox bridge
│
├── web/                         ← Static files served by Caddy at / (PUBLIC — exposed to user browser)
│   └── index.html               ← Welcome page (auto-redirects to /app/)
│
├── workspace/                   ← Scratch space for agent work (PUBLIC — browsable at /_/agent/workspace/)
│
├── skills/                      ← Agent skills (skill docs and instructions)
│
├── .claude/                     ← Claude Code CLI configuration (symlinks for backward compatibility)
│   ├── CLAUDE.md                ← Symlink → /agent/AGENTS.md
│   ├── settings.json            ← Claude Code CLI settings
│   └── skills/                  ← Symlink → /agent/skills/
│
└── .streamlit/                  ← Streamlit configuration
    └── config.toml              ← Streamlit settings (Streamlit hot-reloads, incorrect settings may break the portal)
```
## Post-Goal Learning

After completing any goal, follow the full review process in `prompts/post-goal-review.md`.
This includes documenting learnings (approach, key decisions, reusable patterns, pitfalls)
in the journal entry for the cycle. Always review recent journal entries before starting
a new goal to leverage past learnings.

## Script & Skill

When you created a new script in `scripts/`, you MUST also create a corresponding skill in `skills/<script-name>/SKILL.md` with frontmatter (`name`, `description`) and body content (path, arguments, examples)
Never save one-off scripts in the `scripts/` directory - they won't be tracked, documented, or reusable.

## Scheduled Tasks

The scheduler evaluates `/agent/memory/scheduled_tasks.json` and injects due tasks
into `inbox.json` as goal-type commands. It is run by `heartbeat.sh` via `--check`
before each heartbeat cycle.

- **Quick ref**: `uv run python scripts/scheduler.py --check` (inject due tasks) | `--list` (show all)
- **Full docs**: See the `scheduler` skill for schema, schedule types, and task creation guide

## Credential Management (KeePass)

The agent has a built-in KeePass credential store for managing secrets, API keys,
passwords, and other sensitive data.

- **Database**: `/home/agent/.keepass/credentials.kdbx` (no password, no keyfile)
- **Portal UI**: Credentials tab in the Streamlit portal (search, add, edit, delete)
- **CLI**: `uv run python scripts/keepass.py <command>` — commands: init, store, get, list, search, groups, delete
- **Full docs**: See the `keepass` skill for all flags and examples

### Security Notes

- The KeePass database has no password — the Docker container is the security boundary
- Passwords are returned in plaintext by `get` and `--json` — do not log output publicly
- The database file is NOT in `/agent/web/` or `/agent/workspace/` — it is not browsable
- The database path (`/home/agent/.keepass/`) is outside the Caddy file-server root

## Telegram Bridge

A background service that bridges Telegram messages to the agent's inbox/outbox queue.
For setup instructions, configuration, and usage details, read the source file directly:
`services/telegram_bridge.py` — it contains inline documentation covering bot token setup,
chat ID configuration, environment variables, and the message flow.
