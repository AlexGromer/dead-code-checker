"""graph.report: how the findings of the graph checker are shown: a finding without an explanation is not printed.

    ensure_why(f) / ensure_all(fs)   every finding leaves with a why: the one the level computed (why-dead of the graph, the
                                     registration of a root) or, for a notice, the rule that made it
    register_why(table)              a layer that adds finding codes (the engines, the semantic layer) gives their texts here
    to_text(fs, meta) / to_json(fs, meta) / to_md(fs, meta)   the formats of the command line; json is sorted and stable, md is
                                     the report.md of a run
    The dc.suspect findings (level I, a dead claim nothing corroborates, graph.verdict) are hidden by every format unless
    meta["show_suspects"] (--show-suspects): the count is said instead ("N suspects hidden, --show-suspects").
    A dead-symbol claim carries a numeric confidence (graph.confidence, a second axis: it never changes the level or the code): text and
    md print it under the finding with its sum, json has "confidence" and "confidence_note". meta["min_confidence"] (--min-confidence N)
    leaves out the findings whose confidence is under N, display only (the exit code, findings.json and the viewer keep them) and says how
    many ("N finding(s) below confidence M hidden, --min-confidence"); a finding without a confidence is never left out.

Source text is never in a finding (only names, paths, lines and rule ids), so nothing here can leak a secret: the same rule
as the evidence pool of the graph.
"""

from __future__ import annotations

import json
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import confidence
from .model import Finding
from .verdict import SUSPECT

DEFAULT_WHY: Dict[str, str] = {
    "dc.engine-missing": "the extractor of a language in scope cannot run at all (its module cannot be imported); the files of the language are left out of the graph",
    "dc.degraded-extractor": "tree-sitter, its grammar for this language or pwsh is not installed, so these files are not extracted at all and are left out of the graph (never guessed); the message says what to install",
    "dc.graph-incomplete": "part of the tree is not in the graph because its extractor cannot run; the verdicts that need it are off (never guessed)",
    "dc.unsupported-language": "no silent skips: the graph has no extractor for this language yet, so no dead-code verdict covers the file; only the text checks ran",
    "dc.check-failed": "a failure of the checker itself (an exception, a crash, a store error) is a notice and never a block; the finished results are kept and the reason is shown",
    "dc.bad-budget": "[graph.budget] holds a value the check cannot use (bad type, not positive, unknown key) or above its ceiling; the default or the ceiling is used",
    "dc.disabled": "enabled = false is valid until its disabled_until date and needs a reason; the check is off until then",
    "dc.disabled-expired": "enabled = false carries a disabled_until date that has passed: the check runs again; renew the date with a reason or remove the disable",
    "dc.waive-expired": "a waive carries an until date that has passed and no longer covers anything; renew it with a reason or remove it",
    "dc.bad-config": "a repository config may not choose what runs or where data goes; fix the [graph] table",
    "dc.bad-facts": "an extractor produced a fact the model does not allow; the fact was skipped, not repaired",
    "dc.bad-root-source": "a file that registers entry points (a manifest, a unit file, a build file) could not be read; the entry points it names are not rooted, so a script only it names may look unreferenced",
    "dc.root-dangling": "a registration that points into the void is an error; fix the path or remove the registration",
    SUSPECT: "a dead-symbol claim of the syntactic graph is a verdict only when an S-tier index (SCIP, compiler) of the package or the native engine of the language agrees; it may be reflection, a framework convention or a dynamic call; hidden by default (--show-suspects), drawn in the viewer, never blocks",
    "dc.no-base": "without a base commit every file counts as added, so every finding is pre-existing: the errors of the tree are shown as warnings and nothing blocks",
}


def register_why(table: Mapping[str, str]) -> None:
    """Add the default explanations of finding codes that another layer produces (a code that has one already keeps it)."""
    for code, text in table.items():
        DEFAULT_WHY.setdefault(code, text)


def ensure_why(f: Finding) -> Finding:
    if not f.why:
        f.why = (
            DEFAULT_WHY.get(f.code)
            or f"rule {f.code}: no root reaches the symbol, see `dead-code-checker graph why <node>`"
        )
    return f


def ensure_all(fs: Iterable[Finding]) -> List[Finding]:
    return [ensure_why(f) for f in fs]


def visible(fs: Sequence[Finding], meta: Optional[dict] = None) -> Tuple[List[Finding], int]:
    """(findings to show, suspects left out): the dc.suspect ones are left out unless meta["show_suspects"]."""
    if (meta or {}).get("show_suspects"):
        return list(fs), 0
    shown = [f for f in fs if f.code != SUSPECT]
    return shown, len(fs) - len(shown)


def _hidden(n: int) -> str:
    return f"{n} suspects hidden, --show-suspects"


def floored(fs: Sequence[Finding], meta: Optional[dict] = None) -> Tuple[List[Finding], int]:
    """(findings to show, findings left out): those with a confidence under meta["min_confidence"] are left out (display only)."""
    floor = int((meta or {}).get("min_confidence") or 0)
    if floor <= 0:
        return list(fs), 0
    shown = [f for f in fs if not confidence.below(f, floor)]
    return shown, len(fs) - len(shown)


def _low(n: int, meta: Optional[dict]) -> str:
    return f"{n} finding(s) below confidence {int((meta or {}).get('min_confidence') or 0)} hidden, --min-confidence"


def _conf_line(f: Finding) -> str:
    return f"confidence {f.confidence}/100" + (f" ({f.conf_note})" if f.conf_note else "")


def to_text(fs: Sequence[Finding], meta: Optional[dict] = None) -> str:
    fs, hid = visible(fs, meta)
    fs, low = floored(fs, meta)
    lines = []
    for f in ensure_all(fs):
        lines.append(f.render())
        lines += [f"    {_conf_line(f)}"] if f.confidence is not None else []
        lines += [f"    {ln}" for ln in f.why.split("\n")]
    e, s = sum(f.level == "E" for f in fs), sum(f.code == SUSPECT for f in fs)
    info = sum(f.level == "I" for f in fs) - s  # notes of the engine layer (a server that did not answer, ...) are not suspects
    lines += [f"dead-code-checker: {_hidden(hid)}"] if hid else []
    lines += [f"dead-code-checker: {_low(low, meta)}"] if low else []
    lines.append(
        f"dead-code-checker: {e} error(s), {len(fs) - e - s - info} warning(s)"
        + (f", {s} suspect(s)" if s else "")
        + (f", {info} info" if info else "")
        + _tail(meta)
    )
    return "\n".join(lines)


def _tail(meta: Optional[dict]) -> str:
    if not meta:
        return ""
    bits = [f"{meta[k]} {k}" for k in ("files", "nodes", "edges") if k in meta]
    if meta.get("unfinished"):
        bits.append("unfinished: " + ", ".join(meta["unfinished"]))
    if meta.get("unsupported"):
        bits.append("no extractor: " + ", ".join(meta["unsupported"]))
    return f" ({', '.join(bits)})" if bits else ""


def to_json(fs: Sequence[Finding], meta: Optional[dict] = None, timing: bool = False) -> str:
    """Deterministic by default: the wall-clock `timing_ms` never enters the payload, so two runs on the same tree are
    byte-identical. `timing=True` (--timing) adds it as a separate top-level "timing" block."""
    fs, hid = visible(fs, meta)
    fs, low = floored(fs, meta)
    meta = dict(meta or {})
    clock = meta.pop("timing_ms", None)
    meta.pop("show_suspects", None)
    if hid:
        meta["suspects_hidden"] = hid
    if low:
        meta["below_confidence_hidden"] = low
    doc = {"findings": [f.as_dict() for f in ensure_all(fs)], "meta": meta}
    if timing and clock is not None:
        doc["timing"] = clock
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False)


def _links_line(meta: dict) -> str:
    """First line of a stored report.md: links to the pages of the run and its directory, or why there is none."""
    links = meta.get("links") or {}
    pages = " ".join(f"[{k}]({u})" for k, u in sorted(links.items())) or meta.get("no_page") or "no page"
    return f"Pages: {pages}; run directory `{meta['dir']}`"


def to_md(fs: Sequence[Finding], meta: Optional[dict] = None) -> str:
    meta = meta or {}
    fs, hid = visible(fs, meta)
    fs, low = floored(fs, meta)
    out = [_links_line(meta)] if meta.get("dir") else []
    out += [f"# dead-code-checker {meta.get('scope', '')} report".rstrip(), ""]
    out.append(
        (
            ""
            if meta.get("scope") == "repo"
            else f"Base `{meta.get('base')}`; {meta.get('changed', 0)} changed file(s); "
        )
        + f"{meta.get('files', 0)} files, {meta.get('nodes', 0)} nodes, {meta.get('edges', 0)} edges in the graph."
        if meta.get("base") or meta.get("files")
        else ""
    )
    if meta.get("verdicts"):
        out.append("Verdicts: " + ", ".join(f"{k} {v}" for k, v in sorted(meta["verdicts"].items())) + ".")
    if meta.get("unfinished"):
        out.append("Unfinished (budget): " + ", ".join(meta["unfinished"]) + ".")
    if meta.get("unsupported"):
        out.append("Languages without an extractor (not in the graph): " + ", ".join(meta["unsupported"]) + ".")
    out += [
        "",
        f"{len(fs)} finding(s)." + (f" {_hidden(hid)}." if hid else "") + (f" {_low(low, meta)}." if low else ""),
        "",
    ]
    for f in ensure_all(fs):
        loc = f"{f.path}:{f.line}" if f.line else f.path
        out += [f"## {f.level} `{f.code}` {loc}", "", f.msg, ""]
        out += [f"Confidence: {_conf_line(f)[len('confidence '):]}", ""] if f.confidence is not None else []
        out += ["Why:", "", "```", f.why, "```", ""]
    return "\n".join(x for i, x in enumerate(out) if x or (i and out[i - 1] != ""))
