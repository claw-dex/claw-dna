"""Tests for app.data.workspace loaders."""

from __future__ import annotations

import os

import pytest

from app.data import workspace


@pytest.fixture(autouse=True)
def _clean_caches():
    workspace._WORKSPACE_FILES_CACHE.clear()
    workspace._WORKSPACE_FILE_CACHE.clear()
    yield
    workspace._WORKSPACE_FILES_CACHE.clear()
    workspace._WORKSPACE_FILE_CACHE.clear()


@pytest.fixture
def ws_dir(tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    (agent / "workspace").mkdir(parents=True)
    monkeypatch.setattr("app.data.workspace.AGENT_DIR", str(agent))
    return agent / "workspace"


def test_load_workspace_files_empty(ws_dir):
    out = workspace.load_workspace_files()
    assert out == {"files": [], "count": 0}


def test_load_workspace_files_missing_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("app.data.workspace.AGENT_DIR", str(tmp_path / "nope"))
    out = workspace.load_workspace_files()
    assert out == {"files": [], "count": 0}


def test_load_workspace_files_lists_recursively(ws_dir):
    (ws_dir / "a.txt").write_text("hello")
    sub = ws_dir / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world!")
    out = workspace.load_workspace_files()
    paths = {f["path"] for f in out["files"]}
    assert "a.txt" in paths
    assert os.path.join("sub", "b.txt") in paths
    assert out["count"] == 2
    # sizes populated
    sizes = {f["path"]: f["size"] for f in out["files"]}
    assert sizes["a.txt"] == 5


def test_load_workspace_files_skips_dotfiles_and_dotdirs(ws_dir):
    (ws_dir / ".secret").write_text("s")
    (ws_dir / ".cache").mkdir()
    (ws_dir / ".cache" / "junk.txt").write_text("j")
    (ws_dir / "real.txt").write_text("r")
    out = workspace.load_workspace_files()
    paths = {f["path"] for f in out["files"]}
    assert paths == {"real.txt"}


def test_load_workspace_files_sorted_by_modified_desc(ws_dir):
    a = ws_dir / "old.txt"
    b = ws_dir / "new.txt"
    a.write_text("a")
    b.write_text("b")
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    out = workspace.load_workspace_files()
    assert out["files"][0]["path"] == "new.txt"


def test_read_workspace_file_normal(ws_dir):
    (ws_dir / "f.txt").write_text("hello")
    assert workspace.read_workspace_file("f.txt") == "hello"
    assert workspace.read_workspace_file("/f.txt") == "hello"


def test_read_workspace_file_path_traversal(ws_dir):
    assert workspace.read_workspace_file("../etc/passwd") is None
    assert workspace.read_workspace_file(".dotfile") is None


def test_read_workspace_file_missing(ws_dir):
    assert workspace.read_workspace_file("missing.txt") is None


def test_read_workspace_file_image(ws_dir):
    (ws_dir / "pic.jpg").write_bytes(b"\xff\xd8\xff")
    out = workspace.read_workspace_file("pic.jpg")
    assert isinstance(out, dict)
    assert out["__type__"] == "image"


def test_read_workspace_file_too_large(ws_dir):
    big = ws_dir / "big.txt"
    big.write_bytes(b"x" * 200_000)
    out = workspace.read_workspace_file("big.txt")
    assert out is not None and "too large" in out


def test_read_workspace_file_caches_by_mtime(ws_dir):
    f = ws_dir / "c.txt"
    f.write_text("v1")
    assert workspace.read_workspace_file("c.txt") == "v1"
    mtime = os.path.getmtime(f)
    f.write_text("v2")
    os.utime(f, (mtime, mtime))
    # cache hit on same mtime
    assert workspace.read_workspace_file("c.txt") == "v1"
    os.utime(f, (mtime + 50, mtime + 50))
    assert workspace.read_workspace_file("c.txt") == "v2"


def test_read_workspace_file_subdir(ws_dir):
    sub = ws_dir / "nested"
    sub.mkdir()
    (sub / "x.txt").write_text("ok")
    assert workspace.read_workspace_file("nested/x.txt") == "ok"
