"""graph.extract.lang_rules: the data-driven rules behind the typed, annotated part of the generic tree-sitter mapping.

ts_generic maps query matches to Facts; for a language that has rules here (java, kotlin) it also reads the modifiers text of a
definition (@mods in defs.scm), the typed variables of types.scm and the import statements of imports.scm. What those texts
MEAN is data (data/frameworks.json), never code:
    lang_rules.<lang>      import regex, visibility words, modifier -> flag words, main method, DI points, protocol method names
                           (called by the JDK, F_PROTOCOL), ext_calls, meta_mods (see its "_doc")
    frameworks.<name>      rules {id, on: class|method|param, ann: [annotation names] and/or mod: [modifier words], kind (default
                           annotation:<id>), bean, why} applied to a language through lang_rules.<lang>.frameworks
                           (on param: an annotated parameter, CDI @Observes, roots the method that has it)

    get(lang) -> Rules | None          def_facts(rl, kind, qual, name, mods, line, parent_kind, parent) -> DefFacts
    param_roots(rl, mods, qual, line)  the roots a parameter annotation gives to its method ("on": "param" rules)
    pkg_dir(lang, path)                the package directory of a file (data src_root strips src/main/java: main and test are one scope)
    type_name(rl, text, wrap)          head type of a type text (wrap: List<T> / Optional<T> / Map<K, V> -> T)
    qualify(rl, find, recv, name)      "T.m" for a receiver whose declared type find(name) knows, "x.m" as written, "*.m" for an expression
    injects(rl, var_kind, mods, owner)      is this variable an injection point;   parse_import(rl, text, line) -> Import
    static_class(rl, text)             the class a static import takes members of ("" for another import)
    bean_kinds() -> frozenset of root kinds that make a class a DI bean (read by link.py);   sha(lang) -> data hash

    meta_rule(kind) / rule(id) / inherit(own, chain)    annotation inheritance (below)

Annotation inheritance (composed annotations): an annotation type of the project (Java @interface, a Kotlin `annotation class`; the
query capture @meta or a modifier word of lang_rules.<lang>.meta_mods) that carries rule annotations hands those rules to the defs that
use it: `@Service @interface Facade` makes `@Facade class UserFacade` a bean, `@RequestMapping(method = GET) @interface Fetch` makes
`@Fetch void orders()` a request mapping. The annotation type gets, instead of roots, the hints RootHint("annotation:<rule id>.meta",
qual) for the rules of ALL sides (class and method) that its annotations match, and the marker RootHint("annotation:.meta", qual);
graph.link turns them, with the annotation uses (refs of kind reg) of the files, into derived roots: lang_rules.inherit closes the
rule ids over a chain of annotation types (@UseCase carries @Facade carries @Service). The rule data is the same as for a written
annotation, nothing about a framework is code here.

Annotation roots come out as RootHint("annotation:<rule id>", qual, line, "hard", "convention") (or the rule's own kind, e.g.
"test" for JUnit); a rule with "accessors": true (an entity, a ConfigurationProperties class: reflection reads and writes it)
also roots the public get/set/is methods of the class, as annotation:<rule id>.accessor (the prefixes are lang_rules.<lang>.accessors).
A rule with "ref_only": true gives no root (the def is no entry point for it): it only writes refs. A rule with "schema": {group, attrs, type,
type_attrs, self, snake, unless} names schema objects (graph.extract.xlang kind schema): the strings of the annotation's attrs (the member's own name
when `self` and there is none; snake_case too when `snake`; skipped when one of the annotations in `unless` is present), each as `<group>:<type>.<name>`
where the type is `type` or the string of type_attrs (`@SchemaMapping(typeName = "Book", field = "author")`): JPA @Table / @Entity -> the table,
Spring GraphQL @QueryMapping / @SchemaMapping and DGS -> the field.
The rule's `why` is in the data for the why-alive text. Strings and annotation arguments are dropped before
the words are read, so "@GetMapping(\"/@x\")" is the annotation GetMapping and nothing else.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import posixpath
import re
from typing import Callable, Dict, FrozenSet, Iterable, List, Mapping, NamedTuple, Optional, Tuple

from ..model import FLAG_NAMES, F_DECORATED, F_PROTOCOL, Import, RootHint

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "frameworks.json")
_STR = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')
_PAR = re.compile(r"\([^()]*\)")
_ANN = re.compile(r"@(?:\w+:)?([\w.]+)")
_TYPE = re.compile(r"([\w.]+)\s*(?:<(.*)>)?$", re.S)
_VARIANCE = re.compile(r"^(?:\?\s*(?:extends|super)\s+|out\s+|in\s+)")
_SIMPLE = re.compile(r"^[A-Za-z_$][\w$]*$")
_DOTTED = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")
SIDE = {"class": "class", "iface": "class", "method": "method", "func": "method"}
MOD = "mod:"  # by_ann key of a rule that matches a modifier word (Kotlin operator, infix) instead of an annotation


class Rules(NamedTuple):
    lang: str
    imp: Optional["re.Pattern"]
    vis: dict
    flags: dict
    this: bool
    main: dict
    di: dict
    by_ann: dict  # annotation simple name -> [rule]
    sha: str
    acc: Optional["re.Pattern"] = (
        None  # name of a bean property accessor (get/set/is + Name), None = the language has none
    )
    acc_ids: FrozenSet[str] = (
        frozenset()
    )  # ids of the rules whose classes are read and written by reflection (their accessors live)
    src_root: Optional["re.Pattern"] = (
        None  # the source-set part of a path that is no part of the package (src/main/java)
    )
    ext_calls: bool = False  # a call with a receiver may be a call of a top-level extension function of that name
    protocol: FrozenSet[str] = frozenset()  # method names that the language or the JDK calls itself (readObject)
    meta_mods: FrozenSet[str] = frozenset()  # modifier words of an annotation type (Kotlin: annotation)


class DefFacts(NamedTuple):
    vis: str
    flags: int
    roots: Tuple[RootHint, ...]
    anns: Tuple[str, ...]  # annotation simple names written on the definition
    rules: Tuple[str, ...]  # ids of the framework rules that matched
    calls: Tuple[
        str, ...
    ] = ()  # methods that the annotations name as strings: "m" of the same class (@MethodSource), "*.m" of any class (@Bean(initMethod))
    schema: Tuple[
        str, ...
    ] = ()  # schema objects that the annotations name ("sql:orders", "graphql:Query.books"), see the docstring


@functools.lru_cache(maxsize=4)
def _data(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _rules_of(d: dict, lang: str) -> Optional[Rules]:
    spec = (d.get("lang_rules") or {}).get(lang)
    if not spec:
        return None
    rules, by_ann = [], {}
    for fwk in spec.get("frameworks", ()):
        for r in d["frameworks"][fwk]["rules"]:
            rules.append(r)
            for a in [*r.get("ann", ()), *(MOD + w for w in r.get("mod", ()))]:
                by_ann.setdefault(a, []).append(r)
    blob = json.dumps([spec, rules], sort_keys=True, separators=(",", ":"))
    pre = spec.get("accessors")
    return Rules(
        lang,
        re.compile(spec["import"]) if spec.get("import") else None,
        spec.get("vis", {}),
        spec.get("flags", {}),
        bool(spec.get("this")),
        spec.get("main") or {},
        spec.get("di") or {},
        by_ann,
        hashlib.sha256(blob.encode()).hexdigest(),
        re.compile(r"(?:%s)[A-Z]" % "|".join(map(re.escape, pre))) if pre else None,
        frozenset(r["id"] for r in rules if r.get("accessors")),
        re.compile(spec["src_root"]) if spec.get("src_root") else None,
        bool(spec.get("ext_calls")),
        frozenset(spec.get("protocol", ())),
        frozenset(spec.get("meta_mods", ())),
    )


@functools.lru_cache(maxsize=16)
def _get(lang: str, path: str) -> Optional[Rules]:
    return _rules_of(_data(path), lang)


def get(lang: str) -> Optional[Rules]:
    return _get(lang, DATA)


def sha(lang: str) -> str:
    """Hash of the rules of `lang` ("" without rules): part of the grammar sha, so a changed rule re-extracts the shards."""
    rl = get(lang)
    return rl.sha if rl else ""


@functools.lru_cache(maxsize=4)
def _beans(path: str) -> FrozenSet[str]:
    return frozenset(
        f"annotation:{r['id']}" for f in _data(path).get("frameworks", {}).values() for r in f["rules"] if r.get("bean")
    )


def bean_kinds() -> FrozenSet[str]:
    return _beans(DATA)


META = ".meta"  # hint kind annotation:<rule id>.meta (annotation:.meta without a rule): the definition is an annotation type


@functools.lru_cache(maxsize=4)
def _by_id(path: str) -> dict:
    return {r["id"]: r for f in _data(path).get("frameworks", {}).values() for r in f["rules"]}


def rule(rid: str) -> Optional[dict]:
    """The framework rule of an id (data/frameworks.json), None for an unknown one."""
    return _by_id(DATA).get(rid)


def meta_rule(kind: str) -> Optional[str]:
    """The rule id of a hint kind annotation:<id>.meta ("" for the marker annotation:.meta), None for any other kind."""
    return kind[len("annotation:") : -len(META)] if kind.startswith("annotation:") and kind.endswith(META) else None


def inherit(own: Mapping[str, Iterable[str]], chain: Mapping[str, Iterable[str]]) -> Dict[str, Tuple[str, ...]]:
    """{annotation type: rule ids it hands on}: its own (own) and those of every annotation type it uses (chain), cycle safe."""
    out = {}
    for k in sorted(set(own) | set(chain)):
        seen, todo, ids = set(), [k], set()
        while todo:
            a = todo.pop()
            if a not in seen:
                seen.add(a)
                ids.update(own.get(a, ()))
                todo.extend(chain.get(a, ()))
        out[k] = tuple(sorted(ids))
    return out


# ------------------------------------------------------------------ modifiers, annotations, definitions
def split_mods(text: str) -> Tuple[List[str], List[str]]:
    """(modifier words, annotation simple names) of a modifiers text."""
    s = _STR.sub('""', text)
    while True:
        t = _PAR.sub("", s)
        if t == s:
            break
        s = t
    return re.findall(r"\w+", _ANN.sub(" ", s)), [m.group(1).rpartition(".")[2] for m in _ANN.finditer(s)]


def ann_strings(mods: str, ann: str, attrs: Optional[Tuple[str, ...]] = None) -> List[str]:
    """The string literals of the arguments of annotation `ann` in a modifiers text: @MethodSource("a", "b") -> [a, b]. With `attrs`
    only the strings of those attributes (a bare string is the attribute `value`): @Bean(name = "n", initMethod = "m") -> [m].
    """
    out = []
    for m in re.finditer(r"@(?:[\w.]*\.)?" + re.escape(ann) + r"\s*\(", mods):
        depth, i, lit = 1, m.end(), None
        while i < len(mods) and depth:
            ch = mods[i]
            if lit:
                lit = None if ch == lit else lit
                i += 2 if ch == "\\" else 1
                continue
            lit = ch if ch in "\"'" else None
            depth += (ch == "(") - (ch == ")")
            i += 1
        args = mods[m.end() : i]
        for part in (
            re.split(r",(?=\s*\w+\s*=)", args) if attrs else [args]
        ):  # "name = value" parts; `{"a", "b"}` stays whole
            key = re.match(r"\s*(\w+)\s*=", part)
            if not attrs or (key.group(1) if key else "value") in attrs:
                out += [x[1:-1] for x in _STR.findall(part)]
    return out


_METHOD = re.compile(r"(?:[\w.$]*#)?([\w$]+)")  # "m", "#m", "T#m"
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _schema_refs(spec: dict, mods: str, ann: str, name: str, anns: Iterable[str]) -> List[str]:
    """The refs `<group>:[<type>.]<name>` of a rule with "schema" for annotation `ann` of a definition called `name` (see the docstring)."""
    if set(spec.get("unless", ())) & set(anns):
        return []
    attrs = tuple(spec.get("attrs", ()))
    names = (ann_strings(mods, ann, attrs) if attrs else []) or (
        [name, *([_CAMEL.sub("_", name).lower()] if spec.get("snake") else [])] if spec.get("self") else []
    )
    types = (ann_strings(mods, ann, tuple(spec["type_attrs"])) if spec.get("type_attrs") else []) or (
        [spec["type"]] if spec.get("type") else []
    )
    pre = [t + "." for t in types] or [""]
    return [f"{spec['group']}:{p}{n}" for p in pre for n in names]


def _named(rule: dict, mods: str, ann: str) -> List[str]:
    """Methods that the string arguments of annotation `ann` name (rule keys targets, attrs): "string" = of the same class (a
    "T#m" names another class: skipped), "member" = of any class (the type a factory method returns): the ref "*.m"."""
    out = []
    for s in ann_strings(mods, ann, tuple(rule.get("attrs", ())) or None):
        m = _METHOD.fullmatch(s)
        if m and rule["targets"] == "member":
            out.append(f"*.{m.group(1)}")
        elif m and (s.startswith("#") or "#" not in s):
            out.append(m.group(1))
    return out


def _is_main(m: dict, kind: str, qual: str, name: str, words: List[str], anns: List[str]) -> bool:
    if not (m and kind in ("method", "func") and name in m["names"] and all(w in words for w in m["mods"])):
        return False
    return not (m.get("toplevel") and "." in qual and not set(m.get("ann", ())) & set(anns))


def def_facts(
    rl: Rules,
    kind: str,
    qual: str,
    name: str,
    mods: str,
    line: int,
    parent_kind: str,
    parent: Optional["DefFacts"] = None,
    meta: bool = False,
) -> DefFacts:
    """`parent`: the facts of the enclosing class, whose rules may give its public accessors a root (data: accessors). `meta`: the
    definition is an annotation type (query capture @meta; also a modifier word of meta_mods): see the module docstring.
    """
    words, anns = split_mods(mods)
    vis = next((rl.vis[w] for w in words if w in rl.vis and w != "default"), rl.vis.get("default", "public"))
    if vis == "package" and parent_kind == "iface" and not set(words) & {"private", "protected", "internal"}:
        vis = "public"  # an interface member is public unless it says otherwise
    flags = 0
    for w in words + anns:
        if w in rl.flags:
            flags |= FLAG_NAMES[rl.flags[w]]
    if kind == "method" and name in rl.protocol:
        flags |= F_PROTOCOL  # called by the language or the JDK, never by name: its class owns it
    meta = meta or bool(rl.meta_mods & set(words))
    sides = tuple(SIDE.values()) if meta else (SIDE.get(kind),)
    matched = [(a, r) for a in anns + [MOD + w for w in words] for r in rl.by_ann.get(a, ()) if r["on"] in sides]
    hits = {(r["id"], r.get("kind") or f"annotation:{r['id']}") for _, r in matched if not r.get("ref_only")}
    if meta:  # an annotation type hands its rules on to the defs that use it and has no root of its own
        marks = {f"annotation:{i}{META}" for i, _ in hits} | {f"annotation:{META}"}
        return DefFacts(
            vis, flags, tuple(sorted(RootHint(k, qual, line, "hard", "convention") for k in marks)), tuple(anns), ()
        )
    calls = sorted({c for a, r in matched if r.get("targets") for c in _named(r, mods, a)})
    schema = sorted({c for a, r in matched if r.get("schema") for c in _schema_refs(r["schema"], mods, a, name, anns)})
    roots = {RootHint(k, qual, line, "hard", "convention") for _, k in hits}
    if parent and kind == "method" and vis == "public" and rl.acc and rl.acc.match(name):
        roots |= {
            RootHint(f"annotation:{i}.accessor", qual, line, "hard", "convention")
            for i in parent.rules
            if i in rl.acc_ids
        }
    if _is_main(rl.main, kind, qual, name, words, anns):
        roots.add(RootHint("main", qual, line, "hard", "convention"))
        if "." in qual:  # the class that hosts main is started with it
            roots.add(RootHint("main", qual.rpartition(".")[0], line, "hard", "convention"))
    return DefFacts(
        vis,
        flags | (F_DECORATED if hits else 0),
        tuple(sorted(roots)),
        tuple(anns),
        tuple(sorted(i for i, _ in hits)),
        tuple(calls),
        tuple(schema),
    )


def param_roots(rl: Rules, mods: str, qual: str, line: int) -> Tuple[RootHint, ...]:
    """The roots of method `qual` from the annotations of one of its parameters (rules with "on": "param", CDI @Observes)."""
    hits = {
        r.get("kind") or f"annotation:{r['id']}"
        for a in split_mods(mods)[1]
        for r in rl.by_ann.get(a, ())
        if r["on"] == "param"
    }
    return tuple(RootHint(k, qual, line, "hard", "convention") for k in sorted(hits))


def pkg_dir(lang: str, path: str) -> str:
    """The directory that is the package of a file: the source-set part (src/main/java, src/test/kotlin) is no part of it, so the
    main and the test tree of one package are one scope. A path with no such part is its own directory."""
    rl, d = get(lang), posixpath.dirname(path)
    return rl.src_root.sub("", d, count=1) if rl and rl.src_root else d


# ------------------------------------------------------------------ types, receivers, injection
def _last_arg(args: str) -> str:
    depth, start = 0, 0
    for i, ch in enumerate(args):
        depth += (ch == "<") - (ch == ">")
        if ch == "," and depth == 0:
            start = i + 1
    return args[start:]


def type_name(rl: Rules, text: str, wrap: bool) -> str:
    """Head type of a type text as written ("a.B", "B"); "" for a primitive, `var` or text that is no type. Annotations, `?`
    and array brackets are dropped; with `wrap` a wrapper from the data (List<B>, Map<K, B>) gives its last argument."""
    s = _ANN.sub("", _PAR.sub("", text)).strip().rstrip("?").strip()
    while s.endswith("[]"):
        s = s[:-2].rstrip()
    m = _TYPE.match(s)
    if not m:
        return ""
    head, args = m.group(1), m.group(2)
    if wrap and args and head.rpartition(".")[2] in rl.di.get("wrappers", ()):
        return type_name(rl, _VARIANCE.sub("", _last_arg(args).strip()), True)
    return "" if head[:1].islower() and "." not in head else head


def qualify(rl: Rules, find: Callable[[str], Optional[str]], recv: str, name: str) -> str:
    """The name a call `recv.name(...)` is linked by: this.x.m -> x.m; a variable whose declared type T find() knows -> "T.m" (the
    interface or base method: link adds the override edges); a name or a dotted name as written (a class, an unknown variable);
    anything else (a call chain, a literal) -> "*.m", which link reads as "a member named m"."""
    r = " ".join(recv.split())
    r = r[5:] if r.startswith("this.") else r
    if r in ("this", "super"):
        return f"{r}.{name}"
    if _SIMPLE.match(r):
        return f"{find(r) or r}.{name}"
    return f"{r}.{name}" if _DOTTED.match(r) else f"*.{name}"


def injects(rl: Rules, var_kind: str, mods: str, owner: Optional[DefFacts]) -> bool:
    """A constructor parameter (data: di.ctor), a field or property with an inject annotation, a parameter of a method that
    carries an inject annotation or matches one of di.param_rules."""
    di = rl.di
    if var_kind == "ctor":
        return bool(di.get("ctor"))
    inject = set(di.get("inject", ()))
    if var_kind == "param":
        return bool(owner) and bool(inject & set(owner.anns) or set(di.get("param_rules", ())) & set(owner.rules))
    return bool(inject & set(split_mods(mods)[1]))


def parse_import(rl: Rules, text: str, line: int) -> Optional[Import]:
    m = rl.imp.match(text) if rl.imp else None
    if not m:
        return None
    g = m.groupdict()
    return Import(g["m"], ("*",) if g.get("star") else (), g.get("as") or "", line, 0, "")


def static_class(rl: Rules, text: str) -> str:
    """`import static a.b.C.m;` and `import static a.b.C.*;` use the class C (a member import has no other trace in the code); the
    simple name: a dotted one would resolve to the file, and the class is what the import takes the member from."""
    m = rl.imp.match(text) if rl.imp else None
    if not m or not m.groupdict().get("static"):
        return ""
    return (m["m"] if m.groupdict().get("star") else m["m"].rpartition(".")[0]).rpartition(".")[2]
