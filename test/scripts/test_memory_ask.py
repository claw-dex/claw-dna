"""Tests for scripts/memory_ask.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import memory_ask as ma


def test_parse_args_question_only():
    a = ma.parse_args(["script", "what is X?"])
    assert a["question"] == "what is X?"
    assert a["k"] == 20


def test_parse_args_overrides():
    a = ma.parse_args(
        ["script", "Q", "--k", "5", "--mv2", "/tmp/x.mv2", "--context-only", "--json"]
    )
    assert a["k"] == 5
    assert a["mv2"] == "/tmp/x.mv2"
    assert a["context_only"] is True
    assert a["json_mode"] is True


def test_parse_args_help():
    a = ma.parse_args(["script", "--help"])
    assert a["help"] is True


def test_parse_args_bad_k(capsys):
    with pytest.raises(SystemExit):
        ma.parse_args(["script", "Q", "--k", "notanumber"])


def test_parse_args_custom_system():
    a = ma.parse_args(["script", "Q", "--system", "be brief"])
    assert a["system_prompt"] == "be brief"


def test_clean_text_strips_metadata():
    text = "real content here title: ignore this part tags: a,b"
    assert ma._clean_text(text) == "real content here"


def test_clean_text_newline_metadata():
    text = "content\ntitle: ignored"
    assert ma._clean_text(text) == "content"


def test_clean_text_empty():
    assert ma._clean_text("") == ""
    assert ma._clean_text(None) == ""


def test_clean_text_strips_metadata_lines():
    text = "real line\nuri: mv2://abc\nstatus: ok\nanother real"
    out = ma._clean_text(text)
    assert "uri:" not in out
    assert "status:" not in out
    assert "real line" in out


def test_require_sdk_exits_when_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(ma, "memvid_sdk", None)
    with pytest.raises(SystemExit):
        ma._require_sdk()
    assert "memvid_sdk" in capsys.readouterr().err


def test_retrieve_context_missing_file(tmp_path, mocker, capsys):
    # Provide a fake sdk so _require_sdk doesn't exit first
    mocker.patch.object(ma, "memvid_sdk", mocker.MagicMock())
    with pytest.raises(SystemExit):
        ma.retrieve_context(str(tmp_path / "no.mv2"), "q", 5)
    assert "not found" in capsys.readouterr().err


def test_retrieve_context_mv004_returns_empty(tmp_path, mocker):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("p")
    fake_sdk = mocker.MagicMock()
    fake_mem = mocker.MagicMock()
    fake_mem.ask.side_effect = Exception("MV004 something")
    fake_sdk.use.return_value = fake_mem
    mocker.patch.object(ma, "memvid_sdk", fake_sdk)
    out = ma.retrieve_context(str(mv2), "q", 5)
    assert out["results"] == []


def test_retrieve_context_returns_hits(tmp_path, mocker):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("p")
    fake_sdk = mocker.MagicMock()
    fake_mem = mocker.MagicMock()
    fake_mem.ask.return_value = {
        "chunks": [
            {"title": "T1", "snippet": "snippet text", "score": 0.9, "frame_id": 1},
            {"title": "T2", "snippet": "more text", "score": 0.5, "frame_id": 2},
        ],
        "stats": {"total_hits": 2, "took_ms": 12},
    }
    fake_sdk.use.return_value = fake_mem
    mocker.patch.object(ma, "memvid_sdk", fake_sdk)
    out = ma.retrieve_context(str(mv2), "q", 5)
    assert len(out["results"]) == 2
    assert out["total_hits"] == 2
    assert out["stats"]["retrieval_ms"] == 12


def test_retrieve_context_other_error_exits(tmp_path, mocker, capsys):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("p")
    fake_sdk = mocker.MagicMock()
    fake_mem = mocker.MagicMock()
    fake_mem.ask.side_effect = RuntimeError("disk on fire")
    fake_sdk.use.return_value = fake_mem
    mocker.patch.object(ma, "memvid_sdk", fake_sdk)
    with pytest.raises(SystemExit):
        ma.retrieve_context(str(mv2), "q", 5)
    assert "memvid ask failed" in capsys.readouterr().err


def test_main_no_question(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["memory_ask.py"])
    with pytest.raises(SystemExit):
        ma.main()
    assert "QUESTION is required" in capsys.readouterr().err


def test_main_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["memory_ask.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        ma.main()
    assert exc.value.code == 0


def test_main_no_results_json(monkeypatch, mocker, capsys, tmp_path):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("p")
    monkeypatch.setattr(
        sys, "argv", ["memory_ask.py", "Q", "--mv2", str(mv2), "--json"]
    )
    mocker.patch.object(
        ma,
        "retrieve_context",
        return_value={"results": [], "context": "", "total_hits": 0, "stats": {}},
    )
    with pytest.raises(SystemExit) as exc:
        ma.main()
    assert exc.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["context_hits"] == 0


def test_main_context_only_json(monkeypatch, mocker, capsys, tmp_path):
    mv2 = tmp_path / "x.mv2"
    mv2.write_text("p")
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_ask.py", "Q", "--mv2", str(mv2), "--context-only", "--json"],
    )
    mocker.patch.object(
        ma,
        "retrieve_context",
        return_value={
            "results": [{"title": "t", "score": 0.9}],
            "context": "ctx",
            "total_hits": 1,
            "stats": {"retrieval_ms": 5},
        },
    )
    with pytest.raises(SystemExit) as exc:
        ma.main()
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["context"] == "ctx"
    assert data["context_hits"] == 1
