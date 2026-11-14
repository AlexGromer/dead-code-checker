"""graph.levels: the branch level on seeded canaries, the budgets, the determinism of the graph for 1/2/8 workers, incremental ==
cold, the steps where the semantic layer and the engines plug in, the findings for a language that has no extractor or whose
extractor cannot run, and graph.extract.batch.

python3 -m pytest -q tests/test_graph_levels.py

The code under test is written in the toy language dcref (tests/graph_support.py): the extractors of the real languages arrive with
the language packs. Every test works in a throwaway repository under tmp_path with DEAD_CODE_CHECKER_HOME pointing at tmp_path: no
real data directory is read or written.
"""

import json
import random
import shutil

import pytest
from graph_support import commit_all, git, new_repo, write

from dead_code_checker.graph import contracts, levels, paths, reach, report, roots, safety, verdict
from dead_code_checker.graph import link as dclink
from dead_code_checker.graph.extract import batch
from dead_code_checker.graph.model import Finding, canonical

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.usefixtures("hermetic_git", "data_root", "dcref"),
]


class Proven(verdict.Proof):
    """A proof that corroborates every dead claim: the tests of the graph's own verdicts and levels run with it, the corroboration
    rule itself (what is a W, what a dc.suspect) is tested in tests/test_graph_verdict.py."""

    def text(self, key, path):
        return "test: corroborated"


PROVEN = Proven()
APP = "def helper\ndef run\nref helper\ndef main\nref run\nroot main main\n"
CHAIN = "def link_a\nref link_b\ndef link_b\nref link_c\ndef link_c\n"
UNIT = "main file:bin/gone\n"  # a registration that points at a file that is not there


def claim(f):
    """The code of a finding; a dc.suspect stands for the dead-symbol claim it replaced."""
    return verdict.was(f) or f.code


def codes(fs, level=None):
    return sorted((f.code, f.path) for f in fs if level in (None, f.level))


@pytest.fixture
def repo(tmp_path):
    return new_repo(
        tmp_path,
        {"lib/app.dcref": APP, "lib/other.dcref": "def spare_one\n", "run.dcroots": "main file:lib/app.dcref\n"},
    )


def quals(fs, code):
    return sorted(f.msg.split("`")[1] for f in fs if f.code == code)


# ================================================================ branch level: the canaries
def test_dead_chain_is_a_cluster_and_every_finding_has_a_why(repo):
    write(repo, "lib/chain.dcref", CHAIN)
    rep = levels.branch(str(repo), "HEAD", proof=PROVEN)
    got = [f for f in rep.findings if f.code == "dc.dead-cluster"]
    assert quals(rep.findings, "dc.dead-cluster") == ["link_a", "link_b", "link_c"]
    assert all(f.level == "W" and f.path == "lib/chain.dcref" and f.line for f in got)
    assert all("dead cluster of 3" in f.msg for f in got)
    assert rep.findings and all(f.why.strip() for f in rep.findings)
    assert "no incoming edge" in next(f for f in got if "link_a" in f.msg).why  # why-dead of the head of the chain
    assert rep.meta["scope"] == "branch" and rep.meta["unfinished"] == [] and rep.meta["base"] == "HEAD"


def test_without_a_proof_the_dead_claims_are_hidden_suspects(repo):
    write(repo, "lib/chain.dcref", CHAIN)
    rep = levels.branch(str(repo), "HEAD")
    assert {(f.level, f.code) for f in rep.findings} == {("I", "dc.suspect")} and len(rep.findings) == 3
    assert all(claim(f) == "dc.dead-cluster" and verdict.NOT_PROVEN in f.why for f in rep.findings)
    assert "suspects hidden" in report.to_text(rep.findings, rep.meta) and "dc.suspect" not in report.to_text(
        rep.findings
    )


def test_registration_to_a_missing_file_is_E_root_dangling(repo):
    write(repo, "lib/new.dcroots", UNIT)
    fs = levels.branch(str(repo), "HEAD").findings
    assert [(f.level, f.path, f.code) for f in fs if f.level == "E"] == [("E", "lib/new.dcroots", "dc.root-dangling")]
    assert "bin/gone" in fs[0].msg and all(f.why for f in fs)


def test_a_dangling_registration_that_the_diff_does_not_touch_is_not_reported(repo):
    write(repo, "lib/old.dcroots", UNIT)
    commit_all(repo, "old registration")
    write(repo, "lib/app.dcref", APP + "def extra\nref helper\n")
    assert [f for f in levels.branch(str(repo), "HEAD").findings if f.code == "dc.root-dangling"] == []


def test_the_branch_that_removes_the_last_caller_orphans_the_callee(repo):
    write(repo, "lib/app.dcref", "def helper\ndef run\ndef main\nref run\nroot main main\n")
    fs = levels.branch(str(repo), "HEAD").findings
    o = [f for f in fs if f.code == "dc.orphaned-by-branch"]
    assert [(f.level, f.path, f.msg.split("`")[1]) for f in o] == [("W", "lib/app.dcref", "helper")]
    assert "run" in o[0].msg and "lost its last caller" in o[0].msg
    assert "before the diff" in o[0].why and o[0].why.startswith("dead")


def test_a_symbol_that_was_already_dead_is_not_blamed_on_the_diff(repo):
    write(repo, "lib/old.dcref", "def unused_before\ndef other\n")
    commit_all(repo, "dead already")
    write(repo, "lib/old.dcref", "def unused_before\nref other\ndef other\n")  # touched, still dead
    assert not [
        f for f in levels.branch(str(repo), "HEAD").findings if f.code.startswith("dc.") and f.path == "lib/old.dcref"
    ]


def test_new_dead_function_is_introduced_dead_and_an_ignore_marker_silences_it(repo):
    write(repo, "lib/new.dcref", "def fresh\n\n# deadcode: ignore\ndef silenced\n")
    fs = levels.branch(str(repo), "HEAD", proof=PROVEN).findings
    assert [(f.code, f.msg.split("`")[1]) for f in fs if f.code == "dc.introduced-dead"] == [
        ("dc.introduced-dead", "fresh")
    ]
    assert all(f.level == "W" for f in fs)  # a dead claim is never an E


def test_the_ignore_file_marker_and_the_dunders_and_a_tree_without_roots(tmp_path, repo):
    write(repo, "lib/skip.dcref", "# deadcode: ignore-file\ndef zzz_dead\n")
    write(repo, "lib/mod.dcref", "def __getattr__\ndef really_dead\n")
    got = {
        (f.path, f.msg.split("`")[1])
        for f in levels.branch(str(repo), "HEAD", proof=PROVEN).findings
        if f.code.startswith("dc.")
    }
    assert got == {("lib/mod.dcref", "really_dead")}  # no finding for the ignored file, none for the module hook
    bare = new_repo(tmp_path, {}, name="bare", commit=False)
    write(bare, "lib.dcref", "def a\nref b\ndef b\n")
    rep = levels.branch(str(bare), None, proof=PROVEN)
    assert rep.meta.get("no_roots") is True and not [f for f in rep.findings if f.code.startswith("dc.dead")]


def test_unchanged_files_report_nothing(repo):
    assert levels.branch(str(repo), "HEAD").findings == ()


def test_a_waive_of_the_configuration_drops_a_graph_finding(repo):
    write(repo, "lib/chain.dcref", CHAIN)
    write(
        repo,
        ".dead-code-checker.toml",
        '[[graph.waive]]\ncode = "dc.dead-cluster"\npath_glob = "lib/chain.dcref"\nreason = "seeded"\nuntil = 2999-01-01\n',
    )
    assert not [f for f in levels.branch(str(repo), "HEAD", proof=PROVEN).findings if f.code == "dc.dead-cluster"]
    write(
        repo,
        ".dead-code-checker.toml",
        '[[graph.waive]]\ncode = "dc.dead"\npath_glob = "lib/chain.dcref"\nreason = "the claim a suspect stands for"\nuntil = 2999-01-01\n',
    )
    rep = levels.branch(str(repo), "HEAD")  # a suspect of dead-cluster is not covered by a waive of dc.dead
    assert {claim(f) for f in rep.findings} == {"dc.dead-cluster"}
    write(
        repo,
        ".dead-code-checker.toml",
        '[[graph.waive]]\ncode = "dc.dead-cluster"\npath_glob = "lib/*"\nreason = "seeded"\nuntil = 2999-01-01\n',
    )
    assert (
        levels.branch(str(repo), "HEAD").findings == ()
    )  # a suspect is silenced by the waive of the claim it stands for


def test_a_disabled_check_says_so_and_runs_nothing(repo):
    write(repo, "lib/chain.dcref", CHAIN)
    write(
        repo,
        ".dead-code-checker.toml",
        '[graph]\nenabled = false\ndisabled_reason = "migration"\ndisabled_until = 2999-01-01\n',
    )
    rep = levels.branch(str(repo), "HEAD", proof=PROVEN)
    assert [(f.level, f.code) for f in rep.findings] == [("W", "dc.disabled")] and rep.meta["disabled"] is True
    write(repo, ".dead-code-checker.toml", "[graph]\nbogus = 1\n")
    rep = levels.branch(str(repo), "HEAD", proof=PROVEN)
    assert ("E", "dc.bad-config") in {(f.level, f.code) for f in rep.findings} and "dc.dead-cluster" in {
        f.code for f in rep.findings
    }


def test_no_base_says_so(tmp_path):
    r = new_repo(tmp_path, {"a.dcref": "def f\n"}, name="nb", commit=False)
    rep = levels.branch(str(r), None)
    assert (
        "dc.no-base" in {f.code for f in rep.findings} and all(f.why for f in rep.findings) and rep.meta["base"] is None
    )


def test_no_resolvable_base_downgrades_pre_existing_E_to_W_and_never_blocks(repo):
    """A branch called trunk (no upstream, origin/HEAD, main, master): the whole tree is the diff, every dangling registration is
    older than any diff, so one W dc.no-base and no E."""
    write(repo, "lib/old.dcroots", UNIT)
    commit_all(repo, "pre-existing dangling registration")
    git(repo, "branch", "-m", "trunk")
    rep = levels.branch(str(repo), None)
    fs = rep.findings
    assert [f.code for f in fs if f.code == "dc.no-base"] == ["dc.no-base"] and rep.meta["base"] is None
    assert not [f for f in fs if f.level == "E"]
    assert [(f.level, f.code) for f in fs if f.code == "dc.root-dangling"] == [("W", "dc.root-dangling")]
    assert all("pre-existing" in f.msg for f in fs if f.code == "dc.root-dangling")
    git(repo, "branch", "-m", "main")  # a resolvable base: no dc.no-base, and the diff against it is empty
    assert "dc.no-base" not in {f.code for f in levels.branch(str(repo), None).findings}


# ================================================================ the findings about the run
def test_a_failure_of_the_checker_is_a_w_never_an_e(repo, monkeypatch):
    monkeypatch.setattr(levels.vcs, "default_base", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git exploded")))
    rep = levels.branch(str(repo), None)
    assert [(f.level, f.code, f.path) for f in rep.findings] == [("W", "dc.check-failed", "(dead-code-checker)")]
    assert "branch check failed (RuntimeError: git exploded)" in rep.findings[0].msg and rep.findings[0].why


def test_run_skips_every_stage_after_the_budget_and_names_them():
    t = [0.0]
    run = levels.Run("branch", 10, clock=lambda: t[0])
    assert run.stage("tree", lambda: "x") == "x"
    t[0] = 11
    assert run.stage("extract", lambda: pytest.fail("a stage after the budget must not start")) is None
    assert run.stage("link", lambda: pytest.fail("not even a later cheap one")) is None
    f = run.finding()
    assert (
        (f.level, f.code) == ("W", "dc.branch-budget")
        and "not done: extract, link" in f.msg
        and "finished: tree" in f.msg
    )
    assert run.unfinished() == ["extract", "link"] and [x.render() for x in run.findings()] == [f.render()]
    assert levels.Run("branch", 1).finding() is None
    r2 = levels.Run("repo", 0)
    r2.stage("tree", lambda: 1)
    assert r2.finding().code == "dc.budget"


def test_a_stage_that_raises_costs_its_result_and_halts_the_later_ones():
    run = levels.Run("branch", 60)
    assert run.stage("tree", lambda: 1) == 1

    def boom():
        raise ValueError("stage failed\nwith a second line")

    assert run.stage("extract", boom) is None and run.stage("roots", lambda: pytest.fail("halted")) is None
    f = run.failure()
    assert (f.level, f.code) == (
        "W",
        "dc.check-failed",
    ) and "stage extract: ValueError: stage failed with a second line" in f.msg
    assert "not run: roots" in f.msg and "finished: tree" in f.msg and run.unfinished() == ["extract", "roots"]
    assert levels.Run("branch", 1).failure() is None


def test_budget_spent_after_reach_keeps_the_finished_results(repo, monkeypatch):
    write(repo, "lib/new.dcroots", UNIT)
    write(repo, "lib/chain.dcref", CHAIN)
    real, spent = reach.analyze, []

    def slow(*a, **k):  # the budget runs out while the reach stage is running
        out = real(*a, **k)
        spent.append(1)
        return out

    monkeypatch.setattr(reach, "analyze", slow)
    monkeypatch.setattr(levels.Budget, "over", lambda self: bool(spent))
    rep = levels.branch(str(repo), "HEAD", budget_s=60, proof=PROVEN)
    cs = {f.code for f in rep.findings}
    assert "dc.root-dangling" in cs  # a finished stage: kept
    assert "dc.branch-budget" in cs and "dc.dead-cluster" not in cs
    assert rep.meta["unfinished"] == ["diff"] and {"tree", "persist", "reach"} <= set(rep.meta["stages"])


def test_the_graph_is_stored_when_the_budget_runs_out_after_the_link(repo, monkeypatch):
    spent, real = [], dclink.link

    def link_then_spend(*a, **k):
        out = real(*a, **k)
        spent.append(1)
        return out

    over = levels.Budget.over
    monkeypatch.setattr(dclink, "link", link_then_spend)
    monkeypatch.setattr(levels.Budget, "over", lambda self: bool(spent))
    write(repo, "lib/chain.dcref", CHAIN)
    rep = levels.branch(str(repo), "HEAD", budget_s=60)
    assert (
        "persist" in rep.meta["stages"]
        and "reach" in rep.meta["unfinished"]
        and "dc.branch-budget" in {f.code for f in rep.findings}
    )
    monkeypatch.setattr(levels.Budget, "over", over)
    monkeypatch.setattr(dclink, "link", lambda *a, **k: pytest.fail("the second run must read the stored graph"))
    st, _ = levels.open_store(str(repo))
    assert levels.build(str(repo), st).graph.nodes  # warm: the cache is no longer cold forever
    st.close()


def test_a_branch_run_with_an_empty_diff_still_warms_the_cache(repo):
    assert levels.branch(str(repo), "HEAD").findings == ()
    st, _ = levels.open_store(str(repo))
    assert st.get_meta("graph_key") and st.get_graph().nodes
    st.close()


def test_a_budget_that_is_gone_before_the_was_it_dead_check_reports_no_blame(repo, monkeypatch):
    real, over = levels._base, levels.Budget.over

    def base_then_spend(*a, **k):  # the budget runs out between the base link and the reverse search on it
        out = real(*a, **k)
        monkeypatch.setattr(levels.Budget, "over", lambda self: True)
        return out

    monkeypatch.setattr(levels, "_base", base_then_spend)
    monkeypatch.setattr(reach, "touched", lambda *a, **k: pytest.fail("the check was to be skipped"))
    write(repo, "lib/app.dcref", APP + "def extra\n")
    rep = levels.branch(str(repo), "HEAD", budget_s=60, proof=PROVEN)
    monkeypatch.setattr(levels.Budget, "over", over)
    assert not [f for f in rep.findings if f.code == "dc.introduced-dead"]
    assert any("was-it-dead-before" in f.msg for f in rep.findings if f.code == "dc.branch-budget")


def test_store_unavailable_is_a_warning_and_the_checks_still_run(repo, monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the data directory should be")
    monkeypatch.setenv("DEAD_CODE_CHECKER_HOME", str(blocker))
    write(repo, "lib/chain.dcref", CHAIN)
    fs = levels.branch(str(repo), "HEAD", proof=PROVEN).findings
    assert "dc.store-unavailable" in {f.code for f in fs} and "dc.dead-cluster" in {f.code for f in fs}
    st, note = levels.open_store(str(repo))
    assert st is None and note.level == "W" and note.why


def test_the_store_lives_in_the_data_directory_and_never_in_the_repository(repo, data_root):
    write(repo, "lib/chain.dcref", CHAIN)
    levels.branch(str(repo), "HEAD", proof=PROVEN)
    lay = paths.resolve(repo)
    assert lay.store.is_file() and str(lay.store).startswith(str(data_root))
    assert not [
        p
        for p in repo.rglob("*")
        if p.suffix == ".sqlite" or "dead-code" in p.name and p.name != ".dead-code-checker.toml"
    ]
    assert git(repo, "status", "--porcelain").count("sqlite") == 0


# ================================================================ a language without an extractor, or whose extractor cannot run
def test_language_without_an_extractor_is_a_warning_not_a_silent_skip(repo):
    write(repo, "svc/App.csproj", "<Project />\n")  # msbuild: a config language that only the root sources read
    assert "msbuild" in levels.build(str(repo), None).unsupported
    rep = levels.branch(str(repo), "HEAD")
    assert "msbuild" in rep.meta["unsupported"]
    w = [f for f in rep.findings if f.code == "dc.unsupported-language"]
    assert len(w) == 1 and w[0].level == "W" and w[0].path == "svc/App.csproj" and "no msbuild extractor" in w[0].msg
    assert "no extractor: msbuild" in report.to_text(rep.findings, rep.meta)  # the text path shows it too
    assert "Languages without an extractor (not in the graph): msbuild" in report.to_md(rep.findings, rep.meta)
    write(repo, "svc/Two.csproj", "<Project />\n")
    assert (
        "(+1 more)"
        in next(f for f in levels.branch(str(repo), "HEAD").findings if f.code == "dc.unsupported-language").msg
    )


def test_an_unsupported_file_that_is_not_in_the_diff_is_not_reported(repo):
    write(repo, "svc/main.go", "package main\n")
    commit_all(repo, "go")
    rep = levels.branch(str(repo), "HEAD")  # nothing changed against HEAD
    assert not [f for f in rep.findings if f.code == "dc.unsupported-language"]


def test_an_extractor_that_does_not_support_a_language_yet_is_not_supported(repo, monkeypatch):
    monkeypatch.setattr(contracts._EXTRACTORS["dcref"], "supports", lambda lang: False, raising=False)
    assert "dcref" in levels.build(str(repo), None).unsupported


def test_an_extractor_that_cannot_run_is_e_engine_missing_with_the_why_of_its_probe(repo, monkeypatch):
    gone = Finding("E", "(grammar of dcref)", 0, "dc.engine-missing", "the grammar of dcref is not installed")
    monkeypatch.setattr(contracts._EXTRACTORS["dcref"], "probe", lambda lang, cfg=None: gone, raising=False)
    write(repo, "lib/new.dcref", "def fresh\n")
    b = levels.build(str(repo), None)
    assert set(b.missing) == {"dcref"} and b.missing["dcref"].why and "left out of the graph" in b.missing["dcref"].why
    assert {n.kind for n in b.graph.nodes if n.path.endswith(".dcref")} == {
        "file"
    }  # no definitions, only the file nodes
    fs = levels.branch(str(repo), "HEAD").findings
    assert [(f.level, f.path, f.code) for f in fs if f.code == "dc.engine-missing"] == [
        ("E", "lib/new.dcref", "dc.engine-missing")
    ]
    write(repo, "lib/new.dcref", "def fresh\n")
    commit_all(repo, "that file is no more part of the diff")
    write(repo, "README.md", "# changed\n")
    inc = [f for f in levels.branch(str(repo), "HEAD").findings if f.code == "dc.graph-incomplete"]
    assert [(f.level, f.path) for f in inc] == [
        ("W", "(dead-code-checker)")
    ] and "dcref files are not in the graph" in inc[0].msg


def test_an_extractor_module_that_cannot_be_imported_is_missing_not_unsupported(repo, monkeypatch):
    monkeypatch.setattr(contracts, "defined", lambda lang, langs=None: lang == "dcref")
    monkeypatch.delitem(contracts._EXTRACTORS, "dcref")
    b = levels.build(str(repo), None)
    assert set(b.missing) == {"dcref"} and "dc.engine-missing" == b.missing["dcref"].code and not b.unsupported


# ================================================================ determinism, incremental
def synth(repo, files=70, per=14):
    """A repository large enough to be split into several link chunks: every file calls into the next one."""
    for i in range(files):
        lines = [f"def f{i}_{j}\nref f{i}_{j + 1}" for j in range(per - 1)]
        lines.append(f"def f{i}_{per - 1}\nimport m{(i + 1) % files} f{(i + 1) % files}_0\nref f{(i + 1) % files}_0")
        write(repo, f"pkg/m{i}.dcref", "\n".join(lines) + "\n")


def test_graph_is_byte_identical_for_1_2_8_workers(repo, monkeypatch):
    synth(repo)
    monkeypatch.setattr(
        dclink.link, "__kwdefaults__", {**dclink.link.__kwdefaults__, "min_chunk": 40}
    )  # force several chunks
    digs = {w: levels.build(str(repo), None, workers=w).graph.digest() for w in (1, 2, 8)}
    assert len(set(digs.values())) == 1, digs


def test_incremental_equals_cold_after_random_edits(repo):
    synth(repo, files=18, per=6)
    r = str(repo)
    st, _ = levels.open_store(r)
    levels.build(r, st)
    rng = random.Random(20261111)
    for rnd in range(5):
        for _ in range(rng.randint(1, 3)):
            i = rng.randrange(18)
            p = repo / f"pkg/m{i}.dcref"
            if not p.exists():
                continue
            txt = p.read_text()
            how = rng.choice(["add", "drop", "rewire", "delete"])
            if how == "add":
                txt += f"def extra{rnd}_{i}\nref f{i}_0\n"
            elif how == "drop":
                txt = txt.replace(f"def f{i}_1\nref f{i}_2\n", f"def f{i}_1\n")
            elif how == "rewire":
                txt = txt.replace(f"ref f{i}_3\n", f"ref f{(i + 5) % 18}_0\n")
            else:
                p.unlink()
                continue
            p.write_text(txt)
        warm_graph = levels.build(r, st)
        cold_graph = levels.build(r, None)
        assert warm_graph.graph.digest() == cold_graph.graph.digest(), rnd
        assert warm_graph.key == cold_graph.key
    st.close()


def test_a_stored_graph_of_the_same_inputs_is_read_not_linked(repo, monkeypatch):
    r = str(repo)
    st, _ = levels.open_store(r)
    b = levels.build(r, st)
    levels.persist(st, b)
    monkeypatch.setattr(
        dclink, "link", lambda *a, **k: pytest.fail("the graph of unchanged inputs must come from the store")
    )
    b2 = levels.build(r, st)
    assert b2.graph.digest() == b.graph.digest() and b2.key == b.key
    write(repo, "lib/app.dcref", APP + "def added\n")
    monkeypatch.undo()
    assert levels.build(r, st).key != b.key  # any changed shard changes the key
    st.close()


def test_persist_stores_snapshot_graph_and_findings_once(repo):
    r = str(repo)
    st, _ = levels.open_store(r)
    b = levels.build(r, st)
    levels.persist(st, b)
    assert st.get_meta("graph_key") == b.key and st.get_meta("graph_tree") == b.tree.id and st.get_graph() == b.graph
    assert st.get_snapshot(b.tree.id) == b.keys and st.trees() == [b.tree.id]
    n = st.shard_count()
    st.put_graph(canonical([]))  # a stored graph with this key is not written again
    levels.persist(st, b)
    assert not st.get_graph().nodes and st.shard_count() == n
    st.close()


def test_a_registration_target_that_no_extractor_reads_still_gets_its_node(repo):
    write(repo, "notes.txt", "plain text\n")
    write(repo, "lib/new.dcroots", "main file:notes.txt\n")
    b = levels.build(str(repo), None)
    assert "file:notes.txt" in {n.key for n in b.graph.nodes} and any(r.node == "file:notes.txt" for r in b.graph.roots)
    assert [f for f in b.findings if f.code == "dc.root-dangling"] == []
    assert reach.analyze(b.graph).verdict("file:notes.txt") == "alive"


def test_the_incremental_base_graph_equals_the_full_link_of_the_base_tree(repo):
    write(
        repo, "lib/app.dcref", "def helper\ndef run\ndef main\nref run\nroot main main\n"
    )  # run() stops calling helper()
    r = str(repo)
    b = levels.build(r, None)
    deltas = levels.branch_deltas(r, "HEAD")
    base = levels._base(b, deltas, None)
    inc = levels._base_graph(b, base)
    old = dict(b.facts())
    old["lib/app.dcref"] = batch.extract_many([("dcref", "lib/app.dcref", "0" * 40, APP)])[0][0].facts()
    full = dclink.link(old, workers=1).graph
    edge = lambda g: {(e.src, e.dst, e.kind, e.tier, e.site) for e in g.edges}  # noqa: E731
    assert [n.key for n in inc.nodes] == [n.key for n in full.nodes]
    assert edge(inc) == edge(full)
    full = canonical(full.nodes, full.edges, b.graph.roots, full.evid)  # the base graph keeps the head roots
    assert reach.analyze(inc).verdicts() == reach.analyze(full).verdicts()


# ================================================================ the build and its seams
def test_extractor_link_and_roots_agree_on_the_module_node_and_the_main_root(repo):
    b = levels.build(str(repo), None)
    assert [f.render() for f in b.findings] == []  # no E dc.bad-facts (a duplicate module node), no dangling root
    nodes = {n.key: n for n in b.graph.nodes}
    mains = [r for r in b.graph.roots if r.kind == "main" and r.node == "dcref:lib/app.dcref"]
    assert mains and all(r.node in nodes for r in b.graph.roots if r.node)
    res = reach.analyze(b.graph)
    assert res.verdict("dcref:lib/app.dcref#helper") == "alive"
    assert res.why_alive("dcref:lib/app.dcref#helper").root.kind == "main"
    assert b.cold == 2 and levels.build(str(repo), None).key == b.key  # the two dcref files; the roots file is no code


def test_a_class_registration_gives_a_selector_root_even_where_the_extractor_cannot_run(repo, no_tree_sitter):
    write(repo, "src/p/Impl.java", "class Impl {}\n")
    write(repo, "src/main/resources/META-INF/services/p.Plugin", "p.Impl\n")
    assert "java" in levels.build(str(repo), None).missing  # no tree-sitter: the class has no node to bind to
    rs = roots.build(str(repo))
    assert [(r.kind, r.selector) for r in rs.roots if r.kind == "classreg"] == [("classreg", "class:p.Impl")]


def test_test_files_are_test_only_never_dead(tmp_path):
    r = new_repo(
        tmp_path,
        {
            "lib/a.dcref": "def used\ndef only_in_tests\ndef never\nroot main used\n",
            "tests/test_a.dcref": "def test_x\nimport a only_in_tests\nref only_in_tests\ndef helper\n",
        },
        name="pt",
    )
    b = levels.build(str(r), None)
    v = {k.split(":", 1)[1]: x for k, x in reach.analyze(b.graph).verdicts().items()}
    assert v["tests/test_a.dcref#test_x"] == v["tests/test_a.dcref#helper"] == "test-only"
    assert (
        v["lib/a.dcref#only_in_tests"] == "test-only"
        and v["lib/a.dcref#used"] == "alive"
        and v["lib/a.dcref#never"] == "dead"
    )
    write(r, "tests/test_b.dcref", "def test_new\n")
    assert [
        f for f in levels.branch(str(r), "HEAD", proof=PROVEN).findings if f.code.startswith("dc.")
    ] == []  # a test is no dead code


def test_a_bad_fact_is_a_W_never_an_E():
    bad = [
        Finding("E", "a.py", 3, "dc.bad-facts", "ref kind 'x' is not an edge kind"),
        Finding("E", "a.py", 1, "dc.root-dangling", "points into the void"),
        Finding("W", "a.py", 2, "dc.bad-root-source", "cannot be read"),
        Finding("E", "b.py", 2, "dc.root-dangling", "another file"),
    ]
    got = levels._roots_touching(bad, {"a.py"})
    assert [(f.level, f.code) for f in got] == [
        ("W", "dc.bad-facts"),
        ("E", "dc.root-dangling"),
        ("W", "dc.bad-root-source"),
    ]


def test_match_keys_takes_a_key_a_suffix_or_a_fragment():
    keys = ["dcref:a.dcref", "dcref:a.dcref#run", "dcref:b.dcref#run", "dcref:b.dcref#runnable"]
    assert levels.match_keys(keys, "dcref:a.dcref#run") == ["dcref:a.dcref#run"]
    assert levels.match_keys(keys, "run") == ["dcref:a.dcref#run", "dcref:b.dcref#run"]
    assert levels.match_keys(keys, "b.dcref#run") == ["dcref:b.dcref#run"]
    assert levels.match_keys(keys, "runna") == ["dcref:b.dcref#runnable"] and levels.match_keys(keys, "zzz") == []


def test_why_explains_a_node_alive_or_dead_and_names_the_ambiguous_and_missing_ones(repo):
    write(repo, "lib/chain.dcref", CHAIN)
    assert levels.why(str(repo), "no_such_symbol_xyz") == "no node matches 'no_such_symbol_xyz'"
    assert "is ambiguous" in levels.why(str(repo), "link_")
    alive = levels.why(str(repo), "lib/app.dcref#helper")
    assert alive.startswith("dcref:lib/app.dcref#helper\nalive: main root") and "registered at lib/app.dcref:6" in alive
    dead = levels.why(str(repo), "link_a")
    assert (
        dead.splitlines()[0] == "dcref:lib/chain.dcref#link_a" and "no incoming edge" in dead and "dead-cluster" in dead
    )


# ================================================================ the steps: the semantic layer and the engines plug in here
def test_the_semantic_step_refines_the_graph_and_its_proof_corroborates(repo):
    write(repo, "lib/chain.dcref", CHAIN)
    calls = []

    def semantic(root, built, store, notes, deadline, clock):
        calls.append((root, deadline is not None, store is not None))
        notes.append(Finding("I", "(x)", 0, "dc.sem-note", "a note of the layer"))
        return built.graph, verdict.Proof(frozenset({"lib/chain.dcref"}))

    rep = levels.branch(str(repo), "HEAD", steps=levels.Steps(semantic=semantic))
    assert (
        calls == [(str(repo), True, True)]
        and "semantic" in rep.meta["stages"]
        or "semantic" in rep.meta.get("stages", ())
    )
    assert {(f.level, f.code) for f in rep.findings if f.code != "dc.sem-note"} == {("W", "dc.dead-cluster")}
    assert all("corroborated: S-tier" in f.why for f in rep.findings if f.code == "dc.dead-cluster")
    assert any(f.code == "dc.sem-note" for f in rep.findings)


def test_the_prove_step_runs_after_reach_for_the_dead_symbols_and_may_hand_back_a_proof(repo):
    write(repo, "lib/chain.dcref", CHAIN)
    seen = []

    def prove(root, built, res, deltas, cfg, run, notes, proof):
        seen.append(
            (
                sorted(d.path for d in deltas),
                res.verdict("dcref:lib/chain.dcref#link_a"),
                cfg.mode,
                levels.left(run) > 0,
            )
        )
        return (
            built,
            res,
            verdict.both(
                proof, verdict.Proof(nodes={"dcref:lib/chain.dcref#link_a": "engine@1 rule at lib/chain.dcref:1"})
            ),
        )

    rep = levels.branch(str(repo), "HEAD", steps=levels.Steps(prove=prove))
    assert seen == [(["lib/chain.dcref"], "dead-cluster", "closed_world", True)]
    by = {f.msg.split("`")[1] if f.code == "dc.dead-cluster" else claim(f): f.level for f in rep.findings}
    assert by["link_a"] == "W" and {f.code for f in rep.findings if f.msg.startswith("[")} == {
        "dc.suspect"
    }  # one proven, two suspects


def test_a_step_that_raises_costs_its_own_result_and_a_w_and_the_stages_after_it_still_run(repo):
    write(repo, "lib/chain.dcref", CHAIN)

    def semantic(*a):
        raise OSError("the cache is unreadable")

    rep = levels.branch(str(repo), "HEAD", proof=PROVEN, steps=levels.Steps(semantic=semantic))
    failed = [f for f in rep.findings if f.code == "dc.check-failed"]
    assert (
        len(failed) == 1
        and failed[0].level == "W"
        and "semantic check failed (OSError: the cache is unreadable)" in failed[0].msg
    )
    assert quals(rep.findings, "dc.dead-cluster") == ["link_a", "link_b", "link_c"] and rep.meta["unfinished"] == []


def test_soft_does_not_start_a_step_after_the_budget_and_names_it():
    t = [0.0]
    run, out = levels.Run("branch", 5, clock=lambda: t[0]), []
    assert levels.soft(run, "native", lambda: 7, out) == 7 and run.done == ["native"]
    t[0] = 6
    assert levels.soft(run, "lsp", lambda: pytest.fail("late"), out) is None and run.skipped == ["lsp"] and out == []
    assert levels.left(run) == pytest.approx(-1.0)


def test_analyzed_proof_applies_the_semantic_step_on_top_of_the_stored_graph(repo):
    seen = []

    def semantic(root, built, store, notes, deadline, clock):
        seen.append((deadline, store.get_meta("graph_key") == built.key))
        return built.graph, verdict.Proof(frozenset({"x.py"}))

    b, res, proof = levels.analyzed_proof(str(repo), steps=levels.Steps(semantic=semantic))
    assert (
        seen == [(None, True)]
        and proof.covered == {"x.py"}
        and res.verdict("dcref:lib/app.dcref#main") == "alive"
        and b.graph.nodes
    )
    b2, res2 = levels.analyzed(str(repo))
    assert b2.graph == b.graph and res2.verdicts() == res.verdicts()


# ================================================================ what the viewer and the ledger are handed
def test_the_sink_gets_the_graph_the_marks_and_the_suspects_and_what_a_marker_or_a_waive_silenced(repo):
    write(repo, "lib/chain.dcref", CHAIN)
    write(repo, "lib/new.dcref", "# deadcode: ignore\ndef quiet\n")
    sink = {}
    rep = levels.branch(str(repo), "HEAD", sink=sink)
    assert sink["run"].scope == "branch" and set(sink) >= {"graph", "res", "tree", "marks", "suspects", "suppressed"}
    k = {levels._qual(n.key): n.key for n in sink["graph"].nodes}
    assert {k["link_a"], k["link_b"], k["link_c"], k["quiet"]} <= sink[
        "suspects"
    ]  # what the viewer draws, the whole graph
    assert sink["marks"][k["link_a"]] & 1 and sink["marks"][k["link_a"]] & 2  # touched by the diff, newly dead
    assert [(why, f.msg.split("`")[1]) for why, f in sink["suppressed"]] == [("ignore", "quiet")]
    assert {f.msg.split("`")[1] for f in rep.findings} == {"link_a", "link_b", "link_c"} and all(
        f.code == "dc.suspect" for f in rep.findings
    )


def test_the_marks_say_newly_alive_for_a_symbol_that_was_dead_at_the_base(repo):
    write(repo, "lib/other.dcref", "def spare_one private\n")  # touched, same node key
    write(repo, "lib/use.dcref", "def user\nref spare_one\nroot main user\n")
    sink = {}
    levels.branch(str(repo), "HEAD", sink=sink)
    k = {levels._qual(n.key): n.key for n in sink["graph"].nodes}
    assert sink["res"].verdict(k["spare_one"]) == "weak-alive"  # reached by a name match from another file
    assert sink["marks"][k["spare_one"]] == 1 | 4 and sink["marks"][k["user"]] == 1  # touched and newly alive; touched


# ================================================================ extract.batch
def test_extract_many_is_one_unit_per_content_and_the_same_for_1_2_8_workers(dcref):
    items = [("dcref", f"d/{n}.dcref", f"blob{i % 5}", f"def f{i % 5}\n") for i, n in enumerate("abcdefghij")]
    results = [batch.extract_many(items, workers=w, chunk=2) for w in (1, 2, 8)]
    assert results[0] == results[1] == results[2]
    shards, missing = results[0]
    assert len(shards) == 5 and missing == {} and [s.key for s in shards] == sorted(s.key for s in shards)
    have = {shards[0].key, shards[1].key}
    assert len(batch.extract_many(items, chunk=2, have=have)[0]) == 3  # a warm cache costs no parse


def test_extract_many_names_the_languages_whose_extractor_cannot_run_and_extracts_nothing_for_them(no_tree_sitter):
    shards, missing = batch.extract_many(
        [("java", "A.java", "b1", "class A {}"), ("zzz", "x.zzz", "b2", "x"), ("dcref", "a.dcref", "b3", "def f\n")]
    )
    assert [s.lang for s in shards] == ["dcref"]
    assert (
        set(missing) == {"java", "zzz"}
        and "tree-sitter is not installed" in missing["java"]
        and "unknown language" in missing["zzz"]
    )
    assert batch.extract_many([]) == ([], {})


def test_a_corrupt_stored_graph_does_not_break_the_run(repo, data_root):
    lay = paths.resolve(repo)
    paths.ensure(lay)
    lay.store.write_bytes(b"this is not sqlite" * 100)
    lay.store.chmod(0o600)
    write(repo, "lib/chain.dcref", CHAIN)
    rep = levels.branch(str(repo), "HEAD", proof=PROVEN)
    assert quals(rep.findings, "dc.dead-cluster") == ["link_a", "link_b", "link_c"]
    assert json.dumps(rep.meta["verdicts"])


# ================================================================ graph.safety
def test_a_failure_of_ours_is_one_line_and_a_w_row():
    exc = ValueError("two\nlines   and   spaces")
    assert safety.reason(exc) == "ValueError: two lines and spaces" and len(safety.reason(ValueError("x" * 500))) == 200
    level, path, line, code, msg = safety.check_failed("branch", exc)
    assert (level, path, line, code) == ("W", safety.WHERE, 0, "dc.check-failed")
    assert msg == "the branch check failed (ValueError: two lines and spaces); it was skipped, nothing is blocked"


def test_without_a_base_every_e_about_the_tree_is_a_w_in_place_but_a_missing_engine_stays():
    fs = [
        Finding("E", "a", 1, "dc.root-dangling", "points into the void"),
        Finding("E", "(extractor of x)", 0, "dc.engine-missing", "no grammar"),
        Finding("W", "b", 1, "dc.dead-cluster", "dead"),
    ]
    got = safety.pre_existing(fs)
    assert got is fs and [f.level for f in fs] == ["W", "E", "W"]
    assert (
        fs[0].msg.endswith("(pre-existing: there is no base to compare with, nothing is blocked)")
        and fs[1].msg == "no grammar"
    )


def test_a_dead_claim_of_the_branch_level_has_a_number_and_a_notice_has_none(repo):
    write(repo, "lib/chain.dcref", CHAIN)
    rep = levels.branch(str(repo), "HEAD", proof=PROVEN)
    got = [f for f in rep.findings if f.code == "dc.dead-cluster"]
    assert len(got) == 3 and all(isinstance(f.confidence, int) and 0 <= f.confidence <= 100 and f.conf_note for f in got)
    assert all("sources engine" in f.conf_note for f in got)  # the proof that corroborates is the first fact of the sum
    assert all(f.confidence is None for f in rep.findings if f.code not in ("dc.dead-cluster", verdict.SUSPECT))
    sus = levels.branch(str(repo), "HEAD")
    assert {f.code for f in sus.findings if f.confidence is not None} == {verdict.SUSPECT}
    assert min(f.confidence for f in got) > max(f.confidence for f in sus.findings if f.code == verdict.SUSPECT)
