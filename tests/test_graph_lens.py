"""graph.lens, graph.extract.bodyhash and the lenses of the viewer (unused / duplicates / architecture / health).

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_lens.py

Synthetic fixtures only. The normalised body hash (python by ast, the tree-sitter languages by the text of the definition node), the keys
of the definitions against the linker's, the duplicate groups, the module graph (strongly connected components, layers), the health score,
the blob and the run directory files, the SVG of the publication, the wiring (a run has the lens files and the page carries the blob),
the viewer's lens logic under node (it runs when node is installed). The wiring tests run the real Python extractor on a small git
repository (no grammar is needed); a headless browser is not used.
"""

import ast
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from graph_support import commit_all, git, new_repo, write

from dead_code_checker.graph import artifacts, cli, contracts, levels, lens, link, model, paths, reach, snippets, viz
from dead_code_checker.graph.extract import bodyhash, common, py as dcpy, ts_generic as tg
from dead_code_checker.graph.model import Def, Facts, canonical, edge, node, root

ROOT = Path(__file__).resolve().parent.parent
VIEWER = ROOT / "dead_code_checker" / "graph" / "viewer"
NODE = shutil.which("node")
DATA = lens.load_data()

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test


BODY = '''
def {name}(items, limit):
    """{doc}"""
    total = 0
    seen = []
    for item in items:  # comment {name}
        if item in seen:
            continue
        seen.append(item)
        total += len(item) * 2
        if total > limit:
            break
    return total, seen
'''


def fn(src):
    return ast.parse(src).body[0]


# ---------------------------------------------------------------- the body hash: python
def test_py_body_ignores_names_of_locals_comments_docstring_and_the_function_name():
    a = bodyhash.py_body(fn(BODY.format(name="scan_a", doc="first")))
    b = bodyhash.py_body(fn(BODY.format(name="other", doc="second").replace("item", "entry").replace("total", "acc")))
    assert a[0].startswith("py:") and a == b and a[1] >= DATA["dup"]["min_size"]["python"]


def test_py_body_sees_a_changed_operation_a_global_a_constant_and_a_decorator():
    base = bodyhash.py_body(fn(BODY.format(name="f", doc="d")))[0]
    for bad in (
        BODY.format(name="f", doc="d").replace("* 2", "* 3"),  # a constant
        BODY.format(name="f", doc="d").replace("len(item)", "size(item)"),  # a global the function reads
        BODY.format(name="f", doc="d").replace("total > limit", "total >= limit"),  # an operator
        "@cache\n" + BODY.format(name="f", doc="d").lstrip("\n"),  # a decorator: another function in effect
    ):
        assert bodyhash.py_body(fn(bad))[0] != base, bad


def test_py_body_global_and_nonlocal_names_are_not_renamed():
    src = "def f(a):\n    global G\n    G = a + 1\n    for i in range(a):\n        G += i * 3\n        G -= i // 2\n    return G\n"
    other = src.replace("G", "H")
    assert bodyhash.py_body(fn(src))[0] != bodyhash.py_body(fn(other))[0]  # a global is part of the meaning


def test_py_body_async_nested_defs_and_handlers_are_stable():
    src = (
        "async def f(a, b):\n    def inner(x):\n        return x + a + 1\n    try:\n        r = await b(inner(2))\n"
        "    except ValueError as err:\n        r = str(err) + 'x'\n    return [y * 2 for y in r if y]\n"
    )
    ren = src.replace("inner", "helper").replace("err", "exc").replace("r =", "res =").replace("return [y", "return [z").replace("y *", "z *").replace("for y", "for z").replace("if y", "if z").replace("(r)", "(res)").replace("r if", "res if").replace("str(err)", "str(exc)")  # fmt: skip
    ren = ren.replace("for y in r", "for z in res")
    a, b = bodyhash.py_body(fn(src)), bodyhash.py_body(fn(ren))
    assert a[0] and a == bodyhash.py_body(fn(src))  # deterministic
    assert b[0]  # a different spelling of the same function hashes too


def test_py_body_small_bodies_and_huge_ones_have_none(monkeypatch):
    assert bodyhash.py_body(fn("def f():\n    return 1\n")) == bodyhash.NONE
    monkeypatch.setitem(bodyhash._data()[0]["dup"], "max_body", 10)
    assert bodyhash.py_body(fn(BODY.format(name="f", doc="d"))) == bodyhash.NONE


# ---------------------------------------------------------------- the body hash: tree-sitter languages (text)
JS_A = "function render(a, b) { // hi\n  let out = [];\n  for (let i = 0; i < a.length; i++) {\n    if (a[i] > b) { out.push(a[i] * 2); }\n  }\n  return out;\n}"
JS_B = "function draw(x, y) {\n  /* other comment */ let acc = [];\n  for (let k = 0; k < x.length; k++) {\n    if (x[k] > y) { acc.push(x[k] * 2); }\n  }\n  return acc;\n}"


def span(text, name):
    i = text.index(name)
    return i, i + len(name)


def test_text_body_js_equal_after_renaming_locals_and_dropping_comments():
    a = bodyhash.text_body("javascript", JS_A, span(JS_A, "render"))
    b = bodyhash.text_body("javascript", JS_B, span(JS_B, "draw"))
    assert a[0].startswith("ts:") and a == b and a[1] > DATA["dup"]["store_min"]
    assert bodyhash.text_body("javascript", JS_A.replace("* 2", "* 3"), span(JS_A, "render"))[0] != a[0]
    assert (
        bodyhash.text_body("javascript", JS_A.replace("out.push", "out.unshift"), span(JS_A, "render"))[0] != a[0]
    )  # a member name stays


def test_text_body_c_family_names_params_by_type_first():
    c1 = "int total(int *items, size_t n) {\n  int acc = 0; /* sum */\n  for (size_t i = 0; i < n; i++) {\n    if (items[i] > 0) { acc += items[i] * 2; }\n  }\n  return acc;\n}"
    c2 = "int sum2(int *vals, size_t count) {\n  int s = 0; // twice\n  for (size_t j = 0; j < count; j++) {\n    if (vals[j] > 0) { s += vals[j] * 2; }\n  }\n  return s;\n}"
    a, b = bodyhash.text_body("c", c1, span(c1, "total")), bodyhash.text_body("c", c2, span(c2, "sum2"))
    assert a[0] and a == b
    d = bodyhash.text_body("c", c2.replace("> 0", "> 1"), span(c2, "sum2"))
    assert d[0] != b[0]


def test_text_body_comment_styles_per_language_and_strings_keep_their_slashes():
    sh = 'f() {\n  local a=$1  # note\n  b=$2\n  echo "$a // $b"; echo done; echo more; echo again; echo x; echo y\n}'
    sh2 = sh.replace("# note", "# other note")
    assert bodyhash.text_body("shell", sh)[0] == bodyhash.text_body("shell", sh2)[0]
    assert (
        bodyhash.text_body("shell", sh)[0] != bodyhash.text_body("shell", sh.replace("$a // $b", "$a / $b"))[0]
    )  # the string is code
    lua = "function f(a)\n  local t = {}  -- c\n  for i = 1, a do t[#t + 1] = i * 2 + a end\n  --[[ block\n  comment ]]\n  return t\nend"
    assert bodyhash.text_body("lua", lua)[0] == bodyhash.text_body("lua", lua.replace("-- c", "-- zzz"))[0]


def test_text_body_names_that_are_not_declared_stay_as_written():
    a = "function f(a) {\n  total = total + a.len;\n  count = count + 1;\n  return total * count + helper(a) + other(a) + 1;\n}"
    b = a.replace("helper", "assist")
    assert bodyhash.text_body("javascript", a)[0] != bodyhash.text_body("javascript", b)[0]


def test_text_body_small_has_none():
    assert bodyhash.text_body("javascript", "function f() { return 1; }") == bodyhash.NONE


def test_data_sha_follows_the_data_file(tmp_path, monkeypatch):
    sha = bodyhash.data_sha()
    assert re.fullmatch(r"[0-9a-f]{12}", sha)
    f = tmp_path / "lens.json"
    f.write_text(json.dumps(dict(DATA, note="edited")))
    monkeypatch.setattr(bodyhash, "DATA", str(f))
    bodyhash._data.cache_clear()
    try:
        assert bodyhash.data_sha() != sha
    finally:
        monkeypatch.undo()
        bodyhash._data.cache_clear()


# ---------------------------------------------------------------- the facts carry it
def test_facts_payload_roundtrip_and_an_old_payload_without_the_fields():
    f = Facts("python", defs=(Def("a", "func", 1, 3, "public", 0, -1, "", "py:abc", 40), Def("b", "func", 5, 6)))
    back = Facts.from_payload(f.to_payload())
    assert back == f and back.defs[0].bh == "py:abc" and back.defs[1].bsz == 0
    doc = json.loads(f.to_payload())
    doc["cols"]["defs"] = [r[:8] for r in doc["cols"]["defs"]]  # a shard written before the fields existed
    old = Facts.from_payload(json.dumps(doc).encode())
    assert old.defs[0].bh == "" and old.defs[0].bsz == 0 and old.defs[0].qual == "a"


def test_python_extractor_hashes_functions_and_methods_not_classes_or_stubs():
    text = (
        "class K:\n    def m(self, items, limit):\n        total = 0\n        seen = []\n        for item in items:\n            if item in seen:\n                continue\n            seen.append(item)\n            total += len(item) * 2\n            if total > limit:\n                break\n        return total, seen\n\n    def stub(self):\n        pass\n"
        + BODY.format(name="f", doc="x")
    )
    f = dcpy.PyExtractor().extract("python", "k.py", text)
    by = {d.qual: d for d in f.defs}
    assert by["K"].bh == "" and by["K.stub"].bh == ""
    assert by["K.m"].bh.startswith("py:") and by["f"].bh.startswith("py:") and by["K.m"].bsz >= 30
    assert (
        by["K.m"].bh != by["f"].bh
    )  # `self` is a parameter of m: the same loop in a function with other parameters is another body


def test_python_shard_key_and_ext_version_follow_the_body_hash(monkeypatch):
    ex = dcpy.PyExtractor()
    sha = ex.grammar_sha("python")
    monkeypatch.setattr(bodyhash, "data_sha", lambda: "x" * 12)
    assert ex.grammar_sha("python") != sha
    assert ex.version >= 6 and tg.TsGeneric.version >= 5


def test_ts_body_helper_uses_the_byte_offsets_of_the_name_even_with_unicode():
    text = "// é\nfunction rendér(a, b) {\n  let out = [];\n  for (let i = 0; i < a.length; i++) {\n    if (a[i] > b) { out.push(a[i] * 2); }\n  }\n  return out;\n}\n"
    data = text.encode()
    s = text.index("function")
    e = text.rindex("}") + 1
    bs, be = len(text[:s].encode()), len(text[:e].encode())
    n0 = len(text[: text.index("rendér")].encode())
    plain = bodyhash.text_body("javascript", JS_A, span(JS_A, "render"))
    got = tg._body("javascript", data, (bs, be), (n0, n0 + len("rendér".encode())))
    assert got[1] == plain[1]
    assert (
        tg._body("javascript", data, (bs, be), None)[0] != got[0]
    )  # without the name the name token is part of the body


def test_merge_defs_drops_the_hash_of_a_name_defined_twice():
    a = Def("f", "func", 1, 5, "public", 0, -1, "", "py:1", 40)
    b = Def("f", "func", 9, 14, "public", 0, -1, "", "py:2", 41)
    (m,) = common.merge_defs([a, b])
    assert m.bh == "" and m.bsz == 0
    (one,) = common.merge_defs([a])
    assert one.bh == "py:1"


# ---------------------------------------------------------------- keys of definitions = the linker's
def test_def_keys_are_the_keys_the_linker_gives():
    twice = "def f(a):\n    return a\n\n\ndef f(a, b):\n    return b\n\n\ndef g():\n    pass\n"
    facts = {}
    for path, text in {"m.py": twice, "n.py": BODY.format(name="x", doc="d")}.items():
        facts[path] = dcpy.PyExtractor().extract("python", path, text)
    # a repeated name with one arity: two defs share qual and arity -> the h8 suffix
    facts["r.py"] = Facts(
        "python", defs=(Def("h", "func", 1, 2, arity=1), Def("h", "func", 4, 5, arity=1), Def("k", "func", 7, 8))
    )
    g = link.link(facts).graph
    have = {n.key for n in g.nodes if n.kind != "file"}
    mine = {k for p, f in facts.items() for _d, k in lens.def_keys(f.lang, p, f.defs)}
    assert mine == have and any("~" in k for k in have)


# ---------------------------------------------------------------- duplicate groups
def mk_facts(spec):
    """{path: [(qual, kind, bh, bsz)]} -> {path: Facts} with consecutive lines."""
    out = {}
    for path, defs in spec.items():
        lang = "javascript" if path.endswith(".js") else "python"
        out[path] = Facts(
            lang,
            defs=tuple(
                Def(q, k, 1 + 10 * i, 8 + 10 * i, "public", 0, -1, "", bh, sz) for i, (q, k, bh, sz) in enumerate(defs)
            ),
        )
    return out


def keys_of(facts):
    return {k for p, f in facts.items() for _d, k in lens.def_keys(f.lang, p, f.defs)}


def test_dup_groups_thresholds_kinds_generated_order_and_languages():
    cfg = DATA["dup"]
    facts = mk_facts(
        {
            "a/x.py": [
                ("f", "func", "py:A", 100),
                ("g", "func", "py:B", 35),
                ("C", "class", "py:K", 200),
                ("tiny", "func", "py:T", 10),
            ],
            "b/y.py": [
                ("f2", "func", "py:A", 100),
                ("g2", "func", "py:B", 35),
                ("C2", "class", "py:K", 200),
                ("tiny2", "func", "py:T", 10),
            ],
            "c/z.py": [("f3", "func", "py:A", 100), ("lone", "func", "py:L", 100)],
            "w/u.js": [("a", "func", "ts:J", 60), ("b", "func", "ts:J", 60)],
            "w/v.js": [
                ("c", "func", "ts:S", 30),
                ("d", "func", "ts:S", 30),
            ],  # under the javascript default min_size 40
        }
    )
    keys = keys_of(facts)
    flags = {"python:c/z.py#f3": model.F_GENERATED}  # generated code is no duplicate
    gs = lens.dup_groups(facts, keys, flags, cfg)
    names = [[m[2].qual for m in g] for g in gs]
    assert names == [
        ["f", "f2"],
        ["a", "b"],
        ["g", "g2"],
    ], names  # ranked by size * (n - 1), 100 > 60 > 35; class and tiny never
    assert all(m[0] <= n[0] for g in gs for m, n in zip(g, g[1:]))  # members by path
    assert lens.dup_groups(facts, set(), {}, cfg) == []  # a def that is no node of the graph is no member


def test_dup_groups_need_min_group_members(monkeypatch):
    facts = mk_facts({"a.py": [("f", "func", "py:A", 100)], "b.py": [("g", "func", "py:B", 100)]})
    assert lens.dup_groups(facts, keys_of(facts), {}, DATA["dup"]) == []


# ---------------------------------------------------------------- graph algorithms
def test_scc_chain_cycle_and_selfloop_free():
    assert lens.scc(0, []) == []
    comp = lens.scc(5, [[1], [2], [0], [4], []])
    assert comp[0] == comp[1] == comp[2] and len({comp[3], comp[4], comp[0]}) == 3


def test_layers_chain_diamond_cycle_and_determinism():
    lay, comp, n = lens.layers(4, [(0, 1), (0, 2), (1, 3), (2, 3)])
    assert lay == [0, 1, 1, 2] and n == 3
    lay, comp, n = lens.layers(5, [(0, 1), (1, 2), (2, 1), (2, 3), (4, 3)])  # 1 <-> 2 is a cycle: one layer
    assert lay[1] == lay[2] == 1 and lay[3] == 2 and lay[0] == 0 and lay[4] == 0 and comp[1] == comp[2] != comp[0]
    assert lens.layers(4, [(3, 0), (2, 3), (1, 2)])[0] == [3, 0, 1, 2]  # a chain against the vertex numbers
    e = [(0, 1), (1, 2), (3, 2), (2, 4)]
    assert lens.layers(5, e) == lens.layers(5, list(reversed(e)))  # the order of the edges does not matter
    assert lens.layers(0, []) == ([], [], 0)


def test_longest_path_decides_the_layer():
    lay, _c, n = lens.layers(4, [(0, 1), (1, 2), (0, 2), (2, 3)])
    assert lay == [0, 1, 2, 3] and n == 4


def test_layers_and_scc_are_iterative_a_chain_of_20000_modules_does_not_recurse():
    n = 20000
    lay, comp, nl = lens.layers(n, [(i, i + 1) for i in range(n - 1)] + [(n - 1, n - 2)])  # the last two are a cycle
    assert nl == n - 1 and lay[0] == 0 and lay[n - 1] == lay[n - 2] == n - 2 and comp[n - 1] == comp[n - 2]


def test_a_big_module_graph_is_cut_by_the_data_caps_and_counted():
    n = 3000
    nodes, es = [], []
    for i in range(n):
        nodes += [node(f"python:m{i // 10:04d}/f{i % 10}.py", "file", line0=1, line1=9)]
    for i in range(n):
        es.append(
            edge(
                f"python:m{i // 10:04d}/f{i % 10}.py",
                f"python:m{((i * 7) % n) // 10:04d}/f{((i * 7) % n) % 10}.py",
                "ref",
            )
        )
    g = canonical(nodes, es, [root("main", "x:1", node=nodes[0].key)])
    ln = lens.collect(g, reach.analyze(g), None)
    arch = lens.arch_doc(ln)
    assert ln.info["arch"]["modules"] == 300 and len(arch["mods"]) == 300 <= DATA["arch"]["max_modules"]
    small = json.loads(json.dumps(DATA))
    small["arch"]["max_modules"], small["arch"]["max_edges"] = 50, 40
    arch = lens.arch_doc(lens.collect(g, reach.analyze(g), None, data=small))
    assert (
        len(arch["mods"]) == 50 and arch["omitted"] == 250 and len(arch["edges"]) <= 40 and arch["omitted_edges"] >= 0
    )
    assert all(e[0] < 50 and e[1] < 50 for e in arch["edges"])


def test_module_of_depth_and_folding():
    assert (
        lens.module_of("a/b/c/d.py", 2) == "a/b"
        and lens.module_of("a/b/c/d.py", 0) == "a/b/c"
        and lens.module_of("x.py", 2) == "."
    )
    mods = lens._modules(
        ["a/b/x.py", "a/b/y.py", "a/c/z.py", "q.py"], {"a/b/x.py": 1, "a/b/y.py": 1, "a/c/z.py": 1, "q.py": 5}, 2, 3
    )
    assert mods["a/b/x.py"] == mods["a/c/z.py"] == "a" and mods["q.py"] == "."


def test_health_score_formula_and_grades_come_from_the_data():
    h = DATA["health"]
    assert (
        abs(sum(h["weights"].values()) - 1.0) < 1e-9
    ), "the weights add up to 1: a module with every penalty at 1 scores 0"
    assert lens.health_score(h, 0, 0, 0, False, 0) == 100.0
    assert lens.health_score(h, 1, 1, 10**6, True, 10**6) == 0.0
    assert lens.health_score(h, 0.5, 0, 0, False, 0) == round(100 * (1 - 0.5 * h["weights"]["dead"]), 1)
    assert lens.health_score(h, 0, 0, h["fan_cap"], False, 0) == round(100 * (1 - h["weights"]["fan"]), 1)
    assert lens.grade(h, 100) == h["grades"][0][1] and lens.grade(h, 0) == h["grades"][-1][1]
    lows = [g[0] for g in h["grades"]]
    assert lows == sorted(lows, reverse=True) and lows[-1] == 0


# ---------------------------------------------------------------- collect: one graph, the three chunks
def synth():
    """pkg/core <-> pkg/util (a cycle), app -> both, tools (python) -> web (javascript, an edge between languages); two duplicate
    functions, one of them dead."""
    spec = {
        "app/main.py": [("main", "func", "", 0)],
        "pkg/core/a.py": [("run", "func", "py:D", 80), ("dead_one", "func", "", 0), ("spare", "func", "", 0)],
        "pkg/util/b.py": [("pad", "func", "py:D", 80), ("x", "func", "", 0), ("y", "func", "", 0)],
        "tools/g.py": [("gen", "func", "", 0)],
        "web/u.js": [("ui", "func", "", 0)],
    }
    facts = mk_facts(spec)
    nodes, ix = [], {}
    for p, f in facts.items():
        nodes.append(node(f"{f.lang}:{p}", "file", line0=1, line1=40))
        for d, k in lens.def_keys(f.lang, p, f.defs):
            nodes.append(node(k, d.kind, line0=d.line0, line1=d.line1))
    key = lambda p, q: next(
        k for pp, f in facts.items() for _d, k in lens.def_keys(f.lang, pp, f.defs) if pp == p and k.endswith("#" + q)
    )  # noqa: E731
    es = [
        edge(key("app/main.py", "main"), key("pkg/core/a.py", "run"), "call"),
        edge(key("pkg/core/a.py", "run"), key("pkg/util/b.py", "pad"), "call"),
        edge(key("pkg/util/b.py", "pad"), key("pkg/core/a.py", "run"), "call"),
        edge(key("tools/g.py", "gen"), key("web/u.js", "ui"), "ref", "N"),
        edge(key("tools/g.py", "gen"), key("pkg/util/b.py", "x"), "call"),
    ]
    rs = [
        root("main", "app/main.py:1", node=key("app/main.py", "main")),
        root("main", "tools/g.py:1", node=key("tools/g.py", "gen")),
    ]
    g = canonical(nodes, es, rs)
    return g, reach.analyze(g), facts


def test_collect_builds_dup_arch_and_health_chunks():
    g, res, facts = synth()
    ln = lens.collect(g, res, facts)
    assert (
        set(ln.chunks) == {"arch", "health", "dup.0"}
        and ln.order == ("arch", "health", "dup.0")
        and ln.info["state"] == "on"
    )
    arch = lens.arch_doc(ln)
    mods = {m["n"]: m for m in arch["mods"]}
    assert set(mods) == {"app/main.py".rsplit("/", 1)[0], "pkg/core", "pkg/util", "tools", "web"}
    assert mods["pkg/core"]["c"] == mods["pkg/util"]["c"] >= 0 and mods["app"]["c"] == -1  # the cycle
    assert mods["pkg/core"]["l"] == mods["pkg/util"]["l"] == mods["app"]["l"] + 1  # a layer below its user
    ix = {m["n"]: i for i, m in enumerate(arch["mods"])}
    cross = [e for e in arch["edges"] if e[3]]
    assert cross == [[ix["tools"], ix["web"], 1, 1]]  # the one edge between python and javascript
    assert (
        arch["cycles"] == [["pkg/core", "pkg/util"]]
        and ln.info["arch"]["cycles"] == 1
        and ln.info["arch"]["cross_edges"] == 1
    )
    dup = json.loads(gzip.decompress(ln.chunks["dup.0"]))
    assert (
        [len(g_["m"]) for g_ in dup["groups"]] == [2]
        and ln.info["dup"]["groups"] == 1
        and ln.info["dup"]["members"] == 2
    )
    names = sorted(m[1] for m in dup["groups"][0]["m"])
    assert names == ["pad", "run"] and {dup["files"][m[0]] for m in dup["groups"][0]["m"]} == {
        "pkg/core/a.py",
        "pkg/util/b.py",
    }
    h = json.loads(gzip.decompress(ln.chunks["health"]))
    rows = {r["n"]: r for r in h["rows"]}
    core = rows["pkg/core"]
    assert core["k"] == 3 and core["dup"] == round(1 / 3, 3) and core["cyc"] == 1 and core["o"] == 1 and core["i"] == 2
    want = lens.health_score(DATA["health"], core["dead"], 1 / 3, 1, True, 3)
    assert abs(core["s"] - want) <= 0.1
    assert rows["app"]["s"] > core["s"]  # the module in a cycle with a dead symbol and a duplicate scores lower
    assert h["rows"][0]["g"] != "" and [r["g"] == "" for r in h["rows"]] == sorted(
        r["g"] == "" for r in h["rows"]
    )  # ranked first, unranked last


def test_collect_is_deterministic_and_does_not_need_facts():
    g, res, facts = synth()
    a, b = lens.collect(g, res, facts), lens.collect(g, res, lambda: facts)
    assert a.chunks == b.chunks and a.info == b.info  # gzip with mtime 0
    n = lens.collect(g, res, None)
    assert (
        n.info["dup"]["state"] == "off" and "facts" in n.info["dup"]["reason"] and set(n.chunks) == {"arch", "health"}
    )
    assert n.chunks["arch"] == a.chunks["arch"]


def test_collect_caps_are_counted_never_silent():
    g, res, facts = synth()
    data = json.loads(json.dumps(DATA))
    data["arch"]["max_modules"] = 2
    data["dup"]["max_members"] = 1
    ln = lens.collect(g, res, facts, data=data)
    arch = lens.arch_doc(ln)
    assert (
        len(arch["mods"]) == 2
        and arch["omitted"] == 3
        and ln.info["arch"]["shown"] == 2
        and ln.info["arch"]["modules"] == 5
    )
    assert all(e[0] < 2 and e[1] < 2 for e in arch["edges"])
    assert ln.info["dup"]["omitted_members"] == 1 and ln.info["dup"]["members"] == 2
    data["dup"]["max_groups"] = 0
    assert lens.collect(g, res, facts, data=data).info["dup"]["omitted_groups"] == 1


def test_a_dead_member_is_marked_with_its_verdict_and_the_suspect_word():
    g, res, facts = synth()
    ln = lens.collect(g, res, facts, suspects=["python:pkg/util/b.py#pad"])
    dup = json.loads(gzip.decompress(ln.chunks["dup.0"]))
    vds = {m[1]: m[4] for m in dup["groups"][0]["m"]}
    assert vds["pad"] == "suspect" and vds["run"] in ("alive", "weak-alive")


# ---------------------------------------------------------------- the blob, the run directory, the svg
def test_blob_layout_roundtrip_and_cap_leaves_chunks_for_the_folder():
    g, res, facts = synth()
    ln = lens.collect(g, res, facts)
    b = lens.blob(ln)
    assert b[:4] == b"DCL1"
    hl = int.from_bytes(b[4:8], "little")
    head = json.loads(b[8 : 8 + hl])
    assert (
        set(head["chunks"]) == {"arch", "health", "dup.0"}
        and head["info"]["left"] == []
        and head["info"]["state"] == "on"
    )
    for name, (off, ln_) in head["chunks"].items():
        assert b[8 + hl + off : 8 + hl + off + ln_] == ln.chunks[name]
    small = lens.blob(ln, cap=len(ln.chunks["arch"]) + 1)
    sh = json.loads(small[8 : 8 + int.from_bytes(small[4:8], "little")])
    assert list(sh["chunks"]) == ["arch"] and sh["info"]["left"] == [
        "health",
        "dup.0",
    ]  # whole chunks, in the priority order
    assert lens.blob(lens.off("x")) is None


def test_write_manifest_prefix_stale_chunks_and_foreign_files(tmp_path):
    g, res, facts = synth()
    ln = lens.collect(g, res, facts)
    d = tmp_path / "run"
    (d / "lens").mkdir(parents=True)
    (d / "lens" / "lens-dup.7.json.gz").write_bytes(b"old")  # a chunk of an earlier run: ours, goes
    (d / "lens" / "lens-arch.json.gz.tmp99").write_bytes(b"x")
    (d / "lens" / "notes.txt").write_text("mine")  # a foreign file stays
    assert lens.write(str(d), ln) == 4
    got = sorted(p.name for p in (d / "lens").iterdir())
    assert got == [
        "lens-arch.json.gz",
        "lens-dup.0.json.gz",
        "lens-health.json.gz",
        "lens-manifest.json.gz",
        "notes.txt",
    ]
    man = json.loads(gzip.decompress((d / "lens" / "lens-manifest.json.gz").read_bytes()))
    assert man["chunks"] == {k: f"lens-{k}.json.gz" for k in ln.chunks} and man["info"] == ln.info and man["v"] == 1
    assert lens.write(str(tmp_path / "none"), lens.off("x")) == 0 and not (tmp_path / "none").exists()


def test_arch_svg_has_names_and_counts_only_marks_cycles_and_cross_language_edges():
    import xml.etree.ElementTree as ET

    g, res, facts = synth()
    ln = lens.collect(g, res, facts)
    svg = lens.arch_svg(ln)
    ET.fromstring(svg)
    assert (
        "pkg/core" in svg and "#c0392b" in svg and "stroke-dasharray" in svg
    )  # a cycle outline, a dashed edge between languages
    for word in ("a.py", "b.py", "def ", "return", "pad", "dead_one", "main.py"):
        assert word not in svg, word
    assert lens.arch_svg(lens.off("x")) is None


# ---------------------------------------------------------------- the wiring
APP_PY = "def helper():\n    return 1\n\n\ndef run():\n    return helper() + 1\n\n\ndef main():\n    return run()\n\n\nif __name__ == '__main__':\n    main()\n"


@pytest.fixture(autouse=True)
def no_tools(monkeypatch):
    """The pages are made without Graphviz and without gitleaks: nothing here depends on what is installed."""
    monkeypatch.setattr(viz, "dot_path", lambda: None)
    monkeypatch.setattr(snippets, "find_gitleaks", lambda: None)


@pytest.fixture
def run_repo(tmp_path, hermetic_git):
    r = new_repo(
        tmp_path,
        {
            "lib/app.py": APP_PY,
            "lib/one.py": BODY.format(name="scan_one", doc="a") + "\n\ndef use():\n    return scan_one([], 1)\n",
            "lib/two.py": BODY.format(name="scan_two", doc="b").replace("item", "x"),
        },
    )
    write(r, "lib/app.py", APP_PY.replace("return helper() + 1", "return 42"))
    return r


def stored(repo, out, **kw):
    """levels.branch and artifacts.publish into the directory `out`: (run, sink)."""
    lay = paths.resolve(paths.find_root(repo), out)
    sink: dict = {}
    rep = levels.branch(str(repo), "HEAD", sink=sink, layout=lay)
    return artifacts.publish(lay, str(repo), "branch", list(rep.findings), dict(rep.meta), sink, **kw), sink


def pack_of(run_dir):
    return json.loads(gzip.decompress((Path(run_dir) / "graph.json.gz").read_bytes()))


def test_the_branch_sink_carries_the_facts_and_a_run_has_the_lens_files(run_repo, tmp_path):
    run, sink = stored(run_repo, tmp_path / "out")
    assert callable(sink["facts"]) and "lib/one.py" in sink["facts"]()
    d = Path(run.dir)
    assert (d / "lens" / "lens-manifest.json.gz").exists()
    m = re.search(r'id="dclens">([^<]+)<', (d / "index.html").read_text())
    assert m, "the page carries the lens blob"
    import base64

    blob = base64.b64decode(m.group(1))
    head = json.loads(blob[8 : 8 + int.from_bytes(blob[4:8], "little")])
    assert {"arch", "health", "dup.0"} <= set(head["chunks"]) and head["info"]["dup"]["groups"] == 1
    meta = pack_of(d)["head"]["meta"]["lens"]
    assert meta["state"] == "on" and meta["dup"]["chunks"] == ["dup.0"] and "python" in meta["dup"]["langs"]
    assert not any("lens" in f.code for f in run.findings if f.level in "EW")


def test_the_facts_getter_reopens_a_closed_store_and_has_none_without_a_path(run_repo):
    lay = paths.resolve(paths.find_root(run_repo))
    st, _ = levels.open_store(str(run_repo), lay)
    levels.persist(st, levels.build(str(run_repo), st))
    st.close()
    st, _ = levels.open_store(str(run_repo), lay)
    b = levels.build(str(run_repo), st)  # warm: the graph is read from the cache, the facts are not loaded
    st.close()  # the store of a run is closed when the pages are made
    assert levels.facts_getter(b, None)() == {}
    assert levels.facts_getter(b, str(lay.store.parent / "gone" / "x.sqlite"))() == {}
    assert levels.facts_getter(b, str(lay.store))().keys() >= {"lib/one.py", "lib/two.py"}


def test_a_warm_check_reads_the_facts_through_a_fresh_store(run_repo, tmp_path, capsys):
    """The graph of a warm branch run comes from the cache and its facts are never loaded; the store of the run is closed when the pages
    are made, so the facts getter of the sink opens a fresh one (a closed store answered with a lens that was off)."""
    outs = []
    for name in ("first", "second"):  # cold: the facts were extracted; warm: the graph is in the cache
        out = tmp_path / name
        rc = cli.main(["check", "--scope", "branch", "--base", "HEAD", "--root", str(run_repo), "--out", str(out)])
        assert rc == 0
        (d,) = [p for p in out.iterdir() if p.is_dir() and not p.is_symlink()]
        outs.append(d)
    capsys.readouterr()
    for d in outs:
        meta = pack_of(d)["head"]["meta"]["lens"]
        assert meta["state"] == "on", (d, meta)
        assert meta["dup"]["state"] == "on" and (d / "lens" / "lens-manifest.json.gz").exists()


def test_view_writes_the_lens_files_too(run_repo, tmp_path, capsys):
    out = tmp_path / "views"
    assert cli.main(["view", "--root", str(run_repo), "--out", str(out)]) == 0
    capsys.readouterr()
    (d,) = [p for p in out.iterdir() if p.is_dir() and not p.is_symlink()]
    meta = pack_of(d)["head"]["meta"]["lens"]
    assert meta["state"] == "on" and meta["dup"]["state"] == "on" and meta["dup"]["groups"] == 1
    assert {p.name for p in (d / "lens").iterdir()} == {
        "lens-arch.json.gz",
        "lens-dup.0.json.gz",
        "lens-health.json.gz",
        "lens-manifest.json.gz",
    }


def test_a_lens_that_fails_is_off_with_the_reason_never_a_failed_run(run_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(lens, "collect", lambda *a, **k: 1 / 0)
    run, _sink = stored(run_repo, tmp_path / "boom")
    d = Path(run.dir)
    assert d.exists() and (d / "index.html").exists() and not (d / "lens").exists()
    assert "ZeroDivisionError" in pack_of(d)["head"]["meta"]["lens"]["reason"]


def test_a_failed_write_of_the_lens_files_is_a_warning_and_the_pages_stay(run_repo, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(lens, "write", boom)
    run, _sink = stored(run_repo, tmp_path / "full")
    d = Path(run.dir)
    assert (d / "index.html").exists() and not (d / "lens").exists()
    assert any(f.code == "dc.viewer-lens-off" and "disk full" in f.msg for f in run.findings)


def test_a_stale_chunk_of_an_earlier_run_in_the_same_directory_goes_a_foreign_file_stays(tmp_path):
    g, res, facts = synth()
    d = tmp_path / "run"
    (d / "lens").mkdir(parents=True)
    (d / "lens" / "lens-dup.9.json.gz").write_bytes(b"old")
    (d / "lens" / "readme.txt").write_text("mine")
    viz.write_run(str(d), "repo", g, res, [], {"scope": "repo"}, lens=lens.collect(g, res, facts))
    assert sorted(p.name for p in (d / "lens").iterdir()) == [
        "lens-arch.json.gz",
        "lens-dup.0.json.gz",
        "lens-health.json.gz",
        "lens-manifest.json.gz",
        "readme.txt",
    ]


def test_prune_removes_the_lens_files_with_the_run(run_repo, tmp_path):
    run, _sink = stored(run_repo, tmp_path / "out")
    d = Path(run.dir)
    assert (d / "lens").is_dir() and artifacts._bytes(str(d)) > artifacts._bytes(str(d / "lens")) > 0
    shutil.rmtree(d)  # a run directory goes as a whole (graph.artifacts.prune does exactly this)
    assert not d.exists()


def test_a_publication_page_carries_no_lens():
    g, res, facts = synth()
    pg = viz.make_page(g, res, meta={"scope": "repo"})
    assert 'id="dclens"' not in pg.html and "lens" not in pg.pack.head["meta"]


def test_a_page_with_the_lens_carries_the_blob_and_the_info():
    g, res, facts = synth()
    ln = lens.collect(g, res, facts)
    pg = viz.make_page(g, res, lens=ln)
    assert pg.ok and pg.pack.head["meta"]["lens"]["left"] == [] and 'id="dclens"' in pg.html
    tiny = viz.make_page(g, res, lens=ln, limit=10)
    assert not tiny.ok  # the size loop still refuses a page that cannot fit


def test_a_page_whose_blob_cap_is_small_names_the_chunks_left_in_the_folder(monkeypatch):
    g, res, facts = synth()
    ln = lens.collect(g, res, facts)
    data = dict(lens.load_data(), inline_bytes=len(ln.chunks["arch"]) + 1)
    monkeypatch.setattr(lens, "load_data", lambda: data)
    pg = viz.make_page(g, res, lens=ln)
    assert pg.pack.head["meta"]["lens"]["left"] == ["health", "dup.0"]


def test_lenses_data_has_the_documented_keys():
    assert {"dup", "norm", "arch", "health", "inline_bytes", "chunk_rows"} <= set(DATA)
    assert DATA["dup"]["min_group"] >= 2 and DATA["dup"]["store_min"] <= min(DATA["dup"]["min_size"].values())


# ---------------------------------------------------------------- the viewer's lens logic under node
def node_run(tmp_path, js, *args):
    (tmp_path / "t.js").write_text(js)
    r = subprocess.run(
        [NODE, str(tmp_path / "t.js"), str(VIEWER / "viewer.js"), *args], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


LOGIC_JS = r"""
const fs = require("fs");
const V = require(process.argv[2]);
const dir = process.argv[3];
const blobU8 = new Uint8Array(fs.readFileSync(dir + "/lens.blob"));
const arch = JSON.parse(fs.readFileSync(dir + "/arch.json"));
const dup = JSON.parse(fs.readFileSync(dir + "/dup.json"));
const health = JSON.parse(fs.readFileSync(dir + "/health.json"));
(async () => {
  const out = {};
  const blob = V.parseLensBlob(blobU8);
  out.chunks = [...blob.chunks.keys()].sort();
  out.infoState = blob.info.state;
  const L = V.lensStore(blob, null);
  out.where = ["arch", "dup.0", "nope"].map((n) => V.lensWhere(L, n));
  const a = await V.lensChunk(L, "arch");
  out.archEq = JSON.stringify(a) === JSON.stringify(arch);
  out.again = (await V.lensChunk(L, "arch")) === a;  /* cached */
  out.chunksOf = [V.lensChunks(L, "dup"), V.lensChunks(L, "arch"), V.lensChunks(L, "unused")];
  /* a store without a blob: the info says there are chunks in the folder */
  const L2 = V.lensStore(null, { state: "on", dup: { chunks: ["dup.0"] } });
  out.where2 = ["arch", "dup.0"].map((n) => V.lensWhere(L2, n));
  const gz = new Uint8Array(fs.readFileSync(dir + "/lens-arch.json.gz"));
  const man = new Uint8Array(fs.readFileSync(dir + "/lens-manifest.json.gz"));
  const file = (name, u8) => ({ name, arrayBuffer: async () => u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength) });
  out.picked = await V.lensPick(L2, [file("lens-manifest.json.gz", man), file("lens-arch.json.gz", gz)]);
  out.where3 = ["arch", "dup.0", "zzz"].map((n) => V.lensWhere(L2, n));
  out.archFromFolder = JSON.stringify(await V.lensChunk(L2, "arch")) === JSON.stringify(arch);
  out.gates = [V.lensGate(null, "dup").ok, V.lensGate(null, "unused").ok, V.lensGate({ info: { state: "off", reason: "R" } }, "arch").why,
    V.lensGate({ info: { state: "on", dup: { state: "off", reason: "no facts" } } }, "dup").why, V.lensGate({ info: { state: "on", dup: { state: "off" } } }, "health").ok];
  /* architecture */
  const lay = V.archLayout(arch, 700), lay2 = V.archLayout(arch, 200);
  out.layout = { cols: lay.cols, cols2: lay2.cols, w: lay.width, n: lay.boxes.length, layers: lay.bands.map((b) => b.layer), overlap: false };
  const rects = lay.boxes.filter(Boolean);
  for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++) { const p = rects[i], q = rects[j]; if (p.x < q.x + q.w && q.x < p.x + p.w && p.y < q.y + q.h && q.y < p.y + p.h) out.layout.overlap = true; }
  out.cycleFirst = lay.boxes.filter((b) => arch.mods[b.i].c >= 0).map((b) => arch.mods[b.i].n);
  out.below = lay.boxes[arch.mods.findIndex((m) => m.n === "pkg/core")].y > lay.boxes[arch.mods.findIndex((m) => m.n === "app")].y;
  const many = { mods: arch.mods.map((m) => ({ ...m, c: -1 })), edges: Array.from({ length: 2000 }, (_, j) => [j % 3, (j + 1) % 3, j + 1, 0]) };
  out.edgeAll = V.archEdgeSet(arch, -1).length === arch.edges.length;
  const capped = V.archEdgeSet(many, -1, 100);
  out.edgeCap = [capped.length, capped.includes(1998), capped.slice(0, 1)[0] >= 0];
  const cyc = { mods: arch.mods.map((m, i) => ({ ...m, c: i === 1 || i === 2 ? 0 : -1 })), edges: [[1, 2, 1, 0], [2, 1, 1, 0], [0, 3, 99, 0], [0, 4, 98, 0]] };
  out.cycKept = [V.archEdgeSet(cyc, -1, 3), V.archEdgeSet(cyc, 3, 2)];  /* the edges of a cycle and of the selected module are never dropped */
  const nb = V.archNeighbours(arch, arch.mods.findIndex((m) => m.n === "pkg/core"));
  out.nb = [nb.uses.map((x) => arch.mods[x.mod].n), nb.usedBy.map((x) => arch.mods[x.mod].n)];
  out.ramp = [V.rampColor([[0, "a", "i"], [0.5, "b", "j"], [1, "c", "k"]], 0)[0], V.rampColor([[0, "a", "i"], [0.5, "b", "j"], [1, "c", "k"]], 0.3)[0], V.rampColor([[0, "a", "i"], [0.5, "b", "j"]], 0.9)[0], V.rampColor([], 0.5)[0]];
  /* duplicates */
  const gs = V.dupGroups(dup);
  const m0 = gs[0].m;
  out.dup = { n: gs.length, file0: gs[0].files[m0[0][0]], all: V.dupFilter(gs, {}).length, big: V.dupFilter(gs, { minSize: 10 ** 6 }).length,
    q: V.dupFilter(gs, { q: "PAD" }).map((g) => g.m.length), q2: V.dupFilter(gs, { q: "zzz" }).length, tests: V.dupFilter(gs, { hideTests: true }).map((g) => g.m.length),
    unused: V.dupFilter(gs, { onlyUnused: true }).length };
  /* health */
  const byS = V.healthSort(health.rows, "s", 1).map((r) => r.n), byN = V.healthSort(health.rows, "n", 1).map((r) => r.n), desc = V.healthSort(health.rows, "k", -1).map((r) => r.n);
  out.health = { byS, byN, desc, ungradedLast: V.healthSort(health.rows, "s", -1).map((r) => r.g === "") };
  const core = health.rows.find((r) => r.n === "pkg/core");
  const parts = V.healthParts(core, health);
  out.parts = parts.map((p) => p.name);
  out.lost = Math.round(parts.reduce((s, p) => s + p.points, 0) * 10) / 10;
  out.score = core.s;
  out.prefix = [V.modPrefix("."), V.modPrefix("pkg/core")];
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not NODE, reason="node is not installed: the lens logic is checked there, not in a browser")
def test_viewer_js_lens_logic(tmp_path):
    g, res, facts = synth()
    ln = lens.collect(g, res, facts)
    (tmp_path / "lens.blob").write_bytes(lens.blob(ln))
    for k, name in (("arch", "arch.json"), ("health", "health.json"), ("dup.0", "dup.json")):
        (tmp_path / name).write_bytes(gzip.decompress(ln.chunks[k]))
    (tmp_path / "lens-arch.json.gz").write_bytes(ln.chunks["arch"])
    (tmp_path / "lens-manifest.json.gz").write_bytes(lens.manifest(ln))
    o = node_run(tmp_path, LOGIC_JS, str(tmp_path))
    assert o["chunks"] == ["arch", "dup.0", "health"] and o["infoState"] == "on"
    assert o["where"] == ["inline", "inline", "pick"]  # a chunk the blob lacks is in the folder of the run
    assert o["archEq"] and o["again"] and o["chunksOf"] == [["dup.0"], ["arch"], []]
    assert (
        o["where2"] == ["pick", "pick"]
        and o["picked"] == 2
        and o["where3"] == ["picked", "pick", "absent"]
        and o["archFromFolder"]
    )
    assert o["gates"] == [False, True, "R", "no facts", True]
    ly = o["layout"]
    assert ly["n"] == 5 and ly["cols"] >= 3 and ly["cols2"] == 1 and ly["layers"] == [0, 1] and not ly["overlap"]
    assert sorted(o["cycleFirst"]) == ["pkg/core", "pkg/util"] and o["below"]
    assert o["edgeAll"] and o["edgeCap"] == [100, True, True]
    assert o["cycKept"] == [[0, 1, 2], [0, 1, 2]]
    assert o["nb"] == [["pkg/util"], ["app", "pkg/util"]]
    assert o["ramp"] == ["a", "b", "b", "#bdbdbd"]
    assert o["dup"] == {
        "n": 1,
        "file0": "pkg/core/a.py",
        "all": 1,
        "big": 0,
        "q": [1],
        "q2": 0,
        "tests": [2],
        "unused": 0,
    }
    hs = o["health"]
    assert hs["byN"] == sorted(hs["byN"]) and set(hs["byS"]) == set(hs["byN"]) == set(hs["desc"])
    assert hs["ungradedLast"] == sorted(hs["ungradedLast"])
    assert sorted(o["parts"]) == sorted(DATA["health"]["weights"]) and abs(100 - o["lost"] - o["score"]) < 0.3
    assert o["prefix"] == ["", "pkg/core/"]



# ---------------------------------------------------------------- the page: stub DOM boot and headless chromium
STUB_LENS_JS = r"""
const fs = require("fs");
const [jsPath, pagePath] = process.argv.slice(2);
const page = fs.readFileSync(pagePath, "utf8");
const grab = (id) => { const m = new RegExp('id="' + id + '"[^>]*>([^<]*)<').exec(page); return m && m[1]; };
const d = /id="dcdata" data-enc="(\w+)">([^<]*)</.exec(page);
const ctx = new Proxy({}, { get: (t, k) => (k in t ? t[k] : () => ({})), set: (t, k, v) => { t[k] = v; return true; } });
class El {
  constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.style = {}; this.dataset = {}; this.className = ""; this._t = ""; this.hidden = false; this.value = ""; this.checked = false; this.files = []; this.attrs = {}; this.disabled = false; this.title = ""; this.classList = { toggle() {}, contains() { return false; } }; }
  get textContent() { return this._t; }
  set textContent(v) { this._t = v; this.children = []; }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = v; } getAttribute(k) { return this.attrs[k]; }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 900, height: 600 }; }
  getContext() { return ctx; }
  setPointerCapture() {} addEventListener() {} focus() {} blur() {} click() {}
}
const all = {};
const byId = (id) => (all[id] = all[id] || new El("div"));
all.err = Object.assign(new El("div"), { hidden: true });
all.dcdata = Object.assign(new El("script"), { _t: d[2], dataset: { enc: d[1] } });
if (grab("dclens")) all.dclens = Object.assign(new El("script"), { _t: grab("dclens") });
const handlers = {};
globalThis.document = { getElementById: byId, createElement: (t) => new El(t), createElementNS: (ns, t) => new El(t), createTextNode: (t) => ({ _t: t }), createDocumentFragment: () => new El("frag"), documentElement: new El("html"), addEventListener: (k, f) => { handlers[k] = f; }, activeElement: null, title: "" };
globalThis.document.body = new El("body");
globalThis.Path2D = class { moveTo() {} lineTo() {} quadraticCurveTo() {} bezierCurveTo() {} };
globalThis.window = { addEventListener() {}, devicePixelRatio: 1 };
globalThis.location = { hash: process.argv[4] || "" };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "#123456" });
globalThis.requestAnimationFrame = (f) => f();
globalThis.matchMedia = () => ({ matches: false });
Object.defineProperty(globalThis, "navigator", { value: { clipboard: { writeText: () => Promise.resolve() } }, configurable: true });
const text = (e) => (e._t || "") + " " + (e.children || []).map(text).join(" ");
const count = (e, pred) => (pred(e) ? 1 : 0) + (e.children || []).reduce((s, c) => s + count(c, pred), 0);
require(jsPath);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  await sleep(300);
  const out = { err: all.err.hidden === false && all.err._t, on: ["unused", "dup", "arch", "health"].map((n) => [!all["l-" + n].disabled, all["l-" + n].dataset.on]) };
  for (const n of ["dup", "arch", "health", "unused"]) {
    all["l-" + n].onclick();
    await sleep(120);
    out[n] = { lens: document.documentElement.dataset.lens, ready: document.documentElement.dataset.lensReady, stageHidden: byId("stage").hidden, lenspHidden: byId("lensp").hidden,
      bar: text(byId("lbar")).slice(0, 80), body: n === "dup" ? count(byId("lbody"), (e) => e.className === "drow") : n === "arch" ? count(byId("lbody"), (e) => e.tagName === "G" && String(e.attrs.class || "").startsWith("amod")) : count(byId("lbody"), (e) => e.tagName === "TR"), msg: byId("lmsg")._t };
  }
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def lens_page(tmp_path, with_lens=True):
    g, res, facts = synth()
    ln = (
        lens.collect(g, res, facts)
        if with_lens
        else lens.off("no lens data in this run")
    )
    pg = viz.make_page(g, res, lens=ln, meta={"scope": "repo"})
    (tmp_path / "page.html").write_text(pg.html)
    return pg


@pytest.mark.skipif(
    not NODE, reason="node is not installed: the page code is run against a stub DOM there, not in a browser"
)
def test_viewer_boots_with_the_lens_switcher_and_each_lens_fills_its_panel(tmp_path):
    lens_page(tmp_path)
    (tmp_path / "stub.js").write_text(STUB_LENS_JS)
    r = subprocess.run(
        [NODE, str(tmp_path / "stub.js"), str(VIEWER / "viewer.js"), str(tmp_path / "page.html")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["err"] in ("", False, None), o
    assert o["on"] == [[True, "1"], [True, "0"], [True, "0"], [True, "0"]]
    assert (
        o["dup"]["lens"] == "dup"
        and o["dup"]["stageHidden"] is True
        and o["dup"]["lenspHidden"] is False
        and o["dup"]["body"] == 2
        and o["dup"]["ready"] == "dup"
    )
    assert o["arch"]["body"] == 5 and "5 module(s)" in o["arch"]["bar"]
    assert o["health"]["body"] == 6  # the header row and five modules
    assert (
        o["unused"]["lens"] == "unused" and o["unused"]["stageHidden"] is False and o["unused"]["lenspHidden"] is True
    )


@pytest.mark.skipif(
    not NODE, reason="node is not installed: the page code is run against a stub DOM there, not in a browser"
)
def test_viewer_without_lens_data_disables_the_three_lenses_with_the_reason(tmp_path):
    lens_page(tmp_path, with_lens=False)
    (tmp_path / "stub.js").write_text(STUB_LENS_JS)
    r = subprocess.run(
        [NODE, str(tmp_path / "stub.js"), str(VIEWER / "viewer.js"), str(tmp_path / "page.html")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["err"] in ("", False, None) and o["on"] == [[True, "1"], [False, "0"], [False, "0"], [False, "0"]]
    assert o["dup"]["lens"] == "unused"  # a click on a disabled lens does nothing


