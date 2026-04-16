"""Memory file loaders."""

import os

from app.data._cache import _register_cache
from app.data._helpers import _IMAGE_EXTENSIONS, _read_text_safe
from app.shared import MEMORY_DIR

_MEMFILES_CACHE = _register_cache()
_MEMORY_FILE_CACHE = _register_cache()


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
