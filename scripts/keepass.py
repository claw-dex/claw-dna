#!/usr/bin/env python3
"""
keepass.py -- KeePass credential manager for the agent.

Manage credentials stored in a KeePass database.
The database uses no password and no keyfile (container is the security boundary).

Usage:
    python3 keepass.py init
    python3 keepass.py list [--group GROUP] [--json]
    python3 keepass.py get TITLE [--json]
    python3 keepass.py store --title T --username U --password P [--url URL] [--notes N] [--group G] [--json]
    python3 keepass.py delete TITLE [--json]
    python3 keepass.py groups [--json]
    python3 keepass.py search QUERY [--json]

Exit codes:
    0 = success
    1 = error
    2 = not found / empty result
"""

import argparse
import json
import sys
from pathlib import Path

KEEPASS_DIR = Path("/home/agent/.keepass")
DB_PATH = KEEPASS_DIR / "credentials.kdbx"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _open_db():
    """Open the KeePass database. Returns (kp, None) on success or (None, error_msg) on failure."""
    if not DB_PATH.exists():
        return None, f"Database not found at {DB_PATH}. Run 'init' first."
    try:
        from pykeepass import PyKeePass

        kp = PyKeePass(str(DB_PATH), password="")
        return kp, None
    except Exception as exc:
        return None, f"Failed to open database: {exc}"


def _entry_to_dict(entry, include_password=False):
    """Serialize a pykeepass Entry to a plain dict."""
    group_path = "/".join(entry.group.path) if entry.group and entry.group.path else ""
    d = {
        "title": entry.title or "",
        "username": entry.username or "",
        "url": entry.url or "",
        "notes": entry.notes or "",
        "tags": entry.tags or [],
        "group": group_path,
    }
    if include_password:
        d["password"] = entry.password or ""
    if entry.ctime:
        d["created"] = entry.ctime.isoformat()
    if entry.mtime:
        d["modified"] = entry.mtime.isoformat()
    return d


def _find_or_create_group(kp, group_path):
    """Find or create a nested group from a path like 'Services/AWS'."""
    parts = [p.strip() for p in group_path.split("/") if p.strip()]
    current = kp.root_group
    for part in parts:
        found = kp.find_groups(name=part, group=current, recursive=False, first=True)
        if found:
            current = found
        else:
            current = kp.add_group(current, part)
    return current


def _print_result(data, as_json, human_fmt=None):
    """Print result as JSON or human-readable."""
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    elif human_fmt:
        print(human_fmt)
    else:
        print(data)


# ── Subcommands ───────────────────────────────────────────────────────────────


def cmd_init(args):
    """Create the KeePass database if it does not exist."""
    if DB_PATH.exists():
        msg = f"Database already exists at {DB_PATH}"
        _print_result({"status": "exists", "path": str(DB_PATH)}, args.json, msg)
        return 0

    try:
        from pykeepass import create_database

        KEEPASS_DIR.mkdir(parents=True, exist_ok=True)
        kp = create_database(str(DB_PATH), password="")
        kp.save()
        msg = f"Created database at {DB_PATH}"
        _print_result({"status": "created", "path": str(DB_PATH)}, args.json, msg)
        return 0
    except Exception as exc:
        print(f"Error creating database: {exc}", file=sys.stderr)
        return 1


def cmd_list(args):
    """List all credential entries (no passwords)."""
    kp, err = _open_db()
    if err:
        print(err, file=sys.stderr)
        return 1

    entries = kp.entries
    if args.group:
        group = kp.find_groups(name=args.group, first=True)
        if not group:
            print(f"Group '{args.group}' not found.", file=sys.stderr)
            return 2
        entries = [e for e in entries if e.group == group]

    if not entries:
        _print_result({"entries": [], "count": 0}, args.json, "No entries found.")
        return 2

    result = [_entry_to_dict(e) for e in entries]

    if args.json:
        _print_result({"entries": result, "count": len(result)}, True)
    else:
        for e in result:
            group_label = f" [{e['group']}]" if e["group"] else ""
            print(f"  {e['title']}{group_label}  —  {e['username']}  {e['url']}")
        print(f"\n{len(result)} entries total.")
    return 0


def cmd_get(args):
    """Get a specific entry by title (includes password)."""
    kp, err = _open_db()
    if err:
        print(err, file=sys.stderr)
        return 1

    entry = kp.find_entries(title=args.title, first=True)
    if not entry:
        msg = f"Entry '{args.title}' not found."
        if args.json:
            _print_result({"error": msg}, True)
        else:
            print(msg, file=sys.stderr)
        return 2

    result = _entry_to_dict(entry, include_password=True)

    if args.json:
        _print_result(result, True)
    else:
        print(f"  Title:    {result['title']}")
        print(f"  Username: {result['username']}")
        print(f"  Password: {result['password']}")
        print(f"  URL:      {result['url']}")
        print(f"  Group:    {result['group']}")
        if result["notes"]:
            print(f"  Notes:    {result['notes']}")
    return 0


def cmd_store(args):
    """Store a new credential or update an existing one."""
    kp, err = _open_db()
    if err:
        print(err, file=sys.stderr)
        return 1

    # Determine target group
    dest_group = kp.root_group
    if args.group:
        dest_group = _find_or_create_group(kp, args.group)

    # Check if entry with same title already exists in the target group
    existing = kp.find_entries(title=args.title, group=dest_group, first=True)
    if existing:
        existing.username = args.username
        existing.password = args.password
        if args.url is not None:
            existing.url = args.url
        if args.notes is not None:
            existing.notes = args.notes
        if args.group and existing.group != dest_group:
            kp.move_entry(existing, dest_group)
        kp.save()
        msg = f"Updated entry '{args.title}'"
        _print_result({"status": "updated", "title": args.title}, args.json, msg)
    else:
        kp.add_entry(
            dest_group,
            title=args.title,
            username=args.username,
            password=args.password,
            url=args.url or "",
            notes=args.notes or "",
        )
        kp.save()
        msg = f"Stored entry '{args.title}'"
        _print_result({"status": "created", "title": args.title}, args.json, msg)
    return 0


def cmd_delete(args):
    """Delete an entry by title."""
    kp, err = _open_db()
    if err:
        print(err, file=sys.stderr)
        return 1

    entry = kp.find_entries(title=args.title, first=True)
    if not entry:
        msg = f"Entry '{args.title}' not found."
        if args.json:
            _print_result({"error": msg}, True)
        else:
            print(msg, file=sys.stderr)
        return 2

    kp.delete_entry(entry)
    kp.save()
    msg = f"Deleted entry '{args.title}'"
    _print_result({"status": "deleted", "title": args.title}, args.json, msg)
    return 0


def cmd_groups(args):
    """List all groups with entry counts."""
    kp, err = _open_db()
    if err:
        print(err, file=sys.stderr)
        return 1

    groups = kp.groups
    result = []
    for g in groups:
        path = "/".join(g.path) if g.path else "(root)"
        entry_count = len(g.entries) if g.entries else 0
        result.append({"path": path, "name": g.name, "entries": entry_count})

    if not result:
        _print_result({"groups": [], "count": 0}, args.json, "No groups found.")
        return 2

    if args.json:
        _print_result({"groups": result, "count": len(result)}, True)
    else:
        for g in result:
            print(f"  {g['path']}  ({g['entries']} entries)")
        print(f"\n{len(result)} groups total.")
    return 0


def cmd_search(args):
    """Search entries by title, username, or URL."""
    import re

    kp, err = _open_db()
    if err:
        print(err, file=sys.stderr)
        return 1

    query = args.query
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error as exc:
        print(f"Invalid regex pattern '{query}': {exc}", file=sys.stderr)
        return 1
    matches = []
    for entry in kp.entries:
        if (
            (entry.title and pattern.search(entry.title))
            or (entry.username and pattern.search(entry.username))
            or (entry.url and pattern.search(entry.url))
        ):
            matches.append(_entry_to_dict(entry))

    if not matches:
        msg = f"No entries matching '{query}'."
        if args.json:
            _print_result({"entries": [], "count": 0, "query": query}, True)
        else:
            print(msg)
        return 2

    if args.json:
        _print_result({"entries": matches, "count": len(matches), "query": query}, True)
    else:
        for e in matches:
            group_label = f" [{e['group']}]" if e["group"] else ""
            print(f"  {e['title']}{group_label}  —  {e['username']}  {e['url']}")
        print(f"\n{len(matches)} matches for '{query}'.")
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="KeePass credential manager for the agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # init
    sub.add_parser("init", help="Create the KeePass database if missing")

    # list
    p_list = sub.add_parser("list", help="List all entries (no passwords)")
    p_list.add_argument("--group", help="Filter by group name")

    # get
    p_get = sub.add_parser("get", help="Get entry details including password")
    p_get.add_argument("title", help="Entry title to retrieve")

    # store
    p_store = sub.add_parser("store", help="Store or update a credential")
    p_store.add_argument("--title", required=True, help="Entry title")
    p_store.add_argument("--username", required=True, help="Username / login")
    p_store.add_argument("--password", required=True, help="Password / secret")
    p_store.add_argument("--url", help="URL associated with the credential")
    p_store.add_argument("--notes", help="Additional notes")
    p_store.add_argument("--group", help="Group path (e.g. 'Services/AWS')")

    # delete
    p_del = sub.add_parser("delete", help="Delete an entry")
    p_del.add_argument("title", help="Entry title to delete")

    # groups
    sub.add_parser("groups", help="List all groups with entry counts")

    # search
    p_search = sub.add_parser("search", help="Search entries by title/username/url")
    p_search.add_argument("query", help="Search query (regex supported)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    cmd_map = {
        "init": cmd_init,
        "list": cmd_list,
        "get": cmd_get,
        "store": cmd_store,
        "delete": cmd_delete,
        "groups": cmd_groups,
        "search": cmd_search,
    }

    sys.exit(cmd_map[args.command](args))


if __name__ == "__main__":
    main()
