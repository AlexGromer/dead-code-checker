"""graph.fixverify: `dead-code-checker graph fix --dry-run --verify`, the removals of the patch checked before they are offered.

The patch is applied to a COPY of the tree (never the work tree: the copy is made from the files git lists, in a private directory
that is deleted afterwards) and, per language, the cheapest check that can show a removal unsafe runs on it. There are two classes
of check and the line between them is a rule of the trust model:

    local      NON-EXECUTING: it compiles or type-checks the copy and runs none of it. Python: compile() + symtable + ast of the patched
               files and an import scan of the tree, star imports included (graph.pycheck, a separate `python3 -I` process in the
               bubblewrap box of graph.sandbox; in this process when the host has no box, said in a note; it never imports, runs or
               evals project code in either mode); Go: `go vet ./...` (compile only, offline: GOFLAGS=-mod=mod GOPROXY=off GOSUMDB=off
               GOTOOLCHAIN=local CGO_ENABLED=0, the offline module cache of graph.gocache); TypeScript: `tsc --noEmit`. A toolchain
               check runs only in the box, never without one (without bubblewrap it is `unverified`, with the reason), and only for a
               command of NON_EXECUTING below: data/fix.json cannot widen that list, so a check that runs project code (tests,
               build.rs, maven or gradle plugins, npm scripts, coverage or trace runs) cannot be configured as local.
    runtime    EXECUTING: the tests of the project, cargo check (build scripts), mvn and gradle, npm test. It runs ONLY on a separate
               execution target (a Target: a configured remote host, see graph.fix `_exec_target`) with the project copy in a sandbox
               there, and what comes back is data (RuntimeResult). With no such target nothing of it runs on this machine: the
               report carries ONE line "runtime mode: no execution target (<why>)" (level I) and the symbols stay at what the local
               check said.

    verify(root, units, render, texts, opts, host, spec, workers) -> Report(kept, rows, notes, checks, langs)

`units` are the removals of the plan (one per top-level span, the symbols that go with it inside), `render(units)` gives the text of
every changed file with exactly those removals, `opts` = Options(target, why): the execution target or None and the reason there is
none. Per language, the local class first (the cheapest), then the runtime class on what survived, with the same search for both:

    round 0    the whole patch (and, for a check that can fail on its own, the tree as it is: a check that fails there judges nothing);
    round 1    when it fails: the removals of each file alone, in parallel;
    round 2    those of a file that still fails, one symbol each, in parallel;
    then       the survivors together once more; if they fail together (two removals that need each other), a binary search over
               their order finds the first one that breaks them, it is dropped and the search goes on;
    bounds     data/fix.json verify.<class>: rounds and checks per class; what is not proven when they are spent is dropped, never kept.

The units of a round run in parallel through par.run_units (a thread pool); the units and the results are in the order of (path, first
line), whatever the worker count. Every symbol gets a status:

    verified-runtime  the check on the target passed          verified-local  the local check passed (no target, or no runtime check)
    unverified        no check could judge it (none for the language, the box or the tool is missing, the check fails on the tree
                      as it is): the removal STAYS in the patch with the reason, nobody showed it unsafe
    failed            the check broke on it, or it could not be shown to pass after the whole patch broke: DROPPED from the patch and
                      listed with the first error line of the check

Nothing is written into the work tree: the copies live under a private temporary directory. The copy of the tree is made once
(`base`, the files git lists without the secret-looking ones, symlinks that leave the tree skipped). The Python check only READS it: it
gets `base` read-only and the edited texts as an overlay, so it costs no copy at all. A toolchain check (go vet, tsc) builds next to
the sources, so each of them gets a clone of `base` with the edits written over (`cp -a --reflink=auto`: copy-on-write where the file
system has it, a full copy where not) that is deleted when the check ends; at most `workers` clones exist at once, so the disk cost
on a file system without reflinks is `workers` x the size of the tree (the rounds and checks bounds of data/fix.json limit the time,
not the space). A failure of a check is an `unverified` (or, when the whole patch is known to fail, a drop), never a traceback.

The runtime seam is the small class Target below: graph.fix resolves ONE target (or None and the reason) and hands it over in
Options. A job travels as data: the tracked files at HEAD with `edits` put over them, `argv` run in each directory of `dirs`, back
come the exit code and the first error line of the check (secrets in it are masked).
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from .. import safepath
from . import engines as dce
from . import gocache, par, proc, pycheck, rtcfg, sandbox

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fix.json")
VERIFIED_RUNTIME, VERIFIED_LOCAL, UNVERIFIED, FAILED = "verified-runtime", "verified-local", "unverified", "failed"
STATUSES = (VERIFIED_RUNTIME, VERIFIED_LOCAL, UNVERIFIED, FAILED)
# The ONLY toolchain commands that may run locally (they compile or type-check and run nothing of the project). data/fix.json cannot add one.
NON_EXECUTING = (("go", "vet"), ("go", "build"), ("tsc", "--noEmit"))
RUNTIME_SKIPPED = rtcfg.SKIP  # the one skip line of the runtime mode, see skip_line
MAX_LINE = 300
SKIP_DIRS = {".git", "node_modules", "__pycache__"}
SECRET_NAMES = (
    ".env*",
    "*.pem",
    "*.key",
    "id_*",
    "*.p12",
    "*.pfx",
    "*.kdbx",
    ".netrc",
    ".pgpass",
    "*.gpg",
    "*.asc",
)  # never copied
PATH = "/usr/local/bin:/usr/bin:/bin"  # the PATH inside the box (the directory of the tool is put in front of it)


# ------------------------------------------------------------------ the data
class Check(NamedTuple):
    lang: str
    klass: str  # local | runtime
    name: str
    how: str  # the sentence of data/fix.json that says what the class means for this check
    argv: Tuple[str, ...] = ()
    marker: str = ""  # the manifest file of the package: the check runs in the directory that holds it
    baseline: bool = False  # the check may fail on the tree as it is: run it there first
    builtin: str = ""  # "pycheck"
    env: Tuple[Tuple[str, str], ...] = ()
    offline: str = ""  # "go": the offline module cache of graph.gocache


def table() -> dict:
    with open(DATA, encoding="utf-8") as fh:
        return json.load(fh)["verify"]


def checks(lang: str, spec: Optional[dict] = None) -> Dict[str, Check]:
    """{"local": Check, "runtime": Check} of a language (a class that has none is missing). ValueError for a local check that is not
    one of NON_EXECUTING (or pycheck): the trust rule is a property of this code, not of the data."""
    out: Dict[str, Check] = {}
    for klass, e in ((spec or table())["langs"].get(lang) or {}).items():
        c = Check(
            lang,
            klass,
            e["check"],
            e.get("class", ""),
            tuple(e.get("argv") or ()),
            e.get("marker", ""),
            bool(e.get("baseline")),
            e.get("builtin", ""),
            tuple(sorted((e.get("env") or {}).items())),
            e.get("offline", ""),
        )
        if klass == "local" and not (c.builtin == "pycheck" or tuple(c.argv[:2]) in NON_EXECUTING):
            raise ValueError(f"{lang}: {c.name} is not a non-executing check: only the target runs it (class runtime)")
        out[klass] = c
    return out


def classes(lang: str, spec: Optional[dict] = None) -> List[Tuple[str, str, str, str]]:
    """[(class, check, where it runs, what the class means)] of a language, local first: "here" for a non-executing check,
    "target only" for an executing one (it never runs on this machine)."""
    cs = checks(lang, spec)
    return [
        (k, cs[k].name, "here" if k == "local" else "target only", cs[k].how) for k in ("local", "runtime") if k in cs
    ]


# ------------------------------------------------------------------ what the caller hands over and gets back
class Unit(NamedTuple):
    """One removal: a top-level span of one file; `items` are the symbols that go with it (the symbol first, then the ones inside)."""

    path: str
    first: int
    last: int
    items: tuple
    lang: str


class Row(NamedTuple):
    path: str
    line: int
    qual: str
    code: str
    status: str
    check: str  # the checks that passed (or the one that failed), "" for unverified
    klass: str  # local | runtime | "" (the class of the deciding check)
    detail: str  # the first error line of a failed one, the reason of an unverified one
    conf: int = -1  # the confidence of the claim (graph.confidence, 0..100), -1 = none


class Report(NamedTuple):
    kept: Tuple[Unit, ...]  # the removals of the output patch: everything that did not fail
    rows: Tuple[Row, ...]  # one per symbol, sorted by (path, line, qual)
    notes: Tuple[Tuple[str, str], ...]  # (level I | W, text)
    checks: int  # runs of a check (the ones on the tree as it is included)
    langs: Tuple[
        str, ...
    ] = ()  # the languages of the patch, sorted: `classes(lang)` says which class each of their checks is


class Outcome(NamedTuple):
    ok: Optional[
        bool
    ]  # True: passed; False: the candidate breaks the check; None: the check cannot judge (error says why)
    error: str = ""


class RuntimeJob(NamedTuple):
    """What a Target is asked to run: the project at `root` (its tracked files at HEAD) with `edits` put over it, in a sandbox there,
    `argv` in each of `dirs`, the directories of the nearest `marker` above the edited files (the root when ""). The target decides,
    by its own allowlist, whether it runs it at all."""

    root: str
    lang: str
    check: str
    argv: Tuple[str, ...]
    marker: str
    edits: Tuple[Tuple[str, str], ...]  # (path, the whole patched text), sorted; () = the tree as it is (the baseline)
    timeout_s: int
    dirs: Tuple[str, ...] = (
        "",
    )  # the directories (relative to root, "" = root) that argv runs in, one after the other: the package dirs


class RuntimeResult(NamedTuple):
    ran: bool  # the command ran to its end (rc is its exit code); False: error says why it did not
    rc: Optional[int]
    error: str = ""  # the first error line of the output, or the reason it did not run


class Target:
    """The separate execution target of the trust rule (a configured remote host with a sandbox). The seam of this module: graph.fix
    resolves an object like this one (or None and the reason). Nothing that runs project code runs anywhere else."""

    name = "none"

    def unavailable(self) -> str:
        """ "" when it can run a job now, else one line: why not (not configured, not reachable, no sandbox there)."""
        return "no execution target"

    def run(self, job: RuntimeJob) -> RuntimeResult:  # pragma: no cover - interface
        raise NotImplementedError


class Options(NamedTuple):
    target: Optional[Target] = None
    why: str = ""  # why there is no target (a line for the note)
    base: Optional[str] = None  # the data root (graph.paths.Layout.base): where the offline Go module cache is looked for
    go_cache: Optional[str] = None  # --go-cache (else $DEAD_CODE_CHECKER_GO_CACHE, else <base>/caches/gomod)


# ------------------------------------------------------------------ the host: replaceable, so that the tests run no tool
class Host:
    """The host side of the local checks. Every method may be replaced (the tests do): which tool, the box around it, how it runs."""

    tmp_base: Optional[str] = None

    def resolve(self, name: str) -> str:
        """Absolute path of a tool: the engines directories, then PATH (graph.engines.search_dirs, as the engines are found), "" = none."""
        return dce.resolve([name], dce.search_dirs()) or ""

    def python(self) -> str:
        """The interpreter of the Python check: the one that runs the checker."""
        return sys.executable or ""

    def box(self, argv: List[str], dirs: Dict[str, str], env: dict, ro: Sequence[str], cwd: str) -> Optional[List[str]]:
        """argv inside the bubblewrap box (no network, only `dirs` writable), None when this host has no bwrap that works."""
        bw = sandbox.check_box()
        return [*sandbox.bwrap_prefix(bw, dirs, env, ro, cwd), *argv] if bw else None

    def run(self, argv: Sequence[str], cwd: str, timeout: float, env: dict) -> tuple:
        return proc.run_group(argv, cwd, timeout, env)

    def tracked(self, root: str) -> List[str]:
        """The files a copy takes: tracked and untracked-not-ignored (git), else everything but .git and node_modules."""
        try:
            r = safepath.run(
                ["git", "-C", root, "ls-files", "-z", "-co", "--exclude-standard"],
                capture_output=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
            )
            if r.returncode == 0:
                return sorted(x for x in r.stdout.decode("utf-8", "replace").split("\0") if x)
        except (OSError, subprocess.SubprocessError):
            pass
        out = []
        for d, dirs, files in os.walk(root):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
            out += [os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/") for f in sorted(files)]
        return out

    def offline_env(self, spec_env: dict, kind: str, opts: Optional["Options"] = None) -> Tuple[dict, str]:
        """(environment of the tool, note): the offline variables of data/fix.json and, for Go, the module cache of graph.gocache."""
        if kind != "go":
            return dict(spec_env), ""
        d, why = gocache.state(opts.base if opts else None, opts.go_cache if opts else None)
        return gocache.apply(dict(spec_env), d, why)


mask_secrets = rtcfg.mask_secrets  # one mask for everything a check or a target says: key-shaped tokens, bearer, URL passwords, key=value
skip_line = rtcfg.skip_line  # the ONE line of a runtime step that did not run


def _first_error(out: str, err: str, strip: str = "") -> str:
    """The line of a tool's output that says what broke (see graph.rtcfg.first_error), at most MAX_LINE characters, secrets masked."""
    return rtcfg.first_error(out, err, strip, MAX_LINE)


def _rmtree(path: str) -> None:
    def fix(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=fix)


def _inside(real_root: str, real_path: str) -> bool:
    """`real_path` is the real root itself or below it (a sibling `/a/bc` of `/a/b` is not: the separator counts)."""
    return real_path == real_root or real_path.startswith(real_root.rstrip(os.sep) + os.sep)


class Workspace:
    """A pristine copy of the tree (`base`) and, per candidate, a clone of it with the edits written over (CoW where the file system
    has it). The work tree is only read; files that look like secrets are not copied."""

    def __init__(self, root: str, host: Host, scratch: str):
        self.root, self.host, self.scratch = root, host, scratch
        self.base = os.path.join(scratch, "base")
        self._n = 0
        self._lock = threading.Lock()
        self.skipped_links: List[str] = (
            []
        )  # tracked symlinks that are not in the copy (absolute, or they leave the tree): for the report
        os.mkdir(self.base)
        real = os.path.realpath(root)
        links: List[Tuple[str, str]] = []
        for rel in host.tracked(root):
            if any(fnmatch.fnmatchcase(os.path.basename(rel), g) for g in SECRET_NAMES):
                continue
            src = os.path.join(root, rel)
            if not _inside(real, os.path.realpath(os.path.dirname(src))):  # a path that leaves the tree is never copied
                continue
            dst = os.path.join(self.base, rel)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.islink(src):
                    target = os.readlink(src)
                    if os.path.isabs(target) or os.path.splitdrive(target)[0]:
                        self.skipped_links.append(
                            rel
                        )  # an absolute link would read the host's files in the copy (tsc follows it)
                        continue
                    os.symlink(target, dst)
                    links.append((rel, dst))
                elif os.path.isfile(src):
                    shutil.copy2(src, dst)
            except OSError:
                continue
        # a relative link stays only when, resolved INSIDE the copy (through the other links too, `..` after a link), it ends in the copy
        real_base = os.path.realpath(self.base)
        for rel, dst in links:
            if not _inside(real_base, os.path.realpath(dst)):
                self.skipped_links.append(rel)
                with contextlib.suppress(OSError):
                    os.unlink(dst)
        self.skipped_links.sort()

    def candidate(self, edits: Dict[str, str]) -> str:
        """A fresh directory: the base with `edits` ({path: text}) written over it. Remove it with drop()."""
        with self._lock:
            self._n += 1
            dst = os.path.join(self.scratch, f"cand{self._n}")
        cp, done = self.host.resolve("cp"), False
        if cp:
            r = subprocess.run(
                [cp, "-a", "--reflink=auto", "--", self.base, dst], capture_output=True, stdin=subprocess.DEVNULL
            )
            done = r.returncode == 0
        if not done:
            shutil.copytree(self.base, dst, symlinks=True, dirs_exist_ok=True)
        for rel, text in edits.items():
            p = os.path.join(dst, rel)
            if os.path.islink(p):
                os.unlink(p)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        return dst

    def drop(self, path: str) -> None:
        _rmtree(path)


def _package_dirs(tree: str, paths: Sequence[str], marker: str) -> List[str]:
    """The directories (relative, "" = the root) that hold `marker` nearest above each of `paths`; [] when one of them has none."""
    out: List[str] = []
    for p in paths:
        d = os.path.dirname(p)
        while True:
            if os.path.isfile(os.path.join(tree, d, marker)):
                out.append(d)
                break
            if not d:
                return []
            d = os.path.dirname(d)
    return sorted(set(out))


def _tool_dirs(exe: str, *extra: str) -> List[str]:
    """The directories the box must show read-only for the tool at `exe`: its own directory (the real one behind a symlink), and the
    prefix above a `bin` directory (a Go or Node installation keeps its library there), so that a tool installed under the home
    directory, which the box hides, still starts. `extra` are more directories (the prefix of the interpreter). Only existing ones."""
    out: List[str] = []
    for e in (exe, os.path.realpath(exe)):
        d = os.path.dirname(e)
        for cand in (d, os.path.dirname(d) if os.path.basename(d) == "bin" else ""):
            if cand and cand not in ("/", "/usr") and os.path.isdir(cand) and cand not in out:
                out.append(cand)
    for d in extra:
        if d and d not in ("/", "/usr") and os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def _ro_outside(ro: Sequence[str], dirs: Dict[str, str]) -> List[str]:
    """`ro` without the directories that hold (or are) one of the writable `dirs`: a read-only bind over a parent would cover them."""
    return [d for d in ro if not any(_inside(os.path.realpath(d), os.path.realpath(w)) for w in dirs.values())]


class Verifier:
    def __init__(
        self,
        root: str,
        render: Callable[[Sequence[Unit]], Dict[str, str]],
        texts: Dict[str, str],
        opts: Options,
        host: Host,
        spec: dict,
        workers: Optional[int],
    ):
        self.root, self.render, self.texts = root, render, texts
        self.target, self.host, self.spec, self.workers = opts.target, host, spec, workers
        self.opts, self.why = opts, opts.why
        self._no_runtime: Optional[str] = (
            None  # asked on first use (see no_runtime): a patch with no removals asks nobody
        )
        self._rt_lock = threading.Lock()
        self.ws: Optional[Workspace] = None
        self.paths: List[str] = (
            []
        )  # the files of the language being checked (the scope of a check on the tree as it is)
        self.left = {k: dict(rounds=spec[k]["rounds"], checks=spec[k]["checks"]) for k in ("local", "runtime")}
        self.runs = 0
        self.skipped = False  # a language in the patch has a runtime check and there was no target
        self.notes: List[Tuple[str, str]] = []
        self._lock = threading.Lock()

    @property
    def no_runtime(self) -> str:
        """Empty: a target is ready; else the one line that says why there is none. The target is asked ONCE, when a language of the
        patch has a runtime check to run (a health call to a remote target is a network round trip: none for a patch without removals).
        """
        with self._rt_lock:
            if self._no_runtime is None:
                self._no_runtime = (
                    self.target.unavailable() if self.target else (self.why or "no execution target is configured")
                )
            return self._no_runtime

    def note(self, level: str, text: str) -> None:
        text = mask_secrets(text)  # a note quotes the first error line of a check ("could not judge: ...")
        with self._lock:
            if (level, text) not in self.notes:
                self.notes.append((level, text))

    def local(self, c: Check, units: Sequence[Unit]) -> Outcome:
        """One local check of the candidate "the tree with `units` removed" (() = the tree as it is)."""
        edits = dict(sorted(self.render(list(units)).items()))
        # the Python check only reads: it gets the pristine copy and the edits as text (no copy of the tree per check); a toolchain
        # check works in a directory of its own (the build writes next to the sources), so it gets a clone with the edits written over
        cand = None if c.builtin == "pycheck" else self.ws.candidate(edits)  # type: ignore[union-attr]
        try:
            return self._py(c, self.ws.base, units, edits) if cand is None else self._tool(c, cand, edits)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - a check never raises: it cannot judge
            return Outcome(None, f"{c.name} failed to run ({type(exc).__name__}: {exc})")
        finally:
            if cand is not None:
                self.ws.drop(cand)  # type: ignore[union-attr]

    def _py(self, c: Check, tree: str, units: Sequence[Unit], edits: Dict[str, str]) -> Outcome:
        if not edits:
            return Outcome(True)
        req = {
            "tree": tree,
            "files": {p: {"old": self.texts[p], "new": t} for p, t in edits.items()},
            "removed": [{"path": u.path, "qual": u.items[0].qual} for u in units],
        }
        py, boxed = self.host.python(), None
        work = tempfile.mkdtemp(prefix="dcc-pychk-", dir=os.path.dirname(tree))
        try:
            script = os.path.abspath(pycheck.__file__)
            if py:
                dirs = {n: os.path.join(work, n) for n in ("home", "tmp", "out")}
                for d in dirs.values():
                    os.mkdir(d, 0o700)
                reqfile = os.path.join(dirs["out"], "req.json")
                with open(reqfile, "w", encoding="utf-8") as fh:
                    json.dump(req, fh)
                env = {"PATH": PATH, "HOME": dirs["home"], "TMPDIR": dirs["tmp"], "LANG": "C.UTF-8"}
                ro = [os.path.dirname(script), tree, *_tool_dirs(py, sys.base_prefix)]
                boxed = self.host.box([py, "-I", script, reqfile], dirs, env, _ro_outside(ro, dirs), tree)
            if (
                boxed is None
            ):  # no box on this host: the same code in this process (it parses and compiles, nothing runs)
                self.note(
                    "I",
                    f"{c.name} ran in this process: no bwrap box on this host (it parses and compiles, nothing runs)",
                )
                res = pycheck.check(req)
            else:
                rc, so, se = self.host.run(boxed, tree, float(self.spec["local"]["timeout_s"]), {})
                try:
                    res = json.loads(so.decode("utf-8", "replace"))
                except ValueError:
                    return Outcome(None, f"{_first_error('', se.decode('utf-8', 'replace'))} (rc {rc})")
            return Outcome(True) if res["ok"] else Outcome(False, str(res["errors"][0])[:MAX_LINE])
        finally:
            _rmtree(work)

    def _tool(self, c: Check, cand: str, edits: Dict[str, str]) -> Outcome:
        exe = self.host.resolve(c.argv[0])
        if not exe:
            return Outcome(None, f"{c.argv[0]} was not found (the engines directories and PATH are searched)")
        dirs_rel = _package_dirs(cand, sorted(edits) or self.paths, c.marker) if c.marker else [""]
        if not dirs_rel:
            return Outcome(None, f"no {c.marker} above the changed files")
        work = tempfile.mkdtemp(prefix="dcc-tool-", dir=os.path.dirname(cand))
        try:
            dirs = {"tree": cand, "home": os.path.join(work, "home"), "tmp": os.path.join(work, "tmp")}
            os.mkdir(dirs["home"], 0o700)
            os.mkdir(dirs["tmp"], 0o700)
            extra, note = self.host.offline_env(dict(c.env), c.offline, self.opts)
            if note:
                self.note("I", note)
            tool_dirs = _tool_dirs(exe)
            path = os.pathsep.join([*tool_dirs[:1], PATH])  # `#!/usr/bin/env node` of a tsc found next to its node
            env = {"PATH": path, "HOME": dirs["home"], "TMPDIR": dirs["tmp"], "LANG": "C.UTF-8", **extra}
            if c.offline == "go":
                env["GOCACHE"] = os.path.join(dirs["tmp"], "gocache")
            ro = [*([env["GOMODCACHE"]] if env.get("GOMODCACHE") else []), *tool_dirs]
            for rel in dirs_rel:
                cwd = os.path.normpath(os.path.join(cand, rel))
                boxed = self.host.box([exe, *c.argv[1:]], dirs, env, _ro_outside(ro, dirs), cwd)
                if boxed is None:
                    return Outcome(None, f"no bwrap box on this host: {c.name} is not run without one")
                rc, so, se = self.host.run(boxed, cwd, float(self.spec["local"]["timeout_s"]), {})
                if rc != 0:
                    return Outcome(
                        False, _first_error(so.decode("utf-8", "replace"), se.decode("utf-8", "replace"), cand)
                    )
            return Outcome(True)
        except subprocess.TimeoutExpired:
            return Outcome(None, f"{c.name} did not finish in {self.spec['local']['timeout_s']} s")
        finally:
            _rmtree(work)

    # -- the runtime check: only through the target
    def runtime(self, c: Check, units: Sequence[Unit]) -> Outcome:
        edits = tuple(sorted(self.render(list(units)).items()))
        dirs = (
            _package_dirs(self.ws.base if self.ws else self.root, [p for p, _ in edits] or self.paths, c.marker)
            if c.marker
            else [""]
        )
        if not dirs:
            return Outcome(None, f"no {c.marker} above the changed files")
        job = RuntimeJob(
            self.root, c.lang, c.name, c.argv, c.marker, edits, int(self.spec["runtime"]["timeout_s"]), tuple(dirs)
        )
        try:
            r = self.target.run(job)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            return Outcome(None, f"the target failed ({type(exc).__name__}: {exc})")
        if not r.ran:
            return Outcome(None, r.error or "the target did not run the check")
        return Outcome(True) if r.rc == 0 else Outcome(False, (r.error or f"{c.name} exited {r.rc}")[:MAX_LINE])

    # -- the search
    def _pool(self, subsets: List[Tuple[Unit, ...]], run: Callable, klass: str) -> List[Outcome]:
        def one(i: int) -> Outcome:
            try:
                return run(subsets[i])
            except Exception as exc:  # noqa: BLE001
                return Outcome(None, f"{type(exc).__name__}: {exc}")

        wanted = self.workers or (self.spec["runtime"].get("parallel") if klass == "runtime" else None)
        return par.run_units(list(range(len(subsets))), one, jobs=par.workers(wanted), key=lambda i: i, kind="thread")

    def _batch(self, subsets: List[Tuple[Unit, ...]], run: Callable, klass: str) -> List[Optional[Outcome]]:
        """One round: the outcomes of `subsets` in order; None for those the budget of the class does not allow."""
        b = self.left[klass]
        n = min(len(subsets), b["checks"]) if b["rounds"] > 0 else 0
        if n <= 0:
            return [None] * len(subsets)
        b["rounds"] -= 1
        b["checks"] -= n
        self.runs += n
        return [*self._pool(subsets[:n], run, klass), *([None] * (len(subsets) - n))]

    def search(self, units: List[Unit], c: Check, run: Callable) -> Tuple[List[Unit], Dict[Unit, str], str]:
        """(passed, {dropped unit: why}, the reason nothing could be judged, "" when something could)."""
        first = self._batch(([()] if c.baseline else []) + [tuple(units)], run, c.klass)
        if c.baseline:
            base = first[0]
            if base is None:
                return [], {}, f"no budget for {c.name} on the tree as it is"
            if base.ok is not True:
                return (
                    [],
                    {},
                    (f"{c.name} fails on the tree as it is: {base.error}" if base.ok is False else base.error),
                )
        whole = first[-1]
        if whole is None:
            return [], {}, f"no budget for {c.name}"
        if whole.ok is None:
            return [], {}, whole.error
        if whole.ok:
            return list(units), {}, ""
        err0 = whole.error
        if len(units) == 1:
            return [], {units[0]: err0}, ""
        groups: List[List[Unit]] = []
        for u in units:
            if groups and groups[-1][0].path == u.path:
                groups[-1].append(u)
            else:
                groups.append([u])

        def unproven(o: Optional[Outcome]) -> str:
            how = f"could not judge it ({o.error})" if o else "was out of budget"
            return f"not proven: {c.name} {how}; the whole patch fails: {err0}"

        dropped: Dict[Unit, str] = {}
        passed: List[Unit] = []
        proven = {frozenset()}  # the sets of units that are known to pass
        split: List[Unit] = []
        if len(groups) == 1:
            split = groups[0]
        else:
            for g, o in zip(groups, self._batch([tuple(g) for g in groups], run, c.klass)):
                if o is not None and o.ok:
                    passed += g
                    proven.add(frozenset(g))
                elif o is not None and o.ok is False and len(g) == 1:
                    dropped[g[0]] = o.error
                elif o is not None and o.ok is False:
                    split += g
                else:
                    dropped.update({u: unproven(o) for u in g})
        if split:
            for u, o in zip(split, self._batch([(u,) for u in split], run, c.klass)):
                if o is not None and o.ok:
                    passed.append(u)
                    proven.add(frozenset([u]))
                elif o is not None and o.ok is False:
                    dropped[u] = o.error
                else:
                    dropped[u] = unproven(o)
        keep, more = self._compose(sorted(passed, key=lambda u: (u.path, u.first)), c, run, proven)
        dropped.update(more)
        return keep, dropped, ""

    def _compose(self, keep: List[Unit], c: Check, run: Callable, proven: set) -> Tuple[List[Unit], Dict[Unit, str]]:
        """The survivors checked together; the first one that breaks them is dropped, until they pass (bounded by the class budget).

        What comes back is always a set that was seen to pass (in `proven`, or the last run of this loop): when the budget ends in the
        middle of the search only the longest proven prefix stays, and a prefix is proven only by a run of exactly that set, so the
        first survivor of a group that passed as a whole does not count as passing alone."""
        dropped: Dict[Unit, str] = {}
        while keep and frozenset(keep) not in proven:
            (o,) = self._batch([tuple(keep)], run, c.klass)
            if o is not None and o.ok:
                break
            if o is None or o.ok is None:
                how = f"could not judge ({o.error})" if o else "was out of budget"
                dropped.update({u: f"not proven together: {c.name} {how}" for u in keep})
                return [], dropped
            # keep[:lo] is proven to pass (the empty set always is; the first survivor only when it was run on its own,
            # not when it passed as a member of a group), keep[:hi] is seen to fail
            lo, hi, err = (1 if frozenset(keep[:1]) in proven else 0), len(keep), o.error
            while hi - lo > 1:
                mid = (lo + hi) // 2
                (m,) = self._batch([tuple(keep[:mid])], run, c.klass)
                if m is None or m.ok is None:  # the bound is spent: keep[:lo] is proven, the rest is not and goes
                    dropped.update(
                        {
                            u: f"not proven together: the search for the breaking removal ran out of {c.klass} budget"
                            for u in keep[lo:]
                        }
                    )
                    return keep[:lo], dropped
                if m.ok:
                    lo = mid
                    proven.add(frozenset(keep[:mid]))
                else:
                    hi, err = mid, m.error
            culprit = keep[hi - 1]
            dropped[culprit] = (
                f"breaks {c.name} together with the removals before it: {err}"
                if hi > 1
                else f"fails {c.name} on its own: {err}"
            )
            keep = keep[: hi - 1] + keep[hi:]
        return keep, dropped

    def language(self, lang: str, units: List[Unit], out: Dict[Unit, Tuple[str, str, str, str]]) -> None:
        """out[unit] = (status, check names, class, detail) for the units of one language."""
        cs = checks(lang, self.spec)
        self.paths = sorted({u.path for u in units})
        passes: Dict[Unit, List[Check]] = {}
        failed: Dict[Unit, Tuple[Check, str]] = {}
        todo, reasons = list(units), []
        if "local" in cs:
            c = cs["local"]
            passed, dropped, why = self.search(todo, c, lambda s, c=c: self.local(c, s))
            failed.update({u: (c, e) for u, e in dropped.items()})
            todo = [u for u in todo if u not in dropped]
            if why:
                reasons.append(why)
            for u in passed:
                passes[u] = [c]
        else:
            reasons.append(f"no non-executing check for {lang}")
        if "runtime" in cs:
            c = cs["runtime"]
            if self.no_runtime:
                self.skipped = True
                reasons.append("no runtime target (see the runtime mode note)")  # the exact line is in the notes, once
            elif todo:
                passed, dropped, why = self.search(todo, c, lambda s, c=c: self.runtime(c, s))
                failed.update({u: (c, e) for u, e in dropped.items()})
                if why:
                    reasons.append(why)
                    self.note("I", f"{lang}: the runtime check could not judge: {why}")
                for u in passed:
                    passes[u] = [*passes.get(u, []), c]
        else:
            reasons.append(f"no runtime check for {lang}")
        for u in units:
            if u in failed:
                out[u] = (FAILED, failed[u][0].name, failed[u][0].klass, failed[u][1])
            elif u in passes:
                last = passes[u][-1]
                out[u] = (
                    VERIFIED_RUNTIME if last.klass == "runtime" else VERIFIED_LOCAL,
                    " + ".join(x.name for x in passes[u]),
                    last.klass,
                    "",
                )
            else:
                out[u] = (UNVERIFIED, "", "", "; ".join(dict.fromkeys(reasons)))


def verify(
    root: str,
    units: Sequence[Unit],
    render: Callable[[Sequence[Unit]], Dict[str, str]],
    texts: Dict[str, str],
    opts: Optional[Options] = None,
    host: Optional[Host] = None,
    spec: Optional[dict] = None,
    workers: Optional[int] = None,
) -> Report:
    """The report for the removals `units` (see the module docstring). Reads the tree, writes only a private temporary directory."""
    opts, host, spec = opts or Options(), host or Host(), spec or table()
    v = Verifier(root, render, texts, opts, host, spec, workers)
    order = sorted(units, key=lambda u: (u.path, u.first, u.last))
    out: Dict[Unit, Tuple[str, str, str, str]] = {}
    if order:
        scratch = tempfile.mkdtemp(prefix="dcc-fixv-", dir=host.tmp_base)
        try:
            v.ws = Workspace(root, host, scratch)
            if v.ws.skipped_links:
                n, shown = len(v.ws.skipped_links), ", ".join(v.ws.skipped_links[:3])
                v.note(
                    "I",
                    f"{n} tracked symlink(s) not copied for the checks (absolute, or they leave the tree; a check must not read host files through them): "
                    + shown
                    + (", ..." if n > 3 else ""),
                )
            for lang in sorted({u.lang for u in order}):
                v.language(lang, [u for u in order if u.lang == lang], out)
        finally:
            _rmtree(scratch)
        if v.skipped:
            v.note("I", f"{skip_line(v.no_runtime)}; tests and build checks were not run")
    rows: List[Row] = []
    for u in order:
        st, name, klass, detail = out[u]
        for k, it in enumerate(u.items):
            rows.append(
                Row(
                    it.path,
                    it.line,
                    it.qual,
                    it.code,
                    st,
                    name,
                    klass,
                    (
                        mask_secrets(detail) if k == 0 else f"goes with {u.items[0].qual}"
                    ),  # whatever a check or a target said
                    getattr(it, "conf", -1),
                )
            )
    kept = tuple(u for u in order if out[u][0] != FAILED)
    langs = tuple(sorted({u.lang for u in order}))
    return Report(kept, tuple(sorted(rows, key=lambda r: (r.path, r.line, r.qual))), tuple(v.notes), v.runs, langs)
