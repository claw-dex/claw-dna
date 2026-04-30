"""Unit tests for app.data.script."""

from __future__ import annotations

import os

import pytest

from app.data import script


@pytest.fixture
def scripts_dir(tmp_path, monkeypatch):
    d = tmp_path / "scripts"
    d.mkdir()
    monkeypatch.setattr("app.data.script.SCRIPTS_DIR", str(d))
    # Reset caches
    script._SCRIPT_DESC_CACHE.clear()
    from app.data import _cache as cache_mod

    cache_mod._cache_clear_all()
    yield d
    script._SCRIPT_DESC_CACHE.clear()
    cache_mod._cache_clear_all()


# ---------- _script_description ----------


def test_description_from_python_docstring(scripts_dir):
    p = scripts_dir / "foo.py"
    p.write_text('"""This is a foo helper."""\nprint("hi")\n')
    assert script._script_description(str(p)) == "This is a foo helper."


def test_description_from_shell_comment(scripts_dir):
    p = scripts_dir / "foo.sh"
    p.write_text("#!/bin/bash\n# Does some thing\necho hi\n")
    assert script._script_description(str(p)) == "Does some thing"


def test_description_strips_scriptname_prefix(scripts_dir):
    p = scripts_dir / "tool.py"
    p.write_text('"""tool.py — actual description."""\n')
    assert script._script_description(str(p)) == "actual description."


def test_description_strips_scriptname_dash_prefix(scripts_dir):
    p = scripts_dir / "tool.py"
    p.write_text('"""tool.py - dashed desc."""\n')
    assert script._script_description(str(p)) == "dashed desc."


def test_description_multiline_docstring(scripts_dir):
    p = scripts_dir / "x.py"
    p.write_text('"""\nFirst real line of doc.\n"""\n')
    assert script._script_description(str(p)) == "First real line of doc."


def test_description_missing_file(scripts_dir):
    assert script._script_description(str(scripts_dir / "nope.py")) == ""


def test_description_empty_file(scripts_dir):
    p = scripts_dir / "blank.py"
    p.write_text("")
    assert script._script_description(str(p)) == ""


def test_description_uses_mtime_cache(scripts_dir):
    p = scripts_dir / "a.py"
    p.write_text('"""original."""\n')
    first = script._script_description(str(p))
    assert first == "original."
    # Mutate without touching mtime — cached value should still come back
    mtime = os.path.getmtime(p)
    p.write_text('"""changed."""\n')
    os.utime(p, (mtime, mtime))
    cached = script._script_description(str(p))
    assert cached == "original."


# ---------- load_scripts ----------


def test_load_scripts_empty_dir(scripts_dir):
    assert script.load_scripts() == []


def test_load_scripts_lists_py_and_sh(scripts_dir):
    (scripts_dir / "a.py").write_text('"""alpha doc."""\n')
    (scripts_dir / "b.sh").write_text("#!/bin/bash\n# beta doc\n")
    (scripts_dir / "ignored.txt").write_text("ignore me")
    result = script.load_scripts()
    names = [s["name"] for s in result]
    assert "a.py" in names
    assert "b.sh" in names
    assert "ignored.txt" not in names


def test_load_scripts_assigns_categories(scripts_dir):
    (scripts_dir / "cycle_start.py").write_text('"""cycle start."""\n')
    (scripts_dir / "random_helper.py").write_text('"""random."""\n')
    by_name = {s["name"]: s for s in script.load_scripts()}
    assert by_name["cycle_start.py"]["category"] == "Cycle Management"
    assert by_name["random_helper.py"]["category"] == "Other"


def test_load_scripts_includes_metadata(scripts_dir):
    (scripts_dir / "a.py").write_text('"""hello."""\n')
    result = script.load_scripts()
    assert len(result) == 1
    entry = result[0]
    assert entry["size"] > 0
    assert "modified" in entry
    assert entry["description"] == "hello."


def test_load_scripts_uses_dir_mtime_cache(scripts_dir):
    (scripts_dir / "a.py").write_text('"""a."""\n')
    first = script.load_scripts()
    # Add a new file without updating directory mtime
    dir_mtime = os.path.getmtime(scripts_dir)
    (scripts_dir / "b.py").write_text('"""b."""\n')
    os.utime(scripts_dir, (dir_mtime, dir_mtime))
    second = script.load_scripts()
    assert second == first


def test_load_scripts_handles_missing_dir(monkeypatch):
    monkeypatch.setattr("app.data.script.SCRIPTS_DIR", "/nonexistent/zzz/qq")
    from app.data import _cache as cache_mod

    cache_mod._cache_clear_all()
    assert script.load_scripts() == []
