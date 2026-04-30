#!/usr/bin/env python3
"""
memory_inspect.py — Inspect a long-term memory `.mv2` file (memvid SDK).

Read-only diagnostics for a memvid `.mv2` index. Reports on-disk footprint
(file + sibling artifacts), total frame/entry count, source/label/tag
distribution, preview-length distribution, and timestamp span. Also compares
the indexed counts against the source JSON files (journal, cycles, inbox
history) so you can spot inflation from auto-chunking or stale records.

Usage:
    uv run python scripts/memory_inspect.py
    uv run python scripts/memory_inspect.py --mv2 /agent/memory/long_term_memory.mv2
    uv run python scripts/memory_inspect.py --top-tags 30
    uv run python scripts/memory_inspect.py --sample 5         # show 5 raw entries
    uv run python scripts/memory_inspect.py --json
    uv run python scripts/memory_inspect.py --api               # dump SDK surface

Optional:
    --mv2 PATH        Path to the .mv2 file (default: /agent/memory/long_term_memory.mv2)
    --memory PATH     Path to the memory directory holding the source JSON files
                      (journal/cycles/inbox_history). Defaults to the parent
                      directory of --mv2, so passing --mv2 alone is enough for
                      most cases.
    --limit N         Max entries to iterate via timeline() (default: 200000)
    --top-tags N      How many top tags to print (default: 20)
    --sample N        Print N raw timeline entries (default: 0)
    --deep            Call SDK introspection (stats/memories_stats/state/
                      get_capacity/doctor/verify) and fetch full frames for
                      the largest fan-out records to find what's eating disk
    --frame N         Fetch frame N via mem.frame() and print full content
    --api             Print dir(mem) for the opened handle and exit
    --json            Output report as JSON

Exit codes: 0 = success, 1 = error (file not found, SDK error)
"""

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    import memvid_sdk
except ImportError:
    memvid_sdk = None


MEMORY = Path("/agent/memory")
DEFAULT_MV2 = MEMORY / "long_term_memory.mv2"


def _require_sdk():
    if memvid_sdk is None:
        print(
            "ERROR: memvid_sdk not installed (not available on this runtime).",
            file=sys.stderr,
        )
        sys.exit(1)


def parse_args(argv):
    args = argv[1:]
    # `mv2` and `memory` start as None so we can tell explicit overrides apart
    # from defaults — when only `--mv2` is given, we auto-derive `memory` from
    # the mv2 file's parent directory so source-JSON comparison works on
    # non-default .mv2 files.
    result = {
        "mv2": None,
        "memory": None,
        "limit": 200000,
        "top_tags": 20,
        "sample": 0,
        "deep": False,
        "frame": None,
        "api": False,
        "json_mode": False,
        "help": False,
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            result["help"] = True
        elif a == "--mv2" and i + 1 < len(args):
            i += 1
            result["mv2"] = args[i]
        elif a == "--memory" and i + 1 < len(args):
            i += 1
            result["memory"] = args[i]
        elif a == "--limit" and i + 1 < len(args):
            i += 1
            result["limit"] = int(args[i])
        elif a == "--top-tags" and i + 1 < len(args):
            i += 1
            result["top_tags"] = int(args[i])
        elif a == "--sample" and i + 1 < len(args):
            i += 1
            result["sample"] = int(args[i])
        elif a == "--deep":
            result["deep"] = True
        elif a == "--frame" and i + 1 < len(args):
            i += 1
            result["frame"] = int(args[i])
        elif a == "--api":
            result["api"] = True
        elif a == "--json":
            result["json_mode"] = True
        i += 1

    # Resolve defaults — `--mv2` falls back to DEFAULT_MV2; `--memory`
    # auto-derives from the mv2 parent so callers only need to pass `--mv2`.
    if result["mv2"] is None:
        result["mv2"] = str(DEFAULT_MV2)
    if result["memory"] is None:
        result["memory"] = str(Path(result["mv2"]).resolve().parent)
    return result


# ── File-system inspection ──────────────────────────────────────────────────


def inspect_files(mv2: Path) -> dict:
    """List the .mv2 and every sibling artifact (backup, wal, tmp copies)."""
    parent = mv2.parent
    base = mv2.name
    siblings = []
    if parent.exists():
        for p in sorted(parent.iterdir()):
            n = p.name
            if not p.is_file():
                continue
            # Anything that mentions the .mv2 stem — including hidden tmp
            # copies the SDK writes during atomic commits (e.g. .name.RAND).
            if base in n or n.startswith("." + base.split(".")[0]):
                try:
                    siblings.append(
                        {
                            "name": n,
                            "size": p.stat().st_size,
                            "mtime": datetime.fromtimestamp(
                                p.stat().st_mtime, tz=timezone.utc
                            ).isoformat(),
                        }
                    )
                except OSError as e:
                    siblings.append({"name": n, "error": str(e)})
    return {
        "mv2": str(mv2),
        "exists": mv2.exists(),
        "size": mv2.stat().st_size if mv2.exists() else 0,
        "siblings": siblings,
    }


# ── SDK-level inspection ────────────────────────────────────────────────────


def _open_readonly(mv2: Path):
    _require_sdk()
    return memvid_sdk.use(
        "basic",
        str(mv2),
        mode="open",
        enable_vec=True,
        enable_lex=True,
        read_only=True,
    )


def _entry_fields(entry):
    """Pull (frame_id, ts, preview, uri, child_frames) from a timeline entry."""
    if isinstance(entry, dict):
        g = entry.get
    else:
        g = lambda k, d=None: getattr(entry, k, d)  # noqa: E731
    return {
        "frame_id": g("frame_id"),
        "timestamp": g("timestamp"),
        "preview": g("preview") or "",
        "uri": g("uri"),
        "child_frames": list(g("child_frames", []) or []),
    }


def _parse_meta_from_preview(preview: str) -> dict:
    """Extract `tags:` / `labels:` / `category:` / `title:` from preview text.

    The SDK appends frame metadata inline after the content, e.g.
        <content> title: ... tags: ... labels: ... category: "..."
    We slice on the first marker to recover the appended block.
    """
    meta = {"title": "", "label": "", "category": "", "tags": []}
    if not preview:
        return meta
    # Find the metadata block — the appended portion starts at the first marker.
    markers = [" title: ", " tags: ", " labels: ", " category: "]
    cut = min((preview.find(m) for m in markers if m in preview), default=-1)
    if cut == -1:
        return meta
    block = preview[cut:]

    def _grab(field: str) -> str:
        # Match `field: ...` up to the next ` <other-field>: ` boundary.
        key = f" {field}: "
        i = block.find(key)
        if i == -1:
            return ""
        rest = block[i + len(key) :]
        next_i = len(rest)
        for other in ("title", "tags", "labels", "category", "uri"):
            if other == field:
                continue
            j = rest.find(f" {other}: ")
            if 0 <= j < next_i:
                next_i = j
        return rest[:next_i].strip().strip('"')

    meta["title"] = _grab("title")
    meta["label"] = _grab("labels")
    meta["category"] = _grab("category")
    raw_tags = _grab("tags")
    if raw_tags:
        # Tags are usually comma- or space-separated; tolerate either.
        for sep in (",", " "):
            if sep in raw_tags:
                meta["tags"] = [t.strip() for t in raw_tags.split(sep) if t.strip()]
                break
        else:
            meta["tags"] = [raw_tags]
    return meta


def _content_len(preview: str) -> int:
    """Length of the *content* portion of a preview (before metadata block)."""
    if not preview:
        return 0
    cut = -1
    for m in (" title: ", " tags: ", " labels: ", " category: "):
        i = preview.find(m)
        if i != -1 and (cut == -1 or i < cut):
            cut = i
    return cut if cut != -1 else len(preview)


def _bucket(n: int) -> str:
    if n < 100:
        return "<100B"
    if n < 1_000:
        return "<1KB"
    if n < 10_000:
        return "<10KB"
    if n < 100_000:
        return "<100KB"
    if n < 1_000_000:
        return "<1MB"
    return ">=1MB"


_BUCKET_ORDER = ("<100B", "<1KB", "<10KB", "<100KB", "<1MB", ">=1MB")


def inspect_index(mv2: Path, limit: int) -> dict:
    """Iterate the index via timeline() and aggregate stats."""
    mem = _open_readonly(mv2)
    try:
        entries = mem.timeline(limit=limit) or []
    except Exception as e:
        return {"error": f"timeline() failed: {e}", "entries_seen": 0}

    label_counts = Counter()
    source_counts = Counter()
    tag_counts = Counter()
    date_counts = Counter()
    bucket_counts = Counter()
    child_counts = Counter()

    total = 0
    total_content = 0
    total_preview = 0
    max_content = 0
    max_preview = 0
    ts_min = None
    ts_max = None

    for e in entries:
        f = _entry_fields(e)
        total += 1
        meta = _parse_meta_from_preview(f["preview"])
        clen = _content_len(f["preview"])
        plen = len(f["preview"])
        total_content += clen
        total_preview += plen
        if clen > max_content:
            max_content = clen
        if plen > max_preview:
            max_preview = plen
        bucket_counts[_bucket(clen)] += 1
        n_children = len(f["child_frames"])
        child_counts[n_children if n_children < 10 else "10+"] += 1
        if meta["label"]:
            label_counts[meta["label"]] += 1
        for t in meta["tags"]:
            tag_counts[t] += 1
            if t.startswith("date:"):
                date_counts[t[5:]] += 1
            if t in ("inbox", "journal", "cycle", "goal"):
                source_counts[t] += 1
        ts = f["timestamp"]
        if isinstance(ts, (int, float)) and ts > 0:
            if ts_min is None or ts < ts_min:
                ts_min = ts
            if ts_max is None or ts > ts_max:
                ts_max = ts

    return {
        "entries_seen": total,
        "limit": limit,
        "truncated": total >= limit,
        "content_total_bytes": total_content,
        "content_max_bytes": max_content,
        "preview_total_bytes": total_preview,
        "preview_max_bytes": max_preview,
        "content_size_buckets": {b: bucket_counts.get(b, 0) for b in _BUCKET_ORDER},
        "label_counts": dict(label_counts.most_common()),
        "source_counts": dict(source_counts.most_common()),
        "child_frame_counts": dict(child_counts),
        "top_tags": tag_counts,
        "top_dates": date_counts,
        "timestamp_min": ts_min,
        "timestamp_max": ts_max,
    }


def _ts_to_iso(ts):
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return None


# ── Deep introspection via SDK methods ──────────────────────────────────────


def _safe_call(obj, name: str, *args, **kwargs):
    """Call a method by name, returning the result or {'error': ...} on failure.

    Suppresses native stderr written by the underlying Rust SDK (e.g. doctor's
    `doctor: ...` probe lines) so that --json output stays parseable.
    """
    fn = getattr(obj, name, None)
    if not callable(fn):
        return {"error": f"{name} not callable"}
    with _silenced_stderr():
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return {"error": f"{name} raised: {type(e).__name__}: {e}"}


import contextlib  # noqa: E402
import os  # noqa: E402


@contextlib.contextmanager
def _silenced_stderr():
    """Redirect FD 2 to /dev/null for the duration of the block.

    The memvid SDK is implemented in Rust and writes diagnostic lines (e.g.
    `doctor: probe start`) directly to file-descriptor 2, bypassing Python's
    sys.stderr — so contextlib.redirect_stderr can't catch them. We dup the
    FD, point 2 at /dev/null, then restore.
    """
    try:
        old_fd = os.dup(2)
    except OSError:
        yield
        return
    try:
        with open(os.devnull, "wb") as devnull:
            os.dup2(devnull.fileno(), 2)
            try:
                yield
            finally:
                os.dup2(old_fd, 2)
    finally:
        os.close(old_fd)


def _frame_uri(entry_or_id) -> str:
    """Build the mv2:// URI the SDK expects for frame() / blob() lookups.

    Timeline entries already carry a `uri` field — we prefer it. If only the
    integer frame_id is available, fall back to the canonical form.
    """
    if isinstance(entry_or_id, dict):
        u = entry_or_id.get("uri")
        if isinstance(u, str) and u:
            return u
        fid = entry_or_id.get("frame_id")
    else:
        fid = entry_or_id
    return f"mv2://frame/{int(fid)}"


def _to_jsonable(obj, depth: int = 0):
    """Convert SDK return values (dicts, dataclasses, custom objects) to JSON."""
    if depth > 6:
        return repr(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(x, depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v, depth + 1) for k, v in obj.items()}
    # Dataclass-ish or attrs: take public attributes
    if hasattr(obj, "__dict__"):
        return {
            k: _to_jsonable(v, depth + 1)
            for k, v in vars(obj).items()
            if not k.startswith("_")
        }
    if hasattr(obj, "_asdict"):  # namedtuple
        return {k: _to_jsonable(v, depth + 1) for k, v in obj._asdict().items()}
    return repr(obj)


def deep_inspect(mv2: Path, limit: int) -> dict:
    """Call SDK introspection methods and fetch full frames for fan-out records.

    The goal is to discover where the bytes actually live: vector index segments,
    lex index, frame blobs, retired records, WAL pages, etc.
    """
    mem = _open_readonly(mv2)
    out = {}

    # Plain no-arg introspection methods discovered via --api.
    for name in (
        "stats",
        "memories_stats",
        "state",
        "get_capacity",
        "list_tables",
        "functions",
    ):
        attr = getattr(mem, name, None)
        if attr is None:
            continue
        if callable(attr):
            out[name] = _to_jsonable(_safe_call(mem, name))
        else:
            out[name] = _to_jsonable(attr)

    # doctor / verify often print or return health data
    out["doctor"] = _to_jsonable(_safe_call(mem, "doctor"))
    out["verify"] = _to_jsonable(_safe_call(mem, "verify"))

    # For each table the SDK exposes, dump its schema/row count if reachable.
    tables = out.get("list_tables") or []
    if isinstance(tables, list) and tables:
        table_info = {}
        for t in tables[:30]:
            table_info[str(t)] = _to_jsonable(_safe_call(mem, "get_table", t))
        out["tables"] = table_info

    # Fetch the largest fan-out frames: from timeline, find entries with
    # the most child_frames and pull each parent + its first child via
    # mem.frame() to see what's actually stored.
    try:
        entries = mem.timeline(limit=limit) or []
    except Exception as e:
        out["timeline_error"] = str(e)
        return out

    fanout = []
    for e in entries:
        f = _entry_fields(e)
        n = len(f["child_frames"])
        if n > 0:
            fanout.append((n, f))
    fanout.sort(key=lambda x: -x[0])

    fan_samples = []
    for n, f in fanout[:5]:
        parent_uri = f.get("uri") or _frame_uri(f["frame_id"])
        rec = {
            "frame_id": f["frame_id"],
            "uri": parent_uri,
            "n_children": n,
            "preview_120": (f["preview"] or "")[:120],
        }
        rec["parent_frame"] = _to_jsonable(_safe_call(mem, "frame", parent_uri))
        if f["child_frames"]:
            child_id = f["child_frames"][0]
            child_uri = _frame_uri(child_id) if isinstance(child_id, int) else child_id
            rec["first_child_uri"] = child_uri
            rec["first_child_frame"] = _to_jsonable(_safe_call(mem, "frame", child_uri))
            rec["first_child_blob_len"] = _maybe_blob_len(mem, child_uri)
        rec["parent_blob_len"] = _maybe_blob_len(mem, parent_uri)
        fan_samples.append(rec)
    out["fanout_samples"] = fan_samples
    out["fanout_top_counts"] = [n for n, _ in fanout[:20]]

    # Sample a few zero-child frames too — they may carry bulky vector blobs.
    zero_child = [
        _entry_fields(e) for e in entries if not _entry_fields(e)["child_frames"]
    ]
    flat_samples = []
    for f in zero_child[:3]:
        uri = f.get("uri") or _frame_uri(f["frame_id"])
        flat_samples.append(
            {
                "frame_id": f["frame_id"],
                "uri": uri,
                "preview_120": (f["preview"] or "")[:120],
                "frame": _to_jsonable(_safe_call(mem, "frame", uri)),
                "blob_len": _maybe_blob_len(mem, uri),
            }
        )
    out["flat_frame_samples"] = flat_samples

    return out


def _maybe_blob_len(mem, frame_uri):
    """Try mem.blob(uri) and return byte length, or describe failure.

    Accepts an mv2:// URI string (the SDK's expected form). Pass an int
    frame_id only via _frame_uri() conversion at the call site.
    """
    fn = getattr(mem, "blob", None)
    if not callable(fn):
        return None
    if isinstance(frame_uri, int):
        frame_uri = _frame_uri(frame_uri)
    with _silenced_stderr():
        try:
            b = fn(frame_uri)
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"
    try:
        return len(b)
    except TypeError:
        return f"len-failed: {type(b).__name__}"


# ── Source-JSON counts (for comparison) ─────────────────────────────────────


def _safe_load_list(p: Path) -> int:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return len(data) if isinstance(data, list) else 0
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def inspect_sources(memory_dir: Path) -> dict:
    """Count entries in each source JSON the build path would ingest."""
    msgs_dir = memory_dir.resolve().parent / "messages"
    return {
        "journal.json": _safe_load_list(memory_dir / "journal.json"),
        "journal-archive.json": _safe_load_list(memory_dir / "journal-archive.json"),
        "cycles.json": _safe_load_list(memory_dir / "cycles.json"),
        "cycles-archive.json": _safe_load_list(memory_dir / "cycles-archive.json"),
        "messages/inbox_history.json": _safe_load_list(msgs_dir / "inbox_history.json"),
        "messages/inbox.json": _safe_load_list(msgs_dir / "inbox.json"),
    }


# ── Reporting ───────────────────────────────────────────────────────────────


def _fmt_bytes(n: int) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def print_text_report(report: dict, top_tags: int) -> None:
    files = report["files"]
    idx = report["index"]
    src = report["sources"]

    print("── File ────────────────────────────────────────────────")
    print(f"  path: {files['mv2']}")
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
    n = idx["entries_seen"]
    print(f"  entries_seen: {n} (limit={idx['limit']}, truncated={idx['truncated']})")
    print(
        f"  content total: {_fmt_bytes(idx['content_total_bytes'])}  "
        f"max: {_fmt_bytes(idx['content_max_bytes'])}  "
        f"avg: {_fmt_bytes(idx['content_total_bytes'] // max(n, 1))}/entry"
    )
    print(
        f"  preview total: {_fmt_bytes(idx['preview_total_bytes'])}  "
        f"max: {_fmt_bytes(idx['preview_max_bytes'])}"
    )

    if files["size"] and n:
        bytes_per_entry = files["size"] / n
        ratio = bytes_per_entry / max(1, idx["content_total_bytes"] / n)
        print(
            f"  on-disk per entry: {_fmt_bytes(int(bytes_per_entry))}  "
            f"(file_size / entries_seen, "
            f"{ratio:.0f}x the avg content size)"
        )

    print(
        f"  inflation vs sources: file={n}, source_total={total_src}, "
        f"ratio={n / max(total_src, 1):.2f}x"
    )

    print("\n  content-size buckets:")
    for b in _BUCKET_ORDER:
        v = idx["content_size_buckets"].get(b, 0)
        if v:
            print(f"    {b:<8} {v}")

    print("\n  child_frame_counts (how many sub-frames per entry):")
    for k, v in sorted(
        idx["child_frame_counts"].items(),
        key=lambda kv: (isinstance(kv[0], str), kv[0]),
    ):
        print(f"    {str(k):<6} {v}")

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


def main():
    opts = parse_args(sys.argv)
    if opts["help"]:
        print(__doc__)
        sys.exit(0)

    mv2 = Path(opts["mv2"])
    if not mv2.exists():
        print(f"ERROR: {mv2} not found.", file=sys.stderr)
        sys.exit(1)

    if opts["api"]:
        mem = _open_readonly(mv2)
        public = [a for a in dir(mem) if not a.startswith("_")]
        print("dir(mem):")
        for a in public:
            obj = getattr(mem, a, None)
            kind = "method" if callable(obj) else type(obj).__name__
            print(f"  {a:<30} ({kind})")
        sys.exit(0)

    if opts["frame"] is not None:
        mem = _open_readonly(mv2)
        uri = _frame_uri(opts["frame"])
        rec = {
            "frame_id": opts["frame"],
            "uri": uri,
            "frame": _to_jsonable(_safe_call(mem, "frame", uri)),
            "blob_len": _maybe_blob_len(mem, uri),
        }
        print(json.dumps(rec, indent=2, default=str))
        sys.exit(0)

    if opts["deep"]:
        deep = deep_inspect(mv2, opts["limit"])
        files = inspect_files(mv2)
        report = {"files": files, "deep": deep}
        print(json.dumps(report, indent=2, default=str))
        sys.exit(0)

    files = inspect_files(mv2)
    sources = inspect_sources(Path(opts["memory"]))
    index = inspect_index(mv2, opts["limit"])

    if opts["sample"] > 0:
        try:
            mem = _open_readonly(mv2)
            sample = mem.timeline(limit=opts["sample"]) or []
            index["sample_entries"] = [_entry_fields(e) for e in sample]
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
            print("\n── Sample entries ──────────────────────────────────────")
            for i, e in enumerate(index.get("sample_entries", []), 1):
                print(
                    f"  [{i}] frame_id={e['frame_id']} ts={e['timestamp']} "
                    f"children={len(e['child_frames'])}"
                )
                preview = (e["preview"] or "")[:300]
                print(f"      preview: {preview}")


if __name__ == "__main__":
    main()
