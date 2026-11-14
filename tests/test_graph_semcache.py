"""graph.semcache: the semantic cache in the store, its keys and payloads, the incremental
lookup, the overlay over the cached indexes and the garbage collection.

python3 -m pytest -q tests/test_graph_semcache.py
The index and the graph are those of the committed py fixture of test_graph_sem (the real output of scip-python); nothing is started
and nothing touches the network.
"""

import base64
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_graph_sem import copy_fixture, edges_to, py_facts, text_in, tree_of
from dead_code_checker.graph import adapters, engines as dce, jobs, link, sem, semcache
from dead_code_checker.graph import store as dcs

ROOT = Path(__file__).resolve().parent.parent

DAY = 86400.0


@pytest.fixture
def store(tmp_path):
    s = dcs.Store(str(tmp_path / "g.sqlite"))
    yield s
    s.close()


@pytest.fixture
def world(tmp_path):
    base = copy_fixture(tmp_path, "py")
    return base, tree_of(base), link.link(py_facts(base), workers=1).graph, (base / "index.scip").read_bytes()


def task(engine, rel="", index=True):
    j = jobs.Job(f"{engine}@{rel or '.'}", engine, ("x",), rel, "stdout", "", 1, index, (), (), index)
    return jobs.Task(j.label, 1, j)


def hit(tool="vulture", path="pkg/app.py", line=5):
    return adapters.Hit(tool, "2.14", path, line, 0, "unused-function", "unused function", "func", "x", 100)


# ---------------------------------------------------------------- stamp, store, reload
def test_an_index_is_stamped_stored_under_its_hash_and_read_back_whole(store, world):
    _, tree, _, data = world
    key = semcache.put_index(store, tree, "scip-python", "", data, "0.6.8", now=100.0)
    m = sem.meta_for(data, tree, "", "python", "scip-python")
    assert key == f"sem:scip-python::{m.input_hash}" == semcache.sem_key("scip-python", "", m.input_hash)
    body = json.loads(store.get_engine_result(key))
    assert set(body) == {"meta", "index"} and body["meta"] == m._asdict()
    assert base64.b64decode(body["index"]) == data and m.index_sha256 == hashlib.sha256(data).hexdigest()
    assert semcache.get_index(store, key) == (m, data)
    assert json.loads(store.get_meta(semcache.RECORD))[key] == {"fam": "python", "t": 100.0, "ver": "0.6.8"}


def test_a_package_root_is_part_of_the_key_and_the_hash(store, world):
    _, tree, _, data = world
    moved = {"svc/" + p: b for p, b in tree.items()}
    a = semcache.put_index(store, tree, "scip-python", "", data)
    b = semcache.put_index(store, moved, "scip-python", "svc", data)
    assert a != b and b.startswith("sem:scip-python:svc:")
    assert semcache.get_index(store, b)[0].root == "svc"


def test_a_missing_damaged_or_foreign_item_is_a_miss_never_an_error(store, world):
    _, tree, _, data = world
    key = semcache.put_index(store, tree, "scip-python", "", data)
    assert semcache.get_index(store, "sem:scip-python::nope") is None
    body = json.loads(store.get_engine_result(key))
    for bad in (
        b"not json",
        b"[]",
        json.dumps(dict(body, index="@@@")).encode(),
        json.dumps(dict(body, index=base64.b64encode(b"other").decode())).encode(),  # not the stamped blob
        json.dumps({"meta": {"engine": "x"}, "index": ""}).encode(),
    ):
        store.put_engine_result(key, "scip-python", bad)
        assert semcache.get_index(store, key) is None


def test_an_engine_without_a_family_has_no_package_and_is_not_cached(store, world):
    _, tree, _, data = world
    assert semcache.family_of("ast-grep") == "" and semcache.family_of("nonesuch") == ""
    assert semcache.family_of("scip-python") == "python" and semcache.family_of("rust-analyzer") == "rust"
    assert semcache.family_of("scip-dotnet") == "csharp" and semcache.family_of("scip-typescript") == "js"
    assert semcache.put_index(store, tree, "ast-grep", "", data) is None and store.engine_keys("") == []


def test_an_index_above_the_limit_of_the_overlay_is_refused_with_the_reason(store, world, monkeypatch):
    _, tree, _, data = world
    monkeypatch.setitem(sem.config(), "max_index_bytes", len(data) - 1)
    with pytest.raises(sem.SemError, match="above the limit"):
        semcache.put_index(store, tree, "scip-python", "", data)
    assert store.engine_keys("sem:") == []


def test_matches_of_a_tool_round_trip_under_the_hash_of_its_family(store, world):
    _, tree, _, _ = world
    ms = [
        adapters.Match("python:pkg/app.py::use", hit(), True, 0),
        adapters.Match("python:pkg/app.py::x", hit(line=9), False, 0),
    ]
    key = semcache.put_matches(store, tree, "vulture", ms, "2.14", now=5.0)
    assert key == semcache.eng_key("vulture", sem.package_hash(sem.package_files(tree, "", "python")))
    assert store.get_engine_result(key) == adapters.to_payload(ms)  # the payload of the format, nothing around it
    assert semcache.get_matches(store, key) == adapters.from_payload(adapters.to_payload(ms))
    assert semcache.put_matches(store, tree, "vulture", [], "2.14") == key and semcache.get_matches(store, key) == []
    store.put_engine_result(key, "vulture", b"{broken")
    assert semcache.get_matches(store, key) is None and semcache.get_matches(store, "eng:vulture:none") is None


# ---------------------------------------------------------------- incremental
def test_a_package_with_a_cached_index_is_skipped_and_a_changed_one_is_refreshed(store, world, monkeypatch):
    _, tree, _, data = world
    ver = ["1.0"]
    monkeypatch.setattr(dce, "lock_entry", lambda lock, name, plat=None: {"version": ver[0]})
    t = task("scip-python")
    run, rows = semcache.split_cached(store, tree, [t], ("index",), None)
    assert run == [t] and rows == []  # nothing cached yet
    semcache.put_index(store, tree, "scip-python", "", data, "1.0", now=1.0)
    run, rows = semcache.split_cached(store, tree, [t], ("index",), None)
    assert run == [] and [(r["engine"], r["label"], r["status"]) for r in rows] == [
        ("scip-python", "scip-python@.", "cached")
    ]
    assert [v["t"] for v in json.loads(store.get_meta(semcache.RECORD)).values()][0] > 1.0  # a hit keeps it alive
    ver[0] = "1.1"  # the indexer was upgraded: the same files, another engine
    assert semcache.split_cached(store, tree, [t], ("index",), None)[0] == [t]
    ver[0] = "1.0"
    changed = dict(tree, **{"pkg/app.py": "0" * 40})  # one file of the package changed
    assert semcache.split_cached(store, changed, [t], ("index",), None)[0] == [t]
    unrelated = dict(tree, **{"docs/readme.md": "1" * 40})  # a file that is no input of the python package
    assert semcache.split_cached(store, unrelated, [t], ("index",), None)[0] == []
    assert semcache.split_cached(store, tree, [t], (), None)[0] == [t]  # no `reuse`: everything runs


def test_only_the_package_that_changed_is_run_again(store, world):
    _, tree, _, data = world
    both = {**tree, **{"svc/" + p: b for p, b in tree.items()}}
    for root in ("", "svc"):
        semcache.put_index(store, both, "scip-python", root, data)
    a, b = task("scip-python", ""), task("scip-python", "svc")
    assert semcache.split_cached(store, both, [a, b], ("index",), None)[0] == []
    outside = {**both, "pkg/shapes.py": "2" * 40}  # a file of the root package, outside svc/
    assert semcache.split_cached(store, outside, [a, b], ("index",), None)[0] == [a]
    inside = {**both, "svc/pkg/shapes.py": "2" * 40}  # the root package holds every .py below it, svc/ included
    assert semcache.split_cached(store, inside, [a, b], ("index",), None)[0] == [a, b]


def test_an_engine_job_is_cached_by_its_tool_and_only_with_eng_in_reuse(store, world):
    _, tree, _, _ = world
    t = task("vulture", index=False)
    semcache.put_matches(store, tree, "vulture", [], "")
    assert semcache.split_cached(store, tree, [t], ("index",), None)[0] == [
        t
    ]  # an indexer-only reuse never skips an engine
    run, rows = semcache.split_cached(store, tree, [t], ("index", "eng"), None)
    assert run == [] and rows[0]["status"] == "cached"


def test_an_engine_of_several_families_is_stamped_with_the_family_of_its_package(store, monkeypatch):
    kt = {"app/Main.kt": "1" * 40, "app/build.gradle.kts": "2" * 40}
    jv = {"app/Main.java": "3" * 40, "pom.xml": "4" * 40}
    import copy

    two = copy.deepcopy(dce.load_registry())
    for fam in ("java", "kotlin"):  # an engine of two families (a JVM indexer): the registry shipped here has none
        two["languages"][fam]["roles"].setdefault("sem", []).append({"engine": "cs"})
    monkeypatch.setattr(dce, "load_registry", lambda path=None: two)
    assert semcache.family_of("cs", two) == "java"  # nothing to look at: the first family of the registry
    assert semcache.family_of("cs", two, jv) == "java" and semcache.family_of("cs", two, kt, "app") == "kotlin"
    assert semcache.family_of("cs", two, kt, "other") == "java"  # no file of either family there
    assert semcache.family_of("scip-python", files=kt) == "python" and semcache.family_of("ast-grep") == ""
    key = semcache.put_index(
        store, kt, "cs", "app", b"x"
    )  # a kotlin package is hashed over kotlin files, not java ones
    assert key == semcache.sem_key("cs", "app", sem.package_hash(sem.package_files(kt, "app", "kotlin")))
    assert json.loads(store.get_meta(semcache.RECORD))[key]["fam"] == "kotlin"
    assert (
        semcache.split_cached(store, kt, [task("cs", "app")], ("index",), None)[0] == []
    )  # and found again by its job


def test_a_damaged_cached_index_is_not_cached_so_its_job_runs_and_writes_it_again(store, world, monkeypatch):
    _, tree, _, data = world
    monkeypatch.setattr(dce, "lock_entry", lambda lock, name, plat=None: {"version": "1"})
    key, t = semcache.put_index(store, tree, "scip-python", "", data, "1"), task("scip-python")
    assert semcache.split_cached(store, tree, [t], ("index",), None)[0] == []
    body = json.loads(store.get_engine_result(key))
    for bad in (
        b"junk",
        json.dumps(dict(body, index=base64.b64encode(b"other").decode())).encode(),
    ):  # unreadable; not the stamped bytes
        store.put_engine_result(key, "scip-python", bad)
        assert semcache.get_index(store, key) is None
        assert semcache.split_cached(store, tree, [t], ("index",), None)[0] == [t]  # not held: the indexer runs
    semcache.put_index(store, tree, "scip-python", "", data, "1")  # what the job stores
    assert semcache.get_index(store, key) == (sem.Meta(**body["meta"]), data)
    assert semcache.split_cached(store, tree, [t], ("index",), None)[0] == []


def test_a_hit_and_a_store_take_the_clock_of_the_caller(store, world, monkeypatch):
    _, tree, _, data = world
    monkeypatch.setattr(dce, "lock_entry", lambda lock, name, plat=None: {"version": "1"})
    monkeypatch.setattr(time, "time", lambda: pytest.fail("the host clock was read"))
    semcache.put_index(store, tree, "scip-python", "", data, "1", now=100.0)
    semcache.split_cached(store, tree, [task("scip-python")], ("index",), None, now=500.0)
    assert {v["t"] for v in json.loads(store.get_meta(semcache.RECORD)).values()} == {500.0}


def test_a_row_and_its_record_are_stored_together_or_not_at_all(store, world, monkeypatch):
    _, tree, _, data = world

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(semcache, "_save", boom)
    with pytest.raises(OSError):
        semcache.put_index(store, tree, "scip-python", "", data)
    with pytest.raises(OSError):
        semcache.put_matches(store, tree, "vulture", [])
    assert store.engine_keys("") == []  # a row without a record would be collected by the next gc: neither stays


def test_keeping_the_matches_of_the_tools_collects_the_cache(store, world):
    _, tree, _, _ = world
    eng = SimpleNamespace(runs=[SimpleNamespace(tool="vulture", ver="2.14", rc=0)], down=())
    for n in range(5):
        files = dict(tree, **{"pkg/app.py": f"{n}" * 40})
        assert semcache.keep_matches(store, files, eng, [], now=1000.0 + n) == 1
    assert len(store.engine_keys("eng:vulture:")) == semcache.KEEP  # the gc is part of keeping, no caller can forget it


# ---------------------------------------------------------------- the overlay
def test_the_overlay_applies_the_cached_index_of_the_current_files(store, tmp_path, world):
    base, tree, g, data = world
    semcache.put_index(store, tree, "scip-python", "", data)
    r = semcache.overlay(store, g, tree, text_in(base))
    assert [(x.status, x.coverage) for x in r.reports] == [("applied", 1.0)]
    assert edges_to(r.graph, "Alpha.ping") == {("use", "call", "S")}
    assert r.covered == frozenset(k for k in tree if k.endswith(".py"))
    assert all("dc-sem-" not in f.msg and str(tmp_path) not in f.msg for f in r.findings)  # no temporary name in a W
    assert [x.index for x in r.reports] == ["scip-python/index.scip"]  # the same text every night


def test_the_overlay_of_an_empty_cache_is_the_graph_itself(store, world):
    _, tree, g, _ = world
    r = semcache.overlay(store, g, tree)
    assert r.graph is g and r.findings == () and r.reports == () and r.covered == frozenset()


def test_an_index_of_older_files_is_not_applied_and_the_package_is_named_once(store, world):
    base, tree, g, data = world
    semcache.put_index(store, tree, "scip-python", "", data)
    now = dict(tree, **{"pkg/app.py": "9" * 40})
    r = semcache.overlay(store, g, now, text_in(base))
    assert r.graph is g and r.reports == () and r.covered == frozenset()
    (f,) = r.findings
    assert (f.level, f.code) == ("W", "dc.sem-stale") and "scip-python" in f.msg and "graph index" in f.msg
    semcache.put_index(
        store, now, "scip-python", "", data
    )  # the new hash is cached next to the old one: nothing is stale
    assert not [x for x in semcache.overlay(store, g, now, text_in(base)).findings if x.code == "dc.sem-stale"]


def test_a_package_that_is_gone_is_not_named_stale_every_night(store, world):
    base, tree, g, data = world
    moved = dict(tree, **{"svc/pkg/app.py": tree["pkg/app.py"]})
    semcache.put_index(store, moved, "scip-python", "svc", data)  # an index of the package svc/ ...
    here = semcache.overlay(store, g, dict(moved, **{"svc/pkg/app.py": "7" * 40}), text_in(base))
    assert [f.code for f in here.findings] == ["dc.sem-stale"]  # ... changed: stale, as before
    gone = semcache.overlay(store, g, tree, text_in(base))  # ... deleted (or renamed to svc2/): nothing to be stale for
    assert gone.findings == () and gone.graph is g
    renamed = {("svc2/" + p[4:] if p.startswith("svc/") else p): b for p, b in moved.items()}
    assert not [f for f in semcache.overlay(store, g, renamed, text_in(base)).findings if f.code == "dc.sem-stale"]


def test_a_damaged_cached_item_is_a_w_not_a_crash(store, world):
    base, tree, g, data = world
    key = semcache.put_index(store, tree, "scip-python", "", data)
    store.put_engine_result(key, "scip-python", b"junk")
    r = semcache.overlay(store, g, tree, text_in(base))
    assert r.graph is g and [f.code for f in r.findings] == ["dc.sem-index"]


def test_a_record_that_is_not_a_record_is_an_empty_cache(store, world):
    _, tree, g, _ = world
    for bad in ("{oops", "[]", '{"k": 1}', '{"k": {"t": "x", "fam": "python"}}'):
        store.set_meta(semcache.RECORD, bad)
        assert semcache.overlay(store, g, tree).graph is g


def test_one_malformed_entry_of_the_record_costs_that_entry_and_no_other(store, world):
    base, tree, g, data = world
    good = semcache.put_index(store, tree, "scip-python", "", data, now=1000.0)
    tool = semcache.put_matches(store, tree, "vulture", [], "", now=1000.0)
    rec = json.loads(store.get_meta(semcache.RECORD))
    rec["sem:scip-go:svc:abc"] = {"t": "yesterday", "fam": "go"}  # not an entry
    rec["eng:shellcheck:abc"] = 3
    store.set_meta(semcache.RECORD, json.dumps(rec))
    store.put_engine_result("sem:scip-go:svc:abc", "scip-go", b"{}")
    assert set(semcache._load(store)) == {good, tool}
    assert [x.status for x in semcache.overlay(store, g, tree, text_in(base)).reports] == [
        "applied"
    ]  # the good one is used
    assert semcache.gc(store, now=1010.0) == 1  # the row of the bad entry goes as a row without a record ...
    assert sorted(store.engine_keys("")) == sorted([good, tool])  # ... and nothing else
    assert set(json.loads(store.get_meta(semcache.RECORD))) == {good, tool}


def test_deleting_engine_results_counts_the_rows_that_were_there(store):
    store.put_engine_result("eng:a:1", "a", b"1")
    store.put_engine_result("eng:a:2", "a", b"2")
    assert store.delete_engine_results(["eng:a:1", "eng:a:none", "eng:a:2", "eng:a:1"]) == 2
    assert store.delete_engine_results([]) == 0 and store.engine_keys("") == []


# ---------------------------------------------------------------- garbage collection
def test_gc_keeps_the_newest_three_hashes_per_engine_and_root_and_per_tool(store, world):
    _, tree, _, data = world
    for n in range(5):
        files = dict(tree, **{"pkg/app.py": f"{n}" * 40, "svc/pkg/app.py": f"{n}" * 40})
        semcache.put_index(store, files, "scip-python", "", data, now=1000.0 + n)
        semcache.put_index(store, files, "scip-python", "svc", data, now=1000.0 + n)
        semcache.put_matches(store, files, "vulture", [], "", now=1000.0 + n)
    other = semcache.put_matches(store, tree, "shellcheck", [], "", now=1000.0)  # another tool: counted on its own
    removed = semcache.gc(store, now=1010.0)
    assert removed == 6 and semcache.KEEP == 3
    keys = store.engine_keys("")
    assert len([k for k in keys if k.startswith("sem:scip-python::")]) == 3
    assert len([k for k in keys if k.startswith("sem:scip-python:svc:")]) == 3
    assert len([k for k in keys if k.startswith("eng:vulture:")]) == 3 and other in keys

    def key_of(n):
        files = dict(tree, **{"pkg/app.py": f"{n}" * 40, "svc/pkg/app.py": f"{n}" * 40})
        return semcache.sem_key("scip-python", "", sem.package_hash(sem.package_files(files, "", "python")))

    newest, oldest = key_of(4), key_of(0)
    assert newest in keys and oldest not in keys  # the three NEWEST stay
    assert set(json.loads(store.get_meta(semcache.RECORD))) == set(keys)  # the record follows the rows
    assert semcache.gc(store, now=1010.0) == 0


def test_gc_drops_what_is_older_than_thirty_days_and_what_has_no_record_or_no_row(store, world):
    _, tree, _, data = world
    old = semcache.put_index(store, tree, "scip-python", "", data, now=1000.0)
    young = semcache.put_matches(store, tree, "vulture", [], "", now=1000.0 + 29 * DAY)
    store.put_engine_result("sem:scip-go:svc:abc", "scip-go", b"{}")  # a row nobody recorded
    store.put_engine_result("adapter:vulture:t1", "x", b"1")  # not ours: never touched
    rec = json.loads(store.get_meta(semcache.RECORD))
    rec["sem:scip-go:svc:gone"] = {"t": 1000.0 + 29 * DAY, "fam": "go", "ver": ""}  # a record without a row
    store.set_meta(semcache.RECORD, json.dumps(rec))
    assert semcache.gc(store, now=1000.0 + 31 * DAY) == 2
    assert sorted(store.engine_keys("")) == sorted([young, "adapter:vulture:t1"])
    assert old not in store.engine_keys("") and set(json.loads(store.get_meta(semcache.RECORD))) == {young}


def test_gc_of_an_empty_store_is_zero(store):
    assert semcache.gc(store) == 0 and json.loads(store.get_meta(semcache.RECORD)) == {}
