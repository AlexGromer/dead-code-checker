"""graph.extract.cfgts: the tree-sitter config languages make, just, dockerfile, nginx, jsonnet, libsonnet.
Queries are DATA (data/queries/<grammar>/{defs,refs}.scm), the
rules are data/cfg.json (make, just, nginx), this module only maps matches to Facts, a function of (lang, text) only. A grammar that is
not installed is ExtractorMissing (cfg.parse), never a fallback. Names are "<tag>:<name>" with the name escaped like in graph.extract.cfg (_q).
  make       def "recipe:<target>" (kind job) per named target of a rule (a pattern rule `%.o`, a computed target and the special targets
             `.PHONY` ... are no recipes). Refs: the prerequisites of a rule (normal and order-only), what its recipe lines run
             (`$(MAKE) x`, `make -C d x`, `just x`: graph.roots.build_targets), from the recipe that holds them; a computed word (`build: $(SUBS)`,
             `$(MAKE) $(T)`, `a-$(X)`) is a dynamic site that reads every recipe it can name (the Helm layer does the same), never a skipped
             word (`$(wildcard ..)` and the option variables `$(MAKEFLAGS)` name no recipe); `include` /
             `-include` files (a `$(wildcard d/*.mk)` is a pattern) as `include` refs. Roots, soft and by convention (data make): the default
             goal (`.DEFAULT_GOAL`, else the first target), the conventional names (`all`, `test`, `clean` ...) and the .PHONY targets
             (declared as commands: people, CI and docs run them); a recipe none of that reaches, and no CI line, other recipe or
             Makefile names, is dead. CI lines name recipes through cfg.py, shell scripts through the cross-language facts of ts_generic (xlang), docs
             through the roots layer (graph.roots DocRoots: build:recipe:x), a Dockerfile RUN below. A Makefile without any
             recipe (variables and includes only) is read by make from outside: a soft root of the file. What a recipe RUNS stays with
             graph.rootsrc (the commands are roots there).
  just       the same for a justfile: def "recipe:<name>"; refs: dependencies (`b`, `(b "arg")`), `just x` / `just {{ t }}` lines, `alias a := b`;
             `import 'f'` and `mod m 'd'` give `include` refs (data just.mod_files). A recipe is public unless its name starts with `_` or it
             has the `[private]` attribute: a public one is a soft root (`just --list` shows it), a private one lives through its callers.
  dockerfile def "stage:<name>" (kind resource, lower case: Docker compares stage names without case) per FROM, "stage:<index>" for a stage
             without `AS`. Refs from the stage that holds them: `FROM x` (x an earlier stage), `COPY --from=x` / `ADD --from=x` /
             `RUN --mount=from=x` (a number is the index of the stage); the file reaches its LAST stage (what `docker build` builds). The
             recipes a RUN / CMD / ENTRYPOINT line names (`RUN make x`) are roots of graph.rootsrc, as the scripts it runs are.
             CI, compose and docker build lines name a stage through cfg.py (`--target x`, `build: {target: x}`). The file itself is no
             root: an unreferenced Dockerfile is dead (graph.rootsrc reads what it RUNS).
  nginx      `include` and the directives that name a file of the tree (data nginx.file_directives: js_import, load_module,
             *_by_lua_file ...) give `include` refs; root, alias, ssl_certificate and the like are no code and give nothing. An nginx file
             is read by the server: a soft root of the file.
  jsonnet    `import` / `importstr` files as `include` refs. A .jsonnet file is a program that jsonnet or tanka renders: a soft root; a
             .libsonnet file lives while something imports it.
"""

from __future__ import annotations

import os
import re
from bisect import bisect_right
from typing import Dict, List, Optional, Tuple

from ..contracts import Extractor, register_extractor
from ..model import Facts, Finding, RootHint
from . import cfg
from .ts_generic import probe_grammar

_PLAIN = re.compile(r"[^%$\s]+")


class _Src:
    """A parse with the helpers every mapping needs."""

    def __init__(self, data: bytes, parsed):
        self.data, self.parsed, self.out = data, parsed, cfg._Out()

    def txt(self, n) -> str:
        return self.data[n.start_byte : n.end_byte].decode("utf-8", "replace")

    @staticmethod
    def line(n) -> int:
        return n.start_point[0] + 1

    def scoped(self, spans: List[Tuple[int, int, List[str]]], pos: int) -> List[str]:
        """The quals of the innermost definition span that contains `pos` ([''] = the file)."""
        return next((q for s, e, q in reversed(spans) if s <= pos < e), [""])

    def ref(self, name: str, kind: str, node, scopes: List[str]) -> None:
        for sc in scopes:
            self.out.r(name, kind, self.line(node), sc)

    def build(self, text: str, node, scopes: List[str]) -> None:
        """The recipes and stages a shell text names, from each of `scopes` (refs, computed words as dynamic sites)."""
        self.out.build(text, self.line(node), scopes)

    def include(self, path: str, node, scopes: List[str] = ("",)) -> None:  # type: ignore[assignment]
        p = cfg._path(path)
        if p:
            self.ref(p, "include", node, list(scopes))


def _soft_root(out: "cfg._Out", target: str, line: int, kind: str = "init") -> None:
    out.roots.append(RootHint(kind, target, line, "soft", "convention"))


# ================================================================== make
def _make(s: _Src) -> None:
    rd, out = cfg.rules()["make"], s.out
    special = re.compile(rd["special"])
    wild = tuple(f"$({f}" for f in rd["file_funcs"])
    found: Dict[Tuple[int, int], dict] = {}
    for _, caps in s.parsed.run("defs"):
        for r in caps.get("rule", ()):
            e = found.setdefault((r.start_byte, r.end_byte), {"node": r, "targets": {}})
            e["targets"].update({t.start_byte: s.txt(t) for t in caps.get("target", ())})
    spans, first, phony, defined = [], "", set(), set()
    for (b, e), rule in sorted(found.items(), key=lambda kv: (kv[0][0], -kv[0][1])):
        all_t = [t for _, t in sorted(rule["targets"].items())]
        rule["special"] = bool(all_t) and all(special.match(t) for t in all_t)
        rule["names"] = [t for t in all_t if _PLAIN.fullmatch(t) and not special.match(t)]
        quals = [f"recipe:{cfg._q(t)}" for t in rule["names"]]
        for q in quals:
            out.d(q, "job", s.line(rule["node"]), rule["node"].end_point[0] + 1)
            defined.add(q)
        first = first or (rule["names"][0] if rule["names"] else "")
        spans.append((b, e, quals or [""]))
    goal = first
    for _, caps in s.parsed.run("refs"):
        for r in caps.get("rule", ()):
            rule = found.get((r.start_byte, r.end_byte)) or {
                "targets": {},
                "names": [],
                "special": False,
            }  # `$(BIN): x`: no word
            for w in caps.get("prereq", ()):
                name = s.txt(w)
                scopes = [f"recipe:{cfg._q(t)}" for t in rule["names"]] or [""]
                if rd["phony"] in rule["targets"].values():
                    if _PLAIN.fullmatch(name):
                        phony.add(f"recipe:{cfg._q(name)}")
                elif rule["special"] or "%" in name or name.startswith(wild):
                    continue  # `.SUFFIXES: .c`, a pattern prerequisite `%.c`, `$(wildcard ..)` (files): no recipe
                elif _PLAIN.fullmatch(name):
                    s.ref(f"recipe:{cfg._q(name)}", "ref", w, scopes)
                elif "$" in name:
                    s.build(f"make {name}", w, scopes)  # `$(SUBS)`, `a-$(X)`: every recipe it can name
        for p in caps.get("path", ()):
            s.include(s.txt(p), p)
        for c in caps.get("cmd", ()):
            s.build(s.txt(c), c, s.scoped(spans, c.start_byte))
        for v in caps.get("var", ()):
            if s.txt(v) == rd["goal"] and caps.get("val"):
                goal = s.txt(caps["val"][0]).strip()
    public = {f"recipe:{cfg._q(n)}" for n in [goal, *rd["public"]] if n} | phony
    for q in sorted(public & defined):
        _soft_root(out, q, next(d.line0 for d in out.defs if d.qual == q))
    out.unknown("init")  # variables and includes only: make reads the file from outside


# ================================================================== just
def _just(s: _Src) -> None:
    rd, out = cfg.rules()["just"], s.out
    recipes: Dict[Tuple[int, int], dict] = {}
    for _, caps in s.parsed.run("defs"):
        for r in caps.get("recipe", ()):
            e = recipes.setdefault((r.start_byte, r.end_byte), {"node": r, "name": "", "private": False})
            e["name"] = s.txt(caps["name"][0]) if caps.get("name") else e["name"]
            e["private"] |= any(s.txt(a) in rd["private_attrs"] for a in caps.get("attr", ()))
    spans = []
    for (b, e), rec in sorted(recipes.items()):
        qual = f"recipe:{cfg._q(rec['name'])}"
        out.d(qual, "job", s.line(rec["node"]), rec["node"].end_point[0] + 1)
        spans.append((b, e, [qual]))
        if not (rec["private"] or rec["name"].startswith("_")):
            _soft_root(out, qual, s.line(rec["node"]))
    for _, caps in s.parsed.run("refs"):
        for r in caps.get("recipe", ()):
            for d in caps.get("dep", ()):
                out.r(
                    f"recipe:{cfg._q(s.txt(d))}",
                    "ref",
                    s.line(d),
                    f"recipe:{cfg._q(recipes[(r.start_byte, r.end_byte)]['name'])}",
                )
        for c in caps.get("cmd", ()):
            s.build(s.txt(c), c, s.scoped(spans, c.start_byte))
        for t in caps.get("target", ()):  # an alias is a way in: the file names the recipe
            out.r(f"recipe:{cfg._q(s.txt(t))}", "ref", s.line(t), "")
        path = s.txt(caps["path"][0]).strip("'\"") if caps.get("path") else ""
        for m in caps.get("mod", ()):  # `mod name 'dir'`: the justfile of the module may be written in several ways
            for tpl in rd["mod_files"]:
                s.include(tpl.format(p=path or s.txt(m)), m)
        if path and not caps.get("mod"):  # import 'file'
            s.include(path, caps["path"][0])
    out.unknown("init")


# ================================================================== dockerfile
def _dockerfile(s: _Src, text: str) -> None:
    out = s.out
    found = {}  # start byte of a FROM -> (node, alias)
    for _, caps in s.parsed.run("defs"):
        node = caps["stage"][0]
        found[node.start_byte] = (node, s.txt(caps["alias"][0]) if caps.get("alias") else "")
    starts = sorted(found)
    quals = [f"stage:{cfg._q(found[b][1].lower())}" if found[b][1] else f"stage:{i}" for i, b in enumerate(starts)]
    named = {found[b][1].lower(): i for i, b in enumerate(starts) if found[b][1]}
    for i, b in enumerate(starts):  # a stage runs to the line before the next FROM
        end = found[starts[i + 1]][0].start_point[0] if i + 1 < len(starts) else len(text.splitlines())
        out.d(quals[i], "resource", s.line(found[b][0]), max(s.line(found[b][0]), end))

    def use(name: str, node, kind: str) -> None:
        """The stage `name` (a name or the index of an EARLIER stage) is used from the stage that holds `node`."""
        here = bisect_right(starts, node.start_byte) - 1  # -1: before the first FROM (ARG)
        i = int(name) if name.isdigit() else named.get(name.lower())
        if i is not None and i < max(here, 0):
            out.r(quals[i], kind, s.line(node), quals[here] if here >= 0 else "")

    for _, caps in s.parsed.run("refs"):
        for key, rx in (("from", r"--from=(\S+)"), ("mount", r"(?:^|,)from=(\S+)")):
            for n in caps.get(key, ()):
                m = re.search(rx, s.txt(n))
                if m:
                    use(m.group(1).strip("\"'"), n, "ref")
        for n in caps.get("image", ()):  # `FROM builder`: a bare name (a tag or a digest makes it an image)
            use(s.txt(n), n, "ref")
    if quals:
        out.r(quals[-1], "ref", s.line(found[starts[-1]][0]), "")


# ================================================================== nginx
def _nginx(s: _Src) -> None:
    rd = cfg.rules()["nginx"]
    files = re.compile(rd["file_directives"])
    for _, caps in s.parsed.run("refs"):
        cmd, arg = s.txt(caps["cmd"][0]), caps["arg"][0]
        if cmd in rd["include"] or files.fullmatch(cmd):
            s.include(s.txt(arg).strip("\"'").lstrip("/"), arg)
    _soft_root(s.out, "", 1)


# ================================================================== jsonnet
def _jsonnet(s: _Src, lang: str) -> None:
    for _, caps in s.parsed.run("refs"):
        for p in caps.get("path", ()):
            s.include(s.txt(p), p)
    if lang == "jsonnet":
        _soft_root(s.out, "", 1)


# ================================================================== extractor
class CfgTs(Extractor):
    name, version, langs = "cfgts", 1, ("make", "just", "dockerfile", "nginx", "jsonnet", "libsonnet")

    def grammar_sha(self, lang: str) -> str:
        # the mapping code and the helpers it calls (cfg._Out, _path, _q ...): an edit of either is a new key
        return cfg.grammar_sha(lang, os.path.abspath(__file__), cfg.__file__)

    def probe(self, lang: str, cfg: Optional[dict] = None) -> Optional[Finding]:
        return probe_grammar(lang, cfg)

    def extract(self, lang: str, path: str, text: str) -> Facts:
        text = text[1:] if text[:1] == "﻿" else text
        data = text.encode("utf-8", "replace")
        s = _Src(data, cfg.parse(lang, data))
        if lang == "make":
            _make(s)
        elif lang == "just":
            _just(s)
        elif lang == "dockerfile":
            _dockerfile(s, text)
        elif lang == "nginx":
            _nginx(s)
        else:
            _jsonnet(s, lang)
        return s.out.facts(lang, text, int(s.parsed.errors))


register_extractor(CfgTs())
