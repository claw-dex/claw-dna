#!/usr/bin/env python3
"""
metrics_collector.py — Collect and store time-series metrics for the agent.

Measures Streamlit portal response time, system resource usage (CPU, memory, disk),
and appends a snapshot to /agent/memory/metrics.json (ring buffer, max 200 entries).

Usage:
    python3 metrics_collector.py              # Collect one snapshot and append
    python3 metrics_collector.py --report     # Print last 10 snapshots as table
    python3 metrics_collector.py --json       # Print last snapshot as JSON
    python3 metrics_collector.py --summary    # Print one-line summary (averages)
    python3 metrics_collector.py --limit N    # Report on last N snapshots (default 10)
    python3 metrics_collector.py --max N      # Max ring buffer size (default 200)
"""

import json
import shutil
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

AGENT_DIR = Path("/agent")
MEMORY_DIR = AGENT_DIR / "memory"
METRICS_FILE = MEMORY_DIR / "metrics.json"
BASE_URL = "http://localhost:8081/app"
DEFAULT_MAX = 200

# Endpoints to probe (path, warn_ms)
# Streamlit exposes /_stcore/health which returns "ok" when ready.
PROBED_ENDPOINTS = [
    ("/_stcore/health", 100),
]


# ─── Collection ───────────────────────────────────────────────────────────────

def _probe_endpoint(path: str) -> dict:
    """Measure response time for one endpoint. Returns {ms, status, ok}."""
    url = BASE_URL + path
    t0 = time.perf_counter()
    try:
        req = urllib.request.urlopen(url, timeout=5)
        status = req.status
        body = req.read().decode("utf-8", errors="replace").strip()
        ok = (status == 200 and body == "ok")
    except urllib.error.HTTPError as e:
        status = e.code
        ok = False
    except Exception:
        status = 0
        ok = False
    ms = round((time.perf_counter() - t0) * 1000, 1)
    return {"ms": ms, "status": status, "ok": ok}


def _read_sys_resource(key: str) -> float | None:
    """Read a single value from /proc. Returns None on failure."""
    try:
        if key == "mem_used_mb":
            lines = Path("/proc/meminfo").read_text().splitlines()
            info = {}
            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
            total = info.get("MemTotal", 0)
            avail = info.get("MemAvailable", 0)
            used = total - avail
            return round(used / 1024, 1)  # MB
        elif key == "mem_total_mb":
            lines = Path("/proc/meminfo").read_text().splitlines()
            for line in lines:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024, 1)
        elif key == "load_1m":
            parts = Path("/proc/loadavg").read_text().split()
            return float(parts[0])
        elif key == "disk_used_gb":
            usage = shutil.disk_usage("/agent/workspace")
            return round(usage.used / (1024 ** 3), 3)
        elif key == "disk_total_gb":
            usage = shutil.disk_usage("/agent/workspace")
            return round(usage.total / (1024 ** 3), 3)
    except Exception:
        pass
    return None


def collect_snapshot(max_entries: int = DEFAULT_MAX) -> dict:
    """Collect one metrics snapshot and append to metrics.json."""
    now = datetime.now(timezone.utc).isoformat()

    # Probe API endpoints
    api_times = {}
    for path, _warn in PROBED_ENDPOINTS:
        key = path.lstrip("/").replace("/", "_").replace("-", "_")
        api_times[key] = _probe_endpoint(path)

    # System resources
    resources = {
        "mem_used_mb":  _read_sys_resource("mem_used_mb"),
        "mem_total_mb": _read_sys_resource("mem_total_mb"),
        "load_1m":      _read_sys_resource("load_1m"),
        "disk_used_gb": _read_sys_resource("disk_used_gb"),
        "disk_total_gb": _read_sys_resource("disk_total_gb"),
    }

    snapshot = {
        "ts": now,
        "api": api_times,
        "sys": resources,
    }

    # Load existing, append, trim to ring buffer
    entries = _load_metrics()
    entries.append(snapshot)
    if len(entries) > max_entries:
        entries = entries[-max_entries:]

    # Atomic write
    tmp = METRICS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2))
    tmp.rename(METRICS_FILE)

    return snapshot


# ─── Read ─────────────────────────────────────────────────────────────────────

def _load_metrics() -> list:
    """Load existing metrics. Returns [] if file missing or invalid."""
    if not METRICS_FILE.exists():
        return []
    try:
        data = json.loads(METRICS_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_recent(limit: int = 10) -> list:
    """Return last N snapshots."""
    return _load_metrics()[-limit:]


# ─── Reporting ────────────────────────────────────────────────────────────────

def _avg(values: list) -> float | None:
    clean = [v for v in values if isinstance(v, (int, float))]
    return round(sum(clean) / len(clean), 1) if clean else None


def report_table(limit: int = 10) -> None:
    """Print a summary table of recent snapshots."""
    entries = get_recent(limit)
    if not entries:
        print("No metrics collected yet. Run without flags to collect.")
        return

    hdr = f"{'Timestamp':<27} {'portal_ms':>10} {'load':>6} {'mem%':>6} {'disk_gb':>8}"
    print(hdr)
    print("-" * len(hdr))

    for e in entries:
        ts = e.get("ts", "")[:26]
        api = e.get("api", {})
        sys_ = e.get("sys", {})

        health_ms = api.get("_stcore_health", {}).get("ms", "-")

        load = sys_.get("load_1m", "-")
        mem_used  = sys_.get("mem_used_mb")
        mem_total = sys_.get("mem_total_mb")
        if mem_used and mem_total:
            mem_pct = f"{round(mem_used / mem_total * 100)}%"
        else:
            mem_pct = "-"
        disk = sys_.get("disk_used_gb", "-")

        print(f"{ts:<27} {str(health_ms):>10} {str(load):>6} {mem_pct:>6} {str(disk):>8}")


def report_summary(limit: int = 10) -> str:
    """Return a one-line summary of averages over last N snapshots."""
    entries = get_recent(limit)
    if not entries:
        return "No metrics data."

    health_vals = [e["api"].get("_stcore_health", {}).get("ms") for e in entries if "api" in e]
    load_vals   = [e["sys"].get("load_1m") for e in entries if "sys" in e]
    mem_vals    = [e["sys"].get("mem_used_mb") for e in entries if "sys" in e]
    mem_totals  = [e["sys"].get("mem_total_mb") for e in entries if "sys" in e]

    avg_health = _avg(health_vals)
    avg_load   = _avg(load_vals)

    mem_pct = None
    clean_used  = [v for v in mem_vals if isinstance(v, (int, float))]
    clean_total = [v for v in mem_totals if isinstance(v, (int, float))]
    if clean_used and clean_total:
        mem_pct = round(sum(clean_used) / len(clean_used) / (sum(clean_total) / len(clean_total)) * 100)

    n = len(entries)
    parts = [f"n={n}"]
    if avg_health: parts.append(f"portal={avg_health}ms")
    if avg_load:   parts.append(f"load={avg_load}")
    if mem_pct:    parts.append(f"mem={mem_pct}%")
    return "avg: " + "  ".join(parts)


# ─── Regression detection ─────────────────────────────────────────────────────

def check_regressions(warn_thresholds: dict | None = None) -> list[str]:
    """
    Compare most recent snapshot against rolling average of prior 9 snapshots.
    Returns list of warning strings (empty = all good).
    """
    entries = _load_metrics()
    if len(entries) < 3:
        return []

    recent = entries[-1]
    baseline = entries[-10:-1]  # up to 9 prior snapshots

    warnings = []
    default_warn_pct = 50  # warn if latest is >50% slower than baseline avg

    endpoints = {
        "_stcore_health": 100,
    }
    overrides = warn_thresholds or {}

    for key, abs_warn_ms in endpoints.items():
        latest_ms = recent.get("api", {}).get(key, {}).get("ms")
        if latest_ms is None:
            continue
        base_vals = [e.get("api", {}).get(key, {}).get("ms") for e in baseline]
        base_vals = [v for v in base_vals if isinstance(v, (int, float))]
        if not base_vals:
            continue
        base_avg = sum(base_vals) / len(base_vals)
        pct_increase = ((latest_ms - base_avg) / base_avg * 100) if base_avg > 0 else 0
        threshold_pct = overrides.get(key, default_warn_pct)
        if pct_increase > threshold_pct and latest_ms > abs_warn_ms * 0.5:
            warnings.append(
                f"{key}: {latest_ms}ms (baseline avg {round(base_avg, 1)}ms, +{round(pct_increase)}%)"
            )

    return warnings


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    report = "--report" in args
    as_json = "--json" in args
    summary = "--summary" in args
    regressions = "--regressions" in args

    limit = 10
    if "--limit" in args:
        idx = args.index("--limit")
        if idx + 1 < len(args):
            try:
                limit = int(args[idx + 1])
            except ValueError:
                pass

    max_entries = DEFAULT_MAX
    if "--max" in args:
        idx = args.index("--max")
        if idx + 1 < len(args):
            try:
                max_entries = int(args[idx + 1])
            except ValueError:
                pass

    if report:
        report_table(limit)
        return

    if summary:
        entries = get_recent(limit)
        if not entries:
            print("No data yet.")
            return
        print(report_summary(limit))
        return

    if regressions:
        warns = check_regressions()
        if warns:
            print("REGRESSIONS DETECTED:")
            for w in warns:
                print(f"  ⚠  {w}")
            sys.exit(1)
        else:
            print("No regressions detected.")
        return

    # Default: collect one snapshot
    snap = collect_snapshot(max_entries)
    if as_json:
        print(json.dumps(snap, indent=2))
    else:
        ts = snap["ts"][:19].replace("T", " ")
        health_ms = snap["api"].get("_stcore_health", {}).get("ms", "?")
        load      = snap["sys"].get("load_1m", "?")
        mem_used  = snap["sys"].get("mem_used_mb")
        mem_total = snap["sys"].get("mem_total_mb")
        mem_pct   = f"{round(mem_used / mem_total * 100)}%" if mem_used and mem_total else "?"
        disk      = snap["sys"].get("disk_used_gb", "?")
        print(f"[{ts}] portal={health_ms}ms  load={load}  mem={mem_pct}  disk={disk}GB")


if __name__ == "__main__":
    main()
