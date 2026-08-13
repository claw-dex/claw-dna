"""Pluggable metrics handlers.

Drop a `.py` file in this directory that satisfies the contract in
``services/metrics/base.py`` and ``scripts/metrics_db.py`` picks it up on the
next build — no registration list to edit.

    services/metrics/usage.py   → token usage parsed from cycle transcripts
    services/metrics/<yours>.py → anything else worth measuring

Modules whose name starts with `_`, plus `base` itself, are skipped. A module
that fails to import or fails validation is reported by ``discover()`` rather
than raising, so one broken handler cannot take the metrics store down.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

# Re-exported so a handler can `from services.metrics import HandlerResult`
# without reaching into the base module.
from services.metrics.base import (  # noqa: F401
    HandlerContext,
    HandlerError,
    HandlerResult,
    validate,
)

__all__ = [
    "HandlerContext",
    "HandlerError",
    "HandlerResult",
    "validate",
    "discover",
    "handler_names",
]

HANDLER_DIR = Path(__file__).resolve().parent
_SKIP = {"base"}


def handler_names() -> list:
    """Sorted names of every candidate handler module in this directory."""
    return sorted(
        p.stem
        for p in HANDLER_DIR.glob("*.py")
        if not p.stem.startswith("_") and p.stem not in _SKIP
    )


def _import(name: str):
    """Import a handler module by stem, without requiring services/ on sys.path."""
    qualified = f"{__name__}.{name}"
    if qualified in sys.modules:
        return sys.modules[qualified]
    try:
        return importlib.import_module(qualified)
    except ImportError:
        # Fall back to loading straight off disk: the collector runs as a
        # script from /agent/scripts, where `services` may not be importable
        # as a package.
        spec = importlib.util.spec_from_file_location(
            qualified, HANDLER_DIR / f"{name}.py"
        )
        if spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            # A half-initialised module must not stay cached: _import()
            # short-circuits on sys.modules, so the next discover() would
            # find an attribute-less object and report "missing NAME,
            # TABLES, ..." instead of the real ImportError.
            sys.modules.pop(qualified, None)
            raise
        return module


def discover() -> tuple:
    """Return ``(handlers, errors)``.

    ``handlers`` are validated modules sorted by NAME; ``errors`` is a list of
    ``(module_name, message)`` for anything that failed to import or validate.
    Callers should surface the errors — silence would make a typo in a handler
    look like "this metric just has no data".
    """
    handlers = []
    errors = []
    seen = {}
    for name in handler_names():
        try:
            module = _import(name)
            validate(module)
        except Exception as exc:
            errors.append((name, f"{type(exc).__name__}: {exc}"))
            continue
        if module.NAME in seen:
            errors.append(
                (name, f"duplicate NAME {module.NAME!r} (also in {seen[module.NAME]})")
            )
            continue
        seen[module.NAME] = name
        handlers.append(module)
    handlers.sort(key=lambda m: m.NAME)
    return handlers, errors
