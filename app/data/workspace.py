"""Workspace file loaders."""

import os
from datetime import datetime, timezone

from app.data._cache import _register_cache
from app.data._helpers import _IMAGE_EXTENSIONS, _read_text_safe
from app.shared import AGENT_DIR

_WORKSPACE_FILES_CACHE = _register_cache()
_WORKSPACE_FILE_CACHE = _register_cache()


def load_workspace_files():
    """List files in /agent/workspace/.

    Uses directory mtime-based caching: re-walks only when the workspace directory
    itself changes (file added/removed). Previously TTL=30s caused a full os.walk
    + os.stat pass every 30s even during idle cycles when nothing had changed.
    """
    workspace = f"{AGENT_DIR}/workspace"
    try:
        dir_mtime = os.path.getmtime(workspace)
    except OSError:
        dir_mtime = 0.0
    cached = _WORKSPACE_FILES_CACHE.get("data")
    if cached is not None:
        result, c_mtime = cached
        if c_mtime == dir_mtime:
            return result
    files = []
    try:
        for root, dirs, filenames in os.walk(workspace):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, workspace)
                try:
                    st_info = os.stat(fpath)
                    size = st_info.st_size
                    mtime = datetime.fromtimestamp(
                        st_info.st_mtime, tz=timezone.utc
                    ).isoformat()
                except OSError:
                    size = 0
                    mtime = None
                files.append({"path": rel, "size": size, "modified": mtime})
                if len(files) >= 200:
                    break
            if len(files) >= 200:
                break
    except OSError:
        pass
    files.sort(key=lambda f: f.get("modified") or "", reverse=True)
    result = {"files": files, "count": len(files)}
    _WORKSPACE_FILES_CACHE["data"] = (result, dir_mtime)
    return result


def read_workspace_file(path):
    """Read a single workspace file by relative path.

    Uses per-file mtime-based caching: re-reads from disk only when the file's
    mtime changes. Previously @_cache(ttl=10) caused a full re-read every 10s
    even when the file hadn't changed — wasteful for static workspace assets
    that are written once per cycle (~5 min apart).
    """
    rel_path = path.lstrip("/")
    if ".." in rel_path or rel_path.startswith("."):
        return None
    filepath = os.path.normpath(os.path.join(f"{AGENT_DIR}/workspace", rel_path))
    workspace_abs = os.path.abspath(f"{AGENT_DIR}/workspace")
    if not os.path.abspath(filepath).startswith(workspace_abs + os.sep):
        return None
    if not os.path.isfile(filepath):
        _WORKSPACE_FILE_CACHE.pop(filepath, None)
        return None
    ext = os.path.splitext(filepath)[1].lower()
    if ext in _IMAGE_EXTENSIONS:
        return {"__type__": "image", "path": filepath}
    try:
        mtime = os.path.getmtime(filepath)
    except OSError:
        return None
    cached = _WORKSPACE_FILE_CACHE.get(filepath)
    if cached is not None:
        result, c_mtime = cached
        if c_mtime == mtime:
            return result
    size = os.path.getsize(filepath)
    if size > 100_000:
        result = f"(File too large to preview: {round(size/1024)} KB)"
    else:
        result = _read_text_safe(filepath)
    _WORKSPACE_FILE_CACHE[filepath] = (result, mtime)
    return result
