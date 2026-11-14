"""graph.verdict: the corroboration rule for a "dead" verdict. Standard library only.

The syntactic graph does not see types, reflection or the conventions of a framework, so on its own it gives false "dead". A
finding that calls a CODE SYMBOL dead (data/verdict.json "codes": dc.dead, dc.dead-cluster, dc.introduced-dead) is what it says
only when something corroborates it:
  S-tier   a semantic index (SCIP) was applied to the package of the symbol: the edge set that covers it shows no caller
  engine   the native engine of the language agrees: an EXACT hit on the symbol by an engine of data/verdict.json "engines"
           (go-deadcode RTA, rustc dead_code, knip, PMD, Roslyn, vulture at 100% confidence)
The corroboration is written into the why of the finding ("corroborated: <which index or engine>"). Without it the finding
becomes `dc.suspect`, level I (never E, never W): the old code stays in front of the message ("[dc.dead] ..."), the report
hides it by default ("N suspects hidden, --show-suspects"), the viewer draws it in its own colour. Left to the graph (this
module never touches them): stub-entry, root-dangling, dangling-ref, orphaned-by-branch, cross-language edges.

    Proof(covered, nodes, confs)      what corroborates: paths of S-covered packages, node key -> text of an engine hit; `confs` is the
                                      engine's own confidence per node, read by graph.confidence only (it corroborates nothing)
    build(covered, matches)           a Proof from the covered paths and a list of engine matches (an exact one counts; `confs` takes
                                      the percent an engine gave to an exact dead-symbol hit, counted or not)
    both(a, b) -> Proof               the proof of two sources: a node is corroborated when either is
    Proof.text(key, path)             "" = nothing corroborates the node
    settle(f, text, codes) -> Finding  f with the proof in its why, or the dc.suspect that replaces it; other codes pass unchanged
                                      (`codes`: the claims to settle, default data "codes")
    was(f) -> str                     the code a dc.suspect replaced ("" for any other finding)
    proof_of(f) -> str                the corroboration text settle() wrote into the why of f ("" = none); proof_tool(text) -> str
                                      names who gave it: "s-tier", or the engine ("rustc" of "rustc@1.80 dead_code at a.rs:3")
    claim(f) -> str                   the code of the claim a finding makes: was(f), else its own code (waives)
    schema_lang(lang) -> bool         a schema language (data "schema": sql, proto, graphql): its claims are settled by the same rule; claims(lang)
                                      are the codes settle() reads for the nodes of a language: "codes" and, for these, the claims of "schema"
    lsp_blind(graph, nodes) -> dict   node key -> why, for the nodes whose "no reference" answer of a language server proves
                                      nothing (a hook a base class or a framework calls by name); only a node that is NOT in it
                                      can be corroborated by the silence of a server (data/verdict.json "lsp_blind")
    whole_clusters(clusters, kinds, ok) -> set   the keys of the clusters in which EVERY symbol is corroborated (the members
                                      only use each other: one hook nobody can vouch for keeps the rest alive)
    SEVERITY                          the verdicts, worst first: the order of the call tree and of the viewer
A failure here is never a block: a Proof that is empty (no overlay, no engine) only makes suspects of the claims.
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, Iterable, Mapping, NamedTuple, Optional, Tuple

from .model import F_DECORATED, F_DYNAMIC, F_OVERRIDE, F_PROTOCOL, Finding, split_key

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "verdict.json")
RULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "rules.json")
SUSPECT = "dc.suspect"
SUSPECTS = (
    "suspect"  # the pseudo-verdict of a dead symbol that nothing corroborates (the code of its finding is SUSPECT)
)
SEVERITY = (
    "dead-cluster",
    "dead",
    SUSPECTS,
    "weak-alive",
    "unknown",
    "public-unused",
    "test-only",
    "doc-only",
    "alive",
)
BLIND_FLAGS = F_OVERRIDE | F_PROTOCOL | F_DYNAMIC | F_DECORATED
NOT_PROVEN = "not corroborated: no S-tier index covers the package and no native engine of the language agrees"
_WAS = re.compile(r"^\[([\w.-]+)\] ")
_TABLE: Dict[str, dict] = {}


def table() -> dict:
    if DATA not in _TABLE:
        with open(DATA, encoding="utf-8") as fh:
            _TABLE[DATA] = json.load(fh)
    return _TABLE[DATA]


class Proof(NamedTuple):
    covered: frozenset = (
        frozenset()
    )  # paths of the packages whose SCIP overlay was applied in full (the semantic overlay)
    nodes: Mapping[str, str] = (
        {}
    )  # node key -> "tool@ver rule at path:line" of an authoritative engine hit on that node
    confs: Mapping[str, int] = (
        {}
    )  # node key -> the highest confidence (percent) an engine gave to an exact dead-symbol hit on it, corroborating or not (graph.confidence reads it; never a verdict)

    def text(self, key: str, path: str) -> str:
        if path in self.covered:
            return f"S-tier: the SCIP index of the package of {path} was applied and shows no caller"
        return self.nodes.get(key) or ""


def agrees(tool: str, rule: str = "", claim: str = "", conf: int = 0) -> bool:
    """True when an engine hit with this rule, claim and confidence is one that corroborates (data/verdict.json "engines")."""
    spec = table()["engines"].get(tool)
    if spec is None:
        return False
    return (
        ("rules" not in spec or rule in spec["rules"])
        and ("claims" not in spec or claim in spec["claims"])
        and conf >= spec.get("min_conf", 0)
    )


def _hit_text(tool: str, ver: str, rule: str, path: str, line: int) -> str:
    return f"{tool}{'@' + ver if ver else ''} {rule} at {path}:{line}".replace("  ", " ")


def build(covered: Iterable[str] = (), matches: Iterable = ()) -> Proof:
    """`matches` are engine matches (objects with node, exact and hit = tool, ver, rule, claim, conf, path, line): only an exact
    one counts, and only when the engine table of data/verdict.json accepts the hit."""
    matches = list(matches)
    nodes: Dict[str, str] = {}
    for m in sorted(matches, key=lambda m: (m.node, m.hit.path, m.hit.line, m.hit.rule)):
        h = m.hit
        if m.exact and agrees(h.tool, h.rule, h.claim, h.conf):
            nodes.setdefault(m.node, _hit_text(h.tool, h.ver, h.rule, h.path, h.line))
    confs: Dict[str, int] = {}
    for m in matches:
        if m.exact and m.hit.conf > 0 and m.hit.claim in table()["symbols"]:
            confs[m.node] = max(confs.get(m.node, 0), m.hit.conf)
    return Proof(frozenset(covered), nodes, confs)


class _Joined(Proof):
    parts: tuple = ()

    def text(self, key: str, path: str) -> str:
        return next((t for t in (p.text(key, path) for p in self.parts) if t), "")


def both(a: Proof, b: Proof) -> Proof:
    """A proof that holds wherever `a` or `b` does (`a` first). Two plain proofs merge their tables; a proof that answers text() by
    its own rule (a subclass) stays in charge of its part."""
    if type(b) is Proof and not (b.covered or b.nodes or b.confs):
        return a
    if type(a) is Proof and not (a.covered or a.nodes or a.confs):
        return b
    cf = {k: max(a.confs.get(k, 0), b.confs.get(k, 0)) for k in sorted({*a.confs, *b.confs})}
    if type(a) is Proof and type(b) is Proof:
        return Proof(a.covered | b.covered, {**b.nodes, **a.nodes}, cf)
    j = _Joined(a.covered | b.covered, {**b.nodes, **a.nodes}, cf)
    j.parts = (a, b)
    return j


def schema_lang(lang: str) -> bool:
    return lang in table()["schema"]["langs"]


def claims(lang: str) -> Tuple[str, ...]:
    """The finding codes that settle() reads for a node of `lang`: the dead-symbol claims of the data file, and for a schema language more."""
    return (*table()["codes"], *(table()["schema"]["claims"] if schema_lang(lang) else ()))


def settle(f: Finding, text: str, codes: Optional[Iterable[str]] = None) -> Finding:
    """`f` with `text` (the proof) added to its why, or, without a proof, the dc.suspect that stands for it. A finding whose code is not
    one of `codes` (default: the dead-symbol claims of data/verdict.json) is returned as it is."""
    if f.code not in (table()["codes"] if codes is None else codes):
        return f
    tail = f.why or ""
    if text:
        f.why = f"{tail}\ncorroborated: {text}".lstrip("\n")
        return f
    return Finding("I", f.path, f.line, SUSPECT, f"[{f.code}] {f.msg}", f"{NOT_PROVEN}\n{tail}".rstrip("\n"))


def proof_of(f) -> str:
    """The text of the corroboration that settle() put into the why of `f` ("" when it has none, or `f` is a dc.suspect)."""
    return next(
        (ln[len("corroborated: ") :] for ln in reversed((f.why or "").split("\n")) if ln.startswith("corroborated: ")),
        "",
    )


def proof_tool(text: str) -> str:
    """Who gave a corroboration text: "s-tier" for the SCIP overlay, else the engine name (the word before `@` or the first space)."""
    return "s-tier" if text.startswith("S-tier") else text.split(" ", 1)[0].split("@", 1)[0]


def was(f) -> str:
    """The code a dc.suspect replaced, "" for any other finding."""
    m = _WAS.match(f.msg) if f.code == SUSPECT else None
    return m.group(1) if m else ""


def claim(f) -> str:
    """The code of the claim `f` makes: the claim a dc.suspect stands for, else the finding's own code. A waive of the
    configuration goes by it, so a suspect is silenced like the finding it replaced."""
    return was(f) or f.code


def _protocol_names(lang: str) -> frozenset:
    """The names data/rules.json lists for `lang` as called through an interface of the language or a library."""
    with open(RULES, encoding="utf-8") as fh:
        return frozenset((json.load(fh).get(lang) or {}).get("protocol_names", ()))


def lsp_blind(graph, nodes: Iterable) -> Dict[str, str]:
    """{node key: reason} for the nodes whose "no reference" answer of a language server corroborates nothing. A server answers about
    the code it is given: a method that a base class, a framework or an interface of a library calls BY NAME (urllib calls
    `<scheme>_open` of a handler, `http.Handler` calls ServeHTTP) has no reference in the tree and is alive. Blind are
      - a node flagged override, protocol, dynamic-zone or decorated, and a dunder
      - a node with an `override` edge into it (the class has a base that the tree lacks, or the method overrides one) or an `impl`
        edge out of it (it implements a member of a base)
      - a method whose name is in data/verdict.json "lsp_blind" "names" of its language (urllib handlers ...) or in the protocol_names
        of data/rules.json
      - a method of a language with implicit interfaces (data "implicit"): the exported ones may satisfy an interface the tree lacks
    A plain function or class, and a method of a class whose bases are all in the tree and that overrides nothing, are not blind.
    """
    spec = table()["lsp_blind"]
    over = {e.dst for e in graph.edges if e.kind == "override"} | {e.src for e in graph.edges if e.kind == "impl"}
    names = {lang: [re.compile(x) for x in pats] for lang, pats in spec["names"].items()}
    out: Dict[str, str] = {}
    for n in nodes:
        qual = split_key(n.key)[2]
        last = qual.rpartition(".")[2]
        why = ""
        if n.flags & BLIND_FLAGS:
            why = "it is flagged override, protocol, dynamic or decorated: something outside the code calls it"
        elif last[:2] == last[-2:] == "__" and len(last) > 4:
            why = "a dunder is called by the interpreter, never by name"
        elif n.key in over:
            why = "it overrides or implements a method of a base, or its class has a base outside the tree"
        elif n.kind == "method" and any(r.match(last) for r in names.get(n.lang, ())):
            why = f"`{last}` is a method the framework calls by name (data/verdict.json lsp_blind)"
        elif n.kind == "method" and last in _protocol_names(n.lang):
            why = f"`{last}` is a protocol name of {n.lang} (data/rules.json)"
        elif n.kind == "method" and n.lang in spec["implicit"]["langs"] and n.vis in spec["implicit"]["vis"]:
            why = (
                f"{n.lang} types satisfy interfaces implicitly: an exported method may satisfy one that the tree lacks"
            )
        if why:
            out[n.key] = why
    return out


def whole_clusters(clusters: Iterable[Tuple[str, ...]], kinds: Mapping[str, str], ok: Iterable[str]) -> set:
    """The keys of the dead clusters in which EVERY symbol (a func, method or class: the kinds of data/verdict.json "symbols") is in
    `ok` (corroborated). The members only use each other, so one that nothing vouches for may be a hook that keeps all of them alive:
    a cluster with one such member is not corroborated at all. `kinds` = node key -> kind."""
    have, symbols, out = set(ok), set(table()["symbols"]), set()
    for c in clusters:
        if all(k in have for k in c if kinds.get(k) in symbols):
            out.update(c)
    return out
