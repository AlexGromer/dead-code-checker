"""graph.sem: the SCIP reader, the overlay and the refutation rule, the LSP client, arbitration.

python3 -m pytest -q tests/test_graph_sem.py

No indexer, no language server and no network runs here.
  tests/fixtures/sem/<py|ts|go>/   tiny projects and the index.scip the installed indexers made of them, offline, in the
        directory itself (the commands are in fixture_commands below). They pin the reader on real output: legacy ranges
        (scip-python, scip-typescript), typed single-line ranges and is_implementation relationships (scip-go). The graphs of
        all three are written down as the Facts an extractor gives (py_facts, ts_facts, go_facts).
  tests/graph_scip_enc.py    writes an index for the rules no indexer shows on request (coverage 0.99, a `local` symbol, two
        indexes that resolve one name differently, a hostile file)
  tests/graph_fake_lsp.py    a scripted language server on stdio
"""

import ast
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import graph_scip_enc as enc
from dead_code_checker.graph import engines, link, model as md, sem
from dead_code_checker.graph.model import Def, Facts, Import, Ref

ROOT = Path(__file__).resolve().parent.parent

FIX = ROOT / "tests" / "fixtures" / "sem"
FAKE_LSP = ROOT / "tests" / "graph_fake_lsp.py"


def fixture_commands():
    """How the committed indexes were made: the sources written back as copy_fixture does, then, in that directory, with the
    installed engines and no network:
    py   scip-python index . --project-name fixpy --project-version 0.1 --output index.scip
    ts   NO_UPDATE_NOTIFIER=1 npm_config_offline=true scip-typescript index --output index.scip
    go   GOTOOLCHAIN=local GOPROXY=off GOFLAGS=-mod=readonly scip-go --output index.scip
    union/py   scip-python index . --project-name fixpy --project-version 0.1 --output index.scip      (x: A | B, Union[A, B])
    union/ts   NO_UPDATE_NOTIFIER=1 npm_config_offline=true scip-typescript index --output index.scip   (the same, and an any)
    all of them under `unshare -rn` with a private HOME; the .ts sources are stored as .ts.txt.
    """


# ================================================================ helpers for the fixtures
def copy_fixture(tmp_path: Path, lang: str) -> Path:
    """The fixture directory in tmp_path. A go or ts source is stored as <name>.<ext>.txt (the commit gate wants a tree-sitter
    grammar for every staged .go and .ts file, and none is installed on every host); it is written back as <name>.<ext>.
    """
    src, dst = FIX / lang, tmp_path / lang
    for p in sorted(src.rglob("*")):
        if p.is_file():
            rel = p.relative_to(src)
            rel = rel.with_suffix("") if rel.suffix == ".txt" and Path(rel.stem).suffix else rel
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst / rel)
    return dst


def tree_of(base: Path, prefix: str = "") -> dict:
    """{path: blob} of the fixture's files (not the index and its stamp), the paths under `prefix`."""
    return {
        (prefix + "/" if prefix else "") + p.relative_to(base).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
        for p in sorted(base.rglob("*"))
        if p.is_file() and not p.name.startswith("index.scip")
    }


def d(qual, kind, l0, l1=None):
    return Def(qual, kind, l0, l1 or l0)


def under(prefix, rel):
    return f"{prefix}/{rel}" if prefix else rel


def py_facts(base: Path, prefix: str = ""):
    """What a Python extractor gives for tests/fixtures/sem/py/pkg (the line numbers are those of the files)."""
    shapes = [d("", "module", 1, 16), d("Alpha", "class", 1, 3), d("Alpha.ping", "method", 2, 3)]
    shapes += [d("Beta", "class", 6, 8), d("Beta.ping", "method", 7, 8), d("helper", "func", 11, 12)]
    shapes += [d("unused", "func", 15, 16)]
    app_defs = (d("", "module", 1, 9), d("use", "func", 4, 5), d("main", "func", 8, 9))
    app_refs = (Ref("a.ping", "call", 5, "use"), Ref("helper", "call", 5, "use"))
    app_refs += (Ref("use", "call", 9, "main"), Ref("Alpha", "call", 9, "main"))
    imports = (Import("pkg.shapes", ("Alpha", "helper"), "", 1),)
    return {
        under(prefix, "pkg/__init__.py"): Facts("python", defs=(d("", "module", 1, 1),)),
        under(prefix, "pkg/shapes.py"): Facts("python", defs=tuple(shapes)),
        under(prefix, "pkg/app.py"): Facts("python", defs=app_defs, refs=app_refs, imports=imports),
    }


def ts_facts(prefix: str = ""):
    """What a TypeScript extractor gives for tests/fixtures/sem/ts/src (the line numbers are those of the files)."""
    shapes = [d("", "module", 1, 21), d("Alpha", "class", 1, 5), d("Alpha.ping", "method", 2, 4)]
    shapes += [d("Beta", "class", 7, 11), d("Beta.ping", "method", 8, 10), d("helper", "func", 13, 15)]
    shapes += [d("unused", "func", 17, 19)]
    app_defs = (d("", "module", 1, 9), d("use", "func", 3, 5), d("main", "func", 7, 9))
    app_refs = (Ref("Alpha", "ref", 3), Ref("a.ping", "call", 4, "use"), Ref("helper", "call", 4, "use"))
    app_refs += (Ref("use", "call", 8, "main"), Ref("Alpha", "call", 8, "main"))
    imports = (Import("./shapes", ("Alpha",), "", 1), Import("./shapes", ("helper",), "", 1))
    return {
        under(prefix, "src/shapes.ts"): Facts("typescript", defs=tuple(shapes)),
        under(prefix, "src/app.ts"): Facts("typescript", defs=app_defs, refs=app_refs, imports=imports),
    }


def go_facts(prefix: str = ""):
    """What a Go extractor gives for tests/fixtures/sem/go."""
    shapes = [d("", "module", 1, 15), d("Alpha", "class", 3), d("Alpha.Ping", "method", 5), d("Beta", "class", 7)]
    shapes += [d("Beta.Ping", "method", 9), d("Helper", "func", 11), d("unused", "func", 13)]
    shapes += [d("Pinger", "iface", 15), d("Pinger.Ping", "method", 15)]
    main_refs = (Ref("p.Ping", "call", 5, "use"), Ref("Helper", "call", 5, "use"))
    main_refs += (Ref("use", "call", 7, "main"), Ref("Alpha", "call", 7, "main"))
    main_defs = (d("", "module", 1, 7), d("use", "func", 5), d("main", "func", 7))
    return {
        under(prefix, "shapes/shapes.go"): Facts("go", defs=tuple(shapes)),
        under(prefix, "main.go"): Facts("go", defs=main_defs, refs=main_refs),
    }


def qual(key):
    return md.split_key(key)[2]


def edges_to(g, name, tier=None):
    """{(source qual, kind, tier)} of the edges that end at the node `name`."""
    return {(qual(e.src), e.kind, e.tier) for e in g.edges if qual(e.dst) == name and (tier is None or e.tier == tier)}


def s_sources(g):
    return {g.evid[e.ev].source for e in g.edges if e.tier == "S"}


def text_in(base: Path):
    """text_of for a fixture directory: the file of a tree path (None when it is not there)."""
    return lambda p: (base / p).read_text() if (base / p).is_file() else None


def py_world(tmp_path):
    base = copy_fixture(tmp_path, "py")
    tree = tree_of(base)
    sem.stamp(str(base / "index.scip"), tree, "", "python", "scip-python")
    return link.link(py_facts(base), workers=1).graph, tree, str(base / "index.scip")


# ================================================================ helpers for the hand-built rules
def mk_node(path, name, kind="func", l0=1, l1=None, flags=0, lang="python"):
    return md.node(md.node_key(lang, path, name), kind, lang, path, l0, l1 or l0, flags=flags)


def mk_file(path, lang="python"):
    return md.node(md.node_key(lang, path), "file", lang, path, 1, 1)


def mk_edge(src, dst, kind="call", tier="N", line=0):
    return md.edge(src.key, dst.key, kind, tier, f"{src.path}:{line}" if line else "")


def twin(prefix="", foo_m_flags=0, foo_kind="class", fillers=0):
    """File m.py: class Foo (line 1-2) with method m (2), class Bar (3-4) with method m (4), use(x) (5-6) that calls x.m() on
    line 6: a name edge to each m. `fillers` more one-line functions (lines 11..) pad the number of definitions."""
    p = under(prefix, "m.py")
    t = SimpleNamespace(
        file=mk_file(p),
        foo=mk_node(p, "Foo", foo_kind, 1, 2),
        foo_m=mk_node(p, "Foo.m", "method", 2, flags=foo_m_flags),
        bar=mk_node(p, "Bar", "class", 3, 4),
        bar_m=mk_node(p, "Bar.m", "method", 4),
        use=mk_node(p, "use", "func", 5, 6),
    )
    pad = [mk_node(p, f"f{i}", "func", 11 + i) for i in range(fillers)]
    t.graph = md.canonical(
        [t.file, t.foo, t.foo_m, t.bar, t.bar_m, t.use, *pad],
        [mk_edge(t.use, t.foo_m, line=6), mk_edge(t.use, t.bar_m, line=6)],
    )
    return t


def sy(descs):
    return enc.sym("m", descs)


def twin_index(resolved="Foo#m().", fillers=0, missing=0, refs=(), call_line=5, tool="scip-fake"):
    """The definitions of the twin graph (+ fillers f0.., the last `missing` of them undefined) and one reference on the line
    of x.m() that resolves to `resolved` (None: no reference there)."""
    occs = [
        enc.occ(0, 6, sy("Foo#"), enc.DEFINITION),
        enc.occ(1, 8, sy("Foo#m()."), enc.DEFINITION),
        enc.occ(2, 6, sy("Bar#"), enc.DEFINITION),
        enc.occ(3, 8, sy("Bar#m()."), enc.DEFINITION),
        enc.occ(4, 4, sy("use()."), enc.DEFINITION),
    ]
    if resolved:
        occs.append(enc.occ(call_line, 11, sy(resolved), enc.READ))
    occs += refs
    occs += [enc.occ(10 + i, 4, sy(f"f{i}()."), enc.DEFINITION) for i in range(fillers - missing)]
    return enc.index([enc.doc("m.py", occs)], tool=tool)


def stamped(tmp_path, graph, data, name="index.scip", root="", family="python", engine="scip-fake", tree=None):
    """Write an index and its stamp: (path, tree). The tree holds a blob for every file of the graph."""
    idx = tmp_path / name
    idx.write_bytes(data)
    tree = tree or {n.path: "b-" + n.path for n in graph.nodes}
    sem.stamp(str(idx), tree, root, family, engine)
    return str(idx), tree


# the source of the twin graph: x.m() is on line 6, the line a name edge of use() has as its site
TWIN_LINES = [
    "class Foo:",
    "    def m(self): pass",
    "class Bar:",
    "    def m(self): pass",
    "def use(x):",
    "    return x.m()",
]


def twin_text(prefix="", call="    return x.m()"):
    return {under(prefix, "m.py"): "\n".join([*TWIN_LINES[:5], call, ""])}.get


def refine_twin(tmp_path, t, data, name="index.scip", **kw):
    idx, tree = stamped(tmp_path, t.graph, data, name=name)
    kw.setdefault("text_of", twin_text())
    return sem.refine(t.graph, tree, [idx], workers=1, **kw)


# ================================================================ the wire format
def test_varint_and_fields_roundtrip_edge_values():
    for n in (0, 1, 127, 128, 300, 2**31 - 1, 2**63):
        assert sem._varint(enc.varint(n), 0) == (n, len(enc.varint(n)))
    msg = enc.num(1, 5) + enc.text(2, "ab") + enc.field(3, 5, b"\0\0\0\0") + enc.field(4, 1, b"\0" * 8)
    fields = list(sem._fields(msg))
    assert [(n, wt) for n, wt, _ in fields] == [(1, 0), (2, 2), (3, 5), (4, 1)]
    assert fields[0][2] == 5 and bytes(fields[1][2]) == b"ab"


@pytest.mark.parametrize(
    "data, why",
    [
        (enc.field(2, 2, enc.varint(50) + b"abc"), "runs past"),  # a length longer than what is left
        (enc.field(1, 5, b"\0\0"), "runs past"),  # a fixed32 with two bytes
        (enc.field(2, 3, b""), "wire type 3"),  # a group, which SCIP never uses
        (b"\x80" * 12, "varint"),  # a varint of 12 bytes
        (b"\x08", "not a SCIP index"),  # a key and then nothing: the varint of the value is cut
        (enc.blob(2, b"\x12\x05ab"), "runs past"),  # a document whose own field is cut
    ],
)
def test_hostile_or_truncated_input_is_a_scip_error(data, why):
    with pytest.raises(sem.ScipError, match=why):
        sem.read_scip(data)


def test_a_file_cut_anywhere_is_a_scip_error_or_a_shorter_index_never_another_exception():
    full = (FIX / "go" / "index.scip").read_bytes()
    outcomes = []
    for cut in range(0, len(full), 37):
        try:
            outcomes.append(len(sem.read_scip(full[:cut]).docs))
        except sem.ScipError:
            outcomes.append(-1)
    assert -1 in outcomes and max(outcomes) <= 2 and outcomes[0] == 0


def test_a_known_field_with_the_wrong_wire_type_is_ignored():
    # symbol_roles sent as a string and the symbol as a varint: both dropped, the occurrence keeps its range
    occ = enc.blob(1, enc.varint(1) + enc.varint(2) + enc.varint(3)) + enc.text(3, "x") + enc.num(2, 9)
    o = sem.read_scip(enc.index([enc.doc("a.py", [occ])])).docs["a.py"].occs[0]
    assert (o.line, o.col, o.symbol, o.roles) == (1, 2, "", 0)


def test_empty_input_is_an_empty_index_and_a_missing_file_a_scip_error(tmp_path):
    ix = sem.read_scip(b"")
    assert ix.docs == {} and ix.tool == ""
    with pytest.raises(sem.ScipError, match="No such file"):
        sem.read_scip_file(str(tmp_path / "nope.scip"))


def test_index_file_size_cap_and_stamp_binding(tmp_path, monkeypatch):
    p = tmp_path / "i.scip"
    p.write_bytes(enc.index([enc.doc("a.py")]))
    assert sorted(sem.read_scip_file(str(p)).docs) == ["a.py"]
    assert sem.read_scip_file(str(p), expect_sha=hashlib.sha256(p.read_bytes()).hexdigest()).docs
    with pytest.raises(sem.ScipError, match="not the index that was stamped"):
        sem.read_scip_file(str(p), expect_sha="0" * 64)
    monkeypatch.setitem(sem.config(), "max_index_bytes", 10)
    with pytest.raises(sem.ScipError, match="larger than 10 bytes"):
        sem.read_scip_file(str(p))


def test_range_styles_decode_to_the_same_start_and_a_repeated_path_merges():
    symbol = enc.sym("a", "f().")
    occs = [enc.occ(7 if i else 0, 3, symbol, enc.DEFINITION, style=st) for i, st in enumerate(enc.STYLES)]
    ix = sem.read_scip(enc.index([enc.doc("a.py", occs[:2], language="python"), enc.doc("a.py", occs[2:])]))
    # line 0 of a typed range is the omitted default of protobuf
    assert [(o.line, o.col) for o in ix.docs["a.py"].occs] == [(0, 3), (7, 3), (7, 3), (7, 3)]
    assert ix.docs["a.py"].language == "python"


@pytest.mark.parametrize(
    "lang, tool, docs",
    [
        ("py", "scip-python", ["pkg/__init__.py", "pkg/app.py", "pkg/shapes.py"]),
        ("ts", "scip-typescript", ["src/app.ts", "src/shapes.ts"]),
        ("go", "scip-go", ["main.go", "shapes/shapes.go"]),
    ],
)
def test_the_reader_reads_what_the_real_indexers_wrote(lang, tool, docs):
    ix = sem.read_scip_file(str(FIX / lang / "index.scip"))
    assert (ix.tool, sorted(ix.docs)) == (tool, docs)
    assert ix.version and ix.root.startswith("file:///")
    defs = {sem.symbol_name(o.symbol) for doc in ix.docs.values() for o in doc.occs if o.roles & enc.DEFINITION}
    assert {"Alpha", "Beta", "Helper" if lang == "go" else "helper", "use", "main"} <= defs
    for doc in ix.docs.values():
        assert all(o.symbol and 0 <= o.line < 30 and 0 <= o.col < 120 for o in doc.occs)


def test_go_index_carries_typed_ranges_and_implementation_relationships():
    shapes = sem.read_scip_file(str(FIX / "go" / "index.scip")).docs["shapes/shapes.go"]
    where = {sem.symbol_name(o.symbol): (o.line, o.col) for o in shapes.occs if o.roles & enc.DEFINITION}
    assert where["Alpha"] == (2, 5) and where["Pinger"] == (14, 5) and where["Helper"] == (10, 5)
    rels = {
        (sem.symbol_name(s.symbol), sem.symbol_name(r.symbol)): r.implementation for s in shapes.syms for r in s.rels
    }
    assert rels == {("Alpha", "Pinger"): True, ("Beta", "Pinger"): True, ("Ping", "Ping"): True}


def test_external_symbols_are_read_and_a_field_of_a_later_version_is_skipped():
    ext = enc.info(enc.sym("os", "path#join()."), kind=26, name="join")
    ix = sem.read_scip(enc.index([enc.doc("a.py")], external=[ext]) + enc.num(9, 1) + enc.field(10, 5, b"\0" * 4))
    assert [(x.symbol, x.kind, x.name) for x in ix.external] == [(enc.sym("os", "path#join()."), 26, "join")]
    assert sorted(ix.docs) == ["a.py"]


# ================================================================ symbols
@pytest.mark.parametrize(
    "symbol, want",
    [
        ("scip-python python fixpy 0.1 `pkg.app`/use().", (("pkg.app", "namespace"), ("use", "method"))),
        (
            "scip-python python fixpy 0.1 `pkg.shapes`/Alpha#ping().",
            (("pkg.shapes", "namespace"), ("Alpha", "type"), ("ping", "method")),
        ),
        (
            "scip-python python fixpy 0.1 `pkg.app`/use().(a)",
            (("pkg.app", "namespace"), ("use", "method"), ("a", "param")),
        ),
        (
            "scip-go gomod example.com/x . `example.com/x`/T#F.",
            (("example.com/x", "namespace"), ("T", "type"), ("F", "term")),
        ),
        (
            "scip-typescript npm . . src/`shapes.ts`/Alpha#",
            (("src", "namespace"), ("shapes.ts", "namespace"), ("Alpha", "type")),
        ),
        (
            "scip-x m  n p 1 a/G#[T]",
            (("a", "namespace"), ("G", "type"), ("T", "tparam")),
        ),  # an escaped space in a field
        ("scip-x m p 1 `a``b`.", (("a`b", "term"),)),  # an escaped backtick in a name
        ("scip-x m p 1 a/m(+1).", (("a", "namespace"), ("m", "method"))),  # a method with a disambiguator
        ("scip-x m p 1 a:", (("a", "meta"),)),
        ("scip-x m p 1 a!", (("a", "macro"),)),
    ],
)
def test_descriptors_follow_the_scip_symbol_grammar(symbol, want):
    assert sem.descriptors(symbol) == want


def test_local_malformed_and_unnamed_symbols():
    assert sem.descriptors("local 12") is None and sem.symbol_name("local 3") is None
    for bad in (
        "",
        "scip-x m",
        "scip-x m p 1 a b",
        "scip-x m p 1 name",
        "scip-x m p 1 `open/",
        "scip-x m p 1 a(.",
        "scip-x m p 1 a(1)x",
    ):
        assert sem.descriptors(bad) == () and sem.symbol_name(bad) is None, bad
    assert sem.symbol_name("scip-x m p 1 a/b#c().") == "c"
    assert sem.symbol_name("scip-x m p 1 a/b#c().(arg)") is None  # a parameter is no graph node
    assert sem.symbol_name("scip-x m p 1 a/") is None  # nor is a namespace


# ================================================================ the package and its stamp
def test_package_files_take_the_family_and_its_manifests_under_the_root():
    tree = {
        "svc/a.py": "1",
        "svc/b.go": "2",
        "svc/pyproject.toml": "3",
        "svc/go.mod": "4",
        "svc/README.md": "5",
        "other/c.py": "6",
        "svcx/d.py": "7",
        "e.py": "8",
        "svc/sub/f.pyi": "9",
    }
    assert sorted(sem.package_files(tree, "svc", "python")) == ["svc/a.py", "svc/pyproject.toml", "svc/sub/f.pyi"]
    assert sorted(sem.package_files(tree, "svc/", "go")) == ["svc/b.go", "svc/go.mod"]
    assert sorted(sem.package_files(tree, "", "python")) == sorted(
        ["e.py", "other/c.py", "svc/a.py", "svc/pyproject.toml", "svc/sub/f.pyi", "svcx/d.py"]
    )
    ts = {"ts/a.ts": "1", "ts/b.tsx": "2", "ts/tsconfig.json": "3", "ts/x.py": "4"}
    assert sem.package_files(ts, "ts", "js") == {"ts/a.ts": "1", "ts/b.tsx": "2", "ts/tsconfig.json": "3"}


def test_package_hash_depends_on_paths_and_blobs_not_on_order():
    a = {"x.py": "1", "y.py": "2"}
    assert sem.package_hash(a) == sem.package_hash({"y.py": "2", "x.py": "1"})
    others = [{"x.py": "1", "y.py": "3"}, {"x.py": "1", "z.py": "2"}, {}]
    assert len({sem.package_hash(a), *map(sem.package_hash, others)}) == 4


def test_stamp_writes_the_sidecar_and_load_meta_reads_it(tmp_path):
    idx = tmp_path / "index.scip"
    idx.write_bytes(enc.index([enc.doc("a.py")]))
    m = sem.stamp(str(idx), {"a.py": "b1", "docs/x.md": "b2"}, "", "python", "scip-python")
    assert sem.load_meta(str(idx)) == m
    assert m.input_hash == sem.package_hash({"a.py": "b1"})
    assert m.index_sha256 == hashlib.sha256(idx.read_bytes()).hexdigest()
    assert json.loads(Path(sem.meta_path(str(idx))).read_text())["schema"] == 1
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".meta-")]  # no temp file is left behind


@pytest.mark.parametrize("body", ["", "not json", '{"schema": 2}', '{"schema": 1, "engine": "x"}', "[]"])
def test_a_missing_or_malformed_sidecar_is_a_sem_error(tmp_path, body):
    idx = tmp_path / "index.scip"
    idx.write_bytes(b"")
    if body:
        Path(sem.meta_path(str(idx))).write_text(body)
    with pytest.raises(sem.SemError):
        sem.load_meta(str(idx))


# ================================================================ the overlay on the real fixtures
def test_py_fixture_scip_python_confirms_the_right_class_and_refutes_nothing(tmp_path):
    g, tree, idx = py_world(tmp_path)
    assert edges_to(g, "Beta.ping") == {("use", "call", "N")} and edges_to(g, "Alpha.ping") == {("use", "call", "N")}
    r = sem.refine(g, tree, [idx], workers=1, text_of=text_in(Path(idx).parent))
    assert [(x.status, x.coverage, x.refuted, x.kept) for x in r.reports] == [("applied", 1.0, 0, 1)]
    assert edges_to(r.graph, "Alpha.ping") == {("use", "call", "S")}
    assert edges_to(r.graph, "Beta.ping") == {
        ("use", "call", "N")
    }  # scip-python resolves unions to one member: no refutation
    (f,) = r.findings
    assert (f.level, f.code) == ("W", "dc.sem-refute") and "union" in f.msg and "pkg/app.py:5" in f.msg
    assert edges_to(r.graph, "unused") == set()  # nothing about a symbol nobody references


def test_py_fixture_the_rest_of_the_chain_refutes_when_the_engine_is_trusted(tmp_path, monkeypatch):
    # the committed scip-python index is complete for this fixture (no union): with the data entry gone the name edge goes
    monkeypatch.delitem(sem.config()["engines"], "scip-python")
    g, tree, idx = py_world(tmp_path)
    r = sem.refine(g, tree, [idx], workers=1, text_of=text_in(Path(idx).parent))
    assert r.findings == () and [(x.status, x.refuted, x.kept) for x in r.reports] == [("applied", 1, 0)]
    assert edges_to(r.graph, "Beta.ping") == set() and edges_to(r.graph, "Alpha.ping") == {("use", "call", "S")}


def test_py_fixture_every_s_edge_names_the_engine_and_a_confirmed_edge_is_replaced_not_doubled(tmp_path):
    g, tree, idx = py_world(tmp_path)
    r = sem.refine(g, tree, [idx], workers=1).graph
    assert s_sources(r) == {"scip-python"} and {r.evid[e.ev].extractor for e in r.edges if e.tier == "S"} == {"sem@1"}
    assert ("use", "call", "S") in edges_to(r, "helper") and ("main", "call", "S") in edges_to(r, "use")
    assert edges_to(r, "helper", "N") == set()  # the import edge was an N edge: SCIP confirmed it
    s_keys = {(e.src, e.dst, e.kind, e.site) for e in r.edges if e.tier == "S"}
    assert not {(e.src, e.dst, e.kind, e.site) for e in r.edges if e.tier != "S"} & s_keys


def test_py_fixture_a_resolved_reference_the_graph_had_no_edge_for_gets_an_s_ref_edge(tmp_path):
    g, tree, idx = py_world(tmp_path)
    assert ("use", "ref", "S") not in edges_to(g, "Alpha")
    r = sem.refine(g, tree, [idx], workers=1)
    assert ("use", "ref", "S") in edges_to(r.graph, "Alpha")  # the annotation `a: Alpha` sits on the line of use()
    assert r.reports[0].added == 1


def test_ts_fixture_the_same_rule_on_the_typescript_index(tmp_path):
    base = copy_fixture(tmp_path, "ts")
    tree = tree_of(base)
    sem.stamp(str(base / "index.scip"), tree, "", "js", "scip-typescript")
    g = link.link(ts_facts(), workers=1).graph
    r = sem.refine(g, tree, [str(base / "index.scip")], workers=1, text_of=text_in(base))
    assert r.findings == () and [(x.status, x.coverage) for x in r.reports] == [("applied", 1.0)]
    assert edges_to(r.graph, "Beta.ping") == set() and edges_to(r.graph, "Alpha.ping") == {("use", "call", "S")}
    assert s_sources(r.graph) == {"scip-typescript"}


def test_go_fixture_a_call_through_an_interface_refutes_nothing_and_relationships_give_s_edges(tmp_path):
    base = copy_fixture(tmp_path, "go")
    tree = tree_of(base)
    sem.stamp(str(base / "index.scip"), tree, "", "go", "scip-go")
    g = link.link(go_facts(), workers=1).graph
    assert edges_to(g, "Alpha.Ping") == {("use", "call", "N")} and edges_to(g, "Pinger.Ping") == {("use", "call", "N")}
    r = sem.refine(g, tree, [str(base / "index.scip")], workers=1)
    assert r.findings == () and [(x.status, x.refuted) for x in r.reports] == [("applied", 0)]
    # p.Ping() resolves to the interface method: any implementation may run, so the name edges to them stay
    assert edges_to(r.graph, "Alpha.Ping", "N") == {("use", "call", "N")}
    assert edges_to(r.graph, "Beta.Ping", "N") == {("use", "call", "N")}
    assert edges_to(r.graph, "Pinger.Ping", "S") == {
        ("use", "call", "S"),
        ("Alpha.Ping", "impl", "S"),
        ("Beta.Ping", "impl", "S"),
    }
    # the relationships: the interface method reaches its implementations, the types implement the interface
    assert edges_to(r.graph, "Alpha.Ping", "S") == {("Pinger.Ping", "override", "S")}
    assert edges_to(r.graph, "Beta.Ping", "S") == {("Pinger.Ping", "override", "S")}
    assert {("Alpha", "impl", "S"), ("Beta", "impl", "S")} <= edges_to(r.graph, "Pinger", "S")
    assert s_sources(r.graph) == {"scip-go"}


# ================================================================ unions, read from what the real indexers wrote
def union_ts_facts():
    """What a TypeScript extractor gives for tests/fixtures/sem/union/ts/src/u.ts."""
    shapes = [d("", "module", 1, 15), d("A", "class", 1, 3), d("A.m", "method", 2), d("B", "class", 5, 7)]
    shapes += [d("B.m", "method", 6), d("use", "func", 9, 11), d("twice", "func", 13, 15)]
    refs = (Ref("x.m", "call", 10, "use"), Ref("x.m", "call", 14, "twice"), Ref("y.m", "call", 14, "twice"))
    return {"src/u.ts": Facts("typescript", defs=tuple(shapes), refs=refs)}


def union_py_facts():
    """What a Python extractor gives for tests/fixtures/sem/union/py/u.py."""
    defs = [d("", "module", 1, 23), d("A", "class", 4, 6), d("A.m", "method", 5, 6), d("B", "class", 9, 11)]
    defs += [
        d("B.m", "method", 10, 11),
        d("use", "func", 14, 15),
        d("use2", "func", 18, 19),
        d("twice", "func", 22, 23),
    ]
    refs = (Ref("x.m", "call", 15, "use"), Ref("x.m", "call", 19, "use2"), Ref("x.m", "call", 23, "twice"))
    refs += (Ref("y.m", "call", 23, "twice"),)
    return {"u.py": Facts("python", defs=tuple(defs), refs=refs)}


def union_world(tmp_path, lang):
    base = copy_fixture(tmp_path, "union/" + lang)
    py = lang == "py"
    facts = union_py_facts() if py else union_ts_facts()
    tree = tree_of(base)
    sem.stamp(str(base / "index.scip"), tree, "", "python" if py else "js", "scip-python" if py else "scip-typescript")
    return link.link(facts, workers=1).graph, tree, str(base / "index.scip"), text_in(base)


def test_union_py_scip_python_resolves_a_member_access_on_a_union_to_the_first_member_only(tmp_path):
    base = copy_fixture(tmp_path, "union/py")
    ix = sem.read_scip_file(str(base / "index.scip"))
    where = {}  # line of u.py -> the classes whose `m` the index resolved there
    for o in ix.docs["u.py"].occs:
        if sem.symbol_name(o.symbol) == "m" and not o.roles & enc.DEFINITION:
            where.setdefault(o.line + 1, []).append(sem.descriptors(o.symbol)[-2][0])
    assert where == {
        15: ["A"],
        19: ["A"],
        23: ["A"],
    }, "B.m is in no occurrence: x.m() on x: Union[A, B], x: A | B and y.m()"
    assert sem.config()["engines"]["scip-python"]["no_refute"]  # the reason the data gives for it


def test_union_py_the_overlay_keeps_every_name_edge_to_the_member_the_index_left_out(tmp_path):
    g, tree, idx, text_of = union_world(tmp_path, "py")
    assert {("use", "call", "N"), ("use2", "call", "N"), ("twice", "call", "N")} <= edges_to(g, "B.m")
    r = sem.refine(g, tree, [idx], workers=1, text_of=text_of)
    assert edges_to(r.graph, "B.m") == edges_to(g, "B.m")  # B.m keeps its three callers
    assert {("use", "call", "S"), ("use2", "call", "S"), ("twice", "call", "S")} <= edges_to(r.graph, "A.m")
    assert (r.reports[0].refuted, r.reports[0].kept) == (
        0,
        4,
    )  # u.py:23 has two refs (x.m, y.m): two edges, one evidence each
    (f,) = r.findings
    assert f.code == "dc.sem-refute" and "union" in f.msg and "4 name edges kept" in f.msg


def test_union_py_without_the_engine_entry_the_member_would_lose_its_callers(tmp_path, monkeypatch):
    # the hazard the data entry guards against: a refutation that is false on a union (every line holds one site of `m`)
    monkeypatch.delitem(sem.config()["engines"], "scip-python")
    g, tree, idx, text_of = union_world(tmp_path, "py")
    r = sem.refine(g, tree, [idx], workers=1, text_of=text_of)
    assert ("use", "call", "N") not in edges_to(r.graph, "B.m") and r.reports[0].refuted >= 2
    assert ("twice", "call", "N") in edges_to(r.graph, "B.m")  # x.m() + y.m(): two sites, one resolved


def test_union_ts_scip_typescript_resolves_both_members_at_one_position(tmp_path):
    ix = sem.read_scip_file(str(copy_fixture(tmp_path, "union/ts") / "index.scip"))
    at = {}  # (line, column) -> the classes whose `m` the index resolved there
    for o in ix.docs["src/u.ts"].occs:
        if sem.symbol_name(o.symbol) == "m" and not o.roles & enc.DEFINITION:
            at.setdefault((o.line + 1, o.col), []).append(sem.descriptors(o.symbol)[-2][0])
    assert at == {(10, 11): ["A", "B"], (14, 11): ["A"]}  # y: any has no occurrence at all


def test_union_ts_both_members_are_confirmed_and_a_second_site_on_the_line_keeps_the_name_edge(tmp_path):
    g, tree, idx, text_of = union_world(tmp_path, "ts")
    r = sem.refine(g, tree, [idx], workers=1, text_of=text_of)
    assert edges_to(r.graph, "A.m") == {("use", "call", "S"), ("twice", "call", "S")}
    # use(x: A | B): both resolved, S. twice(x: A, y: any): `x.m() + y.m()` is two sites, the index resolved one
    assert edges_to(r.graph, "B.m") == {("use", "call", "S"), ("twice", "call", "N")}
    assert (r.reports[0].refuted, r.reports[0].kept) == (0, 2)  # x.m and y.m at line 14: two edges to B.m
    (f,) = r.findings
    assert f.code == "dc.sem-refute" and "src/u.ts:14" in f.msg and "more sites" in f.msg


def test_three_languages_in_one_run_one_unit_each_and_the_same_bytes_for_any_worker_count(tmp_path):
    files, tree, idxs = {}, {}, []
    for lang, family, engine, facts in (
        ("py", "python", "scip-python", lambda b: py_facts(b, "py")),
        ("ts", "js", "scip-typescript", lambda b: ts_facts("ts")),
        ("go", "go", "scip-go", lambda b: go_facts("go")),
    ):
        base = copy_fixture(tmp_path, lang)
        files.update(facts(base))
        tree.update(tree_of(base, lang))
        sem.stamp(str(base / "index.scip"), tree, lang, family, engine)  # the package root is the subdirectory
        idxs.append(str(base / "index.scip"))
    g = link.link(files, workers=1).graph
    digests, text_of = (
        set(),
        text_in(tmp_path),
    )  # the tree paths are lang/...: the fixtures were copied to tmp_path/lang
    for workers, order in ((1, idxs), (2, idxs[::-1]), (8, idxs)):
        r = sem.refine(g, tree, order, workers=workers, text_of=text_of)
        digests.add(r.graph.digest())
        assert [f.code for f in r.findings] == ["dc.sem-refute"]  # scip-python: a union may hide the other member
        assert sorted(x.engine for x in r.reports) == ["scip-go", "scip-python", "scip-typescript"]
        assert {x.status for x in r.reports} == {"applied"}
    assert len(digests) == 1 and g.digest() not in digests
    final = sem.refine(g, tree, idxs, workers=1, text_of=text_of).graph
    assert s_sources(final) == {"scip-python", "scip-typescript", "scip-go"}
    assert {e.dst.partition(":")[0] for e in final.edges if e.dst.endswith("Beta.ping")} == {"python"}  # ts refuted it


# ================================================================ the overlay: refutation rules
def test_refutation_needs_a_name_that_resolved_somewhere_else(tmp_path):
    t = twin()
    r = refine_twin(tmp_path, t, twin_index("Foo#m()."))
    assert edges_to(r.graph, "Bar.m") == set() and edges_to(r.graph, "Foo.m") == {("use", "call", "S")}
    assert (r.reports[0].refuted, r.reports[0].confirmed, r.reports[0].kept) == (1, 1, 0) and r.findings == ()


def test_an_unresolved_line_never_refutes(tmp_path):
    # SCIP has no occurrence for x.m() (an untyped receiver), or only one of another name: absence is not evidence
    t = twin()
    for n, refs in enumerate(([], [enc.occ(5, 11, sy("other()."), enc.READ)])):
        r = refine_twin(tmp_path, t, twin_index(None, refs=refs), name=f"i{n}.scip")
        assert r.graph.edges == t.graph.edges and r.reports[0].refuted == 0 and r.reports[0].status == "applied"


def test_a_resolution_on_another_line_does_not_count(tmp_path):
    t = twin()
    r = refine_twin(tmp_path, t, twin_index("Foo#m().", call_line=3))
    assert edges_to(r.graph, "Bar.m") == {("use", "call", "N")}


def test_only_name_edges_are_refuted_a_t_edge_a_d_edge_and_another_kind_stay(tmp_path):
    t = twin()
    extra = [
        mk_edge(t.use, t.bar_m, tier="T", line=6),
        mk_edge(t.use, t.bar_m, kind="reads", line=6),
        mk_edge(t.use, t.bar_m, tier="D", line=6),
    ]
    g = md.canonical([t.file, t.foo, t.foo_m, t.bar, t.bar_m, t.use], [mk_edge(t.use, t.foo_m, line=6), *extra])
    idx, tree = stamped(tmp_path, g, twin_index("Foo#m()."))
    r = sem.refine(g, tree, [idx], workers=1)
    assert edges_to(r.graph, "Bar.m") == {("use", "call", "T"), ("use", "reads", "N"), ("use", "call", "D")}
    assert r.reports[0].refuted == 0


def test_a_self_edge_is_never_refuted(tmp_path):
    t = twin()
    g = md.canonical(
        [t.file, t.foo, t.foo_m, t.bar, t.bar_m, t.use],
        [mk_edge(t.foo_m, t.foo_m, line=6), mk_edge(t.use, t.foo_m, line=6)],
    )
    idx, tree = stamped(tmp_path, g, twin_index("Bar#m()."))
    assert ("Foo.m", "call", "N") in edges_to(sem.refine(g, tree, [idx], workers=1).graph, "Foo.m")


# ================================================================ refutation: the line must hold one site of the name
CALLS_ON_ONE_LINE = [
    "    return x.m() + y.m()",
    "    a = x.m(); b = y.m()",
    "    return x.m() if c else y.m()",
    "    return [x.m(), y.m(), 1]",
]


@pytest.mark.parametrize("call", CALLS_ON_ONE_LINE)
def test_two_sites_of_the_name_on_one_line_keep_the_name_edge_when_the_index_resolved_one(tmp_path, call):
    # x is typed Foo, y is untyped: the index has ONE occurrence of m on the line, the same bytes as for `return x.m()`
    t = twin()
    r = refine_twin(tmp_path, t, twin_index("Foo#m()."), text_of=twin_text(call=call))
    assert edges_to(r.graph, "Bar.m") == {("use", "call", "N")} and edges_to(r.graph, "Foo.m") == {("use", "call", "S")}
    assert (r.reports[0].refuted, r.reports[0].kept) == (0, 1)
    (f,) = r.findings
    assert (f.level, f.code) == ("W", "dc.sem-refute") and "m.py:6" in f.msg and "more sites" in f.msg and f.why


def test_the_same_index_bytes_refute_when_the_text_shows_one_site(tmp_path):
    t = twin()
    r = refine_twin(tmp_path, t, twin_index("Foo#m()."), text_of=twin_text(call="    return x.m()"))
    assert edges_to(r.graph, "Bar.m") == set() and (r.reports[0].refuted, r.reports[0].kept) == (1, 0)


def test_every_site_of_the_name_on_the_line_resolved_refutes(tmp_path):
    t = twin()
    both = [enc.occ(5, 20, sy("Foo#m()."), enc.READ)]  # a second occurrence, at another column
    r = refine_twin(tmp_path, t, twin_index("Foo#m().", refs=both), text_of=twin_text(call="    return x.m() + y.m()"))
    assert edges_to(r.graph, "Bar.m") == set() and (r.reports[0].refuted, r.reports[0].kept) == (1, 0)
    assert r.findings == ()


@pytest.mark.parametrize("call, refuted", [("    return x.m()", 1), ("    return x.m() + y.m()", 0)])
def test_a_union_resolved_to_two_members_at_one_position_is_one_site_not_two(tmp_path, call, refuted):
    # Foo | Baz at x.m: two occurrences at the same column. They must not pay for the unresolved y.m() as well
    p = "m.py"
    foo, baz, bar = (
        mk_node(p, "Foo", "class", 1, 2),
        mk_node(p, "Baz", "class", 5, 6),
        mk_node(p, "Bar", "class", 3, 4),
    )
    fm, zm, bm = (mk_node(p, "Foo.m", "method", 2), mk_node(p, "Baz.m", "method", 6), mk_node(p, "Bar.m", "method", 4))
    use = mk_node(p, "use", "func", 7, 8)
    g = md.canonical([mk_file(p), foo, fm, bar, bm, baz, zm, use], [mk_edge(use, n, line=8) for n in (fm, bm, zm)])
    occs = [enc.occ(i, 6, sy(s), enc.DEFINITION) for i, s in ((0, "Foo#"), (2, "Bar#"), (4, "Baz#"))]
    occs += [enc.occ(i, 8, sy(s), enc.DEFINITION) for i, s in ((1, "Foo#m()."), (3, "Bar#m()."), (5, "Baz#m()."))]
    occs += [enc.occ(6, 4, sy("use()."), enc.DEFINITION)]
    occs += [enc.occ(7, 11, sy("Foo#m()."), enc.READ), enc.occ(7, 11, sy("Baz#m()."), enc.READ)]
    idx, tree = stamped(tmp_path, g, enc.index([enc.doc(p, occs)]))
    text = "class Foo:\n  def m(self): pass\nclass Bar:\n  def m(self): pass\nclass Baz:\n  def m(self): pass\n"
    text += f"def use(x, y):\n{call}\n"
    r = sem.refine(g, tree, [idx], workers=1, text_of={p: text}.get)
    assert edges_to(r.graph, "Foo.m") == {("use", "call", "S")} == edges_to(r.graph, "Baz.m")
    assert (edges_to(r.graph, "Bar.m") == set()) == (refuted == 1) and (r.reports[0].refuted == refuted)


def test_without_the_text_nothing_is_refuted_and_the_finding_says_why(tmp_path):
    t = twin()
    idx, tree = stamped(tmp_path, t.graph, twin_index("Foo#m()."))
    r = sem.refine(t.graph, tree, [idx], workers=1)
    assert edges_to(r.graph, "Bar.m") == {("use", "call", "N")} and edges_to(r.graph, "Foo.m") == {("use", "call", "S")}
    assert (r.reports[0].refuted, r.reports[0].kept) == (0, 1)
    (f,) = r.findings
    assert f.code == "dc.sem-refute" and "no text of the line was given" in f.msg


def gone(path):
    raise OSError("gone")


@pytest.mark.parametrize(
    "text_of",
    [
        lambda p: None,
        lambda p: "",
        gone,
        lambda p: "one line\n",  # the site line is beyond the text
        twin_text(call="    return x.n()"),  # the name is not on the line (an alias, a rename)
        twin_text(call="    return x.mm() + m_ + _m"),  # only parts of longer identifiers
    ],
    ids=["none", "empty", "oserror", "short", "other-name", "longer-identifiers"],
)
def test_a_line_the_text_does_not_show_keeps_the_name_edge(tmp_path, text_of):
    t = twin()
    r = refine_twin(tmp_path, t, twin_index("Foo#m()."), text_of=text_of)
    assert edges_to(r.graph, "Bar.m") == {("use", "call", "N")} and (r.reports[0].refuted, r.reports[0].kept) == (0, 1)
    assert [f.code for f in r.findings] == ["dc.sem-refute"]


def test_an_engine_that_drops_union_members_confirms_but_never_refutes(tmp_path):
    t = twin()
    idx, tree = stamped(tmp_path, t.graph, twin_index("Foo#m()."), engine="scip-python")
    r = sem.refine(t.graph, tree, [idx], workers=1, text_of=twin_text())
    assert edges_to(r.graph, "Bar.m") == {("use", "call", "N")} and edges_to(r.graph, "Foo.m") == {("use", "call", "S")}
    assert (r.reports[0].refuted, r.reports[0].kept) == (0, 1)
    assert sem.config()["engines"]["scip-python"]["no_refute"][:40] in r.findings[0].msg
    assert sem._no_refute("scip-typescript") == sem._no_refute("scip-go") == ""


@pytest.mark.parametrize("flag", [md.F_ABSTRACT, md.F_PROTOCOL])
def test_a_resolution_to_an_abstract_or_protocol_member_refutes_nothing(tmp_path, flag):
    t = twin(foo_m_flags=flag)
    r = refine_twin(tmp_path, t, twin_index("Foo#m()."))
    assert edges_to(r.graph, "Bar.m") == {("use", "call", "N")}, "any implementation may answer a call through the base"
    assert edges_to(r.graph, "Foo.m") == {("use", "call", "S")}


def test_a_member_of_an_interface_blocks_like_an_abstract_one(tmp_path):
    t = twin(foo_kind="iface")
    r = refine_twin(tmp_path, t, twin_index("Foo#m()."))
    assert edges_to(r.graph, "Bar.m") == {("use", "call", "N")}


FOREIGN = [
    enc.sym("typing", "Proto#m().", pkg="ext 9.9"),  # an external interface or protocol (io.Reader, ast.Visitor, ...)
    enc.sym("m", "Base#m()."),  # a base this index does not define: another package, another index of a monorepo
]


@pytest.mark.parametrize("foreign", FOREIGN, ids=["external", "another-package"])
def test_a_resolution_to_a_symbol_the_package_does_not_define_says_nothing_either_way(tmp_path, foreign):
    # x.m() resolved to a member of an interface that lives outside the package: any implementation of it may run, Foo.m
    # and Bar.m alike, and a type that implements it through this call has no other caller. Coverage is 1.0, the text
    # shows one site: nothing but the ownership of the symbol is missing
    t = twin()
    r = refine_twin(tmp_path, t, twin_index(None, refs=[enc.occ(5, 11, foreign, enc.READ)]))
    assert r.graph.edges == t.graph.edges and r.findings == ()
    assert (r.reports[0].status, r.reports[0].coverage) == ("applied", 1.0)
    assert (r.reports[0].refuted, r.reports[0].kept, r.reports[0].confirmed) == (0, 0, 0)


def test_one_foreign_symbol_of_the_name_among_concrete_ones_keeps_the_name_edge(tmp_path):
    # `x.m() + y.m()`: x is a Foo, y is an external Proto. Every resolved symbol must be concrete code of the package
    t = twin()
    refs = [enc.occ(5, 20, FOREIGN[0], enc.READ)]
    text = twin_text(call="    return x.m() + y.m()")
    r = refine_twin(tmp_path, t, twin_index("Foo#m().", refs=refs), text_of=text)
    assert edges_to(r.graph, "Bar.m") == {("use", "call", "N")} and edges_to(r.graph, "Foo.m") == {("use", "call", "S")}
    assert (r.reports[0].refuted, r.reports[0].confirmed) == (0, 1)
    # the control: the same line with the second site resolved to a concrete symbol of the package refutes
    both = [enc.occ(5, 20, sy("Foo#m()."), enc.READ)]
    r = refine_twin(tmp_path, t, twin_index("Foo#m().", refs=both), text_of=text, name="control.scip")
    assert edges_to(r.graph, "Bar.m") == set() and r.reports[0].refuted == 1


@pytest.mark.parametrize(
    "missing, status, refuted", [(0, "applied", 1), (1, "applied", 1), (2, "partial", 0), (30, "partial", 0)]
)
def test_coverage_is_the_share_of_definitions_the_index_defines_and_0_99_is_enough(tmp_path, missing, status, refuted):
    t = twin(fillers=95)  # 5 + 95 = 100 definitions (the file node is none)
    r = refine_twin(tmp_path, t, twin_index("Foo#m().", fillers=95, missing=missing))
    (rep,) = r.reports
    assert (rep.status, rep.refuted, rep.coverage) == (status, refuted, round((100 - missing) / 100, 4))
    assert edges_to(r.graph, "Foo.m") == {("use", "call", "S")}  # a resolved reference is true at any coverage
    assert (edges_to(r.graph, "Bar.m") == set()) == (refuted == 1)
    assert [f.code for f in r.findings] == ([] if status == "applied" else ["dc.sem-coverage"])
    assert all(f.level == "W" and f.why for f in r.findings)


def test_the_minimum_coverage_is_data_and_a_caller_can_ask_for_more(tmp_path):
    assert sem.config()["min_coverage"] == 0.99
    t = twin(fillers=95)
    r = refine_twin(tmp_path, t, twin_index("Foo#m().", fillers=95, missing=1), min_coverage=1.0)
    assert r.reports[0].status == "partial"


def test_a_node_with_absurd_lines_is_ignored_by_the_owner_lookup():
    huge = mk_node("m.py", "huge", "func", 1, 10**9)
    small = mk_node("m.py", "small", "func", 2, 3)
    own = sem._Owner([mk_file("m.py"), huge, small])
    assert own.at("m.py", 2) == small.key and own.at("m.py", 5) == "python:m.py" and own.at("zz.py", 1) is None


def test_a_package_with_no_definitions_has_full_coverage_and_nothing_to_refute(tmp_path):
    g = md.canonical([mk_file("m.py")], [])
    idx, tree = stamped(tmp_path, g, enc.index([enc.doc("m.py")]))
    r = sem.refine(g, tree, [idx], workers=1)
    assert r.reports[0].coverage == 1.0 and r.graph.edges == ()


def test_confirmation_beats_refutation_and_the_order_of_the_indexes_does_not_matter(tmp_path):
    t = twin()
    a, tree = stamped(tmp_path, t.graph, twin_index("Foo#m().", tool="tool-a"), name="a.scip")
    b, _ = stamped(tmp_path, t.graph, twin_index("Bar#m().", tool="tool-b"), name="b.scip", tree=tree)
    r1, r2 = (sem.refine(t.graph, tree, order, workers=1, text_of=twin_text()) for order in ([a, b], [b, a]))
    assert r1.graph.digest() == r2.graph.digest()
    by_target = {qual(e.dst): (e.tier, r1.graph.evid[e.ev].source) for e in r1.graph.edges}
    assert by_target == {"Foo.m": ("S", "tool-a"), "Bar.m": ("S", "tool-b")}


def test_a_local_symbol_has_no_name_it_binds_to_the_one_node_that_starts_on_its_line(tmp_path):
    outer, inner = mk_node("m.py", "outer", "func", 1, 4), mk_node("m.py", "outer.inner", "func", 2, 3)
    other = mk_node("m.py", "inner", "func", 6)  # a top-level function of the same name, elsewhere
    g = md.canonical(
        [mk_file("m.py"), outer, inner, other], [mk_edge(outer, inner, line=4), mk_edge(outer, other, line=4)]
    )
    occs = [enc.occ(0, 4, sy("outer()."), enc.DEFINITION), enc.occ(1, 8, "local 3", enc.DEFINITION)]
    occs += [enc.occ(3, 11, "local 3", enc.READ), enc.occ(5, 4, sy("inner()."), enc.DEFINITION)]
    idx, tree = stamped(tmp_path, g, enc.index([enc.doc("m.py", occs)]))
    text = "def outer():\n    def inner():\n        pass\n    return inner()\n\ndef inner(): pass\n"
    r = sem.refine(g, tree, [idx], workers=1, text_of={"m.py": text}.get)
    assert edges_to(r.graph, "outer.inner") == {("outer", "call", "S")}  # confirmed through the local symbol
    assert (
        edges_to(r.graph, "inner") == set()
    )  # the call on line 4 is local 3, so the edge to the other `inner` is refuted
    assert (r.reports[0].coverage, r.reports[0].confirmed, r.reports[0].refuted) == (1.0, 1, 1)


def test_a_symbol_defined_on_two_nodes_is_bound_to_neither(tmp_path):
    t = twin()
    occs = [enc.occ(1, 8, sy("Foo#m()."), enc.DEFINITION), enc.occ(3, 8, sy("Foo#m()."), enc.DEFINITION)]
    occs += [enc.occ(5, 11, sy("Foo#m()."), enc.READ)]
    r = refine_twin(tmp_path, t, enc.index([enc.doc("m.py", occs)]))
    assert r.graph.edges == t.graph.edges  # no judgement about a symbol the index defines twice
    assert (r.reports[0].coverage, r.reports[0].status) == (0.0, "partial")


def test_an_external_symbol_and_a_self_reference_add_no_edge(tmp_path):
    t = twin()
    occs = [enc.occ(4, 4, sy("use()."), enc.DEFINITION), enc.occ(1, 8, sy("Foo#m()."), enc.DEFINITION)]
    occs += [enc.occ(3, 8, sy("Bar#m()."), enc.DEFINITION)]
    occs += [enc.occ(5, 11, "scip-fake py ext 9.9 os/path#join().", enc.READ), enc.occ(5, 20, sy("use()."), enc.READ)]
    r = refine_twin(tmp_path, t, enc.index([enc.doc("m.py", occs)]))
    assert r.reports[0].added == 0 and not [e for e in r.graph.edges if e.src == e.dst]


def test_a_new_s_edge_comes_from_the_innermost_node_that_holds_the_line_and_only_once(tmp_path):
    # a call the extractors did not see: line 6 inside use() references Bar.m, twice, and the graph has no edge at all
    t = twin()
    g = md.canonical([t.file, t.foo, t.foo_m, t.bar, t.bar_m, t.use], [])
    refs = [enc.occ(5, 11, sy("Bar#m()."), enc.READ), enc.occ(5, 30, sy("Bar#m()."), enc.READ)]
    idx, tree = stamped(tmp_path, g, twin_index(None, refs=refs))
    r = sem.refine(g, tree, [idx], workers=1)
    (e,) = r.graph.edges
    assert (qual(e.src), qual(e.dst), e.kind, e.tier, e.site) == ("use", "Bar.m", "ref", "S", "m.py:6")
    assert r.graph.evid[e.ev].source == "scip-fake" and r.reports[0].added == 1


def test_relationships_give_impl_and_override_s_edges_once_and_keep_existing_ones(tmp_path):
    base, impl = mk_node("m.py", "Base", "class", 1, 2), mk_node("m.py", "Impl", "class", 3, 4)
    bm, im = mk_node("m.py", "Base.m", "method", 2), mk_node("m.py", "Impl.m", "method", 4)
    g = md.canonical([mk_file("m.py"), base, bm, impl, im], [md.edge(im.key, bm.key, "impl", "T", "m.py:4")])
    infos = [
        enc.info(sy("Impl#"), rels=[enc.rel(sy("Base#"))]),
        enc.info(sy("Impl#m()."), rels=[enc.rel(sy("Base#m()."))]),
    ]
    occs = [
        enc.occ(i, 6, sy(s), enc.DEFINITION)
        for i, s in ((0, "Base#"), (1, "Base#m()."), (2, "Impl#"), (3, "Impl#m()."))
    ]
    idx, tree = stamped(tmp_path, g, enc.index([enc.doc("m.py", occs, infos)]))
    r = sem.refine(g, tree, [idx], workers=1)
    s = {(qual(e.src), qual(e.dst), e.kind) for e in r.graph.edges if e.tier == "S"}
    assert s == {("Impl", "Base", "impl"), ("Base.m", "Impl.m", "override")}
    assert (qual(im.key), qual(bm.key), "impl") in {
        (qual(e.src), qual(e.dst), e.kind) for e in r.graph.edges if e.tier == "T"
    }


def test_a_relationship_to_the_outside_to_itself_or_that_is_no_implementation_adds_nothing(tmp_path):
    t = twin()
    rels = [enc.rel("scip-fake py ext 1 os/Base#"), enc.rel(sy("Foo#")), enc.rel(sy("Bar#"), implementation=False)]
    occs = [enc.occ(0, 6, sy("Foo#"), enc.DEFINITION), enc.occ(2, 6, sy("Bar#"), enc.DEFINITION)]
    r = refine_twin(tmp_path, t, enc.index([enc.doc("m.py", occs, [enc.info(sy("Foo#"), rels=rels)])]))
    assert r.reports[0].added == 0 and r.graph is t.graph


def test_the_package_root_prefixes_the_paths_of_the_index(tmp_path):
    t = twin("svc")
    idx, tree = stamped(tmp_path, t.graph, twin_index("Foo#m()."), root="svc")
    r = sem.refine(t.graph, tree, [idx], workers=1, text_of=twin_text("svc"))
    assert edges_to(r.graph, "Bar.m") == set() and r.reports[0].status == "applied"


def test_an_index_of_files_the_graph_does_not_know_changes_nothing(tmp_path):
    t = twin()
    other = enc.index([enc.doc("zzz.py", [enc.occ(0, 0, sy("Foo#m()."), enc.READ)])])
    r = refine_twin(tmp_path, t, other)
    assert r.graph.edges == t.graph.edges and r.reports[0].added == 0


# ================================================================ stale, foreign and broken indexes
def test_an_overlay_whose_input_hash_is_not_the_current_package_hash_is_ignored(tmp_path):
    t = twin()
    idx, tree = stamped(tmp_path, t.graph, twin_index("Foo#m()."))
    assert sem.refine(t.graph, tree, [idx], workers=1).reports[0].status == "applied"
    r = sem.refine(
        t.graph, {**tree, "m.py": "a-newer-blob"}, [idx], workers=1
    )  # the file changed after the indexer ran
    assert r.graph is t.graph and [(x.status, x.detail) for x in r.reports] == [("stale", "input hash differs")]
    assert [(f.level, f.code) for f in r.findings] == [("W", "dc.sem-stale")]
    assert sem.refine(t.graph, {**tree, "extra.py": "x"}, [idx], workers=1).reports[0].status == "stale"  # a file added
    assert sem.refine(t.graph, {}, [idx], workers=1).reports[0].status == "stale"  # the files are gone
    assert sem.refine(t.graph, {**tree, "notes.md": "x"}, [idx], workers=1).reports[0].status == "applied"  # no input


def test_covered_names_the_paths_of_the_packages_whose_overlay_was_applied_in_full(tmp_path):
    t = twin(fillers=95)
    full = refine_twin(tmp_path, t, twin_index("Foo#m().", fillers=95), name="full.scip")
    partial = refine_twin(tmp_path, t, twin_index("Foo#m().", fillers=95, missing=5), name="partial.scip")
    stale = sem.refine(t.graph, {}, [str(tmp_path / "full.scip")], workers=1)
    assert full.covered == {"m.py"} and partial.covered == frozenset() and stale.covered == frozenset()


def test_a_changed_manifest_makes_the_overlay_stale(tmp_path):
    t = twin()
    tree = {"m.py": "b1", "pyproject.toml": "t1"}
    idx, _ = stamped(tmp_path, t.graph, twin_index("Foo#m()."), tree=tree)
    assert sem.refine(t.graph, tree, [idx], workers=1).reports[0].status == "applied"
    assert sem.refine(t.graph, {**tree, "pyproject.toml": "t2"}, [idx], workers=1).reports[0].status == "stale"


def test_an_index_replaced_after_it_was_stamped_is_refused(tmp_path):
    t = twin()
    idx, tree = stamped(tmp_path, t.graph, twin_index("Foo#m()."))
    Path(idx).write_bytes(twin_index("Bar#m()."))  # an older or foreign index.scip under the same stamp
    r = sem.refine(t.graph, tree, [idx], workers=1)
    assert r.graph is t.graph and r.reports[0].status == "invalid"
    assert "not the index that was stamped" in r.reports[0].detail and [f.code for f in r.findings] == ["dc.sem-index"]


def test_garbage_that_was_stamped_and_a_missing_stamp_are_findings_not_crashes(tmp_path):
    t = twin()
    idx, tree = stamped(tmp_path, t.graph, b"\xff" * 12)
    r = sem.refine(t.graph, tree, [idx], workers=1)
    assert r.reports[0].status == "invalid" and [f.code for f in r.findings] == ["dc.sem-index"] and r.graph is t.graph
    bare = tmp_path / "bare.scip"
    bare.write_bytes(twin_index("Foo#m()."))
    r = sem.refine(t.graph, tree, [str(bare), str(tmp_path / "absent.scip")], workers=1)
    assert [x.status for x in r.reports] == ["invalid", "invalid"] and [f.code for f in r.findings] == [
        "dc.sem-index"
    ] * 2


def test_no_indexes_is_the_input_graph():
    t = twin()
    r = sem.refine(t.graph, {}, [], workers=1)
    assert r.graph is t.graph and r.findings == () and r.reports == ()


def test_the_overlay_is_deterministic_and_leaves_what_it_does_not_touch(tmp_path):
    g, tree, idx = py_world(tmp_path)
    runs = {sem.refine(g, tree, [idx, idx], workers=w).graph.digest() for w in (1, 2) for _ in range(2)}
    assert len(runs) == 1
    r = sem.refine(g, tree, [idx], workers=1).graph
    assert r.nodes == g.nodes and r.roots == g.roots and set(g.evid) <= set(r.evid)
    assert {e for e in g.edges if e.tier == "D"} <= set(r.edges)


# ================================================================ the LSP client against the scripted server
def lsp_script(tmp_path, name="s", **kw):
    log = tmp_path / f"{name}.log"
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({"log": str(log), **kw}))
    return [sys.executable, str(FAKE_LSP), str(p)], log


def log_rows(log: Path):
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def asked(log: Path, method="textDocument/references"):
    return [x for x in log_rows(log) if x.get("m") == method]


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    return w


def test_session_handshake_references_and_the_end_of_the_session(tmp_path, ws):
    refs = {"a.py:4:4": [["a.py", 1], ["b.py", 2], ["/elsewhere/x.py", 9], ["a.py", 4]]}
    argv, log = lsp_script(tmp_path, refs=refs)
    with sem.Lsp(argv, str(ws), time.monotonic() + 20) as c:
        assert c.caps == {"referencesProvider": True}
        assert c.settle(1.0) is True and c.indexed
        c.open("a.py", "python", "x")
        c.open("a.py", "python", "x")  # the same file is opened once
        got = c.references("a.py", 4, 4)
        assert c.references("a.py", 0, 0) == []
    assert got == [("a.py", 1), ("a.py", 4), ("b.py", 2)]  # a location outside the root is dropped, the rest sorted
    assert c.proc.poll() is not None  # the server is gone
    methods = [r["m"] for r in log_rows(log) if "m" in r]
    assert methods.count("textDocument/didOpen") == 1
    assert methods[:2] == ["initialize", "initialized"] and methods[-2:] == ["shutdown", "exit"]


def test_initialization_options_reach_the_server(tmp_path, ws):
    argv, log = lsp_script(tmp_path)
    opts = {"tsserver": {"path": "/engines/typescript/lib/tsserver.js"}}
    with sem.Lsp(argv, str(ws), time.monotonic() + 20, init=opts):
        pass
    (first,) = [r for r in log_rows(log) if r.get("m") == "initialize"]
    assert first["opts"] == opts


def test_the_client_answers_the_requests_of_the_server(tmp_path, ws):
    argv, log = lsp_script(tmp_path)
    with sem.Lsp(argv, str(ws), time.monotonic() + 20) as c:
        c.settle(1.0)
        c.references("a.py", 0, 0)  # a round trip: the server has read our answers by now
    replies = {r["reply_to"]: r["result"] for r in log_rows(log) if "reply_to" in r}
    assert replies == {"window/workDoneProgress/create": None, "workspace/configuration": [None, None]}


def test_indexing_is_known_only_when_the_server_announced_it_and_finished(tmp_path, ws):
    done = {}
    for mode in ("finish", "none", "stuck"):
        argv, _ = lsp_script(tmp_path, mode, progress=mode)
        with sem.Lsp(argv, str(ws), time.monotonic() + (0.8 if mode == "stuck" else 20)) as c:
            done[mode] = (c.settle(0.2), c.progress, sorted(c.tokens))
    assert done == {"finish": (True, True, []), "none": (False, False, []), "stuck": (False, True, ["idx"])}


def test_call_hierarchy_is_used_where_the_server_has_it(tmp_path, ws):
    argv, _ = lsp_script(tmp_path, caps={"callHierarchyProvider": True}, calls={"a.py:4:4": [["a.py", 1]]})
    with sem.Lsp(argv, str(ws), time.monotonic() + 20) as c:
        assert not c.caps.get("referencesProvider") and c.caps["callHierarchyProvider"]
        assert c.incoming_calls("a.py", 4, 4) == [("a.py", 1)]
        assert c.incoming_calls("a.py", 0, 0) == []


def test_a_server_that_dies_or_never_starts_is_an_lsp_error(tmp_path, ws):
    argv, _ = lsp_script(tmp_path, crash_after=1, refs={"a.py:4:4": [["b.py", 2]]})
    with sem.Lsp(argv, str(ws), time.monotonic() + 20) as c:
        assert c.references("a.py", 4, 4) == [("b.py", 2)]
        with pytest.raises(sem.LspError):
            c.references("a.py", 4, 4)
    with pytest.raises(sem.LspError, match="closed the channel|gone"):
        with sem.Lsp([sys.executable, "-c", "pass"], str(ws), time.monotonic() + 20):
            pytest.fail("a server that exits at once has no session")
    with pytest.raises(FileNotFoundError):
        sem.Lsp([str(tmp_path / "no-such-server")], str(ws), time.monotonic() + 20)


def test_a_frame_without_a_length_ends_the_session(tmp_path, ws):
    script = "import sys, time; sys.stdout.write('Garbage: 1\\r\\n\\r\\n'); sys.stdout.flush(); time.sleep(30)"
    t0 = time.monotonic()
    with pytest.raises(sem.LspError, match="closed the channel"):
        with sem.Lsp([sys.executable, "-c", script], str(ws), t0 + 20):
            pytest.fail("a server that talks garbage has no session")
    assert time.monotonic() - t0 < 15


WRONG_FRAMES = [
    ('{"jsonrpc":"2.0","method":"$/progress","params":[1]}', "params that are no object"),
    (
        '{"jsonrpc":"2.0","method":"$/progress","params":{"token":1,"value":"begin"}}',
        "a progress value that is no object",
    ),
    ('{"jsonrpc":"2.0","id":7,"method":"workspace/configuration","params":{"items":5}}', "items that are no list"),
    ('{"jsonrpc":"2.0","id":7,"method":"window/workDoneProgress/create","params":3}', "a token request without params"),
]


@pytest.mark.parametrize("frame", [f for f, _ in WRONG_FRAMES], ids=[w for _, w in WRONG_FRAMES])
def test_a_message_with_odd_fields_is_tolerated_and_the_session_goes_on(tmp_path, ws, frame):
    argv, _ = lsp_script(tmp_path, frames=[frame], refs={"a.py:0:0": [["b.py", 1]]})
    with sem.Lsp(argv, str(ws), time.monotonic() + 20) as c:
        c.settle(0.2)
        assert c.references("a.py", 0, 0) == [("b.py", 1)]


@pytest.mark.parametrize(
    "frame",
    ["[1, 2]", "42", '"text"', "null", '{"jsonrpc":"2.0","id":[1],"result":null}', '{"id":{"a":1},"method":"x"}'],
)
def test_a_frame_that_is_no_json_rpc_message_is_an_lsp_error_not_another_exception(tmp_path, ws, frame):
    argv, _ = lsp_script(tmp_path, frames=[frame])
    with pytest.raises(sem.LspError):
        with sem.Lsp(argv, str(ws), time.monotonic() + 20) as c:
            c.settle(0.2)
            c.references("a.py", 0, 0)


def test_a_deeply_nested_frame_ends_the_session_like_any_garbage(tmp_path, ws):
    script = (
        "import sys, time; body = b'[' * 300000 + b']' * 300000; "
        "sys.stdout.buffer.write(b'Content-Length: %d\\r\\n\\r\\n' % len(body) + body); sys.stdout.flush(); time.sleep(30)"
    )
    t0 = time.monotonic()
    with pytest.raises(sem.LspError, match="closed the channel"):
        with sem.Lsp([sys.executable, "-c", script], str(ws), t0 + 20):
            pytest.fail("a server that sends a body no parser can read has no session")
    assert time.monotonic() - t0 < 15


@pytest.mark.parametrize(
    "raw",
    [{"a": 1}, 7, "x", [1, 2], [{"uri": "file:///a.py", "range": {"start": {"line": "z"}}}]],
    ids=["dict", "int", "str", "list-of-ints", "bad-line"],
)
def test_an_answer_of_the_wrong_shape_is_an_lsp_error_not_another_exception(tmp_path, ws, raw):
    argv, _ = lsp_script(tmp_path, raw={"textDocument/references": raw})
    with sem.Lsp(argv, str(ws), time.monotonic() + 20) as c:
        with pytest.raises(sem.LspError, match="wrong shape"):
            c.references("a.py", 0, 0)


@pytest.mark.parametrize(
    "raw",
    [
        {"textDocument/prepareCallHierarchy": {"a": 1}},
        {"callHierarchy/incomingCalls": 5},
        {"callHierarchy/incomingCalls": [7]},
        {"callHierarchy/incomingCalls": [{"from": {"uri": "file:///a.py"}, "fromRanges": {"a": 1}}]},
    ],
    ids=["prepare-dict", "incoming-int", "incoming-list-of-ints", "from-ranges-dict"],
)
def test_a_call_hierarchy_answer_of_the_wrong_shape_is_an_lsp_error(tmp_path, ws, raw):
    argv, _ = lsp_script(tmp_path, caps={"callHierarchyProvider": True}, raw=raw)
    with sem.Lsp(argv, str(ws), time.monotonic() + 20) as c:
        with pytest.raises(sem.LspError, match="wrong shape"):
            c.incoming_calls("a.py", 0, 0)


def test_the_session_limit_counts_requests_and_stops_the_one_after_it(tmp_path, ws):
    argv, _ = lsp_script(tmp_path, caps={"referencesProvider": True, "callHierarchyProvider": True})
    with sem.Lsp(argv, str(ws), time.monotonic() + 20, limit=3) as c:
        c.references("a.py", 0, 0)  # 1
        assert c.incoming_calls("a.py", 0, 0) == [] and c.queries == 3  # prepare + one incomingCalls: 2 and 3
        with pytest.raises(sem.LspBudgetError):
            c.references("a.py", 0, 0)
        assert c.queries == 3 and issubclass(sem.LspBudgetError, sem.LspError)


def test_a_server_that_never_ends_its_indexing_holds_the_session_only_for_the_cap(tmp_path, ws):
    argv, _ = lsp_script(tmp_path, progress="stuck")
    with sem.Lsp(argv, str(ws), time.monotonic() + 20) as c:
        t0 = time.monotonic()
        assert c.settle(0.2, 0.3) is False
        assert time.monotonic() - t0 < 5 and c.progress and c.tokens == {"idx"}


def test_a_json_rpc_error_is_an_lsp_error_with_its_message(tmp_path, ws):
    argv, _ = lsp_script(tmp_path, errors={"textDocument/references": "boom"})
    with sem.Lsp(argv, str(ws), time.monotonic() + 20) as c:
        with pytest.raises(sem.LspError, match="textDocument/references: boom"):
            c.references("a.py", 0, 0)


def proc_alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):  # gone, or gone while we read it
        return False
    return state != "Z"  # a zombie is dead, it only waits for its parent


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc to see whether a process is alive")
def test_a_server_that_ignores_shutdown_is_killed_with_everything_it_started(tmp_path, ws):
    pidfile = tmp_path / "child.pid"
    argv, _ = lsp_script(tmp_path, ignore_shutdown=True, child=str(pidfile))
    with sem.Lsp(argv, str(ws), time.monotonic() + 20) as c:
        child = int(pidfile.read_text())
        assert proc_alive(child) and proc_alive(c.proc.pid)
    assert c.proc.poll() is not None
    for _ in range(40):  # the kernel needs a moment to reap the child
        if not proc_alive(child):
            break
        time.sleep(0.05)
    assert not proc_alive(child)


def test_a_server_that_floods_the_channel_is_cut_off(tmp_path, ws, monkeypatch):
    monkeypatch.setattr(sem, "FLOOD", 200_000)
    argv, _ = lsp_script(tmp_path, flood=60)
    with sem.Lsp(argv, str(ws), time.monotonic() + 20) as c:
        with pytest.raises(sem.LspError, match="closed the channel"):
            c.references("a.py", 0, 0)


def test_a_server_that_stops_reading_cannot_hold_a_write_past_the_deadline(tmp_path, ws):
    argv, _ = lsp_script(tmp_path, sleep_after_init=True)
    t0 = time.monotonic()
    with sem.Lsp(argv, str(ws), t0 + 1.0) as c:
        with pytest.raises(sem.LspError, match="gone"):
            c.open("big.py", "python", "x" * 5_000_000)  # more than a pipe holds, and nobody reads it
    assert time.monotonic() - t0 < 20


def test_a_request_the_server_does_not_answer_ends_at_the_deadline(tmp_path, ws):
    argv, _ = lsp_script(tmp_path, hang=True)
    t0 = time.monotonic()
    with sem.Lsp(argv, str(ws), t0 + 1.0) as c:
        with pytest.raises(sem.LspTimeoutError):
            c.references("a.py", 4, 4)
    assert time.monotonic() - t0 < 15


# ================================================================ positions
@pytest.mark.parametrize(
    "text, node, want",
    [
        ("def run():\n    pass\n", mk_node("f", "run", "func", 1, 2), (0, 4)),
        (
            "@deco(run)\ndef run():\n    pass\n",
            mk_node("f", "run", "func", 1, 3),
            (1, 4),
        ),  # a decorator's `run` is no def
        ("class K:\n    def m(self):\n        pass\n", mk_node("f", "K.m", "method", 2, 3), (1, 8)),
        ("package p\n\nfunc (a Alpha) Ping() int { return 1 }\n", mk_node("f", "Alpha.Ping", "method", 3), (2, 15)),
        ("class A {\n  ping(): number {\n    return 1;\n  }\n}\n", mk_node("f", "A.ping", "method", 2, 4), (1, 2)),
        ("fn main() {}\n", mk_node("f", "main", "func", 1), (0, 3)),
        ("x = '\U0001f600'; def f(): pass\n", mk_node("f", "f", "func", 1), (0, 14)),  # UTF-16: the emoji is two units
        ("def other():\n    pass\n", mk_node("f", "run", "func", 1, 2), None),
    ],
)
def test_name_position_finds_the_name_of_a_definition(text, node, want):
    assert sem.name_position(text, node) == want


# ================================================================ arbitration
def graph_of(n_funcs=3, lang="python", path="a.py"):
    """n functions f0.. in `path`, two lines each, and `main` after them; the name is on the first line of each."""
    nodes = [mk_file(path, lang)]
    nodes += [mk_node(path, f"f{i}", "func", 2 * i + 1, 2 * i + 2, lang=lang) for i in range(n_funcs)]
    nodes += [mk_node(path, "main", "func", 2 * n_funcs + 1, 2 * n_funcs + 2, lang=lang)]
    return md.canonical(nodes, [])


def source_of(n_funcs, kw="def"):
    return "".join(f"{kw} f{i}():\n    pass\n" for i in range(n_funcs)) + f"{kw} main():\n    pass\n"


def specs_of(argv, engine="fake", family="python"):
    return {family: sem.Spec(engine, tuple(argv), ())}


def arb(tmp_path, g, cands, specs, text, **kw):
    kw.setdefault("grace_s", 0.2)
    return sem.arbitrate(g, cands, str(tmp_path / "ws"), text, specs, **kw)


def key_of(g, name):
    return next(n.key for n in g.nodes if qual(n.key) == name)


@pytest.fixture
def aws(tmp_path):
    (tmp_path / "ws").mkdir()


def test_a_reference_the_server_finds_is_an_s_edge_from_the_node_that_holds_it(tmp_path, aws):
    g, text = graph_of(3), source_of(3)
    argv, log = lsp_script(tmp_path, refs={"a.py:0:4": [["a.py", 6], ["a.py", 0]], "a.py:2:4": []})
    r = arb(tmp_path, g, [key_of(g, "f0"), key_of(g, "f1")], specs_of(argv), lambda p: text)
    assert r.status == {key_of(g, "f0"): "referenced", key_of(g, "f1"): "unreferenced"}
    assert r.queries == 2 and r.findings == () and len(asked(log)) == 2
    (e,) = r.graph.edges
    assert (qual(e.src), qual(e.dst), e.kind, e.tier, e.site) == ("main", "f0", "ref", "S", "a.py:7")
    assert r.graph.evid[e.ev].source == "lsp:fake"  # the engine that produced the edge


def test_silence_counts_only_from_a_server_that_announced_and_finished_its_indexing(tmp_path, aws):
    g, text = graph_of(2), source_of(2)
    cands = [key_of(g, "f0")]
    argv, _ = lsp_script(tmp_path)
    assert arb(tmp_path, g, cands, specs_of(argv), lambda p: text).status[cands[0]] == "unreferenced"
    for name, script in (("none", {"progress": "none"}), ("calls", {"caps": {"callHierarchyProvider": True}})):
        argv, _ = lsp_script(
            tmp_path, name, **script
        )  # no announcement; calls only (a function passed as a value is no call)
        r = arb(tmp_path, g, cands, specs_of(argv), lambda p: text)
        assert r.status[cands[0]] == "unconfirmed" and [f.code for f in r.findings] == ["dc.sem-lsp-incomplete"], name


def test_the_call_hierarchy_finds_references_when_the_server_has_no_references(tmp_path, aws):
    g, text = graph_of(2), source_of(2)
    argv, _ = lsp_script(tmp_path, caps={"callHierarchyProvider": True}, calls={"a.py:0:4": [["a.py", 4]]})
    r = arb(tmp_path, g, [key_of(g, "f0")], specs_of(argv), lambda p: text)
    assert r.status[key_of(g, "f0")] == "referenced"
    assert {(qual(e.src), qual(e.dst), e.tier) for e in r.graph.edges} == {("main", "f0", "S")}


def test_at_most_20_queries_per_language(tmp_path, aws):
    g, text = graph_of(30), source_of(30)
    cands = [key_of(g, f"f{i}") for i in range(25)]
    argv, log = lsp_script(tmp_path)
    r = arb(tmp_path, g, cands, specs_of(argv), lambda p: text)
    assert len(asked(log)) == 20 and r.queries == 20
    assert [r.status[k] for k in cands] == ["unreferenced"] * 20 + ["skipped"] * 5
    (f,) = r.findings
    assert (f.level, f.code) == ("W", "dc.sem-lsp-budget") and "5 candidates" in f.msg and "20 requests" in f.msg
    assert sem.config()["lsp"]["max_queries"] == 20 and sem.config()["lsp"]["budget_s"] == 20


def test_the_limit_is_per_language_and_each_language_has_its_own_server(tmp_path, aws):
    g1, g2 = graph_of(24, "python", "a.py"), graph_of(24, "go", "b.go")
    g = md.canonical([*g1.nodes, *g2.nodes], [])
    texts = {"a.py": source_of(24), "b.go": source_of(24, "func")}
    argv_py, log_py = lsp_script(tmp_path, "py")
    argv_go, log_go = lsp_script(tmp_path, "go")
    cands = [n.key for n in g.nodes if n.kind == "func" and qual(n.key) != "main"]
    specs = {**specs_of(argv_py, "pyls", "python"), **specs_of(argv_go, "gopls", "go")}
    r = arb(tmp_path, g, cands, specs, lambda p: texts[p])
    assert (len(asked(log_py)), len(asked(log_go)), r.queries) == (20, 20, 40)
    assert [f.code for f in r.findings] == ["dc.sem-lsp-budget"] * 2


def test_an_exhausted_budget_starts_no_server_and_says_so(tmp_path, aws):
    g, text = graph_of(3), source_of(3)
    argv, log = lsp_script(tmp_path)
    r = arb(tmp_path, g, [key_of(g, "f0")], specs_of(argv), lambda p: text, budget_s=0)
    assert r.status == {key_of(g, "f0"): "skipped"} and r.queries == 0 and not log.exists()
    assert [f.code for f in r.findings] == ["dc.sem-lsp-budget"]


def test_a_silent_server_cannot_stretch_the_budget(tmp_path, aws):
    g, text = graph_of(3), source_of(3)
    argv, _ = lsp_script(tmp_path, hang=True)
    t0 = time.monotonic()
    r = arb(tmp_path, g, [key_of(g, f"f{i}") for i in range(3)], specs_of(argv), lambda p: text, budget_s=1.0)
    assert time.monotonic() - t0 < 20
    assert set(r.status.values()) == {"skipped"} and [f.code for f in r.findings] == ["dc.sem-lsp-failed"]
    assert "no answer before the deadline" in r.findings[0].msg


def test_a_server_that_crashes_keeps_what_it_answered_and_the_rest_is_reported(tmp_path, aws):
    g, text = graph_of(4), source_of(4)
    argv, _ = lsp_script(tmp_path, crash_after=1, refs={"a.py:0:4": [["a.py", 8]]})
    cands = [key_of(g, f"f{i}") for i in range(3)]
    r = arb(tmp_path, g, cands, specs_of(argv), lambda p: text)
    assert [r.status[k] for k in cands] == ["referenced", "skipped", "skipped"]
    assert [f.code for f in r.findings] == ["dc.sem-lsp-failed"] and "fake" in r.findings[0].msg
    assert len([e for e in r.graph.edges if e.tier == "S"]) == 1


def test_an_error_answer_of_the_server_is_a_failure_with_its_message(tmp_path, aws):
    g, text = graph_of(2), source_of(2)
    argv, _ = lsp_script(tmp_path, errors={"textDocument/references": "boom"})
    r = arb(tmp_path, g, [key_of(g, "f0")], specs_of(argv), lambda p: text)
    assert r.status == {key_of(g, "f0"): "skipped"}
    assert [f.code for f in r.findings] == ["dc.sem-lsp-failed"] and "boom" in r.findings[0].msg


def test_a_family_without_a_usable_server_is_reported_once_and_not_silently_skipped(tmp_path, aws):
    g, text = graph_of(3), source_of(3)
    specs = {"python": "basedpyright is missing: pipx install basedpyright"}
    r = arb(tmp_path, g, [key_of(g, f"f{i}") for i in range(3)], specs, lambda p: text)
    assert set(r.status.values()) == {"skipped"} and r.queries == 0
    (f,) = r.findings
    assert f.code == "dc.sem-lsp-failed" and "python" in f.msg and "pipx install" in f.msg
    assert [f.code for f in arb(tmp_path, g, [key_of(g, "f0")], {}, lambda p: text).findings] == ["dc.sem-lsp-failed"]


def test_candidates_the_graph_or_the_work_tree_cannot_place_are_reported(tmp_path, aws):
    g = graph_of(2)
    argv, log = lsp_script(tmp_path)
    cands = ["python:nowhere.py#x", key_of(g, "f0"), key_of(g, "f1")]
    r = arb(tmp_path, g, cands, specs_of(argv), lambda p: None)
    assert set(r.status.values()) == {"skipped"} and r.queries == 0 and not log.exists()
    (f,) = r.findings
    assert f.code == "dc.sem-lsp-failed" and "3 candidates could not be placed" in f.msg


def test_a_server_that_offers_nothing_is_a_failure_not_an_answer(tmp_path, aws):
    g, text = graph_of(2), source_of(2)
    argv, _ = lsp_script(tmp_path, caps={})
    r = arb(tmp_path, g, [key_of(g, "f0")], specs_of(argv), lambda p: text)
    assert r.status[key_of(g, "f0")] == "skipped" and "neither references nor call hierarchy" in r.findings[0].msg


def test_lsp_edges_are_deterministic_and_one_per_pair_of_nodes(tmp_path, aws):
    g, text = graph_of(3), source_of(3)
    argv, _ = lsp_script(tmp_path, refs={"a.py:0:4": [["a.py", 6], ["a.py", 6], ["a.py", 7]]})
    runs = [arb(tmp_path, g, [key_of(g, "f0")], specs_of(argv), lambda p: text).graph for _ in range(2)]
    assert runs[0].digest() == runs[1].digest()
    (e,) = [e for e in runs[0].edges if e.tier == "S"]  # two lines of main() reference f0: one edge, from the first
    assert (qual(e.src), qual(e.dst), e.site) == ("main", "f0", "a.py:7")


def test_an_lsp_reference_confirms_a_name_edge_with_an_s_edge_and_does_not_repeat_one(tmp_path, aws):
    g0, text = graph_of(3), source_of(3)
    main, f0 = key_of(g0, "main"), key_of(g0, "f0")
    g = md.canonical(g0.nodes, [md.edge(main, f0, "call", "N", "a.py:7")])
    argv, _ = lsp_script(tmp_path, refs={"a.py:0:4": [["a.py", 6]]})
    r = arb(tmp_path, g, [f0], specs_of(argv), lambda p: text)
    assert {(qual(e.src), e.tier) for e in r.graph.edges} == {("main", "N"), ("main", "S")}
    again = arb(tmp_path, r.graph, [f0], specs_of(argv), lambda p: text)
    assert again.graph.edges == r.graph.edges


def test_a_reference_from_the_candidates_own_body_is_no_reference(tmp_path, aws):
    g, text = graph_of(3), source_of(3)
    argv, _ = lsp_script(tmp_path, refs={"a.py:0:4": [["a.py", 1], ["a.py", 0]]})  # its own body, and its declaration
    r = arb(tmp_path, g, [key_of(g, "f0")], specs_of(argv), lambda p: text)
    assert r.status == {key_of(g, "f0"): "unreferenced"} and r.graph is g and r.findings == ()


def test_a_reference_on_the_declaration_line_of_another_node_is_kept(tmp_path, aws):
    # one line: `def cand(): pass; user = lambda: cand()`: cand and user both start on it
    p = "a.py"
    cand, user = mk_node(p, "cand", "func", 1, 1), mk_node(p, "user", "func", 1, 1)
    g = md.canonical([mk_file(p), cand, user], [])
    argv, _ = lsp_script(tmp_path, refs={"a.py:0:4": [["a.py", 0]]})
    text = "def cand(): pass; user = lambda: cand()\n"
    r = arb(tmp_path, g, [cand.key], specs_of(argv), lambda p: text)
    assert r.status == {cand.key: "referenced"}
    assert {(qual(e.src), qual(e.dst), e.tier) for e in r.graph.edges} == {("user", "cand", "S")}


def test_a_reference_from_a_file_the_graph_does_not_know_is_a_reference_without_an_edge(tmp_path, aws):
    g, text = graph_of(2), source_of(2)
    argv, _ = lsp_script(tmp_path, refs={"a.py:0:4": [["gen/other.py", 3]]})
    r = arb(tmp_path, g, [key_of(g, "f0")], specs_of(argv), lambda p: text)
    assert r.status == {key_of(g, "f0"): "referenced"} and r.graph is g


def test_every_request_counts_as_a_query_and_the_limit_stops_a_call_hierarchy_question(tmp_path, aws):
    g, text = graph_of(5), source_of(5)
    argv, log = lsp_script(tmp_path, caps={"callHierarchyProvider": True}, calls={"a.py:0:4": [["a.py", 10]]})
    cands = [key_of(g, f"f{i}") for i in range(4)]
    r = arb(tmp_path, g, cands, specs_of(argv), lambda p: text, max_queries=5)
    # f0: prepare + incoming (2), f1: the same (4), f2: prepare (5), its incomingCalls would be the sixth request
    assert r.queries == 5 and len(asked(log, "textDocument/prepareCallHierarchy")) == 3
    assert len(asked(log, "callHierarchy/incomingCalls")) == 2
    assert [r.status[k] for k in cands] == ["referenced", "unconfirmed", "skipped", "skipped"]
    assert [f.code for f in r.findings] == ["dc.sem-lsp-budget", "dc.sem-lsp-incomplete"]
    assert "2 candidates" in r.findings[0].msg and "5 requests" in r.findings[0].msg


def test_a_stuck_indexing_holds_the_budget_only_for_settle_s_and_the_candidates_are_still_asked(tmp_path, aws):
    g, text = graph_of(3), source_of(3)
    argv, _ = lsp_script(tmp_path, progress="stuck", refs={"a.py:0:4": [["a.py", 6]]})
    cands = [key_of(g, "f0"), key_of(g, "f1")]
    t0 = time.monotonic()
    r = arb(tmp_path, g, cands, specs_of(argv), lambda p: text, settle_s=0.3)
    assert time.monotonic() - t0 < 10  # the budget is 20 s
    assert [r.status[k] for k in cands] == ["referenced", "unconfirmed"]
    assert [f.code for f in r.findings] == ["dc.sem-lsp-incomplete"]
    assert 0 < sem.config()["lsp"]["settle_s"] < sem.config()["lsp"]["budget_s"]


def test_a_server_that_answers_in_the_wrong_shape_is_a_finding_not_a_crash(tmp_path, aws):
    g, text = graph_of(2), source_of(2)
    argv, _ = lsp_script(tmp_path, raw={"textDocument/references": {"locations": []}})
    r = arb(tmp_path, g, [key_of(g, "f0")], specs_of(argv), lambda p: text)
    assert r.status == {key_of(g, "f0"): "skipped"}
    assert [f.code for f in r.findings] == ["dc.sem-lsp-failed"] and "wrong shape" in r.findings[0].msg


def test_a_server_that_ran_without_a_network_namespace_is_reported(tmp_path, aws):
    g, text = graph_of(2), source_of(2)
    argv, _ = lsp_script(tmp_path)
    specs = {"python": sem.Spec("fake", tuple(argv), (), None, False)}
    r = arb(tmp_path, g, [key_of(g, "f0")], specs, lambda p: text)
    assert r.status == {key_of(g, "f0"): "unreferenced"}  # it answered: the answer is kept
    (f,) = r.findings
    assert (f.level, f.code) == ("W", "dc.sem-lsp-sandbox") and "unshare -rn" in f.msg and "fake (python)" in f.msg


# ================================================================ the branch level: overlay first, servers for the rest
def branch_world(tmp_path, **idx_kw):
    t = twin(fillers=95)
    go = mk_node("x.go", "g0", "func", 1, 2, lang="go")
    g = md.canonical([*t.graph.nodes, mk_file("x.go", "go"), go], list(t.graph.edges))
    idx, tree = stamped(tmp_path, g, twin_index("Foo#m().", fillers=95, **idx_kw))
    (tmp_path / "ws").mkdir()
    return t, go, g, idx, tree


def test_branch_applies_the_overlay_and_asks_the_servers_only_about_what_it_does_not_cover(tmp_path):
    t, go, g, idx, tree = branch_world(tmp_path)
    argv, log = lsp_script(tmp_path)
    texts = {"x.go": "func g0() {}\n", "m.py": twin_text()("m.py")}
    specs = {"go": sem.Spec("gopls", tuple(argv), ())}
    b = sem.branch(g, tree, str(tmp_path / "ws"), texts.get, [t.foo_m.key, go.key], [idx], specs, grace_s=0.2)
    assert b.status == {t.foo_m.key: "covered", go.key: "unreferenced"}  # Foo.m: answered by the overlay, not asked
    assert b.queries == 1 and len(asked(log)) == 1 and b.findings == ()
    assert edges_to(b.graph, "Bar.m") == set() and edges_to(b.graph, "Foo.m") == {("use", "call", "S")}
    assert [r.status for r in b.reports] == ["applied"]


def test_branch_asks_about_the_candidates_of_a_package_whose_index_covers_too_little(tmp_path):
    t, go, g, idx, tree = branch_world(tmp_path, missing=5)
    argv, log = lsp_script(tmp_path, refs={"m.py:1:8": [["m.py", 5]]})
    texts = {"m.py": twin_text()("m.py")}
    specs = {"python": sem.Spec("pyls", tuple(argv), ())}
    b = sem.branch(g, tree, str(tmp_path / "ws"), texts.get, [t.foo_m.key], [idx], specs, grace_s=0.2)
    assert b.status == {t.foo_m.key: "referenced"} and len(asked(log)) == 1
    assert [f.code for f in b.findings] == ["dc.sem-coverage"] and b.reports[0].status == "partial"
    assert {(qual(e.src), e.tier) for e in b.graph.edges if qual(e.dst) == "Foo.m"} == {("use", "S")}


def test_branch_without_specs_resolves_the_servers_of_the_languages_it_still_has_to_ask_about(tmp_path, monkeypatch):
    t, go, g, idx, tree = branch_world(tmp_path)
    asked_for = []

    def fake_servers(families):
        asked_for.append(sorted(families))
        return {f: f"{f} server is missing" for f in families}

    monkeypatch.setattr(sem, "servers", fake_servers)
    texts = {"x.go": "func g0() {}\n", "m.py": twin_text()("m.py")}
    b = sem.branch(g, tree, str(tmp_path / "ws"), texts.get, [t.foo_m.key, go.key], [idx])
    assert asked_for == [["go"]]  # Foo.m is covered by the overlay: python needs no server
    assert b.status[go.key] == "skipped" and [f.code for f in b.findings] == ["dc.sem-lsp-failed"]


# ================================================================ servers: the registry and the one engine resolver
def test_servers_come_from_the_engine_resolver_with_its_environment(monkeypatch):
    seen = []

    def fake_find(reg, lock, name, **kw):
        seen.append((name, kw["names"]))
        return (
            engines.Res(None, "missing", "")
            if name == "gopls"
            else engines.Res("/engines/bin/" + kw["names"][0], "ok", "")
        )

    monkeypatch.setattr(engines, "find_engine", fake_find)
    monkeypatch.setattr(engines, "netless_prefix", lambda: ["unshare", "-rn"])
    out = sem.servers(["python", "go", "yaml"], reg=engines.load_registry(), lock={})
    assert seen == [("gopls", ("gopls",)), ("basedpyright", ("basedpyright-langserver",))]
    want = ("unshare", "-rn", "/engines/bin/basedpyright-langserver", "--stdio")
    assert out["python"] == sem.Spec("basedpyright", want, (("PYTHONDONTWRITEBYTECODE", "1"),))
    assert out["go"].startswith("gopls is missing") and "go install" in out["go"]
    assert out["yaml"] == "no language server is configured for yaml"


def fake_resolver(monkeypatch, prefix):
    monkeypatch.setattr(
        engines, "find_engine", lambda reg, lock, name, **kw: engines.Res("/engines/bin/" + kw["names"][0], "ok", "")
    )
    monkeypatch.setattr(engines, "netless_prefix", lambda: list(prefix))


def test_a_host_without_a_network_namespace_gives_specs_that_say_so(monkeypatch):
    reg = engines.load_registry()
    fake_resolver(monkeypatch, [])
    py = sem.servers(["python"], reg=reg, lock={})["python"]
    assert py.netless is False and py.argv == ("/engines/bin/basedpyright-langserver", "--stdio")
    fake_resolver(monkeypatch, ["unshare", "-rn"])
    py = sem.servers(["python"], reg=reg, lock={})["python"]
    assert py.netless is True and py.argv[:2] == ("unshare", "-rn")


def test_a_server_that_runs_project_code_is_offered_only_to_a_caller_in_the_sandbox(monkeypatch):
    reg = engines.load_registry()
    for prefix in ([], ["unshare", "-rn"]):
        fake_resolver(monkeypatch, prefix)
        out = sem.servers(["rust"], reg=reg, lock={})["rust"]
        assert isinstance(out, str) and "rust-analyzer" in out and "sandbox" in out
        ok = sem.servers(["rust"], reg=reg, lock={}, sandboxed=True)["rust"]
        assert isinstance(ok, sem.Spec) and ok.engine == "rust-analyzer" and ok.netless == bool(prefix)
    cfg = sem.config()["lsp"]["servers"]
    assert cfg["rust"]["needs_sandbox"] is True and not any(
        v.get("needs_sandbox") for k, v in cfg.items() if k != "rust"
    )


def test_clangd_does_not_write_its_index_into_the_tree():
    assert "--background-index=false" in sem.config()["lsp"]["servers"]["cpp"]["args"]


def test_every_configured_server_is_an_engine_of_the_registry_with_that_executable():
    reg = engines.load_registry()
    for fam, s in sem.config()["lsp"]["servers"].items():
        spec = reg["engines"][s["engine"]]
        assert s["exe"] == spec["bin"]["linux"] or s["exe"] in spec.get("aliases", ()), fam
        assert spec["needs_net"] is False and s["args"] in ([], ["--stdio"], ["--background-index=false"])
    assert set(sem.config()["manifests"]) >= {"python", "go", "js"}


def test_the_acceptance_numbers_are_data():
    cfg = sem.config()
    assert (cfg["min_coverage"], cfg["lsp"]["max_queries"], cfg["lsp"]["budget_s"]) == (0.99, 20, 20)


def test_sem_has_no_network_import():
    tree = ast.parse((ROOT / "dead_code_checker" / "graph" / "sem.py").read_text())
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and not n.level}
    banned = (
        "socket",
        "ssl",
        "http",
        "requests",
        "urllib.request",
        "urllib3",
        "ftplib",
        "smtplib",
        "xmlrpc",
        "asyncio",
    )
    assert not [m for m in mods if m.split(".")[0] in banned or m == "urllib.request"]
