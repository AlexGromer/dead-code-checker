"""graph.extract.tpl: the Jinja2 template layer (Ansible, cookiecutter, Salt ...).

The grammar (tree-sitter-jinja2) is flat: every `{% .. %}` is one statement node and a call written in `{{ }}` is a call node, so the
statements are located by the grammar (@stmt in data/queries/jinja2/refs.scm) and read by their keyword. Facts are a function of
(lang, text); a grammar that is not installed is ExtractorMissing (graph.extract.cfg.parse), never a fallback.
  def     `{% macro name(args) %} .. {% endmacro %}` = "macro:<name>" (kind template: a macro is never reported, the viewer shows it).
  refs    a macro call (`{{ name(..) }}`, `{% call name(..) %}`, a call inside a statement), from the macro that holds it, else from the
          file: `name` itself, `alias.name` for a module bound by `{% import "f" as alias %}`, `local` for a name bound by
          `{% from "f" import name as local %}` (the macro `name`); a call of a name that is no macro anywhere links to nothing
          (builtins: data cfg.json jinja2). Macros are matched by name across the jinja2 files (N), as the files are not linked by
          what they import: an import is resolved by the loader's search path, the tree cannot tell which file.
  paths   `extends`, `include` (a list: every string), `import` and `from` give `include` refs to the file (a template name is looked up
          like a path: relative to the including file or by its tail, link.py path_targets); a computed name gives nothing.
          Ansible YAML names templates through `template: src=` (graph.extract.cfg _tool_refs).
  root    a jinja2 file is rendered by another engine from outside the tree (Ansible, a web framework): a soft `template` root of the
          file, never dead for want of a reader the extractor cannot see (as helm-template).
  values  a `.j2` file below a chart's templates/ is still rendered by Helm (Go templates): the `.Values` reads of its `{{ }}` actions are
          read like the Helm layer does (`.Values` is no Jinja2 syntax, so no real Jinja2 template has one).
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from ..contracts import Extractor, register_extractor
from ..model import Facts, Finding, RootHint
from . import cfg
from .ts_generic import probe_grammar

_STMT = re.compile(r"\{%[-+]?\s*(\w+)(.*?)[-+]?%\}", re.S)
_STR = re.compile(r"\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'")
_CALL = re.compile(r"(?<![\w.])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(")
_NAME = re.compile(r"\s*([A-Za-z_]\w*)")
_AS = re.compile(r"\bas\s+([A-Za-z_]\w*)")
_FROM = re.compile(r"\bimport\s+(.*?)(?:\s+with(?:out)?\s+context)?\s*$", re.S)


def _strings(text: str) -> List[str]:
    return [a or b for a, b in _STR.findall(text)]


def _tpl(text: str, data: bytes, parsed) -> Facts:
    out, builtins = cfg._Out(), frozenset(cfg.rules()["jinja2"]["builtins"])
    nodes = []  # (start byte, line, kind, text)
    for _, caps in parsed.run("refs"):
        for kind in ("stmt", "call"):
            for n in caps.get(kind, ()):
                nodes.append(
                    (
                        n.start_byte,
                        n.start_point[0] + 1,
                        kind,
                        data[n.start_byte : n.end_byte].decode("utf-8", "replace"),
                    )
                )
    stack: List[Tuple[str, int]] = []  # open macros (qual, line)
    modules: Dict[str, str] = {}  # `import "f" as m`: the alias of a module
    macros: Dict[str, str] = {}  # `from "f" import a as b`: local name -> macro name

    def call(name: str, line: int) -> None:
        head, _, rest = name.partition(".")
        if rest:  # alias.macro(): a module alias; any other dotted name is an object (loop.cycle, request.get)
            target = rest if head in modules and "." not in rest else ""
        else:
            target = macros.get(name, name)
        if target and target not in builtins and name not in builtins:
            out.r(f"macro:{cfg._q(target)}", "ref", line, stack[-1][0] if stack else "")

    for _, line, kind, body in sorted(nodes):
        if kind == "call":
            m = _CALL.match(body)
            if m:
                call(m.group(1), line)
            continue
        m = _STMT.search(body)
        if not m:
            continue
        kw, rest, line = (
            m.group(1),
            m.group(2),
            line + body.count("\n", 0, m.start()),
        )  # the node starts at the blank before `{%`
        strs = _strings(rest)
        if kw == "macro":
            name = _NAME.match(rest)
            if name:
                stack.append((f"macro:{cfg._q(name.group(1))}", line))
            continue
        if kw == "endmacro" and stack:
            qual, l0 = stack.pop()
            out.d(qual, "template", l0, line)
            continue
        if kw in ("extends", "include", "import", "from") and strs:
            for p in strs if kw == "include" and rest.lstrip().startswith("[") else strs[:1]:
                if cfg._path(p):
                    out.r(cfg._path(p), "include", line, stack[-1][0] if stack else "")
            if kw == "import" and _AS.search(rest):
                modules[_AS.search(rest).group(1)] = strs[0]  # type: ignore[union-attr]
            elif kw == "from" and _FROM.search(rest):
                for item in _FROM.search(rest).group(1).split(","):  # type: ignore[union-attr]
                    parts = item.split()
                    if len(parts) == 3 and parts[1] == "as":
                        macros[parts[2]] = parts[0]
            continue
        rest = (
            re.sub(r"^\s*\([^)]*\)", "", rest) if kw == "call" else rest
        )  # `{% call(x) name() %}`: the caller's arguments
        for c in _CALL.findall(rest):
            call(c, line)
    for qual, l0 in stack:  # a macro that is never closed still defines its name
        out.d(qual, "template", l0, l0)
    # a chart that names its templates *.j2: helm renders them, and `.Values` is no Jinja2
    for m in cfg._ACTION.finditer(text):
        if cfg._VALUES.search(m.group()) or cfg._VALUES_BARE.search(m.group()):
            cfg._expr(m.group()[2:-2], text.count("\n", 0, m.start()) + 1, "", out)
    out.roots.append(RootHint("template", "", 1, "soft", "convention"))
    return out.facts("jinja2", text, int(parsed.errors))


class Tpl(Extractor):
    name, version, langs = "tpl", 1, ("jinja2",)

    def grammar_sha(self, lang: str) -> str:
        # the mapping code and the helpers it calls (cfg._Out, _expr, _path, _q ...): an edit of either is a new key
        return cfg.grammar_sha(lang, os.path.abspath(__file__), cfg.__file__)

    def probe(self, lang: str, cfg: Optional[dict] = None) -> Optional[Finding]:
        return probe_grammar(lang, cfg)

    def extract(self, lang: str, path: str, text: str) -> Facts:
        text = text[1:] if text[:1] == "﻿" else text
        data = text.encode("utf-8", "replace")
        return _tpl(text, data, cfg.parse(lang, data))


register_extractor(Tpl())
