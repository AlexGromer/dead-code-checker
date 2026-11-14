"""dead_code_checker.graph.paths: the data layout, the fallback chain, the repository id.

python3 -m pytest -q tests/test_graph_paths.py
"""

import importlib.util
import os
import re
import shutil
from pathlib import Path

import pytest

from dead_code_checker.graph import paths

ROOT = Path(__file__).resolve().parent.parent
needs_user = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes")


def repo(parent, name="app", git=True):
    r = Path(parent) / name
    r.mkdir(parents=True)
    if git:
        (r / ".git").mkdir()
    return r


def tool(tmp_path):
    t = tmp_path / "tool"
    t.mkdir()
    return t


def test_repo_id_is_name_and_hash_of_the_real_path(tmp_path):
    a = repo(tmp_path / "one")
    assert re.fullmatch(r"app-[0-9a-f]{8}", paths.repo_id(a))
    assert paths.repo_id(a) == paths.repo_id(str(a)) == paths.repo_id(a / "." / ".." / "app")


def test_two_repos_with_the_same_name_get_different_directories(tmp_path):
    a, b = repo(tmp_path / "one"), repo(tmp_path / "two")
    t = tool(tmp_path)
    la, lb = paths.resolve(a, environ={}, tool=t), paths.resolve(b, environ={}, tool=t)
    assert la.repo_id != lb.repo_id and la.repo_id.startswith("app-") and lb.repo_id.startswith("app-")
    assert la.project != lb.project and la.project.parent == lb.project.parent


def test_symlink_to_a_repo_is_the_same_repo(tmp_path):
    a = repo(tmp_path, "real")
    link = tmp_path / "alias"
    link.symlink_to(a)
    assert paths.repo_id(link) == paths.repo_id(a)


def test_repo_id_name_is_sanitised_and_never_empty(tmp_path):
    odd = repo(tmp_path, "my app (v2)!")
    assert re.fullmatch(r"my_app_v2-[0-9a-f]{8}", paths.repo_id(odd))
    assert re.fullmatch(r"[A-Za-z0-9._-]+", paths.repo_id(repo(tmp_path, "é ü")))
    assert re.fullmatch(r"repo-[0-9a-f]{8}", paths.repo_id(repo(tmp_path, "...")))
    assert len(paths.repo_id(repo(tmp_path, "x" * 200))) == 40 + 1 + 8


def test_find_root_walks_up_to_the_git_entry(tmp_path):
    r = repo(tmp_path)
    deep = r / "a" / "b"
    deep.mkdir(parents=True)
    assert paths.find_root(deep) == r.resolve()
    (tmp_path / "plain").mkdir()
    assert paths.find_root(tmp_path / "plain") == (tmp_path / "plain").resolve()


def test_find_root_accepts_a_worktree_git_file(tmp_path):
    r = repo(tmp_path, "wt", git=False)
    (r / ".git").write_text("gitdir: elsewhere\n")
    sub = r / "src"
    sub.mkdir()
    assert paths.find_root(sub) == r.resolve()


def test_tool_dir_is_this_checkout():
    assert paths.tool_dir() == ROOT


def test_tool_dir_is_none_in_site_packages(tmp_path):
    pkg = tmp_path / "site-packages" / "dead_code_checker" / "graph"
    pkg.mkdir(parents=True)
    shutil.copy(ROOT / "dead_code_checker" / "graph" / "paths.py", pkg / "paths.py")
    spec = importlib.util.spec_from_file_location("site_paths", pkg / "paths.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.tool_dir() is None


def test_data_goes_next_to_the_tool(tmp_path):
    t, r = tool(tmp_path), repo(tmp_path / "w")
    lay = paths.resolve(r, environ={}, tool=t)
    assert lay.source == "tool" and lay.warning == "" and lay.out is None
    assert lay.base == t / "data" and lay.project == t / "data" / "projects" / lay.repo_id
    assert lay.runs == lay.project / "runs"
    assert lay.store == lay.project / "store.sqlite" and lay.ledger == lay.project / "ledger.jsonl"
    assert lay.index == t / "data" / "index.html" and lay.root == r.resolve()


def test_resolve_creates_nothing_and_ensure_creates_the_two_directories(tmp_path):
    t, r = tool(tmp_path), repo(tmp_path / "w")
    lay = paths.resolve(r, environ={}, tool=t)
    assert not (t / "data").exists()
    paths.ensure(lay)
    assert lay.project.is_dir() and lay.runs.is_dir() and not lay.store.exists()
    paths.ensure(lay)  # twice is fine


@needs_user
def test_read_only_tool_dir_falls_back_to_the_state_dir_with_one_warning(tmp_path):
    t, r = tool(tmp_path), repo(tmp_path / "w")
    t.chmod(0o555)
    try:
        lay = paths.resolve(r, environ={"XDG_STATE_HOME": str(tmp_path / "state")}, tool=t)
    finally:
        t.chmod(0o755)
    assert lay.source == "state" and lay.base == tmp_path / "state" / "dead-code-checker"
    assert "not writable" in lay.warning and "\n" not in lay.warning and paths.ENV_HOME in lay.warning
    assert not (t / "data").exists()


def test_a_file_where_the_data_directory_should_be_falls_back(tmp_path):
    t, r = tool(tmp_path), repo(tmp_path / "w")
    (t / "data").write_text("not a directory")
    lay = paths.resolve(r, environ={"XDG_STATE_HOME": str(tmp_path / "s")}, tool=t)
    assert lay.source == "state" and lay.warning.count("\n") == 0 and "not writable" in lay.warning


def test_no_tool_dir_means_the_state_dir_and_the_warning_says_why(tmp_path):
    lay = paths.resolve(repo(tmp_path), environ={"XDG_STATE_HOME": str(tmp_path / "s")}, tool=None)
    assert lay.source == "state" and "not in a source checkout" in lay.warning


def test_tool_data_inside_the_analysed_repo_is_not_used(tmp_path):
    r = repo(tmp_path)
    lay = paths.resolve(r, environ={"XDG_STATE_HOME": str(tmp_path / "s")}, tool=r)
    assert lay.source == "state" and "inside the analysed repository" in lay.warning
    assert not any(str(p).startswith(str(r) + os.sep) for p in (lay.base, lay.project, lay.runs, lay.store, lay.index))


def test_state_dir_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert paths.state_home({}) == tmp_path / "home" / ".local" / "state" / "dead-code-checker"
    assert paths.state_home({"XDG_STATE_HOME": "/x"}, nt=False) == Path("/x/dead-code-checker")
    assert paths.state_home({"LOCALAPPDATA": "/L"}, nt=True) == Path("/L/dead-code-checker")


def test_explicit_home_wins_without_a_warning(tmp_path):
    t, r, h = tool(tmp_path), repo(tmp_path / "w"), tmp_path / "chosen"
    lay = paths.resolve(r, environ={paths.ENV_HOME: str(h)}, tool=t)
    assert lay.source == "env" and lay.base == h and lay.warning == ""
    assert lay.project == h / "projects" / lay.repo_id


def test_explicit_home_that_cannot_be_used_is_an_error_not_a_fallback(tmp_path):
    t, r = tool(tmp_path), repo(tmp_path / "w")
    (tmp_path / "file").write_text("x")
    with pytest.raises(paths.DataDirError, match="not writable"):
        paths.resolve(r, environ={paths.ENV_HOME: str(tmp_path / "file")}, tool=t)
    with pytest.raises(paths.DataDirError, match="inside the analysed repository"):
        paths.resolve(r, environ={paths.ENV_HOME: str(r / "d")}, tool=t)


def test_nothing_usable_is_an_error(tmp_path):
    (tmp_path / "file").write_text("x")
    with pytest.raises(paths.DataDirError, match="no usable data directory"):
        paths.resolve(repo(tmp_path), environ={"XDG_STATE_HOME": str(tmp_path / "file")}, tool=None)


def test_out_moves_only_the_runs(tmp_path):
    t, r, o = tool(tmp_path), repo(tmp_path / "w"), tmp_path / "artifacts"
    lay = paths.resolve(r, out=o, environ={}, tool=t)
    assert lay.runs == o and lay.out == o
    assert (
        lay.store.parent == lay.project and lay.ledger.parent == lay.project and lay.project.parent.parent == t / "data"
    )


def test_out_inside_the_repository_is_refused(tmp_path):
    t, r = tool(tmp_path), repo(tmp_path / "w")
    with pytest.raises(paths.DataDirError, match="inside the analysed repository"):
        paths.resolve(r, out=r / "build", environ={}, tool=t)
    with pytest.raises(paths.DataDirError):
        paths.resolve(r, out=r, environ={}, tool=t)


def test_writable_probe(tmp_path):
    assert paths.writable(tmp_path) and paths.writable(tmp_path / "a" / "b" / "c")
    f = tmp_path / "f"
    f.write_text("x")
    assert not paths.writable(f) and not paths.writable(f / "sub")


def test_ensure_turns_a_directory_that_cannot_be_created_into_a_data_dir_error(tmp_path):
    t, r = tool(tmp_path), repo(tmp_path / "w")
    lay = paths.resolve(r, environ={}, tool=t)
    (t / "data").mkdir()
    (t / "data" / "projects").write_text("a file where a directory must go")
    with pytest.raises(paths.DataDirError, match="cannot create the data directory"):
        paths.ensure(lay)
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    other = tmp_path / "other"
    other.mkdir()
    lay = paths.resolve(r, out=blocker / "sub", environ={}, tool=tool(other))
    with pytest.raises(paths.DataDirError):
        paths.ensure(lay)
