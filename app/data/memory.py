"""Memory file loaders."""

import os
import time

from app.data._cache import _register_cache
from app.data._helpers import _IMAGE_EXTENSIONS, _read_text_safe
from app.shared import MEMORY_DIR

_MEMFILES_CACHE = _register_cache()
_MEMORY_FILE_CACHE = _register_cache()
_LTM_SIZE_CACHE = _register_cache()

# The long-term memory store is a LanceDB directory, not a single file, so its
# footprint needs a recursive walk. Keep the name in sync with
# scripts/memory_store.DEFAULT_DB.
LTM_STORE_NAME = "long_term_memory.lancedb"
_LTM_SIZE_TTL_SECONDS = 60


def load_ltm_size() -> int:
    """Total bytes of the long-term memory store, or 0 when it is absent.

    Walking the store can touch a few hundred files, so the result is cached
    for a minute — the size only moves when a cycle closes.
    """
    path = os.path.join(MEMORY_DIR, LTM_STORE_NAME)
    now = time.monotonic()
    cached = _LTM_SIZE_CACHE.get("data")
    if cached is not None:
        size, stamp = cached
        if now - stamp < _LTM_SIZE_TTL_SECONDS:
            return size

    total = 0
    if os.path.isdir(path):
        for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    _LTM_SIZE_CACHE["data"] = (total, now)
    return total


def load_memory_files():
    """List files in /agent/memory/.

    Uses directory mtime-based caching: re-reads only when the directory itself changes
    (file added/removed). Previously TTL=10s caused re-listing every 10s even during idle
    cycles when nothing in MEMORY_DIR changed.
    """
    try:
        dir_mtime = os.path.getmtime(MEMORY_DIR)
    except OSError:
        dir_mtime = 0.0
    cached = _MEMFILES_CACHE.get("data")
    if cached is not None:
        result, c_mtime = cached
        if c_mtime == dir_mtime:
            return result
    try:
        files = [
            f
            for f in os.listdir(MEMORY_DIR)
            if os.path.isfile(os.path.join(MEMORY_DIR, f)) and not f.startswith(".")
        ]
        files.sort()
    except OSError:
        files = []
    _MEMFILES_CACHE["data"] = (files, dir_mtime)
    return files


def read_memory_file(filename):
    """Read a single memory file by name.

    Uses per-file mtime-based caching: re-reads only when the file's mtime changes.
    Previously TTL=10s caused re-reads every 10s even when the file hadn't changed.
    """
    if "/" in filename or ".." in filename or filename.startswith("."):
        return None
    filepath = os.path.join(MEMORY_DIR, filename)
    if not os.path.isfile(filepath):
        _MEMORY_FILE_CACHE.pop(filename, None)
        return None
    ext = os.path.splitext(filepath)[1].lower()
    if ext in _IMAGE_EXTENSIONS:
        return {"__type__": "image", "path": filepath}
    try:
        mtime = os.path.getmtime(filepath)
    except OSError:
        mtime = 0.0
    cached = _MEMORY_FILE_CACHE.get(filename)
    if cached is not None:
        result, c_mtime = cached
        if c_mtime == mtime:
            return result
    result = _read_text_safe(filepath)
    _MEMORY_FILE_CACHE[filename] = (result, mtime)
    return result
