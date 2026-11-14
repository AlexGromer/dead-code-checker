"""`dead-code-checker graph check | view | why | tree | publish | prune | pages` and the tools rows of `doctor`, end to end on a toy
repository (tests/graph_support.py), in process.

python3 -m pytest -q tests/test_graph_cli_full.py

`graph doctor` and the entry point itself are in tests/test_graph_cli.py.
"""

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from graph_support import commit_all, new_repo, write

from dead_code_checker.graph import cli, levels, snippets, viz

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.usefixtures("hermetic_git", "data_root", "dcref"),
]
REAL_GITLEAKS, REAL_DOT = snippets.find_gitleaks, viz.dot_path
APP = "def helper\ndef run\nref helper\ndef main\nref run\nroot main main\n"
CHAIN = "def link_a\nref link_b\ndef link_b\nref link_c\ndef link_c\n"


@pytest.fixture(autouse=True)
def no_tools(monkeypatch):
    monkeypatch.setattr(viz, "dot_path", lambda: None)
    monkeypatch.setattr(snippets, "find_gitleaks", lambda: None)


@pytest.fixture
def repo(tmp_path):
    r = new_repo(tmp_path, {"lib/app.dcref": APP, "run.dcroots": "main file:lib/app.dcref\n"})
    write(r, "lib/chain.dcref", CHAIN)
    return r


def go(capsys, *argv):
    rc = cli.main([str(a) for a in argv])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def runs(data_root):
    return sorted(p for p in Path(data_root).glob("projects/*/runs/*") if p.is_dir() and not p.is_symlink())


# ================================================================ check
def test_check_branch_prints_the_findings_the_link_and_stores_the_run(repo, capsys, data_root):
    rc, out, err = go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo)
    assert rc == 0 and "3 suspects hidden, --show-suspects" in out and "0 error(s), 2 warning(s), 1 info (3 files, 8 nodes, 4 edges)" in out
    (run,) = runs(data_root)
    assert run.name.endswith("-branch-" + out.split("branch-")[1][:8]) and f"{run.as_uri()}/change.html" in out
    assert {p.name for p in run.iterdir()} >= {"index.html", "change.html", "report.md", "sarif.json", "findings.json"}
    assert (data_root / "index.html").exists()
    assert not [p for p in repo.rglob("*") if ".git" not in p.parts and p.name.endswith((".html", ".svg"))]


def test_suspects_are_hidden_unless_asked_for(repo, capsys):
    _, out, _ = go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--no-viz")
    assert "dc.suspect" not in out and "3 suspects hidden, --show-suspects" in out
    _, out, _ = go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--no-viz", "--show-suspects")
    assert out.count("dc.suspect") == 3 and "[dc.dead-cluster] `link_a`" in out


def test_an_E_finding_is_exit_one_in_every_format(repo, capsys):
    write(repo, "new.dcroots", "main file:bin/gone\n")
    for fmt in ("text", "json", "md", "sarif"):
        rc, out, _ = go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--no-viz", "--format", fmt)
        assert rc == 1 and "dc.root-dangling" in out, fmt


def test_formats_json_md_and_the_short_forms(repo, capsys):
    argv = ("check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--no-viz", "--show-suspects")
    go(capsys, *argv)  # warm the cache: the number of shards a run extracted is part of its meta
    _, out, _ = go(capsys, *argv, "--format", "json")
    doc = json.loads(out)
    assert doc["meta"]["scope"] == "branch" and len(doc["findings"]) == 4 and "timing" not in doc and list(doc) == sorted(doc)
    assert go(capsys, *argv, "--json")[1] == out  # the second run is warm: the same document
    _, md, _ = go(capsys, *argv, "--md")
    assert md.startswith("# dead-code-checker branch report")
    _, timed, _ = go(capsys, *argv, "--json", "--timing")
    assert "timing" in json.loads(timed)


def test_sarif_is_one_valid_document_on_stdout_and_nothing_else(repo, capsys):
    rc, out, err = go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--no-viz", "--show-suspects", "--format", "sarif")
    doc = json.loads(out)
    assert rc == 0 and err == "" and doc["version"] == "2.1.0"
    res = doc["runs"][0]["results"]
    # the engine layer is wired in: a toy language has no language server, which is ONE note next to the three suspects
    assert sorted(r["ruleId"] for r in res) == ["dc.sem-lsp-skipped"] + ["dc.suspect"] * 3
    res = [r for r in res if r["ruleId"] == "dc.suspect"]
    assert res[0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "lib/chain.dcref"
    rc, again, _ = go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--no-viz", "--show-suspects", "--format", "sarif")
    assert again == out  # deterministic


def test_check_repo_checks_the_whole_tree_and_nothing_blocks(repo, capsys, data_root):
    commit_all(repo)
    write(repo, "lib/new.dcref", "def fresh\n")
    commit_all(repo)
    rc, out, _ = go(capsys, "check", "--scope", "repo", "--root", repo, "--show-suspects", "--no-viz")
    assert rc == 0 and out.count("dc.suspect") >= 4 and "(4 files" in out  # the committed chain and the new one: the whole tree, not a diff
    rc, js, _ = go(capsys, "check", "--scope", "repo", "--root", repo, "--json", "--show-suspects")
    assert json.loads(js)["meta"]["scope"] == "repo" and any(p.name.endswith("-repo-" + p.name[-8:]) for p in runs(data_root))


def test_check_repo_turns_an_E_about_the_tree_into_a_W(repo, capsys):
    write(repo, "new.dcroots", "main file:bin/gone\n")
    commit_all(repo)
    rc, out, _ = go(capsys, "check", "--scope", "repo", "--root", repo, "--no-viz")
    assert rc == 0 and "W new.dcroots" in out and "pre-existing" in out


def test_out_moves_the_artifacts_and_an_out_inside_the_repository_is_refused(repo, capsys, tmp_path, data_root):
    out_dir = tmp_path / "art"
    rc, out, _ = go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--out", out_dir)
    assert rc == 0 and len(list(out_dir.iterdir())) == 2 and not runs(data_root)  # the run and `latest`
    rc, _, err = go(capsys, "check", "--scope", "branch", "--root", repo, "--out", repo / "art")
    assert rc == 2 and "inside the analysed repository" in err


def test_no_viz_writes_no_pages_and_a_disabled_project_writes_none_either(repo, capsys, data_root):
    go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--no-viz")
    assert not runs(data_root)
    write(repo, ".dead-code-checker.toml", '[graph]\nenabled = false\ndisabled_reason = "migration"\ndisabled_until = "2999-01-01"\n')
    rc, out, _ = go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo)
    assert rc == 0 and "dc.disabled" in out and not runs(data_root)


def test_the_check_never_breaks_the_caller(repo, capsys, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(levels, "branch", boom)
    rc, out, _ = go(capsys, "check", "--scope", "branch", "--root", repo)
    assert rc == 0 and "dc.check-failed" in out and "kaput" in out and "Traceback" not in out


def test_a_directory_that_is_not_a_repository_and_a_bad_jobs_value_are_usage_errors(tmp_path, repo, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    for cmd in (("check", "--scope", "branch"), ("view",), ("why", "x"), ("publish",)):
        rc, _, err = go(capsys, *cmd, "--root", plain)
        assert rc == 2 and "not inside a git repository" in err, cmd
    rc, _, err = go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--jobs", "0", "--no-viz")
    assert rc == 2 and "workers must be an integer" in err
    with pytest.raises(SystemExit) as e:
        cli.main(["check", "--root", str(repo)])  # --scope is required
    assert e.value.code == 2


def test_a_run_with_jobs_gives_the_same_findings(repo, capsys, monkeypatch):
    monkeypatch.delenv("DEAD_CODE_CHECKER_JOBS", raising=False)
    outs = []
    go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--no-viz")  # warm: `extracted` is part of the meta
    for n in ("1", "4"):
        _, out, _ = go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--no-viz", "--json", "--show-suspects", "--jobs", n)
        outs.append(out)
    assert outs[0] == outs[1]
    monkeypatch.delenv("DEAD_CODE_CHECKER_JOBS", raising=False)


# ================================================================ view, why, tree
def test_view_writes_the_overview_pages_of_the_whole_tree(repo, capsys, data_root):
    rc, out, _ = go(capsys, "view", "--root", repo)
    (run,) = runs(data_root)
    assert rc == 0 and out.strip() == f"dead-code-checker: viewer {(run / 'index.html').as_uri()}" and run.name.split("-")[1] == "repo"
    assert (run / "modules.svg").exists() and not (run / "change.html").exists()
    ET.fromstring((run / "modules.svg").read_text())


def test_why_prints_the_chain_or_the_candidates(repo, capsys):
    rc, out, _ = go(capsys, "why", "lib/app.dcref#helper", "--root", repo)
    assert rc == 0 and "dcref:lib/app.dcref#helper" in out and "main" in out
    _, out, _ = go(capsys, "why", "link_b", "--root", repo)
    assert "dead" in out and "link_a" in out
    _, out, _ = go(capsys, "why", "no_such_symbol", "--root", repo)
    assert out.strip() == "no node matches 'no_such_symbol'"


def test_tree_text_json_and_svg(repo, capsys, tmp_path):
    rc, out, _ = go(capsys, "tree", "run", "--root", repo, "--callers")
    assert rc == 0 and out.startswith("dcref:lib/app.dcref#run [alive]") and "main [alive]" in out
    _, js, _ = go(capsys, "tree", "run", "--root", repo, "--json")
    assert set(json.loads(js)) == {"callees", "callers", "node", "verdict"}
    pic = tmp_path / "t.svg"
    rc, out, err = go(capsys, "tree", "run", "--root", repo, "--svg", "--out", pic)
    assert rc == 0 and "nodes written" in out and "dc.graphviz-missing" in err
    svg = ET.fromstring(pic.read_text())
    assert svg.tag.endswith("svg") and pic.read_text().count("<a href=") == 3
    rc, out, _ = go(capsys, "tree", "run", "--root", repo, "--svg")
    assert rc == 0 and out.startswith("<svg")
    rc, _, err = go(capsys, "tree", "run", "--root", repo, "--out", pic)
    assert rc == 2 and "--out goes with --svg" in err
    rc, _, err = go(capsys, "tree", "run", "--root", repo, "--svg", "--depth", "0")
    assert rc == 2 and "--depth is 1..50" in err


# ================================================================ publish, prune, pages
def test_publish_through_the_cli_and_the_stale_warning_of_the_branch_check(repo, capsys, monkeypatch, tmp_path):
    fake = tmp_path / "gitleaks"
    fake.write_text(f"#!{sys.executable}\nimport sys, json\nargv = sys.argv\nopen(argv[argv.index('-r') + 1], 'w').write('[]')\n")
    fake.chmod(0o755)
    monkeypatch.setattr(snippets, "find_gitleaks", REAL_GITLEAKS)
    monkeypatch.setenv(snippets.ENV_GITLEAKS, str(fake))
    rc, out, err = go(capsys, "publish", "--root", repo, "--dry-run")
    assert rc == 0 and "dry run" in out, err and not (repo / "docs").exists()
    rc, out, _ = go(capsys, "publish", "--root", repo)
    assert rc == 0 and (repo / "docs/architecture/index.md").exists()
    _, out, _ = go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--no-viz")
    assert "dc.publish-stale" not in out
    write(repo, "lib/more.dcref", "def more\n")
    _, out, _ = go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--no-viz")
    assert "W docs/architecture: dc.publish-stale" in out
    rc, _, err = go(capsys, "publish", "--root", repo, "--deny-file", tmp_path / "missing.txt")
    assert rc == 2 and "cannot read --deny-file" in err


def test_prune_is_a_dry_run_by_default_and_apply_removes(repo, capsys, data_root):
    for _ in range(3):
        go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo)
    assert len(runs(data_root)) == 3
    rc, out, _ = go(capsys, "prune", "--root", repo, "--keep", "1", "--days", "0")
    assert rc == 0 and "would remove 2 run(s)" in out and "dry run, nothing was removed" in out and len(runs(data_root)) == 3
    rc, out, _ = go(capsys, "prune", "--root", repo, "--keep", "1", "--days", "0", "--apply")
    assert rc == 0 and "removed 2 run(s)" in out and len(runs(data_root)) == 1
    rc, _, err = go(capsys, "prune", "--root", repo, "--keep", "-1")
    assert rc == 2 and "must not be negative" in err
    with pytest.raises(SystemExit):
        cli.main(["prune", "--dry-run", "--apply"])


def test_pages_prints_the_link_of_the_overview(repo, capsys, data_root):
    go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo)
    (data_root / "index.html").unlink()
    rc, out, _ = go(capsys, "pages", "--root", repo)
    assert rc == 0 and out.strip() == f"dead-code-checker graph pages: {(data_root / 'index.html').as_uri()}" and (data_root / "index.html").exists()


# ================================================================ doctor: the tools
def test_doctor_lists_the_tools_and_says_what_each_missing_one_costs(repo, capsys):
    rc, out, _ = go(capsys, "doctor", "--root", repo)
    assert rc == 0
    assert "tool:      gitleaks: not installed; the pages carry no code fragments" in out
    assert "tool:      graphviz-dot: not installed; the built-in emitter draws the SVG files" in out
    rc, js, _ = go(capsys, "doctor", "--root", repo, "--json")
    doc = json.loads(js)
    assert set(doc["tools"]) == {"bwrap", "gitleaks", "graphviz-dot"} and doc["tools"]["gitleaks"]["path"] is None and list(doc) == sorted(doc)


def test_the_tools_are_found_through_their_variables(repo, capsys, tmp_path, monkeypatch):
    exe = tmp_path / "gl"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(snippets, "find_gitleaks", REAL_GITLEAKS)
    monkeypatch.setattr(viz, "dot_path", REAL_DOT)
    monkeypatch.setenv(snippets.ENV_GITLEAKS, str(exe))
    monkeypatch.setenv("DEAD_CODE_CHECKER_DOT", str(exe))
    _, out, _ = go(capsys, "doctor", "--root", repo)
    assert f"tool:      gitleaks: {exe}" in out and f"tool:      graphviz-dot: {exe}" in out


# ================================================================ the entry point
def test_the_console_entry_point_reaches_every_command():
    r = subprocess.run([sys.executable, "-P", "-m", "dead_code_checker", "graph", "--help"], capture_output=True, text=True, timeout=60, env={**os.environ, "DEAD_CODE_CHECKER_HOME": "/nonexistent"})
    assert r.returncode == 0
    for c in ("doctor", "check", "view", "why", "tree", "publish", "prune", "pages"):
        assert c in r.stdout, c


# ================================================================ the engine options of the levels (merged with the engines layer)
@pytest.mark.parametrize("cmd", [["check", "--scope", "branch", "--base", "HEAD"], ["view"], ["why", "lib/app.dcref#main"], ["tree", "main"], ["publish", "--dry-run"]])
def test_a_lock_that_cannot_be_read_is_a_usage_error_for_every_command_that_runs_the_levels(repo, capsys, tmp_path, cmd):
    rc, out, err = go(capsys, *cmd, "--root", repo, "--lock", tmp_path / "absent.json")
    assert rc == 2 and out == "" and "absent.json" in err


def test_the_engine_options_are_accepted_next_to_the_viewer_options(repo, capsys, tmp_path):
    rc, out, err = go(capsys, "check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--no-viz", "--format", "sarif",
                      "--engines-dir", tmp_path, "--no-path", "--go-cache", tmp_path / "gomod")
    assert rc == 0 and json.loads(out)["version"] == "2.1.0"
    rc, out, _ = go(capsys, "doctor", "--root", repo, "--json", "--engines-dir", tmp_path, "--no-path")
    doc = json.loads(out)
    assert rc == 0 and set(doc["tools"]) == {"gitleaks", "graphviz-dot", "bwrap"} and doc["engines"]["dirs"] == [str(tmp_path)]


def test_min_confidence_is_in_the_help_and_a_bad_value_is_a_usage_error(repo, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["check", "--help"])
    assert e.value.code == 0 and "--min-confidence N" in capsys.readouterr().out
    for bad in ("101", "-1", "x"):
        with pytest.raises(SystemExit) as e:
            cli.main(["check", "--scope", "branch", "--root", str(repo), "--min-confidence", bad])
        assert e.value.code == 2 and "--min-confidence" in capsys.readouterr().err


def test_min_confidence_filters_the_printout_only(repo, capsys):
    base = ["check", "--scope", "branch", "--base", "HEAD", "--root", repo, "--no-viz", "--show-suspects"]
    rc, out, _ = go(capsys, *base)
    assert rc == 0 and out.count("confidence 30/100") == 3
    rc, out, _ = go(capsys, *base, "--min-confidence", "31")
    assert rc == 0 and "dc.suspect" not in out and "3 finding(s) below confidence 31 hidden, --min-confidence" in out
    rc, out, _ = go(capsys, *base, "--min-confidence", "30")
    assert rc == 0 and out.count("confidence 30/100") == 3 and "below confidence" not in out
