"""Unit tests for services/metrics/usage.py — transcript token-usage metrics."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import pytest

from services.metrics import usage
from services.metrics.base import HandlerContext

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "transcripts"


def _usage(**overrides):
    block = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 515,
        "cache_read_input_tokens": 104810,
        "output_tokens": 77,
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": "standard",
        "cache_creation": {
            "ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 515,
        },
        "inference_geo": "not_available",
        "iterations": [],
        "speed": "standard",
    }
    block.update(overrides)
    return block


def _record(msg_id, ts, block_type="text", **usage_overrides):
    """One transcript line, in the shape the Claude CLI writes."""
    return {
        "type": "assistant",
        "uuid": f"{msg_id}-{block_type}",
        "requestId": f"req_{msg_id}",
        "timestamp": ts,
        "effort": "medium",
        "isSidechain": False,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [{"type": block_type}],
            "usage": _usage(**usage_overrides),
        },
    }


@pytest.fixture
def ctx(tmp_path):
    memory = tmp_path / "memory"
    (memory / "transcripts").mkdir(parents=True)
    return HandlerContext(
        agent_dir=tmp_path,
        memory_dir=memory,
        messages_dir=tmp_path / "messages",
        now=None,
    )


def _write(ctx, cycle, records, gz=False):
    body = "\n".join(json.dumps(r) for r in records) + "\n"
    name = f"cycle-{cycle}.jsonl" + (".gz" if gz else "")
    path = usage.transcript_dir(ctx) / name
    if gz:
        path.write_bytes(gzip.compress(body.encode()))
    else:
        path.write_text(body)
    return path


# ---------- discovery ----------


def test_transcript_files_are_capped_and_chronological(ctx, monkeypatch):
    for cycle in range(1, 8):
        _write(ctx, cycle, [_record(f"m{cycle}", "2026-08-13T10:00:00Z")])
    monkeypatch.setenv("METRICS_USAGE_CYCLES", "3")
    files = usage.transcript_files(ctx)
    # The newest three cycles, returned oldest-first.
    assert [cycle for cycle, *_ in files] == [5, 6, 7]


def test_transcript_files_ignores_unrelated_names(ctx):
    _write(ctx, 1, [_record("m1", "2026-08-13T10:00:00Z")])
    (usage.transcript_dir(ctx) / "notes.txt").write_text("x")
    (usage.transcript_dir(ctx) / "cycle-abc.jsonl").write_text("x")
    assert [c for c, *_ in usage.transcript_files(ctx)] == [1]


def test_transcript_files_is_empty_without_a_directory(tmp_path):
    ctx = HandlerContext(
        agent_dir=tmp_path,
        memory_dir=tmp_path / "nope",
        messages_dir=tmp_path,
        now=None,
    )
    assert usage.transcript_files(ctx) == []
    assert json.loads(usage.fingerprint(ctx)) == []


def test_fingerprint_tracks_file_identity(ctx):
    _write(ctx, 1, [_record("m1", "2026-08-13T10:00:00Z")])
    before = usage.fingerprint(ctx)
    _write(ctx, 2, [_record("m2", "2026-08-13T11:00:00Z")])
    assert usage.fingerprint(ctx) != before


# ---------- parsing ----------


def test_parse_deduplicates_records_sharing_a_message_id(ctx):
    # One API response written as three content-block records, each repeating
    # the same usage — summing raw would triple the token counts.
    path = _write(
        ctx,
        1,
        [
            _record("msg_a", "2026-08-13T10:00:00Z", "thinking"),
            _record("msg_a", "2026-08-13T10:00:01Z", "text"),
            _record("msg_a", "2026-08-13T10:00:02Z", "tool_use"),
        ],
    )
    rows = usage.parse_transcript(1, path)
    assert len(rows) == 1
    assert rows[0][usage._IDX["output_tokens"]] == 77
    assert rows[0][usage._IDX["input_tokens"]] == 2
    assert rows[0][usage._IDX["message_id"]] == "msg_a"
    # The response happened once; the later block records are not new events.
    assert rows[0][usage._IDX["ts"]] == "2026-08-13T10:00:00Z"
    assert rows[0][usage._IDX["day"]] == "2026-08-13"


def test_parse_emits_one_row_per_distinct_request(ctx):
    path = _write(
        ctx,
        1,
        [
            _record("msg_a", "2026-08-13T10:00:00Z", output_tokens=10),
            _record("msg_b", "2026-08-13T10:05:00Z", output_tokens=32),
        ],
    )
    rows = usage.parse_transcript(1, path)
    assert len(rows) == 2
    assert sum(r[usage._IDX["output_tokens"]] for r in rows) == 42


def test_parse_skips_ids_already_attributed_to_an_earlier_cycle(ctx):
    path = _write(
        ctx,
        2,
        [
            _record("msg_a", "2026-08-13T10:00:00Z", output_tokens=10),
            _record("msg_b", "2026-08-13T10:05:00Z", output_tokens=32),
        ],
    )
    seen = {"msg_a"}
    rows = usage.parse_transcript(2, path, seen)
    assert [r[usage._IDX["message_id"]] for r in rows] == ["msg_b"]
    assert seen == {"msg_a", "msg_b"}  # updated in place for the next cycle


def test_parse_records_the_grain_columns(ctx):
    a = _record("msg_a", "2026-08-13T10:00:00Z", output_tokens=10)
    b = _record("msg_b", "2026-08-13T10:01:00Z", output_tokens=20)
    b["message"]["model"] = "claude-opus-5"
    c = _record("msg_c", "2026-08-13T10:02:00Z", output_tokens=5)
    c["message"]["usage"]["service_tier"] = "priority"
    path = _write(ctx, 1, [a, b, c])

    rows = usage.parse_transcript(1, path)
    grains = {(r[usage._IDX["model"]], r[usage._IDX["service_tier"]]) for r in rows}
    assert grains == {
        ("claude-sonnet-5", "standard"),
        ("claude-opus-5", "standard"),
        ("claude-sonnet-5", "priority"),
    }
    assert {r[usage._IDX["effort"]] for r in rows} == {"medium"}
    assert {r[usage._IDX["speed"]] for r in rows} == {"standard"}


def test_parse_extracts_nested_counters(ctx):
    path = _write(
        ctx,
        1,
        [
            _record(
                "msg_a",
                "2026-08-13T10:00:00Z",
                server_tool_use={"web_search_requests": 3, "web_fetch_requests": 1},
                cache_creation={
                    "ephemeral_1h_input_tokens": 7,
                    "ephemeral_5m_input_tokens": 11,
                },
            )
        ],
    )
    row = usage.parse_transcript(1, path)[0]
    assert row[usage._IDX["web_search_requests"]] == 3
    assert row[usage._IDX["web_fetch_requests"]] == 1
    assert row[usage._IDX["ephemeral_1h_input_tokens"]] == 7
    assert row[usage._IDX["ephemeral_5m_input_tokens"]] == 11


def test_parse_reads_gzipped_transcripts(ctx):
    """heartbeat.sh gzips transcripts older than 7 days."""
    path = _write(ctx, 1, [_record("msg_a", "2026-08-13T10:00:00Z")], gz=True)
    rows = usage.parse_transcript(1, path)
    assert len(rows) == 1
    assert rows[0][usage._IDX["output_tokens"]] == 77


def test_parse_skips_malformed_and_usage_free_lines(ctx):
    path = usage.transcript_dir(ctx) / "cycle-1.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(_record("msg_a", "2026-08-13T10:00:00Z")),
                "{ this is not json",
                "",
                json.dumps({"type": "user", "message": {"content": "hi"}}),
                json.dumps({"type": "attachment"}),
                json.dumps(["not", "a", "dict"]),
                json.dumps({"type": "assistant", "message": {"usage": "not a dict"}}),
            ]
        )
        + "\n"
    )
    assert len(usage.parse_transcript(1, path)) == 1


def test_parse_treats_non_numeric_counters_as_zero(ctx):
    path = _write(
        ctx,
        1,
        [
            _record(
                "msg_a",
                "2026-08-13T10:00:00Z",
                output_tokens=None,
                input_tokens="lots",
            )
        ],
    )
    row = usage.parse_transcript(1, path)[0]
    assert row[usage._IDX["output_tokens"]] == 0
    assert row[usage._IDX["input_tokens"]] == 0


def test_parse_of_an_empty_transcript(ctx):
    path = usage.transcript_dir(ctx) / "cycle-1.jsonl"
    path.write_text("")
    assert usage.parse_transcript(1, path) == []


# ---------- collect ----------


def test_collect_rolls_up_daily_totals(ctx):
    _write(
        ctx,
        1,
        [_record("msg_a", "2026-08-12T10:00:00Z", output_tokens=10, input_tokens=1)],
    )
    _write(
        ctx,
        2,
        [_record("msg_b", "2026-08-13T10:00:00Z", output_tokens=20, input_tokens=2)],
    )
    _write(
        ctx,
        3,
        [_record("msg_c", "2026-08-13T11:00:00Z", output_tokens=30, input_tokens=3)],
    )

    result = usage.collect(ctx)
    daily = {r[0]: r for r in result.tables["metric_usage_daily"]}
    assert set(daily) == {"2026-08-12", "2026-08-13"}
    # (day, cycles, requests, input, cache_creation, cache_read, output, total, ...)
    assert daily["2026-08-13"][1] == 2  # two cycles landed that day
    assert daily["2026-08-13"][2] == 2  # two requests
    assert daily["2026-08-13"][6] == 50  # output tokens
    assert daily["2026-08-12"][6] == 10

    assert result.meta["transcripts"] == 3
    assert result.meta["requests"] == 3
    assert result.meta["output_tokens"] == 60
    assert result.meta["models"] == ["claude-sonnet-5"]
    assert result.meta["total_tokens"] == sum(
        result.meta[f]
        for f in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
    )


def test_collect_on_an_empty_directory(ctx):
    result = usage.collect(ctx)
    assert result.tables["metric_usage_files"] == []
    assert result.tables["metric_usage_daily"] == []
    assert result.meta["requests"] == 0
    assert result.meta["total_tokens"] == 0


def test_collect_declares_only_tables_it_owns(ctx):
    _write(ctx, 1, [_record("msg_a", "2026-08-13T10:00:00Z")])
    result = usage.collect(ctx)
    assert set(result.tables) <= set(usage.TABLES)


# ---------- incremental carry-forward ----------


def _as_context(ctx, previous):
    """Rebuild ctx with a previous_rows callable backed by `previous`."""
    return HandlerContext(
        agent_dir=ctx.agent_dir,
        memory_dir=ctx.memory_dir,
        messages_dir=ctx.messages_dir,
        now=ctx.now,
        previous_rows=lambda table: previous.get(table, []),
    )


def test_unchanged_transcripts_are_carried_forward(ctx, monkeypatch):
    _write(ctx, 1, [_record("msg_a", "2026-08-13T10:00:00Z", output_tokens=10)])
    _write(ctx, 2, [_record("msg_b", "2026-08-13T11:00:00Z", output_tokens=20)])
    first = usage.collect(ctx)
    assert first.meta["transcripts_parsed"] == 2
    assert first.meta["transcripts_reused"] == 0

    previous = dict(first.tables)
    parsed = []
    real_parse = usage.parse_transcript
    monkeypatch.setattr(
        usage,
        "parse_transcript",
        lambda cycle, path, seen=None: (
            parsed.append(cycle),
            real_parse(cycle, path, seen),
        )[1],
    )

    # Nothing changed → both reused, and parse_transcript is never called.
    second = usage.collect(_as_context(ctx, previous))
    assert second.meta["transcripts_reused"] == 2
    assert second.meta["transcripts_parsed"] == 0
    assert parsed == []
    assert second.meta["output_tokens"] == first.meta["output_tokens"]
    assert second.tables["metric_usage_daily"] == first.tables["metric_usage_daily"]


def test_only_the_changed_transcript_is_reparsed(ctx, monkeypatch):
    _write(ctx, 1, [_record("msg_a", "2026-08-13T10:00:00Z", output_tokens=10)])
    _write(ctx, 2, [_record("msg_b", "2026-08-13T11:00:00Z", output_tokens=20)])
    first = usage.collect(ctx)
    previous = dict(first.tables)

    # Cycle 2 gets another request appended — its size changes.
    _write(
        ctx,
        2,
        [
            _record("msg_b", "2026-08-13T11:00:00Z", output_tokens=20),
            _record("msg_c", "2026-08-13T11:05:00Z", output_tokens=5),
        ],
    )
    parsed = []
    real_parse = usage.parse_transcript
    monkeypatch.setattr(
        usage,
        "parse_transcript",
        lambda cycle, path, seen=None: (
            parsed.append(cycle),
            real_parse(cycle, path, seen),
        )[1],
    )

    second = usage.collect(_as_context(ctx, previous))
    assert parsed == [2]
    assert second.meta["transcripts_parsed"] == 1
    assert second.meta["transcripts_reused"] == 1
    assert second.meta["output_tokens"] == 35


def test_a_rewritten_transcript_of_the_same_size_is_reparsed(ctx, monkeypatch):
    """Carry-forward keys on (size, mtime) — a same-size rewrite still bumps mtime."""
    _write(ctx, 1, [_record("msg_a", "2026-08-13T10:00:00Z", output_tokens=10)])
    first = usage.collect(ctx)
    previous = dict(first.tables)

    path = usage.transcript_dir(ctx) / "cycle-1.jsonl"
    size_before = path.stat().st_size
    _write(ctx, 1, [_record("msg_a", "2026-08-13T10:00:00Z", output_tokens=99)])
    assert path.stat().st_size == size_before  # identical length, new content
    os.utime(path, (path.stat().st_mtime + 10,) * 2)

    second = usage.collect(_as_context(ctx, previous))
    assert second.meta["transcripts_parsed"] == 1
    assert second.meta["output_tokens"] == 99


def test_carry_forward_ignores_malformed_previous_rows(ctx):
    _write(ctx, 1, [_record("msg_a", "2026-08-13T10:00:00Z", output_tokens=10)])
    previous = {"metric_usage_files": [None, (), "junk"], "metric_usage_cycles": [42]}
    result = usage.collect(_as_context(ctx, previous))
    assert result.meta["transcripts_parsed"] == 1
    assert result.meta["output_tokens"] == 10


# ---------- against the committed transcript fixtures ----------
#
# test/fixtures/transcripts/ reproduces the Claude CLI's record shape field for
# field with fabricated content (see test/fixtures/make_transcripts.py). Real
# transcripts hold the agent's reasoning, cwd, and branch names and are never
# committed. manifest.json carries the totals the generator *intended*, derived
# independently of this parser.


def _load_fixtures(ctx):
    """Copy the committed fixtures into the handler's transcript dir."""
    for source in sorted(FIXTURES.glob("cycle-*")):
        (usage.transcript_dir(ctx) / source.name).write_bytes(source.read_bytes())
    return json.loads((FIXTURES / "manifest.json").read_text())


def test_fixtures_are_present():
    """A silent skip here would hide the only shape-guarding test."""
    assert FIXTURES.is_dir(), f"missing transcript fixtures at {FIXTURES}"
    assert sorted(p.name for p in FIXTURES.glob("cycle-*")) == [
        "cycle-9001.jsonl",
        "cycle-9002.jsonl",
        "cycle-9003.jsonl.gz",
    ]


def test_totals_match_the_fixture_manifest(ctx):
    manifest = _load_fixtures(ctx)
    result = usage.collect(ctx)

    expected = manifest["totals"]
    for field, value in expected.items():
        assert result.meta[field] == value, field
    assert result.meta["models"] == manifest["models"]
    assert result.meta["transcripts"] == 3


def test_deduplication_actually_matters_on_the_fixtures(ctx):
    """The fixtures must exercise the inflation the dedup rule exists to stop."""
    manifest = _load_fixtures(ctx)
    result = usage.collect(ctx)
    # 53 assistant records collapse to 17 distinct API responses: one response
    # per content block within a transcript, plus cycle-9002 resuming 9001's
    # session and repeating its whole history.
    assert manifest["assistant_records"] > 2 * manifest["totals"]["requests"]
    assert result.meta["requests"] == manifest["totals"]["requests"]


def test_per_cycle_attribution_matches_the_manifest(ctx):
    manifest = _load_fixtures(ctx)
    result = usage.collect(ctx)

    by_cycle = {}
    for row in result.tables["metric_usage_requests"]:
        bucket = by_cycle.setdefault(
            row[usage._IDX["cycle"]], {"requests": 0, "output_tokens": 0}
        )
        bucket["requests"] += 1
        bucket["output_tokens"] += row[usage._IDX["output_tokens"]]

    assert {str(k): v for k, v in by_cycle.items()} == manifest["per_cycle"]
    # cycle-9002 resumed 9001's session, so only its new responses count.
    assert by_cycle[9002]["requests"] == 3


def test_per_day_rollup_matches_the_manifest(ctx):
    manifest = _load_fixtures(ctx)
    result = usage.collect(ctx)
    daily = {
        row[0]: {"cycles": row[1], "requests": row[2], "output_tokens": row[6]}
        for row in result.tables["metric_usage_daily"]
    }
    assert daily == manifest["per_day"]


def test_the_gzipped_fixture_is_read(ctx):
    _load_fixtures(ctx)
    result = usage.collect(ctx)
    files = {row[0]: row[1] for row in result.tables["metric_usage_files"]}
    assert files[9003] == "cycle-9003.jsonl.gz"
    assert any(
        row[usage._IDX["cycle"]] == 9003
        for row in result.tables["metric_usage_requests"]
    )


def test_fixture_grain_columns_are_populated(ctx):
    """service_tier / speed / effort / model all vary across the fixtures."""
    _load_fixtures(ctx)
    result = usage.collect(ctx)
    rows = result.tables["metric_usage_requests"]
    assert {r[usage._IDX["service_tier"]] for r in rows} == {"standard", "priority"}
    assert {r[usage._IDX["effort"]] for r in rows} == {"medium", "high"}
    assert {r[usage._IDX["model"]] for r in rows} == {
        "claude-sonnet-5",
        "claude-opus-5",
    }
    assert {r[usage._IDX["speed"]] for r in rows} == {"standard"}
