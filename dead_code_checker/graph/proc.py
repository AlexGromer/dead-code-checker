"""graph.proc: how an engine process is started. Standard library only.

run_group(argv, cwd, timeout, env=None) -> (rc, stdout, stderr)   a child in its OWN process group; on a timeout or any error
                                      the whole group is killed (no grandchild of go, cargo or npm survives); raises
                                      subprocess.TimeoutExpired or OSError. A shell is never used.
kill_live() -> int                    SIGKILL to the groups that are running now (posix); how many were signalled
netless_prefix() -> list              the argv prefix of a network-less run (`unshare -rn`, else bubblewrap with --unshare-net),
                                      [] when this host offers neither; probed once per process
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import List, Optional, Sequence, Set, Tuple

from .. import safepath

_LIVE: Set[int] = set()
_NETLESS: List[List[str]] = []


def kill_live() -> int:
    n = 0
    for pid in sorted(_LIVE) if hasattr(os, "killpg") else ():
        try:
            os.killpg(pid, signal.SIGKILL)
            n += 1
        except OSError:
            pass
    return n


def run_group(argv: Sequence[str], cwd: str, timeout: float, env: Optional[dict] = None) -> Tuple[int, bytes, bytes]:
    posix = hasattr(os, "killpg")
    p = safepath.popen(  # a bare program name is resolved through the absolute entries of PATH only; the child gets such a PATH
        list(argv),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=posix,
    )
    _LIVE.add(p.pid)

    def kill_group() -> None:
        try:
            if posix:
                os.killpg(p.pid, signal.SIGKILL)
            else:
                p.kill()
        except OSError:
            pass

    try:
        try:
            out, err = p.communicate(timeout=timeout)
        except BaseException:
            kill_group()
            try:
                p.communicate(timeout=5)
            except Exception:  # noqa: BLE001 - the child is already killed; nothing else to do with it
                pass
            raise
        kill_group()  # stragglers of a finished tool (a daemon a go command left behind)
        return p.returncode, out, err
    finally:
        _LIVE.discard(p.pid)


def _works(argv: List[str]) -> bool:
    try:
        return (
            safepath.run([*argv, safepath.which("true") or "/usr/bin/true"], capture_output=True, timeout=10, stdin=subprocess.DEVNULL).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def netless_prefix() -> List[str]:
    """["unshare", "-rn"] when this host lets a user make a network-less namespace, else the bubblewrap form, else []."""
    if not _NETLESS:
        found: List[str] = []
        if os.name == "posix":
            unshare, bwrap = safepath.which("unshare"), safepath.which("bwrap")
            if unshare and _works([unshare, "-rn"]):
                found = [unshare, "-rn"]
            elif bwrap and _works([bwrap, "--unshare-net", "--dev-bind", "/", "/"]):
                found = [bwrap, "--unshare-net", "--dev-bind", "/", "/"]
        _NETLESS.append(found)
    return list(_NETLESS[0])
