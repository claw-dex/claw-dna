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
      ("⚙️ System",            system_tab,     "System",          "Agent Console"),
      ("🔧 Services & Cron",   services_tab,   "Services & Cron", "Agent Console"),
      ("🔭 Agent Overview",    overview_tab,   "Agent Overview",  "Agent Console"),
      # Core — file / credential / email management
      ("📁 Workspace",         workspace_tab,  "Workspace",       "Core"),
      ("🔑 Credentials",       credential_tab, "Credentials",     "Core"),
      ("📧 Email",             emails_tab,     "Email",           "Core"),
  ]
  ```