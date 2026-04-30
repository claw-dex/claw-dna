#!/usr/bin/env python3
"""
cycle_close.py — One-command cycle close automation.

Automates the repetitive boilerplate from cycle-close.md:
  1. Marks the in-progress cycles.json entry as completed (computes duration)
  2. Updates state.json (cycle_number, status, last_cycle_summary, last_cycle_type)
  3. Appends a journal entry to journal.json
  4. Normalizes cycles.json schema (inlined — no subprocess)
  5. Archives inbox.json items to inbox_history.json, then clears inbox.json
  6. Checks for stale tab/test/script counts and warns when drift is found
  7. Auto-backs up memory files if last backup >1h old (inlined — no subprocess)
  8. Reports what was written

Usage:
    uv run python scripts/cycle_close.py \\
        --type evolve \\
        --category efficiency \\
        --summary "Built cycle_close.py to automate end-of-cycle boilerplate" \\
        --actions "Built scripts/cycle_close.py" "Updated AGENTS.md" "Tested portal health" \\
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
    --goal TEXT           What you set out to do (defaults to the goal recorded by
                          cycle_start.py on the in-progress cycle entry; omitted
                          from the journal entry if neither is set)
    --no-normalize        Skip cycles.json normalization after writing
    --dry-run             Print what would be written, but write nothing

Exit codes: 0 = success, 1 = error (missing required args, write failure)

Added in cycle 24 (efficiency): replaces manual Python one-liners at end of every cycle.
Enhanced in cycle 66 (efficiency): fixed python3→uv run python.
Enhanced in cycle 79 (efficiency): stub start uses state.last_heartbeat for accurate durations.
Enhanced in cycle 86 (efficiency): --cycle is now optional (auto-detected from state.json).
Enhanced in cycle 114 (prompt_evolution): auto stale-count check runs every cycle — warns when
    tab count, test count, or script count in AGENTS.md/prompts diverges from actual values.
Enhanced in cycle 117 (efficiency): test count cached by self_test.py mtime — avoids 1.4s
    subprocess on cycles where self_test.py hasn't changed (typical case).
Enhanced in cycle 119 (efficiency): inlined normalize_cycles and outbox-history logic —
    eliminates 2 `uv run python` subprocesses per cycle (~150ms overhead removed).
Enhanced in cycle 167 (efficiency): auto-backup memory files if last backup >1h old —
    eliminates the recurring ⚠ STALE BACKUP warning in cycle_start.py.
"""

import fcntl
import json
import glob as glob_mod
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

MEMORY = Path("/agent/memory")
SCRIPTS = Path("/agent/scripts")


# ── Inlined: normalize_cycles logic ─────────────────────────────────────────


def _normalize_cycle_entry(entry: dict) -> tuple:
    """Normalize a single cycles.json entry. Returns (normalized_entry, changes_count).

    Handles legacy fields from early cycles:
      - "timestamp" → "start"
      - "goal" → "summary"
      - computes duration_seconds when start+end present but duration missing
      - adds default status/type if absent
    """
    c = dict(entry)
    n = 0

    if "timestamp" in c and "start" not in c:
        c["start"] = c.pop("timestamp")
        n += 1
    elif "timestamp" in c:
        del c["timestamp"]
        n += 1

    if "goal" in c and "summary" not in c:
        c["summary"] = c.pop("goal")
        n += 1
    elif "goal" in c and "summary" in c:
        del c["goal"]
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

    if "status" not in c:
        c["status"] = "completed"
        n += 1
    if "type" not in c:
        c["type"] = "evolve"
        n += 1
    if "cycle" in c and not isinstance(c["cycle"], int):
        try:
            c["cycle"] = int(c["cycle"])
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
        from scripts.memory_ingest import chunk_inbox_entry
    except Exception as e:
        print(f"  ⚠ inbox memvid — import skipped: {e}")
        return []
    out = []
    for msg in items:
        c = chunk_inbox_entry(msg)
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


def _archive_inbox(cycle_start_ts=None, ingest_buffer: list = None):
    """Archive pre-cycle items in /agent/messages/inbox.json to inbox_history.json.

    Items whose ``received_at`` is on or before ``cycle_start_ts`` are archived
    and ingested into long-term memory (best-effort). Items that arrived
    mid-cycle (after ``cycle_start_ts``) are left in inbox.json so the next
    cycle can process them.

    All inbox read/partition/rewrite happens under an exclusive lock on
    ``inbox.json.lock`` (the same lock used by ``services.shared.write_to_inbox``
    and ``app.shared.AtomicJSON``), so concurrent appenders cannot have their
    messages dropped or double-archived. inbox.json is rewritten *before*
    inbox_history.json is updated, so a failure during rewrite cannot leave
    items duplicated across both files.

    Returns the number of items archived, or -1 on failure. Returns 0 when
    inbox is missing or has no archivable items.
    """
    inbox_path = Path("/agent/messages/inbox.json")
    history_path = Path("/agent/messages/inbox_history.json")
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


# ── Stale-count check ────────────────────────────────────────────────────────


def check_stale_counts():
    """Auto-detect mismatched counts in AGENTS.md and prompts.

    Recurring failure mode: cycle adds a tab/script but AGENTS.md and
    server.md still show the old number. This check runs every cycle so
    drift is caught immediately rather than lingering until the next
    prompt_evolution cycle.

    Prints a warning with exact fix commands only when a mismatch is found.
    Silent (no output) when everything matches — avoids noise in normal runs.
    """
    issues = []

    # ── 1. Tab count ─────────────────────────────────────────────────────────
    server_py = Path("/agent/server.py")
    if server_py.exists():
        src = server_py.read_text()
        idx = src.find("TAB_REGISTRY = [")
        if idx != -1:
            body = src[idx:]
            actual_tabs = body[: body.find("]")].count("(")
        else:
            actual_tabs = None

        if actual_tabs is not None:
            # Check prompts/server.md for tab count references
            server_md = Path("/agent/prompts/server.md")
            if server_md.exists():
                md_text = server_md.read_text()
                matches = re.findall(r"(\d+)\s+tab", md_text)
                for m in matches:
                    if int(m) != actual_tabs:
                        issues.append(
                            f"  ⚠ Tab count mismatch: server.md says '{m} tab*' but "
                            f"TAB_REGISTRY has {actual_tabs} tabs"
                        )
                        break

            # Check AGENTS.md for tab count
            agents_md = Path("/agent/AGENTS.md")
            if agents_md.exists():
                md_text = agents_md.read_text()
                matches = re.findall(r"(\d+)\s+tabs?\s+total", md_text)
                for m in matches:
                    if int(m) != actual_tabs:
                        issues.append(
                            f"  ⚠ Tab count mismatch: AGENTS.md says '{m} tabs total' but "
                            f"TAB_REGISTRY has {actual_tabs} tabs"
                        )
                        break

    # ── 2. Self-test count ───────────────────────────────────────────────────
    self_test = Path("/agent/scripts/self_test.py")
    error_triage = Path("/agent/prompts/error-triage.md")
    self_heal_md = Path("/agent/prompts/self-heal.md")

    if self_test.exists():
        # Get test count using mtime-based cache to avoid 1.4s subprocess on every cycle.
        # Cache file: /agent/memory/self_test_count.json → {mtime: float, count: int}
        # Only re-runs self_test.py when the file has actually changed.
        _count_cache = MEMORY / "self_test_count.json"
        actual_tests = None
        try:
            current_mtime = self_test.stat().st_mtime
            # Check cache
            cached = None
            if _count_cache.exists():
                try:
                    cached = json.loads(_count_cache.read_text())
                except Exception:
                    cached = None
            if cached and abs(cached.get("mtime", 0) - current_mtime) < 0.001:
                # Cache hit — self_test.py unchanged, use stored count
                actual_tests = cached.get("count")
            else:
                # Cache miss — run self_test and cache result
                try:
                    import importlib.util

                    spec = importlib.util.spec_from_file_location(
                        "self_test", str(self_test)
                    )
                    self_test_mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(self_test_mod)
                    self_test_mod.run_all()
                    actual_tests = len(self_test_mod.results)
                    if actual_tests:
                        _count_cache.write_text(
                            json.dumps({"mtime": current_mtime, "count": actual_tests})
                        )
                except Exception:
                    pass
        except Exception:
            actual_tests = None

        if actual_tests is not None:
            for prompt_path in [error_triage, self_heal_md]:
                if prompt_path.exists():
                    prompt_text = prompt_path.read_text()
                    matches = re.findall(r"(\d+)[\s-]test", prompt_text)
                    for pm in matches:
                        if int(pm) != actual_tests:
                            issues.append(
                                f"  ⚠ Test count mismatch: {prompt_path.name} says '{pm} test*' but "
                                f"self_test.py reports {actual_tests} tests — update the prompt"
                            )
                            break  # one warning per file is enough

    # ── 3. Script count ──────────────────────────────────────────────────────
    scripts_dir = Path("/agent/scripts")
    if scripts_dir.exists():
        actual_scripts = len(
            glob_mod.glob(str(scripts_dir / "*.py"))
            + glob_mod.glob(str(scripts_dir / "*.sh"))
        )
        agents_md = Path("/agent/AGENTS.md")
        if agents_md.exists():
            md_text = agents_md.read_text()
            # Look for "N scripts" patterns in capabilities section
            matches = re.findall(r"(\d+)\s+scripts?\b", md_text)
            for m in matches:
                if int(m) != actual_scripts and abs(int(m) - actual_scripts) > 1:
                    issues.append(
                        f"  ⚠ Script count: AGENTS.md references '{m} scripts' but "
                        f"/agent/scripts/ has {actual_scripts} files — run sync-capabilities.py"
                    )
                    break

    # ── Output ───────────────────────────────────────────────────────────────
    if issues:
        print("\n[STALE COUNTS DETECTED] Fix before closing:")
        for issue in issues:
            print(issue)
        print("  → Update AGENTS.md and prompts/ to match actual counts (takes ~30s)")
    # Silent when all counts match


# ── Auto-backup ──────────────────────────────────────────────────────────────

# Files to backup (mirrors BACKUP_FILES + OPTIONAL_FILES in memory_backup.py)
_BACKUP_FILES = [
    "state.json",
    "cycles.json",
    "goal.json",
    "journal.json",
    "server_errors.json",
    "command_history.json",
    "bootstrap.json",
]
_BACKUP_OPTIONAL = []


def _auto_backup_if_stale(dry_run: bool = False) -> None:
    """Create a memory backup if the last one is >1h old.

    Inlined to avoid subprocess overhead (~100ms for uv run python memory_backup.py).
    Mirrors the core logic of memory_backup.py: create a timestamped snapshot dir,
    copy critical files, prune if >20 backups exist.

    cycle_start.py shows ⚠ STALE when the last backup is >1h old. Running this
    at cycle-close time keeps that warning quiet and protects against data loss.
    """
    backup_root = MEMORY / "backups"
    backup_root.mkdir(exist_ok=True)

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

    Routes through ``memory_ingest._detect_and_chunk`` so the chunk schema
    matches the rebuild path exactly. Returns ``[]`` on import failure.
    """
    if not entry:
        return []
    try:
        from scripts.memory_ingest import _detect_and_chunk
    except Exception as e:
        print(f"  ⚠ memvid — import skipped: {e}")
        return []
    try:
        return list(_detect_and_chunk(entry) or [])
    except Exception as e:
        print(f"  ⚠ memvid — chunking failed: {e}")
        return []


def _flush_memvid_buffer(chunks: list) -> None:
    """Write all buffered chunks to the .mv2 in a single open + ONE commit.

    Per-call commits on the memvid `.mv2` rewrite the segment catalog and
    reserve significant on-disk space, so cycle_close batches every record
    it would ingest (inbox messages + cycle record + journal entry) into a
    single buffer and flushes them here at the end of the cycle.

    On first run the .mv2 doesn't exist; we run a one-shot ``build()`` from
    the source JSON files (journal/cycles/inbox_history). Steps 1, 3, and 5
    of ``main()`` have already flushed those files to disk, so build()
    ingests this cycle's records via the source JSON. The buffered chunks
    are therefore **intentionally discarded** on this branch — re-ingesting
    them would create duplicates.
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
        msg = f"  ✓ memvid — batched {ok} chunk(s) in 1 commit"
        if fail:
            msg += f" ({fail} failed)"
        print(msg)
    except SystemExit as e:
        print(f"  ⚠ memvid flush — exit {e.code}")
    except Exception as e:
        print(f"  ⚠ memvid flush — failed: {e}")


# ── Main ────────────────────────────────────────────────────────────────────


def main():
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
    # Goal resolution order: explicit --goal > goal recorded by cycle_start.py
    # on the in-progress cycle entry. Never fall back to --summary — goal
    # (planned) and summary (delivered) are different concepts and silently
    # aliasing them produced journal entries where both fields were identical.
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

    # ── Auto-detect cycle number if not provided ─────────────────────────────
    if opts["cycle"] is None:
        # Prefer the most recent in_progress entry — cycle_start.py always writes one.
        # This is immune to state.cycle_number being pre-updated by the agent.
        ip_entries = [
            c for c in cycles if c.get("status") == "in_progress" and c.get("cycle")
        ]
        if ip_entries:
            detected = max(c["cycle"] for c in ip_entries)
            print(
                f"  ℹ  --cycle not specified — auto-detected from in_progress entry: {detected}"
            )
        else:
            # Fallback: state.cycle_number + 1 (no in_progress entry means cycle_start.py didn't run)
            state_cycle = state.get("cycle_number")
            if state_cycle is not None and isinstance(state_cycle, int):
                detected = state_cycle + 1
            elif cycles:
                detected = max(c.get("cycle", 0) for c in cycles) + 1
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
        if c.get("cycle") == cycle_n:
            cycle_entry = c
            break

    if cycle_entry is None:
        # Fallback: create stub if cycle_start.py didn't insert the start record.
        # Uses state.last_cycle_run as approximate start time. Falls back to
        # `now` only if state.json is missing or corrupt (gives duration=0 in that case).
        stub_start = state.get("last_cycle_run") or state.get("last_heartbeat") or now
        cycle_entry = {
            "cycle": cycle_n,
            "start": stub_start,
            "type": opts["type"],
            "status": "in_progress",
        }
        cycles.append(cycle_entry)
        print(
            f"  ⚠  No existing entry for cycle {cycle_n} — created stub (start={stub_start[:19]})"
        )

    # Pull goal from the in-progress cycle entry (written by cycle_start.py)
    # when --goal wasn't passed at close time.
    if not goal_text:
        goal_text = cycle_entry.get("goal")

    start_ts = cycle_entry.get("start")
    duration = None
    if start_ts:
        try:
            start_dt = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
            duration = round((datetime.now(timezone.utc) - start_dt).total_seconds())
        except Exception:
            pass

    # ── Build updated values ─────────────────────────────────────────────────
    cycle_update = {
        "end": now,
        "status": opts["status"],
        "summary": opts["summary"],
        "type": opts["type"],
    }
    if opts["category"]:
        cycle_update["category"] = opts["category"]
    if duration is not None:
        cycle_update["duration_seconds"] = duration

    state_update = {
        "cycle_number": cycle_n,
        "status": "idle",
        "current_goal": goal_text if goal_text else None,
        "last_cycle_summary": opts["summary"],
        "last_cycle_type": opts["type"],
        "last_cycle_end": now,
    }
    if opts["category"]:
        state_update["last_cycle_category"] = opts["category"]

    journal_entry = {
        "cycle": cycle_n,
        "timestamp": now,
        "status": opts["status"],
        "type": opts["type"],
        "actions": opts["actions"],
        "summary": opts["summary"],
    }
    if goal_text:
        journal_entry["goal"] = goal_text
    if opts["category"]:
        journal_entry["category"] = opts["category"]

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

    # 1a. Buffer the finalized cycle record for the end-of-cycle memvid flush.
    memvid_buffer.extend(_entry_chunks_for_memvid(cycle_entry))

    # 2. Update state.json
    state.update(state_update)
    write_atomic(state_path, state)
    print(f"  ✓ state.json updated (cycle_number={cycle_n})")

    # 3. Append journal entry
    already_in_journal = any(e.get("cycle") == cycle_n for e in journal)
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
        )
        if archived_n > 0:
            print(
                f"  ✓ inbox archive — {archived_n} pre-cycle item(s) appended to inbox_history.json; mid-cycle arrivals carried forward"
            )
        elif archived_n < 0:
            print(
                "  ⚠ inbox archive — FAILED; inbox left intact for next cycle to retry"
            )

    # 6. Stale-count check (always — catches drift from any cycle type)
    check_stale_counts()

    # 7. Auto-backup memory files if last backup >1h old (eliminates manual step 6)
    _auto_backup_if_stale(dry_run=False)

    # 8. Sync auto memory (markdown files for agent native memory)
    _sync_auto_memory()

    # 9. Buffer the journal entry, then flush every memvid write for this
    #    cycle in a single open + ONE commit (inbox messages + cycle record
    #    + journal entry).
    memvid_buffer.extend(_entry_chunks_for_memvid(journal_entry))
    _flush_memvid_buffer(memvid_buffer)

    print(f"\n[cycle-close] Done. Cycle {cycle_n} closed.\n")


if __name__ == "__main__":
    main()
