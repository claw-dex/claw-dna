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
"""

from __future__ import annotations

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


def validate(module) -> None:
    """Raise HandlerError unless *module* satisfies the contract."""
    missing = [a for a in REQUIRED_ATTRS if not hasattr(module, a)]
    if missing:
        raise HandlerError(
            f"{getattr(module, '__name__', module)!r} is missing {', '.join(missing)}"
        )
    if not isinstance(module.NAME, str) or not module.NAME:
        raise HandlerError(f"{module.__name__}: NAME must be a non-empty string")
    if not isinstance(module.TABLES, (list, tuple)) or not module.TABLES:
        raise HandlerError(f"{module.__name__}: TABLES must be a non-empty list")
    if not isinstance(module.SCHEMA, (list, tuple)) or not module.SCHEMA:
        raise HandlerError(f"{module.__name__}: SCHEMA must be a non-empty list")
    if not callable(module.collect):
        raise HandlerError(f"{module.__name__}: collect must be callable")
