#!/usr/bin/env python3
"""
transcript_filter.py — Filter and render cycle-*.jsonl transcripts.

Walks a transcripts directory (default /agent/memory/transcripts), keeps
only cycle transcripts whose first JSON `timestamp` falls inside a time
window, and emits one of three outputs:

    --paths     One absolute path per line (default).
    --markdown  Streamlined markdown rendering of human-readable text
                content (user prompts, assistant text/thinking, tool
                calls, tool results) — not raw JSON.
    --json      JSON array of {path, created_at, cycle} objects.

Add --no-thinking to drop assistant `thinking` blocks from --markdown output.
Pass explicit FILES... to bypass globbing AND the time-window filter.

--markdown output is paginated: at most --page-size chars per page (default
25000). Use --page N to view subsequent pages. Page boundaries fall between
JSONL entries, never inside one — a single entry that exceeds page-size
still gets its own page intact.

Rendered chunks are cached on disk per transcript file at
<transcript_dir>/.cache/transcript_filter/<basename>.cache.json so that
subsequent --page N invocations reuse the parsed result. Cache is keyed
on size + mtime + format version + --no-thinking flag, and is
self-invalidating when the source changes. Override the location with
--cache-dir DIR or disable entirely with --no-cache.

The JSON `timestamp` inside the transcript is authoritative — filesystem
mtime is intentionally ignored.

Usage:
    python3 scripts/transcript_filter.py --hours 24
    python3 scripts/transcript_filter.py --hours 24 --markdown
    python3 scripts/transcript_filter.py --since 2026-04-23T00:00:00Z \\
        --until 2026-04-25T00:00:00Z --json
    python3 scripts/transcript_filter.py --dir /tmp/transcripts file1.jsonl
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DIR = Path("/agent/memory/transcripts")
CYCLE_GLOB = "cycle-*.jsonl"
CYCLE_RE = re.compile(r"cycle-(\d+)\.jsonl$")

# Truncation limits for markdown rendering.
MAX_BLOCK_LINES = 200
MAX_BLOCK_CHARS = 20000


def parse_iso(ts):
    """Parse an ISO 8601 string into an aware UTC datetime, or None."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_args(argv):
    args = {
        "dir": str(DEFAULT_DIR),
        "hours": None,
        "since": None,
        "until": None,
        "mode": "paths",
        "files": [],
        "no_thinking": False,
        "page": 1,
        "page_size": 25000,
        "cache_dir": None,  # None → default of <transcript_parent>/.cache/transcript_filter
        "no_cache": False,
    }
    mode_set = False

    def need_value(flag, idx):
        if idx + 1 >= len(argv):
            print(flag + " requires a value", file=sys.stderr)
            sys.exit(2)
        return argv[idx + 1]

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        elif a == "--dir":
            args["dir"] = need_value(a, i)
            i += 1
        elif a == "--hours":
            raw = need_value(a, i)
            try:
                args["hours"] = float(raw)
            except ValueError:
                print("invalid --hours: " + raw, file=sys.stderr)
                sys.exit(2)
            i += 1
        elif a == "--since":
            args["since"] = need_value(a, i)
            i += 1
        elif a == "--until":
            args["until"] = need_value(a, i)
            i += 1
        elif a == "--paths":
            args["mode"] = "paths"
            mode_set = True
        elif a == "--markdown":
            args["mode"] = "markdown"
            mode_set = True
        elif a == "--json":
            args["mode"] = "json"
            mode_set = True
        elif a == "--no-thinking":
            args["no_thinking"] = True
        elif a == "--page":
            raw = need_value(a, i)
            try:
                page = int(raw)
            except ValueError:
                print("invalid --page: " + raw, file=sys.stderr)
                sys.exit(2)
            if page < 1:
                print("--page must be >= 1", file=sys.stderr)
                sys.exit(2)
            args["page"] = page
            i += 1
        elif a == "--cache-dir":
            args["cache_dir"] = need_value(a, i)
            i += 1
        elif a == "--no-cache":
            args["no_cache"] = True
        elif a == "--page-size":
            raw = need_value(a, i)
            try:
                size = int(raw)
            except ValueError:
                print("invalid --page-size: " + raw, file=sys.stderr)
                sys.exit(2)
            if size < 1:
                print("--page-size must be >= 1", file=sys.stderr)
                sys.exit(2)
            args["page_size"] = size
            i += 1
        elif a.startswith("--"):
            print("unknown flag: " + a, file=sys.stderr)
            sys.exit(2)
        else:
            args["files"].append(a)
        i += 1
    # Default window: last 24h if neither --hours nor --since is supplied.
    if args["hours"] is None and args["since"] is None:
        args["hours"] = 24.0
    args["_mode_set"] = mode_set
    return args


def resolve_window(args):
    now = datetime.now(timezone.utc)
    if args["until"]:
        until = parse_iso(args["until"])
        if until is None:
            print("invalid --until: " + args["until"], file=sys.stderr)
            sys.exit(2)
    else:
        until = now
    if args["since"]:
        since = parse_iso(args["since"])
        if since is None:
            print("invalid --since: " + args["since"], file=sys.stderr)
            sys.exit(2)
    elif args["hours"] is not None:
        since = now - timedelta(hours=args["hours"])
    else:
        since = None
    return since, until


def iter_entries(path):
    """Yield parsed JSON objects from a JSONL file, skipping bad lines."""
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as e:
        print("cannot open " + str(path) + ": " + str(e), file=sys.stderr)
        return
    with f:
        for line in f:
            line = line.strip()
            if not line or line[0] not in "{[":
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def first_timestamp(path):
    """Return the first ISO timestamp found in the transcript, or None."""
    for obj in iter_entries(path):
        ts = obj.get("timestamp")
        if not ts and isinstance(obj.get("message"), dict):
            ts = obj["message"].get("timestamp")
        dt = parse_iso(ts) if ts else None
        if dt is not None:
            return dt
    return None


def cycle_number(path):
    m = CYCLE_RE.search(str(path))
    return int(m.group(1)) if m else None


def in_window(dt, since, until):
    if dt is None:
        return False
    if since is not None and dt < since:
        return False
    if until is not None and dt > until:
        return False
    return True


def discover_files(args):
    if args["files"]:
        return [Path(p) for p in args["files"]]
    base = Path(args["dir"])
    if not base.is_dir():
        print("directory not found: " + str(base), file=sys.stderr)
        return []
    return sorted(base.glob(CYCLE_GLOB))


def truncate_block(text):
    if not isinstance(text, str):
        text = str(text)
    lines = text.splitlines()
    truncated = False
    if len(lines) > MAX_BLOCK_LINES:
        extra = len(lines) - MAX_BLOCK_LINES
        lines = lines[:MAX_BLOCK_LINES]
        text = "\n".join(lines) + "\n… [truncated " + str(extra) + " more lines]"
        truncated = True
    if len(text) > MAX_BLOCK_CHARS:
        text = text[:MAX_BLOCK_CHARS] + "\n… [truncated, output exceeded char limit]"
        truncated = True
    return text, truncated


def format_hours(h):
    """Compact label for an hours value: 24.0 -> '24h', 1.5 -> '1.5h'."""
    if float(h).is_integer():
        return str(int(h)) + "h"
    return ("%g" % float(h)) + "h"


def render_tool_input(inp):
    try:
        pretty = json.dumps(inp, indent=2, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        pretty = str(inp)
    pretty, _ = truncate_block(pretty)
    return pretty


def render_tool_result_content(content):
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict):
                if blk.get("type") == "text":
                    parts.append(str(blk.get("text", "")))
                else:
                    # Stub non-text blocks (image, server_tool_use, etc.) so
                    # envelope/binary fields never leak into the markdown.
                    parts.append("[" + str(blk.get("type", "block")) + "]")
            else:
                parts.append(str(blk))
        text = "\n".join(parts)
    else:
        text = str(content)
    text, _ = truncate_block(text)
    return text


def quote_lines(text, prefix="> "):
    if not text:
        return prefix
    return "\n".join(
        prefix + line if line else prefix.rstrip() for line in text.splitlines()
    )


def render_tool_call(name, inp):
    """Render a tool call inline when the input is a single string field,
    otherwise fall back to a fenced JSON block."""
    if isinstance(inp, dict) and len(inp) == 1:
        only_val = next(iter(inp.values()))
        if (
            isinstance(only_val, str)
            and "\n" not in only_val
            and "`" not in only_val
            and len(only_val) <= 200
        ):
            return "> **Tool call:** " + str(name) + " — `" + only_val + "`"
    pretty = render_tool_input(inp)
    return "> **Tool call:** " + str(name) + "\n```json\n" + pretty + "\n```"


def render_assistant_blocks(blocks, no_thinking, role_state):
    out = []
    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        btype = blk.get("type")
        if btype == "text":
            text = str(blk.get("text", "")).strip()
            if not text:
                continue
            if role_state["last"] == "assistant":
                out.append((None, text))
            else:
                out.append(("assistant", text))
                role_state["last"] = "assistant"
        elif btype == "thinking":
            if no_thinking:
                continue
            text = str(blk.get("thinking", "")).strip()
            if text:
                text, _ = truncate_block(text)
                # Prepend the marker to the whole text so it always lands on
                # the first non-empty content line (truncate_block can leave
                # a trailing sentinel, but never alters the leading char).
                out.append((None, quote_lines("💭 " + text)))
                role_state["last"] = None
        elif btype == "tool_use":
            out.append(
                (None, render_tool_call(blk.get("name", "?"), blk.get("input", {})))
            )
            role_state["last"] = None
        elif btype == "tool_result":
            text = render_tool_result_content(blk.get("content", ""))
            out.append((None, "> **Tool result:**\n" + quote_lines(text)))
            role_state["last"] = None
    return out


def render_user_content(content, role_state):
    out = []
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return out
        if role_state["last"] == "user":
            out.append((None, text))
        else:
            out.append(("user", text))
            role_state["last"] = "user"
        return out
    if isinstance(content, list):
        for blk in content:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                text = str(blk.get("text", "")).strip()
                if not text:
                    continue
                if role_state["last"] == "user":
                    out.append((None, text))
                else:
                    out.append(("user", text))
                    role_state["last"] = "user"
            elif btype == "tool_result":
                text = render_tool_result_content(blk.get("content", ""))
                out.append((None, "> **Tool result:**\n" + quote_lines(text)))
                role_state["last"] = None
    return out


# Top-level entries we render. Everything else (queue-operation,
# queued_command, last-prompt, system, summary, attachment, …) is dropped.
RENDERED_TYPES = {"user", "assistant"}
ROLE_HEADINGS = {"user": "### User", "assistant": "### Assistant"}

# Cache version — bump if the rendered markdown format changes in any way
# that would invalidate stored entries (heading style, separator, etc.).
CACHE_VERSION = 2
DEFAULT_CACHE_SUBDIR = ".cache/transcript_filter"


def cache_path_for(transcript_path, cache_dir):
    """Return the on-disk cache file path for one transcript, given the
    user-supplied cache_dir (or None for the default colocated layout)."""
    if cache_dir:
        base = Path(cache_dir)
    else:
        base = Path(transcript_path).parent / DEFAULT_CACHE_SUBDIR
    return base / (Path(transcript_path).name + ".cache.json")


def load_cached_chunks(transcript_path, cache_dir, no_thinking):
    """Return the cached chunks list for `transcript_path` if the cache
    file exists and matches the current source size+mtime+flags+version,
    else None. Failures (missing file, corrupt JSON, key mismatch) are
    treated as cache miss — never raise."""
    cp = cache_path_for(transcript_path, cache_dir)
    try:
        st = os.stat(transcript_path)
    except OSError:
        return None
    try:
        with open(cp, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("v") != CACHE_VERSION:
        return None
    if payload.get("size") != st.st_size:
        return None
    if payload.get("mtime") != st.st_mtime:
        return None
    if bool(payload.get("no_thinking")) != bool(no_thinking):
        return None
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not all(isinstance(c, str) for c in chunks):
        return None
    return chunks


def save_cached_chunks(transcript_path, cache_dir, no_thinking, chunks):
    """Write `chunks` to the cache. Best-effort: any I/O failure is
    silently ignored (e.g. read-only fs, permission denied)."""
    cp = cache_path_for(transcript_path, cache_dir)
    try:
        st = os.stat(transcript_path)
    except OSError:
        return
    payload = {
        "v": CACHE_VERSION,
        "source": str(transcript_path),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "no_thinking": bool(no_thinking),
        "chunks": chunks,
    }
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp = cp.with_suffix(cp.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, cp)
    except OSError:
        pass


def get_chunks(transcript_path, no_thinking, cache_dir, use_cache):
    """Return the list of rendered chunk strings for one transcript file.
    Reads from disk cache when valid; otherwise renders fresh and caches."""
    if use_cache:
        cached = load_cached_chunks(transcript_path, cache_dir, no_thinking)
        if cached is not None:
            return cached
    chunks = [text for _kind, text in render_chunks(transcript_path, no_thinking)]
    if use_cache:
        save_cached_chunks(transcript_path, cache_dir, no_thinking, chunks)
    return chunks


def render_chunks(path, no_thinking=False):
    """Yield atomic markdown chunks for one transcript file:

      ('cycle', '## Cycle N')           — once at the start of the file
      ('entry', '<rendered entry>')     — once per kept JSONL entry

    Each entry chunk corresponds to exactly one JSONL line (one user or
    assistant entry). Pagination treats these chunks as atomic: a chunk
    larger than the page size still gets its own page rather than being
    split mid-line.
    """
    cyc = cycle_number(path)
    header = "## Cycle " + str(cyc) if cyc is not None else "## " + path.name
    yield "cycle", header

    role_state = {"last": None}
    for obj in iter_entries(path):
        if obj.get("type") not in RENDERED_TYPES:
            continue
        if obj.get("isSidechain") is True:
            continue
        otype = obj["type"]
        msg = obj.get("message") or {}
        pieces = []
        if otype == "user":
            pieces = render_user_content(msg.get("content", ""), role_state)
        elif otype == "assistant":
            content = msg.get("content")
            if isinstance(content, list):
                pieces = render_assistant_blocks(content, no_thinking, role_state)
            elif isinstance(content, str) and content.strip():
                text = content.strip()
                if role_state["last"] == "assistant":
                    pieces = [(None, text)]
                else:
                    pieces = [("assistant", text)]
                    role_state["last"] = "assistant"
        if not pieces:
            continue
        lines = []
        for role, text in pieces:
            if role in ROLE_HEADINGS:
                lines.append(ROLE_HEADINGS[role])
            lines.append(text)
        yield "entry", "\n".join(lines)


def paginate(chunks, page_size):
    """Group rendered chunks into pages of at most `page_size` characters.

    Chunks are atomic — never split. A chunk that exceeds page_size on its
    own gets its own page (per the spec: always include the full content
    of a JSONL line, even if it overflows). Newline join cost is included.
    Returns a list of page strings.
    """
    pages = []
    current = []
    current_len = 0
    sep = 2  # "\n\n" between chunks within a page
    for c in chunks:
        clen = len(c)
        if current and current_len + sep + clen > page_size:
            pages.append("\n\n".join(current))
            current = [c]
            current_len = clen
        else:
            if current:
                current_len += sep
            current.append(c)
            current_len += clen
    if current:
        pages.append("\n\n".join(current))
    return pages


def main(argv):
    args = parse_args(argv)
    since, until = resolve_window(args)

    files = discover_files(args)
    explicit = bool(args["files"])
    kept = []
    for p in files:
        ts = first_timestamp(p)
        if ts is None:
            if not explicit:
                print("warn: no timestamp in " + str(p), file=sys.stderr)
                continue
        # Explicit FILES... bypass the time window — render whatever was named.
        if explicit or in_window(ts, since, until):
            kept.append((p, ts))

    kept.sort(key=lambda pt: pt[1] or datetime.max.replace(tzinfo=timezone.utc))

    mode = args["mode"]
    if mode == "paths":
        for p, _ in kept:
            print(os.path.abspath(str(p)))
    elif mode == "json":
        out = [
            {
                "path": os.path.abspath(str(p)),
                "created_at": ts.isoformat() if ts else None,
                "cycle": cycle_number(p),
            }
            for p, ts in kept
        ]
        print(json.dumps(out, indent=2))
    elif mode == "markdown":
        now_local = datetime.now(timezone.utc).astimezone()
        date_str = now_local.strftime("%Y %B %d (%A)")
        if args["hours"] is not None and not args["since"]:
            window_label = format_hours(args["hours"]) + " transcripts"
        else:
            window_label = "transcripts"

        use_cache = not args["no_cache"]
        all_chunks = []
        for p, _ in kept:
            all_chunks.extend(
                get_chunks(p, args["no_thinking"], args["cache_dir"], use_cache)
            )

        pages = paginate(all_chunks, args["page_size"]) or [""]
        total_pages = len(pages)
        page_num = args["page"]
        if page_num > total_pages:
            print(
                "page "
                + str(page_num)
                + " out of range (only "
                + str(total_pages)
                + " page(s))",
                file=sys.stderr,
            )
            return 2

        top_header = "# " + window_label + " as of date " + date_str
        if total_pages > 1:
            top_header += (
                " — Page "
                + str(page_num)
                + " of "
                + str(total_pages)
                + " (re-run with --page N to view other pages)"
            )

        print(top_header)
        body = pages[page_num - 1]
        if body:
            print("")
            print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
