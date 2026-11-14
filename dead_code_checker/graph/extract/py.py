"""graph.extract.py: Python facts through the stdlib `ast`.

Reuses the v1 text checker (dead_code_checker.core) instead of copying it: py_parse, py_symbols (which top-level names are symbols, constants included),
py_stub_reason (stub body), py_hook_stub_reason (a whole-file hook stub), SAFE_DECORATORS and the dotted-name helpers. The
result is a function of the text only (contracts.py); a syntax error is Facts.errors = 1 with no facts (ast has no partial tree).

What the Facts mean (link.py and roots.py read exactly this)
  defs     qual "" = the module itself (kind module; its `stub` is py_hook_stub_reason of the file when the file is at most HOOK_STUB_MAX_CHARS, F_STUB unset);
           "f", "Cls", "Cls.meth", "outer.inner" (a def nested in a def: kind func, vis private), "Cls.CONST", "CONST".
           A def inside if/try/with/for/while/match is found too; a name defined twice is one Def (common.merge_defs).
           kind: func | method | class | const. vis: private (leading "_", not a dunder; a nested def), exported (named in
           a literal __all__), else public. flags: decorated (a decorator outside SAFE_DECORATORS and the member decorators
           below), stub (py_stub_reason), abstract (abstractmethod, overload), override, protocol (a dunder method, a
           Protocol class and its members). arity is -1: Python has no overloads, so node keys carry no arity.
           bh / bsz: the normalised body hash of a func or method that is no stub and has at least data/lens.json "dup" "store_min" ast nodes
           (graph.extract.bodyhash: docstring, comments, annotations, the function's name and local names do not count); the duplicates
           lens of the viewer reads it
  refs     one per use: name as written ("f", "a.b.f", "self.m"; "?.m" when the receiver is an expression such as f().m),
           kind call | ref | inherit, scope = qual of the enclosing def ("" = module level). A bare name bound in the enclosing
           function (parameter, assignment target, except/with/for/match capture) is NOT a ref: it is local. Attributes never
           are skipped. Decorators and defaults belong to the ENCLOSING scope (they run when the def runs). A constant
           getattr(x, "n") is the ref "x.n"; names in a literal __all__ are refs from the module; string annotations are parsed.
  imports  one Import per bound name: `import a.b as c` -> (a.b, (), c); `from m import x as y` -> (m, (x,), y, level);
           `from m import *` -> names ("*",); importlib.import_module("m") and __import__("m") with a constant are imports too.
  roots    main (the `if __name__ == "__main__"` guard, target "" = the module; hard, convention; a bare call statement at module level
           (`main()`, `show(load(p))`) outside the guard makes the file a script: soft main on the module, what the statements call is used; link.py drops it for a test file and for a package `__init__`), export (a name defined
           here and listed in a literal __all__; soft, convention), annotation:<decorator as written> (a def with a decorator
           outside the safe set, e.g. annotation:mcp.tool, annotation:app.route; hard, convention: the framework calls it), annotation:<id> (a top-level
           class that a framework instantiates by its name and calls by contract, data/frameworks.json py_class_roots: the Ansible plugin classes
           `FilterModule` with filters(), `LookupModule(LookupBase)` ...; the class and the contract methods it defines; hard, convention; graph.link
           keeps these hints only for a file that is a plugin of the framework, see the scope of that table).
  dyn      getattr(x, <non-constant>) -> "getattr(x, *)"; importlib.import_module / __import__ with a non-constant argument;
           module:<name or glob> for a module loaded by name: spec_from_file_location("m", ..), a call of a wrapper that hands its parameter on to a
           loader (`_module("plugin_one")`), a computed name with a literal prefix (f"pkg.{info.name}" is pkg.*, narrowed by the
           `startswith("probe_")` tests of the function to pkg.probe_*): the linker roots the matching modules (graph.link, D);
           globals()[<non-constant>].
  xlang    exec | lit | ffi | env refs: what the file says to other languages (a command it runs, a path string, a native symbol, an environment
           variable), see extract.xlang.
Nothing is read from the path and nothing is executed.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from functools import lru_cache
from typing import Dict, FrozenSet, List, Optional, Tuple

from ..contracts import Extractor, register_extractor
from ..model import (
    F_ABSTRACT,
    F_DECORATED,
    F_OVERRIDE,
    F_PROTOCOL,
    F_STUB,
    Def,
    DynSite,
    Facts,
    Import,
    Ref,
    RootHint,
)
from . import bodyhash, xlang
from .common import merge_defs, module_def, text_checker

# decorators that only describe a member: they do not register the def anywhere (SAFE_DECORATORS of core.py + these)
MEMBER_DECORATORS = frozenset(
    {
        "property",
        "setter",
        "getter",
        "deleter",
        "abstractmethod",
        "abstractproperty",
        "overload",
        "override",
        "abstractclassmethod",
        "abstractstaticmethod",
    }
)
HOOK_STUB_MAX_CHARS = (
    20000  # a hook stub is tiny; py_hook_stub_reason walks the whole tree several times, so a big file is not asked
)
ABSTRACT = frozenset({"abstractmethod", "abstractproperty", "overload", "abstractclassmethod", "abstractstaticmethod"})
IMPORT_FNS = frozenset({"importlib.import_module", "import_module", "__import__", "importlib.__import__"})
LOADER_LAST = frozenset(
    {"import_module", "__import__", "spec_from_file_location", "find_spec"}
)  # a module loaded by its name
LOADER_RX = re.compile("|".join(sorted(LOADER_LAST)))
MODNAME = re.compile(r"[A-Za-z_][\w.]*\Z")
_SCOPES = tuple(
    getattr(ast, n)
    for n in (
        "FunctionDef",
        "AsyncFunctionDef",
        "ClassDef",
        "Lambda",
        "ListComp",
        "SetComp",
        "DictComp",
        "GeneratorExp",
    )
)
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_MATCH_NAMES = tuple(getattr(ast, n) for n in ("MatchAs", "MatchStar") if hasattr(ast, n))


def _is_main_guard(t) -> bool:
    return (
        isinstance(t, ast.Compare)
        and isinstance(t.left, ast.Name)
        and t.left.id == "__name__"
        and len(t.ops) == 1
        and isinstance(t.ops[0], ast.Eq)
        and isinstance(t.comparators[0], ast.Constant)
        and t.comparators[0].value == "__main__"
    )


def _script_line(stmts) -> int:
    """Line of the first statement at module level (inside if / for / try / with too, never in a def or class, never under the main guard,
    which is a root of its own) that only runs code: a bare call such as `main()`, `show(load(p))`, `sys.exit(main())`. 0 = none.
    """
    for s in stmts:
        if isinstance(s, ast.Expr) and isinstance(s.value, (ast.Call, ast.Await)):
            return s.lineno
        if isinstance(s, ast.If) and _is_main_guard(s.test):
            continue
        for sub in _substmts(s):
            if line := _script_line(sub):
                return line
    return 0


def _lit_prefix(n) -> str:
    """The text a module name starts with before its first computed part: "pkg.mod", f"pkg.{x}", "pkg." + x, "pkg.%s" % x, "pkg.{}".format(x)."""
    if isinstance(n, ast.Constant):
        return n.value if isinstance(n.value, str) else ""
    if isinstance(n, ast.JoinedStr):
        head = []
        for v in n.values:
            if not (isinstance(v, ast.Constant) and isinstance(v.value, str)):
                break
            head.append(v.value)
        return "".join(head)
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
        return _lit_prefix(n.left) + (_lit_prefix(n.right) if isinstance(n.left, ast.Constant) else "")
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod):
        return _lit_prefix(n.left).partition("%")[0]
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "format":
        return _lit_prefix(n.func.value).partition("{")[0]
    return ""


def _starts(fn) -> List[str]:
    """The literals the function tests names with (`info.name.startswith("probe_")`): a discovery loop picks its modules by prefix."""
    found = (
        c
        for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "startswith"
    )
    return sorted(
        {
            c.args[0].value
            for c in found
            if c.args and isinstance(c.args[0], ast.Constant) and isinstance(c.args[0].value, str)
        }
    )


def _loader_wrappers(tree) -> Dict[str, Tuple[int, ...]]:
    """{name: indexes of the arguments} of the functions that build the module name or the path of a loader from their parameters (`def
    _module(name): spec_from_file_location(name, os.path.join(HERE, name + ".py"))`, `import_module(f"pkg.{name}")`): a call of one with a
    constant is a module loaded by that name."""
    out: Dict[str, set] = {}
    for f in ast.walk(tree):
        if not isinstance(f, _FUNCS):
            continue
        params = [a.arg for a in (*f.args.posonlyargs, *f.args.args)]
        skip = 1 if params[:1] in (["self"], ["cls"]) else 0
        for c in ast.walk(f):
            if isinstance(c, ast.Call) and c.args and _chain(c.func)[0].rsplit(".", 1)[-1] in LOADER_LAST:
                used = {n.id for a in c.args[:2] for n in ast.walk(a) if isinstance(n, ast.Name)}
                out.setdefault(f.name, set()).update(params.index(p) - skip for p in used if p in params)
    return {k: tuple(sorted(i for i in v if i >= 0)) for k, v in out.items() if any(i >= 0 for i in v)}


def _dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _chain(n) -> Tuple[str, Optional[ast.AST]]:
    """('a.b.c', None) for a Name/Attribute chain; ('?.c', root expression) when the chain starts at another expression."""
    parts = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        parts.append(n.id)
        return ".".join(reversed(parts)), None
    parts.append("?")
    return ".".join(reversed(parts)), n


def _is_protocol(base) -> bool:
    """A base named Protocol: Protocol, typing.Protocol and the generic Protocol[T]."""
    return _chain(base.value if isinstance(base, ast.Subscript) else base)[0].rsplit(".", 1)[-1] == "Protocol"


FRAMEWORKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "frameworks.json")


@lru_cache(maxsize=1)
def _class_rules() -> Tuple[Dict[str, Tuple[dict, ...]], str]:
    """({class name: its rules}, sha of the table): data/frameworks.json py_class_roots (a framework that instantiates a class by its name)."""
    with open(FRAMEWORKS, encoding="utf-8") as fh:
        rules = json.load(fh).get("py_class_roots", {}).get("rules", [])
    by: Dict[str, List[dict]] = {}
    for r in rules:
        by.setdefault(r["class"], []).append(r)
    sha = hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()
    return {k: tuple(v) for k, v in by.items()}, sha


def _class_hooks(n: ast.ClassDef, qual: str) -> List[RootHint]:
    """Roots of a top-level class that a framework builds by its name (py_class_roots): the class and the contract methods it defines, hard.
    The class qualifies when it defines one of the rule's methods or has one of its bases; a rule with neither is qualified by the name.
    """
    out: List[RootHint] = []
    rules = _class_rules()[0].get(n.name, ())
    if not rules:
        return out
    meths = {b.name: b.lineno for b in n.body if isinstance(b, _FUNCS)}
    bases = {_chain(b.value if isinstance(b, ast.Subscript) else b)[0].rsplit(".", 1)[-1] for b in n.bases}
    for r in rules:
        want = set(r.get("methods", ()))
        if (want & meths.keys()) or (set(r.get("bases", ())) & bases) or not (want or r.get("bases")):
            kind = "annotation:" + r["id"]
            out.append(RootHint(kind, qual, n.lineno, "hard", "convention"))
            out += [RootHint(kind, f"{qual}.{m}", meths[m], "hard", "convention") for m in sorted(want & meths.keys())]
    return out


def _bound(body) -> FrozenSet[str]:
    """Names a function body binds itself (assignment, del, for/with/except/match targets), not those of nested scopes, not
    imports and not nested def names (a call of those is a ref the linker resolves by scope), minus global/nonlocal names.
    """
    out, glob, stack = set(), set(), list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, _SCOPES):
            continue
        if isinstance(n, ast.Name):
            if isinstance(n.ctx, (ast.Store, ast.Del)):
                out.add(n.id)
        elif isinstance(n, ast.ExceptHandler):
            if n.name:
                out.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            glob.update(n.names)
        elif _MATCH_NAMES and isinstance(n, _MATCH_NAMES):
            if n.name:
                out.add(n.name)
        elif hasattr(ast, "MatchMapping") and isinstance(n, ast.MatchMapping) and n.rest:
            out.add(n.rest)
        stack.extend(ast.iter_child_nodes(n))
    return frozenset(out - glob)


def _arg_names(a: ast.arguments) -> List[ast.arg]:
    return [*a.posonlyargs, *a.args, *a.kwonlyargs, *([a.vararg] if a.vararg else []), *([a.kwarg] if a.kwarg else [])]


def _strs(v) -> List[Tuple[str, int]]:
    """String constants of a literal list/tuple (and of `a + b` of such)."""
    if isinstance(v, (ast.List, ast.Tuple)):
        return [(e.value, e.lineno) for e in v.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add):
        return _strs(v.left) + _strs(v.right)
    return []


def _all_names(tree) -> List[Tuple[str, int]]:
    out = []
    for s in tree.body:
        if isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in s.targets):
            out += _strs(s.value)
        elif (
            isinstance(s, (ast.AnnAssign, ast.AugAssign))
            and isinstance(s.target, ast.Name)
            and s.target.id == "__all__"
            and s.value is not None
        ):
            out += _strs(s.value)
        elif isinstance(s, ast.Expr) and isinstance(s.value, ast.Call) and isinstance(s.value.func, ast.Attribute):
            f = s.value.func
            if isinstance(f.value, ast.Name) and f.value.id == "__all__" and f.attr in ("extend", "append"):
                for a in s.value.args:
                    out += _strs(a) or (
                        [(a.value, a.lineno)] if isinstance(a, ast.Constant) and isinstance(a.value, str) else []
                    )
    return out


def _substmts(s):
    """The statement lists inside a compound statement that is not itself a def or class."""
    if isinstance(s, (*_FUNCS, ast.ClassDef)):
        return
    for field in ("body", "orelse", "finalbody"):
        v = getattr(s, field, None)
        if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
            yield v
    for h in (*getattr(s, "handlers", ()), *getattr(s, "cases", ())):
        yield h.body


class _XPass:
    """The cross-language pass of the Python extractor (xlang.PyScan) under a guard: a bug in it costs that pass (the refs it found so
    far are dropped, errors + 1 in the Facts), like the tree-sitter path does, never the defs and refs of the file."""

    def __init__(self, tree):
        self.scan, self.err = None, 0
        try:
            self.scan = xlang.PyScan(tree)
        except Exception:  # noqa: BLE001
            self.err = 1

    def _run(self, name: str, *args):
        if self.scan is None:
            return ()
        try:
            return getattr(self.scan, name)(*args)
        except Exception:  # noqa: BLE001
            self.scan, self.err = None, 1
            return ()

    def const(self, n, scope):
        self._run("const", n, scope)

    def attribute(self, n, scope):
        self._run("attribute", n, scope)

    def call(self, n, scope):
        self._run("call", n, scope)

    def subscript(self, n, scope):
        self._run("subscript", n, scope)

    def finish(self):
        return self._run("finish")


class _Build:
    def __init__(self, dc, tree, text: str):
        self.dc, self.tree, self.text = dc, tree, text
        self.safe = frozenset(dc.SAFE_DECORATORS) | MEMBER_DECORATORS
        self.exports = {n for n, _ in _all_names(tree)}
        self.defs: List[Def] = []
        self.qual_of: Dict[int, str] = {}
        self.refs: List[Ref] = []
        self.imports: List[Import] = []
        self.roots: List[RootHint] = []
        self.dyn: List[DynSite] = []
        self.wrappers = _loader_wrappers(tree) if LOADER_RX.search(text) else {}
        self.fnode: Dict[str, ast.AST] = {}  # qual -> its def, for the prefixes a discovery loop tests
        self.xl = _XPass(tree)  # exec, lit, ffi and env refs (extract.xlang), under a guard

    # ------------------------------------------------------------ definitions
    def vis(self, name: str, ctx: str) -> str:
        if ctx == "func":
            return "private"
        if name.startswith("_") and not _dunder(name):
            return "private"
        return "exported" if ctx == "module" and name in self.exports else "public"

    def collect(self, stmts, prefix: str, ctx: str, proto: bool) -> None:
        dc = self.dc
        for s in dc.py_symbols(ast.Module(list(stmts), [])):
            n, qual = s.node, prefix + s.name
            if s.kind == "constant":
                if ctx != "func":  # a constant inside a function is a local, not a symbol
                    self.defs.append(Def(qual, "const", s.start, s.end, self.vis(s.name, ctx)))
                continue
            is_class = isinstance(n, ast.ClassDef)
            names = [(dc._dotted(d.func if isinstance(d, ast.Call) else d), dc._deco_name(d)) for d in n.decorator_list]
            flags = F_DECORATED if any(last not in self.safe for _, last in names) else 0
            flags |= F_ABSTRACT if any(last in ABSTRACT for _, last in names) else 0
            flags |= F_OVERRIDE if any(last == "override" for _, last in names) else 0
            is_proto = is_class and any(_is_protocol(b) for b in n.bases)
            if is_proto or (not is_class and ((ctx == "class" and _dunder(s.name)) or proto)):
                flags |= F_PROTOCOL  # a typing.Protocol class and its members: structural, never named by the code that satisfies them
            stub = "" if is_class else (dc.py_stub_reason(n) or "")
            bh, bsz = bodyhash.NONE if is_class or stub else bodyhash.py_body(n)  # the duplicates lens (data/lens.json)
            self.defs.append(
                Def(
                    qual,
                    "class" if is_class else ("method" if ctx == "class" else "func"),
                    s.start,
                    s.end,
                    self.vis(s.name, ctx),
                    flags | (F_STUB if stub else 0),
                    -1,
                    stub,
                    bh,
                    bsz,
                )
            )
            self.qual_of[id(n)] = qual
            for d, (full, last) in zip(n.decorator_list, names):
                if full and last not in self.safe:
                    self.roots.append(RootHint(f"annotation:{full}", qual, d.lineno, "hard", "convention"))
            if is_class:
                if ctx == "module":
                    self.roots.extend(_class_hooks(n, qual))
                self.collect(n.body, qual + ".", "class", proto or is_proto)
            else:
                self.collect(n.body, qual + ".", "func", False)
        for s in stmts:
            for sub in _substmts(s):
                self.collect(sub, prefix, ctx, proto)

    # ------------------------------------------------------------ uses
    def add(self, name: str, kind: str, node, scope: str) -> None:
        self.refs.append(Ref(name, kind, getattr(node, "lineno", 0), scope))

    def visit(self, n, scope: str, locs: Tuple[FrozenSet[str], ...]) -> None:
        m = getattr(self, "v_" + type(n).__name__, None)
        if m is not None:
            m(n, scope, locs)
            return
        for c in ast.iter_child_nodes(n):
            self.visit(c, scope, locs)

    def expr_ref(self, n, kind: str, scope: str, locs) -> None:
        name, base = _chain(n)
        if base is None:
            if not (isinstance(n, ast.Name) and any(n.id in loc for loc in locs)):
                self.add(name, kind, n, scope)
        else:
            if not isinstance(base, ast.Constant):  # "x".join(...): not a name of ours
                self.add(name, kind, n, scope)
            self.visit(base, scope, locs)

    def ann(self, n, scope: str, locs) -> None:
        if isinstance(n, ast.Constant) and isinstance(n.value, str):  # a forward reference "Foo"
            try:
                inner = ast.parse(n.value.strip(), mode="eval").body
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                return
            ast.increment_lineno(inner, n.lineno - 1)
            n = inner
        self.visit(n, scope, locs)

    def v_Name(self, n, scope, locs):
        if isinstance(n.ctx, ast.Load):
            self.expr_ref(n, "ref", scope, locs)

    def v_Constant(self, n, scope, locs):
        self.xl.const(n, scope)

    def v_Attribute(self, n, scope, locs):
        if isinstance(n.ctx, ast.Load):
            self.xl.attribute(n, scope)
            self.expr_ref(n, "ref", scope, locs)
        else:
            self.visit(n.value, scope, locs)

    def v_Call(self, n, scope, locs):
        self.xl.call(n, scope)
        f, name = n.func, ""
        if isinstance(f, ast.Attribute):
            self.xl.attribute(f, scope)
        if isinstance(f, (ast.Name, ast.Attribute)):
            name = _chain(f)[0]
            self.expr_ref(f, "call", scope, locs)
        else:
            self.visit(f, scope, locs)
        for a in n.args:
            self.visit(a, scope, locs)
        for k in n.keywords:
            self.visit(k.value, scope, locs)
        if name == "getattr" and len(n.args) >= 2:
            recv = _chain(n.args[0])[0] if isinstance(n.args[0], (ast.Name, ast.Attribute)) else "?"
            a = n.args[1]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                self.refs.append(Ref(f"{recv}.{a.value}", "ref", n.lineno, scope))
            else:
                self.dyn.append(DynSite(f"getattr({recv}, *)", n.lineno, scope))
        elif name in IMPORT_FNS and n.args:
            a = n.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                mod = a.value.lstrip(".")
                self.imports.append(Import(mod, (), "", n.lineno, len(a.value) - len(mod), scope))
            else:
                self.dyn.append(DynSite(f"{name.rsplit('.', 1)[-1]}(*)", n.lineno, scope))
                self.by_name(a, n.lineno, scope)
        elif name.rsplit(".", 1)[-1] in LOADER_LAST and n.args:
            self.by_name(n.args[0], n.lineno, scope)
        for idx in self.wrappers.get(name.rsplit(".", 1)[-1], ()):
            if len(n.args) > idx:
                self.by_name(n.args[idx], n.lineno, scope)

    def by_name(self, a, line: int, scope: str) -> None:
        """A module loaded by name (importlib, spec_from_file_location, a wrapper of those): the dyn site `module:<glob>` roots the matching
        modules. A constant is the module; a computed name is its literal prefix (and the prefix tests of the enclosing function:
        `startswith("probe_")` after f"pkg.{name}" is pkg.probe_*); a name that is none of these (a parameter) is the wrapper's business.
        """
        head = _lit_prefix(a)
        if isinstance(a, ast.Constant):
            if MODNAME.match(head.lstrip(".")):  # a module name, not a path or a prose string
                self.dyn.append(DynSite(f"module:{head.lstrip('.')}", line, scope))
            return
        fn = self.fnode.get(scope)
        for lit in (_starts(fn) if fn is not None else ()) or ([""] if head else []):
            if head or lit:
                self.dyn.append(DynSite(f"module:{head}{lit}*", line, scope))

    def v_Subscript(self, n, scope, locs):
        self.xl.subscript(n, scope)
        v = n.value
        if (
            isinstance(v, ast.Call)
            and isinstance(v.func, ast.Name)
            and v.func.id in ("globals", "locals", "vars")
            and not v.args
        ):
            if isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
                self.refs.append(Ref(n.slice.value, "ref", n.lineno, scope))
            else:
                self.dyn.append(DynSite(f"{v.func.id}()[*]", n.lineno, scope))
                self.visit(n.slice, scope, locs)
            return
        self.visit(v, scope, locs)
        self.visit(n.slice, scope, locs)

    def v_Import(self, n, scope, locs):
        for a in n.names:
            self.imports.append(Import(a.name, (), a.asname or "", n.lineno, 0, scope))

    def v_ImportFrom(self, n, scope, locs):
        for a in n.names:
            self.imports.append(Import(n.module or "", (a.name,), a.asname or "", n.lineno, n.level or 0, scope))

    def v_If(self, n, scope, locs):
        if _is_main_guard(n.test):
            self.roots.append(RootHint("main", "", n.lineno, "hard", "convention"))
        for c in ast.iter_child_nodes(n):
            self.visit(c, scope, locs)

    def v_AnnAssign(self, n, scope, locs):
        self.ann(n.annotation, scope, locs)
        self.visit(n.target, scope, locs)
        if n.value is not None:
            self.visit(n.value, scope, locs)

    def v_FunctionDef(self, n, scope, locs):
        a = n.args
        for d in n.decorator_list:
            self.visit(d, scope, locs)
        for d in [*a.defaults, *[k for k in a.kw_defaults if k is not None]]:
            self.visit(d, scope, locs)
        for x in _arg_names(a):
            if x.annotation is not None:
                self.ann(x.annotation, scope, locs)
        if n.returns is not None:
            self.ann(n.returns, scope, locs)
        inner = (*locs, frozenset(x.arg for x in _arg_names(a)) | _bound(n.body))
        q = self.qual_of.get(id(n), scope)
        self.fnode[q] = n
        for s in n.body:
            self.visit(s, q, inner)

    v_AsyncFunctionDef = v_FunctionDef

    def v_ClassDef(self, n, scope, locs):
        for d in n.decorator_list:
            self.visit(d, scope, locs)
        q = self.qual_of.get(id(n), scope)
        for b in n.bases:
            if isinstance(b, (ast.Name, ast.Attribute)):
                self.expr_ref(
                    b, "inherit", q, locs
                )  # the scope is the CLASS: the linker reads its bases (override, external base)
            else:
                self.visit(b, scope, locs)
        for k in n.keywords:
            self.visit(k.value, scope, locs)
        for s in n.body:
            self.visit(s, q, locs)

    def v_Lambda(self, n, scope, locs):
        a = n.args
        for d in [*a.defaults, *[k for k in a.kw_defaults if k is not None]]:
            self.visit(d, scope, locs)
        self.visit(n.body, scope, (*locs, frozenset(x.arg for x in _arg_names(a))))

    def v_ListComp(self, n, scope, locs):
        names = {m.id for g in n.generators for m in ast.walk(g.target) if isinstance(m, ast.Name)}
        inner = (*locs, frozenset(names))
        for i, g in enumerate(n.generators):  # the first iterable is evaluated outside the comprehension
            self.visit(g.iter, scope, locs if i == 0 else inner)
            self.visit(g.target, scope, inner)
            for c in g.ifs:
                self.visit(c, scope, inner)
        for part in (getattr(n, "elt", None), getattr(n, "key", None), getattr(n, "value", None)):
            if part is not None:
                self.visit(part, scope, inner)

    v_SetComp = v_GeneratorExp = v_DictComp = v_ListComp

    # ------------------------------------------------------------ whole file
    def facts(self, lang: str) -> Facts:
        dc = self.dc
        self.collect(self.tree.body, "", "module", False)
        self.visit(self.tree, "", ())
        defined = {d.qual for d in self.defs if "." not in d.qual}
        for name, line in _all_names(self.tree):
            self.refs.append(Ref(name, "ref", line, ""))
            if name in defined:
                self.roots.append(RootHint("export", name, line, "soft", "convention"))
        if line := _script_line(
            self.tree.body
        ):  # a script: running the file runs its module-level calls (soft: a library may do so too)
            self.roots.append(RootHint("main", "", line, "soft", "convention"))
        hook = dc.py_hook_stub_reason(self.tree) if len(self.text) <= HOOK_STUB_MAX_CHARS else None
        mod = module_def(self.text, hook or "")
        return Facts(
            lang,
            tuple(merge_defs([mod, *self.defs])),
            tuple(self.refs) + tuple(self.xl.finish()),
            tuple(self.imports),
            tuple(self.roots),
            tuple(self.dyn),
            self.xl.err,
        )


@lru_cache(maxsize=1)
def _detector_sha() -> str:
    """Fingerprint of the code whose verdicts are baked into the facts (py_symbols, the stub detectors, the decorator
    lists live in core.py): part of the shard key through grammar_sha, so a change there invalidates the shards."""
    with open(text_checker().__file__, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


@lru_cache(maxsize=1)
def _body_sha() -> str:
    """The code of the body hash (graph.extract.bodyhash) decides Def.bh: part of the shard key."""
    with open(bodyhash.__file__, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


class PyExtractor(Extractor):
    # 2: cross-language facts (xlang); 3: inherit refs in the class scope; 4: Protocol classes, script statements, loaders; 5: loaders named by the path of the loader;
    # 6: classes that a framework builds by name (py_class_roots); 7: the normalised body hash of functions and methods (Def.bh / bsz, graph.extract.bodyhash)
    name, version, langs = "py", 7, ("python",)

    def grammar_sha(self, lang: str) -> str:
        return hashlib.sha256(
            f"{_detector_sha()}|{xlang.sha()}|{_class_rules()[1]}|{bodyhash.data_sha()}|{_body_sha()}".encode()
        ).hexdigest()

    def extract(self, lang: str, path: str, text: str) -> Facts:
        dc = text_checker()
        tree = dc.py_parse(text)
        if tree is None:
            return Facts(lang, errors=1)
        try:
            return _Build(dc, tree, text).facts(lang)
        except RecursionError:  # a pathologically deep expression: report it, do not crash the batch
            return Facts(lang, errors=1)


register_extractor(PyExtractor())
