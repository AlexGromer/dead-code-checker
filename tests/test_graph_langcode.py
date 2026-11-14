"""Code languages read by tree-sitter queries and rules as data: Lua, Ruby, PHP, Scala, Groovy, Nix, Starlark and the markup hosts HTML, Vue, Svelte.

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_langcode.py

What runs where
  recorded   tests/fixtures/langcode/<case>/parse.json holds the query matches the real grammars gave when the fixtures were recorded;
             the mapping to Facts, the linker and the verdicts of every case run on them with no grammar installed, and the recorded
             matches are tied to the query files (a query edit that moves a pattern fails the test).
  real       the same cases through the real grammar (tests/real_grammar.py asks graph.ts); skipped with the reason when tree-sitter or
             the grammar is not installed.
  seams      the data-driven rules of ts_generic (path_sub, ref_cmd, path_roots, def_kw, pascal, embed), langs.json entry_re and the test-name patterns.
Each case is a closed world: expected.json has the verdict of every definition ("closed_world": {"<path>#<qual>": verdict}), the verdict of
every file ("files") and some roots that the extractor must give; both maps were reviewed by hand against the sources.
"""

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test

ROOT = Path(__file__).resolve().parent.parent
import real_grammar as cg  # noqa: E402
import recorded_parse as rp  # noqa: E402
from dead_code_checker.graph import contracts, langs as dclangs, link as dlink, reach as rc, roots as droots  # noqa: E402
from dead_code_checker.graph.extract import ts_generic as tg  # noqa: E402
from dead_code_checker.graph.model import RootHint  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "langcode"
QDIR = Path(tg.QUERY_DIR)
LG = dclangs.load()
NEW = ("lua", "ruby", "php", "scala", "groovy", "nix", "starlark", "html", "vue", "svelte")
GRAMMARS = {"lua", "ruby", "php", "scala", "groovy", "nix", "starlark", "html", "vue", "svelte"}
CASES = ["lua", "ruby", "php", "scala", "groovy", "nix", "starlark", "web", "svelte", "jvm"]


@pytest.fixture(autouse=True)
def _no_backend():
    yield
    tg.set_backend(None)


# ====================================================================== cases
def sources(case: str) -> dict:
    base = FIX / case
    return {f.relative_to(base).as_posix()[:-4]: f.read_text(encoding="utf-8") for f in sorted(base.rglob("*.txt"))}


def recorded(case: str) -> dict:
    return json.loads((FIX / case / "parse.json").read_text(encoding="utf-8"))


def code_files(case: str) -> list:
    return [p for p in sources(case) if LG.detect(p) and LG.extractor(LG.detect(p)) == "ts_generic"]


def install_recorded(case: str) -> None:
    """The seam: the recorded matches of whichever text the extractor is asked about (a file, or a block that a file embeds)."""
    by_text = {}
    for key, entry in recorded(case).items():
        text = entry["src"] if "src" in entry else sources(case)[key]
        by_text[text] = {k: v for k, v in entry.items() if k in rp.KINDS}
    tg.set_backend(lambda grammar, data: rp.Recorded(data.decode(), by_text[data.decode()]))


def extract_all(case: str) -> dict:
    srcs = sources(case)
    return {p: contracts.get_extractor(LG.detect(p)).extract(LG.detect(p), p, srcs[p]) for p in code_files(case)}


def verdicts(files: dict) -> dict:
    # the entry roots are the production ones: graph.roots resolves script_lang (langs.json script_ext and entry_re) to a hard main root
    found = droots.resolve({}, {p: "100644" for p in files}, lambda p: None)
    entries = sorted(r.node.split(":", 1)[1] for r in found.roots if r.kind == "main")
    assert entries == sorted(p for p in files if LG.script_lang(p)), entries
    hints = {p: [RootHint("main", "file:" + p, 1, "hard", "convention")] for p in entries}
    res = dlink.link(files, hints, workers=1)
    assert not res.findings, res.findings
    rr = rc.analyze(res.graph)
    return {n.key.split(":", 1)[1]: rr.verdict(n.key) for n in res.graph.nodes}


def check_golden(case: str, got: dict) -> None:
    exp = json.loads((FIX / case / "expected.json").read_text(encoding="utf-8"))
    assert {k: v for k, v in got.items() if "#" in k} == exp[
        "closed_world"
    ]  # every definition, the metrics corpus format
    assert {k: v for k, v in got.items() if "#" not in k} == exp["files"]
    assert {tuple(r) for r in exp["roots"]} <= {
        (h.kind, f"{p}#{h.target}") for p, f in FILES[case].items() for h in f.roots
    }


FILES: dict = {}


# ====================================================================== queries are data
def test_every_new_language_has_a_grammar_queries_and_rules():
    for lang in NEW:
        spec = LG.info(lang)
        assert spec["extractor"] == "ts_generic" and spec["role"] == "code" and spec["grammar"] in GRAMMARS, lang
        assert lang in json.loads((ROOT / "dead_code_checker/graph/data/rules.json").read_text()), lang  # {} = no rules
        qdir = QDIR / spec["grammar"]
        assert (qdir / "refs.scm").is_file() and (qdir / "rules.json").is_file(), lang
        assert {p.stem for p in qdir.glob("*.scm")} <= set(tg.QUERY_KINDS), lang
        json.loads((qdir / "rules.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("grammar", sorted(GRAMMARS))
def test_queries_are_balanced_and_carry_no_predicates(grammar):
    for p in sorted((QDIR / grammar).glob("*.scm")):
        text = re.sub(r";[^\n]*", "", p.read_text(encoding="utf-8"))
        assert text.count("(") == text.count(")") and text.count("[") == text.count("]"), p.name
        assert "#" not in text, "queries carry no predicates"
        for cap in set(re.findall(r"@([A-Za-z_.]+)", text)):
            head = cap.split(".")[0]
            assert head in {
                "def",
                "ref",
                "root",
                "stub",
                "dyn",
                "vis",
                "flag",
                "name",
                "mod",
                "kw",
                "cmd",
                "scope",
                "member",
                "scan",
                "block",
                "script",
                "tag",
                "recv",
                "obj",
                "body",
                "local",
                "self",
            }, (p.name, cap)


@pytest.mark.parametrize("case", CASES)
def test_recorded_parses_fit_their_query_files(case):
    n = 0
    for key, entry in recorded(case).items():
        lang = LG.detect(key.partition("#")[0])
        grammar = entry.get("grammar") or LG.grammar(lang)
        n += rp.check_patterns(QDIR / grammar, {key: {k: v for k, v in entry.items() if k in rp.KINDS}})
    assert n > 10


def test_a_recorded_match_that_does_not_fit_the_query_is_refused():
    with pytest.raises(AssertionError):
        rp.check_patterns(QDIR / "lua", {"a.lua": {"defs": [[99, {"def.func": "x", "name": "x"}]]}})
    with pytest.raises(AssertionError):
        rp.check_patterns(QDIR / "lua", {"a.lua": {"defs": [[0, {"def.func": "x", "ghost": "x"}]]}})


# ====================================================================== golden fixtures: recorded matches (no grammar needed)
@pytest.mark.parametrize("case", CASES)
def test_golden_fixture_on_the_recorded_parse(case):
    install_recorded(case)
    FILES[case] = extract_all(case)
    assert all(f.errors == 0 for f in FILES[case].values())
    check_golden(case, verdicts(FILES[case]))


# ====================================================================== real grammars (the helper loads them from the cache, unlocked)
def _needs(*grammars):
    why = next((r for r in (cg.reason(g) for g in grammars) if r), "")
    return pytest.mark.skipif(bool(why), reason=f"real grammar not usable on this host: {why}")


REAL = {
    "lua": ["lua"],
    "ruby": ["ruby"],
    "php": ["php"],
    "scala": ["scala"],
    "groovy": ["groovy"],
    "nix": ["nix"],
    "starlark": ["starlark"],
    "web": ["html", "vue", "javascript", "typescript", "php"],
    "svelte": ["svelte", "typescript", "javascript"],
    "jvm": ["java", "scala", "groovy"],
}


@pytest.mark.parametrize("case", CASES)
def test_real_grammar_agrees_with_the_recorded_facts_and_the_golden_fixture(case):
    if why := next((r for r in (cg.reason(g) for g in REAL[case]) if r), ""):
        pytest.skip(f"real grammar not usable on this host: {why}")
    install_recorded(case)
    rec = extract_all(case)
    tg.set_backend(None)
    with cg.use(*REAL[case]):
        real = extract_all(case)
    for p, f in rec.items():
        assert (f.defs, f.refs, f.imports, f.roots, f.dyn) == (
            real[p].defs,
            real[p].refs,
            real[p].imports,
            real[p].roots,
            real[p].dyn,
        ), p
    FILES[case] = real
    check_golden(case, verdicts(real))


# ====================================================================== seams: the data-driven rules of ts_generic
def pidx(grammar: str, kind: str, *caps: str) -> int:
    """Index of the first pattern of the query file that has all `caps`."""
    return next(i for i, c in enumerate(rp.scm_patterns(QDIR / grammar / f"{kind}.scm")) if set(caps) <= c)


def from_matches(lang: str, path: str, src: str, matches: dict):
    tg.set_backend(lambda grammar, data: rp.Recorded(src, matches))
    return contracts.get_extractor(lang).extract(lang, path, src)


@pytest.mark.parametrize(
    "grammar,name,want",
    [
        ("lua", "lib.util", "lib/util.lua"),
        ("lua", "x.lua", "x.lua"),
        ("lua", "cjson", "cjson.lua"),
        ("ruby", "lib/helper", "lib/helper.rb"),
        ("ruby", "tasks.rb", "tasks.rb"),
        ("nix", "./mk.nix", "./mk.nix"),
        ("nix", "./pkgs", "./pkgs/default.nix"),
        ("nix", "../common/", "../common/default.nix"),
        ("nix", "..", "../default.nix"),
        ("nix", "/etc/x.nix", ""),
        ("nix", "<nixpkgs>", ""),
        ("nix", "./config.json", "./config.json"),
        ("starlark", "//tools:defs.bzl", "tools/defs.bzl"),
        ("starlark", ":local.bzl", "local.bzl"),
        ("starlark", "//:root.bzl", "root.bzl"),
        ("starlark", "@rules_cc//cc:defs.bzl", ""),
        ("html", "css/site.css?v=1", "css/site.css"),
        ("html", "about.html#top", "about.html"),
        ("html", "https://x.org/a.js", ""),
        ("html", "#top", ""),
        ("html", "mailto:a@b.c", ""),
        ("html", "//cdn.x/a.js", ""),
        ("html", "/img/logo.png", "img/logo.png"),
    ],
)
def test_path_sub_spells_a_file_reference_as_a_path(grammar, name, want):
    assert tg._path_name(tg.rules_of(grammar), "include", name) == want
    assert tg._path_name(tg.rules_of(grammar), "call", name) == name  # only the refs of the kind that names a file


def test_an_include_pattern_is_kept_by_its_callee_not_by_the_shell_command_list():
    src = 'require("lib.util")\nprint("a.b")\n'
    inc = pidx("lua", "refs", "ref.include", "cmd")
    f = from_matches(
        "lua",
        "m.lua",
        src,
        {
            "refs": [
                [inc, {"ref.include": 'require("lib.util")', "cmd": "require", "name": "lib.util"}],
                [inc, {"ref.include": 'print("a.b")', "cmd": "print", "name": "a.b"}],
            ]
        },
    )
    assert [r.name for r in f.refs if r.kind == "include"] == ["lib/util.lua"]


def test_a_dyn_site_needs_a_callee_the_rule_names():
    src = "require(name)\nprint(x)\n"
    dyn = pidx("lua", "refs", "dyn", "cmd")
    f = from_matches(
        "lua",
        "m.lua",
        src,
        {
            "refs": [
                [dyn, {"block": "require(name)", "cmd": "require", "dyn": "name"}],
                [dyn, {"block": "print(x)", "cmd": "print", "dyn": "x"}],
            ]
        },
    )
    assert [d.pattern for d in f.dyn] == ["name"]


def test_path_roots_root_the_definitions_of_the_files_a_framework_calls():
    src = "class UsersController\n  def index; end\nend\n"
    cls, meth = pidx("ruby", "defs", "def.class"), pidx("ruby", "defs", "def.func")
    m = {
        "defs": [
            [cls, {"def.class": src.strip(), "name": "UsersController"}],
            [meth, {"def.func": "def index; end", "name": "index"}],
        ]
    }
    hit = from_matches("ruby", "app/controllers/users_controller.rb", src, m)
    assert {(h.kind, h.target, h.strength) for h in hit.roots} == {
        ("annotation:rails", "UsersController", "hard"),
        ("annotation:rails", "UsersController.index", "hard"),
    }
    assert not from_matches("ruby", "lib/users.rb", src, m).roots
    model = from_matches("ruby", "app/models/user.rb", src, m)
    assert {(h.target, h.strength) for h in model.roots} == {
        ("UsersController", "soft"),
        ("UsersController.index", "soft"),
    }  # a model is reached by an association symbol or autoload: its class and methods are roots, only weakly
    service = from_matches("ruby", "app/services/pay.rb", src, m)
    assert {h.target for h in service.roots} == {"UsersController.index"}  # a service class is named by its constant


def test_a_loose_grammar_definition_needs_its_keyword_word():
    src = "class Tools {\n}\n"
    p = pidx("groovy", "defs", "def.class", "kw", "name")
    for kw, want in (("class", {"Tools"}), ("def", set())):
        text = src.replace("class", kw)
        f = from_matches(
            "groovy", "lib/T.groovy", text, {"defs": [[p, {"def.class": text.strip(), "kw": kw, "name": "Tools"}]]}
        )
        assert {d.qual for d in f.defs if d.qual} == want, kw


def test_a_template_tag_is_a_component_and_an_element_is_not():
    src = "<div><MyComp/><my-comp/><b>x</b></div>\n"
    p = pidx("vue", "refs", "ref.ref", "name")
    caps = lambda tag: {"ref.ref": f"<{tag}", "name": tag}  # noqa: E731
    f = from_matches(
        "vue", "a.vue", src, {"refs": [[p, caps("MyComp")], [p, caps("my-comp")], [p, caps("b")], [p, caps("div")]]}
    )
    assert {r.name for r in f.refs} == {"MyComp", "my-comp"}


def test_a_scan_pattern_with_a_callee_rule_reads_only_the_attributes_it_names():
    src = '<a class="btn" onclick="go()">x</a>\n'
    p = pidx("html", "refs", "scan", "cmd")
    f = from_matches(
        "html",
        "a.html",
        src,
        {
            "refs": [
                [p, {"block": 'class="btn"', "cmd": "class", "scan": "btn"}],
                [p, {"block": 'onclick="go()"', "cmd": "onclick", "scan": "go()"}],
            ]
        },
    )
    assert {(r.name, r.kind) for r in f.refs} == {("go", "call")}


def test_a_bare_attribute_value_is_a_file_edge_in_a_component():
    install_recorded("svelte")
    f = extract_all("svelte")["src/routes/+page.svelte"]
    assert "logo.png" in {r.name for r in f.refs if r.kind == "include"}  # <img src=logo.png alt="logo">
    assert "alt" not in {r.name for r in f.refs}  # not a file attribute


@pytest.mark.parametrize("grammar", ["html", "vue", "svelte"])
def test_every_attribute_value_pattern_has_a_bare_sibling(grammar):
    """HTML5 allows src=a.js: a pattern on (quoted_attribute_value (attribute_value)) needs one on the bare (attribute_value) (no grammar needed)."""
    quoted_form = "(quoted_attribute_value (attribute_value)"
    pats = rp.scm_pattern_texts(QDIR / grammar / "refs.scm")
    quoted = [p for p in pats if quoted_form in p]
    bare = [p for p in pats if "(attribute_value)" in p.replace(quoted_form, "")]
    assert len(quoted) == len(bare) >= 1, (grammar, quoted, bare)


# ====================================================================== constructed cases on the real grammar
def real_verdicts(grammars, srcs: dict) -> dict:
    """Extract, link (entry files are hard roots) and analyse `srcs` with the real grammars of the cache; {qualified key: verdict}."""
    if why := next((r for r in (cg.reason(g) for g in grammars) if r), ""):
        pytest.skip(f"real grammar not usable on this host: {why}")
    with cg.use(*grammars):
        files = {p: contracts.get_extractor(LG.detect(p)).extract(LG.detect(p), p, t) for p, t in srcs.items()}
    return verdicts(files)


ALIVE = ("alive", "weak-alive")


def test_a_method_passed_as_a_block_symbol_is_used():
    v = real_verdicts(
        ["ruby"],
        {
            "Rakefile": "A.go\n",
            "lib/a.rb": "class A\n  def self.go\n    [1].map(&:norm)\n  end\n  def norm; end\n  def stale; end\nend\n",
        },
    )
    assert v["lib/a.rb#A.norm"] in ALIVE and v["lib/a.rb#A.stale"] == "dead", v


def test_a_model_reached_only_by_an_association_symbol_is_not_dead():
    v = real_verdicts(
        ["ruby"],
        {
            "app/models/post.rb": "class Post < ApplicationRecord\n  has_many :comments\nend\n",
            "app/models/comment.rb": "class Comment < ApplicationRecord\n  belongs_to :post\nend\n",
            "app/services/orphan.rb": "class Orphan\nend\n",
        },
    )
    assert v["app/models/post.rb#Post"] in ALIVE and v["app/models/comment.rb#Comment"] in ALIVE, v
    assert v["app/services/orphan.rb#Orphan"] not in ALIVE, v  # a service is named by its constant: nothing names it


def test_a_lua_function_stored_as_a_member_value_is_used():
    v = real_verdicts(
        ["lua"],
        {
            "init.lua": "local u = require('lib.util')\nregister(u.g)\nlocal t = {on = u.h}\nu.k = u.m\nreturn t\n",
            "lib/util.lua": "local M = {}\n" + "".join(f"function M.{n}() end\n" for n in "ghkmz") + "return M\n",
        },
    )
    assert all(v[f"lib/util.lua#M.{n}"] in ALIVE for n in "ghm"), v  # register(u.g), {on = u.h}, u.k = u.m
    assert v["lib/util.lua#M.z"] == "dead", v


@pytest.mark.parametrize("attr", ["src=app.js", 'src="app.js"', "src='app.js'"])
def test_a_script_src_is_an_edge_with_any_quoting(attr):
    v = real_verdicts(
        ["html", "javascript"],
        {"index.html": f"<script {attr}></script>\n", "app.js": "function boot() {}\nboot();\n"},
    )
    assert v["app.js"] in ALIVE and v["app.js#boot"] in ALIVE, v


def test_a_form_action_is_an_edge_to_the_script_it_names():
    v = real_verdicts(
        ["html", "php"],
        {"index.html": "<form action=submit.php></form>\n", "submit.php": "<?php\nfunction h() {}\nh();\n"},
    )
    assert v["submit.php"] in ALIVE, v


@pytest.mark.parametrize("handler", ["@click=onGo3", '@click="onGo3"', ":p=onGo3"])
def test_a_vue_handler_is_used_with_any_quoting(handler):
    v = real_verdicts(
        ["vue", "html", "javascript"],
        {
            "index.html": "<script type=module src=main.js></script>\n",
            "main.js": "import App from './src/App.vue';\nApp;\n",
            "src/App.vue": f"<template><b {handler}>x</b></template>\n<script>\nfunction onGo3() {{}}\nfunction idle() {{}}\n</script>\n",
        },
    )
    assert v["src/App.vue#onGo3"] in ALIVE and v["src/App.vue#idle"] == "dead", v


# ====================================================================== embedded code
def test_a_script_block_is_extracted_as_javascript_with_the_lines_of_the_page():
    install_recorded("web")
    f = extract_all("web")["index.html"]
    page = sources("web")["index.html"].splitlines()
    onclick = next(d for d in f.defs if d.qual == "onGo")
    assert "function onGo" in page[onclick.line0 - 1]
    assert [(i.module, i.names) for i in f.imports] == [
        ("./mod.js", ("a",))
    ]  # the module script: a JS import, resolved by the linker
    assert {(h.kind, h.target, h.strength) for h in f.roots} == {
        ("script", "onGo", "soft")
    }  # an inline handler calls it by name
    assert not [d for d in f.defs if d.qual not in ("", "onGo")]  # the JSON block and the src= block are no code
    assert f.lang == "html" and f.errors == 0


def test_the_attributes_of_a_page_are_file_edges_and_nothing_else():
    install_recorded("web")
    f = extract_all("web")["index.html"]
    assert sorted(r.name for r in f.refs if r.kind == "include") == [
        "about.html",
        "css/site.css",
        "img/logo.png",
        "js/app.js",
        "js/late.js",
        "src/main.js",
        "submit.php",
    ]
    assert "stylesheet" not in {r.name for r in f.refs}  # a class or rel value is no script text


def test_a_vue_component_imported_by_the_script_is_reached_through_its_template_tag():
    install_recorded("web")
    f = extract_all("web")["src/App.vue"]
    assert ("MyComp", "ref", "") in {(r.name, r.kind, r.scope) for r in f.refs}
    assert ("./MyComp.vue", ("default as MyComp",)) in {(i.module, i.names) for i in f.imports}
    assert {"handler", "fmt"} <= {r.name for r in f.refs if r.kind == "call"}  # @click="handler($event)", {{ fmt(n) }}
    assert "onBare" in {r.name for r in f.refs}  # @click=onBare: a bare value is a handler too
    assert not {h.kind for h in f.roots}  # the template words are the uses, no definition is rooted


# ====================================================================== langs.json
def test_entry_re_makes_the_files_a_tool_runs_or_serves_entry_points():
    for path, lang in [
        ("Rakefile", "ruby"),
        ("config.ru", "ruby"),
        ("lib/tasks/db.rake", "ruby"),
        ("public/index.php", "php"),
        ("index.php", "php"),
        ("Jenkinsfile", "groovy"),
        ("app/build.gradle", "groovy"),
        ("flake.nix", "nix"),
        ("a/default.nix", "nix"),
        ("pkg/BUILD.bazel", "starlark"),
        ("BUILD", "starlark"),
        ("web/+page.svelte", "svelte"),
        ("site/index.html", "html"),
        ("pkg/init.lua", "lua"),
        ("project/build.sbt", "scala"),
        ("x.sc", "scala"),
    ]:
        assert LG.script_lang(path) == lang, path
    for path in (
        "lib/util.rb",
        "src/Mailer.php",
        "lib/mod.lua",
        "tools/defs.bzl",
        "src/App.vue",
        "modules/box.nix",
        "lib/Tools.groovy",
        "src/Service.scala",
    ):
        assert LG.script_lang(path) is None, path


def test_the_new_languages_are_detected_by_name_extension_and_shebang():
    for path, lang, head in [
        ("a/b.rb", "ruby", None),
        ("x.gemspec", "ruby", None),
        ("Gemfile", "ruby", None),
        ("bin/t", "ruby", b"#!/usr/bin/env ruby\n"),
        ("a.php", "php", None),
        ("t", "php", b"#!/usr/bin/php\n"),
        ("a.lua", "lua", None),
        ("t", "lua", b"#!/usr/bin/env lua\n"),
        ("A.scala", "scala", None),
        ("a.groovy", "groovy", None),
        ("a.nix", "nix", None),
        ("a.bzl", "starlark", None),
        ("WORKSPACE", "starlark", None),
        ("a.html", "html", None),
        ("a.vue", "vue", None),
        ("a.svelte", "svelte", None),
    ]:
        assert LG.detect(path, head) == lang, (path, head)


def test_jvm_languages_share_one_link_group_and_class_by_name_languages_keep_a_used_file_alive():
    assert LG.info("scala")["link_group"] == LG.info("groovy")["link_group"] == LG.info("java")["link_group"] == "jvm"
    assert all(LG.info(k).get("file_init") for k in ("ruby", "php", "scala", "groovy"))
    assert {LG.info(k)["family"] for k in ("html", "vue", "svelte")} == {
        "js"
    }  # their script blocks are JS / TS: one linker family


def test_test_files_of_the_new_languages_are_test_paths():
    from dead_code_checker.graph import roots

    for name in (
        "UserSpec.scala",
        "AppTest.scala",
        "ToolsTests.groovy",
        "user_spec.rb",
        "user_test.rb",
        "test_user.rb",
        "util_spec.lua",
        "UserTest.php",
    ):
        assert roots.is_test_path(f"src/{name}"), name
    for name in ("user.rb", "Mailer.php", "util.lua", "Service.scala"):
        assert not roots.is_test_path(f"src/{name}"), name


# ====================================================================== bare names of inherited and mixed-in members (rules self_word, @self, @local)
def test_a_ruby_method_called_by_its_bare_name_from_a_subclass_or_an_including_class_is_used():
    v = real_verdicts(
        ["ruby"],
        {
            "Rakefile": 'require_relative "lib/a"\nCircle.new.go(1)\nCircle.new.show\n',
            "lib/a.rb": "class Base\n  def base_m; end\n  def idle; end\n  def arg; end\nend\n"
            "module Mix\n  def inst; end\n  def host_call\n    host_m\n  end\nend\n"
            "class Circle < Base\n  include Mix\n  def go(arg)\n    arg + base_m + inst\n    base_m.size\n  end\n"
            "  def show\n    host_call\n  end\n  def host_m; end\nend\n",
        },
    )
    for q in (
        "Base.base_m",
        "Mix.inst",
        "Mix.host_call",
        "Circle.host_m",
    ):  # a subclass, an includer, a module that calls its host
        assert v[f"lib/a.rb#{q}"] in ALIVE, (q, v)
    assert v["lib/a.rb#Base.idle"] == "dead", v
    assert v["lib/a.rb#Base.arg"] == "dead", v  # a parameter named like a method is no call of it


def test_a_scala_member_called_by_its_bare_name_from_a_subclass_or_a_trait_user_is_used():
    v = real_verdicts(
        ["scala"],
        {
            "src/B.scala": "trait T { def tm: Int = 1; def tn: Int = 2 }\nclass A { def base_m = 1; def never = 2; def p = 3 }\n"
            "class B extends A with T {\n  def go(p: Int) = {\n    val loc = 1\n    if (flag) base_m + tm + p + loc else 0\n  }\n"
            "  def flag = true\n}\nobject Main { def main(args: Array[String]): Unit = println(new B().go(1)) }\n",
        },
    )
    for q in ("A.base_m", "T.tm", "B.flag"):
        assert v[f"src/B.scala#{q}"] in ALIVE, (q, v)
    assert (
        v["src/B.scala#A.never"] == v["src/B.scala#T.tn"] == v["src/B.scala#A.p"] == "dead"
    ), v  # p is a parameter in go


def test_a_scala_object_with_main_is_an_entry_and_so_is_every_object_that_holds_it():
    v = real_verdicts(
        ["scala"],
        {
            "src/Main.scala": "object Main { def main(args: Array[String]): Unit = println(1) }\n"
            "object Outer { object Inner { def main(a: Array[String]): Unit = () } }\nobject Idle { def f = 1 }\nclass Lone\n"
        },
    )
    assert all(
        v[f"src/Main.scala#{q}"] in ALIVE for q in ("Main", "Main.main", "Outer", "Outer.Inner", "Outer.Inner.main")
    ), v
    assert v["src/Main.scala#Idle"] == "dead" and v["src/Main.scala#Lone"] == "dead", v


def test_the_self_word_seam_adds_the_member_use_for_a_bare_name_the_code_does_not_bind():
    src = "class A\n  def go(arg)\n    arg + helper\n  end\nend\ndef top\n  helper\nend\n"
    use = pidx("ruby", "refs", "self", "name")
    loc = pidx("ruby", "refs", "local")
    cls, meth = pidx("ruby", "defs", "def.class"), pidx("ruby", "defs", "def.func")
    f = from_matches(
        "ruby",
        "m.rb",
        src,
        {
            "defs": [
                [cls, {"def.class": src.split("def top")[0].strip(), "name": "A"}],
                [meth, {"def.func": "def go(arg)\n    arg + helper\n  end", "name": "go"}],
                [meth, {"def.func": "def top\n  helper\nend", "name": "top"}],
            ],
            "refs": [
                [loc, {"local": "arg"}],
                [use, {"ref.ref": "arg#1", "name": "arg", "self": "arg"}],
                [use, {"ref.ref": "helper", "name": "helper", "self": "helper"}],
                [use, {"ref.ref": "helper#1", "name": "helper", "self": "helper"}],
            ],
        },
    )
    assert {(r.scope, r.name) for r in f.refs} == {("A.go", "helper"), ("A.go", "self.helper"), ("top", "helper")}
    # no parameter of A.go is a use, a def outside a type has no self; one with another word (scala) names its own


def test_a_roots_rule_with_host_roots_the_objects_around_the_main_method():
    src = "object Main {\n  def main(a: Int): Unit = ()\n}\n"
    obj, meth, root = (
        pidx("scala", "defs", "def.class"),
        pidx("scala", "defs", "def.func"),
        pidx("scala", "roots", "root.main"),
    )
    f = from_matches(
        "scala",
        "Main.scala",
        src,
        {
            "defs": [
                [obj, {"def.class": src.strip(), "name": "Main"}],
                [meth, {"def.func": "def main(a: Int): Unit = ()", "name": "main"}],
            ],
            "roots": [[root, {"root.main": "main"}]],
        },
    )
    assert {(h.kind, h.target) for h in f.roots} == {("main", "Main.main"), ("main", "Main")}


# ====================================================================== PHP callable arrays, Svelte directives, Lua pcall(require), html javascript: links
@pytest.mark.parametrize(
    "call",
    [
        "usort($a, [Sorter::class, 'cmp'])",
        'usort($a, [Sorter::class, "cmp"])',
        "call_user_func([new Sorter, 'cmp'])",
        "array_map(['Sorter', 'cmp'], $a)",
        "array_map([$obj, 'cmp'], $a)",
    ],
)
def test_a_php_callable_array_keeps_the_method_it_names_in_any_class(call):
    v = real_verdicts(
        ["php"],
        {
            "index.php": f"<?php\nrequire 'a.php';\nfunction run($a, $obj) {{ {call}; }}\nrun([], null);\n",
            "a.php": "<?php\nclass Sorter { static function cmp($a, $b) {} static function dead() {} }\n",
        },
    )
    assert v["a.php#Sorter.cmp"] in ALIVE and v["a.php#Sorter.dead"] == "dead", v


def test_a_php_include_with_double_quotes_is_an_edge_and_an_interpolated_one_is_not_a_name():
    v = real_verdicts(
        ["php"],
        {
            "index.php": '<?php\nrequire "a.php";\nrequire_once("b/$x.php");\n',
            "a.php": "<?php\nfunction fa() {}\nfa();\n",
            "b/c.php": "<?php\nfunction fb() {}\n",
        },
    )
    assert v["a.php"] in ALIVE and v["b/c.php"] not in ALIVE, v


@pytest.mark.parametrize(
    "stmt",
    [
        "include __DIR__ . '/../src/two.php';",
        'require_once __DIR__ . "/../src/two.php";',
        "require dirname(__FILE__) . '/../src/two.php';",
        "include_once(__DIR__ . '/../src/two.php');",
        "require '../src/two.php';",
    ],
)
def test_a_php_include_of_a_path_joined_to_the_directory_of_the_file_is_an_edge(stmt):
    """`include __DIR__ . '/../src/two.php'`: the literal starts with a slash because the directory of the file is joined in front
    (rules path_sub), so the file is found relative to the including file, and a file nothing includes stays dead."""
    v = real_verdicts(
        ["php"],
        {
            "public/index.php": f"<?php\n{stmt}\n",
            "src/two.php": "<?php\nfunction two_f() {}\ntwo_f();\n",
            "src/lone.php": "<?php\nfunction lone_f() {}\nlone_f();\n",
        },
    )
    assert (
        v["src/two.php"] in ALIVE and v["src/two.php#two_f"] in ALIVE
    ), v  # the file is included, its top-level code runs
    assert v["src/lone.php"] not in ALIVE, v


def test_a_svelte_directive_names_a_function_of_the_script():
    v = real_verdicts(
        ["svelte", "typescript"],
        {
            "src/routes/+page.svelte": '<script lang="ts">\n  function tip(n: Node) { return {}; }\n  function fly(n: Node) { return {}; }\n'
            "  function inA(n: Node) { return {}; }\n  function outB(n: Node) { return {}; }\n  function flip(n: Node) { return {}; }\n"
            "  let on = false;\n  function idle() {}\n</script>\n"
            "<div use:tip transition:fly|local={{ d: 1 }} in:inA out:outB animate:flip class:on>x</div>\n"
        },
    )
    assert all(v[f"src/routes/+page.svelte#{n}"] in ALIVE for n in ("tip", "fly", "inA", "outB", "flip")), v
    assert v["src/routes/+page.svelte#idle"] == "dead", v


def test_a_lua_module_loaded_by_pcall_require_is_an_edge():
    v = real_verdicts(
        ["lua"],
        {
            "init.lua": "local ok, m = pcall(require, 'lib.dynamic')\nlocal n = pcall(require, \"lib.other\")\nregister(handler, 'lib.none')\n",
            "lib/dynamic.lua": "local M = {}\nfunction M.f() end\nreturn M\n",
            "lib/other.lua": "local M = {}\nfunction M.g() end\nreturn M\n",
            "lib/none.lua": "local M = {}\nreturn M\n",
        },
    )
    assert v["lib/dynamic.lua"] in ALIVE and v["lib/other.lua"] in ALIVE and v["lib/none.lua"] not in ALIVE, v


@pytest.mark.parametrize("href", ['href="javascript:jj()"', "href=javascript:jj()", 'href="  javascript:jj(1)"'])
def test_a_javascript_link_calls_the_function_of_a_classic_script(href):
    v = real_verdicts(
        ["html", "javascript"],
        {
            "index.html": f'<script src="app.js"></script>\n<a {href}>x</a>\n<a href="app.html">y</a>\n',
            "app.js": "function jj() {}\nfunction html() {}\nfunction app() {}\n",
        },
    )
    assert v["app.js#jj"] in ALIVE, v
    assert v["app.js#html"] == v["app.js#app"] == "dead", v  # a file name is no script text


def test_scan_value_keeps_the_words_after_the_scheme_of_the_attribute_value_it_names():
    src = '<a href="javascript:go()">x</a><a href="about.html">y</a><a onclick="stop()">z</a>\n'
    p = pidx("html", "refs", "scan", "cmd")
    f = from_matches(
        "html",
        "a.html",
        src,
        {
            "refs": [
                [p, {"block": 'href="javascript:go()"', "cmd": "href", "scan": "javascript:go()"}],
                [p, {"block": 'href="about.html"', "cmd": "href", "scan": "about.html"}],
                [p, {"block": 'onclick="stop()"', "cmd": "onclick", "scan": "stop()"}],
            ]
        },
    )
    assert {(r.name, r.kind) for r in f.refs} == {("go", "call"), ("stop", "call")}


# ====================================================================== the shard key of a page covers the code it embeds
def test_the_shard_key_of_html_vue_and_svelte_covers_the_languages_of_their_script_blocks(monkeypatch):
    ex = contracts.get_extractor("html")
    langs = ("html", "vue", "svelte", "lua", "javascript", "typescript")
    base = {lang: ex.grammar_sha(lang) for lang in langs}
    orig = tg.queries_sha
    for edited, want in (
        ("typescript", {"vue", "svelte", "typescript"}),  # html has no TypeScript block: its <script> is JavaScript
        ("javascript", {"html", "vue", "svelte", "javascript"}),
    ):
        monkeypatch.setattr(tg, "queries_sha", lambda g, edited=edited: "x" if g == edited else orig(g))
        assert {lang for lang in langs if ex.grammar_sha(lang) != base[lang]} == want, edited  # Lua never
    monkeypatch.setattr(tg, "queries_sha", orig)
    monkeypatch.setattr(tg.tsm, "grammar_id", lambda: "tree-sitter==9|tree-sitter-language-pack==9")
    assert {lang for lang in langs if ex.grammar_sha(lang) != base[lang]} == set(langs)  # the installed versions decide every grammar's facts
    monkeypatch.setattr(tg, "_js_data", lambda: (None, None, None, None, "framework data v2", None))
    assert ex.grammar_sha("vue") != base["vue"]  # the framework data of the JS facts


def test_the_new_code_languages_have_no_engine_family_and_scala_and_groovy_link_with_the_jvm():
    for lang in ("lua", "ruby", "php", "scala", "groovy", "nix", "starlark"):
        assert LG.family(lang) is None, lang
    assert LG.info("scala").get("link_group") == "jvm" and LG.info("groovy").get("link_group") == "jvm"


# ====================================================================== implicit_receiver is a flag of a language, not of its link group
@pytest.mark.parametrize(
    "lang, ext, want", [("kotlin", "kt", "weak-alive"), ("java", "java", "dead"), ("scala", "scala", "dead")]
)
def test_a_bare_name_is_a_member_of_an_implicit_receiver_in_kotlin_only(lang, ext, want):
    """langs.json implicit_receiver (E1: `fun Foo.baz() { bar() }`) is set for Kotlin. Java and Scala share its link group (jvm) but have no
    implicit receiver (Scala resolves a bare name through self_word and its @local parameters), so a bare `bar` there never reaches
    the member `bar` of another class: the flag worked per link group before the merge and made `p` of `def go(p: Int)` use `A.p`.
    """
    from dead_code_checker.graph.model import Def, Facts, Ref

    one = Facts(
        lang,
        defs=(
            Def("Foo", "class", 1, 4, "public", 0, -1, ""),
            Def("Foo.bar", "method", 2, 2, "public", 0, -1, ""),
            Def("Foo.spare", "method", 3, 3, "public", 0, -1, ""),
            Def("main", "func", 5, 6, "public", 0, -1, ""),
        ),
        refs=(Ref("Foo", "ref", 6, "main"), Ref("bar", "call", 6, "main")),
        roots=(RootHint("main", "main", 5),),
    )
    res = dlink.link({f"A.{ext}": one}, {}, workers=1)
    rr = rc.analyze(res.graph)
    got = {n.key.split(":", 1)[1]: rr.verdict(n.key) for n in res.graph.nodes}
    assert got[f"A.{ext}#Foo.bar"] == want and got[f"A.{ext}#Foo.spare"] == "dead", got


# ====================================================================== Scala implicits: an import is the only trace of an implicit definition
SCALA_IMP = (
    "package app\nclass S\nobject Imp {\n  implicit val ord: Ordering[S] = null\n  implicit def toS(i: Int): S = new S\n"
    "  implicit class RichS(s: S) { def pp = 1 }\n  implicit object Reg\n  def plain = 1\n}\n"
    "object Orphan {\n  implicit val lost: Ordering[S] = null\n  implicit def lostConv(i: Int): S = new S\n}\n"
)
SCALA_MAIN = "object Main { def main(args: Array[String]): Unit = println(5) }\n"


@pytest.mark.parametrize(
    "imp",
    ["import app.Imp._", "import app.Imp.{ord, toS => cvt}", "import app.Imp.toS", "import app.Imp.{given, ord}"],
)
def test_a_scala_implicit_that_a_file_imports_is_not_dead_and_one_nobody_imports_is(imp):
    v = real_verdicts(["scala"], {"src/Imp.scala": SCALA_IMP, "src/Use.scala": f"package other\n{imp}\n{SCALA_MAIN}"})
    assert all(v[f"src/Imp.scala#Imp.{n}"] in ALIVE for n in ("ord", "toS", "RichS", "Reg")), v
    assert v["src/Imp.scala#Imp.plain"] == "dead", v  # not implicit: nothing names it
    assert v["src/Imp.scala#Orphan.lost"] == v["src/Imp.scala#Orphan.lostConv"] == "dead", v  # no file imports Orphan
    assert v["src/Imp.scala#Orphan"] == "dead", v


def test_a_scala_3_given_that_a_file_imports_is_not_dead():
    src = (
        "package app\nclass S\nobject Imp {\n  given g1: Ordering[S] = null\n  given Ordering[Int] = null\n"
        "  given conv: Conversion[Int, S] with { def apply(i: Int) = new S }\n  def plain = 1\n}\n"
    )
    v = real_verdicts(
        ["scala"], {"src/Imp.scala": src, "src/Use.scala": f"package other\nimport app.Imp.given\n{SCALA_MAIN}"}
    )
    assert v["src/Imp.scala#Imp.g1"] in ALIVE and v["src/Imp.scala#Imp.conv"] in ALIVE, v
    assert v["src/Imp.scala#Imp.plain"] == "dead", v


def test_a_scala_implicit_is_not_kept_alive_by_the_import_of_a_dead_file():
    v = real_verdicts(
        ["scala"],
        {
            "src/Imp.scala": SCALA_IMP,
            "src/Dead.scala": "package other\nimport app.Imp._\nobject Idle { def idle = 1 }\n",
            "src/Use.scala": f"package third\n{SCALA_MAIN}",
        },
    )
    assert v["src/Imp.scala#Imp.ord"] not in ALIVE and v["src/Imp.scala#Imp.toS"] not in ALIVE, v
