---
name: portal-config
description: Unified portal configuration management. Manages public hostname (for external access via Cloudflare Tunnel), timezone (for time-aware operations), and authentication (basic HTTP auth via Caddy). Use to configure the portal for production deployment, set timezone for scheduled tasks, or secure the portal with credentials.
---

# portal-config

**Path:** `scripts/portal_config.py`

Unified configuration manager for the agent portal. Combines hostname, timezone, and auth management into a single script with subcommand-based CLI.

## Subcommands

### hostname

Manage the public URL for external portal access (e.g., via Cloudflare Tunnel).

When the agent is exposed via a public proxy, configure the public URL so the agent uses it in user-facing links instead of `localhost:8080`. The URL is stored in `/agent/memory/portal_config.json` and injected into the system prompt.

**Flags:**

| Flag | Description |
|------|-------------|
| `--set URL` | Set public hostname (e.g., `https://agent.example.com`) |
| `--show` | Display current public URL and when it was set |
| `--clear` | Remove public URL (revert to localhost) |

**Examples:**

```bash
# Set public hostname
uv run python scripts/portal_config.py hostname --set https://agent.example.com

# View current hostname
uv run python scripts/portal_config.py hostname --show

# Clear hostname
uv run python scripts/portal_config.py hostname --clear
```

### timezone

Manage timezone for time-aware operations (scheduled tasks, logs, timestamps).

The timezone is stored in `/agent/memory/portal_config.json` and used by heartbeat.sh for timestamp display in logs. Uses IANA timezone database format.

**Flags:**

| Flag | Description |
|------|-------------|
| `--set TZ` | Set timezone (e.g., `America/New_York`, `UTC`, `Europe/London`) |
| `--show` | Display current timezone |
| `--clear` | Remove timezone (revert to UTC) |

**Examples:**

```bash
# Set timezone
uv run python scripts/portal_config.py timezone --set America/New_York

# View current timezone
uv run python scripts/portal_config.py timezone --show

# Clear timezone
uv run python scripts/portal_config.py timezone --clear
```

**Valid timezone names:**
- `UTC`
- `America/New_York`
- `America/Los_Angeles`
- `Europe/London`
- `Asia/Tokyo`
- See full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones

### auth

Manage basic HTTP authentication via Caddy Admin API. Credentials are stored in KeePass for persistence across Caddy restarts.

When enabled, the portal requires username/password authentication via HTTP Basic Auth. Auth is automatically re-applied after Caddy restarts by `bootstrap.sh`.

**Flags:**

| Flag | Description |
|------|-------------|
| `--enable USER:PASS` | Enable auth with credentials |
| `--disable` | Remove auth and delete credentials from KeePass |
| `--reapply` | Re-apply auth from KeePass (post-restart, called by bootstrap.sh) |
| `--show` | Display current credentials |
| `--rollback` | Force-remove auth route (escape hatch) |

**Examples:**

```bash
# Enable authentication
uv run python scripts/portal_config.py auth --enable admin:secure_password_123

# Check current credentials
uv run python scripts/portal_config.py auth --show

# Disable authentication
uv run python scripts/portal_config.py auth --disable

# Re-apply after Caddy restart (automated by bootstrap.sh)
uv run python scripts/portal_config.py auth --reapply

# Emergency rollback
uv run python scripts/portal_config.py auth --rollback
```

**How auth works:**

1. **Enable**: Generates bcrypt hash via `caddy hash-password`, adds auth route to Caddy via Admin API, stores credentials in KeePass (`System/PORTAL_BASIC_AUTH`)
2. **Reapply**: Called automatically by `bootstrap.sh` on startup to restore auth from KeePass if it exists
3. **Disable**: Removes Caddy route via Admin API, deletes KeePass entry
4. **Rollback**: Emergency cleanup if auth is stuck (removes route without KeePass validation)

**Credential storage:**
- **Location**: KeePass database at `/home/agent/.keepass/credentials.kdbx`
- **Group**: System
- **Entry**: PORTAL_BASIC_AUTH
- **Fields**: username, password (plaintext), notes (bcrypt hash)

## Configuration File

All settings (except auth credentials) are stored in `/agent/memory/portal_config.json`:

```json
{
  "public_url": "https://agent.example.com",
  "timezone": "America/New_York"
}
```

The script uses atomic updates via `save_portal_config()` from `app/data/write.py` to preserve other keys when updating individual properties.

## Integration

- **bootstrap.sh**: Automatically runs `portal_config.py auth --reapply` on startup (line 100)
- **heartbeat.sh**: Reads `timezone` from `portal_config.json` for log timestamps (line 66-69)
- **chat.py**: Reads `public_url` from `portal_config.json` for system prompt injection
- **Portal UI**: System tab → Run Script section exposes all commands

## Migration from Legacy Scripts

This script supersedes `portal-hostname.py` and `portal-auth.py`, which now have been removed. The new `portal_config.py` provides a unified interface for all portal configuration needs.

**Old commands still work:**
```bash
# Old style (via symlinks)
uv run python scripts/portal-hostname.py --set https://example.com
uv run python scripts/portal-auth.py --enable user:pass
```

**New canonical syntax:**
```bash
# New style (recommended)
uv run python scripts/portal_config.py hostname --set https://example.com
uv run python scripts/portal_config.py auth --enable user:pass
```
