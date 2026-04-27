# System Check

Periodic health check of the agent's runtime, internal state, and external
surface. Analyse the findings, classify any issues by severity, and take
recovery action when a core component is unhealthy. Write a brief summary to
your outbox; alert the user immediately on any critical issue.

## 1. Local health

Run the fast automated probes first — they cover the portal process, ports,
and Streamlit health endpoint:

```bash
bash /agent/scripts/health_check.sh --retries 2 --delay 2
uv run python scripts/self_test.py --record
```

Also inspect:

- Service status via `uv run python scripts/service_manager.py status` (or the
  equivalent inspection — list each managed service and its state).
- Resource usage: CPU load, memory, disk free on `/agent` and `/tmp`.
- Recent errors in `logs/` (e.g. `webhook_receiver.log`, server logs) — scan
  the tail for tracebacks or repeated `[ERROR]` lines since the last check.
- Heartbeat freshness for each background service (`*.heartbeat` files):
  flag any heartbeat older than ~3× its expected interval.

## 2. Public webhook probe

The webhook endpoints MUST be public and unauthenticated so external services
(e.g. Meta, the WhatsApp bridge) can deliver events. Verify this from the
outside:

1. Read the configured public hostname:
   ```
   uv run python scripts/portal_config.py hostname --show
   ```
   If no public hostname is configured, record this as a warning and skip the
   remaining probe steps.

2. Probe the base webhook receiver and each registered sub-handler that
   should be live:
   ```
   curl -sS -o /dev/null -D - <public_url>/webhook/
   curl -sS -o /dev/null -D - <public_url>/webhook/whatsapp-bridge
   ```

3. Validate each response:
   - Status code MUST be `200`.
   - Response MUST NOT include a `WWW-Authenticate` header.
   - Status MUST NOT be `401` or `403`, and the body MUST NOT indicate that
     authentication (basic auth or otherwise) is required.

Any of the following counts as a **critical issue** and must be alerted:

- Non-200 response from `<public_url>/webhook/` or any expected sub-handler.
- `401` / `403` response, or a `WWW-Authenticate` header on any webhook URL.
- Connection failure / DNS failure / TLS error reaching the public hostname.

## 3. Analyse findings

For every anomaly collected in steps 1–2, decide:

- **Severity** — `critical` (core component down or webhook unreachable /
  auth-protected), `warning` (degraded but functional, e.g. high resource
  usage, stale non-essential heartbeat, public hostname unset), or `info`
  (transient blip already recovered).
- **Component** — portal app (Streamlit on 8081), Caddy gateway (8080),
  webhook_receiver, scheduler_daemon, memory/state files, external network,
  or other.
- **Likely root cause** — based on logs and probe output, not guesswork. If
  the data is insufficient to decide, say so explicitly.

## 4. Recovery actions

When the issue affects a **core system component** — the agent portal app,
the Caddy gateway, or anything that breaks the user's communication channel —
**switch to `prompts/self-heal.md` and follow that recovery procedure**. That
prompt is the authoritative recipe for portal/gateway recovery; do not
improvise an alternative fix here.

For non-core issues:

- Webhook probe failure with portal still healthy → check Caddy routing and
  the `webhook_receiver` service. If the receiver is down, restart it via
  `scripts/service_manager.py`. If Caddy itself is wrong, escalate via
  `prompts/self-heal.md`.
- Public hostname unset → record a warning and surface it to the user; do
  not attempt to set it automatically.
- Service heartbeat stale but service non-critical → restart that service
  via `scripts/service_manager.py` and re-check.
- Disk / memory pressure → note it, suggest cleanup, do not delete data
  without explicit user instruction.

Always re-run the relevant probe after any recovery action and confirm the
issue is cleared before closing this check.

## 5. Output

Append a single summary message to the outbox that covers **every part** of
this system check — not just the webhook probe. Do not omit a section just
because it passed; an explicit "OK" line for each area is required so the
user can see at a glance that it was actually verified.

The summary MUST include:

- **Overall verdict** — `OK` / `degraded` / `critical` with one-line reason.
- **Local health** — result of `health_check.sh` and `self_test.py`
  (pass/fail + key counters).
- **Services** — one line per managed service with its state and heartbeat
  freshness (portal app, Caddy, webhook_receiver, scheduler_daemon, and any
  other service listed by `service_manager.py`).
- **Resources** — CPU load, memory used/free, disk free on `/agent` and
  `/tmp`.
- **Logs** — counts of new errors / tracebacks since the previous check
  (per log file), or "clean" if none.
- **Public hostname** — the configured value, or "not configured".
- **Webhook probe** — per-URL result (status code + pass/fail + whether a
  `WWW-Authenticate` header was present).
- **Issues found** — for each: severity, component, likely root cause, and
  the action taken (including whether `self-heal.md` was invoked).
- **Resolution status** — whether all critical issues are now cleared, or
  which remain open and why.

Alert the user immediately for any unresolved critical issue.
