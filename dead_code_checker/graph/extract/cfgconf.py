"""graph.extract.cfgconf: the config files of services that no grammar covers, read by small line and token scanners. Rules are DATA (data/cfgconf.json); this module only maps them to Facts, a function of (lang, text) only. No grammar,
nothing is run. An unknown directive is skipped, never an error; a name that is no file or an unbalanced brace gives what was read so far
(keepalived and Alloy count an unbalanced brace in Facts.errors). A file that is not in the tree is no edge and no finding here: link.py
resolves `include` / `exec` paths (an absolute path by its tail, a `${VAR}` part is a wildcard) and finds nothing to link; what the
removal of a referenced file breaks is a question for the edges of the graph, not a check of this module.

Refs: `exec` = a script or command line that the service runs (T when one file of the tree has the name, N when several have it, as every path
ref), `include` = a file the service reads (config, certificate, map, module). Every language below is a service config: read by the
service from outside the tree, so the file is a soft `init` root (as nginx in cfgts).
  haproxy     `haproxy.cfg` and a `.cfg` / `.conf` file with the section keywords at the start of a line (data/langs.json). Statements: the
              directives of data haproxy.directives (`lua-load`, `external-check command`, `errorfile CODE FILE`, `command` of a `program`
              section, `server-state-file`, `ssl-dh-param-file`), the keywords of data haproxy.token_keys anywhere in a line
              (`bind :443 ssl crt F ca-file F`, `server .. crl-file F`), `-f FILE` of an acl, `map(FILE)` converters, `.include FILE`. A
              command line is read by the command parser of graph.roots: the script is the first word (or the one after an interpreter).
  keepalived  `keepalived.conf` or a `.conf` with a block statement (`vrrp_instance X {`). Defs: `vrrp_script:<n>` (kind job) per vrrp_script block,
              `vrrp_instance:<n>` and `vrrp_sync_group:<n>` (kind resource). Refs: `track_script { n }` -> vrrp_script:n from the instance, `group { A B }`
              -> vrrp_instance:A from the sync group, the file -> every instance and sync group (keepalived runs them all). A vrrp_script that no
              instance tracks is never run: dead. Commands (data keepalived.exec_keys: `script "..."`, `notify*`, `notify_up`, `quorum_up`,
              `misc_path`) are `exec` refs from the block that holds them; `include PATTERN` is an `include` ref (a pattern is a glob). Names link across the
              files of the group, so an included file's scripts can be tracked by the instance of the including file (N).
  alloy       `.alloy`, `.river`. A block `type "label" {` is a component: def `component:<type>.<label>` (kind resource; inside a
              `declare` the name is prefixed with the declare). `declare "n" {` is def `declare:<n>`, used by a block of type `n` or `<import label>.n`
              (ref `declare:n`, the import label is the label of an `import.file`). Alloy starts every component, so the file (or the declare that holds it)
              refs each one: a component is never dead for want of a reader, the edges show the data flow: a dotted name in an expression
              (`prometheus.remote_write.mimir.receiver`) refs each prefix of 2..4 segments (`component:<prefix>`), the ones that name no
              component link to nothing. `import.file "l" { filename = "x.alloy" }` and the attributes of data alloy.path_keys with a string
              value are `include` refs; import.git / .http / .string read something outside the tree (no edge, said here, not skipped silently).
  redis       `redis.conf`, `sentinel.conf`, or a `.conf` that starts directives like `maxmemory` or `loadmodule` (data/langs.json). The
              directives of data redis.directives give refs: `include`, `loadmodule`, `aclfile`, the tls-* files, `dbfilename` / `appendfilename` /
              `logfile` / `pidfile` (also joined with `dir` when relative), `sentinel notification-script M PATH` and `client-reconfig-script` (exec).
              `rename-command` and every other directive name no file.
  logql       `.logql`: a query file that logcli or Grafana reads: a soft root, nothing to link. (The YAML rule files of the Loki ruler are read
              by graph.extract.cfg: a soft root through `unknown`, and `ruler.storage.local.directory` names the directory of the rules.)
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
from functools import lru_cache
from typing import Dict, List, NamedTuple, Optional, Tuple

from ..contracts import Extractor, register_extractor
from ..model import Facts, RootHint
from ..roots import DATA as ROOTS_DATA, command_targets
from . import cfg

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cfgconf.json")


@lru_cache(maxsize=1)
def rules() -> dict:
    with open(DATA, encoding="utf-8") as fh:
        return json.load(fh)


def _words(line: str, braces: bool = False, comment: str = "#") -> List[str]:
    """The words of a line: blanks separate them, "..." and '...' group (a backslash escapes inside "..." and outside quotes), an
    unquoted `comment` character at the start of a word ends the line; `braces`: an unquoted { or } is a word of its own.
    """
    out: List[str] = []
    cur: List[str] = []
    q, have, i = "", False, 0

    def flush() -> None:
        nonlocal cur, have
        if cur or have:
            out.append("".join(cur))
        cur, have = [], False

    while i < len(line):
        c = line[i]
        if q:
            if c == "\\" and q == '"' and i + 1 < len(line):
                i += 1
                cur.append(line[i])
            elif c == q:
                q = ""
            else:
                cur.append(c)
        elif c in "\"'":
            q, have = c, True
        elif c.isspace():
            flush()
        elif c in comment and not cur and not have:
            break
        elif braces and c in "{}":
            flush()
            out.append(c)
        elif c == "\\" and i + 1 < len(line):
            i += 1
            cur.append(line[i])
        else:
            cur.append(c)
        i += 1
    flush()
    return out


def _fpath(p: str) -> str:
    """A file named in a config as the path of an `include` / `exec` ref: computed parts are `*`, an absolute path loses its root (the
    linker finds it by its tail, as for nginx), '' when it is no path."""
    q = cfg._path(p).lstrip("/")
    q = q[2:] if q.startswith("./") else q
    return q if re.search(r"\w", q.replace("*", "")) else ""


def _file(out: "cfg._Out", raw: str, kind: str, line: int, scope: str = "") -> None:
    p = _fpath(raw)
    if p:
        out.r(p, kind, line, scope)


def _command(out: "cfg._Out", cmd: str, line: int, scope: str = "") -> None:
    """The files a command line runs (graph.roots.command_targets: the script after an interpreter or wrapper, else the first word)."""
    for sel in command_targets(cmd):
        scheme, _, val = sel.partition(":")
        if scheme == "file":
            _file(out, val, "exec", line, scope)


def _soft_root(out: "cfg._Out") -> None:
    out.roots.append(RootHint("init", "", 1, "soft", "convention"))


def _haproxy(text: str, out: "cfg._Out") -> None:
    rd, section = rules()["haproxy"], ""
    sections, token_keys = set(rd["sections"]), set(rd["token_keys"])
    flag, map_call = rd["pattern_flag"], re.compile(rd["map_call"])
    for n, raw in enumerate(text.splitlines(), 1):
        ws = _words(raw)
        if not ws:
            continue
        if ws[0] in sections:
            section = ws[0]
        elif ws[0] in rd["include"] and len(ws) > 1:
            _file(out, ws[1], "include", n)
            continue
        for d in rd["directives"]:
            k = len(d["w"]) + d.get("skip", 0)
            if ws[: len(d["w"])] == d["w"] and len(ws) > k and d.get("in", section) == section:
                if d["kind"] == "exec":
                    _command(out, shlex.join(ws[k:]), n)
                else:
                    _file(out, ws[k], "include", n)
        for i, w in enumerate(ws[:-1]):
            if w in token_keys or (w == flag and i > 0):
                _file(out, ws[i + 1], "include", n)
        for m in map_call.finditer(raw):
            _file(out, m.group(1).strip("\"' "), "include", n)
    _soft_root(out)


def _keepalived(text: str, out: "cfg._Out") -> int:
    rd = rules()["keepalived"]
    defs = {rd["script_block"]: "job", rd["instance_block"]: "resource", rd["group_block"]: "resource"}
    stack: List[Tuple[List[str], str, int]] = []  # (header words, qual of the def or '', start line)
    runs: List[Tuple[str, int]] = []  # the instances and sync groups: the file refs them
    errors = 0

    def close(n: int) -> None:
        _, qual, start = stack.pop()
        if qual:
            out.d(qual, defs[qual.partition(":")[0]], start, n)

    def statement(ws: List[str], n: int) -> None:
        if not ws:
            return
        head = stack[-1][0][:1] if stack else []
        scope = next((q for _, q, _ in reversed(stack) if q), "")
        if ws[0] in rd["include"] and len(ws) > 1:
            _file(out, ws[1], "include", n)
        elif ws[0] in rd["exec_keys"] and len(ws) > 1:
            _command(out, ws[1] if len(ws) == 2 else shlex.join(ws[1:]), n, scope)
        elif head == [rd["track_block"]]:
            out.r(f"vrrp_script:{cfg._q(ws[0])}", "ref", n, scope)
        elif head == [rd["group_members"]]:
            for m in ws:
                out.r(f"vrrp_instance:{cfg._q(m)}", "ref", n, scope)

    ws: List[str] = []
    n = 0
    for n, raw in enumerate(text.splitlines(), 1):
        for w in _words(raw, braces=True, comment="!#"):
            if w == "{":
                kind = ws[0] if ws else ""
                qual = f"{kind}:{cfg._q(ws[1])}" if kind in defs and len(ws) > 1 else ""
                if qual and kind != rd["script_block"]:
                    runs.append((qual, n))
                stack.append((ws, qual, n))
                ws = []
            elif w == "}":
                statement(ws, n)
                ws = []
                if stack:
                    close(n)
                else:
                    errors += 1
            else:
                ws.append(w)
        statement(ws, n)
        ws = []
    errors += len(stack)
    while stack:
        close(n)
    for qual, line in runs:  # keepalived starts every instance and sync group of its file
        out.r(qual, "ref", line, "")
    _soft_root(out)
    return errors


class _Tok(NamedTuple):
    kind: str  # id | str | p | nl
    val: str
    line: int


_ALLOY = re.compile(
    r"(?P<ws>[ \t\r]+)|(?P<cm>//[^\n]*|/\*.*?(?:\*/|\Z))|(?P<nl>\n)|(?P<id>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
    r'|(?P<str>"(?:[^"\\\n]|\\.)*"?|`[^`]*`?)|(?P<num>\d[\w.]*)|(?P<p>.)',
    re.S,
)


def _alloy_tokens(text: str) -> List[_Tok]:
    """Identifiers (dotted names in one token), strings (quotes removed), punctuation and line breaks; comments and blanks go."""
    out, line = [], 1
    for m in _ALLOY.finditer(text):
        kind, val = m.lastgroup or "p", m.group()
        if kind == "str":
            out.append(_Tok("str", val[1:-1] if len(val) > 1 and val[-1] == val[0] else val[1:], line))
        elif kind in ("id", "nl", "p"):
            out.append(_Tok(kind, val, line))
        line += val.count("\n")
    return out


class _Frame(NamedTuple):
    kind: str  # root | declare | component | block
    scope: str  # qual of the declare or component that holds the frame, '' = the file
    decl: str  # name of the declare around, '' = none
    qual: str  # the def of this frame ('' for a plain block)
    line: int


class _Alloy:
    """One pass over the tokens: a stack of block frames, attributes read up to the end of their expression."""

    def __init__(self, text: str, out: "cfg._Out"):
        self.rd, self.out, self.toks = rules()["alloy"], out, _alloy_tokens(text)
        self.stack = [_Frame("root", "", "", "", 1)]
        self.runs: List[Tuple[str, int, str]] = []  # (component qual, line, scope): the runtime starts every component
        self.errors = 0
        self.labels = {  # import.file "lib": a block type `lib.name` is an instance of the declare `name` of that file
            self.toks[i + 1].val
            for i, t in enumerate(self.toks[:-1])
            if t.kind == "id" and t.val in self.rd["import_blocks"] and self.toks[i + 1].kind == "str"
        }

    def is_p(self, j: int, chars: str) -> bool:
        return j < len(self.toks) and self.toks[j].kind == "p" and self.toks[j].val in chars

    def run(self) -> int:
        i, n = 0, len(self.toks)
        while i < n:
            t = self.toks[i]
            if self.is_p(i, "}"):
                self.close(t.line)
                i += 1
            elif t.kind != "id":
                i += 1
            else:
                label = self.toks[i + 1].val if i + 1 < n and self.toks[i + 1].kind == "str" else None
                j = i + 1 + (label is not None)
                if self.is_p(j, "{"):
                    self.open(t, label)
                    i = j + 1
                elif label is None and self.is_p(j, "="):
                    i = self.attribute(t.val, j + 1)
                else:
                    i += 1
        self.errors += len(self.stack) - 1
        while len(self.stack) > 1:
            self.close(self.toks[-1].line if self.toks else 1)
        for qual, line, scope in self.runs:
            self.out.r(qual, "ref", line, scope)
        return self.errors

    def close(self, line: int) -> None:
        if len(self.stack) == 1:
            self.errors += 1
            return
        f = self.stack.pop()
        if f.qual:
            self.out.d(f.qual, "resource", f.line, line)

    def open(self, t: _Tok, label: Optional[str]) -> None:
        """A block `type "label" {`: a declare, an import, or a component (or the instance of a declare); the frame it opens."""
        top, typ, qual, kind, decl = self.stack[-1], t.val, "", "block", self.stack[-1].decl
        if typ == "declare" and label:
            qual, kind, decl = f"declare:{cfg._q(label)}", "declare", label
        elif (
            top.kind in ("root", "declare")
            and label is not None
            and typ not in self.rd["plain_blocks"] + list(self.rd["import_blocks"])
        ):
            qual, kind = f"component:{cfg._q((decl + ':' if decl else '') + typ + '.' + label)}", "component"
            self.runs.append((qual, t.line, top.scope))
            head, _, rest = typ.partition(".")
            if not rest:  # `name "x" {}`: an instance of the declare `name`
                self.out.r(f"declare:{cfg._q(typ)}", "ref", t.line, top.scope)
            elif head in self.labels:  # `lib.name "x" {}`: of the declare `name` of the imported file
                self.out.r(f"declare:{cfg._q(rest)}", "ref", t.line, top.scope)
        self.stack.append(_Frame(kind, qual or top.scope, decl, qual, t.line))

    def attribute(self, key: str, j: int) -> int:
        """`key = expression` from toks[j]: a file (a string value of a path key) and the component names the expression mentions."""
        depth, k, n = 0, j, len(self.toks)
        while k < n:
            if self.is_p(k, "([{"):
                depth += 1
            elif self.is_p(k, ")]}"):
                if depth == 0:
                    break
                depth -= 1
            elif self.toks[k].kind == "nl" and depth == 0:
                break
            k += 1
        expr, frame = self.toks[j:k], self.stack[-1]
        if key in self.rd["path_keys"] and len(expr) == 1 and expr[0].kind == "str":
            _file(self.out, expr[0].val, "include", expr[0].line, frame.scope)
        for t in expr:
            segs = t.val.split(".") if t.kind == "id" else []
            for m in range(2, min(len(segs), self.rd["max_chain"]) + 1):
                name = (frame.decl + ":" if frame.decl else "") + ".".join(segs[:m])
                self.out.r(f"component:{cfg._q(name)}", "ref", t.line, frame.scope)
        return k


def _alloy(text: str, out: "cfg._Out") -> int:
    errors = _Alloy(text, out).run()
    _soft_root(out)
    return errors


def _redis(text: str, out: "cfg._Out") -> None:
    rd = rules()["redis"]
    seen: Dict[str, List[str]] = {}
    pending: List[Tuple[str, int, str]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        if raw.lstrip().startswith("#"):
            continue
        ws = _words(raw, comment="")
        if not ws:
            continue
        if ws[0].lower() == rd["dir_key"] and len(ws) > 1:
            seen["dir"] = [ws[1]]
        for d in rd["directives"]:
            head = d["w"].split()
            if [x.lower() for x in ws[: len(head)]] == head and len(ws) > len(head) + d["arg"]:
                arg = ws[len(head) + d["arg"]]
                if d["kind"] == "exec":
                    _command(out, arg, n)
                else:
                    _file(out, arg, "include", n)
                    if head[0] in rd["dir_join"] and not arg.startswith("/"):
                        pending.append((arg, n, "include"))
    for arg, n, kind in pending:  # a relative name is relative to `dir` (wherever it is written in the file)
        if "dir" in seen:
            _file(out, posixpath.join(seen["dir"][0], arg), kind, n)
    _soft_root(out)


class CfgConf(Extractor):
    name, version, langs = "cfgconf", 1, ("haproxy", "keepalived", "alloy", "redis", "logql")

    def grammar_sha(self, lang: str) -> str:
        """sha256 of what decides the facts: the rules, the command parser's data (roots), this module and cfg.py (its helpers)."""
        h = hashlib.sha256()
        for path in (DATA, ROOTS_DATA, os.path.abspath(__file__), cfg.__file__):
            with open(path, "rb") as fh:
                h.update(fh.read())
        return h.hexdigest()

    def extract(self, lang: str, path: str, text: str) -> Facts:
        text = text[1:] if text[:1] == "﻿" else text
        out, errors = cfg._Out(), 0
        if lang == "haproxy":
            _haproxy(text, out)
        elif lang == "keepalived":
            errors = _keepalived(text, out)
        elif lang == "alloy":
            errors = _alloy(text, out)
        elif lang == "redis":
            _redis(text, out)
        else:
            _soft_root(out)
        return out.facts(lang, text, errors)


register_extractor(CfgConf())
