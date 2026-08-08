#!/usr/bin/env python3
"""
search.py — Hybrid ripgrep + BM25S full-text search over /agent/ files.

Flow:
  1. rg pre-filters files in the target dir that contain the query (fast, exact match)
  2. Any new files found by rg are appended to the persistent catalog
  3. All catalog files are checked for stale indices (mtime-based)
  4. BM25S searches every file — large files (>100KB) use a cached on-disk index,
     small files are indexed on the fly
  5. All per-file hits are globally re-ranked by BM25 score
  6. Top results printed to stdout with file path + snippet

Persistent state:
  /home/agent/.bm25s/search_catalog.json  — list of tracked file paths
  /home/agent/.bm25s/search_indices/      — per-file cached BM25 indices (>100KB only)

Usage:
  uv run python scripts/search.py "myspec-coder"
  uv run python scripts/search.py "webhook" --dir /agent/memory/
  uv run python scripts/search.py "evolution" --top 20
  uv run python scripts/search.py "goal failed" --no-rg
  uv run python scripts/search.py "myspec-coder" --json
  uv run python scripts/search.py --rebuild-all   (force-rebuild all stale indices)
  uv run python scripts/search.py --list-catalog   (show tracked files)
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import bm25s

# ── Persistent paths ───────────────────────────────────────────────────────
BM25S_HOME = Path("/home/agent/.bm25s")
INDEX_DIR = BM25S_HOME / "search_indices"
CATALOG_PATH = BM25S_HOME / "search_catalog.json"

# ── Defaults ───────────────────────────────────────────────────────────────
DEFAULT_SEARCH_DIR = "/agent"
LARGE_FILE_THRESHOLD = 100 * 1024  # 100 KB — files above this get a cached index

DEFAULT_CATALOG_FILES = [
    "/agent/memory/journal.json",
    "/agent/memory/journal_archive.json",
    "/agent/memory/goal.json",
    "/agent/memory/goal_history.json",
    "/agent/memory/failures.json",
    "/agent/memory/capabilities.json",
    "/agent/memory/cycles.json",
    "/agent/memory/state.json",
    "/agent/messages/inbox.json",
    "/agent/messages/inbox_history.json",
    "/agent/messages/outbox.json",
    "/agent/messages/outbox_history.json",
]

# rg glob exclusions — only patterns NOT already covered by .gitignore.
# rg respects .gitignore by default, so things like .venv, __pycache__,
# node_modules, .git, *.lock are skipped automatically.
RG_EXCLUDES = [
    # binary / media not listed in .gitignore
    "!*.npy",
    "!*.db",
    "!*.kdbx",
    "!*.lance",
    "!*.lancedb",
    "!*.png",
    "!*.jpg",
    "!*.jpeg",
    "!*.gif",
    "!*.ico",
    "!*.woff*",
    "!*.ttf",
    "!*.zip",
    "!*.tar.gz",
    # index store itself
    "!**/.bm25s/**",
    # high-volume per-cycle log directories — too noisy for general search
    "!**/memory/transcripts/**",
    "!**/memory/logs/**",
    "!**/agent/backup/**",
]

# Directories that .gitignore excludes but searches MUST always cover.
# These are passed as explicit rg search roots so rg traverses into them
# even though `memory/`, `messages/`, `web/`, `workspace/` are gitignored.
FORCED_INCLUDE_DIRS = [
    "/agent/memory",
    "/agent/messages",
    "/agent/workspace",
    "/agent/web",
]

# Maximum number of files rg may auto-add to the catalog in a single run.
# Prevents a broad query from bloating the catalog with thousands of log files.
MAX_RG_CATALOG_ADDITIONS = 20

# Default per-run concurrency for per-file BM25 indexing + retrieval.
# bm25s leans on numpy/scipy which release the GIL, so threads give a real
# wall-clock speedup for catalogs with many >100 KB files.
DEFAULT_WORKERS = 4


# ── Directory bootstrap ────────────────────────────────────────────────────
def ensure_dirs() -> None:
    BM25S_HOME.mkdir(parents=True, exist_ok=True)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)


# ══ Catalog ════════════════════════════════════════════════════════════════


def load_catalog() -> List[str]:
    """Load catalog from disk; seed with defaults if it doesn't exist yet."""
    if not CATALOG_PATH.exists():
        catalog = list(dict.fromkeys(DEFAULT_CATALOG_FILES))  # ordered dedup
        _save_catalog(catalog)
        return catalog
    with open(CATALOG_PATH) as f:
        return json.load(f).get("files", [])


def _save_catalog(files: List[str]) -> None:
    with open(CATALOG_PATH, "w") as f:
        json.dump({"files": files}, f, indent=2)


def add_to_catalog(new_files: List[str], catalog: List[str]) -> Tuple[List[str], int]:
    """
    Append new_files to catalog (skip duplicates).
    Caps auto-additions per run at MAX_RG_CATALOG_ADDITIONS to prevent
    broad queries from bloating the catalog with thousands of log files.
    Returns (updated_catalog, n_added).
    """
    existing = set(catalog)
    added = 0
    for fp in new_files:
        if added >= MAX_RG_CATALOG_ADDITIONS:
            break
        if fp not in existing:
            catalog.append(fp)
            existing.add(fp)
            added += 1
    return catalog, added


# ══ ripgrep ════════════════════════════════════════════════════════════════


def _is_within(path: str, parent: str) -> bool:
    """True iff `path` equals `parent` or is nested inside it."""
    p = path.rstrip("/")
    par = parent.rstrip("/")
    return p == par or p.startswith(par + "/")


def _select_search_roots(
    search_dir: str,
    forced_dirs: List[str],
    exists: Callable[[str], bool] = os.path.exists,
) -> List[str]:
    """
    Compute the rg search roots: always include `search_dir`, plus every
    forced dir that strictly lives inside `search_dir`. Forced dirs need
    explicit roots only because `.gitignore` would otherwise drop them
    during traversal — they must never widen the user's chosen scope.

    - Skip forced dirs missing on disk.
    - Skip a forced dir equal to `search_dir` (exact duplicate root).
    - Skip a forced dir outside `search_dir` (would widen scope).
    - Include forced dirs strictly inside `search_dir`.
    """
    sd = search_dir.rstrip("/")
    roots = [search_dir]
    for fd in forced_dirs:
        if not exists(fd):
            continue
        fd_norm = fd.rstrip("/")
        if fd_norm == sd:
            continue  # exact duplicate
        if not _is_within(fd_norm, sd):
            continue  # forced dir lies outside search_dir → don't widen
        roots.append(fd)
    return roots


def _build_rg_command(
    query: str,
    search_dir: str,
    forced_dirs: Optional[List[str]] = None,
    excludes: Optional[List[str]] = None,
    exists: Callable[[str], bool] = os.path.exists,
) -> List[str]:
    """Assemble the rg argv list. Pure function — easy to unit-test."""
    if forced_dirs is None:
        forced_dirs = FORCED_INCLUDE_DIRS
    if excludes is None:
        excludes = RG_EXCLUDES

    glob_args: List[str] = []
    for g in excludes:
        glob_args += ["--glob", g]

    roots = _select_search_roots(search_dir, forced_dirs, exists=exists)

    return (
        ["rg", "--files-with-matches", "--no-messages", "--follow", "--hidden"]
        + glob_args
        + [query]
        + roots
    )


def rg_find_files(query: str, search_dir: str) -> List[str]:
    """
    Run rg --files-with-matches to get absolute paths of text files
    that contain the query string anywhere. Binary files are skipped
    automatically by rg.
    """
    cmd = _build_rg_command(query, search_dir)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return [
            str(Path(line.strip()).resolve())
            for line in result.stdout.splitlines()
            if line.strip()
        ]
    except FileNotFoundError:
        print("[warn] rg not found — skipping ripgrep pre-filter", file=sys.stderr)
        return []
    except subprocess.TimeoutExpired:
        print("[warn] rg timed out after 30s", file=sys.stderr)
        return []


# ══ File → docs ════════════════════════════════════════════════════════════


def _flatten(obj, sep: str = " ") -> str:
    """Recursively extract all string leaf values from any JSON value."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return sep.join(_flatten(v) for v in obj.values())
    if isinstance(obj, list):
        return sep.join(_flatten(v) for v in obj)
    return str(obj) if obj is not None else ""


def file_to_docs(path: str) -> Tuple[List[str], List[str]]:
    """
    Load a file and return (docs, doc_ids).

    Splitting strategy by file type:
      .json   → one doc per top-level array item or dict value
      .jsonl  → one doc per line
      other   → split on double-newline (paragraphs); fall back to 30-line chunks

    Returns ([], []) on read error or empty content.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"[warn] cannot read {path}: {exc}", file=sys.stderr)
        return [], []

    suffix = p.suffix.lower()

    # ── JSON ────────────────────────────────────────────────────────────
    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            pass  # fall through to plain-text path
        else:
            if isinstance(data, list):
                raw_docs = [(_flatten(item), str(i)) for i, item in enumerate(data)]
            elif isinstance(data, dict):
                raw_docs = [(_flatten(v), str(k)) for k, v in data.items()]
            else:
                raw_docs = [(_flatten(data), "0")]
            pairs = [(d, i) for d, i in raw_docs if d.strip()]
            if pairs:
                docs, ids = zip(*pairs)
                return list(docs), list(ids)
            return [], []

    # ── JSONL ───────────────────────────────────────────────────────────
    if suffix == ".jsonl":
        docs, ids = [], []
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                docs.append(_flatten(json.loads(line)))
            except json.JSONDecodeError:
                docs.append(line)
            ids.append(f"L{lineno}")
        return docs, ids

    # ── Plain text / Markdown / Python / etc. ───────────────────────────
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) >= 3:
        return paragraphs, [f"P{i}" for i in range(len(paragraphs))]

    # Very few paragraphs (e.g. code files) — chunk by 30 lines
    lines = text.splitlines()
    chunk_size = 30
    docs, ids = [], []
    for i in range(0, len(lines), chunk_size):
        chunk = "\n".join(lines[i : i + chunk_size]).strip()
        if chunk:
            docs.append(chunk)
            ids.append(f"L{i + 1}-{min(i + chunk_size, len(lines))}")
    return docs, ids


# ══ Index management ═══════════════════════════════════════════════════════


def _index_dir_for(file_path: str) -> Path:
    """Return a deterministic subdirectory path under INDEX_DIR for a given file."""
    digest = hashlib.md5(file_path.encode()).hexdigest()[:12]
    safe_name = Path(file_path).name.replace(".", "_")
    return INDEX_DIR / f"{safe_name}_{digest}"


def _mtime_stamp(idx_dir: Path) -> Path:
    return idx_dir / "source_mtime"


def _needs_rebuild(file_path: str, idx_dir: Path) -> bool:
    stamp = _mtime_stamp(idx_dir)
    if not idx_dir.exists() or not stamp.exists():
        return True
    try:
        return stamp.read_text().strip() != str(os.path.getmtime(file_path))
    except OSError:
        return True


def _save_mtime(file_path: str, idx_dir: Path) -> None:
    _mtime_stamp(idx_dir).write_text(str(os.path.getmtime(file_path)))


# ══ k selection ════════════════════════════════════════════════════════════


def _k_for_file(file_path: str) -> int:
    """Pick retrieval depth k based on file size."""
    try:
        size = os.path.getsize(file_path)
    except OSError:
        return 5
    if size < LARGE_FILE_THRESHOLD:  # < 100 KB
        return 5
    if size < 500 * 1024:  # 100 KB – 500 KB
        return 10
    return 20  # > 500 KB


# ══ Build / load retriever ═════════════════════════════════════════════════


def _build_retriever(docs: List[str]) -> bm25s.BM25:
    tokens = bm25s.tokenize(docs, stopwords="en", show_progress=False)
    r = bm25s.BM25()
    r.index(tokens)
    del tokens  # free ~4.6 MB per 1 K docs
    return r


def _get_retriever(
    file_path: str,
    docs: List[str],
    force_rebuild: bool = False,
) -> bm25s.BM25:
    """
    Return a BM25 retriever for file_path.
    - Files ≤ 100 KB → always build on the fly (no cache)
    - Files  > 100 KB → load from INDEX_DIR if fresh, else rebuild + save
    """
    file_size = os.path.getsize(file_path)
    use_cache = file_size > LARGE_FILE_THRESHOLD

    if not use_cache:
        return _build_retriever(docs)

    idx_dir = _index_dir_for(file_path)
    if force_rebuild or _needs_rebuild(file_path, idx_dir):
        r = _build_retriever(docs)
        idx_dir.mkdir(parents=True, exist_ok=True)
        r.save(str(idx_dir))
        _save_mtime(file_path, idx_dir)
        return r

    return bm25s.BM25.load(str(idx_dir), load_corpus=False)


# ══ Concurrency helpers ═══════════════════════════════════════════════════


def _resolve_workers(workers: Optional[int]) -> int:
    """Clamp the worker count to a sane range. None / <=0 → DEFAULT_WORKERS."""
    if workers is None or workers <= 0:
        return DEFAULT_WORKERS
    return workers


def _search_files_concurrent(
    files: List[str],
    query: str,
    workers: int = DEFAULT_WORKERS,
    search_fn: Optional[Callable[[str, str], List[Dict]]] = None,
) -> Tuple[List[Dict], int]:
    """Run `search_fn(file, query)` across `files` in a thread pool.

    - Skips files that don't exist on disk (matches the prior sequential
      loop's behaviour).
    - With workers <= 1 (or a single file) runs sequentially to avoid
      thread-pool overhead.
    - Returns (all_hits, n_searched) where order of all_hits does not
      matter — caller re-ranks by BM25 score.
    """
    if search_fn is None:
        search_fn = search_file
    existing = [fp for fp in files if Path(fp).exists()]
    n_searched = len(existing)
    if not existing:
        return [], 0

    if workers <= 1 or len(existing) == 1:
        all_hits: List[Dict] = []
        for fp in existing:
            all_hits.extend(search_fn(fp, query))
        return all_hits, n_searched

    pool_size = min(workers, len(existing))
    all_hits = []
    with ThreadPoolExecutor(max_workers=pool_size) as ex:
        for hits in ex.map(lambda fp: search_fn(fp, query), existing):
            all_hits.extend(hits)
    return all_hits, n_searched


def _rebuild_indices_concurrent(
    files: List[str],
    workers: int = DEFAULT_WORKERS,
) -> List[Tuple[str, str]]:
    """Rebuild cached indices for every >100 KB file in `files`.

    Returns a list of (status, message) tuples in the input file order so the
    caller can print a deterministic log. Status is one of:
    "skip-missing", "skip-small", "skip-empty", "built", "error".

    A single bad file (parse failure, retriever build crash) is captured as
    ("error", "...") instead of propagating, so one corrupt file cannot poison
    the whole batch.
    """

    def _one(fp: str) -> Tuple[str, str]:
        try:
            if not Path(fp).exists():
                return ("skip-missing", f"  skip  {fp}  (missing)")
            size = os.path.getsize(fp)
            if size <= LARGE_FILE_THRESHOLD:
                return (
                    "skip-small",
                    f"  skip  {fp}  ({size/1024:.0f} KB < 100 KB, not cached)",
                )
            docs, _ = file_to_docs(fp)
            if not docs:
                return ("skip-empty", f"  skip  {fp}  (no parseable docs)")
            t0 = time.perf_counter()
            _get_retriever(fp, docs, force_rebuild=True)
            elapsed = (time.perf_counter() - t0) * 1000
            return ("built", f"  built {fp}  ({len(docs)} docs, {elapsed:.0f} ms)")
        except Exception as exc:
            return ("error", f"  error {fp}  ({exc})")

    if workers <= 1 or len(files) <= 1:
        return [_one(fp) for fp in files]

    pool_size = min(workers, len(files))
    with ThreadPoolExecutor(max_workers=pool_size) as ex:
        # Preserve input order for deterministic output.
        return list(ex.map(_one, files))


# ══ Snippet ════════════════════════════════════════════════════════════════


def _snippet(text: str, query: str, length: int = 220) -> str:
    """Return a short excerpt centred around the first query-word hit."""
    lower = text.lower()
    best = -1
    for word in query.lower().split():
        i = lower.find(word)
        if i != -1 and (best == -1 or i < best):
            best = i
    if best == -1:
        best = 0
    start = max(0, best - 60)
    end = min(len(text), start + length)
    excerpt = text[start:end].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + excerpt + ("…" if end < len(text) else "")


# ══ Per-file search ════════════════════════════════════════════════════════


def search_file(
    file_path: str,
    query: str,
    force_rebuild: bool = False,
) -> List[Dict]:
    """
    Search a single file. Returns a list of hit dicts:
      {"file": str, "doc_id": str, "score": float, "snippet": str}
    """
    if not Path(file_path).exists():
        return []

    docs, doc_ids = file_to_docs(file_path)
    if not docs:
        return []

    k = min(_k_for_file(file_path), len(docs))

    try:
        retriever = _get_retriever(file_path, docs, force_rebuild=force_rebuild)
    except Exception as exc:
        print(f"[warn] index error for {file_path}: {exc}", file=sys.stderr)
        return []

    query_tokens = bm25s.tokenize([query], stopwords="en", show_progress=False)
    try:
        results, scores = retriever.retrieve(query_tokens, k=k)
    except Exception as exc:
        print(f"[warn] retrieve error for {file_path}: {exc}", file=sys.stderr)
        return []

    hits = []
    for raw_idx, score in zip(results[0], scores[0]):
        if float(score) == 0.0:
            break
        idx = int(raw_idx)
        hits.append(
            {
                "file": file_path,
                "doc_id": doc_ids[idx],
                "score": float(score),
                "snippet": _snippet(docs[idx], query),
            }
        )
    return hits


# ══ CLI ════════════════════════════════════════════════════════════════════


def _build_json_payload(
    hits: List[Dict],
    query: str,
    n_files: int,
    rg_count: int,
    elapsed_ms: float,
    search_dir: str,
    no_rg: bool,
    top: int,
) -> Dict:
    """Shape the search output as a JSON-serializable dict.

    Keeps the CLI human-readable layout's information content but emits
    structured data: top-N hits with rank, score, doc_id, file path, and
    snippet, plus run metadata (timing, counts, scope).
    """
    top_hits = hits[:top]
    return {
        "query": query,
        "search_dir": search_dir,
        "no_rg": no_rg,
        "rg_count": rg_count,
        "files_searched": n_files,
        "total_hits": len(hits),
        "returned_hits": len(top_hits),
        "elapsed_ms": round(elapsed_ms, 2),
        "hits": [
            {
                "rank": rank,
                "score": round(float(h["score"]), 4),
                "doc_id": h["doc_id"],
                "file": h["file"],
                "snippet": h["snippet"],
            }
            for rank, h in enumerate(top_hits, 1)
        ],
    }


def _print_results_json(
    hits: List[Dict],
    query: str,
    n_files: int,
    rg_count: int,
    elapsed_ms: float,
    search_dir: str,
    no_rg: bool,
    top: int,
) -> None:
    payload = _build_json_payload(
        hits=hits,
        query=query,
        n_files=n_files,
        rg_count=rg_count,
        elapsed_ms=elapsed_ms,
        search_dir=search_dir,
        no_rg=no_rg,
        top=top,
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _print_results(
    hits: List[Dict],
    query: str,
    n_files: int,
    rg_count: int,
    elapsed_ms: float,
    search_dir: str,
    no_rg: bool,
    top: int,
) -> None:
    top_hits = hits[:top]
    rg_note = (
        f"rg skipped (--no-rg)"
        if no_rg
        else f"{rg_count} file(s) found by rg in {search_dir}"
    )
    print(f'\n🔍  Query : "{query}"')
    print(f"    {rg_note} | {n_files} file(s) searched | {elapsed_ms:.0f} ms total")
    print(f"    Showing top {len(top_hits)} of {len(hits)} hit(s)\n")
    print("─" * 82)

    if not top_hits:
        print("  (no results)")
    else:
        for rank, hit in enumerate(top_hits, 1):
            print(
                f"  #{rank:>2}  score={hit['score']:.4f}"
                f"  [doc {hit['doc_id']}]"
                f"  {hit['file']}"
            )
            print(f"       {hit['snippet']}")
            print()

    print("─" * 82 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hybrid ripgrep + BM25S search over /agent/ files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("query", nargs="?", help="Search query string")
    parser.add_argument(
        "--dir",
        "-d",
        default=DEFAULT_SEARCH_DIR,
        metavar="DIR",
        help=f"Directory for rg pre-filter (default: {DEFAULT_SEARCH_DIR})",
    )
    parser.add_argument(
        "--top",
        "-k",
        type=int,
        default=10,
        metavar="N",
        help="Number of top results to display (default: 10)",
    )
    parser.add_argument(
        "--no-rg",
        action="store_true",
        help="Skip ripgrep pre-filter; search only existing catalog files",
    )
    parser.add_argument(
        "--rebuild-all",
        action="store_true",
        help="Force-rebuild all cached indices then exit (no query needed)",
    )
    parser.add_argument(
        "--list-catalog",
        action="store_true",
        help="Print all files currently in the search catalog then exit",
    )
    parser.add_argument(
        "--reset-catalog",
        action="store_true",
        help="Reset catalog to default files only (removes all auto-discovered entries)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the default formatted output",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=DEFAULT_WORKERS,
        metavar="N",
        help=(
            f"Number of concurrent worker threads for per-file BM25 indexing "
            f"and retrieval (default: {DEFAULT_WORKERS}; set to 1 to disable)"
        ),
    )

    args = parser.parse_args()
    ensure_dirs()

    # ── Utility modes ──────────────────────────────────────────────────────
    if args.reset_catalog:
        catalog = list(dict.fromkeys(DEFAULT_CATALOG_FILES))
        _save_catalog(catalog)
        print(f"\nCatalog reset to {len(catalog)} default file(s).\n")
        for fp in catalog:
            print(f"  {fp}")
        print()
        return

    if args.list_catalog:
        catalog = load_catalog()
        print(f"\nSearch catalog ({len(catalog)} file(s))  [{CATALOG_PATH}]\n")
        for fp in catalog:
            exists = "✓" if Path(fp).exists() else "✗"
            size = (
                f"{os.path.getsize(fp)/1024:.1f} KB" if Path(fp).exists() else "missing"
            )
            print(f"  {exists}  {size:>10}  {fp}")
        print()
        return

    if args.rebuild_all:
        catalog = load_catalog()
        workers = _resolve_workers(args.workers)
        print(
            f"\nRebuilding indices for {len(catalog)} catalog file(s) "
            f"({workers} worker(s))…\n"
        )
        for _status, msg in _rebuild_indices_concurrent(catalog, workers=workers):
            print(msg)
        print("\nDone.\n")
        return

    if not args.query:
        parser.print_help()
        sys.exit(1)

    t_start = time.perf_counter()

    # ── 1. Load catalog ────────────────────────────────────────────────────
    catalog = load_catalog()

    # ── 2. rg pre-filter → extend catalog ─────────────────────────────────
    rg_files: List[str] = []
    if not args.no_rg:
        rg_files = rg_find_files(args.query, args.dir)
        catalog, n_added = add_to_catalog(rg_files, catalog)
        if n_added:
            _save_catalog(catalog)

    # ── 3. Scope catalog to --dir when specified ───────────────────────────
    search_root = str(Path(args.dir).resolve())
    is_scoped = args.dir != DEFAULT_SEARCH_DIR
    scoped_catalog = (
        [
            fp
            for fp in catalog
            if fp.startswith(search_root + os.sep) or fp == search_root
        ]
        if is_scoped
        else catalog
    )

    # ── 4. Search scoped catalog files (concurrent thread pool) ───────────
    all_hits, n_searched = _search_files_concurrent(
        scoped_catalog,
        args.query,
        workers=_resolve_workers(args.workers),
    )

    # ── 5. Global re-rank ──────────────────────────────────────────────────
    all_hits.sort(key=lambda h: h["score"], reverse=True)

    # ── 6. Output ──────────────────────────────────────────────────────────
    elapsed_ms = (time.perf_counter() - t_start) * 1000
    printer = _print_results_json if args.json else _print_results
    printer(
        hits=all_hits,
        query=args.query,
        n_files=n_searched,
        rg_count=len(rg_files),
        elapsed_ms=elapsed_ms,
        search_dir=args.dir,
        no_rg=args.no_rg,
        top=args.top,
    )


if __name__ == "__main__":
    main()
