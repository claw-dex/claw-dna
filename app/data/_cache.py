"""Cache infrastructure — TTL cache, mtime-based decorators, and auto-registration."""

import os
import threading
import time
from functools import wraps

import streamlit as st

from app.data._helpers import _read_json_safe

# ── Custom TTL cache (replaces @st.cache_data to avoid cachetools TOCTOU race) ─
# The cachetools TTLCache has a race: `key in cache` passes, then TTL expires
# before `cache[key]`, causing an uncaught KeyError that crashes Streamlit tabs.
# This implementation uses a single dict.get() call (atomic) to avoid the race.

_MISSING = object()


class _TTLCache:
    """Thread-safe TTL cache using atomic dict.get() to avoid TOCTOU race."""

    def __init__(self):
        self._store: dict = {}  # key -> (value, expiry_float)
        self._lock = threading.Lock()

    def get(self, key):
        """Return (value, found). Never raises KeyError."""
        with self._lock:
            entry = self._store.get(key, _MISSING)
        if entry is _MISSING:
            return None, False
        value, expiry = entry
        if time.monotonic() > expiry:
            with self._lock:
                # Double-check under lock before removing
                entry2 = self._store.get(key, _MISSING)
                if entry2 is not _MISSING and time.monotonic() > entry2[1]:
                    del self._store[key]
            return None, False
        return value, True

    def set(self, key, value, ttl):
        with self._lock:
            self._store[key] = (value, time.monotonic() + ttl)

    def clear(self):
        with self._lock:
            self._store.clear()


_GLOBAL_CACHE = _TTLCache()

# Registry of all mtime-cache dicts; populated by @_mfile_cache / @_mmfile_cache at decoration time.
# _cache_clear_all() iterates this list so new loaders are cleared automatically.
_MFILE_CACHES: list = []

# Registry of hand-rolled cache dicts; populated by _register_cache().
# _cache_clear_all() iterates this list so domain modules don't need explicit listing.
_HAND_CACHES: list[dict] = []


def _register_cache() -> dict:
    """Create and register a hand-rolled cache dict, auto-cleared by _cache_clear_all()."""
    cache: dict = {}
    _HAND_CACHES.append(cache)
    return cache


def _mfile_cache(path_fn, default_fn):
    """Decorator: mtime-based cache for a single-file JSON loader.

    Replaces the 6-8 lines of identical boilerplate in each simple mtime-cached
    loader (get mtime → check cache → load+store). Cache dicts are allocated once
    per decorated function and auto-registered in _MFILE_CACHES so _cache_clear_all()
    clears them without listing each one explicitly.

    Args:
        path_fn:    callable() → str  — path to the JSON file (called each invocation)
        default_fn: callable() → any — fresh default value (called on miss)

    The wrapped function receives the loaded data and may return a modified value
    (e.g. for type-normalisation). It must accept exactly one positional arg: ``data``.

    Usage::

        @_mfile_cache(lambda: f"{MEMORY_DIR}/state.json",
                      lambda: {"status": "awaiting_first_heartbeat", "cycle_number": 0})
        def load_state(data):
            return data          # no post-processing needed

        @_mfile_cache(lambda: f"{MEMORY_DIR}/cycles.json", list)
        def load_cycles(data):
            return data if isinstance(data, list) else []  # normalise
    """
    _store: dict = {}  # {"data": (result, mtime)}
    _MFILE_CACHES.append(_store)

    def decorator(fn):
        @wraps(fn)
        def wrapper():
            path = path_fn()
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            cached = _store.get("data")
            if cached is not None:
                result, cached_mtime = cached
                if cached_mtime == mtime:
                    return result
            data = _read_json_safe(path, default_fn())
            result = fn(data)
            _store["data"] = (result, mtime)
            return result

        return wrapper

    return decorator


def _mmfile_cache(path_fns):
    """Decorator: mtime-based cache for derived loaders depending on multiple source files.

    Like @_mfile_cache but for functions that read from N source files (directories,
    derived computed values, or any set of paths). Auto-registered in _MFILE_CACHES so
    _cache_clear_all() clears it without manual listing.

    Args:
        path_fns: list of callables, each callable() → str (path to a source file or dir)

    The decorated function receives no arguments — it does its own loading (typically
    via the already-mtime-cached single-file loaders). Cache key is a tuple of mtimes.

    Usage::

        @_mmfile_cache([lambda: GOALS_PATH, lambda: f"{MEMORY_DIR}/cycles.json"])
        def load_goal_stats():
            goals = load_goals() or []
            cycles = load_cycles() or []
            ...
            return result
    """
    _store: dict = {}  # {"data": (result, mtime_tuple)}
    _MFILE_CACHES.append(_store)

    def _mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    def decorator(fn):
        @wraps(fn)
        def wrapper():
            key = tuple(_mtime(p()) for p in path_fns)
            cached = _store.get("data")
            if cached is not None:
                result, cached_key = cached
                if cached_key == key:
                    return result
            result = fn()
            _store["data"] = (result, key)
            return result

        return wrapper

    return decorator


def _cache(ttl=10):
    """Decorator: cache function result for `ttl` seconds with atomic TTL lookup.

    Key strategy (optimised in cycle 145):
    - No-arg calls: use fn.__name__ directly (no allocation per call).
    - Calls with args: use a tuple key (fn.__name__, *args, **items) —
      tuples hash faster than their str() equivalent and avoid a string alloc.
    - clear() matches on k[0] == fn.__name__ instead of str.startswith().
    """

    def decorator(fn):
        _fn_name = fn.__name__  # capture once at decoration time

        @wraps(fn)
        def wrapper(*args, **kwargs):
            if args or kwargs:
                try:
                    key = (_fn_name,) + args + tuple(sorted(kwargs.items()))
                except TypeError:
                    key = _fn_name  # unhashable arg fallback
            else:
                key = _fn_name  # constant key — no tuple/string allocation

            value, found = _GLOBAL_CACHE.get(key)
            if found:
                return value
            value = fn(*args, **kwargs)
            _GLOBAL_CACHE.set(key, value, ttl)
            return value

        def clear():
            """Clear all cached entries for this function."""
            with _GLOBAL_CACHE._lock:
                keys_to_del = [
                    k
                    for k in _GLOBAL_CACHE._store
                    if k == _fn_name
                    or (isinstance(k, tuple) and k and k[0] == _fn_name)
                ]
                for k in keys_to_del:
                    del _GLOBAL_CACHE._store[k]

        wrapper.clear = clear
        return wrapper

    return decorator


def _cache_clear_all():
    """Clear all cached data (called by write operations)."""
    _GLOBAL_CACHE.clear()
    # Clear all mtime-based caches auto-registered via @_mfile_cache / @_mmfile_cache.
    for c in _MFILE_CACHES:
        c.clear()
    # Clear all hand-rolled caches auto-registered via _register_cache().
    for c in _HAND_CACHES:
        c.clear()
    try:
        st.cache_data.clear()
    except Exception:
        pass
