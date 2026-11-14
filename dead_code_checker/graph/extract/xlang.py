"""graph.extract.xlang: cross-language facts. Stdlib only, data in data/xlang.json.

Five kinds of fact, written by the extractors of every language that has rules here (python by `ast`, shell, go, js/ts, java, kotlin, c#, rust,
c/c++ by tree-sitter queries data/queries/<grammar>/xlang.scm, powershell by a text scan) and linked by graph.link / graph.xlink:
  exec  Ref(name, "exec")   a command a call or a shell line runs: `subprocess.run(["bash", "scripts/x.sh"])`, os.system, exec.Command,
        child_process.spawn/exec, ProcessBuilder, Runtime.exec, Process.Start, Command::new, system(); every shell command node. The line
        goes through graph.roots.command_targets, so interpreters, launchers and builtins are read as everywhere else: `python3 -u x.py` is
        x.py, `bin/tool` is bin/tool, `tool` is the file of that name (graph.link path_targets, T when one file). PowerShell `&` and
        Start-Process are the exec refs pwsh.py already writes.
  lit   Ref(path, "lit")    a string literal that spells a repo-relative path (data lit). Link: N edge to the file that the path names
        exactly (relative to the file or to the tree root); the strings an exec call uses are not written twice.
  ffi   Ref(symbol, "ffi")  a native symbol a file binds: python `lib.add` on a ctypes.CDLL / cffi dlopen handle, [DllImport] and
        [LibraryImport] methods (the symbol is EntryPoint or the method name), Java `native` methods (the JNI name Java_<package>_
        <Class>_<method>), cgo `C.fn` in a file with `import "C"`, a Rust `extern "C" { fn f(); }` block. Link: the top-level defs of that
        name in the exporting groups (data ffi_groups: c/c++, rust, go): T when one, else N. The exporting side needs no fact: a Rust
        `#[no_mangle] extern "C" fn` and a Go `//export` are roots already, a C function is a def.
  env   Ref("r:NAME" | "w:NAME", "env")   an environment variable (or `cfg:` configuration key: System.getProperty, viper) that the code reads
        or writes: os.getenv / os.environ[..], process.env.X, os.Getenv, System.getenv, Environment.GetEnvironmentVariable, env::var,
        getenv, $X and `export X=` of shell, $env:X of PowerShell. Link: N edge from the reader to the writers of the SAME name in
        ANOTHER language group, never within one language (that is the language's own call graph).
  build Ref("recipe:x" | "stage:x", "ref") / DynSite("name:recipe:<glob>")  the Makefile / justfile recipe or Dockerfile stage that a shell command
        line names (`make test`, `$(MAKE) -C sub all`, `just build`, `docker build --target dev`: graph.roots.build_targets, data
        build_tools; a computed word `make $T` is a dynamic site that reads every recipe it can name). Only the families that read whole
        command lines (shell). Link: the names of the config languages see the shell files through the link group cfg (langs.json).
  schema Ref("<group>:<name>", "schema")   a schema object the code names: the table a string literal that is a DML statement reads (`SELECT .. FROM
        orders o JOIN items i`, `INSERT INTO t`, a name built at run time is a glob: `sql:audit_*`, `sql:*`; families with `schema_sql` in the data; a statement
        written over several literals is one text: `"SELECT a " + "FROM b"`, an f-string, C `"a" "b"` (sql_fold, data concat; any other operand is a hole that
        a name makes a glob), a literal that starts with a clause (`FROM b`: what a StringBuilder appends) is read on its own), the
        object a call names (`schema.<group>` role: graphql-java `dataFetcher("books", ..)`), and, from graph.extract.lang_rules, the table a JPA @Table / @Entity
        maps and the field a Spring GraphQL / DGS mapping serves. The SQL text of a schema file goes through the same scan (sql_names, with the DDL words). Link: graph.xlink
        (names matched by the segments of the qualified name, N; the globs D); nothing here can make a finding either.
Tiers are the whole policy: exec is T (a command named in the code is run), lit / env / a non-unique ffi symbol are N. An N edge makes a
node weak-alive in graph.reach, never alive, so an env edge or a path string alone can only suppress a dead verdict, it never creates a
finding and a node whose only evidence is one stays out of the strong class (alive for sure) that errors would rest on.
Limits (documented, not hidden): the callee is matched as written (no alias tracking: `from subprocess import run` is not
`subprocess.run`), `Command::new("python3").arg("x.py")` names python3 only, a computed argument ends the literal run, a symbol looked up
by a computed name is no fact. The mapping reads text and nodes only (start_byte, end_byte, start_point), nothing is executed.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import posixpath
import re
import shlex
from functools import lru_cache
from typing import Iterable, List, NamedTuple, Optional, Tuple

from ..model import DynSite, Ref

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "xlang.json")
ENV_UPPER = re.compile(
    r"^[A-Z][A-Z0-9_]*$"
)  # a shell variable read or exported without an owner object: only the env convention
STR = r"""@?(?:"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`[^`]*`)"""  # a string token (C# verbatim @"..." too)
OPEN = re.compile(r"[\s(\[{]*")
WRAP = re.compile(r"(?:new\s+)?[\w.<>:]+(?:\[\])*\s*[(\[{]")  # Arrays.asList(  new String[]{  List.of(
ITEM = re.compile(r"\s*(" + STR + r")\s*(?=,|[\])}]|$)\s*,?")
VARDIR = re.compile(r"^(?:\$\{?\w+\}?|%\w+%)/")
SUBST = re.compile(r"\$\{[^}]*\}")
UNQ = re.compile(r"""^@?(?:"(.*)"|'(.*)'|`(.*)`)$""", re.S)
PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
CGO = re.compile(r'^\s*import\s+"C"|^\s*"C"\s*$', re.M)
PWSH_ENV = re.compile(
    r"\$env:([A-Za-z_]\w*)(\s*=(?!=))?|\[(?:System\.)?Environment\]::(Get|Set)EnvironmentVariable\(\s*['\"](\w+)", re.I
)
CALLEE_MAX = 200  # bytes between the start of a call and its argument list that still count as a callee
CMD_CUT = re.compile(r"(?<!\\)\n")


class Rule(NamedTuple):
    rx: "re.Pattern"
    role: str
    join: str
    skip: int


class Rules(NamedTuple):
    calls: Tuple[Rule, ...]
    any_rx: Optional["re.Pattern"]  # any call rule: one search tells a call that no rule reads
    env_objects: frozenset
    ffi_load: Optional["re.Pattern"]
    ffi_decl: Tuple[dict, ...]
    cmd: bool
    schema_sql: bool = False  # a string literal of the family that is a DML statement names tables (schema refs)
    concat: Optional[dict] = (
        None  # {node type: operator} of the tree-sitter nodes that join string operands (a statement over several literals)
    )


@lru_cache(maxsize=1)
def data() -> dict:
    with open(DATA, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=None)
def rules(family: str) -> Optional[Rules]:
    """The compiled rules of a language family (langs.json), None when the family has none."""
    f = data()["families"].get(family)
    if f is None:
        return None
    calls = f.get("calls", ())
    return Rules(
        tuple(Rule(re.compile(c["re"]), c["role"], c.get("join", "argv"), c.get("skip", 0)) for c in calls),
        re.compile("|".join(f"(?:{c['re']})" for c in calls)) if calls else None,
        frozenset(f.get("env_objects", ())),
        re.compile(f["ffi_load"]) if f.get("ffi_load") else None,
        tuple(f.get("ffi_decl", ())),
        bool(f.get("cmd")),
        bool(f.get("schema_sql")),
        f.get("concat"),
    )


@lru_cache(maxsize=1)
def sha() -> str:
    """sha256(xlang.json | the command-line data of frameworks.json | this file): all of it decides the facts (part of every shard key)."""
    from .. import roots
    fw = roots.fw()
    cmd = {
        k: sorted(fw[k]) if isinstance(fw[k], frozenset) else fw[k]
        for k in ("interpreters", "launchers", "builtins", "shells", "wrappers", "script_ext", "build_tools")
    }
    h = hashlib.sha256(json.dumps([data(), cmd], sort_keys=True).encode())
    with open(os.path.abspath(__file__), "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


# ---------------------------------------------------------------- literals and command lines
def literal(token: str) -> Optional[str]:
    """The text inside a string token (quotes, a C# verbatim `@`, escapes removed); None when it is no string."""
    t = token.strip()
    m = UNQ.match(t)
    if not m:
        return None
    body = next(g for g in m.groups() if g is not None)
    return body if t[0] == "@" else re.sub(r"\\([\\\"'`])", r"\1", body)  # a verbatim string keeps its backslashes


def path_like(s: str) -> str:
    """The repo-relative spelling of a string that names a path, else ''."""
    s = s.replace("\\", "/")
    d = data()["lit"]
    if not d["min"] <= len(s) <= d["max"] or not re.match(d["re"], s) or not ("/" in s or re.search(d["ext"], s)):
        return ""
    return s


def _skip(t: str, n: int) -> str:
    """`t` without its first `n` top-level arguments."""
    depth, quote = 0, ""
    for i, c in enumerate(t):
        if quote:
            quote = "" if c == quote and t[i - 1] != "\\" else quote
        elif c in "\"'`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            n -= 1
            if not n:
                return t[i + 1 :]
    return ""


def lead(args: str, skip: int = 0, subst: bool = False) -> List[str]:
    """The leading run of string literals of an argument list text (`("bin/tool", ["x"])`, `(ctx, "a")` with skip 1,
    `(new String[]{"a", "b"})`); it ends at the first argument that is no whole literal (`"a" + x`, a name). `subst`: a `${..}` of a
    template literal becomes `0` (a command line keeps its program), else a literal that has one is no name and ends the run.
    """
    t = args.strip()
    if skip:
        t = _skip(t[1:] if t[:1] == "(" else t, skip)
    pos = OPEN.match(t).end()
    if pos < len(t) and t[pos] not in "\"'`@":
        w = WRAP.match(t, pos)
        pos = OPEN.match(t, w.end()).end() if w else pos
    out: List[str] = []
    while True:
        m = ITEM.match(t, pos)
        s = literal(m.group(1)) if m else None
        if s is None:
            return out
        if "${" in s:
            if not subst:
                return out
            s = SUBST.sub("0", s)
        out.append(s)
        pos = m.end()


def exec_names(strs: List[str], join: str = "argv") -> List[str]:
    """The files a command (argv literals, or one command line) runs: what graph.roots.command_targets reads (a `file:` or `cmd:` selector)."""
    if not strs:
        return []
    from .. import roots
    line = strs[0] if len(strs) == 1 else (" ".join(strs) if join == "line" else " ".join(shlex.quote(s) for s in strs))
    out = []
    for sel in roots.command_targets(line):
        scheme, _, v = sel.partition(":")
        v = VARDIR.sub(
            "", v
        )  # `$HERE/gen.py`, `"${ROOT}"/bin/x`: the directory is a variable, the path below it is a suffix
        if scheme in ("file", "cmd") and v and not v.startswith(("/", "~")) and "$" not in v:
            out.append(v)
    return out


def rule_facts(r: Rule, lits: List[str]) -> List[Tuple[str, str]]:
    """[(ref kind, name)] of one rule that matched a call whose leading literals are `lits`."""
    if r.role == "exec":
        return [("exec", n) for n in exec_names(lits, r.join)]
    if r.role.startswith(
        "schema."
    ):  # the first literal names an object of the group: graphql-java dataFetcher("books", ..)
        return [("schema", f"{r.role[7:]}:{lits[0]}")] if lits else []
    key = lits[0].split("=")[0] if lits else ""
    if key and re.match(data()["name"], key):
        kind, _, ns = r.role.partition(".")
        return [("env", env_name(kind == "cfg", ns, key))]
    return []


def call_facts(rs: Rules, callee: str, args) -> List[Tuple[str, str]]:
    """exec and env facts of one call: `callee` is the text before the argument list, `args` a function that gives the list's text."""
    name = re.sub(r"\s+", "", re.sub(r"^new\s+", "", callee.strip()))
    if not (rs.any_rx and rs.any_rx.search(name)):
        return []
    text = args()
    return [f for r in rs.calls if r.rx.search(name) for f in rule_facts(r, lead(text, r.skip, r.role == "exec"))]


def env_name(cfg: bool, role: str, name: str) -> str:
    """`r:NAME` / `w:NAME` (role r read, w write); a configuration key (cfg) is `r:cfg:KEY`."""
    return f"{role}:{'cfg:' if cfg else ''}{name}"


def env_ref(cfg: bool, role: str, name: str, line: int, scope: str) -> Ref:
    return Ref(env_name(cfg, role, name), "env", line, scope)


def settle(refs: Iterable[Ref]) -> List[Ref]:
    """The refs without the lit that an exec ref of the same line and scope already names (T beats N, and one edge is enough)."""
    refs = list(refs)
    ex = {(r.line, r.scope, posixpath.normpath(r.name)) for r in refs if r.kind == "exec"}
    return [r for r in refs if r.kind != "lit" or (r.line, r.scope, posixpath.normpath(r.name)) not in ex]


def jni_name(pkg: str, cls: str, method: str) -> str:
    """Java_<package>_<Class>_<method> with the JNI escapes (`_` is `_1`, `.` is `_`, a nested class `$` is `_00024`)."""

    def esc(s: str) -> str:
        return "".join(
            "_1" if c == "_" else c if c.isascii() and c.isalnum() else "_" if c == "." else "_0%04x" % ord(c)
            for c in s
        )

    return "Java_" + esc((pkg + "." if pkg else "") + cls.replace(".", "$")) + "_" + esc(method)


# ---------------------------------------------------------------- sql (schema refs)
SQL_SKIP = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'", re.S)  # comments and string values: no names in them
SQL_PART = r'"[^"\n]+"|`[^`\n]+`|\[[^\]\n]+\]|[A-Za-z_]\w*(?:\$\w+)*'
SQL_NAME = re.compile(rf"(?:{SQL_PART})(?:\s*\.\s*(?:{SQL_PART}))*")
SQL_CTE = re.compile(
    r"(?:\bWITH(?:\s+RECURSIVE)?|,)\s*([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s+AS\s*(?:(?:NOT\s+)?MATERIALIZED\s*)?\(", re.I
)
SQL_HOLE = re.compile(r"[%{$:?@#<]")  # a placeholder where a name is expected (%s, {t}, ${t}, :t, ?, #{t}, <t>)
SQL_TAIL = re.compile(r"[%{$]")  # one right after the written part of a name: audit_%s, t_{kind}, p_${x}
HOLE = " {} "  # an operand of a concatenated statement that is no string (blanks around: a name before it is as written, `"FROM orders" + where`)


class Sql(NamedTuple):
    verb: "re.Pattern"
    clause: "re.Pattern"  # the start of a literal that is a part of a statement (`FROM orders`: what a StringBuilder appends)
    dml: "re.Pattern"
    ddl: "re.Pattern"
    call: "re.Pattern"
    sep: "re.Pattern"  # an alias and the comma of a FROM list
    cap: Tuple[int, int, int]  # the length of a literal that is read, of a statement over several operands


@lru_cache(maxsize=1)
def sql() -> Sql:
    d = data()["schema"]["sql"]
    alt = lambda k: "|".join(f"(?:{x})" for x in d[k])  # noqa: E731
    return Sql(
        re.compile(d["verb"], re.I),
        re.compile(d["clause"], re.I),
        re.compile(
            rf"\b(?:{alt('dml')})(?:\b|(?<=\W))\s*", re.I
        ),  # a keyword, not the start of a word (`updated_at`, `joined`)
        re.compile(rf"\b(?:{alt('ddl')})(?:\b|(?<=\W))\s*", re.I),
        re.compile(rf"\b(?:{alt('call')})\s+", re.I),
        re.compile(rf"(?:\s+(?:AS\s+)?(?!(?:{'|'.join(d['stop'])})\b)\w+)?\s*,\s*", re.I),
        (d["min"], d["max"], d["fold_max"]),
    )


def sql_clean(name: str) -> str:
    """A SQL name as written without its quotes and blanks: `"Audit" . "log"` -> `Audit.log`."""
    return re.sub(r'\s+|["`\[\]]', "", name)


def _names(s: str, pos: int, many: bool) -> List[str]:
    """The names that start at `pos` of cleaned SQL (a FROM list: several, each with its alias): a plain name, a name with a computed
    tail (`audit_%s`, `t_` before a `+`) is the glob `audit_*`, a computed name or a keyword that ends the text is `*`.
    """
    out: List[str] = []
    while True:
        m = SQL_NAME.match(s, pos)
        if m is None:
            if not s[pos:].strip() or SQL_HOLE.match(s, pos):
                out.append("*")
            return out
        name = sql_clean(m.group())
        out.append(name + "*" if name.endswith("_") or SQL_TAIL.match(s, m.end()) else name)
        sep = sql().sep.match(s, m.end()) if many else None
        if sep is None:
            return out
        pos = sep.end()


def sql_names(text: str, ddl: bool = False) -> List[str]:
    """The tables and routines that SQL text names, as written (`orders`, `public.orders`; a name built at run time is a glob). After
    FROM, JOIN, INTO and UPDATE (data schema.sql.dml; a FROM list gives every table), after REFERENCES, ALTER TABLE and the other words that
    a schema file has too (ddl), after CALL / PERFORM / EXECUTE FUNCTION. The names of a WITH are no tables."""
    s, q = SQL_SKIP.sub(" ", text), sql()
    cte = {m.group(1).lower() for m in SQL_CTE.finditer(s)}
    out: List[str] = []
    for rx in (q.dml, q.ddl, q.call) if ddl else (q.dml, q.call):
        for m in rx.finditer(s):
            out += _names(s, m.end(), m.group().strip().upper() == "FROM")
    return sorted({n for n in out if n.lower() not in cte})


def sql_text(s: str, fold: bool = False) -> List[str]:
    """The schema refs (`sql:<name>`) of the text of a string of code that is a DML statement (`SELECT ..`, `insert into ..`, `WITH ..`, `CALL ..`) or
    the part of one that starts with a clause (`FROM orders o`, `LEFT JOIN items`); [] for any other string. A fully computed name (`"SELECT * FROM " + t`)
    counts for an upper-case statement or one with a `*`, `=` or `(` only: a lower-case sentence that ends in `from` is no query.
    """
    q = sql()
    if not q.cap[0] <= len(s) <= q.cap[2 if fold else 1] or not (v := q.verb.match(s) or q.clause.match(s)):
        return []
    shaped = v.group().strip().isupper() or re.search(r"[*=(]", s)
    return [f"sql:{n}" for n in sql_names(s) if n != "*" or shaped]


def sql_body(token: str) -> Optional[str]:
    """The text of a string token as written (quotes, a Java / C# text block with three quotes, a backtick string, a verbatim string)."""
    t = token.strip()
    return t[3:-3] if t.startswith('"""') else literal(t)


def sql_literal(token: str) -> List[str]:
    """sql_text of a string token."""
    s = sql_body(token)
    return [] if s is None else sql_text(s)


def sql_fold(parts: List[Optional[str]]) -> List[str]:
    """sql_text of a statement that is written over several operands (`"SELECT a " + "FROM b"`, `f"SELECT {c} FROM b"`): the string operands are its
    text (None is any other operand: a name, a call, a `?:`), every other operand a hole that a name there makes a glob (`{}` between blanks, so `"FROM orders" + where` names orders); holes before the first
    literal are no part of the statement (`hint + "SELECT .."`)."""
    first = next((i for i, p in enumerate(parts) if p is not None), len(parts))
    return sql_text("".join(HOLE if p is None else p for p in parts[first:]), True)


# ---------------------------------------------------------------- tree-sitter
def _operands(n) -> list:
    return [c for c in n.named_children if "comment" not in c.type]


def _joins(n, ops: dict) -> bool:
    """Does the tree-sitter node join string operands (data concat: `a + b` of binary_expression, the juxtaposition `"a" "b"` of C)?"""
    want = ops.get(n.type)
    if want is None:
        return False
    op = n.child_by_field_name("operator") or next(
        (c for c in n.children if not c.is_named), None
    )  # (kotlin: the first token)
    return not want or (op is not None and op.type == want)


def concat_chains(ops: dict, lits: dict, data_b: bytes) -> Tuple[dict, dict]:
    """The statements that are written over several operands: ({(start, end) of a literal: the chain it is a part of}, {chain: (its node, its parts)});
    a chain is the outermost node that joins operands (see _joins, parentheses are looked through) and holds a literal, its parts the string bodies and
    None for any other operand, in order. `lits` is {(start, end): node} of the string literals of the tree. One climb per node (memo) and no recursion:
    a chain of thousands of literals costs a pass."""
    top: dict = {}
    of: dict = {}
    nodes: dict = {}
    for key, n in lits.items():
        path, p, root = [], n, None
        while True:
            q = p.parent
            while q is not None and q.type == "parenthesized_expression":
                q = q.parent
            if q is None or not _joins(q, ops):
                break
            k = (q.start_byte, q.end_byte, q.type)
            if k in top:
                root = top[k]
                break
            path.append(k)
            root, p = k, q
            nodes[k] = q
        for k in path:
            top[k] = root
        if root is not None:
            of[key] = root
    parts: dict = {}
    for root in sorted(set(of.values())):
        out, stack = [], [nodes[root]]
        while stack:
            x = stack.pop()
            if _joins(x, ops) or x.type == "parenthesized_expression":
                stack.extend(reversed(_operands(x)))
            elif (x.start_byte, x.end_byte) in lits:
                out.append(sql_body(data_b[x.start_byte : x.end_byte].decode("utf-8", "replace")))
            else:
                out.append(None)
        parts[root] = (nodes[root], out)
    return of, parts


def build_refs(line_text: str, line: int, scope: str) -> Tuple[List[Ref], List[DynSite]]:
    """The recipe / stage refs and the dynamic sites of one command line (see the kind `build` above)."""
    from .. import roots
    names = roots.build_targets(line_text)
    return (
        [Ref(q, "ref", line, scope) for q in names if "*" not in q],
        [DynSite(f"name:{q}", line, scope) for q in names if "*" in q],
    )


def ts_refs(family: str, text: str, data_b: bytes, parsed, line, scope_of) -> Tuple[List[Ref], List[DynSite]]:
    """(refs, dynamic sites) of one file from the matches of its xlang.scm. Capture vocabulary: @lit (a string node); @call + @args (a
    call node and its argument list node: the callee is the text between them); @cmd (a shell command node); @env.r / @env.w with @name and, for a member
    or subscript, @obj (kept when the object text is one of the rule's env_objects; none = shell, upper-case names only); @ffi.decl +
    @name (a declaration that binds a native symbol, see data ffi_decl), @ffi.use + @obj + @name (cgo `C.fn`)."""
    rs = rules(family)
    if rs is None:
        return [], []

    def txt(n) -> str:
        return data_b[n.start_byte : n.end_byte].decode("utf-8", "replace")

    matches = [caps for _, caps in parsed.run("xlang")]
    of, chains = {}, {}
    if rs.schema_sql and rs.concat:
        lits = {(n.start_byte, n.end_byte): n for caps in matches for n in caps.get("lit", ())}
        of, chains = concat_chains(rs.concat, lits, data_b)
    written = {(n.start_byte, n.end_byte) for caps in matches for n in caps.get("env.w", ())}
    env_b = frozenset(o.encode() for o in rs.env_objects)
    cgo = bool(CGO.search(text))
    pkg = (PACKAGE.search(text) or [None, ""])[1]
    out: List[Ref] = []
    dyn: List[DynSite] = []
    for caps in matches:
        for n in caps.get("lit", ()):
            s = literal(txt(n))
            name = path_like(txt(n) if s is None else s)  # a shell word is no quoted token
            if name:
                out.append(Ref(name, "lit", line(n), scope_of(n.start_byte)))
            if rs.schema_sql and (n.start_byte, n.end_byte) not in of:  # a part of a chain is read with the whole
                out += [Ref(x, "schema", line(n), scope_of(n.start_byte)) for x in sql_literal(txt(n))]
        for n in caps.get("cmd", ()):
            first = CMD_CUT.split(txt(n).replace("\\\n", " "), 1)[0]
            out += [Ref(x, "exec", line(n), scope_of(n.start_byte)) for x in exec_names([first], "line")]
            if rs.cmd:
                bref, bdyn = build_refs(first, line(n), scope_of(n.start_byte))
                out += bref
                dyn += bdyn
        for n in caps.get("call", ()):
            for a in caps.get("args", ())[:1]:
                if a.start_byte - n.start_byte > CALLEE_MAX:  # a long call chain is no callee of the rules
                    continue
                callee = data_b[n.start_byte : a.start_byte].decode("utf-8", "replace")
                for kind, name in call_facts(rs, callee, lambda: txt(a)):
                    out.append(Ref(name, kind, line(n), scope_of(n.start_byte)))
        for role in ("r", "w"):
            for n in caps.get(f"env.{role}", ()):
                nm, ob = caps.get("name"), caps.get("obj")
                if not nm or (ob and data_b[ob[0].start_byte : ob[0].end_byte].replace(b" ", b"") not in env_b):
                    continue  # (a cheap test first: every member expression of a js file matches the pattern)
                if role == "r" and (n.start_byte, n.end_byte) in written:
                    continue
                obj = txt(ob[0]).replace(" ", "") if ob else ""
                key = literal(txt(nm[0]))
                key = txt(nm[0]) if key is None else key
                ok = obj in rs.env_objects if obj else bool(ENV_UPPER.match(key))
                if ok and re.match(data()["name"], key):
                    out.append(env_ref(False, role, key, line(n), scope_of(n.start_byte)))
        for n in caps.get("ffi.decl", ()):
            nm = caps["name"][0]
            decl, scope = txt(n)[:600], scope_of(nm.start_byte)
            for rule in rs.ffi_decl:
                if not re.match(rule["re"], decl):
                    continue
                entry = re.search(rule["entry"], decl) if rule.get("entry") else None
                sym = entry.group(1) if entry else txt(nm)
                if rule.get("jni"):
                    cls = scope.rpartition(".")[0]
                    sym = jni_name(pkg, cls, sym) if cls else ""
                if sym:
                    out.append(Ref(sym, "ffi", line(nm), scope))
                break
        for n in caps.get("ffi.use", ()):
            if cgo and caps.get("obj") and txt(caps["obj"][0]) == "C" and caps.get("name"):
                out.append(Ref(txt(caps["name"][0]), "ffi", line(n), scope_of(n.start_byte)))
    for n, parts in chains.values():  # the statements written over several literals: one text, at the line of its start
        out += [Ref(x, "schema", line(n), scope_of(n.start_byte)) for x in sql_fold(parts)]
    return settle(out), dyn


# ---------------------------------------------------------------- python (ast)
def _dotted(n) -> str:
    """`a.b.c` of a name chain; a call in the chain is `f()` (`FFI().dlopen`); '' for any other expression."""
    parts = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if isinstance(n, ast.Call) and _dotted(n.func):
        n = ast.Name(id=_dotted(n.func) + "()")
    return ".".join([n.id, *reversed(parts)]) if isinstance(n, ast.Name) else ""


def _lits(args, skip: int) -> List[str]:
    """The leading run of constant strings of call arguments: plain positional ones, then the elements of a list or tuple."""
    out: List[str] = []
    for a in args[skip:]:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            out.append(a.value)
            continue
        if isinstance(a, (ast.List, ast.Tuple)):
            for e in a.elts:
                if not (isinstance(e, ast.Constant) and isinstance(e.value, str)):
                    break
                out.append(e.value)
        break
    return out


def _py_joins(n) -> bool:
    return isinstance(n, ast.JoinedStr) or (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add))


def _py_parts(root) -> list:
    """The operands of a statement written as `a + b + c` or an f-string, in order: the str Constant nodes and None for any other operand (`"..".format(..)`
    and `".." % ..` are their format string)."""
    out, stack = [], [root]
    while stack:
        x = stack.pop()
        if isinstance(x, ast.BinOp) and isinstance(x.op, ast.Add):
            stack += [x.right, x.left]
        elif isinstance(x, ast.JoinedStr):
            stack.extend(reversed(x.values))
        elif isinstance(x, ast.BinOp) and isinstance(x.op, ast.Mod):
            stack.append(x.left)
        elif isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) and x.func.attr == "format":
            stack.append(x.func.value)
        else:
            out.append(x if isinstance(x, ast.Constant) and isinstance(x.value, str) else None)
    return out


class PyScan:
    """What a python file says to other languages (see the module docstring): feed it the call, attribute, subscript and constant nodes
    the extractor walks, then take finish()."""

    def __init__(self, tree):
        self.rs = rules("python")
        self.out: List[Ref] = []
        self.sql: dict = (
            {}
        )  # id(str Constant of a statement written over several operands) -> its schema names (the first literal has them all)
        self.handles = set()  # dotted names bound to a native library handle: `lib`, `self.lib`
        joins = []
        for n in ast.walk(tree):
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(n.value, ast.Call) and self._loads(n.value):
                self.handles.update(
                    filter(None, (_dotted(t) for t in (n.targets if isinstance(n, ast.Assign) else [n.target])))
                )
            elif self.rs.schema_sql and _py_joins(n):
                joins.append(n)
        inner = {id(c) for n in joins if isinstance(n, ast.BinOp) for c in (n.left, n.right) if _py_joins(c)}
        for n in joins:
            parts = [] if id(n) in inner else _py_parts(n)
            lits = [p for p in parts if p is not None]
            if (
                len(parts) > 1 and lits and id(lits[0]) not in self.sql
            ):  # one statement: its first literal carries it, the others are read through it
                self.sql.update((id(p), []) for p in lits)
                self.sql[id(lits[0])] = sql_fold([None if p is None else p.value for p in parts])

    def _loads(self, call) -> bool:
        return bool(self.rs.ffi_load and self.rs.ffi_load.search(_dotted(call.func)))

    def call(self, n, scope: str) -> None:
        name = _dotted(n.func)
        if not (name and self.rs.any_rx and self.rs.any_rx.search(name)):
            return
        for r in self.rs.calls:
            if r.rx.search(name):
                self.out += [Ref(x, k, n.lineno, scope) for k, x in rule_facts(r, _lits(n.args, r.skip))]

    def attribute(self, n, scope: str) -> None:
        """`lib.add` on a library handle (or on the call that loads it): the native symbol add."""
        v = n.value
        if _dotted(v) in self.handles or (isinstance(v, ast.Call) and self._loads(v)):
            self.out.append(Ref(n.attr, "ffi", n.lineno, scope))

    def subscript(self, n, scope: str) -> None:
        """os.environ["X"] read (Load) or written (Store)."""
        s = n.slice
        if _dotted(n.value) in self.rs.env_objects and isinstance(s, ast.Constant) and isinstance(s.value, str):
            if isinstance(n.ctx, (ast.Load, ast.Store)) and re.match(data()["name"], s.value):
                self.out.append(env_ref(False, "r" if isinstance(n.ctx, ast.Load) else "w", s.value, n.lineno, scope))

    def const(self, n, scope: str) -> None:
        if isinstance(n.value, str):
            name = path_like(n.value)
            if name:
                self.out.append(Ref(name, "lit", n.lineno, scope))
            if self.rs.schema_sql:
                names = self.sql.get(id(n))
                self.out += [Ref(x, "schema", n.lineno, scope) for x in (sql_text(n.value) if names is None else names)]

    def finish(self) -> List[Ref]:
        return settle(self.out)


# ---------------------------------------------------------------- powershell (text)
def pwsh_refs(text: str, scope_of) -> List[Ref]:
    """`$env:X` and [Environment]::Get/SetEnvironmentVariable of a script; `scope_of((line, col))` is the definition around."""
    out: List[Ref] = []
    for i, ln in enumerate(text.splitlines(), 1):
        if ln.lstrip().startswith("#"):
            continue
        for m in PWSH_ENV.finditer(ln):
            name = m.group(1) or m.group(4)
            write = bool(m.group(2)) or (m.group(3) or "").lower() == "set"
            out.append(env_ref(False, "w" if write else "r", name, i, scope_of((i, m.start()))))
    return out
