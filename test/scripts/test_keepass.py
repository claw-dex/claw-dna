"""Tests for scripts/keepass.py."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

import keepass


@pytest.fixture(autouse=True)
def clear_cache():
    keepass.clear_credential_cache()
    yield
    keepass.clear_credential_cache()


@pytest.fixture
def redirect_db(monkeypatch, tmp_path):
    db = tmp_path / "credentials.kdbx"
    monkeypatch.setattr(keepass, "KEEPASS_DIR", tmp_path)
    monkeypatch.setattr(keepass, "DB_PATH", db)
    return db


# ─── Stubs to replace pykeepass entries / DB ────────────────────────────────


class FakeGroup:
    def __init__(self, name="System", path=None):
        self.name = name
        self.path = path if path is not None else [name]
        self.entries = []


class FakeEntry:
    def __init__(
        self,
        title="t",
        username="u",
        password="p",
        url="",
        notes="",
        group=None,
        tags=None,
    ):
        self.title = title
        self.username = username
        self.password = password
        self.url = url
        self.notes = notes
        self.tags = tags or []
        self.group = group or FakeGroup()
        self.ctime = datetime(2024, 1, 1)
        self.mtime = datetime(2024, 1, 2)


class FakeKP:
    def __init__(self):
        self.root_group = FakeGroup("root", path=[])
        self.entries = []
        self.groups = [self.root_group]
        self.saved = False

    def find_entries(self, title=None, group=None, first=False):
        results = [
            e
            for e in self.entries
            if (title is None or e.title == title)
            and (group is None or e.group is group)
        ]
        if first:
            return results[0] if results else None
        return results

    def find_groups(self, name=None, group=None, recursive=True, first=False):
        results = [g for g in self.groups if (name is None or g.name == name)]
        if first:
            return results[0] if results else None
        return results

    def add_entry(self, group, title, username, password, url="", notes=""):
        e = FakeEntry(title, username, password, url, notes, group=group)
        self.entries.append(e)
        group.entries.append(e)
        return e

    def add_group(self, parent, name):
        g = FakeGroup(name, path=parent.path + [name])
        self.groups.append(g)
        return g

    def delete_entry(self, entry):
        self.entries.remove(entry)

    def move_entry(self, entry, group):
        entry.group = group

    def save(self):
        self.saved = True


@pytest.fixture
def fake_kp(monkeypatch, redirect_db):
    redirect_db.write_bytes(b"stub")  # exists check passes
    kp = FakeKP()
    monkeypatch.setattr(keepass, "_open_db", lambda: (kp, None))
    return kp


# ─── Helpers ────────────────────────────────────────────────────────────────


def test_open_db_missing(redirect_db):
    kp, err = keepass._open_db()
    assert kp is None
    assert "not found" in err


def test_entry_to_dict_basic():
    e = FakeEntry(
        title="x", username="u", password="secret", url="https://e", notes="n"
    )
    d = keepass._entry_to_dict(e, include_password=True)
    assert d["title"] == "x"
    assert d["password"] == "secret"
    d2 = keepass._entry_to_dict(e, include_password=False)
    assert "password" not in d2


def test_find_or_create_group_creates_path(fake_kp):
    g = keepass._find_or_create_group(fake_kp, "Services/AWS")
    assert g.name == "AWS"


def test_find_or_create_group_finds_existing(fake_kp):
    fake_kp.groups.append(FakeGroup("System"))
    g = keepass._find_or_create_group(fake_kp, "System")
    assert g.name == "System"


# ─── Public API ─────────────────────────────────────────────────────────────


def test_get_credential_returns_password(fake_kp):
    fake_kp.entries.append(FakeEntry(title="API_KEY", password="s3cret"))
    assert keepass.get_credential("API_KEY") == "s3cret"


def test_get_credential_missing(fake_kp):
    assert keepass.get_credential("nope") is None


def test_get_credential_caches(fake_kp, monkeypatch):
    fake_kp.entries.append(FakeEntry(title="K", password="v1"))
    assert keepass.get_credential("K") == "v1"
    # Now break _open_db; cached value should still be returned
    monkeypatch.setattr(keepass, "_open_db", lambda: (None, "err"))
    assert keepass.get_credential("K") == "v1"


def test_get_credential_entry_returns_dict(fake_kp):
    fake_kp.entries.append(FakeEntry(title="K", username="u", password="v"))
    d = keepass.get_credential_entry("K")
    assert d["password"] == "v"
    assert d["username"] == "u"


def test_store_credential_creates(fake_kp):
    ok = keepass.store_credential("New", "u", "p", group="System")
    assert ok is True
    assert any(e.title == "New" for e in fake_kp.entries)
    assert fake_kp.saved


def test_store_credential_updates_existing(fake_kp):
    fake_kp.entries.append(FakeEntry(title="K", username="old", password="old"))
    ok = keepass.store_credential("K", "newu", "newp")
    assert ok
    e = next(e for e in fake_kp.entries if e.title == "K")
    assert e.username == "newu"
    assert e.password == "newp"


# ─── Subcommands ────────────────────────────────────────────────────────────


def test_cmd_init_already_exists(redirect_db, capsys):
    redirect_db.write_bytes(b"stub")
    args = argparse.Namespace(json=True)
    rc = keepass.cmd_init(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "exists"


def test_cmd_init_creates(monkeypatch, redirect_db, capsys):
    # DB doesn't exist
    fake_kp = FakeKP()

    monkeypatch.setitem(
        sys.modules,
        "pykeepass",
        SimpleNamespace(create_database=lambda path, password="": fake_kp),
    )
    args = argparse.Namespace(json=True)
    rc = keepass.cmd_init(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "created"


def test_cmd_list_empty(fake_kp, capsys):
    args = argparse.Namespace(group=None, json=True)
    rc = keepass.cmd_list(args)
    assert rc == 2
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 0


def test_cmd_list_entries(fake_kp, capsys):
    fake_kp.entries.append(FakeEntry(title="A"))
    fake_kp.entries.append(FakeEntry(title="B"))
    args = argparse.Namespace(group=None, json=True)
    rc = keepass.cmd_list(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 2


def test_cmd_get_not_found(fake_kp, capsys):
    args = argparse.Namespace(title="missing", json=True)
    rc = keepass.cmd_get(args)
    assert rc == 2


def test_cmd_get_found(fake_kp, capsys):
    fake_kp.entries.append(FakeEntry(title="K", password="p"))
    args = argparse.Namespace(title="K", json=True)
    rc = keepass.cmd_get(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["password"] == "p"


def test_cmd_store_creates(fake_kp, capsys):
    args = argparse.Namespace(
        title="New",
        username="u",
        password="p",
        url=None,
        notes=None,
        group="System",
        json=True,
    )
    rc = keepass.cmd_store(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "created"


def test_cmd_delete_not_found(fake_kp, capsys):
    args = argparse.Namespace(title="missing", json=True)
    rc = keepass.cmd_delete(args)
    assert rc == 2


def test_cmd_delete_success(fake_kp, capsys):
    fake_kp.entries.append(FakeEntry(title="K"))
    args = argparse.Namespace(title="K", json=True)
    rc = keepass.cmd_delete(args)
    assert rc == 0
    assert all(e.title != "K" for e in fake_kp.entries)


def test_cmd_groups_lists(fake_kp, capsys):
    fake_kp.groups.append(FakeGroup("G1"))
    args = argparse.Namespace(json=True)
    rc = keepass.cmd_groups(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] >= 1


def test_cmd_search_match(fake_kp, capsys):
    fake_kp.entries.append(FakeEntry(title="Github_PAT"))
    args = argparse.Namespace(query="github", json=True)
    rc = keepass.cmd_search(args)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 1


def test_cmd_search_no_match(fake_kp, capsys):
    args = argparse.Namespace(query="zzz", json=True)
    rc = keepass.cmd_search(args)
    assert rc == 2


def test_cmd_search_invalid_regex(fake_kp, capsys):
    args = argparse.Namespace(query="(", json=True)
    rc = keepass.cmd_search(args)
    assert rc == 1
