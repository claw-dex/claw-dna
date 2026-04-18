"""Shared utilities for agent background services.

Provides atomic JSON file operations with proper file locking to prevent
data corruption when multiple services write to the same files concurrently
(e.g., inbox.json written by telegram_bridge, github_watcher, webhook_receiver,
and webhook_receiver's whatsapp sub-handler).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import time as _time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# --- Lock timeout ---
_FLOCK_TIMEOUT = 10.0  # seconds


def _timed_flock(lock_f, timeout: float = _FLOCK_TIMEOUT):
    """Acquire exclusive flock within *timeout* seconds.

    Uses LOCK_NB + exponential backoff instead of SIGALRM so it is safe
    to call from any thread (webhook_receiver uses a thread pool).
    Raises TimeoutError if the lock cannot be acquired in time.
    """
    deadline = _time.monotonic() + timeout
    delay = 0.05  # start at 50 ms
    while True:
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return  # acquired
        except BlockingIOError:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Could not acquire file lock within {timeout}s")
            _time.sleep(min(delay, remaining))
            delay = min(delay * 1.5, 1.0)  # back off up to 1 s


# --- Canonical paths ---
MESSAGES_DIR = Path("/agent/messages")
INBOX_FILE = MESSAGES_DIR / "inbox.json"


def atomic_write_json(path: Path, data, **kwargs):
    """Write JSON atomically: write to temp file, then os.replace().

    Prevents data corruption if the process is killed mid-write.
    Temp file is created in the same directory as the target to ensure
    os.replace() is an atomic rename on the same filesystem.
    """
    content = json.dumps(data, **kwargs)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}."
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_json_file(path: Path, default=None):
    """Safely read and parse a JSON file, returning default on error.

    Type-safety: the parsed JSON is validated against the type of *default*
    (list or dict).  If the file contains valid JSON of the wrong type
    (e.g. a dict when a list is expected), *default* is returned instead
    of letting callers crash on unexpected slice/iteration operations.
    """
    if default is None:
        default = []
    try:
        if not path.exists():
            return default
        raw = path.read_text().strip()
        if not raw:
            return default
        data = json.loads(raw)
        # Validate parsed type matches what the caller expects
        if not isinstance(data, type(default)):
            log.warning(
                f"Type mismatch in {path}: expected {type(default).__name__}, "
                f"got {type(data).__name__}; returning default"
            )
            return default
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Failed to read {path}: {e}")
        return default


def _inbox_dedup_key(item: dict) -> str:
    """Generate a deduplication key for an inbox item.

    Uses type + content (stripped/lowered) so that identical goals or
    messages submitted by different services (or the same service across
    poll cycles) are recognised as duplicates while they sit unprocessed
    in the inbox.
    """
    t = str(item.get("type", "")).strip().lower()
    c = str(item.get("content", "")).strip().lower()
    return f"{t}:{c}"


def write_to_inbox(
    items: list, *, inbox_file: Path | None = None, dedup: bool = True
) -> bool:
    """Append items to inbox.json atomically with file locking.

    Uses fcntl.flock on a shared lock file to coordinate between all
    service processes that may write to inbox.json concurrently.

    When *dedup* is True (default), items whose type+content already
    exist in the inbox are silently skipped, preventing duplicate goals
    from piling up when services fire before the inbox is consumed.

    Returns True on success, False on failure (so callers can detect and
    handle write failures rather than silently losing data).
    """
    if not items:
        return True
    target = inbox_file or INBOX_FILE
    lock_path = str(target) + ".lock"
    try:
        with open(lock_path, "a+") as lock_f:
            _timed_flock(lock_f)
            try:
                existing = []
                if target.exists():
                    raw = target.read_text().strip()
                    if raw:
                        try:
                            parsed = json.loads(raw)
                            existing = parsed if isinstance(parsed, list) else []
                        except json.JSONDecodeError:
                            log.warning(
                                f"{target.name} contained invalid JSON, starting fresh"
                            )
                            existing = []

                if dedup:
                    existing_keys = {
                        _inbox_dedup_key(e) for e in existing if isinstance(e, dict)
                    }
                    new_items = []
                    for item in items:
                        key = _inbox_dedup_key(item) if isinstance(item, dict) else ""
                        if key and key in existing_keys:
                            log.debug(f"Skipping duplicate inbox item: {key[:80]}")
                        else:
                            new_items.append(item)
                            if key:
                                existing_keys.add(key)
                    skipped = len(items) - len(new_items)
                    if skipped:
                        log.info(
                            f"Dedup: skipped {skipped}/{len(items)} duplicate item(s)"
                        )
                    items = new_items

                if items:
                    existing.extend(items)
                    atomic_write_json(target, existing, indent=2)
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
        if items:
            log.info(f"Added {len(items)} item(s) to {target.name}")
        return True
    except Exception as e:
        log.error(f"Failed to write {target.name}: {e}")
        return False


def write_to_outbox(items: list, *, outbox_file: Path | None = None) -> bool:
    """Append items to outbox.json atomically with file locking.

    Mirrors write_to_inbox but for the outbox. Prevents data loss when
    multiple writers (health-notify, outbox-manager, cycle-close) and
    readers (telegram_bridge, whatsapp_bridge_handler) access outbox.json
    concurrently.

    Returns True on success, False on failure.
    """
    if not items:
        return True
    target = outbox_file or (MESSAGES_DIR / "outbox.json")
    lock_path = str(target) + ".lock"
    try:
        with open(lock_path, "a+") as lock_f:
            _timed_flock(lock_f)
            try:
                existing = []
                if target.exists():
                    raw = target.read_text().strip()
                    if raw:
                        try:
                            parsed = json.loads(raw)
                            existing = parsed if isinstance(parsed, list) else []
                        except json.JSONDecodeError:
                            log.warning(
                                f"{target.name} contained invalid JSON, starting fresh"
                            )
                            existing = []
                existing.extend(items)
                atomic_write_json(target, existing, indent=2)
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
        log.info(f"Added {len(items)} item(s) to {target.name}")
        return True
    except Exception as e:
        log.error(f"Failed to write {target.name}: {e}")
        return False


def read_outbox_locked(*, outbox_file: Path | None = None) -> list:
    """Read outbox.json under the shared file lock.

    Ensures reads are serialized with writes (write_to_outbox,
    locked_outbox_rw) so readers never see a stale or partially-written
    snapshot.  Returns an empty list on any error.
    """
    target = outbox_file or (MESSAGES_DIR / "outbox.json")
    lock_path = str(target) + ".lock"
    try:
        with open(lock_path, "a+") as lock_f:
            _timed_flock(lock_f)
            try:
                if not target.exists():
                    return []
                raw = target.read_text().strip()
                if not raw:
                    return []
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"Failed to read {target.name} under lock: {e}")
                return []
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except Exception as e:
        log.warning(f"Failed to acquire lock for reading {target.name}: {e}")
        # Fallback: read without lock rather than losing all outbox messages
        return read_json_file(target, default=[])


def locked_outbox_rw(fn, *, outbox_file: Path | None = None) -> bool:
    """Read-modify-write outbox.json under a file lock.

    *fn* receives the current list and must return the new list to write.
    Used by outbox-manager for archive/trim operations that need to
    read the full list, transform it, and write back atomically.

    Returns True on success, False on failure.
    """
    target = outbox_file or (MESSAGES_DIR / "outbox.json")
    lock_path = str(target) + ".lock"
    try:
        with open(lock_path, "a+") as lock_f:
            _timed_flock(lock_f)
            try:
                existing = []
                if target.exists():
                    raw = target.read_text().strip()
                    if raw:
                        try:
                            parsed = json.loads(raw)
                            existing = parsed if isinstance(parsed, list) else []
                        except json.JSONDecodeError:
                            log.warning(f"{target.name} invalid JSON, starting fresh")
                new_data = fn(existing)
                if new_data is None:
                    log.warning(
                        f"locked_outbox_rw callback returned None for {target.name}; "
                        "writing empty list to prevent corruption"
                    )
                    new_data = []
                atomic_write_json(target, new_data, indent=2)
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
        return True
    except Exception as e:
        log.error(f"Failed to update {target.name}: {e}")
        return False


def locked_json_rw(fn, *, json_file: Path, default=None) -> bool:
    """Read-modify-write any JSON file under a file lock.

    *fn* receives the current data and must return the new data to write.
    Generic version of locked_outbox_rw — works with any JSON file.

    Returns True on success, False on failure.
    """
    if default is None:
        default = []
    lock_path = str(json_file) + ".lock"
    try:
        with open(lock_path, "a+") as lock_f:
            _timed_flock(lock_f)
            try:
                existing = default
                if json_file.exists():
                    raw = json_file.read_text().strip()
                    if raw:
                        try:
                            existing = json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning(f"{json_file.name} invalid JSON, using default")
                new_data = fn(existing)
                if new_data is None:
                    log.warning(
                        f"locked_json_rw callback returned None for {json_file.name}; "
                        "preserving existing data to prevent corruption"
                    )
                    new_data = existing
                atomic_write_json(json_file, new_data, indent=2)
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
        return True
    except Exception as e:
        log.error(f"Failed to update {json_file.name}: {e}")
        return False


# --- Canonical paths (service errors) ---
SERVER_ERRORS_FILE = Path("/agent/memory/server_errors.json")
_MAX_SERVICE_ERRORS = 50


def surface_error(
    service_name: str,
    error,
    *,
    context: str = "",
    max_errors: int = _MAX_SERVICE_ERRORS,
) -> None:
    """Surface a service error to server_errors.json for agent observability.

    Writes a structured error entry that is visible to cycle-start, self-heal,
    and error-triage cycles — enabling automatic detection and recovery of
    service failures that would otherwise only appear in log files.

    Uses the same ``tab`` field as portal tab errors so existing consumers
    (cycle-start, self_test, webhook_receiver) display service entries without
    any changes.  A ``source_type="service"`` field distinguishes these entries
    from portal tab errors (``source_type="portal_tab"``) for future filtering.

    This function is fire-and-forget: it logs a warning on failure but never
    raises, so it is safe to call from within exception handlers.
    """
    try:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tab": service_name,
            "source_type": "service",
            "error": str(error),
            "error_type": (
                type(error).__name__ if isinstance(error, BaseException) else "str"
            ),
        }
        if context:
            entry["context"] = context

        def _append_and_trim(existing):
            if not isinstance(existing, list):
                existing = []
            existing.append(entry)
            return existing[-max_errors:]

        locked_json_rw(_append_and_trim, json_file=SERVER_ERRORS_FILE, default=[])
    except Exception as exc:  # noqa: BLE001
        log.warning(
            f"surface_error: failed to write service error for {service_name!r}: {exc}"
        )


def append_to_history(items: list, history_file: Path, *, max_entries: int = 500):
    """Append items to a history JSON file with file locking and size cap.

    Uses the same locking pattern as write_to_inbox to prevent concurrent
    write corruption.
    """
    if not items:
        return
    lock_path = str(history_file) + ".lock"
    try:
        with open(lock_path, "a+") as lock_f:
            _timed_flock(lock_f)
            try:
                history = []
                if history_file.exists():
                    raw = history_file.read_text().strip()
                    if raw:
                        try:
                            parsed = json.loads(raw)
                            history = parsed if isinstance(parsed, list) else []
                        except json.JSONDecodeError:
                            log.warning(
                                f"{history_file.name} contained invalid JSON, starting fresh"
                            )
                            history = []
                history.extend(items)
                history = history[-max_entries:]
                atomic_write_json(history_file, history, indent=2)
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except Exception as e:
        log.warning(f"Failed to write {history_file.name}: {e}")
