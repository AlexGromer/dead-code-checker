"""graph.viz: visualization of the reachability graph. Stdlib only, deterministic (no clock in a page).

    build_pack(graph, res, keep=None, marks=None, findings=(), meta=None) -> Pack   columnar data pack: strings[], files[] and
                                         parallel Int32 columns (nodes n_*, edges e_*, roots r_*, evidence v_*, omitted-node
                                         cells c_*); verdict, par[] / pare[] / rt[] come from the reach.Result as they are, so
                                         the viewer's why-alive is the par[] chain and nothing is recomputed in the browser. Also
                                         the graph view's columns (graph.layout, cached by graph hash when a cache directory is given): cells a_* (directory, file or
                                         omitted-nodes cell: parent, kind, rectangle, name, node count, dead + suspect count, worst
                                         verdict), node positions n_x / n_y / n_agg (the centre of the treemap cell, fixed point,
                                         head.world = [width, height, Q]), file-pair edges g_src / g_dst / g_w / g_tier; and the code
                                         snippets s_node / s_str (graph.snippets: scanned for secrets, at most 40 lines per symbol)
                                         (the findings of the head carry "confidence" / "confidence_note" of a dead-symbol claim,
                                         graph.confidence; the findings list of the viewer shows it as a badge and sorts by it on request)
    to_bytes(pack) / from_bytes(blob)    "DCV1", u32 header length, JSON header (tables, palette, strings, files, meta,
                                         findings, column names and lengths), then the columns as little-endian int32
    to_json_gz(pack)                     graph.json.gz: the same data as JSON lists (the viewer opens it through a file input)
    make_page(graph, res, ..., snippets=Snips) -> Page   ONE self-contained html page: graph/viewer/{shell.html,viewer.css,viewer.js} with the
                                         pack as base64(deflate) in a script tag (plain=True: base64 of the raw pack). No
                                         network, no CDN, no fetch; names and code reach the DOM through textContent only. A page
                                         with code says it is local (<meta name="dc-local">, meta.local: "local page: contains source
                                         code, do not publish"); graph.json.gz carries the same code
    make_page(..., lens=lens.Lens)       the lenses of the viewer (graph.lens: duplicates, architecture, health next to the unused colouring): the chunks of
                                         the lens ride in the page as ONE inline blob (<script id="dclens">, whole chunks up to data/lens.json "inline_bytes",
                                         lazily inflated when a lens is first shown); meta.lens = the lens info (state on | off, the reason, per-lens
                                         numbers, the chunks that did not fit in `left`); the rest is in the run's lens/ folder, which the page's folder
                                         button picks once. A page that had to be cut to its smallest form (no evidence) has the unused lens only.
                                         Publication pages have none
    change_set / diagram / modules_diagram -> Diagram    the nodes of the branch subgraph (touched symbols, callers,
                                         paths to roots, callees, dead neighbours) and the module overview
    module_grid(diagram, widths) -> (boxes, box width, width, height, frames)    the boxes of the module overview: emit_svg draws them,
                                         graph.publish's .drawio places its cells on the same ones
    render_svg(diagram, timeout) -> (svg, Finding|None)    Graphviz `dot -Tsvg` when it is installed (without network when bwrap is
                                         there), else the built-in layered emitter (emit_svg) and a W with the install hint; a dot
                                         that fails or times out is a W too, never silent
    write_run(out_dir, scope, graph, res, findings, meta, marks=..., lens=Lens) -> Artifacts   index.html, modules.svg, graph.json.gz and
                                         (branch) change.html + change.svg, report.md (first line: the file:// link), findings.json and
                                         sarif.json (graph.sarif); with `lens`, lens/lens-<chunk>.json.gz + lens/lens-manifest.json.gz (a failed
                                         write is W dc.viewer-lens-off, never the reason the run is lost)
    write_report(out_dir, findings, meta, main, note)    report.md + findings.json alone (write_run's tail; a run whose graph
                                         stages did not finish has no page and says so on the first line)
    summary_doc(pairs, meta, langs)      the few numbers of a run that the overview page (graph.pages) shows: E / W / suspect counts,
                                         graph size, languages; write_run returns it as Artifacts.summary

Size: a page above PAGE_MAX (24 MB) or a graph above FULL_NODES / FULL_EDGES is cut to the nodes that are not plainly alive
(dead, dead-cluster, weak-alive, test-only, doc-only, unknown, public-unused), the roots, the marked nodes and their k-hop
neighbours (k = 2, then 1, then 0, then 0 without evidence and why texts), every kept node's par[] chain (so why-alive stays
whole); the omitted nodes remain as per-module counts ("cells"), meta.truncated says so, graph.json.gz holds everything. A
page that is over the limit even then is not written (W dc.viz-too-large). A cut page says so as W dc.viz-truncated, except in the
a caller that runs the check on every step (write_run quiet=True): there it is level I, still in report.md.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import html
import json
import math
import os
import re
import struct
import subprocess
import sys
import zlib
from array import array
from collections import Counter
from pathlib import Path
from typing import AbstractSet, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from .. import safepath
from . import layout as dclayout
from . import lens as dclens
from . import report, safety, sarif
from .model import EDGE_KINDS, NODE_KINDS, TIERS, TRUSTS, VERDICTS, Finding, Graph, split_key
from .snippets import LOCAL, Snips

VIEWER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer")
PAGE_MAX = 24 * 1024 * 1024
FULL_NODES, FULL_EDGES = 60_000, 250_000
WHY_MAX = 2_000  # why-dead texts kept per page (the rest: `dead-code-checker graph why NODE`)
FINDINGS_MAX = 3_000
CHANGE_CAP = 2_000  # nodes of the change subgraph page
SVG_MAX = 300  # nodes of an SVG; more collapses to modules
MAGIC = b"DCV1"
MARK_TOUCHED, MARK_NEWDEAD, MARK_NEWALIVE = 1, 2, 4
# verdict -> (fill, ink); the same table is the page's palette (colour is never the only channel: the page and the SVG also
# print the verdict word, and the page has glyphs, outlines and hatching)
SUSPECTS = "suspect"  # a pack verdict, not a reach one: a dead symbol that nothing corroborates (graph.verdict), drawn in its own colour
PACK_VERDICTS = VERDICTS + (SUSPECTS,)
PALETTE = {
    "dead": ("#d32f2f", "#ffffff"),
    "dead-cluster": ("#9a1c1c", "#ffffff"),
    "weak-alive": ("#ef8a2b", "#1a1a1a"),
    "test-only": ("#f2c230", "#1a1a1a"),
    "doc-only": ("#8fa3b0", "#1a1a1a"),
    "public-unused": ("#9b7fd0", "#1a1a1a"),
    "unknown": ("#bdbdbd", "#1a1a1a"),
    "alive": ("#7fb685", "#1a1a1a"),
    SUSPECTS: ("#d48bb8", "#1a1a1a"),
}
# module colour of the overview: the share of dead + dead-cluster + suspect nodes of a module -> (upper bound, fill, ink, caption)
RAMP = (
    (0.0, "#7fb685", "#1a1a1a", "0"),
    (0.10, "#e9de5b", "#1a1a1a", "up to 10 %"),
    (0.25, "#f4b73f", "#1a1a1a", "up to 25 %"),
    (0.50, "#ee7a2a", "#1a1a1a", "up to 50 %"),
    (0.75, "#cf3a2f", "#ffffff", "up to 75 %"),
    (1.0, "#8e1b1b", "#ffffff", "above 75 %"),
)
MOD_GAP_Y = 32
BOX_H, GAP_X, GAP_Y = (
    38,
    18,
    54,
)  # the built-in emitter's box height and gaps (the grid estimate of dot uses the same box)
_WORST = ("dead-cluster", "dead", SUSPECTS, "weak-alive", "unknown", "public-unused", "test-only", "doc-only", "alive")
_EK = {k: i for i, k in enumerate(EDGE_KINDS)}
_TI = {t: i for i, t in enumerate(TIERS)}
_META_KEYS = (
    "scope", "base", "head", "changed", "files", "nodes", "edges", "roots", "verdicts", "unfinished", "unsupported",
    "missing", "no_roots", "links", "mode", "hops", "local", "code", "lens",
)  # fmt: skip
assert array("i").itemsize == 4


# ---------------------------------------------------------------- the data pack
class Pack(NamedTuple):
    head: dict
    cols: Dict[str, array]


class _Tab:
    """Interned strings in first-use order (the same input gives the same table)."""

    def __init__(self):
        self.items: List[str] = []
        self.ix: Dict[str, int] = {}

    def __call__(self, s: str) -> int:
        i = self.ix.get(s)
        if i is None:
            i = self.ix[s] = len(self.items)
            self.items.append(s)
        return i


def _close(res, keep: Iterable[int]) -> Set[int]:
    """`keep` plus the par[] chain of every kept node: a kept node's why-alive path is never cut."""
    out = set(keep)
    for v in list(out):
        while True:
            u = res.par[v]
            if u < 0 or u in out:
                break
            out.add(u)
            v = u
    return out


def _select(res, graph: Graph, hops: int, marks, universe: Optional[Set[int]] = None) -> Set[int]:
    """The truncation set: not plainly alive, roots, marked nodes, and `hops` undirected hops around them."""
    p, vs = res.prep, res.verdicts()
    seed = {i for i, k in enumerate(p.keys) if vs[k] != "alive"}
    seed |= {p.idx[r.node] for r in graph.roots if r.node in p.idx}
    seed |= {p.idx[k] for k in (marks or ()) if k in p.idx}
    if universe is not None:
        seed &= universe
    keep, front = set(seed), seed
    for _ in range(hops):
        nxt = set()
        for v in front:
            nxt.update(p.dst[p.off[v] : p.off[v + 1]])
            nxt.update(p.rsrc[p.roff[v] : p.roff[v + 1]])
        nxt -= keep
        if universe is not None:
            nxt &= universe
        keep |= nxt
        front = nxt
    return keep


def _dir(path: str) -> str:
    return path.rpartition("/")[0] or "."


LAYOUT_VERSION = 1  # part of the layout cache key: bump it when graph.layout changes what it returns
LAYOUT_KEEP = 24  # layouts kept in the cache directory (newest first)
_BAD = ("dead", "dead-cluster", SUSPECTS)


def _code_meta(snippets: Optional[Snips]) -> dict:
    return {"code": snippets.info, "local": LOCAL if snippets.text else ""} if snippets is not None else {}


def _layout_key(paths, weights, cuts, esrc, edst, etier) -> str:
    """sha256 over everything the layout depends on: the graph hash of the cache."""
    h = hashlib.sha256(b"DCL%d\0%d\0%d\0%d\0" % (LAYOUT_VERSION, len(paths), len(cuts), len(esrc)))
    h.update("\0".join([*paths, *(d for d, _ in cuts)]).encode("utf-8", "surrogatepass"))
    for col in (weights, [n for _, n in cuts], esrc, edst, etier):
        h.update(array("i", col).tobytes())
    return h.hexdigest()


def _cached_layout(paths, weights, cuts, esrc, edst, etier, d: Optional[str] = None) -> dclayout.Layout:
    """dclayout.compute through a cache keyed by the graph hash, in the directory `d` (a deletable cache: a missing, foreign,
    damaged or short entry is computed again and replaced; a directory that cannot be used is not used; None: no cache)."""
    if d is None:
        return dclayout.compute(paths, weights, cuts, esrc, edst, etier)
    key = _layout_key(paths, weights, cuts, esrc, edst, etier)
    path = os.path.join(d, key + ".dcv")
    try:
        with open(path, "rb") as fh:
            pk = from_bytes(fh.read())
        if len(pk.cols["n_x"]) == len(paths) and len(pk.cols["k_row"]) == len(cuts):
            return dclayout.Layout(pk.head["names"], pk.cols)
    except (OSError, ValueError, KeyError):
        pass
    lay = dclayout.compute(paths, weights, cuts, esrc, edst, etier)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        if not os.path.islink(d):
            _put(path, to_bytes(Pack({"names": lay.names}, lay.cols)))
            old = sorted((os.path.getmtime(os.path.join(d, f)), f) for f in os.listdir(d) if f.endswith(".dcv"))
            for _, f in old[:-LAYOUT_KEEP]:
                os.unlink(os.path.join(d, f))
    except OSError:
        pass
    return lay


def _layout_cols(
    graph: Graph, old: Sequence[int], ec, cc, nvd, st: "_Tab", cache: Optional[str] = None
) -> Dict[str, array]:
    """The columns of the graph view: the cells (a_*), the positions of the nodes (n_x, n_y, n_agg) and the file-pair edges
    (g_*) of graph.layout, plus per cell the node count, the dead + suspect count and the worst verdict (a_n, a_bad, a_vd).
    """
    paths = [graph.nodes[o].path for o in old]
    weights = [1 + min(50, max(0, graph.nodes[o].line1 - graph.nodes[o].line0)) for o in old]
    cuts = [(st.items[d], n) for d, n in zip(cc["c_dir"], cc["c_n"])]
    lay = _cached_layout(paths, weights, cuts, ec["e_src"], ec["e_dst"], [k & 7 for k in ec["e_kd"]], cache)
    cols = dict(lay.cols)
    krow, parent, agg = cols.pop("k_row"), cols["a_parent"], cols["n_agg"]
    rank = [_WORST.index(v) for v in PACK_VERDICTS]
    bad = {PACK_VERDICTS.index(v) for v in _BAD}
    cnt, dead, worst = [0] * len(parent), [0] * len(parent), [len(_WORST)] * len(parent)

    def add(r: int, n: int, v: int) -> None:
        cnt[r] += n
        dead[r] += n * (v in bad)
        worst[r] = min(worst[r], rank[v])

    for f, v in zip(agg, nvd):
        add(f, 1, v)
    for r, n, v in zip(krow, cc["c_n"], cc["c_vd"]):
        add(r, n, v)
    for r in range(len(parent) - 1, 0, -1):  # pre-order: a child's row is above its parent's
        cnt[parent[r]] += cnt[r]
        dead[parent[r]] += dead[r]
        worst[parent[r]] = min(worst[parent[r]], worst[r])
    cols["a_name"] = array("i", (st(x) for x in lay.names))
    cols["a_n"], cols["a_bad"] = array("i", cnt), array("i", dead)
    cols["a_vd"] = array("i", (PACK_VERDICTS.index(_WORST[w]) if w < len(_WORST) else -1 for w in worst))
    return cols


def build_pack(
    graph: Graph,
    res,
    *,
    keep: Optional[Iterable[int]] = None,
    marks: Optional[Dict[str, int]] = None,
    findings: Sequence[Finding] = (),
    meta: Optional[dict] = None,
    why_max: int = WHY_MAX,
    evid: bool = True,
    universe: Optional[Set[int]] = None,
    suspects: Iterable[str] = (),
    snippets: Optional[Mapping[str, str]] = None,
    ev_ok: Optional[AbstractSet[str]] = None,
    layout_dir: Optional[str] = None,
) -> Pack:
    """Columnar pack of `graph` (all nodes, or the `keep` node indexes closed over their par[] chains). Node order is the
    canonical node order of the graph, so an index of the full pack is the index of the reach.Result. `universe` = the node
    indexes that count as "everything" for the truncation mark and the cells (None: the whole graph). `suspects` = the node keys
    whose dead verdict nothing corroborates (graph.verdict): the pack names their verdict "suspect". `snippets` = node key -> the code
    text of the symbol (graph.snippets, already scanned): the s_node / s_str columns carry the ones of the kept nodes.
    `ev_ok` = the edge evidence texts (up to 120 bytes of a source line) that the same gitleaks scan found clean (Snips.ev_ok): v_snip
    carries no other one (None: no filter, for a caller that has no scan). `layout_dir` = the directory of the layout cache (None: none)."""
    p, marks = res.prep, marks or {}
    vd = {**res.verdicts(), **dict.fromkeys(suspects, SUSPECTS)}
    old = sorted(_close(res, keep)) if keep is not None else list(range(len(p.keys)))
    new = {o: i for i, o in enumerate(old)}
    st, ft = _Tab(), _Tab()
    langs = sorted({graph.nodes[o].lang for o in old})
    lix = {x: i for i, x in enumerate(langs)}
    ec = {k: array("i") for k in ("e_src", "e_dst", "e_kd", "e_site", "e_ev")}
    enew: Dict[int, int] = {}
    ev_old: Dict[int, int] = {}
    for j, e in enumerate(graph.edges):
        s, d = p.idx.get(e.src), p.idx.get(e.dst)
        if s in new and d in new:
            enew[j] = len(ec["e_src"])
            ec["e_src"].append(new[s])
            ec["e_dst"].append(new[d])
            ec["e_kd"].append(_EK[e.kind] << 3 | _TI[e.tier])
            ec["e_site"].append(st(e.site))
            ec["e_ev"].append(ev_old.setdefault(e.ev, len(ev_old)) if evid and e.ev >= 0 else -1)
    nc = {k: array("i") for k in ("n_file", "n_name", "n_sfx", "n_kind", "n_lang", "n_l0", "n_l1", "n_vis", "n_flags", "n_vd", "n_par", "n_pare", "n_rt", "n_mark", "n_why")}  # fmt: skip
    vis = ("private", "package", "public", "exported")
    dead_n = 0
    for o in old:
        key, n = p.keys[o], graph.nodes[o]
        _lang, path, qual, _ar, h8 = split_key(key)
        v = vd[key]
        why = -1
        if v in ("dead", "dead-cluster", SUSPECTS) and dead_n < why_max:
            dead_n += 1
            why = st(res.why_dead(key).render())
        reached = res.state[o] != 0
        par, pare = res.par[o], res.pare[o]
        for k, x in (
            ("n_file", ft(n.path)),
            ("n_name", st(qual or path.rpartition("/")[2])),
            ("n_sfx", st("#" + key.partition(":")[2].partition("#")[2] if qual else "~" + h8 if h8 else "")),
            ("n_kind", NODE_KINDS.index(n.kind)),
            ("n_lang", lix[n.lang]),
            ("n_l0", n.line0),
            ("n_l1", n.line1),
            ("n_vis", vis.index(n.vis)),
            ("n_flags", n.flags),
            ("n_vd", PACK_VERDICTS.index(v)),
            ("n_par", new[par] if reached and par >= 0 else -1),
            ("n_pare", enew[pare] if reached and pare >= 0 else -1),
            ("n_rt", res.rt[o] if reached else -1),
            ("n_mark", marks.get(key, 0)),
            ("n_why", why),
        ):
            nc[k].append(x)
    rc = {k: array("i") for k in ("r_node", "r_kind", "r_origin", "r_str", "r_trust", "r_stub", "r_sel")}
    for r in graph.roots:
        for k, x in (
            ("r_node", new.get(p.idx.get(r.node), -1) if r.node else -1),
            ("r_kind", st(r.kind)),
            ("r_origin", st(r.origin)),
            ("r_str", int(r.strength == "soft")),
            ("r_trust", TRUSTS.index(r.trust)),
            ("r_stub", st(r.stub)),
            ("r_sel", st(r.selector)),
        ):
            rc[k].append(x)
    vc = {k: array("i") for k in ("v_src", "v_file", "v_line", "v_snip", "v_ext")}
    pool = graph.evid
    for oi in sorted(ev_old, key=ev_old.get):
        ev = pool[oi]
        for k, x in (("v_src", st(ev.source)), ("v_file", st(ev.file)), ("v_line", ev.line), ("v_snip", st(ev.snippet if ev_ok is None or ev.snippet in ev_ok else "")), ("v_ext", st(ev.extractor))):  # fmt: skip
            vc[k].append(x)
    cc = {k: array("i") for k in ("c_dir", "c_vd", "c_n")}
    whole = range(len(p.keys)) if universe is None else sorted(universe)
    cut = [o for o in whole if o not in new]
    if cut:  # the omitted nodes stay as counts per (module, verdict)
        cnt = Counter((_dir(graph.nodes[o].path), vd[p.keys[o]]) for o in cut)
        for (d, v), c in sorted(cnt.items()):
            cc["c_dir"].append(st(d))
            cc["c_vd"].append(PACK_VERDICTS.index(v))
            cc["c_n"].append(c)
    sc = {k: array("i") for k in ("s_node", "s_str")}
    for o in old if snippets else ():
        if p.keys[o] in snippets:
            sc["s_node"].append(new[o])
            sc["s_str"].append(st(snippets[p.keys[o]]))
    lc = _layout_cols(graph, old, ec, cc, nc["n_vd"], st, layout_dir)
    cols = {**nc, **ec, **rc, **vc, **cc, **lc, **sc}
    m = {k: meta[k] for k in _META_KEYS if meta and k in meta}
    m.update(
        counts=dict(sorted(Counter(vd.values()).items())), nodes_total=len(whole), edges_total=len(graph.edges), nodes_kept=len(old), truncated=bool(cut)
    )  # fmt: skip
    head = {
        "v": 1,
        "tables": {"verdicts": list(PACK_VERDICTS), "kinds": list(NODE_KINDS), "edge_kinds": list(EDGE_KINDS), "tiers": list(TIERS), "trusts": list(TRUSTS), "vis": list(vis)},  # fmt: skip
        "palette": PALETTE,
        "ramp": [list(r) for r in RAMP],
        "world": [dclayout.WORLD_W, dclayout.WORLD_H, dclayout.Q],
        "strings": st.items,
        "files": ft.items,
        "langs": langs,
        "meta": m,
        "findings": [f.as_dict() for f in report.ensure_all(list(findings)[:FINDINGS_MAX])],
    }
    return Pack(head, cols)


def to_bytes(pack: Pack) -> bytes:
    head = dict(pack.head, cols=[[k, len(v)] for k, v in pack.cols.items()])
    h = json.dumps(head, separators=(",", ":"), sort_keys=True).encode()
    h += b" " * (-len(h) % 4)  # the columns start 4-byte aligned
    out = [MAGIC, struct.pack("<I", len(h)), h]
    for a in pack.cols.values():
        if sys.byteorder == "big":
            a = array("i", a)
            a.byteswap()
        out.append(a.tobytes())
    return b"".join(out)


def from_bytes(blob: bytes) -> Pack:
    if blob[:4] != MAGIC:
        raise ValueError("not a dead-code-checker pack")
    (hl,) = struct.unpack_from("<I", blob, 4)
    head = json.loads(blob[8 : 8 + hl])
    off, cols = 8 + hl, {}
    for name, n in head.pop("cols"):
        a = array("i")
        a.frombytes(blob[off : off + 4 * n])
        if sys.byteorder == "big":
            a.byteswap()
        cols[name], off = a, off + 4 * n
    if off != len(blob):
        raise ValueError("trailing bytes after the last column")
    return Pack(head, cols)


def to_json_gz(pack: Pack) -> bytes:
    doc = {"head": pack.head, "cols": {k: list(v) for k, v in pack.cols.items()}}
    return gzip.compress(json.dumps(doc, separators=(",", ":"), sort_keys=True).encode(), 6, mtime=0)


# ---------------------------------------------------------------- the page
class Page(NamedTuple):
    html: str
    ok: bool  # False: even the smallest cut is over the limit
    truncated: bool
    size: int
    kept: int
    total: int
    hops: Optional[int]  # None: whole graph
    pack: Optional[Pack] = None  # the pack the page carries


def _asset(name: str) -> str:
    return Path(VIEWER, name).read_text(encoding="utf-8")


def _page(blob: bytes, plain: bool, title: str, local: bool = False, lens: Optional[bytes] = None) -> str:
    vals = {
        "LENS": (
            f'<script type="application/octet-stream" id="dclens">{base64.b64encode(lens).decode("ascii")}</script>'
            if lens
            else ""
        ),
        "TITLE": html.escape(title),
        "LOCAL": f'<meta name="dc-local" content="{html.escape(LOCAL)}">' if local else "",
        "CSS": _asset("viewer.css"),
        "JS": _asset("viewer.js"),
        "ENC": "raw" if plain else "deflate",
        "DATA": base64.b64encode(blob if plain else zlib.compress(blob, 6)).decode("ascii"),
    }
    return re.sub(r"@@(\w+)@@", lambda m: vals[m.group(1)], _asset("shell.html"))  # one pass: no re-scan of the data


def make_page(
    graph: Graph,
    res,
    *,
    keep: Optional[Iterable[int]] = None,
    marks=None,
    findings: Sequence[Finding] = (),
    meta: Optional[dict] = None,
    title: str = "dead-code-checker",
    plain: bool = False,
    limit: int = PAGE_MAX,
    full_nodes: int = FULL_NODES,
    full_edges: int = FULL_EDGES,
    why_max: int = WHY_MAX,
    links: Sequence[Tuple[str, str]] = (),
    suspects: Iterable[str] = (),
    snippets: Optional[Snips] = None,
    layout_dir: Optional[str] = None,
    lens: Optional[dclens.Lens] = None,
) -> Page:
    universe = None if keep is None else _close(res, keep)
    total = len(res.prep.keys) if universe is None else len(universe)
    big = total > full_nodes or (universe is None and len(graph.edges) > full_edges)
    m = dict(meta or {}, links=[list(x) for x in links], mode="index" if universe is None else "change")
    if snippets is not None:
        m.update(_code_meta(snippets))
    page = None
    for hops, rich in ([] if big else [(None, True)]) + [(2, True), (1, True), (0, True), (0, False)]:
        sel = universe if hops is None else _select(res, graph, hops, marks, universe)
        code = snippets.text if snippets is not None and rich else None
        lblob = dclens.blob(lens) if lens is not None and lens.chunks and rich else None
        lmeta = (
            {"lens": json.loads(lblob[8 : 8 + int.from_bytes(lblob[4:8], "little")])["info"]}
            if lblob
            else ({"lens": lens.info} if lens is not None else {})
        )
        pack = build_pack(
            graph,
            res,
            keep=sel,
            marks=marks,
            findings=findings,
            meta=dict(m, hops=hops, **lmeta),
            why_max=why_max if rich else 0,
            evid=rich,
            universe=universe,
            suspects=suspects,
            snippets=code,
            ev_ok=snippets.ev_ok if snippets is not None else None,
            layout_dir=layout_dir,
        )
        text = _page(to_bytes(pack), plain, title, bool(code and len(pack.cols["s_node"])), lblob)
        kept = pack.head["meta"]["nodes_kept"]
        page = Page(text, len(text) <= limit, kept < total, len(text), kept, total, hops, pack)
        if page.ok:
            return page
    return page


# ---------------------------------------------------------------- diagrams (module overview, change subgraph)
class DNode(NamedTuple):
    id: str
    label: str
    sub: str  # second line: the verdict word, or counts of a module
    group: str  # cluster: the top-level directory
    verdict: str
    url: str
    mark: int
    ramp: int = -1  # a module of the overview: its index in RAMP (the fill), else the fill is the verdict's


class DEdge(NamedTuple):
    a: str
    b: str
    tier: str
    n: int


class Diagram(NamedTuple):
    title: str
    nodes: Tuple[DNode, ...]
    edges: Tuple[DEdge, ...]
    modules: bool = False  # the module overview: a compact grid, loose edges and the RAMP legend


def change_set(res, touched: Iterable[str], newly: Iterable[str] = (), cap: int = CHANGE_CAP) -> Set[int]:
    """Node indexes of the branch subgraph, by priority up to `cap`: the touched and newly dead symbols, their callers, the
    par[] paths to their roots, their callees (dead neighbours are among callers and callees), then the closure over par[].
    """
    p = res.prep
    seeds = [p.idx[k] for k in sorted(set(touched) | set(newly)) if k in p.idx]
    out: Set[int] = set()

    def take(it):
        for v in it:
            if len(out) >= cap:
                return
            out.add(v)

    take(seeds)
    take(u for v in seeds for u in p.rsrc[p.roff[v] : p.roff[v + 1]])
    for v in seeds:
        take(sorted(_close(res, [v]) - out))
    take(u for v in seeds for u in p.dst[p.off[v] : p.off[v + 1]])
    return _close(res, out)


def _ramp(share: float) -> int:
    """Index in RAMP of a module whose share of dead + dead-cluster + suspect nodes is `share` (0 only for none at all)."""
    return 0 if share <= 0 else next(i for i, r in enumerate(RAMP) if i and share <= r[0] + 1e-9)


def _paint(n: DNode) -> Tuple[str, str]:
    return RAMP[n.ramp][1:3] if n.ramp >= 0 else PALETTE[n.verdict]


def _mod(path: str, depth: int) -> str:
    return "/".join(path.split("/")[:-1][:depth]) or "."


def diagram(
    graph: Graph,
    res,
    idxs: Iterable[int],
    marks,
    title: str,
    page: str,
    max_nodes: int = SVG_MAX,
    modules: bool = False,
    suspects: Iterable[str] = (),
) -> Diagram:
    """Nodes `idxs` as a diagram; more than `max_nodes` (or modules=True) collapses to modules at the deepest depth that fits.
    `suspects` = the node keys whose dead verdict nothing corroborates (graph.verdict): drawn in the suspect colour, never counted as dead,
    like the HTML viewer."""
    p, vd, marks = res.prep, {**res.verdicts(), **dict.fromkeys(suspects, SUSPECTS)}, marks or {}
    idxs = sorted(idxs)
    if modules or len(idxs) > max_nodes:
        depth = max((len(graph.nodes[o].path.split("/")) - 1 for o in idxs), default=0)
        while depth > 0 and len({_mod(graph.nodes[o].path, depth) for o in idxs}) > max_nodes:
            depth -= 1
        mods: Dict[str, List[int]] = {}
        for o in idxs:
            mods.setdefault(_mod(graph.nodes[o].path, depth), []).append(o)
        of = {o: m for m, os_ in mods.items() for o in os_}
        nodes = []
        for i, m in enumerate(sorted(mods)):
            vs = Counter(vd[p.keys[o]] for o in mods[m])
            worst, dead, sus = next(v for v in _WORST if vs[v]), vs["dead"] + vs["dead-cluster"], vs[SUSPECTS]
            sub = f"{len(mods[m])} nodes, {dead} dead, {sus} suspect"
            mark = max((marks.get(p.keys[o], 0) for o in mods[m]), default=0)
            nodes.append(
                DNode(
                    f"m{i}",
                    m,
                    sub,
                    m.split("/")[0],
                    worst,
                    f"{page}#d=" + _q(m),
                    mark,
                    _ramp((dead + sus) / len(mods[m])),
                )
            )
        ids = {n.label: n.id for n in nodes}
        cnt: Dict[Tuple[str, str], List] = {}
        for e in graph.edges:
            s, d = p.idx.get(e.src), p.idx.get(e.dst)
            if s in of and d in of and of[s] != of[d]:
                row = cnt.setdefault((ids[of[s]], ids[of[d]]), [e.tier, 0])
                row[0], row[1] = min(row[0], e.tier, key=_TI.get), row[1] + 1
        return Diagram(title, tuple(nodes), tuple(DEdge(a, b, t, n) for (a, b), (t, n) in sorted(cnt.items())), True)
    have = set(idxs)
    nodes = []
    for o in idxs:
        n, key = graph.nodes[o], p.keys[o]
        nodes.append(DNode(f"n{o}", split_key(key)[2] or n.path.rpartition("/")[2], vd[key], _mod(n.path, 1), vd[key], f"{page}#k=" + _q(key), marks.get(key, 0)))  # fmt: skip
    best: Dict[Tuple[str, str], str] = {}
    for e in graph.edges:
        s, d = p.idx.get(e.src), p.idx.get(e.dst)
        if s in have and d in have and s != d:
            k = (f"n{s}", f"n{d}")
            best[k] = min(best.get(k, e.tier), e.tier, key=_TI.get)
    return Diagram(title, tuple(nodes), tuple(DEdge(a, b, t, 1) for (a, b), t in sorted(best.items())))


def modules_diagram(
    graph: Graph, res, marks=None, max_nodes: int = SVG_MAX, suspects: Iterable[str] = (), page: str = "index.html"
) -> Diagram:
    """The module overview; `page` is the page its boxes link to (graph.publish: "", the links stay inside the picture)."""
    return diagram(
        graph,
        res,
        range(len(res.prep.keys)),
        marks,
        "modules",
        page,
        max_nodes,
        modules=True,
        suspects=suspects,
    )


def _q(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="/:#~.-_", errors="replace")


# ---------------------------------------------------------------- DOT and the built-in emitter
_BAD_XML = re.compile("[^\t\n\r -\ud7ff\ue000-\ufffd]")


def _clip(s: str, n: int = 40) -> str:
    s = _BAD_XML.sub("", s)
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _dq(s: str) -> str:
    return _clip(s, 60).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _box_w(n: DNode) -> float:
    return min(240.0, 16 + 6.4 * max(len(_clip(n.label)), len(_clip(n.sub))))


def _layout(d: Diagram, aspect: float = 1.0) -> Dict[str, Tuple[int, int]]:
    """The grid of the module overview: node id -> (level, column). A group (top-level directory) is a block of about
    sqrt(n) columns, the blocks are packed left to right in rows of at most `budget` columns, the budget being chosen so that
    the picture is about `aspect` times wider than high (a box is wider than high, so a square count of cells is not square).
    """
    groups: Dict[str, List[str]] = {}
    for n in d.nodes:
        groups.setdefault(n.group, []).append(n.id)
    cw = sum(_box_w(n) for n in d.nodes) / max(1, len(d.nodes)) + GAP_X
    ratio = (BOX_H + GAP_Y) / cw
    budget = max(1, math.ceil(math.sqrt(aspect * len(d.nodes) * ratio)))
    pos: Dict[str, Tuple[int, int]] = {}
    x = top = tall = 0
    for _, ids in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        cols = min(budget, max(1, math.ceil(math.sqrt(len(ids) * ratio))))
        if x and x + cols > budget:
            x, top, tall = 0, top + tall, 0
        pos.update({v: (top + i // cols, x + i % cols) for i, v in enumerate(ids)})
        x, tall = x + cols, max(tall, -(-len(ids) // cols))
    return pos


def _legend() -> List[Tuple[str, str]]:
    return [(r[1], r[3]) for r in RAMP]


def _dot_grid(d: Diagram, ix: Dict[str, str]) -> List[str]:
    """Invisible edges that make dot rank the modules as the _layout grid: every node of level L > 0 hangs under one node of
    level L - 1 (the one in its column, else the nearest column), so a level is exactly a rank; the real edges carry no
    constraint. Neither rank=same nor newrank: flat edges between clusters of one rank make dot 2.42 loop in mincross.
    """
    pos = _layout(d)
    levels: Dict[int, List[Tuple[int, str]]] = {}
    for v, (lv, c) in pos.items():
        levels.setdefault(lv, []).append((c, v))
    out = []
    for lv in sorted(levels):
        if lv:
            above = sorted(levels[lv - 1])
            for c, v in sorted(levels[lv]):
                out.append(f"  {ix[min(above, key=lambda t: (abs(t[0] - c), t[0]))[1]]} -> {ix[v]} [style=invis];")
    return out


def to_dot(d: Diagram) -> str:
    """DOT text. Every id is generated (n0, n1, ...): the names from the source tree appear only as quoted labels. The module
    overview (d.modules) is a compact grid and has no real edges in it: a long edge makes dot add a virtual node to every rank
    it crosses, and 900 of them made the picture 5 times wider than the grid. dot only places the boxes (invisible edges give
    the ranks, a legend of the colour ramp is the graph label); render_svg draws the module edges over that layout."""
    ix = {n.id: f"v{i}" for i, n in enumerate(d.nodes)}
    out = [
        "digraph G {",
        '  rankdir=TB; node [shape=box, style=filled, fontname="sans-serif", fontsize=10]; edge [arrowsize=0.6];',
    ]
    if d.modules:
        out.append("  nodesep=0.15; ranksep=0.25; splines=line;")
    groups: Dict[str, List[DNode]] = {}
    for n in d.nodes:
        groups.setdefault(n.group, []).append(n)

    def line(n: DNode) -> str:
        fill, ink = _paint(n)
        w = 3 if n.mark & MARK_NEWDEAD else 2 if n.mark & MARK_TOUCHED else 1
        extra = " peripheries=2" if n.mark & MARK_NEWDEAD else ""
        return f'{ix[n.id]} [label="{_dq(n.label)}\\n{_dq(n.sub)}", fillcolor="{fill}", fontcolor="{ink}", penwidth={w}{extra}, URL="{_dq(n.url)}", tooltip="{_dq(n.label)}"];'

    for gi, (g, ns) in enumerate(sorted(groups.items())):
        if len(groups) > 1:
            out.append(f'  subgraph cluster_{gi} {{ label="{_dq(g)}"; style=rounded;')
        out += ["    " + line(n) for n in ns]
        if len(groups) > 1:
            out.append("  }")
    style = {"S": "solid", "T": "solid", "N": "dashed", "D": "dotted"}
    if d.modules:
        out += _dot_grid(d, ix)
    else:
        for e in d.edges:
            out.append(f"  {ix[e.a]} -> {ix[e.b]} [style={style[e.tier]}, penwidth={1 + (e.n > 9) + (e.n > 99)}];")
    if d.modules:
        cells = "".join(f'<td bgcolor="{f}" width="22">&nbsp;</td><td>{c}</td>' for f, c in _legend())
        out.append(
            '  labelloc=b; labeljust=l; fontsize=10; label=<<table border="0" cellborder="1" cellspacing="2"><tr><td border="0">module colour: share of dead and suspect nodes</td>'
            + cells
            + "</tr></table>>;"
        )
    return "\n".join(out + ["}", ""])


def _layers(ids: List[str], adj: Dict[str, List[str]]) -> List[List[str]]:
    """DFS cycle break, longest-path layers, two barycentre sweeps: the rows of the layered picture."""
    state: Dict[str, int] = {}
    order: List[str] = []
    back: Set[Tuple[str, str]] = set()

    def dfs(v):
        state[v] = 1
        for u in sorted(adj[v]):
            if state.get(u) == 1:
                back.add((v, u))
            elif u not in state:
                dfs(u)
        state[v] = 2
        order.append(v)

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 4 * len(ids) + 100))
    for v in sorted(ids):
        if v not in state:
            dfs(v)
    layer = {v: 0 for v in ids}
    for v in reversed(order):  # reverse postorder: every non-back edge goes forward
        for u in adj[v]:
            if (v, u) not in back:
                layer[u] = max(layer[u], layer[v] + 1)
    rows: List[List[str]] = [[] for _ in range(max(layer.values(), default=-1) + 1)]
    for v in sorted(ids):
        rows[layer[v]].append(v)
    pos = {v: i for r in rows for i, v in enumerate(r)}
    preds: Dict[str, List[str]] = {v: [] for v in ids}
    for v in ids:
        for u in adj[v]:
            preds[u].append(v)
    for nbr, seq in ((preds, rows[1:]), (adj, rows[-2::-1])):
        for r in seq:
            r.sort(key=lambda v: (sum(pos[u] for u in nbr[v]) / len(nbr[v]) if nbr[v] else pos[v], v))
            pos.update({v: i for i, v in enumerate(r)})
    return rows


def _cut(a: Tuple[float, float], b: Tuple[float, float], w: float, h: float) -> Tuple[float, float]:
    """The point where the segment from the centre `a` towards `b` leaves the box w x h around `a`."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = min(w / 2 / abs(dx) if dx else math.inf, h / 2 / abs(dy) if dy else math.inf)
    return a[0] + dx * t, a[1] + dy * t


_DASH = {"S": "", "T": "", "N": ' stroke-dasharray="6 3"', "D": ' stroke-dasharray="2 3"'}
Box = Tuple[float, float, float, float]  # x0, y0, x1, y1
_MARKER = '<defs><marker id="a" markerUnits="userSpaceOnUse" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#555"/></marker></defs>'


def _edge_line(e: DEdge, a: Box, b: Box, names: Optional[Tuple[str, str]] = None) -> str:
    """A straight edge from the border of box `a` to the border of box `b` ('' for boxes with one centre). With `names` (the
    module overview) the width grows with the call count and the edge has a tooltip, and a merged edge (n > 1) a count label.
    """
    ca, cb = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2), ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    if ca == cb:
        return ""
    (x1, y1), (x2, y2) = _cut(ca, cb, a[2] - a[0], a[3] - a[1]), _cut(cb, ca, b[2] - b[0], b[3] - b[1])
    sw = f' stroke-width="{1 + (e.n > 9) + (e.n > 99)}"' if names else ""
    tip = f"<title>{html.escape(_clip(names[0]))} to {html.escape(_clip(names[1]))}: {e.n}</title>" if names else ""
    out = f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"{_DASH[e.tier]}{sw} marker-end="url(#a)">{tip}</line>'
    if names and e.n > 1:
        out += f'<text x="{(x1 + x2) / 2:.1f}" y="{(y1 + y2) / 2:.1f}" font-size="8" fill="#333" stroke="none">{e.n}</text>'
    return out


_DOT_NODE = re.compile(r'class="node">\s*<title>(v\d+)</title>.*?<polygon[^>]*points="([^"]+)"', re.S)


def _with_edges(svg: str, d: Diagram) -> str:
    """The module edges drawn over the boxes dot placed (to_dot has none): straight lines under the boxes, over the frames.
    ValueError when the dot SVG does not have the boxes where they are expected."""
    ix = {f"v{i}": n for i, n in enumerate(d.nodes)}
    box: Dict[str, Box] = {}
    for m in _DOT_NODE.finditer(svg):
        xy = [tuple(float(c) for c in pt.split(",")) for pt in m.group(2).split()]
        box[ix[m.group(1)].id] = (
            min(x for x, _ in xy),
            min(y for _, y in xy),
            max(x for x, _ in xy),
            max(y for _, y in xy),
        )
    at = svg.find('<g id="node')
    if len(box) != len(d.nodes) or at < 0:
        raise ValueError(f"{len(box)} of {len(d.nodes)} boxes found in the dot SVG")
    by = {n.id: n for n in d.nodes}
    lines = [_edge_line(e, box[e.a], box[e.b], (by[e.a].label, by[e.b].label)) for e in d.edges if e.a != e.b]
    g = '<g stroke="#555" fill="none" stroke-width="1" stroke-opacity="0.6">' + "".join(lines) + "</g>\n"
    return svg[:at] + _MARKER + g + svg[at:]


def module_grid(d: Diagram, w: Dict[str, float], margin: int = 16):
    """The boxes of the module overview (the layout of emit_svg, shared with graph.publish's .drawio): (xy, cw, width, height, frames) =
    node id -> (x, y) of its box, the one box width, the picture's width and height (without the legend) and the group frames
    (group, x, y, width, height). `w` = the width of each box before they are made equal."""
    pos = _layout(d)
    cw = max(w.values(), default=0)
    width = 2 * margin + (max((c for _, c in pos.values()), default=-1) + 1) * (cw + GAP_X) - GAP_X
    xy = {v: (margin + c * (cw + GAP_X), margin + lv * (BOX_H + MOD_GAP_Y)) for v, (lv, c) in pos.items()}
    frames = []
    for g in sorted({n.group for n in d.nodes}):
        box = [xy[n.id] for n in d.nodes if n.group == g]
        x0, y0 = min(x for x, _ in box) - 6, min(y for _, y in box) - 6
        frames.append((g, x0, y0, max(x for x, _ in box) + cw + 6 - x0, max(y for _, y in box) + BOX_H + 6 - y0))
    total_h = margin * 2 + (max((lv for lv, _ in pos.values()), default=-1) + 1) * (BOX_H + MOD_GAP_Y) - MOD_GAP_Y
    return xy, cw, width, total_h, frames


def emit_svg(d: Diagram) -> str:
    """Built-in SVG. A diagram is layered (DFS cycle break, longest-path layers, barycentre sweeps) and a layer wider than
    the picture should be is wrapped after W nodes; the module overview is the _layout grid with a frame per group. Edges are
    straight lines from box to box, the nodes are <a href> links like the ones dot writes. Meant for up to a few hundred nodes.
    """
    ids = [n.id for n in d.nodes]
    adj: Dict[str, List[str]] = {i: [] for i in ids}
    for e in d.edges:
        if e.a != e.b and e.b not in adj[e.a]:
            adj[e.a].append(e.b)
    by = {n.id: n for n in d.nodes}
    w = {v: _box_w(by[v]) for v in ids}
    margin, gap_y = 16, GAP_Y
    xy: Dict[str, Tuple[float, float]] = {}
    frames: List[Tuple[str, float, float, float, float]] = []
    if d.modules:
        gap_y = MOD_GAP_Y  # the frame label sits in the gap
        xy, cw, width, total_h, frames = module_grid(d, w, margin)
        w = dict.fromkeys(ids, cw)
    else:
        rows = _layers(ids, adj)
        cap = max(
            1, math.ceil(math.sqrt(2.0 * len(ids) * (BOX_H + gap_y) / (sum(w.values()) / max(1, len(ids)) + GAP_X)))
        )
        rows = [r[i : i + cap] for r in rows for i in range(0, len(r), cap)]
        widths = [sum(w[v] + GAP_X for v in r) - GAP_X for r in rows]
        width = max(widths, default=0) + 2 * margin
        for li, r in enumerate(rows):
            x = margin + (width - 2 * margin - widths[li]) / 2
            for v in r:
                xy[v] = (x, margin + li * (BOX_H + gap_y))
                x += w[v] + GAP_X
        total_h = margin * 2 + len(rows) * (BOX_H + gap_y) - gap_y if rows else 2 * margin
    legend = []
    if d.modules:
        width = max(width, 2 * margin + 6 * 96)
        total_h += 34
        legend.append(
            f'<text x="{margin}" y="{total_h - margin - 2}" font-size="10">module colour: share of dead and suspect nodes</text>'
        )
        for i, (fill, cap_) in enumerate(_legend()):
            x = margin + i * 96
            legend.append(
                f'<rect x="{x}" y="{total_h - margin + 2}" width="14" height="10" fill="{fill}" stroke="#333"/><text x="{x + 18}" y="{total_h - margin + 11}" font-size="9">{html.escape(cap_)}</text>'
            )
        total_h += 14
    esc = html.escape
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{total_h:.0f}" viewBox="0 0 {width:.0f} {total_h:.0f}" font-family="sans-serif" font-size="11">',
        f"<title>{esc(_clip(d.title, 80))}</title>",
        f'<polygon points="0,0 {width:.0f},0 {width:.0f},{total_h:.0f} 0,{total_h:.0f}" fill="#ffffff" stroke="none"/>',
        _MARKER,
    ]
    for g, fx, fy, fw, fh in frames:
        out.append(
            f'<rect x="{fx:.1f}" y="{fy:.1f}" width="{fw:.1f}" height="{fh:.1f}" rx="6" fill="none" stroke="#999"/><text x="{fx + 4:.1f}" y="{fy - 3:.1f}" font-size="9" fill="#555">{esc(_clip(g, 40))}</text>'
        )
    out.append('<g stroke="#555" fill="none" stroke-width="1"' + (' stroke-opacity="0.6"' if d.modules else "") + ">")
    for e in d.edges:
        if e.a != e.b:
            ba, bb = (*xy[e.a], xy[e.a][0] + w[e.a], xy[e.a][1] + BOX_H), (
                *xy[e.b],
                xy[e.b][0] + w[e.b],
                xy[e.b][1] + BOX_H,
            )
            out.append(_edge_line(e, ba, bb, (by[e.a].label, by[e.b].label) if d.modules else None))
    out.append("</g>")
    for v in ids:
        n, (nx, ny) = by[v], xy[v]
        fill, ink = _paint(n)
        sw = 3 if n.mark & MARK_NEWDEAD else 2 if n.mark & MARK_TOUCHED else 1
        stroke = "#00695c" if n.mark & MARK_TOUCHED else "#333"
        out.append(
            f'<a href="{esc(_BAD_XML.sub("", n.url), quote=True)}"><title>{esc(_clip(n.label, 200))} ({esc(n.verdict)})</title>'
            f'<rect x="{nx:.1f}" y="{ny:.1f}" width="{w[v]:.1f}" height="{BOX_H}" rx="4" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'
            f'<text x="{nx + w[v] / 2:.1f}" y="{ny + 15}" text-anchor="middle" fill="{ink}">{esc(_clip(n.label))}</text>'
            f'<text x="{nx + w[v] / 2:.1f}" y="{ny + 29}" text-anchor="middle" fill="{ink}" font-size="9">{esc(_clip(n.sub))}</text></a>'
        )
    return "\n".join(out + legend + ["</svg>", ""])


def dot_path() -> Optional[str]:
    """The Graphviz `dot` binary: $DEAD_CODE_CHECKER_DOT (an absolute path), else the first one on PATH; None when there is none."""
    own = (os.environ.get("DEAD_CODE_CHECKER_DOT") or "").strip()
    if own:
        return own if os.path.isabs(own) and os.path.isfile(own) and os.access(own, os.X_OK) else None
    return safepath.which("dot.exe" if os.name == "nt" else "dot") or None


_NETLESS: Dict[str, Tuple[str, ...]] = {}


def netless_prefix() -> Tuple[str, ...]:
    """The command prefix that runs a program without network and with a read-only file system, when bwrap is installed and
    works here; () otherwise (the program then runs unsandboxed: bwrap is optional)."""
    if "p" not in _NETLESS:
        exe = safepath.which("bwrap")
        pre = (exe, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-net", "--die-with-parent") if exe else ()
        try:
            ok = bool(pre) and safepath.run([*pre, safepath.which("true") or "/usr/bin/true"], capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        _NETLESS["p"] = pre if ok else ()
    return _NETLESS["p"]


INSTALL_DOT = "install Graphviz (apt install graphviz, brew install graphviz, or https://graphviz.org/download/)"


def render_svg(d: Diagram, timeout: float = 15.0) -> Tuple[str, Optional[Finding]]:
    """(svg, finding): Graphviz `dot -Tsvg` when it is installed and answers in time, else the built-in emitter and a W that says
    why and how to install Graphviz. The text goes to its stdin; the run is without network when bwrap is available."""
    exe = dot_path()
    if exe is None:
        return emit_svg(d), Finding("W", safety.WHERE, 0, "dc.graphviz-missing", f"Graphviz dot is not installed; the SVG files are drawn by the built-in emitter (plainer layout); {INSTALL_DOT}", "without Graphviz the SVG comes from the built-in emitter; the HTML page is unaffected")  # fmt: skip
    env = {k: os.environ[k] for k in ("HOME", "LANG", "LC_ALL", "XDG_CACHE_HOME", "SYSTEMROOT") if k in os.environ}
    try:
        p = safepath.run([*netless_prefix(), exe, "-Tsvg"], input=to_dot(d).encode("utf-8", "replace"), capture_output=True, timeout=timeout, env=env)  # fmt: skip
        err = p.stderr.decode("utf-8", "replace").strip().split("\n")[0][:200] if p.returncode else ""
        if p.returncode == 0 and b"<svg" in p.stdout:
            svg = p.stdout.decode("utf-8", "replace")
            return (_with_edges(svg, d) if d.modules else svg), None
        err = err or "no svg in the output"
    except subprocess.TimeoutExpired:
        err = f"timed out after {timeout:g}s"
    except (OSError, ValueError) as exc:
        err = str(exc)
    return emit_svg(d), Finding("W", safety.WHERE, 0, "dc.graphviz-failed", f"dot did not produce {d.title}.svg ({err}); the built-in emitter drew it instead", "dot ran and failed: the SVG is plainer, the findings are unaffected")  # fmt: skip


# ---------------------------------------------------------------- artifacts of a run
class Artifacts(NamedTuple):
    dir: str
    files: Tuple[str, ...]
    viewer: str  # the page the report links (change.html on the branch level, else index.html)
    findings: Tuple[Finding, ...]  # W findings about the artifacts themselves (no Graphviz, truncated, too large)
    summary: Optional[dict] = (
        None  # summary_doc of the run (findings of the checks and of the artifacts, languages of the graph)
    )


def _put(path: str, data) -> None:
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8", "replace"))
    os.replace(tmp, path)


def _latest(out_dir: str) -> None:
    link = os.path.join(os.path.dirname(out_dir), "latest")
    tmp = f"{link}.tmp{os.getpid()}"
    try:
        os.symlink(os.path.basename(out_dir), tmp)
        os.replace(tmp, link)
    except OSError:  # no symlinks (Windows without the right): a one-line pointer file
        _put(link, os.path.basename(out_dir) + "\n")


def summary_doc(
    pairs: Iterable[Tuple[str, str]], meta: Optional[Mapping] = None, langs: Optional[Iterable[str]] = None
) -> dict:
    """The few numbers of a run that the overview page (graph.pages) shows: E and W findings, suspects (code dc.suspect, level I), the
    size of the graph, the languages of its nodes (None = not known). `pairs` = (level, code) of every finding of the run.
    """
    ps, meta = list(pairs), meta or {}
    return {
        "v": 1,
        "E": sum(lv == "E" for lv, _ in ps),
        "W": sum(lv == "W" for lv, _ in ps),
        "suspect": sum(cd == "dc.suspect" for _, cd in ps),
        "files": meta.get("files"),
        "nodes": meta.get("nodes"),
        "edges": meta.get("edges"),
        "langs": sorted(langs) if langs is not None else None,
    }


def write_report(
    out_dir: str, findings: Sequence[Finding], meta: Optional[dict] = None, main: str = "", note: str = ""
) -> None:
    """report.md, findings.json and sarif.json of a run. report.md starts with the link to `main` (the page the run opens) and its path; a run
    without a page (`main` empty) says `note` there, never a link to a file that is not there."""
    fs = report.ensure_all(list(findings))
    first = (
        f"{Path(out_dir, main).resolve().as_uri()}  {os.path.join(os.path.abspath(out_dir), main)}" if main else note
    )
    os.makedirs(out_dir, exist_ok=True)
    _put(os.path.join(out_dir, "report.md"), f"{first or 'no page'}\n\n{report.to_md(fs, meta)}\n")
    _put(
        os.path.join(out_dir, "findings.json"), report.to_json(fs, dict(meta or {}, show_suspects=True)) + "\n"
    )  # the record keeps them all
    _put(os.path.join(out_dir, "sarif.json"), sarif.to_sarif(fs, dict(meta or {}, show_suspects=True)))


def write_run(
    out_dir: str,
    scope: str,
    graph: Graph,
    res,
    findings: Sequence[Finding] = (),
    meta: Optional[dict] = None,
    *,
    marks: Optional[Dict[str, int]] = None,
    plain: bool = False,
    limit: int = PAGE_MAX,
    timeout: Optional[float] = None,
    full_nodes: int = FULL_NODES,
    full_edges: int = FULL_EDGES,
    suspects: Iterable[str] = (),
    snippets: Optional[Snips] = None,
    quiet: bool = False,
    layout_dir: Optional[str] = None,
    lens: Optional[dclens.Lens] = None,
) -> Artifacts:
    """The artifacts of a branch or repo run next to report.md. Branch: change.html + change.svg (the change subgraph) and
    index.html + modules.svg; repo: index.html + modules.svg. Every page links the others (relative names). `suspects` = the node keys
    drawn as suspects (graph.verdict); report.md hides the dc.suspect findings unless meta["show_suspects"], findings.json keeps them.
    `snippets` (graph.snippets.collect) = the code of the symbols for the pages and graph.json.gz; its findings join the run's.
    `quiet`: a cut page (dc.viz-truncated) is level I, not W: in a big tree it is there on every run and says nothing to act on;
    report.md still lists it, and the explicit view / check CLI keeps the W. `layout_dir` = the layout cache directory (None: none).
    """
    os.makedirs(out_dir, exist_ok=True)
    timeout = timeout if timeout is not None else 15.0 if scope == "branch" else 60.0
    meta, extra, files = dict(meta or {}), list(snippets.findings if snippets else ()), []
    branch = scope == "branch"
    changed = [k for k, b in (marks or {}).items() if b & MARK_TOUCHED]
    newly = [k for k, b in (marks or {}).items() if b & MARK_NEWDEAD]
    links = [("overview", "index.html"), ("modules", "modules.svg")] + ([("change", "change.html"), ("change svg", "change.svg")] if branch else []) + [("report", "report.md"), ("graph", "graph.json.gz")]  # fmt: skip

    def warn(w):
        if w and not (w.code == "dc.graphviz-missing" and any(x.code == w.code for x in extra)):
            extra.append(w)

    def page(name, title, keep):
        pg = make_page(
            graph,
            res,
            keep=keep,
            marks=marks,
            findings=findings,
            meta=meta,
            title=title,
            plain=plain,
            limit=limit,
            full_nodes=full_nodes,
            full_edges=full_edges,
            links=links,
            suspects=suspects,
            snippets=snippets,
            layout_dir=layout_dir,
            lens=lens,
        )
        if not pg.ok:
            extra.append(Finding("W", safety.WHERE, 0, "dc.viz-too-large", f"{name} is {pg.size >> 20} MB even cut to {pg.kept} of {pg.total} nodes (limit {limit >> 20} MB): not written; open graph.json.gz", "the page limit is 24 MB"))  # fmt: skip
            return pg
        if pg.truncated:
            extra.append(Finding("I" if quiet else "W", safety.WHERE, 0, "dc.viz-truncated", f"{name} shows {pg.kept} of {pg.total} nodes (dead, weak-alive, roots, marked nodes and {pg.hops} hop(s) around them); graph.json.gz has all of them", "above the size limit the page is cut, never silently"))  # fmt: skip
        _put(os.path.join(out_dir, name), pg.html)
        files.append(name)
        return pg

    if lens is not None and lens.chunks:
        try:
            if dclens.write(out_dir, lens):
                files.append(f"{dclens.DIR}/{dclens.MANIFEST}")
        except OSError as exc:  # the chunks are an extra: the pages and the report stay
            extra.append(Finding("W", safety.WHERE, 0, "dc.viewer-lens-off", f"the lens files could not be written to {dclens.DIR}/ ({exc})", "the pages carry only what fits in them"))  # fmt: skip
    index = page("index.html", f"dead-code-checker {scope}", None)
    svg, w = render_svg(modules_diagram(graph, res, marks, suspects=suspects), timeout)
    _put(os.path.join(out_dir, "modules.svg"), svg)
    files.append("modules.svg")
    warn(w)
    if branch:
        cs = change_set(res, changed, newly)
        page("change.html", "dead-code-checker branch: change subgraph", cs)
        svg, w = render_svg(diagram(graph, res, cs, marks, "change", "change.html", suspects=suspects), timeout)
        _put(os.path.join(out_dir, "change.svg"), svg)
        files.append("change.svg")
        warn(w)
    whole = (
        index.pack
        if index is not None and index.ok and not index.truncated
        else build_pack(
            graph,
            res,
            marks=marks,
            findings=findings,
            meta=dict(meta, **_code_meta(snippets), **({"lens": lens.info} if lens is not None else {})),
            suspects=suspects,
            snippets=snippets.text if snippets else None,
            ev_ok=snippets.ev_ok if snippets else None,
            layout_dir=layout_dir,
        )
    )
    _put(os.path.join(out_dir, "graph.json.gz"), to_json_gz(whole))
    files.append("graph.json.gz")
    main = (
        "change.html"
        if branch and "change.html" in files
        else "index.html" if "index.html" in files else "graph.json.gz"
    )
    write_report(out_dir, list(findings) + extra, meta, main)
    files += ["report.md", "findings.json", "sarif.json"]
    _latest(out_dir)
    summary = summary_doc(((f.level, f.code) for f in [*findings, *extra]), meta, {n.lang for n in graph.nodes})
    return Artifacts(
        os.path.abspath(out_dir),
        tuple(sorted(files)),
        os.path.join(os.path.abspath(out_dir), main),
        tuple(extra),
        summary,
    )
