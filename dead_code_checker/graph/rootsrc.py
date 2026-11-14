"""graph.rootsrc: the root sources of ordinary projects: packaging, build and container files. Data: data/rootsrc.json.

Root extractors (graph.contracts.RootExtractor, cached as virtual shards "root:<name>" by the blob sha) that read ONE file's text and
execute nothing. They register through graph.roots (which calls register()); graph.roots.resolve turns the hints into roots, so each root
carries its kind and the file:line of the registration for why-alive.
       pyproject   [project.scripts] [project.gui-scripts] [project.entry-points.*] [tool.poetry.scripts|plugins.*]
                   [tool.setuptools] script-files                                                  -> bin | export, target mod:<module>#<func>
       setup-cfg   [options.entry_points] and [options] scripts                                       -> bin | export
       setup-py    setup(entry_points=..., scripts=...) read with ast, a literal only (a name bound to a literal at module level is
                   followed); a value that is no literal is W, nothing is ever imported or run     -> bin | export
       cargo       [[bin]] [lib] [[example]] [[bench]] [[test]] build, and what cargo finds by convention (src/main.rs, src/bin,
                   examples, benches, build.rs); the library is a root (export) when the crate is published, a proc-macro or a cdylib
                                                                                                    -> bin | export | init | test
       csproj      OutputType Exe (Program.cs by convention; Main and top-level statements are the C# extractor's), a test project
                   (IsTestProject, a test framework reference): every .cs of the project is test code    -> bin | test
       gradle      mainClass of the application / Spring Boot plugin, Main-Class of a manifest            -> main (class:<name>)
       maven       mainClass / start-class of exec, spring-boot and jar plugins in pom.xml                -> main (class:<name>)
       docker      CMD, ENTRYPOINT, HEALTHCHECK CMD -> bin; RUN -> ci-job. A path is the script itself, or, when COPY / ADD put a
                   source file or directory at the path (WORKDIR followed), that source; a Makefile / justfile recipe the line names
                   (`RUN make x`, build:<qual> of graph.classreg) is a root as well
       make, just  the recipes of a Makefile / justfile (variables expanded, `$(shell ..)` read, an unknown leading variable keeps
                   only a script path)                                                                -> ci-job
       compose     `command` and `entrypoint` of the services of a docker-compose file                          -> ci-job
          exec_entry  (graph.roots.resolve, a tree rule: the git mode is no text) a tracked file with the exec bit and a shebang line, outside a
                   test path                                                                      -> bin (hard, the file itself)
   Not here, already read elsewhere (tests pin each): package.json bin/main/exports/scripts (graph.roots NpmRoots), Go `package main`
   and cmd/* (data/rules.json roots), *_test.go (frameworks.json test_names), Rust main and #[test] (rules.json), C# Main and top-level
   statements (queries/csharp/roots.scm), Spring Boot @SpringBootApplication (frameworks.json), GitHub / GitLab CI run and script
   lines, k8s and Helm command / args (the cfg extractor).
The files themselves (Dockerfile, Makefile, compose file) are no roots: a config file lives through its readers, as the tests of
the cfg extractor pin (an unreferenced Dockerfile is dead); what they RUN is a root whatever their own verdict.
A source that cannot be read (broken TOML / XML / ini / syntax, a value of the wrong type) is the hint RootHint(BAD, reason, line):
graph.roots.resolve turns it into W dc.bad-root-source and reads nothing else of the file; no extractor raises. Facts of one file depend
on its text only, so a path in a hint is relative to the file (sibling:) or to the repository root (file:), both are given where
the working directory is not known (Makefile, Dockerfile).
"""

from __future__ import annotations

import ast
import configparser
import hashlib
import json
import os
import posixpath
import re
import shlex
import tomllib
import xml.etree.ElementTree as ET
from functools import lru_cache
from typing import Dict, Iterable, List, Sequence, Tuple

from .contracts import RootExtractor, register_roots
from .extract import yamlite
from .model import RootHint

BAD = "bad-source"  # RootHint kind of a source that cannot be read; target = the reason
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "rootsrc.json")


@lru_cache(maxsize=1)
def data() -> dict:
    with open(DATA, encoding="utf-8") as fh:
        return json.load(fh)


def _r():
    """graph.roots, imported late: it imports this module."""
    from . import roots

    return roots


def bad(reason: str, line: int = 1) -> RootHint:
    return RootHint(BAD, " ".join(reason.split())[:200], max(1, line), "soft", "convention")


def _hint(kind: str, target: str, line: int, strength: str = "hard", trust: str = "registered") -> RootHint:
    return RootHint(kind, target, line, strength, trust)


def _table(v, what: str) -> dict:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ValueError(f"{what} is not a table")
    return v


def _rel(p: str) -> str:
    """A path of a manifest as a relative selector value: no ./, no leading /, "" when it leaves the directory."""
    n = posixpath.normpath(p.replace("\\", "/"))
    return "" if n.startswith(("/", "..")) or n == "." else n


def _err_line(e: Exception) -> int:
    """Line of a parse error: SyntaxError.lineno, ElementTree ParseError.position, "(at line N" of tomllib, else 1."""
    pos = getattr(e, "position", None)
    if isinstance(getattr(e, "lineno", None), int):
        return e.lineno  # type: ignore[attr-defined]
    m = re.search(r"line (\d+)", str(e))
    return pos[0] if isinstance(pos, tuple) else int(m.group(1)) if m else 1


class _Src(RootExtractor):
    """A source file read by `read(text)`; its cache key carries the sha of data/rootsrc.json."""

    version = 1
    key = ""

    @property
    def ext_ver(self) -> str:
        return (
            f"{self.name}@{self.version}.{hashlib.sha256(json.dumps(data(), sort_keys=True).encode()).hexdigest()[:8]}"
        )

    def matches(self, path: str) -> bool:
        return bool(re.search(data()["files"][self.key], path))

    def read(self, text: str) -> List[RootHint]:
        raise NotImplementedError

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        try:
            return _r()._dedup(self.read(text.lstrip("\ufeff")))
        except Exception as e:  # a source that cannot be read is a W, a bug of ours too: never a crash, never a block
            return [bad(f"{type(e).__name__}: {e}", _err_line(e))]


# ---------------------------------------------------------------- Python packaging
_SPEC = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*(?::\s*([\w.]+))?\s*(?:\[[^\]]*\])?\s*$")  # pkg.mod:func [extra]


class _Fixed:
    """The `of` of _Lines for a source that has no text positions (an ast node knows its line)."""

    def __init__(self, line: int):
        self.line = line

    def of(self, s: str) -> int:
        return self.line


def _entries(items, kind: str, at, head: str, out: List[RootHint]) -> None:
    """Entry points {name: "pkg.mod:func"} -> bin / export hints on mod:<module>#<func>, each on the line of its spec (searched after the
    line of `head`, the section or group); a value that is no spec is a bad hint."""
    at.of(head)
    for name, spec in items.items():  # file order
        m = _SPEC.match(spec) if isinstance(spec, str) else None
        line = at.of(spec) if isinstance(spec, str) and spec else at.of(name)
        if m:
            out.append(_hint(kind, "mod:" + m.group(1) + ("#" + m.group(2) if m.group(2) else ""), line))
        else:
            out.append(bad(f"entry point `{name}` is not `module:function`: {str(spec)[:60]}", line))


def _scripts(paths, line: int, out: List[RootHint]) -> None:
    for p in paths if isinstance(paths, list) else [paths]:
        if _rel(str(p)):
            out.append(_hint("bin", "sibling:" + _rel(str(p)), line))


class PyprojectRoots(_Src):
    name, key = "pyproject", "pyproject"

    def read(self, text: str) -> List[RootHint]:
        doc, out = tomllib.loads(text), []
        at = _r()._Lines(text)
        proj, tool = _table(doc.get("project"), "[project]"), _table(doc.get("tool"), "[tool]")
        poetry, groups = _table(tool.get("poetry"), "[tool.poetry]"), data()["py_bin_groups"]
        for sect in ("scripts", "gui-scripts"):
            _entries(_table(proj.get(sect), f"[project.{sect}]"), "bin", at, f"[project.{sect}]", out)
        for g, items in _table(proj.get("entry-points"), "[project.entry-points]").items():
            _entries(_table(items, f"[project.entry-points.{g}]"), "bin" if g in groups else "export", at, g, out)
        files = {}
        for name, v in _table(poetry.get("scripts"), "[tool.poetry.scripts]").items():
            if isinstance(v, dict) and v.get("type") == "file":  # poetry 2: a script file of the repository
                files[name] = v.get("reference", "")
            else:
                _entries(
                    {name: v.get("reference") if isinstance(v, dict) else v}, "bin", at, "[tool.poetry.scripts]", out
                )
        for name, ref in files.items():
            _scripts([ref], at.of(name), out)
        for g, items in _table(poetry.get("plugins"), "[tool.poetry.plugins]").items():
            _entries(_table(items, f"[tool.poetry.plugins.{g}]"), "export", at, g, out)
        for p in _table(tool.get("setuptools"), "[tool.setuptools]").get("script-files", []):
            _scripts([p], at.of(str(p)), out)
        return out


def _items(body: str) -> Dict[str, str]:
    """`name = module:func` lines of an entry point group (a line with no `=` is a name with no spec: a bad entry)."""
    out = {}
    for ln in body.splitlines():
        k, eq, v = ln.partition("=")
        if ln.strip():
            out[k.strip() if eq else ln.strip()] = v.strip()
    return out


def _ini(text: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str  # type: ignore[assignment,method-assign]  # entry point names are case sensitive
    cp.read_string(text)
    return cp


def _groups(groups: Dict[str, Dict[str, str]], at, out: List[RootHint]) -> None:
    for g, items in groups.items():
        _entries(items, "bin" if g in data()["py_bin_groups"] else "export", at, g, out)


class SetupCfgRoots(_Src):
    name, key = "setup-cfg", "setup_cfg"

    def read(self, text: str) -> List[RootHint]:
        cp, out, at = _ini(text), [], _r()._Lines(text)
        if cp.has_section("options.entry_points"):
            for g, body in cp.items("options.entry_points"):
                _groups({g: _items(body)}, at, out)
        if cp.has_option("options", "scripts"):
            names = cp.get("options", "scripts").replace(",", "\n").split()
            _scripts(names, at.of("scripts"), out)
        return out


def _setup_kwargs(tree: ast.AST) -> Iterable[Tuple[str, ast.AST]]:
    """(keyword, value node) of every setup(...) call; a name bound to a value at module level is replaced by that value."""
    bound = {
        t.id: n.value for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)
    }
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "setup":
            for kw in n.keywords:
                v = kw.value
                yield kw.arg or "", bound.get(v.id, v) if isinstance(v, ast.Name) else v


class SetupPyRoots(_Src):
    """setup(entry_points=..., scripts=...) of a setup.py: read with ast, a literal only; the file is never imported or run."""

    name, key = "setup-py", "setup_py"

    def read(self, text: str) -> List[RootHint]:
        out = []
        for arg, node in _setup_kwargs(ast.parse(text)):
            if arg not in ("entry_points", "scripts"):
                continue
            try:
                v = ast.literal_eval(node)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                out.append(
                    bad(
                        f"setup({arg}=..) is not a literal, it is not evaluated: its scripts are not rooted",
                        node.lineno,
                    )
                )
                continue
            if arg == "scripts":
                _scripts(v, node.lineno, out)
            elif isinstance(v, str):  # the ini text of setuptools: [group] sections of `name = module:func`
                cp = _ini(v)
                _groups({g: dict(cp.items(g)) for g in cp.sections()}, _Fixed(node.lineno), out)
            else:
                tab = _table(v, "entry_points")
                _groups(
                    {g: _items("\n".join([i] if isinstance(i, str) else i)) for g, i in tab.items()},
                    _Fixed(node.lineno),
                    out,
                )
        return out


# ---------------------------------------------------------------- Cargo, .NET, JVM
class CargoRoots(_Src):
    name, key = "cargo", "cargo"

    def read(self, text: str) -> List[RootHint]:
        doc, out, cg = tomllib.loads(text), [], data()["cargo"]
        at = _r()._Lines(text)
        pkg, lib = _table(doc.get("package"), "[package]"), _table(doc.get("lib"), "[lib]")
        if not pkg and not lib and "bin" not in doc:
            return []  # a workspace manifest: its members have their own

        def put(kind: str, paths: Iterable[str], line: int, strength: str = "hard") -> None:
            out.extend(_hint(kind, "sibling:" + p, line, strength) for p in paths if p)

        head = at.of("[package]")
        for auto, kind, paths, strength in (  # autobins = false (and the like) switches the convention off
            ("autobins", "bin", [*cg["main"], *cg["bins"]], "hard"),
            ("autoexamples", "bin", cg["examples"], "hard"),
            ("autobenches", "test", cg["benches"], "soft"),
        ):
            if pkg.get(auto) is not False:
                put(kind, paths, head, strength)
        for key, kind, strength in (
            ("bin", "bin", "hard"),
            ("example", "bin", "hard"),
            ("bench", "test", "soft"),
            ("test", "test", "soft"),
        ):
            for t in doc.get(key, []):
                t = _table(t, f"[[{key}]]")
                name = str(t.get("name", ""))
                if t.get("path"):
                    paths = [str(t["path"])]
                elif name:  # the file cargo looks for by the target name
                    where = {"bin": "src/bin", "example": "examples", "bench": "benches", "test": "tests"}[key]
                    paths = [f"{where}/{name}.rs", f"{where}/{name}/main.rs"]
                else:
                    paths = []
                put(kind, map(_rel, paths), at.of(name or f"[[{key}]]"), strength)
        build = pkg.get("build", cg["build"])
        if isinstance(build, str):  # `build = false` or a missing default file: nothing
            put("init", [_rel(build)], at.of(build) if "build" in pkg else head)
        types = [*lib.get("crate-type", []), *(["proc-macro"] if lib.get("proc-macro") else [])]
        if pkg.get("publish", True) not in (False, []) or set(types) & set(cg["lib_types"]):
            put("export", [_rel(str(lib.get("path", cg["lib"])))], at.of("[lib]") if lib else head)
        return out


def _tag(e: ET.Element) -> str:
    return e.tag.rpartition("}")[2]


class CsprojRoots(_Src):
    name, key = "csproj", "csproj"

    def read(self, text: str) -> List[RootHint]:
        root, cs, out = ET.fromstring(text), data()["csproj"], []
        props = {_tag(e): (e.text or "").strip() for e in root.iter() if len(e) == 0 and e.text}
        refs = {e.get("Include") or e.get("Update") or "" for e in root.iter() if _tag(e) == "PackageReference"}
        exe = props.get("OutputType") in cs["exe"] or (
            "OutputType" not in props and any(s in root.get("Sdk", "") for s in cs["exe_sdks"])
        )
        if exe:
            out.append(_hint("bin", "sibling:" + cs["entry"], 1))
        if props.get("IsTestProject", "").lower() == "true" or any(re.match(cs["test_refs"], r) for r in refs):
            out.append(_hint("test", "sibling:*.cs", 1, "soft", "convention"))  # every .cs of the project directory
        return out


class GradleRoots(_Src):
    name, key = "gradle", "gradle"

    def read(self, text: str) -> List[RootHint]:
        out, scheme = [], _r().CLASS_SCHEME
        text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), text, flags=re.S)
        for n, ln in enumerate(text.split("\n"), 1):
            for rx in data()["gradle"]:
                out += [
                    _hint("main", f"{scheme}{m.group(1)}", n) for m in re.finditer(rx, re.sub(r"(^|\s)//.*$", "", ln))
                ]
        return out


class MavenRoots(_Src):
    name, key = "maven", "maven"

    def read(self, text: str) -> List[RootHint]:
        root, scheme, at = ET.fromstring(text), _r().CLASS_SCHEME, _r()._Lines(text)
        names = set(data()["maven"])
        return [
            _hint("main", f"{scheme}{e.text.strip()}", at.of(e.text.strip()))
            for e in root.iter()
            if _tag(e) in names and e.text and re.fullmatch(r"[\w.$]+", e.text.strip())
        ]


# ---------------------------------------------------------------- command files: Dockerfile, Makefile, justfile, compose
class ComposeRoots(_Src):
    """`command` and `entrypoint` of the services of a docker-compose file: the scripts of the repository the containers run."""

    name, key = "compose", "compose"

    def read(self, text: str) -> List[RootHint]:
        out = []
        for doc in yamlite.parse(text)[0]:
            services = yamlite.get(doc, "services")
            for _, svc in services.val if services is not None and services.kind == "m" else []:  # type: ignore[union-attr]
                for key in ("command", "entrypoint"):
                    node = yamlite.get(svc, key)
                    if node is None:
                        continue
                    # a string is a shell line, a list is the argv
                    cmd = shlex.join(yamlite.scalar(n) for n in node.val) if node.kind == "s" else yamlite.scalar(node)  # type: ignore[union-attr]
                    for kind, v in _targets(cmd):  # the paths are those of the image: from the repository root only
                        if kind == "file" and _rel(v):
                            out.append(_hint("ci-job", "file:" + v, node.line))
                        elif kind == "cmd":
                            out.append(_hint("ci-job", "cmd:" + v, node.line, "soft", "convention"))
        return out


def _joined(text: str) -> List[Tuple[int, str]]:
    """(first line number, text) of the logical lines of a file: a trailing backslash joins the next line."""
    out, cur, start = [], "", 0
    for n, ln in enumerate(text.split("\n"), 1):
        if not cur:
            start = n
        cur += ln.rstrip("\r")
        if cur.endswith("\\"):
            cur = cur[:-1] + " "
        else:
            out.append((start, cur))
            cur = ""
    return out + ([(start, cur)] if cur else [])


_OPAQUE = re.compile(r"[$%{}@~]|^[A-Za-z]:")  # a variable, a home or a drive path: no repository path


def _targets(cmd: str) -> List[Tuple[str, str]]:
    """(kind, value) of what a command line runs: ("file", path as written, normalized) or ("cmd", bare name); an opaque path is dropped."""
    out = []
    for t in _r().command_targets(cmd):
        kind, _, v = t.partition(":")
        if kind in ("file", "cmd") and v and not _OPAQUE.search(v):
            out.append((kind, posixpath.normpath(v) if kind == "file" else v))
    return out


def _both(kind: str, path: str, line: int) -> List[RootHint]:
    """The hints of a repository path whose base directory is not known: next to the file and from the repository root."""
    return [_hint(kind, f"{s}:{path}", line) for s in ("sibling", "file")] if _rel(path) else []


_VAR = re.compile(r"\$\$|\$[({]([\w.-]+)[)}]|\{\{\s*([\w-]+)\s*\}\}")


def _expand(cmd: str, env: Dict[str, str], depth: int = 0) -> str:
    """Make `$(VAR)` / just `{{var}}` replaced by their assignments; an unknown variable is dropped, `$$` is `$`."""

    def sub(m):
        if m.group(0) == "$$":
            return "$"
        v = env.get(m.group(1) or m.group(2))
        return _expand(v, env, depth + 1) if v is not None and depth < 3 else ""

    return _VAR.sub(sub, cmd)


def _shell_calls(s: str) -> List[str]:
    """The command lines of the $(shell ...) calls of a make line (balanced parentheses)."""
    out = []
    for m in re.finditer(r"\$\(\s*shell\s+", s):
        depth, i = 1, m.end()
        while i < len(s) and depth:
            depth += {"(": 1, ")": -1}.get(s[i], 0)
            i += 1
        out.append(s[m.end() : i - 1 if depth == 0 else i])
    return out


_ASSIGN = re.compile(r"^([A-Za-z_][\w.-]*)\s*(?::=|::=|\?=|\+=|=)\s*(.*?)\s*$")
_LEAD = re.compile(r"(?:\$[({]|\{\{)\s*([\w.-]+)")


def _recipes(text: str, just: bool) -> List[Tuple[int, bool, str]]:
    """(line, lead, command) of the recipe lines of a Makefile / justfile, variables expanded, and of the commands inside their
    `$(shell ..)` calls and backticks. lead: the first word is a variable that no assignment defines (`$(PYTHON) x.py`): only a
    script path of such a line is a root, `$(CP) a b` names no program of the repository."""
    env, out, in_rule = {}, [], False
    for n, ln in _joined(text):
        body = ln.strip()
        if not body or body.startswith("#"):
            continue
        recipe = (ln[:1] in " \t" and in_rule) if just else ln.startswith("\t")
        m = None if recipe else _ASSIGN.match(ln)
        if m:
            env[m.group(1)] = m.group(2).strip("\"'")[:500]  # a bound: variables that refer to variables
            out += [
                (n, False, c) for c in _shell_calls(m.group(2)) + (re.findall(r"`([^`]*)`", m.group(2)) if just else [])
            ]
        elif not recipe:
            in_rule = just and ":" in ln  # a recipe header (`build target="x": dep`); its indented lines follow
        else:
            cmd = body.lstrip("@-+ ")
            lead = _LEAD.match(cmd)
            out.append((n, bool(lead and lead.group(1) not in env and lead.group(1) != "shell"), _expand(cmd, env)))
            out += [(n, False, _expand(c, env)) for c in _shell_calls(cmd)]
    return out


class _CmdFile(_Src):
    just = False

    def read(self, text: str) -> List[RootHint]:
        out = []
        for n, lead, cmd in _recipes(text, self.just):
            if re.match(r"\s*(\$\(MAKE\)|make\b|just\b)", cmd):
                continue
            for kind, v in _targets(cmd):
                if kind == "cmd" and not lead:
                    out.append(_hint("ci-job", "cmd:" + v, n, "soft", "convention"))
                elif kind == "file" and (not lead or os.path.splitext(v)[1] in _r().fw()["script_ext"]):
                    out += _both("ci-job", v, n)
        return out


class MakeRoots(_CmdFile):
    name, key = "make", "make"


class JustRoots(_CmdFile):
    name, key, just = "just", "just", True


_INST = re.compile(r"\s*(?:ONBUILD\s+)?([A-Za-z]+)\s*(.*)$", re.S)
_HEREDOC = re.compile(r"<<-?[\"']?([A-Za-z_]\w*)[\"']?")  # `<<EOF`, not the shift of `1 << 2`
_FLAG = re.compile(r"--[\w-]+(?:=\S+)?\s+")  # RUN --mount=..., COPY --chmod=755, HEALTHCHECK --interval=5s


def _instructions(text: str) -> List[Tuple[int, str, str]]:
    """(line, KEYWORD, arguments) of a Dockerfile: continuations joined, comments dropped; the body of a `RUN <<EOF` heredoc is the
    command (its lines joined with `;`), the body of any other heredoc (COPY <<EOF) is a file, no command."""
    out, i = [], 0
    lines = _joined("\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith("#")))
    while i < len(lines):
        n, ln = lines[i]
        i += 1
        m = _INST.match(ln)
        if not m:
            continue
        kw, rest = m.group(1).upper(), m.group(2).strip()
        h = _HEREDOC.search(rest)
        end = next((j for j in range(i, len(lines)) if lines[j][1].strip() == h.group(1)), -1) if h else -1
        if end >= 0:  # no terminator: not a heredoc, the line is an instruction like any other
            body = [ln.strip() for _, ln in lines[i:end]]
            i = end + 1
            if kw != "RUN":
                continue
            rest = "; ".join(body)
        out.append((n, kw, rest))
    return out


def _copy_words(rest: str) -> List[str]:
    """Words of the arguments of COPY / ADD: the JSON form or the shell form."""
    try:
        v = json.loads(rest)
        if isinstance(v, list):
            return [str(x) for x in v]
    except ValueError:
        pass
    try:
        return shlex.split(rest)
    except ValueError:
        return rest.split()


def _command(kw: str, rest: str) -> str:
    """The command line of an instruction: the exec form (JSON array) is joined into one, HEALTHCHECK has it after CMD."""
    if kw == "RUN":
        while (m := _FLAG.match(rest)) is not None:
            rest = rest[m.end() :]
    if kw == "HEALTHCHECK":
        m = re.search(r"\bCMD\s+(.*)$", rest, re.S)
        rest = m.group(1) if m else ""
    try:
        v = json.loads(rest)
    except ValueError:
        return rest
    return shlex.join(str(x) for x in v) if isinstance(v, list) else rest


def _copied(path: str, copies: Sequence[Tuple[List[str], str]]) -> List[str]:
    """Sources of the COPY / ADD instructions that put `path` (absolute in the image) in place: the file itself, or a directory of
    the context copied as a whole (a candidate that is not in the tree is silent)."""
    out = []
    for srcs, dest in copies:
        base = dest.rstrip("/")
        for s in srcs:
            if path in (base, posixpath.join(base, posixpath.basename(s))):
                out.append(s)
            if path.startswith(base + "/"):
                out.append(posixpath.normpath(posixpath.join(s, path[len(base) + 1 :])))
    return out


class DockerRoots(_Src):
    name, key, version = "docker", "docker", 2  # 2: the recipes a command line names (build:<qual>)

    def read(self, text: str) -> List[RootHint]:
        out, wd, copies = [], "/", []
        for n, kw, rest in _instructions(text):
            if kw == "WORKDIR" and rest and "$" not in rest:
                wd = posixpath.normpath(posixpath.join(wd, rest.strip("\"'")))
            elif kw in ("COPY", "ADD"):
                w = [a for a in _copy_words(_FLAG.sub("", rest + " ")) if "://" not in a]
                if "--from" not in rest and len(w) >= 2:
                    copies.append(([_rel(a) or "." for a in w[:-1]], posixpath.join(wd, w[-1])))
            elif kw in ("CMD", "ENTRYPOINT", "RUN", "HEALTHCHECK"):
                kind, cmd = "ci-job" if kw == "RUN" else "bin", _command(kw, rest)
                recipes = _r().build_hints(cmd)  # `RUN make x`: the recipe x
                out += [_hint(kind, q, n, "soft", "convention") for q in recipes]
                for what, v in _targets(cmd):
                    if what == "cmd":
                        out.append(_hint(kind, "cmd:" + v, n, "soft", "convention"))
                        continue
                    out += _both(kind, v, n)  # the path as written, when the image is built from the context root
                    for src in _copied(posixpath.normpath(v if v.startswith("/") else posixpath.join(wd, v)), copies):
                        out += _both(kind, src, n)
        return out


EXTRACTORS = (
    PyprojectRoots,
    SetupCfgRoots,
    SetupPyRoots,
    CargoRoots,
    CsprojRoots,
    GradleRoots,
    MavenRoots,
    DockerRoots,
    MakeRoots,
    JustRoots,
    ComposeRoots,
)


@lru_cache(maxsize=1)
def _exec_rule():
    d = data()["exec_entry"]
    return d, re.compile(d["shebang"]), re.compile(d["skip"])


def exec_entry(path: str, mode: str, head: str):
    """(kind, strength, trust) of the root that a tracked executable script is, else None: the exec bit of the git mode and a shebang
    line (people run such a file by hand, data/rootsrc.json exec_entry). The root sits on the file itself: it is alive, and so is
    what its top-level code calls; a function nothing calls stays what it was."""
    d, shebang, skip = _exec_rule()
    if mode != d["mode"] or skip.search(path) or not shebang.match(head or ""):
        return None
    return d["kind"], d["strength"], d["trust"]


def register() -> None:
    """Register the extractors (graph.roots calls this each time it is loaded, so a reset of the registry and a reload restore them)."""
    for cls in EXTRACTORS:
        register_roots(cls())
