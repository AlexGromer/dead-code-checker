"""graph.fixverify and graph.pycheck: `dead-code-checker graph fix --dry-run --verify`, the removals of the patch checked on a COPY of the
tree before they are offered.

python3 -m pytest -q tests/test_graph_fixverify.py

The search (rounds, bisect, budgets, statuses) is tested with a scripted local check; the Python check itself with real files and,
where this host has bubblewrap, in the real box; the toolchain checks with a fake host and, in the real box, with stub `go` and `tsc`
scripts on a PATH the test builds; the runtime class with a fake target. Nothing here runs project code: the fixtures carry a trap (a
module that would write a marker when it is imported or run) and the tests prove the marker never appears. No network.
"""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from graph_support import new_repo, write
from test_graph_fix import APP, STIER, applied, prove, tree_hash, with_chain

from dead_code_checker.graph import cli, engines, fix, levels, pycheck, sandbox
from dead_code_checker.graph import fixverify as fv
from dead_code_checker.graph.fixverify import Outcome, Unit

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.usefixtures("hermetic_git", "data_root"),
]


# ================================================================ helpers
@pytest.fixture
def repo(tmp_path):
    return new_repo(tmp_path, {"lib/app.py": APP})


def item(path, line, qual, code="dc.dead"):
    return fix.Item(path, line, qual, code)


def unit(path, first, last, qual, lang="python", inner=()):
    return Unit(path, first, last, (item(path, first, qual), *(item(path, first + 1, q) for q in inner)), lang)


def spec(runtime=True, **over):
    """The real table with one language `python` whose checks are the ones asked for."""
    t = fv.table()
    py = {"local": {"check": "scripted", "builtin": "pycheck"}}
    if runtime:
        py["runtime"] = {"check": "scripted-tests", "argv": ["python3", "-m", "pytest"], "baseline": True}
    t["langs"] = {"python": py, **over}
    return t


def project(tmp_path, files):
    root = tmp_path / "proj"
    for rel, text in files.items():
        write(root, rel, text)
    return str(root)


def render_names(units):
    """A render that writes the sorted names of the removed symbols: the content does not matter to a scripted check."""
    return {u.path: "# gone: " + ",".join(sorted(i.qual for i in u.items)) + "\n" for u in units}


def scripted(monkeypatch, bad=(), pairs=(), unjudged=(), calls=None):
    """Replace the local check: `bad` names fail alone, `pairs` fail together, `unjudged` names make the check unable to judge."""

    def local(self, c, units):
        names = {i.qual for u in units for i in u.items[:1]}
        if calls is not None:
            calls.append(sorted(names))
        for n in bad:
            if n in names:
                return Outcome(False, f"{n}: scripted failure")
        for a, b in pairs:
            if a in names and b in names:
                return Outcome(False, f"{a} and {b} clash")
        if names & set(unjudged):
            return Outcome(None, "scripted: the tool could not run")
        return Outcome(True)

    monkeypatch.setattr(fv.Verifier, "local", local)


def by_symbol(rep):
    return {r.qual: r for r in rep.rows}


class FakeTarget(fv.Target):
    name = "fake"

    def __init__(self, bad=(), why="", rc=0):
        self.jobs, self.bad, self.why = [], set(bad), why

    def unavailable(self):
        return self.why

    def run(self, job):
        self.jobs.append(job)
        gone = {p: t for p, t in job.edits}
        broken = [b for b in self.bad if any(b in t for t in gone.values())]
        if broken:
            return fv.RuntimeResult(True, 1, f"FAILED test_x.py::test_{broken[0]} - AttributeError")
        return fv.RuntimeResult(True, 0, "")


FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": "def a1():\n    return 1\n\n\ndef a2():\n    return 2\n",
    "pkg/b.py": "def b1():\n    return 1\n\n\ndef b2():\n    return 2\n",
    "pkg/c.py": "def c1():\n    return 1\n",
}


def units3():
    return [
        unit("pkg/a.py", 1, 2, "a1"),
        unit("pkg/a.py", 5, 6, "a2"),
        unit("pkg/b.py", 1, 2, "b1"),
        unit("pkg/b.py", 5, 6, "b2"),
        unit("pkg/c.py", 1, 2, "c1"),
    ]


# ================================================================ the table and the trust rule
def test_every_local_check_of_the_table_is_non_executing():
    t = fv.table()
    for lang in t["langs"]:
        cs = fv.checks(lang)
        if "local" in cs:
            c = cs["local"]
            assert c.builtin == "pycheck" or tuple(c.argv[:2]) in fv.NON_EXECUTING, (lang, c.name)
    runtime = {c.name for lang in t["langs"] for k, c in fv.checks(lang).items() if k == "runtime"}
    assert {
        "pytest",
        "cargo check",
        "mvn compile",
        "npm test",
        "go test",
    } <= runtime  # tests and builds are runtime only


@pytest.mark.parametrize(
    "argv",
    [
        ["cargo", "check"],
        ["mvn", "-o", "compile"],
        ["npm", "test"],
        ["pytest"],
        ["go", "test", "./..."],
        ["gradle", "build"],
    ],
)
def test_a_check_that_executes_project_code_cannot_be_configured_as_local(argv):
    bad = spec()
    bad["langs"]["rust"] = {"local": {"check": " ".join(argv), "argv": argv}}
    with pytest.raises(ValueError, match="not a non-executing check"):
        fv.checks("rust", bad)


def test_the_data_cannot_widen_the_list_of_local_commands():
    assert fv.NON_EXECUTING == (("go", "vet"), ("go", "build"), ("tsc", "--noEmit"))
    t = spec()
    t["langs"]["go"] = {"local": {"check": "go generate", "argv": ["go", "generate", "./..."]}}
    with pytest.raises(ValueError):
        fv.checks("go", t)


# ================================================================ the search
def test_a_patch_that_passes_is_checked_once_and_every_symbol_is_verified_local(tmp_path, monkeypatch):
    calls = []
    scripted(monkeypatch, calls=calls)
    rep = fv.verify(project(tmp_path, FILES), units3(), render_names, {}, spec=spec(runtime=False))
    assert len(calls) == 1 and rep.checks == 1 and len(rep.kept) == 5
    assert {r.status for r in rep.rows} == {fv.VERIFIED_LOCAL} and {r.check for r in rep.rows} == {"scripted"}
    assert all(r.klass == "local" and r.detail == "" for r in rep.rows)


def test_a_failing_patch_is_split_by_file_then_by_symbol_and_only_the_failing_symbol_is_dropped(tmp_path, monkeypatch):
    calls = []
    scripted(monkeypatch, bad=["b2"], calls=calls)
    rep = fv.verify(project(tmp_path, FILES), units3(), render_names, {}, spec=spec(runtime=False))
    rows = by_symbol(rep)
    assert (
        rows["b2"].status == fv.FAILED
        and rows["b2"].detail == "b2: scripted failure"
        and rows["b2"].check == "scripted"
    )
    assert {q for q, r in rows.items() if r.status == fv.VERIFIED_LOCAL} == {"a1", "a2", "b1", "c1"}
    assert [u.items[0].qual for u in rep.kept] == ["a1", "a2", "b1", "c1"]
    # round 0 the whole, round 1 one run per file, round 2 one per symbol of the failing file, then the survivors together
    assert calls[0] == ["a1", "a2", "b1", "b2", "c1"]
    assert sorted(calls[1:4]) == [["a1", "a2"], ["b1", "b2"], ["c1"]]
    assert sorted(calls[4:6]) == [["b1"], ["b2"]] and calls[6] == ["a1", "a2", "b1", "c1"]
    assert rep.checks == len(calls) == 7


def test_the_result_does_not_depend_on_the_worker_count(tmp_path, monkeypatch):
    scripted(monkeypatch, bad=["a2", "c1"], pairs=[("a1", "b1")])
    root = project(tmp_path, FILES)
    reps = [fv.verify(root, units3(), render_names, {}, spec=spec(runtime=False), workers=w) for w in (1, 2, 4)]
    assert reps[0] == reps[1] == reps[2]
    st = {r.qual: r.status for r in reps[0].rows}
    assert (
        st["a2"] == st["c1"] == fv.FAILED and st["b1"] == fv.FAILED and st["a1"] == fv.VERIFIED_LOCAL
    )  # b1 breaks with a1


def test_two_removals_that_pass_alone_and_fail_together_cost_the_later_one(tmp_path, monkeypatch):
    scripted(monkeypatch, pairs=[("a1", "c1")])
    rep = fv.verify(project(tmp_path, FILES), units3(), render_names, {}, spec=spec(runtime=False))
    rows = by_symbol(rep)
    assert rows["c1"].status == fv.FAILED and "together with the removals before it" in rows["c1"].detail
    assert rows["c1"].detail.endswith("a1 and c1 clash")
    assert {q for q, r in rows.items() if r.status == fv.VERIFIED_LOCAL} == {"a1", "a2", "b1", "b2"}


def test_what_the_budget_cannot_prove_is_dropped_never_kept(tmp_path, monkeypatch):
    scripted(monkeypatch, bad=["b2"])
    t = spec(runtime=False)
    t["local"]["checks"] = 3  # the whole, then two of the three files: nothing is left for the symbols
    rep = fv.verify(project(tmp_path, FILES), units3(), render_names, {}, spec=t)
    rows = by_symbol(rep)
    assert rep.checks == 3
    for q in ("b1", "b2", "c1"):  # the file runs that did not fit and the file that fails and could not be split
        assert rows[q].status == fv.FAILED
    assert all("whole patch fails" in rows[q].detail or q == "b2" for q in ("b1", "b2", "c1"))
    assert not any(r.status == fv.VERIFIED_LOCAL and r.qual == "b2" for r in rep.rows)


def test_no_round_left_means_the_survivors_are_not_kept(tmp_path, monkeypatch):
    scripted(monkeypatch, bad=["b2"])
    t = spec(runtime=False)
    t["local"]["rounds"] = 1
    rep = fv.verify(project(tmp_path, FILES), units3(), render_names, {}, spec=t)
    assert rep.kept == () and {r.status for r in rep.rows} == {fv.FAILED} and rep.checks == 1


def scripted_sets(monkeypatch, fails, calls):
    """Replace the local check: a run fails when the set of removed names is exactly one of `fails` (a set that passes as a member of a
    group may fail alone, and the other way round)."""

    def local(self, c, units):
        names = frozenset(i.qual for u in units for i in u.items[:1])
        calls.append(sorted(names))
        return Outcome(False, f"{'+'.join(sorted(names))}: scripted failure") if names in fails else Outcome(True)

    monkeypatch.setattr(fv.Verifier, "local", local)


def test_a_survivor_that_passed_only_inside_a_group_is_not_kept_when_the_search_runs_out_of_checks(
    tmp_path, monkeypatch
):
    # a1 and a2 are one file: the file run (a1+a2) passes, a1 alone would not; c1 is the other file; all three together fail
    calls = []
    scripted_sets(monkeypatch, [frozenset({"a1"}), frozenset({"a1", "a2", "c1"})], calls)
    t = spec(runtime=False)
    t["local"]["checks"] = 4  # the whole, the two files, the survivors together: nothing is left for the binary search
    us = [unit("pkg/a.py", 1, 2, "a1"), unit("pkg/a.py", 5, 6, "a2"), unit("pkg/c.py", 1, 2, "c1")]
    rep = fv.verify(project(tmp_path, FILES), us, render_names, {}, spec=t)
    assert rep.checks == 4 and len(calls) == 4 and ["a1"] not in calls  # a1 alone was never seen to pass
    assert rep.kept == () and {r.status for r in rep.rows} == {fv.FAILED}
    assert all("ran out of local budget" in r.detail for r in rep.rows)


def test_what_the_search_proved_before_the_bound_is_kept_and_the_rest_is_dropped(tmp_path, monkeypatch):
    # three files: each passes alone, b1+c1 clash; the survivors together fail, and the check that would split them does not fit
    calls = []
    scripted_sets(monkeypatch, [frozenset({"b1", "c1"}), frozenset({"a1", "b1", "c1"})], calls)
    t = spec(runtime=False)
    t["local"]["checks"] = 5  # the whole, the three files, the survivors together
    us = [unit("pkg/a.py", 1, 2, "a1"), unit("pkg/b.py", 1, 2, "b1"), unit("pkg/c.py", 1, 2, "c1")]
    rep = fv.verify(project(tmp_path, FILES), us, render_names, {}, spec=t)
    rows = by_symbol(rep)
    assert rep.checks == 5 and [u.items[0].qual for u in rep.kept] == ["a1"]  # a1 was run on its own and passed
    assert rows["a1"].status == fv.VERIFIED_LOCAL
    assert rows["b1"].status == rows["c1"].status == fv.FAILED and "ran out of local budget" in rows["b1"].detail


def test_with_checks_enough_the_first_survivor_that_fails_alone_is_the_one_dropped(tmp_path, monkeypatch):
    calls = []
    scripted_sets(monkeypatch, [frozenset({"a1"}), frozenset({"a1", "a2", "c1"})], calls)
    us = [unit("pkg/a.py", 1, 2, "a1"), unit("pkg/a.py", 5, 6, "a2"), unit("pkg/c.py", 1, 2, "c1")]
    rep = fv.verify(project(tmp_path, FILES), us, render_names, {}, spec=spec(runtime=False))
    rows = by_symbol(rep)
    assert [u.items[0].qual for u in rep.kept] == ["a2", "c1"]
    assert rows["a1"].status == fv.FAILED and rows["a1"].detail == "fails scripted on its own: a1: scripted failure"
    assert rows["a2"].status == rows["c1"].status == fv.VERIFIED_LOCAL
    assert calls[-1] == ["a2", "c1"]  # what is kept was run as it is kept


def test_a_check_that_cannot_judge_leaves_the_removals_unverified_in_the_patch(tmp_path, monkeypatch):
    scripted(monkeypatch, unjudged=["a1"])
    rep = fv.verify(project(tmp_path, FILES), units3(), render_names, {}, spec=spec(runtime=False))
    assert len(rep.kept) == 5 and {r.status for r in rep.rows} == {fv.UNVERIFIED}
    assert all("scripted: the tool could not run" in r.detail for r in rep.rows)


def test_a_symbol_inside_a_removed_one_has_the_status_of_its_removal(tmp_path, monkeypatch):
    scripted(monkeypatch, bad=["Outer"])
    us = [unit("pkg/a.py", 1, 6, "Outer", inner=("Outer.m",)), unit("pkg/c.py", 1, 2, "c1")]
    rep = fv.verify(project(tmp_path, FILES), us, render_names, {}, spec=spec(runtime=False))
    rows = by_symbol(rep)
    assert rows["Outer"].status == rows["Outer.m"].status == fv.FAILED
    assert rows["Outer.m"].detail == "goes with Outer" and rows["c1"].status == fv.VERIFIED_LOCAL


def test_without_units_nothing_runs_and_nothing_is_copied(tmp_path, monkeypatch):
    scripted(monkeypatch)
    rep = fv.verify(project(tmp_path, FILES), [], render_names, {}, spec=spec())
    assert rep == fv.Report((), (), (), 0)


# ================================================================ the runtime class: only on a target
def test_without_a_target_the_executing_step_is_skipped_with_one_line_and_nothing_runs_here(tmp_path, monkeypatch):
    scripted(monkeypatch)

    class NoRun(fv.Host):
        def run(self, *a, **k):
            raise AssertionError("a process was started on this machine")

    rep = fv.verify(
        project(tmp_path, FILES),
        units3(),
        render_names,
        {},
        fv.Options(None, "no host is configured"),
        host=NoRun(),
        spec=spec(),
    )
    assert {r.status for r in rep.rows} == {fv.VERIFIED_LOCAL}  # the local check said it, the executing one is skipped
    assert len(rep.notes) == 1
    level, text = rep.notes[0]
    assert level == "I" and text.startswith("runtime mode: no execution target") and "no host is configured" in text


def test_the_runtime_mode_line_is_said_once_and_not_repeated_in_every_row(tmp_path, monkeypatch):
    scripted(monkeypatch)
    t = spec(runtime=False, rust={"runtime": {"check": "cargo check", "argv": ["cargo", "check"], "baseline": True}})
    us = [unit("src/lib.rs", 3, 5, "old_fn", lang="rust"), unit("src/lib.rs", 8, 9, "older_fn", lang="rust")]
    rep = fv.verify(project(tmp_path, {"src/lib.rs": "x\n"}), us, render_names, {}, fv.Options(None, "off"), spec=t)
    assert {r.status for r in rep.rows} == {fv.UNVERIFIED} and len(rep.notes) == 1
    assert not any(fv.RUNTIME_SKIPPED in r.detail for r in rep.rows)
    assert all("no non-executing check for rust" in r.detail and "runtime mode note" in r.detail for r in rep.rows)


def test_a_target_that_is_not_ready_is_no_target(tmp_path, monkeypatch):
    scripted(monkeypatch)
    t = FakeTarget(why="the target does not answer (ssh timeout)")
    rep = fv.verify(project(tmp_path, FILES), units3(), render_names, {}, fv.Options(t), spec=spec())
    assert t.jobs == [] and {r.status for r in rep.rows} == {fv.VERIFIED_LOCAL}
    assert [n[1] for n in rep.notes] == [
        "runtime mode: no execution target (the target does not answer (ssh timeout)); tests and build checks were not run"
    ]


def test_with_a_target_the_removals_are_verified_runtime_and_the_job_is_data(tmp_path, monkeypatch):
    scripted(monkeypatch)
    t = FakeTarget()
    root = project(tmp_path, FILES)
    rep = fv.verify(root, units3(), render_names, {}, fv.Options(t), spec=spec())
    assert {r.status for r in rep.rows} == {fv.VERIFIED_RUNTIME} and rep.notes == ()
    assert {r.check for r in rep.rows} == {"scripted + scripted-tests"} and {r.klass for r in rep.rows} == {"runtime"}
    base, whole = t.jobs  # the tree as it is, then the whole patch
    assert (
        base.edits == () and base.root == root and base.argv == ("python3", "-m", "pytest") and base.timeout_s == 1800
    )
    assert [p for p, _ in whole.edits] == ["pkg/a.py", "pkg/b.py", "pkg/c.py"]


def test_the_target_only_sees_what_the_cheap_local_check_let_through(tmp_path, monkeypatch):
    scripted(monkeypatch, bad=["a1"])
    t = FakeTarget()
    rep = fv.verify(project(tmp_path, FILES), units3(), render_names, {}, fv.Options(t), spec=spec())
    seen = {n for j in t.jobs for _, text in j.edits for n in ("a1", "a2", "b1", "b2", "c1") if n in text}
    assert (
        "a1" not in seen and rep.rows[0].status == fv.FAILED
    )  # a1 died at the local class; it never left this machine
    assert by_symbol(rep)["a2"].status == fv.VERIFIED_RUNTIME


def test_a_failing_test_on_the_target_drops_the_symbol_with_its_first_error_line(tmp_path, monkeypatch):
    scripted(monkeypatch)
    t = FakeTarget(bad=["b2"])
    rep = fv.verify(project(tmp_path, FILES), units3(), render_names, {}, fv.Options(t), spec=spec())
    rows = by_symbol(rep)
    assert rows["b2"].status == fv.FAILED and rows["b2"].klass == "runtime" and "test_b2" in rows["b2"].detail
    assert rows["b2"].check == "scripted-tests" and rows["a1"].status == fv.VERIFIED_RUNTIME
    # every job of the target is a (path, text) edit set, never a command line of ours
    assert all(isinstance(j, fv.RuntimeJob) and isinstance(j.edits, tuple) for j in t.jobs)


# key-shaped parts are built here: a literal in the source would trip the secret scanner of the commit
AWS_KEY, ANT_KEY = "AKIA" + "ABCDEFGHIJKLMNOP", "sk-" + "ant-api03-abcdefghijklmnop"
SECRETS_IN_AN_ERROR = (
    f"AssertionError: DB_PASSWORD=hunter2hunter2 AWS {AWS_KEY} via postgres://bob:s3cr3tpw@db.example:5432/x "
    f"Authorization: Bearer abcdefghijklmnopqrstu {ANT_KEY}"
)
SECRET_PARTS = ("hunter2hunter2", AWS_KEY, "s3cr3tpw", "abcdefghijklmnopqrstu", ANT_KEY)


def test_the_first_error_line_of_a_check_never_carries_a_secret(tmp_path, monkeypatch, capsys):
    """The error line that comes back from a check or from the target (a test that prints its environment, a URL with a password) is
    masked wherever it enters: the local tool, the target, the rows, the summary of fix."""
    assert not any(x in fv._first_error("", SECRETS_IN_AN_ERROR) for x in SECRET_PARTS)  # a local tool's output
    assert not any(x in fv._first_error(SECRETS_IN_AN_ERROR, "") for x in SECRET_PARTS)
    assert (
        fv._first_error("", "pkg/a.py:3: error: name 'x' is not defined")
        == "pkg/a.py:3: error: name 'x' is not defined"
    )  # ordinary lines stay
    scripted(monkeypatch)

    class Leaky(FakeTarget):
        def run(self, job):
            self.jobs.append(job)
            return fv.RuntimeResult(True, 1, SECRETS_IN_AN_ERROR)  # a Target that returns the raw text

    rep = fv.verify(project(tmp_path, FILES), units3(), render_names, {}, fv.Options(Leaky()), spec=spec())
    # a target that fails also before the patch judges nothing: the line is the reason, in a note ("could not judge: ...")
    assert any("AssertionError" in tx for _lv, tx in rep.notes)
    for r in rep.rows:
        assert not any(x in r.detail for x in SECRET_PARTS), r.detail
    text = "\n".join(fix._verify_lines(rep))
    assert not any(x in text for x in SECRET_PARTS) and "AssertionError: DB_PASSWORD=***" in text

    # and a row that is dropped with the line as its detail (the target fails only after the patch)
    class After(FakeTarget):
        def run(self, job):
            self.jobs.append(job)
            return fv.RuntimeResult(True, 0 if not job.edits else 1, "" if not job.edits else SECRETS_IN_AN_ERROR)

    rep = fv.verify(project(tmp_path / "two", FILES), units3(), render_names, {}, fv.Options(After()), spec=spec())
    dropped = [r for r in rep.rows if r.status == fv.FAILED]
    assert dropped and all("AssertionError: DB_PASSWORD=***" in r.detail for r in dropped)
    assert not any(x in "\n".join(fix._verify_lines(rep)) for x in SECRET_PARTS)


def test_tests_that_fail_before_the_patch_judge_nothing(tmp_path, monkeypatch):
    scripted(monkeypatch)

    class Red(FakeTarget):
        def run(self, job):
            return (
                fv.RuntimeResult(True, 1, "FAILED test_old.py - already red") if job.edits == () else super().run(job)
            )

    rep = fv.verify(project(tmp_path, FILES), units3(), render_names, {}, fv.Options(Red()), spec=spec())
    assert {r.status for r in rep.rows} == {
        fv.VERIFIED_LOCAL
    }  # the local verdict stands; the runtime class could not judge
    assert len(rep.kept) == 5
    assert [n[1] for n in rep.notes] == [
        "python: the runtime check could not judge: scripted-tests fails on the tree as it is: FAILED test_old.py - already red"
    ]


def test_a_language_with_no_local_check_is_unverified_without_a_target_and_runtime_verified_with_one(
    tmp_path, monkeypatch
):
    scripted(monkeypatch)
    t = spec(runtime=False, rust={"runtime": {"check": "cargo check", "argv": ["cargo", "check"], "baseline": True}})
    us = [unit("src/lib.rs", 3, 5, "old_fn", lang="rust")]
    root = project(tmp_path, {"src/lib.rs": "x\n"})
    rep = fv.verify(root, us, render_names, {}, fv.Options(None, "no target"), spec=t)
    assert rep.rows[0].status == fv.UNVERIFIED and "no non-executing check for rust" in rep.rows[0].detail
    assert len(rep.kept) == 1 and len(rep.notes) == 1 and rep.notes[0][1].startswith(fv.RUNTIME_SKIPPED)
    tg = FakeTarget()
    rep = fv.verify(root, us, render_names, {}, fv.Options(tg), spec=t)
    assert rep.rows[0].status == fv.VERIFIED_RUNTIME and rep.rows[0].check == "cargo check" and rep.notes == ()
    assert tg.jobs[-1].argv == ("cargo", "check") and tg.jobs[-1].lang == "rust"


def test_a_language_without_any_check_is_unverified_and_stays(tmp_path, monkeypatch):
    scripted(monkeypatch)
    us = [unit("a.zig", 1, 2, "f", lang="zig")]
    rep = fv.verify(project(tmp_path, {"a.zig": "x"}), us, render_names, {}, fv.Options(None, "x"), spec=spec())
    assert (
        rep.rows[0].status == fv.UNVERIFIED
        and "no non-executing check for zig" in rep.rows[0].detail
        and len(rep.kept) == 1
    )
    assert rep.notes == ()  # nothing in the patch has a runtime check: no skip line


def test_a_target_that_raises_judges_nothing(tmp_path, monkeypatch):
    scripted(monkeypatch)

    class Boom(FakeTarget):
        def run(self, job):
            raise OSError("ssh: connection reset")

    rep = fv.verify(project(tmp_path, FILES), units3(), render_names, {}, fv.Options(Boom()), spec=spec())
    assert {r.status for r in rep.rows} == {fv.VERIFIED_LOCAL} and len(rep.kept) == 5


# ================================================================ the Python check (non-executing)
OLD = "import os\n\n\ndef keep():\n    return helper() + 1\n\n\ndef helper():\n    return 2\n\n\ndef dead():\n    return os.getcwd()\n"
DEAD_GONE = OLD.replace("\n\ndef dead():\n    return os.getcwd()\n", "")


def check(tmp, files, new, removed, path="pkg/mod.py", old=OLD):
    root = project(tmp, files)
    return pycheck.check(
        {
            "tree": root,
            "files": {path: {"old": old, "new": new}},
            "removed": [{"path": path, "qual": q} for q in removed],
        }
    )


def test_a_clean_removal_passes_the_python_check(tmp_path):
    res = check(tmp_path, {"pkg/__init__.py": "", "pkg/mod.py": DEAD_GONE}, DEAD_GONE, ["dead"])
    assert res == {"ok": True, "errors": []}


def test_a_name_the_removal_leaves_dangling_is_found(tmp_path):
    new = OLD.replace("def helper():\n    return 2\n\n\n", "")
    res = check(tmp_path, {"pkg/mod.py": new}, new, ["helper"])
    assert res["ok"] is False and res["errors"] == ["pkg/mod.py:5: `helper` is read but no longer defined"]


def test_a_syntax_error_is_found_and_names_that_were_undefined_before_are_not_blamed(tmp_path):
    res = check(tmp_path, {"pkg/mod.py": "x"}, "def f(:\n", ["f"])
    assert res["errors"][0].startswith("pkg/mod.py:1: does not compile (SyntaxError")
    old = "def f():\n    return missing_before()\n\n\ndef g():\n    return 1\n"
    new = "def f():\n    return missing_before()\n"
    assert check(tmp_path, {"pkg/mod.py": new}, new, ["g"], old=old)["ok"] is True


def test_scopes_are_real_scopes(tmp_path):
    old = (
        "COUNT = 0\n\n\ndef used():\n    global COUNT\n    COUNT += 1\n    return [x for x in range(3)]\n\n\n"
        "class K:\n    attr = 1\n\n    def m(self):\n        return self.attr + len('x')\n\n\ndef dead():\n    return 1\n"
    )
    new = old.replace("\n\ndef dead():\n    return 1\n", "")
    assert check(tmp_path, {"pkg/mod.py": new}, new, ["dead"], old=old)["ok"] is True
    deco_old = "def deco(f):\n    return f\n\n\n@deco\ndef g():\n    return 1\n"
    deco_new = "@deco\ndef g():\n    return 1\n"  # the decorator is read at module level and is gone
    res = check(tmp_path, {"pkg/mod.py": deco_new}, deco_new, ["deco"], old=deco_old)
    assert res["errors"] == ["pkg/mod.py:1: `deco` is read but no longer defined"]


def test_an_importer_of_a_removed_name_is_found_absolute_relative_and_through_the_module(tmp_path):
    files = {
        "pkg/__init__.py": "",
        "pkg/mod.py": DEAD_GONE,
        "pkg/user1.py": "from pkg.mod import dead\n",
        "pkg/user2.py": "from .mod import dead as d\n",
        "pkg/user3.py": "from . import mod\n\nmod.dead()\n",
        "pkg/user4.py": "import pkg.mod as m\n\nm.dead()\n",
        "pkg/user5.py": "from pkg.mod import keep\n",  # a name that stays
        "pkg/user6.py": "from other.mod import dead\n",  # another module that is not the patched one
    }
    res = check(tmp_path, files, DEAD_GONE, ["dead"])
    assert res["ok"] is False
    assert res["errors"] == [
        "pkg/user1.py:1: imports `dead`, which the patch removes from pkg/mod.py",
        "pkg/user2.py:1: imports `dead`, which the patch removes from pkg/mod.py",
        "pkg/user3.py:3: reads `mod.dead`, which the patch removes from pkg/mod.py",
        "pkg/user4.py:3: reads `m.dead`, which the patch removes from pkg/mod.py",
    ]  # user5 imports a name that stays, user6 another module


def test_a_method_is_checked_for_syntax_and_names_only(tmp_path):
    files = {"pkg/mod.py": "class A:\n    def m(self):\n        return 1\n", "pkg/use.py": "from pkg.mod import m\n"}
    old = "class A:\n    def m(self):\n        return 1\n\n    def n(self):\n        return 2\n"
    new = "class A:\n    def m(self):\n        return 1\n"
    assert check(tmp_path, files, new, ["A.n"], old=old)["ok"] is True  # `obj.n` is not decidable here


def test_all_that_still_names_a_removed_symbol_is_found(tmp_path):
    new = "__all__ = ['keep', 'dead']\n\n\ndef keep():\n    return 1\n"
    old = new + "\n\ndef dead():\n    return 2\n"
    res = check(tmp_path, {"pkg/mod.py": new}, new, ["dead"], old=old)
    assert res["errors"] == ["pkg/mod.py:1: __all__ names `dead`, which the patch removes"]


def test_the_python_check_runs_nothing_of_the_tree(tmp_path):
    marker = tmp_path / "EXECUTED"
    trap = f"open({str(marker)!r}, 'w').write('x')\nraise SystemExit(3)\n\n\ndef dead():\n    return 1\n"
    files = {"pkg/mod.py": trap, "pkg/user.py": f"open({str(marker)!r}, 'w').write('y')\n"}
    new = trap.replace("\n\ndef dead():\n    return 1\n", "")
    assert check(tmp_path, files, new, ["dead"], old=trap)["ok"] is True
    assert not marker.exists()


def test_pycheck_main_reads_a_request_file_and_prints_json(tmp_path, capsys):
    req = tmp_path / "req.json"
    req.write_text(
        json.dumps({"tree": str(tmp_path), "files": {"a.py": {"old": "x = 1\n", "new": "x = (\n"}}, "removed": []})
    )
    assert pycheck.main(["pycheck.py", str(req)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert pycheck.main(["pycheck.py"]) == 2


def test_the_python_check_in_the_box_gives_the_verdict_of_the_process_and_runs_nothing(tmp_path):
    """The real Host: in the bubblewrap box when this machine has one, in this process when it has not (a note then says so)."""
    marker = tmp_path / "EXECUTED"
    trap = f"open({str(marker)!r}, 'w').write('x')\n\n\ndef dead():\n    return 1\n"
    files = {"pkg/mod.py": trap, "pkg/user.py": "from pkg.mod import dead\n"}
    root = project(tmp_path, files)
    gone = trap.replace("\n\ndef dead():\n    return 1\n", "")
    u = [unit("pkg/mod.py", 4, 5, "dead")]
    render = lambda us: {"pkg/mod.py": gone}  # noqa: E731
    rep = fv.verify(root, u, render, {"pkg/mod.py": trap}, spec=spec(runtime=False), host=fv.Host())
    assert not marker.exists()
    assert rep.rows[0].status == fv.FAILED and "pkg/user.py:1: imports `dead`" in rep.rows[0].detail
    in_process = [n for n in rep.notes if "ran in this process" in n[1]]
    assert len(in_process) == (0 if sandbox.check_box() else 1)  # the note says it when there was no box


# ================================================================ the toolchain checks: a fake host, no tool runs
GO_OLD = "package svc\n\nfunc keep() int { return helper() }\n\nfunc helper() int { return 1 }\n"
GO_NEW = "package svc\n\nfunc keep() int { return helper() }\n"


class FakeHost(fv.Host):
    def __init__(self, box=True, tools=("go", "tsc")):
        self.boxed, self.tools, self.runs, self.boxes = box, {t: f"/usr/bin/{t}" for t in tools}, [], []

    def resolve(self, name):
        return self.tools.get(name, "")

    def box(self, argv, dirs, env, ro, cwd):
        self.boxes.append({"argv": argv, "dirs": dict(dirs), "env": dict(env), "ro": list(ro), "cwd": cwd})
        return ["BOX", *argv] if self.boxed else None

    def run(self, argv, cwd, timeout, env):
        self.runs.append((list(argv), cwd, timeout, env))
        src = Path(cwd, "main.go").read_text() if Path(cwd, "main.go").exists() else ""
        if "func helper" not in src and "helper()" in src:
            return 1, b"", b"# svc\n" + f"{cwd}/main.go:3:30: undefined: helper\n".encode()
        return 0, b"", b""


def go_project(tmp_path):
    return project(tmp_path, {"svc/go.mod": "module svc\n", "svc/main.go": GO_OLD, "other/x.txt": "x"})


def go_spec():
    t = spec(runtime=False)
    t["langs"] = {"go": {"local": fv.table()["langs"]["go"]["local"]}}
    return t


def go_render(us):
    """The patched text for a candidate that removes something, nothing for the tree as it is."""
    return {"svc/main.go": GO_NEW} if us else {}


def go_unit(qual="helper"):
    return Unit("svc/main.go", 5, 5, (item("svc/main.go", 5, qual),), "go")


def test_a_go_removal_that_leaves_a_use_is_dropped_with_the_first_error_line_of_vet(tmp_path):
    host = FakeHost()
    rep = fv.verify(
        go_project(tmp_path),
        [go_unit()],
        go_render,
        {"svc/main.go": GO_OLD},
        host=host,
        spec=go_spec(),
    )
    r = rep.rows[0]
    assert r.status == fv.FAILED and r.check == "go vet" and r.klass == "local"
    assert r.detail == "svc/main.go:3:30: undefined: helper"  # the copy's directory is cut off the path
    assert rep.kept == () and rep.checks == 1 + 1  # the tree as it is, then the one removal (the whole patch)


def test_go_runs_offline_in_the_box_in_the_package_directory_and_only_there(tmp_path, monkeypatch):
    cache = tmp_path / "gomod"
    cache.mkdir()
    (cache / "present").write_text("x")
    monkeypatch.setenv("DEAD_CODE_CHECKER_GO_CACHE", str(cache))
    host = FakeHost()
    fv.verify(
        go_project(tmp_path),
        [go_unit()],
        go_render,
        {"svc/main.go": GO_OLD},
        host=host,
        spec=go_spec(),
    )
    b = host.boxes[-1]
    assert b["argv"] == ["/usr/bin/go", "vet", "./..."] and b["cwd"].endswith("/svc")
    assert b["env"]["GOMODCACHE"] == str(cache) and str(cache) in b["ro"]
    for k, v in (
        ("GOFLAGS", "-mod=mod"),
        ("GOPROXY", "off"),
        ("GOSUMDB", "off"),
        ("GOTOOLCHAIN", "local"),
        ("CGO_ENABLED", "0"),
    ):
        assert b["env"][k] == v
    assert b["env"]["HOME"] != os.environ.get("HOME") and set(b["dirs"]) == {"tree", "home", "tmp"}
    assert all(argv[0] == "BOX" for argv, *_ in host.runs)  # nothing started outside the box


def test_a_toolchain_check_is_never_run_without_a_box(tmp_path):
    host = FakeHost(box=False)
    rep = fv.verify(
        go_project(tmp_path),
        [go_unit()],
        go_render,
        {"svc/main.go": GO_OLD},
        host=host,
        spec=go_spec(),
    )
    assert host.runs == [] and rep.rows[0].status == fv.UNVERIFIED and "no bwrap box on this host" in rep.rows[0].detail
    assert len(rep.kept) == 1


def test_a_tool_that_is_not_found_is_not_run(tmp_path):
    host = FakeHost(tools=())
    rep = fv.verify(
        go_project(tmp_path),
        [go_unit()],
        go_render,
        {"svc/main.go": GO_OLD},
        host=host,
        spec=go_spec(),
    )
    assert host.runs == [] and "go was not found" in rep.rows[0].detail


def test_a_check_that_fails_on_the_tree_as_it_is_judges_nothing(tmp_path):
    class Red(FakeHost):
        def run(self, argv, cwd, timeout, env):
            self.runs.append(argv)
            return 1, b"", b"go: cannot find module providing package x: module lookup disabled by GOPROXY=off\n"

    rep = fv.verify(
        go_project(tmp_path),
        [go_unit()],
        go_render,
        {"svc/main.go": GO_OLD},
        host=Red(),
        spec=go_spec(),
    )
    assert rep.rows[0].status == fv.UNVERIFIED and rep.rows[0].detail.startswith(
        "go vet fails on the tree as it is: go: cannot find module"
    )
    assert len(rep.kept) == 1


def test_a_missing_manifest_is_unverified(tmp_path):
    root = project(tmp_path, {"svc/main.go": GO_OLD})
    rep = fv.verify(root, [go_unit()], go_render, {"svc/main.go": GO_OLD}, host=FakeHost(), spec=go_spec())
    assert rep.rows[0].status == fv.UNVERIFIED and "no go.mod above the changed files" in rep.rows[0].detail


def test_first_error_picks_the_line_that_says_what_broke():
    out = "# example.com/svc\nvet: main.go:3:30: undefined: helper\nFAIL\n"
    assert fv._first_error(out, "") == "vet: main.go:3:30: undefined: helper"
    assert (
        fv._first_error("", "/tmp/c1/src/a.ts(3,5): error TS2304: Cannot find name 'x'.\n", "/tmp/c1")
        == "src/a.ts(3,5): error TS2304: Cannot find name 'x'."
    )
    assert fv._first_error("", "") == "no message"


def test_package_dirs_find_the_nearest_manifest(tmp_path):
    root = project(tmp_path, {"go.mod": "m", "a/b/c.go": "x", "svc/go.mod": "m", "svc/d/e.go": "x"})
    assert fv._package_dirs(root, ["a/b/c.go", "svc/d/e.go"], "go.mod") == ["", "svc"]
    assert fv._package_dirs(str(tmp_path), ["proj/a/b/c.go"], "nope.mod") == []


# ================================================================ the toolchain checks in the real box, with stub tools
# The stubs are shell scripts on a directory the test puts first in the engines search path: they stand in for `go vet` and `tsc`, check the
# command line and the environment they were given, and report like the real tools. They need the real bubblewrap box.
needs_box = pytest.mark.skipif(not sandbox.check_box(), reason="needs a working bubblewrap box on this host")

STUB_GO = """#!/bin/sh
# the marker of a stub that started (outside the box it is written, inside the box its directory does not exist)
(echo > "{ran}") 2>/dev/null
[ "$1" = vet ] && [ "$2" = ./... ] || {{ echo "go: unexpected arguments: $*" >&2; exit 2; }}
for kv in GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local CGO_ENABLED=0 GOFLAGS=-mod=mod; do
  [ "$(printenv "${{kv%%=*}}")" = "${{kv#*=}}" ] || {{ echo "go: ${{kv%%=*}} is not ${{kv#*=}}" >&2; exit 3; }}
done
[ -n "$GOMODCACHE" ] || {{ echo "go: no module cache" >&2; exit 3; }}
[ -z "$FAKE_TOKEN_FOR_TEST" ] || {{ echo "go: the environment of the checker leaked" >&2; exit 3; }}
[ "$(grep -c : /proc/net/dev)" = 1 ] || {{ echo "go: there is a network interface in the box" >&2; exit 3; }}
[ -f go.mod ] || {{ echo "go: no go.mod in $(pwd)" >&2; exit 4; }}
[ -f ../outside-path ] && {{ p=$(cat ../outside-path); echo x > "$p" 2>/dev/null; }}
if grep -q 'helper()' main.go && ! grep -q 'func helper' main.go; then
  echo "# svc"; echo "$(pwd)/main.go:3:30: undefined: helper" >&2; exit 1
fi
exit 0
"""
STUB_TSC = """#!/bin/sh
# the marker of a stub that started (outside the box it is written, inside the box its directory does not exist)
(echo > "{ran}") 2>/dev/null
[ "$1" = --noEmit ] && [ "$#" = 1 ] || {{ echo "tsc: unexpected arguments: $*" >&2; exit 2; }}
[ -f tsconfig.json ] || {{ echo "tsc: no tsconfig.json in $(pwd)" >&2; exit 4; }}
if grep -rq 'used()' src && ! grep -rq 'function used' src; then
  echo "$(pwd)/src/a.ts(3,5): error TS2304: Cannot find name 'used'."; exit 2
fi
exit 0
"""


@pytest.fixture
def stubs(tmp_path, monkeypatch):
    """A directory with the stub `go` and `tsc` first in the engines search path, an offline Go cache that is not empty, a marker path a
    stub touches when it starts, and a variable of the checker's environment that must never reach a tool."""
    bin_dir = tmp_path / "bin"  # named bin: the box would bind its parent (tmp_path), which holds the scratch directory too
    bin_dir.mkdir()
    ran = tmp_path / "stub-ran"
    for name, text in (("go", STUB_GO), ("tsc", STUB_TSC)):
        p = bin_dir / name
        p.write_text(text.format(ran=ran))
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    cache = tmp_path / "gomod"
    cache.mkdir()
    (cache / "present").write_text("x")
    monkeypatch.setattr(engines, "_SETUP", [None])
    monkeypatch.setenv(engines.ENV_DIR, str(bin_dir))
    monkeypatch.setenv("DEAD_CODE_CHECKER_GO_CACHE", str(cache))
    monkeypatch.setenv("FAKE_TOKEN_FOR_TEST", "leaky")
    return SimpleNamespace(ran=ran, cache=cache, bin=bin_dir)


GO_FILES = {
    "svc/go.mod": "module example.test/svc\n\ngo 1.21\n",
    "svc/main.go": "package svc\n\nfunc Keep() int { return helper() }\n\nfunc helper() int { return 1 }\n\nfunc Dead() int { return 2 }\n",
}


def go_removals(root):
    old = Path(root, "svc/main.go").read_text()
    lines = old.splitlines(keepends=True)

    def render(us):
        gone = {n for u in us for n in range(u.first, u.last + 1)}
        return {"svc/main.go": "".join(x for i, x in enumerate(lines, 1) if i not in gone)} if us else {}

    us = [
        Unit("svc/main.go", 5, 5, (item("svc/main.go", 5, "helper"),), "go"),
        Unit("svc/main.go", 7, 7, (item("svc/main.go", 7, "Dead"),), "go"),
    ]
    return us, render, {"svc/main.go": old}


@needs_box
def test_go_vet_runs_offline_in_the_real_box_and_drops_the_removal_that_leaves_a_use(tmp_path, stubs):
    root = project(tmp_path, GO_FILES)
    outside = tmp_path / "outside-marker"
    Path(root, "outside-path").write_text(str(outside))  # the stub tries to write there from inside the box
    us, render, texts = go_removals(root)
    before = tree_hash(Path(root))
    scratch = tmp_path / "scratch"  # under the parent of the stub directory: that parent must not be bound over it
    scratch.mkdir()
    host = fv.Host()
    host.tmp_base = str(scratch)
    rep = fv.verify(root, us, render, texts, fv.Options(None, "test", str(tmp_path / "data"), None), host=host, spec=go_spec())
    rows = by_symbol(rep)
    assert rows["helper"].status == fv.FAILED and rows["helper"].check == "go vet" and rows["helper"].klass == "local"
    assert rows["helper"].detail == "svc/main.go:3:30: undefined: helper"  # the copy's directory is cut off the path
    assert rows["Dead"].status == fv.VERIFIED_LOCAL and rows["Dead"].check == "go vet"
    assert [u.items[0].qual for u in rep.kept] == ["Dead"]
    assert not any("no offline Go module cache" in t for _lv, t in rep.notes)
    assert not outside.exists()  # nothing written by the box is visible outside it
    assert tree_hash(Path(root)) == before and not Path(root, "svc/go.sum").exists()  # the work tree is untouched
    assert list(scratch.iterdir()) == []  # every copy is gone


@needs_box
def test_tsc_runs_in_the_real_box_in_the_package_directory(tmp_path, stubs):
    files = {
        "web/tsconfig.json": "{}\n",
        "web/src/a.ts": "export function used() { return 1 }\n\nexport function gone() { return 2 }\n\nused()\n",
    }
    root = project(tmp_path, files)
    old = files["web/src/a.ts"]
    lines = old.splitlines(keepends=True)

    def render(us):
        gone = {n for u in us for n in range(u.first, u.last + 1)}
        return {"web/src/a.ts": "".join(x for i, x in enumerate(lines, 1) if i not in gone)} if us else {}

    us = [
        Unit("web/src/a.ts", 1, 1, (item("web/src/a.ts", 1, "used"),), "typescript"),
        Unit("web/src/a.ts", 3, 3, (item("web/src/a.ts", 3, "gone"),), "typescript"),
    ]
    rep = fv.verify(root, us, render, {"web/src/a.ts": old}, fv.Options(None, "test"), host=fv.Host(), spec=tsc_spec())
    rows = by_symbol(rep)
    assert rows["used"].status == fv.FAILED and rows["used"].check == "tsc --noEmit"
    assert rows["used"].detail == "web/src/a.ts(3,5): error TS2304: Cannot find name 'used'."
    assert rows["gone"].status == fv.VERIFIED_LOCAL


def tsc_spec():
    t = spec(runtime=False)
    t["langs"] = {"typescript": {"local": fv.table()["langs"]["typescript"]["local"]}}
    return t


def test_a_toolchain_check_is_never_run_on_a_host_without_a_box(tmp_path, stubs, monkeypatch):
    """The stub is found and would run, but there is no bubblewrap box: it is not started, the removal is unverified, and says why."""
    monkeypatch.setattr(sandbox, "_MODE", [sandbox.Mode("none", (), sandbox.NOTE_NONE)])  # restored by monkeypatch
    root = project(tmp_path, GO_FILES)
    us, render, texts = go_removals(root)
    rep = fv.verify(root, us, render, texts, fv.Options(None, "test"), host=fv.Host(), spec=go_spec())
    assert not stubs.ran.exists()  # nothing was started on this machine
    assert {r.status for r in rep.rows} == {fv.UNVERIFIED} and len(rep.kept) == 2
    assert all("no bwrap box on this host" in r.detail for r in rep.rows)


def test_without_bubblewrap_the_box_is_none_and_a_mode_that_is_not_bwrap_gives_no_box(monkeypatch):
    assert sandbox.check_box(sandbox.Mode("none", (), sandbox.NOTE_NONE)) == ""
    assert sandbox.check_box(sandbox.Mode("netless", ("unshare", "-rn"), sandbox.NOTE_NETLESS)) == ""
    monkeypatch.setattr(sandbox, "_MODE", [sandbox.Mode("none", (), sandbox.NOTE_NONE)])
    assert fv.Host().box(["true"], {}, {}, [], "/") is None


def test_a_relative_entry_of_path_never_finds_a_planted_cp_go_or_tsc(tmp_path, monkeypatch):
    """`.` on PATH while the checker stands in the analysed repository: the tools of the checks are found by absolute directories only."""
    real_path = os.environ.get("PATH", "")
    evil = tmp_path / "tree"
    evil.mkdir()
    marker = tmp_path / "ran"
    for name in ("cp", "go", "tsc"):
        f = evil / name
        f.write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
        f.chmod(0o755)
    monkeypatch.chdir(evil)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "../tree", real_path]))
    engines.reset()
    host = fv.Host()
    for name in ("cp", "go", "tsc"):
        got = host.resolve(name)
        assert got == "" or (os.path.isabs(got) and Path(got).resolve() != (evil / name).resolve())
    root = project(tmp_path, {"a.py": "x = 1\n"})
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    ws = fv.Workspace(root, host, str(scratch))
    cand = ws.candidate({"a.py": "y = 2\n"})  # copies with the real cp (or in-process), never the planted one
    assert Path(cand, "a.py").read_text() == "y = 2\n" and not marker.exists()


def test_the_go_environment_is_the_offline_one_with_the_module_cache_of_graph_gocache(tmp_path):
    cache = tmp_path / "gomod"
    cache.mkdir()
    (cache / "present").write_text("x")
    env, note = fv.Host().offline_env({"GOFLAGS": "-mod=mod", "GOPROXY": "off"}, "go", fv.Options(None, "", None, str(cache)))
    assert note == "" and env["GOMODCACHE"] == str(cache) and env["GOFLAGS"] == "-mod=mod" and env["GOPROXY"] == "off"
    env, note = fv.Host().offline_env({"GOFLAGS": "-mod=mod"}, "go", fv.Options(None, "", None, str(tmp_path / "absent")))
    assert "GOMODCACHE" not in env and "no offline Go module cache" in note  # the note names the reason and the fill command
    assert fv.Host().offline_env({"A": "1"}, "", fv.Options()) == ({"A": "1"}, "")  # another family: the variables as they are


def test_a_read_only_directory_above_a_writable_one_is_left_out_of_the_box(tmp_path):
    w = tmp_path / "scratch" / "cand1"
    w.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    keep = fv._ro_outside([str(tmp_path), str(tmp_path / "scratch"), str(w), str(other)], {"tree": str(w)})
    assert keep == [str(other)]  # a bind over a parent (or the directory itself) would hide the writable bind
    assert fv._tool_dirs("/usr/bin/env") and "/" not in fv._tool_dirs("/usr/bin/env")


# ================================================================ the copy: the work tree is only read
def test_the_copy_holds_the_tracked_files_without_secrets_and_is_removed_afterwards(tmp_path, monkeypatch):
    scripted(monkeypatch)
    root = project(tmp_path, {**FILES, ".env": "SECRET=1\n", "keys/id_rsa": "k", "keys/server.pem": "p"})
    seen = {}

    def local(self, c, units):
        cand = self.ws.candidate({"pkg/c.py": "# edited\n"})
        seen["files"] = sorted(str(p.relative_to(cand)) for p in Path(cand).rglob("*") if p.is_file())
        seen["edited"] = Path(cand, "pkg/c.py").read_text()
        self.ws.drop(cand)
        return Outcome(True)

    monkeypatch.setattr(fv.Verifier, "local", local)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    host = fv.Host()
    host.tmp_base = str(scratch)
    before = tree_hash(Path(root))
    fv.verify(root, units3(), render_names, {}, spec=spec(runtime=False), host=host)
    assert ".env" not in seen["files"] and "keys/id_rsa" not in seen["files"] and "keys/server.pem" not in seen["files"]
    assert "pkg/a.py" in seen["files"] and seen["edited"] == "# edited\n"
    assert list(scratch.iterdir()) == []  # every copy is gone
    assert tree_hash(Path(root)) == before  # the work tree is as it was, byte for byte (and mode)


def test_a_tracked_symlink_that_is_absolute_or_leaves_the_copy_is_not_copied(tmp_path, monkeypatch):
    """An absolute symlink is not copied as it is (tsc would read host files through it). Such links are left out of the copy and said
    once in the report; a relative link that stays inside the tree is kept (also through `..` after a link)."""
    scripted(monkeypatch)
    host_file = tmp_path / "host-secret.txt"
    host_file.write_text("HOST DATA\n")
    root = project(tmp_path, {**FILES, "sub/inner.txt": "inner\n"})
    proot = Path(root)
    os.symlink(str(host_file), proot / "abs-link")  # absolute: reads a host file
    os.symlink("../host-secret.txt", proot / "up-link")  # relative, leaves the tree
    os.symlink("../../host-secret.txt", proot / "sub" / "deep-up")  # relative, leaves the tree from a sub directory
    os.symlink("sub/inner.txt", proot / "ok-link")  # relative, inside the tree: stays
    os.symlink("inner.txt", proot / "sub" / "sibling")  # relative, same directory: stays
    os.symlink("..", proot / "sub" / "to-root")  # a link to the root itself stays inside
    os.symlink("sub/to-root/..", proot / "dotdot")  # through a link, `..` leaves the tree: not kept
    os.symlink(
        "abs-link", proot / "chain"
    )  # a link to an absolute link: the absolute one is not there, the chain is a dangling name only
    seen = {}

    def local(self, c, units):
        cand = self.ws.candidate({})
        seen["links"] = sorted(str(p.relative_to(cand)) for p in Path(cand).rglob("*") if p.is_symlink())
        seen["reads"] = [
            str(p.relative_to(cand)) for p in Path(cand).rglob("*") if p.is_symlink() and p.exists() and p.is_file()
        ]
        seen["host"] = [
            str(p.relative_to(cand)) for p in Path(cand).rglob("*") if p.is_file() and p.read_text() == "HOST DATA\n"
        ]
        self.ws.drop(cand)
        return Outcome(True)

    monkeypatch.setattr(fv.Verifier, "local", local)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    class Git(fv.Host):  # what `git ls-files` lists: the files and every symlink, also one that points at a directory
        tmp_base = str(scratch)

        def tracked(self, r):
            out = []
            for d, dirs, files in os.walk(r):
                out += [
                    os.path.relpath(os.path.join(d, n), r)
                    for n in files + [x for x in dirs if os.path.islink(os.path.join(d, x))]
                ]
            return sorted(out)

    rep = fv.verify(root, units3(), render_names, {}, spec=spec(runtime=False), host=Git())
    assert "abs-link" not in seen["links"] and "up-link" not in seen["links"] and "sub/deep-up" not in seen["links"]
    assert "dotdot" not in seen["links"]
    assert "ok-link" in seen["links"] and "sub/sibling" in seen["links"] and "sub/to-root" in seen["links"]
    assert seen["host"] == []  # nothing in the copy reads the host file, directly or through a chain
    assert "ok-link" in seen["reads"]
    notes = [t for lv, t in rep.notes if "symlink" in t]
    assert len(notes) == 1 and "not copied" in notes[0] and "abs-link" in notes[0] and notes[0].startswith("4 tracked")
    assert list(scratch.iterdir()) == []


def test_a_workspace_lists_the_links_it_left_out_and_a_tree_without_such_links_says_nothing(tmp_path, monkeypatch):
    root, scratch = tmp_path / "proj", tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    (root / "a.txt").write_text("a")
    os.symlink("/etc/hostname", root / "abs")
    os.symlink("a.txt", root / "rel")

    class H(fv.Host):
        def tracked(self, r):
            return ["a.txt", "abs", "rel"]

    ws = fv.Workspace(str(root), H(), str(scratch))
    assert (
        ws.skipped_links == ["abs"]
        and os.path.islink(Path(ws.base) / "rel")
        and not os.path.lexists(Path(ws.base) / "abs")
    )
    os.unlink(root / "abs")

    class H2(H):
        def tracked(self, r):
            return ["a.txt", "rel"]

    scratch2 = tmp_path / "scratch2"
    scratch2.mkdir()
    assert fv.Workspace(str(root), H2(), str(scratch2)).skipped_links == []
    scripted(monkeypatch)
    rep = fv.verify(str(root), [unit("a.txt", 1, 1, "x")], render_names, {}, spec=spec(runtime=False), host=H2())
    assert not [t for _lv, t in rep.notes if "symlink" in t]


# ================================================================ fix.plan --verify on a real repository
def test_plan_verify_keeps_the_patch_of_a_clean_chain_and_gives_every_symbol_a_status(repo, tmp_path):
    with_chain(repo)
    base = fix.plan(str(repo), "branch", base="HEAD", proof=STIER)
    p = fix.plan(str(repo), "branch", base="HEAD", proof=STIER, verify=fv.Options(None, "nothing configured here"))
    assert p.patch == base.patch and [i.qual for i in p.removed] == ["link_a", "link_b", "link_c"]
    assert [(r.qual, r.status) for r in p.verify.rows] == [
        (q, fv.VERIFIED_LOCAL) for q in ("link_a", "link_b", "link_c")
    ]
    skip = [n for n in p.verify.notes if n[1].startswith(fv.RUNTIME_SKIPPED)]
    assert len(skip) == 1 and skip[0][0] == "I" and "nothing configured here" in skip[0][1]
    assert (applied(repo, p.patch, tmp_path) / "lib/app.py").read_text().count("link_") == 0
    assert (
        fix.plan(str(repo), "branch", base="HEAD", proof=STIER).verify is None
    )  # without --verify nothing of it exists


def crafted(monkeypatch, claims):
    """A plan whose graph claims `claims` ((path, first, last, qual)) are dead: the way to feed the verifier a WRONG claim."""

    def fake_branch(root, base, sink=None, proof=None, **kw):
        sink["graph"] = object()
        return SimpleNamespace(findings=[], meta={})

    def fake_candidates(fs, graph, codes, paths, min_conf=0):
        todo = {}
        for path, a, b, qual in claims:
            it = fix.Item(path, a, qual, "dc.dead")
            todo.setdefault(path, []).append((a, b, it))
        return todo, [], {}

    monkeypatch.setattr(levels, "branch", fake_branch)
    monkeypatch.setattr(fix, "candidates", fake_candidates)


WRONG = {
    "lib/a.py": "def used_elsewhere():\n    return 1\n\n\ndef really_dead():\n    return 2\n",
    "lib/b.py": "from lib.a import used_elsewhere\n\n\ndef main():\n    return used_elsewhere()\n",
    "lib/__init__.py": "",
}


def test_plan_verify_drops_a_wrong_claim_and_keeps_the_right_one(repo, tmp_path, monkeypatch):
    for rel, text in WRONG.items():
        write(repo, rel, text)
    crafted(monkeypatch, [("lib/a.py", 1, 2, "used_elsewhere"), ("lib/a.py", 5, 6, "really_dead")])
    plain = fix.plan(str(repo), "branch", base="HEAD")
    assert [i.qual for i in plain.removed] == ["used_elsewhere", "really_dead"]  # what --dry-run alone would offer
    p = fix.plan(str(repo), "branch", base="HEAD", verify=fv.Options(None, "none"))
    assert [i.qual for i in p.removed] == ["really_dead"]
    rows = by_symbol(p.verify)
    assert rows["used_elsewhere"].status == fv.FAILED and rows["really_dead"].status == fv.VERIFIED_LOCAL
    assert (
        rows["used_elsewhere"].detail == "lib/b.py:1: imports `used_elsewhere`, which the patch removes from lib/a.py"
    )
    new = (applied(repo, p.patch, tmp_path) / "lib/a.py").read_text()
    assert "really_dead" not in new and "used_elsewhere" in new  # the patch removes only what passed


def test_the_command_reports_the_status_per_symbol_and_the_dropped_ones(repo, monkeypatch, capsys):
    for rel, text in WRONG.items():
        write(repo, rel, text)
    crafted(monkeypatch, [("lib/a.py", 1, 2, "used_elsewhere"), ("lib/a.py", 5, 6, "really_dead")])
    monkeypatch.setattr(fix, "_exec_target", lambda root, host=None, user=None, port=None: (None, "no host is configured"))
    before = tree_hash(repo)
    argv = ["fix", "--dry-run", "--verify", "--root", str(repo), "--base", "HEAD"]
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert (
        out.startswith("--- a/lib/a.py")
        and "really_dead" in out.split("+++")[0] + out
        and "used_elsewhere" not in out.replace("def used_elsewhere", "")
    )
    assert (
        "fix --dry-run --verify: 1 symbol(s) in the patch (0 verified-runtime, 1 verified-local, 0 unverified), 1 dropped"
        in err
    )
    assert "dropped lib/a.py:1 used_elsewhere [py-compile+names+imports]: lib/b.py:1: imports `used_elsewhere`" in err
    assert "verified-local lib/a.py:5 really_dead [py-compile+names+imports]" in err
    assert (
        "  python: local py-compile+names+imports (here); runtime pytest (target only)" in err
    )  # the class of each check
    assert "  I runtime mode: no execution target (no host is configured)" in err
    assert cli.main([*argv, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    v = doc["verify"]
    assert v["counts"] == {"verified-runtime": 0, "verified-local": 1, "unverified": 0, "failed": 1}
    assert [d["symbol"] for d in v["dropped"]] == ["used_elsewhere"] and v["dropped"][0]["class"] == "local"
    py = {c["class"]: c for c in v["languages"]["python"]}
    assert py["local"]["runs"] == "here" and py["local"]["means"].startswith("non-executing")
    assert py["runtime"]["runs"] == "target only" and py["runtime"]["means"].startswith("executing")
    assert v["notes"] == [{"level": "I", "text": v["notes"][0]["text"]}] and v["notes"][0]["text"].startswith(
        "runtime mode: no execution target"
    )
    assert [r["symbol"] for r in doc["removed"]] == ["really_dead"]
    assert tree_hash(repo) == before  # still dry-run: nothing written into the work tree


def test_a_verify_that_fails_inside_is_a_message_and_exit_2_never_a_traceback(repo, monkeypatch, capsys):
    with_chain(repo)
    prove(monkeypatch)
    monkeypatch.setattr(fix, "_exec_target", lambda root, host=None, user=None, port=None: (None, "off"))

    def boom(*a, **k):
        raise RuntimeError("the copy failed")

    monkeypatch.setattr(fv, "verify", boom)
    before = tree_hash(repo)
    rc = cli.main(["fix", "--dry-run", "--verify", "--root", str(repo), "--base", "HEAD"])
    out, err = capsys.readouterr()
    assert rc == 2 and out == "" and "dead-code-checker graph fix: failed (RuntimeError: the copy failed); nothing was written" in err
    assert "Traceback" not in err and tree_hash(repo) == before


# ================================================================ the executing checks: only on the target, and the host option
def test_the_executing_checks_run_only_on_the_target_the_command_resolves(repo, monkeypatch, capsys):
    with_chain(repo)
    prove(monkeypatch)
    t, asked = FakeTarget(), []

    def resolve(root, host=None, user=None, port=None):
        asked.append((root, host, user, port))
        return t, ""

    monkeypatch.setattr(fix, "_exec_target", resolve)
    before = tree_hash(repo)
    argv = ["fix", "--dry-run", "--verify", "--host", "box.example", "--user", "me", "--port", "2222", "--root", str(repo), "--base", "HEAD"]
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert asked == [(str(repo), "box.example", "me", 2222)]  # the user's side of the target goes through to the one function that resolves it
    assert "3 symbol(s) in the patch (3 verified-runtime, 0 verified-local, 0 unverified), 0 dropped" in err
    assert "runtime mode" not in err and out.startswith("--- a/lib/app.py")
    base, whole = t.jobs  # the tree as it is, then the whole patch
    assert base.edits == () and base.argv[:3] == ("python3", "-m", "pytest") and base.lang == "python"
    assert [p for p, _ in whole.edits] == ["lib/app.py"] and "link_" not in whole.edits[0][1]
    assert tree_hash(repo) == before


def test_without_a_target_the_command_says_so_once_and_runs_nothing_of_the_project(repo, monkeypatch, capsys):
    with_chain(repo)
    prove(monkeypatch)
    monkeypatch.setattr(fix, "_exec_target", lambda root, host=None, user=None, port=None: (None, "runtime mode is not available in this build"))
    assert cli.main(["fix", "--dry-run", "--verify", "--root", str(repo), "--base", "HEAD"]) == 0
    out, err = capsys.readouterr()
    lines = [ln for ln in err.splitlines() if "runtime mode" in ln]
    assert lines == ["  I runtime mode: no execution target (runtime mode is not available in this build); tests and build checks were not run"]
    assert "3 verified-local" in err and out.startswith("--- a/lib/app.py")


def test_the_flag_is_part_of_the_command_line():
    a = cli.build_parser().parse_args(["fix", "--dry-run", "--verify", "--host", "h.example"])
    assert a.verify is True and a.host == "h.example" and a.dry_run is True and a.min_confidence == 0


# ================================================================ the sandbox prefix of the local checks
def test_the_bubblewrap_prefix_closes_the_box(tmp_path):
    dirs = {"tree": str(tmp_path / "t"), "home": str(tmp_path / "h")}
    env = {"HOME": dirs["home"], "LANG": "C.UTF-8"}
    pre = sandbox.bwrap_prefix("/usr/bin/bwrap", dirs, env, [str(tmp_path / "ro")], dirs["tree"])
    assert pre[0] == "/usr/bin/bwrap" and "--unshare-all" in pre and "--clearenv" in pre and pre[-1] == "--"
    assert "--new-session" in pre and "--die-with-parent" in pre and "--share-net" not in pre
    assert pre[pre.index("--chdir") + 1] == dirs["tree"]
    assert ["--setenv", "HOME", dirs["home"]] == pre[pre.index("--setenv") : pre.index("--setenv") + 3]
    binds = [pre[i + 1] for i, x in enumerate(pre) if x == "--bind"]
    assert binds == list(dirs.values())  # only the private directories are writable
    assert pre[pre.index("--ro-bind", pre.index("--tmpfs")) :].count(str(tmp_path / "ro")) >= 2  # bound read-only (src and dst)
    assert pre.index("--tmpfs") < pre.index("--bind") < pre.index("--setenv")  # the hiding comes before the binds
    assert "/run" in [pre[i + 1] for i, x in enumerate(pre) if x == "--tmpfs"]  # the daemon sockets are out of reach


def test_the_home_directory_and_the_runtime_directory_are_hidden_and_a_socket_is_masked(tmp_path, monkeypatch):
    home, run = tmp_path / "home" / "me", tmp_path / "xdg"
    home.mkdir(parents=True)
    run.mkdir()
    assert os.path.realpath(home) not in sandbox.hidden_dirs({"HOME": str(home)})  # under /tmp: the box has a fresh one anyway
    monkeypatch.setattr(sandbox, "_FRESH", ())  # the test directories are under /tmp: pretend they are not
    hidden = sandbox.hidden_dirs({"HOME": str(home), "XDG_RUNTIME_DIR": str(run)})
    assert "/run" in hidden and os.path.realpath(home) in hidden and os.path.realpath(run) in hidden
    assert sandbox.hidden_dirs({"HOME": "/"}).count("/") == 0  # the root is never a home to hide
    sock_dir = tmp_path / "s"
    sock_dir.mkdir()
    path = sock_dir / "d.sock"
    srv = socket.socket(socket.AF_UNIX)
    try:
        srv.bind(str(path))
        table = tmp_path / "unix"
        table.write_text(
            "Num RefCount Protocol Flags Type St Inode Path\n"
            f"0000: 00000002 00000000 00010000 0001 01 1 {os.path.realpath(path)}\n"
            "0000: 00000002 00000000 00010000 0001 01 2 @abstract\n"
            "0000: 00000002 00000000 00010000 0001 01 3\n"
        )
        assert sandbox.host_sockets([], str(table)) == [os.path.realpath(path)]
        assert sandbox.host_sockets([str(sock_dir)], str(table)) == []  # a directory the box hides anyway
        assert sandbox.host_sockets([str(sock_dir)], str(table), keep=[str(sock_dir)]) == [os.path.realpath(path)]
        assert sandbox.host_sockets([], str(tmp_path / "no-table")) == []
    finally:
        srv.close()


@needs_box
def test_a_command_in_the_box_has_no_network_no_home_and_no_environment(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    probe = (
        "import os, sys; "
        "print(os.environ.get('FAKE_TOKEN_FOR_TEST', 'unset')); "
        "print(sum(1 for ln in open('/proc/net/dev') if ':' in ln)); "
        "print(os.path.exists(os.path.expanduser('~') + '/.ssh'))"
    )
    argv = [*sandbox.bwrap_prefix(sandbox.check_box(), {"work": str(work)}, {"HOME": str(work)}, [], str(work)), sys.executable, "-I", "-c", probe]
    env = dict(os.environ, FAKE_TOKEN_FOR_TEST="leaky")
    r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["unset", "1", "False"]  # no variable of the caller, loopback only, an empty home


# ================================================================ star imports, a lazy target, the copy
STAR_OLD = "def keep():\n    return 1\n\n\ndef helper():\n    return 2\n\n\ndef dead2():\n    return 3\n"
STAR_NEW = "def keep():\n    return 1\n"
USER = "from pkg.a import *\n\n\ndef use():\n    return helper()\n"
STAR_ERR = "pkg/b.py:5: reads `helper`, which the patch removes from pkg/a.py; it comes in through `import *` at line 1"


def star_check(tmp, files, removed=("helper", "dead2"), old=STAR_OLD, new=STAR_NEW, extra=None):
    """pycheck on pkg/a.py losing `removed`; `files` are the other files of the tree, `extra` more patched files."""
    root = project(tmp, {"pkg/__init__.py": "", "pkg/a.py": new, **files})
    req = {
        "tree": root,
        "files": {"pkg/a.py": {"old": old, "new": new}, **(extra or {})},
        "removed": [{"path": "pkg/a.py", "qual": q} for q in removed],
    }
    return pycheck.check(req)


def test_a_star_import_that_reads_a_removed_name_is_a_failure_not_a_proof(tmp_path):
    res = star_check(tmp_path, {"pkg/b.py": USER})
    assert res["ok"] is False and res["errors"] == [STAR_ERR]  # `dead2` is not read by anyone: one finding


@pytest.mark.parametrize("line", ["from pkg.a import *", "from .a import *", "from a import *", "from pkg.a import*"])
def test_a_star_import_is_found_absolute_relative_and_from_a_source_root(tmp_path, line):
    res = star_check(tmp_path, {"pkg/b.py": USER.replace("from pkg.a import *", line)}, removed=("helper",))
    assert res["ok"] is False and res["errors"] == [STAR_ERR]


def test_a_star_import_is_no_finding_when_the_name_is_bound_unread_or_from_another_module(tmp_path):
    for body in (
        "from pkg.a import *\n\nhelper = 1\n\n\ndef use():\n    return helper\n",  # the importer binds the name itself
        "from pkg.a import *\n\nx = keep()\n",  # the name stays unread
        "from other.a import *\n\n\ndef use():\n    return helper()\n",  # a different module
        "from pkg.a import *\nfrom pkg.c import helper\n\n\ndef use():\n    return helper()\n",  # bound by another import
    ):
        assert star_check(tmp_path, {"pkg/b.py": body})["ok"] is True, body


def test_what_a_star_import_cannot_bring_is_not_counted(tmp_path):
    private_old = STAR_NEW + "\n\ndef _hidden():\n    return 2\n"
    user = "from pkg.a import *\n\n\ndef use():\n    return _hidden()\n"  # a `_private` name: no star import brings it
    assert star_check(tmp_path, {"pkg/b.py": user}, ("_hidden",), private_old)["ok"] is True
    listed = "__all__ = ['keep']\n\n\n" + STAR_NEW  # a literal __all__ that does not list `helper`
    assert star_check(tmp_path, {"pkg/b.py": USER}, ("helper",), "__all__ = ['keep']\n\n\n" + STAR_OLD, listed)["ok"]
    computed = "__all__ = ['keep'] + more()\n\n\n"  # a computed __all__ decides nothing: every name counts
    res = star_check(tmp_path, {"pkg/b.py": USER}, ("helper",), computed + STAR_OLD, computed + STAR_NEW)
    assert res["errors"] == [STAR_ERR]
    appended = (
        "__all__ = []\n__all__.append('helper')\n\n\n"  # another use of __all__ than the literal one: not literal
    )
    res = star_check(tmp_path, {"pkg/b.py": USER}, ("helper",), appended + STAR_OLD, appended + STAR_NEW)
    assert res["errors"] == [STAR_ERR]


def test_a_chain_of_star_imports_hands_the_removed_names_on(tmp_path):
    chain = {
        "pkg/b.py": "from pkg.a import *\n",
        "pkg/c.py": "from pkg.b import *\n\n\ndef use():\n    return helper()\n",
        "pkg/d.py": "from pkg.b import helper\n",  # an explicit import out of the re-exporting module
        "pkg/e.py": "from pkg.b import keep\n",  # a name that stays
    }
    res = star_check(tmp_path, chain, removed=("helper",))
    assert res["errors"] == [
        "pkg/c.py:5: reads `helper`, which the patch removes from pkg/a.py (re-exported by pkg/b.py); "
        "it comes in through `import *` at line 1",
        "pkg/d.py:1: imports `helper`, which the patch removes from pkg/a.py (re-exported by pkg/b.py)",
    ]


def test_a_chain_stops_where_a_file_defines_the_name_itself_and_a_cycle_ends(tmp_path):
    own = {
        "pkg/b.py": "from pkg.a import *\n\n\ndef helper():\n    return 9\n",
        "pkg/c.py": USER.replace("pkg.a", "pkg.b"),
    }
    assert star_check(tmp_path, own, removed=("helper",))["ok"] is True
    cycle = {
        "pkg/b.py": "from pkg.a import *\nfrom pkg.c import *\n",
        "pkg/c.py": "from pkg.b import *\n\n\ndef use():\n    return helper()\n",
    }
    res = star_check(tmp_path, cycle, removed=("helper",))
    assert [e.split(":")[0] for e in res["errors"]] == ["pkg/c.py"]  # the search ends, the finding is one


def test_the_other_patched_files_are_read_as_they_are_after_the_patch(tmp_path):
    gone_use = {"pkg/b.py": {"old": USER, "new": "from pkg.a import *\n"}}  # the patch removes `use` from b too
    assert star_check(tmp_path, {"pkg/b.py": USER}, ("helper",), extra=gone_use)["ok"] is True
    assert (
        star_check(tmp_path, {"pkg/b.py": USER}, ("helper",))["ok"] is False
    )  # b alone, as on disk: it still reads it


def test_a_star_import_user_makes_the_removal_failed_in_the_report_and_the_clean_one_stays(tmp_path):
    root = project(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": STAR_OLD, "pkg/b.py": USER})
    us = [unit("pkg/a.py", 5, 6, "helper"), unit("pkg/a.py", 9, 10, "dead2")]

    def render(units):
        names = {u.items[0].qual for u in units}
        text = STAR_OLD
        for n, cut in (("helper", "\n\ndef helper():\n    return 2\n"), ("dead2", "\n\ndef dead2():\n    return 3\n")):
            text = text.replace(cut, "") if n in names else text
        return {"pkg/a.py": text} if names else {}

    rep = fv.verify(root, us, render, {"pkg/a.py": STAR_OLD}, spec=spec(runtime=False), host=fv.Host())
    rows = by_symbol(rep)
    assert rows["helper"].status == fv.FAILED and "pkg/b.py:5: reads `helper`" in rows["helper"].detail
    assert rows["dead2"].status == fv.VERIFIED_LOCAL
    assert [u.items[0].qual for u in rep.kept] == ["dead2"]  # only the removal that passed stays in the patch


def test_the_python_check_makes_no_copy_of_the_tree_and_reads_the_pristine_one_read_only(tmp_path, monkeypatch):
    made, seen, boxes = [], [], []
    orig, real = fv.Workspace.candidate, pycheck.check
    monkeypatch.setattr(fv.Workspace, "candidate", lambda self, edits: made.append(edits) or orig(self, edits))
    monkeypatch.setattr(pycheck, "check", lambda req: seen.append(req["tree"]) or real(req))

    class Rec(fv.Host):
        def box(self, argv, dirs, env, ro, cwd):
            boxes.append((list(ro), cwd))
            return None  # no box on this host: the same code in this process

    root = project(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": STAR_OLD, "pkg/b.py": USER})

    def render(us):
        return {"pkg/a.py": STAR_OLD.replace("\n\ndef dead2():\n    return 3\n", "")} if us else {}

    rep = fv.verify(
        root, [unit("pkg/a.py", 9, 10, "dead2")], render, {"pkg/a.py": STAR_OLD}, spec=spec(runtime=False), host=Rec()
    )
    assert rep.rows[0].status == fv.VERIFIED_LOCAL
    assert made == [] and seen and all(t.endswith("/base") for t in seen)  # the pristine copy, never a clone per check
    assert boxes and all(cwd.endswith("/base") and cwd in ro for ro, cwd in boxes)  # bound read-only: it is in `ro`


class CountingTarget(FakeTarget):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.asked = 0

    def unavailable(self):
        self.asked += 1
        return super().unavailable()


CARGO = {"runtime": {"check": "cargo check", "argv": ["cargo", "check"], "baseline": True}}


def test_the_target_is_not_asked_when_there_is_nothing_to_check(tmp_path, monkeypatch):
    scripted(monkeypatch)
    t = CountingTarget()
    rep = fv.verify(project(tmp_path, FILES), [], render_names, {}, fv.Options(t), spec=spec())
    assert t.asked == 0 and t.jobs == [] and rep.rows == () and rep.notes == () and rep.checks == 0
    zig = [
        unit("a.zig", 1, 2, "f", lang="zig")
    ]  # a language with no runtime check at all does not need the target either
    fv.verify(project(tmp_path / "z", {"a.zig": "x"}), zig, render_names, {}, fv.Options(t), spec=spec())
    assert t.asked == 0


def test_the_target_is_asked_once_however_many_languages_and_rounds(tmp_path, monkeypatch):
    scripted(monkeypatch, bad=["a1"])
    us = units3() + [unit("src/lib.rs", 3, 5, "old_fn", lang="rust")]
    files = {**FILES, "src/lib.rs": "x\n"}
    t = CountingTarget()
    rep = fv.verify(project(tmp_path, files), us, render_names, {}, fv.Options(t), spec=spec(rust=CARGO))
    assert t.asked == 1 and len(t.jobs) >= 2 and by_symbol(rep)["old_fn"].status == fv.VERIFIED_RUNTIME
    down = CountingTarget(why="the target does not answer")
    rep = fv.verify(project(tmp_path / "d", files), us, render_names, {}, fv.Options(down), spec=spec(rust=CARGO))
    assert (
        down.asked == 1 and down.jobs == []
    )  # a target that is down is asked once too, the skip line carries its reason
    assert [n[1] for n in rep.notes if n[1].startswith(fv.RUNTIME_SKIPPED)] == [
        "runtime mode: no execution target (the target does not answer); tests and build checks were not run"
    ]


def test_a_sibling_directory_with_the_same_prefix_is_outside_the_tree(tmp_path):
    assert fv._inside("/a/b", "/a/b") and fv._inside("/a/b", "/a/b/c") and fv._inside("/", "/x")
    assert not fv._inside("/a/b", "/a/bc") and not fv._inside("/a/b", "/a") and not fv._inside("/a/b", "/a/bc/d")
    root, evil, scratch = tmp_path / "proj", tmp_path / "proj-evil", tmp_path / "scratch"
    for d in (root, evil, scratch):
        d.mkdir()
    (evil / "secret.txt").write_text("s")
    (root / "ok.txt").write_text("o")
    os.symlink(
        evil, root / "link"
    )  # a tracked path that goes through a link to a sibling whose name starts like the root

    class H(fv.Host):
        def tracked(self, r):
            return ["ok.txt", "link/secret.txt"]

    ws = fv.Workspace(str(root), H(), str(scratch))
    assert (Path(ws.base) / "ok.txt").read_text() == "o"
    assert not (Path(ws.base) / "link").exists() and not list(Path(ws.base).rglob("secret.txt"))
