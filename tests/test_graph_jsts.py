"""JS/TS/TSX: the tree-sitter queries of data/queries/{javascript,typescript,tsx}, their mapping to Facts (graph.extract.ts_generic),
package.json roots (graph.roots NpmRoots) and the link of the result.

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_jsts.py

The mapping is tested through the backend seam (ts_generic.set_backend) with canned query matches: each canned match is what the query
of that pattern yields for the fixture text, written by hand from the node types the .scm files name, so it needs no grammar. The tests
with the REAL grammars skip with a reason when tree-sitter or the grammar is not installed.
"""

import json
import shutil
import subprocess
import re
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test

ROOT = Path(__file__).resolve().parent.parent
from dead_code_checker.graph import contracts, langs as dclangs, link as dlink, reach as rc, roots, ts as tsm  # noqa: E402
from dead_code_checker.graph.extract import ts_generic as tg  # noqa: E402
from graph_support import inline_units  # noqa: E402

FIX = (
    ROOT / "tests" / "fixtures" / "js"
)  # *.txt: a .js fixture would be a changed JS file of the gate (E until the grammar is installed)


@pytest.fixture(autouse=True)
def registered():
    """other dc test files reset the registry: the ts_generic extractor must be registered again."""
    import importlib

    if "javascript" not in {k for k in ("javascript",) if contracts._EXTRACTORS.get(k)}:
        importlib.reload(tg)
    if "npm" not in {r.name for r in contracts.root_extractors()}:
        importlib.reload(roots)
    yield
    tg.set_backend(None)


# ====================================================================== canned parser
class FNode(NamedTuple):
    start_byte: int
    end_byte: int
    start_point: tuple
    end_point: tuple


class Canned:
    """A parser double: .run(kind) answers with the matches the test registered; nodes are located by text."""

    def __init__(self, text: str, errors: int = 0):
        self.text, self.errors, self.m = text, errors, {k: [] for k in tg.QUERY_KINDS}

    def _node(self, a: int, b: int) -> FNode:
        pt = lambda o: (self.text.count("\n", 0, o), o - (self.text.rfind("\n", 0, o) + 1))  # noqa: E731
        return FNode(a, b, pt(a), pt(b))

    def nd(self, sub: str, ctx: str = "") -> FNode:
        """The node of `sub` inside the first occurrence of `ctx` (default: the first occurrence of sub)."""
        base = self.text.index(ctx) if ctx else 0
        i = self.text.index(sub, base)
        return self._node(i, i + len(sub))

    def sp(self, start: str, end: str) -> FNode:
        """From the first `start` to the end of the first `end` after it."""
        i = self.text.index(start)
        j = self.text.index(end, i) + len(end)
        return self._node(i, j)

    def add(self, kind: str, idx: int = 0, **caps):
        self.m[kind].append((idx, {k: v if isinstance(v, list) else [v] for k, v in caps.items()}))
        return self

    def run(self, kind):
        return self.m[kind]


def extract(text: str, parsed, lang="javascript", path="a.js"):
    tg.set_backend(lambda grammar, data: parsed)
    return tg.TsGeneric().extract(lang, path, text)


def canned_app_js(text: str) -> Canned:
    c = Canned(text)
    nd, sp = c.nd, c.sp
    imp = sp("import def", ";")
    c.add("imports", 0, **{"import": imp, "module": nd('"./lib/util.js"')})
    c.add("imports", 1, **{"import": imp, "default": nd("def"), "module": nd('"./lib/util.js"')})
    c.add("imports", 3, **{"import": imp, "name": nd("a", "{ a"), "module": nd('"./lib/util.js"')})
    c.add("imports", 3, **{"import": imp, "name": nd("b", "b as"), "alias": nd("bb"), "module": nd('"./lib/util.js"')})
    ns_ = sp("import * as", ";")
    c.add("imports", 0, **{"import": ns_, "module": nd('"./lib/ns.js"')})
    c.add("imports", 2, **{"import": ns_, "ns": nd("ns", "* as ns"), "module": nd('"./lib/ns.js"')})
    c.add("imports", 0, **{"import": sp('import "./side.js"', ";"), "module": nd('"./side.js"', 'import "./side')})
    fs_ = sp('require("fs")', ")")
    c.add("imports", 4, **{"import": fs_, "req": nd("require", 'require("fs")'), "module": nd('"fs"')})
    decl = sp("fs =", ")")
    c.add(
        "imports",
        5,
        **{"import": decl, "ns": nd("fs", "fs ="), "req": nd("require", 'require("fs")'), "module": nd('"fs"')},
    )
    cj = sp('require("./cjs.js")', ")")
    c.add("imports", 4, **{"import": cj, "req": nd("require", 'require("./cjs.js")'), "module": nd('"./cjs.js"')})
    dd = sp("{ p, q: qq } =", ")")
    c.add(
        "imports",
        6,
        **{
            "import": dd,
            "name": nd("p", "{ p"),
            "req": nd("require", 'require("./cjs.js")'),
            "module": nd('"./cjs.js"'),
        },
    )
    c.add(
        "imports",
        7,
        **{
            "import": dd,
            "name": nd("q", "q: qq"),
            "alias": nd("qq"),
            "req": nd("require", 'require("./cjs.js")'),
            "module": nd('"./cjs.js"'),
        },
    )
    # f("x") is no import
    c.add(
        "imports", 4, **{"import": nd("setTimeout(cb)"), "req": nd("setTimeout"), "module": nd("cb", "setTimeout(cb")}
    )
    re_ = sp("export { a2", ";")
    c.add("imports", 9, **{"reexport": re_, "name": nd("a2"), "alias": nd("renamed"), "module": nd('"./re.js"')})
    st = sp("export * from", ";")
    c.add("imports", 8, **{"reexport": st, "star": nd("*", "export *"), "module": nd('"./all.js"')})

    c.add("defs", 0, **{"def.func": sp("function used", "}"), "name": nd("used", "function used")})
    c.add("defs", 0, **{"def.func": sp("function helper", "}"), "name": nd("helper", "function helper")})
    c.add("defs", 4, **{"def.func": sp("arrow =", "}"), "name": nd("arrow", "arrow =")})
    c.add("defs", 2, **{"def.class": sp("class Widget", "\n}"), "name": nd("Widget", "class Widget")})
    c.add("defs", 3, **{"def.method": sp("#secret", "{}"), "name": nd("#secret", "  #secret")})
    c.add("defs", 3, **{"def.method": sp("render()", "}"), "name": nd("render", "render()")})
    c.add("defs", 0, **{"def.func": sp("function marked", "}"), "name": nd("marked", "function marked")})
    c.add(
        "stub",
        0,
        **{
            "stub.def": sp("function marked", "}"),
            "name": nd("marked", "function marked"),
            "body": nd("{}", "function marked() "),
        },
    )

    c.add("refs", 0, **{"ref.call": nd("helper()", "return helper()"), "name": nd("helper", "return helper()")})
    c.add("refs", 1, **{"ref.call": nd("ns.run()"), "name": nd("ns.run")})
    c.add("refs", 0, **{"ref.call": nd("def()", "return def()"), "name": nd("def", "return def()")})
    c.add("refs", 1, **{"ref.call": nd("this.#secret()"), "name": nd("this.#secret")})
    c.add("refs", 0, **{"ref.call": nd("setTimeout(cb)"), "name": nd("setTimeout")})
    c.add("refs", 8, **{"ref.ref": nd("(cb)"), "name": nd("cb", "setTimeout(cb")})
    c.add("refs", 13, **{"ref.inherit": nd("class Widget extends Base"), "name": nd("Base", "extends Base")})

    c.add("exports", 0, **{"export": sp("export function used", "}"), "name": nd("used", "export function used")})
    c.add("exports", 0, **{"export": sp("export default class", "\n}"), "name": nd("Widget", "export default class")})
    c.add(
        "exports", 4, **{"export": sp("export default class", "\n}"), "default": nd("Widget", "export default class")}
    )
    c.add(
        "exports", 2, **{"export": re_, "name": nd("a2"), "alias": nd("renamed")}
    )  # the statement is a re-export: skipped
    c.add("roots", 0, **{"root.marker": nd("// deadcode: root entry")})
    return c


def text_of(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def d_(f, qual):
    return next(x for x in f.defs if x.qual == qual)


# ====================================================================== javascript facts (canned matches)
def test_js_defs_kinds_visibility_and_stub():
    f = extract(text_of("app.js.txt"), canned_app_js(text_of("app.js.txt")))
    got = {d.qual: (d.kind, d.vis) for d in f.defs}
    assert got == {
        "": ("module", "public"),
        "used": ("func", "exported"),
        "helper": ("func", "private"),
        "arrow": ("func", "private"),
        "Widget": ("class", "exported"),
        "Widget.#secret": ("method", "private"),
        "Widget.render": ("method", "public"),
        "marked": ("func", "private"),
        "default": ("const", "exported"),
        "renamed": ("const", "exported"),
        "*": ("const", "exported"),  # `export * from "./all.js"`: stands for every export of that module (link)
    }
    assert d_(f, "marked").stub == "empty body" and not d_(f, "helper").stub
    assert (d_(f, "used").line0, d_(f, "Widget").line1) == (9, 15)
    assert [(r.kind, r.target) for r in f.roots] == [("manual", "marked")]


def test_js_refs_calls_weak_refs_and_inherit_scope():
    f = extract(text_of("app.js.txt"), canned_app_js(text_of("app.js.txt")))
    r = {(x.name, x.kind, x.scope) for x in f.refs}
    assert ("helper", "call", "used") in r and ("ns.run", "call", "helper") in r and ("def", "call", "helper") in r
    assert ("this.#secret", "call", "Widget.render") in r and ("cb", "ref", "Widget.render") in r
    assert ("Base", "inherit", "Widget") in r  # the scope of an inherit is the derived class
    assert ("Widget", "ref", "default") in r and (
        "__re_renamed.a2",
        "ref",
        "renamed",
    ) in r  # alias defs read their target


def test_js_imports_es_cjs_and_reexport():
    f = extract(text_of("app.js.txt"), canned_app_js(text_of("app.js.txt")))
    assert [(i.module, i.names, i.alias, i.line, i.scope) for i in f.imports] == [
        ("./all.js", ("*",), "", 7, ""),
        ("./cjs.js", (), "", 5, ""),  # the bare require call next to the declarator
        ("./cjs.js", ("p", "q as qq"), "", 5, ""),
        ("./lib/ns.js", (), "ns", 2, ""),
        ("./lib/util.js", ("default as def", "a", "b as bb"), "", 1, ""),
        (
            "./re.js",
            (),
            "__re_renamed",
            6,
            "renamed",
        ),  # a re-export is read through a hidden namespace, scoped to its alias
        ("./side.js", (), "", 3, ""),
        ("fs", (), "", 4, ""),
        ("fs", (), "fs", 4, ""),
    ]
    assert not any(i.module == "cb" for i in f.imports)  # setTimeout(cb) is a call, not a require


def test_js_cjs_exports_and_text_filters():
    text = text_of("cjs.js.txt")
    c = Canned(text)
    nd, sp = c.nd, c.sp
    for name in ("run", "helper", "other", "lone"):
        c.add("defs", 0, **{"def.func": sp(f"function {name}", "}"), "name": nd(name, f"function {name}")})
    asg = lambda ctx: sp(ctx, ";")  # noqa: E731
    c.add(
        "exports",
        5,
        **{
            "export": asg("exports.helper"),
            "obj": nd("exports", "exports.helper"),
            "prop": nd("helper", "exports.helper"),
            "default": nd("helper", "= helper"),
        },
    )
    mod = asg("module.exports")
    c.add(
        "exports",
        6,
        **{"export": mod, "obj": nd("module"), "prop": nd("exports", "module.exports"), "name": nd("run", "{ run")},
    )
    c.add(
        "exports",
        7,
        **{
            "export": mod,
            "obj": nd("module"),
            "prop": nd("exports", "module.exports"),
            "alias": nd("alias"),
            "name": nd("other", "alias: other"),
        },
    )
    c.add(
        "exports", 5, **{"export": mod, "obj": nd("util"), "prop": nd("go"), "default": nd("util", "return util")}
    )  # not exports.x: ignored
    f = extract(text, c)
    assert {d.qual: d.vis for d in f.defs} == {
        "": "public",
        "run": "exported",
        "helper": "exported",
        "other": "exported",
        "lone": "private",
        "alias": "exported",
    }
    assert ("other", "ref", "alias") in {(r.name, r.kind, r.scope) for r in f.refs}
    assert d_(f, "alias").kind == "const"


def test_js_name_filters_optional_chaining_calls_on_calls_computed_keys():
    text = "function f() { a?.b(); g()(); x[i](); }\nclass K { ['k']() {} m() {} }\n"
    c = Canned(text)
    nd = c.nd
    c.add("defs", 0, **{"def.func": c.sp("function f", "}"), "name": nd("f", "function f")})
    c.add("defs", 0, **{"def.class": c.sp("class K", "}\n"), "name": nd("K", "class K")})
    c.add("defs", 3, **{"def.method": nd("['k']() {}"), "name": nd("['k']")})  # a computed key is no name
    c.add("defs", 3, **{"def.method": nd("m() {}"), "name": nd("m", "m() {}")})
    c.add("refs", 1, **{"ref.call": nd("a?.b()"), "name": nd("a?.b")})
    c.add("refs", 0, **{"ref.call": nd("g()()"), "name": nd("g()()")})  # the callee text is a call: dropped
    c.add("refs", 14, dyn=nd("x[i]"))
    f = extract(text, c)
    assert {d.qual for d in f.defs} == {"", "f", "K", "K.m"}
    assert [(r.name, r.kind) for r in f.refs] == [("a.b", "call")]
    assert [(x.pattern, x.scope) for x in f.dyn] == [("x[i]", "f")]


# ====================================================================== typescript and tsx
def test_ts_interfaces_enums_implements_and_type_refs():
    text = text_of("app.ts.txt")
    c = Canned(text)
    nd, sp = c.nd, c.sp
    c.add("defs", 2, **{"def.iface": sp("interface Named", "}"), "name": nd("Named", "interface Named")})
    c.add("defs", 1, **{"def.class": sp("abstract class Base", "\n}"), "name": nd("Base", "abstract class Base")})
    c.add("defs", 0, **{"def.class": sp("class Impl", "\n}"), "name": nd("Impl", "class Impl")})
    c.add("defs", 4, **{"def.method": sp("run(", "}"), "name": nd("run", "  run(")})
    c.add("defs", 4, **{"def.method": sp("private make(", "}"), "name": nd("make", "private make")})
    c.add("defs", 3, **{"def.class": sp("enum Mode", "}"), "name": nd("Mode", "enum Mode")})
    c.add("defs", 4, **{"def.func": sp("mk =", "Impl()"), "name": nd("mk", "mk =")})
    c.add("refs", 17, **{"ref.impl": nd("Named", "implements Named"), "name": nd("Named", "implements Named")})
    c.add("refs", 15, **{"ref.inherit": nd("class Impl extends Base"), "name": nd("Base", "extends Base")})
    c.add("refs", 18, **{"ref.ref": nd("Shape", "run(a: Shape"), "name": nd("Shape", "run(a: Shape")})
    c.add("refs", 3, **{"ref.call": nd("new Impl()"), "name": nd("Impl", "new Impl()")})
    c.add("refs", 1, **{"ref.call": nd("this.make(a)"), "name": nd("this.make")})
    c.add("imports", 3, **{"import": sp("import type", ";"), "name": nd("Shape", "{ Shape"), "module": nd('"./shape"')})
    for n, ctx in (
        ("Named", "export interface"),
        ("Base", "abstract class"),
        ("Impl", "export class Impl"),
        ("Mode", "export enum"),
        ("mk", "export const mk"),
    ):
        c.add("exports", 0, **{"export": nd(ctx), "name": nd(n, ctx)})
    f = extract(text, c, "typescript", "a.ts")
    assert {d.qual: (d.kind, d.vis) for d in f.defs} == {
        "": ("module", "public"),
        "Named": ("iface", "exported"),
        "Base": ("class", "exported"),
        "Impl": ("class", "exported"),
        "Impl.run": ("method", "public"),
        "Impl.make": ("method", "public"),
        "Mode": ("class", "exported"),
        "mk": ("func", "exported"),
    }
    r = {(x.name, x.kind, x.scope) for x in f.refs}
    assert ("Named", "impl", "Base") in r and ("Base", "inherit", "Impl") in r and ("Impl", "call", "mk") in r
    assert ("Shape", "ref", "Impl.run") in r and ("this.make", "call", "Impl.run") in r
    assert [(i.module, i.names) for i in f.imports] == [("./shape", ("Shape",))]


def test_tsx_extractor_is_registered_and_shares_the_typescript_queries():
    for lang in ("javascript", "typescript", "tsx"):
        assert contracts.get_extractor(lang).name == "ts_generic"
    assert dclangs.load().query_alias == {
        "tsx": "typescript",
        "terraform": "hcl",
    }  # (terraform: the hcl queries)
    ts_files = [Path(p).parent.name for p in tg._files("typescript", "defs")]
    assert ts_files == ["typescript"] and [Path(p).parent.name for p in tg._files("tsx", "defs")] == ["typescript"]
    assert [Path(p).parent.name for p in tg._files("tsx", "refs")] == [
        "typescript",
        "tsx",
    ]  # the alias first, then the own file
    assert "jsx_self_closing_element" in tg._query_text("tsx", "refs") and "jsx" not in tg._query_text(
        "typescript", "refs"
    )
    assert (
        tg._queries("tsx")
        == tg._queries("typescript")
        == ["defs", "exports", "imports", "refs", "roots", "stub", "xlang"]
    )
    assert tg.queries_sha("tsx") != tg.queries_sha("typescript")  # the shard of a tsx file follows its own queries


# ====================================================================== queries as data
GRAMMARS = ("javascript", "typescript", "tsx")
ALLOWED = {
    "defs": {"def.func", "def.class", "def.method", "def.iface", "name", "deco"},
    "refs": {"ref.call", "ref.ref", "ref.inherit", "ref.impl", "name", "dyn", "mem"},
    "roots": {
        "root.marker",
        "root.main",
    },  # root.main-toplevel / root.main-guard: the statements of a script (rules.json)
    "stub": {"stub.def", "name", "body"},
    "imports": {"import", "reexport", "module", "name", "alias", "default", "ns", "star", "req", "pattern", "await"},
    "exports": {"export", "name", "alias", "default", "obj", "prop", "key", "fn"},
}


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_js_queries_use_the_documented_vocabulary(grammar):
    for kind, ok in ALLOWED.items():
        files = tg._files(grammar, kind)
        assert files, (grammar, kind)
        for path in files:
            text = re.sub(r";[^\n]*", "", Path(path).read_text(encoding="utf-8"))
            assert text.count("(") == text.count(")") and text.count("[") == text.count("]"), path
            used = set(re.findall(r"@([A-Za-z_.]+)", text))
            assert used and used <= ok, (path, used - ok)
            assert "#" not in re.sub(r'"[^"]*"', "", text), "queries carry no predicates"


def test_js_queries_do_not_name_a_node_the_grammar_may_lack():
    """Node names that differ between grammar versions or between javascript and typescript stay out of the shared files."""
    js, ts = tg._query_text("javascript", "defs") + tg._query_text("javascript", "refs"), tg._query_text(
        "typescript", "refs"
    )
    for bad in ("function_expression", "(function)", "type_identifier", "extends_clause"):
        assert bad not in js, bad
    for bad in ("jsx_", "class_heritage (identifier)"):
        assert bad not in ts, bad
    assert "(class_heritage (extends_clause" in ts and "(class_heritage (identifier)" in tg._query_text(
        "javascript", "refs"
    )


def test_js_extractor_without_grammar_is_extractor_missing_never_a_fallback(no_tree_sitter):
    tg.set_backend(None)
    for lang in ("javascript", "typescript", "tsx"):
        with pytest.raises(contracts.ExtractorMissing) as ei:
            contracts.get_extractor(lang).extract(lang, "a.x", "export const a = 1;\n")
        assert ei.value.lang == lang and "tree-sitter is not installed" in ei.value.reason
        f = tg.missing_finding(lang, "a.x")
        assert (f.level, f.code) == ("W", "dc.degraded-extractor")


def _real_grammar() -> str:
    try:
        tsm.get_parser("javascript")
        tsm.get_parser("typescript")
        return ""
    except Exception as exc:  # LookupError, or the venv is missing
        return str(exc) or type(exc).__name__


@pytest.mark.skipif(
    bool(_real_grammar()),
    reason=f"tree-sitter javascript/typescript grammars are not installed on this host: {_real_grammar()}",
)
def test_js_and_ts_with_the_real_grammars():
    """On a host with the grammars: a query that does not compile or a changed capture fails here, not in the gate."""
    tg.set_backend(None)
    f = contracts.get_extractor("javascript").extract("javascript", "app.js", text_of("app.js.txt"))
    assert {"used", "helper", "arrow", "Widget", "Widget.render", "marked"} <= {
        d.qual for d in f.defs
    } and f.errors == 0
    assert ("Base", "inherit", "Widget") in {(r.name, r.kind, r.scope) for r in f.refs}
    assert any(i.module == "./lib/util.js" and "default as def" in i.names for i in f.imports)
    t = contracts.get_extractor("typescript").extract("typescript", "app.ts", text_of("app.ts.txt"))
    assert {"Named", "Base", "Impl", "Impl.run", "Mode", "mk"} <= {d.qual for d in t.defs} and t.errors == 0
    assert ("Named", "impl", "Base") in {(r.name, r.kind, r.scope) for r in t.refs}


# ====================================================================== link: the facts reach the right defs
def util_facts() -> "tg.Facts":
    text = "export function a() {}\nexport default function def() {}\nexport function unused() {}\n"
    c = Canned(text)
    for n in ("a", "def", "unused"):
        c.add("defs", 0, **{"def.func": c.sp(f"function {n}", "}"), "name": c.nd(n, f"function {n}")})
    for n, ctx in (
        ("a", "export function a"),
        ("unused", "export function unused"),
        ("def", "export default function"),
    ):
        c.add("exports", 0, **{"export": c.nd(ctx), "name": c.nd(n, ctx)})
    c.add(
        "exports", 4, **{"export": c.nd("export default function"), "default": c.nd("def", "export default function")}
    )
    return extract(text, c, path="lib/util.js")


def simple_facts(text: str, defs=(), refs=(), imports=(), exports=()) -> "tg.Facts":
    c = Canned(text)
    for n in defs:
        c.add("defs", 0, **{"def.func": c.sp(f"function {n}", "}"), "name": c.nd(n, f"function {n}")})
    for n, ctx in exports:
        c.add("exports", 0, **{"export": c.nd(ctx), "name": c.nd(n, ctx)})
    for kind, ctx, name in refs:
        c.add("refs", 0, **{f"ref.{kind}": c.nd(ctx), "name": c.nd(name, ctx)})
    for stmt, mod, names in imports:
        for nm, al in names or [("", "")]:
            caps = {"import": c.nd(stmt), "module": c.nd(mod, stmt)}
            if nm:
                caps["name"] = c.nd(nm, stmt)
            if al:
                caps["alias"] = c.nd(al, stmt)
            c.add("imports", 0, **caps)
    return extract(text, c)


def test_link_default_import_named_import_reexport_and_dead_export():
    app_text = text_of("app.js.txt")
    files = {
        "app.js": extract(app_text, canned_app_js(app_text)),
        "lib/util.js": util_facts(),
        "re.js": simple_facts(
            "export function a2() {}\nexport function lonely() {}\n",
            ["a2", "lonely"],
            exports=[("a2", "export function a2"), ("lonely", "export function lonely")],
        ),
        "use.js": simple_facts(
            text_of("use.js.txt"),
            refs=[("call", "renamed()", "renamed")],
            imports=[("import { renamed }", '"./app.js"', [("renamed", "")])],
        ),
    }
    for p in files:
        assert files[p].lang == "javascript"
    res = rc.analyze(dlink.link(files, {"(roots)": [roots.RootHint("main", "file:use.js", 1)]}, workers=1).graph)
    v = {k.split(":", 1)[1]: x for k, x in res.verdicts().items()}
    live = {"alive", "weak-alive"}  # a name imported by name is an N edge: weak-alive
    assert {v[k] in live for k in ("lib/util.js#a", "lib/util.js#def", "lib/util.js#default")} == {True}
    assert v["lib/util.js#unused"] == "dead"  # exported, imported by nobody
    assert v["app.js#renamed"] in live and v["re.js#a2"] in live  # use.js -> renamed -> __re_renamed.a2 -> re.js
    assert v["re.js#lonely"] == "dead"
    assert v["app.js#marked"] == "alive"  # `// deadcode: root` is a manual root
    assert v["app.js#helper"] == "dead-cluster" and v["app.js#arrow"] == "dead"  # nothing reaches them from use.js


# ====================================================================== package.json roots
PKG = """{
  "name": "p",
  "main": "lib/index.js",
  "module": "./esm/index.mjs",
  "bin": {"tool": "bin/tool.js", "other": "./bin/other.js"},
  "exports": {".": {"import": "./esm/index.mjs", "require": "./cjs/index.cjs"}, "./sub/*": "./s/*.js", "./package.json": "./package.json"},
  "browser": {"fs": false},
  "scripts": {"start": "node server.js"}
}
"""


def test_npm_roots_main_bin_exports_conditions():
    h = {(x.kind, x.target, x.line) for x in roots.extract_hints("pkg/package.json", PKG)}
    plain = {
        (k, t.removeprefix("sibling:"), ln)
        for k, t, ln in h
        if not t.startswith(("sibling:./lib/", "sibling:./esm/", "sibling:./cjs/", "sibling:./s/"))
        or t.endswith((".js", ".mjs", ".cjs"))
    }  # noqa: E501
    assert {
        ("export", "./lib/index.js", 3),
        ("export", "./esm/index.mjs", 4),
        ("export", "./cjs/index.cjs", 6),
        ("export", "./s/*.js", 6),  # a subpath pattern: every file it matches (resolve)
        ("export", "./server.js", 8),  # scripts: `node server.js`
        ("export", "./package.json", 6),
        ("bin", "./bin/tool.js", 5),
        ("bin", "./bin/other.js", 5),
    } <= plain
    # main, module and exports name build outputs: the sources they are built from are hints too (silent when absent)
    src = {t.removeprefix("sibling:./src/index") for _, t, _ in h if t.startswith("sibling:./src/index")}
    assert src == {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
    assert ("export", "sibling:./s/*.js", 6) in h and ("export", "sibling:./src/*.ts", 6) not in h  # ./s is no out dir
    assert all(x.trust == "registered" for x in roots.extract_hints("package.json", PKG))
    assert "sibling:./cli.js" in {h.target for h in roots.extract_hints("package.json", '{"bin": "cli.js"}')}
    # a package.json that is no JSON object is a bad source (W dc.bad-root-source at resolve), not silence and not a crash
    for broken in ("{not json", "[]"):
        assert [h.kind for h in roots.extract_hints("package.json", broken)] == ["bad-source"]
    assert (
        roots.extract_hints("lib/package-lock.json", PKG) == [] and roots.extract_hints("a/package.json.bak", PKG) == []
    )


def test_npm_roots_resolve_against_the_tree_and_a_missing_build_output_is_silent():
    hints = {"pkg/package.json": roots.extract_hints("pkg/package.json", PKG)}
    files = {
        "pkg/package.json": "100644",
        "pkg/lib/index.js": "100644",
        "pkg/bin/tool.js": "100755",
        "pkg/esm/index.mjs": "100644",
    }
    rs = roots.resolve(hints, files, lambda p: "")
    got = {(r.kind, r.node) for r in rs.roots if r.kind in ("export", "bin")}
    assert ("export", "javascript:pkg/lib/index.js") in got and ("bin", "javascript:pkg/bin/tool.js") in got
    assert ("export", "javascript:pkg/esm/index.mjs") in got
    assert not [
        f for f in rs.findings if f.code == "dc.root-dangling"
    ], "dist/ is built: a missing file is no finding"  # cjs/ and other.js are absent


def test_npm_roots_keep_a_package_entry_alive_in_the_graph():
    """The composition of levels.py: link the facts, resolve the hints against the tree, add the roots to the graph."""
    from dead_code_checker.graph.model import canonical

    hints = {"package.json": roots.extract_hints("package.json", '{"main": "src/api.js"}')}
    api = simple_facts("export function go() {}\n", ["go"], exports=[("go", "export function go")])
    files = {"src/api.js": api, "src/dead.js": simple_facts("function z() {}\n", ["z"])}
    rs = roots.resolve(hints, {"package.json": "100644", "src/api.js": "100644", "src/dead.js": "100644"}, lambda p: "")
    lk = dlink.link(files, workers=1)
    g = canonical(lk.graph.nodes, lk.graph.edges, tuple(lk.graph.roots) + tuple(rs.roots), lk.graph.evid)
    for mode in ("library", "closed_world"):  # the entry of a package is used by its consumers, in either mode
        v = {k.split(":", 1)[1]: x for k, x in rc.analyze(g, mode).verdicts().items()}
        assert v["src/api.js"] == "alive" and v["src/api.js#go"] == "alive", mode
    assert v["src/dead.js"] == "dead" and v["src/dead.js#z"] == "dead"


def test_npm_roots_types_source_scripts_and_dist_to_src():
    pkg = '{"main": "dist/index.js", "types": "./dist/index.d.ts", "source": "src/main.ts", "scripts": {"a": "tsx src/cli.ts", "b": "echo hi", "c": "node /abs/x.js"}}'
    h = {(x.target, x.strength) for x in roots.extract_hints("package.json", pkg)}
    assert ("sibling:./src/index.ts", "hard") in h and ("sibling:./src/main.ts", "hard") in h
    assert ("sibling:./src/cli.ts", "soft") in h and not [t for t, _ in h if "abs" in t or "hi" in t]
    files = {"package.json": "100644", "src/index.ts": "100644", "src/lib.ts": "100644"}
    rs = roots.resolve({"package.json": roots.extract_hints("package.json", pkg)}, files, lambda p: "")
    assert {r.node for r in rs.roots if r.kind == "export"} == {"typescript:src/index.ts"} and not rs.findings


# ====================================================================== fix round 1: verdicts of the link on the facts
def _v(files, hints=("use.js",), g=None):
    rt = [roots.RootHint("main", f"file:{p}", 1) for p in hints]
    res = rc.analyze(dlink.link(files, {"(roots)": rt}, workers=1).graph)
    return {k.split(":", 1)[1]: x for k, x in res.verdicts().items()}


LIVE = {"alive", "weak-alive"}


def test_constructor_is_a_protocol_member_of_a_live_class():
    text = "class C { constructor() {} m() {} }\nnew C();\n"
    c = Canned(text)
    c.add("defs", 0, **{"def.class": c.sp("class C", "}\n"), "name": c.nd("C", "class C")})
    c.add("defs", 3, **{"def.method": c.nd("constructor() {}"), "name": c.nd("constructor")})
    c.add("defs", 3, **{"def.method": c.nd("m() {}"), "name": c.nd("m", "m() {}")})
    c.add("refs", 2, **{"ref.call": c.nd("new C()"), "name": c.nd("C", "new C()")})
    v = _v({"use.js": extract(text, c, path="use.js")})
    assert v["use.js#C"] == "alive" and v["use.js#C.constructor"] in LIVE


def test_export_star_barrel_import_by_name_and_renamed():
    util = simple_facts("export function foo() {}\n", ["foo"], exports=[("foo", "export function foo")])
    util = util._replace(lang="typescript")
    barrel = simple_facts("export * from './util';\n", imports=[("export * from './util'", "'./util'", [("*", "")])])
    for name, text, caps in (
        ("foo", "import { foo } from './index'; foo();\n", [("call", "foo()", "foo")]),
        ("f", "import { foo as f } from './index'; f();\n", [("call", "f()", "f")]),
    ):
        imp = [("import {", "'./index'", [("foo", "f" if name == "f" else "")])]
        use = simple_facts(text, refs=caps, imports=imp)
        v = _v({"util.js": util._replace(lang="javascript"), "index.js": barrel, "use.js": use})
        assert v["util.js#foo"] in LIVE, name


def test_export_star_chain_of_barrels_and_default_is_not_re_exported():
    util = simple_facts("export function foo() {}\nexport function dflt() {}\n", ["foo", "dflt"])
    def star(m):
        return simple_facts(
            f"export * from '{m}';\n", imports=[(f"export * from '{m}'", f"'{m}'", [("*", "")])]
        )  # noqa: E731
    use = simple_facts(
        "import { foo } from './b'; foo();\n",
        refs=[("call", "foo()", "foo")],
        imports=[("import {", "'./b'", [("foo", "")])],
    )
    v = _v({"util.js": util, "a.js": star("./util"), "b.js": star("./a"), "use.js": use})
    assert v["util.js#foo"] in LIVE and v["util.js#dflt"] == "dead"


def test_cjs_module_exports_default_function_reached_through_the_required_alias():
    text = "function foo() {}\nmodule.exports = foo;\n"
    c = Canned(text)
    c.add("defs", 0, **{"def.func": c.sp("function foo", "}"), "name": c.nd("foo", "function foo")})
    c.add(
        "exports",
        6,
        **{
            "export": c.sp("module.exports", "foo"),
            "obj": c.nd("module"),
            "prop": c.nd("exports"),
            "default": c.nd("foo", "= foo"),
        },
    )
    util = extract(text, c, path="util.js")
    c2 = Canned("const fn = require('./util'); fn();\n")
    c2.add(
        "imports",
        5,
        **{
            "import": c2.sp("fn =", ")"),
            "ns": c2.nd("fn", "fn ="),
            "req": c2.nd("require"),
            "module": c2.nd("'./util'"),
        },
    )
    c2.add("refs", 0, **{"ref.call": c2.nd("fn()"), "name": c2.nd("fn", "fn()")})
    use = extract(c2.text, c2, path="use.js")
    v = _v({"util.js": util, "use.js": use})
    assert v["util.js#foo"] in LIVE and v["util.js#default"] in LIVE


def test_non_plain_receivers_keep_the_member_name():
    cases = {
        "new App().start": "~.start",
        "getX().doY": "~.doY",
        "a.b().c": "~.c",
        "(x as T).m": "~.m",
        "x!.m": "~.m",
        "a[i].b": "~.b",
        "foo\n  .bar": "foo.bar",
        "fetch(u)\n  .then": "~.then",
        "a?.b": "a.b",
        "g()()": "",
    }
    assert {k: tg._js_name(k) for k in cases} == cases
    text = "class App { start() {} }\nnew App().start();\n"
    c = Canned(text)
    c.add("defs", 0, **{"def.class": c.sp("class App", "}\n"), "name": c.nd("App", "class App")})
    c.add("defs", 3, **{"def.method": c.nd("start() {}"), "name": c.nd("start")})
    c.add("refs", 2, **{"ref.call": c.nd("new App()"), "name": c.nd("App", "new App()")})
    c.add("refs", 1, **{"ref.call": c.nd("new App().start()"), "name": c.nd("new App().start")})
    f = extract(text, c, path="use.js")
    assert ("~.start", "call") in {(r.name, r.kind) for r in f.refs}
    assert _v({"use.js": f})["use.js#App.start"] in LIVE


def test_member_values_are_weak_refs_but_callees_and_inner_objects_are_not():
    text = "class H { handle() {} get value() {} }\nfunction f(emitter, h) { emitter.on('x', this.handle); h.a.b(); return o.value; }\n"
    c = Canned(text)
    c.add("defs", 0, **{"def.func": c.sp("function f", "}\n"), "name": c.nd("f", "function f")})
    for sub, nm in (
        ("this.handle", "this.handle"),
        ("h.a.b", "h.a.b"),
        ("h.a", "h.a"),
        ("o.value", "o.value"),
        ("emitter.on", "emitter.on"),
    ):
        c.add("refs", 9, **{"ref.ref": c.nd(sub), "name": c.nd(nm), "mem": c.nd(sub)})
    c.add("refs", 1, **{"ref.call": c.nd("emitter.on"), "name": c.nd("emitter.on")})
    c.add("refs", 1, **{"ref.call": c.nd("h.a.b"), "name": c.nd("h.a.b")})
    f = extract(text, c)
    got = {(r.name, r.kind) for r in f.refs}
    assert ("this.handle", "ref") in got and ("o.value", "ref") in got
    assert ("h.a", "ref") not in got and ("h.a.b", "ref") not in got and ("emitter.on", "ref") not in got
    assert {("emitter.on", "call"), ("h.a.b", "call")} <= got


def test_declaration_name_is_no_self_reference_and_unused_interface_is_dead():
    text = "interface Foo {}\nclass Bar {}\n"
    c = Canned(text)
    c.add("defs", 0, **{"def.iface": c.sp("interface Foo", "{}"), "name": c.nd("Foo")})
    c.add("defs", 0, **{"def.class": c.sp("class Bar", "{}"), "name": c.nd("Bar")})
    c.add(
        "refs", 0, **{"ref.ref": c.nd("Foo"), "name": c.nd("Foo")}
    )  # (type_identifier) matches the declaration name too
    c.add("refs", 0, **{"ref.ref": c.nd("Bar"), "name": c.nd("Bar")})
    f = extract(text, c, "typescript", "t.ts")
    assert not f.refs
    assert {k: x for k, x in _v({"use.ts": f}, hints=()).items() if "#" in k} == {
        "use.ts#Foo": "dead",
        "use.ts#Bar": "dead",
    }


def test_export_as_default_and_the_vocabulary_of_new_patterns():
    text = "function a() {}\nexport { a as default };\n"
    c = Canned(text)
    c.add("defs", 0, **{"def.func": c.sp("function a", "}"), "name": c.nd("a", "function a")})
    c.add("exports", 2, **{"export": c.sp("export {", ";"), "name": c.nd("a", "{ a"), "alias": c.nd("default")})
    f = extract(text, c)
    assert d_(f, "default").vis == "exported" and ("a", "ref", "default") in {(r.name, r.kind, r.scope) for r in f.refs}
    assert d_(f, "a").vis == "exported"


# ====================================================================== fix round 2
def _ts(f):
    return f._replace(lang="typescript")


def _app(spec, name="Button"):
    return simple_facts(
        f"import {{ {name} }} from '{spec}'; {name}();\n",
        refs=[("call", f"{name}()", name)],
        imports=[("import {", f"'{spec}'", [(name, "")])],
    )


@pytest.mark.parametrize(
    "spec",
    [
        "./components/Button",
        "components/Button",
        "@/components/Button",
        "~/components/Button",
        "#internal/components/Button",
    ],
)
def test_alias_and_base_url_imports_reach_the_file(spec):
    """No tsconfig is read: the name after an alias marker is looked up as a path suffix, never `external`."""
    btn = _ts(simple_facts("export function Button() {}\n", ["Button"], exports=[("Button", "export function Button")]))
    v = _v({"src/components/Button.tsx": btn, "src/app.ts": _ts(_app(spec))}, hints=("src/app.ts",))
    assert v["src/components/Button.tsx#Button"] in LIVE, spec


def test_scoped_workspace_package_resolves_to_its_directory_and_subpath():
    ui = _ts(simple_facts("export function Card() {}\n", ["Card"], exports=[("Card", "export function Card")]))
    files = {
        "packages/ui/index.ts": ui,
        "packages/ui/button.ts": _ts(
            simple_facts("export function B() {}\n", ["B"], exports=[("B", "export function B")])
        ),
    }
    for spec, name, key in (
        ("@org/ui", "Card", "packages/ui/index.ts#Card"),
        ("@org/ui/button", "B", "packages/ui/button.ts#B"),
    ):
        v = _v({**files, "app.ts": _ts(_app(spec, name))}, hints=("app.ts",))
        assert v[key] in LIVE, spec


def test_an_unresolvable_package_is_still_external():
    btn = _ts(simple_facts("export function Button() {}\n", ["Button"], exports=[("Button", "export function Button")]))
    for spec in ("react", "@types/react", "lodash/get"):
        v = _v({"src/components/Button.tsx": btn, "app.ts": _ts(_app(spec))}, hints=("app.ts",))
        assert v["src/components/Button.tsx#Button"] == "dead", spec


def test_object_literal_method_is_no_def_and_its_body_belongs_to_the_enclosing_scope():
    text = "function main() { const handlers = { onClick() { helper(); } }; handlers.onClick(); }\nfunction helper() {}\nmain();\n"
    c = Canned(text)
    c.add(
        "defs",
        0,
        **{"def.func": c.sp("function main", "}; handlers.onClick(); }"), "name": c.nd("main", "function main")},
    )
    c.add(
        "defs", 3, **{"def.method": c.nd("onClick() { helper(); }"), "name": c.nd("onClick")}
    )  # what a loose pattern yields
    c.add("defs", 0, **{"def.func": c.sp("function helper", "}"), "name": c.nd("helper", "function helper")})
    c.add("refs", 0, **{"ref.call": c.nd("helper()", "onClick() {"), "name": c.nd("helper", "onClick() {")})
    c.add("refs", 1, **{"ref.call": c.nd("handlers.onClick()"), "name": c.nd("handlers.onClick")})
    c.add("refs", 0, **{"ref.call": c.nd("main()", "}\nmain"), "name": c.nd("main", "}\nmain")})
    f = extract(text, c, path="use.js")
    assert {d.qual for d in f.defs} == {"", "main", "helper"}
    assert ("helper", "call", "main") in {(r.name, r.kind, r.scope) for r in f.refs}
    v = _v({"use.js": f})
    assert v["use.js#main"] == "alive" and v["use.js#helper"] in LIVE and "use.js#onClick" not in v


@pytest.mark.parametrize("lang", ["javascript", "typescript"])
def test_method_queries_match_a_class_body_only(lang):
    text = tg._query_text(lang, "defs")
    assert "(class_body (method_definition name: (_) @name) @def.method)" in text
    # a decorated member: javascript has the decorator inside the method, typescript has it as a sibling in the class body
    text = text.replace("(class_body (decorator)+ @deco . (method_definition", "(class_body (method_definition")
    assert not re.search(r"(?<!class_body )\(method_definition", text)


def _graph(files, pkg, tree):
    """levels.py composition: link, resolve the root hints of package.json against `tree`, put the roots into the graph."""
    from dead_code_checker.graph.model import canonical

    hints = {p: roots.extract_hints(p, t) for p, t in pkg.items()}
    rs = roots.resolve(hints, {p: "100644" for p in tree}, lambda p: "")
    lk = dlink.link(files, workers=1)
    g = canonical(lk.graph.nodes, lk.graph.edges, tuple(lk.graph.roots) + tuple(rs.roots), lk.graph.evid)
    return {k.split(":", 1)[1]: x for k, x in rc.analyze(g).verdicts().items()}, rs


def _barrel():
    text = "export * from './a';\nexport { b } from './b';\n"
    c = Canned(text)
    c.add("imports", 8, **{"reexport": c.sp("export * from", ";"), "star": c.nd("*"), "module": c.nd("'./a'")})
    c.add("imports", 9, **{"reexport": c.sp("export { b", ";"), "name": c.nd("b", "{ b"), "module": c.nd("'./b'")})
    return _ts(extract(text, c, path="src/index.ts"))


def _lib_files():
    def exp(n):
        return _ts(
            simple_facts(
                f"export function {n}() {{}}\nexport function {n}Unused() {{}}\n",
                [n, n + "Unused"],
                exports=[(n, f"export function {n}()"), (n + "Unused", f"export function {n}Unused")],
            )
        )  # noqa: E731,E501
    return {"src/index.ts": _barrel(), "src/a.ts": exp("api"), "src/b.ts": exp("b")}


def test_package_entry_keeps_its_exported_api_alive_through_a_barrel():
    files = _lib_files()
    v, _ = _graph(files, {"package.json": '{"main": "src/index.ts"}'}, ["package.json", *files])
    assert v["src/a.ts#api"] == "alive" and v["src/a.ts#apiUnused"] == "alive"  # export * from './a': the whole module
    assert v["src/b.ts#b"] in LIVE and v["src/index.ts#b"] == "alive"  # export { b } from './b': only the named one
    assert v["src/b.ts#bUnused"] == "dead"


def test_a_barrel_that_is_only_imported_does_not_keep_every_export_alive():
    use = simple_facts(
        "import { api } from './src/index'; api();\n",
        refs=[("call", "api()", "api")],
        imports=[("import {", "'./src/index'", [("api", "")])],
    )
    res = rc.analyze(
        dlink.link(
            {**_lib_files(), "use.ts": use}, {"(r)": [roots.RootHint("main", "file:use.ts", 1)]}, workers=1
        ).graph
    )
    v = {k.split(":", 1)[1]: x for k, x in res.verdicts().items()}
    assert v["src/a.ts#api"] in LIVE and v["src/a.ts#apiUnused"] in ("dead", "dead-cluster")


def test_exported_class_members_of_the_entry_are_api_but_private_ones_are_not():
    text = "export class Api { run() {} _hid() {} }\nclass Inner { go() {} }\n"
    c = Canned(text)
    c.add("defs", 2, **{"def.class": c.sp("class Api", "}\n"), "name": c.nd("Api", "class Api")})
    c.add("defs", 3, **{"def.method": c.nd("run() {}"), "name": c.nd("run")})
    c.add("defs", 3, **{"def.method": c.nd("_hid() {}"), "name": c.nd("_hid")})
    c.add("defs", 2, **{"def.class": c.sp("class Inner", "}\n"), "name": c.nd("Inner", "class Inner")})
    c.add("defs", 3, **{"def.method": c.nd("go() {}"), "name": c.nd("go")})
    c.add(
        "exports", 0, **{"export": c.nd("export class Api { run() {} _hid() {} }"), "name": c.nd("Api", "export class")}
    )
    f = extract(text, c, path="src/api.js")
    v, _ = _graph({"src/api.js": f}, {"package.json": '{"main": "src/api.js"}'}, ["package.json", "src/api.js"])
    assert v["src/api.js#Api"] == "alive" and v["src/api.js#Api.run"] == "alive"
    assert v["src/api.js#Api._hid"] != "alive" and v["src/api.js#Inner"] == "dead"


def test_exports_subpath_pattern_roots_every_matching_code_file():
    pkg = '{"exports": {"./sub/*": "./s/*.js", "./css/*": "./s/*.css"}}'
    tree = ["package.json", "s/a.js", "s/deep/b.js", "s/skip.css", "x.js"]
    files = {
        p: simple_facts("export function f() {}\n", ["f"], exports=[("f", "export function f")])
        for p in tree
        if p.endswith(".js")
    }
    v, rs = _graph(files, {"package.json": pkg}, tree)
    assert {r.node for r in rs.roots if r.kind == "export"} == {"javascript:s/a.js", "javascript:s/deep/b.js"}
    assert v["s/a.js#f"] == "alive" and v["s/deep/b.js#f"] == "alive" and v["x.js#f"] == "dead"
    assert not [f for f in rs.findings if f.code == "dc.root-dangling"]


def test_framework_entry_conventions_are_data_driven_and_need_the_dependency():
    nxt = '{"dependencies": {"next": "14", "react": "18"}}'
    hs = {(h.target, h.trust, h.strength) for h in roots.extract_hints("web/package.json", nxt)}
    assert ("sibling:./pages/*", "convention", "hard") in hs and ("sibling:./src/app/*", "convention", "hard") in hs
    assert not [t for t, _, _ in hs if "main" in t]  # vite is not a dependency
    assert roots.extract_hints("package.json", '{"dependencies": {"react": "18"}}') == []
    tree = ["web/package.json", "web/pages/index.tsx", "web/pages/api/x.ts", "web/lib/util.ts", "web/pages/style.css"]
    rs = roots.resolve(
        {"web/package.json": roots.extract_hints("web/package.json", nxt)}, {p: "100644" for p in tree}, lambda p: ""
    )
    assert {r.node for r in rs.roots if r.kind == "export"} == {
        "tsx:web/pages/index.tsx",
        "typescript:web/pages/api/x.ts",
    }
    vite = '{"devDependencies": {"vite": "5"}}'
    assert ("sibling:./src/main.*", "convention") in {
        (h.target, h.trust) for h in roots.extract_hints("package.json", vite)
    }
    assert "js_frameworks" in roots.fw() and {"next", "vite", "webpack", "@angular/core"} <= set(
        roots.fw()["js_frameworks"]
    )


def test_index_html_local_script_is_an_entry_remote_ones_are_not():
    html = '<html>\n<script src="https://cdn.x/y.js"></script>\n<script type="module" src="/src/main.tsx"></script>\n<script src=\'./boot.js?v=1\'></script>\n'
    hs = [(h.kind, h.target, h.line) for h in roots.extract_hints("app/index.html", html)]
    # a module script is an entry (its API); a classic one is kind script: its top-level definitions are globals of the page
    assert hs == [("export", "sibling:./src/main.tsx", 3), ("script", "sibling:./boot.js", 4)]
    assert roots.extract_hints("app/popup.html", html) == roots.extract_hints(
        "app/index.html", html
    )  # any page, not index.html only
    assert roots.extract_hints("app/page.txt", html) == []
    rs = roots.resolve(
        {"app/index.html": roots.extract_hints("app/index.html", html)}, {"app/src/main.tsx": "100644"}, lambda p: ""
    )
    assert [r.node for r in rs.roots] == ["tsx:app/src/main.tsx"] and not rs.findings


# ====================================================================== pass 3: false dead of the module and class forms
from dead_code_checker.graph.model import F_DECORATED, F_PROTOCOL  # noqa: E402


def lib_util_facts():
    """lib/util.js: exports a, def (default) and unused (util_facts)."""
    return util_facts()


def call_import(c: Canned, ctx: str, req: str = "import", **extra):
    """The bare call fragment the imports query yields for `ctx`: `import('./m')` or `require('./m')`."""
    caps = {"import": c.nd(ctx), "req": c.nd(req, ctx), "module": c.nd(re.search(r"'[^']*'", ctx).group(0), ctx)}
    return c.add("imports", 12, **{**caps, **extra})


def bound_import(c: Canned, stmt: str, ctx_mod: str, req: str = "import", **caps):
    """The declarator fragment (`const { a } = await import('./m')`) next to the bare call of the same module node."""
    mod = re.search(r"'[^']*'", ctx_mod).group(0)
    return c.add(
        "imports", 13, **{"import": c.nd(stmt), "req": c.nd(req, ctx_mod), "module": c.nd(mod, ctx_mod), **caps}
    )


def _importer(text: str, bound=None) -> "tg.Facts":
    c = Canned(text)
    call = re.search(r"(import|require)\('[^']*'\)", text).group(0)
    call_import(c, call, req=call.split("(")[0])
    if bound:
        bound(c)
    return extract(text, c, path="app.js")


def test_dynamic_import_without_a_binding_reaches_every_export_of_the_lazy_module():
    """Counterexample of the review: `lazy(() => import('./lazy.js'))` left lazy.js#Page and #default dead-cluster."""
    text = "const P = lazy(() => import('./lazy.js'));\n"
    f = _importer(text)
    alias = next(i.alias for i in f.imports if i.module == "./lazy.js")
    assert alias.startswith("__ns") and ("./lazy.js", (), alias, 1, 0, "") in {tuple(i) for i in f.imports}
    assert (alias, "ref", "") in {(r.name, r.kind, r.scope) for r in f.refs}
    v = _v({"app.js": f, "lazy.js": lib_util_facts()}, hints=("app.js",))
    assert {v[f"lazy.js#{n}"] in LIVE for n in ("a", "def", "default", "unused")} == {True}


def _declared_import(decl: str, binding: dict, awaited: bool, use: str = ""):
    """`function f() { <decl>; <use> }` with the call fragment and the declarator fragment of one dynamic import."""
    mod = "import('./lib/util.js')"
    text = f"function f() {{ {decl}; {use} }}\nf();\n"
    c = Canned(text)
    c.add("defs", 0, **{"def.func": c.sp("function f", "}\n"), "name": c.nd("f", "function f")})
    call_import(c, mod)
    c.add("refs", 0, **{"ref.call": c.nd("f()", "}\nf"), "name": c.nd("f", "}\nf()")})
    caps = {k: c.nd(*v) for k, v in binding.items()}
    caps["await"] = c.nd(f"await {mod}") if awaited else None
    bound_import(c, decl[len("const ") :], mod, **{k: v for k, v in caps.items() if v is not None})
    if use:
        name = use.split("(")[0]
        c.add("refs", 1, **{"ref.call": c.nd(use), "name": c.nd(name)})
    return extract(text, c, path="app.js")


def test_awaited_dynamic_import_with_a_destructuring_or_namespace_binding_is_precise():
    for decl, binding, use in (
        ("const { a } = await import('./lib/util.js')", {"name": ("a", "{ a")}, "a()"),
        ("const ns = await import('./lib/util.js')", {"ns": ("ns", "ns =")}, "ns.a()"),
    ):
        f = _declared_import(decl, binding, True, use)
        assert not any(i.alias.startswith("__ns") for i in f.imports), decl  # bound: no whole-namespace use
        v = _v({"app.js": f, "lib/util.js": lib_util_facts()}, hints=("app.js",))
        assert v["lib/util.js#a"] in LIVE and v["lib/util.js#unused"] == "dead", decl


def test_a_dynamic_import_without_await_holds_a_promise_so_the_namespace_is_used_as_a_whole():
    """`const p = import(m); p.then(m => m.render())`: p binds no namespace, nothing says which export is read."""
    f = _declared_import("const p = import('./lib/util.js')", {"ns": ("p", "p =")}, False)
    assert [i.alias.startswith("__ns") for i in f.imports] == [True] and not any(i.alias == "p" for i in f.imports)
    v = _v({"app.js": f, "lib/util.js": lib_util_facts()}, hints=("app.js",))
    assert {v[f"lib/util.js#{n}"] in LIVE for n in ("a", "unused", "default")} == {True}


def test_computed_import_specifier_is_a_glob_dynsite_that_reaches_the_files_it_can_name():
    assert tg._specs("`./pages/${n}.js`") == ["./pages/*.js"] and tg._specs('"./c/" + name') == ["./c/*"]
    assert tg._specs("'./cmds/' + dir + '/' + name + '.js'") == ["./cmds/*/*.js"]
    # a head that is no path is looked up as a path suffix like a static `import "components/Button"` (see the alias tests below)
    assert tg._specs("`lodash/${x}`") == ["/*/lodash/*"] and tg._specs("`./plain.js`") == ["./plain.js"]
    text = "function load(n) { return import(`./pages/${n}.js`); }\nload('a');\n"
    c = Canned(text)
    c.add("defs", 0, **{"def.func": c.sp("function load", "}"), "name": c.nd("load", "function load")})
    c.add(
        "imports",
        14,
        **{
            "import": c.nd("import(`./pages/${n}.js`)"),
            "req": c.nd("import", "import(`"),
            "pattern": c.nd("`./pages/${n}.js`"),
        },
    )
    c.add("refs", 0, **{"ref.call": c.nd("load('a')"), "name": c.nd("load", "load('a')")})
    f = extract(text, c, path="app.js")
    assert [(d.pattern, d.scope) for d in f.dyn] == [("import:./pages/*.js", "load")] and not f.imports
    def page(n):
        return simple_facts(
            f"export function view{n}() {{}}\n", [f"view{n}"], exports=[(f"view{n}", f"export function view{n}")]
        )
    files = {
        "app.js": f,
        "pages/a.js": page("A"),
        "pages/b.js": page("B"),
        "other/c.js": page("C"),
        "pages/c.css.js": page("D"),
    }
    v = _v(files, hints=("app.js",))
    assert v["pages/a.js#viewA"] in LIVE and v["pages/b.js#viewB"] in LIVE and v["pages/c.css.js#viewD"] in LIVE
    assert v["other/c.js#viewC"] == "dead"  # outside the glob
    assert tg._js_name("import(`./x`)") == ""  # a call is no name


def test_require_without_a_binding_uses_the_module_as_a_whole_a_bound_one_does_not():
    text = "use(require('./mw'));\nrequire('./util').a();\nconst x = require('./bound');\n"
    c = Canned(text)
    for ctx in ("require('./mw')", "require('./util')", "require('./bound')"):
        call_import(c, ctx, req="require")
    bound_import(c, "x =", "require('./bound')", req="require", ns=c.nd("x", "x ="), **{"import": c.sp("x =", ")")})
    f = extract(text, c, path="main.js")
    hidden = {i.module: i.alias for i in f.imports if i.alias.startswith("__ns")}
    assert set(hidden) == {"./mw", "./util"}  # ./bound is bound to x
    assert {(r.name, r.kind) for r in f.refs} == {(a, "ref") for a in hidden.values()}
    def mod(n):
        return simple_facts(
            f"export function e{n}() {{}}\n", [f"e{n}"], exports=[(f"e{n}", f"export function e{n}")]
        )
    v = _v({"main.js": f, "mw.js": mod("mw"), "util.js": mod("util"), "bound.js": mod("bound")}, hints=("main.js",))
    assert v["mw.js#emw"] in LIVE and v["util.js#eutil"] in LIVE
    assert v["bound.js#ebound"] == "dead"  # x is bound and never read


def test_new_url_import_meta_url_is_a_file_edge_not_a_namespace_use():
    text = "new Worker(new URL('./w.js', import.meta.url));\n"
    c = Canned(text)
    c.add(
        "imports",
        15,
        **{"import": c.nd("new URL('./w.js', import.meta.url)"), "req": c.nd("URL"), "module": c.nd("'./w.js'")},
    )
    c.add(
        "imports", 4, **{"import": c.nd("Worker(new"), "req": c.nd("Worker"), "module": c.nd("'./w.js'")}
    )  # not require-like
    f = extract(text, c, path="app.js")
    assert [(i.module, i.names, i.alias) for i in f.imports] == [("./w.js", (), "")] and not f.refs


def _class_facts(methods, header="", deco=None, base="Base", path="use.js"):
    """`class App extends Base { <methods> }` + `new App()` (the review's counterexample); Base is a class of the same file."""
    text = (
        f"{header}class Base {{}}\nclass App extends {base} {{ "
        + " ".join(f"{m}() {{}}" for m in methods)
        + " }\nnew App();\n"
    )
    c = Canned(text)
    c.add("defs", 2, **{"def.class": c.nd("class Base {}"), "name": c.nd("Base", "class Base")})
    c.add("defs", 2, **{"def.class": c.sp("class App", "}\nnew"), "name": c.nd("App", "class App")})
    for m in methods:
        c.add("defs", 3, **{"def.method": c.nd(f"{m}() {{}}"), "name": c.nd(m, f"{m}() {{}}")})
    c.add("refs", 2, **{"ref.call": c.nd("new App()"), "name": c.nd("App", "new App()")})
    c.add("refs", 13, **{"ref.inherit": c.nd(f"class App extends {base}"), "name": c.nd(base, f"extends {base}")})
    if header:  # import { X } from 'mod';
        mod = re.search(r"'([^']*)'", header).group(1)
        c.add("imports", 3, **{"import": c.sp("import", ";"), "name": c.nd("X"), "module": c.nd(f"'{mod}'")})
    return extract(text, c, path=path)


LIFECYCLE = ["render", "componentDidMount", "ngOnInit", "toJSON", "valueOf", "handleEvent"]


def test_lifecycle_and_protocol_methods_of_a_live_class_are_alive_others_are_not():
    """Counterexample of the review: `class App extends Base { render() {} }; new App()` left App.render dead."""
    f = _class_facts(LIFECYCLE + ["other"])
    flags = {d.qual: bool(d.flags & F_PROTOCOL) for d in f.defs}
    assert sorted(q for q, p in flags.items() if p) == sorted(f"App.{m}" for m in LIFECYCLE)
    v = _v({"use.js": f})
    assert v["use.js#App"] == "alive" and {v[f"use.js#App.{m}"] in LIVE for m in LIFECYCLE} == {True}
    assert v["use.js#App.other"] == "dead" and v["use.js#Base"] in LIVE


def test_a_dead_class_does_not_keep_its_lifecycle_methods_alive():
    f = _class_facts(LIFECYCLE)
    f = f._replace(refs=tuple(r for r in f.refs if r.kind != "call"))  # nobody makes an App
    v = {k: x for k, x in _v({"use.js": f}, hints=()).items() if "#" in k}
    assert {v["use.js#App"], *(v[f"use.js#App.{m}"] for m in LIFECYCLE)} <= {"dead", "dead-cluster"}


def test_framework_members_count_only_in_a_file_that_imports_the_framework():
    plain = _class_facts(["canActivate", "created"])
    assert not any(d.flags & F_PROTOCOL for d in plain.defs)
    ng = _class_facts(["canActivate", "created", "mounted"], header="import { X } from '@angular/core';\n")
    assert [d.qual for d in ng.defs if d.flags & F_PROTOCOL] == [
        "App.canActivate"
    ]  # vue's created / mounted: not imported
    vue = _class_facts(["canActivate", "created", "mounted"], header="import { X } from 'vue/dist/x';\n")
    assert [d.qual for d in vue.defs if d.flags & F_PROTOCOL] == ["App.created", "App.mounted"]  # a subpath counts
    v = _v({"use.js": ng})
    assert v["use.js#App.canActivate"] in LIVE and v["use.js#App.created"] == "dead"


def test_js_member_data_is_data_not_code(monkeypatch):
    any_, by, safe, ambient, sha, tpl = tg._js_data()
    assert {"componentDidMount", "ngOnInit", "toJSON", "valueOf", "handleEvent", "render", "connectedCallback"} <= any_
    assert "@angular/*" in dict(by) and "deprecated" in safe and ambient.search("src/global.d.ts")
    base = roots.fw()
    patched = {**base, "js_members": {**base["js_members"], "any": [*base["js_members"]["any"], "myHook"]}}
    shas = {lang: tg.TsGeneric().grammar_sha(lang) for lang in ("javascript", "typescript", "tsx", "shell")}
    try:
        monkeypatch.setattr(roots, "fw", lambda: patched)
        tg._js_data.cache_clear()
        new = {lang: tg.TsGeneric().grammar_sha(lang) for lang in shas}
        assert "myHook" in tg._js_data()[0]
    finally:
        monkeypatch.undo()
        tg._js_data.cache_clear()
    assert all(
        new[lang] != shas[lang] for lang in ("javascript", "typescript", "tsx")
    )  # data is part of a JS shard key
    assert new["shell"] == shas["shell"]  # a language that reads none of it keeps its key


def _decorated():
    text = (
        "@Injectable({})\nexport class A {\n  @HostListener('x') onX() {}\n  @deprecated() old() {}\n  plain() {}\n}\n"
    )
    c = Canned(text)
    c.add(
        "defs",
        1,
        **{"def.class": c.sp("class A", "\n}"), "name": c.nd("A", "class A"), "deco": c.nd("@Injectable({})")},
    )
    c.add("defs", 4, **{"def.method": c.nd("onX() {}"), "name": c.nd("onX"), "deco": c.nd("@HostListener('x')")})
    c.add("defs", 4, **{"def.method": c.nd("old() {}"), "name": c.nd("old"), "deco": c.nd("@deprecated()")})
    c.add("defs", 4, **{"def.method": c.nd("plain() {}"), "name": c.nd("plain")})
    return extract(text, c, "typescript", "src/a.ts")


def test_a_decorated_class_or_member_is_an_annotation_root_unless_the_decorator_registers_nothing():
    f = _decorated()
    assert [(r.kind, r.target, r.line, r.strength, r.trust) for r in sorted(f.roots)] == [
        ("annotation:HostListener", "A.onX", 3, "hard", "convention"),
        ("annotation:Injectable", "A", 1, "hard", "convention"),
    ]
    assert {d.qual: bool(d.flags & F_DECORATED) for d in f.defs if d.qual} == {
        "A": True,
        "A.onX": True,
        "A.old": False,
        "A.plain": False,
    }
    v = _v({"src/a.ts": f}, hints=())
    assert v["src/a.ts#A"] == "alive" and v["src/a.ts#A.onX"] == "alive"  # the framework calls them
    assert v["src/a.ts#A.old"] == "dead" and v["src/a.ts#A.plain"] == "dead"


def _ns_barrel(path="barrel.js"):
    text = "export * as ns from './m';\n"
    c = Canned(text)
    c.add(
        "imports", 16, **{"reexport": c.sp("export * as ns", ";"), "ns": c.nd("ns", "* as ns"), "module": c.nd("'./m'")}
    )
    return extract(text, c, path=path)


def _m_exports():
    return simple_facts(
        "export function run() {}\nexport function other() {}\n",
        ["run", "other"],
        exports=[("run", "export function run"), ("other", "export function other")],
    )


def test_namespace_re_export_is_read_and_reaches_every_export_of_the_module():
    """Counterexample of the review: `export * as ns from 'm'` gave no import edge and no def: m was dead."""
    f = _ns_barrel()
    assert [(d.qual, d.kind, d.vis) for d in f.defs if d.qual] == [("ns", "const", "exported")]
    assert [(i.module, i.names, i.alias, i.scope) for i in f.imports] == [("./m", (), "__re_ns", "ns")]
    assert {(r.name, r.kind, r.scope) for r in f.refs} == {("__re_ns", "ref", "ns")}
    use = simple_facts(
        "import { ns } from './barrel'; ns.run();\n",
        refs=[("call", "ns.run()", "ns.run")],
        imports=[("import {", "'./barrel'", [("ns", "")])],
    )
    v = _v({"barrel.js": f, "m.js": _m_exports(), "use.js": use})
    assert v["barrel.js#ns"] in LIVE and {v[f"m.js#{n}"] in LIVE for n in ("run", "other")} == {True}
    v, _ = _graph(
        {"index.js": f, "m.js": _m_exports()},
        {"package.json": '{"main": "index.js"}'},
        ["package.json", "index.js", "m.js"],
    )
    assert (
        v["index.js#ns"] == "alive" and v["m.js#run"] in LIVE and v["m.js#other"] in LIVE
    )  # a package whose entry is the barrel


def test_namespace_import_used_as_a_value_reaches_every_export_a_member_access_does_not():
    ns = "import * as h from './handlers';\n"
    handlers = simple_facts(
        "export function a() {}\nexport function b() {}\n",
        ["a", "b"],
        exports=[("a", "export function a"), ("b", "export function b")],
    )
    res = {}
    for name, use in (("value", "register(h);\n"), ("member", "h.a();\n")):
        c = Canned(ns + use)
        c.add("imports", 0, **{"import": c.sp("import * as h", ";"), "module": c.nd("'./handlers'")})
        c.add(
            "imports",
            2,
            **{"import": c.sp("import * as h", ";"), "ns": c.nd("h", "* as h"), "module": c.nd("'./handlers'")},
        )
        if name == "value":
            c.add("refs", 0, **{"ref.call": c.nd("register(h)"), "name": c.nd("register", "register(h)")})
            c.add("refs", 8, **{"ref.ref": c.nd("(h)"), "name": c.nd("h", "register(h")})
        else:
            c.add("refs", 1, **{"ref.call": c.nd("h.a()"), "name": c.nd("h.a")})
        res[name] = _v({"handlers.js": handlers, "use.js": extract(ns + use, c, path="use.js")})
    assert res["value"]["handlers.js#a"] in LIVE and res["value"]["handlers.js#b"] in LIVE  # `h` escaped to register()
    assert res["member"]["handlers.js#a"] in LIVE and res["member"]["handlers.js#b"] == "dead"


def _cjs(text, *matches):
    """Facts of `text` (functions foo, a, c, bar are defs) with exports.scm matches {capture: (text, inside)}; "export" is the
    text of the statement."""
    c = Canned(text)
    for n in ("foo", "a", "c", "bar", "x"):
        if f"function {n}" in text:
            c.add("defs", 0, **{"def.func": c.sp(f"function {n}", "}"), "name": c.nd(n, f"function {n}")})
    for caps in matches:
        c.add("exports", 9, export=c.nd(caps["export"]), **{k: c.nd(*v) for k, v in caps.items() if k != "export"})
    return extract(text, c, path="index.js")


def test_cjs_module_exports_member_assignment_exports_the_name():
    """Counterexample of the review: `module.exports.foo = foo` left foo private, dead in a package whose main is this file."""
    text = "function foo() {}\nfunction bar() {}\nmodule.exports.foo = foo;\nfoo.exports.bar = bar;\n"
    f = _cjs(
        text,
        {
            "export": "module.exports.foo = foo",
            "obj": ("module",),
            "prop": ("exports", "module.exports.foo"),
            "key": ("foo", "exports.foo"),
            "default": ("foo", "= foo"),
        },
        {
            "export": "foo.exports.bar = bar",
            "obj": ("foo", "foo.exports.bar"),
            "prop": ("exports", "foo.exports.bar"),
            "key": ("bar", "exports.bar"),
            "default": ("bar", "= bar"),
        },
    )
    assert {d.qual: d.vis for d in f.defs if d.qual} == {
        "foo": "exported",
        "bar": "private",
    }  # only module.exports.k exports
    v, _ = _graph({"index.js": f}, {"package.json": '{"main": "index.js"}'}, ["package.json", "index.js"])
    assert v["index.js#foo"] == "alive" and v["index.js#bar"] == "dead"


def test_cjs_object_assign_to_module_exports_exports_its_names_and_only_that_call_does():
    text = "function a() {}\nfunction c() {}\nfunction x() {}\nObject.assign(module.exports, { a, b: c });\nmerge(module.exports, { x });\n"
    assign = {
        "export": "Object.assign(module.exports, { a, b: c })",
        "fn": ("Object.assign",),
        "obj": ("module", "Object.assign(module"),
        "prop": ("exports", "Object.assign(module.exports"),
    }
    f = _cjs(
        text,
        {**assign, "name": ("a", "{ a")},
        {**assign, "alias": ("b", "b: c"), "name": ("c", "b: c")},
        {
            "export": "merge(module.exports, { x })",
            "fn": ("merge",),
            "obj": ("module", "merge(module"),
            "prop": ("exports", "merge(module.exports"),
            "name": ("x", "{ x"),
        },
    )
    assert {d.qual: d.vis for d in f.defs if d.qual} == {
        "a": "exported",
        "c": "exported",
        "b": "exported",
        "x": "private",
    }
    assert ("c", "ref", "b") in {
        (r.name, r.kind, r.scope) for r in f.refs
    }  # merge(module.exports, { x }) exports nothing
    for lang in ("javascript", "typescript"):
        assert (
            "(call_expression function: (_) @fn arguments: (arguments (member_expression object: (identifier) @obj"
            in tg._query_text(lang, "exports")
        )
    v, _ = _graph({"index.js": f}, {"package.json": '{"main": "index.js"}'}, ["package.json", "index.js"])
    assert v["index.js#a"] == "alive" and v["index.js#b"] == "alive" and v["index.js#c"] in LIVE


def test_ts_import_equals_require_and_export_equals():
    text = "import x = require('./m');\nexport = x;\n"
    c = Canned(text)
    c.add("imports", 17, **{"import": c.sp("import x", ";"), "ns": c.nd("x", "import x"), "module": c.nd("'./m'")})
    c.add("exports", 7, **{"export": c.nd("export = x"), "default": c.nd("x", "= x")})
    f = extract(text, c, "typescript", "a.ts")
    # `export = x` of an import binding hands the module on: the alias def `default` reads m through a hidden import of its own
    assert [(i.module, i.names, i.alias, i.scope) for i in f.imports] == [
        ("./m", (), "__re_default", "default"),
        ("./m", (), "x", ""),
    ]
    assert [(d.qual, d.vis) for d in f.defs if d.qual] == [("default", "exported")]
    assert ("__re_default", "ref", "default") in {(r.name, r.kind, r.scope) for r in f.refs}
    ts = {k: tg._query_text("typescript", k) for k in ("imports", "exports")}
    assert "import_require_clause" in ts["imports"] and '(export_statement "=" (identifier) @default)' in ts["exports"]
    assert "import_require_clause" not in tg._query_text("javascript", "imports")  # a TypeScript form only


def _entry_roots(pkg: str, tree):
    hints = {"package.json": roots.extract_hints("package.json", pkg)}
    rs = roots.resolve(hints, {p: "100644" for p in ["package.json", *tree]}, lambda p: "")
    return {r.node.split(":", 1)[1] for r in rs.roots if r.kind == "export"}, rs


def test_built_entry_maps_to_js_sources_and_to_dual_build_layouts():
    """Counterexamples of the review: `./lib/index.js` gave only src/index.ts[x]; `./dist/esm/index.js` gave src/esm/index.ts."""
    js = ["src/index.js", "src/index.jsx", "src/index.mjs", "src/other.js"]
    got, rs = _entry_roots('{"main": "./lib/index.js"}', js)
    assert got == {"src/index.js", "src/index.jsx", "src/index.mjs"} and not rs.findings
    tree = ["src/index.ts", "src/util/deep.ts", "src/other.ts"]
    pkg = '{"main": "./dist/cjs/index.js", "module": "./dist/esm/index.js", "types": "./dist/types/index.d.ts"}'
    assert _entry_roots(pkg, tree)[0] == {"src/index.ts"}  # dist/esm and dist/cjs name no directory of ours
    assert _entry_roots('{"exports": {"./util/*": "./dist/esm/util/*.js"}}', tree)[0] == {"src/util/deep.ts"}
    assert _entry_roots('{"main": "./dist/src/index.js"}', tree)[0] == {"src/index.ts"}  # tsc with rootDir "."
    assert _entry_roots('{"main": "./build/index.cjs"}', ["source/index.cts"])[0] == {"source/index.cts"}
    assert _entry_roots('{"main": "./index.js"}', ["index.ts", "src/index.ts"])[0] == {"index.ts"}  # built in place
    assert (
        _entry_roots('{"main": "./dist/index.js"}', ["lib/index.ts", "tests/index.ts"])[0] == set()
    )  # not a source dir


def test_npm_source_candidates_are_data():
    b = roots.fw()["js_build"]
    assert {"dist", "lib", "esm"} <= set(b["out_dirs"]) and {"esm", "cjs", "types"} <= set(b["variant_dirs"])
    assert b["src_dirs"] and {".ts", ".tsx", ".js", ".jsx", ".mjs"} <= set(b["src_exts"])
    assert (
        roots._npm_sources("./dist/esm/index.d.ts")[-len(b["src_dirs"]) * len(b["src_exts"]) :][0]
        == f"./{b['src_dirs'][0]}/index{b['src_exts'][0]}"
    )
    assert all(
        c != "./lib/index.js" for c in roots._npm_sources("./lib/index.js")
    )  # the built path itself is not a candidate


def test_tool_configs_are_entries_and_ambient_declaration_files_are_the_api():
    for path in (
        "vite.config.ts",
        "web/tailwind.config.cjs",
        ".eslintrc.js",
        "gulpfile.babel.js",
        ".storybook/main.ts",
        "jest.setup.js",
        "setupTests.ts",
        "karma.conf.js",
        "src/Button.stories.tsx",
        "src/__mocks__/api.js",
    ):
        assert [(h.kind, h.target, h.strength, h.trust) for h in roots.extract_hints(path, "")] == [
            ("main", f"file:{path}", "hard", "convention")
        ], path
    assert [(h.kind, h.target) for h in roots.extract_hints("src/types/global.d.ts", "")] == [
        ("export", "file:src/types/global.d.ts")
    ]
    for path in ("src/app.ts", "src/config.ts", "lib/jest.js", "a.config.json", "src/mydts.ts"):
        assert not [h for h in roots.extract_hints(path, "") if h.target == f"file:{path}"], path
    text = "interface Window { app: string }\ninterface Unused {}\n"
    c = Canned(text)
    c.add(
        "defs", 2, **{"def.iface": c.nd("interface Window { app: string }"), "name": c.nd("Window", "interface Window")}
    )
    c.add("defs", 2, **{"def.iface": c.nd("interface Unused {}"), "name": c.nd("Unused", "interface Unused")})
    amb = extract(text, c, "typescript", "src/types/global.d.ts")
    assert {d.qual: d.vis for d in amb.defs if d.qual} == {"Window": "exported", "Unused": "exported"}  # global scope
    c2 = Canned(text)
    c2.add("defs", 2, **{"def.iface": c2.nd("interface Unused {}"), "name": c2.nd("Unused", "interface Unused")})
    plain = extract(text, c2, "typescript", "src/types.ts")
    cfg = simple_facts(
        "function plugin() {}\nexport default { plugins: [plugin()] };\n",
        ["plugin"],
        refs=[("call", "[plugin()", "plugin")],
    )
    tree = ["src/types/global.d.ts", "src/types.ts", "vite.config.ts"]
    v, rs = _graph(
        {"src/types/global.d.ts": amb, "src/types.ts": plain, "vite.config.ts": _ts(cfg)}, {p: "" for p in tree}, tree
    )
    assert v["src/types/global.d.ts#Window"] == "alive" and v["src/types/global.d.ts#Unused"] == "alive"
    assert (
        v["src/types.ts#Unused"] == "dead" and v["vite.config.ts#plugin"] == "alive"
    )  # the config file is loaded by its tool


def test_operand_queries_name_the_same_parents_in_javascript_and_typescript():
    def parent(lang):
        return set(
            re.findall(r"^\((\w+) (?:\w+: )?\(identifier\) @name\) @ref\.ref", tg._query_text(lang, "refs"), re.M)
        )  # noqa: E731
    js, ts = parent("javascript"), parent("typescript")
    new = {
        "assignment_pattern",
        "augmented_assignment_expression",
        "yield_expression",
        "throw_statement",
        "switch_case",
        "for_in_statement",
    }
    assert new <= js and new <= ts and js - {"field_definition", "jsx_expression"} <= ts  # jsx is the tsx query
    assert "field_definition" in js and "public_field_definition" in ts and "public_field_definition" not in js


def test_decorator_patterns_are_in_the_defs_queries_of_both_grammars():
    member = {
        # javascript: the decorator is a field of the method; typescript and tsx: a sibling of it in the class body, one match
        # for all the decorators of a member (`+`), found against the real grammar (the field form never matches there)
        "javascript": "(class_body (method_definition decorator: (decorator) @deco name: (_) @name) @def.method)",
        "typescript": "(class_body (decorator)+ @deco . (method_definition name: (_) @name) @def.method)",
    }
    for lang, cls in (
        ("javascript", ("class_declaration",)),
        ("typescript", ("class_declaration", "abstract_class_declaration")),
    ):
        text = tg._query_text(lang, "defs")
        assert member[lang] in text
        assert all(f"({c} decorator: (decorator) @deco name:" in text for c in cls)
        assert "(export_statement decorator: (decorator) @deco declaration: (class_declaration name:" in text


def test_bundler_globs_and_require_resolve_name_files_of_the_tree():
    text = "const m = import.meta.glob('./pages/*.tsx');\nrequire.context('./cmds', true, /x/);\nfork(require.resolve('./job.js'));\n"
    c = Canned(text)
    for stmt, req, mod in (
        ("import.meta.glob('./pages/*.tsx')", "import.meta.glob", "'./pages/*.tsx'"),
        ("require.context('./cmds', true, /x/)", "require.context", "'./cmds'"),
        ("require.resolve('./job.js')", "require.resolve", "'./job.js'"),
    ):
        c.add("imports", 12, **{"import": c.nd(stmt), "req": c.nd(req, stmt), "module": c.nd(mod, stmt)})
    f = extract(text, c, path="app.js")
    assert sorted(d.pattern for d in f.dyn) == ["import:./cmds/*", "import:./pages/*.tsx"]
    assert [(i.module, i.names, i.alias) for i in f.imports] == [("./job.js", (), "")]  # a path, not a namespace use
    def page(n):
        return _ts(simple_facts(f"export function {n}() {{}}\n", [n], exports=[(n, f"export function {n}")]))
    files = {
        "app.js": f,
        "pages/a.tsx": page("A"),
        "cmds/run.ts": page("run"),
        "job.js": page("job"),
        "lib/z.ts": page("Z"),
    }
    v = _v(files, hints=("app.js",))
    assert (
        v["pages/a.tsx#A"] in LIVE
        and v["cmds/run.ts#run"] in LIVE
        and v["job.js"] == "alive"
        and v["lib/z.ts#Z"] == "dead"
    )


def test_root_absolute_glob_is_relative_to_the_project_not_to_the_file():
    text = "const m = import.meta.glob('/src/routes/*.ts');\n"
    c = Canned(text)
    c.add(
        "imports",
        12,
        **{
            "import": c.nd("import.meta.glob('/src/routes/*.ts')"),
            "req": c.nd("import.meta.glob"),
            "module": c.nd("'/src/routes/*.ts'"),
        },
    )
    f = extract(text, c, path="src/deep/app.js")
    assert [d.pattern for d in f.dyn] == ["import:/src/routes/*.ts"]
    route = _ts(simple_facts("export function r() {}\n", ["r"], exports=[("r", "export function r")]))
    v = _v({"src/deep/app.js": f, "src/routes/a.ts": route}, hints=("src/deep/app.js",))
    assert v["src/routes/a.ts#r"] in LIVE


def test_a_dynamically_imported_barrel_passes_its_whole_namespace_on():
    text = "const m = import('./src/index');\n"
    c = Canned(text)
    call_import(c, "import('./src/index')")
    app = extract(text, c, path="app.js")
    v = _v({**_lib_files(), "app.js": app}, hints=("app.js",))
    assert v["src/a.ts#api"] in LIVE and v["src/a.ts#apiUnused"] in LIVE  # through the `export *` of the barrel
    assert v["src/b.ts#b"] in LIVE and v["src/b.ts#bUnused"] == "dead"  # export { b } from './b': only b


def test_methods_of_a_template_bound_class_are_soft_roots_a_plain_class_has_none():
    """An Angular `@Component` calls `onSave()` from its template: nothing in the tree does, the method is not dead."""
    text = "@Component({})\nclass A { onSave() {} }\nclass B { onSave() {} }\n"
    c = Canned(text)
    c.add(
        "defs", 1, **{"def.class": c.sp("class A", "}\n"), "name": c.nd("A", "class A"), "deco": c.nd("@Component({})")}
    )
    c.add("defs", 4, **{"def.method": c.nd("onSave() {}"), "name": c.nd("onSave")})
    c.add("defs", 1, **{"def.class": c.sp("class B", "}\n"), "name": c.nd("B", "class B")})
    c.add("defs", 4, **{"def.method": c.nd("onSave() {}", "class B"), "name": c.nd("onSave", "class B { onSave")})
    f = extract(text, c, "typescript", "src/a.ts")
    assert sorted((r.kind, r.target, r.strength) for r in f.roots) == [
        ("annotation:Component", "A", "hard"),
        ("template", "A.onSave", "soft"),
    ]
    v = _v({"src/a.ts": _ts(f)}, hints=())
    assert v["src/a.ts#A"] == "alive" and v["src/a.ts#A.onSave"] == "weak-alive"
    assert v["src/a.ts#B"] == "dead" and v["src/a.ts#B.onSave"] in ("dead", "dead-cluster")


# ====================================================================== second pass: API through barrels, entries, types, specifiers
def _class_file(path: str, default: bool = False):
    """`export [default] class Api { run() {} _hid() {} }` and `export class Other { go() {} }` (exported, but never re-exported by name)."""
    head = "export default class Api" if default else "export class Api"
    text = f"{head} {{ run() {{}} _hid() {{}} }}\nexport class Other {{ go() {{}} }}\n"
    c = Canned(text)
    c.add("defs", 2, **{"def.class": c.sp("class Api", "}\n"), "name": c.nd("Api", "class Api")})
    c.add("defs", 3, **{"def.method": c.nd("run() {}"), "name": c.nd("run")})
    c.add("defs", 3, **{"def.method": c.nd("_hid() {}"), "name": c.nd("_hid")})
    c.add("defs", 2, **{"def.class": c.sp("class Other", "}\n"), "name": c.nd("Other", "class Other")})
    c.add("defs", 3, **{"def.method": c.nd("go() {}"), "name": c.nd("go")})
    stmt = c.sp(head, "}\n")
    c.add("exports", 0, export=stmt, name=c.nd("Api", "class Api"))
    if default:
        c.add("exports", 4, export=stmt, default=c.nd("Api", "class Api"))
    c.add("exports", 0, export=c.sp("export class Other", "}\n"), name=c.nd("Other", "class Other"))
    return _ts(extract(text, c, "typescript", path))


def _barrel_of(kind: str):
    """src/index.ts re-exporting the Api of src/a.ts in the form `kind`."""
    text = {
        "star": "export * from './a';\n",
        "named": "export { Api } from './a';\n",
        "renamed": "export { Api as Client } from './a';\n",
        "default": "export { default as Api } from './a';\n",
        "ns": "export * as lib from './a';\n",
        "bound": "import { Api } from './a';\nexport { Api };\n",
        "bound-default": "import Api from './a';\nexport default Api;\n",
    }[kind]
    c = Canned(text)
    mod = c.nd("'./a'")
    if kind == "star":
        c.add("imports", 8, reexport=c.sp("export * from", ";"), star=c.nd("*"), module=mod)
    elif kind in ("named", "renamed", "default"):
        al = (
            {"alias": c.nd("Client")}
            if kind == "renamed"
            else {"alias": c.nd("Api", "as Api")} if kind == "default" else {}
        )
        nm = "default" if kind == "default" else "Api"
        c.add("imports", 9, reexport=c.sp("export {", ";"), name=c.nd(nm, "export {"), module=mod, **al)
    elif kind == "ns":
        c.add("imports", 10, reexport=c.sp("export * as", ";"), ns=c.nd("lib"), module=mod)
    elif kind == "bound":
        c.add("imports", 3, **{"import": c.sp("import {", ";"), "name": c.nd("Api", "{ Api"), "module": mod})
        c.add("exports", 2, export=c.sp("export {", ";"), name=c.nd("Api", "export { Api"))
    else:
        c.add("imports", 1, **{"import": c.sp("import Api", ";"), "default": c.nd("Api", "import Api"), "module": mod})
        c.add("exports", 4, export=c.sp("export default", ";"), default=c.nd("Api", "default Api"))
    return _ts(extract(text, c, "typescript", "src/index.ts"))


@pytest.mark.parametrize("kind", ["star", "named", "renamed", "default", "ns", "bound", "bound-default"])
def test_class_files_behind_a_barrel_expose_their_public_members(kind):
    """Counterexample of the review: `export * from './a'` and `export { Api } from './a'` kept the class alive and src/a.ts#Api.run
    dead, while the same class in the entry file was API. Every form that hands the class on makes its public members API.
    """
    files = {
        "src/index.ts": _barrel_of(kind),
        "src/a.ts": _class_file("src/a.ts", default=kind in ("default", "bound-default")),
    }
    v, _ = _graph(files, {"package.json": '{"main": "src/index.ts"}'}, ["package.json", *files])
    assert v["src/a.ts#Api"] == "alive" and v["src/a.ts#Api.run"] == "alive", kind
    assert v["src/a.ts#Api._hid"] != "alive", kind  # a private member is no API
    everything = kind in ("star", "ns")  # the module as a whole is handed on: the other class too
    assert (v["src/a.ts#Other.go"] == "alive") == everything and (v["src/a.ts#Other"] == "alive") == everything, kind


def test_a_barrel_that_nothing_registers_as_an_entry_does_not_expose_members():
    files = {"src/index.ts": _barrel_of("star"), "src/a.ts": _class_file("src/a.ts")}
    v, _ = _graph(files, {"package.json": '{"main": "src/other.ts"}'}, ["package.json", *files])
    assert v["src/a.ts#Api.run"] in ("dead", "dead-cluster") and v["src/a.ts#Api"] in ("dead", "dead-cluster")


def test_a_chain_of_barrels_and_a_commonjs_barrel_expose_the_members_too():
    deep = _ts(_barrel_of("star"))._replace(
        lang="typescript"
    )  # src/lib/index.ts: export * from './a' (resolved from src/lib/)
    top_text = "export * from './lib';\n"
    c = Canned(top_text)
    c.add("imports", 8, reexport=c.sp("export * from", ";"), star=c.nd("*"), module=c.nd("'./lib'"))
    top = _ts(extract(top_text, c, "typescript", "src/index.ts"))
    files = {"src/index.ts": top, "src/lib/index.ts": deep, "src/lib/a.ts": _class_file("src/lib/a.ts")}
    v, _ = _graph(files, {"package.json": '{"main": "src/index.ts"}'}, ["package.json", *files])
    assert v["src/lib/a.ts#Api.run"] == "alive" and v["src/lib/a.ts#Other.go"] == "alive"
    # module.exports = require('./a') in a plain-JS package: the whole module value is handed on
    text = "module.exports = require('./a');\n"
    cj = Canned(text)
    call_import(cj, "require('./a')", "require")
    cls = "class Api { run() {} }\nmodule.exports = Api;\n"
    ca = Canned(cls)
    ca.add("defs", 2, **{"def.class": ca.sp("class Api", "}\n"), "name": ca.nd("Api", "class Api")})
    ca.add("defs", 3, **{"def.method": ca.nd("run() {}"), "name": ca.nd("run")})
    ca.add(
        "exports",
        9,
        export=ca.nd("module.exports = Api"),
        obj=ca.nd("module"),
        prop=ca.nd("exports"),
        default=ca.nd("Api", "= Api"),
    )
    files = {"index.js": extract(text, cj, path="index.js"), "a.js": extract(cls, ca, path="a.js")}
    v, _ = _graph(files, {"package.json": '{"main": "index.js"}'}, ["package.json", *files])
    assert v["a.js#Api"] == "alive" and v["a.js#Api.run"] == "alive"


def test_the_reexport_rule_is_marked_on_the_edges_that_hand_a_module_on():
    lk = dlink.link({"src/index.ts": _barrel_of("named"), "src/a.ts": _class_file("src/a.ts")}, workers=1)
    rules = {lk.graph.evid[e.ev].snippet.split(":")[0] for e in lk.graph.edges if e.ev >= 0}
    assert "reexport" in rules and dlink.LINKER_V.startswith("13.")
    assert lk.graph.evid[0].extractor == f"link@{dlink.LINKER_V}"


# ---------------------------------------------------------------------- entries without an extension, directories, scripts
@pytest.mark.parametrize(
    "pkg, tree, want",
    [
        ('{"main": "index"}', ["index.js", "other.js"], {"index.js"}),
        ('{"main": "lib"}', ["lib/index.js", "lib/util.js"], {"lib/index.js"}),
        ('{"main": "./lib/"}', ["lib/index.js", "lib/util.js"], {"lib/index.js"}),
        ('{"main": "lib"}', ["lib.js", "lib/util.js"], {"lib.js"}),
        ('{"main": "./lib/"}', ["src/index.ts", "src/util.ts"], {"src/index.ts"}),  # built into lib/
        ('{"main": "index"}', ["index.mjs"], {"index.mjs"}),
        ('{"main": "."}', ["index.js", "util.js"], {"index.js"}),
        ('{"main": "./"}', ["index.ts", "util.ts"], {"index.ts"}),
        ('{"scripts": {"start": "node server"}}', ["server.js", "lib.js"], {"server.js"}),
        ('{"scripts": {"start": "node ./server --port 3"}}', ["server.js"], {"server.js"}),
        ('{"scripts": {"start": "node dist/app"}}', ["src/app.ts"], {"src/app.ts"}),
        (
            '{"scripts": {"build": "tsup src/index.ts src/cli.ts --format esm"}}',
            ["src/index.ts", "src/cli.ts", "src/x.ts"],
            {"src/index.ts", "src/cli.ts"},
        ),
        ('{"scripts": {"build": "esbuild --entry=./app/main.js --bundle"}}', ["app/main.js"], {"app/main.js"}),
        ('{"scripts": {"dev": "cross-env X=1 node -r dotenv/config server.js"}}', ["server.js"], {"server.js"}),
    ],
)
def test_extensionless_and_directory_entries_and_script_files_are_roots(pkg, tree, want):
    """Counterexamples of the review: `"main": "index"`, `"main": "lib"`, `"main": "./lib/"` and `node server` gave no root."""
    got, rs = _entry_roots(pkg, tree)
    assert got == want and not rs.findings


def test_an_extensionless_bin_is_a_root():
    pkg = '{"bin": {"tool": "./bin/tool"}}'
    hints = {"package.json": roots.extract_hints("package.json", pkg)}
    rs = roots.resolve(hints, {"package.json": "100644", "bin/tool.js": "100644"}, lambda p: "")
    assert {r.node for r in rs.roots if r.kind == "bin"} == {"javascript:bin/tool.js"}


def test_a_plain_js_package_with_an_extensionless_main_keeps_its_api_alive():
    api = simple_facts("function go() {}\nfunction stay() {}\n", ["go", "stay"], exports=[("go", "function go")])
    v, _ = _graph({"index.js": api}, {"package.json": '{"main": "index"}'}, ["package.json", "index.js"])
    assert v["index.js#go"] == "alive" and v["index.js#stay"] == "dead"


def test_bare_script_words_are_files_only_for_an_interpreter():
    assert roots.command_targets("node server") == ["cmd:server"]  # the shared parser is unchanged
    assert roots.command_targets("node server", bare=True) == ["file:server"]
    assert roots.command_targets("eslint .", bare=True) == ["cmd:eslint"]  # a plain command stays a command
    assert roots.command_targets("node ./server --port 3", bare=True) == ["file:server"]
    assert roots.command_targets("tsx watch src/a.ts", bare=True) == ["cmd:watch"]  # a subcommand is no file
    assert sorted(roots._script_files("tsx watch src/a.ts && node b.mjs")) == ["b.mjs", "src/a.ts"]
    assert roots._script_files("echo hi && jest --coverage") == []


# ---------------------------------------------------------------------- framework entry conventions, bundler configs, test files
@pytest.mark.parametrize(
    "deps, file",
    [
        ('{"react-scripts": "5"}', "src/index.js"),  # Create React App
        ('{"react-scripts": "5"}', "src/setupTests.js"),
        ('{"@nestjs/core": "10"}', "src/main.ts"),
        ('{"gatsby": "5"}', "src/pages/index.js"),
        ('{"gatsby": "5"}', "src/pages/blog/[slug].tsx"),
        ('{"gatsby": "5"}', "gatsby-config.js"),
        ('{"gatsby": "5"}', "gatsby-node.js"),
        ('{"gatsby": "5"}', "gatsby-browser.js"),
        ('{"gatsby": "5"}', "src/templates/post.js"),
        ('{"expo": "50"}', "App.js"),
        ('{"expo": "50"}', "index.js"),
        ('{"expo": "50", "expo-router": "3"}', "app/_layout.tsx"),
        ('{"react-native": "0.73"}', "index.js"),
        ('{"react-native": "0.73"}', "App.tsx"),
        ('{"astro": "4"}', "src/pages/api/x.ts"),
        ('{"@vue/cli-service": "5"}', "src/main.js"),
        ('{"@docusaurus/core": "3"}', "sidebars.js"),
        ('{"electron": "30"}', "src/main/index.ts"),
        ('{"cypress": "13"}', "cypress/support/e2e.ts"),
    ],
)
def test_framework_entry_conventions_of_the_review_are_roots(deps, file):
    """Counterexamples of the review: react-scripts + src/index.js, @nestjs/core + src/main.ts, gatsby pages and node files,
    expo + App.js each returned no root."""
    pkg = f'{{"dependencies": {deps}}}'
    assert _entry_roots(pkg, [file, "src/lib/unused.js"])[0] == {file}
    assert _entry_roots('{"dependencies": {"left-pad": "1"}}', [file])[0] == set()  # a convention needs its dependency


@pytest.mark.parametrize(
    "config, text, tree, want",
    [
        (
            "webpack.config.js",
            "module.exports = { entry: './src/app.js', mode: 'production' };\n",
            ["src/app.js", "src/x.js"],
            {"src/app.js"},
        ),
        (
            "webpack.config.js",
            "module.exports = {\n  entry: {\n    main: './src/main.js',\n    admin: ['./src/admin.js', './src/polyfills.js'],\n  },\n};\n",
            ["src/main.js", "src/admin.js", "src/polyfills.js", "src/x.js"],
            {"src/main.js", "src/admin.js", "src/polyfills.js"},
        ),
        (
            "rollup.config.js",
            "export default { input: 'src/main.js', output: { file: 'dist/b.js' } };\n",
            ["src/main.js", "dist/b.js"],
            {"src/main.js"},
        ),
        (
            "tsup.config.ts",
            "export default defineConfig({ entry: ['src/index.ts', 'src/cli.ts'], format: ['esm'] });\n",
            ["src/index.ts", "src/cli.ts", "src/x.ts"],
            {"src/index.ts", "src/cli.ts"},
        ),
        (
            "scripts/esbuild.config.mjs",
            "await build({ entryPoints: [path.resolve(__dirname, 'app/main.ts')] });\n",
            ["scripts/app/main.ts", "app/main.ts"],
            {"scripts/app/main.ts"},
        ),
        (
            "webpack.config.js",
            "module.exports = { entry: './src/index' };\n",
            ["src/index.jsx"],
            {"src/index.jsx"},
        ),  # extensionless
        (
            "vite.config.ts",
            "export default { build: { lib: { entry: resolve(__dirname, 'lib/main.ts') } } };\n",
            ["lib/main.ts"],
            {"lib/main.ts"},
        ),
    ],
    ids=["webpack-single", "webpack-multi", "rollup", "tsup", "esbuild", "webpack-extless", "vite-lib"],
)
def test_bundler_config_entries_are_roots(config, text, tree, want):
    """Counterexample of the review: a webpack `entry`, a rollup `input`, a tsup / esbuild `entry` gave no root."""
    hints = {config: roots.extract_hints(config, text)}
    assert {h.kind for h in hints[config]} == {
        "main",
        "export",
    }  # the config is an entry itself, its bundle entries export
    rs = roots.resolve(hints, {p: "100644" for p in [config, *tree]}, lambda p: "")
    assert {r.node.split(":", 1)[1] for r in rs.roots if r.kind == "export"} == want and not rs.findings


def test_a_config_value_that_is_no_path_is_no_entry():
    text = "module.exports = { entry: 'main', input: 'text', entries: [url + '/x'], entryPoints: ['https://cdn/x.js', '@scope/pkg'] };\n"
    assert [h.kind for h in roots.extract_hints("webpack.config.js", text)] == ["main"]


@pytest.mark.parametrize(
    "path",
    [
        "cypress/e2e/login.cy.ts",
        "src/Button.cy.tsx",
        "cypress/support/commands.ts",
        "cypress/component/a.spec.js",
        "e2e/app.cy.js",
    ],
)
def test_cypress_specs_and_support_files_are_tests(path):
    """Counterexample of the review: `*.cy.ts` and cypress/ were no test file for the roots (dead file) nor for the flags (reported)."""
    from dead_code_checker.graph.model import F_TEST

    assert roots.is_test_path(path) and dlink.path_flags(path) & F_TEST
    f = simple_facts("function helper() {}\n", ["helper"])._replace(lang=dclangs.load().detect(path))
    v, rs = _graph({path: f}, {"package.json": "{}"}, ["package.json", path])
    assert v[f"{path}#helper"] == "test-only" and [r.kind for r in rs.roots] == ["test"]


def test_non_test_paths_next_to_the_cypress_names_stay_production_code():
    from dead_code_checker.graph.model import F_TEST

    for path in ("src/policy.ts", "src/cypress-helper.ts", "src/cy.ts", "src/a.cy.json"):
        assert not roots.is_test_path(path) and not dlink.path_flags(path) & F_TEST, path


# ---------------------------------------------------------------------- typeof at the type level
def test_typeof_in_a_type_is_a_use_of_the_value():
    """Counterexample of the review: `type Store = ReturnType<typeof makeStore>` gave no ref, `export function makeStore` in a non-entry
    file was dead although deleting it breaks the compile."""
    for g in ("typescript", "tsx"):
        assert "(type_query (_) @name) @ref.ref" in tg._query_text(g, "refs"), g
    assert "type_query" not in tg._query_text("javascript", "refs")  # a TypeScript node
    text = "import { makeStore, Mode } from './store';\ntype Store = ReturnType<typeof makeStore>;\ntype K = keyof typeof Mode;\ntype C = typeof cfg.api;\ntype N = typeof f(1);\n"
    c = Canned(text)
    c.add(
        "imports",
        3,
        **{"import": c.sp("import {", ";"), "name": c.nd("makeStore", "{ makeStore"), "module": c.nd("'./store'")},
    )
    c.add(
        "imports", 3, **{"import": c.sp("import {", ";"), "name": c.nd("Mode", ", Mode"), "module": c.nd("'./store'")}
    )
    for sub, ctx in (
        ("makeStore", "typeof makeStore"),
        ("Mode", "typeof Mode"),
        ("cfg.api", "typeof cfg.api"),
        ("f(1)", "typeof f(1)"),
    ):
        c.add("refs", 30, **{"ref.ref": c.nd(sub, ctx), "name": c.nd(sub, ctx)})
    use = extract(text, c, "typescript", "src/types.ts")
    assert {(r.name, r.kind) for r in use.refs} == {
        ("makeStore", "ref"),
        ("Mode", "ref"),
        ("cfg.api", "ref"),
    }  # a call is no name
    st = "export function makeStore() {}\nexport enum Mode { A }\nexport function unused() {}\n"
    cs = Canned(st)
    cs.add(
        "defs", 0, **{"def.func": cs.sp("function makeStore", "{}"), "name": cs.nd("makeStore", "function makeStore")}
    )
    cs.add("defs", 0, **{"def.func": cs.sp("function unused", "{}"), "name": cs.nd("unused", "function unused")})
    cs.add("defs", 2, **{"def.class": cs.sp("enum Mode", "}"), "name": cs.nd("Mode", "enum Mode")})
    for n, ctx in (
        ("makeStore", "export function makeStore"),
        ("Mode", "export enum"),
        ("unused", "export function unused"),
    ):
        cs.add("exports", 0, export=cs.nd(ctx), name=cs.nd(n, ctx))
    files = {"src/types.ts": use, "src/store.ts": extract(st, cs, "typescript", "src/store.ts")}
    v = _v({k: _ts(f) for k, f in files.items()}, hints=("src/types.ts",))
    assert v["src/store.ts#makeStore"] in LIVE and v["src/store.ts#Mode"] in LIVE and v["src/store.ts#unused"] == "dead"


# ---------------------------------------------------------------------- computed specifiers that are no string
def _computed(text: str, call: str, req: str, pat: str, path: str = "src/loader.js"):
    c = Canned(text)
    c.add("imports", 14, **{"import": c.nd(call), "req": c.nd(req, call), "pattern": c.nd(pat, call)})
    return extract(text, c, path=path)


@pytest.mark.parametrize(
    "text, call, req, pat, want",
    [
        ("const m = require(name);\n", "require(name)", "require", "name", "import:*"),
        (
            "require(path.join(__dirname, 'handlers', n));\n",
            "require(path.join(__dirname, 'handlers', n))",
            "require",
            "path.join(__dirname, 'handlers', n)",
            "import:./handlers/*",
        ),
        (
            "import(`${root}/x/${n}.js`);\n",
            "import(`${root}/x/${n}.js`)",
            "import",
            "`${root}/x/${n}.js`",
            "import:/*/x/*.js",
        ),
        (
            "const m = import.meta.glob(['./pages/*.tsx', '!./pages/x.tsx']);\n",
            "import.meta.glob(['./pages/*.tsx', '!./pages/x.tsx'])",
            "import.meta.glob",
            "['./pages/*.tsx', '!./pages/x.tsx']",
            "import:./pages/*.tsx",
        ),
        ("fork(require.resolve(file));\n", "require.resolve(file)", "require.resolve", "file", "import:*"),
        ("require(`./c/${n}`);\n", "require(`./c/${n}`)", "require", "`./c/${n}`", "import:./c/*"),
    ],
)
def test_a_require_or_import_with_a_computed_argument_is_always_a_dynsite(text, call, req, pat, want):
    """Counterexample of the review: `require(name)`, `require(path.join(__dirname, ..))`, a template that starts with `${root}` and the
    array form of import.meta.glob produced no import and no DynSite: a silent no-edge case."""
    f = _computed(text, call, req, pat)
    assert [d.pattern for d in f.dyn] == [want] and not [i for i in f.imports if i.module]


def test_other_calls_and_plain_strings_are_no_dynsite():
    assert not _computed("log(name);\n", "log(name)", "log", "name").dyn  # not require-like
    assert not _computed("require('./x');\n", "require('./x')", "require", "'./x'").dyn  # a plain string is @module
    assert tg._specs("'./x'") == [] and tg._specs("") == []
    for g in ("javascript", "typescript"):
        assert "(arguments . (_) @pattern)" in tg._query_text(g, "imports"), g


def test_a_computed_specifier_reaches_what_it_can_name_and_marks_it_dynamic():
    from dead_code_checker.graph.model import F_DYNAMIC

    def page(n):
        return _ts(simple_facts(f"export function {n}() {{}}\n", [n], exports=[(n, f"export function {n}")]))
    files = {
        "src/loader.js": _computed(
            "require(path.join(__dirname, 'handlers', n));\n",
            "require(path.join(__dirname, 'handlers', n))",
            "require",
            "path.join(__dirname, 'handlers', n)",
        ),
        "src/handlers/a.js": page("a"),
        "src/other/b.js": page("b"),
    }
    v = _v(files, hints=("src/loader.js",))
    assert v["src/handlers/a.js#a"] in LIVE and v["src/other/b.js#b"] == "dead"
    everything = {
        **files,
        "src/loader.js": _computed("require(name);\n", "require(name)", "require", "name"),
        "lib/z.js": page("z"),
    }
    v = _v(everything, hints=("src/loader.js",))
    assert (
        v["src/handlers/a.js#a"] in LIVE and v["src/other/b.js#b"] in LIVE
    )  # everything under the directory of the caller
    assert v["lib/z.js#z"] == "dead"
    lk = dlink.link(everything, workers=1)
    assert {n.key for n in lk.graph.nodes if n.flags & F_DYNAMIC} >= {"typescript:src/other/b.js#b"}
    deep = {
        **files,
        "src/loader.js": _computed(
            "import(`${root}/x/${n}.js`);\n", "import(`${root}/x/${n}.js`)", "import", "`${root}/x/${n}.js`"
        ),
        "a/x/b.js": page("p"),
        "x/c.js": page("q"),
        "y/d.js": page("r"),
    }
    v = _v(deep, hints=("src/loader.js",))
    assert (
        v["a/x/b.js#p"] in LIVE and v["x/c.js#q"] in LIVE and v["y/d.js#r"] == "dead"
    )  # x/ at any depth, also at the top


# ---------------------------------------------------------------------- a shard is keyed by its blob: data and mapping code are inputs too
def test_root_extractor_cache_keys_follow_the_data_they_read(monkeypatch):
    import copy

    names = ("npm", "jstool", "html", "sfc")
    base = {rx.name: rx.ext_ver for rx in contracts.root_extractors()}
    assert all(base[n].split("@")[1].count(".") == 1 for n in names), base  # <version>.<sha of the data keys>
    for key, changed in (
        ("js_frameworks", "npm"),
        ("js_tool_files", "jstool"),
        ("html_files", "html"),
        ("js_alias_roots", "sfc"),
        ("interpreters", "npm"),
        ("js_tool_entry_keys", "jstool"),
        ("js_build", "npm"),
    ):
        data = copy.deepcopy(roots.fw())
        v = data[key]
        data[key] = v + "|x$" if isinstance(v, str) else [*v, "(^|/)x$"] if isinstance(v, list) else {**v, "x": ["y"]}
        monkeypatch.setattr(roots, "fw", lambda d=data: d)
        now = {rx.name: rx.ext_ver for rx in contracts.root_extractors()}
        assert now[changed] != base[changed], key
        assert {n: v for n, v in now.items() if n != changed and n in ("settings", "cron", "workflow")} == {
            n: v for n, v in base.items() if n in ("settings", "cron", "workflow")
        }
    monkeypatch.undo()
    assert {rx.name: rx.ext_ver for rx in contracts.root_extractors()} == base


def test_the_mapping_code_and_its_version_are_part_of_the_ts_generic_cache_key(monkeypatch):
    ex = tg.TsGeneric()
    assert ex.ext_ver != "ts_generic@1"  # the facts of this pass differ from the shards of version 1
    before = ex.grammar_sha("typescript")
    monkeypatch.setattr(tg, "_code_sha", lambda: "another mapping")
    assert ex.grammar_sha("typescript") != before and ex.grammar_sha("typescript") != tg.TsGeneric().grammar_sha(
        "shell"
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_changed_framework_table_is_not_served_from_old_root_shards(tmp_path, monkeypatch):
    import copy
    from dead_code_checker.graph import store, vcs

    repo = tmp_path / "r"
    (repo / "web").mkdir(parents=True)
    (repo / "web" / "package.json").write_text('{"dependencies": {"gatsby": "5"}}')
    for args in (("init", "-q"), ("add", "-A")):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    tree = vcs.snapshot(str(repo))
    with store.Store(str(tmp_path / "g" / "graph.sqlite")) as st:
        first = roots.collect(str(repo), tree, st, workers=1)["web/package.json"]
        assert "sibling:./gatsby-node.*" in {h.target for h in first}
        data = copy.deepcopy(roots.fw())
        data["js_frameworks"]["gatsby"] = ["./only-this.*"]
        monkeypatch.setattr(roots, "fw", lambda: data)
        inline_units(monkeypatch)  # the patched table must reach the extraction, which runs in a pool
        second = roots.collect(str(repo), tree, st, workers=1)["web/package.json"]
        assert "sibling:./only-this.*" in {h.target for h in second} and "sibling:./gatsby-node.*" not in {
            h.target for h in second
        }


# ---------------------------------------------------------------------- export var, rest elements, single-file components
def test_export_var_of_an_arrow_function_is_exported():
    """Counterexample of the review: `export var f = () => {}` matched only the lexical_declaration pattern: f stayed private."""
    for g in ("javascript", "typescript"):
        assert (
            "(export_statement declaration: (variable_declaration (variable_declarator name: (identifier) @name))) @export"
            in tg._query_text(g, "exports")
        )
    text = "export var f = () => {};\nvar g = () => {};\n"
    c = Canned(text)
    c.add("defs", 4, **{"def.func": c.nd("f = () => {}"), "name": c.nd("f", "var f")})
    c.add("defs", 4, **{"def.func": c.nd("g = () => {}"), "name": c.nd("g", "var g")})
    c.add("exports", 0, export=c.nd("export var f = () => {};"), name=c.nd("f", "var f"))
    f = extract(text, c, path="src/index.js")
    assert {d.qual: d.vis for d in f.defs if d.qual} == {"f": "exported", "g": "private"}
    v, _ = _graph({"src/index.js": f}, {"package.json": '{"main": "src/index.js"}'}, ["package.json", "src/index.js"])
    assert v["src/index.js#f"] == "alive" and v["src/index.js#g"] == "dead"


@pytest.mark.parametrize("awaited", [False, True])
def test_a_rest_element_of_a_destructured_require_or_import_takes_the_whole_namespace(awaited):
    """Counterexample of the review: `const {a, ...rest} = require(m)` bound only a, but rest can read any other export."""
    for g in ("javascript", "typescript"):
        assert "(object_pattern (rest_pattern (identifier) @ns))" in tg._query_text(g, "imports"), g
    call = "await import('./lib/util.js')" if awaited else "require('./lib/util.js')"
    text = f"function f() {{ const {{ a, ...rest }} = {call}; use(rest); }}\nf();\n"
    c = Canned(text)
    c.add("defs", 0, **{"def.func": c.sp("function f", "}\n"), "name": c.nd("f", "function f")})
    req, bare = ("import", "import('./lib/util.js')") if awaited else ("require", "require('./lib/util.js')")
    call_import(c, bare, req)
    extra = {"await": c.nd(call)} if awaited else {}
    bound_import(c, "{ a, ...rest } = " + call, bare, req, name=c.nd("a", "{ a"), **extra)
    bound_import(c, "{ a, ...rest } = " + call, bare, req, ns=c.nd("rest", "...rest"), **extra)
    c.add("refs", 1, **{"ref.call": c.nd("use(rest)"), "name": c.nd("use")})
    c.add("refs", 1, **{"ref.ref": c.nd("(rest)"), "name": c.nd("rest", "use(rest")})
    c.add("refs", 0, **{"ref.call": c.nd("f()", "}\nf"), "name": c.nd("f", "}\nf()")})
    f = extract(text, c, path="app.js")
    got = {(i.module, i.names, i.alias) for i in f.imports if not i.alias.startswith("__ns")}
    assert got == {("./lib/util.js", ("a",), ""), ("./lib/util.js", (), "rest"), ("./lib/util.js", (), "")}, got
    v = _v({"app.js": f, "lib/util.js": lib_util_facts()}, hints=("app.js",))
    assert {v[f"lib/util.js#{n}"] in LIVE for n in ("a", "def", "unused")} == {
        True
    }  # rest escaped as a value: any export may be read


VUE = '<template><Btn /></template>\n<script setup lang="ts">\nimport Btn from "./components/Btn.vue";\nimport { fmt } from "@/utils/format";\nimport Vue from "vue";\nconst m = await import("../lazy");\n</script>\n'


def test_files_imported_only_from_single_file_components_are_entries():
    """Counterexample of the review: .vue / .svelte / .astro are no language, so what only their script imports had no importer."""
    got = {(h.kind, h.target, h.line, h.strength) for h in roots.extract_hints("src/App.vue", VUE)}
    assert not [t for _, t, _, _ in got if "Btn" in t]  # a component is an entry itself, not code of the graph
    assert ("export", "file:src/utils/format", 4, "hard") in got and (
        "export",
        "file:src/utils/format.ts",
        4,
        "hard",
    ) in got
    assert ("export", "file:src/utils/format/index.tsx", 4, "hard") in got
    assert ("export", "sibling:./../lazy.ts", 6, "hard") in got
    assert not [t for _, t, _, _ in got if "vue" in t]  # a package ("vue") is no file of ours
    svelte = "<script>\n  import { db } from '$lib/db';\n  import './app.css';\n</script>\n<h1>x</h1>\n"
    assert {
        (h.target, h.line)
        for h in roots.extract_hints("src/routes/+page.svelte", svelte)
        if h.target.startswith("file:")
    } >= {("file:src/lib/db.js", 2)}
    assert not [h for h in roots.extract_hints("src/routes/+page.svelte", svelte) if "css" in h.target]  # no code
    astro = "---\nimport Layout from '../layouts/Main';\nconst x = 1;\n---\n<Layout />\n<script>\n  import '../client';\n</script>\n"
    ts = {h.target for h in roots.extract_hints("src/pages/index.astro", astro)}
    assert "sibling:./../layouts/Main.ts" in ts and "sibling:./../client.js" in ts
    for other in ("src/a.js", "src/a.vue.bak", "src/a.html"):
        assert roots.extract_hints(other, VUE) == [], other


def test_the_api_of_a_file_imported_from_a_component_is_alive_without_it_it_is_dead():
    fmt = _ts(
        simple_facts(
            "export function fmt() {}\nexport function other() {}\n",
            ["fmt", "other"],
            exports=[("fmt", "export function fmt"), ("other", "export function other")],
        )
    )
    tree = ["src/App.vue", "src/utils/format.ts"]
    v, rs = _graph({"src/utils/format.ts": fmt}, {"src/App.vue": VUE}, tree)
    assert v["src/utils/format.ts#fmt"] == "alive" and v["src/utils/format.ts#other"] == "alive" and not rs.findings
    v, _ = _graph({"src/utils/format.ts": fmt}, {}, tree)
    assert v["src/utils/format.ts#fmt"] == "dead"


def test_the_entry_scanner_skips_brackets_inside_strings_and_stops_at_the_end_of_the_value():
    text = "module.exports = { entry: 'src/[x]/main.js', output: { path: 'dist/x.js' } };\n"
    assert [(ln, p) for ln, p, _ in roots._entry_paths(text)] == [(1, "src/[x]/main.js")]
    multi = (
        "export default {\n  input: [\n    'a/one.js',\n    'a/two.js',\n  ],\n  output: { file: 'dist/o.js' },\n}\n"
    )
    assert [(ln, p) for ln, p, _ in roots._entry_paths(multi)] == [(3, "a/one.js"), (4, "a/two.js")]
    assert [(p, s) for _, p, s in roots._entry_paths("x = { entry: './a/' + name + '.js' }")] == [
        ("a", True)
    ]  # a fragment: soft


@pytest.mark.skipif(
    bool(_real_grammar()),
    reason=f"tree-sitter javascript/typescript grammars are not installed on this host: {_real_grammar()}",
)
def test_second_wave_constructs_with_the_real_grammars():
    """On a host with the grammars: the patterns of the second pass (variable_declaration exports, rest_pattern, the
    generic first-argument pattern, type_query) must compile and yield these facts. Not run on this host."""
    tg.set_backend(None)
    src = (
        "export var f = () => {};\nconst { a, ...rest } = require('./m');\nrequire(name);\n"
        "require(path.join(__dirname, 'h', n));\nimport(`${root}/x/${n}.js`);\nconst g = import.meta.glob(['./p/*.js']);\n"
        "export * as ns from './n';\n"
    )
    f = contracts.get_extractor("javascript").extract("javascript", "app.js", src)
    assert f.errors == 0 and {d.qual: d.vis for d in f.defs}["f"] == "exported"
    assert {("./m", ("a",), ""), ("./m", (), "rest")} <= {(i.module, i.names, i.alias) for i in f.imports}
    assert {d.pattern for d in f.dyn} >= {"import:*", "import:./h/*", "import:/*/x/*.js", "import:./p/*.js"}
    ts = "export type Store = ReturnType<typeof makeStore>;\nexport function makeStore() {}\n"
    t = contracts.get_extractor("typescript").extract("typescript", "s.ts", ts)
    assert ("makeStore", "ref") in {(r.name, r.kind) for r in t.refs}


# ====================================================================== third pass: paths, computed aliases, page scripts, bundles
def _api(name: str, extra: str = "", ts: bool = True) -> "tg.Facts":
    """`export function <name>() {}` (+ `export function <extra>`): the exports a consumer reaches or not."""
    names = [name, *([extra] if extra else [])]
    text = "".join(f"export function {n}() {{}}\n" for n in names)
    f = simple_facts(text, names, exports=[(n, f"export function {n}") for n in names])
    return _ts(f) if ts else f


@pytest.mark.parametrize(
    "importer, spec, index",
    [
        ("src/sub/use.js", "..", "src/index.js"),
        ("src/sub/use.js", "../", "src/index.js"),
        ("src/use.js", ".", "src/index.js"),
        ("src/use.js", "./", "src/index.js"),
        ("src/use.js", "..", "index.js"),  # the stem of an index file at the project root is ''
        ("src/use.js", "../", "index.js"),
        ("use.js", ".", "index.js"),
        ("use.js", "./", "index.js"),
        ("src/sub/use.js", "../..", "index.js"),
        ("src/sub/use.js", "../../", "index.js"),
        ("src/sub/use.js", "../sub", "src/sub/index.js"),
    ],
)
def test_a_directory_index_spelled_dot_dotdot_or_with_a_slash_is_the_imported_file(importer, spec, index):
    """Counterexample of the review: `import { bar } from ".."` left src/index.js#bar dead (the js branch took only './' and '../')."""
    files = {importer: _app(spec, "bar"), index: _api("bar", "unused")}
    v = _v(files, hints=(importer,))
    assert v[f"{index}#bar"] in LIVE and v[f"{index}#unused"] == "dead", (importer, spec)
    assert v[index] == "alive"  # the file edge too


def test_dot_and_dotdot_are_paths_only_for_js_a_name_with_a_query_is_the_file():
    assert dlink._Ix.resolve_module.__doc__  # the entry point of every specifier
    v = _v({"src/use.js": _app("./a?raw", "bar"), "src/a.js": _api("bar", "other")}, hints=("src/use.js",))
    assert v["src/a.js#bar"] in LIVE and v["src/a.js#other"] == "dead"  # vite: `./a?raw`, `./w?worker`


def test_a_dotted_file_name_is_no_extension_of_the_sibling_stem():
    """Counterexample of the review: with foo.js and foo.utils.js, `from './foo.utils'` resolved to foo.js (dead there, alive here)."""
    for spec in ("./foo.utils", "./foo.utils.js"):
        files = {"src/use.js": _app(spec, "bar"), "src/foo.js": _api("bar"), "src/foo.utils.js": _api("bar")}
        v = _v(files, hints=("src/use.js",))
        assert v["src/foo.utils.js#bar"] in LIVE and v["src/foo.js#bar"] == "dead", spec
    for name in ("service", "config", "types"):  # foo.service next to foo.ts
        files = {
            "src/use.ts": _app(f"./foo.{name}", "bar"),
            "src/foo.ts": _api("bar"),
            f"src/foo.{name}.ts": _api("bar"),
        }
        v = _v({k: _ts(f) for k, f in files.items()}, hints=("src/use.ts",))
        assert v[f"src/foo.{name}.ts#bar"] in LIVE and v["src/foo.ts#bar"] == "dead", name


@pytest.mark.parametrize(
    "spec, tree, want",
    [
        ("./foo.js", ["src/foo.ts"], "src/foo.ts"),  # TS with the output extension
        ("./foo.mjs", ["src/foo.mts"], "src/foo.mts"),
        ("./foo.jsx", ["src/foo.tsx"], "src/foo.tsx"),
        ("./foo", ["src/foo.ts", "src/foo/index.ts"], "src/foo.ts"),  # a file wins over the index of a directory (Node)
        ("./foo/", ["src/foo.ts", "src/foo/index.ts"], "src/foo.ts"),
        ("./foo/index", ["src/foo.ts", "src/foo/index.ts"], "src/foo/index.ts"),
        ("./foo.js", ["src/foo.js", "src/foo.ts"], "src/foo.js"),  # the exact file first
    ],
)
def test_extension_and_directory_spellings_follow_node_resolution(spec, tree, want):
    files = {p: _api("bar") for p in tree}
    v = _v({**files, "src/use.ts": _ts(_app(spec, "bar"))}, hints=("src/use.ts",))
    assert {p for p in tree if v[f"{p}#bar"] in LIVE} == {want}, (spec, tree)


@pytest.mark.parametrize(
    "text, want",
    [
        ("`@/locales/${lang}.ts`", ["/*/locales/*.ts"]),
        ("'@/locales/' + lang + '.js'", ["/*/locales/*.js"]),
        ("`~/plugins/${n}`", ["/*/plugins/*"]),
        ("`$lib/i18n/${n}.json`", ["/*/i18n/*.json"]),
        ("`@org/ui/${n}`", ["/*/ui/*"]),
        ("`components/${n}`", ["/*/components/*"]),  # a base-url path, looked up like the static `components/Button`
        ("`@/${n}`", ["*"]),  # nothing but the alias: every file under the directory of the caller
        ("`@/locales/en`", ["@/locales/en"]),  # no computed part: a module name, resolved like `import '@/locales/en'`
        ("['@/p/*.vue', '!@/p/x.vue', './q/*.ts', 'lodash']", ["./q/*.ts", "/*/p/*.vue"]),
    ],
)
def test_a_computed_specifier_with_an_alias_or_base_url_head_is_a_suffix_glob(text, want):
    """Counterexample of the review: `import(`@/locales/${lang}.ts`)` gave no DynSite while the static `import('@/locales/en')` resolved."""
    assert tg._specs(text) == want


def test_a_lazy_alias_import_reaches_the_files_under_the_alias_directory():
    f = _computed(
        "const m = import(`@/locales/${lang}.ts`);\n",
        "import(`@/locales/${lang}.ts`)",
        "import",
        "`@/locales/${lang}.ts`",
        "src/main.ts",
    )
    assert [d.pattern for d in f.dyn] == ["import:/*/locales/*.ts"] and not [i for i in f.imports if i.module]
    files = {
        "src/main.ts": _ts(f),
        "src/locales/en.ts": _api("en"),
        "src/locales/de.ts": _api("de"),
        "src/pages/locales.ts": _api("page"),
        "src/other/x.ts": _api("x"),
    }
    v = _v(files, hints=("src/main.ts",))
    assert v["src/locales/en.ts#en"] in LIVE and v["src/locales/de.ts#de"] in LIVE
    assert v["src/other/x.ts#x"] == "dead" and v["src/pages/locales.ts#page"] == "dead"
    lodash = _computed("require(`lodash/${f}`);\n", "require(`lodash/${f}`)", "require", "`lodash/${f}`", "src/main.ts")
    v = _v({"src/main.ts": _ts(lodash), "src/other/x.ts": _api("x")}, hints=("src/main.ts",))
    assert v["src/other/x.ts#x"] == "dead"  # a package of the same name that is not in the tree reaches nothing


@pytest.mark.parametrize(
    "stmt, req, mod, want",
    [
        ("import.meta.glob('@/pages/*.ts')", "import.meta.glob", "'@/pages/*.ts'", "import:/*/pages/*.ts"),
        ("import.meta.glob('~/pages/*.ts')", "import.meta.glob", "'~/pages/*.ts'", "import:/*/pages/*.ts"),
        ("require.context('@/pages', true)", "require.context", "'@/pages'", "import:/*/pages/*"),
        ("require.context('./pages', true)", "require.context", "'./pages'", "import:./pages/*"),
    ],
)
def test_bundler_globs_with_an_alias_head_name_files_of_the_tree(stmt, req, mod, want):
    text = f"const m = {stmt};\n"
    c = Canned(text)
    c.add("imports", 12, **{"import": c.nd(stmt), "req": c.nd(req, stmt), "module": c.nd(mod, stmt)})
    f = extract(text, c, "typescript", "src/app.ts")
    assert [d.pattern for d in f.dyn] == [want]
    v = _v({"src/app.ts": f, "src/pages/a.ts": _api("a"), "src/lib/z.ts": _api("z")}, hints=("src/app.ts",))
    assert v["src/pages/a.ts#a"] in LIVE and v["src/lib/z.ts#z"] == "dead"


# ---------------------------------------------------------------------- scripts that a page loads
PAGE = (
    '<html>\n<body onload="init()">\n<button onclick="onSave()">save</button>\n'
    '<script src="app.js"></script>\n<script type="module" src="/src/main.js"></script>\n<script>init()</script>\n</body></html>\n'
)


def _classic_script():
    text = "function init() {}\nfunction onSave() {}\nclass Store { load() {} _hid() {} }\nvar state = 1;\n"
    c = Canned(text)
    c.add("defs", 0, **{"def.func": c.nd("function init() {}"), "name": c.nd("init", "function init")})
    c.add("defs", 0, **{"def.func": c.nd("function onSave() {}"), "name": c.nd("onSave", "function onSave")})
    c.add("defs", 2, **{"def.class": c.sp("class Store", "_hid() {} }"), "name": c.nd("Store", "class Store")})
    c.add("defs", 3, **{"def.method": c.nd("load() {}"), "name": c.nd("load")})
    c.add("defs", 3, **{"def.method": c.nd("_hid() {}"), "name": c.nd("_hid")})
    return extract(text, c, path="app.js")


def test_functions_of_a_classic_script_that_a_page_loads_are_globals_not_dead():
    """Counterexample of the review: app.js was alive but app.js#init and #onSave (called by the page, not by code) were dead."""
    tree = ["index.html", "app.js", "src/main.js"]
    mod = simple_facts(
        "function boot() {}\nfunction unused() {}\nboot();\n", ["boot", "unused"], refs=[("call", "boot();", "boot")]
    )
    v, rs = _graph({"app.js": _classic_script(), "src/main.js": mod}, {"index.html": PAGE}, tree)
    assert {v[f"app.js#{n}"] for n in ("init", "onSave", "Store", "Store.load")} == {"alive"}
    assert v["app.js#Store._hid"] == "dead"  # only the public members of a class are reachable by name from a page
    assert v["src/main.js#boot"] == "alive" and v["src/main.js#unused"] == "dead"  # a module keeps its own scope
    assert {r.kind for r in rs.roots} == {"script", "export", "main"} and not rs.findings
    assert {r.node for r in rs.roots if r.kind == "main"} == {
        "html:index.html"
    }  # html is a language: a served page is an entry (langs.json entry_re)
    v, _ = _graph({"app.js": _classic_script()}, {}, ["app.js"])
    assert v["app.js#init"] == "dead"  # no page, no root


def test_a_script_root_is_hard_and_seeds_top_level_definitions_only():
    rs = roots.resolve(
        {"index.html": roots.extract_hints("index.html", PAGE)},
        {p: "100644" for p in ("app.js", "src/main.js")},
        lambda p: "",
    )
    sc = [r for r in rs.roots if r.kind == "script"]
    assert [(r.node, r.strength, r.trust) for r in sc] == [("javascript:app.js", "hard", "registered")]


def test_what_an_inline_module_script_imports_is_an_entry_of_the_page():
    html = '<script type="module">\n  import { init } from "./app.js";\n  import("@/lazy");\n  init();\n</script>\n<script>import("./classic.js")</script>\n'
    hs = {(h.kind, h.target, h.line) for h in roots.extract_hints("web/index.html", html)}
    assert ("export", "sibling:./app.js", 2) in hs and ("export", "file:src/lazy.ts", 3) in hs
    assert not [h for h in hs if "classic" in h[1]]  # a classic inline script has no import statement
    v, _ = _graph(
        {"web/app.js": _api("init", "other", ts=False)}, {"web/index.html": html}, ["web/index.html", "web/app.js"]
    )
    assert (
        v["web/app.js#init"] == "alive" and v["web/app.js#other"] == "alive"
    )  # the API of an imported file is used by the page


def test_script_tags_after_a_script_without_a_closing_tag_are_all_read():
    html = '<script src="a.js"/>\n<script src="b.js"></script>\n<script src=c.js defer></script>\n<script src="//cdn/x.js"></script>\n'
    got = [(h.kind, h.target) for h in roots.extract_hints("index.html", html)]
    assert got == [("script", "sibling:./a.js"), ("script", "sibling:./b.js"), ("script", "sibling:./c.js")]


# ---------------------------------------------------------------------- built entries named after their flavour
@pytest.mark.parametrize(
    "built, src",
    [
        ("./dist/index.esm.js", "./src/index.ts"),
        ("./dist/index.cjs.js", "./src/index.ts"),
        ("./dist/index.mjs", "./src/index.ts"),
        ("./dist/mylib.umd.min.js", "./src/mylib.ts"),
        ("./dist/mylib.umd.min.js", "./src/index.ts"),  # a bundle of the package: its source is the index
        ("./dist/bundle.js", "./src/bundle.ts"),
        ("./dist/bundle.js", "./src/index.ts"),
        ("./dist/esm/index.esm.js", "./src/index.tsx"),
        ("./index.esm.js", "./src/index.ts"),  # a flavoured bundle at the package root
        ("./dist/cjs/util/deep.production.js", "./src/util/deep.ts"),
    ],
)
def test_flavoured_bundle_names_map_to_their_sources(built, src):
    """Counterexamples of the review: ./dist/index.esm.js gave only ./src/index.esm.ts, ./dist/bundle.js only ./src/bundle.ts."""
    assert src in roots._npm_sources(built), built


def test_a_nested_build_file_and_a_pattern_are_no_bundle_of_the_index():
    assert "./src/index.ts" not in roots._npm_sources("./dist/util/deep.js")
    assert "./src/index.ts" not in roots._npm_sources("./dist/esm/util/*.js")
    assert "./src/index.ts" not in roots._npm_sources("./src/x.js")  # not a build directory, no flavour
    assert (
        roots._plain("a/b.esm") == "a/b" and roots._plain("mylib.umd.min") == "mylib" and roots._plain("min") == "min"
    )


def test_a_package_with_flavoured_bundles_keeps_its_source_api_alive():
    pkg = '{"main": "./dist/index.cjs.js", "module": "./dist/index.esm.js", "unpkg": "./dist/mylib.umd.min.js", "types": "./dist/index.d.ts"}'
    got, rs = _entry_roots(pkg, ["src/index.ts", "src/other.ts"])
    assert got == {"src/index.ts"} and not rs.findings
    v, _ = _graph({"src/index.ts": _api("make", "other")}, {"package.json": pkg}, ["package.json", "src/index.ts"])
    assert v["src/index.ts#make"] == "alive"  # dist is gitignored: the source is the only root
    assert _entry_roots('{"jsdelivr": "./dist/x.min.js"}', ["src/index.js"])[0] == {"src/index.js"}


# ---------------------------------------------------------------------- framework conventions that are data only
@pytest.mark.parametrize(
    "dep, file",
    [
        ("nuxt", "composables/useAuth.ts"),
        ("nuxt", "utils/format.ts"),
        ("nuxt", "components/Btn.ts"),
        ("nuxt", "stores/cart.ts"),
        ("nuxt", "modules/mine.ts"),
        ("nuxt", "app.config.ts"),
        ("nuxt", "app/plugins/x.ts"),
        ("@sveltejs/kit", "src/params/slug.ts"),
        ("@sveltejs/kit", "src/service-worker.ts"),
        ("@sveltejs/kit", "src/hooks.server.ts"),
        ("astro", "src/content.config.ts"),
        ("astro", "src/content/config.ts"),
        ("vuepress", ".vuepress/config.js"),
        ("vuepress", "docs/.vuepress/enhanceApp.js"),
    ],
)
def test_framework_auto_loaded_files_of_the_review_are_roots(dep, file):
    got, _ = _entry_roots(json.dumps({"dependencies": {dep: "1"}}), [file])
    assert got == {file}, (dep, file)


def test_a_vuepress_config_is_a_tool_config_without_the_dependency():
    assert {h.kind for h in roots.extract_hints(".vuepress/config.js", "module.exports = {}")} == {"main"}
    assert {h.kind for h in roots.extract_hints("docs/.vuepress/enhanceApp.ts", "export default {}")} == {"main"}
    assert roots.extract_hints("docs/.vuepress/other.js", "x") == []


MANIFEST = json.dumps(
    {
        "manifest_version": 3,
        "background": {"service_worker": "dist/background.js"},
        "content_scripts": [
            {"matches": ["<all_urls>"], "js": ["content/a.js", "/content/b.js"], "css": ["x.css"]},
            {"js": ["c.js"]},
        ],
        "action": {"default_popup": "popup.html"},
    }
)


def test_webextension_manifest_scripts_are_entries_and_a_built_one_maps_to_its_source():
    got = {(h.kind, h.target) for h in roots.extract_hints("ext/manifest.json", MANIFEST)}
    assert {("export", "sibling:./dist/background.js"), ("export", "sibling:./src/background.ts")} <= got
    assert {
        ("export", "sibling:./content/a.js"),
        ("export", "sibling:./content/b.js"),
        ("export", "sibling:./c.js"),
    } <= got
    assert not [t for _, t in got if "css" in t or "popup" in t]  # a page is read through html_files
    assert roots.extract_hints("manifest.json", '{"name": "a pwa", "start_url": "/"}') == []  # a web app manifest
    assert roots.extract_hints("manifest.json", "not json") == []
    tree = ["ext/manifest.json", "ext/src/background.ts", "ext/content/a.js", "ext/c.js", "ext/lib.js"]
    rs = roots.resolve(
        {"ext/manifest.json": roots.extract_hints("ext/manifest.json", MANIFEST)},
        {p: "100644" for p in tree},
        lambda p: "",
    )
    assert {r.node.split(":", 1)[1] for r in rs.roots} == {"ext/src/background.ts", "ext/content/a.js", "ext/c.js"}
    assert not rs.findings  # dist/background.js and content/b.js are not in the tree: silent


def test_the_root_extractors_of_this_wave_are_keyed_by_the_data_they_read(monkeypatch):
    import copy

    base = {rx.name: rx.ext_ver for rx in contracts.root_extractors()}
    assert (
        "manifest" in base
        and base["html"].split("@")[1].startswith("2.")
        and base["npm"].split("@")[1].startswith("4.")
    )
    for key, changed in (("js_manifest", "manifest"), ("js_build", "manifest"), ("js_alias_roots", "html")):
        data = copy.deepcopy(roots.fw())
        data[key] = {**data[key], "x": ["y"]}
        monkeypatch.setattr(roots, "fw", lambda d=data: d)
        assert {rx.name: rx.ext_ver for rx in contracts.root_extractors()}[changed] != base[changed], key
    monkeypatch.undo()


# ---------------------------------------------------------------------- class expressions keep their base
def test_the_class_expression_patterns_are_in_the_refs_queries_of_both_grammars():
    js, ts = tg._query_text("javascript", "refs"), tg._query_text("typescript", "refs")
    assert "(class (class_heritage (identifier) @name)) @ref.inherit" in js
    assert "(class (class_heritage (member_expression) @name)) @ref.inherit" in js
    assert "(class (class_heritage (extends_clause value: (identifier) @name))) @ref.inherit" in ts
    assert "(class (class_heritage (implements_clause (type_identifier) @name))) @ref.impl" in ts
    assert "(class (class_heritage" in tg._query_text(
        "tsx", "refs"
    )  # tsx reads the typescript file first (query_alias)


@pytest.mark.parametrize("head", ["export default class extends Base {}", "const Mixed = class extends Base {};"])
def test_an_anonymous_default_or_expression_class_keeps_its_base_alive(head):
    """Counterexample of the review: the heritage query named class_declaration only, so a base used only by an anonymous class was dead."""
    text = "export class Base { run() {} }\n"
    c = Canned(text)
    c.add("defs", 2, **{"def.class": c.sp("class Base", "}"), "name": c.nd("Base", "class Base")})
    c.add("defs", 3, **{"def.method": c.nd("run() {}"), "name": c.nd("run")})
    c.add("exports", 0, export=c.sp("export class", "}"), name=c.nd("Base", "class Base"))
    base = extract(text, c, path="base.js")
    use = f"import {{ Base }} from './base.js';\n{head}\n"
    u = Canned(use)
    u.add(
        "imports", 3, **{"import": u.sp("import", ";"), "name": u.nd("Base", "{ Base"), "module": u.nd("'./base.js'")}
    )
    u.add(
        "refs", 13, **{"ref.inherit": u.sp("class extends", "Base"), "name": u.nd("Base", "extends Base")}
    )  # scope: the file
    files = {"base.js": base, "use.js": extract(use, u, path="use.js")}
    assert [(r.name, r.kind, r.scope) for r in files["use.js"].refs] == [("Base", "inherit", "")]
    v = _v(files, hints=("use.js",))
    assert v["base.js#Base"] in LIVE


@pytest.mark.skipif(
    bool(_real_grammar()),
    reason=f"tree-sitter javascript/typescript grammars are not installed on this host: {_real_grammar()}",
)
def test_third_wave_constructs_with_the_real_grammars():
    """On a host with the grammars: the class-expression heritage patterns and the alias globs of the third pass must
    compile and yield these facts. Not run on this host."""
    tg.set_backend(None)
    src = (
        "export default class extends Base {}\nconst A = class extends ns.Mid {};\n"
        "const m = import(`@/locales/${lang}.ts`);\nconst g = import.meta.glob('~/pages/*.ts');\n"
        "const P = lazy(() => import(/* webpackChunkName: \"p\" */ './Page'));\n"
    )
    f = contracts.get_extractor("javascript").extract("javascript", "app.js", src)
    assert f.errors == 0
    assert {("Base", "inherit", ""), ("ns.Mid", "inherit", "")} <= {(r.name, r.kind, r.scope) for r in f.refs}
    assert {d.pattern for d in f.dyn} >= {"import:/*/locales/*.ts", "import:/*/pages/*.ts"}
    assert "./Page" in {i.module for i in f.imports}  # a comment before the specifier
    t = contracts.get_extractor("typescript").extract(
        "typescript", "a.ts", "export default class extends B implements I {}\n"
    )
    assert {("B", "inherit", ""), ("I", "impl", "")} <= {(r.name, r.kind, r.scope) for r in t.refs}


# ---------------------------------------------------------------------- a comment in front of the argument of import()
def test_a_comment_in_front_of_the_specifier_is_no_part_of_it():
    assert tg._specs('/* webpackChunkName: "x" */') == []  # the comment alone: the module is the other match
    assert tg._specs('/* webpackChunkName: "p" */ `./pages/${n}.js`') == ["./pages/*.js"]
    assert tg._specs('// c\n"./a"') == [] and tg._specs("/* a */ /* b */ name") == ["*"]
    assert tg._specs("'//cdn/x' + n") == ["//cdn/x*"]  # a `//` inside a string is no comment
    for g in ("javascript", "typescript"):
        q = tg._query_text(g, "imports")
        assert "(arguments (comment) . (string) @module)" in q and "(arguments (comment) . (_) @pattern)" in q, g


def test_a_webpack_magic_comment_before_a_lazy_module_is_read():
    """`lazy(() => import(/* webpackChunkName: "p" */ './Page'))`: the comment is a named child, the string no longer the first one."""
    text = "const P = lazy(() => import(/* webpackChunkName: \"p\" */ './Page'));\n"
    c = Canned(text)
    call = c.nd("import(/* webpackChunkName: \"p\" */ './Page')")
    c.add("imports", 14, **{"import": call, "req": c.nd("import"), "pattern": c.nd('/* webpackChunkName: "p" */')})
    c.add("imports", 13, **{"import": call, "req": c.nd("import"), "module": c.nd("'./Page'")})
    f = extract(text, c, path="app.js")
    assert [(i.module, i.names) for i in f.imports if i.module] == [("./Page", ())] and not f.dyn
    v = _v({"app.js": f, "Page.js": _api("Page", "other", ts=False)}, hints=("app.js",))
    assert v["Page.js#Page"] in LIVE and v["Page.js#other"] in LIVE  # the namespace of a lazy module is used as a whole


# ---------------------------------------------------------------------- E1: a value of a module read through its members
# The counterexamples of an earlier re-check, with the REAL grammars (skipped with the reason when they are not installed): an import
# whose name no def of its file answers (`export const api = new ApiClient()` is no def) used to be external, so `api.get()`
# reached no member; a CommonJS module value (`module.exports = Foo`) was only looked up for top-level defs.
VALUE_CASES = {
    "named-instance": (
        {"util.js": "class ApiClient { get() {} spare() {} }\nexport const api = new ApiClient();\n"},
        "import { api } from './util';\napi.get();\n",
        "ApiClient",
    ),
    "default-instance": (
        {"svc.js": "class S { get() {} spare() {} }\nexport default new S();\n"},
        "import svc from './svc';\nsvc.get();\n",
        "S",
    ),
    "export-list": (
        {"util.js": "class ApiClient { get() {} spare() {} }\nconst api = new ApiClient();\nexport { api };\n"},
        "import { api } from './util';\napi.get();\n",
        "ApiClient",
    ),
    "barrel": (
        {
            "util.js": "class ApiClient { get() {} spare() {} }\nexport const api = new ApiClient();\n",
            "index.js": "export * from './util';\n",
        },
        "import { api } from './index';\napi.get();\n",
        "ApiClient",
    ),
    "cjs-destructured": (
        {"util.js": "class ApiClient { get() {} spare() {} }\nexports.api = new ApiClient();\n"},
        "const { api } = require('./util');\napi.get();\n",
        "ApiClient",
    ),
    "cjs-instance": (
        {"foo.js": "class Foo { get() {} spare() {} }\nmodule.exports = new Foo();\n"},
        "const foo = require('./foo');\nfoo.get();\n",
        "Foo",
    ),
    "cjs-class-static": (
        {"foo.js": "class Foo { static get() {} spare() {} }\nmodule.exports = Foo;\n"},
        "const Foo = require('./foo');\nFoo.get();\n",
        "Foo",
    ),
    "ts-instance": (
        {"util.ts": "class ApiClient { get(): void {} spare(): void {} }\nexport const api = new ApiClient();\n"},
        "import { api } from './util';\napi.get();\n",
        "ApiClient",
    ),
}


@pytest.mark.skipif(
    bool(_real_grammar()), reason=f"tree-sitter javascript/typescript grammars are not installed: {_real_grammar()}"
)
@pytest.mark.parametrize("case", sorted(VALUE_CASES))
def test_a_method_read_through_an_exported_instance_or_a_module_value_is_not_dead(case):
    srcs, use, cls = VALUE_CASES[case]
    tg.set_backend(None)
    lang = "typescript" if case.startswith("ts-") else "javascript"
    files = {p: contracts.get_extractor(lang).extract(lang, p, t) for p, t in srcs.items()}
    files["use.ts" if lang == "typescript" else "use.js"] = contracts.get_extractor(lang).extract(lang, "use.js", use)
    got = _v(files, hints=(next(p for p in files if p.startswith("use.")),))
    mod = next(p for p in srcs if p.split(".")[0] in ("util", "svc", "foo"))
    assert got[f"{mod}#{cls}.get"] == "weak-alive"  # N: the receiver is a value, its type is not followed
    assert got[f"{mod}#{cls}.spare"] == "dead"  # a member nobody reads stays dead
    assert got[f"{mod}#{cls}"] in LIVE


@pytest.mark.skipif(
    bool(_real_grammar()), reason=f"tree-sitter javascript/typescript grammars are not installed: {_real_grammar()}"
)
def test_an_import_that_no_file_of_the_tree_answers_stays_external_and_reads_nothing():
    tg.set_backend(None)
    ex = contracts.get_extractor("javascript")
    files = {
        "lib.js": ex.extract("javascript", "lib.js", "class Client { get() {} }\nexport const x = 1;\n"),
        "use.js": ex.extract("javascript", "use.js", "import { api } from 'some-package';\napi.get();\n"),
    }
    assert (
        _v(files)["lib.js#Client.get"] == "dead"
    )  # `some-package` is no file of the tree: nothing of ours is read from it
