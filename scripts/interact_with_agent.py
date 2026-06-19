#!/usr/bin/env python3
"""
Interact With Agent
===================
Operator CLI for runtime maintenance of registered agents.

Subcommands:
  clear-chat      Archive then wipe `memory/chat/<name>/chat_history.json`
                  (records appended to chat_history_archive.json first)
                  and the in-memory chat tail. Queued via control flag in
                  agents.json; daemon picks it up on its next sweep.
  clear-session   Wipe `memory/chat/<name>/<name>.session` and reconnect
                  the SDK so the next turn starts a fresh thread (no
                  resume=). Also queued via control flag.
  send-message    Append a message envelope to the target agent's
                  inbox.json. Works for any registered agent
                  (internal/external) and the reserved name `main`.
                  Uses `services.shared.write_to_inbox` for atomic,
                  locked, optionally-deduped writes.

Usage:
  uv run python scripts/interact_with_agent.py clear-chat    --name planner
  uv run python scripts/interact_with_agent.py clear-chat    --all
  uv run python scripts/interact_with_agent.py clear-session --name planner
  uv run python scripts/interact_with_agent.py clear-session --all
  uv run python scripts/interact_with_agent.py send-message  --name planner \\
      --content "Please summarize today's cycle" \\
      [--type message] [--source operator-cli] [--priority 3] \\
      [--reply-to messages/inbox.json] [--id <uuid>] \\
      [--subject "..."] [--from operator] [--no-dedup]

Exit codes:
  0  success / queued (or no-op for external agents on clear-* ops)
  1  invalid arguments, named agent not found, or write failed
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_SERVICES_DIR = _SCRIPT_DIR.parent / "services"
sys.path.insert(0, str(_SERVICES_DIR))
from envelope import parse_from  # noqa: E402
from shared import (  # noqa: E402
    AGENT_CONTROL_CLEAR_CHAT,
    AGENT_CONTROL_CLEAR_SESSION,
    AGENT_CONTROL_FIELD,
    INBOX_FILE,
    read_json_file,
    set_agent_control_flag,
    write_to_inbox,
)

BASE = _SCRIPT_DIR.parent
AGENTS_FILE = BASE / "memory" / "agents.json"

# Default reply_to: the main agent's inbox path. Matches the convention
# used elsewhere in the system (services/internal_agent_chat.py:560,
# services/external_agent_api.py:713) where reply_to is a path string,
# not a message id.
DEFAULT_REPLY_TO = "messages/inbox.json"
DEFAULT_SOURCE = "operator-cli"
DEFAULT_PRIORITY = 3
DEFAULT_TYPE = "message"
RESERVED_MAIN = "main"

# Control flags live as a `control` dict embedded in each agent's
# entry in agents.json. The daemon polls agents.json every sweep
# (SWEEP_SECONDS) and consumes set flags between turns. Both writers
# (this CLI and the daemon's strip-after-apply path) go through
# `locked_json_rw`, which serializes updates with an exclusive flock
# on `agents.json.lock`, so concurrent writers cannot clobber each
# other or partially overwrite the file.
# Re-export the canonical control-field constants from services.shared so
# existing code paths (and tests that patched these as `iac.CONTROL_*`)
# keep working transparently.
CONTROL_FIELD = AGENT_CONTROL_FIELD
CONTROL_CLEAR_CHAT = AGENT_CONTROL_CLEAR_CHAT
CONTROL_CLEAR_SESSION = AGENT_CONTROL_CLEAR_SESSION

# Must match SWEEP_SECONDS in services/internal_agent_chat.py.
SWEEP_SECONDS = 10


def _load_agents() -> list:
    agents = read_json_file(AGENTS_FILE, default=[])
    return agents if isinstance(agents, list) else []


def _find_agent(agents: list, name: str) -> dict | None:
    for a in agents:
        if isinstance(a, dict) and a.get("name") == name:
            return a
    return None


def _active_internal_names(agents: list) -> list[str]:
    out: list[str] = []
    for a in agents:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "internal":
            continue
        if a.get("status") == "deactivated":
            continue
        n = a.get("name")
        if isinstance(n, str) and n:
            out.append(n)
    return out


def _set_control_flag(names: list[str], key: str, label: str) -> int:
    """Thin CLI wrapper around `services.shared.set_agent_control_flag`.

    The shared helper handles the locked read-modify-write so this CLI
    and the portal mutate `agents.json` through the same code path.
    """
    if not names:
        return 0
    ok, matched = set_agent_control_flag(names, key, agents_file=AGENTS_FILE)
    if not ok:
        print("error: failed to update agents.json", file=sys.stderr)
        return 1

    matched_set = set(matched)
    for n in names:
        if n in matched_set:
            print(
                "queued "
                + label
                + " for "
                + repr(n)
                + "; daemon applies within ~"
                + str(SWEEP_SECONDS)
                + "s"
            )
        else:
            # Should not happen — _resolve_targets validated existence —
            # but flag it loudly if it does.
            print(
                "warning: agent "
                + repr(n)
                + " disappeared from agents.json before flag could be written",
                file=sys.stderr,
            )
    return 0


def _resolve_targets(args, label: str) -> tuple[list[str], int]:
    """Return (target_names, exit_code_addition).

    Validates --name vs --all and filters by type=internal. Prints a
    skip notice for an external named agent (exit 0) and an error for
    an unknown named agent (exit 1).
    """
    agents = _load_agents()

    if args.all:
        names = _active_internal_names(agents)
        if not names:
            print("(no active internal agents found)")
        return names, 0

    name = args.name.strip()
    if not name:
        print("error: --name must not be empty", file=sys.stderr)
        return [], 1
    entry = _find_agent(agents, name)
    if entry is None:
        print(
            "error: no agent named " + repr(name) + " in agents.json", file=sys.stderr
        )
        return [], 1
    if entry.get("type") == "external":
        print(
            "skipped " + repr(name) + ": " + label + " only applies to internal agents"
        )
        return [], 0
    if entry.get("type") != "internal":
        print(
            "error: agent "
            + repr(name)
            + " has unsupported type="
            + repr(entry.get("type")),
            file=sys.stderr,
        )
        return [], 1
    return [name], 0


def cmd_clear_chat(args) -> int:
    targets, rc = _resolve_targets(args, "clear-chat")
    if rc:
        return rc
    return _set_control_flag(targets, CONTROL_CLEAR_CHAT, "clear-chat")


def cmd_clear_session(args) -> int:
    targets, rc = _resolve_targets(args, "clear-session")
    if rc:
        return rc
    return _set_control_flag(targets, CONTROL_CLEAR_SESSION, "clear-session")


def _resolve_inbox_path(name: str) -> tuple[Path | None, int]:
    """Map an agent name to the inbox path used by the inbox writer.

    The reserved name `main` maps to `services.shared.INBOX_FILE`. Every
    other name must have an entry in `agents.json` with a non-empty
    `inbox` field; that field is interpreted relative to the repo root
    (matching the convention used by `register_internal_agent.py`).
    """
    if name == RESERVED_MAIN:
        return INBOX_FILE, 0
    agents = _load_agents()
    entry = _find_agent(agents, name)
    if entry is None:
        print(
            "error: no agent named " + repr(name) + " in agents.json", file=sys.stderr
        )
        return None, 1
    inbox = entry.get("inbox")
    if not isinstance(inbox, str) or not inbox:
        print(
            "error: agent " + repr(name) + " has no `inbox` path in agents.json",
            file=sys.stderr,
        )
        return None, 1
    p = Path(inbox)
    if not p.is_absolute():
        p = BASE / p
    return p, 0


def cmd_send_message(args) -> int:
    name = args.name.strip()
    if not name:
        print("error: --name must not be empty", file=sys.stderr)
        return 1

    if args.content is not None and args.content_file is not None:
        print(
            "error: --content and --content-file are mutually exclusive",
            file=sys.stderr,
        )
        return 1
    if args.content is None and args.content_file is None:
        print("error: one of --content or --content-file is required", file=sys.stderr)
        return 1

    if args.content_file is not None:
        try:
            content = Path(args.content_file).read_text()
        except OSError as exc:
            print("error: could not read --content-file: " + str(exc), file=sys.stderr)
            return 1
    else:
        content = args.content

    if not content:
        print("error: message content must not be empty", file=sys.stderr)
        return 1

    priority = args.priority
    if not (1 <= priority <= 5):
        print("error: --priority must be between 1 and 5", file=sys.stderr)
        return 1

    target, rc = _resolve_inbox_path(name)
    if target is None:
        return rc

    msg_id = (args.id or "").strip() or str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    envelope: dict = {
        "id": msg_id,
        "type": args.type,
        "content": content,
        "priority": priority,
        "timestamp": now,
        "reply_to": args.reply_to,
    }
    if args.subject:
        envelope["subject"] = args.subject
    # Build the structured `from`; source now lives in from.source.
    from_obj: dict = {}
    if getattr(args, "from_", None):
        # Accept a JSON object (structured identity) or a plain string, which
        # parse_from wraps as {"raw": ...} so it degrades gracefully.
        raw_from = args.from_
        try:
            parsed = json.loads(raw_from)
        except (ValueError, TypeError):
            parsed = raw_from
        from_obj = parse_from(parsed)
    if args.source:
        from_obj.setdefault("source", args.source)
    if from_obj:
        envelope["from"] = from_obj

    target.parent.mkdir(parents=True, exist_ok=True)
    ok = write_to_inbox([envelope], inbox_file=target, dedup=not args.no_dedup)
    if not ok:
        print("error: write_to_inbox failed for " + str(target), file=sys.stderr)
        return 1

    print("sent message id=" + msg_id + " to " + str(target))
    return 0


def _add_target_args(sp: argparse.ArgumentParser) -> None:
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--name", help="Target agent name")
    g.add_argument(
        "--all",
        action="store_true",
        help="Apply to every active internal agent",
    )


def main() -> int:
    p = argparse.ArgumentParser(
        description="Operator CLI for runtime maintenance of registered agents",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_chat = sub.add_parser(
        "clear-chat",
        help="Archive + clear memory/chat/<name>/chat_history.json (queued via control flag)",
    )
    _add_target_args(p_chat)
    p_chat.set_defaults(func=cmd_clear_chat)

    p_sess = sub.add_parser(
        "clear-session",
        help="Reset the SDK resume id for an internal agent (queued via sentinel)",
    )
    _add_target_args(p_sess)
    p_sess.set_defaults(func=cmd_clear_session)

    p_send = sub.add_parser(
        "send-message",
        help=(
            "Append a message envelope to a registered agent's inbox.json "
            "(or to the main inbox via --name main)"
        ),
    )
    p_send.add_argument(
        "--name",
        required=True,
        help="Target agent name (use 'main' for the main agent inbox)",
    )
    g_content = p_send.add_mutually_exclusive_group(required=False)
    g_content.add_argument("--content", help="Message body (text)")
    g_content.add_argument("--content-file", help="Read message body from file")
    p_send.add_argument(
        "--type",
        default=DEFAULT_TYPE,
        help="Envelope type (default: %(default)s). Internal agents only "
        "process type=message.",
    )
    p_send.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help="Source label (default: %(default)s)",
    )
    p_send.add_argument(
        "--priority",
        type=int,
        default=DEFAULT_PRIORITY,
        help="Priority 1-5 (default: %(default)s)",
    )
    p_send.add_argument(
        "--reply-to",
        default=DEFAULT_REPLY_TO,
        help="Path the recipient should reply to (default: %(default)s)",
    )
    p_send.add_argument("--id", help="Override the generated uuid4 message id")
    p_send.add_argument(
        "--subject", help="Optional subject (used by internal-agent prompts)"
    )
    p_send.add_argument(
        "--from",
        dest="from_",
        help="Optional `from` field (used by internal-agent prompts)",
    )
    p_send.add_argument(
        "--no-dedup",
        action="store_true",
        help="Disable type+content dedup in write_to_inbox",
    )
    p_send.set_defaults(func=cmd_send_message)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
