"""Tests for services/scheduler_daemon.py — config + helpers (not the loop)."""

from __future__ import annotations

import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# _resolve_poll_interval
# ---------------------------------------------------------------------------


def test_resolve_poll_interval_default_when_unset(
    monkeypatch, patch_scheduler_daemon_paths
):
    monkeypatch.delenv("SCHEDULER_DAEMON_POLL_SECONDS", raising=False)
    assert (
        patch_scheduler_daemon_paths._resolve_poll_interval()
        == patch_scheduler_daemon_paths.DEFAULT_POLL_INTERVAL
    )


def test_resolve_poll_interval_default_when_empty(
    monkeypatch, patch_scheduler_daemon_paths
):
    monkeypatch.setenv("SCHEDULER_DAEMON_POLL_SECONDS", "")
    assert (
        patch_scheduler_daemon_paths._resolve_poll_interval()
        == patch_scheduler_daemon_paths.DEFAULT_POLL_INTERVAL
    )


def test_resolve_poll_interval_falls_back_on_garbage(
    monkeypatch, patch_scheduler_daemon_paths
):
    monkeypatch.setenv("SCHEDULER_DAEMON_POLL_SECONDS", "fast")
    assert (
        patch_scheduler_daemon_paths._resolve_poll_interval()
        == patch_scheduler_daemon_paths.DEFAULT_POLL_INTERVAL
    )


def test_resolve_poll_interval_clamps_low_values(
    monkeypatch, patch_scheduler_daemon_paths
):
    monkeypatch.setenv("SCHEDULER_DAEMON_POLL_SECONDS", "0")
    assert (
        patch_scheduler_daemon_paths._resolve_poll_interval()
        == patch_scheduler_daemon_paths.MIN_POLL_INTERVAL
    )
    monkeypatch.setenv("SCHEDULER_DAEMON_POLL_SECONDS", "-50")
    assert (
        patch_scheduler_daemon_paths._resolve_poll_interval()
        == patch_scheduler_daemon_paths.MIN_POLL_INTERVAL
    )


def test_resolve_poll_interval_accepts_valid_value(
    monkeypatch, patch_scheduler_daemon_paths
):
    monkeypatch.setenv("SCHEDULER_DAEMON_POLL_SECONDS", "120")
    assert patch_scheduler_daemon_paths._resolve_poll_interval() == 120


# ---------------------------------------------------------------------------
# _write_heartbeat
# ---------------------------------------------------------------------------


def test_write_heartbeat_writes_iso_timestamp(patch_scheduler_daemon_paths):
    patch_scheduler_daemon_paths.HEARTBEAT_FILE.parent.mkdir(
        parents=True, exist_ok=True
    )
    patch_scheduler_daemon_paths._write_heartbeat()
    text = patch_scheduler_daemon_paths.HEARTBEAT_FILE.read_text().strip()
    # Parses as ISO timestamp.
    datetime.fromisoformat(text)


def test_write_heartbeat_swallows_oserror(
    monkeypatch, patch_scheduler_daemon_paths, tmp_path
):
    # Point heartbeat at a path inside a non-existent dir; without the dir
    # being created first this would raise FileNotFoundError.
    bad = tmp_path / "no" / "such" / "dir" / "hb"
    monkeypatch.setattr(patch_scheduler_daemon_paths, "HEARTBEAT_FILE", bad)
    # No raise expected.
    patch_scheduler_daemon_paths._write_heartbeat()
    assert not bad.exists()


# ---------------------------------------------------------------------------
# _interruptible_sleep
# ---------------------------------------------------------------------------


class _FakeTime:
    """Standalone fake time module — substituted for ``scheduler_daemon.time``
    so we never patch the real :mod:`time` (pytest itself relies on it)."""

    def __init__(self):
        self._now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self._now

    def sleep(self, s):
        self.sleeps.append(s)
        self._now += s


def test_interruptible_sleep_returns_when_running_flips_false(
    monkeypatch, patch_scheduler_daemon_paths
):
    sd = patch_scheduler_daemon_paths
    fake = _FakeTime()
    monkeypatch.setattr(sd, "time", fake)

    ticks = {"n": 0}

    def is_running():
        ticks["n"] += 1
        return ticks["n"] <= 3

    sd._interruptible_sleep(30, is_running)

    # Should sleep in 1-second slices, then exit early when is_running flips.
    assert all(s == pytest.approx(1.0) for s in fake.sleeps)
    assert len(fake.sleeps) == 3


def test_interruptible_sleep_returns_immediately_when_not_running(
    monkeypatch, patch_scheduler_daemon_paths
):
    sd = patch_scheduler_daemon_paths
    fake = _FakeTime()
    monkeypatch.setattr(sd, "time", fake)

    sd._interruptible_sleep(60.0, lambda: False)
    assert fake.sleeps == []  # never entered the loop body


def test_interruptible_sleep_clamps_final_slice_to_remaining(
    monkeypatch, patch_scheduler_daemon_paths
):
    sd = patch_scheduler_daemon_paths
    fake = _FakeTime()
    monkeypatch.setattr(sd, "time", fake)

    sd._interruptible_sleep(2.5, lambda: True)
    # Two full 1.0 slices, then a 0.5 final slice.
    assert fake.sleeps == [pytest.approx(1.0), pytest.approx(1.0), pytest.approx(0.5)]


# ---------------------------------------------------------------------------
# _import_scheduler
# ---------------------------------------------------------------------------


def test_import_scheduler_loads_module(
    monkeypatch, tmp_path, patch_scheduler_daemon_paths
):
    # Drop a stub scheduler module that exposes check_and_inject(),
    # and put it on sys.path before _import_scheduler runs.
    stub_dir = tmp_path / "stub_scripts"
    stub_dir.mkdir()
    (stub_dir / "scheduler.py").write_text(textwrap.dedent("""
            def check_and_inject():
                return 0
            """))
    monkeypatch.syspath_prepend(str(stub_dir))
    # Drop any cached scheduler module so the import picks up our stub.
    sys.modules.pop("scheduler", None)

    mod = patch_scheduler_daemon_paths._import_scheduler()
    assert callable(mod.check_and_inject)
    assert mod.check_and_inject() == 0
