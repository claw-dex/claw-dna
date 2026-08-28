"""Bash log loaders (legacy, read-only)."""

import glob
import os
import time
from datetime import datetime

from app.data._cache import _register_cache
from app.data._helpers import _read_json_safe, _read_text_safe, _pid_alive
from app.shared import LOGS_DIR

_LOGS_CACHE = _register_cache()
_LOG_DETAIL_CACHE = _register_cache()


def load_logs():
    """List bash command log metadata from /agent/memory/logs/.

    Uses directory mtime-based caching: re-scans only when the logs directory
    changes (new bash-N.json file added). Previously @_cache(ttl=5) caused a
    full glob + stat pass every 5s even when no new logs had been written.
    Note: in-progress logs (status=running) need live PID checks — we add a
    30-second time bucket so running jobs still refresh periodically.
    """
    try:
        dir_mtime = os.path.getmtime(LOGS_DIR)
    except OSError:
        dir_mtime = 0.0
    # Running jobs need periodic refresh even when dir hasn't changed;
    # bucket into 30s windows so live job status stays reasonably fresh.
    time_bucket = int(time.monotonic() // 30)

    cached = _LOGS_CACHE.get("data")
    if cached is not None:
        result, c_mtime, c_bucket = cached
        if c_mtime == dir_mtime and c_bucket == time_bucket:
            return result

    logs = []
    for meta_file in sorted(glob.glob(f"{LOGS_DIR}/bash-*.json")):
        raw = _read_json_safe(meta_file)
        if not raw:
            continue
        # Work on a fresh copy so we don't mutate any cached object
        meta = dict(raw)
        if meta.get("status") == "running" and not _pid_alive(meta.get("pid")):
            meta["status"] = "exited"
        if meta.get("exit_code") is None and meta.get("exitcode_log"):
            try:
                with open(meta["exitcode_log"]) as f:
                    meta["exit_code"] = int(f.read().strip())
            except (OSError, ValueError):
                pass
        if meta.get("started_at") and meta.get("ended_at"):
            try:
                start = datetime.fromisoformat(meta["started_at"])
                end = datetime.fromisoformat(meta["ended_at"])
                meta["duration_seconds"] = round((end - start).total_seconds(), 1)
            except Exception:
                pass
        logs.append(meta)
    _LOGS_CACHE["data"] = (logs, dir_mtime, time_bucket)
    return logs


def load_log_detail(num):
    """Load a single bash log entry with its stdout/stderr content.

    Uses compound mtime-based caching on (bash-N.json, stdout_log, stderr_log).
    Re-reads only when a file actually changes — previously @_cache(ttl=3) re-read
    all three files every 3 seconds even for long-completed logs that never change.

    For running processes the stdout/stderr files grow continuously, so they will
    have updated mtimes and the cache naturally invalidates on each new output.
    """
    meta_path = f"{LOGS_DIR}/bash-{num}.json"

    def _mtime(p):
        try:
            return os.path.getmtime(p) if p else 0.0
        except OSError:
            return 0.0

    meta_mtime = _mtime(meta_path)
    raw = _read_json_safe(meta_path)
    if not raw:
        _LOG_DETAIL_CACHE.pop(num, None)
        return None

    meta = dict(raw)
    stdout_path = meta.get("stdout_log", "")
    stderr_path = meta.get("stderr_log", "")
    stdout_mtime = _mtime(stdout_path)
    stderr_mtime = _mtime(stderr_path)

    cached = _LOG_DETAIL_CACHE.get(num)
    if cached is not None:
        result, c_meta_m, c_out_m, c_err_m = cached
        if (
            c_meta_m == meta_mtime
            and c_out_m == stdout_mtime
            and c_err_m == stderr_mtime
        ):
            return result

    if meta.get("status") == "running" and not _pid_alive(meta.get("pid")):
        meta["status"] = "exited"
    stdout = _read_text_safe(stdout_path) or ""
    stderr = _read_text_safe(stderr_path) or ""
    max_len = 10000
    if len(stdout) > max_len:
        stdout = stdout[:max_len] + "\n... (truncated)"
    if len(stderr) > max_len:
        stderr = stderr[:max_len] + "\n... (truncated)"
    result = {**meta, "stdout": stdout, "stderr": stderr}
    _LOG_DETAIL_CACHE[num] = (result, meta_mtime, stdout_mtime, stderr_mtime)
    return result


load_log_detail.clear = _LOG_DETAIL_CACHE.clear
