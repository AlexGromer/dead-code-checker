"""graph.paths: where the data of a run lives. Nothing is ever written into the analysed repository.

    <data root>/index.html                         the overview of all projects
    <data root>/projects/<name>-<hash>/runs/       the artifacts of the runs
    <data root>/projects/<name>-<hash>/store.sqlite, ledger.jsonl
    <data root>/caches/gomod                       the offline Go module cache (graph.gocache; --go-cache or $DEAD_CODE_CHECKER_GO_CACHE moves it)

<name> is the directory name of the repository, <hash> the first 8 hex digits of the sha256 of its real path, so two
repositories called `app` get two directories. The data root is, first match wins:

    $DEAD_CODE_CHECKER_HOME    an explicit choice; when it cannot be used that is an error, not a silent fallback
    <tool dir>/data            the directory of the project the package was installed from (a git clone, an unpacked
                               release, an editable install); needs a pyproject.toml beside the package
    $XDG_STATE_HOME/dead-code-checker (default ~/.local/state/dead-code-checker; %LOCALAPPDATA% on Windows)

The second is used only when it is writable and not inside the analysed repository. When it is not (pip into
site-packages, a system install, one install and several users) the third is used and `Layout.warning` holds ONE line
that says so. `--out DIR` moves only the run artifacts (`runs`) to DIR; the store and the ledger stay in the project
directory. DIR inside the analysed repository is refused.

    layout = resolve(find_root("."), out=None)       pure: it probes writability but creates nothing
    ensure(layout)                                   creates the project directory and the runs directory (DataDirError when it cannot)
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Mapping, NamedTuple, Optional, Union

ENV_HOME = "DEAD_CODE_CHECKER_HOME"
APP = "dead-code-checker"
AUTO = object()  # resolve(tool=AUTO): detect the tool directory


class DataDirError(RuntimeError):
    """No usable data directory, or an output directory that would write into the analysed repository."""


class Layout(NamedTuple):
    root: Path  # the analysed repository (real path)
    repo_id: str  # <name>-<hash>
    base: Path  # the data root
    source: str  # which rule chose it: env | tool | state
    project: Path  # <base>/projects/<repo_id>
    runs: Path  # <project>/runs, or the --out directory
    store: Path
    ledger: Path
    index: Path
    out: Optional[Path]
    warning: str  # "" or the one line that says why the tool directory was not used


def tool_dir() -> Optional[Path]:
    """The project directory the package sits in (a clone, an unpacked release, an editable install), else None:
    a package in site-packages has no project directory of its own to keep data in."""
    top = Path(__file__).resolve().parents[2]
    return top if (top / "pyproject.toml").is_file() and (top / "dead_code_checker").is_dir() else None


def find_root(start: Union[str, Path]) -> Path:
    """The repository that contains `start`: the nearest parent with a .git entry, else `start` itself (real path)."""
    here = Path(os.path.realpath(start))
    for p in (here, *here.parents):
        if (p / ".git").exists():
            return p
    return here


def repo_id(root: Union[str, Path]) -> str:
    """<name>-<hash> of a repository: the same real path always gives the same id, different paths different ones."""
    real = Path(os.path.realpath(root))
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", real.name).strip("._")[:40] or "repo"
    return f"{name}-{hashlib.sha256(real.as_posix().encode('utf-8')).hexdigest()[:8]}"


def state_home(environ: Optional[Mapping[str, str]] = None, nt: Optional[bool] = None) -> Path:
    """The per-user fallback data root."""
    env = os.environ if environ is None else environ
    nt = os.name == "nt" if nt is None else nt
    if nt:
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP


def writable(path: Union[str, Path]) -> bool:
    """Can `path` be created and written to? A real write probe in the nearest existing directory above it (the access
    bits lie on read-only mounts and ACLs); the probe file is unnamed and leaves nothing behind."""
    p = Path(os.path.abspath(path))
    while not p.exists():
        if p.parent == p:
            return False
        p = p.parent
    if not p.is_dir():
        return False
    try:
        with tempfile.TemporaryFile(dir=p):
            return True
    except OSError:
        return False


def _inside(path: Path, root: Path) -> bool:
    path, root = Path(os.path.realpath(path)), Path(os.path.realpath(root))
    return path == root or root in path.parents


def resolve(
    root: Union[str, Path],
    out: Union[str, Path, None] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    tool: object = AUTO,
    nt: Optional[bool] = None,
) -> Layout:
    """The data layout of the repository at `root`; see the module docstring for the order and the warning.

    `tool` is the project directory the data root `data/` sits in (AUTO: tool_dir(); None: there is none)."""
    env = os.environ if environ is None else environ
    real = Path(os.path.realpath(root))
    rid = repo_id(real)
    outdir = None
    if out is not None:
        outdir = Path(os.path.abspath(out))
        if _inside(outdir, real):
            raise DataDirError(f"--out {outdir} is inside the analysed repository {real}: nothing is written there")
    warning = ""
    explicit = (env.get(ENV_HOME) or "").strip()
    if explicit:
        base, source = Path(os.path.abspath(os.path.expanduser(explicit))), "env"
        if _inside(base, real):
            raise DataDirError(f"{ENV_HOME}={base} is inside the analysed repository {real}")
        if not writable(base):
            raise DataDirError(f"{ENV_HOME}={base} is not writable")
    else:
        top = tool_dir() if tool is AUTO else tool
        cand = Path(str(top)) / "data" if top else None
        if cand is None:
            why = "the package is not in a source checkout"
        elif _inside(cand, real):
            why = f"{cand} is inside the analysed repository"
        elif not writable(cand):
            why = f"{cand} is not writable"
        else:
            why = ""
        if not why:
            base, source = cand, "tool"  # type: ignore[assignment]
        else:
            base, source = state_home(env, nt), "state"
            if _inside(base, real) or not writable(base):
                raise DataDirError(f"no usable data directory ({why}; {base} is not usable either): set {ENV_HOME}")
            warning = f"{APP}: no data directory next to the tool ({why}); using {base} (set {ENV_HOME} to choose)"
    project = base / "projects" / rid
    return Layout(
        real, rid, base, source, project, outdir or project / "runs", project / "store.sqlite",
        project / "ledger.jsonl", base / "index.html", outdir, warning,
    )  # fmt: skip


def ensure(layout: Layout) -> None:
    """Create the project directory and the runs directory (the only directories a run creates). A directory that cannot be
    created (a file in the way, no permission, a read-only mount) is a DataDirError, never a bare OSError."""
    try:
        layout.project.mkdir(parents=True, exist_ok=True)
        layout.runs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DataDirError(f"cannot create the data directory {layout.runs}: {exc}") from exc
