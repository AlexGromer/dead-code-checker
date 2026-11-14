"""Stand-ins for tree-sitter-java and tree-sitter-kotlin, for the tests of graph.extract.ts_generic with the java/kotlin queries.

FakeJava answers `parsed.run(kind)` in the capture vocabulary of dead_code_checker/graph/data/queries/java/*.scm (defs, refs, types, imports,
roots) for the golden Spring fixtures in tests/fixtures/jvm: one declaration per line, annotations on lines of their own or
before the modifiers, `}` (or `};`, `},`, `});`) alone on a line. It knows blocks (`... {`), constructors and initializer blocks
(@scope regions), anonymous classes (`new T(...) {` in a method or as a field initializer, with the members of the class as
declarations: @anon regions), annotation types (`@interface`: the @meta capture), annotation uses (ref.reg), enum constants with a body, method references (`this::m`, `Type::m`, `Type::new`), lambda
parameters and the annotations of a parameter (@mods of a @var.param). It is NOT a Java parser and says nothing about the
real grammar: it exercises the facts MAPPING (what ts_generic and lang_rules do with a match). The real-grammar test is skipped while no grammar is
installed. FakeKotlin answers canned matches for chosen spans of a source.
"""

import re
from typing import NamedTuple

MODS = r"(?:(?:public|private|protected|static|final|abstract|default|synchronized)\s+)*"
TYPE = r"[\w.<>\[\], ?]+?"
RE_TYPE = re.compile(
    rf"^(?P<mods>{MODS})(?P<kw>class|@?interface|enum|record)\s+(?P<name>\w+)(?:<[^>]*>)?"
    r"(?:\s+extends\s+(?P<ext>[^{]+?))?(?:\s+implements\s+(?P<impl>[^{]+?))?\s*\{$"
)
RE_METHOD = re.compile(
    rf"^(?P<mods>{MODS})(?:<[^>]+>\s+)?(?P<ret>{TYPE})\s+(?P<name>\w+)\((?P<params>[^)]*)\)\s*(?P<end>\{{|;)$"
)
RE_CTOR = re.compile(rf"^(?P<mods>{MODS})(?P<name>\w+)\((?P<params>[^)]*)\)\s*\{{$")
RE_FIELD = re.compile(rf"^(?P<mods>{MODS})(?P<type>{TYPE})\s+(?P<name>\w+)\s*(?:=.*)?;$")
RE_FIELD_ANON = re.compile(
    rf"^(?P<mods>{MODS})(?P<type>{TYPE})\s+(?P<name>\w+)\s*=\s*new\s+[A-Z][\w<>, ?]*\(.*\)\s*\{{$"
)
RE_ENUM_BODY = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]*)(?:\(.*\))?\s*\{$")  # an enum constant with a body
RE_LOCAL = re.compile(rf"^(?:final\s+)?(?P<type>{TYPE})\s+(?P<name>\w+)\s*=")
RE_CALL = re.compile(r"(?:(?P<recv>[A-Za-z_][\w.]*)\.)?(?P<name>[A-Za-z_]\w*)\(")
RE_FIELD_USE = re.compile(r"(?<![\w.])(?!this\b)([A-Za-z_]\w*)\.\w+\b(?!\s*\()")
RE_MREF = re.compile(r"(?<![\w.])(?P<recv>[A-Za-z_][\w.]*)::(?P<name>\w+)")  # this::m, Type::m, Type::new
KEYWORDS = {"if", "for", "while", "switch", "return", "catch", "super", "this", "new", "synchronized"}


class FNode(NamedTuple):
    start_byte: int
    end_byte: int
    start_point: tuple
    end_point: tuple


class _Base:
    """Text -> nodes. Columns are characters of the line; the sources of the fixtures are ASCII."""

    def __init__(self, text: str, errors: int = 0):
        self.text, self.errors, self.m = text, errors, {}
        self.lines, self.offs, pos = text.split("\n"), [], 0
        for ln in self.lines:
            self.offs.append(pos)
            pos += len(ln.encode()) + 1

    def node(self, r0, c0, r1, c1):
        return FNode(self.offs[r0] + c0, self.offs[r1] + c1, (r0, c0), (r1, c1))

    def find(self, row, needle, col=0):
        c = self.lines[row].index(needle, col)
        return self.node(row, c, row, c + len(needle))

    def add(self, kind, caps):
        self.m.setdefault(kind, []).append((0, caps))

    def run(self, kind):
        return self.m.get(kind, [])


class FakeJava(_Base):
    def __init__(self, text: str, errors: int = 0):
        super().__init__(text, errors)
        stack, pending = (
            [],
            None,
        )  # stack of (kind, name, def kind, def start, caps); pending = (row, col) of the first annotation
        for row, raw in enumerate(self.lines):
            ln, ind = raw.strip(), len(raw) - len(raw.lstrip())
            if not ln or ln.startswith("package "):
                continue
            if ln.startswith("//"):
                self.add("roots", {"root.marker": [self.node(row, ind, row, len(raw))]})
            elif ln.startswith("import "):
                self.add("imports", {"import": [self.node(row, ind, row, len(raw))]})
            elif re.match(r"^@\w+(\(.*\))?$", ln):
                n = self.find(row, re.match(r"@(\w+)", ln).group(1), ind)
                self.add("refs", {"ref.reg": [self.node(row, ind, row, len(raw))], "name": [n]})  # an annotation use
                pending = pending or (row, ind)
            elif re.fullmatch(r"\}[);,]*", ln):
                kind, _, dk, start, caps = stack.pop()
                if dk:
                    self.add("defs", {**caps, dk: [self.node(*start, row, ind + 1)]})
            elif stack and stack[-1][0] in ("method", "ctor", "block"):
                self.statement(row, ln, ind)
                if ln.endswith("{"):
                    anon = re.search(r"\bnew\s+[A-Z][\w<>, ?]*\(.*\)\s*\{$", ln)
                    stack.append(
                        ("type", "", "anon", (row, ind + ln.rindex("{")), {}) if anon else ("block", "", "", None, None)
                    )
            else:
                if not self.declaration(row, ln, ind, pending or (row, ind), stack):
                    raise AssertionError(f"FakeJava: line {row + 1} not understood: {ln!r}")
                pending = None

    def declaration(self, row, ln, ind, start, stack):
        cls = next((s[1] for s in reversed(stack) if s[0] == "type"), "")
        if ln in ("static {", "{"):  # an initializer block: a region of the class like a constructor
            stack.append(("ctor", "", "scope", (row, ind), {}))
            return True
        if RE_ENUM_BODY.match(ln) and stack and stack[-1][2] == "def.class":
            stack.append(("type", "", "anon", (row, ind + ln.rindex("{")), {}))
            return True
        for rx in (RE_TYPE, RE_CTOR, RE_METHOD, RE_FIELD_ANON, RE_FIELD):
            m = rx.match(ln)
            if m and (rx is not RE_CTOR or m["name"] == cls):
                break
        else:
            return False
        words_end = ind + len(m["mods"].rstrip())
        caps = {"name": [self.find(row, m["name"], ind + len(m["mods"]))]}
        if start != (row, ind) or m["mods"]:
            caps["mods"] = [self.node(*start, row, words_end)]
        if rx is RE_TYPE:
            iface = m["kw"].endswith("interface")
            if m["kw"] == "@interface":  # the annotation type capture @meta of java/defs.scm
                caps["meta"] = [self.node(*start, row, len(self.lines[row]))]
            for group, kind in (("ext", "inherit"), ("impl", "impl")):
                base = self.lines[row].index(m[group]) if m[group] else 0
                for t in re.finditer(r"[A-Z]\w*", m[group] or ""):
                    n = self.node(row, base + t.start(), row, base + t.end())
                    self.add("refs", {f"ref.{'inherit' if iface else kind}": [n], "name": [n]})
            stack.append(("type", m["name"], "def.iface" if iface else "def.class", start, caps))
        elif rx is RE_METHOD:
            self.params(row, ln, m["params"], "param")
            self.types(row, m["ret"])
            self.types(row, m["params"])
            if m["end"] == "{":
                stack.append(("method", m["name"], "def.method", start, caps))
            else:
                self.add("defs", {**caps, "def.method": [self.node(*start, row, len(self.lines[row]))]})
        elif rx is RE_CTOR:
            self.params(row, ln, m["params"], "ctor")
            self.types(row, m["params"])
            stack.append(("ctor", m["name"], "scope", start, {}))
        else:
            if rx is RE_FIELD_ANON:
                stack.append(("type", "", "anon", (row, ind + ln.rindex("{")), {}))
            vc = {
                "var.field": [self.node(*start, row, len(self.lines[row]))],
                "type": [self.find(row, m["type"], ind)],
                "name": caps["name"],
            }
            self.add("types", {**vc, **({"mods": caps["mods"]} if "mods" in caps else {})})
            self.types(row, m["type"])
            if rx is RE_FIELD and "=" in ln:  # the initializer is code of the class: lambdas, calls, method references
                eq = ln.index("=") + 1
                self.statement(row, ln[eq:].strip(), ind + eq + len(ln[eq:]) - len(ln[eq:].lstrip()))
        return True

    def params(self, row, ln, params, kind):
        base = self.lines[row].index("(") + 1
        for part in params.split(","):
            p = part.strip()
            if p:
                t, _, n = p.rpartition(" ")
                c = base + part.index(p)
                ann = re.match(
                    r"(?:@\w+(?:\([^)]*\))?\s+)+", t
                )  # annotations before the type: the modifiers of the parameter
                skip = ann.end() if ann else 0
                caps = {
                    "type": [self.node(row, c + skip, row, c + len(t))],
                    "name": [self.node(row, c + len(t) + 1, row, c + len(p))],
                }
                if ann:
                    caps["mods"] = [self.node(row, c, row, c + len(ann.group().rstrip()))]
                whole = [self.node(row, c, row, c + len(p))]
                self.add("types", {f"var.{kind}": whole, **caps})
                if kind == "ctor":  # a constructor parameter is also a parameter: the mapping reads it once
                    self.add("types", {"var.param": whole, **caps})
            base += len(part) + 1

    def types(self, row, text):
        line = self.lines[row]
        col = line.index(text) if text in line else 0
        for t in re.finditer(r"\b[A-Z]\w*", text):
            n = self.node(row, col + t.start(), row, col + t.end())
            self.add("refs", {"name": [n], "ref.ref": [n]})

    def statement(self, row, ln, ind):
        text = ln.rstrip(";")
        m = RE_LOCAL.match(text)
        if m and not m["type"].startswith("return"):
            self.add(
                "types",
                {
                    "var.local": [self.node(row, ind, row, ind + len(text))],
                    "type": [self.find(row, m["type"], ind)],
                    "name": [self.find(row, m["name"], ind + len(m["type"]))],
                },
            )
            self.types(row, m["type"])
        for lam in re.finditer(
            r"(?:\b(\w+)|\(([\w, ]+)\))\s*->", text
        ):  # inferred lambda parameters: a variable with no @type
            for nm in re.finditer(r"\w+", lam.group(1) or lam.group(2)):
                a = lam.start(1 if lam.group(1) else 2) + nm.start()
                n = self.node(row, ind + a, row, ind + a + len(nm.group()))
                self.add("types", {"var.param": [n], "name": [n]})
        for t in re.finditer(r"\bnew\s+([A-Z]\w*)|\b([A-Z]\w*)\.class\b", text):
            g = 1 if t.group(1) else 2
            n = self.node(row, ind + t.start(g), row, ind + t.end(g))
            self.add("refs", {"name": [n], "ref.ref": [n]})
        for mr in RE_MREF.finditer(text):  # the two method_reference patterns of refs.scm
            recv = self.node(row, ind + mr.start("recv"), row, ind + mr.end("recv"))
            whole = self.node(row, ind + mr.start(), row, ind + mr.end())
            if mr["name"] != "new":
                name = self.node(row, ind + mr.start("name"), row, ind + mr.end("name"))
                self.add("refs", {"ref.ref": [whole], "recv": [recv], "name": [name]})
            if re.fullmatch(
                r"(?!this$|super$)[A-Za-z_]\w*", mr["recv"]
            ):  # a plain identifier qualifier is a use of a type
                self.add("refs", {"ref.ref": [whole], "name": [recv]})
        for f in RE_FIELD_USE.finditer(text):  # a.b not followed by `(`: the object of a field access
            n = self.node(row, ind + f.start(1), row, ind + f.end(1))
            self.add("refs", {"name": [n], "ref.ref": [n]})
        for c in RE_CALL.finditer(text):
            if c["name"] in KEYWORDS or text[: c.start()].rstrip().endswith("new"):
                continue
            caps = {
                "ref.call": [self.node(row, ind + c.start(), row, ind + c.end())],
                "name": [self.node(row, ind + c.start("name"), row, ind + c.end("name"))],
            }
            if c["recv"]:
                caps["recv"] = [self.node(row, ind + c.start("recv"), row, ind + c.end("recv"))]
            self.add("refs", caps)


class FakeKotlin(_Base):
    """Canned matches for chosen spans: k.match("defs", **{"def.class": "class Foo", "name": ("Foo", 0)}) where a value is a needle
    or (needle, nth occurrence); a needle spans from its first occurrence on."""

    def span(self, needle, nth=0):
        i = -1
        for _ in range(nth + 1):
            i = self.text.index(needle, i + 1)
        r0, r1 = self.text.count("\n", 0, i), self.text.count("\n", 0, i + len(needle))
        return self.node(
            r0,
            i - (self.text.rfind("\n", 0, i) + 1),
            r1,
            i + len(needle) - (self.text.rfind("\n", 0, i + len(needle)) + 1),
        )

    def match(self, kind, **caps):
        self.add(
            kind, {k.replace("__", "."): [self.span(*(v if isinstance(v, tuple) else (v,)))] for k, v in caps.items()}
        )
        return self
