#!/usr/bin/env python3
"""
callmebot.py -- Voice call escalation via CallMeBot API.

Makes voice calls to the agent owner's Telegram when human intervention
is needed but the human hasn't responded. Uses the free CallMeBot
Telegram Call API (https://www.callmebot.com/telegram-call-api/).

Prerequisites:
    1. TELEGRAM_OWNER_USERNAME must be set in KeePass (shared with telegram_bridge)
    2. User must send /start to @CallMeBot_txtbot on Telegram

Usage:
    python3 callmebot.py setup [--lang en-US-Standard-B]
    python3 callmebot.py call --text "I need your API key"
    python3 callmebot.py status [--json]

Exit codes:
    0 = success
    1 = error
    2 = rate-limited
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

BASE = Path("/agent")
STATE_FILE = BASE / "memory" / "callmebot_state.json"
SCRIPTS_DIR = BASE / "scripts"

KEEPASS_TELEGRAM_OWNER_USERNAME = "TELEGRAM_OWNER_USERNAME"  # shared with telegram_bridge
KEEPASS_CALLMEBOT_LANG = "CALLMEBOT_LANG"

# CallMeBot only provides an HTTP endpoint (no HTTPS available).
API_URL = "http://api.callmebot.com/start.php"
DEFAULT_LANG = "en-US-Standard-B"
MAX_TEXT_LENGTH = 256
COOLDOWN_SECONDS = 1800  # 30 minutes cooldown between calls to avoid spamming the user
HTTP_TIMEOUT = 10  # seconds

# API response patterns indicating the user hasn't authorized CallMeBot
UNAUTHORIZED_REGEX = re.compile(r"Authorization for user @\w+ is not received\.", re.IGNORECASE)
UNAUTHORIZED_EXACT = "Warning! User not authorized."
# API response pattern for wrong username format (missing @ prefix)
FORMAT_ERROR_REGEX = re.compile(r"ERROR:\s*User\s+\S+\s+has wrong format", re.IGNORECASE)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _normalize_username(user: str) -> str:
    """Ensure the username has the @ prefix required by CallMeBot API."""
    if user and not user.startswith("@"):
        return f"@{user}"
    return user


def _print_result(data, as_json, human_fmt=None):
    """Print result as JSON or human-readable."""
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    elif human_fmt:
        print(human_fmt)
    else:
        print(data)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _load_state() -> dict:
    """Load callmebot state file."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"last_call_timestamp": None, "total_calls": 0}


def _save_state(state: dict):
    """Atomically save callmebot state file."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_FILE.parent, suffix=".tmp")
    try:
        with open(fd, "w") as f:
            json.dump(state, f, indent=2)
        Path(tmp).replace(STATE_FILE)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _cooldown_remaining(state: dict) -> int:
    """Return seconds remaining in cooldown, or 0 if ready."""
    ts = state.get("last_call_timestamp")
    if not ts:
        return 0
    try:
        last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        remaining = COOLDOWN_SECONDS - elapsed
        return max(0, int(remaining))
    except Exception:
        return 0


def _check_api_error(body: str, user: str) -> str | None:
    """If the response body indicates an error, return an error message."""
    if UNAUTHORIZED_REGEX.search(body) or UNAUTHORIZED_EXACT in body:
        return (
            f"CallMeBot: user {user} is not authorized.\n"
            f"To fix: send /start to @CallMeBot_txtbot on Telegram, "
            f"or visit https://api2.callmebot.com/txt/login.php"
        )
    if FORMAT_ERROR_REGEX.search(body):
        return (
            f"CallMeBot: username format error for {user}.\n"
            f"Username must start with @ (e.g., @myuser) or be a phone number."
        )
    # Catch any other ERROR response from the API
    error_match = re.search(r"ERROR:\s*(.+?)(?:\.|<)", body)
    if error_match:
        return f"CallMeBot API error: {error_match.group(1).strip()}"
    return None


# ── KeePass helpers (same pattern as telegram_bridge.py) ─────────────────────


def keepass_get(title: str) -> str | None:
    """Retrieve a credential from KeePass by title."""
    try:
        r = subprocess.run(
            ["uv", "run", "python", str(SCRIPTS_DIR / "keepass.py"), "--json", "get", title],
            capture_output=True, text=True, cwd=str(BASE), timeout=15,
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            return data.get("password") or data.get("Password")
    except Exception as e:
        print(f"KeePass get({title!r}) failed: {e}", file=sys.stderr)
    return None


def keepass_store(title: str, value: str, group: str = "System") -> bool:
    """Store a credential in KeePass."""
    try:
        r = subprocess.run(
            ["uv", "run", "python", str(SCRIPTS_DIR / "keepass.py"), "store",
             "--title", title, "--username", "callmebot", "--password", value, "--group", group],
            capture_output=True, text=True, cwd=str(BASE), timeout=15,
        )
        return r.returncode == 0
    except Exception as e:
        print(f"KeePass store({title!r}) failed: {e}", file=sys.stderr)
    return False


# ── Subcommands ──────────────────────────────────────────────────────────────


def cmd_setup(args):
    """Configure CallMeBot (language preference + authorization check)."""
    # Verify TELEGRAM_OWNER_USERNAME exists in KeePass
    user = keepass_get(KEEPASS_TELEGRAM_OWNER_USERNAME)
    if not user:
        print(
            "TELEGRAM_OWNER_USERNAME not found in KeePass.\n"
            "Set it up via telegram_bridge first, or run:\n"
            "  uv run python scripts/keepass.py store --title TELEGRAM_OWNER_USERNAME "
            "--username agent --password @myuser --group System",
            file=sys.stderr,
        )
        return 1
    user = _normalize_username(user)

    lang = args.lang or DEFAULT_LANG
    if args.lang and not keepass_store(KEEPASS_CALLMEBOT_LANG, lang):
        print("Failed to store CallMeBot language in KeePass.", file=sys.stderr)
        return 1

    # Verify authorization by making a test call to the API
    params = urllib.parse.urlencode({
        "user": user,
        "text": "CallMeBot setup test",
        "lang": lang,
        "rpt": "1",
    })
    try:
        req = urllib.request.Request(f"{API_URL}?{params}")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        unauth_msg = _check_api_error(body, user)
        if unauth_msg:
            result = {"status": "unauthorized", "user": user, "message": unauth_msg}
            _print_result(result, args.json, unauth_msg)
            return 1
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        unauth_msg = _check_api_error(body, user)
        if unauth_msg:
            result = {"status": "unauthorized", "user": user, "message": unauth_msg}
            _print_result(result, args.json, unauth_msg)
            return 1
        # Non-auth HTTP error — warn but don't block setup
        print(f"Warning: could not verify authorization (HTTP {e.code})", file=sys.stderr)
    except Exception as e:
        print(f"Warning: could not verify authorization ({e})", file=sys.stderr)

    result = {"status": "configured", "user": user, "lang": lang}
    human = f"CallMeBot configured and authorized for {user} (voice: {lang})."
    _print_result(result, args.json, human)
    return 0


def cmd_call(args):
    """Make a voice call via CallMeBot."""
    # Get user from shared TELEGRAM_OWNER_USERNAME credential
    user = keepass_get(KEEPASS_TELEGRAM_OWNER_USERNAME)
    if not user:
        print("TELEGRAM_OWNER_USERNAME not found in KeePass. Run: callmebot.py setup", file=sys.stderr)
        return 1
    user = _normalize_username(user)

    # Check rate limit
    state = _load_state()
    remaining = _cooldown_remaining(state)
    if remaining > 0:
        result = {
            "status": "rate_limited",
            "cooldown_remaining_seconds": remaining,
            "message": f"Rate limited. Next call available in {remaining}s.",
        }
        _print_result(result, args.json, result["message"])
        return 2

    # Get language preference
    lang = keepass_get(KEEPASS_CALLMEBOT_LANG) or DEFAULT_LANG

    # Truncate and encode text
    text = args.text[:MAX_TEXT_LENGTH]
    if len(args.text) > MAX_TEXT_LENGTH:
        print(f"Warning: text truncated to {MAX_TEXT_LENGTH} chars.", file=sys.stderr)

    # Build API URL
    params = urllib.parse.urlencode({
        "user": user,
        "text": text,
        "lang": lang,
        "rpt": "2",
    })
    url = f"{API_URL}?{params}"

    # Make the call (urlopen raises HTTPError on non-2xx, so success = 2xx)
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")

        # Check for unauthorized user in response body
        unauth_msg = _check_api_error(body, user)
        if unauth_msg:
            result = {"status": "unauthorized", "user": user, "message": unauth_msg}
            _print_result(result, args.json, unauth_msg)
            return 1

        # Success — update state
        state["last_call_timestamp"] = _now_iso()
        state["total_calls"] = state.get("total_calls", 0) + 1
        _save_state(state)

        result = {
            "status": "called",
            "user": user,
            "text": text,
            "total_calls": state["total_calls"],
        }
        _print_result(result, args.json, f"Voice call sent to {user}: \"{text}\"")
        return 0

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        unauth_msg = _check_api_error(body, user)
        if unauth_msg:
            result = {"status": "unauthorized", "user": user, "message": unauth_msg}
            _print_result(result, args.json, unauth_msg)
            return 1
        print(f"CallMeBot API error: HTTP {e.code} — {e.reason}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"CallMeBot API error: {e}", file=sys.stderr)
        return 1


def cmd_status(args):
    """Check CallMeBot configuration and rate limit status."""
    user = keepass_get(KEEPASS_TELEGRAM_OWNER_USERNAME)
    configured = user is not None
    user = _normalize_username(user) if configured else None
    lang = (keepass_get(KEEPASS_CALLMEBOT_LANG) or DEFAULT_LANG) if configured else None

    state = _load_state()
    remaining = _cooldown_remaining(state) if configured else 0
    can_call = configured and remaining == 0

    result = {
        "configured": configured,
        "user": user,
        "lang": lang,
        "last_call": state.get("last_call_timestamp"),
        "can_call_now": can_call,
        "cooldown_remaining_seconds": remaining,
        "total_calls": state.get("total_calls", 0),
    }

    if args.json:
        _print_result(result, True)
    else:
        if not configured:
            print("CallMeBot: not configured (TELEGRAM_OWNER_USERNAME not found in KeePass).")
            print("  Run: callmebot.py setup")
        else:
            print(f"CallMeBot: configured for {user} (voice: {lang})")
            print(f"  Total calls: {state.get('total_calls', 0)}")
            if can_call:
                print("  Status: ready (can call now)")
            else:
                print(f"  Status: cooldown ({remaining}s remaining)")
    return 0


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Voice call escalation via CallMeBot API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # setup
    p_setup = sub.add_parser("setup", help="Verify config and set voice language")
    p_setup.add_argument("--lang", help=f"Voice language (default: {DEFAULT_LANG})")

    # call
    p_call = sub.add_parser("call", help="Make a voice call (rate-limited)")
    p_call.add_argument("--text", required=True, help="Message to speak (max 256 chars)")

    # status
    sub.add_parser("status", help="Check configuration and rate limit status")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    cmd_map = {
        "setup": cmd_setup,
        "call": cmd_call,
        "status": cmd_status,
    }

    sys.exit(cmd_map[args.command](args))


if __name__ == "__main__":
    main()