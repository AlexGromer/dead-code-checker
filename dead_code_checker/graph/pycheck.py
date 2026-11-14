"""graph.pycheck: the local, NON-EXECUTING check of a Python removal (`dead-code-checker graph fix --dry-run --verify`, graph.fixverify).

Nothing of the checked code is imported, run or evaluated: the text of the patched files is compiled (`compile` builds a code object
and never runs it), read with `ast` and `symtable`, and the other files of the tree are only parsed. Stdlib only. It is meant to run
as a separate process inside the strict box of graph.fixverify (bubblewrap: no network, the tree is read-only), and it is importable
for the tests and for a host without a sandbox (the report then says so).

    python3 -I pycheck.py REQUEST.json          prints {"ok": bool, "errors": ["path:line: message", ...]} (rc 0 whenever it ran)
    check(request) -> {"ok", "errors"}          request = {"tree": DIR, "files": {path: {"old": text, "new": text}},
                                                           "removed": [{"path": p, "qual": q}]}
    `tree` is the tree BEFORE the patch, read only: the new text of every patched file is in `files` and is read instead of the file
    of the tree, so a check needs no private copy of the tree.

What it proves (and what it does not: a dynamic use, getattr, a string name, a call through a plugin table is no business of a
static check; the runtime check, the tests on the execution target, is):
    syntax     every patched file compiles
    names      no global name that the old file bound and read is read in the new one without a binding (symtable: scopes, global
               and nonlocal declarations, comprehension and class scopes; builtins aside): the removal left no dangling reference
    imports    no other .py file of the tree imports a removed module-level name from the module of the patched file (`from a.b import
               name`, relative imports resolved against the importing file, `import a.b as m; m.name`, `from a import b; b.name`),
               and none reads one through `from a.b import *` (a name the importing file reads and binds nowhere itself; a name
               that a star import cannot bring, a `_private` one with no `__all__`, is not counted); a file that star-imports
               the patched module hands the removed names on to the files that star-import IT (followed to the end of the chain)
    __all__    no literal `__all__` of the patched file names a removed symbol
A removed method or nested definition is checked for syntax and names only: whom `obj.method` reaches is not decidable here.
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import re
import symtable
import sys
from typing import Dict, Iterable, List, Optional, Set, Tuple

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
    "site-packages",
}
STAR_IMPORT = re.compile(r"import\s*\*")
MODULE_DUNDERS = {
    "__file__",
    "__name__",
    "__doc__",
    "__package__",
    "__spec__",
    "__loader__",
    "__builtins__",
    "__path__",
    "__cached__",
    "__annotations__",
    "__class__",
    "__debug__",
}


def _parts(path: str) -> List[str]:
    """The dotted parts of the module a .py path is: `a/b/__init__.py` -> [a, b], `a/b.py` -> [a, b]."""
    p = path[:-3] if path.endswith(".py") else path
    parts = [x for x in p.split("/") if x]
    return parts[:-1] if parts and parts[-1] == "__init__" else parts


def _dotted(node: ast.AST) -> Optional[str]:
    names: List[str] = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
        return ".".join(reversed(names))
    return None


def _suffix_of(mod: str, target: List[str]) -> bool:
    """True when the absolute import `mod` can name the module whose path parts are `target` (a source root above it is fine)."""
    want = mod.split(".")
    return bool(want) and len(want) <= len(target) and target[len(target) - len(want) :] == want


def _symbols(source: str, filename: str = "<patched>") -> Tuple[Set[str], Set[str]]:
    """(defined, reads): the global names the module binds (assignment, import, def, class, `global` assignment in a function) and
    the ones it reads (module level, and `global`/free reads in the nested scopes)."""
    table = symtable.symtable(source, filename, "exec")
    defined: Set[str] = set()
    reads: Set[str] = set()

    def walk(t: symtable.SymbolTable, top: bool) -> None:
        for s in t.get_symbols():
            if s.is_declared_global() and s.is_assigned():
                defined.add(s.get_name())
            if top:
                if s.is_assigned() or s.is_imported() or s.is_namespace():
                    defined.add(s.get_name())
                if s.is_referenced():
                    reads.add(s.get_name())
            elif s.is_global() and s.is_referenced():
                reads.add(s.get_name())
        for c in t.get_children():
            walk(c, False)

    walk(table, True)
    return defined, reads


def _first_lines(source: str, wanted: Set[str]) -> Dict[str, int]:
    """{name: first line} of the `ast.Name` nodes of `source` that carry one of `wanted` (a name that has no node gets line 1)."""
    lines: Dict[str, int] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in wanted:
            lines[node.id] = min(lines.get(node.id, node.lineno), node.lineno)
    return {n: lines.get(n, 1) for n in sorted(wanted)}


def global_reads(source: str, filename: str = "<patched>") -> Dict[str, int]:
    """{name: first line} of the global names the module reads and never binds: no module-level assignment, import, def or class,
    no `global` assignment in a function, not a builtin. A star import hides nothing here (it is the stricter reading).
    """
    defined, reads = _symbols(source, filename)
    return _first_lines(source, {n for n in reads - defined if not hasattr(builtins, n) and n not in MODULE_DUNDERS})


def _all_names(tree: ast.AST) -> List[Tuple[str, int]]:
    """[(name, line)] of the literal strings of the module's `__all__ = [...]` / `+= [...]`."""
    out: List[Tuple[str, int]] = []
    for node in getattr(tree, "body", []):
        tgt = None
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            tgt = node.value
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == "__all__":
            tgt = node.value
        if isinstance(tgt, (ast.List, ast.Tuple)):
            out += [(e.value, e.lineno) for e in tgt.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return out


def _py_files(tree_dir: str) -> Iterable[str]:
    for d, dirs, files in os.walk(tree_dir):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not os.path.islink(os.path.join(d, x)))
        for f in sorted(files):
            p = os.path.join(d, f)
            if f.endswith(".py") and not os.path.islink(p):
                yield os.path.relpath(p, tree_dir).replace(os.sep, "/")


def star_exports(tree: ast.AST, names: Iterable[str]) -> Set[str]:
    """The part of `names` that `from <module> import *` can bring out of the module `tree`: with a literal `__all__` the names it
    lists, with a computed one (anything but plain assignments of lists or tuples) all of them, without one the names that do not
    start with an underscore."""
    binds = [
        n
        for n in getattr(tree, "body", [])
        if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets))
        or (
            isinstance(n, (ast.AugAssign, ast.AnnAssign))
            and isinstance(n.target, ast.Name)
            and n.target.id == "__all__"
        )
    ]
    if not binds:
        return {n for n in names if not n.startswith("_")}
    mentions = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "__all__")
    literal = mentions == len(binds) and all(isinstance(b.value, (ast.List, ast.Tuple)) for b in binds)
    listed = {n for n, _ in _all_names(tree)}
    return {n for n in names if n in listed} if literal else set(names)


def _from_target(node: ast.ImportFrom, pkg: List[str], target: List[str]) -> Tuple[bool, Set[str]]:
    """(is `node` a `from` import out of the module `target`, the imported names that are the module `target` itself as a submodule)."""
    if node.level:
        keep = len(pkg) - (node.level - 1)
        if keep < 0:
            return False, set()
        base = pkg[:keep] + (node.module.split(".") if node.module else [])
        return base == target, {a.name for a in node.names if base + [a.name] == target}
    mod = node.module or ""
    return _suffix_of(mod, target), {a.name for a in node.names if _suffix_of(f"{mod}.{a.name}", target)}


def importers(
    tree_dir: str,
    patched: str,
    names: Set[str],
    star: Optional[Set[str]] = None,
    overlay: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Where a file of the tree other than `patched` imports (or reaches through an imported module) one of the module-level `names`
    of the module that `patched` is: ["path:line: ..."]. `star` = the part of `names` that `from <module> import *` brings (default:
    all of them; see star_exports): a file that star-imports the module and reads such a name that it binds nowhere itself is a
    finding, and it hands the names on to the files that star-import IT (a chain of re-exports, followed to its end; a name that
    the re-exporting file defines itself stops there). `overlay` = {path: text} read instead of the file on disk (the other patched
    files as they are after the patch, so that the tree needs no private copy per candidate)."""
    star = set(names if star is None else star)
    parsed: Dict[str, Optional[Tuple[ast.AST, str]]] = {}

    def load(rel: str) -> Optional[Tuple[ast.AST, str]]:
        if rel not in parsed:
            parsed[rel] = None
            try:
                if rel in (overlay or {}):
                    text = (overlay or {})[rel]
                else:
                    with open(os.path.join(tree_dir, rel), encoding="utf-8") as fh:
                        text = fh.read()
                if any(n in text for n in names) or STAR_IMPORT.search(text):  # the cheap filter
                    parsed[rel] = (ast.parse(text), text)
            except (OSError, UnicodeDecodeError, SyntaxError, ValueError, RecursionError, MemoryError):
                pass
        return parsed[rel]

    files = list(_py_files(tree_dir))
    found: List[str] = []
    seen = {patched}  # the modules whose namespace carries the names (the patched one, then its star re-exporters)
    work: List[Tuple[str, Set[str], Set[str]]] = [(patched, set(names), star)]
    while work:
        mod, carried, carried_star = work.pop(0)
        target = _parts(mod)
        tail = patched if mod == patched else f"{patched} (re-exported by {mod})"
        for rel in files:
            info = load(rel) if rel not in (mod, patched) else None
            if info is None:
                continue
            tree, text = info
            pkg = _parts(rel)[:-1] if not rel.endswith("/__init__.py") else _parts(rel)
            aliases: Set[str] = set()  # names and dotted names that stand for the module itself
            stars: List[int] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if _suffix_of(a.name, target):
                            aliases.add(a.asname or a.name)
                elif isinstance(node, ast.ImportFrom):
                    same, subs = _from_target(node, pkg, target)
                    for a in node.names:
                        if a.name == "*":
                            if same:
                                stars.append(node.lineno)
                        elif same and a.name in carried:
                            found.append(
                                f"{rel}:{node.lineno}: imports `{a.name}`, which the patch removes from {tail}"
                            )
                        elif a.name in subs:
                            aliases.add(a.asname or a.name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in carried and _dotted(node.value) in aliases:
                    found.append(
                        f"{rel}:{node.lineno}: reads `{_dotted(node.value)}.{node.attr}`, which the patch removes from {tail}"
                    )
            if not stars:
                continue
            try:
                defined, reads = _symbols(text, rel)
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                continue  # a file that does not compile is broken with or without the patch
            hit = {n for n in carried_star if n in reads and n not in defined}
            found += [
                f"{rel}:{ln}: reads `{n}`, which the patch removes from {tail}; it comes in through `import *` at line {stars[0]}"
                for n, ln in _first_lines(text, hit).items()
            ]
            if (
                rel not in seen
            ):  # the file hands the names it does not define itself on to the files that star-import it
                seen.add(rel)
                handed = carried_star - defined
                work.append((rel, handed, star_exports(tree, handed)))
    return sorted(set(found))


def check(req: dict) -> dict:
    """The verdict of one candidate: {"ok": bool, "errors": [...]}, errors sorted (path, line, message), the first one is the reason."""
    errors: List[Tuple[str, int, str]] = []
    top: Dict[str, Set[str]] = {}
    overlay = {p: ft["new"] for p, ft in (req.get("files") or {}).items()}
    for r in req.get("removed", ()):
        if r["qual"] and "." not in r["qual"]:
            top.setdefault(r["path"], set()).add(r["qual"])
    for path, ft in sorted((req.get("files") or {}).items()):
        try:
            compile(ft["new"], path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            errors.append(
                (
                    path,
                    int(getattr(exc, "lineno", 0) or 0),
                    f"does not compile ({type(exc).__name__}: {getattr(exc, 'msg', exc)})",
                )
            )
            continue
        try:
            before, after = global_reads(ft["old"], path), global_reads(ft["new"], path)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            before, after = {}, {}
        errors += [(path, ln, f"`{n}` is read but no longer defined") for n, ln in after.items() if n not in before]
        gone = top.get(path, set())
        if gone:
            errors += [
                (path, ln, f"__all__ names `{n}`, which the patch removes")
                for n, ln in _all_names(ast.parse(ft["new"]))
                if n in gone
            ]
            try:
                brought = star_exports(ast.parse(ft["old"]), gone)
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                brought = set(gone)  # the old text does not parse: nothing is known about what a star import brings
            errors += [
                (p, int(ln), m)
                for p, ln, m in (_split(x) for x in importers(req["tree"], path, gone, brought, overlay))
            ]
    errors.sort()
    return {"ok": not errors, "errors": [f"{p}:{ln}: {m}" if ln else f"{p}: {m}" for p, ln, m in errors]}


def _split(row: str) -> Tuple[str, str, str]:
    path, line, msg = row.split(":", 2)
    return path, line, msg.strip()


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print("usage: pycheck.py REQUEST.json", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        req = json.load(fh)
    json.dump(check(req), sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
