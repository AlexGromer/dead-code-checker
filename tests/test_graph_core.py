"""The core of the graph checker: model, langs, contracts and the toy extractor, the store and vcs.

python3 -m pytest -q tests/test_graph_core.py

Every repository lives under tmp_path; nothing touches the network or the data directory of the tool.
"""

import os
import random
import shutil
import stat
import sys
from pathlib import Path

import pytest
from graph_support import git

from dead_code_checker import core
from dead_code_checker.graph import contracts, langs, model, store, vcs

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.usefixtures("hermetic_git"),
]
ROOT = Path(__file__).resolve().parent.parent


# ================================================================ model


# ================================================================ model
def test_node_key_roundtrip():
    k = model.node_key("python", "a/b#c%d.py", "Cls.meth", 2, "deadbeef")
    assert k == "python:a/b%23c%25d.py#Cls.meth/2~deadbeef"
    assert model.split_key(k) == ("python", "a/b#c%d.py", "Cls.meth", 2, "deadbeef")
    assert model.split_key(model.node_key("go", "x.go")) == ("go", "x.go", "", -1, "")
    assert model.node_key("go", "x.go", "F", -1) == "go:x.go#F"
    with pytest.raises(ValueError):
        model.node_key("", "x")


def test_factories_validate():
    n = model.node("python:a.py#f", "func", line0=3)
    assert (n.lang, n.path, n.line1) == ("python", "a.py", 3)
    for bad in (lambda: model.node("python:a.py#f", "bogus"), lambda: model.edge("a", "b", "call", "X"),
                lambda: model.edge("a", "b", "jump"), lambda: model.root("main", node="a", selector="file:x"),
                lambda: model.root("nope", node="a"), lambda: model.root("hook", node="a"),  # the root kind of an agent setup is no kind here
                lambda: model.root("main", selector="url:x")):  # fmt: skip
        with pytest.raises(ValueError):
            bad()
    assert model.root("annotation:spring.scheduled", node="a").kind.startswith("annotation:")
    assert model.TIERS == {"S": 1, "T": 2, "N": 4, "D": 6}
    assert model.flags_of("test", "stub") == model.F_TEST | model.F_STUB


def test_evidence_snippet_is_cut_to_120_bytes():
    e = model.evidence("py", "a.py", 3, "é" * 200)
    assert len(e.snippet.encode()) <= 120 and e.snippet.startswith("é")


def _sample_graph(rng=None):
    nodes = [model.node(f"python:m.py#f{i}", "func", line0=i) for i in range(12)]
    evid = [model.evidence("py", "m.py", i, f"rule{i % 3}") for i in range(6)]
    edges = [
        model.edge(f"python:m.py#f{i}", f"python:m.py#f{(i * 5 + 1) % 12}", "call", "TNSD"[i % 4], f"m.py:{i}", i % 6)
        for i in range(12)
    ]
    edges.append(model.edge("python:m.py#f0", "python:m.py#f1", "ref"))
    roots = [
        model.root("main", "m.py:1", node="python:m.py#f0"),
        model.root("bin", "s.json:4", selector="file:bin/x", strength="soft"),
    ]
    if rng:
        perm = list(range(len(evid)))
        rng.shuffle(perm)  # the pool order changes, the edges keep pointing at the same records
        evid = [evid[perm.index(i)] for i in range(len(evid))]
        edges = [e._replace(ev=perm[e.ev]) if e.ev >= 0 else e for e in edges]
        for lst in (nodes, edges, roots):
            rng.shuffle(lst)
    return nodes, edges, roots, evid


def test_canonical_is_order_independent_and_remaps_evidence():
    ref = model.canonical(*_sample_graph())
    for seed in range(5):
        g = model.canonical(*_sample_graph(random.Random(seed)))
        assert g == ref and g.digest() == ref.digest()
    # the evidence behind an edge is the same record whatever the pool order
    by_site = {e.site: ref.evid[e.ev] for e in ref.edges if e.ev >= 0}
    assert by_site["m.py:7"].line == 1  # i=7 -> pool index 1
    assert list(ref.nodes) == sorted(ref.nodes) and list(ref.edges) == sorted(ref.edges)


def test_canonical_rejects_conflicting_duplicates_and_bad_evidence_index():
    a = model.node("go:x.go#F", "func")
    with pytest.raises(ValueError):
        model.canonical([a, a._replace(line0=9)])
    assert model.canonical([a, a]).nodes == (a,)
    with pytest.raises(ValueError):
        model.canonical([a], [model.edge(a.key, a.key, "call", ev=3)])


def test_finding_has_the_fields_and_the_dict_of_the_text_checker():
    a = core.Finding("W", "x/y.py", 7, "dead-func", "f is never called")
    b = model.Finding("W", "x/y.py", 7, "dead-func", "f is never called")
    assert a.as_dict() == b.as_dict() and "why" not in b.as_dict()
    assert b.render() == "dead-code-checker: W x/y.py:7: dead-func: f is never called"
    assert model.Finding("E", "p", 0, "c", "m").render() == "dead-code-checker: E p: c: m"  # no line: no ":0"
    assert model.Finding("E", "p", 1, "c", "m", why="roots: none").as_dict()["why"] == "roots: none"
    order = [
        model.Finding("W", "b", 1, "c", "m"),
        model.Finding("E", "z", 1, "c", "m"),
        model.Finding("W", "b", 1, "c", "m"),
    ]
    assert [f.path for f in model.sort_findings(order)] == ["z", "b"]


def _facts():
    return model.Facts(
        "dcref",
        defs=(model.Def("b", "func", 5, 5), model.Def("A.m", "method", 2, 3, "private", model.F_STUB, 1, "pass")),
        refs=(model.Ref("b", "call", 9, "A.m"), model.Ref("a.f", "ref", 1)),
        imports=(model.Import("os.path", ("join", "split"), "", 1, 0, ""), model.Import("x", (), "y", 2, 1, "A.m")),
        roots=(model.RootHint("main", "b", 5),),
        dyn=(model.DynSite("getattr(x, s)", 4, "A.m"),),
        errors=1,
    )


def test_facts_payload_roundtrip_and_canonical_order():
    f = _facts()
    p = f.to_payload()
    back = model.Facts.from_payload(p)
    assert back.to_payload() == p and back.imports[0].names == ("join", "split") and isinstance(back.defs[0], model.Def)
    shuffled = f._replace(defs=f.defs[::-1], refs=f.refs[::-1], imports=f.imports[::-1])
    assert shuffled.to_payload() == p
    with pytest.raises(ValueError):
        model.Facts.from_payload(p.replace(b'"v":1', b'"v":9'))


def test_shard_key_depends_on_every_input_and_nothing_else():
    k = model.shard_key("b1", "python", "py@1", "g")
    assert k == model.shard_key("b1", "python", "py@1", "g")
    assert len({k, model.shard_key("b2", "python", "py@1", "g"), model.shard_key("b1", "go", "py@1", "g"),
                model.shard_key("b1", "python", "py@2", "g"), model.shard_key("b1", "python", "py@1", "h")}) == 5  # fmt: skip
    s = model.new_shard("b1", "python", "py@1", _facts(), "g")
    assert s.key == k and s.sha == model.payload_sha(s.payload) and s.facts() == model.Facts.from_payload(s.payload)


def test_selector_scheme_tells_a_selector_from_a_name():
    assert model.selector_scheme("node:python:a.py#f") == "node" and model.selector_scheme("file:bin/x") == "file"
    assert model.selector_scheme("class:a.B#m") == "class" and model.selector_scheme("build:recipe:test") == "build"
    assert (
        model.selector_scheme("node") == ""
        and model.selector_scheme("node::x") == ""
        and model.selector_scheme("f") == ""
    )
    assert model.qual_name("a.b/c:d") == "a%2Eb%2Fc%3Ad" and model.qual_name("a*b") == "a*b"
    assert model.flags_of() == 0 and model.flags_of("generated") == model.F_GENERATED


# ================================================================ langs


L = langs.load()


@pytest.mark.parametrize("path,head,want", [
    ("a/b.py", None, "python"), ("A.PY", None, "python"), ("x.pyi", None, "python"), ("lib/a.sh", None, "shell"),
    ("main.go", None, "go"), ("src/lib.rs", None, "rust"), ("a.mjs", None, "javascript"), ("a.d.ts", None, "typescript"),
    ("c.tsx", None, "tsx"), ("A.java", None, "java"), ("a.kts", None, "kotlin"), ("a.c", None, "c"), ("a.hpp", None, "cpp"),
    ("a.cs", None, "csharp"), ("m.psm1", None, "powershell"), ("main.tf", None, "hcl"), ("ci.yml", None, "yaml"),
    ("Dockerfile", None, "dockerfile"), ("sub/Makefile", None, "make"), ("a.service", None, "systemd"),
    ("a.json", None, "json"), ("README.md", None, "markdown"), ("a.csproj", None, "msbuild"),
    ("noext", None, None), ("weird.zzz", b"#!/bin/sh\n", "shell"), ("weird.zzz", b"text\n", None), (".py", None, None),
    ("bin/tool", b"#!/usr/bin/env python3\nprint(1)", "python"), ("bin/t", "#!/usr/bin/python3.11\n", "python"),
    ("bin/s", b"#!/bin/bash -e\n", "shell"), ("bin/e", b"#!/usr/bin/env -S bash -eu\n", "shell"),
    ("bin/n", b"#!/usr/bin/env FOO=1 node\n", "javascript"), ("bin/p", b"#!/usr/bin/env pwsh\n", "powershell"),
    ("bin/x", b"#!/usr/bin/perl\n", None), ("bin/y", b"no shebang\n", None), ("bin/z", b"#!\n", None),
    ("a.h", None, "c"), ("a.h", b"#include <stdio.h>\nint f(void);", "c"), ("a.h", b"namespace a {\n}\n", "cpp"),
    ("a.h", b"#pragma once\ntemplate <class T> T f();", "cpp"), ("a.h", b'extern "C++" {', "cpp"),
])  # fmt: skip
def test_detect(path, head, want):
    assert L.detect(path, head) == want


def test_needs_head():
    assert (
        L.needs_head("bin/tool") and L.needs_head("a.h") and not L.needs_head("a.py") and not L.needs_head("Dockerfile")
    )


def test_langs_table_is_consistent():
    for name, spec in L.langs.items():
        assert spec["role"] in ("code", "config", "data"), name
        assert spec.get("family") is None or isinstance(spec["family"], str), name
        if spec.get("extractor"):
            # an extractor reads code or config; the schema languages (sql, proto, graphql) are data that is read as well
            assert spec["role"] != "data" or spec["extractor"] == "schema", name
    assert L.family("python") == "python" and L.grammar("python") == "python" and L.extractor("python") == "py"
    assert L.analyzable("python") and L.analyzable("yaml") and not L.analyzable("json") and L.role("markdown") == "data"
    assert all(rx is None or hasattr(rx, "search") for rules in L.ambiguous.values() for rx, _ in rules)
    grammars = {spec.get("grammar") for spec in L.langs.values()}
    for alias, target in L.query_alias.items():  # grammar -> the grammar whose queries it shares
        assert target in grammars, alias


def test_script_lang_names_a_file_that_its_tool_runs():
    assert L.script_lang("app/build.gradle.kts") == "kotlin" and L.script_lang("lib/util.py") is None
    assert L.script_lang("a/b/index.php") == "php" and L.script_lang("README.md") is None


def test_langs_builds_from_a_dict_and_extension_conflicts_resolve_deterministically():
    data = {"langs": {"a": {"ext": [".x"]}, "b": {"ext": [".x", ".y"]}}}
    assert langs.Langs(data).detect("f.x") == "b" == langs.Langs(data).detect("f.x")  # sorted order, last one wins


def test_profile():
    files = ["a.py", "b.py", "c.go", "README.md", "x.json", "bin/run", "bin/old", "inc/a.h", "Dockerfile"]
    heads = {"bin/run": b"#!/bin/bash\n", "bin/old": b"#!/usr/bin/perl\n", "inc/a.h": b"namespace x {}"}
    p = langs.profile(files, head=heads.get)
    assert p.languages == ("python", "cpp", "dockerfile", "go", "shell") and p.mode == "closed_world"
    assert p.counts["markdown"] == 1 and p.counts["python"] == 2 and "json" not in p.languages
    assert langs.profile(files, mode="library").mode == "library"
    assert langs.profile([]).languages == ()
    with pytest.raises(ValueError):
        langs.profile(files, mode="open")


# ================================================================ contracts + the toy extractor


DCREF = """\
import os.path join
def Cls 1 stub:pass
def Cls.run private
ref helper     # call from Cls.run
dyn getattr(x, s)
def helper 2
root main helper
nonsense here
"""


def test_reference_extractor_contract(dcref):
    ex = contracts.get_extractor("dcref")
    assert ex.ext_ver == "ref@1" and ex.langs == ("dcref",)
    f = ex.extract("dcref", "a.dcref", DCREF)
    assert [d.qual for d in f.defs] == ["Cls", "Cls.run", "helper"] and f.errors == 1
    assert (
        f.defs[0].stub == "pass"
        and f.defs[0].flags & model.F_STUB
        and f.defs[1].vis == "private"
        and f.defs[2].arity == 2
    )
    assert f.refs == (model.Ref("helper", "call", 4, "Cls.run"),) and f.imports[0].names == ("join",)
    assert f.roots == (model.RootHint("main", "helper", 7),) and f.dyn[0].scope == "Cls.run"
    # a function of (lang, text) only: the path does not matter, and the same content gives the same shard everywhere
    assert ex.extract("dcref", "other/place.dcref", DCREF) == f
    s1 = contracts.make_shard(ex, "dcref", "a.dcref", "blob1", DCREF)
    s2 = contracts.make_shard(ex, "dcref", "z/b.dcref", "blob1", DCREF)
    assert s1 == s2 and s1.ext_ver == "ref@1" and s1.facts() == f


def test_registry_imports_the_module_named_in_langs_json_and_registers_again_after_a_reset(tmp_path, monkeypatch):
    import dead_code_checker.graph.extract as ext

    (tmp_path / "zzmod.py").write_text(
        "from dead_code_checker.graph import contracts, model\n"
        "class X(contracts.Extractor):\n"
        "    name, version, langs = 'zz', 1, ('zzlang',)\n"
        "    def extract(self, lang, path, text):\n"
        "        return model.Facts(lang)\n"
        "contracts.register_extractor(X())\n"
    )
    monkeypatch.setattr(ext, "__path__", [*ext.__path__, str(tmp_path)])  # the module is found next to the real ones
    monkeypatch.setattr(contracts, "_EXTRACTORS", {})
    monkeypatch.setattr(contracts, "_ROOTS", {})
    monkeypatch.delitem(sys.modules, "dead_code_checker.graph.extract.zzmod", raising=False)
    fake = langs.Langs({"langs": {"zzlang": {"ext": [".zz"], "extractor": "zzmod"}}})
    assert contracts.defined("zzlang", fake) and "zzlang" not in contracts._EXTRACTORS
    assert contracts.get_extractor("zzlang", fake).name == "zz"
    contracts.reset_registry()  # forgotten ...
    assert contracts.root_extractors() == [] and not contracts._EXTRACTORS
    assert contracts.get_extractor("zzlang", fake).name == "zz"  # ... and registered again by a second import
    monkeypatch.delitem(sys.modules, "dead_code_checker.graph.extract.zzmod", raising=False)


def test_missing_extractor_is_explicit_never_a_fallback():
    fake = langs.Langs(
        {"langs": {"zz": {"ext": [".zz"], "extractor": "no_such_module"}, "nn": {"ext": [".nn"], "extractor": None}}}
    )
    for lang, text in (("zz", "cannot be imported"), ("nn", "no extractor"), ("nope", "unknown language")):
        with pytest.raises(contracts.ExtractorMissing, match=text) as ei:
            contracts.get_extractor(lang, fake)
        assert ei.value.lang == lang and ei.value.reason


def test_defined_tells_a_language_without_an_extractor_from_one_that_cannot_run():
    fake = langs.Langs(
        {"langs": {"zz": {"extractor": "no_such_module"}, "nn": {"extractor": None}, "py": {"extractor": "py"}}}
    )
    assert (
        not contracts.defined("zz", fake) and not contracts.defined("nn", fake) and not contracts.defined("nope", fake)
    )
    assert contracts.defined("py", fake)  # a module of graph/extract/ that exists

    class Plain(contracts.Extractor):
        name, version, langs = "plain", 1, ("zz",)

        def extract(self, lang, path, text):
            return model.Facts(lang)

    assert Plain().supports("zz") is True and Plain().probe("zz") is None and Plain().ext_ver == "plain@1"


def test_abstract_contracts_cannot_be_instantiated_and_registration_needs_a_name():
    with pytest.raises(TypeError):
        contracts.Extractor()
    with pytest.raises(TypeError):
        contracts.RootExtractor()

    class Bare(contracts.Extractor):
        def extract(self, lang, path, text):
            return model.Facts(lang)

    with pytest.raises(ValueError):
        contracts.register_extractor(Bare())


def test_reference_root_extractor_and_virtual_shard(dcref):
    ex = contracts.get_extractor("dcref")
    rx = next(r for r in contracts.root_extractors() if r.name == "dcroots")
    assert rx.matches("x/roots.dcroots") and not rx.matches("x.py")
    hints = rx.extract("r.dcroots", "bin file:bin/x\n# c\nworkflow cmd:y soft\nbad\n", None)
    assert hints == [model.RootHint("bin", "file:bin/x", 1), model.RootHint("workflow", "cmd:y", 3, "soft")]
    sh = contracts.make_root_shard(rx, "r.dcroots", "blobR", "bin file:bin/x\n")
    assert sh.lang == "root:dcroots" and sh.facts().roots[0].target == "file:bin/x"
    assert sh.key != contracts.make_shard(ex, "dcref", "r.dcroots", "blobR", "").key


# ================================================================ store


# ================================================================ store
@pytest.fixture
def st(tmp_path):
    s = store.Store(str(tmp_path / "d" / "u" / "r" / "store.sqlite"))
    yield s
    s.close()


def _shards(n=5):
    ex = contracts.get_extractor("dcref")
    return [contracts.make_shard(ex, "dcref", f"{i}.dcref", f"blob{i}", f"def f{i}\nref g{i}\n") for i in range(n)]


def test_store_permissions_journal_and_reopen(st, dcref):
    d = os.path.dirname(st.path)
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700 and stat.S_IMODE(os.stat(st.path).st_mode) == 0o600
    assert st._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "truncate"
    assert st.reset_reason == "" and st.check()
    st.put_shards(_shards())
    st.close()
    again = store.Store(st.path)
    assert again.reset_reason == "" and again.shard_count() == 5
    again.close()


def test_shard_roundtrip(st, dcref):
    shards = _shards()
    assert st.put_shards(shards) == 5
    assert st.has_shards([s.key for s in shards] + ["nope"]) == {s.key for s in shards}
    got = st.get_shard(shards[2].key)
    assert got == shards[2] and got.facts().defs[0].qual == "f2"
    assert st.get_shard("nope") is None
    changed = shards[0]._replace(payload=shards[1].payload, sha="")
    assert (
        st.put_shards([changed], replace=False) == 1 and st.get_shard(shards[0].key) == shards[0]
    )  # the old row stays
    with pytest.raises(store.StoreError):
        st.put_shard(shards[1]._replace(sha="0" * 64))


def test_corrupt_shard_row_is_deleted_not_served(st, dcref):
    s = _shards(1)[0]
    st.put_shard(s)
    st._conn.execute("UPDATE shard SET payload_sha=? WHERE key=?", ("f" * 64, s.key))
    assert st.get_shard(s.key) is None and st.has_shards([s.key]) == set()
    st.put_shard(s)
    st._conn.execute("UPDATE shard SET facts=? WHERE key=?", (b"not zlib", s.key))
    assert st.get_shard(s.key) is None


def test_get_facts_decodes_each_intact_shard_once_and_heals_the_corrupt_ones(st, dcref):
    sh = _shards(4)
    st.put_shards(sh)
    st._conn.execute("UPDATE shard SET payload_sha=? WHERE key=?", ("f" * 64, sh[1].key))
    st._conn.execute("UPDATE shard SET facts=? WHERE key=?", (b"not zlib", sh[2].key))
    got = st.get_facts([sh[0].key, sh[1].key, sh[2].key, sh[3].key, "absent", sh[0].key])
    assert got == {sh[0].key: sh[0].facts(), sh[3].key: sh[3].facts()}
    assert st.has_shards([s.key for s in sh]) == {sh[0].key, sh[3].key}  # the bad rows are gone, like with get_shard
    assert st.get_facts([]) == {}


def test_put_graph_of_a_canonical_graph_stores_the_same_rows(st, tmp_path):
    g = model.canonical(*_sample_graph(random.Random(3)))
    st.put_graph(g, canonical_input=True)
    assert st.get_graph() == g
    other = store.Store(str(tmp_path / "o" / "u" / "r" / "store.sqlite"))
    other.put_graph(model.Graph(g.nodes, tuple(reversed(g.edges)), g.roots, g.evid))  # an unsorted one is sorted first
    assert other.get_graph() == g
    other.close()


def test_snapshot_by_tree_and_gc(st, dcref):
    sh = _shards(4)
    st.put_shards(sh)
    st.put_snapshot("t1", {"a": sh[0].key, "b": sh[1].key})
    st.put_snapshot("t2", {"a": sh[2].key})
    assert st.get_snapshot("t1") == {"a": sh[0].key, "b": sh[1].key} and st.trees() == ["t1", "t2"]
    st.put_snapshot("t1", {"b": sh[1].key})  # replaces
    assert st.get_snapshot("t1") == {"b": sh[1].key}
    assert (
        st.gc(["t1"]) == 3 and st.trees() == ["t1"] and st.shard_count() == 1
    )  # t2 dropped, only sh[1] stays referenced
    assert st.get_snapshot("missing") == {}


def test_graph_roundtrip_is_canonical_and_deterministic(tmp_path):
    ref = model.canonical(*_sample_graph())
    digests = set()
    for seed in range(3):
        with store.Store(str(tmp_path / f"s{seed}" / "g.sqlite")) as s:
            s.put_graph(model.Graph(*_sample_graph(random.Random(seed))))
            g = s.get_graph()
            assert g == ref
            digests.add(g.digest())
            s.put_graph(model.Graph(*_sample_graph()))  # replace-all, no leftovers
            assert s.get_graph() == ref
    assert digests == {ref.digest()}


def test_graph_with_dangling_edge_is_refused_and_rolled_back(st):
    n = model.node("go:x.go#F", "func")
    st.put_graph(model.canonical([n]))
    with pytest.raises(store.StoreError):
        st.put_graph(model.Graph((n,), (model.edge(n.key, "go:x.go#G", "call"),), (), ()))
    assert st.get_graph().nodes == (n,)


def test_tx_rolls_back_on_error_and_nests(st):
    with pytest.raises(RuntimeError):
        with st.tx():
            st.set_meta("a", "1")
            raise RuntimeError
    assert st.get_meta("a") is None
    with st.tx():
        st.set_meta("a", "1")
        with st.tx():
            st.set_meta("b", "2")
    assert (st.get_meta("a"), st.get_meta("b"), st.get_meta("c", "dflt")) == ("1", "2", "dflt")


def test_engine_results(st):
    st.put_engine_result("k1", "vulture", b'{"x":1}')
    assert st.get_engine_result("k1") == b'{"x":1}' and st.get_engine_result("k2") is None
    st.put_engine_result("k1", "vulture", b"2")
    assert st.get_engine_result("k1") == b"2"
    for k in ("sem:a:1", "sem:a:2", "eng:b:1"):
        st.put_engine_result(k, "scip", b"x")
    assert st.engine_keys("sem:") == ["sem:a:1", "sem:a:2"] and st.engine_keys("") == sorted(st.engine_keys(""))
    assert st.delete_engine_results(["sem:a:1", "nope"]) == 1 and st.engine_keys("sem:") == ["sem:a:2"]


@pytest.mark.parametrize("damage", ["garbage", "schema", "loose"])
def test_unusable_cache_is_dropped_and_reported(tmp_path, damage, dcref):
    path = str(tmp_path / "c" / "graph.sqlite")
    with store.Store(path) as s:
        s.put_shards(_shards(2))
        if damage == "schema":
            s._conn.execute("UPDATE meta SET v='0' WHERE k='schema_version'")
    if damage == "garbage":
        Path(path).write_bytes(b"this is not sqlite" * 200)
    if damage == "loose":
        os.chmod(path, 0o644)
    with store.Store(path) as s2:
        assert s2.reset_reason and s2.shard_count() == 0 and s2.get_meta("schema_version") == str(store.SCHEMA_VERSION)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_foreign_user_and_symlink_are_refused(tmp_path, monkeypatch):
    path = str(tmp_path / "c" / "graph.sqlite")
    store.Store(path).close()
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(store.StoreError, match="foreign"):
        store.Store(path)
    monkeypatch.undo()
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "lnk"
    link.symlink_to(real)
    with pytest.raises(store.StoreError):
        store.Store(str(link / "graph.sqlite"))
    target = tmp_path / "t.sqlite"
    target.write_bytes(b"")
    (tmp_path / "d2").mkdir()
    (tmp_path / "d2" / "graph.sqlite").symlink_to(target)
    with pytest.raises(store.StoreError):
        store.Store(str(tmp_path / "d2" / "graph.sqlite"))


# ================================================================ vcs


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    for rel, text in {
        "a.py": "def a(): pass\n",
        "lib/b.py": "x = 1\n",
        "bin/run": "#!/bin/bash\necho\n",
        "doc.md": "# d\n",
        ".gitignore": "ignored.py\n",
    }.items():
        (r / rel).parent.mkdir(exist_ok=True)
        (r / rel).write_text(text)
    os.chmod(r / "bin/run", 0o755)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "one")
    return r


def hash_object(r, rel):
    return git(r, "hash-object", rel)


def test_blob_sha_equals_git_hash_object(repo):
    for rel in ("a.py", "bin/run"):
        assert vcs.blob_sha((repo / rel).read_bytes()) == hash_object(repo, rel)
    assert vcs.blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert vcs.object_format(str(repo)) == "sha1"


def test_snapshot_clean_tracked_files_come_from_the_index(repo):
    t = vcs.snapshot(str(repo))
    assert t.git and sorted(t.files) == [".gitignore", "a.py", "bin/run", "doc.md", "lib/b.py"]
    assert all(not f.dirty and f.tracked for f in t.files.values())
    assert (
        t.files["a.py"].blob == hash_object(repo, "a.py")
        and t.files["bin/run"].mode == "100755"
        and t.files["a.py"].mode == "100644"
    )
    assert t.id == vcs.snapshot(str(repo)).id and t.skipped == {}


def test_snapshot_dirty_untracked_deleted_ignored_staged(repo):
    (repo / "a.py").write_text("def a(): return 1\n")  # modified
    (repo / "new.py").write_text("n = 1\n")  # untracked
    (repo / "ignored.py").write_text("i = 1\n")  # ignored
    (repo / "lib/b.py").unlink()  # deleted
    (repo / "doc.md").write_text("# staged\n")
    git(repo, "add", "doc.md")  # staged and otherwise clean: its index blob is used
    t = vcs.snapshot(str(repo))
    assert sorted(t.files) == [".gitignore", "a.py", "bin/run", "doc.md", "new.py"]
    assert t.files["a.py"].dirty and t.files["a.py"].tracked and t.files["a.py"].blob == hash_object(repo, "a.py")
    assert (
        t.files["new.py"].dirty
        and not t.files["new.py"].tracked
        and t.files["new.py"].blob == hash_object(repo, "new.py")
    )
    assert not t.files["doc.md"].dirty and t.files["doc.md"].blob == hash_object(repo, "doc.md")
    assert not t.files["bin/run"].dirty


def test_snapshot_skips_symlinks_and_reports_it(repo):
    (repo / "lnk.py").symlink_to("a.py")
    git(repo, "add", "lnk.py")
    t = vcs.snapshot(str(repo))
    assert "lnk.py" not in t.files and t.skipped == {"lnk.py": "symlink"}


def test_snapshot_want_filter(repo):
    (repo / "z.py").write_text("z\n")
    t = vcs.snapshot(str(repo), want=lambda p: p.endswith(".py"))
    assert sorted(t.files) == ["a.py", "lib/b.py", "z.py"]


def test_no_git_mode_gives_the_same_tree_as_git_mode(repo, tmp_path):
    (repo / "a.py").write_text("def a(): return 2\n")
    (repo / "new.py").write_text("n = 1\n")
    copy = tmp_path / "copy"
    shutil.copytree(repo, copy, ignore=shutil.ignore_patterns(".git", "ignored.py"))
    (copy / "lnk").symlink_to("a.py")  # a symlink is never part of the tree
    tg, tn = vcs.snapshot(str(repo)), vcs.snapshot(str(copy))
    assert tg.git and not tn.git
    assert {p: (f.mode, f.blob) for p, f in tg.files.items()} == {p: (f.mode, f.blob) for p, f in tn.files.items()}
    assert tg.id == tn.id and all(f.dirty and not f.tracked for f in tn.files.values())


def test_blocked_git_falls_back_to_the_walk(repo):
    with core.foreign_git():
        t = vcs.snapshot(str(repo))
        assert not t.git and not vcs.git_ok(str(repo))
        assert ".git/HEAD" not in t.files and "a.py" in t.files and vcs.cat_batch(str(repo), ["x" * 40]) == {}
    assert vcs.snapshot(str(repo)).git


def test_snapshot_is_identical_for_1_2_and_8_workers(repo):
    for i in range(40):
        (repo / f"u{i}.py").write_text(f"v = {i}\n")
    base = vcs.snapshot(str(repo), workers=1)
    for w in (2, 8):
        assert vcs.snapshot(str(repo), workers=w) == base
    assert len(base.files) == 45


def test_unmerged_path_counts_as_dirty_once(repo):
    git(repo, "checkout", "-qb", "side")
    (repo / "a.py").write_text("side\n")
    git(repo, "commit", "-qam", "side")
    git(repo, "checkout", "-q", "main")
    (repo / "a.py").write_text("main\n")
    git(repo, "commit", "-qam", "main")
    git(repo, "merge", "side", check=False)
    t = vcs.snapshot(str(repo))
    assert t.files["a.py"].dirty and t.files["a.py"].blob == hash_object(repo, "a.py")
    assert [e.stage for e in vcs.ls_files(str(repo)) if e.path == "a.py"] == [1, 2, 3]


def test_empty_repository_without_commits(tmp_path):
    r = tmp_path / "empty"
    r.mkdir()
    git(r, "init", "-q")
    (r / "x.py").write_text("x\n")
    t = vcs.snapshot(str(r))
    assert t.git and list(t.files) == ["x.py"] and vcs.default_base(str(r)) is None


def test_read_files_batch_and_disk(repo):
    (repo / "a.py").write_text("dirty!\n")
    (repo / "new.py").write_text("new\n")
    t = vcs.snapshot(str(repo))
    got = vcs.read_files(str(repo), t, ["a.py", "lib/b.py", "bin/run", "new.py", "missing.py"])
    assert got == {"a.py": b"dirty!\n", "bin/run": b"#!/bin/bash\necho\n", "lib/b.py": b"x = 1\n", "new.py": b"new\n"}
    blob = t.files["lib/b.py"].blob
    assert vcs.cat_batch(str(repo), [blob, "0" * 40]) == {blob: b"x = 1\n", "0" * 40: None}
    assert vcs.cat_batch(str(repo), [blob], max_bytes=2) == {blob: None}


def test_read_files_in_no_git_mode(repo, tmp_path):
    copy = tmp_path / "c2"
    shutil.copytree(repo, copy, ignore=shutil.ignore_patterns(".git"))
    t = vcs.snapshot(str(copy))
    assert vcs.read_files(str(copy), t, ["a.py"]) == {"a.py": b"def a(): pass\n"}


def test_diff_name_status_with_rename_and_changed(repo):
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "mv", "lib/b.py", "lib/c.py")
    (repo / "a.py").write_text("def a(): return 1\n")
    git(repo, "rm", "-q", "doc.md")
    (repo / "bin/new").write_text("n\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "two")
    (repo / "u.py").write_text("untracked\n")
    ch = vcs.diff_name_status(str(repo), base, "HEAD")
    assert ch == [
        vcs.Change("M", "a.py"),
        vcs.Change("A", "bin/new"),
        vcs.Change("D", "doc.md"),
        vcs.Change("R", "lib/c.py", "lib/b.py"),
    ]
    assert vcs.changed(str(repo), base)[-1] == vcs.Change("?", "u.py") and len(vcs.changed(str(repo), base)) == 5
    (repo / "a.py").write_text("again\n")
    assert vcs.diff_name_status(str(repo), "HEAD") == [vcs.Change("M", "a.py")]
    git(repo, "add", "a.py")
    assert vcs.diff_name_status(str(repo), "HEAD", cached=True) == [vcs.Change("M", "a.py")]
    for bad in ("--output=/tmp/x", "", "a\nb"):
        with pytest.raises(ValueError):
            vcs.diff_name_status(str(repo), bad)
    assert vcs.diff_name_status(str(repo), "no-such-ref") == []


def test_merge_base_and_default_base(repo):
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "feature")
    (repo / "f.py").write_text("f\n")
    git(repo, "add", "f.py")
    git(repo, "commit", "-qm", "f")
    assert vcs.merge_base(str(repo), "main", "HEAD") == first and vcs.merge_base(str(repo), "nope") is None
    assert vcs.default_base(str(repo)) == (first, "main")
    assert vcs.default_base(str(repo), "main") == (first, "main") and vcs.default_base(str(repo), "nope") is None
    git(repo, "branch", "--set-upstream-to=main")
    assert vcs.default_base(str(repo)) == (first, "@{upstream}")
    assert vcs.head_commit(str(repo)) == git(repo, "rev-parse", "HEAD")


# ================================================================ whole path: tree -> shards -> store


# ================================================================ whole path: tree -> shards -> store
def test_tree_to_store_cold_equals_warm(repo, tmp_path, dcref):
    (repo / "r.dcref").write_text("def f\nref g\ndef g\n")
    (repo / "s.dcref").write_text("def f\nref g\ndef g\n")  # same content as r.dcref: one shard
    git(repo, "add", "-A")
    t = vcs.snapshot(str(repo), want=lambda p: langs.detect(p) == "dcref")
    assert sorted(t.files) == ["r.dcref", "s.dcref"] and t.files["r.dcref"].blob == t.files["s.dcref"].blob
    ex = contracts.get_extractor("dcref")
    data = vcs.read_files(str(repo), t, t.files)
    results = []
    for n in range(2):
        with store.Store(str(tmp_path / f"w{n}" / "g.sqlite")) as s:
            keys = {}
            for p, f in t.files.items():
                sh = contracts.make_shard(ex, "dcref", p, f.blob, data[p].decode())
                keys[p] = sh.key
                s.put_shards([sh])
            s.put_snapshot(t.id, keys)
            assert s.shard_count() == 1 and len(set(s.get_snapshot(t.id).values())) == 1
            results.append(s.get_shard(keys["r.dcref"]))
    assert results[0] == results[1]
