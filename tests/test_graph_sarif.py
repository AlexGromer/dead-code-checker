"""graph.sarif: the findings as SARIF 2.1.0, checked against a small subset of the schema, deterministic, and wired into
`graph check --format sarif`.

python3 -m pytest -q tests/test_graph_sarif.py
"""

import json

import pytest

from dead_code_checker import __version__
from dead_code_checker.graph import sarif
from dead_code_checker.graph.model import Finding

LEVELS = {"none", "note", "warning", "error"}


def validate(doc):
    """The subset of the SARIF 2.1.0 schema that code-scanning services rely on: the log, the run, the tool, the rules, the results
    and their locations. AssertionError names what is wrong."""
    assert doc["version"] == "2.1.0" and doc["$schema"].endswith("sarif-2.1.0.json")
    assert isinstance(doc["runs"], list) and len(doc["runs"]) == 1
    run = doc["runs"][0]
    drv = run["tool"]["driver"]
    assert drv["name"] == "dead-code-checker" and drv["version"] == __version__
    ids = [r["id"] for r in drv["rules"]]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    for r in drv["rules"]:
        assert r["shortDescription"]["text"] and r["fullDescription"]["text"]
        assert r["defaultConfiguration"]["level"] in LEVELS
    for res in run["results"]:
        assert res["level"] in LEVELS and isinstance(res["message"]["text"], str) and res["message"]["text"]
        assert drv["rules"][res["ruleIndex"]]["id"] == res["ruleId"]
        for loc in res.get("locations", []):
            pl = loc["physicalLocation"]
            assert pl["artifactLocation"]["uri"] and "\\" not in pl["artifactLocation"]["uri"]
            assert pl["artifactLocation"]["uriBaseId"] == "%SRCROOT%"
            if "region" in pl:
                assert isinstance(pl["region"]["startLine"], int) and pl["region"]["startLine"] >= 1
        assert set(res) <= {"ruleId", "ruleIndex", "level", "message", "locations", "partialFingerprints", "properties", "rank"}
        if "rank" in res:  # the confidence of a dead-symbol claim (graph.confidence): the same number twice
            assert 0 <= res["rank"] <= 100 and res["properties"]["confidence"] == res["rank"]
    return run


FS = [
    Finding("W", "lib/chain.py", 3, "dc.dead-cluster", "`b` is dead", "no incoming edge"),
    Finding("E", "units/a.service", 1, "dc.root-dangling", "points into the void"),
    Finding("W", "(dead-code-checker)", 0, "dc.budget", "the budget is spent"),
    Finding("I", "lib/s p/x.py", 9, "dc.suspect", "[dc.dead] `z`", "not corroborated"),
    Finding("W", "lib/chain.py", 1, "dc.dead-cluster", "`a` is dead", "head of the chain"),
]


def test_the_document_is_valid_and_has_a_rule_per_code_and_a_result_per_finding():
    run = validate(json.loads(sarif.to_sarif(FS, {"show_suspects": True})))
    assert [r["id"] for r in run["tool"]["driver"]["rules"]] == ["dc.budget", "dc.dead-cluster", "dc.root-dangling", "dc.suspect"]
    got = [(r["ruleId"], r["level"], r.get("locations", [{}])[0].get("physicalLocation", {}).get("region", {}).get("startLine")) for r in run["results"]]
    assert got == [
        ("dc.budget", "warning", None),
        ("dc.dead-cluster", "warning", 1),
        ("dc.dead-cluster", "warning", 3),
        ("dc.suspect", "note", 9),
        ("dc.root-dangling", "error", 1),
    ]  # sorted by path, line, code
    assert sorted(r["level"] for r in run["results"]) == ["error", "note", "warning", "warning", "warning"]


def test_a_finding_about_the_run_has_no_location_and_a_path_is_percent_encoded():
    run = validate(json.loads(sarif.to_sarif(FS, {"show_suspects": True})))
    by = {r["message"]["text"]: r for r in run["results"]}
    assert "locations" not in by["the budget is spent"]
    assert by["[dc.dead] `z`"]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "lib/s%20p/x.py"
    assert by["points into the void"]["properties"]["why"].startswith("a registration that points into the void")


def test_suspects_are_hidden_unless_asked_for_like_in_every_format():
    hidden = validate(json.loads(sarif.to_sarif(FS)))
    assert "dc.suspect" not in {r["ruleId"] for r in hidden["results"]} and len(hidden["results"]) == 4
    shown = json.loads(sarif.to_sarif(FS, {"show_suspects": True}))["runs"][0]["results"]
    assert len(shown) == 5


def test_the_document_is_byte_for_byte_the_same_for_any_input_order():
    a = sarif.to_sarif(FS, {"show_suspects": True})
    b = sarif.to_sarif(list(reversed([Finding(f.level, f.path, f.line, f.code, f.msg, f.why) for f in FS])), {"show_suspects": True})
    assert a == b and a.endswith("}\n")
    for secret in ("/tmp", "/home", "T00:", "202"):
        assert secret not in a


def test_fingerprints_follow_the_finding_not_its_position_in_the_list():
    f = Finding("W", "a.py", 4, "dc.dead", "`x` is dead")
    g = Finding("W", "a.py", 40, "dc.dead", "`x` is dead")  # moved down the file
    fa = json.loads(sarif.to_sarif([f]))["runs"][0]["results"][0]["partialFingerprints"]
    fb = json.loads(sarif.to_sarif([g]))["runs"][0]["results"][0]["partialFingerprints"]
    assert fa == fb and fa["deadCodeChecker/v1"]


def test_an_empty_run_is_valid():
    run = validate(json.loads(sarif.to_sarif([])))
    assert run["results"] == [] and run["tool"]["driver"]["rules"] == []


@pytest.mark.parametrize("level,want", [("E", "error"), ("W", "warning"), ("I", "note")])
def test_levels(level, want):
    run = json.loads(sarif.to_sarif([Finding(level, "a.py", 1, "dc.x", "m")], {"show_suspects": True}))["runs"][0]
    assert run["results"][0]["level"] == want


def claim(path, line, n, note="base 40 = 40"):
    f = Finding("W", path, line, "dc.dead", f"`f{line}` is dead", "no root reaches it")
    f.confidence, f.conf_note = n, note
    return f


def test_a_claim_carries_its_confidence_as_a_property_and_as_the_rank():
    fs = [claim("a.py", 3, 85), claim("b.py", 1, 22), Finding("W", "c.py", 1, "dc.engine-missing", "no engine")]
    run = validate(json.loads(sarif.to_sarif(fs)))
    by = {r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]: r for r in run["results"]}
    assert by["a.py"]["rank"] == 85.0 and by["a.py"]["properties"]["confidence"] == 85
    assert by["a.py"]["properties"]["confidenceNote"] == "base 40 = 40" and by["a.py"]["properties"]["why"]
    assert "rank" not in by["c.py"] and "confidence" not in by["c.py"]["properties"]  # a finding without a claim has neither
    assert sarif.to_sarif(fs) == sarif.to_sarif(list(reversed(fs)))  # still byte-stable


def test_min_confidence_leaves_out_the_claims_under_it_and_keeps_a_finding_without_a_number():
    fs = [claim("a.py", 3, 85), claim("b.py", 1, 22), Finding("W", "c.py", 1, "dc.engine-missing", "no engine")]
    run = json.loads(sarif.to_sarif(fs, {"min_confidence": 50}))["runs"][0]
    assert sorted(r["ruleId"] for r in run["results"]) == ["dc.dead", "dc.engine-missing"]
    assert [r["rank"] for r in run["results"] if "rank" in r] == [85.0]
    assert json.loads(sarif.to_sarif(fs, {"min_confidence": 0})) == json.loads(sarif.to_sarif(fs))
