#!/usr/bin/env python3
"""
Webhook Receiver Service
========================
Generic HTTP webhook handler that captures incoming webhook payloads and
writes them to /agent/messages/inbox.json for agent processing.

- Listens on port 8082
- Caddy routes /webhook/* to this service (prefix is stripped before proxying)
- POST/PUT/DELETE/PATCH: writes event to inbox.json (type="event", source="webhook")
  and logs full payload to webhook_receiver.log for audit
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

from shared import surface_error, write_to_inbox

# --- Paths ---
BASE = Path("/agent")
LOG_DIR        = BASE / "memory" / "logs"
LOG_FILE       = LOG_DIR / "webhook_receiver.log"
HEARTBEAT_DIR  = BASE / "memory" / "heartbeats"
HEARTBEAT_FILE = HEARTBEAT_DIR / "webhook_receiver.heartbeat"

# --- Config ---
WEBHOOK_PORT  = 8082
MAX_BODY_SIZE = 1_048_576  # 1 MB

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
        """Handle POST/PUT/DELETE/PATCH: write event to inbox.json."""
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

            now = datetime.now(timezone.utc).isoformat()

            # Build full event record for logging
            record = {
                "timestamp": now,
                "method": self.command,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "headers": dict(self.headers),
                "body": body_parsed if body_parsed is not None else body_text,
                "body_type": "json" if body_parsed is not None else "text",
                "content_length": content_length,
                "client_address": self.client_address[0],
            }

            # Log full payload to webhook_receiver.log as the audit trail
            log.info(f"Webhook event: {json.dumps(record)}")

            # Sanitize payload into human-readable content string for inbox
            query_str = f"?{parsed.query}" if parsed.query else ""

            # Strip headers that are injected by the infrastructure (Caddy proxy)
            # or that carry sensitive credentials — these are irrelevant/unsafe in inbox
            _SKIP_HEADERS = {
                # Caddy-injected
                "via", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host",
                "x-real-ip",
                # Infrastructure noise
                "host",
                # Sensitive credentials
                "authorization", "cookie", "x-api-key",
                "x-webhook-secret", "x-hub-signature", "x-hub-signature-256",
                "x-auth-token",
            }
            header_pairs = ", ".join(
                f"{k}: {v}" for k, v in self.headers.items()
                if k.lower() not in _SKIP_HEADERS
            )

            body_str = (
                json.dumps(body_parsed) if body_parsed is not None else body_text
            )
            _INBOX_BODY_MAX = 4096  # 4 KB — full payload already in webhook_receiver.log
            if len(body_str) > _INBOX_BODY_MAX:
                body_str = body_str[:_INBOX_BODY_MAX] + f"\n[truncated {len(body_str) - _INBOX_BODY_MAX} bytes — full payload in webhook_receiver.log]"

            content_parts = [f"{self.command} {parsed.path}{query_str}"]
            if header_pairs:
                content_parts.append(f"Headers: {header_pairs}")
            if body_str:
                content_parts.append(f"Body: {body_str}")
            content_parts.append(f"From: {self.client_address[0]}")
            content = "\n".join(content_parts)

            inbox_item = {
                "type": "event",
                "content": content,
                "timestamp": now,
                "received_at": now,
                "source": "webhook",
                "priority": 3,
            }

            success = write_to_inbox([inbox_item], dedup=False)

            if success:
                self._send_json(200, {"status": "received"})
            else:
                log.error(f"Failed to write inbox item for {self.command} {parsed.path}")
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

    # Start HTTP server in a thread
    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()
    log.info(f"HTTP server started on port {WEBHOOK_PORT}")

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
        server.shutdown()
        serve_thread.join(timeout=5)
        log.info("Webhook Receiver stopped.")


if __name__ == "__main__":
    main()
