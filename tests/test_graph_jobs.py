"""graph.jobs: what the plan makes of a tree (the jobs of data/audit.json), and how they run on stub engines: in parallel, with the
registry's network-free environment, the offline Go cache, the sandbox mode, a timeout, a budget, the cache of the semantic layer.

python3 -m pytest -q tests/test_graph_jobs.py

Every engine is a stub script (tests/graph_fake_engines.py) in a directory of the test; the PATH of the host is never searched and
no test touches the network. The stubs print what the real tools printed (the recordings of tests/fixtures/engines).
"""

import copy
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from graph_fake_engines import SH, block_network, isolate, make_engine, make_script
from graph_support import git, new_repo

from dead_code_checker.graph import adapters, engines as dce, jobs, langs, sandbox, semcache, vcs
from dead_code_checker.graph import store as dcstore

FIX = Path(__file__).resolve().parent / "fixtures"
VULTURE_OUT = (FIX / "engines" / "vulture" / "2.14" / "out.txt").read_text()


@pytest.fixture(autouse=True)
def _guards(tmp_path, monkeypatch):
    block_network(monkeypatch)
    monkeypatch.delenv("DEAD_CODE_CHECKER_GO_CACHE", raising=False)
    monkeypatch.setenv("DEAD_CODE_CHECKER_HOME", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    yield from isolate(monkeypatch, tmp_path, tmp_path / "bin")


@pytest.fixture
def bindir(tmp_path):
    return tmp_path / "bin"


def tree_of(repo):
    """A stand-in for levels.Built: what jobs.gather reads (the tree and the language of each path)."""
    tree = vcs.snapshot(str(repo))
    lg = langs.load()
    return SimpleNamespace(tree=tree, langs={p: la for p in tree.files if (la := lg.detect(p))})


@pytest.fixture
def repo(tmp_path):
    return new_repo(
        tmp_path,
        {
            "v.py": "def dead_fn():\n    return 2\n",
            "svc/go.mod": "module x\n",
            "svc/main.go": "package main\n",
            "run.sh": "echo $x\n",
        },
    )


def settings(tmp_path, **kw):
    return jobs.Settings(**{"base": str(tmp_path / "data"), "workers": 2, **kw})


def gather(repo, tmp_path, store=None, roles=("audit",), langs_=None, cfg=None, run=jobs.default_run, **kw):
    return jobs.gather(
        str(repo),
        tree_of(repo),
        settings(tmp_path, roles=roles, langs=langs_, **kw),
        cfg,
        store,
        run,
        mode=kw.pop("mode", None),
    )


def codes(eng):
    return sorted((f.code, f.msg.split(":")[0]) for f in eng.findings)


# ================================================================ the data
def test_the_job_table_is_valid_against_the_registry_and_the_adapters():
    reg, tab = dce.load_registry(), jobs.table()
    assert jobs.validate(tab, reg, adapters.table()) == []
    assert "sootup" not in tab["engines"] and {"vulture", "ruff", "go-deadcode", "staticcheck", "knip", "cargo"} <= set(
        tab["engines"]
    )
    assert {"scip-python", "scip-go", "scip-typescript", "rust-analyzer"} <= set(tab["indexers"])
    for name, e in {**tab["engines"], **tab["indexers"]}.items():
        assert e["argv"][0] == "{bin}", name


def test_validate_names_every_problem_of_a_job_table():
    reg, ad = dce.load_registry(), adapters.table()
    bad = copy.deepcopy(jobs.table())
    bad["schema"] = 3
    bad["engines"]["ghost"] = {"scope": "repo", "argv": ["{bin}"], "stream": "stdout", "fmt": "json"}
    bad["engines"]["vulture"]["scope"] = "galaxy"
    bad["engines"]["knip"]["marker"] = ""
    bad["engines"]["shellcheck"]["dashdash"] = False
    bad["engines"]["tsc"]["argv"] = ["{bin}", "install"]
    bad["engines"]["ruff"]["fmt"] = "yaml"
    bad["engines"]["cppcheck"]["stream"] = "tty"
    bad["engines"]["go-deadcode"]["cache_opt"] = ["perl"]
    bad["indexers"]["scip-python"]["verified"] = "yes"
    bad["indexers"]["scip-go"]["cache"] = ["perl"]
    del bad["indexers"]["scip-dotnet"]["scope"]
    got = "; ".join(jobs.validate(bad, reg, ad))
    for part in ("schema must be 1", "engines.ghost: not in the engines registry", "vulture: scope must be one of",
                 "knip: scope marker needs a marker", "shellcheck: scope files needs dashdash", "tsc: argv must start with {bin} and must not install",
                 "engines.ruff: no adapter format 'yaml'", "cppcheck: stream", "go-deadcode: cache_opt perl has no variable",
                 "scip-python: verified must say", "scip-go: cache perl has no variable", "scip-dotnet: scope must be one of"):  # fmt: skip
        assert part in got, part


# ================================================================ expand
def unit(engine, **kw):
    return dce.Unit(engine, ("x",), False, f"/bin/{engine}", kw.pop("weight", 1), kw.pop("needs_build", False))


def test_a_repo_job_is_one_job_with_the_argv_of_the_data():
    spec = jobs.table()["engines"]["vulture"]
    (j,) = jobs.expand(unit("vulture"), spec, ["a.py"], {"a.py": "python"}, "proj", False)
    assert (
        j.label == "vulture"
        and j.argv == ("/bin/vulture", "--min-confidence", "80", ".")
        and j.rel == ""
        and j.sandbox is False
    )
    assert (j.stream, j.fmt, j.index, j.covered) == ("stdout", "text", False, ())


def test_a_marker_job_runs_in_each_outermost_directory_and_knows_what_it_analyses():
    spec = jobs.table()["engines"]["go-deadcode"]
    paths = ["go.mod", "a.go", "svc/go.mod", "svc/b.go", "tools/go.mod", "tools/c.go", "README.md"]
    langs_of = {p: "go" for p in paths if p.endswith(".go")}
    got = jobs.expand(unit("go-deadcode"), spec, paths, langs_of, "proj", False)
    assert [(j.label, j.rel) for j in got] == [("go-deadcode@.", "")]  # the nested modules are left to the root job...
    assert got[0].covered == (
        "a.go",
    )  # ...but are not part of its ./...: a file it never saw is not one it was silent on
    assert got[0].cache_opt == ("go",)
    nested = jobs.expand(unit("go-deadcode"), spec, paths[2:], langs_of, "proj", False)
    assert [(j.label, j.rel, j.covered) for j in nested] == [
        ("go-deadcode@svc", "svc", ("svc/b.go",)),
        ("go-deadcode@tools", "tools", ("tools/c.go",)),
    ]


def test_a_nest_marker_gets_a_job_in_every_directory():
    spec = jobs.table()["engines"]["tflint"]
    got = jobs.expand(unit("tflint"), spec, ["main.tf", "mod/x.tf", "mod/y.tf"], {}, "p", False)
    assert [j.rel for j in got] == ["", "mod"]


def test_a_files_job_chunks_the_files_and_puts_them_after_dashdash():
    spec = jobs.table()["engines"]["shellcheck"]
    paths = [f"s{i:03d}.sh" for i in range(450)] + ["-rf.sh", "a.py"]
    langs_of = {p: "shell" for p in paths if p.endswith(".sh")}
    got = jobs.expand(unit("shellcheck"), spec, paths, langs_of, "p", False)
    assert [j.label for j in got] == ["shellcheck#0", "shellcheck#1", "shellcheck#2"]
    assert got[0].argv[:4] == ("/bin/shellcheck", "-f", "json", "--") and len(got[0].argv) == 4 + 200
    assert "-rf.sh" in got[0].argv  # a tracked file named like an option is a file: it follows `--`
    assert sum(len(j.argv) - 4 for j in got) == 451
    assert jobs.expand(unit("shellcheck"), spec, ["a.py"], {"a.py": "python"}, "p", False) == []


def test_an_indexer_and_a_build_engine_run_in_the_sandbox_and_the_name_is_filled_in():
    tab = jobs.table()
    (idx,) = jobs.expand(
        unit("scip-python", weight=4, needs_build=True), tab["indexers"]["scip-python"], ["a.py"], {}, "myproj", True
    )
    assert (
        idx.index is True
        and idx.sandbox is True
        and "myproj" in idx.argv
        and idx.argv[-1] == "{out}/index.scip"
        and idx.weight == 4
    )
    (cargo,) = jobs.expand(unit("cargo", needs_build=True), tab["engines"]["cargo"], ["Cargo.toml"], {}, "p", False)
    assert cargo.sandbox is True and cargo.argv[-1] == "{out}/target"
    (plain,) = jobs.expand(unit("ruff"), tab["engines"]["ruff"], ["a.py"], {}, "p", False)
    assert plain.sandbox is False


# ================================================================ the offline caches
def test_the_offline_caches_say_which_can_be_used_and_why_not_the_others(tmp_path):
    use, why = jobs.offline(settings(tmp_path))
    assert use == {} and set(why) == {"go", "nuget", "cargo"}
    assert "does not exist" in why["go"] and "graph cache go --fill" in why["go"] and "not configured" in why["nuget"]
    go, nuget = tmp_path / "go", tmp_path / "nuget"
    (go / "m").mkdir(parents=True)
    nuget.mkdir()
    use, why = jobs.offline(settings(tmp_path, offline_caches={"nuget": str(nuget)}, go_cache=str(go)))
    assert use == {"go": str(go), "nuget": str(nuget)} and set(why) == {"cargo"}
    assert jobs._cache_env("nuget", "/n") == {"NUGET_PACKAGES": "/n"} and jobs._cache_env("cargo", "/c") == {
        "CARGO_HOME": "/c"
    }
    env = jobs._cache_env("go", "/g")
    assert env["GOMODCACHE"] == "/g" and env["GOPROXY"] == "off" and "GOFLAGS" not in env  # the real tree: no -mod=mod


# ================================================================ running engines
@SH
def test_an_engine_of_the_plan_runs_and_its_output_reaches_the_adapters(repo, tmp_path, bindir):
    make_engine(
        bindir, "vulture", stdout="v.py:1: unused function 'dead_fn' (100% confidence)", rc=3, version="vulture 2.14"
    )
    eng = gather(repo, tmp_path, langs_=("python",), only=("vulture",))
    (run,) = eng.runs
    assert (run.tool, run.fmt, run.rc, run.root, run.prefix) == ("vulture", "text", 3, str(repo), "")
    assert run.text.strip().startswith("v.py:1: unused function")
    assert adapters.rc_ok("vulture", run.rc)  # 3 is the healthy "found something" code of vulture
    (hit,) = adapters.parse(run.tool, run.text, run.fmt, run.root)
    assert (hit.path, hit.line, hit.claim, hit.name, hit.conf) == ("v.py", 1, "func", "dead_fn", 100)
    assert (
        eng.rows == ({"engine": "vulture", "label": "vulture", "status": "ok", "detail": ""},)
        and eng.down == ()
        and eng.indexes == ()
    )


@SH
def test_the_engine_gets_a_private_home_the_registry_environment_and_no_network_variables(repo, tmp_path, bindir):
    make_script(
        bindir,
        "go-deadcode",
        'echo "{\\"home\\": \\"$HOME\\", \\"proxy\\": \\"$GOPROXY\\", \\"toolchain\\": \\"$GOTOOLCHAIN\\", \\"cache\\": \\"$GOMODCACHE\\", \\"cwd\\": \\"$PWD\\"}"\n',
    )
    seen = []

    def run(argv, cwd, timeout, env):
        seen.append((argv, cwd, env))
        return jobs.default_run(argv, cwd, timeout, env)

    eng = gather(repo, tmp_path, langs_=("go",), only=("go-deadcode",), run=run)
    ((argv, cwd, env),) = seen
    assert argv[1:] == ["-json", "./..."] and cwd != str(repo / "svc")  # the job runs in a private copy, not in the tree
    assert cwd == os.path.join(env["HOME"], "tree", "svc") and env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert (
        env["GOPROXY"] == "off"
        and env["GOTOOLCHAIN"] == "local"
        and env["GOFLAGS"] == "-mod=readonly"
        and env["GOTELEMETRY"] == "off"
    )
    assert env["HOME"] == env["TMPDIR"] and "dcc-job-" in env["HOME"] and "GOMODCACHE" not in env
    assert not os.path.exists(env["HOME"])  # the private directory of the job is gone
    assert eng.runs[0].prefix == "svc" and eng.runs[0].covered == ("svc/main.go",)


@SH
def test_the_go_cache_is_given_to_an_engine_that_profits_from_it_when_it_can_be_used(repo, tmp_path, bindir):
    make_script(bindir, "go-deadcode", 'echo "[]"\n')
    go = tmp_path / "gomod"
    (go / "m").mkdir(parents=True)
    seen = []

    def run(argv, cwd, timeout, env):
        seen.append(dict(env))
        return jobs.default_run(argv, cwd, timeout, env)

    gather(repo, tmp_path, langs_=("go",), only=("go-deadcode",), run=run, go_cache=str(go))
    gather(repo, tmp_path, langs_=("go",), only=("go-deadcode",), run=run)  # no usable cache: it runs without
    assert seen[0]["GOMODCACHE"] == str(go) and seen[0]["GOPROXY"] == "off" and seen[0]["GOFLAGS"] == "-mod=readonly"
    assert "GOMODCACHE" not in seen[1] and len(seen) == 2


@SH
def test_an_indexer_that_needs_a_cache_is_skipped_with_the_reason_when_there_is_none(repo, tmp_path, bindir):
    make_script(bindir, "scip-go", 'echo "must not run" > "$0.ran"\n')
    eng = gather(repo, tmp_path, roles=("sem",), langs_=("go",), only=("scip-go",))
    assert (
        eng.runs == ()
        and eng.rows[0]["status"] == "skipped"
        and "the offline cache go is not usable" in eng.rows[0]["detail"]
    )
    assert "graph cache go --fill" in eng.rows[0]["detail"] and not (bindir / "scip-go.ran").exists()
    assert eng.down == ("scip-go",) and codes(eng)[0][0] == "dc.engine-incomplete"


@SH
def test_an_indexer_leaves_its_index_in_the_semantic_cache_and_the_next_run_reuses_it(repo, tmp_path, bindir):
    data = (FIX / "sem" / "py" / "index.scip").read_bytes()
    count = tmp_path / "runs"
    make_script(
        bindir,
        "scip-python",
        f'for a in "$@"; do last="$a"; done\ncase "$last" in --version) ;; *) echo run >> {count}; cp "{FIX}/sem/py/index.scip" "$last";; esac\n',
    )
    st = dcstore.Store(str(tmp_path / "g.sqlite"))
    b = tree_of(repo)
    cfg = jobs.Settings(
        roles=("sem",), langs=("python",), only=("scip-python",), workers=1, base=str(tmp_path / "data")
    )
    first = jobs.gather(str(repo), b, cfg, None, st, jobs.default_run, now=1000.0)
    assert [r["status"] for r in first.rows] == ["ok"] and len(first.indexes) == 1
    engine, rel, blob = first.indexes[0]
    assert (engine, rel, blob) == (
        "scip-python",
        "",
        data,
    ) and first.runs == ()  # an index is stored, it is not an engine run
    (key,) = st.engine_keys(semcache.SEM)
    assert key.startswith("sem:scip-python::") and semcache.get_index(st, key)[1] == data
    assert count.read_text() == "run\n"
    second = jobs.gather(str(repo), b, cfg._replace(reuse=("index",)), None, st, jobs.default_run, now=2000.0)
    assert [(r["status"], r["detail"].startswith("files unchanged")) for r in second.rows] == [
        ("cached", True)
    ] and second.indexes == ()
    assert count.read_text() == "run\n"  # the engine was not started again
    (repo / "v.py").write_text(
        "def other():\n    return 1\n"
    )  # the package changed: the index is stale, its job runs again
    third = jobs.gather(
        str(repo), tree_of(repo), cfg._replace(reuse=("index",)), None, st, jobs.default_run, now=3000.0
    )
    assert [r["status"] for r in third.rows] == ["ok"] and count.read_text() == "run\nrun\n"
    changed = make_script(
        bindir, "scip-python", "exit 9\n"
    )  # another engine (other bytes) is another version: the item is stale
    fourth = jobs.gather(
        str(repo), tree_of(repo), cfg._replace(reuse=("index",)), None, st, jobs.default_run, now=4000.0
    )
    assert (
        changed.exists() and [r["status"] for r in fourth.rows] == ["failed"] and "exit 9" in fourth.rows[0]["detail"]
    )
    st.close()


@SH
def test_an_index_that_cannot_be_stored_is_one_w_and_an_indexer_that_writes_none_failed(repo, tmp_path, bindir):
    make_script(
        bindir, "scip", 'for a in "$@"; do last="$a"; done\n'
    )  # the generic `scip` tool: no language family in the registry
    st = dcstore.Store(str(tmp_path / "g.sqlite"))
    make_script(bindir, "scip-python", "exit 0\n")  # exits 0 and writes no index
    eng = jobs.gather(
        str(repo),
        tree_of(repo),
        jobs.Settings(roles=("sem",), langs=("python",), only=("scip-python",), workers=1),
        None,
        st,
    )
    assert (
        eng.rows[0]["status"] == "failed"
        and "no index written" in eng.rows[0]["detail"]
        and eng.down == ("scip-python",)
    )
    st.close()


@SH
def test_an_engine_that_fails_to_start_or_times_out_or_writes_no_file_is_a_w_never_an_error(repo, tmp_path, bindir):
    make_script(bindir, "vulture", "sleep 30\n")
    make_script(bindir, "ruff", "echo boom >&2; exit 0\n")  # healthy exit, but the output is not a ruff report
    eng = gather(repo, tmp_path, langs_=("python",), timeout_s=1.0)
    status = {r["engine"]: (r["status"], r["detail"]) for r in eng.rows}
    assert status["vulture"][0] == "failed" and "timeout after 1s" in status["vulture"][1]
    assert status["ruff"] == ("ok", "")  # the run itself is fine; adapters.reconcile will say the output was not read
    assert "vulture" in eng.down and all(f.level == "W" for f in eng.findings)


@SH
def test_a_stream_written_to_a_file_is_read_and_a_missing_file_is_a_failed_job(repo, tmp_path, bindir):
    repo2 = new_repo(tmp_path, {"A.kt": "class A\n"}, name="kt")
    make_script(
        bindir,
        "detekt",
        'for a in "$@"; do case "$a" in xml:*) f="${a#xml:}";; esac; done\necho "<checkstyle version=\\"4.3\\"></checkstyle>" > "$f"\n',
    )
    eng = gather(repo2, tmp_path, langs_=("kotlin",), only=("detekt",))
    assert eng.rows[0]["status"] == "ok" and eng.runs[0].text.startswith("<checkstyle")
    make_script(bindir, "detekt", "exit 0\n")
    eng = gather(repo2, tmp_path, langs_=("kotlin",), only=("detekt",))
    assert eng.rows[0]["status"] == "failed" and "no detekt.xml written" in eng.rows[0]["detail"]


@SH
def test_the_budget_of_the_run_skips_what_has_not_started_and_names_it(repo, tmp_path, bindir):
    make_engine(bindir, "vulture")
    eng = jobs.gather(
        str(repo),
        tree_of(repo),
        settings(tmp_path, roles=("audit",), langs=("python",), only=("vulture",), budget_s=0.5),
        None,
    )
    assert (
        eng.rows[0]["status"] == "skipped"
        and "the budget of 0.5s is spent" in eng.rows[0]["detail"]
        and eng.down == ("vulture",)
    )


@SH
def test_missing_engines_are_one_w_each_with_the_install_hint_and_the_ones_found_but_not_wired_are_named_once(
    repo, tmp_path, bindir
):
    make_engine(bindir, "vulture")
    make_engine(bindir, "pyan3")  # in the plan of the python audit, installed, and no command line in data/audit.json
    make_engine(bindir, "basedpyright")  # the language-server step runs this one: not "unwired"
    eng = gather(repo, tmp_path, roles=("audit", "sem"), langs_=("python",))
    by = {f.code: [] for f in eng.findings}
    for f in eng.findings:
        by[f.code].append(f.msg)
    (unwired,) = by["dc.engine-unwired"]
    assert "pyan3" in unwired and "basedpyright" not in unwired and "vulture" not in unwired
    inc = by["dc.engine-incomplete"]
    assert any(m.startswith("deptry: not run (missing: ") and "pipx install deptry" in m for m in inc)
    assert any(
        m.startswith("scip-python: not run (missing: ") and "npm install -g @sourcegraph/scip-python" in m for m in inc
    )
    assert len([m for m in inc if m.startswith("vulture")]) == 0 and len({m.split(":")[0] for m in inc}) == len(
        inc
    )  # once per engine
    assert all(f.path == "(dead-code-checker)" and f.level == "W" for f in eng.findings)


@SH
def test_an_engine_waived_in_the_configuration_is_degraded_not_missing(repo, tmp_path, bindir):
    cfg = SimpleNamespace(
        is_waived=lambda code, path=None, engine=None: engine == "deptry" and code == "dc.engine-incomplete"
    )
    eng = gather(repo, tmp_path, langs_=("python",), cfg=cfg)
    msgs = [f.msg for f in eng.findings if f.msg.startswith("deptry")]
    assert len(msgs) == 1 and "waived" in msgs[0] and "install:" not in msgs[0]


def test_an_engine_without_a_build_for_this_os_is_one_w_without_an_install_line(tmp_path, bindir, monkeypatch):
    repo = new_repo(tmp_path, {"a.c": "int main(void){return 0;}\n"}, name="c")
    monkeypatch.setattr(dce, "host_os", lambda: "windows")
    eng = gather(repo, tmp_path, langs_=("cpp",))
    (cflow,) = [f.msg for f in eng.findings if f.msg.startswith("cflow")]
    assert cflow == "cflow: not run here (no build of this engine for this OS)"


@SH
def test_the_result_does_not_depend_on_the_number_of_workers(repo, tmp_path, bindir):
    make_engine(bindir, "vulture", stdout=VULTURE_OUT.strip(), rc=3)
    make_engine(bindir, "shellcheck", stdout="[]")
    make_engine(bindir, "ruff", stdout="[]", rc=0)
    make_script(bindir, "go-deadcode", 'echo "[]"\n')
    one = gather(repo, tmp_path, langs_=("python", "shell", "go"), workers=1)
    many = gather(repo, tmp_path, langs_=("python", "shell", "go"), workers=4)
    assert [r["label"] for r in one.rows] == sorted(r["label"] for r in one.rows)
    assert one.rows == many.rows and one.runs == many.runs
    assert [f.render() for f in one.findings] == [f.render() for f in many.findings]


# ================================================================ the sandbox
@SH
def test_a_build_engine_runs_through_the_sandbox_prefix_and_the_weak_mode_is_one_w(tmp_path, bindir):
    repo = new_repo(tmp_path, {"Cargo.toml": '[package]\nname = "x"\n', "src/lib.rs": "pub fn f() {}\n"}, name="rs")
    make_script(bindir, "cargo", 'echo "{\\"boxed\\": \\"$DCC_BOXED\\", \\"reason\\": \\"x\\"}"\n')
    boxed = sandbox.Mode("netless", ("env", "DCC_BOXED=yes"), "no bubblewrap on this host: test note")
    seen = []

    def run(argv, cwd, timeout, env):
        seen.append((argv, env))
        return jobs.default_run(argv, cwd, timeout, env)

    eng = jobs.gather(
        str(repo),
        tree_of(repo),
        settings(tmp_path, roles=("audit",), langs=("rust",), only=("cargo",)),
        None,
        run=run,
        mode=boxed,
    )
    ((argv, env),) = seen
    assert (
        argv[:2] == ["env", "DCC_BOXED=yes"]
        and argv[2].endswith("/cargo")
        and "--offline" in argv
        and "--message-format=json" in argv
    )
    assert any(a.endswith("/target") and "dcc-job-" in a for a in argv) and env["CARGO_TARGET_DIR"].endswith(
        "/out/cargo-target"
    )
    assert (
        env["GOCACHE"].endswith("/out/go-build")
        and env["XDG_CACHE_HOME"].endswith("/.cache")
        and env["CARGO_NET_OFFLINE"] == "true"
    )
    assert eng.runs[0].text.startswith('{"boxed": "yes"') and [f.msg for f in eng.findings] == [
        "no bubblewrap on this host: test note"
    ]
    assert [f.code for f in eng.findings] == ["dc.sandbox-weak"]
    seen.clear()
    plain = gather(repo, tmp_path, langs_=("rust",), only=("cargo",), run=run)  # the isolate() fixture's mode: none
    assert seen[0][0][0].endswith("/cargo") and plain.findings == ()  # no note when the mode has none


REAL_BWRAP = bool(shutil.which("bwrap")) and sandbox._probe_bwrap()


@SH
@pytest.mark.skipif(not REAL_BWRAP, reason="no working bubblewrap on this host")
def test_a_build_engine_under_the_real_bubblewrap_cannot_write_into_the_tree(tmp_path, bindir):
    repo = new_repo(tmp_path, {"Cargo.toml": '[package]\nname = "x"\n', "src/lib.rs": "pub fn f() {}\n"}, name="rs")
    make_script(
        bindir,
        "cargo",
        'echo built > planted.txt; echo "planted:$?" >&2\nfor a in "$@"; do last="$a"; done\nmkdir -p "$last" && echo ok > "$last/t"\necho "{}"\n',
    )
    eng = jobs.gather(
        str(repo),
        tree_of(repo),
        settings(tmp_path, roles=("audit",), langs=("rust",), only=("cargo",)),
        None,
        mode=sandbox.detect(refresh=True),
    )
    assert not (repo / "planted.txt").exists() and eng.rows[0]["status"] == "ok" and eng.findings == ()


# ================================================================ the analysed tree is never written
WRITER = r"""case "$PWD" in *dcc-job-*) ;; *) echo "not in a private copy: $PWD" >&2; exit 9;; esac
echo x > planted.txt
echo x > Cargo.lock; echo x > go.sum; echo x > tsconfig.tsbuildinfo
mkdir -p __pycache__ .cache node_modules/.cache_probe_never
echo x > __pycache__/m.cpython-313.pyc; echo x > .cache/index
for f in * ../*; do [ -f "$f" ] && echo x >> "$f"; done
echo x > ../planted_above.txt
for d in node_modules ../node_modules ../../node_modules; do
  [ -f "$d/dep.js" ] && echo x >> "$d/dep.js"
  [ -d "$d" ] && echo x > "$d/.package-lock.json"
done
echo "[]"
"""
MARKER_FILE = {"Cargo.toml": '[package]\nname = "x"\n', "go.mod": "module x\n", "package.json": "{}\n"}


def fingerprint(root):
    """Every directory and every byte under `root` (the .git directory too): equal before and after means nothing was written."""
    import hashlib

    h, seen = hashlib.sha256(), []  # (the writer below also tries to make a directory inside node_modules)
    for dp, dns, fns in os.walk(root):
        dns.sort()
        for n in sorted(fns):
            p = os.path.join(dp, n)
            h.update(os.path.relpath(p, root).encode() + b"\0" + (b"L" + os.readlink(p).encode() if os.path.islink(p) else Path(p).read_bytes()) + b"\0")
        seen += [os.path.relpath(os.path.join(dp, d), root) for d in dns]
    return h.hexdigest(), tuple(seen)


def project_for(spec, name):
    """The files that make the tree hold a job of this engine (and a second one below it, for a marker engine)."""
    files = {"README.md": "# x\n", "run.sh": "echo $x\n", "v.py": "def f():\n    return 1\n"}
    if spec["scope"] == "marker":
        mk = spec["marker"].replace("*", "main")
        files.update({mk: MARKER_FILE.get(mk, "x\n"), f"sub/deep/{mk}": MARKER_FILE.get(mk, "x\n"), "sub/deep/a.txt": "a\n", "sub/base.txt": "b\n"})
    return files


def job_context(root, tmp_path, paths, **kw):
    return jobs.Ctx(
        str(root),
        settings(tmp_path),
        jobs.default_run,
        sandbox.Mode("none", (), ""),
        10**12,
        __import__("time").monotonic,
        {},
        tuple(sorted(paths)),
    )


ALL_ENGINES = sorted(
    [("engines", n) for n in jobs.table()["engines"]] + [("indexers", n) for n in jobs.table()["indexers"]]
)


@SH
@pytest.mark.parametrize("section,name", ALL_ENGINES, ids=[f"{s}-{n}" for s, n in ALL_ENGINES])
def test_no_engine_of_the_table_writes_into_the_analysed_tree(section, name, tmp_path, bindir):
    spec = jobs.table()[section][name]
    repo = new_repo(tmp_path, {**project_for(spec, name), ".gitignore": "node_modules/\n"}, name="proj")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "dep.js").write_text("dep\n")  # a small dependency file an engine rewrites in place
    (repo / "node_modules" / ".package-lock.json").write_text("{}\n")
    make_script(bindir, name, WRITER)
    tree = tree_of(repo)
    before = fingerprint(repo)
    u = dce.Unit(name, ("x",), False, str(bindir / name), 1, section == "indexers")
    langs_of = {"run.sh": "shell", "v.py": "python"}
    js = jobs.expand(u, spec, sorted(tree.tree.files), langs_of, "proj", section == "indexers")
    assert js, "the project holds no job of this engine"
    cx = job_context(repo, tmp_path, tree.tree.files)
    for j in js:
        out = jobs._run_job(j._replace(caches=(), cache_opt=()), cx)
        assert out.rc == 0, out  # the writer runs (it refuses to start anywhere but in a private copy: rc 9)
    assert fingerprint(repo) == before, f"{name} wrote into the tree"
    assert not (tmp_path / "planted_above.txt").exists()
    assert git(repo, "status", "--porcelain", "--ignored") == "!! node_modules/"
    assert (repo / "node_modules" / "dep.js").read_text() == "dep\n"


@SH
def test_cargo_cannot_rewrite_the_lock_of_the_tree_and_its_paths_come_back_as_the_real_ones(tmp_path, bindir):
    repo = new_repo(
        tmp_path, {"Cargo.toml": '[package]\nname = "x"\n', "Cargo.lock": "# real lock\n", "src/lib.rs": "pub fn f() {}\n"}, name="rs"
    )
    make_script(
        bindir,
        "cargo",
        'echo "# resolved again" > Cargo.lock; echo "{\\"file\\": \\"$PWD/src/lib.rs\\", \\"lock\\": \\"$(cat Cargo.lock)\\"}"\n',
    )
    before = fingerprint(repo)
    eng = gather(repo, tmp_path, langs_=("rust",), only=("cargo",))
    assert fingerprint(repo) == before and (repo / "Cargo.lock").read_text() == "# real lock\n"
    (run,) = eng.runs
    assert str(repo / "src" / "lib.rs") in run.text and "dcc-job-" not in run.text and "resolved again" in run.text


@SH
def test_a_python_engine_leaves_no_bytecode_and_a_vulture_path_is_the_real_one(tmp_path, bindir, repo):
    make_script(
        bindir,
        "vulture",
        'python3 -c "import os" ; echo "$PWD/v.py:1: unused function \'dead_fn\' (60% confidence)"; echo "$PYTHONDONTWRITEBYTECODE" >&2\n',
    )
    before = fingerprint(repo)
    eng = gather(repo, tmp_path, langs_=("python",), only=("vulture",))
    assert fingerprint(repo) == before and not list(repo.rglob("__pycache__"))
    assert eng.runs[0].text.startswith(f"{repo}/v.py:1:") and "dcc-job-" not in eng.runs[0].text


def _marker_tree(tmp_path):
    repo = new_repo(
        tmp_path,
        {
            "package.json": "{}\n",
            "tsconfig.base.json": "{}\n",
            "other/x.ts": "x\n",
            "app/package.json": "{}\n",
            "app/tsconfig.json": "{}\n",
            "app/src/a.ts": "a\n",
            ".gitignore": "node_modules/\n",
        },
    )
    (repo / "app" / "node_modules").mkdir()
    (repo / "app" / "node_modules" / "dep.js").write_text("dep\n")
    return repo


def _copy_of(repo, tmp_path, job):
    dst = tmp_path / "copy"
    dst.mkdir()
    paths = sorted(tree_of(repo).tree.files)
    work = jobs._private_tree(str(repo), job, paths, str(dst), 10**12, __import__("time").monotonic)
    return dst, work


@SH
def test_the_copy_of_a_marker_job_holds_the_whole_tree_and_the_small_dependencies_are_copied_the_large_ones_linked(tmp_path, bindir):
    repo = _marker_tree(tmp_path)
    big = repo / "app" / "node_modules" / "big.bin"
    big.write_bytes(b"\0" * (jobs.DEP_COPY_MAX + 1))
    (repo / "app" / "node_modules" / "pkg").mkdir()
    (repo / "app" / "node_modules" / "pkg" / "i.js").write_text("i\n")
    os.symlink("pkg/i.js", repo / "app" / "node_modules" / "link.js")  # a link of the dependencies is made again as it is
    j = jobs.Job("tsc@app", "tsc", ("x",), "app", "stdout", "text", 1, False, (), (), False, (), "marker")
    dst, work = _copy_of(repo, tmp_path, j)
    got = sorted(str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file() or p.is_symlink())
    assert work == str(dst / "app")
    assert got == [
        ".gitignore",
        "app/node_modules/big.bin",
        "app/node_modules/dep.js",
        "app/node_modules/link.js",
        "app/node_modules/pkg/i.js",
        "app/package.json",
        "app/src/a.ts",
        "app/tsconfig.json",
        "other/x.ts",  # a sibling directory: a go.work, a `replace ../x`, a path dependency or `references` names it
        "package.json",
        "tsconfig.base.json",
    ]
    nm = dst / "app" / "node_modules"
    assert nm.is_dir() and not nm.is_symlink()
    assert (nm / "dep.js").read_text() == "dep\n" and not (nm / "dep.js").is_symlink()  # a small file is a copy ...
    assert (nm / "big.bin").is_symlink() and (nm / "big.bin").resolve() == big.resolve()  # ... a large one is read through a link
    assert (nm / "link.js").is_symlink() and os.readlink(nm / "link.js") == "pkg/i.js" and (nm / "link.js").read_text() == "i\n"
    (nm / "dep.js").write_text("rewritten\n")
    (nm / ".cache").mkdir()
    (nm / ".cache" / "c").write_text("x")  # what an engine writes there stays in the copy
    assert sorted(os.listdir(repo / "app" / "node_modules")) == ["big.bin", "dep.js", "link.js", "pkg"]
    assert (repo / "app" / "node_modules" / "dep.js").read_text() == "dep\n"
    f = jobs.Job("shellcheck#0", "shellcheck", ("x", "--", "app/src/a.ts"), "", "stdout", "json", 1, False, (), (), False, (), "files")
    assert jobs._copied(f, sorted(tree_of(repo).tree.files)) == ["app/src/a.ts"]


@SH
def test_a_host_without_symbolic_links_gets_copies_of_the_dependency_files(tmp_path, bindir, monkeypatch):
    repo = _marker_tree(tmp_path)
    big = repo / "app" / "node_modules" / "big.bin"
    big.write_bytes(b"b" * (jobs.DEP_COPY_MAX + 1))
    os.symlink("dep.js", repo / "app" / "node_modules" / "link.js")

    def no_privilege(*a, **k):
        raise OSError("A required privilege is not held by the client")

    monkeypatch.setattr(jobs.os, "symlink", no_privilege)
    j = jobs.Job("tsc@app", "tsc", ("x",), "app", "stdout", "text", 1, False, (), (), False, (), "marker")
    dst, _ = _copy_of(repo, tmp_path, j)
    nm = dst / "app" / "node_modules"
    assert not any(p.is_symlink() for p in nm.rglob("*"))
    assert big.read_bytes() == (nm / "big.bin").read_bytes() and (nm / "link.js").read_text() == "dep\n"


@SH
def test_a_dependency_directory_that_cannot_be_mirrored_is_a_failed_job_and_the_engine_never_starts(tmp_path, bindir, monkeypatch):
    repo = _marker_tree(tmp_path)
    (repo / "app" / "node_modules" / "second.js").write_text("2\n")
    monkeypatch.setattr(jobs, "MIRROR_MAX", 1)
    mark = tmp_path / "mark"
    monkeypatch.setenv("DCC_MARK", str(mark))
    make_script(bindir, "tsc", 'echo started > "$DCC_MARK"\n')
    j = jobs.Job("tsc@app", "tsc", (str(bindir / "tsc"),), "app", "stdout", "text", 1, False, (), (), False, (), "marker")
    out = jobs._run_job(j, job_context(repo, tmp_path, tree_of(repo).tree.files))
    assert out.status == "failed" and "no private copy of the tree" in out.detail and "too large to mirror" in out.detail
    assert not mark.exists()


GO_WORK = {
    "go.work": "go 1.18\n\nuse (\n\t./a\n\t./b\n)\n",
    "a/go.mod": "module example.com/a\n\ngo 1.18\n",
    "a/main.go": 'package main\n\nimport "example.com/b"\n\nfunc main() { _ = b.B() }\n',
    "b/go.mod": "module example.com/b\n\ngo 1.18\n",
    "b/b.go": "package b\n\nfunc B() int { return 1 }\n",
}


def _go_workspace_jobs(tmp_path, bindir, script):
    repo = new_repo(tmp_path, GO_WORK, name="ws")
    make_script(bindir, "go-deadcode", script)
    spec = jobs.table()["engines"]["go-deadcode"]
    tree = tree_of(repo)
    u = dce.Unit("go-deadcode", ("x",), False, str(bindir / "go-deadcode"), 1, False)
    js = jobs.expand(u, spec, sorted(tree.tree.files), {"a/main.go": "go", "b/b.go": "go"}, "ws", False)
    return repo, js, job_context(repo, tmp_path, tree.tree.files)


@SH
def test_a_marker_job_of_a_go_workspace_sees_the_sibling_modules_the_go_work_names(tmp_path, bindir):
    repo, js, cx = _go_workspace_jobs(
        tmp_path,
        bindir,
        'for f in ../go.work ../b/go.mod ../b/b.go go.mod; do [ -f "$f" ] || { echo "missing $f" >&2; exit 4; }; done\n'
        'echo "$PWD" | sed "s|.*/tree/||"\ncat ../b/go.mod\n',
    )
    assert [j.rel for j in js] == ["a", "b"]  # one job per module, each in the copy of the whole workspace
    before = fingerprint(repo)
    for j in js:
        out = jobs._run_job(j._replace(caches=(), cache_opt=()), cx)
        assert out.status == "ok" and out.rc == 0, out
        assert out.text.splitlines()[0] == j.rel and "module example.com/b" in out.text
    assert fingerprint(repo) == before


@SH
@pytest.mark.skipif(shutil.which("go") is None, reason="needs the go tool")
def test_the_real_go_tool_lists_the_packages_of_a_workspace_module_in_its_private_copy(tmp_path, bindir):
    repo, js, cx = _go_workspace_jobs(
        tmp_path,
        bindir,
        f'export GOTOOLCHAIN=local GOFLAGS=-mod=readonly GOPROXY=off GOCACHE="$TMPDIR/gocache" GOPATH="$TMPDIR/gopath"\n'
        f'exec "{shutil.which("go")}" list -deps ./...\n',
    )
    before = fingerprint(repo)
    out = jobs._run_job(js[0]._replace(caches=(), cache_opt=()), cx)  # the module a imports the module b of the workspace
    assert out.status == "ok" and out.rc == 0, out
    assert "example.com/a" in out.text.split() and "example.com/b" in out.text.split()
    assert fingerprint(repo) == before


@SH
def test_a_tree_that_cannot_be_copied_is_a_failed_job_and_the_engine_never_starts(tmp_path, bindir, repo, monkeypatch):
    make_script(bindir, "vulture", 'echo started > "$DCC_MARK"\n')
    monkeypatch.setattr(jobs.shutil, "copy", lambda *a, **k: (_ for _ in ()).throw(OSError("No space left on device")))
    mark = tmp_path / "mark"
    monkeypatch.setenv("DCC_MARK", str(mark))
    eng = gather(repo, tmp_path, langs_=("python",), only=("vulture",))
    assert not mark.exists() and eng.runs == () and eng.rows[0]["status"] == "failed"
    assert "no private copy of the tree" in eng.rows[0]["detail"] and "No space left" in eng.rows[0]["detail"]
    assert [f.code for f in eng.findings] == ["dc.engine-incomplete"]
