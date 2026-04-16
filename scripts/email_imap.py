#!/usr/bin/env python3
"""
email_imap.py -- IMAP email client for the agent.

Fetch and verify email via IMAP, using credentials stored in KeePass.
Credentials are retrieved from the KeePass entry titled "Email IMAP".
The IMAP host is read from the entry's URL field (defaults to imap.gmail.com).

Usage:
    python3 email_imap.py auth [--json]
    python3 email_imap.py fetch [--mailbox INBOX] [--filter unseen|seen|all] [--max 20] [--json]
    python3 email_imap.py search --query TEXT [--field subject|from|text] [--mailbox INBOX] [--filter unseen|seen|all] [--max 20] [--json]
    python3 email_imap.py delete --uid UID [--uid UID ...] [--mailbox INBOX] [--json]
    python3 email_imap.py read --uid UID [--mailbox INBOX] [--json]

Exit codes:
    0 = success
    1 = error (auth failure, network, IMAP error)
    2 = credentials not found in KeePass
"""

import argparse
import email
import email.header
import email.utils
import html as html_module
import imaplib
import json
import re
import sys

KEEPASS_ENTRY = "Email IMAP"
IMAP_PORT = 993


# -- Helpers -------------------------------------------------------------------


def _get_credentials():
    """Retrieve IMAP credentials from KeePass (direct import).

    Returns (email, password, host) on success.
    Raises SystemExit(2) if the entry is not found.
    Raises SystemExit(1) on other errors.
    """
    try:
        from scripts.keepass import get_credential_entry

        data = get_credential_entry(KEEPASS_ENTRY)
    except Exception as exc:
        print(json.dumps({"error": f"Failed to load KeePass credentials: {exc}"}))
        sys.exit(1)

    if data is None:
        print(json.dumps({"error": f"KeePass entry '{KEEPASS_ENTRY}' not found."}))
        sys.exit(2)

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


def _connect_and_login(timeout=25):
    """Open an IMAP SSL connection and authenticate."""
    email_addr, password, host = _get_credentials()

    try:
        conn = imaplib.IMAP4_SSL(host, IMAP_PORT, timeout=timeout)
    except Exception as exc:
        _print_json({"error": f"Cannot connect to {host}:{IMAP_PORT}: {exc}"})
        return None, None

    try:
        conn.login(email_addr, password)
        return conn, email_addr
    except imaplib.IMAP4.error as exc:
        _print_json({"error": f"Authentication failed: {exc}"})
        try:
            conn.logout()
        except Exception:
            pass
        return None, None
    except Exception as exc:
        _print_json({"error": f"Connection error: {exc}"})
        try:
            conn.logout()
        except Exception:
            pass
        return None, None


def _select_mailbox(conn, mailbox, readonly):
    """Select a mailbox and return True on success."""
    status, _ = conn.select(mailbox, readonly=readonly)
    if status != "OK":
        _print_json({"error": f"Cannot select mailbox '{mailbox}'"})
        return False
    return True


def _build_search_terms(filter_name, field=None, query=None):
    """Build IMAP SEARCH terms from high-level arguments."""
    criteria_map = {"unseen": "UNSEEN", "seen": "SEEN", "all": "ALL"}
    terms = [criteria_map.get(filter_name, "ALL")]
    if query:
        search_field = {"subject": "SUBJECT", "from": "FROM", "text": "TEXT"}[field]
        terms.extend([search_field, query])
    return terms


def _search_uids(conn, filter_name="all", field=None, query=None):
    """Return message UIDs matching the requested IMAP criteria."""
    search_terms = _build_search_terms(filter_name, field=field, query=query)
    status, data = conn.uid("SEARCH", None, *search_terms)
    if status != "OK":
        _print_json({"error": "IMAP search failed"})
        return None
    return data[0].split() if data and data[0] else []


def _parse_message_headers(raw_headers):
    """Extract a compact message summary from raw RFC 822 headers."""
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

    return {
        "subject": subject or "(no subject)",
        "from": from_display or from_addr or "unknown",
        "date": date_str,
    }


def _extract_body(msg):
    """Extract text/plain and text/html body from a parsed email.Message."""
    text_body = None
    html_body = None

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                continue
            if content_type == "text/plain" and text_body is None:
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload:
                    text_body = payload.decode(charset, errors="replace")
            elif content_type == "text/html" and html_body is None:
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload:
                    html_body = payload.decode(charset, errors="replace")
    else:
        content_type = msg.get_content_type()
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode(charset, errors="replace")
            if content_type == "text/html":
                html_body = decoded
            else:
                text_body = decoded

    # Fallback: strip HTML tags to produce plain text when no text/plain part
    if text_body is None and html_body is not None:
        unescaped = html_module.unescape(html_body)
        no_scripts = re.sub(
            r"<(script|style)[^>]*>.*?</(script|style)>",
            "",
            unescaped,
            flags=re.DOTALL | re.IGNORECASE,
        )
        stripped = re.sub(r"<[^>]+>", "", no_scripts)
        text_body = re.sub(r"\n{3,}", "\n\n", stripped).strip()

    return text_body or "", html_body or ""


def _fetch_messages(conn, uids, max_results):
    """Fetch message headers for the newest UIDs."""
    if not uids:
        return []

    selected_uids = uids[-max_results:][::-1]
    messages = []
    for uid in selected_uids:
        # Use BODY.PEEK so header fetches do not set the \Seen flag.
        status, msg_data = conn.uid(
            "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
        )
        if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
            continue

        message = _parse_message_headers(msg_data[0][1])
        message["uid"] = uid.decode() if isinstance(uid, bytes) else str(uid)
        messages.append(message)

    return messages


# -- Subcommands ---------------------------------------------------------------


def cmd_auth(args):
    """Verify IMAP credentials by attempting login."""
    conn, email_addr = _connect_and_login(timeout=15)
    if not conn:
        return 1

    try:
        _print_json({"status": "ok", "email": email_addr})
        return 0
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def cmd_fetch(args):
    """Fetch email headers from IMAP."""
    conn, _ = _connect_and_login(timeout=25)
    if not conn:
        return 1

    try:
        if not _select_mailbox(conn, args.mailbox, readonly=True):
            return 1

        uids = _search_uids(conn, filter_name=args.filter)
        if uids is None:
            return 1

        messages = _fetch_messages(conn, uids, args.max)
        if not messages:
            _print_json({"messages": [], "count": 0})
            return 0

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


def cmd_read(args):
    """Fetch the full body of a single email by UID."""
    conn, _ = _connect_and_login(timeout=25)
    if not conn:
        return 1

    try:
        if not _select_mailbox(conn, args.mailbox, readonly=True):
            return 1

        uid_str = str(args.uid).strip()
        status, msg_data = conn.uid("FETCH", uid_str, "(BODY.PEEK[])")
        if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
            _print_json({"error": f"Could not fetch message UID {uid_str}"})
            return 1

        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        headers = _parse_message_headers(raw)
        text_body, html_body = _extract_body(msg)

        _print_json(
            {
                "uid": uid_str,
                **headers,
                "body_text": text_body[:15000],
                "has_html": bool(html_body),
            }
        )
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


def cmd_search(args):
    """Search for email headers using IMAP SEARCH criteria."""
    conn, _ = _connect_and_login(timeout=25)
    if not conn:
        return 1

    try:
        if not _select_mailbox(conn, args.mailbox, readonly=True):
            return 1

        uids = _search_uids(
            conn, filter_name=args.filter, field=args.field, query=args.query
        )
        if uids is None:
            return 1

        messages = _fetch_messages(conn, uids, args.max)
        _print_json(
            {
                "messages": messages,
                "count": len(messages),
                "query": args.query,
                "field": args.field,
            }
        )
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


def cmd_delete(args):
    """Delete specific messages by UID from the selected mailbox."""
    conn, _ = _connect_and_login(timeout=25)
    if not conn:
        return 1

    deleted = []
    errors = []

    try:
        if not _select_mailbox(conn, args.mailbox, readonly=False):
            return 1

        for uid in args.uid:
            uid_str = str(uid).strip()
            if not uid_str:
                continue

            status, _ = conn.uid("STORE", uid_str, "+FLAGS.SILENT", r"(\Deleted)")
            if status == "OK":
                deleted.append(uid_str)
            else:
                errors.append({"uid": uid_str, "error": "Failed to mark for deletion"})

        if deleted:
            expunge_status, _ = conn.expunge()
            if expunge_status != "OK":
                _print_json(
                    {
                        "error": "Failed to expunge deleted messages",
                        "deleted_uids": deleted,
                    }
                )
                return 1

        _print_json(
            {
                "deleted_uids": deleted,
                "deleted_count": len(deleted),
                "errors": errors,
            }
        )
        return 0 if not errors else 1

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
    parser.add_argument(
        "--json", action="store_true", default=True, help="Output as JSON (always on)"
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # auth
    sub.add_parser("auth", help="Verify IMAP credentials")

    # fetch
    p_fetch = sub.add_parser("fetch", help="Fetch emails")
    p_fetch.add_argument(
        "--mailbox", default="INBOX", help="IMAP mailbox (default: INBOX)"
    )
    p_fetch.add_argument(
        "--filter",
        default="all",
        choices=["unseen", "seen", "all"],
        help="Email filter (default: all)",
    )
    p_fetch.add_argument(
        "--max", type=int, default=20, help="Max emails to fetch (default: 20)"
    )

    # search
    p_search = sub.add_parser("search", help="Search emails")
    p_search.add_argument("--query", required=True, help="Search query text")
    p_search.add_argument(
        "--field",
        default="text",
        choices=["subject", "from", "text"],
        help="Field to search (default: text)",
    )
    p_search.add_argument(
        "--mailbox", default="INBOX", help="IMAP mailbox (default: INBOX)"
    )
    p_search.add_argument(
        "--filter",
        default="all",
        choices=["unseen", "seen", "all"],
        help="Email filter (default: all)",
    )
    p_search.add_argument(
        "--max", type=int, default=20, help="Max emails to fetch (default: 20)"
    )

    # delete
    p_delete = sub.add_parser("delete", help="Delete emails by UID")
    p_delete.add_argument(
        "--mailbox", default="INBOX", help="IMAP mailbox (default: INBOX)"
    )
    p_delete.add_argument(
        "--uid", required=True, nargs="+", help="One or more IMAP UIDs to delete"
    )

    # read
    p_read = sub.add_parser("read", help="Read full email body by UID")
    p_read.add_argument(
        "--mailbox", default="INBOX", help="IMAP mailbox (default: INBOX)"
    )
    p_read.add_argument("--uid", required=True, help="IMAP UID of the message to read")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    cmd_map = {
        "auth": cmd_auth,
        "fetch": cmd_fetch,
        "search": cmd_search,
        "delete": cmd_delete,
        "read": cmd_read,
    }

    return cmd_map[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
