"""graph.roots: the entry-point roots of the graph: what is run or loaded by something that is not code in the tree. Data: data/frameworks.json.

Two layers, both deterministic and standard library only:

1. Root extractors (graph.contracts.RootExtractor, cached as virtual shards "root:<name>" by the blob sha): they read ONE file's
   text and return RootHint(kind, target, line, strength, trust, stub) without touching the tree or executing anything.
       systemd    ExecStart* of *.service, Unit= of *.timer                                                         -> systemd
       cron       crontab / cron.d lines; task-xml: Task Scheduler XML                                                        -> cron
       npm        package.json main/module/browser/exports/types/source (kind export; a built entry adds its source candidates, data
                  js_build, with the build flavours of the file name (`index.esm.js`, `x.umd.min.js`) and a bundle's `src/index`; a path
                  without an extension or a directory its Node spellings; a pattern `./s/*.js` is every file it matches), bin, files
                  run by scripts (`node server`, `tsup src/index.ts`), the entry conventions of a framework named
                  in the dependencies (data js_frameworks: next pages/app, react-scripts, nest, gatsby, expo ...) -> export | bin
       html       local <script src> of any html page: a module script (Vite, Parcel) and what an inline module script imports
                  -> export; a classic script, whose top-level definitions are globals the page calls by name -> script
       manifest   the scripts of a WebExtension manifest.json (service worker, background, content scripts), built ones mapped
                  to their sources as npm does                                                                      -> export
       jstool     tool configs (*.config.js, .eslintrc.js, gulpfile.js, .storybook/main.ts) -> main, the entries they name
                  (webpack entry, rollup input, tsup / esbuild entry) -> export; ambient *.d.ts                         -> export
       sfc        local imports of the script of a .vue / .svelte / .astro component (no extractor reads those)         -> export
       gitlab     `.gitlab-ci.yml` (the pipeline; its jobs and includes follow in the graph)                          -> ci-job
       action     `action.yml` of a GitHub action (hard at the repository root, soft below it: `uses: org/repo@v1`)   -> export
       docs       fenced shell blocks and inline code of *.md (the recipes of `make x` / `just x` too: build:<qual>) -> doc-cmd (soft)
       classreg   classes named by META-INF/services, module-info provides, spring.factories, web.xml (graph.classreg)  -> classreg
       rootsrc    ordinary projects (graph.rootsrc, data/rootsrc.json): pyproject / setup.cfg / setup.py entry points, Cargo targets,
                  csproj, Gradle / Maven main classes, the commands of a Dockerfile, Makefile, justfile and compose file -> bin | export | main |
                  ci-job | test | init. A source that cannot be read is the hint BAD, resolve() makes it W dc.bad-root-source.
                  An entry `mod:pkg.cli#main` roots the function as well as the module.
   The npm, html, manifest, jstool and sfc extractors carry the sha of the frameworks.json keys they read in their ext_ver: an edited table
   is a new shard key, never served from shards of the old data.
   A hint target is "file:<path>" (as written, a home variable becomes ~), "cmd:<name>" (a bare command),
   "mod:<pkg.mod>" (python -m), "class:<name>[#<method>]" (a JVM class, kept as a selector root: graph.classreg.bind finds its
   node), "build:<qual>" (a Makefile / justfile recipe or Dockerfile stage that a doc or a Dockerfile command names, `make test`: the
   same, bound to the nodes of that qual, none = no root and no finding), "sibling:<rel>" (next to the config; "" = the
   same-stem .service of a timer), "self:" (the config file itself, the same text for every path). Strength: hard (a path that runs),
   soft (concatenated literal, doc, bin, test).
2. resolve(hints, files, read) turns hints into model.Root against the tree and returns the findings:
   E dc.root-dangling (a registration names a file that is not there). A path outside the repository (an installed binary, a venv, an
   absolute path no path_map covers) is silent: only the kinds and path classes in frameworks.json "dangling" can dangle. Roots found
   by the tree alone: bin/ executables (soft, convention: a bin nothing else references is still unreferenced) and test files (soft,
   convention: verdict test-only). A tracked file with the exec bit and a shebang line outside the test paths is an entry point run by
   hand: a hard bin root on the file itself (graph.rootsrc exec_entry, data/rootsrc.json), so the file and what its top-level code calls
   are alive. A file whose extension is a script_ext of its language (langs.json: Kotlin .kts, Gradle build scripts) is a hard main root
   (convention): its tool runs it. A SQL file that its tool runs by itself (data frameworks.json schema_run_files: db/migration, Flyway
   V1__x.sql, Liquibase, Rails migrate, Spring schema.sql / data.sql, docker-entrypoint-initdb.d) is a soft init root on the file
   (convention): the objects it alters or fills are used (graph.extract.schema: the statements of the file that name a table are refs
   from the file). Config files under tests/fixtures/... register nothing. A resolved target is the file node
   node_key(lang, path), the lang from graph.langs ("file" when unknown); link.py creates that node for every file.

    hints = collect(root, tree, store=None, workers=None)     {config path: [RootHint]}, parallel, cached by blob
    rs = resolve(hints, {path: mode}, read)  /  build(root)   RootSet(roots, findings), both sorted
    command_targets("exec /usr/bin/python3 -I ~/bin/h.py 2>/dev/null") -> ["file:~/bin/h.py"]
    build_targets("make -C d test $(SUBS)") -> ["recipe:test", "recipe:*"]   (a glob is a computed word: the caller's dynamic site)
"""

from __future__ import annotations

import bisect
import fnmatch
import hashlib
import html
import json
import os
import posixpath
import re
from functools import lru_cache
from typing import Callable, Dict, Iterable, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from . import langs as _langs, par, rootsrc as _rs, vcs
from .classreg import BUILD_SCHEME, SCHEME as CLASS_SCHEME, ClassRegistryRoots
from .contracts import RootExtractor, make_root_shard, register_roots, root_extractors
from .model import TRUSTS, Finding, Root, RootHint, node_key, qual_name, root as mk_root, shard_key, sort_findings

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "frameworks.json")
CHUNK_BYTES = 200_000


@lru_cache(maxsize=1)
def fw() -> dict:
    with open(DATA, encoding="utf-8") as fh:
        d = json.load(fh)
    for k in (
        "builtins",
        "wrappers",
        "shells",
        "script_ext",
        "wrapper_value_flags",
        "bin_dirs",
        "bin_win_ext",
        "test_dirs",
    ):
        d[k] = frozenset(d[k])  # membership tests only
    return d


@lru_cache(maxsize=None)
def _rx(key: str) -> "re.Pattern":
    v = fw()[key]
    return re.compile("|".join(f"(?:{p})" for p in v) if isinstance(v, list) else v)


def _is_fixture(path: str) -> bool:
    return any(p in fw()["test_dirs"] for p in path.split("/")[:-1])


def is_test_path(path: str) -> bool:
    return _is_fixture(path) or any(re.search(p, path.rsplit("/", 1)[-1]) for p in fw()["test_names"])


# ---------------------------------------------------------------- command lines
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIR = re.compile(r"^\d*[<>]+&?$")
_NUM = re.compile(r"^\d+(\.\d+)?[smhd]?$")
_WINSLASH = re.compile(r"(?<=[\w.%}~:-])\\(?=[\w.%${~-])")  # C:\Users\x\h.py, .\bin\h.py, %VAR%\x
_HOME = re.compile(r"\$\{?(HOME|USERPROFILE)\}?|%USERPROFILE%")
_OPAQUE = re.compile(r"[$%{@]")  # an unexpanded variable or template placeholder: the path is a suffix only
_ABS = re.compile(r"^(/|~|[A-Za-z]:/)")


_TOK = re.compile(r"""\s*(?:(;|&&?|\|\|?|[()]|\d*[<>]+&?)|((?:[^\s;&|()<>'"\\]|\\.|'[^']*'|"(?:[^"\\]|\\.)*")+))""")
_UNQ = re.compile(r"""'([^']*)'|"((?:[^"\\]|\\.)*)"|\\(.)""")


def _words(cmd: str) -> List[str]:
    """Shell words and operators (; && || | & ( ) and redirections like 2>) of a command line; quotes and escapes are removed.
    An unterminated quote falls back to whitespace splitting."""
    cmd, out, i = _WINSLASH.sub("/", cmd), [], 0
    while i < len(cmd) and cmd[i:].strip():
        m = _TOK.match(cmd, i)
        if not m:
            return cmd.split()
        i = m.end()
        out.append(
            m.group(1)
            or _UNQ.sub(
                lambda q: (
                    q.group(1) if q.group(1) is not None else (q.group(2) if q.group(2) is not None else q.group(3))
                ),
                m.group(2),
            )
        )
    return out


def _norm(tok: str) -> str:
    """Path as the registration names it: a home variable becomes `~`, backslashes become slashes."""
    t = _HOME.sub("~", tok.replace("\\", "/"))
    n = posixpath.normpath(t) if t else "."
    return n.lstrip("/") if t.startswith("/") and not tok.startswith("/") else n


def _base(tok: str) -> str:
    return re.sub(r"(\.exe)?$", "", posixpath.basename(tok.replace("\\", "/")).lower())


def _interp(tok: str) -> Optional[dict]:
    return fw()["interpreters"].get(re.sub(r"[\d.]+$", "", _base(tok)))


def _target(tok: str) -> str:
    if not tok or tok.startswith("-") or _ENV.match(tok):
        return ""
    p = _norm(tok)
    if "/" in p or os.path.splitext(p)[1] in fw()["script_ext"]:
        return "file:" + p
    return "cmd:" + tok if re.fullmatch(r"[\w.+-]+", tok) and tok not in fw()["builtins"] else ""


def _strip_redirects(w: Sequence[str]) -> List[str]:
    out, skip = [], False
    for a in w:
        if skip:
            skip = False
        elif _REDIR.match(a):
            skip = True
        else:
            out.append(a)
    return out


def _skip_prefix(w: Sequence[str]) -> int:
    i = 0
    while i < len(w):
        if _ENV.match(w[i]) or w[i] in ("{", "}", "!", "then", "do", "else"):
            i += 1
        elif posixpath.basename(w[i]) in fw()["wrappers"]:
            i += 1
            while i < len(w) and (w[i].startswith("-") or _ENV.match(w[i]) or _NUM.match(w[i])):
                i += 2 if w[i] in fw()["wrapper_value_flags"] else 1
        elif i + 1 < len(w) and w[i + 1] in fw()["launchers"].get(posixpath.basename(w[i]), ()):
            i += 2  # `poetry run python x.py`: the command follows the subcommand
        else:
            break
    return i


def _script_of(spec: dict, rest: Sequence[str], bare: bool = False) -> Tuple[str, Sequence[str]]:
    """(target, the arguments after it) of an interpreter's arguments; ("", []) for inline code. `bare`: an interpreter of the data
    flag bare_script (node: it resolves server -> server.js) runs a file even when the word has no extension or starts with ./
    """
    j = 0
    while j < len(rest):
        a = rest[j]
        if a in spec.get("code_flags", ()):
            return "", []
        if a == spec.get("module_flag") and j + 1 < len(rest):
            return "mod:" + rest[j + 1], rest[j + 2 :]
        if a in spec.get("file_flags", ()) and j + 1 < len(rest):
            return _target(rest[j + 1]), rest[j + 2 :]
        if a == "--":
            return "", rest[j:]
        j += 2 if a in spec.get("value_flags", ()) else 1
        if a in spec.get("subcommands", ()):
            continue  # `deno run x.ts`
        if not a.startswith("-"):
            t = _target(a) or ""
            if bare and spec.get("bare_script") and not t.startswith("file:") and not _OPAQUE.search(a):
                t = "file:" + _norm(a)
            return t, rest[j:]
    return "", []


def _segment(w: Sequence[str], depth: int, out: List[str], bare: bool = False) -> None:
    w = _strip_redirects(w)
    i = _skip_prefix(w)
    if i >= len(w):
        return
    head, rest = w[i], list(w[i + 1 :])
    base = _base(head)
    tail: Sequence[str] = []
    if base in fw()["shells"]:
        for j, a in enumerate(rest):
            if re.fullmatch(r"-[a-z]*c[a-z]*", a) and j + 1 < len(rest):
                if depth < 3:
                    _cmdline(rest[j + 1], depth + 1, out, bare)
                return
            if not a.startswith("-"):
                out.append(_target(a))
                tail = rest[j + 1 :]
                break
    elif _interp(head) is not None:
        t, tail = _script_of(_interp(head), rest, bare)
        out.append(t)
    elif base in ("source", ".") and rest:
        out.append(_target(rest[0]))
    else:
        out.append(_target(head))
    if "--" in tail and depth < 3:  # `wrapper run -- python3 -I bin/x`: the command after the separator
        _segment(tail[list(tail).index("--") + 1 :], depth + 1, out, bare)


def _segments(cmd: str) -> Iterator[List[str]]:
    """The simple commands of a command line (the words between ; && || | & and parentheses)."""
    seg: List[str] = []
    for a in _words(cmd) + [";"]:
        if a and set(a) <= set(";&|()"):
            if seg:
                yield seg
            seg = []
        else:
            seg.append(a)


def _cmdline(cmd: str, depth: int = 0, out: Optional[List[str]] = None, bare: bool = False) -> List[str]:
    out = [] if out is None else out
    for seg in _segments(cmd):
        _segment(seg, depth, out, bare)
    return out


_HOLE = re.compile(
    r"\$?\{\{.*?\}\}|\$\((?:[^()]|\([^()]*\))*\)|\$\{[^}]*\}|\$[@<^*%?+|]|\$\$?\w+|`[^`]*`|\$?_TPL_"
)  # a computed part: $(x) ${x} $x $@ `x` {{ x }} ${{ x }} (a Helm / just / Actions placeholder)
_NAME = re.compile(r"[\w.@%+*-][\w.@/%+*-]*")
_CONT = re.compile(r"\\\n")


@lru_cache(maxsize=1)
def _build_data() -> Tuple[dict, "re.Pattern", "re.Pattern", "re.Pattern"]:
    bt = fw()["build_tools"]
    names = [*bt["tools"], *bt["stage_tools"], *bt["aliases"]]
    word = re.compile(
        "(?<![\\w-])(?:" + "|".join(re.escape(a) for a in sorted(names, key=len, reverse=True)) + ")(?!\\w)"
    )
    alias = re.compile("|".join(re.escape(a) for a in sorted(bt["aliases"], key=len, reverse=True)))
    return bt, word, alias, re.compile(bt["option_vars"])


def build_targets(cmd: str, depth: int = 0) -> List[str]:
    """The Makefile / justfile recipes and Dockerfile stages a command line names, as quals of the config languages: `make test lint`,
    `$(MAKE) -C sub all`, `just build` give `recipe:test`, `recipe:lint` ...; `docker build --target dev` gives `stage:dev` (data
    build_tools; the command parser of command_targets: wrappers, `cd x &&`, `sh -c '...'`). The words that are options, their values,
    assignments (`CC=gcc`) or the option variables (`$(MAKEFLAGS)`) are no recipes. A word with a computed part (`$(SUBS)`,
    `{{ target }}`, `$@`, `a-$(X)`) is a glob, its holes `*` (`recipe:*`, `recipe:a-*`): the caller makes a dynamic site of it. A word
    that is no recipe of any file links to nothing."""
    bt, word, alias, option_var = _build_data()
    if not word.search(cmd):  # no build tool is named: nothing to parse (a shell script has thousands of lines)
        return []
    text = alias.sub(lambda m: bt["aliases"][m.group()], _CONT.sub(" ", cmd))
    text = _HOLE.sub(lambda m: "" if option_var.fullmatch(re.sub(r"\W", "", m.group())) else "*", text)
    out: List[str] = []
    segments = [seg for line in text.split("\n") for seg in _segments(line)]  # a script has a command line per line
    for seg in segments:
        w = _strip_redirects(seg)
        w = [w[0].lstrip("@+-"), *w[1:]] if w else w  # a just / make recipe line: `@make x`, `-make x`
        i = _skip_prefix(w)
        if i >= len(w):
            continue
        head, rest = _base(w[i]), list(w[i + 1 :])
        if head in bt["tools"]:
            out += _recipes(bt["profiles"][bt["tools"][head]], rest)
        elif head in bt["stage_tools"]:
            out += [f"stage:{n}" for n in _option_values(bt["stage_flag"], rest, str.lower)]
        elif head in fw()["shells"] and depth < 3:  # `bash -c "make test"`
            for j, a in enumerate(rest[:-1]):
                if re.fullmatch(r"-[a-z]*c[a-z]*", a):
                    out += build_targets(rest[j + 1], depth + 1)
                    break
    return list(dict.fromkeys(out))


def _glob(name: str) -> str:
    return "*".join(qual_name(x) for x in name.split("*"))


def _recipes(prof: dict, args: Sequence[str]) -> List[str]:
    """The recipe words of the arguments of make / just (the words that are no option, option value or assignment)."""
    out, skip = [], False
    for k, a in enumerate(args):
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = "=" not in a and (
                a in prof["option_args"]
                or (a in prof["option_numeric"] and k + 1 < len(args) and args[k + 1].isdigit())
            )
        elif "=" not in a and _NAME.fullmatch(a):
            out.append(f"{prof['tag']}:{_glob(a)}")
    return out


def _option_values(flag: str, args: Sequence[str], norm=str) -> List[str]:
    """The values of `flag` in `args`: `--target dev` and `--target=dev` (a value that is no name is dropped)."""
    out = []
    for k, a in enumerate(args):
        v = a[len(flag) + 1 :] if a.startswith(flag + "=") else args[k + 1] if a == flag and k + 1 < len(args) else ""
        if v and _NAME.fullmatch(v):
            out.append(_glob(norm(v)))
    return out


def command_targets(cmd: str, bare: bool = False) -> List[str]:
    """Selectors of the files a command line runs, in order, deduplicated: see the module docstring for the forms. `bare`: the
    script of an interpreter with the data flag bare_script is a file even as a bare word (`node server` is `file:server`, not
    `cmd:server`; `node ./server` is `file:server`, not nothing): npm scripts, where the working directory is the package.
    """
    return list(dict.fromkeys(t for t in _cmdline(cmd, 0, None, bare) if t))


def mod_files(mod: str) -> List[str]:
    """The files a `mod:` selector (python -m pkg.main) can name: pkg/main.py, pkg/main/__main__.py, pkg/main/__init__.py."""
    base = mod.replace(".", "/")
    return [base + ".py", base + "/__main__.py", base + "/__init__.py"]


# ---------------------------------------------------------------- text helpers
class _Lines:
    """Line of a string in the source text, searching forward so that equal strings map to successive lines."""

    def __init__(self, text: str):
        self.t, self.at, self.last = text, 0, 1
        self.nl = [m.start() for m in re.finditer("\n", text)]

    def of(self, s: str) -> int:
        lit = json.dumps(s, ensure_ascii=False)[1:-1]
        i = self.t.find(lit, self.at)
        if i < 0:
            i = self.t.find(lit)
        if i >= 0:
            self.at = i + 1
            self.last = bisect.bisect_left(self.nl, i) + 1
        return self.last


def _json(text: str):
    try:
        return json.loads(text.lstrip("\ufeff"))
    except ValueError:
        return None


def _dedup(hints: Iterable[RootHint]) -> List[RootHint]:
    """One hint per (kind, target, trust, stub): the hard one wins over the soft one, then the earliest line."""
    seen: Dict[Tuple, RootHint] = {}
    for h in hints:
        k = (h.kind, h.target, h.trust, h.stub)
        if k not in seen or (h.strength, h.line) < (seen[k].strength, seen[k].line):  # "hard" < "soft"
            seen[k] = h
    return sorted(seen.values())


def _hints(kind: str, targets: Iterable[str], line: int, strength="hard", trust="registered") -> List[RootHint]:
    return [RootHint(kind, t, line, strength, trust) for t in targets if t]


# ---------------------------------------------------------------- extractors
class SystemdRoots(RootExtractor):
    name, version = "systemd", 1

    def matches(self, path: str) -> bool:
        return bool(_rx("systemd_files").search(path))

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        out, keys, timer = [], set(fw()["systemd_exec_keys"]), False
        lines = re.sub(r"\\\n", " ", text).split("\n")
        unit = False
        for n, raw in enumerate(lines, 1):
            s = raw.strip()
            if s == "[Timer]":
                timer = True
            k, _, v = s.partition("=")
            if k in keys:
                out.extend(_hints("systemd", command_targets(v.lstrip("-@:+!").strip()), n))
            elif k == "Unit" and v.strip():
                unit = True
                out.append(RootHint("systemd", "sibling:" + v.strip(), n))
        if timer and not unit:
            out.append(RootHint("systemd", "sibling:", 1))
        return _dedup(out)


_CRON_FIELD = r"(?:[\d*/,?-]+|[A-Za-z]{3}(?:[-,][A-Za-z]{3})*)"
_CRON_TIME = re.compile(rf"^(@\w+|({_CRON_FIELD}\s+){{5}})")


class CronRoots(RootExtractor):
    name, version = "cron", 1

    def matches(self, path: str) -> bool:
        return bool(_rx("cron_files").search(path))

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        out = []
        for n, raw in enumerate(text.split("\n"), 1):
            s = raw.strip()
            m = None if s.startswith("#") else _CRON_TIME.match(s)
            if m and not _ENV.match(s):
                rest = s[m.end() :].strip()
                tgt = command_targets(rest)  # a cron.d line has a user before the command: read both ways
                if " " in rest:
                    tgt += command_targets(rest.split(None, 1)[1])
                out.extend(_hints("cron", dict.fromkeys(tgt), n))
        return _dedup(out)


class TaskXmlRoots(RootExtractor):
    """Windows Task Scheduler XML: <Exec><Command>..</Command><Arguments>..</Arguments></Exec>."""

    name, version = "task-xml", 1

    def matches(self, path: str) -> bool:
        return bool(_rx("task_xml_files").search(path))

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        out = []
        for m in re.finditer(r"<Exec>(.*?)</Exec>", text, re.S) if "<Exec>" in text else ():
            cmd = re.search(r"<Command>(.*?)</Command>", m.group(1), re.S)
            args = re.search(r"<Arguments>(.*?)</Arguments>", m.group(1), re.S)
            if cmd:
                full = (
                    html.unescape(cmd.group(1)).strip().strip('"') + " " + html.unescape(args.group(1) if args else "")
                )
                out.extend(_hints("cron", command_targets(full), text.count("\n", 0, m.start()) + 1))
        return _dedup(out)


def _js_strings(text: str) -> List[Tuple[int, str, bool]]:
    """(line, value, soft) of the string literals of JS source; comments skipped, `${..}` becomes NUL, soft = the literal is
    part of a template with substitutions or sits next to a '+' (a concatenation: only a fragment of the path)."""
    out, i, n, line = [], 0, len(text), 1
    while i < n:
        c = text[i]
        if c == "\n":
            line, i = line + 1, i + 1
        elif text.startswith("//", i):
            i = text.find("\n", i) if "\n" in text[i:] else n
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line, i = line + text.count("\n", i, j), j
        elif c in "'\"`":
            j, buf, dyn, start = i + 1, [], False, line
            while j < n and text[j] != c:
                d = text[j]
                if d == "\\":
                    buf.append(text[j + 1 : j + 2])
                    j += 2
                    continue
                if d == "\n":
                    if c != "`":
                        break
                    line += 1
                if c == "`" and text.startswith("${", j):
                    depth, j, dyn = 1, j + 2, True
                    while j < n and depth:
                        depth += {"{": 1, "}": -1}.get(text[j], 0)
                        line += text[j] == "\n"
                        j += 1
                    buf.append("\0")
                    continue
                buf.append(d)
                j += 1
            before, after = text[:i].rstrip()[-1:], text[j + 1 :].lstrip()[:1]
            out.append((start, "".join(buf), dyn or before == "+" or after == "+"))
            i = j + 1
        else:
            i += 1
    return out


def _npm_paths(v) -> Iterable[str]:
    """Strings of a package.json entry value: a path, a {condition: path} map, a list; a pattern (`./s/*.js`) names every file it
    matches (resolve expands it against the tree)."""
    if isinstance(v, str):
        if v.startswith("."):
            yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _npm_paths(x)
    elif isinstance(v, list):
        for x in v:
            yield from _npm_paths(x)


_BUILT_EXT = re.compile(r"(\.d)?\.[cm]?[jt]sx?$")
_CMD_DATA = (
    "interpreters",
    "builtins",
    "wrappers",
    "shells",
    "script_ext",
    "wrapper_value_flags",
    "build_tools",
)  # what command_targets and build_targets read


def _data_sha(keys: Sequence[str]) -> str:
    """Short sha of the frameworks.json keys an extractor reads."""
    d = {k: fw()[k] for k in keys}
    return hashlib.sha256(json.dumps(d, sort_keys=True, default=sorted).encode()).hexdigest()[:8]


class _Keyed(RootExtractor):
    """A root extractor whose hints depend on frameworks.json: the sha of `data_keys` is part of its cache key (ext_ver), so an edit
    of that data is never served from shards of the old data."""

    data_keys: Tuple[str, ...] = ()

    @property
    def ext_ver(self) -> str:
        return f"{self.name}@{self.version}.{_data_sha(self.data_keys)}"


def _stems(path: str) -> Tuple[List[str], Sequence[str]]:
    """(stems, extensions) a package path (no leading ./) may mean, Node resolution with the data js_build: a spelled source file is its
    stem with the in-place source extensions (a build next to its source); an extensionless path is itself and its directory's
    `index`, with every source extension; a file of another kind (`x.json`, `s/*.css`) names no source."""
    b = fw()["js_build"]
    path = posixpath.normpath(path)
    if path == ".":  # the package directory itself: "main": "." or "./"
        return [b["index"]], b["src_exts"]
    if _BUILT_EXT.search(path):
        return [_BUILT_EXT.sub("", path)], b["in_place_exts"]
    if "." in path.rsplit("/", 1)[-1]:
        return [], ()
    return [path, f"{path}/{b['index']}"], b["src_exts"]


def _spell(path: str) -> List[str]:
    """The files `path` (relative, no leading ./) may name: see _stems. A file that exists is found by the exact hint of the caller."""
    stems, exts = _stems(path)
    return [s + e for s in stems for e in exts if s + e != posixpath.normpath(path)]


def _plain(rest: str) -> str:
    """`index.esm`, `mylib.umd.min` -> `index`, `mylib`: the build flavours (data js_build flavours) are no part of the source name."""
    head, _, name = rest.rpartition("/")
    fl = fw()["js_build"]["flavours"]
    while "." in name and name.rpartition(".")[0] and "." + name.rpartition(".")[2] in fl:
        name = name.rpartition(".")[0]
    return f"{head}/{name}" if head else name


def _npm_sources(built: str) -> List[str]:
    """Candidate sources of a built entry (`./dist/index.js`, `./dist/esm/index.d.ts`, `./lib/index.js`, `./dist/index.esm.js`, `./index`,
    `./lib/`): its own spellings (_spell) and `<src_dir>/<rest>` in every source extension, where rest is the path without the build
    directory, the build flavours after it (dist/esm, dist/cjs), a `src/` of a tsc rootDir "." and the extension, with and without the
    flavours of the file name (`.esm`, `.umd.min`: rollup, microbundle, tsdx), and for a file at the top of a build directory also the
    `<src_dir>/index` a bundle is made from (`./dist/bundle.js`). A candidate that is not in the tree is silent (kind export does not
    dangle). Data: frameworks.json js_build."""
    b = fw()["js_build"]
    stems, _ = _stems(built)
    out = [f"./{x}" for x in _spell(built)]
    for stem in stems:
        segs = stem.split("/")
        inside = len(segs) > 1 and segs[0] in b["out_dirs"]
        if inside:
            segs = segs[1:]
            while len(segs) > 1 and segs[0] in b["variant_dirs"]:
                segs = segs[1:]
            if len(segs) > 1 and segs[0] in b["src_dirs"]:  # tsc with rootDir ".": dist/src/index.js is src/index.ts
                segs = segs[1:]
        rest = "/".join(segs)
        if inside or _plain(rest) != rest:
            # a file at the top of the build directory may be one bundle of the whole package: its source is src/index
            bundle = inside and "/" not in rest and "*" not in rest
            names = dict.fromkeys((rest, _plain(rest), *((b["index"],) if bundle else ())))
            out += [f"./{d}/{n}{e}" for d in b["src_dirs"] for n in names for e in b["src_exts"]]
    return [c for c in dict.fromkeys(out) if c != built]


def _script_files(cmd: str) -> List[str]:
    """Project-relative paths an npm script runs: the script of an interpreter (`node server`, `tsx src/cli.ts`) and every word that
    is a source file (`tsup src/index.ts`, `rollup -i src/a.js`, `esbuild --entry=./x.ts`)."""
    out = [
        t[5:]
        for t in command_targets(cmd, bare=True)
        if t.startswith("file:") and not t.startswith(("file:/", "file:~"))
    ]
    exts = tuple(fw()["js_build"]["src_exts"])
    for w in _words(cmd):
        w = w.split("=", 1)[1] if w.startswith("--") and "=" in w else w
        if w.endswith(exts) and not w.startswith(("-", "/", "~", "$")) and "://" not in w:
            out.append(w)
    return list(dict.fromkeys(posixpath.normpath(x) for x in out))


class NpmRoots(_Keyed):
    """The entry points of a package: files the registry (npm, a bundler, node itself) loads, not code that calls them. A built
    file (dist/) that is not in the tree is silent: kinds export and bin are not in frameworks.json "dangling"."""

    name, version = "npm", 4
    data_keys = ("npm_files", "npm_entry_keys", "npm_dep_keys", "js_frameworks", "js_build", *_CMD_DATA)

    def matches(self, path: str) -> bool:
        return bool(_rx("npm_files").search(path))

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        doc, pos, out = _json(text), _Lines(text), []
        if not isinstance(doc, dict):
            return [_rs.bad("package.json is not valid JSON or not an object")]

        def bare(v):  # "main": "lib/x.js" is the path ./lib/x.js
            return "./" + v if isinstance(v, str) and not v.startswith(".") else v

        bin_ = doc.get("bin")
        entries = [(k, "export", doc.get(k) if k == "exports" else bare(doc.get(k))) for k in fw()["npm_entry_keys"]]
        entries.append(("bin", "bin", {k: bare(v) for k, v in bin_.items()} if isinstance(bin_, dict) else bare(bin_)))
        for _, kind, v in entries:
            for t in _npm_paths(v):
                at = pos.of(t if t in text else t[2:])
                out.append(RootHint(kind, "sibling:" + t, at))
                out += [RootHint(kind, "sibling:" + src, at) for src in _npm_sources(t)]
        for key in fw()["npm_dep_keys"]:  # a framework that loads files by convention: pages, routes, the main module
            deps = doc.get(key)
            for dep in sorted(d for d in deps if d in fw()["js_frameworks"]) if isinstance(deps, dict) else ():
                at = pos.of(dep)
                out += [
                    RootHint("export", "sibling:" + g, at, "hard", "convention") for g in fw()["js_frameworks"][dep]
                ]
        scripts = doc.get("scripts")  # `node build/x.js`, `node server`, `tsx src/cli.ts`: the files an npm script runs
        for name, cmd in sorted(scripts.items()) if isinstance(scripts, dict) else ():
            if isinstance(cmd, str):
                for t in _script_files(cmd):
                    at = pos.of(cmd)
                    out.append(RootHint("export", f"sibling:./{t}", at, "soft"))
                    out += [RootHint("export", "sibling:" + src, at, "soft") for src in _npm_sources(f"./{t}")]
        return _dedup(out)


def _value_end(text: str, i: int) -> int:
    """End of the JS value that starts at `i` (a string, a call, an array, an object): the first `,` `;` or newline outside any
    bracket and string, or the bracket that closes the enclosing one. At most 2000 characters."""
    depth, n = 0, min(len(text), i + 2000)
    while i < n:
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        depth += (c in "([{") - (c in ")]}")
        if depth < 0 or (depth == 0 and c in ",;\n"):
            return i
        i += 1
    return n


def _entry_paths(text: str) -> List[Tuple[int, str, bool]]:
    """(line, path, soft) of the files a bundler or build tool config names as entries: the string literals of the value of an entry
    key (data js_tool_entry_keys): `entry: "./src/index.js"`, `input: ["a.js", "b.js"]`, `entry: { main: "./src/main.js" }`,
    `entryPoints: [path.resolve(__dirname, "src/x.ts")]`. A literal counts when it is a path (a `/` or a source extension) and
    is no URL, package or absolute path; soft = part of a concatenation."""
    keys = "|".join(re.escape(k) for k in fw()["js_tool_entry_keys"])
    exts = tuple(fw()["js_build"]["src_exts"])
    out = []
    for m in re.finditer(rf"(?<![\w$.])(?:{keys})[\"']?\s*[:=]\s*", text):
        line0 = text.count("\n", 0, m.start())
        for line, val, soft in _js_strings(text[m.end() : _value_end(text, m.end())]):
            path = ("/" in val or (val.endswith(exts) and val not in exts)) and "\0" not in val and "://" not in val
            if path and not val.startswith(("/", "~", "$", "@", "!")):
                out.append((line0 + line, posixpath.normpath(val), soft))
    return out


class JsToolRoots(_Keyed):
    """A tool config (`vite.config.ts`, `.eslintrc.js`, `gulpfile.js`, `.storybook/main.ts`) is loaded by its tool, nobody imports it: an
    entry (kind main), and the files it names as bundle entries (webpack `entry`, rollup `input`, tsup / esbuild `entry` and
    `entryPoints`: kind export, the API of a bundle). An ambient declaration file (`*.d.ts`) is seen by the compiler through
    tsconfig, never imported: its declarations are the API of the project (kind export, hard: graph.reach seeds the exported nodes).
    """

    name, version = "jstool", 2
    data_keys = ("js_tool_files", "js_ambient_files", "js_tool_entry_keys", "js_build")

    def matches(self, path: str) -> bool:
        return bool(_rx("js_tool_files").search(path) or _rx("js_ambient_files").search(path))

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        if _rx("js_ambient_files").search(path):
            return [RootHint("export", "file:" + path, 1, "hard", "convention")]
        out = [RootHint("main", "file:" + path, 1, "hard", "convention")]
        for line, val, soft in _entry_paths(text):
            for t in [val, *_spell(val)]:
                out.append(RootHint("export", f"sibling:./{t}", line, "soft" if soft else "hard", "convention"))
        return _dedup(out)


def _walk(node, parts: Sequence[str]) -> Iterable[str]:
    """The strings at the dotted path `parts` of a JSON document; a list on the way is walked, a string list at the end is its items."""
    if isinstance(node, list):
        for x in node:
            yield from _walk(x, parts)
    elif parts and isinstance(node, dict):
        yield from _walk(node.get(parts[0]), parts[1:])
    elif not parts and isinstance(node, str):
        yield node


class ManifestRoots(_Keyed):
    """A WebExtension manifest names the scripts the browser loads (the service worker, the background and content scripts): nobody
    imports them, so each is an entry, and a built one (`dist/background.js`, usually not in the tree: kind export does not dangle) maps
    to its source like a package entry."""

    name, version = "manifest", 1
    data_keys = ("js_manifest", "js_build")

    def matches(self, path: str) -> bool:
        return bool(re.search(fw()["js_manifest"]["files"], path))

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        doc, pos, out = _json(text), _Lines(text), []
        if not isinstance(doc, dict) or "manifest_version" not in doc:
            return out
        for key in fw()["js_manifest"]["keys"]:
            for v in _walk(doc, key.split(".")):
                if "://" not in v and not v.startswith("//"):
                    rel, at = "./" + posixpath.normpath(v.lstrip("/")), pos.of(v)
                    out += [
                        RootHint("export", "sibling:" + t, at, "hard", "convention") for t in [rel, *_npm_sources(rel)]
                    ]
        return _dedup(out)


_SFC_IMPORT = re.compile(r"""(?:\bfrom|\bimport\s*\(?|\brequire\s*\()\s*(['"])([^'"\n]+)\1""")


def _tails(spec: str) -> List[Tuple[str, str]]:
    """[(path, scheme)] a specifier of a script names: relative to the file, from the project root, or by an alias prefix."""
    if spec.startswith("."):
        return [(posixpath.normpath(spec), "sibling:./")]
    if spec.startswith("/") and not spec.startswith("//"):
        return [(spec.lstrip("/"), "file:")]
    for alias, dirs in fw()["js_alias_roots"].items():
        if spec.startswith(alias):
            return [(posixpath.normpath(d + spec[len(alias) :]), "file:") for d in dirs]
    return []


def _imported_files(code: str, line0: int) -> List[RootHint]:
    """The local files the script `code` (its first line is `line0`) imports, as hard convention roots of kind export: their whole API is
    used by it. A source file or a path that may name one; a component, css or json is no code, a package is no file of ours.
    """
    out = []
    for m in _SFC_IMPORT.finditer(code):
        line = line0 + code.count("\n", 0, m.start())
        for rest, scheme in _tails(m.group(2)):
            if _stems(rest)[0]:
                out += [RootHint("export", scheme + t, line, "hard", "convention") for t in [rest, *_spell(rest)]]
    return out


_SCRIPT = re.compile(r"<script\b([^>]*)>", re.I)
_SRC = re.compile(r"""\bsrc\s*=\s*["']?([^"'\s>#?]+)""", re.I)
_MODULE = re.compile(r"""\btype\s*=\s*["']?\s*module\b""", re.I)


class HtmlRoots(_Keyed):
    """The scripts a page loads are entries of the page: a local `<script type="module" src="/src/main.tsx">` (Vite, Parcel) is an entry of
    kind export (its API), what an inline module script imports likewise; a classic `<script src="app.js">` is kind script: its top-level
    definitions are globals that the page, its inline handlers (`onclick="save()"`) and the other scripts reach by name, so graph.reach seeds
    them all. A remote script is no file of ours. Data: html_files."""

    name, version = "html", 2
    data_keys = ("html_files", "js_alias_roots", "js_build")

    def matches(self, path: str) -> bool:
        return bool(_rx("html_files").search(path))

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        out = []
        for m in _SCRIPT.finditer(text):
            module, src = bool(_MODULE.search(m.group(1))), _SRC.search(m.group(1))
            if src is None and module:  # inline: `<script type="module">import { init } from "./app.js"</script>`
                end = text.find("</script", m.end())
                out += _imported_files(text[m.end() : end if end >= 0 else len(text)], text.count("\n", 0, m.end()) + 1)
            elif src is not None and "://" not in src[1] and not src[1].startswith("//"):
                rel = src[1] if src[1].startswith(("./", "../")) else "./" + src[1].lstrip("/")
                out.append(
                    RootHint("export" if module else "script", "sibling:" + rel, text.count("\n", 0, m.start()) + 1)
                )
        return _dedup(out)


class SfcRoots(_Keyed):
    """The script of a single-file component (`.vue`, `.svelte`, `.astro`) is code no extractor reads, so what it imports would have
    no importer: the local files it imports (relative, or by an alias prefix of the data js_alias_roots) are entries of kind export
    (convention): their whole API is used by it (an imported component is an entry itself: its own imports are read the same
    way). A component that nothing uses keeps its imports alive (an over-approximation). Data: js_sfc (file pattern, script blocks).
    """

    name, version = "sfc", 1
    data_keys = ("js_sfc", "js_alias_roots", "js_build")

    def matches(self, path: str) -> bool:
        return bool(re.search(fw()["js_sfc"]["files"], path))

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        out = []
        for block in fw()["js_sfc"]["blocks"]:
            for blk in re.finditer(block, text, re.S):
                out += _imported_files(blk.group(1), text.count("\n", 0, blk.start(1)) + 1)
        return _dedup(out)


class GitlabRoots(RootExtractor):
    """`.gitlab-ci.yml` is run by GitLab: the pipeline file is a root, the other files are reached through its `include`."""

    name, version = "gitlab", 2

    def matches(self, path: str) -> bool:
        return bool(_rx("gitlab_files").search(path))

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        # no path in the hint: the shard is keyed by the blob, two copies of the file share it
        return [RootHint("ci-job", "self:", 1, "hard", "convention")]


class ActionRoots(RootExtractor):
    """An `action.yml` with a `runs:` section is a GitHub action: another repository runs it with `uses: org/repo[/dir]@ref`,
    which nothing in the tree shows. The repository root is the published action (hard); one below it, e.g. a local composite
    action under .github/actions, may still lack a caller (soft: weak-alive, never dead)."""

    version = 1

    def __init__(self, name: str, key: str, strength: str):
        self.name, self.key, self.strength = name, key, strength

    def matches(self, path: str) -> bool:
        return bool(_rx(self.key).search(path))

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        return [RootHint("export", "self:", 1, self.strength, "convention")] if _rx("action_text").search(text) else []


_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w-]*)")
_BARE = re.compile(r"^[a-z][a-z0-9_.+-]*$")  # a bare word in prose is a command name only when it looks like one
_PROMPT = re.compile(r"^(\$|>|PS>|PS [^>]*>)\s+")


def build_hints(cmd: str) -> List[str]:
    """The `build:<qual>` selectors of the recipes and stages a command line of a doc or a Dockerfile names (a computed word is none)."""
    return [BUILD_SCHEME + q for q in build_targets(cmd) if "*" not in q]


class DocRoots(_Keyed):
    name, version = "docs", 2
    data_keys = ("doc_files", "doc_fences", *_CMD_DATA)

    def matches(self, path: str) -> bool:
        return bool(_rx("doc_files").search(path))

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        out, fence, lang, seen = [], "", "", set()
        for n, raw in enumerate(text.split("\n"), 1):
            m = _FENCE.match(raw)
            if m and not fence:
                fence, lang = m.group(1)[:3], m.group(2).lower()
            elif m and raw.strip().startswith(fence):
                fence = ""
            elif fence:
                s = raw.strip()
                prompt = _PROMPT.match(s)
                s = _PROMPT.sub("", s)
                shell = lang in fw()["doc_fences"] or (not lang and (prompt or s.startswith(("bin/", "./"))))
                build = build_hints(s) if s else []
                # a fence of a shell is read as commands; in a plain fence only `make x` / `just x` is (a recipe named by a
                # line that is not a command costs only a weak-alive verdict, a script path read from prose would be a root)
                if s and s not in seen and not s.startswith("#") and (shell or (not lang and build)):
                    seen.add(s)
                    cmds = command_targets(s) if shell else []
                    out.extend(_hints("doc-cmd", [*cmds, *build], n, "soft", "documented"))
            else:
                for span in re.findall(r"`([^`\n]+)`", raw):
                    if span in seen:
                        continue
                    seen.add(span)
                    if _BARE.match(span):  # a lone word: a command name unless it is a shell builtin or tool
                        ts = [] if span in fw()["builtins"] else ["cmd:" + span]
                    else:
                        ts = [t for t in command_targets(span) if not t.startswith("cmd:") or _BARE.match(t[4:])]
                        ts += build_hints(span)
                    out.extend(_hints("doc-cmd", ts, n, "soft", "documented"))
        return _dedup(out)


for _cls in (
    SystemdRoots,
    CronRoots,
    TaskXmlRoots,
    NpmRoots,
    HtmlRoots,
    JsToolRoots,
    ManifestRoots,
    SfcRoots,
    GitlabRoots,
    DocRoots,
    ClassRegistryRoots,
):
    register_roots(_cls())
for _args in (("action", "action_root_files", "hard"), ("action-nested", "action_nested_files", "soft")):
    register_roots(ActionRoots(*_args))
_rs.register()  # the sources of ordinary projects (pyproject, Cargo, Dockerfile, Makefile ...): graph.rootsrc


def extract_hints(path: str, text: str, cfg=None) -> List[RootHint]:
    """Hints of every registered root extractor that matches `path` (what the shard cache stores per extractor)."""
    return _dedup(h for rx in root_extractors() if rx.matches(path) for h in rx.extract(path, text, cfg))


# ---------------------------------------------------------------- collect (parallel, cached by blob)
def _shards(unit: Tuple[Tuple[str, str, str], ...]) -> list:
    out = []
    for path, blob, text in unit:
        for rx in root_extractors():
            if rx.matches(path):
                out.append((path, make_root_shard(rx, path, blob, text)))
    return out


def _text(data: Optional[bytes]) -> Optional[str]:
    return None if data is None or b"\0" in data[:8192] else data.decode("utf-8", "replace")


def collect(root: str, tree, store=None, cfg=None, workers: Optional[int] = None) -> Dict[str, List[RootHint]]:
    """Hints of every config file of the tree that a root extractor matches, as {path: [RootHint]}. Files under tests or
    fixtures register nothing. Extraction runs through par.run_units (`workers` = --jobs); with a store the shards are read from
    and written to it (key = blob + extractor version), so an unchanged file costs nothing."""
    rxs = root_extractors()
    want = sorted(p for p in tree.files if not _is_fixture(p) and any(rx.matches(p) for rx in rxs))
    keys = {
        (p, rx.name): shard_key(tree.files[p].blob, f"root:{rx.name}", rx.ext_ver)
        for p in want
        for rx in rxs
        if rx.matches(p)
    }
    have = store.has_shards(keys.values()) if store is not None else set()
    hints: Dict[str, List[RootHint]] = {}
    for (p, _), k in sorted(keys.items()):
        if k in have:
            hints.setdefault(p, []).extend(store.get_shard(k).facts().roots)
    cold = sorted({p for (p, _), k in keys.items() if k not in have})
    texts = vcs.read_files(root, tree, cold)
    read = [(p, tree.files[p].blob, t) for p in cold if (t := _text(texts.get(p))) is not None]
    units, cur, size = [], [], 0
    for item in read:  # chunks of about CHUNK_BYTES of text: the cost of an extractor follows the size of the file
        cur.append(item)
        size += len(item[2])
        if size >= CHUNK_BYTES:
            units.append(tuple(cur))
            cur, size = [], 0
    units += [tuple(cur)] if cur else []
    fresh = []
    for res in par.run_units(units, _shards, jobs=workers, key=lambda u: u[0][0] if u else ""):
        fresh.extend(res)
    if store is not None:
        store.put_shards(s for p, s in fresh if keys.get((p, s.lang[5:])) == s.key)
    for p, s in fresh:
        if keys.get((p, s.lang[5:])) == s.key:
            hints.setdefault(p, []).extend(s.facts().roots)
    return {p: _dedup(h) for p, h in sorted(hints.items()) if h}


# ---------------------------------------------------------------- resolve
class RootSet(NamedTuple):
    roots: Tuple[Root, ...]
    findings: Tuple[Finding, ...]


class _Tree:
    def __init__(self, files: Mapping[str, str], maps: Sequence[dict], langs):
        self.files, self.maps, self.langs = files, maps, langs
        self.tails: Dict[str, List[str]] = {}
        self.names: Dict[str, List[str]] = {}
        self.dirs, self.tops, self.cmds = set(), set(), {}
        for p in sorted(files):
            parts = p.split("/")
            self.names.setdefault(parts[-1], []).append(p)
            if len(parts) > 1:
                self.tails.setdefault("/".join(parts[-2:]), []).append(p)
                self.tops.add(parts[0])
            self.dirs.update("/".join(parts[:i]) + "/" for i in range(1, len(parts)))
            if len(parts) == 2 and parts[0] in fw()["bin_dirs"]:
                stem = (
                    os.path.splitext(parts[1])[0] if os.path.splitext(parts[1])[1] in fw()["bin_win_ext"] else parts[1]
                )
                self.cmds.setdefault(parts[1], p)
                self.cmds.setdefault(stem, p)

    def is_code(self, path: str) -> bool:
        lang = self.langs.detect(path)
        return bool(lang) and self.langs.analyzable(lang)

    def rendered(self, path: str) -> List[str]:
        """The template a deploy renders `path` from (an Ansible role's templates/x.service.j2): the same name plus a template
        extension next to it, else that template name anywhere in the tree. Never the plain name elsewhere: a package.json
        entry `dist/index.ts` must not become lib/index.ts or tests/index.ts."""
        exts = fw()["template_ext"]
        near = [path + e for e in exts if path + e in self.files]
        if near:
            return near
        base = posixpath.basename(path)
        return sorted(p for e in exts for p in self.names.get(base + e, ()))[:4]

    def suffix(self, path: str) -> List[str]:
        parts = [x for x in path.split("/") if x]
        return sorted(self.tails.get("/".join(parts[-2:]), ()))[:16] if len(parts) > 1 else []

    def lookup(self, sel: str, kind: str, cfg_path: str) -> Tuple[List[str], str, str]:
        """(paths, trust, missing): a non-empty `missing` is the repo path the registration points at and does not find."""
        scheme, _, val = sel.partition(":")
        pol, f = fw()["dangling"].get(kind, ()), self.files
        if scheme == "cmd":
            return ([self.cmds[val]] if val in self.cmds else []), "convention", ""
        if scheme == "mod":
            cands = mod_files(val.partition("#")[0])  # mod:pkg.cli#main: the function is the resolver's (resolve)
            hit = [c for c in cands if c in f]
            return hit[:1] or [p for c in cands[:2] for p in f if p.endswith("/" + c)][:4], "registered", ""
        if scheme == "sibling":
            c = posixpath.normpath(
                posixpath.join(
                    posixpath.dirname(cfg_path), val or os.path.splitext(os.path.basename(cfg_path))[0] + ".service"
                )
            )
            if "*" in c:  # a pattern (package.json exports `./s/*`, a framework convention): the code files it matches
                return [p for p in sorted(f) if fnmatch.fnmatchcase(p, c) and self.is_code(p)], "registered", ""
            if c in f:
                return [c], "registered", ""
            tpl = self.rendered(c)
            if tpl:
                return tpl, "convention", ""
            return [], "registered", c if "sibling" in pol else ""
        p = val
        if _OPAQUE.search(p) or _ABS.match(p):
            for m in self.maps:
                if p.startswith(m["from"]):
                    c = posixpath.normpath(m["to"] + p[len(m["from"]) :])
                    if c in f:
                        return [c], "registered", ""
                    if m.get("anchored") and "mapped" in pol:
                        return [], "registered", c
            return self.suffix(p), "convention", ""
        if p in f:
            return [p], "registered", ""
        return [], "registered", p if "relative" in pol and not p.startswith("..") else ""

    def node(self, path: str, read: Callable[[str], Optional[str]], qual: str = "") -> str:
        head = read(path) if self.langs.needs_head(path) else None
        return node_key(self.langs.detect(path, head) or "file", path, qual)


def _weak(a: str, b: str) -> str:
    return max(a, b, key=TRUSTS.index)


def resolve(
    hints: Mapping[str, Sequence[RootHint]],
    files: Mapping[str, str],
    read: Callable[[str], Optional[str]],
    maps: Optional[Sequence[dict]] = None,
    langs=None,
) -> RootSet:
    """Roots and findings of `hints` ({config path: hints}) against `files` ({path: git mode}); read(path) -> text or None."""
    tr = _Tree(files, fw()["path_maps"] if maps is None else maps, langs or _langs.load())
    roots: List[Root] = []
    finds: List[Finding] = []
    for cfg_path in sorted(hints):
        if _is_fixture(cfg_path):
            continue
        for h in hints[cfg_path]:
            origin = f"{cfg_path}:{h.line}"
            if h.kind == _rs.BAD:  # a source that cannot be read: W, the file registers nothing (graph.rootsrc)
                finds.append(
                    Finding(
                        "W",
                        cfg_path,
                        h.line,
                        "dc.bad-root-source",
                        f"`{posixpath.basename(cfg_path)}` cannot be read as a root source: {h.target}",
                        f"read by a root extractor at {origin}; the entry points it registers are not rooted, a script only it names may look unreferenced",
                    )
                )
                continue
            if h.target.startswith(
                (CLASS_SCHEME, BUILD_SCHEME)
            ):  # a class that a registration file names, a recipe that a doc names: graph.classreg.bind finds its node
                roots.append(mk_root(h.kind, origin, selector=h.target, strength=h.strength, trust=h.trust))
                continue
            paths, trust, missing = tr.lookup("file:" + cfg_path if h.target == "self:" else h.target, h.kind, cfg_path)
            if missing:
                finds.append(
                    Finding(
                        "E",
                        cfg_path,
                        h.line,
                        "dc.root-dangling",
                        f"{h.kind} registration points at `{missing}`, which is not in the repository",
                        f"registered at {origin} as {h.target}; `{missing}` is not in the tree",
                    )
                )
                roots.append(mk_root(h.kind, origin, selector="file:" + missing, strength=h.strength, trust=h.trust))
            func = h.target.partition("#")[2] if h.target.startswith("mod:") else ""
            for p in paths:
                if func:  # an entry point `pkg.cli:main` runs the function: it is a root of its own, besides the module
                    roots.append(
                        mk_root(h.kind, origin, node=tr.node(p, read, func), strength=h.strength, trust=h.trust)
                    )
                roots.append(
                    mk_root(h.kind, origin, node=tr.node(p, read), strength=h.strength, trust=_weak(h.trust, trust))
                )
    for p, mode in sorted(files.items()):
        parts = p.split("/")
        if len(parts) == 2 and parts[0] in fw()["bin_dirs"]:
            if mode == "100755" or os.path.splitext(p)[1] in fw()["bin_win_ext"] or (read(p) or "").startswith("#!"):
                roots.append(mk_root("bin", f"{p}:1", node=tr.node(p, read), strength="soft", trust="convention"))
        elif is_test_path(p):
            lang = tr.langs.detect(p)
            if lang and tr.langs.analyzable(lang):
                roots.append(mk_root("test", f"{p}:1", node=node_key(lang, p), strength="soft", trust="convention"))
        elif (
            _rx("schema_run_files").search(p) and (lang := tr.langs.detect(p)) and tr.langs.extractor(lang) == "schema"
        ):  # a migration or init script: its tool runs it, and what it alters is used (graph.extract.schema)
            roots.append(mk_root("init", f"{p}:1", node=node_key(lang, p), strength="soft", trust="convention"))
        elif ex := _rs.exec_entry(
            p, mode, (read(p) or "")[:200] if mode == "100755" else ""
        ):  # run by hand: a root on the file
            roots.append(mk_root(ex[0], f"{p}:1", node=tr.node(p, read), strength=ex[1], trust=ex[2]))
        # a script is run by its tool (Gradle runs build.gradle.kts): nothing names it
        elif lang := tr.langs.script_lang(p):
            roots.append(mk_root("main", f"{p}:1", node=node_key(lang, p), strength="hard", trust="convention"))
    key = lambda r: (r.kind, r.origin, r.node, r.selector, r.strength, r.trust, r.stub)  # noqa: E731
    return RootSet(tuple(sorted(set(roots), key=key)), tuple(sort_findings(finds)))


def build(root: str, tree=None, store=None, cfg=None, workers: Optional[int] = None, maps=None, langs=None) -> RootSet:
    """collect + resolve for the work tree of `root` (the tree is snapshotted unless given); file text comes through ONE
    cat-file batch per call (the files that resolve reads are prefetched by a dry resolve)."""
    tree = tree or vcs.snapshot(root, workers=workers)
    hints = collect(root, tree, store, cfg, workers)
    files = {p: f.mode for p, f in tree.files.items()}
    asked: List[str] = []
    resolve(hints, files, lambda p: asked.append(p), maps, langs)
    cache = {p: _text(d) for p, d in vcs.read_files(root, tree, set(asked)).items()}
    return resolve(hints, files, lambda p: cache.get(p), maps, langs)
