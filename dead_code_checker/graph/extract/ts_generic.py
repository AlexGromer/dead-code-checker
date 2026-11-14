"""graph.extract.ts_generic: the language-independent tree-sitter extractor.

A language is DATA: graph/data/queries/<grammar>/{defs,refs,roots,stub}.scm (the grammar name is data/langs.json "grammar").
This module only turns query matches into Facts. bash (shell), Go, Rust, JS/TS, C, C++, C#, Java and Kotlin have queries (per-language rules:
data/rules.json, ts_lang.py, queries/<grammar>/rules.json, lang_rules.py), and so do Lua, Ruby, PHP, Scala, Groovy, Nix, Starlark and the
hosts HTML, Vue, Svelte (queries and rules.json only, see rules_of for the keys they use); every other lang that langs.json maps to this extractor raises ExtractorMissing("no
tree-sitter queries ...") until its queries are added. Nothing falls back silently: a missing grammar is
ExtractorMissing too, and probe() / missing_finding() build the finding (W dc.degraded-extractor) for the caller.

Capture vocabulary of the queries (a query file may omit any kind; one without a file yields no matches)
  defs.scm   @def.<kind>   the whole definition (kind = a model.NODE_KINDS name), @name its identifier, @deco a decorator of it; optional
             @recv (the type that holds a method written outside the type: Go receiver, Rust impl header, any type
             expression, named by rules recv_sub; qual = [outer.]Recv.name), @vis (visibility text), @flag.<name> (a
             model FLAG_NAMES flag of the definition). One node and name = one def: the lowest pattern index wins; the names
             of `var A, B = 1, 2` are separate matches of one node, so each is a def (siblings, not nested). Also @mod (a modifier node of a
             definition: public, static, virtual ...; the text is mapped by queries/<grammar>/rules.json to visibility and flags) and
             @vis.<vis> on the def node (an explicit visibility, e.g. a C header prototype is "exported").
  refs.scm   @ref.<kind>   a use (kind = an edge kind: call | ref | include | inherit | impl | exec ...), @name the used
             name, @cmd the command word of an include pattern, @member (also a member of that name: `_.name`), @obj the
             receiver of a member call (name becomes obj.name), @scope the derived type of an inherit/impl (qualified by
             the definition around it), @dyn a dynamic site (a match with only @dyn is a DynSite), @local a name that a
             parameter, a short declaration or a range binds inside a definition (it may shadow an imported package of the
             same name there: `x.f` is then a member call as well as a package function, rules ref_names), @import a whole
             import / use declaration (-> Import facts or package aliases, rules "import"), @attr with @attr.item an outer
             attribute that belongs to the import right after it (rust `#[path = "x.rs"] mod x;`, rules mod_path), @item_macro
             with @item_body the call of a macro whose body holds items (rust cfg_if!, rules item_macros: its `mod` and `use`
             declarations are imports); @ref.exec on a comment: the directive words that look like paths (rules comment_exec).
  refs.scm   also @dyn.<kind> (a computed or reflected use: `using static Util;` is "members:Util", GetMethod("Run") is "name:Run"; the pattern is
             built by the "dyn" rules of queries/<grammar>/rules.json); @member WITHOUT @name is the identifier of a property or field set by
             name on a receiver the text does not show (a C# object initializer `new Foo { Id = 1 }`): the ref is "?.Id", a member of that name
             (link.py), as the ref of a chained call `new B().Run()` is "?.Run"; @scan an opaque node (a C macro body is one preproc_arg token):
             every identifier in its text is a use, a call when a `(` follows; a `##` paste makes a dyn site (tier D, the name is computed). A ref
             on the identifier that DECLARES a definition is not a use (no self edge: a lone dead function stays dead, not a one-node cluster);
             @self on a bare name (rules "self_word": "self" | "this"): inside a class, trait or module the name is also `<self_word>.name`, a member
             of the type or of its ancestors (Ruby, Scala: `base_m` called from a subclass, a mixin method called by the including class)
  roots.scm  @root.marker  a comment; `deadcode: root <why>` makes the def on or just below that line a manual root, and
             rules comment_roots read directive comments (`//export Name`); @root.cand a top-level function with @name
             (rules roots, @pkg = the declared package); @root.attr an outer attribute with @attr its text (rules attrs
             match its path and the paths wrapped in it, rules attr_wrap: the def that follows it is the root;
             @root.span, an impl block, makes it every def inside the block); @root.<kind>[-variant] | @root.annotation.<rule> any node
             whose text passes the "roots" filter of queries/<grammar>/rules.json roots the definition that contains it (the file when there is
             none): main, test, export, init, annotation:<rule>
  imports.scm  one match = one fragment of a statement, grouped by the statement node: @import (import / require / import()),
             @reexport (`export {..} from`, `export * from`); @module the string, @name (+ @alias) a specifier, @default a
             default binding, @ns a whole-module binding, @star the `*`, @req the callee of a require-like call
             (kept only when its text is require, import or URL), @pattern the first argument of such a call (the mapping reads a
             template string, a concatenation, an array and any other expression as a computed specifier, a plain string is @module)
  exports.scm  @export the statement; @name a locally defined name that is exported (+ @alias = the exported name),
             @default the local name exported as default, @obj/@prop the target of a CommonJS assignment (kept only for
             module.exports = x, module.exports = {..}, module.exports.k = x, Object.assign(module.exports, {..}) and exports.x = y;
             @key the k of module.exports.k, @fn the callee of Object.assign)
  xlang.scm  the cross-language facts (exec, lit, ffi, env refs), read by extract.xlang from data/xlang.json: @call + @args, @cmd, @lit,
             @env.r / @env.w, @ffi.decl, @ffi.use (vocabulary in the docstring of that module); a failure of that pass is Facts.errors + 1, the rest of the facts stay
  stub.scm   @stub.def     the whole definition, @body its brace block (text between the braces -> deadcode.generic_body_stub)
  body hash  Def.bh / bsz of a func or method that is no stub: graph.extract.bodyhash.text_body over the text of the definition node (@def.* span) with
             the byte range of its @name left out: comments and whitespace gone, parameters and declared locals renamed (data/lens.json "norm");
             the duplicates lens of the viewer reads it. The text is the node's, not a parse: a language needs no extra query for it
  embed.scm  @script       the code of a block that is written in another language (a <script> of html, vue, svelte), @tag the text of its start
             tag. rules "embed" picks the language of the block from the tag (first matching [regex, lang] of "lang"; lang "" = not code, skipped),
             the block is extracted by that language with the text before it blanked (same lines), its Facts are merged into the file's;
             "roots": the kind of a soft root on every top-level definition of the block (html: an attribute `onclick="go()"` calls it by name)
  defs.scm   @alias + @aliased  `typedef struct tag {..} alias;` (c): alias is a def of its own, flagged protocol (never reported), that uses the tag: a type
             that code names through the typedef (`alias *p`) keeps the struct tag alive, which nothing else ever names
  Only for a language with rules in data/frameworks.json (lang_rules.py: java, kotlin), three more things are read:
  defs.scm   @scope        a region that is no definition (constructor, lambda, initializer): it has its own parameters and locals, its code
                           belongs to the enclosing definition. Variables are kept per REGION (an overload or a constructor shares its
                           qual with another one, not its variables) and seen by position: a field from the whole class body, a
                           parameter or local from its declaration on, the innermost region first.
  defs.scm   @anon         the body of an anonymous class (`new T() {...}`, `object : T {...}`, an enum constant body): a region like @scope
                           whose definitions have no name to be used by: they live as long as the code that holds the body (a method,
                           a field initializer: the class).
  defs.scm   @meta         marks the definition as an annotation type (a Java @interface; a Kotlin `annotation class` is found by the modifier
                           word of lang_rules.<lang>.meta_mods): it hands the rules of its own annotations to the defs that use it
                           (composed stereotypes, see lang_rules: the hints annotation:<rule>.meta)
  defs.scm   @def.var      a variable definition, kept at the TOP LEVEL of a file only (a Kotlin property: its initializer runs with the file
                           and another file uses it by name); a field of a class or a local of a function is dropped by the mapping
  defs.scm   @mods         the modifiers text of the definition: visibility, abstract, @Override, framework annotations (roots:
                           annotation:<rule>, or the rule's own kind, e.g. test), main
  refs.scm   @recv         the receiver of a call: a variable of a known type T makes the ref "T.m" (the interface or base method),
                           an unqualified call also gets "this.m" inside a class (inherited members), and with the rule ext_calls
                           (Kotlin) a call with a receiver also gets the plain call "m" (a top-level extension function)
  types.scm  @var.<field|param|ctor|local> with @type (none: a lambda or catch parameter), @name, @mods: typed variables (receivers) and injection points (refs of
                           kind di: an inject-annotated field, a constructor parameter, the parameters of a @Bean method); the
                           annotation of a parameter may root its method (rules "on": "param", CDI @Observes)
  imports.scm @import      one import statement, read by the "import" regex of the rules; `import static a.B.m` is a use of B in every
                           top-level class of the file
  refs.scm   @ref.reg      the use of an annotation by the def that carries it (the annotation lies in the span of that def): an edge, and the
                           input of the composed-annotation roots of graph.link. The name of a definition (a node that defs.scm captured as
                           @name) and the names inside an import or package statement (imports.scm @import) are never refs, so a
                           catch-all identifier query (Kotlin) adds neither self-edges nor uses by unused imports.
  A def nested in a method (a local class) or in an @anon body (the methods of an anonymous class) gets a ref from the definition that
  holds it, a method or the class: it lives as long as that one.
Language rules are data too: data/queries/<grammar>/rules.json (rules_of: modifier words, default visibility, name rewrites,
roots filters, ref filters, stub tag); C, C++ and C# are queries + rules and no code here. A query file that the
installed grammar does not compile is ExtractorMissing (QueryCompileError), never an empty graph.
Queries carry no predicates (#eq?, #any-of? ...): predicate support depends on the binding version, so the mapping filters by
text (INCLUDE_CMDS, IDENT, data/rules.json via ts_lang) and the same result holds on any binding that has Query and
QueryCursor (tree_sitter >= 0.25).

Queries of a grammar without a directory of its own come from data/langs.json "query_alias" (tsx = typescript); a kind the
alias has is READ FIRST and the grammar's own file of that kind is appended (tsx/refs.scm adds only the JSX patterns).

Facts (javascript, typescript, tsx)
  defs     functions, classes, methods of a class (the queries match a class body only; a method of an object literal or a class
           expression is no symbol and the extractor drops it too: its body belongs to the enclosing scope), `const f = () => ..`
           (arrow functions only: a function expression has no stable node name across grammar versions), TS interfaces/enums; a name that is not a plain identifier (computed key) is skipped;
           members are public (private when the name starts with "_" or "#"); a top-level name is "exported" when it is
           `export`ed or put on module.exports, else "private" (a module's own).
           An alias def (kind const, vis exported, a ref to the target) is added for `export default x`, `export = x`, `export {a as b}`,
           `module.exports = x`, `module.exports.n = x`, `exports.n = x` and for each name of `export {a as b} from m` (that one also
           carries an Import of m scoped to the alias and the ref `__re_b.a`): an importer binds to the alias, the alias reaches the
           target. An exported name that is an import binding (`import { a } from m; export { a }`, `export default a`) is the same:
           the alias def hands m's `a` on through a hidden import. `export * from m` adds the def `*` (const, exported): link makes
           it reach every export of m. `export * as ns from m` adds the alias def `ns`, an Import of m scoped to it under the hidden
           name `__re_ns` and a value use of that name: the alias reaches every export of m.
           Decorators: a decorated class or member is an annotation root (`annotation:<decorator>`, hard, convention; flag decorated)
           unless the decorator registers nothing (data js_safe_decorators); a method whose name the runtime or a framework calls
           (data js_members: any class, or a file that imports the framework) is flagged protocol. In a declaration file (*.d.ts,
           data js_ambient_files) every top-level definition is exported. The methods of a class decorated by a template-bound
           decorator (data js_template_decorators: Angular @Component, @Directive) are soft roots of kind template.
  refs     call (f(), a.b(), new C(), <Comp/>), ref (an identifier passed, returned, assigned, put in an object or array, a TS
           type use), inherit / impl (extends / implements; a class expression has no def, `export default class extends B {}` and
           `const A = class extends B {}` use B from the enclosing definition). Optional chaining `a?.b` reads as `a.b`.
  imports  ES import (default is the name "default as X", `* as ns` is an alias), require() and await import() (a declarator
           binds the whole module or the destructured names), TS `import x = require(m)`, `new URL("./w.js", import.meta.url)`;
           a re-export is an import scoped to its alias. A require() or import() that nothing binds (`require(m).f()`,
           `import(m).then(..)`, `use(require(m))`) uses the namespace as a whole: an Import under the hidden alias `__ns<byte>` and a
           value use of it (link: every export of m is reachable, tier N). A computed specifier (`import(`./l/${x}.js`)`,
           `require("./c/" + n)`, `require(path.join(__dirname, "h", n))`, `import.meta.glob([..])`, `require(name)`) is the DynSite
           `import:<glob>` (link: each file the glob names, with its exports; a head that is computed too gives `*` = every file under
           the directory of the caller, never an unmarked call; a head that is an alias (`@/l/${x}`, `~/..`, `#..`, `$lib/..`, `@org/..`)
           or a base-url path (`components/${n}`) is looked up as a path suffix, `/*/l/*`, like the static import of the same spelling).
           A receiver that is no plain name (`new A().m()`, `f().m`, `a[i].m`, `(x as T).m`) is recorded as "~.m": any member m.
           A member value (`on("x", this.h)`, `return o.v`: accessor reads) is a weak ref, but not a callee or an inner object.
           A declaration's own name is never a use. Link side: `export * from m` is followed for named imports of the barrel;
           `const f = require(m); f()` reaches the file and its `default` alias (module.exports = f); a class's `constructor` is
           a protocol member.
           Known gap: class fields (`h = () => {}` is no def; their bodies belong to the class).

Seams: set_backend(fn) replaces the parser (tests): fn(grammar, data: bytes) -> object with .errors (int) and .run(kind) ->
[(pattern_index, {capture: [node]})] where a node has start_byte, end_byte, start_point and end_point ((row, col), 0-based).
The real backend is graph.ts.get_parser + tree_sitter.Query/QueryCursor.matches (APIs read from the tree_sitter 0.26.1 stubs); a
grammar that is not installed is ExtractorMissing, and graph.ts fetches nothing unless it is told to.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import sys
import threading
from bisect import bisect_right
from functools import lru_cache
from typing import Callable, Dict, List, Optional, Set, Tuple

from .. import langs as dclangs
from .. import ts as tsm
from ..contracts import ExtractorMissing, Extractor, register_extractor
from ..model import (
    ALIAS_MARKS,
    EDGE_KINDS,
    F_DECORATED,
    F_OVERRIDE,
    F_PROTOCOL,
    F_STUB,
    FLAG_NAMES,
    NODE_KINDS,
    NS_ALIAS,
    RE_ALIAS,
    ROOT_KINDS,
    VIS,
    Def,
    DynSite,
    Facts,
    Finding,
    Import,
    Ref,
    RootHint,
    flags_of,
)
from . import bodyhash, lang_rules
from . import ts_lang as tl
from . import xlang as xl
from .common import generated_sha, text_checker, mark_generated, merge_defs, module_def, protocol_member

QUERY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "queries")
QUERY_KINDS = ("defs", "refs", "roots", "stub", "imports", "exports", "types", "xlang", "embed")
# types.scm variable kinds; when one declaration matches twice (a constructor parameter is also a parameter) the first kind wins
CLASSY = ("class", "iface")
TYPES = (*CLASSY, "module")  # the definitions whose members a bare name may denote (a Ruby module is no class)
VARS = ("var", "const")  # defs of a variable: kept at the top level of a file only (java, kotlin)
REGIONS = ("scope", "anon")  # defs.scm captures of a region that is no definition
SEEN_WHOLE = CLASSY + ("anon",)  # a field is seen from the whole body of its class (or anonymous class)
VAR_KINDS = ("ctor", "field", "param", "local")
INCLUDE_CMDS = {"shell": frozenset({".", "source"})}  # commands whose first argument is a file that gets included
JS_LANGS = frozenset({"javascript", "typescript", "tsx"})
JS_NAME = re.compile(r"^[A-Za-z_$#][\w$#]*(\.[A-Za-z_$#][\w$#]*)*$")
LAST_MEMBER = re.compile(r"\.([A-Za-z_$#][\w$#]*)$")
NS_CALLS = frozenset({"require", "import"})  # a call that yields the namespace of the module it names
# calls whose first argument names a file of ours: new URL("./w.js", import.meta.url), require.resolve("./x"), a bundler glob
REQUIRE = NS_CALLS | {"URL", "require.resolve", "require.context", "import.meta.glob", "import.meta.globEager"}
ROOTED = ("./", "../", "/")  # a specifier that names a file of ours (a path), not a package
COMMENTS = re.compile(r"\A(?:\s*(?:/\*.*?\*/|//[^\n]*))+", re.S)  # the comments in front of a call argument
STR = re.compile(r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`""")  # a string or template literal
DECORATOR = re.compile(r"@\s*([A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)")
STUB_TAG = {
    "shell": "sh",
    "go": "go",
    "rust": "rs",
    "javascript": "js",
    "typescript": "js",
    "tsx": "js",
}  # core.generic_body_stub tags
WORD = re.compile(r"[A-Za-z_]\w*(\s*\()?")  # an identifier of an opaque @scan text, group 1 = a call
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,79}$")
DOTTED = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
MARKER = re.compile(r"deadcode:\s*root\b")
_QUALIFIER = re.compile(r"((?:[A-Za-z_]\w*\.)+)$")  # the `a.b.` before a name
DYN_MAX = 60

# extractors whose grammar the probe checks (data/langs.json "extractor"): ts_generic, cfg (HCL), cfgts and tpl (config languages), schema (sql, proto, graphql)
GRAMMAR_EXTRACTORS = ("ts_generic", "cfg", "cfgts", "tpl", "schema")
_backend: Optional[Callable] = None
_local = threading.local()


def set_backend(fn: Optional[Callable]) -> None:
    """Tests: replace the parser backend (None = the real one)."""
    global _backend
    _backend = fn


# ------------------------------------------------------------------ real backend
def _files(grammar: str, kind: str) -> List[str]:
    """The .scm files of one kind: those of the query_alias grammar first, then the grammar's own."""
    alias = dclangs.load().query_alias.get(grammar)
    names = [g for g in (alias, grammar) if g]
    return [p for p in (os.path.join(QUERY_DIR, g, kind + ".scm") for g in names) if os.path.isfile(p)]


def _queries(grammar: str) -> List[str]:
    return sorted(k for k in QUERY_KINDS if _files(grammar, k))


def _query_text(grammar: str, kind: str) -> Optional[str]:
    parts = []
    for p in _files(grammar, kind):
        with open(p, encoding="utf-8") as fh:
            parts.append(fh.read())
    return "\n".join(parts) if parts else None


@lru_cache(maxsize=16)
def queries_sha(grammar: str) -> str:
    h = hashlib.sha256()
    for k in _queries(grammar):
        h.update(k.encode() + b"\0" + (_query_text(grammar, k) or "").encode() + b"\0")
    h.update(
        json.dumps(rules_of(grammar), sort_keys=True).encode()
    )  # the rules decide the facts as much as the queries do
    return h.hexdigest()


class QueryCompileError(Exception):
    """A query file the installed grammar rejects (a node or field name it does not have)."""


class _TsParsed:
    def __init__(self, grammar: str, mod, language, root):
        self.grammar, self.mod, self.language, self.root = grammar, mod, language, root
        self.errors = int(bool(root.has_error))

    def run(self, kind: str):
        cache = getattr(_local, "queries", None)
        if cache is None:
            cache = _local.queries = {}
        key = (self.grammar, kind)
        hit = cache.get(key)
        if hit is None or hit[0] is not self.language:  # a Query belongs to its Language: a reloaded grammar recompiles
            text = _query_text(self.grammar, kind)
            if text is None:
                hit = (self.language, None)
            else:
                try:
                    hit = (self.language, self.mod.Query(self.language, text))
                except (
                    Exception
                ) as exc:  # noqa: BLE001 - tree_sitter.QueryError and friends: the query does not fit this grammar
                    raise QueryCompileError(
                        f"{self.grammar}/{kind}.scm does not compile on the installed grammar: {exc}"
                    ) from exc
            cache[key] = hit
        q = hit[1]
        return [] if q is None else self.mod.QueryCursor(q).matches(self.root)


def _real_backend(grammar: str, data: bytes):
    parser = tsm.get_parser(grammar)  # LookupError: tree-sitter, the pack or the grammar is not there
    language = parser.language
    mod = sys.modules.get(type(language).__module__)
    if mod is None or not hasattr(mod, "QueryCursor") or not hasattr(mod, "Query"):
        raise LookupError("tree_sitter without Query/QueryCursor (>= 0.25 is required)")
    return _TsParsed(grammar, mod, language, parser.parse(data).root_node)


# ------------------------------------------------------------------ findings
def missing_finding(lang: str, path: str, cfg: Optional[dict] = None) -> Optional[Finding]:
    """None when the grammar of `lang` is usable (or the language needs none); else W dc.degraded-extractor that says what to install:
    the files of the language are not extracted at all and stay out of the graph. `cfg` is unused (nothing is required, so nothing is waived)."""
    info = dclangs.load().langs.get(lang) or {}
    g, ext = info.get("grammar"), info.get("extractor")
    # python (ast), powershell (its own worker) and yaml (yamlite) are read without a grammar
    if not g or ext not in GRAMMAR_EXTRACTORS or info.get("native"):
        return None
    res = tsm.grammar_status(g)
    if res.status == "ok":
        return None
    return Finding(
        "W",
        path,
        1,
        "dc.degraded-extractor",
        f"{lang}: {res.detail}; the {lang} files are not extracted and stay out of the graph",
    )


def probe_grammar(lang: str, cfg: Optional[dict] = None) -> Optional[Finding]:
    """What Extractor.probe returns for an extractor that reads a grammar: missing_finding for a file that is no file."""
    return None if _backend is not None else missing_finding(lang, f"(grammar of {lang})", cfg)


# ------------------------------------------------------------------ matches -> facts
def _text(data: bytes, n) -> str:
    return data[n.start_byte : n.end_byte].decode("utf-8", "replace")


def _js_name(text: str) -> str:
    """The name a JS/TS use is recorded under: a dotted identifier as written (`a?.b` = `a.b`); for a callee or value whose
    receiver is no plain name (`new A().m`, `f().m`, `a[i].m`, `(x as T).m`, `x!.m`) the member alone on an unknown receiver
    ("~.m": the linker takes it for any member named m). "" = nothing to record."""
    name = re.sub(r"\s*\??\.\s*", ".", text)
    if JS_NAME.match(name):
        return name
    m = LAST_MEMBER.search(name)
    return f"~.{m.group(1)}" if m else ""


@lru_cache(maxsize=1)
def _js_data() -> Tuple[frozenset, tuple, frozenset, "re.Pattern", str, frozenset]:
    """frameworks.json (graph.roots): (members any class gets, ((module glob, members), ..), decorators that register nothing,
    ambient file pattern, sha256 of that data: part of the cache key of a JS shard, decorators of a class that a template reads).
    """
    from .. import roots as dcroots
    f = dcroots.fw()
    m, safe = f["js_members"], f["js_safe_decorators"]
    tpl = f["js_template_decorators"]
    sha = hashlib.sha256(json.dumps([m, safe, tpl, f["js_ambient_files"]], sort_keys=True).encode()).hexdigest()
    by = tuple(sorted((k, frozenset(v)) for k, v in m["by_module"].items()))
    return frozenset(m["any"]), by, frozenset(safe), re.compile(f["js_ambient_files"]), sha, frozenset(tpl)


def _protocol(imports) -> frozenset:
    """Method names the runtime or a framework calls: the ones of any class plus those of the frameworks this file imports."""
    any_, by = _js_data()[:2]
    mods = [i.module for i in imports]
    return any_.union(
        *(v for pat, v in by if any(fnmatch.fnmatchcase(m, pat) or m.startswith(pat + "/") for m in mods))
    )


def _glob_of(out: str) -> str:
    """A computed specifier as a glob (`*` = a computed part): a relative or root path as written; a computed head followed by a path
    is that path at any depth (`${root}/x/${n}.js` = `/*/x/*.js`: the linker also takes x/ at the top); a computed head alone is `*`
    (everything under the directory of the file). Any other head is looked up as a path suffix, like a static import: an alias marker
    (`@/locales/${l}.js`, `~/plugins/${n}`, `#utils/${n}`, `$lib/${n}`, `@org/ui/${n}`) is dropped first (graph.model ALIAS_MARKS: no tsconfig is
    read), a base-url or package path (`components/${n}`) is kept: `/*/locales/*.js`. A name no tree directory has names no file.
    """
    out = re.sub(r"\*+", "*", out)
    if out.startswith(ROOTED):
        return out
    segs = out.split("/")
    if len(segs) > 1 and segs[0][:1] in ALIAS_MARKS and segs[0] != "@types":
        out = "/".join(segs[1:])
    if out.startswith("*"):
        return "*" if "/" not in out else "/" + out
    return "/*/" + out if out else ""


def _spec_of(out: str) -> List[str]:
    """One specifier text (`*` = a computed part): a module or path without a computed part as written, else its glob."""
    return [out] if out and "*" not in out else [g for g in [_glob_of(out)] if g]


def _specs(text: str) -> List[str]:
    """The files the first argument of a require-like call names when it is no plain string literal (that one is @module), as paths and
    globs; [] = nothing of ours. A template literal or a concatenation (`./l/${x}.js`, `"./c/" + n`, `@/l/${x}.js`) is its text with `*`
    for the computed parts; an array (`import.meta.glob(["./a/*.ts", "!./a/x.ts"])`) every positive pattern; any other expression (`name`,
    `path.join(__dirname, "h", n)`) the string literals it holds joined into a path, relative to the file when it spells `__dirname` or a
    `./` path, else at any depth, `*` when it holds none: the call can load any file under the directory of the caller.
    """
    # `import(/* webpackChunkName: "x" */ `./l/${n}.js`)`: the comment is no part of the specifier
    t = COMMENTS.sub("", text).strip()
    if not t:
        return []
    if t[0] == "[":
        lits = (m[1:-1] for m in STR.findall(t))
        return sorted(
            {
                x
                for lit in lits
                if not lit.startswith("!") and (lit.startswith(ROOTED) or "*" in lit)
                for x in _spec_of(lit)
            }
        )
    lits = [m[1:-1] for m in STR.findall(t)]
    if t[0] in "\"'`":
        if STR.fullmatch(t):  # one literal: a plain string is @module, a template is its text (a module) or a glob
            return _spec_of(re.sub(r"\$\{[^}]*\}", "*", t[1:-1]) if t[0] == "`" else "")
        parts = [p.strip() for p in t.split("+")]
        return _spec_of("".join(p[1:-1] if len(p) > 1 and p[0] in "\"'" and p[-1] == p[0] else "*" for p in parts))
    if not lits:
        return ["*"]
    path = "/".join(x.strip("/") for x in lits) + ("" if t.rstrip(") \n")[-1:] in "\"'`" else "/*")
    rel = lits[0].startswith(".") or re.search(r"__dirname|__filename|import\.meta", t)
    return [path if lits[0].startswith(ROOTED) else f"./{path}" if rel else f"/*/{path}"]


def _cjs_pairs(caps_text, default: str, name: str, alias: str):
    """(exported name, local name) pairs of a CommonJS assignment: module.exports = x | {..}, module.exports.k = x,
    Object.assign(module.exports, {..}), exports.k = x."""
    o, p, key = caps_text("obj"), caps_text("prop"), caps_text("key")
    if (o, p) == ("module", "exports"):
        if key:
            return [(key, default)] if default else []
        return [("default", default)] if default else [(alias or x, x) for x in [name] if x]
    return [(p, default)] if o == "exports" and default else []


def _kind(key: str, prefix: str, allowed, what: str) -> str:
    kind = key[len(prefix) :]
    if kind not in allowed:
        raise ValueError(f"queries: {key}: {kind!r} is not a {what}")
    return kind


@lru_cache(maxsize=16)
def rules_of(grammar: str) -> dict:
    """data/queries/<grammar>/rules.json (language rules as data), {} when the grammar has none. Keys (all optional)
    vis_mods {word: vis}            a @mod word that sets the visibility (the first one in the source wins)
    flag_mods {word: flag name}     a @mod word that sets a model.FLAG_NAMES flag (abstract, override ...)
    default_vis {kind|"*": vis}     visibility without a modifier (when the key is absent: private for a "_" name, else public)
    parent_default_vis {kind: vis}  the default of a member of that kind of definition (an interface member is public)
    name_map [[old, new]]           replacements in a def or ref name ("::" -> ".", "->" -> ".")
    strip_angles bool               template arguments are dropped from a def or ref name (`Foo<T>::Foo` is Foo.Foo)
    dotted_names bool               a call/ref name that is not a dotted identifier keeps only its last word, as "?.word" (a member of an
                                    unnamed receiver: `new B().Run()`, `a.F().G`, `x?.m`, `p->get()->run()`; link.py resolves it by member name)
    ref_cmd {kind: regex}           a ref match with a @cmd capture is kept only when the text of @cmd matches
    roots {suffix: {re, strength, kinds, flag}}  the @root.<suffix> captures: kept when the text of the node matches `re` and the def that
                                    contains it is of one of `kinds` (def kinds: var, method ...); suffix <kind>[-variant] (main, test, export,
                                    init: `main-toplevel` is another filter for kind main) or annotation.<rule>; the def that contains the node
                                    (else the file) is the root. With `flag` (a model.FLAG_NAMES name) there is NO root: the def gets the flag
                                    (`protocol`: an attribute on a property or field says a framework reads it by reflection). With `host` the classes
                                    and objects around that definition are roots too (`object Main { def main(..) }`: the host is started with it)
    protocol [{re, kinds, vis, parents, skip_mods}]  a def that satisfies every key of one rule is a protocol member (flag protocol: alive with
                                    its type, link.py): `re` is searched in the qualified name, kinds are the def kinds, vis the visibility, parents
                                    the kind of the enclosing def, skip_mods modifier words that exclude it. Data members of a class that a serializer,
                                    a model binder or an ORM reads by name, the members a language calls by convention (GetEnumerator, begin, ...)
    dyn {kind: {re, cmd, cmd_alias, pick}}  a @dyn.<kind> capture is a dyn site "<kind>:<group 1 of re><pick>" (members:Util: every member of Util may be
                                    used by its bare name; name:Run: a member called Run; pick "/Invoke*": only the members of the type whose
                                    name matches the glob, `members:Mw/Invoke*`); the match needs a @cmd capture that matches `cmd`; with `cmd_alias` the @cmd
                                    text must also be the name of an imported package of the file (go: `dep.F(T{})` hands a T to another package, not to a local value)
    stub_tag str                    the deadcode.generic_body_stub tag of the language
    self_word str                   the receiver word of the language (ruby "self", scala "this"): a ref match with @self on a bare name inside a
                                    class, trait or module is also `<word>.name`, a member of the type or of its ancestors, unless a parameter or
                                    local (@local) binds the name or a nested def of that name is nearer
    def_kw {kind: re}               a @def.<kind> match is a definition only when its @kw capture (the keyword word before the name: groovy
                                    `class`, `interface`) matches re; a loose grammar has one node for every statement
    path_sub {kind: [[re, rep]]}    applied to the name of a ref of that kind (include): the spelling of a file reference in this language ->
                                    a path the linker finds (lua `a.b` -> a/b.lua, a Bazel label //pkg:f.bzl -> pkg/f.bzl); a name that ends up empty is no ref
    component_tags bool             a ref of kind ref is a template tag (vue / svelte): <MyComp> and <my-comp> (also recorded as MyComp) are components,
                                    a lowercase name without a hyphen (<b>, <div>) is an element and no use
    ref_cmd {include|dyn|scan: re}  an include pattern, a plain @dyn site or a @scan node with a @cmd is kept when @cmd matches re (shell has the fixed
                                    INCLUDE_CMDS instead; a @dyn or @scan pattern without @cmd is dropped when the rule is set)
    scan_before re                  a @scan node is script text only when the 24 characters before it match re (shell: `bash -c '...'`, `xargs ... sh -c "..."`)
    scan_value {cmd re: value re}   a @scan node whose @cmd matches the key (and that ref_cmd.scan does not keep) is script text only when its value starts
                                    with the value re, the words after it are the uses (html: `href="javascript:go()"`)
    path_roots [{path, kind, kinds?, strength?}]  a file whose repo-relative path matches `path` roots every definition (of one of `kinds`) in it,
                                    a framework that calls them by convention (Rails controllers); a root of the kind, trust convention
    embed {lang: [[re, lang]], roots?}  see embed.scm above"""
    path = os.path.join(QUERY_DIR, grammar or "", "rules.json")
    if not grammar or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _name(rules: dict, text: str) -> str:
    text = text.strip("\"'")
    if rules.get(
        "strip_angles"
    ):  # `Foo<T>::get`: the template arguments are not part of the name (`operator<<` keeps its own)
        head, op, tail = text.partition("operator")
        while re.search(r"<[^<>]*>", head):
            head = re.sub(r"<[^<>]*>", "", head)
        text = head + op + tail
    for old, new in rules.get("name_map", ()):
        text = text.replace(old, new)
    return text


def _path_name(rules: dict, kind: str, name: str) -> str:
    """rules path_sub: the spelling of a file reference of this language as a path (applied to the refs of that kind only)."""
    for pat, rep in rules.get("path_sub", {}).get(kind, ()):
        name = re.sub(pat, rep, name)
    return name


def _hosts(kinds: Dict[str, str], qual: str) -> List[str]:
    """The classes and traits around the definition `qual`, innermost first (a rule with "host": the object that holds `main` is started with it)."""
    out = []
    while "." in qual:
        qual = qual.rpartition(".")[0]
        if kinds.get(qual) in CLASSY:
            out.append(qual)
    return out


def _passes(rule: dict, text: str, kind: str) -> bool:
    """A @root.<suffix> node is kept when its text passes the rule's `re` and the definition that holds it has one of its `kinds`."""
    return bool(not rule.get("re") or re.search(rule["re"], text)) and (not rule.get("kinds") or kind in rule["kinds"])


def _modules(data: bytes, parsed, line, scope_of, local: frozenset = frozenset()):
    """imports.scm and exports.scm -> (imports, alias defs, alias refs, exported top-level names, dynamic sites); `local` = the
    top-level names this file defines (an exported name that is not one of them but an import binding is handed on)."""
    txt = lambda caps, k: _text(data, caps[k][0]) if caps.get(k) else ""  # noqa: E731
    groups: Dict[tuple, dict] = {}
    for _, caps in parsed.run("imports"):
        req = txt(caps, "req")
        if caps.get("req") and req not in REQUIRE:
            continue  # f("x") is no import
        lit, specs = txt(caps, "module").strip("\"'`"), _specs(txt(caps, "pattern"))
        if req == "require.context" and lit:  # a directory: every file under it
            lit, specs = "", _spec_of(lit.rstrip("/") + "/*")
        elif "*" in lit:  # import.meta.glob("./pages/*.tsx", "@/pages/*.vue")
            lit, specs = "", _spec_of(lit)
        path_ = next(
            (x for x in specs if "*" not in x), ""
        )  # a template literal without substitutions is a module path
        globs = [x for x in specs if "*" in x]
        # `const p = import(m)`: p holds a promise, not the namespace (only `await import(m)` binds it)
        promise = req == "import" and not caps.get("await")
        for key in ("import", "reexport"):
            for n in caps.get(key, ()):
                g = groups.setdefault(
                    (n.start_byte, key),
                    {
                        "n": n,
                        "re": key == "reexport",
                        "mod": "",
                        "at": -1,
                        "call": False,
                        "globs": set(),
                        "names": set(),
                        "ns": set(),
                    },
                )
                g["mod"] = g["mod"] or lit or path_
                g["at"] = caps["module"][0].start_byte if caps.get("module") else g["at"]
                g["call"] = g["call"] or req in NS_CALLS
                g["globs"].update(globs)
                if promise:
                    continue
                if caps.get("name"):
                    al = txt(caps, "alias")
                    g["names"].add((caps["name"][0].start_byte, txt(caps, "name") + (f" as {al}" if al else "")))
                if caps.get("default"):
                    g["names"].add((caps["default"][0].start_byte, f"default as {txt(caps, 'default')}"))
                if caps.get("star"):
                    g["names"].add((caps["star"][0].start_byte, "*"))
                g["ns"].update(_text(data, x) for x in caps.get("ns", ()))
    imports: Set[Import] = set()
    defs: List[Def] = []
    refs: Set[Ref] = set()
    dyn: Set[DynSite] = set()
    # local name an import binds -> (module, the name it takes from it; "" = the whole module)
    bind: Dict[str, Tuple[str, str]] = {}
    reexported = {g["n"].start_byte for g in groups.values() if g["re"]}
    bound = {g["at"] for g in groups.values() if g["names"] or g["ns"]}  # a module node that some declarator binds
    used: Set[int] = set()  # module nodes whose namespace is used as a whole
    for g in sorted(groups.values(), key=lambda g: (g["n"].start_byte, g["re"])):
        mod, n = g["mod"], g["n"]
        ln, sc = line(n), scope_of(n.start_byte)
        # require("./c/" + name): any file the glob names (link: dyn pattern import:<glob>)
        for gl in sorted(g["globs"]):
            dyn.add(DynSite(f"import:{gl}", ln, sc))
        if not mod:
            continue
        names = tuple(nm for _, nm in sorted(g["names"]))
        if not g["re"]:
            for nm in names:
                src, _, al = nm.partition(" as ")
                bind[al or src] = (mod, src)
            bind.update((ns, (mod, "")) for ns in g["ns"])
            if names:
                imports.add(Import(mod, names, "", ln, 0, sc))
            imports.update(Import(mod, (), ns, ln, 0, sc) for ns in g["ns"])
            if not names and not g["ns"]:
                # `require(m).f()`, `import(m).then(..)`, `use(require(m))`: nothing binds the namespace, so it is used as a
                # whole (a hidden alias and a value use of it: link makes every export of m reachable, tier N); the declarator
                # of a promise and the call inside it are one use
                whole = g["call"] and g["at"] not in bound
                if whole and g["at"] in used:
                    continue
                used.add(g["at"])
                alias = f"{NS_ALIAS}{n.start_byte}" if whole else ""
                imports.add(Import(mod, (), alias, ln, 0, sc))
                if alias:
                    refs.add(Ref(alias, "ref", ln, sc))
            continue
        for ns in sorted(g["ns"]):  # export * as ns from m: the alias def `ns` is the whole namespace of m
            imports.add(Import(mod, (), RE_ALIAS + ns, ln, 0, ns))
            defs.append(Def(ns, "const", ln, n.end_point[0] + 1, "exported", 0, -1, ""))
            refs.add(Ref(RE_ALIAS + ns, "ref", ln, ns))
        for nm in names:
            src, _, al = nm.partition(" as ")
            if src == "*":  # export * from m: the file imports m whole; the def `*` stands for every export of m (link)
                imports.add(Import(mod, ("*",), "", ln, 0, ""))
                defs.append(Def("*", "const", ln, n.end_point[0] + 1, "exported", 0, -1, ""))
                continue
            local = al or src  # a def of its own, reached by importers; it reads m through a hidden namespace import
            imports.add(Import(mod, (), RE_ALIAS + local, ln, 0, local))
            defs.append(Def(local, "const", ln, n.end_point[0] + 1, "exported", 0, -1, ""))
            refs.add(Ref(f"{RE_ALIAS}{local}.{src}", "ref", ln, local))
    exported: Set[str] = set()
    for _, caps in parsed.run("exports"):
        node = (caps.get("export") or [None])[0]
        if node is None or node.start_byte in reexported or (caps.get("fn") and txt(caps, "fn") != "Object.assign"):
            continue
        default = txt(caps, "default")
        if caps.get("obj"):
            # CommonJS: module.exports = x | {..} | .k = y, Object.assign(module.exports, {..}), exports.x = y
            pairs = _cjs_pairs(lambda k: txt(caps, k), default, txt(caps, "name"), txt(caps, "alias"))
        else:
            pairs = ([("default", default)] if default else []) + [
                (txt(caps, "alias") or x, x) for x in [txt(caps, "name")] if x
            ]
        for alias, target in pairs:
            exported.add(target)
            ln = line(node)
            if target in bind and target not in local:
                # `import { a } from m; export { a }`, `export default a`: an alias def hands m's `a` on like `export { a } from m`
                mod, src = bind[target]
                defs.append(Def(alias, "const", ln, node.end_point[0] + 1, "exported", 0, -1, ""))
                imports.add(Import(mod, (), RE_ALIAS + alias, ln, 0, alias))
                refs.add(Ref(RE_ALIAS + alias + (f".{src}" if src else ""), "ref", ln, alias))
            elif alias != target:
                defs.append(Def(alias, "const", ln, node.end_point[0] + 1, "exported", 0, -1, ""))
                refs.add(Ref(target, "ref", ln, alias))
    return imports, defs, refs, exported, dyn


def _js_defs(defs: List[Def], exported: Set[str], proto: frozenset, ambient: bool) -> List[Def]:
    """A module's top level that is not exported is private to the file (a declaration file declares for the whole program); a
    method the runtime or a framework calls (`proto`) is a protocol member: a live class keeps it alive."""
    out = []
    for d in defs:
        if "." not in d.qual:
            d = d._replace(vis="exported" if ambient or d.qual in exported else "private")
        elif d.kind == "method" and d.qual.rpartition(".")[2] in proto:
            d = d._replace(flags=d.flags | F_PROTOCOL)
        out.append(d)
    return out


def _body(lang: str, data: bytes, span: Tuple[int, int], name: Optional[Tuple[int, int]]) -> Tuple[str, int]:
    """The normalised body hash of the definition node `span` (graph.extract.bodyhash: its text, the name left out)."""
    raw = data[span[0] : span[1]]
    cut = None
    if name and span[0] <= name[0] < name[1] <= span[1]:
        a = len(raw[: name[0] - span[0]].decode("utf-8", "replace"))
        cut = (a, a + len(raw[name[0] - span[0] : name[1] - span[0]].decode("utf-8", "replace")))
    return bodyhash.text_body(lang, raw.decode("utf-8", "replace"), cut)


def _facts(lang: str, text: str, data: bytes, parsed, path: str = "") -> Facts:
    line = lambda n: n.start_point[0] + 1  # noqa: E731
    js = lang in JS_LANGS
    jl = lang_rules.get(
        lang
    )  # data/frameworks.json (java, kotlin: modifiers, annotations, typed variables, imports); None = none
    rl = tl.rules(lang)  # data/rules.json (go, rust: imports, receivers, attribute roots); {} = none
    rules = rules_of(
        dclangs.load().grammar(lang) or ""
    )  # data/queries/<grammar>/rules.json (c, c++, c#: modifiers, protocol, dyn)
    blank = rl.get("blank_names", ())  # names that bind nothing (go, rust `_`); a shell function may be called _
    # definitions: whole-node spans, qualified by the enclosing definitions (outermost first); one node, one def:
    # the pattern with the lowest index claims it
    spans: Dict[Tuple[int, int, int], tuple] = {}  # (start, end, start of the name) -> def
    mods: List[Tuple[int, str]] = (
        []
    )  # (position, text) of every @mod: modifier words of the definition they are written on
    explicit: Dict[Tuple[int, int], str] = {}  # span of a def node -> its @vis.<vis>
    declared: Set[Tuple[int, int]] = set()  # spans of the identifiers that name a definition
    owned: Set[int] = set()  # start bytes of the name and recv nodes (a use at a def's own name is no reference)
    decos: Dict[Tuple[int, int], Set[Tuple[int, str]]] = {}  # span of a def node -> its decorators (line, text)
    jmods: Dict[Tuple[int, int], str] = {}  # span of a def node -> the text of its @mods (java, kotlin)
    metas: Set[Tuple[int, int]] = set()  # spans of annotation types (@meta)
    defspans: Set[Tuple[int, int]] = set()  # spans of def nodes: a def replaces a region of the same span
    namebytes: Dict[Tuple[int, int], Tuple[int, int]] = (
        {}
    )  # span of a def node -> the byte range of its name (the body hash leaves it out)
    refs: Set[Ref] = set()
    jroots: List[RootHint] = []
    typedefs: List[Tuple[str, str, int]] = (
        []
    )  # (alias, tag, line) of `typedef struct tag {..} alias;` (@alias, @aliased)
    for _, caps in sorted(parsed.run("defs"), key=lambda m: m[0]):
        if (
            caps.get("alias")
            and caps.get("aliased")
            and _text(data, caps["alias"][0]) != _text(data, caps["aliased"][0])
        ):
            a = caps["alias"][0]
            typedefs.append((_text(data, a), _text(data, caps["aliased"][0]), line(a)))
            declared.add((a.start_byte, a.end_byte))  # the declarator names the alias def: no use of it
        names = caps.get("name")
        if names and _text(data, names[0]) in blank:
            continue  # `var _ I = (*T)(nil)`, `const _: () = ...`: a statement, never a definition
        mods += [(n.start_byte, _text(data, n)) for n in caps.get("mod", ())]
        # a region that is no definition: a constructor, a lambda, an anonymous class body
        for region in REGIONS if jl else ():
            for n in caps.get(region, ()):
                if (n.start_byte, n.end_byte) not in defspans:
                    spans.setdefault(
                        (n.start_byte, n.end_byte, -1), ("", region, "", "", line(n), n.end_point[0] + 1, 0)
                    )
        flags = 0
        for key in caps:
            if key.startswith("flag."):
                flags |= FLAG_NAMES[_kind(key, "flag.", FLAG_NAMES, "flag name")]
        name, recv, vtext = (_text(data, caps[c][0]) if caps.get(c) else "" for c in ("name", "recv", "vis"))
        info = (_name(rules, name) if name else "", tl.recv_name(rl, recv) if recv else "", vtext)
        if recv and tl.protocol(rl, name):  # a method of a type: it may satisfy an interface of the stdlib or a library
            flags |= F_PROTOCOL
        for key, nodes in caps.items():
            if key.startswith("def.") and names:
                kind = _kind(key, "def.", NODE_KINDS, "node kind")
                kw = rules.get("def_kw", {}).get(kind)
                if kw and not (caps.get("kw") and re.search(kw, _text(data, caps["kw"][0]))):
                    continue  # a loose grammar (groovy) has one node for every statement: the keyword word says what it defines
                if js and not JS_NAME.match(name):
                    continue  # a computed or string key
                declared.add((names[0].start_byte, names[0].end_byte))
                owned.update(caps[c][0].start_byte for c in ("name", "recv") if caps.get(c))
                for n in nodes:
                    sk = (n.start_byte, n.end_byte, names[0].start_byte)
                    spans.setdefault(sk, (kind, *info, line(n), n.end_point[0] + 1, flags))
                    defspans.add((n.start_byte, n.end_byte))
                    namebytes.setdefault((n.start_byte, n.end_byte), (names[0].start_byte, names[0].end_byte))
                    spans.pop((n.start_byte, n.end_byte, -1), None)
                    if jl and "mods" in caps:
                        jmods[(n.start_byte, n.end_byte)] = _text(data, caps["mods"][0])
                    if jl and "meta" in caps:
                        metas.add((n.start_byte, n.end_byte))
                    for d in caps.get("deco", ()):
                        decos.setdefault((n.start_byte, n.end_byte), set()).add((line(d), _text(data, d)))
            elif key.startswith("vis."):
                vis = _kind(key, "vis.", VIS, "visibility")
                for n in nodes:
                    explicit[(n.start_byte, n.end_byte)] = vis
    stubs: Dict[int, str] = {}
    tag = rules.get("stub_tag") or STUB_TAG.get(lang)
    for _, caps in parsed.run("stub"):
        if tag and caps.get("stub.def") and caps.get("body"):
            body = _text(data, caps["body"][0]).strip()
            if body.startswith("{") and body.endswith("}"):
                stubs[caps["stub.def"][0].start_byte] = text_checker().generic_body_stub(tag, body[1:-1]) or ""
    # rows: [qual, kind, name, line0, line1, span, parent row, @vis text, flags, start byte]; scopes: (start, end) sorted by start
    rows: List[list] = []
    scopes: List[Tuple[int, int]] = []
    stack: List[tuple] = []  # (end, qual, kind, row index, start)
    safe, tpl = (_js_data()[2], _js_data()[5]) if js else (frozenset(), frozenset())
    jinfo: Dict[int, object] = {}  # row index -> lang_rules.DefFacts of a definition (java, kotlin)
    annotated: List[RootHint] = []  # JS: a decorator registers the definition with a framework (Angular, NestJS, Lit)
    templated: List[Tuple[str, int]] = []  # classes whose members a template may call
    order = sorted(spans.items(), key=lambda kv: (kv[0][0], -kv[0][1], kv[0][2]))
    for (s, e, _), (kind, name, recv, vtext, l0, l1, fl) in order:
        while stack and (stack[-1][0] <= s or (stack[-1][4], stack[-1][0]) == (s, e)):  # same span: a sibling
            stack.pop()
        parent = stack[-1] if stack else None
        pq, pk, pi = (parent[1], parent[2], parent[3]) if parent else ("", "", -1)
        if jl and kind in VARS and (pk or (pi >= 0 and rows[pi][1] in REGIONS)):
            continue  # a property is a def at the top level only (a field of a class, a local of a function is no def)
        if not kind:  # a region (@scope, @anon): the definitions inside it see the enclosing definition
            stack.append((e, pq, pk, len(rows), s))
            scopes.append((s, e))
            rows.append([pq, name, name, l0, l1, (s, e), pi, "", 0, s])
            continue
        qual = ".".join(x for x in (parent[1] if parent else "", recv, name) if x)
        in_class = bool(parent) and parent[2] in ("class", "iface")
        if js and kind == "method" and not in_class:
            continue  # a method of an object literal or a class expression is no symbol: its body belongs to the enclosing scope
        if kind == "func" and in_class:
            kind = "method"
        hints = []
        for dl, dt in sorted(decos.get((s, e), ())):
            m = DECORATOR.match(dt)
            full = re.sub(r"\s+", "", m.group(1)) if m else ""
            if full and full.rpartition(".")[2] not in safe:
                hints.append(RootHint(f"annotation:{full}", qual, dl, "hard", "convention"))
            if kind == "class" and full.rpartition(".")[2] in tpl:
                templated.append((qual, dl))
        annotated += hints
        fl |= F_DECORATED if hints else 0
        if jl:
            df = jinfo[len(rows)] = lang_rules.def_facts(
                jl, kind, qual, name, jmods.get((s, e), ""), l0, pk, jinfo.get(pi), (s, e) in metas
            )
            fl |= df.flags
            jroots += list(df.roots)
            refs.update(Ref(c, "call", l0, qual) for c in df.calls)
            refs.update(Ref(c, "schema", l0, qual) for c in df.schema)
            held = pi >= 0 and rows[pi][1] == "anon"  # a member of an anonymous class has no use site by name
            if held or (pk in ("method", "func") and (kind in ("class", "iface", "method") or fl & F_OVERRIDE)):
                refs.add(Ref(name, "ref", l0, pq))  # lives as long as the method or the class that holds it
        stack.append((e, qual, kind, len(rows), s))
        scopes.append((s, e))
        rows.append([qual, kind, name, l0, l1, (s, e), pi, vtext, fl, s])
    starts = [x[0] for x in scopes]

    def index_of(pos: int) -> int:  # the innermost definition that contains pos, -1 = none
        i = bisect_right(starts, pos) - 1
        while i >= 0:
            if scopes[i][0] <= pos < scopes[i][1]:
                return i
            i -= 1
        return -1

    def scope_of(pos: int) -> str:
        i = index_of(pos)
        return rows[i][0] if i >= 0 else ""

    def kind_at(pos: int) -> str:  # the kind of the definition that contains pos, "" = none
        i = index_of(pos)
        return rows[i][1] if i >= 0 else ""

    words: Dict[int, List[str]] = {}
    for pos, word in sorted(mods):  # modifiers of the definition they are written on (source order)
        if index_of(pos) >= 0:
            words.setdefault(index_of(pos), []).append(word)
    rootm = parsed.run("roots")
    flagged: Dict[int, int] = {}  # a roots rule with "flag": the definition that holds the node gets the flag, no root
    for _, caps in rootm:
        for key, nodes in caps.items():
            rule = rules.get("roots", {}).get(key[len("root.") :], {}) if key.startswith("root.") else {}
            for n in nodes if rule.get("flag") else ():
                i = index_of(n.start_byte)
                if i >= 0 and _passes(rule, _text(data, n), kind_at(n.start_byte)):
                    flagged[i] = flagged.get(i, 0) | flags_of(rule["flag"])
    vis_mods, flag_mods, dvis = rules.get("vis_mods", {}), rules.get("flag_mods", {}), rules.get("default_vis", {})
    defs: List[Def] = []
    visof: List[str] = []
    quals: Dict[int, str] = {}  # start byte of a def -> its qual
    for i, (qual, kind, name, l0, l1, span, pi, vtext, fl0, s0) in enumerate(rows):
        if jl and kind in REGIONS:
            visof.append("")  # a region is no definition
            continue
        ws = words.get(i, ())
        pk = rows[pi][1] if pi >= 0 else ""
        vis = next((vis_mods[w] for w in ws if w in vis_mods), explicit.get(span))
        if vis is None:
            vis = (tl.vis(rl, name, vtext) or None) if rl else None  # go: exported, rust: pub(crate) ...
        if vis is None:
            vis = rules.get("parent_default_vis", {}).get(pk) or dvis.get(kind) or dvis.get("*")
            vis = vis or ("private" if not dvis and name.startswith(("_", "#") if js else "_") else "public")
        if rl and pi >= 0 and pk == "iface":
            vis = visof[pi]  # the members of an interface or trait (methods, consts) are as visible as it is
        if i in jinfo:
            vis = jinfo[i].vis  # java, kotlin: the rules of frameworks.json decide the visibility
        visof.append(vis)
        fl = fl0 | flagged.get(i, 0) | (F_PROTOCOL if protocol_member(rules, qual, kind, vis, pk, ws) else 0)
        for w in ws:
            fl |= flags_of(flag_mods[w]) if w in flag_mods else 0
        stub = stubs.get(span[0], "")
        quals[s0] = qual
        bh, bsz = (
            _body(lang, data, span, namebytes.get(span)) if kind in ("func", "method") and not stub else bodyhash.NONE
        )
        defs.append(Def(qual, kind, l0, l1, vis, fl | (F_STUB if stub else 0), -1, stub, bh, bsz))
    qk = {d.qual: d.kind for d in defs}  # qual -> kind of a definition

    def bound(
        scope: str, name: str
    ) -> bool:  # a parameter or a local (@local) of the code around the use binds the name
        while scope and qk.get(scope) not in TYPES:
            if name in shadow.get(scope, ()):
                return True
            scope = scope.rpartition(".")[0]
        return False

    def self_member(
        scope: str, name: str
    ) -> bool:  # inside a class, trait or module, unless a nested def of that name is nearer
        while scope and qk.get(scope) not in TYPES:
            if f"{scope}.{name}" in qk:
                return False
            scope = scope.rpartition(".")[0]
        return bool(scope)

    dstarts = sorted(declared)

    def inside_name(
        n,
    ) -> bool:  # the name of a definition, or a part of a qualified one (`Foo::bar`: the identifier `bar`)
        i = bisect_right(dstarts, (n.start_byte, 1 << 62)) - 1
        return i >= 0 and dstarts[i][0] <= n.start_byte and n.end_byte <= dstarts[i][1]

    dyn: Set[DynSite] = set()
    imports: Set[Import] = set()
    decls: Dict[Tuple[int, str], List[tuple]] = (
        {}
    )  # (region, name) -> [(position, type)] of fields, parameters and locals (java, kotlin)
    heads: List[Tuple[int, int]] = []  # the import (and package) statements: the names in them are no uses

    def find(i: int, pos: int, name: str) -> Optional[str]:
        """Type of the variable `name` seen at `pos` in region i: the declaration of the innermost region that has one, the latest
        at or before pos (a field of a class is seen from the whole body); None = no variable of that name."""
        while i >= 0:
            hit = [d for d in decls.get((i, name), ()) if rows[i][1] in SEEN_WHOLE or d[0] <= pos]
            if hit:
                return max(hit)[1]
            i = rows[i][6]
        return None

    def in_head(pos: int) -> bool:
        i = bisect_right(heads, (pos, 1 << 62)) - 1
        return i >= 0 and pos < heads[i][1]

    if jl:
        found: Dict[tuple, tuple] = {}
        for _, caps in parsed.run("types"):
            for key, nodes in caps.items():
                if key.startswith("var.") and caps.get(
                    "name"
                ):  # no @type: a lambda or catch parameter, which only hides
                    vk = _kind(key, "var.", VAR_KINDS, "variable kind")
                    ident = (nodes[0].start_byte, nodes[0].end_byte, caps["name"][0].start_byte)
                    if ident not in found or VAR_KINDS.index(vk) < VAR_KINDS.index(found[ident][0]):
                        found[ident] = (vk, nodes[0], caps)
        for _, (vk, n, caps) in sorted(found.items()):
            i, ttext = index_of(n.start_byte), _text(data, caps["type"][0]) if caps.get("type") else ""
            decls.setdefault((i, _text(data, caps["name"][0])), []).append(
                (n.start_byte, lang_rules.type_name(jl, ttext, False))
            )  # an untyped or primitive variable still hides the outer one of that name
            if vk == "param" and "mods" in caps and i >= 0 and rows[i][1] in ("method", "func"):
                jroots += lang_rules.param_roots(jl, _text(data, caps["mods"][0]), rows[i][0], line(n))
            wrapped = lang_rules.type_name(jl, ttext, True)
            if wrapped and lang_rules.injects(
                jl, vk, _text(data, caps["mods"][0]) if "mods" in caps else "", jinfo.get(i)
            ):
                refs.add(Ref(wrapped, "di", line(n), rows[i][0] if i >= 0 else ""))
        tops = [d.qual for d in defs if "." not in d.qual and d.kind in CLASSY]
        for _, caps in parsed.run("imports"):
            for n in caps.get("import", ()):
                heads.append((n.start_byte, n.end_byte))
                im = lang_rules.parse_import(jl, _text(data, n), line(n))
                if im:
                    imports.add(im)
                cls = lang_rules.static_class(jl, _text(data, n)) if im else ""
                refs.update(
                    Ref(cls, "ref", line(n), q) for q in tops if cls
                )  # a static import uses the class in every top class
        heads.sort()
    aliases: Set[str] = set()
    shadow: Dict[str, Set[str]] = {}  # definition qual -> the names its parameters and locals bind
    inc = INCLUDE_CMDS.get(lang, frozenset())
    mems: List[tuple] = (
        []
    )  # JS: member values (@mem) are weak refs unless the member is a callee or the object of a longer one
    callspans: Set[Tuple[int, int]] = set()
    matches = sorted(parsed.run("refs"), key=lambda m: m[0])
    at = _import_attrs(data, matches)
    strong = set(
        owned
    )  # name nodes a stronger kind uses: the weak ref on the same node is dropped (rules languages only)
    for _, caps in matches:
        for n in caps.get("import", ()):
            ims, al = tl.imports(rl, _text(data, n), line(n), scope_of(n.start_byte), at.get(n.start_byte, ()))
            imports.update(ims)
            aliases |= al
        for n in caps.get("item_body", ()):  # the declarations inside cfg_if! { .. }: raw tokens to the grammar
            name = _text(data, caps["item_macro"][0]) if caps.get("item_macro") else ""
            imports.update(tl.macro_imports(rl, name, _text(data, n), line(n), scope_of(n.start_byte)))
        for n in caps.get("local", ()):
            shadow.setdefault(scope_of(n.start_byte), set()).add(_text(data, n))
        strong.update(n.start_byte for n in caps.get("scope", ()))
        if any(k.startswith("ref.") and k not in ("ref.ref", "ref.exec") for k in caps):
            strong.update(n.start_byte for n in caps.get("name", ()))
    for _, caps in matches:
        kinds = [_kind(k, "ref.", EDGE_KINDS, "edge kind") for k in caps if k.startswith("ref.")]
        dyn_rx = rules.get("ref_cmd", {}).get("dyn")  # a rule: only a call of the callees it names has a dynamic site
        dyn_ok = not dyn_rx or "cmd" in caps and re.search(dyn_rx, _text(data, caps["cmd"][0]))
        for n in caps.get("dyn", ()) if dyn_ok else ():
            dyn.add(DynSite(rl.get("dyn_prefix", "") + _text(data, n)[:DYN_MAX], line(n), scope_of(n.start_byte)))
        for key in (
            k for k in caps if k.startswith("dyn.")
        ):  # @dyn.<kind>: the pattern "<kind>:<group 1 of rules dyn.<kind>.re>"
            rule = rules.get("dyn", {}).get(key[4:], {})
            if rule.get("cmd") and not ("cmd" in caps and re.search(rule["cmd"], _text(data, caps["cmd"][0]))):
                continue
            if rule.get("cmd_alias") and not ("cmd" in caps and _text(data, caps["cmd"][0]) in aliases):
                continue  # the receiver is a local value, not an imported package (the imports come first in a file)
            for n in caps[key]:
                m = re.search(rule.get("re", "(.+)"), _text(data, n).strip())
                if m:
                    dyn.add(
                        DynSite(
                            f"{key[4:]}:{m.group(1)}{rule.get('pick', '')}"[:DYN_MAX], line(n), scope_of(n.start_byte)
                        )
                    )
        scan_rx = rules.get("ref_cmd", {}).get("scan")
        callee = _text(data, caps["cmd"][0]) if caps.get("cmd") else ""
        by_value = next((v for k, v in rules.get("scan_value", {}).items() if re.search(k, callee)), None)
        scan_ok = (
            not scan_rx or re.search(scan_rx, callee) or by_value
        )  # an attribute that holds script text only when its value says so
        for n in caps.get("scan", ()) if scan_ok else ():
            # the text of an opaque node (a macro body, a template expression): calls and plain uses, `##` = computed name
            if rules.get("scan_before") and not re.search(
                rules["scan_before"], data[max(0, n.start_byte - 24) : n.start_byte].decode("utf-8", "replace")
            ):
                continue  # the node is script text only after an option word (`bash -c '...'`)
            body = _text(data, n)
            if by_value and not re.search(
                scan_rx or "$^", callee
            ):  # `href="javascript:go()"`: the script text after the scheme
                head = re.match(by_value, body)
                if not head:
                    continue
                body = body[head.end() :]
            if "##" in body:
                dyn.add(DynSite(body.strip()[:DYN_MAX], line(n), scope_of(n.start_byte)))
            refs.update(
                Ref(m.group().rstrip(" \t(").strip(), "call" if m.group(1) else "ref", line(n), scope_of(n.start_byte))
                for m in WORD.finditer(body)
            )
        obj = _text(data, caps["obj"][0]) if caps.get("obj") else ""
        for kind in kinds:
            if (
                kind == "include"
                and "cmd" in caps
                and "include" not in rules.get("ref_cmd", {})
                and _text(data, caps["cmd"][0]) not in inc
            ):
                continue
            rx = rules.get("ref_cmd", {}).get(kind)
            if rx and "cmd" in caps and not re.search(rx, _text(data, caps["cmd"][0])):
                continue
            for n in caps.get("name", ()):
                if jl:
                    if (n.start_byte, n.end_byte) in declared or in_head(n.start_byte):
                        continue  # the name of a definition and the names of an import are no uses
                elif not rl and inside_name(n):
                    continue
                scope = scope_of(n.start_byte)
                if kind == "exec":  # a directive comment: the path-like words are files that get executed
                    refs.update(Ref(w, "exec", line(n), scope) for w in tl.exec_names(rl, _text(data, n)))
                    continue
                if rl and kind == "ref" and n.start_byte in strong:
                    continue
                if caps.get("scope"):  # impl / supertrait: the derived type, qualified by the module around it
                    scope = ".".join(x for x in (scope, tl.recv_name(rl, _text(data, caps["scope"][0]))) if x)
                word = _text(data, n)
                chained = False
                if js:
                    name = _js_name(word.strip("\"'"))
                    if kind == "call":
                        callspans.add((n.start_byte, n.end_byte))
                    if name and "mem" in caps and kind == "ref":
                        mems.append((n, name, scope))
                        continue
                    found = [name]
                elif rl:
                    found = tl.ref_names(rl, word, obj, aliases, shadow.get(scope, ()))
                else:
                    name = _path_name(rules, kind, _name(rules, word))
                    if caps.get("self") and bound(scope, name):
                        continue  # a parameter or a local, no use of a method
                    if jl:
                        if (kind == "call" and name in inc) or (kind == "ref" and not IDENT.match(name)) or not name:
                            continue
                        if kind == "ref" and qk.get(scope) in CLASSY and name == scope.rpartition(".")[2]:
                            q = _QUALIFIER.search(
                                data[max(0, n.start_byte - 120) : n.start_byte].decode("utf-8", "replace")
                            )
                            if not q:
                                continue  # a type used inside its own definition does not keep it alive
                            name = (
                                q.group(1) + name
                            )  # `com.other.Order` inside Order is another type: the name as written
                        if "recv" in caps:
                            i, pos = index_of(n.start_byte), n.start_byte
                            if jl.ext_calls and kind == "call":  # recv.f(): maybe a top-level extension function f
                                refs.add(Ref(name, kind, line(n), scope))
                            name = lang_rules.qualify(jl, lambda v: find(i, pos, v), _text(data, caps["recv"][0]), name)
                        elif jl.this and kind == "call" and qk.get(scope) in ("class", "iface", "method"):
                            # an unqualified call inside a class may be an inherited member
                            refs.add(Ref(f"this.{name}", kind, line(n), scope))
                    elif caps.get("self") and rules.get("self_word") and "." not in name and self_member(scope, name):
                        refs.add(
                            Ref(f"{rules['self_word']}.{name}", kind, line(n), scope)
                        )  # an inherited or mixed-in member
                    chained = bool(rules.get("dotted_names")) and kind != "include" and not DOTTED.match(name)
                    if chained:  # the receiver is an expression: only the member is a name
                        last = re.search(r"([A-Za-z_]\w*)\s*$", name)
                        name = f"?.{last.group(1)}" if last else ""
                    found = [name]
                    if kind == "ref" and rules.get("component_tags"):
                        # a template tag: <MyComp> and <my-comp> (MyComp too) are components, <b> and <div> are elements
                        found = (
                            [name, "".join(w[:1].upper() + w[1:] for w in name.split("-"))]
                            if "-" in name
                            else [name] * (name[:1].isupper())
                        )
                for name in found:
                    if (
                        (kind == "call" and name in inc)
                        or (kind == "ref" and not chained and not js and not IDENT.match(name))
                        or not name
                        or name in blank
                    ):
                        continue  # `source` itself is an include, not a call; a flag or a path is not a function name
                    refs.add(Ref(name, kind, line(n), scope))
                    if caps.get("member") and "." not in name:  # also a member of that name (a method call in a macro)
                        refs.add(Ref("_." + name, kind, line(n), scope))
            if not caps.get(
                "name"
            ):  # @member alone: a member set by name on a receiver the text does not show (`?.Id`)
                for n in caps.get("member", ()):
                    refs.add(Ref("?." + _text(data, n), kind, line(n), scope_of(n.start_byte)))
    outer: Dict[int, int] = {}
    for n, _, _ in mems:
        outer[n.start_byte] = max(outer.get(n.start_byte, 0), n.end_byte)
    for n, name, sc in mems:
        if (n.start_byte, n.end_byte) not in callspans and outer[n.start_byte] == n.end_byte:
            refs.add(Ref(name, "ref", line(n), sc))
    mimports, adefs, arefs, exported, mdyn = (
        _modules(data, parsed, line, scope_of, frozenset(d.qual for d in defs if "." not in d.qual))
        if js
        else (set(), [], set(), set(), set())
    )
    for (
        alias,
        tag,
        ln,
    ) in (
        typedefs
    ):  # `typedef struct tag {..} tag_t;`: tag_t is a def (never reported) that uses the tag, so a use of tag_t reaches it
        adefs.append(Def(alias, "class", ln, ln, "public", F_PROTOCOL, -1, ""))
        arefs.add(Ref(tag, "ref", ln, alias))
    imports |= mimports
    dyn |= mdyn
    seen, uniq = set(), []
    for r in sorted(refs):  # a plain argument word is a weak ref: keep the first line of each (name, scope)
        if r.kind == "ref":
            if (r.name, r.scope) in seen:
                continue
            seen.add((r.name, r.scope))
        uniq.append(r)
    roots = _roots(rl, data, rootm, defs, quals, line) + annotated + jroots
    for (
        cq,
        dl,
    ) in (
        templated
    ):  # `@Component` / `@Directive`: its template calls the members by name; soft, nothing in the tree does
        members = [d.qual for d in defs if d.kind == "method" and d.qual.startswith(cq + ".")]
        roots += [RootHint("template", q, dl, "soft", "convention") for q in members]
    rule_roots: Set[RootHint] = set()  # rules.json roots: the @root.<kind> captures whose text passes the rule
    for _, caps in rootm:
        for key, nodes in caps.items():
            if key.startswith("root.") and key not in ("root.marker", "root.cand", "root.attr", "root.span"):
                suffix = key[len("root.") :]
                kind = (
                    "annotation:" + suffix[len("annotation.") :]
                    if suffix.startswith("annotation.")
                    else suffix.split("-")[0]
                )
                if not (kind in ROOT_KINDS or kind.startswith("annotation:")):
                    raise ValueError(f"queries: {key}: {kind!r} is not a root kind")
                rule = rules.get("roots", {}).get(suffix, {})
                for n in nodes if not rule.get("flag") else ():
                    if _passes(rule, _text(data, n), kind_at(n.start_byte)):
                        at = scope_of(n.start_byte)
                        for target in (at, *(_hosts(qk, at) if rule.get("host") else ())):
                            rule_roots.add(RootHint(kind, target, line(n), rule.get("strength", "hard"), "convention"))
    first: Dict[Tuple[str, str], RootHint] = (
        {}
    )  # one root per (kind, target): the first line (top-level statements are many)
    for h in sorted(rule_roots):
        first.setdefault((h.kind, h.target), h)
    roots = sorted(set(roots) | set(first.values()))
    for pr in rules.get("path_roots", ()):  # a framework calls the definitions of these files by convention
        if re.search(pr["path"], path):
            roots += [
                RootHint(pr["kind"], d.qual, d.line0, pr.get("strength", "hard"), "convention")
                for d in defs
                if d.qual and (not pr.get("kinds") or d.kind in pr["kinds"])
            ]
    stub = (text_checker().sh_hook_stub_reason(text) or "") if lang == "shell" else ""
    if js:
        defs = _js_defs(defs, exported, _protocol(imports), bool(_js_data()[3].search(path)))
    all_defs = tuple(merge_defs([module_def(text, stub), *mark_generated(text, [*defs, *adefs])]))
    xerr = 0
    try:
        xrefs, xdyn = xl.ts_refs(dclangs.load().family(lang) or "", text, data, parsed, line, scope_of)
    except QueryCompileError:
        raise
    except (
        Exception
    ):  # noqa: BLE001 - a bug of the cross-language pass costs that pass (errors + 1), not the other facts
        xrefs, xdyn, xerr = [], [], 1
    refs_t = tuple(sorted({*uniq, *arefs, *xrefs}))
    return Facts(
        lang,
        all_defs,
        refs_t,
        tuple(sorted(imports)),
        tuple(roots),
        tuple(sorted({*dyn, *xdyn})),
        int(parsed.errors) + xerr,
    )


def _import_attrs(data: bytes, matches) -> Dict[int, List[str]]:
    """{start byte of an @import node: the texts of the outer attributes right before it} (`#[path = "x.rs"] mod x;`): the
    attributes are separate siblings of the declaration, found by position like the attributes of a root (@attr.item the
    whole attribute item, @attr the attribute)."""
    attrs = [
        (i.start_byte, i.end_byte, _text(data, c["attr"][0]))
        for _, c in matches
        for i in c.get("attr.item", ())
        if c.get("attr")
    ]
    imports = {n.start_byte for _, c in matches for n in c.get("import", ())}
    out: Dict[int, List[str]] = {}
    for text, owner, _ in tl.attr_owners(data, attrs, imports) if attrs else ():
        out.setdefault(owner, []).append(text)
    return out


def _roots(rl: dict, data: bytes, found, defs, quals: Dict[int, str], line) -> List[RootHint]:
    """Manual markers, rule roots (top-level names, directive comments) and attribute roots of one file."""
    roots: List[RootHint] = []
    names = {d.qual for d in defs}
    pkgs = {_text(data, n) for _, caps in found for n in caps.get("pkg", ())}
    # impl blocks, for an attribute on one (#[pymethods])
    spans = {n.start_byte: n.end_byte for _, caps in found for n in caps.get("root.span", ())}
    attrs = []
    for _, caps in found:
        for n in caps.get("root.marker", ()):
            ln, t = line(n), _text(data, n)
            if MARKER.search(t):
                target = next((d.qual for d in sorted(defs, key=lambda d: d.line0) if d.line0 in (ln, ln + 1)), "")
                roots.append(RootHint("manual", target, ln, "hard", "registered"))
            roots += [RootHint(k, nm, ln, st, tr) for nm, k, st, tr in tl.comment_roots(rl, t) if nm in names]
        for n in caps.get("root.cand", ()):
            if caps.get("name") and n.start_byte in quals:
                hints = tl.name_roots(rl, _text(data, caps["name"][0]), pkgs)
                roots += [RootHint(k, quals[n.start_byte], line(n), st, tr) for k, st, tr in hints]
        for n in caps.get("root.attr", ()):
            if caps.get("attr"):
                attrs.append((n.start_byte, n.end_byte, _text(data, caps["attr"][0]), line(n)))
    row = {s: ln for s, _, _, ln in attrs}
    for text, owner, at in tl.attr_owners(data, [(s, e, t) for s, e, t, _ in attrs], set(quals) | set(spans)):
        # an attribute on an impl block roots every def inside it
        targets = [q for s, q in quals.items() if owner <= s < spans[owner]] if owner in spans else [quals[owner]]
        for k, st, tr in tl.attr_roots(rl, text):
            roots += [RootHint(k, q, row[at], st, tr) for q in targets]
    return sorted(set(roots))


@lru_cache(maxsize=1)
def _code_sha() -> str:
    """sha256 of this file: the mapping from matches to Facts is an input of every shard like the queries are."""
    with open(os.path.abspath(__file__), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


@lru_cache(maxsize=1)
def _body_code_sha() -> str:
    """The code of the body hash (graph.extract.bodyhash) decides Def.bh: part of the shard key."""
    with open(bodyhash.__file__, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


def so_sha(g: str) -> str:
    """The versions of tree-sitter and of the language pack ('' when they are not installed): part of the shard key of every extractor
    that reads a grammar (graph.ts.grammar_id). `g` is the grammar, kept for the callers: one pack serves all of them."""
    return tsm.grammar_id()


class TsGeneric(Extractor):
    # 2: hidden aliases, DynSites of computed specifiers, exported imports (JS/TS); 3: cross-language facts (xlang); 4: generated blocks,
    # shell script statements and function names as data, C typedef aliases, C++ unseen owners
    # 5: the normalised body hash of functions and methods (Def.bh / bsz, graph.extract.bodyhash)
    name, version = "ts_generic", 5
    langs: Tuple[str, ...] = ()

    def grammar_sha(self, lang: str) -> str:
        """sha256(the installed tree-sitter and pack versions | the query files | the rules of the language | the mapping code | the JS data | the sha of each
        embedded language of a page): all decide the facts."""
        g = dclangs.load().grammar(lang) or ""
        so = so_sha(g)
        extra = f"|{_js_data()[4]}" if lang in JS_LANGS else ""  # the framework data decides the JS facts too
        jrules = lang_rules.sha(lang)  # the rules (frameworks.json) decide the facts of a language that has them
        extra += f"|{jrules}" if jrules else ""
        extra += f"|{xl.sha()}|{generated_sha()}|{bodyhash.data_sha()}|{_body_code_sha()}"  # the cross-language rules, the command-line data and the generated-block markers decide the facts too
        for emb in sorted({e for _, e in rules_of(g).get("embed", {}).get("lang", ()) if e and e != lang}):
            extra += f"|{self.grammar_sha(emb)}"  # a <script> block is extracted by its own language: that grammar, queries, rules and data too
        return hashlib.sha256(f"{so}|{queries_sha(g)}|{tl.rules_sha(lang)}|{_code_sha()}{extra}".encode()).hexdigest()

    def supports(self, lang: str) -> bool:
        """Does the grammar of `lang` have queries in data/queries (a language without them is not implemented yet)?"""
        g = dclangs.load().grammar(lang)
        return bool(g) and bool(_queries(g))

    def probe(self, lang: str, cfg: Optional[dict] = None) -> Optional[Finding]:
        return probe_grammar(lang, cfg)

    def _embedded(self, facts: Facts, g: str, path: str, text: str, data: bytes, parsed) -> Facts:
        """embed.scm: the code blocks written in another language (<script> of html, vue, svelte) are extracted by that language, with the
        text before the block blanked so the lines stay, and merged into the Facts of the file."""
        spec = rules_of(g).get("embed") or {}
        defs, refs, imports, roots, dyn, errors = (
            list(facts.defs),
            set(facts.refs),
            set(facts.imports),
            set(facts.roots),
            set(facts.dyn),
            facts.errors,
        )
        for _, caps in parsed.run("embed"):
            tag = _text(data, caps["tag"][0]) if caps.get("tag") else ""
            sub = next((lg for pat, lg in spec.get("lang", ()) if re.search(pat, tag)), "")
            for n in caps.get("script", ()) if sub else ():
                blank = re.sub(r"[^\n]", " ", data[: n.start_byte].decode("utf-8", "replace"))
                f = self.extract(sub, path, blank + _text(data, n))
                defs += f.defs
                refs |= set(f.refs)
                imports |= set(f.imports)
                # the call statements of an inline script are the page's own: the page or component is rooted by its own rules, never by `main`
                roots |= {h for h in f.roots if (h.kind, h.target, h.strength) != ("main", "", "soft")}
                dyn |= set(f.dyn)
                errors += f.errors
                if spec.get("roots"):  # an inline handler calls the top-level definitions of the block by name
                    roots |= {
                        RootHint(spec["roots"], d.qual, d.line0, "soft", "convention")
                        for d in f.defs
                        if d.qual and "." not in d.qual
                    }
        return Facts(
            facts.lang,
            tuple(merge_defs(defs)),
            tuple(sorted(refs)),
            tuple(sorted(imports)),
            tuple(sorted(roots)),
            tuple(sorted(dyn)),
            errors,
        )

    def extract(self, lang: str, path: str, text: str) -> Facts:
        g = dclangs.load().grammar(lang)
        if not g or not _queries(g):
            raise ExtractorMissing(
                lang,
                f"no tree-sitter queries for grammar {g} (graph/data/queries/{g}/): this language is not implemented yet",
            )
        data = text.encode("utf-8", "replace")
        try:
            parsed = (_backend or _real_backend)(g, data)
        except LookupError as exc:
            raise ExtractorMissing(lang, str(exc)) from exc
        try:
            facts = _facts(lang, text, data, parsed, path)
            return self._embedded(facts, g, path, text, data, parsed) if "embed" in _queries(g) else facts
        except QueryCompileError as exc:  # a .scm that does not compile on this grammar is loud, never an empty graph
            raise ExtractorMissing(lang, f"{exc} (graph/data/queries/{g}/)") from exc


def _register() -> None:
    ex = TsGeneric()
    ex.langs = tuple(sorted(k for k, v in dclangs.load().langs.items() if v.get("extractor") == "ts_generic"))
    register_extractor(ex)


_register()
