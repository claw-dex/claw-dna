"""Test setup for scripts/ — register a `scripts` package alias.

Several scripts import siblings via ``from scripts.memory_ingest import …``.
Pyproject's pythonpath puts ``scripts/`` directly on sys.path so each script
is importable by bare name, but that does not register a ``scripts`` package.
We synthesize one here so the ``from scripts.X`` form resolves.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path


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
