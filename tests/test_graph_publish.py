"""graph.publish: the architecture as files (modules.svg, modules.drawio, calls/*.svg, index.md, .dc-digest), the scan before
anything is written (gitleaks plus the built-in marker scan), the symlink rules of the output directory, the local gh-pages branch,
and the stale check of the branch level.

python3 -m pytest -q tests/test_graph_publish.py

The scan runs with a stand-in gitleaks (tests/gitleaks_stand_in.py); one test uses the real binary when it is installed.
"""

import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest
from graph_support import commit_all, git, new_repo, write

from dead_code_checker.graph import levels, publish, snippets, viz

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.usefixtures("hermetic_git", "data_root", "dcref"),
]
STAND_IN = str(Path(__file__).with_name("gitleaks_stand_in.py"))
APP = "def helper\ndef run\nref helper\ndef main\nref run\nroot main main\n"


@pytest.fixture(autouse=True)
def tools(tmp_path, monkeypatch):
    p = tmp_path / "gitleaks"
    p.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(Path(STAND_IN).parent)!r})\nimport gitleaks_stand_in as g\nsys.exit(g.main(sys.argv[1:], sys.argv[0]))\n")
    p.chmod(0o755)
    monkeypatch.setenv(snippets.ENV_GITLEAKS, str(p))
    monkeypatch.setattr(viz, "dot_path", lambda: None)
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t.invalid"), ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t.invalid")):
        monkeypatch.setenv(k, v)  # an identity for the commit of --gh-pages: the hermetic environment has no git configuration at all
    return str(p)


@pytest.fixture
def repo(tmp_path):
    return new_repo(tmp_path, {"lib/app.dcref": APP, "lib/other.dcref": "def spare\n", "run.dcroots": "main file:lib/app.dcref\n"})


def args(**kw):
    base = dict(out=None, gh_pages=False, dry_run=False, deny_file=None)
    base.update(kw)
    return SimpleNamespace(**base)


def tree(p):
    return sorted(str(q.relative_to(p)) for q in Path(p).rglob("*") if q.is_file())


def pub(repo, **kw):
    return publish.run(args(**kw), str(repo))


# ================================================================ what is rendered
def test_the_files_are_modules_svg_drawio_calls_and_an_index_and_hold_no_source(repo):
    assert pub(repo) == 0
    out = repo / "docs" / "architecture"
    files = tree(out)
    calls = [f for f in files if f.startswith("calls/")]
    assert files == [".dc-digest", "architecture.svg", *calls, "index.md", "modules.drawio", "modules.svg"] and len(calls) == 2  # one picture per entry point
    ET.fromstring((out / "modules.svg").read_text())
    layered = (out / "architecture.svg").read_text()  # the architecture lens (graph.lens.arch_svg): module names and counts only
    ET.fromstring(layered)
    assert "architecture:" in layered and "lib" in layered and "def helper" not in layered and "architecture.svg" in (out / "index.md").read_text()
    for c in calls:
        ET.fromstring((out / c).read_text())
    drawio = ET.fromstring((out / "modules.drawio").read_text())
    assert drawio.tag == "mxfile" and drawio.findall(".//mxCell[@vertex='1']")
    md = (out / "index.md").read_text()
    assert md.startswith(f"# Architecture of {repo.name}") and "| language | files | symbols |" in md and "dcref" in md
    assert "## Entry points (2)" in md and all(c in md for c in calls) and "Input digest:" in md
    assert "def helper" not in "".join((out / f).read_text() for f in files if f != ".dc-digest")  # names only, no code


def test_the_same_tree_gives_the_same_bytes_and_a_second_run_changes_nothing(repo):
    assert pub(repo) == 0
    out = repo / "docs" / "architecture"
    first = {f: (out / f).read_bytes() for f in tree(out)}
    assert pub(repo) == 0
    assert {f: (out / f).read_bytes() for f in tree(out)} == first


def test_digest_lists_the_files_and_stale_files_of_an_earlier_publication_are_removed(repo):
    assert pub(repo) == 0
    out = repo / "docs" / "architecture"
    doc = json.loads((out / ".dc-digest").read_text())
    assert sorted(doc["files"]) == sorted(f for f in tree(out) if f != ".dc-digest")
    (out / "keep.txt").write_text("not ours")
    doc["files"]["calls/099-gone.svg"] = "0" * 64
    (out / "calls" / "099-gone.svg").write_text("<svg/>")
    (out / ".dc-digest").write_text(json.dumps(doc))
    assert pub(repo) == 0
    assert not (out / "calls" / "099-gone.svg").exists() and (out / "keep.txt").read_text() == "not ours"


def test_dry_run_writes_nothing_and_lists_what_it_would(repo, capsys):
    assert pub(repo, dry_run=True) == 0
    assert not (repo / "docs").exists() and "dry run, the scan is clean" in capsys.readouterr().out


# ================================================================ the scan
@pytest.mark.parametrize(
    "text,reason",
    [
        ("-----BEGIN RSA PRIVATE KEY-----", "a private key block"),
        ("AKIA" + "ABCDEFGHIJKLMNOP", "an access key id"),
        ("see ops@example.org", "an e-mail address"),
        ("host 10.20.30.40 is up", "an IPv4 address"),
        ("path /home/alice/project/x", "an absolute local path"),
        ("path C:\\Users\\bob\\x", "an absolute local path"),
        ("password = hunter22xx", "a credential assignment"),
        ("gh" + "p_" + "a" * 36, "a token of a code host"),
    ],
)
def test_the_marker_scan_names_the_reason_and_the_line_never_the_text(text, reason):
    got = publish.marker_scan({"index.md": "fine\n" + text + "\nfine"})
    assert len(got) == 1 and got[0].startswith("index.md:2: ") and reason in got[0] and text not in got[0]


@pytest.mark.parametrize("stamp", ["link@13.fb5b28c1", "py@7.ab12cd34", "ts_generic@5.deadbeef"])
def test_a_version_stamp_of_the_pack_is_no_email_address(stamp):
    """`extractor@version.hash` rides in the strings of a page; a hash that starts with two letters must not read as `user@host.tld`."""
    assert publish.marker_scan({"index.md": stamp + "\n"}) == []
    assert publish.marker_scan({"index.md": "ops@example.org\n"}) != []


def test_clean_text_passes_the_marker_scan_and_version_numbers_are_not_addresses():
    assert publish.marker_scan({"a.md": "v1.2.3 in 1.2.3.4.5 and 300.1.1.1 and x@y\n| `lib` | 3 nodes, 0 dead |\n"}) == []


def test_the_deny_list_adds_words_and_regular_expressions(tmp_path):
    f = tmp_path / "deny.txt"
    f.write_text("# names that must not leave\nAcmeCorp\n\nproj-[0-9]+\n(\n")
    deny = publish.read_deny(str(f))
    assert deny == ["AcmeCorp", "proj-[0-9]+", "("]
    got = publish.marker_scan({"x.md": "ACMECORP here\nproj-42\nan ( open"}, deny)
    assert [g.split(":")[1] for g in got] == ["1", "2", "3"]


def test_a_finding_blocks_everything_and_nothing_is_written(repo, tmp_path, capsys):
    deny = tmp_path / "deny.txt"
    deny.write_text("helper|main\n")
    assert pub(repo, deny_file=str(deny)) == 1
    err = capsys.readouterr().err
    assert "finding(s) in the output; nothing was written" in err and "a word of the deny list" in err
    assert not (repo / "docs").exists()


def test_no_gitleaks_is_a_finding_too_nothing_is_published_unscanned(repo, monkeypatch, capsys):
    monkeypatch.setattr(snippets, "find_gitleaks", lambda: None)
    assert pub(repo) == 1 and "gitleaks is not installed" in capsys.readouterr().err and not (repo / "docs").exists()


def test_a_gitleaks_that_fails_is_a_finding(repo, tools, capsys):
    Path(tools + ".fail").write_text("")
    assert pub(repo) == 1 and "gitleaks did not finish" in capsys.readouterr().err and not (repo / "docs").exists()


def test_the_stand_in_finding_is_reported_by_file_and_lines(tools):
    got = publish.scan({"index.md": "ok\nSECRET_TOKEN\n", "a.svg": "<svg/>"})
    assert got == ["index.md:2-2: gitleaks finding (the value is not shown)"]


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks is not installed")
def test_the_real_gitleaks_stops_a_token(monkeypatch):
    monkeypatch.delenv(snippets.ENV_GITLEAKS)
    token = "gh" + "p_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"
    got = publish.scan({"index.md": f'x = "{token}"\n'})
    assert any("gitleaks finding" in g for g in got)


# ================================================================ the output directory
def test_out_must_be_inside_the_repository_and_not_the_root_or_git(repo, capsys):
    for bad in (str(repo.parent / "elsewhere"), ".", ".git", "../up"):
        assert pub(repo, out=bad) == 2, bad
    assert "--out must be a directory inside the repository" in capsys.readouterr().err
    assert pub(repo, out="docs/arch") == 0 and (repo / "docs" / "arch" / "index.md").exists()


def test_out_that_is_a_symlink_is_refused(repo, tmp_path, capsys):
    real = tmp_path / "real"
    real.mkdir()
    (repo / "docs").mkdir()
    (repo / "docs" / "architecture").symlink_to(real)
    assert pub(repo) == 2 and "--out is a symlink" in capsys.readouterr().err and tree(real) == []


def test_a_file_that_is_a_symlink_out_of_the_directory_refuses_with_nothing_written(repo, tmp_path, capsys):
    out = repo / "docs" / "architecture"
    out.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (out / "index.md").symlink_to(victim)  # a path the publication writes goes through a link
    assert pub(repo) == 2 and "refused" in capsys.readouterr().err
    assert victim.read_text() == "precious" and not (out / "modules.svg").exists()


def test_a_symlink_that_no_path_goes_through_is_left_alone(repo, tmp_path):
    out = repo / "docs" / "architecture"
    out.mkdir(parents=True)
    (out / "stray").symlink_to(tmp_path)
    assert pub(repo) == 0 and (out / "stray").is_symlink()


# ================================================================ stale
def sink_of(repo):
    sink: dict = {}
    levels.branch(str(repo), "HEAD", sink=sink)
    return sink


def test_a_project_that_never_published_has_nothing_to_be_stale(repo):
    assert publish.stale(str(repo), sink_of(repo)) == []


def test_stale_is_silent_when_current_and_one_w_when_the_code_or_a_file_changed(repo):
    assert pub(repo) == 0
    assert publish.stale(str(repo), sink_of(repo)) == []
    write(repo, "lib/new.dcref", "def fresh\n")
    (f,) = publish.stale(str(repo), sink_of(repo))
    assert f.level == "W" and f.code == "dc.publish-stale" and f.path == "docs/architecture" and "code it was made from has changed" in f.msg
    assert pub(repo) == 0 and publish.stale(str(repo), sink_of(repo)) == []
    (repo / "docs" / "architecture" / "index.md").write_text("edited")
    (g,) = publish.stale(str(repo), sink_of(repo))
    assert "index.md is missing or was edited" in g.msg
    assert pub(repo) == 0
    commit_all(repo)
    write(repo, "README.md", "x")  # a file that has no node in the graph does not make it stale
    assert publish.stale(str(repo), sink_of(repo)) == []


def test_a_run_without_a_graph_says_it_could_not_compare(repo):
    assert pub(repo) == 0
    (f,) = publish.stale(str(repo), {})
    assert f.level == "I" and f.code == "dc.publish-unchecked"


# ================================================================ gh-pages
def branches(repo):
    return git(repo, "branch", "--list", "gh-pages")


def test_gh_pages_commits_one_file_to_a_local_branch_and_pushes_nothing(repo, capsys):
    head = git(repo, "rev-parse", "HEAD")
    assert pub(repo, gh_pages=True) == 0
    out = capsys.readouterr().out
    assert "gh-pages committed" in out and "nothing was pushed" in out and "git remote add origin URL" in out
    assert git(repo, "ls-tree", "--name-only", "gh-pages") == "index.html"
    assert git(repo, "rev-parse", "HEAD") == head
    page = git(repo, "show", "gh-pages:index.html")
    assert "dc-local" not in page and "<script" in page
    assert git(repo, "worktree", "list").count("\n") == 0  # the temporary worktree is gone
    assert pub(repo, gh_pages=True) == 0 and "gh-pages unchanged" in capsys.readouterr().out
    assert git(repo, "rev-list", "--count", "gh-pages") == "1"


def test_gh_pages_names_the_remote_when_there_is_one(repo, capsys):
    git(repo, "remote", "add", "origin", "https://example.invalid/x.git")
    assert pub(repo, gh_pages=True) == 0 and "git push origin gh-pages" in capsys.readouterr().out


def test_gh_pages_dry_run_commits_nothing(repo):
    assert pub(repo, gh_pages=True, dry_run=True) == 0 and not branches(repo) and not (repo / "docs").exists()


def test_the_page_is_scanned_through_its_strings(repo, tmp_path, capsys):
    deny = tmp_path / "deny.txt"
    deny.write_text("spare\n")  # a name that is only in the page's data, which is deflated
    assert pub(repo, gh_pages=True, deny_file=str(deny)) == 1
    assert "gh-pages/index.html.strings" in capsys.readouterr().err and not branches(repo)


def test_a_commit_that_git_refuses_is_reported_and_the_files_stay(repo, capsys):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    assert pub(repo, gh_pages=True) == 1
    assert "the page is not committed" in capsys.readouterr().err and (repo / "docs" / "architecture" / "index.md").exists()
