"""graph.gocache: the offline Go module cache. Stdlib only.

go-deadcode and scip-go load the packages of a module with its dependencies, and the engines never reach the network (GOPROXY=off).
Without the dependencies in a module cache both fail ("module lookup disabled by GOPROXY=off"): the Go verdicts stay suspects. This
module is the ONE place that says where that cache is, whether it can be used, what an engine gets from it and how it is filled.

    where      `--go-cache DIR` of the command line, else $DEAD_CODE_CHECKER_GO_CACHE, else <data root>/caches/gomod (the data root
               of graph.paths). Never the GOMODCACHE of the process environment (a session may write it), never the repository's
               configuration.
    use        usable when the directory exists and is not empty; the cache is only read (Go opens its files read-only).
               state(base, override) -> (dir, why), why "" = usable, else the reason in words.
    env        env(reg, dir, private_tree): GOMODCACHE, GOPROXY=off, GOSUMDB=off, GOTOOLCHAIN=local (+ GOFLAGS=-mod=mod only for a run
               in a scratch copy of the tree: -mod=mod may write go.mod and go.sum, the real tree is never given it). A cache that
               cannot be used leaves the environment as it was and says so (apply returns the note); the W of a failed run then
               names the reason and the command that fills it.
    check      check(repo, dir) -> Report: which modules of every go.mod of the repository are missing from the cache. Nothing is
               downloaded: `go list -e -m -json all` with GOFLAGS=-mod=mod GOPROXY=off in a scratch directory that holds a copy of
               go.mod and go.sum (the repository is not touched, a go.work is ignored). A module missing from the cache, or whose
               archive was never extracted, is missing; a replace to a local directory needs no cache. A cache directory that
               does not exist is read as an empty one (a scratch directory stands in: the check never creates it).
    fill       fill_block(dir, repos) -> text: the shell commands to run WITH network access: `go mod download` in a scratch copy of
               go.mod and go.sum with GOMODCACHE=dir. Nothing here runs them. Command line:
               `dead-code-checker graph cache go (--fill | --check) REPO... [--dir DIR] [--json]` (run()).

Exit 0 whatever the check finds (it reports; no gate reads it), 2 for a usage error. A step that cannot run says why (no go on PATH,
a failed `go list`, an unreadable go.mod): never a silent "nothing missing".
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import engines as dce
from . import proc

FAMILY = "gomod"  # the registry's cache family
AUDIT_KEY = "go"  # the same cache in data/audit.json
ENV_DIR = "DEAD_CODE_CHECKER_GO_CACHE"
CHECK_S = 120.0  # one `go list` of a module
MAX_SHOWN = 40  # missing modules named per go.mod in the text output
PROXY = "https://proxy.golang.org,direct"  # what the fill command downloads from (change it for a private proxy)
RunFn = Callable[[Sequence[str], str, float, Optional[dict]], Tuple[int, bytes, bytes]]


# ---------------------------------------------------------------- where, and whether it can be used
def location(base: Optional[os.PathLike] = None, override: Optional[str] = None) -> str:
    """The cache directory: `override`, else $DEAD_CODE_CHECKER_GO_CACHE, else <base>/caches/gomod ("" when there is no base)."""
    chosen = override or os.environ.get(ENV_DIR) or ""
    if chosen:
        return os.path.abspath(os.path.expanduser(chosen))
    return str(Path(base) / "caches" / FAMILY) if base else ""


def problem(d: str) -> str:
    """Why the directory cannot be used as a cache ("" = it can)."""
    if not d:
        return "no directory (set --go-cache or $%s)" % ENV_DIR
    if not os.path.isdir(d):
        return "does not exist"
    try:
        if not any(True for _ in os.scandir(d)):
            return "is empty"
    except OSError as exc:
        return f"cannot be read: {exc.strerror or exc}"
    return ""


def state(base: Optional[os.PathLike] = None, override: Optional[str] = None) -> Tuple[str, str]:
    """(directory, why): why is "" when the cache can be used, else the reason."""
    d = location(base, override)
    return d, problem(d)


def env(reg: dict, d: str, private_tree: bool = False) -> Dict[str, str]:
    """The Go variables of the cache family with {dir} filled. GOFLAGS (-mod=mod) only for a scratch copy of the tree."""
    out = {k: v.replace("{dir}", d) for k, v in reg["sandbox"]["caches"][FAMILY]["env"].items()}
    if not private_tree:
        out.pop(
            "GOFLAGS", None
        )  # the registry's -mod=readonly (and the repository) stay: the real tree is never written
    return out


def apply(base_env: dict, d: str, why: str) -> Tuple[dict, str]:
    """(environment of a Go engine run with the cache in it, note): the note is "" when the cache is in, else why it is not (the
    environment is then returned unchanged)."""
    if why:
        return (
            base_env,
            f"no offline Go module cache ({d or 'no directory'}: {why}); fill it: dead-code-checker graph cache go --fill <repo>",
        )
    return {**base_env, **env(dce.load_registry(), d)}, ""


# ---------------------------------------------------------------- what the cache lacks
class Missing(NamedTuple):
    module: str
    version: str
    why: str


class Report(NamedTuple):
    mod: str  # directory of the go.mod below the repository ("" = the root)
    total: int  # modules of the build list that need the cache
    missing: Tuple[Missing, ...]
    note: str  # "" or why the check of this go.mod could not be made


def modules_of(repo: str, files: Optional[Sequence[str]] = None) -> List[str]:
    """The directories (repository-relative, "" = the root) of every go.mod: the tracked ones (what the audit sees), else, in a
    tree git does not list (not a repository, nothing committed yet), a walk that skips .git, vendor and node_modules.
    """
    found = [f for f in (dce.project_files(repo) if files is None else files) if os.path.basename(f) == "go.mod"]
    if not found and files is None:
        for dp, dns, fns in os.walk(repo):
            dns[:] = sorted(d for d in dns if d not in (".git", "node_modules", "vendor"))
            found += [os.path.relpath(os.path.join(dp, f), repo) for f in fns if f == "go.mod"]
    return sorted({os.path.dirname(f) for f in found})


def _objects(text: str) -> List[dict]:
    """The JSON objects `go list -m -json` prints one after the other."""
    dec, i, out = json.JSONDecoder(), 0, []
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return out
        obj, i = dec.raw_decode(text, i)
        if isinstance(obj, dict):
            out.append(obj)


def _go(dirs: Optional[Sequence[str]] = None) -> str:
    return dce.resolve(["go"], dce.search_dirs() if dirs is None else dirs) or ""


def _needed(edit: dict) -> Optional[set]:
    """The modules `go mod download` fetches for a go.mod: with go 1.17 or later the ones its require lines name (the rest of the
    module graph is read as go.mod files only, no archive); None = every module of the build list (an older go.mod)."""
    try:
        major, minor = (int(x) for x in str(edit.get("Go") or "0.0").split(".")[:2])
    except ValueError:
        return None
    if (major, minor) < (1, 17):
        return None
    return {str(r.get("Path")) for r in edit.get("Require") or [] if isinstance(r, dict)}


def _scratch_env(tmp: str, cache: str) -> Dict[str, str]:
    return {
        "PATH": dce.safe_path(),
        "HOME": tmp,
        "TMPDIR": tmp,
        "GOCACHE": os.path.join(tmp, "gocache"),
        "GOMODCACHE": cache,
        "GOFLAGS": "-mod=mod",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOTOOLCHAIN": "local",
        "GOTELEMETRY": "off",
        "GOWORK": "off",
        "GOVCS": "*:off",
    }


def check_module(repo: str, mod: str, d: str, run_fn: Optional[RunFn] = None, go: Optional[str] = None) -> Report:
    """The modules go.mod+go.sum of `mod` need that the cache `d` does not hold."""
    exe = go if go is not None else _go()
    src = os.path.join(repo, mod)
    if not exe:
        return Report(mod, 0, (), "go was not found on PATH or in the engines directory: the cache cannot be checked")
    run_fn = run_fn or proc.run_group
    tmp = tempfile.mkdtemp(prefix="dcc-gocache-")
    try:
        work = os.path.join(tmp, "mod")
        os.mkdir(work)
        try:
            shutil.copyfile(os.path.join(src, "go.mod"), os.path.join(work, "go.mod"))
            if os.path.isfile(os.path.join(src, "go.sum")):
                shutil.copyfile(os.path.join(src, "go.sum"), os.path.join(work, "go.sum"))
        except OSError as exc:
            return Report(mod, 0, (), f"go.mod cannot be read: {exc}")
        genv = _scratch_env(tmp, d if os.path.isdir(d) else os.path.join(tmp, "empty"))  # the cache is never created
        try:
            rc, out, err = run_fn([exe, "list", "-e", "-m", "-json", "all"], work, CHECK_S, genv)
            erc, eout, _ = run_fn([exe, "mod", "edit", "-json"], work, CHECK_S, genv)
        except (OSError, subprocess.SubprocessError) as exc:
            return Report(mod, 0, (), f"go did not run: {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # go may still be writing its telemetry directory
    try:
        mods = _objects(out.decode("utf-8", "replace"))
        want = _needed(json.loads(eout.decode("utf-8", "replace"))) if erc == 0 else None
    except ValueError:
        mods, want = [], None
    if rc != 0 or not mods:
        tail = [x.strip() for x in err.decode("utf-8", "replace").splitlines() if x.strip()]
        return Report(mod, 0, (), f"go list failed (exit {rc}): {tail[-1][:200] if tail else 'no message'}")
    lacking, total = [], 0
    for m in mods:
        rep = m.get("Replace") or {}
        if m.get("Main") or (rep and not rep.get("Version")) or (want is not None and m.get("Path") not in want):
            continue  # the module itself, a replace to a directory, or a module that is only read as a go.mod file
        total += 1
        got = rep or m
        err_text = (m.get("Error") or {}).get("Err") or ""
        # an archive that is extracted is there; the Error of a module with one is a go.mod of ITS graph that is not (needed only by go < 1.17)
        why = (
            ""
            if got.get("Dir") and not (want is None and err_text)
            else err_text or "the archive is not extracted in the cache"
        )
        if why:
            lacking.append(Missing(str(got.get("Path") or m.get("Path")), str(got.get("Version") or ""), why))
    return Report(mod, total, tuple(lacking), "")


def check(repo: str, d: str, run_fn: Optional[RunFn] = None, go: Optional[str] = None) -> List[Report]:
    """One Report per go.mod of the repository (none = the repository holds no Go module)."""
    return [check_module(repo, m, d, run_fn, go) for m in modules_of(repo)]


# ---------------------------------------------------------------- what you run with network access
def fill_block(d: str, repos: Sequence[str]) -> str:
    """The shell commands that fill (or complete) the cache `d` for the go.mod files of `repos`, to be run where there is network
    access. go.mod and go.sum are copied to a scratch directory first, so nothing is written into a repository."""
    q = shlex.quote
    lines = [f"# with network access: fill the Go module cache {d}", f"mkdir -p {q(d)}"]
    genv = f"GOMODCACHE={q(d)} GOFLAGS=-mod=mod GOPROXY={q(PROXY)} GOSUMDB=sum.golang.org GOTOOLCHAIN=local GOTELEMETRY=off GOWORK=off"
    n = 0
    for repo in repos:
        for mod in modules_of(repo):
            src = os.path.join(repo, mod)
            keep = [f for f in ("go.mod", "go.sum") if os.path.isfile(os.path.join(src, f))]
            copy = " ".join(q(os.path.join(src, f)) for f in keep)
            lines.append(
                f'T=$(mktemp -d) && cp {copy} "$T"/ && (cd "$T" && env {genv} go mod download); rm -rf -- "$T"'
            )
            n += 1
    if not n:
        lines.append("# no go.mod in the given repositories: nothing to download")
    lines.append("# then: dead-code-checker graph cache go --check REPO   (lists what is still missing, no network)")
    return "\n".join(lines)


# ---------------------------------------------------------------- the command line
def render(d: str, why: str, reports: Dict[str, List[Report]]) -> List[str]:
    out = [f"go module cache: {d or '(none)'} " + (f"-- {why}" if why else "-- usable")]
    for repo, reps in reports.items():
        if not reps:
            out.append(f"{repo}: no go.mod")
        for r in reps:
            where = os.path.join(repo, r.mod) if r.mod else repo
            if r.note:
                out.append(f"{where}: not checked: {r.note}")
                continue
            out.append(
                f"{where}: {r.total - len(r.missing)} of {r.total} modules in the cache"
                + (", missing:" if r.missing else "")
            )
            out += [f"  {m.module} {m.version}  ({m.why})" for m in r.missing[:MAX_SHOWN]]
            if len(r.missing) > MAX_SHOWN:
                out.append(f"  ... and {len(r.missing) - MAX_SHOWN} more")
    return out


def run(a, base: Optional[os.PathLike] = None) -> int:
    """`graph cache go`: a.repos, a.check, a.fill, a.dir, a.json. 0 whatever is missing, 2 for a usage error."""
    prog = "dead-code-checker graph cache go"
    if not (a.check or a.fill):
        print(
            f"{prog}: say what to do: --check (what is missing) and/or --fill (the commands to run with network access)",
            file=sys.stderr,
        )
        return 2
    bad = [r for r in a.repos if not os.path.isdir(r)]
    if bad or not a.repos:
        print(
            f"{prog}: {'not a directory: ' + ', '.join(bad) if bad else 'give at least one repository'}",
            file=sys.stderr,
        )
        return 2
    repos = [os.path.abspath(r) for r in a.repos]
    d, why = state(base, a.dir)
    doc: dict = {"dir": d, "usable": not why, "why": why}
    reports: Dict[str, List[Report]] = {}
    if a.check:
        reports = {r: check(r, d) for r in repos}
        doc["repos"] = {
            r: [x._asdict() | {"missing": [m._asdict() for m in x.missing]} for x in reps]
            for r, reps in reports.items()
        }
    block = fill_block(d, repos) if a.fill and d else ""
    if a.fill and not d:
        doc["fill_error"] = why
    if a.json:
        doc["fill"] = block
        print(json.dumps(doc, indent=1, sort_keys=True))
        return 0
    if a.check:
        print("\n".join(render(d, why, reports)))
    elif a.fill and not d:
        print(f"{prog}: {why}")
    if block:
        print(block)
    return 0
