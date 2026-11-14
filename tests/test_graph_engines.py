"""graph.engines and graph.proc: the registry, the lock of sha256 pins, how an engine is found and judged, doctor and plan, and the
`graph doctor` / `graph lock` commands.

python3 -m pytest -q tests/test_graph_engines.py

Every engine here is a stub in a directory of the test (tests/graph_fake_engines.py); the PATH of the host is never searched, no
test touches the network (sockets are made to fail for the whole module).
"""

import copy
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from graph_fake_engines import SH, block_network, isolate, make_engine, make_script

from dead_code_checker.graph import cli, engines, proc, sandbox

OSES = ("linux", "darwin", "windows")


@pytest.fixture(autouse=True)
def _guards(tmp_path, monkeypatch):
    block_network(monkeypatch)
    monkeypatch.setenv("DEAD_CODE_CHECKER_HOME", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    yield from isolate(monkeypatch, tmp_path)


@pytest.fixture
def reg():
    return engines.load_registry()


@pytest.fixture
def bindir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    engines.configure(None, str(d), path=False)
    return d


def sha(p):
    return engines.sha256_file(str(p))


def lock_of(tmp_path, pins, plat=None, name="engines.lock.json"):
    p = tmp_path / name
    engines.write_lock(str(p), engines.make_lock(pins, plat))
    return str(p)


# ================================================================ the registry
def test_the_shipped_registry_is_valid_and_complete(reg):
    assert engines.validate_registry(reg) == []
    assert set(reg["oses"]) == set(OSES) and reg["roles"] == ["branch", "audit", "sem"]
    for name, e in reg["engines"].items():
        for osn in OSES:
            ins = e["install"][osn]
            assert ins["method"] == "degraded" and ins["reason"] or ins["cmd"], (name, osn)
            assert (e["bin"][osn] is None) == (ins["method"] == "degraded"), (name, osn)
        assert e["needs_net"] is False


# What no committed file may carry: an absolute path of some machine, a work tree of the previous internal version. The pattern is
# assembled from pieces so that this file does not contain what it looks for.
HOST_PATHS = re.compile(rb"/" + b"opt/|/" + b"home/[a-z]|/" + b"Users/[A-Za-z]|claude" + b"-toolkit|" + rb"\.tmp-|\bwt/")


def test_install_hints_are_generic_text_for_every_os_and_name_no_host_path(reg):
    for name, e in reg["engines"].items():
        for osn in OSES:
            cmd = engines.install_line(reg, name, osn)
            assert not re.search(r"sudo|/usr/local/lib|/" + r"opt/|root-owned|claude|deploy", cmd, re.I), (name, osn, cmd)
            # no download address and no version number: neither can be checked offline, the lock pins the bytes that were reviewed
            assert not re.search(r"https?://|@v?\d|==\d|-\d+\.\d", cmd), (name, osn, cmd)
    assert engines.install_line(reg, "vulture", "linux").startswith("apt install vulture")
    assert engines.install_line(reg, "ruff", "windows").startswith("pipx install ruff")
    assert engines.install_line(reg, "staticcheck", "darwin") == "go install honnef.co/go/tools/cmd/staticcheck@latest"
    assert engines.install_line(reg, "knip", "linux") == "npm install -g knip"
    assert engines.install_line(reg, "tflint", "linux").startswith("download a release build of tflint for your OS")
    assert engines.install_line(reg, "nope") == "nope: not in the engines registry"


def test_no_fixture_and_no_data_file_carries_a_host_path():
    """The committed SCIP indexes are binary (grep -I skips them): read every byte of the fixtures and of the package data. An index
    written by a real indexer records the project root it ran in; the committed ones are rewritten to a neutral file:///repo/..."""
    here = Path(__file__).resolve().parent
    leaks = []
    for top in (here / "fixtures", Path(engines.__file__).resolve().parents[1]):
        for dp, dns, fns in os.walk(top):
            dns[:] = [d for d in dns if d != "__pycache__"]
            for f in fns:
                path = Path(dp) / f
                m = HOST_PATHS.search(path.read_bytes())
                if m:
                    leaks.append((str(path.relative_to(here.parent)), m.group().decode(errors="replace")))
    assert leaks == []
    roots = sorted((here / "fixtures" / "sem").rglob("index.scip"))
    assert len(roots) == 5 and all(b"file:///repo/" in p.read_bytes() for p in roots)  # the rewritten, neutral project roots


def test_an_engine_with_no_build_for_an_os_is_degraded_with_a_reason(reg):
    assert engines.degraded_reason(reg, "cflow", "windows") == "no build of this engine for this OS"
    assert engines.degraded_reason(reg, "cflow", "linux") is None and engines.degraded_reason(reg, "nope") is None
    assert engines.install_line(reg, "cflow", "windows").startswith("degraded: ")
    res = engines.find_engine(reg, None, "cflow", os_name="windows")
    assert res.status == "degraded" and res.path is None
    assert engines.not_run_here("cflow", res.detail) == "cflow: not run here (no build of this engine for this OS)"


def test_validate_registry_names_every_structural_problem(reg):
    bad = copy.deepcopy(reg)
    bad["schema"] = 2
    bad["engines"]["ruff"]["class"] = "gpl"
    bad["engines"]["ruff"]["needs_net"] = True
    del bad["engines"]["ruff"]["bin"]["darwin"]
    bad["engines"]["vulture"]["aliases"] = ["ruff"]
    bad["engines"]["knip"]["install"]["linux"] = {"method": "npm"}
    bad["languages"]["python"]["roles"]["bogus"] = [{"engine": "ruff"}]
    bad["languages"]["go"]["roles"]["audit"].append({"engine": "ghost"})
    got = "; ".join(engines.validate_registry(bad))
    for part in ("schema must be 1", "ruff: class must be", "needs_net must be false", "ruff: bin and install must name darwin",
                 "name ruff used by", "knip: install.linux needs a cmd", "python: unknown role bogus",
                 "go.audit: unknown engine ghost"):  # fmt: skip
        assert part in got, part


def test_load_registry_raises_for_a_registry_that_is_wrong(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"schema": 3, "engines": {}, "languages": {}}))
    with pytest.raises(ValueError, match="schema must be 1"):
        engines.load_registry(str(p))


def test_every_engine_of_a_language_role_is_in_the_registry_and_every_job_in_the_data_is_an_engine(reg):
    from dead_code_checker.graph import adapters, jobs

    assert jobs.validate(jobs.table(), reg, adapters.table()) == []
    for lang, spec in reg["languages"].items():
        for role, items in spec["roles"].items():
            assert role in engines.ROLES
            assert all(i["engine"] in reg["engines"] for i in items), (lang, role)


# ================================================================ finding an engine
@SH
def test_an_engine_in_the_engines_directory_is_found_without_a_lock(reg, bindir):
    make_engine(bindir, "vulture")
    res = engines.find_engine(reg, None, "vulture")
    assert res == engines.Res(str(bindir / "vulture"), "ok", "")


def test_a_missing_engine_says_where_it_looked(reg, bindir):
    res = engines.find_engine(reg, None, "vulture")
    assert res.status == "missing" and res.path is None and "engines directories or on PATH" in res.detail
    engines.configure(None, None, path=True)
    assert engines.find_engine(reg, None, "vulture", dirs=[]).detail == "not found on PATH"


@SH
def test_the_engines_directory_comes_before_path_and_the_environment_fills_what_is_not_given(
    reg, bindir, tmp_path, monkeypatch
):
    other = tmp_path / "other"
    make_engine(other, "ruff", stdout="from path")
    make_engine(bindir, "ruff", stdout="from dir")
    monkeypatch.setenv("PATH", str(other))
    engines.configure(None, str(bindir), path=True)
    assert engines.find_engine(reg, None, "ruff").path == str(bindir / "ruff")
    assert engines.search_dirs() == [str(bindir), str(other)]
    engines.configure(None, None, path=True)  # nothing configured: the environment decides
    monkeypatch.setenv(engines.ENV_DIR, str(bindir) + os.pathsep + str(tmp_path / "third"))
    assert engines.current().dirs == (str(bindir), str(tmp_path / "third"))
    engines.configure(None, str(other), path=False)  # the option beats the variable
    assert engines.current().dirs == (str(other),) and engines.search_dirs() == [str(other)]


@SH
def test_a_relative_entry_of_path_is_never_searched_and_never_handed_on(reg, bindir, tmp_path, monkeypatch):
    """`.` or `bin` on PATH is the working directory's, often the analysed repository: an engine planted there must not be found, and must
    not be reachable by what an engine starts either."""
    planted = tmp_path / "tree"
    make_engine(planted, "ruff", stdout="planted")
    real = tmp_path / "real"
    make_engine(real, "ruff", stdout="real")
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "../tree", str(real), "relative/dir"]))
    engines.configure(None, None, path=True)
    assert engines.path_dirs() == [str(real)] and engines.search_dirs() == [str(real)]
    assert engines.find_engine(reg, None, "ruff").path == str(real / "ruff")
    assert engines.safe_path() == str(real) and engines.host_env()["PATH"] == str(real)
    assert engines.named_candidates(["ruff"], [".", "../tree", ""]) == []  # a relative directory is skipped by the lookup itself
    assert engines.named_candidates(["ruff"], [str(planted)]) == [("ruff", str(planted / "ruff"))]  # an absolute one is a choice
    monkeypatch.setenv("PATH", ".")
    assert engines.search_dirs() == [] and engines.safe_path() == ""
    monkeypatch.delenv("PATH")
    assert engines.path_dirs() == [] and engines.safe_path() == os.defpath


@SH
def test_an_engines_directory_given_relative_is_made_absolute_where_the_user_stands(reg, bindir, tmp_path, monkeypatch):
    make_engine(tmp_path / "mine", "ruff", stdout="mine")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    engines.configure(None, "mine", path=True)
    assert engines.search_dirs() == [str(tmp_path / "mine")]
    assert engines.find_engine(reg, None, "ruff").path == str(tmp_path / "mine" / "ruff")


@SH
def test_a_lock_pins_the_bytes_of_an_engine(reg, bindir, tmp_path):
    exe = make_engine(bindir, "vulture", stdout="v1")
    engines.configure(lock_of(tmp_path, {"vulture": {"sha256": sha(exe)}}), str(bindir), path=False)
    lock = engines.cached_lock()
    assert engines.find_engine(reg, lock, "vulture") == engines.Res(str(exe), "ok", "")
    exe.write_text(exe.read_text() + "# changed\n")  # the same name, other bytes
    res = engines.find_engine(reg, lock, "vulture")
    assert res.status == "sha-mismatch" and res.path is None and sha(exe)[:12] in res.detail
    assert engines.find_engine(reg, lock, "vulture", verify=False).status == "ok"  # verify=False: no comparison


@SH
def test_with_a_lock_an_engine_it_does_not_list_is_untrusted(reg, bindir, tmp_path):
    exe = make_engine(bindir, "vulture")
    make_engine(bindir, "ruff")
    engines.configure(lock_of(tmp_path, {"vulture": {"sha256": sha(exe)}}), str(bindir), path=False)
    res = engines.find_engine(reg, engines.cached_lock(), "ruff")
    assert res.status == "untrusted" and "not listed in the lock" in res.detail
    assert engines.find_engine(reg, engines.cached_lock(), "knip").status == "missing"  # not found is not untrusted


@SH
def test_a_copy_earlier_on_the_path_does_not_hide_the_pinned_one(reg, bindir, tmp_path, monkeypatch):
    early, late = tmp_path / "early", tmp_path / "late"
    make_engine(early, "ruff", stdout="planted")
    pinned = make_engine(late, "ruff", stdout="the real one")
    monkeypatch.setenv("PATH", f"{early}{os.pathsep}{late}")
    engines.configure(lock_of(tmp_path, {"ruff": {"sha256": sha(pinned)}}), None, path=True)
    assert engines.find_engine(reg, engines.cached_lock(), "ruff").path == str(pinned)
    make_engine(late, "ruff", stdout="replaced")  # now nothing on the path has the pinned bytes
    engines._SHA_MEMO.clear()
    res = engines.find_engine(reg, engines.cached_lock(), "ruff")
    assert res.status == "sha-mismatch" and str(early / "ruff") in res.detail


def test_a_module_is_pinned_by_the_hash_of_its_tree(reg, bindir, tmp_path):
    mod = bindir / "PSScriptAnalyzer"  # a PowerShell module is a directory: the lock pins its whole tree
    (mod / "lib").mkdir(parents=True)
    (mod / "PSScriptAnalyzer.psd1").write_text("@{ ModuleVersion = '1.0' }\n")
    (mod / "lib" / "a.dll").write_bytes(b"\x00one")
    engines.configure(None, str(bindir), path=False)
    assert engines.find_engine(reg, None, "PSScriptAnalyzer", os_name="linux") == engines.Res(str(mod), "ok", "")
    pins = {"PSScriptAnalyzer": {"sha256": engines.tree_sha256(str(mod))}}
    lock = engines.load_lock(lock_of(tmp_path, pins))
    assert engines.find_engine(reg, lock, "PSScriptAnalyzer", os_name="linux") == engines.Res(str(mod), "ok", "")
    (mod / "lib" / "a.dll").write_bytes(b"\x00two")  # one file of the tree changed
    engines._SHA_MEMO.clear()
    res = engines.find_engine(reg, lock, "PSScriptAnalyzer", os_name="linux")
    assert res.status == "sha-mismatch" and res.path is None and "tree sha256" in res.detail
    assert engines.find_engine(reg, lock, "PSScriptAnalyzer", os_name="linux", verify=False).status == "ok"
    other = engines.load_lock(lock_of(tmp_path, {"ruff": {"sha256": "0" * 64}}, name="other.lock.json"))
    res = engines.find_engine(reg, other, "PSScriptAnalyzer", os_name="linux")
    assert res.status == "untrusted" and "not listed in the lock" in res.detail
    rows = engines.doctor(reg, lock, langs={"powershell"}, os_name="linux")
    row = next(r for r in rows if r["engine"] == "PSScriptAnalyzer")
    assert row["status"] == "sha-mismatch" and row["pinned"] is True  # doctor no longer says ok for a changed module
    gone = engines.find_engine(reg, lock, "PSScriptAnalyzer", os_name="linux", dirs=[str(tmp_path / "none")])
    assert gone.status == "missing"


@SH
def test_a_pin_may_name_an_alias_of_a_registered_engine(reg, bindir, tmp_path):
    exe = make_engine(bindir, "basedpyright")
    lock = {
        "schema": 1,
        "platforms": {engines.platform_key(): {"engines": {"bp": {"sha256": sha(exe), "alias_of": "basedpyright"}}}},
    }
    assert engines.lock_entry(lock, "basedpyright")["alias_of"] == "basedpyright"
    assert engines.find_engine(reg, lock, "basedpyright").status == "ok"


@SH
def test_the_names_of_the_go_tool_and_the_legacy_name(reg, bindir):
    make_script(bindir, "deadcode", 'echo "usage of deadcode: a PyPI tool" >&2; exit 2\n')
    accept = engines.accept_for("go-deadcode")
    assert engines.find_engine(reg, None, "go-deadcode", accept=accept).status == "missing"  # not the Go tool: refused
    make_script(bindir, "deadcode", 'echo "usage: deadcode [flags] package... (a Go tool)" >&2; exit 2\n')
    engines._GO_HELP.clear()
    assert engines.find_engine(reg, None, "go-deadcode", accept=accept).path == str(bindir / "deadcode")
    make_engine(bindir, "go-deadcode")  # the registered name wins and needs no look at its help
    assert engines.find_engine(reg, None, "go-deadcode", accept=accept).path == str(bindir / "go-deadcode")
    assert engines.accept_for("ruff") is None


PROBE_BODY = 'echo ran >> "{marker}"\necho "usage: deadcode [flags] package... (a Go tool)" >&2\nexit 2\n'


@SH
def test_a_lock_is_checked_before_the_go_check_starts_a_deadcode_file(reg, bindir, tmp_path):
    """The bare `deadcode` is told from the PyPI package by running `-h`: that must not start a file the lock does not pin."""
    marker = tmp_path / "marker"
    stub = make_script(bindir, "deadcode", PROBE_BODY.format(marker=marker))
    accept = engines.accept_for("go-deadcode")
    plat = engines.platform_key()
    engines._GO_HELP.clear()
    wrong = lock_of(tmp_path, {"go-deadcode": {"sha256": "0" * 64}}, plat, "wrong.json")
    res = engines.find_engine(reg, engines.load_lock(wrong), "go-deadcode", accept=accept)
    assert res.status == "sha-mismatch" and not marker.exists()
    unlisted = lock_of(tmp_path, {"ruff": {"sha256": "1" * 64}}, plat, "unlisted.json")
    res = engines.find_engine(reg, engines.load_lock(unlisted), "go-deadcode", accept=accept)
    assert res.status == "untrusted" and not marker.exists()
    # the same through plan() and doctor(), which pass the check themselves
    for lock in (engines.load_lock(wrong), engines.load_lock(unlisted)):
        plan = engines.plan(reg, lock, "audit", langs={"go"}, dirs=[str(bindir)])
        assert [u.engine for u in plan.units if u.engine == "go-deadcode"] == []
        rows = {r["engine"]: r for r in engines.doctor(reg, lock, langs={"go"}, dirs=[str(bindir)])}
        assert rows["go-deadcode"]["status"] in ("sha-mismatch", "untrusted")
    assert not marker.exists()
    # the pinned bytes are started once, and only then
    pinned = lock_of(tmp_path, {"go-deadcode": {"sha256": sha(stub)}}, plat, "pinned.json")
    res = engines.find_engine(reg, engines.load_lock(pinned), "go-deadcode", accept=accept)
    assert res.status == "ok" and res.path == str(stub) and marker.read_text() == "ran\n"


@SH
def test_a_pinned_deadcode_that_does_not_say_go_is_not_the_go_tool(reg, bindir, tmp_path):
    stub = make_script(bindir, "deadcode", 'echo "usage of deadcode: a PyPI tool" >&2; exit 2\n')
    engines._GO_HELP.clear()
    lock = engines.load_lock(lock_of(tmp_path, {"go-deadcode": {"sha256": sha(stub)}}, engines.platform_key()))
    res = engines.find_engine(reg, lock, "go-deadcode", accept=engines.accept_for("go-deadcode"))
    assert res.status == "missing" and "pinned bytes" in res.detail


@SH
def test_a_copy_without_the_pin_earlier_on_the_path_is_not_started_and_does_not_hide_the_pinned_one(reg, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    marker = tmp_path / "marker"
    make_script(first, "deadcode", PROBE_BODY.format(marker=marker))
    pinned = make_script(second, "deadcode", PROBE_BODY.format(marker=tmp_path / "other"))
    engines._GO_HELP.clear()
    lock = engines.load_lock(lock_of(tmp_path, {"go-deadcode": {"sha256": sha(pinned)}}, engines.platform_key()))
    res = engines.find_engine(
        reg, lock, "go-deadcode", dirs=[str(first), str(second)], accept=engines.accept_for("go-deadcode")
    )
    assert res.status == "ok" and res.path == str(pinned) and not marker.exists()


def test_exe_names_follow_the_os(reg):
    assert engines.exe_names(reg, "ruff", "windows") == ("ruff.exe",) and engines.exe_names(reg, "ruff") == ("ruff",)
    assert engines.exe_names(reg, "go-deadcode", "linux") == ("go-deadcode", "deadcode")
    assert engines.exe_names(reg, "cflow", "windows") == () and engines.exe_names(reg, "nope") == ()


def test_on_windows_a_bare_name_gets_the_usual_extensions(tmp_path):
    d = tmp_path / "w"
    make_script(d, "tool.cmd", "")
    assert engines.candidates(["tool"], [str(d)], os_name="windows") == [str(d / "tool.cmd")]
    assert engines.candidates(["tool.cmd"], [str(d)], os_name="windows") == [str(d / "tool.cmd")]
    assert engines.candidates(["tool"], [str(d)], os_name="linux") == []


# ================================================================ the lock
def test_no_lock_is_none_and_a_lock_that_was_asked_for_and_cannot_be_read_is_an_error(tmp_path):
    assert engines.load_lock() is None and engines.cached_lock() is None
    with pytest.raises(ValueError, match="cannot be read"):
        engines.load_lock(str(tmp_path / "absent.json"))
    engines.configure(str(tmp_path / "absent.json"), None)
    with pytest.raises(ValueError, match="cannot be read"):
        engines.cached_lock()


@pytest.mark.parametrize(
    "text, why",
    [
        ("{not json", "not valid JSON"),
        ('{"schema": 2, "platforms": {}}', "schema must be 1"),
        ("[1]", "schema must be 1"),
        ('{"schema": 1}', "platforms must be an object"),
        ('{"schema": 1, "platforms": {"linux-x86_64": []}}', "engines must be an object"),
        (
            '{"schema": 1, "platforms": {"linux-x86_64": {"engines": {"ruff": {"sha256": "abc"}}}}}',
            "sha256 must be 64 hex",
        ),
        ('{"schema": 1, "platforms": {"linux-x86_64": {"engines": {"ruff": "x"}}}}', "sha256 must be 64 hex"),
    ],
)
def test_a_malformed_lock_is_refused(tmp_path, text, why):
    p = tmp_path / "l.json"
    p.write_text(text)
    with pytest.raises(ValueError, match=why):
        engines.load_lock(str(p))


def test_make_lock_keeps_the_other_platforms_and_write_lock_is_atomic_and_sorted(tmp_path):
    base = engines.make_lock({"a": {"sha256": "1" * 64}}, "windows-x86_64")
    lock = engines.make_lock(
        {"z": {"sha256": "2" * 64, "version": "1"}, "b": {"sha256": "3" * 64}}, "linux-x86_64", base
    )
    assert list(lock["platforms"]) == ["linux-x86_64", "windows-x86_64"]
    assert list(lock["platforms"]["linux-x86_64"]["engines"]) == ["b", "z"]
    p = tmp_path / "sub.lock.json"
    (tmp_path).mkdir(exist_ok=True)
    engines.write_lock(str(p), lock)
    assert engines.check_lock(json.loads(p.read_text()), str(p)) == lock
    assert [x.name for x in tmp_path.iterdir() if x.name.startswith(".lock-")] == []
    assert (
        engines.lock_engines(lock, "windows-x86_64") == {"a": {"sha256": "1" * 64}}
        and engines.lock_engines(lock, "x") == {}
    )


def test_the_lock_is_read_again_when_the_file_changes(tmp_path):
    p = tmp_path / "l.json"
    engines.write_lock(str(p), engines.make_lock({"ruff": {"sha256": "1" * 64}}))
    engines.configure(str(p), None)
    assert engines.lock_entry(engines.cached_lock(), "ruff")["sha256"] == "1" * 64
    engines.write_lock(str(p), engines.make_lock({"ruff": {"sha256": "2" * 64, "version": "9"}}))
    assert engines.lock_entry(engines.cached_lock(), "ruff")["sha256"] == "2" * 64


def test_the_lock_comes_from_the_environment_when_no_option_gives_it(tmp_path, monkeypatch):
    p = lock_of(tmp_path, {"ruff": {"sha256": "1" * 64}})
    monkeypatch.setenv(engines.ENV_LOCK, p)
    assert engines.current().lock == p and engines.cached_lock() is not None
    engines.configure("/elsewhere.json", None)
    assert engines.current().lock == "/elsewhere.json"


def test_sha256_helpers(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"abc")
    assert engines.sha256_file(str(f)) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    f.write_bytes(b"abd")  # same name and size, other bytes: the memo is keyed by the stat, so it follows
    assert engines.sha256_file(str(f)) != "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    d = tmp_path / "tree"
    (d / "a").mkdir(parents=True)
    (d / "a" / "x").write_text("1")
    (d / "y").write_text("2")
    h1 = engines.tree_sha256(str(d))
    assert engines.tree_sha256(str(d)) == h1 and engines.tree_sha256(str(f)) == engines.sha256_file(str(f))
    (d / "a" / "x").write_text("3")
    assert engines.tree_sha256(str(d)) != h1


# ================================================================ versions
@SH
def test_a_version_is_read_from_the_engine_by_its_own_command_without_a_network(reg, bindir):
    make_engine(bindir, "vulture", version="vulture 2.14")
    spec = reg["engines"]["vulture"]
    path = engines.find_engine(reg, None, "vulture").path
    assert engines.probe_version(spec, path) == "2.14"
    make_engine(bindir, "ruff", version="ruff 0.17.4 (abc 2026-01-01)")
    assert engines.probe_version(reg["engines"]["ruff"], str(bindir / "ruff")) == "0.17.4"
    make_script(bindir, "knip", "exit 3\n")
    assert engines.probe_version(reg["engines"]["knip"], str(bindir / "knip")) is None  # nothing matches
    assert engines.probe_version({}, str(bindir / "knip")) is None  # no version command
    assert engines.probe_version(reg["engines"]["knip"], str(bindir / "gone")) is None  # cannot be started


def test_parse_version_uses_the_registry_pattern_when_there_is_one(reg):
    spec = reg["engines"]["go-deadcode"]
    assert engines.parse_version(spec, "x\tmod\tgolang.org/x/tools\tv0.52.0\th1:abc") == "v0.52.0"
    assert engines.parse_version({}, "tool version 3.4.5-rc1 built") == "3.4.5-rc1"
    assert engines.parse_version({}, "no number here") is None


def test_an_engine_gets_the_path_and_the_toolchain_variables_and_nothing_else_of_the_environment(tmp_path, monkeypatch):
    home = tmp_path / "h"
    (home / ".rustup").mkdir(parents=True)
    for k, v in {
        "HOME": str(home),
        "GOMODCACHE": "/poisoned",
        "GOPROXY": "https://x",
        "HTTPS_PROXY": "http://p",
        "RUSTUP_TOOLCHAIN": "stable",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RUSTUP_HOME", raising=False)
    env = engines.host_env()
    assert set(env) == {"PATH", "RUSTUP_TOOLCHAIN", "RUSTUP_HOME"} and env["RUSTUP_HOME"] == str(home / ".rustup")
    monkeypatch.setenv("RUSTUP_HOME", str(tmp_path / "elsewhere"))
    assert "RUSTUP_HOME" not in engines.host_env()  # named but not there: nothing is invented
    (tmp_path / "elsewhere").mkdir()
    assert engines.host_env()["RUSTUP_HOME"] == str(tmp_path / "elsewhere")


# ================================================================ doctor, plan, lock rows
@SH
def test_doctor_lists_engines_with_status_install_hint_and_pins(reg, bindir, tmp_path):
    ruff, vult = make_engine(bindir, "ruff"), make_engine(bindir, "vulture")
    engines.configure(lock_of(tmp_path, {"ruff": {"sha256": sha(ruff)}}), str(bindir), path=False)
    rows = engines.doctor(reg, engines.cached_lock(), langs={"python"})
    by = {r["engine"]: r for r in rows}
    assert by["ruff"]["status"] == "ok" and by["ruff"]["pinned"] is True and by["ruff"]["install"] == ""
    assert by["vulture"]["status"] == "untrusted" and by["vulture"]["pinned"] is False
    assert by["deptry"]["status"] == "missing" and by["deptry"]["install"].startswith("pipx install deptry")
    assert by["ruff"]["langs"] == ["python"] and "audit" in by["ruff"]["roles"] and by["ruff"]["path"] == str(ruff)
    assert [r["status"] == "ok" for r in rows] == sorted(
        r["status"] == "ok" for r in rows
    )  # the ones to act on come first
    assert (
        vult.exists() and engines.doctor(reg, None, langs={"python"})[0]["pinned"] is None
    )  # no lock: not pinned, not unpinned


def test_doctor_leaves_out_what_the_project_does_not_need(reg, bindir):
    both = {r["engine"] for r in engines.doctor(reg, None, langs={"go"}, files=["go.mod", "main.go"])}
    assert {"go-deadcode", "staticcheck"} <= both
    none = {r["engine"] for r in engines.doctor(reg, None, langs={"go"}, files=["main.go"])}
    assert "go-deadcode" not in none and "staticcheck" not in none  # needs go.mod: not applicable is not missing
    assert engines.needs_met(["go.mod"], ["a/go.mod"]) and not engines.needs_met(["Cargo.toml", "go.mod"], ["go.mod"])


@SH
def test_doctor_can_ask_each_engine_for_its_version(reg, bindir):
    make_engine(bindir, "vulture", version="vulture 2.14")
    rows = engines.doctor(reg, None, langs={"python"}, versions=True)
    assert next(r for r in rows if r["engine"] == "vulture")["version"] == "2.14"
    assert "version" not in next(r for r in rows if r["engine"] == "deptry")


def test_the_exit_code_fails_only_for_a_strict_run(reg):
    rows = engines.doctor(reg, None, langs={"python"})
    assert engines.exit_code(rows) == 0 and engines.exit_code(rows, strict=True) == 2
    assert engines.exit_code([{"status": "degraded"}, {"status": "ok"}], strict=True) == 0


@SH
def test_plan_sorts_the_engines_of_a_role_into_usable_missing_and_waived(reg, bindir):
    make_engine(bindir, "vulture")
    make_engine(bindir, "ruff")
    plan = engines.plan(reg, None, "audit", langs={"python"})
    assert sorted(u.engine for u in plan.units) == ["ruff", "vulture"]
    assert {m["engine"] for m in plan.missing} == {"py-deadcode", "deptry", "pyan3"}
    assert all(m["detail"].startswith("missing: ") and m["install"] for m in plan.missing) and plan.degraded == []
    assert next(u for u in plan.units if u.engine == "ruff").needs_build is False
    cfg = SimpleNamespace(
        is_waived=lambda code, path=None, engine=None: code == "dc.engine-incomplete" and engine == "deptry"
    )
    waived = engines.plan(reg, None, "audit", langs={"python"}, cfg=cfg)
    assert [d["engine"] for d in waived.degraded] == ["deptry"] and "waived" in waived.degraded[0]["detail"]
    assert "deptry" not in {m["engine"] for m in waived.missing}
    assert engines.waived_by(None, "x") is False and engines.waived_by(object(), "x") is False


def test_plan_puts_a_build_engine_and_its_weight_into_the_unit_and_skips_what_needs_a_marker(reg, tmp_path):
    d = tmp_path / "b"
    make_script(d, "cargo", "")
    plan = engines.plan(reg, None, "audit", langs={"rust"}, dirs=[str(d)], files=["Cargo.toml", "src/lib.rs"])
    cargo = next(u for u in plan.units if u.engine == "cargo")
    assert cargo.needs_build is True and cargo.weight == 4 and cargo.path == str(d / "cargo")
    bare = engines.plan(reg, None, "audit", langs={"rust"}, dirs=[str(d)], files=["src/lib.rs"])
    assert "cargo" not in {u.engine for u in bare.units} | {m["engine"] for m in bare.missing}


def test_plan_names_an_engine_with_no_build_for_the_os_as_degraded(reg):
    plan = engines.plan(reg, None, "audit", langs={"cpp"}, os_name="windows", dirs=[])
    assert "cflow" in {d["engine"] for d in plan.degraded}
    assert next(d for d in plan.degraded if d["engine"] == "cflow")["detail"].startswith("degraded: ")


@SH
def test_lock_rows_pin_every_engine_found_with_its_version(reg, bindir):
    exe = make_engine(bindir, "vulture", version="vulture 2.14")
    make_engine(bindir, "ruff", version="ruff 0.17.4")
    rows = engines.lock_rows(reg)
    assert rows["vulture"] == {"sha256": sha(exe), "version": "2.14"} and rows["ruff"]["version"] == "0.17.4"
    assert set(rows) == {"vulture", "ruff"}
    assert "version" not in engines.lock_rows(reg, versions=False)["vulture"]


def test_project_files_and_languages(tmp_path):
    r = tmp_path / "p"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "a.py").write_text("x = 1\n")
    (r / "go.mod").write_text("module x\n")
    (r / "node_modules").mkdir()
    (r / "node_modules" / "z.js").write_text("")
    files = engines.project_files(str(r))  # not a git repository: a bounded walk that skips node_modules
    assert sorted(files) == ["go.mod", "pkg/a.py"]
    assert engines.project_langs(engines.load_registry(), files) == {"*", "python", "go"}
    assert engines.project_files(str(r), limit=1) and len(engines.project_files(str(r), limit=1)) == 1


# ================================================================ processes
@SH
def test_run_group_kills_the_whole_group_on_a_timeout(tmp_path):
    marker = tmp_path / "grandchild"
    script = make_script(tmp_path, "slow", f"(sleep 30; echo late > {marker}) &\nsleep 30\n")
    import subprocess
    import time

    t = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        proc.run_group([str(script)], str(tmp_path), 1.0, {"PATH": os.environ["PATH"]})
    assert time.monotonic() - t < 10 and not marker.exists() and proc._LIVE == set()


@SH
def test_run_group_returns_the_output_and_the_exit_code(tmp_path):
    script = make_script(tmp_path, "t", "echo out; echo err >&2; exit 3\n")
    assert proc.run_group([str(script)], str(tmp_path), 10, {"PATH": os.environ["PATH"]}) == (3, b"out\n", b"err\n")
    with pytest.raises(OSError):
        proc.run_group([str(tmp_path / "absent")], str(tmp_path), 5)


def test_netless_prefix_is_a_list_that_is_probed_once(monkeypatch):
    proc._NETLESS.clear()
    first = proc.netless_prefix()
    assert isinstance(first, list) and proc.netless_prefix() == first and len(proc._NETLESS) == 1
    proc._NETLESS.clear()
    monkeypatch.setattr(proc.safepath, "which", lambda name: "")
    assert proc.netless_prefix() == []
    proc._NETLESS.clear()


# ================================================================ the commands
def run_cli(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "a.py").write_text("def f():\n    return 1\n")
    (r / "go.mod").write_text("module x\n")
    return r


@SH
def test_doctor_prints_the_engines_of_the_repository_with_status_and_hints(repo, bindir, capsys):
    make_engine(bindir, "vulture", version="vulture 2.14")
    rc, out, err = run_cli(
        capsys, "doctor", "--root", str(repo), "--engines-dir", str(bindir), "--no-path", "--versions"
    )
    assert rc == 0 and err.count("\n") <= 1
    assert "no lock: engines are used as found" in out and f"engines dir: {bindir}" in out
    line = next(x for x in out.splitlines() if x.lstrip().startswith("vulture "))
    assert " ok " in line and str(bindir / "vulture") in line and "2.14" in line
    deptry = next(x for x in out.splitlines() if x.lstrip().startswith("deptry "))
    assert " missing " in deptry and "pipx install deptry" in deptry
    assert any(x.lstrip().startswith("go-deadcode ") for x in out.splitlines())  # the repository holds Go
    assert not any(x.lstrip().startswith("knip ") for x in out.splitlines())  # and no JavaScript
    assert "sandbox:" in out and "go cache:" in out and "(does not exist)" in out
    rc, out, _ = run_cli(capsys, "doctor", "--root", str(repo), "--engines-dir", str(bindir), "--no-path", "--all")
    assert any(x.lstrip().startswith("knip ") for x in out.splitlines())


@SH
def test_doctor_json_carries_the_engines_the_lock_and_the_sandbox(repo, bindir, tmp_path, capsys):
    exe = make_engine(bindir, "vulture")
    lock = lock_of(tmp_path, {"vulture": {"sha256": sha(exe)}})
    rc, out, _ = run_cli(
        capsys, "doctor", "--root", str(repo), "--engines-dir", str(bindir), "--no-path", "--lock", lock, "--json"
    )
    doc = json.loads(out)
    e = doc["engines"]
    assert (
        rc == 0
        and e["lock"] == lock
        and e["dirs"] == [str(bindir)]
        and e["sandbox"]["kind"] in ("none", "bwrap", "netless")
    )
    assert e["go_cache"]["usable"] is False and e["go_cache"]["dir"].endswith(os.path.join("caches", "gomod"))
    row = next(r for r in e["rows"] if r["engine"] == "vulture")
    assert row["status"] == "ok" and row["pinned"] is True


@SH
def test_doctor_says_when_an_engine_does_not_match_the_lock_and_strict_fails(repo, bindir, tmp_path, capsys):
    exe = make_engine(bindir, "vulture")
    lock = lock_of(tmp_path, {"vulture": {"sha256": "0" * 64}})
    rc, out, _ = run_cli(
        capsys, "doctor", "--root", str(repo), "--engines-dir", str(bindir), "--no-path", "--lock", lock
    )
    assert rc == 0 and "sha-mismatch" in out and exe.exists()
    rc, _, _ = run_cli(
        capsys, "doctor", "--root", str(repo), "--engines-dir", str(bindir), "--no-path", "--lock", lock, "--strict"
    )
    assert rc == 2


def test_doctor_with_a_lock_that_cannot_be_read_is_a_usage_error(repo, tmp_path, capsys):
    rc, out, err = run_cli(capsys, "doctor", "--root", str(repo), "--lock", str(tmp_path / "absent.json"))
    assert rc == 2 and out == "" and "cannot be read" in err


@SH
def test_graph_lock_writes_the_pins_and_a_later_doctor_trusts_exactly_those_bytes(repo, bindir, tmp_path, capsys):
    exe = make_engine(bindir, "vulture", version="vulture 2.14")
    out_lock = tmp_path / "pins.json"
    rc, out, _ = run_cli(capsys, "lock", "--out", str(out_lock), "--engines-dir", str(bindir), "--no-path")
    assert rc == 0 and "1 engines pinned for" in out
    lock = json.loads(out_lock.read_text())
    assert lock["platforms"][engines.platform_key()]["engines"]["vulture"] == {"sha256": sha(exe), "version": "2.14"}
    args = ("doctor", "--root", str(repo), "--engines-dir", str(bindir), "--no-path", "--lock", str(out_lock), "--json")
    row = lambda o: next(r for r in json.loads(o)["engines"]["rows"] if r["engine"] == "vulture")  # noqa: E731
    assert row(run_cli(capsys, *args)[1])["status"] == "ok"
    exe.write_text(exe.read_text() + "# tampered\n")
    assert row(run_cli(capsys, *args)[1])["status"] == "sha-mismatch"
    # a second run for another platform keeps the first
    rc, _, _ = run_cli(
        capsys, "lock", "--out", str(out_lock), "--lock", str(out_lock), "--engines-dir", str(bindir), "--no-path"
    )
    assert rc == 0 and engines.platform_key() in json.loads(out_lock.read_text())["platforms"]


def test_graph_lock_reports_an_unwritable_target(tmp_path, capsys):
    rc, _, err = run_cli(capsys, "lock", "--out", str(tmp_path / "nodir" / "l.json"))
    assert rc == 2 and "cannot write" in err
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    rc, _, err = run_cli(capsys, "lock", "--out", str(tmp_path / "l.json"), "--lock", str(bad))
    assert rc == 2 and "not valid JSON" in err


def test_the_modules_import_no_network_library():
    import ast

    banned = {"socket", "ssl", "http", "requests", "urllib3", "ftplib", "smtplib", "xmlrpc", "asyncio"}
    for name in (
        "engines",
        "proc",
        "sandbox",
        "jobs",
        "gocache",
        "semuse",
        "semcache",
        "semindex",
        "steps",
        "adapters",
    ):
        tree = ast.parse((Path(engines.__file__).parent / f"{name}.py").read_text())
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and not n.level}
        assert not [m for m in mods if m.split(".")[0] in banned or m == "urllib.request"], name


def test_sandbox_module_is_loaded_for_the_doctor_row():
    assert sandbox.detect().kind in ("none", "bwrap", "netless") and sys.version_info >= (3, 11)
