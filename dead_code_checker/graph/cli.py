"""graph.cli: `dead-code-checker graph ...`.

    dead-code-checker graph doctor [--root DIR] [--out DIR] [--jobs N] [--json] [ENGINE OPTIONS] [--all] [--versions] [--strict] [--go-cache DIR]
    dead-code-checker graph check --scope branch|repo [--base REF] [--root DIR] [--out DIR] [--no-viz] [--plain] [--jobs N]
                                  [--show-suspects] [--min-confidence N] [--format text|json|md|sarif | --json | --md] [--timing]
                                  [ENGINE OPTIONS]
    dead-code-checker graph fix --dry-run [--scope branch|repo] [--code CODE]... [--path GLOB]... [--root DIR] [--base REF]
                                          [--verify [--host HOST]] [--min-confidence N] [--jobs N] [--json] [ENGINE OPTIONS]
    dead-code-checker graph view [--root DIR] [--out DIR] [--plain] [--jobs N] [ENGINE OPTIONS]
    dead-code-checker graph why NODE [--root DIR] [ENGINE OPTIONS]
    dead-code-checker graph tree NODE [--root DIR] [--callers | --callees] [--depth N] [--width N] [--json | --svg [--out FILE]] [ENGINE OPTIONS]
    dead-code-checker graph publish [--root DIR] [--out DIR] [--gh-pages] [--dry-run] [--deny-file FILE] [ENGINE OPTIONS]
    dead-code-checker graph prune [--dry-run | --apply] [--keep N] [--days D] [--root DIR]
    dead-code-checker graph pages [--root DIR]
    dead-code-checker graph lock --out FILE [--engines-dir DIR] [--no-path] [--lock FILE]
    dead-code-checker graph index [--root DIR] [--jobs N] [--only ENGINE,...] [--timeout S] [--go-cache DIR] [--no-sandbox] [ENGINE OPTIONS]
    dead-code-checker graph cache go (--check | --fill) REPO... [--dir DIR] [--json]
    dead-code-checker graph runtime [--root DIR] [--host HOST] [--classes] [--json]

ENGINE OPTIONS: `--engines-dir DIR` (directories separated like PATH; searched before PATH; `--no-path` searches only them) and
`--lock FILE` (sha256 pins: an engine runs only with the pinned bytes); check, fix, view, why, tree and publish also take `--go-cache DIR`.
The same are read from $DEAD_CODE_CHECKER_ENGINES_DIR / _LOCK.

doctor prints what a run would use and writes nothing: the repository, its data directory (and which rule chose it), the
configuration (file, variables, problems), the worker count, the tools around the checker (gitleaks for the code fragments of the
pages, Graphviz for the pictures, bwrap for a sandbox) and one row per analysis engine that applies to the languages of the
repository (`--all`: every engine of the registry) with its status (ok | missing | untrusted | sha-mismatch | degraded), the
install hint of this OS and whether the lock pins it; `--versions` asks each found engine for its version. Exit 0; 1 when the
configuration has an E; 2 for a usage error, when no data directory can be used, when the lock cannot be read, or with `--strict`
when an engine is not usable. The one warning about the data directory goes to standard error.

check: branch = merge-base(HEAD, upstream) .. work tree (graph.levels); repo = the whole tree (every file counts as added, so a dead
symbol of the tree is reported as the codes of the branch level are, as W: nothing blocks on what was already there). Output is text
with the why of every finding, or --format json | md | sarif (SARIF 2.1.0, graph.sarif); --json and --md are the short forms. Exit 1
when a finding is E, 2 for a usage or data-directory error, 0 otherwise; a check that fails itself is a W dc.check-failed and exit 0,
never a traceback. A dead-symbol claim that no index or engine corroborates is a suspect (dc.suspect, level I, graph.verdict): hidden
in the text, json, md and sarif output with a count line, shown by --show-suspects (findings.json and the viewer always have them).
Every dead-symbol claim has a numeric confidence 0..100 (graph.confidence, data/confidence.json): a second axis that never changes a
level or a code; text, md, json, sarif (properties.confidence, rank), findings.json and the viewer show it. --min-confidence N leaves
out of the printed text, json, md and sarif the claims below N (display only: the exit code, findings.json, report.md and the viewer of
the run keep them; a finding without a confidence is never left out; a line says how many); N is an integer 0..100, anything else is a
usage error (exit 2).
fix --dry-run prints, on standard output, a unified diff that removes the confirmed dead symbols (graph.fix: Python functions, methods and
classes whose exact span is checked against the source; whatever it cannot cut exactly is listed as manual on standard error). It never
writes the tree and never commits; without --dry-run it refuses (exit 2). The scopes are those of check; a claim no index or engine
corroborates (a suspect) is never removed. --min-confidence N removes only claims of at least N. --verify applies the patch to a COPY of
the tree and runs, per language, the cheapest check that can show a removal unsafe: non-executing checks here (Python: compile, scopes and
imports, in the bubblewrap box when there is one; Go: vet; TypeScript: tsc --noEmit, only in the box), executing ones (tests, builds) only
on a configured remote execution target (--host HOST), else skipped with one line. A patch that fails is split by file, then by symbol;
the output patch keeps what was not shown unsafe and every symbol gets a status (verified-local, verified-runtime, unverified, failed).
--json adds "verify".
Both scopes write their artifacts (index.html, modules.svg, graph.json.gz, report.md, findings.json, sarif.json; the branch level also
change.html and change.svg) to <data dir>/runs/<utc>-<scope>-<head8>/ with a `latest` pointer, or under --out DIR, and print the
links; --no-viz skips them, --plain makes the pages carry the data pack without deflate. view = the same for the whole tree, without
the findings of a check. why: why-alive or why-dead of one node (a key, or a `path#qual` / `qual` suffix). tree: the callers and
callees of a node. publish: names and counts, never source code, as files of the repository (graph.publish). prune: old runs of
every project of the data root (a dry run unless --apply). pages: the overview page of the data root.

lock writes a lock file with the sha256 (and the version) of every engine found now, for this platform; the other platforms of an
existing `--lock` file are kept. index fills the semantic cache (graph.semindex). cache go checks and fills the offline Go module
cache (graph.gocache).

runtime runs the tests (or the configured command) of the project under a tracer on ONE separate ssh host and stores the symbols that
ran (graph.runtime): the host is `host` of `[graph.runtime]` when $DEAD_CODE_CHECKER_RUNTIME_HOSTS also names it, or --host HOST (which
skips that list); this machine is refused and nothing ever runs here. With no usable target it prints ONE line and exits 0 (so does a
run that failed); 2 is a usage error or a project that has not opted in. --classes prints which checks execute project code (the
target only) and which only read it. `check` then reads the newest stored run of the project (it never contacts the target): a symbol
that ran is not reported dead (I dc.alive-runtime) and the confidence of a claim about it goes down.

The engine layer plugs in through `_steps` (graph.steps.default: the semantic cache and the engines of the branch level); the engine
setup (`--engines-dir`, `--lock`, `--no-path`) is fixed once in `main`, a lock that cannot be read is a usage error (exit 2).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .. import safepath
from . import artifacts, config, engines, gocache, levels, par, paths, publish, report, rtcfg, safety, sandbox, semindex, snippets, viz
from . import steps as _engine_steps
from . import pages as _pages
from . import tree as _tree
from .model import Finding, sort_findings

FORMATS = ("text", "json", "md", "sarif")
ENGINE_CMDS = ("check", "fix", "view", "why", "tree", "publish")  # the commands that take the engine options and run the levels


def _engine_args(p: argparse.ArgumentParser, go_cache: bool = False) -> None:
    p.add_argument("--engines-dir", help="directories (separated like PATH) searched for engines before PATH")
    p.add_argument("--lock", help="sha256 pins of the engines: an engine runs only with the pinned bytes")
    p.add_argument("--no-path", action="store_true", help="look for engines only in --engines-dir, not on PATH")
    if go_cache:
        p.add_argument("--go-cache", help="the offline Go module cache (default $%s or the data directory)" % gocache.ENV_DIR)


def _setup(a: argparse.Namespace) -> Optional[str]:
    """Fix the engine setup of this process from the options (the environment fills what is not given). A lock that cannot be read is
    a usage error: the text is returned, None when all is well."""
    engines.configure(a.lock, a.engines_dir, path=not a.no_path)
    try:
        engines.cached_lock()
    except ValueError as e:
        return str(e)
    return None


def _engine_rows(a: argparse.Namespace, lay: paths.Layout) -> List[dict]:
    reg = engines.load_registry()
    files = None if a.all else engines.project_files(str(lay.root))
    langs = None if a.all else engines.project_langs(reg, files)
    return engines.doctor(reg, engines.cached_lock(), langs=langs, files=files, versions=a.versions)


def _steps(a: argparse.Namespace, root: str, lay: Optional[paths.Layout] = None) -> Optional[levels.Steps]:
    """The layers that plug into the levels: the semantic cache and the engines (graph.steps). The engine setup was fixed in main."""
    opts = _engine_steps.Opts(
        base=str(lay.base) if lay is not None else None, layout=lay, go_cache=getattr(a, "go_cache", None)
    )
    return _engine_steps.default(opts)


# ---------------------------------------------------------------- tools around the checker
def tools() -> dict:
    """What is installed around the checker: name -> {"path": ..., "role": ...} (path None = not installed), read-only."""
    bw = safepath.which("bwrap") or None
    return {
        "gitleaks": {"path": snippets.find_gitleaks(), "role": "code fragments in the pages and the publish scan"},
        "graphviz-dot": {"path": viz.dot_path(), "role": "the pictures (without it a built-in emitter draws them)"},
        "bwrap": {
            "path": bw if viz.netless_prefix() else None,
            "role": "a sandbox without network for what runs outside the checker (without it: unsandboxed)",
        },
    }


def _tool_lines(t: dict) -> List[str]:
    out = []
    for name, d in t.items():
        if d["path"]:
            out.append(f"tool:      {name}: {d['path']}")
        elif name == "gitleaks":
            out.append(f"tool:      {name}: not installed; the pages carry no code fragments ({snippets.NOTE})")
        elif name == "graphviz-dot":
            out.append(f"tool:      {name}: not installed; the built-in emitter draws the SVG files ({viz.INSTALL_DOT})")
        else:
            out.append(f"tool:      {name}: not available; whatever runs outside the checker runs unsandboxed")
    return out


# ---------------------------------------------------------------- shared
def _repo(a: argparse.Namespace, need_git: bool = True) -> Tuple[Optional[str], Optional[paths.Layout], int]:
    """(root, layout, exit code): the repository of --root (default: the one of the current directory) and its data layout."""
    root = paths.find_root(getattr(a, "root", None) or ".")
    if need_git and not (root / ".git").exists():
        print("dead-code-checker graph: not inside a git repository (use --root)", file=sys.stderr)
        return None, None, 2
    try:
        lay = paths.resolve(root, getattr(a, "out", None) if getattr(a, "cmd", "") in ("check", "view", "doctor") else None)
    except paths.DataDirError as exc:
        print(f"dead-code-checker: {exc}", file=sys.stderr)
        return None, None, 2
    if lay.warning:
        print(lay.warning, file=sys.stderr)
    return str(root), lay, 0


def _jobs(a: argparse.Namespace) -> Optional[int]:
    """--jobs: validated here, handed to the passes through $DEAD_CODE_CHECKER_JOBS (the one place par.workers reads)."""
    if getattr(a, "jobs", None) is None:
        return None
    n = par.workers(a.jobs)
    os.environ[par.ENV_JOBS] = str(n)
    return n


def _link_line(meta: dict) -> str:
    return f"dead-code-checker: viewer {Path(meta['viewer']).as_uri()}" if meta.get("viewer") else ""


def _publish_stale(root: str, fs: Sequence[Finding], meta: dict, sink: dict) -> List[Finding]:
    """For a project that has published its architecture: ONE W dc.publish-stale (a stored digest, no render)."""
    if meta.get("disabled"):
        return list(fs)
    return sort_findings([*fs, *_guard(publish.stale, root, sink)])


def _guard(fn, *args) -> List[Finding]:
    try:
        return list(fn(*args))
    except Exception as exc:  # noqa: BLE001 - a failure of ours is a notice, never a block
        return [Finding(*safety.check_failed("publish", exc))]


# ---------------------------------------------------------------- check
def _scope_repo(root: str, layout: paths.Layout, steps: Optional[levels.Steps], sink: dict) -> levels.Report:
    from . import repo

    return repo.check(root, sink=sink, steps=steps, layout=layout)


def _check(a: argparse.Namespace) -> int:
    root, lay, rc = _repo(a)
    if root is None or lay is None:
        return rc
    _jobs(a)
    steps = _steps(a, root, lay)
    sink: dict = {}
    t0 = time.monotonic()
    if a.scope == "branch":
        rep = levels.branch(root, a.base, sink=sink, steps=steps, layout=lay)
    else:
        rep = _scope_repo(root, lay, steps, sink)
    fs, meta = list(rep.findings), dict(rep.meta)
    meta["show_suspects"] = a.show_suspects
    if a.scope == "branch":
        fs = _publish_stale(root, fs, meta, sink)
    if not (meta.get("disabled") or a.no_viz):
        try:
            budget = config.load(root).budget("branch")
        except Exception:  # noqa: BLE001 - a config that cannot be read: the default
            budget = levels.BRANCH_S
        run = artifacts.publish(lay, root, a.scope, fs, meta, sink, plain=a.plain, deadline=t0 + budget)
        fs = run.findings
    if a.min_confidence:  # after the artifacts: report.md, findings.json and the pages of the run keep every finding
        meta["min_confidence"] = a.min_confidence
    fmt = a.format
    if fmt == "json":
        print(report.to_json(fs, meta, a.timing))
    elif fmt == "md":
        print(report.to_md(fs, meta))
    elif fmt == "sarif":
        from . import sarif

        sys.stdout.write(sarif.to_sarif(fs, meta))
    else:
        print(report.to_text(fs, meta))
        if meta.get("viewer"):
            sys.stdout.write(_link_line(meta) + "\n")
            sys.stdout.write(artifacts.link_line(a.scope, fs, meta["links"], meta["dir"]) + "\n")
    return 1 if any(f.level == "E" for f in fs) else 0


def _runtime_cmd(a: argparse.Namespace) -> int:
    from . import runtime

    if a.classes:
        return runtime.run_cli(a, None)
    root, lay, rc = _repo(a)
    if root is None or lay is None:
        return rc
    return runtime.run_cli(a, root, lay)


def _fix_cmd(a: argparse.Namespace) -> int:
    from . import fix

    return fix.run(a)


def _view(a: argparse.Namespace) -> int:
    root, lay, rc = _repo(a)
    if root is None or lay is None:
        return rc
    _jobs(a)
    notes: list = []
    b, res, proof = levels.analyzed_proof(root, notes, _steps(a, root, lay), lay)
    meta = {
        "scope": "repo",
        "files": len(b.tree.files),
        "nodes": len(b.graph.nodes),
        "edges": len(b.graph.edges),
        "roots": len(b.graph.roots),
        "verdicts": res.counts(),
    }
    fs = list(b.findings) + notes
    sink = {
        "graph": b.graph,
        "res": res,
        "tree": b.tree,
        "suspects": levels.suspect_keys(b.graph, res, proof),
        "facts": levels.facts_getter(b, str(lay.store)),
    }
    run = artifacts.publish(lay, root, "repo", fs, meta, sink, plain=a.plain)
    sys.stdout.write((_link_line(meta) or "dead-code-checker graph view: nothing written") + "\n")
    for f in run.findings:
        if f.code.startswith(("dc.viz", "dc.graphviz", "dc.viewer")):
            print(f.render(), file=sys.stderr)
    return 1 if any(f.level == "E" for f in run.findings) else 0


# ---------------------------------------------------------------- doctor
def _runtime_allowed(cfg: config.Config) -> bool:
    return bool(cfg.runtime.host) and rtcfg.host_allowed(cfg.runtime.host)


def _runtime_row(cfg: config.Config) -> str:
    """The one `runtime:` row of doctor: off, or the host of the file and whether the user allows it. Nothing is contacted."""
    r = cfg.runtime
    if not r.enabled:
        return "off"
    if not r.host:
        return "on, no host in the file (the host is given on the command line)"
    if _runtime_allowed(cfg):
        return f"on, host {r.host} (allowed by {rtcfg.ENV_HOSTS})"
    return f"on, host {r.host} (not allowed: name it in {rtcfg.ENV_HOSTS} or pass it on the command line)"


def _doctor(a: argparse.Namespace) -> int:
    root = paths.find_root(a.root or ".")
    try:
        lay = paths.resolve(root, a.out)
        jobs = par.workers(a.jobs)
    except (paths.DataDirError, ValueError) as e:
        print(f"dead-code-checker: {e}", file=sys.stderr)
        return 2
    bad = _setup(a)
    if bad:
        print(f"dead-code-checker: {bad}", file=sys.stderr)
        return 2
    cfg = config.load(str(lay.root))
    if lay.warning:
        print(lay.warning, file=sys.stderr)
    t = tools()
    rows = _engine_rows(a, lay)
    setup, mode = engines.current(), sandbox.detect()
    go_dir, go_why = gocache.state(lay.base, a.go_cache)
    lock_note = (
        f"pinned by {setup.lock} (the bytes of each engine, not what it reads or runs)"
        if setup.lock
        else "no lock: engines are used as found (--lock FILE pins them by sha256)"
    )
    if a.json:
        doc = {
            "repo": {"root": str(lay.root), "id": lay.repo_id},
            "data": {
                "source": lay.source,
                "root": str(lay.base),
                "project": str(lay.project),
                "runs": str(lay.runs),
                "store": str(lay.store),
                "ledger": str(lay.ledger),
                "index": str(lay.index),
            },
            "config": {**cfg.as_dict(), "runtime_host_allowed": _runtime_allowed(cfg)},
            "tools": t,
            "workers": jobs,
            "engines": {
                "lock": setup.lock or None,
                "dirs": list(setup.dirs),
                "sandbox": {"kind": mode.kind, "note": mode.note},
                "go_cache": {"dir": go_dir, "usable": not go_why, "why": go_why},
                "rows": rows,
            },
        }
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        print(f"repo:      {lay.root} ({lay.repo_id})")
        print(f"data dir:  {lay.project} (chosen by: {lay.source})")
        print(f"runs:      {lay.runs}")
        print(f"index:     {lay.index}")
        print(
            f"config:    {cfg.source or 'none'}: mode {cfg.mode}, {'on' if cfg.enabled else 'off'}, "
            f"budget commit {cfg.budget('commit'):g}s branch {cfg.budget('branch'):g}s, "
            f"viewer code {'on' if cfg.viewer_code else 'off'} ({cfg.viewer_code_bytes} bytes), {len(cfg.waives)} waive"
        )
        print(f"runtime:   {_runtime_row(cfg)}")
        print(f"workers:   {jobs}")
        for line in _tool_lines(t):
            print(line)
        print(f"engines:   {lock_note}; engines dir: {os.pathsep.join(setup.dirs) or 'none (PATH only)'}")
        print(f"sandbox:   {mode.kind}" + (f" ({mode.note})" if mode.note else ""))
        print(f"go cache:  {go_dir} ({'usable' if not go_why else go_why})")
        for r in rows:
            line = f"  {r['engine']:<26} {r['status']:<12} {','.join(r['roles'])}: {','.join(r['langs'])}"
            if r["status"] == "ok":
                line += f"  {r['path']}" + (f" {r['version']}" if r.get("version") else "")
                line += "" if r["pinned"] in (None, True) else "  (not pinned)"
            else:
                line += f"  {r['detail']}; {r['install']}"
            print(line)
        for p in cfg.problems:
            print(p.render())
    code = 1 if any(p.level == "E" for p in cfg.problems) else 0
    return code or engines.exit_code(rows, a.strict)


# ---------------------------------------------------------------- the other commands
def _why(a: argparse.Namespace) -> int:
    root, lay, rc = _repo(a)
    if root is None:
        return rc
    print(levels.why(root, a.node, _steps(a, root, lay), lay))
    return 0


def _tree_cmd(a: argparse.Namespace) -> int:
    root, lay, rc = _repo(a)
    if root is None:
        return rc
    return _tree.run(a, root, _steps(a, root, lay), lay)


def _publish_cmd(a: argparse.Namespace) -> int:
    root, lay, rc = _repo(a)
    if root is None:
        return rc
    return publish.run(a, root, _steps(a, root, lay), lay)


def _prune(a: argparse.Namespace) -> int:
    root, lay, rc = _repo(a, need_git=False)
    if root is None or lay is None:
        return rc
    try:
        r = artifacts.prune(lay.base, a.keep, a.days, dry_run=not a.apply)
    except ValueError as exc:
        print(f"dead-code-checker graph prune: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - a cleanup of ours never fails the caller
        print(f"dead-code-checker graph prune: W dc.prune-failed: old runs could not be cleaned up ({type(exc).__name__}: {exc})")
        return 0
    print("\n".join(r.lines()))
    if r.dry_run and r.removed:
        print("dead-code-checker graph prune: dry run, nothing was removed; --apply removes these runs")
    return 0


def _pages_cmd(a: argparse.Namespace) -> int:
    root, lay, rc = _repo(a, need_git=False)
    if root is None or lay is None:
        return rc
    return _pages.run(lay)


def _lock_cmd(a: argparse.Namespace) -> int:
    engines.configure(None, a.engines_dir, path=not a.no_path)
    reg = engines.load_registry()
    base = None
    if a.lock and os.path.exists(a.lock):
        try:
            base = engines.load_lock(a.lock)
        except ValueError as e:
            print(f"dead-code-checker: {e}", file=sys.stderr)
            return 2
    found = engines.lock_rows(reg, versions=not a.no_versions)
    lock = engines.make_lock(found, base=base)
    try:
        engines.write_lock(a.out, lock)
    except OSError as e:
        print(f"dead-code-checker: cannot write {a.out}: {e.strerror or e}", file=sys.stderr)
        return 2
    print(f"dead-code-checker: {len(found)} engines pinned for {engines.platform_key()} in {a.out}")
    return 0


def _index_cmd(a: argparse.Namespace) -> int:
    from . import jobs

    root = paths.find_root(a.root or ".")
    try:
        lay = paths.resolve(root, None)
        workers = par.workers(a.jobs)
    except (paths.DataDirError, ValueError) as e:
        print(f"dead-code-checker: {e}", file=sys.stderr)
        return 2
    bad = _setup(a)
    if bad:
        print(f"dead-code-checker: {bad}", file=sys.stderr)
        return 2
    if lay.warning:
        print(lay.warning, file=sys.stderr)
    if a.no_sandbox:
        sandbox.set_mode(sandbox.Mode("none", (), sandbox.NOTE_NONE))
    only = tuple(x for x in (a.only or "").split(",") if x) or None
    st = jobs.Settings(timeout_s=a.timeout, only=only, workers=workers, base=str(lay.base), go_cache=a.go_cache)
    r = semindex.index_repo(str(lay.root), st, lay)
    print(semindex.render(str(lay.root), r))
    return 0


def _cache_cmd(a: argparse.Namespace) -> int:
    base = None
    try:
        if a.repos and os.path.isdir(a.repos[0]):
            base = paths.resolve(paths.find_root(a.repos[0]), None).base
    except paths.DataDirError:
        base = None
    engines.configure(None, None)
    return gocache.run(a, base)


def _percent(text: str) -> int:
    """argparse type of --min-confidence: an integer 0..100 (anything else is a usage error, exit 2)."""
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if not 0 <= n <= 100:
        raise argparse.ArgumentTypeError(f"{n} is not between 0 and 100")
    return n


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dead-code-checker graph", description="The reachability-graph checker.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser, out: bool = False) -> None:
        p.add_argument("--root", help="repository (default: the one that contains the current directory)")
        if out:
            p.add_argument("--out", type=Path, help="directory for the run artifacts (outside the repository)")

    jobs_help = f"workers (default ${par.ENV_JOBS} or the CPUs, at most {par.MAX_DEFAULT})"

    p = sub.add_parser("doctor", help="print the data directory, the configuration, the tools and the engines a run would use")
    common(p, out=True)
    p.add_argument("--jobs", type=int, help=jobs_help)
    p.add_argument("--json", action="store_true", help="as JSON")
    p.add_argument("--all", action="store_true", help="every engine of the registry, not only those of the repository's languages")
    p.add_argument("--versions", action="store_true", help="ask each engine found for its version")
    p.add_argument("--go-cache", help="the offline Go module cache (default $%s or the data directory)" % gocache.ENV_DIR)
    p.add_argument("--strict", action="store_true", help="exit 2 when an engine is missing, not in the lock or does not match it")
    _engine_args(p)

    c = sub.add_parser("check", help="the dead-code checks of one level, with the artifacts of the run")
    c.add_argument("--scope", choices=("branch", "repo"), required=True)
    c.add_argument("--base", help="--scope branch: compare with this ref (default: upstream, origin/HEAD, main, master)")
    common(c, out=True)
    c.add_argument("--no-viz", action="store_true", help="do not write the html and svg artifacts")
    c.add_argument("--plain", action="store_true", help="pages carry the data pack without deflate (no DecompressionStream needed)")
    c.add_argument("--jobs", type=int, help=jobs_help)
    c.add_argument("--show-suspects", action="store_true", help="show the dc.suspect findings (dead claims nothing corroborates)")
    c.add_argument(
        "--min-confidence",
        type=_percent,
        default=0,
        metavar="N",
        help="leave out the dead-symbol claims whose confidence (0..100) is under N (display only: findings.json keeps them; a finding without a confidence is never left out)",
    )
    c.add_argument("--format", choices=FORMATS, default="text", help="the output (default text)")
    c.add_argument("--json", action="store_const", dest="format", const="json", help="--format json")
    c.add_argument("--md", action="store_const", dest="format", const="md", help="--format md")
    c.add_argument("--timing", action="store_true", help="with json: add a separate top-level timing block (not byte-stable)")
    _engine_args(c, go_cache=True)

    fx = sub.add_parser("fix", help="a patch (for review) that removes the confirmed dead symbols; never writes the tree")
    fx.add_argument("--dry-run", action="store_true", help="print the patch (required: nothing else is supported)")
    fx.add_argument("--scope", choices=("branch", "repo"), default="branch", help="the findings of the branch check (default) or of the whole tree")
    fx.add_argument("--code", action="append", help="only findings of this code (repeatable), e.g. dc.introduced-dead")
    fx.add_argument("--path", action="append", help="only files matching this glob or lying under this directory (repeatable)")
    fx.add_argument("--base", help="--scope branch: compare with this ref (default: upstream, origin/HEAD, main, master)")
    common(fx)
    fx.add_argument("--jobs", type=int, help=jobs_help)
    fx.add_argument(
        "--verify",
        action="store_true",
        help="check the removals on a copy of the tree (non-executing checks here; tests and builds only on a configured execution target) and keep what is not shown unsafe",
    )
    fx.add_argument("--host", help="--verify: the ssh host of the execution target (it must be allowed by $DEAD_CODE_CHECKER_RUNTIME_HOSTS)")
    fx.add_argument("--user", help="--verify: the ssh login (default: the user@ of the host's entry in $DEAD_CODE_CHECKER_RUNTIME_HOSTS, else your ssh configuration)")
    fx.add_argument("--port", type=int, help="--verify: the ssh port (default: the :port of the host's entry, else your ssh configuration)")
    fx.add_argument(
        "--min-confidence",
        type=_percent,
        default=0,
        metavar="N",
        help="remove only claims whose confidence (0..100) is at least N; the rest is skipped with the reason",
    )
    fx.add_argument("--json", action="store_true", help="one JSON document: patch, removed, manual, skipped (+ verify)")
    _engine_args(fx, go_cache=True)

    v = sub.add_parser("view", help="the whole-tree graph as index.html + modules.svg (the repo level)")
    common(v, out=True)
    v.add_argument("--plain", action="store_true")
    v.add_argument("--jobs", type=int, help=jobs_help)
    _engine_args(v, go_cache=True)

    w = sub.add_parser("why", help="why a node is alive or dead")
    w.add_argument("node")
    common(w)
    _engine_args(w, go_cache=True)

    tr = sub.add_parser("tree", help="the callers and callees tree of a node (the text twin of the viewer's panel)")
    tr.add_argument("node", help="a node key, a path#qual / qual / path suffix")
    common(tr)
    side = tr.add_mutually_exclusive_group()
    side.add_argument("--callers", action="store_true", help="only who calls it (up)")
    side.add_argument("--callees", action="store_true", help="only what it calls (down)")
    tr.add_argument("--depth", type=int, default=_tree.DEPTH, help=f"levels drawn (default {_tree.DEPTH})")
    tr.add_argument("--width", type=int, default=_tree.WIDTH, help=f"edges per level (default {_tree.WIDTH}; the rest is counted)")
    how = tr.add_mutually_exclusive_group()
    how.add_argument("--json", action="store_true")
    how.add_argument("--svg", action="store_true", help="the trees as one SVG picture (Graphviz dot when installed, else built in)")
    tr.add_argument("--out", help="with --svg: the file to write (default: standard output)")
    _engine_args(tr, go_cache=True)

    pb = sub.add_parser("publish", help="the architecture of the project as files (names and counts); scanned before it is written")
    common(pb)
    pb.add_argument("--out", help="the directory inside the repository (default: docs/architecture)")
    pb.add_argument("--gh-pages", action="store_true", help="also commit the interactive page to the LOCAL branch gh-pages; never pushes")
    pb.add_argument("--dry-run", action="store_true", help="render and scan, print what would be written, write nothing")
    pb.add_argument("--deny-file", help="extra words or regular expressions (one per line) that must not appear in the output")
    _engine_args(pb, go_cache=True)

    pn = sub.add_parser("prune", help="remove old runs of the data directory (the keep rule of data/pages.json); a dry run by default")
    common(pn)
    mode = pn.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print what would go and the bytes (the default)")
    mode.add_argument("--apply", action="store_true", help="remove those runs")
    pn.add_argument("--keep", type=int, metavar="N", help="runs kept per project and scope (default: data/pages.json)")
    pn.add_argument("--days", type=float, metavar="D", help="every run younger than D days is kept (default: data/pages.json)")

    rt = sub.add_parser("runtime", help="the project's tests under a tracer on a separate ssh host: the symbols that ran (never run here)")
    common(rt)
    rt.add_argument("--host", help="the ssh host to run on (skips $%s; the host key must be in your known_hosts)" % rtcfg.ENV_HOSTS)
    rt.add_argument("--user", help="the ssh login (default: the user@ of the host's entry in $%s, else your ssh configuration)" % rtcfg.ENV_HOSTS)
    rt.add_argument("--port", type=int, help="the ssh port (default: the :port of the host's entry, else your ssh configuration)")
    rt.add_argument("--classes", action="store_true", help="which checks execute project code (target only) and which only read it; no target needed")
    rt.add_argument("--json", action="store_true", help="as JSON")

    pg = sub.add_parser("pages", help="write the overview page of the data directory and print its link")
    common(pg)

    lk = sub.add_parser("lock", help="write a lock file with the sha256 of the engines found now")
    lk.add_argument("--out", required=True, help="the lock file to write")
    lk.add_argument("--lock", help="an existing lock whose other platforms are kept")
    lk.add_argument("--engines-dir", help="directories (separated like PATH) searched for engines before PATH")
    lk.add_argument("--no-path", action="store_true", help="look for engines only in --engines-dir, not on PATH")
    lk.add_argument("--no-versions", action="store_true", help="do not run the engines to learn their version")

    ix = sub.add_parser("index", help="run the engines and indexers over the repository and keep what they say in the cache")
    ix.add_argument("--root", help="repository (default: the one that contains the current directory)")
    ix.add_argument("--jobs", type=int, help="workers")
    ix.add_argument("--only", help="comma-separated engine names to run")
    ix.add_argument("--timeout", type=float, default=900.0, help="seconds one engine may run (default 900)")
    ix.add_argument("--go-cache", help="the offline Go module cache (default $%s or the data directory)" % gocache.ENV_DIR)
    ix.add_argument("--no-sandbox", action="store_true", help="run build engines and indexers without bubblewrap (one warning says so)")
    _engine_args(ix)

    ca = sub.add_parser("cache", help="offline caches of the engines")
    cs = ca.add_subparsers(dest="family", required=True)
    g = cs.add_parser("go", help="the offline Go module cache: what is missing, the commands that fill it")
    g.add_argument("repos", nargs="*", metavar="REPO")
    g.add_argument("--check", action="store_true", help="list the modules of every go.mod that the cache lacks")
    g.add_argument("--fill", action="store_true", help="print the commands that fill it (run them with network access)")
    g.add_argument("--dir", help="the cache directory (default $%s or the data directory)" % gocache.ENV_DIR)
    g.add_argument("--json", action="store_true", help="as JSON")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    if getattr(a, "jobs", None) is not None:  # a bad --jobs is a usage error, not a failure of the check
        try:
            par.workers(a.jobs)
        except ValueError as exc:
            print(f"dead-code-checker graph {a.cmd}: {exc}", file=sys.stderr)
            return 2
    if a.cmd in ENGINE_CMDS:  # fix the engine setup once; a lock that cannot be read is a usage error, never a check failure
        bad = _setup(a)
        if bad:
            print(f"dead-code-checker: {bad}", file=sys.stderr)
            return 2
    try:
        if a.cmd == "doctor":
            return _doctor(a)
        if a.cmd == "check":
            try:
                return _check(a)
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - the check never breaks the caller: one W, exit 0
                if isinstance(exc, SystemExit) and exc.code in (None, 0, 2):
                    raise
                print(Finding(*safety.check_failed(a.scope, exc)).render())
                return 0
        return {
            "fix": _fix_cmd,
            "view": _view,
            "why": _why,
            "tree": _tree_cmd,
            "publish": _publish_cmd,
            "prune": _prune,
            "runtime": _runtime_cmd,
            "pages": _pages_cmd,
            "lock": _lock_cmd,
            "index": _index_cmd,
            "cache": _cache_cmd,
        }[a.cmd](a)
    except ValueError as exc:  # a bad --jobs and the like: a usage error
        print(f"dead-code-checker graph {a.cmd}: {exc}", file=sys.stderr)
        return 2
