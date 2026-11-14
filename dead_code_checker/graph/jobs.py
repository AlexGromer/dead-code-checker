"""graph.jobs: the engines of a plan over a tree: what the registry's plan makes of it and how it runs.

    plan (graph.engines.plan, roles audit and sem, or branch) -> data/audit.json (how each engine is called) -> Job -> run in parallel
    -> adapters.Run

    table() / validate(tab, reg, adapter_tab)    data/audit.json and its check against the registry and the adapters
    expand(unit, spec, paths, langs_of, ...)     the jobs of one engine over this tree (scope repo | marker | files)
    gather(root, b, st, cfg, store, ...)         the whole stage: plan, jobs in parallel (a thread pool), results ordered by label

An engine of the plan (role audit or sem) without an entry in data/audit.json is named in ONE W dc.engine-unwired; one that is
missing, not in the lock, not run or degraded is W dc.engine-incomplete with the action (once per engine, also when two roles ask for
it). An engine that builds the project (needs_build) and every indexer (SCIP) runs through graph.sandbox: bubblewrap when it is
installed (no network, the tree read-only), else with a note (W dc.sandbox-weak, once per run). An indexer runs only when each
offline cache it names (data/audit.json `cache`) is usable; an engine that merely profits from one (`cache_opt`: go-deadcode with the
Go module cache) runs without it and gets it when it is usable. The Go cache is the one of graph.gocache. The index of an indexer is
stamped with the hash of its package and kept in the semantic cache of graph.semcache (the overlay that applies it is graph.sem.refine).
The analysed tree is never written. Every job runs in a private copy of the files it analyses (made in the job's scratch directory:
the files of the tree, no symlinks, the whole tree for a `repo` and a `marker` job, the listed files for a `files` job: a marker
job in a subdirectory still sees its siblings, which a go.work, a `replace ../x`, a Cargo path dependency or a tsconfig
`references` names), so Cargo.lock, a tsbuildinfo, a cache or bytecode that an engine writes lands in the copy and is deleted with
it; the paths in the engine's output are translated back to the real tree. The directories that git ignores and an engine reads
(DEP_DIRS: node_modules, .terraform) are not part of the tree and a copy could be gigabytes: those of the job's directory and of
the directories above it are mirrored into the copy as directories of the same shape whose small files (up to DEP_COPY_MAX) are
copied and whose larger ones are links (a file an engine creates or rewrites in place lands in the copy, except a large file
rewritten in place when no sandbox holds the tree read-only; a host that cannot make links gets a copy of each file instead). The
copy of every job is its own, so the disk and the I/O of a run are that of the tree (and of its dependencies) times the number of
workers. Python engines run with PYTHONDONTWRITEBYTECODE=1. cargo is not given --locked: a library without a Cargo.lock would fail; its lock is written into the
copy. With Settings.reuse a job whose package or tool is already in the cache with the same hash and engine version is not run again (its
row says `cached`; an index that cannot be read back is not cached). An index that cannot be stored (above the limit, an engine
without a family) is one W dc.sem-index. The files of a `files` job follow `--`: a tracked file named like an option is a file.
Engines.down names the engines of the plan that did not run (missing, failed, skipped): what a run that compares itself with the
previous one must not count as fixed.
"""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from . import adapters, engines as dce, gocache, par, proc, report, safety, sandbox, sem, semcache
from .model import Finding

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "audit.json")
SCOPES = ("repo", "marker", "files")
STREAMS = re.compile(r"^(stdout|stderr|file:[A-Za-z0-9_.-]+)$")
BANNED = ("install", "--install", "update", "--update", "fetch", "--fetch", "download", "--download", "upgrade")
CHUNK = 200  # files of one `files` job
DEP_DIRS = ("node_modules", ".terraform")  # ignored by git, read by an engine: mirrored into the private copy (see _mirror)
DEP_COPY_MAX = 128 << 10  # a dependency file up to this size is copied into the mirror (an engine may rewrite it), a larger one is linked
MIRROR_MAX = 400_000  # entries of the dependency directories of one job
COPY_S = 600.0  # the private copy of one job
TEXT_MAX = 64 << 20  # engine output kept
INDEX_MAX = 256 << 20  # one SCIP index kept in the store
_TAB: Dict[str, dict] = {}
WHY = {
    "dc.engine-incomplete": "this engine, indexer or step of the plan did not run, so its evidence is missing (never guessed)",
    "dc.sandbox-weak": "a build engine ran without file-system isolation: install bubblewrap (bwrap) so that its writes stay in a private scratch directory and it has no network",
    "dc.engine-unwired": "the registry puts these engines in the audit or sem plan and they are installed, but data/audit.json has no command line for them yet; they are named, not skipped silently",
}
report.register_why(WHY)


class Settings(NamedTuple):
    """What a run of the engines is told by the command line or the caller (never by the repository)."""

    timeout_s: float = 900.0  # one engine job
    budget_s: float = 5400.0  # all jobs together; a job that starts after that is skipped and named
    only: Optional[Tuple[str, ...]] = None  # engine names to run; None = the whole plan
    offline_caches: Optional[dict] = None  # {"go": dir, "nuget": dir, "cargo": dir}: what an indexer needs offline
    # "eng" is for `graph index` only: a skipped tool has no Run for the reconcile
    reuse: Tuple[
        str, ...
    ] = ()  # "index": an indexer whose package hash is cached is not run; "eng": same for an engine's tool
    workers: Optional[int] = None
    roles: Tuple[str, ...] = ("audit", "sem")  # the roles of the plan
    langs: Optional[Tuple[str, ...]] = None  # registry languages to plan for (None: those the tree holds)
    dirs: Optional[Tuple[str, ...]] = (
        None  # where the engines are looked for (None: the configured directories, then PATH)
    )
    base: Optional[str] = None  # the data root (graph.paths.Layout.base): the default place of the Go cache
    go_cache: Optional[str] = None  # the Go module cache (None: $DEAD_CODE_CHECKER_GO_CACHE, else <base>/caches/gomod)


Runner = Callable[[Sequence[str], str, float, Optional[dict]], Tuple[int, str, str]]


# ---------------------------------------------------------------- the data
def table(path: Optional[str] = None) -> dict:
    p = path or DATA
    if p not in _TAB:
        with open(p, encoding="utf-8") as fh:
            _TAB[p] = json.load(fh)
    return _TAB[p]


def validate(tab: dict, reg: dict, adapter_tab: dict) -> List[str]:
    """Problems of data/audit.json against the registry and the adapters (the second statement of a format is checked, not trusted)."""
    bad = []
    if tab.get("schema") != 1:
        bad.append("schema must be 1")
    for section in ("engines", "indexers"):
        for name, e in sorted((tab.get(section) or {}).items()):
            if name not in reg["engines"]:
                bad.append(f"{section}.{name}: not in the engines registry")
            if e.get("scope") not in SCOPES:
                bad.append(f"{section}.{name}: scope must be one of {SCOPES}")
            if e.get("scope") == "marker" and not e.get("marker"):
                bad.append(f"{section}.{name}: scope marker needs a marker")
            if e.get("scope") == "files" and not e.get("langs"):
                bad.append(f"{section}.{name}: scope files needs langs")
            if e.get("scope") == "files" and e.get("dashdash") is not True:
                bad.append(
                    f"{section}.{name}: scope files needs dashdash: true (the engine takes `--` before the file names)"
                )
            argv = e.get("argv") or []
            if not argv or argv[0] != "{bin}" or any(a in BANNED for a in argv):
                bad.append(f"{section}.{name}: argv must start with {{bin}} and must not install or fetch")
            bad += [
                f"{section}.{name}: cache_opt {c} has no variable"
                for c in e.get("cache_opt", [])
                if c not in tab["cache_env"]
            ]
            if section == "engines":
                t = adapter_tab["tools"].get(name)
                if t is None or e.get("fmt") not in t["formats"]:
                    bad.append(f"engines.{name}: no adapter format {e.get('fmt')!r}")
                if not STREAMS.match(e.get("stream", "")):
                    bad.append(f"engines.{name}: stream")
            else:
                if not isinstance(e.get("verified"), bool):
                    bad.append(f"indexers.{name}: verified must say whether the command line was recorded")
                bad += [
                    f"indexers.{name}: cache {c} has no variable"
                    for c in e.get("cache", [])
                    if c not in tab["cache_env"]
                ]
    return bad


# ---------------------------------------------------------------- running things
def default_run(argv: Sequence[str], cwd: str, timeout: float, env: Optional[dict] = None) -> Tuple[int, str, str]:
    """(rc, stdout, stderr); rc -9 on a timeout, 127 when the program is missing. No shell; the child has a process group of its
    own and the whole group is killed on a timeout."""
    try:
        rc, out, err = proc.run_group(argv, cwd, timeout, env)
    except subprocess.TimeoutExpired:
        return -9, "", f"timeout after {timeout:.0f}s"
    except OSError as e:
        return 127, "", str(e)
    return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class Job(NamedTuple):
    label: str
    engine: str
    argv: Tuple[str, ...]  # {bin} already replaced, {out} still to be
    rel: str  # directory of the run below the root ("" = the root)
    stream: str
    fmt: str
    weight: int
    sandbox: bool
    caches: Tuple[str, ...]
    covered: Tuple[str, ...]  # files a reach engine analyses (paths below the root)
    index: bool  # a SCIP indexer
    cache_opt: Tuple[str, ...] = ()  # offline caches the engine uses when they can be used and runs without otherwise
    scope: str = "repo"  # repo | marker | files: which files of the tree the job's private copy holds


class Out(NamedTuple):
    job: Job
    status: str  # ok | failed | skipped
    detail: str
    rc: Optional[int]
    text: str
    blob: Optional[bytes]  # the index of an indexer


def _marker_dirs(paths: Iterable[str], pattern: str, nest: bool) -> List[str]:
    dirs = sorted({posixpath.dirname(p) for p in paths if fnmatch.fnmatchcase(posixpath.basename(p), pattern)})
    if nest:
        return dirs
    top: List[str] = []
    for d in dirs:  # parents sort first: a directory below one that already runs is left to it
        if not any(o == "" or d == o or d.startswith(o + "/") for o in top):
            top.append(d)
    return top


def expand(
    unit: dce.Unit, spec: dict, paths: Sequence[str], langs_of: Dict[str, str], name: str, index: bool
) -> List[Job]:
    """The jobs one engine of the plan makes of this tree (data/audit.json `scope`)."""
    argv = tuple(unit.path if a == "{bin}" else a.replace("{name}", name) for a in spec["argv"])
    reach_langs = set((adapters.table()["tools"].get(unit.engine) or {}).get("reach") or ())

    def job(label: str, args: Tuple[str, ...], rel: str = "", covered: Tuple[str, ...] = ()) -> Job:
        stream, fmt, caches = spec.get("stream", "stdout"), spec.get("fmt", ""), tuple(spec.get("cache", ()))
        opt = tuple(spec.get("cache_opt", ()))
        return Job(
            label,
            unit.engine,
            args,
            rel,
            stream,
            fmt,
            unit.weight,
            unit.needs_build or index,
            caches,
            covered,
            index,
            opt,
            spec["scope"],
        )

    if spec["scope"] == "repo":
        return [job(unit.engine, argv)]
    if spec["scope"] == "marker":
        every = _marker_dirs(paths, spec["marker"], True)

        def own(d: str) -> Tuple[str, ...]:
            """The files a run in `d` is known to analyse: below `d`, but not below a nested marker directory (a nested Go module is not
            part of `./...`): a file an engine never saw must not count as one it was silent on."""
            inner = tuple(o + "/" for o in every if o != d and (not d or o.startswith(d + "/")))
            return tuple(
                p
                for p in paths
                if (not d or p.startswith(d + "/")) and langs_of.get(p) in reach_langs and not p.startswith(inner)
            )

        return [
            job(f"{unit.engine}@{d or '.'}", argv, d, own(d))
            for d in _marker_dirs(paths, spec["marker"], bool(spec.get("nest")))
        ]
    files = sorted(p for p in paths if langs_of.get(p) in spec["langs"])
    return [
        job(f"{unit.engine}#{i // CHUNK}", argv + ("--",) + tuple(files[i : i + CHUNK]))
        for i in range(0, len(files), CHUNK)
    ]


class Task(NamedTuple):
    """One unit of the parallel stage: an engine job."""

    label: str
    weight: int
    job: Job


class Ctx(NamedTuple):
    root: str
    st: Settings
    run: Runner
    mode: sandbox.Mode
    deadline: float
    clock: Callable[[], float]
    envs: Dict[str, dict]  # engine -> the env of the registry (GOPROXY=off ...)
    paths: Tuple[str, ...] = ()  # the files of the tree (sorted, "/"-separated): what a private copy is made of


def _skip(job: Job, why: str) -> Out:
    return Out(job, "skipped", why, None, "", None)


def offline(st: Settings) -> Tuple[Dict[str, str], Dict[str, str]]:
    """({cache name: directory} of the offline caches that can be used, {cache name: why one cannot}). The Go cache comes from
    graph.gocache (--go-cache, $DEAD_CODE_CHECKER_GO_CACHE, else <data root>/caches/gomod); nuget and cargo only from
    Settings.offline_caches."""
    configured, use, why = st.offline_caches or {}, {}, {}
    for name in table()["cache_env"]:
        if name == gocache.AUDIT_KEY:
            d, bad = gocache.state(st.base, configured.get(name) or st.go_cache)
            how = f"the Go module cache {d or '(none)'}: {bad}; fill it: dead-code-checker graph cache go --fill REPO"
        else:
            d = configured.get(name) or ""
            bad = "" if d and os.path.isdir(d) else "not configured"
            how = f"the offline cache {name}: {bad}"
        if bad:
            why[name] = how
        else:
            use[name] = d
    return use, why


def _cache_env(name: str, d: str) -> dict:
    """The variables an engine gets for the cache `name`: the Go ones of graph.gocache (no -mod=mod: the tree is the real one), else one."""
    if name == gocache.AUDIT_KEY:
        return gocache.env(dce.load_registry(), d)
    return {table()["cache_env"][name]: d}


def _outputs(job: Job, out_dir: str, so: str, se: str) -> Tuple[str, Optional[bytes]]:
    """(text, index) of a finished job: the stream the data names, or the file the engine wrote into {out}."""
    if job.index:
        path = os.path.join(out_dir, "index.scip")
        if not os.path.isfile(path) or os.path.getsize(path) > INDEX_MAX:
            return "", None
        with open(path, "rb") as fh:
            return "", fh.read()
    if job.stream.startswith("file:"):
        try:
            with open(os.path.join(out_dir, job.stream[5:]), encoding="utf-8", errors="replace") as fh:
                return fh.read(TEXT_MAX), None
        except OSError:
            return "", None
    return (so if job.stream == "stdout" else se)[:TEXT_MAX], None


def _base_env(tmp: str) -> dict:
    return {**dce.host_env(), "HOME": tmp, "TMPDIR": tmp, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}


def _chain(rel: str) -> List[str]:
    """The directories from the root down to `rel`: "" , "a", "a/b" for "a/b"."""
    parts = rel.split("/") if rel else []
    return ["/".join(parts[:i]) for i in range(len(parts) + 1)]


def _copied(job: Job, paths: Sequence[str]) -> List[str]:
    """The files of the tree that go into the private copy of `job` (see the module docstring): the named ones for a `files` job,
    else all of them (a marker job reads its siblings through go.work, `replace`, path dependencies and `references`)."""
    if job.scope == "files":
        named = set(job.argv[job.argv.index("--") + 1 :]) if "--" in job.argv else set()
        return [p for p in paths if p in named]
    return list(paths)


def _link(target: str, dst: str) -> bool:
    """Make the link `dst` -> `target`; False when this host cannot make links (no privilege), any other error is raised."""
    try:
        os.symlink(target, dst)
        return True
    except FileExistsError:
        raise
    except OSError:
        return False


def _dependency_file(src: str, dst: str) -> None:
    """A dependency file in the mirror: a copy when it is small (an engine that rewrites it in place changes the copy), else a link to
    the real one (also a file that is not a regular one: a copy of a pipe would wait for a writer); a host that cannot make links
    gets a copy of it too."""
    st = os.stat(src)
    if (st.st_size > DEP_COPY_MAX or not stat.S_ISREG(st.st_mode)) and _link(src, dst):
        return
    shutil.copy(src, dst)


def _mirror(real: str, link: str, end: float, clock) -> None:
    """A directory of real directories whose files are copies of the small files of `real` and links to the large ones (a link of
    `real` is made again as it is): an engine reads the dependencies, and a file or directory it creates there, or a small file it
    rewrites, lands in the copy. Raises OSError or TimeoutError (too many entries, no time left, a file that cannot be copied)."""
    n = 0
    for dp, dns, fns in os.walk(real, followlinks=False):
        to = os.path.join(link, os.path.relpath(dp, real)) if dp != real else link
        os.makedirs(to, exist_ok=True)
        files = set(fns)
        for name in dns + fns:
            n += 1
            if n > MIRROR_MAX or clock() >= end:
                raise TimeoutError(f"{real} is too large to mirror into the private copy ({MIRROR_MAX} entries, {COPY_S:g}s)")
            src, dst = os.path.join(dp, name), os.path.join(to, name)
            if os.path.islink(src):
                if not _link(os.readlink(src), dst):
                    if not os.path.isfile(src):
                        raise OSError(f"{dst}: no symbolic link can be made here and {src} is not a file to copy")
                    shutil.copy(src, dst)  # no links on this host: the file the link names
            elif name in files:
                try:
                    _dependency_file(src, dst)
                except FileNotFoundError:
                    pass  # gone since the walk started
            # a real directory is made when the walk gets there


def _private_tree(root: str, job: Job, paths: Sequence[str], dst: str, end: float, clock) -> str:
    """Make the private copy of the files of `job` below `dst`; -> the directory the job runs in (the copy of root/job.rel).
    Raises OSError or TimeoutError. A file gone since the snapshot is left out."""
    made = set()
    for p in _copied(job, paths):
        if clock() >= end:
            raise TimeoutError(f"the private copy of the tree for {job.label} ran out of time")
        to = os.path.join(dst, *p.split("/"))
        d = os.path.dirname(to)
        if d not in made:
            os.makedirs(d, exist_ok=True)
            made.add(d)
        try:
            shutil.copy(os.path.join(root, *p.split("/")), to)
        except FileNotFoundError:
            pass
    work = os.path.join(dst, *job.rel.split("/")) if job.rel else dst
    os.makedirs(work, exist_ok=True)
    for d in _chain(job.rel):
        for name in DEP_DIRS:  # the dependencies live in the real tree: mirrored with links, not copied
            real, link = os.path.join(root, *d.split("/"), name), os.path.join(dst, *d.split("/"), name)
            if os.path.isdir(real) and not os.path.islink(real) and not os.path.lexists(link):
                _mirror(real, link, end, clock)
    return work


def _back(text: str, copy: str, root: str) -> str:
    """The paths of the private copy in an engine's output, as the paths of the real tree (also as JSON-escaped text)."""
    for c in dict.fromkeys((copy, os.path.realpath(copy))):
        for a, b in ((c, root), (c.replace("\\", "\\\\"), root.replace("\\", "\\\\"))):
            text = text.replace(a, b)
    return text


def _run_job(job: Job, cx: Ctx) -> Out:
    left = cx.deadline - cx.clock()
    if left <= 1:
        return _skip(job, f"the budget of {cx.st.budget_s:g}s is spent")
    caches, why = offline(cx.st)
    absent = [c for c in job.caches if c not in caches]
    if absent:
        return _skip(job, f"the offline cache {', '.join(absent)} is not usable ({'; '.join(why[c] for c in absent)})")
    tmp = tempfile.mkdtemp(prefix="dcc-job-")
    try:
        out = os.path.join(tmp, "out")
        os.mkdir(out, 0o700)
        env = _base_env(tmp)
        env.update(cx.envs.get(job.engine, {}))
        for c in (*job.caches, *(c for c in job.cache_opt if c in caches)):
            env.update(_cache_env(c, caches[c]))
        tree = os.path.join(tmp, "tree")  # the engine never gets the analysed tree itself, only this copy of the files it analyses
        os.mkdir(tree, 0o700)
        try:
            cwd = _private_tree(cx.root, job, cx.paths, tree, cx.clock() + min(COPY_S, left), cx.clock)
        except (OSError, TimeoutError) as e:
            return Out(job, "failed", f"no private copy of the tree: {type(e).__name__}: {e}"[:200], None, "", None)
        argv = [a.replace("{out}", out) for a in job.argv]
        if job.sandbox:
            scratch = dce.load_registry()["sandbox"].get("scratch_env", {})
            env.update({k: v.replace("{out}", out).replace("{home}", tmp) for k, v in scratch.items()})
            keep = [cx.root, os.path.dirname(argv[0])] + [
                d for c, d in caches.items() if c in job.caches or c in job.cache_opt
            ]
            argv = sandbox.wrap(cx.mode, argv, cwd, scratch=[tmp], ro=[d for d in keep if d and os.path.isdir(d)])
        rc, so, se = cx.run(argv, cwd, min(cx.st.timeout_s, left), env)
        if rc in (-9, 127):
            return Out(job, "failed", ((se or so).strip().splitlines() or ["no output"])[-1][:200], rc, "", None)
        text, blob = _outputs(job, out, so, se)
        text = _back(text, tree, cx.root)
        if job.index and (rc != 0 or blob is None):
            return Out(
                job, "failed", f"exit {rc}, " + ("no index written" if blob is None else "see the engine"), rc, "", None
            )
        if job.stream.startswith("file:") and not text and not job.index:
            return Out(job, "failed", f"exit {rc}, no {job.stream[5:]} written", rc, "", None)
        return Out(job, "ok", "", rc, text, blob)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class Engines(NamedTuple):
    runs: Tuple[adapters.Run, ...]  # raw engine output, data for adapters.reconcile
    findings: Tuple[Finding, ...]  # W: not run, missing, unwired, indexes not applied, no sandbox
    rows: Tuple[dict, ...]  # one per job: engine, label, status, detail
    notes: Tuple[str, ...]
    down: Tuple[str, ...] = ()  # engines of the plan that did not run (sorted, no repeats)
    indexes: Tuple[Tuple[str, str, bytes], ...] = ()  # (engine, package root, SCIP blob) of the indexers that ran here


def _incomplete(name: str, detail: str, install: str = "") -> Finding:
    if detail.startswith("degraded: "):
        # no build for this OS: said once, with no install line
        return Finding(
            "W", safety.WHERE, 0, "dc.engine-incomplete", dce.not_run_here(name, detail[len("degraded: ") :])
        )
    act = f"; install: {install}" if install and not install.startswith("degraded") else ""
    return Finding("W", safety.WHERE, 0, "dc.engine-incomplete", f"{name}: not run ({detail}){act}")


def _plan_tasks(root: str, b, st: Settings, cfg) -> Tuple[List[Task], List[Finding], List[str]]:
    """The jobs of the plan (the roles of Settings) over this tree, the W for what the plan holds but cannot run, and who they are."""
    reg, lock, tab = dce.load_registry(), dce.cached_lock(), table()
    only = None if st.only is None else set(st.only)
    paths = sorted(b.tree.files)
    present = dce.project_langs(reg, paths)
    kw = dict(langs=present if st.langs is None else present & set(st.langs), files=paths, dirs=st.dirs, cfg=cfg)
    tasks, fs, absent, unwired, seen = [], [], [], set(), set()
    served = {
        s["engine"] for s in sem.config()["lsp"]["servers"].values()
    }  # the language-server step runs these, not a job
    for role in st.roles:
        plan = dce.plan(reg, lock, role, **kw)
        for u in plan.units:
            if u.engine in seen or (only is not None and u.engine not in only):
                continue
            idx = u.engine in tab["indexers"]
            spec = tab["indexers"][u.engine] if idx else tab["engines"].get(u.engine)
            if (
                spec is None
            ):  # an LSP or another engine of the sem role is named too: nothing of the plan goes unmentioned
                if role != "branch" and u.engine not in served:
                    unwired.add(u.engine)
                continue
            seen.add(u.engine)
            jobs = expand(u, spec, paths, b.langs, os.path.basename(root), idx)
            tasks += [Task(j.label, j.weight, j) for j in jobs]
        for row in plan.missing + plan.degraded:  # an engine of two roles is one W
            if row["engine"] not in absent and (only is None or row["engine"] in only):
                fs.append(_incomplete(row["engine"], row["detail"], row["install"]))
                absent.append(row["engine"])
    if unwired:
        msg = f"the plan holds installed engines that this version does not run: {', '.join(sorted(unwired))}"
        fs.append(Finding("W", safety.WHERE, 0, "dc.engine-unwired", msg))
    return tasks, fs, absent


def gather(
    root: str,
    b,
    st: Settings,
    cfg=None,
    store=None,
    run: Runner = default_run,
    clock=time.monotonic,
    now: Optional[float] = None,
    mode: Optional[sandbox.Mode] = None,
) -> Engines:
    """The engines of the plan over this tree run in parallel; see the module docstring. `b` is a levels.Built (its tree and langs);
    `cfg` the graph.config.Config (an engine waived in it is degraded, not missing); `now` (epoch seconds, None = the host clock)
    stamps what goes into the semantic cache; `mode` is the sandbox mode (None: sandbox.detect())."""
    tasks, fs, down = _plan_tasks(root, b, st, cfg)
    files, lock, cached, reg = semcache.blobs(b.tree), dce.cached_lock(), [], dce.load_registry()
    if store is not None and st.reuse:
        tasks, cached = semcache.split_cached(store, files, tasks, st.reuse, lock, now)
    mode = mode or (sandbox.detect() if any(t.job.sandbox for t in tasks) else sandbox.Mode("none", (), ""))
    if any(t.job.sandbox for t in tasks) and mode.note:
        fs.append(Finding("W", safety.WHERE, 0, "dc.sandbox-weak", mode.note))
    envs = {n: dict(e.get("env") or {}) for n, e in dce.load_registry()["engines"].items()}
    cx = Ctx(root, st, run, mode, clock() + st.budget_s, clock, envs, tuple(sorted(b.tree.files)))
    outs = par.run_units(
        tasks, lambda t: _run_job(t.job, cx), jobs=par.workers(st.workers), kind="thread", key=lambda t: t.label
    )
    runs, rows, notes, indexes = [], list(cached), [], []

    def keep(engine: str, rel: str, blob: bytes, label: str, path: str) -> None:
        ver = dce.engine_version(reg, lock, engine, path)
        try:
            if store is not None and semcache.put_index(store, files, engine, rel, blob, ver, now) is None:
                why = f"{engine} has no language family in the registry: its index of {rel or '.'} cannot be cached"
                fs.append(Finding("W", safety.WHERE, 0, "dc.sem-index", f"{label}: {why}"))
        except sem.SemError as e:
            fs.append(Finding("W", safety.WHERE, 0, "dc.sem-index", f"{label}: {e}"))

    for o in outs:
        j = o.job
        rows.append({"engine": j.engine, "label": j.label, "status": o.status, "detail": o.detail})
        if o.status != "ok":
            fs.append(_incomplete(j.label, o.detail))
            down.append(j.engine)
            continue
        if j.index:
            keep(j.engine, j.rel, o.blob, j.label, j.argv[0])
            indexes.append((j.engine, j.rel, o.blob))
        else:
            cwd = os.path.join(root, j.rel) if j.rel else root
            runs.append(
                adapters.Run(
                    j.engine,
                    o.text,
                    dce.engine_version(reg, lock, j.engine, j.argv[0]),
                    j.fmt,
                    cwd,
                    j.covered,
                    o.rc,
                    j.rel,
                )
            )
    if store is not None and indexes:
        semcache.gc(store, now)
    return Engines(tuple(runs), tuple(fs), tuple(rows), tuple(notes), tuple(sorted(set(down))), tuple(indexes))
