"""graph.semindex: `dead-code-checker graph index`, the full run that fills the semantic cache (graph.semcache). Never a gate: the
caller exits 0 whatever happens here (2 only for a usage error).

    index_repo(root, st, layout=None, run=None, clock=time.monotonic, now=None, mode=None) -> Result

Every indexer (SCIP, in the sandbox when bubblewrap is installed) and every engine of the audit and sem roles that applies to the
languages of the repository runs over the whole tree, and what it produced is stored under the hash of the files of its package:
the index as sem:<engine>:<root>:<hash>, the matches of a tool as eng:<tool>:<hash>. The run is incremental: a job whose item is
already in the cache with the same hash and engine version is not run (its row says `cached`; the counts say skipped, refreshed,
failed). What the cache holds is applied by the levels (graph.steps.semantic): a branch check afterwards finds the dead claims
corroborated without running any engine.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from . import adapters, config, jobs, levels, paths, reach, safety, sandbox, semcache
from .model import Finding


class Result(NamedTuple):
    status: str  # ok | disabled | no-store
    findings: Tuple[Finding, ...]
    counts: Dict[str, int]  # skipped, refreshed, failed (jobs) and indexes, tools (items the cache holds afterwards)
    rows: Tuple[dict, ...]


def _count(rows, store) -> Dict[str, int]:
    c = Counter(r["status"] for r in rows)
    return {
        "skipped": c["cached"],
        "refreshed": c["ok"],
        "failed": c["failed"] + c["skipped"],
        "indexes": len(store.engine_keys(semcache.SEM)),
        "tools": len(store.engine_keys(semcache.ENG)),
    }


def index_repo(
    root: str,
    st: jobs.Settings,
    layout: Optional[paths.Layout] = None,
    run: Callable = jobs.default_run,
    clock: Callable[[], float] = time.monotonic,
    now: Optional[float] = None,
    mode: Optional[sandbox.Mode] = None,
) -> Result:
    cfg = config.load(root)
    if not cfg.enabled:
        return Result("disabled", (), {}, ())
    lay = layout or paths.resolve(paths.find_root(root))
    store, note = levels.open_store(root, lay)
    if store is None:
        why = "the graph cache cannot be opened: the semantic cache lives in it, so there is nothing to fill"
        return Result("no-store", (Finding("W", safety.WHERE, 0, "dc.store-unavailable", why),), {}, ())
    try:
        b = levels.build(root, store, cfg=cfg.as_dict(), workers=st.workers)
        levels.persist(store, b)
        res = reach.analyze(b.graph, cfg.mode)
        base = st.base or str(lay.base)
        eng = jobs.gather(
            root,
            b,
            st._replace(reuse=tuple(dict.fromkeys(st.reuse + ("index", "eng"))), base=base),
            cfg,
            store,
            run,
            clock,
            now,
            mode,
        )
        rec = adapters.reconcile(b.graph, eng.runs, res, ledger=str(lay.ledger))
        semcache.keep_matches(store, semcache.blobs(b.tree), eng, rec.matches, now)
        fs: List[Finding] = [note] if note else []
        fs += list(eng.findings) + list(rec.findings)
        return Result("ok", tuple(fs), _count(eng.rows, store), tuple(eng.rows))
    finally:
        store.close()


def render(root: str, r: Result) -> str:
    """The text of the command: one line per W, then the summary."""
    lines = [f.render() for f in r.findings]
    if r.status != "ok":
        return "\n".join(lines + [f"dead-code-checker graph index: {root}: not run ({r.status})"])
    c = r.counts
    lines.append(
        f"dead-code-checker graph index: {root}: skipped {c['skipped']}, refreshed {c['refreshed']}, failed {c['failed']}; "
        f"the cache holds {c['indexes']} index(es) and {c['tools']} tool result(s)"
    )
    return "\n".join(lines)
