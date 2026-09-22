---
name: metrics-daemon-handler
description: Write a new metrics collection handler for the metrics daemon — a drop-in Python module under services/metrics/ that contributes its own DuckDB tables to the shared metrics store. Use when asked to "track", "measure", "collect metrics for", or "add a metric" for anything the portal does not already surface (token spend, API latency, third-party stats, disk growth, anything time-series), or when a dashboard needs a number that would otherwise be computed at render time. Also covers debugging an existing handler that shows no data, reports `failed` or `carried` in metric_handler_status, or is not picking up source changes.
---

# metrics-daemon-handler

Add a metrics collector by dropping a `.py` file into `services/metrics/`. The
next build picks it up — there is no registration list to edit.

**Contract:** `services/metrics/base.py`
**Worked example:** `services/metrics/usage.py`
**Collector:** `scripts/metrics_db.py` · **Daemon:** `services/metrics_daemon.py`

## When to write a handler

Write one when a number needs collecting on a schedule and reading cheaply. The
portal must never compute a metric at render time — that rule is why this
plugin system exists. If the value is *state* rather than a metric (agent
status, heartbeat, current goal, service liveness, queue depth, raw log
content), read it live from JSON instead; do not add a handler.

## The contract

```python
"""One-line description of what this measures and from where."""

from services.metrics.base import HandlerResult

NAME   = "mymetric"                                       # unique; namespaces meta keys
TABLES = ["metric_mymetric"]                              # every table SCHEMA creates
SCHEMA = ["CREATE TABLE metric_mymetric (day VARCHAR, n BIGINT)"]

POLL_INTERVAL_SECONDS = 900        # optional — collect at most this often

def fingerprint(ctx):              # optional — skip the rebuild when nothing moved
    return str(my_source_mtime)

def collect(ctx):
    return HandlerResult(
        tables={"metric_mymetric": [("2026-08-13", 42)]},   # tuples, DDL column order
        meta={"total": 42},                                 # stored as "mymetric.total"
    )
```

`ctx` is a `HandlerContext`: `agent_dir`, `memory_dir`, `messages_dir`, the
build's `now` (always use this, never `datetime.now()`, so builds are
reproducible in tests), `sources` (core payloads the collector already
decoded — read these rather than re-opening the JSON), `previous_meta`, and
`previous_rows(table)`.

## What the collector guarantees

- **It owns the database.** Your handler never opens DuckDB. The collector
  holds the single write connection and does the atomic build-and-swap.
- **Your tables always exist.** DDL runs before any handler collects, so a
  reader never hits a missing table because your `collect` raised.
- **Failure is isolated.** Each handler runs in its own transaction. A raising
  handler leaves its tables empty, is recorded in `metric_handler_status`, and
  cannot affect the core metrics or another handler. (This matters more than it
  sounds: DuckDB aborts a whole transaction on a runtime error, so a handler
  returning a string for an `INTEGER` column used to lose *every* core table.)
- **Every table has exactly one owner**, so one feature's metrics can never be
  overwritten or polluted by another's. See "Isolation rules" below.
- **Nothing in `ctx` is shared.** Each handler gets its own context: `sources`
  is a read-only view over copied containers, `previous_meta` is a copy, and
  `previous_rows` hands back fresh tuples for *your* tables only. You cannot
  reach another handler's data, and it cannot reach yours.
- **Installing, removing, or editing a handler always rebuilds.** The roster
  (names, declared tables, and a hash of your `SCHEMA`) is part of the store's
  refresh key, so your tables appear on the next poll even if you declare no
  `fingerprint()` — and a DDL edit invalidates the carried rows rather than
  re-inserting them positionally into columns that have moved.

## Isolation rules

These are enforced, not conventions. A violation is rejected with a message in
`metric_handler_status`, never silently applied:

| Rule | Why |
|---|---|
| `NAME` matches `^[a-z0-9][a-z0-9_-]*$` | It prefixes your meta keys as `<NAME>.<key>`; a dot would make `a.b`+`k` and `a`+`b.k` collide |
| Meta keys match `^[a-z0-9][a-z0-9_]*$` | Same reason — `github` returning `_stats.total` would write into `github_stats`'s namespace |
| Table names match `^[a-z0-9][a-z0-9_]*$` | They are interpolated into SQL by the generic reader |
| You may not declare a core table, or one another handler claimed | Your rows would append into `cycles`, or interleave with a peer's |
| `SCHEMA` holds only plain `CREATE TABLE` statements — no `IF NOT EXISTS`, no `;` inside | DuckDB runs *every* statement in a string, so `"CREATE TABLE mine (…); DELETE FROM cycles"` would create your table and wipe a core one. `IF NOT EXISTS` silently no-ops onto a table someone else owns, and both handlers then append into it |
| After your DDL, the tables created must equal your `TABLES`, and none may have disappeared | Backstop, checked *before* the commit so a violation is rolled back |
| `collect()` may only return tables it declared | Same ownership rule, on the write side |

## Two levers for cost

Both are optional. Reach for them when the source is expensive to read.

**`fingerprint(ctx)`** — cheap identity of your sources (mtimes, sizes, a
count). Folded into the collector's refresh check, so the store rebuilds when
your source moves even though the core JSON did not. Omit it and your handler
re-runs whenever any core source changes: correct, just less lazy.

**`POLL_INTERVAL_SECONDS`** — the daemon polls the whole store every 5 minutes;
this opts you out of most of those builds. Inside the interval the collector
carries your rows and meta forward verbatim (readers see no gap) and
substitutes your last recorded fingerprint, so your sources are not even
stat()ed. Once the interval elapses you are due again and your fingerprint is
read on each poll until a build happens — that read is the only way a new
source is noticed, so it cannot be deferred further.

`metric_handler_status.state` reports `collected`, `carried`, or `failed`. A
forced rebuild (`--rebuild`, and the portal's cold-start path) ignores
intervals — skipping a handler there would publish empty tables.

**`meta={"pending": N}`** — the reserved key. Return N > 0 to say "I have work
left over" and you stay due on the next build regardless of your interval. Use
it with a per-build work cap so a first run against a large backlog converges
over a few builds instead of stalling the daemon in one pass. Report 0 (or omit
it) once caught up.

## Incremental collection

`ctx.previous_rows(table)` returns the last build's rows. Store an identity for
each unit of work — a file's `(name, size, mtime)`, an API cursor, a max id —
and carry forward whatever has not changed instead of re-reading it. This is
what keeps a 5-minute poll cheap over a growing source. See `usage.py`, which
re-parses only the transcript of the cycle that just ran.

Cap the work per build. The first run on an established agent is the case
that bites: `usage` parses at most `METRICS_USAGE_MAX_PARSE` (default 20)
transcripts per build and reports the rest as `pending`, rather than reading a
whole window of ~300 KB files in one pass. Anything that could face a backlog
should do the same.

Watch three traps:

- **Stamp identity *before* you read.** If you record a source's mtime after
  parsing it, anything written during the parse is marked already-seen while
  its rows are missing — and stays missing.
- **Distinguish "empty" from "unreadable."** Carrying forward a table you could
  not read publishes emptiness that looks like real data.
- **A unit of work can legitimately produce zero rows.** Key "already
  processed" off the identity record, not off whether rows came out of it —
  otherwise those units re-run on every build and a backlog never drains.

## Exposing it to the portal

1. Add a loader in `app/data/metrics.py` — a single `SELECT` from your table,
   no aggregation (the whole point is that the collector already did it). Use
   `load_handler_meta("mymetric")` for the scalars you put in `meta`, and
   `load_metric_table(table, order_by=…, descending=…, limit=…, where=…)` as a
   generic reader when a named loader would add nothing. Both are cached
   against the database's mtime like every other accessor.
2. Re-export it from `app/data/__init__.py`.
3. Render it from a tab. Page files may only call `app/data/` functions —
   see `.claude/rules/streamlit-app.md`.
4. Degrade honestly: an unavailable store and a genuinely empty one produce the
   same empty rows, so check before rendering "0" as a fact. `load_handler_status()`
   surfaces a handler that failed.

## Verify

```bash
# Build and check the handler ran
uv run python scripts/metrics_db.py --rebuild
uv run python scripts/metrics_db.py --stats          # your tables + row counts

# Confirm state / errors
uv run python -c "import duckdb; print(duckdb.connect('/agent/memory/metrics.duckdb', read_only=True).execute('SELECT * FROM metric_handler_status').fetchall())"

# Second run should be a no-op if nothing changed
uv run python scripts/metrics_db.py                  # → "up-to-date"
```

Tests belong in `test/services/metrics/`. Test `collect()` directly with a
hand-built `HandlerContext` — no database needed — and add a collector-level
case in `test/scripts/test_metrics_db.py` if the wiring itself matters.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Handler absent from `metric_handler_status` | Module failed to import or validate — `discover()` returns the reason; check the daemon log |
| `state='failed'` | `collect()` raised, or returned a table not in `TABLES`, or rows whose arity/types do not match the DDL |
| `state='carried'` forever | Inside `POLL_INTERVAL_SECONDS`; expected. Force with `--rebuild` |
| Tables exist but empty | `collect()` returned no rows, or a carry-forward found nothing to carry |
| Source changed, nothing rebuilt | No `fingerprint()`, or the handler is not due yet |
| `declares core table(s)` / `already claimed` | `TABLES` names a table the core or another handler owns — rename yours |
| `SCHEMA may only contain plain CREATE TABLE` | One `CREATE TABLE` per statement; no `IF NOT EXISTS`, no embedded `;`, nothing else |
| `schema failed: did not create X` / `created undeclared` / `dropped` | Your DDL and `TABLES` disagree, or it removed a table |
| `NAME … must match` / `meta key(s) must match` | Lowercase, no dots — these namespace your data |
| Numbers frozen after a DDL edit | Should not happen (the schema hash forces a rebuild); check `metric_handler_status.state` |

## Known limits

Worth knowing before building something ambitious on this:

- **No watchdog on `collect()`.** A handler that hangs (an HTTP fetch without a
  timeout) stalls the daemon, and on a cold-start build it stalls portal reads
  too. Always set timeouts on anything that leaves the machine.
- **No retention contract.** The database is rebuilt from scratch each time, so
  anything you want to *accumulate* survives only by being read back through
  `ctx.previous_rows` and re-published. That is fine for bounded windows; for
  long-lived history, keep the source of truth in a file your handler owns
  under `memory/metrics/` and materialise it into the store.
- **No cross-handler queries.** Each loader reads one table; joins across
  handler tables would need a hand-written loader in `app/data/metrics.py`.
