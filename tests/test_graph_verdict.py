"""graph.verdict and graph.report: the corroboration rule for a "dead" verdict, and how the findings are shown.

A claim that a code symbol is dead (dc.dead, dc.dead-cluster, dc.introduced-dead) is a W only when an S-tier index of the package
or the native engine of the language agrees; otherwise it is a dc.suspect (level I): hidden by default, drawn by the viewer.

python3 -m pytest -q tests/test_graph_verdict.py

No engine is started and nothing touches the network: an engine hit is a plain tuple here.
"""

import json
from typing import NamedTuple

import pytest

from dead_code_checker.graph import model as md, report, verdict
from dead_code_checker.graph.model import Finding
from dead_code_checker.graph.verdict import Proof


class Hit(NamedTuple):
    tool: str
    ver: str
    path: str
    line: int
    rule: str
    claim: str
    conf: int


class Match(NamedTuple):
    node: str
    hit: Hit
    exact: bool


def hit(tool, rule="", claim="", conf=0, path="lib/chain.py", line=1, ver="1"):
    return Hit(tool, ver, path, line, rule, claim, conf)


def match(node, h, exact=True):
    return Match(node, h, exact)


# ================================================================ the rule: which engines corroborate
@pytest.mark.parametrize(
    "tool,rule,claim,conf,ok",
    [
        ("go-deadcode", "unreachable", "func", 0, True),  # RTA
        ("go-deadcode", "unreachable", "class", 0, False),
        ("rustc", "dead_code", "func", 0, True),
        ("rustc", "unused_imports", "import", 0, False),
        ("cargo", "dead_code", "class", 0, True),
        ("knip", "exports", "export", 0, True),
        ("knip", "files", "file", 0, False),
        ("pmd", "UnusedPrivateMethod", "method", 0, True),
        ("pmd", "UnusedLocalVariable", "local", 0, False),
        ("roslyn", "IDE0051", "member", 0, True),
        ("roslyn", "CS0219", "local", 0, False),
        ("vulture", "unused-function", "func", 100, True),
        ("vulture", "unused-function", "func", 99, False),  # 100% confidence only
        ("vulture", "unused-variable", "var", 100, False),
        ("ruff", "F401", "import", 0, False),  # never names a function, method or class
        ("staticcheck", "U1000", "func", 0, False),  # not an authoritative engine
        ("nobody", "x", "func", 100, False),
    ],
)
def test_agrees_per_engine(tool, rule, claim, conf, ok):
    assert verdict.agrees(tool, rule, claim, conf) is ok


def test_build_counts_only_an_exact_hit_of_an_authoritative_engine():
    good = match("py:lib/chain.py#link_a/0", hit("vulture", "unused-function", "func", 100, line=1))
    loose = match("py:lib/chain.py#link_b/0", hit("vulture", "unused-function", "func", 100), exact=False)
    weak = match("py:lib/chain.py#link_c/0", hit("vulture", "unused-function", "func", 90))
    other = match("py:lib/chain.py#x/0", hit("ruff", "F401", "import"))
    p = verdict.build(matches=[weak, other, loose, good])
    assert set(p.nodes) == {"py:lib/chain.py#link_a/0"}
    assert p.nodes["py:lib/chain.py#link_a/0"] == "vulture@1 unused-function at lib/chain.py:1"
    assert not p.covered and verdict.build(covered=["a/b.py"]).covered == frozenset({"a/b.py"})


def test_build_keeps_the_hit_that_sorts_first_when_two_engines_agree_on_a_node():
    a = match("n", hit("rustc", "dead_code", "func", path="a.rs", line=3, ver=""))
    b = match("n", hit("cargo", "dead_code", "func", path="a.rs", line=5, ver="2"))
    assert verdict.build(matches=[b, a]).nodes == {"n": "rustc dead_code at a.rs:3"}
    assert verdict.build(matches=[a, b]).nodes == {"n": "rustc dead_code at a.rs:3"}


def test_proof_text_names_the_index_or_the_engine_and_is_empty_without_one():
    p = Proof(frozenset({"pkg/a.py"}), {"k": "vulture@1 unused-function at b.py:2"})
    assert p.text("zz", "pkg/a.py").startswith("S-tier: the SCIP index")
    assert p.text("k", "b.py") == "vulture@1 unused-function at b.py:2"
    assert p.text("zz", "d.py") == "" and Proof().text("k", "b.py") == ""


def test_both_holds_wherever_either_proof_does_and_keeps_a_proof_that_answers_by_its_own_rule():
    a, b = Proof(frozenset({"a.py"}), {"x": "A says x"}), Proof(frozenset({"b.py"}), {"x": "B says x", "y": "B says y"})
    j = verdict.both(a, b)
    assert j.covered == {"a.py", "b.py"} and j.text("x", "z.py") == "A says x" and j.text("y", "z.py") == "B says y"
    assert verdict.both(a, Proof()) is a and verdict.both(Proof(), b) is b

    class Always(Proof):
        def text(self, key, path):
            return "always"

    c = verdict.both(a, Always())
    assert c.text("nothing", "z.py") == "always" and c.text("x", "z.py") == "A says x" and c.covered == {"a.py"}


# ================================================================ settle: W with the proof in the why, else a suspect
def dead_finding(code="dc.dead-cluster", level="W"):
    return Finding(level, "lib/x.py", 4, code, "`f` is dead", "no incoming edge")


@pytest.mark.parametrize("code", ["dc.dead", "dc.dead-cluster", "dc.introduced-dead"])
def test_a_dead_claim_without_proof_is_a_suspect_of_level_I(code):
    s = verdict.settle(dead_finding(code, "E"), "")  # even an E never survives without proof
    assert (s.level, s.code, s.path, s.line) == ("I", "dc.suspect", "lib/x.py", 4)
    assert s.msg == f"[{code}] `f` is dead" and verdict.was(s) == code and verdict.claim(s) == code
    assert s.why.split("\n")[0] == verdict.NOT_PROVEN and s.why.endswith("no incoming edge")


@pytest.mark.parametrize("code", ["dc.dead", "dc.dead-cluster", "dc.introduced-dead"])
def test_a_corroborated_claim_stays_and_says_by_what(code):
    f = dead_finding(code)
    s = verdict.settle(f, "vulture@2 unused-function at lib/x.py:4")
    assert s is f and (s.level, s.code) == ("W", code)
    assert s.why == "no incoming edge\ncorroborated: vulture@2 unused-function at lib/x.py:4"
    assert verdict.was(s) == "" and verdict.claim(s) == code
    assert (
        verdict.proof_of(s) == "vulture@2 unused-function at lib/x.py:4"
        and verdict.proof_tool(verdict.proof_of(s)) == "vulture"
    )


def test_the_proof_text_tells_who_gave_it():
    assert verdict.proof_tool("S-tier: the SCIP index of the package of a.py was applied") == "s-tier"
    assert (
        verdict.proof_tool("rustc@1.80 dead_code at a.rs:3") == "rustc"
        and verdict.proof_tool("rustc dead_code at a.rs:3") == "rustc"
    )
    assert verdict.proof_of(dead_finding()) == "" and verdict.proof_of(verdict.settle(dead_finding(), "")) == ""


def test_settle_takes_the_claims_to_settle_from_the_caller_when_given():
    f = Finding("W", "a.py", 1, "x.custom", "m")
    assert verdict.settle(f, "") is f  # not a dead claim of the table
    assert verdict.settle(f, "", codes=["x.custom"]).code == "dc.suspect"


@pytest.mark.parametrize(
    "code",
    [
        "dc.orphaned-by-branch",
        "dc.root-dangling",
        "dc.dangling-ref",
        "dc.engine-disagree",
        "dc.engine-missing",
    ],
)
def test_what_the_graph_decides_is_never_touched(code):
    f = dead_finding(code, "E")
    assert verdict.settle(f, "") is f and (f.level, f.code, f.why) == ("E", code, "no incoming edge")


def test_the_severity_order_puts_the_suspects_between_dead_and_weak_alive():
    s = verdict.SEVERITY
    assert (
        s[0] == "dead-cluster"
        and s[-1] == "alive"
        and s.index("dead") < s.index(verdict.SUSPECTS) < s.index("weak-alive")
    )
    assert set(md.VERDICTS) <= set(s)


# ================================================================ how the findings are shown
def mixed():
    return [
        Finding("E", "a.py", 1, "dc.root-dangling", "empty"),
        Finding("W", "a.py", 2, "dc.introduced-dead", "`x` is dead", "why x"),
        Finding("I", "b.py", 3, "dc.suspect", "[dc.dead] `y` is dead", "not corroborated"),
        Finding("I", "b.py", 4, "dc.suspect", "[dc.dead] `z` is dead", "not corroborated"),
    ]


def test_text_json_and_md_hide_the_suspects_and_count_them():
    text, js, md_ = report.to_text(mixed()), json.loads(report.to_json(mixed())), report.to_md(mixed())
    assert "dc.suspect" not in text and "dead-code-checker: 2 suspects hidden, --show-suspects" in text
    assert "1 error(s), 1 warning(s)" in text
    assert [f["code"] for f in js["findings"]] == ["dc.root-dangling", "dc.introduced-dead"] and js["meta"] == {
        "suspects_hidden": 2
    }
    assert "dc.suspect" not in md_ and "2 finding(s). 2 suspects hidden, --show-suspects." in md_


def test_show_suspects_prints_them_all_and_says_no_hidden_line():
    meta = {"show_suspects": True}
    text, js, md_ = (
        report.to_text(mixed(), meta),
        json.loads(report.to_json(mixed(), meta)),
        report.to_md(mixed(), meta),
    )
    assert text.count("dc.suspect") == 2 and "hidden" not in text and "1 error(s), 1 warning(s), 2 suspect(s)" in text
    assert len(js["findings"]) == 4 and js["meta"] == {} and "dc.suspect" in md_ and "hidden" not in md_


def test_a_run_without_suspects_prints_the_plain_lines():
    fs = mixed()[:2]
    assert report.to_text(fs).endswith("dead-code-checker: 1 error(s), 1 warning(s)") and "hidden" not in report.to_md(
        fs
    )


def test_every_finding_leaves_with_a_why_and_a_layer_can_add_its_own_default():
    f = Finding("W", "a.py", 1, "dc.unsupported-language", "no extractor")
    assert report.ensure_why(f).why.startswith("no silent skips") and report.ensure_why(f) is f
    g = Finding("W", "a.py", 1, "dc.made-up", "m")
    assert "dc.made-up" in report.ensure_why(g).why and "graph why" in g.why
    keep = Finding("W", "a.py", 1, "dc.unsupported-language", "m", "my own why")
    assert report.ensure_why(keep).why == "my own why"
    report.register_why({"dc.layer-x": "text of a layer", "dc.unsupported-language": "never replaces"})
    try:
        assert report.ensure_why(Finding("W", "p", 1, "dc.layer-x", "m")).why == "text of a layer"
        assert report.DEFAULT_WHY["dc.unsupported-language"].startswith("no silent skips")
    finally:
        report.DEFAULT_WHY.pop("dc.layer-x")
    assert [f.why for f in report.ensure_all([Finding("W", "p", 1, "dc.bad-facts", "m")])] == [
        report.DEFAULT_WHY["dc.bad-facts"]
    ]


def test_the_text_tail_names_the_graph_size_the_unfinished_stages_and_the_languages_without_an_extractor():
    meta = {"files": 3, "nodes": 10, "edges": 7, "unfinished": ["diff"], "unsupported": ["lua"]}
    tail = report.to_text([], meta).splitlines()[-1]
    assert (
        tail
        == "dead-code-checker: 0 error(s), 0 warning(s) (3 files, 10 nodes, 7 edges, unfinished: diff, no extractor: lua)"
    )


def test_json_is_stable_and_leaves_the_clock_out_unless_asked():
    meta = {"files": 1, "timing_ms": {"tree": 5}, "show_suspects": True}
    doc = json.loads(report.to_json(mixed(), meta))
    assert "timing" not in doc and "timing_ms" not in doc["meta"] and "show_suspects" not in doc["meta"]
    assert [f["line"] for f in doc["findings"]] == [1, 2, 3, 4] and all(f["why"] for f in doc["findings"])
    assert json.loads(report.to_json(mixed(), meta, timing=True))["timing"] == {"tree": 5}
    assert report.to_json(mixed(), meta) == report.to_json(mixed(), meta)  # the same input, the same bytes


def test_md_report_names_the_base_the_counts_the_verdicts_and_what_was_not_done():
    meta = {"scope": "branch", "base": "main", "changed": 2, "files": 5, "nodes": 9, "edges": 4, "verdicts": {"alive": 3, "dead": 1},
            "unfinished": ["diff"], "unsupported": ["lua"], "dir": "/data/run1", "links": {"graph": "graph.html"}}  # fmt: skip
    md_ = report.to_md(mixed()[:2], meta)
    lines = md_.splitlines()
    assert (
        lines[0] == "Pages: [graph](graph.html); run directory `/data/run1`"
        and lines[1] == "# dead-code-checker branch report"
    )
    assert "Base `main`; 2 changed file(s); 5 files, 9 nodes, 4 edges in the graph." in md_
    assert (
        "Verdicts: alive 3, dead 1." in md_
        and "Unfinished (budget): diff." in md_
        and "Languages without an extractor" in md_
    )
    assert "## E `dc.root-dangling` a.py:1" in md_ and "Why:" in md_
    repo = report.to_md(
        [], {"scope": "repo", "files": 4, "nodes": 6, "edges": 2, "no_page": "no page: viewer_code is off", "dir": "d"}
    )
    assert repo.splitlines()[0] == "Pages: no page: viewer_code is off; run directory `d`" and "Base" not in repo


# ================================================================ the silence of a language server (verdict.lsp_blind, whole_clusters)
def _blind_graph():
    def n(path, qual, kind="func", lang="python", vis="public", fl=0):
        return md.node(md.node_key(lang, path, qual), kind, lang, path, 1, 1, vis=vis, flags=fl)

    nodes = {
        "plain": n("a.py", "in_table"),
        "plain_cls": n("a.py", "Own"),
        "own_m": n("a.py", "Own.m", "method"),
        "wall": n("a.py", "_Wall", "class"),
        "http_open": n("a.py", "_Wall.http_open", "method"),
        "redirect": n("a.py", "_NoRe.redirect_request", "method"),
        "http_error": n("a.py", "_Eh.http_error_404", "method"),
        "https_open": n("a.py", "_Wall.https_open", "method"),
        "file_open": n("a.py", "file_open"),
        "dyn": n("a.py", "dynamic", fl=md.F_DYNAMIC),
        "deco": n("a.py", "decorated", fl=md.F_DECORATED),
        "dunder": n("a.py", "Own.__call__", "method"),
        "ovr": n("a.py", "Kid.run", "method"),
        "impl": n("a.py", "Impl.run", "method"),
        "go_exp": n("s.go", "T.Handle2", "method", "go", "exported"),
        "go_low": n("s.go", "T.handle", "method", "go", "package"),
        "go_fn": n("s.go", "Serve", "func", "go", "exported"),
        "go_proto": n("s.go", "T.Reset", "method", "go", "package"),  # a protocol name of rules.json, whatever its vis
    }
    es = [
        md.edge(nodes["wall"].key, nodes["https_open"].key, "override", "N", "a.py:1"),  # a base the tree lacks
        md.edge(nodes["wall"].key, nodes["http_open"].key, "override", "N", "a.py:1"),
        md.edge(nodes["plain_cls"].key, nodes["ovr"].key, "override", "T", "a.py:1"),
        md.edge(nodes["impl"].key, nodes["own_m"].key, "impl", "T", "a.py:1"),
    ]
    return md.canonical(list(nodes.values()), es), nodes


def test_the_silence_of_a_server_proves_nothing_for_a_hook_that_a_base_class_or_a_framework_calls_by_name():
    g, n = _blind_graph()
    blind = verdict.lsp_blind(g, g.nodes)
    for name in ("http_open", "https_open", "redirect", "http_error", "dyn", "deco", "dunder", "ovr", "impl"):
        assert n[name].key in blind, name  # external base / override / implements / urllib names / flags / dunder
    assert n["redirect"].key in blind and "framework" in blind[n["redirect"].key]
    for name in ("plain", "plain_cls", "wall", "own_m", "file_open", "go_low", "go_fn"):
        assert (
            n[name].key not in blind
        ), name  # a plain function or class, a method that overrides nothing, an unexported Go method


def test_gopls_silence_does_not_corroborate_an_exported_method_that_may_satisfy_an_interface_of_a_library():
    g, n = _blind_graph()
    blind = verdict.lsp_blind(g, g.nodes)
    assert n["go_exp"].key in blind and "implicitly" in blind[n["go_exp"].key]
    assert n["go_proto"].key in blind and "protocol name" in blind[n["go_proto"].key]
    assert (
        n["go_low"].key not in blind and n["go_fn"].key not in blind
    )  # an unexported method fits only an interface of its own package


def test_a_dead_cluster_is_corroborated_only_when_every_symbol_of_it_is():
    kinds = {"a": "func", "b": "func", "c": "class", "f": "file", "d": "func"}
    clusters = (("a", "b", "c", "f"), ("d",))
    assert verdict.whole_clusters(clusters, kinds, {"a", "b", "c"}) == {"a", "b", "c", "f"}  # a file node is no symbol
    assert verdict.whole_clusters(clusters, kinds, {"a", "b", "d"}) == {"d"}  # c is missing: a, b, c, f stay suspects
    assert verdict.whole_clusters(clusters, kinds, set()) == set()


def test_verdict_data_is_complete():
    tab = verdict.table()
    assert set(tab["codes"]) == {"dc.dead", "dc.dead-cluster", "dc.introduced-dead"} and tab["symbols"] == [
        "func",
        "method",
        "class",
    ]
    assert set(tab["engines"]) >= {"go-deadcode", "rustc", "cargo", "knip", "pmd", "roslyn", "vulture"}
    assert not {"legacy", "native", "shadow_e"} & set(
        tab
    )  # the claims of the text checker and the ledger tables are not part of it
    assert tab["lsp_blind"]["implicit"]["langs"] == ["go"] and tab["lsp_blind"]["names"]["python"]


def test_build_and_both_carry_the_engine_confidence_that_proves_nothing():
    weak = match("py:lib/chain.py#link_a/0", hit("vulture", "unused-function", "func", 60))
    other = match("py:lib/chain.py#link_b/0", hit("ruff", "F401", "import", 80))  # not a dead-symbol claim
    loose = match("py:lib/chain.py#link_c/0", hit("vulture", "unused-function", "func", 90), exact=False)
    p = verdict.build(matches=[weak, other, loose])
    assert dict(p.confs) == {"py:lib/chain.py#link_a/0": 60} and not p.nodes  # 60 % is no corroboration (min_conf 100)
    assert p.text("py:lib/chain.py#link_a/0", "lib/chain.py") == ""
    strong = verdict.build(matches=[match("py:lib/chain.py#link_a/0", hit("vulture", "unused-function", "func", 100))])
    j = verdict.both(p, strong)
    assert dict(j.confs) == {"py:lib/chain.py#link_a/0": 100} and set(j.nodes) == {"py:lib/chain.py#link_a/0"}
    assert verdict.both(p, verdict.Proof()) is p and verdict.both(verdict.Proof(), p) is p  # an empty side changes nothing
    assert verdict.Proof().confs == {} and verdict.build().confs == {}  # old callers of Proof(covered, nodes) stay valid
