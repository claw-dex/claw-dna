#!/usr/bin/env python3
"""
metrics_db.py — Built-in metrics collector backed by a local DuckDB file.

Reads every JSON source the portal used to aggregate at render time, computes all
Overview metrics once, and stores both the raw rows and the pre-computed metric
tables in /agent/memory/metrics.duckdb.

The portal (app/data/metrics.py) only ever runs `SELECT * FROM metric_*` against
this file — no metric is ever computed during a Streamlit render.

Concurrency
-----------
DuckDB allows one read-write process OR many read-only processes per file, so the
build never writes the live file in place. It builds into `<db>.tmp` and then
os.replace()s it onto the real path. Readers holding an open read-only handle keep
their inode alive across the swap and see a consistent snapshot.

Usage:
    python3 metrics_db.py                 # refresh if sources changed
    python3 metrics_db.py --refresh       # same as above
    python3 metrics_db.py --rebuild       # force a rebuild
    python3 metrics_db.py --stats         # print row counts per table
    python3 metrics_db.py --json          # print stats as JSON
    python3 metrics_db.py --db PATH       # override the database path
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import duckdb
except ImportError:  # pragma: no cover - duckdb ships with the container runtime
    duckdb = None

# ─── Paths ────────────────────────────────────────────────────────────────────

AGENT_DIR = Path(os.environ.get("AGENT_DIR", "/agent"))
MEMORY_DIR = AGENT_DIR / "memory"
MESSAGES_DIR = AGENT_DIR / "messages"
WORKSPACE_DIR = AGENT_DIR / "workspace"
DB_PATH = Path(os.environ.get("METRICS_DB_PATH", str(MEMORY_DIR / "metrics.duckdb")))

# Bump whenever a table is added/changed: is_ready() treats an older version as
# not-ready, so the portal rebuilds instead of querying a missing table.
SCHEMA_VERSION = 3

# Evolution categories, in display order. Mirrors app/data/cycle.py:load_balance.
ALL_CATEGORIES = [
    "reliability",
    "observability",
    "capability",
    "efficiency",
    "prompt_evolution",
]

# Bounded materialisations — the Overview tab never shows more than these.
VELOCITY_LIMIT = 20
GOAL_DURATION_LIMIT = 20
IMPROVEMENTS_LIMIT = 50
TIMELINE_DAYS = 7
RECENT_GOALS_LIMIT = 5

# The directory-walk inputs (workspace size, LanceDB store size) are the only
# expensive ones; reuse the previous measurement between builds.
WORKSPACE_WALK_INTERVAL_MINUTES = 15
LTM_STORE_NAME = "long_term_memory.lancedb"

# Memory-file health thresholds. Mirrors app/system_tab.py — the collector owns
# the classification, the tab owns the labels, icons, and colours.
MEMORY_SIZE_WARN_KB = 500
MEMORY_SIZE_CRIT_KB = 2000
MEMORY_AGE_WARN_HOURS = 24
MEMORY_AGE_CRIT_HOURS = 72
# Intentionally infrequently updated — skip the age checks for these.
MEMORY_AGE_EXEMPT = {"bootstrap.json", "link_cache.json"}

# Errors within this window count as "recent" on an agent's Health tab.
AGENT_ERROR_RECENT_HOURS = 24

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def db_path() -> Path:
    """Current database path (module global so tests can monkeypatch it)."""
    return Path(DB_PATH)


def _sources() -> dict:
    """Map of logical name → source file path. Read at call time so tests can patch."""
    return {
        "cycles": MEMORY_DIR / "cycles.json",
        "cycles_archive": MEMORY_DIR / "cycles_archive.json",
        "journal": MEMORY_DIR / "journal.json",
        "journal_archive": MEMORY_DIR / "journal_archive.json",
        "goals": MEMORY_DIR / "goal.json",
        "state": MEMORY_DIR / "state.json",
        "weights": MEMORY_DIR / "evolution_weights.json",
        "errors": MEMORY_DIR / "server_errors.json",
        "sys_metrics": MEMORY_DIR / "metrics.json",
        "agents": MEMORY_DIR / "agents.json",
        "inbox": MESSAGES_DIR / "inbox.json",
        "inbox_history": MESSAGES_DIR / "inbox_history.json",
        "outbox_history": MESSAGES_DIR / "outbox_history.json",
    }


def _memory_json_files() -> list:
    """Every non-backup *.json in the memory dir, sorted — as (name, size, mtime).

    Backs both the memory-file health table and the fingerprint: those metrics
    depend on files that are not individually listed in _sources().
    """
    out = []
    try:
        names = sorted(
            f
            for f in os.listdir(MEMORY_DIR)
            if f.endswith(".json") and not f.endswith(".backup")
        )
    except OSError:
        return out
    for name in names:
        try:
            st = os.stat(MEMORY_DIR / name)
        except OSError:
            continue
        out.append((name, st.st_size, st.st_mtime))
    return out


# ─── Small helpers ────────────────────────────────────────────────────────────


def _read_json(path, default):
    """Read a JSON file, returning `default` when missing or unparseable."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
        return default


def _as_list(value):
    return value if isinstance(value, list) else []


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _jdump(value) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return "[]"


def _num(value):
    """Coerce to float, or None when the value is not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _day_of(ts) -> str:
    """Day bucket for a cycle. Matches the `str(start)[:10]` slice the tab used."""
    return str(ts or "")[:10]


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return _jdump(value)


def _workspace_mb():
    """Total size of /agent/workspace in MB, skipping hidden dirs/files.

    Same walk as app/data/system.py:load_system_info — moved here so it stays out
    of the render path entirely.
    """
    try:
        total = 0
        for dirpath, dirs, filenames in os.walk(WORKSPACE_DIR):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                try:
                    total += os.path.getsize(os.path.join(dirpath, fname))
                except OSError:
                    pass
        return round(total / (1024**2), 1)
    except OSError:
        return None


def _previous_meta() -> dict:
    """Decoded `meta` from the live database, or {} when it is absent/unreadable."""
    if duckdb is None:
        return {}
    target = db_path()
    if not target.exists():
        return {}
    try:
        con = duckdb.connect(str(target), read_only=True)
    except Exception:
        return {}
    try:
        rows = con.execute("SELECT key, value FROM meta").fetchall()
        return {k: json.loads(v) for k, v in rows}
    except Exception:
        return {}
    finally:
        con.close()


def _previous_rows(tables) -> dict:
    """Rows of each named table from the live database, keyed by table name.

    Handlers use this to carry forward work that has not changed (see the
    incremental parse in services/metrics/usage.py). One connection covers
    every table — a handler declaring three tables should not cost three opens.
    Missing or unreadable tables come back as [].
    """
    tables = list(tables)
    # None means "could not read", which is NOT the same as an empty table: a
    # handler carrying its rows forward must collect fresh rather than publish
    # emptiness it cannot vouch for.
    result = {table: None for table in tables}
    if duckdb is None or not tables:
        return result
    target = db_path()
    if not target.exists():
        return result
    try:
        con = duckdb.connect(str(target), read_only=True)
    except Exception:
        return result
    try:
        for table in tables:
            try:
                result[table] = con.execute(f"SELECT * FROM {table}").fetchall()
            except Exception:
                result[table] = None
    finally:
        con.close()
    return result


def _ltm_bytes() -> int:
    """Total size of the LanceDB long-term-memory store, in bytes.

    Same walk as app/data/memory.py:load_ltm_size, moved off the render path.
    Always an int (0 when absent or unreadable) so the column is non-nullable
    end to end and the tab can format it without a None check.
    """
    path = MEMORY_DIR / LTM_STORE_NAME
    if not path.exists():
        return 0
    total = 0
    try:
        for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
            for fname in files:
                try:
                    total += os.path.getsize(os.path.join(root, fname))
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def _throttled(now, previous, key, measure):
    """Carry a previous expensive measurement forward until it ages out.

    Both directory walks (workspace, LanceDB store) are O(files) over dirs that
    grow into the hundreds of MB, and state.json changes every heartbeat — so
    unthrottled they would run on nearly every daemon poll.
    Returns (value, measured_at_iso).
    """
    prev_at = _parse_dt(previous.get(f"{key}_at"))
    if prev_at is not None and key in previous:
        age_minutes = (now - prev_at).total_seconds() / 60
        if 0 <= age_minutes < WORKSPACE_WALK_INTERVAL_MINUTES:
            return previous[key], previous[f"{key}_at"]
    return measure(), now.isoformat()


def _parse_dt(value):
    """Parse an ISO timestamp into an aware datetime, or None. Mirrors app.shared.parse_dt."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ─── Source loading ───────────────────────────────────────────────────────────


def _migrations():
    """Return the (cycles, journal) list migrators, or no-ops when unavailable."""
    try:
        from scripts.repair_memory_files import (
            migrate_cycles_list,
            migrate_journal_list,
        )
    except ImportError:
        try:
            from repair_memory_files import migrate_cycles_list, migrate_journal_list
        except ImportError:
            return (lambda x: x), (lambda x: x)
    return migrate_cycles_list, migrate_journal_list


def _merge_by_cycle(active: list, archived: list, reverse: bool) -> list:
    """Merge active + archive, dedup by cycle_number (active wins), sort by cycle."""
    seen = {e.get("cycle_number") for e in active if isinstance(e, dict)}
    merged = [e for e in active if isinstance(e, dict)]
    merged += [
        e for e in archived if isinstance(e, dict) and e.get("cycle_number") not in seen
    ]
    merged.sort(key=lambda e: _int_or_none(e.get("cycle_number")) or 0, reverse=reverse)
    return merged


def load_sources() -> dict:
    """Read and normalise every raw source. Mirrors the existing app/data loaders."""
    src = _sources()
    migrate_cycles_list, migrate_journal_list = _migrations()

    # Read every source exactly once. `raw[key] is None` means missing or
    # unparseable, which _compute_memory_files reports as "parse err" — so the
    # undecoded value is kept alongside the normalised one.
    raw = {key: _read_json(path, None) for key, path in src.items()}

    cycles_active = _as_list(raw["cycles"])
    cycles_archived = _as_list(raw["cycles_archive"])
    migrate_cycles_list(cycles_active)
    migrate_cycles_list(cycles_archived)
    # Ascending by cycle_number — same order load_cycles() produced.
    cycles = _merge_by_cycle(cycles_active, cycles_archived, reverse=False)

    journal_active = _as_list(raw["journal"])
    journal_archived = _as_list(raw["journal_archive"])
    migrate_journal_list(journal_active)
    migrate_journal_list(journal_archived)
    # Descending — same order _parse_journal_entries() produced.
    journal = _merge_by_cycle(journal_active, journal_archived, reverse=True)

    return {
        "cycles": cycles,
        "journal": journal,
        # Only dict elements survive: a hand-edited or half-written goal.json
        # holding a bare string or null must not take the whole collector down.
        "goals": [g for g in _as_list(raw["goals"]) if isinstance(g, dict)],
        "state": _as_dict(raw["state"]),
        "weights": _as_dict(raw["weights"]),
        "errors": _as_list(raw["errors"]),
        "sys_metrics": _as_list(raw["sys_metrics"]),
        "agents": _as_list(raw["agents"]),
        "inbox": _as_list(raw["inbox"]),
        # Payloads keyed by memory-dir filename, handed to _compute_memory_files
        # so it counts entries without re-parsing what we just read. cycles.json
        # and journal.json are the largest files in the memory dir; re-reading
        # them for a len() would triple the parse cost of every build.
        "decoded": {
            path.name: raw[key]
            for key, path in src.items()
            if path.parent == MEMORY_DIR
        },
        # Raw per-file counts for the Memory Overview strip, which reports
        # "active vs archived" separately rather than the merged totals above.
        "raw_counts": {
            key: _entry_count(raw[key])
            for key in (
                "cycles",
                "cycles_archive",
                "journal",
                "journal_archive",
                "inbox_history",
                "outbox_history",
            )
        },
        "file_sizes": {
            key: _file_size(src[key]) for key in ("inbox_history", "outbox_history")
        },
    }


def _entry_count(data):
    """len() for a list or dict, else 0. Matches memory_tab's _json_count."""
    if isinstance(data, (list, dict)):
        return len(data)
    return 0


def _file_size(path):
    """Size in bytes, or None when the file is missing.

    None is meaningful: the Memory tab renders "—" for an absent history file
    and "0 B" for an empty one, which a 0-on-error would conflate.
    """
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def source_fingerprint(now=None, previous_meta=None, force_handlers=False) -> str:
    """Fingerprint of every source: (name, mtime, size) tuples as JSON.

    Carries a UTC hour bucket as well, because two metrics are time-relative and
    not file-relative: `metric_health.errors_24h` (a rolling 24h window) and the
    "N error(s) logged today" suggestion. Without it a quiet agent would keep
    showing yesterday's error count forever. The replaced `load_suggest()` did
    the same thing with a date bucket in its cache key (app/data/suggest.py).

    *previous_meta* is the live database's meta, used to decide whether each
    handler is due; it is read on demand when not supplied. Set
    *force_handlers* to match a build that will collect every handler
    regardless of its interval, so the stored fingerprint describes what was
    actually collected.
    """
    now = now or datetime.now(timezone.utc)
    parts = [["_hour_bucket", now.strftime("%Y-%m-%dT%H"), 0]]
    for name, path in sorted(_sources().items()):
        try:
            st = os.stat(path)
            parts.append([name, st.st_mtime, st.st_size])
        except OSError:
            parts.append([name, 0.0, -1])
    # The memory-file health metrics cover every *.json in the memory dir, not
    # just the ones _sources() names, so they need to be in the key too.
    for name, size, mtime in _memory_json_files():
        parts.append([f"memory/{name}", mtime, size])
    # Handlers read sources the core knows nothing about (transcripts, and
    # whatever a future handler adds), so each contributes its own key.
    handlers, errors = _handlers()
    previous = previous_meta if previous_meta is not None else _previous_meta()
    # The roster itself is part of the key: installing, removing, or renaming a
    # handler must rebuild even when it declares no fingerprint(), otherwise
    # its tables would simply be absent from the store — and readers would get
    # empty defaults, silently, until some unrelated source happened to change.
    parts.append(
        [
            "handler_roster",
            json.dumps([[h.NAME, sorted(h.TABLES), _schema_hash(h)] for h in handlers]),
            0,
        ]
    )
    for handler in handlers:
        fn = getattr(handler, "fingerprint", None)
        if fn is None:
            continue
        if not force_handlers and not _handler_due(handler, now, previous):
            # Not due: reuse the fingerprint recorded at its last collection so
            # churn in its sources cannot trigger a rebuild before then. This
            # also skips the fingerprint's own I/O (for `usage`, a stat() per
            # transcript). When the interval elapses the live value is read
            # again and any change shows up immediately.
            parts.append(
                [
                    f"handler/{handler.NAME}",
                    previous.get(f"{handler.NAME}.fingerprint", ""),
                    0,
                ]
            )
            continue
        try:
            ctx = _handler_context(now, {}, previous, lambda _t: [], handler)
            parts.append([f"handler/{handler.NAME}", fn(ctx), 0])
        except Exception as exc:
            # A handler that can't fingerprint must not pin the store: make the
            # key vary so the build re-runs and records the failure.
            parts.append([f"handler/{handler.NAME}", f"error: {exc}", 0])
    for name, message in errors:
        parts.append([f"handler_error/{name}", message, 0])
    return json.dumps(parts)


# ─── Metric computation ───────────────────────────────────────────────────────
#
# Everything below runs at collection time. The portal reads the results verbatim.


def _journal_by_cycle(journal: list) -> dict:
    """Index journal entries by cycle number for the summary join."""
    out = {}
    for entry in journal:
        num = _int_or_none(entry.get("cycle_number"))
        if num is not None and num not in out:
            out[num] = entry
    return out


def _enriched_summary(cycle: dict, journal_entry: dict) -> str:
    """Journal summary preferred over the cycle's own — what the tab did inline."""
    return _text(
        journal_entry.get("summary")
        or journal_entry.get("outcome")
        or cycle.get("summary")
        or ""
    )


def _compute_daily(cycles: list) -> list:
    """Per-day rollup of cycle counts, active time, and category/type breakdowns."""
    by_day: dict[str, dict] = {}
    for c in cycles:
        day = _day_of(c.get("start"))
        if not day:
            continue
        row = by_day.setdefault(
            day,
            {
                "day": day,
                "cycles_total": 0,
                "cycles_completed": 0,
                "cycles_failed": 0,
                "active_seconds": 0.0,
                "goal_cycles": 0,
                "evolve_cycles": 0,
                "categories": {},
                "types": {},
            },
        )
        row["cycles_total"] += 1
        status = c.get("cycle_status")
        if status == "completed":
            row["cycles_completed"] += 1
        elif status == "failed":
            row["cycles_failed"] += 1
        row["active_seconds"] += _num(c.get("duration_seconds")) or 0.0
        category = c.get("cycle_category") or ""
        if category:
            row["categories"][category] = row["categories"].get(category, 0) + 1
        ctype = c.get("cycle_type") or "unknown"
        row["types"][ctype] = row["types"].get(ctype, 0) + 1
        if ctype == "goal":
            row["goal_cycles"] += 1
        elif ctype == "evolve":
            row["evolve_cycles"] += 1

    rows = []
    for day in sorted(by_day, reverse=True):
        row = by_day[day]
        rows.append(
            (
                row["day"],
                row["cycles_total"],
                row["cycles_completed"],
                row["cycles_failed"],
                round(row["active_seconds"], 3),
                row["goal_cycles"],
                row["evolve_cycles"],
                len(row["categories"]),
                _jdump(row["categories"]),
                _jdump(row["types"]),
            )
        )
    return rows


def _compute_timeline(cycles: list, journal_index: dict) -> list:
    """Chronological per-cycle rows for the most recent TIMELINE_DAYS days."""
    days = sorted({_day_of(c.get("start")) for c in cycles if _day_of(c.get("start"))})
    keep = set(days[-TIMELINE_DAYS:])
    rows = []
    for c in cycles:
        day = _day_of(c.get("start"))
        if day not in keep:
            continue
        num = _int_or_none(c.get("cycle_number"))
        entry = journal_index.get(num, {}) if num is not None else {}
        rows.append(
            (
                day,
                num,
                _text(c.get("start")),
                _text(c.get("cycle_type")),
                _text(c.get("cycle_category")),
                _text(c.get("cycle_status")),
                _num(c.get("duration_seconds")),
                _enriched_summary(c, entry),
            )
        )
    rows.sort(key=lambda r: (r[0], r[2]))
    return rows


def _compute_goal_stats(goals: list, cycles: list) -> dict:
    """Goal performance rollup. Same keys as app/data/goal.py:load_goal_stats."""
    total = len(goals)
    completed = sum(1 for g in goals if g.get("status") == "completed")
    failed = sum(1 for g in goals if g.get("status") == "failed")

    delegated = [g for g in goals if isinstance(g.get("delegated_to"), dict)]
    by_agent: dict[str, int] = {}
    for g in delegated:
        name = g["delegated_to"].get("name")
        if name:
            by_agent[name] = by_agent.get(name, 0) + 1

    goal_cycles = [
        c
        for c in cycles
        if c.get("cycle_type") == "goal" and _num(c.get("duration_seconds")) is not None
    ]
    avg_dur = None
    if goal_cycles:
        avg_dur = sum(_num(c["duration_seconds"]) for c in goal_cycles) / len(
            goal_cycles
        )

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "completion_rate": (completed / total) if total else 0.0,
        "avg_goal_duration_seconds": avg_dur,
        "delegated_total": len(delegated),
        "delegated_awaiting": sum(
            1 for g in delegated if g.get("status") in ("pending", "in_progress")
        ),
        "delegated_completed": sum(
            1 for g in delegated if g.get("status") == "completed"
        ),
        "delegated_failed": sum(1 for g in delegated if g.get("status") == "failed"),
        "delegated_by_agent": by_agent,
        "goal_cycles": goal_cycles,
    }


def _compute_balance(cycles: list, weights_data: dict) -> tuple[list, dict]:
    """Evolution category balance rows + the scalars that go in `meta`."""
    evolve = [
        c for c in cycles if c.get("cycle_type") == "evolve" and c.get("cycle_category")
    ]
    all_time: dict[str, int] = {}
    for c in evolve:
        cat = c["cycle_category"]
        all_time[cat] = all_time.get(cat, 0) + 1
    recent: dict[str, int] = {}
    for c in evolve[-10:]:
        cat = c["cycle_category"]
        recent[cat] = recent.get(cat, 0) + 1

    weights = _as_dict(weights_data.get("weights"))
    goal_signals = _as_list(weights_data.get("goal_signals"))

    suggestion = weights_data.get("suggestion") if weights else None
    if not suggestion:
        min_count = min(all_time.get(c, 0) for c in ALL_CATEGORIES)
        candidates = [c for c in ALL_CATEGORIES if all_time.get(c, 0) == min_count]
        suggestion = candidates[0] if candidates else None

    rows = []
    for cat in ALL_CATEGORIES:
        w = _as_dict(weights.get(cat))
        rows.append(
            (
                cat,
                all_time.get(cat, 0),
                recent.get(cat, 0),
                _num(w.get("score")) or 0.0,
                _num(w.get("base_need")) or 0.0,
                _num(w.get("recency_boost")) or 0.0,
                _num(w.get("goal_alignment")) or 0.0,
                _num(w.get("roi_bonus")) or 0.0,
                _num(w.get("maturity_penalty")) or 0.0,
                cat == suggestion,
            )
        )

    # Pre-compute the denominator the progress bars used to derive with max().
    max_score = max((r[3] for r in rows), default=0.0)
    meta = {
        "balance_total_evolve_cycles": sum(all_time.values()),
        "balance_suggestion": suggestion,
        "balance_has_weights": bool(weights),
        "balance_max_score": max(max_score, 1.0),
        "balance_goal_signals": goal_signals,
    }
    return rows, meta


def _compute_velocity(cycles: list) -> tuple[list, dict]:
    """Last VELOCITY_LIMIT completed cycles (chronological) + the three averages."""
    completed = [
        c
        for c in cycles
        if c.get("cycle_status") == "completed"
        and c.get("start")
        and _num(c.get("duration_seconds")) is not None
    ]
    recent = sorted(completed, key=lambda c: str(c.get("start") or ""), reverse=True)[
        :VELOCITY_LIMIT
    ]
    recent.reverse()  # chronological, as the chart renders left-to-right

    rows = [
        (
            i,
            _int_or_none(c.get("cycle_number")),
            _text(c.get("cycle_type")),
            _text(c.get("start")),
            _num(c.get("duration_seconds")),
        )
        for i, c in enumerate(recent)
    ]

    def _avg(values):
        clean = [v for v in values if v is not None]
        return round(sum(clean) / len(clean)) if clean else None

    meta = {
        "velocity_count": len(rows),
        "velocity_avg_all": _avg([r[4] for r in rows]),
        "velocity_avg_evolve": _avg([r[4] for r in rows if r[2] == "evolve"]),
        "velocity_avg_goal": _avg([r[4] for r in rows if r[2] == "goal"]),
    }
    return rows, meta


def _compute_improvements(cycles: list, journal_index: dict) -> list:
    """Completed evolve/goal cycles, newest first, with journal-enriched summaries."""
    picked = [
        c
        for c in cycles
        if c.get("cycle_status") == "completed"
        and c.get("cycle_type") in ("evolve", "goal")
    ]
    picked.sort(key=lambda c: str(c.get("start") or ""), reverse=True)
    rows = []
    for rank, c in enumerate(picked[:IMPROVEMENTS_LIMIT]):
        num = _int_or_none(c.get("cycle_number"))
        entry = journal_index.get(num, {}) if num is not None else {}
        actions = entry.get("actions") or c.get("actions") or []
        rows.append(
            (
                rank,
                num,
                _text(c.get("cycle_type")),
                _text(c.get("cycle_category")),
                _text(c.get("start")),
                _num(c.get("duration_seconds")),
                _enriched_summary(c, entry),
                _jdump(_as_list(actions)),
            )
        )
    return rows


def _compute_health(state: dict, goals: list, errors: list, inbox: list, now) -> tuple:
    """The live health strip: status, heartbeat, cycle #, active goals, 24h errors."""
    cutoff = now - timedelta(hours=24)
    recent = []
    for e in errors:
        if not isinstance(e, dict):
            continue
        ts = _parse_dt(e.get("timestamp"))
        if ts and ts > cutoff:
            recent.append(e)
    tabs = sorted({_text(e.get("tab")) or "?" for e in recent})
    active_goals = sum(
        1 for g in goals if g.get("status") in ("in_progress", "pending")
    )
    return (
        _text(state.get("agent_status") or "unknown"),
        _text(state.get("last_heartbeat")),
        _int_or_none(state.get("cycle_number")) or 0,
        active_goals,
        len(recent),
        _jdump(tabs),
        len(inbox),
    )


def _compute_velocity_per_hour(cycles: list):
    """Cycles/hour over the last 10 completed cycles. Mirrors load_cycle_velocity."""
    completed = [
        c for c in cycles if c.get("cycle_status") == "completed" and c.get("start")
    ]
    if len(completed) < 2:
        return None
    recent = sorted(completed, key=lambda c: str(c.get("start") or ""), reverse=True)[
        :10
    ]
    if len(recent) < 2:
        return None
    newest = _parse_dt(recent[0].get("start"))
    oldest = _parse_dt(recent[-1].get("start"))
    if newest is None or oldest is None:
        return None
    span_hours = (newest - oldest).total_seconds() / 3600
    return round(len(recent) / span_hours, 1) if span_hours > 0 else None


def _compute_memory_files(now, decoded=None) -> tuple[list, dict]:
    """Size/age/health for every *.json in the memory dir, plus the summary.

    `decoded` maps a filename to a payload load_sources() already parsed, so the
    biggest files (cycles.json, journal.json and their archives) are read once
    per build rather than again for their entry count.
    """
    decoded = decoded or {}
    rows = []
    total_kb = 0.0
    warn_count = 0
    crit_count = 0

    for fname, size_bytes, mtime in _memory_json_files():
        size_kb = size_bytes / 1024
        total_kb += size_kb
        age_hours = (
            now - datetime.fromtimestamp(mtime, tz=timezone.utc)
        ).total_seconds() / 3600

        age_exempt = fname in MEMORY_AGE_EXEMPT
        size_crit = size_kb >= MEMORY_SIZE_CRIT_KB
        size_warn = size_kb >= MEMORY_SIZE_WARN_KB
        age_crit = not age_exempt and age_hours >= MEMORY_AGE_CRIT_HOURS
        age_warn = not age_exempt and age_hours >= MEMORY_AGE_WARN_HOURS

        # Per-dimension grades as well as the overall one, so the tab can colour
        # the size and age columns without re-deriving the thresholds.
        size_health = "crit" if size_crit else ("warn" if size_warn else "ok")
        age_health = "crit" if age_crit else ("warn" if age_warn else "ok")

        if size_crit or age_crit:
            health = "crit"
            crit_count += 1
        elif size_warn or age_warn:
            health = "warn"
            warn_count += 1
        else:
            health = "ok"

        data = (
            decoded[fname] if fname in decoded else _read_json(MEMORY_DIR / fname, None)
        )
        if isinstance(data, list):
            entry_count, entry_kind = len(data), "entries"
        elif isinstance(data, dict):
            entry_count, entry_kind = len(data), "keys"
        else:
            entry_count, entry_kind = None, "parse err"

        rows.append(
            (
                fname,
                round(size_kb, 3),
                entry_count,
                entry_kind,
                round(age_hours, 4),
                age_exempt,
                health,
                size_health,
                age_health,
            )
        )

    meta = {
        "memory_files_total": len(rows),
        "memory_files_total_kb": round(total_kb, 3),
        "memory_files_warn": warn_count,
        "memory_files_crit": crit_count,
        "memory_files_ok": len(rows) - warn_count - crit_count,
    }
    return rows, meta


def _compute_agent_errors(agents: list, errors: list, now) -> list:
    """Per-agent error rollup. Mirrors agents_tab._load_agent_errors matching.

    An error belongs to an agent when its `context` starts with "<name>:"
    (service-level errors from the internal_agent_chat daemon) or its `tab`
    equals the agent name (a direct tab render error).
    """
    cutoff = now - timedelta(hours=AGENT_ERROR_RECENT_HOURS)
    clean = [e for e in errors if isinstance(e, dict)]
    rows = []
    seen = set()
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        name = agent.get("name")
        # A duplicate registry entry would otherwise emit two rows, and the
        # reader takes the first one arbitrarily.
        if not name or name in seen:
            continue
        seen.add(name)
        prefix = f"{name}:"
        matched = [
            e
            for e in clean
            if str(e.get("context") or "").startswith(prefix) or e.get("tab") == name
        ]
        matched.sort(key=lambda e: _text(e.get("timestamp")), reverse=True)
        recent = sum(
            1 for e in matched if (_parse_dt(e.get("timestamp")) or cutoff) > cutoff
        )
        rows.append(
            (
                _text(name),
                len(matched),
                recent,
                _text(matched[0].get("timestamp")) if matched else "",
            )
        )
    return rows


def _compute_suggestions(src: dict, journal_total: int, workspace_mb, today: str):
    """Ranked action suggestions. Ported from app/data/suggest.py:load_suggest."""
    suggestions = []
    goals = src["goals"]
    in_progress = [g for g in goals if g.get("status") == "in_progress"]
    pending = [g for g in goals if g.get("status") == "pending"]
    if in_progress:
        suggestions.append(
            {
                "priority": "high",
                "category": "goal",
                "action": f"Continue in-progress goal: {_text(in_progress[0].get('content'))[:80]}",
                "reason": f"{len(in_progress)} goal(s) in progress",
            }
        )
    if pending:
        suggestions.append(
            {
                "priority": "high",
                "category": "goal",
                "action": f"Start pending goal: {_text(pending[0].get('content'))[:80]}",
                "reason": f"{len(pending)} goal(s) pending",
            }
        )
    if src["inbox"]:
        suggestions.append(
            {
                "priority": "high",
                "category": "goal",
                "action": "Process inbox messages",
                "reason": f"{len(src['inbox'])} unprocessed message(s) in inbox",
            }
        )
    if journal_total > 30:
        suggestions.append(
            {
                "priority": "medium",
                "category": "efficiency",
                "action": "Run: uv run python scripts/journal_archive.py",
                "reason": f"Journal has {journal_total} entries — archive old entries to speed up loading",
            }
        )
    todays_errors = [
        e
        for e in src["errors"]
        if isinstance(e, dict) and _text(e.get("timestamp"))[:10] >= today
    ]
    if todays_errors:
        suggestions.append(
            {
                "priority": "medium",
                "category": "reliability",
                "action": "Investigate recent server errors",
                "reason": f"{len(todays_errors)} error(s) logged today",
            }
        )
    if not any(s["priority"] == "high" for s in suggestions):
        counts: dict[str, int] = {}
        for c in src["cycles"]:
            if c.get("cycle_type") == "evolve" and c.get("cycle_category"):
                cat = c["cycle_category"]
                counts[cat] = counts.get(cat, 0) + 1
        underserved = sorted(ALL_CATEGORIES, key=lambda c: counts.get(c, 0))
        if underserved:
            first = underserved[0]
            suggestions.append(
                {
                    "priority": "low",
                    "category": "evolve",
                    "action": f"Evolve: focus on {first.replace('_', ' ')} (least served category)",
                    "reason": f"{first} has {counts.get(first, 0)} cycles",
                }
            )
    if workspace_mb and workspace_mb > 800:
        suggestions.append(
            {
                "priority": "medium",
                "category": "efficiency",
                "action": "Clean up workspace — approaching 1GB limit",
                "reason": f"Workspace is {workspace_mb}MB (limit: 1GB)",
            }
        )
    suggestions.sort(key=lambda s: _PRIORITY_ORDER.get(s["priority"], 3))
    return [
        (i, s["priority"], s["category"], s["action"], s["reason"])
        for i, s in enumerate(suggestions)
    ]


# ─── Schema + build ───────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE meta (key VARCHAR PRIMARY KEY, value VARCHAR);

CREATE TABLE cycles (
    cycle_number INTEGER, cycle_type VARCHAR, cycle_category VARCHAR,
    cycle_status VARCHAR, cycle_goal VARCHAR, day VARCHAR,
    start_ts VARCHAR, end_ts VARCHAR, duration_seconds DOUBLE,
    summary VARCHAR, actions VARCHAR
);
CREATE TABLE journal (
    cycle_number INTEGER, timestamp VARCHAR, cycle_type VARCHAR,
    summary VARCHAR, outcome VARCHAR, actions VARCHAR
);
CREATE TABLE goals (
    idx INTEGER, content VARCHAR, status VARCHAR, created_at VARCHAR,
    delegated_to_name VARCHAR
);
CREATE TABLE errors (timestamp VARCHAR, tab VARCHAR, message VARCHAR);
CREATE TABLE agent_state (
    agent_status VARCHAR, last_heartbeat VARCHAR, cycle_number INTEGER,
    current_goal VARCHAR, last_cycle_summary VARCHAR, last_cycle_run VARCHAR
);
CREATE TABLE sys_snapshots (
    ts VARCHAR, portal_ms DOUBLE, portal_status INTEGER, portal_ok BOOLEAN,
    mem_used_mb DOUBLE, mem_total_mb DOUBLE, load_1m DOUBLE,
    disk_used_gb DOUBLE, disk_total_gb DOUBLE
);

CREATE TABLE metric_health (
    agent_status VARCHAR, last_heartbeat VARCHAR, cycle_number INTEGER,
    active_goals INTEGER, errors_24h INTEGER, error_tabs VARCHAR,
    inbox_count INTEGER
);
CREATE TABLE metric_daily (
    day VARCHAR, cycles_total INTEGER, cycles_completed INTEGER,
    cycles_failed INTEGER, active_seconds DOUBLE, goal_cycles INTEGER,
    evolve_cycles INTEGER, category_count INTEGER,
    categories VARCHAR, types VARCHAR
);
CREATE TABLE metric_day_timeline (
    day VARCHAR, cycle_number INTEGER, start_ts VARCHAR, cycle_type VARCHAR,
    cycle_category VARCHAR, cycle_status VARCHAR, duration_seconds DOUBLE,
    summary VARCHAR
);
CREATE TABLE metric_goal_stats (
    total INTEGER, completed INTEGER, failed INTEGER, completion_rate DOUBLE,
    avg_goal_duration_seconds DOUBLE, delegated_total INTEGER,
    delegated_awaiting INTEGER, delegated_completed INTEGER,
    delegated_failed INTEGER, delegated_by_agent VARCHAR
);
CREATE TABLE metric_goal_cycle_durations (
    rank INTEGER, cycle_number INTEGER, duration_seconds DOUBLE
);
CREATE TABLE metric_recent_goals (
    rank INTEGER, content VARCHAR, status VARCHAR, created_at VARCHAR
);
CREATE TABLE metric_balance (
    category VARCHAR, all_time_count INTEGER, recent10_count INTEGER,
    score DOUBLE, base_need DOUBLE, recency_boost DOUBLE,
    goal_alignment DOUBLE, roi_bonus DOUBLE, maturity_penalty DOUBLE,
    is_suggested BOOLEAN
);
CREATE TABLE metric_velocity (
    rank INTEGER, cycle_number INTEGER, cycle_type VARCHAR,
    start_ts VARCHAR, duration_seconds DOUBLE
);
CREATE TABLE metric_improvements (
    rank INTEGER, cycle_number INTEGER, cycle_type VARCHAR,
    cycle_category VARCHAR, start_ts VARCHAR, duration_seconds DOUBLE,
    summary VARCHAR, actions VARCHAR
);
CREATE TABLE metric_suggestions (
    rank INTEGER, priority VARCHAR, category VARCHAR,
    action VARCHAR, reason VARCHAR
);
CREATE TABLE metric_memory_overview (
    ltm_bytes BIGINT, journal_active INTEGER, journal_archived INTEGER,
    cycles_active INTEGER, cycles_archived INTEGER,
    inbox_history INTEGER, outbox_history INTEGER,
    inbox_history_bytes BIGINT, outbox_history_bytes BIGINT
);
CREATE TABLE metric_memory_files (
    fname VARCHAR, size_kb DOUBLE, entry_count INTEGER, entry_kind VARCHAR,
    age_hours DOUBLE, age_exempt BOOLEAN, health VARCHAR,
    size_health VARCHAR, age_health VARCHAR
);
CREATE TABLE metric_agent_errors (
    agent VARCHAR, total INTEGER, recent_count INTEGER, last_error_ts VARCHAR
);
CREATE TABLE metric_handler_status (
    name VARCHAR, state VARCHAR, ok BOOLEAN, rows INTEGER,
    duration_ms DOUBLE, error VARCHAR
);
"""

# Core tables. Handler tables are appended by all_tables() at call time —
# a handler dropped into services/metrics/ must show up in --stats too.
TABLES = [
    "meta",
    "cycles",
    "journal",
    "goals",
    "errors",
    "agent_state",
    "sys_snapshots",
    "metric_health",
    "metric_daily",
    "metric_day_timeline",
    "metric_goal_stats",
    "metric_goal_cycle_durations",
    "metric_recent_goals",
    "metric_balance",
    "metric_velocity",
    "metric_improvements",
    "metric_suggestions",
    "metric_memory_overview",
    "metric_memory_files",
    "metric_agent_errors",
    "metric_handler_status",
]


# ─── Pluggable handlers (services/metrics/*.py) ───────────────────────────────


def _handlers() -> tuple:
    """Discover handler modules. Returns ``(handlers, errors)``; never raises.

    Import failures are returned rather than raised so a broken handler cannot
    stop the core metrics from being collected.
    """
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from services.metrics import discover
    except Exception as exc:  # pragma: no cover - only if the package is broken
        return [], [("services.metrics", f"{type(exc).__name__}: {exc}")]
    try:
        handlers, errors = discover()
    except Exception as exc:  # pragma: no cover - discover() catches its own
        return [], [("services.metrics", f"{type(exc).__name__}: {exc}")]

    # A handler may not claim a core table. Nothing stops it declaring
    # TABLES = ["cycles"]: the DDL would not collide (it creates something
    # else), the undeclared-table guard would pass, and its rows would be
    # appended straight into the core table.
    # Two handlers sharing a table name would interleave their rows — one
    # feature's metrics silently polluted by another's — so every table in the
    # store has exactly one owner.
    core = set(TABLES)
    claimed = {}
    safe = []
    for handler in handlers:
        clash = sorted(core.intersection(handler.TABLES))
        if clash:
            errors.append((handler.NAME, f"declares core table(s): {', '.join(clash)}"))
            continue
        taken = sorted(t for t in handler.TABLES if t in claimed)
        if taken:
            owners = ", ".join(f"{t} (owned by {claimed[t]})" for t in taken)
            errors.append(
                (handler.NAME, f"declares table(s) already claimed: {owners}")
            )
            continue
        for table in handler.TABLES:
            claimed[table] = handler.NAME
        safe.append(handler)
    return safe, errors


def all_tables() -> list:
    """Core tables plus every table declared by a discovered handler."""
    tables = list(TABLES)
    handlers, _errors = _handlers()
    for handler in handlers:
        for table in handler.TABLES:
            if table not in tables:
                tables.append(table)
    return tables


def handler_interval(handler):
    """A handler's minimum gap between collections, or None for every build."""
    raw = getattr(handler, "POLL_INTERVAL_SECONDS", None)
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _handler_due(handler, now, previous_meta) -> bool:
    """Whether *handler* should collect on this build.

    Due when it declares no interval, has never run, its recorded run time is
    unreadable, or that much time has passed. A clock that jumped backwards
    (negative elapsed) counts as due rather than pinning the handler forever.
    """
    # A handler that reported leftover work stays due until it is caught up,
    # whatever its interval — otherwise a backfill would advance by one slice
    # per interval, and (for `usage`, which attributes oldest-first) the most
    # recent data would be the last to appear.
    if _int_or_none(previous_meta.get(f"{handler.NAME}.pending")) or 0:
        return True

    interval = handler_interval(handler)
    if interval is None:
        return True
    last = _parse_dt(previous_meta.get(f"{handler.NAME}.collected_at"))
    if last is None:
        return True
    elapsed = (now - last).total_seconds()
    return elapsed < 0 or elapsed >= interval


def _meta_key_re():
    """The handler contract's meta-key rule, imported lazily."""
    from services.metrics.base import META_KEY_RE

    return META_KEY_RE


def _handler_context(now, src, previous_meta, previous_rows, handler=None):
    """Build a context for one handler.

    Every handler gets its *own* context, and nothing mutable is shared:

    * ``sources`` is a read-only view, so a handler cannot re-order or clear a
      payload the next handler is about to read.
    * ``previous_meta`` is a copy — writing to a shared one could set another
      handler's ``<NAME>.collected_at`` and pin it as never-due.
    * ``previous_rows`` hands back fresh tuples, and only for tables this
      handler owns. Returning the live list let a handler mutate rows that
      another handler was about to carry forward verbatim.
    """
    from types import MappingProxyType

    from services.metrics.base import HandlerContext

    owned = set(handler.TABLES) if handler is not None else None

    def _rows(table):
        if owned is not None and table not in owned:
            return []
        return [tuple(r) for r in previous_rows(table) or []]

    # Shallow-copy the containers: MappingProxyType stops a handler rebinding a
    # key, but not `ctx.sources["cycles"].clear()`. The records inside are
    # still shared (deep-copying every cycle per handler would be wasteful), so
    # handlers must treat them as read-only — the containers themselves are
    # what an accidental sort()/clear()/append() would damage.
    safe_sources = {
        key: list(value) if isinstance(value, list) else value
        for key, value in src.items()
    }

    return HandlerContext(
        agent_dir=AGENT_DIR,
        memory_dir=MEMORY_DIR,
        messages_dir=MESSAGES_DIR,
        now=now,
        sources=MappingProxyType(safe_sources),
        previous_meta=dict(previous_meta),
        previous_rows=_rows,
    )


def _schema_hash(handler) -> str:
    """Digest of a handler's DDL, so a column change is a detectable event.

    Carrying rows forward is positional. A same-arity column swap or retype
    would land values in the wrong columns with no error anywhere — and for an
    incremental handler that mis-filing then republishes itself on every later
    build. Any DDL edit therefore invalidates both the store and the carry.
    """
    try:
        body = "\n".join(str(s) for s in handler.SCHEMA)
    except Exception:
        return ""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _table_names(con) -> set:
    """Every table currently in the open database."""
    try:
        return {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
    except Exception:
        return set()


def _carry_forward(con, handler, carried):
    """Re-insert a not-due handler's previous rows. Returns the count, or None.

    None means the copy failed and the caller should collect fresh instead of
    publishing empty tables.
    """
    # A table the previous build did not have (a handler that just added one,
    # or a database swapped out mid-read) reads back as None. Publishing it
    # empty would look like real data — collect fresh instead.
    if any(carried.get(table) is None for table in handler.TABLES):
        return None
    try:
        total = 0
        con.execute("BEGIN TRANSACTION")
        for table in handler.TABLES:
            rows = [tuple(r) for r in carried.get(table) or []]
            _insert(con, table, rows)
            total += len(rows)
        con.execute("COMMIT")
        return total
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        return None


def _require_duckdb():
    if duckdb is None:  # pragma: no cover - exercised only without the dependency
        raise RuntimeError(
            "ERROR: duckdb not installed. Add it to pyproject.toml and run `uv sync`."
        )


def _insert(con, table: str, rows: list) -> None:
    if not rows:
        return
    placeholders = ", ".join("?" for _ in rows[0])
    con.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)


def _cycle_rows(cycles: list) -> list:
    return [
        (
            _int_or_none(c.get("cycle_number")),
            _text(c.get("cycle_type")),
            _text(c.get("cycle_category")),
            _text(c.get("cycle_status")),
            _text(c.get("cycle_goal")),
            _day_of(c.get("start")),
            _text(c.get("start")),
            _text(c.get("end")),
            _num(c.get("duration_seconds")),
            _text(c.get("summary")),
            _jdump(_as_list(c.get("actions"))),
        )
        for c in cycles
    ]


def _journal_rows(journal: list) -> list:
    return [
        (
            _int_or_none(e.get("cycle_number")),
            _text(e.get("timestamp")),
            _text(e.get("cycle_type")),
            _text(e.get("summary")),
            _text(e.get("outcome")),
            _jdump(_as_list(e.get("actions"))),
        )
        for e in journal
    ]


def _goal_rows(goals: list) -> list:
    rows = []
    for i, g in enumerate(goals):
        delegated = g.get("delegated_to")
        rows.append(
            (
                i,
                _text(g.get("content") or g.get("goal")),
                _text(g.get("status")),
                _text(g.get("created_at") or g.get("source_timestamp")),
                _text(delegated.get("name")) if isinstance(delegated, dict) else "",
            )
        )
    return rows


def _sys_snapshot_rows(snapshots: list) -> list:
    rows = []
    for s in snapshots:
        if not isinstance(s, dict):
            continue
        api = _as_dict(s.get("api")).get("_stcore_health")
        api = _as_dict(api)
        sysinfo = _as_dict(s.get("sys"))
        rows.append(
            (
                _text(s.get("ts")),
                _num(api.get("ms")),
                _int_or_none(api.get("status")),
                bool(api.get("ok")),
                _num(sysinfo.get("mem_used_mb")),
                _num(sysinfo.get("mem_total_mb")),
                _num(sysinfo.get("load_1m")),
                _num(sysinfo.get("disk_used_gb")),
                _num(sysinfo.get("disk_total_gb")),
            )
        )
    return rows


def build(
    dest: Path,
    src: dict | None = None,
    now=None,
    fingerprint=None,
    force_handlers: bool = False,
) -> None:
    """Create a fresh database at `dest` (overwriting it) from the JSON sources."""
    _require_duckdb()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        # _parse_dt always returns aware datetimes, so a naive `now` would make
        # every comparison against a stored stamp raise TypeError and abort the
        # whole build. Assume UTC, as everything else here does.
        now = now.replace(tzinfo=timezone.utc)
    # Stamp the fingerprint from BEFORE the read. If a source is written while we
    # are reading, a pre-read fingerprint makes the next refresh() rebuild (one
    # redundant build); a post-read one would mark this stale snapshot current
    # and freeze it until some unrelated source happens to change.
    previous = _previous_meta()
    if fingerprint is None:
        fingerprint = source_fingerprint(now, previous, force_handlers=force_handlers)
    src = src if src is not None else load_sources()

    # `previous` (read above, before any unlink) carries the throttle state,
    # each handler's last-run stamp, and its incremental cache. refresh()
    # builds into a tmp path so the live file survives, but build() can also be
    # called directly on the live path, where unlinking first would destroy all
    # of it and force every walk and parse to start from scratch.
    handlers, handler_errors = _handlers()
    carried = _previous_rows(
        [table for handler in handlers for table in handler.TABLES]
    )

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    cycles = src["cycles"]
    journal = src["journal"]
    journal_index = _journal_by_cycle(journal)

    con = duckdb.connect(str(dest))
    try:
        con.execute("BEGIN TRANSACTION")
        for statement in _SCHEMA.strip().split(";"):
            if statement.strip():
                con.execute(statement)

        # ── Raw tables ──
        _insert(con, "cycles", _cycle_rows(cycles))
        _insert(con, "journal", _journal_rows(journal))
        _insert(con, "goals", _goal_rows(src["goals"]))
        _insert(
            con,
            "errors",
            [
                (
                    _text(e.get("timestamp")),
                    _text(e.get("tab")),
                    _text(e.get("error") or e.get("message")),
                )
                for e in src["errors"]
                if isinstance(e, dict)
            ],
        )
        state = src["state"]
        _insert(
            con,
            "agent_state",
            [
                (
                    _text(state.get("agent_status")),
                    _text(state.get("last_heartbeat")),
                    _int_or_none(state.get("cycle_number")) or 0,
                    _text(state.get("current_goal")),
                    _text(state.get("last_cycle_summary")),
                    _text(state.get("last_cycle_run")),
                )
            ],
        )
        _insert(con, "sys_snapshots", _sys_snapshot_rows(src["sys_metrics"]))

        # ── Metric tables ──
        _insert(
            con,
            "metric_health",
            [_compute_health(state, src["goals"], src["errors"], src["inbox"], now)],
        )
        _insert(con, "metric_daily", _compute_daily(cycles))
        _insert(con, "metric_day_timeline", _compute_timeline(cycles, journal_index))

        stats = _compute_goal_stats(src["goals"], cycles)
        _insert(
            con,
            "metric_goal_stats",
            [
                (
                    stats["total"],
                    stats["completed"],
                    stats["failed"],
                    stats["completion_rate"],
                    stats["avg_goal_duration_seconds"],
                    stats["delegated_total"],
                    stats["delegated_awaiting"],
                    stats["delegated_completed"],
                    stats["delegated_failed"],
                    _jdump(stats["delegated_by_agent"]),
                )
            ],
        )
        goal_durations = stats["goal_cycles"][-GOAL_DURATION_LIMIT:]
        _insert(
            con,
            "metric_goal_cycle_durations",
            [
                (i, _int_or_none(c.get("cycle_number")), _num(c["duration_seconds"]))
                for i, c in enumerate(goal_durations)
            ],
        )

        sorted_goals = sorted(
            src["goals"],
            key=lambda g: _text(g.get("created_at") or g.get("source_timestamp")),
            reverse=True,
        )
        _insert(
            con,
            "metric_recent_goals",
            [
                (
                    i,
                    _text(g.get("content") or g.get("goal")),
                    _text(g.get("status")),
                    _text(g.get("created_at")),
                )
                for i, g in enumerate(sorted_goals[:RECENT_GOALS_LIMIT])
            ],
        )

        balance_rows, balance_meta = _compute_balance(cycles, src["weights"])
        _insert(con, "metric_balance", balance_rows)

        velocity_rows, velocity_meta = _compute_velocity(cycles)
        _insert(con, "metric_velocity", velocity_rows)

        _insert(
            con, "metric_improvements", _compute_improvements(cycles, journal_index)
        )

        workspace_mb, workspace_mb_at = _throttled(
            now, previous, "workspace_mb", _workspace_mb
        )
        ltm_bytes, ltm_bytes_at = _throttled(now, previous, "ltm_bytes", _ltm_bytes)

        raw_counts = src["raw_counts"]
        file_sizes = src["file_sizes"]
        _insert(
            con,
            "metric_memory_overview",
            [
                (
                    ltm_bytes,
                    raw_counts["journal"],
                    raw_counts["journal_archive"],
                    raw_counts["cycles"],
                    raw_counts["cycles_archive"],
                    raw_counts["inbox_history"],
                    raw_counts["outbox_history"],
                    file_sizes["inbox_history"],
                    file_sizes["outbox_history"],
                )
            ],
        )

        memory_file_rows, memory_file_meta = _compute_memory_files(
            now, decoded=src.get("decoded")
        )
        _insert(con, "metric_memory_files", memory_file_rows)

        _insert(
            con,
            "metric_agent_errors",
            _compute_agent_errors(src["agents"], src["errors"], now),
        )

        _insert(
            con,
            "metric_suggestions",
            _compute_suggestions(
                src,
                journal_total=len(journal),
                workspace_mb=workspace_mb,
                today=now.date().isoformat(),
            ),
        )

        # ── Pluggable handlers ──
        # Each runs in isolation: a raising handler leaves its (already
        # created) tables empty, is recorded in metric_handler_status, and
        # does not affect the core metrics or the other handlers.
        #
        # Commit the core work FIRST. A DuckDB transaction is aborted wholesale
        # by a runtime error — a handler returning a string for an INTEGER
        # column raises ConversionException, after which every later statement
        # fails and COMMIT silently degrades to a rollback, losing *every* core
        # table. DuckDB has no SAVEPOINT to scope that, so each handler gets its
        # own transaction instead. The build writes to a private tmp file that
        # is only swapped in at the end, so committing in stages is invisible
        # to readers.
        con.execute("COMMIT")

        # Handler DDL runs before any handler collects, so a handler's tables
        # exist (empty) even when its collect() raises — readers never hit a
        # missing table just because one plugin is broken.
        for handler in list(handlers):
            try:
                # Re-check every statement here, not just at discovery: the
                # created/dropped comparison below cannot see a DELETE or an
                # UPDATE, so a multi-statement string that creates its declared
                # table and then wipes a core one would look perfectly healthy.
                from services.metrics.base import check_schema_statement

                before = _table_names(con)
                con.execute("BEGIN TRANSACTION")
                for statement in handler.SCHEMA:
                    check_schema_statement(statement)
                    con.execute(statement)

                # Verify BEFORE committing, so a violation is rolled back
                # rather than merely reported: the handler must have created
                # exactly the tables it declared and removed none. Anything
                # else means it is writing somewhere it does not own —
                # `CREATE TABLE IF NOT EXISTS x` silently no-ops onto another
                # handler's table (both then append into it), an undeclared
                # CREATE escapes the ownership check, and a DROP takes a core
                # table with it. validate() already refuses non-CREATE
                # statements at discovery; this is the backstop.
                after = _table_names(con)
                created = after - before
                dropped = before - after
                declared = set(handler.TABLES)
                if created != declared or dropped:
                    missing = sorted(declared - created)
                    extra = sorted(created - declared)
                    detail = []
                    if missing:
                        detail.append(f"did not create {', '.join(missing)}")
                    if extra:
                        detail.append(f"created undeclared {', '.join(extra)}")
                    if dropped:
                        detail.append(f"dropped {', '.join(sorted(dropped))}")
                    raise ValueError("; ".join(detail))

                con.execute("COMMIT")
            except Exception as exc:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                handler_errors.append(
                    (handler.NAME, f"schema failed: {type(exc).__name__}: {exc}")
                )
                handlers.remove(handler)

        handler_meta = {}
        status_rows = [
            (name, "failed", False, 0, 0.0, message) for name, message in handler_errors
        ]
        for handler in handlers:
            # A DDL change makes the previous rows' shape untrustworthy, so
            # the handler starts from scratch rather than reading them back at
            # offsets that no longer mean what they did.
            rows_source = (
                (lambda _t: [])
                if previous.get(f"{handler.NAME}.schema_hash") != _schema_hash(handler)
                else (lambda t: carried.get(t) or [])
            )
            ctx = _handler_context(now, src, previous, rows_source, handler)
            # A handler with POLL_INTERVAL_SECONDS collects on its own cadence,
            # not the daemon's. Between runs its rows and meta are copied over
            # verbatim, so readers see no gap and nothing recomputes. If the
            # copy fails we fall through and collect fresh rather than
            # publishing an empty table.
            schema_changed = previous.get(
                f"{handler.NAME}.schema_hash"
            ) != _schema_hash(handler)
            if (
                not force_handlers
                and not schema_changed
                and not _handler_due(handler, now, previous)
            ):
                started = time.monotonic()
                carried_rows = _carry_forward(con, handler, carried)
                if carried_rows is not None:
                    prefix = f"{handler.NAME}."
                    handler_meta.update(
                        {k: v for k, v in previous.items() if k.startswith(prefix)}
                    )
                    status_rows.append(
                        (
                            handler.NAME,
                            "carried",
                            True,
                            carried_rows,
                            round((time.monotonic() - started) * 1000, 1),
                            "",
                        )
                    )
                    continue

            started = time.monotonic()
            # Read the fingerprint BEFORE collecting, for the same reason the
            # store-level one is stamped before load_sources(): `usage.collect`
            # is a multi-second parse, and a transcript written during it would
            # otherwise be recorded as already-seen while its rows are missing —
            # freezing that cycle out until some unrelated source changed.
            fingerprint_fn = getattr(handler, "fingerprint", None)
            handler_fingerprint = None
            if fingerprint_fn is not None:
                try:
                    handler_fingerprint = fingerprint_fn(ctx)
                except Exception:
                    handler_fingerprint = ""
            try:
                # collect() runs outside the transaction: for `usage` that is a
                # multi-second transcript parse, and there is no reason to hold
                # the write lock across it.
                result = handler.collect(ctx)
                tables = getattr(result, "tables", None)
                if tables is None:
                    tables = (result or {}).get("tables", {})
                extra = getattr(result, "meta", None)
                if extra is None:
                    extra = (result or {}).get("meta", {})

                if not isinstance(tables, dict):
                    raise TypeError(
                        f"tables must be a dict, got {type(tables).__name__}"
                    )
                if not isinstance(extra, dict):
                    raise TypeError(f"meta must be a dict, got {type(extra).__name__}")
                unknown = set(tables) - set(handler.TABLES)
                if unknown:
                    raise ValueError(
                        f"returned undeclared table(s): {', '.join(sorted(unknown))}"
                    )
                meta_key_re = _meta_key_re()
                bad_keys = sorted(k for k in extra if not meta_key_re.match(str(k)))
                if bad_keys:
                    # Meta is stored as "<NAME>.<key>"; a key with a dot or a
                    # separator could address another handler's namespace.
                    raise ValueError(
                        f"meta key(s) must match {meta_key_re.pattern}: "
                        f"{', '.join(bad_keys)}"
                    )

                written = 0
                con.execute("BEGIN TRANSACTION")
                for table, rows in tables.items():
                    rows = list(rows or [])
                    _insert(con, table, rows)
                    written += len(rows)
                con.execute("COMMIT")
                for key, value in (extra or {}).items():
                    handler_meta[f"{handler.NAME}.{key}"] = value
                # Stamp the run so the interval and the deferred fingerprint
                # have something to measure from.
                handler_meta[f"{handler.NAME}.collected_at"] = now.isoformat()
                handler_meta[f"{handler.NAME}.schema_hash"] = _schema_hash(handler)
                if handler_fingerprint is not None:
                    handler_meta[f"{handler.NAME}.fingerprint"] = handler_fingerprint
                elapsed_ms = round((time.monotonic() - started) * 1000, 1)
                status_rows.append(
                    (handler.NAME, "collected", True, written, elapsed_ms, "")
                )
            except Exception as exc:
                # Discard whatever this handler managed to write and clear the
                # aborted-transaction state before the next one starts.
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                elapsed_ms = round((time.monotonic() - started) * 1000, 1)
                status_rows.append(
                    (
                        handler.NAME,
                        "failed",
                        False,
                        0,
                        elapsed_ms,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
        status_rows.sort(key=lambda r: r[0])

        con.execute("BEGIN TRANSACTION")
        _insert(con, "metric_handler_status", status_rows)

        meta = {
            "schema_version": SCHEMA_VERSION,
            "built_at": now.isoformat(),
            # status_rows are (name, state, ok, rows, duration_ms, error).
            "handlers_ok": sum(1 for r in status_rows if r[2]),
            "handlers_failed": sum(1 for r in status_rows if not r[2]),
            "handlers_collected": sum(1 for r in status_rows if r[1] == "collected"),
            "handlers_carried": sum(1 for r in status_rows if r[1] == "carried"),
            "source_fingerprint": fingerprint,
            "workspace_mb": workspace_mb,
            "workspace_mb_at": workspace_mb_at,
            "ltm_bytes": ltm_bytes,
            "ltm_bytes_at": ltm_bytes_at,
            "journal_total": len(journal),
            "cycles_total": len(cycles),
            "cycle_velocity_per_hour": _compute_velocity_per_hour(cycles),
            "agent_error_recent_hours": AGENT_ERROR_RECENT_HOURS,
            **balance_meta,
            **velocity_meta,
            **memory_file_meta,
            # Namespaced by handler NAME, so two handlers can both publish a
            # "total" without colliding with each other or with the core.
            **handler_meta,
        }
        _insert(con, "meta", [(k, _jdump(v)) for k, v in meta.items()])
        con.execute("COMMIT")
    finally:
        con.close()


def _stored_fingerprint(path: Path):
    """Read meta.source_fingerprint + schema_version from an existing DB."""
    _require_duckdb()
    try:
        con = duckdb.connect(str(path), read_only=True)
    except Exception:
        return None, None
    try:
        rows = con.execute(
            "SELECT key, value FROM meta WHERE key IN "
            "('source_fingerprint', 'schema_version')"
        ).fetchall()
    except Exception:
        return None, None
    finally:
        con.close()
    values = {k: v for k, v in rows}
    try:
        # Stored via _jdump(), so one json.loads() yields the original values.
        fingerprint = json.loads(values["source_fingerprint"])
        version = json.loads(values["schema_version"])
    except (KeyError, TypeError, ValueError):
        return None, None
    return fingerprint, version


def refresh(force: bool = False) -> bool:
    """Rebuild the database if the sources changed. Returns True when it rebuilt.

    Always builds into `<db>.tmp` and atomically replaces the live file, so
    readers never observe a partial database and the writer never contends with
    them for DuckDB's single-writer lock.
    """
    _require_duckdb()
    dest = db_path()
    now = datetime.now(timezone.utc)
    previous = _previous_meta()
    # Every handler re-collects on a forced rebuild (the operator escape hatch
    # and the portal's cold-start path) and on a schema bump — in both cases a
    # skipped handler would publish empty or stale-shaped tables.
    stored, version = _stored_fingerprint(dest) if dest.exists() else (None, None)
    force_handlers = force or version != SCHEMA_VERSION
    # The fingerprint must be computed under the same flag it will be stored
    # with: deferring a handler's fingerprint here while collecting it fresh in
    # build() would publish a mismatch, and the very next poll would rebuild
    # again for no reason.
    fingerprint = source_fingerprint(now, previous, force_handlers=force_handlers)
    if not force and dest.exists():
        if version == SCHEMA_VERSION and stored == fingerprint:
            return False

    # The tmp name is unique per build: the metrics_daemon poll, the cycle_close
    # dispatch, and two Streamlit threads doing a cold-start build can all
    # overlap, and builders sharing one tmp path would corrupt each other's
    # file. Each builds its own, then swaps; the last swap wins and every
    # candidate is complete.
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        build(tmp, now=now, fingerprint=fingerprint, force_handlers=force_handlers)
        os.replace(tmp, dest)
    finally:
        # A failed build must leave the previous database untouched. DuckDB
        # writes a sibling .wal, so clear that too or a later build inherits it.
        for stray in (tmp, tmp.with_name(tmp.name + ".wal")):
            try:
                stray.unlink()
            except OSError:
                pass
    return True


def is_ready(path: Path | None = None) -> bool:
    """True when the database exists and carries the current schema version."""
    target = Path(path) if path else db_path()
    if not target.exists() or target.stat().st_size == 0:
        return False
    _, version = _stored_fingerprint(target)
    return version == SCHEMA_VERSION


def stats(path: Path | None = None) -> dict:
    """Row counts per table plus the meta block."""
    _require_duckdb()
    target = Path(path) if path else db_path()
    if not target.exists():
        return {"db": str(target), "exists": False, "tables": {}, "meta": {}}
    con = duckdb.connect(str(target), read_only=True)
    try:
        counts = {}
        for table in all_tables():
            try:
                counts[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[
                    0
                ]
            except Exception:
                counts[table] = None
        try:
            meta_rows = con.execute("SELECT key, value FROM meta").fetchall()
            meta = {k: json.loads(v) for k, v in meta_rows}
        except Exception:
            meta = {}
    finally:
        con.close()
    return {"db": str(target), "exists": True, "tables": counts, "meta": meta}


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    global DB_PATH

    args = list(sys.argv[1:] if argv is None else argv)
    if "--help" in args or "-h" in args:
        print(__doc__.strip())
        return 0

    if "--db" in args:
        idx = args.index("--db")
        if idx + 1 < len(args):
            DB_PATH = Path(args[idx + 1])

    if "--stats" in args or "--json" in args:
        data = stats()
        if "--json" in args:
            print(json.dumps(data, indent=2, default=str))
            return 0
        print(f"db: {data['db']}  exists={data['exists']}")
        for table, count in data["tables"].items():
            print(f"  {table:<32} {count if count is not None else '-':>8}")
        meta = data.get("meta") or {}
        if meta.get("built_at"):
            print(f"  built_at: {meta['built_at']}")
        return 0

    rebuilt = refresh(force="--rebuild" in args)
    print(f"{'rebuilt' if rebuilt else 'up-to-date'}: {db_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
