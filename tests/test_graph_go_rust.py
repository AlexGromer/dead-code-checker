"""Go and Rust extraction: the queries as data, the facts mapping, the golden mini-repos.

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_go_rust.py

The mapping is exercised through the ts_generic backend seam with the recorded matches of tests/fixtures/{go,rust}/<case>/parse.json
(tests/recorded_parse.py; the sources are <file>.txt), tied to the .scm files by pattern index and capture names, so it needs no
grammar. The tests marked "real grammar" run the queries on the installed grammar and skip with an explicit reason when tree-sitter
or the grammar is missing. The golden verdicts test the MAPPING (facts -> graph -> verdict); that a query matches real syntax is
the business of the real-grammar tests, so each construct fixed after a review is also listed there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test

ROOT = Path(__file__).resolve().parent.parent
import recorded_parse as rp  # noqa: E402
from dead_code_checker.graph import contracts, link as dlink, model as md, reach as rc, roots as rootsm, ts as tsm  # noqa: E402
from dead_code_checker.graph.extract import ts_generic as tg, ts_lang as tl  # noqa: E402
from dead_code_checker.graph.model import Facts, RootHint  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
CASES = [("go", "app", "go"), ("rust", "crate", "rust")]  # (lang, case dir, grammar)
SPLIT = (
    [  # consts, enums, values, types and impls spread over files; stdlib interface methods and /v2 imports; attributes
        ("go", "split", "go"),
        ("rust", "split", "rust"),
        ("go", "ext", "go"),
        ("rust", "attrs", "rust"),
        ("go", "vers", "go"),  # /vN import paths, //go:linkname, a blank var
        ("rust", "assoc", "rust"),  # associated types and consts
        ("rust", "gates", "rust"),  # cfg and cfg_attr, unsafe(...) attributes, #[used] statics, const _
        ("rust", "inline", "rust"),  # items of inline `mod`s imported with use, crate:: paths, a nested turbofish
        ("go", "shadow", "go"),  # a local or parameter named like an imported package
        ("go", "embed", "go"),  # embedded fields of another package, behind a pointer, of the project
        ("go", "protocol", "go"),  # flag.Value, sync.Locker, ast.Visitor, hash.Hash, context.Context methods
        ("rust", "path", "rust"),  # #[path] and cfg_attr(path) on `mod x;`, cfg-gated twins of one mod
        ("rust", "workspace", "rust"),  # three packages of a cargo workspace that import each other by directory name
    ]
)
ALL = CASES + SPLIT
VOCAB = {
    "go": {
        "defs": {"def.method", "def.func", "def.iface", "def.class", "def.const", "def.var", "name", "recv"},
        "refs": {
            "ref.call",
            "ref.inherit",
            "ref.ref",
            "ref.exec",
            "name",
            "obj",
            "import",
            "local",
            "cmd",
            "dyn.members",
        },
        "roots": {"root.marker", "root.cand", "name", "pkg"},
        "stub": {"stub.def", "name", "body"},
    },
    "rust": {
        "defs": {
            "def.method",
            "def.func",
            "def.iface",
            "def.class",
            "def.const",
            "def.var",
            "def.module",
            "name",
            "recv",
            "vis",
            "flag.protocol",
            "flag.abstract",
        },
        "refs": {"ref.call", "ref.impl", "ref.inherit", "ref.ref", "name", "obj", "scope", "member", "import", "dyn"}
        | {"attr", "attr.item", "item_macro", "item_body"},
        "roots": {"root.marker", "root.cand", "root.attr", "root.span", "name", "attr"},
        "stub": {"stub.def", "name", "body"},
    },
}


@pytest.fixture(autouse=True)
def _no_backend():
    yield
    tg.set_backend(None)


def load_case(lang: str, case: str):
    base = FIX / lang / case
    rec = json.loads((base / "parse.json").read_text(encoding="utf-8"))
    return {
        f: ((base / (f + ".txt")).read_text(encoding="utf-8"), m) for f, m in rec.items()
    }, rec  # stored as <file>.txt: analyzers and the repo self-check must not read them as code


def install(srcs):
    """The seam: the recorded parse of whichever file the extractor is asked about (the backend sees only the bytes)."""
    by_text = {text: m for text, m in srcs.values()}
    tg.set_backend(lambda grammar, data: rp.Recorded(data.decode(), by_text[data.decode()]))


def extract_all(lang: str, case: str):
    srcs, _ = load_case(lang, case)
    install(srcs)
    ex = contracts.get_extractor(lang)
    return {f: ex.extract(lang, f, text) for f, (text, _) in srcs.items()}


def facts_of(lang: str, case: str, path: str) -> Facts:
    return extract_all(lang, case)[path]


def defs(f: Facts):
    return {d.qual: d for d in f.defs}


def verdicts(lang: str, case: str, mode: str):
    files = extract_all(lang, case)
    # the test roots are the production ones: graph.roots.resolve decides by path which files are tests
    found = rootsm.resolve({}, {f: "100644" for f in files}, lambda p: None)
    tests = [r.node.split(":", 1)[1] for r in found.roots if r.kind == "test"]
    assert sorted(tests) == sorted(f for f in files if f.endswith("_test.go"))
    hints = {f: [RootHint("test", "file:" + f, 1, "soft", "convention")] for f in tests}
    res = dlink.link(files, hints, workers=1)
    assert not res.findings, res.findings
    rr = rc.analyze(res.graph, mode)
    return {n.key.split(":", 1)[1]: rr.verdict(n.key) for n in res.graph.nodes if n.kind != "file"}, res


# ====================================================================== queries are data
@pytest.mark.parametrize("lang,case,grammar", CASES)
def test_queries_exist_and_use_the_documented_vocabulary(lang, case, grammar):
    qdir = Path(tg.QUERY_DIR) / grammar
    assert sorted(p.stem for p in qdir.glob("*.scm")) == [
        "defs",
        "refs",
        "roots",
        "stub",
        "xlang",
    ]  # imports / exports.scm are JS-only
    assert set(p.stem for p in qdir.glob("*.scm")) <= set(tg.QUERY_KINDS)
    for kind, ok in VOCAB[lang].items():
        text = re.sub(r";[^\n]*", "", (qdir / f"{kind}.scm").read_text(encoding="utf-8"))
        assert text.count("(") == text.count(")") and text.count("[") == text.count("]"), kind
        used = set(re.findall(r"@([A-Za-z_.]+)", text))
        assert used and used <= ok, (kind, used - ok)
        assert "#" not in text, "queries carry no predicates"
    assert contracts.get_extractor(lang).name == "ts_generic"


@pytest.mark.parametrize("lang,case,grammar", ALL)
def test_recorded_parses_fit_their_query_files(lang, case, grammar):
    _, rec = load_case(lang, case)
    assert rp.check_patterns(Path(tg.QUERY_DIR) / grammar, rec) > 10


def test_a_recorded_match_that_does_not_fit_the_query_is_refused():
    with pytest.raises(AssertionError):
        rp.check_patterns(Path(tg.QUERY_DIR) / "go", {"a.go": {"defs": [[99, {"def.func": "x", "name": "x"}]]}})
    with pytest.raises(AssertionError):
        rp.check_patterns(Path(tg.QUERY_DIR) / "go", {"a.go": {"defs": [[4, {"def.func": "x", "ghost": "x"}]]}})


def test_grammar_sha_of_go_and_rust_follows_their_queries():
    shas = {contracts.get_extractor(lang).grammar_sha(lang) for lang in ("go", "rust", "shell")}
    assert len(shas) == 3 and all(re.fullmatch(r"[0-9a-f]{64}", s) for s in shas)


def test_grammar_sha_follows_the_rules_of_the_language_so_a_changed_rule_leaves_no_stale_shard(monkeypatch):
    """The Go protocol names are data in rules.json: facts cached before a new name was added carry no protocol flag."""
    ex, rust = contracts.get_extractor("go"), contracts.get_extractor("rust")
    base, rust_base = ex.grammar_sha("go"), rust.grammar_sha("rust")
    rules = json.loads(json.dumps(tl._all()))
    rules["go"]["protocol_names"].append("Brand")
    with monkeypatch.context() as m:
        m.setattr(tl, "_all", lambda: rules)
        tl.rules_sha.cache_clear()
        assert ex.grammar_sha("go") != base and rust.grammar_sha("rust") == rust_base
    tl.rules_sha.cache_clear()
    assert ex.grammar_sha("go") == base and tl.rules_sha("klingon") == ""


# ====================================================================== golden mini-repos
@pytest.mark.parametrize("lang,case,grammar", ALL)
def test_golden_verdicts(lang, case, grammar):
    exp = json.loads((FIX / lang / case / "expected.json").read_text(encoding="utf-8"))
    got, _ = verdicts(lang, case, "closed_world")
    assert got == exp["closed_world"]
    lib, _ = verdicts(lang, case, "library")
    assert {k: v for k, v in lib.items() if got[k] != v} == exp["library"]


@pytest.mark.parametrize("lang,case,grammar", CASES)
def test_golden_expectations_cover_dead_cluster_and_test_only_where_the_language_has_them(lang, case, grammar):
    exp = json.loads((FIX / lang / case / "expected.json").read_text(encoding="utf-8"))["closed_world"]
    kinds = set(exp.values())
    assert {"alive", "weak-alive", "dead", "dead-cluster"} <= kinds
    assert ("test-only" in kinds) == (
        lang == "go"
    )  # go: *_test.go is a test file; rust: #[test] is a soft annotation root


@pytest.mark.parametrize("lang,case,grammar", ALL)
def test_the_graph_does_not_depend_on_file_order_or_worker_count(lang, case, grammar):
    files = extract_all(lang, case)
    base = dlink.link(files, workers=1).graph.digest()
    assert dlink.link(dict(reversed(list(files.items()))), workers=1).graph.digest() == base
    assert dlink.link(files, workers=2).graph.digest() == base


@pytest.mark.parametrize("lang,case,grammar", ALL)
def test_facts_are_a_pure_function_of_the_text_and_survive_the_shard_roundtrip(lang, case, grammar):
    a, b = extract_all(lang, case), extract_all(lang, case)
    for p, f in a.items():
        assert f.to_payload() == b[p].to_payload()
        assert Facts.from_payload(f.to_payload()).to_payload() == f.to_payload()


# ====================================================================== go facts
def test_go_defs_receivers_interfaces_and_visibility():
    d = defs(facts_of("go", "app", "shapes.go"))
    assert {q: (x.kind, x.vis) for q, x in d.items() if q} == {
        "Shape": ("iface", "exported"),
        "Shape.Area": ("method", "exported"),  # an interface method is a member of the interface (CHA candidate)
        "Circle": ("class", "exported"),  # the struct pattern (6) wins over the any-type pattern (7) on one node
        "Circle.Area": ("method", "exported"),  # a pointer receiver
        "Circle.Perimeter": ("method", "exported"),  # a value receiver
        "Named": ("class", "exported"),
        "Orphan": ("class", "exported"),
    }
    m = defs(facts_of("go", "app", "main.go"))
    assert m["run"].vis == "package" and m["setup"].stub == "empty body" and m["main"].stub == ""


def test_go_embedding_is_inheritance_and_a_type_name_or_receiver_is_no_use_of_itself():
    f = facts_of("go", "app", "shapes.go")
    assert {(r.name, r.kind, r.scope) for r in f.refs} == {
        ("Circle", "inherit", "Named"),
        ("c.R", "ref", "Circle.Area"),
    }


def test_go_calls_package_aliases_and_exec_directives():
    f = facts_of("go", "app", "main.go")
    r = {(x.name, x.kind, x.scope) for x in f.refs}
    assert ("Helper", "call", "main") in r  # util.Helper(): util is an imported package, the bare name is matched
    assert ("s.Area", "call", "run") in r  # a member call on a value: members named Area
    assert ("Println", "call", "run") in r and ("run", "call", "main") in r
    assert ("Shape", "ref", "run") in r and ("Circle", "ref", "run") in r  # type uses
    assert ("tools/gen.go", "exec", "") in r  # //go:generate go run tools/gen.go
    assert not any(n == "s.Area" and k == "ref" for n, k, _ in r)  # the weak selector ref on a called node is dropped
    assert f.imports == () and f.dyn == ()


def test_go_roots_main_only_in_package_main_init_always():
    f = facts_of("go", "app", "main.go")
    assert [(r.kind, r.target, r.strength, r.trust) for r in f.roots] == [
        ("init", "init", "hard", "convention"),
        ("main", "main", "hard", "convention"),
    ]
    assert facts_of("go", "app", "util/util.go").roots == ()  # package util
    assert tl.name_roots(tl.rules("go"), "main", {"util"}) == [] and tl.name_roots(tl.rules("go"), "main", set()) == []
    assert tl.name_roots(tl.rules("go"), "init", {"util"}) == [("init", "hard", "convention")]


def test_go_export_directive_and_marker_roots():
    rl = tl.rules("go")
    assert tl.comment_roots(rl, "//export Callback") == [("Callback", "export", "hard", "convention")]
    assert tl.comment_roots(rl, "// export Callback") == [] and tl.comment_roots(rl, "//exported Foo") == []
    src = "package main\n\n//export Callback\nfunc Callback() {}\n\n// deadcode: root plugin entry\nfunc plugin() {}\n"
    m = {
        "defs": [
            [4, {"def.func": "func Callback() {}", "name": "Callback"}],
            [4, {"def.func": "func plugin() {}", "name": "plugin"}],
        ],
        "roots": [[0, {"root.marker": "//export Callback"}], [0, {"root.marker": "// deadcode: root plugin entry"}]],
    }
    tg.set_backend(lambda g, d: rp.Recorded(src, m))
    f = contracts.get_extractor("go").extract("go", "x.go", src)
    assert sorted((r.kind, r.target, r.trust) for r in f.roots) == [
        ("export", "Callback", "convention"),
        ("manual", "plugin", "registered"),
    ]
    ghost = "package main\n\n//export Ghost\n"  # an export of a name that is no def of the file adds nothing
    tg.set_backend(lambda g, d: rp.Recorded(ghost, {"roots": [[0, {"root.marker": "//export Ghost"}]]}))
    assert contracts.get_extractor("go").extract("go", "x.go", ghost).roots == ()


def test_go_import_alias_rules():
    assert tl._go_alias('"fmt"') == {"fmt", "fmt", "fmt"}
    assert tl._go_alias('yaml "gopkg.in/yaml.v3"') == {"yaml"}
    assert {"foo", "go-foo"} <= tl._go_alias('"github.com/x/go-foo"')
    assert tl._go_alias('"gopkg.in/yaml.v3"') >= {"yaml"}
    assert (
        tl._go_alias('_ "embed"') == set() and tl._go_alias('. "strings"') == set() and tl._go_alias("garbage") == set()
    )
    assert tl._go_alias('"example.com/lib/v2"') == {"lib", "v2"} and tl._go_alias('"github.com/x/foo.go"') >= {"foo"}
    assert tl.ref_name({}, "Helper", "util", {"util"}) == "Helper" and tl.ref_name({}, "Do", "x", {"util"}) == "x.Do"
    assert tl.ref_name({}, "Do", "a.b().c", set()) == "_.Do"  # a receiver that is no plain path: members of that name


def test_go_exec_directive_words():
    rl = tl.rules("go")
    assert tl.exec_names(rl, "//go:generate go run tools/gen.go -out x") == ["tools/gen.go"]
    assert tl.exec_names(rl, "//go:generate stringer -type=Pill") == []
    assert (
        tl.exec_names(rl, "// go:generate go run x.go") == [] and tl.exec_names({}, "//go:generate go run x.go") == []
    )


def test_go_major_version_import_and_multi_name_specs_and_stdlib_interface_methods():
    f = facts_of("go", "ext", "main.go")
    r = {(x.name, x.kind, x.scope) for x in f.refs}
    assert ("Open", "call", "main") in r and ("Push", "call", "main") in r  # lib/v2 -> lib, heap: the bare name
    d = defs(f)
    assert {q: x.kind for q, x in d.items() if q in ("Left", "Right")} == {"Left": "var", "Right": "var"}
    assert not any("." in q for q in d if q in ("Left.Right", "Right.Left"))  # siblings, not nested
    flagged = {q for q, x in d.items() if x.flags & md.F_PROTOCOL}
    assert flagged == {"IntHeap.Push", "IntHeap.Pop"}  # Len is known to the linker; Unused is no interface method
    assert not defs(facts_of("go", "ext", "lib/lib.go"))["Close"].flags & md.F_PROTOCOL  # a function, not a method


def test_go_protocol_names_are_data_and_apply_to_methods_only():
    rl = tl.rules("go")
    names = ("Push", "Pop", "Header", "WriteHeader", "Flush", "Seek", "Scan", "Value", "MarshalText", "Is", "Format")
    names += ("RoundTrip", "WithAttrs", "Reconcile", "ValidateCreate")  # http, slog, controller-runtime interfaces
    assert all(tl.protocol(rl, n) for n in names)
    assert not tl.protocol(rl, "Open") and not tl.protocol({}, "Push") and not tl.protocol(tl.rules("rust"), "Push")


def test_two_defs_on_one_span_are_siblings_a_nested_one_still_nests():
    src = "var A, B = 1, 2\nfunc f() {\n}\n"
    m = {
        "defs": [
            [10, {"def.var": "A, B = 1, 2", "name": "A"}],
            [10, {"def.var": "A, B = 1, 2", "name": "B"}],
            [4, {"def.func": "func f() {…\n}", "name": "f"}],
        ]
    }
    tg.set_backend(lambda g, d: rp.Recorded(src, m))
    assert sorted(defs(contracts.get_extractor("go").extract("go", "x.go", src))) == ["", "A", "B", "f"]


# ====================================================================== rust facts
def test_rust_defs_impl_methods_traits_and_visibility():
    d = defs(facts_of("rust", "crate", "src/shapes.rs"))
    got = {q: (x.kind, x.vis) for q, x in d.items() if q}
    assert got["Shape"] == ("iface", "public") and got["Shape.area"] == (
        "method",
        "public",
    )  # trait members follow the trait
    assert got["Square"] == ("class", "public") and got["Square.new"] == ("method", "public")
    assert got["Square.dead_method"] == ("method", "private") and got["Square.area"] == ("method", "private")
    assert got["tests"] == ("module", "private") and got["tests.square_area"] == (
        "func",
        "private",
    )  # items of an inline mod
    assert d["Shape.area"].flags & md.F_ABSTRACT and not d["Shape.label"].flags & md.F_ABSTRACT
    assert (
        d["Square.area"].flags & md.F_PROTOCOL and not d["Square.dead_method"].flags & md.F_PROTOCOL
    )  # trait impl method
    assert [q for q in d if q.endswith(".area")] == [
        "Shape.area",
        "Square.area",
    ], "one node, one def: the plain fn pattern must not add a second area"


def test_rust_impl_is_an_impl_edge_from_the_derived_type_and_weak_type_refs_at_names_are_dropped():
    f = facts_of("rust", "crate", "src/shapes.rs")
    r = {(x.name, x.kind, x.scope) for x in f.refs}
    assert ("Shape", "impl", "Square") in r
    assert ("Square", "ref", "Square.new") in r and ("String.from", "call", "Shape.label") in r
    # the trait / type names in `impl Shape for Square {`, `pub struct Square` are no uses from the file scope
    assert not any(k == "ref" and s == "" for _, k, s in r)


def test_rust_roots_attributes_and_main():
    f = facts_of("rust", "crate", "src/shapes.rs")
    assert [(r.kind, r.target, r.strength) for r in f.roots] == [
        ("annotation:rust-ffi", "ffi_entry", "hard"),  # #[no_mangle]
        ("annotation:rust-test", "tests.square_area", "soft"),  # #[test]; not kind test, see data/rules.json
    ]
    assert [(r.kind, r.target) for r in facts_of("rust", "crate", "src/main.rs").roots] == [("main", "main")]
    assert facts_of("rust", "crate", "src/utils.rs").roots == ()


def test_rust_attribute_owner_skips_stacked_attributes_comments_and_whitespace():
    src = '#[test]\n#[ignore = "x[y]"]\n/// doc\n// note\n#[tokio::test]\nfn t() {}\n\n#[no_mangle]\nfn other() {}\n'
    src += "#[derive(Debug)] struct S;\n"
    at = lambda s: src.index(s)  # noqa: E731
    attrs = [
        (at("#[test]"), at("#[test]") + 7, "test"),
        (at("#[ignore"), at("/// doc") - 1, "ignore"),
        (at("#[tokio"), at("#[tokio") + 15, "tokio::test"),
        (at("#[no_mangle]"), at("#[no_mangle]") + 12, "no_mangle"),
        (at("#[derive"), at("#[derive") + 16, "derive"),
    ]
    starts = {at("fn t"), at("fn other"), at("struct S")}
    owners = tl.attr_owners(src.encode(), attrs, starts)
    assert sorted((t, o) for t, o, _ in owners) == sorted(
        [
            ("test", at("fn t")),
            ("ignore", at("fn t")),
            ("tokio::test", at("fn t")),
            ("no_mangle", at("fn other")),
            ("derive", at("struct S")),
        ]
    )
    assert (
        tl.attr_owners(b"#[test]\nlet x = 1;\nfn t() {}\n", [(0, 7, "test")], {18}) == []
    )  # something else in between


def test_rust_attribute_rules():
    rl = tl.rules("rust")
    kinds = lambda a: [k for k, _, _ in tl.attr_roots(rl, a)]  # noqa: E731
    assert kinds("test") == kinds("tokio::test") == kinds("rstest") == ["annotation:rust-test"]
    assert kinds("tokio::main") == kinds("actix_web::main") == ["main"]
    assert kinds("no_mangle") == kinds("wasm_bindgen") == kinds("macro_export") == ["annotation:rust-ffi"]
    assert kinds("cfg") == kinds("derive") == kinds("inline") == kinds("testing") == []


def test_rust_use_and_mod_declarations_become_imports():
    def imp(t):
        return sorted(
            (i.module, i.names, i.alias) for i in tl.imports(tl.rules("rust"), t, 3, "s")[0]
        )  # noqa: E731
    assert imp("use crate::utils::helper;") == [("crate::utils", ("helper",), "")]
    assert imp("pub use a::b as c;") == [("a", ("b",), "c")]
    assert imp("use a::{b, c::d, e as f, self, *};") == [
        ("a", (), ""),
        ("a", ("*",), ""),
        ("a", ("b",), ""),
        ("a", ("e",), "f"),
        ("a::c", ("d",), ""),
    ]
    assert imp("use a::b::{self as m};") == [("a::b", (), "m")]
    assert imp("use ::std::io;") == [("std", ("io",), "")] and imp("use serde;") == [("serde", (), "")]
    assert imp("use a::{b::{c, d}, e};") == [("a", ("e",), ""), ("a::b", ("c",), ""), ("a::b", ("d",), "")]
    assert imp("pub(crate) mod x;") == [("self::x", (), "x")] and imp("mod x {}") == [] and imp("garbage") == []
    assert (
        tl.imports(tl.rules("rust"), "use a::b;", 7, "f")[0][0].line == 7
        and tl.imports(tl.rules("rust"), "use a::b;", 7, "f")[0][0].scope == "f"
    )


def test_rust_names_calls_and_member_refs():
    rl = tl.rules("rust")
    assert (
        tl.ref_name(rl, "crate::utils::helper", "", ()) == "utils.helper" and tl.ref_name(rl, "self::f", "", ()) == "f"
    )
    assert tl.ref_name(rl, "Vec::<u8>::new", "", ()) == "Vec.new" and tl.ref_name(rl, "a ::\n b", "", ()) == "a.b"
    assert (
        tl.ref_name(rl, "area", "self", ()) == "self.area" and tl.ref_name(rl, "m", "self.inner", ()) == "self.inner.m"
    )
    assert tl.ref_name(rl, "m", "foo(x)\n.bar()", ()) == "_.m"
    f = facts_of("rust", "crate", "src/main.rs")
    r = {(x.name, x.kind, x.scope) for x in f.refs}
    assert {("utils.helper", "call", "main"), ("Square.new", "call", "main"), ("s.area", "call", "main")} <= r
    assert ("my_macro", "call", "main") in r  # a macro call is a call of the macro
    assert ("a", "ref", "main") in r and ("_.a", "ref", "main") in r  # a token inside a macro call: weak, also a member


def test_rust_macro_rules_is_a_dynamic_site_of_its_file():
    f = facts_of("rust", "crate", "src/macros.rs")
    assert [(x.pattern, x.scope) for x in f.dyn] == [("macro:my_macro", "my_macro")]
    assert defs(f)["my_macro"].kind == "func"


def test_rust_vis_rules():
    rl = tl.rules("rust")
    assert [tl.vis(rl, "f", t) for t in ("pub", "pub(crate)", "pub (super)", "pub(in a::b)", "")] == [
        "public",
        "package",
        "package",
        "package",
        "private",
    ]
    assert [tl.vis(tl.rules("go"), n, "") for n in ("Foo", "foo", "_x", "X")] == [
        "exported",
        "package",
        "package",
        "exported",
    ]
    assert tl.vis({}, "foo") == ""  # no rule: the caller keeps its default


def test_rust_turbofish_method_call_is_a_member_call_and_the_method_is_alive():
    f = facts_of("rust", "attrs", "src/main.rs")
    r = {(x.name, x.kind, x.scope) for x in f.refs}
    assert {("c.get", "call", "main"), ("c.via_turbofish", "call", "main")} <= r  # c.get::<u8>(): obj c, not a path
    got, _ = verdicts("rust", "attrs", "closed_world")
    assert got["src/main.rs#Ctx.via_turbofish"] == "weak-alive" and got["src/main.rs#Ctx.never_called"] == "dead"
    pats = rp.scm_patterns(Path(tg.QUERY_DIR) / "rust" / "refs.scm")
    assert pats[5] == {"ref.call", "obj", "name"}  # the generic_function > field_expression pattern


def test_rust_function_named_in_an_attribute_string_is_a_use_and_entry_attributes_are_roots():
    f = facts_of("rust", "attrs", "src/main.rs")
    assert ("default_port", "ref", "Config") in {(x.name, x.kind, x.scope) for x in f.refs}
    assert [(r.kind, r.target, r.strength) for r in f.roots] == [
        ("annotation:rust-ffi", "Handle", "hard"),  # #[pyclass]
        ("annotation:rust-ffi", "native", "hard"),  # #[pymodule]
        ("annotation:rust-hook", "on_panic", "hard"),  # #[panic_handler]
        ("annotation:rust-hook", "start", "hard"),  # #[cortex_m_rt::entry]
        ("annotation:rust-test", "web_case", "soft"),  # #[wasm_bindgen_test]
        ("main", "main", "hard"),
    ]
    got, _ = verdicts("rust", "attrs", "closed_world")
    assert got["src/main.rs#default_port"] == "alive" and got["src/main.rs#unused_default"] == "dead"
    rl = tl.rules("rust")
    assert tl.ref_name(rl, '"default_port"') == "default_port" and tl.ref_name(rl, '"a::b"') == "a.b"
    kinds = lambda a: [k for k, _, _ in tl.attr_roots(rl, a)]  # noqa: E731
    assert kinds("pymodule") == kinds("pyclass") == ["annotation:rust-ffi"]
    assert kinds("panic_handler") == kinds("entry") == kinds("cortex_m_rt::entry") == ["annotation:rust-hook"]
    assert (
        kinds("interrupt") == kinds("riscv_rt::exception") == kinds("alloc_error_handler") == ["annotation:rust-hook"]
    )
    assert kinds("wasm_bindgen_test") == ["annotation:rust-test"] and kinds("other::entry") == kinds("serde") == []


# ====================================================================== second review: associated items, versions, gates
def test_a_go_import_path_ending_in_a_version_binds_the_directory_and_the_module_name():
    """Reviewed counterexample: api/v1 is a directory with `package v1` (kubebuilder, k8s), called as v1.NewThing()."""
    assert tl._go_alias('"example.com/app/api/v1"') >= {"v1", "api"}
    assert tl._go_alias('"k8s.io/api/core/v1"') >= {"v1", "core"} and tl._go_alias('"example.com/lib/v2"') == {
        "lib",
        "v2",
    }
    assert tl._go_alias('"example.com/x/v2/sub"') == {"sub"}  # a version in the middle is not the last element
    assert tl._go_alias('"example.com/x/v1alpha1"') == {"v1alpha1"} and tl._go_alias('"v2"') == {"v2"}
    assert tl._go_alias('"github.com/x/foo-bar"') >= {"foo-bar", "foo_bar", "foobar"}
    assert tl._go_alias('x "example.com/app/api/v1"') == {"x"}  # an explicit alias is the only name
    rl = tl.rules("go")
    names = tl._go_alias('"example.com/app/api/v1"')
    assert tl.ref_name(rl, "NewThing", "v1", names) == "NewThing" and tl.ref_name(rl, "Get", "c", names) == "c.Get"


def test_go_call_through_a_version_directory_package_is_a_use_and_the_rest_of_it_is_dead():
    r = refset("go", "vers", "main.go")
    assert {("NewThing", "call", "main"), ("Open", "call", "main"), ("Help", "call", "main")} <= r
    assert not any(n.endswith(("NewThing", "Open", "Help")) and "." in n for n, _, _ in r)  # never `v1.NewThing`
    got, _ = verdicts("go", "vers", "closed_world")
    assert got["api/v1/v1.go#NewThing"] == got["lib/v2/lib.go#Open"] == got["helpers/v3/h.go#Help"] == "weak-alive"
    assert got["api/v1/v1.go#Unused"] == "dead"


def test_go_linkname_roots_its_function_and_a_blank_var_is_no_definition():
    rl = tl.rules("go")
    assert tl.comment_roots(rl, "//go:linkname hook runtime.hook") == [("hook", "export", "hard", "convention")]
    assert tl.comment_roots(rl, "//go:linkname hook") == [("hook", "export", "hard", "convention")]
    assert tl.comment_roots(rl, "//go:linknamex hook") == [] and tl.comment_roots(rl, "// go:linkname hook") == []
    f = facts_of("go", "vers", "main.go")
    assert [(r.kind, r.target) for r in f.roots] == [("export", "runtimeHook"), ("main", "main")]
    assert "_" not in {d.qual for d in f.defs}  # `var _ fmt.Stringer = (*Worker)(nil)` is an assertion
    assert ("Worker", "ref", "") in {(r.name, r.kind, r.scope) for r in f.refs}  # its type use belongs to the file
    assert not any(r.name == "_" for r in f.refs)
    got, _ = verdicts("go", "vers", "closed_world")
    assert got["main.go#runtimeHook"] == "alive" and got["main.go#Worker"] == "alive"


def test_build_tag_variants_in_two_files_are_all_used_by_one_call():
    """Go: plat_linux.go and plat_windows.go define one function, t_linux.go and t_windows.go one method of T."""
    gof = lambda *d: Facts("go", tuple(md.Def(q, k, 1, 1) for q, k in d))  # noqa: E731
    files = {
        "types.go": gof(("T", "class")),
        "t_linux.go": gof(("T.sys", "method")),
        "t_windows.go": gof(("T.sys", "method")),
        "plat_linux.go": gof(("plat", "func")),
        "plat_windows.go": gof(("plat", "func")),
        "main.go": Facts(
            "go",
            (md.Def("main", "func", 1, 1),),
            (md.Ref("plat", "call", 3, "main"), md.Ref("t.sys", "call", 4, "main"), md.Ref("T", "ref", 4, "main")),
            roots=(RootHint("main", "main", 1),),
        ),
    }
    res = dlink.link(files, workers=1)
    v = rc.analyze(res.graph, "closed_world")
    got = {n.key.split(":", 1)[1]: v.verdict(n.key) for n in res.graph.nodes if n.kind != "file"}
    assert got == {  # one node per qual: both variants are one def, found by name (N: a guess, never dead)
        "types.go#T": "weak-alive",  # found by name from another file
        "t_linux.go#T.sys": "weak-alive",
        "t_windows.go#T.sys": "weak-alive",
        "plat_linux.go#plat": "weak-alive",
        "plat_windows.go#plat": "weak-alive",
        "main.go#main": "alive",
    }


def test_the_blank_name_is_language_data_a_shell_function_may_be_called_underscore():
    assert tl.rules("go")["blank_names"] == tl.rules("rust")["blank_names"] == ["_"]
    assert "blank_names" not in tl.rules("shell")
    src = '_() { gettext "$@"; }\nmain() { _ hello; }\n'
    m = {
        "defs": [
            [0, {"def.func": '_() { gettext "$@"; }', "name": "_"}],
            [0, {"def.func": "main() { _ hello; }", "name": "main"}],
        ],
        "refs": [[0, {"ref.call": "_ hello", "name": "_"}]],
    }
    tg.set_backend(lambda g, d: rp.Recorded(src, m))
    f = contracts.get_extractor("shell").extract("shell", "bin/x.sh", src)
    assert {d.qual for d in f.defs if d.qual} == {"_", "main"} and ("_", "call", "main") in {
        (r.name, r.kind, r.scope) for r in f.refs
    }


def test_rust_an_associated_type_is_no_definition_and_the_associated_consts_are_members():
    """Reviewed counterexample: `impl Add for Point { type Output = Point; ... }` was the dead private class `Output`."""
    d = defs(facts_of("rust", "assoc", "src/main.rs"))
    assert not [q for q in d if q.rpartition(".")[2] in ("Output", "Unit")]
    assert {q: (x.kind, x.vis, bool(x.flags & md.F_PROTOCOL)) for q, x in d.items() if x.kind == "const"} == {
        "Limits.MAX": ("const", "private", False),
        "Limits.UNUSED": ("const", "private", False),
        "Limits.OPEN": ("const", "public", False),
        "Shape.SIDES": ("const", "public", False),  # a trait item is as visible as the trait
        "Limits.SIDES": ("const", "private", True),  # impl Shape for Limits: lives with its type
    }
    assert {q: x.kind for q, x in d.items() if q in ("Alias", "inner.Id")} == {"Alias": "class", "inner.Id": "class"}
    r = refset("rust", "assoc", "src/main.rs")
    assert ("Self.MAX", "ref", "Limits.cap") in r and ("Limits.OPEN", "ref", "main") in r
    got, _ = verdicts("rust", "assoc", "closed_world")
    assert not [k for k in got if k.endswith(("#Output", "#Unit"))] and got["src/main.rs#Point.add"] == "alive"
    assert got["src/main.rs#Limits.MAX"] == got["src/main.rs#Limits.OPEN"] == "alive"
    assert got["src/main.rs#Limits.UNUSED"] == "dead"  # a private associated const nobody reads is still dead
    assert got["src/main.rs#Shape.SIDES"] == got["src/main.rs#Limits.SIDES"] == "alive"


def test_rust_qualified_path_calls_are_member_calls_by_name():
    """`<Limits>::make()` and `<T as Trait>::f()` name no path: they were no use of f at all (an empty name)."""
    rl = tl.rules("rust")
    names = ("<Limits as Shape>::sides", "<Limits>::make", "<Vec<u8> as Tr>::m", "<T as Default>::default::<X>")
    assert [tl.ref_name(rl, n, "", ()) for n in names] == ["_.sides", "_.make", "_.m", "_.default"]
    assert tl.ref_name(rl, "<Self as Tr>::ID", "", ()) == "_.ID"
    assert tl.ref_name(rl, "Vec::<u8>::new", "", ()) == "Vec.new" and tl.ref_name(rl, "a::b", "", ()) == "a.b"
    assert ("_.make", "call", "main") in refset("rust", "assoc", "src/main.rs")
    got, _ = verdicts("rust", "assoc", "closed_world")
    assert got["src/main.rs#Limits.make"] == "weak-alive"  # a member found by name


def test_rust_type_item_queries_count_only_file_and_module_level_aliases_and_impl_items_come_first():
    """No grammar on this host: the anchoring of the query text is what keeps impl associated types out of the defs."""
    pats = rp.scm_pattern_texts(Path(tg.QUERY_DIR) / "rust" / "defs.scm")
    types = [p for p in pats if "type_item" in p]
    assert len(types) == 2 and all(p.startswith(("(source_file", "(mod_item")) for p in types), types
    assert not [p for p in pats if "associated_type" in p]
    consts = [i for i, p in enumerate(pats) if "const_item" in p]
    assert [("impl_item" in pats[i], "@recv" in pats[i]) for i in consts] == [
        (True, True),
        (True, True),
        (False, False),
    ]
    assert [("trait:" in pats[i], "@flag.protocol" in pats[i]) for i in consts[:2]] == [(True, True), (False, False)]
    assert consts == sorted(consts) and consts[2] > consts[1]  # the impl patterns win on one node: lower index


def test_rust_wrapped_attributes_name_the_attributes_inside_them():
    rl = tl.rules("rust")
    paths = lambda t: tl.attr_paths(rl, t)  # noqa: E731
    assert paths("unsafe(no_mangle)") == ["unsafe", "no_mangle"]
    assert paths('unsafe(export_name = "x")') == ["unsafe", "export_name"]
    assert paths('cfg_attr(feature = "python", pyclass)') == ["cfg_attr", "pyclass"]
    assert paths('cfg_attr(all(unix, feature = "a,b"), derive(Debug), unsafe(no_mangle))') == [
        "cfg_attr",
        "derive",
        "unsafe",
        "no_mangle",
    ]
    assert paths('cfg_attr(feature = ")", pyclass)') == ["cfg_attr", "pyclass"]  # a bracket inside a string
    assert paths('cfg_attr(feature = "a\\"b,c", pyclass)') == ["cfg_attr", "pyclass"]  # an escaped quote
    assert paths("cfg_attr(test)") == ["cfg_attr"] and paths("cfg_attr") == ["cfg_attr"]  # nothing wrapped
    assert paths('tokio::test(flavor = "x")') == ["tokio::test"] and paths("::std::test") == ["::std::test"]
    assert paths('serde(default = "f")') == ["serde"] and paths("") == [] and paths("(x)") == []
    assert tl.attr_paths({}, "unsafe(no_mangle)") == ["unsafe"]  # wrappers are data of the language


def test_rust_wrapped_and_generic_attribute_rules():
    rl = tl.rules("rust")
    kinds = lambda a: [k for k, _, _ in tl.attr_roots(rl, a)]  # noqa: E731
    assert kinds("unsafe(no_mangle)") == kinds('unsafe(export_name = "x")') == ["annotation:rust-ffi"]
    assert (
        kinds('cfg_attr(feature = "py", pyclass)')
        == kinds("cfg_attr(not(wasm), wasm_bindgen)")
        == ["annotation:rust-ffi"]
    )
    assert kinds("cfg_attr(test, test)") == ["annotation:rust-test"]
    assert kinds('cfg_attr(feature = "a", unsafe(no_mangle))') == ["annotation:rust-ffi"]  # a wrapper in a wrapper
    assert kinds("cfg_attr(not(test), derive(Debug))") == kinds("cfg(test)") == kinds("unsafe(deref)") == []
    assert (
        kinds("used") == kinds("global_allocator") == kinds('unsafe(link_section = ".x")') == ["annotation:rust-hook"]
    )
    assert kinds("divan::bench") == kinds("sqlx::test") == kinds("test_case") == ["annotation:rust-test"]
    assert kinds("embassy_executor::main") == kinds("rocket::launch") == kinds("main") == ["main"]
    assert kinds("uniffi::export") == ["annotation:rust-ffi"] and kinds("testing") == kinds("domain") == []
    assert tl.attr_roots(rl, "unsafe(no_mangle)") != tl.attr_roots({}, "unsafe(no_mangle)") == []


def test_rust_cfg_gated_variants_unsafe_attributes_and_compiler_kept_statics():
    f = facts_of("rust", "gates", "src/main.rs")
    assert [(r.kind, r.target, r.strength) for r in f.roots] == [
        ("annotation:rust-ffi", "PyThing", "hard"),  # #[cfg_attr(feature = "python", pyclass)]
        ("annotation:rust-ffi", "conditional_export", "hard"),  # #[cfg_attr(feature = "ffi", unsafe(no_mangle))]
        ("annotation:rust-ffi", "ffi_2024", "hard"),  # #[unsafe(no_mangle)], required since edition 2024
        ("annotation:rust-hook", "ALLOC", "hard"),  # #[global_allocator]
        ("annotation:rust-hook", "KEEP", "hard"),  # #[used]
        ("annotation:rust-test", "gated_test", "soft"),  # #[cfg_attr(test, test)]
        ("main", "main", "hard"),
    ]
    assert [d.qual for d in f.defs].count("platform") == 1  # #[cfg(unix)] and #[cfg(not(unix))] fn platform: one def
    assert "_" not in {d.qual for d in f.defs} and not any(r.name == "_" for r in f.refs)  # const _: () = ();
    got, _ = verdicts("rust", "gates", "closed_world")
    assert got["src/main.rs#platform"] == "alive" and got["src/main.rs#Alloc"] == "alive"
    assert got["src/main.rs#Plain"] == got["src/main.rs#unused_helper"] == "dead"  # derive and cfg root nothing


# ====================================================================== third review: inline modules, shadowed packages
def rs_files(files):
    """{path: Facts} of rust files given as path -> (defs [(qual, kind)], refs [(name, kind, scope)], imports [Import]); a
    file that defines `main` is the root."""
    return {
        path: Facts(
            "rust",
            tuple(md.Def(q, k, i + 1, i + 1) for i, (q, k) in enumerate(dfs)),
            tuple(md.Ref(n, k, 1, sc) for n, k, sc in rfs),
            tuple(ims),
            (RootHint("main", "main", 1),) if any(q == "main" for q, _ in dfs) else (),
        )
        for path, (dfs, rfs, ims) in files.items()
    }


def rs_verdicts(files):
    res = dlink.link(files, workers=1)
    rr = rc.analyze(res.graph, "closed_world")
    return {n.key.split(":", 1)[1]: rr.verdict(n.key) for n in res.graph.nodes if n.kind != "file"}


M = "src/main.rs"
INLINE = [("utils", "module"), ("utils.helper", "func"), ("main", "func")]
CALL = [("helper", "call", "main")]


@pytest.mark.parametrize("module", ["utils", "crate::utils", "self::utils"])
def test_an_item_of_an_inline_module_imported_with_use_is_used_by_its_bare_name(module):
    """Reviewed counterexample: `mod utils { pub fn helper() {} } use utils::helper; fn main() { helper(); }` was dead."""
    got = rs_verdicts(rs_files({M: (INLINE, CALL, [md.Import(module, ("helper",), "", 4)])}))
    assert got == {"src/main.rs#main": "alive", "src/main.rs#utils": "alive", "src/main.rs#utils.helper": "alive"}


def test_inline_module_imports_in_all_their_forms():
    mods = [("utils", "module"), ("utils.deep", "module"), ("utils.deep.leaf", "func"), ("utils.one", "func")]
    mods += [("utils.two", "func"), ("utils.three", "func"), ("utils.unused", "func"), ("main", "func")]
    calls = [("leaf", "call", "main"), ("renamed", "call", "main"), ("three", "call", "main")]
    ims = [
        md.Import("utils::deep", ("leaf",), "", 1),  # a module inside the module
        md.Import("utils", ("one",), "renamed", 2),  # use utils::one as renamed
        md.Import("utils", ("*",), "", 3),  # the glob binds the children
    ]
    got = rs_verdicts(rs_files({M: (mods, calls, ims)}))
    assert {q: v for q, v in got.items() if q.startswith("src/main.rs#utils")} == {
        "src/main.rs#utils": "alive",
        "src/main.rs#utils.deep": "alive",
        "src/main.rs#utils.deep.leaf": "alive",
        "src/main.rs#utils.one": "alive",  # through its alias
        "src/main.rs#utils.two": "dead",
        "src/main.rs#utils.three": "alive",  # through the glob
        "src/main.rs#utils.unused": "dead",  # a glob binds names, it does not use them
    }
    inner = [("a", "module"), ("a.utils", "module"), ("a.utils.helper", "func"), ("a.run", "func"), ("main", "func")]
    ims = [md.Import("utils", ("helper",), "", 1, 0, "a.run")]  # a use inside fn run of mod a: scope outward
    got = rs_verdicts(rs_files({M: (inner, [("helper", "call", "a.run"), ("a.run", "call", "main")], ims)}))
    assert (
        got["src/main.rs#a.utils.helper"] == "weak-alive"
    )  # bound (T); the chain from main to a.run is a call by name


def test_an_inline_module_of_another_file_and_the_crate_root_are_found_by_a_crate_path():
    root = [
        ("shared", "module"),
        ("shared.common", "func"),
        ("shared.unused", "func"),
        ("top", "func"),
        ("main", "func"),
    ]
    files = rs_files(
        {
            M: (root, [("worker.work", "call", "main")], [md.Import("self::worker", (), "worker", 1)]),
            "src/worker.rs": (
                [("work", "func")],
                [("common", "call", "work"), ("top", "call", "work")],
                [md.Import("crate::shared", ("common",), "", 1), md.Import("crate", ("top",), "", 2)],
            ),
        }
    )
    got = rs_verdicts(files)
    assert got["src/main.rs#shared.common"] == "alive" and got["src/main.rs#shared.unused"] == "dead"
    assert (
        got["src/main.rs#shared"] == "alive" and got["src/main.rs#top"] == "weak-alive"
    )  # by name: no file is `crate`


def test_a_crate_relative_import_that_names_no_def_is_matched_by_name_but_an_external_one_is_not():
    """`use crate::x` / `super::x` / a re-export can never be an external crate; `use serde::Serialize` can."""
    lib = ([("Serialize", "class")], [], [])
    for module, want in (
        ("serde", "dead"),
        ("crate", "weak-alive"),
        ("super", "weak-alive"),
        ("crate::api", "weak-alive"),
    ):
        use = ([("main", "func")], [("Serialize", "ref", "main")], [md.Import(module, ("Serialize",), "", 1)])
        assert rs_verdicts(rs_files({M: use, "src/lib_a.rs": lib}))["src/lib_a.rs#Serialize"] == want, module
    use = ([("main", "func")], [("alias", "call", "main")], [md.Import("crate", ("real",), "alias", 1)])
    got = rs_verdicts(rs_files({M: use, "src/lib_a.rs": ([("real", "func")], [], [])}))
    assert (
        got["src/lib_a.rs#real"] == "weak-alive"
    )  # `use crate::real as alias; alias()`: the name that counts is `real`


def test_a_re_export_through_an_inline_module_is_matched_by_the_name_of_its_item():
    """`mod prelude { pub use crate::shared::common; } use prelude::common;`: prelude defines no `common`, shared does."""
    defs = [("shared", "module"), ("shared.common", "func"), ("shared.unused", "func"), ("prelude", "module")]
    ims = [md.Import("crate::shared", ("common",), "", 1, 0, "prelude"), md.Import("prelude", ("common",), "", 2)]
    got = rs_verdicts(rs_files({M: (defs + [("main", "func")], [("common", "call", "main")], ims)}))
    assert got["src/main.rs#shared.common"] == "weak-alive" and got["src/main.rs#shared.unused"] == "dead"
    assert got["src/main.rs#prelude"] == "alive"


def test_a_python_import_of_a_module_called_crate_stays_external():
    """The crate / self / super fallback is for languages with such paths: `from crate import g` is a package here."""
    use = Facts(
        "python",
        (md.Def("f", "func", 1, 1),),
        (md.Ref("g", "call", 1, ""),),
        (md.Import("crate", ("g",), "", 1),),
        (RootHint("main", "", 1),),
    )
    lib = Facts("python", (md.Def("g", "func", 1, 1),))
    got = rs_verdicts({"b.py": use, "c.py": lib})
    assert got["c.py#g"] == "dead"  # no edge: g is bound to an external module
    assert (
        dlink.link({"b.py": use, "c.py": lib}, workers=2).graph.digest()
        == dlink.link({"b.py": use, "c.py": lib}, workers=1).graph.digest()
    )


# --- go: a local named like an imported package
def test_ref_names_adds_the_member_spelling_only_where_a_local_shadows_the_package():
    rl = tl.rules("go")
    names = {"config", "user"}
    assert tl.ref_names(rl, "Validate", "config", names, {"config"}) == ["Validate", "config.Validate"]
    assert tl.ref_names(rl, "Validate", "config", names, {"other"}) == ["Validate"]  # a package function only
    assert tl.ref_names(rl, "Validate", "config", names) == ["Validate"]
    assert tl.ref_names(rl, "Do", "x", names, {"x"}) == ["x.Do"]  # not an alias: the member form it always was
    assert tl.ref_names(rl, "Do", "", names, {"config"}) == ["Do"]
    assert tl.ref_names(rl, "Do", "a.b().c", names, {"c"}) == ["_.Do"]
    assert tl.ref_names({}, "f", "config", names, {"config"}) == [
        "f",
        "config.f",
    ]  # the rule is the shape, not the language


def test_go_locals_and_parameters_named_like_an_imported_package_make_the_call_a_method_call_too():
    """Reviewed counterexample: `func run(config *config.Config) { config.Validate() }` made Config.Validate dead."""
    f = facts_of("go", "shadow", "main.go")
    r = {(x.name, x.kind, x.scope) for x in f.refs}
    assert {("Validate", "call", "run"), ("config.Validate", "call", "run")} <= r  # the parameter
    assert {("New", "call", "save"), ("user.New", "call", "save")} <= r  # user := user.New(): the declaration
    assert {("Save", "call", "save"), ("user.Save", "call", "save")} <= r  # ... and the use after it
    assert ("Load", "call", "plain") in r and ("config.Load", "call", "plain") not in r  # no local: a package function
    got, _ = verdicts("go", "shadow", "closed_world")
    assert got["config/config.go#Config.Validate"] == got["user/user.go#User.Save"] == "weak-alive"
    assert got["config/config.go#Load"] == "weak-alive" and got["config/config.go#Config.Load"] == "dead"


def test_a_local_binds_the_name_in_its_own_definition_only():
    src = "package p\nfunc a(config int) { config.X() }\nfunc b() { config.X() }\n"
    imp = 'import "example.com/config"\n'
    src = src.replace("package p\n", "package p\n" + imp)
    m = {
        "defs": [
            [4, {"def.func": "func a(config int) { config.X() }", "name": "a"}],
            [4, {"def.func": "func b() { config.X() }", "name": "b"}],
        ],
        "refs": [
            [8, {"import": '"example.com/config"'}],
            [13, {"local": "config#1"}],
            [1, {"ref.call": "config.X()", "obj": "config", "name": "X"}],
            [1, {"ref.call": "config.X()#1", "obj": "config", "name": "X"}],
        ],
    }
    tg.set_backend(lambda g, d: rp.Recorded(src, m))
    f = contracts.get_extractor("go").extract("go", "x.go", src)
    assert {(r.name, r.scope) for r in f.refs} == {("X", "a"), ("config.X", "a"), ("X", "b")}


def test_go_embedded_fields_of_every_form_are_inherit_refs_and_keep_the_methods_of_an_external_base():
    """Reviewed: `struct{ pb.UnimplementedGreeterServer }` and `*pb.X` were no inherit, so the external-base rule never ran."""
    f = facts_of("go", "embed", "main.go")
    inh = {(r.name, r.scope) for r in f.refs if r.kind == "inherit"}
    assert inh == {
        ("UnimplementedGreeterServer", "server"),
        ("UnimplementedGreeterServer", "ptrServer"),
        ("Base", "derived"),
    }
    got, _ = verdicts("go", "embed", "closed_world")
    assert got["main.go#server.SayHello"] == got["main.go#ptrServer.Serve"] == "weak-alive"
    assert got["main.go#quiet.Run"] == got["main.go#Base.Hello"] == "dead"  # nothing external, nothing kept


def test_go_query_patterns_for_embedding_and_locals_are_anchored_as_documented():
    """No grammar on this host: the shape of the pattern text is what the real-grammar test below checks against syntax."""
    pats = rp.scm_pattern_texts(Path(tg.QUERY_DIR) / "go" / "refs.scm")
    assert pats[3].startswith("(field_declaration !name type: (type_identifier)") and pats[8].startswith(
        "(import_spec)"
    )  # stable indices
    inherit = [p for p in pats if "@ref.inherit" in p]
    shape = [re.match(r"\(field_declaration !name type: \((\w+)(?: \((\w+))?", p).groups() for p in inherit]
    assert shape == [
        ("type_identifier", None),
        ("qualified_type", None),
        ("generic_type", None),
        ("pointer_type", "type_identifier"),
        ("pointer_type", "qualified_type"),
    ]
    local = [p for p in pats if "@local" in p]
    assert [re.match(r"\((\w+)", p).group(1) for p in local] == [
        "parameter_declaration",
        "variadic_parameter_declaration",
        "short_var_declaration",
        "var_spec",
        "range_clause",
        "type_switch_statement",
        "receive_statement",
    ]
    assert all(re.search(r"(name|left|alias): \(?(identifier|expression_list)", p) for p in local)
    assert all("@ref." not in p for p in local), "a local is a binding, never a use"


# --- rust: a turbofish with nested generics
def test_rust_balanced_generic_arguments_are_dropped_from_a_path():
    """Reviewed: the regex `::<[^>]*>` left `Wrapper>.new` for `Wrapper::<Vec<u8>>::new`."""
    rl = tl.rules("rust")
    name = lambda t: tl.ref_name(rl, t, "", ())  # noqa: E731
    assert name("Wrapper::<Vec<u8>>::new") == "Wrapper.new"
    assert name("HashMap::<String, Vec<u8>>::new") == "HashMap.new"
    assert name("a::<B<C<D>>>::f::<E<F>>") == "a.f"
    assert name("Vec::<u8>::new") == "Vec.new" and name("std::mem::size_of::<T>") == "std.mem.size_of"
    assert name("Foo::<Box<dyn Fn() -> u8>>::bar") == "Foo.bar"  # the > of -> closes nothing
    assert name("<Vec::<u8> as Tr>::m") == "_.m" and name("<T as Default>::default::<X<Y>>") == "_.default"
    assert name("Wrapper::<Vec<u8>") == "Wrapper"  # a group that never closes runs to the end, never an exception
    assert name("plain::path") == "plain.path" and name("") == ""
    assert "::<[^>]*>" not in [p for p, _ in rl["name_sub"]] and rl["strip_groups"] == [["::<", ">"]]
    assert tl._strip_groups("a::<b>", ()) == "a::<b>" and tl._strip_groups("a::<b>::c::<d>", [["::<", ">"]]) == "a::c"


def test_a_call_through_a_nested_turbofish_reaches_the_member():
    f = facts_of("rust", "inline", "src/main.rs")
    assert ("Wrapper.new", "call", "main") in {(r.name, r.kind, r.scope) for r in f.refs}
    got, _ = verdicts("rust", "inline", "closed_world")
    assert got["src/main.rs#Wrapper.new"] == "alive" and got["src/main.rs#Wrapper.never_called"] == "dead"


# ====================================================================== fourth review: workspace crates, #[path], protocol names
UT = "util/src/lib.rs"


@pytest.mark.parametrize("base", ["", "crates/", "libs/rust/"])
def test_items_of_another_crate_of_a_workspace_are_used_through_the_name_of_its_directory(base):
    """Reviewed counterexample: app imports util::{helper} and util::net::{conn} and calls util::other(): all three were dead,
    nothing resolved a crate name to a directory and the import was external."""
    calls = [("helper", "call", "main"), ("conn", "call", "main"), ("util.other", "call", "main")]
    ims = [md.Import("util", ("helper",), "", 1), md.Import("util::net", ("conn",), "", 2)]
    files = {
        f"{base}app/src/main.rs": ([("main", "func")], calls, ims),
        f"{base}{UT}": (
            [("helper", "func"), ("other", "func"), ("unused", "func")],
            [],
            [md.Import("self::net", (), "net", 1)],
        ),
        f"{base}util/src/net.rs": ([("conn", "func"), ("spare", "func")], [], []),
    }
    got = rs_verdicts(rs_files(files))
    assert {k.split("#")[1]: v for k, v in got.items() if "util/" in k} == {
        "helper": "alive",
        "other": "alive",  # util::other() with no use: the path starts with a crate
        "unused": "dead",
        "conn": "alive",  # util::net is src/net.rs of the crate, not the net.rs of another one
        "spare": "dead",
    }


def test_every_form_of_naming_a_crate_binds_its_items_and_a_re_export_is_matched_by_name():
    lib = [("top", "func"), ("viaalias", "func"), ("direct", "func"), ("deep", "module"), ("deep.leaf", "func")]
    lib += [("deep.spare", "func"), ("never", "func")]
    calls = [("top", "call", "main"), ("u.viaalias", "call", "main"), ("util.direct", "call", "main")]
    calls += [("leaf", "call", "main"), ("net.conn", "call", "main"), ("reexported", "call", "main")]
    ims = [
        md.Import("util", (), "", 1),
        md.Import("util", ("top",), "", 1),
        md.Import("util", (), "u", 2),
    ]  # use util::{self, top}
    ims += [
        md.Import("util::deep", ("leaf",), "", 3),
        md.Import("util", ("net",), "", 4),
    ]  # an inline mod of lib.rs, a file mod
    ims += [
        md.Import("util", ("reexported",), "", 5)
    ]  # `pub use inner::reexported;` in lib.rs: util defines no such item
    files = {
        "app/src/main.rs": ([("main", "func")], calls, ims),
        UT: (lib, [], []),
        "util/src/net.rs": ([("conn", "func"), ("spare", "func")], [], []),
        "util/src/inner.rs": ([("reexported", "func")], [], []),
    }
    got = {k.split("/", 2)[2]: v for k, v in rs_verdicts(rs_files(files)).items() if k.startswith("util/")}
    assert got == {
        "lib.rs#top": "alive",
        "lib.rs#viaalias": "alive",  # use util as u
        "lib.rs#direct": "alive",  # use util; util::direct()
        "lib.rs#deep": "alive",  # the module on the way of `use util::deep::leaf`, defined inline in lib.rs
        "lib.rs#deep.leaf": "alive",
        "lib.rs#deep.spare": "dead",
        "lib.rs#never": "dead",
        "net.rs#conn": "alive",
        "net.rs#spare": "dead",
        "inner.rs#reexported": "weak-alive",  # matched by its name (N): the re-export is not followed
    }


def test_a_use_that_goes_on_through_an_inline_module_of_a_file_module_keeps_that_module_alive():
    """`use outer::deep::leaf;` with `mod deep` written inside outer.rs: the module def was dead although its item is alive."""
    outer = [("deep", "module"), ("deep.leaf", "func"), ("deep.spare", "func")]
    use = ([("main", "func")], [("leaf", "call", "main")], [md.Import("outer::deep", ("leaf",), "", 1)])
    got = rs_verdicts(rs_files({M: use, "src/outer.rs": (outer, [], [])}))
    assert (got["src/outer.rs#deep"], got["src/outer.rs#deep.leaf"], got["src/outer.rs#deep.spare"]) == (
        "alive",
        "alive",
        "dead",
    )


def test_a_head_no_package_directory_answers_is_matched_by_name_in_a_repo_with_a_library_crate():
    """The name of a package is in its Cargo.toml: src/lib.rs at the top of the repo with its bins, tests and examples
    (`use mycrate::run;`), a directory named otherwise than its package (`core/` is `my-core`) or a rename. std is never the project.
    """
    lib = ([("run", "func"), ("spare", "func"), ("HashMap", "class")], [], [])
    ims = [md.Import("mycrate", ("run",), "", 1), md.Import("std::collections", ("HashMap",), "", 2)]
    cli = ([("main", "func")], [("run", "call", "main"), ("HashMap", "ref", "main")], ims)
    got = rs_verdicts(rs_files({"src/lib.rs": lib, "src/bin/cli.rs": cli}))
    assert (got["src/lib.rs#run"], got["src/lib.rs#spare"], got["src/lib.rs#HashMap"]) == ("weak-alive", "dead", "dead")
    app = ([("main", "func")], [("Config", "ref", "main")], [md.Import("my_core", ("Config",), "", 1)])
    core = ([("Config", "class"), ("Spare", "class")], [], [])
    got = rs_verdicts(rs_files({"app/src/main.rs": app, "core/src/lib.rs": core}))
    assert (got["core/src/lib.rs#Config"], got["core/src/lib.rs#Spare"]) == ("weak-alive", "dead")


def test_a_package_with_only_a_main_rs_has_no_name_to_import_and_other_languages_are_not_touched():
    tool = ([("f", "func")], [], [])
    app = ([("main", "func")], [("f", "call", "main")], [md.Import("tool", ("f",), "", 1)])
    got = rs_verdicts(rs_files({"app/src/main.rs": app, "tool/src/main.rs": tool}))
    assert got["tool/src/main.rs#f"] == "dead"  # a binary is no one's dependency: `tool` is an external crate here
    py = Facts("python", (md.Def("g", "func", 1, 1),))
    use = Facts(
        "python",
        (md.Def("f", "func", 1, 1),),
        (md.Ref("g", "call", 1, ""),),
        (md.Import("util", ("g",), "", 1),),
        (RootHint("main", "", 1),),
    )
    lib = ([("helper", "func")], [], [])
    got = rs_verdicts({"b.py": use, "util.py": py, **rs_files({UT: lib})})  # a python `util` is no rust crate
    assert got["util.py#g"] == "alive" and got["util/src/lib.rs#helper"] == "dead"


def test_a_crate_path_prefers_the_modules_of_the_crate_it_is_written_in():
    """Every package has a net.rs: `use crate::net::ping` in crates/shared-lib is that crate's net.rs even from a deep file, where
    the shortest path (app/src/net.rs) used to win."""
    deep = ([("go", "func")], [("ping", "call", "go")], [md.Import("crate::net", ("ping",), "", 1)])
    files = {
        "crates/shared-lib/src/lib.rs": (
            [("top", "func")],
            [("deep.go", "call", "top")],
            [md.Import("self::deep", (), "deep", 1)],
        ),
        "crates/shared-lib/src/a/deep.rs": deep,
        "crates/shared-lib/src/net.rs": ([("ping", "func")], [], []),
        "app/src/main.rs": ([("main", "func")], [("shared_lib.top", "call", "main")], []),
        "app/src/net.rs": ([("ping", "func")], [], []),
    }
    got = rs_verdicts(rs_files(files))
    assert got["crates/shared-lib/src/net.rs#ping"] == "alive" and got["app/src/net.rs#ping"] == "dead"


def test_a_trait_impl_in_another_inline_module_lives_with_its_type():
    """`mod b { impl fmt::Display for super::Foo { fn fmt .. } }`: its qual is b.Foo.fmt and Foo is no def of module b, so the
    method was no member of Foo and the protocol rule (a trait impl lives with its type) never reached it."""
    defs = (
        md.Def("Foo", "class", 1, 1),
        md.Def("b", "module", 2, 6),
        md.Def("b.Foo.fmt", "method", 3, 3, "private", md.F_PROTOCOL),
        md.Def("b.Foo.unused", "method", 4, 4, "private"),
        md.Def("main", "func", 7, 7),
    )
    facts = Facts("rust", defs, (md.Ref("Foo", "ref", 7, "main"),), (), (RootHint("main", "main", 1),))
    got = rs_verdicts({M: facts})
    assert (got["src/main.rs#b.Foo.fmt"], got["src/main.rs#b.Foo.unused"]) == ("alive", "dead")
    mod_b = (
        md.Def("b", "module", 1, 3),
        md.Def("b.Foo.fmt", "method", 2, 2, "private", md.F_PROTOCOL),
        md.Def("main", "func", 4, 4),
    )
    use = Facts("rust", mod_b, (md.Ref("Foo", "ref", 4, "main"),), (), (RootHint("main", "main", 1),))
    foo = Facts("rust", (md.Def("Foo", "class", 1, 1),))  # the type in another file, found by its bare name (N)
    assert rs_verdicts({M: use, "src/foo.rs": foo})["src/main.rs#b.Foo.fmt"] == "weak-alive"


def test_rust_a_path_attribute_before_mod_imports_that_file_relative_to_the_declaring_file():
    got = {(i.module, i.alias) for i in facts_of("rust", "path", "src/main.rs").imports}
    assert got == {
        ("./sys/unix.rs", "imp"),
        ("./sys/windows.rs", "imp"),  # the cfg-gated twin of `mod imp;`
        ("./gen/data.rs", "data"),  # a doc comment sits between the attributes
        ("./alt.rs", "other"),  # cfg_attr(cond, path = ...) names a file too, both variants
        ("./plain.rs", "other"),
        ("self::local", "local"),
        ("self::unix", "unix"),  # inside cfg_if! { if #[cfg(unix)] { mod unix; use unix as plat; } else { .. } }
        ("unix", "plat"),
        ("./win/mod.rs", "win"),  # the attribute before a `mod` inside the macro is read as well
        ("win", "plat"),
    }


def test_rust_item_macros_are_read_as_text_for_mod_and_use():
    rl = tl.rules("rust")
    body = '{\n  if #[cfg(unix)] {\n    pub(crate) mod unix;\n    use unix::imp;\n  } else {\n    #[path = "w.rs"]\n    mod win;\n  }\n}'
    got = [(i.module, i.names, i.alias, i.line) for i in tl.macro_imports(rl, "cfg_if::cfg_if", body, 10, "s")]
    assert got == [("self::unix", (), "unix", 12), ("unix", ("imp",), "", 13), ("./w.rs", (), "win", 16)]
    assert tl.macro_imports(rl, "cfg_if", "{ mod a; }", 1) == tl.macro_imports(rl, "a::b::cfg_match", "{ mod a; }", 1)
    assert len(tl.macro_imports(rl, "cfg_if", "{ mod a; }", 1)) == 1
    assert (
        tl.macro_imports(rl, "println", "{ mod a; }", 1) == [] and tl.macro_imports({}, "cfg_if", "{ mod a; }", 1) == []
    )
    assert (
        tl.macro_imports(rl, "cfg_if", "{ let reuse = x; fn mod_a() {} }", 1) == []
    )  # a word that only contains `use`


def test_rust_attribute_and_item_macro_patterns_close_refs_scm_so_the_recorded_indexes_stay_put():
    pats = rp.scm_patterns(Path(tg.QUERY_DIR) / "rust" / "refs.scm")
    assert pats[18] == {"attr", "attr.item"} and pats[19] == {"item_macro", "item_body"} and len(pats) == 20


def test_rust_path_attribute_rules():
    rl = tl.rules("rust")
    def imp(text, *attrs):
        return [
            (i.module, i.names, i.alias) for i in tl.imports(rl, text, 3, "", attrs)[0]
        ]  # noqa: E731
    assert imp("mod imp;", 'path = "sys/unix.rs"') == [("./sys/unix.rs", (), "imp")]
    assert imp("pub(crate) mod imp;", "cfg(unix)", 'path = "../shared/imp.rs"') == [("../shared/imp.rs", (), "imp")]
    assert imp("mod imp;", 'path = "./imp.rs"', 'path = r"raw.rs"') == [
        ("./imp.rs", (), "imp"),
        ("./raw.rs", (), "imp"),
    ]
    both = imp("mod imp;", 'cfg_attr(unix, path = "u.rs")', 'cfg_attr(not(unix), path = "w.rs")')
    assert both == [("./u.rs", (), "imp"), ("./w.rs", (), "imp")]
    assert imp("mod imp;") == imp("mod imp;", "cfg(unix)") == [("self::imp", (), "imp")]  # no path: imp.rs / imp/mod.rs
    assert imp("mod imp;", 'my_path = "no.rs"', 'doc = "see path"', "derive(Path)") == [("self::imp", (), "imp")]
    assert imp("use a::b;", 'path = "x.rs"') == [("a", ("b",), "")]  # only a mod declaration takes a path attribute
    assert tl.imports({}, "mod imp;", 3, "", ('path = "x.rs"',))[0] == []  # a language without the rule reads nothing


def test_rust_an_attribute_belongs_to_the_declaration_right_after_it_only():
    src = '#[path = "a.rs"]\nfn not_a_mod() {}\n\nmod b;\n#[path = "c.rs"] // trailing\n/* gap */ mod c;\n'
    attr = lambda a: [18, {"attr.item": f"#[{a}]", "attr": a}]  # noqa: E731
    m = {"refs": [attr('path = "a.rs"'), [16, {"import": "mod b;"}], attr('path = "c.rs"'), [16, {"import": "mod c;"}]]}
    tg.set_backend(lambda g, d: rp.Recorded(src, m))
    got = {(i.module, i.alias) for i in contracts.get_extractor("rust").extract("rust", "x.rs", src).imports}
    assert got == {("self::b", "b"), ("./c.rs", "c")}


def test_a_path_attribute_naming_no_file_binds_nothing_not_an_inline_module_named_like_a_directory():
    defs = [("sys", "module"), ("sys.f", "func"), ("main", "func")]
    use = (defs, [("imp.f", "call", "main")], [md.Import("./sys/unix.rs", (), "imp", 1)])
    got = rs_verdicts(rs_files({M: use}))
    assert got["src/main.rs#sys"] == "dead" and got["src/main.rs#sys.f"] == "dead"  # the file is not in the tree


def test_go_protocol_names_cover_the_canonical_interfaces_and_the_whole_method_set():
    rl = tl.rules("go")
    reviewed = (
        "Set Lock Unlock Visit Reset Size BlockSize Sum Enabled Handle Type Accept Addr Done Err Deadline".split()
    )
    reviewed += "LocalAddr RemoteAddr SetDeadline".split()  # the names of the second review
    rest = (
        "RLock RUnlock TryLock Sum32 Sum64 Level Network SetReadDeadline SetWriteDeadline UnreadByte UnreadRune".split()
    )
    rest += (
        "GobEncode GobDecode IsZero MarshalTOML UnmarshalTOML Cookies SetCookies Stat ReadDir ModTime IsDir Sys".split()
    )
    assert all(tl.protocol(rl, n) for n in reviewed + rest), [n for n in reviewed + rest if not tl.protocol(rl, n)]
    assert not any(tl.protocol(rl, n) for n in ("Open", "Run", "Idle", "Spare", "Name", "Start"))
    names = rl["protocol_names"]
    assert len(names) == len(set(names)) and all(isinstance(n, str) and n[:1].isupper() for n in names)


# ====================================================================== the linker reads these facts
def test_rust_trait_impl_methods_live_with_their_type_and_inherent_dead_ones_do_not():
    got, res = verdicts("rust", "crate", "closed_world")
    assert got["src/shapes.rs#Square.area"] == "alive" and got["src/shapes.rs#Square.dead_method"] == "dead"
    assert got["src/shapes.rs#Shape.label"] == "dead"  # a default method nobody calls or overrides
    short = lambda k: k.split(":", 1)[1]  # noqa: E731
    es = {(short(e.src), short(e.dst), e.kind, e.tier) for e in res.graph.edges}
    assert ("src/shapes.rs#Square", "src/shapes.rs#Shape", "impl", "T") in es  # CHA: the trait is the base of the type
    assert ("src/shapes.rs#Shape.area", "src/shapes.rs#Square.area", "override", "T") in es
    assert ("src/main.rs#main", "src/utils.rs#helper", "call", "T") in es  # mod utils; utils::helper()
    assert ("src/main.rs#main", "src/shapes.rs#Square.new", "call", "T") in es  # use shapes::{Square}; Square::new()


def test_rust_in_file_test_roots_do_not_turn_a_whole_file_into_test_only():
    got, _ = verdicts("rust", "crate", "closed_world")
    assert not [k for k, v in got.items() if v == "test-only"]
    assert got["src/shapes.rs#tests.square_area"] == "weak-alive" and got["src/shapes.rs#Unused"] == "dead"


def test_macro_dynamic_site_keeps_the_functions_of_its_file_weakly_alive_and_no_other_file():
    got, res = verdicts("rust", "crate", "closed_world")
    assert got["src/macros.rs#helper_in_macro"] == "weak-alive" and got["src/macros.rs#my_macro"] == "weak-alive"
    assert got["src/utils.rs#lib_unused"] == "dead" and got["src/main.rs#dead_fn"] == "dead"
    assert any(e.tier == "D" and e.src.endswith("#my_macro") for e in res.graph.edges)


def test_go_methods_called_through_an_interface_are_matched_by_name():
    got, _ = verdicts("go", "app", "closed_world")
    assert got["shapes.go#Circle.Area"] == "weak-alive" and got["shapes.go#Shape.Area"] == "weak-alive"
    assert got["shapes.go#Circle.Perimeter"] == "dead" and got["shapes.go#Named"] == "dead"
    assert (
        got["util/util.go#Helper"] == "weak-alive" and got["util/util.go#Unused"] == "dead"
    )  # util.Helper(): by name, tier N
    assert got["main_test.go#onlyForTest"] == "test-only" and got["main.go#setup"] == "alive"  # init() -> setup()
    assert got["main.go#deadA"] == got["main.go#deadB"] == "dead-cluster"


# ====================================================================== values, split types, any impl type
def refset(lang: str, case: str, path: str):
    return {(r.name, r.kind, r.scope) for r in facts_of(lang, case, path).refs}


def edges_of(lang: str, case: str):
    _, res = verdicts(lang, case, "closed_world")
    return {(e.src.split(":", 1)[1], e.dst.split(":", 1)[1], e.kind, e.tier) for e in res.graph.edges}


def test_a_const_var_or_function_value_used_outside_call_arguments_is_a_use():
    go = refset("go", "split", "a.go")
    assert {
        ("ErrNotFound", "ref", "code"),
        ("Max", "ref", "main"),
        ("handler", "ref", "main"),
    } <= go  # return X, n > X, cb := X
    assert ("code", "call", "main") in go and ("Circle", "ref", "main") in go
    assert not any(
        n in ("Max", "ErrNotFound") and s == "" for n, _, s in go
    )  # `const Max`, `var ErrNotFound` are no uses
    rs = refset("rust", "split", "src/main.rs")
    want = {
        ("LIMIT", "ref", "main"),
        ("consts.MAX", "ref", "main"),
        ("Color.Red", "ref", "main"),
        ("handler", "ref", "main"),
    }
    assert want <= rs and ("Foo", "ref", "main") in rs


def test_the_unused_one_of_each_kind_is_still_dead():
    go, _ = verdicts("go", "split", "closed_world")
    rs, _ = verdicts("rust", "split", "closed_world")
    assert [k for k, v in go.items() if v == "dead"] == ["a.go#UnusedVar", "b.go#Circle.Dead"]
    assert sorted(k for k, v in rs.items() if v == "dead") == ["src/consts.rs#UNUSED", "src/imp.rs#Py"] + [
        "src/imp.rs#Shape.area"
    ]
    assert go["a.go#Max"] == go["a.go#ErrNotFound"] == rs["src/consts.rs#LIMIT"] == rs["src/main.rs#Color"] == "alive"


def test_methods_of_a_type_declared_in_another_file_join_that_type():
    assert ("a.go#Circle", "b.go#Circle.String", "ref", "T") in edges_of("go", "split")  # a protocol name
    assert ("src/main.rs#Foo", "src/imp.rs#Foo.fmt", "ref", "T") in edges_of("rust", "split")  # impl Display for Foo
    assert ("src/main.rs#Foo", "src/imp.rs#Foo.area", "ref", "T") in edges_of("rust", "split")  # impl Shape for &Foo


def _facts(*defs):
    return Facts("go", tuple(md.Def(q, k, i + 1, i + 1) for i, (q, k) in enumerate(defs)))


def test_a_member_joins_the_type_of_its_own_directory_not_one_of_the_same_name_elsewhere():
    def pairs(fs):
        return {
            (e.src.split(":", 1)[1], e.dst.split(":", 1)[1]) for e in dlink.link(fs, workers=1).graph.edges
        }  # noqa: E731
    es = pairs(
        {
            "p1/t.go": _facts(("T", "class")),
            "p2/t.go": _facts(("T", "class")),
            "p2/m.go": _facts(("T.String", "method")),
            "q/m.go": _facts(("U.String", "method")),  # a type that is nowhere: the file owns the protocol method
        }
    )
    assert ("p2/t.go#T", "p2/m.go#T.String") in es and ("p1/t.go#T", "p2/m.go#T.String") not in es
    assert ("q/m.go", "q/m.go#U.String") in es
    far = pairs({"p1/t.go": _facts(("T", "class")), "x/m.go": _facts(("T.String", "method"))})
    assert ("p1/t.go#T", "x/m.go#T.String") in far  # no type of that name next to it: the one of the family


def test_rust_impl_headers_name_their_type_whatever_the_type_expression_is():
    rl = tl.rules("rust")
    table = {
        "Foo": "Foo",
        "Foo<T>": "Foo",
        "&Foo": "Foo",
        "&'a mut Foo<T>": "Foo",
        "Box<dyn X>": "Box",
        "crate::m::Foo": "Foo",
        "f64": "f64",
        "*const Foo": "Foo",
        "dyn Foo + Send": "Foo",
        "[T]": "_",
        "(A, B)": "_",
        "<T as X>::Y": "_",
        "fn(u8)": "_",
    }
    assert {k: tl.recv_name(rl, k) for k in table} == table
    assert tl.recv_name(tl.rules("go"), "Circle") == "Circle" and tl.recv_name({}, "  X ") == "X"
    d = defs(facts_of("rust", "split", "src/imp.rs"))
    assert {q for q in d if "." in q} == {"Shape.area", "Foo.fmt", "f64.area", "Foo.area", "Py.exposed"}
    assert "area" not in d and "fmt" not in d  # no flat, private, flag-less method that nothing calls
    assert all(d[q].flags & md.F_PROTOCOL for q in ("Foo.fmt", "f64.area", "Foo.area"))
    assert not d["Py.exposed"].flags & md.F_PROTOCOL
    r = refset("rust", "split", "src/imp.rs")
    assert ("Shape", "impl", "f64") in r and ("Shape", "impl", "Foo") in r and ("std.fmt.Display", "impl", "Foo") in r


def test_a_trait_impl_for_a_type_the_project_does_not_define_is_owned_by_its_file():
    assert ("src/imp.rs", "src/imp.rs#f64.area", "ref", "T") in edges_of("rust", "split")
    got, _ = verdicts("rust", "split", "closed_world")
    assert got["src/imp.rs#f64.area"] == "alive"


def test_rust_framework_attributes_root_handlers_and_pymethods_blocks():
    f = facts_of("rust", "split", "src/main.rs")
    assert [(r.kind, r.target, r.strength) for r in f.roots] == [
        ("annotation:rust-route", "index", "hard"),
        ("main", "main", "hard"),
    ]
    g = facts_of("rust", "split", "src/imp.rs")
    assert [(r.kind, r.target) for r in g.roots] == [
        ("annotation:rust-ffi", "Py.exposed")
    ]  # the methods of the block, not Py
    rl = tl.rules("rust")
    kinds = lambda a: [k for k, _, _ in tl.attr_roots(rl, a)]  # noqa: E731
    assert kinds("get") == kinds("actix_web::post") == kinds("rocket::route") == ["annotation:rust-route"]
    assert (
        kinds("catch")
        == kinds("rocket::catch")
        == kinds("poem::handler")
        == kinds("salvo::handler")
        == ["annotation:rust-route"]
    )
    assert kinds("handler") == kinds("poem::handler") and kinds("other::handler") == []
    assert kinds("ctor") == kinds("ctor::dtor") == kinds("tauri::command") == ["annotation:rust-hook"]
    assert kinds("pymethods") == kinds("pyfunction") == ["annotation:rust-ffi"]
    assert kinds("getter") == kinds("gets") == kinds("command") == []


def test_a_language_without_a_rules_entry_is_an_error_not_an_empty_rule_set():
    assert tl.rules("shell") == {}  # an explicit empty entry
    with pytest.raises(KeyError, match="no entry for 'klingon'"):
        tl.rules("klingon")


# ====================================================================== missing pieces are loud
@pytest.mark.parametrize("lang", ["go", "rust"])
def test_a_missing_grammar_is_extractor_missing_never_a_fallback(lang, monkeypatch):
    tg.set_backend(None)
    monkeypatch.setattr(tsm, "get_parser", lambda g: (_ for _ in ()).throw(LookupError(f"grammar {g} is missing: x")))
    with pytest.raises(contracts.ExtractorMissing, match=f"grammar {lang} is missing"):
        contracts.get_extractor(lang).extract(lang, "a", "")


def test_a_bad_capture_in_go_or_rust_queries_is_an_error_not_silence():
    src = "fn f() {}\n"
    tg.set_backend(lambda g, d: rp.Recorded(src, {"defs": [[4, {"def.nonsense": "fn f() {}", "name": "f"}]]}))
    with pytest.raises(ValueError, match="not a node kind"):
        contracts.get_extractor("rust").extract("rust", "a.rs", src)
    tg.set_backend(
        lambda g, d: rp.Recorded(src, {"defs": [[4, {"def.func": "fn f() {}", "flag.nope": "fn f() {}", "name": "f"}]]})
    )
    with pytest.raises(ValueError, match="not a flag name"):
        contracts.get_extractor("rust").extract("rust", "a.rs", src)


# ====================================================================== the real grammars (host dependent)
def _grammar_problem(g: str) -> str:
    try:
        tsm.get_parser(g)
        return ""
    except LookupError as exc:
        return str(exc)


@pytest.mark.parametrize("lang,case,grammar", ALL)
def test_real_grammar_agrees_with_the_recorded_facts(lang, case, grammar):
    problem = _grammar_problem(grammar)
    if problem:
        pytest.skip(f"tree-sitter {grammar} grammar is not installed on this host: {problem}")
    srcs, _ = load_case(lang, case)
    recorded = extract_all(lang, case)
    tg.set_backend(None)
    ex = contracts.get_extractor(lang)
    for f, (text, _) in srcs.items():
        real = ex.extract(lang, f, text)
        assert real.errors == 0, f
        assert {(d.qual, d.kind) for d in real.defs} == {(d.qual, d.kind) for d in recorded[f].defs}, f
        assert {(r.name, r.kind, r.scope) for r in real.refs} >= {
            (r.name, r.kind, r.scope) for r in recorded[f].refs if r.kind in ("call", "impl", "inherit", "exec")
        }, f
        assert set(real.imports) == set(recorded[f].imports) and set(real.roots) == set(recorded[f].roots), f


GO_SYNTAX = """package p

const (
	A = 1
	B = 2
)

var (
	C = 3
	D = 4
)

var E, F = 5, 6

type I interface {
	M()
}

type G[T any] struct{}

func (g *G[T]) Get() {}

func f() {
	var local = 1
	_ = local
}
"""
RUST_SYNTAX = """pub struct Foo;
pub trait Tr { fn m(&self); }
impl Tr for f64 { fn m(&self) {} }
impl Tr for &Foo { fn m(&self) {} }
impl Tr for Box<dyn Tr> { fn m(&self) {} }
impl Tr for [Foo] { fn m(&self) {} }
impl Tr for crate::Foo { fn m(&self) {} }
impl<T> Foo { pub(crate) fn g(&self) {} }
"""


@pytest.mark.parametrize(
    "lang,src,want",
    [
        ("go", GO_SYNTAX, {"A", "B", "C", "D", "E", "F", "I", "I.M", "G", "G.Get", "f"}),  # E, F: one spec, two names
        ("rust", RUST_SYNTAX, {"Foo", "Tr", "Tr.m", "f64.m", "Foo.m", "Box.m", "_.m", "Foo.g"}),
    ],
    ids=["go", "rust"],
)
def test_real_grammar_reads_the_syntax_the_recorded_parses_cannot_check(lang, src, want):
    """Grouped const / var blocks, interface members (method_elem), generic receivers, every impl type expression."""
    problem = _grammar_problem(lang)
    if problem:
        pytest.skip(f"tree-sitter {lang} grammar is not installed on this host: {problem}")
    tg.set_backend(None)
    facts = contracts.get_extractor(lang).extract(lang, "x." + ("go" if lang == "go" else "rs"), src)
    assert facts.errors == 0
    assert {d.qual for d in facts.defs if d.qual} == want  # a local `var` is no package-level def


RUST_USES = """pub struct Ctx;
impl Ctx { fn get<T>(&self) -> T { loop {} } }
pub struct Config {
    #[serde(default = "default_port", skip_serializing_if = "is_zero")]
    port: u16,
}
fn use_it(c: Ctx, d: &Ctx) {
    let _ = c.get::<u8>();
    let _ = self.handle::<Foo>(1);
    let _ = Vec::<u8>::new();
}
#[pymodule]
fn native() {}
#[cortex_m_rt::entry]
fn start() {}
#[unsafe(no_mangle)]
pub extern "C" fn exported() {}
#[cfg_attr(feature = "py", pyclass)]
struct Gated;
#[used]
static KEEP: u8 = 1;
"""
GO_USES = """package p

import (
	"container/heap"
	"example.com/app/api/v1"
	"example.com/lib/v2"
)

//go:linkname hook runtime.hook
func hook() {}

func f(h any) {
	heap.Push(h, 3)
	lib.Open()
	v1.NewThing()
}
"""


@pytest.mark.parametrize(
    "lang,src,refs,roots",
    [
        (
            "rust",
            RUST_USES,
            {"c.get", "self.handle", "Vec.new", "default_port", "is_zero"},
            {"native", "start", "exported", "Gated", "KEEP"},
        ),
        ("go", GO_USES, {"Push", "Open", "NewThing"}, {"hook"}),
    ],
    ids=["rust", "go"],
)
def test_real_grammar_reads_method_turbofish_attribute_strings_and_package_aliases(lang, src, refs, roots):
    """The constructs fixed after review: they have no recorded match that could stand in for the real grammar."""
    problem = _grammar_problem(lang)
    if problem:
        pytest.skip(f"tree-sitter {lang} grammar is not installed on this host: {problem}")
    tg.set_backend(None)
    facts = contracts.get_extractor(lang).extract(lang, "x." + ("go" if lang == "go" else "rs"), src)
    assert facts.errors == 0
    assert refs <= {r.name for r in facts.refs} and roots <= {r.target for r in facts.roots}


RUST_ASSOC = """use std::ops::Add;
pub struct Point;
impl Add for Point {
    type Output = Point;
    fn add(self, o: Point) -> Point { self }
}
impl Point {
    const MAX: u32 = 8;
    pub const OPEN: u32 = 2;
}
pub trait Shape {
    const SIDES: u32;
    type Unit;
}
impl Shape for Point {
    const SIDES: u32 = 4;
    type Unit = u8;
}
type Alias = Point;
mod inner {
    pub type Id = u32;
}
const _: () = ();
"""
GO_ASSOC = """package p

type Worker struct{}

var _ = (*Worker)(nil)

const _ = 1
"""


@pytest.mark.parametrize(
    "lang,src,want",
    [
        (
            "rust",
            RUST_ASSOC,
            {
                "Point",
                "Point.add",
                "Point.MAX",
                "Point.OPEN",
                "Shape",
                "Shape.SIDES",
                "Point.SIDES",
                "Alias",
                "inner",
                "inner.Id",
            },
        ),
        ("go", GO_ASSOC, {"Worker"}),
    ],
    ids=["rust", "go"],
)
def test_real_grammar_reads_associated_items_and_skips_blank_definitions(lang, src, want):
    """Reviewed: an associated type of an impl (Output, Unit) is no def, associated consts are members, `_` is no def."""
    problem = _grammar_problem(lang)
    if problem:
        pytest.skip(f"tree-sitter {lang} grammar is not installed on this host: {problem}")
    tg.set_backend(None)
    facts = contracts.get_extractor(lang).extract(lang, "x." + ("go" if lang == "go" else "rs"), src)
    assert facts.errors == 0
    assert {d.qual for d in facts.defs if d.qual} == want


RUST_MODS = """mod utils {
    pub fn helper() {}
    pub mod deep {
        pub fn leaf() {}
    }
}
use utils::{helper, deep::leaf};
use crate::utils::helper as renamed;
use prelude::*;
pub struct Wrapper<T>(T);
impl<T: Default> Wrapper<T> {
    pub fn new() -> Self { loop {} }
}
fn main() {
    helper();
    Wrapper::<Vec<u8>>::new();
    HashMap::<String, Vec<u8>>::new();
}
"""
GO_SHADOW = """package p

import (
	"example.com/app/config"
	"example.com/app/user"
)

func run(config *config.Config) { config.Validate() }

func save() {
	user := user.New()
	user.Save()
}

func each(xs []int, ch chan int) {
	for config := range xs {
		config.Range()
	}
	switch user := any(1).(type) {
	case int:
		user.Switch()
	}
	select {
	case config := <-ch:
		config.Recv()
	}
}

func variadic(user ...int) { user.Rest() }

func plain() { config.Load() }
"""
GO_EMBED = """package p

import pb "example.com/app/pb"

type A struct{ pb.Unimplemented }
type B struct{ *pb.Unimplemented }
type C struct{ *Base }
type D struct{ Base[int] }
type E struct{ x pb.Other }
type F struct{ Base }
"""


@pytest.mark.parametrize(
    "lang,src,defs,imports,refs",
    [
        (
            "rust",
            RUST_MODS,
            {"utils", "utils.helper", "utils.deep", "utils.deep.leaf", "Wrapper", "Wrapper.new", "main"},
            {("utils", ("helper",)), ("utils::deep", ("leaf",)), ("crate::utils", ("helper",)), ("prelude", ("*",))},
            {"Wrapper.new", "HashMap.new", "helper"},
        ),
    ],
    ids=["rust"],
)
def test_real_grammar_reads_inline_modules_uses_and_nested_turbofish(lang, src, defs, imports, refs):
    """Reviewed: items of an inline mod imported with use, and `Wrapper::<Vec<u8>>::new` (a nested turbofish)."""
    problem = _grammar_problem(lang)
    if problem:
        pytest.skip(f"tree-sitter {lang} grammar is not installed on this host: {problem}")
    tg.set_backend(None)
    facts = contracts.get_extractor(lang).extract(lang, "x.rs", src)
    assert facts.errors == 0
    assert {d.qual for d in facts.defs if d.qual} == defs
    assert imports <= {(i.module, i.names) for i in facts.imports} and refs <= {r.name for r in facts.refs}
    assert not any(">" in r.name or "<" in r.name for r in facts.refs if r.kind == "call")  # no generic left in a path


def test_real_grammar_reads_shadowing_locals_and_every_embedded_form_in_go():
    """Reviewed: parameters, :=, range, type switch, select and variadic bind a name that shadows a package; embedding of
    pb.X, *pb.X, *X and X[T] is an inherit ref (a named field `x pb.Other` is not)."""
    problem = _grammar_problem("go")
    if problem:
        pytest.skip(f"tree-sitter go grammar is not installed on this host: {problem}")
    tg.set_backend(None)
    ex = contracts.get_extractor("go")
    got = {(r.name, r.scope) for r in ex.extract("go", "x.go", GO_SHADOW).refs if r.kind == "call"}
    shadowed = {
        ("config.Validate", "run"),
        ("user.New", "save"),
        ("user.Save", "save"),
        ("config.Range", "each"),
        ("user.Switch", "each"),
        ("config.Recv", "each"),
        ("user.Rest", "variadic"),
    }
    assert shadowed <= got and {("Validate", "run"), ("Rest", "variadic"), ("Load", "plain")} <= got
    assert ("config.Load", "plain") not in got  # nothing binds config in plain: only the package function
    f = ex.extract("go", "x.go", GO_EMBED)
    assert f.errors == 0
    assert {(r.name, r.scope) for r in f.refs if r.kind == "inherit"} == {
        ("Unimplemented", "A"),
        ("Unimplemented", "B"),
        ("Base", "C"),
        ("Base", "D"),
        ("Base", "F"),
    }


RUST_PATH = """cfg_if::cfg_if! {
    if #[cfg(unix)] {
        mod unix;
        use unix as plat;
    } else {
        #[path = "win/mod.rs"]
        mod win;
    }
}

#[cfg(unix)]
#[path = "sys/unix.rs"]
mod imp;

/// docs
#[cfg_attr(windows, path = "sys/windows.rs")]
mod other;

#[path = "x.rs"]
fn not_a_mod() {}

mod plain;
"""


def test_real_grammar_reads_path_attributes_before_mod_and_the_items_of_cfg_if():
    """Reviewed: `#[path = ".."] mod x;` (also stacked with cfg, behind a doc comment, in cfg_attr) and the declarations
    inside cfg_if! { .. } name files; the attribute of a fn belongs to no mod."""
    problem = _grammar_problem("rust")
    if problem:
        pytest.skip(f"tree-sitter rust grammar is not installed on this host: {problem}")
    tg.set_backend(None)
    facts = contracts.get_extractor("rust").extract("rust", "x.rs", RUST_PATH)
    assert facts.errors == 0
    assert {(i.module, i.alias) for i in facts.imports} == {
        ("self::unix", "unix"),
        ("unix", "plat"),
        ("./win/mod.rs", "win"),
        ("./sys/unix.rs", "imp"),
        ("./sys/windows.rs", "other"),
        ("self::plain", "plain"),
    }


# ====================================================================== counterexamples of an earlier re-check
# A lib + bin package at the top of the repo: its name is in Cargo.toml, not in a directory, so a path that starts with the crate's
# own name is read inside the library crates (link.py lib_path): `use mycrate::cli;`, `use mycrate::cli::{self, Args};`, `mycrate::run()`.
CLI = [("parse", "func"), ("Args", "class"), ("Args.new", "method"), ("spare", "func")]
SELF_NAME = {
    "use-module": (
        [md.Import("mycrate", ("cli",), "", 1)],
        [("cli.parse", "call", "main"), ("cli.Args.new", "call", "main")],
        {"cli.rs#parse": "alive", "cli.rs#Args": "alive", "cli.rs#Args.new": "alive", "cli.rs#spare": "dead"},
    ),
    "use-self-and-item": (  # use mycrate::cli::{self, Args}; cli::parse(); Args::new()
        [md.Import("mycrate::cli", (), "", 1), md.Import("mycrate::cli", ("Args",), "", 1)],
        [("cli.parse", "call", "main"), ("Args.new", "call", "main")],
        {"cli.rs#parse": "alive", "cli.rs#Args": "alive", "cli.rs#Args.new": "alive", "cli.rs#spare": "dead"},
    ),
    "use-alias": (  # use mycrate::cli as c; c::parse()
        [md.Import("mycrate::cli", (), "c", 1)],
        [("c.parse", "call", "main")],
        {"cli.rs#parse": "alive", "cli.rs#Args": "dead", "cli.rs#Args.new": "dead", "cli.rs#spare": "dead"},
    ),
    "no-use-item": (  # fn main() { mycrate::run(); }
        [],
        [("mycrate.run", "call", "main")],
        {"lib.rs#run": "weak-alive", "lib.rs#spare": "dead", "cli.rs#parse": "dead"},
    ),
    "no-use-module-path": (  # fn main() { mycrate::cli::parse(); mycrate::cli::Args::new(); }
        [],
        [("mycrate.cli.parse", "call", "main"), ("mycrate.cli.Args.new", "call", "main")],
        {
            "cli.rs#parse": "weak-alive",
            "cli.rs#Args": "weak-alive",
            "cli.rs#Args.new": "weak-alive",
            "cli.rs#spare": "dead",
        },
    ),
}


@pytest.mark.parametrize("case", sorted(SELF_NAME))
@pytest.mark.parametrize("user", ["src/main.rs", "src/bin/tool.rs", "tests/api.rs", "examples/demo.rs"])
def test_a_lib_bin_package_is_reached_through_its_own_name(case, user):
    ims, calls, want = SELF_NAME[case]
    files = {
        user: ([("main", "func")], calls, ims),
        "src/lib.rs": ([("run", "func"), ("spare", "func")], [], []),
        "src/cli.rs": (CLI, [], []),
    }
    got = rs_verdicts(rs_files(files))
    assert {k.split("/", 1)[1]: v for k, v in got.items() if k.startswith("src/") and not k.endswith("#main")} == {
        **{"lib.rs#run": "dead", "lib.rs#spare": "dead", **{f"cli.rs#{q}": "dead" for q, _ in CLI}},
        **want,
    }


def test_a_path_that_starts_with_a_dependency_or_the_toolchain_is_not_the_crate():
    """`use serde::de::Visitor` with no `de` module in the crate stays external; std, core and alloc never are the crate; a repo
    with no library crate has no crate to name. (`app.run()` on a value of unknown type may still match a root fn of the library by
    its name, N: the text does not tell a path from a method call.)"""
    ims = [md.Import("serde::de", ("Visitor",), "", 1), md.Import("std::fmt", (), "", 2)]
    calls = [("std.fmt", "call", "main"), ("core.parse", "call", "main"), ("Visitor.visit", "call", "main")]
    files = {
        "src/main.rs": ([("main", "func")], calls, ims),
        "src/lib.rs": ([("fmt", "func"), ("spare", "func")], [], []),
        "src/cli.rs": ([("parse", "func")], [], []),
    }
    got = rs_verdicts(rs_files(files))
    assert (got["src/lib.rs#fmt"], got["src/lib.rs#spare"], got["src/cli.rs#parse"]) == ("dead", "dead", "dead")
    bins = {
        "src/main.rs": ([("main", "func")], [("mycrate.run", "call", "main")], []),
        "tool/src/main.rs": ([("run", "func")], [], []),
    }
    assert rs_verdicts(rs_files(bins))["tool/src/main.rs#run"] == "dead"


def test_a_member_found_by_its_qualified_name_in_a_module_uses_its_type_as_well():
    """`m::Foo::new()` with m a module file: Foo.new alone was alive, Foo itself dead."""
    files = {
        "src/main.rs": ([("main", "func")], [("m.Foo.new", "call", "main")], [md.Import("self::m", (), "m", 1)]),
        "src/m.rs": ([("Foo", "class"), ("Foo.new", "method"), ("Foo.spare", "method"), ("Bar", "class")], [], []),
    }
    got = rs_verdicts(rs_files(files))
    assert (got["src/m.rs#Foo"], got["src/m.rs#Foo.new"], got["src/m.rs#Foo.spare"], got["src/m.rs#Bar"]) == (
        "alive",
        "alive",
        "dead",
        "dead",
    )


# Go: the `package` clause is not the last element of the import path, so `storage.New()` has a receiver that is no known alias
GO_RECV = [
    # (receiver, name, aliases, locals, expected names)
    ("storage", "New", {"store"}, (), ["storage.New", "New"]),  # package storage in .../internal/store
    ("store", "New", {"store"}, (), ["New"]),  # the alias the path gives: the bare name only
    ("store", "New", {"store"}, {"store"}, ["New", "store.New"]),  # shadowed by a local: both spellings
    ("conn", "Close", set(), {"conn"}, ["conn.Close"]),  # a local: a method call
    ("s.db", "Query", set(), (), ["s.db.Query"]),  # a field is no package
    ("", "run", set(), (), ["run"]),
]


@pytest.mark.parametrize("obj,name,aliases,local,want", GO_RECV)
def test_go_a_receiver_that_may_be_an_unknown_package_records_the_bare_name_too(obj, name, aliases, local, want):
    rl = tl.rules("go")
    assert sorted(tl.ref_names(rl, name, obj, aliases, local)) == sorted(want)


def test_go_the_bare_name_of_an_unknown_receiver_is_a_rule_of_the_language_not_of_every_one():
    assert tl.ref_names(tl.rules("go"), "New", "storage", {"store"}) == ["storage.New", "New"]
    assert tl.ref_names({**tl.rules("go"), "bare_unknown_recv": False}, "New", "storage", {"store"}) == ["storage.New"]
    assert tl.ref_names(tl.rules("rust"), "new", "storage", set()) == ["storage.new"]  # rust paths are not packages


GO_STORE = "package storage\n\nfunc New() int { return 1 }\n\nfunc Spare() int { return 2 }\n"
GO_USE = 'package main\n\nimport "example.com/app/internal/store"\n\nfunc main() {\n\tstorage.New()\n}\n'


def test_go_a_package_clause_that_differs_from_the_directory_keeps_its_functions_weak_alive():
    """Counterexample of an earlier re-check: import ".../internal/store" with `package storage` in it, called as storage.New()."""
    problem = _grammar_problem("go")
    if problem:
        pytest.skip(f"tree-sitter go grammar is not installed on this host: {problem}")
    tg.set_backend(None)
    ex = contracts.get_extractor("go")
    files = {
        "main.go": ex.extract("go", "main.go", GO_USE),
        "internal/store/s.go": ex.extract("go", "internal/store/s.go", GO_STORE),
    }
    assert {r.name for r in files["main.go"].refs if r.kind == "call"} >= {"storage.New", "New"}
    res = dlink.link(files, workers=1)
    rr = rc.analyze(res.graph, "closed_world")
    got = {n.key.split(":", 1)[1]: rr.verdict(n.key) for n in res.graph.nodes if n.kind != "file"}
    assert got["internal/store/s.go#New"] == "weak-alive" and got["internal/store/s.go#Spare"] == "dead"
