"""Script inventory loader."""

import os
from datetime import datetime, timezone

from app.data._cache import _register_cache
from app.shared import SCRIPTS_DIR

# Mtime-based description cache: path -> (mtime, description_str)
# Avoids re-reading script files when content hasn't changed.
# Not auto-registered — mtime-based per-file cache doesn't need global invalidation.
_SCRIPT_DESC_CACHE: dict = {}

_SCRIPT_CATEGORIES = {
    "cycle_start.py": "Cycle Management",
    "cycle_close.py": "Cycle Management",
    "cycle_report.py": "Cycle Management",
    "repair_memory_files.py": "Memory",
    "memory_ask.py": "Memory",
    "memory_recall.py": "Memory",
    "memory_ingest.py": "Memory",
    "memory_inspect.py": "Memory",
    "sync_memory_files.py": "Memory",
    "journal_archive.py": "Memory",
    "health_check.sh": "Diagnostics",
    "self_test.py": "Diagnostics",
    "maintain.py": "Diagnostics",
    "metrics_collector.py": "Diagnostics",
    "app_check.py": "Diagnostics",
    "milestone_report.py": "Diagnostics",
    "server_restart.sh": "Diagnostics",
    "log_cleanup.sh": "Diagnostics",
    "service_manager.py": "Services",
    "portal_config.py": "Services",
    "scheduler.py": "Services",
    "keepass.py": "Services",
    "notes.py": "Other",
    "reminder.py": "Other",
    "email_imap.py": "Other",
    "callmebot.py": "Other",
}


_SCRIPTS_CACHE = _register_cache()


def _script_description(path: str) -> str:
    """Extract the first meaningful docstring or comment line from a script.

    Strips leading 'scriptname — ' prefix so descriptions are concise.
    Uses an mtime-based cache to avoid re-reading unchanged files.
    """
    fname = os.path.basename(path)

    # Check mtime cache — skip file read if unchanged
    try:
        mtime = os.path.getmtime(path)
        cached = _SCRIPT_DESC_CACHE.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    except OSError:
        return ""

    raw = ""
    try:
        with open(path) as fh:
            in_docstring = False
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#!"):
                    continue
                # Shell comment
                if stripped.startswith("# ") and not stripped.startswith("#!/"):
                    raw = stripped.lstrip("# ").strip()
                    break
                # Python triple-quoted docstring (opening line)
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    inner = stripped.strip('"""').strip("'''").strip()
                    if inner:
                        raw = inner
                        break
                    in_docstring = True
                    continue
                if in_docstring:
                    if (
                        stripped
                        and not stripped.startswith('"""')
                        and not stripped.startswith("'''")
                    ):
                        raw = stripped
                    break
    except OSError:
        pass

    # Strip "scriptname — " or "scriptname - " prefix if present
    for sep in (" — ", " - "):
        prefix = fname + sep
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break

    _SCRIPT_DESC_CACHE[path] = (mtime, raw)
    return raw


def load_scripts():
    """List utility scripts from /agent/scripts/.

    Uses directory mtime-based caching: re-reads only when the scripts directory itself
    changes (script added/removed). Previously TTL=30s caused a full directory scan + stat
    on every 30s expiry even when no scripts had been added or modified.
    Note: individual script content changes won't invalidate this cache (only dir mtime
    changes), but script descriptions are separately mtime-cached per-file via _SCRIPT_DESC_CACHE.
    """
    try:
        dir_mtime = os.path.getmtime(SCRIPTS_DIR)
    except OSError:
        dir_mtime = 0.0
    cached = _SCRIPTS_CACHE.get("data")
    if cached is not None:
        result, c_mtime = cached
        if c_mtime == dir_mtime:
            return result
    scripts = []
    try:
        for f in sorted(os.listdir(SCRIPTS_DIR)):
            fpath = os.path.join(SCRIPTS_DIR, f)
            if os.path.isfile(fpath) and (f.endswith(".py") or f.endswith(".sh")):
                st_info = os.stat(fpath)
                scripts.append(
                    {
                        "name": f,
                        "size": st_info.st_size,
                        "modified": datetime.fromtimestamp(
                            st_info.st_mtime, tz=timezone.utc
                        ).isoformat(),
                        "description": _script_description(fpath),
                        "category": _SCRIPT_CATEGORIES.get(f, "Other"),
                    }
                )
    except OSError:
        pass
    _SCRIPTS_CACHE["data"] = (scripts, dir_mtime)
    return scripts
