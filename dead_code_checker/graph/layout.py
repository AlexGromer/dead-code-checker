"""graph.layout: the deterministic hierarchical layout of the graph view.

    compute(paths, weights, cuts, esrc, edst, etier) -> Layout      directory -> file -> symbol, positions of the leaves, the file-level edges

The geometry is the one of the viewer's treemap (viewer.js buildTree/squarify/layout, same operations in the same order, so the
doubles are the same): a leaf symbol sits at the centre of its treemap cell, a file and a directory are the cells that hold
them, the cells of omitted nodes (a truncated page) are cells of their own. The treemap orders the children of a cell by weight
(descending) and then by name in UTF-16 code units; this module and the viewer do exactly that, so a graph view and a treemap
of one pack show a symbol in one place (tests/test_graph_viz.py compares the two under node, when node is installed).

Fixed point: every coordinate column is the world coordinate times Q (16) rounded half up, an Int32 that is the same on every
host. World = WORLD_W x WORLD_H.

    paths[i], weights[i]      the file path and the treemap weight (1 + the lines of the symbol, at most 51) of node i
    cuts                      [(directory, count)] one cell per row: nodes that a truncated page does not carry
    esrc, edst, etier         the kept edges as node indexes and the tier index (0 = best): aggregated per file pair

Layout.cols (Int32): a_parent a_kind(0 directory, 1 file, 2 omitted) a_x a_y a_w a_h  one row per cell in pre-order of the
sorted tree (row 0 is the root, so a subtree is a contiguous range), n_x n_y n_agg one row per node (n_agg = the row of its file
cell), g_src g_dst g_w g_tier one row per file pair that has an edge, sorted by (src, dst), g_w = number of edges, g_tier = the
best tier of them. Layout.names[r] = the name of cell row r (a directory segment, a file name, "" for the root and omitted cells). k_row[j] = the
cell row of the j-th entry of `cuts` (not a pack column: the caller gives that cell its count and verdict).
"""

from __future__ import annotations

import math
from array import array
from typing import Dict, List, NamedTuple, Sequence, Tuple

WORLD_W, WORLD_H = 1600, 1000
Q = 16
CELL_COLS = ("a_parent", "a_kind", "a_x", "a_y", "a_w", "a_h")
DIR, FILE, CUT = 0, 1, 2


class Layout(NamedTuple):
    names: List[str]
    cols: Dict[str, array]


class _Cell:
    __slots__ = (
        "name",
        "key",
        "kind",
        "wt",
        "kids",
        "dirs",
        "files",
        "items",
        "x",
        "y",
        "w",
        "h",
        "row",
        "node",
    )

    def __init__(self, name: str, kind: int, node: int = -1, wt: int = 0):
        self.name, self.kind, self.node, self.wt = name, kind, node, wt
        self.key = name.encode("utf-16-be", "surrogatepass")  # JS compares strings by UTF-16 code units
        self.kids: List["_Cell"] = []
        self.dirs: Dict[str, "_Cell"] = {}
        self.files: Dict[str, "_Cell"] = {}
        self.items: List["_Cell"] = []
        self.x = self.y = self.w = self.h = 0.0
        self.row = -1


def _squarify(items: Sequence[_Cell], x: float, y: float, w: float, h: float) -> None:
    total = sum(it.wt for it in items)
    if not (total > 0) or w <= 0 or h <= 0:
        for it in items:
            it.x, it.y, it.w, it.h = x, y, 0.0, 0.0
        return
    scale = (w * h) / total

    def worst(sm: float, mn: float, mx: float, side: float) -> float:
        return max((side * side * mx) / (sm * sm), (sm * sm) / (side * side * mn))

    i = 0
    while i < len(items):
        side = min(w, h)
        sm, mn, mx, w0, j = 0.0, math.inf, 0.0, math.inf, i
        while j < len(items):
            a = items[j].wt * scale
            s2 = sm + a
            w1 = worst(s2, min(mn, a), max(mx, a), side)
            if j > i and w1 > w0:
                break
            sm, mn, mx, w0 = s2, min(mn, a), max(mx, a), w1
            j += 1
        if w >= h:
            cw, yy = sm / h, y
            for k in range(i, j):
                ih = (items[k].wt * scale) / cw
                items[k].x, items[k].y, items[k].w, items[k].h = x, yy, cw, ih
                yy += ih
            x += cw
            w -= cw
        else:
            rh, xx = sm / w, x
            for k in range(i, j):
                iw = (items[k].wt * scale) / rh
                items[k].x, items[k].y, items[k].w, items[k].h = xx, y, iw, rh
                xx += iw
            y += rh
            h -= rh
        i = j


def _place(cell: _Cell, x: float, y: float, w: float, h: float) -> None:
    cell.x, cell.y, cell.w, cell.h = x, y, w, h
    pad, hdr = min(w, h) * 0.012, min(h * 0.1, 26)
    _squarify(
        cell.kids,
        x + pad,
        y + pad + hdr,
        max(w - 2 * pad, 0),
        max(h - 2 * pad - hdr, 0),
    )
    for k in cell.kids:
        if k.kind != CUT and k.node < 0:
            _place(k, k.x, k.y, k.w, k.h)


def _finish(cell: _Cell) -> None:
    """The children in treemap order (heavier first, then by name), the weight of a cell = the sum of its children."""
    if cell.kind == DIR:
        for k in (*cell.dirs.values(), *cell.files.values()):
            _finish(k)
        cell.kids = [*cell.dirs.values(), *cell.files.values(), *cell.items]
    cell.wt = sum(k.wt for k in cell.kids)
    cell.kids.sort(key=lambda k: (-k.wt, k.key))


def _q(v: float) -> int:
    return math.floor(v * Q + 0.5)


def compute(
    paths: Sequence[str],
    weights: Sequence[int],
    cuts: Sequence[Tuple[str, int]],
    esrc: Sequence[int],
    edst: Sequence[int],
    etier: Sequence[int],
) -> Layout:
    root = _Cell("", DIR)

    def dir_of(p: str) -> _Cell:
        d = root
        if p and p != ".":
            for seg in p.split("/"):
                n = d.dirs.get(seg)
                if n is None:
                    n = d.dirs[seg] = _Cell(seg, DIR)
                d = n
        return d

    by_path: Dict[str, _Cell] = {}
    leaves: List[_Cell] = []
    for i, p in enumerate(paths):
        f = by_path.get(p)
        if f is None:
            k = p.rfind("/")
            f = by_path[p] = _Cell(p[k + 1 :], FILE)
            dir_of(p[:k] if k >= 0 else "").files[p] = f
        leaf = _Cell("", FILE, node=i, wt=weights[i])
        f.kids.append(leaf)
        leaves.append(leaf)
    omitted: List[_Cell] = []
    for d, n in cuts:
        omitted.append(_Cell("", CUT, wt=n))
        dir_of(d).items.append(omitted[-1])
    _finish(root)
    _place(root, 0.0, 0.0, float(WORLD_W), float(WORLD_H))

    order: List[_Cell] = []
    parent = array("i")

    def walk(c: _Cell, up: int) -> None:
        c.row = len(order)
        order.append(c)
        parent.append(up)
        if c.kind == DIR:
            for k in c.kids:
                walk(k, c.row)
        elif c.kind == FILE:
            for k in c.kids:
                k.row = c.row  # a leaf has no row of its own: its "row" is the file cell that holds it

    walk(root, -1)
    cols = {k: array("i") for k in (*CELL_COLS, "n_x", "n_y", "n_agg", "g_src", "g_dst", "g_w", "g_tier")}
    cols["k_row"] = array("i", (c.row for c in omitted))
    cols["a_parent"] = parent
    for c in order:
        cols["a_kind"].append(c.kind)
        for k, v in zip(("a_x", "a_y", "a_w", "a_h"), (c.x, c.y, c.w, c.h)):
            cols[k].append(_q(v))
    for lf in leaves:
        cols["n_x"].append(_q(lf.x + lf.w / 2))
        cols["n_y"].append(_q(lf.y + lf.h / 2))
        cols["n_agg"].append(lf.row)
    agg = cols["n_agg"]
    best: Dict[Tuple[int, int], List[int]] = {}
    for s, d, t in zip(esrc, edst, etier):
        a, b = agg[s], agg[d]
        if a != b:
            row = best.setdefault((a, b), [0, t])
            row[0] += 1
            row[1] = min(row[1], t)
    for (a, b), (n, t) in sorted(best.items()):
        cols["g_src"].append(a)
        cols["g_dst"].append(b)
        cols["g_w"].append(n)
        cols["g_tier"].append(t)
    return Layout([c.name for c in order], cols)
