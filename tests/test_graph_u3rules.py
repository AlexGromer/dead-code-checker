"""Nine patterns that made a live symbol look dead, as rules: one regression per pattern, on SYNTHETIC fixtures, each with the control that
must stay as it was (a library with no top-level statements is still dead, a member nobody names is still dead). The graph is built the
way the levels build it (levels.build on a throwaway git repository, reach.analyze); the Go link rules also run on hand-written Facts,
which need no grammar. A test that needs a tree-sitter grammar or pwsh is skipped with the reason when the host has none.

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_u3rules.py

  1 script run by an interpreter   py / sh / ps1 statements at the top level make the file a soft entry: what they call is used
  2 generated library blocks       `// BEGIN GENERATED` .. `// END GENERATED` (data/generated.json): the members are flagged generated, nobody reports them
  3 C typedef struct tag {} tag_t  the typedef name is a def that uses the tag
  4 C++ factory / registration     instance(), getRoutes(), execute() are soft roots (data: queries/cpp/rules.json)
  5 modules loaded by name         importlib / spec_from_file_location / a wrapper of those, a prefix discovery loop: dyn site module:<glob>
  6 typing.Protocol                the class is protocol like its members
  7 function names as data         `export -f f`, `bash -c 'f ..'`, `run_cmd f`, `pool.map(one, xs)`
  8 Go build-constraint pairs      one symbol in two files of a directory: each keeps the other alive
  9 Go type and method, one name   a bare `renderPage` in a method is the type
"""

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test

ROOT = Path(__file__).resolve().parent.parent
from dead_code_checker.graph import contracts, levels, link as dlink, model as md, reach, roots, ts as tsm  # noqa: E402
from dead_code_checker.graph.extract import pwsh as dpwsh  # noqa: E402

DEAD = ("dead", "dead-cluster")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """A private data dir; the root extractors registered again (another test file resets the registry in a shared process)."""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("DEAD_CODE_CHECKER_HOME", str(d))
    if "settings" not in {r.name for r in contracts.root_extractors()}:
        import importlib

        importlib.reload(roots)


def need_grammar(*grammars):
    for g in grammars:
        try:
            tsm.get_parser(g)
        except LookupError as exc:
            pytest.skip(f"tree-sitter grammar {g} is not usable on this host: {exc}")


def git(cwd, *args):
    cmd = ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
    subprocess.run([*cmd, "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


def repo_of(tmp_path, files):
    """A committed throwaway repository; a value is the text or (text, mode)."""
    r = tmp_path / "repo"
    r.mkdir(parents=True)
    git(r, "init", "-q")
    for rel, v in files.items():
        text, mode = v if isinstance(v, tuple) else (v, 0o644)
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        p.chmod(mode)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "x")
    return r


class G:
    """The graph of a repository: verdict(path#qual), reportable(path#qual), the roots."""

    def __init__(self, repo):
        self.g = levels.build(str(repo), None, workers=1).graph
        self.res = reach.analyze(self.g, "closed_world")
        self.by = {n.key.split(":", 1)[1]: n for n in self.g.nodes}

    def verdict(self, k):
        return self.res.verdict(self.by[k].key)

    def reportable(self, k):
        return levels.reportable(self.by[k], self.res)

    def dead(self, k):
        return self.verdict(k) in DEAD

    def roots(self, kind):
        return sorted(r.node.split(":", 1)[1] for r in self.g.roots if r.kind == kind and r.node)


MAIN = (
    "#!/usr/bin/env python3\ndef main():\n    return 1\n\n\nif __name__ == '__main__':\n    main()\n",
    0o755,
)  # a production root


# ====================================================================== 1 a script run by an interpreter
SCRIPT = "import sys\n\n\ndef load(p):\n    return open(p).read()\n\n\ndef show(x):\n    print(x)\n\n\nshow(load(sys.argv[1]))\n"
LIB = "def load(p):\n    return open(p).read()\n\n\ndef show(x):\n    print(x)\n"


def test_python_script_statements_keep_the_functions_they_call(tmp_path):
    g = G(repo_of(tmp_path, {"main.py": MAIN, "tools/report/summary.py": SCRIPT, "tools/report/helpers.py": LIB}))
    assert [g.verdict(f"tools/report/summary.py#{q}") for q in ("load", "show")] == ["weak-alive"] * 2
    assert "tools/report/summary.py" in g.roots("main")
    assert g.dead("tools/report/helpers.py#load") and g.dead(
        "tools/report/helpers.py#show"
    )  # no statement at the top level: a library


def test_the_statements_of_a_test_file_make_no_script(tmp_path):
    test = "import sys\n\nsys.path.insert(0, 'lib')\nfrom helpers import used\n\n\ndef test_it():\n    assert used()\n"
    helpers = "def used():\n    return 1\n"
    g = G(repo_of(tmp_path, {"main.py": MAIN, "tests/test_it.py": test, "helpers.py": helpers}))
    assert g.verdict("helpers.py#used") == "test-only" and "tests/test_it.py" not in g.roots("main")


def test_python_a_library_with_only_assignments_and_the_main_guard_is_not_a_soft_script(tmp_path):
    lib = "import logging\n\nlog = logging.getLogger(__name__)\nLIMIT = int('3')\n\n\ndef used_nowhere():\n    return LIMIT\n"
    g = G(repo_of(tmp_path, {"main.py": MAIN, "pkg/mod.py": lib}))
    assert g.dead("pkg/mod.py#used_nowhere") and "pkg/mod.py" not in g.roots("main")
    guarded = (
        "def run():\n    return 1\n\n\nif __name__ == '__main__':\n    run()\n"  # the guard is the hard root of its own
    )
    h = G(repo_of(tmp_path / "b", {"main.py": MAIN, "tool.py": guarded}))
    assert h.verdict("tool.py#run") == "alive"


def test_the_statements_of_a_package_init_make_no_script(tmp_path):
    init = "import warnings\n\nwarnings.filterwarnings('ignore')\n\n\ndef _quiet():\n    return 1\n"
    g = G(repo_of(tmp_path, {"main.py": MAIN, "pkg/__init__.py": init, "pkg/tool.py": SCRIPT}))
    assert "pkg/__init__.py" not in g.roots("main") and "pkg/tool.py" in g.roots(
        "main"
    )  # an import runs an __init__, nobody runs it as a file
    assert g.dead("pkg/__init__.py#_quiet")


def test_only_script_statements_of_a_test_file_or_an_init_are_dropped_other_soft_main_hints_stay():
    def verdict(path, target):
        hint = md.RootHint("main", target, 3, "soft", "convention")
        facts = go_facts([("helper", "func", 3, 5, "package", 0, -1, "")], [("helper", "call", 4, "")], [hint])
        return analyse({path: facts})[f"{path}#helper"]

    assert verdict("p/a.go", "") not in DEAD  # a statement at the top level of a source file: the module is a script
    assert (
        verdict("p/a_test.go", "") in DEAD and verdict("p/__init__.go", "") in DEAD
    )  # the test framework or an import runs those
    assert verdict("p/a_test.go", "helper") not in DEAD  # a soft main that names a def is no script statement: kept


SHELL = '#!/bin/bash\nwarn() { echo "w: $1"; }\nunused() { :; }\nset -euo pipefail\nwarn hi\n'
SHLIB = '#!/bin/bash\nset -e\nwarn() { echo "w: $1"; }\n'


def test_shell_top_level_commands_make_the_file_a_script_even_without_the_exec_bit(tmp_path):
    need_grammar("bash")
    g = G(repo_of(tmp_path, {"main.py": MAIN, "setup.sh": SHELL, "lib.sh": SHLIB}))
    assert g.verdict("setup.sh#warn") == "weak-alive" and "setup.sh" in g.roots("main")
    assert g.dead("setup.sh#unused")  # nothing calls it: a script keeps what its statements call, no more
    assert g.dead("lib.sh#warn") and "lib.sh" not in g.roots("main")  # `set -e` and definitions only: a library


POWERSHELL = "param([string]$Name)\nSet-StrictMode -Version Latest\nfunction Show-Problem { param($m) Write-Host $m }\nfunction Never { 'x' }\nShow-Problem 'hi'\n"
PSLIB = "Set-StrictMode -Version Latest\nfunction Show-Problem { param($m) Write-Host $m }\n"


def test_powershell_top_level_statement_makes_the_file_a_script():
    if dpwsh._find() is None:
        pytest.skip("no usable pwsh engine on this host")
    ex = contracts.get_extractor("powershell")
    script = ex.extract("powershell", "install.ps1", POWERSHELL)
    lib = ex.extract("powershell", "lib.ps1", PSLIB)
    assert [(h.kind, h.target, h.strength) for h in script.roots] == [("main", "", "soft")]
    assert not lib.roots  # strict mode is what a library sets too


# ====================================================================== 2 generated library blocks
LIBBLOCK = (
    "// BEGIN GENERATED\nfunction sumLen(s) {\n  return s.length;\n}\nfunction tidyPath(p) {\n  return sumLen(p);\n}\n// END GENERATED\n"
)
TEMPLATE = LIBBLOCK + "function unused() {\n  return 2;\n}\nfunction main() {\n  return 1;\n}\nmain();\n"


def test_members_of_a_generated_library_block_are_flagged_and_never_reported(tmp_path):
    need_grammar("javascript")
    g = G(repo_of(tmp_path, {"main.py": MAIN, "flows/report.js": TEMPLATE}))
    for q in ("sumLen", "tidyPath"):
        assert g.by[f"flows/report.js#{q}"].flags & md.F_GENERATED and not g.reportable(f"flows/report.js#{q}")
    assert (
        not g.by["flows/report.js#unused"].flags & md.F_GENERATED
    )  # outside the block: a dead function like any other
    assert g.dead("flows/report.js#unused") and g.reportable("flows/report.js#unused")


def test_generated_block_markers_are_data_and_unterminated_blocks_flag_nothing(tmp_path):
    from dead_code_checker.graph.extract import common

    defs = [md.Def("a", "func", 2, 3, "public", 0, -1, ""), md.Def("b", "func", 6, 7, "public", 0, -1, "")]
    text = "x\n// <generated>\nfoo\n// </generated>\nz\nbar\n"
    assert [bool(d.flags & md.F_GENERATED) for d in common.mark_generated(text, defs)] == [True, False]
    assert [d.flags for d in common.mark_generated("// BEGIN GENERATED\nopen forever\n", defs)] == [0, 0]
    assert common.generated_sha()


# ====================================================================== 3 C typedef struct tag { } tag_t
CHDR = "typedef struct pool_worker {\n    int fd;\n} pool_worker_t;\n\nint worker_run(pool_worker_t *w);\n"
CSRC = '#include "core.h"\nint worker_run(pool_worker_t *w) { return w->fd; }\nint main(void) { pool_worker_t w = {0}; return worker_run(&w); }\n'


def test_c_struct_tag_is_alive_when_its_typedef_name_is_used(tmp_path):
    need_grammar("c")
    g = G(repo_of(tmp_path, {"mod/core.h": CHDR, "mod/core.c": CSRC}))
    assert g.verdict("mod/core.h#pool_worker") in ("weak-alive", "alive")
    assert g.by["mod/core.h#pool_worker_t"].flags & md.F_PROTOCOL and not g.reportable("mod/core.h#pool_worker_t")


def test_c_struct_tag_with_an_unused_typedef_is_still_dead(tmp_path):
    need_grammar("c")
    hdr = CHDR + "typedef struct orphan_tag { int x; } orphan_t;\n"
    g = G(repo_of(tmp_path, {"mod/core.h": hdr, "mod/core.c": CSRC}))
    assert g.dead("mod/core.h#orphan_tag") and g.reportable("mod/core.h#orphan_tag")


# ====================================================================== 4 C++ factory / registration
CPPHDR = "class ItemController : public Base {\npublic:\n  static ItemController* instance();\n  void decodeItem();\n  void decodeHelper();\n};\n"
CPPSRC = (
    '#include "ItemController.h"\nItemController* ItemController::instance() { static ItemController c; return &c; }\n'
    "void ItemController::decodeItem() { decodeHelper(); }\nvoid ItemController::decodeHelper() {}\nint main() { return 0; }\n"
)
CPPHEADERLESS = (
    "void ItemController::execute() { decodeItem(); }\n"
    "void ItemController::decodeItem() { decodeHelper(); }\nvoid ItemController::decodeHelper() {}\n"
    "void ItemController::neverCalled() {}\n"
    "std::vector<int>* ItemController::getRoutes() { return nullptr; }\n"
)


def test_cpp_class_built_by_a_static_factory_is_reached_from_outside(tmp_path):
    need_grammar("cpp")
    g = G(repo_of(tmp_path, {"src/ItemController.h": CPPHDR, "src/ItemController.cpp": CPPSRC}))
    assert g.verdict("src/ItemController.cpp#ItemController.instance") == "weak-alive"
    assert (
        g.verdict("src/ItemController.cpp#ItemController.decodeItem") == "weak-alive"
    )  # the class has an unseen base: its members are used
    assert g.roots("annotation:cpp-hook")


def test_cpp_registration_and_entry_members_of_a_class_with_no_header_are_roots_the_rest_is_not(tmp_path):
    need_grammar("cpp")
    g = G(repo_of(tmp_path, {"main.py": MAIN, "src/ItemController.cpp": CPPHEADERLESS}))
    p = "src/ItemController.cpp#ItemController."
    assert [g.verdict(p + m) for m in ("execute", "getRoutes", "decodeItem", "decodeHelper")] == ["weak-alive"] * 4
    assert g.dead(
        p + "neverCalled"
    )  # a member that the entries do not reach stays dead: the rule roots the hooks, not the class


# ====================================================================== 5 modules loaded by name
POLICY = (
    "import importlib.util\nimport os\n\nHERE = os.path.dirname(__file__)\n\n\ndef _module(name):\n"
    "    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + '.py'))\n    return spec\n\n\n"
    "def run():\n    return _module('plugin_one')\n\n\nif __name__ == '__main__':\n    run()\n"
)
PLUGIN = "def _fold(x):\n    return x\n\n\ndef entry():\n    return _fold(1)\n"
DISCOVER = (
    "import importlib\nimport pkgutil\n\n\ndef load():\n    for info in pkgutil.iter_modules(['plugins']):\n"
    "        if info.name.startswith('probe_'):\n            importlib.import_module(f'plugins.{info.name}')\n\n\n"
    "if __name__ == '__main__':\n    load()\n"
)


def test_a_module_named_by_a_loader_wrapper_is_used_through_its_module_object(tmp_path):
    g = G(repo_of(tmp_path, {"app/host.py": POLICY, "app/plugin_one.py": PLUGIN, "app/stranger.py": PLUGIN}))
    assert [g.verdict(f"app/plugin_one.py#{q}") for q in ("_fold", "entry")] == ["weak-alive"] * 2
    assert g.dead("app/stranger.py#entry")  # a module nobody names is dead as before


def test_a_discovery_loop_with_a_name_prefix_uses_the_matching_modules_only(tmp_path):
    g = G(repo_of(tmp_path, {"disco.py": DISCOVER, "plugins/probe_alpha.py": PLUGIN, "plugins/helpers.py": PLUGIN}))
    assert g.verdict("plugins/probe_alpha.py#entry") == "weak-alive"
    assert g.dead("plugins/helpers.py#entry")


def test_loader_sites_are_dyn_facts_of_the_extractor():
    ex = contracts.get_extractor("python")
    facts = ex.extract("python", "h.py", POLICY)
    assert [d.pattern for d in facts.dyn] == ["module:plugin_one"]
    sites = ex.extract("python", "d.py", DISCOVER).dyn
    assert sorted(d.pattern for d in sites) == ["import_module(*)", "module:plugins.probe_*"]
    guard = (
        "import importlib.util, os\n\n\ndef _load(name, alias=None):\n    alias = alias or name\n"
        "    return importlib.util.spec_from_file_location(alias, os.path.join('.', name + '.py'))\n\n\n"
        "se = _load('mod_alpha')\nsp = _load('mod_beta', 'mod_beta_alias')\nx = _load('/some/path')\n"
    )  # the module is named by the path of the loader as well as by its first argument; a path is no module name
    assert [d.pattern for d in ex.extract("python", "g.py", guard).dyn] == [
        "module:mod_alpha",
        "module:mod_beta",
        "module:mod_beta_alias",
    ]
    plain = ex.extract(
        "python", "p.py", "import importlib\n\n\ndef f(name):\n    return importlib.import_module(name)\n"
    )
    assert [d.pattern for d in plain.dyn] == ["import_module(*)"]  # a name nobody knows: only the old site


# ====================================================================== 6 structural contracts that no code names
PROTO = "from typing import Protocol\n\n\nclass Backend(Protocol):\n    def fetch(self, x): ...\n\n\nclass Plain:\n    def run(self):\n        return 1\n"


def test_protocol_classes_and_their_members_are_never_reported(tmp_path):
    g = G(repo_of(tmp_path, {"main.py": MAIN, "api/contracts.py": PROTO}))
    for q in ("Backend", "Backend.fetch"):
        assert g.by[f"api/contracts.py#{q}"].flags & md.F_PROTOCOL and not g.reportable(f"api/contracts.py#{q}")
    assert g.dead("api/contracts.py#Plain") and g.reportable("api/contracts.py#Plain")


def test_generic_and_qualified_protocol_bases_are_protocols_too(tmp_path):
    src = (
        "import typing\nfrom typing import Generic, Protocol, TypeVar\n\nT = TypeVar('T')\n\n\n"
        "class Gen(Protocol[T]):\n    def get(self) -> T: ...\n\n\n"
        "class Qual(typing.Protocol):\n    def put(self, x): ...\n\n\n"
        "class Mixed(Protocol, Generic[T]):\n    def drop(self): ...\n\n\n"
        "class Sub(typing.Protocol[T]):\n    def hold(self): ...\n\n\n"
        "class NotOne(Generic[T]):\n    def run(self):\n        return 1\n"
    )
    g = G(repo_of(tmp_path, {"main.py": MAIN, "api/shapes.py": src}))
    for q in ("Gen", "Gen.get", "Qual", "Qual.put", "Mixed", "Mixed.drop", "Sub", "Sub.hold"):
        assert g.by[f"api/shapes.py#{q}"].flags & md.F_PROTOCOL and not g.reportable(f"api/shapes.py#{q}"), q
    assert g.dead("api/shapes.py#NotOne") and g.reportable("api/shapes.py#NotOne")  # Generic alone is no protocol


# ====================================================================== 7 function names handed over as data
SHDATA = (
    '#!/bin/bash\ncheck_item() { echo "$1"; }\nrun_two() { :; }\nrun_three() { :; }\nlater() { :; }\nnever() { :; }\n'
    "export -f check_item\nrows | xargs -0 -I{} bash -c 'check_item \"$1\"' _ {}\npoll_until 5 run_three\n"
    "sh -c 'later; true'\nrun_cmd run_two\n"
)
PYPOOL = (
    "from concurrent.futures import ThreadPoolExecutor\n\n\ndef apply(todo):\n    def one(x):\n        return x\n\n"
    "    def other(x):\n        return x\n\n    with ThreadPoolExecutor() as pool:\n        list(pool.map(one, todo))\n"
    "        pool.submit(other, 1)\n\n\nif __name__ == '__main__':\n    apply([1])\n"
)


def test_shell_function_names_in_export_f_bash_c_and_arguments_are_references(tmp_path):
    need_grammar("bash")
    g = G(repo_of(tmp_path, {"fetcher.sh": (SHDATA, 0o755)}))
    assert [g.dead(f"fetcher.sh#{q}") for q in ("check_item", "run_two", "run_three", "later")] == [False] * 4
    assert g.dead("fetcher.sh#never")


def test_shell_bash_c_string_is_script_text_only_after_dash_c(tmp_path):
    need_grammar("bash")
    src = "#!/bin/bash\nfoo() { :; }\nbar() { :; }\nrun 'foo bar'\nsh -c 'bar'\n"
    g = G(repo_of(tmp_path, {"x.sh": (src, 0o755)}))
    assert not g.dead("x.sh#bar") and g.dead("x.sh#foo")  # 'foo bar' follows no -c: an argument, not a script


def test_shell_dash_c_of_another_command_is_no_script_text(tmp_path):
    need_grammar("bash")
    src = "#!/bin/bash\nfoo() { :; }\nbar() { :; }\nbaz() { :; }\nn=$(grep -c \"foo\" log)\nsudo -u x bash -lc 'bar'\nxargs -I{} /bin/sh -c 'baz'\n"
    g = G(repo_of(tmp_path, {"x.sh": (src, 0o755)}))
    assert (
        g.dead("x.sh#foo") and not g.dead("x.sh#bar") and not g.dead("x.sh#baz")
    )  # grep -c counts: its string is a pattern


def test_python_nested_functions_handed_to_a_pool_are_used(tmp_path):
    g = G(repo_of(tmp_path, {"lib/pool_user.py": PYPOOL}))
    assert [g.verdict(f"lib/pool_user.py#apply.{q}") for q in ("one", "other")] != ["dead"] * 2
    assert not g.dead("lib/pool_user.py#apply.one") and not g.dead("lib/pool_user.py#apply.other")


# ====================================================================== 8 Go build-constraint pairs, 9 type and method of one name
def go_facts(defs, refs=(), roots=()):
    return md.Facts("go", tuple(md.Def(*d) for d in defs), tuple(md.Ref(*r) for r in refs), (), tuple(roots))


def analyse(files):
    lk = dlink.link(files, workers=1)
    res = reach.analyze(lk.graph, "closed_world")
    return {n.key.split(":", 1)[1]: res.verdict(n.key) for n in lk.graph.nodes}


MAIN_GO = go_facts(
    [("main", "func", 3, 5, "package", 0, -1, "")],
    [("live", "call", 4, "main")],
    [md.RootHint("main", "main", 3, "hard", "convention")],
)


def stub(name, line=7):
    return go_facts([(name, "func", line, line, "package", 0, -1, "")])


def test_go_files_that_exclude_each_other_define_one_symbol():
    procfs = go_facts(
        [("live", "func", 3, 5, "package", 0, -1, ""), ("collectItems", "func", 7, 9, "package", 0, -1, "")],
        [("collectItems", "call", 4, "live")],
    )
    files = {
        "gw/main.go": MAIN_GO,
        "gw/native.go": procfs,
        "gw/plat_windows.go": stub("collectItems"),
        "other/x.go": stub("collectItems"),
    }
    v = analyse(files)
    assert (
        v["gw/native.go#collectItems"] == "weak-alive"
    )  # the call resolves to the def of its own file (main.go reaches live by its name: N)
    assert v["gw/plat_windows.go#collectItems"] == "weak-alive"  # the same symbol built for another platform
    assert v["other/x.go#collectItems"] in DEAD  # another directory is another package: no twin


def test_go_twins_leave_test_files_alone():
    a = go_facts(
        [("live", "func", 3, 5, "package", 0, -1, ""), ("collectItems", "func", 7, 9, "package", 0, -1, "")],
        [("collectItems", "call", 4, "live")],
    )
    v = analyse({"p/main.go": MAIN_GO, "p/a.go": a, "p/a_test.go": stub("collectItems")})
    assert (
        v["p/a.go#collectItems"] == "weak-alive" and v["p/a_test.go#collectItems"] in DEAD
    )  # an external test package may repeat a name


def test_go_bare_name_in_a_method_is_the_type_never_the_method_of_the_same_name():
    f = go_facts(
        [
            ("server", "class", 3, 3, "package", 0, -1, ""),
            ("server.serveList", "method", 5, 8, "package", 0, -1, ""),
            ("renderPage", "class", 10, 12, "package", 0, -1, ""),
            ("server.renderPage", "method", 14, 16, "package", 0, -1, ""),
            ("main", "func", 18, 20, "package", 0, -1, ""),
        ],
        [
            ("renderPage", "ref", 6, "server.serveList"),
            ("server.serveList", "call", 19, "main"),
            ("s.serveList", "call", 19, "main"),
        ],
        [md.RootHint("main", "main", 18, "hard", "convention")],
    )
    v = analyse({"cmd/api/main.go": f})
    assert v["cmd/api/main.go#renderPage"] not in DEAD  # the type is used as a type
    assert (
        v["cmd/api/main.go#server.renderPage"] in DEAD
    )  # `s.renderPage(..)` was never written: the method has no caller


def test_python_class_body_rule_is_the_same_flag_and_still_holds():
    from dead_code_checker.graph import langs as dclangs

    spec = dclangs.load().langs
    assert spec["python"]["members_by_receiver"] and spec["go"]["members_by_receiver"] and spec["go"]["build_twins"]
    assert "members_by_receiver" not in spec["java"] and "build_twins" not in spec["rust"]
