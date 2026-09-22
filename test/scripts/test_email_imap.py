"""Tests for scripts/email_imap.py."""

from __future__ import annotations

import argparse
import email.message
import imaplib
import json
import sys

import pytest

import email_imap


def test_quote_mailbox_simple():
    assert email_imap._quote_mailbox("INBOX") == "INBOX"


def test_quote_mailbox_with_space():
    assert email_imap._quote_mailbox("My Folder") == '"My Folder"'


def test_quote_mailbox_escaping():
    out = email_imap._quote_mailbox('weird"name')
    assert out.startswith('"') and out.endswith('"')
    assert '\\"' in out


def test_build_search_terms_all():
    assert email_imap._build_search_terms("all") == ["ALL"]


def test_build_search_terms_unseen():
    assert email_imap._build_search_terms("unseen") == ["UNSEEN"]


def test_build_search_terms_with_query():
    assert email_imap._build_search_terms("seen", field="subject", query="hi") == [
        "SEEN",
        "SUBJECT",
        "hi",
    ]


def test_decode_header_value_plain():
    assert email_imap._decode_header_value("Hello") == "Hello"


def test_decode_header_value_empty():
    assert email_imap._decode_header_value("") == ""


def test_decode_header_value_encoded():
    # RFC 2047 encoded
    encoded = "=?utf-8?B?SGVsbG8=?="  # "Hello"
    assert "Hello" in email_imap._decode_header_value(encoded)


def test_parse_message_headers_basic():
    raw = (
        b"Subject: Hi there\r\n"
        b"From: Alice <alice@example.com>\r\n"
        b"Date: Mon, 1 Jan 2024 12:00:00 +0000\r\n"
        b"\r\n"
    )
    parsed = email_imap._parse_message_headers(raw)
    assert parsed["subject"] == "Hi there"
    assert "Alice" in parsed["from"]
    assert "2024" in parsed["date"]


def test_parse_message_headers_no_subject():
    raw = b"From: x@y.com\r\n\r\n"
    parsed = email_imap._parse_message_headers(raw)
    assert parsed["subject"] == "(no subject)"


def test_extract_body_plain_text():
    msg = email.message.EmailMessage()
    msg.set_content("Hello world")
    text, html = email_imap._extract_body(msg)
    assert "Hello world" in text
    assert html == ""


def test_extract_body_html_only():
    msg = email.message.EmailMessage()
    msg.set_content("<p>Hi <b>there</b></p>", subtype="html")
    text, html = email_imap._extract_body(msg)
    # HTML is detected
    assert "<p>" in html or "Hi" in html
    # text fallback strips tags
    assert "Hi" in text
    assert "<p>" not in text


def _install_fake_keepass(monkeypatch, get_entry_fn):
    """Install a stub `scripts.keepass` so email_imap's deferred import works."""
    import types

    fake = types.ModuleType("scripts.keepass")
    fake.get_credential_entry = get_entry_fn
    monkeypatch.setitem(sys.modules, "scripts.keepass", fake)


def test_get_credentials_missing(monkeypatch, capsys):
    _install_fake_keepass(monkeypatch, lambda title: None)
    with pytest.raises(SystemExit) as exc:
        email_imap._get_credentials()
    assert exc.value.code == 2


def test_get_credentials_incomplete(monkeypatch):
    _install_fake_keepass(
        monkeypatch, lambda title: {"username": "", "password": "", "url": ""}
    )
    with pytest.raises(SystemExit) as exc:
        email_imap._get_credentials()
    assert exc.value.code == 1


def test_get_credentials_success(monkeypatch):
    _install_fake_keepass(
        monkeypatch,
        lambda title: {
            "username": "u@x.com",
            "password": "pw",
            "url": "imaps://imap.example.com/",
        },
    )
    e, p, h = email_imap._get_credentials()
    assert e == "u@x.com"
    assert p == "pw"
    assert h == "imap.example.com"


def test_get_credentials_default_host(monkeypatch):
    _install_fake_keepass(
        monkeypatch,
        lambda title: {"username": "u@x.com", "password": "pw", "url": ""},
    )
    _, _, h = email_imap._get_credentials()
    assert h == "imap.gmail.com"


# ─── Subcommands ────────────────────────────────────────────────────────────


class FakeIMAP:
    def __init__(self):
        self.logged_out = False
        self.selected = None
        self.search_data = [b"1 2 3"]
        self.fetch_returns = {}

    def login(self, u, p):
        return ("OK", [b"ok"])

    def logout(self):
        self.logged_out = True

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return ("OK", self.search_data)
        if cmd == "FETCH":
            uid = args[0]
            data = self.fetch_returns.get(uid)
            if data:
                return ("OK", [(b"x", data)])
            return ("OK", [None])
        if cmd == "STORE":
            return ("OK", [b""])
        return ("NO", [b""])

    def expunge(self):
        return ("OK", [b""])


def _patch_imap(monkeypatch, fake):
    monkeypatch.setattr(
        email_imap,
        "_get_credentials",
        lambda: ("u@x.com", "pw", "imap.example.com"),
    )
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host, port, timeout=25: fake)


def test_cmd_auth_success(monkeypatch, capsys):
    fake = FakeIMAP()
    _patch_imap(monkeypatch, fake)
    rc = email_imap.cmd_auth(argparse.Namespace())
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "ok"
    assert fake.logged_out


def test_cmd_auth_login_fails(monkeypatch, capsys):
    fake = FakeIMAP()

    def bad_login(u, p):
        raise imaplib.IMAP4.error("bad creds")

    fake.login = bad_login
    _patch_imap(monkeypatch, fake)
    rc = email_imap.cmd_auth(argparse.Namespace())
    assert rc == 1


def test_cmd_fetch_no_messages(monkeypatch, capsys):
    fake = FakeIMAP()
    fake.search_data = [b""]
    _patch_imap(monkeypatch, fake)
    args = argparse.Namespace(mailbox="INBOX", filter="all", max=20)
    rc = email_imap.cmd_fetch(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 0


def test_cmd_fetch_with_messages(monkeypatch, capsys):
    fake = FakeIMAP()
    fake.search_data = [b"1"]
    fake.fetch_returns = {
        b"1": b"Subject: hi\r\nFrom: a@b.com\r\nDate: Mon, 1 Jan 2024 12:00:00 +0000\r\n\r\n"
    }
    _patch_imap(monkeypatch, fake)
    args = argparse.Namespace(mailbox="INBOX", filter="all", max=20)
    rc = email_imap.cmd_fetch(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 1
    assert data["messages"][0]["subject"] == "hi"


def test_cmd_delete(monkeypatch, capsys):
    fake = FakeIMAP()
    _patch_imap(monkeypatch, fake)
    args = argparse.Namespace(mailbox="INBOX", uid=["5", "6"])
    rc = email_imap.cmd_delete(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["deleted_count"] == 2
    assert "5" in data["deleted_uids"]


def test_cmd_search_with_query(monkeypatch, capsys):
    fake = FakeIMAP()
    fake.search_data = [b""]
    _patch_imap(monkeypatch, fake)
    args = argparse.Namespace(
        mailbox="INBOX", filter="all", max=10, field="subject", query="urgent"
    )
    rc = email_imap.cmd_search(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["query"] == "urgent"
    assert data["field"] == "subject"


def test_cmd_select_mailbox_failure(monkeypatch, capsys):
    fake = FakeIMAP()

    def bad_select(mb, readonly=False):
        return ("NO", [b""])

    fake.select = bad_select
    _patch_imap(monkeypatch, fake)
    args = argparse.Namespace(mailbox="Junk", filter="all", max=5)
    rc = email_imap.cmd_fetch(args)
    assert rc == 1
