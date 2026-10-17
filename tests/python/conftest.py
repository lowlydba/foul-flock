"""Shared fixtures for the build-pipeline unit tests.

The pipeline is the build/foulflock package. Tests address it as one
namespace, `build_mod`: reading a name returns it from the module that
defines it, and monkeypatching a name sets it on that module, which is
where the pipeline's own cross-module calls look it up. Tests run fully
offline: anything that would touch Overpass or Overture is either
monkeypatched or fed from a temporary cache directory.
"""

import ast
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# build/ holds scripts, not a package; tests import its sibling modules.
sys.path.insert(0, str(ROOT / "build"))

PIPELINE_MODULES = ("config", "rulesfile", "overpass", "guidelines", "overture",
                    "matching", "viewcones", "artifacts", "cli")


def _top_level_names(module) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


class Pipeline:
    """Routes attribute reads and writes to the defining foulflock module.
    Names a module only imports (json, time, yaml) resolve to the first
    module that has them."""

    def __init__(self, modules):
        owners = {}
        for mod in modules:
            for name in _top_level_names(mod):
                owners[name] = mod
        object.__setattr__(self, "_modules", modules)
        object.__setattr__(self, "_owners", owners)

    def __getattr__(self, name):
        if name in self._owners:
            return getattr(self._owners[name], name)
        for mod in self._modules:
            if hasattr(mod, name):
                return getattr(mod, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name not in self._owners:
            raise AttributeError(f"{name} is not defined by the pipeline")
        setattr(self._owners[name], name, value)


@pytest.fixture(scope="session")
def build_mod():
    return Pipeline([importlib.import_module(f"foulflock.{m}")
                     for m in PIPELINE_MODULES])


@pytest.fixture()
def sandbox(build_mod, tmp_path, monkeypatch):
    """Redirect the module's cache and output dirs into tmp and reset
    refresh state, so tests never touch the real cache/ or web/data/."""
    monkeypatch.setattr(build_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(build_mod, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(build_mod, "REFRESH_SCOPES", set())
    return tmp_path
