"""graph.adapters: what the dead-code engines say, as evidence on graph nodes. Stdlib only.

parse(tool, text, fmt=None, root="", ver="", keep_all=False) -> [Hit]
    The output of one engine run (ruff, vulture, shellcheck, rustc, cargo, staticcheck, go-deadcode, knip, tsc, pmd, checkstyle,
    detekt, tflint, deadvalues, cppcheck, clang-tidy, PSScriptAnalyzer, roslyn) as plain Hit records. The formats and the field
    paths are DATA (data/adapters.json, each one has a recording of the real engine under tests/fixtures/engines/; `unrecorded`
    gives the reason for an engine that has none yet); this file only walks them. A Hit with a claim (func, var, class, file, import,
    ...) says "the engine finds this unused"; the other diagnostics of the run are dropped unless keep_all. `root` is cut off
    absolute paths. Text that cannot be read, JSON or XML of another shape than the format states (data: top, need, fail, root) and an
    error the engine reports itself (tflint errors[]) are an AdapterError, never an empty list (no silent fallback); an empty text
    and the empty answer of the format (`[]`, an empty report) are a clean run. A text format cannot tell a crash from a clean run,
    except for the error lines of its engine (data fail_re): the rest is rc_ok(tool, rc), which reconcile applies to Run.rc. The same
    text always gives the same sorted list.
attach(graph, hits) -> (Graph, [Match])
    Each Hit becomes an Evidence in the pool and is tied to a node: the narrowest node that contains the line and has the
    reported name (exact=True), else the narrowest one around the line, else the file node (exact=False, evidence only).
    to_payload / from_payload keep the matches in store.engine_result.
disagree(graph, matches, res, covered) -> [Disagreement]
    Graph and engine disagree when an exact claim about a node (a dispute kind: not an unused export, import or local) meets a
    graph verdict alive or weak-alive ("graph-alive"), when a reach engine that analysed a file (covered) does not list a
    function the graph holds dead ("engine-alive"), or when an engine that says something alive about a node (data alive_claims,
    empty today) names exactly a node the graph holds dead. The verdict stays with the graph; the engine is a clue.
to_findings(dis) -> W dc.engine-disagree      ledger_append(path, records)   ledger.jsonl, one deduplicated line per disagreement
    (locked: parallel writers never duplicate a line)
reconcile(graph, runs, res, ledger=None) -> Reconciled(graph, matches, disagreements, findings)   the whole path for [Run];
    a Run with an exit code that is not a healthy one (rc_ok) is W dc.engine-output and says nothing else (no hits, not covered)
"""

from __future__ import annotations

import contextlib
import json
import os
import posixpath
import re
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from . import report
from .model import Finding, Graph, canonical, evidence, split_key

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "adapters.json")
WHY = {
    "dc.engine-disagree": "the graph and an engine disagree about a symbol; the verdict stays with the graph, the engine result is evidence on the node (see `graph why`) and is recorded in the ledger",
    "dc.engine-output": "an engine ran but its output could not be read by the adapter; its evidence is missing from this run (never guessed)",
}
report.register_why(WHY)
ALIVE_SIDE = ("alive", "weak-alive")
DEAD_SIDE = ("dead", "dead-cluster")
MSG_MAX = 300
_TABLE: Dict[str, dict] = {}


class AdapterError(ValueError):
    def __init__(self, tool: str, reason: str):
        super().__init__(f"{tool}: {reason}")
        self.tool, self.reason = tool, reason


def table(path: Optional[str] = None) -> dict:
    p = path or DATA
    if p not in _TABLE:
        with open(p, encoding="utf-8") as fh:
            _TABLE[p] = json.load(fh)
    return _TABLE[p]


class Hit(NamedTuple):
    tool: str
    ver: str
    path: str  # relative to the project root, posix
    line: int  # 0 = the engine gives none
    col: int
    rule: str
    msg: str
    claim: str  # what the engine finds unused (a key of claim_nodes, or import/local/param/dep), "" = not a dead-code claim
    name: str  # the symbol as the engine writes it, "" = unknown
    conf: int  # engine confidence in percent (vulture), 0 = none


# ---------------------------------------------------------------- reading a field
def _walk(obj, path: str) -> list:
    """The values a field path leads to (steps: name, name[], name[key], []); [] on the document is the list itself."""
    cur = [obj]
    for step in path.split(".") if path else ():
        name, br, key = re.fullmatch(r"(\w*)(\[(\w*)\])?", step).groups()  # type: ignore[union-attr]
        nxt = []
        for c in cur:
            if name:
                c = c.get(name) if isinstance(c, dict) else None
            if br is None:
                nxt.append(c)
            else:
                items = c if isinstance(c, list) else [c] if isinstance(c, dict) else []
                nxt += [i for i in items if not key or (isinstance(i, dict) and i.get(key))]
        cur = nxt
    return [c for c in cur if c is not None]


def _jget(row, path: str):
    return next((v for v in _walk(row, path) if v != ""), "")


def _xget(el, path: str):
    if path == ".":
        return el.text or ""
    where, at, attr = path.rpartition("@")
    node = el.find(where.rstrip("/") if at else path) if (where or not at) else el
    if node is None:
        return ""
    return (node.get(attr) if at else node.text) or ""


def _field(spec: dict, key: str, row, outer, read) -> object:
    """`a|b` takes the first non-empty alternative, '=x' is a constant, '^a' reads the outer row."""
    for alt in filter(None, (spec.get(key) or "").split("|")):
        if alt.startswith("="):
            return alt[1:]
        v = read(outer if alt.startswith("^") else row, alt.lstrip("^"))
        if v not in ("", None):
            return v
    return ""


def _rel(path, root: str) -> str:
    p = str(path).replace("\\", "/")
    if p.startswith("file://"):
        p = p[7:]
    if re.match(r"^/[A-Za-z]:/", p):
        p = p[1:]
    r = root.replace("\\", "/").rstrip("/")
    if r and p.startswith(r + "/"):
        p = p[len(r) + 1 :]
    return posixpath.normpath(p) if p else ""


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _claim(t: dict, rule: str, msg: str) -> str:
    for pat, kind in t.get("claims", ()):
        if re.fullmatch(pat, rule, re.I):
            if isinstance(kind, dict):
                return next((k for mp, k in kind.items() if re.search(mp, msg)), "")
            return kind
    return ""


def _hit(tool, t, spec, ver, root, file, line, col, rule, msg, name="", conf=0, groups=None) -> Optional[Hit]:
    path = _rel(file, root)
    if not path:
        return None
    rule = str(rule)
    if spec.get("rule_fmt"):
        rule = spec["rule_fmt"].format(v=rule, **(groups or {})).replace(" ", "-")
    if spec.get("rule_tail"):
        rule = rule.rsplit(".", 1)[-1]
    msg = " ".join(str(msg).split())[:MSG_MAX]
    name = str(name or "")
    if not name and (spec.get("name_re") or t.get("name_re")):
        m = re.search(spec.get("name_re") or t["name_re"], msg)
        name = m.group("name") if m else ""
    if not msg and spec.get("msg_fmt"):
        msg = spec["msg_fmt"].format(name=name, rule=rule)
    return Hit(tool, ver, path, _int(line), _int(col), rule, msg, _claim(t, rule, msg), name.strip(), _int(conf))


# ---------------------------------------------------------------- formats
def _shaped(doc, spec: dict) -> bool:
    """Is the document the JSON the format describes: top = list (null = an empty Go slice; items are objects) | object (the
    keys of `need` are there); `list|object` allows both."""
    top = spec.get("top", "list").split("|")
    if doc is None or isinstance(doc, list):
        return "list" in top and all(isinstance(r, dict) for r in doc or ())
    return isinstance(doc, dict) and "object" in top and all(k in doc for k in spec.get("need", ()))


def _json(tool, t, spec, text, ver, root):
    docs = []
    try:
        if spec.get("lines"):
            docs = [json.loads(ln) for ln in text.splitlines() if ln.startswith("{")]
        elif text.strip():
            docs = [json.loads(text)]
    except ValueError as e:
        raise AdapterError(tool, f"not JSON: {str(e)[:80]}") from e
    shaped = [d for d in docs if _shaped(d, spec)]  # lines: other messages (artifacts, notes) may be mixed in
    if docs and not shaped:
        got = type(docs[0]).__name__ + (f" with keys {sorted(docs[0])[:4]}" if isinstance(docs[0], dict) else "")
        want = spec.get("top", "list") + (f" with keys {spec['need']}" if spec.get("need") else "")
        raise AdapterError(tool, f"JSON of the wrong shape: expected {want}, got {got}")
    for doc in shaped:
        for bad in _walk(doc, spec["fail"]) if spec.get("fail") else ():
            msg = (bad.get("message") or bad.get("summary") or bad) if isinstance(bad, dict) else bad
            raise AdapterError(tool, f"the engine reports an error: {' '.join(str(msg).split())[:MSG_MAX]}")
        for outer in _walk(doc, spec.get("rows", "")):
            for row in _walk(outer, spec["each"]) if spec.get("each") else [outer]:
                if spec.get("skip") and _jget(row, spec["skip"]):
                    continue
                for g in spec.get("groups") or [None]:  # knip: one list per kind of finding, the file is on the row
                    for it in _walk(row, g + "[]") if g else [row]:
                        if g:
                            it = it if isinstance(it, dict) else {"name": it}
                        rule = g or _field(spec, "rule", row, outer, _jget)
                        yield _hit(
                            tool,
                            t,
                            spec,
                            ver,
                            root,
                            _field(spec, "file", row, outer, _jget),
                            _field(spec, "line", it, outer, _jget),
                            _field(spec, "col", it, outer, _jget),
                            rule,
                            _field(spec, "msg", it, outer, _jget),
                            _field(spec, "name", it, outer, _jget),
                        )


def _text(tool, t, spec, text, ver, root):
    rx, bad = re.compile(spec["re"]), re.compile(spec.get("fail_re") or "(?!)")
    for ln in text.splitlines():
        if bad.match(ln):
            raise AdapterError(tool, f"the engine reports an error: {ln.strip()[:MSG_MAX]}")
        m = rx.match(ln.rstrip())
        if m:
            g = m.groupdict()
            yield _hit(
                tool,
                t,
                spec,
                ver,
                root,
                g["file"],
                g["line"],
                g.get("col"),
                g.get("rule") or "",
                g.get("msg") or "",
                g.get("name") or "",
                g.get("conf"),
                g,
            )


def _xml(tool, t, spec, text, ver, root):
    if "<!ENTITY" in text:
        raise AdapterError(tool, "XML with entity declarations is refused")
    try:
        top = ET.fromstring(text)
    except ET.ParseError as e:
        raise AdapterError(tool, f"not XML: {e}") from e
    for el in top.iter():
        el.tag = el.tag.rpartition("}")[2]  # the PMD report has a namespace
    if spec.get("root") and top.tag not in spec["root"]:
        raise AdapterError(tool, f"XML of the wrong shape: root <{top.tag}>, expected <{'|'.join(spec['root'])}>")
    for outer in top.iterfind(spec["rows"]):
        for row in outer.iterfind(spec["each"]) if spec.get("each") else [outer]:
            yield _hit(
                tool,
                t,
                spec,
                ver,
                root,
                _field(spec, "file", row, outer, _xget),
                _field(spec, "line", row, outer, _xget),
                _field(spec, "col", row, outer, _xget),
                _field(spec, "rule", row, outer, _xget),
                (_field(spec, "msg", row, outer, _xget) or "").strip(),
                (_field(spec, "name", row, outer, _xget) or "").strip(),
            )


_KINDS = {"json": _json, "text": _text, "xml": _xml}


def parse(
    tool: str, text: str, fmt: Optional[str] = None, root: str = "", ver: str = "", keep_all: bool = False
) -> List[Hit]:
    tab = table()
    t = tab["tools"].get(tool)
    if t is None:
        raise AdapterError(tool, "no adapter for this engine")
    spec = t["formats"].get(fmt or t["default"])
    if spec is None:
        raise AdapterError(tool, f"no format {fmt!r} (known: {', '.join(sorted(t['formats']))})")
    if "use" in spec:
        spec = {**tab["templates"][spec["use"]], **{k: v for k, v in spec.items() if k != "use"}}
    hits = {h for h in _KINDS[spec["kind"]](tool, t, spec, text, ver, root) if h and (h.claim or keep_all)}
    return sorted(hits, key=lambda h: (h.path, h.line, h.col, h.rule, h.name, h.msg))


# ---------------------------------------------------------------- evidence on nodes
class Match(NamedTuple):
    node: str
    hit: Hit
    exact: bool  # the node is the one the engine names; False = only the narrowest node around the line
    ev: int  # index into Graph.evid of the returned graph


def _tail(name: str) -> str:
    """The last name of a symbol: `pkg.Type.m`, `Type::m`, `f()` and the Go method `(*T).m` (receiver dropped) all end in the name."""
    name = re.sub(r"\([^()]*\)\.", "", name.strip())
    return re.split(r"[.:\s]+", re.sub(r"\(.*", "", name).strip())[-1] if name else ""


def _span(n):
    return (max(n.line1, n.line0) - n.line0, n.key)


def _locate(by_path: Mapping[str, list], hit: Hit, kinds: Mapping[str, list]) -> Optional[Tuple[str, bool]]:
    nodes = by_path.get(hit.path, ())
    files = [n for n in nodes if n.kind in ("file", "module")]
    if hit.claim == "file":
        return (files[0].key, True) if files else None
    pool = [n for n in nodes if n.kind not in ("file", "module") and n.line0 <= hit.line <= max(n.line1, n.line0)]
    named = [n for n in pool if _tail(split_key(n.key)[2]) == _tail(hit.name) and n.kind in kinds.get(hit.claim, ())]
    if named and hit.name:
        return min(named, key=_span).key, True
    if pool:
        return min(pool, key=_span).key, False
    return (files[0].key, False) if files else None


def _by_path(graph: Graph) -> Dict[str, list]:
    out: Dict[str, list] = {}
    for n in graph.nodes:
        out.setdefault(n.path, []).append(n)
    return out


def attach(graph: Graph, hits: Iterable[Hit]) -> Tuple[Graph, List[Match]]:
    kinds = table()["claim_nodes"]
    by_path = _by_path(graph)
    found = []
    for h in sorted(set(hits)):
        at = _locate(by_path, h, kinds)
        if at:
            ev = evidence(
                h.tool, h.path, h.line, f"{h.rule} {h.name}".strip(), f"{h.tool}@{h.ver}" if h.ver else h.tool
            )
            found.append((at[0], h, at[1], ev))
    g = canonical(graph.nodes, graph.edges, graph.roots, graph.evid + tuple(e for *_, e in found))
    where = {e: i for i, e in enumerate(g.evid)}
    return g, [Match(n, h, ex, where[e]) for n, h, ex, e in found]


def to_payload(matches: Iterable[Match]) -> bytes:
    rows = sorted([m.node, m.exact, *m.hit] for m in matches)
    return json.dumps(rows, separators=(",", ":"), sort_keys=True).encode()


def from_payload(payload: bytes) -> List[Tuple[str, bool, Hit]]:
    return [(r[0], bool(r[1]), Hit(*r[2:])) for r in json.loads(payload)]


# ---------------------------------------------------------------- graph against engine
class Disagreement(NamedTuple):
    side: str  # graph-alive: the engine says unused, the graph holds it alive | engine-alive: a reach engine does not list it
    node: str
    verdict: str
    tool: str
    ver: str
    rule: str
    path: str
    line: int
    name: str
    claim: str


def disagree(
    graph: Graph, matches: Iterable[Match], res, covered: Optional[Mapping[str, Tuple[str, Iterable[str]]]] = None
):
    """res needs .verdict(key) (reach.Result); .excluded(key) is used when it has one. covered = {tool: (version, paths the
    engine analysed)}: only a reach engine (data: "reach": [languages]) that says what it analysed can be silent."""
    tab = table()
    quiet, alive = set(tab["no_dispute"]), set(tab.get("alive_claims", ()))
    skip = getattr(res, "excluded", lambda k: False)
    out, told = [], set()
    for m in matches:
        h = m.hit
        if (
            h.claim in alive
        ):  # an engine that says something alive about a node (alive_claims): it disputes a node the graph holds dead
            if m.exact and res.verdict(m.node) in DEAD_SIDE and not skip(m.node):
                out.append(
                    Disagreement(
                        "engine-alive",
                        m.node,
                        res.verdict(m.node),
                        h.tool,
                        h.ver,
                        h.rule,
                        h.path,
                        h.line,
                        h.name,
                        h.claim,
                    )
                )
            continue
        if m.exact and h.claim and h.claim not in quiet and h.claim in tab["claim_nodes"]:
            told.add((h.tool, m.node))
            v = res.verdict(m.node)
            if v in ALIVE_SIDE:
                out.append(
                    Disagreement("graph-alive", m.node, v, h.tool, h.ver, h.rule, h.path, h.line, h.name, h.claim)
                )
    for tool, (ver, paths) in sorted((covered or {}).items()):
        langs, paths = tab["tools"].get(tool, {}).get("reach", ()), set(paths)
        for n in graph.nodes:
            if n.path in paths and n.lang in langs and n.kind in ("func", "method") and (tool, n.key) not in told:
                v = res.verdict(n.key)
                if v in DEAD_SIDE and not skip(n.key):
                    out.append(
                        Disagreement(
                            "engine-alive", n.key, v, tool, ver, "", n.path, n.line0, split_key(n.key)[2], "func"
                        )
                    )
    return sorted(out)


def _disputed(graph: Graph, hits: Sequence[Hit], res) -> List[Hit]:
    """`hits` without the alive claims (data alive_claims) that dispute nothing: one row per reachable method of a project would fill
    the evidence pool, and only one about a node the graph holds dead says anything."""
    alive, kinds = set(table().get("alive_claims", ())), table()["claim_nodes"]
    if not any(h.claim in alive for h in hits):
        return list(hits)
    by_path, keep = _by_path(graph), []
    for h in hits:
        at = _locate(by_path, h, kinds) if h.claim in alive else None
        if h.claim not in alive or (at and at[1] and res.verdict(at[0]) in DEAD_SIDE):
            keep.append(h)
    return keep


def to_findings(dis: Iterable[Disagreement]) -> List[Finding]:
    out = []
    for d in dis:
        if d.side == "graph-alive":
            msg = f"the graph holds {d.node} {d.verdict}, but {d.tool} {d.rule} finds {d.name or 'it'} unused"
            why = f"{d.tool}@{d.ver or '?'} {d.rule} at {d.path}:{d.line} claims {d.claim} {d.name}".rstrip()
        else:
            msg = f"the graph holds {d.node} {d.verdict}, but {d.tool} does not list it as unreachable"
            why = f"{d.tool}@{d.ver or '?'} analysed {d.path} and found the function reachable"
        why += "\nthe verdict stays with the graph; the engine result is evidence on the node, see `graph why`"
        out.append(Finding("W", d.path, d.line, "dc.engine-disagree", msg, why))
    return out


# ---------------------------------------------------------------- ledger
def ledger_record(d: Disagreement) -> dict:
    return {"kind": "engine-disagree", **d._asdict()}


def _mkdirs(d: str) -> None:
    """Like os.makedirs, but every directory it creates is 0700 (makedirs gives the parents the default mode)."""
    todo = []
    while d and not os.path.isdir(d) and d not in todo:
        todo.append(d)
        d = os.path.dirname(d)
    for p in reversed(todo):
        with contextlib.suppress(FileExistsError):
            os.mkdir(p, 0o700)


def _lock(fd: int) -> None:
    """An exclusive lock on the open file, released when it is closed (flock; msvcrt on Windows, not run on this host)."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


def ledger_append(path: str, records: Iterable[dict]) -> int:
    """Append the records that are not in the file yet (jsonl, sorted keys, no clock: the same run writes nothing twice).
    Read and append happen under one lock, so parallel runs never write a line twice. Directories it creates 0700 (all of
    them, not only the last), file 0600, a symlink in place of the file is refused. Returns the number of lines written.
    """
    lines = sorted({json.dumps(r, sort_keys=True, separators=(",", ":")) for r in records})
    if not lines:
        return 0
    d = os.path.dirname(os.path.abspath(path))
    _mkdirs(d)
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        _lock(fd)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as fh:
            have = set(fh.read().splitlines())
        new = [ln for ln in lines if ln not in have]
        if new:
            os.write(fd, ("\n".join(new) + "\n").encode())
        return len(new)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- the whole path
class Run(NamedTuple):
    tool: str
    text: str
    ver: str = ""
    fmt: str = ""
    root: str = ""
    covered: Tuple[str, ...] = ()  # files the engine analysed (a reach engine only)
    rc: Optional[int] = None  # the exit code of the engine; None = the caller does not say (not checked)
    prefix: str = ""  # the directory the engine ran in, below the root ("" = the root): its paths are relative to it


def rc_ok(tool: str, rc: Optional[int]) -> bool:
    """A healthy exit code of the engine (data ok_rc); True when the code is unknown or the engine has no list."""
    codes = table()["tools"].get(tool, {}).get("ok_rc")
    return rc is None or codes is None or rc in codes


class Reconciled(NamedTuple):
    graph: Graph
    matches: Tuple[Match, ...]
    disagreements: Tuple[Disagreement, ...]
    findings: Tuple[Finding, ...]


def reconcile(graph: Graph, runs: Sequence[Run], res, ledger: Optional[str] = None) -> Reconciled:
    hits, bad, covered = [], [], {}
    for r in runs:
        if not rc_ok(r.tool, r.rc):
            why = f"exit code {r.rc} (a healthy run ends with {table()['tools'][r.tool]['ok_rc']}): the engine failed, its output is not used"
            bad.append(Finding("W", "", 0, "dc.engine-output", f"{r.tool}: {why}"))
            continue
        try:
            hits += [
                h._replace(path=posixpath.join(r.prefix, h.path)) if r.prefix else h
                for h in parse(r.tool, r.text, r.fmt or None, r.root, r.ver)
            ]
        except AdapterError as e:  # unread output says nothing about the graph either: not covered
            bad.append(Finding("W", "", 0, "dc.engine-output", f"the output of {e.tool} was not read: {e.reason}"))
            continue
        if r.covered:  # several runs of one engine (a Go module each): the files they analysed add up
            covered[r.tool] = (r.ver, tuple(sorted(set(covered.get(r.tool, ("", ()))[1]) | set(r.covered))))
    g, matches = attach(graph, _disputed(graph, hits, res))
    dis = disagree(g, matches, res, covered)
    if ledger and dis:
        ledger_append(ledger, [ledger_record(d) for d in dis])
    return Reconciled(g, tuple(matches), tuple(dis), tuple(bad + to_findings(dis)))
