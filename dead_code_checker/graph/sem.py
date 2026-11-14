"""graph.sem: the semantic layer. Stdlib only, nothing here touches the network.

The graph (ast, tree-sitter, imports) is always built and is over-wide; semantics confirms or refutes, it never builds a graph
from scratch. Two sources:

SCIP (a package-wide, deterministic index): read_scip, stamp, refine
    read_scip(bytes) -> Index      the protobuf wire format read with the stdlib: Document, Occurrence (the legacy `range` and the
                                   typed single/multi line ranges), SymbolInformation with its Relationships (is_implementation).
                                   Only the start of a range is kept. A truncated or hostile file is a ScipError, never a crash.
    stamp(index, tree, root, family, engine) -> Meta    the sidecar <index>.meta.json an indexer run leaves: engine, package root,
                                   language family, input_hash = package_hash of the files the index was made from, index_sha256.
    refine(graph, tree, indexes, workers=None, text_of=None) -> Refined(graph, findings, reports)   one parallel unit per index.
  An overlay is applied only when meta.input_hash equals package_hash of the CURRENT files of the package (tree = {path: blob})
  and the index file is the stamped one (sha256); a stale or foreign index is ignored with W dc.sem-stale / dc.sem-index.
  An applied overlay does, per package (a symbol is bound to a graph node by name and line: a definition occurrence of `name`
  on a line inside the node's lines; a `local` symbol, which has no name, by the one node that starts on that line):
    S edges     an N or T edge (call, ref, import, inherit) whose site line holds a SCIP reference to a symbol of its target
                becomes tier S (same src, dst, kind, site). A resolved reference the graph has no edge for gets an S `ref` edge
                from the innermost func/method/class/iface that holds the line (the file node outside any); an
                is_implementation relationship gives S `impl` (sub -> base) and, for members, `override` (base -> sub) edges.
    refutation  an N edge is dropped only when (1) the index covers the package: coverage >= min_coverage (data/sem.json), the
                share of the package's func/method/class/iface nodes the index defines; (2) its site line holds a SCIP reference
                that resolved the SAME name to a symbol other than the edge's target, and every such symbol is bound to a
                concrete node of THIS package: an abstract, protocol or interface member (a call through it may run any
                implementation) and a symbol the package does not define (an external interface or protocol such as
                io.Reader, a base in another package or index, a clash) are no opinion; (3) the target is bound to a
                symbol; (4) the line provably holds ONE reference site of that name, or all of them resolved: the text of the
                line (text_of) holds the name no more often than the index has distinct positions of it. A graph edge knows
                its line, not its column, so `x.m() + y.m()` with an untyped y looks like `x.m()` to the index: the second
                call is no evidence against a name edge, and nothing is refuted without the text; (5) the engine resolves
                a member access on a union type completely (data/sem.json "engines": scip-python gives the first member
                only, so its overlays confirm but never refute). An unresolved line never refutes. A T edge is never
                refuted. Every name edge kept by (4) or (5) is counted in Report.kept and named in W dc.sem-refute; below the
                coverage the S edges are still added (a resolved reference is true) and W dc.sem-coverage says that nothing
                was refuted. Two overlapping indexes: a confirmation beats a refutation, so the result does not depend on
                their order.
  Evidence: every S edge points at one Evidence row whose `source` is the engine that produced it (scip-python, ...).

LSP (addressed queries for a handful of candidates): Lsp, servers, arbitrate
    Lsp(argv, root, deadline)      a stdio JSON-RPC client: initialize, textDocument/references, prepareCallHierarchy +
                                   incomingCalls where the server has them, shutdown; it answers the server's requests
                                   (workDoneProgress/create, configuration, ...) and tracks $/progress. Every query is one
                                   request, counted and limited; a message or an answer of the wrong shape is an LspError.
    servers(families) -> {family: Spec | reason}   from data/sem.json and the one engine resolver (graph.engines: the
                                   engines directory, then PATH, pinned by the lock when there is one; a no-network
                                   namespace when the host has one, a private HOME). A server that runs
                                   project code (needs_sandbox: rust-analyzer) is offered only to a caller that says it runs
                                   in a sandbox (sandboxed=True).
    arbitrate(graph, cands, root, text_of, specs) -> Arb(graph, status, queries, findings)   at most max_queries requests per
                                   language and budget_s for all languages together (data/sem.json: 20 and 20 s), one server per
                                   language, the languages in parallel. A reference from outside the candidate is an S edge from
                                   the node that holds it (evidence source "lsp:<engine>", status "referenced"); the candidate's
                                   own body (recursion) is no reference. "No references" counts only when the server announced
                                   its indexing and finished it (status "unreferenced"), else it is "unconfirmed". That is
                                   evidence for the caller, never a refutation: a server has no coverage
                                   figure, so nothing is dropped from the graph because of it.
Every degradation is a W finding (dc.sem-stale, -index, -coverage, -refute, -lsp-failed, -lsp-incomplete, -lsp-budget, -lsp-sandbox):
none is silent.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import posixpath
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from functools import lru_cache
from typing import Callable, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple
from urllib.parse import unquote, urlparse

from . import langs as _langs
from . import par, report, safety
from .model import F_ABSTRACT, F_PROTOCOL, Edge, Evidence, Finding, Graph, Node, canonical, evidence, split_key

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sem.json")
SEM_V = "sem@1"  # Evidence.extractor of what this module writes
# the edge kinds that stand for a reference a SCIP occurrence can confirm
REF_KINDS = ("call", "ref", "import", "inherit")
DEF_KINDS = ("func", "method", "class", "iface")  # what coverage counts, and what can hold a reference (the caller)
NAMED = ("type", "term", "method", "macro")  # descriptor kinds of a symbol that can be a graph node
DEFINITION, FORWARD = 1, 64  # Occurrence.symbol_roles bits
LINE_MAX = (
    1 << 20
)  # a node that claims lines beyond this holds no reference (a shard from elsewhere must not size an array)
WHY = {
    "dc.sem-stale": "an overlay is applied only when its input hash is the current package hash: run the indexer again",
    "dc.sem-index": "an index without its stamp, or that is not the stamped file, is tied to no files",
    "dc.sem-coverage": "refuting a name edge needs an index that covers the package (data/sem.json: min_coverage)",
    "dc.sem-refute": "a name edge is dropped only when its line holds one site of the name (or all resolved) and the engine resolves unions",
    "dc.sem-lsp-failed": "what a server answered is kept; the candidates it did not answer were not arbitrated",
    "dc.sem-lsp-incomplete": "a server that may still be indexing proves nothing by silence: 'no references' is not used",
    "dc.sem-lsp-budget": "the budget is per run: max_queries requests per language, budget_s for all languages together",
    "dc.sem-lsp-sandbox": "a language server runs project code: without a no-network namespace only its offline environment holds it back",
}


report.register_why(WHY)


@lru_cache(maxsize=None)
def config() -> dict:
    with open(DATA, encoding="utf-8") as fh:
        return json.load(fh)


class SemError(ValueError):
    """A sidecar or an index that cannot be used; the message is the reason (it ends up in a W finding)."""


class ScipError(SemError):
    pass


def _w(code: str, msg: str) -> Finding:
    return Finding("W", safety.WHERE, 0, code, msg, WHY[code])


# ================================================================ SCIP: the wire format
def _varint(b, i: int) -> Tuple[int, int]:
    r = b[i]
    if r < 0x80:  # one byte: nearly every key, length, line and column
        return r, i + 1
    r, s, i = r & 0x7F, 7, i + 1
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if c < 0x80:
            return r, i
        s += 7
        if s > 63:
            raise ScipError("varint longer than 10 bytes")


def _fields(b):
    """(field number, wire type, value) of a message: an int for a varint, a memoryview for the other wire types."""
    i, end = 0, len(b)
    while i < end:
        key, i = _varint(b, i)
        wt = key & 7
        if wt == 0:
            v, i = _varint(b, i)
        else:
            if wt == 2:
                n, i = _varint(b, i)
            elif wt in (1, 5):
                n = 8 if wt == 1 else 4
            else:
                raise ScipError(f"unsupported wire type {wt}")
            if i + n > end:
                raise ScipError("a field runs past the end of its message")
            v, i = b[i : i + n], i + n
        yield key >> 3, wt, v


def _s(v) -> str:
    return sys.intern(str(v, "utf-8", "replace"))


class Occ(NamedTuple):
    line: int  # 0-based start line
    col: int
    symbol: str
    roles: int  # Definition 1, Import 2, WriteAccess 4, ReadAccess 8, Generated 16, Test 32, ForwardDefinition 64


class Rel(NamedTuple):
    symbol: str
    reference: bool
    implementation: bool
    type_definition: bool
    definition: bool


class Sym(NamedTuple):
    symbol: str
    kind: int
    name: str  # display_name
    rels: Tuple[Rel, ...]


class Doc(NamedTuple):
    path: str  # relative to the index's project root, "/" separated
    language: str
    occs: Tuple[Occ, ...]
    syms: Tuple[Sym, ...]


class Index(NamedTuple):
    tool: str
    version: str
    root: str  # Metadata.project_root
    docs: Dict[str, Doc]
    external: Tuple[Sym, ...]


def _packed(v) -> List[int]:
    out, i, n = [], 0, len(v)
    while i < n:
        x, i = _varint(v, i)
        out.append(x)
    return out


def _occ(b) -> Occ:
    rng, sym, roles, typed = [], "", 0, None
    for n, wt, v in _fields(b):
        if n == 1:  # the legacy range: [line, col, end col] or [line, col, end line, end col], packed or not
            rng += [v] if wt == 0 else _packed(v) if wt == 2 else []
        elif n == 2 and wt == 2:
            sym = _s(v)
        elif n == 3 and wt == 0:
            roles = v
        elif n in (8, 9) and wt == 2:
            # SingleLineRange, MultiLineRange: fields 1 and 2 are the start line and character
            p = {k: x for k, w, x in _fields(v) if w == 0}
            typed = (p.get(1, 0), p.get(2, 0))
    return Occ(*(typed or (tuple(rng[:2]) if len(rng) >= 2 else (0, 0))), sym, roles)


def _sym(b) -> Sym:
    sym, kind, name, rels = "", 0, "", []
    for n, wt, v in _fields(b):
        if n == 1 and wt == 2:
            sym = _s(v)
        elif n == 5 and wt == 0:
            kind = v
        elif n == 6 and wt == 2:
            name = str(v, "utf-8", "replace")
        elif n == 4 and wt == 2:
            target, flags = "", {}
            for k, w, x in _fields(v):
                if k == 1 and w == 2:
                    target = _s(x)
                elif w == 0:
                    flags[k] = bool(x)
            # fields 2..5: reference, implementation, type_definition, definition
            rels.append(Rel(target, *(flags.get(k, False) for k in (2, 3, 4, 5))))
    return Sym(sym, kind, name, tuple(rels))


def _doc(b) -> Doc:
    path, lang, occs, syms = "", "", [], []
    for n, wt, v in _fields(b):
        if n == 1 and wt == 2:
            path = str(v, "utf-8", "replace")
        elif n == 4 and wt == 2:
            lang = str(v, "utf-8", "replace")
        elif n == 2 and wt == 2:
            occs.append(_occ(v))
        elif n == 3 and wt == 2:
            syms.append(_sym(v))
    return Doc(path, lang, tuple(occs), tuple(syms))


def read_scip(data) -> Index:
    """Decode a SCIP index (Index.metadata, documents, external_symbols). ScipError on anything that is not one."""
    tool = version = root = ""
    docs: Dict[str, Doc] = {}
    ext: List[Sym] = []
    try:
        for n, wt, v in _fields(memoryview(data)):
            if wt != 2:
                continue
            if n == 1:
                for m, w, x in _fields(v):
                    if m == 3 and w == 2:
                        root = str(x, "utf-8", "replace")
                    elif m == 2 and w == 2:
                        info = {k: str(y, "utf-8", "replace") for k, w2, y in _fields(x) if w2 == 2}
                        tool, version = info.get(1, ""), info.get(2, "")
            elif n == 2:
                d = _doc(v)
                old = docs.get(d.path)
                docs[d.path] = (
                    d if old is None else Doc(d.path, d.language or old.language, old.occs + d.occs, old.syms + d.syms)
                )
            elif n == 3:
                ext.append(_sym(v))
    except ScipError:
        raise
    except (IndexError, ValueError) as e:
        raise ScipError(f"not a SCIP index ({type(e).__name__}: {e})") from e
    return Index(tool, version, root, docs, tuple(ext))


def read_scip_file(path: str, expect_sha: str = "") -> Index:
    """read_scip of a file of at most max_index_bytes; expect_sha (sha256 hex) must match the bytes when given."""
    try:
        if os.path.getsize(path) > config()["max_index_bytes"]:
            raise ScipError(f"{path} is larger than {config()['max_index_bytes']} bytes")
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        raise ScipError(f"{path}: {e}") from e
    if expect_sha and hashlib.sha256(data).hexdigest() != expect_sha:
        raise ScipError(f"{path} is not the index that was stamped (sha256 differs)")
    return read_scip(data)


# ================================================================ SCIP: symbols
# scheme, manager, package, version (a doubled space is an escaped one), then the descriptors
_HEAD = re.compile(r"(?:(?:[^ ]| {2})+ ){4}(.*)", re.S)
_DESC = re.compile(r"(`(?:[^`]|``)+`|[\w+$-]+)(\([^)]*\)\.|[/#.:!])|\[([^\]]+)\]|\(([^)]*)\)")
_SUFFIX = {"/": "namespace", "#": "type", ".": "term", ":": "meta", "!": "macro"}


@lru_cache(maxsize=1 << 17)
def descriptors(symbol: str) -> Optional[Tuple[Tuple[str, str], ...]]:
    """[(name, kind)] of a global symbol, kind in namespace type term meta macro method param tparam; None for a `local`
    symbol; () for a malformed one."""
    if symbol.startswith("local "):
        return None
    head = _HEAD.match(symbol)
    out, i = [], 0
    s = head.group(1) if head else ""
    while head and i < len(s):
        m = _DESC.match(s, i)
        if m is None:
            return ()
        name, suffix, tparam, param = m.groups()
        if name is None:
            out.append((tparam or param, "tparam" if tparam else "param"))
        else:
            name = name[1:-1].replace("``", "`") if name[0] == "`" else name
            out.append((name, "method" if suffix[0] == "(" else _SUFFIX[suffix]))
        i = m.end()
    return tuple(out)


def symbol_name(symbol: str) -> Optional[str]:
    """The name of the last descriptor when it can be a graph node (type, term, method, macro), else None."""
    d = descriptors(symbol)
    return d[-1][0] if d and d[-1][1] in NAMED else None


# ================================================================ SCIP: the package and its stamp
def package_files(tree: Mapping[str, str], root: str, family: str, lg=None) -> Dict[str, str]:
    """{path: blob} of what an index of `family` under `root` is made from: the files of the family and its manifests."""
    lg = lg or _langs.load()
    mani = set(config()["manifests"].get(family, ()))
    pre = root.strip("/")
    pre = pre + "/" if pre else ""
    out = {}
    for p, blob in tree.items():
        if p.startswith(pre):
            lang = lg.detect(p)
            if (lang and lg.family(lang) == family) or posixpath.basename(p) in mani:
                out[p] = blob
    return out


def package_hash(files: Mapping[str, str]) -> str:
    """sha256 over the sorted (path, blob) pairs: the rule of vcs.tree_id, without modes."""
    h = hashlib.sha256(b"sem-package\0")
    for p in sorted(files):
        h.update(f"{p}\0{files[p]}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


class Meta(NamedTuple):
    engine: str
    root: str  # package root, relative to the repository root ("" = the root)
    family: str  # language family of data/langs.json
    input_hash: str
    index_sha256: str


def meta_path(index: str) -> str:
    return index + ".meta.json"


def load_meta(index: str) -> Meta:
    try:
        with open(meta_path(index), encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("schema") != 1:
            raise SemError("schema must be 1")
        return Meta(*(str(d[k]) for k in Meta._fields))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise SemError(f"{meta_path(index)}: {type(e).__name__}: {e}") from e


def meta_for(data: bytes, tree: Mapping[str, str], root: str, family: str, engine: str, lg=None) -> Meta:
    """The Meta of an index (its bytes) just made from `tree` (the {path: blob} the indexer read)."""
    h = hashlib.sha256(data).hexdigest()
    return Meta(engine, root.strip("/"), family, package_hash(package_files(tree, root, family, lg)), h)


def write_meta(index: str, m: Meta) -> None:
    """<index>.meta.json, replaced atomically."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(index)), prefix=".meta-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, **m._asdict()}, fh, indent=1, sort_keys=True)
    os.replace(tmp, meta_path(index))


def stamp(index: str, tree: Mapping[str, str], root: str, family: str, engine: str, lg=None) -> Meta:
    """Write <index>.meta.json for an index just made from `tree` (a snapshot of the files the indexer read)."""
    with open(index, "rb") as fh:
        m = meta_for(fh.read(), tree, root, family, engine, lg)
    write_meta(index, m)
    return m


# ================================================================ SCIP: the overlay
class _Owner:
    """The innermost func/method/class/iface node that holds a 1-based line of a file; the file node outside any."""

    def __init__(self, nodes: Iterable[Node]):
        self.defs: Dict[str, List[Node]] = {}
        self.file: Dict[str, str] = {}
        self.lines: Dict[str, List[Optional[str]]] = {}
        for n in nodes:
            if n.kind in DEF_KINDS and n.line1 <= LINE_MAX:
                self.defs.setdefault(n.path, []).append(n)
            elif n.kind == "file":
                self.file[n.path] = n.key

    def at(self, path: str, line: int) -> Optional[str]:
        if path not in self.lines:  # paint the outermost first: an inner node overwrites its parent
            ns = sorted(self.defs.get(path, ()), key=lambda n: (n.line0 - n.line1, n.line0, n.key))
            arr: List[Optional[str]] = [None] * (max((n.line1 for n in ns), default=0) + 2)
            for n in ns:
                arr[n.line0 : n.line1 + 1] = [n.key] * (n.line1 - n.line0 + 1)
            self.lines[path] = arr
        arr = self.lines[path]
        return (arr[line] if 0 <= line < len(arr) else None) or self.file.get(path)


def _qual_name(key: str) -> str:
    return split_key(key)[2].rsplit(".", 1)[-1]


def _site(e: Edge) -> Optional[Tuple[str, int]]:
    p, _, ln = e.site.rpartition(":")
    return (p, int(ln)) if p and ln.isdigit() else None


class Report(NamedTuple):
    index: str
    engine: str
    status: str  # applied | partial (coverage below the minimum: S edges only) | stale | invalid
    detail: str
    coverage: float  # share of the package's func/method/class/iface nodes the index defines
    added: int  # S edges for references the graph had no edge for (relationship edges included)
    confirmed: int  # N and T edges that became S
    refuted: int  # N edges dropped
    kept: int = 0  # contradicted N edges that stay, unproven to be one site (W dc.sem-refute)


class Cand(NamedTuple):
    """An N edge the index contradicts: its line resolved `name` elsewhere, at `seen` distinct positions."""

    edge: Edge
    name: str
    seen: int


class Delta(NamedTuple):
    report: Report
    confirm: Tuple[Tuple[Edge, Edge], ...]  # (old edge, its S replacement)
    refute: Tuple[Cand, ...]  # candidates: refine drops the ones whose line text proves them
    add: Tuple[Edge, ...]
    ev: Optional[Evidence]


def _skey(symbol: str, path: str) -> str:
    """A `local` symbol is scoped to its document."""
    return f"{path} {symbol}" if symbol.startswith("local ") else symbol


def _bind(
    ix: Index, gp: Mapping[str, str], nodes: Sequence[Node], files
) -> Tuple[Dict[str, str], Dict[str, str], Set[str]]:
    """(symbol -> node key, symbol -> "path:line" of its definition, the keys of the package's definitions). A symbol is the
    one of a node when its definition occurrence names it inside the node's lines (a `local` symbol, which has no name, when
    the node is the only one that starts on that line); a symbol that lands on two nodes is bound to neither."""
    by_name: Dict[Tuple[str, str], List[Node]] = {}
    by_line: Dict[Tuple[str, int], List[Node]] = {}
    total: Set[str] = set()
    for n in nodes:
        if n.kind in DEF_KINDS and n.path in files:
            total.add(n.key)
            by_name.setdefault((n.path, _qual_name(n.key)), []).append(n)
            by_line.setdefault((n.path, n.line0), []).append(n)
    bound: Dict[str, str] = {}
    site: Dict[str, str] = {}
    clash: Set[str] = set()
    for p in sorted(ix.docs):
        for o in ix.docs[p].occs:
            if not o.roles & DEFINITION:
                continue
            line, path, name = o.line + 1, gp[p], symbol_name(o.symbol)
            if descriptors(o.symbol) is None:
                near = by_line.get((path, line), ())
                pick = near[0] if len(near) == 1 else None
            else:
                near = [n for n in by_name.get((path, name), ()) if n.line0 <= line <= n.line1] if name else []
                pick = min(near, key=lambda n: (n.line1 - n.line0, n.line0, n.key), default=None)
            if pick is not None:
                k = _skey(o.symbol, path)
                if bound.setdefault(k, pick.key) != pick.key:
                    clash.add(k)
                site.setdefault(k, f"{path}:{line}")
    for k in clash:
        del bound[k]
    return bound, site, total


def _dispatching(n: Node, kinds: Mapping[Tuple[str, str], str]) -> bool:
    """A member that other implementations answer for (abstract, protocol, a member of an interface): a call that resolves to
    it may run any of them, so it says nothing against a name edge to one of those."""
    if n.flags & (F_ABSTRACT | F_PROTOCOL):
        return True
    return kinds.get((n.path, split_key(n.key)[2].rpartition(".")[0])) == "iface"


def _overlay(task) -> Delta:
    """One index against the slice of the graph that belongs to its package (a pure function: it runs in a worker)."""
    index, meta, files, nodes, edges, min_cov = task
    rep = Report(index, meta.engine, "invalid", "", 0.0, 0, 0, 0)
    try:
        ix = read_scip_file(index, meta.index_sha256)
    except SemError as e:
        return Delta(rep._replace(detail=str(e)), (), (), (), None)
    gp = {p: posixpath.normpath(posixpath.join(meta.root, p)) for p in ix.docs}
    bound, defsite, total = _bind(ix, gp, nodes, files)
    node = {n.key: n for n in nodes}
    kinds = {(n.path, split_key(n.key)[2]): n.kind for n in nodes}
    syms_of: Dict[str, Set[str]] = {}
    for k, nk in bound.items():
        syms_of.setdefault(nk, set()).add(k)
    cov = len(total & set(syms_of)) / len(total) if total else 1.0
    # (path, line) -> [(symbol, the name it resolved to, column)]
    refs: Dict[Tuple[str, int], List[Tuple[str, Optional[str], int]]] = {}
    for p in sorted(ix.docs):
        for o in ix.docs[p].occs:
            if o.roles & (DEFINITION | FORWARD) or not o.symbol:
                continue
            k = _skey(o.symbol, gp[p])
            named = descriptors(o.symbol) is not None
            nm = symbol_name(o.symbol) if named else (_qual_name(bound[k]) if k in bound else None)
            refs.setdefault((gp[p], o.line + 1), []).append((k, nm, o.col))
    confirm, refute, pairs = [], [], set()
    for e in edges:
        pairs.add((e.src, e.dst))
        s = _site(e)
        if e.tier not in ("N", "T") or e.kind not in REF_KINDS or s not in refs or e.dst not in syms_of:
            continue
        here = refs[s]
        if any(k in syms_of[e.dst] for k, _, _ in here):
            confirm.append((e, e._replace(tier="S", ev=0)))
        elif e.tier == "N" and cov >= min_cov and e.src != e.dst:
            name = _qual_name(e.dst)
            same = [(k, col) for k, nm, col in here if nm == name]  # the name resolved, to something else
            # ... and all of it is concrete code of this package: an external or foreign symbol of the name has no opinion
            if same and all(k in bound and not _dispatching(node[bound[k]], kinds) for k, _ in same):
                refute.append(Cand(e, name, len({col for _, col in same})))
    own, add = _Owner(nodes), []
    for (path, line), here in sorted(refs.items()):  # a resolved reference the graph has no edge for
        src = own.at(path, line)
        for k, _, _ in here:
            dst = bound.get(k)
            if src and dst and src != dst and (src, dst) not in pairs:
                pairs.add((src, dst))
                add.append(Edge(src, dst, "ref", "S", f"{path}:{line}", 0))
    have = {(e.src, e.dst, e.kind) for e in edges}
    for p in sorted(ix.docs):  # is_implementation: a type implements its base, a member overrides the base's member
        for sy in ix.docs[p].syms:
            sub = bound.get(_skey(sy.symbol, gp[p]))
            for r in (r for r in sy.rels if r.implementation):
                base = bound.get(_skey(r.symbol, gp[p]))
                if not sub or not base or sub == base:
                    continue
                ends = [(sub, base, "impl")] + (
                    [] if node[sub].kind in ("class", "iface") else [(base, sub, "override")]
                )
                for a, b, kind in ends:
                    if (a, b, kind) not in have:
                        have.add((a, b, kind))
                        add.append(Edge(a, b, kind, "S", defsite.get(_skey(sy.symbol, gp[p]), ""), 0))
    ok = cov >= min_cov
    why = (
        "" if ok else f"the index defines {cov:.1%} of the package's definitions (< {min_cov:.0%}): nothing was refuted"
    )
    rep = rep._replace(status="applied" if ok else "partial", detail=why, coverage=round(cov, 4))
    rep = rep._replace(added=len(add), confirmed=len(confirm))  # refuted: refine counts what the text proves
    ev = evidence(ix.tool or meta.engine, meta.root, 0, f"scip {ix.version} {len(ix.docs)} documents", SEM_V)
    return Delta(rep, tuple(confirm), tuple(refute), tuple(add), ev)


@lru_cache(maxsize=4096)
def _word(name: str):
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


def _proven(cands: Sequence[Cand], text_of: Callable[[str], Optional[str]], workers: int) -> Set[Cand]:
    """The candidates whose line text holds the name at least once and no more often than the index has distinct positions of it:
    every site of the name on the line is resolved, so none of them is the call the name edge stands for. One thread per file.
    """
    by_path: Dict[str, List[Cand]] = {}
    for c in cands:
        by_path.setdefault(_site(c.edge)[0], []).append(c)  # a candidate has a site: it was found by it

    def check(item) -> List[Cand]:
        path, rows = item
        try:
            text = text_of(path)
        except (OSError, ValueError):
            text = None
        lines = text.split("\n") if text else []
        out = []
        for c in rows:
            ln = _site(c.edge)[1]
            if 0 < ln <= len(lines) and 1 <= len(_word(c.name).findall(lines[ln - 1])) <= c.seen:
                out.append(c)
        return out

    w = max(1, min(par.workers(workers), len(by_path)))
    done = par.run_units(sorted(by_path.items()), check, jobs=w, key=lambda u: u[0], kind="thread")
    return {c for rows in done for c in rows}


def _kept_note(kept: Sequence[Cand], reason: str) -> str:
    sites = ", ".join(sorted({c.edge.site for c in kept})[:3])
    return f"{len(kept)} name edges kept ({reason}), e.g. at {sites}"


def _no_refute(engine: str) -> str:
    """Why the overlays of `engine` never refute (data/sem.json "engines"), "" when they may."""
    return str((config().get("engines") or {}).get(engine, {}).get("no_refute", ""))


class Refined(NamedTuple):
    graph: Graph
    findings: Tuple[Finding, ...]
    reports: Tuple[Report, ...]
    covered: frozenset = frozenset()  # paths of the packages whose overlay was applied in full (coverage >= minimum)


def refine(
    graph: Graph,
    tree: Mapping[str, str],
    indexes: Sequence[str],
    *,
    workers: Optional[int] = None,
    min_coverage: Optional[float] = None,
    text_of: Optional[Callable[[str], Optional[str]]] = None,
    lg=None,
) -> Refined:
    """Apply the SCIP overlays of `indexes` (paths of index.scip files, each with its meta sidecar) to the graph. `tree` is the
    current {path: blob sha} of the work tree: an overlay whose input hash is not the hash of its package now is ignored.
    text_of(path) is the text of a work-tree file (the one the index was made from): without it no name edge is refuted.
    """
    lg = lg or _langs.load()
    min_cov = config()["min_coverage"] if min_coverage is None else min_coverage
    reports: List[Report] = []
    found: List[Finding] = []
    plan: List[Tuple[str, Meta, Dict[str, str]]] = []
    for ix in sorted(set(indexes)):
        try:
            meta = load_meta(ix)
        except SemError as e:
            reports.append(Report(ix, "", "invalid", str(e), 0.0, 0, 0, 0))
            found.append(_w("dc.sem-index", f"SCIP index ignored: {e}"))
            continue
        files = package_files(tree, meta.root, meta.family, lg)
        if package_hash(files) == meta.input_hash:
            plan.append((ix, meta, files))
        else:
            reports.append(Report(ix, meta.engine, "stale", "input hash differs", 0.0, 0, 0, 0))
            found.append(
                _w("dc.sem-stale", f"SCIP index {ix} ({meta.engine}) is older than the files of its package: ignored")
            )
    if not plan:
        return Refined(graph, tuple(found), tuple(reports))
    files_of = {ix: files for ix, _, files in plan}
    # one pass over the graph however many packages there are: path -> the plan entries that own it
    where: Dict[str, List[int]] = {}
    for i, (_, _, files) in enumerate(plan):
        for p in files:
            where.setdefault(p, []).append(i)
    part: List[Tuple[list, list]] = [([], []) for _ in plan]
    for n in graph.nodes:
        for i in where.get(n.path, ()):
            part[i][0].append(n)
    for e in graph.edges:
        for i in where.get(e.site.rpartition(":")[0], ()):
            part[i][1].append(e)
    tasks = [(ix, meta, frozenset(files), *part[i], min_cov) for i, (ix, meta, files) in enumerate(plan)]
    w = max(1, min(par.workers(workers), len(tasks)))
    deltas = par.run_units(tasks, _overlay, jobs=w, key=lambda t: t[0], kind="process" if w > 1 else "thread")
    swap: Dict[Edge, Edge] = {}
    drop: Set[Edge] = set()
    new: List[Edge] = []
    pool = list(graph.evid)
    asked = [c for d in deltas if not _no_refute(d.report.engine) for c in d.refute]
    proven = _proven(asked, text_of, workers) if asked and text_of else set()
    for d in deltas:
        rep = d.report
        if rep.status != "applied":
            code = "dc.sem-index" if rep.status == "invalid" else "dc.sem-coverage"
            found.append(_w(code, f"SCIP index {rep.index}: {rep.detail}"))
        if rep.status == "invalid":
            reports.append(rep)
            continue
        ok = [c for c in d.refute if c in proven]
        kept = [c for c in d.refute if c not in proven]
        if kept:
            why = _no_refute(rep.engine) or (
                "the line holds the name at more sites than the index resolved"
                if text_of
                else "no text of the line was given"
            )
            found.append(_w("dc.sem-refute", f"SCIP index {rep.index}: {_kept_note(kept, why)}"))
        reports.append(rep._replace(refuted=len(ok), kept=len(kept)))
        at = len(pool)
        pool.append(d.ev)
        swap.update({old: s._replace(ev=at) for old, s in d.confirm if old not in swap})
        drop.update(c.edge for c in ok)
        new += [e._replace(ev=at) for e in d.add]
    out = tuple(sorted(reports, key=lambda r: r.index))
    full = frozenset(p for r in out if r.status == "applied" for p in files_of[r.index])
    if not (swap or drop or new):
        return Refined(graph, tuple(found), out, full)
    # a confirmation beats a refutation: an edge in `swap` stays whatever another index says
    edges = [swap.get(e, e) for e in graph.edges if e in swap or e not in drop]
    return Refined(canonical(graph.nodes, edges + new, graph.roots, pool), tuple(found), out, full)


# ================================================================ LSP: the client
class LspError(Exception):
    """The server is gone, answered with an error, or did not answer in time."""


class LspTimeoutError(LspError):
    pass


class LspBudgetError(LspError):
    """The session's request limit is reached."""


def _list(res, method: str) -> list:
    if res is None:
        return []
    if not isinstance(res, list):
        raise LspError(f"{method}: an answer of the wrong shape ({type(res).__name__})")
    return res


def _obj(x) -> dict:
    """What a server sent where an object is due: anything else is an empty one (the fields that follow are then absent)."""
    return x if isinstance(x, dict) else {}


LANGUAGE_ID = {"tsx": "typescriptreact", "jsx": "javascriptreact"}
FRAME_MAX = 1 << 26  # one message of a server
FLOOD = 1 << 28  # everything a server may send in one session


class Lsp:
    """A language server over stdio: `with Lsp(argv, root, deadline, env=...) as c:` runs initialize and initialized, then
    c.references / c.incoming_calls. Every wait ends at `deadline` (an absolute time of `clock`); every query is one request
    and the one after `limit` is an LspBudgetError. A malformed message or answer is an LspError, never another exception.
    The server runs in its own session and is killed with its process group on exit."""

    def __init__(
        self,
        argv: Sequence[str],
        root: str,
        deadline: float,
        *,
        env: Optional[dict] = None,
        init: Optional[dict] = None,
        limit: Optional[int] = None,
        clock=time.monotonic,
    ):
        self.root, self.deadline, self.clock, self.init = os.path.realpath(root), deadline, clock, init
        self.limit, self.queries = limit, 0  # requests of references / call hierarchy: at most `limit`
        self.q: queue.Queue = queue.Queue()
        self.got: Dict[int, dict] = {}
        self.tokens: Set[str] = set()
        self.progress = False  # the server announced work ($/progress begin, workDoneProgress/create) at least once
        self.opened: Set[str] = set()
        self.caps: dict = {}
        self.n = 0
        pipes = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL}
        self.proc = subprocess.Popen(list(argv), cwd=self.root, env=env, start_new_session=True, **pipes)
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()
        # a server that stops reading blocks a write for ever: whatever it does, it is gone a second after the deadline
        self.watchdog = threading.Timer(max(0.0, deadline - clock()) + 1.0, self._kill)
        self.watchdog.daemon = True
        self.watchdog.start()

    # -- transport
    def _pump(self) -> None:
        out, total = self.proc.stdout, 0
        try:
            while True:
                n = None
                for line in iter(out.readline, b"\r\n"):  # the headers end with an empty line
                    if not line:
                        raise EOFError
                    k, _, v = line.partition(b":")
                    if k.strip().lower() == b"content-length":
                        n = int(v)
                total += n or 0
                if n is None or n > FRAME_MAX or total > FLOOD:
                    raise ValueError("bad frame or too much output")
                self.q.put(json.loads(out.read(n)))
        except (EOFError, ValueError, OSError, RecursionError):  # RecursionError: a hostile, deeply nested body
            self.q.put(None)

    def _send(self, msg: dict) -> None:
        body = json.dumps({"jsonrpc": "2.0", **msg}, separators=(",", ":")).encode()
        try:
            self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise LspError(f"the server is gone ({e})") from e

    def _take(self, until: float) -> None:
        """Handle one message of the server; LspTimeoutError when none comes by `until`, LspError when the channel is dead."""
        try:
            m = self.q.get(timeout=max(0.0, until - self.clock()) + 0.001)
        except queue.Empty:
            raise LspTimeoutError("no answer before the deadline") from None
        if m is None:
            raise LspError("the server closed the channel")
        if not isinstance(m, dict):
            raise LspError(f"a message that is not a JSON-RPC object ({type(m).__name__})")
        method, mid, p = m.get("method"), m.get("id"), _obj(m.get("params"))
        if mid is not None and not isinstance(mid, (int, str)):
            raise LspError("a message with an id that is neither a number nor a string")
        if method and mid is not None:  # a request of the server: acknowledge it
            if method == "window/workDoneProgress/create":
                self.tokens.add(str(p.get("token")))
                self.progress = True
            items = p.get("items")
            answer = [None] * len(items) if method == "workspace/configuration" and isinstance(items, list) else None
            try:
                self._send({"id": mid, "result": answer})
            except LspError:  # the server may be gone already; what it said before is still in the queue
                pass
        elif method == "$/progress":
            kind, tok = _obj(p.get("value")).get("kind"), str(p.get("token"))
            if kind == "begin":
                self.tokens.add(tok)
                self.progress = True
            elif kind == "end":
                self.tokens.discard(tok)
        elif mid is not None:
            self.got[mid] = m

    def request(self, method: str, params: dict, until: Optional[float] = None):
        self.n += 1
        mid, until = self.n, self.deadline if until is None else until
        self._send({"id": mid, "method": method, "params": params})
        while mid not in self.got:
            self._take(until)
        r = self.got.pop(mid)
        if r.get("error"):
            err = r["error"]
            raise LspError(f"{method}: {err.get('message', err) if isinstance(err, dict) else err}")
        return r.get("result")

    def notify(self, method: str, params: dict) -> None:
        self._send({"method": method, "params": params})

    # -- session
    def __enter__(self) -> "Lsp":
        try:
            uri = self.uri("")
            caps = {"window": {"workDoneProgress": True}, "workspace": {"configuration": True}}
            caps["textDocument"] = {"references": {}, "callHierarchy": {}}
            p = {
                "processId": os.getpid(),
                "rootUri": uri,
                "capabilities": caps,
                "clientInfo": {"name": "dead-code-checker"},
            }
            p["workspaceFolders"] = [{"uri": uri, "name": os.path.basename(self.root) or "root"}]
            if self.init:
                p["initializationOptions"] = self.init
            self.caps = _obj(_obj(self.request("initialize", p)).get("capabilities"))
            self.notify("initialized", {})
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _kill(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            self.proc.kill()

    def close(self) -> None:
        try:
            if self.proc.poll() is None and self.reader.is_alive():  # a dead channel cannot answer
                self.request("shutdown", {}, min(self.deadline, self.clock() + 1.0))
                self.notify("exit", {})
                self.proc.wait(timeout=1.0)  # a server that was asked to exit gets a moment to do it
        except (LspError, OSError, subprocess.TimeoutExpired):
            pass
        self.watchdog.cancel()
        self._kill()
        self.proc.wait()
        self.reader.join(timeout=2.0)
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                if pipe is self.proc.stdin or not self.reader.is_alive():
                    pipe.close()
            except OSError:  # a dead server leaves unflushed bytes behind: BrokenPipeError
                pass

    # -- queries
    def uri(self, rel: str) -> str:
        return pathlib.Path(os.path.join(self.root, rel)).as_uri()

    def rel(self, uri: str) -> Optional[str]:
        """The path of a file URI relative to the root, None outside it."""
        r = os.path.relpath(os.path.realpath(unquote(urlparse(uri).path)), self.root).replace(os.sep, "/")
        return None if r.startswith("..") else r

    def open(self, rel: str, lang: str, text: str) -> None:
        if rel not in self.opened:
            self.opened.add(rel)
            doc = {"uri": self.uri(rel), "languageId": LANGUAGE_ID.get(lang, lang), "version": 1, "text": text}
            self.notify("textDocument/didOpen", {"textDocument": doc})

    def settle(self, grace: float, cap: Optional[float] = None) -> bool:
        """Wait up to `grace` for the server to announce work, then up to `cap` more (to the deadline when None) for it to be
        done. True = it announced it and finished: only then does an empty `references` answer mean something."""
        quiet = min(self.deadline, self.clock() + grace)
        busy = self.deadline if cap is None else min(self.deadline, quiet + cap)
        try:
            while not self.progress:
                self._take(quiet)
            while self.tokens:
                self._take(busy)
        except LspTimeoutError:
            pass
        return self.indexed

    @property
    def indexed(self) -> bool:
        return self.progress and not self.tokens

    def _query(self, method: str, params: dict):
        if self.limit is not None and self.queries >= self.limit:
            raise LspBudgetError(f"{self.limit} requests were made")
        self.queries += 1
        return self.request(method, params)

    def _at(self, rel: str, line: int, col: int) -> dict:
        return {"textDocument": {"uri": self.uri(rel)}, "position": {"line": line, "character": col}}

    def references(self, rel: str, line: int, col: int) -> List[Tuple[str, int]]:
        """[(path, 0-based line)] of the references to the symbol at a position, the declaration excluded, inside the root."""
        ask = {**self._at(rel, line, col), "context": {"includeDeclaration": False}}
        res = _list(self._query("textDocument/references", ask), "references")
        try:
            return self._locs((x.get("uri") or x.get("targetUri"), x.get("range") or x.get("targetRange")) for x in res)
        except (AttributeError, TypeError, ValueError) as e:
            raise LspError(f"textDocument/references: an answer of the wrong shape ({type(e).__name__})") from e

    def incoming_calls(self, rel: str, line: int, col: int) -> List[Tuple[str, int]]:
        """The same through the call hierarchy (prepareCallHierarchy, then incomingCalls of each item): calls only. Every
        request counts as one query."""
        out = []
        try:
            for item in _list(
                self._query("textDocument/prepareCallHierarchy", self._at(rel, line, col)), "prepareCallHierarchy"
            ):
                for c in _list(self._query("callHierarchy/incomingCalls", {"item": item}), "incomingCalls"):
                    caller = _obj(c.get("from"))
                    out.append((caller.get("uri"), (c.get("fromRanges") or [caller.get("range")])[0]))
            return self._locs(out)
        except (AttributeError, TypeError, ValueError, IndexError, KeyError) as e:
            raise LspError(f"callHierarchy: an answer of the wrong shape ({type(e).__name__})") from e

    def _locs(self, pairs) -> List[Tuple[str, int]]:
        found = {
            (self.rel(u), int(_obj(_obj(rng).get("start")).get("line", 0)))
            for u, rng in pairs
            if isinstance(u, str) and u
        }
        return sorted((p, ln) for p, ln in found if p is not None)


# ================================================================ LSP: servers and arbitration
class Spec(NamedTuple):
    engine: str
    argv: Tuple[str, ...]  # the no-network prefix (when the host has one), the trusted binary, its arguments
    env: Tuple[Tuple[str, str], ...]  # the registry's network-free environment of the engine
    init: Optional[dict] = None  # initializationOptions of the server (data/sem.json: "init")
    netless: bool = True  # False: this host has no no-network namespace (W dc.sem-lsp-sandbox)


def servers(
    families: Iterable[str],
    *,
    reg: Optional[dict] = None,
    lock: Optional[dict] = None,
    sandboxed: bool = False,
) -> Dict[str, object]:
    """{family: Spec} for the families whose language server is installed and trusted, {family: reason} for the others. A
    server that runs project code (data/sem.json needs_sandbox: rust-analyzer) is offered only with sandboxed=True, the
    caller's word that `root` is a scratch copy of the tree and the server runs without network. A Spec says
    whether the no-network namespace was available (Spec.netless)."""
    from . import engines as _eng

    reg = reg or _eng.load_registry()
    lock = lock if lock is not None else _eng.cached_lock()
    prefix = tuple(_eng.netless_prefix())
    out: Dict[str, object] = {}
    for fam in sorted(set(families)):
        s = config()["lsp"]["servers"].get(fam)
        if s is None:
            out[fam] = f"no language server is configured for {fam}"
            continue
        if s.get("needs_sandbox") and not sandboxed:
            out[fam] = (
                f"{s['engine']} runs project code (build scripts, proc-macros): it is not started outside the sandbox"
            )
            continue
        res = _eng.find_engine(reg, lock, s["engine"], names=(s["exe"],))
        if res.status != "ok":
            hint = f"; install: {_eng.install_line(reg, s['engine'])}" if res.status == "missing" else ""
            out[fam] = (
                f"{s['engine']} is {res.status}: {res.detail}{hint}"
                if res.detail
                else f"{s['engine']} is {res.status}: {_eng.install_line(reg, s['engine'])}"
            )
            continue
        env = tuple(sorted(((reg["engines"].get(s["engine"]) or {}).get("env") or {}).items()))
        out[fam] = Spec(s["engine"], prefix + (res.path, *s["args"]), env, s.get("init"), bool(prefix))
    return out


_KW = r"(?:def|class|func|fn|function|fun|interface|struct|enum|trait|type|impl|object|sub)"


def name_position(text: str, node: Node) -> Optional[Tuple[int, int]]:
    """(0-based line, UTF-16 column) of the name of a definition: the first `<keyword> [(receiver)] name` inside the node's
    lines, else the first whole-word `name` (a method that has no keyword)."""
    name = re.escape(_qual_name(node.key))
    first = max(0, node.line0 - 1)
    lines = text.split("\n")[first : max(node.line0, node.line1)]
    for pat in (rf"\b{_KW}\s+(?:\([^)]*\)\s*)?({name})\b", rf"\b({name})\b"):
        for i, ln in enumerate(lines):
            m = re.search(pat, ln)
            if m:
                return first + i, len(ln[: m.start(1)].encode("utf-16-le")) // 2
    return None


def _family(lg, lang: str) -> str:
    return (lg.family(lang) if lang in lg.langs else None) or lang


class Arb(NamedTuple):
    graph: Graph  # the input graph plus the S edges the servers found
    status: Dict[str, str]  # candidate key -> referenced | unreferenced | unconfirmed | skipped
    queries: int  # requests sent to the servers
    findings: Tuple[Finding, ...]


def _serve(unit) -> tuple:
    """One language: one server, the rows in order, until the deadline or the request limit. Runs in a thread.
    -> (family, {candidate: (locations, silence counts)}, requests made, note). Silence counts when the server answered a
    references request after it announced its indexing and finished it."""
    fam, spec, root, rows, deadline, home, clock, (grace, settle, maxq) = unit
    got: Dict[str, Tuple[list, bool]] = {}
    note, c = "", None
    home = os.path.join(home, fam)
    os.makedirs(home, exist_ok=True)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "TMPDIR": home, **dict(spec.env)}
    try:
        with Lsp(spec.argv, root, deadline, env=env, init=spec.init, limit=maxq, clock=clock) as c:
            c.settle(grace, settle)
            refs_ok, calls_ok = bool(c.caps.get("referencesProvider")), bool(c.caps.get("callHierarchyProvider"))
            if not (refs_ok or calls_ok):
                raise LspError("the server offers neither references nor call hierarchy")
            for key, path, lang, text, line, col in rows:
                if clock() >= deadline:
                    break
                c.open(path, lang, text)
                try:
                    locs = c.references(path, line, col) if refs_ok else c.incoming_calls(path, line, col)
                except LspBudgetError:
                    break
                got[key] = (locs, refs_ok and c.indexed)
    except (LspError, OSError) as e:
        note = f"{type(e).__name__}: {e}"
    return fam, got, c.queries if c else 0, note


def arbitrate(
    graph: Graph,
    cands: Sequence[str],
    root: str,
    text_of: Callable[[str], Optional[str]],
    specs: Mapping[str, object],
    *,
    max_queries: Optional[int] = None,
    budget_s: Optional[float] = None,
    grace_s: Optional[float] = None,
    settle_s: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    workers: Optional[int] = None,
    lg=None,
) -> Arb:
    """Ask the language servers about `cands` (node keys, the most interesting first: amb, dead-likely, unknown): at most
    max_queries requests per language (a call-hierarchy question is 1 + one per item), all languages within budget_s
    together, the languages in parallel. `specs` is servers(): a family without a Spec is reported once (W dc.sem-lsp-failed),
    never skipped silently; a Spec of a host without a no-network namespace is reported (W dc.sem-lsp-sandbox). text_of(path)
    is the text of a work-tree file. `root` is where the servers run, and they may write there (caches, target/): the caller
    gives a scratch copy of the tree, this function does not make one."""
    cfg = config()["lsp"]
    maxq = cfg["max_queries"] if max_queries is None else max_queries
    secs = cfg["budget_s"] if budget_s is None else budget_s
    deadline, grace = clock() + secs, cfg["grace_s"] if grace_s is None else grace_s
    lg = lg or _langs.load()
    nodes = {n.key: n for n in graph.nodes}
    status = {key: "skipped" for key in cands}
    rows: Dict[str, list] = {}
    cut: Dict[str, int] = {}  # family -> candidates beyond max_queries
    why: Dict[str, str] = {}  # family -> why it has no server
    unplaced = 0
    for key in cands:
        n = nodes.get(key)
        fam = _family(lg, n.lang) if n is not None else None
        spec = specs.get(fam)
        if fam is not None and not isinstance(spec, Spec):
            why[fam] = spec or "no language server was given for it"
            continue
        if len(rows.get(fam, ())) >= maxq:  # no need to read the file of a candidate nobody will ask about
            cut[fam] = cut.get(fam, 0) + 1
            continue
        text = text_of(n.path) if n is not None else None
        pos = name_position(text, n) if text else None
        if pos is None:
            unplaced += 1
        else:
            rows.setdefault(fam, []).append((key, n.path, n.lang, text, *pos))
    found = [_w("dc.sem-lsp-failed", f"no language server answer for {f}: {r}") for f, r in sorted(why.items())]
    if unplaced:
        msg = f"{unplaced} candidates could not be placed (not in the graph, file unreadable, or name not in its lines)"
        found.append(_w("dc.sem-lsp-failed", msg))
    runnable = {f: r for f, r in sorted(rows.items()) if r}
    results = {}
    if runnable and clock() < deadline:
        with tempfile.TemporaryDirectory(prefix="dcc-lsp-") as home:
            lim = (grace, cfg["settle_s"] if settle_s is None else settle_s, maxq)
            units = [(f, specs[f], root, r, deadline, home, clock, lim) for f, r in runnable.items()]
            w = max(1, min(par.workers(workers), len(units)))
            res = par.run_units(units, _serve, jobs=w, key=lambda u: u[0], kind="thread")
            results = {r[0]: r for r in res}
    own = _Owner(graph.nodes)
    hits, queried = [], 0
    for fam, fam_rows in runnable.items():
        _, got, used, note = results.get(fam, (fam, {}, 0, ""))
        queried += used
        spec = specs[fam]
        eng = spec.engine
        for r in fam_rows:
            if r[0] in got:  # a reference of the candidate's own body is none (recursion), nor is its declaration
                locs, silent = got[r[0]]
                outside = [(p, ln) for p, ln in locs if own.at(p, ln + 1) != r[0]]
                status[r[0]] = "referenced" if outside else "unreferenced" if silent else "unconfirmed"
                hits += [(r[0], eng, p, ln) for p, ln in outside]
        left = sum(1 for r in fam_rows if r[0] not in got) + cut.get(fam, 0)
        if note:
            found.append(_w("dc.sem-lsp-failed", f"{eng} ({fam}): {note}"))
        elif left:
            found.append(
                _w(
                    "dc.sem-lsp-budget",
                    f"{eng} ({fam}): {left} candidates were not queried (budget: {maxq} requests, {secs:g}s)",
                )
            )
        if not note and "unconfirmed" in {status[r[0]] for r in fam_rows}:
            found.append(
                _w(
                    "dc.sem-lsp-incomplete",
                    f"{eng} ({fam}) did not report finished indexing: no reference found is no evidence",
                )
            )
        if fam in results and not spec.netless:
            msg = f"{eng} ({fam}) ran without a no-network namespace (unshare -rn is not available here)"
            found.append(_w("dc.sem-lsp-sandbox", msg))
    pool, edges = list(graph.evid), []
    have = {(e.src, e.dst) for e in graph.edges if e.tier == "S"}
    for cand, eng, path, line in sorted(set(hits)):
        src = own.at(path, line + 1)
        if src and (src, cand) not in have:
            have.add((src, cand))
            pool.append(evidence(f"lsp:{eng}", "", 0, "textDocument/references", SEM_V))
            edges.append(Edge(src, cand, "ref", "S", f"{path}:{line + 1}", len(pool) - 1))
    g = canonical(graph.nodes, [*graph.edges, *edges], graph.roots, pool) if edges else graph
    return Arb(g, dict(sorted(status.items())), queried, tuple(found))


class Branch(NamedTuple):
    graph: Graph
    status: Dict[str, str]  # candidate key -> covered | referenced | unreferenced | unconfirmed | skipped
    findings: Tuple[Finding, ...]
    reports: Tuple[Report, ...]
    queries: int


def branch(
    graph: Graph,
    tree: Mapping[str, str],
    root: str,
    text_of: Callable[[str], Optional[str]],
    cands: Sequence[str],
    indexes: Sequence[str] = (),
    specs: Optional[Mapping[str, object]] = None,
    *,
    lg=None,
    **limits,
) -> Branch:
    """Design 3.3 step 5, the semantic step of the branch level: apply the fresh SCIP overlays (refine), then ask the language
    servers (arbitrate) about the candidates that no overlay applied in full covers. `limits` are workers, max_queries,
    budget_s, grace_s and clock of arbitrate; specs defaults to servers() of the languages of those candidates."""
    lg = lg or _langs.load()
    r = refine(graph, tree, indexes, workers=limits.get("workers"), text_of=text_of, lg=lg)
    rest = [k for k in cands if split_key(k)[1] not in r.covered]
    if specs is None:
        specs = servers({_family(lg, split_key(k)[0]) for k in rest})
    a = arbitrate(r.graph, rest, root, text_of, specs, lg=lg, **limits)
    status = {k: "covered" for k in cands}
    status.update(a.status)
    return Branch(a.graph, status, r.findings + a.findings, r.reports, a.queries)
