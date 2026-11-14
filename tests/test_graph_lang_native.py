"""Native languages: C, C++, C# (tree-sitter queries + rules.json) and PowerShell (pwsh AST worker).

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_lang_native.py

What runs where
  PowerShell   a real pwsh 7 parse (skipped with the reason when pwsh is not on this host), plus the facts mapping through the
               worker seam (extract.pwsh.set_worker) that needs no pwsh at all.
  C, C++, C#   the mapping (modifiers, visibility, roots, name rules) is tested through the backend seam (ts_generic.set_backend) with
               hand-placed query matches in the documented capture vocabulary, so it needs no grammar; the queries themselves are
               checked for vocabulary and balance. The tests against the real grammar (golden fixtures under tests/fixtures/native/<lang>/)
               skip with an explicit reason when tree-sitter or the grammar is not installed.
"""

import hashlib
import json
import re
import types
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test

ROOT = Path(__file__).resolve().parent.parent
from dead_code_checker.graph import contracts, langs as dclangs, link as dlink, model, reach as rc, ts as tsm  # noqa: E402
from dead_code_checker.graph.extract import pwsh as pw, ts_generic as tg  # noqa: E402
from dead_code_checker.graph.extract.common import module_def  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "native"
QDIR = Path(tg.QUERY_DIR)


def _pwsh_reason():
    try:
        res = pw._find()
        return "" if res.status == "ok" else f"pwsh is {res.status}: {res.detail}"
    except Exception as exc:  # noqa: BLE001 - a host where the loader is unusable
        return f"pwsh lookup failed: {exc}"


PWSH_REASON = _pwsh_reason()
needs_pwsh = pytest.mark.skipif(bool(PWSH_REASON), reason=f"no usable pwsh engine on this host ({PWSH_REASON})")


def _grammar_reason(g):
    try:
        tsm.get_parser(g)
        return ""
    except LookupError as exc:
        return str(exc)


def needs_grammar(g):
    return pytest.mark.skipif(
        bool(_grammar_reason(g)), reason=f"tree-sitter grammar {g} is not installed on this host: {_grammar_reason(g)}"
    )


def defs_of(f):
    return {d.qual: d for d in f.defs}


def refs_of(f, kind=None):
    return {(r.name, r.kind, r.scope) for r in f.refs if kind in (None, r.kind)}


# ====================================================================== the scripted parser (query matches placed by hand)
class FNode(NamedTuple):
    start_byte: int
    end_byte: int
    start_point: tuple
    end_point: tuple


class Scripted:
    """A parse result whose matches are placed by hand: add(kind, {capture: text | (text, nth) | [..]}). The first capture of a call is the
    anchor (the nth occurrence in the source), the others are searched from the start of the anchor, so `name` finds the name INSIDE
    the definition. Strings are located by text, so the sources of the tests keep their tokens unique where it matters.
    """

    def __init__(self, text, errors=0):
        self.text, self.errors, self.m = text, errors, {k: [] for k in tg.QUERY_KINDS}
        self.data = text.encode()

    def find(self, sub, nth=0, frm=0):
        i = frm - 1
        for _ in range(nth + 1):
            i = self.text.index(sub, i + 1)
        return i

    def node(self, i, sub):
        j = i + len(sub)
        pt = lambda k: (self.text.count("\n", 0, k), k - (self.text.rfind("\n", 0, k) + 1))  # noqa: E731
        return FNode(len(self.text[:i].encode()), len(self.text[:j].encode()), pt(i), pt(j))

    def add(self, kind, caps, pattern=0):
        out, anchor = {}, None
        for key, val in caps.items():
            nodes = []
            for v in val if isinstance(val, list) else [val]:
                sub, nth = v if isinstance(v, tuple) else (v, 0)
                i = self.find(sub, nth, 0 if anchor is None else anchor)
                anchor = i if anchor is None else anchor
                nodes.append(self.node(i, sub))
            out[key] = nodes
        self.m[kind].append((pattern, out))
        return self

    def run(self, kind):
        return self.m[kind]


def extract(lang, text, build, errors=0):
    s = Scripted(text, errors)
    build(s)
    tg.set_backend(lambda g, data: s)
    try:
        return tg.TsGeneric().extract(lang, "x", text)
    finally:
        tg.set_backend(None)


# ====================================================================== C
C_SRC = """#include "util.h"
static int helper(int x) { return x; }
int api(int x) { return helper(x); }
int prototype_only(int);
static int hidden_proto(int);
__attribute__((constructor)) void boot(void) { p->run(1); (*fp)(2); }
int main(void) { return api(0) + table[0](1); }
"""


def build_c(s):
    s.add("defs", {"def.func": "static int helper(int x) { return x; }", "name": "helper", "mod": "static"})
    s.add("defs", {"def.func": "int api(int x) { return helper(x); }", "name": "api"})
    s.add(
        "defs",
        {"def.func": "int prototype_only(int);", "name": "prototype_only", "vis.exported": "int prototype_only(int);"},
    )
    s.add(
        "defs",
        {
            "def.func": "static int hidden_proto(int);",
            "name": "hidden_proto",
            "vis.exported": "static int hidden_proto(int);",
        },
    )
    s.add("defs", {"mod": ("static", 1)})  # the storage class of the second prototype (a direct child of it)
    s.add("defs", {"def.func": "__attribute__((constructor)) void boot(void) { p->run(1); (*fp)(2); }", "name": "boot"})
    s.add("defs", {"def.func": "int main(void) { return api(0) + table[0](1); }", "name": "main"})
    s.add("refs", {"ref.include": '#include "util.h"', "name": '"util.h"'})
    s.add("refs", {"ref.call": "helper(x)", "name": "helper"})
    s.add("refs", {"ref.call": "p->run(1)", "name": "p->run"})
    s.add("refs", {"ref.call": "api(0)", "name": "api"})
    s.add("refs", {"ref.call": "table[0](1)", "name": "table[0]"})  # a computed callee: no function name to keep
    s.add("refs", {"ref.call": "(*fp)(2)", "name": "(*fp)"})
    s.add("roots", {"root.main": "main"})
    s.add("roots", {"root.annotation.c-attribute": "__attribute__((constructor))"})
    s.add(
        "roots", {"root.annotation.c-attribute": "__attribute__"}
    )  # filtered by the rule's regex: no constructor word in this node text
    s.add("stub", {"stub.def": "static int helper(int x) { return x; }", "name": "helper", "body": "{ return x; }"})


def test_c_visibility_static_private_prototype_exported():
    f = extract("c", C_SRC, build_c)
    d = defs_of(f)
    assert d["helper"].vis == "private" and d["api"].vis == "public" and d["main"].vis == "public"
    assert d["prototype_only"].vis == "exported"
    assert d["hidden_proto"].vis == "private"  # `static` wins over the explicit @vis of a prototype
    assert not d["helper"].stub  # `return x;` is a real body
    assert d["api"].kind == "func" and not d["api"].stub


def test_c_refs_name_rules_and_includes():
    f = extract("c", C_SRC, build_c)
    r = refs_of(f)
    assert ("util.h", "include", "") in r
    assert ("helper", "call", "api") in r and ("api", "call", "main") in r
    assert ("p.run", "call", "boot") in r  # "->" is read as "."
    assert not any(n.startswith("table") or n == "?.fp" for n, _, _ in r) or ("?.table", "call", "main") not in r


def test_c_roots_main_and_attribute_filtered_by_rule_text():
    f = extract("c", C_SRC, build_c)
    roots = {(h.kind, h.target, h.strength, h.trust) for h in f.roots}
    assert ("main", "main", "hard", "convention") in roots
    assert ("annotation:c-attribute", "boot", "hard", "convention") in roots
    assert (
        len([h for h in f.roots if h.kind.startswith("annotation:")]) == 1
    )  # the bare "__attribute__" node does not match


def test_c_main_name_filter_rejects_other_functions():
    f = extract("c", C_SRC, lambda s: (build_c(s), s.add("roots", {"root.main": ("api", 0)})))
    assert [h.target for h in f.roots if h.kind == "main"] == ["main"]


# ====================================================================== C++
CPP_SRC = """class Shape { public: virtual double area() const = 0; };
class Square : public Shape { public: double area() const override { return 1; } };
double Shape::twice() const { return 2 * area(); }
extern "C" int plugin_entry(void) { return 1; }
TEST(Suite, Area) { Square s; }
void run() { this->go(); ns::util(1); }
"""


def build_cpp(s):
    s.add("defs", {"def.class": "class Shape { public: virtual double area() const = 0; };", "name": "Shape"})
    s.add(
        "defs", {"def.func": "virtual double area() const = 0;", "name": "area", "mod": "virtual"}
    )  # a method declaration (pure virtual)
    s.add(
        "defs",
        {
            "def.class": "class Square : public Shape { public: double area() const override { return 1; } };",
            "name": "Square",
        },
    )
    s.add("defs", {"def.func": "double area() const override { return 1; }", "name": "area", "mod": "override"})
    s.add("defs", {"def.func": "double Shape::twice() const { return 2 * area(); }", "name": "Shape::twice"})
    s.add("defs", {"def.func": "int plugin_entry(void) { return 1; }", "name": "plugin_entry"})
    s.add("defs", {"def.func": "TEST(Suite, Area) { Square s; }", "name": "TEST"})
    s.add("defs", {"def.func": "void run() { this->go(); ns::util(1); }", "name": "run"})
    s.add("refs", {"ref.inherit": "public Shape", "name": "Shape"})
    s.add("refs", {"ref.call": "2 * area()", "name": "area"})
    s.add("refs", {"ref.call": "this->go()", "name": "this->go"})
    s.add("refs", {"ref.call": "ns::util(1)", "name": "ns::util"})
    s.add("roots", {"root.annotation.extern-c": "int plugin_entry(void) { return 1; }"})
    s.add("roots", {"root.test": "TEST"})
    s.add("roots", {"root.test": "run"})  # not a test macro name
    s.add("roots", {"root.main": "run"})


def test_cpp_qualified_names_and_member_defs():
    f = extract("cpp", CPP_SRC, build_cpp)
    d = defs_of(f)
    assert {"Shape", "Shape.area", "Square", "Square.area", "Shape.twice", "plugin_entry", "TEST", "run"} <= set(d)
    assert d["Shape.area"].kind == "method" and d["Square.area"].kind == "method"
    assert d["Shape.twice"].kind == "func"  # an out-of-class definition is a top-level func named "Shape.twice"


def test_cpp_virtual_and_override_flags_drive_dispatch_edges():
    d = defs_of(extract("cpp", CPP_SRC, build_cpp))
    assert d["Shape.area"].flags & model.F_ABSTRACT and d["Square.area"].flags & model.F_OVERRIDE
    assert not d["Shape.twice"].flags


def test_cpp_refs_inherit_scope_is_the_derived_class_and_arrow_this():
    f = extract("cpp", CPP_SRC, build_cpp)
    r = refs_of(f)
    assert ("Shape", "inherit", "Square") in r
    assert ("this.go", "call", "run") in r and ("ns.util", "call", "run") in r


def test_cpp_roots_extern_c_test_macro_and_main_filter():
    f = extract("cpp", CPP_SRC, build_cpp)
    got = {(h.kind, h.target) for h in f.roots}
    assert ("annotation:extern-c", "plugin_entry") in got and ("test", "TEST") in got
    assert not any(
        h.kind in ("test", "main") and h.target == "run" for h in f.roots
    )  # `run` is neither a macro test nor main


# ====================================================================== C#
CS_SRC = """public interface IRepo { void Save(); }
public class SqlRepo : IRepo
{
    public void Save() { }
    private void Unused() { }
    internal static int Counter;
    protected virtual void Hook() { }
    void Plain() { }
}
class Host : BackgroundService { }
public class Tests
{
    [Fact]
    public void Works() { }
    [Obsolete]
    public void Old() { }
    [HttpGet("/x")]
    public IActionResult Get() { return null; }
    public static void Main(string[] args) { services.AddScoped<IRepo, SqlRepo>(); services.Other<IRepo, Host>(); }
    public void Todo() { }
}
Console.WriteLine("top");
Console.WriteLine("second");
"""


def build_cs(s):
    iface = "public interface IRepo { void Save(); }"
    sql = CS_SRC[CS_SRC.index("public class SqlRepo") : CS_SRC.index("class Host")]
    host = "class Host : BackgroundService { }"
    tests = CS_SRC[CS_SRC.index("public class Tests") : CS_SRC.index('Console.WriteLine("top")')]
    s.add("defs", {"def.iface": iface, "name": "IRepo", "mod": "public"})
    s.add("defs", {"def.func": "void Save();", "name": "Save"})
    s.add("defs", {"def.class": sql.rstrip(), "name": "SqlRepo", "mod": "public"})
    s.add("defs", {"def.func": "public void Save() { }", "name": "Save", "mod": "public"})
    s.add("defs", {"def.func": "private void Unused() { }", "name": "Unused", "mod": "private"})
    s.add("defs", {"def.var": "internal static int Counter;", "name": "Counter", "mod": ["internal", "static"]})
    s.add("defs", {"def.func": "protected virtual void Hook() { }", "name": "Hook", "mod": ["protected", "virtual"]})
    s.add("defs", {"def.func": "void Plain() { }", "name": "Plain"})
    s.add("defs", {"def.class": host, "name": "Host"})
    s.add("defs", {"def.class": tests.rstrip(), "name": "Tests", "mod": "public"})
    s.add("defs", {"def.func": "[Fact]\n    public void Works() { }", "name": "Works", "mod": "public"})
    s.add("defs", {"def.func": "[Obsolete]\n    public void Old() { }", "name": "Old", "mod": "public"})
    s.add(
        "defs",
        {
            "def.func": '[HttpGet("/x")]\n    public IActionResult Get() { return null; }',
            "name": "Get",
            "mod": "public",
        },
    )
    main = "public static void Main(string[] args) { services.AddScoped<IRepo, SqlRepo>(); services.Other<IRepo, Host>(); }"
    s.add("defs", {"def.func": main, "name": "Main", "mod": ["public", "static"]})
    s.add("defs", {"def.func": "public void Todo() { }", "name": "Todo", "mod": "public"})
    s.add("refs", {"ref.inherit": ("IRepo", 1), "name": "IRepo"})
    s.add("refs", {"ref.inherit": "BackgroundService", "name": "BackgroundService"})
    s.add(
        "refs", {"ref.di": "services.AddScoped<IRepo, SqlRepo>()", "cmd": "AddScoped", "name": ["IRepo", "SqlRepo"]}, 1
    )
    s.add("refs", {"ref.di": "services.Other<IRepo, Host>()", "cmd": "Other", "name": ["IRepo", "Host"]}, 1)
    s.add("refs", {"ref.call": 'Console.WriteLine("top")', "name": "Console.WriteLine"})
    s.add("refs", {"ref.call": "services.AddScoped<IRepo, SqlRepo>()", "name": "services.AddScoped"})
    s.add("roots", {"root.test": "Fact"})
    s.add("roots", {"root.test": "Obsolete"})
    s.add("roots", {"root.annotation.aspnet": "HttpGet"})
    s.add("roots", {"root.annotation.aspnet": "Obsolete"})
    s.add("roots", {"root.main": "Main"})
    s.add("roots", {"root.main": "Get"})
    s.add("roots", {"root.main-toplevel": 'Console.WriteLine("top");'})
    s.add("roots", {"root.main-toplevel": 'Console.WriteLine("second");'})
    s.add("roots", {"root.annotation.dotnet-host": "BackgroundService"})
    s.add("roots", {"root.annotation.dotnet-host": "IRepo"})
    s.add("stub", {"stub.def": "public void Todo() { }", "name": "Todo", "body": "{ }"})
    s.add("stub", {"stub.def": "public void Works() { }", "name": "Works", "body": "{ }"})


def cs():
    return extract("csharp", CS_SRC, build_cs)


def test_csharp_visibility_modifiers_defaults_and_interface_members():
    d = defs_of(cs())
    assert d["IRepo"].kind == "iface" and d["IRepo"].vis == "public"
    assert (
        d["IRepo.Save"].vis == "public" and d["IRepo.Save"].kind == "method"
    )  # an interface member is public without a modifier
    assert (
        d["SqlRepo.Save"].vis == "public"
        and d["SqlRepo.Unused"].vis == "private"
        and d["SqlRepo.Plain"].vis == "private"
    )
    assert (
        d["SqlRepo.Counter"].vis == "package" and d["SqlRepo.Hook"].vis == "public"
    )  # internal = package, protected = API of the library mode
    assert d["Host"].vis == "package"  # a type without a modifier is internal


def test_csharp_virtual_is_dispatch_and_stub_bodies():
    d = defs_of(cs())
    assert d["SqlRepo.Hook"].flags & model.F_ABSTRACT
    assert d["Tests.Todo"].stub and d["Tests.Todo"].flags & model.F_STUB


def test_csharp_refs_di_filter_inherit_scope_and_dotted_names():
    r = refs_of(cs())
    assert ("IRepo", "inherit", "SqlRepo") in r and ("BackgroundService", "inherit", "Host") in r
    assert ("IRepo", "di", "Tests.Main") in r and ("SqlRepo", "di", "Tests.Main") in r
    assert not any(
        k == "di" and n == "Host" for n, k, _ in r
    )  # services.Other<..> is not a registration (rules.json ref_cmd)
    assert ("Console.WriteLine", "call", "") in r


def test_csharp_roots_main_tests_aspnet_host_and_dedupe():
    f = cs()
    got = {(h.kind, h.target) for h in f.roots}
    assert ("main", "Tests.Main") in got and ("test", "Tests.Works") in got
    assert ("annotation:aspnet", "Tests.Get") in got
    assert ("annotation:dotnet-host", "Host") in got
    assert not any(h.target == "Tests.Old" for h in f.roots)  # [Obsolete] is neither a test nor a framework entry
    assert not any(
        h.kind == "annotation:dotnet-host" and h.target == "SqlRepo" for h in f.roots
    )  # IRepo is not a host base
    top = [h for h in f.roots if h.kind == "main" and h.target == ""]
    assert len(top) == 1  # two top-level statements are one program root (the first line)


def test_csharp_cha_interface_call_reaches_the_implementation_through_link():
    f = {"Svc.cs": cs()}
    res = dlink.link(f, workers=1)
    keys = {n.key.split("#", 1)[-1]: n.key for n in res.graph.nodes if "#" in n.key}
    # the interface method is abstract-like: an impl edge connects sub.m -> base.m and base.m -> sub.m when the base is an iface
    assert any(e.kind in ("override", "impl") for e in res.graph.edges if keys["SqlRepo.Save"] in (e.src, e.dst))


# ====================================================================== fix round 1: macro bodies, declaring identifiers, static members, C# uses
MACRO_SRC = """#define LOG(m) log_impl(m)
#define HANDLER on_event
static void log_impl(const char *m) { }
static void on_event(void) { }
static void unused_static(void) { }
int main(void) { LOG("x"); return 0; }
"""
PASTE_SRC = "#define PASTE(n) impl_##n\n"


def build_macro(s):
    s.add("defs", {"def.func": "#define LOG(m) log_impl(m)", "name": "LOG"})
    for fn in ("log_impl(const char *m) { }", "on_event(void) { }", "unused_static(void) { }"):
        s.add("defs", {"def.func": f"static void {fn}", "name": fn.split("(")[0], "mod": "static"})
    s.add("defs", {"def.func": 'int main(void) { LOG("x"); return 0; }', "name": "main"})
    s.add("refs", {"scan": "log_impl(m)"})  # the preproc_arg of LOG: one opaque token
    s.add("refs", {"scan": "on_event"})  # an object-like macro
    s.add("refs", {"ref.call": 'LOG("x")', "name": "LOG"})
    s.add("roots", {"root.main": "main"})


def test_c_macro_body_calls_are_uses_and_a_paste_is_a_dyn_site():
    r = refs_of(extract("c", MACRO_SRC, build_macro))
    assert ("log_impl", "call", "LOG") in r and (
        "m",
        "ref",
        "LOG",
    ) in r  # a macro parameter is a weak use too: over-alive, never dead
    assert ("on_event", "ref", "") in r
    def paste(s):
        return (
            s.add("defs", {"def.func": "#define PASTE(n) impl_##n", "name": "PASTE"}),
            s.add("refs", {"scan": "impl_##n"}),
        )  # noqa: E731
    f = extract("c", PASTE_SRC, paste)
    assert [(d.pattern, d.scope) for d in f.dyn] == [("impl_##n", "PASTE")] and ("impl_", "ref", "PASTE") in refs_of(f)


def test_c_static_function_called_only_from_a_macro_is_alive_through_link_and_reach():
    f = extract("c", MACRO_SRC, build_macro)
    v = {k.split(":", 1)[1]: x for k, x in rc.analyze(dlink.link({"a.c": f}, workers=1).graph).verdicts().items()}
    assert v["a.c#log_impl"] == "alive" and v["a.c#LOG"] == "alive" and v["a.c#on_event"] == "alive"
    assert v["a.c#unused_static"] == "dead"


def test_the_identifier_that_declares_a_definition_is_not_a_use_of_it():
    src = "struct Box { int a; };\nstruct Box make(void) { struct Box b; return b; }\n"

    def build(s):
        s.add("defs", {"def.class": "struct Box { int a; };", "name": "Box"})
        s.add("defs", {"def.func": "struct Box make(void) { struct Box b; return b; }", "name": "make"})
        s.add("refs", {"ref.ref": "struct Box { int a; };", "name": "Box"})  # the tag of its own definition
        s.add("refs", {"ref.ref": "struct Box b;", "name": "Box"})

    r = refs_of(extract("c", src, build))
    assert ("Box", "ref", "Box") not in r  # no self loop: a lone unused struct is dead, not a one-node cluster
    assert ("Box", "ref", "make") in r


def test_cpp_static_is_private_on_a_free_function_and_public_on_a_class_member():
    src = "static int helper() { return 1; }\nclass Foo { public: static Foo create() { return Foo(); } };\n"

    def build(s):
        s.add("defs", {"def.func": "static int helper() { return 1; }", "name": "helper", "mod": "static"})
        s.add("defs", {"def.class": "class Foo { public: static Foo create() { return Foo(); } };", "name": "Foo"})
        s.add(
            "defs", {"def.func": "static Foo create() { return Foo(); }", "name": "create"}
        )  # no @mod: defs.scm does not match it

    d = defs_of(extract("cpp", src, build))
    assert d["helper"].vis == "private" and d["Foo.create"].vis == "public"
    # library mode: the exported API of a header class stays alive, the file-local helper does not
    f = extract("cpp", src, build)
    short = {
        k.split(":", 1)[1]: x
        for k, x in rc.analyze(dlink.link({"a.hpp": f}, workers=1).graph, "library").verdicts().items()
    }
    assert short["a.hpp#Foo.create"] not in ("dead", "dead-cluster") and short["a.hpp#helper"] in (
        "dead",
        "dead-cluster",
    )


def test_cpp_defs_query_takes_static_only_from_a_file_or_namespace_function():
    text = (QDIR / "cpp" / "defs.scm").read_text()
    code = "\n".join(re.sub(r";.*$", "", ln) for ln in text.splitlines())
    mods = [ln.strip() for ln in code.splitlines() if "storage_class_specifier" in ln and "@mod" in ln]
    assert mods and all(
        ln.startswith(
            (
                "(translation_unit (function_definition",
                "(declaration_list (function_definition",
                "(translation_unit (declaration",
            )
        )
        for ln in mods
    ), mods
    assert (
        "(namespace_definition !name" in code and "@vis.private" in code
    )  # an anonymous namespace is internal linkage


def test_queries_of_the_fix_round_have_their_documented_shape():
    cs_refs = (QDIR / "csharp" / "refs.scm").read_text()
    assert re.search(r"^\(identifier\) @name @ref\.ref$", cs_refs, re.M) and re.search(
        r"^\(member_access_expression\) @name @ref\.ref$", cs_refs, re.M
    )
    for g in ("c", "cpp"):
        refs = (QDIR / g / "refs.scm").read_text()
        assert "(preproc_function_def (preproc_arg) @scan)" in refs and "(preproc_def (preproc_arg) @scan)" in refs, g


DTO = "class Dto { public int V; }"
UTIL = "class Util { public static int Parse(string s) { return 2; } public int OnClick() { return 3; } }"
GET = "public Dto Get() { Util u = null; var l = list.Select(Util.Parse); x.Click += u.OnClick; if (_flag) { lock (_sync) { _count++; } } return null; }"
SVC = f"class Svc {{ private bool _flag; private int _count; private object _sync; {GET} }}"
PROG = "class Prog { static void Main() { var svc = new Svc(); svc.Get(); } }"
CS_USES = "\n".join([DTO, UTIL, SVC, PROG]) + "\n"


def build_cs_uses(s):
    for cls, name in ((DTO, "Dto"), (UTIL, "Util"), (SVC, "Svc"), (PROG, "Prog")):
        s.add("defs", {"def.class": cls, "name": name})
    s.add("defs", {"def.var": "public int V;", "name": "V", "mod": "public"})
    s.add("defs", {"def.func": "public static int Parse(string s) { return 2; }", "name": "Parse", "mod": "public"})
    s.add("defs", {"def.func": "public int OnClick() { return 3; }", "name": "OnClick", "mod": "public"})
    for field in ("_flag", "_count", "_sync"):
        s.add(
            "defs",
            {
                "def.var": f"private {'bool' if field == '_flag' else 'int' if field == '_count' else 'object'} {field};",
                "name": field,
                "mod": "private",
            },
        )
    s.add("defs", {"def.func": GET, "name": "Get", "mod": "public"})
    s.add(
        "defs", {"def.func": "static void Main() { var svc = new Svc(); svc.Get(); }", "name": "Main", "mod": "static"}
    )
    # refs.scm: `(identifier) @name @ref.ref` (the return type, a receiver, a field read) and `(member_access_expression) @name @ref.ref`
    s.add("refs", {"ref.ref": "public Dto Get()", "name": "Dto"})
    s.add("refs", {"ref.ref": "Util u = null;", "name": "Util"})
    s.add("refs", {"ref.ref": "if (_flag)", "name": "_flag"})
    s.add("refs", {"ref.ref": "lock (_sync)", "name": "_sync"})
    s.add("refs", {"ref.ref": "_count++", "name": "_count"})
    s.add(
        "refs", {"ref.ref": "list.Select(Util.Parse)", "name": "Util.Parse"}
    )  # a method group through a member access
    s.add("refs", {"ref.ref": "x.Click += u.OnClick;", "name": "u.OnClick"})
    s.add("refs", {"ref.call": "svc.Get()", "name": "svc.Get"})
    s.add("refs", {"ref.call": "new Svc()", "name": "Svc"})
    s.add("roots", {"root.main": "Main"})


def test_csharp_method_groups_return_types_and_private_field_reads_keep_their_targets_alive():
    f = extract("csharp", CS_USES, build_cs_uses)
    r = refs_of(f)
    assert {
        ("Util.Parse", "ref", "Svc.Get"),
        ("u.OnClick", "ref", "Svc.Get"),
        ("Dto", "ref", "Svc.Get"),
        ("_flag", "ref", "Svc.Get"),
    } <= r
    v = {k.split(":", 1)[1]: x for k, x in rc.analyze(dlink.link({"A.cs": f}, workers=1).graph).verdicts().items()}
    for k in ("Util.Parse", "Util.OnClick", "Dto", "Svc._flag", "Svc._sync", "Svc._count"):
        assert v[f"A.cs#{k}"] not in ("dead", "dead-cluster"), (k, v)


# ====================================================================== the scripted-parser contract itself
def test_unknown_root_and_vis_capture_names_are_an_error_of_the_query():
    with pytest.raises(ValueError, match="not a root kind"):
        extract("c", C_SRC, lambda s: s.add("roots", {"root.bogus": "main"}))
    with pytest.raises(ValueError, match="not a visibility"):
        extract(
            "c",
            C_SRC,
            lambda s: s.add(
                "defs",
                {
                    "def.func": "int api(int x) { return helper(x); }",
                    "name": "api",
                    "vis.secret": "int api(int x) { return helper(x); }",
                },
            ),
        )


def test_a_query_that_does_not_compile_is_extractor_missing_never_an_empty_graph(monkeypatch):
    class P:
        errors = 0

        def run(self, kind):
            raise tg.QueryCompileError("csharp/defs.scm does not compile on the installed grammar: bad field")

    tg.set_backend(lambda g, d: P())
    try:
        with pytest.raises(contracts.ExtractorMissing, match="does not compile"):
            tg.TsGeneric().extract("csharp", "x.cs", "class A {}")
    finally:
        tg.set_backend(None)


def test_a_real_binding_query_error_becomes_a_query_problem():
    class Mod:
        @staticmethod
        def Query(lang, text):  # noqa: N802
            raise RuntimeError("Invalid node type foo")

    p = tg._TsParsed("c", Mod, object(), types.SimpleNamespace(has_error=False))
    with pytest.raises(tg.QueryCompileError, match="c/defs.scm does not compile"):
        p.run("defs")


# ====================================================================== queries and rules: vocabulary, balance, registration
VOCAB = {
    "defs": re.compile(
        r"@(def\.(?:%s)|name|mod|alias|aliased|vis\.(?:%s))\b" % ("|".join(model.NODE_KINDS), "|".join(model.VIS))
    ),
    "refs": re.compile(r"@(ref\.(?:%s)|name|member|cmd|dyn(?:\.[a-z]+)?|scan)\b" % "|".join(model.EDGE_KINDS)),
    "roots": re.compile(r"@(root\.marker|root\.(?:main|test|export|init)(?:-[a-z]+)?|root\.annotation\.[a-z0-9-]+)\b"),
    "stub": re.compile(r"@(stub\.def|name|body)\b"),
}


@pytest.mark.parametrize("g", ["c", "cpp", "csharp"])
def test_native_queries_use_the_documented_vocabulary_and_balanced_parens(g):
    rules = json.loads((QDIR / g / "rules.json").read_text())
    for kind in ("defs", "refs", "roots", "stub"):
        text = (QDIR / g / f"{kind}.scm").read_text()
        code = "\n".join(re.sub(r";.*$", "", ln) for ln in text.splitlines())
        code = re.sub(r'"(?:[^"\\]|\\.)*"', '""', code)
        assert code.count("(") == code.count(")") and code.count("[") == code.count("]"), f"{g}/{kind}.scm"
        caps = re.findall(r"@[\w.-]+", code)
        assert caps, f"{g}/{kind}.scm captures nothing"
        for c in caps:
            assert VOCAB[kind].fullmatch(c) or VOCAB[kind].match(
                c + " "
            ), f"{g}/{kind}.scm: capture {c} is outside the vocabulary"
        assert not re.search(r"#(eq|match|any-of|not-eq)\?", code), f"{g}/{kind}.scm: predicates are binding dependent"
        if kind == "roots":  # every filtered capture has its rule, every rule has a capture
            suffixes = {c[len("@root.") :] for c in caps if c != "@root.marker"}
            assert suffixes <= set(rules["roots"]) or g == "cpp" and suffixes <= set(rules["roots"]), (
                g,
                suffixes - set(rules["roots"]),
            )
            assert set(rules["roots"]) <= suffixes, (g, set(rules["roots"]) - suffixes)


@pytest.mark.parametrize("g", ["c", "cpp", "csharp"])
def test_native_rules_are_valid_data(g):
    rules = json.loads((QDIR / g / "rules.json").read_text())
    assert set(rules) <= {
        "_doc",
        "stub_tag",
        "vis_mods",
        "flag_mods",
        "default_vis",
        "parent_default_vis",
        "name_map",
        "dotted_names",
        "ref_cmd",
        "roots",
        "protocol",
        "dyn",
        "strip_angles",
    }
    assert set(rules.get("vis_mods", {}).values()) <= set(model.VIS)
    assert set(rules.get("default_vis", {}).values()) | set(rules.get("parent_default_vis", {}).values()) <= set(
        model.VIS
    )
    assert set(rules.get("flag_mods", {}).values()) <= set(model.FLAG_NAMES)
    for suffix, rule in rules["roots"].items():
        kind = "annotation:" + suffix[11:] if suffix.startswith("annotation.") else suffix.split("-")[0]
        assert kind in model.ROOT_KINDS or kind.startswith("annotation:")
        assert rule.get("strength", "hard") in model.STRENGTHS
        assert set(rule) <= {"re", "strength", "kinds", "flag"}
        assert set(rule.get("kinds", ())) <= set(model.NODE_KINDS) and rule.get("flag", "protocol") in model.FLAG_NAMES
        re.compile(rule.get("re", ""))
    for rx in rules.get("ref_cmd", {}).values():
        re.compile(rx)
    for rule in rules.get("protocol", ()):
        assert set(rule) <= {"re", "kinds", "vis", "parents", "skip_mods"} and rule
        assert set(rule.get("kinds", ())) | set(rule.get("parents", ())) <= set(model.NODE_KINDS)
        assert set(rule.get("vis", ())) <= set(model.VIS)
        re.compile(rule.get("re", ""))
    for kind, rule in rules.get("dyn", {}).items():
        assert set(rule) <= {"re", "cmd", "pick"} and re.compile(rule.get("re", "(.)")).groups == 1, kind
        re.compile(rule.get("cmd", ""))
    assert rules["stub_tag"] in ("c", "cs")


def test_native_languages_are_registered_by_the_langs_data():
    lg = dclangs.load()
    for lang, g in (("c", "c"), ("cpp", "cpp"), ("csharp", "csharp")):
        assert lg.langs[lang]["extractor"] == "ts_generic" and tg._queries(g) == [
            "defs",
            "refs",
            "roots",
            "stub",
            "xlang",
        ]
    assert lg.langs["powershell"]["extractor"] == "pwsh" and lg.grammar("powershell") is None
    assert contracts.get_extractor("powershell").name == "pwsh"
    assert contracts.get_extractor("csharp").name == "ts_generic"


def test_rules_are_part_of_the_grammar_sha(monkeypatch):
    base = tg.queries_sha("csharp")
    tg.queries_sha.cache_clear()
    tg.rules_of.cache_clear()
    real = tg.rules_of
    monkeypatch.setattr(tg, "rules_of", lambda g: {**real(g), "extra": 1})
    try:
        assert tg.queries_sha("csharp") != base
    finally:
        monkeypatch.undo()
        tg.queries_sha.cache_clear()
        tg.rules_of.cache_clear()


def test_a_missing_grammar_of_a_native_language_is_loud_with_the_install_command(no_tree_sitter):
    tg.set_backend(None)
    with pytest.raises(contracts.ExtractorMissing):
        contracts.get_extractor("csharp").extract("csharp", "a.cs", "class A {}")
    f = tg.missing_finding("csharp", "a.cs")
    assert f is not None and f.code == "dc.degraded-extractor" and "csharp" in f.msg and "[treesitter]" in f.msg


def test_ci_style_test_name_patterns_cover_the_native_test_files():
    from dead_code_checker.graph import roots

    names = roots.fw()["test_names"]
    for n in ("Used.Tests.ps1", "ServiceTests.cs", "a_test.cpp", "a_test.c", "UnitTest.cs"):
        assert any(re.search(p, n) for p in names), n


# ====================================================================== real grammars: golden fixtures (skipped until installed)
def _tree(lang_dir, exts):
    base = FIX / lang_dir
    return {p.name: p.read_text() for p in sorted(base.iterdir()) if p.suffix in exts}


def _golden(lang, g, lang_dir, exts, hints=None):
    tg.set_backend(None)
    exp = json.loads((FIX / lang_dir / "expected.json").read_text())
    ex = contracts.get_extractor(lang)
    files = {}
    for name, text in _tree(lang_dir, exts).items():
        eff = lang if lang != "cpp" or name.endswith((".cpp", ".hpp")) else lang
        files[name] = ex.extract(eff, name, text)
    res = dlink.link(files, workers=1)
    assert not res.findings
    verdict = rc.analyze(res.graph).verdicts()
    short = {k.split(":", 1)[1]: v for k, v in verdict.items()}
    return exp, files, short


@needs_grammar("c")
def test_c_golden_fixture_with_the_real_grammar():
    exp, files, short = _golden("c", "c", "c", {".c", ".h"})
    roots = {(h.kind, h.target) for f in files.values() for h in f.roots}
    assert {tuple(r) for r in exp["roots"]} <= roots
    for k, vis in exp["vis"].items():
        path, qual = k.split("#")
        assert defs_of(files[path])[qual].vis == vis, k
    assert {k for k, v in short.items() if v in ("dead", "dead-cluster") and "#" in k} == set(exp["dead"])


@needs_grammar("cpp")
def test_cpp_golden_fixture_with_the_real_grammar():
    exp, files, short = _golden("cpp", "cpp", "cpp", {".cpp", ".hpp"})
    roots = {(h.kind, h.target) for f in files.values() for h in f.roots}
    assert {tuple(r) for r in exp["roots"]} <= roots
    assert {k for k, v in short.items() if v in ("dead", "dead-cluster") and "#" in k} == set(exp["dead"])


@needs_grammar("csharp")
def test_csharp_golden_fixture_with_the_real_grammar():
    exp, files, short = _golden("csharp", "csharp", "csharp", {".cs"})
    roots = {(h.kind, h.target) for f in files.values() for h in f.roots}
    assert {tuple(r) for r in exp["roots"]} <= roots
    for k, vis in exp["vis"].items():
        path, qual = k.split("#")
        assert defs_of(files[path])[qual].vis == vis, k
    assert {k for k, v in short.items() if v in ("dead", "dead-cluster") and "#" in k} == set(exp["dead"])
    assert set(exp["test_only"]) <= {k for k, v in short.items() if v == "test-only"}
    assert {short[k] for k in exp["alive"]} <= {"alive", "weak-alive"}


# ====================================================================== PowerShell: the facts mapping through the worker seam (no pwsh)
@pytest.fixture
def worker():
    box = {}
    pw.set_worker(lambda text: box["reply"])
    yield box
    pw.set_worker(None)


def ps(box, rec, errors=0, text="x\n" * 20):
    box["reply"] = {"errors": errors, "rec": rec}
    return contracts.get_extractor("powershell").extract("powershell", "a.ps1", text)


def test_pwsh_names_are_case_folded_and_scope_prefixes_dropped(worker):
    f = ps(
        worker,
        [
            ["func", "Get-Foo", 1, 1, 3, 2, False, 1],
            ["func", "script:Hidden", 4, 1, 4, 20, False, 1],
            ["cmd", "get-FOO", 5, 1, 5, 9, "call", [[]]],
        ],
    )
    assert {"get-foo", "hidden"} <= set(defs_of(f)) and ("get-foo", "call", "") in refs_of(f)


def test_pwsh_empty_function_is_a_stub_and_methods_nest_in_classes(worker):
    f = ps(
        worker,
        [
            ["func", "Todo", 1, 1, 1, 12, False, 0],
            ["class", "A", 3, 1, 9, 2, ["B"], "class"],
            ["method", "M", 4, 1, 5, 2, False, 0, False],
            ["method", "A", 6, 1, 6, 6, True, 0, False],
            ["prop", "p", 7, 1, 7, 8],
            ["class", "IFoo", 10, 1, 11, 2, [], "interface"],
            ["member", "go", 4, 3, 4, 9, False, "$this", True],
        ],
    )
    d = defs_of(f)
    assert d["todo"].stub == "empty body" and d["todo"].flags & model.F_STUB
    assert d["a.m"].stub == "empty body"  # an empty method is a stub, an empty constructor is not
    assert d["a.a"].stub == "" and d["a.p"].kind == "var" and d["ifoo"].kind == "iface"
    r = refs_of(f)
    assert ("b", "inherit", "a") in r and ("this.go", "call", "a.m") in r


def test_pwsh_export_module_member_roots_only_names_defined_here(worker):
    f = ps(
        worker,
        [
            ["func", "A", 1, 1, 1, 10, False, 1],
            ["func", "B", 2, 1, 2, 10, False, 1],
            ["cmd", "Export-ModuleMember", 3, 1, 3, 40, "call", [["-Function", "A", "Elsewhere"]]],
        ],
    )
    assert [(h.kind, h.target, h.strength) for h in f.roots] == [("export", "a", "soft")]
    assert defs_of(f)["a"].vis == "exported" and defs_of(f)["b"].vis == "public"
    assert ("elsewhere", "ref", "") in refs_of(
        f
    )  # a name defined in another file is a ref, never a root: no false dc.root-dangling
    assert not any(r.name == "export-modulemember" for r in f.refs)


def test_pwsh_export_wildcard_and_other_switches(worker):
    f = ps(
        worker,
        [
            ["func", "A", 1, 1, 1, 10, False, 1],
            ["func", "B", 2, 1, 2, 10, False, 1],
            ["cmd", "Export-ModuleMember", 3, 1, 3, 40, "call", [["-Function", "*", "-Alias", "al"]]],
        ],
    )
    assert sorted(h.target for h in f.roots) == ["a", "b"]  # `-Alias al` names an alias, not a function


def test_pwsh_includes_exec_and_computed_commands(worker):
    f = ps(
        worker,
        [
            ["cmd", "$PSScriptRoot\\lib\\x.ps1", 1, 1, 1, 30, "dot", [[]]],
            ["cmd", "./tool.ps1", 2, 1, 2, 12, "amp", [[]]],
            ["cmd", "Import-Module", 3, 1, 3, 30, "call", [["./mod/M.psm1", "-Force"]]],
            ["using", "./u.psm1", 4, 1, 4, 20, "Module"],
            ["using", "System.IO", 5, 1, 5, 20, "Namespace"],
            ["dyn", "$handler", 6, 1, 6, 10],
            ["cmd", "Register-X", 7, 1, 7, 30, "call", [["-CommandName", "Get-Thing", "plain"]]],
        ],
    )
    r = refs_of(f)
    assert {
        ("lib/x.ps1", "include", ""),
        ("tool.ps1", "exec", ""),
        ("mod/M.psm1", "include", ""),
        ("u.psm1", "include", ""),
    } <= r
    assert {
        ("get-thing", "ref", ""),
        ("plain", "ref", ""),
    } <= r  # a plain name may be a function or a class too (New-Object Bar)
    assert not any(
        n.startswith("-") or n == "./mod/m.psm1" for n, _, _ in r
    )  # a parameter name and a path are no names
    assert not any(n == "system.io" for n, _, _ in r)
    assert [d.pattern for d in f.dyn] == ["$handler"]


def test_pwsh_static_members_types_new_and_builtin_types(worker):
    f = ps(
        worker,
        [
            ["member", "Run", 1, 1, 1, 12, True, "Util", True],
            ["member", "new", 2, 1, 2, 12, True, "Util", True],
            ["member", "Count", 3, 1, 3, 12, False, "$list", False],
            ["member", "X", 4, 1, 4, 12, False, "", False],
            ["type", "Util", 5, 1, 5, 6],
            ["type", "int", 6, 1, 6, 5],
        ],
    )
    r = refs_of(f)
    assert {
        ("util.run", "call", ""),
        ("util.new", "call", ""),
        ("util", "call", ""),
        ("list.count", "ref", ""),
        ("?.x", "ref", ""),
        ("util", "ref", ""),
    } <= r
    assert not any(n == "int" for n, _, _ in r)


def test_pwsh_computed_command_words_are_dyn_sites_with_the_literal_they_carry(worker):
    f = ps(
        worker,
        [
            [
                "dyn",
                "(Join-Path $PSScriptRoot 'x.ps1')",
                1,
                1,
                1,
                39,
                "dot",
                "shape",
                [[["x.ps1"]]],
            ],  # worker output of a real parse
            ["dyn", '"Get-$n"', 2, 1, 2, 11, "amp", "str", [[["Get-$n"]]]],
            ["dyn", "(\"Get-\" + 'Zed')", 3, 1, 3, 19, "amp", "expr", [[["Get-", "Zed"]]]],
            ["dyn", '"$root\\tool.ps1"', 4, 1, 4, 19, "amp", "str", [[["$root\\tool.ps1"]]]],
            ["dyn", "$x", 5, 1, 5, 5, "amp", "var", [[[]]]],
            ["dyn", "$o.$m()", 6, 1, 6, 8],  # a computed member has no extras
            ["fvar", "Foo-Bar", 7, 6, 7, 25],
        ],
    )
    r = refs_of(f)
    assert ("x.ps1", "include", "") in r and ("tool.ps1", "exec", "") in r and ("foo-bar", "ref", "") in r
    assert sorted(d.pattern for d in f.dyn) == sorted(
        ["(Join-Path $PSScriptRoot 'x.ps1')", "name:get-*", "name:*get-*zed*", '"$root\\tool.ps1"', "$x", "$o.$m()"]
    )
    assert not any(n.startswith("(") or "$" in n for n, _, _ in r)  # no junk ref named after the expression text


def test_pwsh_dyn_name_pattern_is_a_glob_of_lowercase_names_with_escaped_brackets():
    assert pw._dyn('"Get-$n"', "amp", "str", [["Get-$n"]]) == ("name:get-*", [])
    assert pw._dyn('"A[1]-${x}-$(f)"', "amp", "str", [["A[1]-${x}-$(f)"]])[0] == "name:a[[]1[]]-*-*"
    assert (
        pw._dyn("(Get-Command Foo)", "amp", "expr", [[]])[0] == "(Get-Command Foo)"
    )  # no literal: the text, every function of the file


def test_pwsh_manifest_hashtable_keys(worker):
    f = ps(
        worker,
        [
            ["hash", "FunctionsToExport", 2, 1, 2, 40, [["Get-A", "b"]]],
            ["hash", "RootModule", 3, 1, 3, 30, [["M.psm1"]]],
            ["hash", "Author", 4, 1, 4, 30, [["me"]]],
        ],
    )
    r = refs_of(f)
    assert {("get-a", "ref", ""), ("b", "ref", ""), ("M.psm1", "include", "")} <= r and not any(
        n == "me" for n, _, _ in r
    )


def test_pwsh_manual_root_marker_and_error_count(worker):
    f = ps(
        worker,
        [["func", "Entry", 2, 1, 2, 20, False, 1]],
        errors=3,
        text="# deadcode: root entry\nfunction Entry { 1 }\n",
    )
    assert [(h.kind, h.target) for h in f.roots] == [("manual", "entry")] and f.errors == 3


def test_pwsh_facts_do_not_depend_on_the_path_and_survive_the_shard_roundtrip(worker):
    rec = [["func", "A", 1, 1, 2, 2, False, 1], ["cmd", "B", 1, 3, 1, 5, "call", [[]]]]
    worker["reply"] = {"errors": 0, "rec": rec}
    ex = contracts.get_extractor("powershell")
    a, b = ex.extract("powershell", "x/a.ps1", "t"), ex.extract("powershell", "y/b.ps1", "t")
    assert a == b
    sh = contracts.make_shard(ex, "powershell", "x/a.ps1", "blob", "t")
    assert model.Facts.from_payload(sh.payload) == a if hasattr(model.Facts, "from_payload") else sh.payload


def test_pwsh_shard_key_follows_the_worker_script(monkeypatch):
    ex = contracts.get_extractor("powershell")
    from dead_code_checker.graph.extract import xlang  # the env rules decide the facts as much as the worker does

    assert ex.grammar_sha("powershell") == hashlib.sha256(f"{pw.script_sha()}|{xlang.sha()}".encode()).hexdigest()
    assert len(pw.script_sha()) == 64


# ====================================================================== PowerShell: the engine and its absence
class FakeRes(NamedTuple):
    path: object
    status: str
    detail: str


def test_pwsh_missing_is_extractor_missing_and_a_warning_with_the_install_hint():
    pw._local.__dict__.pop("w", None)  # a worker that an earlier test started would answer instead
    pw.set_finder(lambda: FakeRes(None, "missing", "pwsh is not on PATH"))
    pw.set_worker(None)
    try:
        with pytest.raises(contracts.ExtractorMissing, match="pwsh is not on PATH"):
            contracts.get_extractor("powershell").extract("powershell", "a.ps1", "function A {}")
        f = pw.missing_finding("powershell", "a.ps1")
        assert f is not None and f.code == "dc.degraded-extractor" and f.level == "W" and "pwsh" in f.msg
        assert "install PowerShell 7" in f.msg and "DEAD_CODE_CHECKER_PWSH" in f.msg
        assert contracts.get_extractor("powershell").probe("powershell").code == "dc.degraded-extractor"
    finally:
        pw.set_finder(None)


def test_pwsh_missing_finding_is_none_when_pwsh_is_usable():
    pw.set_finder(lambda: FakeRes("/usr/bin/pwsh", "ok", ""))
    try:
        assert pw.missing_finding("powershell", "a.ps1") is None and contracts.get_extractor("powershell").probe("powershell") is None
    finally:
        pw.set_finder(None)


def test_pwsh_a_worker_that_dies_is_a_parse_error_count_not_a_crash(tmp_path):
    dead = tmp_path / "pwsh"
    dead.write_text("#!/bin/sh\nexit 0\n")
    dead.chmod(0o755)
    pw.set_finder(lambda: FakeRes(str(dead), "ok", ""))
    pw.set_worker(None)
    pw._local.__dict__.pop("w", None)
    try:
        f = contracts.get_extractor("powershell").extract("powershell", "a.ps1", "function A {}")
        assert f.errors == 1 and [d.qual for d in f.defs] == [""]
    finally:
        pw.set_finder(None)
        pw._local.__dict__.pop("w", None)


# ====================================================================== PowerShell with the real pwsh parser
PS_SRC = """using module ./m.psm1
. $PSScriptRoot\\lib\\common.ps1
function Get-Foo { param($x) Bar-Baz $x; & $handler; [Util]::Run(1); $this.Go() }
class A : B, IC { [int]$p; A() {} [void] M() { $this.N(); [A]::S() } static [void] S() {} }
Export-ModuleMember -Function Get-Foo, 'Other'
$h = @{ FunctionsToExport = @('a','b') }
"""


@needs_pwsh
def test_pwsh_real_parse_defs_refs_roots():
    pw.set_worker(None)
    f = contracts.get_extractor("powershell").extract("powershell", "a.ps1", PS_SRC)
    d = defs_of(f)
    assert (
        {"get-foo", "a", "a.a", "a.m", "a.p", "a.s"} <= set(d)
        and d["a.p"].kind == "var"
        and d["a.s"].stub == "empty body"
    )
    assert d["get-foo"].vis == "exported"
    r = refs_of(f)
    assert {
        ("bar-baz", "call", "get-foo"),
        ("util.run", "call", "get-foo"),
        ("this.go", "call", "get-foo"),
        ("this.n", "call", "a.m"),
        ("a.s", "call", "a.m"),
        ("b", "inherit", "a"),
        ("ic", "inherit", "a"),
        ("lib/common.ps1", "include", ""),
        ("m.psm1", "include", ""),
        ("other", "ref", ""),
    } <= r
    assert [d_.pattern for d_ in f.dyn] == ["$handler"]
    assert [(h.kind, h.target) for h in f.roots] == [("export", "get-foo")] and f.errors == 0


@needs_pwsh
def test_pwsh_real_computed_command_words_and_function_drive():
    src = "\n".join(
        [
            "function Get-Dyn1 { 1 }",
            "function Run { & \"Get-$n\"; & (\"Get-\" + 'Zed'); . (Join-Path $PSScriptRoot 'lib/init.ps1'); & $x; $f = ${function:Get-Dyn1} }",
            ". $PSScriptRoot\\y.ps1",
            '. "$PSScriptRoot/z.ps1"',
            '. "$base/w.ps1"',
        ]
    )
    f = contracts.get_extractor("powershell").extract("powershell", "a.ps1", src)
    r = refs_of(f)
    assert {
        ("y.ps1", "include", ""),
        ("z.ps1", "include", ""),
        ("lib/init.ps1", "include", "run"),
        ("w.ps1", "include", ""),
        ("get-dyn1", "ref", "run"),
    } <= r
    assert not any(n.startswith("(") or n.startswith("get-$") for n, _, _ in r) and ("join-path", "call", "run") in r
    assert {(d.pattern, d.scope) for d in f.dyn} == {
        ("name:get-*", "run"),
        ("name:*get-*zed*", "run"),
        ("$x", "run"),
        ("(Join-Path $PSScriptRoot 'lib/init.ps1')", "run"),
        ('"$base/w.ps1"', ""),
    }


@needs_pwsh
def test_pwsh_real_parse_syntax_error_is_counted_and_facts_are_kept():
    f = contracts.get_extractor("powershell").extract("powershell", "a.ps1", "function Ok { 1 }\nfunction Bad { \n")
    assert f.errors >= 1 and "ok" in defs_of(f)


@needs_pwsh
def test_pwsh_real_worker_serves_many_files_and_is_deterministic():
    ex = contracts.get_extractor("powershell")
    srcs = [f"function F{i} {{ G{i} }}\nfunction G{i} {{ 1 }}\n" for i in range(5)]
    first = [ex.extract("powershell", f"{i}.ps1", s) for i, s in enumerate(srcs)]
    again = [ex.extract("powershell", f"{i}.ps1", s) for i, s in enumerate(srcs)]
    assert first == again and all(f"f{i}" in defs_of(first[i]) for i in range(5))
    assert len(pw._local.w.proc.args) == 5 and pw._local.w.alive  # one worker process, not one per file


@needs_pwsh
def test_pwsh_real_unicode_and_quotes_survive_the_json_pipe():
    f = contracts.get_extractor("powershell").extract("powershell", "u.ps1", 'function Get-Ünï { "a`"b ✓" }\nGet-Ünï\n')
    assert "get-ünï" in defs_of(f) and ("get-ünï", "call", "") in refs_of(f)


@needs_pwsh
def test_pwsh_real_parse_never_runs_the_code(tmp_path):
    marker = tmp_path / "ran"
    src = f"New-Item -ItemType File -Path '{marker}'\n[System.IO.File]::WriteAllText('{marker}', 'x')\n"
    contracts.get_extractor("powershell").extract("powershell", "evil.ps1", src)
    assert not marker.exists()


@needs_pwsh
def test_pwsh_real_golden_fixture_dead_test_only_clusters_stubs_exports():
    base = FIX / "pwsh"
    exp = json.loads((base / "expected.json").read_text())
    ex = contracts.get_extractor("powershell")
    files = {}
    for p in sorted(base.rglob("*")):
        if p.suffix in (".ps1", ".psm1"):
            rel = p.relative_to(base).as_posix()
            files[rel] = ex.extract("powershell", rel, p.read_text())
    hints = {"tests/Used.Tests.ps1": [model.RootHint("test", "file:tests/Used.Tests.ps1", 1, "soft", "convention")]}
    res = dlink.link(files, hints, workers=1)
    assert not res.findings
    out = rc.analyze(res.graph)
    v = {k.split(":", 1)[1]: x for k, x in out.verdicts().items()}
    assert {k for k, x in v.items() if x in ("dead", "dead-cluster")} == set(exp["dead"])
    assert {k for k, x in v.items() if x == "test-only"} == set(exp["test_only"])
    assert [sorted(k.split(":", 1)[1] for k in c) for c in out.clusters] == [sorted(c) for c in exp["cluster"]]
    nodes = {n.key.split(":", 1)[1]: n for n in res.graph.nodes}
    assert [k for k in exp["stubs"] if not nodes[k].flags & model.F_STUB] == []
    assert [k for k in exp["exported"] if nodes[k].vis != "exported"] == []
    assert (v["run.ps1"], v["run.ps1#main"]) == ("alive", "alive")
    assert [k for k in exp["alive"] if v[k] in ("dead", "dead-cluster")] == []  # computed forms are not dead code


@needs_pwsh
def test_pwsh_real_golden_is_identical_for_1_2_8_workers():
    base = FIX / "pwsh"
    ex = contracts.get_extractor("powershell")
    files = {
        p.relative_to(base).as_posix(): ex.extract("powershell", p.name, p.read_text())
        for p in sorted(base.rglob("*"))
        if p.suffix in (".ps1", ".psm1")
    }
    digests = {dlink.link(files, workers=w).graph.digest() for w in (1, 2, 8)}
    assert len(digests) == 1


# ====================================================================== fix round 2: constructors, plain-name arguments, identifiers in any position
def verdicts(files, hints=None):
    out = rc.analyze(dlink.link(files, hints, workers=1).graph)
    return {k.split(":", 1)[1]: v for k, v in out.verdicts().items()}


def dead(v):
    return {k for k, x in v.items() if x in ("dead", "dead-cluster")}


def test_is_ctor_names_a_member_like_its_class_and_never_a_python_method():
    assert all(dlink.is_ctor(q) for q in ("Foo.Foo", "Foo.~Foo", "ns.Foo.Foo", "a.B.~B"))
    assert not any(dlink.is_ctor(q) for q in ("Foo", "Foo.bar", "Foo.Bar", "Foo.~Bar", "a.Foo.bar", "~Foo"))
    f = contracts.get_extractor("python").extract(
        "python", "a.py", "class Foo:\n    def Foo(self):\n        return 1\n"
    )
    node = next(n for n in dlink.link({"a.py": f}, workers=1).graph.nodes if n.key.endswith("#Foo.Foo"))
    assert not node.flags & model.F_PROTOCOL


HPP = "#pragma once\nclass Foo { public: Foo(); ~Foo(); void run(); };\nclass Bar { public: Bar() {} ~Bar() {} };\n"
CPP = '#include "foo.hpp"\nFoo::Foo() {}\nFoo::~Foo() {}\nvoid Foo::run() {}\n'
MAIN = '#include "foo.hpp"\nint main() { Foo f; Bar *b = new Bar(); return 0; }\n'


def cpp_ctor_files():
    def hpp(s):
        s.add("defs", {"def.class": "class Foo { public: Foo(); ~Foo(); void run(); };", "name": "Foo"})
        s.add("defs", {"def.func": "Foo();", "name": "Foo"})
        s.add("defs", {"def.func": "~Foo();", "name": "~Foo"})
        s.add("defs", {"def.func": "void run();", "name": "run"})
        s.add("defs", {"def.class": "class Bar { public: Bar() {} ~Bar() {} };", "name": "Bar"})
        s.add("defs", {"def.func": "Bar() {}", "name": "Bar"})
        s.add("defs", {"def.func": "~Bar() {}", "name": "~Bar"})

    def cpp(s):
        s.add("defs", {"def.func": "Foo::Foo() {}", "name": "Foo::Foo"})
        s.add("defs", {"def.func": "Foo::~Foo() {}", "name": "Foo::~Foo"})
        s.add("defs", {"def.func": "void Foo::run() {}", "name": "Foo::run"})
        s.add("refs", {"ref.include": '#include "foo.hpp"', "name": '"foo.hpp"'})

    def main(s):
        s.add("defs", {"def.func": "int main() { Foo f; Bar *b = new Bar(); return 0; }", "name": "main"})
        s.add("refs", {"ref.include": '#include "foo.hpp"', "name": '"foo.hpp"'})
        s.add("refs", {"ref.ref": "Foo f;", "name": "Foo"})
        s.add("refs", {"ref.call": "new Bar()", "name": "Bar"})
        s.add("roots", {"root.main": "main"})

    return {
        "foo.hpp": extract("cpp", HPP, hpp),
        "foo.cpp": extract("cpp", CPP, cpp),
        "main.cpp": extract("cpp", MAIN, main),
    }


def test_cpp_constructors_and_destructors_are_alive_with_a_used_class_inline_or_out_of_class():
    files = cpp_ctor_files()
    v = verdicts(files)
    for k in (
        "foo.hpp#Foo.Foo",
        "foo.hpp#Foo.~Foo",
        "foo.cpp#Foo.Foo",
        "foo.cpp#Foo.~Foo",
        "foo.hpp#Bar.Bar",
        "foo.hpp#Bar.~Bar",
    ):
        assert v[k] not in ("dead", "dead-cluster"), (k, v[k])
    assert v["foo.hpp#Foo"] != "dead" and v["foo.cpp#Foo.run"] in (
        "dead",
        "dead-cluster",
    )  # an ordinary method nobody calls stays dead


def test_cpp_constructor_of_an_unused_class_is_not_alive_by_itself():
    def hpp(s):
        s.add("defs", {"def.class": "class Foo { public: Foo(); };", "name": "Foo"})
        s.add("defs", {"def.func": "Foo();", "name": "Foo"})

    def cpp(s):
        s.add("defs", {"def.func": "Foo::Foo() {}", "name": "Foo::Foo"})
        s.add("refs", {"ref.include": '#include "foo.hpp"', "name": '"foo.hpp"'})

    v = verdicts(
        {
            "foo.hpp": extract("cpp", "class Foo { public: Foo(); };\n", hpp),
            "foo.cpp": extract("cpp", CPP.split("\n")[0] + "\nFoo::Foo() {}\n", cpp),
        }
    )
    assert v["foo.hpp#Foo"] in ("dead", "dead-cluster") and v["foo.cpp#Foo.Foo"] in ("dead", "dead-cluster")


def test_cpp_constructor_test_is_sensitive_to_the_rule(monkeypatch):
    monkeypatch.setattr(dlink, "is_ctor", lambda q: False)
    v = verdicts(cpp_ctor_files())
    assert v["foo.hpp#Bar.Bar"] in ("dead", "dead-cluster")  # the control: without the rule the finding reproduces


CS_CTOR = "class Service { public Service() { } ~Service() { } public void Run() { } }\nclass Prog { static void Main() { var s = new Service(); s.Run(); } }\n"


def build_cs_ctor(s):
    s.add(
        "defs",
        {"def.class": "class Service { public Service() { } ~Service() { } public void Run() { } }", "name": "Service"},
    )
    s.add("defs", {"def.func": "public Service() { }", "name": "Service", "mod": "public"})
    s.add("defs", {"def.func": "public void Run() { }", "name": "Run", "mod": "public"})
    s.add(
        "defs", {"def.class": "class Prog { static void Main() { var s = new Service(); s.Run(); } }", "name": "Prog"}
    )
    s.add(
        "defs", {"def.func": "static void Main() { var s = new Service(); s.Run(); }", "name": "Main", "mod": "static"}
    )
    s.add("refs", {"ref.call": "new Service()", "name": "Service"})
    s.add("refs", {"ref.call": "s.Run()", "name": "s.Run"})
    s.add("roots", {"root.main": "Main"})


def test_csharp_constructor_is_alive_with_its_class():
    f = extract("csharp", CS_CTOR, build_cs_ctor)
    assert "Service.Service" in defs_of(f)
    v = verdicts({"A.cs": f})
    assert v["A.cs#Service.Service"] not in ("dead", "dead-cluster") and v["A.cs#Service"] == "alive"


C_CAST = """static int cmp(const void *a, const void *b) { return 0; }
static void handler(int sig) { }
static void parens(void) { }
static void unused(void) { }
int main(void) { qsort(arr, n, 4, (int(*)(const void*, const void*))cmp); signal(2, (void*)handler); (parens)(); return 0; }
"""


def test_c_function_used_as_a_value_inside_a_cast_or_parentheses_is_alive():
    def build(s):
        for fn in ("cmp", "handler", "parens", "unused"):
            s.add("defs", {"def.func": f"static {'int' if fn == 'cmp' else 'void'} {fn}", "name": fn, "mod": "static"})
        s.add("defs", {"def.func": "int main(void) {", "name": "main"})
        # refs.scm: every identifier, so the value of a cast_expression and of a parenthesized_expression are uses
        s.add("refs", {"ref.ref": "(int(*)(const void*, const void*))cmp", "name": "cmp"})
        s.add("refs", {"ref.ref": "(void*)handler", "name": "handler"})
        s.add("refs", {"ref.ref": "(parens)", "name": "parens"})
        s.add("roots", {"root.main": "main"})

    f = extract("c", C_CAST, build)
    v = verdicts({"a.c": f})
    assert {k for k in dead(v) if "#" in k} == {"a.c#unused"}


@pytest.mark.parametrize("g", ["c", "cpp"])
def test_c_and_cpp_refs_query_takes_every_identifier_not_a_list_of_positions(g):
    refs = (QDIR / g / "refs.scm").read_text()
    assert re.search(r"^\(identifier\) @name @ref\.ref$", refs, re.M)
    for pos in (
        "argument_list",
        "initializer_list",
        "assignment_expression",
        "return_statement",
        "binary_expression",
        "pointer_expression",
    ):
        assert f"({pos} " not in refs, (g, pos)


def test_an_identifier_inside_the_name_of_a_definition_is_not_a_use():
    src = "double Shape::twice() const { return 2; }\nvoid run() { fp = twice; }\n"

    def build(s):
        s.add("defs", {"def.func": "double Shape::twice() const { return 2; }", "name": "Shape::twice"})
        s.add("defs", {"def.func": "void run() { fp = twice; }", "name": "run"})
        s.add(
            "refs", {"ref.ref": "Shape::twice", "name": "Shape"}
        )  # the scope of a qualified name: inside the declared name
        s.add("refs", {"ref.ref": "Shape::twice() const", "name": "twice"})  # its last identifier
        s.add("refs", {"ref.ref": "fp = twice", "name": "twice"})

    r = refs_of(extract("cpp", src, build))
    assert r == {("twice", "ref", "run")}


# ---- PowerShell: plain-name arguments (worker seam) and real parses
def test_pwsh_plain_name_arguments_of_any_command_are_weak_refs(worker):
    f = ps(
        worker,
        [
            ["cmd", "New-Object", 1, 1, 1, 20, "call", [["Bar"]]],
            ["cmd", "New-Object", 2, 1, 2, 30, "call", [["-TypeName", "Baz", "-ArgumentList", "1"]]],
            ["cmd", "Get-Command", 3, 1, 3, 20, "call", [["helper"]]],
            ["cmd", "Set-Alias", 4, 1, 4, 20, "call", [["al", "Helper2"]]],
            ["cmd", "Write-Output", 5, 1, 5, 20, "call", [["a b", "x.y", "-Name"]]],
        ],
    )
    r = refs_of(f, "ref")
    assert {"bar", "baz", "helper", "al", "helper2"} == {n for n, _, _ in r}


PS_CTOR = """class Foo {
    Foo() { Write-Output 1 }
    [void] Run() { Write-Output 2 }
}
class Bar { Bar() { Write-Output 3 } }
class Baz { Baz() { Write-Output 4 } }
class Unused { Unused() { Write-Output 5 } }
function helper { Write-Output 6 }
function helper2 { Write-Output 7 }
function lonely { Write-Output 8 }
$a = [Foo]::new()
$b = New-Object Bar
$c = New-Object -TypeName Baz -ArgumentList 1
$d = Get-Command helper
Set-Alias al helper2
"""


@needs_pwsh
def test_pwsh_real_constructors_new_object_get_command_and_set_alias_keep_their_targets_alive():
    pw.set_worker(None)
    f = contracts.get_extractor("powershell").extract("powershell", "run.ps1", PS_CTOR)
    hints = {"run.ps1": [model.RootHint("main", "file:run.ps1", 1, "hard", "convention")]}
    v = verdicts({"run.ps1": f}, hints)
    for k in ("foo", "foo.foo", "bar", "bar.bar", "baz", "baz.baz", "helper", "helper2"):
        assert v[f"run.ps1#{k}"] not in ("dead", "dead-cluster"), (k, v)
    assert {k for k in dead(v) if "#" in k} >= {"run.ps1#lonely", "run.ps1#unused", "run.ps1#foo.run"}


# ====================================================================== second pass 2: owner edge, data members, convention members, string uses
def _v(f, path="A.cs"):
    return verdicts({path: f})


def _vh(f, path="a.ps1"):  # a script that is run: its file is a root
    return verdicts({path: f}, {path: [model.RootHint("main", path, 1)]})


PROGRAM = "public static class Program { public static void Main() { } }\n"


def build_program(s):
    s.add("defs", {"def.class": PROGRAM.strip(), "name": "Program", "mod": ["public", "static"]})
    s.add("defs", {"def.func": "public static void Main() { }", "name": "Main", "mod": ["public", "static"]})
    s.add("roots", {"root.main": "Main"})


def test_csharp_a_live_member_keeps_its_class_alive():
    """The counterexample of the review: the root is on Main, the class that holds it must not be dead."""
    v = _v(extract("csharp", PROGRAM, build_program))
    assert v["A.cs#Program.Main"] == "alive" and v["A.cs#Program"] == "alive"


def test_csharp_golden_expectation_names_the_program_class_alive():
    exp = json.loads((FIX / "csharp" / "expected.json").read_text())
    assert "Program.cs#Program" in exp["alive"] and "Program.cs#Program" not in exp["dead"]
    assert exp["alive"] == sorted(exp["alive"]) and not set(exp["alive"]) & set(exp["dead"])


def test_a_live_member_of_a_nested_class_keeps_every_enclosing_class_alive():
    src = "class Outer { public class Inner { public static void Main() { } } }\n"

    def build(s):
        s.add("defs", {"def.class": src.strip(), "name": "Outer"})
        s.add("defs", {"def.class": "public class Inner { public static void Main() { } }", "name": "Inner"})
        s.add("defs", {"def.func": "public static void Main() { }", "name": "Main", "mod": ["public", "static"]})
        s.add("roots", {"root.main": "Main"})

    v = _v(extract("csharp", src, build))
    assert {v[f"A.cs#{k}"] for k in ("Outer", "Outer.Inner", "Outer.Inner.Main")} == {"alive"}


def test_the_holder_edge_is_a_flag_of_the_language_data_not_of_every_language():
    f = extract("csharp", PROGRAM, build_program)
    off = json.loads(Path(dclangs.DATA).read_text())
    for spec in off["langs"].values():
        spec.pop("holder_edge", None)
    res = dlink.link({"A.cs": f}, workers=1, langs=dclangs.Langs(off))
    v = rc.analyze(res.graph).verdicts()
    assert v["csharp:A.cs#Program.Main"] == "alive" and v["csharp:A.cs#Program"] in (
        "dead",
        "dead-cluster",
    )  # the control
    on = {k for k, s in dclangs.load().langs.items() if s.get("holder_edge")}
    assert on == {"cpp", "csharp", "powershell"}
    py = contracts.get_extractor("python").extract(
        "python", "a.py", "class Dead:\n    def m(self):\n        return 1\n"
    )
    assert (
        _v(py, "a.py")["a.py#Dead.m"] == "dead"
    )  # python keeps one finding per symbol, not a cluster of the class and its method


def test_the_holder_edge_of_an_out_of_class_member_goes_to_the_type_of_that_name_in_the_family():
    def hpp(s):
        s.add("defs", {"def.class": "class Foo { public: void run(); };", "name": "Foo"})
        s.add("defs", {"def.func": "void run();", "name": "run"})

    def cpp(s):
        s.add("defs", {"def.func": "void Foo::run() {}", "name": "Foo::run"})

    def main(s):
        s.add("defs", {"def.func": "int main() { g.run(); }", "name": "main"})
        s.add("refs", {"ref.call": "g.run()", "name": "g.run"})
        s.add("roots", {"root.main": "main"})

    v = verdicts(
        {
            "foo.hpp": extract("cpp", "class Foo { public: void run(); };\n", hpp),
            "foo.cpp": extract("cpp", "void Foo::run() {}\n", cpp),
            "main.cpp": extract("cpp", "int main() { g.run(); }\n", main),
        }
    )
    assert v["foo.cpp#Foo.run"] != "dead" and v["foo.hpp#Foo"] not in ("dead", "dead-cluster")


# ------------------------------------------------------------------ C#: data members, convention members, host bases, string uses
DTO_SRC = """public class Dto { public string Name { get; set; } public static int Counter; public const int Max = 1; private int Hidden { get; set; } [SerializeField] private float speed; [Obsolete] private int old; }
public class Unused { public string Id { get; set; } }
class Prog { static void Main() { var d = JsonSerializer.Deserialize<Dto>(s); } }
"""


def build_dto(s):
    one, two = DTO_SRC.splitlines()[:2]
    s.add("defs", {"def.class": one, "name": "Dto", "mod": "public"})
    s.add("defs", {"def.var": "public string Name { get; set; }", "name": "Name", "mod": "public"})
    s.add("defs", {"def.var": "public static int Counter;", "name": "Counter", "mod": ["public", "static"]})
    s.add("defs", {"def.var": "public const int Max = 1;", "name": "Max", "mod": ["public", "const"]})
    s.add("defs", {"def.var": "private int Hidden { get; set; }", "name": "Hidden", "mod": "private"})
    s.add("defs", {"def.var": "[SerializeField] private float speed;", "name": "speed", "mod": "private"})
    s.add("defs", {"def.var": "[Obsolete] private int old;", "name": "old", "mod": "private"})
    s.add("defs", {"def.class": two, "name": "Unused", "mod": "public"})
    s.add("defs", {"def.var": "public string Id { get; set; }", "name": "Id", "mod": "public"})
    s.add("defs", {"def.class": DTO_SRC.splitlines()[2], "name": "Prog"})
    s.add(
        "defs",
        {
            "def.func": "static void Main() { var d = JsonSerializer.Deserialize<Dto>(s); }",
            "name": "Main",
            "mod": "static",
        },
    )
    s.add("refs", {"ref.call": "JsonSerializer.Deserialize<Dto>(s)", "name": "JsonSerializer.Deserialize"})
    s.add(
        "refs", {"ref.ref": "Deserialize<Dto>", "name": "Dto"}
    )  # (identifier) @name @ref.ref: the type argument is a use
    s.add("roots", {"root.main": "Main"})
    s.add("roots", {"root.annotation.member-attr": "SerializeField"})
    s.add("roots", {"root.annotation.member-attr": "Obsolete"})


def test_csharp_serialized_properties_of_a_used_class_are_alive_and_the_rest_is_not():
    """The counterexample of the review (a DTO read through JsonSerializer): Name has no call, no read, only a serializer."""
    f = extract("csharp", DTO_SRC, build_dto)
    fl = {q: d.flags & model.F_PROTOCOL for q, d in defs_of(f).items()}
    assert fl["Dto.Name"] and fl["Dto.speed"]  # a public instance member, a member with a framework attribute
    assert not any(
        fl[q] for q in ("Dto.Counter", "Dto.Max", "Dto.Hidden", "Dto.old", "Dto")
    )  # static, const, private, [Obsolete]
    v = _v(f)
    assert v["A.cs#Dto"] == "alive" and v["A.cs#Dto.Name"] == "alive" and v["A.cs#Dto.speed"] == "alive"
    for k in ("Dto.Counter", "Dto.Max", "Dto.Hidden", "Dto.old", "Unused", "Unused.Id"):
        assert v[f"A.cs#{k}"] in ("dead", "dead-cluster"), k  # a class nobody uses stays dead with its properties


def test_csharp_data_member_rule_is_what_keeps_the_property_alive(monkeypatch):
    real = tg.rules_of
    monkeypatch.setattr(tg, "rules_of", lambda g: {k: v for k, v in real(g).items() if k != "protocol"})
    v = _v(extract("csharp", DTO_SRC, build_dto))
    assert v["A.cs#Dto"] == "alive" and v["A.cs#Dto.Name"] in ("dead", "dead-cluster")  # the control of the test above


CONV_SRC = """class Host { public void Configure() { } public IEnumerator GetEnumerator() { return null; } public void OnGetAsync() { } public void Helper() { } }
class Prog { static void Main() { var h = new Host(); } }
"""


def build_conv(s):
    s.add("defs", {"def.class": CONV_SRC.splitlines()[0], "name": "Host"})
    for m in (
        "public void Configure() { }",
        "public IEnumerator GetEnumerator() { return null; }",
        "public void OnGetAsync() { }",
        "public void Helper() { }",
    ):
        s.add("defs", {"def.func": m, "name": m.split("(")[0].split()[-1], "mod": "public"})
    s.add("defs", {"def.class": CONV_SRC.splitlines()[1], "name": "Prog"})
    s.add("defs", {"def.func": "static void Main() { var h = new Host(); }", "name": "Main", "mod": "static"})
    s.add("refs", {"ref.call": "new Host()", "name": "Host"})
    s.add("roots", {"root.main": "Main"})


def test_csharp_members_a_host_or_the_language_calls_by_convention_are_alive_with_their_class(monkeypatch):
    v = _v(extract("csharp", CONV_SRC, build_conv))
    for k in ("Configure", "GetEnumerator", "OnGetAsync"):
        assert v[f"A.cs#Host.{k}"] == "alive", k
    assert v["A.cs#Host.Helper"] in ("dead", "dead-cluster")  # an ordinary method nobody calls
    real = tg.rules_of
    monkeypatch.setattr(tg, "rules_of", lambda g: {**real(g), "protocol": real(g)["protocol"][:1]})  # the control
    assert _v(extract("csharp", CONV_SRC, build_conv))["A.cs#Host.Configure"] in ("dead", "dead-cluster")


def test_csharp_host_base_names_root_a_class_derived_from_a_project_base_and_a_qualified_base():
    src = "class OrdersController : BaseController { }\nclass Api : Microsoft.AspNetCore.Mvc.ControllerBase { }\nclass Plain : Foo { }\nclass Mono : MonoBehaviour { }\n"

    def build(s):
        for cls in ("OrdersController", "Api", "Plain", "Mono"):
            s.add("defs", {"def.class": next(ln for ln in src.splitlines() if f"class {cls} " in ln), "name": cls})
        for base in ("BaseController", "Microsoft.AspNetCore.Mvc.ControllerBase", "Foo", "MonoBehaviour"):
            s.add("roots", {"root.annotation.dotnet-host": base})

    got = {h.target for h in extract("csharp", src, build).roots if h.kind == "annotation:dotnet-host"}
    assert got == {"OrdersController", "Api", "Mono"}


STRINGS_SRC = """using static Ns.Util;
using static System.Math;
public static class Util { public static int Helper() { return 1; } }
public class Svc { public void Run() { } public void Spare() { } }
class Prog { static void Main() { t.GetMethod("Run"); t.Equals("Spare"); Helper(); } }
"""


def build_strings(s):
    s.add(
        "defs",
        {
            "def.class": "public static class Util { public static int Helper() { return 1; } }",
            "name": "Util",
            "mod": ["public", "static"],
        },
    )
    s.add(
        "defs", {"def.func": "public static int Helper() { return 1; }", "name": "Helper", "mod": ["public", "static"]}
    )
    s.add(
        "defs",
        {
            "def.class": "public class Svc { public void Run() { } public void Spare() { } }",
            "name": "Svc",
            "mod": "public",
        },
    )
    s.add("defs", {"def.func": "public void Run() { }", "name": "Run", "mod": "public"})
    s.add("defs", {"def.func": "public void Spare() { }", "name": "Spare", "mod": "public"})
    s.add("defs", {"def.class": STRINGS_SRC.splitlines()[4], "name": "Prog"})
    s.add(
        "defs",
        {
            "def.func": 'static void Main() { t.GetMethod("Run"); t.Equals("Spare"); Helper(); }',
            "name": "Main",
            "mod": "static",
        },
    )
    s.add("refs", {"dyn.static": "using static Ns.Util;"})
    s.add("refs", {"dyn.static": "using static System.Math;"})
    s.add("refs", {"cmd": "GetMethod", "dyn.name": '"Run"'})
    s.add("refs", {"cmd": "Equals", "dyn.name": '"Spare"'})
    s.add("roots", {"root.main": "Main"})


def test_csharp_using_static_and_reflection_strings_are_dyn_sites_with_their_own_patterns():
    f = extract("csharp", STRINGS_SRC, build_strings)
    assert sorted(d.pattern for d in f.dyn) == [
        "name:Run",
        "static:Math",
        "static:Util",
    ]  # Equals is not a reflection command
    v = _v(f)
    assert v["A.cs#Util.Helper"] not in ("dead", "dead-cluster") and v["A.cs#Util"] not in ("dead", "dead-cluster")
    assert v["A.cs#Svc.Run"] not in ("dead", "dead-cluster")  # named by GetMethod("Run")
    assert v["A.cs#Svc.Spare"] in ("dead", "dead-cluster")  # a string given to another method names nothing
    assert v["A.cs#Svc"] not in ("dead", "dead-cluster")  # the holder edge: a live member keeps its class


def test_a_using_static_of_a_type_the_project_lacks_makes_nothing_alive():
    src = "using static System.Math;\nclass Prog { static void Main() { } void Spare() { } }\n"

    def build(s):
        s.add("defs", {"def.class": src.splitlines()[1], "name": "Prog"})
        s.add("defs", {"def.func": "static void Main() { }", "name": "Main", "mod": "static"})
        s.add("defs", {"def.func": "void Spare() { }", "name": "Spare"})
        s.add("refs", {"dyn.static": "using static System.Math;"})
        s.add("roots", {"root.main": "Main"})

    assert _v(extract("csharp", src, build))["A.cs#Prog.Spare"] in ("dead", "dead-cluster")


def test_dyn_capture_kinds_are_data_of_the_language(monkeypatch):
    real = tg.rules_of
    monkeypatch.setattr(tg, "rules_of", lambda g: {k: v for k, v in real(g).items() if k != "dyn"})
    patterns = {d.pattern for d in extract("csharp", STRINGS_SRC, build_strings).dyn}
    assert (
        "name:Run" not in patterns and "static:Util" not in patterns
    )  # without the rules the capture text is the pattern


# ------------------------------------------------------------------ C and C++
def test_c_entry_points_of_a_loader_and_link_time_hooks_are_roots_by_name_and_attribute():
    src = 'int PyInit_mod(void) { return 0; }\nvoid Java_com_x_Foo_bar(void) { }\nint mainly(void) { return 0; }\nint DllMain(void) { return 1; }\nint JNI_OnLoad(void) { return 0; }\nint Java(void) { return 0; }\n__attribute__((weak)) void hook(void) { }\n__attribute__((section(".init"))) void sec(void) { }\n__attribute__((unused)) void other(void) { }\n'

    def build(s):
        for fn in (
            "PyInit_mod",
            "Java_com_x_Foo_bar",
            "mainly",
            "DllMain",
            "JNI_OnLoad",
            "Java",
            "hook",
            "sec",
            "other",
        ):
            line = next(ln for ln in src.splitlines() if f" {fn}(" in ln)
            s.add("defs", {"def.func": line, "name": fn})
            if fn in ("hook", "sec", "other"):
                s.add(
                    "roots",
                    {
                        "root.annotation.c-attribute": (
                            line.split(")")[0] + ")" if fn != "sec" else '__attribute__((section(".init")))'
                        )
                    },
                )
            else:
                s.add("roots", {"root.main": (fn, 0)})

    f = extract("c", src, build)
    assert {h.target for h in f.roots if h.kind == "main"} == {
        "PyInit_mod",
        "Java_com_x_Foo_bar",
        "DllMain",
        "JNI_OnLoad",
    }
    assert {h.target for h in f.roots if h.kind == "annotation:c-attribute"} == {"hook", "sec"}


def test_cpp_template_members_written_out_of_class_lose_their_arguments_and_operators_are_protocol():
    hpp = "template <class T> class Foo { public: Foo(); void run(); void helper(); };\n"
    cpp = '#include "foo.hpp"\ntemplate <class T> Foo<T>::Foo() {}\ntemplate <class T> bool Foo<std::vector<T>>::operator==(const Foo& o) const { return true; }\ntemplate <class T> void Foo<T>::run() {}\n'
    main = '#include "foo.hpp"\nint main() { Foo<int> f; f.run(); return 0; }\n'

    def b_hpp(s):
        s.add("defs", {"def.class": hpp.strip(), "name": "Foo"})
        for m, n in (("Foo();", "Foo"), ("void run();", "run"), ("void helper();", "helper")):
            s.add("defs", {"def.func": m, "name": n})

    def b_cpp(s):
        s.add("defs", {"def.func": "Foo<T>::Foo() {}", "name": "Foo<T>::Foo"})
        s.add(
            "defs",
            {
                "def.func": "bool Foo<std::vector<T>>::operator==(const Foo& o) const { return true; }",
                "name": "Foo<std::vector<T>>::operator==",
            },
        )
        s.add("defs", {"def.func": "void Foo<T>::run() {}", "name": "Foo<T>::run"})
        s.add("refs", {"ref.include": '#include "foo.hpp"', "name": '"foo.hpp"'})

    def b_main(s):
        s.add("defs", {"def.func": "int main() { Foo<int> f; f.run(); return 0; }", "name": "main"})
        s.add("refs", {"ref.include": '#include "foo.hpp"', "name": '"foo.hpp"'})
        s.add("refs", {"ref.ref": "Foo<int> f;", "name": "Foo"})
        s.add("refs", {"ref.call": "f.run()", "name": "f.run"})
        s.add("roots", {"root.main": "main"})

    fc = extract("cpp", cpp, b_cpp)
    assert set(defs_of(fc)) == {"", "Foo.Foo", "Foo.operator==", "Foo.run"}
    assert (
        defs_of(fc)["Foo.operator=="].flags & model.F_PROTOCOL and not defs_of(fc)["Foo.run"].flags & model.F_PROTOCOL
    )
    v = verdicts({"foo.hpp": extract("cpp", hpp, b_hpp), "foo.cpp": fc, "main.cpp": extract("cpp", main, b_main)})
    assert v["foo.cpp#Foo.Foo"] != "dead" and v["foo.cpp#Foo.operator=="] != "dead" and v["foo.cpp#Foo.run"] != "dead"
    assert v["foo.hpp#Foo.helper"] in ("dead", "dead-cluster")


def test_cpp_free_functions_found_by_argument_dependent_lookup_are_roots():
    src = "void to_json(json& j, const Foo& f) { }\nvoid swap(Foo& a, Foo& b) { }\nvoid helper() { }\n"

    def build(s):
        for fn in ("to_json", "swap", "helper"):
            s.add("defs", {"def.func": next(ln for ln in src.splitlines() if f"void {fn}(" in ln), "name": fn})
            s.add("roots", {"root.annotation.adl": (fn, 0)})

    assert {h.target for h in extract("cpp", src, build).roots if h.kind == "annotation:adl"} == {"to_json", "swap"}


def test_cpp_strip_angles_leaves_operators_and_plain_names_alone():
    rules = {"strip_angles": True}
    for raw, want in (
        ("Foo<T>::get", "Foo::get"),
        ("a<b<c>>::d", "a::d"),
        ("Foo::operator<", "Foo::operator<"),
        ("Foo<T>::operator<<", "Foo::operator<<"),
        ("plain", "plain"),
    ):
        assert tg._name(rules, raw) == want
    assert tg._name({}, "Foo<T>::get") == "Foo<T>::get"


# ------------------------------------------------------------------ PowerShell
def test_pwsh_class_properties_and_conventional_methods_live_with_their_class_and_the_rest_does_not(worker):
    f = ps(
        worker,
        [
            ["class", "A", 1, 1, 9, 2, [], "class"],
            ["prop", "Name", 2, 1, 2, 12],
            ["method", "ToString", 3, 1, 3, 30, False, 1, False],
            ["method", "Dead", 4, 1, 4, 20, False, 1, False],
            ["class", "Unused", 10, 1, 12, 2, [], "class"],
            ["prop", "P", 11, 1, 11, 9],
            ["type", "A", 13, 1, 13, 5],
        ],
    )
    d = defs_of(f)
    assert (
        d["a.name"].flags & model.F_PROTOCOL
        and d["a.tostring"].flags & model.F_PROTOCOL
        and not d["a.dead"].flags & model.F_PROTOCOL
    )
    v = _vh(f)
    assert v["a.ps1#a.name"] != "dead"
    assert v["a.ps1#a.tostring"] != "dead" and v["a.ps1#a.dead"] in ("dead", "dead-cluster")
    assert v["a.ps1#unused"] in ("dead", "dead-cluster") and v["a.ps1#unused.p"] in ("dead", "dead-cluster")


def test_pwsh_a_live_member_keeps_its_class_alive(worker):
    f = ps(
        worker,
        [
            ["class", "A", 1, 1, 5, 2, [], "class"],
            ["method", "Run", 2, 1, 2, 20, False, 1, True],
            ["member", "run", 7, 1, 7, 9, False, "$x", True],
        ],
    )
    v = _vh(f)
    assert (
        v["a.ps1#a.run"] != "dead" and v["a.ps1#a"] != "dead"
    )  # `$x.Run()` is a name match: the class is not named anywhere


def test_pwsh_a_function_with_an_alias_attribute_is_a_soft_root(worker):
    f = ps(
        worker,
        [
            ["func", "Get-Foo", 1, 1, 3, 2, False, 1, True],
            ["func", "Get-Plain", 4, 1, 5, 2, False, 1, False],
            ["func", "Old", 6, 1, 6, 9, False, 1],
        ],
    )
    assert [(h.kind, h.target, h.strength) for h in f.roots] == [("annotation:alias", "get-foo", "soft")]


def test_pwsh_scripts_to_process_of_a_manifest_is_an_include(worker):
    f = ps(
        worker,
        [["hash", "ScriptsToProcess", 1, 1, 1, 40, ["./init.ps1"]], ["hash", "RootModule", 2, 1, 2, 30, ["M.psm1"]]],
    )
    assert {("init.ps1", "include", ""), ("M.psm1", "include", "")} <= refs_of(f)


def test_pwsh_rules_are_part_of_the_shard_key(monkeypatch, tmp_path):
    base = pw.script_sha()
    other = tmp_path / "rules.json"
    other.write_text('{"protocol": []}')
    pw.script_sha.cache_clear()
    pw._rules.cache_clear()
    monkeypatch.setattr(pw, "RULES", str(other))
    try:
        assert pw.script_sha() != base
    finally:
        monkeypatch.undo()
        pw.script_sha.cache_clear()
        pw._rules.cache_clear()
    assert pw.script_sha() == base


@needs_pwsh
def test_pwsh_real_alias_attribute_hashtable_cast_property_and_tostring():
    src = "class Foo { [string]$Name; [string] ToString() { return 'x' } [void] Dead() { } }\nfunction Get-Foo { [CmdletBinding()][Alias('gf')]param() 1 }\nfunction Get-Param { param([Alias('x')]$a) 3 }\n$f = [Foo]@{ Name = 'a' }\n"
    f = contracts.get_extractor("powershell").extract("powershell", "a.ps1", src)
    assert [(h.kind, h.target) for h in f.roots] == [
        ("annotation:alias", "get-foo")
    ]  # a parameter alias is not a function alias
    v = _vh(f)
    assert (
        v["a.ps1#foo.name"] != "dead"
        and v["a.ps1#foo.tostring"] != "dead"
        and v["a.ps1#foo.dead"] in ("dead", "dead-cluster")
    )


# ====================================================================== second pass 3: chained receivers, bare names that reach members, host attributes
def scripted(lang, src, defs=(), refs=(), roots=()):
    """extract() with the matches as data: defs [(kind, snippet, name, mods)], refs and roots [capture dict] (see Scripted.add)."""

    def build(s):
        for kind, snippet, name, *mods in defs:
            s.add("defs", {f"def.{kind}": snippet, "name": name, **({"mod": mods[0]} if mods else {})})
        for caps in refs:
            s.add("refs", caps)
        for caps in roots:
            s.add("roots", caps)

    return extract(lang, src, build)


def alive(v, *keys):  # not reported: alive, weak-alive, test-only
    return all(v[k] not in ("dead", "dead-cluster") for k in keys)


CHAIN_SRC = """class Repo { public void Save() { } public void Spare() { } }
class Svc { public Repo GetRepo() { return null; } public void Tidy() { } }
class Prog { static void Main(Svc svc) { svc.GetRepo().Save(); new Svc().Tidy(); svc?.Tidy2(); } void Tidy2() { } }
"""


def chain_file():
    return scripted(
        "csharp",
        CHAIN_SRC,
        defs=[
            ("class", "class Repo { public void Save() { } public void Spare() { } }", "Repo"),
            ("func", "public void Save() { }", "Save", "public"),
            ("func", "public void Spare() { }", "Spare", "public"),
            ("class", "class Svc { public Repo GetRepo() { return null; } public void Tidy() { } }", "Svc"),
            ("func", "public Repo GetRepo() { return null; }", "GetRepo", "public"),
            ("func", "public void Tidy() { }", "Tidy", "public"),
            ("class", CHAIN_SRC.splitlines()[2], "Prog"),
            (
                "func",
                "static void Main(Svc svc) { svc.GetRepo().Save(); new Svc().Tidy(); svc?.Tidy2(); }",
                "Main",
                "static",
            ),
            ("func", "void Tidy2() { }", "Tidy2"),
        ],
        refs=[
            {"ref.call": "svc.GetRepo().Save", "name": "svc.GetRepo().Save"},
            {"ref.call": "svc.GetRepo", "name": "svc.GetRepo"},
            {"ref.call": "new Svc().Tidy", "name": "new Svc().Tidy"},
            {"ref.call": "new Svc()", "name": "Svc"},
            {"ref.call": "svc?.Tidy2()", "member": "Tidy2"},
        ],
        roots=[{"root.main": "Main"}],
    )


def test_csharp_a_member_called_through_a_chained_or_conditional_receiver_is_alive():
    """Review round 2, blocker 1: `new B().Run()`, `a.F().G()` and `x?.M()` wrote the ref `?.m`, which link.py read as the bare name m (top-level defs only)."""
    f = chain_file()
    assert {
        ("?.Save", "call", "Prog.Main"),
        ("?.Tidy", "call", "Prog.Main"),
        ("?.Tidy2", "call", "Prog.Main"),
    } <= refs_of(f)
    v = _v(f)
    assert alive(v, "A.cs#Repo.Save", "A.cs#Svc.GetRepo", "A.cs#Svc.Tidy", "A.cs#Prog.Tidy2", "A.cs#Repo", "A.cs#Svc")
    assert v["A.cs#Repo.Spare"] in ("dead", "dead-cluster")  # nothing calls it: a name match is not a blanket


def test_a_chained_receiver_ref_reaches_members_by_name_and_never_a_top_level_def():
    def hpp(s):
        s.add("defs", {"def.class": "class Foo { public: void run(); };", "name": "Foo"})
        s.add("defs", {"def.func": "void run();", "name": "run"})

    def other(s):
        s.add("defs", {"def.func": "void run() { }", "name": "run"})

    def main(s):
        s.add("defs", {"def.func": "int main() { getFoo()->run(); }", "name": "main"})
        s.add("refs", {"ref.call": "getFoo()->run", "name": "getFoo()->run"})
        s.add("roots", {"root.main": "main"})

    f = extract("cpp", "int main() { getFoo()->run(); }\n", main)
    assert ("?.run", "call", "main") in refs_of(f)  # name_map -> getFoo().run, not a dotted identifier
    v = verdicts(
        {
            "foo.hpp": extract("cpp", "class Foo { public: void run(); };\n", hpp),
            "other.cpp": extract("cpp", "void run() { }\n", other),
            "main.cpp": f,
        }
    )
    assert alive(v, "foo.hpp#Foo.run") and v["other.cpp#run"] in ("dead", "dead-cluster")


def test_a_chained_receiver_read_of_a_property_is_a_use_of_the_member():
    src = "class Box { internal int Size { get; set; } internal int Spare { get; set; } }\nclass Prog { static void Main() { Make().Size.ToString(); } }\n"
    f = scripted(
        "csharp",
        src,
        defs=[
            ("class", src.splitlines()[0], "Box"),
            ("var", "internal int Size { get; set; }", "Size", "internal"),
            ("var", "internal int Spare { get; set; }", "Spare", "internal"),
            ("class", src.splitlines()[1], "Prog"),
            ("func", "static void Main() { Make().Size.ToString(); }", "Main", "static"),
        ],
        refs=[{"ref.ref": "Make().Size", "name": "Make().Size"}],
        roots=[{"root.main": "Main"}],
    )
    assert ("?.Size", "ref", "Prog.Main") in refs_of(f)  # a weak ref keeps the member, as `x.Size` does
    v = _v(f)
    assert alive(v, "A.cs#Box.Size") and v["A.cs#Box.Spare"] in ("dead", "dead-cluster")


def test_csharp_a_property_set_by_an_object_initializer_is_a_use_of_it():
    src = "class Foo { internal int Id { get; set; } internal int Spare { get; set; } }\nclass Prog { static void Main() { var f = new Foo { Id = 1 }; } }\n"
    f = scripted(
        "csharp",
        src,
        defs=[
            ("class", src.splitlines()[0], "Foo"),
            ("var", "internal int Id { get; set; }", "Id", "internal"),
            ("var", "internal int Spare { get; set; }", "Spare", "internal"),
            ("class", src.splitlines()[1], "Prog"),
            ("func", "static void Main() { var f = new Foo { Id = 1 }; }", "Main", "static"),
        ],
        refs=[{"ref.call": "new Foo { Id = 1 }", "name": "Foo"}, {"ref.ref": "{ Id = 1 }", "member": "Id"}],
        roots=[{"root.main": "Main"}],
    )
    assert ("?.Id", "ref", "Prog.Main") in refs_of(f)
    v = _v(f)
    assert alive(v, "A.cs#Foo.Id") and v["A.cs#Foo.Spare"] in ("dead", "dead-cluster")


def test_pwsh_a_method_called_on_the_result_of_a_call_is_alive_with_what_it_calls(worker):
    """Review round 2, blocker 1 (PowerShell): `$o.Run().Finish()` is the ref `?.finish`; Finish calls Cleanup, so a missed Finish also killed Cleanup."""
    f = ps(
        worker,
        [
            ["class", "Svc", 1, 1, 8, 2, [], "class"],
            ["method", "Run", 2, 1, 2, 30, False, 1, False],
            ["method", "Finish", 3, 1, 5, 2, False, 1, False],
            ["method", "Cleanup", 6, 1, 6, 30, False, 1, False],
            ["method", "Spare", 7, 1, 7, 30, False, 1, False],
            ["member", "Cleanup", 4, 3, 4, 20, False, "$this", True],
            ["member", "Run", 12, 1, 12, 9, False, "$o", True],
            ["member", "Finish", 12, 10, 12, 20, False, "", True],
        ],
    )
    assert ("?.finish", "call", "") in refs_of(f)
    v = _vh(f)
    assert alive(v, "a.ps1#svc.run", "a.ps1#svc.finish", "a.ps1#svc.cleanup", "a.ps1#svc")
    assert v["a.ps1#svc.spare"] in ("dead", "dead-cluster")


@needs_pwsh
def test_pwsh_real_chained_method_call_keeps_the_method_and_its_callee_alive():
    src = "class Svc { [Svc] Run() { return $this } [void] Finish() { $this.Cleanup() } [void] Cleanup() { } [void] Spare() { } }\n$o = [Svc]::new()\n$o.Run().Finish()\n"
    v = _vh(contracts.get_extractor("powershell").extract("powershell", "a.ps1", src))
    assert alive(v, "a.ps1#svc.run", "a.ps1#svc.finish", "a.ps1#svc.cleanup")
    assert v["a.ps1#svc.spare"] in ("dead", "dead-cluster")


INH_SRC = """class A { protected void Helper() { } protected void Spare() { } }
class B : A { public void Run() { Helper(); } }
class Prog { static void Main() { var b = new B(); b.Run(); } }
"""


def inh_file():
    return scripted(
        "csharp",
        INH_SRC,
        defs=[
            ("class", INH_SRC.splitlines()[0], "A"),
            ("func", "protected void Helper() { }", "Helper", "protected"),
            ("func", "protected void Spare() { }", "Spare", "protected"),
            ("class", INH_SRC.splitlines()[1], "B"),
            ("func", "public void Run() { Helper(); }", "Run", "public"),
            ("class", INH_SRC.splitlines()[2], "Prog"),
            ("func", "static void Main() { var b = new B(); b.Run(); }", "Main", "static"),
        ],
        refs=[
            {"ref.inherit": ": A", "name": "A"},
            {"ref.call": ("Helper()", 1), "name": "Helper"},
            {"ref.call": "new B()", "name": "B"},
            {"ref.call": "b.Run()", "name": "b.Run"},
        ],
        roots=[{"root.main": "Main"}],
    )


def test_csharp_a_base_class_method_called_by_its_bare_name_from_a_derived_class_is_alive():
    """Review round 2, blocker 2a: `class B : A { void Run() { Helper(); } }` left A.Helper dead, even in one file."""
    v = _v(inh_file())
    assert alive(v, "A.cs#A.Helper", "A.cs#A") and v["A.cs#A.Spare"] in ("dead", "dead-cluster")


def test_the_bare_name_lookup_of_a_member_is_a_flag_of_the_language_data():
    off = json.loads(Path(dclangs.DATA).read_text())
    for spec in off["langs"].values():
        spec.pop("type_scope", None)
    res = dlink.link({"A.cs": inh_file()}, workers=1, langs=dclangs.Langs(off))
    assert rc.analyze(res.graph).verdicts()["csharp:A.cs#A.Helper"] in ("dead", "dead-cluster")  # the control
    assert {k for k, s in dclangs.load().langs.items() if s.get("type_scope")} == {"cpp", "csharp"}
    py = contracts.get_extractor("python").extract(
        "python",
        "a.py",
        "class A:\n    def helper(self):\n        return 1\nclass B(A):\n    def run(self):\n        return helper()\n",
    )
    assert _v(py, "a.py")["a.py#A.helper"] == "dead"  # python has no implicit self: a bare name is no member


PART1 = "partial class P { static void Main() { Other(); } }\n"
PART2 = "partial class P { void Other() { } void Spare() { } }\n"


def test_csharp_a_member_of_the_other_part_of_a_partial_class_is_reached_by_its_bare_name():
    def one(s):
        s.add("defs", {"def.class": PART1.strip(), "name": "P"})
        s.add("defs", {"def.func": "static void Main() { Other(); }", "name": "Main", "mod": "static"})
        s.add("refs", {"ref.call": "Other()", "name": "Other"})
        s.add("roots", {"root.main": "Main"})

    def two(s):
        s.add("defs", {"def.class": PART2.strip(), "name": "P"})
        s.add("defs", {"def.func": "void Other() { }", "name": "Other"})
        s.add("defs", {"def.func": "void Spare() { }", "name": "Spare"})

    v = verdicts({"P1.cs": extract("csharp", PART1, one), "P2.cs": extract("csharp", PART2, two)})
    assert alive(v, "P2.cs#P.Other", "P2.cs#P") and v["P2.cs#P.Spare"] in ("dead", "dead-cluster")


def test_csharp_a_bare_name_in_a_nested_class_reaches_the_member_of_the_outer_class_in_another_part():
    outer = "partial class Outer { public class Inner { public static void Main() { Shared(); } } }\n"
    more = "partial class Outer { static void Shared() { } }\n"

    def b1(s):
        s.add("defs", {"def.class": outer.strip(), "name": "Outer"})
        s.add("defs", {"def.class": "public class Inner { public static void Main() { Shared(); } }", "name": "Inner"})
        s.add(
            "defs", {"def.func": "public static void Main() { Shared(); }", "name": "Main", "mod": ["public", "static"]}
        )
        s.add("refs", {"ref.call": "Shared()", "name": "Shared"})
        s.add("roots", {"root.main": "Main"})

    def b2(s):
        s.add("defs", {"def.class": more.strip(), "name": "Outer"})
        s.add("defs", {"def.func": "static void Shared() { }", "name": "Shared", "mod": "static"})

    v = verdicts({"O1.cs": extract("csharp", outer, b1), "O2.cs": extract("csharp", more, b2)})
    assert alive(v, "O2.cs#Outer.Shared")


def test_csharp_a_member_named_like_its_type_does_not_hide_the_type():
    """`public Address Address { get; set; }`: the type identifier resolves to the property in its scope chain; the class is used all the same."""
    src = "class Address { }\nclass Order { public Address Address { get; set; } }\nclass Prog { static void Main() { var o = new Order(); } }\n"
    f = scripted(
        "csharp",
        src,
        defs=[
            ("class", "class Address { }", "Address"),
            ("class", src.splitlines()[1], "Order"),
            ("var", "public Address Address { get; set; }", ("Address", 1), "public"),
            ("class", src.splitlines()[2], "Prog"),
            ("func", "static void Main() { var o = new Order(); }", "Main", "static"),
        ],
        refs=[
            {"ref.ref": "public Address Address { get; set; }", "name": ("Address", 0)},
            {"ref.call": "new Order()", "name": "Order"},
        ],
        roots=[{"root.main": "Main"}],
    )
    assert ("Address", "ref", "Order.Address") in refs_of(f) or ("Address", "ref", "Order") in refs_of(f)
    assert alive(_v(f), "A.cs#Address")


def test_csharp_this_field_method_chain_reaches_the_method_of_the_field_type():
    src = "class Repo { public void Save() { } }\nclass Svc { Repo repo; public static void Main() { var s = new Svc(); s.Go(); } void Go() { this.repo.Save(); } }\n"
    f = scripted(
        "csharp",
        src,
        defs=[
            ("class", "class Repo { public void Save() { } }", "Repo"),
            ("func", "public void Save() { }", "Save", "public"),
            ("class", src.splitlines()[1], "Svc"),
            ("var", "Repo repo;", "repo"),
            ("func", "public static void Main() { var s = new Svc(); s.Go(); }", "Main", ["public", "static"]),
            ("func", "void Go() { this.repo.Save(); }", "Go"),
        ],
        refs=[{"ref.call": "this.repo.Save()", "name": "this.repo.Save"}, {"ref.call": "s.Go()", "name": "s.Go"}],
        roots=[{"root.main": "Main"}],
    )
    assert alive(_v(f), "A.cs#Repo.Save", "A.cs#Repo")


# ------------------------------------------------------------------ C and C++: a member declared in the class of a header and written out of class
HPP2 = "class Foo { public: void run(); private: void helper(); void spare(); };\n"
CPP2 = "void Foo::helper() { }\nvoid Foo::run() { helper(); }\n"
MAIN2 = "int main() { Foo f; f.run(); }\n"


def cpp_pair_files(
    hpp_src=HPP2,
    cpp_src=CPP2,
    cpp_defs=(("Foo::helper", "void Foo::helper() { }"), ("Foo::run", "void Foo::run() { helper(); }")),
):
    def hpp(s):
        s.add("defs", {"def.class": hpp_src.strip(), "name": "Foo"})
        for name in ("run", "helper", "spare"):
            if f"void {name}();" in hpp_src:
                s.add("defs", {"def.func": f"void {name}();", "name": name})

    def cpp(s):
        for name, snippet in cpp_defs:
            s.add("defs", {"def.func": snippet, "name": name})
        s.add("refs", {"ref.call": "helper();", "name": "helper"})

    def main(s):
        s.add("defs", {"def.func": MAIN2.strip(), "name": "main"})
        s.add("refs", {"ref.call": "f.run()", "name": "f.run"})
        s.add("roots", {"root.main": "main"})

    return {
        "foo.hpp": extract("cpp", hpp_src, hpp),
        "foo.cpp": extract("cpp", cpp_src, cpp),
        "main.cpp": extract("cpp", MAIN2, main),
    }


def test_cpp_the_header_declaration_of_a_live_helper_is_alive():
    """Review round 2, blocker 2b: Foo::run is live and calls helper(); the declaration foo.hpp#Foo.helper was reported for deletion (the build breaks)."""
    v = verdicts(cpp_pair_files())
    assert alive(v, "foo.cpp#Foo.run", "foo.cpp#Foo.helper", "foo.hpp#Foo.helper", "foo.hpp#Foo.run", "foo.hpp#Foo")
    assert v["foo.hpp#Foo.spare"] in ("dead", "dead-cluster")  # declared, never defined, never called


def test_cpp_a_member_called_from_the_header_reaches_its_definition_in_the_cpp():
    hpp = "class Foo { public: void run() { helper(); } void helper(); };\n"
    cpp = "void Foo::helper() { }\n"

    def b_hpp(s):
        s.add("defs", {"def.class": hpp.strip(), "name": "Foo"})
        s.add("defs", {"def.func": "void run() { helper(); }", "name": "run"})
        s.add("defs", {"def.func": "void helper();", "name": "helper"})
        s.add("refs", {"ref.call": "helper();", "name": "helper"})

    def b_cpp(s):
        s.add("defs", {"def.func": "void Foo::helper() { }", "name": "Foo::helper"})

    def b_main(s):
        s.add("defs", {"def.func": MAIN2.strip(), "name": "main"})
        s.add("refs", {"ref.call": "f.run()", "name": "f.run"})
        s.add("roots", {"root.main": "main"})

    files = {
        "foo.hpp": extract("cpp", hpp, b_hpp),
        "foo.cpp": extract("cpp", cpp, b_cpp),
        "main.cpp": extract("cpp", MAIN2, b_main),
    }
    assert alive(verdicts(files), "foo.cpp#Foo.helper", "foo.hpp#Foo.helper")


def test_cpp_a_namespace_qualified_out_of_class_definition_belongs_to_the_class_of_the_header():
    hpp = "namespace n { class Foo { public: void run(); void helper(); }; }\n"
    cpp = "void n::Foo::helper() { }\nvoid n::Foo::run() { helper(); }\n"

    def b_hpp(s):
        s.add("defs", {"def.class": "class Foo { public: void run(); void helper(); };", "name": "Foo"})
        s.add("defs", {"def.func": "void run();", "name": "run"})
        s.add("defs", {"def.func": "void helper();", "name": "helper"})

    def b_cpp(s):
        s.add("defs", {"def.func": "void n::Foo::helper() { }", "name": "n::Foo::helper"})
        s.add("defs", {"def.func": "void n::Foo::run() { helper(); }", "name": "n::Foo::run"})
        s.add("refs", {"ref.call": "helper();", "name": "helper"})

    def b_main(s):
        s.add("defs", {"def.func": MAIN2.strip(), "name": "main"})
        s.add("refs", {"ref.call": "f.run()", "name": "f.run"})
        s.add("roots", {"root.main": "main"})

    f_cpp = extract("cpp", cpp, b_cpp)
    assert {"n.Foo.helper", "n.Foo.run"} <= set(defs_of(f_cpp))
    v = verdicts({"foo.hpp": extract("cpp", hpp, b_hpp), "foo.cpp": f_cpp, "main.cpp": extract("cpp", MAIN2, b_main)})
    assert alive(
        v, "foo.hpp#Foo.helper", "foo.cpp#n.Foo.helper", "foo.hpp#Foo"
    )  # the holder edge finds the class under its short name too


def test_cpp_a_base_class_member_called_by_its_bare_name_from_a_derived_class_is_alive():
    src = "class A { protected: void helper(); void spare(); };\nclass B : public A { public: void run() { helper(); } };\nint main() { B b; b.run(); }\n"

    def build(s):
        s.add("defs", {"def.class": "class A { protected: void helper(); void spare(); };", "name": "A"})
        s.add("defs", {"def.func": "void helper();", "name": "helper"})
        s.add("defs", {"def.func": "void spare();", "name": "spare"})
        s.add("defs", {"def.class": "class B : public A { public: void run() { helper(); } };", "name": "B"})
        s.add("defs", {"def.func": "void run() { helper(); }", "name": "run"})
        s.add("defs", {"def.func": "int main() { B b; b.run(); }", "name": "main"})
        s.add("refs", {"ref.inherit": ": public A", "name": "A"})
        s.add("refs", {"ref.call": "helper(); } };", "name": "helper"})
        s.add("refs", {"ref.call": "b.run()", "name": "b.run"})
        s.add("roots", {"root.main": "main"})

    v = _v(extract("cpp", src, build), "a.cpp")
    assert alive(v, "a.cpp#A.helper") and v["a.cpp#A.spare"] in ("dead", "dead-cluster")


def test_the_edges_of_a_split_member_do_not_depend_on_the_worker_count():
    files = cpp_pair_files()
    assert len({dlink.link(files, workers=w).graph.digest() for w in (1, 2, 8)}) == 1


# ------------------------------------------------------------------ C#: an attribute that makes a host build or call the type; PowerShell DSC
HOST_SRC = """[Export(typeof(IPlugin))] class MyPlugin : IPlugin { }
[System.Runtime.Serialization.DataContract] class Msg { }
[Obsolete] class Old { }
class Menu { [MenuItem("x")] static void Tool() { } }
class Prog { static void Main() { } }
"""


def test_csharp_a_class_or_member_with_a_host_attribute_is_a_root_and_a_plain_attribute_is_not():
    """Review round 2, minor: `[Export(typeof(IPlugin))] class MyPlugin : IPlugin` (MEF), [DataContract] and friends came out dead."""
    src = HOST_SRC.splitlines()
    f = scripted(
        "csharp",
        HOST_SRC,
        defs=[
            ("class", src[0], "MyPlugin"),
            ("class", src[1], "Msg"),
            ("class", src[2], "Old"),
            ("class", src[3], "Menu"),
            (
                "func",
                '[MenuItem("x")] static void Tool() { }',
                "Tool",
                "static",
            ),  # the attribute is part of the declaration
            ("class", src[4], "Prog"),
            ("func", "static void Main() { }", "Main", "static"),
        ],
        roots=[
            {"root.annotation.host-attr": "Export"},
            {"root.annotation.host-attr": "System.Runtime.Serialization.DataContract"},
            {"root.annotation.host-attr": "Obsolete"},  # the query matches every attribute, rules.json filters
            {"root.annotation.host-attr": "MenuItem"},
            {"root.main": "Main"},
        ],
    )
    assert {(h.kind, h.target) for h in f.roots if h.kind.startswith("annotation:")} == {
        ("annotation:host-attr", "MyPlugin"),
        ("annotation:host-attr", "Msg"),
        ("annotation:host-attr", "Menu.Tool"),
    }
    v = _v(f)
    assert alive(v, "A.cs#MyPlugin", "A.cs#Msg", "A.cs#Menu.Tool", "A.cs#Menu") and v["A.cs#Old"] in (
        "dead",
        "dead-cluster",
    )


def test_csharp_host_attribute_names_are_data_of_the_language():
    rx = re.compile(tg.rules_of("csharp")["roots"]["annotation.host-attr"]["re"])
    for yes in (
        "Export",
        "ExportAttribute",
        "System.ComponentModel.Composition.Export",
        "DataContract",
        "ServiceContract",
        "XmlRoot",
        "ComVisible",
        "CreateAssetMenu",
        "MenuItem",
        "Table",
    ):
        assert rx.search(yes), yes
    for no in ("Obsolete", "Serializable", "ExportedThing", "MyExport", "SuppressMessage", "Fact"):
        assert not rx.search(no), no
    assert "annotation.host-attr" in {
        c[len("@root.") :]
        for c in re.findall(r"@root\.annotation\.[\w-]+", (QDIR / "csharp" / "roots.scm").read_text())
    }


def test_pwsh_dsc_resource_methods_live_with_their_class_and_a_plain_method_does_not(worker):
    f = ps(
        worker,
        [
            ["class", "Res", 1, 1, 8, 2, [], "class"],
            ["method", "Get", 2, 1, 2, 30, False, 1, False],
            ["method", "Set", 3, 1, 3, 30, False, 1, False],
            ["method", "Test", 4, 1, 4, 30, False, 1, False],
            ["method", "Spare", 5, 1, 5, 30, False, 1, False],
            ["type", "Res", 12, 1, 12, 6],
        ],
    )
    v = _vh(f)
    assert alive(v, "a.ps1#res.get", "a.ps1#res.set", "a.ps1#res.test") and v["a.ps1#res.spare"] in (
        "dead",
        "dead-cluster",
    )


def test_csharp_a_generic_method_called_on_a_receiver_is_a_use_of_the_member():
    """`repo.Get<Foo>(1)`: the member_access text has type arguments; it used to be dropped (not a dotted identifier, no last word) and only the bare `Get` was left."""
    src = "class Repo { public T Get<T>(int id) { return default; } public void Spare() { } }\nclass Prog { static void Main(Repo repo) { repo.Get<Repo>(1); repo.Make<int>().Run<Repo>(); } }\n"
    f = scripted(
        "csharp",
        src,
        defs=[
            ("class", src.splitlines()[0], "Repo"),
            ("func", "public T Get<T>(int id) { return default; }", "Get", "public"),
            ("func", "public void Spare() { }", "Spare", "public"),
            ("class", src.splitlines()[1], "Prog"),
            (
                "func",
                "static void Main(Repo repo) { repo.Get<Repo>(1); repo.Make<int>().Run<Repo>(); }",
                "Main",
                "static",
            ),
        ],
        refs=[
            {"ref.call": "repo.Get<Repo>", "name": "repo.Get<Repo>"},
            {"ref.call": "repo.Make<int>().Run<Repo>", "name": "repo.Make<int>().Run<Repo>"},
        ],
        roots=[{"root.main": "Main"}],
    )
    assert {("repo.Get", "call", "Prog.Main"), ("?.Run", "call", "Prog.Main")} <= refs_of(f)
    v = _v(f)
    assert alive(v, "A.cs#Repo.Get", "A.cs#Repo") and v["A.cs#Repo.Spare"] in ("dead", "dead-cluster")


def test_python_a_method_called_on_the_result_of_a_call_is_alive_too():
    """The `?.m` ref is written by the python extractor as well (`make().run()`): it names a member, not a top-level def."""
    src = "class A:\n    def run(self):\n        return 1\n\n    def spare(self):\n        return 2\n\n\ndef make():\n    return A()\n\n\nmake().run()\n"
    f = contracts.get_extractor("python").extract("python", "a.py", src)
    assert ("?.run", "call", "") in refs_of(f)
    v = _vh(f, "a.py")
    assert alive(v, "a.py#A.run") and v["a.py#A.spare"] == "dead"


# ====================================================================== second pass 4: string sinks, loaded module files, inherited host roots, namespaces
def test_pwsh_a_string_handed_to_a_sink_names_the_command_it_runs(worker):
    """Review round 2, blocker 1: `iex "Lit-Arg -n 1"`, `[scriptblock]::Create("Via-Sb 1")`, `powershell -Command "Via-Cmdstr -x 1"` and
    `Start-Process pwsh -ArgumentList "-NoProfile -Command Via-Jobarg"` were neither a ref nor a dyn site: false dead on the command they name.
    """
    p = [1, 1, 1, 30]
    f = ps(
        worker,
        [
            ["cmd", "Invoke-Expression", *p, "call", [["Do-$action"]], [[]]],
            ["cmd", "iex", *p, "call", [["Lit-Arg -n 1"]], [[]]],
            ["cmd", "Start-Process", *p, "call", [["pwsh", "-ArgumentList", "-NoProfile -Command Via-Jobarg"]], [[]]],
            ["cmd", "powershell.exe", *p, "call", [["-Command", "Via-Cmdstr -x 1"]], [[]]],
            ["member", "Create", *p, True, "scriptblock", True, [["Via-Sb 1"]], [[]]],
            ["member", "InvokeScript", *p, False, "", True, [["Foo-Bar; Second-Cmd | Third-Cmd"]], [[]]],
            ["cmd", "pwsh", *p, "call", [["-File", "./tool.ps1", "-Quiet"]], [[]]],
            ["cmd", "iex", *p, "call", [[]], [["$cmd"]]],
            ["cmd", "Write-Host", *p, "call", [["Not-A-Call -x 1"]], [[]]],
            ["member", "Run", *p, False, "$o", True, [["Run-Me"]], [[]]],
        ],
    )
    r = refs_of(f)
    assert {
        ("lit-arg", "call", ""),
        ("via-sb", "call", ""),
        ("foo-bar", "call", ""),
        ("second-cmd", "call", ""),
        ("third-cmd", "call", ""),
        ("via-jobarg", "ref", ""),
        ("via-cmdstr", "ref", ""),
        ("tool.ps1", "exec", ""),
    } <= r
    assert sorted(d.pattern for d in f.dyn) == ["$cmd", "name:do-*"]
    assert not any(
        n in ("not-a-call", "run-me", "-x", "-quiet") for n, _, _ in r
    )  # a string no sink evaluates, a parameter


def test_pwsh_sink_words_follow_the_mode_of_the_data():
    assert pw._sink("Do-$action", "command") == ([], ["name:do-*"])
    assert pw._sink("$cmd -x 1", "command") == (
        [],
        ["$cmd -x 1"],
    )  # the command itself is computed: the whole string is the site
    assert pw._sink("$cmd -x 1", "words") == ([], [])
    assert pw._sink("a; b -x | c && d", "command")[0] == [(w, "call") for w in "abcd"]
    assert pw._sink("& { Foo-Bar $x }", "command")[0] == [("foo-bar", "call")]
    assert pw._sink("-NoProfile -ExecutionPolicy Bypass -File x.ps1", "words")[0] == [
        ("bypass", "ref"),
        ("x.ps1", "exec"),
    ]
    assert pw._sink("Get-[1]x", "command") == ([], [])  # not a name


def test_pwsh_sink_rules_are_data_of_the_extractor(worker, monkeypatch):
    rec = [["cmd", "iex", 1, 1, 1, 9, "call", [["Lit-Arg -n 1"]], [[]]]]
    assert ("lit-arg", "call", "") in refs_of(ps(worker, rec))
    monkeypatch.setattr(pw, "_rules", lambda: {})
    assert ("lit-arg", "call", "") not in refs_of(ps(worker, rec))


SINK_SRC = """function Do-Build { 1 }
function Lit-Arg { 1 }
function Via-Jobarg { 1 }
function Via-Sb { 1 }
function Via-Cmdstr { 1 }
function Spare-Fn { 1 }
function Main {
  param($action, $x)
  Invoke-Expression "Do-$action"
  iex "Lit-Arg -n 1"
  [scriptblock]::Create("Via-Sb 1").Invoke()
  Start-Process pwsh -ArgumentList "-NoProfile -Command Via-Jobarg"
  powershell -Command "Via-Cmdstr -x 1"
}
Main
"""


@needs_pwsh
def test_pwsh_real_commands_named_by_a_sink_string_are_not_dead():
    pw.set_worker(None)
    v = _vh(contracts.get_extractor("powershell").extract("powershell", "a.ps1", SINK_SRC))
    assert alive(v, *(f"a.ps1#{n}" for n in ("do-build", "lit-arg", "via-jobarg", "via-sb", "via-cmdstr", "main")))
    assert v["a.ps1#spare-fn"] == "dead"  # the control: a function nothing names stays dead


@needs_pwsh
def test_pwsh_real_a_computed_script_string_is_a_dyn_site_of_the_file():
    pw.set_worker(None)
    src = "function Hidden { 1 }\nfunction Other { 2 }\niex $cmd\n"
    f = contracts.get_extractor("powershell").extract("powershell", "a.ps1", src)
    assert [d.pattern for d in f.dyn] == ["$cmd"]
    assert alive(_vh(f), "a.ps1#hidden", "a.ps1#other")


@pytest.mark.parametrize(
    "args,want",
    [
        (["-Path", "$PSScriptRoot\\Public\\*.ps1", "-ErrorAction", "SilentlyContinue"], "Public/*.ps1"),
        (["-Path", "Join-Path", "Public", "-Filter", "*.ps1", "-Recurse"], "Public/**/*.ps1"),
        (["$PSScriptRoot/Private", "-Filter", "*.ps1"], "Private/*.ps1"),
        (["-Recurse", "$PSScriptRoot\\Lib"], "Lib/**/*"),
        (["$PSScriptRoot", "-Filter", "*.psm1"], "*.psm1"),
        (["-Path", "$base\\Public\\*.ps1"], "Public/*.ps1"),
        (["-Path", "Split-Path", "-Filter", "*.ps1"], "*.ps1"),
        (["*.log"], ""),
        (["-Path", "$PSScriptRoot\\data", "-Filter", "*.json"], ""),
    ],
)
def test_pwsh_the_include_glob_of_a_get_childitem(args, want):
    assert pw._glob(pw._rules()["globs"], args) == want


def test_pwsh_an_enumeration_is_an_include_glob_only_in_a_file_that_loads_a_computed_path(worker):
    gci = ["cmd", "Get-ChildItem", 1, 1, 1, 40, "call", [["-Path", "$PSScriptRoot\\Public\\*.ps1"]], [[]]]
    loop = ["dyn", "$import.FullName", 3, 1, 3, 20, "dot", "expr", [[[]]]]
    assert ("Public/*.ps1", "include", "") in refs_of(ps(worker, [gci, loop]))
    assert not refs_of(ps(worker, [gci]), "include")  # the control: a script that only lists files
    imp = ["cmd", "Import-Module", 3, 1, 3, 30, "call", [["FullName"]], [["$_.FullName"]]]
    assert ("Public/*.ps1", "include", "") in refs_of(ps(worker, [gci, imp]))
    pipe = ["cmd", "Import-Module", 3, 1, 3, 30, "call", [[]], [[]]]  # `gci ... | Import-Module`
    assert ("Public/*.ps1", "include", "") in refs_of(ps(worker, [gci, pipe]))
    logs = ["cmd", "gci", 1, 1, 1, 20, "call", [["*.log"]], [[]]]
    assert not refs_of(ps(worker, [logs, loop]), "include")  # a pattern that names no script


def _glob_files(includer, ref, *others):
    def one(path, refs=()):
        return path, model.Facts("powershell", (module_def("x\n"),), tuple(refs))

    return dict([one(includer, [model.Ref(ref, "include", 1, "")])] + [one(o) for o in others])


def _included(files, includer):
    res = dlink.link(files, workers=1)
    return sorted(e.dst.split(":", 1)[1] for e in res.graph.edges if e.kind == "include" and e.src.endswith(includer))


def test_link_an_include_glob_names_every_file_it_matches_relative_to_the_includer():
    files = _glob_files(
        "Mod/Mod.psm1",
        "Public/*.ps1",
        "Mod/Public/A.ps1",
        "Mod/Public/B.ps1",
        "Mod/Public/sub/C.ps1",
        "Other/Public/D.ps1",
    )
    assert _included(files, "Mod.psm1") == [
        "Mod/Public/A.ps1",
        "Mod/Public/B.ps1",
    ]  # one directory; the near files win over a suffix match
    files = _glob_files("Mod/Mod.psm1", "Public/**/*.ps1", "Mod/Public/A.ps1", "Mod/Public/sub/C.ps1", "Mod/Priv/X.ps1")
    assert _included(files, "Mod.psm1") == ["Mod/Public/A.ps1", "Mod/Public/sub/C.ps1"]
    files = _glob_files("build.ps1", "Lib/*.ps1", "tools/Lib/x.ps1", "tools/Lib/y.psm1")
    assert _included(files, "build.ps1") == ["tools/Lib/x.ps1"]  # nothing near: the files that end with the pattern
    assert _included(_glob_files("a.ps1", "*.ps1", "b.ps1"), "a.ps1") == [
        "b.ps1"
    ]  # a glob that matches the includer is not an include of itself


MOD_FILES = {
    "run.ps1": "Import-Module $PSScriptRoot/Mod/Mod.psd1\nGet-A\n",
    "Mod/Mod.psd1": "@{ RootModule = 'Mod.psm1'; FunctionsToExport = @('Get-A', 'Get-B') }\n",
    "Mod/Mod.psm1": "\n".join(
        [
            "$public  = @(Get-ChildItem -Path $PSScriptRoot\\Public\\*.ps1 -ErrorAction SilentlyContinue)",
            "$private = @(Get-ChildItem -Path $PSScriptRoot\\Private\\*.ps1 -ErrorAction SilentlyContinue)",
            "foreach ($import in @($public + $private)) { . $import.FullName }",
            "Export-ModuleMember -Function $public.Basename",
            "",
        ]
    ),
    "Mod/Public/Get-A.ps1": "function Get-A { Priv-Helper }\n",
    "Mod/Public/Get-B.ps1": "function Get-B { 1 }\n",
    "Mod/Private/Priv-Helper.ps1": "function Priv-Helper { 1 }\n",
    "Mod/Public/Deep/Get-C.ps1": "function Get-C { 1 }\n",
    "Mod/Other/Stray.ps1": "function Stray { 1 }\n",
}


@needs_pwsh
def test_pwsh_real_a_module_that_dot_sources_its_public_and_private_folders_reaches_every_file_of_them():
    """Review round 2, blocker 2: the classic layout reported its loaded files dead, while their functions were weak-alive."""
    pw.set_worker(None)
    ex = contracts.get_extractor("powershell")
    files = {p: ex.extract("powershell", p, t) for p, t in MOD_FILES.items()}
    v = verdicts(files, {"run.ps1": [model.RootHint("main", "file:run.ps1", 1, "hard", "convention")]})
    loaded = ("Mod/Mod.psm1", "Mod/Public/Get-A.ps1", "Mod/Public/Get-B.ps1", "Mod/Private/Priv-Helper.ps1")
    assert alive(v, *loaded, *(f"{p}#{n}" for p, n in zip(loaded[1:], ("get-a", "get-b", "priv-helper"))))
    assert v["Mod/Public/Get-A.ps1"] == "alive"  # an include of every match, not a guess
    assert (
        v["Mod/Other/Stray.ps1"] == "dead" and v["Mod/Public/Deep/Get-C.ps1"] == "dead"
    )  # the controls: outside the globs


HOST_BASE_SRC = "public class Base : ControllerBase { }\n"
HOST_DERIVED_SRC = """public class Orders : Base { public IActionResult Get() { return null; } }
public class Free { public void Orphan() { } }
"""


def host_files():
    base = scripted(
        "csharp",
        HOST_BASE_SRC,
        defs=[("class", HOST_BASE_SRC.strip(), "Base", "public")],
        refs=[{"ref.inherit": ": ControllerBase", "name": "ControllerBase"}],
        roots=[{"root.annotation.dotnet-host": "ControllerBase"}],
    )
    lines = HOST_DERIVED_SRC.splitlines()
    derived = scripted(
        "csharp",
        HOST_DERIVED_SRC,
        defs=[
            ("class", lines[0], "Orders", "public"),
            ("func", "public IActionResult Get() { return null; }", "Get", "public"),
            ("class", lines[1], "Free", "public"),
            ("func", "public void Orphan() { }", "Orphan", "public"),
        ],
        refs=[{"ref.inherit": ": Base", "name": "Base"}],
    )
    return {"A.cs": base, "B.cs": derived}


def test_csharp_a_class_derived_from_a_host_root_class_is_a_root_too_and_so_are_its_members():
    """Review round 2, blocker 3: `class Base : ControllerBase` plus `class Orders : Base { Get() }` gave Base alive, Orders and Orders.Get dead."""
    v = verdicts(host_files())
    assert alive(v, "A.cs#Base", "B.cs#Orders", "B.cs#Orders.Get")
    assert v["B.cs#Free"] in ("dead", "dead-cluster") and v["B.cs#Free.Orphan"] in (
        "dead",
        "dead-cluster",
    )  # the control
    res = dlink.link(host_files(), workers=1)
    heirs = [r for r in res.graph.roots if r.node.endswith("B.cs#Orders")]
    assert [(r.kind, r.origin, r.strength) for r in heirs] == [("annotation:dotnet-host", "A.cs:1", "hard")]
    assert not res.findings


def test_csharp_every_level_of_a_hierarchy_below_a_host_base_is_rooted_class_and_interface_alike():
    unity = (
        "public class Character : MonoBehaviour { }\n"
        "public class Enemy : Character { void Update() { } }\n"
        "public class Boss : Enemy { void Update() { } }\n"
        "public interface IJob : IHostedService { }\n"
        "public class Job : IJob { public void Run() { } }\n"
    )
    ln = unity.splitlines()
    f = scripted(
        "csharp",
        unity,
        defs=[
            ("class", ln[0], "Character"),
            ("class", ln[1], "Enemy"),
            ("func", "void Update() { }", "Update"),
            ("class", ln[2], "Boss"),
            ("func", "void Update() { }", ("Update", 1)[0]),
            ("iface", ln[3], "IJob"),
            ("class", ln[4], "Job"),
            ("func", "public void Run() { }", "Run", "public"),
        ],
        refs=[
            {"ref.inherit": ": MonoBehaviour", "name": "MonoBehaviour"},
            {"ref.inherit": ": Character", "name": "Character"},
            {"ref.inherit": ": Enemy", "name": "Enemy"},
            {"ref.inherit": ": IHostedService", "name": "IHostedService"},
            {"ref.inherit": ": IJob", "name": "IJob"},
        ],
        roots=[
            {"root.annotation.dotnet-host": "MonoBehaviour"},
            {"root.annotation.dotnet-host": "IHostedService"},
        ],
    )
    v = _v(f)
    assert alive(v, "A.cs#Character", "A.cs#Enemy", "A.cs#Boss", "A.cs#IJob", "A.cs#Job", "A.cs#Job.Run")


def test_csharp_a_class_derived_from_a_plain_class_is_not_rooted_by_inheritance():
    src = "public class Plain : Foo { }\npublic class Sub : Plain { public void Run() { } }\n"
    ln = src.splitlines()
    f = scripted(
        "csharp",
        src,
        defs=[("class", ln[0], "Plain"), ("class", ln[1], "Sub"), ("func", "public void Run() { }", "Run", "public")],
        refs=[{"ref.inherit": ": Foo", "name": "Foo"}, {"ref.inherit": ": Plain", "name": "Plain"}],
    )
    v = _v(f)
    assert {v["A.cs#Plain"], v["A.cs#Sub"]} <= {"dead", "dead-cluster"}


def test_inherited_roots_are_a_flag_of_the_language_data_and_do_not_depend_on_the_worker_count():
    off = json.loads(Path(dclangs.DATA).read_text())
    for spec in off["langs"].values():
        spec.pop("root_inherit", None)
    res = dlink.link(host_files(), workers=1, langs=dclangs.Langs(off))
    assert rc.analyze(res.graph).verdicts()["csharp:B.cs#Orders"] in ("dead", "dead-cluster")  # the control
    assert {k: s["root_inherit"] for k, s in dclangs.load().langs.items() if s.get("root_inherit")} == {
        "csharp": ["annotation:dotnet-host"]
    }
    assert len({dlink.link(host_files(), workers=w).graph.digest() for w in (1, 2, 8)}) == 1


def test_namespaces_are_a_flag_of_the_language_data():
    assert {k for k, s in dclangs.load().langs.items() if s.get("namespaceless")} == {"cpp", "csharp"}


NS_HDR = "namespace util { int helper() { return 1; } int other() { return 2; } }\n"
NS_MAIN = '#include "u.hpp"\nint main() { return util::helper(); }\n'


def ns_files():
    """The ref of the call is `util.helper` ONLY: no bare `helper` that a weak capture of the grammar would add."""
    hdr = scripted(
        "cpp",
        NS_HDR,
        defs=[("func", "int helper() { return 1; }", "helper"), ("func", "int other() { return 2; }", "other")],
    )
    main = scripted(
        "cpp",
        NS_MAIN,
        defs=[("func", "int main() { return util::helper(); }", "main")],
        refs=[{"ref.include": '"u.hpp"', "name": '"u.hpp"'}, {"ref.call": "util::helper", "name": "util::helper"}],
        roots=[{"root.main": "main"}],
    )
    return {"u.hpp": hdr, "m.cpp": main}


def test_cpp_a_name_qualified_by_a_namespace_resolves_by_its_last_part():
    """Review round 2, minor: with the hand-placed ref `util.helper` the function of the namespace in the header came out dead;
    the verdict depended on the weak `(identifier)` capture inside the qualified name."""
    v = verdicts(ns_files())
    assert alive(v, "u.hpp#helper") and v["u.hpp#other"] in ("dead", "dead-cluster")


def test_cpp_a_namespace_less_resolution_is_a_flag_of_the_language_data():
    off = json.loads(Path(dclangs.DATA).read_text())
    for spec in off["langs"].values():
        spec.pop("namespaceless", None)
    res = dlink.link(ns_files(), workers=1, langs=dclangs.Langs(off))
    assert rc.analyze(res.graph).verdicts()["cpp:u.hpp#helper"] in ("dead", "dead-cluster")  # the control
    py = contracts.get_extractor("python").extract(
        "python", "a.py", "def helper():\n    return 1\n\n\ndef main():\n    return obj.helper()\n"
    )
    assert (
        _vh(py, "a.py")["a.py#helper"] == "dead"
    )  # python: `obj.helper` is a member of obj, never the module function


def test_csharp_a_type_named_by_a_qualified_name_alone_is_used():
    src = "public class Util { }\nclass Prog { static void Main() { var t = typeof(Ns.Util); } }\n"
    ln = src.splitlines()
    f = scripted(
        "csharp",
        src,
        defs=[
            ("class", ln[0], "Util", "public"),
            ("class", ln[1], "Prog"),
            ("func", "static void Main() { var t = typeof(Ns.Util); }", "Main", "static"),
        ],
        refs=[{"ref.ref": "Ns.Util", "name": "Ns.Util"}],
        roots=[{"root.main": "Main"}],
    )
    assert alive(_v(f), "A.cs#Util")


def test_pwsh_a_string_that_names_a_script_is_an_include_only_in_a_file_that_loads_a_computed_path(worker):
    named = [
        ["str", "lib\\x.ps1", 1, 1, 1, 20],
        ["str", "$base\\y.psm1", 2, 1, 2, 20],
        ["str", "Invoke-Build", 4, 1, 4, 9],
    ]
    loop = ["dyn", "$p", 3, 1, 3, 5, "dot", "var", [[[]]]]
    assert {("lib/x.ps1", "include", ""), ("y.psm1", "include", ""), ("invoke-build", "ref", "")} <= refs_of(
        ps(worker, [*named, loop])
    )
    assert not refs_of(ps(worker, named), "include") and ("invoke-build", "ref", "") not in refs_of(
        ps(worker, named)
    )  # the control: a script that only mentions them


def test_pwsh_a_wildcard_given_to_get_command_is_a_name_glob(worker):
    f = ps(worker, [["cmd", "Get-Command", 1, 1, 1, 30, "call", [["Test-*", "-CommandType", "Function"]], [[]]]])
    assert [d.pattern for d in f.dyn] == ["name:test-*"]


@needs_pwsh
def test_pwsh_real_files_named_by_strings_of_a_loading_script_and_functions_found_by_a_pattern_are_not_dead():
    pw.set_worker(None)
    ex = contracts.get_extractor("powershell")
    src = {
        "main.ps1": "\n".join(
            [
                "$p = Join-Path $PSScriptRoot 'lib/x.ps1'",
                ". $p",
                "foreach ($f in @('a.ps1', 'b.ps1')) { . (Join-Path $PSScriptRoot $f) }",
                "Get-Command 'Test-*' | ForEach-Object { & $_ }",
                "$map = @{ build = 'Invoke-Build' }; & $map[$args[0]]",
                "function Test-One { 1 }",
                "function Spare { 2 }",
                "",
            ]
        ),
        "lib/x.ps1": "function X { 1 }\n",
        "lib/build.ps1": "function Invoke-Build { 1 }\n",
        "a.ps1": "function A { 1 }\n",
        "b.ps1": "function B { 1 }\n",
        "other.ps1": "function Other { 1 }\n",
    }
    files = {p: ex.extract("powershell", p, t) for p, t in src.items()}
    v = verdicts(files, {"main.ps1": [model.RootHint("main", "file:main.ps1", 1, "hard", "convention")]})
    assert alive(v, "lib/x.ps1", "a.ps1", "b.ps1", "main.ps1#test-one", "lib/build.ps1#invoke-build")
    assert v["other.ps1"] == "dead"  # the control: a file nobody names


PART_A = "public partial class Orders : ControllerBase { }\n"
PART_B = "public partial class Orders { public IActionResult Get() { return null; } public void Spare() { } }\n"


def partial_files(swap=False):
    a = scripted(
        "csharp",
        PART_A,
        defs=[("class", PART_A.strip(), "Orders", "public")],
        refs=[{"ref.inherit": ": ControllerBase", "name": "ControllerBase"}],
        roots=[{"root.annotation.dotnet-host": "ControllerBase"}],
    )
    b = scripted(
        "csharp",
        PART_B,
        defs=[
            ("class", PART_B.strip(), "Orders", "public"),
            ("func", "public IActionResult Get() { return null; }", "Get", "public"),
            ("func", "public void Spare() { }", "Spare", "public"),
        ],
    )
    return {"B.cs": b, "A.cs": a} if swap else {"A.cs": a, "B.cs": b}


@pytest.mark.parametrize("swap", [False, True])
def test_csharp_the_other_part_of_a_partial_host_class_is_rooted_and_has_the_external_base(swap):
    """The part with the base list is rooted; the part with the actions has no base list of its own: both are one class."""
    v = verdicts(partial_files(swap))
    assert alive(v, "A.cs#Orders", "B.cs#Orders", "B.cs#Orders.Get")


def test_csharp_a_class_derived_from_a_partial_host_class_is_rooted_too():
    derived = scripted(
        "csharp",
        "public class Sub : Orders { public void Run() { } }\n",
        defs=[
            ("class", "public class Sub : Orders { public void Run() { } }", "Sub", "public"),
            ("func", "public void Run() { }", "Run", "public"),
        ],
        refs=[{"ref.inherit": ": Orders", "name": "Orders"}],
    )
    v = verdicts({**partial_files(), "C.cs": derived})
    assert alive(v, "C.cs#Sub", "C.cs#Sub.Run")


def test_csharp_a_partial_class_without_a_host_base_stays_dead_with_its_members():
    files = partial_files()
    a = scripted(
        "csharp",
        PART_A,
        defs=[("class", PART_A.strip(), "Orders", "public")],
        refs=[{"ref.inherit": ": ControllerBase", "name": "ControllerBase"}],
    )  # the control: the same base list, no root of the host
    v = verdicts({"A.cs": a, "B.cs": files["B.cs"]})
    assert v["B.cs#Orders"] in ("dead", "dead-cluster")


# ====================================================================== counterexamples of an earlier re-check
CS_MW = (
    "public class Mw {\n  private readonly RequestDelegate _next;\n  public Mw(RequestDelegate next) { _next = next; }\n"
    "  public async Task InvokeAsync(HttpContext c) { Helper(); await _next(c); }\n  private void Helper() {}\n  private void Spare() {}\n}\n"
)
CS_START = {
    "generic": "app.UseMiddleware<Mw>();",
    "qualified": "app.UseMiddleware<Acme.Web.Mw>(1, 2);",
    "typeof": "app.UseMiddleware(typeof(Mw));",
}


def _cs_verdicts(files, entry="Program.cs"):
    if _grammar_reason("csharp"):
        pytest.skip(f"tree-sitter grammar csharp is not installed on this host: {_grammar_reason('csharp')}")
    tg.set_backend(None)
    facts = {p: contracts.get_extractor("csharp").extract("csharp", p, t) for p, t in files.items()}
    return verdicts(facts, {entry: [model.RootHint("main", f"file:{entry}", 1, "hard", "convention")]}), facts


@pytest.mark.parametrize("case", sorted(CS_START))
def test_csharp_convention_middleware_registered_by_use_middleware_keeps_invoke(case):
    """A class with no base that has InvokeAsync / Invoke is built and called by the host; the other members stay judged by their calls."""
    prog = "class Program {\n  static void Main() { var app = Build(); %s }\n}\n" % CS_START[case]
    v, facts = _cs_verdicts({"Program.cs": prog, "Mw.cs": CS_MW})
    assert [d.pattern for d in facts["Program.cs"].dyn] == ["members:Mw/Invoke*"]
    assert alive(v, "Mw.cs#Mw", "Mw.cs#Mw.InvokeAsync", "Mw.cs#Mw.Helper", "Mw.cs#Mw.Mw")
    assert v["Mw.cs#Mw.Spare"] in ("dead", "dead-cluster")  # not every member: only the conventional entry points


def test_csharp_a_live_type_named_middleware_keeps_invoke_and_invoke_async_whatever_registers_it():
    """`Register(typeof(AuthMiddleware))` is a use of the type, not of Invoke: the host calls that by convention (protocol member)."""
    mw = CS_MW.replace("Mw", "AuthMiddleware").replace("InvokeAsync", "Invoke")
    v, _ = _cs_verdicts(
        {"Program.cs": "class Program { static void Main() { Register(typeof(AuthMiddleware)); } }\n", "Mw.cs": mw}
    )
    assert alive(v, "Mw.cs#AuthMiddleware", "Mw.cs#AuthMiddleware.Invoke")
    assert v["Mw.cs#AuthMiddleware.Spare"] in ("dead", "dead-cluster")
    other = mw.replace("AuthMiddleware", "Handler")  # the control: a type not named *Middleware and not registered
    v, _ = _cs_verdicts(
        {"Program.cs": "class Program { static void Main() { Register(typeof(Handler)); } }\n", "Mw.cs": other}
    )
    assert alive(v, "Mw.cs#Handler") and v["Mw.cs#Handler.Invoke"] in ("dead", "dead-cluster")


def test_csharp_use_middleware_with_a_name_nothing_defines_adds_no_edge():
    v, facts = _cs_verdicts(
        {
            "Program.cs": "class Program { static void Main() { app.UseMiddleware<External.Mw>(); app.UseCors(); } }\n",
            "Mw.cs": CS_MW.replace("Mw", "Mine"),
        }
    )
    assert [d.pattern for d in facts["Program.cs"].dyn] == ["members:Mw/Invoke*"]
    assert v["Mw.cs#Mine.InvokeAsync"] in ("dead", "dead-cluster")  # another type of the same shape is not registered


PS_PSM1 = (
    "$Public = @(Get-ChildItem -Path $PSScriptRoot\\Public\\*.ps1 -ErrorAction SilentlyContinue)\n"
    "foreach ($f in $Public) { . $f.FullName }\n"
    "Export-ModuleMember -Function $Public.Basename\n"
)
PS_TOOLS = {
    "Tools/Tools.psm1": PS_PSM1,
    "Tools/Tools.psd1": "@{ RootModule = 'Tools.psm1'; FunctionsToExport = @('Get-Thing') }\n",
    "Tools/Public/Get-Thing.ps1": "function Get-Thing { 1 }\n",
    "Tools/Public/Set-Thing.ps1": "function Set-Thing { 1 }\n",
    "Tools/Other/Stray.ps1": "function Stray { 1 }\n",
}


def _ps_verdicts(files, entry):
    pw.set_worker(None)
    ex = contracts.get_extractor("powershell")
    facts = {p: ex.extract("powershell", p, t) for p, t in files.items()}
    return verdicts(facts, {entry: [model.RootHint("main", f"file:{entry}", 1, "hard", "convention")]}), facts


@needs_pwsh
def test_pwsh_real_a_computed_export_list_exports_the_functions_of_the_loaded_files():
    """Counterexample: `Export-ModuleMember -Function $Public.Basename` gave no root and no site, Set-Thing was dead."""
    files = {**PS_TOOLS, "run.ps1": "Import-Module $PSScriptRoot/Tools/Tools.psm1\nGet-Thing\n"}
    v, facts = _ps_verdicts(files, "run.ps1")
    assert "funcs:Public/*.ps1" in [d.pattern for d in facts["Tools/Tools.psm1"].dyn]
    assert alive(v, "Tools/Public/Get-Thing.ps1#get-thing", "Tools/Public/Set-Thing.ps1#set-thing")
    assert v["Tools/Other/Stray.ps1#stray"] == "dead"  # outside the glob of the loop
    only = {k: x for k, x in files.items() if k != "run.ps1"}  # the module as the root itself (no consumer in the tree)
    v, _ = _ps_verdicts(only, "Tools/Tools.psm1")
    assert alive(v, "Tools/Public/Get-Thing.ps1#get-thing", "Tools/Public/Set-Thing.ps1#set-thing")


@needs_pwsh
def test_pwsh_real_a_computed_export_list_in_a_file_with_no_loop_exports_its_own_functions():
    own = "function Get-A { 1 }\nfunction Get-B { 1 }\n$names = @('Get-A', 'Get-B')\nExport-ModuleMember -Function $names\n"
    v, facts = _ps_verdicts({"m.psm1": own, "run.ps1": "Import-Module ./m.psm1\n"}, "run.ps1")
    assert not facts["m.psm1"].dyn and alive(v, "m.psm1#get-a", "m.psm1#get-b")  # like `-Function *`
    assert {d.vis for d in facts["m.psm1"].defs if d.qual in ("get-a", "get-b")} == {"exported"}
    literal = "function Get-A { 1 }\nfunction Get-B { 1 }\nExport-ModuleMember -Function Get-A\n"
    v, _ = _ps_verdicts({"m.psm1": literal, "run.ps1": "Import-Module ./m.psm1\n"}, "run.ps1")
    assert alive(v, "m.psm1#get-a") and v["m.psm1#get-b"] in ("dead", "dead-cluster")  # a literal list is still exact


PS_IMPORTS = {
    "name": "Import-Module Tools\nGet-Thing\n",
    "name-parameter": "Import-Module -Name Tools -Force\nGet-Thing\n",
    "using-module": "using module Tools\nGet-Thing\n",
    "directory": "Import-Module -Name $PSScriptRoot\\Tools\nGet-Thing\n",
    "directory-slash": "Import-Module $PSScriptRoot/Tools\nGet-Thing\n",
}


@needs_pwsh
@pytest.mark.parametrize("case", sorted(PS_IMPORTS))
def test_pwsh_real_a_module_imported_by_name_or_directory_reaches_its_manifest(case):
    """Counterexample: `Import-Module Tools` linked to nothing, so Tools.psd1 was dead (a deletion advice) and so was the module."""
    v, _ = _ps_verdicts({**PS_TOOLS, "run.ps1": PS_IMPORTS[case]}, "run.ps1")
    assert v["Tools/Tools.psd1"] == "alive" and v["Tools/Tools.psm1"] == "alive"
    assert alive(v, "Tools/Public/Get-Thing.ps1#get-thing", "Tools/Public/Set-Thing.ps1#set-thing")
    assert v["Tools/Other/Stray.ps1"] == "dead"


@needs_pwsh
def test_pwsh_real_a_module_name_nothing_in_the_tree_answers_adds_nothing():
    v, facts = _ps_verdicts(
        {
            **PS_TOOLS,
            "run.ps1": "Import-Module ActiveDirectory\nImport-Module Microsoft.PowerShell.Utility\nGet-Thing\n",
        },
        "run.ps1",
    )
    assert v["Tools/Tools.psd1"] in ("dead", "dead-cluster") and v["Tools/Tools.psm1"] in ("dead", "dead-cluster")
    assert {r.name for r in facts["run.ps1"].refs if r.kind == "include"} >= {
        "ActiveDirectory.psd1",
        "ActiveDirectory/ActiveDirectory.psm1",
    }
