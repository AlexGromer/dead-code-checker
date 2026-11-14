"""graph.sandbox: bubblewrap when it works, `unshare -rn` when only that does, else an unsandboxed run with a note.

python3 -m pytest -q tests/test_graph_sandbox.py

The three modes are decided on a host that is faked (which, subprocess.run); one test runs the real bubblewrap when this host has a
working one and is skipped, with the reason, when it has not.
"""

import shutil
import subprocess
from types import SimpleNamespace

import pytest

from dead_code_checker import safepath
from dead_code_checker.graph import proc, sandbox


@pytest.fixture(autouse=True)
def _restore():
    old = list(sandbox._MODE)
    sandbox._MODE.clear()
    proc._NETLESS.clear()
    yield
    sandbox._MODE.clear()
    sandbox._MODE.extend(old)
    proc._NETLESS.clear()


def fake_host(monkeypatch, have, works):
    """`have`: programs which() finds; `works`: argv[0] values whose probe exits 0."""
    monkeypatch.setattr(safepath, "which", lambda name: f"/fake/{name}" if name in have else "")
    seen = []

    def run(argv, **kw):
        seen.append(list(argv))
        return SimpleNamespace(returncode=0 if argv[0].rsplit("/", 1)[-1] in works else 1)

    monkeypatch.setattr(subprocess, "run", run)  # safepath.run starts the probes through it: argv[0] is the absolute path found
    return seen


def test_bubblewrap_is_used_when_the_probe_works(monkeypatch):
    seen = fake_host(monkeypatch, {"bwrap", "unshare"}, {"bwrap", "unshare"})
    mode = sandbox.detect()
    assert mode.kind == "bwrap" and mode.prefix == ("/fake/bwrap",) and mode.note == ""
    assert seen[0][0] == "/fake/bwrap" and "--unshare-net" in seen[0] and seen[0][-1] == "/usr/bin/true"
    assert sandbox.detect() is mode and len(seen) == 1  # probed once per process
    assert sandbox.detect(refresh=True) is not mode


def test_a_bubblewrap_that_is_installed_but_does_not_work_is_not_used(monkeypatch):
    fake_host(monkeypatch, {"bwrap", "unshare"}, {"unshare"})  # a container without user namespaces for bwrap
    mode = sandbox.detect()
    assert (
        mode.kind == "netless"
        and mode.prefix == ("/fake/unshare", "-rn")
        and "no network but without file-system isolation" in mode.note
    )


def test_without_either_the_engine_runs_unsandboxed_and_the_note_says_so(monkeypatch):
    fake_host(monkeypatch, set(), set())
    mode = sandbox.detect()
    assert mode == sandbox.Mode("none", (), sandbox.NOTE_NONE) and "unsandboxed" in mode.note
    assert sandbox.wrap(mode, ["tool", "-x"], "/repo", scratch=["/s"], ro=["/r"]) == ["tool", "-x"]


def test_netless_mode_only_cuts_the_network(monkeypatch):
    fake_host(monkeypatch, {"unshare"}, {"unshare"})
    mode = sandbox.detect()
    assert mode.kind == "netless" and sandbox.wrap(mode, ["tool", "a"], "/repo", scratch=["/s"]) == [
        "/fake/unshare",
        "-rn",
        "tool",
        "a",
    ]


def test_the_bubblewrap_command_line(monkeypatch):
    mode = sandbox.Mode("bwrap", ("/usr/bin/bwrap",), "")
    argv = sandbox.wrap(mode, ["tool", "-x"], "/repo/sub", scratch=["/tmp/s1", "/tmp/s2"], ro=["/tmp/r"])
    assert argv[0] == "/usr/bin/bwrap" and argv[-3:] == ["--", "tool", "-x"]
    assert argv[1:11] == sandbox.BWRAP_BASE  # no network, own pid space, the whole file system read-only
    assert ["--tmpfs", "/tmp"] == argv[11:13]
    assert argv[13:] == ["--ro-bind", "/tmp/r", "/tmp/r", "--bind", "/tmp/s1", "/tmp/s1", "--bind", "/tmp/s2", "/tmp/s2",
                         "--chdir", "/repo/sub", "--", "tool", "-x"]  # fmt: skip
    assert (
        "--unshare-net" in sandbox.BWRAP_BASE
        and "--ro-bind" in sandbox.BWRAP_BASE
        and "--die-with-parent" in sandbox.BWRAP_BASE
    )


def test_set_mode_fixes_the_mode(monkeypatch):
    fake_host(monkeypatch, {"bwrap"}, {"bwrap"})
    sandbox.set_mode(sandbox.Mode("none", (), ""))
    assert sandbox.detect().kind == "none"


REAL = shutil.which("bwrap") and sandbox._probe_bwrap()


@pytest.mark.skipif(not REAL, reason="no working bubblewrap on this host")
def test_the_real_bubblewrap_keeps_the_tree_read_only_and_the_scratch_writable(tmp_path):
    tree, scratch, outside = tmp_path / "tree", tmp_path / "scratch", tmp_path / "outside"
    for d in (tree, scratch, outside):
        d.mkdir()
    (tree / "a.txt").write_text("a")
    (outside / "x").write_text("x")
    mode = sandbox.detect(refresh=True)
    assert mode.kind == "bwrap"
    script = 'echo hurt > a.txt; echo "tree:$?"; echo ok > "$S/o.txt"; echo "scratch:$?"; ls "$O" 2>/dev/null | wc -l'
    argv = sandbox.wrap(mode, ["sh", "-c", script], str(tree), scratch=[str(scratch)], ro=[str(tree)])
    r = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "S": str(scratch), "O": str(outside)},
        timeout=60,
    )
    lines = r.stdout.split()
    assert (
        tree / "a.txt"
    ).read_text() == "a" and "tree:0" not in lines  # the write into the tree failed: it is read-only inside
    assert "scratch:0" in lines and (scratch / "o.txt").read_text() == "ok\n"
    assert lines[-1] == "0"  # /tmp is a fresh tmpfs: a directory of the host's /tmp that is not shared is not there
