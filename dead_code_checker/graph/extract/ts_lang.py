"""graph.extract.ts_lang: the per-language rules of ts_generic (data/rules.json) and the few text parsers they name.

ts_generic maps query matches to Facts; what differs per language (visibility, which top-level names and attributes are
roots, how a use / import declaration reads, how a qualified name is spelled) is data in rules.json plus the helpers
here. Stdlib only, pure functions of their arguments, nothing is read but the rules file.

    rules(lang) -> dict                   the rules.json entry of lang ({} is an explicit entry: bash); KeyError without
    rules_sha(lang) -> str                sha256 of that entry: part of the shard key (ts_generic.grammar_sha)
    vis(rl, name, cap_text) -> str        "" when the language has no visibility rule (the caller keeps its default)
    ref_name(rl, text, obj, aliases)      the name of a use as the linker reads it: "x.f" for a receiver, name_sub applied
    ref_names(rl, text, obj, aliases, shadow)
                                          the names one use records: ref_name, and `x.f` besides when x is an imported
                                          package that a local of the same name shadows (`config := config.New()`)
    protocol(rl, name) -> bool            a method of this name satisfies an interface of the stdlib or a library (rules
                                          protocol_names): the linker keeps it alive with its type
    recv_name(rl, text)                   the type name of an impl header or a method receiver (recv_sub; "_" if none)
    imports(rl, text, line, scope, attrs) (Import facts, package aliases) of one @import capture; attrs are the texts of the
                                          attributes right before it (`#[path = "x.rs"] mod x;`)
    macro_imports(rl, name, body, line, scope)
                                          the Import facts declared in the body of an item macro (rules item_macros: cfg_if!)
    exec_names(rl, text)                  path-like command words of a directive comment (go:generate)
    comment_roots(rl, text), name_roots(rl, name, pkgs), attr_roots(rl, text)
                                          the (kind, strength, trust) of a root, by rule
    attr_paths(rl, text)                  the paths of an attribute and of the attributes wrapped in it (rules attr_wrap:
                                          `unsafe(no_mangle)`, `cfg_attr(feature = "py", pyclass)`)
    attr_owners(data, attrs, starts)      which def an attribute belongs to: the one that starts right after it, other
                                          attributes, whitespace and comments in between (#[test] #[ignore] fn t() {})
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from ..model import Import

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "rules.json")
_GAP = re.compile(rb"(?:\s+|//[^\n]*|/\*.*?\*/)*", re.S)
_WS = re.compile(r"\s+")
_PLAIN = re.compile(r"[A-Za-z_][\w.]*")


@lru_cache(maxsize=1)
def _all() -> dict:
    with open(DATA, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=64)
def rules_sha(lang: str) -> str:
    """sha256 of the rules entry of `lang` ('' without one): the facts depend on it (protocol flags, path attributes), so it is
    part of the shard key together with the queries; a changed rule must not leave stale shards."""
    rl = _all().get(lang)
    return "" if rl is None else hashlib.sha256(json.dumps(rl, sort_keys=True).encode()).hexdigest()


def rules(lang: str) -> dict:
    try:
        return _all()[lang]
    except KeyError:
        raise KeyError(
            f"data/rules.json has no entry for {lang!r} (an empty object {{}} is the entry of a language without rules)"
        ) from None


@lru_cache(maxsize=256)
def _rx(p: str) -> "re.Pattern":
    return re.compile(p)


def vis(rl: dict, name: str, cap_text: str = "") -> str:
    tried = rl.get("vis_cap", ()) if cap_text else rl.get("vis_name", ())
    subject = cap_text if cap_text else name
    return next((v for p, v in tried if _rx(p).search(subject)), rl.get("vis_default", ""))


def protocol(rl: dict, name: str) -> bool:
    return name in rl.get("protocol_names", ())


def _strip_groups(t: str, groups: Sequence[Sequence[str]]) -> str:
    """Remove each balanced group from `t` that starts with a prefix of `groups` (its last character opens the group, the
    second item closes it; the `>` of an `->` is no closer): `Wrapper::<Vec<u8>>::new` -> `Wrapper::new`. A group that never
    closes runs to the end."""
    for head, close in groups:
        op, i = head[-1], t.find(head)
        while i >= 0:
            depth, j = 0, i + len(head) - 1
            while j < len(t):
                depth += (t[j] == op) - (t[j] == close and t[j - 1] != "-")
                j += 1
                if depth == 0:
                    break
            t = t[:i] + (t[j:] if depth == 0 else "")
            i = t.find(head)
    return t


def _norm(rl: dict, text: str) -> str:
    t = _strip_groups(_WS.sub("", text), rl.get("strip_groups", ()))
    for p, rep in rl.get("name_sub", ()):
        t = _rx(p).sub(rep, t)
    return t


def recv_name(rl: dict, text: str) -> str:
    """`&'a mut Foo<T>`, `Box<dyn X>`, `crate::m::Foo` -> Foo, Box, Foo; a type with no name (`[T]`, a tuple) -> `_`."""
    t = _WS.sub(" ", text).strip()
    subs = rl.get("recv_sub")
    if not subs:
        return t
    for p, rep in subs:
        t = _rx(p).sub(rep, t)
    return t if _PLAIN.fullmatch(t) and "." not in t else "_"


def ref_name(rl: dict, text: str, obj: str = "", aliases: Iterable[str] = ()) -> str:
    """`x.f` for a call with a receiver (`_.f` when the receiver is no plain path: the linker then matches members named
    f); the bare `f` when the receiver is an imported package alias (the package is not resolvable from the extractor;
    ref_names adds `x.f` where a local shadows the package).
    """
    t = _norm(rl, text)
    if not obj:
        return t
    o = _norm(rl, obj)
    return t if o in aliases else f"{o if _PLAIN.fullmatch(o) else '_'}.{t}"


def ref_names(rl: dict, text: str, obj: str = "", aliases: Iterable[str] = (), shadow: Iterable[str] = ()) -> List[str]:
    """ref_name, and `x.f` besides when the receiver x is an imported package AND a local of the enclosing definition (`shadow`):
    Go allows `func run(config *config.Config) { config.Validate() }`, where x.f is a method call though x is a package name
    elsewhere, and a local may be declared after a first use of the package: both spellings are recorded. Rule bare_unknown_recv (Go):
    a plain receiver that is no alias and no local of the definition may be a package named by its `package` clause, which the
    import path does not say (`import ".../internal/store"` with `package storage`): `storage.New()` is `x.F` and the bare `F`.
    """
    name = ref_name(rl, text, obj, aliases)
    o = _norm(rl, obj) if obj else ""
    if o and o in aliases and o in shadow:
        return [name, f"{o}.{name}"]
    if (
        o
        and rl.get("bare_unknown_recv")
        and o not in aliases
        and o not in shadow
        and "." not in o
        and _PLAIN.fullmatch(o)
    ):
        return [name, _norm(rl, text)]  # maybe a package whose `package` clause is not the last element of its path
    return [name]


# ------------------------------------------------------------------ imports
def _split_top(s: str, opens: str = "{", closes: str = "}") -> List[str]:
    """Split at the commas outside any bracket pair and outside a "string"."""
    out, depth, cur, quote, esc = [], 0, [], False, False
    for ch in s:
        if quote:
            quote = esc or ch != '"'
            esc = ch == "\\" and not esc
        else:
            quote = ch == '"'
            depth += (ch in opens) - (ch in closes)
            if ch == "," and depth == 0:
                out.append("".join(cur))
                cur = []
                continue
        cur.append(ch)
    out.append("".join(cur))
    return [p.strip() for p in out if p.strip()]


def _use_paths(spec: str, pre: Tuple[str, ...], out: list) -> None:
    spec = spec.strip()
    if "{" in spec:
        head, _, rest = spec.partition("{")
        base = pre + tuple(t.strip() for t in head.split("::") if t.strip())
        for part in _split_top(rest[: rest.rindex("}")] if "}" in rest else rest):
            _use_paths(part, base, out)
    elif spec:
        path, _, alias = spec.partition(" as ")
        out.append((pre + tuple(t.strip() for t in path.split("::") if t.strip()), alias.strip()))


_RS_USE = re.compile(r"^\s*(?:pub\s*(?:\([^)]*\)\s*)?)?use\s+(.*?)\s*;?\s*$", re.S)
_RS_MOD = re.compile(r"^\s*(?:pub\s*(?:\([^)]*\)\s*)?)?mod\s+([A-Za-z_]\w*)\s*;")


def _rust_imports(rl: dict, text: str, line: int, scope: str, attrs: Sequence[str]) -> List[Import]:
    mod = _RS_MOD.match(text)
    if mod:  # `mod x;` names the file x.rs / x/mod.rs next to this one, a path attribute names the file itself
        name, rx = mod.group(1), rl.get("mod_path")
        files = sorted({m.group(1) for a in attrs for m in _rx(rx).finditer(a)}) if rx else []
        if files:  # relative to the directory of this file (the linker reads a "./" module as a path)
            return [Import(f if f.startswith(("./", "../")) else f"./{f}", (), name, line, 0, scope) for f in files]
        return [Import(f"self::{name}", (), name, line, 0, scope)]
    use = _RS_USE.match(text)
    paths: list = []
    if use:
        _use_paths(use.group(1).lstrip(":"), (), paths)
    out = []
    for toks, alias in paths:
        if not toks:
            continue
        last, head = toks[-1], "::".join(toks[:-1])
        if last == "self" and head:
            out.append(Import(head, (), alias, line, 0, scope))
        elif last == "*" and head:
            out.append(Import(head, ("*",), "", line, 0, scope))
        elif head:
            out.append(Import(head, (last,), alias, line, 0, scope))
        else:
            out.append(Import(last, (), alias, line, 0, scope))
    return out


_GO_SPEC = re.compile(r'^\s*(?:([A-Za-z_]\w*|\.)\s+)?"([^"]+)"')


def _go_alias(text: str) -> Set[str]:
    """Package names an import spec may bind. Only the path says it: a trailing /vN is either a module major version (the
    package is the element before it) or a directory with `package vN`, so both names are bound; a package whose `package`
    clause differs from its last path element (not /vN, go-, -go, .go, .vN) is unknown here: ref_names records its x.F as
    x.F and as the bare F (rule bare_unknown_recv).
    """
    m = _GO_SPEC.match(text)
    if not m:
        return set()
    explicit, path = m.groups()
    if explicit:
        return set() if explicit in ("_", ".") else {explicit}
    parts = path.split("/")
    lasts = [parts[-1]]
    if len(parts) > 1 and re.fullmatch(r"v\d+", parts[-1]):
        lasts.append(parts[-2])  # lib/v2 is package lib (a module major version), api/v1 is package v1 (a directory)
    names: Set[str] = set()
    for last in lasts:
        base = re.sub(r"(^go-|-go$|\.go$|\.v\d+$)", "", last)  # gopkg.in/yaml.v3 -> yaml, github.com/x/go-foo -> foo
        names |= {last, base, base.replace("-", "_"), base.replace("-", "")}
    return names


_RS_ITEM = re.compile(
    r"((?:#\s*\[[^\]]*\]\s*)*)(?<![\w:.])((?:pub\s*(?:\([^)]*\)\s*)?)?(?:mod\s+[A-Za-z_]\w*\s*;|use\s[^;]*;))"
)  # attributes, then a `mod x;` or a `use ..;` declaration


def macro_imports(rl: dict, name: str, body: str, line: int, scope: str = "") -> List[Import]:
    """`cfg_if! { if #[cfg(unix)] { mod unix; use unix as plat; } else { #[path = "w.rs"] mod win; } }`: what the body declares."""
    rx = rl.get("item_macros")
    if not rx or not _rx(rx).search(name.strip()):
        return []
    out: List[Import] = []
    for m in _RS_ITEM.finditer(body):
        attrs = [a.strip() for a in re.findall(r"#\s*\[([^\]]*)\]", m.group(1))]
        out += _rust_imports(rl, m.group(2), line + body.count("\n", 0, m.start(2)), scope, attrs)
    return out


def imports(
    rl: dict, text: str, line: int, scope: str = "", attrs: Sequence[str] = ()
) -> Tuple[List[Import], Set[str]]:
    kind = rl.get("import")
    if kind == "rust-use":
        return _rust_imports(rl, text, line, scope, attrs), set()
    if kind == "go-alias":
        return [], _go_alias(text)
    return [], set()


# ------------------------------------------------------------------ roots
def exec_names(rl: dict, text: str) -> List[str]:
    rx = rl.get("comment_exec")
    m = _rx(rx).search(text.strip()) if rx else None
    if not m:
        return []
    return [w for w in m.group(1).split() if not w.startswith("-") and ("/" in w or w.endswith(".go"))]


def _hint(r: dict) -> Tuple[str, str, str]:
    return r["kind"], r.get("strength", "hard"), r.get("trust", "convention")


def comment_roots(rl: dict, text: str) -> List[Tuple[str, str, str, str]]:
    """[(name, kind, strength, trust)] of the directive comments (`//export Foo`) that match."""
    out = []
    for r in rl.get("comment_roots", ()):
        m = _rx(r["re"]).search(text.strip())
        if m:
            out.append((m.group(1), *_hint(r)))
    return out


def name_roots(rl: dict, name: str, pkgs: Set[str]) -> List[Tuple[str, str, str]]:
    return [
        _hint(r)
        for r in rl.get("roots", ())
        if _rx(r["name"]).search(name) and (not r.get("pkg") or any(_rx(r["pkg"]).search(p) for p in pkgs))
    ]


_ATTR = re.compile(r"\s*((?:::)?\s*[A-Za-z_]\w*(?:\s*::\s*[A-Za-z_]\w*)*)\s*(.*)\Z", re.S)


def attr_paths(rl: dict, text: str) -> List[str]:
    """`tokio::test(flavor = "x")` -> [tokio::test]; a wrapper (rules attr_wrap: name -> number of leading arguments that are
    no attribute) adds the paths of the attributes inside it: `unsafe(no_mangle)` -> [unsafe, no_mangle],
    `cfg_attr(feature = "py", pyclass, derive(Debug))` -> [cfg_attr, pyclass, derive]."""
    m = _ATTR.match(text)
    if not m:
        return []
    path, rest = _WS.sub("", m.group(1)), m.group(2).strip()
    skip = rl.get("attr_wrap", {}).get(path)
    if skip is None or not (rest.startswith("(") and rest.endswith(")")):
        return [path]
    return [path] + [q for arg in _split_top(rest[1:-1], "([{", ")]}")[skip:] for q in attr_paths(rl, arg)]


def attr_roots(rl: dict, text: str) -> List[Tuple[str, str, str]]:
    paths = attr_paths(rl, text)
    return [_hint(r) for r in rl.get("attrs", ()) if any(_rx(r["re"]).search(p) for p in paths)]


def attr_owners(data: bytes, attrs: Sequence[Tuple[int, int, str]], starts: Set[int]) -> List[Tuple[str, int, int]]:
    """[(attribute text, start byte of the def it belongs to, start byte of the attribute)] for attrs (start, end, text)."""
    ends: Dict[int, int] = {s: e for s, e, _ in attrs}
    out = []
    for s, e, text in sorted(attrs):
        p = e
        while True:
            p = _GAP.match(data, p).end()
            if p not in ends:
                break
            p = ends[p]
        if p in starts:
            out.append((text, p, s))
    return out
