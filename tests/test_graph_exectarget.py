"""One execution target (graph.runtime.ExecTarget and exec_target, the check run of graph.rtbox): the executing checks of a verified fix travel as a
check run of the delivery of the runtime-signal mode, to the same one allowed ssh host and nowhere else.

python3 -m pytest -q -p no:cacheprovider tests/test_graph_exectarget.py

Nothing here starts a project, a test framework, bwrap or ssh: "ssh" is a function call over a directory (tests/graph_rt_target.py), the box is
graph.rtbox.box() with a starter that answers from a table. The repositories are synthetic. The job and the result are the real types of graph.fixverify.
"""

import io
import json
import os
import shutil
import tarfile

import graph_fake_target as F
import pytest
from graph_rt_target import FakeTransport, RuntimeJob, RuntimeResult, make_repo

from dead_code_checker.graph import rtbox, rtcfg, runtime, vcs

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.usefixtures("hermetic_git", "data_root"),
]

HOST = "target.invalid"
ENABLED = rtcfg.RuntimeCfg(enabled=True, host=HOST)


@pytest.fixture
def env(data_root):
    return {"PATH": os.environ["PATH"], "DEAD_CODE_CHECKER_HOME": str(data_root), rtcfg.ENV_HOSTS: HOST}


FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": "def a1():\n    return 1\n\n\ndef a2():\n    return 2\n",
    "pkg/b.py": "def b1():\n    return 1\n\n\ndef b2():\n    return 2\n",
    "svc/go.mod": "module example.com/svc\n\ngo 1.21\n",
    "svc/x.go": "package svc\n\nfunc X() int {\n\treturn 1\n}\n",
}


class CheckBox(F.FakeBox):
    """The box of the target for check runs: `rule(cwd, cmd)` -> (rc, stderr text) or None (then the FakeBox default: rc 0)."""

    def __init__(self, rule=None, **kw):
        super().__init__(**kw)
        self.rule = rule

    def __call__(self, argv, cwd, timeout, env):
        got = super().__call__(argv, cwd, timeout, env)
        hit = self.rule(cwd, self.calls[-1]["cmd"]) if self.rule else None
        return (hit[0], b"", hit[1].encode()) if hit else (0, b"", b"") if got[0] == 0 else got


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo", FILES, exe=())


def target(env, tmp_path, **kw):
    t = FakeTransport(tmp_path / "target", **kw)
    got, why = runtime.exec_target(str(tmp_path), ENABLED, env, make=lambda tg, e: t)
    assert why == "" and isinstance(got, runtime.ExecTarget)
    return got, t


def job(
    repo, edits=(), dirs=("",), argv=("python3", "-m", "pytest", "-x"), lang="python", check="pytest", timeout_s=60
):
    return RuntimeJob(str(repo), lang, check, tuple(argv), "", tuple(edits), timeout_s, tuple(dirs))


def sent_inner(t):
    """The delivery (in.tar) of the one run that was put."""
    (data,) = t.sent.values()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as outer:
        return tarfile.open(fileobj=io.BytesIO(outer.extractfile("in.tar").read()), mode="r:")


# ================================================================ who may be the target
def test_the_one_target_follows_the_rules_of_the_runtime_command(env, tmp_path):
    made = []
    make = lambda tg, e: made.append(tg) or FakeTransport(tmp_path / "t")  # noqa: E731
    got, why = runtime.exec_target(str(tmp_path), ENABLED, env, make=make)
    assert isinstance(got, runtime.ExecTarget) and why == "" and made == [runtime.rttransport.Target(HOST)]
    assert got.name == "tester@target.invalid"
    off, why = runtime.exec_target(str(tmp_path), rtcfg.RuntimeCfg(False, HOST), env, make=make)
    assert off is None and "[graph.runtime] enabled = true" in why
    got, why = runtime.exec_target(str(tmp_path), rtcfg.RuntimeCfg(False), env, host=HOST, make=make)
    assert got is not None  # the command line opts in for this call
    clean = {k: v for k, v in env.items() if k != rtcfg.ENV_HOSTS}
    none, why = runtime.exec_target(str(tmp_path), ENABLED, clean, make=make)
    assert none is None and rtcfg.ENV_HOSTS in why and f"--host {HOST}" in why
    none, why = runtime.exec_target(str(tmp_path), rtcfg.RuntimeCfg(True, "localhost"), env, make=make)
    assert none is None and "this machine" in why
    none, why = runtime.exec_target(str(tmp_path), rtcfg.RuntimeCfg(True), env, host="localhost", make=make)
    assert none is None and "this machine" in why
    assert len(made) == 2  # nothing else was ever built


def test_the_config_is_read_from_the_repository_when_none_is_given(env, tmp_path, repo):
    (repo / ".dead-code-checker.toml").write_text(f'[graph.runtime]\nenabled = true\nhost = "{HOST}"\n')
    got, why = runtime.exec_target(str(repo), environ=env, make=lambda tg, e: FakeTransport(tmp_path / "t"))
    assert got is not None and why == ""
    none, why = runtime.exec_target(
        str(repo), environ={**env, rtcfg.ENV_HOSTS: ""}, make=lambda tg, e: FakeTransport(tmp_path / "t")
    )
    assert none is None and "have not allowed" in why
    (repo / ".dead-code-checker.toml").write_text("")
    none, why = runtime.exec_target(str(repo), environ=env)
    assert none is None and "enabled = true" in why


def test_a_job_that_cannot_reach_the_target_is_unavailable_with_the_reason(env, tmp_path):
    class Down(FakeTransport):
        def reachable(self):
            return False, "ssh timeout"

    tg, _ = runtime.exec_target(str(tmp_path), ENABLED, env, make=lambda t, e: Down(tmp_path / "down"))
    assert tg.unavailable() == "tester@target.invalid unreachable: ssh timeout"
    ok, _ = target(env, tmp_path)
    assert ok.unavailable() == ""


# ================================================================ the job travels as a check run
def test_a_job_is_a_check_run_of_head_with_the_edits_over_it_and_the_exit_code_comes_back(env, tmp_path, repo):
    box = CheckBox()
    tg, t = target(env, tmp_path, box=box)
    edit = "def a2():\n    return 2\n"
    res = tg.run(job(repo, [("pkg/a.py", edit)]))
    assert tuple(res) == (True, 0, "") and t.log == ["put", "run", "pull", "cleanup"]
    with sent_inner(t) as tf:
        spec = json.load(tf.extractfile("spec.json"))
        assert tf.extractfile("tree/pkg/a.py").read().decode() == edit  # the patched text, not HEAD
        assert tf.extractfile("tree/pkg/b.py").read().decode() == FILES["pkg/b.py"]  # HEAD
    assert spec["runs"] == [{"label": "pytest", "langs": ["python"], "argv": list(job(repo).argv), "cwd": ""}]
    assert spec["files"]["pkg/a.py"] == vcs.blob_sha(
        edit.encode(), "sha1"
    )  # the blob id of the edit: the box checks it
    assert spec["files"]["pkg/b.py"] != spec["files"]["pkg/a.py"] and spec["timeout"] == 60
    (call,) = box.calls
    assert call["cmd"] == list(job(repo).argv) and call["cwd"].endswith("/tree")
    assert "DCRT_OUT" not in call["env"] and "PYTHONPATH" not in call["env"]  # no tracer: nothing of it in a check run
    assert call["env"]["PYTHONHASHSEED"] == "0"  # the environment of the language, as for a traced run


def test_a_failing_check_gives_its_exit_code_and_the_first_error_line_and_nothing_else(env, tmp_path, repo):
    box = CheckBox(lambda cwd, cmd: (1, "# header\ntests/test_b.py:3: AttributeError: b1\nmore lines nobody asked for"))
    tg, t = target(env, tmp_path, box=box)
    res = tg.run(job(repo, [("pkg/b.py", "def b2():\n    return 2\n")]))
    assert tuple(res) == (True, 1, "tests/test_b.py:3: AttributeError: b1")
    assert "nobody asked for" not in json.dumps(list(res))


def test_one_command_per_package_directory_and_the_first_failure_is_the_answer(env, tmp_path, repo):
    seen = []

    def rule(cwd, cmd):
        seen.append(cwd.split("/tree", 1)[1] or "/")
        return (2, "svc/x.go:3: undefined: Y") if cwd.endswith("/tree/svc") else None

    tg, t = target(env, tmp_path, box=CheckBox(rule))
    res = tg.run(job(repo, [("svc/x.go", "package svc\n")], dirs=("", "svc"), argv=("go", "test", "./..."), lang="go"))
    assert tuple(res) == (True, 2, "svc/x.go:3: undefined: Y") and seen == ["/", "/svc"]
    with sent_inner(t) as tf:
        runs = json.load(tf.extractfile("spec.json"))["runs"]
    assert [r["cwd"] for r in runs] == ["", "svc"] and runs[0]["langs"] == ["go"]


def test_a_language_without_a_tracer_is_a_check_run_with_no_languages(env, tmp_path, repo):
    tg, t = target(env, tmp_path)
    assert tg.run(job(repo, argv=("cargo", "check", "--offline"), lang="rust", check="cargo check")).ran
    with sent_inner(t) as tf:
        (run,) = json.load(tf.extractfile("spec.json"))["runs"]
    assert run["langs"] == [] and run["cwd"] == ""


# ================================================================ nothing ran: ran=False and the reason, never an exception
@pytest.mark.parametrize(
    "kw, want",
    [
        ({"put_rc": 255}, "did not take the project copy"),
        ({"put_rc": 1}, "could not be put"),
        ({"run_rc": 255}, "no transport"),
        ({"run_rc": 2}, "ended with rc 2"),
        ({"tools": F.NoBwrap}, "has no sandbox"),
        ({"box": CheckBox(timeout_main=True)}, "did not finish in 60 s"),
    ],
)
def test_a_target_that_could_not_run_the_check_says_why(env, tmp_path, repo, kw, want):
    tg, t = target(env, tmp_path, **kw)
    res = tg.run(job(repo))
    assert res.ran is False and res.rc is None and want in res.error
    assert t.log[-1] == "cleanup"  # a started run is always cleaned up, whatever it answered


def test_an_edit_outside_the_copy_a_missing_directory_and_a_bad_command_are_reasons(env, tmp_path, repo):
    tg, t = target(env, tmp_path)
    r = tg.run(job(repo, [("nope/x.py", "x = 1\n")]))
    assert not r.ran and "an edited file is not in the project copy (nope/x.py)" in r.error and t.log == []
    r = tg.run(job(repo, dirs=("missing",)))
    assert not r.ran and r.error
    assert not tg.run(job(repo, dirs=())).ran and not tg.run(job(repo, argv=())).ran
    assert not tg.run(job(repo, dirs=tuple(str(i) for i in range(9)))).ran  # over the 8 commands of a spec


def test_a_secret_in_an_edit_stops_the_copy_like_one_in_a_tracked_file(env, tmp_path, repo):
    tg, t = target(env, tmp_path)
    assert tg.run(job(repo, [("pkg/a.py", "x = 1\n")])).ran
    t.log.clear()
    r = tg.run(job(repo, [("pkg/a.py", "KEY = '" + "-----BEGIN " + "PRIVATE KEY-----" + "'\n")]))
    assert not r.ran and "the secret checks stop the copy" in r.error and "pkg/a.py" in r.error and t.log == []


def test_a_job_that_raises_is_a_message_never_an_exception(env, tmp_path, repo, monkeypatch):
    tg, _ = target(env, tmp_path)
    monkeypatch.setattr(runtime, "pack", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = tg.run(job(repo))
    assert tuple(r) == (False, None, "internal error: RuntimeError: boom")


def test_the_job_type_of_the_stand_in_is_what_the_target_reads():
    assert RuntimeJob._fields == ("root", "lang", "check", "argv", "marker", "edits", "timeout_s", "dirs")
    assert RuntimeResult._fields[:2] == ("ran", "rc")


# ================================================================ the spec of a check run, on the target
def spec_doc(**run):
    r = {"label": "pytest", "langs": ["python"], "argv": ["python3", "-m", "pytest"], "cwd": "", **run}
    return {
        "schema": 1,
        "run_id": "0" * 32,
        "commit": "a" * 40,
        "target": "t",
        "runs": [r],
        "timeout": 60,
        "files": {"a.py": "b" * 40},
    }


def test_the_box_accepts_a_check_run_and_still_refuses_what_it_refused():
    s = rtbox.check_spec(spec_doc())
    assert s.runs[0].cwd == "" and s.runs[0].argv == ("python3", "-m", "pytest")
    assert rtbox.check_spec(spec_doc(langs=[])).runs[0].langs == ()  # rust, typescript: no tracer, no language
    assert rtbox.check_spec(spec_doc(cwd="svc/sub")).runs[0].cwd == "svc/sub"
    for bad in ({"cwd": "../x"}, {"cwd": "/etc"}, {"cwd": 3}, {"langs": ["cobol"]}):
        with pytest.raises(rtbox.BoxError):
            rtbox.check_spec(spec_doc(**bad))
    traced = spec_doc()
    del traced["runs"][0]["cwd"]
    assert rtbox.check_spec(traced).runs[0].cwd is None  # a traced run is as before ...
    traced["runs"][0]["langs"] = []
    with pytest.raises(rtbox.BoxError):  # ... and needs a language
        rtbox.check_spec(traced)
    extra = spec_doc()
    extra["runs"][0]["env"] = {"A": "1"}
    with pytest.raises(rtbox.BoxError):
        rtbox.check_spec(extra)


def test_a_result_with_an_error_line_is_cleaned_to_the_note_length_and_a_foreign_key_is_refused():
    chars = rtcfg.data()["result"]["note_chars"]
    run = {
        "label": "pytest",
        "langs": [],
        "argv0": "python3",
        "rc": 1,
        "timed_out": False,
        "seconds": 1.0,
        "tracers": {},
    }
    c = runtime._clean_run({**run, "error": "x" * 900}, chars)
    assert c["error"] == "x" * chars and c["rc"] == 1
    assert "error" in runtime.RUN_OPT and "env" not in runtime.RUN_OPT


# ================================================================ fix --verify over this target
def test_a_verified_fix_runs_its_executing_check_on_this_target_and_drops_what_the_tests_there_miss(env, tmp_path, repo):
    """graph.fixverify with the REAL ExecTarget over the fake channel: the baseline runs on HEAD, the removal of `b1` on HEAD with the
    patched text over it; the target's tests (a rule over the copy it was sent) fail for b1 and pass for a1, so b1 is dropped and a1 stays."""
    from dead_code_checker.graph import fix, fixverify as fv

    def rule(cwd, cmd):
        text = open(os.path.join(cwd, "pkg/b.py"), encoding="utf-8").read()
        return None if "def b1" in text else (1, "tests/test_b.py:3: AttributeError: b1")

    tg, t = target(env, tmp_path, box=CheckBox(rule))
    item = lambda path, qual: fix.Item(path, 1, qual, "dc.dead")  # noqa: E731
    units = [
        fv.Unit("pkg/a.py", 1, 2, (item("pkg/a.py", "a1"),), "python"),
        fv.Unit("pkg/b.py", 1, 2, (item("pkg/b.py", "b1"),), "python"),
    ]

    def render(us):
        return {u.path: fix.cut(FILES[u.path], [(u.first, u.last)]) for u in us}

    rep = fv.verify(str(repo), units, render, dict(FILES), fv.Options(tg, ""), workers=1)
    st = {r.qual: r.status for r in rep.rows}
    assert st == {"a1": "verified-runtime", "b1": "failed"}, rep.rows
    assert [r.detail for r in rep.rows if r.qual == "b1"] == ["tests/test_b.py:3: AttributeError: b1"]
    assert len(t.sent) >= 1  # the checks went over the channel, none ran here
