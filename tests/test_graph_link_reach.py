"""graph.link and graph.reach: resolution of Facts into the graph, tiers, roots, and the verdicts.

python3 -m pytest -q tests/test_graph_link_reach.py

Facts are built by hand (any language) or with the toy extractor (dcref, tests/graph_support.py). Nothing here needs a
tree-sitter grammar or a language pack.
"""

import heapq
import random
from collections import defaultdict

import pytest

from dead_code_checker.graph import contracts, link as dlink, model as md, reach as rc
from dead_code_checker.graph.model import DynSite, Facts, Import, Ref, RootHint


# ---------------------------------------------------------------- helpers
def d(qual, kind=None, line=1, flags=0, arity=-1, vis="public"):
    kind = kind or ("method" if "." in qual else "class" if qual[:1].isupper() else "func")
    return md.Def(qual, kind, line, line, vis, flags, arity)


def facts_of(lang="python", defs=(), refs=(), imports=(), roots=(), dyn=()):
    return Facts(lang, tuple(defs), tuple(refs), tuple(imports), tuple(roots), tuple(dyn))


def short(k):
    return k.split(":", 1)[1]


def edges(res):
    return {(short(e.src), short(e.dst), e.kind, e.tier) for e in res.graph.edges}


def has(res, src, dst, kind=None, tier=None):
    return any(s == src and t == dst and (kind is None or k == kind) and (tier is None or tr == tier)
               for s, t, k, tr in edges(res))  # fmt: skip


def node_of(res, name):
    return next(n for n in res.graph.nodes if short(n.key) == name)


def run(files, hints=None, **kw):
    kw.setdefault("workers", 1)
    return dlink.link(files, hints, **kw)


# ================================================================ link: scope, imports
def test_scope_chain_self_members_and_python_class_scope():
    res = run({"a.py": facts_of(defs=[d("main"), d("helper"), d("main.inner"), d("Cls"), d("Cls.run"), d("Cls.aux")],
                         refs=[Ref("helper", "call", 3, "main"), Ref("inner", "call", 4, "main"),
                               Ref("self.aux", "call", 5, "Cls.run"), Ref("aux", "call", 6, "Cls.run")])})  # fmt: skip
    assert has(res, "a.py#main", "a.py#helper", "call", "T")
    assert has(res, "a.py#main", "a.py#main.inner", "call", "T")
    assert has(res, "a.py#Cls.run", "a.py#Cls.aux", "call", "T")
    # python: a bare name in a method does not see the class body, so the bare `aux` adds nothing
    assert sum(1 for e in res.graph.edges if short(e.src) == "a.py#Cls.run" and short(e.dst) == "a.py#Cls.aux") == 1


def test_imports_bind_names_and_externals_add_nothing():
    files = {
        "pkg/__init__.py": facts_of(defs=[d("init_fn")]),
        "pkg/util.py": facts_of(defs=[d("tool"), d("Helper"), d("Helper.go")]),
        "pkg/core.py": facts_of(
            defs=[d("run")],
            imports=[
                Import("pkg.util", ("tool", "Helper as H"), "", 1),
                Import("os", (), "", 2),
                Import("pkg", (), "", 3),
                Import("", ("util",), "", 4, 1),
            ],  # fmt: skip
            refs=[
                Ref(n, "call", 9, "run")
                for n in ("tool", "H.go", "os.path.join", "pkg.util.tool", "util.tool", "pkg.init_fn")
            ],
        ),
    }
    res = run(files)
    for dst in ("pkg/util.py#tool", "pkg/util.py#Helper", "pkg/util.py#Helper.go", "pkg/__init__.py#init_fn"):
        assert has(res, "pkg/core.py#run", dst, "call", "T"), dst
    # os.path.join: `os` is an external import, there is no N guess for `join`
    assert not any(e[3] == "N" and e[0] == "pkg/core.py#run" for e in edges(res))
    assert has(res, "pkg/core.py", "pkg/util.py", "import", "T") and has(
        res, "pkg/core.py", "pkg/__init__.py", "import", "T"
    )
    assert has(res, "pkg/core.py", "pkg/util.py#tool", "import", "N")  # a named import: the weak edge of a re-export


def test_star_import_and_relative_levels():
    res = run({"lib/a.py": facts_of(defs=[d("only")]), "lib/sub/b.py": facts_of(defs=[d("deep")]),
               "lib/sub/c.py": facts_of(defs=[d("use")], imports=[Import("a", ("only",), "", 1, 2), Import("b", ("*",), "", 2, 1)],
                                 refs=[Ref("only", "call", 5, "use"), Ref("deep", "call", 6, "use")])})  # fmt: skip
    assert has(res, "lib/sub/c.py#use", "lib/a.py#only", "call", "T")
    assert has(res, "lib/sub/c.py#use", "lib/sub/b.py#deep", "call", "T")


def test_a_member_through_a_star_import_stays_a_guess_outside_the_package_languages():
    """`from m import *` then `C.f()`: C and C.f are two targets, which is a guess (N) as before the JVM unit; the same wildcard
    import of Java is a resolution of one class (T): that rule is for the languages whose package is its directory."""

    def files(lang, mod, ext):
        defs = [d("C"), d("C.f")]
        return {
            f"lib/{mod}.{ext}": facts_of(lang, defs=defs),
            f"app/use.{ext}": facts_of(
                lang,
                defs=[d("use")],
                imports=[Import(mod, ("*",), "", 1, 0)],
                refs=[Ref("C.f", "call", 5, "use")],
            ),
        }

    py = run(files("python", "m", "py"))
    assert has(py, "app/use.py#use", "lib/m.py#C.f", "call", "N")
    assert has(py, "app/use.py#use", "lib/m.py#C", "call", "N")
    assert not has(py, "app/use.py#use", "lib/m.py#C.f", "call", "T")
    jv = run(files("java", "p", "java"))
    assert has(jv, "app/use.java#use", "lib/p.java#C.f", "call", "T")


def test_name_match_is_n_and_amb_marks_wide_matches():
    files = {f"m{i}.py": facts_of(defs=[d(f"C{i}"), d(f"C{i}.go")]) for i in range(10)}
    files["x.py"] = facts_of(defs=[d("f")], refs=[Ref("obj.go", "call", 2, "f"), Ref("frob", "call", 3, "f")])
    files["y.py"] = facts_of(defs=[d("frob")])
    res = run(files)
    assert sum(1 for e in edges(res) if e[0] == "x.py#f" and e[1].endswith(".go") and e[3] == "N") == 10
    assert len(res.amb) == 10 and all(k.endswith(".go") for k in res.amb)
    assert has(res, "x.py#f", "y.py#frob", "call", "N")


def test_language_groups_do_not_mix_but_exec_crosses():
    res = run({"a.sh": facts_of("shell", defs=[d("deploy")], refs=[Ref("bin/tool.py", "exec", 3, "deploy"), Ref("nothere", "exec", 4)]),
               "bin/tool.py": facts_of(defs=[d("main")]), "b.py": facts_of(defs=[d("deploy")], refs=[Ref("deploy", "call", 1)])})  # fmt: skip
    assert has(res, "a.sh#deploy", "bin/tool.py", "exec", "T")
    assert not has(res, "b.py", "a.sh#deploy") and has(res, "b.py", "b.py#deploy", "call", "T")
    assert not any(e[2] == "exec" and "nothere" in e[1] for e in edges(res))


def test_shell_include_closure_makes_defs_visible_and_cfg_reads():
    res = run({"run.sh": facts_of("shell", defs=[d("go")], imports=[Import("lib/common.sh", (), "", 1)], refs=[Ref("log", "call", 5, "go")]),
               "lib/common.sh": facts_of("shell", defs=[d("log")]),
               "cfg.yaml": facts_of("yaml", defs=[d("TOKEN", "cfgkey")]),
               "r.py": facts_of(defs=[d("f")], refs=[Ref("TOKEN", "reads", 2, "f")])})  # fmt: skip
    assert has(res, "run.sh#go", "lib/common.sh#log", "call", "T")
    assert has(res, "r.py#f", "cfg.yaml#TOKEN", "reads", "N")


# ================================================================ link: classes
def test_inheritance_override_impl_external_base_protocol():
    res = run(
        {
            "m.py": facts_of(
                defs=[
                    d("Base"),
                    d("Base.run", flags=md.F_ABSTRACT),
                    d("Base.stop"),
                    d("Child"),
                    d("Child.run"),
                    d("Ext"),
                    d("Ext.m"),
                    d("Plain"),
                    d("Plain.m"),
                    d("Plain.__str__"),
                ],  # fmt: skip
                refs=[
                    Ref("Base", "inherit", 5, "Child"),
                    Ref("other.Thing", "inherit", 6, "Ext"),
                    Ref("object", "inherit", 7, "Plain"),
                ],
            )
        }
    )
    assert has(res, "m.py#Child", "m.py#Base", "inherit", "T")
    assert has(res, "m.py#Base.run", "m.py#Child.run", "override", "T")
    assert has(res, "m.py#Child.run", "m.py#Base.run", "impl", "T")  # abstract parent: the implementation uses it
    assert not has(res, "m.py#Base.stop", "m.py#Child.run")
    assert node_of(res, "m.py#Child.run").flags & md.F_OVERRIDE
    assert has(res, "m.py#Ext", "m.py#Ext.m", "override", "N")  # base that resolves to nothing: members may override it
    assert not has(res, "m.py#Plain", "m.py#Plain.m")  # `object` is a builtin base
    assert (
        has(res, "m.py#Plain", "m.py#Plain.__str__", "ref", "T")
        and node_of(res, "m.py#Plain.__str__").flags & md.F_PROTOCOL
    )


def test_members_through_the_chain_across_files():
    res = run({"b.py": facts_of(defs=[d("Base"), d("Base.helper")]),
               "c.py": facts_of(defs=[d("Kid"), d("Kid.go")], imports=[Import("b", ("Base",), "", 1)],
                         refs=[Ref("Base", "inherit", 3, "Kid"), Ref("self.helper", "call", 4, "Kid.go")])})  # fmt: skip
    assert has(res, "c.py#Kid.go", "b.py#Base.helper", "call", "T")
    assert has(res, "c.py#Kid", "b.py#Base", "inherit", "T")


def test_overloads_get_distinct_keys_and_arity():
    res = run({"j.java": facts_of("java", defs=[d("A"), d("A.f", arity=1), d("A.f", arity=2), d("A.g", arity=0), d("A.g", arity=0)],
                           refs=[Ref("f", "call", 9, "A.g")])})  # fmt: skip
    keys = [n.key for n in res.graph.nodes]
    assert "java:j.java#A.f/1" in keys and "java:j.java#A.f/2" in keys
    gs = [k for k in keys if "#A.g/0" in k]
    assert len(gs) == 2 and all("~" in k for k in gs)  # equal qual+arity: h8 tells them apart
    assert has(res, "j.java#A.g/0~" + gs[0].rsplit("~", 1)[1], "j.java#A.f/1", "call", "T")


# ================================================================ link: dynamic sites, roots, flags
def test_dyn_sites_are_d_edges_and_flag_the_targets():
    files = {"d.py": facts_of(defs=[d("Cls"), d("Cls.a"), d("Cls.b"), d("other")],
                       dyn=[DynSite("getattr", 5, "Cls.a"), DynSite("file", 6, "other"), DynSite("import:app.plugins.p1", 7, "other")]),
             "app/loader.py": facts_of(defs=[d("load")], dyn=[DynSite("pkg:app/plugins", 3, "load"), DynSite("name:P*", 4, "load")]),
             "app/plugins/p1.py": facts_of(defs=[d("P1"), d("P1.x")]), "app/plugins/p2.py": facts_of(defs=[d("P2")])}  # fmt: skip
    res = run(files)
    assert has(res, "d.py#Cls.a", "d.py#Cls.b", "ref", "D") and has(res, "d.py#Cls.a", "d.py#Cls.a", "ref", "D")
    assert has(res, "d.py#other", "d.py#Cls.b", "ref", "D") and not has(res, "d.py#Cls.a", "d.py#other", "ref", "D")
    assert has(res, "d.py#other", "app/plugins/p1.py", "import", "D")
    assert has(res, "app/loader.py#load", "app/plugins/p1.py#P1", "ref", "D") and has(
        res, "app/loader.py#load", "app/plugins/p2.py#P2", "ref", "D"
    )
    assert not has(
        res, "app/loader.py#load", "app/plugins/p1.py#P1.x", "ref", "D"
    )  # pkg: wants classes, name:P* matches last names
    assert node_of(res, "d.py#Cls.b").flags & md.F_DYNAMIC and node_of(res, "app/plugins/p2.py#P2").flags & md.F_DYNAMIC
    assert not node_of(res, "d.py#Cls").flags & md.F_DYNAMIC  # the class itself is no target of the getattr wildcard


def test_roots_resolve_to_nodes_and_dangling_roots_are_e_findings():
    files = {"bin/x": facts_of("shell", defs=[d("main")], roots=[RootHint("main", "main", 3), RootHint("cron", "ghost", 4)]),
             "lib/m.py": facts_of(defs=[d("f")])}  # fmt: skip
    hints = {"config.toml": [RootHint("cron", "file:bin/x", 12), RootHint("cron", "cmd:x", 13), RootHint("cron", "file:bin/none", 14),
                               RootHint("export", "node:python:lib/m.py#f", 15, "soft", "convention"),
                               RootHint("cron", "node:python:lib/m.py#zzz", 16)]}  # fmt: skip
    res = run(files, hints)
    got = {(r.kind, short(r.node) if r.node else r.selector, r.origin, r.strength) for r in res.graph.roots}
    assert ("cron", "bin/x", "config.toml:12", "hard") in got and ("cron", "bin/x", "config.toml:13", "hard") in got
    assert ("main", "bin/x#main", "bin/x:3", "hard") in got and (
        "main",
        "bin/x",
        "bin/x:3",
        "hard",
    ) in got  # def root also roots the file
    assert ("export", "lib/m.py#f", "config.toml:15", "soft") in got
    dangling = {(r.kind, r.selector) for r in res.graph.roots if not r.node}
    assert dangling == {
        ("cron", "node:shell:bin/x#ghost"),
        ("cron", "file:bin/none"),
        ("cron", "node:python:lib/m.py#zzz"),
    }
    es = [f for f in res.findings if f.code == "dc.root-dangling"]
    assert len(es) == 3 and all(f.level == "E" and f.why for f in es)
    assert node_of(res, "bin/x#main").flags & md.F_ENTRY


def test_a_symbol_named_like_a_selector_scheme_is_a_name_not_a_selector():
    """A regression: `@pytest.fixture def node(...)` gave E dc.root-dangling, the target `node` was read as
    the selector scheme `node:`. A scheme needs its colon, and `node::x` is a language path."""
    fx = [RootHint("annotation:pytest.fixture", t, i) for i, t in enumerate(("node", "file", "cmd", "class"), 1)]
    files = {"tests/t.py": facts_of(defs=[d("node"), d("file"), d("cmd"), d("klass")], roots=fx)}
    res = run(files)
    assert [f.msg for f in res.findings if f.code == "dc.root-dangling"] == [
        "annotation:pytest.fixture root 'class' resolves to no node"
    ]  # only the target with no def of that name
    for name in ("node", "file", "cmd"):
        assert node_of(res, f"tests/t.py#{name}").flags & md.F_ENTRY
    assert [
        md.selector_scheme(t) for t in ("node", "node:", "node:python:a#f", "node::x", "file:bin/x", "cmd:x", "a:b")
    ] == [
        "",
        "node",
        "node",
        "",
        "file",
        "cmd",
        "",
    ]


def test_bad_facts_are_findings_not_exceptions():
    res = run({"a.py": facts_of(defs=[d("ok"), md.Def("bad", "bogus", 2, 2)], refs=[Ref("ok", "jump", 3, "ok")],
                         roots=[RootHint("nokind", "ok", 4)])})  # fmt: skip
    assert [f.code for f in res.findings] == ["dc.bad-facts"] * 3 and all(f.level == "E" for f in res.findings)
    assert [short(n.key) for n in res.graph.nodes] == ["a.py", "a.py#ok"]


def test_path_flags_come_from_the_path():
    res = run({"tests/test_a.py": facts_of(defs=[d("test_x")]), "vendor/lib.py": facts_of(defs=[d("v")]), "src/a_pb2.py": facts_of(defs=[d("g")]),
               "src/real.py": facts_of(defs=[d("r")])})  # fmt: skip
    fl = {short(n.key): n.flags for n in res.graph.nodes}
    assert (
        fl["tests/test_a.py#test_x"] & md.F_TEST
        and fl["vendor/lib.py#v"] & md.F_GENERATED
        and fl["src/a_pb2.py#g"] & md.F_GENERATED
    )
    assert fl["src/real.py#r"] == 0 and fl["tests/test_a.py"] & md.F_TEST


# ================================================================ link: determinism and chunking
def _project(seed, nfiles=24):
    rng = random.Random(seed)
    files = {}
    names = [f"m{i}" for i in range(nfiles)]
    for i, name in enumerate(names):
        lines = []
        for j in range(rng.randint(2, 6)):
            lines.append(f"def f{i}_{j}")
            for _ in range(rng.randint(0, 3)):
                k = rng.randrange(nfiles)
                lines.append(
                    rng.choice([f"ref f{k}_{rng.randrange(4)}", f"ref f{k}_{rng.randrange(4)}", f"import m{k} f{k}_0"])
                )
        if i % 7 == 0:
            lines.append(f"root main f{i}_0")
        files[f"src/{name}.dcref"] = contracts.get_extractor("dcref").extract("dcref", "x", "\n".join(lines))
    return files


def test_graph_is_byte_identical_for_1_2_and_8_workers_and_any_chunking(dcref):
    files = _project(7)
    digests, reach = set(), set()
    for w, mc in ((1, 2000), (2, 10), (8, 10), (8, 1), (4, 30)):
        res = dlink.link(files, {"cfg.dcroots": [RootHint("cron", "file:src/m3.dcref", 1)]}, workers=w, min_chunk=mc)
        digests.add(res.graph.digest())
        reach.add(rc.analyze(res.graph).digest())
    assert len(digests) == 1 and len(reach) == 1
    assert (
        dlink._chunks([(p, f) for p, f in sorted(files.items())], 8, 10).__len__() > 1
    )  # the test does exercise several chunks
    assert len(res.graph.edges) > 50


def test_input_order_does_not_change_the_graph(dcref):
    files = _project(11, 12)
    a = dlink.link(files, workers=1)
    b = dlink.link(dict(reversed(list(files.items()))), workers=1)
    assert a.graph.digest() == b.graph.digest() and a.amb == b.amb


def test_two_copies_of_one_content_link_alike_per_path(dcref):
    text = "def a\nref b\ndef b\n"
    ex = contracts.get_extractor("dcref")
    f = ex.extract("dcref", "x", text)
    r1, r2 = run({"x/one.dcref": f, "y/two.dcref": f}), run({"x/one.dcref": f})
    assert {e for e in edges(r2)} <= {e for e in edges(r1)} and len(r1.graph.nodes) == 2 * len(r2.graph.nodes)


# ================================================================ link -> reach on text and on code
def _dcref(**files):
    ex = contracts.get_extractor("dcref")
    return {p.replace("__", "/").replace("_dc", ".dcref"): ex.extract("dcref", p, t) for p, t in files.items()}


def test_dead_chain_is_a_cluster_and_the_text_grep_would_have_called_it_alive(dcref):
    files = _dcref(app_dc="def main\nref used\nroot main main\ndef used\ndef a\nref b\ndef b\nref c\ndef c\ndef lone\n")
    res = rc.analyze(run(files).graph)
    v = {short(k): x for k, x in res.verdicts().items()}
    assert v["app.dcref#main"] == v["app.dcref#used"] == "alive"
    assert [v["app.dcref#" + n] for n in "abc"] == ["dead-cluster"] * 3 and v["app.dcref#lone"] == "dead"
    assert [tuple(map(short, c)) for c in res.clusters] == [("app.dcref#a", "app.dcref#b", "app.dcref#c")]
    why = res.why_dead([k for k in res.prep.keys if k.endswith("#b")][0])
    assert why.incoming[0][0].endswith("#a") and "unreachable" in why.incoming[0][4] and len(why.cluster) == 3
    assert "no incoming edge" in res.why_dead([k for k in res.prep.keys if k.endswith("#lone")][0]).render()


def test_recursion_alone_does_not_keep_a_symbol_alive(dcref):
    files = _dcref(r_dc="def main\nroot main main\ndef spin\nref spin\n")
    res = rc.analyze(run(files).graph)
    k = [k for k in res.prep.keys if k.endswith("#spin")][0]
    assert res.verdict(k) == "dead-cluster" and "self reference" in res.why_dead(k).render()


def test_d_site_keeps_the_dispatch_targets_weak_alive():
    base = dict(defs=[d("main"), d("Reg"), d("Reg.handler_a"), d("Reg.dispatch")], roots=[RootHint("main", "main", 1)],
                refs=[Ref("Reg", "call", 3, "main"), Ref("r.dispatch", "call", 4, "main")])  # fmt: skip
    with_d = rc.analyze(run({"p.py": facts_of(**base, dyn=[DynSite("getattr", 5, "Reg.dispatch")])}).graph)
    without = rc.analyze(run({"p.py": facts_of(**base)}).graph)
    ha = "python:p.py#Reg.handler_a"
    assert with_d.verdict(ha) == "weak-alive" and without.verdict(ha) == "dead"
    why = with_d.why_alive(ha)
    assert [s.tier for s in why.steps] == ["N", "D"] and why.steps[-1].kind == "ref" and why.root.kind == "main"
    assert "weak-alive" in why.render() and "python:p.py#Reg.dispatch" in why.render()


def test_override_of_a_live_parent_and_protocol_methods_are_alive():
    files = {"m.py": facts_of(defs=[d("main"), d("Base"), d("Base.run"), d("Kid"), d("Kid.run"), d("Kid.__repr__"), d("Kid.other")],
                       roots=[RootHint("main", "main", 1)],
                       refs=[Ref("Kid", "call", 2, "main"), Ref("Base", "inherit", 3, "Kid"), Ref("Base.run", "call", 4, "main")])}  # fmt: skip
    v = rc.analyze(run(files).graph).verdicts()
    assert v["python:m.py#Kid.run"] == "alive" and v["python:m.py#Kid.__repr__"] == "alive"
    assert v["python:m.py#Kid.other"] == "dead" and v["python:m.py#Base"] == "alive"


def test_roots_through_the_pipeline_script_main_imports_and_test_only():
    files = {
        "bin/x": facts_of(
            "shell", defs=[d("main")], roots=[RootHint("main", "main", 1)], imports=[Import("lib/s.sh", (), "", 2)]
        ),
        "lib/s.sh": facts_of("shell", defs=[d("helper")], refs=[Ref("only_in_tests", "call", 3, "helper")]),
        "t/test_s.py": facts_of(
            defs=[d("test_a")], roots=[RootHint("test", "test_a", 1)], refs=[Ref("helper", "exec", 2, "test_a")]
        ),  # fmt: skip
        "docs.md": facts_of("python"),
    }
    hints = {"README.md": [RootHint("doc-cmd", "cmd:s.sh", 3, "soft", "documented")]}
    res = rc.analyze(run(files, hints).graph)
    v = {short(k): x for k, x in res.verdicts().items()}
    assert v["bin/x"] == v["bin/x#main"] == "alive" and v["lib/s.sh"] == "alive"  # sourced file: executed
    assert v["lib/s.sh#helper"] == "dead"  # a sourced file does not make its functions alive
    assert v["t/test_s.py#test_a"] == "test-only" and v["docs.md"] == "dead"
    assert res.excluded("python:t/test_s.py#test_a") and not res.excluded("shell:bin/x#main")


# ================================================================ reach: random graphs against a naive traversal
KINDS = ("cron", "main", "test", "doc-cmd", "bin", "export")


def _random_graph(seed, n=40, m=90):
    rng = random.Random(seed)
    nodes = [md.node(md.node_key("python", f"f{i % 5}.py", f"n{i}"), "func", vis=rng.choice(["private", "public", "exported"]))
             for i in range(n)]  # fmt: skip
    edges_ = [
        md.edge(nodes[rng.randrange(n)].key, nodes[rng.randrange(n)].key, "call", rng.choice("STTNND"), f"x:{i}")
        for i in range(m)
    ]
    roots = [md.root(rng.choice(KINDS), f"r:{i}", node=nodes[rng.randrange(n)].key, strength=rng.choice(["hard", "hard", "soft"]))
             for i in range(rng.randint(0, 5))]  # fmt: skip
    return md.canonical(nodes, edges_, roots)


def _naive(graph, mode="closed_world"):
    adj = defaultdict(list)
    for e in graph.edges:
        adj[e.src].append((e.dst, md.TIERS[e.tier]))
    grp = defaultdict(list)
    where = {n.key: n for n in graph.nodes}
    for r in graph.roots:
        c = "test" if r.kind == "test" else "doc" if r.kind == "doc-cmd" else "soft" if r.strength == "soft" else "hard"
        hit = [r.node]
        if c == "test" and r.node in where and where[r.node].kind == "file":  # a test file seeds all of its symbols
            hit = [n.key for n in graph.nodes if n.path == where[r.node].path]
        grp[c] += hit
    claimed, dist = {}, {}

    def dij(code, seeds, strong, extra=()):
        h, done = [], {}
        for k, dd in list(seeds) + [(v, du + w) for u, du, w, v in extra]:
            if k not in claimed:
                heapq.heappush(h, (dd, k))
        while h:
            dd, v = heapq.heappop(h)
            if v in done or v in claimed:
                continue
            done[v] = dd
            for u, w in adj[v]:
                if u not in claimed and u not in done and (not strong or w <= 2):
                    heapq.heappush(h, (dd + w, u))
        for k, dd in done.items():
            claimed[k], dist[k] = code, dd

    dij("alive", [(k, 0) for k in grp["hard"]], True)
    extra = [(u, dist[u], w, v) for u in sorted(claimed) for v, w in adj[u] if v not in claimed]
    dij("weak-alive", [(k, 0) for k in grp["soft"]], False, extra)
    dij("test-only", [(k, 0) for k in grp["test"]], False)
    dij("doc-only", [(k, 0) for k in grp["doc"]], False)
    dead = {n.key for n in graph.nodes if n.key not in claimed}
    und = defaultdict(set)
    for e in graph.edges:
        if e.src in dead and e.dst in dead:
            und[e.src].add(e.dst), und[e.dst].add(e.src)
    verdict = dict(claimed)
    for k in sorted(dead):
        verdict[k] = "dead-cluster" if und[k] else "dead"
    return verdict, dist


@pytest.mark.parametrize("seed", range(60))
def test_forward_matches_a_naive_traversal_on_random_graphs(seed):
    g = _random_graph(seed)
    res = rc.analyze(g)
    want, dist = _naive(g)
    assert res.verdicts() == want
    for k, i in res.prep.idx.items():
        assert (res.dist[i] if res.state[i] else None) == dist.get(k)
    # every par[] chain is a real path of exactly that cost that ends in a root of the node's class
    for k in want:
        w = res.why_alive(k)
        if want[k] in ("dead", "dead-cluster"):
            assert w is None
            continue
        cost = sum(md.TIERS[s.tier] for s in w.steps)
        assert cost == dist[k] and (w.steps[-1].dst if w.steps else w.root.node) == k
        assert all(a.dst == b.src for a, b in zip(w.steps, w.steps[1:])) and (
            not w.steps or w.steps[0].src == w.root.node
        )
        if want[k] == "alive":
            assert all(s.tier in "ST" for s in w.steps)


@pytest.mark.parametrize("seed", range(40))
def test_bounded_reverse_search_agrees_with_the_forward_pass(seed):
    g = _random_graph(seed + 1000)
    prep = rc.prepare(g)
    res = rc.forward(prep)
    got = rc.touched(prep, [n.key for n in g.nodes])
    for k, t in got.items():
        f = res.verdict(k)
        assert (t.verdict == f) or (f in ("dead", "dead-cluster") and t.verdict in ("dead", "dead-cluster")), (k, f, t)
        if t.path:
            assert t.path[-1].dst == k and all(a.dst == b.src for a, b in zip(t.path, t.path[1:]))
            assert (t.verdict != "alive") or all(s.tier in "ST" for s in t.path)


def test_the_cheapest_path_does_not_hide_an_all_t_path_behind_an_n_edge():
    ks = [md.node_key("python", "a.py", n) for n in ("r", "t", "a", "b")]
    g = md.canonical(
        [md.node(k, "func") for k in ks],
        [
            md.edge(ks[0], ks[1], "call", "N"),
            md.edge(ks[0], ks[2], "call", "T"),
            md.edge(ks[2], ks[3], "call", "T"),
            md.edge(ks[3], ks[1], "call", "T"),
        ],  # fmt: skip
        [md.root("main", "m:1", node=ks[0])],
    )
    res = rc.analyze(g)
    assert res.verdict(ks[1]) == "alive" and res.dist[res.prep.idx[ks[1]]] == 6
    assert [s.tier for s in res.why_alive(ks[1]).steps] == ["T", "T", "T"]


def test_weak_alive_test_only_doc_only_soft_and_why_dead_reasons():
    ks = {
        n: md.node_key("python", "a.py", n)
        for n in ("r", "viaN", "t", "tonly", "d", "donly", "s", "soft", "dead", "tcall", "lone")
    }
    es = [
        md.edge(ks["r"], ks["viaN"], "call", "N"),
        md.edge(ks["t"], ks["tonly"], "call", "T"),
        md.edge(ks["d"], ks["donly"], "ref", "T"),
        md.edge(ks["s"], ks["soft"], "call", "T"),
        md.edge(ks["tcall"], ks["dead"], "call", "T"),
        md.edge(ks["r"], ks["tonly"], "call", "D"),
    ]
    rs = [md.root("main", "m:1", node=ks["r"]), md.root("test", "t:1", node=ks["t"]), md.root("doc-cmd", "d:1", node=ks["d"], strength="soft"),
          md.root("workflow", "w:1", node=ks["s"], strength="soft"), md.root("cron", selector="file:gone", origin="s.json:3")]  # fmt: skip
    res = rc.analyze(md.canonical([md.node(k, "func") for k in ks.values()], es, rs))
    v = {n: res.verdict(k) for n, k in ks.items()}
    assert v == {"r": "alive", "viaN": "weak-alive", "t": "test-only", "tonly": "weak-alive", "d": "doc-only", "donly": "doc-only",
                 "s": "weak-alive", "soft": "weak-alive", "dead": "dead-cluster", "tcall": "dead-cluster", "lone": "dead"}  # fmt: skip
    assert res.prep.dangling and "dangle" in res.why_dead(ks["dead"]).roots
    wd = res.why_dead(ks["viaN"])
    assert wd.incoming[0][2] == "N" and "weak" in wd.incoming[0][4]
    assert "unreachable" in res.why_dead(ks["dead"]).incoming[0][4]
    only_t = md.canonical(
        [md.node(ks["t"], "func"), md.node(ks["tonly"], "func")],
        [md.edge(ks["t"], ks["tonly"], "call", "T")],
        [md.root("test", "t:1", node=ks["t"])],
    )
    r2 = rc.analyze(only_t)
    assert r2.verdict(ks["tonly"]) == "test-only" and "test roots" in r2.why_dead(ks["tonly"]).incoming[0][4]
    assert r2.why_alive(ks["tonly"]).root.kind == "test"


def test_library_mode_public_unused_and_unindexed_packages():
    a, b, c = (md.node_key("python", "p/m.py", n) for n in "abc")
    g = md.canonical(
        [
            md.node(a, "func", vis="public", pkg="p"),
            md.node(b, "func", vis="private", pkg="p"),
            md.node(c, "func", vis="exported", pkg="q"),
        ]
    )
    assert {rc.analyze(g).verdict(k) for k in (a, b, c)} == {"dead"}
    lib = rc.analyze(g, "library")
    assert [lib.verdict(k) for k in (a, b, c)] == ["public-unused", "dead", "public-unused"]
    assert rc.analyze(g, "library", unindexed=["q"]).verdict(c) == "unknown"
    with pytest.raises(ValueError):
        rc.analyze(g, "nope")


def test_reverse_search_cap_gives_unknown_never_a_guess():
    ks = [md.node_key("python", "c.py", f"n{i:03d}") for i in range(120)]
    g = md.canonical([md.node(k, "func") for k in ks], [md.edge(ks[i], ks[i + 1], "call", "T") for i in range(119)],
                    [md.root("main", "m:1", node=ks[0])])  # fmt: skip
    prep = rc.prepare(g)
    assert rc.touched(prep, [ks[-1]], cap=50)[ks[-1]].verdict == "unknown"
    t = rc.touched(prep, [ks[-1], "python:nope#x"])
    assert t[ks[-1]].verdict == "alive" and len(t[ks[-1]].path) == 119
    assert t["python:nope#x"].verdict == "unknown"
    assert rc.touched(prep, [ks[5]], cap=rc.CAP)[ks[5]].steps < 50  # the search stops at the first root it meets


def test_touched_dead_chain_and_orphan():
    ks = {n: md.node_key("python", "o.py", n) for n in ("main", "orphan", "chain", "tail")}
    g = md.canonical(
        [md.node(k, "func") for k in ks.values()],
        [md.edge(ks["chain"], ks["tail"], "call", "T")],
        [md.root("main", "m:1", node=ks["main"])],
    )
    t = rc.touched(rc.prepare(g), ks.values())
    assert (t[ks["orphan"]].verdict, t[ks["chain"]].verdict, t[ks["tail"]].verdict) == ("dead", "dead", "dead-cluster")
    assert "chain" in t[ks["tail"]].why and t[ks["tail"]].ancestors == 1


def test_edges_to_unknown_nodes_are_skipped_not_fatal():
    a = md.node_key("python", "a.py", "a")
    g = md.Graph(
        (md.node(a, "func"),), (md.edge(a, "python:zz.py#gone", "call"),), (md.root("main", "m:1", node=a),), ()
    )
    prep = rc.prepare(g)
    assert prep.skipped == 1 and rc.forward(prep).verdict(a) == "alive"


def test_reach_result_does_not_depend_on_the_input_order_of_the_graph():
    rng = random.Random(3)
    g = _random_graph(5)
    nodes, es, rs = list(g.nodes), list(g.edges), list(g.roots)
    rng.shuffle(nodes), rng.shuffle(es), rng.shuffle(rs)
    assert rc.analyze(md.canonical(nodes, es, rs)).digest() == rc.analyze(g).digest()


# ================================================================ P1 fix round 1
def _test_repo_graph():
    files = {
        "lib/a.py": facts_of(defs=[d("used"), d("only_in_tests"), d("dead_helper"), d("dead_a"), d("dead_b")],
                             refs=[Ref("dead_b", "call", 4, "dead_a"), Ref("dead_a", "call", 5, "dead_b")]),
        "bin/tool": facts_of(defs=[d("main")], roots=[RootHint("main", "main", 1)], refs=[Ref("used", "exec", 2, "main")]),
        "tests/test_a.py": facts_of(defs=[d("test_x"), d("TestK"), d("TestK.test_m"), d("helper")],
                                    refs=[Ref("only_in_tests", "call", 3, "test_x"), Ref("helper", "call", 4, "TestK.test_m")]),
    }  # fmt: skip
    files["bin/tool"] = facts_of(defs=[d("main")], roots=[RootHint("main", "main", 1)], imports=[Import("lib.a", ("used",), "", 1)],
                                 refs=[Ref("used", "call", 2, "main")])  # fmt: skip
    hints = {"(tests)": [RootHint("test", "file:tests/test_a.py", 1, "soft", "convention")]}
    return run(files, hints).graph


def test_a_test_root_seeds_every_symbol_of_its_file_so_plain_tests_are_not_dead():
    g = _test_repo_graph()
    res = rc.analyze(g)
    v = {short(k): x for k, x in res.verdicts().items()}
    # the test framework collects test_x, TestK.test_m and what the test file defines: test-only, never dead or a dead cluster
    assert (
        v["tests/test_a.py#test_x"] == v["tests/test_a.py#TestK.test_m"] == v["tests/test_a.py#helper"] == "test-only"
    )
    assert v["lib/a.py#only_in_tests"] == "test-only"  # used only inside a test body
    assert v["lib/a.py#used"] == "alive" and v["lib/a.py#dead_helper"] == "dead"
    assert v["lib/a.py#dead_a"] == v["lib/a.py#dead_b"] == "dead-cluster"
    wa = res.why_alive("python:tests/test_a.py#test_x")
    assert wa.verdict == "test-only" and wa.root.kind == "test" and wa.steps == ()
    assert not res.why_dead("python:lib/a.py#dead_helper").render().startswith("test-only")


def test_reverse_search_agrees_with_the_forward_pass_on_test_seeds():
    g = _test_repo_graph()
    prep = rc.prepare(g)
    got = rc.touched(prep, [n.key for n in g.nodes if n.kind != "file"])
    fwd = rc.forward(prep).verdicts()
    assert {k: t.verdict for k, t in got.items() if t.verdict != "dead-cluster"} == {
        k: v for k, v in fwd.items() if k in got and v != "dead-cluster"
    }
    assert got["python:tests/test_a.py#test_x"].verdict == "test-only"


def test_why_dead_names_the_annotation_roots_once_not_one_by_one():
    files = {"a.py": facts_of(defs=[d("main"), d("lonely")], roots=[RootHint("annotation:spring-a", "main", 1), RootHint("annotation:spring-b", "main", 2),
                                                                    RootHint("annotation:spring-c", "main", 3), RootHint("cron", "main", 4)])}  # fmt: skip
    text = rc.analyze(run(files).graph).why_dead("python:a.py#lonely").render()
    assert "annotation of 3 rules" in text and "spring-a" not in text and "cron" in text


def test_a_dotted_call_on_an_unseen_module_alias_reaches_the_top_level_def_of_that_name():
    files = {
        "gate.py": facts_of(defs=[d("run")], refs=[Ref("m.DTS.coverage", "call", 3, "run")]),
        "ts.py": facts_of(defs=[d("coverage"), d("Cls"), d("Cls.coverage")]),
    }
    res = run(files)
    assert has(res, "gate.py#run", "ts.py#coverage", "call", "N") and has(
        res, "gate.py#run", "ts.py#Cls.coverage", "call", "N"
    )


def test_link_paths_is_the_part_of_a_full_link_that_the_given_files_emit():
    files = {
        "a.py": facts_of(defs=[d("f"), d("g")], refs=[Ref("g", "call", 2, "f"), Ref("h", "call", 3, "f")]),
        "b.py": facts_of(defs=[d("h"), d("Base"), d("Base.m"), d("Kid"), d("Kid.m")], imports=[Import("a", ("f",), "", 1)],
                         refs=[Ref("f", "call", 5, "h"), Ref("Base", "inherit", 6, "Kid")]),
        "c.py": facts_of(defs=[d("k")], imports=[Import("b", ("h",), "", 1)], refs=[Ref("h", "call", 4, "k"), Ref("x.g", "call", 5, "k")]),
    }  # fmt: skip
    full = run(files)
    for paths in (["a.py"], ["b.py", "c.py"], ["a.py", "b.py", "c.py"], ["zzz.py"]):
        part = dlink.link_paths(files, paths, workers=1)
        want = {(e.src, e.dst, e.kind, e.tier, e.site) for e in full.graph.edges if e.site.rpartition(":")[0] in paths}
        assert {(e.src, e.dst, e.kind, e.tier, e.site) for e in part.edges} == want
        assert [n.key for n in part.nodes] == [n.key for n in full.graph.nodes]
        assert all(e.ev == -1 for e in part.edges)
    assert dlink.link_paths(files, [], workers=1).edges == ()


def test_canonical_graph_does_not_depend_on_the_input_order_and_dedupes():
    f = {"a.py": facts_of(defs=[d("f"), d("g")], refs=[Ref("g", "call", 2, "f"), Ref("g", "call", 2, "f")])}
    g1 = run(f).graph
    g2 = md.canonical(reversed(g1.nodes), list(reversed(g1.edges)) + list(g1.edges), g1.roots, g1.evid)
    assert g2 == g1 and all(type(e) is md.Edge for e in g1.edges)
    with pytest.raises(ValueError, match="outside the pool"):
        md.canonical(g1.nodes, [md.Edge("python:a.py#f", "python:a.py#g", "call", "T", "a.py:2", 99)], (), ())
