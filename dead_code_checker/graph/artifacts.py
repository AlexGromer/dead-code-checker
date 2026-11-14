"""graph.artifacts: the ONE entry that stores a branch or repo run: <project>/runs/<utc>-<scope>-<head8>/ of the data layout
(graph.paths), or <out>/<utc>-<scope>-<head8>/ with `--out DIR`.

    run = publish(layout, root, scope, fs, meta, sink, plain=False, quiet=False, deadline=None, housekeep=True, now=None)
                                              -> Run(dir, links, line, findings)
    new_dir(layout, root, scope, now=None)    a fresh run directory (0700 in the default place)
    link_line(scope, fs, links, d, why="")    the one line a CLI prints
    prune(layout_or_base, keep_last=None, keep_days=None, now=None, dry_run=True, cap=None) -> Pruned
                                              the cleanup of old runs (`graph prune`)
    housekeeping(layout, d, summary, now=None) -> [Finding]
                                              what runs after a stored run: repo note, summary, cleanup with a cap, overview page

`graph check --scope branch|repo` and `graph view` come through publish(), and graph.viz.write_run is the only writer behind it
(pages, graph.json.gz, report.md, findings.json, sarif.json, the `latest` pointer): the same run writes the same files and prints
the same line. `sink` is what levels.branch fills (graph, res, the tree of the build, marks, suspects). A run whose graph stages
did not finish has nothing to draw: a W (dc.viz-skipped) is among the findings and the line says "no page: <why>". A directory
that cannot be written does not raise: Run.dir is None, the line says "run not stored", a W (dc.viz-failed) is among the findings
and the checks stay the authority. `publish` fills meta (dir, links, viewer) for report.to_md and the --json output.

Cleanup (prune, defaults in data/pages.json): in the runs directory of every project of the data root the last `keep_last` runs of
every scope (branch, repo) are kept, and every run younger than `keep_days` days, and the run that `latest` points to. The age and
the order come from the run directory name (<utc>-<scope>-<head8>[-n]), never from the mtime; a directory whose name is no run name,
a symlink, or a name with an impossible date is left alone. `graph prune` only prints what would go and the bytes unless it is told
to --apply; the automatic cleanup after a stored run removes at most `auto_cap` runs per call and is never the reason a run fails: an
error there is one W (dc.prune-failed) among the run's findings, and so is a failed overview page (dc.pages-failed). A run stored
with `--out` (not under the data root) is neither cleaned nor listed.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple, Union

from . import lens, paths, safety, snippets, store, vcs, verdict, viz
from .model import Finding, sort_findings

PAGES = (".html", ".svg")
NO_GRAPH = "no page: no graph to draw (the graph stages did not finish)"
NO_GRAPH_MSG = "no graph to draw (the graph stages did not finish); no pages were written"

RUN_NAME = re.compile(r"^(\d{8}T\d{6}Z)-([a-z]+)-([0-9a-f]{1,64}|nohead)(?:-(\d+))?$")
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pages.json")
LAYOUT_CACHE = "layout-cache"  # in the project directory: the cache of the graph view's layout (deletable)


def settings() -> dict:
    """data/pages.json (prune and index defaults); a file that cannot be read is an error, not a silent default."""
    with open(DATA, encoding="utf-8") as fh:
        return json.load(fh)


def project_dirs(base: Union[str, Path]) -> List[str]:
    """The project directories (<base>/projects/<name>-<hash>) of a data root, sorted; symlinks are not projects."""
    root = os.path.join(str(base), "projects")
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(root, n)
        if os.path.isdir(p) and not os.path.islink(p):
            out.append(p)
    return out


def runs_dir(project: str) -> str:
    return os.path.join(project, "runs")


class RunDir(NamedTuple):
    name: str
    path: str
    ts: float  # UTC seconds from the name
    scope: str
    seq: int  # the -n suffix of a second run in the same second


def run_ts(stamp: str) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(stamp, "%Y%m%dT%H%M%SZ")))
    except ValueError:
        return None


def list_runs(rdir: str) -> List[RunDir]:
    """The runs of one runs directory, oldest first (date, then suffix, then name); names that are no run name, symlinks and files
    are not runs."""
    out: List[RunDir] = []
    try:
        names = sorted(os.listdir(rdir))
    except OSError:
        return out
    for n in names:
        m, p = RUN_NAME.match(n), os.path.join(rdir, n)
        ts = run_ts(m.group(1)) if m else None
        if ts is not None and os.path.isdir(p) and not os.path.islink(p):
            out.append(RunDir(n, p, ts, m.group(2), int(m.group(4) or 0)))
    return sorted(out, key=lambda r: (r.ts, r.seq, r.name))


def latest_target(rdir: str) -> str:
    """The run name that `latest` points to (a symlink, or the one-line pointer file where there are no symlinks), "" if none."""
    link = os.path.join(rdir, "latest")
    try:
        if os.path.islink(link):
            return os.path.basename(os.readlink(link))
        with open(link, encoding="utf-8") as fh:
            return os.path.basename(fh.readline().strip())
    except OSError:
        return ""


def _bytes(path: str) -> int:
    n = 0
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            try:
                n += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                continue  # gone or unreadable meanwhile: it counts for nothing
    return n


class Pruned(NamedTuple):
    removed: Tuple[Tuple[str, int], ...]  # (run directory, bytes) of the runs that went (dry run: that would go)
    kept: int
    deferred: int  # runs over the keep rule that the `cap` left for the next cleanup
    errors: Tuple[str, ...]
    dry_run: bool

    @property
    def total(self) -> int:
        return sum(b for _, b in self.removed)

    def lines(self) -> List[str]:
        verb = "would remove" if self.dry_run else "removed"
        out = [f"{verb} {p} ({b} bytes)" for p, b in self.removed]
        more = f"; {self.deferred} more for the next cleanup (cap)" if self.deferred else ""
        out.append(
            f"dead-code-checker graph prune: {verb} {len(self.removed)} run(s), {self.total} bytes; kept {self.kept}{more}"
        )
        return out + [f"dead-code-checker graph prune: error: {e}" for e in self.errors]


def prune(
    base: Union[str, Path],
    keep_last: Optional[int] = None,
    keep_days: Optional[float] = None,
    *,
    now: Optional[float] = None,
    dry_run: bool = True,
    cap: Optional[int] = None,
) -> Pruned:
    """Remove the runs of the data root `base` that the keep rule (module docstring) does not keep, oldest first; `dry_run` (the
    default) removes nothing. keep_last / keep_days default to data/pages.json. `cap`: at most that many runs go in this call
    (the rest is `deferred`)."""
    cfg = settings()["prune"]
    keep_last = cfg["keep_last"] if keep_last is None else keep_last
    keep_days = cfg["keep_days"] if keep_days is None else keep_days
    if keep_last < 0 or keep_days < 0:
        raise ValueError("keep_last and keep_days must not be negative")
    now = time.time() if now is None else now
    young, kept, gone = now - keep_days * 86400, 0, []
    for proj in project_dirs(base):
        rd = runs_dir(proj)
        if os.path.islink(rd) or not os.path.isdir(rd):
            continue
        runs, by_scope = list_runs(rd), {}
        for r in runs:
            by_scope.setdefault(r.scope, []).append(r)
        keep = {r.name for rs in by_scope.values() for r in rs[max(len(rs) - keep_last, 0) :]}
        keep |= {r.name for r in runs if r.ts > young} | {latest_target(rd)}
        kept += sum(r.name in keep for r in runs)
        gone += [r for r in runs if r.name not in keep]
    gone.sort(key=lambda r: (r.ts, r.seq, r.path))
    take = gone if cap is None else gone[: max(cap, 0)]
    removed, errors = [], []
    for r in take:
        size = _bytes(r.path)
        if not dry_run:
            try:
                shutil.rmtree(r.path)
            except FileNotFoundError:  # removed meanwhile by another cleanup: gone is what was wanted
                removed.append((r.path, size))
                continue
            except OSError as exc:
                errors.append(f"{r.path}: {exc}")
                continue
        removed.append((r.path, size))
    return Pruned(tuple(removed), kept, len(gone) - len(take), tuple(errors), dry_run)


class Run(NamedTuple):
    dir: Optional[str]
    links: Dict[str, str]  # page name -> file:// url
    line: str
    findings: List[Finding]  # the checks' findings plus the W ones about the artifacts (sorted)


def run_name(scope: str, head: str = "", now: Optional[float] = None) -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}-{scope}-{(head or 'nohead')[:8]}"


def new_dir(layout: paths.Layout, root: str, scope: str, now: Optional[float] = None) -> str:
    """A fresh run directory in layout.runs; a second run in the same second gets a -1, -2 ... suffix."""
    stem = run_name(scope, vcs.head_commit(root) or "", now)
    base = str(layout.runs)
    paths.ensure(layout)
    if layout.out is None:
        store._secure_dir(base)  # the default place: ours, 0700; a directory the caller chose with --out is left as it is
    for i in range(100):
        d = os.path.join(base, stem if i == 0 else f"{stem}-{i}")
        try:
            os.mkdir(d, 0o700 if layout.out is None else 0o777)
            return d
        except FileExistsError:
            continue
    raise store.StoreError(f"no free run directory for {stem}")


def _w(code: str, msg: str, why: str) -> Finding:
    return Finding("W", safety.WHERE, 0, code, msg, why)


def _snippets(root: str, sink: dict, deadline: Optional[float] = None) -> snippets.Snips:
    """The code of the symbols for the pages (graph.snippets: budget, gitleaks, [graph] viewer_code). Whatever goes wrong is a W
    (dc.viewer-code-off) and a page without code: the pages are never the reason a check fails. `deadline` (time.monotonic()):
    what the reading and the gitleaks run may use; when it passes the page has no code (reason "budget")."""
    try:
        from . import config

        if sink.get("tree") is None:
            return snippets.off("the graph has no file tree to check the code against")
        return snippets.collect(
            root,
            sink["graph"],
            sink["res"],
            sink["tree"],
            sink.get("marks"),
            sink.get("suspects", ()),
            config.load(root),
            deadline=deadline,
        )
    except Exception as exc:  # noqa: BLE001 - a failure here is a finding, not a crash
        return snippets.off(f"collecting the code failed ({type(exc).__name__}: {exc})")


def _lens(sink: dict) -> lens.Lens:
    """The data of the viewer lenses (graph.lens: duplicates, architecture, health). Whatever goes wrong is a lens that is off with the
    reason, never a failed run."""
    try:
        return lens.collect(sink["graph"], sink["res"], sink.get("facts"), sink.get("suspects", ()))
    except Exception as exc:  # noqa: BLE001 - a failure here is a finding, not a crash
        return lens.off(f"computing the lenses failed ({type(exc).__name__}: {exc})")


def _head(scope: str, fs: Sequence[Finding]) -> str:
    e, w = (sum(f.level == k for f in fs) for k in "EW")
    s = sum(f.code == verdict.SUSPECT for f in fs)
    info = sum(f.level == "I" for f in fs) - s
    return f"dead-code-checker: {scope}: {e} E, {w} W" + (f", {s} suspect(s)" if s else "") + (f", {info} info" if info else "")


_HOUSEKEEPING = threading.Lock()  # one housekeeping at a time per process


def housekeeping(layout: paths.Layout, d: str, summary: Optional[dict], now: Optional[float] = None) -> List[Finding]:
    """What follows a stored run (below), one caller at a time per process."""
    with _HOUSEKEEPING:
        return _housekeeping(layout, d, summary, now)


def _housekeeping(layout: paths.Layout, d: str, summary: Optional[dict], now: Optional[float]) -> List[Finding]:
    """What follows a stored run `d` (in the runs directory of the data root): the note of the repository and the numbers of the run
    for the overview page, the cleanup of old runs with the auto_cap of data/pages.json, and the overview page itself. Cheap, and
    never the reason a run fails: every step that goes wrong is one W (dc.prune-failed, dc.pages-failed) in the returned list, the
    others still run."""
    from . import pages

    fs: List[Finding] = []

    def step(code: str, what: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a failure here is a finding, not a crash
            fs.append(
                _w(code, f"{what} ({type(exc).__name__}: {exc})", "the run is stored; only this housekeeping step failed")
            )

    def note() -> None:
        pages.note_repo(str(layout.project), str(layout.root))
        if summary is not None:
            pages.remember(str(layout.project), os.path.basename(d), summary)

    def clean() -> None:
        r = prune(layout.base, now=now, dry_run=False, cap=settings()["prune"]["auto_cap"])
        if r.errors:
            raise OSError(f"{len(r.errors)} run(s) could not be removed, the first: {r.errors[0]}")

    step("dc.pages-failed", "the note for the overview page could not be written", note)
    step("dc.prune-failed", "old runs could not be cleaned up", clean)
    step("dc.pages-failed", "the overview page could not be written", lambda: pages.write_index(layout.base, now))
    return fs


def publish(
    layout: paths.Layout,
    root: str,
    scope: str,
    fs: Sequence[Finding],
    meta: dict,
    sink: dict,
    *,
    plain: bool = False,
    deadline: Optional[float] = None,
    quiet: bool = False,
    housekeep: bool = True,
    now: Optional[float] = None,
) -> Run:
    """Store the run in a fresh directory of layout.runs with graph.viz.write_run; `plain` is the pages' --plain. meta gets dir and
    links (and viewer when there is a page). Without a graph (sink has no res) nothing is stored and the run says so. `deadline` (a
    time.monotonic() value) bounds the code of the pages (graph.snippets). `quiet`: what the pages say about themselves that is no
    news (a cut page, dc.viz-truncated) is level I, still in report.md. A run stored in the default place (no --out) is followed by
    housekeeping(): the overview page, the cleanup of old runs; their failures join the findings as W. `housekeep=False` skips it."""
    fs, links, why = list(fs), {}, ""
    if "res" not in sink:
        why = NO_GRAPH
        fs.append(_w("dc.viz-skipped", NO_GRAPH_MSG, "the viewer needs the linked graph and its verdicts"))
        return Run(None, {}, f"{_head(scope, fs)}; {why}", sort_findings(fs))
    try:
        d = new_dir(layout, root, scope, now)
        art = viz.write_run(
            d,
            scope,
            sink["graph"],
            sink["res"],
            fs,
            meta,
            marks=sink.get("marks"),
            plain=plain,
            suspects=sink.get("suspects", ()),
            snippets=_snippets(root, sink, deadline),
            lens=_lens(sink),
            quiet=quiet,
            layout_dir=str(layout.project / LAYOUT_CACHE),
        )
        d, fs = art.dir, fs + list(art.findings)
        links = {n: Path(d, n).as_uri() for n in art.files if n.endswith(PAGES)}
        meta["viewer"] = art.viewer
    except (OSError, ValueError, store.StoreError, paths.DataDirError) as exc:
        msg = f"the pages could not be written ({type(exc).__name__}: {exc})"
        fs.append(_w("dc.viz-failed", msg, "the check itself finished; only the artifacts are missing"))
        return Run(None, {}, f"{_head(scope, fs)}; run not stored ({exc})", sort_findings(fs))
    meta.update(dir=d, links=links)
    if layout.out is None and housekeep:  # a run stored in a directory of the caller's is not part of the data root
        fs = fs + housekeeping(layout, d, art.summary, now)
    fs = sort_findings(fs)
    return Run(d, links, link_line(scope, fs, links, d, why), fs)


def link_line(scope: str, fs: Sequence[Finding], links: Dict[str, str], d: str, why: str = "") -> str:
    pages = " ".join(links[k] for k in sorted(links)) or why or "no page"
    return f"{_head(scope, fs)}; {pages} ; report {Path(d, 'report.md').as_uri()}"
