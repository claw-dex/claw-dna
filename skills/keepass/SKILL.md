---
name: keepass
description: KeePass credential manager for the agent. Stores and retrieves API keys, passwords, and secrets in a local KeePass database (no master password — container is the security boundary). Use to store new credentials (store), retrieve them during automation (get), list all entries, or search by keyword. Example scenarios: storing API keys, retrieving portal auth credentials, or initializing the database for the first time with init.
---

# keepass

**Path:** `scripts/keepass.py`

Manages credentials in a local KeePass database at `/agent/memory/credentials.kdbx`. No master password — the container filesystem is the security boundary.

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `init` | Create the KeePass database (run once on setup) |
| `list` | List all credential entries |
| `get TITLE` | Retrieve a credential by title |
| `store` | Store a new credential (use `--title`, `--username`, `--password`) |
| `delete TITLE` | Delete a credential entry by title |
| `groups` | List all groups in the database |
| `search QUERY` | Search entries by keyword |

## Flags

| Flag | Description |
|------|-------------|
| `--json` | Output as JSON |
| `--title T` | Entry title (used with `store`) |
| `--username U` | Username (used with `store`) |
| `--password P` | Password or secret (used with `store`) |

## Examples

```bash
# Initialize the database (first time only)
uv run python scripts/keepass.py init

# Store an API key
uv run python scripts/keepass.py store --title MY_API_KEY --username agent --password "sk-..."

# Retrieve a credential
uv run python scripts/keepass.py get MY_API_KEY

# List all entries (no passwords shown)
uv run python /agent/scripts/keepass.py list
uv run python /agent/scripts/keepass.py --json list

# Search for a credential
uv run python scripts/keepass.py search "github"
uv run python /agent/scripts/keepass.py search "github"

# Delete a credential
uv run python scripts/keepass.py delete OLD_KEY

# Store a credential
uv run python /agent/scripts/keepass.py store --title "GitHub Token" --username "bot" --password "ghp_xxx" --group "API Keys"

# Retrieve a credential (includes password)
uv run python /agent/scripts/keepass.py get "GitHub Token"
uv run python /agent/scripts/keepass.py --json get "GitHub Token"

# List groups
uv run python /agent/scripts/keepass.py groups
```
