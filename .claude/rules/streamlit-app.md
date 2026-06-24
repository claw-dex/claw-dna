---
paths:
  - "server.py"
  - "app/*.py"
  - "app/**/*.py"
---

# Streamlit App Development Rules

- Always validate python syntax after editing with `uv run python -m py_compile <file.py>`
- Never use deprecated `use_container_width` parameter. Use `width="stretch"` (for full width) or `width="content"` (for fit-to-content) instead.
- The following tabs are core functionalities and MUST not be removed. Always keep them at the top of the tab registry in `server.py`:

  ```
  TAB_REGISTRY = [
      # Agent Console — operational tools
      ("🎛️ Command Center",   commands_tab,   "Command Center",  "Agent Console"),
      ("📓 Memory",            memory_tab,     "Memory",          "Agent Console"),
      ("🔭 Overview",          overview_tab,   "Overview",        "Agent Console"),
      ("🤖 Agents",            agents_tab,     "Agents",          "Agent Console"),
      # Core — file / credential / email / system management
      ("📁 Workspace",         workspace_tab,  "Workspace",       "Core"),
      ("🔑 Credentials",       credential_tab, "Credentials",     "Core"),
      ("📧 Email",             emails_tab,     "Email",           "Core"),
      ("⚙️ System",            system_tab,     "System",          "Core"),
      ("🔧 Services & Cron",   services_tab,   "Services & Cron", "Core"),
  ]
  ```

## Data Loading Rules

- **NEVER** load data directly inside a Streamlit page file (`server.py`, `app/*.py`, `app/**/*.py`). This includes:
  - Reading local files (e.g. `open(...)`, `json.load(...)`, `pd.read_csv(...)`)
  - Making API calls (e.g. `requests.get(...)`, `httpx.get(...)`)
  - Calling any 3rd-party library or SDK (e.g. `boto3`, `openai`, `anthropic`, database clients)
- All data operations **MUST** be implemented as functions in a dedicated Python file under `app/data/`.
  - Example: `app/data/goal.py`, `app/data/metrics.py`, `app/data/agents.py`
- All data loading functions inside `app/data/` **MUST** use a cache decorator to avoid redundant I/O and improve portal performance:
  - Use `@_mfile_cache` for single-file caching (invalidated when the file changes).
  - Use `@_mmfile_cache` for multi-file caching (invalidated when any of the watched files change).
  - If neither applies, use `@st.cache_data` with an appropriate `ttl`.
- Streamlit page code may only call functions imported from `app/data/`; it must not contain raw I/O or SDK calls inline.

## 3rd-Party Platform Dashboards

- **Always prefer locally-collected data** when building dashboards that display data from 3rd-party platforms (e.g. GitHub, Google, PostHog, Slack, Linear, Stripe, etc.).
  - Many platforms enforce strict API rate limits. Fetching data in real-time during portal rendering causes slow load times, spinner-heavy UIs, and hard-to-diagnose failures — all of which degrade the user experience.
- **Do not fetch from 3rd-party APIs at render time.** If the data you need already exists in a local file (metrics snapshot, event log, exported JSON/CSV, etc.), read that file instead.
- **Pattern for new data sources that must be fetched:**
  1. Create a standalone fetch script under `scripts/` (e.g. `scripts/fetch_github_stats.py`) that calls the external API and saves the result to a local file (e.g. `memory/metrics/github_stats.json`).
  2. Expose the data to the portal through a cached function in `app/data/` that reads the local file.
  3. Schedule the fetch script to run daily using the `scheduler` skill so the local data stays fresh without any real-time API calls from the portal.
- **Example flow:**
  ```
  scripts/fetch_github_stats.py   ← fetches GitHub API → writes memory/metrics/github_stats.json
  app/data/github.py              ← @_mfile_cache reads memory/metrics/github_stats.json
  app/pages/overview_tab.py       ← calls app/data/github.py functions only
  Scheduled daily via scheduler skill
  ```
