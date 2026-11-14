"""dead_code_checker.safepath: a program the tool starts by name is found in the absolute entries of PATH only. A relative entry (`.`, `bin`,
an empty one) would be the working directory's, often the repository that is being analysed, so a `git`, `ssh`, `bwrap`, `dot`, `gitleaks` or
`cp` planted in it must never run.

python3 -m pytest -q tests/test_graph_safepath.py

Nothing here reaches the network. The planted programs are shell scripts that append their name to a marker file; a test fails when the
marker exists. ssh is never started: the argv that would start it is recorded.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from graph_support import new_repo

from dead_code_checker import core, safepath
from dead_code_checker.graph import (
    cli,
    engines,
    fixverify,
    proc,
    publish,
    rtcfg,
    rttransport,
    runtime,
    sandbox,
    snippets,
    vcs,
    viz,
)

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="shell scripts stand in for the planted programs"),
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.usefixtures("hermetic_git", "data_root"),
]

PLANTED = ("git", "ssh", "bwrap", "unshare", "dot", "gitleaks", "cp", "go", "true")


def plant(directory: Path, marker: Path, names=PLANTED) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        f = directory / name
        f.write_text(f"#!/bin/sh\necho {name} >> {marker}\nexit 0\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def real_path():
    return os.environ.get("PATH", os.defpath)


# ================================================================ the helper
def test_only_the_absolute_entries_of_path_are_used(tmp_path, monkeypatch):
    good = tmp_path / "bin"
    good.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "bin", "../x", str(good), "relative/dir"]))
    assert safepath.path_dirs() == [str(good)] and safepath.safe_path() == str(good)
    monkeypatch.setenv("PATH", ".")
    assert safepath.path_dirs() == [] and safepath.safe_path() == ""
    monkeypatch.delenv("PATH")
    assert safepath.path_dirs() == [] and safepath.safe_path() == os.defpath  # no PATH at all: the system default, as the OS would


def test_which_never_answers_with_a_file_of_the_working_directory(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    plant(tmp_path / "work", marker, ("tool",))
    monkeypatch.chdir(tmp_path / "work")
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(tmp_path / "work")[len(str(tmp_path)) + 1 :]]))
    assert safepath.which("tool") == ""
    assert safepath.which("./tool") == "" and safepath.which("tool/") == ""  # a relative path is not a program name
    assert safepath.which(str(tmp_path / "work" / "tool")) == str(tmp_path / "work" / "tool")  # an absolute path is the caller's choice
    assert safepath.which("") == "" and not marker.exists()
    monkeypatch.setenv("PATH", str(tmp_path / "work"))  # the same directory, absolute: found
    assert safepath.which("tool") == str(tmp_path / "work" / "tool")


def test_argv_resolves_a_bare_name_and_refuses_a_program_that_is_not_there(tmp_path, monkeypatch):
    plant(tmp_path / "bin", tmp_path / "ran", ("tool",))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert safepath.argv(["tool", "-x"]) == [str(tmp_path / "bin" / "tool"), "-x"]
    assert safepath.argv(["/abs/prog", "a"]) == ["/abs/prog", "a"] and safepath.argv([]) == []
    with pytest.raises(FileNotFoundError):  # an OSError: every caller already treats it as "could not start the program"
        safepath.argv(["absent-tool"])
    monkeypatch.chdir(tmp_path / "bin")
    monkeypatch.setenv("PATH", ".")
    with pytest.raises(FileNotFoundError):
        safepath.argv(["tool"])


def test_the_child_gets_a_path_of_absolute_entries(tmp_path, monkeypatch, real_path):
    seen = []

    def fake_run(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", real_path, "bin"]))
    safepath.run(["git", "--version"])
    argv, kw = seen[-1]
    assert os.path.isabs(argv[0]) and kw["env"]["PATH"] == safepath.safe_path() and "." not in kw["env"]["PATH"].split(os.pathsep)
    safepath.run(["git", "--version"], env={"HOME": "/h", "PATH": ".:/usr/bin:bin"})  # a given environment: only its PATH is filtered
    assert seen[-1][1]["env"] == {"HOME": "/h", "PATH": "/usr/bin"}
    safepath.run(["git", "--version"], env={"HOME": "/h"})  # and one without a PATH gets none
    assert seen[-1][1]["env"] == {"HOME": "/h"}


# ================================================================ the programs the tool starts, with `.` on PATH and a planted program in the repository
@pytest.fixture
def scene(tmp_path, monkeypatch, real_path):
    """A repository that holds planted git, ssh, bwrap, ... and PATH with `.` and `bin` in front, the working directory inside it."""
    repo = new_repo(tmp_path, {"a.py": "def f():\n    return 1\n", "b.py": "x = 1\n"})
    marker = tmp_path / "ran"
    plant(repo, marker)
    plant(repo / "bin", marker)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "bin", real_path]))
    monkeypatch.setattr(sandbox, "_MODE", [])
    monkeypatch.setattr(proc, "_NETLESS", [])
    monkeypatch.setattr(viz, "_NETLESS", {})
    monkeypatch.delenv("DEAD_CODE_CHECKER_DOT", raising=False)
    monkeypatch.delenv(snippets.ENV_GITLEAKS, raising=False)
    engines.reset()
    return repo, marker


def test_a_planted_git_is_never_run_by_the_text_checker(scene):
    repo, marker = scene
    root = str(repo)
    assert core._git(root, "rev-parse", "--is-inside-work-tree") == (0, b"true\n")  # the real git answered, not the planted one
    assert core.toplevel(root) == root
    assert core._cat_batch(root, ["HEAD:a.py"])["HEAD:a.py"] == b"def f():\n    return 1\n"
    assert not marker.exists()


def test_a_planted_git_is_never_run_by_the_graph_checker(scene):
    repo, marker = scene
    root = str(repo)
    head = vcs.head_commit(root)
    assert head and len(head) >= 40
    assert [e.path for e in vcs.ls_files(root)] == ["a.py", "b.py"] and vcs.git_ok(root)
    blobs = vcs.cat_batch(root, [vcs.blob_sha(b"x = 1\n")])
    assert list(blobs.values()) == [b"x = 1\n"]  # the batch process was the real git
    assert "a.py" in engines.project_files(root)
    assert "a.py" in fixverify.Host().tracked(root)
    assert publish._git(root, "rev-parse", "HEAD") == head
    assert not marker.exists()


def test_a_planted_git_is_never_run_by_a_whole_check(scene, capsys):
    repo, marker = scene
    assert cli.main(["check", "--scope", "repo", "--root", str(repo), "--no-viz", "--show-suspects"]) in (0, 1)
    assert "dead-code-checker:" in capsys.readouterr().out
    assert not marker.exists()


def test_planted_bwrap_unshare_dot_gitleaks_cp_and_go_are_never_found_or_run(scene):
    repo, marker = scene
    mode = sandbox.detect(refresh=True)
    assert all(not str(p).startswith(str(repo)) for p in mode.prefix) and mode.kind in ("bwrap", "netless", "none")
    assert all(not str(p).startswith(str(repo)) for p in proc.netless_prefix())
    assert all(not str(p).startswith(str(repo)) for p in viz.netless_prefix())
    dot = viz.dot_path()
    assert dot is None or (os.path.isabs(dot) and not dot.startswith(str(repo)))
    leaks = snippets.find_gitleaks()
    assert leaks is None or (os.path.isabs(leaks) and not leaks.startswith(str(repo)))
    for name in ("cp", "go"):
        got = fixverify.Host().resolve(name)
        assert got == "" or (os.path.isabs(got) and not got.startswith(str(repo)))
    assert engines.search_dirs() == [d for d in engines.search_dirs() if os.path.isabs(d) and d != str(repo)]
    assert not marker.exists()


def test_ssh_is_found_and_started_by_an_absolute_path_with_a_clean_path(scene, monkeypatch):
    repo, marker = scene
    assert safepath.which("ssh") != str(repo / "ssh")
    started = []

    def fake_run(argv, **kw):
        started.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, 255, b"", b"no route\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    d = rtcfg.data()
    tr = rttransport.SshTransport(rttransport.Target("target.invalid"), d["timeouts"], d["result"]["max_bytes"])
    assert tr._run is safepath.run  # the default seams are the safe ones
    ok, why = tr.reachable()
    assert not ok and started
    argv, kw = started[0]
    ssh = safepath.which("ssh")
    if ssh:
        assert argv[0] == ssh and not argv[0].startswith(str(repo))
    assert "." not in kw["env"]["PATH"].split(os.pathsep) and all(os.path.isabs(x) for x in kw["env"]["PATH"].split(os.pathsep) if x)
    assert not marker.exists()


def test_a_missing_ssh_is_a_reason_even_when_one_is_planted_in_the_working_directory(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    work = tmp_path / "work"
    plant(work, marker, ("ssh",))
    monkeypatch.chdir(work)
    monkeypatch.setenv("PATH", ".")  # nothing absolute: there is no ssh for the tool
    t, why = runtime.target_for(rtcfg.RuntimeCfg(True, "target.invalid"), {rtcfg.ENV_HOSTS: "target.invalid"})
    assert t is None and "ssh is missing" in why and not marker.exists()


def test_the_text_checker_without_a_usable_git_says_so_and_runs_nothing_from_the_tree(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    work = tmp_path / "work"
    plant(work, marker, ("git",))
    monkeypatch.chdir(work)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "bin"]))
    assert core._git(str(work), "status") == (127, b"")  # git cannot be started: the same answer as a missing git
    assert core.toplevel(str(work)) is None and not marker.exists()
