"""graph.sarif: the findings of a run as SARIF 2.1.0 (`dead-code-checker graph check --format sarif`).

    to_sarif(fs, meta=None) -> str      one JSON document, sorted and stable: two runs on the same tree give the same bytes

One run, one tool (`dead-code-checker`). `tool.driver.rules` has one rule per finding code that occurs (id = the code, sorted by
id; the short description is the code, the full description the rule's explanation). `results` has one result per finding
(sorted by path, line, code, message): `ruleId`, `ruleIndex`, `level` (E = error, W = warning, I = note), `message.text`, and,
for a finding about a file, a physical location (`artifactLocation.uri` = the path relative to the repository root with
`uriBaseId` %SRCROOT%, `region.startLine` when the finding has a line). A finding about the run itself (budget, a missing
engine, a failed check) has no location. The `why` of a finding is in `properties.why`; `partialFingerprints` holds a hash of
code, path and message, so that a code-scanning service can follow a finding across commits. The dc.suspect findings (level I, a
dead claim nothing corroborates) are left out unless meta["show_suspects"], like in every other format.

A dead-symbol claim carries its numeric confidence (graph.confidence, 0..100, a second axis that decides no level) in
`properties.confidence` (with `properties.confidenceNote`, the sum in one line) and as the SARIF `rank` (0..100: how much the tool
thinks the result deserves attention; here the same number, so a code-scanning service can sort by it); a finding without a claim has
neither. meta["min_confidence"] leaves out the claims under it (report.floored: the rule of every other format).

There is no clock and no absolute path in the document.
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote

from .. import __version__
from . import report
from .model import Finding
from .safety import WHERE

SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
VERSION = "2.1.0"
LEVEL = {"E": "error", "W": "warning", "I": "note"}
INFO_URI = "https://github.com/AlexGromer/dead-code-checker"


def _fingerprint(f: Finding) -> str:
    return hashlib.sha256(f"{f.code}\0{f.path}\0{f.msg}".encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _result(f: Finding, index: Dict[str, int]) -> dict:
    out: dict = {
        "ruleId": f.code,
        "ruleIndex": index[f.code],
        "level": LEVEL.get(f.level, "note"),
        "message": {"text": f.msg},
    }
    if f.path and f.path != WHERE:
        loc: dict = {"artifactLocation": {"uri": quote(f.path.replace("\\", "/"), safe="/"), "uriBaseId": "%SRCROOT%"}}
        if f.line and f.line > 0:
            loc["region"] = {"startLine": int(f.line)}
        out["locations"] = [{"physicalLocation": loc}]
    out["partialFingerprints"] = {"deadCodeChecker/v1": _fingerprint(f)}
    props: dict = {}
    if f.why:
        props["why"] = f.why
    if f.confidence is not None:
        props["confidence"] = f.confidence
        if f.conf_note:
            props["confidenceNote"] = f.conf_note
        out["rank"] = float(f.confidence)
    if props:
        out["properties"] = props
    return out


def _rules(fs: Iterable[Finding]) -> List[dict]:
    first: Dict[str, Finding] = {}
    for f in fs:
        first.setdefault(f.code, f)
    rules = []
    for code in sorted(first):
        why = report.DEFAULT_WHY.get(code) or first[code].why or f"rule {code}"
        rules.append(
            {
                "id": code,
                "name": code,
                "shortDescription": {"text": code},
                "fullDescription": {"text": why.split("\n")[0]},
                "defaultConfiguration": {"level": LEVEL.get(first[code].level, "note")},
            }
        )
    return rules


def to_sarif(fs: Sequence[Finding], meta: Optional[dict] = None) -> str:
    shown, _hidden = report.visible(fs, meta)
    shown, _low = report.floored(shown, meta)
    shown = sorted(report.ensure_all(shown), key=lambda f: (f.path, f.line, f.code, f.msg, f.level))
    rules = _rules(shown)
    index = {r["id"]: i for i, r in enumerate(rules)}
    doc = {
        "$schema": SCHEMA,
        "version": VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "dead-code-checker",
                        "version": __version__,
                        "informationUri": INFO_URI,
                        "rules": rules,
                    }
                },
                "originalUriBaseIds": {"%SRCROOT%": {"description": {"text": "the root of the analysed repository"}}},
                "results": [_result(f, index) for f in shown],
            }
        ],
    }
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
