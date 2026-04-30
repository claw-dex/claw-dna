"""Tests for scripts/memory_inspect.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import memory_inspect as mi


def test_parse_args_defaults():
    a = mi.parse_args(["script"])
    assert a["mv2"].endswith("long_term_memory.mv2")
    # memory derives from mv2 parent
    assert a["memory"] == str(Path(a["mv2"]).resolve().parent)


def test_parse_args_explicit_mv2_derives_memory(tmp_path):
    p = tmp_path / "custom.mv2"
    a = mi.parse_args(["script", "--mv2", str(p)])
    assert a["mv2"] == str(p)
    assert a["memory"] == str(tmp_path.resolve())


def test_parse_args_options():
    a = mi.parse_args(
        [
            "script",
            "--top-tags",
            "5",
            "--sample",
            "3",
            "--limit",
            "100",
            "--deep",
            "--api",
            "--json",
        ]
    )
    assert a["top_tags"] == 5
    assert a["sample"] == 3
    assert a["limit"] == 100
    assert a["deep"] is True
    assert a["api"] is True
    assert a["json_mode"] is True


def test_parse_args_help():
    a = mi.parse_args(["script", "--help"])
    assert a["help"] is True


def test_entry_fields_dict():
    e = mi._entry_fields(
        {
            "frame_id": 7,
            "timestamp": 100,
            "preview": "p",
            "uri": "u",
            "child_frames": [1, 2],
        }
    )
    assert e["frame_id"] == 7
    assert e["preview"] == "p"
    assert e["child_frames"] == [1, 2]


def test_entry_fields_object():
    class E:
        frame_id = 1
        timestamp = 2
        preview = "x"
        uri = "u"
        child_frames = []

    f = mi._entry_fields(E())
    assert f["frame_id"] == 1
    assert f["preview"] == "x"


def test_parse_meta_from_preview_basic():
    text = 'body content title: My Title tags: a, b labels: cat category: "X"'
    meta = mi._parse_meta_from_preview(text)
    assert meta["title"] == "My Title"
    assert meta["category"] == "X"
    assert meta["label"] == "cat"
    assert "a" in meta["tags"]


def test_parse_meta_from_preview_no_marker():
    meta = mi._parse_meta_from_preview("just plain content with no markers")
    assert meta["title"] == ""
    assert meta["tags"] == []


def test_parse_meta_from_preview_empty():
    assert mi._parse_meta_from_preview("") == {
        "title": "",
        "label": "",
        "category": "",
        "tags": [],
    }


def test_content_len():
    assert mi._content_len("real text title: x") == len("real text")
    assert mi._content_len("just plain") == len("just plain")
    assert mi._content_len("") == 0


def test_bucket_thresholds():
    assert mi._bucket(50) == "<100B"
    assert mi._bucket(500) == "<1KB"
    assert mi._bucket(5_000) == "<10KB"
    assert mi._bucket(50_000) == "<100KB"
    assert mi._bucket(500_000) == "<1MB"
    assert mi._bucket(2_000_000) == ">=1MB"


def test_frame_uri_from_int():
    assert mi._frame_uri(42) == "mv2://frame/42"


def test_frame_uri_from_dict_with_uri():
    assert mi._frame_uri({"uri": "mv2://frame/9", "frame_id": 1}) == "mv2://frame/9"


def test_frame_uri_from_dict_fallback():
    assert mi._frame_uri({"frame_id": 5}) == "mv2://frame/5"


def test_to_jsonable_primitives():
    assert mi._to_jsonable(None) is None
    assert mi._to_jsonable(1) == 1
    assert mi._to_jsonable("s") == "s"


def test_to_jsonable_list_dict():
    out = mi._to_jsonable({"a": [1, 2], "b": {"x": 3}})
    assert out == {"a": [1, 2], "b": {"x": 3}}


def test_to_jsonable_object_with_dict():
    class O:
        def __init__(self):
            self.a = 1
            self._private = 2

    out = mi._to_jsonable(O())
    assert out == {"a": 1}


def test_safe_load_list_missing(tmp_path):
    assert mi._safe_load_list(tmp_path / "no.json") == 0


def test_safe_load_list_ok(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("[1, 2, 3]")
    assert mi._safe_load_list(p) == 3


def test_safe_load_list_not_a_list(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"a":1}')
    assert mi._safe_load_list(p) == 0


def test_inspect_files_missing(tmp_path):
    out = mi.inspect_files(tmp_path / "missing.mv2")
    assert out["exists"] is False
    assert out["size"] == 0


def test_inspect_files_with_siblings(tmp_path):
    mv2 = tmp_path / "long_term_memory.mv2"
    mv2.write_bytes(b"data")
    (tmp_path / "long_term_memory.mv2.backup").write_bytes(b"b")
    out = mi.inspect_files(mv2)
    assert out["exists"] is True
    assert out["size"] == 4
    sib_names = {s["name"] for s in out["siblings"]}
    assert "long_term_memory.mv2" in sib_names
    assert "long_term_memory.mv2.backup" in sib_names


def test_inspect_sources(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    msgs = tmp_path / "messages"
    msgs.mkdir()
    (mem / "journal.json").write_text("[1, 2]")
    (msgs / "inbox.json").write_text("[1]")
    counts = mi.inspect_sources(mem)
    assert counts["journal.json"] == 2
    assert counts["messages/inbox.json"] == 1


def test_ts_to_iso_valid():
    s = mi._ts_to_iso(1700000000)
    assert s and "T" in s


def test_ts_to_iso_invalid():
    assert mi._ts_to_iso(0) is None
    assert mi._ts_to_iso("not-a-num") is None


def test_fmt_bytes():
    assert "B" in mi._fmt_bytes(50)
    assert "KB" in mi._fmt_bytes(2048)


def test_fmt_bytes_none():
    assert mi._fmt_bytes(None) == "?"


def test_main_missing_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["memory_inspect.py", "--mv2", str(tmp_path / "no.mv2")]
    )
    with pytest.raises(SystemExit):
        mi.main()
    assert "not found" in capsys.readouterr().err


def test_main_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["memory_inspect.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        mi.main()
    assert exc.value.code == 0


def test_main_api_dump(monkeypatch, tmp_path, mocker, capsys):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("p")
    fake_mem = mocker.MagicMock(spec=["foo", "bar"])
    fake_mem.foo = lambda: None
    fake_mem.bar = 42
    mocker.patch.object(mi, "_open_readonly", return_value=fake_mem)
    monkeypatch.setattr(sys, "argv", ["memory_inspect.py", "--mv2", str(mv2), "--api"])
    with pytest.raises(SystemExit) as exc:
        mi.main()
    assert exc.value.code == 0
    assert "dir(mem)" in capsys.readouterr().out


def test_main_json_report(monkeypatch, tmp_path, mocker, capsys):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("p")
    monkeypatch.setattr(sys, "argv", ["memory_inspect.py", "--mv2", str(mv2), "--json"])
    mocker.patch.object(
        mi,
        "inspect_index",
        return_value={"entries_seen": 0, "limit": 10, "truncated": False},
    )
    mi.main()
    data = json.loads(capsys.readouterr().out)
    assert "files" in data
    assert "sources" in data
    assert "index" in data
