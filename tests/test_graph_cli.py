"""`dead-code-checker graph doctor` and its wiring into the console entry point.

python3 -m pytest -q tests/test_graph_cli.py
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from dead_code_checker import core
from dead_code_checker.graph import cli, paths

ROOT = Path(__file__).resolve().parent.parent


def run(*args, cwd, home, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("DEAD_CODE_CHECKER_")}
    e["DEAD_CODE_CHECKER_HOME"] = str(home)
    e.update(env)
    return subprocess.run(
        [sys.executable, "-P", "-m", "dead_code_checker", "graph", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=e,
        timeout=120,
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "work" / "app"
    (r / ".git").mkdir(parents=True)
    (r / "src").mkdir()
    return r


def tree(p):
    return sorted(str(q.relative_to(p)) for q in Path(p).rglob("*"))


def test_doctor_prints_the_data_dir_and_the_config(repo, tmp_path):
    home = tmp_path / "home"
    r = run("doctor", cwd=repo / "src", home=home)  # from a subdirectory: the repository root is found
    assert r.returncode == 0 and r.stderr == ""
    rid = paths.repo_id(repo)
    assert f"repo:      {repo.resolve()} ({rid})" in r.stdout
    assert f"data dir:  {home / 'projects' / rid} (chosen by: env)" in r.stdout
    assert "config:    none: mode closed_world, on, budget commit 20s branch 900s" in r.stdout
    assert re.search(r"(?m)^workers:   \d+$", r.stdout)


def test_doctor_writes_nothing_anywhere(repo, tmp_path):
    home = tmp_path / "home"
    before = tree(repo)
    r = run("doctor", "--out", str(tmp_path / "art"), cwd=repo, home=home)
    assert r.returncode == 0
    assert tree(repo) == before and not home.exists() and not (tmp_path / "art").exists()


def test_doctor_json(repo, tmp_path):
    home = tmp_path / "home"
    (repo / ".dead-code-checker.toml").write_text('[graph]\nmode = "library"\n')
    r = run("doctor", "--json", "--jobs", "3", "--root", str(repo), cwd=tmp_path, home=home)
    doc = json.loads(r.stdout)
    assert r.returncode == 0 and doc["workers"] == 3 and doc["repo"]["id"] == paths.repo_id(repo)
    assert doc["config"]["mode"] == "library" and doc["config"]["config_file"] == ".dead-code-checker.toml"
    assert doc["data"]["source"] == "env" and doc["data"]["runs"].endswith(os.path.join(doc["repo"]["id"], "runs"))
    assert list(doc) == sorted(doc)


def test_doctor_out_and_variables(repo, tmp_path):
    r = run(
        "doctor",
        "--json",
        "--out",
        str(tmp_path / "art"),
        cwd=repo,
        home=tmp_path / "h",
        DEAD_CODE_CHECKER_JOBS="2",
        DEAD_CODE_CHECKER_MODE="library",
    )
    doc = json.loads(r.stdout)
    assert doc["data"]["runs"] == str(tmp_path / "art") and doc["workers"] == 2 and doc["config"]["mode"] == "library"


def test_doctor_exit_1_on_a_config_error_and_it_is_listed(repo, tmp_path):
    (repo / ".dead-code-checker.toml").write_text('[graph]\nmode = "open"\nargv = ["x"]\n')
    r = run("doctor", cwd=repo, home=tmp_path / "h")
    assert r.returncode == 1
    assert r.stdout.count("dc.bad-config") == 2 and "E .dead-code-checker.toml" in r.stdout


def test_doctor_exit_2_when_out_is_inside_the_repository(repo, tmp_path):
    r = run("doctor", "--out", str(repo / "build"), cwd=repo, home=tmp_path / "h")
    assert r.returncode == 2 and "inside the analysed repository" in r.stderr and r.stdout == ""


def test_doctor_exit_2_when_no_data_directory_is_usable(repo, tmp_path):
    (tmp_path / "file").write_text("x")
    r = run("doctor", cwd=repo, home=tmp_path / "file")
    assert r.returncode == 2 and "not writable" in r.stderr


def test_doctor_exit_2_on_a_bad_jobs_value(repo, tmp_path):
    assert run("doctor", "--jobs", "0", cwd=repo, home=tmp_path / "h").returncode == 2


def test_the_one_warning_goes_to_stderr_once(repo, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(paths.ENV_HOME, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(paths, "tool_dir", lambda: None)
    assert cli.main(["doctor", "--root", str(repo)]) == 0
    out = capsys.readouterr()
    assert out.err.count("\n") == 1 and "no data directory next to the tool" in out.err
    assert "chosen by: state" in out.out and not (tmp_path / "state").exists()


def test_no_subcommand_is_a_usage_error():
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 2


def test_graph_is_reached_through_the_console_entry_point(repo, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "h"))
    assert core.main(["graph", "doctor", "--root", str(repo)]) == 0
    assert "data dir:" in capsys.readouterr().out
    assert core.main(["defaults"]) == 0  # the v1 commands are untouched
    assert "[deadcode]" in capsys.readouterr().out


def test_the_graph_package_and_its_script_are_in_pyproject():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'dead-code-checker = "dead_code_checker.core:main"' in text
    listed = re.search(r"(?m)^packages = \[(.*)\]$", text).group(1)
    top = ROOT / "dead_code_checker"
    on_disk = {"dead_code_checker"} | {
        "dead_code_checker." + ".".join(init.parent.relative_to(top).parts)
        for init in top.rglob("__init__.py")
        if init.parent != top
    }
    assert set(re.findall(r'"([^"]+)"', listed)) == on_disk


def test_the_package_data_globs_of_pyproject_cover_every_data_file_and_the_extra_is_declared():
    import tomllib

    doc = tomllib.loads((ROOT / "pyproject.toml").read_text())
    globs = doc["tool"]["setuptools"]["package-data"]["dead_code_checker.graph"]
    pkg = ROOT / "dead_code_checker" / "graph"
    covered = {p for g in globs for p in pkg.glob(g) if p.is_file()}
    every = {p for p in (pkg / "data").rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    assert every and not sorted(str(p.relative_to(pkg)) for p in every - covered)  # a data file the wheel would miss
    assert {"tree-sitter", "tree-sitter-language-pack"} == {
        re.split(r"[<>=!~ ]", d)[0] for d in doc["project"]["optional-dependencies"]["treesitter"]
    }
    assert doc["project"]["dependencies"] == []  # the extra is the only dependency
