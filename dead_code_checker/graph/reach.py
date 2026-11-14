"""graph.reach: reachability and verdicts over the symbol graph. Standard library only.

    prep = prepare(graph)                     CSR arrays (array('i')), root groups, dangling roots
    res = forward(prep, mode="closed_world")  full multi-source Dijkstra on a bucket queue (Dial), verdict of every node
    res = analyze(graph)                      the two together
    res.verdict(key) / res.verdicts() / res.counts() / res.clusters / res.excluded(key) / res.digest()
    res.why_alive(key) -> WhyAlive | None     the par[] chain from the root: [Step(src, dst, kind, tier, site)], render()
    res.why_dead(key) -> WhyDead              roots seen, every incoming edge and why it does not count, render() = Finding.why
    touched(prep, keys, cap=50_000)           bounded reverse BFS per key for commit/branch (no full forward pass);
                                              more than `cap` steps -> verdict "unknown", never a guess

A test root on a file node seeds every node of that file (test functions, classes, methods and fixtures are collected by the
test framework, nothing calls them by name). A hard root of kind export (a package entry: package.json main/module/exports, the library
of a published Cargo crate) on a file node also seeds the exported nodes of that file (public ones too: a Rust `pub fn`) and the
public members of its exported classes, and the same for what its re-exports hand on (link rule `reexport`: a barrel index over class files): the API of a package is used by its consumers, not by
this tree. A hard root of kind script (a classic `<script src>` of an html page) seeds every top-level definition of the file and the public
members of its classes: they are globals that the page reaches by name.
Root groups (what the roots are, not how sure the edge is): prod-hard = hard roots of any kind but test and doc-cmd, prod-soft
= soft ones, test = kind "test", doc = kind "doc-cmd". Passes, each claims only still unreached nodes, so a node gets the
verdict of the first pass that reaches it and the why-alive path of that pass:
  1. alive       Dijkstra from prod-hard over S/T edges only (the cheapest path may not hide an all-T path behind an N edge)
  2. weak-alive  Dijkstra over every edge, seeded by the relaxations out of the alive nodes and by prod-soft roots (dist 0)
  3. test-only   from the test roots, any edge        4. doc-only   from the doc-cmd roots, any edge
Left over: dead; the weakly connected components of the dead subgraph that have >= 2 nodes or a self loop are dead-cluster
(the dead chains a text grep takes for alive; `res.clusters`); library mode: a left-over public/exported non-file node is
public-unused, a node of a package named in `unindexed` is unknown. Every tier counts as an edge (over-approximation), the
tier only picks the class and the path. Ties are broken by (distance, node index, edge index): the same Graph gives the same
Result. res.excluded(key): generated/vendor/test nodes keep their verdict but the caller does not report them.
"""

from __future__ import annotations

import hashlib
import json
from array import array
from collections import deque
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from .model import F_GENERATED, F_TEST, MODES, REEXPORT_RULE, TIERS, Edge, Graph, Root, split_key

CAP = 50_000
UNREACHED, ALIVE, WEAK, TEST, DOC = range(5)
_NAME = {ALIVE: "alive", WEAK: "weak-alive", TEST: "test-only", DOC: "doc-only"}
_CLS = ("hard", "soft", "test", "doc")
_STRONG = TIERS["T"]  # an edge with weight <= this is S or T: a confirmed use
INF = 1 << 30


class Prep(NamedTuple):
    graph: Graph
    keys: Tuple[str, ...]
    idx: Dict[str, int]
    off: array  # forward CSR: edges of node v are j in range(off[v], off[v+1])
    dst: array
    w: array  # tier weight of the forward edge
    eid: array  # index into graph.edges
    roff: array  # reverse CSR
    rsrc: array
    rw: array
    reid: array
    seeds: Dict[str, List[Tuple[int, int]]]  # root class -> [(node, root index)]
    rootcls: Dict[int, List[Tuple[str, int]]]  # node -> [(class, root index)]
    dangling: Tuple[int, ...]  # root indexes that resolve to no node
    skipped: int  # edges whose endpoint is no node


class _Api:
    """The API of a package entry (what its consumers use, not this tree): the exported nodes of its file, the public members of its
    exported classes, and what its re-exports hand on, transitively (edges of the link rule REEXPORT_RULE: `export * from m`,
    `export {a} from m`, `export * as ns from m`, a required or imported module value that is passed on; every edge of an alias def,
    kind const): a library with a barrel index and the classes in other files exposes those classes and their public members.
    """

    def __init__(self, graph: Graph, idx: Dict[str, int]):
        self.g, self.idx = graph, idx
        self.by_path: Dict[str, List[int]] = {}
        self.members: Dict[int, List[int]] = {}  # node -> public members of the class it is
        self._hands: Optional[Dict[int, List[int]]] = None
        for i, n in enumerate(graph.nodes):
            self.by_path.setdefault(n.path, []).append(i)

    @property
    def hands(self) -> Dict[int, List[int]]:
        """node -> the nodes its re-export edges reach (built on the first package entry: test roots need only by_path)."""
        if self._hands is None:
            g, idx, self._hands = self.g, self.idx, {}
            marks = {i for i, ev in enumerate(g.evid) if ev.snippet.startswith(REEXPORT_RULE + ":")}
            for (
                ed
            ) in (
                g.edges
            ):  # a const is an alias def (`export default x`, `export {a as b}`): it stands for what it names
                if ed.src in idx and ed.dst in idx and (ed.ev in marks or g.nodes[idx[ed.src]].kind == "const"):
                    self._hands.setdefault(idx[ed.src], []).append(idx[ed.dst])
        return self._hands

    def _public(self, i: int) -> List[int]:
        if i not in self.members:
            n, q = self.g.nodes[i], split_key(self.g.nodes[i].key)[2]
            self.members[i] = [
                j
                for j in self.by_path[n.path]
                if self.g.nodes[j].vis == "public" and split_key(self.g.nodes[j].key)[2].startswith(q + ".")
            ]
        return self.members[i]

    def globals_of(self, entry: Sequence[int]) -> List[int]:
        """The globals of a classic script (`<script src="app.js">`, no exports): every top-level definition of its file, whatever its
        visibility, and the public members of its classes: the page, its inline handlers and the other scripts reach them by name.
        """
        out = []
        for i in entry:
            n = self.g.nodes[i]
            if n.kind != "file" and "." not in split_key(n.key)[2]:
                out += [i, *(self._public(i) if n.kind in ("class", "iface") else ())]
        return sorted(set(out))

    def of(self, entry: Sequence[int]) -> List[int]:
        """The API nodes of the entry file whose nodes are `entry` (its file node is not part of the answer)."""
        out, seen, todo = [], set(), list(entry)
        while todo:
            i = todo.pop()
            if i in seen:
                continue
            seen.add(i)
            n = self.g.nodes[i]
            if n.kind != "file" and n.vis not in (
                "exported",
                "public",
            ):  # public: a `pub fn` of lib.rs, a Java / C# modifier
                continue  # only what the module exports is handed on (the file node of a module stands for the module itself)
            todo += self.hands.get(i, ())
            if n.kind != "file":
                out.append(i)
                if n.kind in ("class", "iface"):
                    out += self._public(i)
        return sorted(set(out))


def _csr(n: int, a: Sequence[int], b: Sequence[int], w: Sequence[int], e: Sequence[int]):
    """Stable sort by `a`: (offsets, other end, weight, edge id) as arrays."""
    order = sorted(range(len(a)), key=a.__getitem__)
    off = array("i", [0]) * (n + 1)
    for x in a:
        off[x + 1] += 1
    for i in range(n):
        off[i + 1] += off[i]
    return (
        off,
        array("i", [b[i] for i in order]),
        array("b", [w[i] for i in order]),
        array("i", [e[i] for i in order]),
    )


def prepare(graph: Graph) -> Prep:
    keys = tuple(n.key for n in graph.nodes)
    idx = {k: i for i, k in enumerate(keys)}
    a, b, w, e, skipped = [], [], [], [], 0
    for j, ed in enumerate(graph.edges):
        s, d = idx.get(ed.src), idx.get(ed.dst)
        if s is None or d is None:
            skipped += 1
            continue
        a.append(s), b.append(d), w.append(TIERS[ed.tier]), e.append(j)
    off, dst, fw, eid = _csr(len(keys), a, b, w, e)
    roff, rsrc, rw, reid = _csr(len(keys), b, a, w, e)
    seeds: Dict[str, List[Tuple[int, int]]] = {c: [] for c in _CLS}
    rootcls: Dict[int, List[Tuple[str, int]]] = {}
    dangling = []
    api: Optional[_Api] = None
    for ri, r in enumerate(graph.roots):
        v = idx.get(r.node) if r.node else None
        if v is None:
            dangling.append(ri)
            continue
        c = "test" if r.kind == "test" else "doc" if r.kind == "doc-cmd" else "soft" if r.strength == "soft" else "hard"
        hit = [v]
        if graph.nodes[v].kind == "file" and (c == "test" or (c == "hard" and r.kind in ("export", "script"))):
            api = api or _Api(graph, idx)
            same = api.by_path.get(graph.nodes[v].path, hit)
            # the test framework collects every test of the file; a package entry (kind export) is its exported API; a classic script (kind
            # script) has no exports, its top-level definitions are globals
            hit = same if c == "test" else [v] + (api.globals_of(same) if r.kind == "script" else api.of(same))
        for u in hit:
            seeds[c].append((u, ri))
            rootcls.setdefault(u, []).append((c, ri))
    for c in seeds:
        seeds[c].sort()
    return Prep(graph, keys, idx, off, dst, fw, eid, roff, rsrc, rw, reid, seeds, rootcls, tuple(dangling), skipped)


def _dial(
    p: Prep, state: array, code: int, dist: array, par: array, pare: array, rt: array, seeds, strong: bool
) -> None:
    """One Dial pass: claims every state-0 node reachable from the seeds [(node, dist, parent, edge, root)]."""
    buckets: Dict[int, List[int]] = {}
    pending = 0

    def offer(v, d, pv, e, r):
        nonlocal pending
        if state[v] == UNREACHED and d < dist[v]:
            dist[v], par[v], pare[v], rt[v] = d, pv, e, r
            buckets.setdefault(d, []).append(v)
            pending += 1

    for s in seeds:
        offer(*s)
    d = 0
    while pending:
        lst = buckets.pop(d, None)
        if lst:
            pending -= len(lst)
            for v in sorted(set(lst)):
                if state[v] != UNREACHED or dist[v] != d:
                    continue
                state[v] = code
                for j in range(p.off[v], p.off[v + 1]):
                    u = p.dst[j]
                    if state[u] == UNREACHED and (not strong or p.w[j] <= _STRONG):
                        offer(u, d + p.w[j], v, p.eid[j], rt[v])
        d += 1


class Step(NamedTuple):
    src: str
    dst: str
    kind: str
    tier: str
    site: str


class WhyAlive(NamedTuple):
    key: str
    verdict: str
    root: Root
    steps: Tuple[Step, ...]

    def render(self) -> str:
        r = self.root
        head = f"{self.verdict}: {r.kind} root {r.node} ({r.trust}, {r.strength}) registered at {r.origin}"
        return " -> ".join([head] + [f"{s.dst} [{s.kind}/{s.tier} at {s.site}]" for s in self.steps])


class WhyDead(NamedTuple):
    key: str
    verdict: str
    roots: str  # what was searched from
    incoming: Tuple[Tuple[str, str, str, str, str], ...]  # (src, kind, tier, site, why it does not count)
    cluster: Tuple[str, ...]

    def render(self) -> str:
        lines = [f"{self.verdict}: {self.roots}"]
        lines += [f"incoming {k}/{t} from {s} at {site}: {why}" for s, k, t, site, why in self.incoming] or [
            "no incoming edge"
        ]
        if self.cluster:
            lines.append(
                f"member of a dead cluster of {len(self.cluster)}: "
                + ", ".join(self.cluster[:8])
                + (" ..." if len(self.cluster) > 8 else "")
            )
        return "\n".join(lines)


class Result:
    def __init__(
        self,
        prep: Prep,
        state: array,
        dist: array,
        par: array,
        pare: array,
        rt: array,
        verdicts: List[str],
        clusters: Tuple[Tuple[str, ...], ...],
    ):
        self.prep, self.state, self.dist, self.par, self.pare, self.rt = prep, state, dist, par, pare, rt
        self._v, self.clusters = verdicts, clusters

    def verdict(self, key: str) -> str:
        return self._v[self.prep.idx[key]]

    def verdicts(self) -> Dict[str, str]:
        return dict(zip(self.prep.keys, self._v))

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for v in self._v:
            out[v] = out.get(v, 0) + 1
        return dict(sorted(out.items()))

    def excluded(self, key: str) -> bool:
        return bool(self.prep.graph.nodes[self.prep.idx[key]].flags & (F_GENERATED | F_TEST))

    def digest(self) -> str:
        blob = json.dumps([self._v, list(self.dist), list(self.par), list(self.pare)], separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def why_alive(self, key: str) -> Optional[WhyAlive]:
        """Root and the par[] chain of any reached node (alive, weak-alive, test-only, doc-only); None for the rest."""
        v = self.prep.idx[key]
        if self.state[v] == UNREACHED:
            return None
        chain = []
        r = self.rt[v]
        while self.par[v] >= 0:
            e: Edge = self.prep.graph.edges[self.pare[v]]
            chain.append(Step(e.src, e.dst, e.kind, e.tier, e.site))
            v = self.par[v]
        return WhyAlive(key, self._v[self.prep.idx[key]], self.prep.graph.roots[r], tuple(reversed(chain)))

    def why_dead(self, key: str) -> WhyDead:
        """Why `key` is not (strongly) alive: the roots searched, every incoming edge and the reason it does not count."""
        p, v = self.prep, self.prep.idx[key]
        n = {c: len(p.seeds[c]) for c in _CLS}
        kinds = {p.graph.roots[ri].kind for c in ("hard", "soft") for _, ri in p.seeds[c]}
        rules = {k for k in kinds if k.startswith("annotation:")}
        kinds = sorted(
            (kinds - rules) | ({f"annotation of {len(rules)} rules"} if rules else set())
        )  # not one name per rule
        roots = (
            f"searched from {n['hard']} hard and {n['soft']} soft production roots ({', '.join(kinds) or 'none'}), "
            f"{n['test']} test seeds and {n['doc']} doc roots; {len(p.dangling)} registrations dangle"
        )
        inc = []
        for j in range(p.roff[v], p.roff[v + 1]):
            e, s = p.graph.edges[p.reid[j]], p.rsrc[j]
            st = self.state[s]
            if s == v:
                why = "self reference: the symbol is its own only caller"
            elif st == UNREACHED:
                why = "the source is itself unreachable from every root"
            elif st in (TEST, DOC):
                why = f"the source is reachable only from {'test' if st == TEST else 'doc'} roots, not production"
            else:
                why = f"tier {e.tier} is not a confirmed use (name match or dynamic site): counts as weak"
            inc.append((e.src, e.kind, e.tier, e.site, why))
        cl = next((c for c in self.clusters if key in c), ())
        return WhyDead(key, self._v[v], roots, tuple(sorted(inc)), cl)


def forward(prep: Prep, mode: str = "closed_world", unindexed: Iterable[str] = ()) -> Result:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}, not {mode!r}")
    n = len(prep.keys)
    state, dist = array("b", [0]) * n, array("i", [INF]) * n
    par, pare, rt = array("i", [-1]) * n, array("i", [-1]) * n, array("i", [-1]) * n
    _dial(prep, state, ALIVE, dist, par, pare, rt, [(v, 0, -1, -1, ri) for v, ri in prep.seeds["hard"]], True)
    seeds = [(v, 0, -1, -1, ri) for v, ri in prep.seeds["soft"]]
    for u in range(n):  # the relaxations out of the alive nodes over every tier
        if state[u] == ALIVE:
            seeds += [
                (prep.dst[j], dist[u] + prep.w[j], u, prep.eid[j], rt[u]) for j in range(prep.off[u], prep.off[u + 1])
            ]
    _dial(prep, state, WEAK, dist, par, pare, rt, seeds, False)
    for code, c in ((TEST, "test"), (DOC, "doc")):
        _dial(prep, state, code, dist, par, pare, rt, [(v, 0, -1, -1, ri) for v, ri in prep.seeds[c]], False)
    verdicts = [_NAME.get(s, "dead") for s in state]
    uf = list(range(n))  # weakly connected components of the dead subgraph

    def find(x):
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    loop, size = set(), {}
    for v in range(n):
        if state[v] == UNREACHED:
            for j in range(prep.off[v], prep.off[v + 1]):
                u = prep.dst[j]
                if state[u] == UNREACHED:
                    uf[find(v)] = find(u)
                    if u == v:
                        loop.add(v)
    for v in range(n):
        if state[v] == UNREACHED:
            size[find(v)] = size.get(find(v), 0) + 1
    groups: Dict[int, List[str]] = {}
    for v in range(n):
        if state[v] == UNREACHED and (size[find(v)] > 1 or v in loop):
            groups.setdefault(find(v), []).append(prep.keys[v])
            verdicts[v] = "dead-cluster"
    nodes, skip = prep.graph.nodes, set(unindexed)
    if mode == "library":
        for v in range(n):
            if state[v] == UNREACHED:
                if nodes[v].pkg in skip:
                    verdicts[v] = "unknown"
                elif nodes[v].kind != "file" and nodes[v].vis in ("public", "exported"):
                    verdicts[v] = "public-unused"
    clusters = tuple(sorted(tuple(sorted(g)) for g in groups.values()))
    return Result(prep, state, dist, par, pare, rt, verdicts, clusters)


def analyze(graph: Graph, mode: str = "closed_world", unindexed: Iterable[str] = ()) -> Result:
    return forward(prepare(graph), mode, unindexed)


# ---------------------------------------------------------------- bounded reverse BFS
class Touched(NamedTuple):
    key: str
    verdict: str  # alive weak-alive test-only doc-only dead dead-cluster public-unused unknown
    steps: int
    path: Tuple[Step, ...]  # root first, to the key (empty for dead and unknown)
    ancestors: int  # nodes that reach the key (all dead when the verdict is dead*)
    why: str


def _rev_bfs(p: Prep, t: int, strong: bool, budget: int):
    """Reverse BFS from t. -> (found {class: node}, parent map, steps used, exhausted?)"""
    seen: Dict[int, Tuple[int, int]] = {t: (-1, -1)}
    q, found, steps = deque([t]), {}, 0
    while q:
        v = q.popleft()
        steps += 1
        for c, _ in p.rootcls.get(v, ()):
            found.setdefault(c, v)
        if "hard" in found or (not strong and "soft" in found):
            return found, seen, steps, False
        if steps > budget:
            return found, seen, steps, True
        for j in range(p.roff[v], p.roff[v + 1]):
            steps += 1
            s = p.rsrc[j]
            if s not in seen and (not strong or p.rw[j] <= _STRONG):
                seen[s] = (v, p.reid[j])
                q.append(s)
    return found, seen, steps, steps > budget


def _path(p: Prep, seen, v: int) -> Tuple[Step, ...]:
    out = []
    while seen[v][0] >= 0:  # seen[x] = (the node one step closer to the target, edge): follows the edge from x on
        e = p.graph.edges[seen[v][1]]
        out.append(Step(e.src, e.dst, e.kind, e.tier, e.site))
        v = seen[v][0]
    return tuple(out)


def touched(prep: Prep, keys: Iterable[str], cap: int = CAP, mode: str = "closed_world") -> Dict[str, Touched]:
    """Verdict of each key from the reverse reachability alone: strong (S/T) edges first, then every edge; `cap` bounds the
    steps (nodes + edges looked at) per key. A key that is no node is unknown. dead-cluster here = dead with dead callers.
    """
    out: Dict[str, Touched] = {}
    for key in sorted(set(keys)):
        t = prep.idx.get(key)
        if t is None:
            out[key] = Touched(key, "unknown", 0, (), 0, "not a node of the graph")
            continue
        found, seen, used, over = _rev_bfs(prep, t, True, cap)
        if "hard" in found:
            out[key] = Touched(key, "alive", used, _path(prep, seen, found["hard"]), len(seen) - 1, "")
            continue
        if not over:
            f2, seen, u2, over = _rev_bfs(prep, t, False, cap - used)
            found, used = f2, used + u2
        if over:
            out[key] = Touched(key, "unknown", used, (), 0, f"more than {cap} steps of reverse search: no verdict")
            continue
        for c, name in (("hard", "weak-alive"), ("soft", "weak-alive"), ("test", "test-only"), ("doc", "doc-only")):
            if c in found:
                out[key] = Touched(key, name, used, _path(prep, seen, found[c]), len(seen) - 1, "")
                break
        else:
            node = prep.graph.nodes[t]
            anc = len(seen) - 1
            v = (
                "public-unused"
                if mode == "library" and node.kind != "file" and node.vis in ("public", "exported")
                else "dead-cluster" if anc else "dead"
            )
            callers = sorted(prep.keys[s] for s in seen if s != t)[:5]
            why = f"no root reaches it; {anc} dead caller(s)" + (f": {', '.join(callers)}" if callers else "")
            out[key] = Touched(key, v, used, (), anc, why)
    return out
