"""The fake channel of the tests of the runtime-signal mode, controlling half (graph.runtime, graph.rttransport). Nothing here starts a process
but git: "ssh" is a function call over a directory.

    FakeTransport  the transport of runtime.run(): put/run/pull/cleanup work on a directory of the test the way the four remote commands of
                   graph.rttransport do (the same members taken out of what is put, the hashes of the tool's own files checked before the box
                   starts), and run() is graph.rtbox.box() on that directory with the FakeBox of tests/graph_fake_target.py: the same code that
                   runs on a real target, minus the process
    make_repo      a synthetic git repository of a few files (python, javascript, go), committed
    RuntimeJob, RuntimeResult   the real types of graph.fixverify, re-exported
The module has a unique name on purpose: a test must not `from conftest import`.
"""

import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import graph_fake_target as F
from graph_support import git, write

from dead_code_checker.graph import rtbox, rttransport, vcs
from dead_code_checker.graph.fixverify import RuntimeJob, RuntimeResult  # noqa: F401  (re-exported for the tests of the target)


def make_repo(path, files: Optional[Dict[str, str]] = None, exe=("app.py",)) -> Path:
    """A git repository of synthetic files, committed. The files in `exe` get the exec bit."""
    r = Path(path)
    r.mkdir(parents=True, exist_ok=True)
    git(r, "init", "-q")
    for rel, text in (F.FILES if files is None else files).items():
        write(r, rel, text, 0o755 if rel in exe else None)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    return r


def tool_hashes() -> Dict[str, str]:
    out = {}
    for name, path in rtbox.delivery_files().items():
        with open(path, "rb") as fh:
            out[name] = hashlib.sha256(fh.read()).hexdigest()
    return out


class FakeTransport:
    """The ssh channel of runtime.run() as function calls over a directory. `box` is the FakeBox the target uses; `tools` its sandbox."""

    def __init__(
        self,
        base,
        box: Optional[F.FakeBox] = None,
        tools=F.FakeTools,
        label="tester@target.invalid",
        put_rc=0,
        run_rc=None,
        tamper=None,
    ):
        self.base, self.box, self.tools, self.label = Path(base), box or F.FakeBox(), tools, label
        self.name = label
        self.put_rc, self.run_rc, self.tamper = put_rc, run_rc, tamper
        self.log: List[str] = []
        self.sent: Dict[str, bytes] = {}
        self.base.mkdir(mode=0o700, exist_ok=True)

    def dir(self, run_id) -> Path:
        return self.base / f"rt-{run_id}"

    def reachable(self) -> Tuple[bool, str]:
        self.log.append("reachable")
        return True, "ok"

    def put(self, run_id, data):
        self.log.append("put")
        if self.put_rc:
            return self.put_rc, "recorded put failure"
        d = self.dir(run_id)
        d.mkdir(mode=0o700)
        for n in ("out", "tmp", "data"):
            (d / n).mkdir(mode=0o700)
        self.sent[run_id] = data
        with tarfile.open(
            fileobj=io.BytesIO(data), mode="r:"
        ) as tf:  # the members the put command names, and nothing else
            for m in tf.getmembers():
                if m.name in rttransport.PUT_MEMBERS and m.isreg():
                    dest = d / m.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(tf.extractfile(m).read())
        return 0, ""

    def run(self, run_id, sha256, wait_s, tool=None):
        self.log.append("run")
        if self.run_rc is not None:
            return self.run_rc, "recorded run failure"
        d = self.dir(run_id)
        for name, want in (tool or {}).items():  # the check the run command makes before it starts the box
            if hashlib.sha256((d / "box" / name).read_bytes()).hexdigest() != want:
                return 1, f"dead-code-checker: {name} on the target is not the file that was sent"
        try:
            return rtbox.box(str(d), sha256, on_target=True, starter=self.box, tools=self.tools), "ok"
        except rtbox.BoxError as e:
            return 2, str(e)

    def pull(self, run_id):
        self.log.append("pull")
        out = self.dir(run_id) / "out"
        if self.tamper:
            self.tamper(out)
        if not all((out / n).is_file() for n in rttransport.DELIVERY):
            return None, "pull failed (rc 2): tar: manifest.json: Cannot stat: No such file or directory"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:") as tf:
            for n in rttransport.DELIVERY:
                tf.add(out / n, arcname=n)
        return buf.getvalue(), ""

    def cleanup(self, run_id):
        import shutil

        self.log.append("cleanup")
        shutil.rmtree(self.dir(run_id), ignore_errors=True)


def write_cfg(repo, text):
    (Path(repo) / ".dead-code-checker.toml").write_text(text)


class Guard:
    """What the code under test started: every Popen (subprocess.run included) and every os-level way to run a program. subprocess itself
    may use os.posix_spawn for a program given by an absolute path; that is allowed only inside a Popen that passed the allow-list."""

    def __init__(self, monkeypatch, allow=("git",)):
        self.started, self.allow, self.inside = [], allow, 0
        real, guard = subprocess.Popen, self

        class Popen(real):
            def __init__(self, args, *a, **k):
                exe = os.path.basename(args if isinstance(args, str) else args[0])
                guard.started.append(exe)
                if exe not in guard.allow:
                    raise AssertionError(f"a process was started on this machine: {args!r}")
                guard.inside += 1
                try:
                    super().__init__(args, *a, **k)
                finally:
                    guard.inside -= 1

        monkeypatch.setattr(subprocess, "Popen", Popen)
        for name in (
            "system",
            "popen",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "posix_spawn",
            "posix_spawnp",
            "spawnv",
            "spawnve",
            "fork",
        ):
            if hasattr(os, name):
                real_fn = getattr(os, name)

                def blocked(*a, _n=name, _real=real_fn, **k):
                    if _n.startswith("posix_spawn") and guard.inside:
                        return _real(*a, **k)  # subprocess starting an allowed program the posix_spawn way
                    raise AssertionError(f"os.{_n} called")

                monkeypatch.setattr(os, name, blocked)


def stored_doc(repo, rows, files=None):
    commit = vcs.head_commit(str(repo))
    files = (
        files if files is not None else {f: vcs.blob_sha((repo / f).read_bytes()) for f in {r["file"] for r in rows}}
    )
    return {
        "schema": 1,
        "run_id": F.RUN_ID,
        "commit": commit,
        "target": "tester@target.invalid",
        "status": "ok",
        "notes": [],
        "runs": [],
        "files": files,
        "executed": rows,
    }


def row(file, line, qual="f", lang="python"):
    return {"lang": lang, "file": file, "qual": qual, "line": line, "end": 0, "hit": 1}
