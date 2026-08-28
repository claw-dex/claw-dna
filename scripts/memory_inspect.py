#!/usr/bin/env python3
"""
memory_inspect.py — Inspect the long-term memory store (LanceDB).

Read-only diagnostics for the LanceDB store. Reports on-disk footprint (the
store directory plus any sibling rebuild/backup directories), row count,
source/label/tag distribution, text-length distribution, and timestamp span.
Also compares the indexed counts against the source JSON files (journal,
journal archive, inbox history) so you can spot stale or duplicated records.

Usage:
    uv run python scripts/memory_inspect.py
    uv run python scripts/memory_inspect.py --db /agent/memory/long_term_memory.lancedb
    uv run python scripts/memory_inspect.py --top-tags 30
    uv run python scripts/memory_inspect.py --sample 5         # show 5 raw rows
    uv run python scripts/memory_inspect.py --json
    uv run python scripts/memory_inspect.py --api               # dump table API surface
    uv run python scripts/memory_inspect.py --stats             # embedding/index health check
    uv run python scripts/memory_inspect.py --stats --json      # machine-readable stats

Optional:
    --db PATH         Path to the LanceDB store (default: /agent/memory/long_term_memory.lancedb)
    --memory PATH     Path to the memory directory holding the source JSON files
                      (journal/journal_archive/inbox_history). Defaults to the parent
                      directory of --db, so passing --db alone is enough for
                      most cases.
    --limit N         Max rows to scan (default: 200000)
    --top-tags N      How many top tags to print (default: 20)
    --sample N        Print N raw rows (default: 0)
    --deep            Add version history, fragment stats and per-file disk usage
    --row ID          Fetch a single row by its id and print full content
    --api             Print dir(table) for the opened handle and exit
    --json            Output report as JSON
    --stats           Report index health and compare the stored vector dimension
                      against memory_store.EMBED_DIM. Exit code 2 = MISMATCH.

Exit codes: 0 = success, 1 = error (store not found, query error), 2 = dimension mismatch
"""

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from scripts import memory_store as store
from scripts.memory_store import DEFAULT_DB, EMBED_DIM, EMBED_MODEL


def parse_args(argv):
    args = argv[1:]
    # `db` and `memory` start as None so we can tell explicit overrides apart
    # from defaults — when only `--db` is given, we auto-derive `memory` from
    # the store's parent directory so source-JSON comparison works on
    # non-default stores.
    result = {
        "db": None,
        "memory": None,
        "limit": 200000,
        "top_tags": 20,
        "sample": 0,
        "deep": False,
        "row": None,
        "api": False,
        "json_mode": False,
        "help": False,
        "stats": False,
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            result["help"] = True
        elif a == "--db" and i + 1 < len(args):
            i += 1
            result["db"] = args[i]
        elif a == "--memory" and i + 1 < len(args):
            i += 1
            result["memory"] = args[i]
        elif a in ("--limit", "--top-tags", "--sample") and i + 1 < len(args):
            key = a.lstrip("-").replace("-", "_")
            i += 1
            try:
                result[key] = int(args[i])
            except ValueError:
                print(
                    f"ERROR: {a} must be an integer, got: {args[i]!r}", file=sys.stderr
                )
                sys.exit(1)
        elif a == "--deep":
            result["deep"] = True
        elif a == "--row" and i + 1 < len(args):
            i += 1
            result["row"] = args[i]
        elif a == "--api":
            result["api"] = True
        elif a == "--json":
            result["json_mode"] = True
        elif a == "--stats":
            result["stats"] = True
        i += 1

    # Resolve defaults — `--db` falls back to DEFAULT_DB; `--memory`
    # auto-derives from the store's parent so callers only need to pass `--db`.
    if result["db"] is None:
        result["db"] = str(DEFAULT_DB)
    if result["memory"] is None:
        result["memory"] = str(Path(result["db"]).resolve().parent)
    return result


# ── File-system inspection ──────────────────────────────────────────────────


def inspect_files(db_path: Path) -> dict:
    """Measure the store directory and any sibling rebuild/backup directories."""
    parent = db_path.parent
    base = db_path.name
    siblings = []
    if parent.exists():
        for p in sorted(parent.iterdir()):
            if p.name == base or not p.name.startswith(base):
                continue
            try:
                siblings.append(
                    {
                        "name": p.name,
                        "size": store.dir_size(p),
                        "mtime": datetime.fromtimestamp(
                            p.stat().st_mtime, tz=timezone.utc
                        ).isoformat(),
                    }
                )
            except OSError as e:
                siblings.append({"name": p.name, "error": str(e)})
    return {
        "db": str(db_path),
        "exists": db_path.exists(),
        "size": store.dir_size(db_path) if db_path.exists() else 0,
        "siblings": siblings,
    }


def _open(db_path: Path):
    """Open the memories table, or exit 1 with a clear message."""
    try:
        tbl = store.open_table(db_path)
    except Exception as e:
        print(f"ERROR: cannot open {db_path}: {e}", file=sys.stderr)
        sys.exit(1)
    if tbl is None:
        print(
            f"ERROR: no '{store.TABLE_NAME}' table in {db_path}. "
            "Run: uv run python scripts/memory_ingest.py --build",
            file=sys.stderr,
        )
        sys.exit(1)
    return tbl


# ── Index inspection ────────────────────────────────────────────────────────

_BUCKET_ORDER = ["<100B", "<1KB", "<10KB", "<100KB", "<1MB", ">=1MB"]


def _bucket(n: int) -> str:
    """Bucket a byte count into a coarse size class."""
    if n < 100:
        return "<100B"
    if n < 1024:
        return "<1KB"
    if n < 10 * 1024:
        return "<10KB"
    if n < 100 * 1024:
        return "<100KB"
    if n < 1024 * 1024:
        return "<1MB"
    return ">=1MB"


def _ts_to_iso(ts) -> str:
    """Render a unix timestamp as ISO-8601 UTC, or "?" when unusable."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


SCAN_COLUMNS = ["id", "title", "label", "text", "tags", "source", "date", "ts"]


def inspect_index(db_path: Path, limit: int) -> dict:
    """Scan the table once and aggregate every distribution the report shows."""
    tbl = _open(db_path)
    try:
        total_rows = tbl.count_rows()
    except Exception as e:
        return {"error": f"count_rows failed: {type(e).__name__}: {e}"}

    try:
        rows = tbl.search().select(SCAN_COLUMNS).limit(max(limit, 1)).to_list()
    except Exception as e:
        return {"error": f"scan failed: {type(e).__name__}: {e}"}

    label_counts: Counter = Counter()
    source_counts: Counter = Counter()
    tag_counts: Counter = Counter()
    date_counts: Counter = Counter()
    bucket_counts: Counter = Counter()
    id_counts: Counter = Counter()

    text_total = 0
    text_max = 0
    ts_min = None
    ts_max = None

    for row in rows:
        text = row.get("text") or ""
        size = len(text.encode("utf-8", errors="ignore"))
        text_total += size
        text_max = max(text_max, size)
        bucket_counts[_bucket(size)] += 1

        label_counts[row.get("label") or "(none)"] += 1
        source_counts[row.get("source") or "(none)"] += 1
        id_counts[row.get("id") or ""] += 1
        if row.get("date"):
            date_counts[row["date"]] += 1
        for tag in row.get("tags") or []:
            tag_counts[tag] += 1

        ts = row.get("ts")
        if isinstance(ts, int):
            ts_min = ts if ts_min is None else min(ts_min, ts)
            ts_max = ts if ts_max is None else max(ts_max, ts)

    duplicates = sum(c - 1 for c in id_counts.values() if c > 1)

    return {
        "rows_total": total_rows,
        "rows_seen": len(rows),
        "limit": limit,
        "truncated": len(rows) < total_rows,
        "duplicate_ids": duplicates,
        "text_total_bytes": text_total,
        "text_max_bytes": text_max,
        "text_size_buckets": dict(bucket_counts),
        "label_counts": dict(label_counts),
        "source_counts": dict(source_counts),
        "top_tags": tag_counts,
        "top_dates": date_counts,
        "timestamp_min": ts_min,
        "timestamp_max": ts_max,
    }


def inspect_stats(db_path: Path) -> dict:
    """Report index health and verify the stored vector dimension.

    Key field `dimension_aligned` is:
      True  — the vector column width matches memory_store.EMBED_DIM
      False — MISMATCH: the store was built with a different embedding model
              and every semantic query is meaningless until it is rebuilt
      None  — could not determine
    """
    tbl = _open(db_path)

    stored_dim = store.vector_dimension(tbl)
    aligned = None if stored_dim is None else (stored_dim == EMBED_DIM)

    indexes = []
    try:
        for idx in tbl.list_indices():
            entry = {
                "name": getattr(idx, "name", None),
                "type": getattr(idx, "index_type", None),
                "columns": list(getattr(idx, "columns", []) or []),
            }
            try:
                istats = tbl.index_stats(entry["name"])
                entry["indexed_rows"] = getattr(istats, "num_indexed_rows", None)
                entry["unindexed_rows"] = getattr(istats, "num_unindexed_rows", None)
            except Exception:
                pass
            indexes.append(entry)
    except Exception as e:
        return {"error": f"list_indices failed: {type(e).__name__}: {e}"}

    result = {
        "expected_model": EMBED_MODEL,
        "expected_dimension": EMBED_DIM,
        "stored_dimension": stored_dim,
        "dimension_aligned": aligned,
        "indexes": indexes,
        "has_vec_index": any(str(i.get("type", "")).startswith("Ivf") for i in indexes),
        "has_fts_index": any(i.get("type") == "FTS" for i in indexes),
        "size_bytes": store.dir_size(db_path),
    }

    try:
        result["row_count"] = tbl.count_rows()
    except Exception:
        result["row_count"] = None
    try:
        result["version"] = tbl.version
    except Exception:
        result["version"] = None

    # Retained versions are the storage-health number: LanceDB is copy-on-write,
    # so un-reclaimed versions are what makes the store grow out of proportion
    # to the data.
    result["retained_versions"] = store.version_count(tbl)
    result["compact_threshold"] = store.COMPACT_VERSION_THRESHOLD
    stamp = store.compact_stamp_path(db_path)
    try:
        result["last_compacted"] = datetime.fromtimestamp(
            stamp.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    except OSError:
        result["last_compacted"] = None
    try:
        import lancedb

        result["lancedb_version"] = getattr(lancedb, "__version__", None)
    except Exception:
        pass

    return result


def deep_inspect(db_path: Path) -> dict:
    """Version history, fragment statistics and per-file disk usage."""
    tbl = _open(db_path)
    out: dict = {}

    try:
        out["stats"] = tbl.stats()
    except Exception as e:
        out["stats_error"] = f"{type(e).__name__}: {e}"

    try:
        versions = tbl.list_versions()
        out["version_count"] = len(versions)
        out["versions"] = versions[-10:]
    except Exception as e:
        out["versions_error"] = f"{type(e).__name__}: {e}"

    # Largest files on disk — shows whether space is going to data, indexes or
    # accumulated manifests from many small writes.
    files = []
    for root, _dirs, names in os.walk(db_path, onerror=lambda _e: None):
        for name in names:
            full = os.path.join(root, name)
            try:
                files.append(
                    {
                        "path": os.path.relpath(full, str(db_path)),
                        "size": os.path.getsize(full),
                    }
                )
            except OSError:
                continue
    files.sort(key=lambda f: f["size"], reverse=True)
    out["file_count"] = len(files)
    out["largest_files"] = files[:25]
    return out


# ── Source JSON comparison ──────────────────────────────────────────────────


def _safe_load_list(p: Path) -> int:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return len(data) if isinstance(data, list) else 0
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def _load_with_legacy(memory_dir: Path, name: str, legacy_name: str) -> int:
    """Count entries from `name`, falling back to `legacy_name` if the new file is absent."""
    new_path = memory_dir / name
    if new_path.exists():
        return _safe_load_list(new_path)
    return _safe_load_list(memory_dir / legacy_name)


def inspect_sources(memory_dir: Path) -> dict:
    """Count entries in each source JSON the build path would ingest."""
    msgs_dir = memory_dir.resolve().parent / "messages"
    return {
        "journal.json": _safe_load_list(memory_dir / "journal.json"),
        "journal_archive.json": _load_with_legacy(
            memory_dir, "journal_archive.json", "journal-archive.json"
        ),
        "messages/inbox_history.json": _safe_load_list(msgs_dir / "inbox_history.json"),
        "messages/inbox.json": _safe_load_list(msgs_dir / "inbox.json"),
    }


# ── Reporting ───────────────────────────────────────────────────────────────


def _fmt_bytes(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def print_stats_report(s: dict) -> None:
    """Human-readable output for --stats."""
    if "error" in s:
        print(f"ERROR: {s['error']}")
        return

    aligned = s["dimension_aligned"]
    if aligned is True:
        status = "✅ ALIGNED"
    elif aligned is False:
        status = "❌ MISMATCH"
    else:
        status = "⚠️  UNKNOWN"

    print("── Embedding ───────────────────────────────────────────────────────")
    print(f"  Configured model:     {s['expected_model']}")
    print(f"  Expected dimension:   {s['expected_dimension']}")
    print(f"  Stored dimension:     {s.get('stored_dimension', '?')}")
    print(f"  Dimension alignment:  {status}")
    if aligned is False:
        print("\n  ⚠️  Fix: uv run python scripts/memory_ingest.py --build")

    print("\n── Index Health ────────────────────────────────────────────────────")
    print(f"  Rows:                 {s.get('row_count', '?')}")
    print(f"  Store size:           {_fmt_bytes(s.get('size_bytes'))}")
    print(f"  Has full-text index:  {'✅' if s.get('has_fts_index') else '❌'}")
    print(
        f"  Has vector index:     "
        f"{'✅' if s.get('has_vec_index') else '— (brute-force scan)'}"
    )
    for idx in s.get("indexes") or []:
        detail = f"{idx.get('type')} on {', '.join(idx.get('columns') or [])}"
        pending = idx.get("unindexed_rows")
        if pending:
            detail += f"  ({pending} row(s) not yet indexed)"
        name = str(idx.get("name") or "?")
        print(f"    - {name:<24} {detail}")
    print(f"  Table version:        {s.get('version', '?')}")
    retained = s.get("retained_versions")
    threshold = s.get("compact_threshold")
    note = ""
    if isinstance(retained, int) and isinstance(threshold, int):
        note = "  (compaction due)" if retained >= threshold else ""
    print(f"  Retained versions:    {retained}{note}")
    print(f"  Last compacted:       {s.get('last_compacted') or 'never'}")
    if s.get("lancedb_version"):
        print(f"  LanceDB version:      {s['lancedb_version']}")


def print_text_report(report: dict, top_tags: int) -> None:
    files = report["files"]
    idx = report["index"]
    src = report["sources"]

    print("── Store ───────────────────────────────────────────────")
    print(f"  path: {files['db']}")
    print(f"  size: {_fmt_bytes(files['size'])}  (exists={files['exists']})")
    print(f"  siblings ({len(files['siblings'])}):")
    for s in files["siblings"]:
        if "error" in s:
            print(f"    ! {s['name']}: {s['error']}")
        else:
            print(f"    - {s['name']:<48} {_fmt_bytes(s['size']):>10}  {s['mtime']}")

    print("\n── Source JSON counts (what --build would ingest) ──────")
    total_src = 0
    for k, v in src.items():
        print(f"  {k:<32} {v}")
        total_src += v
    print(f"  {'TOTAL':<32} {total_src}")

    print("\n── Index ───────────────────────────────────────────────")
    if "error" in idx:
        print(f"  ERROR: {idx['error']}")
        return
    n = idx["rows_seen"]
    print(
        f"  rows: {idx['rows_total']} total, {n} scanned "
        f"(limit={idx['limit']}, truncated={idx['truncated']})"
    )
    if idx.get("duplicate_ids"):
        print(f"  ⚠️  duplicate ids: {idx['duplicate_ids']}")
    print(
        f"  text total: {_fmt_bytes(idx['text_total_bytes'])}  "
        f"max: {_fmt_bytes(idx['text_max_bytes'])}  "
        f"avg: {_fmt_bytes(idx['text_total_bytes'] // max(n, 1))}/row"
    )

    if files["size"] and n:
        bytes_per_row = files["size"] / n
        ratio = bytes_per_row / max(1, idx["text_total_bytes"] / n)
        print(
            f"  on-disk per row: {_fmt_bytes(int(bytes_per_row))}  "
            f"(store_size / rows_scanned, {ratio:.0f}x the avg text size)"
        )

    print(
        f"  ratio vs sources: rows={idx['rows_total']}, source_total={total_src}, "
        f"ratio={idx['rows_total'] / max(total_src, 1):.2f}x"
    )

    print("\n  text-size buckets:")
    for b in _BUCKET_ORDER:
        v = idx["text_size_buckets"].get(b, 0)
        if v:
            print(f"    {b:<8} {v}")

    print("\n  source/label breakdown:")
    for k, v in idx["source_counts"].items():
        print(f"    source:{k:<10} {v}")
    for k, v in idx["label_counts"].items():
        print(f"    label:{k:<11} {v}")

    if idx.get("timestamp_min") and idx.get("timestamp_max"):
        print(
            f"\n  timestamp span: {_ts_to_iso(idx['timestamp_min'])} "
            f"→ {_ts_to_iso(idx['timestamp_max'])}"
        )

    tags = idx.get("top_tags") or Counter()
    if tags:
        print(f"\n  top {top_tags} tags:")
        for t, v in tags.most_common(top_tags):
            print(f"    {t:<40} {v}")

    dates = idx.get("top_dates") or Counter()
    if dates:
        print("\n  entries per date (top 14):")
        for d, v in sorted(dates.items())[-14:]:
            print(f"    {d:<12} {v}")


def main(argv: list[str] | None = None):
    opts = parse_args([""] + argv if argv is not None else sys.argv)
    if opts["help"]:
        print(__doc__)
        sys.exit(0)

    db_path = Path(opts["db"])
    if not db_path.exists():
        print(f"ERROR: {db_path} not found.", file=sys.stderr)
        sys.exit(1)

    if opts["stats"]:
        s = inspect_stats(db_path)
        if opts["json_mode"]:
            print(json.dumps(s, indent=2, default=str))
        else:
            print_stats_report(s)
        # Exit 2 on mismatch so callers can check $?
        if s.get("dimension_aligned") is False or "error" in s:
            sys.exit(2)
        sys.exit(0)

    if opts["api"]:
        tbl = _open(db_path)
        public = [a for a in dir(tbl) if not a.startswith("_")]
        print("dir(table):")
        for a in public:
            obj = getattr(tbl, a, None)
            kind = "method" if callable(obj) else type(obj).__name__
            print(f"  {a:<30} ({kind})")
        sys.exit(0)

    if opts["row"] is not None:
        row_id = str(opts["row"])
        # Row ids are truncated sha256 hex. Validate rather than sanitise, so a
        # malformed id is an error instead of a silently rewritten query.
        if not re.fullmatch(r"[0-9a-f]{1,64}", row_id):
            print(
                f"ERROR: invalid row id {opts['row']!r} (expected hex characters)",
                file=sys.stderr,
            )
            sys.exit(1)
        tbl = _open(db_path)
        rows = tbl.search().where(f"id = '{row_id}'").limit(1).to_list()
        if not rows:
            print(f"ERROR: no row with id {opts['row']!r}", file=sys.stderr)
            sys.exit(1)
        row = dict(rows[0])
        row.pop("vector", None)  # 384 floats add nothing to a human-readable dump
        print(json.dumps(store.row_to_dict(row), indent=2, default=str))
        sys.exit(0)

    if opts["deep"]:
        report = {"files": inspect_files(db_path), "deep": deep_inspect(db_path)}
        print(json.dumps(report, indent=2, default=str))
        sys.exit(0)

    files = inspect_files(db_path)
    sources = inspect_sources(Path(opts["memory"]))
    index = inspect_index(db_path, opts["limit"])

    if opts["sample"] > 0:
        try:
            tbl = _open(db_path)
            sample = (
                tbl.search().select(SCAN_COLUMNS).limit(opts["sample"]).to_list() or []
            )
            index["sample_entries"] = sample
        except Exception as e:
            index["sample_error"] = str(e)

    report = {"files": files, "sources": sources, "index": index}

    if opts["json_mode"]:
        # Counters → dicts; trim top_tags/top_dates to top 50 to keep JSON tidy.
        if isinstance(index.get("top_tags"), Counter):
            index["top_tags"] = dict(index["top_tags"].most_common(50))
        if isinstance(index.get("top_dates"), Counter):
            index["top_dates"] = dict(index["top_dates"].most_common(50))
        print(json.dumps(report, indent=2, default=str))
    else:
        print_text_report(report, opts["top_tags"])
        if opts["sample"] > 0:
            print("\n── Sample rows ─────────────────────────────────────────")
            for i, e in enumerate(index.get("sample_entries", []), 1):
                print(
                    f"  [{i}] id={e.get('id')} ts={e.get('ts')} "
                    f"label={e.get('label')} source={e.get('source')}"
                )
                preview = (e.get("text") or "")[:300]
                print(f"      text: {preview}")


if __name__ == "__main__":
    main()
