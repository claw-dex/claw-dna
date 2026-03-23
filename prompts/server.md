# Agent Web Portal — Architecture Reference

## Multi-Service Gateway (v1)

```
User Browser → localhost:8080 (Caddy Gateway)
                  ├── /          → redirect to /app/
                  └── /app/*     → localhost:8081 (Streamlit, baseUrlPath=/app/)

bootstrap.sh (PID 1) — process manager with watchdog
  ├── caddy run (port 8080, admin API on 2019)
  └── uv run streamlit run server.py --server.baseUrlPath /app/ --server.enableCORS=false --server.enableXsrfProtection=false (port 8081)
```

| Service | Port | URL Path | Notes |
|---------|------|----------|-------|
| Caddy | 8080 | `/` (redirect), `/app/*` | Gateway, admin API on 2019 |
| Streamlit | 8081 | `/app/` (via Caddy) | Hot-reload, baseUrlPath=/app/ |

**Caddy admin API:** `http://localhost:2019/config/` (JSON API for dynamic route configuration, internal only)
**Gateway config:** `/agent/Caddyfile`

## Streamlit Portal

**Entry point:** `server.py`
**Framework:** Streamlit
**Hot-reload:** Built-in (`runOnSave = true` in `.streamlit/config.toml`)
**Port:** `8081` (configured in `.streamlit/config.toml`)
**Health check:** `GET http://localhost:8081/app/_stcore/health` → returns `"ok"` when ready

### File Layout

```
server.py          — Streamlit entry point: page config, header, always-visible sections, tab registry
.streamlit/
  config.toml      — runOnSave=true, port=8081, headless=true, dark theme
Caddyfile          — Caddy gateway configuration
app/
  __init__.py      — Package marker
  shared.py        — Path constants + _write_json_atomic + _startup_check + AtomicJSON
  chat.py          — Chat interface (always visible above tabs, persistent async Claude SDK)
  glance.py        — Quick Glance dashboard (always visible above tabs, Goals/Inbox/Outbox summary)
  commands.py      — Command Center tab (PROTECTED — do NOT modify)
  memory_tab.py    — Memory tab (journal, logs, goals, memory files, search)
  system.py        — System tab (health, diagnostics, scripts)
  services_tab.py  — Services & Cron tab (service management, scheduled tasks)
  overview.py      — Agent Overview tab (activity, goal stats, evolution balance)
  workspace.py     — Workspace tab (file upload, Caddy file browser)
  credential.py    — Credentials tab (KeePass database management)
  emails.py        — Email tab (Google Workspace OAuth setup)
  data/            — Data loading package (mtime-based caching + write operations)
    __init__.py    — Re-exports all public functions for backward compatibility
    _cache.py      — Cache infrastructure (TTL cache, mtime decorators, auto-registration)
    _helpers.py    — Internal helpers (_read_json_safe)
    state.py       — load_state, load_services, load_services_full
    goal.py        — load_goals, load_goal_stats
    cycle.py       — load_cycles, load_cycle_velocity, load_cycle_logs, load_cycle_log_content, load_balance, load_activity
    journal.py     — load_journal (merged journal + archive, paginated)
    message.py     — load_inbox, load_outbox, load_outbox_history, load_history
    log.py         — load_logs, load_log_detail
    memory.py      — load_memory_files, read_memory_file
    workspace.py   — load_workspace_files, read_workspace_file
    system.py      — load_errors, load_system_info, load_validate, load_plugins, load_scheduled_tasks
    script.py      — load_scripts
    search.py      — search (full-text across all sources)
    suggest.py     — load_suggest (ranked action suggestions)
    write.py       — All write operations (14 functions)
```

### Always-Visible Sections (above tabs)

Two modules render above the tab strip in `server.py` (after the header, before the tab groups). Both are always visible regardless of which tab is selected:

1. **Quick Glance** (`app/glance.py`) — compact Goals/Inbox/Outbox summary with unified timeline (last 10 items, filterable)
2. **Chat** (`app/chat.py`) — persistent async chat interface using `claude-agent-sdk` with streaming, session resume, and cost tracking

### Tab Registry (TAB_REGISTRY in server.py ~line 20)

8 tabs in 2 groups. Each entry is `(label, module, display_name[, group])`. The 4th `group` element is optional (defaults to `"General"`). With multiple groups, a `st.segmented_control` bar lets users switch groups, with `st.tabs` inside each group.

| Label | Module | Group |
|-------|--------|-------|
| Command Center | commands | Agent Console |
| Memory | memory_tab | Agent Console |
| System | system | Agent Console |
| Services & Cron | services_tab | Agent Console |
| Agent Overview | overview | Agent Console |
| Workspace | workspace | Core |
| Credentials | credential | Core |
| Email | emails | Core |

### Header Metrics

7-column header bar rendered in `server.py` after the title:

| Metric | Source | Notes |
|--------|--------|-------|
| Status | `state.json` → `status` | Color-coded pill (idle=yellow, running=green, healing=red, bootstrapping=blue, awaiting_first_heartbeat=white) |
| Cycle | `state.json` → `cycle_number` | Current cycle number |
| Heartbeat | `state.json` → `last_heartbeat` | Freshness icon: green <5m, yellow 5-30m, red >30m |
| Velocity | `load_cycle_velocity()` | Cycles per hour (rolling last 10 completed) |
| Services | `load_services()` | Running/total count with status icon |
| Current Goal | `state.json` → `current_goal` | Truncated to 60 chars with tooltip |
| Portal Health | `load_errors()` | 24h error count with severity icon |

### Auto-Refresh

`streamlit-autorefresh` polls at two rates:
- **1 second** when `st.session_state["chat_streaming"]` is `True` (fast polling during chat)
- **60 seconds** otherwise (normal idle refresh)

### Splash Screen

On first session load (`app_initialized` not in session state), a `hydralit_components` loader animation is shown while `_startup_check()` runs. Subsequent page loads skip the animation but still call `_init()` (cached via `@st.cache_resource`).

### First-Run Setup

When `cycle_number == 0`, the portal shows a setup screen instead of the main UI:

1. **Gate:** If `AGENT_CREDENTIALS_PATH` does not exist, shows authentication instructions and blocks
2. **Goal input:** Text area + timezone selector (from `zoneinfo.available_timezones()`)
3. **On submit:** Calls `write_first_goal()`, `save_portal_config("timezone", tz)`, and `trigger_bootstrap_heartbeat()`
4. **Progress:** Tracks via `st.session_state["bootstrap_triggered"]`, shows status until agent completes bootstrap

### Key Paths

Defined in `app/shared.py`:

| Constant               | Value                                | Purpose                          |
|------------------------|--------------------------------------|----------------------------------|
| `AGENT_DIR`            | `/agent`                             | Root agent directory             |
| `MEMORY_DIR`           | `/agent/memory`                      | State, journal, capabilities     |
| `LOGS_DIR`             | `/agent/memory/logs`                 | Cycle and service logs           |
| `MESSAGES_DIR`         | `/agent/messages`                    | Inbox/outbox message queues      |
| `SCRIPTS_DIR`          | `/agent/scripts`                     | Utility scripts (.py, .sh)       |
| `GOALS_PATH`           | `/agent/memory/goal.json`            | Persistent goal tracker          |
| `HISTORY_PATH`         | `/agent/memory/command_history.json` | Command history (last 50)        |
| `ERROR_LOG_PATH`       | `/agent/memory/server_errors.json`   | Server error log (last 20)       |
| `CHAT_HISTORY_PATH`    | `/agent/memory/chat_history.json`    | Chat message history (max 200)   |
| `PORTAL_CONFIG_PATH`   | `/agent/memory/portal_config.json`   | Portal settings (timezone, etc.) |
| `AGENT_CREDENTIALS_PATH` | `/home/agent/.claude/.credentials.json` | Claude SDK credentials       |

### Data Layer (`app/data/`)

A package of 15 modules. All public functions are re-exported from `app/data/__init__.py` for backward compatibility (`from app.data import load_state` works unchanged).

Most data loading uses **mtime-based caching** (re-reads only when the source file changes). Only `load_system_info()` retains `@_cache(ttl=60)` — it reads OS resources with no file mtime to track. Write actions call `_cache_clear_all()` to bust stale cache after mutations. `@st.cache_data` is **banned** (see AGENTS.md).

#### Cache Infrastructure (`app/data/_cache.py`)

| Component | Purpose |
|-----------|---------|
| `_TTLCache` | Thread-safe TTL cache using atomic `dict.get()` to avoid cachetools TOCTOU race |
| `@_mfile_cache(path_fn, default_fn)` | Decorator: mtime-based cache for single-file JSON loaders |
| `@_mmfile_cache([path_fns])` | Decorator: mtime-based cache for multi-file derived loaders |
| `@_cache(ttl=seconds)` | Decorator: atomic TTL cache (optimized key strategy for no-arg calls) |
| `_register_cache()` | Create and register a hand-rolled cache dict, auto-cleared by `_cache_clear_all()` |
| `_MFILE_CACHES` | Registry of mtime-cache dicts (populated by decorators at decoration time) |
| `_HAND_CACHES` | Registry of hand-rolled cache dicts (populated by `_register_cache()`) |
| `_cache_clear_all()` | Clears `_GLOBAL_CACHE` + all `_MFILE_CACHES` + all `_HAND_CACHES` + `st.cache_data` |

#### Read Functions — mtime-based (invalidate on file change)

| Function | Module | Source |
|----------|--------|--------|
| `load_state()` | `state.py` | `state.json` |
| `load_services()` | `state.py` | `state.json` mtime + 5s monotonic bucket (PID liveness) |
| `load_services_full()` | `state.py` | `state.json` + `services.json` |
| `load_goals()` | `goal.py` | `goal.json` |
| `load_cycles()` | `cycle.py` | `cycles.json` |
| `load_cycle_logs()` | `cycle.py` | cycle log dir mtime |
| `load_cycle_log_content(num)` | `cycle.py` | per-file mtime (write-once after cycle ends) |
| `load_inbox()` | `message.py` | `inbox.json` |
| `load_outbox()` | `message.py` | `outbox.json` |
| `load_outbox_history()` | `message.py` | `outbox_history.json` |
| `load_history()` | `message.py` | `command_history.json` |
| `load_journal(limit, offset)` | `journal.py` | `journal.json` + `journal-archive.json` (both mtimes) |
| `load_errors()` | `system.py` | `server_errors.json` |
| `load_validate()` | `system.py` | 6 memory files + 30s heartbeat bucket |
| `load_plugins()` | `system.py` | `.claude/settings.json` |
| `load_scheduled_tasks()` | `system.py` | `scheduled_tasks.json` |
| `load_logs()` | `log.py` | `bash-*.json` in LOGS_DIR + 30s PID bucket for running jobs |
| `load_log_detail(num)` | `log.py` | `bash-N.json` + stdout/stderr mtimes |
| `load_scripts()` | `script.py` | `SCRIPTS_DIR` dir mtime |
| `load_memory_files()` | `memory.py` | `MEMORY_DIR` dir mtime |
| `read_memory_file(filename)` | `memory.py` | per-file mtime |
| `load_workspace_files()` | `workspace.py` | workspace dir mtime |
| `read_workspace_file(path)` | `workspace.py` | per-file mtime |
| `search(query)` | `search.py` | multi-file mtime tuple (journal, archive, goals, cycles, history) |
| `load_suggest()` | `suggest.py` | multi-file mtimes + workspace dir + date bucket |

#### Read Functions — TTL-based (OS resources; no source file to mtime-check)

| Function | Module | TTL | Notes |
|----------|--------|-----|-------|
| `load_system_info()` | `system.py` | 60s | `/proc/meminfo`, `/proc/uptime`, `shutil.disk_usage`, `os.getloadavg()` |

#### Read Functions — multi-file derived (use `@_mmfile_cache`)

| Function | Module | Cache Key |
|----------|--------|-----------|
| `load_cycle_velocity()` | `cycle.py` | `cycles.json` mtime |
| `load_goal_stats()` | `goal.py` | `goal.json` + `cycles.json` mtimes |
| `load_activity()` | `cycle.py` | `command_history.json` + `cycles.json` mtimes |
| `load_balance()` | `cycle.py` | `cycles.json` + `journal.json` + `evolution_weights.json` mtimes (hand-rolled `_register_cache`) |

#### Write Actions (`app/data/write.py`)

All write functions call `_cache_clear_all()` after mutation.

| Function | Purpose |
|----------|---------|
| `save_portal_config(key, value)` | Update a single key in `portal_config.json` (AtomicJSON) |
| `queue_to_inbox(content, cmd_type, timestamp, priority)` | Append to `inbox.json` + history (AtomicJSON, priority 1-5) |
| `run_script(script_name, args)` | Execute whitelisted script (30s timeout, 20KB stdout cap) |
| `write_first_goal(content, timestamp)` | Write first goal to `goal.json` on bootstrap (no-op if exists) |
| `trigger_bootstrap_heartbeat()` | Fire `heartbeat.sh` in background for bootstrap cycle |
| `update_goal_status(goal_index, new_status)` | Update goal status by index (AtomicJSON) |
| `delete_inbox_item(item_index)` | Delete single inbox item by index (AtomicJSON) |
| `clear_outbox()` | Archive outbox to `outbox_history.json` (dedup by timestamp), then clear |
| `remove_service(name)` | Remove from both `state.json` and `services.json` |
| `stop_service(name)` | Stop running service via `service-manager.py` (15s timeout) |
| `start_service(name)` | Restart dead service using saved command from `services.json` |
| `create_scheduled_task(task_data)` | Add new scheduled task to `scheduled_tasks.json` (AtomicJSON) |
| `update_scheduled_task(task_id, updates)` | Update fields of existing scheduled task |
| `delete_scheduled_task(task_id)` | Delete scheduled task by ID |

### Startup Check (`app/shared.py`)

`_startup_check()` runs once via `@st.cache_resource` in server.py:
1. Creates missing critical directories
2. Creates or repairs critical JSON files (backs up corrupt as `.corrupt`)
3. Removes stale `.tmp` files older than 30 seconds

### Atomic Writes

All JSON mutations use `_write_json_atomic()` from `app/shared.py` — writes to a temp file then `os.replace()`. For concurrent read-modify-write operations, `AtomicJSON` context manager provides exclusive file locking via `fcntl.flock`.

### Tab Crash Isolation

`_safe_render(module, tab_name)` in `server.py` wraps every module's `render()` call (including always-visible sections and all tabs) in a try/except so one broken section cannot crash the portal. Errors are:
- Displayed inline (user sees error in that section/tab only)
- Logged to `server_errors.json` (agent detects on next cycle, last 20 kept)
- Special case: Streamlit cache LRU eviction KeyErrors are silently retried once

### Critical Files & Defaults

| File | Default |
|------|---------|
| `state.json` | `{cycle_number: 0, status: "idle", current_goal: null, last_cycle_summary: null, created_at: null, last_heartbeat: null, last_cycle_run: null, last_cycle_end: null, services: {}}` |
| `cycles.json` | `[]` |
| `goal.json` | `[]` |
| `command_history.json` | `[]` |
| `server_errors.json` | `[]` |
| `inbox.json` | `[]` |
| `outbox.json` | `[]` |
| `journal.json` | `[]` |

### Adding a New Tab (quick reference)

1. Create `app/<module_name>.py` with a `render()` function
2. Import at top of `server.py`: `from app import <module_name>`
3. Add one entry to `TAB_REGISTRY`: `("Label", <module_name>, "Display Name", "Group")`
   - The 4th `group` element is optional — omit it to default to `"General"`
   - Use an existing group name (e.g. `"Agent Console"`) to place the tab alongside others in that group
   - Use a new group name to create a new section (a `st.segmented_control` bar will appear automatically once there are multiple groups)
4. Verify: `curl -s http://localhost:8081/app/_stcore/health`

No other changes needed — the tab loop renders all entries automatically.

### Data Display Indicator Labels

All data displayed on the portal **must** include a clear indicator label when the data is not live/real. This helps users immediately understand the nature of the data they are viewing.

| Data Type | Required Label | Example |
|-----------|---------------|---------|
| Simulation data | **(Simulation)** | "Cycle Velocity (Simulation)" |
| Placeholder data | **(Placeholder)** | "Goals (Placeholder)" |
| Sample/example data | **(Sample)** | "Activity Log (Sample)" |
| Mock data | **(Mock)** | "System Info (Mock)" |
| Cached/stale data | **(Cached)** | "Services (Cached)" |
| Estimated/projected data | **(Estimated)** | "Balance (Estimated)" |

**Rules:**
- The indicator label must appear **inline** with or immediately adjacent to the data display (e.g., in the section header, metric label, or chart title) — not hidden in a tooltip or footnote.
- Use `st.caption` or parenthetical text in the `st.metric`/`st.header`/`st.subheader` label to show the indicator.
- When the data source transitions from non-real to real data, the indicator label must be removed automatically.
- Never display synthetic data without an indicator — users must always know what they are looking at.
