"""graph.steps: the engines and the language servers behind the two seams of the branch level (levels.Steps), end to end on a throwaway
repository: a dead claim of the diff is a suspect (I) until an engine or a server corroborates it (W), a reference a server finds
makes the symbol alive, and everything that cannot run is named and costs only its own result.

python3 -m pytest -q tests/test_graph_steps.py

The code under test is written in the toy language of tests/graph_support.py, registered here as "python" (so the registry's Python
engines and servers apply to it; one statement per line: `def name`, `ref name`, `root kind qual`). The engines are stub scripts that
print what vulture / go-deadcode printed (tests/graph_fake_engines.py); the language server is the scripted one of
tests/graph_fake_lsp.py. The PATH of the host is never searched, no host engine runs, no test touches the network.
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from graph_fake_engines import SH, block_network, isolate, make_engine, make_script
from graph_support import DCREF_LANG, RefExtractor, RefRoots, commit_all, git, new_repo, write

from dead_code_checker.graph import contracts, engines as dce, langs, levels, sem, steps, verdict

pytestmark = [
    SH,
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.usefixtures("hermetic_git", "data_root"),
]

FAKE_LSP = Path(__file__).resolve().parent / "graph_fake_lsp.py"
APP = "def helper\ndef run\nref helper\ndef main\nref run\nroot main main\n"
CHAIN = "def link_a\nref link_b\ndef link_b\nref link_c\ndef link_c\n"
ROOTS = "main file:lib/app.py\n"


class PyToy(RefExtractor):
    langs = ("python",)


@pytest.fixture(autouse=True)
def world(tmp_path, monkeypatch):
    """The toy language as "python", the stub engines directory (empty) and no host engine, lock or sandbox."""
    table = json.loads(Path(langs.DATA).read_text(encoding="utf-8"))
    table["langs"]["python"] = {**table["langs"]["python"], "extractor": None, "ext": [".py"], "shebang": []}
    p = tmp_path / "langs.json"
    p.write_text(json.dumps(table), encoding="utf-8")
    monkeypatch.setattr(langs, "DATA", str(p))
    monkeypatch.setitem(contracts._EXTRACTORS, "python", PyToy())
    monkeypatch.setitem(contracts._ROOTS, "dcroots", RefRoots())
    block_network(monkeypatch)
    monkeypatch.delenv(steps.LSP_OFF, raising=False)
    assert DCREF_LANG  # the shared toy is imported for its extractor classes
    yield from isolate(monkeypatch, tmp_path, tmp_path / "bin")


@pytest.fixture
def bindir(tmp_path):
    return tmp_path / "bin"


@pytest.fixture
def repo(tmp_path):
    return new_repo(tmp_path, {"lib/app.py": APP, "lib/other.py": "def spare_one\n", "run.dcroots": ROOTS})


def vulture(bindir, *lines, rc=3):
    return make_engine(bindir, "vulture", stdout="\n".join(lines), rc=rc, version="vulture 2.14")


def branch(repo, opts=steps.Opts(lsp=False), **kw):
    return levels.branch(str(repo), "HEAD", steps=steps.default(opts), **kw)


def claims(fs):
    """(claim, level) of the dead-symbol claims: a dc.suspect counts as the claim it replaced."""
    dead = verdict.table()["codes"]
    return sorted((verdict.claim(f), f.level) for f in fs if verdict.claim(f) in dead)


def by_name(fs, path="lib/chain.py"):
    return {f.msg.split("`")[1]: (verdict.claim(f), f.level) for f in fs if f.path == path and "`" in f.msg}


# ================================================================ the engines
def test_without_engines_or_servers_every_dead_claim_is_a_hidden_suspect(repo):
    write(repo, "lib/chain.py", CHAIN)
    rep = branch(repo)
    assert claims(rep.findings) == [("dc.dead-cluster", "I")] * 3 and not [f for f in rep.findings if f.level == "E"]
    inc = [f for f in rep.findings if f.code == "dc.engine-incomplete"]
    assert any(f.msg.startswith("vulture: not run (missing") and "install:" in f.msg for f in inc)  # said, not skipped


def test_an_engine_that_names_the_symbol_exactly_corroborates_it_and_only_it(repo, bindir):
    write(repo, "lib/chain.py", CHAIN)
    vulture(bindir, "lib/chain.py:1: unused function 'link_a' (100% confidence)")
    rep = branch(repo)
    got = by_name(rep.findings)
    assert got["link_a"] == ("dc.dead-cluster", "W") and got["link_b"] == ("dc.dead-cluster", "I")
    why = next(f for f in rep.findings if "`link_a`" in f.msg).why
    assert "corroborated: vulture@2.14 unused-function at lib/chain.py:1" in why
    assert not any(f.code == "dc.engine-incomplete" and f.msg.startswith("vulture") for f in rep.findings)
    assert [s for s in rep.meta["stages"] if s == "native"] == ["native"]


def test_an_engine_hit_below_the_confidence_the_table_asks_for_corroborates_nothing(repo, bindir):
    write(repo, "lib/chain.py", CHAIN)
    vulture(bindir, "lib/chain.py:1: unused function 'link_a' (60% confidence)")
    assert claims(branch(repo).findings) == [("dc.dead-cluster", "I")] * 3


def test_an_engine_whose_run_failed_is_a_w_and_the_claims_stay_suspects(repo, bindir):
    write(repo, "lib/chain.py", CHAIN)
    vulture(
        bindir, "lib/chain.py:1: unused function 'link_a' (100% confidence)", rc=2
    )  # 2: not a healthy exit of vulture
    fs = branch(repo).findings
    assert any(
        f.code == "dc.engine-output" and f.level == "W" and "vulture" in f.msg and "exit code 2" in f.msg for f in fs
    )
    assert claims(fs) == [("dc.dead-cluster", "I")] * 3


def test_no_engine_runs_when_nothing_dead_is_left_unproven(repo, bindir, tmp_path):
    marker = tmp_path / "ran"
    make_script(bindir, "vulture", f"echo ran > {marker}\n")
    write(repo, "lib/chain.py", CHAIN)

    class Proven(verdict.Proof):
        def text(self, key, path):
            return "test: corroborated"

    assert claims(branch(repo, proof=Proven()).findings) == [("dc.dead-cluster", "W")] * 3
    commit_all(repo, "chain")
    write(repo, "lib/app.py", APP + "ref helper\n")  # a changed file with nothing dead in it
    assert claims(branch(repo).findings) == [] and not marker.exists()


def test_engines_are_asked_only_about_the_languages_of_the_candidates(repo, bindir, tmp_path):
    marker = tmp_path / "ran"
    make_script(bindir, "go-deadcode", f"echo ran > {marker}\necho '[]'\n")
    make_script(bindir, "staticcheck", f"echo ran > {marker}\n")
    write(repo, "svc/go.mod", "module x\n")
    write(repo, "lib/chain.py", CHAIN)
    branch(repo)
    assert not marker.exists()  # the dead claims are Python: a Go engine of the host has nothing to say about them


def test_an_engine_that_cannot_run_costs_its_own_result_and_a_w_never_an_e(repo, bindir, monkeypatch):
    write(repo, "lib/chain.py", CHAIN)
    vulture(bindir, "lib/chain.py:1: unused function 'link_a' (100% confidence)")

    def boom(*a, **k):
        raise RuntimeError("the engine layer failed")

    monkeypatch.setattr(steps.jobs, "gather", boom)
    fs = branch(repo).findings
    assert any(f.code == "dc.check-failed" and f.level == "W" and "the engine layer failed" in f.msg for f in fs)
    assert claims(fs) == [("dc.dead-cluster", "I")] * 3 and not [f for f in fs if f.level == "E"]


def test_the_engines_can_be_switched_off(repo, bindir, tmp_path):
    marker = tmp_path / "ran"
    make_script(bindir, "vulture", f"echo ran > {marker}\n")
    write(repo, "lib/chain.py", CHAIN)
    assert claims(branch(repo, steps.Opts(lsp=False, native=False)).findings) == [("dc.dead-cluster", "I")] * 3
    assert not marker.exists()


def test_a_lock_decides_which_engine_runs(repo, bindir, tmp_path):
    write(repo, "lib/chain.py", CHAIN)
    exe = vulture(bindir, "lib/chain.py:1: unused function 'link_a' (100% confidence)")
    lock = tmp_path / "l.json"
    dce.write_lock(str(lock), dce.make_lock({"vulture": {"sha256": dce.sha256_file(str(exe)), "version": "2.14"}}))
    dce.configure(str(lock), str(bindir), path=False)
    assert by_name(branch(repo).findings)["link_a"] == ("dc.dead-cluster", "W")  # the pinned bytes run
    exe.write_text(exe.read_text() + "# changed\n")
    dce._SHA_MEMO.clear()
    fs = branch(repo).findings
    assert by_name(fs)["link_b"] == ("dc.dead-cluster", "I")  # other bytes: not run, what nothing else vouches for stays a suspect
    assert any(f.code == "dc.engine-incomplete" and "sha-mismatch" in f.msg for f in fs)


# ================================================================ the language servers
def lsp(tmp_path, monkeypatch, refs=None, **script):
    p = tmp_path / "lsp.json"
    p.write_text(json.dumps({"refs": refs or {}, **script}))
    spec = sem.Spec("fake-ls", (sys.executable, str(FAKE_LSP), str(p)), (), None, True)
    monkeypatch.setattr(sem, "servers", lambda fams, **kw: {f: spec for f in fams})


ON = steps.Opts(lsp=True, native=False)


def test_a_server_that_finished_indexing_and_found_no_reference_corroborates(repo, tmp_path, monkeypatch):
    write(repo, "lib/chain.py", CHAIN)
    lsp(tmp_path, monkeypatch)
    fs = branch(repo, ON).findings
    assert [(f.code, f.level) for f in fs if f.path == "lib/chain.py"] == [("dc.dead-cluster", "W")] * 3
    assert (
        "corroborated: fake-ls (language server) found no reference"
        in next(f for f in fs if f.path == "lib/chain.py").why
    )


def test_a_reference_the_server_finds_makes_the_symbol_alive(repo, tmp_path, monkeypatch):
    write(repo, "lib/chain.py", CHAIN)
    lsp(tmp_path, monkeypatch, refs={"lib/chain.py:0:4": [["lib/app.py", 4]]})  # main mentions link_a
    assert claims(branch(repo, ON).findings) == []


def test_a_server_that_is_not_installed_is_one_i_line_and_the_claims_stay(repo, monkeypatch):
    write(repo, "lib/chain.py", CHAIN)
    monkeypatch.setattr(sem, "servers", lambda fams, **kw: {f: "basedpyright is missing" for f in fams})
    fs = branch(repo, ON).findings
    (i,) = [f for f in fs if f.code == "dc.sem-lsp-skipped"]
    assert i.level == "I" and "python (basedpyright is missing)" in i.msg
    assert claims(fs) == [("dc.dead-cluster", "I")] * 3


def test_the_real_resolver_names_the_missing_server_and_how_to_install_it(repo):
    write(repo, "lib/chain.py", CHAIN)
    fs = branch(repo, ON).findings  # the isolated engines directory has no basedpyright
    (i,) = [f for f in fs if f.code == "dc.sem-lsp-skipped"]
    assert "basedpyright is missing" in i.msg and "pipx install basedpyright" in i.msg


def test_a_server_that_does_not_answer_is_a_w_never_an_e(repo, tmp_path, monkeypatch):
    write(repo, "lib/chain.py", CHAIN)
    lsp(tmp_path, monkeypatch, crash_after=0, hang=True)
    real = sem.arbitrate
    monkeypatch.setattr(sem, "arbitrate", lambda *a, **k: real(*a, **{**k, "budget_s": 2, "grace_s": 0.2}))
    fs = branch(repo, ON).findings
    assert [f for f in fs if f.level == "E"] == [] and any(
        f.code.startswith("dc.sem-lsp") and f.level == "W" for f in fs
    )
    assert claims(fs) == [("dc.dead-cluster", "I")] * 3


def test_the_language_server_step_has_the_budget_of_the_data_file(repo, tmp_path, monkeypatch):
    write(repo, "lib/chain.py", CHAIN)
    lsp(tmp_path, monkeypatch)
    seen = {}
    real = sem.arbitrate
    monkeypatch.setattr(sem, "arbitrate", lambda *a, **k: seen.update(k) or real(*a, **k))
    branch(repo, ON)
    assert seen["budget_s"] == min(float(sem.config()["lsp"]["budget_s"]), 60.0) and "max_queries" not in seen


def test_the_servers_run_on_a_private_copy_the_work_tree_is_never_theirs(repo, tmp_path, monkeypatch):
    write(repo, "lib/chain.py", CHAIN)
    lsp(tmp_path, monkeypatch)
    seen, real = {}, sem.arbitrate

    def arbitrate(graph, keys, root, text_of, specs, **kw):
        seen["root"] = root
        seen["files"] = sorted(os.listdir(os.path.join(root, "lib")))
        write(root, ".cache/index.bin", "what a server leaves behind")  # a server writes into its root
        return real(graph, keys, root, text_of, specs, **kw)

    monkeypatch.setattr(sem, "arbitrate", arbitrate)
    fs = branch(repo, ON).findings
    assert seen["root"] != str(repo) and seen["files"] == [
        "app.py",
        "chain.py",
        "other.py",
    ]  # the new, untracked file is in the copy
    assert (
        not os.path.exists(seen["root"]) and not (repo / ".cache").exists()
    )  # the copy is gone, the tree is as it was
    assert git(repo, "status", "--porcelain") == "?? lib/chain.py"
    assert (
        claims(fs) == [("dc.dead-cluster", "W")] * 3
        and "in a copy of the files of the tree" in next(f for f in fs if f.path == "lib/chain.py").why
    )


def test_a_private_copy_that_cannot_be_made_is_a_w_and_the_claims_stay_suspects(repo, tmp_path, monkeypatch):
    write(repo, "lib/chain.py", CHAIN)
    lsp(tmp_path, monkeypatch)
    monkeypatch.setattr(sem, "arbitrate", lambda *a, **k: pytest.fail("no copy, no server"))
    monkeypatch.setattr(steps.shutil, "copy", lambda *a, **k: (_ for _ in ()).throw(OSError("No space left on device")))
    fs = branch(repo, ON).findings
    assert any(f.code == "dc.check-failed" and f.level == "W" and "No space left" in f.msg for f in fs)
    assert claims(fs) == [("dc.dead-cluster", "I")] * 3 and [f for f in fs if f.level == "E"] == []


def test_the_switch_that_turns_the_servers_off_is_one_i_line(repo, monkeypatch):
    write(repo, "lib/chain.py", CHAIN)
    monkeypatch.setattr(sem, "servers", lambda *a, **k: pytest.fail("switched off"))
    off = branch(repo, steps.Opts(lsp=False)).findings
    monkeypatch.setenv(steps.LSP_OFF, "off")
    env = branch(repo, ON).findings
    for fs, why in ((off, "language servers are switched off"), (env, "DEAD_CODE_CHECKER_LSP=off")):
        (i,) = [f for f in fs if f.code == "dc.sem-lsp-skipped"]
        assert i.level == "I" and why in i.msg
        assert claims(fs) == [("dc.dead-cluster", "I")] * 3


WALLS = "def walls._Wall.http_open\ndef walls._Wall.close_it\n"


def test_a_handler_that_a_library_calls_by_name_is_never_corroborated_by_the_silence_of_a_server(
    repo, tmp_path, monkeypatch
):
    write(repo, "lib/walls.py", WALLS)  # urllib calls http_open by name: no reference in the tree
    lsp(tmp_path, monkeypatch)
    got = by_name(branch(repo, ON).findings, "lib/walls.py")
    assert got["walls._Wall.http_open"][1] == "I"  # blind: its silence proves nothing
    assert got["walls._Wall.close_it"][1] == "W"  # an ordinary method of the same class is corroborated


def test_a_dead_cluster_with_one_member_that_nothing_vouches_for_is_suspects_all_of_it(repo, tmp_path, monkeypatch):
    # link_a -> link_b -> hook (a handler of a library: its http_open is called by name): one cluster, one member nobody can vouch for
    write(
        repo,
        "lib/chain.py",
        "def link_a\nref link_b\ndef link_b\nref walls._Wall.http_open\ndef walls._Wall.http_open\n",
    )
    lsp(tmp_path, monkeypatch)
    got = by_name(branch(repo, ON).findings)
    assert len(got) == 3 and all(lv == "I" for _, lv in got.values()), got  # not one W: the hook keeps the rest alive


def test_engines_and_servers_work_together_the_engine_goes_first(repo, bindir, tmp_path, monkeypatch):
    write(repo, "lib/chain.py", CHAIN)
    vulture(bindir, "lib/chain.py:1: unused function 'link_a' (100% confidence)")
    lsp(tmp_path, monkeypatch, refs={"lib/chain.py:2:4": [["lib/app.py", 4]]})  # link_c is mentioned somewhere: alive
    fs = branch(repo, steps.Opts(lsp=True, native=True)).findings
    got = by_name(fs)
    assert "link_c" not in got or got["link_c"][0] != "dc.dead-cluster"  # the reference the server found made it alive
    assert got["link_a"][1] in ("W", "I")


# ================================================================ the cache: `graph index`, and what a branch run keeps
def toggled(bindir, on, count, hit="lib/chain.py:1: unused function 'link_a' (100% confidence)"):
    """A vulture that prints `hit` while the file `on` exists and otherwise only counts that it was asked."""
    return make_script(
        bindir,
        "vulture",
        f'[ "$1" = "--version" ] && {{ echo "vulture 2.14"; exit 0; }}\n'
        f'if [ -e {on} ]; then echo "{hit}"; exit 3; fi\necho asked >> {count}\nexit 0\n',
    )


def test_graph_index_fills_the_cache_and_the_branch_level_reads_it_without_asking_the_engine(
    repo, bindir, tmp_path, capsys
):
    from dead_code_checker.graph import cli

    on, count = tmp_path / "on", tmp_path / "asked"
    on.write_text("")
    toggled(bindir, on, count)
    write(repo, "lib/chain.py", CHAIN)
    argv = [
        "index",
        "--root",
        str(repo),
        "--engines-dir",
        str(bindir),
        "--no-path",
        "--no-sandbox",
        "--jobs",
        "1",
        "--only",
        "vulture",
    ]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "refreshed 1, failed 0" in out and "1 tool result(s)" in out
    on.unlink()  # from now on the engine would say nothing: only the cache can corroborate link_a
    fs = branch(repo).findings
    assert by_name(fs)["link_a"] == ("dc.dead-cluster", "W") and not count.exists()
    assert (
        "corroborated: vulture@2.14 unused-function at lib/chain.py:1" in next(f for f in fs if "`link_a`" in f.msg).why
    )
    assert cli.main(argv) == 0 and "skipped 1, refreshed 0" in capsys.readouterr().out  # nothing changed: not run again
    assert not count.exists()
    write(
        repo, "lib/chain.py", CHAIN + "def link_d\n"
    )  # the files changed: the item is stale, the engine is asked again
    assert cli.main(argv) == 0 and "refreshed 1" in capsys.readouterr().out and count.read_text() == "asked\n"


def test_what_a_branch_run_learned_from_an_engine_is_kept_for_the_next_run_of_the_same_files(repo, bindir, tmp_path):
    on, count = tmp_path / "on", tmp_path / "asked"
    on.write_text("")
    toggled(bindir, on, count)
    write(repo, "lib/chain.py", CHAIN)
    assert by_name(branch(repo).findings)["link_a"] == ("dc.dead-cluster", "W")  # the engine ran and said so
    on.unlink()
    assert by_name(branch(repo).findings)["link_a"] == ("dc.dead-cluster", "W")  # the second run has it from the cache
    assert not count.exists()
    write(repo, "lib/chain.py", CHAIN + "def link_d\n")
    assert by_name(branch(repo).findings)["link_a"] == (
        "dc.dead-cluster",
        "I",
    )  # new files, the engine says nothing now
    assert count.read_text() == "asked\n"
