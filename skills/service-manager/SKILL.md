---
name: service-manager
description: Manage agent background processes. Use to register a new background service (start), stop one (stop), remove one permanently (remove), restart one (restart), check its status (status), health-check all registered services (health), list all entries (list), or remove dead/stale entries (cleanup). Example: starting a webhook listener or a custom API service.
---

# service-manager

**Path:** `scripts/service_manager.py`

Manages background agent processes. Use this for any long-running background process (web servers, notebooks, APIs, workers) — it handles PID tracking, health checks, log management, and state.json integration.

If service require a port, chose a port in range 8083–8090. Ports 8080 (Caddy), 8081 (Streamlit), and 8082 (webhook_receiver) are reserved and not managed here.

## When to Use

- Starting a web server, API, or dashboard on a port
- Running a Jupyter notebook for the user
- Any process that must survive the current heartbeat cycle
- Any process the user should be able to access via browser

## Available Ports

| Port | Assignment |
|------|------------|
| 8080 | Caddy gateway (reserved) |
| 8081 | Streamlit portal (reserved) |
| 8082 | **Reserved** — `webhook_receiver` (built-in, auto-starts) |
| 8083–8090 | Available for agent services |

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `list` | List all registered services with their port, PID liveness, and status |
| `start NAME [PORT] [--auto-start] -- CMD...` | Register and start a service; `--auto-start` marks it for automatic restart on each heartbeat |
| `stop NAME` | Stop a running service and keep its entry for restart |
| `remove NAME` | Stop (if running) and remove a service entry entirely |
| `restart NAME` | Restart a registered service (stops if running, then starts with stored command) |
| `status NAME` | Check the status of one service |
| `health` | Health-check all registered services (verifies PIDs and ports) |
| `cleanup` | Remove dead or stale service entries |

## Examples

```bash
# List all managed services
uv run python scripts/service_manager.py list

# Start a webhook listener on port 8083
uv run python scripts/service_manager.py start webhook 8083 -- python3 webhook_server.py

# Start a service and mark it to auto-restart on every heartbeat
uv run python scripts/service_manager.py start webhook 8083 --auto-start -- python3 webhook_server.py

# Start a Jupyter notebook on port 8088
uv run python scripts/service_manager.py start jupyter 8088 -- \
  jupyter notebook --no-browser --port=8088 --ip=0.0.0.0 --NotebookApp.token=''

# Start a FastAPI app on port 8085
uv run python scripts/service_manager.py start myapi 8085 -- \
  uvicorn app:app --host 0.0.0.0 --port 8085

# Start a simple static file server on port 8084
uv run python scripts/service_manager.py start fileserver 8084 -- \
  python3 -m http.server 8084 --directory /agent/workspace

# Check its status
uv run python scripts/service_manager.py status webhook

# Health-check all services
uv run python scripts/service_manager.py health

# Stop the service (keeps entry for restart)
uv run python scripts/service_manager.py stop webhook

# Remove a service entirely
uv run python scripts/service_manager.py remove webhook

# Restart the service (uses stored command and port)
uv run python scripts/service_manager.py restart webhook

# Remove stale/dead entries
uv run python scripts/service_manager.py cleanup
```

## Log Files

Service stdout/stderr are captured in `/agent/memory/logs/`:
- `service-<name>.stdout.log`
- `service-<name>.stderr.log`

Check logs with:
```bash
tail -50 /agent/memory/logs/service-<name>.stdout.log
tail -50 /agent/memory/logs/service-<name>.stderr.log
```

## Exposing Services via Caddy

After starting a service, add a Caddy reverse proxy route so users can
access it through the gateway on port 8080. Use the Caddy admin API:

```bash
# Expose a service at /myservice/* → localhost:<port>
curl -X POST http://localhost:2019/config/apps/http/servers/gateway/routes \
  -H 'Content-Type: application/json' \
  -d '{
    "@id": "route-myservice",
    "match": [{"path": ["/myservice/*"]}],
    "handle": [{
      "handler": "reverse_proxy",
      "upstreams": [{"dial": "localhost:<port>"}]
    }]
  }'
```

Changes take effect immediately — no restart needed. The user can then
access the service via the Caddy Gateway.

## HTTP Health Check (health_url)

Services that expose an HTTP health endpoint can register a `health_url` in `services.json`.
When set, `cycle_start.py` performs an HTTP check on every heartbeat in addition to PID liveness.
A service whose PID is alive but whose health URL returns non-2xx is treated as a **zombie** and
automatically restarted.

To add a health URL to an existing service, edit `memory/services.json` directly:
```json
"health_url": "http://localhost:<port>/health"
```

The webhook_receiver (`port 8082`) already has `health_url` configured.

## Port Allocation Rules

- Only use ports 8083–8090 for new services (8082 is reserved for webhook_receiver)
- Check `service_manager.py list` before starting to avoid port conflicts
- Always log new port bindings in your journal entry
- Maximum 3 concurrent services (constitution resource limit)