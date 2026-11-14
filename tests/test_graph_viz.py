"""graph.viz, graph.layout: the data pack, the one-file page, the SVG pictures, Graphviz or the built-in emitter, the artifacts of a
run, and the viewer's own script under node (the layout equals the treemap, the why chain is the par chain of the reach).

python3 -m pytest -q tests/test_graph_viz.py

Node tests skip with a reason when node is missing. Nothing here needs gitleaks or Graphviz: a fake `dot` stands in.
"""

import base64
import gzip
import json
import random
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import pytest

from dead_code_checker.graph import layout, reach, viz
from dead_code_checker.graph.model import Finding, canonical, edge, node, root
from dead_code_checker.graph.snippets import Snips

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
VIEWER = Path(viz.VIEWER)


def key(path, qual=""):
    return f"py:{path}#{qual}" if qual else f"py:{path}"


@pytest.fixture
def no_dot(monkeypatch):
    monkeypatch.setattr(viz, "dot_path", lambda: None)


def small():
    """main -> run -> helper (alive); chain a -> b (dead cluster, in lib/); w reached by a name-match edge only."""
    ns = [node(key("app.py", q), "func", line0=1 + 3 * i, line1=2 + 3 * i) for i, q in enumerate(["main", "run", "helper"])]
    ns += [node(key("lib/chain.py", q), "func", line0=1 + 3 * i, line1=2 + 3 * i) for i, q in enumerate(["a", "b"])]
    ns += [node(key("lib/weak.py", "w"), "func", line0=1, line1=2)]
    es = [
        edge(key("app.py", "main"), key("app.py", "run"), "call", "T", "app.py:2"),
        edge(key("app.py", "run"), key("app.py", "helper"), "call", "S", "app.py:5"),
        edge(key("lib/chain.py", "a"), key("lib/chain.py", "b"), "call", "T", "lib/chain.py:2"),
        edge(key("app.py", "main"), key("lib/weak.py", "w"), "ref", "N", "app.py:3"),
    ]
    return canonical(ns, es, [root("main", "app.py:1", node=key("app.py", "main"))])


def gfix(n_files=14, seed=5):
    """Nested directories with non-ASCII names, symbols of many sizes, calls of all tiers, a few roots."""
    rnd = random.Random(seed)
    dirs = ["src/été", "src/zeta", "src/zeta/deep/er", "docs/α", "tools", ""]
    ns, ks = [], []
    for f in range(n_files):
        path = f"{dirs[f % len(dirs)]}/m{f}.py".lstrip("/")
        for s in range(rnd.randint(2, 7)):
            k = key(path, f"f{s}")
            ks.append(k)
            ns.append(node(k, "func", line0=1 + 9 * s, line1=1 + 9 * s + rnd.randint(0, 12)))
    es = [
        edge(k, rnd.choice(ks), "call", rnd.choice("STND"), f"x.py:{i}")
        for i, k in enumerate(ks)
        for _ in range(rnd.randint(0, 3))
    ]
    g = canonical(ns, es, [root("main", "x:1", node=k) for k in ks[::9]])
    return g, reach.analyze(g)


def chain_graph(n=60):
    """main -> c0 -> c1 -> ... (alive), plus a dead pair: a page cut around the roots and the dead keeps a few of them."""
    ks = [key(f"m{i}.py", f"c{i}") for i in range(n)]
    ns = [node(k, "func", line0=1, line1=3) for k in ks] + [node(key("dead.py", q), "func", line0=1, line1=2) for q in ("x", "y")]
    es = [edge(ks[i], ks[i + 1], "call", "T", f"m{i}.py:2") for i in range(n - 1)] + [edge(key("dead.py", "x"), key("dead.py", "y"), "call", "T", "dead.py:2")]
    g = canonical(ns, es, [root("main", "m0.py:1", node=ks[0])])
    return g, reach.analyze(g)


def decode_page(html):
    m = re.search(r'data-enc="(\w+)">([^<]*)<', html)
    raw = base64.b64decode(m.group(2))
    return viz.from_bytes(raw if m.group(1) == "raw" else zlib.decompress(raw))


def strip_data(html):
    return re.sub(r'(<script type="application/octet-stream"[^>]*>)[^<]*', r"\1", html)


# ---------------------------------------------------------------- the pack
def test_pack_roundtrips_and_is_deterministic():
    g, res = gfix()
    pk = viz.build_pack(g, res)
    blob = viz.to_bytes(pk)
    back = viz.from_bytes(blob)
    assert back.head == json.loads(json.dumps(pk.head)) and {k: list(v) for k, v in back.cols.items()} == {k: list(v) for k, v in pk.cols.items()}
    assert viz.to_bytes(viz.build_pack(g, res)) == blob
    assert len(pk.cols["n_vd"]) == len(g.nodes) and len(pk.cols["e_src"]) == len(g.edges)
    with pytest.raises(ValueError):
        viz.from_bytes(b"nope" + blob[4:])
    with pytest.raises(ValueError):
        viz.from_bytes(blob + b"\0\0\0\0")


def test_why_alive_in_the_pack_is_the_par_chain_of_reach():
    g = small()
    res = reach.analyze(g)
    pk = viz.build_pack(g, res)
    idx = {pk.head["strings"][pk.cols["n_name"][i]]: i for i in range(len(pk.cols["n_name"]))}
    p = res.prep
    for q in ("run", "helper"):
        i, o = idx[q], p.idx[key("app.py", q)]
        par_new = pk.cols["n_par"][i]
        assert par_new >= 0 and pk.head["strings"][pk.cols["n_name"][par_new]] == p.keys[res.par[o]].split("#")[1]
    assert pk.cols["n_par"][idx["a"]] == -1  # a dead node has no chain


def test_why_dead_text_is_the_reach_text_and_capped():
    g = small()
    res = reach.analyze(g)
    pk = viz.build_pack(g, res)
    texts = [pk.head["strings"][w] for w in pk.cols["n_why"] if w >= 0]
    assert texts and any(res.why_dead(key("lib/chain.py", "a")).render() == t for t in texts)
    assert all(w < 0 for w in viz.build_pack(g, res, why_max=0).cols["n_why"])


def test_keep_closes_over_par_and_marks_and_findings_travel():
    g = small()
    res = reach.analyze(g)
    helper = res.prep.idx[key("app.py", "helper")]
    pk = viz.build_pack(g, res, keep=[helper], marks={key("lib/chain.py", "a"): 3}, findings=[Finding("W", "a.py", 1, "dc.x", "m")])
    names = {pk.head["strings"][i] for i in pk.cols["n_name"]}
    assert names == {"helper", "run", "main"}  # the par chain of helper
    assert pk.head["findings"][0]["code"] == "dc.x" and pk.head["findings"][0]["why"]
    pk2 = viz.build_pack(g, res, keep=[helper, res.prep.idx[key("lib/chain.py", "a")]], marks={key("lib/chain.py", "a"): 3})
    assert 3 in list(pk2.cols["n_mark"])


def test_truncation_keeps_dead_roots_and_par_chains_and_counts_the_rest():
    g, res = chain_graph()
    pg = viz.make_page(g, res, full_nodes=10)
    assert pg.ok and pg.truncated and pg.kept < pg.total and pg.hops in (2, 1, 0)
    pk = decode_page(pg.html)
    assert pk.head["meta"]["truncated"] and pk.head["meta"]["nodes_total"] == len(g.nodes)
    assert sum(pk.cols["c_n"]) == len(g.nodes) - len(pk.cols["n_vd"])  # the omitted nodes remain as counts
    vs = res.verdicts()
    dead = sum(1 for k in res.prep.keys if vs[k] in ("dead", "dead-cluster"))
    shown = sum(1 for v in pk.cols["n_vd"] if pk.head["tables"]["verdicts"][v] in ("dead", "dead-cluster"))
    assert shown == dead


def test_page_limit_steps_down_then_refuses():
    g, res = chain_graph()
    whole = viz.make_page(g, res)
    smaller = viz.make_page(g, res, limit=len(whole.html) - 1)
    assert smaller.ok and smaller.truncated and len(smaller.html) < len(whole.html)
    refused = viz.make_page(g, res, limit=10)
    assert not refused.ok


# ---------------------------------------------------------------- the page
def test_page_has_no_network_no_eval_and_a_csp():
    g = small()
    pg = viz.make_page(g, reach.analyze(g))
    code = strip_data(pg.html)
    assert "default-src 'none'" in code and "script-src 'unsafe-inline'" in code and "img-src data:" in code
    nohttp = code.replace("http-equiv", "")
    assert nohttp.count("http://www.w3.org/2000/svg") == 1  # the SVG namespace of the lenses: an identifier, never requested
    nohttp = nohttp.replace("http://www.w3.org/2000/svg", "")
    for bad in ("http", "fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "importScripts", "import(", "<link", "<iframe", "url(", "@import", "src="):
        assert bad not in nohttp, bad
    js = (VIEWER / "viewer.js").read_text()
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "Function(", "localStorage", "location.href", "window.open"):
        assert bad not in js, bad
    assert "</script" not in js and "</script" not in (VIEWER / "viewer.css").read_text()
    assert code.count("<script") == 2 and "@@" not in code


def test_page_data_decodes_to_the_pack_deflate_and_plain():
    g = small()
    res = reach.analyze(g)
    want = viz.to_bytes(viz.build_pack(g, res, meta={"links": [], "mode": "index", "hops": None}))
    for plain in (False, True):
        pg = viz.make_page(g, res, plain=plain)
        m = re.search(r'data-enc="(\w+)">([^<]*)<', pg.html)
        assert m.group(1) == ("raw" if plain else "deflate")
        raw = base64.b64decode(m.group(2))
        assert (raw if plain else zlib.decompress(raw)) == want
    assert viz.make_page(g, res).html == viz.make_page(g, res).html


def test_title_is_escaped_and_hostile_names_stay_in_the_data():
    evil = 'x"><script>alert(1)</script>'
    g = canonical([node(key("evil.py", evil), "func", line0=1)], [], [root("main", "evil.py:1", node=key("evil.py", evil))])
    pg = viz.make_page(g, reach.analyze(g), title="<b>t</b>")
    code = strip_data(pg.html)
    assert "<b>t</b>" not in code and "&lt;b&gt;t&lt;/b&gt;" in code and "alert(1)" not in code
    assert code.count("<script") == 2


def test_page_with_code_says_it_is_local_and_without_code_does_not():
    g = small()
    res = reach.analyze(g)
    k = key("app.py", "run")
    withcode = viz.make_page(g, res, snippets=Snips({k: "def run(): pass"}, {"state": "on", "n": 1}, ()))
    assert 'name="dc-local"' in withcode.html and "do not publish" in withcode.html
    pk = decode_page(withcode.html)
    assert len(pk.cols["s_node"]) == 1 and "def run(): pass" in pk.head["strings"]
    nocode = viz.make_page(g, res, snippets=Snips({}, {"state": "off", "reason": "x"}, ()))
    assert 'name="dc-local"' not in nocode.html and len(decode_page(nocode.html).cols["s_node"]) == 0


# ---------------------------------------------------------------- the layout
def test_layout_cache_gives_the_same_bytes_and_survives_damage(tmp_path, monkeypatch):
    g, res = gfix()
    calls = []
    real = layout.compute
    monkeypatch.setattr(layout, "compute", lambda *a: calls.append(1) or real(*a))
    cache = str(tmp_path / "lc")
    plain = viz.to_bytes(viz.build_pack(g, res))
    first = viz.to_bytes(viz.build_pack(g, res, layout_dir=cache))
    assert first == plain and len(calls) == 2
    assert viz.to_bytes(viz.build_pack(g, res, layout_dir=cache)) == first and len(calls) == 2  # from the cache
    (entry,) = list(Path(cache).glob("*.dcv"))
    entry.write_bytes(entry.read_bytes()[:-9])  # damaged: computed again, replaced
    assert viz.to_bytes(viz.build_pack(g, res, layout_dir=cache)) == first and len(calls) == 3
    assert viz.to_bytes(viz.build_pack(g, res, layout_dir=cache)) == first and len(calls) == 3


def test_layout_cells_are_preorder_nested_and_every_leaf_sits_in_its_file_cell():
    g, res = gfix()
    c = viz.build_pack(g, res).cols
    cells, n_nodes = len(c["a_parent"]), len(c["n_x"])
    assert c["a_parent"][0] == -1 and all(c["a_parent"][r] < r for r in range(1, cells))
    for r in range(1, cells):
        p = c["a_parent"][r]
        assert c["a_x"][r] >= c["a_x"][p] - 1 and c["a_y"][r] >= c["a_y"][p] - 1
        assert c["a_x"][r] + c["a_w"][r] <= c["a_x"][p] + c["a_w"][p] + 2
    assert (c["a_x"][0], c["a_y"][0], c["a_w"][0], c["a_h"][0]) == (0, 0, layout.WORLD_W * layout.Q, layout.WORLD_H * layout.Q)
    for i in range(n_nodes):
        f = c["n_agg"][i]
        assert c["a_kind"][f] == layout.FILE and c["a_x"][f] - 1 <= c["n_x"][i] <= c["a_x"][f] + c["a_w"][f] + 1
    assert sum(1 for k in c["a_kind"] if k == layout.FILE) == len({n.path for n in g.nodes})


# ---------------------------------------------------------------- the SVG pictures
def nasty():
    ns = tuple(viz.DNode(f"n{i}", lab, "sub <&>", "g", "dead", 'x"y.html#k=<z>', 0) for i, lab in enumerate(["a\x01b", "<x>", "é" * 100, "ok"]))
    es = (viz.DEdge("n0", "n1", "T", 1), viz.DEdge("n1", "n2", "N", 1), viz.DEdge("n2", "n3", "D", 3), viz.DEdge("n3", "n0", "S", 1), viz.DEdge("n0", "n0", "T", 1))
    return viz.Diagram("t<>", ns, es)


def test_builtin_svg_is_well_formed_with_hostile_labels():
    d = nasty()
    svg = viz.emit_svg(d)
    root_ = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    assert root_.tag == ns + "svg" and int(root_.get("width")) > 0
    assert len(root_.findall(f".//{ns}rect")) == len(d.nodes) and len(root_.findall(f".//{ns}a")) == len(d.nodes)
    assert len(root_.findall(f".//{ns}line")) == 4  # the self loop is not drawn
    assert "\x01" not in svg and "<x>" not in svg


def test_builtin_svg_empty_cycle_and_300_nodes():
    assert ET.fromstring(viz.emit_svg(viz.Diagram("e", (), ()))) is not None
    ns = tuple(viz.DNode(f"n{i}", f"f{i}", "dead", "g", "dead", "x.html", 0) for i in range(300))
    es = tuple(viz.DEdge(f"n{i}", f"n{(i * 7 + 1) % 300}", "T", 1) for i in range(300))
    assert len(ET.fromstring(viz.emit_svg(viz.Diagram("c", ns, es))).findall(".//{http://www.w3.org/2000/svg}rect")) == 300


def test_dot_text_escapes_names_and_ids_are_generated():
    d = viz.Diagram("t", (viz.DNode("n1", 'x"]; evil [label="y', "a\\b\nc", "g", "dead", 'u"rl', 0),), ())
    dot = viz.to_dot(d)
    assert "\n  evil" not in dot and '\\"]; evil [label=\\"y' in dot and "v0 [" in dot


def test_modules_diagram_marks_dead_and_suspects_never_as_dead():
    g = small()
    res = reach.analyze(g)
    d = viz.modules_diagram(g, res, suspects=[key("lib/chain.py", "a"), key("lib/chain.py", "b")])
    by = {n.label: n for n in d.nodes}
    assert d.modules and "2 suspect" in by["lib"].sub and "0 dead" in by["lib"].sub
    plain = viz.diagram(g, res, range(len(g.nodes)), {}, "x", "p.html", suspects=[key("lib/chain.py", "a")])
    assert {n.verdict for n in plain.nodes if n.label == "a"} == {"suspect"}
    assert viz.PALETTE["suspect"][0] in viz.emit_svg(plain) and viz.PALETTE["dead-cluster"][0] in viz.emit_svg(plain)


def test_render_svg_without_graphviz_uses_the_emitter_and_says_how_to_install(no_dot):
    g = small()
    svg, w = viz.render_svg(viz.modules_diagram(g, reach.analyze(g)))
    assert ET.fromstring(svg) is not None
    assert w.level == "W" and w.code == "dc.graphviz-missing" and viz.INSTALL_DOT in w.msg


def fake_dot(tmp_path, monkeypatch, body):
    p = tmp_path / "fakedot"
    p.write_text(f"#!{sys.executable}\n{body}\n")
    p.chmod(0o755)
    monkeypatch.setenv("DEAD_CODE_CHECKER_DOT", str(p))
    monkeypatch.setattr(viz, "netless_prefix", lambda: ())  # the fake needs no sandbox
    return str(p)


def test_a_failing_or_slow_dot_is_a_warning_not_a_crash(tmp_path, monkeypatch):
    d = viz.modules_diagram(small(), reach.analyze(small()))
    for body, timeout, why in (
        ("import sys; sys.stderr.write('boom\\n'); sys.exit(3)", 15, "boom"),
        ("import time; time.sleep(30)", 0.5, "timed out"),
    ):
        fake_dot(tmp_path, monkeypatch, body)
        svg, w = viz.render_svg(d, timeout)
        assert ET.fromstring(svg) is not None and w.code == "dc.graphviz-failed" and why in w.msg


def test_a_dot_that_answers_is_used_as_it_is(tmp_path, monkeypatch):
    fake_dot(tmp_path, monkeypatch, "import sys; sys.stdin.read(); print('<svg xmlns=\"http://www.w3.org/2000/svg\"><title>from dot</title></svg>')")
    g = small()
    svg, w = viz.render_svg(viz.diagram(g, reach.analyze(g), range(len(g.nodes)), {}, "change", "change.html"))
    assert w is None and "from dot" in svg
    svg, w = viz.render_svg(viz.modules_diagram(g, reach.analyze(g)))  # an overview needs the boxes: a dot without them is a W
    assert w is not None and w.code == "dc.graphviz-failed" and ET.fromstring(svg) is not None


def test_dot_path_honours_the_variable_only_for_an_absolute_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("DEAD_CODE_CHECKER_DOT", "relative/dot")
    assert viz.dot_path() is None
    monkeypatch.setenv("DEAD_CODE_CHECKER_DOT", str(tmp_path / "missing"))
    assert viz.dot_path() is None


@pytest.mark.skipif(shutil.which("dot") is None, reason="graphviz is not installed")
def test_real_graphviz_svg_has_the_nodes_and_links():
    g = small()
    d = viz.diagram(g, reach.analyze(g), range(len(g.nodes)), {}, "change", "change.html")
    svg, w = viz.render_svg(d)
    assert w is None and svg.count('class="node"') == len(d.nodes) and "change.html#k=" in svg
    mods, w = viz.render_svg(viz.modules_diagram(g, reach.analyze(g)))
    assert w is None and ET.fromstring(mods) is not None


def test_the_overview_output_is_deterministic(no_dot):
    g, res = gfix(30)
    d = viz.modules_diagram(g, res)
    assert viz.emit_svg(d) == viz.emit_svg(d) and len(d.nodes) > 3


# ---------------------------------------------------------------- the artifacts of a run
def test_write_run_branch_writes_every_artifact_and_links_them(tmp_path, no_dot):
    g = small()
    res = reach.analyze(g)
    marks = {key("app.py", "run"): 1, key("lib/chain.py", "a"): 1 | 2}
    fs = [Finding("W", "lib/chain.py", 1, "dc.dead-cluster", "`a` is dead")]
    out = tmp_path / "runs" / "20260101T000000Z-branch-abcd1234"
    art = viz.write_run(str(out), "branch", g, res, fs, {"scope": "branch", "nodes": len(g.nodes), "verdicts": res.counts()}, marks=marks)
    names = {p.name for p in out.iterdir()}
    assert names == {"index.html", "modules.svg", "change.html", "change.svg", "graph.json.gz", "report.md", "findings.json", "sarif.json"}
    assert set(art.files) == names and art.viewer == str(out / "change.html")
    text = (out / "report.md").read_text()
    assert text.split("\n")[0].startswith("file://") and "# dead-code-checker branch report" in text
    assert [f.code for f in art.findings] == ["dc.graphviz-missing"]  # one W for both SVGs
    assert "dc.dead-cluster" in (out / "findings.json").read_text()
    sarif = json.loads((out / "sarif.json").read_text())
    assert sarif["version"] == "2.1.0" and {r["ruleId"] for r in sarif["runs"][0]["results"]} == {"dc.dead-cluster", "dc.graphviz-missing"}
    for s in ("modules.svg", "change.svg"):
        ET.fromstring((out / s).read_text())
    ch = decode_page((out / "change.html").read_text())
    assert ch.head["meta"]["mode"] == "change" and ch.head["meta"]["links"][0] == ["overview", "index.html"]
    keys = {ch.head["files"][ch.cols["n_file"][i]] + "#" + ch.head["strings"][ch.cols["n_name"][i]]: ch.cols["n_mark"][i] for i in range(len(ch.cols["n_mark"]))}
    assert keys["lib/chain.py#a"] == 3 and keys["app.py#run"] == 1 and "lib/weak.py#w" not in keys
    ix = decode_page((out / "index.html").read_text())
    assert ix.head["meta"]["mode"] == "index" and len(ix.cols["n_vd"]) == len(g.nodes)
    full = json.loads(gzip.decompress((out / "graph.json.gz").read_bytes()))
    assert full["cols"]["n_vd"] == list(viz.build_pack(g, res).cols["n_vd"])
    assert (out.parent / "latest").resolve() == out.resolve() or (out.parent / "latest").read_text().strip() == out.name
    assert art.summary["W"] == 2 and art.summary["nodes"] == len(g.nodes) and art.summary["langs"] == ["py"]


def test_write_run_repo_has_no_change_page_and_a_too_large_page_is_a_warning_not_a_file(tmp_path, no_dot):
    g = small()
    res = reach.analyze(g)
    art = viz.write_run(str(tmp_path / "r"), "repo", g, res, [], {"scope": "repo"})
    assert "change.html" not in art.files and "index.html" in art.files and art.viewer.endswith("index.html")
    art = viz.write_run(str(tmp_path / "big"), "repo", g, res, [], {"scope": "repo"}, limit=10)
    assert "index.html" not in art.files and any(f.code == "dc.viz-too-large" for f in art.findings)
    assert art.viewer.endswith("graph.json.gz") and (tmp_path / "big" / "graph.json.gz").exists()


def test_write_run_truncated_page_is_marked_and_the_json_has_everything(tmp_path, no_dot):
    g, res = chain_graph()
    art = viz.write_run(str(tmp_path / "t"), "repo", g, res, [], {"scope": "repo"}, full_nodes=10)
    (w,) = [f for f in art.findings if f.code == "dc.viz-truncated"]
    assert w.level == "W"
    full = json.loads(gzip.decompress((tmp_path / "t" / "graph.json.gz").read_bytes()))
    assert len(full["cols"]["n_vd"]) == len(g.nodes)
    quiet = viz.write_run(str(tmp_path / "q"), "repo", g, res, [], {"scope": "repo"}, full_nodes=10, quiet=True)
    assert [f.level for f in quiet.findings if f.code == "dc.viz-truncated"] == ["I"]


def test_write_report_without_a_page_says_why_and_never_links_a_missing_file(tmp_path):
    viz.write_report(str(tmp_path), [], {"scope": "branch"}, note="no page: nothing to draw")
    assert (tmp_path / "report.md").read_text().startswith("no page: nothing to draw")


# ---------------------------------------------------------------- the viewer's own script under node
def run_js(tmp_path, script, *args):
    (tmp_path / "t.js").write_text(script)
    r = subprocess.run([NODE, str(tmp_path / "t.js"), str(VIEWER / "viewer.js"), *map(str, args)], capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


SAME = r"""
const V = require(process.argv[2]); const fs = require("fs");
(async () => {
  const pack = await V.readPack(new Uint8Array(fs.readFileSync(process.argv[3])));
  const m = V.buildModel(pack); V.buildTree(m); const G = V.graphModel(m);
  const q = (v) => Math.floor(v * 16 + 0.5), bad = [];
  for (let i = 0; i < m.N; i++) {
    const k = m.leaf[i];
    if (q(k.x + k.w / 2) !== m.c.n_x[i] || q(k.y + k.h / 2) !== m.c.n_y[i]) bad.push(i);
  }
  console.log(JSON.stringify({ N: m.N, bad: bad.length, C: G.C }));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@needs_node
def test_python_layout_puts_every_symbol_at_the_centre_of_its_treemap_cell_for_full_and_cut_packs(tmp_path):
    g, res = gfix(n_files=30)
    cg, cres = chain_graph()
    for name, pk in (("full.dcv", viz.build_pack(g, res)), ("cut.dcv", viz.make_page(cg, cres, full_nodes=10).pack)):
        p = tmp_path / name
        p.write_bytes(viz.to_bytes(pk))
        out = run_js(tmp_path, SAME, p)
        assert out["N"] == len(pk.cols["n_x"]) > 0 and out["bad"] == 0, (name, out)


WHY = r"""
const V = require(process.argv[2]); const fs = require("fs");
(async () => {
  const pack = await V.readPack(new Uint8Array(fs.readFileSync(process.argv[3])));
  const m = V.buildModel(pack); V.buildTree(m);
  const out = {};
  for (let i = 0; i < m.N; i++) { const w = V.whyChain(m, i); out[V.nKey(m, i)] = w ? w.steps.length : -1; }
  console.log(JSON.stringify({ N: m.N, chains: out }));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@needs_node
def test_viewer_js_loads_a_pack_and_its_why_chain_is_the_par_chain(tmp_path):
    g = small()
    res = reach.analyze(g)
    p = tmp_path / "s.dcv"
    p.write_bytes(viz.to_bytes(viz.build_pack(g, res)))
    out = run_js(tmp_path, WHY, p)
    assert out["N"] == len(g.nodes)
    # main is a root (chain of 1), run is one hop below it, helper two; a dead node has none
    lens = {k.split("#")[1]: v for k, v in out["chains"].items()}
    assert (lens["main"], lens["run"], lens["helper"], lens["a"]) == (0, 1, 2, -1)


@needs_node
def test_viewer_js_refuses_what_is_not_a_pack(tmp_path):
    script = r"""
const V = require(process.argv[2]);
(async () => {
  let msg = "";
  try { await V.readPack(new TextEncoder().encode('{"hello": 1}')); } catch (e) { msg = e.message; }
  console.log(JSON.stringify({ msg }));
})();
"""
    out = run_js(tmp_path, script)
    assert out["msg"].startswith("not a dead-code-checker graph") and "keys: hello" in out["msg"]


@needs_node
def test_the_script_inside_a_generated_page_is_valid_javascript(tmp_path):
    g = small()
    html = viz.make_page(g, reach.analyze(g)).html
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == 1
    f = tmp_path / "page.js"
    f.write_text(scripts[0])
    r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


CHROMIUM = next((p for p in (shutil.which(n) for n in ("chromium", "chromium-browser", "google-chrome", "chrome")) if p), None)


@pytest.mark.skipif(CHROMIUM is None, reason="no chromium or chrome is installed")
def test_a_generated_page_boots_in_a_real_browser_and_draws_the_summary(tmp_path):
    g = small()
    for name, plain in (("deflate.html", False), ("plain.html", True)):
        p = tmp_path / name
        p.write_text(viz.make_page(g, reach.analyze(g), plain=plain).html, encoding="utf-8")
        r = subprocess.run(
            [CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=4000", "--dump-dom", p.as_uri()],
            capture_output=True, text=True, timeout=120,
        )  # fmt: skip
        if r.returncode != 0 or "<html" not in r.stdout:
            pytest.skip("chromium does not start here: " + r.stderr[-200:])
        assert 'data-boot="ok"' in r.stdout, name
        assert '<span id="summary">6 of 6 nodes, 4 edges;' in r.stdout and '<div id="err" role="alert" hidden="">' in r.stdout, name


def conf_findings():
    out = []
    for path, n, level in (("lib/chain.py", 20, "W"), ("lib/chain.py", 90, "W"), ("lib/weak.py", None, "W"), ("app.py", 55, "I")):
        f = Finding(level, path, 1, "dc.dead", f"`f{n}` is dead")
        f.confidence, f.conf_note = n, "base 40 = 40"
        out.append(f)
    return out


def test_the_findings_of_the_pack_carry_the_confidence_of_a_claim_and_nothing_else():
    g = small()
    pk = viz.build_pack(g, reach.analyze(g), findings=[*conf_findings(), Finding("W", "x.py", 1, "dc.x", "m")])
    got = pk.head["findings"]
    assert [f.get("confidence") for f in got] == [20, 90, None, 55, None]
    assert got[0]["confidence_note"] == "base 40 = 40" and "confidence_note" not in got[2]
    assert viz.build_pack(g, reach.analyze(g), findings=conf_findings()).head == viz.build_pack(g, reach.analyze(g), findings=conf_findings()).head


@needs_node
def test_the_viewer_script_has_the_badge_the_sort_and_the_select_of_the_shell():
    js = (VIEWER / "viewer.js").read_text(encoding="utf-8")
    assert 'st.sort === "conf"' in js and "confBadge(f.confidence)" in js and '$("f-sort").onchange' in js
    shell = (VIEWER / "shell.html").read_text(encoding="utf-8")
    assert 'id="f-sort"' in shell and '<option value="conf">confidence</option>' in shell
    assert "#list b.conf" in (VIEWER / "viewer.css").read_text(encoding="utf-8")


@pytest.mark.skipif(CHROMIUM is None, reason="no chromium or chrome is installed")
def test_a_real_browser_shows_the_badge_and_sorts_the_list_by_confidence(tmp_path):
    g = small()
    html = viz.make_page(g, reach.analyze(g), findings=conf_findings(), plain=True).html
    pick = (  # after the boot: the list as the run gave it, then sorted by confidence
        "<script>setTimeout(function(){var l=function(){return Array.prototype.map.call(document.querySelectorAll('#list li'),"
        "function(x){return x.textContent;});};var a=l();var s=document.getElementById('f-sort');s.value='conf';"
        "s.dispatchEvent(new Event('change'));var o=document.createElement('pre');o.id='probe';"
        "o.textContent=JSON.stringify({before:a,after:l()});document.body.appendChild(o);},1500);</script></body>"
    )
    p = tmp_path / "sort.html"
    p.write_text(html.replace("</body>", pick), encoding="utf-8")
    r = subprocess.run(
        [CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=6000", "--dump-dom", p.as_uri()],
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    if r.returncode != 0 or "<html" not in r.stdout:
        pytest.skip("chromium does not start here: " + r.stderr[-200:])
    m = re.search(r'<pre id="probe">(.*?)</pre>', r.stdout, re.S)
    assert m, "the page did not boot"
    import html as htmllib

    got = json.loads(htmllib.unescape(m.group(1)))
    badges = lambda rows: [re.search(r"\[(\d+)\]", x).group(1) if re.search(r"\[(\d+)\]", x) else "-" for x in rows]  # noqa: E731
    assert badges(got["before"]) == ["20", "90", "-", "55"]
    assert badges(got["after"]) == ["90", "55", "20", "-"]  # the claim without a number goes last; the level decided nothing

