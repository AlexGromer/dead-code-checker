"""dead_code_checker.langs and the extra language groups of dead_code_checker.core.

    python3 -m pytest -q tests/test_deadcode_langs.py

Every repository lives under tmp_path (real git); nothing touches the network. Fixtures are inline strings.
"""

import hashlib
import os
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from dead_code_checker import core as dc  # noqa: E402
from dead_code_checker import langs as dl  # noqa: E402
from test_deadcode import mkrepo, sh, write  # noqa: E402

UNREF = "deadcode.unreferenced-symbol"
PUBLIC = "deadcode.unreferenced-public-symbol"
NAME_RE = re.compile(r"\b(?:function|class|constant|selector) (\S+?):")


def audit(root, langs=None, **kw):
    kw.setdefault("foreign", True)
    kw.setdefault("public_api", "as-private")
    kw.setdefault("backend", "git")
    kw.setdefault("jobs", 1)
    return dc.scan([str(root)], root=str(root), languages=langs, **kw)


def names(fs, *codes):
    codes = codes or (UNREF, PUBLIC)
    return {NAME_RE.search(f.msg).group(1) for f in fs if f.code in codes and NAME_RE.search(f.msg)}


def tree_hash(root):
    h = hashlib.sha256()
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for n in sorted(files):
            p = os.path.join(d, n)
            h.update(os.path.relpath(p, root).encode() + b"\0")
            if os.path.islink(p):
                h.update(b"link:" + os.readlink(p).encode())
            else:
                with open(p, "rb") as fh:
                    h.update(fh.read())
    return h.hexdigest()


# ---------------------------------------------------------------- 0. tables
def test_language_tables_agree_between_the_two_modules():
    assert set(dc.EXTRA_LANG_TAGS) == set(dl.EXTRA_LANGS)
    assert set(dc.CORE_LANGS) == set(dl.LANG_GROUPS["core"])
    assert set(dl.LANG_EXT_EXTRA.values()) == set(dl.EXTRA_LANGS)
    assert dl.resolve(["A"]) == set(dl.LANG_GROUPS["A"])
    assert dl.resolve(["all"]) >= set(dl.EXTRA_LANGS) | set(dc.CORE_LANGS)
    assert dl.resolve(["java", "B"]) == {"java", "rb", "lua", "ex", "hs", "r"}


# ---------------------------------------------------------------- 1. one fixture per language
# (extension of the declarations, declaration(name, recursive), extension of the user file, user(used, commented))
CASES = {
    "java": (
        "java",
        lambda n, r: f"class {n} {{\n  {n if r else 'int'} x;\n}}\n",
        "java",
        lambda u, c: f"class Entry {{\n  {u} v;\n}}\n// {c} is only a comment\n",
        "Cap",
    ),
    "c": (
        "c",
        lambda n, r: f"static int {n}(int a) {{\n  return {n + '(a - 1)' if r else 'a + 1'};\n}}\n",
        "c",
        lambda u, c: f"int main(void) {{\n  return {u}(1);\n}}\n// {c} is only a comment\n",
        "low",
    ),
    "cs": (
        "cs",
        lambda n, r: f"class {n} {{\n  {n if r else 'int'} x;\n}}\n",
        "cs",
        lambda u, c: f"class Program {{\n  {u} v;\n}}\n// {c} is only a comment\n",
        "Cap",
    ),
    "kt": (
        "kt",
        lambda n, r: f"fun {n}(a: Int): Int {{\n  return {n + '(a - 1)' if r else 'a + 1'}\n}}\n",
        "kt",
        lambda u, c: f"fun entry() {{\n  {u}(2)\n}}\n// {c} is only a comment\n",
        "low",
    ),
    "swift": (
        "swift",
        lambda n, r: f"func {n}(_ a: Int) -> Int {{\n  return {n + '(a - 1)' if r else 'a + 1'}\n}}\n",
        "swift",
        lambda u, c: f"func entry() {{\n  _ = {u}(1)\n}}\n// {c} is only a comment\n",
        "low",
    ),
    "scala": (
        "scala",
        lambda n, r: f"def {n}(a: Int): Int = {n + '(a - 1)' if r else 'a + 1'}\n",
        "scala",
        lambda u, c: f"object Entry {{\n  val x = {u}(1)\n}}\n// {c} is only a comment\n",
        "low",
    ),
    "dart": (
        "dart",
        lambda n, r: f"int {n}(int a) {{\n  return {n + '(a - 1)' if r else 'a + 1'};\n}}\n",
        "dart",
        lambda u, c: f"void entry() {{\n  {u}(1);\n}}\n// {c} is only a comment\n",
        "low",
    ),
    "php": (
        "php",
        lambda n, r: f"function {n}($a) {{\n  return {n + '($a - 1)' if r else '$a + 1'};\n}}\n",
        "php",
        lambda u, c: f"<?php\nentry({u}(1));\n// {c} is only a comment\n",
        "low",
    ),
    "rb": (
        "rb",
        lambda n, r: f"def {n}(a)\n  {n + '(a - 1)' if r else 'a + 1'}\nend\n",
        "rb",
        lambda u, c: f"puts {u}(1)\n# {c} is only a comment\n",
        "snake",
    ),
    "lua": (
        "lua",
        lambda n, r: f"local function {n}(a)\n  return {n + '(a - 1)' if r else 'a + 1'}\nend\n",
        "lua",
        lambda u, c: f"print({u}(1))\n-- {c} is only a comment\n",
        "snake",
    ),
    "ex": (
        "ex",
        lambda n, r: f"defmodule {n} do\n  def f, do: {n + '.f()' if r else '1'}\nend\n",
        "exs",
        lambda u, c: f"IO.inspect({u}.f())\n# {c} is only a comment\n",
        "Cap",
    ),
    "hs": (
        "hs",
        lambda n, r: f"{n} :: Int -> Int\n{n} 0 = 0\n{n} a = {n + ' (a - 1)' if r else 'a + 1'}\n",
        "hs",
        lambda u, c: f"main :: IO ()\nmain = print ({u} 1)\n-- {c} is only a comment\n",
        "low",
    ),
    "r": (
        "r",
        lambda n, r: f"{n} <- function(a) {{\n  {n + '(a - 1)' if r else 'a + 1'}\n}}\n",
        "R",
        lambda u, c: f"print({u}(1))\n# {c} is only a comment\n",
        "snake",
    ),
}
STYLE = {
    "Cap": ("DeadAlpha", "UsedAlpha", "RecAlpha", "CmtAlpha"),
    "low": ("deadAlpha", "usedAlpha", "recAlpha", "cmtAlpha"),
    "snake": ("dead_alpha", "used_alpha", "rec_alpha", "cmt_alpha"),
}


@pytest.mark.parametrize("lang", sorted(CASES))
def test_dead_used_recursive_and_comment_only_per_language(tmp_path, lang):
    ext, decl, uext, user, style = CASES[lang]
    dead, used, rec, cmt = STYLE[style]
    files = {
        f"src/decls.{ext}": decl(dead, False)
        + "\n"
        + decl(used, False)
        + "\n"
        + decl(rec, True)
        + "\n"
        + decl(cmt, False),
        f"src/user.{uext}": user(used, cmt),
    }
    root = mkrepo(tmp_path)
    for rel, text in files.items():
        write(root, rel, text)
    got = names(audit(root, [lang]))
    assert {dead, rec, cmt} <= got, (lang, got)
    assert used not in got, (lang, got)


def test_css_selectors_dead_used_selector_only_and_ignore(tmp_path):
    root = mkrepo(tmp_path)
    write(
        root,
        "web/site.css",
        ".btn-unused { color: red; }\n.btn { color: blue; }\n.only-sel { margin: .5em; }\n"
        ".wrap .only-sel:hover { margin: 0; }\n/* deadcode: ignore */\n.kept-sel { x: y }\n#hero { width: 1px; }\n",
    )
    write(root, "web/page.html", '<div class="btn"><p id="hero"></p></div>\n')
    write(root, "web/app.jsx", 'export const A = () => <b className="btn">x</b>;\n')
    got = names(audit(root, ["css"]), UNREF)
    assert {"btn-unused", "only-sel"} <= got
    assert not ({"btn", "hero", "kept-sel"} & got)
    assert "5em" not in got and not any(n[0].isdigit() for n in got)


# ---------------------------------------------------------------- 2. stubs of group A
STUBS = {
    "kt": ("kt", "fun stubby(): Int {\n  TODO()\n}\n", "fun real(): Int {\n  return 3 + 4\n}\n"),
    "cs": ("cs", "class K {\n}\n", None),
    "scala": ("scala", "def stubby(a: Int): Int = {\n  ???\n}\n", "def real(a: Int): Int = {\n  a * 3 + 4\n}\n"),
    "swift": (
        "swift",
        'func stubby() {\n  fatalError("not implemented")\n}\n',
        "func real() -> Int {\n  return 3 + 4\n}\n",
    ),
    "dart": ("dart", "int stubby() {\n  throw UnimplementedError();\n}\n", "int real() {\n  return 3 + 4;\n}\n"),
    "php": (
        "php",
        "function stubby() {\n  throw new \\Exception('not implemented');\n}\n",
        "function real() {\n  return 3 + 4;\n}\n",
    ),
    "c": ("c", "static void stubby(void) {\n}\n", "static int real(void) {\n  return 3 + 4;\n}\n"),
}


@pytest.mark.parametrize("lang", sorted(k for k, v in STUBS.items() if v[2]))
def test_stub_functions_of_group_a(tmp_path, lang):
    ext, stub, real = STUBS[lang]
    root = mkrepo(tmp_path)
    write(root, f"src/a.{ext}", stub + "\n" + real)
    fs = audit(root, [lang])
    stubs = {f.msg.split(":")[0] for f in fs if f.code == "deadcode.stub-function"}
    assert stubs == {"stubby"}, (lang, stubs)


def test_csharp_throw_not_implemented_is_a_stub(tmp_path):
    assert dl.body_stub("cs", "\n  throw new NotImplementedException();\n")
    assert dl.body_stub("cs", "\n  return 3 + 4;\n") is None
    assert dl.body_stub("java", '\n  throw new UnsupportedOperationException("not implemented");\n')


# ---------------------------------------------------------------- 3. annotations
def test_framework_annotations_mean_used_and_harmless_ones_do_not(tmp_path):
    root = mkrepo(tmp_path)
    write(
        root,
        "src/A.java",
        "@Service\nclass ManagedBean {\n  int x;\n}\n\n@Deprecated\nclass OldBean {\n  int y;\n}\n\n"
        '@SuppressWarnings("all") class QuietBean {\n  int z;\n}\n',
    )
    write(
        root, "src/B.cs", "[ApiController]\nclass CtlBean {\n  int x;\n}\n\n[Obsolete]\nclass ObsBean {\n  int y;\n}\n"
    )
    write(root, "src/C.swift", "@main\nstruct AppBean {\n}\n\npublic func hello() {\n  print(3 + 4)\n}\n")
    got = names(audit(root, ["java", "cs", "swift"]))
    assert {"OldBean", "ObsBean", "QuietBean"} <= got
    assert not ({"ManagedBean", "CtlBean", "AppBean"} & got)
    assert "hello" in got  # the @main line above is not an annotation of the next declaration


# ---------------------------------------------------------------- 4. public API modes
PUB_FILES = {
    "p/lib.go": "package p\n\nfunc Exported() int {\n\treturn 4 + 5\n}\n\nfunc private() int {\n\treturn 6 + 7\n}\n",
    "p/lib.rs": "pub fn exposed() -> i32 {\n    3 + 4\n}\n\nfn hidden() -> i32 {\n    5 + 6\n}\n",
    "p/Pub.java": "public class PubThing {\n  int x;\n}\n\nclass PrivThing {\n  int y;\n}\n",
}


def test_public_api_modes(tmp_path):
    root = mkrepo(tmp_path)
    for rel, text in PUB_FILES.items():
        write(root, rel, text)
    langs = ["go", "rs", "java"]
    rep = audit(root, langs, public_api="report")
    assert names(rep, PUBLIC) == {"Exported", "exposed", "PubThing"}
    assert {"private", "hidden", "PrivThing"} <= names(rep, UNREF) and not (
        names(rep, UNREF) & {"Exported", "exposed", "PubThing"}
    )
    ex = audit(root, langs, public_api="exempt")
    assert names(ex, PUBLIC) == set() and not (names(ex) & {"Exported", "exposed", "PubThing"})
    assert {"private", "hidden", "PrivThing"} <= names(ex, UNREF)
    ap = audit(root, langs, public_api="as-private")
    assert names(ap, PUBLIC) == set() and {"Exported", "exposed", "PubThing", "private"} <= names(ap, UNREF)


# ---------------------------------------------------------------- 5. test / entry / generated conventions
def test_test_names_entries_and_generated_files():
    for t in (
        "src/FooTest.java",
        "a/FooTests.cs",
        "x/FooSpec.scala",
        "q/foo_test.rb",
        "r/test-thing.R",
        "h/FooSpec.hs",
    ):
        assert dc.is_test(t), t
    assert not dc.is_test("src/Contest.java") and not dc.is_test("src/Latest.java")
    assert {"Program.cs", "mix.exs", "Rakefile"} <= dc.ENTRY_NAMES


def test_generated_vendor_and_foreign_ignore_paths_are_skipped(tmp_path):
    root = mkrepo(tmp_path)
    write(root, "lib/real.dart", "int lonelyOne(int a) {\n  return a + 1;\n}\n")
    write(root, "lib/model.g.dart", "int generatedOne(int a) {\n  return a + 2;\n}\n")
    write(
        root,
        "gen/api.go",
        "// Code generated by protoc. DO NOT EDIT.\npackage gen\n\nfunc GeneratedTwo() int {\n\treturn 9 + 9\n}\n",
    )
    write(root, "Pods/Vendored.swift", "func vendoredOne() {\n  print(1 + 2)\n}\n")
    write(root, "obj/Debug.cs", "class ObjThing {\n  int x;\n}\n")
    got = names(audit(root, ["all"]))
    assert "lonelyOne" in got
    assert not ({"generatedOne", "GeneratedTwo", "vendoredOne", "ObjThing"} & got)
    assert dc.LAST_STATS["skipped"].get("generated", 0) >= 1


# ---------------------------------------------------------------- 6. block ends of group B
def test_group_b_definitions_have_correct_ranges():
    rb = {
        s.name: s
        for s in dl.symbols("rb", "class Foo::Bar\n  def x\n  end\nend\ndef helper(a)\n  helper(a)\nend\nLIMIT = 3\n")
    }
    assert (rb["Bar"].start, rb["Bar"].end) == (1, 4) and (rb["helper"].start, rb["helper"].end) == (5, 7)
    assert rb["LIMIT"].kind == "constant"
    lua = {
        s.name: s
        for s in dl.symbols("lua", "local function a(x)\n  return a(x)\nend\nfunction M.pub(y)\n  return 1\nend\n")
    }
    assert (lua["a"].start, lua["a"].end, lua["a"].public) == (1, 3, False) and lua["pub"].public
    ex = dl.symbols("ex", "defmodule A.B.C do\n  def f, do: 1\nend\n")
    assert [(s.name, s.start, s.end) for s in ex] == [("C", 1, 3)]
    hs = {
        s.name: s
        for s in dl.symbols(
            "hs",
            "module M where\nhelper :: Int -> Int\nhelper 0 = 1\nhelper n = helper (n-1)\n\ndata T = T Int\nmain :: IO ()\n",
        )
    }
    assert (hs["helper"].start, hs["helper"].end) == (2, 4) and "main" not in hs and hs["T"].kind == "class"
    r = {s.name: s for s in dl.symbols("r", "#' @export\npub <- function(x) {\n  x + 1\n}\npriv = function(y) y\n")}
    assert (r["pub"].start, r["pub"].end, r["pub"].public) == (2, 4, True) and r["priv"].public is False


def test_self_call_in_a_group_b_body_does_not_count(tmp_path):
    root = mkrepo(tmp_path)
    write(root, "app/helper.rb", "def only_here(a)\n  only_here(a - 1)\nend\n")
    assert "only_here" in names(audit(root, ["rb"]))


# ---------------------------------------------------------------- 7. a foreign tree is only read
def test_foreign_scan_writes_nothing_and_runs_no_git_command_from_the_tree(tmp_path):
    root = mkrepo(tmp_path)
    write(root, "src/a.java", "class Lonely {\n  int x;\n}\n")
    write(root, "src/b.py", "def lonely_py(a):\n    return a + 1\n")
    marker = tmp_path / "fsmonitor-ran"
    sh(root, "config", "core.fsmonitor", f"touch {marker}")
    sh(root, "config", "core.pager", f"touch {marker}")
    before = tree_hash(root)
    fs = audit(root, ["all"])
    assert "Lonely" in names(fs)
    assert not marker.exists()
    assert tree_hash(root) == before


def test_foreign_scan_never_reads_a_symlink(tmp_path):
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("def leaked_secret_fn(a):\n    return 'TOPSECRET-VALUE'\n")
    root = mkrepo(tmp_path)
    os.symlink(secret, root / "leak.py")
    write(root, "ok.py", "def visible_fn(a):\n    return a + 1\n")
    fs = audit(root, ["py"])
    assert "visible_fn" in names(fs) and "leaked_secret_fn" not in names(fs)
    assert "TOPSECRET" not in "\n".join(f.render() for f in fs)
    assert dc.LAST_STATS["skipped"].get("symlink") == 1


def test_foreign_scan_ignores_the_target_config_unless_asked(tmp_path):
    root = mkrepo(tmp_path, {".dead-code-checker.toml": "[deadcode]\nnot_a_key = 1\nstrict_dirs = ['bin']\n"})
    write(root, "bin/tool.py", "x = 1\n")
    fs = audit(root, ["py"])
    assert not [f for f in fs if f.code == "deadcode.bad-config"]
    assert [f for f in fs if f.code == "deadcode.unreferenced-file" and f.level == "W"]
    assert not [f for f in fs if f.level == "E"]
    fs2 = audit(root, ["py"], use_repo_config=True)
    assert [f for f in fs2 if f.code == "deadcode.bad-config"]


def test_report_inside_the_target_is_refused(tmp_path, capsys):
    root = mkrepo(tmp_path)
    write(root, "a.py", "def f():\n    return 1\n")
    before = tree_hash(root)
    rc = dc.main(["scan", str(root), "--root", str(root), "--foreign", "--report", str(root / "REPORT.md")])
    assert rc == 2 and not (root / "REPORT.md").exists() and tree_hash(root) == before
    assert "outside the scanned tree" in capsys.readouterr().err


# ---------------------------------------------------------------- 8. parallel
def big_tree(tmp_path):
    root = mkrepo(tmp_path)
    for i in range(40):
        write(
            root,
            f"rb/m{i}.rb",
            f"def dead_rb_{i}(a)\n  a + {i}\nend\n\ndef used_rb_{i}(a)\n  a\nend\nputs used_rb_{i}(1)\n",
        )
        write(
            root,
            f"java/C{i}.java",
            f"class Dead{i} {{\n  int x;\n}}\n\nclass Live{i} {{\n  Dead{i if i % 2 else 0} y;\n}}\n",
        )
        write(root, f"css/s{i}.css", f".dead-{i} {{ color: red; }}\n.live-{i} {{ color: blue; }}\n")
        write(root, f"py/p{i}.py", f"def dead_py_{i}(a):\n    return a + {i}\n")
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", "more")
    return root


@pytest.mark.parametrize("backend", ["git", "walk"])
def test_jobs_one_and_many_give_identical_findings(tmp_path, backend):
    root = big_tree(tmp_path)

    def run(jobs):
        fs = audit(root, ["all"], jobs=jobs, backend=backend)
        return [f.render() for f in fs], dict(dc.LAST_STATS)

    serial, _ = run(1)
    many, st = run(4)
    assert serial == many and len(serial) > 100
    assert st["extracted"] > 0 and st["jobs"] == 4


# ---------------------------------------------------------------- 9. staged / session stay on the core languages
def test_languages_are_opt_in_for_staged(tmp_path):
    java = {"src/Thing.java": "class Orphan {\n  int x;\n}\n"}
    root = mkrepo(tmp_path)
    write(root, "src/Thing.java", java["src/Thing.java"])
    sh(root, "add", "-A")
    assert dc.check_staged(str(root)) == []
    root2 = mkrepo(tmp_path, {".dead-code-checker.toml": '[deadcode]\nlanguages = ["java"]\n'}, name="opt")
    write(root2, "src/Thing.java", java["src/Thing.java"])
    sh(root2, "add", "-A")
    assert "Orphan" in names(dc.check_staged(str(root2)))
    root3 = mkrepo(tmp_path, {".dead-code-checker.toml": '[deadcode]\nlanguages = ["cobol"]\n'}, name="bad")
    cfg, probs = dc.load_config(str(root3))
    assert [p for p in probs if p.code == "deadcode.bad-config" and p.level == "E"]
    assert cfg["languages"] == list(dc.CORE_LANGS)


# ---------------------------------------------------------------- 10. CLI
def test_cli_foreign_scan_with_report(tmp_path, capsys):
    root = mkrepo(tmp_path)
    write(root, "app/a.rb", "def orphan_rb(a)\n  a + 1\nend\n")
    write(root, "app/b.java", "class IgnoredByLang {\n  int x;\n}\n")
    rep = tmp_path / "out" / "report.md"
    rep.parent.mkdir()
    rc = dc.main(
        [
            "scan",
            str(root),
            "--root",
            str(root),
            "--foreign",
            "--lang",
            "B",
            "--public-api",
            "exempt",
            "--jobs",
            "2",
            "--report",
            str(rep),
        ]
    )
    assert rc == 0
    text = rep.read_text()
    assert "orphan_rb" in text and "IgnoredByLang" not in text
    assert "files per language: rb 1" in text and "target HEAD:" in text and "def orphan_rb" not in text
    capsys.readouterr()
    assert dc.main(["defaults"]) == 0
    assert "languages = " in capsys.readouterr().out


def test_cli_rejects_an_unknown_language(tmp_path, capsys):
    root = mkrepo(tmp_path)
    assert dc.main(["scan", str(root), "--root", str(root), "--lang", "cobol"]) == 2


# ---------------------------------------------------------------- 11. hostile git config, report guard
def _hostile_worktree(tmp_path):
    """A target whose .git/config redirects the work tree to a directory the attacker chose."""
    root = mkrepo(tmp_path, {"a.rb": "def lonely_rb(a)\n  a\nend\n"}, name="tgt")
    other = tmp_path / "elsewhere"
    other.mkdir()
    write(other, "decoy.rb", "def decoy_rb(a)\n  a\nend\n")
    sh(root, "config", "core.worktree", str(other))
    return root, other


def test_hostile_core_worktree_does_not_hijack_the_scan_base(tmp_path):
    root, other = _hostile_worktree(tmp_path)
    assert dc.toplevel(str(root)) == str(root.resolve())
    fs = dc.scan([str(root)], foreign=True, backend="git", jobs=1)
    assert "lonely_rb" in names(fs) and "decoy_rb" not in names(fs)
    assert not dc.LAST_STATS.get("skipped", {}).get("symlink")
    assert "rb" in dc.LAST_STATS.get("languages", {"rb": 1})


def test_hostile_core_worktree_does_not_redirect_the_reference_index(tmp_path):
    root, other = _hostile_worktree(tmp_path)
    write(other, "user.rb", "lonely_rb(1)\n")  # a use that exists only in the attacker's directory
    fs = dc.scan([str(root)], foreign=True, backend="git", jobs=1)
    assert "lonely_rb" in names(fs)


def test_report_guard_follows_symlinks_to_the_target(tmp_path):
    root = mkrepo(tmp_path, {"a.py": "def f():\n    return 1\n"}, name="tgt")
    link = tmp_path / "L"
    link.symlink_to(root, target_is_directory=True)
    before = tree_hash(root)
    rc = dc.main(["scan", str(link), "--foreign", "--report", str(root / "R1.md")])
    assert rc == 2 and not (root / "R1.md").exists() and tree_hash(root) == before


def test_report_guard_checks_every_path_not_only_the_first(tmp_path):
    t1 = mkrepo(tmp_path, {"a.py": "def f():\n    return 1\n"}, name="t1")
    t2 = mkrepo(tmp_path, {"b.py": "def g():\n    return 1\n"}, name="t2")
    rc = dc.main(["scan", str(t1), str(t2), "--foreign", "--report", str(t2 / "R2.md")])
    assert rc == 2 and not (t2 / "R2.md").exists()


def test_report_guard_ignores_a_hostile_core_worktree(tmp_path):
    root, other = _hostile_worktree(tmp_path)
    rc = dc.main(["scan", str(root), "--foreign", "--report", str(root / "R3.md")])
    assert rc == 2 and not (root / "R3.md").exists()


def test_report_outside_every_target_is_still_written(tmp_path, capsys):
    root = mkrepo(tmp_path, {"a.py": "def f():\n    return 1\n"}, name="tgt")
    out = tmp_path / "out" / "R.md"
    out.parent.mkdir()
    rc = dc.main(["scan", str(root), "--foreign", "--report", str(out)])
    assert rc in (0, 1) and out.is_file()


def test_report_guard_follows_symlinks_for_a_target_that_is_not_a_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    write(plain, "a.rb", "def f(a)\n  a\nend\n")
    link = tmp_path / "L"
    link.symlink_to(plain, target_is_directory=True)
    rc = dc.main(["scan", str(link), "--foreign", "--report", str(plain / "R1.md")])
    assert rc == 2 and not (plain / "R1.md").exists()
    rc = dc.main(["scan", str(plain / "a.rb"), "--foreign", "--report", str(plain / "R4.md")])
    assert rc == 2 and not (plain / "R4.md").exists()


def test_multi_path_foreign_scan_refuses_a_path_outside_the_base(tmp_path):
    t1 = mkrepo(tmp_path, {"a.rb": "def f1(a)\n  a\nend\n"}, name="t1")
    t2 = mkrepo(tmp_path, {"b.rb": "def f2(a)\n  a\nend\n"}, name="t2")
    with pytest.raises(ValueError, match="outside"):
        dc.scan([str(t1), str(t2)], foreign=True, jobs=1)


def test_multi_path_foreign_scan_cli_exits_2_and_audits_nothing(tmp_path, capsys):
    t1 = mkrepo(tmp_path, {"a.rb": "def f1(a)\n  a\nend\n"}, name="t1")
    t2 = mkrepo(tmp_path, {"b.rb": "def f2(a)\n  a\nend\n"}, name="t2")
    rc = dc.main(["scan", str(t1), str(t2), "--foreign"])
    cap = capsys.readouterr()
    assert rc == 2 and "outside" in cap.err and "f1" not in cap.out


def test_multi_path_foreign_scan_inside_one_base_reads_every_path(tmp_path):
    root = mkrepo(tmp_path, {"d1/a.rb": "def f1(a)\n  a\nend\n", "d2/b.rb": "def f2(a)\n  a\nend\n"}, name="one")
    fs = dc.scan([str(root / "d1"), str(root / "d2")], foreign=True, jobs=1, backend="git")
    assert any("f1" in f.msg for f in fs) and any("f2" in f.msg for f in fs)
    assert {f.path for f in fs} >= {"d1/a.rb", "d2/b.rb"}


def test_multi_path_foreign_scan_with_explicit_root_refuses_the_outside_path(tmp_path):
    t1 = mkrepo(tmp_path, {"a.rb": "def f1(a)\n  a\nend\n"}, name="t1")
    t2 = mkrepo(tmp_path, {"b.rb": "def f2(a)\n  a\nend\n"}, name="t2")
    with pytest.raises(ValueError, match="outside"):
        dc.scan([str(t1), str(t2)], root=str(t1), foreign=True, jobs=1)


# ---------------------------------------------------------------- hostile trees: symlinked parent dir, special files
import subprocess  # noqa: E402

def _cli_scan(root, *extra, timeout=30):
    return subprocess.run(
        [sys.executable, "-P", "-m", "dead_code_checker", "scan", str(root), "--foreign", *extra],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _tracked_then_symlinked_dir(tmp_path, outside_name="ref.rb"):
    """A repo whose index tracks evil/ref.rb, but evil is now a symlink to a directory outside the tree."""
    root = mkrepo(tmp_path, {"a.rb": "def fn0007(a)\n  a\nend\n", "evil/ref.rb": "x\n"})
    out = tmp_path / "out"
    out.mkdir()
    (root / "evil" / "ref.rb").unlink()
    (root / "evil").rmdir()
    os.symlink(out, root / "evil")
    return root, out


def test_foreign_walker_never_reads_through_a_symlinked_parent_dir(tmp_path):
    root, out = _tracked_then_symlinked_dir(tmp_path)
    (out / "ref.rb").write_text("fn0007(1)\n")
    sk = {}
    ri = dc.RefIndex([str(root)], backend="walk", foreign=True, skipped=sk)
    hits = ri.find(["fn0007"])
    assert not [h for h in hits["fn0007"] if str(out) in os.path.realpath(h.path)]
    assert all(str(root) in h.path for h in hits["fn0007"])
    assert sk.get("outside-root", 0) >= 1
    fs = audit(root, ["rb"], backend="walk")
    assert "fn0007" in names(fs)  # the outside file does not suppress the finding
    assert dc.LAST_STATS["skipped"].get("outside-root", 0) >= 1


def test_foreign_walker_reads_ordinary_files_in_real_subdirs(tmp_path):
    root = mkrepo(tmp_path, {"a.rb": "def fn0007(a)\n  a\nend\n", "sub/ref.rb": "fn0007(1)\n"})
    sk = {}
    hits = dc.RefIndex([str(root)], backend="walk", foreign=True, skipped=sk).find(["fn0007"])
    assert any(h.path.endswith("sub/ref.rb") for h in hits["fn0007"]) and not sk.get("outside-root")


def test_foreign_scan_does_not_hang_on_a_fifo_in_the_tree(tmp_path):
    root = mkrepo(tmp_path, {"a.rb": "def g1(a)\n  a\nend\n"})
    os.mkfifo(root / "b.rb")
    r = _cli_scan(root, "--report", str(tmp_path / "r.md"))
    assert r.returncode == 0, r.stderr
    assert "special 1" in (tmp_path / "r.md").read_text()


def test_foreign_walker_skips_a_fifo_and_counts_it(tmp_path):
    root = tmp_path / "plain"  # not a repository: the walker lists the directory itself (git omits special files)
    (root).mkdir()
    (root / "a.rb").write_text("def g1(a)\n  a\nend\n")
    os.mkfifo(root / "b.rb")
    sk = {}
    hits = dc.RefIndex([str(root)], backend="walk", foreign=True, skipped=sk).find(["g1"])
    assert hits["g1"] and sk.get("special") == 1


def test_foreign_scan_does_not_hang_on_a_fifo_behind_a_symlinked_dir(tmp_path):
    root, out = _tracked_then_symlinked_dir(tmp_path)
    os.mkfifo(out / "ref.rb")
    # more than GIT_MAX_NAMES candidate names: the default backend falls back to the walker
    (root / "many.rb").write_text("".join(f"def m{i:04d}(a)\n  a\nend\n" for i in range(320)))
    r = _cli_scan(root, "--report", str(tmp_path / "r.md"))
    assert r.returncode == 0, r.stderr
    assert "outside-root" in (tmp_path / "r.md").read_text()


def test_non_foreign_scan_does_not_hang_on_a_fifo_either(tmp_path):
    root = tmp_path / "plain"  # not a repository: the walker lists the directory itself (git omits special files)
    (root).mkdir()
    (root / "a.rb").write_text("def g1(a)\n  a\nend\n")
    os.mkfifo(root / "b.rb")
    sk = {}
    hits = dc.RefIndex([str(root)], backend="walk", skipped=sk).find(["g1"])
    assert hits["g1"] and sk.get("special") == 1


# ---------------------------------------------------------------- fix round 5: cwd-relative paths, linear extractors
def _cli_in(cwd, *args, timeout=60):
    return subprocess.run(
        [sys.executable, "-P", "-m", "dead_code_checker", *args], capture_output=True, text=True, timeout=timeout, cwd=str(cwd)
    )


def test_cli_foreign_scan_of_a_path_relative_to_cwd_audits_it(tmp_path):
    t = tmp_path / "T"
    (t / "sub").mkdir(parents=True)
    sh(t, "init", "-q")
    (t / "sub" / "a.rb").write_text("def dead_one(a)\n  a\nend\n")
    rel = _cli_in(tmp_path, "scan", "T", "--foreign", "--json")
    ab = _cli_in(tmp_path, "scan", str(t), "--foreign", "--json")
    assert rel.returncode == ab.returncode == 0, rel.stderr
    assert "dead_one" in ab.stdout and "dead_one" in rel.stdout
    assert rel.stdout == ab.stdout


def test_cli_foreign_scan_of_a_relative_path_outside_the_base_is_refused(tmp_path):
    t1 = mkrepo(tmp_path, {"a.rb": "def f1(a)\n  a\nend\n"}, name="T1")
    mkrepo(tmp_path, {"b.rb": "def f2(a)\n  a\nend\n"}, name="T2")
    r = _cli_in(tmp_path, "scan", str(t1), "T2", "--foreign", "--report", str(tmp_path / "R.md"))
    assert r.returncode == 2 and "outside" in r.stderr and not (tmp_path / "R.md").exists()
    r = _cli_in(tmp_path, "scan", "T1", "T2", "--foreign")
    assert r.returncode == 2 and "outside" in r.stderr


def test_scan_api_resolves_a_relative_path_from_cwd(tmp_path, monkeypatch):
    root = mkrepo(tmp_path, {"sub/a.rb": "def dead_two(a)\n  a\nend\n", "sub/b.py": "def dead_three(a):\n    return a\n"}, name="R")
    monkeypatch.chdir(tmp_path)
    for foreign in (True, False):
        fs = dc.scan(["R/sub"], foreign=foreign, jobs=1, backend="git")
        assert any("dead_three" in f.msg for f in fs), foreign
        assert any("dead_two" in f.msg for f in fs) == foreign
    assert root.exists()


def _symbols_in_subprocess(lang, gen, timeout=25):
    code = (
        "import sys,json; sys.path.insert(0, sys.argv[1]); from dead_code_checker import langs as L\n"
        "t = (" + gen + ")\n"
        "print(len(L.symbols(sys.argv[2], t)), len(t))\n"
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", code, str(ROOT), lang], capture_output=True, text=True, timeout=timeout
    )


@pytest.mark.parametrize(
    "lang,gen",
    [
        ("java", "''.join('class A%d {\\n' % i for i in range(30000))"),
        ("java", "''.join('public void m%d() {\\n' % i for i in range(30000))"),
        ("java", "''.join('class A%d { String s = \"\\n' % i for i in range(30000))"),
        ("kt", "''.join('fun f%d(x: Int) {\\n' % i for i in range(30000))"),
        ("php", "'namespace X {\\n' + ''.join('function f%d() {\\n' % i for i in range(30000))"),
        ("c", "''.join('static int f%d(int x) {\\n' % i for i in range(30000))"),
        ("dart", "''.join('int f%d(int x) {\\n' % i for i in range(30000))"),
        ("r", "''.join('f%d <- function(x) {\\n' % i for i in range(30000))"),
        ("rb", "''.join('def a%d(x)\\n' % i for i in range(30000))"),
        ("lua", "''.join('function a%d(x)\\n' % i for i in range(30000))"),
        ("ex", "''.join('def a%d(x) do\\n' % i for i in range(30000))"),
        ("hs", "'f :: Int\\n' * 30000"),
        ("java", "'/*' + ''.join('class A%d {\\n' % i for i in range(30000))"),
    ],
)
def test_extractors_stay_linear_on_unbalanced_input(lang, gen):
    r = _symbols_in_subprocess(lang, gen)
    assert r.returncode == 0, r.stderr
    assert int(r.stdout.split()[0]) >= 0


def test_unbalanced_declaration_ends_on_its_own_line():
    text = "".join(f"class A{i} {{\n" for i in range(20))
    syms = dl.symbols("java", text)
    assert [s.start for s in syms] == list(range(1, 21)) and all(s.end >= s.start for s in syms)
    rb = dl.symbols("rb", "def lone(x)\n  x\n")
    assert rb and rb[0].end == 1


def test_balanced_extraction_is_unchanged_and_fast():
    text = "".join(f"public class C{i} {{\n  void m() {{ }}\n}}\n" for i in range(3000))
    syms = dl.symbols("java", text)
    assert len(syms) >= 3000
    assert (syms[0].start, syms[0].end) == (1, 3) and (syms[1].start, syms[1].end) == (4, 6)
    rbt = "".join(f"def a{i}(x)\n  x\nend\n" for i in range(3000))
    rs = dl.symbols("rb", rbt)
    assert (rs[0].start, rs[0].end) == (1, 3) and (rs[-1].start, rs[-1].end) == (8998, 9000)


def test_cs_namespace_pattern_is_used_not_dead():
    fs = dc.scan([str(ROOT / "dead_code_checker" / "langs.py")], root=str(ROOT), jobs=1)
    assert not any("_CS_NS_BLOCK" in f.msg for f in fs)


# ---------------------------------------------------------------- fix round 6: --root decides what a relative path means
_ROOT_FIXTURE = {"lib/x.py": "def used():\n    return 1\n\n\ndef dead_fn():\n    return 2\n\n\nprint(used())\n"}


def test_cli_relative_path_with_root_resolves_against_root_not_cwd(tmp_path):
    root = mkrepo(tmp_path, _ROOT_FIXTURE, name="R")
    w = tmp_path / "W"
    w.mkdir()
    from_w = _cli_in(w, "scan", "lib", "--root", str(root))
    from_r = _cli_in(root, "scan", "lib", "--root", str(root))
    assert from_w.returncode == from_r.returncode == 0, from_w.stderr
    assert "dead_fn" in from_w.stdout and from_w.stdout == from_r.stdout


def test_cli_foreign_relative_path_with_root_resolves_against_root(tmp_path):
    root = mkrepo(tmp_path, _ROOT_FIXTURE, name="R")
    w = tmp_path / "W"
    w.mkdir()
    r = _cli_in(w, "scan", "lib", "--root", str(root), "--foreign")
    assert r.returncode == 0, r.stderr
    assert "dead_fn" in r.stdout


def test_scan_api_relative_path_with_root_ignores_cwd(tmp_path, monkeypatch):
    root = mkrepo(tmp_path, _ROOT_FIXTURE, name="R")
    w = tmp_path / "W"
    (w / "lib").mkdir(parents=True)  # a decoy: a cwd-relative reading would audit this empty tree
    monkeypatch.chdir(w)
    fs = dc.scan(["lib"], root=str(root), jobs=1, backend="git")
    assert any("dead_fn" in f.msg for f in fs)


@pytest.mark.parametrize("extra", [[], ["--foreign"]])
def test_cli_a_scan_path_that_does_not_exist_exits_2_not_a_silent_clean_audit(tmp_path, extra):
    root = mkrepo(tmp_path, _ROOT_FIXTURE, name="R")
    w = tmp_path / "W"
    w.mkdir()
    r = _cli_in(w, "scan", "nope", "--root", str(root), *extra)
    assert r.returncode == 2 and "does not exist" in r.stderr and not r.stdout.strip()
    r = _cli_in(w, "scan", "lib", *extra)  # no --root: relative to the cwd, W/lib is missing
    assert r.returncode == 2 and "does not exist" in r.stderr


def test_scan_api_missing_path_raises(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        dc.scan([str(tmp_path / "missing")], jobs=1)


# ---------------------------------------------------------------- registry files, config, Haskell tests
_RB_DEAD = "def deadfn(a)\n  a\nend\n"
_HOOK_JSON = '{"hooks":{"PostToolUse":[{"hooks":[{"type":"command","command":"ruby a.rb"}]}]}}'


def _plain_tree(tmp_path, name="plain"):
    t = tmp_path / name
    t.mkdir()
    (t / "a.rb").write_text(_RB_DEAD)
    return t


@pytest.mark.parametrize("fifo_name", ["catalog.json", "settings.json", "settings.local.json", "registry.json"])
@pytest.mark.parametrize("extra", [["--foreign"], ["--lang", "rb"]])
def test_registry_name_that_is_a_fifo_does_not_hang_a_plain_tree_scan(tmp_path, fifo_name, extra):
    t = _plain_tree(tmp_path)
    os.mkfifo(t / fifo_name)
    r = subprocess.run(
        [sys.executable, "-P", "-m", "dead_code_checker", "scan", str(t), "--jobs", "1", *extra],
        capture_output=True, text=True, timeout=20,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "deadfn" in r.stdout


def test_registry_name_that_is_a_fifo_in_the_git_index_does_not_hang_a_foreign_scan(tmp_path):
    root = mkrepo(tmp_path, {"a.rb": _RB_DEAD, "settings.json": "{}\n"})
    os.unlink(root / "settings.json")
    os.mkfifo(root / "settings.json")  # the index still lists it as a regular file
    r = _cli_scan(root, "--jobs", "1", timeout=20)
    assert r.returncode == 0, r.stderr
    assert "deadfn" in r.stdout


def test_scan_api_registry_fifo_does_not_hang(tmp_path):
    t = _plain_tree(tmp_path)
    os.mkfifo(t / "hooks.json")
    r = subprocess.run(
        [
            sys.executable, "-I", "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); from dead_code_checker import core as d; "
            "print(len(d.scan([sys.argv[2]], foreign=False, jobs=1, backend='walk')))",
            str(ROOT), str(t),
        ],
        capture_output=True, text=True, timeout=20,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr


def test_foreign_registry_symlink_to_a_file_outside_the_tree_is_not_read(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "x.json").write_text(_HOOK_JSON)  # would register a.rb
    t = _plain_tree(tmp_path, "T")
    os.symlink(out / "x.json", t / "settings.json")
    with_link = sorted((f.code, f.path) for f in audit(t, backend="walk"))
    assert dc.LAST_STATS["skipped"].get("symlink", 0) >= 1
    os.unlink(t / "settings.json")
    without = sorted((f.code, f.path) for f in audit(t, backend="walk"))
    assert with_link == without  # the outside content changed nothing
    assert any(code == UNREF for code, _p in with_link)


def test_foreign_registry_in_a_symlinked_dir_outside_the_tree_is_not_read(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "settings.json").write_text(_HOOK_JSON)
    t = _plain_tree(tmp_path, "T")
    os.symlink(out, t / "cfgdir")
    with_link = sorted((f.code, f.path) for f in audit(t, backend="git"))
    os.unlink(t / "cfgdir")
    assert with_link == sorted((f.code, f.path) for f in audit(t, backend="git"))


def test_foreign_registry_regular_file_inside_the_tree_is_still_read(tmp_path):
    t = _plain_tree(tmp_path, "T")
    (t / "settings.json").write_text(_HOOK_JSON)
    base = sorted((f.code, f.path) for f in audit(t, backend="walk"))
    (t / "settings.json").write_text("{}")
    assert base != sorted((f.code, f.path) for f in audit(t, backend="walk"))  # a registered file is not reported


@pytest.mark.parametrize("name", ["src/Latest.hs", "src/Contest.hs", "src/Protest.hs", "app/Main.hs"])
def test_haskell_names_that_merely_end_in_test_are_not_tests(name):
    assert not dc.is_test(name)


@pytest.mark.parametrize("name", ["src/FooSpec.hs", "src/FooTest.hs", "src/foo_test.hs", "src/foo_spec.hs"])
def test_haskell_test_naming_conventions_are_tests(name):
    assert dc.is_test(name)


_POLICY = '[deadcode]\ntests_count = true\n'


def test_use_repo_config_does_not_follow_a_symlinked_policy_outside_the_tree(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "policy.toml").write_text('[deadcode]\nignore = ["deadfn"]\n')  # would hide the finding
    t = _plain_tree(tmp_path, "T")
    os.symlink(out / "policy.toml", t / ".dead-code-checker.toml")
    fs = audit(t, backend="walk", use_repo_config=True)
    assert any("deadfn" in f.msg for f in fs)
    assert any(f.code == "deadcode.bad-config" for f in fs)  # the link is reported, not silently honoured


def test_use_repo_config_does_not_hang_on_a_fifo_policy(tmp_path):
    t = _plain_tree(tmp_path, "T")
    os.mkfifo(t / ".dead-code-checker.toml")
    r = _cli_scan(t, "--use-repo-config", "--jobs", "1", timeout=20)
    assert r.returncode == 0, r.stderr
    assert "deadfn" in r.stdout


def test_own_scan_does_not_hang_on_a_fifo_policy_either(tmp_path):
    t = _plain_tree(tmp_path, "T")
    os.mkfifo(t / ".dead-code-checker.toml")
    r = subprocess.run(
        [sys.executable, "-P", "-m", "dead_code_checker", "scan", str(t), "--jobs", "1"],
        capture_output=True, text=True, timeout=20,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr


def test_policy_symlinked_to_a_fifo_does_not_hang_an_own_scan(tmp_path):
    t = _plain_tree(tmp_path, "T")
    os.mkfifo(t / "pipe")
    os.symlink(t / "pipe", t / ".dead-code-checker.toml")  # os.path.isfile follows the link, so only read_regular saves the run
    r = subprocess.run(
        [sys.executable, "-P", "-m", "dead_code_checker", "scan", str(t), "--jobs", "1", "--lang", "rb"],
        capture_output=True, text=True, timeout=20,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------- fix round 6: ReDoS in annotation line and Dart signature
@pytest.mark.parametrize(
    "lang,gen",
    [
        # exponential before the fix: repeated empty arg lists then a tail that breaks the full match
        ("java", "'@a()' + ' @a()' * 40 + ' )x\\nclass X {\\n}\\n'"),
        ("kt", "'@a(' * 2000 + ' )x\\nclass X {\\n}\\n'"),
        ("java", "'@a(' + 'x' * 400000 + '\\nclass X {\\n}\\n'"),
        ("swift", "'@a ' * 100000 + ')x\\nclass X {\\n}\\n'"),
        # quadratic or worse before the fix: long whitespace run on an unindented Dart line
        ("dart", "'a' + ' ' * 40000 + 'x\\n'"),
        ("dart", "'a' + ' ' * 40000 + 'x ( {\\n'"),
        ("dart", "'a' + ' ' * 40000 + 'x ({\\nvoid used() {}\\n'"),
        ("dart", "'a<b' + ', c' * 100000 + ' x (\\n'"),
    ],
)
def test_extractors_stay_linear_on_long_hostile_lines(lang, gen):
    r = _symbols_in_subprocess(lang, gen, timeout=10)
    assert r.returncode == 0, r.stderr


def test_annotation_line_keeps_its_meaning():
    java = "@Deprecated\n@Foo(a = bar(1), b = \"x\") @Baz\npublic class Z {\n}\n"
    syms = dl.symbols("java", java)
    assert syms and syms[0].decorated
    assert not dl.symbols("java", "@Deprecated @Override\nclass Y {\n}\n")[0].decorated
    assert dl.symbols("java", "@Deprecated(since = \"1\")\nclass W {\n}\n")[0].decorated is False


def test_dart_functions_are_still_found():
    text = "int add(int a, int b) {\n  return a + b;\n}\nvoid run() {\n}\n"
    names = {s.name for s in dl.symbols("dart", text)}
    assert {"add", "run"} <= names


def test_cli_foreign_scan_of_hostile_annotation_and_dart_lines_finishes(tmp_path):
    root = mkrepo(
        tmp_path,
        {
            "annT/A.java": "@a()" + " @a()" * 30 + " )x\nclass Lonely {\n}\n",
            "annT/b.dart": "a" + " " * 40000 + "x\nvoid used() {}\n",
        },
        name="T",
    )
    r = _cli_scan(root, "--jobs", "1", timeout=20)
    assert r.returncode in (0, 1), r.stderr


# ---------------------------------------------------------------- fix round 7: comment stripping is linear
def _timed(code, timeout=10):
    return subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True, timeout=timeout)


def test_body_stub_with_many_unclosed_block_comment_markers_is_linear():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); from dead_code_checker import langs as L\n"
        "b = ' { val s = \"' + '/* ' * 400000 + '\" }'\n"
        "print(L.body_stub('kt', b))\n"
    )
    assert _timed(code).returncode == 0


def test_generic_body_stub_with_many_unclosed_block_comment_markers_is_linear():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); from dead_code_checker import core as D\n"
        "b = '{ var s = \"' + '/* ' * 400000 + '\" }'\n"
        "print(D.generic_body_stub('js', b))\n"
    )
    assert _timed(code).returncode == 0


def test_css_line_of_nested_comment_markers_is_linear():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); from dead_code_checker import langs as L\n"
        "print(len(L.symbols('css', '/*' * 400000)))\n"
    )
    assert _timed(code).returncode == 0


def test_hostile_comment_files_finish_in_a_foreign_cli_scan(tmp_path):
    root = mkrepo(
        tmp_path,
        {
            "h/a.kt": 'fun f() {\n val s = "' + "/* " * 100000 + '"\n}\n',
            "h/b.css": "/*" * 200000 + "\n.used { }\n",
            "h/c.js": "function g() { var s = '" + "/* " * 100000 + "'; }\n",
        },
        name="T",
    )
    r = _cli_scan(root, "--jobs", "1", timeout=20)
    assert r.returncode in (0, 1), r.stderr


def test_block_comment_stripping_keeps_its_meaning():
    assert dl.body_stub("kt", "/* a */") == "empty body"
    assert dl.body_stub("kt", "/* a */ /* b */") == "empty body"
    assert dl.body_stub("kt", "/* a */ return;") == "return;"
    assert dl.body_stub("kt", "/* open") is None
    assert dl.body_stub("kt", "x /*/ y */") is None
    assert dc.generic_body_stub("js", "/* a */ /* b */") == "empty body"
    assert dc.generic_body_stub("js", "/* open") is None
    css = dl.symbols("css", "/* a */ .x { }\n/* b\n.y { }\n*/\n.z { }\n.w { } /* c */ .v { }\n")
    got = {s.name for s in css}
    assert {"x", "z", "w"} <= got and "y" not in got


# ---------------------------------------------------------------- fix round 8: generated-header check, C static scan, hostile .git
def test_generated_header_check_is_linear_on_hostile_first_line():
    code = (
        "import sys, time; sys.path.insert(0, sys.argv[1]); from dead_code_checker import langs as L\n"
        "s = '// ' + 'Code generated ' * 70000 + '\\nclass A {}\\n'\n"
        "t = time.time()\n"
        "r = (L.is_generated(s, True), L.is_generated(s, False))\n"
        "print(r, time.time() - t)\n"
    )
    r = _timed(code, timeout=20)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("(False, False)")
    assert float(r.stdout.rsplit(" ", 1)[1]) < 1.0


def test_generated_header_markers_keep_their_meaning():
    assert dl.is_generated("// Code generated by protoc. DO NOT EDIT.\npackage x\n", False)
    assert dl.is_generated("/* @generated */\n", False)
    assert dl.is_generated("// <auto-generated/>\n", False)
    assert not dl.is_generated("// DO NOT EDIT this hand-written table\n", False)
    assert dl.is_generated("// DO NOT EDIT\n", True)
    assert not dl.is_generated("a\nb\nc\nd\ne\n// @generated\n", True)
    assert dl.is_generated("a\nb\nc\nd\n// @generated\n", True)


def test_generated_hostile_file_finishes_in_a_foreign_cli_scan(tmp_path):
    root = mkrepo(tmp_path, {"h/a.java": "// " + "Code generated " * 20000 + "\nclass A {}\n"}, name="T")
    t0 = time.time()
    r = _cli_scan(root, "--jobs", "1", timeout=20)
    assert r.returncode in (0, 1), r.stderr
    assert time.time() - t0 < 10


def test_c_static_scan_without_brace_or_semicolon_is_linear():
    code = (
        "import sys, time; sys.path.insert(0, sys.argv[1]); from dead_code_checker import langs as L\n"
        "t = time.time(); L.symbols('c', 'static a(\\n' * 200000)\n"
        "print(time.time() - t)\n"
    )
    r = _timed(code, timeout=60)  # 2 MB: the quadratic scan took ~17 s here, the bounded one well under 1 s
    assert r.returncode == 0, r.stderr
    assert float(r.stdout) < 5.0


def test_c_static_scan_keeps_its_meaning():
    got = {s.name for s in dl.symbols("c", "static int helper_q(int a)\n{\n  return a;\n}\nstatic int x = 3;\n")}
    assert "helper_q" in got


def _git_fifo_include_tree(tmp_path):
    root = mkrepo(tmp_path, {"a.rb": "def fn0007(a)\n  a\nend\n"}, name="T")
    with open(root / ".git" / "config", "a") as fh:
        fh.write("[include]\n\tpath = inc\n")
    os.mkfifo(root / ".git" / "inc")
    return root


def test_hostile_git_include_fifo_does_not_stall_a_foreign_scan(tmp_path):
    root = _git_fifo_include_tree(tmp_path)
    t0 = time.time()
    r = _cli_scan(root, "--jobs", "1", timeout=40)
    assert r.returncode in (0, 1), r.stderr
    assert time.time() - t0 < 20
    assert "fn0007" in r.stdout + r.stderr


def test_hostile_git_fifo_head_does_not_stall_a_foreign_scan(tmp_path):
    root = mkrepo(tmp_path, {"a.rb": "def fn0007(a)\n  a\nend\n"}, name="T")
    (root / ".git" / "HEAD").unlink()
    os.mkfifo(root / ".git" / "HEAD")
    t0 = time.time()
    r = _cli_scan(root, "--jobs", "1", timeout=40)
    assert r.returncode in (0, 1), r.stderr
    assert time.time() - t0 < 20


def test_git_include_is_still_honoured_for_our_own_trees(tmp_path):
    root = mkrepo(tmp_path, {"a.rb": "x\n"}, name="T")
    (root / ".git" / "inc").write_text("[core]\n\tignorecase = false\n")
    with open(root / ".git" / "config", "a") as fh:
        fh.write("[include]\n\tpath = inc\n")
    assert dc._git(str(root), "ls-files")[0] == 0


# ---------------------------------------------------------------- round 8: hostile .git nodes and linear regexes
def _scan_fast(root, limit=20):
    t0 = time.time()
    r = _cli_scan(root, "--jobs", "1", timeout=60)
    assert r.returncode in (0, 1), r.stderr
    assert time.time() - t0 < limit
    assert "fn0007" in r.stdout + r.stderr


def _plain_git_tree(tmp_path):
    return mkrepo(tmp_path, {"a.rb": "def fn0007(a)\n  a\nend\n"}, name="T")


def test_hostile_gitignore_fifo_does_not_stall_a_foreign_scan(tmp_path):
    root = _plain_git_tree(tmp_path)
    os.mkfifo(root / ".gitignore")
    _scan_fast(root)


def test_hostile_nested_gitignore_fifo_does_not_stall_a_foreign_scan(tmp_path):
    root = mkrepo(tmp_path, {"a.rb": "def fn0007(a)\n  a\nend\n", "sub/b.rb": "x\n"}, name="T")
    os.mkfifo(root / "sub" / ".gitignore")
    _scan_fast(root)


def test_hostile_gitattributes_fifo_does_not_stall_a_foreign_scan(tmp_path):
    root = _plain_git_tree(tmp_path)
    os.mkfifo(root / ".gitattributes")
    _scan_fast(root)


def test_hostile_info_exclude_fifo_does_not_stall_a_foreign_scan(tmp_path):
    root = _plain_git_tree(tmp_path)
    (root / ".git" / "info").mkdir(exist_ok=True)
    if (root / ".git" / "info" / "exclude").exists():
        (root / ".git" / "info" / "exclude").unlink()
    os.mkfifo(root / ".git" / "info" / "exclude")
    _scan_fast(root)


def test_hostile_objects_alternates_fifo_does_not_stall_a_foreign_scan(tmp_path):
    root = _plain_git_tree(tmp_path)
    (root / ".git" / "objects" / "info").mkdir(parents=True, exist_ok=True)
    os.mkfifo(root / ".git" / "objects" / "info" / "alternates")
    _scan_fast(root)


def test_hostile_commondir_config_include_fifo_does_not_stall_a_foreign_scan(tmp_path):
    root = _plain_git_tree(tmp_path)
    common = root / ".g"
    os.rename(root / ".git", common)
    wt = common / "worktrees" / "w"
    wt.mkdir(parents=True)
    (wt / "HEAD").write_text((common / "HEAD").read_text())
    (wt / "index").write_bytes((common / "index").read_bytes())
    (wt / "commondir").write_text("../..\n")
    (root / ".git").write_text("gitdir: .g/worktrees/w\n")
    with open(common / "config", "a") as fh:
        fh.write("[include]\n\tpath = inc\n")
    os.mkfifo(common / "inc")
    _scan_fast(root)


def test_hostile_config_worktree_include_fifo_does_not_stall_a_foreign_scan(tmp_path):
    root = _plain_git_tree(tmp_path)
    (root / ".git" / "config.worktree").write_text("[include]\n\tpath = inc\n")
    os.mkfifo(root / ".git" / "inc")
    _scan_fast(root)


def test_foreign_scan_does_not_run_git_at_all(tmp_path, monkeypatch):
    root = _plain_git_tree(tmp_path)
    with dc.foreign_git(True):
        assert dc.git_blocked(str(root)) is True
        assert dc._git(str(root), "ls-files")[0] == 127
    assert dc.git_blocked(str(root)) is False


def _record_git_calls(monkeypatch):
    """Every subprocess.run whose argv starts with git, recorded (and answered as a failed git)."""
    calls = []
    real = dc.subprocess.run

    def spy(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and os.path.basename(str(cmd[0])) == "git":
            calls.append(list(cmd))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(dc.subprocess, "run", spy)
    return calls


def _no_git_tree(tmp_path):
    root = tmp_path / "T"
    root.mkdir()
    (root / "a.rb").write_text("def fn0007(a)\n  a\nend\n")
    return root


def test_foreign_scan_api_runs_no_git_subprocess_on_a_tree_without_dot_git(tmp_path, monkeypatch):
    root = _no_git_tree(tmp_path)
    calls = _record_git_calls(monkeypatch)
    dc.scan([str(root)], foreign=True, jobs=1)
    assert calls == []


def test_foreign_cli_runs_no_git_subprocess_on_a_tree_without_dot_git(tmp_path, monkeypatch):
    root = _no_git_tree(tmp_path)
    report = tmp_path / "out" / "R.md"
    report.parent.mkdir()
    calls = _record_git_calls(monkeypatch)
    assert dc.main(["scan", str(root), "--foreign", "--report", str(report)]) == 0
    assert calls == []
    assert report.exists()


def test_foreign_helpers_do_not_fall_back_to_git(tmp_path, monkeypatch):
    root = _no_git_tree(tmp_path)
    calls = _record_git_calls(monkeypatch)
    assert dc.toplevel(str(root), foreign=True) is None
    dc.scan_base([str(root)], foreign=True)
    dc.tree_roots([str(root)], foreign=True)
    assert calls == []


def test_foreign_scan_of_a_bare_layout_tree_with_fifo_include_does_not_stall(tmp_path):
    root = tmp_path / "bare" / "T"
    (root / "objects").mkdir(parents=True)
    (root / "refs" / "heads").mkdir(parents=True)
    (root / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "config").write_text("[core]\n\tbare = true\n[include]\n\tpath = inc\n")
    os.mkfifo(root / "inc")
    (root / "a.rb").write_text("def f1(a)\n  a\nend\n")
    report = tmp_path / "bare" / "out" / "R.md"
    report.parent.mkdir()
    t0 = time.time()
    assert dc.main(["scan", str(root), "--foreign", "--report", str(report)]) == 0
    assert time.time() - t0 < 5.0


def _elapsed(fn, *a):
    t0 = time.time()
    fn(*a)
    return time.time() - t0


HOSTILE_STUB = {
    "java": "throw new IllegalStateException(\"" + "todo" * 8000 + "\") x",
    "php": "throw new Exception('" + "todo" * 8000 + "') x",
    "swift": "fatalError(\"" + "todo" * 8000 + "\") x",
}


@pytest.mark.parametrize("lang", sorted(HOSTILE_STUB))
def test_extra_lang_stub_check_is_linear_on_a_hostile_body(lang):
    assert _elapsed(dl.body_stub, lang, HOSTILE_STUB[lang]) < 1.0


@pytest.mark.parametrize(
    "lang,body",
    [
        ("js", 'throw new Error("' + "todo" * 8000 + '") x'),
        ("go", 'panic("' + "todo" * 8000 + '") x'),
        ("sh", " \n" * 32000 + "x"),
        ("sh", "echo" + " " * 32000 + "$"),
    ],
)
def test_core_stub_check_is_linear_on_a_hostile_body(lang, body):
    assert _elapsed(dc.generic_body_stub, lang, body) < 1.0


def test_short_stub_verdicts_are_kept():
    assert dl.body_stub("php", "throw new Exception('todo')") is not None
    assert dl.body_stub("java", "return null;") is not None
    assert dc.generic_body_stub("js", 'throw new Error("not implemented")') is not None
    assert dc.generic_body_stub("go", 'panic("todo")') is not None
    assert dc.generic_body_stub("sh", "# c\n:\n") is not None


def test_yaml_key_regex_is_linear_on_a_whitespace_line():
    text = "alpha_key: 1\n" + " " * 32000 + "x\n"
    assert _elapsed(dc._keys_of, ".yaml", text, None) < 1.0
    assert dc._keys_of(".yaml", "- name_x: 1\n  other_y: 2\n", None) == {"name_x": 1, "other_y": 2}


@pytest.mark.parametrize("cmd", ["echo", "printf"])
def test_sh_hook_stub_is_linear_on_a_whitespace_line(cmd):
    assert _elapsed(dc.sh_hook_stub_reason, cmd + " " * 32000 + "$") < 1.0
    assert dc.sh_hook_stub_reason("echo hello\nexit 0\n") is not None


def test_foreign_scan_with_hostile_yaml_and_stub_bodies_finishes_fast(tmp_path):
    body = 'function deadfn() {\n  throw new Error("' + "todo" * 8000 + '") + x;\n}\n'
    root = mkrepo(
        tmp_path,
        {"a.rb": "def fn0007(a)\n  a\nend\n", "a.js": body, "settings.yaml": "alpha_key: 1\n" + " " * 32000 + "x\n"},
        name="T",
    )
    _scan_fast(root, limit=8)


def test_read_head_resolves_loose_and_packed_refs_and_ignores_a_fifo(tmp_path):
    root = _plain_git_tree(tmp_path)
    want = sh(root, "rev-parse", "HEAD").strip()
    assert dc.read_head(str(root)) == want
    sh(root, "pack-refs", "--all")
    assert dc.read_head(str(root)) == want
    (root / ".git" / "HEAD").unlink()
    os.mkfifo(root / ".git" / "HEAD")
    assert dc.read_head(str(root)) is None


def test_read_head_rejects_a_ref_that_climbs_out(tmp_path):
    root = _plain_git_tree(tmp_path)
    (root / ".git" / "HEAD").write_text("ref: refs/../../x\n")
    assert dc.read_head(str(root)) is None


def test_foreign_skip_counts_a_symlink_once_across_both_walkers(tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("x\n")
    root = mkrepo(tmp_path)
    os.symlink(secret, root / "leak.py")
    write(root, "ok.py", "def visible_fn(a):\n    return a + 1\n")
    audit(root, ["py"], backend="walk")
    assert dc.LAST_STATS["skipped"].get("symlink") == 1


def test_core_symbol_regexes_are_linear_on_hostile_whitespace():
    # A foreign tree is hostile data: `\s*\*?\s*` in the JS function pattern split a run of blanks in many ways.
    code = (
        "import sys, time; sys.path.insert(0, sys.argv[1]); from dead_code_checker import core as D\n"
        "rx = D._SYM_RES['js'][0][1]\n"
        "t = time.time(); rx.match('function' + ' ' * 40000 + 'x'); single = time.time() - t\n"
        "worst = 0.0\n"
        "for lang, kw in (('js', 'export default async function'), ('js', 'const f = async'), ('go', 'func'),\n"
        "                 ('rs', 'pub fn'), ('sh', 'function')):\n"
        "    t = time.time(); D.generic_symbols(lang, kw + ' ' * 40000 + 'x\\n'); worst = max(worst, time.time() - t)\n"
        "print(single, worst)\n"
    )
    r = _timed(code, timeout=120)
    assert r.returncode == 0, r.stderr
    single, worst = map(float, r.stdout.split())
    assert single < 1.0 and worst < 1.0, r.stdout


def test_js_function_pattern_keeps_its_meaning():
    sys.path.insert(0, str(ROOT))
    from dead_code_checker import core as D

    rx = D._SYM_RES["js"][0][1]
    for line, name in (
        ("function foo(a) {", "foo"),
        ("function* gen() {", "gen"),
        ("function *gen() {", "gen"),
        ("function*gen() {", "gen"),
        ("export default async function bar() {", "bar"),
        ("async function  baz ( ) {", "baz"),
    ):
        m = rx.match(line)
        assert m and m.group(1) == name, line
    assert rx.match("functional(x)") is None
