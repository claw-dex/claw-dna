"""Unit tests for the services/metrics handler registry."""

from __future__ import annotations

import sys
import types

import pytest

from services import metrics as registry
from services.metrics.base import HandlerError, HandlerResult, validate


@pytest.fixture(autouse=True)
def _no_module_leaks():
    """Synthetic handlers must not survive into later tests.

    _import() short-circuits on sys.modules, so a module loaded from a
    tmp_path that no longer exists would be handed to the next test.
    """
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("services.metrics."):
            sys.modules.pop(name, None)


def _module(name="fake", **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _valid(**overrides):
    attrs = {
        "NAME": "fake",
        "TABLES": ["metric_fake"],
        "SCHEMA": ["CREATE TABLE metric_fake (a INTEGER)"],
        "collect": lambda ctx: HandlerResult(),
    }
    attrs.update(overrides)
    return _module(**attrs)


# ---------- validate ----------


def test_validate_accepts_a_conforming_module():
    validate(_valid())  # does not raise


@pytest.mark.parametrize("missing", ["NAME", "TABLES", "SCHEMA", "collect"])
def test_validate_rejects_a_missing_attribute(missing):
    mod = _valid()
    delattr(mod, missing)
    with pytest.raises(HandlerError) as exc:
        validate(mod)
    assert missing in str(exc.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"NAME": ""},
        {"NAME": 3},
        {"TABLES": []},
        {"TABLES": "metric_fake"},
        {"SCHEMA": []},
        {"collect": "not callable"},
    ],
)
def test_validate_rejects_malformed_attributes(overrides):
    with pytest.raises(HandlerError):
        validate(_valid(**overrides))


# ---------- discovery ----------


def test_handler_names_finds_the_usage_handler():
    names = registry.handler_names()
    assert "usage" in names
    assert "base" not in names
    assert not any(n.startswith("_") for n in names)


def test_discover_returns_the_usage_handler_without_errors():
    handlers, errors = registry.discover()
    assert errors == []
    assert [h.NAME for h in handlers] == sorted(h.NAME for h in handlers)
    assert "usage" in [h.NAME for h in handlers]
    for handler in handlers:
        validate(handler)


def test_discover_reports_a_broken_handler_instead_of_raising(monkeypatch, tmp_path):
    """One bad plugin must not take the whole registry down."""
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')\n")
    (tmp_path / "good.py").write_text(
        "NAME='good'\nTABLES=['t']\nSCHEMA=['CREATE TABLE t (a INTEGER)']\n"
        "def collect(ctx):\n    return {}\n"
    )
    monkeypatch.setattr(registry, "HANDLER_DIR", tmp_path)
    for name in ("services.metrics.broken", "services.metrics.good"):
        sys.modules.pop(name, None)

    handlers, errors = registry.discover()
    assert [h.NAME for h in handlers] == ["good"]
    assert [name for name, _ in errors] == ["broken"]
    assert "RuntimeError" in errors[0][1]


def test_discover_reports_a_handler_that_fails_validation(monkeypatch, tmp_path):
    (tmp_path / "incomplete.py").write_text("NAME='incomplete'\n")
    monkeypatch.setattr(registry, "HANDLER_DIR", tmp_path)
    sys.modules.pop("services.metrics.incomplete", None)

    handlers, errors = registry.discover()
    assert handlers == []
    assert errors[0][0] == "incomplete"
    assert "TABLES" in errors[0][1]


def test_discover_rejects_a_duplicate_name(monkeypatch, tmp_path):
    body = (
        "NAME='dup'\nTABLES=['t']\nSCHEMA=['CREATE TABLE t (a INTEGER)']\n"
        "def collect(ctx):\n    return {}\n"
    )
    (tmp_path / "one.py").write_text(body)
    (tmp_path / "two.py").write_text(body)
    monkeypatch.setattr(registry, "HANDLER_DIR", tmp_path)
    for name in ("services.metrics.one", "services.metrics.two"):
        sys.modules.pop(name, None)

    handlers, errors = registry.discover()
    assert len(handlers) == 1
    assert "duplicate NAME" in errors[0][1]


# ---------- context ----------


def test_context_meta_get_reads_namespaced_keys():
    from pathlib import Path

    from services.metrics.base import HandlerContext

    ctx = HandlerContext(
        agent_dir=Path("/agent"),
        memory_dir=Path("/agent/memory"),
        messages_dir=Path("/agent/messages"),
        now=None,
        previous_meta={"usage.requests": 12, "requests": 99},
    )
    assert ctx.meta_get("usage", "requests") == 12
    assert ctx.meta_get("usage", "missing", "fallback") == "fallback"


def test_context_previous_rows_defaults_to_empty():
    from pathlib import Path

    from services.metrics.base import HandlerContext

    ctx = HandlerContext(
        agent_dir=Path("/agent"),
        memory_dir=Path("/agent/memory"),
        messages_dir=Path("/agent/messages"),
        now=None,
    )
    assert ctx.previous_rows("anything") == []


def test_a_module_that_fails_to_execute_is_not_left_in_sys_modules(
    monkeypatch, tmp_path
):
    """A half-initialised module would poison every later discover().

    _import() returns early on a sys.modules hit, so a cached broken module
    turns tick 1's real "ImportError: No module named 'yaml'" into tick 2's
    misleading "missing NAME, TABLES, SCHEMA, collect".
    """
    (tmp_path / "needsdep.py").write_text("import a_module_that_does_not_exist\n")
    monkeypatch.setattr(registry, "HANDLER_DIR", tmp_path)
    sys.modules.pop("services.metrics.needsdep", None)

    handlers, errors = registry.discover()
    assert handlers == []
    assert "services.metrics.needsdep" not in sys.modules
    assert "ModuleNotFoundError" in errors[0][1] or "ImportError" in errors[0][1]

    # The second pass reports the same real cause, not a validation error.
    handlers, errors_again = registry.discover()
    assert errors_again[0][1] == errors[0][1]


# ---------- identifier hygiene (isolation between features) ----------


@pytest.mark.parametrize("name", ["a.b", "MyMetric", "_leading", "-leading", "a b"])
def test_validate_rejects_names_that_break_the_meta_namespace(name):
    """NAME prefixes every meta key as "<NAME>.<key>".

    A dot makes that ambiguous: handler "a.b" publishing "k" and handler "a"
    publishing "b.k" both write "a.b.k", and one silently overwrites the other.
    """
    with pytest.raises(HandlerError) as exc:
        validate(_valid(NAME=name))
    assert "NAME" in str(exc.value)


@pytest.mark.parametrize(
    "tables",
    [
        ["Metric_Upper"],
        ["metric-dash"],
        ["metric x"],
        ["metric_a; DROP TABLE cycles"],
        [123],
    ],
)
def test_validate_rejects_malformed_table_names(tables):
    with pytest.raises(HandlerError) as exc:
        validate(_valid(TABLES=tables))
    assert "table name" in str(exc.value)


def test_validate_rejects_duplicate_tables_within_one_handler():
    with pytest.raises(HandlerError) as exc:
        validate(_valid(TABLES=["metric_a", "metric_a"]))
    assert "duplicates" in str(exc.value)


def test_validate_accepts_the_shipped_handler():
    from services.metrics import usage

    validate(usage)  # the real one must satisfy its own rules


# ---------- SCHEMA is whitelisted, not blacklisted ----------


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE cycles",
        "DELETE FROM cycles",
        "INSERT INTO cycles VALUES (1)",
        "ALTER TABLE cycles DROP COLUMN summary",
        "ATTACH '/tmp/other.db'",
        "UPDATE metric_health SET cycle_number = 0",
        "CREATE VIEW v AS SELECT * FROM cycles",
        "CREATE TABLE metric_fake (a INTEGER); DROP TABLE cycles",
        "CREATE TABLE IF NOT EXISTS metric_fake (a INTEGER)",
        "",
        123,
    ],
)
def test_validate_rejects_anything_but_a_plain_create_table(statement):
    """SCHEMA runs verbatim against the shared database.

    A DROP/DELETE/ALTER reaches straight into the core tables, and the
    created-tables check at build time cannot see a deletion or a row change —
    so the statement never gets to run at all.
    """
    with pytest.raises(HandlerError) as exc:
        validate(_valid(SCHEMA=[statement]))
    assert "SCHEMA" in str(exc.value)


def test_validate_requires_schema_and_tables_to_agree():
    with pytest.raises(HandlerError) as exc:
        validate(_valid(TABLES=["metric_a"], SCHEMA=["CREATE TABLE metric_b (x INT)"]))
    assert "must match exactly" in str(exc.value)

    # Declaring two tables but creating one is caught too.
    with pytest.raises(HandlerError):
        validate(
            _valid(
                TABLES=["metric_a", "metric_b"],
                SCHEMA=["CREATE TABLE metric_a (x INT)"],
            )
        )


def test_validate_accepts_a_multiline_create_table():
    validate(
        _valid(
            TABLES=["metric_fake"],
            SCHEMA=["""
                CREATE TABLE metric_fake (
                    day VARCHAR,
                    n BIGINT
                )
                """],
        )
    )


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE metric_fake (x INT); DELETE FROM cycles WHERE x IN (1)",
        "CREATE TABLE metric_fake (x INT); INSERT INTO meta VALUES ('a','b')",
        "CREATE TABLE metric_fake (x INT); UPDATE metric_health SET cycle_number = 0",
        "CREATE TABLE metric_fake (x INT); DROP TABLE cycles",
        "CREATE TABLE metric_fake (x INT);;",
    ],
)
def test_validate_rejects_multi_statement_schema(statement):
    """DuckDB executes EVERY statement in a string passed to execute().

    An embedded semicolon is a full bypass: the declared table is created (so
    the build-time created/dropped check is satisfied) and the trailing
    statement mutates whatever it likes — the core tables included.
    """
    with pytest.raises(HandlerError) as exc:
        validate(_valid(SCHEMA=[statement]))
    assert "';'" in str(exc.value) or "SCHEMA" in str(exc.value)


def test_validate_allows_one_trailing_semicolon():
    validate(_valid(SCHEMA=["CREATE TABLE metric_fake (x INTEGER);"]))


def test_validate_rejects_an_uppercase_create_target():
    """DuckDB would record it as METRIC_FAKE and the build check would confuse."""
    with pytest.raises(HandlerError) as exc:
        validate(
            _valid(TABLES=["metric_fake"], SCHEMA=["CREATE TABLE METRIC_FAKE (x INT)"])
        )
    assert "lowercase" in str(exc.value)
