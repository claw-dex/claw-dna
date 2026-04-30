"""Unit tests for app.data._cache (TTL, mtime, registry)."""

from __future__ import annotations

import json
import time

import pytest

from app.data import _cache as cache_mod


@pytest.fixture(autouse=True)
def _reset_cache_state():
    """Snapshot and restore registry state so tests don't bleed."""
    saved_mfile = list(cache_mod._MFILE_CACHES)
    saved_hand = list(cache_mod._HAND_CACHES)
    cache_mod._GLOBAL_CACHE.clear()
    yield
    cache_mod._MFILE_CACHES[:] = saved_mfile
    cache_mod._HAND_CACHES[:] = saved_hand
    cache_mod._GLOBAL_CACHE.clear()


# ---------- _TTLCache ----------


def test_ttlcache_set_and_get_returns_value():
    c = cache_mod._TTLCache()
    c.set("k", 42, ttl=10)
    val, found = c.get("k")
    assert found is True
    assert val == 42


def test_ttlcache_missing_returns_not_found():
    c = cache_mod._TTLCache()
    val, found = c.get("missing")
    assert found is False
    assert val is None


def test_ttlcache_expires_after_ttl(monkeypatch):
    c = cache_mod._TTLCache()
    fake_time = [1000.0]
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: fake_time[0])
    c.set("k", "v", ttl=5)
    fake_time[0] = 1004.0  # within TTL
    val, found = c.get("k")
    assert found is True
    fake_time[0] = 1006.0  # past TTL
    val, found = c.get("k")
    assert found is False
    # Entry was purged
    assert "k" not in c._store


def test_ttlcache_clear():
    c = cache_mod._TTLCache()
    c.set("a", 1, ttl=10)
    c.set("b", 2, ttl=10)
    c.clear()
    assert c.get("a") == (None, False)
    assert c.get("b") == (None, False)


# ---------- @_cache decorator ----------


def test_cache_decorator_caches_result():
    calls = {"n": 0}

    @cache_mod._cache(ttl=10)
    def f():
        calls["n"] += 1
        return "x"

    assert f() == "x"
    assert f() == "x"
    assert calls["n"] == 1


def test_cache_decorator_with_args_keys_separately():
    calls = {"n": 0}

    @cache_mod._cache(ttl=10)
    def f(x, y=0):
        calls["n"] += 1
        return x + y

    assert f(1, y=2) == 3
    assert f(1, y=2) == 3
    assert f(2, y=2) == 4
    assert calls["n"] == 2


def test_cache_decorator_clear_removes_only_its_keys():
    @cache_mod._cache(ttl=10)
    def f():
        return "f"

    @cache_mod._cache(ttl=10)
    def g():
        return "g"

    f()
    g()
    f.clear()
    # f recomputes; g still cached.
    assert f() == "f"
    val, found = cache_mod._GLOBAL_CACHE.get("g")
    assert found is True


def test_cache_decorator_hashable_args_work():
    @cache_mod._cache(ttl=10)
    def f(x):
        return x * 2

    # Hashable args build a tuple key without raising.
    assert f(5) == 10
    assert f(5) == 10  # cached
    assert f("s") == "ss"


# ---------- @_mfile_cache ----------


def test_mfile_cache_loads_and_caches(tmp_path):
    p = tmp_path / "data.json"
    p.write_text(json.dumps([1, 2, 3]))

    @cache_mod._mfile_cache(lambda: str(p), list)
    def loader(data):
        return data

    assert loader() == [1, 2, 3]
    # Modify file content WITHOUT changing mtime — should still return cached.
    mtime = p.stat().st_mtime
    p.write_text(json.dumps([9, 9, 9]))
    import os as _os

    _os.utime(str(p), (mtime, mtime))
    assert loader() == [1, 2, 3]


def test_mfile_cache_invalidates_on_mtime_change(tmp_path):
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"v": 1}))

    @cache_mod._mfile_cache(lambda: str(p), dict)
    def loader(data):
        return data

    assert loader() == {"v": 1}
    # Bump mtime explicitly.
    import os as _os

    new_mtime = p.stat().st_mtime + 10
    p.write_text(json.dumps({"v": 2}))
    _os.utime(str(p), (new_mtime, new_mtime))
    assert loader() == {"v": 2}


def test_mfile_cache_uses_default_when_missing(tmp_path):
    @cache_mod._mfile_cache(lambda: str(tmp_path / "missing.json"), lambda: ["d"])
    def loader(data):
        return data

    assert loader() == ["d"]


def test_mfile_cache_registers_in_global_registry(tmp_path):
    before = len(cache_mod._MFILE_CACHES)

    @cache_mod._mfile_cache(lambda: str(tmp_path / "x.json"), list)
    def loader(data):
        return data

    assert len(cache_mod._MFILE_CACHES) == before + 1


# ---------- @_mmfile_cache ----------


def test_mmfile_cache_caches_until_any_path_changes(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text("1")
    b.write_text("2")
    calls = {"n": 0}

    @cache_mod._mmfile_cache([lambda: str(a), lambda: str(b)])
    def derived():
        calls["n"] += 1
        return calls["n"]

    assert derived() == 1
    assert derived() == 1  # cached

    # Touch file b -> invalidate.
    import os as _os

    new_mtime = b.stat().st_mtime + 5
    _os.utime(str(b), (new_mtime, new_mtime))
    assert derived() == 2


def test_mmfile_cache_handles_missing_paths(tmp_path):
    @cache_mod._mmfile_cache([lambda: str(tmp_path / "nope.json")])
    def derived():
        return "ok"

    assert derived() == "ok"


# ---------- _register_cache & _cache_clear_all ----------


def test_register_cache_adds_to_hand_caches():
    before = len(cache_mod._HAND_CACHES)
    c = cache_mod._register_cache()
    assert isinstance(c, dict)
    assert c in cache_mod._HAND_CACHES
    assert len(cache_mod._HAND_CACHES) == before + 1


def test_cache_clear_all_clears_everything(monkeypatch, tmp_path):
    # Hand cache
    hc = cache_mod._register_cache()
    hc["k"] = "v"
    # mfile cache
    p = tmp_path / "x.json"
    p.write_text("[]")

    @cache_mod._mfile_cache(lambda: str(p), list)
    def loader(data):
        return data

    loader()  # populate
    # Find the mfile store registered for this loader.
    populated_stores = [s for s in cache_mod._MFILE_CACHES if "data" in s]
    assert populated_stores, "expected at least one populated mfile store"
    # Global TTL
    cache_mod._GLOBAL_CACHE.set("gk", 1, ttl=60)
    # st.cache_data.clear stub
    monkeypatch.setattr(
        cache_mod.st, "cache_data", type("X", (), {"clear": lambda: None})()
    )

    cache_mod._cache_clear_all()

    assert hc == {}
    for s in populated_stores:
        assert s == {}
    val, found = cache_mod._GLOBAL_CACHE.get("gk")
    assert found is False


def test_cache_clear_all_swallows_streamlit_exception(monkeypatch):
    class _Boom:
        def clear(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(cache_mod.st, "cache_data", _Boom())
    # Should not raise.
    cache_mod._cache_clear_all()
