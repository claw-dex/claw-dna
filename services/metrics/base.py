"""Contract shared by every metrics handler under services/metrics/.

A handler is a plain Python module that contributes its own tables to the
DuckDB metrics store built by ``scripts/metrics_db.py``. Handlers never open
the database themselves — the collector owns the single write connection and
the atomic build-and-swap — so a handler only has to answer two questions:

1. "Has my source changed?"  → ``fingerprint(ctx)``
2. "What rows should I store?" → ``collect(ctx)``

Module contract
---------------
Required::

    NAME    = "usage"                       # unique; namespaces meta keys
    TABLES  = ["metric_usage_cycles", ...]  # every table SCHEMA creates
    SCHEMA  = ["CREATE TABLE metric_usage_cycles (...)", ...]

    def collect(ctx: HandlerContext) -> HandlerResult | dict:
        return HandlerResult(tables={"metric_usage_cycles": [(...), ...]},
                             meta={"total_output_tokens": 123})

Optional::

    def fingerprint(ctx: HandlerContext) -> str
        # Folded into the collector's refresh check. Omit it and the handler
        # re-runs whenever any core source changes — correct, just less lazy.

Reserved meta key
-----------------
Returning ``meta={"pending": N}`` with N > 0 tells the collector you have work
left over — a bounded slice of a large backlog, say. The handler is then
treated as due on the next build regardless of ``POLL_INTERVAL_SECONDS``, so a
backfill converges in successive builds instead of one interval apiece. Report
0 (or omit it) once you are caught up.

    POLL_INTERVAL_SECONDS = 900
        # Minimum gap between collections. The daemon polls the whole store on
        # its own (shorter) cadence; a handler whose source is expensive to
        # read, or which nobody needs to the minute, sets this to opt out of
        # most of those builds. Inside the interval the collector carries the
        # handler's rows forward untouched and substitutes its last recorded
        # fingerprint, so its sources cannot trigger a rebuild and are not even
        # stat()ed. Once the interval elapses the handler is due again and its
        # fingerprint is read on each poll until a build actually happens —
        # that read is the only way a new source is noticed, so it cannot be
        # deferred further. Omit the attribute to collect on every build.

Rows are plain tuples in the column order of the handler's own DDL.

Guarantees the collector makes
------------------------------
* DDL runs inside the same transaction as the core tables, so a handler's
  tables always exist even when its ``collect`` raises.
* A handler that raises is isolated: its tables stay empty, the rest of the
  build succeeds, and the failure is recorded in ``metric_handler_status``
  for the portal to surface. One broken handler never blocks the store.
* ``ctx.previous_rows`` exposes the *previous* build's rows for a table, which
  is what makes incremental handlers possible — carry forward what has not
  changed instead of re-parsing it.
* A handler that declares ``POLL_INTERVAL_SECONDS`` is skipped between runs
  with its rows and meta preserved, so readers see no gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class HandlerContext:
    """Everything a handler needs, injected by the collector.

    Attributes:
        agent_dir:     Root of the agent tree (``/agent`` in production).
        memory_dir:    ``<agent_dir>/memory``.
        messages_dir:  ``<agent_dir>/messages``.
        now:           Build timestamp (aware, UTC). Use this instead of
                       ``datetime.now()`` so builds are reproducible in tests.
        sources:       Core payloads ``load_sources()`` already decoded, keyed
                       by logical name (``cycles``, ``journal``, ``goals``, …).
                       Read these rather than re-opening the JSON files.
        previous_meta: Decoded ``meta`` from the live database, or {}. Handler
                       keys appear here namespaced as ``"<NAME>.<key>"``.
        previous_rows: ``previous_rows(table) -> list[tuple]`` for the live
                       database, or [] when it is absent/unreadable.
    """

    agent_dir: Path
    memory_dir: Path
    messages_dir: Path
    now: object
    sources: dict = field(default_factory=dict)
    previous_meta: dict = field(default_factory=dict)
    previous_rows: Callable[[str], list] = lambda _table: []

    def meta_get(self, name: str, key: str, default=None):
        """Read a namespaced meta value written by handler *name*."""
        return self.previous_meta.get(f"{name}.{key}", default)


@dataclass
class HandlerResult:
    """What a handler returns: rows per table, plus scalar meta values.

    ``meta`` keys are namespaced with the handler's NAME before being stored,
    so two handlers can both publish a "total" without colliding.
    """

    tables: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


class HandlerError(Exception):
    """Raised by the registry when a module does not satisfy the contract."""


REQUIRED_ATTRS = ("NAME", "TABLES", "SCHEMA", "collect")

# NAME prefixes every meta key as "<NAME>.<key>", so a dot in the name would
# make the namespace ambiguous: handler "a.b" publishing "k" and handler "a"
# publishing "b.k" both write "a.b.k", and one silently wins.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Handler tables share one database with the core tables, so a name that does
# not mark them as a handler's own invites collisions as the core grows.
TABLE_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")

# SCHEMA statements are executed verbatim against the shared database, so they
# are whitelisted rather than blacklisted: exactly one plain CREATE TABLE per
# declared table. Anything else — DROP, DELETE, INSERT, ALTER, ATTACH — would
# reach straight into the core tables or another handler's, and the
# created-tables check cannot see a deletion or a row-level change.
# `IF NOT EXISTS` is refused too: it silently no-ops onto a table that already
# exists, which is how a handler ends up appending into someone else's.
CREATE_TABLE_RE = re.compile(
    r"^CREATE\s+TABLE\s+(?P<name>[A-Za-z0-9_]+)\s*\(.*\)$",
    re.IGNORECASE | re.DOTALL,
)

# Meta keys are stored as "<NAME>.<key>". A key containing a dot (or the
# separator characters NAME allows) could address another handler's namespace:
# handler "github" publishing "_stats.total" writes "github_stats.total", which
# load_handler_meta("github_stats") would serve as that handler's own.
META_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


def check_schema_statement(statement: str) -> str:
    """Return the table a SCHEMA statement creates, or raise HandlerError.

    Exactly one plain `CREATE TABLE <lowercase_name> (...)`. DuckDB executes
    every statement in a string passed to `execute()`, so an embedded
    semicolon is a full bypass — `"CREATE TABLE mine (x INT); DELETE FROM
    cycles"` would create the declared table (leaving the created/dropped
    check happy) *and* wipe a core table. Semicolons are therefore refused
    outright, apart from one optional terminator.
    """
    if not isinstance(statement, str):
        raise HandlerError("SCHEMA entries must be strings")
    text = statement.strip()
    if text.endswith(";"):
        text = text[:-1].strip()
    head = " ".join(text.split())[:60]
    if ";" in text:
        raise HandlerError(
            f"SCHEMA statements may not contain ';' — DuckDB would execute "
            f"everything after it: {head!r}"
        )
    match = CREATE_TABLE_RE.match(text)
    if not match:
        raise HandlerError(
            f"SCHEMA may only contain plain CREATE TABLE statements "
            f"(no IF NOT EXISTS): {head!r}"
        )
    name = match.group("name")
    if name != name.lower():
        raise HandlerError(
            f"SCHEMA creates {name!r}; table names must be lowercase so they "
            "match the name declared in TABLES"
        )
    return name


def validate(module) -> None:
    """Raise HandlerError unless *module* satisfies the contract."""
    missing = [a for a in REQUIRED_ATTRS if not hasattr(module, a)]
    if missing:
        raise HandlerError(
            f"{getattr(module, '__name__', module)!r} is missing {', '.join(missing)}"
        )
    if not isinstance(module.NAME, str) or not module.NAME:
        raise HandlerError(f"{module.__name__}: NAME must be a non-empty string")
    if not NAME_RE.match(module.NAME):
        raise HandlerError(
            f"{module.__name__}: NAME {module.NAME!r} must match {NAME_RE.pattern} "
            "(lowercase, no dots — it namespaces this handler's meta keys)"
        )
    if not isinstance(module.TABLES, (list, tuple)) or not module.TABLES:
        raise HandlerError(f"{module.__name__}: TABLES must be a non-empty list")
    for table in module.TABLES:
        if not isinstance(table, str) or not TABLE_RE.match(table):
            raise HandlerError(
                f"{module.__name__}: table name {table!r} must match "
                f"{TABLE_RE.pattern}"
            )
    if len(set(module.TABLES)) != len(module.TABLES):
        raise HandlerError(f"{module.__name__}: TABLES contains duplicates")
    if not isinstance(module.SCHEMA, (list, tuple)) or not module.SCHEMA:
        raise HandlerError(f"{module.__name__}: SCHEMA must be a non-empty list")

    created = []
    for statement in module.SCHEMA:
        try:
            created.append(check_schema_statement(statement))
        except HandlerError as exc:
            raise HandlerError(f"{module.__name__}: {exc}") from None
    if sorted(created) != sorted(module.TABLES):
        raise HandlerError(
            f"{module.__name__}: SCHEMA creates {sorted(created)} but TABLES "
            f"declares {sorted(module.TABLES)} — they must match exactly"
        )

    if not callable(module.collect):
        raise HandlerError(f"{module.__name__}: collect must be callable")
