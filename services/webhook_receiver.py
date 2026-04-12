#!/usr/bin/env python3
"""
Webhook Receiver Service
========================
Generic HTTP webhook handler that captures incoming webhook payloads and
records complete request information to /agent/messages/webhook.json.

- Listens on port 8082
- Caddy routes /webhook/* to this service (prefix is stripped before proxying)
- POST/PUT/DELETE/PATCH: records full request info to webhook.json
- GET/HEAD/OPTIONS: returns 200 without recording (probes/preflights)

Setup:
  Start via service manager:
    uv run python scripts/service-manager.py start webhook_receiver 8082 -- uv run python services/webhook_receiver.py

Management:
  uv run python scripts/service-manager.py status webhook_receiver   # check status
  uv run python scripts/service-manager.py stop webhook_receiver     # stop service
  uv run python scripts/service-manager.py list                     # list all services
"""

import json
import signal
import sys
import time
import logging
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

from shared import locked_json_rw, surface_error

# --- Paths ---
BASE = Path("/agent")
WEBHOOK_FILE   = BASE / "messages" / "webhook.json"
LOG_DIR        = BASE / "memory" / "logs"
LOG_FILE       = LOG_DIR / "webhook_receiver.log"
HEARTBEAT_DIR  = BASE / "memory" / "heartbeats"
HEARTBEAT_FILE = HEARTBEAT_DIR / "webhook_receiver.heartbeat"

# --- Config ---
WEBHOOK_PORT        = 8082
MAX_WEBHOOK_ENTRIES = 200
MAX_BODY_SIZE       = 1_048_576  # 1 MB

CADDY_ADMIN    = "http://localhost:2019"
CADDY_ROUTE_ID = "webhook-receiver"

# --- Logging ---
LOG_DIR.mkdir(parents=True, exist_ok=True)
HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("webhook_receiver")


def _write_heartbeat():
    try:
        HEARTBEAT_FILE.write_text(str(time.time()))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Caddy route management
# ---------------------------------------------------------------------------

def register_caddy_route():
    route = {
        "@id": CADDY_ROUTE_ID,
        "match": [{"path": ["/webhook", "/webhook/*"]}],
        "handle": [
            {"handler": "rewrite", "strip_path_prefix": "/webhook"},
            {"handler": "reverse_proxy", "upstreams": [{"dial": f"localhost:{WEBHOOK_PORT}"}]},
        ],
    }
    try:
        requests.delete(f"{CADDY_ADMIN}/id/{CADDY_ROUTE_ID}", timeout=5)
        resp = requests.post(
            f"{CADDY_ADMIN}/config/apps/http/servers/gateway/routes",
            json=route,
            timeout=10,
        )
        if resp.ok:
            log.info(f"Registered Caddy route: /webhook/* -> localhost:{WEBHOOK_PORT}")
        else:
            log.warning(f"Caddy route registration failed ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:
        log.warning(f"Could not register Caddy route: {e}")


def deregister_caddy_route():
    try:
        resp = requests.delete(f"{CADDY_ADMIN}/id/{CADDY_ROUTE_ID}", timeout=10)
        if resp.ok:
            log.info("Deregistered Caddy route")
        else:
            log.warning(f"Caddy deregister failed ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:
        log.warning(f"Could not deregister Caddy route: {e}")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log.debug(f"HTTP: {format % args}")

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":  # HEAD must not include a body (RFC 9110 §9.3.2)
            self.wfile.write(body)

    def _handle_read_request(self):
        """Handle GET/HEAD/OPTIONS: respond 200 without recording."""
        parsed = urlparse(self.path)
        log.debug(f"{self.command} {parsed.path} from {self.client_address[0]} (not recorded)")
        self._send_json(200, {"status": "ok"})

    def _handle_write_request(self):
        """Handle POST/PUT/DELETE/PATCH: record full request to webhook.json."""
        try:
            parsed = urlparse(self.path)

            # Read body
            try:
                content_length = int(self.headers.get("Content-Length") or 0)
            except (ValueError, TypeError):
                content_length = 0
            body_raw = b""
            if content_length > 0:
                body_raw = self.rfile.read(min(content_length, MAX_BODY_SIZE))

            # Attempt JSON parse, fall back to text
            body_text = body_raw.decode("utf-8", errors="replace") if body_raw else ""
            body_parsed = None
            if body_text:
                try:
                    body_parsed = json.loads(body_text)
                except (json.JSONDecodeError, ValueError):
                    pass

            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "method": self.command,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "headers": dict(self.headers),
                "body": body_parsed if body_parsed is not None else body_text,
                "body_type": "json" if body_parsed is not None else "text",
                "content_length": content_length,
                "client_address": self.client_address[0],
            }

            def _append_and_cap(existing):
                if not isinstance(existing, list):
                    existing = []
                existing.append(record)
                return existing[-MAX_WEBHOOK_ENTRIES:]

            success = locked_json_rw(_append_and_cap, json_file=WEBHOOK_FILE, default=[])

            if success:
                log.info(f"Recorded {self.command} {parsed.path} from {self.client_address[0]}")
                self._send_json(200, {"status": "received"})
            else:
                log.error(f"Failed to write webhook record for {self.command} {parsed.path}")
                self._send_json(500, {"status": "error", "detail": "failed to persist"})

        except Exception as e:
            log.error(f"Error handling {self.command} {self.path}: {e}", exc_info=True)
            surface_error("webhook_receiver", e, context=f"{self.command} {self.path}")
            try:
                self._send_json(500, {"status": "error"})
            except Exception:
                pass

    # Route methods to handlers
    do_GET     = _handle_read_request
    do_HEAD    = _handle_read_request
    do_OPTIONS = _handle_read_request
    do_POST    = _handle_write_request
    do_PUT     = _handle_write_request
    do_DELETE  = _handle_write_request
    do_PATCH   = _handle_write_request


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("Webhook Receiver starting up")
    log.info("=" * 60)

    # Ensure output directory exists
    WEBHOOK_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Start HTTP server in a thread
    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()
    log.info(f"HTTP server started on port {WEBHOOK_PORT}")

    # Register Caddy route
    register_caddy_route()

    # Signal handling
    shutdown_requested = False

    def _handle_shutdown(signum, frame):
        nonlocal shutdown_requested
        log.info(f"Received signal {signum}, shutting down...")
        shutdown_requested = True

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    log.info(f"Ready. Listening for webhooks on port {WEBHOOK_PORT}...")
    try:
        while not shutdown_requested:
            _write_heartbeat()
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutdown via KeyboardInterrupt")
    finally:
        log.info("Cleaning up...")
        deregister_caddy_route()
        server.shutdown()
        serve_thread.join(timeout=5)
        log.info("Webhook Receiver stopped.")


if __name__ == "__main__":
    main()
