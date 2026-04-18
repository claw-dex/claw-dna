---
name: clickup-cli
description: >
  Install, authenticate, and use the ClickUp CLI (optimized for AI agents)
  to manage tasks, spaces, folders, lists, goals, and time tracking in ClickUp.
  Use this skill whenever the user asks to interact with ClickUp — listing tasks, creating
  or updating tasks, checking project status, managing sprints, searching across workspaces,
  or any ClickUp operation. Also use for initial installation and token setup.
  Triggers on: "check ClickUp", "create a task in ClickUp", "list my tasks", "update task",
  "what's in ClickUp", "ClickUp setup", "install clickup cli", "search ClickUp tasks",
  "mark task as done", "add task", "ClickUp project", or any mention of ClickUp operations.
---

# ClickUp CLI Skill

A Rust-based CLI for the ClickUp API, optimized for AI agents. Installed at `/usr/local/bin/clickup`.

- **Docs**: https://clickup-cli.com/commands
- **GitHub**: https://github.com/nicholasbester/clickup-cli
- **Config**: `~/.config/clickup-cli/config.toml`

Command pattern: `clickup <resource> <action> [ID] [flags]`

---

## Installation (if not present)

> **GLIBC Requirement**: The pre-built binaries require **GLIBC ≥ 2.38** (released with Ubuntu 23.04 / Debian 13).
> Debian 12 (Bookworm) ships GLIBC 2.36 and **cannot run the pre-built binary**.
> In that case, fall back to **building from source** using the Rust toolchain (see below).

### Step 1 — Check if already installed

```bash
which clickup && clickup --version || echo "NOT_INSTALLED"
```

### Step 2 — Check GLIBC version

```bash
ldd --version | head -1
# If version >= 2.38 → use pre-built binary (fast, ~30 seconds)
# If version <  2.38 → build from source (slow, ~6 minutes)
```

### Option A: Pre-built binary (GLIBC ≥ 2.38 only)

```bash
ARCH=$(dpkg --print-architecture)
case "$ARCH" in
  amd64)   BINARY="clickup-linux-x86_64.tar.gz" ;;
  arm64)   BINARY="clickup-linux-arm64.tar.gz" ;;
  *)       echo "Unsupported arch: $ARCH"; exit 1 ;;
esac
curl -L "https://github.com/nicholasbester/clickup-cli/releases/latest/download/${BINARY}" | tar xz -C /tmp/
sudo mv /tmp/clickup /usr/local/bin/
clickup --version
```

If you see `GLIBC_2.38' not found` or `GLIBC_2.39' not found`, the binary won't run — use Option B instead.

### Option B: Build from source (required for GLIBC < 2.38, e.g. Debian 12)

**Dependencies**: Rust toolchain + C linker (`gcc`). Install both first:

```bash
# 1. Install Rust toolchain (if not present)
which cargo || curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
source "$HOME/.cargo/env"
cargo --version   # confirm: cargo 1.x.x

# 2. Clone and build (takes ~5–6 minutes)
git clone --depth 1 https://github.com/nicholasbester/clickup-cli.git /tmp/clickup-cli
cd /tmp/clickup-cli
cargo build --release

# 3. Install
sudo mv /tmp/clickup-cli/target/release/clickup /usr/local/bin/
clickup --version
```

> **Note**: The build runs in the background in CI environments. Use `run_in_background: true` on the
> `cargo build` step and monitor completion before proceeding to authentication.

### Verify installation

```bash
clickup --version          # should print: clickup X.Y.Z
clickup status             # shows config path (token not yet set)
```

---

## Authentication & Setup

```bash
# Interactive setup (prompts for token)
clickup setup

# Non-interactive (CI/scripting)
clickup setup --token pk_YOUR_TOKEN_HERE

# Verify auth
clickup auth whoami     # shows user info
clickup auth check      # validates token (exit-code only)
clickup status          # shows masked token + default workspace + config path
```

Successfully authentication will save the token to `~/.config/clickup-cli/config.toml`. Token resolution order (highest priority first):
1. `--token TOKEN` flag
2. `CLICKUP_TOKEN` env var
3. `.clickup.toml` (project-level)
4. `~/.config/clickup-cli/config.toml` (global)

---

## Global Flags

Available on every command:

| Flag | Description |
|------|-------------|
| `--output table\|json\|json-compact\|csv` | Output format (default: `table`) |
| `--fields LIST` | Comma-separated field names to display |
| `--no-header` | Omit table header row |
| `-q` / `--quiet` | Print IDs only, one per line (for scripting) |
| `--all` | Fetch all pages (auto-paginate) |
| `--limit N` | Cap total results |
| `--page N` | Manual page selection |
| `--token TOKEN` | Override config file token (one-off) |
| `--workspace ID` | Override default workspace |
| `--timeout SECS` | HTTP timeout (default: 30) |

---

## workspace

```bash
clickup workspace list        # list workspaces
clickup workspace seats       # show seat usage
clickup workspace plan        # show current plan
```

---

## space

```bash
clickup space list [--archived]
clickup space get <ID>
clickup space create --name NAME [--private] [--multiple-assignees]
clickup space update <ID> [--name NAME] [--color HEX]
clickup space delete <ID>
```

---

## folder

```bash
clickup folder list --space <ID> [--archived]
clickup folder get <ID>
clickup folder create --space <ID> --name NAME
clickup folder update <ID> --name NAME
clickup folder delete <ID>
```

---

## list

```bash
clickup list list --folder <ID> [--archived]
clickup list list --space <ID> [--archived]       # folderless lists
clickup list get <ID>
clickup list create --folder <ID> --name NAME [--content TEXT] [--priority N] [--due-date DATE]
clickup list create --space <ID> --name NAME      # folderless list
clickup list update <ID> [--name NAME] [--content TEXT]
clickup list delete <ID>
clickup list add-task <LIST_ID> <TASK_ID>
clickup list remove-task <LIST_ID> <TASK_ID>
```

---

## task

### List & Search
```bash
clickup task list --list <ID> [--status S] [--assignee ID] [--tag T] [--include-closed] [--order-by field] [--reverse]
clickup task search [--space ID] [--folder ID] [--list ID] [--status S] [--assignee ID] [--tag T]
```

### CRUD
```bash
clickup task get <ID> [--subtasks] [--custom-task-id]
clickup task create --list <ID> --name NAME \
  [--description TEXT] [--status S] [--priority 1-4] \
  [--assignee ID] [--tag NAME] [--due-date DATE] [--parent TASK_ID]
clickup task update <ID> [--name X] [--status X] [--priority N] \
  [--add-assignee ID] [--rem-assignee ID] [--description TEXT]
clickup task delete <ID>
```

### Relationships & Tags
```bash
clickup task add-dep <ID> --depends-on <OTHER_ID>
clickup task remove-dep <ID> --depends-on <OTHER_ID>
clickup task link <ID> <TARGET_ID>
clickup task unlink <ID> <TARGET_ID>
clickup task add-tag <ID> <TAG_NAME>
clickup task remove-tag <ID> <TAG_NAME>
```

### Time & Estimates
```bash
clickup task time-in-status <ID>...
clickup task move <ID> --list <LIST_ID>
clickup task set-estimate <ID> --assignee USER_ID --time MS
clickup task replace-estimates <ID> --assignee USER_ID --time MS
```

**Priority values**: `1`=Urgent, `2`=High, `3`=Normal, `4`=Low  
**Date format**: `YYYY-MM-DD`

---

## checklist

```bash
clickup checklist create --task <ID> --name NAME
clickup checklist update <ID> [--name NAME] [--position N]
clickup checklist delete <ID>
clickup checklist add-item <ID> --name NAME [--assignee USER_ID]
clickup checklist update-item <ID> <ITEM_ID> [--name NAME] [--resolved] [--assignee USER_ID]
clickup checklist delete-item <ID> <ITEM_ID>
```

---

## comment

```bash
clickup comment list --task <ID>           # also --list, --view
clickup comment create --task <ID> --text TEXT [--assignee ID] [--notify-all]
clickup comment create --list <ID> --text TEXT
clickup comment create --view <ID> --text TEXT
clickup comment update <ID> --text TEXT [--resolved] [--assignee ID]
clickup comment delete <ID>
clickup comment replies <ID>               # list threaded replies
clickup comment reply <ID> --text TEXT [--assignee ID]
```

---

## tag

```bash
clickup tag list --space <ID>
clickup tag create --space <ID> --name NAME [--fg-color HEX] [--bg-color HEX]
clickup tag update --space <ID> --tag NAME [--name NEW_NAME] [--fg-color HEX] [--bg-color HEX]
clickup tag delete --space <ID> --tag NAME
```

---

## field (Custom Fields)

```bash
clickup field list --list <ID>            # also --folder, --space, --workspace-level
clickup field set <TASK_ID> <FIELD_ID> --value VALUE
clickup field unset <TASK_ID> <FIELD_ID>
```

Value can be a string, number, or JSON for complex field types.

---

## task-type

```bash
clickup task-type list
```

---

## attachment

```bash
clickup attachment list --task <ID>
clickup attachment upload --task <ID> <FILE_PATH>
```

---

## time (Time Tracking)

```bash
# Timer
clickup time start [--task ID] [--description TEXT] [--billable]
clickup time stop
clickup time current

# CRUD
clickup time list [--start-date DATE] [--end-date DATE] [--assignee ID] [--task ID]
clickup time get <ID>
clickup time create --start DATE --duration MS [--task ID] [--description TEXT] [--billable]
clickup time update <ID> [--start DATE] [--end DATE] [--description TEXT] [--billable]
clickup time delete <ID>

# Tags
clickup time tags
clickup time add-tags --entry-id ID --tag NAME [--tag NAME...]
clickup time remove-tags --entry-id ID --tag NAME
clickup time rename-tag --name OLD --new-name NEW

# History
clickup time history <ID>
```

---

## goal

```bash
clickup goal list [--include-completed]
clickup goal get <ID>
clickup goal create --name NAME --due-date DATE --description TEXT [--color HEX] [--owner ID]
clickup goal update <ID> [--name NAME] [--due-date DATE] [--add-owner ID] [--rem-owner ID]
clickup goal delete <ID>

# Key Results
clickup goal add-kr <GOAL_ID> --name NAME --type TYPE --steps-start N --steps-end N [--unit UNIT] [--owner ID]
clickup goal update-kr <KR_ID> --steps-current N [--note TEXT]
clickup goal delete-kr <KR_ID>
```

Key result types: `number`, `currency`, `boolean`, `percentage`, `automatic`

---

## view

```bash
clickup view list --workspace-level         # also --space, --folder, --list
clickup view get <ID>
clickup view create --name NAME --type TYPE --space <ID>   # also --folder, --list, --workspace-level
clickup view update <ID> [--name NAME]
clickup view delete <ID>
clickup view tasks <ID> [--page N]
```

View types: `list`, `board`, `calendar`, `gantt`, `activity`, `map`, `workload`, `table`

---

## member

```bash
clickup member list --task <ID>
clickup member list --list <ID>
```

---

## user

```bash
clickup user invite --email EMAIL [--admin] [--custom-role-id ID]
clickup user get <ID>
clickup user update <ID> [--username NAME] [--admin] [--custom-role-id ID]
clickup user remove <ID>
```

---

## chat (v3)

```bash
# Channels
clickup chat channel-list [--include-closed]
clickup chat channel-create --name NAME [--visibility PUBLIC|PRIVATE]
clickup chat channel-get <ID>
clickup chat channel-update <ID> [--name NAME] [--topic TEXT]
clickup chat channel-delete <ID>
clickup chat channel-followers <ID>
clickup chat channel-members <ID>
clickup chat dm <USER_ID> [USER_ID...]

# Messages
clickup chat message-list --channel <ID>
clickup chat message-send --channel <ID> --text TEXT [--type message|post]
clickup chat message-update <ID> --text TEXT
clickup chat message-delete <ID>

# Reactions & Replies
clickup chat reaction-list <MSG_ID>
clickup chat reaction-add <MSG_ID> --emoji NAME
clickup chat reaction-remove <MSG_ID> <EMOJI>
clickup chat reply-list <MSG_ID>
clickup chat reply-send <MSG_ID> --text TEXT
clickup chat tagged-users <MSG_ID>
```

---

## doc (v3)

```bash
clickup doc list [--creator ID] [--archived]
clickup doc create --name NAME [--visibility PUBLIC|PRIVATE|PERSONAL] [--parent-type TYPE --parent-id ID]
clickup doc get <ID>
clickup doc pages <ID> [--content] [--max-depth N]
clickup doc add-page <DOC_ID> --name NAME [--parent-page ID] [--content TEXT]
clickup doc page <DOC_ID> <PAGE_ID>
clickup doc edit-page <DOC_ID> <PAGE_ID> --content TEXT [--mode replace|append|prepend]
```

---

## webhook

```bash
clickup webhook list
clickup webhook create --endpoint URL --event EVENT [--event EVENT...] \
  [--space ID | --folder ID | --list ID | --task ID]
clickup webhook update <ID> --endpoint URL --event EVENT --status active
clickup webhook delete <ID>
```

Events: `taskCreated`, `taskUpdated`, `taskDeleted`, `taskStatusUpdated`, `taskCommentPosted`, `taskCommentUpdated`, and more.

---

## template

```bash
clickup template list [--page N]
clickup template apply-task <TEMPLATE_ID> --list <ID> --name NAME
clickup template apply-list <TEMPLATE_ID> --folder <ID> --name NAME
clickup template apply-list <TEMPLATE_ID> --space <ID> --name NAME
clickup template apply-folder <TEMPLATE_ID> --space <ID> --name NAME
```

---

## guest (Enterprise)

```bash
clickup guest invite --email EMAIL [--can-edit-tags] [--can-see-time-spent] [--can-create-views] [--custom-role-id ID]
clickup guest get <ID>
clickup guest update <ID> [--can-edit-tags] [--can-see-time-spent] [--can-create-views]
clickup guest remove <ID>

# Share/unshare resources
clickup guest share-task <TASK_ID> <GUEST_ID> --permission read|comment|edit|create
clickup guest unshare-task <TASK_ID> <GUEST_ID>
clickup guest share-list <LIST_ID> <GUEST_ID> --permission LEVEL
clickup guest unshare-list <LIST_ID> <GUEST_ID>
clickup guest share-folder <FOLDER_ID> <GUEST_ID> --permission LEVEL
clickup guest unshare-folder <FOLDER_ID> <GUEST_ID>
```

---

## group

```bash
clickup group list
clickup group create --name NAME --member ID [--member ID...]
clickup group update <ID> [--name NAME] [--add-member ID] [--rem-member ID]
clickup group delete <ID>
```

---

## role (Enterprise)

```bash
clickup role list
```

---

## shared

```bash
clickup shared list    # Tasks, lists, and folders shared with you
```

---

## audit-log (Enterprise, v3)

```bash
clickup audit-log query --type TYPE [--user-id ID] [--start-date DATE] [--end-date DATE]
```

Types: `AUTH`, `CUSTOM_FIELDS`, `HIERARCHY`, `USER`, `AGENT`, `OTHER`

---

## acl (Enterprise, v3)

```bash
clickup acl update <OBJECT_TYPE> <OBJECT_ID> \
  [--private] [--grant-user ID --permission LEVEL] [--revoke-user ID] [--body JSON]
```

---

## agent-config

Generate a compressed CLI reference for AI agent to be used in a promp file:

```bash
clickup agent-config show                          # print to stdout
clickup agent-config inject path/to/prompt.md     # inject into specific file
```

The injected block is delimited with `<!-- clickup-cli:begin -->...<!-- clickup-cli:end -->` and can be updated in-place by re-running the command.

---

## status / completions / mcp

```bash
clickup status                   # show current config (version, token, workspace)

clickup completions bash          # generate shell completions
clickup completions zsh
clickup completions fish
clickup completions powershell

clickup mcp serve                 # start MCP server for native LLM tool integration
```

---

## Output Formats

| Format | Flag | Use Case |
|--------|------|----------|
| Table | *(default)* | Human-readable |
| JSON | `--output json` | Full API response; pipe to `jq` |
| JSON Compact | `--output json-compact` | Default fields only, as JSON array |
| CSV | `--output csv` | Spreadsheet / data processing |
| Quiet | `-q` | IDs only, one per line — ideal for scripting |

```bash
# Get task IDs for a list (for scripting)
clickup task list --list LIST_ID -q

# Parse with jq
clickup task list --list LIST_ID --output json | jq '.[].name'

# Auto-paginate all results
clickup task list --list LIST_ID --all
```

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Client error (bad input, 400) |
| 2 | Auth/permission error (401, 403) |
| 3 | Not found (404) |
| 4 | Rate limited (429) |
| 5 | Server error (5xx) |

---

## Common Scripting Patterns

```bash
# Find tasks assigned to me in a specific status
clickup task search --space SPACE_ID --assignee MY_USER_ID --status "in progress"

# Create a task and capture its ID
TASK_ID=$(clickup task create --list LIST_ID --name "Fix bug" -q)
clickup task add-tag "$TASK_ID" "bug"

# Bulk-close all tasks in a list
clickup task list --list LIST_ID -q | xargs -I{} clickup task update {} --status "complete"

# Find workspace/space/folder IDs for setup
clickup workspace list -q           # workspace IDs
clickup space list -q               # space IDs
clickup folder list --space ID -q   # folder IDs
clickup list list --folder ID -q    # list IDs
```
