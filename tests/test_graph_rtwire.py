"""The stored runtime run in `graph check` (graph.runtime.view, replace_claims, ran_map) and in the second axis (graph.confidence): the repo level
and the branch level read the newest stored run of the project, a symbol that ran is not reported dead (I dc.alive-runtime), a stale run is
still used and says so, and the confidence of a claim moves by the data of graph.confidence, never its level or code. `graph check` never
contacts a target and never starts a run.

python3 -m pytest -q -p no:cacheprovider tests/test_graph_rtwire.py

The run is a stored document written by the test (what the box of the target would have delivered); the repositories are synthetic and written in
the toy language of tests/graph_support.py; nothing is run.
"""

import os
import time
from pathlib import Path

import pytest
from graph_rt_target import Guard, row, stored_doc, write_cfg
from graph_support import inline_units, new_repo, write

from dead_code_checker.graph import confidence, levels, reach, runtime, vcs, verdict
from dead_code_checker.graph import repo as dcrepo
from dead_code_checker.graph.model import Finding

pytestmark = [pytest.mark.usefixtures("hermetic_git", "data_root")]
HOST = "target.invalid"


@pytest.fixture
def env(data_root):
    return {"PATH": os.environ["PATH"], "DEAD_CODE_CHECKER_HOME": str(data_root)}


DEAD = "def orphan\nref gone\ndef gone\ndef lone\n"
APP = "def helper\ndef run\nref helper\ndef main\nref run\nroot main main\n"
ROOTS = "main file:lib/app.dcref\n"


@pytest.fixture
def dead_repo(tmp_path, dcref):
    return new_repo(tmp_path, {"lib/app.dcref": APP, "lib/dead.dcref": DEAD, "run.dcroots": ROOTS}, name="dr")


class Proven(verdict.Proof):
    def text(self, key, path):
        return "test: corroborated"


def check_repo(r, **kw):
    """The repo level of `graph check` on the repository (every dead claim corroborated: W)."""
    return dcrepo.check(str(r), proof=Proven(), **kw)


def dead_claims(rep, path="lib/dead.dcref"):
    return sorted((f.level, f.code, f.msg.split("`")[1]) for f in rep.findings if f.path == path and "`" in f.msg)


def blob_of(r, tree=None):
    return lambda p: vcs.blob_sha((Path(r) / p).read_bytes()) if (Path(r) / p).is_file() else None


def graph_of(r):
    b = levels.build(str(r), None)
    return b, reach.analyze(b.graph, "closed_world")


def test_a_symbol_that_ran_is_not_reported_dead_and_the_rest_is_not_touched(dead_repo, env):
    r = dead_repo
    base = dead_claims(check_repo(r))
    assert base == [
        ("W", "dc.dead-cluster", "gone"),
        ("W", "dc.dead-cluster", "orphan"),
        ("W", "dc.introduced-dead", "lone"),
    ]
    b, _ = graph_of(r)
    doc = stored_doc(r, [row("lib/dead.dcref", 4, "lone")])
    alive, stale = runtime.alive(b.graph.nodes, doc, blob_of(r), "R1")
    assert stale == 0 and [k.rpartition("#")[2] for k in alive] == ["lone"]
    runtime.store_run(str(r), doc, "t@h", vcs.head_commit(str(r)), time.time(), env)
    rep = check_repo(r)
    got = dead_claims(rep)
    assert [g for g in got if g[2] == "lone"] == [("I", "dc.alive-runtime", "lone")]
    assert [g for g in got if g[2] != "lone"] == [g for g in base if g[2] != "lone"]  # the others are as before
    f = next(f for f in rep.findings if f.code == runtime.ALIVE)
    assert "ran at runtime (run 2" in f.msg and f.path == "lib/dead.dcref" and f.line == 4 and f.level == "I"
    assert rep.meta["runtime"].startswith("runtime: 1 symbol(s) of the graph ran in ")
    assert not [f for f in rep.findings if f.code == runtime.STALE]  # a run of today is not stale


def test_one_member_of_a_dead_cluster_that_ran_withdraws_the_whole_cluster(dead_repo, env):
    r = dead_repo
    runtime.store_run(str(r), stored_doc(r, [row("lib/dead.dcref", 3, "gone")]), "t@h", "abcdef01", time.time(), env)
    got = dead_claims(check_repo(r))
    assert [g[:2] for g in got if g[2] in ("gone", "orphan")] == [("I", "dc.alive-runtime")] * 2
    assert [g[:2] for g in got if g[2] == "lone"] == [
        ("W", "dc.introduced-dead")
    ]  # the lone symbol did not run: as before
    msg = next(f.msg for f in check_repo(r).findings if f.code == runtime.ALIVE and "orphan" in f.msg)
    assert "dead cluster of 2 symbols one of which, `gone`, ran at runtime" in msg


def test_a_symbol_that_did_not_run_adds_nothing(dead_repo, env):
    r = dead_repo
    base = dead_claims(check_repo(r))
    runtime.store_run(str(r), stored_doc(r, [row("lib/app.dcref", 1, "helper")]), "t@h", "abcdef01", time.time(), env)
    assert dead_claims(check_repo(r)) == base  # a hit on a live symbol changes nothing about the dead ones


def test_no_stored_run_no_change_and_the_branch_level_reads_it_too(dead_repo, env):
    r = dead_repo
    rep = check_repo(r)
    assert "runtime" not in rep.meta and not [f for f in rep.findings if f.code in (runtime.ALIVE, runtime.STALE)]
    write(r, "lib/new.dcref", "def fresh\n")
    before = levels.branch(str(r), "HEAD", proof=Proven())
    assert [f.code for f in before.findings if "`fresh`" in f.msg] == ["dc.introduced-dead"]
    runtime.store_run(
        str(r),
        stored_doc(r, [row("lib/new.dcref", 1, "fresh")], files={"lib/new.dcref": vcs.blob_sha(b"def fresh\n")}),
        "t@h",
        "abcdef01",
        time.time(),
        env,
    )
    after = levels.branch(str(r), "HEAD", proof=Proven())
    assert [(f.level, f.code) for f in after.findings if "`fresh`" in f.msg] == [("I", "dc.alive-runtime")]


def test_a_stale_run_is_still_used_and_one_note_says_so(dead_repo, env):
    r = dead_repo
    runtime.store_run(
        str(r), stored_doc(r, [row("lib/dead.dcref", 4, "lone")]), "t@h", "abcdef01", time.time() - 30 * 86400, env
    )
    rep = check_repo(r)
    stale = [f for f in rep.findings if f.code == runtime.STALE]
    assert (
        len(stale) == 1
        and stale[0].level == "I"
        and "30 days old" in stale[0].msg
        and "refresh_days = 7" in stale[0].msg
    )
    assert [g[:2] for g in dead_claims(rep) if g[2] == "lone"] == [("I", "dc.alive-runtime")]  # used all the same
    write_cfg(r, "[graph.runtime]\nrefresh_days = 60\n")
    assert not [f for f in check_repo(r).findings if f.code == runtime.STALE]


def test_a_file_that_changed_since_the_run_is_not_used_and_is_counted(dead_repo, env):
    r = dead_repo
    runtime.store_run(str(r), stored_doc(r, [row("lib/dead.dcref", 4, "lone")]), "t@h", "abcdef01", time.time(), env)
    (r / "lib" / "dead.dcref").write_text(DEAD + "# changed\n")
    rep = check_repo(r)
    assert not [f for f in rep.findings if f.code == runtime.ALIVE]
    assert "1 file(s) changed since, their hits are not used" in rep.meta["runtime"]


def test_a_hit_matches_the_symbols_whose_span_holds_the_first_line_of_the_function_that_ran(dead_repo):
    r = dead_repo
    b, _ = graph_of(r)
    names = lambda doc: sorted(k.rpartition("#")[2] for k in runtime.alive(b.graph.nodes, doc, blob_of(r))[0])  # noqa: E731
    assert names(stored_doc(r, [row("lib/app.dcref", 1, "helper")])) == ["helper"]
    assert names(stored_doc(r, [row("lib/dead.dcref", 1, "orphan")])) == ["orphan"]
    assert names(stored_doc(r, [row("lib/app.dcref", 99, "ghost")])) == []  # a line outside every span
    assert (
        names(stored_doc(r, [row("other.py", 1)], files={"other.py": "a" * 40})) == []
    )  # a file the graph does not have


def test_the_map_has_true_for_what_ran_false_for_the_rest_of_a_covered_file_and_nothing_else(dead_repo):
    r = dead_repo
    b, _ = graph_of(r)
    doc = stored_doc(r, [row("lib/dead.dcref", 4, "lone")])
    got = {k.rpartition("#")[2]: v for k, v in runtime.ran_map(b.graph.nodes, doc, blob_of(r)).items()}
    assert got == {
        "lone": True,
        "orphan": False,
        "gone": False,
    }  # the file is in the run: its other symbols did not run
    assert not any("lib/app.dcref" in k for k in runtime.ran_map(b.graph.nodes, doc, blob_of(r)))
    doc["files"]["lib/dead.dcref"] = "0" * 40  # the file changed since: its lines moved, the run says nothing about it
    assert runtime.ran_map(b.graph.nodes, doc, blob_of(r)) == {}
    assert runtime.ran_map(b.graph.nodes, {"files": {}, "executed": []}, blob_of(r)) == {}


def test_the_stored_run_is_the_runtime_input_of_the_confidence_and_moves_a_number_only(dead_repo, env, monkeypatch):
    r = dead_repo
    base = {
        g: f.confidence for f in check_repo(r).findings if f.path == "lib/dead.dcref" and (g := f.msg.split("`")[1])
    }
    assert set(base) == {"lone", "orphan", "gone"} and all(v is not None for v in base.values())
    seen = []
    real = confidence.annotate
    monkeypatch.setattr(
        confidence, "annotate", lambda fs, g, p, gaps, rt=None: seen.append(rt) or real(fs, g, p, gaps, rt)
    )
    check_repo(r)
    assert seen == [None]  # no stored run: the seam gets no map
    runtime.store_run(str(r), stored_doc(r, [row("lib/dead.dcref", 4, "lone")]), "t@h", "abcdef01", time.time(), env)
    rep = check_repo(r)
    (ran,) = seen[1:]
    assert sorted(ran.values()) == [False, False, True] and all(k.endswith(("#lone", "#orphan", "#gone")) for k in ran)
    bump = confidence.table()["runtime"]["not_executed"]
    now = {
        f.msg.split("`")[1]: (f.level, f.code, f.confidence)
        for f in rep.findings
        if f.path == "lib/dead.dcref" and f.code != runtime.ALIVE
    }
    assert set(now) == {"orphan", "gone"}  # `lone` ran: no claim at all
    for q in now:
        assert (
            now[q][2] == min(100, base[q] + bump) and now[q][1] == "dc.dead-cluster"
        )  # the file ran and these did not: a little surer


def test_a_claim_about_a_symbol_that_ran_loses_its_confidence_when_it_is_still_there(dead_repo):
    r = dead_repo
    b, _ = graph_of(r)
    key = next(n.key for n in b.graph.nodes if n.key.endswith("#lone"))
    mk = lambda: Finding("W", "lib/dead.dcref", 4, "dc.dead", "`lone` is dead: no path from any root reaches it")  # noqa: E731
    plain, ran, covered_not_run = mk(), mk(), mk()
    confidence.annotate([plain], b.graph, Proven(), ())
    confidence.annotate([ran], b.graph, Proven(), (), {key: True})
    confidence.annotate([covered_not_run], b.graph, Proven(), (), {key: False})
    spec = confidence.table()["runtime"]
    assert plain.confidence is not None and plain.conf_note != ran.conf_note
    assert ran.confidence == max(0, plain.confidence + spec["executed"])
    assert covered_not_run.confidence == min(100, plain.confidence + spec["not_executed"])


def test_replace_claims_only_touches_dead_claims_about_symbols_that_ran(dead_repo):
    r = dead_repo
    b, res = graph_of(r)
    keep = Finding("W", "lib/dead.dcref", 4, "dc.engine-missing", "`lone` something else")
    claim = Finding("W", "lib/dead.dcref", 4, "dc.dead", "`lone` is dead")
    other = Finding("W", "lib/dead.dcref", 1, "dc.dead", "`orphan` is dead")
    key = next(n.key for n in b.graph.nodes if n.key.endswith("#lone"))
    got = runtime.replace_claims([keep, claim, other], b.graph, res, {key: "run R"})
    assert got[0] is keep and got[2] is other and (got[1].level, got[1].code) == ("I", runtime.ALIVE)
    assert runtime.replace_claims([claim], b.graph, res, {}) == [claim]


def test_a_broken_stored_run_never_breaks_the_check(dead_repo, env, monkeypatch):
    r = dead_repo
    monkeypatch.setattr(runtime, "view", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rep = check_repo(r)
    assert any(f.code == "dc.check-failed" and "runtime" in f.msg for f in rep.findings)
    assert dead_claims(rep)  # the claims of the graph are all there


def test_graph_check_never_contacts_a_target(dead_repo, env, monkeypatch):
    r = dead_repo
    write_cfg(r, f'[graph.runtime]\nenabled = true\nhost = "{HOST}"\n')
    runtime.store_run(str(r), stored_doc(r, [row("lib/dead.dcref", 4, "lone")]), "t@h", "abcdef01", time.time(), env)
    inline_units(monkeypatch)  # pools in threads: the guard below stops os.fork
    guard = Guard(monkeypatch, allow=("git",))
    monkeypatch.setattr(
        runtime, "make_transport", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a transport was made"))
    )
    monkeypatch.setattr(runtime, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a run was started")))
    rep = check_repo(r)
    assert [f for f in rep.findings if f.code == runtime.ALIVE] and set(guard.started) <= {"git"}
