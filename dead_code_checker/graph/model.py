"""graph.model: the data model of the symbol graph and the value types the other modules share.

Everything here is a plain immutable tuple (NamedTuple): picklable for worker processes, hashable, sortable, with no
behaviour beyond validation in the factories. Standard library only.

Graph (what the store keeps and what link and reach read)
    node(key, kind, lang, path, ...)     key = "lang:relpath#Qual.name[/arity][~h8]"  (node_key / split_key)
    edge(src, dst, kind, tier, site, ev) src and dst are node keys; tier S/T/N/D (TIERS: the weight in the why-alive path);
                                         ev indexes the evidence pool (-1 = none)
    root(node|selector, kind, origin, strength, trust, stub)
                                         a selector "<scheme>:<value>" (file:, node:, cmd:) that does not resolve to a node
                                         is itself a finding (dc.root-dangling); class:<name> is no finding, graph.classreg
                                         turns it into the class node (a library class: no root); so is build:<qual> (a
                                         recipe or stage that a doc names: `make test`), bound to the nodes of that qual;
                                         no node is no root and no finding
    evidence(source, file, line, snippet, extractor)  the snippet is cut to 120 bytes: source text is never stored
    canonical(nodes, edges, roots, evid) -> Graph    sorted, deduplicated, evidence pool remapped: the same graph in any
                                         input order gives the same Graph.digest() (the same bytes for 1, 2 or 8 workers)

Per-file facts (what an extractor returns, what a shard stores; graph.contracts holds the abstract interfaces)
    Facts(lang, defs, refs, imports, roots, dyn, errors)   a function of (lang, text) ONLY, never of the path: the shard is
                                         keyed by the blob sha, so the same content at two paths shares one shard.
                                         Names are file-relative ("Cls.meth"); graph.link turns them into node keys.
    Def(qual, kind, line0, line1, vis, flags, arity, stub, bh, bsz)   arity -1 = unknown, stub = reason string or "";
                                         bh / bsz = the normalised body hash of a function or method and its size
                                         (graph.extract.bodyhash; "" and 0 = none): the duplicates lens. Not part of the graph (no Node
                                         field), read by graph.lens from the facts. A shard written before these fields has rows of 8
                                         columns: they load with the defaults
    Ref(name, kind, line, scope)                             name as written ("a.b.f"), scope = qual of the enclosing def
    Import(module, names, alias, line, level, scope)         level = leading dots of a relative import
    RootHint(kind, target, line, strength, trust, stub)      target is a selector or a file-relative qual
    DynSite(pattern, line, scope)                            getattr(x, s), Class.forName, $cmd ...: feeds the D tier
    Facts.to_payload() -> canonical JSON bytes (lists sorted); payload_sha = sha256(payload); a Shard carries both.

Finding(level, path, line, code, msg[, why]) has the fields and the as_dict form of the findings of the text checker
(dead_code_checker.core.Finding) and adds the why-dead explanation: as_dict has "why" only when it is set. It renders with
the prefix of this checker, "dead-code-checker: E path:line: code: msg", like the configuration problems of graph.config.
"""

from __future__ import annotations

import hashlib
import json
import re
from itertools import repeat
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

# ---------------------------------------------------------------- vocabulary
TIERS: Dict[str, int] = {"S": 1, "T": 2, "N": 4, "D": 6}  # weight of an edge in the why-alive path
NODE_KINDS = (
    "module",
    "file",
    "class",
    "iface",
    "func",
    "method",
    "var",
    "const",
    "cfgkey",
    "resource",
    "job",
    "unit",
    "tool",
    "hook",
    "template",
    "tfvar",
)
EDGE_KINDS = (
    "call",
    "ref",
    "import",
    "inherit",
    "impl",
    "override",
    "di",
    "reg",
    "exec",
    "include",
    "reads",
    "lit",
    "ffi",
    "env",
    "schema",
)
# the last four are the cross-language edges (graph.extract.xlang, graph.xlink): lit = a string that spells the path of a file, ffi = a native symbol,
# env = an environment variable or configuration key written in one language and read in another, schema = a table, a proto message or service,
# a GraphQL type or field that code (or another schema file) names
# Hidden import aliases of the JS/TS extractor: `export {a} from m`, `export * as ns from m` and an import bound to an exported name
# (RE_ALIAS + the exported name), a require() / import() whose module value nothing binds (NS_ALIAS + its byte offset). A use of one
# is a re-export: link gives the edge the evidence rule REEXPORT_RULE, reach makes what it hands on part of the entry's API.
RE_ALIAS, NS_ALIAS, REEXPORT_RULE = "__re_", "__ns", "reexport"
# js: a specifier whose first segment starts with one of these is an alias or a scoped package (`@/x`, `~/x`, `#x`, `$lib/x`, `@org/ui`): no tsconfig is
# read, what follows the marker is looked up as a path suffix, in an import (graph.link) and in a computed specifier (the extractor) alike
ALIAS_MARKS = "@~#$"
ROOT_KINDS = (
    "bin",
    "systemd",
    "cron",
    "workflow",
    "ci-job",
    "doc-cmd",
    "main",
    "init",
    "test",
    "export",
    "script",
    "k8s-workload",
    "tf-output",
    "template",
    "classreg",
    "manual",
)  # plus "annotation:<rule>"; classreg = a class that a registration file names (ServiceLoader, web.xml: graph.classreg)
STRENGTHS = ("hard", "soft")
TRUSTS = ("registered", "convention", "documented")  # strongest first
VIS = ("private", "package", "public", "exported")
VERDICTS = ("alive", "weak-alive", "test-only", "doc-only", "public-unused", "dead", "dead-cluster", "unknown")
MODES = ("closed_world", "library")
SELECTOR_SCHEMES = ("file", "node", "cmd", "class", "build")


def selector_scheme(target: str) -> str:
    """The selector scheme of a root target ("node:<key>", "file:<path>", ...), or "" for a plain name: a symbol called `node`
    or `file` (a pytest fixture) and a language path such as `node::x` are names, not selectors."""
    scheme, sep, val = target.partition(":")
    return scheme if sep and scheme in SELECTOR_SCHEMES and not val.startswith(":") else ""


# Node.flags bit mask
F_TEST, F_GENERATED, F_STUB, F_DECORATED, F_ABSTRACT, F_OVERRIDE, F_PROTOCOL, F_DYNAMIC, F_ENTRY, F_IMPLICIT = (
    1 << i for i in range(10)
)
FLAG_NAMES = {
    "test": F_TEST,
    "generated": F_GENERATED,
    "stub": F_STUB,
    "decorated": F_DECORATED,
    "abstract": F_ABSTRACT,
    "override": F_OVERRIDE,
    "protocol": F_PROTOCOL,
    "dynamic-zone": F_DYNAMIC,
    "entry-registered": F_ENTRY,
    "implicit": F_IMPLICIT,
}

SNIPPET_MAX = 120


def flags_of(*names: str) -> int:
    out = 0
    for n in names:
        out |= FLAG_NAMES[n]
    return out


def _need(value, allowed: Sequence[str], what: str):
    if value not in allowed:
        raise ValueError(f"bad {what} {value!r} (allowed: {', '.join(allowed)})")
    return value


# ---------------------------------------------------------------- node keys
_QUAL_ESC = re.compile(r"[./~#:\s]")


def qual_name(name: str) -> str:
    """The name part of a "<tag>:<name>" qual of the config languages (`recipe:a%2Eb`, `stage:dev`, `tpl:app%2Elabels`): the characters
    that are qual or path syntax (. / ~ # : and blanks) are %XX-escaped, so one name is one qual segment. A `*` is kept (a glob).
    """
    return _QUAL_ESC.sub(lambda m: "%%%02X" % ord(m.group()), name)


def _esc(path: str) -> str:
    return path.replace("%", "%25").replace("#", "%23")


def node_key(lang: str, path: str, qual: str = "", arity: Optional[int] = None, h8: str = "") -> str:
    """lang:relpath#Qual.name[/arity][~h8]; '#' and '%' in a path are percent-escaped, arity < 0 or None = omitted."""
    if not lang or ":" in lang or not path:
        raise ValueError("node_key needs a language without ':' and a path")
    key = f"{lang}:{_esc(path)}"
    if qual:
        key += "#" + qual
        if arity is not None and arity >= 0:
            key += f"/{int(arity)}"
    if h8:
        key += "~" + h8
    return key


def split_key(key: str) -> Tuple[str, str, str, int, str]:
    """Inverse of node_key: (lang, path, qual, arity (-1 = none), h8)."""
    lang, _, rest = key.partition(":")
    h8 = ""
    if "~" in rest and len(rest) - rest.rindex("~") == 9:
        rest, h8 = rest[: rest.rindex("~")], rest[rest.rindex("~") + 1 :]
    path, _, qual = rest.partition("#")
    arity = -1
    if qual and "/" in qual and qual.rsplit("/", 1)[1].isdigit():
        qual, a = qual.rsplit("/", 1)
        arity = int(a)
    return lang, path.replace("%23", "#").replace("%25", "%"), qual, arity, h8


# ---------------------------------------------------------------- graph records
class Node(NamedTuple):
    key: str
    kind: str
    lang: str
    path: str
    line0: int
    line1: int
    pkg: str
    vis: str
    flags: int


class Edge(NamedTuple):
    src: str
    dst: str
    kind: str
    tier: str
    site: str  # "path:line" of the reference
    ev: int  # index into the evidence pool, -1 = none


class Root(NamedTuple):
    node: str  # node key, "" while only a selector is known
    selector: str  # "<scheme>:<value>" or ""
    kind: str
    origin: str  # "file:line" of the registration
    strength: str
    trust: str
    stub: str  # stub reason of the registered entry, "" = a real body


class Evidence(NamedTuple):
    source: str  # extractor or engine name
    file: str
    line: int
    snippet: str  # rule id or a cut of the line, <= 120 bytes
    extractor: str  # "name@ver"


def _cut(text: str, n: int = SNIPPET_MAX) -> str:
    raw = text.encode("utf-8", "replace")
    return text if len(raw) <= n else raw[:n].decode("utf-8", "ignore")


def node(
    key: str,
    kind: str,
    lang: str = "",
    path: str = "",
    line0: int = 0,
    line1: int = 0,
    pkg: str = "",
    vis: str = "public",
    flags: int = 0,
) -> Node:
    if not lang or not path:
        l2, p2, *_ = split_key(key)
        lang, path = lang or l2, path or p2
    return Node(
        key,
        _need(kind, NODE_KINDS, "node kind"),
        lang,
        path,
        int(line0),
        int(line1 or line0),
        pkg,
        _need(vis, VIS, "visibility"),
        int(flags),
    )


def edge(src: str, dst: str, kind: str, tier: str = "T", site: str = "", ev: int = -1) -> Edge:
    return Edge(src, dst, _need(kind, EDGE_KINDS, "edge kind"), _need(tier, tuple(TIERS), "tier"), site, int(ev))


def root(
    kind: str,
    origin: str = "",
    node: str = "",
    selector: str = "",
    strength: str = "hard",
    trust: str = "registered",
    stub: str = "",
) -> Root:
    if not (kind in ROOT_KINDS or kind.startswith("annotation:")):
        raise ValueError(f"bad root kind {kind!r}")
    if bool(node) == bool(selector):
        raise ValueError("a root needs exactly one of node or selector")
    if selector:
        _need(selector.partition(":")[0], SELECTOR_SCHEMES, "selector scheme")
    return Root(
        node, selector, kind, origin, _need(strength, STRENGTHS, "strength"), _need(trust, TRUSTS, "trust"), stub
    )


def evidence(source: str, file: str, line: int, snippet: str, extractor: str = "") -> Evidence:
    return Evidence(source, file, int(line), _cut(snippet), extractor)


class Graph(NamedTuple):
    nodes: Tuple[Node, ...]
    edges: Tuple[Edge, ...]
    roots: Tuple[Root, ...]
    evid: Tuple[Evidence, ...]

    def digest(self) -> str:
        """sha256 of the canonical JSON: equal graphs have equal digests (determinism and cold = warm tests)."""
        blob = json.dumps([self.nodes, self.edges, self.roots, self.evid], separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()


def canonical(
    nodes: Iterable[Node], edges: Iterable[Edge] = (), roots: Iterable[Root] = (), evid: Iterable[Evidence] = ()
) -> Graph:
    """Sort and deduplicate; the evidence pool becomes sorted+unique and Edge.ev is remapped (an index outside the pool is
    a ValueError). Two nodes with one key but different data are a ValueError: the linker must not emit them."""
    by_key: Dict[str, Node] = {}
    for n in nodes:
        if by_key.setdefault(n.key, n) != n:
            raise ValueError(f"duplicate node key with different data: {n.key}")
    pool_in = list(evid)
    pool = sorted(set(pool_in))
    where = {e: i for i, e in enumerate(pool)}
    remap, n_in = [where[e] for e in pool_in], len(pool_in)
    es = set()  # plain tuples: the Edge objects are made once, after the dedupe and the sort
    for e in edges:
        if e[5] >= n_in or e[5] < -1:
            raise ValueError(f"edge evidence index {e[5]} outside the pool of {n_in}")
        es.add((e[0], e[1], e[2], e[3], e[4], remap[e[5]] if e[5] >= 0 else -1))
    return Graph(
        tuple(by_key[k] for k in sorted(by_key)),
        tuple(map(tuple.__new__, repeat(Edge), sorted(es))),
        tuple(sorted(set(roots), key=lambda r: (r.kind, r.origin, r.node, r.selector, r.strength, r.trust, r.stub))),
        tuple(pool),
    )


# ---------------------------------------------------------------- per-file facts
class Def(NamedTuple):
    qual: str
    kind: str
    line0: int
    line1: int
    vis: str = "public"
    flags: int = 0
    arity: int = -1
    stub: str = ""
    bh: str = ""  # normalised body hash (graph.extract.bodyhash), "" = none: the duplicates lens of the viewer
    bsz: int = 0  # the size of that body (python: ast nodes, other languages: tokens)


class Ref(NamedTuple):
    name: str
    kind: str  # an edge kind: call, ref, inherit, ...
    line: int
    scope: str = ""


class Import(NamedTuple):
    module: str
    names: Tuple[str, ...]
    alias: str
    line: int
    level: int = 0
    scope: str = ""


class RootHint(NamedTuple):
    kind: str
    target: str
    line: int
    strength: str = "hard"
    trust: str = "registered"
    stub: str = ""


class DynSite(NamedTuple):
    pattern: str
    line: int
    scope: str = ""


_COLS = {"defs": Def, "refs": Ref, "imports": Import, "roots": RootHint, "dyn": DynSite}
PAYLOAD_V = 1


class Facts(NamedTuple):
    lang: str
    defs: Tuple[Def, ...] = ()
    refs: Tuple[Ref, ...] = ()
    imports: Tuple[Import, ...] = ()
    roots: Tuple[RootHint, ...] = ()
    dyn: Tuple[DynSite, ...] = ()
    errors: int = 0  # syntax errors the extractor met (the file was still read as far as possible)

    def to_payload(self) -> bytes:
        cols = {c: sorted([list(f) if isinstance(f, tuple) else f for f in r] for r in getattr(self, c)) for c in _COLS}
        doc = {"v": PAYLOAD_V, "lang": self.lang, "errors": int(self.errors), "cols": cols}
        return json.dumps(doc, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode()

    @staticmethod
    def from_payload(payload: bytes) -> "Facts":
        doc = json.loads(payload)
        if doc.get("v") != PAYLOAD_V:
            raise ValueError(f"unknown facts payload version {doc.get('v')!r}")
        cols = {}
        for c, cls in _COLS.items():
            rows = doc["cols"][c]
            cols[c] = tuple(cls(*(tuple(f) if c == "imports" and i == 1 else f for i, f in enumerate(r))) for r in rows)
        return Facts(doc["lang"], errors=int(doc["errors"]), **cols)


def payload_sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class Shard(NamedTuple):
    key: str  # shard_key(...)
    blob: str  # git-style blob sha of the content (or a virtual id)
    lang: str  # language, or "root:<name>" for a root-extractor virtual shard
    ext_ver: str  # "name@version" of the extractor
    payload: bytes  # Facts.to_payload()
    sha: str = ""  # payload_sha(payload)

    def facts(self) -> Facts:
        return Facts.from_payload(self.payload)


def shard_key(blob: str, lang: str, ext_ver: str, grammar_sha: str = "") -> str:
    """sha256(git_blob_sha | lang | extractor@ver | grammar_sha): no path, no mtime."""
    return hashlib.sha256("\0".join((blob, lang, ext_ver, grammar_sha)).encode()).hexdigest()


def new_shard(blob: str, lang: str, ext_ver: str, facts: Facts, grammar_sha: str = "") -> Shard:
    p = facts.to_payload()
    return Shard(shard_key(blob, lang, ext_ver, grammar_sha), blob, lang, ext_ver, p, payload_sha(p))


# ---------------------------------------------------------------- findings
class Finding:
    """The text checker's Finding plus an optional why-dead explanation and, for a dead-symbol claim, the numeric confidence
    (graph.confidence: 0..100, a second axis next to the level and the code, never an input of either) with the one-line sum that
    gave it (`conf_note`). Both stay None / "" for every finding that makes no such claim."""

    __slots__ = ("level", "path", "line", "code", "msg", "why", "confidence", "conf_note")

    def __init__(self, level: str, path: str, line: int, code: str, msg: str, why: str = ""):
        self.level, self.path, self.line, self.code, self.msg, self.why = level, path, line, code, msg, why
        self.confidence: Optional[int] = None
        self.conf_note = ""

    def render(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        return f"dead-code-checker: {self.level} {loc}: {self.code}: {self.msg}"

    def as_dict(self) -> dict:
        d = {"level": self.level, "path": self.path, "line": self.line, "code": self.code, "msg": self.msg}
        if self.why:
            d["why"] = self.why
        if self.confidence is not None:
            d["confidence"] = self.confidence
            if self.conf_note:
                d["confidence_note"] = self.conf_note
        return d

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return self.render()


def sort_findings(findings: Iterable[Finding]) -> List[Finding]:
    """E first, then (path, line, code, msg); exact duplicates dropped: the order of the text checker."""
    seen, out = set(), []
    for f in sorted(findings, key=lambda f: (f.level != "E", f.path, f.line, f.code, f.msg)):
        k = (f.level, f.path, f.line, f.code, f.msg)
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out
