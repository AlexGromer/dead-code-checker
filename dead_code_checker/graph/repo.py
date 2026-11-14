"""graph.repo: the repo level of `graph check --scope repo`: the whole tree is checked as if every file had just been added.

    check(root, budget_s=None, sink=None, proof=Proof(), steps=None, layout=None) -> levels.Report

It is the branch level (graph.levels) with no base: every file of the tree counts as added, so the dead symbols of the whole tree
are reported with the codes of the branch level (a symbol that is dead now: dc.introduced-dead, a dead cluster: dc.dead-cluster,
and so on; a claim that nothing corroborates is a dc.suspect, level I). Nothing was "introduced" by anyone here: every finding of
the tree is pre-existing, so an E about the tree is shown as W (safety.pre_existing) and nothing blocks; the policy findings about
this machine (dc.engine-missing) stay. The budget is [graph.budget] branch_s; a stage that does not finish is named (dc.budget),
a failure of ours is a W dc.check-failed, and `sink` receives what the viewer draws, exactly as at the branch level. Every dead-symbol claim
of the tree carries its numeric confidence (graph.confidence), put there by the branch level this one runs on.
"""

from __future__ import annotations

from typing import List, Optional

from . import config, levels, paths, report, safety, vcs, verdict
from .model import Finding, sort_findings


def check(
    root: str,
    budget_s: Optional[float] = None,
    sink: Optional[dict] = None,
    proof: verdict.Proof = verdict.Proof(),
    steps: Optional[levels.Steps] = None,
    layout: Optional[paths.Layout] = None,
) -> levels.Report:
    run = levels.Run("repo", levels.BRANCH_S if budget_s is None else budget_s)
    if sink is not None:
        sink["run"] = run
    found: List[Finding] = []
    deltas: List[levels.Delta] = []
    meta: dict = {}
    try:  # a failure of ours is a W, never a block
        cfg = config.load(root)
        if budget_s is None:
            run.budget = levels.Budget(cfg.budget("branch"), run.clock)
        deltas = [levels.Delta(p, "A", None) for p in sorted(vcs.snapshot(root).files)]
        f2, meta = levels._branch(root, deltas, run, cfg, sink, proof, steps or levels.Steps(), layout)
        found += safety.pre_existing(f2)
    except Exception as exc:  # noqa: BLE001
        found.append(Finding(*safety.check_failed("repo", exc)))
    found += run.findings()
    meta.update(
        {
            "scope": "repo",
            "stages": run.done,
            "unfinished": run.unfinished(),
            "timing_ms": run.ms,
        }
    )
    return levels.Report(tuple(report.ensure_all(sort_findings(found))), meta)
