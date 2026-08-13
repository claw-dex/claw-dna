"""
Metrics loaders — read pre-computed Overview metrics from the local DuckDB file.

Every value here is materialised by ``scripts/metrics_db.py`` at collection time
(the metrics_daemon service, plus a hook in scripts/cycle_close.py). This module
only runs ``SELECT * FROM metric_*`` — it never aggregates, joins, or parses a
JSON source file, so the Overview tab does no metric computation at render time.

Concurrency: the collector builds into ``<db>.tmp`` and atomically replaces the
live file, so the portal opens the database read-only and re-opens whenever the
file's mtime changes.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

from app.data._cache import _register_cache

try:
    import duckdb
except ImportError:  # pragma: no cover - duckdb ships with the container runtime
    duckdb = None


# Deliberately NOT registered with _register_cache(): _cache_clear_all() only
# calls .clear(), which would drop the connection without closing it. Because
# the collector replaces the database file rather than updating it in place,
# every orphaned handle would pin a deleted inode. Invalidation is mtime-based
# anyway, so the cache never needs an external clear — _acquire() closes the old
# handle itself when the file is swapped.
_CONN_CACHE: dict = {}  # {"conn": (connection, mtime)}
_QUERY_CACHE = _register_cache()  # {cache_key: (result, mtime)}
_CONN_LOCK = threading.Lock()

# Cold-start build state, guarded by _CONN_LOCK. Kept out of the cache registry
# so a portal write (which calls _cache_clear_all()) can't re-trigger a build
# that is failing persistently; a time-based backoff handles retries instead.
_BUILD_STATE: dict = {}  # {"attempted_at": monotonic float}
_BUILD_RETRY_SECONDS = 300


def _metrics_db():
    """Import the collector lazily — it is a script, not part of the app package.

    Returns None if it cannot be imported, so a broken deployment degrades to an
    empty Overview rather than raising out of a Streamlit render.
    """
    try:
        from scripts import metrics_db

        return metrics_db
    except ImportError:
        pass
    # Fall back to a bare import: scripts/ is on sys.path under pytest, and the
    # `scripts` package name can be shadowed there by test/scripts/.
    scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        import metrics_db

        return metrics_db
    except ImportError:
        return None


def _db_path():
    metrics_db = _metrics_db()
    return str(metrics_db.db_path()) if metrics_db else None


def _db_mtime():
    path = _db_path()
    if not path:
        return 0.0
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _ensure_db():
    """Cold start: build the database if it is missing or has a stale schema.

    Caller must hold _CONN_LOCK: this runs the full ETL, and two Streamlit
    threads entering together would have the collector build over itself.
    A persistently failing build is retried at most every _BUILD_RETRY_SECONDS.
    """
    metrics_db = _metrics_db()
    if metrics_db is None:
        return False
    try:
        if metrics_db.is_ready():
            return True
    except Exception:
        return False

    last = _BUILD_STATE.get("attempted_at")
    if last is not None and (time.monotonic() - last) < _BUILD_RETRY_SECONDS:
        return False
    _BUILD_STATE["attempted_at"] = time.monotonic()
    try:
        metrics_db.refresh(force=True)
        return metrics_db.is_ready()
    except Exception:
        return False


def _acquire():
    """Return (connection, mtime), or (None, 0.0). Caller must hold _CONN_LOCK.

    Re-opens whenever the collector swaps a new file in, closing the previous
    handle. Closing only happens here, under the lock, so a query in flight can
    never have its connection pulled out from under it.
    """
    if duckdb is None:
        return None, 0.0
    mtime = _db_mtime()
    if mtime == 0.0:
        if not _ensure_db():
            return None, 0.0
        mtime = _db_mtime()

    cached = _CONN_CACHE.get("conn")
    if cached is not None:
        conn, cached_mtime = cached
        if cached_mtime == mtime:
            return conn, mtime
        try:
            conn.close()
        except Exception:
            pass
        _CONN_CACHE.pop("conn", None)

    path = _db_path()
    if not path:
        return None, 0.0
    try:
        conn = duckdb.connect(path, read_only=True)
    except Exception:
        return None, 0.0
    _CONN_CACHE["conn"] = (conn, mtime)
    return conn, mtime


def _conn():
    """Read-only connection, or None. Convenience wrapper around _acquire()."""
    with _CONN_LOCK:
        return _acquire()[0]


def _close_conn():
    """Close and forget the cached connection, and reset the cold-start backoff.

    Not needed during normal operation — _acquire() closes the old handle when
    the collector swaps a new file in. Exists so tests (and any future shutdown
    hook) can release the handle deterministically.
    """
    with _CONN_LOCK:
        cached = _CONN_CACHE.pop("conn", None)
        if cached is not None:
            try:
                cached[0].close()
            except Exception:
                pass
        _BUILD_STATE.clear()


def _query(key, sql, params=(), default=None):
    """Run a SELECT, cached against the database file's mtime.

    The whole sequence — resolve the connection, check the cache, execute —
    runs under _CONN_LOCK. Holding it throughout is what makes the result
    consistent with the connection it came from: the collector swaps the file
    out from under the portal while several Streamlit threads render, and doing
    any of these steps outside the lock lets one thread execute on a connection
    another has just closed (which would silently render an empty dashboard).

    Any failure (missing file, half-built database, dropped table) yields the
    caller's default so a tab renders an empty state instead of crashing.
    """
    cache_key = (key, params)
    with _CONN_LOCK:
        # Resolve the connection first: on a cold start this builds the file,
        # which is what determines the mtime the result is cached under.
        conn, mtime = _acquire()
        if conn is None:
            return default

        cached = _QUERY_CACHE.get(cache_key)
        if cached is not None:
            result, cached_mtime = cached
            if cached_mtime == mtime:
                return result

        try:
            cursor = conn.execute(sql, list(params))
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
        except Exception:
            return default

        result = [dict(zip(columns, row)) for row in rows]
        _QUERY_CACHE[cache_key] = (result, mtime)
        return result


def _loads(value, default):
    """Decode a JSON column, falling back to `default`."""
    if value is None:
        return default
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return default
    return decoded if decoded is not None else default


def load_meta():
    """The `meta` table as a decoded dict of scalars."""
    rows = _query("meta", "SELECT key, value FROM meta", default=[])
    return {r["key"]: _loads(r["value"], None) for r in rows or []}


def is_available():
    """True when the metrics store can be read.

    Callers that would otherwise render an empty result as a fact ("no memory
    files", "no errors") should check this first — an unreadable store and a
    genuinely empty one produce the same empty rows.
    """
    return bool(load_meta().get("built_at"))


def built_at():
    """ISO timestamp of the last successful collection, or None."""
    return load_meta().get("built_at")


# ── Health strip ──────────────────────────────────────────────────────────────

_HEALTH_DEFAULT = {
    "agent_status": "unknown",
    "last_heartbeat": "—",
    "cycle_number": 0,
    "active_goals": 0,
    "errors_24h": 0,
    "error_tabs": [],
    "inbox_count": 0,
}


def load_health():
    """Agent status, heartbeat, cycle number, active goals, 24h errors, inbox size."""
    rows = _query("health", "SELECT * FROM metric_health", default=[])
    if not rows:
        return dict(_HEALTH_DEFAULT)
    row = dict(rows[0])
    row["error_tabs"] = _loads(row.get("error_tabs"), [])
    row["agent_status"] = row.get("agent_status") or "unknown"
    row["last_heartbeat"] = row.get("last_heartbeat") or "—"
    return row


# ── Today at a Glance ─────────────────────────────────────────────────────────

_DAY_DEFAULT = {
    "cycles_total": 0,
    "cycles_completed": 0,
    "cycles_failed": 0,
    "active_seconds": 0.0,
    "goal_cycles": 0,
    "evolve_cycles": 0,
    "category_count": 0,
    "categories": {},
    "types": {},
}


def _day_row(day):
    rows = _query(
        "daily", "SELECT * FROM metric_daily WHERE day = ?", (day,), default=[]
    )
    if not rows:
        return dict(_DAY_DEFAULT)
    row = dict(rows[0])
    row["categories"] = _loads(row.get("categories"), {})
    row["types"] = _loads(row.get("types"), {})
    return row


def load_day_glance(day, prev_day):
    """One day's rollup plus the previous day's completed count for the delta."""
    today = _day_row(day)
    yesterday = _day_row(prev_day)
    timeline = (
        _query(
            "timeline",
            "SELECT * FROM metric_day_timeline WHERE day = ? ORDER BY start_ts",
            (day,),
            default=[],
        )
        or []
    )
    return {
        **today,
        "day": day,
        "prev_completed": yesterday.get("cycles_completed", 0),
        "timeline": [dict(r) for r in timeline],
    }


# ── Suggestions ───────────────────────────────────────────────────────────────


def load_suggestions():
    """Ranked action suggestions, highest priority first."""
    rows = _query(
        "suggestions", "SELECT * FROM metric_suggestions ORDER BY rank", default=[]
    )
    return [dict(r) for r in rows or []]


# ── Evolution balance ─────────────────────────────────────────────────────────


def load_balance_metrics():
    """Per-category counts, dynamic weight components, and the suggested focus."""
    rows = _query("balance", "SELECT * FROM metric_balance", default=[]) or []
    meta = load_meta()
    return {
        "rows": [dict(r) for r in rows],
        "total_evolve_cycles": meta.get("balance_total_evolve_cycles") or 0,
        "suggestion": meta.get("balance_suggestion"),
        "has_weights": bool(meta.get("balance_has_weights")),
        "max_score": meta.get("balance_max_score") or 1.0,
        "goal_signals": meta.get("balance_goal_signals") or [],
    }


# ── Goal performance ──────────────────────────────────────────────────────────

_GOAL_DEFAULT = {
    "total": 0,
    "completed": 0,
    "failed": 0,
    "completion_rate": 0.0,
    "avg_goal_duration_seconds": None,
    "delegated_total": 0,
    "delegated_awaiting": 0,
    "delegated_completed": 0,
    "delegated_failed": 0,
    "delegated_by_agent": {},
}


def load_goal_metrics():
    """Goal totals, completion rate, duration sparkline rows, and recent goals."""
    rows = _query("goal_stats", "SELECT * FROM metric_goal_stats", default=[])
    stats = dict(rows[0]) if rows else dict(_GOAL_DEFAULT)
    stats["delegated_by_agent"] = _loads(stats.get("delegated_by_agent"), {})

    durations = (
        _query(
            "goal_durations",
            "SELECT cycle_number, duration_seconds FROM metric_goal_cycle_durations "
            "ORDER BY rank",
            default=[],
        )
        or []
    )
    stats["goal_cycle_durations"] = [
        (r["cycle_number"], r["duration_seconds"]) for r in durations
    ]

    recent = (
        _query(
            "recent_goals",
            "SELECT content, status, created_at FROM metric_recent_goals ORDER BY rank",
            default=[],
        )
        or []
    )
    stats["recent_goals"] = [dict(r) for r in recent]
    return stats


# ── Cycle velocity ────────────────────────────────────────────────────────────


def load_velocity_metrics():
    """Last N completed cycles (chronological) plus the pre-computed averages."""
    rows = (
        _query("velocity", "SELECT * FROM metric_velocity ORDER BY rank", default=[])
        or []
    )
    meta = load_meta()
    return {
        "rows": [dict(r) for r in rows],
        "count": meta.get("velocity_count") or len(rows),
        "avg_all": meta.get("velocity_avg_all"),
        "avg_evolve": meta.get("velocity_avg_evolve"),
        "avg_goal": meta.get("velocity_avg_goal"),
    }


# ── Recent improvements ───────────────────────────────────────────────────────


def load_improvements(limit=10):
    """Completed evolve/goal cycles, newest first, with journal-enriched summaries."""
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 10
    rows = (
        _query(
            "improvements",
            "SELECT * FROM metric_improvements ORDER BY rank LIMIT ?",
            (limit,),
            default=[],
        )
        or []
    )
    result = []
    for r in rows:
        row = dict(r)
        row["actions"] = _loads(row.get("actions"), [])
        result.append(row)
    return result


# ── Portal header ─────────────────────────────────────────────────────────────


def load_cycle_velocity_metric():
    """Cycles per hour over the last 10 completed cycles, or None.

    The header fragment re-renders every 60s for every connected user; deriving
    this used to merge cycles.json + cycles_archive.json on each tick.
    """
    return load_meta().get("cycle_velocity_per_hour")


def load_workspace_mb():
    """Workspace size in MB, or None. Replaces an os.walk on the render path."""
    return load_meta().get("workspace_mb")


# ── Memory overview ───────────────────────────────────────────────────────────

_MEMORY_OVERVIEW_DEFAULT = {
    "ltm_bytes": 0,
    "journal_active": 0,
    "journal_archived": 0,
    "cycles_active": 0,
    "cycles_archived": 0,
    "inbox_history": 0,
    "outbox_history": 0,
    "inbox_history_bytes": 0,
    "outbox_history_bytes": 0,
}


def load_memory_overview():
    """Entry counts and store sizes for the Memory tab's overview strip."""
    rows = _query("memory_overview", "SELECT * FROM metric_memory_overview", default=[])
    if not rows:
        return dict(_MEMORY_OVERVIEW_DEFAULT)
    return dict(rows[0])


# ── Memory-file health ────────────────────────────────────────────────────────


def load_memory_file_health():
    """Per-file size/age/health for every *.json in the memory dir, plus totals.

    Named to avoid colliding with app.data.memory.load_memory_files, which lists
    files for browsing rather than scoring them.
    """
    rows = (
        _query(
            "memory_files",
            "SELECT * FROM metric_memory_files ORDER BY fname",
            default=[],
        )
        or []
    )
    meta = load_meta()
    return {
        "rows": [dict(r) for r in rows],
        "total": meta.get("memory_files_total") or len(rows),
        "total_kb": meta.get("memory_files_total_kb") or 0.0,
        "ok": meta.get("memory_files_ok") or 0,
        "warn": meta.get("memory_files_warn") or 0,
        "crit": meta.get("memory_files_crit") or 0,
        # Distinguishes "the memory dir is empty" from "the store is unreadable".
        "available": bool(meta.get("built_at")),
        "built_at": meta.get("built_at"),
    }


# ── Per-agent error metrics ───────────────────────────────────────────────────


def load_agent_error_metrics(agent_name):
    """Error totals for one agent: all-time, recent-window, and last timestamp."""
    rows = (
        _query(
            "agent_errors",
            "SELECT * FROM metric_agent_errors WHERE agent = ?",
            (agent_name or "",),
            default=[],
        )
        or []
    )
    recent_hours = load_meta().get("agent_error_recent_hours") or 24
    if not rows:
        # `found` lets the caller tell "this agent has no errors" apart from
        # "the store has no row for it" (unreadable, or built before the agent
        # was registered) and fall back to counting the live list instead.
        return {
            "agent": agent_name,
            "total": 0,
            "recent_count": 0,
            "last_error_ts": "",
            "recent_hours": recent_hours,
            "found": False,
        }
    row = dict(rows[0])
    row["recent_hours"] = recent_hours
    row["found"] = True
    return row


# ── Pluggable handlers (services/metrics/*.py) ────────────────────────────────


def load_handler_status():
    """One row per discovered metrics handler: ok, rows written, duration, error.

    Surfaces a handler that failed to import or raised during collection —
    without this a broken plugin looks identical to "this metric has no data".
    """
    rows = _query(
        "handler_status",
        "SELECT * FROM metric_handler_status ORDER BY name",
        default=[],
    )
    return [dict(r) for r in rows or []]


def load_handler_meta(name):
    """Meta values a handler published, with its `<name>.` prefix stripped."""
    prefix = f"{name}."
    return {k[len(prefix) :]: v for k, v in load_meta().items() if k.startswith(prefix)}


# ── Token usage (services/metrics/usage.py) ───────────────────────────────────


def load_usage_totals():
    """Aggregate token usage across every transcript the handler covers."""
    meta = load_handler_meta("usage")
    return {
        "transcripts": meta.get("transcripts") or 0,
        "requests": meta.get("requests") or 0,
        "total_tokens": meta.get("total_tokens") or 0,
        "input_tokens": meta.get("input_tokens") or 0,
        "cache_creation_input_tokens": meta.get("cache_creation_input_tokens") or 0,
        "cache_read_input_tokens": meta.get("cache_read_input_tokens") or 0,
        "output_tokens": meta.get("output_tokens") or 0,
        "web_search_requests": meta.get("web_search_requests") or 0,
        "web_fetch_requests": meta.get("web_fetch_requests") or 0,
        "models": meta.get("models") or [],
        "available": bool(meta),
    }


def load_usage_daily(limit=14):
    """Per-day token totals, newest first."""
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 14
    rows = _query(
        "usage_daily",
        "SELECT * FROM metric_usage_daily ORDER BY day DESC LIMIT ?",
        (limit,),
        default=[],
    )
    return [dict(r) for r in rows or []]


def load_usage_cycles(limit=20):
    """Per-cycle token totals, newest cycle first."""
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 20
    rows = _query(
        "usage_cycles",
        "SELECT * FROM metric_usage_cycles ORDER BY cycle DESC LIMIT ?",
        (limit,),
        default=[],
    )
    return [dict(r) for r in rows or []]
