"""Tests for scripts/memory_recall.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import memory_recall as mr


def test_parse_args_question():
    a = mr.parse_args(["script", "what?"])
    assert a["question"] == "what?"
    assert a["k"] == 5


def test_parse_args_options():
    a = mr.parse_args(
        ["script", "Q", "--k", "10", "--mv2", "/tmp/x.mv2", "--json", "--timeline"]
    )
    assert a["k"] == 10
    assert a["mv2"] == "/tmp/x.mv2"
    assert a["json_mode"] is True
    assert a["timeline"] is True


def test_parse_args_bad_k(capsys):
    with pytest.raises(SystemExit):
        mr.parse_args(["script", "Q", "--k", "x"])


def test_parse_args_help():
    a = mr.parse_args(["script", "--help"])
    assert a["help"] is True


def test_parse_args_since_until():
    a = mr.parse_args(["script", "Q", "--since", "2026-01-01", "--until", "2026-02-01"])
    assert a["since"] == "2026-01-01"
    assert a["until"] == "2026-02-01"


def test_parse_date_to_unix_int_string():
    assert mr._parse_date_to_unix("12345") == 12345


def test_parse_date_to_unix_iso():
    val = mr._parse_date_to_unix("2026-04-30T00:00:00")
    assert isinstance(val, int)
    assert val > 1_700_000_000


def test_parse_date_to_unix_invalid_strict(capsys):
    with pytest.raises(SystemExit):
        mr._parse_date_to_unix("garbage", strict=True)


def test_parse_date_to_unix_invalid_lenient():
    assert mr._parse_date_to_unix("garbage", strict=False) is None


def test_parse_date_to_unix_none():
    assert mr._parse_date_to_unix(None) is None


def test_clean_snippet():
    assert mr._clean_snippet("body title: ignore me") == "body"


def test_clean_snippet_empty():
    assert mr._clean_snippet("") == ""


def test_clean_snippet_strips_metadata_lines():
    text = "actual\nuri: mv2://x\nstatus: ok"
    out = mr._clean_snippet(text)
    assert "uri:" not in out
    assert "actual" in out


def test_hit_to_dict():
    h = mr._hit_to_dict(
        {"snippet": "s", "score": 0.5, "title": "t", "tags": ["a"], "frame_id": 1}, 1
    )
    assert h["rank"] == 1
    assert h["score"] == 0.5
    assert h["title"] == "t"


def test_recall_no_sdk(monkeypatch):
    monkeypatch.setattr(mr, "memvid_sdk", None)
    assert mr.recall("q") == []


def test_recall_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(mr, "MV2_PATH", tmp_path / "missing.mv2")
    monkeypatch.setattr(mr, "memvid_sdk", object())  # not None
    assert mr.recall("q") == []


def test_recall_returns_items(monkeypatch, tmp_path, mocker):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("p")
    monkeypatch.setattr(mr, "MV2_PATH", mv2)
    monkeypatch.setattr(mr, "memvid_sdk", object())
    mocker.patch.object(
        mr,
        "_ask_normalized",
        return_value=(
            [
                {
                    "rank": 1,
                    "score": 0.9,
                    "title": "t",
                    "snippet": "s",
                    "tags": [],
                    "frame_id": 1,
                }
            ],
            1,
        ),
    )
    out = mr.recall("q")
    assert len(out) == 1
    assert out[0]["rank"] == 1


def test_recall_swallows_errors(monkeypatch, tmp_path, mocker):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("p")
    monkeypatch.setattr(mr, "MV2_PATH", mv2)
    monkeypatch.setattr(mr, "memvid_sdk", object())
    mocker.patch.object(mr, "_ask_normalized", side_effect=RuntimeError("nope"))
    assert mr.recall("q") == []


def test_main_no_question(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["memory_recall.py"])
    with pytest.raises(SystemExit):
        mr.main()


def test_main_help(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["memory_recall.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        mr.main()
    assert exc.value.code == 0


def test_main_missing_mv2(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mr, "MV2_PATH", tmp_path / "no.mv2")
    monkeypatch.setattr(sys, "argv", ["memory_recall.py", "what?"])
    with pytest.raises(SystemExit):
        mr.main()
    assert "not found" in capsys.readouterr().err


def test_main_query_json(monkeypatch, tmp_path, mocker, capsys):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("p")
    monkeypatch.setattr(
        sys, "argv", ["memory_recall.py", "Q", "--mv2", str(mv2), "--json"]
    )
    mocker.patch.object(mr, "_ask_normalized", return_value=([], 0))
    mr.main()
    data = json.loads(capsys.readouterr().out)
    assert data["query"] == "Q"
    assert data["results"] == []


def test_main_timeline_json(monkeypatch, tmp_path, mocker, capsys):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("p")
    monkeypatch.setattr(
        sys, "argv", ["memory_recall.py", "--timeline", "--mv2", str(mv2), "--json"]
    )
    fake_mem = mocker.MagicMock()
    fake_mem.timeline.return_value = [
        {
            "frame_id": 1,
            "timestamp": 1700000000,
            "preview": "p",
            "uri": "u",
            "child_frames": [],
        }
    ]
    mocker.patch.object(mr, "_open_readonly", return_value=fake_mem)
    mr.main()
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "timeline"
    assert data["count"] == 1


def test_entry_to_dict_dict_input():
    d = mr._entry_to_dict({"frame_id": 1})
    assert d == {"frame_id": 1}


def test_entry_to_dict_obj_input():
    class E:
        frame_id = 7
        timestamp = 1
        preview = "p"
        uri = "u"
        child_frames = []

    d = mr._entry_to_dict(E())
    assert d["frame_id"] == 7
