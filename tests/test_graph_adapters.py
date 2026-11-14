"""graph.adapters: the output parsers of the dead-code engines against RECORDINGS of the real engines
(tests/fixtures/engines/<tool>/<version>/: the sources, the raw output and meta.json with the command line, the exit code and the
version), the evidence on graph nodes, the graph-against-engine disagreement (W dc.engine-disagree) and its ledger.

python3 -m pytest -q tests/test_graph_adapters.py
No engine is started: every output is a file, so nothing here is skipped. Every recording has its sources next to it; the
cross-check test reads each reported line in the source and finds the reported name there.
"""

import ast
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from dead_code_checker.graph import adapters, engines, model, reach, report, store
from dead_code_checker.graph.adapters import AdapterError, Run

ROOT = Path(__file__).resolve().parent.parent

FIX = ROOT / "tests" / "fixtures" / "engines"


def rec(tool):
    """(directory of the one recorded version, meta)."""
    (d,) = [p for p in (FIX / tool).iterdir() if p.is_dir()]
    meta = json.loads((d / "meta.json").read_text())
    assert meta["version"] == d.name and meta["tool"] == tool
    return d, meta


def hits(tool, out, fmt=None):
    d, meta = rec(tool)
    return adapters.parse(tool, (d / out).read_text(), fmt, meta["root"], meta["version"])


def keys(hs):
    """What a recording must say: (path, line, rule, claim, name)."""
    return [(h.path, h.line, h.rule, h.claim, h.name) for h in hs]


DETEKT = [
    ("A.kt", 3, "UnusedImports", "import", "java.util.List"),
    ("A.kt", 4, "UnusedImports", "import", "java.io.File"),
    ("A.kt", 7, "UnusedPrivateProperty", "var", "unusedProp"),
    ("A.kt", 8, "UnusedPrivateMember", "func", "unusedFun"),
    ("A.kt", 8, "UnusedParameter", "param", "x"),
    ("A.kt", 9, "UnusedParameter", "param", "p"),
    ("A.kt", 12, "UnusedPrivateClass", "class", "Hidden"),
]

ROSLYN = [
    ("Program.cs", 4, "CS0169", "var", "P.unusedField"),
    ("Program.cs", 4, "IDE0051", "member", "P.unusedField"),
    ("Program.cs", 5, "IDE0051", "member", "P.Unused"),
    ("Program.cs", 6, "CS0219", "local", "x"),
]

# every recorded output: tool, out file, format, expected hits
EXPECT = [
    ("ruff", "out.json", "json", [("a.py", 1, "F401", "import", "os"), ("a.py", 6, "F841", "local", "unused_local")]),
    (
        "vulture",
        "out.txt",
        "text",
        [
            ("v.py", 5, "unused-function", "func", "dead_fn"),
            ("v.py", 9, "unused-class", "class", "DeadClass"),
            ("v.py", 10, "unused-variable", "var", "attr"),
        ],
    ),
    ("shellcheck", "out.json", "json", [("s.sh", 2, "SC2034", "var", "unused_var")]),
    ("shellcheck", "out.json1", "json1", [("s.sh", 2, "SC2034", "var", "unused_var")]),
    ("rustc", "out.jsonl", "json", [("dead.rs", 7, "dead_code", "func", "dead_fn")]),
    ("cargo", "out.jsonl", "json", [("src/main.rs", 7, "dead_code", "func", "dead_fn")]),
    (
        "staticcheck",
        "out.jsonl",
        "json",
        [("main.go", 7, "U1000", "func", "deadFn"), ("main.go", 9, "U1000", "func", "unusedHelper")],
    ),
    (
        "staticcheck",
        "out_methods.jsonl",
        "json",
        [("meth/meth.go", 5, "U1000", "func", "(*t).dead"), ("meth/meth.go", 7, "U1000", "func", "t.dead2")],
    ),
    (
        "go-deadcode",
        "out.json",
        "json",
        [("main.go", 7, "unreachable", "func", "deadFn"), ("main.go", 9, "unreachable", "func", "unusedHelper")],
    ),
    (
        "knip",
        "out.json",
        "json",
        [("orphan.ts", 0, "files", "file", "orphan.ts"), ("util.ts", 2, "exports", "export", "deadExport")],
    ),
    (
        "tsc",
        "out.txt",
        "text",
        [
            ("a.ts", 1, "TS6133", "local", "x"),
            ("a.ts", 2, "TS6133", "local", "a"),
            ("a.ts", 2, "TS6133", "local", "unused"),
        ],
    ),
    (
        "pmd",
        "out.xml",
        "xml",
        [
            ("A.java", 4, "UnusedPrivateField", "var", "unusedField"),
            ("A.java", 5, "UnusedPrivateMethod", "method", "unusedMethod"),
        ],
    ),
    (
        "pmd",
        "out.json",
        "json",
        [
            ("A.java", 4, "UnusedPrivateField", "var", "unusedField"),
            ("A.java", 5, "UnusedPrivateMethod", "method", "unusedMethod"),
        ],
    ),
    ("checkstyle", "out.xml", "xml", [("A.java", 1, "UnusedImportsCheck", "import", "java.util.List")]),
    ("detekt", "out.xml", "xml", DETEKT),
    ("detekt", "out.sarif", "sarif", DETEKT),
    (
        "tflint",
        "out.json",
        "json",
        [
            ("main.tf", 1, "terraform_unused_declarations", "tfvar", "unused_var"),
            ("main.tf", 6, "terraform_unused_declarations", "var", "unused_local"),
        ],
    ),
    ("deadvalues", "out.json", "json", [("my.yaml", 2, "DEAD", "cfgkey", "unusedKey")]),
    ("deadvalues", "out.sarif", "sarif", [("my.yaml", 2, "dead", "cfgkey", "unusedKey")]),
    ("cppcheck", "out.xml", "xml", [("c.cpp", 1, "unusedFunction", "func", "unused_fn")]),
    ("clang-tidy", "out.txt", "text", [("t.cpp", 2, "misc-unused-parameters", "param", "unusedParam")]),
    ("PSScriptAnalyzer", "out.json", "json", [("x.ps1", 2, "PSUseDeclaredVarsMoreThanAssignments", "local", "unused")]),
    ("roslyn", "out.sarif", "sarif", ROSLYN),
    ("roslyn", "out.txt", "msbuild", ROSLYN),
]
IDS = [f"{t}-{o}" for t, o, _, _ in EXPECT]


# ---------------------------------------------------------------- the table
def test_table_matches_the_registry_and_the_recordings():
    reg = engines.load_registry()
    tab = adapters.table()
    assert not (set(tab["no_dispute"]) - set(tab["claim_nodes"]))
    for kinds in tab["claim_nodes"].values():
        assert set(kinds) <= set(model.NODE_KINDS)
    plain = {"import", "local", "param", "dep"}
    for tool, t in tab["tools"].items():
        assert t.get("engine", tool) in reg["engines"] or t.get("engine", tool) is None, tool
        if t.get("unrecorded"):  # no recording yet: the data says why (a reason, not a gap)
            assert len(t["unrecorded"]) > 40, tool
        else:
            assert tool in {e[0] for e in EXPECT}, f"{tool} has no recording test"
        for pat, kind in t["claims"]:
            re.compile(pat)
            for k in kind.values() if isinstance(kind, dict) else [kind]:
                assert k in tab["claim_nodes"] or k in plain, (tool, pat, k)
        for name, spec in t["formats"].items():
            assert (spec["kind"] if "kind" in spec else tab["templates"][spec["use"]]["kind"]) in (
                "json",
                "text",
                "xml",
            )
            assert t.get("unrecorded") or any(e[0] == tool and e[2] == name for e in EXPECT), (
                f"{tool} format {name} has no recording test"
            )
        assert t["default"] in t["formats"]


def test_every_recording_is_clean_and_tied_to_its_sources():
    for tool in sorted(p.name for p in FIX.iterdir() if p.is_dir()):
        d, meta = rec(tool)
        assert meta["root"] == "/work" and meta["files"] and meta["runs"]
        for f in meta["files"]:
            assert (d / f).is_file(), (tool, f)
        for run in meta["runs"]:
            assert run["argv"][0] and "/" not in run["argv"][0]  # the engine by name, not by a host path
            text = (d / run["out"]).read_text()
            assert "rec-" not in text and str(ROOT) not in text, f"{tool}/{run['out']} carries a host path"
        net = {"--download", "--install", "install", "fetch", "get", "update"}
        assert not (net & set(meta["runs"][0]["argv"][1:])), tool


# ---------------------------------------------------------------- contract tests on the recordings
@pytest.mark.parametrize("tool,out,fmt,want", EXPECT, ids=IDS)
def test_recording_parses_to_the_expected_claims(tool, out, fmt, want):
    got = hits(tool, out, fmt)
    assert keys(got) == want
    d, meta = rec(tool)
    assert {h.tool for h in got} == {tool} and {h.ver for h in got} == {meta["version"]}
    assert all(h.col >= 0 and h.msg == " ".join(h.msg.split()) for h in got)


@pytest.mark.parametrize("tool,out,fmt,want", EXPECT, ids=IDS)
def test_reported_line_in_the_recorded_source_shows_the_name(tool, out, fmt, want):
    """Output and sources are one recording: the engine's line number points at a line that has the engine's name."""
    d, _ = rec(tool)
    for path, line, rule, claim, name in want:
        if not line or claim == "file":
            continue
        src = (d / path).read_text().splitlines()[line - 1]
        tail = re.split(r"[.:]", name)[-1]
        assert tail in src, (tool, path, line, name, src)


def test_format_variants_of_one_engine_agree():
    def core(hs):
        return [(h.path, h.line, h.rule.lower(), h.claim, h.name) for h in hs]

    assert hits("pmd", "out.xml", "xml") == hits("pmd", "out.json", "json")
    assert hits("shellcheck", "out.json", "json") == hits("shellcheck", "out.json1", "json1")
    assert core(hits("deadvalues", "out.json", "json")) == core(hits("deadvalues", "out.sarif", "sarif"))
    assert core(hits("detekt", "out.xml", "xml")) == core(hits("detekt", "out.sarif", "sarif"))
    d, meta = rec("roslyn")
    txt = adapters.parse("roslyn", (d / "out.txt").read_text(), "msbuild", meta["root"], meta["version"])
    assert core(txt) == core(hits("roslyn", "out.sarif", "sarif"))
    assert [h[:2] for h in core(hits("cargo", "out.jsonl", "json"))] == [("src/main.rs", 7)]


def test_default_format_and_keep_all():
    d, meta = rec("ruff")
    text = (d / "out.json").read_text()
    assert adapters.parse("ruff", text, root="/work") == adapters.parse("ruff", text, "json", "/work")
    d, meta = rec("tflint")
    text = (d / "out.json").read_text()
    only = adapters.parse("tflint", text)
    every = adapters.parse("tflint", text, keep_all=True)
    assert len(only) == 2 and len(every) == 3
    assert [h.rule for h in every if not h.claim] == ["terraform_required_version"]
    assert (
        hits("detekt", "out.xml", "xml")
        and len(adapters.parse("detekt", (rec("detekt")[0] / "out.xml").read_text(), keep_all=True)) == 12
    )


def test_detekt_recording_from_the_p0_unit_is_still_readable():
    """report.xml was recorded by the detekt unit without /work paths; same format, the relative names stay as they are."""
    d, _ = rec("detekt")
    got = adapters.parse("detekt", (d / "report.xml").read_text(), "xml", "/work")
    assert {h.rule for h in got} >= {"UnusedImports", "UnusedPrivateMember", "UnusedPrivateClass"}
    assert all(h.path == "A.kt" for h in got)


def test_vulture_confidence_and_order_independence():
    d, meta = rec("vulture")
    text = (d / "out.txt").read_text()
    got = adapters.parse("vulture", text, root="/work")
    assert {h.conf for h in got} == {60}
    assert adapters.parse("vulture", "\n".join(reversed(text.splitlines())), root="/work") == got
    assert (
        adapters.parse(
            "vulture",
            "v.py:3: unused import 'os' (90% confidence)\nv.py:9: unreachable code after 'return' (100% confidence)\n",
        )[0].conf
        == 90
    )


# ---------------------------------------------------------------- reading, never guessing
def test_unreadable_output_is_an_error_not_an_empty_list():
    with pytest.raises(AdapterError, match="not JSON"):
        adapters.parse("ruff", "ruff: panic")
    with pytest.raises(AdapterError, match="not JSON"):
        adapters.parse("staticcheck", '{"code": "U1000",')
    with pytest.raises(AdapterError, match="not XML"):
        adapters.parse("pmd", "<pmd><file", "xml")
    with pytest.raises(AdapterError, match="entity"):
        adapters.parse("pmd", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><pmd/>', "xml")
    with pytest.raises(AdapterError, match="no adapter"):
        adapters.parse("nope", "")
    with pytest.raises(AdapterError, match="no format"):
        adapters.parse("ruff", "[]", "xml")


def test_clean_runs_and_noise_are_not_errors():
    assert adapters.parse("ruff", "[]") == [] and adapters.parse("ruff", "  \n") == []
    assert adapters.parse("go-deadcode", "null") == [] and adapters.parse("go-deadcode", "") == []
    assert adapters.parse("staticcheck", "") == [] and adapters.parse("tsc", "Found 0 errors.\n") == []
    assert (
        adapters.parse("rustc", "warning: 1 warning emitted\n") == []
    )  # not JSON lines: rustc prints them without --error-format
    assert adapters.parse("clang-tidy", "1 warning generated.\n    2 | int f(int x)\n") == []
    # a diagnostic without a file (a summary, a compile error of the build itself) is not a hit
    assert (
        adapters.parse("staticcheck", '{"code":"compile","location":{"file":"","line":0,"column":0},"message":"x"}')
        == []
    )
    # PSScriptAnalyzer prints one object, not a list, for a single finding
    one = '{"RuleName":"PSReviewUnusedParameter","Line":3,"Column":9,"Message":"The parameter \'p\' has been declared but not used.","ScriptPath":"/w/a.ps1"}'
    assert keys(adapters.parse("PSScriptAnalyzer", one, root="/w")) == [
        ("a.ps1", 3, "PSReviewUnusedParameter", "param", "p")
    ]


def json_formats():
    tab = adapters.table()
    for tool, t in sorted(tab["tools"].items()):
        for fmt, spec in sorted(t["formats"].items()):
            spec = {**tab["templates"][spec["use"]], **spec} if "use" in spec else spec
            if spec["kind"] == "json":
                yield tool, fmt, spec


@pytest.mark.parametrize("tool,fmt,spec", list(json_formats()), ids=lambda v: v if isinstance(v, str) else "")
def test_json_of_another_shape_is_an_error_never_an_empty_list(tool, fmt, spec):
    bad = ['{"error":"boom"}', '{"zzz":[]}']
    if not spec.get("lines"):  # in a lines format only an object line counts, the rest is noise
        bad += ['"str"', "42", "true", "[1,2,3]", '["a"]'] + (["[]", "null"] if spec["top"] == "object" else [])
    for text in bad:
        with pytest.raises(AdapterError, match="wrong shape"):
            adapters.parse(tool, text, fmt)
    if spec["top"] == "list":  # the empty answer of a list engine is a clean run
        assert adapters.parse(tool, "[]", fmt) == [] and adapters.parse(tool, "null", fmt) == []


def test_a_format_mismatch_is_an_error_not_a_lost_finding():
    def text(tool, out):
        return (rec(tool)[0] / out).read_text()

    with pytest.raises(AdapterError, match="wrong shape"):
        adapters.parse("shellcheck", text("shellcheck", "out.json1"), "json")  # json1 is an object with comments
    with pytest.raises(AdapterError, match="wrong shape"):
        adapters.parse("shellcheck", text("shellcheck", "out.json"), "json1")
    with pytest.raises(AdapterError, match="wrong shape"):
        adapters.parse("deadvalues", text("deadvalues", "out.sarif"), "json")
    with pytest.raises(AdapterError, match="wrong shape"):
        adapters.parse("deadvalues", text("deadvalues", "out.json"), "sarif")
    with pytest.raises(AdapterError, match="not XML"):
        adapters.parse("pmd", text("pmd", "out.json"), "xml")
    with pytest.raises(AdapterError, match="not JSON"):
        adapters.parse("pmd", text("pmd", "out.xml"), "json")
    with pytest.raises(AdapterError, match="wrong shape"):
        adapters.parse("pmd", text("checkstyle", "out.xml"), "xml")  # another XML report: the root says so
    with pytest.raises(AdapterError, match="wrong shape"):
        adapters.parse("cppcheck", "<pmd/>")
    assert adapters.parse("pmd", "<pmd/>") == [] and adapters.parse("cppcheck", "<results><errors/></results>") == []


def test_an_error_the_engine_reports_itself_is_an_error():
    d, meta = rec("tflint")
    doc = json.loads((d / "out.json").read_text())
    assert doc["errors"] == [] and adapters.parse("tflint", json.dumps(doc))  # the real run: findings, no errors
    doc["errors"] = [{"message": "Failed to load configuration:\n  bad", "severity": "error"}]
    with pytest.raises(AdapterError, match="reports an error: Failed to load configuration: bad"):
        adapters.parse("tflint", json.dumps(doc))
    with pytest.raises(AdapterError, match="reports an error: boom"):
        adapters.parse("tflint", '{"issues":[],"errors":[{"message":"boom"}]}')


def test_lines_of_other_kinds_between_the_diagnostics_do_not_hide_them():
    d, meta = rec("rustc")
    text = (d / "out.jsonl").read_text()
    want = adapters.parse("rustc", text, root="/work")
    assert want
    noise = '{"$message_type":"artifact","artifact":"x.rmeta","emit":"metadata"}\nnote: a plain line\n'
    assert adapters.parse("rustc", noise + text, root="/work") == want
    assert adapters.parse("rustc", text + '{"error":"x"}\n', root="/work") == want
    with pytest.raises(AdapterError, match="wrong shape"):
        adapters.parse("rustc", '{"error":"x"}\n', root="/work")


def test_exit_codes_of_a_healthy_run():
    tab = adapters.table()["tools"]
    assert adapters.rc_ok("ruff", 0) and adapters.rc_ok("ruff", 1) and not adapters.rc_ok("ruff", 2)
    assert adapters.rc_ok("vulture", 3) and not adapters.rc_ok("vulture", 1)
    assert adapters.rc_ok("ruff", None) and adapters.rc_ok("detekt", 99) and adapters.rc_ok("nope", 5)  # not checked
    seen = 0
    for tool in tab:  # every exit code of every recording is a healthy one
        if tab[tool].get("unrecorded"):
            continue
        for run in rec(tool)[1]["runs"]:
            if "ok_rc" in tab[tool]:
                assert adapters.rc_ok(tool, run["rc"]), (tool, run)
                seen += 1
    assert seen >= 12
    assert not {"checkstyle", "detekt", "roslyn"} & {t for t in tab if "ok_rc" in tab[t]}  # no recording-proven list


def test_a_failed_run_is_one_warning_and_says_nothing_else(tmp_path):
    g = go_graph()
    res = reach.analyze(g)
    d, meta = rec("go-deadcode")
    only_one = (
        (d / "out.json")
        .read_text()
        .replace('"Name": "unusedHelper"', '"Name": "other"')
        .replace('"Line": 9', '"Line": 99')
    )

    def run(text, rc=None):
        return Run("go-deadcode", text, meta["version"], "json", meta["root"], ("main.go",), rc)

    assert [x.side for x in adapters.reconcile(g, [run(only_one, 0)], res).disagreements] == ["engine-alive"]  # control
    r = adapters.reconcile(g, [run(only_one, 2)], res)
    assert r.disagreements == () and r.matches == ()
    assert [(f.level, f.code) for f in r.findings] == [("W", "dc.engine-output")] and "exit code 2" in r.findings[0].msg
    # garbage that parses as "nothing unreachable" must not read as an engine that analysed the file and found it alive
    ledger = tmp_path / "ledger.jsonl"
    r = adapters.reconcile(g, [run('{"error":"packages.Load failed"}')], res, str(ledger))
    assert r.disagreements == () and not ledger.exists()
    assert [f.code for f in r.findings] == ["dc.engine-output"] and "wrong shape" in r.findings[0].msg
    r = adapters.reconcile(g, [run('{"issues":[]}')], res)
    assert r.disagreements == () and [f.code for f in r.findings] == ["dc.engine-output"]


def test_a_text_engine_crash_is_told_by_its_exit_code():
    crash = 'Traceback (most recent call last):\n  File "x", line 1\nSyntaxError: bad\n'
    assert adapters.parse("vulture", crash) == []  # the text alone cannot tell: this is why Run has rc
    g = graph_for_python()
    r = adapters.reconcile(g, [Run("vulture", crash, "2.14", "text", "/w", (), 1)], Res({}))
    assert [(f.level, f.code) for f in r.findings] == [("W", "dc.engine-output")]
    assert "exit code 1" in r.findings[0].msg and "vulture" in r.findings[0].msg
    assert adapters.reconcile(g, [Run("vulture", "", "2.14", "text", "/w", (), 0)], Res({})).findings == ()


def test_tsc_error_without_a_file_is_an_engine_failure_not_a_clean_run():
    """rc 1 is also the exit code of a run with findings, so the recorded line (real tsc: -p on a missing path) tells."""
    d, meta = rec("tsc")
    text = (d / "out_error.txt").read_text()
    assert text.startswith("error TS5058:") and "/work/nodir" in text
    assert [r["rc"] for r in meta["runs"] if r["out"] == "out_error.txt"] == [1]
    with pytest.raises(AdapterError, match="reports an error: error TS5058"):
        adapters.parse("tsc", text, root="/work")
    r = adapters.reconcile(graph_for_python(), [Run("tsc", text, meta["version"], "text", "/work", (), 1)], Res({}))
    assert [(f.level, f.code) for f in r.findings] == [("W", "dc.engine-output")] and r.matches == ()
    assert adapters.parse("tsc", (d / "out.txt").read_text(), root="/work")  # the findings run is untouched


@pytest.mark.parametrize(
    "path,root,want",
    [
        ("/work/a/b.py", "/work", "a/b.py"),
        ("/work/a/b.py", "/work/", "a/b.py"),
        ("file:///work/a.kt", "/work", "a.kt"),
        ("file:///C:/w/a.cs", "C:/w", "a.cs"),
        ("C:\\w\\src\\a.cs", "C:/w", "src/a.cs"),
        ("./src/../a.py", "", "a.py"),
        ("/elsewhere/a.py", "/work", "/elsewhere/a.py"),
        ("/workshop/a.py", "/work", "/workshop/a.py"),
        ("", "/work", ""),
    ],
)
def test_paths_are_relative_to_the_project_root(path, root, want):
    assert adapters._rel(path, root) == want


def test_field_paths():
    doc = {"a": [{"b": 1, "ok": True}, {"b": 2}], "c": {"d": "x"}, "n": None}
    assert adapters._walk(doc, "c.d") == ["x"]
    assert adapters._walk(doc, "a[].b") == [1, 2]
    assert adapters._walk(doc, "a[ok].b") == [1]
    assert adapters._walk(doc, "n") == [] and adapters._walk(doc, "zz.y") == []
    assert adapters._walk([1, 2], "[]") == [1, 2] and adapters._walk({"x": 1}, "[]") == [{"x": 1}]
    assert adapters._walk(doc, "") == [doc]
    spec = {"f": "=const", "g": "@x|@y", "h": "^k"}
    assert adapters._field(spec, "f", {}, {}, adapters._jget) == "const"
    assert adapters._field(spec, "h", {"k": 1}, {"k": 2}, adapters._jget) == 2


# ---------------------------------------------------------------- evidence on nodes
def graph_for_python(with_attr=True):
    n = model.node
    ns = [
        n("python:v.py", "file", line0=1, line1=1),
        n("python:v.py#used", "func", line0=1, line1=2),
        n("python:v.py#dead_fn", "func", line0=5, line1=6),
        n("python:v.py#DeadClass", "class", line0=9, line1=10),
    ]
    if with_attr:
        ns.append(n("python:v.py#DeadClass.attr", "var", line0=10, line1=10))
    ev = (model.evidence("py", "v.py", 12, "used()", "py@1"),)
    return model.canonical(
        ns,
        [model.edge("python:v.py", "python:v.py#used", "call", "T", "v.py:12", 0)],
        [model.root("main", node="python:v.py", origin="v.py:1")],
        ev,
    )


def vulture_hits():
    return hits("vulture", "out.txt", "text")


def test_attach_ties_every_hit_to_the_node_with_that_name():
    g0 = graph_for_python()
    g, ms = adapters.attach(g0, vulture_hits())
    assert [(m.node.split("#")[1], m.exact) for m in ms] == [
        ("dead_fn", True),
        ("DeadClass", True),
        ("DeadClass.attr", True),
    ]
    ev = g.evid[ms[0].ev]
    assert ev == model.Evidence("vulture", "v.py", 5, "unused-function dead_fn", "vulture@2.14")
    assert len(g.evid) == len(g0.evid) + 3
    # the evidence of the graph before is still what its edges point at
    assert g.evid[g.edges[0].ev] == g0.evid[g0.edges[0].ev] and (g.nodes, g.roots) == (g0.nodes, g0.roots)


def test_attach_without_the_named_node_keeps_the_hit_as_a_clue_on_the_narrowest_node():
    g, ms = adapters.attach(graph_for_python(with_attr=False), vulture_hits())
    attr = [m for m in ms if m.hit.name == "attr"]
    assert [(m.node, m.exact) for m in attr] == [("python:v.py#DeadClass", False)]


def test_attach_file_claim_and_unknown_files():
    n = model.node
    g0 = model.canonical(
        [
            n("js:orphan.ts", "file", line0=1),
            n("js:util.ts", "file", line0=1),
            n("js:util.ts#deadExport", "func", line0=2, line1=2),
        ]
    )
    g, ms = adapters.attach(g0, hits("knip", "out.json", "json"))
    got = {(m.node, m.hit.claim, m.exact) for m in ms}
    assert got == {("js:orphan.ts", "file", True), ("js:util.ts#deadExport", "export", True)}
    _, none = adapters.attach(g0, hits("vulture", "out.txt", "text"))
    assert none == []


def test_attach_is_deterministic_and_does_not_grow_the_graph_on_nothing():
    g0 = graph_for_python()
    hs = vulture_hits()
    a, _ = adapters.attach(g0, hs)
    b, _ = adapters.attach(g0, list(reversed(hs)) + hs)
    assert a.digest() == b.digest()
    assert adapters.attach(g0, [])[0].digest() == g0.digest()


def test_payload_round_trip_in_the_store(tmp_path):
    _, ms = adapters.attach(graph_for_python(), vulture_hits())
    payload = adapters.to_payload(ms)
    assert adapters.to_payload(reversed(ms)) == payload
    assert [(n, e, h) for n, e, h in adapters.from_payload(payload)] == sorted((m.node, m.exact, m.hit) for m in ms)
    with store.Store(str(tmp_path / "d" / "u" / "r" / "graph.sqlite")) as st:
        st.put_engine_result("k", "vulture", payload)
        assert adapters.from_payload(st.get_engine_result("k"))[0][2].tool == "vulture"


# ---------------------------------------------------------------- graph against engine
class Res:
    """What disagree needs of reach.Result."""

    def __init__(self, verdicts, excluded=()):
        self.v, self.x = verdicts, set(excluded)

    def verdict(self, key):
        return self.v.get(key, "unknown")

    def excluded(self, key):
        return key in self.x


def test_graph_alive_against_an_exact_claim_is_a_disagreement():
    g, ms = adapters.attach(graph_for_python(), vulture_hits())
    res = Res(
        {"python:v.py#dead_fn": "alive", "python:v.py#DeadClass": "dead", "python:v.py#DeadClass.attr": "weak-alive"}
    )
    dis = adapters.disagree(g, ms, res)
    assert [(d.side, d.node.split("#")[1], d.verdict, d.rule) for d in dis] == [
        ("graph-alive", "DeadClass.attr", "weak-alive", "unused-variable"),
        ("graph-alive", "dead_fn", "alive", "unused-function"),
    ]


@pytest.mark.parametrize("verdict", ["dead", "dead-cluster", "test-only", "doc-only", "public-unused", "unknown"])
def test_no_disagreement_when_graph_and_engine_both_find_it_unused(verdict):
    g, ms = adapters.attach(graph_for_python(), vulture_hits())
    assert adapters.disagree(g, ms, Res({m.node: verdict for m in ms})) == []


def test_only_claims_about_the_node_itself_can_dispute():
    n = model.node
    g0 = model.canonical([n("js:util.ts", "file", line0=1), n("js:util.ts#deadExport", "func", line0=2, line1=2)])
    g, ms = adapters.attach(g0, hits("knip", "out.json", "json"))
    alive = Res({m.node: "alive" for m in ms})
    assert adapters.disagree(g, ms, alive) == []  # an unused export (and the file claim of another path) is a clue only
    py = model.node
    g1 = model.canonical([py("python:a.py", "file", line0=1), py("python:a.py#f", "func", line0=3, line1=8)])
    g, ms = adapters.attach(g1, hits("ruff", "out.json", "json"))  # import and local claims
    assert ms and adapters.disagree(g, ms, Res({"python:a.py#f": "alive", "python:a.py": "alive"})) == []


def test_a_clue_that_is_not_exact_never_disputes():
    g, ms = adapters.attach(graph_for_python(with_attr=False), vulture_hits())
    res = Res({m.node: "alive" for m in ms})
    assert {d.name for d in adapters.disagree(g, ms, res)} == {
        "dead_fn",
        "DeadClass",
    }  # attr sat on DeadClass: not exact


def go_graph():
    n = model.node
    ns = [
        n("go:main.go", "file", line0=1),
        n("go:main.go#main", "func", line0=3, line1=3),
        n("go:main.go#used", "func", line0=5, line1=5),
        n("go:main.go#deadFn", "func", line0=7, line1=7),
        n("go:main.go#unusedHelper", "func", line0=9, line1=9),
    ]
    es = [
        model.edge("go:main.go#main", "go:main.go#used", "call"),
        model.edge("go:main.go#unusedHelper", "go:main.go#deadFn", "call"),
    ]
    return model.canonical(ns, es, [model.root("main", node="go:main.go#main", origin="main.go:3")])


def test_reach_engine_silence_on_a_covered_file_is_a_disagreement():
    g = go_graph()
    res = reach.analyze(g)
    assert res.verdict("go:main.go#deadFn") == "dead-cluster" and res.verdict("go:main.go#used") == "alive"
    d, meta = rec("go-deadcode")
    text = (d / "out.json").read_text()
    both = [Run("go-deadcode", text, meta["version"], "json", meta["root"], ("main.go",))]
    assert adapters.reconcile(g, both, res).disagreements == ()
    only_one = text.replace('"Name": "unusedHelper"', '"Name": "other"').replace('"Line": 9', '"Line": 99')
    r = adapters.reconcile(g, [Run("go-deadcode", only_one, meta["version"], "json", meta["root"], ("main.go",))], res)
    assert [(x.side, x.node, x.tool, x.ver) for x in r.disagreements] == [
        ("engine-alive", "go:main.go#unusedHelper", "go-deadcode", "v0.52.0")
    ]
    # the engine analysed nothing (covered empty) or another file: it says nothing about the graph
    assert adapters.reconcile(g, [Run("go-deadcode", only_one, "v0.52.0", "json", "/work")], res).disagreements == ()
    assert (
        adapters.reconcile(
            g, [Run("go-deadcode", only_one, "v0.52.0", "json", "/work", ("other.go",))], res
        ).disagreements
        == ()
    )
    # a generated or test node is not reported, and a tool that is not a reach engine cannot be silent
    assert (
        adapters.disagree(
            g,
            [],
            Res({"go:main.go#unusedHelper": "dead"}, {"go:main.go#unusedHelper"}),
            {"go-deadcode": ("v", ["main.go"])},
        )
        == []
    )
    assert adapters.disagree(g, [], Res({"go:main.go#unusedHelper": "dead"}), {"staticcheck": ("v", ["main.go"])}) == []


def test_a_run_below_the_root_puts_its_paths_below_the_root_and_several_runs_of_one_engine_add_up_their_covered_files():
    """The nightly audit runs an engine once per module (dc.jobs): its paths are relative to the module, its covered files
    are named from the root, and a second module must not make the first one's files forget what was analysed."""
    n = model.node
    ns = [
        n("go:svc/main.go", "file", line0=1),
        n("go:svc/main.go#main", "func", line0=3, line1=3),
        n("go:svc/main.go#unusedHelper", "func", line0=9, line1=9),
        n("go:svc/main.go#deadFn", "func", line0=7, line1=7),
        n("go:other/x.go", "file", line0=1),
    ]
    es = [model.edge("go:svc/main.go#unusedHelper", "go:svc/main.go#deadFn", "call")]
    g = model.canonical(ns, es, [model.root("main", node="go:svc/main.go#main", origin="svc/main.go:3")])
    res = reach.analyze(g)
    d, meta = rec("go-deadcode")
    only_one = (
        (d / "out.json")
        .read_text()
        .replace('"Name": "unusedHelper"', '"Name": "other"')
        .replace('"Line": 9', '"Line": 99')
    )
    svc = Run("go-deadcode", only_one, "v0.52.0", "json", meta["root"], ("svc/main.go",), 0, "svc")
    other = Run("go-deadcode", "[]", "v0.52.0", "json", "/elsewhere", ("other/x.go",), 0, "other")
    for runs in ([svc], [svc, other], [other, svc]):  # whatever order the module jobs finished in
        r = adapters.reconcile(g, runs, res)
        assert [(x.side, x.node) for x in r.disagreements] == [("engine-alive", "go:svc/main.go#unusedHelper")]
    full = (d / "out.json").read_text()
    hits = adapters.parse("go-deadcode", full, None, meta["root"], "v0.52.0")
    assert {h.path for h in hits} == {"main.go"}  # what the engine prints is relative to the directory it ran in
    mine = adapters.reconcile(g, [svc._replace(text=full)], res).matches
    assert {m.hit.path for m in mine} == {"svc/main.go"}  # ... and the run says where that directory is
    assert adapters.reconcile(g, [svc._replace(text=full, prefix="")], res).matches == ()  # without it no node is found


def test_reconcile_end_to_end_with_a_real_reach_result_and_the_ledger(tmp_path):
    g = go_graph()
    # the graph reads a name edge main -> deadFn (tier N): weak-alive, while staticcheck and go-deadcode both find it unused
    g = model.canonical(
        g.nodes, [*g.edges, model.edge("go:main.go#main", "go:main.go#deadFn", "call", "N", "main.go:3")], g.roots
    )
    res = reach.analyze(g)
    assert res.verdict("go:main.go#deadFn") == "weak-alive"
    runs = []
    for tool, out in (("go-deadcode", "out.json"), ("staticcheck", "out.jsonl")):
        d, meta = rec(tool)
        runs.append(
            Run(
                tool,
                (d / out).read_text(),
                meta["version"],
                "json",
                meta["root"],
                ("main.go",) if tool == "go-deadcode" else (),
            )
        )
    runs.append(Run("ruff", "ruff: crashed"))
    ledger = str(tmp_path / "led" / "ledger.jsonl")
    r = adapters.reconcile(g, runs, res, ledger)
    assert [(d.tool, d.node, d.verdict) for d in r.disagreements] == [
        ("go-deadcode", "go:main.go#deadFn", "weak-alive"),
        ("staticcheck", "go:main.go#deadFn", "weak-alive"),
    ]
    codes = [(f.level, f.code) for f in r.findings]
    assert codes == [("W", "dc.engine-output"), ("W", "dc.engine-disagree"), ("W", "dc.engine-disagree")]
    f = r.findings[1]
    assert f.path == "main.go" and f.line == 7 and "weak-alive" in f.msg and "go-deadcode" in f.msg
    assert "stays with the graph" in f.why and f.render().startswith(
        "dead-code-checker: W main.go:7: dc.engine-disagree:"
    )
    assert len(r.matches) == 4 and len(r.graph.evid) == len(g.evid) + 4
    lines = Path(ledger).read_text().splitlines()
    assert lines == sorted(lines) and len(lines) == 2
    assert json.loads(next(x for x in lines if '"go-deadcode"' in x)) == {
        "kind": "engine-disagree",
        "side": "graph-alive",
        "node": "go:main.go#deadFn",
        "verdict": "weak-alive",
        "tool": "go-deadcode",
        "ver": "v0.52.0",
        "rule": "unreachable",
        "path": "main.go",
        "line": 7,
        "name": "deadFn",
        "claim": "func",
    }
    # the same run again writes nothing new, a new disagreement appends one line
    again = adapters.reconcile(g, runs, res, ledger)
    assert [f.as_dict() for f in again.findings] == [f.as_dict() for f in r.findings] and Path(
        ledger
    ).read_text().splitlines() == lines
    assert adapters.ledger_append(ledger, [adapters.ledger_record(r.disagreements[0])]) == 0
    assert (
        adapters.ledger_append(ledger, [{"kind": "engine-disagree", "node": "x"}]) == 1
        and len(Path(ledger).read_text().splitlines()) == 3
    )


def test_ledger_file_is_private_and_refuses_a_symlink(tmp_path):
    p = tmp_path / "d" / "ledger.jsonl"
    assert adapters.ledger_append(str(p), []) == 0 and not p.exists()
    assert adapters.ledger_append(str(p), [{"b": 1, "a": 2}]) == 1
    assert p.read_text() == '{"a":2,"b":1}\n'
    if os.name != "nt":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(p.parent.stat().st_mode) == 0o700
        link = tmp_path / "d" / "link.jsonl"
        link.symlink_to(p)
        with pytest.raises(OSError):
            adapters.ledger_append(str(link), [{"c": 1}])
        assert p.read_text() == '{"a":2,"b":1}\n'


def test_go_methods_are_matched_by_name_whatever_the_receiver_is():
    """Real staticcheck writes `func (*t).dead is unused` and `func t.dead2 is unused` (recording out_methods.jsonl)."""
    got = hits("staticcheck", "out_methods.jsonl", "json")
    assert [h.name for h in got] == ["(*t).dead", "t.dead2"] and {h.claim for h in got} == {"func"}
    assert [adapters._tail(n) for n in ("(*t).dead", "t.dead2", "(T).m", "pkg.(*T).m", "f()", "a::b", "")] == [
        "dead",
        "dead2",
        "m",
        "m",
        "f",
        "b",
        "",
    ]
    n = model.node
    ns = [
        n("go:meth/meth.go", "file", line0=1),
        n("go:meth/meth.go#t.dead", "method", line0=5, line1=5),
        n("go:meth/meth.go#t.dead2", "method", line0=7, line1=7),
        n("go:meth/meth.go#t.used", "method", line0=9, line1=9),
    ]
    g, ms = adapters.attach(model.canonical(ns, [], []), got)
    assert [(m.node, m.exact) for m in ms] == [("go:meth/meth.go#t.dead", True), ("go:meth/meth.go#t.dead2", True)]
    res = Res({"go:meth/meth.go#t.dead": "alive", "go:meth/meth.go#t.dead2": "weak-alive"})
    dis = adapters.disagree(g, ms, res)
    assert [(d.side, d.node, d.verdict, d.name) for d in dis] == [
        ("graph-alive", "go:meth/meth.go#t.dead", "alive", "(*t).dead"),
        ("graph-alive", "go:meth/meth.go#t.dead2", "weak-alive", "t.dead2"),
    ]


def test_parallel_writers_never_duplicate_a_ledger_line(tmp_path):
    ledger = tmp_path / "x" / "y" / "ledger.jsonl"
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); from dead_code_checker.graph import adapters; "
        "print(adapters.ledger_append(sys.argv[2], [{'n': i} for i in range(60)]))"
    )
    procs = [
        subprocess.Popen([sys.executable, "-I", "-c", code, str(ROOT), str(ledger)], stdout=subprocess.PIPE, text=True)
        for _ in range(6)
    ]
    written = [int(p.communicate()[0]) for p in procs]
    assert all(p.returncode == 0 for p in procs)
    lines = ledger.read_text().splitlines()
    assert len(lines) == 60 and len(set(lines)) == 60 and sum(written) == 60


def test_the_ledger_is_read_and_appended_under_one_lock(tmp_path, monkeypatch):
    """The processes above rarely collide on a small file, so the lock itself is pinned: taken once, before the read."""
    p = tmp_path / "ledger.jsonl"
    seen = []
    real = adapters._lock
    monkeypatch.setattr(adapters, "_lock", lambda fd: (seen.append(p.read_text() if p.exists() else None), real(fd)))
    assert adapters.ledger_append(str(p), [{"a": 1}]) == 1 and adapters.ledger_append(str(p), [{"a": 1}, {"b": 2}]) == 1
    assert seen == ["", '{"a":1}\n']


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_every_directory_the_ledger_creates_is_private(tmp_path):
    p = tmp_path / "a" / "b" / "c" / "ledger.jsonl"
    old = os.umask(0o022)
    try:
        assert adapters.ledger_append(str(p), [{"k": 1}]) == 1
    finally:
        os.umask(old)
    for d in (p.parent, p.parent.parent, p.parent.parent.parent):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


def test_disagreement_findings_have_a_default_why_and_a_code():
    assert "dc.engine-disagree" in report.DEFAULT_WHY and "dc.engine-output" in report.DEFAULT_WHY
    f = report.ensure_why(
        adapters.to_findings(
            [adapters.Disagreement("engine-alive", "go:a.go#f", "dead", "go-deadcode", "", "", "a.go", 3, "f", "func")]
        )[0]
    )
    assert f.code == "dc.engine-disagree" and "does not list it as unreachable" in f.msg and "go-deadcode@?" in f.why


# ---------------------------------------------------------------- hygiene
def test_adapters_imports_only_the_standard_library():
    tree = ast.parse((ROOT / "dead_code_checker" / "graph" / "adapters.py").read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and not n.level:
            mods.add((n.module or "").split(".")[0])
    assert mods <= set(sys.stdlib_module_names), mods
    assert not ({"deadcode", "subprocess", "socket", "urllib"} & mods)  # parsing runs nothing and opens no connection
