"""Token-usage metrics parsed from cycle transcripts.

Source: ``/agent/memory/transcripts/cycle-<N>.jsonl`` (and the ``.jsonl.gz``
form heartbeat.sh compresses them into after 7 days). Every assistant record
carries the API response's ``message.usage`` block::

    {"input_tokens": 2, "cache_creation_input_tokens": 515,
     "cache_read_input_tokens": 104810, "output_tokens": 77,
     "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
     "service_tier": "standard",
     "cache_creation": {"ephemeral_1h_input_tokens": 0,
                        "ephemeral_5m_input_tokens": 515},
     "inference_geo": "not_available", "iterations": [], "speed": "standard"}

Two levels of deduplication
---------------------------
1. *Within* a transcript: one API response is written once per content block
   (thinking, text, tool_use), each repeating the **same** usage object. In the
   sample transcripts that is 22 records for 12 real requests — summing raw
   inflates output tokens by ~1.8x.

2. *Across* transcripts: heartbeat.sh resumes the session of an in-progress
   goal (``-r $SESSION_ID``, heartbeat.sh:483) and then copies the **whole**
   session file to ``cycle-<N>.jsonl`` at every cycle end (heartbeat.sh:561).
   A goal spanning k cycles therefore produces k transcripts where each is a
   superset of the last, and a per-file count would report every early request
   k times.

So ``message.id`` is deduplicated globally, oldest cycle first: a request is
attributed to the first cycle whose transcript contains it — the cycle it
actually ran in. ``metric_usage_requests`` holds each request exactly once.

Incrementality
--------------
A finished cycle's transcript never changes, so per-file ``(fname, size,
mtime)`` identity carries its already-attributed rows forward instead of
re-reading it. Steady state parses only the transcript of the cycle that just
ran. What that key does *not* protect against:

* A transcript still being copied while it is parsed — the partial parse is
  recorded against the pre-copy stat, so the next build sees a new size and
  re-parses. Self-correcting within one poll.
* Cycles aging out of the window: their requests leave ``seen``, so a resumed
  session's older requests get re-attributed to the new oldest cycle. Totals
  stay correct (still counted once); only the cycle attribution shifts at the
  boundary. ``metric_usage_daily`` rows for fully aged-out days are carried
  forward so long-range daily history survives a modest cycle window.

Config (env):
    METRICS_USAGE_CYCLES   how many recent transcripts to keep (default 200)
    METRICS_TRANSCRIPT_DIR override the transcript directory
"""

from __future__ import annotations

import gzip
import json
import os
import re
from pathlib import Path

from services.metrics.base import HandlerResult

NAME = "usage"

TABLES = [
    "metric_usage_files",
    "metric_usage_requests",
    "metric_usage_cycles",
    "metric_usage_daily",
]

SCHEMA = [
    # Per-transcript identity — the incremental cache key.
    """
    CREATE TABLE metric_usage_files (
        cycle INTEGER, fname VARCHAR, size BIGINT, mtime DOUBLE,
        requests INTEGER, first_ts VARCHAR, last_ts VARCHAR
    )
    """,
    # One row per distinct API response, attributed to the first cycle whose
    # transcript contained it. This is the carry-forward unit and the grain
    # every rollup below is derived from.
    """
    CREATE TABLE metric_usage_requests (
        cycle INTEGER, message_id VARCHAR, ts VARCHAR, day VARCHAR,
        model VARCHAR, effort VARCHAR, service_tier VARCHAR, speed VARCHAR,
        input_tokens BIGINT, cache_creation_input_tokens BIGINT,
        cache_read_input_tokens BIGINT, output_tokens BIGINT,
        ephemeral_1h_input_tokens BIGINT, ephemeral_5m_input_tokens BIGINT,
        web_search_requests BIGINT, web_fetch_requests BIGINT
    )
    """,
    # Rollup: one row per (cycle, model, effort, service_tier, speed).
    """
    CREATE TABLE metric_usage_cycles (
        cycle INTEGER, day VARCHAR, model VARCHAR, effort VARCHAR,
        service_tier VARCHAR, speed VARCHAR, requests INTEGER,
        input_tokens BIGINT, cache_creation_input_tokens BIGINT,
        cache_read_input_tokens BIGINT, output_tokens BIGINT,
        ephemeral_1h_input_tokens BIGINT, ephemeral_5m_input_tokens BIGINT,
        web_search_requests BIGINT, web_fetch_requests BIGINT,
        first_ts VARCHAR, last_ts VARCHAR
    )
    """,
    # Rollup: one row per day. Days whose cycles have aged out of the window
    # are carried forward rather than recomputed, so history outlives the cap.
    """
    CREATE TABLE metric_usage_daily (
        day VARCHAR, cycles INTEGER, requests INTEGER,
        input_tokens BIGINT, cache_creation_input_tokens BIGINT,
        cache_read_input_tokens BIGINT, output_tokens BIGINT,
        total_tokens BIGINT, web_search_requests BIGINT,
        web_fetch_requests BIGINT
    )
    """,
]

DEFAULT_RECENT_CYCLES = 200
CYCLE_RE = re.compile(r"^cycle-(\d+)\.jsonl(\.gz)?$")

# Token counters summed by every rollup.
_TOKEN_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "ephemeral_1h_input_tokens",
    "ephemeral_5m_input_tokens",
    "web_search_requests",
    "web_fetch_requests",
)

# Column order of metric_usage_requests, so rows read back from the previous
# build can be indexed by name instead of by magic offset.
_REQUEST_COLS = [
    "cycle",
    "message_id",
    "ts",
    "day",
    "model",
    "effort",
    "service_tier",
    "speed",
    *_TOKEN_FIELDS,
]
_IDX = {name: i for i, name in enumerate(_REQUEST_COLS)}

_BILLED_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


# ─── source discovery ─────────────────────────────────────────────────────────


def transcript_dir(ctx) -> Path:
    override = os.environ.get("METRICS_TRANSCRIPT_DIR")
    if override:
        return Path(override)
    return ctx.memory_dir / "transcripts"


def _recent_cycles() -> int:
    raw = os.environ.get("METRICS_USAGE_CYCLES")
    try:
        n = int(raw) if raw else DEFAULT_RECENT_CYCLES
    except ValueError:
        return DEFAULT_RECENT_CYCLES
    return max(1, n)


def transcript_files(ctx) -> list:
    """The N most recent transcripts as (cycle, path, size, mtime), oldest first.

    At most one file per cycle: `gzip` writes `cycle-N.jsonl.gz` before removing
    `cycle-N.jsonl`, so a crash mid-compress can leave both. Counting both would
    double that cycle's tokens, so the uncompressed copy wins.
    """
    directory = transcript_dir(ctx)
    best: dict = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    for name in names:
        match = CYCLE_RE.match(name)
        if not match:
            continue
        path = directory / name
        try:
            st = os.stat(path)
        except OSError:
            continue
        cycle = int(match.group(1))
        is_gz = bool(match.group(2))
        current = best.get(cycle)
        if current is None or (current[1] and not is_gz):
            best[cycle] = ((cycle, path, st.st_size, st.st_mtime), is_gz)

    found = [entry for entry, _is_gz in best.values()]
    # Newest cycles win the cap, but return chronological: attribution depends
    # on processing oldest-first.
    found.sort(key=lambda item: item[0], reverse=True)
    return sorted(found[: _recent_cycles()], key=lambda item: item[0])


def fingerprint(ctx) -> str:
    """Identity of every transcript we would read. Cheap: one stat() each."""
    return json.dumps(
        [
            [cycle, path.name, size, mtime]
            for cycle, path, size, mtime in transcript_files(ctx)
        ]
    )


# ─── parsing ──────────────────────────────────────────────────────────────────


def _open_transcript(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def _int(value) -> int:
    """Coerce a token counter to int. Missing/`null`/garbage all mean zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _usage_records(path: Path):
    """Yield one (key, entry, usage) per assistant record carrying usage."""
    try:
        handle = _open_transcript(path)
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # a truncated tail line — skip, keep the rest
            if not isinstance(entry, dict):
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            # message.id identifies the API response; requestId and uuid are
            # fallbacks for older record shapes.
            key = message.get("id") or entry.get("requestId") or entry.get("uuid")
            yield key, entry, usage


def parse_transcript(cycle: int, path: Path, seen=None) -> list:
    """Parse one transcript into metric_usage_requests rows.

    *seen* is the set of message ids already attributed to an earlier cycle;
    matching records are skipped (and the set is updated in place) so a resumed
    session's repeated history is counted once, against its first cycle.
    """
    seen = seen if seen is not None else set()
    rows = []

    for key, entry, usage in _usage_records(path):
        if key is not None:
            if key in seen:
                continue
            seen.add(key)

        ts = entry.get("timestamp")
        ts = ts if isinstance(ts, str) else ""
        message = entry.get("message") or {}

        counters = {field: 0 for field in _TOKEN_FIELDS}
        for field in _BILLED_FIELDS:
            counters[field] = _int(usage.get(field))
        cache_creation = usage.get("cache_creation")
        if isinstance(cache_creation, dict):
            counters["ephemeral_1h_input_tokens"] = _int(
                cache_creation.get("ephemeral_1h_input_tokens")
            )
            counters["ephemeral_5m_input_tokens"] = _int(
                cache_creation.get("ephemeral_5m_input_tokens")
            )
        server_tool_use = usage.get("server_tool_use")
        if isinstance(server_tool_use, dict):
            counters["web_search_requests"] = _int(
                server_tool_use.get("web_search_requests")
            )
            counters["web_fetch_requests"] = _int(
                server_tool_use.get("web_fetch_requests")
            )

        rows.append(
            (
                cycle,
                str(key or ""),
                ts,
                ts[:10],
                str(message.get("model") or ""),
                str(entry.get("effort") or ""),
                str(usage.get("service_tier") or ""),
                str(usage.get("speed") or ""),
                *(counters[field] for field in _TOKEN_FIELDS),
            )
        )
    return rows


# ─── rollups ──────────────────────────────────────────────────────────────────


def _cycle_rollup(request_rows: list) -> list:
    """One row per (cycle, model, effort, service_tier, speed)."""
    grains: dict = {}
    for row in request_rows:
        key = tuple(
            row[_IDX[c]] for c in ("cycle", "model", "effort", "service_tier", "speed")
        )
        bucket = grains.setdefault(
            key,
            {
                "day": row[_IDX["day"]],
                "requests": 0,
                "first_ts": "",
                "last_ts": "",
                **{f: 0 for f in _TOKEN_FIELDS},
            },
        )
        bucket["requests"] += 1
        ts = row[_IDX["ts"]]
        if ts:
            if not bucket["first_ts"] or ts < bucket["first_ts"]:
                bucket["first_ts"] = ts
                bucket["day"] = ts[:10]
            if ts > bucket["last_ts"]:
                bucket["last_ts"] = ts
        for field in _TOKEN_FIELDS:
            bucket[field] += row[_IDX[field]] or 0

    rows = []
    for (cycle, model, effort, tier, speed), bucket in sorted(grains.items()):
        rows.append(
            (
                cycle,
                bucket["day"],
                model,
                effort,
                tier,
                speed,
                bucket["requests"],
                *(bucket[f] for f in _TOKEN_FIELDS),
                bucket["first_ts"],
                bucket["last_ts"],
            )
        )
    return rows


def _daily_rollup(request_rows: list) -> dict:
    """One row per day, keyed by day so carried-forward days can be merged."""
    by_day: dict = {}
    for row in request_rows:
        day = row[_IDX["day"]]
        bucket = by_day.setdefault(
            day,
            {"cycles": set(), "requests": 0, **{f: 0 for f in _TOKEN_FIELDS}},
        )
        bucket["cycles"].add(row[_IDX["cycle"]])
        bucket["requests"] += 1
        for field in _TOKEN_FIELDS:
            bucket[field] += row[_IDX[field]] or 0

    out = {}
    for day, bucket in by_day.items():
        out[day] = (
            day,
            len(bucket["cycles"]),
            bucket["requests"],
            bucket["input_tokens"],
            bucket["cache_creation_input_tokens"],
            bucket["cache_read_input_tokens"],
            bucket["output_tokens"],
            sum(bucket[f] for f in _BILLED_FIELDS),
            bucket["web_search_requests"],
            bucket["web_fetch_requests"],
        )
    return out


# ─── collection ───────────────────────────────────────────────────────────────


def _carried(ctx):
    """Previous build's file identities and request rows, indexed by cycle."""
    files = {}
    for row in ctx.previous_rows("metric_usage_files"):
        try:
            files[row[0]] = (row[1], row[2], row[3])  # cycle -> (fname, size, mtime)
        except (IndexError, TypeError):
            continue
    requests: dict = {}
    for row in ctx.previous_rows("metric_usage_requests"):
        try:
            requests.setdefault(row[0], []).append(tuple(row))
        except (IndexError, TypeError):
            continue
    daily = {}
    for row in ctx.previous_rows("metric_usage_daily"):
        try:
            daily[row[0]] = tuple(row)
        except (IndexError, TypeError):
            continue
    return files, requests, daily


def collect(ctx) -> HandlerResult:
    files = transcript_files(ctx)
    previous_files, previous_requests, previous_daily = _carried(ctx)

    seen = set()
    file_rows = []
    request_rows = []
    parsed = 0
    reused = 0

    # Oldest first: a request belongs to the first cycle that recorded it.
    for cycle, path, size, mtime in files:
        identity = (path.name, size, mtime)
        if previous_files.get(cycle) == identity and cycle in previous_requests:
            rows = previous_requests[cycle]
            # Carried rows were already deduped against earlier cycles, but
            # their ids still have to enter `seen` for the cycles after them.
            seen.update(r[_IDX["message_id"]] for r in rows if r[_IDX["message_id"]])
            reused += 1
        else:
            rows = parse_transcript(cycle, path, seen)
            parsed += 1

        request_rows.extend(rows)
        stamps = [r[_IDX["ts"]] for r in rows if r[_IDX["ts"]]]
        file_rows.append(
            (
                cycle,
                path.name,
                size,
                mtime,
                len(rows),
                min(stamps) if stamps else "",
                max(stamps) if stamps else "",
            )
        )

    cycle_rows = _cycle_rollup(request_rows)
    daily = _daily_rollup(request_rows)

    # Days older than the window are frozen from the previous build so daily
    # history is not lost when their cycles age out.
    oldest_day = min(daily) if daily else None
    for day, row in previous_daily.items():
        if day not in daily and (oldest_day is None or day < oldest_day):
            daily[day] = row
    daily_rows = [daily[day] for day in sorted(daily, reverse=True)]

    totals = {field: 0 for field in _TOKEN_FIELDS}
    for row in request_rows:
        for field in _TOKEN_FIELDS:
            totals[field] += row[_IDX[field]] or 0

    return HandlerResult(
        tables={
            "metric_usage_files": file_rows,
            "metric_usage_requests": request_rows,
            "metric_usage_cycles": cycle_rows,
            "metric_usage_daily": daily_rows,
        },
        meta={
            "transcripts": len(file_rows),
            "transcripts_parsed": parsed,
            "transcripts_reused": reused,
            "requests": len(request_rows),
            "total_tokens": sum(totals[f] for f in _BILLED_FIELDS),
            "models": sorted(
                {r[_IDX["model"]] for r in request_rows if r[_IDX["model"]]}
            ),
            **totals,
        },
    )
