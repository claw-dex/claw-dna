#!/usr/bin/env python3
"""
email_imap.py -- IMAP email client for the agent.

Fetch and verify email via IMAP, using credentials stored in KeePass.
Credentials are retrieved from the KeePass entry titled "Email IMAP".
The IMAP host is read from the entry's URL field (defaults to imap.gmail.com).

Usage:
    python3 email_imap.py auth [--json]
    python3 email_imap.py fetch [--mailbox INBOX] [--filter unseen|seen|all] [--max 20] [--json]

Exit codes:
    0 = success
    1 = error (auth failure, network, IMAP error)
    2 = credentials not found in KeePass
"""

import argparse
import email
import email.header
import email.utils
import imaplib
import json
import os
import subprocess
import sys

KEEPASS_ENTRY = "Email IMAP"
IMAP_PORT = 993
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
KEEPASS_SCRIPT = os.path.join(SCRIPTS_DIR, "keepass.py")


# -- Helpers -------------------------------------------------------------------


def _get_credentials():
    """Retrieve IMAP credentials from KeePass.

    Returns (email, password, host) on success.
    Raises SystemExit(2) if the entry is not found.
    Raises SystemExit(1) on other errors.
    """
    try:
        result = subprocess.run(
            ["uv", "run", "python", KEEPASS_SCRIPT, "--json", "get", KEEPASS_ENTRY],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        print(json.dumps({"error": f"Failed to call keepass.py: {exc}"}))
        sys.exit(1)

    if result.returncode == 2:
        print(json.dumps({"error": f"KeePass entry '{KEEPASS_ENTRY}' not found."}))
        sys.exit(2)
    if result.returncode != 0:
        print(json.dumps({"error": f"keepass.py error: {result.stderr.strip()}"}))
        sys.exit(1)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(json.dumps({"error": "Invalid JSON from keepass.py"}))
        sys.exit(1)

    email_addr = data.get("username", "")
    password = data.get("password", "")
    raw_url = data.get("url", "") or "imap.gmail.com"
    if "://" in raw_url:
        raw_url = raw_url.split("://", 1)[1]
    host = raw_url.rstrip("/")

    if not email_addr or not password:
        print(json.dumps({"error": "KeePass entry missing username or password."}))
        sys.exit(1)

    return email_addr, password, host


def _decode_header_value(raw):
    """Decode an RFC 2047 encoded header into a plain string."""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for content, charset in parts:
        if isinstance(content, bytes):
            decoded.append(content.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(content)
    return " ".join(decoded)


def _print_json(data):
    print(json.dumps(data, indent=2, default=str))


# -- Subcommands ---------------------------------------------------------------


def cmd_auth(args):
    """Verify IMAP credentials by attempting login."""
    email_addr, password, host = _get_credentials()

    try:
        conn = imaplib.IMAP4_SSL(host, IMAP_PORT, timeout=15)
    except Exception as exc:
        _print_json({"error": f"Cannot connect to {host}:{IMAP_PORT}: {exc}"})
        return 1

    try:
        conn.login(email_addr, password)
        _print_json({"status": "ok", "email": email_addr})
        return 0
    except imaplib.IMAP4.error as exc:
        _print_json({"error": f"Authentication failed: {exc}"})
        return 1
    except Exception as exc:
        _print_json({"error": f"Connection error: {exc}"})
        return 1
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def cmd_fetch(args):
    """Fetch email headers from IMAP."""
    email_addr, password, host = _get_credentials()

    mailbox = args.mailbox
    max_results = args.max
    criteria_map = {"unseen": "UNSEEN", "seen": "SEEN", "all": "ALL"}
    search_criteria = criteria_map.get(args.filter, "ALL")

    try:
        conn = imaplib.IMAP4_SSL(host, IMAP_PORT, timeout=25)
    except Exception as exc:
        _print_json({"error": f"Cannot connect to {host}:{IMAP_PORT}: {exc}"})
        return 1

    try:
        conn.login(email_addr, password)
        status, _ = conn.select(mailbox, readonly=True)
        if status != "OK":
            _print_json({"error": f"Cannot select mailbox '{mailbox}'"})
            return 1

        status, data = conn.search(None, search_criteria)
        if status != "OK":
            _print_json({"error": "IMAP search failed"})
            return 1

        msg_ids = data[0].split()
        if not msg_ids:
            _print_json({"messages": [], "count": 0})
            return 0

        # Take the last N (most recent) and reverse for newest-first order
        msg_ids = msg_ids[-max_results:][::-1]

        messages = []
        for mid in msg_ids:
            status, msg_data = conn.fetch(
                mid, "(BODY[HEADER.FIELDS (SUBJECT FROM DATE)])"
            )
            if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue

            raw_headers = msg_data[0][1]
            msg = email.message_from_bytes(raw_headers)

            subject = _decode_header_value(msg.get("Subject", ""))

            from_raw = msg.get("From", "")
            from_name, from_addr = email.utils.parseaddr(from_raw)
            from_display = _decode_header_value(from_name) if from_name else from_addr

            date_str = msg.get("Date", "")
            try:
                dt = email.utils.parsedate_to_datetime(date_str)
                date_str = dt.isoformat()
            except Exception:
                pass  # keep raw string

            messages.append({
                "subject": subject or "(no subject)",
                "from": from_display or from_addr or "unknown",
                "date": date_str,
            })

        _print_json({"messages": messages, "count": len(messages)})
        return 0

    except imaplib.IMAP4.error as exc:
        _print_json({"error": f"IMAP error: {exc}"})
        return 1
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# -- Main ----------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="IMAP email client for the agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", default=True,
                        help="Output as JSON (always on)")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # auth
    sub.add_parser("auth", help="Verify IMAP credentials")

    # fetch
    p_fetch = sub.add_parser("fetch", help="Fetch emails")
    p_fetch.add_argument("--mailbox", default="INBOX",
                         help="IMAP mailbox (default: INBOX)")
    p_fetch.add_argument("--filter", default="all",
                         choices=["unseen", "seen", "all"],
                         help="Email filter (default: all)")
    p_fetch.add_argument("--max", type=int, default=20,
                         help="Max emails to fetch (default: 20)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    cmd_map = {
        "auth": cmd_auth,
        "fetch": cmd_fetch,
    }

    return cmd_map[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
