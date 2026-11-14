"""graph.classreg: classes that registration files name. Standard library only.

A service provider is a class that nothing calls by name: ServiceLoader reads META-INF/services/<interface> (or `provides S with C` of
module-info.java), Spring Boot reads spring.factories, a servlet container reads WEB-INF/web.xml, Android reads its manifest. What
each file kind is and where it names a class is DATA (data/frameworks.json "class_registries": path regex, reader, tags, attributes);
this module only has the five readers (lines, props, match, xml, dotted).

    ClassRegistryRoots      the root extractor "classreg": RootHint("classreg", "class:<name>[#<method>]", line, "hard", "registered")
                            for every class (and method: Spring XML init-method) that such a file names, in file order
    bind(nodes, roots) -> Tuple[Root]
                            roots with a "class:" selector (graph.roots.resolve keeps them: a file path cannot tell which node of the
                            tree is the class) become roots of the class nodes of the linked graph; levels calls it where the roots
                            are merged into the graph. `a.b.C` matches a class whose package directory and name end with it (a
                            package is its directory, as in graph.link; `$` is a nesting dot); `#m` roots the methods of that name; a
                            name that matches no class is a library class: no root and no finding (an unresolved path of any other
                            registration is silent when it leaves the repository, too). A language with package_loose in
                            langs.json (Kotlin: the package need not match the directory) also matches by the simple name when no
                            class matches by its package
                            roots with a "build:<qual>" selector (a recipe or a stage that a doc names, `make test`: graph.roots.build_targets)
                            become roots of the nodes whose qual is that (`recipe:test`, `stage:dev`, any config language); a doc
                            that names no recipe of the tree gives no root and no finding
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from typing import Dict, Iterable, Iterator, List, NamedTuple, Optional, Tuple

from . import langs as _langs
from .contracts import RootExtractor
from .extract import lang_rules
from .model import Node, Root, RootHint, node_key, root as mk_root, split_key

SCHEME = "class:"
BUILD_SCHEME = "build:"
CLASSY = ("class", "iface")
_NAME = re.compile(r"\.?[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
_JAVA_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_XML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_ATTR = re.compile(r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_LIST = re.compile(r"[^\s,\\]+")
_QTAG = re.compile(r"<([A-Za-z_]\w*(?:\.[A-Za-z_][\w$]*)+)[\s/>]")  # <com.acme.View ...>: an element named like a class
_DOTTED = re.compile(r"[A-Za-z_$][\w$]*(?:\.[\w$]+)+")
_ANY = r"[\w:.-]+"  # any element name


class Rule(NamedTuple):
    spec: dict
    files: "re.Pattern"
    need: Optional["re.Pattern"]
    rx: Optional["re.Pattern"]
    shape: Optional["re.Pattern"] = None  # a name that does not fullmatch it is no class (values of a config file)


@functools.lru_cache(maxsize=4)
def _load(path: str) -> Tuple[Tuple[Rule, ...], str]:
    with open(path, encoding="utf-8") as fh:
        rules = json.load(fh)["class_registries"]["rules"]
    comp = tuple(
        Rule(
            r,
            re.compile(r["files"]),
            re.compile(r["need"]) if r.get("need") else None,
            re.compile(r["re"]) if r.get("re") else None,
            re.compile(r["name"]) if r.get("name") else None,
        )
        for r in rules
    )
    return comp, hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()[:8]


def rules() -> Tuple[Rule, ...]:
    return _load(lang_rules.DATA)[0]


# ------------------------------------------------------------------ readers: (spec, text) -> [(line, class text, [methods])]
Found = Tuple[int, str, Tuple[str, ...]]


def _blank(comment: "re.Pattern", text: str) -> str:
    """The comments of `comment` replaced by spaces: offsets and line numbers stay (// is no comment in XML: a namespace URL)."""
    return comment.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _lines(spec: dict, text: str, rx) -> Iterator[Found]:
    for n, raw in enumerate(text.split("\n"), 1):
        yield n, raw.split("#", 1)[0].strip(), ()


def _props(spec: dict, text: str, rx) -> Iterator[Found]:
    """key=value lines of a .properties file: every item of a value (a backslash continues the line) is a class."""
    logical, start = "", 0
    for n, raw in enumerate(text.split("\n"), 1):
        s = raw.strip()
        if not logical and s[:1] in ("#", "!", ""):
            continue
        start = start or n
        logical += s[:-1] if s.endswith("\\") else s
        if not s.endswith("\\"):
            for item in _LIST.findall(logical.partition("=")[2]):
                yield start, item, ()
            logical, start = "", 0


def _match(spec: dict, text: str, rx) -> Iterator[Found]:
    text = _blank(_JAVA_COMMENT, text)
    for m in rx.finditer(text):
        for item in _LIST.finditer(m.group(1)):
            yield _line(text, m.start(1) + item.start()), item.group(), ()


def _dotted(spec: dict, text: str, rx) -> Iterator[Found]:
    """Properties and YAML: every dotted name in a value (after the first = or :, or in a `- item`) is a class candidate."""
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.split("#", 1)[0]
        value = line.partition("=")[2] if "=" in line else line.partition(":")[2] if ":" in line else line.lstrip(" -")
        for m in _DOTTED.finditer(value):
            yield n, m.group(), ()


def _xml(spec: dict, text: str, rx) -> Iterator[Found]:
    text = _blank(_XML_COMMENT, text)
    if spec.get("tags") == "qualified":  # <com.acme.View ...>: the element is the class
        for m in _QTAG.finditer(text):
            yield _line(text, m.start()), m.group(1), ()
    for tag in spec.get("text", ()):  # <servlet-class>p.S</servlet-class>
        for m in re.finditer(rf"<{re.escape(tag)}\b[^>]*>\s*([^<\s][^<]*?)\s*</{re.escape(tag)}\s*>", text):
            yield _line(text, m.start(1)), m.group(1), ()
    # <bean class="p.B" init-method="start"/>; the tag * is any element
    for tag, names in spec.get("attrs", {}).items():
        for m in re.finditer(rf"<{_ANY if tag == '*' else re.escape(tag)}\b([^>]*)>", text):
            at = {k: a or b for k, a, b in _ATTR.findall(m.group(1))}
            for name in names:
                if at.get(name):
                    yield _line(text, m.start()), at[name], tuple(at[k] for k in spec.get("members", ()) if at.get(k))


READERS = {"lines": _lines, "props": _props, "match": _match, "xml": _xml, "dotted": _dotted}


def _hints(rule: Rule, text: str) -> List[RootHint]:
    out = set()
    for line, name, members in READERS[rule.spec["read"]](rule.spec, text, rule.rx):
        name = name.replace("$", ".").lstrip(".") if _NAME.fullmatch(name) else ""
        if name and (rule.shape is None or rule.shape.fullmatch(name)):
            out.add(RootHint("classreg", SCHEME + name, line, "hard", "registered"))
            out.update(
                RootHint("classreg", f"{SCHEME}{name}#{m}", line, "hard", "registered")
                for m in members
                if m.isidentifier()
            )
    return sorted(out)


class ClassRegistryRoots(RootExtractor):
    name, version = "classreg", 1

    @property
    def ext_ver(self) -> str:  # the rules are data: a changed rule re-reads the files
        return f"{self.name}@{self.version}.{_load(lang_rules.DATA)[1]}"

    def matches(self, path: str) -> bool:
        return any(r.files.search(path) for r in rules())

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        text = text.lstrip("\ufeff")
        hits = {
            h
            for r in rules()
            if r.files.search(path) and (r.need is None or r.need.search(text))
            for h in _hints(r, text)
        }
        return sorted(hits, key=lambda h: (h.line, h.target))


# ------------------------------------------------------------------ binding to the nodes of the graph
def bind(nodes: Iterable[Node], roots: Iterable[Root]) -> Tuple[Root, ...]:
    """`roots` with every "class:<name>[#<method>]" selector root replaced by roots of the nodes it names and every "build:<qual>"
    selector root by roots of the nodes of that qual (see the module docstring); the other roots are returned as they are.
    """
    roots, nodes = tuple(roots), tuple(nodes)
    return _bind_builds(nodes, _bind_classes(nodes, roots))


def _bind_builds(nodes: Tuple[Node, ...], roots: Tuple[Root, ...]) -> Tuple[Root, ...]:
    want = [r for r in roots if r.selector.startswith(BUILD_SCHEME)]
    if not want:
        return roots
    by_qual: Dict[str, List[Node]] = {}
    for n in nodes:
        by_qual.setdefault(split_key(n.key)[2], []).append(n)
    out = [r for r in roots if r not in want]
    for r in want:
        for n in by_qual.get(r.selector[len(BUILD_SCHEME) :], ()):
            keys = (n.key, node_key(n.lang, n.path))  # a root of a definition also roots its file, as in graph.link
            out += [mk_root(r.kind, r.origin, node=k, strength=r.strength, trust=r.trust) for k in keys]
    return tuple(out)


def _bind_classes(nodes: Tuple[Node, ...], roots: Tuple[Root, ...]) -> Tuple[Root, ...]:
    want = [r for r in roots if r.selector.startswith(SCHEME)]
    if not want:
        return roots
    info = _langs.load().langs
    classes: Dict[str, List[Tuple[str, str, bool]]] = {}  # simple name -> [(qualified name, node key, loose)]
    methods: Dict[Tuple[str, str, str], List[str]] = {}  # (path, class qual, name) -> node keys of the overloads
    for n in nodes:
        if not (info.get(n.lang) or {}).get("package_dir"):
            continue
        qual = split_key(n.key)[2]
        if n.kind in CLASSY:
            pkg = [x for x in lang_rules.pkg_dir(n.lang, n.path).split("/") if x]
            classes.setdefault(qual.rpartition(".")[2], []).append(
                (".".join([*pkg, qual]), n.key, bool(info[n.lang].get("package_loose")))
            )
        elif n.kind == "method":
            cls, _, name = qual.rpartition(".")
            methods.setdefault((n.path, cls, name), []).append(n.key)
    out = [r for r in roots if r not in want]
    for r in want:
        name, _, method = r.selector[len(SCHEME) :].partition("#")
        cands = classes.get(name.rpartition(".")[2], ())
        hit = [c for c in cands if c[0] == name or c[0].endswith("." + name)]
        for _full, key, _loose in hit or [c for c in cands if c[2]]:  # a loose package: the simple name decides
            lang, path, qual, _, _ = split_key(key)
            keys = methods.get((path, qual, method), []) if method else [key]
            if keys:  # a root of a definition also roots its file, as in graph.link
                keys.append(node_key(lang, path))
            out += [mk_root(r.kind, r.origin, node=k, strength=r.strength, trust=r.trust) for k in keys]
    return tuple(out)
