#!/usr/bin/env python3
"""
Register Internal Agent
=======================
Adds (or updates) an entry in /agent/memory/agents.json and creates the
empty per-agent files at /agent/messages/internal/<name>/.

The matching daemon `services/internal_agent_chat.py` hot-reloads
agents.json on its sweep tick (10s), so a new internal agent is picked
up without a restart.

Most SDK options (allowed_tools, permission_mode, cwd, add_dirs, ...)
are fixed and identical to `app/chat.py`. The per-agent customizations
are an OPTIONAL `system_prompt` text that gets APPENDED to the shared
pre-built system prompt, plus optional `model` and `effort` overrides
for the agent's SDK session.

Outbound delivery uses the per-session `send_reply` MCP tool — the LLM
chooses where to deliver. The agents.json `outbox_routing_rules` list
shapes what the tool's description tells the LLM about valid recipients.
Each rule is `{"description": "...", "agent": "<name>"}`. The reserved
name `main` is always available even with no rules.

Usage:
  uv run python scripts/register_internal_agent.py \\
      --name planner \\
      --responsibilities "Plan multi-step tasks for the main agent" \\
      [--system-prompt-file /path/to/prompt.md] \\
      [--outbox-routing-rules-file /path/to/rules.json] \\
      [--model sonnet] [--effort high]

  uv run python scripts/register_internal_agent.py --list
  uv run python scripts/register_internal_agent.py --deactivate planner
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_SERVICES_DIR = _SCRIPT_DIR.parent / "services"
sys.path.insert(0, str(_SERVICES_DIR))
from shared import (  # noqa: E402
    EFFORT_LEVELS,
    chat_archive_path,
    chat_history_path,
    ensure_chat_dir,
    locked_json_rw,
    normalize_effort,
    normalize_model,
    read_json_file,
    session_path,
)

BASE = _SCRIPT_DIR.parent
AGENTS_FILE = BASE / "memory" / "agents.json"
INTERNAL_DIR = BASE / "messages" / "internal"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def _ensure_files(name: str) -> tuple[Path, Path, Path]:
    # Inbox files stay under messages/internal/<name>/.
    agent_dir = INTERNAL_DIR / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    inbox = agent_dir / "inbox.json"
    inbox_history = agent_dir / "inbox_history.json"
    for f in (inbox, inbox_history):
        if not f.exists():
            f.write_text("[]")
    # Chat history (and archive + .session) live under
    # /agent/memory/chat/<name>/ in the unified layout.
    ensure_chat_dir(name)
    return inbox, inbox_history, chat_history_path(name)


def _load_json_arg(file_arg: str | None, inline_arg: str | None):
    if file_arg:
        return json.loads(Path(file_arg).read_text())
    if inline_arg:
        return json.loads(inline_arg)
    return None


def _load_text_arg(file_arg: str | None, inline_arg: str | None) -> str | None:
    if file_arg:
        return Path(file_arg).read_text()
    if inline_arg is not None:
        return inline_arg
    return None


def cmd_register(args) -> int:
    name = args.name.strip()
    if not _NAME_RE.match(name):
        raise SystemExit(
            "invalid agent name " + repr(name) + ": must match " + _NAME_RE.pattern
        )

    inbox, inbox_history, chat_history = _ensure_files(name)

    outbox_routing_rules = _load_json_arg(
        args.outbox_routing_rules_file, args.outbox_routing_rules_inline
    )
    if outbox_routing_rules is not None and not isinstance(outbox_routing_rules, list):
        raise SystemExit("outbox-routing-rules must be a JSON list")
    if isinstance(outbox_routing_rules, list):
        for rule in outbox_routing_rules:
            if not isinstance(rule, dict):
                raise SystemExit("each outbox_routing_rules entry must be an object")
            if not rule.get("agent") or not isinstance(rule.get("agent"), str):
                raise SystemExit("each rule must include a non-empty `agent` string")
            if not rule.get("description") or not isinstance(
                rule.get("description"), str
            ):
                raise SystemExit("each rule must include a `description` string")

    system_prompt = _load_text_arg(args.system_prompt_file, args.system_prompt_inline)

    model = normalize_model(args.model)

    # The daemon coerces a bad effort quietly (agents.json is hand-editable);
    # the CLI rejects it loudly so a typo is caught at registration time.
    effort = normalize_effort(args.effort)
    if effort is None and (args.effort or "").strip():
        raise SystemExit(
            "invalid --effort "
            + repr(args.effort)
            + ": must be one of "
            + ", ".join(EFFORT_LEVELS)
        )

    entry = {
        "type": "internal",
        "name": name,
        "status": "online",
        "inbox": str(inbox),
        "responsibilities": args.responsibilities or "",
    }
    if system_prompt is not None:
        entry["system_prompt"] = system_prompt
    if outbox_routing_rules is not None:
        entry["outbox_routing_rules"] = outbox_routing_rules
    if model is not None:
        entry["model"] = model
    # Note: like `model`, omitting the flag leaves any previously stored value
    # in place (the merge below is `{**existing, **entry}`), so re-registering
    # cannot clear a prior selection.
    if effort is not None:
        entry["effort"] = effort

    def _rw(items):
        if not isinstance(items, list):
            items = []
        for i, existing in enumerate(items):
            if isinstance(existing, dict) and existing.get("name") == name:
                merged = {**existing, **entry}
                # Re-registration intentionally clears "deactivated".
                items[i] = merged
                return items
        items.append(entry)
        return items

    if not locked_json_rw(_rw, json_file=AGENTS_FILE, default=[]):
        raise SystemExit("failed to update agents.json")
    print("Registered/updated internal agent " + repr(name))
    print("  inbox:         " + str(inbox))
    print("  inbox_history: " + str(inbox_history))
    print("  chat_history:  " + str(chat_history))
    return 0


def cmd_list(_args) -> int:
    agents = read_json_file(AGENTS_FILE, default=[])
    internal = [
        a for a in agents if isinstance(a, dict) and a.get("type") == "internal"
    ]
    if not internal:
        print("(no internal agents registered)")
        return 0
    for a in internal:
        rules = a.get("outbox_routing_rules") or []
        print(
            "- "
            + str(a.get("name") or "").ljust(20)
            + " status="
            + str(a.get("status") or "").ljust(12)
            + " rules="
            + str(len(rules) if isinstance(rules, list) else 0)
            + "  "
            + str(a.get("responsibilities") or "")[:80]
        )
    return 0


def cmd_deactivate(args) -> int:
    name = args.deactivate.strip()
    matched = {"hit": False}

    def _rw(items):
        if not isinstance(items, list):
            return items
        for a in items:
            if (
                isinstance(a, dict)
                and a.get("name") == name
                and a.get("type") == "internal"
            ):
                a["status"] = "deactivated"
                matched["hit"] = True
        return items

    locked_json_rw(_rw, json_file=AGENTS_FILE, default=[])
    if not matched["hit"]:
        print("No internal agent named " + repr(name), file=sys.stderr)
        return 1
    print("Deactivated " + repr(name))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Register an internal agent")
    p.add_argument("--name", help="Agent name (snake/kebab-case)")
    p.add_argument("--responsibilities", default="", help="Free-text duties")
    p.add_argument(
        "--system-prompt-file",
        help=(
            "Path to system-prompt text file. The contents are APPENDED "
            "to the shared pre-built system prompt (system.md, "
            "constitution.md, public_url, prior chat history, "
            "claude-system-prompt.md) — they do not replace it. "
            "All other SDK options (allowed_tools, permission_mode, cwd, "
            "etc.) are fixed and identical to app/chat.py."
        ),
    )
    p.add_argument(
        "--system-prompt-inline", help="Inline system-prompt text (appended)"
    )
    p.add_argument(
        "--outbox-routing-rules-file",
        help=(
            "Path to JSON file with the outbox_routing_rules list. "
            'Each entry must look like {"description": "...", "agent": "<name>"}. '
            "Each rule contributes one bullet to the LLM-visible description "
            "of the per-session `send_reply` tool."
        ),
    )
    p.add_argument(
        "--outbox-routing-rules-inline",
        help="Inline JSON list of outbox_routing_rules",
    )
    p.add_argument(
        "--model",
        help=(
            "Optional model override for this agent's SDK session. Accepts "
            "short aliases ('haiku', 'sonnet', 'opus') or a full model id; "
            "the SDK handles resolution. Omit to use the SDK default. "
            "Re-registering with a different value replaces the prior "
            "selection; takes effect on the next session connect "
            "(restart the daemon or trigger clear_session)."
        ),
    )
    p.add_argument(
        "--effort",
        help=(
            "Optional effort level for this agent's SDK session: "
            + ", ".join(EFFORT_LEVELS)
            + ". Guides how much the model thinks per turn (same knob as the "
            "`claude --effort` CLI flag). Omit to use the SDK default. "
            "Takes effect on the next session connect (restart the daemon or "
            "trigger clear_session)."
        ),
    )
    p.add_argument("--list", action="store_true", help="List internal agents")
    p.add_argument(
        "--deactivate",
        metavar="NAME",
        help="Mark an internal agent as deactivated",
    )
    args = p.parse_args()

    if args.list:
        return cmd_list(args)
    if args.deactivate:
        return cmd_deactivate(args)
    if not args.name:
        p.error("--name is required (or use --list / --deactivate)")
    return cmd_register(args)


if __name__ == "__main__":
    sys.exit(main())
