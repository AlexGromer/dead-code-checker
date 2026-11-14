"""graph.confidence: the numeric confidence (0..100) of a dead-symbol claim, a second axis next to the verdict
(graph/confidence.py, graph/data/confidence.json).

python3 -m pytest -q tests/test_graph_confidence.py

Synthetic graphs and findings only (the pure formula runs on facts); the end-to-end tests use the toy language "dcref" of
tests/graph_support.py in a throwaway repository. What is pinned: every factor is monotonic, the result is a deterministic function
of its facts, the verdict (level, code, message, why, exit code) is never touched, the outputs carry the number (json, text, md,
SARIF, findings.json, the page of the viewer) and --min-confidence is a display filter.
"""

import copy
import itertools
import json
import shutil
from pathlib import Path

import pytest
from graph_support import git, new_repo, write

from dead_code_checker.graph import adapters, cli, confidence, levels, report, sarif, snippets, verdict, viz
from dead_code_checker.graph.model import (
    F_DECORATED,
    F_DYNAMIC,
    F_OVERRIDE,
    F_PROTOCOL,
    Edge,
    Finding,
    Node,
    Root,
    canonical,
    node_key,
)
from dead_code_checker.graph.verdict import Proof

CHAIN = "def link_a\nref link_b\ndef link_b\nref link_c\ndef link_c\n"
APP = "def helper\ndef run\nref helper\ndef main\nref run\nroot main main\n"

SPEC = confidence.table()


def score(**kw):
    return confidence.compute(SPEC, kw.pop("claim", "dc.dead"), **kw)[0]


# ================================================================ the data file
def test_the_data_file_names_every_factor_the_formula_reads():
    for k in ("codes", "base", "sources", "diminish", "sources_max", "engine_conf", "hazards", "coverage", "runtime"):
        assert k in SPEC
    assert set(SPEC["hazards"]) == {"decorated", "dynamic", "override", "protocol", "dyn_site_each", "dyn_site_max"}
    assert set(SPEC["coverage"]) == {"own_gap", "other_gap", "no_lang_root"}
    assert set(SPEC["runtime"]) == {"executed", "not_executed"}
    assert (
        all(0 <= v <= 100 for v in SPEC["base"].values()) and SPEC["base"]["default"] < 50
    )  # the graph alone is not sure
    assert (
        all(v < 0 for v in [*SPEC["hazards"].values(), *SPEC["coverage"].values()][:])
        and SPEC["runtime"]["executed"] < 0
    )
    assert all(w > 0 for w in SPEC["sources"].values()) and SPEC["diminish"] == sorted(SPEC["diminish"], reverse=True)
    assert set(SPEC["codes"]) >= {"dc.dead", "dc.dead-cluster", "dc.introduced-dead", verdict.SUSPECT}


def test_the_docstring_of_the_data_file_explains_every_factor():
    note = SPEC["note"]
    for word in ("base", "sources", "engine_conf", "hazards", "coverage", "runtime", "diminish", "dyn_site_each", "own_gap",
                 "other_gap", "no_lang_root", "decorated", "never changes a verdict"):  # fmt: skip
        assert word in note, word


# ================================================================ monotonic factors
def test_more_and_stronger_sources_never_lower_the_score():
    tiers = list(SPEC["sources"])
    seen = {}
    for n in range(0, 4):
        for combo in itertools.combinations_with_replacement(tiers, n):
            seen[combo] = score(sources=list(combo))
    for combo, v in seen.items():
        for t in tiers:
            assert score(sources=[*combo, t]) >= v, (combo, t)  # one more source
    for weak, strong in (("lsp", "engine"), ("engine", "compiler"), ("engine", "s-tier")):
        assert score(sources=[strong]) >= score(sources=[weak])
    assert score(sources=["s-tier"]) > score(sources=[])
    assert score(sources=["s-tier", "engine"]) > score(sources=["s-tier"])  # the second counts less, but it counts


def test_the_sources_are_capped_and_the_order_of_the_list_is_irrelevant():
    many = ["s-tier", "compiler", "engine", "lsp", "s-tier", "engine"]
    assert score(sources=many) - score() <= SPEC["sources_max"]
    for perm in itertools.permutations(["s-tier", "engine", "lsp"]):
        assert score(sources=list(perm)) == score(sources=["s-tier", "engine", "lsp"])


def test_a_higher_engine_confidence_never_lowers_the_score():
    vals = [score(engine_conf=c) for c in range(0, 101, 5)]
    assert vals == sorted(vals) and vals[-1] > vals[0]
    assert score(engine_conf=None) == score(engine_conf=0)  # an engine that reports none adds nothing
    assert score(engine_conf=250) == score(engine_conf=100) and score(engine_conf=-5) == score(engine_conf=0)  # clamped


@pytest.mark.parametrize("flag", [F_DECORATED, F_DYNAMIC, F_OVERRIDE, F_PROTOCOL])
def test_every_hazard_flag_lowers_the_score(flag):
    base = score(sources=["engine"])
    assert score(sources=["engine"], flags=flag) < base
    assert score(sources=["engine"], flags=flag | F_DECORATED | F_DYNAMIC) <= score(sources=["engine"], flags=flag)


def test_dynamic_call_sites_of_the_module_lower_the_score_down_to_a_floor():
    vals = [score(sources=["engine"], dyn_sites=n) for n in range(0, 12)]
    assert vals == sorted(vals, reverse=True) and vals[1] < vals[0]
    assert vals[-1] == vals[-2] == score(sources=["engine"]) + SPEC["hazards"]["dyn_site_max"]  # the penalty stops


def test_a_degraded_language_lowers_the_score_and_an_own_gap_more_than_a_foreign_one():
    base = score(sources=["engine"])
    own, other, noroot = (score(sources=["engine"], **{k: True}) for k in ("own_gap", "other_gap", "no_lang_root"))
    assert own < other < base and noroot < base
    assert score(sources=["engine"], own_gap=True, other_gap=True, no_lang_root=True) <= min(own, other, noroot)


def test_a_runtime_result_moves_the_score_and_none_changes_nothing():
    base = score(sources=["engine"])
    assert score(sources=["engine"], runtime=None) == base
    assert score(sources=["engine"], runtime=True) < base < score(sources=["engine"], runtime=False)


def test_the_score_stays_between_0_and_100():
    worst = score(
        own_gap=True,
        no_lang_root=True,
        runtime=True,
        dyn_sites=99,
        flags=F_DECORATED | F_DYNAMIC | F_OVERRIDE | F_PROTOCOL,
    )
    best = score(sources=list(SPEC["sources"]), engine_conf=100, runtime=False, claim="dc.dead")
    assert worst == 0 and 0 < best <= 100
    top = copy.deepcopy(SPEC)
    top["base"]["dc.dead"] = 100
    assert confidence.compute(top, "dc.dead", sources=["s-tier"], engine_conf=100)[0] == 100


def test_the_claim_sets_the_base_and_a_cluster_starts_lower_than_a_single_symbol():
    assert score(claim="dc.dead-cluster") < score(claim="dc.dead")
    assert score(claim="something.new") == SPEC["base"]["default"]


def test_deterministic_and_the_note_names_the_sum():
    args = dict(sources=["s-tier", "engine"], engine_conf=100, flags=F_DYNAMIC, dyn_sites=2, runtime=False)
    a, b = confidence.compute(SPEC, "dc.dead", **args), confidence.compute(SPEC, "dc.dead", **args)
    assert a == b and a[1].startswith("base 40, sources s-tier+engine +35") and a[1].endswith(f"= {a[0]}")
    assert "dynamic -20" in a[1] and "2 dynamic site(s) -8" in a[1] and "runtime not run +10" in a[1]


# ================================================================ the facts of a graph
def mk_graph(extra_nodes=(), edges=(), roots=True):
    k = {q: node_key("python", "lib/m.py", q) for q in ("plain", "decorated", "dyn", "other")}
    nodes = [
        Node(k["plain"], "func", "python", "lib/m.py", 1, 2, "lib", "public", 0),
        Node(k["decorated"], "func", "python", "lib/m.py", 4, 5, "lib", "public", F_DECORATED),
        Node(k["dyn"], "func", "python", "lib/m.py", 7, 8, "lib", "public", F_DYNAMIC),
        Node(k["other"], "func", "python", "lib/o.py", 1, 2, "lib", "public", 0),
        *extra_nodes,
    ]
    rts = [Root(k["plain"], "", "main", "x:1", "hard", "registered", "")] if roots else []
    return canonical(nodes, edges, rts), k


def claim(path, line, code="dc.dead", level="W", msg="`x` is dead", why="why"):
    return Finding(level, path, line, code, msg, why)


def facts(f):
    return (f.level, f.path, f.line, f.code, f.msg, f.why)


def test_annotate_scores_a_claim_from_the_graph_and_the_proof():
    g, k = mk_graph()
    proven = Proof(nodes={k["plain"]: "go-deadcode@1 unreachable at a.go:1"})
    fs = [claim("lib/m.py", 1), claim("lib/m.py", 4), claim("lib/m.py", 7), claim("lib/o.py", 1)]
    assert confidence.annotate(fs, g, proven) == 4
    plain, deco, dyn, other = (f.confidence for f in fs)
    assert plain == 40 + 20 and deco == 40 - 15 and dyn == 40 - 20  # the engine source only on the node it names
    assert other == 40 and all("= " in f.conf_note for f in fs)


def test_a_corroborated_claim_scores_above_the_suspect_it_would_be_without_the_proof():
    g, k = mk_graph()
    for proof in (Proof(covered=frozenset({"lib/m.py"})), Proof(nodes={k["plain"]: "rustc@1 dead_code at m.rs:1"})):
        w = verdict.settle(claim("lib/m.py", 1), proof.text(k["plain"], "lib/m.py"))
        s = verdict.settle(claim("lib/m.py", 1), "")
        assert w.code == "dc.dead" and s.code == verdict.SUSPECT
        confidence.annotate([w], g, proof)
        confidence.annotate([s], g, Proof())
        assert w.confidence > s.confidence, proof
        assert (
            s.confidence == SPEC["base"]["dc.dead"] and "sources" not in s.conf_note
        )  # a suspect has the base of the code it replaced


def test_the_dynamic_sites_of_the_module_and_the_roots_of_the_language_count():
    d = [
        Edge(
            node_key("python", "lib/m.py", "plain"),
            node_key("python", "lib/m.py", "dyn"),
            "ref",
            "D",
            f"lib/m.py:{n}",
            -1,
        )
        for n in (3, 3, 6)
    ]
    g, k = mk_graph(edges=d)
    f, o = claim("lib/m.py", 1), claim("lib/o.py", 1)
    confidence.annotate([f, o], g)
    assert (
        f.confidence == 40 - 8 and "2 dynamic site(s)" in f.conf_note
    )  # two distinct sites in lib/m.py, none in lib/o.py
    assert o.confidence == 40
    g2, _ = mk_graph(roots=False)
    n = claim("lib/o.py", 1)
    confidence.annotate([n], g2)
    assert (
        n.confidence == 40 - 10 and "no lang root" in n.conf_note
    )  # nothing registered for python: "no root reaches it" says little


def test_a_gap_of_the_extractors_and_a_runtime_result_are_read():
    g, k = mk_graph()
    a, b, c, d = (claim("lib/o.py", 1) for _ in range(4))
    confidence.annotate([a], g, gaps={"ruby"})
    confidence.annotate([b], g, gaps={"python"})  # the language itself
    confidence.annotate([c], g, runtime={k["other"]: True})
    confidence.annotate([d], g, runtime={"lib/o.py": False})
    assert (a.confidence, b.confidence, c.confidence, d.confidence) == (40 - 8, 40 - 25, 40 - 40, 40 + 10)
    e = claim("lib/o.py", 1)
    confidence.annotate([e], g, runtime={})
    assert e.confidence == 40  # an empty runtime result changes nothing


def test_an_engine_confidence_below_the_bar_is_scored_but_corroborates_nothing():
    g, k = mk_graph()
    h = adapters.Hit("vulture", "2", "lib/m.py", 1, 0, "unused-function", "", "func", "plain", 60)
    p = verdict.build(matches=[adapters.Match(k["plain"], h, True, 0)])
    assert p.confs == {k["plain"]: 60} and p.nodes == {}  # 60 % does not corroborate (min_conf 100)
    f = verdict.settle(claim("lib/m.py", 1), p.text(k["plain"], "lib/m.py"))
    assert f.code == verdict.SUSPECT
    confidence.annotate([f], g, p)
    assert f.confidence == 40 + 15 * 60 // 100 and "engine 60%" in f.conf_note
    h100 = adapters.Hit("vulture", "2", "lib/m.py", 1, 0, "unused-function", "", "func", "plain", 100)
    p2 = verdict.build(matches=[adapters.Match(k["plain"], h100, True, 0)])
    w = verdict.settle(claim("lib/m.py", 1), p2.text(k["plain"], "lib/m.py"))
    confidence.annotate([w], g, p2)
    assert w.code == "dc.dead" and w.confidence == 40 + 20 + 15  # an engine source and its own percent
    both = verdict.both(p, p2)
    assert both.confs == {k["plain"]: 100}  # the stronger hit of the two proofs


def test_a_proof_with_its_own_text_rule_counts_as_one_source():
    class Own(Proof):
        def text(self, key, path):
            return "pmd@7 UnusedPrivateMethod at A.java:3"

    g, _ = mk_graph()
    f = claim("lib/m.py", 1)
    confidence.annotate([f], g, Own())
    assert f.confidence == 40 + 20 and "sources engine" in f.conf_note


def test_only_the_claims_get_a_number_and_a_number_is_kept():
    g, _ = mk_graph()
    other = Finding("W", "lib/m.py", 1, "dc.engine-missing", "x")
    c = claim("lib/m.py", 1)
    assert confidence.annotate([other, c], g) == 1 and other.confidence is None and c.confidence is not None
    c.confidence, c.conf_note = 7, "kept"
    assert confidence.annotate([c], g) == 0 and (c.confidence, c.conf_note) == (7, "kept")
    assert (
        "confidence" not in other.as_dict()
        and c.as_dict()["confidence"] == 7
        and c.as_dict()["confidence_note"] == "kept"
    )


def test_annotate_never_raises_and_never_touches_a_verdict():
    g, k = mk_graph()
    fs = [
        claim("lib/m.py", 1),
        claim("lib/m.py", 4, code=verdict.SUSPECT, level="I", msg="[dc.dead] `x` is dead"),
        claim("lib/zzz.py", 9),
    ]
    before = [facts(f) for f in fs]
    confidence.annotate(fs, g, Proof(covered=frozenset({"lib/m.py"})), gaps={"go"}, runtime={"lib/m.py": True})
    assert [facts(f) for f in fs] == before  # level, code, message, why: exactly as they were
    assert all(
        f.confidence is not None for f in fs
    )  # a claim with no node of the graph (lib/zzz.py) is scored on the claim itself
    assert confidence.annotate([claim("lib/m.py", 1)], None) == 0  # a broken input is a no-op, not a traceback
    assert confidence.annotate([claim("lib/m.py", 1)], g, object()) == 0


def test_the_whole_run_is_deterministic_and_independent_of_the_order_of_findings():
    g, k = mk_graph()
    proof = Proof(covered=frozenset({"lib/m.py"}), nodes={k["other"]: "knip@1 exports at o.ts:1"})
    fs = [claim("lib/m.py", 1), claim("lib/m.py", 4), claim("lib/m.py", 7), claim("lib/o.py", 1)]
    out = []
    for order in (fs, list(reversed(copy.deepcopy(fs)))):
        confidence.annotate(order, g, proof)
        out.append(sorted((f.path, f.line, f.confidence, f.conf_note) for f in order))
    assert out[0] == out[1]
    again = copy.deepcopy(fs)
    for f in again:
        f.confidence, f.conf_note = None, ""
    confidence.annotate(again, g, proof)
    assert sorted((f.path, f.line, f.confidence, f.conf_note) for f in again) == out[0]


# ================================================================ the verdict rule is untouched
def test_the_verdict_data_and_the_confirmation_rule_do_not_read_the_confidence():
    # the corroboration of a claim is the same with and without a number in the proof
    base = Proof(covered=frozenset({"lib/m.py"}))
    rich = Proof(covered=frozenset({"lib/m.py"}), confs={"python:lib/m.py#plain": 100})
    for text in (base.text("python:lib/m.py#plain", "lib/m.py"), rich.text("python:lib/m.py#plain", "lib/m.py")):
        assert verdict.settle(claim("lib/m.py", 1), text).code == "dc.dead"
    assert (
        verdict.settle(claim("lib/m.py", 1), Proof(confs={"x": 100}).text("x", "lib/m.py")).code == verdict.SUSPECT
    )  # a number alone proves nothing


# ================================================================ the outputs
def sample():
    a = claim("lib/a.py", 3, msg="`a` is dead", why="because")
    b = claim("lib/b.py", 5, code=verdict.SUSPECT, level="I", msg="[dc.dead] `b` is dead", why="not corroborated")
    c = Finding("W", "lib/c.py", 1, "dc.engine-missing", "no engine", "")
    d = claim("lib/d.py", 9, code="dc.dead-cluster", msg="`d` in a cluster")
    for f, n in ((a, 85), (b, 22), (d, 61)):
        f.confidence, f.conf_note = n, f"base 40 = {n}"
    return [a, b, c, d]


def test_json_text_and_md_show_the_number_and_the_filter_is_display_only():
    fs = sample()
    meta = {"show_suspects": True}
    doc = json.loads(report.to_json(fs, meta))
    by = {f["path"]: f for f in doc["findings"]}
    assert by["lib/a.py"]["confidence"] == 85 and by["lib/a.py"]["confidence_note"] == "base 40 = 85"
    assert "confidence" not in by["lib/c.py"]
    assert "confidence 85/100 (base 40 = 85)" in report.to_text(
        fs, meta
    ) and "Confidence: 85/100 (base 40 = 85)" in report.to_md(fs, meta)
    kept = json.loads(report.to_json(fs, dict(meta, min_confidence=60)))
    assert sorted(f["path"] for f in kept["findings"]) == [
        "lib/a.py",
        "lib/c.py",
        "lib/d.py",
    ]  # 22 is out, the finding without a number stays
    assert kept["meta"]["below_confidence_hidden"] == 1
    assert "1 finding(s) below confidence 60 hidden, --min-confidence" in report.to_text(
        fs, dict(meta, min_confidence=60)
    )
    assert "below confidence 60 hidden" in report.to_md(fs, dict(meta, min_confidence=60))
    assert (
        json.loads(report.to_json(fs, dict(meta, min_confidence=0)))["findings"] == doc["findings"]
    )  # no floor, no change
    assert len(fs) == 4  # the filter dropped nothing from the list it was given
    assert json.loads(report.to_json(fs, dict(meta, min_confidence=100)))["meta"]["below_confidence_hidden"] == 3


def test_sarif_carries_the_confidence_as_a_property_and_as_the_rank():
    fs = sample()
    doc = json.loads(sarif.to_sarif(fs, {"show_suspects": True}))
    res = {r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]: r for r in doc["runs"][0]["results"]}
    assert (
        doc["version"] == "2.1.0"
        and res["lib/a.py"]["properties"]["confidence"] == 85
        and res["lib/a.py"]["rank"] == 85.0
    )
    assert res["lib/a.py"]["properties"]["confidenceNote"] == "base 40 = 85" and res["lib/a.py"]["level"] == "warning"
    assert "rank" not in res["lib/c.py"] and "confidence" not in res["lib/c.py"].get("properties", {})
    assert res["lib/b.py"]["level"] == "note" and res["lib/b.py"]["rank"] == 22.0
    assert [r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]] == sorted({f.code for f in fs})
    assert sarif.to_sarif(fs, {"show_suspects": True}) == sarif.to_sarif(
        list(reversed(fs)), {"show_suspects": True}
    )  # stable
    hidden = json.loads(sarif.to_sarif(fs, {}))  # the suspect is hidden as in every format
    assert "lib/b.py" not in {
        r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] for r in hidden["runs"][0]["results"]
    }
    low = json.loads(sarif.to_sarif(fs, {"show_suspects": True, "min_confidence": 70}))
    assert sorted(r["rank"] for r in low["runs"][0]["results"] if "rank" in r) == [85.0]


def test_a_min_confidence_argument_is_a_percent():
    for bad in ("101", "-1", "x"):
        with pytest.raises(SystemExit):
            cli.main(["check", "--scope", "branch", "--min-confidence", bad])


# ================================================================ end to end
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
STIER = Proof(covered=frozenset({"lib/chain.dcref"}))


@pytest.fixture
def repo(tmp_path, hermetic_git, data_root, dcref, monkeypatch):
    monkeypatch.setattr(viz, "dot_path", lambda: None)
    monkeypatch.setattr(snippets, "find_gitleaks", lambda: None)
    r = new_repo(tmp_path, {"lib/app.dcref": APP, "run.dcroots": "main file:lib/app.dcref\n"})
    write(r, "lib/chain.dcref", CHAIN)
    return r


def go(capsys, *argv):
    rc = cli.main([str(a) for a in argv])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


@needs_git
def test_a_branch_run_gives_every_dead_claim_a_number_and_changes_no_verdict(repo):
    rep = levels.branch(str(repo), "HEAD", proof=STIER)
    claims = [f for f in rep.findings if f.code == "dc.dead-cluster"]
    assert len(claims) == 3 and all(isinstance(f.confidence, int) and 0 <= f.confidence <= 100 for f in claims)
    assert all(f.conf_note.startswith("base 30, sources s-tier +25") for f in claims)
    again = levels.branch(str(repo), "HEAD", proof=STIER)
    assert [facts(f) for f in again.findings] == [facts(f) for f in rep.findings]  # same run, same findings
    assert [(f.confidence, f.conf_note) for f in again.findings] == [(f.confidence, f.conf_note) for f in rep.findings]
    sus = levels.branch(str(repo), "HEAD")  # no proof: every claim is a suspect, still with a number (the base of the claim)
    suspects = [f for f in sus.findings if f.code == verdict.SUSPECT]
    assert len(suspects) == 3 and all(f.confidence == 30 and "sources" not in f.conf_note for f in suspects)
    assert all(f.confidence is None for f in rep.findings if f.code not in confidence.table()["codes"])


@needs_git
def test_the_repo_level_numbers_its_claims_too(repo):
    from dead_code_checker.graph import repo as repo_level

    rep = repo_level.check(str(repo), proof=STIER)
    claims = [f for f in rep.findings if f.code == "dc.dead-cluster"]
    assert len(claims) == 3 and all(f.confidence is not None and f.confidence <= 100 for f in claims)


@needs_git
def test_check_json_and_sarif_show_the_number_and_the_floor_hides_without_changing_the_exit(repo, capsys):
    base = ["check", "--scope", "branch", "--root", repo, "--base", "HEAD", "--no-viz", "--show-suspects"]
    rc, out, _ = go(capsys, *base, "--json")
    full = json.loads(out)
    sus = [f for f in full["findings"] if f["code"] == verdict.SUSPECT]
    assert rc == 0 and len(sus) == 3 and all("confidence" in f for f in sus) and "min_confidence" not in full["meta"]
    rc, out, _ = go(capsys, *base, "--json", "--min-confidence", "99")
    cut = json.loads(out)
    assert rc == 0 and [f for f in cut["findings"] if f["code"] == verdict.SUSPECT] == []
    assert cut["meta"]["below_confidence_hidden"] == 3
    rc, out, _ = go(capsys, *base, "--format", "sarif")
    doc = json.loads(out)
    assert rc == 0 and any("rank" in r for r in doc["runs"][0]["results"])
    rc, out, _ = go(capsys, *base, "--min-confidence", "99")
    assert rc == 0 and "3 finding(s) below confidence 99 hidden, --min-confidence" in out
    rc, out, _ = go(capsys, *base, "--min-confidence", "0")
    assert rc == 0 and "below confidence" not in out and "confidence 30/100" in out


@needs_git
def test_a_bad_min_confidence_is_a_usage_error(repo, capsys):
    for bad in ("101", "-1", "x", "1.5"):
        with pytest.raises(SystemExit) as e:
            cli.main(["check", "--scope", "branch", "--root", str(repo), "--min-confidence", bad])
        assert e.value.code == 2
        assert "--min-confidence" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["check", "--help"])
    assert "--min-confidence N" in capsys.readouterr().out


@needs_git
def test_findings_json_and_the_page_of_a_run_keep_every_finding_whatever_the_floor(repo, tmp_path, capsys):
    out = tmp_path / "run"
    rc, text, _ = go(
        capsys, "check", "--scope", "branch", "--root", repo, "--base", "HEAD", "--out", out, "--min-confidence", "100", "--plain", "--show-suspects"
    )
    assert rc == 0 and "below confidence 100 hidden" in text
    (out,) = [p for p in out.iterdir() if p.is_dir() and not p.is_symlink()]  # <out>/<utc>-branch-<head8>/
    doc = json.loads((out / "findings.json").read_text())
    sus = [f for f in doc["findings"] if f["code"] == verdict.SUSPECT]
    assert len(sus) == 3 and all("confidence" in f for f in sus)  # the artifacts are complete: the floor filters the printout
    assert "below confidence" not in (out / "report.md").read_text()
    assert '"rank": 30.0' in (out / "sarif.json").read_text()
    page = (out / "index.html").read_text()
    assert 'id="f-sort"' in page and "confidence" in page
