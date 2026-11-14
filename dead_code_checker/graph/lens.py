"""graph.lens: the data of the viewer lenses (unused / duplicates / architecture / health over ONE graph).
Stdlib only, deterministic (no clock). Every number comes from the run's own graph, verdicts and facts; the knobs are
data/lens.json (min body sizes, the module depth, the health weights and grades).

    collect(graph, res, facts, suspects=(), data=None) -> Lens        facts: {path: Facts} (a callable returning it is called here) or None
    off(reason) -> Lens                                             no lens data (the page says why; the unused lens needs none)
    blob(lens) -> bytes | None                                      the chunks that ride inside a page (b"DCL1", u32 LE header length, JSON header
                                                                    {"chunks": {name: [offset, length]}, "info": {...}}, the gzip chunks one
                                                                    after another; up to data "inline_bytes", whole chunks in priority order)
    manifest(lens) -> bytes                                         lens/lens-manifest.json.gz: {"v":1, "chunks": {name: file}, "info": {...}}
    write(out_dir, lens) -> int                                     lens/lens-<name>.json.gz of every chunk + the manifest (the `lens-` prefix: the viewer's folder picker
                                                                    reads files by name, src/manifest.json.gz must not clash); stale chunks of ours go, a foreign file never
    arch_svg(lens) -> str                                           the architecture lens as an SVG of names only (graph.publish: no code, no paths below the module)
    module_of(path, depth) / def_keys(lang, path, defs)             the module of a file; the node key of every definition (the linker's rule)

Lenses and their chunks (a chunk is gzip json, mtime 0):
  unused        today's colouring (dead / suspect / weak-alive ...): no chunk, the pack has it
  duplicates    chunks "dup.<i>": {"files": [...], "vd": [...], "groups": [{"h", "s" (body size), "n" (symbols), "m": [[file, qual, line0, line1, verdict, test], ...]}]}.
                A group = definitions (kinds of data "dup" "kinds", generated files out) whose normalised body hash (graph.extract.bodyhash, kept in the
                facts as Def.bh / bsz) is equal, with a body of at least min_size (per language) and min_group members. The groups are ranked by
                the duplicated mass s * (n - 1); at most max_groups groups of at most max_members listed members; the rest is counted
                (info.dup.omitted_groups, omitted_members), never dropped silently. Without facts (the shards could not be read) there is no duplicates lens and info.dup.state says so. Languages whose extractors keep no body hash (powershell,
                config and schema languages) have no duplicates: info.dup.langs lists the ones that have
  architecture  chunk "arch": {"mods": [{"n", "k" (nodes), "l" (layer), "c" (cycle id or -1), "x" (languages), "d" (dead share)}], "edges":
                [[from, to, weight, cross-language]], "layers", "cycles": [[module, ...]], "omitted"}. A module = the directory of a file cut to data "arch"
                "depth" segments (small ones folded into their parent). Edge a -> b: a node of a uses a node of b (data "edge_kinds"); a layer is
                the longest path from a module nobody depends on, over the condensation (strongly connected components of 2+ modules are the
                cycles, drawn together on the same layer); an edge between two languages is marked
  health        chunk "health": {"rows": [{"n", "k" (symbols), "loc", "i" (fan-in), "o" (fan-out), "dead", "dup", "cyc", "s" (score), "g" (grade)}],
                "weights", "grades"}: the score is 100 * (1 - sum(weight * penalty)), the penalties in data "health" (dead share, duplicate share,
                fan-out over fan_cap, in a cycle, symbols over size_cap); worst first; modules under min_symbols are listed, not ranked (g = "")
info (always in the blob header and in the manifest): {"state": "on" | "off", "reason", "dup": {...}, "arch": {...}, "health": {...}, "dir"}.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import posixpath
import re
import html
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from .model import F_GENERATED, F_TEST, Def, node_key

DIR = "lens"
MANIFEST = "lens-manifest.json.gz"
MAGIC = b"DCL1"
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "lens.json")
OURS = re.compile(r"^lens-(?:arch|health|dup\.[0-9]+|manifest)\.json\.gz(?:\.tmp[0-9]+)?$")
DEAD = frozenset({"dead", "dead-cluster", "suspect"})
NOT_SYMBOL = frozenset({"file", "module"})


class Lens(NamedTuple):
    chunks: Dict[str, bytes]  # name -> gzip bytes
    order: Tuple[str, ...]  # chunk names in the priority of the inline blob
    info: dict


def load_data() -> dict:
    with open(DATA, encoding="utf-8") as fh:
        return json.load(fh)


def off(reason: str) -> Lens:
    return Lens({}, (), {"state": "off", "reason": reason})


def _gz(doc) -> bytes:
    return gzip.compress(json.dumps(doc, separators=(",", ":"), sort_keys=True).encode("utf-8"), 6, mtime=0)


# ---------------------------------------------------------------- keys and modules
def def_keys(lang: str, path: str, defs: Iterable[Def]) -> List[Tuple[Def, str]]:
    """(definition, node key) of every definition of a file with a name, by the rule of graph.link._Ix.add_file: a name that is
    defined twice (same qual and arity) gets the h8 of its occurrence number."""
    ds = [d for d in defs if d.qual]
    count = Counter((d.qual, d.arity) for d in ds)
    seen: Counter = Counter()
    out = []
    for d in sorted(ds, key=lambda d: (d.line0, d.qual, d.arity, d.kind)):
        ident = (d.qual, d.arity)
        h8 = hashlib.sha1(f"{d.qual}/{d.arity}#{seen[ident]}".encode()).hexdigest()[:8] if count[ident] > 1 else ""
        seen[ident] += 1
        out.append((d, node_key(lang, path, d.qual, d.arity, h8)))
    return out


def module_of(path: str, depth: int) -> str:
    """The module of a file: its directory cut to `depth` segments (0 = the whole directory); "." for a file at the root."""
    d = posixpath.dirname(path)
    if not d:
        return "."
    segs = d.split("/")
    return "/".join(segs[:depth]) if depth > 0 else d


def _modules(paths: Iterable[str], count: Mapping[str, int], depth: int, min_nodes: int) -> Dict[str, str]:
    """path -> module; a module with fewer than min_nodes nodes is folded into its parent directory (until it is big enough or is the root)."""
    by = {p: module_of(p, depth) for p in paths}
    if min_nodes <= 1:
        return by
    size: Counter = Counter()
    for p, m in by.items():
        size[m] += count.get(p, 0)
    fold: Dict[str, str] = {}
    for m in sorted(size, key=lambda x: -x.count("/")):
        if size[m] < min_nodes and m != ".":
            parent = posixpath.dirname(m) or "."
            fold[m] = parent
            size[parent] += size[m]

    def up(m: str) -> str:
        while m in fold:
            m = fold[m]
        return m

    return {p: up(m) for p, m in by.items()}


# ---------------------------------------------------------------- graph algorithms
def scc(n: int, adj: Sequence[Iterable[int]]) -> List[int]:
    """Tarjan, iterative: the component number of every vertex 0..n-1 (components are numbered in the order they are completed)."""
    index, low, comp = [-1] * n, [0] * n, [-1] * n
    onstack, stack, counter, ncomp = [False] * n, [], 0, 0
    for root in range(n):
        if index[root] >= 0:
            continue
        work = [(root, iter(sorted(adj[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        onstack[root] = True
        while work:
            v, it = work[-1]
            for w in it:
                if index[w] < 0:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    onstack[w] = True
                    work.append((w, iter(sorted(adj[w]))))
                    break
                if onstack[w]:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    while True:
                        w = stack.pop()
                        onstack[w] = False
                        comp[w] = ncomp
                        if w == v:
                            break
                    ncomp += 1
    return comp


def layers(n: int, edges: Iterable[Tuple[int, int]]) -> Tuple[List[int], List[int], int]:
    """(layer of every vertex, component of every vertex, layer count). An edge a -> b: a depends on b. A layer is the longest path from a
    vertex that nothing depends on, over the condensation: the vertices of one cycle share a layer."""
    es = sorted(set(edges))
    adj: List[List[int]] = [[] for _ in range(n)]
    for a, b in es:
        adj[a].append(b)
    comp = scc(n, adj)
    nc = max(comp) + 1 if n else 0
    out: List[Set[int]] = [set() for _ in range(nc)]
    indeg = [0] * nc
    for a, b in es:
        ca, cb = comp[a], comp[b]
        if ca != cb and cb not in out[ca]:
            out[ca].add(cb)
            indeg[cb] += 1
    lay = [0] * nc
    ready = [c for c in range(nc) if indeg[c] == 0]
    while ready:
        c = ready.pop()
        for d in sorted(out[c]):
            lay[d] = max(lay[d], lay[c] + 1)
            indeg[d] -= 1
            if indeg[d] == 0:
                ready.append(d)
    return [lay[comp[v]] for v in range(n)], comp, (max(lay) + 1 if nc else 0)


# ---------------------------------------------------------------- duplicates
def _min_size(cfg: dict, lang: str) -> int:
    m = cfg["min_size"]
    return int(m.get(lang, m["default"]))


def dup_groups(
    facts: Mapping[str, object], keys: Set[str], flags: Mapping[str, int], cfg: dict
) -> List[List[Tuple[str, str, Def, str]]]:
    """The groups of definitions with one normalised body hash: [[(path, key, def, lang), ...], ...] (members sorted by path, line), every
    group with min_group members, every body at least the min_size of its language; the groups ranked by size * (members - 1), then by hash.
    """
    by: Dict[str, List[Tuple[str, str, Def, str]]] = defaultdict(list)
    kinds = frozenset(cfg["kinds"])
    for path in sorted(facts):
        f = facts[path]
        for d, key in def_keys(f.lang, path, f.defs):
            if not d.bh or d.kind not in kinds or key not in keys or flags.get(key, 0) & F_GENERATED:
                continue
            if d.bsz < _min_size(cfg, f.lang):
                continue
            by[d.bh].append((path, key, d, f.lang))
    groups = [sorted(g, key=lambda m: (m[0], m[2].line0, m[2].qual)) for g in by.values() if len(g) >= cfg["min_group"]]
    groups.sort(key=lambda g: (-(g[0][2].bsz * (len(g) - 1)), g[0][2].bh))
    return groups


# ---------------------------------------------------------------- health
def health_score(h: dict, dead: float, dup: float, fout: int, cyc: bool, syms: int) -> float:
    """100 * (1 - sum(weight * penalty)) with the penalties of data "health", rounded to one decimal."""
    w = h["weights"]
    pen = {
        "dead": dead,
        "dup": dup,
        "fan": min(1.0, fout / h["fan_cap"]) if h["fan_cap"] else 0.0,
        "cycle": 1.0 if cyc else 0.0,
        "size": min(1.0, syms / h["size_cap"]) if h["size_cap"] else 0.0,
    }
    return round(100.0 * max(0.0, 1.0 - sum(w.get(k, 0.0) * v for k, v in pen.items())), 1)


def grade(h: dict, score: float) -> str:
    for low, name in h["grades"]:
        if score >= low:
            return name
    return h["grades"][-1][1]


# ---------------------------------------------------------------- collect
def collect(graph, res, facts, suspects: Iterable[str] = (), data: Optional[dict] = None) -> Lens:
    """The lens chunks of a run (see the module docstring). `facts`: {path: Facts}, a callable that returns it, or None (no duplicates)."""
    cfg = data or load_data()
    vd = {**res.verdicts(), **dict.fromkeys(suspects, "suspect")}
    nodes = graph.nodes
    fmap = None
    if callable(facts):
        facts = facts()
    if facts:
        fmap = facts
    keyset = {n.key for n in nodes}
    flags = {n.key: n.flags for n in nodes}
    chunks: Dict[str, bytes] = {}
    info: dict = {"state": "on", "reason": ""}

    # modules
    per_file: Counter = Counter(n.path for n in nodes)
    a = cfg["arch"]
    mod_of = _modules(per_file.keys(), per_file, a["depth"], a["min_nodes"])
    mods = sorted(set(mod_of.values()))
    mix = {m: i for i, m in enumerate(mods)}
    by_key = {n.key: n for n in nodes}

    # duplicates
    dcfg = cfg["dup"]
    dup_of: Counter = Counter()  # module -> symbols in a duplicate group
    groups: List[list] = []
    if fmap is None:
        info["dup"] = {
            "state": "off",
            "reason": "no per-file facts in this run (the shards of the cache could not be read, or no file was extracted)",
            "groups": 0,
        }
    else:
        groups = dup_groups(fmap, keyset, flags, dcfg)
        for g in groups:
            for path, _k, _d, _l in g:
                dup_of[mod_of.get(path, ".")] += 1
        langs = sorted(
            {lang for g in groups for *_x, lang in g} | {f.lang for f in fmap.values() if any(d.bh for d in f.defs)}
        )
        shown = groups[: dcfg["max_groups"]]
        step = int(cfg["chunk_rows"] // 4) or 100
        names = []
        for ci in range(0, len(shown), step):
            part = shown[ci : ci + step]
            files: Dict[str, int] = {}
            rows = []
            for g in part:
                ms = []
                for path, key, d, _l in g[: dcfg["max_members"]]:
                    fi = files.setdefault(path, len(files))
                    v = vd.get(key, "alive")
                    ms.append([fi, d.qual, d.line0, d.line1, v, int(bool(flags.get(key, 0) & F_TEST))])
                rows.append({"h": g[0][2].bh, "s": g[0][2].bsz, "n": len(g), "m": ms})
            name = f"dup.{ci // step}"
            chunks[name] = _gz({"files": list(files), "groups": rows})
            names.append(name)
        info["dup"] = {
            "state": "on",
            "groups": len(groups),
            "members": sum(len(g) for g in groups),
            "omitted_groups": max(0, len(groups) - len(shown)),
            "omitted_members": sum(max(0, len(g) - dcfg["max_members"]) for g in shown),
            "chunks": names,
            "step": step,
            "langs": langs,
            "min_size": dcfg["min_size"],
        }

    # architecture: the module graph
    es: Counter = Counter()
    cross: Set[Tuple[int, int]] = set()
    kinds = frozenset(a["edge_kinds"])
    for e in graph.edges:
        if kinds and e.kind not in kinds:
            continue
        s, d = by_key.get(e.src), by_key.get(e.dst)
        if s is None or d is None:
            continue
        ms, md = mix[mod_of[s.path]], mix[mod_of[d.path]]
        if ms == md:
            continue
        es[(ms, md)] += 1
        if s.lang != d.lang:
            cross.add((ms, md))
    lay, comp, nl = layers(len(mods), es.keys())
    csize = Counter(comp)
    cyc_id: Dict[int, int] = {}
    for c in sorted(c for c, n in csize.items() if n > 1):
        cyc_id[c] = len(cyc_id)
    mnodes: Counter = Counter()
    mlangs: Dict[str, Set[str]] = defaultdict(set)
    mdead: Counter = Counter()
    msyms: Counter = Counter()
    mloc: Counter = Counter()
    for n in nodes:
        m = mod_of[n.path]
        mnodes[m] += 1
        mlangs[m].add(n.lang)
        if n.kind == "file":
            mloc[m] += max(0, n.line1)
        if n.kind not in NOT_SYMBOL:
            msyms[m] += 1
            mdead[m] += vd.get(n.key, "alive") in DEAD
    fin: Dict[int, Set[int]] = defaultdict(set)
    fout: Dict[int, Set[int]] = defaultdict(set)
    for x, y in es:
        fout[x].add(y)
        fin[y].add(x)
    ranked = sorted(range(len(mods)), key=lambda i: (-mnodes[mods[i]], mods[i]))
    keep = ranked[: a["max_modules"]]
    kset = set(keep)
    newix = {o: i for i, o in enumerate(sorted(keep))}
    amods = [
        {
            "n": mods[o],
            "k": mnodes[mods[o]],
            "l": lay[o],
            "c": cyc_id.get(comp[o], -1),
            "x": sorted(mlangs[mods[o]]),
            "d": round(mdead[mods[o]] / msyms[mods[o]], 3) if msyms[mods[o]] else 0.0,
        }
        for o in sorted(keep)
    ]
    aedges = [
        [newix[x], newix[y], w, int((x, y) in cross)] for (x, y), w in sorted(es.items()) if x in kset and y in kset
    ]
    omitted_edges = max(0, len(aedges) - a["max_edges"])
    if omitted_edges:
        aedges = sorted(aedges, key=lambda r: (-r[2], r[0], r[1]))[: a["max_edges"]]
    cycles = [sorted(mods[v] for v in range(len(mods)) if comp[v] == c) for c in sorted(cyc_id)]
    chunks["arch"] = _gz(
        {
            "mods": amods,
            "edges": aedges,
            "layers": nl,
            "cycles": cycles,
            "omitted": len(mods) - len(keep),
            "omitted_edges": omitted_edges,
        }
    )
    info["arch"] = {
        "modules": len(mods),
        "shown": len(keep),
        "edges": len(es),
        "layers": nl,
        "cycles": len(cycles),
        "cross_edges": len(cross),
        "depth": a["depth"],
    }

    # health
    h = cfg["health"]
    rows = []
    for i, m in enumerate(mods):
        k = msyms[m]
        dead = mdead[m] / k if k else 0.0
        dup = dup_of[m] / k if k else 0.0
        cyc = comp[i] in cyc_id
        sc = health_score(h, dead, dup, len(fout[i]), cyc, k)
        rows.append(
            {
                "n": m,
                "k": k,
                "loc": mloc[m],
                "i": len(fin[i]),
                "o": len(fout[i]),
                "dead": round(dead, 3),
                "dup": round(dup, 3),
                "cyc": int(cyc),
                "s": sc,
                "g": grade(h, sc) if k >= h["min_symbols"] else "",
            }
        )
    rows.sort(key=lambda r: (r["g"] == "", r["s"], r["n"]))
    rows = rows[: a["max_modules"]]
    chunks["health"] = _gz(
        {
            "rows": rows,
            "weights": h["weights"],
            "grades": h["grades"],
            "fan_cap": h["fan_cap"],
            "size_cap": h["size_cap"],
        }
    )
    info["health"] = {
        "modules": len(mods),
        "shown": len(rows),
        "weights": h["weights"],
        "ranked": sum(1 for r in rows if r["g"]),
    }
    order = ["arch", "health"] + [n for n in info.get("dup", {}).get("chunks", [])]
    return Lens(chunks, tuple(order), info)


# ---------------------------------------------------------------- the page blob and the run directory
def blob(lens: Lens, cap: Optional[int] = None) -> Optional[bytes]:
    """The inline blob: whole chunks in the priority order (arch, health, dup.0, dup.1, ...) while they fit `cap` (default data "inline_bytes").
    The chunks that did not fit are named in info.left (they are in the run's lens/ folder)."""
    if not lens.chunks:
        return None
    cap = int(load_data()["inline_bytes"]) if cap is None else cap
    out: Dict[str, Tuple[int, int]] = {}
    parts: List[bytes] = []
    pos, left = 0, []
    for name in lens.order:
        c = lens.chunks[name]
        if pos + len(c) > cap and out:
            left.append(name)
            continue
        out[name] = (pos, len(c))
        parts.append(c)
        pos += len(c)
    info = dict(lens.info, left=left)
    head = json.dumps(
        {"chunks": {k: list(v) for k, v in out.items()}, "info": info}, separators=(",", ":"), sort_keys=True
    ).encode()
    return MAGIC + len(head).to_bytes(4, "little") + head + b"".join(parts)


def _name(chunk: str) -> str:
    return f"lens-{chunk}.json.gz"


def manifest(lens: Lens) -> bytes:
    return _gz({"v": 1, "chunks": {k: _name(k) for k in lens.chunks}, "info": lens.info})


def _put(path: str, data: bytes) -> None:
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def write(out_dir: str, lens: Lens) -> int:
    """lens/lens-<name>.json.gz of every chunk and lens/lens-manifest.json.gz. Stale chunks of ours go, a foreign file of lens/ stays.
    Returns the number of files written."""
    if not lens.chunks:
        return 0
    d = os.path.join(out_dir, DIR)
    os.makedirs(d, exist_ok=True)
    mine = {_name(k) for k in lens.chunks} | {MANIFEST}
    for f in os.listdir(d):
        if f not in mine and OURS.match(f):
            try:
                os.unlink(os.path.join(d, f))
            except OSError:
                pass
    for k, c in lens.chunks.items():
        _put(os.path.join(d, _name(k)), c)
    _put(os.path.join(d, MANIFEST), manifest(lens))
    return len(mine)


# ---------------------------------------------------------------- the architecture lens as SVG (publication: names only)
def arch_doc(lens: Lens) -> Optional[dict]:
    c = lens.chunks.get("arch")
    return json.loads(gzip.decompress(c)) if c else None


def arch_svg(lens: Lens, max_modules: int = 80) -> Optional[str]:
    """The module dependency view as an SVG: one row per layer (the longest path from a module nobody depends on), a box per module (its
    name and node count only), cycles outlined in red, edges between languages dashed. No code, no file or symbol names. None without data.
    """
    doc = arch_doc(lens)
    if not doc or not doc["mods"]:
        return None
    mods = sorted(range(len(doc["mods"])), key=lambda i: (-doc["mods"][i]["k"], doc["mods"][i]["n"]))[:max_modules]
    keep = set(mods)
    rows: Dict[int, List[int]] = defaultdict(list)
    for i in sorted(keep, key=lambda i: (doc["mods"][i]["l"], doc["mods"][i]["n"])):
        rows[doc["mods"][i]["l"]].append(i)
    bw, bh, gx, gy, m = 150, 34, 16, 46, 16
    width = max((len(r) for r in rows.values()), default=1) * (bw + gx) + 2 * m
    pos: Dict[int, Tuple[float, float]] = {}
    for li, layer in enumerate(sorted(rows)):
        r = rows[layer]
        x0 = m + (width - 2 * m - len(r) * (bw + gx) + gx) / 2
        for j, i in enumerate(r):
            pos[i] = (x0 + j * (bw + gx), m + 24 + li * (bh + gy))
    height = m * 2 + 24 + len(rows) * (bh + gy)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" height="{height:.0f}" font-family="sans-serif" font-size="11">',
        f'<title>architecture lens: {len(doc["mods"])} modules, {doc["layers"]} layers, {len(doc["cycles"])} cycles</title>',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{m}" y="{m + 8}" font-size="12" fill="#222">architecture: {len(doc["mods"])} modules, {doc["layers"]} layers, {len(doc["cycles"])} cycle(s); dashed = between languages; red = in a cycle</text>',
    ]
    for a, b, w, x in doc["edges"]:
        if a in pos and b in pos:
            (ax, ay), (bx, by) = pos[a], pos[b]
            in_cycle = doc["mods"][a]["c"] >= 0 and doc["mods"][a]["c"] == doc["mods"][b]["c"]
            colour = "#c0392b" if in_cycle else "#7a8a99"
            dash = ' stroke-dasharray="5 3"' if x else ""
            out.append(
                f'<line x1="{ax + bw / 2:.0f}" y1="{ay + bh:.0f}" x2="{bx + bw / 2:.0f}" y2="{by:.0f}" stroke="{colour}" stroke-width="{min(3, 0.6 + w ** 0.5 / 3):.1f}"{dash} opacity="0.7"/>'
            )
    for i, (x, y) in sorted(pos.items()):
        mod = doc["mods"][i]
        cyc = mod["c"] >= 0
        out.append(
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{bw}" height="{bh}" rx="4" fill="#eef3f7" stroke="{"#c0392b" if cyc else "#5b7083"}" stroke-width="{2 if cyc else 1}"/>'
        )
        name = mod["n"] if len(mod["n"]) <= 22 else "\u2026" + mod["n"][-21:]
        out.append(f'<text x="{x + 6:.0f}" y="{y + 14:.0f}" fill="#111">{html.escape(name)}</text>')
        out.append(
            f'<text x="{x + 6:.0f}" y="{y + 28:.0f}" fill="#445">{mod["k"]} nodes, {html.escape(",".join(mod["x"]))}</text>'
        )
    out.append("</svg>")
    return "\n".join(out)
