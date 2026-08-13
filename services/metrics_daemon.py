#!/usr/bin/env python3
"""
Metrics Daemon Service
======================
Long-running background service that keeps /agent/memory/metrics.duckdb in sync
with the JSON sources it derives from, so the portal never computes a metric at
render time.

- Wakes every METRICS_DAEMON_POLL_SECONDS (default 300s, floor 60s) and calls
  ``metrics_db.refresh()`` — a no-op unless a source file changed, so an idle
  tick costs a handful of stat() calls.
- Writes /agent/memory/heartbeats/metrics_daemon.heartbeat each tick so the
  service health view can surface failures.
- Reacts to SIGTERM / SIGINT cleanly.

scripts/cycle_close.py also dispatches a one-shot refresh at the end of every
cycle, so the Overview tab is current the moment a cycle lands; this daemon
covers everything else that touches the sources between cycles.

Setup (registered with auto_start=true so service_manager.py auto-start brings
it up each cycle):
  uv run python scripts/service_manager.py start metrics_daemon \\
      --auto-start -- uv run python services/metrics_daemon.py

Management:
  uv run python scripts/service_manager.py status metrics_daemon
  uv run python scripts/service_manager.py stop metrics_daemon
  uv run python scripts/service_manager.py restart metrics_daemon
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --- Paths ---
BASE = Path("/agent")
LOG_DIR = BASE / "memory" / "logs"
LOG_FILE = LOG_DIR / "metrics_daemon.log"
HEARTBEAT_DIR = BASE / "memory" / "heartbeats"
HEARTBEAT_FILE = HEARTBEAT_DIR / "metrics_daemon.heartbeat"

# --- Config ---
# Sources change on the heartbeat cadence (15 min by default), and cycle_close
# dispatches its own refresh the moment a cycle lands — so a tighter poll would
# only burn stat() calls. This covers the between-cycle writers (scheduler,
# bridges, portal actions) and bounds staleness to 5 minutes.
DEFAULT_POLL_INTERVAL = 300
MIN_POLL_INTERVAL = 60  # floor: a tighter poll can't see anything new

log = logging.getLogger("metrics_daemon")


def _resolve_poll_interval() -> int:
    """Parse METRICS_DAEMON_POLL_SECONDS with safe fallback.

    A non-integer or empty value falls back to the default, and anything below
    MIN_POLL_INTERVAL is clamped up to it — both with a warning, rather than
    crashing the daemon at import time.
    """
    raw = os.environ.get("METRICS_DAEMON_POLL_SECONDS")
    if raw is None or raw == "":
        return DEFAULT_POLL_INTERVAL
    try:
        n = int(raw)
    except ValueError:
        log.warning(
            f"Invalid METRICS_DAEMON_POLL_SECONDS={raw!r}; "
            f"falling back to {DEFAULT_POLL_INTERVAL}s"
        )
        return DEFAULT_POLL_INTERVAL
    if n < MIN_POLL_INTERVAL:
        log.warning(
            f"METRICS_DAEMON_POLL_SECONDS={n} below minimum "
            f"{MIN_POLL_INTERVAL}s; clamping"
        )
        return MIN_POLL_INTERVAL
    return n


def _import_metrics_db():
    """Import metrics_db.py from /agent/scripts (or the dev checkout's scripts/).

    Done lazily so this module can be imported on hosts that don't have
    /agent (the docstring promises import safety).
    """
    scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import metrics_db  # noqa: PLC0415 — intentional lazy import

    return metrics_db


def _setup_logging():
    """Create log/heartbeat dirs and attach file+stdout handlers.

    Done in main() (not at import) so the module can be imported on hosts
    without /agent (e.g. tests, dev machines).
    """
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


def _write_heartbeat():
    """Touch the heartbeat file so the service health view knows we're alive."""
    try:
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError as e:
        log.warning(f"Could not write heartbeat: {e}")


def _interruptible_sleep(seconds: float, is_running) -> None:
    """Sleep in 1-second slices so SIGTERM is observed within ~1s.

    *is_running* is a zero-arg callable returning False once shutdown begins.
    Guards against negative remainders if the loop body runs slowly.
    """
    end = time.monotonic() + seconds
    while is_running():
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def main() -> int:
    _setup_logging()
    poll_interval = _resolve_poll_interval()
    metrics_db = _import_metrics_db()

    log.info("=" * 60)
    log.info(f"Metrics Daemon starting up (poll interval: {poll_interval}s)")
    log.info(f"Database: {metrics_db.db_path()}")
    log.info("=" * 60)

    shutdown_requested = False

    def _handle_shutdown(signum, _frame):
        nonlocal shutdown_requested
        log.info(f"Received signal {signum}, shutting down...")
        shutdown_requested = True

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    consecutive_errors = 0
    while not shutdown_requested:
        _write_heartbeat()
        try:
            started = time.monotonic()
            rebuilt = metrics_db.refresh()
            consecutive_errors = 0
            if rebuilt:
                elapsed_ms = round((time.monotonic() - started) * 1000)
                log.info(f"Rebuilt metrics database in {elapsed_ms}ms")
        except Exception as e:
            consecutive_errors += 1
            # Cap log noise on persistent failures: warn at 1, 5, 10, ...
            if consecutive_errors == 1 or consecutive_errors % 5 == 0:
                log.error(
                    f"refresh failed (run #{consecutive_errors}): {e}",
                    exc_info=True,
                )

        _interruptible_sleep(poll_interval, lambda: not shutdown_requested)

    log.info("Metrics Daemon stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
