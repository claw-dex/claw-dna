#!/usr/bin/env python3
"""
cycle_close.py — One-command cycle close automation.

Automates the repetitive boilerplate from cycle-close.md:
  1. Marks the in-progress cycles.json entry as completed (computes duration)
  2. Updates state.json (cycle_number, status, last_cycle_summary; clears current_goal)
  3. Appends a journal entry to journal.json
  4. Normalizes cycles.json schema (inlined — no subprocess)
  5. Archives inbox.json items to inbox_history.json, then clears inbox.json
  6. Auto-backs up memory files if last backup >1h old (inlined — no subprocess)
  7. Dispatches the long-term-memory (memvid) flush in a detached background
     process so the script returns immediately. The .mv2 becomes durable a
     few seconds after "Done." prints. Logs to /agent/memory/.memvid_flush.log.
     Use --no-bg-memvid to flush inline (e.g., when a downstream caller needs
     the .mv2 fully written before exit).
  8. Reports what was written

Usage:
    uv run python scripts/cycle_close.py \\
        --type evolve \\
        --category efficiency \\
        --summary "Built cycle_close.py to automate end-of-cycle boilerplate" \\
        --actions "Built scripts/cycle_close.py" "Tested portal health" \\
        --status completed

    # --cycle is OPTIONAL: auto-detected from state.json (state.cycle_number + 1)
    # Override only if auto-detection gives the wrong number:
    uv run python scripts/cycle_close.py --cycle 24 --type evolve ...

    uv run python scripts/cycle_close.py --help

Required flags:
    --type TYPE           Cycle type: evolve | goal | self-heal | dream (see prompts/enum.md → Cycle Type)
    --summary TEXT        1-2 sentence summary of what was done and why it matters

Optional flags:
    --cycle N             Cycle number (integer). Default: auto-detected from state.json
                            (state.cycle_number + 1, or max cycle in cycles.json + 1)
    --category CAT        Evolve / dream category (required when --type is evolve or dream):
                            reliability | observability | capability | efficiency | prompt_evolution
                            | memory_consolidation | sleep
                            (memory_consolidation and deep_sleep are dream-only)
    --actions TEXT…       One or more action strings (space-separated, each in quotes)
    --status STATUS       Cycle status: completed | failed (default: completed)
    --goal TEXT           What you set out to do. Defaults to `cycle_goal` on
                          the in-progress cycle entry (set by cycle_start.py).
                          Pass explicitly to override what cycle_start recorded.
                          state.current_goal is the dynamic in-flight task and
                          is NOT consulted here.
    --no-normalize        Skip cycles.json normalization after writing
    --no-bg-memvid        Run the memvid flush inline instead of in a detached
                            background process (default: background)
    --dry-run             Print what would be written, but write nothing

Exit codes: 0 = success, 1 = error (missing required args, write failure)

Enum Reference: See prompts/enum.md for agent status values and other enums.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

MEMORY = Path("/agent/memory")
SCRIPTS = Path("/agent/scripts")
INBOX_FILE = Path("/agent/messages/inbox.json")
INBOX_HISTORY_FILE = Path("/agent/messages/inbox_history.json")


# ── Inlined: normalize_cycles logic ─────────────────────────────────────────


def _normalize_cycle_entry(entry: dict) -> tuple:
    """Normalize a single cycles.json entry. Returns (normalized_entry, changes_count).

    Handles legacy fields:
      - "timestamp" → "start"
      - rename legacy keys to current schema (cycle → cycle_number,
        status → cycle_status, type → cycle_type, category → cycle_category,
        goal → cycle_goal) via memory_repair.migrate_cycle_entry
      - drop "summary" — summaries live on journal.json now, mirroring them
        onto cycles.json was redundant
      - computes duration_seconds when start+end present but duration missing
      - adds default cycle_status/cycle_type if absent
    """
    from scripts.memory_repair import migrate_cycle_entry

    c = dict(entry)
    n = 0

    if "timestamp" in c and "start" not in c:
        c["start"] = c.pop("timestamp")
        n += 1
    elif "timestamp" in c:
        del c["timestamp"]
        n += 1

    # Apply schema-key renames (idempotent; counts a change if anything moved).
    before_keys = set(c.keys())
    migrate_cycle_entry(c)
    if set(c.keys()) != before_keys:
        n += 1

    # "summary" no longer belongs on cycle records — it lives on journal.json.
    if "summary" in c:
        del c["summary"]
        n += 1

    if "start" in c and "end" in c and "duration_seconds" not in c:
        try:
            dur = round(
                (
                    datetime.fromisoformat(c["end"])
                    - datetime.fromisoformat(c["start"])
                ).total_seconds(),
                1,
            )
            c["duration_seconds"] = dur
            n += 1
        except (ValueError, TypeError):
            pass

    if "cycle_status" not in c:
        c["cycle_status"] = "completed"
        n += 1
    if "cycle_type" not in c:
        c["cycle_type"] = "evolve"
        n += 1
    if "cycle_number" in c and not isinstance(c["cycle_number"], int):
        try:
            c["cycle_number"] = int(c["cycle_number"])
            n += 1
        except (ValueError, TypeError):
            pass

    return c, n


def _run_normalize_inlined(cycles_path: Path, verbose: bool = True) -> int:
    """Normalize cycles.json in-process. Returns number of changes made.

    Replaces the subprocess call to normalize_cycles.py --write --quiet.
    Skips the backup (cycles.json was just written atomically by write_atomic).
    """
    try:
        data = json.loads(cycles_path.read_text())
        if not isinstance(data, list):
            return 0
        normalized, total_changes = [], 0
        for entry in data:
            norm, n = _normalize_cycle_entry(entry)
            normalized.append(norm)
            total_changes += n
        if total_changes > 0:
            tmp = str(cycles_path) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(normalized, f, indent=2)
            os.replace(tmp, str(cycles_path))
        return total_changes
    except Exception:
        return 0


# ── Inbox archiving ─────────────────────────────────────────────────────────


def _inbox_chunks_for_memvid(items: list) -> list:
    """Convert archived inbox messages into memvid chunks (no I/O).

    Returns a list of chunk dicts ready for ``memory_ingest.append_many``.
    Skipped messages (too short / not a dict) are silently dropped to mirror
    the previous per-message ingest semantics.
    """
    if not items:
        return []
    try:
        from scripts.memory_ingest import transform_inbox_entry
    except Exception as e:
        print(f"  ⚠ inbox memvid — import skipped: {e}")
        return []
    out = []
    for msg in items:
        c = transform_inbox_entry(msg)
        if c is not None:
            out.append(c)
    return out


def _parse_iso(ts):
    """Parse an ISO-8601 timestamp; return None if missing or unparseable."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _is_pre_cycle_item(msg, cutoff_dt):
    """True when an inbox item should be archived (predates the cycle start).

    Items missing or with unparseable ``received_at`` are treated as
    pre-existing (legacy items written before this field was required).
    When ``cutoff_dt`` is None, falls back to archiving everything.
    """
    if cutoff_dt is None:
        return True
    if not isinstance(msg, dict):
        return True
    ra_dt = _parse_iso(msg.get("received_at"))
    if ra_dt is None:
        return True
    return ra_dt <= cutoff_dt


def _archive_inbox(
    cycle_start_ts=None,
    ingest_buffer: list = None,
    cycle_number: int | None = None,
):
    """Archive pre-cycle items in /agent/messages/inbox.json to inbox_history.json.

    Items whose ``received_at`` is on or before ``cycle_start_ts`` are archived
    and ingested into long-term memory (best-effort). Items that arrived
    mid-cycle (after ``cycle_start_ts``) are left in inbox.json so the next
    cycle can process them.

    When ``cycle_number`` is provided, each archived item gets ``cycle_number``
    stamped on it (if absent) before being written to ``inbox_history.json`` and
    converted to a memvid chunk, so the value is preserved on both the rebuild
    path (read back from inbox_history.json) and the live append path.

    All inbox read/partition/rewrite happens under an exclusive lock on
    ``inbox.json.lock`` (the same lock used by ``services.shared.write_to_inbox``
    and ``app.shared.AtomicJSON``), so concurrent appenders cannot have their
    messages dropped or double-archived. inbox.json is rewritten *before*
    inbox_history.json is updated, so a failure during rewrite cannot leave
    items duplicated across both files.

    Returns the number of items archived, or -1 on failure. Returns 0 when
    inbox is missing or has no archivable items.
    """
    inbox_path = INBOX_FILE
    history_path = INBOX_HISTORY_FILE
    lock_path = str(inbox_path) + ".lock"

    if not inbox_path.exists():
        return 0

    cutoff_dt = _parse_iso(cycle_start_ts)
    to_archive = []

    try:
        with open(lock_path, "a+") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                try:
                    items = json.loads(inbox_path.read_text())
                except Exception as e:
                    print(f"  ⚠ inbox archive — failed to read inbox.json: {e}")
                    return -1
                if not isinstance(items, list) or not items:
                    return 0

                to_archive = [m for m in items if _is_pre_cycle_item(m, cutoff_dt)]
                if not to_archive:
                    return 0

                kept = [m for m in items if not _is_pre_cycle_item(m, cutoff_dt)]

                # Stamp the closing cycle number onto each archived message so
                # the value travels into both inbox_history.json and the
                # memvid chunk (transform_inbox_entry reads entry["cycle_number"]).
                if cycle_number is not None:
                    for m in to_archive:
                        if isinstance(m, dict) and "cycle_number" not in m:
                            m["cycle_number"] = cycle_number

                # Rewrite inbox.json FIRST (still under the lock). If this
                # fails we abort without touching history, so no duplicates.
                tmp_inbox = inbox_path.with_suffix(inbox_path.suffix + ".tmp")
                try:
                    tmp_inbox.write_text(json.dumps(kept, indent=2))
                    tmp_inbox.rename(inbox_path)
                except Exception as e:
                    tmp_inbox.unlink(missing_ok=True)
                    print(f"  ⚠ inbox archive — failed to rewrite inbox.json: {e}")
                    return -1
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except Exception as e:
        print(f"  ⚠ inbox archive — lock acquisition failed: {e}")
        return -1

    # From here, inbox.json no longer contains the archived items. Append
    # them to history and ingest into memvid (best-effort, non-fatal).
    history = []
    if history_path.exists():
        try:
            loaded = json.loads(history_path.read_text())
        except Exception as e:
            print(f"  ⚠ inbox archive — failed to read inbox_history.json: {e}")
            return -1
        if not isinstance(loaded, list):
            print(
                "  ⚠ inbox archive — inbox_history.json is not a list; aborting to avoid overwriting"
            )
            return -1
        history = loaded

    history.extend(to_archive)
    write_atomic(history_path, history)

    # Buffer the archived items for the single end-of-cycle memvid commit.
    # If no buffer is provided, fall through silently — main() owns the flush.
    if ingest_buffer is not None:
        ingest_buffer.extend(_inbox_chunks_for_memvid(to_archive))

    return len(to_archive)


# ── Argument parsing (no external deps) ─────────────────────────────────────


def parse_args(argv):
    args = argv[1:]
    result = {
        "cycle": None,
        "type": None,
        "category": None,
        "summary": None,
        "goal": None,
        "actions": [],
        "status": "completed",
        "no_normalize": False,
        "no_bg_memvid": False,
        "dry_run": False,
        "help": False,
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            result["help"] = True
        elif a == "--cycle" and i + 1 < len(args):
            i += 1
            try:
                result["cycle"] = int(args[i])
            except ValueError:
                die(f"--cycle must be an integer, got: {args[i]!r}")
        elif a == "--type" and i + 1 < len(args):
            i += 1
            result["type"] = args[i]
        elif a == "--category" and i + 1 < len(args):
            i += 1
            result["category"] = args[i]
        elif a == "--summary" and i + 1 < len(args):
            i += 1
            result["summary"] = args[i]
        elif a == "--goal" and i + 1 < len(args):
            i += 1
            result["goal"] = args[i]
        elif a == "--status" and i + 1 < len(args):
            i += 1
            result["status"] = args[i]
        elif a == "--actions":
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                result["actions"].append(args[i])
                i += 1
            continue
        elif a == "--no-normalize":
            result["no_normalize"] = True
        elif a == "--no-bg-memvid":
            result["no_bg_memvid"] = True
        elif a == "--dry-run":
            result["dry_run"] = True
        i += 1
    return result


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception as e:
        die(f"Failed to read {path}: {e}")


def write_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(path)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        die(f"Failed to write {path}: {e}")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ── Auto-backup ──────────────────────────────────────────────────────────────

# Files included in each cycle-close memory snapshot.
_BACKUP_FILES = [
    "state.json",
    "cycles.json",
    "goal.json",
    "journal.json",
    "server_errors.json",
    "bootstrap.json",
]
_BACKUP_OPTIONAL = []


def _auto_backup_if_stale(dry_run: bool = False) -> None:
    """Create a memory backup if the last one is >1h old.

    Creates a timestamped snapshot dir, copies critical files, prunes if >20
    backups exist. cycle_start.py shows ⚠ STALE when the last backup is >1h
    old; running this at cycle-close keeps that warning quiet.
    """
    backup_root = Path("/agent/backup/memory")
    backup_root.mkdir(parents=True, exist_ok=True)

    # Find the most recent backup by listing dirs (format: YYYYMMDDTHHMMSSZ)
    existing = sorted(
        [d for d in backup_root.iterdir() if d.is_dir() and d.name.endswith("Z")],
        key=lambda d: d.name,
    )
    last_backup_age_secs = None
    if existing:
        latest = existing[-1]
        try:
            latest_dt = datetime.strptime(latest.name, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
            last_backup_age_secs = (
                datetime.now(timezone.utc) - latest_dt
            ).total_seconds()
        except ValueError:
            pass

    # Only backup if >1h old (3600s) or no backup exists
    if last_backup_age_secs is not None and last_backup_age_secs < 3600:
        age_min = int(last_backup_age_secs / 60)
        print(f"  ✓ backup — recent ({age_min}m ago), skipping")
        return

    if dry_run:
        print(
            f"  [dry-run] backup — would create snapshot (last backup "
            f"{int(last_backup_age_secs/60)}m ago)"
            if last_backup_age_secs
            else f"  [dry-run] backup — would create snapshot (no prior backup)"
        )
        return

    # Create timestamped snapshot directory
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap_dir = backup_root / ts
    snap_dir.mkdir()

    copied = 0
    for fname in _BACKUP_FILES + _BACKUP_OPTIONAL:
        src = MEMORY / fname
        if src.exists():
            dst = snap_dir / Path(fname).name
            shutil.copy2(str(src), str(dst))
            copied += 1

    # Prune: keep at most 20 backups
    all_backups = sorted(
        [d for d in backup_root.iterdir() if d.is_dir() and d.name.endswith("Z")],
        key=lambda d: d.name,
    )
    pruned = 0
    while len(all_backups) > 20:
        old = all_backups.pop(0)
        shutil.rmtree(str(old), ignore_errors=True)
        pruned += 1

    age_str = (
        f"{int(last_backup_age_secs/60)}m ago"
        if last_backup_age_secs
        else "first backup"
    )
    prune_str = f", pruned {pruned}" if pruned else ""
    print(f"  ✓ backup — created {ts} ({copied} files{prune_str}; last was {age_str})")


# ── Auto Memory Sync ──────────────────────────────────────────────────────


def _sync_auto_memory() -> None:
    """Sync JSON memory → .md files via memory_sync.py.

    Non-fatal: if sync fails, cycle-close prints a warning but exits 0.
    """
    try:
        from scripts.memory_sync import sync_all

        sync_all()
        print(f"  ✓ auto memory — synced via memory_sync.py")
    except Exception as e:
        print(f"  ⚠ auto memory sync failed (non-fatal): {e}")


# ── Long-term memory (memvid via memory_ingest.py) ───────────────────────────


def _entry_chunks_for_memvid(entry: dict) -> list:
    """Convert a cycle/journal/goal entry into memvid chunks (no I/O).

    Routes through ``memory_ingest._detect_and_transform`` so the chunk
    schema matches the rebuild path exactly. Returns ``[]`` on import
    failure or when the entry produced no ingestible chunk.
    """
    if not entry:
        return []
    try:
        from scripts.memory_ingest import _detect_and_transform
    except Exception as e:
        print(f"  ⚠ memvid — import skipped: {e}")
        return []
    try:
        chunk = _detect_and_transform(entry)
    except Exception as e:
        print(f"  ⚠ memvid — transform failed: {e}")
        return []
    return [chunk] if chunk is not None else []


def _flush_memvid_buffer(chunks: list) -> None:
    """Write all buffered chunks to the .mv2 via a single ``put_many`` call.

    cycle_close batches every record it would ingest (inbox messages +
    journal entry) into a single buffer and flushes them here at the end
    of the cycle. Cycle records are not buffered — cycles.json is excluded
    from long-term memory.

    The SDK auto-checkpoints internally (every ~1000 puts or when the
    WAL reaches 75% capacity), so no manual commit is issued here.
    Segment-catalog rewrites only happen when the SDK decides — typically
    every few hundred cycles rather than every cycle — which keeps the
    .mv2 from growing unboundedly fast.

    On first run the .mv2 doesn't exist; we run a one-shot ``build()`` from
    the source JSON files (journal/journal_archive/inbox_history). Steps 1,
    3, and 5 of ``main()`` have already flushed those files to disk, so
    build() ingests this cycle's records via the source JSON. The buffered
    chunks are therefore **intentionally discarded** on this branch —
    re-ingesting them would create duplicates.
    """
    try:
        from scripts.memory_ingest import DEFAULT_MV2, append_many, build
    except Exception as e:
        print(f"  ⚠ memvid — import skipped: {e}")
        return

    if not DEFAULT_MV2.exists():
        try:
            build(MEMORY, DEFAULT_MV2, quiet=True)
            print(
                f"  ✓ memvid — built new {DEFAULT_MV2.name} "
                f"(this cycle's records included via source JSON)"
            )
            return
        except SystemExit as e:
            print(f"  ⚠ memvid — build failed (exit {e.code}); skipping ingest")
            return
        except Exception as e:
            print(f"  ⚠ memvid — build failed: {e}; skipping ingest")
            return

    if not chunks:
        return

    try:
        ok, fail = append_many(DEFAULT_MV2, chunks, quiet=True)
        msg = f"  ✓ memvid — batched {ok} chunk(s) (auto-checkpoint)"
        if fail:
            msg += f" ({fail} failed)"
        print(msg)
    except SystemExit as e:
        print(f"  ⚠ memvid flush — exit {e.code}")
    except Exception as e:
        print(f"  ⚠ memvid flush — failed: {e}")


# ── Background flush dispatcher ──────────────────────────────────────────────


def _sweep_stale_memvid_buffers() -> None:
    """Delete leftover .memvid_buffer_* temp files older than 1h.

    Defensive cleanup in case a prior background child died before unlinking
    its buffer.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    try:
        for p in MEMORY.glob(".memvid_buffer_*.json"):
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def _dispatch_memvid_flush_bg(chunks: list, cycle_n: int) -> None:
    """Spawn a detached subprocess to run ``_flush_memvid_buffer`` and return.

    Cycle-close has no remaining steps after the memvid flush, so blocking on
    its 5–10s commit just delays the user-facing "done" message. We stage the
    chunks to a JSON temp file and launch a detached child process to run the
    actual flush in the background. Failures fall back to an inline flush.
    """
    try:
        from scripts.memory_ingest import DEFAULT_MV2
    except Exception as e:
        print(f"  ⚠ memvid bg — import failed ({e}); flushing inline")
        _flush_memvid_buffer(chunks)
        return

    if not chunks and DEFAULT_MV2.exists():
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    buf_path = MEMORY / f".memvid_buffer_{cycle_n}_{ts}.json"
    log_path = MEMORY / ".memvid_flush.log"

    try:
        buf_path.write_text(json.dumps(chunks))
    except Exception as e:
        print(f"  ⚠ memvid bg — failed to stage buffer ({e}); flushing inline")
        _flush_memvid_buffer(chunks)
        return

    try:
        with open(log_path, "ab") as log_f:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--__flush-memvid",
                    str(buf_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log_f,
                stderr=log_f,
                start_new_session=True,
                close_fds=True,
            )
        print(
            f"  ✓ memvid — flush dispatched in background "
            f"(pid={proc.pid}, durable in ~5–10s, log={log_path})"
        )
    except Exception as e:
        print(f"  ⚠ memvid bg — spawn failed ({e}); flushing inline")
        buf_path.unlink(missing_ok=True)
        _flush_memvid_buffer(chunks)


def _flush_memvid_child(buf_path: Path) -> int:
    """Background-mode entry point: load chunks, flush memvid under a lock.

    Holds an exclusive ``flock`` on ``<mv2>.flush.lock`` for the duration of
    the flush so a back-to-back cycle-close can't have two children writing
    the same .mv2 concurrently. The lock waits rather than fails — cycles are
    sequential in normal operation, so contention is rare and serialization
    is the correct behavior.
    """
    try:
        from scripts.memory_ingest import DEFAULT_MV2
    except Exception as e:
        print(f"⚠ bg flush — import failed: {e}", flush=True)
        return 1

    started = datetime.now(timezone.utc).isoformat()
    print(f"[{started}] memvid bg flush starting (buf={buf_path.name})", flush=True)

    # Validate buf_path: must live under MEMORY and match the staging pattern.
    # The flag is internal, but defending against a stray invocation prevents
    # the finally-block unlink from touching arbitrary files.
    try:
        resolved = buf_path.resolve()
        if (
            resolved.parent != MEMORY.resolve()
            or not resolved.name.startswith(".memvid_buffer_")
            or not resolved.name.endswith(".json")
        ):
            print(
                f"[{started}] ⚠ bg flush — refusing buf path outside MEMORY: {resolved}",
                flush=True,
            )
            return 1
    except Exception as e:
        print(f"[{started}] ⚠ bg flush — buf path validation failed: {e}", flush=True)
        return 1

    try:
        chunks = json.loads(buf_path.read_text()) if buf_path.exists() else []
    except Exception as e:
        print(f"[{started}] ⚠ bg flush — failed to read buffer: {e}", flush=True)
        chunks = []

    lock_path = str(DEFAULT_MV2) + ".flush.lock"
    rc = 0
    try:
        with open(lock_path, "a+") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                _flush_memvid_buffer(chunks)
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except Exception as e:
        print(f"[{started}] ⚠ bg flush — failed: {e}", flush=True)
        rc = 1
    finally:
        try:
            buf_path.unlink(missing_ok=True)
        except Exception:
            pass
        ended = datetime.now(timezone.utc).isoformat()
        print(f"[{ended}] memvid bg flush done (rc={rc})", flush=True)
    return rc


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    # Internal re-entry: background memvid flush spawned by
    # _dispatch_memvid_flush_bg. Runs only the flush, then exits.
    if len(sys.argv) >= 3 and sys.argv[1] == "--__flush-memvid":
        sys.exit(_flush_memvid_child(Path(sys.argv[2])))

    _sweep_stale_memvid_buffers()

    opts = parse_args(sys.argv)

    if opts["help"]:
        print(__doc__)
        sys.exit(0)

    # Validate required args
    if not opts["type"]:
        die("--type TYPE is required (evolve | goal | self-heal | dream)")
    if not opts["summary"]:
        die("--summary TEXT is required")
    if opts["type"] in ("evolve", "dream") and not opts["category"]:
        die(f"--category CAT is required when --type is {opts['type']}")

    valid_types = {"evolve", "goal", "self-heal", "dream"}
    if opts["type"] not in valid_types:
        die(f"--type must be one of: {', '.join(sorted(valid_types))}")

    evolve_cats = {
        "reliability",
        "observability",
        "capability",
        "efficiency",
        "prompt_evolution",
    }
    dream_cats = {"memory_consolidation", "deep_sleep"}
    valid_cats = evolve_cats | dream_cats
    if opts["category"]:
        if opts["category"] not in valid_cats:
            die(f"--category must be one of: {', '.join(sorted(valid_cats))}")
        if opts["type"] == "evolve" and opts["category"] not in evolve_cats:
            die(
                f"--category {opts['category']} is dream-only; evolve cycles must use one of: "
                f"{', '.join(sorted(evolve_cats))}"
            )
        if opts["type"] == "dream" and opts["category"] not in dream_cats:
            die(
                f"--category {opts['category']} is evolve-only; dream cycles must use one of: "
                f"{', '.join(sorted(dream_cats))}"
            )

    valid_statuses = {"completed", "failed"}
    if opts["status"] not in valid_statuses:
        die(f"--status must be one of: {', '.join(sorted(valid_statuses))}")

    now = now_iso()
    # Resolution order: explicit --goal > cycle_entry.cycle_goal (set by
    # cycle_start.py at the start of the cycle) > legacy cycle_entry.goal
    # (in-progress entries written before the cycle_goal rename). Never fall
    # back to --summary — goal (planned) and summary (delivered) are
    # different concepts. We deliberately do NOT read state.current_goal:
    # the agent may rewrite it mid-cycle as it picks up sub-tasks, but the
    # journal entry should record the original cycle goal, not the last
    # in-flight task.
    goal_text = opts["goal"]

    # ── Load existing data ───────────────────────────────────────────────────
    cycles_path = MEMORY / "cycles.json"
    state_path = MEMORY / "state.json"
    journal_path = MEMORY / "journal.json"

    cycles = load_json(cycles_path)
    state = load_json(state_path)
    journal = load_json(journal_path)

    if not isinstance(cycles, list):
        die("cycles.json is not a list")
    if not isinstance(state, dict):
        die("state.json is not a dict")
    if not isinstance(journal, list):
        die("journal.json is not a list")

    # Migrate legacy field names in memory before any reads so the rest of
    # this function only sees the current schema. Disk gets rewritten when
    # we save updates below.
    from scripts.memory_repair import (
        migrate_cycles_list,
        migrate_journal_list,
        migrate_state_dict,
    )

    migrate_state_dict(state)
    migrate_cycles_list(cycles)
    migrate_journal_list(journal)

    # ── Auto-detect cycle number if not provided ─────────────────────────────
    if opts["cycle"] is None:
        # Prefer the most recent in_progress entry — cycle_start.py always writes one.
        # This is immune to state.cycle_number being pre-updated by the agent.
        ip_entries = [
            c
            for c in cycles
            if c.get("cycle_status") == "in_progress" and c.get("cycle_number")
        ]
        if ip_entries:
            detected = max(c["cycle_number"] for c in ip_entries)
            print(
                f"  ℹ  --cycle not specified — auto-detected from in_progress entry: {detected}"
            )
        else:
            # Fallback: state.cycle_number + 1 (no in_progress entry means cycle_start.py didn't run)
            state_cycle = state.get("cycle_number")
            if state_cycle is not None and isinstance(state_cycle, int):
                detected = state_cycle + 1
            elif cycles:
                detected = max(c.get("cycle_number", 0) for c in cycles) + 1
            else:
                detected = 1
            print(
                f"  ℹ  --cycle not specified — auto-detected from state: {detected} "
                f"(no in_progress entry found)"
            )
        opts["cycle"] = detected

    cycle_n = opts["cycle"]

    # ── Find this cycle's entry and compute duration ─────────────────────────
    cycle_entry = None
    for c in cycles:
        if c.get("cycle_number") == cycle_n:
            cycle_entry = c
            break

    if cycle_entry is None:
        # Fallback: create stub if cycle_start.py didn't insert the start record.
        # Uses state.last_cycle_run as approximate start time. Falls back to
        # `now` only if state.json is missing or corrupt (gives duration=0 in that case).
        stub_start = state.get("last_cycle_run") or state.get("last_heartbeat") or now
        cycle_entry = {
            "cycle_number": cycle_n,
            "start": stub_start,
            "cycle_type": opts["type"],
            "cycle_status": "in_progress",
        }
        cycles.append(cycle_entry)
        print(
            f"  ⚠  No existing entry for cycle {cycle_n} — created stub (start={stub_start[:19]})"
        )

    # Pull goal from the in-progress cycle entry (written by cycle_start.py
    # under the `cycle_goal` field) when --goal wasn't passed at close time.
    if not goal_text:
        goal_text = cycle_entry.get("cycle_goal")

    start_ts = cycle_entry.get("start")
    duration = None
    if start_ts:
        try:
            start_dt = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
            duration = round((datetime.now(timezone.utc) - start_dt).total_seconds())
        except Exception:
            pass

    # ── Build updated values ─────────────────────────────────────────────────
    # `summary` is intentionally omitted — it lives on the journal entry and
    # mirroring it onto cycles.json was redundant.
    cycle_update = {
        "end": now,
        "cycle_status": opts["status"],
        "cycle_type": opts["type"],
    }
    if opts["category"]:
        cycle_update["cycle_category"] = opts["category"]
    if duration is not None:
        cycle_update["duration_seconds"] = duration

    # state.current_goal is the dynamic in-flight sub-task (the agent
    # rewrites it mid-cycle as it picks up tasks). Cycle is now idle, so
    # clear it to None — the planned cycle goal already lives on
    # cycles.json:cycle_goal and on the journal entry. The next cycle's
    # agent will repopulate current_goal as soon as it picks up a sub-task.
    state_update = {
        "cycle_number": cycle_n,
        "agent_status": "idle",
        "current_goal": None,
        "last_cycle_summary": opts["summary"],
    }

    journal_entry = {
        "cycle_number": cycle_n,
        "timestamp": now,
        "cycle_status": opts["status"],
        "cycle_type": opts["type"],
        "actions": opts["actions"],
        "summary": opts["summary"],
    }
    # Record the planned goal on the journal entry. Resolution already
    # preferred --goal over cycle_entry.cycle_goal above; we just persist it
    # if anything was found. --goal at close acts as an explicit override of
    # whatever cycle_start.py recorded.
    if goal_text:
        journal_entry["cycle_goal"] = goal_text
    if opts["category"]:
        journal_entry["cycle_category"] = opts["category"]

    # ── Print plan ───────────────────────────────────────────────────────────
    print(
        f"\n[cycle-close] Cycle {cycle_n} — {opts['type']}"
        + (f" / {opts['category']}" if opts["category"] else "")
        + f" — {opts['status']}"
    )
    if duration is not None:
        m, s = divmod(duration, 60)
        print(f"  Duration:  {m}m {s}s ({duration}s)")
    print(f"  Summary:   {opts['summary'][:90]}")
    if opts["actions"]:
        for a in opts["actions"]:
            print(f"  Action:    {a}")
    print(f"  Dry-run:   {opts['dry_run']}")

    if opts["dry_run"]:
        print("\n[DRY RUN] Would write:")
        print(f"  cycles.json      — update entry for cycle {cycle_n}")
        print(f"  state.json       — cycle_number={cycle_n}, status=idle")
        print(f"  journal.json     — append 1 entry")
        sys.exit(0)

    # ── Apply updates ────────────────────────────────────────────────────────
    # All long-term-memory writes for this cycle are buffered into one list
    # and flushed in a single open + many puts + ONE commit at the end. Per-
    # call commits on memvid rewrite the segment catalog and reserve
    # significant on-disk space — batching keeps file growth bounded.
    memvid_buffer: list = []

    # 1. Update cycles.json
    cycle_entry.update(cycle_update)
    write_atomic(cycles_path, cycles)
    print(f"\n  ✓ cycles.json updated (cycle {cycle_n})")

    # 2. Update state.json
    state.update(state_update)
    write_atomic(state_path, state)
    print(f"  ✓ state.json updated (cycle_number={cycle_n})")

    # 3. Append journal entry
    already_in_journal = any(e.get("cycle_number") == cycle_n for e in journal)
    if already_in_journal:
        print(
            f"  ⚠ journal.json — entry for cycle {cycle_n} already exists, skipping duplicate write"
        )
    else:
        journal.append(journal_entry)
        write_atomic(journal_path, journal)
        print(f"  ✓ journal.json — appended entry (total: {len(journal)})")

    # 4. Normalize cycles (optional) — inlined to avoid subprocess overhead
    if not opts["no_normalize"]:
        try:
            changes = _run_normalize_inlined(cycles_path)
            if changes > 0:
                print(f"  ✓ normalize — {changes} legacy field(s) fixed")
            else:
                print(f"  ✓ normalize — no changes needed")
        except Exception as e:
            print(f"  ⚠ normalize failed (non-fatal): {e}")

    # 5. Archive inbox.json → inbox_history.json (goal cycles only;
    #    evolve/self-heal/dream cycles must not touch inbox so pending user commands survive)
    if opts["type"] == "goal":
        archived_n = _archive_inbox(
            cycle_start_ts=cycle_entry.get("start"),
            ingest_buffer=memvid_buffer,
            cycle_number=cycle_n,
        )
        if archived_n > 0:
            print(
                f"  ✓ inbox archive — {archived_n} pre-cycle item(s) appended to inbox_history.json; mid-cycle arrivals carried forward"
            )
        elif archived_n < 0:
            print(
                "  ⚠ inbox archive — FAILED; inbox left intact for next cycle to retry"
            )

    # 6. Auto-backup memory files if last backup >1h old (eliminates manual step 6)
    _auto_backup_if_stale(dry_run=False)

    # 7. Sync auto memory (markdown files for agent native memory)
    _sync_auto_memory()

    # 8. Buffer the journal entry, then flush every memvid write for this
    #    cycle in a single open + ONE commit (inbox messages + journal entry).
    #    cycle records are no longer ingested — cycles.json is excluded from
    #    long-term memory.
    memvid_buffer.extend(_entry_chunks_for_memvid(journal_entry))
    if opts["no_bg_memvid"]:
        _flush_memvid_buffer(memvid_buffer)
    else:
        _dispatch_memvid_flush_bg(memvid_buffer, cycle_n)

    print(f"\n[cycle-close] Done. Cycle {cycle_n} closed.\n")


if __name__ == "__main__":
    main()
