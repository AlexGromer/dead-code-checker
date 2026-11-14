"""graph.gocache: the offline Go module cache: where it is, whether it can be used, what an engine gets from it, what it lacks and the
commands that fill it; `graph cache go`.

python3 -m pytest -q tests/test_graph_gocache.py

The module under test is synthetic: example.test/app requires example.test/dep, which a hand-made file:// proxy serves; the cache is
filled by the real `go` toolchain from that proxy, so no network is involved. A test that needs the toolchain is skipped, with the
reason, on a host that has none. The process environment is never the source of the cache (GOMODCACHE of the session is poisoned in
every test to prove it).
"""

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from graph_fake_engines import SH, block_network, make_script

from dead_code_checker.graph import cli, engines as dce, gocache

REG = dce.load_registry()
GO = shutil.which("go")
needs_go = pytest.mark.skipif(
    not GO, reason="no go toolchain on this host: the synthetic module cannot be filled or checked"
)
MAIN = 'package main\n\nimport "example.test/dep"\n\nfunc main() { println(dep.Used()) }\n\nfunc orphan() int { return 3 }\n'
DEP_GO = "package dep\n\nfunc Used() int { return 1 }\n"


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    block_network(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(
        "GOMODCACHE", str(tmp_path / "poisoned-session-cache")
    )  # the process env must never be the source
    monkeypatch.delenv(gocache.ENV_DIR, raising=False)
    monkeypatch.delenv(dce.ENV_LOCK, raising=False)
    monkeypatch.delenv(dce.ENV_DIR, raising=False)
    dce.reset()
    old = os.umask(0o022)
    yield
    os.umask(old)
    dce.reset()


def _writable(path):
    for dp, _dns, fns in os.walk(path):
        os.chmod(dp, 0o755)
        for f in fns:
            os.chmod(os.path.join(dp, f), 0o644)


def proxy_module(proxy: Path, mod: str, files: dict, requires: str = "") -> None:
    d = proxy / mod / "@v"
    d.mkdir(parents=True)
    gomod = f"module {mod}\n\ngo 1.20\n{requires}"
    (d / "v1.0.0.mod").write_text(gomod)
    (d / "v1.0.0.info").write_text(json.dumps({"Version": "v1.0.0", "Time": "2024-01-01T00:00:00Z"}))
    (d / "list").write_text("v1.0.0\n")
    with zipfile.ZipFile(d / "v1.0.0.zip", "w") as z:
        z.writestr(f"{mod}@v1.0.0/go.mod", gomod)
        for name, text in files.items():
            z.writestr(f"{mod}@v1.0.0/{name}", text)


def go_env(tmp: Path, cache: Path, proxy: str) -> dict:
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp),
        "GOCACHE": str(tmp / "gocache"),
        "GOMODCACHE": str(cache),
        "GOFLAGS": "-mod=mod",
        "GOPROXY": proxy,
        "GOSUMDB": "off",
        "GOTOOLCHAIN": "local",
        "GOTELEMETRY": "off",
        "GOVCS": "*:off",
    }


def build_stand(tmp_path, request, graph_only):
    """repo (example.test/app, a git repository), the cache filled from the file proxy and made read-only, an empty cache.
    graph_only: dep's go.mod also requires a module that nothing imports and whose archive nobody downloads."""
    if not GO:
        pytest.skip("no go toolchain on this host")
    proxy, repo, cache = tmp_path / "proxy", tmp_path / "repo", tmp_path / "cache"
    extra = "\nrequire example.test/graphonly v1.0.0\n" if graph_only else ""
    proxy_module(proxy, "example.test/dep", {"dep.go": DEP_GO}, extra)
    if graph_only:
        proxy_module(proxy, "example.test/graphonly", {"g.go": "package graphonly\n"})
        (proxy / "example.test/graphonly/@v/v1.0.0.zip").unlink()  # only its go.mod is ever read
    repo.mkdir()
    (repo / "go.mod").write_text("module example.test/app\n\ngo 1.20\n\nrequire example.test/dep v1.0.0\n")
    (repo / "main.go").write_text(MAIN)
    work = tmp_path / "work"
    work.mkdir()
    r = subprocess.run(
        [GO, "mod", "tidy"], cwd=repo, env=go_env(work, cache, f"file://{proxy}"), capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    scratch = (
        tmp_path / "scratch"
    )  # the fill: `go mod download` in a copy of go.mod and go.sum (it also stores the .info files)
    scratch.mkdir()
    for name in ("go.mod", "go.sum"):
        shutil.copy(repo / name, scratch / name)
    r = subprocess.run(
        [GO, "mod", "download"], cwd=scratch, env=go_env(work, cache, f"file://{proxy}"), capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    (tmp_path / "empty").mkdir()
    for p in (cache, tmp_path / "empty"):
        for dp, _dns, fns in os.walk(p):
            for f in fns:
                os.chmod(os.path.join(dp, f), 0o444)
            os.chmod(dp, 0o555)
    request.addfinalizer(lambda: _writable(cache))
    request.addfinalizer(lambda: _writable(tmp_path / "empty"))
    return SimpleNamespace(repo=repo, cache=cache, empty=tmp_path / "empty", tmp=tmp_path)


@pytest.fixture
def stand(tmp_path, request):
    return build_stand(tmp_path, request, False)


@pytest.fixture
def graph_stand(tmp_path, request):
    return build_stand(tmp_path, request, True)


# ------------------------------------------------------------------ where the cache is, and whether it can be used
def test_the_cache_is_the_option_then_the_variable_then_the_data_root_never_the_process_gomodcache(
    tmp_path, monkeypatch
):
    base = tmp_path / "data"
    assert gocache.location(base) == str(base / "caches" / "gomod")  # not the GOMODCACHE of the session
    assert gocache.location(None) == ""
    monkeypatch.setenv(gocache.ENV_DIR, str(tmp_path / "from-env"))
    assert gocache.location(base) == str(tmp_path / "from-env")
    assert gocache.location(base, str(tmp_path / "option")) == str(
        tmp_path / "option"
    )  # the option wins over the variable
    monkeypatch.setenv(gocache.ENV_DIR, "~/cache")
    assert gocache.location(None) == os.path.abspath(os.path.expanduser("~/cache"))


def test_a_cache_is_usable_when_it_exists_and_is_not_empty(tmp_path):
    base = tmp_path / "data"
    d, why = gocache.state(base)
    assert d == str(base / "caches" / "gomod") and why == "does not exist"
    assert gocache.state(None) == ("", "no directory (set --go-cache or $DEAD_CODE_CHECKER_GO_CACHE)")
    (base / "caches" / "gomod").mkdir(parents=True)
    assert gocache.state(base) == (d, "is empty")
    (base / "caches" / "gomod" / "x").write_text("x")
    assert gocache.state(base) == (d, "")
    f = tmp_path / "afile"
    f.write_text("")
    assert gocache.state(None, str(f))[1] == "does not exist"  # a file is not a cache directory


def test_the_variables_of_the_family_and_the_tree_they_are_for():
    reg_env = REG["sandbox"]["caches"]["gomod"]["env"]
    assert {"GOMODCACHE", "GOFLAGS", "GOPROXY", "GOSUMDB", "GOTOOLCHAIN"} <= set(reg_env) and reg_env[
        "GOFLAGS"
    ] == "-mod=mod"
    private = gocache.env(REG, "/c", private_tree=True)  # a scratch copy of the tree
    assert private == {
        "GOMODCACHE": "/c",
        "GOFLAGS": "-mod=mod",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOTOOLCHAIN": "local",
    }
    real = gocache.env(REG, "/c")  # the real tree: -mod=mod could write go.mod and go.sum
    assert "GOFLAGS" not in real and real["GOMODCACHE"] == "/c" and real["GOPROXY"] == "off"


def test_apply_leaves_the_environment_alone_and_says_why_when_there_is_no_cache(tmp_path):
    base = {"A": "1", "GOMODCACHE": "/session"}
    d, why = gocache.state(tmp_path / "data")
    env, note = gocache.apply(base, d, why)
    assert env is base and "does not exist" in note and "graph cache go --fill" in note
    c = tmp_path / "c"
    (c / "m").mkdir(parents=True)
    env, note = gocache.apply(base, *gocache.state(None, str(c)))
    assert note == "" and env["GOMODCACHE"] == str(c) and env["A"] == "1" and base["GOMODCACHE"] == "/session"


# ------------------------------------------------------------------ --check and --fill
@needs_go
def test_check_names_what_the_cache_lacks_and_touches_nothing(graph_stand):
    stand = graph_stand
    before = sorted(p.name for p in stand.repo.iterdir())
    gomod = (stand.repo / "go.mod").read_bytes(), (stand.repo / "go.sum").read_bytes()
    (rep,) = gocache.check(str(stand.repo), str(stand.empty), go=GO)
    assert rep.mod == "" and rep.note == "" and rep.total == 1
    assert [(m.module, m.version) for m in rep.missing] == [("example.test/dep", "v1.0.0")]
    assert "GOPROXY=off" in rep.missing[0].why  # no network was tried
    (full,) = gocache.check(str(stand.repo), str(stand.cache), go=GO)
    assert full.total == 1 and full.missing == ()  # example.test/graphonly is read as a go.mod only: not missing
    assert sorted(p.name for p in stand.repo.iterdir()) == before
    assert (
        (stand.repo / "go.mod").read_bytes(),
        (stand.repo / "go.sum").read_bytes(),
    ) == gomod  # -mod=mod ran on a copy
    gone = stand.tmp / "no-such-cache"
    assert gocache.check(str(stand.repo), str(gone), go=GO)[0].missing and not gone.exists()  # never created


@needs_go
def test_check_says_why_a_go_mod_cannot_be_checked(stand):
    (stand.repo / "go.mod").write_text("module example.test/app\n\ngo 1.99.9\n")
    (rep,) = gocache.check(str(stand.repo), str(stand.cache), go=GO)
    assert (
        rep.missing == () and rep.total == 0 and "go list failed" in rep.note
    )  # a toolchain newer than this go: not "all present"
    none = gocache.check_module(str(stand.repo), "", str(stand.cache), go="")
    assert "go was not found on PATH or in the engines directory" in none.note


@needs_go
def test_a_replace_to_a_directory_needs_no_cache(stand):
    (stand.repo / "go.mod").write_text(
        "module example.test/app\n\ngo 1.20\n\nrequire example.test/loc v1.0.0\n\nreplace example.test/loc => ./loc\n"
    )
    (stand.repo / "loc").mkdir()
    (stand.repo / "loc" / "go.mod").write_text("module example.test/loc\n\ngo 1.20\n")
    rep = gocache.check_module(str(stand.repo), "", str(stand.empty), go=GO)
    assert rep.total == 0 and rep.missing == () and rep.note == ""


def test_check_with_a_command_that_fails_or_cannot_start_is_a_note_not_a_crash(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n\ngo 1.20\n")

    def failing(argv, cwd, timeout, env):
        return 1, b"", b"go: updates to go.mod needed\n"

    rep = gocache.check_module(str(repo), "", str(tmp_path / "c"), run_fn=failing, go="/bin/true")
    assert rep.total == 0 and "go list failed (exit 1): go: updates to go.mod needed" in rep.note

    def absent(argv, cwd, timeout, env):
        raise OSError("no such file")

    assert (
        "go did not run: OSError"
        in gocache.check_module(str(repo), "", str(tmp_path / "c"), run_fn=absent, go="/x/go").note
    )
    assert (
        "go.mod cannot be read" in gocache.check_module(str(repo), "nosuch", str(tmp_path / "c"), go="/bin/true").note
    )


def test_the_modules_of_a_repository_are_the_go_mod_files_tracked_or_found(tmp_path):
    repo = tmp_path / "r"
    (repo / "svc").mkdir(parents=True)
    (repo / "svc" / "go.mod").write_text("module x\n")
    (repo / "go.mod").write_text("module y\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "go.mod").write_text("module z\n")
    assert gocache.modules_of(str(repo)) == ["", "svc"]  # a walk that skips node_modules (not a git repository)
    assert gocache.modules_of(str(repo), ["a/go.mod", "b/c.go", "go.mod"]) == ["", "a"]


def test_fill_prints_the_commands_and_runs_nothing(tmp_path):
    repo = tmp_path / "r"
    (repo / "svc").mkdir(parents=True)
    (repo / "svc" / "go.mod").write_text("module x\n")
    (repo / "svc" / "go.sum").write_text("")
    (repo / "go.mod").write_text("module y\n")  # no go.sum
    d = "/data/cache dir"
    text = gocache.fill_block(d, [str(repo)])
    assert text.startswith("# with network access: fill the Go module cache") and f"mkdir -p '{d}'" in text
    assert "sudo" not in text and "chown" not in text and "root" not in text
    lines = [ln for ln in text.splitlines() if "go mod download" in ln]
    assert len(lines) == 2  # one per go.mod
    root_line = next(ln for ln in lines if f"cp {repo}/go.mod " in ln and "go.sum" not in ln.split("&&")[1])
    assert f"{repo}/svc/go.mod" in next(ln for ln in lines if "svc" in ln) and f"{repo}/svc/go.sum" in text
    for ln in lines:
        assert "GOMODCACHE='/data/cache dir'" in ln and "GOFLAGS=-mod=mod" in ln and "GOTOOLCHAIN=local" in ln
        assert "mktemp -d" in ln and 'rm -rf -- "$T"' in ln  # the repository is never the working directory
    assert root_line and "graph cache go --check" in text
    (tmp_path / "none").mkdir()
    assert "no go.mod" in gocache.fill_block(d, [str(tmp_path / "none")])


@needs_go
def test_the_command_checks_fills_and_never_fails_on_what_is_missing(stand, capsys):
    argv = ["cache", "go", "--check", "--fill", str(stand.repo), "--dir", str(stand.empty)]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert (
        "example.test/dep v1.0.0" in out and "0 of 1 modules in the cache, missing:" in out and "go mod download" in out
    )
    assert cli.main(["cache", "go", "--check", str(stand.repo), "--dir", str(stand.cache), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["usable"] is True and doc["repos"][str(stand.repo)][0]["missing"] == []
    assert cli.main(["cache", "go", str(stand.repo)]) == 2  # neither --check nor --fill
    assert cli.main(["cache", "go", "--check", str(stand.tmp / "nope")]) == 2
    assert cli.main(["cache", "go", "--check"]) == 2


def test_fill_without_any_directory_says_why(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DEAD_CODE_CHECKER_HOME", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n")
    assert cli.main(["cache", "go", "--fill", str(repo)]) == 0
    out = capsys.readouterr().out
    assert (
        str(tmp_path / "data" / "caches" / "gomod") in out and "go mod download" in out
    )  # the default is under the data layout
    assert not (tmp_path / "data" / "caches").exists()  # saying it creates nothing


@SH
def test_the_go_binary_is_looked_up_with_the_engine_resolver(tmp_path):
    d = tmp_path / "bin"
    make_script(d, "go", "")
    assert gocache._go([str(d)]) == str(d / "go") and gocache._go([str(tmp_path / "nothing")]) == ""
