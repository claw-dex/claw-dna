"""Journal loaders — parse + paginate journal entries."""

import os

from app.data._cache import _register_cache
from app.data._helpers import _read_json_safe
from app.shared import MEMORY_DIR


_JOURNAL_CACHE = _register_cache()


def _parse_journal_entries():
    """Load journal.json + journal-archive.json entries, sorted by cycle descending.

    Uses mtime-based caching: re-parses only when either file actually changes.
    More efficient than TTL-based caching — avoids redundant I/O when multiple
    load_journal(limit=N) calls occur within the same render cycle.
    """
    active_path  = f"{MEMORY_DIR}/journal.json"
    archive_path = f"{MEMORY_DIR}/journal-archive.json"

    try:
        active_mtime = os.path.getmtime(active_path)
    except OSError:
        active_mtime = 0.0
    try:
        archive_mtime = os.path.getmtime(archive_path)
    except OSError:
        archive_mtime = 0.0

    cached = _JOURNAL_CACHE.get("data")
    if cached is not None:
        result, cached_am, cached_arch = cached
        if cached_am == active_mtime and cached_arch == archive_mtime:
            return result

    active = _read_json_safe(active_path, [])
    if not isinstance(active, list):
        active = []
    archived = _read_json_safe(archive_path, [])
    if not isinstance(archived, list):
        archived = []
    # Merge; deduplicate by cycle number (active takes precedence)
    active_cycles = {e.get("cycle") for e in active}
    merged = active + [e for e in archived if e.get("cycle") not in active_cycles]
    merged.sort(key=lambda e: e.get("cycle", 0), reverse=True)

    _JOURNAL_CACHE["data"] = (merged, active_mtime, archive_mtime)
    return merged


def load_journal(limit=20, offset=0):
    """Paginate journal entries from JSON files."""
    all_entries = _parse_journal_entries()
    total = len(all_entries)
    page = all_entries[offset:offset + limit] if limit > 0 else all_entries[offset:]
    return {"entries": page, "total": total, "offset": offset, "has_more": (offset + len(page)) < total}
