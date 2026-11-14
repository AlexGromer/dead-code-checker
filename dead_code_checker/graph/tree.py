"""graph.tree: the call tree of one symbol, `dead-code-checker graph tree NODE`.

    dead-code-checker graph tree NODE [--root DIR] [--callers | --callees] [--depth N] [--width N] [--json | --svg [--out FILE]]
    build(prep, verdicts, v, up, depth, width) -> dict     the tree of node v as nested dicts
    render(doc, up) -> [line]                               its indented text
    svg(graph, res, vd, docs, focus) -> (svg, Finding|None)    the trees as one picture: viz.diagram + viz.render_svg
    run(a, root, steps=None) -> exit code                   the command, given the parsed arguments and the repository root

NODE is what `graph why` takes: a node key, a `path#qual` / `qual` / path suffix. One match is drawn; none or several: the
candidates are listed and the exit code is 2. The graph is the one `why` reads (levels.analyzed_proof: stored shards reused), the
verdicts are the ones of the viewer (a dead claim that nothing corroborates is a "suspect").

A level lists the edges around a node (callers: edges into it, callees: edges out of it) worst verdict first (dead-cluster, dead,
suspect, weak-alive, ... alive), then by name, key and edge index, so the same graph prints the same tree. The first `width` edges
of a level are shown ("+N more" says how many are cut). Rows are made level by level and each row is marked:
  cycle  the symbol is one of its own ancestors in this tree              seen  the symbol is already shown elsewhere in this tree
  limit  the symbol has more edges but `depth` levels are drawn
and a marked row is not expanded. A row shows the edge that leads to it: kind/tier and the site (file:line) of the reference.
--svg draws the nodes of the shown trees (and the edges between them) as one SVG: the symbol in the middle is outlined, the fill is
the verdict, a node links to index.html#k=KEY of a run directory. Graphviz dot lays it out when it is installed, else the built-in
layered emitter does, with a W on standard error that says how to install dot; more than SVG_NODES nodes is a usage error (lower
--depth / --width). The SVG goes to --out FILE or to standard output.
Exit 0 on success, 2 for a usage error (no repository, no or several matches, a bad limit).
"""

from __future__ import annotations

import json
import sys
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import levels, reach
from .model import split_key
from .verdict import SEVERITY

SVG_NODES = 600  # nodes of a tree picture
DEPTH, WIDTH, DEPTH_MAX = 6, 25, 50
_MARKS = {"cycle": "(cycle)", "seen": "(shown above)", "limit": "(depth limit)"}


def edges_of(prep: reach.Prep, v: int, up: bool) -> List[Tuple[int, int]]:
    """(edge index, node at the other end) of every edge into (up) or out of (down) node v."""
    if up:
        return [(prep.reid[j], prep.rsrc[j]) for j in range(prep.roff[v], prep.roff[v + 1])]
    return [(prep.eid[j], prep.dst[j]) for j in range(prep.off[v], prep.off[v + 1])]


def build(prep: reach.Prep, vd: Dict[str, str], v: int, up: bool, depth: int = DEPTH, width: int = WIDTH) -> dict:
    """The tree of node v as nested dicts {key, name, verdict, path, line, edge, mark, n, more, kids}; the root has edge None."""
    g = prep.graph
    seen: Set[int] = set()

    def name(i: int) -> str:
        return split_key(prep.keys[i])[2] or g.nodes[i].path.rpartition("/")[2]

    def order(es: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
        def rank(x: Tuple[int, int]):
            k = SEVERITY.index(vd[prep.keys[x[1]]]) if vd[prep.keys[x[1]]] in SEVERITY else len(SEVERITY)
            return k, name(x[1]), prep.keys[x[1]], x[0]

        return sorted(es, key=rank)

    def row(i: int, e: int, anc: Tuple[int, ...]):
        es = edges_of(prep, i, up)
        mark = "cycle" if i in anc else "seen" if i in seen else "limit" if es and len(anc) >= depth else ""
        seen.add(i)
        n, ed = g.nodes[i], g.edges[e] if e >= 0 else None
        doc = {
            "key": prep.keys[i],
            "name": name(i),
            "verdict": vd[prep.keys[i]],
            "path": n.path,
            "line": n.line0,
            "edge": {"kind": ed.kind, "tier": ed.tier, "site": ed.site} if ed else None,
            "mark": mark,
            "n": len(es),
            "more": 0,
            "kids": [],
        }
        return doc, i, es

    def expand(doc: dict, i: int, es: Sequence[Tuple[int, int]], anc: Tuple[int, ...]) -> None:
        es = order(es)
        doc["more"] = max(0, len(es) - width)
        kids = [row(u, e, anc + (i,)) for e, u in es[:width]]
        doc["kids"] = [k[0] for k in kids]
        for kd, u, kes in kids:
            if kes and not kd["mark"]:
                expand(kd, u, kes, anc + (i,))

    root, i, es = row(v, -1, ())
    expand(root, i, es, ())
    return root


def render(doc: dict, up: bool) -> List[str]:
    """The indented text of one tree: `name [verdict] path:line  via kind/tier at site  (mark)`, then `+N more` where a level is cut."""
    head = "callers (who calls it, up)" if up else "callees (what it calls, down)"
    out = [f"{head}: {doc['n']}" if doc["n"] else f"{head}: none"]

    def walk(d: dict, ind: int) -> None:
        for k in d["kids"]:
            e = k["edge"]
            tail = f"  via {e['kind']}/{e['tier']} at {e['site']}" + (f"  {_MARKS[k['mark']]}" if k["mark"] else "")
            out.append(f"{'  ' * ind}{k['name']} [{k['verdict']}] {k['path']}:{k['line']}{tail}")
            walk(k, ind + 1)
        if d["more"]:
            out.append(f"{'  ' * ind}+{d['more']} more")

    walk(doc, 1)
    return out


def keys_of(doc: dict) -> Set[str]:
    out = {doc["key"]}
    for k in doc["kids"]:
        out |= keys_of(k)
    return out


def svg(graph, res, vd: Dict[str, str], docs: Dict[str, dict], focus: str):
    """(svg, finding) of the nodes in `docs` (the trees of build) and the edges between them; `focus` is outlined."""
    from . import viz

    keys = set().union(*(keys_of(d) for d in docs.values()))
    idxs = sorted(res.prep.idx[k] for k in keys)
    d = viz.diagram(
        graph,
        res,
        idxs,
        {focus: viz.MARK_TOUCHED},
        f"tree of {split_key(focus)[2] or focus}",
        "index.html",
        max_nodes=len(idxs) + 1,
        suspects=[k for k in keys if vd[k] == "suspect"],
    )
    return viz.render_svg(d)


def run(a, root: str, steps: Optional[levels.Steps] = None, layout=None) -> int:
    """`graph tree`: print the callers and/or callees tree of one node (text, --json or --svg). `a` has node, callers, callees,
    depth, width, json, svg and out; `root` is the repository."""
    if getattr(a, "out", None) and not getattr(a, "svg", False):
        print("dead-code-checker graph tree: --out goes with --svg", file=sys.stderr)
        return 2
    if not (1 <= a.depth <= DEPTH_MAX and a.width >= 1):
        print(f"dead-code-checker graph tree: --depth is 1..{DEPTH_MAX} and --width at least 1", file=sys.stderr)
        return 2
    notes: list = []
    b, res, proof = levels.analyzed_proof(root, notes, steps, layout)
    for f in notes:
        print(f.render(), file=sys.stderr)
    hit = levels.match_keys([n.key for n in b.graph.nodes], a.node)
    if len(hit) != 1:
        print(
            (
                f"dead-code-checker graph tree: no node matches {a.node!r}"
                if not hit
                else f"dead-code-checker graph tree: {a.node!r} is ambiguous, say one of:\n  " + "\n  ".join(hit)
            ),
            file=sys.stderr,
        )
        return 2
    vd = {**res.verdicts(), **dict.fromkeys(levels.suspect_keys(b.graph, res, proof), "suspect")}
    prep, v = res.prep, res.prep.idx[hit[0]]
    dirs = (
        [("callers", True)]
        if a.callers
        else [("callees", False)] if a.callees else [("callers", True), ("callees", False)]
    )
    docs = {name: build(prep, vd, v, up, a.depth, a.width) for name, up in dirs}
    if getattr(a, "svg", False):
        n = len(set().union(*(keys_of(d) for d in docs.values())))
        if n > SVG_NODES:
            print(
                f"dead-code-checker graph tree: {n} nodes are too many for an SVG (at most {SVG_NODES}): lower --depth or --width",
                file=sys.stderr,
            )
            return 2
        pic, w = svg(b.graph, res, vd, docs, hit[0])
        if w is not None:
            print(w.render(), file=sys.stderr)
        if not a.out:
            sys.stdout.write(pic)
            return 0
        try:
            with open(a.out, "w", encoding="utf-8") as fh:
                fh.write(pic)
        except OSError as exc:
            print(f"dead-code-checker graph tree: cannot write {a.out}: {exc}", file=sys.stderr)
            return 2
        print(f"dead-code-checker graph tree: {n} nodes written to {a.out}")
        return 0
    if a.json:
        print(json.dumps({"node": hit[0], "verdict": vd[hit[0]], **docs}, indent=1, sort_keys=True))
        return 0
    print(f"{hit[0]} [{vd[hit[0]]}]")
    for name, up in dirs:
        print("\n".join(render(docs[name], up)))
    return 0
