"""graph.fix: `dead-code-checker graph fix --dry-run`, a patch for REVIEW that removes the dead symbols a check has confirmed.

    dead-code-checker graph fix --dry-run [--scope branch|repo] [--code CODE]... [--path GLOB]... [--root DIR] [--base REF]
                                          [--verify [--host HOST]] [--min-confidence N] [--json] [ENGINE OPTIONS]

It NEVER writes the work tree, never stages, never commits: the patch goes to standard output (git apply / patch -p1 take it, a
reviewer reads it), the rest to standard error. Without --dry-run the command refuses (exit 2, "only --dry-run is supported").

What is removed: a function, method or class that a finding of data/fix.json "codes" names AND that something corroborates (an
S-tier index or an engine, graph.verdict: the finding is not a dc.suspect; a claim that is only a suspect is never removable, and the
output counts those). The scopes are the two levels of `graph check`: branch = the findings of `check --scope branch` (--base as
there), repo = the findings of the whole tree as the repo level sees it (every file counts as added). --code keeps the findings of
those codes, --path the files that match a glob (or lie under a directory prefix).

The span is the one of the graph, and it is checked against the source before it is cut: the file must parse, and a definition with
the same first line (decorators included), last line and name must be there. Only languages of data/fix.json "span_langs" are cut
(Python: the ast span). Everything else is listed as MANUAL with the reason, never guessed: another language, a symbol that no
longer matches the file, a symlink, a removal that would leave an empty block (the result must still parse; each removal is tried
alone when all together do not), a symbol of a dead symbol that is already inside a removed one (it goes with it). The blank lines
around a removed definition are tidied (no hole of three lines, no blank line after a block header, none at the end of the file).
The comment lines DIRECTLY above a removed definition (no blank line between, the same indentation as the `def` or `class` or its first
decorator: `# why this exists`, `# ---- section ----`) describe it and go with it (attached); a comment after a blank line, a
comment of another indentation and the `#!` / coding line of the top of a file stay. A block header is read from the tokens of its line, so
`class A:  # note` and `if x:  # why` count as headers as well as `def f():`.

    plan(root, scope, codes, paths, base, proof, verify, min_conf, steps, layout) -> Plan(patch, removed, manual, skipped, notes, verify)
    cut(text, spans) -> str                                  the text without the 1-based inclusive (first, last) line spans
    attached(text, spans) -> spans                           the same spans, each one starting at the comment lines directly above it
    run(a) -> int                                            the command: 0, 2 for usage or a failure of the tool itself

--verify (graph.fixverify) checks the removals before they are offered: the patch is applied to a COPY of the tree and, per language,
the cheapest check that can show a removal unsafe runs on it. Local checks never execute project code (Python: compile, scopes,
imports in a separate process in the bubblewrap box, in this process with a note when there is no box; Go: vet offline and
TypeScript: tsc --noEmit, only in the box). Executing checks (tests, build scripts, mvn, gradle, npm test) run ONLY on a configured
remote execution target (`_exec_target`, --host HOST names the ssh host of [graph.runtime]); with no target the executing step is
SKIPPED with one line ("runtime mode: no execution target (<why>)") and nothing of the project runs on this machine. When the whole
patch fails it is split by file, then by symbol (bounded rounds, parallel, deterministic), and the patch keeps only what was not shown
unsafe. The report names a status per symbol: verified-local, verified-runtime, unverified (no check could judge it: it stays in the
patch, with the reason), failed (dropped, with the first error line of the check). Still dry-run: nothing is written into the work tree.

Confidence (graph.confidence, a second axis that never changes a verdict): every claim has a number 0..100. The files of the patch and
the lists of removed and manual symbols go the surest first (highest confidence, then path and line), --min-confidence N removes only
claims of at least N (the rest is skipped with that reason; a claim with no number is skipped too: a floor is a promise), and --json
has "confidence" per symbol. Without the flag nothing is dropped.

Exit 0 also when the patch is empty (the output says why). A run of the checks stays read-only: no pages, no run folder.
"""

from __future__ import annotations

import ast
import difflib
import fnmatch
import io
import json
import os
import sys
import tokenize
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import langs, levels, verdict
from . import repo as _repo
from .model import Finding, Graph, Node

NOT_PROVEN = "not corroborated (a suspect: no index or engine vouches for it, so it is not removable)"
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fix.json")


def table() -> dict:
    with open(DATA, encoding="utf-8") as fh:
        return json.load(fh)


class Item(NamedTuple):
    path: str
    line: int
    qual: str
    code: str
    reason: str = ""  # why it is manual; "" for a removed symbol
    conf: int = -1  # the confidence of the claim (dc.confidence, 0..100), -1 = the finding has none


class Plan(NamedTuple):
    patch: str
    removed: Tuple[Item, ...]
    manual: Tuple[Item, ...]
    skipped: Dict[str, int]  # reason -> number of findings that were not candidates
    notes: Tuple[str, ...]
    verify: Optional[object] = (
        None  # graph.fixverify.Report of --verify (removed is then what stays in the patch), else None
    )


# ---------------------------------------------------------------- pure functions on text: no I/O, nothing is written
_NOISE = (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER)


def opens_block(line: str) -> bool:
    """True when the last token of `line` that is not a comment is a `:` (a block header: `def f():`, `class A:  # note`, `else:  # why`,
    the last line of a multi-line header `):  # c`). A `#` inside a string is no comment; a line the tokenizer cannot finish is read up to
    the point where it stops."""
    last = None
    try:
        for t in tokenize.generate_tokens(io.StringIO(line.strip("\r\n")).readline):
            if t.type not in _NOISE:
                last = t
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    return last is not None and last.type == tokenize.OP and last.string == ":"


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def attached(text: str, spans: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """`spans` ((first, last), 1-based, inclusive) with each first line moved up over the comment lines directly above it: no blank line in
    between and the indentation of the first line of the span; never into the span before it, never over a `#!` or a coding line.
    """
    lines = text.splitlines()
    out: List[Tuple[int, int]] = []
    floor = 0
    for first, last in sorted(spans):
        ind, k = _indent(lines[first - 1]) if 0 < first <= len(lines) else "", first
        while k - 2 >= floor:
            above = lines[k - 2]
            if not above.lstrip().startswith("#") or _indent(above) != ind:
                break
            if k - 2 < 2 and (above.startswith("#!") or "coding" in above):
                break
            k -= 1
        out.append((k, last))
        floor = last
    return out


def cut(text: str, spans: Sequence[Tuple[int, int]]) -> str:
    """`text` without the lines of `spans` ((first, last), 1-based, inclusive, disjoint), the blank lines around each hole tidied."""
    lines = text.splitlines(keepends=True)
    for first, last in sorted(spans, reverse=True):
        before, after = lines[: first - 1], lines[last:]
        blank_before = not before or not before[-1].strip()
        if not after:  # the end of the file: no blank lines left dangling
            while before and not before[-1].strip():
                before.pop()
        elif blank_before or opens_block(
            before[-1]
        ):  # no hole of blank lines, no blank line after a block header (a trailing comment does not hide it)
            while after and not after[0].strip():
                after.pop(0)
        lines = before + after
    return "".join(lines)


def _defs(tree: ast.AST) -> set:
    """{(first line with decorators, last line, name)} of every def and class of a module."""
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return {
        (min([n.lineno] + [d.lineno for d in n.decorator_list]), n.end_lineno, n.name)
        for n in ast.walk(tree)
        if isinstance(n, kinds)
    }


def _parses(text: str) -> bool:
    try:
        ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    return True


def _inside(sp: Tuple[int, int, Item], chosen: Sequence[Tuple[int, int, Item]]) -> bool:
    return any(c[0] <= sp[0] and sp[1] <= c[1] for c in chosen)


def patch_file(text: str, wanted: Sequence[Tuple[int, int, Item]]) -> Tuple[str, List[Item], List[Item]]:
    """(new text, removed, manual) for the symbols `wanted` of one Python file. Each must match a definition of the source; the result
    must parse: all together first, else one by one outermost first, and the ones that break it are manual. A symbol inside a removed
    one goes with it."""
    manual: List[Item] = []
    try:
        have = _defs(ast.parse(text))
    except (SyntaxError, ValueError) as exc:
        return text, [], [it._replace(reason=f"the file does not parse ({type(exc).__name__})") for _, _, it in wanted]
    ok = sorted(
        (sp for sp in wanted if (sp[0], sp[1], sp[2].qual.rpartition(".")[2]) in have),
        key=lambda t: (t[0], -t[1], t[2].qual),
    )
    manual += [
        sp[2]._replace(reason="the definition is not at the lines of the graph (the file changed since)")
        for sp in wanted
        if sp not in ok
    ]
    chosen: List[Tuple[int, int, Item]] = []
    for sp in ok:
        if not _inside(sp, chosen):
            chosen.append(sp)
    spans = lambda cs: attached(text, [(a, b) for a, b, _ in cs])  # noqa: E731 - with the comments directly above each
    if not _parses(cut(text, spans(chosen))):  # one by one: what breaks the file stays and is listed
        chosen = []
        for sp in ok:
            if _inside(sp, chosen):
                continue
            if _parses(cut(text, spans(chosen + [sp]))):
                chosen.append(sp)
            else:
                manual.append(
                    sp[2]._replace(reason="removing it would leave an empty block (the file would not parse)")
                )
    removed = [sp[2] for sp in ok if sp in chosen or _inside(sp, chosen)]
    return cut(text, spans(chosen)), removed, manual


def unified(path: str, old: str, new: str) -> str:
    """The unified diff of one file, `\\ No newline at end of file` markers included, so that git apply takes it as it is."""
    out = []
    for ln in difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}", n=3
    ):
        out.append(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n")
    return "".join(out)


# ---------------------------------------------------------------- the plan
def _wanted(path: str, paths: Sequence[str]) -> bool:
    return not paths or any(fnmatch.fnmatchcase(path, g) or path.startswith(g.rstrip("/") + "/") for g in paths)


def _read(root: str, rel: str) -> Optional[str]:
    """The text of a tracked-looking file under `root`, or None for a symlink, a path that leaves the root, a missing or binary file."""
    p = os.path.join(root, rel)
    if os.path.islink(p) or not os.path.realpath(p).startswith(os.path.realpath(root) + os.sep):
        return None
    try:
        with open(p, encoding="utf-8", newline="") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def candidates(fs: Sequence[Finding], graph: Graph, codes: Sequence[str], paths: Sequence[str], min_conf: int = 0):
    """(items by file, manual, skipped): the corroborated findings of data/fix.json "codes" tied to their nodes; with `min_conf` only
    those whose confidence is at least that."""
    spec = table()
    at: Dict[Tuple[str, int], Node] = {}
    for n in graph.nodes:
        if n.kind in spec["kinds"]:
            at.setdefault((n.path, n.line0), n)
    todo: Dict[str, List[Tuple[int, int, Item]]] = {}
    manual: List[Item] = []
    skipped: Dict[str, int] = {}
    for f in sorted(fs, key=lambda f: (f.path, f.line, f.code)):
        claim = verdict.claim(f)
        if (
            claim not in spec["codes"]
            or (codes and claim not in codes and f.code not in codes)
            or not _wanted(f.path, paths)
        ):
            continue
        if not verdict.proof_of(f):
            skipped[NOT_PROVEN] = skipped.get(NOT_PROVEN, 0) + 1
            continue
        if min_conf and (f.confidence is None or f.confidence < min_conf):
            why = f"confidence below the floor of {min_conf} (--min-confidence)"
            skipped[why] = skipped.get(why, 0) + 1
            continue
        n = at.get((f.path, f.line))
        qual = f.msg.split("`")[1] if f.msg.count("`") >= 2 else ""
        it = Item(f.path, f.line, qual, f.code, conf=-1 if f.confidence is None else f.confidence)
        if n is None:
            manual.append(it._replace(reason="no symbol of the graph at that line"))
        elif n.lang not in spec["span_langs"]:
            manual.append(it._replace(qual=levels._qual(n.key), reason=f"no exact-span removal for {n.lang} yet"))
        else:
            it = it._replace(qual=levels._qual(n.key))
            todo.setdefault(f.path, []).append((n.line0, n.line1, it))
    return todo, manual, skipped


def plan(
    root: str,
    scope: str = "branch",
    codes: Sequence[str] = (),
    paths: Sequence[str] = (),
    base: Optional[str] = None,
    proof: verdict.Proof = verdict.Proof(),
    verify=None,
    min_conf: int = 0,
    steps: Optional[levels.Steps] = None,
    layout=None,
) -> Plan:
    """The removal patch of the confirmed dead symbols of `scope` (see the module docstring). Reads the work tree and the caches, writes nothing.
    `verify` (a graph.fixverify.Options: the execution target or why there is none) turns on --verify: the patch keeps what was not shown unsafe.
    `min_conf` (--min-confidence) keeps only the claims of at least that confidence (see candidates); the files and the lists go the surest first.
    `steps` and `layout` are what `graph check` hands to its level (the semantic cache and the engines, the data directory).
    """
    notes: List[str] = []
    sink: dict = {}
    if scope == "branch":
        rep = levels.branch(root, base, sink=sink, proof=proof, steps=steps, layout=layout)
    else:
        rep = _repo.check(root, sink=sink, proof=proof, steps=steps, layout=layout)
    fs, graph = list(rep.findings), sink.get("graph")
    if graph is None:
        why = next(
            (k for k in ("disabled", "docs_only", "cold", "nothing_to_check") if rep.meta.get(k)),
            "the graph stages did not finish",
        )
        return Plan("", (), (), {}, (f"no graph of the {scope} run ({why}): nothing to remove",))
    todo, manual, skipped = candidates(fs, graph, codes, paths, min_conf)
    patch, removed, texts, kept_of = [], [], {}, []
    def surest(path: str) -> tuple:  # the file with the surest claim first
        return -max(it.conf for _, _, it in todo[path]), path

    order = sorted(todo, key=surest)
    for path in order:
        text = _read(root, path)
        if text is None:
            manual += [
                it._replace(reason="the file is a symlink, outside the tree, binary or unreadable")
                for _, _, it in todo[path]
            ]
            continue
        new, gone, left = patch_file(text, todo[path])
        removed += gone
        manual += left
        texts[path] = text
        kept_of.append((path, todo[path], gone))
        patch.append(unified(path, text, new) if new != text else "")
    report = None
    if verify is not None:
        from . import fixverify

        units = [u for path, spans, gone in kept_of for u in _units(path, spans, gone)]
        report = fixverify.verify(root, units, _render(texts), texts, verify)
        report = report._replace(  # the rows keep the order of the lists: surest first
            rows=tuple(sorted(report.rows, key=lambda r: (-r.conf, r.path, r.line, r.qual)))
        )
        rendered = _render(texts)(report.kept)  # what stays: the removals that were not shown unsafe
        patch = [
            unified(p, texts[p], rendered[p]) if rendered.get(p, texts[p]) != texts[p] else ""
            for p in order
            if p in texts
        ]
        removed = [it for u in report.kept for it in u.items]
    def key(it: Item) -> tuple:  # surest first, then path and line
        return -it.conf, it.path, it.line, it.qual

    return Plan(
        "".join(patch), tuple(sorted(removed, key=key)), tuple(sorted(manual, key=key)), skipped, tuple(notes), report
    )


def _units(path: str, spans: Sequence[Tuple[int, int, Item]], gone: Sequence[Item]) -> list:
    """The removals of one file as graph.fixverify units: one per outermost span that goes, the symbols inside it listed with it."""
    from . import fixverify

    top: List[list] = []
    for a, b, it in sorted((sp for sp in spans if sp[2] in gone), key=lambda sp: (sp[0], -sp[1], sp[2].qual)):
        if top and top[-1][0] <= a and b <= top[-1][1]:
            top[-1][2].append(it)
        else:
            top.append([a, b, [it]])
    lang = langs.detect(path) or "python"
    return [fixverify.Unit(path, a, b, tuple(items), lang) for a, b, items in top]


def _render(texts: Dict[str, str]) -> Callable[[Sequence], Dict[str, str]]:
    """render(units) -> {path: the text without exactly those units' spans (the comments directly above each go too)}."""

    def render(units: Sequence) -> Dict[str, str]:
        by: Dict[str, List[Tuple[int, int]]] = {}
        for u in units:
            by.setdefault(u.path, []).append((u.first, u.last))
        return {p: cut(texts[p], attached(texts[p], sp)) for p, sp in sorted(by.items())}

    return render


# ---------------------------------------------------------------- the command
def _doc(scope: str, p: Plan) -> dict:
    def as_d(it: Item) -> dict:
        return {
            "path": it.path,
            "line": it.line,
            "symbol": it.qual,
            "code": it.code,
            **({"confidence": it.conf} if it.conf >= 0 else {}),
            **({"reason": it.reason} if it.reason else {}),
        }

    doc = {
        "scope": scope,
        "dry_run": True,
        "patch": p.patch,
        "removed": [as_d(i) for i in p.removed],
        "manual": [as_d(i) for i in p.manual],
        "skipped": dict(sorted(p.skipped.items())),
        "notes": list(p.notes),
    }
    if p.verify is not None:  # --verify: a status per symbol; the dropped ones are the rows with status failed
        rows = [
            {
                "path": r.path,
                "line": r.line,
                "symbol": r.qual,
                "code": r.code,
                **({"confidence": r.conf} if r.conf >= 0 else {}),
                "status": r.status,
                **({"check": r.check} if r.check else {}),
                **({"class": r.klass} if r.klass else {}),
                **({"detail": r.detail} if r.detail else {}),
            }
            for r in p.verify.rows
        ]
        doc["verify"] = {
            "rows": rows,
            "counts": _counts(p.verify),
            "dropped": [r for r in rows if r["status"] == "failed"],
            "notes": [{"level": lv, "text": tx} for lv, tx in p.verify.notes],
            "checks": p.verify.checks,
            "languages": {
                lang: [
                    {"class": k, "check": name, "runs": where, "means": how} for k, name, where, how in _classes(lang)
                ]
                for lang in p.verify.langs
            },
        }
    return doc


def _classes(lang: str) -> list:
    from . import fixverify

    return fixverify.classes(lang)


def _counts(rep) -> Dict[str, int]:
    from . import fixverify

    return {st: sum(1 for r in rep.rows if r.status == st) for st in fixverify.STATUSES}


def _verify_lines(rep) -> List[str]:
    """The --verify part of the summary: the notes (the runtime-mode line among them), then a line per symbol that is not simply verified."""
    out = [f"  {lv} {tx}" for lv, tx in rep.notes]
    for lang in rep.langs:  # which class each check is, per language
        out.append(f"  {lang}: " + "; ".join(f"{k} {name} ({where})" for k, name, where, _ in _classes(lang)))
    for r in rep.rows:
        if r.status == "failed":
            out.append(f"  dropped {r.path}:{r.line} {r.qual} [{r.check}]: {r.detail}")
        elif r.status == "unverified":
            out.append(f"  unverified {r.path}:{r.line} {r.qual}: {r.detail}")
        else:
            out.append(f"  {r.status} {r.path}:{r.line} {r.qual} [{r.check}]")
    return out


def _summary(p: Plan) -> List[str]:
    head = f"dead-code-checker graph fix --dry-run: {len(p.removed)} symbol(s) in the patch, {len(p.manual)} manual, nothing written"
    if p.verify is not None:
        c = _counts(p.verify)
        head = (
            f"dead-code-checker graph fix --dry-run --verify: {len(p.removed)} symbol(s) in the patch ({c['verified-runtime']} verified-runtime, "
            f"{c['verified-local']} verified-local, {c['unverified']} unverified), {c['failed']} dropped, {len(p.manual)} manual, "
            "nothing written"
        )
    out = [head]
    out += [f"  note: {n}" for n in p.notes]
    out += _verify_lines(p.verify) if p.verify is not None else []
    out += [f"  skipped {n}: {why}" for why, n in sorted(p.skipped.items())]
    out += [
        f"  manual  {i.path}:{i.line} {i.qual} [{i.code}]{f' confidence {i.conf}' if i.conf >= 0 else ''}: {i.reason}"
        for i in p.manual
    ]
    if not p.patch and not p.notes:
        out.append("  the patch is empty")
    return out


def _exec_target(root: str, host: Optional[str] = None, user: Optional[str] = None, port: Optional[int] = None):
    """(target, why): the ONE execution target of --verify, from the runtime mode (graph.runtime.exec_target: the configured ssh host of
    [graph.runtime], the allow-list $DEAD_CODE_CHECKER_RUNTIME_HOSTS, `host` = --host), or None and the reason. The only place that
    knows about graph.runtime, and it imports it here, not at the top: a build without the runtime mode answers (None, why)."""
    try:
        from . import runtime
    except ImportError:
        return None, "runtime mode is not available in this build"
    return runtime.exec_target(root, cfg=None, environ=None, host=host, user=user, port=port)


def _options(
    root: str,
    host: Optional[str] = None,
    base: Optional[str] = None,
    go_cache: Optional[str] = None,
    user: Optional[str] = None,
    port: Optional[int] = None,
):
    """The execution target of --verify as graph.fixverify.Options; a target that cannot be resolved is no target, never a failure."""
    from . import fixverify

    try:
        target, why = _exec_target(root, host, user, port)
    except Exception as exc:  # noqa: BLE001 - no target is a skipped step, never a failure of the command
        target, why = None, f"the execution target cannot be configured ({type(exc).__name__}: {exc})"
    return fixverify.Options(target, why, base, go_cache)


def run(a, planner: Callable[..., Plan] = plan) -> int:
    """`graph fix`: refuses without --dry-run (exit 2). The patch is the only thing on standard output (--json: one document)."""
    if not a.dry_run:
        print(
            "dead-code-checker graph fix: only --dry-run is supported (the command prints a patch for review and never writes the tree)",
            file=sys.stderr,
        )
        return 2
    from . import cli

    root, lay, rc = cli._repo(a)
    if root is None or lay is None:
        return rc
    try:
        cli._jobs(a)
        extra: dict = {"steps": cli._steps(a, root, lay), "layout": lay}
        if getattr(a, "verify", False):
            extra["verify"] = _options(
                root,
                getattr(a, "host", None),
                str(lay.base),
                getattr(a, "go_cache", None),
                getattr(a, "user", None),
                getattr(a, "port", None),
            )
        if getattr(a, "min_confidence", 0):
            extra["min_conf"] = a.min_confidence
        p = planner(root, a.scope, a.code or (), a.path or (), a.base, **extra)
    except (
        Exception
    ) as exc:  # noqa: BLE001 - a tool of review: its own failure is a message and exit 2, never a traceback
        print(f"dead-code-checker graph fix: failed ({type(exc).__name__}: {exc}); nothing was written", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(_doc(a.scope, p), indent=1, sort_keys=True))
    else:
        sys.stdout.write(p.patch)
        print("\n".join(_summary(p)), file=sys.stderr)
    return 0
