"""graph.tree: the call tree of one symbol: callers and callees, worst verdict first, cycles and repeats marked, the limits of
depth and width, and the command that prints it.

python3 -m pytest -q tests/test_graph_tree.py
"""

import json
import shutil
from types import SimpleNamespace

import pytest
from graph_support import new_repo

from dead_code_checker.graph import levels, reach, tree
from dead_code_checker.graph.model import canonical, edge, node, root
from dead_code_checker.graph.verdict import Proof

pytestmark = pytest.mark.usefixtures("hermetic_git", "data_root", "dcref")


def key(path, qual=""):
    return f"py:{path}#{qual}" if qual else f"py:{path}"


def treeg():
    """main -> a, b, w00..w29 (a wide level); a -> c, b -> c (c is shown once), c -> a (a cycle), c -> e1 -> e2 -> e3 (a chain);
    dead x and y call a (they come first in the callers of a); a name-match edge main -> weak.
    """
    k = lambda q: key("app.py", q)  # noqa: E731
    names = ["main", "a", "b", "c", "e1", "e2", "e3", "x", "y", "weak"] + [f"w{i:02d}" for i in range(30)]
    ns = [node(k(q), "func", line0=1 + 3 * i, line1=2 + 3 * i) for i, q in enumerate(names)]
    pairs = [
        ("main", "a"),
        ("main", "b"),
        ("a", "c"),
        ("b", "c"),
        ("c", "a"),
        ("c", "e1"),
        ("e1", "e2"),
        ("e2", "e3"),
    ]
    pairs += [("x", "a"), ("y", "a")] + [("main", f"w{i:02d}") for i in range(30)]
    es = [edge(k(s), k(d), "call", "T", f"app.py:{2 + j}") for j, (s, d) in enumerate(pairs)]
    es.append(edge(k("main"), k("weak"), "ref", "N", "app.py:99"))
    return canonical(ns, es, [root("main", "app.py:1", node=k("main"))])


# ---------------------------------------------------------------- the Python tree
def vds(res):
    return res.verdicts()


def tree_of(g, q, up, **kw):
    res = reach.analyze(g)
    return tree.build(res.prep, vds(res), res.prep.idx[key("app.py", q)], up, **kw)


def rows(doc, d=1):
    out = []
    for k in doc["kids"]:
        out.append((d, k["name"], k["mark"], k["edge"]["site"]))
        out += rows(k, d + 1)
    return out


def test_callers_come_worst_verdict_first_then_by_name_and_cycles_are_marked():
    doc = tree_of(treeg(), "a", True, depth=3)
    assert [(d, n, m) for d, n, m, _ in rows(doc)][:4] == [
        (1, "x", ""),
        (1, "y", ""),
        (1, "c", ""),
        (2, "a", "cycle"),
    ]
    assert doc["n"] == 4 and [k["verdict"] for k in doc["kids"]] == [
        "dead",
        "dead",
        "alive",
        "alive",
    ]
    assert doc["kids"][0]["edge"] == {
        "kind": "call",
        "tier": "T",
        "site": "app.py:" + str(2 + 8),
    }


def test_callees_seen_nodes_are_marked_and_not_expanded_and_the_depth_limit_stops():
    doc = tree_of(treeg(), "main", False, depth=4, width=3)
    got = rows(doc)
    names = [(d, n, m) for d, n, m, _ in got]
    assert doc["more"] == 30 and len(doc["kids"]) == 3  # 33 edges, 3 shown
    assert (1, "a", "") in names and (2, "c", "") in names and (3, "a", "cycle") in names
    assert (1, "b", "") in names and (2, "c", "seen") in names  # c was shown below a
    assert (3, "e1", "") in names and (4, "e2", "limit") in names and not any(n == "e3" for _, n, _ in names)
    c = next(k for k in doc["kids"][1]["kids"] if k["name"] == "c")
    assert c["n"] == 2 and c["more"] == 0


def test_width_limit_counts_what_is_cut_and_the_order_is_deterministic():
    doc = tree_of(treeg(), "main", False, depth=1)
    assert len(doc["kids"]) == tree.WIDTH and doc["more"] == 33 - tree.WIDTH
    assert doc == tree_of(treeg(), "main", False, depth=1)
    assert doc["kids"][0]["name"] == "weak" and doc["kids"][0]["verdict"] == "weak-alive"  # worse than alive, so first
    assert [k["name"] for k in doc["kids"][1:4]] == ["a", "b", "w00"]


def test_render_lists_edge_kind_site_and_marks():
    res = reach.analyze(treeg())
    doc = tree.build(res.prep, vds(res), res.prep.idx[key("app.py", "a")], True, depth=3, width=1)
    text = "\n".join(tree.render(doc, True))
    assert text.splitlines()[0] == "callers (who calls it, up): 4"
    assert "  x [dead] app.py:" in text and "via call/T at app.py:10" in text
    assert "+3 more" in text
    empty = tree.build(res.prep, vds(res), res.prep.idx[key("app.py", "main")], True)
    assert tree.render(empty, True) == ["callers (who calls it, up): none"]


# ---------------------------------------------------------------- the command
APP = "def helper\ndef run\nref helper\ndef main\nref run\nroot main main\n"


@pytest.fixture
def repo(tmp_path):
    return new_repo(tmp_path, {"lib/app.dcref": APP, "lib/other.dcref": "def helper\n"})


def args(node, **kw):
    return SimpleNamespace(
        **{
            "node": node,
            "callers": False,
            "callees": False,
            "depth": tree.DEPTH,
            "width": tree.WIDTH,
            "json": False,
            **kw,
        }
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
class TestCommand:
    def test_prints_both_trees_with_verdicts_edge_kinds_and_sites(self, repo, capsys):
        assert tree.run(args("lib/app.dcref#run"), str(repo)) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0].startswith("dcref:lib/app.dcref#run [alive]")
        assert "callers (who calls it, up): 1" in out and "callees (what it calls, down): 1" in out
        assert "main [alive] lib/app.dcref:4" in out and "helper [alive] lib/app.dcref:1" in out
        assert "via call/" in out and "at lib/app.dcref:" in out

    def test_one_side_json_and_the_limits(self, repo, capsys):
        assert tree.run(args("run", callers=True, json=True), str(repo)) == 0
        doc = json.loads(capsys.readouterr().out)
        assert set(doc) == {"node", "verdict", "callers"} and doc["callers"]["kids"][0]["name"] == "main"
        assert tree.run(args("run", callees=True, depth=1, width=1), str(repo)) == 0
        out = capsys.readouterr().out
        assert "callers" not in out and "callees (what it calls, down): 1" in out
        for bad in ({"depth": 0}, {"depth": 51}, {"width": 0}):
            assert tree.run(args("run", **bad), str(repo)) == 2
        assert "--depth is 1..50" in capsys.readouterr().err

    def test_ambiguous_and_unknown_names_exit_2_and_list_the_candidates(self, repo, capsys):
        assert tree.run(args("helper"), str(repo)) == 2
        err = capsys.readouterr().err
        assert "ambiguous" in err and "dcref:lib/app.dcref#helper" in err and "dcref:lib/other.dcref#helper" in err
        assert tree.run(args("no_such_symbol_xyz"), str(repo)) == 2
        assert "no node matches" in capsys.readouterr().err

    def test_a_dead_symbol_shows_as_dead_and_a_suspect_as_suspect(self, repo, capsys):
        (repo / "lib" / "chain.dcref").write_text("def link_a\nref link_b\ndef link_b\n")
        assert tree.run(args("link_b", callers=True), str(repo)) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0] == "dcref:lib/chain.dcref#link_b [suspect]" and "link_a [suspect]" in out

        def semantic(
            root, built, store, notes, deadline, clock
        ):  # an index covers the package: the claim is corroborated
            return built.graph, Proof(frozenset({"lib/chain.dcref"}))

        assert tree.run(args("link_b", callers=True), str(repo), steps=levels.Steps(semantic=semantic)) == 0
        assert capsys.readouterr().out.splitlines()[0] == "dcref:lib/chain.dcref#link_b [dead-cluster]"
