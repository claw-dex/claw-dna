"""Message loaders — inbox, inbox_history, outbox, outbox_history, history."""

from app.data._cache import _mfile_cache
from app.shared import MESSAGES_DIR, HISTORY_PATH


@_mfile_cache(lambda: f"{MESSAGES_DIR}/inbox.json", list)
def load_inbox(data):
    """Load inbox.json — mtime-cached, 0 reads between user commands."""
    return data


@_mfile_cache(lambda: f"{MESSAGES_DIR}/inbox_history.json", list)
def load_inbox_history(data):
    """Load inbox_history.json — mtime-cached, 0 parses between cycle writes."""
    return data


@_mfile_cache(lambda: f"{MESSAGES_DIR}/outbox.json", list)
def load_outbox(data):
    """Load outbox.json — mtime-cached; normalise to list (may be a single dict)."""
    if isinstance(data, dict):
        return [data]
    return data if isinstance(data, list) else []


@_mfile_cache(lambda: f"{MESSAGES_DIR}/outbox_history.json", list)
def load_outbox_history(data):
    """Load outbox_history.json (17KB+) — mtime-cached, 0 parses between cycle writes.

    Lives alongside inbox.json / inbox_history.json / outbox.json under
    /agent/messages/ — outbox_history was previously under /agent/memory/
    but that placement was an outlier vs. the other messaging artifacts.
    """
    return data


@_mfile_cache(lambda: HISTORY_PATH, list)
def load_history(data):
    """Load command_history.json — mtime-cached, invalidates at user interaction rate."""
    return data
