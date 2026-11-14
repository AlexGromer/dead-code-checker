"""dead_code_checker.safepath: how the tool finds and starts the programs around it (git, ssh, bwrap, dot, gitleaks, the engines). Standard
library only.

A relative entry of PATH (`.`, `bin`, an empty entry) is looked up from the working directory. The checker is often started inside the very
repository it analyses, so a `git`, `ssh`, `bwrap` or `cp` planted in that repository would run as the checker's own tool. Everything that
starts a program by its bare name goes through this module instead:

    path_dirs(default="") -> [dir]     the absolute entries of PATH (the value of PATH, or `default` when PATH is not set)
    safe_path() -> str                 those entries joined: the PATH a started program is given
    which(name) -> str                 the absolute path of the executable `name` found in those directories, "" when there is none; a name
                                       with a directory part is answered only when it is absolute and executable
    argv(args) -> [str]                `args` with a bare program name replaced by its absolute path; FileNotFoundError when there is none
    run(args, **kw), popen(args, **kw) subprocess.run / subprocess.Popen on argv(args), with an environment whose PATH is safe_path()
                                       (the real environment when `env` is not given; a given `env` keeps its own variables and only a PATH
                                       in it is filtered)

A program that is not found is an OSError (FileNotFoundError), which every caller already handles like any other failure to start a program;
the bare name is never handed to the operating system to look up.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List, Optional, Sequence


def path_dirs(default: str = "") -> List[str]:
    """The entries of PATH that are absolute directories; a relative or empty entry is dropped (see the module docstring)."""
    return [d for d in os.environ.get("PATH", default).split(os.pathsep) if d and os.path.isabs(d)]


def safe_path() -> str:
    """The PATH a started program is given: the absolute entries of the real one, else the system default."""
    return os.pathsep.join(path_dirs(os.defpath))


def _is_exe(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def which(name: str) -> str:
    """The absolute path of the executable `name` in the absolute entries of PATH, "" when there is none."""
    if not name:
        return ""
    if os.path.dirname(name):  # a path, not a bare name: only an absolute one is taken as it is
        return name if os.path.isabs(name) and _is_exe(name) else ""
    exts = [""]
    if os.name == "nt" and not os.path.splitext(name)[1]:
        exts = [x for x in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if x]
    for d in path_dirs(os.defpath):
        for e in exts:
            cand = os.path.join(d, name + e)
            if _is_exe(cand):
                return cand
    return ""


def argv(args: Sequence[str]) -> List[str]:
    """`args` with a bare program name resolved to an absolute path. FileNotFoundError when the program is not in the absolute entries
    of PATH."""
    args = list(args)
    if args and not os.path.dirname(args[0]):
        found = which(args[0])
        if not found:
            raise FileNotFoundError(f"{args[0]}: not found in the absolute directories of PATH")
        args[0] = found
    return args


def _only_absolute(path: str) -> str:
    return os.pathsep.join(d for d in path.split(os.pathsep) if d and os.path.isabs(d))


def _env(env: Optional[Dict[str, str]]) -> Dict[str, str]:
    """The environment of a started program: the real one with a safe PATH, or the given one with the relative entries of its own PATH
    (when it has one) dropped."""
    if env is None:
        out = dict(os.environ)
        out["PATH"] = safe_path()
        return out
    out = dict(env)
    if "PATH" in out:
        out["PATH"] = _only_absolute(out["PATH"])
    return out


def run(args: Sequence[str], **kw: Any) -> "subprocess.CompletedProcess":
    """subprocess.run of argv(args) with a safe PATH in the environment."""
    kw["env"] = _env(kw.get("env"))
    return subprocess.run(argv(args), **kw)


def popen(args: Sequence[str], **kw: Any) -> "subprocess.Popen":
    """subprocess.Popen of argv(args) with a safe PATH in the environment."""
    kw["env"] = _env(kw.get("env"))
    return subprocess.Popen(argv(args), **kw)
