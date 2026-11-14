"""graph.pages: the overview page of all projects of a data root, <data root>/index.html (file name: data/pages.json).

    write_index(base, now=None) -> str      write the page, return its path
    collect(base) -> [Row]                  one Row per project directory that has a stored run
    render(rows, now, page_dir) -> str      the html text of the rows (links relative to page_dir)
    note_repo(project, root)                repo.json: the path and name of the repository of a project directory
    remember(project, run_name, summary)    summaries.json: the numbers of a run (graph.viz.summary_doc)
    run(layout) -> int                      `graph pages`: write the page and print its file:// link

One row per project (the directories of <base>/projects): the name and path (repo.json, written by every stored run; a directory of
older runs has none and the row says so), the last branch run and the last repo run, each as a link to its page and to its report.md
with its date, head, and E / W / suspect counts, and the languages of the graph. The numbers of a run come from summaries.json
next to the runs (remembered when the run is stored); a run that has none is read once from its findings.json and, for the
languages, from the head of its graph.json.gz when that is at most `summary_gz_max` bytes (a bigger one has no languages and the
cell says why), and the result is stored there. summaries.json keeps no entry of a run that is gone.

The page is static: plain html and css, no script, no network, no font or image of another origin (a Content-Security-Policy of
`default-src 'none'` says so), links relative to the page, so it opens from file:// and moves with the data directory. It lists local
paths of the machine: it says so, and is not published. The same input gives the same page (the only clock is `now`, in the
footer). Written by `graph pages` and refreshed after every stored run (graph.artifacts.housekeeping); never the reason a check
fails.
"""

from __future__ import annotations

import gzip
import html
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import quote

from . import artifacts, paths, viz

TEMPLATE = os.path.join(viz.VIEWER, "pages.html")
CACHE, NOTE = "summaries.json", "repo.json"
PAGE_ORDER = (
    "change.html",
    "index.html",
)  # the page a run opens: the change subgraph of a branch run, else the whole graph


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write(path: str, text: str) -> None:
    tmp = f"{path}.tmp{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- what is known about a project and its runs
def note_repo(project: str, root: str) -> None:
    """repo.json of a project directory: where the repository is (rewritten only when it changed)."""
    doc = {"path": os.path.abspath(root), "name": os.path.basename(os.path.abspath(root)) or os.path.abspath(root)}
    if _read_json(os.path.join(project, NOTE)) != doc:
        _write(os.path.join(project, NOTE), json.dumps(doc, sort_keys=True) + "\n")


def _cache(project: str) -> Dict[str, dict]:
    doc = _read_json(os.path.join(project, CACHE))
    return {k: v for k, v in doc.items() if isinstance(v, dict) and v.get("v") == 1} if isinstance(doc, dict) else {}


def _store(project: str, cache: Dict[str, dict]) -> None:
    alive = {r.name for r in artifacts.list_runs(artifacts.runs_dir(project))}
    doc = {k: v for k, v in sorted(cache.items()) if k in alive}
    if _read_json(os.path.join(project, CACHE)) != doc:
        _write(os.path.join(project, CACHE), json.dumps(doc, sort_keys=True) + "\n")


def remember(project: str, run_name: str, summary: dict) -> None:
    cache = _cache(project)
    cache[run_name] = summary
    _store(project, cache)


def derive(run_path: str, gz_max: int) -> Tuple[Optional[dict], str]:
    """(summary, why) of a run that has none: from findings.json, the languages from graph.json.gz when it is small enough."""
    doc = _read_json(os.path.join(run_path, "findings.json"))
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
        return None, "findings.json cannot be read"
    pairs = [(f.get("level"), f.get("code")) for f in doc["findings"] if isinstance(f, dict)]
    langs, gz = None, os.path.join(run_path, "graph.json.gz")
    try:
        if os.path.getsize(gz) <= gz_max:
            with gzip.open(gz, "rt", encoding="utf-8") as fh:
                langs = json.load(fh)["head"]["langs"]
        else:
            return viz.summary_doc(pairs, doc.get("meta"), None), f"graph.json.gz is over {gz_max} bytes: no languages"
    except (OSError, ValueError, KeyError, TypeError):
        return viz.summary_doc(pairs, doc.get("meta"), None), "graph.json.gz cannot be read: no languages"
    return viz.summary_doc(pairs, doc.get("meta"), langs), ""


class RunRow(NamedTuple):
    name: str
    scope: str
    ts: float
    head: str
    pages: Tuple[Tuple[str, str], ...]  # (label, path of the file)
    summary: Optional[dict]
    why: str  # what is missing in `summary`


class Row(NamedTuple):
    project: str  # the directory name <name>-<hash>
    name: str
    path: str  # "" = not recorded
    branch: Optional[RunRow]
    repo: Optional[RunRow]
    langs: Optional[Tuple[str, ...]]
    langs_why: str


def _run_row(r: artifacts.RunDir, cache: Dict[str, dict], cfg: dict, got: Dict[str, dict]) -> RunRow:
    why, summary = "", cache.get(r.name)
    if summary is None:
        summary, why = derive(r.path, cfg["index"]["summary_gz_max"])
        if summary is not None:
            summary = {**summary, "langs_why": why} if why else summary
            got[r.name] = summary
    elif summary.get("langs") is None:
        why = summary.get("langs_why") or "the languages of this run were not recorded"
    page = next((n for n in PAGE_ORDER if os.path.isfile(os.path.join(r.path, n))), "")
    pages = ((("page", os.path.join(r.path, page)),) if page else ()) + (
        (("report", os.path.join(r.path, "report.md")),) if os.path.isfile(os.path.join(r.path, "report.md")) else ()
    )
    head = r.name.split("-", 2)[2] if r.name.count("-") >= 2 else ""
    return RunRow(r.name, r.scope, r.ts, head, pages, summary, why)


def collect(base: str) -> List[Row]:
    """The rows of the data root `base`, sorted by name then project directory."""
    cfg = artifacts.settings()
    return sorted(
        (
            _row_of(p, cfg)
            for p in artifacts.project_dirs(base)
            if artifacts.list_runs(artifacts.runs_dir(p))
        ),
        key=lambda r: (r.name, r.project),
    )


def _row_of(project: str, cfg: dict) -> Row:
    cache, got = _cache(project), {}
    runs = [(r, _run_row(r, cache, cfg, got)) for r in artifacts.list_runs(artifacts.runs_dir(project))]
    if got:  # a summary that had to be derived is kept, so the next page does not read that run again
        try:
            _store(project, {**cache, **got})
        except OSError:
            got = {}  # a project directory that cannot be written: the page is still right, only slower next time
    runs.sort(key=lambda x: (x[0].ts, x[0].seq, x[0].name))
    branch = next((x for r, x in reversed(runs) if r.scope == "branch"), None)
    repo = next((x for r, x in reversed(runs) if r.scope == "repo"), None)
    known = [x for x in (repo, branch) if x]
    pick = next((x.summary["langs"] for x in known if x.summary and x.summary.get("langs") is not None), None)
    why = next((x.why for x in known if x.why), "no run has a summary")
    note = _read_json(os.path.join(project, NOTE))
    note = note if isinstance(note, dict) else {}
    rid = os.path.basename(project)
    return Row(
        rid,
        note.get("name") or f"project {rid[:12]}",
        note.get("path") or "",
        branch,
        repo,
        None if pick is None else tuple(pick),
        "" if pick is not None else why,
    )


# ---------------------------------------------------------------- the page
def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(ts))


def _url(path: str, page_dir: str) -> str:
    return quote(os.path.relpath(path, page_dir).replace(os.sep, "/"), safe="/")


def _counts(s: Optional[dict], why: str) -> str:
    if s is None:
        return f'<span class="muted">no numbers: {html.escape(why)}</span>'
    e, w, u = s["E"], s["W"], s["suspect"]
    return f'<span class="n{" bad" if e else ""}" title="policy findings">E {e}</span> <span class="n">W {w}</span> <span class="n muted">suspect {u}</span>'


def _run_cell(r: Optional[RunRow], page_dir: str, what: str) -> str:
    if r is None:
        return f'<span class="muted">no {what} yet</span>'
    links = (
        " ".join(f'<a href="{_url(p, page_dir)}">{html.escape(lb)}</a>' for lb, p in r.pages)
        or '<span class="muted">no page</span>'
    )
    return f'<div>{links}</div><div class="muted">{_utc(r.ts)} {html.escape(r.head)}</div><div>{_counts(r.summary, r.why)}</div>'


def _row(r: Row, page_dir: str) -> str:
    where = html.escape(r.path) if r.path else '<span class="muted">path not recorded (the next run records it)</span>'
    langs = (
        (html.escape(", ".join(r.langs)) if r.langs else '<span class="muted">none</span>')
        if r.langs is not None
        else f'<span class="muted">{html.escape(r.langs_why)}</span>'
    )
    return (
        f'<tr><th scope="row"><div>{html.escape(r.name)}</div><div class="muted path">{where}</div></th>'
        f"<td>{_run_cell(r.branch, page_dir, 'branch run')}</td><td>{_run_cell(r.repo, page_dir, 'repo run')}</td>"
        f'<td class="langs">{langs}</td></tr>'
    )


def render(rows: Sequence[Row], now: float, page_dir: str) -> str:
    """The html of the rows; links are relative to `page_dir`, the directory the page is written to."""
    body = (
        "\n".join(_row(r, page_dir) for r in rows)
        or '<tr><td colspan="4" class="muted">no run is stored yet: run `dead-code-checker graph check --scope branch` in a repository</td></tr>'
    )
    vals = {"COUNT": str(len(rows)), "ROWS": body, "NOW": _utc(now)}
    with open(TEMPLATE, encoding="utf-8") as fh:
        return re.sub(r"@@(\w+)@@", lambda m: vals[m.group(1)], fh.read())


def write_index(base: str, now: Optional[float] = None) -> str:
    """Write <base>/index.html (the name is data/pages.json "index.file") and return its path."""
    cfg = artifacts.settings()["index"]
    page_dir = str(base)
    os.makedirs(page_dir, exist_ok=True)
    now = time.time() if now is None else now
    path = os.path.join(page_dir, cfg["file"])
    _write(path, render(collect(page_dir), now, page_dir))
    return path


def run(layout: paths.Layout) -> int:
    """`graph pages`: write the overview page of this data directory and print its link; exit 0 (a page that cannot be written
    is a W on standard output and exit 0 too: nothing of ours is a failed command for the caller)."""
    try:
        path = write_index(str(layout.base))
    except Exception as exc:  # noqa: BLE001
        print(
            f"dead-code-checker graph pages: W dc.pages-failed: the overview page could not be written ({type(exc).__name__}: {exc})"
        )
        return 0
    print(f"dead-code-checker graph pages: {Path(path).as_uri()}")
    return 0
