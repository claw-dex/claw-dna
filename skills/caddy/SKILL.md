---
name: caddy
description: Use when the user wants to expose a local dev server under a stable hostname/path, share a localhost app with someone, route multiple local apps through a single entry point, or check what local services are currently running. Triggers on phrases like "set up a proxy for my app", "give me a URL for this localhost server", "what's running on localhost", "register my app", "make this dev server reachable", or when the user mentions port conflicts between local services.
---

# Caddy Local Proxy Manager

Manage local development reverse proxies via Caddy's Admin API at `localhost:2019`.

## Session Naming

When registering a proxy, use one of these approaches:
1. **User provides name**: Use exactly what they specify
2. **Generate from context**: Use project directory name + short random suffix (e.g., `myapp-x7k2`)

Example generation:
```bash
NAME="$(basename "$PWD" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g')-$(head -c4 /dev/urandom | xxd -p | head -c4)"
```

## Quick Reference

| Task | Command |
|------|---------|
| Check status | `curl -sf http://localhost:2019/config/` |
| List routes | `curl -sf http://localhost:2019/config/apps/http/servers/gateway/routes` |
| Add route | `POST /config/apps/http/servers/gateway/routes` |
| Delete route | `DELETE /id/proxy_<name>` |

## Workflows

### Check Caddy Status

```bash
curl -sf http://localhost:2019/config/ 2>/dev/null
```

If fails: "Caddy not running. Install: `brew install caddy`, Start: `caddy start`"

### List All Proxies

```bash
curl -sf http://localhost:2019/config/apps/http/servers/gateway/routes 2>/dev/null
```

Display as table: Name | URL | Backend

### Register a Proxy

1. **Get or generate name** (ask user or generate from project + random)

2. **Find available port** (if not specified):
```bash
for port in $(seq 3000 3100); do
  lsof -i :$port > /dev/null 2>&1 || { echo $port; break; }
done
```

3. **Initialize server if needed**:
```bash
curl -sf http://localhost:2019/config/apps/http/servers/gateway > /dev/null 2>&1 || \
curl -sf -X POST http://localhost:2019/load \
  -H "Content-Type: application/json" \
  -d '{"apps":{"http":{"servers":{"gateway":{"listen":[":80"],"routes":[]}}}}}'
```

4. **Add route** (replace NAME and PORT):
```bash
curl -sf -X POST "http://localhost:2019/config/apps/http/servers/gateway/routes" \
  -H "Content-Type: application/json" \
  -d '{"@id":"proxy_NAME","match":[{"host":["NAME.localhost"]}],"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"localhost:PORT"}]}],"terminal":true}'
```

5. **Report**: "Registered: http://NAME.localhost → localhost:PORT"

### Remove a Proxy

```bash
curl -sf -X DELETE "http://localhost:2019/id/proxy_NAME"
```

### Update a Proxy

Delete then re-add with new port.

## Route JSON Structure

```json
{
  "@id": "proxy_<name>",
  "match": [{"host": ["<name>.localhost"]}],
  "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "localhost:<port>"}]}],
  "terminal": true
}
```

## Error Handling

| Error | Solution |
|-------|----------|
| Caddy not running | `brew install caddy && caddy start` |
| Port 80 denied | `sudo caddy start` |
| Route exists | Ask to update or pick different name |

## Platform Notes

- **macOS**: `*.localhost` resolves to 127.0.0.1 automatically
- **Linux**: May need `/etc/hosts` entries
