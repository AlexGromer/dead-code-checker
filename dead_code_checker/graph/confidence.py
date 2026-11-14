"""graph.confidence: a numeric confidence (0..100) for every dead-symbol claim, the second axis next to the verdict. Stdlib only.

The verdict stays categorical and is decided elsewhere (graph.reach, the confirmation rule of graph.verdict): this module only READS
what a run holds and writes two fields of a finding, `confidence` (an int) and `conf_note` (the sum in one line). It never changes a
level, a code, a message, a why, the order a check sorts by, the exit code or what is hidden by default; a failure of the module
leaves the findings as they were (annotate never raises). The formula and its weights are DATA, data/confidence.json (every factor is
explained there); nothing here is tuned in code.

    score = clamp(base + sources + engine_conf + hazards + coverage + runtime, 0, 100)

    base         the claim: a claim of the graph 40, a dead cluster 30; a dc.suspect has the base of the code it replaced
    sources      the independent corroborations (S-tier overlay, compiler, other engine, language server), strongest first, each scaled
                 by "diminish" (the second adds half, the third a quarter), capped: more or stronger sources never lower it
    engine_conf  the percent an engine gave to its own hit on the symbol (vulture; Proof.confs), also when it did not corroborate
    hazards      decorated, dynamic-zone, override, protocol flags of the node, and the distinct D-tier (getattr / reflection / eval /
                 string dispatch) call sites of its module: the things the syntactic graph cannot follow
    coverage     a language of the same family (own_gap) or another one (other_gap) with no working extractor in this tree, no hard
                 production root of the language of the symbol: part of the callers is not in the graph
    runtime      `runtime` = {node key or path: True (ran) | False (the file was covered, the symbol never ran)}: ran = a heavy penalty;
                 no runtime result, no change

    compute(spec, claim, sources, engine_conf, flags, dyn_sites, own_gap, other_gap, no_lang_root, runtime) -> (score, note)
                 the pure formula; every argument is a fact, the result is a function of them alone (the tests of monotony run on it)
    annotate(fs, graph, proof, gaps, runtime) -> int
                 sets `confidence` and `conf_note` of every finding of data "codes" (a claim of a symbol) in `fs`, in place, and returns how
                 many it set; a finding that already has one keeps it (annotating twice is harmless). `gaps` = the languages with no working
                 extractor (gap_langs(built)), `runtime` = a map from a run with coverage, or None (the default: no runtime result)
    gap_langs(built) -> set          the languages of a levels.Built that have no extractor here (missing or unsupported)
    below(f, floor) -> bool          True when `f` carries a confidence under `floor` (the filter of --min-confidence; a finding without one never is)
    sources_of(proof, key, path) -> list    the tiers of what corroborates a node, read from a graph.verdict.Proof

Facts only, no source text: the note names tiers, tools and numbers, so it leaks nothing (the rule of graph.report).
"""

from __future__ import annotations

import json
import os
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import langs as _langs
from . import verdict
from .model import F_DECORATED, F_DYNAMIC, F_OVERRIDE, F_PROTOCOL, Finding, Graph, split_key

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "confidence.json")
SYMBOL_KINDS = ("func", "method", "class")
_TABLE: Dict[str, dict] = {}


def table() -> dict:
    if DATA not in _TABLE:
        with open(DATA, encoding="utf-8") as fh:
            _TABLE[DATA] = json.load(fh)
    return _TABLE[DATA]


def _clamp(x: int) -> int:
    return max(0, min(100, x))


# ---------------------------------------------------------------- the formula
def compute(
    spec: Mapping,
    claim: str,
    sources: Sequence[str] = (),
    engine_conf: Optional[int] = None,
    flags: int = 0,
    dyn_sites: int = 0,
    own_gap: bool = False,
    other_gap: bool = False,
    no_lang_root: bool = False,
    runtime: Optional[bool] = None,
) -> Tuple[int, str]:
    """(score, note) from facts. `sources` = tiers of data "sources" (s-tier, compiler, engine, lsp, native), one entry per independent
    corroboration; `engine_conf` = percent or None; `flags` = Node.flags; `dyn_sites` = distinct D-tier call sites of the module;
    `runtime` = True (the symbol ran), False (the file was covered, the symbol did not), None (no runtime result)."""
    base = spec["base"].get(claim, spec["base"]["default"])
    parts: List[Tuple[str, int]] = [("base", base)]
    ws = sorted((spec["sources"].get(s, 0) for s in sources), reverse=True)
    dim = spec["diminish"]
    got = min(spec["sources_max"], sum(w * dim[i] for i, w in enumerate(ws[: len(dim)])) // 100)
    if got:
        parts.append(("sources " + "+".join(sorted(sources, key=lambda s: (-spec["sources"].get(s, 0), s))), got))
    if engine_conf:
        parts.append(
            (f"engine {max(0, min(100, engine_conf))}%", spec["engine_conf"] * max(0, min(100, engine_conf)) // 100)
        )
    hz = spec["hazards"]
    for name, bit in (
        ("decorated", F_DECORATED),
        ("dynamic", F_DYNAMIC),
        ("override", F_OVERRIDE),
        ("protocol", F_PROTOCOL),
    ):
        if flags & bit:
            parts.append((name, hz[name]))
    if dyn_sites:
        parts.append((f"{dyn_sites} dynamic site(s)", max(hz["dyn_site_max"], hz["dyn_site_each"] * dyn_sites)))
    cv = spec["coverage"]
    for name, on in (("own gap", own_gap), ("other gap", other_gap), ("no lang root", no_lang_root)):
        if on:
            parts.append((name, cv[name.replace(" ", "_")]))
    if runtime is not None:
        parts.append(
            (
                "runtime ran" if runtime else "runtime not run",
                spec["runtime"]["executed" if runtime else "not_executed"],
            )
        )
    total = sum(v for _, v in parts)
    note = ", ".join(f"{n} {v:+d}" if n != "base" else f"base {v}" for n, v in parts if v or n == "base")
    return _clamp(total), f"{note} = {_clamp(total)}"


# ---------------------------------------------------------------- the facts of a run
def _tier(text: str) -> str:
    """The tier of a corroboration text of graph.verdict: s-tier, lsp, compiler or engine."""
    if text.startswith("S-tier"):
        return "s-tier"
    if "language server" in text:
        return "lsp"
    return "compiler" if verdict.proof_tool(text) in table()["compilers"] else "engine"


def sources_of(proof, key: str, path: str) -> List[str]:
    """The tiers of what corroborates a node (one entry each): the SCIP overlay of its package, the engine or server hit on the node.
    Read from the tables of a graph.verdict.Proof."""
    out: List[str] = []
    if path in proof.covered:
        out.append("s-tier")
    if key in proof.nodes:
        out.append(_tier(proof.nodes[key]))
    if not out:  # a proof that answers text() by its own rule (a subclass): what it says counts as one source
        text = proof.text(key, path)
        if text:
            out.append(_tier(text))
    return out


def gap_langs(built) -> set:
    """The languages of a levels.Built that have no working extractor here: a grammar missing, or no extractor yet."""
    return set(getattr(built, "missing", None) or ()) | set(getattr(built, "unsupported", None) or ())


def _family(lang: str) -> str:
    try:
        lg = _langs.load()
        return (lg.family(lang) if lang in lg.langs else None) or lang
    except Exception:  # noqa: BLE001 - the family is a refinement: without it a language is its own family
        return lang


def _index(graph: Graph, paths: set):
    """(nodes by (path, line0), nodes by path, distinct D-tier sites by path, languages with a hard production root)."""
    at: Dict[Tuple[str, int], object] = {}
    by_path: Dict[str, list] = {}
    for n in graph.nodes:
        if n.path in paths and n.kind in SYMBOL_KINDS:
            at.setdefault((n.path, n.line0), n)
            by_path.setdefault(n.path, []).append(n)
    sites: Dict[str, set] = {}
    for e in graph.edges:
        if e.tier == "D":
            p = e.site.rpartition(":")[0]
            if p in paths:
                sites.setdefault(p, set()).add(e.site)
    rooted = set()
    for r in graph.roots:
        if r.node and r.strength == "hard" and r.kind not in ("test", "doc-cmd"):
            rooted.add(split_key(r.node)[0])
    return at, by_path, {p: len(s) for p, s in sites.items()}, rooted


def _node_of(f: Finding, at, by_path):
    n = at.get((f.path, f.line))
    if n is not None:
        return n
    inside = [m for m in by_path.get(f.path, ()) if m.line0 <= f.line <= max(m.line0, m.line1)]
    return min(inside, key=lambda m: (max(m.line0, m.line1) - m.line0, m.key)) if inside else None


def annotate(
    fs: Iterable[Finding],
    graph: Graph,
    proof=None,
    gaps: Iterable[str] = (),
    runtime: Optional[Mapping[str, bool]] = None,
) -> int:
    """Set `confidence` and `conf_note` of the dead-symbol findings of `fs` (data "codes"), in place; returns how many were set. A claim
    whose symbol is not in `graph` (no symbol at that line) is scored on the facts that do not need the node (the claim itself, what
    corroborates its file); nothing here raises. `runtime` is the seam of a runtime run: node key (or path) -> ran; None = no result."""
    try:
        return _annotate(list(fs), graph, proof, set(gaps), runtime)
    except Exception:  # noqa: BLE001 - a second axis must never break a check: the findings stay as they were
        return 0


def _annotate(fs: List[Finding], graph: Graph, proof, gaps: set, runtime) -> int:
    spec = table()
    todo = [f for f in fs if f.code in spec["codes"] and f.confidence is None and f.path]
    if not todo:
        return 0
    proof = proof if proof is not None else verdict.Proof()
    at, by_path, sites, rooted = _index(graph, {f.path for f in todo})
    gap_fam = {_family(g) for g in gaps}
    done = 0
    for f in todo:
        n = _node_of(f, at, by_path)
        claim = verdict.claim(f)
        key = n.key if n is not None else ""
        lang = n.lang if n is not None else ""
        srcs = sources_of(proof, key, f.path)
        run = None
        if runtime:
            run = runtime.get(key, runtime.get(f.path)) if key else runtime.get(f.path)
        own = bool(lang) and _family(lang) in gap_fam
        score, note = compute(
            spec,
            claim,
            srcs,
            proof.confs.get(key) if key else None,
            n.flags if n is not None else 0,
            sites.get(f.path, 0),
            own,
            bool(gaps) and not own,
            bool(lang) and lang not in rooted,
            run,
        )
        f.confidence, f.conf_note = score, note
        done += 1
    return done


def below(f: Finding, floor: int) -> bool:
    """True when `f` has a confidence and it is under `floor` (a finding that makes no dead-symbol claim has none and is never below)."""
    return f.confidence is not None and f.confidence < floor
