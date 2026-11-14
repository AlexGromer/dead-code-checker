"""graph.link: resolve the per-file Facts into the symbol graph. Standard library only.

    res = link(files, root_hints=None, workers=None)   files: {path: Facts};  res: Linked(graph, amb, findings)
    res.graph      model.Graph (canonical: the same input gives the same Graph.digest() for any worker count)
    res.amb        node keys that an over-wide name match (> AMB_MAX candidates) points at
    res.findings   E dc.root-dangling (a root selector or qual that is no node), E dc.bad-facts (an extractor wrote an
                   invalid kind: the fact is skipped, never fixed up)

Nodes: one `file` node per file (lang:path; the module Def, qual "", is that node: it only widens the line range) and one node per
Def (key = model.node_key, `~h8` for equal qual+arity). Flags
from the path (test, generated; vendor = generated) are added here, never by the extractor; link adds protocol (dunder and
PROTOCOL_NAMES, a constructor or destructor `Foo.Foo` / `Foo.~Foo` of a non-python class), override (a method that overrides an ancestor's) and dynamic-zone (target of a D edge), entry-registered.

Edges and tiers (a reference that resolves to nothing in the project adds no edge: it is external):
  T  scope chain (innermost out; a family with "members_by_receiver" in langs.json, python and go, skips class scopes seen from a nested def:
     the members of a type are named through a receiver, so a bare `renderPage` in a method is the type of that name), self/this/cls/super members through the
     inheritance chain, import bindings (import X [as Y], from X import n [as m], relative levels, star), the includes
     closure (c/cpp/shell/powershell see the defs of included files), `exec`/`include` path refs to files of ANY language (a
     `*`, `**` or `?` in the name expands to every file it matches, all T, relative to the including file or the tree root:
     GitLab `include: local: '/ci/*.yml'`).
  N  name match, edges go to ALL candidates of the language family: bare name -> top-level defs of that name, `x.m` with x
     unknown -> members named m; a named import adds an N edge to the imported def (a re-export may be its only use); js: a name the imported file does not
     define is looked up in the files it re-exports with `export * from m`; a bare module binding also reaches the `default` def,
     and when it is used as a value (passed, returned, stored: kind ref) every export of the module (N): the namespace escaped;
     js: a name that a file of the tree exports but that is no def (`export const api = new ApiClient()`, a re-export through a barrel,
     `const { api } = require(m)`) is a value read through its members (`api.get()`: members named get, N); `Foo.create()` on a module
     value (`const Foo = require(m)` of `module.exports = Foo`, `import svc from m` of `export default new S()`) that no top-level def of m
     answers reaches the `default` def (T) and the members of that name (N); Kotlin (langs.json implicit_receiver): a bare name that nothing
     answers (`bar()` in `fun Foo.baz()` or in an apply / with / builder lambda) is also a member of that name (N);
     js: the def `*` of a barrel (`export * from m`) reaches every export of m (T), so an entry root that seeds the exports of the
     barrel keeps the whole re-exported API alive (the edges of a re-export carry the evidence rule `reexport`: `export * from`, the hidden
     aliases `__re_<name>` of `export {a} from m` / `export * as ns` / an import that is exported, `__ns<n>` of a module value nothing
     binds; graph.reach follows them from a package entry and seeds the public members of the exported classes it finds); a specifier that starts with an alias marker (`@/x`, `~/x`, `#x`, `$lib/x`,
     `@org/ui`) is looked up by what follows the marker as a path suffix (no tsconfig is read: an over-approximation). A path specifier
     (`./x`, `../x`, `.`, `..`, with or without a trailing slash or a `?query`) names the file spelled, else the file with that stem (only a
     source extension of the family is dropped: `./foo.utils` is foo.utils.*, never foo.*), else the index of a directory of that name;
     a file wins over a directory (Node), the project root is the stem ''.
  D  DynSite wildcards, pattern = "<kind>[:arg]":  getattr|attr|members[:Type[/glob]] -> members of Type (only those whose name matches
     the glob: `members:Mw/Invoke*`, a host that calls a middleware by convention), else of the enclosing
     class, else every def of the file;  pkg:<prefix> -> classes under the path/package prefix;  name:<glob> -> defs of
     the family by last name (the classes that enclose a nested class of that name too: `Class.forName("p.Outer$Inner")`);  module:<name or glob> -> a module loaded by
     name (python importlib / spec_from_file_location, `pkg.probe_*` for a discovery loop): the file and its top-level defs;  import:<module> -> the module file, import:<glob> (js, `*`) -> each file the glob names with its
     exports (`./l/*.js` from the file, `/x/*` from the project root, `*/x/*.js` at any depth, `*` everything under the directory
     of the file: a computed require(name) names nothing else);  funcs:<glob> -> every function of the files the include glob names
     (powershell, a computed Export-ModuleMember);  static:<Type> -> every member of Type (a type the project lacks: none;
     c# `using static`);  implicit:<Type> -> the members of Type that carry the implicit flag (Scala `implicit` and `given`: an
     `import app.Imp._`, `.given`, `.{a, b}` or `.a` brings them into scope, they are used without being named: N, nothing else of
     Type is touched);  file|cmd|$cmd|anything else -> funcs of the file.
  Cross-language edges (graph.extract.xlang has the data, the extractors write the facts, graph.xlink resolves them; refs of kind lit | ffi | env): exec refs of any language
     are the exec edges above (a program or script that a call or a shell line runs, T); lit = a string that spells the path of a file (N,
     the file named exactly); ffi = a native symbol (ctypes / cffi handle, DllImport, JNI `native`, cgo `C.f`, a Rust extern "C" block) ->
     the top-level defs of that name in c/c++, rust, go (T for one candidate, else N); env = an environment variable or configuration key
     read in one language -> the code that writes it in another (N). N means weak-alive in graph.reach: these edges only suppress a dead
     verdict, they never create a finding.
  schema = a table, a proto message or service, a GraphQL type or field that code or another schema file names (graph.xlink: the schema files
     of graph.extract.schema; N from code, T between schema files, a name built at run time D); generated proto stubs (FooGrpc, foo_pb2) and
     the members of a resolver class are N edges to the objects they serve.
  A config file (role config in langs.json) is alive while one of its entries (cfgkey, resource, job ...) is: entry -> file (T).
  A file of a language with `readers_dir` (helm-values: templates) and no such directory beside it is read from outside the tree
  (a values override for an upstream chart): a soft root on the file and N edges file -> its cfgkeys, so nothing in it is dead.
  A `reads` ref names a config key (cfgkey) by its exact qual, or every key below a prefix when the name ends with '*' (N edges).
  T  also: a top-level def of the same directory in a package language (langs.json package_dir: java, kotlin), seen without an import;
     the facade class of a Kotlin file (Util.kt -> UtilKt, langs.json file_facade) is the file for the code of the other language.
  A language group (langs.json link_group: java and kotlin are "jvm") shares the tables: one language sees the declarations of the other.
  package_loose (Kotlin: the package need not match the directory): a named import that no directory explains is bound by the name of
  the top-level declaration, never to "external".
  S  is never produced here (the SCIP overlay of the semantic layer).
  di edges: a ref of kind di (a field, constructor or @Bean parameter that injects type T) goes to T and to every bean (a class that
  has a root of a kind marked "bean" in data/frameworks.json) that is T or inherits from it: T when it is the only bean, else N.
Holder edge (families whose langs.json entry says "holder_edge": csharp, cpp, powershell): member -> the type that holds it (T; N when
the type is a name that several types of the family carry, as for `Foo::run` written in a .cpp), so a live member keeps its class alive
(`static class Program { static void Main() }` is alive through Main, a nested class keeps its outer class).
Type scope (families whose langs.json entry says "type_scope": csharp, cpp): a bare name written in a member is `this.name` for the C#/C++
reader, so it also denotes a member of the enclosing type, of every other declaration of that type (a C# partial part, the class of a header
when the member is written in a .cpp) and of its ancestors (`Helper()` of a base class); a type of the same name stays used too (`Address
Address`). A member that is written twice (declared in the class of a header, defined as `Foo::run` in a .cpp) is one member: its two
nodes keep each other alive (rule decl). A reference on a receiver the text does not name, `new B().Run()`, `a.F().G`, `x?.m` (the extractor
writes `?.m`), is a member named m (N), never a top-level def.
Namespace-less families (langs.json "namespaceless": csharp, cpp): namespaces are no defs, so the head of a dotted name that no scope knows
(`util::helper`, `Ns.Util`) is a namespace and the last part is the name: a member of that name or a top-level def of it, whether or not the
extractor also wrote the bare last identifier.
A module declared inline (`mod m { .. }`, def kind module) is a module of its own file: an import whose module no file is named
after binds the names inside it (a glob binds its children), in the importing file from its scope outward, else in any file that
declares it; a name of a crate / self / super path that nothing defines (a re-export) is matched by its name (N), never external.
A crate (langs.json "crate": rust) is a package directory whose src/ holds lib.rs (or only main.rs: a binary has no name
to import), named like its directory: a path that starts with that name (`use util::net::conn`, `util::other()`) is read
inside that crate, from src/lib.rs down. The name of a package is in its Cargo.toml, which is not read here: in a repo with
a library crate every other head of a `use` that is no toolchain crate (std, core ...) is taken for a crate of the project
that is named otherwise (the package at the top of the repo, a directory named differently, a rename) and the imported
names are matched (N); a path that goes on into a module file of a library crate (`use mycrate::cli;`, `mycrate::cli::parse()` in
src/main.rs, tests/ or examples/) is read inside the library crates (lib_path); without a use, and for the crate root alone, the
head may be a value (`x.f()`), so those are N. A repo with no library crate (only binaries) has no such crate, its other heads are external.
crate / self / super paths prefer the files of the importing file's own package. A name bound twice in one scope by the
same kind of import (`#[cfg(unix)] mod imp;` and `#[cfg(windows)] mod imp;`) keeps every target.
A member whose type is no def of its own file (a go method, a rust impl in another file or in another inline module:
`mod b { impl super::Foo { .. } }`) joins the type of that name in its directory, else any type of that name of the family;
a protocol one with no such type (`impl Tr for f64`) is owned by its file.
Build twins (langs.json "build_twins": go): the defs of one name in two files of a directory are one symbol, because only a build constraint (`plat_windows.go`
beside `native.go`, `//go:build`) lets them coexist: each keeps the other alive (T, rule buildtag), tests excepted (an external test package may repeat a name).
Copy twins (langs.json "copy_twins": python): a file that sits under another root with the same relative path and the same Facts as another file
(`staging/overlay/lib/pkg/mod.py` beside `pkg/mod.py`: a deploy payload, an overlay, a vendored copy) is the same module, because which of the two a dotted
import loads depends on sys.path, which the tree does not say: the defs of one qual keep each other alive (N, rule copytwin, the file nodes too).
Implicit edges: top-level def -> its file (T) for a language with file_init in langs.json (Kotlin: the initializers of the top-level
properties and the top-level statements of a file run when its facade class loads, i.e. when any declaration of it is used), and the
file -> each of its top-level var/const defs (the property initializers run with the file, nothing needs to name the property);
class -> its protocol/dunder members (T); class -> every member when it has a base that resolves to nothing
and is not a builtin (N, rule external-base); base.m -> sub.m (override, T) and sub.m -> base.m (impl, T) when base.m is
abstract or in an iface; class -> base for inherit/impl refs (Ref.scope is the DERIVED class qual, the base name is seen
from its parent scope). A scope that is no def (a lambda) falls back to the nearest enclosing def, else to the file node.

The parts of a partial class (and every other declaration of a type, type_scope families) are one type: they share their bases (a part without
a base list of its own is as external as the part that has one) and a root on one part roots all of them.
Inherited roots (langs.json "root_inherit": the root kinds a subclass inherits; csharp: annotation:dotnet-host): the host that builds a class by its
base (`ControllerBase`, `MonoBehaviour`, `IHostedService`) builds every class derived from it, however many levels down, so each class that has
such a rooted class (or interface) among its ancestors is rooted too, with the kind, strength and origin of that root.

Annotation inheritance (lang_rules.inherit): a ref of kind reg is the use of an annotation by the def that carries it; a project annotation
type (a hint annotation:<rule>.meta, see extract.lang_rules) hands the rules of its own annotations to every def that uses it, through
a chain of such types: @Facade on a class whose type carries @Service roots the class like @Service (a derived root and, for a rule
marked "bean", a DI bean). The annotation type itself is no root.
Roots (Facts.roots and root_hints[path], the virtual shards of the root extractors): target = selector "file:<path>",
"node:<key>", "cmd:<name>" (a file named <name>, preferably under bin/), "build:<qual>" (the recipe / stage nodes of that qual, a doc or
a Dockerfile named it; none is no finding), "self:" (the file that registers it: a hint that is
the same for every path, so a shard keyed by the blob serves two copies of one file) or, in a code file, a qual of that file ("" = the file). A hint on a
def also roots its file (running a script executes its module level). Each Root carries the node (or, when dangling, the
selector) and origin "path:line"; the target node gets F_ENTRY.

link_paths(files, paths) -> Partial(nodes, edges, findings): the index over ALL files but the edges of `paths` only (a file's edges are a
function of its own facts and the index): the base side of a branch diff is the changed files at their previous version.

Parallel: the index (def tables, module index, flags) is one serial reduction; the per-file work (import bindings and base
classes, then edge emission) runs through par.run_units in chunks of >= min_chunk work items. The result does not depend
on the chunking: a file is a pure function of (its Facts, the index) and canonical() orders the output.
"""

from __future__ import annotations

import fnmatch
import functools
import hashlib
import json
import posixpath
import re
from bisect import bisect_left
from collections import Counter
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from . import langs as _langs
from . import par
from . import roots as _roots
from . import xlink
from .extract import lang_rules
from .extract.common import generated_path
from .extract.lang_rules import bean_kinds, pkg_dir
from .model import (
    ALIAS_MARKS,
    EDGE_KINDS,
    F_ABSTRACT,
    F_DYNAMIC,
    F_ENTRY,
    F_GENERATED,
    F_IMPLICIT,
    F_OVERRIDE,
    F_PROTOCOL,
    F_TEST,
    NS_ALIAS,
    RE_ALIAS,
    REEXPORT_RULE,
    Edge,
    Evidence,
    Facts,
    Finding,
    Graph,
    Node,
    canonical,
    evidence,
    node,
    node_key,
    root,
    selector_scheme,
    sort_findings,
)

# part of the key of a stored graph (sha256(shard keys | roots | LINKER_V)): the version of this code and the sha of the data it reads
# (frameworks.json test_dirs / test_names flag a path as test)
# 12: copy twins; 13: the plugin-class roots only for a plugin file (py_class_roots scope), the script statements of a JS/TS package index are no
# root, Go members:T keeps the exported members of a declared type only
LINKER_V = (
    "13."
    + hashlib.sha256(
        json.dumps(
            [
                _roots.fw()["test_dirs"],
                _roots.fw()["test_names"],
                xlink.rules(),
                _roots.fw()["py_class_roots"].get("scope"),
            ],
            sort_keys=True,
            default=sorted,
        ).encode()
    ).hexdigest()[:8]
)
AMB_MAX = 8


@functools.lru_cache(maxsize=1)
def _class_root_kinds() -> frozenset:
    """The root kinds of data/frameworks.json py_class_roots rules (annotation:<id>): a framework class that is built by its name."""
    return frozenset("annotation:" + r["id"] for r in _roots.fw()["py_class_roots"]["rules"])


def _plugin_scope(path: str, facts) -> bool:
    """A file in which a py_class_roots class is a plugin of its framework: a directory of its path matches `scope` dirs (filter_plugins/,
    plugins/), or it imports a module of `scope` imports (ansible.*). Nothing else: a class named ActionModule with a run() method in any
    other file is no Ansible action plugin (the extractor sees only the text and cannot tell)."""
    sc = _roots.fw()["py_class_roots"].get("scope") or {}
    if any(fnmatch.fnmatchcase(d, g) for d in path.split("/")[:-1] for g in sc.get("dirs", ())):
        return True
    tops = frozenset(sc.get("imports", ()))
    return any(i.level == 0 and i.module.partition(".")[0] in tops for i in facts.imports)


MIN_CHUNK = (
    400_000  # work items (defs + refs + imports + dyn sites) per chunk; below one chunk runs without a process pool.
)
# Measured on a repository of 1900 files (~420k items, 660k edges): serial 17 s CPU; 2, 4 and 8 process workers cost 25-40 s CPU and
# more wall time (pickling of the facts in, of the 700k edges out). The pool starts to pay for trees ~5x bigger.
SELF_NAMES = ("self", "this", "cls", "super", "Self", "base", "$this")
CLASSY = ("class", "iface")
INDEXISH = ("__init__", "index", "mod")  # a file with this stem also denotes its directory (package)
RELATIVE_ROOTS = ("crate", "self", "super")  # a path that starts with one names a module of this crate
INITS = ("var", "const")  # top-level defs whose initializer code runs with the file (file_init languages)
# includes make the defs of the included file visible (cfg: the link group of shell)
GLOBAL_FAMS = ("cpp", "shell", "powershell", "cfg")
EXEMPT_BASES = {"object", "ABC", "Generic", "Protocol", "NamedTuple", "TypedDict", "Enum", "Exception", "BaseException"}
PROTOCOL_NAMES = {
    "String",
    "Error",
    "ServeHTTP",
    "Len",
    "Less",
    "Swap",
    "Read",
    "Write",
    "Close",
    "Unwrap",
    "MarshalJSON",
    "UnmarshalJSON",
    "toString",
    "equals",
    "hashCode",
    "compareTo",
    "Dispose",
    "GetHashCode",
    "Equals",
    "ToString",
    "constructor",  # js/ts: called by `new C()`, which edges to the class
}
_GLOB_PART = re.compile(r"\*\*/|\*\*|\*|\?")
_TEST = re.compile(
    r"(^|/)(tests?|__tests__|testdata|spec)/|(^|/)test_[^/]*$|_test\.[^/.]+$|\.(test|spec)\.[^/.]+$|(^|/)conftest\.py$"
)
_GEN = re.compile(
    r"(^|/)(vendor|node_modules|third_party|generated)/|_pb2(_grpc)?\.pyi?$|\.pb\.go$|\.min\.js$|\.generated\.[^/.]+$"
)


def is_ctor(qual: str) -> bool:
    """`Foo.Foo` or `Foo.~Foo` (a constructor or a destructor is named like its class)."""
    a, _, b = qual.rpartition(".")
    return bool(a) and b.lstrip("~") == a.rpartition(".")[2]


@functools.lru_cache(maxsize=None)
def _glob_re(pattern: str):
    """A path glob as a regex: `**/` any directories (or none), `*` and `?` inside one segment."""
    out, pos = [], 0
    for m in _GLOB_PART.finditer(pattern):
        out.append(re.escape(pattern[pos : m.start()]))
        out.append({"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}[m.group()])
        pos = m.end()
    return re.compile("".join(out) + re.escape(pattern[pos:]) + r"\Z")


_wild_rx = _glob_re  # the name the cfg unit tests use


def _glob(path: str, pattern: str) -> bool:
    """A file matches a module glob with or without its extension (`./l/*` names `l/en.js`)."""
    return fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(posixpath.splitext(path)[0], pattern)


def path_flags(path: str) -> int:
    """Flags a path alone implies (the extractor never sees the path): test = the table here or frameworks.json (graph.roots), generated = the
    table here (any language) or, for a schema file only, data/generated.json "paths" (vendored copies, google/protobuf).
    """
    return (F_TEST if _TEST.search(path) or _roots.is_test_path(path) else 0) | (
        F_GENERATED if _GEN.search(path) or generated_path(path) else 0
    )


class Linked(NamedTuple):
    graph: Graph
    amb: Tuple[str, ...]
    findings: Tuple[Finding, ...]


class _Bind(NamedTuple):
    names: dict  # (scope, local) -> (kind mod|sym|plain|ext|name, paths for mod, keys for sym, (real name,) for name)
    stars: tuple  # ((scope, path), ...)
    incs: tuple  # paths this file imports or includes whole
    edges: tuple  # (scope, dst key, kind, tier, line, rule, text): import and inherit edges
    bases: tuple  # (class key, base keys, external-base flag)
    uses: tuple = ()  # (scope, line, annotation keys): annotation uses that resolve to defs of the project


_NOBIND = _Bind({}, (), (), (), ())  # no import binding: a name is looked up as written


@functools.lru_cache(maxsize=None)  # the same few thousand spellings come back for every call site
def _parts(name: str) -> Tuple[str, ...]:
    return tuple(p for p in name.replace("::", ".").replace("->", ".").replace("?.", ".").split(".") if p)


def _chain(scope: str) -> List[str]:
    out = []
    while scope:
        out.append(scope)
        scope = scope.rpartition(".")[0]
    return out + [""]


class _Ix:
    """The read-only index every per-file unit needs (picklable; holds no Facts)."""

    def __init__(self, langs=None, crates: Optional[Mapping[str, dict]] = None):
        info = langs.langs if langs else {}
        self.pkg_langs = frozenset(k for k, v in info.items() if v.get("package_dir"))  # a package is its directory
        self.pkg_loose = frozenset(k for k, v in info.items() if v.get("package_loose"))  # ... but need not be
        self.facade = {k: v["file_facade"] for k, v in info.items() if v.get("file_facade")}  # lang -> facade suffix
        self.cspec: Dict[str, dict] = dict(crates or {})  # group -> langs.json "crate"
        self.srcs: Dict[str, Dict[str, Tuple[str, int, str]]] = {}  # group -> crate src dir -> (name, rank, root file)
        self.named: Dict[Tuple[str, str], List[str]] = {}  # (group, crate name) -> its src dirs
        self.libs: set = set()  # groups with a package that can be imported (it has a library crate)
        self.libdirs: Dict[str, List[str]] = {}  # group -> src dirs of its library crates
        self.pkg: Dict[str, str] = {}  # path -> the crate src dir it is under
        self.paths: Dict[str, Tuple[str, str, str]] = {}  # path -> (lang, language group, file key)
        self.defs: Dict[str, Dict[str, List[str]]] = {}  # path -> qual -> keys
        self.kind: Dict[str, str] = {}  # node key -> kind (files included)
        self.where: Dict[str, str] = {}  # node key -> path
        self.line: Dict[str, int] = {}  # def key -> line0
        self.flags: Dict[str, int] = {}  # def key -> flags (path flags included)
        self.top: Dict[Tuple[str, str], List[str]] = {}  # (group, name) -> top-level defs
        self.mem: Dict[Tuple[str, str], List[str]] = {}  # (group, name) -> members
        self.members: Dict[str, Dict[str, List[str]]] = {}  # class key -> name -> keys
        self.types: Dict[Tuple[str, str], List[str]] = (
            {}
        )  # (group, qual) -> class and iface defs of that qual, any file
        self.tkey: Dict[str, Tuple[str, str]] = {}  # class key -> its (group, qual) in types
        self.outer: Dict[str, Dict[str, List[str]]] = (
            {}
        )  # type key -> name -> members written outside the type (holder_edge)
        self.stems: Dict[Tuple[str, str], List[str]] = {}  # (group, path stem) -> paths
        self.stem_of: Dict[str, Tuple[str, str]] = {}
        self.mods: Dict[Tuple[str, str], List[str]] = {}  # (group, dotted suffix) -> paths
        self.byb: Dict[str, List[str]] = {}  # basename -> paths
        self.pkgdir: Dict[Tuple[str, str], List[str]] = (
            {}
        )  # (group, package directory) -> paths (package_dir languages)
        self.fac: Dict[Tuple[str, str], List[str]] = {}  # (group, facade class name) -> paths (Util.kt is UtilKt)
        self.cfg: Dict[str, List[str]] = {}
        self.cfg_order: List[str] = []
        self.cfg_files: set = set()  # paths of role-config languages: an entry that lives keeps its file alive
        self.extern: set = set()  # config files whose readers must sit beside them and do not (see _extern)
        self.inl: Dict[Tuple[str, str], List[str]] = {}  # (group, qual) -> files that declare the module inline
        # (group, type, name, key) of a member whose type is no def of its own file
        self.loose: List[Tuple[str, str, str, str]] = []
        # path -> protocol members with no type to live with: the file owns them
        self.orphans: Dict[str, List[str]] = {}
        self.bases: Dict[str, Tuple[str, ...]] = {}
        self.ext: set = set()  # classes with a base that resolves to nothing
        self.anc: Dict[str, Tuple[str, ...]] = {}
        self.beans: set = (
            set()
        )  # classes a DI container instantiates (a root of a kind with "bean": true in frameworks.json)
        self.encl: Dict[str, List[str]] = (
            {}
        )  # nested class key -> the classes that enclose it (package_dir: a used Inner uses its Outer)
        self.subs: Dict[str, List[str]] = {}  # class key -> every class that has it among its ancestors (CHA)
        self.incs: Dict[str, Tuple[str, ...]] = {}
        self.holder_fams: frozenset = (
            frozenset()
        )  # language groups whose members keep their type alive (langs.json holder_edge)
        self.tscope: frozenset = frozenset()  # language groups whose bare names see the members of the enclosing type
        self.nons: frozenset = frozenset()  # language groups without namespace defs: `ns::f` names the last part
        self.recv_fams: frozenset = (
            frozenset()
        )  # language groups whose members are named through a receiver only (langs.json members_by_receiver)
        self.twin_fams: frozenset = (
            frozenset()
        )  # language groups whose same-named defs of one directory are one symbol (langs.json build_twins)
        self.twins: Dict[str, List[str]] = (
            {}
        )  # def key -> the defs of that name in the other files of its directory (build-constraint pairs)
        self.copy_fams: frozenset = (
            frozenset()
        )  # language groups whose identical files under another root are one module (langs.json copy_twins)
        self.copies: Dict[str, List[str]] = (
            {}
        )  # def key -> the keys of the same qual in the identical copies of its file
        self.implicit_recv: frozenset = (
            frozenset()
        )  # languages (not groups: Scala shares the jvm group) whose bare name may be a member of an implicit receiver
        self.inherit: Dict[str, frozenset] = {}  # language group -> root kinds that a subclass inherits (root_inherit)
        self.sx: Dict[str, Tuple[str, ...]] = {}  # js path -> modules of its `export * from m`
        self.exp: Dict[str, Tuple[str, ...]] = {}  # js path -> keys of its top-level exports that `export *` passes on
        # the source extensions of the js family (data/langs.json, set by _prepare): the only ones a specifier drops
        self.jsext: frozenset = frozenset()
        self.init: Dict[str, List[str]] = {}  # path -> its top-level defs, for a file_init language
        self.meta: Dict[str, List[str]] = {}  # annotation type key -> ids of the rules on its own annotations
        self.anntypes: set = set()  # annotation types: they hand rules on, they get none
        self.derived: List[Tuple[str, str, str]] = []  # (def key, root kind, origin): roots given by annotation types
        self.xl = None  # graph.xlink.XIx (native symbols, environment writers), built by _prepare

    # ---------------------------------------------------------- build
    def add_file(self, path: str, facts: Facts, g: str, pflags: int, bad: list, init: bool = False) -> list:
        """Index one file; returns its nodes (the file node first). `init`: langs.json file_init, see the module docstring."""
        lang, fkey = facts.lang, node_key(facts.lang, path)
        self.paths[path], self.kind[fkey], self.where[fkey] = (lang, g, fkey), "file", path
        self.byb.setdefault(posixpath.basename(path), []).append(path)
        pkg = lang in self.pkg_langs
        if pkg:
            self.pkgdir.setdefault((g, pkg_dir(lang, path)), []).append(path)
        full = posixpath.splitext(path)[0]
        segs = full.split("/")
        short = "/".join(segs[:-1]) if segs[-1] in INDEXISH else full
        self.stem_of[path] = (full, short)
        spec = self.cspec.get(g)
        if spec and len(segs) > 1 and segs[-2] == spec["src"] and segs[-1] in spec["roots"]:  # src/lib.rs, src/main.rs
            src, rank = "/".join(segs[:-1]), spec["roots"].index(segs[-1])
            if rank < self.srcs.setdefault(g, {}).get(src, ("", len(spec["roots"]), ""))[1]:
                self.srcs[g][src] = (segs[-3].replace("-", "_") if len(segs) > 2 else "", rank, path)
        sfx = self.facade.get(lang)
        facade = f"{segs[-1][:1].upper()}{segs[-1][1:]}{sfx}" if sfx else ""
        if facade:
            self.fac.setdefault((g, facade), []).append(path)
        for stem in {full, short} | ({"/".join(segs[:-1] + [facade])} if facade else set()):
            self.stems.setdefault((g, stem), []).append(path)
            s = stem.split("/") if stem else []
            for i in range(len(s)):
                self.mods.setdefault((g, ".".join(s[i:])), []).append(path)
        raw = path.split("/")  # with the extension: #include "a/b.h" -> a.b.h
        for i in range(len(raw)):
            self.mods.setdefault((g, ".".join(raw[i:])), []).append(path)
        if g != "python":  # a package is its directory (go, java, c#, kotlin)
            d = raw[:-1]
            for i in range(len(d)):
                self.mods.setdefault((g, ".".join(d[i:])), []).append(path)
        if g == "js":
            self.sx[path] = tuple(im.module for im in facts.imports if im.names == ("*",))
        out = [node(fkey, "file", lang, path, 1, 1, posixpath.dirname(path), "package", pflags)]
        table = self.defs.setdefault(path, {})
        count = Counter((d.qual, d.arity) for d in facts.defs)
        seen: Counter = Counter()
        made = []
        for d in sorted(facts.defs, key=lambda d: (d.line0, d.qual, d.arity, d.kind)):
            if not d.qual:  # the module def (extract.common.module_def) IS the file node: it only widens its line range
                out[0] = out[0]._replace(line1=max(out[0].line1, d.line1), flags=out[0].flags | d.flags)
                continue
            ident = (d.qual, d.arity)
            h8 = hashlib.sha1(f"{d.qual}/{d.arity}#{seen[ident]}".encode()).hexdigest()[:8] if count[ident] > 1 else ""
            seen[ident] += 1
            key = node_key(lang, path, d.qual, d.arity, h8)
            last = d.qual.rpartition(".")[2]
            ctor = g != "python" and is_ctor(d.qual)  # a use of the type is the use of its constructor and destructor
            fl = (
                d.flags
                | pflags
                | (
                    F_PROTOCOL
                    if ctor or "." in d.qual and (last[:2] == last[-2:] == "__" or last in PROTOCOL_NAMES)
                    else 0
                )
            )
            try:
                out.append(node(key, d.kind, lang, path, d.line0, d.line1, posixpath.dirname(path), d.vis, fl))
            except ValueError as e:
                bad.append(Finding("E", path, d.line0, "dc.bad-facts", f"def {d.qual!r}: {e}"))
                continue
            table.setdefault(d.qual, []).append(key)
            self.kind[key], self.where[key], self.flags[key], self.line[key] = d.kind, path, fl, d.line0
            made.append((d.qual, key))
            if d.kind == "cfgkey":
                self.cfg.setdefault(d.qual, []).append(key)
            elif d.kind == "module":
                self.inl.setdefault((g, d.qual), []).append(path)
        if g == "js":
            vis = {d.qual: d.vis for d in facts.defs}
            self.exp[path] = tuple(k for q, k in made if vis.get(q) == "exported" and q not in ("default", "*"))
        beans = bean_kinds()
        for h in facts.roots:
            if h.kind in beans:
                self.beans.update(k for k in table.get(h.target, ()) if self.kind[k] in CLASSY)
            rid = lang_rules.meta_rule(h.kind)
            for k in table.get(h.target, ()) if rid is not None else ():
                self.anntypes.add(k)
                self.meta.setdefault(k, []).extend([rid] if rid else [])
        for qual, key in made:
            parent, _, last = qual.rpartition(".")
            if not parent:
                self.top.setdefault((g, qual), []).append(key)
                if init:
                    self.init.setdefault(path, []).append(key)
                continue
            self.mem.setdefault((g, last), []).append(key)
            if not table.get(parent):  # `b.Foo.x`: an impl of a type of another module is no member of any def here
                self.loose.append((g, parent, last, key))
            for pk in table.get(parent, ()):
                if self.kind[pk] in CLASSY:
                    self.members.setdefault(pk, {}).setdefault(last, []).append(key)
                    if self.kind[key] in CLASSY and pkg:
                        self.encl.setdefault(key, []).append(pk)
        for qual, key in made:
            if self.kind[key] in CLASSY:
                self.types.setdefault((g, qual), []).append(key)
                self.tkey[key] = (g, qual)
        return out

    def seal(self):
        for tab in (self.mods, self.stems, self.inl):
            for k in tab:
                tab[k] = sorted(set(tab[k]))
        for path, table in self.defs.items():
            g = self.paths[path][1]
            for qual, ks in table.items() if g in self.holder_fams else ():
                parent, _, last = qual.rpartition(".")
                if parent and not self.is_class(path, parent):  # `Foo::run` in a .cpp: the type is declared in a header
                    for c in self.type_named(g, parent):
                        self.outer.setdefault(c, {}).setdefault(last, []).extend(ks)
        by: Dict[tuple, List[str]] = {}
        for (
            path,
            table,
        ) in (
            self.defs.items()
        ):  # go: a symbol declared in two files of one directory exists once per build (plat_windows.go, native.go)
            for qual, ks in table.items() if self.paths[path][1] in self.twin_fams else ():
                by.setdefault((posixpath.dirname(path), qual), []).extend(k for k in ks if not self.flags[k] & F_TEST)
        for ks in by.values():
            for k in ks if len({self.where[m] for m in ks}) > 1 else ():
                self.twins[k] = [m for m in ks if self.where[m] != self.where[k]]
        for g, srcs in self.srcs.items():
            for src in sorted(srcs):
                name, rank, _root = srcs[src]
                if rank:  # a package with no library crate (only main.rs) is no one's dependency: its name is no path
                    continue
                self.libs.add(g)
                self.libdirs.setdefault(g, []).append(src)
                if name and name not in RELATIVE_ROOTS:
                    self.named.setdefault((g, name), []).append(src)
        for p, (_, g, _k) in self.paths.items():  # the crate a file belongs to: the closest src dir above it
            d = posixpath.dirname(p)
            while d and g in self.srcs and d not in self.srcs[g]:
                d = posixpath.dirname(d)
            if d and g in self.srcs:
                self.pkg[p] = d
        for g, parent, last, key in self.loose:  # a go method or a rust impl written in another file than its type
            name = parent.rpartition(".")[2]
            cands = self.top.get((g, name), []) + (self.mem.get((g, name), []) if "." in parent else [])
            types = sorted(c for c in cands if self.kind[c] in CLASSY)
            here = posixpath.dirname(self.where[key])
            for c in [c for c in types if posixpath.dirname(self.where[c]) == here] or types:
                self.members.setdefault(c, {}).setdefault(last, []).append(key)
            if not types and self.flags[key] & F_PROTOCOL:  # a trait impl for a type the project lacks (f64, [T])
                self.orphans.setdefault(self.where[key], []).append(key)
        self.loose = []
        self.cfg_order = sorted(self.cfg)

    def cfg_keys(self, name: str) -> List[str]:
        """Config keys a `reads` ref names: exactly `name`, or every key below the prefix when it ends with '*'."""
        if not name.endswith("*"):
            return list(self.cfg.get(name, ()))
        pre, out = name[:-1], []
        for i in range(bisect_left(self.cfg_order, pre), len(self.cfg_order)):
            if not self.cfg_order[i].startswith(pre):
                break
            out += self.cfg[self.cfg_order[i]]
        return out

    def finish(self, binds: Mapping[str, _Bind]):
        """Merge the round-A results: base classes, ancestors (BFS, cycle safe), include lists."""
        for p in sorted(binds):
            for ck, bks, ext in binds[p].bases:
                self.bases[ck] = tuple(sorted(set(bks)))
                if ext:
                    self.ext.add(ck)
            self.incs[p] = tuple(sorted(set(binds[p].incs)))
        # the parts of a partial class are one type: each has the bases of all of them
        for (g, _), parts in sorted(self.types.items()):
            if g in self.tscope and len(parts) > 1:
                bks = tuple(sorted({b for c in parts for b in self.bases.get(c, ())} - set(parts)))
                for c in parts:
                    self.bases[c] = bks
                    if any(x in self.ext for x in parts):
                        self.ext.add(c)
        for ck in sorted(self.bases):
            out, todo = [], list(self.bases[ck])
            while todo:
                a = todo.pop(0)
                if a != ck and a not in out:
                    out.append(a)
                    todo += self.bases.get(a, ())
            self.anc[ck] = tuple(out)
            for a in out:
                self.subs.setdefault(a, []).append(ck)
        self._derive(binds)

    def _derive(self, binds: Mapping[str, _Bind]):
        """Annotation inheritance (module docstring): the rules a def gets from the project annotation types it uses."""
        uses: Dict[str, Dict[str, str]] = {}  # def key -> {annotation key: origin of the first use}
        for p in sorted(binds):
            for scope, line, keys in binds[p].uses:
                for x in self.src_keys(p, scope):
                    for a in keys if self.kind[x] in lang_rules.SIDE else ():
                        uses.setdefault(x, {}).setdefault(a, f"{p}:{line}")
        eff = lang_rules.inherit(self.meta, {x: list(u) for x, u in uses.items() if x in self.anntypes})
        for x in sorted(set(uses) - self.anntypes):  # an annotation type hands rules on and gets none itself
            for a, origin in sorted(uses[x].items()):
                for rid in eff.get(a, ()):
                    rule = lang_rules.rule(rid)
                    if rule and rule["on"] == lang_rules.SIDE[self.kind[x]]:
                        self.derived.append((x, rule.get("kind") or f"annotation:{rid}", origin))
                        if rule.get("bean") and self.kind[x] in CLASSY:
                            self.beans.add(x)

    # ---------------------------------------------------------- lookups
    def is_class(self, path: str, qual: str) -> bool:
        return any(self.kind[k] in CLASSY for k in self.defs[path].get(qual, ()))

    def member(self, ck: str, name: str, own: bool = True) -> List[str]:
        for c in ((ck,) if own else ()) + self.anc.get(ck, ()):
            if self.members.get(c, {}).get(name):
                return self.members[c][name]
        return []

    def type_named(self, g: str, qual: str) -> List[str]:
        """The types of the family named `qual`, in any file; a leading qualifier the type is not declared under is dropped (`n::Foo::run`
        written out of class, Foo declared inside `namespace n`: namespaces are no defs)."""
        parts = qual.split(".")
        for i in range(len(parts)):
            if (g, ".".join(parts[i:])) in self.types:
                return self.types[(g, ".".join(parts[i:]))]
        return []

    def holders(self, path: str, g: str, qual: str) -> List[str]:
        """The types that hold the member `qual` of `path`: the type of that file, else every type of that name in the family
        (a method written as `Foo::run` in a .cpp, its class is in the header)."""
        parent = qual.rpartition(".")[0]
        if not parent:
            return []
        return [k for k in self.defs[path].get(parent, ()) if self.kind[k] in CLASSY] or self.type_named(g, parent)

    def implicit(self, g: str, chain: List[str], name: str) -> List[str]:
        """Members a bare `name` denotes in the scope `chain` of a type_scope family: those of the innermost enclosing type that has one, of
        every declaration of that type, and of its ancestors."""
        if (g, name) not in self.mem:  # no type of the family has a member of that name (a local, a type, a parameter)
            return []
        for sc in chain:
            hit = [m for c in self.type_named(g, sc) for m in self.member(c, name)] if sc else []
            if hit:
                return hit
        return []

    def _shadow(self, g: str, head: str, pairs: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
        """A member named like a type (`Address Address`) hides the type from the scope chain, not from the program: the type is used too."""
        if g not in self.tscope:
            return pairs
        seen = {k for k, _ in pairs}
        return pairs + [(k, "N") for k in self.top.get((g, head), ()) if k not in seen]

    def parts(self, ck: str) -> List[str]:
        """The class keys that declare the same type as `ck` (the other parts of a partial class), `ck` included, for a type_scope family."""
        t = self.tkey.get(ck)
        return self.types[t] if t and t[0] in self.tscope else [ck]

    def has_ext(self, ck: str) -> bool:
        """A base of the class or of one of its ancestors resolves to nothing: members may override unseen methods."""
        return ck in self.ext or any(a in self.ext for a in self.anc.get(ck, ()))

    def src_keys(self, path: str, scope: str) -> List[str]:
        d = self.defs[path]
        while scope:
            if scope in d:
                return d[scope]
            scope = scope.rpartition(".")[0]
        return [self.paths[path][2]]

    def _best(self, path: str, cands: List[str], toks: List[str]) -> List[str]:
        if len(cands) < 2:
            return list(cands)
        want, own = "/".join(toks), self.pkg.get(path)
        picks = (lambda p: self.pkg.get(p) == own,) if own else ()  # a crate sees its own modules first
        for pick in picks + (
            lambda p: want in self.stem_of[p],
            lambda p: posixpath.dirname(p) == posixpath.dirname(path),
        ):
            hit = [p for p in cands if pick(p)]
            if hit:
                return hit
        m = min(p.count("/") for p in cands)
        return [p for p in cands if p.count("/") == m]

    def resolve_module(self, g: str, path: str, module: str, level: int = 0) -> Tuple[List[str], List[str]]:
        """(files the module name denotes, trailing tokens that name something inside it): longest known prefix wins."""
        toks = [t for t in re.split(r"::|[./\\]", module) if t]
        if level:  # python relative import
            d = posixpath.dirname(path)
            for _ in range(level - 1):
                d = posixpath.dirname(d)
            for k in range(len(toks), -1, -1):
                hit = self.stems.get((g, posixpath.join(d, *toks[:k]) if k else d))
                if hit:
                    return hit, toks[k:]
            return [], toks
        if module.startswith(("./", "../")) or (g == "js" and module in (".", "..")):  # js style path
            p = posixpath.normpath(
                posixpath.join(posixpath.dirname(path), module.partition("?")[0] if g == "js" else module)
            )
            p = "" if p == "." else p  # the project root: the stem of its index file
            if p in self.paths:
                return [p], []
            ext = posixpath.splitext(p)[1]
            # `./foo.utils` names foo.utils.*, not foo.*: only a source extension of the family is dropped
            base = p[: len(p) - len(ext)] if g != "js" or ext in self.jsext else p
            for b in dict.fromkeys((base, p)):
                hit = self.stems.get((g, b), [])
                if hit:  # a file wins over the index of a directory of that name (Node)
                    return [q for q in hit if self.stem_of[q][0] == b] or hit, []
            return [], []
        if toks and (g, toks[0]) in self.named:  # `util::net::conn`: a crate of this repo
            return self._in_crate(g, self.named[(g, toks[0])], toks[1:])
        while g == "js" and len(toks) > 1 and toks[0][0] in ALIAS_MARKS and toks[0] != "@types":
            toks = toks[1:]  # `@/components/Button`, `@org/ui/button`: the marker names no directory we know
        while toks and toks[0] in ("crate", "self", "super") and len(toks) > 1:  # rust
            toks = toks[1:]
        for k in range(len(toks), 0, -1):
            hit = self.mods.get((g, ".".join(toks[:k])))
            if hit:
                return self._best(path, hit, toks[:k]), toks[k:]
        return self.lib_path(g, toks) or ([], toks)

    def _in_crate(self, g: str, dirs: Sequence[str], rest: List[str]) -> Tuple[List[str], List[str]]:
        """(files, trailing tokens) of the path `rest` inside the crates whose src dirs are `dirs`: the longest prefix that is a
        module file, else the crate root file (an inline module, an item or a re-export of lib.rs follows in the tokens).
        """
        found = []
        for d in dirs:
            for k in range(len(rest), -1, -1):
                hit = self.stems.get((g, posixpath.join(d, *rest[:k]))) if k else [self.srcs[g][d][2]]
                if hit:
                    found.append((k, hit))
                    break
        top = max(k for k, _ in found)
        return sorted({p for k, hit in found if k == top for p in hit}), rest[top:]

    def crate_head(self, g: str, head: str) -> bool:
        """`head` may name a crate of the project: a package directory, or, in a repo with a library crate, any crate but the
        toolchain's (a package is named in its Cargo.toml, which is not read here: the package at the top of the repo, a
        directory named otherwise, a `package = ".."` rename)."""
        return (g, head) in self.named or (g in self.libs and head not in self.cspec[g]["sysroot"])

    def lib_path(self, g: str, toks: List[str], root: bool = False) -> Optional[Tuple[List[str], List[str]]]:
        """(files, trailing tokens) of the path `toks` when its head is the name of a library crate that no directory gives: the
        package at the top of the repo, in a lib + bin layout, is named in its Cargo.toml (`mycrate::cli::parse()` from src/main.rs,
        tests/ and examples/). The rest is read inside the library crates like a `crate::` path, but only when it names a module
        file of one (`root`: or a def of the crate root); a head that is no crate of the project (a dependency) rarely does.
        """
        if len(toks) < 2 or g not in self.libs or (g, toks[0]) in self.named or toks[0] in RELATIVE_ROOTS:
            return None
        if toks[0] in self.cspec[g]["sysroot"]:
            return None
        files, rest = self._in_crate(g, self.libdirs[g], toks[1:])
        if len(rest) < len(toks) - 1 or (root and any(rest[0] in self.defs[p] for p in files)):
            return files, rest
        return None

    def inline_module(self, g: str, path: str, scope: str, module: str) -> Tuple[List[str], List[str], List[str]]:
        """(files, qual tokens, module keys) of the module an import names when it is declared inline (`mod m { .. }`): in the
        importing file from `scope` outward, else in any file of the language; leading crate / self / super names no module.
        """
        toks = [t for t in re.split(r"::|[./\\]", module) if t]
        lead = bool(toks) and toks[0] in RELATIVE_ROOTS
        while toks and toks[0] in RELATIVE_ROOTS:
            toks = toks[1:]
        for k in range(len(toks), 0, -1):
            want = ".".join(toks[:k])
            for sc in [""] if lead else _chain(scope):
                q = f"{sc}.{want}" if sc else want
                own = [x for x in self.defs[path].get(q, ()) if self.kind[x] == "module"]
                if own:
                    return [path], q.split(".") + toks[k:], own
            hit = self.inl.get((g, want), [])
            if hit:
                return hit, toks, [x for p in hit for x in self.defs[p].get(want, ()) if self.kind[x] == "module"]
        return [], toks, []

    def children(self, paths: Sequence[str], qual: Sequence[str]) -> Dict[str, List[str]]:
        """{name: keys} of the defs directly inside the definition `qual` of each file (the names a glob import binds)."""
        pre, out = ".".join(qual) + ".", {}
        for p in paths:
            for q, ks in self.defs[p].items():
                if q.startswith(pre) and "." not in q[len(pre) :]:
                    out.setdefault(q[len(pre) :], []).extend(ks)
        return out

    def star_closure(self, g: str, path: str) -> List[str]:
        """Files whose exports `path` re-exports through `export * from m` (transitive, cycle safe)."""
        out, todo, seen = [], [path], {path}
        while todo:
            p = todo.pop(0)
            for m in self.sx.get(p, ()):
                for q in self.resolve_module(g, p, m)[0]:
                    if q not in seen:
                        seen.add(q)
                        out.append(q)
                        todo.append(q)
        return out

    def path_targets(self, path: str, name: str) -> List[Tuple[str, str]]:
        """Files a path or a command name points at, any language: [(file key, tier)]."""
        n = name.strip()
        n = n[2:] if n.startswith("./") else n
        near = posixpath.normpath(posixpath.join(posixpath.dirname(path), n))
        if _GLOB_PART.search(n):
            # every file the pattern matches is included: relative to the includer or the tree root, else anywhere under that tail
            pats = (_glob_re(near), _glob_re(n.lstrip("/")))
            hit = [p for p in self.paths if p != path and any(r.match(p) for r in pats)] or [
                p for p in self.paths if p != path and _glob_re("**/" + n.lstrip("/")).match(p)
            ]
            return [(self.paths[p][2], "T") for p in sorted(hit)]
        cands = [p for p in self.byb.get(posixpath.basename(n), ()) if p == n or p == near or p.endswith("/" + n)]
        if "/" not in n:
            cands = [p for p in cands if p.startswith("bin/") or "/bin/" in p] or cands
        return [(self.paths[p][2], "T" if len(cands) == 1 else "N") for p in sorted(cands)]

    # ---------------------------------------------------------- name resolution
    def _by_name(self, g: str, name: str, member: bool) -> List[Tuple[str, str]]:
        return [(k, "N") for k in (self.mem if member else self.top).get((g, name), ())]

    @staticmethod
    def _down(pairs: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
        """Several distinct targets are a guess, not a resolution: N."""
        return pairs if len({k for k, _ in pairs}) < 2 else [(k, "N") for k, _ in pairs]

    def _attr(self, g: str, ks: Sequence[str], rest: List[str]) -> List[Tuple[str, str]]:
        """`ks` found, `rest` is what is written after it: the first token is a member when ks are classes; a member that is a
        class (a nested type: Outer.Inner.go) is entered, so the tokens after it are its members."""
        out = [(k, "T") for k in ks]
        for k in ks:  # a used nested class keeps its outer classes (the file cannot lose them)
            while k in self.encl:
                k = self.encl[k][0]
                out.append((k, "T"))
        if rest:
            mem = [m for c in ks if self.kind[c] in CLASSY for m in self.member(c, rest[0])]
            if not mem:
                return out + self._by_name(g, rest[-1], True)
            out += [(m, "T") for m in mem]
            nested = [m for m in mem if self.kind[m] in CLASSY]
            if nested and len(rest) > 1:
                out += self._attr(g, nested, rest[1:])
        return list(dict.fromkeys(out))

    def _api(self, path: str) -> Tuple[str, ...]:
        """What a namespace object of the file holds: its top-level exports, its default, the barrel `*` that passes more on."""
        d = self.defs[path]
        return (*self.exp.get(path, ()), *d.get("default", ()), *d.get("*", ()))

    def _in_modules(self, g: str, paths: Sequence[str], rest: List[str], whole: bool = False) -> List[Tuple[str, str]]:
        if not rest:
            out = self._down([(self.paths[p][2], "T") for p in paths])
            if g == "js":  # `const f = require(m); f()`: the module value is its default export (module.exports = f)
                out += [(k, "T") for p in paths for k in self.defs[p].get("default", ())]
                # the namespace itself passed, returned, stored or re-exported (`whole`): any export may be read from it
                out += [(k, "N") for p in paths for k in self._api(p)] if whole else []
            return out
        for j in range(len(rest), 0, -1):
            ks = [k for p in paths for k in self.defs[p].get(".".join(rest[:j]), ())]
            if ks:
                # `m::Foo::new()`: the member is found by its qualified name, the type that holds it is used as well
                held = [k for i in range(1, j) for p in paths for k in self.defs[p].get(".".join(rest[:i]), ())]
                return self._down(self._attr(g, ks, rest[j:])) + [(k, "T") for k in held if self.kind[k] in CLASSY]
        out = self._by_name(g, rest[-1], len(rest) > 1)
        # `const Foo = require('./foo'); Foo.create()`, `import svc from './svc'; svc.run()`: the module value is its default export
        # (module.exports = Foo, export default new S()), and what is read from it is a member of that value, found by its name (N)
        value = [k for p in paths for k in self.defs[p].get("default", ())] if g == "js" else []
        return out + [(k, "T") for k in value] + (self._by_name(g, rest[-1], True) if value and len(rest) == 1 else [])

    def resolve(
        self, path: str, g: str, bd: _Bind, scope: str, name: str, kind: str = ""
    ) -> Tuple[List[Tuple[str, str]], str]:
        """(targets [(key, tier)], rule) of the name `name` (a use of edge kind `kind`) written in `scope` of file `path`."""
        parts = _parts(name)
        if not parts:
            return [], ""
        if name.startswith("?."):  # a receiver the extractor could not name (`new B().Run()`): any member of that name
            return self._by_name(g, parts[-1], True), "name"
        head, rest = parts[0], parts[1:]
        chain, defs = _chain(scope), self.defs[path]
        if head in SELF_NAMES and rest:
            cls = next((s for s in chain if s and self.is_class(path, s)), None)
            if cls:
                mem = [m for c in defs[cls] if self.kind[c] in CLASSY for m in self.member(c, rest[0], head != "super")]
                out = [(m, "T") for m in mem]
                if (
                    not mem or len(rest) > 1
                ):  # `this.repo.Save()`: the field is found, Save is a member of what it holds
                    out += self._by_name(g, rest[-1], True)
                return out, "self"
        for sc in chain:
            if g in self.recv_fams and sc and sc != scope and self.is_class(path, sc):
                continue  # python: a class body is no scope for the defs nested in it; go: a bare `renderPage` is the type, never the method
            ks = defs.get(f"{sc}.{head}" if sc else head)
            if ks:
                return self._shadow(g, head, self._attr(g, ks, rest)), "scope"
        if g in self.tscope:
            ks = self.implicit(g, chain, head)
            if ks:
                return self._shadow(g, head, self._down(self._attr(g, ks, rest))), "this"
        for sc in chain:
            e = bd.names.get((sc, head))
            if e is None:
                continue
            bkind, tg = e
            if bkind == "ext":
                return [], "external"
            if bkind == "name":
                # an in-crate name that no import target answers (a re-export): any def of that name, a module item too
                got, rule = self.resolve(path, g, _NOBIND, "", ".".join((tg[0],) + rest))
                return (got if rest else got + self._by_name(g, tg[0], True)), rule
            if bkind == "sym":
                return self._attr(g, tg, rest), "import"
            if bkind == "mod":
                return self._in_modules(g, tg, rest, kind == "ref"), "import"
            paths, rest2 = self.resolve_module(
                g, path, ".".join(parts)
            )  # plain `import a.b`: the ref spells the module
            return (self._in_modules(g, paths, rest2) if paths else []), "import"
        lang = self.paths[path][0]
        if lang in self.pkg_langs:
            ks = [k for p in self.pkgdir.get((g, pkg_dir(lang, path)), ()) for k in self.defs[p].get(head, ())]
            if ks:
                return self._attr(g, ks, rest), "package"
        extra = [p for sc, p in bd.stars if sc in chain]
        if g in GLOBAL_FAMS:
            todo, seen = [path], {path}
            while todo:
                for p in self.incs.get(todo.pop(), ()):
                    if p not in seen:
                        seen.add(p)
                        todo.append(p)
            extra += sorted(seen - {path})
        ks = [k for p in extra for k in self.defs[p].get(head, ())]
        if ks:
            out = self._attr(g, ks, rest)
            if lang not in self.pkg_langs:  # as before: a star import of the other languages stays a guess for a member
                return self._down(out), "star"
            return (
                out if len(set(ks)) < 2 else [(k, "N") for k, _ in out]
            ), "star"  # a wildcard import of one class is a resolution, two classes of that name a guess
        fac = self.fac.get((g, head))
        # UtilKt.f() of Java: the top-level f of Util.kt (the file itself is used even when f is a generated accessor)
        if fac:
            out = list(dict.fromkeys([(self.paths[p][2], "T") for p in fac] + self._in_modules(g, fac, rest)))
            return (out if len(fac) == 1 else [(k, "N") for k, _ in out]), "facade"
        if rest and (g, head) in self.named:  # `util::other()` without a use (`util.f()` may call a method of a value)
            paths, rest2 = self.resolve_module(g, path, ".".join(parts))
            return self._in_modules(g, paths, rest2) + self._by_name(g, rest[-1], True), "import"
        lp = self.lib_path(g, list(parts), root=True) if rest else None
        if lp:  # `mycrate::run()`, `mycrate::cli::parse()` without a use: the head may be a value (`x.f()`), so N
            return [(k, "N") for k, _ in self._in_modules(g, *lp)], "import"
        if rest:
            # `x.f`: a member of something. `x.y.f`: also a top-level def of the module that x.y may be (m.DTS.coverage(...) with m a
            # parameter): not for a name that many modules define. Without namespace defs `util::helper` is that too (head = a namespace).
            mem = self._by_name(g, rest[-1], True)
            top = self._by_name(g, rest[-1], False) if len(rest) > 1 or g in self.nons else []
            return mem + (top if len(top) <= AMB_MAX else []), "name"
        top = self._by_name(g, head, False)
        if (
            lang in self.implicit_recv
        ):  # `bar()` in an extension function or a lambda with receiver (apply, with, a builder): a member
            return top + self._by_name(g, head, True), "name"
        return top, "name"

    def di_targets(self, tg: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
        """Injection of type T: T itself and every bean that is T or has T among its ancestors (class hierarchy analysis). One
        candidate bean is a resolution (T), several are a guess (N): the container picks by qualifier or primary, which is not read.
        """
        out = dict(tg)
        beans = sorted(
            {b for k, _ in tg if self.kind.get(k) in CLASSY for b in [k, *self.subs.get(k, ())] if b in self.beans}
        )
        out.update({b: "T" if len(beans) == 1 else "N" for b in beans})
        return sorted(out.items())

    def dyn_targets(self, path: str, g: str, bd: _Bind, scope: str, pattern: str) -> Tuple[List[str], str]:
        """(target keys, edge kind) of a dynamic site; see the module docstring for the pattern grammar."""
        pat, _, arg = pattern.partition(":")
        defs = self.defs[path]
        every = [k for ks in defs.values() for k in ks]
        if (
            pat == "static"
        ):  # `using static Util;`: the members of that type are used by their bare name; a type the project lacks adds nothing
            cls = [k for k, _ in self.resolve(path, g, bd, scope, arg)[0] if self.kind[k] in CLASSY]
            return sorted({m for c in cls for ms in self.members.get(c, {}).values() for m in ms}), "ref"
        if (
            pat == "implicit"
        ):  # `import app.Imp._` (Scala): what the type holds as implicit is in scope of the file and used unnamed
            cls = [k for k, _ in self.resolve(path, g, bd, scope, arg)[0] if self.kind[k] in CLASSY]
            return (
                sorted(
                    {
                        m
                        for c in cls
                        for ms in self.members.get(c, {}).values()
                        for m in ms
                        if self.flags[m] & F_IMPLICIT
                    }
                ),
                "ref",
            )
        if pat in ("getattr", "attr", "members"):
            arg, _, pick = arg.partition("/")  # `members:Mw/Invoke*`: only the members whose name matches the glob
            # Go: `dep.F(T{})` hands a value to ANOTHER package (queries/go/refs.scm): code of that package can call only the EXPORTED members of T
            # through an interface, so only those stay used; and a T that this tree does not declare names no member (no enclosing type, not every def of the file)
            gov = g == "go" and pat == "members" and bool(arg)
            cls = [k for k, _ in self.resolve(path, g, bd, scope, arg)[0] if self.kind[k] in CLASSY] if arg else []
            for s in _chain(scope) if not cls and not gov else ():
                if s and self.is_class(path, s):
                    cls = [k for k in defs[s] if self.kind[k] in CLASSY]
                    break
            if not cls:
                return ([] if gov else sorted(every)), "ref"
            return (
                sorted(
                    {
                        m
                        for c in cls
                        for a in (c,) + self.anc.get(c, ())
                        for nm, ms in self.members.get(a, {}).items()
                        if fnmatch.fnmatchcase(nm, pick or "*") and (not gov or nm[:1].isupper())
                        for m in ms
                    }
                ),
                "ref",
            )
        if pat == "pkg":
            pre = arg.replace(".", "/").strip("/")
            return (
                sorted(
                    k
                    for p, (_, pg, _f) in self.paths.items()
                    if pg == g and (not pre or f"/{pre}/" in f"/{p}")
                    for ks in self.defs[p].values()
                    for k in ks
                    if self.kind[k] in CLASSY
                ),
                "ref",
            )
        if pat == "name":
            hit = {
                k
                for tab in (self.top, self.mem)
                for (tg, nm), ks in tab.items()
                if tg == g and fnmatch.fnmatchcase(nm, arg)
                for k in ks
            }
            for k in sorted(hit):  # a nested class loaded by name (`p.Outer$Inner`) keeps the classes that enclose it
                while k in self.encl:
                    k = self.encl[k][0]
                    hit.add(k)
            return sorted(hit), "ref"
        if pat == "import" and g == "js" and ("*" in arg or "?" in arg):  # import(`./l/${x}.js`): each file it can name
            want = (
                arg.lstrip("/")
                if arg.startswith("/")
                else posixpath.normpath(posixpath.join(posixpath.dirname(path), arg))
            )
            wants = (want, want[2:]) if want.startswith("*/") else (want,)  # `${root}/x/*.js`: x/ at any depth
            hit = [
                p for p, (_, pg, _f) in self.paths.items() if pg == g and p != path and any(_glob(p, w) for w in wants)
            ]
            return (
                sorted({k for p in hit for k in (self.paths[p][2], *self._api(p))}),
                "import",
            )
        if (
            pat == "funcs"
        ):  # powershell `Export-ModuleMember -Function $Public.Basename`: the functions of the files the include glob names
            files = sorted({self.where[k] for k, _ in self.path_targets(path, arg)})
            return sorted(k for p in files for ks in self.defs[p].values() for k in ks if self.kind[k] == "func"), "ref"
        if pat == "import":
            return sorted(self.paths[p][2] for p in self.resolve_module(g, path, arg)[0]), "import"
        if (
            pat == "module"
        ):  # a module loaded by name (importlib, spec_from_file_location): the module object holds its top-level defs
            names = [m for (mg, m) in self.mods if mg == g and fnmatch.fnmatchcase(m, arg)] if "*" in arg else [arg]
            hit = {q for m in names for q in self.resolve_module(g, path, m)[0]} - {path}
            return (
                sorted(
                    {
                        k
                        for q in hit
                        for k in (self.paths[q][2], *(k for n, ks in self.defs[q].items() if "." not in n for k in ks))
                    }
                ),
                "import",
            )
        fn = [k for k in every if self.kind[k] in ("func", "method")]
        return sorted(fn or every), "ref"

    def resolve_target(self, src: str, target: str, code: bool) -> List[str]:
        """Node keys a root target denotes (see the module docstring)."""
        scheme = selector_scheme(target)
        val = target[len(scheme) + 1 :] if scheme else target
        if scheme == "file" or (not scheme and not code):
            p = posixpath.normpath(val if scheme == "file" else target)
            return [self.paths[p][2]] if p in self.paths else []
        if scheme == "node":
            return [val] if val in self.kind else []
        if scheme == "cmd":
            return [k for k, _ in self.path_targets(src, val)]
        if scheme == "build":  # a recipe or stage that a doc or a Dockerfile names: the nodes of that qual, in any file
            return sorted(k for defs in self.defs.values() for k in defs.get(val, ()))
        if code and not target and src in self.paths:  # the extractor's "the module itself" (python main guard, ...)
            return [self.paths[src][2]]
        return list(self.defs.get(src, {}).get(target, ()))


# ---------------------------------------------------------------- per-file units
def _bind(names: dict, key: tuple, entry: tuple) -> None:
    """Bind a name. The same name bound again to modules or symbols in one scope (the cfg-gated variants `#[cfg(unix)] mod imp;`
    and `#[cfg(windows)] mod imp;`, a try / except import) keeps the targets of both."""
    old = names.get(key)
    if old and old[0] == entry[0] and entry[0] in ("mod", "sym"):
        entry = (entry[0], tuple(sorted(set(old[1]) | set(entry[1]))))
    names[key] = entry


def _round_a(ix: _Ix, chunk) -> list:
    """Import bindings and base classes of each file: [(path, _Bind)]."""
    out = []
    for path, facts in chunk:
        g, names, stars, incs, edges, bases = ix.paths[path][1], {}, [], [], [], []
        lang = ix.paths[path][0]
        pkg, loose = lang in ix.pkg_langs, lang in ix.pkg_loose
        for im in facts.imports:
            sc = im.scope
            paths, rest = ix.resolve_module(g, path, im.module, im.level)
            mods: List[str] = []
            relative = bool(im.level) or im.module.startswith(("./", "../"))
            if not paths and not relative:  # no file: an inline module (`mod m { .. }`) of this file or another one
                paths, rest, mods = ix.inline_module(g, path, sc, im.module)
            if paths and not mods:  # the path goes on through inline modules of the file: `use util::deep::leaf`
                for j in range(1, len(rest) + 1):
                    inl = [k for p in paths for k in ix.defs[p].get(".".join(rest[:j]), ()) if ix.kind[k] == "module"]
                    edges += [
                        (sc, k, "import", "T" if len(inl) == 1 else "N", im.line, "import", im.module) for k in inl
                    ]
            first = (_parts(im.module) or [""])[0]
            internal = bool(mods) or (g != "python" and (first in RELATIVE_ROOTS or ix.crate_head(g, first)))
            one = len(paths) == 1 or len({posixpath.dirname(p) for p in paths}) == 1  # a file, or a package directory
            edges += [(sc, k, "import", "T" if len(mods) == 1 else "N", im.line, "import", im.module) for k in mods]
            if not rest:
                edges += [
                    (sc, ix.paths[p][2], "import", "T" if one else "N", im.line, "import", im.module) for p in paths
                ]
            if not im.names:
                incs += paths
                if im.alias:
                    ks = [k for p in paths for k in ix.defs[p].get(".".join(rest), ())] if rest else []
                    if not rest and pkg:  # java, kotlin: `import a.B as C` binds C to the class B of the file
                        ks = [k for p in paths for k in ix.defs[p].get((_parts(im.module) or [""])[-1], ())]
                    if not ks and loose:  # the package of the declaration is not its directory
                        ks = list(ix.top.get((g, (_parts(im.module) or [""])[-1]), ()))
                    _bind(
                        names,
                        (sc, im.alias),
                        ("sym", tuple(ks)) if ks else (("mod", tuple(paths)) if paths and not rest else ("ext", ())),
                    )
                    continue
                head = _parts(im.module)[:1] or [im.module]
                if paths or (sc, head[0]) not in names:
                    names[(sc, head[0])] = ("plain", ()) if paths else ("ext", ())
                if (
                    g != "python" and head[0] != (_parts(im.module) or [""])[-1]
                ):  # java, rust, go: the last token is the name
                    last = _parts(im.module)[-1]
                    toks = _parts(im.module)
                    inner = (
                        ".".join(toks[len(toks) - len(rest) - 1 :]) if rest and pkg else last
                    )  # p.Outer.Inner: Outer.Inner
                    ks = [k for p in paths for k in ix.defs[p].get(inner, ())]
                    # a package (Kotlin): any of its files may hold the declaration, whatever the file is called
                    if not ks and rest and pkg:
                        ks = [k for p in paths for k in ix.defs[p].get(".".join(rest), ())]
                    far = not ks and loose  # no directory explains the package: the declaration is found by its name
                    ks = ks or (list(ix.top.get((g, last), ())) if far else [])
                    # a nested class or a declaration of a package file: the import uses the file that holds it
                    if ks and (rest or far):
                        held = sorted({ix.where[k] for k in ks})
                        edges += [
                            (sc, ix.paths[p][2], "import", "T" if len(held) == 1 else "N", im.line, "import", im.module)
                            for p in held
                        ]
                    _bind(
                        names,
                        (sc, last),
                        ("sym", tuple(ks)) if ks else (("mod", tuple(paths)) if paths else ("ext", ())),
                    )
                continue
            for raw in im.names:
                nm, as_, al = raw.partition(" as ")
                nm = nm.strip()
                local = al.strip() if as_ else (im.alias if len(im.names) == 1 and im.alias else nm)
                if nm == "*":
                    if mods:  # the children of an inline module bind by their names; an explicit `use` wins
                        for child, ks in ix.children(paths, rest).items():
                            names.setdefault((sc, child), ("sym", tuple(ks)))
                    else:
                        stars += [(sc, p) for p in paths]
                        incs += paths
                    continue
                ks = [k for p in paths for k in ix.defs[p].get(".".join(rest + [nm]), ())]
                if not ks and g == "js" and not rest and nm != "default":  # a barrel: export * from m
                    ks = [k for p in paths for q in ix.star_closure(g, p) for k in ix.defs[q].get(nm, ())]
                sub, srest = ix.resolve_module(g, path, ".".join(x for x in (im.module, nm) if x), im.level)
                if ks:
                    _bind(names, (sc, local), ("sym", tuple(ks)))
                    edges += [(sc, k, "import", "N", im.line, "import-name", nm) for k in ks]
                elif sub and not srest:
                    _bind(names, (sc, local), ("mod", tuple(sub)))
                    edges += [
                        (sc, ix.paths[p][2], "import", "T" if len(sub) == 1 else "N", im.line, "import", nm)
                        for p in sub
                    ]
                else:  # a path of this crate that no def answers (a re-export) is not external: matched by its name
                    # js: a file of the tree exports it, but it is no def (`export const api = new ApiClient()`, `export { api }`
                    # of a const, a re-export through a barrel): the value is read through its members, by their names
                    value = g == "js" and bool(paths) and not rest
                    names[(sc, local)] = ("name", (nm,)) if internal or value else ("ext", ())
        bd = _Bind(names, tuple(stars), (), (), ())
        uses = []
        for r in facts.refs:
            if r.kind == "reg":
                tg, _ = ix.resolve(path, g, bd, r.scope, r.name)
                uses += [(r.scope, r.line, tuple(k for k, _ in tg))] if tg else []
            if r.kind in ("inherit", "impl") and ix.is_class(path, r.scope):
                tg, _ = ix.resolve(path, g, bd, r.scope.rpartition(".")[0], r.name)
                last = (_parts(r.name) or [""])[-1]
                for c in (k for k in ix.defs[path][r.scope] if ix.kind[k] in CLASSY):
                    tg2 = [(k, t) for k, t in tg if k != c]
                    edges += [(r.scope, k, r.kind, t, r.line, "inherit", r.name) for k, t in tg2]
                    bases.append((c, tuple(k for k, _ in tg2), not tg2 and last not in EXEMPT_BASES))
        out.append((path, _Bind(names, tuple(stars), tuple(incs), tuple(edges), tuple(bases), tuple(uses))))
    return out


def _round_b(ix: _Ix, chunk) -> Tuple[list, list, dict, set, list]:
    """Edges of each file: (edges, evidence pool, flag additions {key: bits}, amb keys, findings); Edge.ev is pool-local."""
    edges: List[Edge] = []
    pool: List[Evidence] = []
    memo: Dict[tuple, int] = {}
    flags: Dict[str, int] = {}
    amb: set = set()
    bad: List[Finding] = []

    def emit(path, srcs, dsts, kind, line, rule, text):
        if kind not in EDGE_KINDS:
            bad.append(Finding("E", path, line, "dc.bad-facts", f"ref kind {kind!r} is not an edge kind"))
            return
        site = f"{path}:{line}"
        for k, tier in dsts:
            ev = memo.get((path, line, rule, text))
            if ev is None:
                ev = memo[(path, line, rule, text)] = len(pool)
                pool.append(evidence("link", path, line, f"{rule}:{text}", f"link@{LINKER_V}"))
            for s in srcs:
                edges.append(
                    Edge(s, k, kind, tier, site, ev)
                )  # kind is checked above, tier is one of S T N D by construction
            if tier == "D":
                flags[k] = flags.get(k, 0) | F_DYNAMIC

    def twin(path, srcs, dsts, line, text):
        # a member declared in its class and written out of class (`Foo::run` in a .cpp) is one member
        emit(path, srcs, [(m, "T" if len(dsts) == 1 else "N") for m in dsts], "ref", line, "decl", text)

    for path, facts, bd in chunk:
        g = ix.paths[path][1]
        for scope, dst, kind, tier, line, rule, text in bd.edges:
            emit(path, ix.src_keys(path, scope), [(dst, tier)], kind, line, rule, text)
        for r in facts.refs:
            if r.kind in ("inherit", "impl") and ix.is_class(path, r.scope):
                continue  # linked in round A
            srcs = ix.src_keys(path, r.scope)
            if r.kind in ("exec", "include"):
                emit(path, srcs, ix.path_targets(path, r.name), r.kind, r.line, "path", r.name)
            elif r.kind == "reads":
                emit(path, srcs, [(k, "N") for k in ix.cfg_keys(r.name)], "reads", r.line, "cfg", r.name)
            elif r.kind in xlink.KINDS:  # cross-language: a path string, a native symbol, an environment variable
                tg, rule = xlink.targets(ix, path, g, r)
                emit(path, srcs, tg, r.kind, r.line, rule, r.name)
            else:
                tg, rule = ix.resolve(path, g, bd, r.scope, r.name, r.kind)
                if r.kind == "di":
                    tg, rule = ix.di_targets(tg), "di"
                if rule == "name" and len(tg) > AMB_MAX:
                    amb.update(k for k, _ in tg)
                if g == "js" and r.name.startswith(
                    (RE_ALIAS, NS_ALIAS)
                ):  # a module value handed on (export .. from, require)
                    rule = REEXPORT_RULE
                emit(path, srcs, tg, r.kind, r.line, rule or "none", r.name)
        for sk in (
            ix.defs[path].get("*", ()) if g == "js" else ()
        ):  # `export * from m`: the def `*` is every export of m
            to = [(k, "T") for q in ix.star_closure(g, path) for k in ix.exp.get(q, ())]
            emit(path, [sk], to, "ref", ix.line[sk], REEXPORT_RULE, "export *")
        for srcs, dsts, line, rule, text in xlink.code_edges(
            ix, path, facts
        ):  # generated proto stubs, GraphQL resolver classes
            emit(path, srcs, dsts, "schema", line, rule, text)
        for iface, impl in xlink.implementers(ix, path):  # an interface reaches the types that implement it
            emit(path, [iface], [(impl, "T")], "schema", ix.line[iface], "schema", "implemented by")
        for d in facts.dyn:
            ks, kind = ix.dyn_targets(path, g, bd, d.scope, d.pattern)
            emit(path, ix.src_keys(path, d.scope), [(k, "D") for k in ks], kind, d.line, "dyn", d.pattern)
        fkey = ix.paths[path][2]
        if path in ix.cfg_files:
            for ks in ix.defs[path].values():
                for k in ks:
                    emit(path, [k], [(fkey, "T")], "ref", ix.line[k], "cfg-file", "implicit")
            if path in ix.extern:
                keys = [(k, "N") for ks in ix.defs[path].values() for k in ks if ix.kind[k] == "cfgkey"]
                emit(path, [fkey], keys, "reads", 1, "cfg-extern", "implicit")
        for k in ix.orphans.get(path, ()):
            emit(path, [fkey], [(k, "T")], "ref", ix.line[k], "protocol", "implicit")
        if g in ix.holder_fams:  # a live member keeps the type that holds it
            for qual, ks in ix.defs[path].items():
                tg = ix.holders(path, g, qual)
                decl = [m for c in tg for m in ix.members.get(c, {}).get(qual.rpartition(".")[2], ())]
                for k in ks if tg else ():
                    emit(path, [k], [(c, "T" if len(tg) == 1 else "N") for c in tg], "ref", ix.line[k], "holder", qual)
                    twin(path, [k], [m for m in decl if m != k], ix.line[k], qual)
        for ks in ix.defs[
            path
        ].values():  # build-constraint pairs: whichever twin the build picks is called, the other is the same symbol
            for k in ks if ix.twins else ():
                emit(
                    path,
                    [k],
                    [(m, "T") for m in ix.twins.get(k, ())],
                    "ref",
                    ix.line[k],
                    "buildtag",
                    ix.twins.get(k, [""])[0],
                )
        for ks in (
            ix.defs[path].values() if ix.copies else ()
        ):  # the same module under another root: whichever copy the import loads is used
            for k in ks:
                emit(
                    path,
                    [k],
                    [(m, "N") for m in ix.copies.get(k, ())],
                    "ref",
                    ix.line[k],
                    "copytwin",
                    ix.copies.get(k, [""])[0],
                )
        for k in ix.init.get(path, ()):  # the top-level code of the file runs when any top-level declaration is used
            emit(path, [k], [(ix.paths[path][2], "T")], "ref", ix.line[k], "file-init", "implicit")
            if ix.kind[k] in INITS:  # ... and runs every property initializer: nothing has to name the property
                emit(path, [ix.paths[path][2]], [(k, "T")], "ref", ix.line[k], "file-init", "initializer")
        classes = [k for ks in ix.defs[path].values() for k in ks if ix.kind[k] in CLASSY]
        for ck in classes:  # class level implicit edges
            mems, line = ix.members.get(ck, {}), ix.line[ck]
            every = [m for ms in mems.values() for m in ms]
            emit(path, [ck], [(m, "T") for m in every if ix.flags[m] & F_PROTOCOL], "ref", line, "protocol", "implicit")
            out = ix.outer.get(ck, {})
            loose = [m for ms in out.values() for m in ms if ix.flags[m] & F_PROTOCOL]
            emit(path, [ck], [(m, "N") for m in loose], "ref", line, "protocol", "out-of-class")
            for name in sorted(out.keys() & mems.keys()):
                twin(path, mems[name], out[name], line, name)
            if ix.has_ext(ck):
                emit(path, [ck], [(m, "N") for m in every], "override", line, "external-base", "implicit")
            for name, ks in sorted(mems.items()):
                for a in ix.anc.get(ck, ()):
                    for ak in ix.members.get(a, {}).get(name, ()):
                        emit(path, [ak], [(k, "T") for k in ks], "override", line, "override", name)
                        if ix.kind[a] == "iface" or ix.flags[ak] & F_ABSTRACT:
                            for k in ks:
                                emit(path, [k], [(ak, "T")], "impl", line, "impl", name)
                        for k in ks:
                            flags[k] = flags.get(k, 0) | F_OVERRIDE
    return edges, pool, flags, amb, bad


def _extern(files: Mapping[str, Facts], lg) -> set:
    """Paths of languages with `readers_dir` that have no sibling directory of that name anywhere above a file of the tree."""
    need = {p: (lg.langs.get(f.lang) or {}).get("readers_dir") for p, f in files.items()}
    need = {p: d for p, d in need.items() if d}
    if not need:
        return set()
    have = {("/".join(parts[:i]), parts[i]) for parts in (p.split("/") for p in files) for i in range(len(parts) - 1)}
    return {p for p, d in need.items() if (posixpath.dirname(p), d) not in have}


def _chunks(items: list, workers: int, min_chunk: int) -> List[list]:
    w = [1 + sum(len(getattr(it[1], c)) for c in ("defs", "refs", "imports", "dyn")) for it in items]
    n = max(1, min(workers, sum(w) // max(1, min_chunk)))
    target, out, cur, acc = sum(w) / n, [], [], 0.0
    for it, x in zip(items, w):
        cur.append(it)
        acc += x
        if acc >= target and len(out) < n - 1:
            out.append(cur)
            cur, acc = [], 0.0
    return out + ([cur] if cur else [])


def _par(fn, chunks: List[list], workers: int) -> list:
    kind = "process" if len(chunks) > 1 and workers > 1 else "thread"
    return par.run_units(chunks, fn, jobs=max(1, min(workers, len(chunks))), key=lambda c: c[0][0], kind=kind)


# ---------------------------------------------------------------- entry point
class _Pre(NamedTuple):
    """The serial part of a link (index, nodes, import bindings): what the per-file edge units read."""

    ix: _Ix
    nodes: list
    findings: list
    binds: dict
    workers: int
    min_chunk: int


def _families(lg, key: str) -> frozenset:
    """The language groups whose langs.json entry sets the flag `key` (the flag works per family, as `holder_edge` does)."""
    return frozenset(
        (spec.get("link_group") or spec.get("family") or lang) for lang, spec in lg.langs.items() if spec.get(key)
    )


COPY_GROUP_MAX = (
    24  # files of one name that are looked at for copies (`__init__.py` of a big tree is no copy of anything)
)


def _copy_twins(ix: _Ix, files: Mapping[str, Facts]) -> None:
    """ix.copies: the defs (and file nodes) of two files that are the same module laid out under two roots. Both files are of a copy_twins family,
    the path of one ends with `/` + the path of the other, and their Facts are equal (defs, refs, imports, roots, dyn) with at least one def.
    """
    if not ix.copy_fams:
        return
    for names in ix.byb.values():
        cand = sorted(p for p in names if ix.paths[p][1] in ix.copy_fams and len(files[p].defs) > 1)
        if not 2 <= len(cand) <= COPY_GROUP_MAX:
            continue
        for a in cand:
            fa = files[a]
            for b in cand:
                fb = files[b]
                if a == b or not b.endswith("/" + a) or ix.paths[a][1] != ix.paths[b][1]:
                    continue
                if (fa.lang, fa.defs, fa.refs, fa.imports, fa.roots, fa.dyn) != (
                    fb.lang,
                    fb.defs,
                    fb.refs,
                    fb.imports,
                    fb.roots,
                    fb.dyn,
                ):
                    continue
                for qual in sorted(ix.defs[a].keys() & ix.defs[b].keys()):
                    for ka in ix.defs[a][qual]:
                        ix.copies.setdefault(ka, []).extend(
                            kb for kb in ix.defs[b][qual] if kb not in ix.copies.get(ka, ())
                        )
                    for kb in ix.defs[b][qual]:
                        ix.copies.setdefault(kb, []).extend(
                            ka for ka in ix.defs[a][qual] if ka not in ix.copies.get(kb, ())
                        )


def _prepare(files: Mapping[str, Facts], workers, langs, flags_of_path, min_chunk: int) -> _Pre:
    lg = langs or _langs.load()
    w = par.workers(workers)
    crates = {(s.get("link_group") or s.get("family") or k): s["crate"] for k, s in lg.langs.items() if "crate" in s}
    ix, nodes, findings = _Ix(lg, crates), [], []
    ix.jsext = frozenset(e for v in lg.langs.values() if v.get("family") == "js" for e in v.get("ext", ()))
    order = sorted(files)
    for p in order:
        info = lg.langs.get(files[p].lang) or {}
        fam = info.get("link_group") or info.get("family") or files[p].lang
        nodes += ix.add_file(p, files[p], fam, flags_of_path(p), findings, bool(info.get("file_init")))
    ix.holder_fams, ix.tscope, ix.nons, ix.recv_fams, ix.twin_fams = (
        _families(lg, k) for k in ("holder_edge", "type_scope", "namespaceless", "members_by_receiver", "build_twins")
    )
    ix.copy_fams = _families(lg, "copy_twins")
    ix.implicit_recv = frozenset(lang for lang, spec in lg.langs.items() if spec.get("implicit_receiver"))
    for lang, spec in lg.langs.items():
        fam = spec.get("link_group") or spec.get("family") or lang
        ix.inherit[fam] = ix.inherit.get(fam, frozenset()) | frozenset(spec.get("root_inherit", ()))
    ix.seal()
    _copy_twins(ix, files)
    ix.xl = xlink.index(ix, files)
    ix.cfg_files = {p for p in order if (lg.langs.get(files[p].lang) or {}).get("role") == "config"}
    ix.extern = _extern(files, lg) & ix.cfg_files
    items = [(p, files[p]) for p in order]
    binds = (
        dict(x for part in _par(functools.partial(_round_a, ix), _chunks(items, w, min_chunk), w) for x in part)
        if items
        else {}
    )
    ix.finish(binds)
    return _Pre(ix, nodes, findings, binds, w, min_chunk)


def _edges(pre: _Pre, files: Mapping[str, Facts], paths: Sequence[str]):
    """Round B over `paths`: (edges, evidence pool, flag additions {key: bits}, amb keys, findings), Edge.ev indexes the pool."""
    items = [(p, files[p], pre.binds[p]) for p in paths]
    parts = (
        _par(functools.partial(_round_b, pre.ix), _chunks(items, pre.workers, pre.min_chunk), pre.workers)
        if items
        else []
    )
    edges: List[Edge] = []
    pool: List[Evidence] = []
    add: Dict[str, int] = {}
    amb: set = set()
    bad: list = []
    for es, ev, fl, am, bd in parts:
        off = len(pool)
        edges += [Edge(e[0], e[1], e[2], e[3], e[4], e[5] + off) if e[5] >= 0 else e for e in es] if off else es
        pool += ev
        for k, v in fl.items():
            add[k] = add.get(k, 0) | v
        amb |= am
        bad += bd
    return edges, pool, add, amb, bad


class Partial(NamedTuple):
    nodes: Tuple[Node, ...]  # every node of the given facts (flags included)
    edges: Tuple[Edge, ...]  # the edges that the files `paths` emit (ev = -1), sorted
    findings: Tuple[Finding, ...]


def link_paths(
    files: Mapping[str, Facts],
    paths: Iterable[str],
    *,
    workers: Optional[int] = None,
    langs=None,
    flags_of_path=path_flags,
    min_chunk: int = MIN_CHUNK,
) -> Partial:
    """The index over ALL `files` but the edges of `paths` only: a file's edges are a function of its own facts and the index, so
    this equals the `paths` part of link(files) at a fraction of the cost (the base graph of a branch diff)."""
    pre = _prepare(files, workers, langs, flags_of_path, min_chunk)
    edges, _pool, add, _amb, bad = _edges(pre, files, sorted(set(paths) & set(files)))
    nodes = tuple(
        sorted((n._replace(flags=n.flags | add[n.key]) if n.key in add else n for n in pre.nodes), key=lambda n: n.key)
    )
    return Partial(
        nodes,
        tuple(sorted({Edge(e[0], e[1], e[2], e[3], e[4], -1) for e in edges})),
        tuple(sort_findings(pre.findings + bad)),
    )


def link(
    files: Mapping[str, Facts],
    root_hints: Optional[Mapping[str, Iterable]] = None,
    *,
    workers: Optional[int] = None,
    langs=None,
    flags_of_path=path_flags,
    min_chunk: int = MIN_CHUNK,
) -> Linked:
    """Link {path: Facts} (+ root hints of config files {path: [RootHint]}) into a Graph; see the module docstring."""
    pre = _prepare(files, workers, langs, flags_of_path, min_chunk)
    ix, nodes, findings = pre.ix, pre.nodes, pre.findings
    order = sorted(files)
    edges, pool, add, amb, bad = _edges(pre, files, order)
    findings += bad
    roots, seeds = [], {}  # seeds: class key -> [(hint, origin)] of the roots that its subclasses inherit

    def place(h, origin, k):
        roots.append(root(h.kind, origin, node=k, strength=h.strength, trust=h.trust, stub=h.stub))
        add[k] = add.get(k, 0) | F_ENTRY
        if ix.kind[k] != "file":  # running the file executes its module level
            roots.append(
                root(h.kind, origin, node=ix.paths[ix.where[k]][2], strength=h.strength, trust=h.trust, stub=h.stub)
            )

    srcs = [(p, files[p].roots, True) for p in order] + [(p, h, False) for p, h in sorted((root_hints or {}).items())]
    for src, hints, code in srcs:
        for h in hints:
            if lang_rules.meta_rule(h.kind) is not None:
                continue  # a project annotation type: it hands its rules on (ix.derived) and is no root itself
            if (
                code
                and (h.kind, h.target, h.strength) == ("main", "", "soft")
                and (
                    flags_of_path(src) & F_TEST
                    or posixpath.basename(src).startswith("__init__.")
                    or (ix.paths[src][1] == "js" and posixpath.basename(src).partition(".")[0] == "index")
                )
            ):
                continue  # script statements (target "" = the module) of a test file (`sys.path.insert(..)`), of a package `__init__` (`warnings.filterwarnings(..)`) or of a JS/TS package index (index.js, index.ts, index.d.ts): the test framework or an import runs them, nobody runs the file (package.json main/bin and npm scripts that name an index root it as they do any file)
            if code and h.kind in _class_root_kinds() and not _plugin_scope(src, files[src]):
                continue  # `class ActionModule: def run()` of a file that is no Ansible plugin (data py_class_roots scope): no framework builds it
            origin = f"{src}:{h.line}"
            h = h._replace(target=f"file:{src}") if h.target == "self:" else h
            ks = ix.resolve_target(src, h.target, code)
            ks = [*dict.fromkeys(p for k in ks for p in ix.parts(k))]  # a root on a part of a partial class roots all
            if not ks and selector_scheme(h.target) == "build":
                continue  # `make x` of a doc that no recipe of the tree answers (graph.classreg.bind: no root, no finding)
            try:
                if not ks:
                    sel = (
                        h.target
                        if selector_scheme(h.target)
                        else (f"node:{node_key(files[src].lang, src, h.target)}" if code else f"file:{h.target}")
                    )
                    roots.append(root(h.kind, origin, selector=sel, strength=h.strength, trust=h.trust, stub=h.stub))
                    findings.append(
                        Finding(
                            "E",
                            src,
                            h.line,
                            "dc.root-dangling",
                            f"{h.kind} root {h.target!r} resolves to no node",
                            f"registered at {origin}; no node for {sel} among {len(files)} linked files",
                        )
                    )
                for k in ks:
                    place(h, origin, k)
                    if code and ix.kind[k] in CLASSY and h.kind in ix.inherit.get(ix.paths[src][1], ()):
                        seeds.setdefault(k, []).append((h, origin))
            except ValueError as e:
                findings.append(Finding("E", src, h.line, "dc.bad-facts", f"root hint {h.kind!r} {h.target!r}: {e}"))
    for k, kind, origin in ix.derived:  # the roots that annotation types give to the defs that use them
        roots += [
            root(kind, origin, node=n, strength="hard", trust="convention") for n in (k, ix.paths[ix.where[k]][2])
        ]
        add[k] = add.get(k, 0) | F_ENTRY
    for c in sorted(ix.anc) if seeds else ():  # a subclass of a rooted class: the host builds it by its base
        have = {h.kind for h, _ in seeds.get(c, ())}
        heirs = {(h, o) for a in ix.anc[c] for h, o in seeds.get(a, ())}
        for h, origin in sorted(heirs, key=lambda x: (x[0].kind, x[1])):
            if h.kind not in have:
                have.add(h.kind)
                place(h, origin, c)
    for p in sorted(ix.extern):
        roots.append(root("init", f"{p}:1", node=ix.paths[p][2], strength="soft", trust="convention"))
    nodes = [n._replace(flags=n.flags | add[n.key]) if n.key in add else n for n in nodes]
    return Linked(canonical(nodes, edges, roots, pool), tuple(sorted(amb)), tuple(sort_findings(findings)))
