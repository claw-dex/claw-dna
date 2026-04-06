---
name: email-imap
description: IMAP email client for the agent. Fetches and verifies email via IMAP using credentials stored in KeePass (entry "Email IMAP"). Supports any IMAP provider — the server host is read from the KeePass entry's URL field. Use to verify email authentication (auth) or fetch inbox emails with filtering (fetch). Credentials are stored via the keepass skill with title "Email IMAP", username as email address, password as app/account password, and url as the IMAP server host. Example scenarios: checking for new unread emails, fetching recent inbox messages, or verifying IMAP connectivity.
---

# email-imap

**Path:** `scripts/email_imap.py`

Fetches and verifies email via IMAP protocol. Supports any IMAP provider. Credentials are retrieved from KeePass (entry titled "Email IMAP") via `keepass.py`. The IMAP host is read from the entry's URL field (defaults to `imap.gmail.com` if empty).

## Prerequisites

Store IMAP credentials in KeePass before using this script:

```bash
# Gmail (use an App Password from https://myaccount.google.com/apppasswords)
uv run python scripts/keepass.py store --title "Email IMAP" --username "you@gmail.com" --password "xxxx xxxx xxxx xxxx" --url "imap.gmail.com" --group "Email"

# Outlook / Microsoft 365
uv run python scripts/keepass.py store --title "Email IMAP" --username "you@outlook.com" --password "your-password" --url "outlook.office365.com" --group "Email"

# Custom IMAP server
uv run python scripts/keepass.py store --title "Email IMAP" --username "you@example.com" --password "your-password" --url "imap.example.com" --group "Email"
```

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `auth` | Verify IMAP credentials by attempting login |
| `fetch` | Fetch email headers (subject, from, date) |

## Flags

| Flag | Applies to | Description |
|------|-----------|-------------|
| `--mailbox NAME` | `fetch` | IMAP mailbox to search (default: `INBOX`) |
| `--filter TYPE` | `fetch` | Filter: `unseen`, `seen`, or `all` (default: `all`) |
| `--max N` | `fetch` | Max emails to return (default: `20`) |

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Error (auth failure, network, IMAP error) |
| `2` | Credentials not found in KeePass |

## Examples

```bash
# Verify IMAP credentials
uv run python scripts/email_imap.py auth
uv run python /agent/scripts/email_imap.py auth

# Fetch latest 20 emails from inbox
uv run python scripts/email_imap.py fetch
uv run python /agent/scripts/email_imap.py fetch

# Fetch unread emails only
uv run python scripts/email_imap.py fetch --filter unseen

# Fetch from Gmail's All Mail folder (Gmail only)
uv run python scripts/email_imap.py fetch --mailbox "[Gmail]/All Mail" --filter seen

# Fetch latest 5 unread emails
uv run python scripts/email_imap.py fetch --filter unseen --max 5
```
