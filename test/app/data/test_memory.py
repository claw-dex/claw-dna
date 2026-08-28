"""Tests for app.data.memory loaders."""

from __future__ import annotations

import os

import pytest

from app.data import memory


@pytest.fixture(autouse=True)
def _clean_caches():
    memory._MEMFILES_CACHE.clear()
    memory._MEMORY_FILE_CACHE.clear()
    yield
    memory._MEMFILES_CACHE.clear()
    memory._MEMORY_FILE_CACHE.clear()


@pytest.fixture
def mem_dir(tmp_path, monkeypatch):
    d = tmp_path / "memory"
    d.mkdir()
    monkeypatch.setattr("app.data.memory.MEMORY_DIR", str(d))
    return d


def test_load_memory_files_empty(mem_dir):
    assert memory.load_memory_files() == []


def test_load_memory_files_lists_and_sorts(mem_dir):
    (mem_dir / "b.txt").write_text("b")
    (mem_dir / "a.txt").write_text("a")
    (mem_dir / "c.json").write_text("{}")
    assert memory.load_memory_files() == ["a.txt", "b.txt", "c.json"]


def test_load_memory_files_skips_dotfiles_and_dirs(mem_dir):
    (mem_dir / "visible.txt").write_text("v")
    (mem_dir / ".hidden").write_text("h")
    (mem_dir / "subdir").mkdir()
    assert memory.load_memory_files() == ["visible.txt"]


def test_load_memory_files_missing_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("app.data.memory.MEMORY_DIR", str(tmp_path / "nope"))
    assert memory.load_memory_files() == []


def test_load_memory_files_caches_until_dir_changes(mem_dir):
    (mem_dir / "a.txt").write_text("a")
    first = memory.load_memory_files()
    assert first == ["a.txt"]
    # Tamper with on-disk: cache should still serve stale until dir mtime changes.
    # Force same mtime to simulate "no change":
    (mem_dir / "ghost.txt").write_text("g")
    # Reset directory mtime to original to verify cache hit path
    cached_entry = memory._MEMFILES_CACHE.get("data")
    assert cached_entry is not None
    # change mtime to match cached → cache hit (returns first list)
    _, c_mtime = cached_entry
    os.utime(mem_dir, (c_mtime, c_mtime))
    assert memory.load_memory_files() == first  # cache hit, ignores ghost
    # Now bump mtime → cache invalidates
    os.utime(mem_dir, (c_mtime + 100, c_mtime + 100))
    assert "ghost.txt" in memory.load_memory_files()


def test_read_memory_file_normal(mem_dir):
    (mem_dir / "note.txt").write_text("hello")
    assert memory.read_memory_file("note.txt") == "hello"


def test_read_memory_file_rejects_path_traversal(mem_dir):
    assert memory.read_memory_file("../etc/passwd") is None
    assert memory.read_memory_file("a/b") is None
    assert memory.read_memory_file(".hidden") is None


def test_read_memory_file_missing(mem_dir):
    assert memory.read_memory_file("missing.txt") is None


def test_read_memory_file_image_returns_marker(mem_dir):
    img = mem_dir / "pic.png"
    img.write_bytes(b"\x89PNG\r\n")
    result = memory.read_memory_file("pic.png")
    assert isinstance(result, dict)
    assert result["__type__"] == "image"
    assert result["path"].endswith("pic.png")


def test_read_memory_file_caches_by_mtime(mem_dir):
    f = mem_dir / "x.txt"
    f.write_text("v1")
    assert memory.read_memory_file("x.txt") == "v1"
    # Overwrite content but keep same mtime → cache returns stale
    mtime = os.path.getmtime(f)
    f.write_text("v2")
    os.utime(f, (mtime, mtime))
    assert memory.read_memory_file("x.txt") == "v1"
    # Bump mtime → cache invalidates
    os.utime(f, (mtime + 50, mtime + 50))
    assert memory.read_memory_file("x.txt") == "v2"


def test_read_memory_file_clears_cache_on_delete(mem_dir):
    f = mem_dir / "tmp.txt"
    f.write_text("data")
    assert memory.read_memory_file("tmp.txt") == "data"
    f.unlink()
    assert memory.read_memory_file("tmp.txt") is None
    assert "tmp.txt" not in memory._MEMORY_FILE_CACHE
