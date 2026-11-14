"""graph.sandbox: where an engine that builds or indexes the project runs. Standard library only.

An engine that executes project code (a build script, a procedural macro, an indexer that type-checks the sources) should not be
able to write into the repository or reach the network. bubblewrap (`bwrap`) is optional:

    bwrap      present and working on this host: the engine sees the whole file system read-only, has no network, a fresh /tmp and
               /proc, and may write only into the scratch directories it is given; the repository is read-only for it.
    netless    no bwrap, but `unshare -rn` works: no network, nothing else is isolated.
    none       neither: the engine runs unsandboxed, as the user who started the checker, and `Mode.note` says so.

    detect() -> Mode(kind, prefix, note)       probed once per process; the note is "" for bwrap
    wrap(mode, argv, cwd, scratch, ro) -> argv  the command line to start: the prefix, the writable scratch directories (bwrap), argv
    bwrap_prefix(bwrap, dirs, env, ro, cwd)      the strict box of the local checks of `graph fix --verify` (graph.fixverify): no network,
                                                 a cleared environment, daemon sockets and the real home hidden, only `dirs` writable
    check_box() -> str                           the bubblewrap that can build that box on this host ("" = none: such a check is not run)

The checker never refuses to run an engine for lack of a sandbox: the note travels with the result (W dc.sandbox-weak, once per run).
"""

from __future__ import annotations

import os
import stat
import subprocess
from typing import Dict, List, NamedTuple, Optional, Sequence

from .. import safepath
from . import proc

try:
    import pwd  # the real home of this user; Windows has none (and no bwrap)
except ImportError:  # pragma: no cover
    pwd = None  # type: ignore

BWRAP_BASE = [
    "--unshare-net",
    "--unshare-pid",
    "--die-with-parent",
    "--ro-bind",
    "/",
    "/",
    "--dev",
    "/dev",
    "--proc",
    "/proc",
]
NOTE_NETLESS = "no bubblewrap on this host: the engine runs with no network but without file-system isolation"
NOTE_NONE = "no bubblewrap on this host: the engine runs unsandboxed (it can read and write what the user who started the checker can)"


class Mode(NamedTuple):
    kind: str  # bwrap | netless | none
    prefix: tuple
    note: str


_MODE: List[Mode] = []


def _probe_bwrap() -> bool:
    exe = safepath.which("bwrap")
    if not exe or os.name != "posix":
        return False
    try:
        r = safepath.run(
            [exe, *BWRAP_BASE, "--tmpfs", "/tmp", safepath.which("true") or "/usr/bin/true"],
            capture_output=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def detect(refresh: bool = False) -> Mode:
    if refresh or not _MODE:
        _MODE.clear()
        if _probe_bwrap():
            _MODE.append(Mode("bwrap", (safepath.which("bwrap"),), ""))
        else:
            net = proc.netless_prefix()
            _MODE.append(Mode("netless", tuple(net), NOTE_NETLESS) if net else Mode("none", (), NOTE_NONE))
    return _MODE[0]


def set_mode(mode: Mode) -> None:
    """Fix the mode (the tests do; a caller that wants no sandbox at all passes Mode("none", (), NOTE_NONE))."""
    _MODE.clear()
    _MODE.append(mode)


def wrap(mode: Mode, argv: Sequence[str], cwd: str, scratch: Sequence[str] = (), ro: Sequence[str] = ()) -> List[str]:
    """The command line that runs `argv` in `cwd` under `mode`; `scratch` are the directories the engine may write (bwrap only: for
    the other modes the file system is as it is); `ro` are directories that must stay visible (and read-only) although
    /tmp is a fresh tmpfs: the repository, the engine, the caches."""
    if mode.kind == "bwrap":
        out = [*mode.prefix, *BWRAP_BASE, "--tmpfs", "/tmp"]
        for d in ro:
            out += ["--ro-bind", d, d]
        for d in scratch:
            out += ["--bind", d, d]
        return [*out, "--chdir", cwd, "--", *argv]
    return [*mode.prefix, *argv]


# ---------------------------------------------------------------- the strict box of the local checks of `graph fix --verify`
UNIX_TABLE = "/proc/net/unix"  # the unix sockets of the host: the last column is the path of a file system one
_FRESH = ("/tmp", "/dev", "/proc")  # directories the box replaces anyway (a fresh tmpfs, devfs, procfs)
_NOT_HOME = ("/", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/var", "/opt", "/sys", "/boot")
_BOX: Dict[str, bool] = {}


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def _passwd_home() -> str:
    """The home directory of this uid in the passwd database ("" = Windows or a uid without an entry)."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir if pwd is not None else ""
    except KeyError:
        return ""


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:  # gone since the table was read
        return False


def hidden_dirs(environ: Optional[dict] = None) -> List[str]:
    """The directories of the host that the strict box replaces with an empty tmpfs (after `--ro-bind / /`, before the private binds):
    /run (the user bus, the agents' and the container daemons' sockets), /var/run when it is a real directory, the real HOME of this
    user (passwd and $HOME) and $XDG_RUNTIME_DIR. A path that does not exist, that /tmp, /dev or /proc replaces anyway, or that is
    already inside a hidden one is left out."""
    env = os.environ if environ is None else environ
    names = [env.get("HOME") or "", env.get("XDG_RUNTIME_DIR") or "", _passwd_home()]
    out = ["/run"]
    if os.path.isdir("/var/run") and not os.path.islink("/var/run"):
        out.append("/var/run")
    for name in names:
        if not (name and os.path.isabs(name) and os.path.isdir(name)):
            continue
        real = os.path.realpath(name)  # bwrap does not mount onto a symlink
        if real in _NOT_HOME or any(_under(real, d) for d in [*out, *_FRESH]):
            continue
        out = [d for d in out if not _under(d, real)] + [real]  # a parent replaces what it holds
    return sorted(out)


def _unix_rows(table: str) -> List[str]:
    """The rows of the unix socket table without its header ([] when it cannot be read)."""
    try:
        with open(table, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[1:]
    except OSError:
        return []


def host_sockets(skip: Sequence[str], table: str = UNIX_TABLE, keep: Sequence[str] = ()) -> List[str]:
    """The file system unix sockets of the host (the path column of /proc/net/unix) outside the `skip` directories, as real paths of
    sockets that exist now: the box covers each with /dev/null, so connect() fails there too. A socket inside a `keep` directory stays
    in even when a `skip` one holds it: the box binds such a directory back from the host, which makes the sockets inside reachable."""
    found = set()
    for row in _unix_rows(table):
        cols = row.split(None, 7)
        if len(cols) < 8 or not cols[7].startswith("/"):  # unnamed, abstract or relative
            continue
        real = os.path.join(os.path.realpath(os.path.dirname(cols[7])), os.path.basename(cols[7]))
        if _is_socket(real) and (any(_under(real, d) for d in keep) or not any(_under(real, d) for d in skip)):
            found.add(real)
    return sorted(found)


def bwrap_prefix(bwrap: str, dirs: Dict[str, str], env: dict, ro: Sequence[str], cwd: str) -> List[str]:
    """The strict bubblewrap prefix: nothing of the host is writable but the run's private directories `dirs`, no network, the
    caller's environment cleared and the allowlisted one `env` set (`--tmpfs /tmp` hides what lives there, so the binds come after
    it). The daemon sockets are masked: /run, the real HOME and $XDG_RUNTIME_DIR by an empty tmpfs before the binds, every other
    socket file of the host by /dev/null AFTER all of them (a bind of a cache re-opens the sockets it holds): a read-only bind does
    not stop connect(). `ro` are the directories bound read-only (an offline cache, the directory of the tool, the checked tree).
    The argv of the command goes after the returned list (it ends in `--`)."""
    a = [bwrap, "--die-with-parent", "--new-session", "--unshare-all", "--clearenv"]
    a += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    hidden = hidden_dirs()
    for d in hidden:
        a += ["--tmpfs", d]
    for d in dirs.values():  # tree, home, tmp, out
        a += ["--bind", d, d]
    for d in ro:
        a += ["--ro-bind", d, d]
    for sock in host_sockets([*hidden, *_FRESH], keep=[*ro, *map(os.path.realpath, ro)]):
        a += ["--ro-bind", "/dev/null", sock]
    for k, v in sorted(env.items()):
        a += ["--setenv", k, v]
    return a + ["--chdir", cwd, "--"]


def check_box(mode: Optional[Mode] = None) -> str:
    """The absolute path of the bubblewrap that builds the strict box of bwrap_prefix on this host, "" when there is none (no bwrap,
    the mode was fixed to something else, or the namespaces of the strict box cannot be made here). Probed once per path."""
    m = mode or detect()
    if m.kind != "bwrap" or not m.prefix:
        return ""
    bw = m.prefix[0]
    if bw not in _BOX:
        probe = [*bwrap_prefix(bw, {}, {}, [], "/"), safepath.which("true") or "/usr/bin/true"]
        try:
            r = safepath.run(probe, capture_output=True, timeout=15, stdin=subprocess.DEVNULL)
            _BOX[bw] = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _BOX[bw] = False
    return bw if _BOX[bw] else ""
