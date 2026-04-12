#!/usr/bin/env python3
"""
notes.py -- Personal notes/scratchpad manager for the agent.

Stores quick notes, links, and snippets in /agent/memory/notes.json.
Each note has an ID, title, content, optional tags, and timestamps.

Usage:
    uv run python scripts/notes.py add --title "Meeting notes" --content "Discussed Q2 roadmap"
    uv run python scripts/notes.py add --title "Useful link" --content "https://example.com" --tags "reference,web"
    uv run python scripts/notes.py list
    uv run python scripts/notes.py list --tag "reference"
    uv run python scripts/notes.py list --json
    uv run python scripts/notes.py get --id <note_id>
    uv run python scripts/notes.py search --query "roadmap"
    uv run python scripts/notes.py edit --id <note_id> --content "Updated content"
    uv run python scripts/notes.py delete --id <note_id>
    uv run python scripts/notes.py tags                        # list all tags with counts
    uv run python scripts/notes.py export --format md          # export as Markdown
    uv run python scripts/notes.py stats                       # summary statistics

Options for 'add':
    --title TEXT       Note title (required)
    --content TEXT     Note body (required)
    --tags TAG,TAG     Comma-separated tags (optional)
    --pin              Pin this note to the top

Options for 'edit':
    --id ID            Note ID (required)
    --title TEXT       New title (optional)
    --content TEXT     New content (optional)
    --tags TAG,TAG     Replace tags (optional)
    --pin / --unpin    Toggle pin status

Exit codes: 0 = success, 1 = error
"""

import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

NOTES_PATH = Path("/agent/memory/notes.json")
_LOCK_PATH = str(NOTES_PATH) + ".lock"


@contextmanager
def _flock(timeout=10):
    """Acquire exclusive lock on notes.json for safe read-modify-write."""
    with open(_LOCK_PATH, "a+") as lock_f:
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Could not acquire notes.json lock within {timeout}s")
                    time.sleep(0.05)
            yield
        finally:
            try:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass


def load_notes() -> list:
    try:
        return json.loads(NOTES_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_notes(notes: list):
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(NOTES_PATH.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(notes, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(NOTES_PATH))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _gen_id(title: str) -> str:
    ts = datetime.now(timezone.utc).isoformat()
    return hashlib.sha256(f"{title}{ts}".encode()).hexdigest()[:8]


def cmd_add(args: list) -> int:
    title = _get_flag(args, "--title")
    content = _get_flag(args, "--content")
    tags_str = _get_flag(args, "--tags")
    pinned = "--pin" in args

    if not title or not content:
        print("Error: --title and --content are required.", file=sys.stderr)
        return 1

    tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else []
    now = datetime.now(timezone.utc).isoformat()
    note_id = _gen_id(title)

    note = {
        "id": note_id,
        "title": title,
        "content": content,
        "tags": tags,
        "pinned": pinned,
        "created_at": now,
        "updated_at": now,
    }

    with _flock():
        notes = load_notes()

        # Check for duplicate ID (extremely unlikely)
        existing_ids = {n.get("id") for n in notes}
        while note_id in existing_ids:
            note_id = _gen_id(title + note_id)
            note["id"] = note_id

        notes.append(note)
        save_notes(notes)
    print(f"Note added: {note_id} -- {title}")
    if tags:
        print(f"  Tags: {', '.join(tags)}")
    return 0


def cmd_list(args: list) -> int:
    notes = load_notes()
    tag_filter = _get_flag(args, "--tag")
    as_json = "--json" in args

    if tag_filter:
        notes = [n for n in notes if tag_filter in n.get("tags", [])]

    if not notes:
        if as_json:
            print("[]")
        else:
            print("No notes found.")
        return 0

    # Sort: pinned first, then by updated_at descending
    notes.sort(key=lambda n: (not n.get("pinned", False), n.get("updated_at", "")))

    if as_json:
        print(json.dumps(notes, indent=2))
        return 0

    for n in notes:
        pin = "[PIN] " if n.get("pinned") else ""
        tags = f" [{', '.join(n.get('tags', []))}]" if n.get("tags") else ""
        updated = (n.get("updated_at") or "")[:16].replace("T", " ")
        print(f"  {pin}{n['id']}  {n.get('title', '(untitled)')}{tags}  ({updated})")
        # Show first 80 chars of content
        content_preview = (n.get("content") or "")[:80].replace("\n", " ")
        if content_preview:
            print(f"           {content_preview}")
    print(f"\n  {len(notes)} note(s)")
    return 0


def cmd_get(args: list) -> int:
    note_id = _get_flag(args, "--id")
    as_json = "--json" in args
    if not note_id:
        print("Error: --id is required.", file=sys.stderr)
        return 1

    notes = load_notes()
    note = next((n for n in notes if n.get("id") == note_id), None)
    if not note:
        print(f"Error: note '{note_id}' not found.", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(note, indent=2))
    else:
        pin = " [PINNED]" if note.get("pinned") else ""
        print(f"ID:      {note['id']}{pin}")
        print(f"Title:   {note.get('title', '')}")
        print(f"Tags:    {', '.join(note.get('tags', [])) or '(none)'}")
        print(f"Created: {note.get('created_at', '')}")
        print(f"Updated: {note.get('updated_at', '')}")
        print(f"---")
        print(note.get("content", ""))
    return 0


def cmd_search(args: list) -> int:
    query = _get_flag(args, "--query")
    as_json = "--json" in args
    if not query:
        print("Error: --query is required.", file=sys.stderr)
        return 1

    notes = load_notes()
    query_lower = query.lower()
    matches = [
        n for n in notes
        if query_lower in (n.get("title", "") + " " + n.get("content", "") + " " + " ".join(n.get("tags", []))).lower()
    ]

    if as_json:
        print(json.dumps(matches, indent=2))
        return 0

    if not matches:
        print(f"No notes matching '{query}'.")
        return 0

    print(f"Found {len(matches)} note(s) matching '{query}':\n")
    for n in matches:
        tags = f" [{', '.join(n.get('tags', []))}]" if n.get("tags") else ""
        print(f"  {n['id']}  {n.get('title', '(untitled)')}{tags}")
        # Show matching context
        content = n.get("content", "")
        idx = content.lower().find(query_lower)
        if idx >= 0:
            start = max(0, idx - 30)
            end = min(len(content), idx + len(query) + 30)
            snippet = content[start:end].replace("\n", " ")
            if start > 0:
                snippet = "..." + snippet
            if end < len(content):
                snippet = snippet + "..."
            print(f"           {snippet}")
    return 0


def cmd_edit(args: list) -> int:
    note_id = _get_flag(args, "--id")
    if not note_id:
        print("Error: --id is required.", file=sys.stderr)
        return 1

    new_title = _get_flag(args, "--title")
    new_content = _get_flag(args, "--content")
    new_tags = _get_flag(args, "--tags")
    pin = "--pin" in args
    unpin = "--unpin" in args

    if not any([new_title, new_content, new_tags is not None, pin, unpin]):
        print("No changes specified. Use --title, --content, --tags, --pin, or --unpin.")
        return 1

    with _flock():
        notes = load_notes()
        note = next((n for n in notes if n.get("id") == note_id), None)
        if not note:
            print(f"Error: note '{note_id}' not found.", file=sys.stderr)
            return 1

        if new_title:
            note["title"] = new_title
        if new_content:
            note["content"] = new_content
        if new_tags is not None:
            note["tags"] = [t.strip() for t in new_tags.split(",") if t.strip()]
        if pin:
            note["pinned"] = True
        if unpin:
            note["pinned"] = False

        note["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_notes(notes)
    print(f"Note updated: {note_id} -- {note.get('title', '')}")
    return 0


def cmd_delete(args: list) -> int:
    note_id = _get_flag(args, "--id")
    if not note_id:
        print("Error: --id is required.", file=sys.stderr)
        return 1

    with _flock():
        notes = load_notes()
        before = len(notes)
        notes = [n for n in notes if n.get("id") != note_id]

        if len(notes) == before:
            print(f"Error: note '{note_id}' not found.", file=sys.stderr)
            return 1

        save_notes(notes)
    print(f"Note deleted: {note_id}")
    return 0


def cmd_tags(args: list) -> int:
    notes = load_notes()
    tag_counts: dict[str, int] = {}
    for n in notes:
        for tag in n.get("tags", []):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    if not tag_counts:
        print("No tags found.")
        return 0

    as_json = "--json" in args
    if as_json:
        print(json.dumps(tag_counts, indent=2))
        return 0

    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        print(f"  {tag}: {count} note(s)")
    return 0


def cmd_export(args: list) -> int:
    notes = load_notes()
    fmt = _get_flag(args, "--format") or "md"

    if not notes:
        print("No notes to export.")
        return 0

    # Sort: pinned first, then by created_at
    notes.sort(key=lambda n: (not n.get("pinned", False), n.get("created_at", "")))

    if fmt == "md":
        lines = ["# Notes\n"]
        for n in notes:
            pin = " (pinned)" if n.get("pinned") else ""
            tags = f"Tags: {', '.join(n.get('tags', []))}" if n.get("tags") else ""
            lines.append(f"## {n.get('title', '(untitled)')}{pin}\n")
            if tags:
                lines.append(f"*{tags}*\n")
            lines.append(f"*Created: {n.get('created_at', '')[:16]}*\n")
            lines.append(f"\n{n.get('content', '')}\n")
            lines.append("---\n")
        print("\n".join(lines))
    elif fmt == "json":
        print(json.dumps(notes, indent=2))
    else:
        print(f"Unknown format: {fmt}. Use 'md' or 'json'.", file=sys.stderr)
        return 1
    return 0


def cmd_stats(args: list) -> int:
    notes = load_notes()
    as_json = "--json" in args

    total = len(notes)
    pinned = sum(1 for n in notes if n.get("pinned"))
    all_tags = set()
    for n in notes:
        all_tags.update(n.get("tags", []))

    stats = {
        "total": total,
        "pinned": pinned,
        "unique_tags": len(all_tags),
        "tags": sorted(all_tags),
    }

    if as_json:
        print(json.dumps(stats, indent=2))
    else:
        print(f"  Notes: {total}")
        print(f"  Pinned: {pinned}")
        print(f"  Unique tags: {len(all_tags)}")
        if all_tags:
            print(f"  Tags: {', '.join(sorted(all_tags))}")
    return 0


def _get_flag(args: list, flag: str) -> str | None:
    """Extract --flag value from args list."""
    try:
        idx = args.index(flag)
        if idx + 1 < len(args):
            return args[idx + 1]
    except ValueError:
        pass
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    cmd = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "add": cmd_add,
        "list": cmd_list,
        "get": cmd_get,
        "search": cmd_search,
        "edit": cmd_edit,
        "delete": cmd_delete,
        "tags": cmd_tags,
        "export": cmd_export,
        "stats": cmd_stats,
    }

    if cmd in ("--help", "-h"):
        print(__doc__)
        return 0

    fn = commands.get(cmd)
    if fn is None:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(f"Available: {', '.join(commands.keys())}", file=sys.stderr)
        return 1

    return fn(args)


# --- Public API (for direct import by services) ---

def add_note(title: str, content: str, tags: list[str] | None = None, pinned: bool = False) -> str | None:
    """Add a note programmatically. Returns the note ID on success, None on error."""
    try:
        tags = tags or []
        now = datetime.now(timezone.utc).isoformat()
        note_id = _gen_id(title)
        note = {
            "id": note_id,
            "title": title,
            "content": content,
            "tags": tags,
            "pinned": pinned,
            "created_at": now,
            "updated_at": now,
        }
        with _flock():
            notes = load_notes()
            existing_ids = {n.get("id") for n in notes}
            while note_id in existing_ids:
                note_id = _gen_id(title + note_id)
                note["id"] = note_id
            notes.append(note)
            save_notes(notes)
        return note_id
    except Exception:
        return None


def list_notes(tag: str | None = None) -> list:
    """Return all notes, optionally filtered by tag."""
    notes = load_notes()
    if tag:
        notes = [n for n in notes if tag in n.get("tags", [])]
    notes.sort(key=lambda n: n.get("pinned", False), reverse=True)
    return notes


def search_notes(query: str) -> list:
    """Return notes whose title, content, or tags contain the query string."""
    notes = load_notes()
    q = query.lower()
    return [
        n for n in notes
        if q in (n.get("title", "") + " " + n.get("content", "") + " " + " ".join(n.get("tags", []))).lower()
    ]


if __name__ == "__main__":
    sys.exit(main())
