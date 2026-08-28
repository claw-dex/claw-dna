"""Tests for scripts/metrics_collector.py."""

from __future__ import annotations

import json

import pytest

import metrics_collector as mc


@pytest.fixture
def patched(monkeypatch, agent_root):
    metrics_file = agent_root / "memory" / "metrics.json"
    monkeypatch.setattr(mc, "MEMORY_DIR", agent_root / "memory")
    monkeypatch.setattr(mc, "METRICS_FILE", metrics_file)
    return metrics_file


def test_load_metrics_missing(patched):
    assert mc._load_metrics() == []


def test_load_metrics_invalid_json(patched):
    patched.write_text("{not json")
    assert mc._load_metrics() == []


def test_load_metrics_not_list(patched):
    patched.write_text(json.dumps({"x": 1}))
    assert mc._load_metrics() == []


def test_load_metrics_valid(patched):
    patched.write_text(json.dumps([{"ts": "x"}]))
    assert mc._load_metrics() == [{"ts": "x"}]


def test_get_recent_limit(patched):
    patched.write_text(json.dumps([{"i": i} for i in range(20)]))
    out = mc.get_recent(5)
    assert len(out) == 5
    assert out[0]["i"] == 15


def test_avg_basic():
    assert mc._avg([1, 2, 3]) == 2.0
    assert mc._avg([]) is None
    assert mc._avg([1, "x", 3]) == 2.0
    assert mc._avg([None, None]) is None


def test_probe_endpoint_failure(monkeypatch):
    def fake_open(*a, **kw):
        raise OSError("network down")

    monkeypatch.setattr(mc.urllib.request, "urlopen", fake_open)
    result = mc._probe_endpoint("/_stcore/health")
    assert result["ok"] is False
    assert result["status"] == 0


def test_probe_endpoint_success(monkeypatch):
    class FakeResp:
        status = 200

        def read(self):
            return b"ok"

    def fake_open(url, timeout=5):
        return FakeResp()

    monkeypatch.setattr(mc.urllib.request, "urlopen", fake_open)
    result = mc._probe_endpoint("/_stcore/health")
    assert result["ok"] is True
    assert result["status"] == 200


def test_collect_snapshot_writes_ring(monkeypatch, patched):
    monkeypatch.setattr(
        mc, "_probe_endpoint", lambda p: {"ms": 5.0, "status": 200, "ok": True}
    )
    monkeypatch.setattr(mc, "_read_sys_resource", lambda k: 100.0)

    snap = mc.collect_snapshot(max_entries=3)
    assert "ts" in snap
    assert snap["api"]["_stcore_health"]["ok"] is True

    # Add 4 more — should ring-buffer to 3
    for _ in range(4):
        mc.collect_snapshot(max_entries=3)
    data = json.loads(patched.read_text())
    assert len(data) == 3


def test_check_regressions_too_few(patched):
    patched.write_text(json.dumps([{"api": {}, "sys": {}}]))
    assert mc.check_regressions() == []


def test_check_regressions_warns(patched):
    # baseline 9 fast snapshots, latest is slow
    entries = []
    for _ in range(9):
        entries.append({"api": {"_stcore_health": {"ms": 10}}, "sys": {}})
    entries.append({"api": {"_stcore_health": {"ms": 200}}, "sys": {}})
    patched.write_text(json.dumps(entries))
    warns = mc.check_regressions()
    assert any("_stcore_health" in w for w in warns)


def test_report_table_no_data(capsys, patched):
    mc.report_table(10)
    out = capsys.readouterr().out
    assert "No metrics" in out


def test_report_summary_no_data(patched):
    assert "No metrics" in mc.report_summary(10)


def test_report_summary_with_data(patched):
    patched.write_text(
        json.dumps(
            [
                {
                    "api": {"_stcore_health": {"ms": 10}},
                    "sys": {"load_1m": 0.5, "mem_used_mb": 100, "mem_total_mb": 1000},
                },
                {
                    "api": {"_stcore_health": {"ms": 20}},
                    "sys": {"load_1m": 0.7, "mem_used_mb": 200, "mem_total_mb": 1000},
                },
            ]
        )
    )
    out = mc.report_summary(10)
    assert "n=2" in out
    assert "portal=" in out


def test_main_summary_mode(monkeypatch, capsys, patched):
    patched.write_text(json.dumps([{"api": {"_stcore_health": {"ms": 10}}, "sys": {}}]))
    monkeypatch.setattr("sys.argv", ["metrics_collector.py", "--summary"])
    mc.main()
    out = capsys.readouterr().out
    assert "n=1" in out or "No data" in out


def test_main_report_mode(monkeypatch, capsys, patched):
    patched.write_text(
        json.dumps([{"api": {"_stcore_health": {"ms": 10}}, "sys": {"load_1m": 0.1}}])
    )
    monkeypatch.setattr("sys.argv", ["metrics_collector.py", "--report"])
    mc.main()
    out = capsys.readouterr().out
    assert "Timestamp" in out


def test_main_regressions_mode_clean(monkeypatch, capsys, patched):
    patched.write_text(json.dumps([{"api": {}, "sys": {}}]))
    monkeypatch.setattr("sys.argv", ["metrics_collector.py", "--regressions"])
    mc.main()
    out = capsys.readouterr().out
    assert "No regressions" in out
