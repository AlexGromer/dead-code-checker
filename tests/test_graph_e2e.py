"""End to end through the real extractors: who is alive in a small git repository (levels.build + reach.analyze).

A script that only a build or deploy file names (Makefile, justfile, Dockerfile, CI, compose, Kubernetes) is alive; a library nobody
runs or imports is dead. The Python variant needs no grammar and runs everywhere; the shell variant reads the scripts through the
bash grammar and skips with its reason when tree-sitter or the grammar is not installed.

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_e2e.py
"""

import importlib

import pytest
from graph_support import commit_all, git, write

from dead_code_checker.graph import contracts, levels, model, reach, roots
from dead_code_checker.graph import ts as tsload


pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test


@pytest.fixture(autouse=True)
def registered():
    """Other test files reset the registry: graph.roots registers its root extractors (rootsrc's included) again."""
    if "pyproject" not in {r.name for r in contracts.root_extractors()}:
        importlib.reload(roots)


def build(tmp_path, files):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    for p, t in files.items():
        write(r, p, t)
    commit_all(r, "init")
    b = levels.build(str(r), None, workers=1)
    return b, reach.analyze(b.graph)


def verdicts(b, res):
    out = {}
    for n in b.graph.nodes:
        parts = model.split_key(n.key)
        out[parts[1] + ("#" + parts[2] if parts[2] else "")] = res.verdict(n.key)
    return out


def named_only_by_build_files(ext: str, launcher: list, body: str, lib: str):
    """{path: text} of a repo whose scripts (one per kind of build file) are named by that file only, plus one library nobody runs."""

    def argv(name, prefix=""):
        return [*launcher, f"{prefix}scripts/{name}.{ext}"]

    def line(name, prefix=""):
        return " ".join(argv(name, prefix))

    def jlist(name, prefix=""):
        return "[" + ", ".join(f'"{w}"' for w in argv(name, prefix)) + "]"

    entry = "[" + ", ".join(f'"{w}"' for w in [*launcher, f"/srv/dock.{ext}"]) + "]"
    files = {f"scripts/{n}.{ext}": body for n in ("mk", "dock", "gh", "gl", "comp", "k8s", "just")}
    files[f"scripts/orphan.{ext}"] = lib
    files.update(
        {
            "Makefile": f"all:\n\t{line('mk')} --x\n",
            "justfile": f"default:\n    {line('just')}\n",
            "Dockerfile": f"FROM x\nCOPY scripts/dock.{ext} /srv/dock.{ext}\nENTRYPOINT {entry}\n",
            ".github/workflows/ci.yml": f"on: push\njobs:\n  b:\n    runs-on: x\n    steps:\n      - run: {line('gh')}\n",
            ".gitlab-ci.yml": f"build:\n  script:\n    - {line('gl')}\n",
            "docker-compose.yml": f"services:\n  a:\n    image: x\n    command: {jlist('comp')}\n",
            "k8s/d.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: a\nspec:\n  template:\n    spec:\n"
            f'      containers:\n        - name: c\n          image: x\n          command: ["sh", "scripts/k8s.{ext}"]\n',
        }
    )
    return files


def check(tmp_path, files, ext):
    b, res = build(tmp_path, files)
    v = verdicts(b, res)
    for name in ("mk", "dock", "gh", "gl", "comp", "k8s", "just"):
        assert v[f"scripts/{name}.{ext}"] == "alive", (name, v)
    assert v[f"scripts/orphan.{ext}"] == "dead", v
    assert not [f for f in b.findings if f.level == "E"]
    key = next(n.key for n in b.graph.nodes if n.path == f"scripts/mk.{ext}")
    why = res.why_alive(key).render()  # the root has a kind and the file:line of its source
    assert "ci-job root" in why and "Makefile:2" in why, why


def test_a_python_script_named_only_by_a_build_or_deploy_file_is_alive_an_orphan_is_dead(tmp_path):
    # no statement at the top level: a library, which only a root can make alive (a script with a call statement is an entry of its own)
    lib = "def main():\n    return 1\n"
    check(tmp_path, named_only_by_build_files("py", ["python3"], lib, "def lonely():\n    return 3\n"), "py")


def _bash():
    res = tsload.grammar_status("bash")
    return "" if res.status == "ok" else res.detail


@pytest.mark.skipif(bool(_bash()), reason=f"the bash grammar is not usable here: {_bash()}")
def test_a_shell_script_named_only_by_a_build_or_deploy_file_is_alive_an_orphan_is_dead(tmp_path):
    lib = "#!/bin/sh\nhelper() {\n  echo hi\n}\n"  # no statement at the top level: a library, not a script
    check(tmp_path, named_only_by_build_files("sh", [], lib, lib.replace("helper", "orphan")), "sh")


# ====================================================================== a hostile file is one file error, never a lost run
HOSTILE = "a: " + "[" * 2000 + "]" * 2000 + "\n"  # 2000 nested flow sequences: far beyond the recursion limit of the reader


def test_a_hostile_yaml_file_is_a_file_error_and_the_rest_of_the_repository_is_still_checked(tmp_path):
    files = {"app.py": "def main():\n    return helper()\n\n\ndef helper():\n    return 1\n\n\ndef lonely():\n    return 2\n"}
    files["deploy/bad.yaml"] = HOSTILE
    files["Makefile"] = "all:\n\tpython3 app.py\n"
    b, res = build(tmp_path, files)
    assert not [f for f in b.findings if f.code == "dc.check-failed"], [f.render() for f in b.findings]
    assert verdicts(b, res)["app.py#lonely"] == "dead"  # the python file next to the hostile one is analysed as usual
