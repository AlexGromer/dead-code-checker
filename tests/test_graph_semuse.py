"""graph.semuse: the semantic cache of the store is USED: the SCIP overlays refine the graph and name the covered paths, the cached
engine matches corroborate, a stale or foreign item is not applied and one W says so, the deadline of the level is kept, and a cache
that cannot be read costs the proof and never the check.

python3 -m pytest -q tests/test_graph_semuse.py

The cache is built here, by hand, in the format graph.semcache fixes (store.engine_result: `sem:<engine>:<root>:<package_hash>` =
JSON {meta, index}, `eng:<tool>:<input_hash>` = adapters.to_payload); the graph and its index are the twin world of test_graph_sem.
Nothing is started and nothing touches the network.
"""

import base64
import json
from pathlib import Path

import graph_scip_enc as enc  # noqa: F401
import test_graph_sem as T
from dead_code_checker.graph import adapters, sem, semcache, semuse, verdict
from dead_code_checker.graph import store as dcstore


def sem_item(blob, tree, root="", family="python", engine="scip-python", file_tree=None, tmp=None):
    """(key, payload) of a `sem:` item: the blob stamped for `tree` by sem.stamp, the way an indexer run leaves it."""
    idx = Path(tmp) / f"{engine}.scip"
    idx.write_bytes(blob)
    meta = sem.stamp(str(idx), tree, root, family, engine)
    payload = {"meta": meta._asdict(), "index": base64.b64encode(blob).decode()}
    return f"sem:{engine}:{root}:{meta.input_hash}", json.dumps(payload).encode()


def twin_cache(tmp_path, t, data, engine="scip-fake", **kw):
    st = dcstore.Store(str(tmp_path / "g.sqlite"))
    tree = {n.path: "b-" + n.path for n in t.graph.nodes}
    key, payload = sem_item(data, tree, engine=engine, tmp=tmp_path, **kw)
    st.put_engine_result(key, engine, payload)
    return st, tree


def test_refuted_edges_leave_the_returned_graph_and_the_proof_names_the_covered_paths(tmp_path):
    t = T.twin()
    (tmp_path / "m.py").write_text(
        T.twin_text()("m.py")
    )  # the text of the line: the proof that it holds one site of the name
    st, tree = twin_cache(tmp_path, t, T.twin_index("Foo#m()."))
    notes = []
    g, proof = semuse.proof(str(tmp_path), t.graph, tree, st, notes=notes)
    assert T.edges_to(t.graph, "Bar.m") == {("use", "call", "N")}
    assert T.edges_to(g, "Bar.m") == set() and T.edges_to(g, "Foo.m") == {
        ("use", "call", "S")
    }  # reach differs: Bar.m is dead now
    assert proof.covered == frozenset({"m.py"}) and notes == []


def test_engine_matches_of_a_current_hash_corroborate_a_node_a_stale_one_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(semuse, "_version", lambda lock, tool: "2.14")  # the version the lock names now
    t = T.twin()
    st, tree = twin_cache(tmp_path, t, T.twin_index("Foo#m()."), engine="scip-python")
    h = adapters.Hit("vulture", "2.14", "m.py", 4, 0, "unused-method", "unused method 'm'", "method", "m", 100)
    m = adapters.Match(t.bar_m.key, h, True, 0)
    now = next(k.rpartition(":")[2] for k in st.engine_keys("sem:"))  # the hash of the python package of this tree
    st.put_engine_result(f"eng:vulture:{now}", "vulture", adapters.to_payload([m]))
    semcache._touch(
        st, {f"eng:vulture:{now}": ("python", "2.14")}, 1.0
    )  # the record: the version the matches were made by
    st.put_engine_result(
        "eng:vulture:" + "0" * 64, "vulture", adapters.to_payload([adapters.Match(t.foo_m.key, h, True, 0)])
    )
    notes = []
    _, proof = semuse.proof(str(tmp_path), t.graph, tree, st, notes=notes)
    assert (
        proof.text(t.bar_m.key, "x.py").startswith("vulture@2.14 unused-method")
        and proof.text(t.foo_m.key, "x.py") == ""
    )
    assert [n.code for n in notes] == [
        "dc.sem-refute"
    ]  # vulture has a current item: its older one is history, not a finding
    notes.clear()
    st.put_engine_result(f"eng:ruff:{'1' * 64}", "ruff", adapters.to_payload([]))
    notes.clear()
    semuse.proof(str(tmp_path), t.graph, tree, st, notes=notes)
    assert [f.code for f in notes if f.code != "dc.sem-refute"] == ["dc.sem-stale"] and "ruff" in notes[0].msg


def test_engine_matches_of_another_engine_version_are_ignored_with_one_w_sem_stale(tmp_path, monkeypatch):
    t = T.twin()
    st, tree = twin_cache(tmp_path, t, T.twin_index("Foo#m()."), engine="scip-python")
    h = adapters.Hit("vulture", "2.14", "m.py", 4, 0, "unused-method", "unused method 'm'", "method", "m", 100)
    now = next(k.rpartition(":")[2] for k in st.engine_keys("sem:"))
    key = f"eng:vulture:{now}"
    st.put_engine_result(key, "vulture", adapters.to_payload([adapters.Match(t.bar_m.key, h, True, 0)]))
    semcache._touch(st, {key: ("python", "2.14")}, 1.0)
    monkeypatch.setattr(semuse, "_version", lambda lock, tool: "2.14")
    notes = []
    _, proof = semuse.proof(str(tmp_path), t.graph, tree, st, notes=notes)
    assert proof.text(t.bar_m.key, "x.py").startswith("vulture@2.14") and [n.code for n in notes] == ["dc.sem-refute"]
    notes.clear()
    monkeypatch.setattr(semuse, "_version", lambda lock, tool: "2.15")  # the lock names a newer engine now
    _, proof = semuse.proof(str(tmp_path), t.graph, tree, st, notes=notes)
    assert proof.text(t.bar_m.key, "x.py") == ""
    stale = [f for f in notes if f.code == "dc.sem-stale"]
    assert len(stale) == 1 and "vulture" in stale[0].msg
    semcache._save(st, {})  # no record: the version is unknown, the item is no proof either
    monkeypatch.setattr(semuse, "_version", lambda lock, tool: "2.14")
    notes.clear()
    _, proof = semuse.proof(str(tmp_path), t.graph, tree, st, notes=notes)
    assert proof.text(t.bar_m.key, "x.py") == "" and [f.code for f in notes if f.code == "dc.sem-stale"] == [
        "dc.sem-stale"
    ]


def test_verdict_both_keeps_a_proof_that_decides_by_its_own_rule():
    plain = verdict.Proof(covered=frozenset({"a.py"}), nodes={"k": "engine hit"})

    class Everything(verdict.Proof):
        def text(self, key, path):
            return "given"

    assert verdict.both(plain, verdict.Proof()) is plain and verdict.both(verdict.Proof(), plain) is plain
    merged = verdict.both(plain, verdict.Proof(nodes={"j": "other"}))
    assert (
        merged.text("k", "z.py") == "engine hit"
        and merged.text("j", "z.py") == "other"
        and merged.covered == plain.covered
    )
    j = verdict.both(Everything(), plain)
    assert j.text("q", "q.py") == "given" and verdict.both(plain, Everything()).text("k", "q.py") == "engine hit"
    assert verdict.both(plain, Everything()).text("zz", "q.py") == "given"


class _CountingLangs:
    """langs.load() with the number of distinct files detect() was asked about."""

    def __init__(self, real):
        self.real, self.asked, self.langs, self.family = real, set(), real.langs, real.family

    def detect(self, path):
        self.asked.add(path)
        return self.real.detect(path)


def test_a_deadline_stops_the_package_hashes_of_a_big_tree_one_w_and_the_graph_is_untouched(tmp_path, monkeypatch):
    st = dcstore.Store(str(tmp_path / "g.sqlite"))
    st.put_engine_result("sem:scip-python::" + "0" * 64, "scip-python", b"{}")
    tree = {f"pkg/m{i}.py": f"b{i}" for i in range(6000)}
    counting = _CountingLangs(semuse._langs.load())
    monkeypatch.setattr(semuse._langs, "load", lambda: counting)
    ticks = []
    clock = lambda: ticks.append(1) or float(len(ticks))  # noqa: E731 - a clock that advances one second per question
    notes = []
    graph = object()
    g, proof = semuse.proof("", graph, tree, st, notes=notes, deadline=3.0, clock=clock)
    assert (
        g is graph and proof.covered == frozenset() and [(f.level, f.code) for f in notes] == [("W", "dc.sem-budget")]
    )
    assert 0 < len(counting.asked) < 1500  # asked every 512 files, stopped at the second look: not the 6000 of the tree
    counting.asked.clear()
    notes.clear()
    semuse.proof("", graph, tree, st, notes=notes)  # no deadline: the whole tree is hashed
    assert len(counting.asked) == 6000 and [f.code for f in notes] == ["dc.sem-stale"]


def test_an_index_that_does_not_fit_into_what_is_left_of_the_budget_is_not_applied_a_w_names_it(tmp_path, monkeypatch):
    t = T.twin()
    (tmp_path / "m.py").write_text(T.twin_text()("m.py"))
    st, tree = twin_cache(tmp_path, t, T.twin_index("Foo#m()."))
    notes = []
    g, proof = semuse.proof(str(tmp_path), t.graph, tree, st, notes=notes, deadline=100.0, clock=lambda: 90.0)
    assert (
        g is not t.graph and proof.covered == frozenset({"m.py"}) and notes == []
    )  # 10 s left: a few hundred bytes fit
    real = sem.config()
    monkeypatch.setattr(
        sem, "config", lambda: {**real, "apply_bytes_per_s": 10}
    )  # a very slow host: 30 s for this index
    notes = []
    g, proof = semuse.proof(str(tmp_path), t.graph, tree, st, notes=notes, deadline=100.0, clock=lambda: 90.0)
    assert g is t.graph and proof.covered == frozenset()
    (w,) = notes
    assert (w.level, w.code) == ("W", "dc.sem-budget") and "not applied" in w.msg and "10 s are left" in w.msg
    semuse.proof(str(tmp_path), t.graph, tree, st, notes=(n := []))  # no deadline (view, why, tree): always applied
    assert n == [] and sem.config()["apply_bytes_per_s"] == 10


def test_a_cache_that_cannot_be_read_is_a_w_never_an_e_and_the_graph_survives(tmp_path, monkeypatch):
    t = T.twin()
    st, tree = twin_cache(tmp_path, t, T.twin_index("Foo#m()."))

    def boom(prefix):
        raise OSError("disk I/O error")

    monkeypatch.setattr(st, "engine_keys", boom)
    notes = []
    g, proof = semuse.proof(str(tmp_path), t.graph, tree, st, notes=notes)
    assert g is t.graph and [(f.level, f.code) for f in notes] == [("W", "dc.check-failed")]
    assert "OSError" in notes[0].msg and proof.covered == frozenset() and proof.nodes == {}
    assert semuse.proof(str(tmp_path), t.graph, tree, None)[0] is t.graph  # no store: the graph as it was


def test_paths_limit_the_packages_that_are_read_and_a_stale_item_elsewhere_is_not_named(tmp_path):
    t = T.twin()
    st, tree = twin_cache(tmp_path, t, T.twin_index("Foo#m()."))
    (key,) = st.engine_keys("sem:")
    st.put_engine_result(
        "sem:scip-python:svc:" + "0" * 64, "scip-python", st.get_engine_result(key)
    )  # another package, stale
    notes = []
    semuse.proof(str(tmp_path), t.graph, tree, st, notes=notes, paths={"m.py"})
    assert not [f for f in notes if f.code == "dc.sem-stale"]  # svc holds none of the paths asked about
