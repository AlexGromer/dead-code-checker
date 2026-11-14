"""graph.fix: `dead-code-checker graph fix --dry-run`, a patch for review that removes the confirmed dead symbols, and that never writes
the tree.

python3 -m pytest -q tests/test_graph_fix.py

Every test works in a throwaway git repository under tmp_path with DEAD_CODE_CHECKER_HOME there. The code under test is Python (the one
language fix cuts); the corroboration of a dead claim is a scripted graph.verdict.Proof (the S-tier of the file), so no engine or index
runs. The patches are applied with `git apply` on a COPY of the tree.
"""

import ast
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from graph_support import git, new_repo, write

from dead_code_checker.graph import cli, fix, levels
from dead_code_checker.graph.model import Finding, Node, canonical
from dead_code_checker.graph.verdict import Proof

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.usefixtures("hermetic_git", "data_root"),
]

APP = '''"""the app"""


def helper():
    return 41


def run():
    return helper() + 1


def main():
    return run()


if __name__ == "__main__":
    main()
'''
CHAIN = "def link_a():\n    return link_b()\n\n\ndef link_b():\n    return link_c()\n\n\ndef link_c():\n    return 3\n"
STIER = Proof(covered=frozenset({"lib/app.py", "lib/extra.py"}))
LONE = "def lone():\n    return 2\n"


@pytest.fixture
def repo(tmp_path):
    return new_repo(tmp_path, {"lib/app.py": APP})


def with_chain(repo):
    write(repo, "lib/app.py", (repo / "lib/app.py").read_text() + "\n\n" + CHAIN)


def tree_hash(root: Path) -> str:
    """One digest of every file of the work tree (the .git directory aside: git itself may refresh its index)."""
    h = hashlib.sha256()
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x != ".git")
        for f in sorted(files):
            p = Path(d) / f
            h.update(f"{p.relative_to(root)}\0{p.stat().st_mode}\0".encode() + p.read_bytes() + b"\0")
    return h.hexdigest()


def applied(repo: Path, patch: str, tmp_path: Path) -> Path:
    """The tree with `patch` applied by git, on a copy: the repository itself is left alone."""
    copy = tmp_path / "copy"
    shutil.copytree(repo, copy, dirs_exist_ok=True)
    r = subprocess.run(["git", "-C", str(copy), "apply", "-"], input=patch, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    return copy


def prove(monkeypatch, proof=STIER):
    """`graph check`'s semantic step answers with `proof` (what an index of the language would corroborate)."""
    steps = levels.Steps(semantic=lambda root, built, store, notes, deadline, clock: (built.graph, proof))
    monkeypatch.setattr(cli, "_steps", lambda a, root, lay=None: steps)


def test_dry_run_patch(repo, tmp_path):
    with_chain(repo)
    p = fix.plan(str(repo), "branch", base="HEAD", proof=STIER)
    assert [(i.qual, i.code) for i in p.removed] == [
        ("link_a", "dc.dead-cluster"),
        ("link_b", "dc.dead-cluster"),
        ("link_c", "dc.dead-cluster"),
    ]
    assert p.manual == () and p.patch.startswith("--- a/lib/app.py\n+++ b/lib/app.py\n")
    copy = applied(repo, p.patch, tmp_path)
    new = (copy / "lib/app.py").read_text()
    ast.parse(new)
    assert "link_" not in new and new.endswith('if __name__ == "__main__":\n    main()\n')
    assert "def link_a" in (repo / "lib/app.py").read_text()  # the repository itself still has them
    assert fix.plan(str(repo), "branch", base="HEAD", proof=STIER).patch == p.patch  # deterministic


def test_dry_run_no_write(repo, capsys):
    with_chain(repo)
    git(repo, "add", "-A")
    head, status, before = git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"), tree_hash(repo)
    argv = ["fix", "--dry-run", "--root", str(repo), "--base", "HEAD"]
    assert cli.main(argv) == 0  # no proof in the cache: the claims are suspects, the patch is empty
    out, err = capsys.readouterr()
    assert out == "" and "0 symbol(s) in the patch" in err and "not corroborated" in err
    fix.plan(str(repo), "branch", base="HEAD", proof=STIER)  # a plan with a patch writes nothing either
    fix.plan(str(repo), "repo", proof=STIER)
    assert cli.main([*argv, "--json"]) == 0
    capsys.readouterr()
    assert tree_hash(repo) == before
    assert (
        git(repo, "rev-parse", "HEAD") == head and git(repo, "status", "--porcelain") == status
    )  # no commit, nothing staged or unstaged more


def test_without_dry_run_the_command_refuses(repo, capsys):
    before = tree_hash(repo)
    assert cli.main(["fix", "--root", str(repo)]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "only --dry-run is supported" in err
    assert tree_hash(repo) == before


def test_the_command_prints_the_patch_alone_on_stdout_and_json_on_request(repo, tmp_path, monkeypatch, capsys):
    with_chain(repo)
    prove(monkeypatch)
    argv = ["fix", "--dry-run", "--root", str(repo), "--base", "HEAD"]
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert (
        out.startswith("--- a/lib/app.py")
        and "+++ " in out
        and "3 symbol(s) in the patch, 0 manual, nothing written" in err
    )
    assert (applied(repo, out, tmp_path) / "lib/app.py").read_text().count("link_") == 0
    assert cli.main([*argv, "--json", "--path", "lib/*.py", "--code", "dc.dead-cluster"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["dry_run"] is True and doc["scope"] == "branch" and doc["patch"] == out
    assert [r["symbol"] for r in doc["removed"]] == ["link_a", "link_b", "link_c"] and doc["manual"] == []


def test_a_suspect_is_never_removed(repo):
    with_chain(repo)
    p = fix.plan(str(repo), "branch", base="HEAD")  # nothing corroborates: dc.suspect, level I
    assert p.patch == "" and p.removed == () and sum(p.skipped.values()) == 3 and list(p.skipped) == [fix.NOT_PROVEN]
    assert "suspect" in fix.NOT_PROVEN and "not removable" in fix.NOT_PROVEN


def test_the_filters_keep_the_findings_of_a_code_and_a_path(repo):
    with_chain(repo)
    write(repo, "lib/extra.py", LONE)
    both = fix.plan(str(repo), "branch", base="HEAD", proof=STIER)
    assert {i.path for i in both.removed} == {"lib/app.py", "lib/extra.py"}
    assert {
        i.path for i in fix.plan(str(repo), "branch", paths=["lib/extra.py"], base="HEAD", proof=STIER).removed
    } == {"lib/extra.py"}
    assert {i.path for i in fix.plan(str(repo), "branch", paths=["lib/"], base="HEAD", proof=STIER).removed} == {
        "lib/app.py",
        "lib/extra.py",
    }
    assert {i.path for i in fix.plan(str(repo), "branch", paths=["hooks"], base="HEAD", proof=STIER).removed} == set()
    only = fix.plan(str(repo), "branch", codes=["dc.dead-cluster"], base="HEAD", proof=STIER)
    assert {i.qual for i in only.removed} == {"link_a", "link_b", "link_c"}


def test_the_repo_scope_removes_the_dead_symbols_of_the_whole_tree(repo, tmp_path):
    write(repo, "lib/extra.py", LONE)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "dead code on the base")
    assert (
        fix.plan(str(repo), "branch", base="HEAD", proof=STIER).patch == ""
    )  # not in the diff: the branch scope has nothing
    p = fix.plan(str(repo), "repo", proof=STIER)
    assert [(i.path, i.qual) for i in p.removed] == [("lib/extra.py", "lone")]
    assert p.removed[0].code in ("dc.dead", "dc.introduced-dead")
    assert (applied(repo, p.patch, tmp_path) / "lib/extra.py").read_text() == ""
    assert "lone" in (repo / "lib/extra.py").read_text()


def test_the_repo_scope_without_a_corroboration_removes_nothing_and_says_so(repo, capsys):
    write(repo, "lib/extra.py", LONE)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "dead code on the base")
    p = fix.plan(str(repo), "repo")  # no proof: every dead claim of the tree is a suspect
    assert p.patch == "" and p.removed == () and sum(p.skipped.values()) >= 1 and list(p.skipped) == [fix.NOT_PROVEN]
    assert cli.main(["fix", "--dry-run", "--scope", "repo", "--root", str(repo)]) == 0
    out, err = capsys.readouterr()
    assert out == "" and "skipped 1: not corroborated (a suspect" in err and "the patch is empty" in err


def test_a_language_without_exact_spans_is_manual():
    g = canonical(
        [
            Node("rust:a.rs#f", "func", "rust", "a.rs", 3, 5, "", "private", 0),
            Node("python:b.py#g", "func", "python", "b.py", 1, 2, "", "public", 0),
        ]
    )
    ok = "corroborated: rustc dead_code at a.rs:3"
    fs = [
        Finding("W", "a.rs", 3, "dc.dead", "`f` is dead", ok),
        Finding("W", "b.py", 1, "dc.dead", "`g` is dead", ok),
        Finding("W", "c.py", 9, "dc.dead", "`h` is dead", ok),
    ]
    todo, manual, skipped = fix.candidates(fs, g, (), ())
    assert [(i.path, i.qual, i.reason) for i in manual] == [
        ("a.rs", "f", "no exact-span removal for rust yet"),
        ("c.py", "h", "no symbol of the graph at that line"),
    ]
    assert list(todo) == ["b.py"] and skipped == {}


SRC = "import os\n\n\ndef keep():\n    return 1\n\n\n@staticmethod\ndef gone(x):\n    return x\n\n\nclass Box:\n    def a(self):\n        return 1\n\n    def b(self):\n        return 2\n\n\ndef tail():\n    return 3\n"


def span(src, name, qual=None):
    node = next(n for n in ast.walk(ast.parse(src)) if getattr(n, "name", "") == name)
    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return first, node.end_lineno, fix.Item("m.py", first, qual or name, "dc.dead")


def test_a_decorated_definition_goes_with_its_decorator_and_the_blank_lines_are_tidied():
    new, removed, manual = fix.patch_file(SRC, [span(SRC, "gone")])
    assert manual == [] and [i.qual for i in removed] == ["gone"]
    assert new == SRC.replace("@staticmethod\ndef gone(x):\n    return x\n\n\n", "")
    ast.parse(new)


def test_the_last_definition_takes_the_blank_lines_before_it_and_a_first_one_the_blank_line_after_the_header():
    new, _, _ = fix.patch_file(SRC, [span(SRC, "tail")])
    assert new.endswith("        return 2\n") and ast.parse(new)
    new, _, _ = fix.patch_file(SRC, [span(SRC, "a", "Box.a")])
    assert "class Box:\n    def b(self):" in new and "def a" not in new


def test_a_symbol_inside_a_removed_one_goes_with_it_and_the_last_member_of_a_block_is_manual():
    both = [span(SRC, "Box"), span(SRC, "a", "Box.a"), span(SRC, "b", "Box.b")]
    new, removed, manual = fix.patch_file(SRC, both)
    assert manual == [] and sorted(i.qual for i in removed) == ["Box", "Box.a", "Box.b"] and "Box" not in new
    only = "class Box:\n    def a(self):\n        return 1\n"
    new, removed, manual = fix.patch_file(only, [span(only, "a", "Box.a")])
    assert (
        new == only
        and removed == []
        and [i.reason for i in manual] == ["removing it would leave an empty block (the file would not parse)"]
    )
    # two of three members: both go; all three would leave the class empty, so the last one stays
    three = "class Box:\n    def a(self):\n        return 1\n\n    def b(self):\n        return 2\n"
    new, removed, manual = fix.patch_file(three, [span(three, "a", "Box.a"), span(three, "b", "Box.b")])
    assert [i.qual for i in removed] == ["Box.a"] and [i.qual for i in manual] == ["Box.b"] and ast.parse(new)


def test_a_span_that_is_not_in_the_source_is_manual_and_so_is_a_file_that_does_not_parse():
    wrong = (4, 9, fix.Item("m.py", 4, "keep", "dc.dead"))
    new, removed, manual = fix.patch_file(SRC, [wrong])
    assert new == SRC and removed == [] and "not at the lines of the graph" in manual[0].reason
    new, removed, manual = fix.patch_file("def broken(:\n", [(1, 1, fix.Item("m.py", 1, "broken", "dc.dead"))])
    assert removed == [] and "does not parse" in manual[0].reason


def test_a_symlink_or_a_path_outside_the_tree_is_not_read(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (tmp_path / "secret.py").write_text("def x():\n    pass\n")
    os.symlink(tmp_path / "secret.py", root / "link.py")
    (root / "real.py").write_text("x = 1\n")
    assert fix._read(str(root), "link.py") is None and fix._read(str(root), "../secret.py") is None
    assert fix._read(str(root), "real.py") == "x = 1\n" and fix._read(str(root), "absent.py") is None


def test_a_file_without_a_final_newline_gets_the_marker_git_apply_needs(tmp_path):
    old, new = "def a():\n    pass\n\n\ndef b():\n    pass", "def a():\n    pass\n"
    patch = fix.unified("m.py", old, new)
    (tmp_path / "m.py").write_text(old)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    r = subprocess.run(["git", "-C", str(tmp_path), "apply", "-"], input=patch, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "m.py").read_text() == new


@pytest.mark.parametrize(
    "text,spans,want",
    [
        ("a\n\n\nb\n\n\nc\n", [(4, 4)], "a\n\n\nc\n"),  # between two blocks: the blank lines after it go
        ("a\nb\n\n\nc\n", [(2, 2)], "a\n\n\nc\n"),  # right after code: the blank lines after it stay
        ("a\n\n\nb\n", [(4, 4)], "a\n"),  # the end of the file: no dangling blank lines
        ("b\n\n\nc\n", [(1, 1)], "c\n"),  # the start of the file
        ("a\n\n\nb\n\n\nc\n\n\nd\n", [(4, 4), (7, 7)], "a\n\n\nd\n"),  # two holes
    ],
)
def test_cut_tidies_the_blank_lines(text, spans, want):
    assert fix.cut(text, spans) == want


# ------------------------------------------------------------------ comments above a definition, block headers with a comment
ATTACH = (
    "#!/usr/bin/env python3\n"
    "import os\n"
    "\n"
    "# not attached: a blank line separates it\n"
    "\n"
    "# the helper of the old export\n"
    "# ---- old ----\n"
    "@staticmethod\n"
    "def gone(x):\n"
    "    return x\n"
    "\n"
    "\n"
    "def kept():\n"
    "    return 1\n"
    "\n"
    "\n"
    "class Box:\n"
    "    x = 1\n"
    "    # about a: it goes with a\n"
    "    def a(self):\n"
    "        return 1\n"
    "\n"
    "    # about b, stays: it is a comment of the class body, b is kept\n"
    "    def b(self):\n"
    "        return 2\n"
)


def test_the_comment_lines_directly_above_a_removed_definition_go_with_it():
    new, removed, manual = fix.patch_file(ATTACH, [span(ATTACH, "gone")])
    assert manual == [] and [i.qual for i in removed] == ["gone"]
    assert "the helper of the old export" not in new and "---- old ----" not in new
    assert "# not attached: a blank line separates it" in new and new.startswith("#!/usr/bin/env python3\nimport os\n")
    assert "def kept" in new
    ast.parse(new)
    new, removed, manual = fix.patch_file(ATTACH, [span(ATTACH, "a", "Box.a")])
    assert manual == [] and "about a" not in new and "about b, stays" in new and "def b" in new
    ast.parse(new)


def test_a_comment_of_another_indentation_a_blank_separated_one_and_the_top_lines_stay():
    text = "#!/usr/bin/env python3\ndef f():\n    pass\n"
    new, _, _ = fix.patch_file(text, [span(text, "f")])
    assert new == "#!/usr/bin/env python3\n"  # the shebang is no description of f
    text = "x = 1\n    # indented, not the def's\ndef f():\n    pass\n"
    spans = fix.attached(text, [(3, 4)])
    assert spans == [(3, 4)]
    text = "# a\n\n# b\ndef f():\n    pass\n"
    assert fix.attached(text, [(4, 5)]) == [(3, 5)]
    # a comment line never reaches into the span before it
    text = "def g():\n    pass\n# c\ndef f():\n    pass\n"
    assert fix.attached(text, [(1, 2), (4, 5)]) == [(1, 2), (3, 5)]


def test_two_neighbouring_removals_do_not_overlap_their_comments():
    text = "def a():\n    pass\n# of b\ndef b():\n    pass\n# of c\ndef c():\n    pass\n\n\ndef d():\n    pass\n"
    new, removed, _ = fix.patch_file(text, [span(text, "a"), span(text, "b"), span(text, "c")])
    assert sorted(i.qual for i in removed) == ["a", "b", "c"] and new == "def d():\n    pass\n"


@pytest.mark.parametrize(
    "line,want",
    [
        ("def f():", True),
        ("class A:  # a note", True),
        ("if x:  # why: because", True),
        ("    else:   # nothing", True),
        ("):  # end of a multi-line header", True),
        ('def f(a="#"):', True),
        ('x = "a:"  # c:', False),
        ("x = 1  # comment:", False),
        ("# just a comment:", False),
        ("x = {1: 2}", False),
        ("", False),
        ('s = "# not a comment' + '"', False),
    ],
)
def test_a_block_header_is_read_from_the_tokens_so_a_trailing_comment_hides_nothing(line, want):
    assert fix.opens_block(line) is want


def test_no_blank_line_is_left_after_a_header_with_a_trailing_comment():
    text = "class Box:  # the box\n    def a(self):\n        return 1\n\n    def b(self):\n        return 2\n"
    new, removed, _ = fix.patch_file(text, [span(text, "a", "Box.a")])
    assert [i.qual for i in removed] == ["Box.a"]
    assert new == "class Box:  # the box\n    def b(self):\n        return 2\n"
    text = "if True:  # always\n    pass\nelse:  # never\n    def z():\n        pass\n\n    y = 1\n"
    new, removed, _ = fix.patch_file(text, [span(text, "z")])
    assert new == "if True:  # always\n    pass\nelse:  # never\n    y = 1\n"


# ------------------------------------------------------------------ the confidence floor and the order
def test_the_claims_carry_a_confidence_and_the_lists_go_the_surest_first(repo):
    write(repo, "lib/extra.py", LONE)
    with_chain(repo)
    p = fix.plan(str(repo), "branch", base="HEAD", proof=STIER)
    assert p.removed and all(i.conf >= 0 for i in p.removed)
    confs = [i.conf for i in p.removed]
    assert confs == sorted(confs, reverse=True)  # the surest first
    assert [(-i.conf, i.path, i.line, i.qual) for i in p.removed] == sorted(
        (-i.conf, i.path, i.line, i.qual) for i in p.removed
    )


def test_min_confidence_removes_only_claims_of_at_least_that_number(repo, tmp_path):
    write(repo, "lib/extra.py", LONE)
    with_chain(repo)
    everything = fix.plan(str(repo), "branch", base="HEAD", proof=STIER)
    by_conf = {i.qual: i.conf for i in everything.removed}
    assert len(set(by_conf.values())) >= 2  # the chain and the lone function do not score alike
    top = max(by_conf.values())
    kept = fix.plan(str(repo), "branch", base="HEAD", proof=STIER, min_conf=top)
    assert {i.qual for i in kept.removed} == {q for q, c in by_conf.items() if c >= top}
    why = f"confidence below the floor of {top} (--min-confidence)"
    assert kept.skipped == {why: len(by_conf) - len(kept.removed)}
    new = (applied(repo, kept.patch, tmp_path) / "lib/app.py").read_text() if "lib/app.py" in kept.patch else None
    assert new is None or all(q not in new for q in {i.qual for i in kept.removed})
    assert fix.plan(str(repo), "branch", base="HEAD", proof=STIER, min_conf=101).removed == ()
    assert fix.plan(str(repo), "branch", base="HEAD", proof=STIER, min_conf=0).patch == everything.patch  # no flag: nothing dropped


def test_a_claim_without_a_number_is_skipped_by_a_floor_and_kept_without_one():
    g = canonical([Node("python:b.py#g", "func", "python", "b.py", 1, 2, "", "public", 0)])
    f = Finding("W", "b.py", 1, "dc.dead", "`g` is dead", "corroborated: s-tier")
    assert f.confidence is None
    todo, _, skipped = fix.candidates([f], g, (), (), min_conf=1)
    assert todo == {} and sum(skipped.values()) == 1
    todo, _, skipped = fix.candidates([f], g, (), ())
    assert list(todo) == ["b.py"] and skipped == {} and todo["b.py"][0][2].conf == -1
    f.confidence = 70
    assert list(fix.candidates([f], g, (), (), min_conf=70)[0]) == ["b.py"]
    assert fix.candidates([f], g, (), (), min_conf=71)[0] == {}


def test_the_files_of_the_patch_go_the_surest_first_and_json_has_the_confidence(repo, monkeypatch, capsys):
    write(repo, "lib/extra.py", LONE)
    with_chain(repo)
    prove(monkeypatch)
    p = fix.plan(str(repo), "branch", base="HEAD", proof=STIER)
    heads = [ln[6:] for ln in p.patch.splitlines() if ln.startswith("+++ b/")]
    first = p.removed[0]
    assert heads[0] == first.path and sorted(heads) == ["lib/app.py", "lib/extra.py"]
    assert cli.main(["fix", "--dry-run", "--root", str(repo), "--base", "HEAD", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [r["confidence"] for r in doc["removed"]] == [i.conf for i in p.removed]
    with pytest.raises(SystemExit) as exc:  # a usage error of argparse
        cli.main(["fix", "--dry-run", "--root", str(repo), "--base", "HEAD", "--min-confidence", "101"])
    assert exc.value.code == 2
    capsys.readouterr()
    assert cli.main(["fix", "--dry-run", "--root", str(repo), "--base", "HEAD", "--min-confidence", "100", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["removed"] == [] and any("--min-confidence" in k for k in doc["skipped"])


def test_without_verify_the_planner_gets_no_verify_argument_and_the_json_has_no_verify_key(repo, capsys, monkeypatch):
    seen = {}

    def planner(root, scope, codes, paths, base, **kw):
        seen["kw"] = kw
        return fix.Plan("", (), (), {}, ())

    a = SimpleNamespace(
        dry_run=True, scope="branch", code=None, path=None, base=None, json=True, root=str(repo), verify=False
    )
    assert fix.run(a, planner=planner) == 0 and "verify" not in seen["kw"] and "min_conf" not in seen["kw"]
    assert "verify" not in json.loads(capsys.readouterr().out)
    monkeypatch.setattr(fix, "_exec_target", lambda root, host=None, user=None, port=None: (None, "no target in this test"))
    a.verify, a.host = True, None
    assert fix.run(a, planner=planner) == 0 and seen["kw"]["verify"].target is None


def test_the_execution_target_is_resolved_in_one_function_and_a_build_without_the_runtime_mode_says_so(monkeypatch):
    target, why = fix._exec_target("/nowhere", None)
    if why == "runtime mode is not available in this build":
        assert target is None
    opts = fix._options("/nowhere")
    assert opts.target is None or hasattr(opts.target, "run")
    monkeypatch.setattr(fix, "_exec_target", lambda root, host=None, user=None, port=None: (_ for _ in ()).throw(OSError("disk")))
    o = fix._options("/nowhere")
    assert o.target is None and "the execution target cannot be configured (OSError: disk)" in o.why
    monkeypatch.setattr(fix, "_exec_target", lambda root, host=None, user=None, port=None: ("T", "") if host == "h" else (None, "off"))
    assert fix._options("/x", "h").target == "T" and fix._options("/x").why == "off"
