#!/usr/bin/env python3
"""
Scheduler Daemon Service
========================
Long-running background service that monitors /agent/memory/scheduled_tasks.json
and injects due tasks into /agent/messages/inbox.json without depending on
heartbeat cycle frequency.

- Wakes every POLL_INTERVAL_SECONDS (default 30s) and calls
  scheduler.check_and_inject() — the same atomic, flock-guarded function used
  by `scripts/scheduler.py --check`.
- Writes /agent/memory/heartbeats/scheduler_daemon.heartbeat each tick so the
  service health view can surface failures.
- Reacts to SIGTERM / SIGINT cleanly.

Setup (registered with auto_start=true so service_manager.py auto-start brings
it up each cycle):
  uv run python scripts/service_manager.py start scheduler_daemon \\
      --auto-start -- uv run python services/scheduler_daemon.py

Management:
  uv run python scripts/service_manager.py status scheduler_daemon
  uv run python scripts/service_manager.py stop scheduler_daemon
  uv run python scripts/service_manager.py restart scheduler_daemon
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
LOG_FILE = LOG_DIR / "scheduler_daemon.log"
HEARTBEAT_DIR = BASE / "memory" / "heartbeats"
HEARTBEAT_FILE = HEARTBEAT_DIR / "scheduler_daemon.heartbeat"

# --- Config ---
DEFAULT_POLL_INTERVAL = 30
MIN_POLL_INTERVAL = 1  # avoid busy-loop if env value is bogus

log = logging.getLogger("scheduler_daemon")


def _resolve_poll_interval() -> int:
    """Parse SCHEDULER_DAEMON_POLL_SECONDS with safe fallback.

    A bogus value (non-integer, empty, or <= 0) falls back to the default
    and emits a warning rather than crashing the daemon at import time.
    """
    raw = os.environ.get("SCHEDULER_DAEMON_POLL_SECONDS")
    if raw is None or raw == "":
        return DEFAULT_POLL_INTERVAL
    try:
        n = int(raw)
    except ValueError:
        log.warning(
            f"Invalid SCHEDULER_DAEMON_POLL_SECONDS={raw!r}; "
            f"falling back to {DEFAULT_POLL_INTERVAL}s"
        )
        return DEFAULT_POLL_INTERVAL
    if n < MIN_POLL_INTERVAL:
        log.warning(
            f"SCHEDULER_DAEMON_POLL_SECONDS={n} below minimum "
            f"{MIN_POLL_INTERVAL}s; clamping"
        )
        return MIN_POLL_INTERVAL
    return n


def _import_scheduler():
    """Import scheduler.py from /agent/scripts (or the dev checkout's scripts/).

    Done lazily so this module can be imported on hosts that don't have
    /agent (the docstring promises import safety).
    """
    scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import scheduler  # noqa: PLC0415 — intentional lazy import

    return scheduler


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
    scheduler = _import_scheduler()

    log.info("=" * 60)
    log.info(f"Scheduler Daemon starting up (poll interval: {poll_interval}s)")
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
            injected = scheduler.check_and_inject()
            consecutive_errors = 0
            if injected:
                log.info(f"Injected {injected} due task(s) into inbox")
        except Exception as e:
            consecutive_errors += 1
            # Cap log noise on persistent failures: warn at 1, 5, 25, 125, ...
            if consecutive_errors == 1 or consecutive_errors % 5 == 0:
                log.error(
                    f"check_and_inject failed (run #{consecutive_errors}): {e}",
                    exc_info=True,
                )

        _interruptible_sleep(poll_interval, lambda: not shutdown_requested)

    log.info("Scheduler Daemon stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
