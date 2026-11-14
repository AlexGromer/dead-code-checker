"""Precision rules for dead-cluster claims: five generic rules, each on SYNTHETIC fixtures with the control that must stay as it was.

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_precluster.py

A graph is built the way the levels build it (levels.build on a throwaway git repository, reach.analyze). The Python-extractor and linker parts
run everywhere; a test that needs a tree-sitter grammar is skipped with the reason when the host has none.
  1 shell: a command word that starts with literal text and goes on with an expansion (`"check_$t"`, `check_${t}`) is the glob name:check_* (D)
  2 JavaScript / TypeScript: a call statement at the top level of a file, or `if (require.main === module)`, makes the file a script (soft main);
    a package index (index.js, index.ts) is no script
  3 Python: the plugin classes that a framework builds by NAME and calls by contract (Ansible FilterModule.filters, LookupModule(LookupBase) ...),
    data py_class_roots, only in a file that is a plugin (a plugins directory, or an import of ansible)
  4 Python: the same module laid out under two roots (an overlay, a deploy payload): same relative path, same facts -> the defs keep each other alive (N, copytwin)
  5 Go: a value of a type written as a literal in an argument of a call on an imported package is used through an interface the tree does not
    declare: members:T (D), the exported members of a declared type only
"""

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test

ROOT = Path(__file__).resolve().parent.parent
from dead_code_checker.graph import contracts, levels, reach, roots, ts as tsm  # noqa: E402

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
    """The graph of a repository: verdict(path#qual), dead(path#qual), the roots of a kind."""

    def __init__(self, repo):
        self.g = levels.build(str(repo), None, workers=1).graph
        self.res = reach.analyze(self.g, "closed_world")
        self.by = {n.key.split(":", 1)[1]: n for n in self.g.nodes}

    def verdict(self, k):
        return self.res.verdict(self.by[k].key)

    def dead(self, k):
        return self.verdict(k) in DEAD

    def roots(self, kind):
        return sorted(r.node.split(":", 1)[1] for r in self.g.roots if r.kind == kind and r.node)


MAIN = (
    "#!/usr/bin/env python3\ndef main():\n    return 1\n\n\nif __name__ == '__main__':\n    main()\n",
    0o755,
)  # a production root, so that a graph is "rooted" and its dead verdicts are claims


def facts_of(lang, text, path="x"):
    return contracts.get_extractor(lang).extract(lang, path, text)


# ====================================================================== 1 shell: a computed function name with a literal prefix
PREFIX_SH = '#!/bin/bash\ncheck_a() { echo a; }\ncheck_b() { echo b; }\nunrelated() { echo u; }\nfor t in a b; do\n  "check_$t"\ndone\n'


def test_shell_command_word_with_a_literal_prefix_and_an_expansion_is_a_name_glob(tmp_path):
    need_grammar("bash")
    g = G(repo_of(tmp_path, {"main.py": MAIN, "ops/run.sh": PREFIX_SH}))
    assert [g.verdict(f"ops/run.sh#check_{x}") for x in "ab"] == ["weak-alive"] * 2
    assert g.dead("ops/run.sh#unrelated")  # a name that does not start with the prefix is not touched


def test_shell_prefix_in_braces_and_in_the_middle_of_a_string_are_read_too():
    need_grammar("bash")
    dyn = {d.pattern for d in facts_of("shell", 'check_${t} x\n"run_${a}_post" y\nfoo$x$y\n').dyn}
    assert {"name:check_*", "name:run_*", "name:foo*"} <= dyn


def test_shell_a_path_a_short_prefix_and_a_bare_expansion_make_no_name_glob():
    need_grammar("bash")
    text = '"$D/tool" --go\n/srv/$x run\n"./$y/z" a\na_$v c\n$cmd arg\n"${fn}" b\n'
    dyn = {d.pattern for d in facts_of("shell", text).dyn}
    assert not [p for p in dyn if p.startswith("name:")]  # `a_` is a prefix of 2 characters: the rule wants 3 or more
    assert "$cmd" in dyn  # the bare expansion keeps its own site (every function of the file, D)


def test_shell_prefix_site_only_touches_functions_of_that_prefix_in_other_files_too(tmp_path):
    need_grammar("bash")
    other = "#!/bin/bash\ncheck_z() { echo z; }\nbuild_z() { echo b; }\n"
    g = G(repo_of(tmp_path, {"main.py": MAIN, "ops/run.sh": PREFIX_SH, "ops/other.sh": other}))
    assert (
        g.verdict("ops/other.sh#check_z") == "weak-alive"
    )  # name:<glob> is by last name, over the family (documented over-approximation)
    assert g.dead("ops/other.sh#build_z")


# ====================================================================== 2 JavaScript / TypeScript: statements of a script
JS_SCRIPT = "function build() { return 1; }\nfunction helper() { return 2; }\nconsole.log(build());\n"


def test_js_top_level_call_statement_makes_the_file_a_script(tmp_path):
    need_grammar("javascript")
    g = G(repo_of(tmp_path, {"main.py": MAIN, "tools/gen.js": JS_SCRIPT}))
    assert g.verdict("tools/gen.js#build") == "weak-alive"
    assert g.dead("tools/gen.js#helper")  # only what the statements call is used
    assert "tools/gen.js" in g.roots("main")


def test_ts_script_and_the_iife_await_and_catch_forms(tmp_path):
    need_grammar("typescript")
    files = {
        "main.py": MAIN,
        "tools/a.ts": "function build(): number { return 1; }\nfunction helper(): number { return 2; }\nconsole.log(build());\n",
        "tools/b.js": "async function main() { return helper(); }\nfunction helper() { return 2; }\nmain().catch(e => { process.exit(1); });\n",
        "tools/c.js": "function build() { return 1; }\n(async () => { await build(); })();\n",
        "tools/d.js": "function build() { return 1; }\nawait build();\n",
    }
    g = G(repo_of(tmp_path, files))
    assert g.verdict("tools/a.ts#build") == "weak-alive" and g.dead("tools/a.ts#helper")
    assert g.verdict("tools/b.js#main") == "weak-alive" and g.verdict("tools/b.js#helper") == "weak-alive"
    assert g.verdict("tools/c.js#build") == "weak-alive"
    assert g.verdict("tools/d.js#build") == "weak-alive"


def test_js_require_main_guard_and_import_meta_main_make_a_script(tmp_path):
    need_grammar("javascript")
    files = {
        "main.py": MAIN,
        "tools/a.js": "function build() { return 1; }\nif (require.main === module) { build(); }\n",
        "tools/b.js": "function build() { return 1; }\nif (import.meta.main) { build(); }\n",
        "tools/c.js": "function build() { return 1; }\nif (process.env.X) { build(); }\n",
    }
    g = G(repo_of(tmp_path, files))
    assert g.verdict("tools/a.js#build") == "weak-alive" and g.verdict("tools/b.js#build") == "weak-alive"
    assert g.dead("tools/c.js#build")  # another condition is no script guard


def test_js_a_library_an_assignment_a_call_in_a_function_and_a_test_file_make_no_script(tmp_path):
    need_grammar("javascript")
    files = {
        "main.py": MAIN,
        "lib/util.js": "function load() { return 1; }\nmodule.exports = { load };\n",
        "tools/assign.js": "function build() { return 1; }\nconst out = build();\n",
        "tools/infn.js": "function build() { return 1; }\nfunction run() { return build(); }\n",
        "tests/a.test.js": "function helper() { return 1; }\ndescribe('x', () => { helper(); });\n",
    }
    g = G(repo_of(tmp_path, files))
    assert g.dead("lib/util.js#load")
    assert g.dead("tools/assign.js#build")  # a value computed at load is no statement that runs code
    assert g.dead("tools/infn.js#build") and g.dead("tools/infn.js#run")
    assert "tests/a.test.js" not in g.roots(
        "main"
    )  # the test framework runs the file: graph.link drops the statements of a test file


def test_js_a_package_index_is_no_script_unless_the_package_names_it(tmp_path):
    """`index.js` / `index.ts` of a package is loaded by `require`/`import`, nobody runs it; its top-level call statements made it a
    script root. A named script of the same shape stays one, and package.json main/bin (and npm scripts) root an index as they root any file.
    """
    need_grammar("javascript", "typescript")
    body = "function build() { return 1; }\nfunction helper() { return 2; }\nconsole.log(build());\n"
    files = {
        "main.py": MAIN,
        "pkg/index.js": body,
        "pkg/src/index.ts": body.replace("1;", "1;").replace("() {", "(): number {", 2),
        "pkg/index.d.ts": "declare function f(): void;\nconsole.log(1);\n",
        "tools/gen.js": body,  # the control: any other name with statements is a script
    }
    g = G(repo_of(tmp_path, files))
    assert g.dead("pkg/index.js#build") and g.dead("pkg/index.js#helper")
    assert g.dead("pkg/src/index.ts#build")
    assert "pkg/index.js" not in g.roots("main") and "pkg/src/index.ts" not in g.roots("main")
    assert g.verdict("tools/gen.js#build") == "weak-alive" and "tools/gen.js" in g.roots("main")
    named = {**files, "pkg/package.json": '{"name": "p", "main": "index.js"}\n'}
    g2 = G(repo_of(tmp_path / "named", named))
    assert g2.verdict("pkg/index.js#build") in (
        "alive",
        "weak-alive",
    )  # the package names it: its statements run, what they call is used
    assert g2.dead("pkg/index.js#helper")


# ====================================================================== 3 Python: plugin classes that a framework builds by name
FILTER = "def one(x):\n    return x\n\n\ndef spare(x):\n    return x\n\n\nclass FilterModule(object):\n    def filters(self):\n        return {'one': one}\n"


def test_ansible_filter_plugin_class_its_filters_method_and_the_functions_it_returns_are_used(tmp_path):
    g = G(repo_of(tmp_path, {"main.py": MAIN, "infra/filter_plugins/f.py": FILTER}))
    p = "infra/filter_plugins/f.py"
    assert [g.verdict(f"{p}#{q}") for q in ("FilterModule", "FilterModule.filters", "one")] == ["alive"] * 3
    assert g.dead(f"{p}#spare")  # a function that the plugin does not hand out is still dead
    assert f"{p}#FilterModule" in g.roots("annotation:ansible.filter") and f"{p}#FilterModule.filters" in g.roots(
        "annotation:ansible.filter"
    )


def test_ansible_plugin_classes_by_contract_method_by_base_and_by_name():
    py = lambda text: {
        (h.kind, h.target) for h in facts_of("python", text).roots if h.kind.startswith("annotation:ansible")
    }  # noqa: E731
    assert py("class LookupModule(LookupBase):\n    def run(self, t):\n        return t\n") == {
        ("annotation:ansible.lookup", "LookupModule"),
        ("annotation:ansible.lookup", "LookupModule.run"),
    }
    assert py("from ansible.plugins.lookup import LookupBase\nclass LookupModule(LookupBase):\n    pass\n") == {
        ("annotation:ansible.lookup", "LookupModule")
    }  # the base alone qualifies, no contract method to root
    assert py("class ModuleDocFragment(object):\n    DOCUMENTATION = 'x'\n") == {
        ("annotation:ansible.docfragment", "ModuleDocFragment")
    }
    assert py(
        "class CallbackModule(ansible.plugins.callback.CallbackBase):\n    def v2_playbook_on_start(self, r):\n        pass\n"
    ) == {
        ("annotation:ansible.callback", "CallbackModule")
    }  # a dotted base is read by its last name


def test_ansible_class_names_without_the_contract_and_nested_classes_are_no_plugins(tmp_path):
    py = lambda text: [
        h for h in facts_of("python", text).roots if h.kind.startswith("annotation:ansible")
    ]  # noqa: E731
    assert not py(
        "class FilterModule(object):\n    def other(self):\n        return 1\n"
    )  # no filters(): not the contract
    assert py(
        "class LookupModule(Base):\n    def run(self):\n        return 1\n"
    )  # run() is the contract of a lookup, whatever the base
    assert not py(
        "def make():\n    class FilterModule(object):\n        def filters(self):\n            return {}\n    return FilterModule\n"
    )
    assert not py(
        "class Wrapper:\n    class FilterModule(object):\n        def filters(self):\n            return {}\n"
    )
    g = G(
        repo_of(
            tmp_path,
            {"main.py": MAIN, "lib/f.py": "class FilterModule(object):\n    def other(self):\n        return 1\n"},
        )
    )
    assert g.dead("lib/f.py#FilterModule")


ACTION = "class ActionModule(object):\n    def run(self, tmp=None, task_vars=None):\n        return {}\n"
LOOKUP_IMPORT = "from ansible.plugins.lookup import LookupBase\n\n\nclass LookupModule(LookupBase):\n    def run(self, terms, variables=None):\n        return terms\n"


def test_ansible_plugin_roots_need_an_ansible_plugin_file(tmp_path):
    """any `class ActionModule: def run()` got a hard root. Only a file under a *_plugins/ (or plugins/) directory, or one
    that imports ansible.*, is a plugin; the extractor sees the text alone, so graph.link decides (data py_class_roots scope).
    """
    files = {
        "main.py": MAIN,
        "infra/action_plugins/act.py": ACTION,  # *_plugins/
        "infra/roles/web/filter_plugins/f.py": FILTER,
        "col/plugins/lookup/l.py": "class LookupModule(object):\n    def run(self, t):\n        return t\n",  # plugins/ of a collection
        "lib/imports.py": LOOKUP_IMPORT,  # not in a plugins dir, but imports ansible
        "lib/imports2.py": "import ansible.plugins.action as a\n\n\nclass ActionModule(a.ActionBase):\n    def run(self):\n        return {}\n",
        "lib/plain.py": ACTION,  # a class of that name and contract, no Ansible anywhere
        "lib/plain_filter.py": FILTER,
        "lib/relative.py": "from .ansible import LookupBase\n\n\nclass LookupModule(LookupBase):\n    def run(self, t):\n        return t\n",
        "lib/pluginsish.py": "class ActionModule(object):\n    def run(self):\n        return 1\n",  # `plugins` in the file name is no directory
        "plugins_x/notplugin.py": ACTION,  # a directory that merely starts with the word
    }
    g = G(repo_of(tmp_path, files))
    for p in (
        "infra/action_plugins/act.py#ActionModule",
        "infra/action_plugins/act.py#ActionModule.run",
        "infra/roles/web/filter_plugins/f.py#FilterModule",
        "col/plugins/lookup/l.py#LookupModule.run",
        "lib/imports.py#LookupModule",
        "lib/imports2.py#ActionModule.run",
    ):
        assert not g.dead(p), p
    assert g.dead("infra/roles/web/filter_plugins/f.py#spare")  # still only what the plugin hands out
    for p in (
        "lib/plain.py#ActionModule",
        "lib/plain.py#ActionModule.run",
        "lib/plain_filter.py#FilterModule",
        "lib/plain_filter.py#one",
        "lib/relative.py#LookupModule",
        "lib/pluginsish.py#ActionModule",
        "plugins_x/notplugin.py#ActionModule",
    ):
        assert g.dead(p), p
    assert [
        r for r in g.roots("annotation:ansible.action") if "#" in r
    ] == [  # (the files of the classes are roots too: running the file)
        "infra/action_plugins/act.py#ActionModule",
        "infra/action_plugins/act.py#ActionModule.run",
        "lib/imports2.py#ActionModule",
        "lib/imports2.py#ActionModule.run",
    ]
    assert not any(r.startswith(("lib/plain", "plugins_x")) for r in g.roots("annotation:ansible.action"))
    # the extractor still reports the hint from the text alone (a shard is keyed by the blob, not by the path)
    assert {h.kind for h in facts_of("python", ACTION).roots} >= {"annotation:ansible.action"}


def test_the_linker_version_follows_the_scope_data():
    """The Ansible scope (data py_class_roots scope) decides which roots the linker makes: it is part of LINKER_V, so a changed table (or the
    new linker rules, version 13) invalidates the stored graphs and the reach cache."""
    import hashlib
    import json

    from dead_code_checker.graph import link

    assert link.LINKER_V.startswith("13.")
    scope = roots.fw()["py_class_roots"]["scope"]
    assert scope == {"dirs": ["*_plugins", "plugins"], "imports": ["ansible", "ansible_collections"]}
    base = [roots.fw()["test_dirs"], roots.fw()["test_names"], link.xlink.rules()]
    h = lambda sc: hashlib.sha256(json.dumps([*base, sc], sort_keys=True, default=sorted).encode()).hexdigest()[
        :8
    ]  # noqa: E731
    assert link.LINKER_V == "13." + h(scope) and h(scope) != h({**scope, "dirs": ["*_plugins"]})


def test_py_class_roots_table_is_part_of_the_python_shard_key():
    from dead_code_checker.graph.extract import py as dpy

    assert dpy.PyExtractor().grammar_sha("python") != ""
    assert dpy._class_rules()[1] and "FilterModule" in dpy._class_rules()[0]


# ====================================================================== 4 Python: one module under two roots
LIBMOD = (
    "def discover(x):\n    return normalize(x)\n\n\ndef normalize(x):\n    return x\n\n\ndef unused(x):\n    return x\n"
)
USER = (
    "#!/usr/bin/env python3\nfrom pkg.core.reg import discover\n\n\ndef main():\n    return discover(1)\n\n\nif __name__ == '__main__':\n    main()\n",
    0o755,
)
OVERLAY = {
    "staging/overlay/lib/pkg/__init__.py": "",
    "staging/overlay/lib/pkg/core/__init__.py": "",
    "staging/overlay/lib/pkg/core/reg.py": LIBMOD,
}
BASE = {"main.py": USER, "pkg/__init__.py": "", "pkg/core/__init__.py": "", "pkg/core/reg.py": LIBMOD}


def test_identical_copy_under_another_root_keeps_the_same_defs_alive(tmp_path):
    g = G(repo_of(tmp_path, {**BASE, **OVERLAY}))
    ov = "staging/overlay/lib/pkg/core/reg.py"
    assert g.verdict("pkg/core/reg.py#discover") == "alive"
    assert [g.verdict(f"{ov}#{q}") for q in ("discover", "normalize")] == [
        "weak-alive"
    ] * 2  # what the original uses is used in its copy
    assert g.dead("pkg/core/reg.py#unused") and g.dead(f"{ov}#unused")  # the one nobody calls is dead in both


def test_a_copy_that_differs_by_one_line_a_name_clash_and_a_dead_original_are_no_twins(tmp_path):
    files = {**BASE, "staging/overlay/lib/pkg/core/reg.py": LIBMOD + "\n\ndef extra(x):\n    return x\n"}
    g = G(repo_of(tmp_path, files))
    assert g.dead("staging/overlay/lib/pkg/core/reg.py#discover")  # the facts differ: another module of the same name
    clash = {"main.py": MAIN, "svc_a/util/tools.py": LIBMOD, "svc_b/util/tools.py": LIBMOD}
    c = G(repo_of(tmp_path / "c", clash))
    assert c.dead("svc_a/util/tools.py#discover") and c.dead(
        "svc_b/util/tools.py#discover"
    )  # neither path ends with the other: no overlay
    dead = {"main.py": MAIN, "pkg/core/reg.py": LIBMOD, **OVERLAY}
    d = G(repo_of(tmp_path / "d", dead))
    assert d.dead("pkg/core/reg.py#discover") and d.dead(
        "staging/overlay/lib/pkg/core/reg.py#discover"
    )  # twins share fate: nobody uses either


def test_copy_twins_are_a_per_language_flag_of_langs_json():
    from dead_code_checker.graph import langs as dclangs

    spec = dclangs.load().langs
    assert spec["python"].get("copy_twins") is True and not spec["go"].get("copy_twins")


# ====================================================================== 5 Go: a value handed to another package
GO_IMPORTS = 'package main\n\nimport (\n\t"dep"\n)\n\nfunc main() {\n\trun()\n}\n'
GO_QUIET = "\ntype quiet struct{}\n\nfunc (quiet) Infof(f string, a ...any) {}\nfunc (quiet) Other() {}\n"


def test_go_literal_handed_to_an_imported_package_may_be_used_through_its_interface(tmp_path):
    need_grammar("go")
    files = {
        "go.mod": "module ex.com/x\n\ngo 1.21\n",
        "main.go": GO_IMPORTS + GO_QUIET + "func run() { dep.WithLogger(quiet{}) }\n",
    }
    g = G(repo_of(tmp_path, files))
    assert [g.verdict(f"main.go#quiet.{m}") for m in ("Infof", "Other")] == ["weak-alive"] * 2


def test_go_pointer_literal_counts_too_and_a_local_receiver_does_not(tmp_path):
    need_grammar("go")
    files = {
        "go.mod": "module ex.com/x\n\ngo 1.21\n",
        "main.go": GO_IMPORTS
        + GO_QUIET
        + "type mw struct{}\n\nfunc (*mw) Apply() {}\ntype loc struct{}\n\nfunc (loc) Spare() {}\n"
        "type reg struct{}\n\nfunc (reg) Add(x any) {}\nfunc run() { dep.Use(&mw{}); r := reg{}; r.Add(loc{}) }\n",
    }
    g = G(repo_of(tmp_path, files))
    assert g.verdict("main.go#mw.Apply") == "weak-alive"
    assert g.dead(
        "main.go#loc.Spare"
    )  # `r.Add(loc{})`: r is a local value, the callee is in the tree: matched by interface name
    assert g.dead("main.go#quiet.Infof")  # quiet is never handed over here


GO_MIXED = (
    "\ntype quiet struct{}\n\nfunc (quiet) Infof(f string, a ...any) {}\nfunc (quiet) debugf() {}\nfunc (quiet) Ünicode() {}\n"
    "func (quiet) _x() {}\nfunc (*quiet) Ptr() {}\n"
)


def test_go_only_the_exported_methods_of_a_value_handed_to_another_package_stay_used(tmp_path):
    """members:T kept EVERY member of T; code of another package can call only the exported ones through an interface."""
    need_grammar("go")
    files = {
        "go.mod": "module ex.com/x\n\ngo 1.21\n",
        "main.go": GO_IMPORTS
        + GO_MIXED
        + "func (q quiet) helper() { q.debugf() }\nfunc run() { dep.WithLogger(&quiet{}) }\n",
    }
    g = G(repo_of(tmp_path, files))
    assert [g.verdict(f"main.go#quiet.{m}") for m in ("Infof", "Ünicode", "Ptr")] == ["weak-alive"] * 3
    assert g.dead("main.go#quiet.debugf") and g.dead("main.go#quiet._x")  # unexported: no other package can call them
    assert g.dead("main.go#quiet.helper")


def test_go_a_type_that_the_tree_does_not_declare_keeps_nothing_alive(tmp_path):
    """The fallback of an unresolved T was `every def of the file` (and the enclosing type): back to what it was before the rule, nothing."""
    need_grammar("go")
    files = {
        "go.mod": "module ex.com/x\n\ngo 1.21\n",
        "main.go": GO_IMPORTS
        + "\ntype holder struct{}\n\nfunc (holder) Method() { dep.WithLogger(ghost{}) }\nfunc (holder) Spare() {}\nfunc spare() {}\n"
        "func run() { dep.WithLogger(ghost{}); holder{}.Method() }\n",
    }
    g = G(repo_of(tmp_path, files))
    assert g.dead("main.go#spare") and g.dead(
        "main.go#holder.Spare"
    )  # `ghost` is declared nowhere: no member, no def of the file is kept
    assert g.verdict("main.go#holder.Method") != "dead"  # called by run: alive by its own reference, not by the site


def test_go_the_dyn_site_is_a_fact_of_the_extractor_with_the_import_alias_check():
    need_grammar("go")
    src = GO_IMPORTS + GO_QUIET + "func run() { dep.WithLogger(quiet{}); other.X(quiet{}); dep.Y(pkg.T{}) }\n"
    assert [d.pattern for d in facts_of("go", src, "main.go").dyn] == [
        "members:quiet"
    ]  # `other` is no import, pkg.T is no local type


# ====================================================================== the linker rules on hand-written facts (no grammar needed)
def _facts(lang, defs, refs=(), roots=(), dyn=(), imports=()):
    from dead_code_checker.graph import model as md

    return md.Facts(
        lang,
        tuple(md.Def(*d) for d in defs),
        tuple(md.Ref(*r) for r in refs),
        tuple(imports),
        tuple(roots),
        tuple(md.DynSite(*d) for d in dyn),
    )


def _verdicts(files):
    from dead_code_checker.graph import link as dlink

    lk = dlink.link(files, workers=1)
    res = reach.analyze(lk.graph, "closed_world")
    return {n.key.split(":", 1)[1]: res.verdict(n.key) for n in lk.graph.nodes}


def _hard_main(line=3):
    from dead_code_checker.graph import model as md

    return md.RootHint("main", "main", line, "hard", "convention")


def test_linker_go_members_site_keeps_the_exported_members_of_a_declared_type_only():
    f = _facts(
        "go",
        [
            ("main", "func", 3, 5, "package", 0, -1, ""),
            ("quiet", "class", 7, 7, "package", 0, -1, ""),
            ("quiet.Infof", "method", 8, 8, "package", 0, -1, ""),
            ("quiet.debugf", "method", 9, 9, "package", 0, -1, ""),
            ("quiet._x", "method", 10, 10, "package", 0, -1, ""),
            ("quiet.Ünicode", "method", 11, 11, "package", 0, -1, ""),
        ],
        roots=[_hard_main()],
        dyn=[("members:quiet", 4, "main")],
    )
    v = _verdicts({"main.go": f})
    assert v["main.go#quiet.Infof"] not in DEAD and v["main.go#quiet.Ünicode"] not in DEAD
    assert v["main.go#quiet.debugf"] in DEAD and v["main.go#quiet._x"] in DEAD  # no other package can call an unexported method


def test_linker_go_members_site_of_an_undeclared_type_keeps_nothing():
    f = _facts(
        "go",
        [
            ("main", "func", 3, 5, "package", 0, -1, ""),
            ("holder", "class", 7, 7, "package", 0, -1, ""),
            ("holder.Spare", "method", 8, 8, "package", 0, -1, ""),
            ("spare", "func", 9, 9, "package", 0, -1, ""),
        ],
        roots=[_hard_main()],
        dyn=[("members:ghost", 4, "main"), ("members:ghost", 8, "holder.Spare")],
    )
    v = _verdicts({"main.go": f})
    assert v["main.go#spare"] in DEAD and v["main.go#holder.Spare"] in DEAD  # `ghost` is declared nowhere: neither the file nor the enclosing type is kept


def test_linker_a_js_package_index_is_no_script_but_another_file_is():
    from dead_code_checker.graph import model as md

    body = [("build", "func", 1, 1, "public", 0, -1, ""), ("helper", "func", 2, 2, "public", 0, -1, "")]
    soft = [md.RootHint("main", "", 3, "soft", "convention")]
    calls = [("build", "call", 3, "")]
    files = {
        "pkg/index.js": _facts("javascript", body, calls, soft),
        "pkg/index.d.ts": _facts("typescript", body, calls, soft),
        "tools/gen.js": _facts("javascript", body, calls, soft),
    }
    v = _verdicts(files)
    assert v["pkg/index.js#build"] in DEAD and v["pkg/index.d.ts#build"] in DEAD
    assert v["tools/gen.js#build"] not in DEAD and v["tools/gen.js#helper"] in DEAD
