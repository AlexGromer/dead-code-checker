import os
import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# subprocess tests run `python -P -m dead_code_checker` from a fixture directory: the package must be importable there
os.environ["PYTHONPATH"] = ROOT + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """The data root of a graph run is a directory of the test: nothing is written into the tool directory."""
    d = tmp_path / "dcc-data"
    d.mkdir()
    monkeypatch.setenv("DEAD_CODE_CHECKER_HOME", str(d))
    return d


@pytest.fixture
def hermetic_git(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def no_tree_sitter(monkeypatch):
    """The machine has no tree-sitter (the optional extra), whatever this one has: `import tree_sitter` fails, nothing is cached."""
    from dead_code_checker.graph import ts

    monkeypatch.setitem(sys.modules, "tree_sitter", None)
    ts.reset()
    yield
    monkeypatch.undo()
    ts.reset()


@pytest.fixture
def dcref(tmp_path, monkeypatch):
    """The toy language "dcref" and its root extractor "dcroots" (tests/graph_support.py), registered for this test."""
    from graph_support import install_dcref

    install_dcref(monkeypatch, tmp_path)


@pytest.fixture
def registry_restored():
    """A test that calls contracts.reset_registry() leaves the registry as it found it: the same extractor and root extractor objects,
    so the instances that other test modules hold (and the files that run after this one) see the registry they imported."""
    from dead_code_checker.graph import contracts

    extractors, roots = dict(contracts._EXTRACTORS), dict(contracts._ROOTS)
    yield
    contracts._EXTRACTORS.clear()
    contracts._EXTRACTORS.update(extractors)
    contracts._ROOTS.clear()
    contracts._ROOTS.update(roots)
