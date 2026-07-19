"""Shared fixtures for the build-pipeline unit tests.

build/build.py is a script, not a package, so it's loaded by file path
under a stable module name. Tests run fully offline: anything that would
touch Overpass or Overture is either monkeypatched or fed from a
temporary cache directory.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def build_mod():
    spec = importlib.util.spec_from_file_location(
        "foulflock_build", ROOT / "build" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["foulflock_build"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sandbox(build_mod, tmp_path, monkeypatch):
    """Redirect the module's cache and output dirs into tmp and reset
    refresh state, so tests never touch the real cache/ or web/data/."""
    monkeypatch.setattr(build_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(build_mod, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(build_mod, "REFRESH_SCOPES", set())
    return tmp_path
