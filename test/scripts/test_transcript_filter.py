"""Tests for scripts/transcript_filter.py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import transcript_filter as tf


def test_parse_iso_valid():
    dt = tf.parse_iso("2026-04-30T12:00:00Z")
    assert dt is not None
    assert dt.tzinfo == timezone.utc


def test_parse_iso_invalid():
    assert tf.parse_iso("garbage") is None
    assert tf.parse_iso("") is None
    assert tf.parse_iso(None) is None


def test_parse_iso_naive_assumed_utc():
    dt = tf.parse_iso("2026-04-30T12:00:00")
    assert dt is not None
    assert dt.tzinfo == timezone.utc


def test_parse_args_defaults():
    a = tf.parse_args([])
    assert a["hours"] == 24.0
    assert a["mode"] == "paths"
    assert a["page"] == 1


def test_parse_args_modes():
    a = tf.parse_args(["--markdown"])
    assert a["mode"] == "markdown"
    a = tf.parse_args(["--json"])
    assert a["mode"] == "json"


def test_parse_args_unknown_flag(capsys):
    with pytest.raises(SystemExit):
        tf.parse_args(["--bogus"])


def test_parse_args_missing_value(capsys):
    with pytest.raises(SystemExit):
        tf.parse_args(["--hours"])


def test_parse_args_invalid_hours(capsys):
    with pytest.raises(SystemExit):
        tf.parse_args(["--hours", "abc"])


def test_parse_args_invalid_page(capsys):
    with pytest.raises(SystemExit):
        tf.parse_args(["--page", "0"])


def test_parse_args_files_positional():
    a = tf.parse_args(["foo.jsonl", "bar.jsonl"])
    assert a["files"] == ["foo.jsonl", "bar.jsonl"]


def test_parse_args_since_only():
    a = tf.parse_args(["--since", "2026-01-01"])
    assert a["since"] == "2026-01-01"
    assert a["hours"] is None


def test_resolve_window_hours():
    args = {"hours": 24.0, "since": None, "until": None}
    since, until = tf.resolve_window(args)
    assert since is not None
    assert until is not None
    assert (until - since).total_seconds() == pytest.approx(24 * 3600, abs=2)


def test_resolve_window_since_invalid(capsys):
    args = {"hours": None, "since": "garbage", "until": None}
    with pytest.raises(SystemExit):
        tf.resolve_window(args)


def test_in_window_none_dt():
    assert tf.in_window(None, None, None) is False


def test_in_window_within():
    now = datetime.now(timezone.utc)
    assert tf.in_window(now, now - timedelta(hours=1), now + timedelta(hours=1)) is True


def test_in_window_before():
    now = datetime.now(timezone.utc)
    assert (
        tf.in_window(now - timedelta(hours=2), now - timedelta(hours=1), None) is False
    )


def test_cycle_number():
    assert tf.cycle_number(Path("/tmp/cycle-12.jsonl")) == 12
    assert tf.cycle_number(Path("/tmp/foo.jsonl")) is None


def test_iter_entries_skips_bad(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a":1}\nnot json\n{"b":2}\n  \n# comment\n')
    items = list(tf.iter_entries(p))
    assert items == [{"a": 1}, {"b": 2}]


def test_iter_entries_missing_file(tmp_path, capsys):
    items = list(tf.iter_entries(tmp_path / "absent.jsonl"))
    assert items == []


def test_first_timestamp(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text(
        json.dumps({"type": "user", "timestamp": "2026-04-30T00:00:00Z"}) + "\n"
    )
    dt = tf.first_timestamp(p)
    assert dt is not None
    assert dt.year == 2026


def test_first_timestamp_in_message(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text(json.dumps({"message": {"timestamp": "2026-04-30T00:00:00Z"}}) + "\n")
    dt = tf.first_timestamp(p)
    assert dt is not None


def test_first_timestamp_none(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"foo": "bar"}\n')
    assert tf.first_timestamp(p) is None


def test_truncate_block_lines():
    text = "\n".join(str(i) for i in range(tf.MAX_BLOCK_LINES + 50))
    out, truncated = tf.truncate_block(text)
    assert truncated is True
    assert "truncated" in out


def test_truncate_block_chars():
    text = "x" * (tf.MAX_BLOCK_CHARS + 100)
    out, truncated = tf.truncate_block(text)
    assert truncated is True


def test_truncate_block_short():
    out, truncated = tf.truncate_block("short")
    assert truncated is False
    assert out == "short"


def test_format_hours():
    assert tf.format_hours(24) == "24h"
    assert tf.format_hours(1.5) == "1.5h"


def test_render_tool_input_json():
    s = tf.render_tool_input({"a": 1})
    assert '"a"' in s


def test_render_tool_result_string():
    assert tf.render_tool_result_content("hello") == "hello"


def test_render_tool_result_list_with_text():
    out = tf.render_tool_result_content([{"type": "text", "text": "hi"}])
    assert "hi" in out


def test_render_tool_result_list_with_other():
    out = tf.render_tool_result_content([{"type": "image"}])
    assert "[image]" in out


def test_quote_lines():
    out = tf.quote_lines("line1\nline2")
    assert out.splitlines()[0].startswith("> ")


def test_quote_lines_empty():
    assert tf.quote_lines("") == "> "


def test_render_tool_call_inline():
    s = tf.render_tool_call("Bash", {"command": "ls"})
    assert "Bash" in s
    assert "`ls`" in s


def test_render_tool_call_block():
    s = tf.render_tool_call("Edit", {"file": "x", "content": "y"})
    assert "```json" in s


def test_paginate_simple():
    pages = tf.paginate(["a" * 10, "b" * 10, "c" * 10], 25)
    # First two fit (10+2+10=22 <= 25), then "c" goes to next page
    assert len(pages) == 2


def test_paginate_oversize_chunk():
    pages = tf.paginate(["x" * 100], 10)
    assert len(pages) == 1  # chunk gets its own page


def test_paginate_empty():
    assert tf.paginate([], 100) == []


def test_render_chunks_basic(tmp_path):
    p = tmp_path / "cycle-7.jsonl"
    p.write_text(
        json.dumps({"type": "user", "message": {"content": "hello"}})
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hi back"}]},
            }
        )
        + "\n"
    )
    chunks = list(tf.render_chunks(p))
    kinds = [k for k, _ in chunks]
    assert "cycle" in kinds
    assert kinds.count("entry") >= 2
    cycle_chunk = next(c for k, c in chunks if k == "cycle")
    assert "Cycle 7" in cycle_chunk


def test_render_chunks_skips_sidechain(tmp_path):
    p = tmp_path / "cycle-1.jsonl"
    p.write_text(
        json.dumps(
            {
                "type": "user",
                "isSidechain": True,
                "message": {"content": "skipme"},
            }
        )
        + "\n"
    )
    chunks = list(tf.render_chunks(p))
    # Only the cycle header
    assert all(k == "cycle" or "skipme" not in c for k, c in chunks)


def test_cache_roundtrip(tmp_path):
    p = tmp_path / "cycle-1.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {"content": "x"}}) + "\n")
    cd = tmp_path / "cache"
    chunks = ["a", "b"]
    tf.save_cached_chunks(p, str(cd), False, chunks)
    loaded = tf.load_cached_chunks(p, str(cd), False)
    assert loaded == chunks


def test_cache_invalidates_on_mtime_change(tmp_path):
    p = tmp_path / "cycle-1.jsonl"
    p.write_text("data")
    cd = tmp_path / "cache"
    tf.save_cached_chunks(p, str(cd), False, ["x"])
    # Modify file
    p.write_text("data2")
    assert tf.load_cached_chunks(p, str(cd), False) is None


def test_cache_load_missing(tmp_path):
    assert tf.load_cached_chunks(tmp_path / "no.jsonl", str(tmp_path), False) is None


def test_discover_files_explicit(tmp_path):
    args = {"files": ["a.jsonl", "b.jsonl"], "dir": str(tmp_path)}
    files = tf.discover_files(args)
    assert [str(f) for f in files] == ["a.jsonl", "b.jsonl"]


def test_discover_files_dir(tmp_path):
    (tmp_path / "cycle-1.jsonl").write_text("")
    (tmp_path / "cycle-2.jsonl").write_text("")
    (tmp_path / "other.jsonl").write_text("")
    args = {"files": [], "dir": str(tmp_path)}
    files = tf.discover_files(args)
    names = sorted(p.name for p in files)
    assert names == ["cycle-1.jsonl", "cycle-2.jsonl"]


def test_discover_files_missing_dir(tmp_path, capsys):
    args = {"files": [], "dir": str(tmp_path / "nope")}
    files = tf.discover_files(args)
    assert files == []
    assert "directory not found" in capsys.readouterr().err


def test_main_paths_mode(tmp_path, monkeypatch, capsys):
    p = tmp_path / "cycle-3.jsonl"
    p.write_text(
        json.dumps({"timestamp": "2026-04-30T00:00:00Z", "type": "user"}) + "\n"
    )
    rc = tf.main(["--dir", str(tmp_path), "--hours", "100000", "--paths"])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(p.resolve()) in out or str(p) in out


def test_main_json_mode(tmp_path, capsys):
    p = tmp_path / "cycle-4.jsonl"
    p.write_text(
        json.dumps({"timestamp": "2026-04-30T00:00:00Z", "type": "user"}) + "\n"
    )
    rc = tf.main(["--dir", str(tmp_path), "--hours", "100000", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert data[0]["cycle"] == 4
