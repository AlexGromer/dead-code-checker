"""graph.extract.bodyhash: the normalised body hash of a definition (the duplicates lens of the viewer; data/lens.json "dup" and "norm").

    py_body(fn) -> (hash, size)           a python function: the ast of its body and parameters, as a token stream with the docstring, the
                                          annotations and the function's own name left out and every local name (parameters, assigned,
                                          loop, with, except and comprehension targets, nested defs) renamed by first occurrence. Comments
                                          and whitespace are not in an ast. size = ast nodes
    text_body(lang, text, name_span) -> (hash, size)    a tree-sitter language: the text of the definition node (the extractor has it at hand with
                                          the span of the name), tokenised, comments removed (per language, data), the name left out, the
                                          parameters and the names that are assigned or declared renamed by first occurrence. size = tokens
    data_sha()                            sha of data/lens.json: part of the extractors' shard key (a data edit changes the facts)
    store_min()                           the size from which a hash is kept ("" and 0 below it)

hash = first 16 hex of sha1 over the token stream, prefixed by the language family ("py:", "ts:"): bodies of different languages never group.
Deterministic, no clock, no environment. A body bigger than data "dup" "max_body" tokens has no hash (a generated blob is no duplicate).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "lens.json")
NONE: Tuple[str, int] = ("", 0)
_ID = re.compile(r"[^\W\d]\w*")


@lru_cache(maxsize=1)
def _data() -> Tuple[dict, str]:
    with open(DATA, "rb") as fh:
        raw = fh.read()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()[:12]


def data_sha() -> str:
    return _data()[1]


def store_min() -> int:
    return int(_data()[0]["dup"]["store_min"])


def _max_body() -> int:
    return int(_data()[0]["dup"]["max_body"])


def _digest(prefix: str, toks: List[str]) -> str:
    return prefix + hashlib.sha1("\x1f".join(toks).encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------- python (ast)
_SKIP = frozenset({"ctx", "type_comment", "annotation", "returns", "type_params"})


def _py_locals(fn) -> List[str]:
    """The local names of a function in the order of their first occurrence (parameters first), minus `global` / `nonlocal` names."""
    out: Dict[str, None] = {}
    outer = set()
    a = fn.args
    for x in [*a.posonlyargs, *a.args, a.vararg, *a.kwonlyargs, a.kwarg]:
        if x is not None:
            out.setdefault(x.arg, None)
    for n in ast.walk(fn):
        if n is fn:
            continue
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            outer.update(n.names)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.setdefault(n.id, None)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.setdefault(n.name, None)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.setdefault(n.name, None)
        elif isinstance(n, ast.arg):
            out.setdefault(n.arg, None)
    return [k for k in out if k not in outer]


def py_body(fn) -> Tuple[str, int]:
    """(hash, size) of a FunctionDef / AsyncFunctionDef, NONE when it is smaller than store_min or too big."""
    toks: List[str] = []
    nodes = [0]
    rename = {k: f"${i}" for i, k in enumerate(_py_locals(fn))}
    cap = _max_body()

    def walk(n) -> None:
        if len(toks) > cap:
            return
        if isinstance(n, list):
            toks.append("[")
            for x in n:
                walk(x)
            toks.append("]")
            return
        if not isinstance(n, ast.AST):
            toks.append(f"{type(n).__name__}:{n!r}")
            return
        t = type(n).__name__
        nodes[0] += 1
        if isinstance(n, ast.Name):
            toks.append("N:" + rename.get(n.id, n.id))
            return
        if isinstance(n, ast.arg):
            toks.append("A:" + rename.get(n.arg, n.arg))
            return
        toks.append(t)
        for f in n._fields:
            if f in _SKIP:
                continue
            v = getattr(n, f, None)
            if f == "name" and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler)):
                toks.append(rename.get(v, v) if isinstance(v, str) else "")  # a nested def or handler name is a local
                continue
            if f == "body" and isinstance(v, list) and v and _docstring(v[0]):
                v = v[1:]
            toks.append(f + "=")
            walk(v)

    try:
        walk(fn.args)
        walk(fn.body[1:] if fn.body and _docstring(fn.body[0]) else fn.body)
        walk(fn.decorator_list)
    except RecursionError:
        return NONE
    size = nodes[0] + 1  # + the function itself
    if size < store_min() or len(toks) > cap:
        return NONE
    return _digest("py:", toks), size


def _docstring(s) -> bool:
    return isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str)


# ---------------------------------------------------------------- tree-sitter languages (text)
@lru_cache(maxsize=64)
def _lex(lang: str) -> "re.Pattern":
    c = _data()[0]["norm"]["comments"]
    spec = c.get(lang) or c["default"]
    parts = [re.escape(b) + r"[\s\S]*?" + re.escape(e) for b, e in spec["block"]]
    parts += [re.escape(p) + r"[^\n]*" for p in spec["line"]]
    pat = "|".join(f"(?:{p})" for p in parts)
    cm = f"(?P<c>{pat})|" if pat else ""
    return re.compile(
        cm
        + r"""(?P<s>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\[\s\S]|[^`\\])*`)|(?P<n>\d[\w.]*)|(?P<i>[^\W\d]\w*)|(?P<w>\s+)|(?P<o>:=|==|!=|<=|>=|=>|->|::|\+\+|--|&&|\|\||<<|>>|[-+*/%&|^]=|[\s\S])"""
    )


def _tokens(lang: str, text: str) -> List[Tuple[str, str]]:
    out = []
    for m in _lex(lang).finditer(text):
        k = m.lastgroup
        if k in ("c", "w"):
            continue
        out.append((k, m.group()))
    return out


def _param_names(toks: List[Tuple[str, str]], lo: int, style: str, skip: frozenset) -> Tuple[set, int]:
    """The parameter names of the first parenthesis group at or after `lo`; the index after its closing bracket."""
    i = lo
    while i < len(toks) and toks[i][1] != "(":
        if toks[i][1] in ("{", ";", "="):
            return set(), lo
        i += 1
    depth, seg, segs = 0, [], []
    j = i
    while j < len(toks):
        t = toks[j][1]
        if t in ("(", "[", "{"):
            depth += 1
            if depth == 1:
                j += 1
                continue
        elif t in (")", "]", "}"):
            depth -= 1
            if depth == 0:
                break
        if depth == 1 and t == ",":
            segs.append(seg)
            seg = []
        elif depth == 1:
            seg.append(toks[j])
        j += 1
    segs.append(seg)
    names = set()
    for sg in segs:
        ids = []
        for k, t in sg:
            if t in ("=", ":") and k == "o":
                break  # a default value, a type after the colon
            if k == "i" and t not in skip:
                ids.append(t)
        if ids:
            names.add(ids[0] if style == "name_first" else ids[-1])
    return names, j + 1


def text_body(lang: str, text: str, name_span: Optional[Tuple[int, int]] = None) -> Tuple[str, int]:
    """(hash, size) of the text of a definition node (`name_span`: character offsets of its name inside `text`), NONE when it is
    smaller than store_min or bigger than data "dup" "max_body" tokens."""
    d = _data()[0]["norm"]
    if name_span and 0 <= name_span[0] < name_span[1] <= len(text):
        text = text[: name_span[0]] + " " + text[name_span[1] :]
    toks = _tokens(lang, text)
    if len(toks) < store_min() or len(toks) > _max_body():
        return NONE
    style = d["params"].get(lang) or d["params"]["default"]
    skip = frozenset(d["param_skip"])
    decl, loopw = frozenset(d["decl_kw"]), frozenset(d["local_in"])
    locs, _ = _param_names(toks, 0, style, skip)
    n = len(toks)
    for i, (k, t) in enumerate(toks):
        if k != "i":
            continue
        prev = toks[i - 1][1] if i else ""
        if prev in (".", "->", "::"):
            continue
        nxt = toks[i + 1][1] if i + 1 < n else ""
        if prev in decl or nxt in ("=", ":=") or nxt in loopw:
            if t not in decl and t not in loopw:
                locs.add(t)
    rename: Dict[str, str] = {}
    out: List[str] = []
    for i, (k, t) in enumerate(toks):
        if k == "i" and t in locs and (i == 0 or toks[i - 1][1] not in (".", "->", "::")):
            out.append(rename.setdefault(t, f"${len(rename)}"))
        else:
            out.append(t)
    return _digest("ts:", out), n
