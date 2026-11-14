"""graph.levels: the branch level of the graph checker, the graph build it shares with the whole-repository views, and the seams
where the semantic layer and the engines plug in.

    branch(root, base=None, budget_s=None, sink=None, proof=Proof(), steps=None, layout=None) -> Report
                                                    merge-base(HEAD, upstream) .. work tree; Report(findings, meta)
    build(root, store, ...) -> Built                snapshot -> shards (cached by blob) -> roots -> link; the graph is a function
                                                    of the shard set, so an incremental build equals a cold one by construction
    analyzed_proof(root) / analyzed(root)           the graph of the work tree with its reach.Result (and Proof): what `why` and
                                                    `tree` read
    why(root, name) -> str                          why-alive / why-dead of one node

The branch level (and the repo level, which runs on it) gives every dead-symbol claim of its result a numeric confidence 0..100
(graph.confidence, the last step of `_check`, from the graph and the proof it settled the claims with): a second axis next to the
level and the code, never an input of either.

The newest stored runtime run of the project (graph.runtime, `graph runtime`), when there is one, is read by `_check` (files only: the
target is never contacted): a dead claim about a symbol that ran is replaced by the I finding dc.alive-runtime, the run is the `runtime`
input of graph.confidence, and a run older than refresh_days is still used with one I dc.runtime-stale. No stored run, no change.

Graph findings (all W unless stated; the whole tree is the graph, the diff only picks what is reported):
  E dc.root-dangling                    a registration of a CHANGED file, or onto a changed or deleted file, points into the void
                                        (graph.roots: the findings are repo-wide, only the ones that touch the diff are reported)
  E dc.engine-missing                   a language of a changed file whose extractor exists but cannot be loaded at all
  W dc.degraded-extractor               a language of a changed file whose extractor cannot run here (tree-sitter, its grammar or pwsh is
                                        not installed): its files are not in the graph, the message says what to install;
                                        W dc.graph-incomplete names such a language that is left out of the graph otherwise
  W dc.unsupported-language             a changed file of a language with no extractor yet: one finding per language, never a silent
                                        skip (also in meta["unsupported"] and in the text and md tails)
  W dc.orphaned-by-branch               a symbol that LOST an incoming edge from a changed file on this diff and is now dead
  W dc.dead-cluster                     a changed symbol that is dead and sits in a dead cluster (functions that only call each other)
  W dc.introduced-dead                  a changed symbol that is dead now and was not dead at the base
  I dc.suspect                          what dead-cluster and introduced-dead become when nothing corroborates them (graph.verdict:
                                        an S-tier index of the package or the native engine of the language must agree; the proof is a
                                        verdict.Proof, empty = every claim is a suspect); orphaned-by-branch and the registration
                                        findings are the graph's own verdicts and stay
    introduced = dead(head) & candidates - dead(base); candidates = definitions that overlap an added line and the targets of
    edges that the diff removed. The base side is incremental (`_base`): the changed files are linked again at their previous
    version over the index of the whole tree, their edges are the removed-edge candidates, and dead(base) is a bounded reverse
    search on the head graph with those edges swapped in (`_base_graph`), so a symbol that was already dead is never blamed on
    the diff. The base graph uses the head roots (a registration that the diff removed is not seen here).
  W dc.budget / dc.branch-budget, W dc.store-unavailable   what was not done and why (never silent): a budget stop lists the
    unfinished stages, finished results are kept. The branch level stores the graph right after the link (`_warm`), whatever
    is left of the budget: a run that is stopped later still leaves a warm cache. E dc.bad-facts of the linker is a W here.

The seams (Steps): `semantic` applies the semantic cache (SCIP indexes, engine matches) on top of the built graph: refuted edges
change reach, and the Proof it returns corroborates dead claims; `prove` asks the engines and the language servers about the dead
symbols of the diff that nothing corroborates yet. Both are optional; without them every dead claim of the diff is a suspect. A
step that raises costs its own result and a W, never the run (`soft`).

The checker never breaks a commit (graph.safety): only the policy findings above may be E. A stage that raises (tree, extract,
roots, link, reach, diff) is a W dc.check-failed with the reason, the finished stages keep their results and the later ones are not
run; so is an exception anywhere else in a level (the guard at the entry of branch). The budgets come from [graph.budget]
(defaults commit 20 s, branch 900 s). No hard production root in the whole graph (a scratch repository, a pure library): the dead
verdicts would only mean "no roots", so the diff checks stay off and meta["no_roots"] says so. Every finding carries a why
(report.ensure_why). `# deadcode: ignore` on the definition line or the line above silences a graph finding like it does a text
checker one; a waive of the configuration drops it. Nothing here runs a model or an engine.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import time
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from .. import core
from . import classreg, confidence, contracts, paths, reach, report, runtime, safety, vcs, verdict
from . import langs as _langs
from . import link as _link
from . import roots as _roots
from . import store as _store
from . import config
from .config import BUDGETS
from .extract import batch as _batch
from .model import (
    F_ABSTRACT,
    F_GENERATED,
    F_OVERRIDE,
    F_PROTOCOL,
    Edge,
    Facts,
    Finding,
    Graph,
    Node,
    canonical,
    shard_key,
    sort_findings,
    split_key,
)

BRANCH_S = BUDGETS["branch_s"]  # the default; [graph.budget] overrides
KINDS = ("func", "method", "class")  # what the diff checks report on
SKIP_FLAGS = F_PROTOCOL | F_ABSTRACT | F_OVERRIDE  # excluded from a verdict: a live parent or the protocol owns them
DEAD = ("dead", "dead-cluster")


class Steps(NamedTuple):
    """The layers that plug into the levels; both are optional.
    semantic(root, built, store, notes, deadline, clock) -> (graph', Proof): the cache of the semantic layer applied to the stored
        graph (`notes` collects its W findings, `deadline` is a `clock()` value: the end of the budget of the level, or None)
    prove(root, built, res, deltas, cfg, run, notes, proof) -> (built', res', proof'): the engines and language servers asked about
        the dead symbols of the changed files that nothing corroborates yet (`soft` runs a sub-step under the budget and the guard)
    """

    semantic: Optional[Callable] = None
    prove: Optional[Callable] = None


# ---------------------------------------------------------------- budget and run state
class Budget:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.clock, self.limit, self.t0 = clock, seconds, clock()

    def over(self) -> bool:
        return self.clock() - self.t0 >= self.limit


class Run:
    """Stages one after another; a stage that starts after the budget is gone is skipped and named, never half done."""

    def __init__(self, scope: str, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.scope, self.budget, self.clock = scope, Budget(seconds, clock), clock
        self.done: List[str] = []
        self.skipped: List[str] = []  # not started: the budget was gone
        self.failed: List[Tuple[str, str]] = []  # (stage, reason): the stage raised
        self.halted: List[str] = []  # not started: an earlier stage failed (their input would be partial)
        self.ms: Dict[str, int] = {}
        self.running: Optional[str] = None  # the stage being worked on (a caller that gives up on the run names it)

    def stage(self, name: str, fn: Callable, *args):
        if self.failed:
            self.halted.append(name)
            return None
        if self.skipped or self.budget.over():
            self.skipped.append(name)
            return None
        t = self.clock()
        self.running = name
        try:
            out = fn(*args)
        except Exception as exc:  # noqa: BLE001 - a stage that fails costs its result and a W, never the commit
            self.failed.append((name, safety.reason(exc)))
            return None
        finally:
            self.running = None
        self.ms[name] = int((self.clock() - t) * 1000)
        self.done.append(name)
        return out

    def unfinished(self) -> List[str]:
        return self.skipped + [n for n, _ in self.failed] + self.halted

    def failure(self) -> Optional[Finding]:
        if not self.failed:
            return None
        why = "; ".join(f"{n}: {r}" for n, r in self.failed)
        msg = f"the {self.scope} check failed in stage {why}; not run: {', '.join(self.halted) or 'nothing'}; finished: {', '.join(self.done) or 'nothing'}"
        return Finding(
            "W",
            safety.WHERE,
            0,
            "dc.check-failed",
            msg,
            "a failure of the checker is a notice, never a block: finished results are kept",
        )

    def findings(self) -> List[Finding]:
        return [f for f in (self.finding(), self.failure()) if f]

    def finding(self) -> Optional[Finding]:
        if not self.skipped:
            return None
        code = "dc.branch-budget" if self.scope == "branch" else "dc.budget"
        return Finding(
            "W",
            safety.WHERE,
            0,
            code,
            f"{self.scope} budget of {self.budget.limit:g}s is spent: not done: {', '.join(self.skipped)}; finished: {', '.join(self.done) or 'nothing'} (results kept)",
            "the stages run in order tree, extract, roots, link, (branch: persist), reach, diff; a stage is never started after the budget",
        )


class Delta(NamedTuple):
    path: str
    status: str  # A M D T R C ? (as vcs.Change; "?" untracked)
    added: Optional[frozenset]  # added line numbers in the head, None = every line
    base: Optional[str] = None  # text of the previous version (None: the path is new)
    old: str = ""  # previous path of a rename


# ---------------------------------------------------------------- the build
class Built(NamedTuple):
    tree: vcs.Tree
    facts: Callable  # Lazy: {path: Facts} of every linked file (loaded on first call)
    keys: Dict[str, str]  # path -> shard key of the extracted files
    langs: Dict[str, str]  # path -> language
    graph: Graph
    roots: _roots.RootSet
    findings: Tuple[Finding, ...]  # link and root findings (E dc.root-dangling, dc.stub-entry, dc.bad-facts)
    missing: Dict[str, Finding]  # lang -> the P0 finding of an extractor that cannot run
    unsupported: Tuple[str, ...]  # languages with no extractor yet (P3)
    cold: int  # files extracted by this build
    key: str  # sha256 of everything the graph is a function of (shard keys, other files, roots, linker version)


def _extractor(lang: str, cfg: Optional[dict]):
    """(extractor, finding) of a language: the extractor when it can run now; (None, None) when the language has none yet (it is
    reported as not supported); (None, finding) when there is one that cannot run (the finding says why)."""
    if not contracts.defined(lang):
        return None, None
    try:
        ex = contracts.get_extractor(lang)
    except contracts.ExtractorMissing as exc:
        return None, Finding(
            "E",
            f"(extractor of {lang})",
            0,
            "dc.engine-missing",
            exc.reason,
            "the extractor of this language cannot run",
        )
    if not ex.supports(lang):
        return None, None
    f = ex.probe(lang, cfg)
    if f is None:
        return ex, None
    why = f.why or f"{lang} files are left out of the graph until the grammar is installed"
    return None, Finding(f.level, f.path, f.line, f.code, f.msg, why)


def _detect(root: str, tree: vcs.Tree, lg) -> Tuple[Dict[str, str], Dict[str, bytes]]:
    """{path: lang} of the files that are not plain data (a data language with an extractor, the schema languages, is read), and the heads
    read for the files whose name alone does not decide."""
    paths = sorted(tree.files)
    heads = vcs.read_files(root, tree, [p for p in paths if lg.needs_head(p)])
    out = {}
    for p in paths:
        lang = lg.detect(p, heads.get(p))
        if lang and (lg.langs[lang].get("role") != "data" or lg.extractor(lang)):
            out[p] = lang
    return out, heads


def _text(data: Optional[bytes]) -> Optional[str]:
    return None if data is None or b"\0" in data[:8192] else data.decode("utf-8", "replace")


class Lazy:
    """A value computed on first use (the per-file facts: a graph that is in the cache never needs them)."""

    def __init__(self, fn: Callable):
        self.fn, self.v, self.done = fn, None, False

    def __call__(self):
        if not self.done:
            self.v, self.done = self.fn(), True
        return self.v


class Plan(NamedTuple):
    langs_of: Dict[str, str]
    heads: Dict[str, bytes]
    keys: Dict[str, str]  # path -> shard key of the extractable files
    missing: Dict[str, Finding]
    unsupported: Tuple[str, ...]
    cold: Tuple[str, ...]  # paths without a shard


def _plan(root: str, tree: vcs.Tree, lg, store, cfg: Optional[dict]) -> Plan:
    """Which files are extracted by which extractor, and which of them have no shard yet; no file is read for it."""
    langs_of, heads = _detect(root, tree, lg)
    exs, gsha, missing, unsupported, keys = {}, {}, {}, set(), {}
    for p in sorted(langs_of):
        lang = langs_of[p]
        if lang in missing or lang in unsupported or _link.path_flags(p) & F_GENERATED:
            continue
        if lang not in exs:
            exs[lang], found = _extractor(lang, cfg)
            if found is not None:
                missing[lang] = found
            elif exs[lang] is None:
                unsupported.add(lang)
            gsha[lang] = exs[lang].grammar_sha(lang) if exs[lang] is not None else ""
        if lang not in missing and lang not in unsupported:
            keys[p] = shard_key(tree.files[p].blob, lang, exs[lang].ext_ver, gsha[lang])
    have = store.has_shards(keys.values()) if store is not None else set()
    return Plan(
        langs_of,
        heads,
        keys,
        missing,
        tuple(sorted(unsupported)),
        tuple(p for p in sorted(keys) if keys[p] not in have),
    )


def _extract(root: str, tree: vcs.Tree, pl: Plan, store, workers) -> Dict[str, Facts]:
    """Extract the cold files (in parallel) and keep their shards: {shard key: Facts}."""
    heads = {p: _text(d) for p, d in pl.heads.items()}
    texts = {
        **heads,
        **{p: _text(d) for p, d in vcs.read_files(root, tree, [p for p in pl.cold if p not in heads]).items()},
    }
    items = [(pl.langs_of[p], p, tree.files[p].blob, texts[p]) for p in pl.cold if texts.get(p) is not None]
    shards, _gone = _batch.extract_many(items, workers=workers)
    if store is not None and shards:
        store.put_shards(shards, replace=False)
    return {s.key: s.facts() for s in shards}


def build(
    root: str,
    store: Optional[_store.Store] = None,
    *,
    tree: Optional[vcs.Tree] = None,
    cfg: Optional[dict] = None,
    workers: Optional[int] = None,
    run: Optional[Run] = None,
) -> Built:
    """The graph of the work tree of `root`. With a store the shards are read from and written to it, and a graph whose inputs
    (shard keys, other files, roots, linker version) are those of the stored one is read instead of linked. `cfg` is handed to
    the extractors' probes. `run` makes each step a budgeted stage (None = no budget).
    """
    lg = _langs.load()
    stage = (lambda name, fn, *a: fn(*a)) if run is None else run.stage
    tree = tree or vcs.snapshot(root, workers=workers)
    pl = stage("tree", _plan, root, tree, lg, store, cfg)
    if pl is None:
        return Built(tree, Lazy(dict), {}, {}, canonical([]), _roots.RootSet((), ()), (), {}, (), 0, "")
    got = stage("extract", _extract, root, tree, pl, store, workers) or {}
    rs = stage("roots", _roots.build, root, tree, store, None, workers) or _roots.RootSet((), ())
    own = {p for p in pl.langs_of if not _link.path_flags(p) & F_GENERATED}
    extra = {}  # a registration target that is no analysed file still needs its node
    for r in rs.roots:
        lang, path, qual, *_ = split_key(r.node) if r.node else ("", "", "x")
        if not qual and path in tree.files and path not in own:
            extra[path] = lang
    key = _graph_key(pl, own, extra, rs)
    hit = store is not None and store.get_meta("graph_key") == key

    def load_facts() -> Dict[str, Facts]:
        if store is not None:  # the warm shards, one decode each
            got.update(store.get_facts({k for k in pl.keys.values() if k not in got}))
        facts = {p: got[k] for p, k in pl.keys.items() if k in got}
        for p in sorted(
            own
        ):  # a file node for every file that is no extracted code (exec and include edges point at it)
            facts.setdefault(p, Facts(pl.langs_of[p]))
        return {**facts, **{p: Facts(lang) for p, lang in extra.items() if p not in facts}}

    facts = Lazy(load_facts)

    def link() -> Tuple[Graph, List[Finding]]:
        if hit:  # same shards, same roots, same linker: the stored graph is the answer
            return store.get_graph(), [Finding(*r) for r in json.loads(store.get_meta("graph_findings", "[]"))]
        lk = _link.link(facts(), workers=workers)
        bound = classreg.bind(lk.graph.nodes, rs.roots)  # a class that a registration file names has its node only now
        g = canonical(lk.graph.nodes, lk.graph.edges, tuple(lk.graph.roots) + bound, lk.graph.evid)
        return g, list(lk.findings)

    graph, lf = stage("link", link) or (canonical([]), [])
    fnd = sort_findings(lf + [Finding(f.level, f.path, f.line, f.code, f.msg, f.why) for f in rs.findings])
    return Built(
        tree, facts, dict(pl.keys), pl.langs_of, graph, rs, tuple(fnd), pl.missing, pl.unsupported, len(pl.cold), key
    )


def _graph_key(pl: Plan, own: Set[str], extra: Dict[str, str], rs: _roots.RootSet) -> str:
    h = hashlib.sha256(f"graph\0{_link.LINKER_V}\0".encode())
    for p in sorted(own):
        h.update(f"{p}\0{pl.langs_of[p]}\0{pl.keys.get(p, '')}\n".encode("utf-8", "surrogateescape"))
    h.update(json.dumps([sorted(extra.items()), rs.roots], separators=(",", ":")).encode())
    return h.hexdigest()


def persist(store: _store.Store, b: Built) -> None:
    """Snapshot and graph: what the next run reads instead of building again. Skipped when the stored graph is this one."""
    if store.get_meta("graph_key") == b.key:
        return
    with store.tx():
        store.put_snapshot(b.tree.id, b.keys)
        store.put_graph(b.graph, canonical_input=True)
        store.set_meta("graph_tree", b.tree.id)
        store.set_meta("graph_key", b.key)
        store.set_meta(
            "graph_findings", json.dumps([[f.level, f.path, f.line, f.code, f.msg, f.why] for f in b.findings])
        )
        store.gc([b.tree.id])


def open_store(root: str, layout: Optional[paths.Layout] = None) -> Tuple[Optional[_store.Store], Optional[Finding]]:
    """The store of the repository (its data directory is created), or (None, W dc.store-unavailable): a run without the cache is
    correct, only slower."""
    try:
        lay = layout or paths.resolve(paths.find_root(root))
        paths.ensure(lay)
        return _store.Store(str(lay.store)), None
    except (paths.DataDirError, _store.StoreError, OSError, ValueError) as exc:
        return None, Finding(
            "W",
            safety.WHERE,
            0,
            "dc.store-unavailable",
            f"the graph cache cannot be opened ({exc}); every shard is extracted again and nothing is cached",
            "the cache is a convenience: a run without it is correct, only slower",
        )


def facts_getter(b: Built, store_path: Optional[str] = None) -> Callable:
    """The per-file facts for the pages (graph.lens, the duplicates lens), asked for AFTER the run: b.facts() reads the shards through the
    store of the run, which is closed by then, so a closed store is answered by a fresh one on `store_path` (a read of the shards,
    nothing is extracted or written; without a path: no facts). Called only by a page that wants the lenses; the facts of a file whose
    shard is gone are missing, not guessed."""

    def get() -> Dict[str, Facts]:
        try:
            return b.facts()
        except Exception:  # noqa: BLE001 - sqlite3.ProgrammingError: the store of the run is closed
            if not store_path:
                return {}
            try:
                st = _store.Store(store_path)
            except (_store.StoreError, OSError, ValueError):
                return {}
            try:
                got = st.get_facts(set(b.keys.values()))
            finally:
                st.close()
            return {p: got[k] for p, k in b.keys.items() if k in got}

    return get


# ---------------------------------------------------------------- the diff checks
def _qual(key: str) -> str:
    return split_key(key)[2] or split_key(key)[1]


def _dunder(key: str) -> bool:
    """__getattr__, __init__ ...: called by the interpreter (PEP 562 for a module-level one), never by name."""
    last = _qual(key).rpartition(".")[2]
    return last[:2] == last[-2:] == "__" and len(last) > 4


def _hits(lines: Optional[Sequence[int]], a: int, b: int) -> bool:
    if lines is None:
        return True
    i = bisect.bisect_left(lines, a)
    return i < len(lines) and lines[i] <= b


class Base(NamedTuple):
    """The base side of a diff: the nodes of the tree with the changed files at their previous version, and the edges that those
    files emit (their edges are a function of their own facts and that tree's index, so the rest of the base graph is the head's).
    """

    nodes: Tuple[Node, ...]
    edges: Tuple[Edge, ...]
    changed: frozenset  # paths of the diff, old names of renames included


def _base(b: Built, deltas: Sequence[Delta], workers) -> Optional[Base]:
    """The base side of the diff, incremental: only the changed files are linked again (index and import bindings over the whole tree,
    edges of the changed files only). None: nothing existed before (only added files): no removed edges, nothing dead at the base.
    """
    olds = [d for d in deltas if d.base is not None]
    if not olds:
        return None
    facts = dict(b.facts())
    for d in deltas:
        if d.status in ("A", "?", "C", "R"):
            facts.pop(d.path, None)
    items = []
    for d in olds:
        path = d.old or d.path
        lang = b.langs.get(d.path) or _langs.detect(path)
        if lang:
            items.append((lang, path, vcs.blob_sha(d.base.encode("utf-8", "surrogateescape")), d.base))
    shards, _ = _batch.extract_many(items, workers=workers)
    by_key = {s.key: s for s in shards}
    for lang, path, blob, _t in items:
        ex = _safe_ext(lang)
        sh = by_key.get(shard_key(blob, lang, ex.ext_ver, ex.grammar_sha(lang))) if ex else None
        facts[path] = sh.facts() if sh else Facts(lang)
    changed = frozenset({d.path for d in deltas} | {d.old for d in deltas if d.old})
    part = _link.link_paths(facts, changed, workers=workers)
    return Base(part.nodes, part.edges, changed)


def _base_graph(b: Built, base: Base) -> Graph:
    """The base graph for the reverse searches: the edges of the unchanged files as the head has them (the head roots too, as
    before), the edges of the changed files at their previous version. An edge to or from a symbol that did not exist is dropped.
    """
    keys = {n.key for n in base.nodes}
    keep = [
        e for e in b.graph.edges if e.site.rpartition(":")[0] not in base.changed and e.src in keys and e.dst in keys
    ]
    return Graph(base.nodes, tuple(keep) + base.edges, b.graph.roots, b.graph.evid)


def _safe_ext(lang: str):
    try:
        return contracts.get_extractor(lang)
    except contracts.ExtractorMissing:
        return None


def reportable(n: Node, res: reach.Result) -> bool:
    """A node the diff checks may report as dead: a func, method or class that is not protocol, abstract, an override,
    generated or a test, and not a dunder. The verdict is not asked here."""
    return n.kind in KINDS and not n.flags & SKIP_FLAGS and not res.excluded(n.key) and not _dunder(n.key)


def has_production_root(graph: Graph) -> bool:
    """Without a production entry point "dead" would only mean "no roots": the diff checks stay off."""
    return any(r.strength == "hard" and r.kind not in ("test", "doc-cmd") for r in graph.roots)


def suspect_keys(
    graph: Graph, res: reach.Result, proof: verdict.Proof, spare: Iterable[Tuple[str, int]] = ()
) -> Set[str]:
    """The dead symbols that nothing corroborates (graph.verdict): what the viewer draws as suspects, whole graph, not only the diff.
    `spare` = (path, line) of the symbols whose dead verdict is the graph's own finding (orphaned-by-branch): they stay "dead".
    """
    if not has_production_root(graph):
        return set()
    return {
        n.key
        for n in graph.nodes
        if res.verdict(n.key) in DEAD
        and reportable(n, res)
        and (n.path, n.line0) not in spare
        and not proof.text(n.key, n.path)
    }


def diff_findings(
    b: Built,
    res: reach.Result,
    deltas: Sequence[Delta],
    base: Optional[Base],
    run: Optional[Run] = None,
    proof: verdict.Proof = verdict.Proof(),
) -> List[Finding]:
    nodes = {n.key: n for n in b.graph.nodes}
    added = {d.path: (None if d.added is None else sorted(d.added)) for d in deltas}
    cand: Dict[str, List[Tuple[str, str]]] = {}  # key -> [(src, site)] edges the diff removed ([] = only affected)
    for n in b.graph.nodes:
        if n.kind in KINDS and n.path in added and _hits(added[n.path], n.line0, n.line1):
            cand[n.key] = []
    if base is not None:
        bpath = {n.key: n.path for n in base.nodes}
        kept = {(e.src, e.dst, e.kind) for e in b.graph.edges if e.site.rpartition(":")[0] in base.changed}
        for e in base.edges:
            if (
                bpath.get(e.src) in base.changed
                and (e.src, e.dst, e.kind) not in kept
                and e.dst in nodes
                and nodes[e.dst].kind in KINDS
            ):
                cand.setdefault(e.dst, []).append((e.src, e.site))
    dead = [k for k in sorted(cand) if res.verdict(k) in DEAD and reportable(nodes[k], res)]
    was = {}
    if base is not None and dead:
        if (
            run is not None and run.budget.over()
        ):  # without the base check every old dead symbol would be blamed on the diff
            run.skipped.append("diff: was-it-dead-before check")
            return []
        was = reach.touched(reach.prepare(_base_graph(b, base)), dead)
    out = []
    for k in dead:
        if k in was and was[k].verdict in DEAD:
            continue  # dead before the diff too: not its doing
        n, v = nodes[k], res.verdict(k)
        why = res.why_dead(k)
        q = _qual(k)
        if cand[k]:
            src = ", ".join(sorted({f"{_qual(s)} at {site}" for s, site in cand[k]})[:3])
            code, msg = (
                "dc.orphaned-by-branch",
                f"`{q}` lost its last caller on this branch (was used by {src}) and no root reaches it now",
            )
            extra = (
                f"\nbefore the diff: {len(cand[k])} edge(s) from changed files reached it; after: none from a live node"
            )
        elif v == "dead-cluster":
            code, msg = (
                "dc.dead-cluster",
                f"`{q}` is part of a dead cluster of {len(why.cluster)} symbols that only use each other; no root reaches any of them",
            )
            extra = ""
        else:
            code, msg = (
                "dc.introduced-dead",
                f"`{q}` is dead: no path from any root reaches it, and it was not dead before this diff",
            )
            extra = ""
        out.append(
            verdict.settle(
                Finding("W", n.path, n.line0, code, msg, why.render() + extra), proof.text(k, n.path), verdict.claims(n.lang)
            )
        )
    return out


def change_marks(
    b: Built, res: reach.Result, deltas: Sequence[Delta], base: Optional[Base], found: Sequence[Finding]
) -> Dict[str, int]:
    """Node key -> bits for the viewer: 1 touched (a definition that overlaps an added line, or the file node of a changed file),
    2 newly dead (the node of an orphaned-by-branch, introduced-dead, dead-cluster or suspect finding), 4 newly alive (dead at the base,
    not dead now: a bounded reverse search on the base graph, like diff_findings)."""
    added = {d.path: (None if d.added is None else sorted(d.added)) for d in deltas}
    marks: Dict[str, int] = {}
    fpaths = {f.path for f in found}
    at: Dict[Tuple[str, int], str] = {}
    for n in b.graph.nodes:
        if n.path in added and (n.kind == "file" or _hits(added[n.path], n.line0, n.line1)):
            marks[n.key] = 1
        if n.path in fpaths:
            at[(n.path, n.line0)] = n.key
    for f in found:
        k = at.get((f.path, f.line))
        if k and f.code in ("dc.orphaned-by-branch", "dc.introduced-dead", "dc.dead-cluster", verdict.SUSPECT):
            marks[k] = marks.get(k, 0) | 2
    if base is not None:
        live = [k for k, v in marks.items() if v & 1 and res.verdict(k) not in DEAD]
        was = reach.touched(reach.prepare(_base_graph(b, base)), live) if live else {}
        for k, t in was.items():
            if t.verdict in DEAD:
                marks[k] |= 4
    return marks


def _roots_touching(found: Iterable[Finding], paths: Set[str]) -> List[Finding]:
    """Root findings that concern the diff: the file itself, the registering file, or the target the registration names.
    dc.bad-facts (an extractor wrote an invalid fact; the fact is skipped) is no policy case: it is reported as W.
    """
    return [
        f if f.code != "dc.bad-facts" else Finding("W", f.path, f.line, f.code, f.msg, f.why)
        for f in found
        if f.code in ("dc.root-dangling", "dc.bad-facts", "dc.bad-root-source")
        and any(
            p == f.path or f"registered at {p}:" in f.why or f"`{p}`" in f.msg or f"registered at {p} " in f.why
            for p in paths
        )
    ]


def _missing_findings(b: Built, paths: Set[str]) -> List[Finding]:
    out = []
    for lang, f in sorted(b.missing.items()):
        mine = sorted(p for p in paths if b.langs.get(p) == lang)
        if mine:
            out.append(Finding(f.level, mine[0], 1, f.code, f.msg, f.why))
        else:
            out.append(
                Finding(
                    "W", safety.WHERE, 0, "dc.graph-incomplete", f"{lang} files are not in the graph: {f.msg}", f.why
                )
            )
    return out


def _unsupported_findings(b: Built, paths: Set[str]) -> List[Finding]:
    """One W per language without an extractor that has a changed file: those files are not in the graph (no silent skip)."""
    out = []
    for lang in b.unsupported:
        mine = sorted(p for p in paths if b.langs.get(p) == lang)
        if mine:
            more = f" (+{len(mine) - 1} more)" if len(mine) > 1 else ""
            msg = f"{mine[0]}{more}: no {lang} extractor yet, the file is not in the graph"
            out.append(Finding("W", mine[0], 1, "dc.unsupported-language", msg))
    return out


def _waived(cfg, f: Finding) -> bool:
    """A waive of the configuration covers a finding by its code, and a dc.suspect by the claim it stands for as well."""
    return cfg.is_waived(f.code, f.path) or cfg.is_waived(verdict.claim(f), f.path)


def _ignored(root: str, tree: vcs.Tree, found: List[Finding]) -> List[Finding]:
    """Drop the graph findings that the markers of the text checker silence: `# deadcode: ignore` on the definition line or the
    line above, `deadcode: ignore-file` in the first 10 lines of the file."""
    want = sorted({f.path for f in found if f.code.startswith("dc.") and f.line and f.path in tree.files})
    texts = {p: _text(d) or "" for p, d in vcs.read_files(root, tree, want).items()}
    return [
        f
        for f in found
        if f.path not in texts
        or not f.line
        or not (core.has_ignore_file(texts[f.path]) or core.line_ignored(texts[f.path].split("\n"), f.line))
    ]


# ---------------------------------------------------------------- steps, the checks and the levels
def soft(run: Run, name: str, fn: Callable, out: List[Finding]):
    """A step whose failure costs its own result and a W; the stages after it still run. Not started once the budget is gone."""
    if run.skipped or run.budget.over():
        run.skipped.append(name)
        return None
    t = run.clock()
    run.running = name
    try:
        got = fn()
    except Exception as exc:  # noqa: BLE001 - a failure of ours is a notice
        out.append(Finding(*safety.check_failed(name, exc)))
        return None
    finally:
        run.running = None
    run.ms[name] = int((run.clock() - t) * 1000)
    run.done.append(name)
    return got


def left(run: Run) -> float:
    """Seconds of the budget of the run that are left."""
    return run.budget.limit - (run.clock() - run.budget.t0)


def _check(
    root: str,
    deltas: Sequence[Delta],
    run: Run,
    cfg,
    store: Optional[_store.Store],
    out: List[Finding],
    sink: Optional[dict],
    proof: verdict.Proof,
    steps: Steps,
    layout: Optional[paths.Layout] = None,
) -> Tuple[List[Finding], dict]:
    paths = {d.path for d in deltas} | {d.old for d in deltas if d.old}
    b = build(root, store, cfg=cfg.as_dict(), run=run)
    if store is not None and "link" in run.done:  # warm the cache before anything that may run out of time
        out += _warm(store, b, run)
    out += _roots_touching(b.findings, paths)  # the stages that finished keep their results
    out += _missing_findings(b, paths) + _unsupported_findings(b, paths)
    if steps.semantic is not None:  # the cache of the semantic layer: the refined graph and what corroborates
        end = (
            run.budget.t0 + run.budget.limit
        )  # on the clock of the run: what is left of the budget is what the cache may use
        got = soft(run, "semantic", lambda: steps.semantic(root, b, store, out, end, run.clock), out)
        if got is not None:
            b, proof = b._replace(graph=got[0]), verdict.both(proof, got[1])
    res = run.stage("reach", reach.analyze, b.graph, cfg.mode)
    no_roots = not has_production_root(b.graph)
    if res is not None and not no_roots and steps.prove is not None:
        b, res, proof = steps.prove(root, b, res, deltas, cfg, run, out, proof)
    rt = _runtime_view(root, b, cfg, layout, out)  # the newest stored runtime run of the project, if there is one (graph.runtime)
    found: List[Finding] = []
    if res is not None and not no_roots:  # without a production entry point "dead" would only mean "no roots"

        def diff():
            base = _base(b, deltas, None)
            got = runtime.replace_claims(diff_findings(b, res, deltas, base, run, proof), b.graph, res, rt.alive)
            if sink is not None:
                sink["marks"] = change_marks(b, res, deltas, base, got)
            return got

        found = run.stage("diff", diff) or []
    if sink is not None and res is not None:  # what the viewer draws: the graph, its verdicts, what the diff touched
        gone = {
            (f.path, f.line) for f in found if f.code == "dc.orphaned-by-branch"
        }  # the graph's own verdict: stays "dead"
        sink.update(
            graph=b.graph,
            res=res,
            tree=b.tree,
            facts=facts_getter(
                b, getattr(store, "path", None)
            ),  # the per-file facts, loaded only when a page asks for them (graph.lens: the duplicates lens)
            marks=sink.get("marks", {}),
            suspects=suspect_keys(b.graph, res, proof, gone),
        )
    shown = _ignored(root, b.tree, found)
    pool = out + shown
    out = [f for f in pool if not _waived(cfg, f)]
    if sink is not None:  # what the ledger records: findings that a marker or a waive silenced
        left_, kept = {id(f) for f in shown}, {id(f) for f in out}
        sink["suppressed"] = [("ignore", f) for f in found if id(f) not in left_] + [
            ("waive", f) for f in pool if id(f) not in kept
        ]
    if res is not None:  # the second axis (graph.confidence): never touches a verdict, never raises
        # runtime: the map node key -> ran of the newest stored run (graph.runtime); empty when the project has none
        confidence.annotate(out, b.graph, proof, confidence.gap_langs(b), rt.ran or None)
    meta = {
        "files": len(b.tree.files),
        "nodes": len(b.graph.nodes),
        "edges": len(b.graph.edges),
        "roots": len(b.graph.roots),
        "extracted": b.cold,
        "unsupported": list(b.unsupported),
        "missing": sorted(b.missing),
    }
    if res is not None:
        meta["verdicts"] = res.counts()
    if no_roots:
        meta["no_roots"] = True  # the diff checks were not run: see the module docstring
    if rt.run:
        meta["runtime"] = rt.notes[0]
    return out, meta


def _runtime_view(root: str, b: Built, cfg, layout, out: List[Finding]) -> runtime.View:
    """What the newest stored runtime run says about the graph (graph.runtime.view): files only, the target is never contacted. A stale run is one
    I finding; a failure of ours is a W and the run is not used."""
    try:
        v = runtime.view(root, b, cfg.runtime, layout=layout)
    except Exception as exc:  # noqa: BLE001 - a convenience: a failure costs the stored run, never the check
        out.append(Finding(*safety.check_failed("runtime", exc)))
        return runtime.View()
    out += v.findings
    return v


def _warm(store, b: Built, run: Run) -> List[Finding]:
    """Store the graph the moment it is linked, whatever is left of the budget: the next run reads it instead of linking again.
    A budget that stopped the run before this step is what kept the cache cold forever; the write is the price of the warm-up.
    """
    t = run.clock()
    try:
        persist(store, b)
    except Exception as exc:  # noqa: BLE001 - the cache is a convenience
        return [
            Finding(
                "W",
                safety.WHERE,
                0,
                "dc.store-unavailable",
                f"the graph could not be stored ({type(exc).__name__}: {exc}); the next run links again",
                "the cache is a convenience: a run without it is correct, only slower",
            )
        ]
    run.ms["persist"] = int((run.clock() - t) * 1000)
    run.done.append("persist")
    return []


# ---------------------------------------------------------------- the level
class Report(NamedTuple):
    findings: Tuple[Finding, ...]
    meta: dict


def branch_deltas(root: str, base: str) -> List[Delta]:
    changes = vcs.changed(root, base)
    hunks = vcs.added_lines(root, base)
    olds = vcs.base_blobs(root, base, [c.old or c.path for c in changes if c.status not in ("A", "?", "C")])
    blobs = vcs.cat_batch(root, olds.values())
    out = []
    for c in changes:
        old = c.old or c.path
        text = None if c.status in ("A", "?", "C") or old not in olds else _text(blobs.get(olds[old]))
        added = None if c.status in ("?", "A") else frozenset(hunks.get(c.path, ()))
        out.append(Delta(c.path, c.status, added, text, c.old))
    return out


def branch(
    root: str,
    base: Optional[str] = None,
    budget_s: Optional[float] = None,
    sink: Optional[dict] = None,
    proof: verdict.Proof = verdict.Proof(),
    steps: Optional[Steps] = None,
    layout: Optional[paths.Layout] = None,
) -> Report:
    """The branch check of the repository at `root` against `base` (default: merge-base with the upstream or the main branch).
    `sink`, when given, receives graph, res, tree, marks and suspects (what the viewer draws) of the run when its graph stages
    finished, and `run`, the Run of this call from its start: a caller that runs the level in a thread and gives up at its own
    deadline reads which stage was running and which were done. `budget_s` None = [graph.budget] branch_s (default 900 s).
    `proof` is what corroborates a dead claim (graph.verdict): without it, and without `steps`, every dead claim of the diff is a
    suspect. `layout` = where the data of the run lives (default: graph.paths.resolve)."""
    run = Run("branch", BRANCH_S if budget_s is None else budget_s)
    if sink is not None:
        sink["run"] = run
    found: List[Finding] = []
    deltas: List[Delta] = []
    meta: dict = {}
    ref = None
    try:  # the guard at the entry of the branch level: a failure of ours is a W, never a block
        cfg = config.load(root)
        if budget_s is None:
            run.budget = Budget(cfg.budget("branch"), run.clock)
        ref = vcs.default_base(root, base)
        if ref is None:
            found.append(
                Finding(
                    "W",
                    safety.WHERE,
                    0,
                    "dc.no-base",
                    "no base to compare with (no upstream, origin/HEAD, main or master, or --base is not a commit): the whole tree is the diff, so every finding is pre-existing and none blocks",
                    "a branch check needs merge-base(HEAD, upstream); without one every file counts as added and every E of the tree is shown as W",
                )
            )
            deltas = [Delta(p, "A", None) for p in sorted(vcs.snapshot(root).files)]
        else:
            deltas = branch_deltas(root, ref[0])
        f2, meta = _branch(root, deltas, run, cfg, sink, proof, steps or Steps(), layout)
        found += f2 if ref is not None else safety.pre_existing(f2)
    except Exception as exc:  # noqa: BLE001
        found.append(Finding(*safety.check_failed("branch", exc)))
    found += run.findings()
    meta.update(
        {
            "scope": "branch",
            "base": ref[1] if ref else None,
            "changed": len(deltas),
            "stages": run.done,
            "unfinished": run.unfinished(),
            "timing_ms": run.ms,
        }
    )
    return Report(tuple(report.ensure_all(sort_findings(found))), meta)


def _branch(root, deltas, run, cfg, sink, proof, steps, layout) -> Tuple[List[Finding], dict]:
    notes = [Finding(p.level, p.where, 0, p.code, p.msg) for p in cfg.problems]
    if not cfg.enabled:
        return notes, {"disabled": True}
    store, note = open_store(root, layout)
    try:
        return _check(root, deltas, run, cfg, store, notes + ([note] if note else []), sink, proof, steps, layout)
    finally:
        if store is not None:
            store.close()


def analyzed_proof(
    root: str,
    notes: Optional[list] = None,
    steps: Optional[Steps] = None,
    layout: Optional[paths.Layout] = None,
) -> Tuple[Built, reach.Result, verdict.Proof]:
    """The graph of the work tree (stored shards reused, then persisted; the semantic cache applied on top when `steps` has one: the
    stored graph is the unrefined one) with its reach.Result and the Proof: what `why` and `tree` read."""
    store, _ = open_store(root, layout)
    b = build(root, store)
    g, proof = b.graph, verdict.Proof()
    if store is not None:
        persist(store, b)
        if steps is not None and steps.semantic is not None:
            g, proof = steps.semantic(root, b, store, notes if notes is not None else [], None, time.monotonic)
        store.close()
    return b._replace(graph=g), reach.analyze(g, config.load(root).mode), proof


def analyzed(root: str, steps: Optional[Steps] = None, layout: Optional[paths.Layout] = None):
    """analyzed_proof without the Proof: what `why` reads."""
    return analyzed_proof(root, None, steps, layout)[:2]


def match_keys(keys: Sequence[str], name: str) -> List[str]:
    """The node keys `name` stands for: a key, else a `path#qual` / `qual` / path suffix, else (at most 8) keys that contain it."""
    hit = [k for k in keys if k == name] or [k for k in keys if k.endswith("#" + name) or k.endswith(":" + name)]
    return hit or [k for k in keys if name in k][:8]


def why(root: str, name: str, steps: Optional[Steps] = None, layout: Optional[paths.Layout] = None) -> str:
    """why-alive or why-dead of a node: `name` is a node key, a `path#qual` / `qual` suffix, or a path."""
    b, res = analyzed(root, steps, layout)
    hit = match_keys([n.key for n in b.graph.nodes], name)
    if len(hit) != 1:
        return f"no node matches {name!r}" if not hit else f"{name!r} is ambiguous, say one of:\n  " + "\n  ".join(hit)
    k = hit[0]
    wa = res.why_alive(k)
    return f"{k}\n" + (wa.render() if wa is not None else res.why_dead(k).render())
