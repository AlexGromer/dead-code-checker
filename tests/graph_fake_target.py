"""A fake execution target for the tests of the runtime-signal mode (graph.rtbox). Nothing here starts a process.

    FakeBox        the `starter` argument of rtbox.box(): records every command line the box would have started (bwrap, prlimit and the
                   command) and EXECUTES NOTHING; it writes the files the tracers would have written (recorded formats) and answers
                   the report command (`go tool cover -func`) with recorded text
    FakeTools      rtbox.Tools of a host that has a bubblewrap and a prlimit (never started: FakeBox is the only caller)
    fixtures       a few synthetic project files (python, javascript, go), the recorded tracer outputs
    never          a real project, a real ssh, a real test run: the project is a few lines the test wrote
The module has a unique name on purpose: a test must not `from conftest import`.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import List

RUN_ID = "0123456789abcdef0123456789abcdef"

PY_A = '''"""pkg.a"""


def used():
    return 1


def never():
    return 2


class Klass:
    def meth(self):
        return 3

    def idle(self):
        return 4


def helper():
    return used() + 1
'''
PY_MAIN = """#!/usr/bin/env python3
import pkg.a


def main():
    return pkg.a.helper()


main()
"""
JS_APP = """function used() {\n  return 1;\n}\n\nfunction unused() {\n  return 2;\n}\nmodule.exports = { used };\n"""
GO_A = "package pkg\n\nfunc Used() int {\n\treturn 1\n}\n\nfunc Unused() int {\n\treturn 2\n}\n"
GO_MOD = "module example.com/m\n\ngo 1.21\n"
FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": PY_A,
    "app.py": PY_MAIN,
    "tests/test_a.py": "import pkg.a\n\n\ndef test_used():\n    assert pkg.a.used() == 1\n",
    "web/app.js": JS_APP,
    "go.mod": GO_MOD,
    "svc/a.go": GO_A,
}


def line_of(text: str, needle: str) -> int:
    return text[: text.index(needle)].count("\n") + 1


# ------------------------------------------------------------------ recorded tracer output
def py_trace(rows=None) -> dict:
    return {
        "v": 1,
        "fn": (
            rows
            if rows is not None
            else [
                ["pkg/a.py", "used", line_of(PY_A, "def used")],
                ["pkg/a.py", "Klass.meth", line_of(PY_A, "def meth")],
                ["app.py", "main", line_of(PY_MAIN, "def main")],
                ["not/tracked.py", "x", 1],  # a file the file list does not have: dropped by the reader
            ]
        ),
    }


def v8_doc(tree: str, text: str = JS_APP, path: str = "web/app.js") -> dict:
    a, b = text.index("function used"), text.index("function unused")
    c = text.index("module.exports")

    def fn(name, s, e, n):
        return {"functionName": name, "ranges": [{"startOffset": s, "endOffset": e, "count": n}], "isBlockCoverage": True}

    return {
        "result": [
            {
                "scriptId": "1",
                "url": f"file://{tree}/{path}",
                "functions": [fn("", 0, len(text), 1), fn("used", a, b - 2, 3), fn("unused", b, c, 0)],
            },
            {"scriptId": "2", "url": "node:internal/modules/cjs/loader", "functions": [fn("load", 0, 99, 5)]},
        ]
    }


GO_FUNC = (
    "example.com/m/svc/a.go:3:\tUsed\t100.0%\nexample.com/m/svc/a.go:7:\tUnused\t0.0%\ntotal:\t(statements)\t50.0%\n"
)


class FakeTools:
    """rtbox.Tools: a bubblewrap and a prlimit that exist (never started: FakeBox is the only caller)."""

    prlimit = "/usr/bin/prlimit"
    bwrap = "/usr/bin/bwrap"
    bwrap_note = ""


class NoBwrap(FakeTools):
    bwrap = ""
    bwrap_note = "bwrap at /usr/bin/bwrap cannot create namespaces here (recorded reason)"


def split_box(argv: List[str]):
    """(setenv dict, prlimit flags, the command) of the command line rtbox._exec builds: bwrap ... -- prlimit FLAGS -- COMMAND."""
    i = argv.index("--")
    head, rest = argv[:i], argv[i + 1 :]
    env = {head[j + 1]: head[j + 2] for j, a in enumerate(head) if a == "--setenv"}
    k = rest.index("--")
    return env, rest[1:k], rest[k + 1 :]


class FakeBox:
    """Records and answers; starts nothing. `py`, `v8`, `go` switch off a tracer's output ("" = write nothing)."""

    def __init__(self, rc=0, go_rc=0, py=None, v8=True, go=GO_FUNC, timeout_main=False, out=b"", err=b""):
        self.rc, self.go_rc, self.py, self.v8, self.go, self.timeout_main = rc, go_rc, py, v8, go, timeout_main
        self.out, self.err = out or b"recorded stdout that nobody asked for", err or b"recorded stderr"
        self.calls: List[dict] = []

    def __call__(self, argv, cwd, timeout, env):
        senv, flags, cmd = split_box(list(argv))
        self.calls.append(
            {"argv": list(argv), "env": senv, "flags": flags, "cmd": cmd, "cwd": cwd, "timeout": timeout, "outer_env": dict(env or {})}
        )
        if cmd[:3] == ["go", "tool", "cover"]:
            return self.go_rc, self.go.encode(), b""
        if self.timeout_main:
            raise subprocess.TimeoutExpired(argv, timeout)
        if "DCRT_OUT" in senv and self.py != "":
            Path(senv["DCRT_OUT"], "4242.json").write_text(json.dumps(self.py or py_trace()), encoding="utf-8")
        if "NODE_V8_COVERAGE" in senv and self.v8:
            Path(senv["NODE_V8_COVERAGE"], "coverage-4242-1-0.json").write_text(
                json.dumps(v8_doc(os.path.realpath(cwd))), encoding="utf-8"
            )
        return self.rc, self.out, self.err
