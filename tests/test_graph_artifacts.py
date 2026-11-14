"""graph.artifacts and graph.pages: where a run is stored (the data layout), the one line it prints, the cleanup of old runs, and
the overview page of the data root.

python3 -m pytest -q tests/test_graph_artifacts.py

Runs are made from a toy repository (tests/graph_support.py) and a data root under tmp_path; the pages are made without Graphviz and
without gitleaks (`no_tools`), so nothing here depends on what is installed.
"""

import json
import os
import re
import shutil
import time
from pathlib import Path

import pytest
from graph_support import new_repo, write

from dead_code_checker.graph import artifacts, levels, pages, paths, snippets, viz

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.usefixtures("hermetic_git", "data_root", "dcref"),
]
CHAIN = "def link_a\nref link_b\ndef link_b\nref link_c\ndef link_c\n"


@pytest.fixture(autouse=True)
def no_tools(monkeypatch):
    monkeypatch.setattr(viz, "dot_path", lambda: None)
    monkeypatch.setattr(snippets, "find_gitleaks", lambda: None)


@pytest.fixture
def repo(tmp_path):
    r = new_repo(tmp_path, {"lib/app.dcref": "def run\ndef main\nref run\nroot main main\n", "run.dcroots": "main file:lib/app.dcref\n"})
    write(r, "lib/chain.dcref", CHAIN)
    return r


def stored(repo, scope="branch", out=None, **kw):
    lay = paths.resolve(paths.find_root(repo), out)
    sink: dict = {}
    rep = levels.branch(str(repo), "HEAD", sink=sink, layout=lay)
    meta = dict(rep.meta)
    run = artifacts.publish(lay, str(repo), scope, list(rep.findings), meta, sink, **kw)
    return lay, run, meta


# ================================================================ a stored run
def test_a_run_is_stored_in_the_runs_directory_of_the_project_and_linked(repo):
    lay, run, meta = stored(repo, now=1_700_000_000)
    d = Path(run.dir)
    assert d.parent == lay.runs and re.fullmatch(r"20231114T221320Z-branch-[0-9a-f]{8}", d.name)
    assert (d.stat().st_mode & 0o777) == 0o700
    assert {p.name for p in d.iterdir()} >= {"index.html", "change.html", "modules.svg", "change.svg", "graph.json.gz", "report.md", "findings.json", "sarif.json"}
    assert (lay.runs / "latest").resolve() == d.resolve()
    assert meta["dir"] == str(d) and meta["viewer"].endswith("change.html") and set(run.links) == {"index.html", "modules.svg", "change.html", "change.svg"}
    assert run.line.startswith("dead-code-checker: branch: 0 E, 2 W, 3 suspect(s)") and d.joinpath("report.md").as_uri() in run.line
    assert not list(Path(repo).rglob("*.html"))  # nothing is written into the analysed repository


def test_a_second_run_in_the_same_second_gets_a_suffix(repo):
    lay, a, _ = stored(repo, now=1_700_000_000)
    _, b, _ = stored(repo, now=1_700_000_000)
    assert Path(b.dir).name == Path(a.dir).name + "-1"


def test_housekeeping_writes_the_note_the_summary_and_the_overview_page(repo):
    lay, run, _ = stored(repo)
    assert json.loads((lay.project / "repo.json").read_text()) == {"name": "repo", "path": str(repo.resolve())}
    summ = json.loads((lay.project / "summaries.json").read_text())
    assert summ[Path(run.dir).name]["suspect"] == 3 and summ[Path(run.dir).name]["langs"] == ["dcref"]
    page = lay.index.read_text()
    assert lay.index.parent == lay.base and f"runs/{Path(run.dir).name}/change.html" in page and "suspect 3" in page


def test_out_stores_under_the_directory_the_caller_chose_and_touches_nothing_else(repo, tmp_path):
    out = tmp_path / "art"
    lay, run, _ = stored(repo, out=out)
    assert Path(run.dir).parent == out and (out / "latest").exists()
    assert not lay.index.exists() and not (lay.project / "summaries.json").exists()


def test_without_a_graph_nothing_is_stored_and_the_line_says_why(repo):
    lay = paths.resolve(paths.find_root(repo))
    meta: dict = {}
    run = artifacts.publish(lay, str(repo), "branch", [], meta, {})
    assert run.dir is None and run.line.endswith(artifacts.NO_GRAPH) and [f.code for f in run.findings] == ["dc.viz-skipped"]
    assert not lay.runs.exists()


def test_a_directory_that_cannot_be_written_is_a_warning_not_a_crash(repo, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file")
    lay = paths.resolve(paths.find_root(repo), blocker / "sub")
    sink: dict = {}
    rep = levels.branch(str(repo), "HEAD", sink=sink)
    run = artifacts.publish(lay, str(repo), "branch", list(rep.findings), {}, sink)
    assert run.dir is None and "run not stored" in run.line and "dc.viz-failed" in {f.code for f in run.findings}


def test_a_failing_housekeeping_step_is_a_warning_and_the_run_stays(repo, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(pages, "write_index", boom)
    lay, run, _ = stored(repo)
    assert run.dir and any(f.code == "dc.pages-failed" and "disk full" in f.msg for f in run.findings)


# ================================================================ prune
def mk_run(project, name, size=10, latest=None):
    d = Path(project) / "runs" / name
    d.mkdir(parents=True)
    (d / "report.md").write_text("x" * size)
    if latest:
        (Path(project) / "runs" / "latest").symlink_to(name)
    return d


def stamp(days_ago, scope="branch", head="abcd1234", now=1_800_000_000):
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now - days_ago * 86400)) + f"-{scope}-{head}"


NOW = 1_800_000_000


def base_with(tmp_path, *names, scope_names=()):
    base = tmp_path / "base"
    proj = base / "projects" / "app-12345678"
    for n in names:
        mk_run(proj, n)
    return base, proj


def test_prune_keeps_the_last_n_per_scope_the_young_and_what_latest_points_to(tmp_path):
    names = [stamp(d) for d in (90, 80, 70, 60)] + [stamp(d, "repo") for d in (90, 80)] + [stamp(1)]
    base, proj = base_with(tmp_path, *names)
    (proj / "runs" / "latest").symlink_to(stamp(90))
    r = artifacts.prune(base, keep_last=1, keep_days=14, now=NOW, dry_run=True)
    gone = sorted(Path(p).name for p, _ in r.removed)
    # branch: the last one (1, also the young) stays, plus the one latest points to (90); repo: the last one (80) stays
    assert gone == sorted([stamp(80), stamp(70), stamp(60), stamp(90, "repo")]) and r.dry_run and r.kept == 3 and r.total == 40
    assert all((proj / "runs" / n).exists() for n in names)  # a dry run removes nothing
    r = artifacts.prune(base, keep_last=1, keep_days=14, now=NOW, dry_run=False)
    assert not any((proj / "runs" / Path(p).name).exists() for p, _ in r.removed) and r.errors == ()


def test_prune_leaves_alone_what_is_not_a_run(tmp_path):
    base, proj = base_with(tmp_path, stamp(90), stamp(80))
    (proj / "runs" / "notes").mkdir()
    (proj / "runs" / "20269999T000000Z-branch-abcd").mkdir()  # an impossible date
    real = tmp_path / "elsewhere"
    real.mkdir()
    (proj / "runs" / stamp(100)).symlink_to(real)  # a symlink is no run
    r = artifacts.prune(base, keep_last=0, keep_days=0, now=NOW, dry_run=False)
    assert sorted(Path(p).name for p, _ in r.removed) == sorted([stamp(90), stamp(80)])
    assert (proj / "runs" / "notes").is_dir() and real.is_dir() and (proj / "runs" / stamp(100)).is_symlink()


def test_the_cap_limits_one_cleanup_and_the_rest_is_deferred(tmp_path):
    base, _ = base_with(tmp_path, *[stamp(d) for d in range(100, 90, -1)])
    r = artifacts.prune(base, keep_last=0, keep_days=0, now=NOW, dry_run=False, cap=3)
    assert len(r.removed) == 3 and r.deferred == 7
    assert [Path(p).name for p, _ in r.removed] == [stamp(d) for d in (100, 99, 98)]  # oldest first


def test_prune_rejects_negative_limits_and_ignores_a_missing_data_root(tmp_path):
    with pytest.raises(ValueError):
        artifacts.prune(tmp_path, keep_last=-1)
    assert artifacts.prune(tmp_path / "nothing", now=NOW).removed == ()


# ================================================================ the overview page
def test_the_overview_has_a_row_per_project_with_relative_links_and_no_script(repo, tmp_path):
    lay, run, _ = stored(repo)
    stored(repo, scope="repo")
    html = lay.index.read_text()
    assert "default-src 'none'" in html and "<script" not in html and "http" not in html.replace("http-equiv", "")
    assert html.count("<tr>") == 2  # the header row is in <thead>, one project below
    assert 'href="projects/' in html and str(lay.base) not in html.replace(str(repo.resolve()), "")
    assert "last repo run" in html and "dcref" in html and "no repo run yet" not in html


def test_a_run_without_a_summary_is_read_from_its_files_once(repo):
    lay, run, _ = stored(repo)
    (lay.project / "summaries.json").unlink()
    rows = pages.collect(str(lay.base))
    assert len(rows) == 1 and rows[0].branch.summary["suspect"] == 3 and rows[0].langs == ("dcref",)
    assert json.loads((lay.project / "summaries.json").read_text())  # remembered


def test_a_hostile_name_is_escaped_and_an_empty_root_says_what_to_do(tmp_path):
    base = tmp_path / "b"
    proj = base / "projects" / "x-1"
    mk_run(proj, stamp(1))
    (proj / "repo.json").write_text(json.dumps({"name": "<img src=x onerror=1>", "path": "/p/<b>"}))
    text = pages.render(pages.collect(str(base)), time.time(), str(base))
    assert "<img" not in text and "&lt;img" in text and "<b>" not in text
    empty = pages.render([], time.time(), str(base))
    assert "no run is stored yet" in empty and "@@" not in empty


def test_pages_is_deterministic_for_a_fixed_clock(repo):
    lay, _, _ = stored(repo)
    rows = pages.collect(str(lay.base))
    assert pages.render(rows, 1.0, str(lay.base)) == pages.render(rows, 1.0, str(lay.base))


def test_run_pages_prints_the_link_and_never_fails(repo, capsys, monkeypatch):
    lay = paths.resolve(paths.find_root(repo))
    assert pages.run(lay) == 0 and Path(lay.index).as_uri() in capsys.readouterr().out
    monkeypatch.setattr(pages, "write_index", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert pages.run(lay) == 0 and "dc.pages-failed" in capsys.readouterr().out
    assert os.path.exists(lay.index)
