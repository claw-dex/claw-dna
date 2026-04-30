#!/usr/bin/env python3
"""
Register External Agent
=======================
Adds (or updates) an entry in /agent/memory/agents.json and creates the
empty per-agent inbox/outbox files at /agent/messages/external/<name>/.

Usage:
  uv run python scripts/register_external_agent.py \
      --name research-bot \
      --responsibilities "Run web research tasks delegated by main agent" \
      --capabilities-file /tmp/caps.json \
      [--timeout-seconds 300]

  uv run python scripts/register_external_agent.py \
      --name research-bot \
      --responsibilities "..." \
      --capabilities-inline '[{"id":"web_search","name":"Web Search","description":"...","category":"automation","enabled":true}]'

  # Operational:
  uv run python scripts/register_external_agent.py --list
  uv run python scripts/register_external_agent.py --deactivate research-bot
  uv run python scripts/register_external_agent.py --setup research-bot
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

# Resolve the services dir relative to this script so it works locally and in
# the deployed container (/agent/scripts → /agent/services).
_SCRIPT_DIR = Path(__file__).resolve().parent
_SERVICES_DIR = _SCRIPT_DIR.parent / "services"
sys.path.insert(0, str(_SERVICES_DIR))
from shared import locked_json_rw, read_json_file  # noqa: E402

# Repo-relative paths so this script can run outside the deployed container too.
BASE = _SCRIPT_DIR.parent
AGENTS_FILE = BASE / "memory" / "agents.json"
EXTERNAL_DIR = BASE / "messages" / "external"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")

# Portal config paths — kept in sync with scripts/portal_config.py.
PORTAL_CONFIG_PATH = BASE / "memory" / "portal_config.json"
KEEPASS_DB = Path("/home/agent/.keepass/credentials.kdbx")
KEEPASS_GROUP = "System"
KEEPASS_PORTAL_BASIC_AUTH = "PORTAL_BASIC_AUTH"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def _ensure_files(name: str) -> tuple[Path, Path]:
    agent_dir = EXTERNAL_DIR / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    inbox = agent_dir / "inbox.json"
    outbox = agent_dir / "outbox.json"
    for f in (inbox, outbox):
        if not f.exists():
            f.write_text("[]")
    return inbox, outbox


def _load_capabilities(args) -> list:
    if args.capabilities_file:
        data = json.loads(Path(args.capabilities_file).read_text())
    elif args.capabilities_inline:
        data = json.loads(args.capabilities_inline)
    else:
        return []
    if not isinstance(data, list):
        raise SystemExit("capabilities must be a JSON list")
    return data


def cmd_register(args) -> int:
    name = args.name.strip()
    if not _NAME_RE.match(name):
        raise SystemExit(f"invalid agent name {name!r}: must match {_NAME_RE.pattern}")
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be > 0")

    capabilities = _load_capabilities(args)
    inbox, outbox = _ensure_files(name)

    entry = {
        "type": "external",
        "name": name,
        "inbox": str(inbox),
        "outbox": str(outbox),
        "capabilities": capabilities,
        "responsibilities": args.responsibilities,
        "status": "offline",
        "timeout_seconds": args.timeout_seconds,
        "last_ping_at": None,
    }

    def _rw(items):
        if not isinstance(items, list):
            items = []
        for i, existing in enumerate(items):
            if isinstance(existing, dict) and existing.get("name") == name:
                merged = {**existing, **entry}
                # Preserve liveness fields so re-registering doesn't drop a
                # currently-online agent into "offline".
                if existing.get("last_ping_at"):
                    merged["last_ping_at"] = existing["last_ping_at"]
                # Re-registration intentionally clears "deactivated" so the
                # operator can use it to bring an agent back. Other statuses
                # are recomputed by the sweeper.
                items[i] = merged
                return items
        items.append(entry)
        return items

    if not locked_json_rw(_rw, json_file=AGENTS_FILE, default=[]):
        raise SystemExit("failed to update agents.json")
    print(f"Registered/updated agent {name!r}")
    print(f"  inbox:  {inbox}")
    print(f"  outbox: {outbox}")
    return 0


def cmd_list(_args) -> int:
    agents = read_json_file(AGENTS_FILE, default=[])
    if not agents:
        print("(no agents registered)")
        return 0
    for a in agents:
        if not isinstance(a, dict):
            continue
        print(
            f"- {str(a.get('name') or ''):20s} "
            f"type={str(a.get('type') or ''):8s} "
            f"status={str(a.get('status') or ''):12s} "
            f"timeout={a.get('timeout_seconds')}s "
            f"last_ping={a.get('last_ping_at')}"
        )
    return 0


def cmd_deactivate(args) -> int:
    name = args.deactivate.strip()
    matched = {"hit": False}

    def _rw(items):
        if not isinstance(items, list):
            return items
        for a in items:
            if isinstance(a, dict) and a.get("name") == name:
                a["status"] = "deactivated"
                matched["hit"] = True
        return items

    locked_json_rw(_rw, json_file=AGENTS_FILE, default=[])
    if not matched["hit"]:
        print(f"No agent named {name!r}", file=sys.stderr)
        return 1
    print(f"Deactivated {name!r}")
    return 0


def _load_public_url() -> str | None:
    if not PORTAL_CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(PORTAL_CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    url = cfg.get("public_url")
    return url.rstrip("/") if isinstance(url, str) and url else None


def _load_basic_auth() -> tuple[str | None, str | None]:
    """Return (username, password) from KeePass, or (None, None) if not configured."""
    if not KEEPASS_DB.exists():
        return None, None
    try:
        from pykeepass import PyKeePass  # type: ignore[import-not-found]

        kp = PyKeePass(str(KEEPASS_DB), password="")
        entry = kp.find_entries(title=KEEPASS_PORTAL_BASIC_AUTH, first=True)
        if not entry:
            return None, None
        return entry.username or None, entry.password or None
    except Exception:
        return None, None


def cmd_setup(args) -> int:
    name = args.setup.strip()
    if not _NAME_RE.match(name):
        print(f"Invalid agent name {name!r}", file=sys.stderr)
        return 1

    agents = read_json_file(AGENTS_FILE, default=[])
    agent = next(
        (a for a in agents if isinstance(a, dict) and a.get("name") == name), None
    )
    if agent is None:
        print(
            f"No agent named {name!r} in {AGENTS_FILE}. "
            "Register it first with --name <name> ...",
            file=sys.stderr,
        )
        return 1

    public_url = _load_public_url()
    base_url = (public_url or "https://<your-host>") + "/external-agent"

    user, password = _load_basic_auth()
    creds = f"{user}:{password}" if user and password else "<user>:<pass>"

    instruction = (
        f"You are external agent '{name}'. Each tick: "
        f"`curl -s -u {creds} -H 'X-Agent-Name: {name}' {base_url}/ping` — "
        "if unread_count>0, "
        f"`curl -s -u {creds} -H 'X-Agent-Name: {name}' -H 'Content-Type: application/json' "
        f"-d '{{\"ids\":<unread_ids>}}' {base_url}/read-inbox` to fetch+mark-read, "
        "do the work, then reply via "
        f"`curl -s -u {creds} -H 'X-Agent-Name: {name}' -H 'Content-Type: application/json' "
        f'-d \'{{"type":"response","subject":"...","content":"..."}}\' '
        f"{base_url}/write-outbox` (type ∈ response|needs_human|error|info; subject+content non-empty). "
        "On any 4xx, read the response's `readme` field and self-correct."
    )

    print("Paste this to your Claude Code/Codex:")
    print(f"```/loop 1m {instruction}```")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Register an external agent")
    p.add_argument("--name", help="Agent name (snake-case or kebab-case)")
    p.add_argument("--responsibilities", default="", help="Free-text duties")
    p.add_argument(
        "--capabilities-file", help="Path to JSON file with capabilities list"
    )
    p.add_argument("--capabilities-inline", help="Inline JSON list of capabilities")
    p.add_argument("--timeout-seconds", type=int, default=300)
    p.add_argument("--list", action="store_true", help="List registered agents")
    p.add_argument("--deactivate", metavar="NAME", help="Mark an agent as deactivated")
    p.add_argument(
        "--setup",
        metavar="NAME",
        help="Print connection instructions for an already-registered agent",
    )
    args = p.parse_args()

    if args.list:
        return cmd_list(args)
    if args.deactivate:
        return cmd_deactivate(args)
    if args.setup:
        return cmd_setup(args)
    if not args.name:
        p.error("--name is required (or use --list / --deactivate / --setup)")
    return cmd_register(args)


if __name__ == "__main__":
    sys.exit(main())
