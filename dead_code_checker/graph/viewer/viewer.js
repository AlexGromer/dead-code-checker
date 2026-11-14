"use strict";
/* dead-code-checker viewer: a Canvas2D treemap (dir > file > symbol) of the symbol graph, a full-screen graph view of the same layout
   (pan, zoom to the cursor, minimap, levels of detail, bundled edges, hit grid), an ego graph of one symbol (#g=KEY&n=N) and the
   code of the selected symbol. The data is the pack of graph/viz.py: strings[], files[], Int32 columns. Nothing is requested from
   anywhere, nothing is evaluated; names and code reach the DOM through textContent only. The logic above boot() is DOM-free
   (node can load this file and test it). */
const MAGIC = "DCV1";
const GLYPH = {
  dead: "D",
  "dead-cluster": "C",
  /* a dead claim that no S-tier index or engine corroborates (graph.verdict) */
  suspect: "S",
  "weak-alive": "W",
  "test-only": "T",
  "doc-only": "O",
  "public-unused": "P",
  unknown: "?",
  alive: "",
};
const FLAGS = [
  "test",
  "generated",
  "stub",
  "decorated",
  "abstract",
  "override",
  "protocol",
  "dynamic-zone",
  "entry-registered",
  "implicit",
];
/* the order of a level of the call tree: the worst verdict first (the same order as _WORST of graph.viz) */
const SEVERITY = [
  "dead-cluster",
  "dead",
  "suspect",
  "weak-alive",
  "unknown",
  "public-unused",
  "test-only",
  "doc-only",
  "alive",
];
const TREE_DEPTH = 6,
  TREE_WIDTH = 25;
const WORLD_W = 1600,
  WORLD_H = 1000,
  LOD_AREA = 2500,
  EDGE_CAP = 600;
/* graph view (see the section below): a directory is opened into its children when it is OPEN_DIR px wide and its children are
   FINE_PX px apart, a file into its symbols at OPEN_FILE px; a frame draws at most NODE_CAP nodes and EDGE_FRAME edges.
   Overview: the cut of the cell tree holds at most MODULE_CELLS module cells at scale 1 (MODULE_CELLS * s^2 zoomed in; the
   smallest opened directories close first), the lifted module-to-module edges are the LIFT_EDGES heaviest (plus 4 per cell) with
   a line width from the count (WIDTH_STEPS: the counts where the next width starts) and the count written on the LIFT_LABELS heaviest */
const OPEN_DIR = 150,
  OPEN_FILE = 220,
  FINE_PX = 12,
  MODULE_CELLS = 40,
  LIFT_EDGES = 80,
  LIFT_LABELS = 24,
  WIDTH_STEPS = [3, 10, 30, 100, 300],
  WIDTH_PX = [1, 1.5, 2.2, 3.2, 4.4, 6],
  GRID_CELL = 8,
  NODE_CAP = 3000,
  EDGE_FRAME = 5000,
  MOVE_EDGES = 1200,
  EGO_COL = 230,
  EGO_ROW = 28,
  EGO_MAX = 6,
  HIDDEN = "hidden: possible secret";
const TIER_COLOR = { S: "#455a64", T: "#1e88e5", N: "#fb8c00", D: "#8e24aa" },
  TIER_ALPHA = {
    S: 1,
    T: 1,
    N: 0.5,
    D: 0.4,
  }; /* a name match or a doc mention is the weakest claim: the faintest line */

/* ---------------------------------------------------------------- decoding */
function b64bytes(s) {
  const b = atob(s),
    u = new Uint8Array(b.length);
  for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i);
  return u;
}
async function inflate(u8, fmt) {
  if (typeof DecompressionStream === "undefined")
    throw Object.assign(
      new Error(
        "this browser has no DecompressionStream: regenerate the page with --plain",
      ),
      { noStream: true },
    );
  const out = new Blob([u8]).stream().pipeThrough(new DecompressionStream(fmt));
  return new Uint8Array(await new Response(out).arrayBuffer());
}
function parsePack(u8) {
  if (String.fromCharCode(u8[0], u8[1], u8[2], u8[3]) !== MAGIC)
    throw new Error("not a dead-code-checker pack");
  const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength),
    hl = dv.getUint32(4, true);
  const head = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + hl)));
  let off = u8.byteOffset + 8 + hl;
  const cols = {};
  for (const [name, n] of head.cols) {
    cols[name] = new Int32Array(u8.buffer.slice(off, off + 4 * n));
    if (cols[name].length !== n) throw notGraph("the file ends inside " + name);
    off += 4 * n;
  }
  return { head, cols };
}
/* what an opened file is, in a few words: the message of every refusal says it */
function describe(o) {
  if (Array.isArray(o)) return "a list of " + o.length;
  if (o === null || typeof o !== "object") return typeof o;
  const ks = Object.keys(o);
  return (
    "keys: " +
    (ks.slice(0, 6).join(", ") || "none") +
    (ks.length > 6 ? ", ..." : "")
  );
}
const notGraph = (got) =>
  new Error(
    "not a dead-code-checker graph: open graph.json.gz from a run directory (got: " +
      got +
      ")",
  );
/* what the model and the renderers read of every node, edge and root; the v_ (evidence) and c_ (cut cells) columns are optional */
const COLS = [
  ...[
    "file",
    "name",
    "sfx",
    "kind",
    "lang",
    "l0",
    "l1",
    "vis",
    "flags",
    "vd",
    "par",
    "pare",
    "rt",
    "mark",
    "why",
  ].map((k) => "n_" + k),
  ...["src", "dst", "kd", "site", "ev"].map((k) => "e_" + k),
  ...["node", "kind", "origin", "str", "trust", "stub", "sel"].map(
    (k) => "r_" + k,
  ),
];
const TABLES = ["verdicts", "kinds", "vis", "edge_kinds", "tiers", "trusts"];
/* the head, the tables and the columns the model reads are there and the columns of one table are as long as its first one */
function checkPack(p) {
  const { head, cols } = p;
  for (const k of ["strings", "files", "langs"])
    if (!Array.isArray(head[k])) throw notGraph("head has no " + k);
  if (!head.tables) throw notGraph("head has no tables");
  for (const k of TABLES)
    if (!Array.isArray(head.tables[k]))
      throw notGraph("head has no tables." + k);
  for (const k of COLS) if (!cols[k]) throw notGraph("no column " + k);
  for (const [pre, lead] of [
    ["n_", "n_file"],
    ["e_", "e_src"],
    ["r_", "r_node"],
    ["v_", "v_src"],
    ["a_", "a_x"],
    ["g_", "g_src"],
    ["s_", "s_node"],
  ])
    for (const k in cols)
      if (
        k.startsWith(pre) &&
        cols[lead] &&
        cols[k].length !== cols[lead].length
      )
        throw notGraph(
          "column " +
            k +
            " has " +
            cols[k].length +
            " values, " +
            lead +
            " has " +
            cols[lead].length,
        );
  return p;
}
function fromObject(o) {
  if (
    !o ||
    typeof o !== "object" ||
    !o.head ||
    typeof o.head !== "object" ||
    !o.cols ||
    !o.cols.n_file
  )
    throw notGraph(describe(o));
  const cols = {};
  for (const k in o.cols) {
    if (!Array.isArray(o.cols[k]))
      throw notGraph("column " + k + " is no list");
    cols[k] = Int32Array.from(o.cols[k]);
  }
  return checkPack({ head: o.head, cols });
}
async function readPack(u8) {
  if (u8[0] === 0x1f && u8[1] === 0x8b)
    try {
      u8 = await inflate(u8, "gzip");
    } catch (e) {
      throw e.noStream
        ? e
        : notGraph("a damaged gzip: " + (e.message || e.name));
    }
  if (String.fromCharCode(u8[0], u8[1], u8[2], u8[3]) === MAGIC)
    try {
      return checkPack(parsePack(u8));
    } catch (e) {
      throw /^not a dead-code-checker graph/.test(e.message)
        ? e
        : notGraph("a damaged pack: " + e.message);
    }
  let o;
  try {
    o = JSON.parse(new TextDecoder().decode(u8));
  } catch (e) {
    throw notGraph("not JSON and not a dead-code-checker pack");
  }
  return fromObject(o);
}

/* ---------------------------------------------------------------- model */
function csr(n, key) {
  const off = new Int32Array(n + 1);
  for (let j = 0; j < key.length; j++) off[key[j] + 1]++;
  for (let i = 0; i < n; i++) off[i + 1] += off[i];
  const pos = off.slice(0, n),
    ids = new Int32Array(key.length);
  for (let j = 0; j < key.length; j++) ids[pos[key[j]]++] = j;
  return { off, ids };
}
function buildModel(pack) {
  const c = pack.cols,
    head = pack.head,
    N = c.n_file.length;
  const m = {
    head,
    c,
    N,
    E: c.e_src.length,
    S: head.strings,
    F: head.files,
    T: head.tables,
    meta: head.meta,
  };
  m.fwd = csr(N, c.e_src);
  m.rev = csr(N, c.e_dst);
  m.snip = new Map();
  if (c.s_node)
    for (let j = 0; j < c.s_node.length; j++)
      m.snip.set(c.s_node[j], m.S[c.s_str[j]]);
  m.rootOf = new Int32Array(N).fill(-1);
  for (let r = 0; r < c.r_node.length; r++)
    if (c.r_node[r] >= 0 && m.rootOf[c.r_node[r]] < 0)
      m.rootOf[c.r_node[r]] = r;
  return m;
}
const nName = (m, i) => m.S[m.c.n_name[i]];
const nPath = (m, i) => m.F[m.c.n_file[i]];
const nVerdict = (m, i) => m.T.verdicts[m.c.n_vd[i]];
const escPath = (p) => p.replace(/%/g, "%25").replace(/#/g, "%23");
const nKey = (m, i) =>
  m.head.langs[m.c.n_lang[i]] + ":" + escPath(nPath(m, i)) + m.S[m.c.n_sfx[i]];
function edgeInfo(m, e) {
  const c = m.c,
    kd = c.e_kd[e];
  return {
    src: nKey(m, c.e_src[e]),
    dst: nKey(m, c.e_dst[e]),
    kind: m.T.edge_kinds[kd >> 3],
    tier: m.T.tiers[kd & 7],
    site: m.S[c.e_site[e]],
  };
}
function rootInfo(m, r) {
  const c = m.c;
  return {
    node: c.r_node[r] >= 0 ? nKey(m, c.r_node[r]) : m.S[c.r_sel[r]],
    kind: m.S[c.r_kind[r]],
    origin: m.S[c.r_origin[r]],
    strength: c.r_str[r] ? "soft" : "hard",
    trust: m.T.trusts[c.r_trust[r]],
    stub: m.S[c.r_stub[r]],
  };
}
/* why-alive: the par[] chain of the reach pass, root first; null when no root reaches the node */
function whyChain(m, v) {
  const c = m.c;
  if (c.n_rt[v] < 0) return null;
  const ids = [];
  for (let u = v; c.n_par[u] >= 0; u = c.n_par[u]) ids.push(c.n_pare[u]);
  ids.reverse();
  return {
    root: rootInfo(m, c.n_rt[v]),
    steps: ids.map((e) => edgeInfo(m, e)),
  };
}

/* ---------------------------------------------------------------- call tree (lazy, over the CSR; DOM-free)
   dir "up": callers (m.rev, the other end is e_src); "down": callees (m.fwd, e_dst). A row is {t, i, e, d, parent, mark, n,
   all, kids, open}: node i reached by edge e at depth d; mark "cycle" = i is an ancestor of itself, "seen" = i is already
   shown in this tree, "limit" = more edges but the depth limit stops here; such rows do not expand. n = edges that pass the
   tier filter; all = those edges sorted (worst verdict, name, key, edge), made when the row is first opened; kids = the rows
   made so far (the first t.width, the rest on "more"). */
const treeCol = (m, dir) => (dir === "up" ? m.c.e_src : m.c.e_dst);
const treeCsr = (m, dir) => (dir === "up" ? m.rev : m.fwd);
function treeEdges(m, st, i, dir) {
  const g = treeCsr(m, dir),
    out = [];
  for (let j = g.off[i]; j < g.off[i + 1]; j++)
    if (st.tier[m.c.e_kd[g.ids[j]] & 7]) out.push(g.ids[j]);
  return out;
}
function treeSort(m, es, dir) {
  const col = treeCol(m, dir),
    rank = (e) => {
      const k = SEVERITY.indexOf(nVerdict(m, col[e]));
      return k < 0 ? SEVERITY.length : k;
    },
    cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  return es
    .map((e) => [rank(e), nName(m, col[e]), e])
    .sort(
      (a, b) =>
        a[0] - b[0] ||
        cmp(a[1], b[1]) ||
        cmp(nKey(m, col[a[2]]), nKey(m, col[b[2]])) ||
        a[2] - b[2],
    )
    .map((x) => x[2]);
}
function treeRow(m, st, t, i, e, parent) {
  const d = parent ? parent.d + 1 : 0,
    n = treeEdges(m, st, i, t.dir).length;
  let mark = "";
  for (let p = parent; p; p = p.parent) if (p.i === i) mark = "cycle";
  if (!mark && t.seen.has(i)) mark = "seen";
  if (!mark && n && d >= t.depth) mark = "limit";
  t.seen.add(i);
  return { t, i, e, d, parent, mark, n, all: null, kids: [], open: false };
}
function treeNew(m, st, i, dir, depth = TREE_DEPTH, width = TREE_WIDTH) {
  const t = { dir, depth, width, seen: new Set(), cur: null };
  t.root = treeRow(m, st, t, i, -1, null);
  return t;
}
/* make the rows of nd up to `upto` (all of them: Infinity); returns how many edges are still unshown */
function treeGrow(m, st, nd, upto) {
  if (!nd.all) nd.all = treeSort(m, treeEdges(m, st, nd.i, nd.t.dir), nd.t.dir);
  const col = treeCol(m, nd.t.dir);
  while (nd.kids.length < Math.min(upto, nd.all.length)) {
    const e = nd.all[nd.kids.length];
    nd.kids.push(treeRow(m, st, nd.t, col[e], e, nd));
  }
  return nd.all.length - nd.kids.length;
}
const treeOpen = (m, st, nd) => {
  if (nd.mark || !nd.n) return false;
  treeGrow(m, st, nd, nd.kids.length || nd.t.width);
  return (nd.open = true);
};
const treeMoreItem = (nd) => nd.moreItem || (nd.moreItem = { more: nd });
/* the rows on screen in order, with the "more" item of a level that is cut */
function treeVisible(nd, out = []) {
  for (const k of nd.kids) {
    out.push(k);
    if (k.open) treeVisible(k, out);
  }
  if (nd.all && nd.kids.length < nd.all.length) out.push(treeMoreItem(nd));
  return out;
}

/* ---------------------------------------------------------------- tree and layout (deterministic, no physics) */
function buildTree(m) {
  const c = m.c,
    root = {
      name: "",
      path: "",
      isDir: true,
      dirs: new Map(),
      files: new Map(),
      items: [],
      parent: null,
    };
  const dirOf = (p) => {
    let d = root;
    if (p && p !== ".")
      for (const seg of p.split("/")) {
        let n = d.dirs.get(seg);
        if (!n) {
          n = {
            name: seg,
            path: d.path ? d.path + "/" + seg : seg,
            isDir: true,
            dirs: new Map(),
            files: new Map(),
            items: [],
            parent: d,
          };
          d.dirs.set(seg, n);
        }
        d = n;
      }
    return d;
  };
  const byFile = new Map();
  m.leaf = new Array(m.N);
  for (let i = 0; i < m.N; i++) {
    const fi = c.n_file[i];
    let cell = byFile.get(fi);
    if (!cell) {
      const p = m.F[fi],
        k = p.lastIndexOf("/"),
        d = dirOf(k < 0 ? "" : p.slice(0, k));
      cell = {
        name: p.slice(k + 1),
        path: p,
        isDir: false,
        kids: [],
        parent: d,
      };
      d.files.set(p, cell);
      byFile.set(fi, cell);
    }
    const leaf = {
      node: i,
      wt: 1 + Math.min(50, Math.max(0, c.n_l1[i] - c.n_l0[i])),
    };
    cell.kids.push(leaf);
    m.leaf[i] = leaf;
  }
  for (let j = 0; j < (c.c_dir ? c.c_dir.length : 0); j++)
    dirOf(m.S[c.c_dir[j]]).items.push({
      vd: c.c_vd[j],
      n: c.c_n[j],
      wt: c.c_n[j],
    });
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0),
    order = (a, b) => b.wt - a.wt || cmp(a.name || "", b.name || "");
  const fin = (cell) => {
    if (cell.isDir)
      cell.kids = [...cell.dirs.values(), ...cell.files.values()]
        .map((k) => (fin(k), k))
        .concat(cell.items);
    cell.wt = cell.kids.reduce((a, k) => a + k.wt, 0);
    cell.kids.sort(order);
  };
  fin(root);
  layout(root, 0, 0, WORLD_W, WORLD_H);
  m.tree = root;
  return root;
}
function squarify(items, x, y, w, h) {
  const total = items.reduce((a, b) => a + b.wt, 0);
  if (!(total > 0) || w <= 0 || h <= 0) {
    for (const it of items) {
      it.x = x;
      it.y = y;
      it.w = 0;
      it.h = 0;
    }
    return;
  }
  const scale = (w * h) / total,
    worst = (sum, mn, mx, side) =>
      Math.max(
        (side * side * mx) / (sum * sum),
        (sum * sum) / (side * side * mn),
      );
  let i = 0;
  while (i < items.length) {
    const side = Math.min(w, h);
    let sum = 0,
      mn = Infinity,
      mx = 0,
      j = i,
      w0 = Infinity;
    for (; j < items.length; j++) {
      const a = items[j].wt * scale,
        s2 = sum + a,
        w1 = worst(s2, Math.min(mn, a), Math.max(mx, a), side);
      if (j > i && w1 > w0) break;
      sum = s2;
      mn = Math.min(mn, a);
      mx = Math.max(mx, a);
      w0 = w1;
    }
    if (w >= h) {
      const cw = sum / h;
      let yy = y;
      for (let k = i; k < j; k++) {
        const ih = (items[k].wt * scale) / cw;
        items[k].x = x;
        items[k].y = yy;
        items[k].w = cw;
        items[k].h = ih;
        yy += ih;
      }
      x += cw;
      w -= cw;
    } else {
      const rh = sum / w;
      let xx = x;
      for (let k = i; k < j; k++) {
        const iw = (items[k].wt * scale) / rh;
        items[k].x = xx;
        items[k].y = y;
        items[k].w = iw;
        items[k].h = rh;
        xx += iw;
      }
      y += rh;
      h -= rh;
    }
    i = j;
  }
}
function layout(cell, x, y, w, h) {
  cell.x = x;
  cell.y = y;
  cell.w = w;
  cell.h = h;
  const pad = Math.min(w, h) * 0.012,
    hdr = Math.min(h * 0.1, 26);
  cell.hdr = hdr;
  squarify(
    cell.kids,
    x + pad,
    y + pad + hdr,
    Math.max(w - 2 * pad, 0),
    Math.max(h - 2 * pad - hdr, 0),
  );
  for (const k of cell.kids) if (k.kids) layout(k, k.x, k.y, k.w, k.h);
}

/* ---------------------------------------------------------------- filters */
function newState(m) {
  return {
    sel: -1,
    depth: 1,
    lang: -1,
    kind: -1,
    mod: "",
    rootk: "",
    diff: 0,
    hideTest: false,
    sort: "" /* findings list: "" = as the run gave them (severity), "conf" = the surest claim first */,
    vd: m.T.verdicts.map(() => 1),
    tier: m.T.tiers.map(() => 1),
    hits: null,
  };
}
function passes(m, st, i) {
  const c = m.c;
  if (!st.vd[c.n_vd[i]]) return false;
  if (st.lang >= 0 && c.n_lang[i] !== st.lang) return false;
  if (st.kind >= 0 && c.n_kind[i] !== st.kind) return false;
  if (st.mod && !nPath(m, i).startsWith(st.mod)) return false;
  if (st.hideTest && c.n_flags[i] & 1) return false;
  if (st.rootk && (m.rootOf[i] < 0 || m.S[c.r_kind[m.rootOf[i]]] !== st.rootk))
    return false;
  if (st.diff && !(c.n_mark[i] & st.diff)) return false;
  return true;
}
function recount(m, st, cell) {
  const cn = new Int32Array(m.T.verdicts.length);
  for (const k of cell.kids) {
    if (k.node !== undefined) {
      k.ok = passes(m, st, k.node);
      if (k.ok) cn[m.c.n_vd[k.node]]++;
    } else if (k.vd !== undefined) {
      k.ok =
        !!st.vd[k.vd] &&
        st.lang < 0 &&
        st.kind < 0 &&
        !st.mod &&
        !st.rootk &&
        !st.diff;
      if (k.ok) cn[k.vd] += k.n;
    } else {
      recount(m, st, k);
      for (let v = 0; v < cn.length; v++) cn[v] += k.counts[v];
    }
  }
  cell.counts = cn;
  cell.tot = cn.reduce((a, b) => a + b, 0);
}
function searchRegex(q) {
  const t = q.trim();
  if (!t) return null;
  const r = /^\/(.+)\/([a-z]*)$/.exec(t);
  try {
    return r
      ? new RegExp(r[1], r[2].replace(/[gy]/g, "").replace(/i/g, "") + "i")
      : new RegExp(t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i");
  } catch (e) {
    return null;
  }
}

/* ---------------------------------------------------------------- graph view (DOM-free)
   The pack carries the layout of graph.layout: the cells a_* (row 0 is the root, rows are in pre-order, so the subtree of a
   cell is the row range [r, end[r])), the position of every node n_x/n_y (fixed point, / world[2]) and its file cell n_agg, and
   the file-pair edges g_*. The positions are the centres of the treemap cells of buildTree (a test compares them). The level of
   detail depends on the zoom only: lodLevel(s) is a step of a third of an octave; frontier() cuts the cell tree at that level
   into blobs (a directory or file drawn as one marker), open files (drawn as their symbols) and frames (opened directories);
   liftEdges() lifts the file-pair edges to the blobs, with a control point towards the common ancestor (bundling). */
const hasLayout = (m) => !!(m.c.a_x && m.c.n_x && m.c.n_agg && m.c.g_src);
function graphModel(m) {
  const c = m.c,
    Q = (m.head.world && m.head.world[2]) || 16,
    C = c.a_x.length,
    G = {
      m,
      Q,
      C,
      x: new Float64Array(C),
      y: new Float64Array(C),
      w: new Float64Array(C),
      h: new Float64Array(C),
      parent: c.a_parent,
      kind: c.a_kind,
      end: new Int32Array(C),
      depth: new Int16Array(C),
      nx: new Float64Array(m.N),
      ny: new Float64Array(m.N),
    };
  for (let r = 0; r < C; r++) {
    G.x[r] = c.a_x[r] / Q;
    G.y[r] = c.a_y[r] / Q;
    G.w[r] = c.a_w[r] / Q;
    G.h[r] = c.a_h[r] / Q;
    G.end[r] = r + 1;
    G.depth[r] = c.a_parent[r] >= 0 ? G.depth[c.a_parent[r]] + 1 : 0;
  }
  const off = new Int32Array(C + 1);
  for (let r = C - 1; r > 0; r--) {
    const p = c.a_parent[r];
    if (G.end[r] > G.end[p]) G.end[p] = G.end[r];
    off[p + 1]++;
  }
  for (let r = 0; r < C; r++) off[r + 1] += off[r];
  const pos = off.slice(0, C),
    ids = new Int32Array(off[C]);
  for (let r = 1; r < C; r++) ids[pos[c.a_parent[r]]++] = r;
  G.kid = { off, ids };
  G.files = csr(C, c.n_agg);
  for (let i = 0; i < m.N; i++) {
    G.nx[i] = c.n_x[i] / Q;
    G.ny[i] = c.n_y[i] / Q;
  }
  G.grid = makeGrid(G);
  G.lod = null;
  return G;
}
/* the spatial grid: the nodes in buckets of GRID_CELL world units, CSR, so a hit test looks at a few buckets */
function makeGrid(G) {
  const wh = G.m.head.world || [WORLD_W, WORLD_H],
    gw = Math.ceil(wh[0] / GRID_CELL),
    gh = Math.ceil(wh[1] / GRID_CELL),
    key = new Int32Array(G.nx.length);
  for (let i = 0; i < key.length; i++)
    key[i] =
      Math.min(gh - 1, Math.max(0, Math.floor(G.ny[i] / GRID_CELL))) * gw +
      Math.min(gw - 1, Math.max(0, Math.floor(G.nx[i] / GRID_CELL)));
  return { gw, gh, ...csr(gw * gh, key) };
}
/* the nearest node within r world units of (wx, wy) that ok() accepts (ties: the lower index), or -1 */
function gridPick(G, wx, wy, r, ok) {
  const g = G.grid;
  r = Math.min(r, 64);
  const x0 = Math.max(0, Math.floor((wx - r) / GRID_CELL)),
    x1 = Math.min(g.gw - 1, Math.floor((wx + r) / GRID_CELL)),
    y0 = Math.max(0, Math.floor((wy - r) / GRID_CELL)),
    y1 = Math.min(g.gh - 1, Math.floor((wy + r) / GRID_CELL));
  let best = -1,
    bd = r * r;
  for (let gy = y0; gy <= y1; gy++)
    for (let gx = x0; gx <= x1; gx++) {
      const k = gy * g.gw + gx;
      for (let j = g.off[k]; j < g.off[k + 1]; j++) {
        const i = g.ids[j],
          dx = G.nx[i] - wx,
          dy = G.ny[i] - wy,
          d = dx * dx + dy * dy;
        if (d <= bd && (best < 0 || d < bd || i < best) && (!ok || ok(i))) {
          best = i;
          bd = d;
        }
      }
    }
  return best;
}
const lodLevel = (s) => Math.round(Math.log2(s) * 3);
const lodScale = (l) => Math.pow(2, l / 3);
function frontier(G, level) {
  const s = lodScale(level),
    C = G.C,
    rep = new Int32Array(C).fill(-1),
    isFrame = new Uint8Array(C),
    isOpen = new Uint8Array(C),
    blobs = [],
    opens = [],
    frames = [],
    cand = [],
    stack = [0];
  /* 1. the cut by size: a directory opens when it is wide enough and its children are far enough apart */
  while (stack.length && C) {
    const c = stack.pop(),
      k0 = G.kid.off[c],
      k1 = G.kid.off[c + 1];
    if (
      G.kind[c] === 0 &&
      k1 > k0 &&
      (c === 0 ||
        (Math.min(G.w[c], G.h[c]) * s >= OPEN_DIR &&
          Math.sqrt((G.w[c] * G.h[c]) / (k1 - k0)) * s >= FINE_PX))
    ) {
      isFrame[c] = 1;
      if (c) cand.push(c);
      for (let j = k1 - 1; j >= k0; j--) stack.push(G.kid.ids[j]);
    }
  }
  /* 2. the module budget: close the smallest opened directories (children first: a child is never larger than its parent)
     until the cut has at most MODULE_CELLS * s^2 cells (the root stays open) */
  const cap = Math.max(MODULE_CELLS, Math.round(MODULE_CELLS * s * s)),
    kids = (c) => {
      let n = 0;
      for (let j = G.kid.off[c]; j < G.kid.off[c + 1]; j++)
        if (!isFrame[G.kid.ids[j]]) n++;
      return n;
    };
  let cells = 0;
  for (let c = 0; c < C; c++) if (isFrame[c]) cells += kids(c);
  if (cells > cap) {
    cand.sort(
      (a, b) =>
        G.w[a] * G.h[a] - G.w[b] * G.h[b] || G.depth[b] - G.depth[a] || a - b,
    );
    for (const c of cand) {
      if (cells <= cap) break;
      cells += 1 - kids(c);
      isFrame[c] = 0;
    }
  }
  /* 3. the cut: frames (still open), blobs (a cell drawn whole), opens (a file drawn as its symbols) */
  stack.push(0);
  while (stack.length && C) {
    const c = stack.pop();
    if (isFrame[c]) {
      frames.push(c);
      for (let j = G.kid.off[c + 1] - 1; j >= G.kid.off[c]; j--)
        stack.push(G.kid.ids[j]);
    } else {
      const open = G.kind[c] === 1 && Math.min(G.w[c], G.h[c]) * s >= OPEN_FILE;
      (open ? opens : blobs).push(c);
      if (open) isOpen[c] = 1;
      rep.fill(c, c, G.end[c]);
    }
  }
  const big = (a, b) => G.w[b] * G.h[b] - G.w[a] * G.h[a] || a - b;
  blobs.sort(big);
  opens.sort(big);
  return { level, s, rep, isFrame, isOpen, blobs, opens, frames };
}
function lcaOf(G, a, b) {
  while (G.depth[a] > G.depth[b]) a = G.parent[a];
  while (G.depth[b] > G.depth[a]) b = G.parent[b];
  while (a !== b && a >= 0) {
    a = G.parent[a];
    b = G.parent[b];
  }
  return a;
}
/* the file-pair edges lifted to the cells of a frontier: [{a, b, w, t, cx, cy}] heaviest first (cx, cy: the bundling control point) */
function liftEdges(G, fr) {
  const c = G.m.c,
    acc = new Map();
  for (let j = 0; j < c.g_src.length; j++) {
    const a = fr.rep[c.g_src[j]],
      b = fr.rep[c.g_dst[j]];
    if (a === b || a < 0 || b < 0) continue;
    const k = a * G.C + b,
      o = acc.get(k);
    if (o) {
      o.w += c.g_w[j];
      if (c.g_tier[j] < o.t) o.t = c.g_tier[j];
    } else acc.set(k, { a, b, w: c.g_w[j], t: c.g_tier[j], cx: 0, cy: 0 });
  }
  const out = [...acc.values()].sort(
    (p, q) => q.w - p.w || p.a - q.a || p.b - q.b,
  );
  for (const e of out) {
    const l = lcaOf(G, e.a, e.b),
      mx = (G.x[e.a] + G.w[e.a] / 2 + G.x[e.b] + G.w[e.b] / 2) / 2,
      my = (G.y[e.a] + G.h[e.a] / 2 + G.y[e.b] + G.h[e.b] / 2) / 2;
    e.cx = l >= 0 ? mx + 0.5 * (G.x[l] + G.w[l] / 2 - mx) : mx;
    e.cy = l >= 0 ? my + 0.5 * (G.y[l] + G.h[l] / 2 - my) : my;
  }
  return out;
}
/* the lod of scale s: computed once per level and kept */
function lodAt(G, s) {
  const level = lodLevel(s);
  if (!G.lod || G.lod.level !== level) {
    const fr = frontier(G, level);
    fr.edges = liftEdges(G, fr);
    G.lod = fr;
  }
  return G.lod;
}
/* the deepest cell of the frontier that holds the world point, or -1 */
function cellAt(G, fr, wx, wy) {
  let c = 0;
  if (
    !G.C ||
    wx < G.x[0] ||
    wy < G.y[0] ||
    wx > G.x[0] + G.w[0] ||
    wy > G.y[0] + G.h[0]
  )
    return -1;
  while (fr.isFrame[c]) {
    let nxt = -1;
    for (let j = G.kid.off[c]; j < G.kid.off[c + 1]; j++) {
      const k = G.kid.ids[j];
      if (
        wx >= G.x[k] &&
        wx <= G.x[k] + G.w[k] &&
        wy >= G.y[k] &&
        wy <= G.y[k] + G.h[k]
      ) {
        nxt = k;
        break;
      }
    }
    if (nxt < 0) return c;
    c = nxt;
  }
  return c;
}
/* the view {s, x, y} that centres node i with its file opened into symbols */
function jumpView(G, i, CW, CH) {
  const f = G.m.c.n_agg[i],
    fw = Math.max(G.w[f], 1e-6),
    fh = Math.max(G.h[f], 1e-6),
    need = (OPEN_FILE * 1.3) / Math.min(fw, fh),
    s = Math.max(need, Math.min(CW / (fw * 1.6), CH / (fh * 1.6), 400));
  return { s, x: CW / 2 - G.nx[i] * s, y: CH / 2 - G.ny[i] * s };
}
/* the hit among several: the name that is the query, then one that starts with it, then the first */
function bestHit(m, hits, q) {
  const t = q.trim().toLowerCase();
  let pre = -1,
    first = -1;
  for (const i of hits) {
    const n = nName(m, i).toLowerCase();
    if (n === t) return i;
    if (pre < 0 && n.startsWith(t)) pre = i;
    if (first < 0 || i < first) first = i;
  }
  return pre >= 0 ? pre : first;
}
/* the nodes around i: depth levels of callers (up, level -1, -2 ...) and of callees (down); edges = [edge, 1 when it leaves the
   node of the previous level]; cap = the edges taken per direction */
function neighbourhood(m, st, i, depth, cap = EDGE_CAP) {
  const c = m.c,
    nodes = new Set([i]),
    lv = new Map([[i, 0]]),
    edges = [];
  for (const [g, out] of [
    [m.rev, 0],
    [m.fwd, 1],
  ]) {
    let front = [i],
      taken = 0;
    const seen = new Set([i]);
    for (let d = 0; d < depth && taken < cap; d++) {
      const nxt = [];
      for (const v of front)
        for (let j = g.off[v]; j < g.off[v + 1]; j++) {
          const e = g.ids[j];
          if (!st.tier[c.e_kd[e] & 7]) continue;
          const u = out ? c.e_dst[e] : c.e_src[e];
          if (taken < cap) {
            edges.push([e, out]);
            taken++;
          }
          if (!seen.has(u)) {
            seen.add(u);
            nodes.add(u);
            if (!lv.has(u)) lv.set(u, out ? d + 1 : -(d + 1));
            nxt.push(u);
          }
        }
      front = nxt;
    }
  }
  return { nodes, edges, lv };
}
const sevRank = (m, i) => {
  const k = SEVERITY.indexOf(nVerdict(m, i));
  return k < 0 ? SEVERITY.length : k;
};
/* the ego graph: columns by level (callers left, callees right), a column ordered like a level of the call tree. Positions are
   the box centres; nodes the same as neighbourhood() */
function egoGraph(m, st, i, n) {
  const nb = neighbourhood(
      m,
      st,
      i,
      Math.max(1, Math.min(EGO_MAX, n)),
      EDGE_CAP,
    ),
    cols = new Map();
  for (const [u, l] of nb.lv) {
    if (!nb.nodes.has(u)) continue;
    if (!cols.has(l)) cols.set(l, []);
    cols.get(l).push(u);
  }
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0),
    at = new Map(),
    order = [],
    px = [],
    py = [];
  for (const l of [...cols.keys()].sort((a, b) => a - b)) {
    const col = cols
      .get(l)
      .sort(
        (a, b) =>
          sevRank(m, a) - sevRank(m, b) ||
          cmp(nName(m, a), nName(m, b)) ||
          cmp(nKey(m, a), nKey(m, b)),
      );
    col.forEach((u, r) => {
      at.set(u, order.length);
      order.push(u);
      px.push(l * EGO_COL);
      py.push((r - (col.length - 1) / 2) * EGO_ROW);
    });
  }
  const b = [Infinity, Infinity, -Infinity, -Infinity];
  for (let k = 0; k < order.length; k++) {
    b[0] = Math.min(b[0], px[k]);
    b[1] = Math.min(b[1], py[k]);
    b[2] = Math.max(b[2], px[k]);
    b[3] = Math.max(b[3], py[k]);
  }
  return {
    center: i,
    n,
    nodes: order,
    at,
    x: px,
    y: py,
    edges: nb.edges,
    bounds: b,
    lv: nb.lv,
  };
}
/* #v=graph&k=KEY&q=TEXT&d=DIR   or   #g=KEY&n=N : the parts of a route, decoded */
function parseRoute(hash) {
  const r = {};
  for (const part of hash.replace(/^#/, "").split("&")) {
    const k = part.indexOf("=");
    if (k < 1) continue;
    let v = part.slice(k + 1);
    try {
      v = decodeURIComponent(v);
    } catch (e) {
      /* a lone % stays as it is */
    }
    r[part.slice(0, k)] = v;
  }
  return r;
}
const snippetOf = (m, i) => (m.snip.has(i) ? m.snip.get(i) : null);

/* ---------------------------------------------------------------- the page */
/* ---------------------------------------------------------------- lenses (DOM-free; the data is written by graph.lens)
   Four lenses over ONE run: unused (the colouring of the graph, no chunk), duplicates, architecture, health. The chunks ride in the page as
   one blob (DCL1: u32 header length, JSON header {chunks: {name: [offset, length]}, info}, gzip json one after another) and are inflated when
   the lens is first shown; what did not fit is in the lens/ folder of the run, which the folder button picks once (the page cannot fetch:
   CSP default-src 'none'). Nothing here evaluates data; names reach the DOM through textContent only. */
const LENS_MAGIC = "DCL1",
  LENS_MANIFEST = "lens-manifest.json.gz",
  LENSES = ["unused", "dup", "arch", "health"],
  LENS_TITLE = {
    unused: "unused",
    dup: "duplicates",
    arch: "architecture",
    health: "health",
  },
  ARCH_BOX = { w: 132, h: 40, gx: 14, gy: 40, m: 16, top: 26, left: 44 },
  ARCH_EDGES = 900,
  DUP_PAGE = 100,
  HEALTH_GRADE = {
    good: ["#7fb685", "#1a1a1a"],
    fair: ["#e9de5b", "#1a1a1a"],
    poor: ["#ee7a2a", "#1a1a1a"],
    bad: ["#cf3a2f", "#ffffff"],
    "": ["#bdbdbd", "#1a1a1a"],
  },
  HEALTH_COLS = [
    ["n", "module"],
    ["g", "grade"],
    ["s", "score"],
    ["k", "symbols"],
    ["loc", "lines"],
    ["i", "fan-in"],
    ["o", "fan-out"],
    ["dead", "dead"],
    ["dup", "dup"],
    ["cyc", "cycle"],
  ];
function parseLensBlob(u8) {
  if (
    !u8 ||
    u8.length < 8 ||
    String.fromCharCode(u8[0], u8[1], u8[2], u8[3]) !== LENS_MAGIC
  )
    throw new Error("not a lens blob");
  const hl = new DataView(u8.buffer, u8.byteOffset, u8.byteLength).getUint32(
      4,
      true,
    ),
    base = 8 + hl,
    chunks = new Map();
  if (base > u8.length) throw new Error("a damaged lens blob");
  const head = JSON.parse(new TextDecoder().decode(u8.subarray(8, base)));
  for (const [name, [off, len]] of Object.entries(head.chunks || {})) {
    if (base + off + len > u8.length) throw new Error("a damaged lens blob");
    chunks.set(name, [base + off, len]);
  }
  return { data: u8, chunks, info: head.info || null };
}
/* the store of one page: the blob (or nothing), the info of the run (the blob header or the page meta), the files that the folder button picked */
const lensStore = (blob, meta) => ({
  packed: blob || null,
  inline: blob ? blob.chunks : new Map(),
  picked: new Map(),
  names: null,
  info: blob ? blob.info : meta || null,
  cache: new Map(),
});
async function lensPick(L, files) {
  for (const f of files) L.picked.set(f.name, f);
  const f = L.picked.get(LENS_MANIFEST);
  if (f) {
    const doc = JSON.parse(
      new TextDecoder().decode(
        await inflate(new Uint8Array(await f.arrayBuffer()), "gzip"),
      ),
    );
    L.names = new Map(Object.entries(doc.chunks || {}));
    if (doc.info) L.info = doc.info;
    L.cache.clear();
  }
  return L.picked.size;
}
/* inline (in the page), picked (in the chosen folder), pick (in the folder that is not chosen yet), absent (the run has no such chunk) */
function lensWhere(L, name) {
  if (L.inline.has(name)) return "inline";
  if (!L.names) return L.info && L.info.state === "on" ? "pick" : "absent";
  const n = L.names.get(name);
  return n === undefined ? "absent" : L.picked.has(n) ? "picked" : "pick";
}
function lensChunk(L, name) {
  const w = lensWhere(L, name);
  if (w !== "inline" && w !== "picked") return Promise.resolve(null);
  if (!L.cache.has(name))
    L.cache.set(
      name,
      (async () => {
        let u8;
        if (w === "inline") {
          const [o, n] = L.inline.get(name);
          u8 = L.packed.data.subarray(o, o + n);
        } else
          u8 = new Uint8Array(
            await L.picked.get(L.names.get(name)).arrayBuffer(),
          );
        const doc = JSON.parse(
          new TextDecoder().decode(await inflate(u8, "gzip")),
        );
        if (!doc || typeof doc !== "object")
          throw new Error("a damaged lens chunk");
        return doc;
      })(),
    );
  return L.cache.get(name);
}
/* can the lens be shown: {ok, why} (the unused lens always can) */
function lensGate(L, lens) {
  if (lens === "unused") return { ok: true, why: "" };
  const info = L && L.info;
  if (!info) return { ok: false, why: "this page carries no lens data" };
  if (info.state === "off")
    return { ok: false, why: info.reason || "lens data is off in this run" };
  if (lens === "dup" && info.dup && info.dup.state === "off")
    return { ok: false, why: info.dup.reason || "no duplicates in this run" };
  return { ok: true, why: "" };
}
/* the chunks of a lens, in order: arch -> ["arch"], dup -> info.dup.chunks */
const lensChunks = (L, lens) =>
  lens === "dup"
    ? (L.info && L.info.dup && L.info.dup.chunks) || []
    : lens === "unused"
      ? []
      : [lens];
/* the colour of a share of dead symbols on the ramp of the pack: [fill, ink] */
function rampColor(ramp, share) {
  for (const r of ramp || []) if (share <= r[0] + 1e-9) return [r[1], r[2]];
  const l = (ramp || [])[(ramp || []).length - 1];
  return l ? [l[1], l[2]] : ["#bdbdbd", "#1a1a1a"];
}
/* architecture: rows by layer (a layer wider than the room wraps into several rows), the modules of a cycle first, then the big ones */
function archLayout(doc, width) {
  const B = ARCH_BOX,
    cols = Math.max(
      1,
      Math.floor((width - B.left - B.m + B.gx) / (B.w + B.gx)),
    ),
    byLayer = new Map();
  doc.mods.forEach((md, i) => {
    if (!byLayer.has(md.l)) byLayer.set(md.l, []);
    byLayer.get(md.l).push(i);
  });
  const boxes = new Array(doc.mods.length),
    bands = [];
  let y = B.top;
  for (const layer of [...byLayer.keys()].sort((a, b) => a - b)) {
    const ids = byLayer
        .get(layer)
        .sort(
          (a, b) =>
            doc.mods[b].c - doc.mods[a].c ||
            doc.mods[b].k - doc.mods[a].k ||
            (doc.mods[a].n < doc.mods[b].n ? -1 : 1),
        ),
      rows = Math.ceil(ids.length / cols),
      y0 = y;
    ids.forEach((i, j) => {
      boxes[i] = {
        i,
        layer,
        x: B.left + (j % cols) * (B.w + B.gx),
        y: y + Math.floor(j / cols) * (B.h + B.gy / 2),
        w: B.w,
        h: B.h,
      };
    });
    y += rows * (B.h + B.gy / 2) + B.gy / 2;
    bands.push({ layer, y: y0, h: y - y0 - B.gy / 2 });
  }
  return {
    boxes,
    bands,
    width: Math.max(width, B.left + cols * (B.w + B.gx)),
    height: y + B.m,
    cols,
  };
}
/* the edges that are drawn: all of them up to `cap`, else the edges of the selected module, the edges inside a cycle and the heaviest others */
function archEdgeSet(doc, sel, cap = ARCH_EDGES) {
  const es = doc.edges;
  if (es.length <= cap) return es.map((_, j) => j);
  const keep = new Set(),
    cyc = (e) => {
      const a = doc.mods[e[0]].c;
      return a >= 0 && a === doc.mods[e[1]].c;
    };
  es.forEach((e, j) => {
    if ((sel >= 0 && (e[0] === sel || e[1] === sel)) || cyc(e)) keep.add(j);
  });
  const rest = es
    .map((e, j) => j)
    .filter((j) => !keep.has(j))
    .sort((a, b) => es[b][2] - es[a][2] || a - b);
  for (const j of rest) {
    if (keep.size >= cap) break;
    keep.add(j);
  }
  return [...keep].sort((a, b) => a - b);
}
/* the dependencies of one module: {uses, usedBy} = [{mod, w, cross}] by weight */
function archNeighbours(doc, i) {
  const out = [],
    inn = [];
  for (const [a, b, w, x] of doc.edges) {
    if (a === i) out.push({ mod: b, w, cross: !!x });
    if (b === i) inn.push({ mod: a, w, cross: !!x });
  }
  const o = (p, q) => q.w - p.w || p.mod - q.mod;
  return { uses: out.sort(o), usedBy: inn.sort(o) };
}
/* the groups of a duplicates chunk with their file table attached; members are [file, qual, line0, line1, verdict, test] */
const dupGroups = (doc) =>
  (doc.groups || []).map((g) => ({ ...g, files: doc.files }));
/* duplicates: the groups that pass the filter (a group needs two members left unless a search text names one of them) */
function dupFilter(groups, o) {
  const q = (o.q || "").toLowerCase(),
    out = [];
  for (const g of groups) {
    if (g.s < (o.minSize || 0)) continue;
    let ms = g.m;
    if (o.hideTests) ms = ms.filter((r) => !r[5]);
    if (q)
      ms = ms.filter(
        (r) =>
          r[1].toLowerCase().includes(q) ||
          g.files[r[0]].toLowerCase().includes(q),
      );
    if (ms.length < (q ? 1 : 2)) continue;
    if (
      o.onlyUnused &&
      !ms.some(
        (r) => r[4] === "dead" || r[4] === "dead-cluster" || r[4] === "suspect",
      )
    )
      continue;
    out.push({ ...g, m: ms });
  }
  return out;
}
/* health: rows sorted by a column; unranked modules (grade "") stay at the end whatever the order */
function healthSort(rows, key, dir) {
  const d = dir < 0 ? -1 : 1;
  return rows.slice().sort((a, b) => {
    if (key !== "n" && (a.g === "") !== (b.g === ""))
      return a.g === "" ? 1 : -1;
    const x = a[key],
      y = b[key],
      c = typeof x === "string" ? (x < y ? -1 : x > y ? 1 : 0) : x - y;
    return d * c || (a.n < b.n ? -1 : a.n > b.n ? 1 : 0);
  });
}
/* what a score is made of: [{name, penalty, weight, points}] in the order of the weights; points = the loss on the 100 scale */
function healthParts(row, data) {
  const W = data.weights || {},
    pen = {
      dead: row.dead,
      dup: row.dup,
      fan: data.fan_cap ? Math.min(1, row.o / data.fan_cap) : 0,
      cycle: row.cyc ? 1 : 0,
      size: data.size_cap ? Math.min(1, row.k / data.size_cap) : 0,
    };
  return Object.keys(W).map((k) => ({
    name: k,
    penalty: pen[k] || 0,
    weight: W[k],
    points: Math.round(100 * W[k] * (pen[k] || 0) * 10) / 10,
  }));
}
/* the path prefix of the module filter that selects a module of the lenses ("." = the files at the root) */
const modPrefix = (name) => (name === "." ? "" : name + "/");


function boot() {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const cv = $("cv"),
    ctx = cv.getContext("2d");
  let m = null,
    st = null,
    V = { s: 1, x: 0, y: 0 },
    CW = 1,
    CH = 1,
    dpr = 1,
    css = {},
    pal = {},
    queued = false,
    nbr = null,
    focus = null,
    hatch = null,
    G = null,
    mode = "tree",
    prevMode = "tree",
    ego = null,
    moving = false,
    moveTimer = 0,
    frameNo = 0,
    stamp = null,
    stampF = null,
    fileRoom = null,
    miniBase = null,
    keyIx = null,
    placed = false;
  const drawn = [],
    views = {},
    mini = $("mini");
  const fail = (e) => {
    const b = $("err");
    b.hidden = false;
    b.textContent = String(e && e.message ? e.message : e);
  };
  const readCss = () => {
    const s = getComputedStyle(document.documentElement);
    for (const k of ["cell", "hdr", "sel", "chg", "nd", "fg", "mut", "bg"])
      css[k] = s.getPropertyValue("--" + k).trim();
  };
  /* the view is being moved: the next frames are drawn simplified until it rests for 160 ms */
  const busy = () => {
    moving = mode === "graph";
    clearTimeout(moveTimer);
    moveTimer = setTimeout(() => {
      moving = false;
      invalidate();
    }, 160);
  };
  const invalidate = () => {
    if (!queued) {
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        draw();
      });
    }
  };

  /* ---- view transform */
  const fit = () => {
    if (mode === "ego" && ego) {
      const b = ego.bounds,
        w = b[2] - b[0] + EBW + 60,
        h = b[3] - b[1] + EBH + 60;
      const room =
        CW -
        (document.body.classList.contains("has-sel")
          ? 420
          : 0); /* the panel covers the right side */
      V.s = Math.min(room / w, CH / h, 2.5);
      V.x = room / 2 - ((b[0] + b[2]) / 2) * V.s;
      V.y = CH / 2 - ((b[1] + b[3]) / 2) * V.s;
    } else {
      V.s = Math.min(CW / WORLD_W, CH / WORLD_H);
      V.x = (CW - WORLD_W * V.s) / 2;
      V.y = (CH - WORLD_H * V.s) / 2;
    }
    invalidate();
  };
  const zoomTo = (r) => {
    placed = true;
    V.s = Math.min(CW / (r.w * 1.15), CH / (r.h * 1.15));
    V.x = CW / 2 - (r.x + r.w / 2) * V.s;
    V.y = CH / 2 - (r.y + r.h / 2) * V.s;
    invalidate();
  };
  const focusCell = (cell) => {
    focus = cell;
    cell === m.tree ? fit() : zoomTo(cell);
  };
  const resize = () => {
    const r = cv.getBoundingClientRect(),
      w = Math.max(1, r.width),
      h = Math.max(1, r.height);
    if (placed) {
      V.x += (w - CW) / 2; /* the centre of the view stays where it was */
      V.y += (h - CH) / 2;
    }
    dpr = window.devicePixelRatio || 1;
    CW = w;
    CH = h;
    cv.width = Math.round(CW * dpr);
    cv.height = Math.round(CH * dpr);
    mini.width = Math.round(200 * dpr);
    mini.height = Math.round(125 * dpr);
    if (m) {
      miniBuild();
      if (placed) invalidate();
      else fit();
    }
  };

  /* ---- drawing */
  const fill = (v) => pal[m.T.verdicts[v]] || ["#bbb", "#111"];
  function leafBox(k) {
    return [k.x * V.s + V.x, k.y * V.s + V.y, k.w * V.s, k.h * V.s];
  }
  function label(text, x, y, w, ink) {
    const n = Math.floor((w - 6) / 6.3);
    if (n < 2) return;
    ctx.fillStyle = ink;
    ctx.fillText(text.length > n ? text.slice(0, n - 1) + "…" : text, x + 3, y);
  }
  function shape(kind, x, y, r) {
    ctx.beginPath();
    if (kind === "test") {
      ctx.moveTo(x, y - r);
      ctx.lineTo(x + r, y + r);
      ctx.lineTo(x - r, y + r);
    } else if (kind === "doc-cmd") {
      ctx.rect(x - r, y - r, 2 * r, 2 * r);
    } else if (/^(bin|main)$/.test(kind)) {
      ctx.moveTo(x, y - r);
      ctx.lineTo(x + r, y);
      ctx.lineTo(x, y + r);
      ctx.lineTo(x - r, y);
    } else {
      ctx.arc(x, y, r, 0, 6.2832);
    }
    ctx.closePath();
  }
  function leaf(k) {
    const [sx, sy, sw, sh] = leafBox(k);
    if (
      sx > CW ||
      sy > CH ||
      sx + sw < 0 ||
      sy + sh < 0 ||
      sw < 0.7 ||
      sh < 0.7
    )
      return;
    const c = m.c;
    if (k.node === undefined) {
      /* omitted by truncation: a hatched cell with its count */
      ctx.globalAlpha = k.ok ? 1 : 0.15;
      ctx.fillStyle = hatch || css.hdr;
      ctx.fillRect(sx, sy, sw, sh);
      ctx.globalAlpha = 1;
      ctx.strokeStyle = fill(k.vd)[0];
      ctx.strokeRect(sx + 0.5, sy + 0.5, sw - 1, sh - 1);
      if (sw > 50 && sh > 14)
        label(
          k.n + " " + m.T.verdicts[k.vd] + " (omitted)",
          sx,
          sy + 11,
          sw,
          css.fg,
        );
      return;
    }
    const i = k.node,
      v = c.n_vd[i],
      [bg, ink] = fill(v);
    ctx.globalAlpha = k.ok ? 1 : 0.12;
    ctx.fillStyle = bg;
    ctx.fillRect(sx, sy, sw, sh);
    if (m.T.verdicts[v] === "unknown") {
      ctx.fillStyle = hatch;
      ctx.fillRect(sx, sy, sw, sh);
    }
    ctx.globalAlpha = 1;
    if (sw < 3 || sh < 3) return;
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(0,0,0,.35)";
    ctx.strokeRect(sx + 0.5, sy + 0.5, sw - 1, sh - 1);
    const mk = c.n_mark[i];
    if (mk & 1) {
      ctx.lineWidth = 2;
      ctx.strokeStyle = css.chg;
      ctx.strokeRect(sx + 1, sy + 1, sw - 2, sh - 2);
    }
    if (mk & 2) {
      ctx.lineWidth = 3;
      ctx.strokeStyle = css.nd;
      ctx.strokeRect(sx + 1.5, sy + 1.5, sw - 3, sh - 3);
    }
    const r = m.rootOf[i];
    if (r >= 0 && m.S[c.r_stub[r]]) {
      ctx.setLineDash([3, 2]);
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#c00";
      ctx.strokeRect(sx + 0.5, sy + 0.5, sw - 1, sh - 1);
      ctx.setLineDash([]);
    }
    if (st.hits && st.hits.has(i)) {
      ctx.lineWidth = 2;
      ctx.strokeStyle = css.sel;
      ctx.strokeRect(sx - 1, sy - 1, sw + 2, sh + 2);
    }
    if (i === st.sel) {
      ctx.lineWidth = 3;
      ctx.strokeStyle = css.sel;
      ctx.strokeRect(sx - 1.5, sy - 1.5, sw + 3, sh + 3);
    } else if (nbr && nbr.nodes.has(i)) {
      ctx.lineWidth = 2;
      ctx.strokeStyle = css.sel;
      ctx.strokeRect(sx, sy, sw, sh);
    }
    if (sw > 36 && sh > 13) {
      ctx.font = "11px system-ui, sans-serif";
      label(
        (GLYPH[m.T.verdicts[v]] ? "[" + GLYPH[m.T.verdicts[v]] + "] " : "") +
          (mk & 4 ? "+" : "") +
          nName(m, i),
        sx,
        sy + 11,
        sw,
        ink,
      );
    }
    if (r >= 0 && sw > 10 && sh > 10) {
      shape(m.S[c.r_kind[r]], sx + sw - 6, sy + 6, 4);
      ctx.fillStyle = "#fff";
      ctx.fill();
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = "#000";
      ctx.stroke();
    }
  }
  function bar(cell, sx, sy, sw, sh) {
    let off = 0;
    for (let v = 0; v < cell.counts.length; v++) {
      if (!cell.counts[v]) continue;
      const w = (sw * cell.counts[v]) / cell.tot;
      ctx.fillStyle = fill(v)[0];
      ctx.fillRect(sx + off, sy, Math.max(w, 0.5), sh);
      off += w;
    }
    ctx.strokeStyle = css.hdr;
    ctx.strokeRect(sx + 0.5, sy + 0.5, sw - 1, sh - 1);
  }
  function walk(cell) {
    const sx = cell.x * V.s + V.x,
      sy = cell.y * V.s + V.y,
      sw = cell.w * V.s,
      sh = cell.h * V.s;
    if (sx > CW || sy > CH || sx + sw < 0 || sy + sh < 0 || sw < 1 || sh < 1)
      return;
    if (!cell.tot) {
      ctx.globalAlpha = 0.25;
      ctx.fillStyle = css.cell;
      ctx.fillRect(sx, sy, sw, sh);
      ctx.globalAlpha = 1;
      return;
    }
    if (sw * sh < LOD_AREA)
      return bar(
        cell,
        sx,
        sy,
        sw,
        sh,
      ); /* LOD: a module is one stacked bar of its verdicts */
    ctx.fillStyle = css.cell;
    ctx.fillRect(sx, sy, sw, sh);
    ctx.strokeStyle = css.hdr;
    ctx.lineWidth = 1;
    ctx.strokeRect(sx + 0.5, sy + 0.5, sw - 1, sh - 1);
    if (cell !== m.tree && sw > 46 && cell.hdr * V.s >= 12) {
      ctx.font = "bold 11px system-ui, sans-serif";
      label(
        cell.name + (cell.isDir ? "/" : "") + "  (" + cell.tot + ")",
        sx,
        sy + 12,
        sw,
        css.fg,
      );
    }
    for (const k of cell.kids) {
      if (k.kids) walk(k);
      else leaf(k);
    }
  }
  function center(i) {
    if (mode === "graph") return [G.nx[i] * V.s + V.x, G.ny[i] * V.s + V.y];
    if (mode === "ego") {
      const k = ego.at.get(i);
      return k === undefined
        ? [-1e6, -1e6]
        : [ego.x[k] * V.s + V.x, ego.y[k] * V.s + V.y];
    }
    const k = m.leaf[i];
    return [(k.x + k.w / 2) * V.s + V.x, (k.y + k.h / 2) * V.s + V.y];
  }
  function drawEdges() {
    if (!nbr) return;
    const c = m.c,
      dash = { S: [], T: [], N: [6, 3], D: [2, 3] };
    for (const [e, out] of nbr.edges) {
      const [ax, ay] = center(c.e_src[e]),
        [bx, by] = center(c.e_dst[e]);
      ctx.strokeStyle = out ? "#c2185b" : "#1976d2";
      ctx.fillStyle = ctx.strokeStyle;
      ctx.lineWidth = 1.5;
      ctx.setLineDash(dash[m.T.tiers[c.e_kd[e] & 7]]);
      ctx.beginPath();
      ctx.moveTo(ax, ay);
      ctx.lineTo(bx, by);
      ctx.stroke();
      ctx.setLineDash([]);
      const a = Math.atan2(by - ay, bx - ax);
      ctx.beginPath();
      ctx.moveTo(bx, by);
      ctx.lineTo(bx - 9 * Math.cos(a - 0.4), by - 9 * Math.sin(a - 0.4));
      ctx.lineTo(bx - 9 * Math.cos(a + 0.4), by - 9 * Math.sin(a + 0.4));
      ctx.closePath();
      ctx.fill();
    }
  }
  function draw() {
    if (!m) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = css.bg;
    ctx.fillRect(0, 0, CW, CH);
    if (mode === "graph") return drawGraph();
    if (mode === "ego") return drawEgo();
    walk(m.tree);
    drawEdges();
  }

  /* ---- graph view: pan, zoom, levels of detail, bundled edges, minimap; ego graph (see the DOM-free section above) */
  const rampOf = (a) => {
    const c = m.c,
      R = m.head.ramp || [],
      share = c.a_n[a] ? c.a_bad[a] / c.a_n[a] : 0;
    let i = 0;
    if (share > 0) for (i = 1; i < R.length - 1 && share > R[i][0] + 1e-9; i++);
    return R[i] || [0, "#bbb", "#111"];
  };
  const tierName = (t) => m.T.tiers[t];
  const dashOf = { S: [], T: [], N: [6, 3], D: [2, 3] };
  /* the width step of a count: how many of WIDTH_STEPS it reaches */
  const widthStep = (w) => WIDTH_STEPS.filter((x) => w >= x).length;
  function strokeBatches(B, alpha, widths, shift) {
    for (const [key, p] of B) {
      const t = key >> shift,
        wb = key & ((1 << shift) - 1);
      ctx.globalAlpha = alpha * (TIER_ALPHA[tierName(t)] || 1);
      ctx.strokeStyle = TIER_COLOR[tierName(t)] || "#555";
      ctx.lineWidth = widths[wb];
      ctx.setLineDash(dashOf[tierName(t)] || []);
      ctx.stroke(p);
    }
    ctx.globalAlpha = 1;
    ctx.setLineDash([]);
  }
  function dot(i, px, py, rad, labelRoom) {
    const c = m.c,
      v = c.n_vd[i],
      [bg, ink] = fill(v),
      r = m.rootOf[i],
      mk = c.n_mark[i];
    if (mk & 2) {
      ctx.lineWidth = 3;
      ctx.strokeStyle = css.nd;
      ctx.beginPath();
      ctx.arc(px, py, rad + 3, 0, 6.2832);
      ctx.stroke();
    }
    if (mk & 1) {
      ctx.lineWidth = 2;
      ctx.strokeStyle = css.chg;
      ctx.beginPath();
      ctx.arc(px, py, rad + 2, 0, 6.2832);
      ctx.stroke();
    }
    shape(r >= 0 ? m.S[c.r_kind[r]] : "", px, py, rad);
    ctx.fillStyle = bg;
    ctx.fill();
    ctx.lineWidth = r >= 0 ? 1.8 : 1;
    ctx.strokeStyle = r >= 0 ? "#000" : "rgba(0,0,0,.5)";
    ctx.stroke();
    if (st.hits && st.hits.has(i)) {
      ctx.lineWidth = 2;
      ctx.strokeStyle = css.sel;
      ctx.beginPath();
      ctx.arc(px, py, rad + 3, 0, 6.2832);
      ctx.stroke();
    }
    if (labelRoom > 0) {
      ctx.font = "11px system-ui, sans-serif";
      label(
        (GLYPH[m.T.verdicts[v]] ? "[" + GLYPH[m.T.verdicts[v]] + "] " : "") +
          nName(m, i),
        px + rad,
        py + 4,
        labelRoom,
        css.fg,
      );
    }
  }
  function drawGraph() {
    const t0 = performance.now(),
      s = V.s,
      c = m.c,
      fr = lodAt(G, s),
      vx0 = -V.x / s,
      vy0 = -V.y / s,
      vx1 = (CW - V.x) / s,
      vy1 = (CH - V.y) / s,
      vis = (k) =>
        !(
          G.x[k] > vx1 ||
          G.y[k] > vy1 ||
          G.x[k] + G.w[k] < vx0 ||
          G.y[k] + G.h[k] < vy0
        ),
      sx = (k) => G.x[k] * s + V.x,
      sy = (k) => G.y[k] * s + V.y,
      cxs = (k) => (G.x[k] + G.w[k] / 2) * s + V.x,
      cys = (k) => (G.y[k] + G.h[k] / 2) * s + V.y;
    let nodes = 0,
      edges = 0,
      files = 0;
    frameNo++;
    drawn.length = 0;
    ctx.font = "bold 11px system-ui, sans-serif";
    for (const k of fr.frames) {
      if (!vis(k)) continue;
      const w = G.w[k] * s;
      ctx.globalAlpha = 0.55;
      ctx.fillStyle = css.cell;
      ctx.fillRect(sx(k), sy(k), w, G.h[k] * s);
      ctx.globalAlpha = 1;
      ctx.strokeStyle = css.hdr;
      ctx.lineWidth = 1;
      ctx.strokeRect(sx(k) + 0.5, sy(k) + 0.5, w - 1, G.h[k] * s - 1);
      if (k && !moving && w > 70)
        label(nPart(k) + "/", sx(k), sy(k) + 12, w, css.fg);
    }
    for (const k of fr.blobs) {
      if (nodes >= NODE_CAP) break;
      if (!vis(k)) continue;
      const w = G.w[k] * s,
        h = G.h[k] * s;
      if (w < 0.8 && h < 0.8) continue;
      if (G.kind[k] === 2) {
        ctx.fillStyle = hatch || css.hdr;
        ctx.fillRect(sx(k), sy(k), w, h);
        ctx.strokeStyle = fill(Math.max(0, c.a_vd[k]))[0];
        ctx.strokeRect(
          sx(k) + 0.5,
          sy(k) + 0.5,
          Math.max(w - 1, 0),
          Math.max(h - 1, 0),
        );
      } else {
        const [, bg, ink] = rampOf(k);
        ctx.fillStyle = bg;
        ctx.fillRect(sx(k), sy(k), w, h);
        if (w > 3 && h > 3) {
          ctx.strokeStyle = "rgba(0,0,0,.35)";
          ctx.lineWidth = 1;
          ctx.strokeRect(sx(k) + 0.5, sy(k) + 0.5, w - 1, h - 1);
        }
        if (!moving && w >= 70 && h >= 14) {
          ctx.font = "11px system-ui, sans-serif";
          label(
            nPart(k) + (G.kind[k] === 0 ? "/" : "") + "  (" + c.a_n[k] + ")",
            sx(k),
            sy(k) + 11,
            w,
            ink,
          );
        }
      }
      nodes++;
    }
    for (const f of fr.opens) {
      if (!vis(f)) continue;
      const w = G.w[f] * s,
        h = G.h[f] * s,
        nn = G.files.off[f + 1] - G.files.off[f];
      if (nodes + nn > NODE_CAP) {
        const [, bg] = rampOf(f);
        ctx.fillStyle = bg;
        ctx.fillRect(sx(f), sy(f), w, h);
        nodes++;
        continue;
      }
      ctx.fillStyle = css.cell;
      ctx.fillRect(sx(f), sy(f), w, h);
      ctx.strokeStyle = css.hdr;
      ctx.lineWidth = 1;
      ctx.strokeRect(sx(f) + 0.5, sy(f) + 0.5, w - 1, h - 1);
      if (!moving) {
        ctx.font = "bold 11px system-ui, sans-serif";
        label(nPart(f), sx(f), sy(f) + 12, w, css.fg);
      }
      stampF[f] = frameNo;
      files++;
      fileRoom[f] = Math.sqrt((w * h) / Math.max(nn, 1));
      for (let j = G.files.off[f]; j < G.files.off[f + 1]; j++) {
        const i = G.files.ids[j],
          px = G.nx[i] * s + V.x,
          py = G.ny[i] * s + V.y;
        if (
          px < -9 ||
          py < -9 ||
          px > CW + 9 ||
          py > CH + 9 ||
          !passes(m, st, i)
        )
          continue;
        stamp[i] = frameNo;
        drawn.push(i);
        nodes++;
      }
    }
    const LB = new Map(),
      SB = new Map(),
      add = (B, key) => {
        let p = B.get(key);
        if (!p) B.set(key, (p = new Path2D()));
        return p;
      };
    const symCap = Math.floor(EDGE_FRAME * 0.7);
    if (!moving)
      for (const i of drawn) {
        const ax = G.nx[i] * s + V.x,
          ay = G.ny[i] * s + V.y;
        for (const [g, out] of [
          [m.fwd, 1],
          [m.rev, 0],
        ])
          for (let j = g.off[i]; j < g.off[i + 1] && edges < symCap; j++) {
            const e = g.ids[j],
              t = c.e_kd[e] & 7,
              u = out ? c.e_dst[e] : c.e_src[e];
            if (!st.tier[t] || u === i) continue;
            let bx, by;
            if (stamp[u] === frameNo) {
              if (!out) continue;
              bx = G.nx[u] * s + V.x;
              by = G.ny[u] * s + V.y;
            } else {
              const r = fr.rep[c.n_agg[u]];
              bx = cxs(r);
              by = cys(r);
            }
            const p = add(SB, t << 2);
            if (out) {
              p.moveTo(ax, ay);
              p.lineTo(bx, by);
            } else {
              p.moveTo(bx, by);
              p.lineTo(ax, ay);
            }
            edges++;
          }
      }
    /* module-to-module edges: all of them only where symbols are drawn, else the heaviest ones (an overview is a few thick
       bundles, not a thicket); the heaviest also carry their count */
    const cap = moving
        ? MOVE_EDGES
        : drawn.length
          ? EDGE_FRAME
          : Math.min(
              EDGE_FRAME,
              LIFT_EDGES + 4 * (fr.blobs.length + fr.opens.length),
            ),
      tags = [];
    let lifted = 0;
    for (const e of fr.edges) {
      if (edges >= cap) break;
      if (stampF[e.a] === frameNo || stampF[e.b] === frameNo) continue;
      const ax = G.x[e.a] + G.w[e.a] / 2,
        ay = G.y[e.a] + G.h[e.a] / 2,
        bx = G.x[e.b] + G.w[e.b] / 2,
        by = G.y[e.b] + G.h[e.b] / 2;
      if (
        Math.max(ax, bx, e.cx) < vx0 ||
        Math.min(ax, bx, e.cx) > vx1 ||
        Math.max(ay, by, e.cy) < vy0 ||
        Math.min(ay, by, e.cy) > vy1
      )
        continue;
      const p = add(LB, (e.t << 3) | widthStep(e.w));
      if (!moving && lifted < LIFT_LABELS)
        tags.push([
          e.w,
          (0.25 * ax + 0.5 * e.cx + 0.25 * bx) * s + V.x,
          (0.25 * ay + 0.5 * e.cy + 0.25 * by) * s + V.y,
        ]);
      lifted++;
      p.moveTo(ax * s + V.x, ay * s + V.y);
      if (moving) p.lineTo(bx * s + V.x, by * s + V.y);
      else
        p.quadraticCurveTo(
          e.cx * s + V.x,
          e.cy * s + V.y,
          bx * s + V.x,
          by * s + V.y,
        );
      edges++;
    }
    strokeBatches(LB, 0.5, WIDTH_PX, 3);
    strokeBatches(SB, 0.7, [1, 1, 1, 1], 2);
    if (tags.length) {
      ctx.font = "bold 10px system-ui, sans-serif";
      ctx.textAlign = "center";
      for (const [w, x, y] of tags) {
        const t = String(w),
          tw = ctx.measureText(t).width + 6;
        ctx.globalAlpha = 0.85;
        ctx.fillStyle = css.bg;
        ctx.fillRect(x - tw / 2, y - 7, tw, 13);
        ctx.globalAlpha = 1;
        ctx.fillStyle = css.fg;
        ctx.fillText(t, x, y + 3);
      }
      ctx.textAlign = "start";
    }
    for (const i of drawn) {
      const f = c.n_agg[i],
        room = fileRoom[f],
        rad = Math.max(2.5, Math.min(7, room * 0.22));
      if (moving) {
        ctx.fillStyle = fill(c.n_vd[i])[0];
        ctx.fillRect(G.nx[i] * s + V.x - 1.5, G.ny[i] * s + V.y - 1.5, 3, 3);
      } else
        dot(
          i,
          G.nx[i] * s + V.x,
          G.ny[i] * s + V.y,
          rad,
          room >= 54 ? Math.min(room, 170) : 0,
        );
    }
    drawEdges();
    if (st.sel >= 0) {
      ctx.lineWidth = 3;
      ctx.strokeStyle = css.sel;
      ctx.beginPath();
      ctx.arc(G.nx[st.sel] * s + V.x, G.ny[st.sel] * s + V.y, 10, 0, 6.2832);
      ctx.stroke();
    }
    const ms = performance.now() - t0,
      d = document.documentElement.dataset;
    d.frameMs = ms.toFixed(1);
    d.nodesDrawn = String(nodes);
    d.edgesDrawn = String(edges);
    d.lod = String(fr.level);
    d.modules = String(fr.blobs.length + fr.opens.length);
    d.symbols = String(drawn.length);
    d.liftedEdges = String(lifted);
    d.openFiles = String(files);
    $("gstat").textContent =
      nodes +
      " nodes, " +
      edges +
      " edges, " +
      ms.toFixed(0) +
      " ms" +
      (moving ? " (moving)" : "");
    drawMini();
  }
  const nPart = (k) => m.S[m.c.a_name[k]];
  /* the minimap: the file cells coloured once (miniBase), the viewport on top of it each frame */
  function miniBuild() {
    if (!G || !mini) return;
    const wh = m.head.world || [WORLD_W, WORLD_H],
      k = mini.width / wh[0];
    miniBase = document.createElement("canvas");
    miniBase.width = mini.width;
    miniBase.height = mini.height;
    const b = miniBase.getContext("2d");
    b.fillStyle = css.cell;
    b.fillRect(0, 0, mini.width, mini.height);
    for (let r = 1; r < G.C; r++) {
      if (G.kind[r] === 0) continue;
      b.fillStyle = G.kind[r] === 2 ? css.hdr : rampOf(r)[1];
      b.fillRect(
        G.x[r] * k,
        G.y[r] * k,
        Math.max(G.w[r] * k, 0.6),
        Math.max(G.h[r] * k, 0.6),
      );
    }
  }
  function drawMini() {
    if (!miniBase) return;
    const wh = m.head.world || [WORLD_W, WORLD_H],
      k = mini.width / wh[0],
      mc = mini.getContext("2d");
    mc.setTransform(1, 0, 0, 1, 0, 0);
    mc.drawImage(miniBase, 0, 0);
    mc.lineWidth = 2 * dpr;
    mc.strokeStyle = css.sel;
    mc.strokeRect(
      (-V.x / V.s) * k,
      (-V.y / V.s) * k,
      (CW / V.s) * k,
      (CH / V.s) * k,
    );
  }
  /* the ego graph: boxes in columns by level, edges as curves with arrows (up = blue, down = pink; dashes tell the tier) */
  const EBW = 170,
    EBH = 20;
  function drawEgo() {
    const s = V.s,
      c = m.c,
      B = new Map(),
      add = (key) => {
        let p = B.get(key);
        if (!p) B.set(key, (p = { p: new Path2D(), arrows: [] }));
        return p;
      };
    for (const [e, out] of ego.edges) {
      const a = ego.at.get(c.e_src[e]),
        b = ego.at.get(c.e_dst[e]);
      if (a === undefined || b === undefined || a === b) continue;
      const fwd = ego.x[a] <= ego.x[b],
        x1 = ego.x[a] + (fwd ? EBW / 2 : -EBW / 2),
        x2 = ego.x[b] + (fwd ? -EBW / 2 : EBW / 2),
        dx = Math.max(40, Math.abs(x2 - x1) / 2) * (fwd ? 1 : -1),
        o = add((out ? 1 : 0) | ((c.e_kd[e] & 7) << 1));
      o.p.moveTo(x1 * s + V.x, ego.y[a] * s + V.y);
      o.p.bezierCurveTo(
        (x1 + dx) * s + V.x,
        ego.y[a] * s + V.y,
        (x2 - dx) * s + V.x,
        ego.y[b] * s + V.y,
        x2 * s + V.x,
        ego.y[b] * s + V.y,
      );
      o.arrows.push([x2 * s + V.x, ego.y[b] * s + V.y, fwd ? 1 : -1]);
    }
    for (const [key, o] of B) {
      ctx.strokeStyle = key & 1 ? "#c2185b" : "#1976d2";
      ctx.fillStyle = ctx.strokeStyle;
      ctx.lineWidth = 1.5;
      ctx.setLineDash(dashOf[tierName(key >> 1)] || []);
      ctx.stroke(o.p);
      ctx.setLineDash([]);
      for (const [ax, ay, sg] of o.arrows) {
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(ax - 9 * sg, ay - 4);
        ctx.lineTo(ax - 9 * sg, ay + 4);
        ctx.closePath();
        ctx.fill();
      }
    }
    ctx.font = "11px system-ui, sans-serif";
    ego.nodes.forEach((i, k) => {
      const [bg, ink] = fill(c.n_vd[i]),
        x = (ego.x[k] - EBW / 2) * s + V.x,
        y = (ego.y[k] - EBH / 2) * s + V.y;
      ctx.fillStyle = bg;
      ctx.fillRect(x, y, EBW * s, EBH * s);
      ctx.lineWidth = i === ego.center ? 3 : i === st.sel ? 3 : 1;
      ctx.strokeStyle =
        i === ego.center || i === st.sel ? css.sel : "rgba(0,0,0,.45)";
      ctx.strokeRect(x, y, EBW * s, EBH * s);
      if (EBH * s >= 9)
        label(
          (GLYPH[m.T.verdicts[c.n_vd[i]]]
            ? "[" + GLYPH[m.T.verdicts[c.n_vd[i]]] + "] "
            : "") + nName(m, i),
          x,
          y + EBH * s * 0.5 + 4,
          EBW * s,
          ink,
        );
    });
    const d = document.documentElement.dataset;
    d.frameMs = "0";
    $("gstat").textContent =
      ego.nodes.length +
      " nodes, " +
      ego.edges.length +
      " edges, " +
      ego.n +
      " level(s)";
  }
  function hitGraph(px, py) {
    const s = V.s,
      wx = (px - V.x) / s,
      wy = (py - V.y) / s,
      i = gridPick(G, wx, wy, 9 / s, (u) => stamp[u] === frameNo);
    if (i >= 0) return { node: i };
    const k = cellAt(G, lodAt(G, s), wx, wy);
    return k >= 0 ? { cell: k } : null;
  }
  function hitEgo(px, py) {
    const wx = (px - V.x) / V.s,
      wy = (py - V.y) / V.s;
    for (let k = 0; k < ego.nodes.length; k++)
      if (
        Math.abs(wx - ego.x[k]) <= EBW / 2 &&
        Math.abs(wy - ego.y[k]) <= EBH / 2
      )
        return { node: ego.nodes[k] };
    return null;
  }

  /* ---- hit test: the deepest visible cell under the pointer (the same LOD rule as the drawing) */
  function hit(px, py) {
    if (mode === "graph") return hitGraph(px, py);
    if (mode === "ego") return hitEgo(px, py);
    const inside = (k) => {
      const [x, y, w, h] = leafBox(k);
      return px >= x && px <= x + w && py >= y && py <= y + h;
    };
    const go = (cell) => {
      const [, , w, h] = leafBox(cell);
      if (!cell.tot || w * h < LOD_AREA) return cell;
      for (const k of cell.kids) if (inside(k)) return k.kids ? go(k) : k;
      return cell;
    };
    return inside(m.tree) ? go(m.tree) : null;
  }

  /* ---- selection, neighbourhood, panel */
  function select(i, zoom) {
    st.sel = i;
    nbr = i >= 0 ? neighbourhood(m, st, i, st.depth) : null;
    panel(i);
    document.body.classList.toggle("has-sel", i >= 0);
    document.documentElement.dataset.sel = i >= 0 ? nKey(m, i) : "";
    if (i >= 0 && zoom) {
      if (mode === "graph") {
        placed = true;
        Object.assign(V, jumpView(G, i, CW, CH));
      } else if (mode === "tree") zoomTo(fileBox(i));
    }
    invalidate();
  }
  function fileBox(i) {
    const k = m.leaf[i];
    let r = { x: k.x, y: k.y, w: k.w, h: k.h };
    const p = nPath(m, i);
    const cell = findFile(p);
    if (cell) r = cell;
    return r;
  }
  function findFile(path) {
    let d = m.tree;
    const segs = path.split("/");
    for (let s = 0; s < segs.length - 1 && d; s++) d = d.dirs.get(segs[s]);
    return d ? d.files.get(path) : null;
  }
  function copy(text) {
    try {
      navigator.clipboard.writeText(text).catch(() => {});
    } catch (e) {
      /* no clipboard: the text stays on screen */
    }
  }
  function link(text, fn) {
    const b = el("button", "lk", text);
    b.onclick = fn;
    return b;
  }
  /* ---- call tree: rows are built from the lazy model above; Enter/Space opens, arrows move, s selects, click selects */
  const treeItem = (it) => (it.more ? it.more : it);
  function paintRow(k) {
    const c = m.c,
      v = nVerdict(m, k.i),
      li = el("li", "tl"),
      row = el("div", "trow"),
      tg = el("span", "tg", k.mark || !k.n ? "." : k.open ? "-" : "+"),
      dot = el("i", "dot"),
      col = (pal[v] || [""])[0];
    if (/^#[0-9a-f]{6}$/i.test(col)) dot.style.background = col;
    row.tabIndex = k.t.cur === k ? 0 : -1;
    row.title = m.S[c.e_site[k.e]];
    row.onkeydown = (ev) => treeKey(ev, k);
    tg.onclick = () => treeToggle(k);
    const name = link(nName(m, k.i), () => select(k.i, true));
    name.tabIndex = -1;
    for (const x of [tg, dot, name]) row.appendChild(x);
    row.appendChild(
      el(
        "span",
        "muted",
        " " +
          v +
          "  " +
          m.T.edge_kinds[c.e_kd[k.e] >> 3] +
          "/" +
          m.T.tiers[c.e_kd[k.e] & 7],
      ),
    );
    if (k.mark)
      row.appendChild(
        el(
          "span",
          "tmark",
          " " +
            { cycle: "(cycle)", seen: "(shown above)", limit: "(depth limit)" }[
              k.mark
            ],
        ),
      );
    k.row = row;
    li.appendChild(row);
    li.role = "treeitem";
    if (k.n && !k.mark) li.ariaExpanded = k.open ? "true" : "false";
    if (k.open) {
      k.ul = el("ul", "tl-l");
      li.appendChild(k.ul);
      paintLevel(k);
    }
    return li;
  }
  function paintLevel(nd) {
    nd.ul.textContent = "";
    for (const k of nd.kids) nd.ul.appendChild(paintRow(k));
    if (nd.all && nd.kids.length < nd.all.length) {
      const it = treeMoreItem(nd),
        li = el("li", "tl"),
        row = el("div", "trow");
      row.tabIndex = -1;
      row.appendChild(el("span", "tg", "+"));
      const more = link("+" + (nd.all.length - nd.kids.length) + " more", () =>
        treeMore(nd),
      );
      more.tabIndex = -1;
      row.appendChild(more);
      row.onkeydown = (ev) => treeKey(ev, it);
      it.row = row;
      li.appendChild(row);
      nd.ul.appendChild(li);
    }
  }
  function treeFocus(it) {
    const t = treeItem(it).t;
    if (!it.more) {
      if (t.cur && t.cur.row) t.cur.row.tabIndex = -1;
      t.cur = it;
      it.row.tabIndex = 0;
    }
    it.row.focus();
  }
  function treeToggle(nd) {
    if (nd.open) nd.open = false;
    else if (!treeOpen(m, st, nd)) return;
    paintLevel(nd.parent);
    treeFocus(nd);
  }
  function treeMore(nd) {
    const was = nd.kids.length;
    treeGrow(m, st, nd, Infinity);
    paintLevel(nd);
    if (nd.kids[was]) treeFocus(nd.kids[was]);
  }
  function treeKey(ev, it) {
    const vis = treeVisible(treeItem(it).t.root),
      at = vis.indexOf(it),
      k = ev.key,
      go = (x) => {
        if (x) treeFocus(x);
        ev.preventDefault();
      };
    if (k === "ArrowDown") go(vis[at + 1]);
    else if (k === "ArrowUp") go(vis[at - 1]);
    else if (k === "Home") go(vis[0]);
    else if (k === "End") go(vis[vis.length - 1]);
    else if (k === "Enter" || k === " ") {
      it.more ? treeMore(it.more) : treeToggle(it);
      ev.preventDefault();
    } else if (it.more) return;
    else if (k === "s") {
      select(it.i, true);
      ev.preventDefault();
    } else if (k === "ArrowRight") {
      if (it.open) go(it.kids[0]);
      else treeToggle(it);
      ev.preventDefault();
    } else if (k === "ArrowLeft") {
      if (it.open) treeToggle(it);
      else go(it.parent && it.parent.parent ? it.parent : null);
    }
  }
  function treeBox(i, dir, title) {
    const t = treeNew(m, st, i, dir),
      box = el("div", "tree");
    box.appendChild(el("h4", "", title + " (" + t.root.n + ")"));
    if (!t.root.n) return box;
    t.root.ul = el("ul", "tl-l");
    t.root.ul.role = "tree";
    treeOpen(m, st, t.root);
    t.cur = t.root.kids[0];
    paintLevel(t.root);
    box.appendChild(t.root.ul);
    return box;
  }
  /* the code of the symbol: the snippet of the pack (already scanned for secrets), line numbers from the symbol's first line */
  function codeBox(i) {
    const box = el("div", "code"),
      t = snippetOf(m, i),
      info = m.meta.code;
    box.appendChild(el("h4", "", "code"));
    if (t === HIDDEN) box.appendChild(el("p", "hidden", HIDDEN));
    else if (t !== null) {
      const l0 = m.c.n_l0[i],
        pre = el("pre", "src");
      pre.textContent = t
        .split("\n")
        .map((x, j) => String(l0 + j).padStart(4) + "  " + x)
        .join("\n");
      box.appendChild(
        el("div", "muted", nPath(m, i) + ":" + l0 + "-" + m.c.n_l1[i]),
      );
      box.appendChild(pre);
    } else
      box.appendChild(
        el(
          "p",
          "muted",
          !info
            ? "no code in this page (made without code snippets)"
            : info.state === "off"
              ? "no code in this page: " + (info.reason || "off")
              : "no code kept for this symbol (dead, changed and root symbols come first; budget " +
                Math.round((info.budget || 0) / 1048576) +
                " MB" +
                (info.stale
                  ? "; " +
                    info.stale +
                    " file(s) changed since the graph was built have no code"
                  : "") +
                ")",
        ),
      );
    return box;
  }
  function panel(i) {
    const P = $("panel");
    P.textContent = "";
    if (i < 0) {
      P.appendChild(el("p", "muted", "Click a symbol."));
      return;
    }
    const c = m.c,
      v = nVerdict(m, i),
      key = nKey(m, i);
    P.appendChild(el("h3", "", nName(m, i)));
    for (const t of [
      m.T.kinds[c.n_kind[i]],
      m.head.langs[c.n_lang[i]],
      (GLYPH[v] ? "[" + GLYPH[v] + "] " : "") + v,
      m.T.vis[c.n_vis[i]],
    ].concat(FLAGS.filter((f, b) => c.n_flags[i] & (1 << b))))
      P.appendChild(el("span", "chip", t));
    const loc = nPath(m, i) + ":" + c.n_l0[i];
    P.appendChild(el("div", "", "")).appendChild(
      link(loc + "  (copy)", () => copy(loc)),
    );
    P.appendChild(el("pre", "", key));
    const nav = el("div", "navrow");
    nav.appendChild(
      link("ego graph (" + st.depth + ")", () => egoRoute(i, st.depth)),
    );
    if (G && mode !== "graph")
      nav.appendChild(
        link("show in graph", () => {
          setMode("graph");
          select(i, true);
        }),
      );
    P.appendChild(nav);
    const mk = c.n_mark[i];
    if (mk)
      P.appendChild(
        el(
          "p",
          "",
          [
            mk & 1 ? "touched by the diff" : "",
            mk & 2 ? "newly dead" : "",
            mk & 4 ? "newly alive" : "",
          ]
            .filter(Boolean)
            .join(", "),
        ),
      );
    const w = whyChain(m, i);
    if (w) {
      P.appendChild(el("h4", "", "why " + v));
      const r = w.root,
        ol = el("ol", "chain");
      ol.appendChild(
        el(
          "li",
          "",
          r.kind +
            " root " +
            r.node +
            " (" +
            r.trust +
            ", " +
            r.strength +
            ") registered at " +
            r.origin +
            (r.stub ? "; STUB: " + r.stub : ""),
        ),
      );
      for (const s of w.steps)
        ol.appendChild(
          el(
            "li",
            "",
            s.src +
              " -> " +
              s.dst +
              "  [" +
              s.kind +
              "/" +
              s.tier +
              " at " +
              s.site +
              "]",
          ),
        );
      P.appendChild(ol);
    } else {
      P.appendChild(el("h4", "", "why " + v));
      P.appendChild(
        el(
          "pre",
          "",
          c.n_why[i] >= 0
            ? m.S[c.n_why[i]]
            : "no why text in this page; run: dead-code-checker graph why " + key,
        ),
      );
    }
    P.appendChild(codeBox(i));
    P.appendChild(treeBox(i, "up", "callers (who calls it, up)"));
    P.appendChild(treeBox(i, "down", "callees (what it calls, down)"));
    const ev = [];
    for (let j = m.rev.off[i]; j < m.rev.off[i + 1]; j++)
      if (c.e_ev[m.rev.ids[j]] >= 0) ev.push(c.e_ev[m.rev.ids[j]]);
    if (ev.length && c.v_src) {
      P.appendChild(el("h4", "", "evidence"));
      const ul = el("ul", "");
      for (const e of ev.slice(0, 20))
        ul.appendChild(
          el(
            "li",
            "",
            m.S[c.v_src[e]] +
              " " +
              m.S[c.v_file[e]] +
              ":" +
              c.v_line[e] +
              " " +
              m.S[c.v_snip[e]] +
              " (" +
              m.S[c.v_ext[e]] +
              ")",
          ),
        );
      P.appendChild(ul);
    }
  }

  /* ---- modes: tree (the treemap), graph (the full graph view), ego (the neighbourhood of one symbol) */
  function setMode(md) {
    if (md === mode) return;
    if (md !== "tree" && !G) {
      fail(
        new Error(
          "this pack has no graph layout (it was made by an older version): regenerate the page",
        ),
      );
      return;
    }
    views[mode] = { ...V };
    placed = false;
    if (mode !== "ego") prevMode = mode;
    mode = md;
    document.body.dataset.mode = md;
    document.documentElement.dataset.gmode = md;
    $("gbar").hidden = md === "tree";
    $("g-egol").hidden = md !== "ego";
    mini.hidden = md !== "graph";
    $("mode").textContent = md === "graph" ? "treemap" : "graph";
    if (views[md] && md !== "ego") Object.assign(V, views[md]);
    else fit();
    invalidate();
  }
  function enterEgo(i, n) {
    n = Math.max(1, Math.min(EGO_MAX, n | 0 || 1));
    ego = egoGraph(m, st, i, n);
    $("g-n").value = String(n);
    const wasEgo = mode === "ego";
    select(i, false);
    setMode("ego");
    if (wasEgo) fit();
    const d = document.documentElement.dataset;
    d.ego = nKey(m, i);
    d.egoNodes = String(ego.nodes.length);
    d.egoEdges = String(ego.edges.length);
  }
  const egoRoute = (i, n) => {
    location.hash = "g=" + encodeURIComponent(nKey(m, i)) + "&n=" + n;
  };
  function indexOfKey(k) {
    if (!keyIx) {
      keyIx = new Map();
      for (let i = 0; i < m.N; i++) keyIx.set(nKey(m, i), i);
    }
    return keyIx.has(k) ? keyIx.get(k) : -1;
  }
  /* the first (best) hit of the search box: the node is selected and, in the graph view, brought to the centre */
  function jumpFirst(again) {
    if (!st.hits || !st.hits.size) return;
    let i = bestHit(m, st.hits, $("q").value);
    if (again && st.hits.has(st.sel)) {
      const hs = [...st.hits];
      i =
        hs[
          (hs.indexOf(st.sel) + 1) % hs.length
        ]; /* Enter again: the next hit */
    }
    select(i, true);
  }

  /* ---- lists, filters, search */
  let byLine = null;
  const nodeAt = (path, line) => {
    if (!byLine) {
      byLine = new Map();
      for (let i = 0; i < m.N; i++)
        byLine.set(nPath(m, i) + ":" + m.c.n_l0[i], i);
    }
    return byLine.get(path + ":" + line);
  };
  const confBadge = (c) => "[" + c + "]";
  function showList() {
    const ul = $("list"),
      h = $("lh");
    ul.textContent = "";
    const add = (parts, fn) => {
      const li = el("li");
      li.tabIndex = 0;
      for (const [cls, t] of parts)
        li.appendChild(el(cls ? "b" : "span", cls, t));
      li.onclick = fn;
      li.onkeydown = (e) => {
        if (e.key === "Enter") fn();
      };
      ul.appendChild(li);
    };
    if (st.hits) {
      const hits = [...st.hits].slice(0, 200);
      h.textContent = "search results (" + st.hits.size + ")";
      for (const i of hits)
        add(
          [
            [
              "",
              nName(m, i) +
                "  " +
                nPath(m, i) +
                ":" +
                m.c.n_l0[i] +
                "  [" +
                nVerdict(m, i) +
                "]",
            ],
          ],
          () => select(i, true),
        );
      return;
    }
    let fs = m.head.findings || [];
    if (fs.length) {
      h.textContent = "findings (" + fs.length + ")";
      if (st.sort === "conf")
        /* a second axis, never an input of the level: claims without a number go last; the sort is stable */
        fs = fs
          .map((f, k) => [f, k])
          .sort(
            (x, y) =>
              (y[0].confidence === undefined ? -1 : y[0].confidence) -
                (x[0].confidence === undefined ? -1 : x[0].confidence) ||
              x[1] - y[1],
          )
          .map((x) => x[0]);
      for (const f of fs.slice(0, 500))
        add(
          [
            [f.level, f.level + " "],
            ...(f.confidence === undefined
              ? []
              : [["conf", confBadge(f.confidence) + " "]]),
            [
              "",
              f.code +
                " " +
                f.path +
                (f.line ? ":" + f.line : "") +
                " " +
                f.msg,
            ],
          ],
          () => {
            const i = nodeAt(f.path, f.line);
            if (i !== undefined) select(i, true);
            else {
              const cell = findFile(f.path);
              if (cell) focusCell(cell);
            }
          },
        );
      return;
    }
    const dead = [];
    for (let i = 0; i < m.N && dead.length < 300; i++) {
      const v = nVerdict(m, i);
      if (v === "dead" || v === "dead-cluster") dead.push(i);
    }
    h.textContent = "dead symbols (first " + dead.length + ")";
    for (const i of dead)
      add([["", nName(m, i) + "  " + nPath(m, i) + ":" + m.c.n_l0[i]]], () =>
        select(i, true),
      );
  }
  function refilter() {
    recount(m, st, m.tree);
    invalidate();
  }
  function search() {
    const rx = searchRegex($("q").value);
    st.hits = null;
    if (rx) {
      st.hits = new Set();
      for (let i = 0; i < m.N; i++)
        if (rx.test(nName(m, i)) || rx.test(nPath(m, i))) st.hits.add(i);
    }
    showList();
    $("ghits").textContent = st.hits ? st.hits.size + " hit(s)" : "";
    invalidate();
  }
  function opt(sel, value, text) {
    const o = el("option", "", text);
    o.value = value;
    sel.appendChild(o);
  }
  function fillControls() {
    const lang = $("f-lang"),
      kind = $("f-kind"),
      root = $("f-root");
    for (const s of [lang, kind, root]) s.textContent = "";
    opt(lang, "-1", "all");
    m.head.langs.forEach((l, i) => opt(lang, i, l));
    opt(kind, "-1", "all");
    m.T.kinds.forEach((k, i) => opt(kind, i, k));
    opt(root, "", "all");
    for (const k of [...new Set(Array.from(m.c.r_kind, (x) => m.S[x]))].sort())
      opt(root, k, k);
    for (const [box, names, key] of [
      [$("f-vd"), m.T.verdicts, "vd"],
      [$("f-tier"), m.T.tiers, "tier"],
    ]) {
      box.textContent = "";
      box.appendChild(el("legend", "", key === "vd" ? "verdict" : "edge tier"));
      names.forEach((n, i) => {
        const l = el("label"),
          c = document.createElement("input");
        c.type = "checkbox";
        c.checked = true;
        c.onchange = () => {
          st[key][i] = c.checked ? 1 : 0;
          key === "vd" ? refilter() : st.sel >= 0 && select(st.sel, false);
        };
        l.appendChild(c);
        l.appendChild(document.createTextNode(" " + n));
        box.appendChild(l);
      });
    }
    $("l-diff").hidden = !m.c.n_mark.some((x) => x);
  }
  function legend() {
    const L = $("legend");
    L.textContent = "";
    for (const v of m.T.verdicts) {
      const s = el("span"),
        i = el("i");
      const col = (pal[v] || [""])[0];
      if (/^#[0-9a-f]{6}$/i.test(col)) i.style.background = col;
      s.appendChild(i);
      s.appendChild(
        document.createTextNode((GLYPH[v] ? "[" + GLYPH[v] + "] " : "") + v),
      );
      L.appendChild(s);
    }
    if (G) {
      const sw = el("span", "", "graph tiles, share of dead + suspect: ");
      for (const r of m.head.ramp || []) {
        const i = el("i");
        if (/^#[0-9a-f]{6}$/i.test(r[1])) i.style.background = r[1];
        i.title = r[3];
        sw.appendChild(i);
      }
      L.appendChild(sw);
    }
    for (const t of [
      "diamond/triangle/square/circle: root",
      "red dashed: stub entry",
      "green outline: touched",
      "thick dark red: newly dead",
      "+: newly alive",
      "hatched: unknown / omitted",
    ])
      L.appendChild(el("span", "", t));
  }

  /* a pack from the page or from the file input replaces the model and every control that depends on it */
  function load(pack, lblob) {
    $("err").hidden = true;
    m = buildModel(pack);
    LS = lensStore(lblob || null, m.meta ? m.meta.lens : null);
    st = newState(m);
    nbr = null;
    byLine = null;
    pal = m.head.palette || {};
    buildTree(m);
    recount(m, st, m.tree);
    G = hasLayout(m) ? graphModel(m) : null;
    stamp = new Int32Array(m.N);
    stampF = new Int32Array(G ? G.C : 0);
    fileRoom = new Float32Array(G ? G.C : 0);
    frameNo = 0;
    keyIx = null;
    placed = false;
    ego = null;
    for (const k in views) delete views[k];
    mode = "tree";
    prevMode = "tree";
    lensName = "unused";
    archSel = -1;
    dupShown = DUP_PAGE;
    $("stage").hidden = false;
    $("lensp").hidden = true;
    $("png").hidden = false;
    document.body.dataset.lens = "unused";
    document.documentElement.dataset.lens = "unused";
    lensButtons();
    document.body.dataset.mode = "tree";
    document.documentElement.dataset.gmode = "tree";
    $("gbar").hidden = true;
    mini.hidden = true;
    $("mode").hidden = !G;
    $("mode").textContent = "graph";
    const mt = m.meta || {},
      cnt = mt.counts || {};
    $("summary").textContent =
      m.N +
      " of " +
      (mt.nodes_total || m.N) +
      " nodes, " +
      m.E +
      " edges; " +
      Object.keys(cnt)
        .map((k) => cnt[k] + " " + k)
        .join(", ");
    $("local").hidden = !mt.local;
    $("local").textContent = mt.local || "";
    const b = $("banner");
    b.hidden = !mt.truncated;
    if (mt.truncated)
      b.textContent =
        "truncated: " +
        m.N +
        " of " +
        mt.nodes_total +
        " nodes kept (not alive, roots, marked, " +
        (mt.hops == null ? "?" : mt.hops) +
        " hop(s) around them; omitted nodes are hatched cells); open graph.json.gz for the full graph";
    const nav = $("links");
    nav.textContent = "";
    for (const [t, h] of mt.links || [])
      if (/^[\w.-]+$/.test(h)) {
        const a = el("a", "", t);
        a.href = h;
        nav.appendChild(a);
      }
    document.title =
      (mt.scope ? "dead-code-checker " + mt.scope : "dead-code-checker") +
      (mt.mode === "change" ? ": change subgraph" : "");
    fillControls();
    legend();
    showList();
    panel(-1);
    resize();
    applyHash();
    document.documentElement.dataset.boot =
      "ok"; /* what a browser check waits for */
  }
  function applyHash() {
    const r = parseRoute(location.hash);
    if (r.g !== undefined) {
      const i = indexOfKey(r.g);
      if (i >= 0) enterEgo(i, +r.n || 1);
      else fail(new Error("no such symbol in this page: " + r.g));
      return;
    }
    if (mode === "ego") setMode(prevMode);
    if (r.lens && LENSES.includes(r.lens)) setLens(r.lens);
    if (r.v === "graph") setMode("graph");
    else if (r.v === "tree") setMode("tree");
    if (r.k !== undefined) {
      const i = indexOfKey(r.k);
      if (i >= 0) select(i, true);
    }
    if (r.q !== undefined) {
      $("q").value = $("gq").value = r.q;
      search();
      jumpFirst();
    }
    if (r.d !== undefined && mode === "tree") {
      let d = m.tree;
      if (r.d !== ".") for (const s of r.d.split("/")) d = d && d.dirs.get(s);
      if (d) focusCell(d);
    }
  }

  /* ---- lenses: unused (the graph as it was), duplicates, architecture, health (the data: graph.lens; the logic above boot()) */
  let lensName = "unused",
    LS = lensStore(null, null),
    lensSeq = 0,
    archSel = -1,
    dupShown = DUP_PAGE,
    healthKey = "s",
    healthDir = 1;
  const dupOpt = { hideTests: false, onlyUnused: false, minSize: 0, q: "" },
    SVGNS = "http://www.w3.org/2000/svg";
  const svg = (tag, attrs, text) => {
    const e = document.createElementNS(SVGNS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const pct = (x) => Math.round(x * 100) + " %";
  const inkOf = (v) => pal[v] || ["#bbb", "#111"];
  function lensButtons() {
    for (const n of LENSES) {
      const b = $("l-" + n),
        g = lensGate(LS, n);
      b.dataset.on = n === lensName ? "1" : "0";
      b.disabled = !g.ok;
      b.title = g.ok
        ? LENS_TITLE[n] + " lens"
        : LENS_TITLE[n] + " lens is off: " + g.why;
    }
  }
  /* the unused lens is the graph as it was; the other three replace the stage by their own panel */
  async function setLens(name) {
    if (!m || !LENSES.includes(name) || !lensGate(LS, name).ok) return;
    const seq = ++lensSeq;
    if (name !== "unused" && mode !== "tree") setMode("tree"); /* the lens panel replaces the stage of the treemap */
    lensName = name;
    document.body.dataset.lens = name;
    document.documentElement.dataset.lens = name;
    document.documentElement.dataset.lensReady = name === "unused" ? "1" : "";
    $("stage").hidden = name !== "unused";
    $("lensp").hidden = name === "unused";
    $("mode").hidden = name !== "unused" || !G;
    $("png").hidden = name !== "unused";
    lensButtons();
    if (name === "unused") {
      resize();
      invalidate();
      return;
    }
    $("lbar").textContent = "";
    $("lbody").textContent = "";
    $("lmsg").textContent = "loading ...";
    $("l-pick-l").hidden = true;
    try {
      const docs = [];
      let pending = 0;
      for (const c of lensChunks(LS, name)) {
        const w = lensWhere(LS, c);
        if (w === "pick" || w === "absent") pending++;
        else docs.push(await lensChunk(LS, c));
      }
      if (seq !== lensSeq) return;
      $("lmsg").textContent = "";
      if (name === "dup") paintDup(docs, pending);
      else if (docs.length)
        name === "arch" ? paintArch(docs[0]) : paintHealth(docs[0]);
      else paintMissing(name);
    } catch (e) {
      if (seq === lensSeq)
        $("lmsg").textContent = "the lens data is damaged: " + e.message;
    }
    if (seq === lensSeq) document.documentElement.dataset.lensReady = name;
  }
  /* the chunk is in the lens/ folder of the run, which the page cannot read by itself */
  function paintMissing(name, n) {
    $("lmsg").textContent =
      (n ? n + " chunk(s) of the " : "The ") +
      LENS_TITLE[name] +
      " lens are in the lens/ folder of this run (the page cannot read a sibling file): choose that folder once.";
    $("l-pick-l").hidden = false;
  }
  function lensOpen(path, line) {
    const i = nodeAt(path, line);
    if (i !== undefined) select(i, false);
    else $("lmsg").textContent = path + ":" + line + " is not in this page";
  }
  const showModule = (name) => {
    const f = modPrefix(name);
    $("f-mod").value = f;
    st.mod = f;
    refilter();
    setLens("unused");
  };
  /* --- duplicates */
  function paintDup(docs, pending) {
    const bar = $("lbar"),
      body = $("lbody"),
      info = (LS.info && LS.info.dup) || {},
      groups = docs.flatMap(dupGroups);
    bar.textContent = "";
    const note = el(
      "span",
      "",
      info.groups +
        " group(s) of definitions with the same normalised body, " +
        info.members +
        " symbol(s)" +
        (info.omitted_groups
          ? "; " + info.omitted_groups + " smaller group(s) not listed"
          : "") +
        (info.omitted_members
          ? "; " + info.omitted_members + " member(s) beyond the per-group cap"
          : "") +
        ". Comments, whitespace and local names do not count; languages: " +
        (info.langs || []).join(", ") +
        ".",
    );
    bar.appendChild(note);
    const box = (id, text, key) => {
      const l = el("label"),
        c = document.createElement("input");
      c.type = "checkbox";
      c.id = id;
      c.checked = dupOpt[key];
      c.onchange = () => {
        dupOpt[key] = c.checked;
        dupShown = DUP_PAGE;
        renderDup();
      };
      l.appendChild(c);
      l.appendChild(document.createTextNode(" " + text));
      bar.appendChild(l);
    };
    box("d-tests", "hide tests", "hideTests");
    box("d-unused", "only groups with an unused copy", "onlyUnused");
    const ms = el("label"),
      mi = document.createElement("input");
    mi.type = "text";
    mi.id = "d-min";
    mi.value = dupOpt.minSize || "";
    mi.placeholder = "min size";
    mi.oninput = () => {
      dupOpt.minSize = +mi.value || 0;
      dupShown = DUP_PAGE;
      renderDup();
    };
    ms.appendChild(document.createTextNode("min size "));
    ms.appendChild(mi);
    bar.appendChild(ms);
    const qs = document.createElement("input");
    qs.type = "search";
    qs.id = "d-q";
    qs.placeholder = "name or path";
    qs.value = dupOpt.q;
    qs.oninput = () => {
      dupOpt.q = qs.value;
      dupShown = DUP_PAGE;
      renderDup();
    };
    bar.appendChild(qs);
    const count = el("b", "", "");
    count.id = "d-count";
    bar.appendChild(count);
    if (pending) paintMissing("dup", pending);
    function renderDup() {
      const got = dupFilter(groups, dupOpt);
      body.textContent = "";
      count.textContent = got.length + " shown";
      const frag = document.createDocumentFragment();
      for (const g of got.slice(0, dupShown)) {
        const card = el("div", "dgrp");
        card.appendChild(
          el(
            "div",
            "dhead",
            g.n +
              " copies, body size " +
              g.s +
              ", hash " +
              g.h.slice(-8) +
              (g.n > g.m.length ? " (" + g.m.length + " listed)" : ""),
          ),
        );
        for (const [fi, qual, l0, l1, vd, test] of g.m) {
          const path = g.files[fi],
            row = el("div", "drow");
          row.tabIndex = 0;
          const chip = el("span", "vchip", vd);
          chip.style.background = inkOf(vd)[0];
          chip.style.color = inkOf(vd)[1];
          row.appendChild(chip);
          row.appendChild(el("b", "", qual));
          row.appendChild(
            el(
              "span",
              "muted",
              path + ":" + l0 + "-" + l1 + (test ? "  [test]" : ""),
            ),
          );
          row.onclick = () => lensOpen(path, l0);
          row.onkeydown = (e) => e.key === "Enter" && lensOpen(path, l0);
          card.appendChild(row);
        }
        frag.appendChild(card);
      }
      body.appendChild(frag);
      if (got.length > dupShown)
        body.appendChild(
          link(
            "show " + Math.min(DUP_PAGE, got.length - dupShown) + " more",
            () => {
              dupShown += DUP_PAGE;
              renderDup();
            },
          ),
        );
      if (!got.length && !pending)
        body.appendChild(el("p", "muted", "No duplicates pass the filters."));
    }
    renderDup();
  }
  /* --- architecture */
  function paintArch(doc) {
    const bar = $("lbar"),
      body = $("lbody"),
      info = (LS.info && LS.info.arch) || {},
      ramp = m.head.ramp || [];
    bar.textContent = "";
    bar.appendChild(
      el(
        "span",
        "",
        doc.mods.length +
          " module(s) of depth " +
          (info.depth || "?") +
          ", " +
          doc.layers +
          " layer(s), " +
          doc.cycles.length +
          " cycle(s), " +
          (info.cross_edges || 0) +
          " cross-language edge(s)" +
          (doc.omitted
            ? "; " + doc.omitted + " smaller module(s) not drawn"
            : "") +
          ". A module uses the ones below it.",
      ),
    );
    const lg = el("span", "lgd");
    for (const r of ramp) {
      const i = el("i");
      i.style.background = r[1];
      i.title = "dead share " + r[3];
      lg.appendChild(i);
    }
    lg.appendChild(document.createTextNode(" dead share; "));
    lg.appendChild(el("b", "cyc", "red"));
    lg.appendChild(document.createTextNode(" in a cycle; "));
    lg.appendChild(el("b", "xl", "dashed"));
    lg.appendChild(document.createTextNode(" between languages"));
    bar.appendChild(lg);
    body.textContent = "";
    const W = Math.max(640, body.clientWidth || 900),
      lay = archLayout(doc, W),
      root = svg("svg", {
        width: lay.width,
        height: lay.height,
        viewBox: "0 0 " + lay.width + " " + lay.height,
        role: "img",
        "aria-label": "module dependency view by layer",
      });
    for (const b of lay.bands) {
      const r = svg("rect", {
        x: 0,
        y: b.y - 6,
        width: lay.width,
        height: b.h + 12,
        class: "aband" + (b.layer % 2 ? " odd" : ""),
      });
      root.appendChild(r);
      root.appendChild(
        svg("text", { x: 6, y: b.y + 14, class: "alay" }, "L" + b.layer),
      );
    }
    const eg = svg("g", { class: "aedges" });
    root.appendChild(eg);
    const drawEdges = () => {
      eg.textContent = "";
      for (const j of archEdgeSet(doc, archSel)) {
        const [a, b, w, x] = doc.edges[j],
          A = lay.boxes[a],
          B = lay.boxes[b];
        if (!A || !B) continue;
        const cyc = doc.mods[a].c >= 0 && doc.mods[a].c === doc.mods[b].c,
          on = archSel >= 0 && (a === archSel || b === archSel),
          kind = on ? "s" : cyc ? "c" : x ? "x" : "n",
          x1 = A.x + A.w / 2,
          y1 = A.y + A.h,
          x2 = B.x + B.w / 2,
          y2 = B.y,
          dy = Math.max(30, Math.abs(y2 - y1) / 2),
          d =
            y2 > y1 + 4
              ? "M" +
                x1 +
                " " +
                y1 +
                "C" +
                x1 +
                " " +
                (y1 + dy) +
                " " +
                x2 +
                " " +
                (y2 - dy) +
                " " +
                x2 +
                " " +
                y2
              : (() => {
                  /* the same row or upwards (a cycle): an arc over the boxes, from top to top */
                  const ax = A.x + A.w / 2,
                    ay = A.y,
                    lift =
                      28 +
                      Math.abs(B.y - A.y) / 2 +
                      Math.abs(x2 - ax) / 6 +
                      (x2 < ax ? 16 : 0); /* the way back is drawn higher */
                  return (
                    "M" +
                    ax +
                    " " +
                    ay +
                    "C" +
                    ax +
                    " " +
                    (ay - lift) +
                    " " +
                    x2 +
                    " " +
                    (y2 - lift) +
                    " " +
                    x2 +
                    " " +
                    y2
                  );
                })(),
          p = svg("path", {
            d,
            class: "aedge " + kind,
            "stroke-width": cyc || on ? 2 : Math.min(3, 0.7 + Math.sqrt(w) / 4),
            opacity: archSel >= 0 && !on ? 0.12 : cyc || on ? 0.95 : 0.55,
          });
        if (x) p.setAttribute("stroke-dasharray", "5 3");
        /* the arrowhead: every edge arrives from above, so it is a small triangle that points down at the top of the module */
        const ah = svg("path", {
          d: "M" + x2 + " " + y2 + "l-4 -8h8z",
          class: "ahead " + kind,
          opacity: archSel >= 0 && !on ? 0.12 : 0.9,
        });
        eg.appendChild(ah);
        const t = svg(
          "title",
          {},
          doc.mods[a].n +
            " -> " +
            doc.mods[b].n +
            ": " +
            w +
            " reference(s)" +
            (x ? ", between languages" : ""),
        );
        p.appendChild(t);
        eg.appendChild(p);
      }
    };
    drawEdges();
    doc.mods.forEach((md, i) => {
      const b = lay.boxes[i];
      if (!b) return;
      const [bg, ink] = rampColor(ramp, md.d),
        g = svg("g", {
          class: "amod" + (i === archSel ? " sel" : ""),
          tabindex: 0,
          role: "button",
          "data-mod": md.n,
        });
      g.appendChild(
        svg("rect", {
          x: b.x,
          y: b.y,
          width: b.w,
          height: b.h,
          rx: 4,
          fill: bg,
          class: md.c >= 0 ? "cyc" : "",
        }),
      );
      const nm = md.n.length > 19 ? "…" + md.n.slice(-18) : md.n;
      g.appendChild(
        svg("text", { x: b.x + 6, y: b.y + 16, fill: ink, class: "an" }, nm),
      );
      g.appendChild(
        svg(
          "text",
          { x: b.x + 6, y: b.y + 31, fill: ink, class: "ak" },
          md.k +
            " nodes" +
            (md.d ? ", " + pct(md.d) + " dead" : "") +
            (md.x.length > 1 ? ", " + md.x.length + " langs" : ""),
        ),
      );
      g.appendChild(
        svg(
          "title",
          {},
          md.n +
            ": " +
            md.k +
            " nodes, layer " +
            md.l +
            (md.c >= 0 ? ", in dependency cycle " + (md.c + 1) : "") +
            ", " +
            md.x.join(", "),
        ),
      );
      const go = () => {
        archSel = archSel === i ? -1 : i;
        drawEdges();
        root
          .querySelectorAll(".amod")
          .forEach((n) =>
            n.classList.toggle(
              "sel",
              n.getAttribute("data-mod") ===
                (archSel >= 0 ? doc.mods[archSel].n : null),
            ),
          );
        archPanel(doc, archSel);
      };
      g.onclick = go;
      g.onkeydown = (e) =>
        (e.key === "Enter" || e.key === " ") && (e.preventDefault(), go());
      root.appendChild(g);
    });
    body.appendChild(root);
    if (doc.cycles.length) {
      const c = el("div", "acycles");
      c.appendChild(el("h2", "", "dependency cycles"));
      doc.cycles
        .slice(0, 20)
        .forEach((cy, n) =>
          c.appendChild(
            el(
              "div",
              "",
              n +
                1 +
                ". " +
                cy.slice(0, 12).join("  <->  ") +
                (cy.length > 12 ? "  ... (" + cy.length + " modules)" : ""),
            ),
          ),
        );
      body.appendChild(c);
    }
    archPanel(doc, archSel);
  }
  function archPanel(doc, i) {
    const P = $("panel");
    P.textContent = "";
    if (i < 0) {
      P.appendChild(
        el(
          "p",
          "muted",
          "Click a module: its dependencies are drawn, the others fade.",
        ),
      );
      return;
    }
    const md = doc.mods[i],
      nb = archNeighbours(doc, i);
    P.appendChild(el("h3", "", md.n));
    for (const t of [
      "layer " + md.l,
      md.k + " nodes",
      pct(md.d) + " dead",
      md.x.join(", "),
    ].concat(md.c >= 0 ? ["in a dependency cycle"] : []))
      P.appendChild(el("span", "chip", t));
    for (const [title, rows] of [
      ["uses", nb.uses],
      ["used by", nb.usedBy],
    ]) {
      P.appendChild(el("h4", "", title + " (" + rows.length + ")"));
      for (const r of rows.slice(0, 30))
        P.appendChild(
          el(
            "div",
            "",
            doc.mods[r.mod].n +
              "  x" +
              r.w +
              (r.cross ? "  (between languages)" : ""),
          ),
        );
    }
    const b = el("button", "", "show this module in the unused lens");
    b.onclick = () => showModule(md.n);
    P.appendChild(b);
  }
  /* --- health */
  function paintHealth(doc) {
    const bar = $("lbar"),
      body = $("lbody"),
      info = (LS.info && LS.info.health) || {};
    bar.textContent = "";
    bar.appendChild(
      el(
        "span",
        "",
        doc.rows.length +
          " module(s), worst first. score = 100 x (1 - " +
          Object.keys(doc.weights)
            .map((k) => doc.weights[k] + " x " + k)
            .join(" - ") +
          "); dead and dup are shares of the module's symbols, fan is fan-out over " +
          doc.fan_cap +
          ", size is symbols over " +
          doc.size_cap +
          "; a module with few symbols is listed, not graded.",
      ),
    );
    const render = () => {
      body.textContent = "";
      const t = document.createElement("table"),
        hd = document.createElement("tr");
      t.className = "htab";
      for (const [k, name] of HEALTH_COLS) {
        const th = document.createElement("th");
        th.textContent =
          name + (k === healthKey ? (healthDir > 0 ? " ▲" : " ▼") : "");
        th.tabIndex = 0;
        const sort = () => {
          healthDir = healthKey === k ? -healthDir : 1;
          healthKey = k;
          render();
        };
        th.onclick = sort;
        th.onkeydown = (e) => e.key === "Enter" && sort();
        hd.appendChild(th);
      }
      t.appendChild(hd);
      for (const r of healthSort(doc.rows, healthKey, healthDir)) {
        const tr = document.createElement("tr"),
          cell = (txt, cls) => {
            const td = document.createElement("td");
            td.textContent = txt;
            if (cls) td.className = cls;
            tr.appendChild(td);
            return td;
          };
        tr.tabIndex = 0;
        cell(r.n, "mn");
        const g = cell(r.g || "-", "gr");
        g.style.background = (HEALTH_GRADE[r.g] || HEALTH_GRADE[""])[0];
        g.style.color = (HEALTH_GRADE[r.g] || HEALTH_GRADE[""])[1];
        const sc = cell("", "sc"),
          bar2 = el("span", "sbar"),
          fill2 = el("i");
        fill2.style.width = Math.max(0, Math.min(100, r.s)) + "%";
        fill2.style.background = (HEALTH_GRADE[r.g] || HEALTH_GRADE[""])[0];
        bar2.appendChild(fill2);
        sc.appendChild(bar2);
        sc.appendChild(document.createTextNode(" " + r.s));
        cell(String(r.k), "num");
        cell(String(r.loc), "num");
        cell(String(r.i), "num");
        cell(String(r.o), "num");
        cell(pct(r.dead), "num");
        cell(pct(r.dup), "num");
        cell(r.cyc ? "yes" : "", "num");
        const go = () => healthPanel(doc, r);
        tr.onclick = go;
        tr.onkeydown = (e) => e.key === "Enter" && go();
        t.appendChild(tr);
      }
      body.appendChild(t);
    };
    render();
  }
  function healthPanel(doc, r) {
    const P = $("panel");
    P.textContent = "";
    P.appendChild(el("h3", "", r.n));
    for (const t of [
      r.g || "not graded",
      "score " + r.s,
      r.k + " symbols",
      r.loc + " lines",
      "fan-in " + r.i,
      "fan-out " + r.o,
    ])
      P.appendChild(el("span", "chip", t));
    P.appendChild(el("h4", "", "what the score is made of"));
    for (const p of healthParts(r, doc))
      P.appendChild(
        el(
          "div",
          "",
          p.name +
            ": penalty " +
            Math.round(p.penalty * 100) / 100 +
            " x weight " +
            p.weight +
            " = -" +
            p.points +
            " points",
        ),
      );
    const b = el("button", "", "show this module in the unused lens");
    b.onclick = () => showModule(r.n);
    P.appendChild(b);
  }


  /* ---- events */
  function theme(t) {
    document.documentElement.dataset.theme = t;
    readCss();
    miniBuild();
    invalidate();
  }
  function init() {
    const dark =
      window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    readCss();
    const h = document.createElement("canvas");
    h.width = h.height = 6;
    const g = h.getContext("2d");
    g.strokeStyle = "rgba(0,0,0,.55)";
    g.lineWidth = 1.2;
    g.beginPath();
    g.moveTo(0, 6);
    g.lineTo(6, 0);
    g.stroke();
    hatch = ctx.createPattern(h, "repeat");
    window.addEventListener("resize", resize);
    for (const [a, b] of [
      ["q", "gq"],
      ["gq", "q"],
    ]) {
      $(a).oninput = () => {
        $(b).value = $(a).value;
        search();
      };
      $(a).onkeydown = (e) => {
        if (e.key === "Enter") jumpFirst(true);
      };
    }
    $("mode").onclick = () => setMode(mode === "graph" ? "tree" : "graph");
    $("g-fit").onclick = () => fit();
    $("g-n").onchange = (e) => ego && egoRoute(ego.center, +e.target.value);
    const miniGo = (e) => {
      const r = mini.getBoundingClientRect(),
        wh = m.head.world || [WORLD_W, WORLD_H];
      placed = true;
      V.x = CW / 2 - ((e.clientX - r.left) / r.width) * wh[0] * V.s;
      V.y = CH / 2 - ((e.clientY - r.top) / r.height) * wh[1] * V.s;
      invalidate();
    };
    let mdrag = false;
    mini.onpointerdown = (e) => {
      mini.setPointerCapture(e.pointerId);
      mdrag = true;
      miniGo(e);
    };
    mini.onpointermove = (e) => mdrag && miniGo(e);
    mini.onpointerup = () => (mdrag = false);
    $("f-lang").onchange = (e) => {
      st.lang = +e.target.value;
      refilter();
    };
    $("f-kind").onchange = (e) => {
      st.kind = +e.target.value;
      refilter();
    };
    $("f-mod").oninput = (e) => {
      st.mod = e.target.value;
      refilter();
    };
    $("f-root").onchange = (e) => {
      st.rootk = e.target.value;
      refilter();
    };
    $("f-diff").onchange = (e) => {
      st.diff = +e.target.value || 0;
      refilter();
    };
    $("f-sort").onchange = (e) => {
      st.sort = e.target.value;
      showList();
    };
    $("f-test").onchange = (e) => {
      st.hideTest = e.target.checked;
      refilter();
    };
    $("f-depth").onchange = (e) => {
      st.depth = +e.target.value;
      if (st.sel >= 0) select(st.sel, false);
    };
    for (const n of LENSES) $("l-" + n).onclick = () => setLens(n);
    $("l-pick").onchange = async (e) => {
      try {
        const n = await lensPick(LS, [...e.target.files]);
        document.documentElement.dataset.lensPicked = String(n);
        if (lensName !== "unused") {
          lensButtons();
          setLens(lensName);
        }
      } catch (x) {
        fail(x);
      }
    };
    $("theme").onclick = () =>
      theme(
        document.documentElement.dataset.theme === "dark" ? "light" : "dark",
      );
    $("png").onclick = () => {
      const a = document.createElement("a");
      a.href = cv.toDataURL("image/png");
      a.download = "dead-code-checker.png";
      a.click();
    };
    $("file").onchange = async (e) => {
      try {
        const f = e.target.files[0];
        if (f) load(await readPack(new Uint8Array(await f.arrayBuffer())));
      } catch (x) {
        fail(x);
      }
    };
    let drag = null;
    cv.onpointerdown = (e) => {
      cv.setPointerCapture(e.pointerId);
      drag = { x: e.clientX, y: e.clientY, vx: V.x, vy: V.y, moved: false };
    };
    cv.onpointermove = (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x,
        dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      if (drag.moved) {
        placed = true;
        V.x = drag.vx + dx;
        V.y = drag.vy + dy;
        busy();
        invalidate();
      }
    };
    cv.onpointerup = (e) => {
      const d = drag;
      drag = null;
      if (!d || d.moved || !m) return;
      const r = cv.getBoundingClientRect(),
        k = hit(e.clientX - r.left, e.clientY - r.top);
      if (k && k.node !== undefined) select(k.node, false);
      else if (k && k.cell !== undefined)
        zoomTo({
          x: G.x[k.cell],
          y: G.y[k.cell],
          w: G.w[k.cell],
          h: G.h[k.cell],
        });
      else select(-1, false);
    };
    cv.ondblclick = (e) => {
      const r = cv.getBoundingClientRect(),
        k = hit(e.clientX - r.left, e.clientY - r.top);
      if (!k) return;
      if (k.node !== undefined) egoRoute(k.node, ego ? ego.n : st.depth);
      else if (k.cell === undefined) {
        focus = k;
        zoomTo(k);
      }
    };
    cv.onwheel = (e) => {
      e.preventDefault();
      const r = cv.getBoundingClientRect(),
        px = e.clientX - r.left,
        py = e.clientY - r.top,
        f = Math.exp(-e.deltaY * 0.0015),
        s = Math.min(Math.max(V.s * f, 0.05), 5e4);
      V.x = px - ((px - V.x) * s) / V.s;
      V.y = py - ((py - V.y) * s) / V.s;
      V.s = s;
      placed = true;
      busy();
      invalidate();
    };
    document.addEventListener("keydown", (e) => {
      const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName);
      if (e.key === "Escape") {
        $("q").value = $("gq").value = "";
        $("ghits").textContent = "";
        st.hits = null;
        showList();
        select(-1, false);
        document.activeElement && document.activeElement.blur();
        return;
      }
      if (typing || !m) return;
      if (e.key === "/") {
        e.preventDefault();
        $("q").focus();
      } else if (e.key === "f") fit();
      else if (e.key === "g" && G) $("mode").click();
      else if (e.key === "t") $("theme").click();
      else if (e.key === "e") $("png").click();
      else if (e.key === "Backspace") {
        e.preventDefault();
        focusCell(focus && focus.parent ? focus.parent : m.tree);
      } else if (/^[123]$/.test(e.key)) {
        st.depth = +e.key;
        $("f-depth").value = e.key;
        if (st.sel >= 0) select(st.sel, false);
      } else if (e.key.startsWith("Arrow")) {
        V.x += e.key === "ArrowLeft" ? 60 : e.key === "ArrowRight" ? -60 : 0;
        V.y += e.key === "ArrowUp" ? 60 : e.key === "ArrowDown" ? -60 : 0;
        invalidate();
        e.preventDefault();
      }
    });
    window.addEventListener("hashchange", () => m && applyHash());
  }
  async function start() {
    try {
      init();
      const d = $("dcdata");
      let u8 = b64bytes(d.textContent.trim());
      if (d.dataset.enc === "deflate") u8 = await inflate(u8, "deflate");
      const ld = $("dclens"),
        lb = ld ? ld.textContent.trim() : "";
      load(await readPack(u8), lb ? parseLensBlob(b64bytes(lb)) : null);
    } catch (e) {
      fail(e);
    }
  }
  start();
}

if (typeof module !== "undefined" && module.exports)
  module.exports = {
    b64bytes,
    inflate,
    parsePack,
    fromObject,
    readPack,
    buildModel,
    buildTree,
    squarify,
    layout,
    newState,
    passes,
    recount,
    searchRegex,
    whyChain,
    SEVERITY,
    treeMoreItem,
    TREE_DEPTH,
    TREE_WIDTH,
    treeEdges,
    treeNew,
    treeGrow,
    treeOpen,
    treeVisible,
    edgeInfo,
    rootInfo,
    nKey,
    nName,
    nPath,
    nVerdict,
    WORLD_W,
    WORLD_H,
    hasLayout,
    graphModel,
    makeGrid,
    gridPick,
    lodLevel,
    lodScale,
    frontier,
    liftEdges,
    lodAt,
    cellAt,
    jumpView,
    bestHit,
    neighbourhood,
    egoGraph,
    parseRoute,
    snippetOf,
    parseLensBlob,
    lensStore,
    lensPick,
    lensWhere,
    lensChunk,
    lensGate,
    lensChunks,
    rampColor,
    archLayout,
    archEdgeSet,
    archNeighbours,
    dupGroups,
    dupFilter,
    healthSort,
    healthParts,
    modPrefix,
    LENSES,
    ARCH_BOX,
    OPEN_DIR,
    OPEN_FILE,
    MODULE_CELLS,
    LIFT_EDGES,
    NODE_CAP,
    EDGE_FRAME,
    HIDDEN,
  };
if (typeof document !== "undefined") boot();
