"""graph.xlink: the link side of the cross-language edges (the facts and their data: graph.extract.xlang). Standard library only.

    xi = index(ix, files)                      once, serial: the native symbols and the environment writers of the whole tree
    targets(ix, path, group, ref) -> (dsts, rule)    dsts = [(node key, tier)] for a ref of kind lit | ffi | env, rule = the evidence rule

lit  the file that the string names EXACTLY (relative to the file, else to the tree root; never the file itself): N.
ffi  the top-level defs named like the symbol in the exporting language groups (data/xlang.json ffi_groups) other than the caller's own:
     T when there is exactly one, else N. A JNI name (Java_pkg_Cls_m) also reaches the overloads `Java_pkg_Cls_m__<signature>` (N).
env  a read `r:NAME` reaches the code that writes `w:NAME` in another language group (the node that holds the write: its definition, else
     its file): N, always. The direction is use -> provider: a live reader keeps the writer it depends on. A name that every process has
     (HOME, PATH, data env_ambient) or that more than env_max_writers places write links nothing: over-wide is no information.
schema  `<group>:<name>` (data xlang.json schema): the objects of the schema languages (graph.extract.schema; the group is the family of the file: sql, proto,
     graphql) whose qualified name ends like the name, or the name like theirs (`orders` = `public.orders`; sql case-insensitive), N from code, T from a schema file
     when there is one candidate; the candidates of the referencing file win. A name that no object has falls back to its owner where the group says so
     (`Book.author` -> the type Book). The fields of a root type that a `schema { query: RootQuery }` block names are
     also the fields `Query.<field>` (the ref `graphql:=query` of the type), the resolvers of the root fields are matched by name without case and underscores
     (`books`, `getBooks`, `resolve_books`, `book_by_id` = `bookById`). A glob (`audit_*`, `*`: a name built at run time) reaches every object that fits, D. `~Node` (an interface, written
     by its implementer) is also an edge from the interface to the implementer, so a type that is only returned as `Node` lives. code_edges: what code says
     without a ref of its own, over the names of the schema files only: the names of the generated code of a proto message or service (stubs: FooGrpc,
     FooBlockingStub, Foo_pb2 ..., data schema.proto.stubs) in a ref, and the members of a resolver class (`QueryResolver.books`, data schema.graphql.resolvers)
     as the resolvers of the root field `Query.books`; both N.
Evidence rules: lit, ffi, env, cfg (a configuration key: System.getProperty, viper), schema, stub, resolver. Every edge here is a tier that graph.reach classes as at
most weak-alive (N) or alive through a unique native symbol (T); nothing here can make a finding, only suppress a dead verdict.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from bisect import bisect_left
from functools import lru_cache
from typing import Dict, List, Mapping, NamedTuple, Tuple

from .extract import xlang
from .model import Facts, Ref

KINDS = ("lit", "ffi", "env", "schema")
JNI = "Java_"
STUB_KINDS = ("call", "ref", "inherit", "impl", "di")  # the refs of code that may spell a generated stub
_ESC = re.compile(r"%([0-9A-F]{2})")
_CAMEL = re.compile(r"(?:^|_)([a-z0-9])")


class XIx(NamedTuple):
    syms: Dict[str, Tuple[Tuple[str, str], ...]]  # symbol -> ((group, node key), ..) of the exporting groups
    jni: Tuple[str, ...]  # the sorted symbols that start with Java_ (the overloads of a native method share a prefix)
    writers: Dict[str, Tuple[Tuple[str, str, str], ...]]  # env name -> ((group, path, scope), ..)
    names: Dict[Tuple[str, str], Tuple[Tuple[Tuple[str, ...], str, str], ...]] = (
        {}
    )  # (group, last segment) -> ((segments, node key, path), ..)
    stubs: Dict[str, Tuple[str, ...]] = {}  # a name that the generated code of a proto object has -> node keys
    fields: Dict[Tuple[str, str], Tuple[str, ...]] = (
        {}
    )  # (operation, field), lower case -> node keys of the root fields
    rev: Dict[str, Tuple[str, ...]] = {}  # an interface -> the types that implement it


def rules() -> list:
    """The data this side reads (part of LINKER_V: the cache key of a graph)."""
    d = xlang.data()
    return [d["ffi_groups"], d["env_ambient"], d["env_max_writers"], d["schema"]]


def _camel(stem: str) -> str:
    return _CAMEL.sub(lambda m: m.group(1).upper(), stem)


def _norm(field: str) -> str:
    """A field name as a resolver may spell it: lower case, no underscore (`book_by_id`, `bookById`, `BookById`)."""
    return field.lower().replace("_", "")


def _schema(ix, files: Mapping[str, Facts]) -> Tuple[dict, dict, dict]:
    """(names, stubs, fields) of the objects of the schema files: see the module docstring (the index of the schema refs)."""
    d, names, stubs, fields = xlang.data()["schema"], {}, {}, {}
    for p in sorted(files):
        grp = ix.paths[p][1]
        spec = d["groups"].get(grp)
        if spec is None:
            continue
        stem, svc = posixpath.splitext(posixpath.basename(p))[0], []
        split = lambda esc: tuple(  # noqa: E731
            (x.lower() if spec.get("fold") else x) for x in _ESC.sub(lambda m: chr(int(m.group(1), 16)), esc).split(".")
        )
        ops = {  # `schema { query: RootQuery }` (a ref `graphql:=query` from the type): the fields of RootQuery are also `Query.<field>`
            split(r.scope.partition(":")[2])[0]: r.name[len("graphql:=") :]
            for r in files[p].refs
            if grp == "graphql" and r.kind == "schema" and r.name.startswith("graphql:=")
        }
        for qual, keys in sorted(ix.defs[p].items()):
            tag, _, esc = qual.partition(":")
            segs = split(esc)
            alias = (ops[segs[0]].capitalize(), *segs[1:]) if tag == "field" and segs[0] in ops else None
            for k in keys:
                for sg in (segs, alias) if alias else (segs,):
                    names.setdefault((grp, sg[-1]), []).append((sg, k, p))
                if tag in ("message", "enum"):
                    stubs.setdefault(segs[-1], []).append(k)
                elif tag == "service":
                    svc.append(k)
                    for t in d["proto"]["stubs"]["service"]:
                        stubs.setdefault(t.format(n=segs[-1]), []).append(k)
                elif tag == "field":
                    for sg in (segs, alias) if alias else (segs,):
                        fields.setdefault((sg[0].lower(), _norm(sg[-1])), []).append(k)
        st = d["proto"]["stubs"]
        for key, to in (("file", [ix.paths[p][2]]), ("file_services", svc)):
            for t in st[key] if grp == "proto" else ():
                for n in {t.format(f=stem, F=_camel(stem)), t.format(f=stem.lower(), F=_camel(stem.lower()))}:
                    stubs.setdefault(n, []).extend(to)
    return names, {k: tuple(v) for k, v in stubs.items()}, {k: tuple(v) for k, v in fields.items()}


def _resolve(xi: XIx, path: str, g: str, ref: str) -> List[Tuple[str, str]]:
    """[(node key, tier)] of a schema ref `<group>:[~]<name>` written in `path`, a file of group `g` (see the module docstring)."""
    grp, _, name = ref.partition(":")
    spec = xlang.data()["schema"]["groups"].get(grp)
    if spec is None:
        return []
    name = name.lstrip("~")
    if name.startswith("="):  # `=query`: the operation that the type of the ref is the root of (_schema), no object
        return []
    segs = tuple((x.lower() if spec.get("fold") else x) for x in name.split("."))
    if (
        "*" in name
    ):  # a name built at run time reaches every object that fits: its last segment, or its whole name with a dot in the glob
        pat = ".".join(segs)
        fit = lambda sg: fnmatch.fnmatchcase(".".join(sg) if "." in pat else sg[-1], pat)  # noqa: E731
        return [(k, "D") for (gr, _), ents in sorted(xi.names.items()) if gr == grp for sg, k, _ in ents if fit(sg)]
    for cut in range(len(segs), 0, -1) if spec.get("parent") else (len(segs),):
        want = segs[:cut]
        cand = [
            (k, p)
            for sg, k, p in xi.names.get((grp, want[-1]), ())
            if sg[-len(want) :] == want or want[-len(sg) :] == sg
        ]
        if cand:
            near = list(dict.fromkeys([c for c in cand if c[1] == path] or cand))
            return [(k, "T" if g == grp and len(near) == 1 else "N") for k, _ in near]
    return []


@lru_cache(maxsize=1)
def _resolvers() -> "re.Pattern":
    return re.compile(xlang.data()["schema"]["graphql"]["resolvers"])


def code_edges(ix, path: str, facts: Facts) -> List[tuple]:
    """(source keys, [(node key, tier)], line, rule, text) of what code says to the schema files without a ref of its own (module docstring)."""
    xi, out = ix.xl, []
    for r in facts.refs if xi.stubs else ():
        if r.kind in STUB_KINDS:
            hit = list(dict.fromkeys(k for seg in re.split(r"[.:]+", r.name) for k in xi.stubs.get(seg, ())))
            if hit:
                out.append((ix.src_keys(path, r.scope), [(k, "N") for k in hit], r.line, "stub", r.name))
    for qual, keys in ix.defs[path].items() if xi.fields else ():
        cls, _, member = qual.rpartition(".")
        op = _resolvers().match(cls.rpartition(".")[2]) if cls else None
        for cand in {_norm(member), _norm(re.sub(r"^(?:get|resolve|fetch)_?", "", member, flags=re.I))} if op else ():
            hit = xi.fields.get((op.group(1).lower(), cand), ())
            if hit:
                out.append((keys, [(k, "N") for k in hit], ix.line[keys[0]], "resolver", qual))
    return out


def implementers(ix, path: str) -> List[Tuple[str, str]]:
    """(interface, implementer) of the interfaces defined in `path`: the edge from the interface to the types that implement it."""
    xi = ix.xl
    return [(k, impl) for ks in ix.defs[path].values() for k in ks for impl in xi.rev.get(k, ())] if xi.rev else []


def index(ix, files: Mapping[str, Facts]) -> XIx:
    groups = frozenset(xlang.data()["ffi_groups"])
    syms: Dict[str, List[Tuple[str, str]]] = {}
    for (g, name), keys in sorted(ix.top.items()):
        if g in groups:
            syms.setdefault(name, []).extend((g, k) for k in keys)
    writers: Dict[str, List[Tuple[str, str, str]]] = {}
    for p in sorted(files):
        for r in files[p].refs:
            if r.kind == "env" and r.name.startswith("w:"):
                writers.setdefault(r.name[2:], []).append((ix.paths[p][1], p, r.scope))
    names, stubs, fields = _schema(ix, files)
    xi = XIx(
        {k: tuple(v) for k, v in syms.items()},
        tuple(sorted(n for n in syms if n.startswith(JNI))),
        {k: tuple(sorted(set(v))) for k, v in writers.items()},
        {k: tuple(v) for k, v in names.items()},
        stubs,
        fields,
    )
    rev: Dict[str, List[str]] = {}
    for p in sorted(files) if names else ():  # `~Node` written by an implementer: the interface reaches it too
        for r in files[p].refs:
            if r.kind == "schema" and r.name.partition(":")[2].startswith("~"):
                for dst, _ in _resolve(xi, p, ix.paths[p][1], r.name):
                    rev.setdefault(dst, []).extend(ix.src_keys(p, r.scope))
    return xi._replace(rev={k: tuple(dict.fromkeys(v)) for k, v in rev.items()})


def _ffi(xi: XIx, g: str, name: str) -> List[Tuple[str, str]]:
    cands = [name] + [n for n in _overloads(xi, name)]
    hit = [k for n in cands for gr, k in xi.syms.get(n, ()) if gr != g]
    return [(k, "T" if len(hit) == 1 else "N") for k in hit]


def _overloads(xi: XIx, name: str):
    if not name.startswith(JNI):
        return
    pre = name + "__"
    for i in range(bisect_left(xi.jni, pre), len(xi.jni)):
        if not xi.jni[i].startswith(pre):
            break
        yield xi.jni[i]


def targets(ix, path: str, g: str, r: Ref) -> Tuple[List[Tuple[str, str]], str]:
    xi = ix.xl
    if r.kind == "schema":
        return _resolve(xi, path, g, r.name), "schema"
    if r.kind == "lit":
        n = posixpath.normpath(r.name)
        hit = {
            p
            for p in (posixpath.normpath(posixpath.join(posixpath.dirname(path), n)), n)
            if p in ix.paths and p != path
        }
        return [(ix.paths[p][2], "N") for p in sorted(hit)], "lit"
    if r.kind == "ffi":
        return _ffi(xi, g, r.name), "ffi"
    cfg = r.name.startswith("r:cfg:")
    if not r.name.startswith("r:"):
        return [], "env"  # a write is no use of anything
    name, d = r.name[2:], xlang.data()
    ws = [w for w in xi.writers.get(name, ()) if w[0] != g]
    if name in d["env_ambient"] or len(ws) > d["env_max_writers"]:
        return [], "env"  # ambient or over-wide: no information
    keys = [k for _, pw, sc in ws for k in ix.src_keys(pw, sc)]
    return [(k, "N") for k in dict.fromkeys(keys)], "cfg" if cfg else "env"
