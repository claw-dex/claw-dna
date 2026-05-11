"""Tests for scripts/search.py — pure helpers, rg command builder, and
end-to-end rg invocation against a fixture repo.

Unit tests cover the pure helpers; the rg-integration tests spin up a
throw-away git repo that mirrors the `/agent` layout and confirm:
- .gitignore is respected for unrelated paths,
- the 4 forced /agent/* dirs are searched when inside --dir scope,
- they are NOT searched when --dir is unrelated.

The integration block auto-skips if `rg` or `git` is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

# Stub the `bm25s` dependency so the module imports on platforms where the
# Linux-only wheel is not installed. The tests exercise pure helpers that
# never touch BM25S, so a bare placeholder is sufficient.
if "bm25s" not in sys.modules:
    stub = types.ModuleType("bm25s")
    stub.BM25 = type("BM25", (), {})  # type: ignore[attr-defined]
    stub.tokenize = lambda *a, **k: []  # type: ignore[attr-defined]
    sys.modules["bm25s"] = stub

import search as s  # noqa: E402

# ── _build_rg_command / _select_search_roots ──────────────────────────────


def test_build_rg_command_includes_hidden_flag():
    cmd = s._build_rg_command("foo", "/agent", exists=lambda _p: True)
    assert "--hidden" in cmd


def test_build_rg_command_passes_all_excludes_as_globs():
    cmd = s._build_rg_command(
        "foo",
        "/agent",
        excludes=["!*.npy", "!**/.bm25s/**"],
        exists=lambda _p: True,
    )
    pairs = list(zip(cmd, cmd[1:]))
    assert ("--glob", "!*.npy") in pairs
    assert ("--glob", "!**/.bm25s/**") in pairs


def test_build_rg_command_query_precedes_roots():
    cmd = s._build_rg_command(
        "foo",
        "/agent",
        forced_dirs=["/agent/memory"],
        excludes=[],
        exists=lambda _p: True,
    )
    qi = cmd.index("foo")
    # query immediately precedes the root list; both roots come after.
    assert cmd[qi + 1 :] == ["/agent", "/agent/memory"]


def test_build_rg_command_default_agent_appends_all_forced_dirs():
    cmd = s._build_rg_command(
        "foo",
        "/agent",
        forced_dirs=[
            "/agent/memory",
            "/agent/messages",
            "/agent/web",
            "/agent/workspace",
        ],
        excludes=[],
        exists=lambda _p: True,
    )
    # all four forced dirs must appear as roots, plus the user's /agent root
    for d in (
        "/agent",
        "/agent/memory",
        "/agent/messages",
        "/agent/web",
        "/agent/workspace",
    ):
        assert d in cmd


def test_select_search_roots_skips_missing_forced_dirs():
    # A forced dir that doesn't exist on disk must never appear as a root.
    present = {"/agent/memory"}
    roots = s._select_search_roots(
        "/agent",
        ["/agent/memory", "/agent/messages"],
        exists=lambda p: p in present,
    )
    assert roots == ["/agent", "/agent/memory"]


def test_select_search_roots_keeps_forced_dirs_inside_search_dir():
    # Forced dirs are gitignored, so even when nested inside search_dir
    # they must be passed as explicit rg roots to bypass .gitignore.
    roots = s._select_search_roots(
        "/agent",
        ["/agent/memory", "/agent/messages"],
        exists=lambda _p: True,
    )
    assert roots == ["/agent", "/agent/memory", "/agent/messages"]


def test_select_search_roots_skips_exact_duplicate():
    roots = s._select_search_roots(
        "/agent/memory",
        ["/agent/memory", "/agent/messages"],
        exists=lambda _p: True,
    )
    # /agent/messages is unrelated to /agent/memory → not added (no widening).
    assert roots == ["/agent/memory"]


def test_select_search_roots_narrow_scope_inside_forced_dir():
    """search_dir inside a forced dir → roots stay narrow, no widening."""
    roots = s._select_search_roots(
        "/agent/memory/sub",
        ["/agent/memory", "/agent/messages"],
        exists=lambda _p: True,
    )
    # /agent/memory is a parent of /agent/memory/sub → must NOT be added
    assert roots == ["/agent/memory/sub"]


def test_select_search_roots_unrelated_scope_drops_forced():
    # An unrelated scope must NOT widen to include the forced agent dirs.
    roots = s._select_search_roots(
        "/unrelated",
        ["/agent/memory", "/agent/web"],
        exists=lambda _p: True,
    )
    assert roots == ["/unrelated"]


# ── add_to_catalog ────────────────────────────────────────────────────────


def test_add_to_catalog_dedups_and_caps(monkeypatch):
    monkeypatch.setattr(s, "MAX_RG_CATALOG_ADDITIONS", 3)
    catalog = ["/a"]
    new = ["/a", "/b", "/c", "/d", "/e"]
    out, n = s.add_to_catalog(new, catalog)
    assert n == 3
    assert out == ["/a", "/b", "/c", "/d"]


def test_add_to_catalog_no_new_entries():
    catalog = ["/a", "/b"]
    out, n = s.add_to_catalog(["/a", "/b"], catalog)
    assert n == 0
    assert out == ["/a", "/b"]


# ── _flatten ──────────────────────────────────────────────────────────────


def test_flatten_string():
    assert s._flatten("hello") == "hello"


def test_flatten_dict():
    out = s._flatten({"k": "v", "k2": "v2"})
    assert "v" in out and "v2" in out


def test_flatten_list_nested():
    out = s._flatten(["a", {"k": "b"}, ["c"]])
    assert "a" in out and "b" in out and "c" in out


def test_flatten_none():
    assert s._flatten(None) == ""


def test_flatten_number():
    assert s._flatten(42) == "42"


# ── file_to_docs ──────────────────────────────────────────────────────────


def test_file_to_docs_json_list(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps([{"k": "alpha"}, {"k": "beta"}]))
    docs, ids = s.file_to_docs(str(p))
    assert docs == ["alpha", "beta"]
    assert ids == ["0", "1"]


def test_file_to_docs_json_dict(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"first": "alpha", "second": "beta"}))
    docs, ids = s.file_to_docs(str(p))
    assert set(zip(ids, docs)) == {("first", "alpha"), ("second", "beta")}


def test_file_to_docs_jsonl(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"k":"alpha"}\n\n{"k":"beta"}\n')
    docs, ids = s.file_to_docs(str(p))
    assert docs == ["alpha", "beta"]
    assert ids == ["L1", "L3"]


def test_file_to_docs_jsonl_invalid_line_kept(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("not json\n")
    docs, ids = s.file_to_docs(str(p))
    assert docs == ["not json"]
    assert ids == ["L1"]


def test_file_to_docs_paragraphs(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("para one\n\npara two\n\npara three")
    docs, ids = s.file_to_docs(str(p))
    assert docs == ["para one", "para two", "para three"]
    assert ids == ["P0", "P1", "P2"]


def test_file_to_docs_small_chunks_by_lines(tmp_path):
    p = tmp_path / "x.py"
    # 35 lines, no double-newlines → fewer than 3 paragraphs → 30-line chunks
    p.write_text("\n".join(f"line{i}" for i in range(35)))
    docs, ids = s.file_to_docs(str(p))
    assert len(docs) == 2
    assert ids == ["L1-30", "L31-35"]


def test_file_to_docs_empty(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("")
    docs, ids = s.file_to_docs(str(p))
    assert docs == [] and ids == []


def test_file_to_docs_missing(tmp_path):
    docs, ids = s.file_to_docs(str(tmp_path / "absent.json"))
    assert docs == [] and ids == []


# ── _snippet ──────────────────────────────────────────────────────────────


def test_snippet_centres_on_first_hit():
    text = "x" * 200 + " hello world " + "y" * 200
    out = s._snippet(text, "hello", length=80)
    assert "hello" in out
    assert out.startswith("…") and out.endswith("…")


def test_snippet_no_match_returns_prefix():
    text = "abcdefg"
    out = s._snippet(text, "xyz", length=10)
    assert out == "abcdefg"


def test_snippet_short_text_no_ellipsis():
    out = s._snippet("hello world", "hello", length=80)
    assert out == "hello world"


# ── _k_for_file ───────────────────────────────────────────────────────────


def test_k_for_file_small(tmp_path):
    p = tmp_path / "small.txt"
    p.write_text("x")
    assert s._k_for_file(str(p)) == 5


def test_k_for_file_medium(tmp_path):
    p = tmp_path / "med.txt"
    p.write_bytes(b"x" * (200 * 1024))  # 200 KB
    assert s._k_for_file(str(p)) == 10


def test_k_for_file_large(tmp_path):
    p = tmp_path / "big.txt"
    p.write_bytes(b"x" * (600 * 1024))  # 600 KB
    assert s._k_for_file(str(p)) == 20


def test_k_for_file_missing(tmp_path):
    assert s._k_for_file(str(tmp_path / "absent")) == 5


# ── _index_dir_for ────────────────────────────────────────────────────────


def test_index_dir_for_deterministic():
    a = s._index_dir_for("/agent/memory/foo.json")
    b = s._index_dir_for("/agent/memory/foo.json")
    assert a == b


def test_index_dir_for_distinct_paths_differ():
    a = s._index_dir_for("/agent/memory/foo.json")
    b = s._index_dir_for("/agent/memory/bar.json")
    assert a != b


def test_index_dir_for_safe_filename():
    p = s._index_dir_for("/some/path/with.dots.json")
    # dots in basename are replaced with underscores
    assert "with_dots_json" in p.name


# ── --json output ─────────────────────────────────────────────────────────


def _sample_hits():
    return [
        {
            "file": "/agent/memory/journal.json",
            "doc_id": "17",
            "score": 4.78213,
            "snippet": "first",
        },
        {
            "file": "/agent/messages/inbox.jsonl",
            "doc_id": "L42",
            "score": 3.12044,
            "snippet": "second",
        },
        {
            "file": "/agent/web/notes.md",
            "doc_id": "P5",
            "score": 2.85932,
            "snippet": "third",
        },
    ]


def test_build_json_payload_top_level_keys():
    payload = s._build_json_payload(
        hits=_sample_hits(),
        query="webhook",
        n_files=8,
        rg_count=5,
        elapsed_ms=142.319,
        search_dir="/agent",
        no_rg=False,
        top=10,
    )
    assert payload["query"] == "webhook"
    assert payload["search_dir"] == "/agent"
    assert payload["no_rg"] is False
    assert payload["rg_count"] == 5
    assert payload["files_searched"] == 8
    assert payload["total_hits"] == 3
    assert payload["returned_hits"] == 3
    # elapsed_ms is rounded to 2 decimals
    assert payload["elapsed_ms"] == 142.32


def test_build_json_payload_hit_shape_and_rank():
    payload = s._build_json_payload(
        hits=_sample_hits(),
        query="q",
        n_files=3,
        rg_count=3,
        elapsed_ms=10.0,
        search_dir="/agent",
        no_rg=False,
        top=10,
    )
    hits = payload["hits"]
    assert [h["rank"] for h in hits] == [1, 2, 3]
    assert hits[0] == {
        "rank": 1,
        "score": 4.7821,  # rounded to 4 decimals
        "doc_id": "17",
        "file": "/agent/memory/journal.json",
        "snippet": "first",
    }


def test_build_json_payload_top_caps_returned_hits():
    payload = s._build_json_payload(
        hits=_sample_hits(),
        query="q",
        n_files=3,
        rg_count=3,
        elapsed_ms=10.0,
        search_dir="/agent",
        no_rg=False,
        top=2,
    )
    assert payload["total_hits"] == 3
    assert payload["returned_hits"] == 2
    assert len(payload["hits"]) == 2
    assert [h["rank"] for h in payload["hits"]] == [1, 2]


def test_build_json_payload_empty_hits():
    payload = s._build_json_payload(
        hits=[],
        query="nope",
        n_files=0,
        rg_count=0,
        elapsed_ms=1.0,
        search_dir="/agent",
        no_rg=True,
        top=10,
    )
    assert payload["total_hits"] == 0
    assert payload["returned_hits"] == 0
    assert payload["hits"] == []
    assert payload["no_rg"] is True


def test_print_results_json_emits_valid_json(capsys):
    s._print_results_json(
        hits=_sample_hits(),
        query="webhook",
        n_files=8,
        rg_count=5,
        elapsed_ms=142.0,
        search_dir="/agent",
        no_rg=False,
        top=10,
    )
    captured = capsys.readouterr()
    # stdout must parse as JSON (no surrounding text, no banner)
    parsed = json.loads(captured.out)
    assert parsed["query"] == "webhook"
    assert len(parsed["hits"]) == 3
    # File paths preserved exactly
    assert parsed["hits"][0]["file"] == "/agent/memory/journal.json"
    # Pretty-printed (indent=2) → multi-line
    assert "\n" in captured.out
    # Nothing on stderr
    assert captured.err == ""


def test_print_results_json_unicode_preserved(capsys):
    s._print_results_json(
        hits=[
            {
                "file": "/tmp/résumé.md",
                "doc_id": "P0",
                "score": 1.0,
                "snippet": "café — naïve façade",
            }
        ],
        query="café",
        n_files=1,
        rg_count=1,
        elapsed_ms=1.0,
        search_dir="/tmp",
        no_rg=False,
        top=10,
    )
    out = capsys.readouterr().out
    parsed = json.loads(out)
    # ensure_ascii=False keeps unicode literal in the output
    assert "café" in out
    assert parsed["hits"][0]["snippet"] == "café — naïve façade"
    assert parsed["hits"][0]["file"] == "/tmp/résumé.md"


# ── End-to-end: real rg invocation against a fixture repo ─────────────────
#
# These tests exercise the actual rg binary against a temp git repo whose
# layout mirrors /agent. They verify the gitignore-override behavior promised
# by `_build_rg_command` — specifically that:
#   - the 4 forced agent dirs ARE searched when inside the --dir scope,
#   - they are NOT searched when --dir points outside them,
#   - .gitignore is otherwise respected (e.g. .venv/, node_modules/),
#   - --hidden flag still descends into hidden non-ignored dirs (.github/).


_NEEDS_RG_GIT = pytest.mark.skipif(
    shutil.which("rg") is None or shutil.which("git") is None,
    reason="requires both rg and git on PATH",
)


@pytest.fixture
def fixture_repo(tmp_path):
    """Build a temp git repo mirroring the agent layout.

    Layout:
      <root>/
        .gitignore           ← ignores memory/, messages/, web/, workspace/,
                               .venv/, node_modules/
        src/file.txt         (NEEDLE)            — searchable
        agent/memory/m.txt   (NEEDLE)            — gitignored, force-included
        agent/messages/x.txt (NEEDLE)            — gitignored, force-included
        agent/web/w.txt      (NEEDLE)            — gitignored, force-included
        agent/workspace/k.txt(NEEDLE)            — gitignored, force-included
        agent/memory/logs/skip.txt (NEEDLE)      — explicitly excluded glob
        agent/other/o.txt    (NEEDLE)            — not gitignored, searched
        unrelated/u.txt      (NEEDLE)            — not gitignored, searched
        .venv/v.txt          (NEEDLE)            — gitignored, must stay out
        node_modules/n.txt   (NEEDLE)            — gitignored, must stay out
        .github/h.yml        (NEEDLE)            — hidden, must appear (--hidden)
    """
    root = tmp_path / "repo"
    root.mkdir()

    (root / ".gitignore").write_text(
        "memory/\nmessages/\nweb/\nworkspace/\n.venv/\nnode_modules/\n"
    )

    files = {
        "src/file.txt": "NEEDLE",
        "agent/memory/m.txt": "NEEDLE",
        "agent/messages/x.txt": "NEEDLE",
        "agent/web/w.txt": "NEEDLE",
        "agent/workspace/k.txt": "NEEDLE",
        "agent/memory/logs/skip.txt": "NEEDLE",
        "agent/other/o.txt": "NEEDLE",
        "unrelated/u.txt": "NEEDLE",
        ".venv/v.txt": "NEEDLE",
        "node_modules/n.txt": "NEEDLE",
        ".github/h.yml": "NEEDLE",
    }
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)

    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    return root


def _run_rg(query, search_dir, forced_dirs, root):
    """Build the command via the production builder, run rg, return relative
    paths of files that matched."""
    cmd = s._build_rg_command(
        query,
        str(search_dir),
        forced_dirs=[str(root / fd.lstrip("/")) for fd in forced_dirs],
        # Same exclude policy the production code ships with.
        excludes=s.RG_EXCLUDES,
    )
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
    hits = sorted(
        str(Path(line).resolve().relative_to(root))
        for line in out.stdout.splitlines()
        if line.strip()
    )
    return hits


_FORCED = ["/agent/memory", "/agent/messages", "/agent/web", "/agent/workspace"]


@_NEEDS_RG_GIT
def test_e2e_dir_agent_includes_forced_dirs_and_respects_gitignore(fixture_repo):
    """--dir=<repo>/agent should hit force-included gitignored dirs but
    still skip .venv, node_modules, memory/logs (excluded by --glob)."""
    hits = _run_rg("NEEDLE", fixture_repo / "agent", _FORCED, fixture_repo)

    # Force-included gitignored dirs are searched:
    assert "agent/memory/m.txt" in hits
    assert "agent/messages/x.txt" in hits
    assert "agent/web/w.txt" in hits
    assert "agent/workspace/k.txt" in hits
    # Non-ignored dir under --dir is searched:
    assert "agent/other/o.txt" in hits
    # The explicit --glob exclusion still wins over the force-include:
    assert "agent/memory/logs/skip.txt" not in hits
    # Outside --dir: not searched at all (rg only walks the given roots):
    assert "src/file.txt" not in hits
    assert "unrelated/u.txt" not in hits
    # Outside-scope gitignored dirs: definitely not searched:
    assert ".venv/v.txt" not in hits
    assert "node_modules/n.txt" not in hits


@_NEEDS_RG_GIT
def test_e2e_dir_unrelated_excludes_forced_dirs(fixture_repo):
    """--dir=<repo>/unrelated must NOT widen to the 4 /agent dirs."""
    hits = _run_rg("NEEDLE", fixture_repo / "unrelated", _FORCED, fixture_repo)

    assert hits == ["unrelated/u.txt"]
    # Sanity: the 4 forced dirs are absent.
    for f in (
        "agent/memory/m.txt",
        "agent/messages/x.txt",
        "agent/web/w.txt",
        "agent/workspace/k.txt",
    ):
        assert f not in hits


@_NEEDS_RG_GIT
def test_e2e_dir_repo_root_respects_gitignore(fixture_repo):
    """--dir=<repo> (root) — gitignore drops memory/messages/web/workspace,
    .venv, node_modules; --hidden allows .github/ to be searched; the four
    forced roots re-include the agent state dirs."""
    hits = _run_rg("NEEDLE", fixture_repo, _FORCED, fixture_repo)

    # gitignored, NOT force-included → must be skipped:
    assert ".venv/v.txt" not in hits
    assert "node_modules/n.txt" not in hits
    # gitignored but force-included via the 4 roots → searched:
    assert "agent/memory/m.txt" in hits
    assert "agent/messages/x.txt" in hits
    assert "agent/web/w.txt" in hits
    assert "agent/workspace/k.txt" in hits
    # Non-ignored content searched normally:
    assert "src/file.txt" in hits
    assert "unrelated/u.txt" in hits
    assert "agent/other/o.txt" in hits
    # --hidden makes .github/ visible (it's not gitignored):
    assert ".github/h.yml" in hits
    # Manual --glob exclusion holds:
    assert "agent/memory/logs/skip.txt" not in hits


@_NEEDS_RG_GIT
def test_e2e_dir_inside_forced_dir_does_not_widen(fixture_repo):
    """--dir=<repo>/agent/memory — forced dirs must not widen scope."""
    hits = _run_rg("NEEDLE", fixture_repo / "agent" / "memory", _FORCED, fixture_repo)

    assert "agent/memory/m.txt" in hits
    # Other forced dirs must NOT be added (would widen the user's scope):
    assert "agent/messages/x.txt" not in hits
    assert "agent/web/w.txt" not in hits
    assert "agent/workspace/k.txt" not in hits
    # The exclude glob still applies inside the narrowed scope:
    assert "agent/memory/logs/skip.txt" not in hits
