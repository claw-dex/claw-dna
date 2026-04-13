---
name: server-restart
description: Trigger a Streamlit hot-reload by touching server.py, causing Streamlit's file-watcher to reload the app without a full process restart. Use after editing portal code to apply changes, when the portal is stale or showing old UI, or with --verify to confirm the portal responds after reload.
---

# server-restart

**Path:** `scripts/server_restart.sh`

Triggers Streamlit's automatic file-watch reload by `touch`ing `server.py`. Faster than a full process restart — no downtime.

## Arguments

| Flag | Description |
|------|-------------|
| _(none)_ | Touch server.py to trigger reload |
| `--verify` | Run `health_check.sh` after the reload to confirm the portal is responding |

## Examples

```bash
# Trigger a hot reload
bash scripts/server_restart.sh

# Reload and verify portal is up
bash scripts/server_restart.sh --verify
```
