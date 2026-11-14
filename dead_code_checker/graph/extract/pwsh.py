"""graph.extract.pwsh: the PowerShell extractor (a pwsh-AST worker).

No tree-sitter grammar is involved. The source is PARSED (never run) by the PowerShell parser itself,
[System.Management.Automation.Language.Parser]::ParseInput, in one long-lived `pwsh -NoProfile -NonInteractive` worker per
Python process (data/pwsh/ast.ps1: JSON request per stdin line, one JSON reply per stdout line; the 1.3 s start of pwsh is paid
once per worker, not per file). pwsh (PowerShell 7) is found by $DEAD_CODE_CHECKER_PWSH (a path to the executable), else on PATH; the
worker only parses, it never runs what it reads. A missing pwsh is ExtractorMissing and probe() / missing_finding() give W
dc.degraded-extractor (the PowerShell files stay out of the graph). Nothing falls back to a regex.

Facts (a function of the text only)
  names    case-folded: PowerShell is case-insensitive, so Get-Foo and get-foo are one def (the report shows the folded name).
  defs     "" = the file (kind module); function/filter/workflow -> func (`script:`/`global:`/`local:` prefixes are dropped, `private:` makes
           it private); class/enum -> class, interface -> iface; methods "cls.m" (ctor "cls.cls": link.is_ctor makes it a protocol member,
           alive with its class); properties "cls.p" (kind var; data/pwsh/rules.json "protocol": a property and the methods the language calls by convention, ToString, Dispose, Parse ..., live with their class). A function
           whose body has no statement is a stub (F_STUB). Names listed by Export-ModuleMember are vis "exported".
  refs     call (a command word, `& name`, [T]::M() as "t.m", $x.M() as "x.m", `$this.M()` as "this.m", "?.m" for a computed receiver; [T]::new
           also calls "t"), ref (a type [T] in a cast/parameter, a property read, any plain-name argument: `New-Object Bar`,
           `Get-Command helper`, `Set-Alias al helper`, `Mock Get-Foo` name a class or a function; weak, over-alive and never a false dead),
           include (`. path`, `using module`, Import-Module PATH, Import-Module NAME / `using module NAME` / a module directory: the spellings
           NAME.psd1, NAME.psm1, NAME/NAME.psd1, NAME/NAME.psm1, RootModule/NestedModules of a manifest; `$PSScriptRoot` and `./` removed,
           backslashes -> /), exec (`& path-to.ps1`), inherit (class bases, scope = the derived class).
  dyn      a computed command word (tier D): `& $x`, `. $x` (the text), `& "Get-$n"` (name:get-*), `& ("Get-" + 'Zed')` (name:*get-*zed*),
           `. (Join-Path $PSScriptRoot 'x.ps1')` (the text, plus an include of x.ps1); `$x.$name()`, a computed member; text cut to 60 chars.
           `${function:Name}` is a ref to the function.
  sinks    data/pwsh/rules.json "sinks": a string handed to a command or member that EVALUATES it is code. `iex "Lit-Arg -n 1"` and
           `[scriptblock]::Create("Via-Sb 1")` call the first word of each statement; `powershell -Command "x"` and `Start-Process pwsh
           -ArgumentList "-NoProfile -Command y"` make every word of the command line a weak ref (a script path an exec); `iex "Do-$action"` is
           the dyn name glob name:do-*, so is `Get-Command 'Test-*'`; `iex $cmd` is a dyn site (the functions of the file).
  globs    data/pwsh/rules.json "globs": in a file that loads a computed path (a dyn `. $f.FullName` / `& $f`, a computed Import-Module, a
           computed sink string) the pattern of a Get-ChildItem is an include glob (`Public/*.ps1`, `-Recurse` -> `Public/**/*.ps1`): the classic
           module layout (a .psm1 that dot-sources Public/*.ps1 and Private/*.ps1 in a loop) reaches its files. link.py matches the glob.
           So does every string of such a file that names a script (`$p = Join-Path $PSScriptRoot 'x.ps1'; . $p`, `@('a.ps1', 'b.ps1') | % { . $_ }`)
           and every string shaped like a command, Verb-Noun (a dispatch table `@{ build = 'Invoke-Build' }; & $map[$a]`): a weak ref to it.
  roots    a command at the top level of the file that rules.json script_skip does not list (Set-StrictMode, Import-Module ..: what a library does too) is a
           statement of a script: soft main on the file (not for a file with Export-ModuleMember: a module), so what the statements call is used and a file
           of functions only gets none;
           Export-ModuleMember -Function a,b (export, soft, only for names defined in the file; a name defined elsewhere becomes a ref, so
           no E dc.root-dangling), `-Function *` (every function of the file); a COMPUTED list (`-Function $Public.Basename`) is `*` for the
           file and the dyn site funcs:<glob> (the functions of the files that the glob of a Get-ChildItem of the file names: the Public folder); `# deadcode: root <why>` before a def (manual); a function with an [Alias('x')] in its param block
           (annotation:alias, soft: callers use the alias, which is no def).
  psd1     FunctionsToExport/CmdletsToExport/AliasesToExport names are refs from the file; RootModule, NestedModules, ModulesToProcess and ScriptsToProcess are includes; the manifest is not a root by itself (roots.py).
Known gaps: script entry (`main`) is decided by roots.py (bin/, shebang), not here; a name written as a computed string or inside a
script block is seen only as the plain-name arguments and calls it contains (a string of a sink: see above); `hidden` members are public; a sink string
built by concatenation or by a function is a dyn site, never a name.

Worker records ([kind, name, startLine, startCol, endLine, endCol, extra...]): str (a string that ends with .ps1/.psm1/.psd1 or is Verb-Noun) | func +isFilter, statements, hasAlias | class +bases, "class|enum|interface" |
method +isCtor, statements, isStatic | prop | cmd +form (call|dot|amp), args, computed args | member +isStatic, receiver, isCall, args, computed args | type |
using (name) +usingKind | fvar (name) | dyn (name = text) +form, shape var|str|expr, literal pieces | hash (name = key) +values.

Seams (tests): set_worker(fn) replaces the worker: fn(text) -> dict like the worker's reply; set_finder(fn) replaces the pwsh lookup.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
from bisect import bisect_right
from functools import lru_cache
from typing import Callable, List, NamedTuple, Optional

from ..contracts import ExtractorMissing, Extractor, register_extractor
from ..model import F_PROTOCOL, F_STUB, Def, DynSite, Facts, Finding, Ref, RootHint
from . import xlang
from .common import merge_defs, module_def, protocol_member

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "pwsh")
SCRIPT, RULES = os.path.join(DATA, "ast.ps1"), os.path.join(DATA, "rules.json")
TIMEOUT = 60.0  # seconds for one reply: a parse of a hostile file must not hang the graph build
DYN_MAX = 60
SCOPE_PREFIX = re.compile(r"^(global|script|local|private):", re.I)
NAMEISH = re.compile(r"^[A-Za-z_][\w-]*$")  # a command or type name: Get-Foo, helper, Bar
PATHISH = re.compile(r"[\\/]|\.(ps1|psm1|psd1)$", re.I)
SCRIPT_EXT = re.compile(r"\.(ps1|psm1|psd1)$", re.I)
COMPUTED = re.compile(
    r"\$|^\("
)  # the text of an argument that holds a variable or is a subexpression (not `@('a', 'b')`)
VAR = re.compile(r"\$\{[^}]*\}|\$\([^)]*\)|\$\w+(?::\w+)?")  # a variable or subexpression inside an expandable string
EXPORT_KEYS = ("functionstoexport", "cmdletstoexport", "aliasestoexport")
INCLUDE_KEYS = ("rootmodule", "nestedmodules", "modulestoprocess", "scriptstoprocess")
BUILTIN_TYPES = frozenset(
    "int long double float decimal bool byte char string object void array hashtable psobject pscustomobject scriptblock datetime timespan regex xml guid uri version switch securestring".split()
)  # types that name nothing in the repo
MARKER = re.compile(r"deadcode:\s*root\b")
STMT = re.compile(r"[;|&\r\n{}()]+")  # the statements of a command string
HOLE = "\0"  # a variable of an expandable string, inside a sink string

_worker_fn: Optional[Callable] = None
_finder: Optional[Callable] = None
_local = threading.local()


def set_worker(fn: Optional[Callable]) -> None:
    global _worker_fn
    _worker_fn = fn


def set_finder(fn: Optional[Callable]) -> None:
    global _finder
    _finder = fn


# ------------------------------------------------------------------ the pwsh executable
ENV_PWSH = "DEAD_CODE_CHECKER_PWSH"
INSTALL_HINT = "install PowerShell 7 (pwsh) and put it on PATH, or name it in $" + ENV_PWSH


class Res(NamedTuple):
    path: Optional[str]
    status: str  # "ok" | "missing"
    detail: str


def _find() -> Res:
    """Where pwsh is: $DEAD_CODE_CHECKER_PWSH when set (it must be an executable file), else PATH. Tests replace it with set_finder."""
    if _finder is not None:
        return _finder()
    named = os.environ.get(ENV_PWSH)
    exe = named if named else shutil.which("pwsh")
    if exe and os.path.isfile(exe) and os.access(exe, os.X_OK):
        return Res(exe, "ok", "")
    return Res(None, "missing", f"{named} is not an executable file" if named else "pwsh is not on PATH")


def missing_finding(lang: str, path: str, cfg: Optional[dict] = None) -> Optional[Finding]:
    """None when pwsh is usable; else W dc.degraded-extractor: the PowerShell files are not extracted (nothing is required, nothing waived)."""
    res = _find()
    if res.status == "ok":
        return None
    return Finding(
        "W",
        path,
        1,
        "dc.degraded-extractor",
        f"{lang}: {res.detail} ({INSTALL_HINT}); the PowerShell files are not extracted and stay out of the graph",
    )


class _Worker:
    """One pwsh process serving many files; a reader thread makes the reply wait bounded."""

    def __init__(self, exe: str):
        self.proc = subprocess.Popen(
            [exe, "-NoProfile", "-NonInteractive", "-File", SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={k: v for k, v in os.environ.items() if k not in ("PSModulePath",)},
        )
        self.lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for ln in self.proc.stdout:
            self.lines.put(ln)
        self.lines.put(None)

    def ask(self, text: str) -> dict:
        req = json.dumps({"text": text}, ensure_ascii=True).encode() + b"\n"
        try:
            self.proc.stdin.write(req)
            self.proc.stdin.flush()
            ln = self.lines.get(timeout=TIMEOUT)
        except (OSError, queue.Empty):
            ln = None
        if not ln:
            self.close()
            return {"errors": 1, "rec": [], "fail": "pwsh worker did not answer"}
        return json.loads(ln.decode("utf-8", "replace"))

    def close(self) -> None:
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None


def _real_worker(text: str) -> dict:
    w = getattr(_local, "w", None)
    if w is None or not w.alive:
        res = _find()
        if res.status != "ok" or not res.path:
            raise ExtractorMissing("powershell", f"{res.detail} ({INSTALL_HINT})")
        try:
            w = _local.w = _Worker(res.path)
        except OSError as exc:
            raise ExtractorMissing("powershell", f"pwsh cannot be started: {exc}") from exc
        atexit.register(w.close)
    return w.ask(text)


# ------------------------------------------------------------------ records -> facts
def _flat(v) -> List[str]:
    """The worker wraps argument lists once more (PowerShell array unrolling): flatten to a list of strings."""
    out: List[str] = []
    for x in v if isinstance(v, list) else [v]:
        out.extend(_flat(x) if isinstance(x, list) else [str(x)])
    return out


def _path(s: str) -> str:
    s = re.sub(r"^\$?\{?(PSScriptRoot|PSCommandPath)\}?", "", s.strip("\"'"), flags=re.I)
    s = re.sub(r"^\$\(?\(?Split-Path[^)]*\)?\)?", "", s).replace("\\", "/")
    return re.sub(r"^(\./)+|^/+", "", s)


def _module_refs(arg: str, line: int, sc: str) -> List[Ref]:
    """`Import-Module Tools`, `using module Tools`, `Import-Module -Name $PSScriptRoot\\Tools`: a module named or located by its directory is
    the manifest Tools/Tools.psd1 (which Import-Module prefers) or the Tools.psm1 of it, at the top or in a directory of that name: an
    include of every spelling, the one that is a file of the tree resolves and the others name nothing."""
    p = _path(arg).rstrip("/")
    if not re.fullmatch(r"[\w.\-/]+", p) or SCRIPT_EXT.search(p):
        return []
    return [
        Ref(f"{q}.{ext}", "include", line, sc) for q in (p, f"{p}/{p.rpartition('/')[2]}") for ext in ("psd1", "psm1")
    ]


def _fold(name: str) -> str:
    return SCOPE_PREFIX.sub("", name).lower()


def _dyn(text: str, form: str, shape: str, frags) -> tuple:
    """(DynSite pattern, script paths) of a computed command word. `& "Get-$n"` -> name:get-* (every function of that shape in the
    repo is a target), `& ("Get-" + 'Zed')` -> name:*get-*zed*, `. (Join-Path $PSScriptRoot 'x.ps1')` -> an include of x.ps1 (the
    last literal piece that names a script); `& $x` and a shape with no literal keep the text (the functions of the file).
    """
    frags = [f for f in _flat(frags) if f]
    pieces = [p for f in frags for p in VAR.split(f) if p]
    inc = [p for p in pieces if SCRIPT_EXT.search(p)] if form != "call" else []
    glob = VAR.sub("*", frags[0]) if shape == "str" and len(frags) == 1 else "*" + "*".join(pieces) + "*"
    if form == "amp" and shape != "var" and glob.strip("*") and not inc:
        return _name_glob(glob), inc
    return text[:DYN_MAX], inc


def _name_glob(glob: str) -> str:
    """The DynSite pattern of a command-name glob: names are lowercase and `[`, `]`, `?` are literal characters of a name."""
    return ("name:" + re.sub(r"[\[\]?]", lambda m: f"[{m.group()}]", glob).lower())[:DYN_MAX]


def _sink(text: str, mode: str) -> tuple:
    """([(name, kind)], [dyn pattern]) of a string that a sink evaluates (see "sinks" of the module docstring). Mode command: the first word that is
    no parameter of each statement is a command (call); mode words: every such word of a launcher's command line is a weak ref. A word with a
    variable or a `*` in it is a name glob; one that is only a variable is the dyn site of the whole string (command mode only).
    """
    refs, dyn = [], []
    for stmt in STMT.split(VAR.sub(HOLE, text)):
        words = [w for w in (x.strip("\"'") for x in stmt.split()) if w and w[0] != "-"]
        for w in words[:1] if mode == "command" else words:
            glob = w.replace(HOLE, "*")
            if "*" in glob:  # `Do-$action`, `Get-Command "Test-*"`
                if glob.strip("*"):
                    dyn.append(_name_glob(glob))
                elif mode == "command" and HOLE in w:
                    dyn.append(text[:DYN_MAX])
            elif PATHISH.search(w):
                refs.append((_path(w), "exec"))
            elif NAMEISH.match(w):
                refs.append((_fold(w), "call" if mode == "command" else "ref"))
    return refs, dyn


def _sink_sites(mode: str, args: List[str], computed: List[str], line: int, sc: str) -> tuple:
    """(refs, dyn sites, loads) of the arguments of a sink; `loads`: a computed script string was run (it may load a file)."""
    refs, dyn = [], []
    for a in args:
        r, d = _sink(a, mode)
        refs += [Ref(n, k, line, sc) for n, k in r]
        dyn += d
    if mode == "command":
        dyn += [c[:DYN_MAX] for c in computed]
    return refs, [DynSite(p, line, sc) for p in dyn], bool(computed) and mode == "command"


def _glob(rule: dict, args: List[str]) -> str:
    """The include glob (relative to the script) that a Get-ChildItem names, "" when it cannot name a script. -Path/-LiteralPath or the first
    positional is the path (the pieces of `(Join-Path $PSScriptRoot 'Public')` are joined, the commands that only build it are dropped),
    -Filter/-Include the pattern, -Recurse inserts `**/`."""
    mode, pieces, pat, rec = "", [], "", False
    for a in args:
        if a[:1] == "-":
            mode = a[1:].lower()
            rec = rec or mode in rule["recurse"]
            mode = "" if mode in rule["switch"] else mode
        elif mode in rule["filter"]:
            pat = a
        elif mode in ("", *rule["path"]) and a.lower() not in rule["helpers"]:
            pieces.append(VAR.split(_path(a))[-1].strip("/"))
    path = "/".join(x for x in pieces if x)
    last = path.rpartition("/")[2]
    full = path if re.search(r"[*?]", last) else f"{path}/{pat or '*'}" if path else pat or "*"
    head, _, tail = full.rpartition("/")
    if rec:
        full = f"{head}/**/{tail}" if head else f"**/{tail}"
    return full if "." not in tail or SCRIPT_EXT.search(tail) or tail.endswith(".*") else ""


@lru_cache(maxsize=1)
def _rules() -> dict:
    with open(RULES, encoding="utf-8") as fh:
        return json.load(fh)


def _facts(text: str, reply: dict) -> Facts:
    rec = [r for r in reply.get("rec", []) if isinstance(r, list) and len(r) >= 6]
    shapes = [r for r in rec if r[0] in ("func", "class", "method", "prop")]
    shapes.sort(key=lambda r: ((r[2], r[3]), (-r[4], -r[5])))  # outermost first at one start
    defs, scopes, stack, roots = [], [], [], []  # scopes: (start, end, qual) sorted by start
    rules = _rules()
    for r in shapes:
        s, e, kind, name = (r[2], r[3]), (r[4], r[5]), r[0], r[1]
        while stack and stack[-1][0] <= s:
            stack.pop()
        parent = stack[-1] if stack else None
        qual = f"{parent[1]}.{_fold(name)}" if parent else _fold(name)
        if kind == "func" and parent and parent[2] == "class":
            kind = "method"
        stub = "empty body" if (kind == "func" and r[7] == 0) or (kind == "method" and not r[6] and r[7] == 0) else ""
        dk = {"func": "func", "method": "method", "prop": "var"}.get(kind) or (
            "iface" if r[7] == "interface" else "class"
        )
        vis = "private" if kind != "class" and name.lower().startswith("private:") else "public"
        proto = protocol_member(rules, qual, dk, vis, parent[2] if parent else "")
        defs.append(Def(qual, dk, s[0], e[0], vis, (F_STUB if stub else 0) | (F_PROTOCOL if proto else 0), -1, stub))
        if (
            kind == "func" and len(r) > 8 and r[8]
        ):  # [Alias('gf')]: callers use the alias, which names no def: weak root, never a false dead
            roots.append(RootHint("annotation:alias", qual, s[0], "soft", "convention"))
        if kind != "prop":
            stack.append((e, qual, "class" if dk in ("class", "iface") else dk))
            scopes.append((s, e, qual))
    starts = [x[0] for x in scopes]

    def scope_of(p: tuple) -> str:  # innermost definition that contains p
        i = bisect_right(starts, p) - 1
        while i >= 0:
            if scopes[i][0] <= p < scopes[i][1]:
                return scopes[i][2]
            i -= 1
        return ""

    top = 0  # the first statement at the top level of the file that runs something: the file is a script
    skip = frozenset(rules.get("script_skip", ()))
    refs, dyn, exported, grand = set(), set(), [], False  # grand: an Export-ModuleMember list that is computed
    sinks, globs, enum, named, loads = rules.get("sinks", {}), rules.get("globs", {}), [], set(), False
    for r in rec:
        kind, line, sc = r[0], r[2], scope_of((r[2], r[3]))
        if kind == "class":
            refs.update(Ref(_fold(b), "inherit", line, _fold(r[1])) for b in _flat(r[6]))
        elif kind == "cmd":
            name, form, args = r[1], r[6], _flat(r[7])
            computed, low = _flat(r[8]) if len(r) > 8 else [], name.lower()
            mode = sinks.get("cmd", {}).get(low[:-4] if low.endswith(".exe") else low)
            if mode:
                add, sites, ran = _sink_sites(mode, args, computed, line, sc)
                refs.update(add)
                dyn.update(sites)
                loads |= ran
            if low in globs.get("commands", ()):
                enum.append((line, sc, _glob(globs, args)))
            loads |= low == "import-module" and (bool(computed) or not args)
            if form == "dot":
                refs.add(Ref(_path(name), "include", line, sc))
            elif form == "amp" and PATHISH.search(name):
                refs.add(Ref(_path(name), "exec", line, sc))
                top = top or (line if not sc else 0)
            elif low == "export-modulemember":
                exported.append((line, args))
                grand = grand or any(COMPUTED.search(c) for c in computed)
            else:
                refs.add(Ref(_fold(name), "call", line, sc))
                top = top or (line if not sc and low not in skip else 0)
                if low == "import-module":
                    for a in (x for x in args if x[:1] != "-"):
                        if SCRIPT_EXT.search(a):
                            refs.add(Ref(_path(a), "include", line, sc))
                        else:  # a module by name or by its directory
                            refs.update(_module_refs(a, line, sc))
            if low != "export-modulemember":
                refs.update(Ref(_fold(a), "ref", line, sc) for a in args if NAMEISH.match(a))
        elif kind == "member":
            name, static, recv, call = r[1].lower(), r[6], r[7], r[8]
            owner = recv.lstrip("$").lower() if recv else "?"
            refs.add(Ref(f"{owner}.{name}", "call" if call else "ref", line, sc))
            mode = sinks.get("members", {}).get(f"{owner}.{name}")
            if mode and len(r) > 10:
                add, sites, ran = _sink_sites(mode, _flat(r[9]), _flat(r[10]), line, sc)
                refs.update(add)
                dyn.update(sites)
                loads |= ran
            if static and name == "new" and recv:
                refs.add(Ref(owner, "call", line, sc))
        elif kind == "type":
            if _fold(r[1]) not in BUILTIN_TYPES:
                refs.add(Ref(_fold(r[1]), "ref", line, sc))
        elif kind == "using" and r[6] == "Module" and r[1]:
            refs.update(
                [Ref(_path(r[1]), "include", line, sc)] if SCRIPT_EXT.search(r[1]) else _module_refs(r[1], line, sc)
            )
        elif kind == "str":  # a script or a Verb-Noun command named by a string: used if the file runs a computed one
            if SCRIPT_EXT.search(r[1]):
                named.add(Ref(VAR.split(_path(r[1]))[-1].lstrip("/"), "include", line, sc))
            else:
                named.add(Ref(_fold(r[1]), "ref", line, sc))
        elif kind == "fvar":  # ${function:Name}: the function as a value (read, call through it, or replaced)
            refs.add(Ref(_fold(r[1]), "ref", line, sc))
        elif kind == "dyn":
            pattern, inc = _dyn(r[1], *(r[6:9] if len(r) > 8 else ("", "", [])))
            dyn.add(DynSite(pattern, line, sc))
            loads |= len(r) > 6 and r[6] in ("dot", "amp")
            refs.update(Ref(_path(p), "include" if r[6] == "dot" else "exec", line, sc) for p in inc)
        elif kind == "hash":
            key, vals = r[1].lower(), _flat(r[6])
            if key in EXPORT_KEYS:
                refs.update(Ref(_fold(v), "ref", line, "") for v in vals if v and v != "*")
            elif key in INCLUDE_KEYS:
                refs.update(Ref(_path(v), "include", line, "") for v in vals if v)
    # a computed command or path runs: what a Get-ChildItem enumerates, or a string that names a script or a command, may be it
    if loads:
        refs.update(Ref(g, "include", line, sc) for line, sc, g in enum if g)
        refs.update(named)
    funcs = {d.qual for d in defs if d.kind == "func"}
    names = set()
    if (
        grand
    ):  # `Export-ModuleMember -Function $Public.Basename`: like `-Function *` for this file, and the functions of the files its loops load
        exported.append((exported[0][0], ["*"]))
        dyn.update(DynSite(f"funcs:{g}"[:DYN_MAX], line, sc) for line, sc, g in enum if g)
    for line, args in exported:  # Export-ModuleMember -Function a, b (-Alias, -Cmdlet, -Variable name other things)
        mode = ""
        for a in args:
            if a.startswith("-"):
                mode = a[1:].lower()
            elif mode.startswith("func") or not mode:
                for q in sorted(funcs) if a == "*" else [_fold(a)]:
                    if q in funcs:  # a name defined in another file is a ref, never a root: no false E dc.root-dangling
                        names.add(q)
                        roots.append(RootHint("export", q, line, "soft", "convention"))
                    else:
                        refs.add(Ref(q, "ref", line, ""))
    defs = [d._replace(vis="exported") if d.qual in names else d for d in defs]
    if (
        top and not exported
    ):  # running the file runs its top-level statements; a module (Export-ModuleMember) is loaded, never run
        roots.append(RootHint("main", "", top, "soft", "convention"))
    xerr = 0
    try:
        refs.update(xlang.pwsh_refs(text, scope_of))  # $env:X and [Environment]::Get/SetEnvironmentVariable
    except (
        Exception
    ):  # noqa: BLE001 - a bug of the cross-language pass costs that pass (errors + 1), not the other facts
        xerr = 1
    for i, ln in enumerate(text.splitlines(), 1):  # `# deadcode: root <why>` on or just above a definition
        if "#" in ln and MARKER.search(ln[ln.index("#") :]):
            target = next((d.qual for d in sorted(defs, key=lambda d: d.line0) if d.line0 in (i, i + 1)), "")
            roots.append(RootHint("manual", target, i, "hard", "registered"))
    return Facts(
        "powershell",
        tuple(merge_defs([module_def(text), *defs])),
        tuple(sorted(refs)),
        (),
        tuple(roots),
        tuple(sorted(dyn)),
        int(reply.get("errors", 0)) + xerr,
    )


@lru_cache(maxsize=1)
def script_sha() -> str:
    h = hashlib.sha256()
    for path in (SCRIPT, RULES):  # the rules decide the facts as much as the worker does
        with open(path, "rb") as fh:
            h.update(fh.read() + b"\0")
    return h.hexdigest()


class Pwsh(Extractor):
    name, version, langs = (
        "pwsh",
        4,
        ("powershell",),
    )  # 2: env refs (xlang); 3: modules by name, computed Export-ModuleMember; 4: a top-level statement makes the file a script (soft main)

    def grammar_sha(self, lang: str) -> str:
        """The worker script decides the records, so its sha is part of the shard key (like the versions of a grammar)."""
        return hashlib.sha256(f"{script_sha()}|{xlang.sha()}".encode()).hexdigest()

    def probe(self, lang: str, cfg: Optional[dict] = None) -> Optional[Finding]:
        return None if _worker_fn is not None else missing_finding(lang, "(pwsh)", cfg)

    def extract(self, lang: str, path: str, text: str) -> Facts:
        return _facts(text, (_worker_fn or _real_worker)(text))


register_extractor(Pwsh())
