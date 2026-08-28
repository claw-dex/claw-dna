"""Shared utilities for agent background services.

Provides atomic JSON file operations with proper file locking to prevent
data corruption when multiple services write to the same files concurrently
(e.g., inbox.json written by telegram_bridge, github_watcher, webhook_receiver,
and webhook_receiver's whatsapp sub-handler).
"""

from __future__ import annotations

import dataclasses
import errno
import fcntl
import json
import logging
import os
import shutil
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
# Unified per-surface chat layout. Both the portal (name="main") and every
# internal-agent (name=<agent name>) keep their chat history, the
# clear_chat archive, and the SDK resume-id sidecar in one directory:
#     /agent/memory/chat/<name>/{chat_history.json, chat_history_archive.json,
#                                <name>.session}
# Inbox files (inbox.json, inbox_history.json) are unrelated to chat and
# stay under /agent/messages/internal/<name>/ — they are not touched by
# the chat-path helpers below.
CHAT_DIR = Path("/agent/memory/chat")
# Sentinel file written once after `migrate_chat_layout` succeeds so the
# migrator does not re-walk the legacy paths on every process restart.
CHAT_MIGRATION_SENTINEL = CHAT_DIR / ".migration_done"


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
    # Stamp received_at on any dict item missing it so cycle_close.py can
    # reliably distinguish pre-cycle items (archive) from mid-cycle arrivals
    # (carry forward). All current callers set this explicitly; this is a
    # belt-and-suspenders fallback for future writers.
    _now_iso = datetime.now(timezone.utc).isoformat()
    for item in items:
        if isinstance(item, dict) and not item.get("received_at"):
            item["received_at"] = _now_iso
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


# ---------------------------------------------------------------------------
# claude-agent-sdk transport tuning — shared by every SDK call site
# (app/chat.py, services/internal_agent_chat.py, scripts/memory_ask.py).
# ---------------------------------------------------------------------------

# Max bytes the SDK buffers while assembling one JSON message from the CLI's
# stdout. The SDK default is 1 MiB, which a single large tool result (a big
# Read, a verbose Bash command, a long Write) blows past — surfacing as
# "JSON message exceeded maximum buffer size of 1048576 bytes" and killing the
# stream mid-turn.
#
# 8 MiB, not more: the SDK's reader speculatively runs `json.loads` on the
# whole buffer after appending *every* ~64 KiB stdout chunk (see
# claude_agent_sdk/_internal/transport/subprocess_cli.py::_read_messages_impl),
# so assembling an N-byte message costs O(N²) scanning, synchronously, on the
# event loop. At 8 MiB a worst-case message costs ~0.5 GB of parse work; at
# 64 MiB it would be ~34 GB and would stall the loop (blocking interrupts and
# control responses) for minutes. 8x the default covers realistic tool
# payloads while keeping that ceiling bounded.
MAX_SDK_BUFFER_BYTES = 8 * 1024 * 1024

# Set once we've warned about an SDK build without `max_buffer_size`. The
# condition is static per process but `sdk_buffer_size_kwargs` is re-invoked
# on every reconnect, so without this a thrashing agent floods the log.
_warned_no_buffer_option = False


def sdk_buffer_size_kwargs(options_cls) -> dict:
    """Return ``{"max_buffer_size": ...}`` if *options_cls* supports it.

    `max_buffer_size` only exists on newer `ClaudeAgentOptions` builds;
    passing it to an older one would raise TypeError and break every SDK call
    site, so probe the dataclass fields instead of pinning a version. Also
    returns ``{}`` for non-dataclass stand-ins (e.g. test doubles).
    """
    global _warned_no_buffer_option
    try:
        names = {f.name for f in dataclasses.fields(options_cls)}
    except Exception:
        return {}
    if "max_buffer_size" in names:
        return {"max_buffer_size": MAX_SDK_BUFFER_BYTES}
    if not _warned_no_buffer_option:
        _warned_no_buffer_option = True
        log.warning(
            "claude-agent-sdk build has no max_buffer_size option; "
            "large tool results may exceed the 1 MiB default buffer"
        )
    return {}


# ── SDK session tuning: model + effort ────────────────────────────
# `effort` guides how much thinking the model does per turn. These are the
# levels the pinned claude-agent-sdk (0.1.76, see uv.lock) declares on
# `ClaudeAgentOptions.effort`, and the SDK maps them 1:1 onto the
# `claude --effort <level>` CLI flag — the same knob
# `.claude/settings.json`'s `effortLevel` sets for the interactive CLI.
# "xhigh" is Opus only (it falls back to "high" on other models) and was
# added in SDK 0.1.76 — `sdk_effort_kwargs` probes whether the *field* exists,
# not whether a build accepts a given level, so an SDK older than the lockfile
# would reject `--effort xhigh` at connect time. The container installs from
# uv.lock, so 0.1.76 is the floor.
# Defined once here and consumed by the internal-agent daemon, its register
# CLI, and the portal chat tab.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Model aliases the portal chat offers. The daemon accepts any non-empty
# string (full model ids included), so it passes `allowed=None`.
# "inherit" is deliberately absent: it is an AgentDefinition alias and is not
# valid for top-level ClaudeAgentOptions.model.
PORTAL_MODEL_CHOICES = ("opus", "sonnet", "haiku")


def normalize_model(value, allowed=None) -> str | None:
    """Coerce a config-supplied model to a usable value, or ``None``.

    Never raises: `agents.json` and `portal_config.json` are hand-editable, so
    a wrong type or an unknown alias must degrade to "use the SDK default"
    rather than break session startup.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if allowed is not None and value not in allowed:
        return None
    return value


def normalize_effort(value) -> str | None:
    """Coerce a config-supplied effort level to one of `EFFORT_LEVELS`.

    Same contract as `normalize_model`: never raises, unknown/blank/wrong-type
    input becomes ``None`` so the SDK default applies. Callers that want to
    surface a rejected value should compare against their raw input.
    """
    return normalize_model(value, allowed=EFFORT_LEVELS)


# Mirrors `_warned_no_buffer_option`: static per process, but the helper runs
# on every reconnect, so latch the warning.
_warned_no_effort_option = False


def sdk_effort_kwargs(options_cls, effort) -> dict:
    """Return ``{"effort": ...}`` if *effort* is set and *options_cls* takes it.

    `effort` is a newer `ClaudeAgentOptions` field than `model`, and
    pyproject.toml pins `claude-agent-sdk` with no version specifier, so probe
    the dataclass fields rather than assuming it exists. Returns ``{}`` for a
    `None` effort, for an SDK build without the option, and for non-dataclass
    stand-ins (e.g. test doubles).
    """
    global _warned_no_effort_option
    if effort is None:
        return {}
    try:
        names = {f.name for f in dataclasses.fields(options_cls)}
    except Exception:
        return {}
    if "effort" in names:
        return {"effort": effort}
    if not _warned_no_effort_option:
        _warned_no_effort_option = True
        log.warning(
            "claude-agent-sdk build has no effort option; "
            "the configured effort level %r is ignored",
            effort,
        )
    return {}


# Substrings the SDK surfaces when its `claude` subprocess has died (SIGKILL /
# OOM / closed pipe). The negative `exit code: -` prefix matches SIGKILL (-9),
# SIGTERM (-15), SIGABRT (-6), etc. without enumerating each signal. Brittle
# substring matching against exception text — re-validate on SDK upgrades.
DEAD_SUBPROCESS_HINTS = (
    "Cannot write to terminated process",
    "exit code: -",
    "BrokenPipeError",
    "process is not running",
)

# Substrings for a *client-side* reader-task death: when the SDK's stdout
# reader raises (e.g. a tool result overflows `max_buffer_size`), it logs the
# exception, pushes one `{"type": "error"}` + `{"type": "end"}` into the
# message stream, and exits — see query.py::_read_messages. The client object
# still looks connected but every later turn comes back empty, so it needs a
# reconnect. It must NOT be retried the way DEAD_SUBPROCESS_HINTS are: the CLI
# subprocess kept running and may already have executed tools and delivered a
# reply, so replaying the prompt risks duplicate side effects.
#
# Only the CLIJSONDecodeError text is listed. The reader's own log line
# ("Fatal error in message reader: …") never reaches the consumer — it
# forwards `str(e)` alone — so matching on it would be dead code.
READER_DEATH_HINTS = ("Failed to decode JSON",)

# Everything that means "this SDK client can no longer serve turns". Detection
# has to be text-based: the reader task re-raises downstream as a bare
# `Exception(str(e))`, so the original SDK error class never survives.
# "exit code:" (no sign) also catches a non-signal CLI exit, which is fatal for
# a client even though it is not worth replaying a turn over.
FATAL_SDK_ERROR_HINTS = (
    DEAD_SUBPROCESS_HINTS
    + READER_DEATH_HINTS
    + (
        "exit code:",
        "Not connected",
    )
)


# ---------------------------------------------------------------------------
# Per-surface chat path helpers — see CHAT_DIR docstring above.
# ---------------------------------------------------------------------------


def chat_dir(name: str) -> Path:
    return CHAT_DIR / name


def chat_history_path(name: str) -> Path:
    return chat_dir(name) / "chat_history.json"


def chat_archive_path(name: str) -> Path:
    return chat_dir(name) / "chat_history_archive.json"


def session_path(name: str) -> Path:
    """Bare-string sidecar holding the SDK resume id for this surface."""
    return chat_dir(name) / (name + ".session")


def ensure_chat_dir(name: str) -> None:
    """Create the per-surface chat directory and its three files.

    Idempotent: existing files are left untouched. The session sidecar
    starts empty (no resume id yet); history and archive start as ``[]``.
    """
    d = chat_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    for f in (chat_history_path(name), chat_archive_path(name)):
        if not f.exists():
            f.write_text("[]")
    sp = session_path(name)
    if not sp.exists():
        sp.write_text("")


def load_session_id(name: str) -> str | None:
    """Read the SDK resume id for *name*. Returns None when unknown."""
    p = session_path(name)
    if not p.exists():
        return None
    try:
        sid = p.read_text().strip()
    except OSError:
        return None
    return sid or None


def save_session_id(name: str, sid: str) -> None:
    """Persist the SDK resume id for *name* via an atomic write."""
    if not isinstance(sid, str) or not sid.strip():
        return
    chat_dir(name).mkdir(parents=True, exist_ok=True)
    target = session_path(name)
    # Bare-string sidecar — write atomically by temp + replace so a crash
    # mid-write cannot leave the file half-written.
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), prefix=".session.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(sid.strip())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# One-shot migrator: legacy layouts → /agent/memory/chat/<name>/.
# Called from the portal and the internal-agent daemon at startup.
# ---------------------------------------------------------------------------


def migrate_chat_layout(
    *,
    legacy_memory_dir: Path = Path("/agent/memory"),
    legacy_messages_internal: Path = Path("/agent/messages/internal"),
    legacy_sessions_dir: Path = Path("/agent/memory/sessions/internal"),
) -> dict:
    """Move legacy chat files into the unified `/agent/memory/chat/` layout.

    Migrations performed (each is independently idempotent — a step is
    skipped if its destination already exists):

    1. Portal history: ``<memory>/chat_history.json`` → ``chat/main/chat_history.json``
    2. Portal session: ``<memory>/chat_meta.json`` → unwrap ``session_id`` →
       ``chat/main/main.session`` (bare string), then delete the old file.
    3. Internal-agent history & archive: for every directory under
       ``messages/internal/<name>/`` move ``chat_history.json`` and
       ``chat_history_archive.json`` (when present) into
       ``chat/<name>/``.
    4. Internal-agent session sidecars: every
       ``memory/sessions/internal/<name>.session`` is moved to
       ``chat/<name>/<name>.session``.

    A sentinel at ``CHAT_MIGRATION_SENTINEL`` short-circuits subsequent
    calls so this is cheap to invoke unconditionally on every startup.

    Returns a small report dict (counts per category) — primarily useful
    for tests; production code can ignore it.
    """
    report: dict = {
        "portal_history": False,
        "portal_session": False,
        "internal_history": 0,
        "internal_archive": 0,
        "internal_session": 0,
        "skipped": True,
    }

    if CHAT_MIGRATION_SENTINEL.exists():
        return report
    report["skipped"] = False

    CHAT_DIR.mkdir(parents=True, exist_ok=True)

    def _move(src: Path, dst: Path) -> bool:
        if not src.exists():
            return False
        if dst.exists():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, dst)
            return True
        except OSError as exc:
            # `os.replace` only works within a single filesystem; on a
            # cross-mount layout (bind mount / tmpfs / Docker volume) it
            # raises EXDEV. Fall back to copy+unlink so the migrator
            # works in those deployments too.
            if getattr(exc, "errno", None) == errno.EXDEV:
                try:
                    shutil.move(str(src), str(dst))
                    return True
                except OSError as exc2:
                    log.warning(
                        "migrate_chat_layout: cross-fs move failed %s -> %s: %s",
                        src,
                        dst,
                        exc2,
                    )
                    return False
            log.warning(
                "migrate_chat_layout: failed to move %s -> %s: %s", src, dst, exc
            )
            return False

    # 1. Portal chat history.
    src_hist = legacy_memory_dir / "chat_history.json"
    dst_hist = chat_history_path("main")
    if _move(src_hist, dst_hist):
        report["portal_history"] = True

    # 2. Portal session id (chat_meta.json -> main.session).
    # The `portal_session` flag flips to True only after BOTH the new
    # sidecar is in place AND the legacy file has been removed, so a
    # crash mid-step leaves the on-disk world in a recoverable state
    # (the next run re-enters and finishes the cleanup).
    src_meta = legacy_memory_dir / "chat_meta.json"
    dst_sess = session_path("main")
    if src_meta.exists():
        try:
            if not dst_sess.exists():
                data = json.loads(src_meta.read_text() or "{}")
                sid = data.get("session_id") if isinstance(data, dict) else None
                if isinstance(sid, str) and sid.strip():
                    save_session_id("main", sid.strip())
                else:
                    # No session id to preserve — just create an empty
                    # sidecar so the layout is consistent.
                    ensure_chat_dir("main")
            # If we reach here, the destination is in place either from
            # this run or a previous partial run. Remove the legacy
            # file; if unlink fails, leave portal_session=False so the
            # next sweep retries (and re-confirm dst_sess existence
            # cheaply).
            try:
                src_meta.unlink()
                report["portal_session"] = True
            except OSError as exc:
                log.warning(
                    "migrate_chat_layout: could not remove %s: %s", src_meta, exc
                )
        except Exception as exc:
            log.warning(
                "migrate_chat_layout: failed to convert chat_meta.json: %s", exc
            )

    # 3. Internal-agent history + archive.
    if legacy_messages_internal.exists():
        for agent_dir_path in legacy_messages_internal.iterdir():
            if not agent_dir_path.is_dir():
                continue
            name = agent_dir_path.name
            if _move(agent_dir_path / "chat_history.json", chat_history_path(name)):
                report["internal_history"] += 1
            if _move(
                agent_dir_path / "chat_history_archive.json",
                chat_archive_path(name),
            ):
                report["internal_archive"] += 1

    # 4. Internal-agent session sidecars.
    if legacy_sessions_dir.exists():
        for sess_file in legacy_sessions_dir.iterdir():
            if not sess_file.is_file() or sess_file.suffix != ".session":
                continue
            name = sess_file.stem
            if _move(sess_file, session_path(name)):
                report["internal_session"] += 1

    # Mark migration complete so we don't re-walk on the next process boot.
    try:
        CHAT_MIGRATION_SENTINEL.write_text(_now_iso())
    except OSError as exc:
        log.warning("migrate_chat_layout: failed to write sentinel: %s", exc)

    return report


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Agent control-flag mutation — single source of truth shared by the CLI
# (scripts/interact_with_agent.py) and the portal (app/agents_tab.py).
# ---------------------------------------------------------------------------

# Canonical names for the on-disk control fields. Kept here so writers
# (CLI, portal) and the daemon's reader (services/internal_agent_chat.py)
# all reference the same constants.
AGENT_CONTROL_FIELD = "control"
AGENT_CONTROL_CLEAR_CHAT = "clear_chat"
AGENT_CONTROL_CLEAR_SESSION = "clear_session"


def set_agent_control_flag(
    names: list[str],
    key: str,
    *,
    agents_file: Path,
    value: bool = True,
) -> tuple[bool, list[str]]:
    """Set ``control[<key>] = value`` on every named agent in agents.json.

    Done in a single locked read-modify-write so concurrent updates from
    other writers (other CLI invocations, register_internal_agent.py,
    the daemon's strip-after-apply path) cannot interleave or drop
    fields.

    Returns ``(ok, matched_names)``:
    * ``ok`` is False only when the locked write itself failed; an empty
      ``names`` list is treated as a successful no-op.
    * ``matched_names`` lists every name that was actually present in
      agents.json; absent names are silently ignored so callers can
      report them with their own UX.
    """
    if not names:
        return True, []
    targets = set(names)
    matched: set[str] = set()

    def _rw(items):
        if not isinstance(items, list):
            items = []
        for a in items:
            if not isinstance(a, dict):
                continue
            n = a.get("name")
            if n not in targets:
                continue
            ctl = a.get(AGENT_CONTROL_FIELD)
            if not isinstance(ctl, dict):
                ctl = {}
            ctl[key] = value
            a[AGENT_CONTROL_FIELD] = ctl
            matched.add(n)
        return items

    if not locked_json_rw(_rw, json_file=agents_file, default=[]):
        return False, []
    return True, sorted(matched)
