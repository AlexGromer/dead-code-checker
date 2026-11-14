"""Stub engines for the tests of the engine layer: a directory of small shell scripts that print what the real tool printed.

    make_engine(bindir, "vulture", stdout="...", rc=3)      <bindir>/vulture, executable: prints stdout and stderr, exits with rc
    make_script(bindir, "scip-python", body)                any script body (it gets the arguments of the real command line)
    isolate(monkeypatch, tmp_path, bindir=None)             the engine setup of this process: only `bindir` (no PATH), no lock, no
                                                            bubblewrap (sandbox mode "none"); the previous setup is restored after
    block_network(monkeypatch)                              an inet socket call raises (a unix socket call goes through)

The scripts need /bin/sh: a test that uses them is skipped on Windows (SH).
"""

import os
import shlex
import socket
import stat
from pathlib import Path

import pytest

SH = pytest.mark.skipif(os.name == "nt", reason="the stub engines are shell scripts")


def make_script(bindir, name, body: str) -> Path:
    p = Path(bindir) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def make_engine(bindir, name, stdout: str = "", stderr: str = "", rc: int = 0, version: str = "") -> Path:
    """An engine that prints `stdout` and `stderr` and exits with `rc` whatever it is given; `--version` prints `version`."""
    body = ""
    if version:
        body += f'[ "$1" = "--version" ] && {{ echo {shlex.quote(version)}; exit 0; }}\n'
    if stdout:
        body += f"cat <<'DCC_EOF'\n{stdout}\nDCC_EOF\n"
    if stderr:
        body += f"cat >&2 <<'DCC_EOF'\n{stderr}\nDCC_EOF\n"
    return make_script(bindir, name, body + f"exit {rc}\n")


def isolate(monkeypatch, tmp_path, bindir=None):
    """Only `bindir` is searched (never the PATH of the host), no lock, no sandbox: the host's engines cannot change a result."""
    from dead_code_checker.graph import engines, sandbox

    monkeypatch.delenv(engines.ENV_LOCK, raising=False)
    monkeypatch.delenv(engines.ENV_DIR, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    engines.configure(None, str(bindir) if bindir else str(tmp_path / "no-engines"), path=False)
    engines._LOCKS.clear()
    old = list(sandbox._MODE)
    sandbox.set_mode(sandbox.Mode("none", (), ""))
    yield
    engines.reset()
    sandbox._MODE.clear()
    sandbox._MODE.extend(old)


def block_network(monkeypatch, names=("connect", "connect_ex", "bind", "sendto")) -> None:
    def guard(real):
        def call(self, *a, **k):
            if getattr(self, "family", None) == socket.AF_UNIX:
                return real(self, *a, **k)
            raise AssertionError("network access attempted")

        return call

    for name in names:
        monkeypatch.setattr(socket.socket, name, guard(getattr(socket.socket, name)))

    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
