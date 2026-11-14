"""graph.steps: the engines and the language servers as the two seams of the levels (graph.levels.Steps).

    default(opts) -> levels.Steps       Steps(semantic=semantic, prove=prove(opts)): hand it to levels.branch / analyzed_proof

    semantic(root, built, store, notes, deadline, clock) -> (graph', Proof)
        the semantic cache of the store applied on top of the built graph (graph.semuse): refuted edges leave the graph, the packages
        a SCIP index covers and the exact engine matches corroborate. Fills nothing: `dead-code-checker graph index` does.
    prove(root, built, res, deltas, cfg, run, notes, proof) -> (built', res', proof')      the branch level only
        the dead symbols of the changed files that nothing corroborates yet are asked of the engines of the role `branch` (the
        ones data/audit.json can run: vulture, go-deadcode, knip, cargo, ...; they run over the tree, in parallel, in the sandbox
        when they build) and, still unproven, of the language servers (graph.sem.arbitrate: a scratch copy of the tree, a handful of
        queries). An engine match that names the symbol exactly and that data/verdict.json accepts corroborates it; a reference
        a server finds is an S edge (reach runs again over it); a server that finds none, when it announced its indexing and
        finished it, corroborates the dead claim unless the symbol is one a framework may call by name (verdict.lsp_blind).
        A step that cannot run is named (W) and the claim stays a suspect. Never an E.

Options (`Opts`): the engines directories and the lock are the process-wide setup of graph.engines (the command line sets them);
`lsp` False switches the language servers off (one I line says so; $DEAD_CODE_CHECKER_LSP=off does the same), `native` False
the engines, `go_cache` and `base` say where the offline Go module cache is (graph.gocache).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import time
from typing import Callable, List, NamedTuple, Optional, Sequence

from . import adapters, engines as dce, jobs, par, paths, reach, safety, sem, semcache, semuse, verdict
from . import langs as _langs
from .model import Finding, Node

LSP_OFF = "DEAD_CODE_CHECKER_LSP"  # =off switches the language servers off (one I line says so)
COPY_CHUNK = 256  # files per unit of the scratch copy


class Opts(NamedTuple):
    lsp: bool = True
    native: bool = True
    base: Optional[str] = None  # the data root (graph.paths.Layout.base)
    layout: Optional[paths.Layout] = None  # where the store of the run is (default: graph.paths.resolve)
    go_cache: Optional[str] = None
    workers: Optional[int] = None
    run: Optional[Callable] = None  # the function that starts an engine (tests); default jobs.default_run


def semantic(root, built, store, notes, deadline, clock=time.monotonic):
    """levels.Steps.semantic: the graph refined by the cache of the store and what corroborates (see the module docstring)."""
    return semuse.proof(root, built.graph, built.tree, store, notes=notes, deadline=deadline, clock=clock)


def _family_of(lg, lang: str) -> str:
    return (lg.family(lang) if lang in lg.langs else None) or lang


def _lsp_skipped(msg: str) -> Finding:
    why = "a language server is asked only when it is installed (graph.engines) and, with a lock, pinned in it; the claims stay as they were"
    return Finding("I", safety.WHERE, 0, "dc.sem-lsp-skipped", msg, why)


def _copy_files(unit) -> int:
    src, dst, paths, end = unit
    for p in paths:
        if time.monotonic() >= end:
            raise TimeoutError("the scratch copy of the tree for the language servers ran out of time")
        to = os.path.join(dst, *p.split("/"))
        os.makedirs(os.path.dirname(to), exist_ok=True)
        with contextlib.suppress(FileNotFoundError):  # gone since the snapshot
            shutil.copy(os.path.join(src, *p.split("/")), to)
    return len(paths)


@contextlib.contextmanager
def private_copy(root: str, tree, seconds: float):
    """The directory of a scratch copy of the files of `tree` (a language server writes caches and build output into its root, so it
    never gets your tree); deleted when the block ends. Raises OSError or TimeoutError when the copy fails or takes more than
    `seconds`. The copy holds what the tree holds (tracked and new files, no symlinks); what git ignores (node_modules,
    virtualenvs) is not in it."""
    end = time.monotonic() + seconds
    with tempfile.TemporaryDirectory(prefix="dcc-lsp-tree-") as dst:
        paths = sorted(tree.files)
        units = [(root, dst, paths[i : i + COPY_CHUNK], end) for i in range(0, len(paths), COPY_CHUNK)]
        par.run_units(
            units, _copy_files, kind="thread", key=lambda u: u[2][0]
        )  # a unit is never empty: range steps over `paths`
        yield dst


def _lsp(root: str, b, cands: Sequence[Node], budget_left: float, out: List[Finding], opts: Opts):
    """Ask the language servers about the dead candidates -> (graph', {key: proof text}) or None. The budget is the one of
    data/sem.json, cut to what is left of the run; a family without a server, or a host that switched them off, is ONE I line, never
    a skip in silence. The servers run on a scratch copy of the tree, not in the work tree."""
    if not opts.lsp or os.environ.get(LSP_OFF, "") == "off":
        out.append(_lsp_skipped(f"the language servers are switched off ({LSP_OFF}=off)"))
        return None
    lg = _langs.load()
    specs = sem.servers({_family_of(lg, n.lang) for n in cands})
    ready = {f: s for f, s in specs.items() if isinstance(s, sem.Spec)}
    gone = sorted(f"{f} ({s})" for f, s in specs.items() if f not in ready)
    if gone:
        out.append(_lsp_skipped(f"no language server answered for the dead candidates of: {'; '.join(gone)}"))
    blind = verdict.lsp_blind(
        b.graph, cands
    )  # a hook that a base class or a framework calls by name: silence proves nothing
    keys = [
        n.key
        for n in sorted(cands, key=lambda n: (n.path, n.line0))
        if _family_of(lg, n.lang) in ready and n.key not in blind
    ]
    if not keys:
        return None
    text_of = semuse.reader(root, b.tree)
    cap = float(sem.config()["lsp"]["budget_s"])
    with private_copy(root, b.tree, min(cap, max(1.0, budget_left / 2))) as copy:
        arb = sem.arbitrate(
            b.graph, keys, copy, text_of, ready, budget_s=min(cap, max(1.0, budget_left)), workers=opts.workers
        )
    out += arb.findings
    by_key = {n.key: n for n in cands}
    texts = {
        k: f"{ready[_family_of(lg, by_key[k].lang)].engine} (language server) found no reference to it, in a copy of the files of the tree"
        for k, st in arb.status.items()
        if st == "unreferenced" and k not in blind
    }
    return arb.graph, texts


def _native(root: str, b, res, cands: Sequence[Node], cfg, budget_left: float, out: List[Finding], opts: Opts):
    """The engines of the role `branch` for the languages of the candidates -> a verdict.Proof of their exact matches (or None)."""
    reg, lg = dce.load_registry(), _langs.load()
    fams = tuple(sorted({_family_of(lg, n.lang) for n in cands} & set(reg["languages"])))
    if not fams:
        return None
    from . import levels

    st = jobs.Settings(
        timeout_s=max(1.0, budget_left), budget_s=max(1.0, budget_left), roles=("branch",), langs=fams,
        workers=opts.workers, base=opts.base, go_cache=opts.go_cache, reuse=("eng",),
    )  # fmt: skip
    store, _ = levels.open_store(
        root, opts.layout
    )  # a store that cannot be opened was said by the level: no cache, correct anyway
    try:
        eng = jobs.gather(root, b, st, cfg, store, opts.run or jobs.default_run)
        out += eng.findings
        rec = adapters.reconcile(b.graph, eng.runs, res)
        if (
            store is not None
        ):  # what the tools said is kept: the next run of the same files reads it (graph.semuse) instead of asking again
            semcache.keep_matches(store, semcache.blobs(b.tree), eng, rec.matches)
    finally:
        if store is not None:
            store.close()
    out += [
        f for f in rec.findings if f.code != "dc.engine-disagree"
    ]  # a disagreement is evidence for `why`, not news on a branch
    return verdict.build(matches=rec.matches)


def prove(opts: Opts = Opts()):
    """levels.Steps.prove (see the module docstring)."""
    from . import levels

    def step(root, b, res, deltas, cfg, run, out, proof):
        paths = {d.path for d in deltas}

        def unproven() -> List[Node]:
            return [
                n
                for n in b.graph.nodes
                if n.path in paths
                and res.verdict(n.key) in levels.DEAD
                and levels.reportable(n, res)
                and not proof.text(n.key, n.path)
            ]

        def cluster_rest() -> List[Node]:
            """The symbols of the dead clusters of `unproven()` that nothing corroborates yet, in any file: the verdict of a cluster is
            the verdict of all of its members (verdict.whole_clusters), so the servers are asked about all of them."""
            have = {n.key for n in unproven()}
            keys = {k for c in res.clusters if have & set(c) for k in c} - have
            by = {n.key: n for n in b.graph.nodes}
            return [
                by[k] for k in sorted(keys) if k in by and by[k].kind in levels.KINDS and not proof.text(k, by[k].path)
            ]

        cands = unproven()
        if cands and opts.native:
            got = levels.soft(
                run, "native", lambda: _native(root, b, res, cands, cfg, levels.left(run) / 2, out, opts), out
            )
            if got is not None:
                proof = verdict.both(proof, got)
                cands = unproven()
        got = (
            levels.soft(run, "lsp", lambda: _lsp(root, b, cands + cluster_rest(), levels.left(run), out, opts), out)
            if cands
            else None
        )
        if got and got[0] is not b.graph:  # a reference the servers found is an S edge: reach again over it
            res2 = levels.soft(run, "lsp-reach", lambda: reach.analyze(got[0], cfg.mode), out)
            if res2 is not None:
                b, res = b._replace(graph=got[0]), res2
        if got and got[1]:
            # a dead cluster is corroborated only when every symbol of it is (by the servers or by what proved it before)
            kinds = {n.key: n.kind for n in b.graph.nodes}
            by = {n.key: n for n in b.graph.nodes}
            ok = set(got[1]) | {k for k, n in by.items() if proof.text(k, n.path)}
            whole = verdict.whole_clusters(res.clusters, kinds, ok)
            in_cluster = {k for c in res.clusters for k in c}
            texts = {k: t for k, t in got[1].items() if k not in in_cluster or k in whole}
            proof = verdict.both(proof, verdict.Proof(nodes=texts))
        return b, res, proof

    return step


def default(opts: Opts = Opts()):
    """The Steps of the levels with the semantic cache and the engines (see the module docstring)."""
    from . import levels

    return levels.Steps(semantic=semantic, prove=prove(opts))
