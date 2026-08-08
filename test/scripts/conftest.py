"""Test setup for scripts/ — register a `scripts` package alias.

Several scripts import siblings via ``from scripts.memory_ingest import …``.
Pyproject's pythonpath puts ``scripts/`` directly on sys.path so each script
is importable by bare name, but that does not register a ``scripts`` package.
We synthesize one here so the ``from scripts.X`` form resolves.
"""

from __future__ import annotations

import hashlib
import math
import sys
import types
from pathlib import Path

import pytest


def _install_scripts_package_alias() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    scripts_dir = repo_root / "scripts"
    if not scripts_dir.is_dir():
        return
    if "scripts" not in sys.modules:
        pkg = types.ModuleType("scripts")
        pkg.__path__ = [str(scripts_dir)]  # type: ignore[attr-defined]
        sys.modules["scripts"] = pkg
    # Eagerly register every scripts/*.py as `scripts.<name>` so any
    # `from scripts.X import …` form resolves without the meta-path importer
    # walking our synthetic package. Auto-discovered so adding a new script
    # never requires editing this file.
    import importlib

    for py in scripts_dir.glob("*.py"):
        name = py.stem
        if name.startswith("_") or f"scripts.{name}" in sys.modules:
            continue
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        sys.modules[f"scripts.{name}"] = mod


_install_scripts_package_alias()


@pytest.fixture
def stub_embeddings(monkeypatch):
    """Replace fastembed with a deterministic hash-based stand-in.

    Real embedding would download ~130 MB of ONNX weights on first use, which
    no test should ever trigger. The vectors are content-derived, so identical
    text still produces identical vectors and similarity ordering stays stable
    across runs — enough for the retrieval assertions here.

    Vectors are L2-normalised to match real bge output, so cosine distances
    stay in the same 0..2 range the production scoring path assumes.

    Returns the memory_store module so tests can reach its constants.
    """
    import memory_store as store

    def _fake_embed(texts):
        vectors = []
        for text in texts:
            digest = hashlib.sha256(str(text).encode("utf-8")).digest()
            raw = [digest[i % len(digest)] / 255.0 for i in range(store.EMBED_DIM)]
            norm = math.sqrt(sum(v * v for v in raw)) or 1.0
            vectors.append([v / norm for v in raw])
        return vectors

    monkeypatch.setattr(store, "embed_texts", _fake_embed)
    monkeypatch.setattr(store, "embed_query", lambda text: _fake_embed([text])[0])
    return store
