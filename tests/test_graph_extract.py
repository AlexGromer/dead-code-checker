"""graph.extract: the Python extractor (stdlib ast, fully testable), the generic tree-sitter extractor with the bash queries
(facts mapping through a FAKE parser backend; the real grammar test is skipped with its reason when tree-sitter or the grammar
is not installed), the loader graph.ts (with fake bindings), and the parallel batch driver.

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_extract.py
"""

import hashlib
import re
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test

ROOT = Path(__file__).resolve().parent.parent
from dead_code_checker import core as deadcode  # noqa: E402
from dead_code_checker.graph import contracts, model, ts as tsm  # noqa: E402
from dead_code_checker.graph.extract import batch, common, py as dcpy, ts_generic as tg  # noqa: E402
from graph_support import inline_units  # noqa: E402

PY = contracts.get_extractor("python")


def facts(src: str, lang: str = "python", path: str = "a.py") -> model.Facts:
    return PY.extract(lang, path, src) if lang == "python" else contracts.get_extractor(lang).extract(lang, path, src)


def defs(f):
    return {d.qual: d for d in f.defs}


def refs(f, kind=None):
    return {(r.name, r.scope) for r in f.refs if kind in (None, r.kind)}


# ====================================================================== python: definitions
SRC_DEFS = '''
"""doc"""
import os
__all__ = ["pub", "Klass"]
MAXV = 3
local_var = 1
def pub(a):
    CONST_IN_FUNC = 1
    def inner():
        return a
    return inner()
def _priv(): pass
class Klass(Base):
    ATTR = 1
    @property
    def p(self): return self._p
    @abstractmethod
    def ab(self): ...
    def __repr__(self): return "x"
    @staticmethod
    def helper(x): return x
if os.name == "nt":
    def plat(): return 1
else:
    def plat(): return 2
try:
    def alt(): pass
except Exception:
    def alt(): return 1
class Proto(Protocol):
    def req(self): ...
'''


def test_python_defs():
    f = facts(SRC_DEFS)
    d = defs(f)
    assert f.errors == 0
    assert d[""].kind == "module" and d[""].line0 == 1
    assert d["pub"].kind == "func" and d["pub"].vis == "exported" and d["Klass"].vis == "exported"
    assert d["_priv"].vis == "private" and d["_priv"].stub == "pass" and d["_priv"].flags & model.F_STUB
    assert d["MAXV"].kind == "const" and "local_var" not in d and "pub.CONST_IN_FUNC" not in d
    assert d["pub.inner"].kind == "func" and d["pub.inner"].vis == "private"
    assert (
        d["Klass.p"].kind == "method" and not d["Klass.p"].flags & model.F_DECORATED
    )  # property is a member decorator
    assert d["Klass.helper"].flags == 0  # staticmethod is safe
    assert d["Klass.ab"].flags & model.F_ABSTRACT and d["Klass.ab"].flags & model.F_STUB
    assert d["Klass.__repr__"].flags & model.F_PROTOCOL and d["Klass.ATTR"].kind == "const"
    assert d["Proto.req"].flags & model.F_PROTOCOL
    assert d["plat"].line0 < d["plat"].line1 and d["plat"].line1 >= 25  # both branches: one Def spanning them
    assert d["alt"].stub == "" and not d["alt"].flags & model.F_STUB  # only one of the two is a stub
    assert [x.qual for x in f.defs].count("plat") == 1


def test_python_decorated_flag_and_class_stub_free():
    f = facts("@app.route('/x')\ndef h(): return 1\n@lru_cache\ndef c(): return 1\n@a.b\nclass K: pass\n")
    d = defs(f)
    assert d["h"].flags & model.F_DECORATED and not d["c"].flags & model.F_DECORATED
    assert d["K"].flags & model.F_DECORATED and d["K"].stub == ""
    assert d["h"].line0 == 1  # the decorator line starts the def


def test_python_module_def_stub_is_the_hook_verdict():
    stub = "import sys\nsys.stdin.read()\nprint('{}')\n"
    real = "import sys, json\nd = json.load(sys.stdin)\nprint(json.dumps({'x': d['y']}))\n"
    assert defs(facts(stub))[""].stub == deadcode.py_hook_stub_reason(deadcode.py_parse(stub)) != ""
    assert defs(facts(real))[""].stub == ""
    assert not defs(facts(stub))[""].flags & model.F_STUB  # the roots layer decides whether the file is registered


def test_python_hook_stub_verdict_is_skipped_for_a_big_file(monkeypatch):
    monkeypatch.setattr(dcpy, "HOOK_STUB_MAX_CHARS", 20)
    stub = "import sys\nsys.stdin.read()\nprint('{}')\n"
    assert defs(facts(stub))[""].stub == "" and len(stub) > 20


# ====================================================================== python: uses
def test_python_refs_calls_and_locals():
    f = facts(
        "import os\n"
        "def helper(): pass\n"
        "def run(cb, n=helper):\n"
        "    x = helper()\n"
        "    cb()\n"
        "    return x + os.getcwd() + other\n"
        "class K(Base, mixins.M):\n"
        "    def m(self):\n"
        "        return self.n() + self.v\n"
        "    def n(self): return len([i for i in range(3) if helper2(i)])\n"
    )
    r = {(x.name, x.kind, x.scope) for x in f.refs}
    assert ("helper", "call", "run") in r and ("helper", "ref", "") in r  # default value: the ENCLOSING scope
    assert ("cb", "call", "run") not in r and ("x", "ref", "run") not in r  # parameters and assigned names are local
    assert ("os.getcwd", "call", "run") in r and ("other", "ref", "run") in r
    assert ("Base", "inherit", "K") in r and ("mixins.M", "inherit", "K") in r  # the scope of a base is the class
    assert ("self.n", "call", "K.m") in r and ("self.v", "ref", "K.m") in r  # attributes are never skipped
    assert ("helper2", "call", "K.n") in r and ("i", "ref", "K.n") not in r  # a comprehension variable is local
    assert ("range", "call", "K.n") in r


def test_python_names_that_must_stay_refs():
    f = facts(
        "def f():\n"
        "    import mod\n"
        "    from pkg import helper\n"
        "    def inner(): pass\n"
        "    inner()\n"
        "    helper()\n"
        "    global G\n"
        "    G = 1\n"
        "    print(G)\n"
        "    return [t for t in t]\n"
    )
    r = refs(f)
    assert ("inner", "f") in r and (
        "helper",
        "f",
    ) in r  # an import or a nested def is resolved by the linker, not local
    assert ("G", "f") in r  # a global is not local
    assert ("t", "f") in r  # the first iterable of a comprehension is outside it


def test_python_decorators_and_class_body_scopes():
    f = facts(
        "def deco(f): return f\n@deco\ndef g(): pass\nclass C:\n    x = make()\n    @deco\n    def m(self): pass\n"
    )
    r = {(x.name, x.scope) for x in f.refs}
    assert ("deco", "") in r and ("deco", "C") in r  # decorators run in the enclosing scope
    assert ("make", "C") in r


def test_python_string_annotation_and_attribute_receiver():
    f = facts("def f(a: 'Foo', b: 'list[Bar]') -> 'Baz': return foo().bar + 'x'.join(a)\n")
    r = refs(f)
    assert {("Foo", ""), ("Bar", ""), ("Baz", "")} <= r
    assert ("?.bar", "f") in r and ("foo", "f") in r and not any(n.endswith(".join") for n, _ in r)  # "x".join: no ref


# ====================================================================== python: imports and dynamic sites
def test_python_imports():
    f = facts(
        "import a.b as c, d\nfrom m import x as y, z\nfrom . import s\nfrom ..p.q import w\nfrom e import *\n"
        "def g():\n    import inner\n"
    )
    got = {(i.module, i.names, i.alias, i.level, i.scope) for i in f.imports}
    assert got == {("a.b", (), "c", 0, ""), ("d", (), "", 0, ""), ("m", ("x",), "y", 0, ""), ("m", ("z",), "", 0, ""),
                   ("", ("s",), "", 1, ""), ("p.q", ("w",), "", 2, ""), ("e", ("*",), "", 0, ""), ("inner", (), "", 0, "g")}  # fmt: skip


def test_python_dynamic_sites():
    f = facts(
        "import importlib\n"
        "def g(obj, name):\n"
        "    getattr(obj, name)\n"
        "    getattr(self.o, 'known')\n"
        "    importlib.import_module('pkg.mod')\n"
        "    importlib.import_module('.rel', __package__)\n"
        "    importlib.import_module(name)\n"
        "    __import__(name)\n"
        "    globals()[name]()\n"
        "    globals()['fixed']()\n"
    )
    assert {d.pattern for d in f.dyn} == {"getattr(obj, *)", "import_module(*)", "__import__(*)", "globals()[*]"}
    assert all(d.scope == "g" for d in f.dyn)
    assert ("self.o.known", "g") in refs(f, "ref") and ("fixed", "g") in refs(f, "ref")
    mods = {(i.module, i.level) for i in f.imports if i.scope == "g"}
    assert mods == {("pkg.mod", 0), ("rel", 1)}


# ====================================================================== python: roots
def test_python_roots():
    f = facts(
        "__all__ = ['f', 'imported_name']\n__all__ += ['g']\n"
        "from x import imported_name\n"
        "def f(): pass\ndef g(): pass\ndef h(): pass\n"
        "@mcp.tool()\ndef t(): pass\n@server.tool('x')\ndef u(): pass\n@property\ndef p(self): pass\n"
        "if __name__ == '__main__':\n    f()\n"
    )
    roots = {(r.kind, r.target) for r in f.roots}
    assert roots == {
        ("export", "f"),
        ("export", "g"),
        ("main", ""),
        ("annotation:mcp.tool", "t"),
        ("annotation:server.tool", "u"),
    }
    assert all(r.strength == "soft" for r in f.roots if r.kind == "export")
    assert all(r.strength == "hard" for r in f.roots if r.kind != "export")
    assert ("imported_name", "") in refs(f, "ref")  # a re-exported name is a ref, not a dangling root
    assert defs(f)["f"].vis == "exported" and defs(f)["h"].vis == "public"


# ====================================================================== python: contract
def test_python_syntax_error_is_an_error_count_not_an_exception():
    for bad in ("def f(:\n", "x = (\n", "\x00", "class\n"):
        f = facts(bad)
        assert f.errors == 1 and f.defs == () and f.refs == ()


def test_python_deep_expression_does_not_crash():
    f = facts("x = " + "(" * 90 + "1" + ")" * 90 + "\ndef f(): pass\n")
    assert f.errors in (0, 1)
    f = facts("x = " + "-" * 5000 + "1\n")
    assert f.errors in (0, 1)  # RecursionError or MemoryError in ast: reported, never raised


def test_python_facts_do_not_depend_on_the_path():
    a, b = facts(SRC_DEFS, path="x/a.py"), facts(SRC_DEFS, path="y/z/b.py")
    assert a.to_payload() == b.to_payload()
    s1 = contracts.make_shard(PY, "python", "x/a.py", "b" * 40, SRC_DEFS)
    s2 = contracts.make_shard(PY, "python", "y/b.py", "b" * 40, SRC_DEFS)
    assert s1 == s2 and s1.ext_ver == "py@7" and model.Facts.from_payload(s1.payload).to_payload() == a.to_payload()


def test_python_reuses_the_text_checker_not_a_copy(monkeypatch):
    monkeypatch.setattr(deadcode, "py_stub_reason", lambda fn: "REUSED")
    monkeypatch.setattr(deadcode, "py_hook_stub_reason", lambda tree: "REUSED-HOOK")
    f = facts("def f(): return 1\n")
    assert defs(f)["f"].stub == "REUSED" and defs(f)[""].stub == "REUSED-HOOK"
    assert common.text_checker() is deadcode


def test_python_shard_key_follows_the_detector_code():
    sha = PY.grammar_sha("python")
    detector = hashlib.sha256(Path(deadcode.__file__).read_bytes()).hexdigest()
    from dead_code_checker.graph.extract import bodyhash, xlang  # the cross-language rules, the class-by-name table and the body hash decide the facts too

    want = hashlib.sha256(
        f"{detector}|{xlang.sha()}|{dcpy._class_rules()[1]}|{bodyhash.data_sha()}|{dcpy._body_sha()}".encode()
    ).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", sha) and sha == want
    assert model.shard_key("b" * 40, "python", PY.ext_ver, sha) != model.shard_key("b" * 40, "python", PY.ext_ver, "")


def test_python_extractor_is_registered_by_the_langs_data(registry_restored):
    contracts.reset_registry()
    assert contracts.get_extractor("python").name == "py"
    assert contracts.get_extractor("shell").name == "ts_generic"
    assert contracts.get_extractor("go") is contracts.get_extractor("shell")  # one extractor, many langs (data)


def test_python_on_this_package_itself_is_clean_and_stable():
    files = sorted((ROOT / "dead_code_checker").rglob("*.py"))
    assert len(files) > 10
    for p in files:
        text = p.read_text(encoding="utf-8")
        f = PY.extract("python", str(p), text)
        assert f.errors == 0, p
        quals = [d.qual for d in f.defs]
        assert len(quals) == len(set(quals)), p
        assert f.to_payload() == PY.extract("python", "other/path.py", text).to_payload(), p
        for d in f.defs:
            assert d.kind in model.NODE_KINDS and d.vis in model.VIS and 1 <= d.line0 <= d.line1, (p, d)
    d = defs(PY.extract("python", "m.py", (ROOT / "dead_code_checker/graph/model.py").read_text(encoding="utf-8")))
    assert d["node_key"].kind == "func" and d["Finding.render"].kind == "method" and d["Graph"].kind == "class"
    assert d["TIERS"].kind == "const" and d["_esc"].vis == "private"


# ====================================================================== batch
def _items(n=12):
    out = []
    for i in range(n):
        src = f"def f{i}(): return g{i % 3}()\ndef g{i % 3}(): pass\n"
        out.append(("python", f"p/m{i}.py", f"{i:040x}", src))
    out.append(("python", "p/copy.py", f"{3:040x}", out[3][3]))  # same blob as m3.py: one unit
    return out


def test_batch_is_identical_for_1_2_8_workers_and_dedupes_blobs():
    runs = [batch.extract_many(_items(), workers=w, chunk=3) for w in (1, 2, 8)]
    for shards, missing in runs:
        assert missing == {} and len(shards) == 12  # 13 items, 12 distinct blobs
    assert runs[0][0] == runs[1][0] == runs[2][0]
    assert [s.key for s in runs[0][0]] == sorted(s.key for s in runs[0][0])
    assert runs[0][0][0].facts().lang == "python"


def test_batch_skips_shards_the_store_has():
    shards, _ = batch.extract_many(_items(), workers=2)
    have = {s.key for s in shards[:5]}
    rest, _ = batch.extract_many(_items(), workers=2, have=have)
    assert {s.key for s in rest} == {s.key for s in shards[5:]}
    again, _ = batch.extract_many(_items(), workers=2, have={s.key for s in shards})
    assert again == []


def test_batch_reports_a_language_without_extractor_instead_of_skipping_it(monkeypatch):
    inline_units(monkeypatch)  # the patch below must reach the extraction, which runs in a pool
    real = tg._queries  # every ts_generic language has queries now: java stands for one that has none
    monkeypatch.setattr(tg, "_queries", lambda g: [] if g == "java" else real(g))
    items = _items(4)[:2] + [("java", "X.java", "c" * 40, "class X {}\n"), ("nope", "y", "d" * 40, "")]
    shards, missing = batch.extract_many(items, workers=1)
    assert len(shards) == 2 and set(missing) == {"java", "nope"}
    assert "no tree-sitter queries" in missing["java"] and "unknown language" in missing["nope"]


def test_batch_turns_a_recursion_error_of_one_file_into_a_file_error(monkeypatch):
    inline_units(monkeypatch)
    cls = type(PY)
    real = cls.extract

    def extract(self, lang, path, text):
        if "boom" in text:
            raise RecursionError("maximum recursion depth exceeded")
        return real(self, lang, path, text)

    monkeypatch.setattr(cls, "extract", extract)
    items = _items(4)[:3] + [("python", "p/boom.py", "e" * 40, "boom = 1\n")]
    shards, missing = batch.extract_many(items, workers=1)
    assert missing == {} and len(shards) == 4
    by = {s.blob: s.facts() for s in shards}
    assert by["e" * 40].errors == 1 and not by["e" * 40].defs and all(f.errors == 0 for b, f in by.items() if b != "e" * 40)


# ====================================================================== bash: queries as data
def test_bash_queries_exist_and_use_the_documented_vocabulary():
    qdir = Path(tg.QUERY_DIR) / "bash"
    assert sorted(p.stem for p in qdir.glob("*.scm")) == [
        "defs",
        "refs",
        "roots",
        "stub",
        "xlang",
    ]  # bash has no imports or exports
    assert set(tg.QUERY_KINDS) >= {"defs", "refs", "roots", "stub", "imports", "exports"}
    allowed = {"defs": {"def.func", "name"}, "refs": {"ref.call", "ref.include", "ref.ref", "name", "cmd", "dyn", "dyn.name", "scan"},
               "roots": {"root.marker", "root.main"}, "stub": {"stub.def", "name", "body"}}  # fmt: skip
    for kind, ok in allowed.items():
        text = re.sub(r";[^\n]*", "", (qdir / f"{kind}.scm").read_text(encoding="utf-8"))
        assert text.count("(") == text.count(")") and text.count("[") == text.count("]"), kind
        used = set(re.findall(r"@([A-Za-z_.]+)", text))
        assert used and used <= ok, (kind, used - ok)
        assert "#" not in text, "queries carry no predicates"
    assert "(function_definition" in (qdir / "defs.scm").read_text(encoding="utf-8")


def test_grammar_sha_follows_the_queries(monkeypatch, tmp_path):
    ex = contracts.get_extractor("shell")
    base = ex.grammar_sha("shell")
    assert re.fullmatch(r"[0-9a-f]{64}", base)
    q = tmp_path / "bash"
    q.mkdir()
    for p in (Path(tg.QUERY_DIR) / "bash").glob("*.scm"):
        (q / p.name).write_text(
            p.read_text(encoding="utf-8") + ("; changed\n" if p.stem == "defs" else ""), encoding="utf-8"
        )
    monkeypatch.setattr(tg, "QUERY_DIR", str(tmp_path))
    tg.queries_sha.cache_clear()
    try:
        assert ex.grammar_sha("shell") != base
    finally:
        tg.queries_sha.cache_clear()


# ====================================================================== bash: facts mapping through a fake parser
class FNode(NamedTuple):
    start_byte: int
    end_byte: int
    start_point: tuple
    end_point: tuple


class FakeShell:
    """A line-based stand-in for tree-sitter-bash that answers the queries of data/queries/bash in their capture vocabulary:
    `name() {` ... `}` (or one line `name() { body }`) are function definitions; every other line is one command."""

    DEF = re.compile(r"^(\s*)([\w.-]+)\(\)\s*\{(.*)$")

    def __init__(self, data: bytes, errors: int = 0):
        self.errors, self.m = errors, {k: [] for k in tg.QUERY_KINDS}
        offs, pos = [], 0
        for ln in data.split(b"\n"):
            offs.append(pos)
            pos += len(ln) + 1
        self.data, self.offs = data, offs
        lines = data.decode().split("\n")
        open_defs = []  # (start line, name, name node, brace offset)
        for i, ln in enumerate(lines):
            if ln.strip().startswith("#"):
                self.m["roots"].append((0, {"root.marker": [self.node(i, ln.index("#"), i, len(ln))]}))
                continue
            mdef = self.DEF.match(ln)
            if mdef:
                name, rest = mdef.group(2), mdef.group(3)
                nn = self.node(i, mdef.start(2), i, mdef.end(2))
                brace = offs[i] + ln.index("{")
                if rest.rstrip().endswith("}"):  # one-liner
                    end = offs[i] + len(ln.rstrip())
                    d = FNode(offs[i], end, (i, 0), (i, len(ln.rstrip())))
                    body = FNode(brace, end, (i, 0), (i, 0))
                    self.m["defs"].append((0, {"def.func": [d], "name": [nn]}))
                    self.m["stub"].append((0, {"stub.def": [d], "name": [nn], "body": [body]}))
                    for stmt in rest.rstrip()[:-1].split(";"):
                        self.command(i, ln, stmt)
                else:
                    open_defs.append((i, name, nn, brace))
                continue
            if ln.strip() == "}" and open_defs:
                i0, name, nn, brace = open_defs.pop()
                end = offs[i] + ln.index("}") + 1
                d = FNode(offs[i0], end, (i0, 0), (i, ln.index("}") + 1))
                self.m["defs"].append((0, {"def.func": [d], "name": [nn]}))
                self.m["stub"].append(
                    (0, {"stub.def": [d], "name": [nn], "body": [FNode(brace, end, (i0, 0), (i, 0))]})
                )
                continue
            if ln.strip():
                self.command(i, ln, ln.strip())
        self.m["roots"].sort(key=lambda x: x[1]["root.marker"][0].start_byte)

    def node(self, r0, c0, r1, c1):
        return FNode(self.offs[r0] + c0, self.offs[r1] + c1, (r0, c0), (r1, c1))

    def command(self, row, line, stmt):
        stmt = stmt.strip()
        toks = [(m.group(), m.start()) for m in re.finditer(r"\S+", stmt)]
        if not toks:
            return
        base = line.index(stmt.strip()) if stmt.strip() else 0
        nodes = [self.node(row, base + c, row, base + c + len(t)) for t, c in toks]
        whole = self.node(row, base, row, base + len(stmt.strip()))
        t0 = toks[0][0]
        if t0.startswith("$"):
            self.m["refs"].append((3, {"dyn": [nodes[0]]}))
            return
        self.m["refs"].append((0, {"ref.call": [whole], "name": [nodes[0]]}))
        if len(toks) > 1:
            self.m["refs"].append((1, {"ref.include": [whole], "cmd": [nodes[0]], "name": [nodes[1]]}))
        for n in nodes[1:]:
            self.m["refs"].append((2, {"ref.ref": [whole], "name": [n]}))

    def run(self, kind):
        return self.m[kind]


@pytest.fixture
def fake_shell():
    tg.set_backend(lambda grammar, data: FakeShell(data))
    yield
    tg.set_backend(None)


SH = """#!/bin/bash
# deadcode: root entry point
main() {
  helper --flag arg1
  source ./lib/common.sh
  . "$DIR/other.sh"
  $handler now
  echo hello
}
helper() {
  inner_call
}
_noop() { :; }
empty() {
}
trap cleanup EXIT
main "$@"
"""


def test_bash_defs_calls_includes(fake_shell):
    f = facts(SH, "shell", "bin/x.sh")
    d = defs(f)
    assert set(d) == {"", "main", "helper", "_noop", "empty"} and f.errors == 0
    assert d["main"].kind == "func" and (d["main"].line0, d["main"].line1) == (3, 9)
    assert d["_noop"].vis == "private" and d["_noop"].stub == ":" and d["_noop"].flags & model.F_STUB  # one-line stub
    assert d["empty"].stub == "empty body" and d["main"].stub == "" and d["helper"].stub == ""
    r = {(x.name, x.kind, x.scope) for x in f.refs}
    assert ("helper", "call", "main") in r and ("inner_call", "call", "helper") in r
    assert ("main", "call", "") in r  # top level
    assert ("./lib/common.sh", "include", "main") in r and ("$DIR/other.sh", "include", "main") in r
    assert not any(k == "call" and n in (".", "source") for n, k, _ in r)  # an include is not a call of `source`
    assert not any(
        k == "include" and n not in ("./lib/common.sh", "$DIR/other.sh") for n, k, _ in r
    )  # only for source / .
    assert ("cleanup", "ref", "") in r and ("hello", "ref", "main") in r  # a plain word argument may name a function
    assert not any(n.startswith("-") or n == '"$DIR/other.sh"' for n, k, _ in r if k == "ref")
    assert [(x.pattern, x.scope) for x in f.dyn] == [("$handler", "main")]


BODY_SH = """
{name}() {{
  local a=$1
  b=$2
  grep -q $a $b && echo found
  mv $b $b.bak && echo moved
  cp $a $b && echo copied
  rm -f $b.bak && echo cleaned
}}
"""


def test_bash_functions_carry_a_body_hash_that_leaves_out_the_name_and_the_comments(fake_shell):
    """The duplicates lens reads Def.bh / bsz: the text of the definition node, its own name left out, no body hash for a stub, a tiny
    function or a module."""
    text = BODY_SH.format(name="sync_one") + BODY_SH.format(name="sync_two").replace("local a=$1", "local x=$1  # other").replace("$a", "$x")
    d = defs(facts(text + SH, "shell", "bin/x.sh"))
    assert d["sync_one"].bh.startswith("ts:") and d["sync_one"].bsz >= 12
    assert d["sync_one"].bh == d["sync_two"].bh and d["sync_one"].bsz == d["sync_two"].bsz
    assert d["helper"].bh == "" and d["_noop"].bh == "" and d[""].bh == "" and d["empty"].bh == ""  # small, a stub, a module
    other = defs(facts(BODY_SH.format(name="sync_one").replace("copied", "moved twice"), "shell", "bin/y.sh"))
    assert other["sync_one"].bh != d["sync_one"].bh


def test_bash_marker_roots_and_module_stub(fake_shell):
    f = facts(SH, "shell")
    assert [(r.kind, r.target, r.line, r.strength, r.trust) for r in f.roots] == [
        ("manual", "main", 2, "hard", "registered")
    ]
    assert defs(f)[""].stub == ""
    hook = "#!/bin/sh\ncat >/dev/null\necho '{}'\n"
    assert defs(facts(hook, "shell"))[""].stub == deadcode.sh_hook_stub_reason(hook) != ""  # sh_hook_stub_reason reused
    f = facts("# deadcode: root top\n# deadcode: ignore\nx=1\n", "shell")
    assert [(r.target, r.line) for r in f.roots] == [("", 1)]  # no def below: the module is the root


def test_bash_nested_and_redefined_defs(fake_shell):
    f = facts(
        "outer() {\n  inner() {\n    deep_call\n  }\n  inner_call\n}\ndup() { :; }\ndup() {\n  work\n}\n", "shell"
    )
    d = defs(f)
    assert set(d) == {"", "outer", "outer.inner", "dup"}
    assert (d["outer.inner"].line0, d["outer.inner"].line1) == (2, 4)
    assert {(r.name, r.scope) for r in f.refs if r.kind == "call"} == {
        (":", "dup"),
        ("deep_call", "outer.inner"),
        ("inner_call", "outer"),
        ("work", "dup"),
    }
    assert (d["dup"].line0, d["dup"].line1, d["dup"].stub, d["dup"].flags) == (
        7,
        10,
        "",
        0,
    )  # one of the two is no stub


class Fixed:
    """A parser double with canned matches."""

    def __init__(self, **by_kind):
        self.errors, self.by = by_kind.pop("errors", 0), by_kind

    def run(self, kind):
        return self.by.get(kind, [])


def _extract_with(parsed, text="x" * 100, lang="shell"):
    tg.set_backend(lambda g, data: parsed)
    try:
        return tg.TsGeneric().extract(lang, "a.sh", text)
    finally:
        tg.set_backend(None)


def test_generic_mapping_makes_a_func_in_a_class_a_method():
    chars = [" "] * 100
    chars[0], chars[10], chars[20:25], chars[90] = "K", "m", list("calls"), "m"
    text = "".join(chars)
    n = lambda a, b, r0=0, r1=0: FNode(a, b, (r0, 0), (r1, 0))  # noqa: E731
    f = _extract_with(Fixed(defs=[(0, {"def.class": [n(0, 80, 0, 9)], "name": [n(0, 1)]}),
                                  (0, {"def.func": [n(10, 40, 2, 4)], "name": [n(10, 11)]}),
                                  (0, {"def.func": [n(90, 99, 11, 11)], "name": [n(90, 91)]})],
                      refs=[(0, {"ref.call": [n(20, 25, 3)], "name": [n(20, 25, 3)]})], errors=2), text)  # fmt: skip
    assert {(d.qual, d.kind) for d in f.defs} == {("", "module"), ("K", "class"), ("K.m", "method"), ("m", "func")}
    assert [(r.name, r.scope, r.line) for r in f.refs] == [("calls", "K.m", 4)] and f.errors == 2


def test_bash_unknown_capture_kinds_are_an_error_of_the_query_not_silence():
    class P:
        errors = 0

        def run(self, kind):
            return (
                [(0, {"def.bogus": [FNode(0, 3, (0, 0), (0, 3))], "name": [FNode(0, 3, (0, 0), (0, 3))]})]
                if kind == "defs"
                else []
            )

    tg.set_backend(lambda g, d: P())
    try:
        with pytest.raises(ValueError, match="not a node kind"):
            tg.TsGeneric().extract("shell", "a.sh", "abc")
    finally:
        tg.set_backend(None)


def test_bash_parse_errors_are_counted_and_the_facts_are_kept(fake_shell):
    tg.set_backend(lambda g, data: FakeShell(data, errors=1))
    f = facts("f() {\n  g\n}\n", "shell")
    assert f.errors == 1 and "f" in defs(f)


def test_bash_facts_survive_the_shard_roundtrip(fake_shell):
    ex = contracts.get_extractor("shell")
    s = contracts.make_shard(ex, "shell", "a.sh", "e" * 40, SH)
    assert s.facts().to_payload() == ex.extract("shell", "other.sh", SH).to_payload() and s.ext_ver == "ts_generic@5"
    assert s.payload == contracts.make_shard(ex, "shell", "b/c.sh", "e" * 40, SH).payload


# ====================================================================== bash: missing pieces are loud
def test_language_without_queries_is_extractor_missing(monkeypatch):
    real = tg._queries  # every ts_generic language has queries now: java stands for one that has none
    monkeypatch.setattr(tg, "_queries", lambda g: [] if g == "java" else real(g))
    with pytest.raises(contracts.ExtractorMissing, match="no tree-sitter queries for grammar java"):
        contracts.get_extractor("java").extract("java", "A.java", "class A {}\n")


def test_missing_grammar_is_extractor_missing_and_a_finding_never_a_fallback(no_tree_sitter):
    tg.set_backend(None)
    with pytest.raises(contracts.ExtractorMissing) as ei:
        contracts.get_extractor("shell").extract("shell", "a.sh", "f() { :; }\n")
    assert ei.value.lang == "shell" and "tree-sitter is not installed" in ei.value.reason
    f = tg.missing_finding("shell", "bin/x.sh")
    assert (f.level, f.code, f.path) == ("W", "dc.degraded-extractor", "bin/x.sh")
    assert "pip install 'dead-code-checker[treesitter]'" in f.msg and "not extracted" in f.msg
    assert tg.missing_finding("python", "a.py") is None  # no grammar needed
    assert tg.missing_finding("yaml", "a.yml") is None  # read by yamlite
    assert contracts.get_extractor("shell").probe("shell").code == "dc.degraded-extractor"
    assert contracts.get_extractor("python").probe("python") is None


def test_real_backend_glue_with_a_fake_binding(monkeypatch):
    """_real_backend / _TsParsed against a fake tree_sitter module: the query text is the data file, matches pass through."""
    seen = {}

    class Language:
        pass

    mod = type(sys)("fake_tree_sitter")
    mod.Language = Language
    Language.__module__ = "fake_tree_sitter"

    class Query:
        def __init__(self, language, src):
            seen.setdefault("queries", []).append(src)

    class QueryCursor:
        def __init__(self, q):
            self.q = q

        def matches(self, node):
            return [(0, {"def.func": [node]})]

    mod.Query, mod.QueryCursor = Query, QueryCursor
    monkeypatch.setitem(sys.modules, "fake_tree_sitter", mod)

    class Root:
        has_error = True

    class Parser:
        language = Language()

        def parse(self, data):
            seen["data"] = data
            return type("T", (), {"root_node": Root()})()

    monkeypatch.setattr(tsm, "get_parser", lambda g: seen.setdefault("grammar", g) and Parser())
    p = tg._real_backend("bash", b"f() { :; }")
    assert seen["grammar"] == "bash" and seen["data"] == b"f() { :; }" and p.errors == 1
    assert p.run("defs") == [(0, {"def.func": [p.root]})]
    assert "function_definition" in seen["queries"][0]
    assert p.run("nope") == []  # no such query file: no matches
    mod2 = type(sys)("old_ts")
    Language.__module__ = "old_ts"
    monkeypatch.setitem(sys.modules, "old_ts", mod2)
    with pytest.raises(LookupError, match="QueryCursor"):
        tg._real_backend("bash", b"")
    monkeypatch.setattr(tsm, "get_parser", lambda g: (_ for _ in ()).throw(LookupError("grammar bash is missing: x")))
    with pytest.raises(contracts.ExtractorMissing, match="grammar bash is missing"):
        tg.set_backend(None) or contracts.get_extractor("shell").extract("shell", "a.sh", "x")


# ====================================================================== bash: the real grammar (host dependent)
def _real_grammar():
    try:
        tsm.get_parser("bash")
        return ""
    except LookupError as exc:
        return str(exc)


@pytest.mark.skipif(
    bool(_real_grammar()), reason=f"tree-sitter bash grammar is not installed on this host: {_real_grammar()}"
)
def test_bash_with_the_real_grammar():
    tg.set_backend(None)
    f = facts(SH, "shell", "bin/x.sh")
    d = defs(f)
    assert {"main", "helper", "_noop", "empty"} <= set(d) and f.errors == 0
    r = {(x.name, x.kind, x.scope) for x in f.refs}
    assert ("helper", "call", "main") in r and ("main", "call", "") in r
    assert any(k == "include" and n.endswith("common.sh") for n, k, _ in r)
    assert [x.pattern for x in f.dyn] == ["$handler"]
    assert d["_noop"].stub and d["empty"].stub
    assert sorted((r.kind, r.target, r.strength) for r in f.roots) == [
        ("main", "", "soft"),
        ("manual", "main", "hard"),
    ]  # a statement at the top level: a script
    assert facts("f() {\n", "shell").errors == 1
