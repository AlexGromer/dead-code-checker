"""graph.runtime and graph.rttransport: the controlling half of the runtime-signal mode, the target rules, the project copy, the ssh command
forms, the round trip, what comes back, the stored runs and the effect on `graph check`.

python3 -m pytest -q -p no:cacheprovider tests/test_graph_runtime.py

The user's rules are the subject: project code runs only on ONE separate ssh host the user allowed, never here. Every round trip uses
tests/graph_rt_target.py: "ssh" is a function call over a directory, the "box" is graph.rtbox.box() with a starter that executes nothing, and a
guard on subprocess/os says which processes the code under test started (git, never the project, a test framework, bwrap or ssh). The few tests of
the ssh command line use fake run/popen functions. The repositories are synthetic; no address of a real host is spelled anywhere.
"""

import hashlib
import io
import ipaddress
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import graph_fake_target as F
import pytest
from graph_rt_target import FakeTransport, Guard, make_repo, row, stored_doc, write_cfg
from graph_support import commit_all, git, inline_units

from dead_code_checker.graph import artifacts, cli, rtbox, rtcfg, rttransport, runtime, vcs

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.usefixtures("hermetic_git", "data_root"),
]

HOST = "target.invalid"
ENABLED = rtcfg.RuntimeCfg(enabled=True, host=HOST)
ALLOW = {rtcfg.ENV_HOSTS: HOST}
RUN_AT = 1_700_000_000


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    inline_units(monkeypatch)  # pools run in threads: a guard on os.fork must not break the secret scan


@pytest.fixture
def env(data_root):
    """The environment of the user: its own data root; the host of the tests allowed."""
    return {"PATH": os.environ["PATH"], "DEAD_CODE_CHECKER_HOME": str(data_root), **ALLOW}


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


def fake(tmp_path, **kw):
    """(make, transport): a transport factory for runtime.run(make=...) that hands out one FakeTransport on a private directory."""
    t = FakeTransport(tmp_path / "target", **kw)
    return (lambda target, env: t), t


def v4(n: int) -> str:
    return str(ipaddress.IPv4Address(n))


def v6(n: int) -> str:
    return str(ipaddress.IPv6Address(n))


# ================================================================ the target: who may be used
def test_this_machine_is_never_a_target(monkeypatch):
    """A host that is this machine (loopback, its own names and addresses) is refused, whatever else is in place; a different host is not."""
    monkeypatch.setattr(socket, "gethostname", lambda: "mybox")
    monkeypatch.setattr(socket, "getfqdn", lambda: "mybox.corp.invalid")
    mine = {ipaddress.IPv4Address(0x0A890011), ipaddress.IPv6Address(0xFE800000000000000000000000000001)}
    monkeypatch.setattr(rttransport, "_own_addresses", lambda: set(mine))
    monkeypatch.setattr(
        rttransport, "_hosts_file_addresses", lambda n: {ipaddress.IPv4Address(0x7F000101)} if n == "alias" else set()
    )
    for bad in (
        "localhost",
        "LocalHost.",
        "localhost.localdomain",
        "x.localhost",
        v4(0x7F000001),
        v4(0x7F090807),
        v6(1),
        "[" + v6(1) + "]",
        "::ffff:" + v4(0x7F000001),
        v4(0),
        "mybox",  # its own host name
        "MYBOX.corp.invalid",  # its FQDN, any case
        "mybox.corp.invalid.",
        v4(0x0A890011),  # an address of its interfaces
        v6(0xFE800000000000000000000000000001),
        "alias",  # a name that /etc/hosts maps to a loopback address
    ):
        assert "this machine" in rttransport.work_machine(bad), bad
    for good in (
        HOST,
        v4(0x0A890012),
        v4(0xC0000207),
        "ci.corp.invalid",
        "mybox2",
        v6(0x20010DB8000000000000000000000005),
        "",
    ):
        assert rttransport.work_machine(good) == "", good


def test_a_host_of_this_machine_gives_the_one_skip_line_and_no_transport_is_built(env, tmp_path, monkeypatch):
    made = []
    for host in ("localhost", v4(0x7F000001), v6(1)):
        rt = rtcfg.RuntimeCfg(enabled=True, host=host)
        t, why = runtime.target_for(rt, env, make=lambda *a: made.append(a))
        assert t is None and "this machine" in why and "never on this one" in why
        out = runtime.run(str(tmp_path), rt, environ=env, make=lambda *a: made.append(a))
        assert out.status == "skipped" and out.line == rtcfg.skip_line(why) and "this machine" in out.line
    assert made == []
    t, why = runtime.target_for(rtcfg.RuntimeCfg(enabled=True), env, host="localhost", make=lambda *a: made.append(a))
    assert t is None and "this machine" in why and made == []  # --host does not skip this rule


def test_the_host_of_the_repository_is_used_only_when_the_user_allows_it(env, tmp_path):
    made = []
    make = lambda target, e: made.append(target) or object()  # noqa: E731
    clean = {k: v for k, v in env.items() if k != rtcfg.ENV_HOSTS}
    t, why = runtime.target_for(ENABLED, clean, make=make)
    assert t is None and made == []
    assert HOST in why and rtcfg.ENV_HOSTS in why and f"--host {HOST}" in why  # says exactly what and how to allow it
    for other in ("", "other.invalid", f"{HOST}x", "a.invalid,b.invalid"):
        t, why = runtime.target_for(ENABLED, {**clean, rtcfg.ENV_HOSTS: other}, make=make)
        assert t is None and rtcfg.ENV_HOSTS in why
    t, why = runtime.target_for(ENABLED, {**clean, rtcfg.ENV_HOSTS: f"x.invalid, {HOST.upper()} ,y.invalid"}, make=make)
    assert (
        t is not None and why == "" and made == [rttransport.Target(HOST)]
    )  # the list is comma-separated, any case, blanks cut
    made.clear()
    t, why = runtime.target_for(ENABLED, {**env, rtcfg.ENV_HOSTS: f"tester@{HOST}:2222"}, make=make)
    assert made == [rttransport.Target(HOST, "tester", 2222)]  # the login is the user's entry of the list


def test_the_login_comes_from_the_users_side_only(env, tmp_path):
    """The user approves a host, not a login: `user` and `port` of the repository's file are not even read, and the command line wins over
    the user's own entry of the list."""
    from dead_code_checker.graph import config

    (tmp_path / ".dead-code-checker.toml").write_text(f'[graph.runtime]\nenabled = true\nhost = "{HOST}"\nuser = "root"\nport = 22\n')
    cfg = config.load(str(tmp_path), environ={}).runtime
    made = []
    make = lambda target, e: made.append(target) or object()  # noqa: E731
    t, why = runtime.target_for(cfg, env, make=make)  # allowed host, no login anywhere: ssh's own configuration decides
    assert t is not None and made == [rttransport.Target(HOST, "", 0)]
    made.clear()
    runtime.target_for(cfg, {**env, rtcfg.ENV_HOSTS: f"ci@{HOST}:2200"}, make=make)
    runtime.target_for(cfg, {**env, rtcfg.ENV_HOSTS: f"ci@{HOST}:2200"}, make=make, user="me", port=2300)
    runtime.target_for(cfg, {**env, rtcfg.ENV_HOSTS: HOST}, host=HOST, make=make, user="me")
    assert made == [rttransport.Target(HOST, "ci", 2200), rttransport.Target(HOST, "me", 2300), rttransport.Target(HOST, "me", 0)]
    assert all(x.user != "root" and x.port != 22 for x in made)
    for kw in ({"user": "a b"}, {"user": "-oProxyCommand=x"}, {"port": 70000}, {"port": True}):
        t_, why = runtime.target_for(cfg, env, make=make, **kw)
        assert t_ is None and ("--user" in why or "--port" in why)
    assert len(made) == 3  # a bad login made no transport


def test_a_host_on_the_command_line_skips_the_list_but_not_the_host_rules(env, tmp_path):
    made = []
    make = lambda target, e: made.append(target) or object()  # noqa: E731
    clean = {k: v for k, v in env.items() if k != rtcfg.ENV_HOSTS}
    t, why = runtime.target_for(rtcfg.RuntimeCfg(True), clean, host="given.invalid", make=make)
    assert t is not None and made == [rttransport.Target("given.invalid")]
    made.clear()
    t, why = runtime.target_for(ENABLED, clean, host="given.invalid", make=make)  # the command line wins over the file
    assert made == [rttransport.Target("given.invalid")]
    for bad in ("-oProxyCommand=x", "a b", "h;rm", "h\nx", "a@b"):
        t, why = runtime.target_for(rtcfg.RuntimeCfg(True), clean, host=bad, make=make)
        assert t is None and why.startswith("the host is not usable") and len(made) == 1


def test_no_host_at_all_is_a_reason(env):
    t, why = runtime.target_for(rtcfg.RuntimeCfg(True), env, make=lambda *a: object())
    assert t is None and "--host" in why and "[graph.runtime] host" in why


def test_ssh_has_to_be_there_unless_a_transport_is_given(env, monkeypatch):
    real_which = runtime.safepath.which
    monkeypatch.setattr(runtime.safepath, "which", lambda name: "" if name == "ssh" else real_which(name))
    t, why = runtime.target_for(ENABLED, env)
    assert t is None and "ssh is missing" in why
    t, why = runtime.target_for(ENABLED, env, make=lambda *a: object())
    assert t is not None


def test_an_unreachable_target_is_a_reason_not_a_crash(env, tmp_path):
    class Down(FakeTransport):
        def reachable(self):
            return False, "ssh timeout"

    t = Down(tmp_path / "t")
    assert runtime.resolve(ENABLED, env, make=lambda *a: t) == (None, "tester@target.invalid unreachable: ssh timeout")

    def boom(target, e):
        raise RuntimeError("config exploded")

    assert runtime.resolve(ENABLED, env, make=boom)[1].startswith("RuntimeError")


# ================================================================ nothing runs here: exactly one skip line
@pytest.mark.parametrize(
    "rt, e, why",
    [
        (rtcfg.RuntimeCfg(True), {}, "no host"),
        (rtcfg.RuntimeCfg(True, HOST), {}, "have not allowed it"),
        (rtcfg.RuntimeCfg(True, "localhost"), ALLOW, "this machine"),
    ],
)
def test_without_a_usable_target_nothing_runs_and_exactly_one_line_is_the_result(repo, monkeypatch, rt, e, why):
    guard = Guard(monkeypatch, allow=())  # not even git: with no target the step stops before it reads the repository
    out = runtime.run(str(repo), rt, environ={"PATH": ""} | e)
    assert guard.started == []
    assert out.status == "skipped" and out.level == "I" and out.line.startswith("runtime mode: no execution target (")
    assert (
        why in out.line
        and out.line.count("\n") == 0
        and out.line == rtcfg.skip_line(out.line[out.line.index("(") + 1 : -1])
    )
    f = out.finding()
    assert (f.level, f.code, f.msg) == ("I", "dc.runtime-skipped", out.line)
    assert out.dir == "" and out.doc is None


def test_the_command_line_prints_that_one_line_and_exits_0(repo, monkeypatch, capsys):
    write_cfg(
        repo, '[graph.runtime]\nenabled = true\nhost = "target.invalid"\ncommand = "rm -rf /"\n'
    )  # a hostile config: it is data for a box
    monkeypatch.delenv(rtcfg.ENV_HOSTS, raising=False)
    guard = Guard(monkeypatch, allow=("git",))
    assert cli.main(["runtime", "--root", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.strip().count("\n") == 0 and out.startswith("runtime mode: no execution target (")
    assert f"--host {HOST}" in out and rtcfg.ENV_HOSTS in out
    assert set(guard.started) <= {"git"}  # git reads; nothing else
    assert cli.main(["runtime", "--root", str(repo), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert (
        doc["status"] == "skipped"
        and doc["line"].startswith("runtime mode: no execution target")
        and doc["symbols"] == 0
    )
    assert cli.main(["runtime", "--root", str(repo), "--host", "localhost"]) == 0
    assert "this machine" in capsys.readouterr().out


def test_the_command_line_says_a_project_that_did_not_opt_in_is_off_and_exits_2(repo, capsys):
    assert cli.main(["runtime", "--root", str(repo)]) == 2
    assert "enabled = true" in capsys.readouterr().out
    assert cli.main(["runtime", "--classes"]) == 0  # no target, no repository needed
    assert "python: executing (target only)" in capsys.readouterr().out
    assert cli.main(["runtime", "--classes", "--json"]) == 0
    assert "python" in json.loads(capsys.readouterr().out)
    assert (
        cli.main(["runtime", "--root", str(repo), "--host", "localhost"]) == 0
    )  # --host opts in for this run: and says why it cannot go
    assert "this machine" in capsys.readouterr().out


def test_the_command_line_outside_a_repository_is_a_usage_error(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cli.main(["runtime", "--root", str(plain)]) == 2
    assert "not inside a git repository" in capsys.readouterr().err


def test_the_command_line_runs_the_round_trip_with_a_transport(repo, env, tmp_path, monkeypatch, capsys):
    t = FakeTransport(tmp_path / "target")
    monkeypatch.setattr(runtime, "make_transport", lambda target, e=None: t)
    real_which = runtime.safepath.which
    monkeypatch.setattr(runtime.safepath, "which", lambda name: "/usr/bin/ssh" if name == "ssh" else real_which(name))
    write_cfg(repo, f'[graph.runtime]\nenabled = true\nhost = "{HOST}"\n')
    monkeypatch.setenv(rtcfg.ENV_HOSTS, HOST)
    assert cli.main(["runtime", "--root", str(repo), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "ok" and doc["symbols"] == 4 and Path(doc["dir"]).is_dir()
    assert t.log == ["reachable", "put", "run", "pull", "cleanup"]
    assert runtime.load_latest(str(repo)).dir == doc["dir"]


def test_the_whole_fake_round_trip_starts_no_process_but_git(env, repo, tmp_path, monkeypatch):
    make, t = fake(tmp_path)
    guard = Guard(monkeypatch)
    out = runtime.run(str(repo), ENABLED, environ=env, make=make, now=RUN_AT, run_id=F.RUN_ID)
    assert out.status == "ok", out.line
    assert set(guard.started) == {"git"}
    assert [c["cmd"][0] for c in t.box.calls] == [
        "python3",
        "go",
        "go",
    ]  # what the BOX was asked to run: pytest, go test, go tool cover
    assert (
        t.log == ["reachable", "put", "run", "pull", "cleanup"] and not t.dir(F.RUN_ID).exists()
    )  # the run's directory is removed


# ================================================================ secrets in what the target says
def test_what_the_target_says_is_masked_before_it_is_stored_or_shown():
    aws = "AKIA" + "FAKEFAKEFAKEFAKE"  # built here: a key-shaped literal in the source would trip a secret scanner
    leak = f"boom DB_PASSWORD=hunter2hunter2 postgres://bob:s3cr3tpw@db.example/x {aws}"
    for line in (rtcfg.first_error(leak, ""), rtcfg.first_error("", "# pkg\n" + leak), rtcfg.mask_secrets(leak)):
        assert "hunter2hunter2" not in line and "s3cr3tpw" not in line and aws not in line and "***" in line
    run = runtime._clean_run(
        {"label": "t", "rc": 1, "error": leak, "why": leak, "tracers": {"python": {"note": leak}}}, 300
    )
    assert "hunter2hunter2" not in json.dumps(run) and "s3cr3tpw" not in json.dumps(run) and aws not in json.dumps(run)
    doc = {
        "schema": rtbox.SCHEMA,
        "run_id": "r" * 32,
        "commit": "c" * 40,
        "target": "u@h",
        "status": "ok",
        "notes": [leak],
        "runs": [],
        "files": {},
        "executed": [],
    }
    assert runtime.check_result(doc, {}, "r" * 32, "c" * 40) == ""
    assert "hunter2hunter2" not in json.dumps(doc) and "***" in doc["notes"][0]


# ================================================================ the project copy: tracked files at HEAD
RUNS = [{"label": "t", "langs": ["python"], "argv": ["pytest"]}]
# the header lines of a private key, put together here so that this file holds no key-shaped text
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
PEM_SSH = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
PEM_PLAIN = "-----BEGIN " + "PRIVATE KEY-----"


def test_the_copy_is_the_tracked_files_of_head_and_nothing_of_the_work_tree(repo):
    (repo / "pkg" / "a.py").write_text("def edited_but_not_committed():\n    pass\n")  # not in the copy
    (repo / "untracked.py").write_text("x = 1\n")
    (repo / ".env").write_text("TOKEN=hunter2hunter2hunter2\n")  # an untracked secret never leaves
    commit = vcs.head_commit(str(repo))
    pkg = runtime.pack(str(repo), commit, RUNS, 300, "tester@target.invalid", F.RUN_ID)
    with tarfile.open(fileobj=io.BytesIO(pkg.data)) as outer:
        assert outer.getnames() == [
            "in.tar",
            *("box/" + n for n in rttransport.TOOL_NAMES),
        ]  # exactly what the put command takes out
        inner = outer.extractfile("in.tar").read()
        boxed = {n: outer.extractfile("box/" + n).read() for n in rttransport.TOOL_NAMES}
    assert pkg.sha256 == hashlib.sha256(inner).hexdigest()
    assert pkg.tool == {n: hashlib.sha256(b).hexdigest() for n, b in boxed.items()}
    for n, path in rtbox.delivery_files().items():  # the tool's OWN files, byte for byte
        assert boxed[n] == Path(path).read_bytes()
    with tarfile.open(fileobj=io.BytesIO(inner)) as tf:
        names = tf.getnames()
        assert names[0] == "spec.json" and sorted(n[5:] for n in names[1:]) == sorted(F.FILES)
        assert tf.extractfile("tree/pkg/a.py").read().decode() == F.PY_A  # the committed bytes
        assert tf.getmember("tree/app.py").mode == 0o755 and tf.getmember("tree/pkg/a.py").mode == 0o644
        assert all(m.uid == 0 and m.mtime == 0 for m in tf.getmembers())  # nothing of this host's users or clocks
        spec = json.load(tf.extractfile("spec.json"))
    assert (
        spec["commit"] == commit and spec["files"]["pkg/a.py"] == vcs.blob_sha(F.PY_A.encode()) and spec["runs"] == RUNS
    )
    assert set(spec) == set(rtbox.SPEC_KEYS)  # exactly the keys the box accepts
    assert pkg.langs == {"go": 1, "javascript": 1, "python": 4}
    assert b"hunter2" not in pkg.data and b"untracked" not in pkg.data


def test_the_copy_stops_for_a_secret_name_or_content_and_says_which_without_the_text(tmp_path):
    named = make_repo(tmp_path / "named", {"a.py": "x = 1\n", "config/id_rsa": "not a key\n"})
    with pytest.raises(runtime.PackError, match="look like secrets") as e:
        runtime.pack(str(named), vcs.head_commit(str(named)), RUNS, 60, "t", F.RUN_ID)
    assert "id_rsa" in str(e.value)
    leak = make_repo(
        tmp_path / "leak",
        {
            "a.py": "x = 1\n",
            "conf.txt": f"{PEM}\nMIIEowIBAAKCAQEAsecretsecret\n-----END RSA PRIVATE KEY-----\n",
        },
    )
    with pytest.raises(runtime.PackError, match="secret checks stop the copy") as e:
        runtime.pack(str(leak), vcs.head_commit(str(leak)), RUNS, 60, "t", F.RUN_ID)
    assert "MIIEow" not in str(e.value) and "conf.txt:private-key" in str(e.value)  # names and rules only


def test_the_secret_names_and_content_rules():
    hit = rttransport.secret_name_hits
    assert hit([".env", "a/.env.local", "k.pem", "id_ed25519", "x/.ssh/config", ".npmrc", "prod-kubeconfig.yaml", "secrets/credentials.json"]) == [
        ".env", "a/.env.local", "k.pem", "id_ed25519", "x/.ssh/config", ".npmrc", "prod-kubeconfig.yaml", "secrets/credentials.json"
    ]  # fmt: skip
    assert (
        hit([".env.example", "config/.env.sample", "a.py", "docs/kubeconfig.sh", "keyboard.py"]) == []
    )  # a template, code, no hit by name
    assert rttransport.scan_content(b"x = 1\n") == set()
    assert "private-key" in rttransport.scan_content((PEM_SSH + "\n").encode())
    assert "aws-access-key" in rttransport.scan_content(("AKIA" + "FAKEFAKEFAKEFAKE").encode())
    assert "generic-secret" in rttransport.scan_content(b'password = "' + b"a1" * 16 + b'"\n')
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:  # a secret inside an archive is found too
        raw = (PEM_PLAIN + "\n").encode()
        ti = tarfile.TarInfo("inner.txt")
        ti.size = len(raw)
        tf.addfile(ti, io.BytesIO(raw))
    assert "private-key" in rttransport.scan_content(buf.getvalue())


def test_the_copy_leaves_out_big_files_links_that_leave_the_tree_and_submodules(tmp_path, monkeypatch):
    d = json.loads(json.dumps(rtcfg.data()))
    d["copy"]["max_file_mb"] = 1
    monkeypatch.setattr(rtcfg, "_CACHE", {"d": d})
    r = make_repo(tmp_path / "r", {"a.py": "x = 1\n", "big.dat": "x" * (2 << 20)}, exe=())
    (r / "good").symlink_to("a.py")
    (r / "bad").symlink_to("../../outside")
    commit_all(r, "links")
    pkg = runtime.pack(str(r), vcs.head_commit(str(r)), RUNS, 60, "t", F.RUN_ID)
    with tarfile.open(
        fileobj=io.BytesIO(tarfile.open(fileobj=io.BytesIO(pkg.data)).extractfile("in.tar").read())
    ) as tf:
        names = {m.name: m for m in tf.getmembers()}
    assert (
        "tree/big.dat" not in names
        and "tree/bad" not in names
        and names["tree/good"].issym()
        and names["tree/good"].linkname == "a.py"
    )
    assert any("over 1 MB" in n for n in pkg.notes) and any("leave the tree" in n for n in pkg.notes)


def test_a_link_is_judged_through_the_other_links():
    assert runtime.confine_links({"a/l": "q/../../x", "a/q": "../d", "d": "x"}) == {
        "a/q": "../d",
        "d": "x",
    }  # the walk leaves the tree
    assert runtime.confine_links({"l": "/etc/passwd", "m": "", "n": "n"}) == {}
    assert runtime.confine_links({"a": "b", "b": "a"}) == {}  # a loop


def test_edits_replace_tracked_files_in_the_copy_and_pass_the_same_checks(repo):
    commit = vcs.head_commit(str(repo))
    edit = "def a2():\n    return 2\n"
    pkg = runtime.pack(str(repo), commit, RUNS, 60, "t", F.RUN_ID, edits={"pkg/a.py": edit})
    inner = tarfile.open(fileobj=io.BytesIO(pkg.data)).extractfile("in.tar").read()
    with tarfile.open(fileobj=io.BytesIO(inner)) as tf:
        assert tf.extractfile("tree/pkg/a.py").read().decode() == edit
        assert tf.extractfile("tree/app.py").read().decode() == F.PY_MAIN
    assert pkg.files["pkg/a.py"] == vcs.blob_sha(edit.encode())
    with pytest.raises(runtime.PackError, match="not in the project copy"):
        runtime.pack(str(repo), commit, RUNS, 60, "t", F.RUN_ID, edits={"nope.py": "x\n"})
    with pytest.raises(runtime.PackError, match="secret checks"):
        runtime.pack(str(repo), commit, RUNS, 60, "t", F.RUN_ID, edits={"pkg/a.py": PEM_PLAIN + "\n"})


def test_a_repository_without_a_commit_or_files_is_a_reason(tmp_path):
    r = tmp_path / "empty"
    r.mkdir()
    git(r, "init", "-q")
    with pytest.raises(runtime.PackError):
        runtime.pack(str(r), "a" * 40, [], 60, "t", F.RUN_ID)


def test_the_plan_of_the_runs_follows_the_languages_of_the_tree_and_the_command():
    present = {"python": 4, "go": 1, "javascript": 1, "java": 1, "rust": 3, "typescript": 2}
    runs, notes = runtime.plan_runs(rtcfg.RuntimeCfg(True), present)
    assert [(r["langs"], r["argv"][0]) for r in runs] == [(["python"], "python3"), (["go"], "go")]
    assert notes == [
        "no command for javascript: set command in [graph.runtime]",
        "no tracer for java, rust, typescript",
    ]
    runs, notes = runtime.plan_runs(rtcfg.RuntimeCfg(True, command=("make", "test")), present)
    assert runs == [{"label": "configured command", "langs": ["python", "javascript", "go"], "argv": ["make", "test"]}]
    assert notes == ["no tracer for java, rust, typescript"]
    assert runtime.plan_runs(rtcfg.RuntimeCfg(True), {"rust": 1}) == ([], ["no tracer for rust"])
    assert runtime.plan_runs(rtcfg.RuntimeCfg(True), {}) == ([], [])


def test_languages_of_the_tree_come_from_the_names_only():
    got = runtime.detect_langs(["a.py", "b.go", "c/d.js", "x.md", "y.ts", "Makefile", "z.json"])
    assert got["go"] == 1 and got["javascript"] == 1 and got["python"] == 1 and "markdown" not in got


# ================================================================ the remote command lines and ssh
def test_the_remote_commands_have_one_fixed_form_each():
    tool = {n: "ab" * 32 for n in rttransport.TOOL_NAMES}
    sha = "cd" * 32
    put, pull, clean = (
        rttransport.put_command(F.RUN_ID),
        rttransport.pull_command(F.RUN_ID),
        rttransport.cleanup_command(F.RUN_ID),
    )
    run = rttransport.run_command(F.RUN_ID, sha, tool)
    for c in (put, pull, clean, run):
        assert c.startswith("sh -c ")  # one shell script whatever the login shell is
        assert (
            F.RUN_ID in c and "is not a private directory of this user" in c
        )  # the guard of the private base directory
    assert 'mkdir -m 700 "$D" && cd "$D" && mkdir -m 700 out tmp data && umask 077 && cat > put.tar' in put
    assert (
        "tar -xf put.tar -- in.tar box/rtbox.py box/runtime.json box/tracers/py/sitecustomize.py && rm -f put.tar"
        in put
    )
    assert pull.endswith('&& cd "$D/out" && tar -cf - -- manifest.json runtime.json\'') and "mkdir" not in pull + clean
    assert clean.endswith('&& rm -rf -- "$D"\'')
    assert 'python3 -I rtbox.py --on-target --box "$D" --sha256 ' + sha in run and run.endswith(
        sha + "'"
    )  # the box command is the last thing
    assert "exec python3 -I -c" in run and " env -i " in run and 'test -f "$D/in.tar"' in run and 'cd "$D/box"' in run
    assert 'HOME="$D/data"' in run and 'TMPDIR="$D/tmp"' in run and "PATH=/usr/local/bin:/usr/bin:/bin" in run
    assert run.index("hashlib") < run.index("exec python3")  # the tool's own files are checked before the box starts
    assert tool["rtbox.py"] in run and run.count("ab" * 32) == 3
    for word in ("SSH_AUTH_SOCK", "TOKEN", "KEY", os.environ.get("HOME", "~nohome"), "scp", "rsync", "sftp"):
        assert word not in run + put + pull + clean


def test_the_remote_commands_refuse_what_is_not_a_hex_id():
    tool = {n: "ab" * 32 for n in rttransport.TOOL_NAMES}
    for bad in ("../x", "abc", F.RUN_ID + "0", "a;b" + "0" * 29, F.RUN_ID.upper(), ""):
        for fn in (rttransport.put_command, rttransport.pull_command, rttransport.cleanup_command):
            with pytest.raises(ValueError):
                fn(bad)
        with pytest.raises(ValueError):
            rttransport.run_command(bad, "0" * 64, tool)
    for bad in ("", "abc", "0" * 63, "0" * 64 + ";rm", "g" * 64):
        with pytest.raises(ValueError):
            rttransport.run_command(F.RUN_ID, bad, tool)
    for bad in ({}, {**tool, "extra": "0" * 64}, {**tool, "rtbox.py": "x;y"}, {"rtbox.py": "0" * 64}):
        with pytest.raises(ValueError):
            rttransport.run_command(F.RUN_ID, "0" * 64, bad)


def test_the_ssh_command_line_has_the_options_the_trust_model_needs():
    argv = rttransport.ssh_argv(
        rttransport.Target("h.invalid", "tester", 2222), "REMOTE", keep_stdin=False, connect_timeout=9
    )
    opts = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
    assert argv[:2] == ["ssh", "-n"] and argv[-3:] == [
        "--",
        "h.invalid",
        "REMOTE",
    ]  # `-- HOST command`: the host can never be an option
    for want in (
        "BatchMode=yes", "StrictHostKeyChecking=yes", "ClearAllForwardings=yes", "ForwardAgent=no", "ForwardX11=no",
        "PermitLocalCommand=no", "ControlMaster=no", "ControlPath=none", "RemoteCommand=none", "ConnectTimeout=9",
    ):  # fmt: skip
        assert want in opts
    assert not any(o.startswith(("StrictHostKeyChecking=no", "StrictHostKeyChecking=accept")) for o in opts)
    assert argv[argv.index("-p") + 1] == "2222" and argv[argv.index("-l") + 1] == "tester"
    plain = rttransport.ssh_argv(rttransport.Target("h.invalid"), "R", keep_stdin=True)
    assert "-n" not in plain and "-p" not in plain and "-l" not in plain and plain[-3:] == ["--", "h.invalid", "R"]
    assert "-F" not in argv  # the user's own ssh config stays in charge of aliases and keys


class FakeProc:
    def __init__(self, rc=0, out=b"", err=b"", wait_raises=False):
        self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(out), io.BytesIO(err)
        self.rc, self.pid, self.wait_raises, self.killed = rc, 4242, wait_raises, False

    def wait(self, timeout=None):
        if self.wait_raises and timeout is not None:
            self.wait_raises = False
            raise subprocess.TimeoutExpired("ssh", timeout)
        return self.rc

    def poll(self):
        return self.rc

    def kill(self):
        self.killed = True


def ssh_transport(**kw):
    d = rtcfg.data()
    return rttransport.SshTransport(
        rttransport.Target("h.invalid", "tester"), d["timeouts"], d["result"]["max_bytes"], **kw
    )


def test_the_ssh_transport_sends_only_the_fixed_commands():
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return SimpleNamespace(returncode=0, stderr=b"", stdout=b"dead-code-checker-ok\n")

    popens = [FakeProc(rc=0, out=b"line 1\nlast line\n"), FakeProc(rc=0, out=b"TARDATA")]

    def fake_popen(cmd, **kw):
        calls.append((cmd, kw))
        return popens.pop(0)

    t = ssh_transport(run=fake_run, popen=fake_popen)
    tool = {n: "0" * 64 for n in rttransport.TOOL_NAMES}
    assert t.label == "tester@h.invalid" and t.reachable() == (True, "ok")
    assert t.put(F.RUN_ID, b"TAR") == (0, "no output")
    cmd, kw = calls[1]
    assert (
        cmd[0] == "ssh" and "-n" not in cmd and cmd[-1] == rttransport.put_command(F.RUN_ID) and kw["input"] == b"TAR"
    )
    assert t.run(F.RUN_ID, "0" * 64, 10, tool) == (0, "last line")
    cmd, kw = calls[2]
    assert (
        "-n" not in cmd and kw["stdin"] == subprocess.PIPE and kw["start_new_session"] is True
    )  # stdin stays open: its EOF is the signal
    assert cmd[-1] == rttransport.run_command(F.RUN_ID, "0" * 64, tool)
    assert t.pull(F.RUN_ID) == (b"TARDATA", "")
    cmd, kw = calls[3]
    assert cmd[1] == "-n" and cmd[-1] == rttransport.pull_command(F.RUN_ID) and kw["stdin"] == subprocess.DEVNULL
    t.cleanup(F.RUN_ID)
    assert calls[4][0][-1] == rttransport.cleanup_command(F.RUN_ID)
    assert all(c[0][0] == "ssh" for c in calls)  # every process the transport starts is ssh


LEAKS = ("password=fakehunter2xx", "bearer fakebearertoken123456", "https://me:fakepass9999@example.org/x")
LEAKY = "ssh: " + " ".join(LEAKS)  # what a target (or ssh, or a project's own output on the channel) might print


SECRET_VALUES = ("fakehunter2xx", "fakebearertoken123456", "fakepass9999")


def clean(text):
    return not any(v in text for v in SECRET_VALUES)


def test_a_line_of_the_target_is_masked_in_every_transport_answer():
    """The last line of what ssh or the target said travels into messages and reports: masked before it is cut, at the source."""
    tool = {n: "0" * 64 for n in rttransport.TOOL_NAMES}
    leaky = SimpleNamespace(returncode=3, stderr=(LEAKY + "\n").encode(), stdout=b"")
    t = ssh_transport(run=lambda *a, **k: leaky, popen=lambda *a, **k: FakeProc(rc=1, out=(LEAKY + "\n").encode(), err=LEAKY.encode()))
    for rc, line in (t.put(F.RUN_ID, b"x"), t.run(F.RUN_ID, "0" * 64, 5, tool), (0, t.pull(F.RUN_ID)[1]), (0, t.reachable()[1])):
        assert clean(line), line
    assert "***" in t.put(F.RUN_ID, b"x")[1] and "***" in t.run(F.RUN_ID, "0" * 64, 5, tool)[1]
    refused = SimpleNamespace(returncode=255, stderr=(LEAKY + "\n").encode(), stdout=b"")
    assert clean(ssh_transport(run=lambda *a, **k: refused).put(F.RUN_ID, b"x")[1])
    boom = ssh_transport(run=lambda *a, **k: (_ for _ in ()).throw(OSError(LEAKY)))
    assert clean(boom.put(F.RUN_ID, b"x")[1]) and clean(boom.reachable()[1])
    assert rttransport.explain(rttransport.Target("h.invalid"), LEAKY).count("***") >= 1


@pytest.mark.parametrize("fail", ["put", "put255", "run", "run255", "pull"])
def test_a_secret_in_a_line_of_the_target_never_reaches_the_outcome(env, tmp_path, repo, fail):
    """Even a transport that does not mask (a test double, a future one) is masked where the lines become the one line of a step."""
    make, t = fake(tmp_path)
    if fail.startswith("put"):
        t.put = lambda rid, data: (255 if fail == "put255" else 1, LEAKY)
    elif fail.startswith("run"):
        t.run = lambda rid, sha, wait, tool=None: (255 if fail == "run255" else 1, LEAKY)
    else:
        t.pull = lambda rid: (None, LEAKY)
    out = runtime.run(str(repo), ENABLED, environ=env, make=make, now=RUN_AT, run_id=F.RUN_ID)
    assert out.status in ("failed", "skipped") and "***" in out.line and clean(out.line), out.line
    assert clean(" ".join(out.notes))


def test_the_ssh_transport_reports_failures_as_pairs(monkeypatch):
    def failing(cmd, **kw):
        return SimpleNamespace(returncode=3, stderr=b"first\nmkdir: File exists\n", stdout=b"")

    t = ssh_transport(run=failing, popen=lambda *a, **k: (_ for _ in ()).throw(OSError("no ssh")))
    tool = {n: "0" * 64 for n in rttransport.TOOL_NAMES}
    assert t.put(F.RUN_ID, b"x") == (3, "mkdir: File exists")
    assert t.run(F.RUN_ID, "0" * 64, 1, tool) == (255, "ssh: no ssh") and t.pull(F.RUN_ID) == (None, "ssh: no ssh")
    assert ssh_transport(run=lambda *a, **k: (_ for _ in ()).throw(OSError("gone"))).put(F.RUN_ID, b"") == (
        255,
        "ssh: gone",
    )
    killed = []
    monkeypatch.setattr(rttransport.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    slow = FakeProc(wait_raises=True)
    t = ssh_transport(popen=lambda *a, **k: slow)
    assert (
        t.run(F.RUN_ID, "0" * 64, 5, tool) == (124, "no answer within 5 s") and killed and slow.stdin.closed
    )  # the channel is closed
    big = FakeProc(out=b"x" * (rtcfg.data()["result"]["max_bytes"] * 2 + 5))
    assert ssh_transport(popen=lambda *a, **k: big).pull(F.RUN_ID)[0] is None
    bad = FakeProc(rc=1, err=b"tar: manifest.json: Cannot stat\n")
    assert ssh_transport(popen=lambda *a, **k: bad).pull(F.RUN_ID) == (
        None,
        "pull failed (rc 1): tar: manifest.json: Cannot stat",
    )


def test_a_host_key_that_is_not_known_is_one_line_that_says_so_and_nothing_is_accepted():
    def refused(cmd, **kw):
        return SimpleNamespace(returncode=255, stderr=b"Host key verification failed.\n", stdout=b"")

    t = ssh_transport(run=refused, popen=lambda *a, **k: FakeProc(rc=255, out=b"Host key verification failed.\n"))
    ok, why = t.reachable()
    assert not ok and "host key of h.invalid is not in your known_hosts" in why and "never accepts a new key" in why
    rc, why = t.put(F.RUN_ID, b"")
    assert rc == 255 and "known_hosts" in why
    tool = {n: "0" * 64 for n in rttransport.TOOL_NAMES}
    assert t.run(F.RUN_ID, "0" * 64, 1, tool)[0] == 255 and "known_hosts" in t.run(F.RUN_ID, "0" * 64, 1, tool)[1]

    def denied(cmd, **kw):
        return SimpleNamespace(returncode=255, stderr=b"tester@h.invalid: Permission denied (publickey).\n", stdout=b"")

    ok, why = ssh_transport(run=denied).reachable()
    assert not ok and "key login only" in why
    out = runtime.run("/nonexistent", ENABLED, environ=ALLOW, make=lambda target, e: ssh_transport(run=refused))
    assert out.status == "skipped" and "known_hosts" in out.line and out.line.count("\n") == 0


def test_the_four_remote_command_forms_do_what_they_say_on_a_shell(tmp_path):
    """The put, pull and cleanup scripts run through a real POSIX shell on a directory of this test (the shell stands for the login shell of the
    target; the run command is not started: it needs the box)."""
    rid = uuid.uuid4().hex
    pkg_inner = b"in-tar-bytes"
    wire = io.BytesIO()
    with tarfile.open(fileobj=wire, mode="w:") as tf:
        for name, data in (
            ("in.tar", pkg_inner),
            *((f"box/{n}", b"x" + n.encode()) for n in rttransport.TOOL_NAMES),
            ("evil", b"no"),
        ):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    sh = lambda cmd, data=None: subprocess.run(shlex.split(cmd), input=data, capture_output=True, timeout=60)  # noqa: E731
    base = Path(f"/tmp/.dcc-rt-{os.getuid()}")
    d = base / f"rt-{rid}"
    try:
        p = sh(rttransport.put_command(rid), wire.getvalue())
        assert p.returncode == 0, p.stderr
        assert (os.lstat(base).st_mode & 0o777, os.lstat(d).st_mode & 0o777) == (0o700, 0o700)
        got = sorted(str(x.relative_to(d)) for x in d.rglob("*") if x.is_file())
        assert got == sorted(
            ["in.tar", *("box/" + n for n in rttransport.TOOL_NAMES)]
        )  # put.tar is gone, `evil` was never taken out
        assert (d / "in.tar").read_bytes() == pkg_inner and (d / "in.tar").stat().st_mode & 0o077 == 0
        assert (
            sh(rttransport.put_command(rid), wire.getvalue()).returncode != 0
        )  # a run directory that exists is never reused
        assert sh(rttransport.pull_command(rid)).returncode != 0  # nothing to pull yet
        (d / "out" / "manifest.json").write_text("{}")
        (d / "out" / "runtime.json").write_text("{}")
        (d / "out" / "other.txt").write_text("not sent")
        pulled = sh(rttransport.pull_command(rid))
        assert pulled.returncode == 0
        with tarfile.open(fileobj=io.BytesIO(pulled.stdout)) as tf:
            assert sorted(tf.getnames()) == ["manifest.json", "runtime.json"]
        assert sh(rttransport.cleanup_command(rid)).returncode == 0 and not d.exists()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_the_tool_files_are_checked_on_the_target_before_the_box_starts(tmp_path):
    files = rtbox.delivery_files()
    want = {n: hashlib.sha256(Path(p).read_bytes()).hexdigest() for n, p in files.items()}
    cmd = [sys.executable, "-I", "-c", rttransport.VERIFY] + [
        x for n in rttransport.TOOL_NAMES for x in (want[n], files[n])
    ]
    assert subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
    bad = cmd[:-2] + ["0" * 64, files[rttransport.TOOL_NAMES[-1]]]
    r = subprocess.run(bad, capture_output=True, timeout=60)
    assert r.returncode != 0 and b"is not the file that was sent" in r.stderr


def test_the_wrapper_ends_the_process_group_when_the_channel_closes():
    held = subprocess.Popen(
        [sys.executable, "-I", "-c", rttransport.RUN_WRAPPER, "1", sys.executable, "-c", "import sys; sys.exit(7)"],
        stdin=subprocess.PIPE,
    )  # the channel stays open while the command runs
    assert held.wait(timeout=60) == 7  # the exit code of the command
    held.stdin.close()
    start = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", rttransport.RUN_WRAPPER, "1", sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
    )
    time.sleep(0.5)
    proc.stdin.close()  # the ssh channel is gone
    assert proc.wait(timeout=30) in (143, 137) and time.monotonic() - start < 30


# ================================================================ the round trip
def one_run(env, tmp_path, repo, **kw):
    make, t = fake(tmp_path, **{k: v for k, v in kw.items() if k in ("box", "tools", "put_rc", "run_rc", "tamper")})
    out = runtime.run(str(repo), kw.get("rt", ENABLED), environ=env, make=make, now=RUN_AT, run_id=F.RUN_ID)
    return out, t


def test_a_run_brings_back_the_symbols_that_ran_and_stores_them(env, tmp_path, repo):
    out, t = one_run(env, tmp_path, repo)
    assert out.status == "ok" and out.level == "I" and out.finding() is None
    doc = out.doc
    rows = {(r["file"], r["qual"]) for r in doc["executed"]}
    assert rows == {("pkg/a.py", "used"), ("pkg/a.py", "Klass.meth"), ("app.py", "main"), ("svc/a.go", "Used")}
    assert out.line.startswith("runtime mode: 4 symbol(s) in 3 file(s) ran on tester@target.invalid (python: ")
    assert "no command for javascript" in out.line  # a language that cannot run is named, not hidden
    stored = Path(out.dir)
    assert (
        stored.parent.name == "runtime"
        and stored.name.startswith("20231114T221320Z-")
        and (stored / "runtime.json").is_file()
    )
    assert (
        "symbols that ran: 4 in 3 file(s)" in (stored / "report.md").read_text()
        and oct(stored.stat().st_mode & 0o777) == "0o700"
    )
    assert json.loads((stored / "runtime.json").read_text())["stored"]["target"] == "tester@target.invalid"
    st = runtime.load_latest(str(repo), env)
    assert (
        st.name == stored.name
        and st.doc["executed"] == doc["executed"]
        and st.ts == runtime._stamp_ts("20231114T221320Z")
    )


def test_what_is_sent_is_what_the_target_checks(env, tmp_path, repo):
    out, t = one_run(env, tmp_path, repo)
    assert out.status == "ok"
    sent = t.sent[F.RUN_ID]
    with tarfile.open(fileobj=io.BytesIO(sent)) as outer:
        assert outer.getnames()[0] == "in.tar"
    (call,) = [c for c in t.box.calls if c["cmd"][0] == "python3"]
    assert (
        call["cwd"].endswith("/tree") and "PYTHONPATH" in call["env"]
    )  # the tracer is the tool's own file in the delivery


def test_the_command_the_box_gets_is_the_configured_one_in_one_run_for_every_traced_language(env, tmp_path, repo):
    out, t = one_run(env, tmp_path, repo, rt=rtcfg.RuntimeCfg(True, HOST, command=("go", "test", "./...")))
    assert out.status == "ok"
    first = t.box.calls[0]["cmd"]
    assert first[:2] == ["go", "test"] and first[2].startswith("-coverprofile=") and first[-1] == "./..."
    assert (
        "PYTHONPATH" in t.box.calls[0]["env"] and "NODE_V8_COVERAGE" in t.box.calls[0]["env"]
    )  # python and javascript by env


def test_a_run_that_failed_on_the_target_is_one_w_line_and_stores_nothing(env, tmp_path, repo):
    out, t = one_run(env, tmp_path, repo, box=F.FakeBox(rc=2, py={"v": 1, "fn": []}, go=""))
    assert out.status == "failed" and out.level == "W" and out.finding().code == "dc.runtime-failed"
    assert out.line.startswith(
        "runtime mode: the command failed on tester@target.invalid (rc 2, 2) and no symbol was recorded"
    )
    assert out.line.count("\n") == 0 and runtime.load_latest(str(repo), env) is None and t.log[-1] == "cleanup"


def test_a_failing_command_that_still_ran_something_is_a_result(env, tmp_path, repo):
    out, _ = one_run(env, tmp_path, repo, box=F.FakeBox(rc=1))
    assert out.status == "ok" and out.doc["runs"][0]["rc"] == 1 and len(out.doc["executed"]) >= 3


@pytest.mark.parametrize(
    "kw,status,text",
    [
        ({"put_rc": 255}, "skipped", "did not take the project copy"),
        ({"put_rc": 1}, "failed", "could not be put on tester@target.invalid (rc 1"),
        ({"run_rc": 255}, "skipped", "no transport to tester@target.invalid"),
        ({"run_rc": 2}, "failed", "ended with rc 2"),
        ({"run_rc": 124}, "failed", "ended with rc 124"),
    ],
)
def test_a_target_that_cannot_be_used_is_a_skip_or_a_failure_with_one_line_and_the_directory_is_cleaned(
    env, tmp_path, repo, kw, status, text
):
    out, t = one_run(env, tmp_path, repo, **kw)
    assert out.status == status and text in out.line and out.line.count("\n") == 0
    want = ("dc.runtime-skipped", "I") if status == "skipped" else ("dc.runtime-failed", "W")
    assert (out.finding().code, out.finding().level) == want
    assert t.log[-1] == "cleanup" and not t.dir(F.RUN_ID).exists()  # also when it failed
    if status == "skipped":
        assert out.line.startswith("runtime mode: no execution target (")


def test_a_tool_file_that_does_not_arrive_intact_stops_the_run(env, tmp_path, repo):
    make, t = fake(tmp_path)
    orig_put = t.put

    def put(run_id, data):  # the hashes are those of the real files; the bytes that arrive are not
        rc, why = orig_put(run_id, data)
        (t.dir(run_id) / "box" / "rtbox.py").write_bytes(b"other")
        return rc, why

    t.put = put
    out = runtime.run(str(repo), ENABLED, environ=env, make=make, run_id=F.RUN_ID)
    assert out.status == "failed" and "is not the file that was sent" in out.line and t.log[-1] == "cleanup"


def test_a_target_without_a_sandbox_is_the_skip_line_and_nothing_ran(env, tmp_path, repo):
    box = F.FakeBox()
    out, t = one_run(env, tmp_path, repo, tools=F.NoBwrap, box=box)
    assert out.status == "skipped" and box.calls == []
    assert out.line.startswith(
        "runtime mode: no execution target (tester@target.invalid has no sandbox: bwrap at /usr/bin/bwrap cannot create"
    )
    assert runtime.load_latest(str(repo), env) is None


def test_no_language_with_a_tracer_means_nothing_to_run_and_says_what_is_missing(env, tmp_path):
    r = make_repo(tmp_path / "rs", {"src/main.rs": "fn main() {}\n", "README.md": "x\n"}, exe=())
    out, _ = one_run(env, tmp_path, r)
    assert out.status == "skipped" and out.line == rtcfg.skip_line("no tracer for rust")


def test_a_repository_that_is_not_one_is_a_skip_line(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    make, t = fake(tmp_path)
    out = runtime.run(str(plain), ENABLED, environ=env, make=make)
    assert out.status == "skipped" and "no commit" in out.line and t.log == ["reachable"]


def test_a_secret_in_the_copy_stops_the_run_before_anything_is_sent(env, tmp_path):
    r = make_repo(tmp_path / "s", {"a.py": "x = 1\n", "id_rsa": "x\n"}, exe=())
    out, t = one_run(env, tmp_path, r)
    assert out.status == "failed" and "look like secrets" in out.line and t.log == ["reachable"]  # not even a put


def test_off_means_off_even_with_a_target_unless_a_host_is_given(env, tmp_path, repo):
    make, t = fake(tmp_path)
    out = runtime.run(str(repo), rtcfg.RuntimeCfg(False, HOST), environ=env, make=make)
    assert out.status == "off" and out.finding() is None and t.log == []
    out = runtime.run(str(repo), rtcfg.RuntimeCfg(False), host=HOST, environ=env, make=make, now=RUN_AT)
    assert out.status == "ok" and t.log[0] == "reachable"


# ================================================================ what comes back is checked
def retag(out, **over):
    """A tamper hook for FakeTransport.pull: rewrite runtime.json (and fix the manifest so only the content is wrong)."""
    p = out / "runtime.json"
    doc = json.loads(p.read_text())
    doc.update(over)
    p.write_text(json.dumps(doc))
    man = json.loads((out / "manifest.json").read_text())
    man.update(runtime_sha256=hashlib.sha256(p.read_bytes()).hexdigest(), runtime_bytes=p.stat().st_size)
    (out / "manifest.json").write_text(json.dumps(man))


def edit_rows(fn):
    def tamper(out):
        doc = json.loads((out / "runtime.json").read_text())
        fn(doc)
        retag(out, **doc)

    return tamper


def _row(**over):
    return {"lang": "python", "file": "app.py", "qual": "x", "line": 1, "end": 0, "hit": 1, **over}


@pytest.mark.parametrize(
    "tamper,why",
    [
        (
            lambda out: (out / "runtime.json").write_text((out / "runtime.json").read_text() + " "),
            "does not match the manifest",
        ),
        (lambda out: (out / "manifest.json").write_text('{"schema": 1}'), "manifest.json is not the one"),
        (lambda out: (out / "manifest.json").unlink(), None),
        (edit_rows(lambda d: d.update(run_id="f" * 32)), "another schema, run or commit"),
        (edit_rows(lambda d: d.update(commit="b" * 40)), "another schema, run or commit"),
        (edit_rows(lambda d: d.update(status="hacked")), "status or target"),
        (edit_rows(lambda d: d.update(extra=1)), "exactly the expected keys"),
        (edit_rows(lambda d: d["files"].update({"never/sent.py": "a" * 40})), "was not sent"),
        (edit_rows(lambda d: d["files"].update({"app.py": "a" * 40})), "was not sent"),
        (
            edit_rows(
                lambda d: d["executed"].append({"lang": "python", "file": "app.py", "qual": "x", "line": 1, "end": 0})
            ),
            "exact shape",
        ),
        (edit_rows(lambda d: d["executed"].append(_row(file="go.mod"))), "no blob id"),
        (edit_rows(lambda d: d["executed"].append(_row(lang="rust"))), "exact shape"),
        (edit_rows(lambda d: d["executed"].append(_row(line=0))), "not a valid number"),
        (edit_rows(lambda d: d["executed"].append(_row(line=True))), "not a valid number"),
        (edit_rows(lambda d: d["executed"].append(_row(hit=0))), "not a valid number"),
        (edit_rows(lambda d: d.update(runs="no")), "runs are not"),
        (edit_rows(lambda d: d.update(runs=[{"surprise": 1}])), "runs are not"),
        (edit_rows(lambda d: d.update(notes="no")), "notes"),
    ],
)
def test_a_result_that_fails_a_check_is_dropped_whole(env, tmp_path, repo, tamper, why):
    out, t = one_run(env, tmp_path, repo, tamper=tamper)
    assert out.status == "failed" and out.level == "W" and out.line.count("\n") == 0
    assert "was rejected" in out.line or "could not be pulled" in out.line
    if why:
        assert why in out.line
    assert runtime.load_latest(str(repo), env) is None and t.log[-1] == "cleanup"  # nothing of it was stored or used


def test_the_words_of_the_target_are_cut_to_plain_text(env, tmp_path, repo):
    def tamper(out):
        doc = json.loads((out / "runtime.json").read_text())
        doc["notes"] = ["line one\nline two\x1b[31m " + "x" * 900]
        doc["runs"][0]["label"] = "L" * 500
        doc["runs"][0]["tracers"]["python"]["note"] = "N" * 900
        retag(out, **doc)

    out, _ = one_run(env, tmp_path, repo, tamper=tamper)
    assert out.status == "ok"
    d, cap = out.doc, rtcfg.data()["result"]["note_chars"]
    assert "\n" not in d["notes"][0] and len(d["notes"][0]) <= cap
    assert len(d["runs"][0]["label"]) <= 120 and len(d["runs"][0]["tracers"]["python"]["note"]) <= cap


def test_a_delivery_that_is_not_exactly_the_two_files_is_refused():
    sent = {"a.py": "a" * 40}

    def tar_of(names, size=10):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:") as tf:
            for n in names:
                ti = tarfile.TarInfo(n)
                ti.size = size
                tf.addfile(ti, io.BytesIO(b"x" * size))
        return buf.getvalue()

    for names in (["manifest.json"], ["manifest.json", "runtime.json", "extra"], ["manifest.json", "../runtime.json"]):
        assert runtime.parse_delivery(tar_of(names), sent, F.RUN_ID, "a" * 40) == (
            None,
            "the delivery is not exactly manifest.json and runtime.json as plain files within the size cap",
        )
    assert runtime.parse_delivery(b"not a tar", sent, F.RUN_ID, "a" * 40)[1] == "the delivery cannot be read"
    assert (
        runtime.parse_delivery(tar_of(["manifest.json", "runtime.json"]), sent, F.RUN_ID, "a" * 40)[1]
        == "the delivery cannot be read"
    )


# ================================================================ stored runs
def test_stored_runs_are_kept_newest_first_and_the_oldest_go(env, repo):
    doc = stored_doc(repo, [row("app.py", 5, "main")])
    keep = rtcfg.data()["keep_runs"]
    dirs = [
        runtime.store_run(str(repo), doc, "t@h", "abcdef0123456789", RUN_AT + 100 * i, env) for i in range(keep + 2)
    ]
    assert all(dirs)
    names = [n for n, _p, _t in runtime.list_runs(str(repo), env)]
    assert (
        len(names) == keep
        and names == sorted(names)
        and os.path.basename(dirs[-1]) == names[-1]
        and not os.path.exists(dirs[0])
    )
    assert runtime.load_latest(str(repo), env).name == names[-1]
    base = Path(runtime.runs_dir(str(repo), env))
    (base / "notes.txt").write_text("x")  # not a run: neither listed nor removed
    (base / "20250101T000000Z-linked").symlink_to(base)
    assert [n for n, _p, _t in runtime.list_runs(str(repo), env)] == names and (base / "notes.txt").exists()
    again = runtime.store_run(str(repo), doc, "t", "abcdef01", RUN_AT + 100 * (keep + 1), env)
    assert again and again.endswith("-1")  # the same second: a -1 suffix, never an overwrite


def test_a_stored_run_that_was_changed_on_disk_is_not_used(env, repo):
    doc = stored_doc(repo, [row("app.py", 5, "main")])
    d = runtime.store_run(str(repo), doc, "t@h", "abcdef01", RUN_AT, env)
    p = Path(d) / "runtime.json"
    body = json.loads(p.read_text())
    body["executed"][0]["line"] = "five"
    p.write_text(json.dumps(body))
    assert runtime.load_latest(str(repo), env) is None
    older = runtime.store_run(str(repo), doc, "t@h", "abcdef01", RUN_AT - 100_000_000, env)
    assert runtime.load_latest(str(repo), env).dir == older  # the newest one that reads back
    (Path(older) / "runtime.json").write_text("{")
    assert runtime.load_latest(str(repo), env) is None


def test_no_data_directory_means_no_runs_and_no_store(repo, tmp_path):
    bad = {"DEAD_CODE_CHECKER_HOME": str(tmp_path / "missing" / "x"), "PATH": os.environ["PATH"]}
    os.makedirs(tmp_path / "missing")
    (tmp_path / "missing" / "x").write_text("a file, not a directory")
    assert runtime.list_runs(str(repo), bad) == [] and runtime.load_latest(str(repo), bad) is None
    assert runtime.store_run(str(repo), stored_doc(repo, []), "t", "abcdef01", RUN_AT, bad) == ""


def test_a_stored_runtime_run_is_no_run_directory_of_the_other_levels(env, repo, data_root):
    runtime.store_run(str(repo), stored_doc(repo, [row("app.py", 5, "main")]), "t@h", "abcdef01", RUN_AT, env)
    project = os.path.dirname(runtime.runs_dir(str(repo), env))
    assert artifacts.list_runs(project) == []  # `runtime` is no run name: pages, prune and `latest` never see it
    pruned = artifacts.prune(data_root, keep_last=0, keep_days=0, dry_run=True)
    assert pruned.removed == () and os.path.isdir(runtime.runs_dir(str(repo), env))
