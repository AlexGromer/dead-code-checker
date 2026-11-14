"""Java and Kotlin: the java/kotlin queries as data, the lang rules (data/frameworks.json: annotation roots of Spring and JUnit, main,
visibility, typed receivers, injection points), the facts mapping through a FAKE parser backend, and the linker side (package scope,
class hierarchy edges, di edges to every bean of a type) on the golden Spring fixture.

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_jvm.py

The fake parser (tests/fake_jvm.py) answers the capture vocabulary of the .scm files; it proves the MAPPING, not that a .scm query
matches what it is meant to on a real grammar. The real-grammar tests skip, with their reason, when tree-sitter or the grammar is not
installed.
"""

import json
import random
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test

ROOT = Path(__file__).resolve().parent.parent
from dead_code_checker.graph import classreg, contracts, link as dlink, model as md, reach, roots as droots, ts as tsm  # noqa: E402
from dead_code_checker.graph.extract import lang_rules as lr, ts_generic as tg  # noqa: E402
from fake_jvm import FNode, FakeJava, FakeKotlin  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "jvm"
SPRING = FIX / "spring"
RL = lr.get("java")
KL = lr.get("kotlin")


@pytest.fixture(autouse=True)
def _backend():
    tg.set_backend(lambda grammar, data: FakeJava(data.decode()))
    yield
    tg.set_backend(None)


@pytest.fixture(autouse=True)
def _roots_registered():
    """Other test files reset the registries: the root extractors of graph.roots (and dc.classreg) must be registered again."""
    import importlib

    if "classreg" not in {r.name for r in contracts.root_extractors()}:
        importlib.reload(droots)


def extract(lang, rel, text):
    return contracts.get_extractor(lang).extract(lang, rel, text)


@pytest.fixture(scope="module")
def spring_files():
    tg.set_backend(lambda grammar, data: FakeJava(data.decode()))
    try:
        return {
            str(p.relative_to(SPRING)): extract("java", str(p.relative_to(SPRING)), p.read_text())
            for p in sorted(SPRING.rglob("*.java"))
        }
    finally:
        tg.set_backend(None)


@pytest.fixture(scope="module")
def spring(spring_files):
    res = dlink.link(spring_files, workers=1)
    return res, reach.analyze(res.graph)


def short(key):
    return key.split("#", 1)[1] if "#" in key else key


def verdicts(spring):
    res, an = spring
    return {short(n.key): an.verdict(n.key) for n in res.graph.nodes if n.kind != "file"}


def edges(spring, kind):
    return {(short(e.src), short(e.dst), e.tier) for e in spring[0].graph.edges if e.kind == kind}


# ====================================================================== the data
def test_data_is_well_formed():
    d = json.loads(Path(lr.DATA).read_text(encoding="utf-8"))
    rules = [r for f in d["frameworks"].values() for r in f["rules"]]
    assert len({r["id"] for r in rules}) == len(rules)  # one id per rule: it is the root kind annotation:<id>
    for r in rules:
        assert r["on"] in ("class", "method", "param") and (r.get("ann") or r.get("mod")) and r["why"]
        assert r.get("targets", "string") in ("string", "member") and not set(r) - {
            "id",
            "on",
            "ann",
            "mod",
            "why",
            "kind",
            "bean",
            "accessors",
            "targets",
            "attrs",
            "ref_only",
            "schema",
        }
        assert not r.get("attrs") or r.get("targets")  # attrs narrows the strings of a rule that reads strings
        if r.get(
            "schema"
        ):  # a rule that names schema objects (graph.extract.lang_rules._schema_refs): refs only, a known group
            assert (
                r.get("ref_only")
                and r["schema"]["group"] in ("sql", "graphql")
                and not set(r["schema"]) - {"group", "attrs", "type", "type_attrs", "self", "snake", "unless"}
            )
        assert r.get("kind", "annotation:x") in md.ROOT_KINDS or r.get("kind", "annotation:x").startswith("annotation:")
    for lang, spec in d["lang_rules"].items():
        if lang.startswith("_"):
            continue
        assert set(spec["frameworks"]) <= set(d["frameworks"]) and "m" in re.compile(spec["import"]).groupindex
        assert set(spec["flags"].values()) <= set(md.FLAG_NAMES) and set(spec["vis"].values()) <= set(md.VIS)
        assert set(spec["main"]) == {"names", "mods", "toplevel", "ann"} and spec["di"]["inject"]
    assert lr.bean_kinds() == {
        "annotation:spring.controller",
        "annotation:spring.bean",
        "annotation:spring.properties",
        "annotation:jakarta.cdi-bean",
        "annotation:jakarta.faces-bean",
        "annotation:micronaut.bean",
        "annotation:android.hilt",
    }


def test_spring_rules_cover_the_design_table():
    ann = {a for f in json.loads(Path(lr.DATA).read_text())["frameworks"]["spring"]["rules"] for a in f["ann"]}
    need = {
        "SpringBootApplication",
        "RestController",
        "Controller",
        "ControllerAdvice",
        "RequestMapping",
        "GetMapping",
        "PostMapping",
        "PutMapping",
        "DeleteMapping",
        "PatchMapping",
        "Bean",
        "Component",
        "Service",
        "Repository",
        "Configuration",
        "Scheduled",
        "KafkaListener",
        "RabbitListener",
        "JmsListener",
        "EventListener",
        "TransactionalEventListener",
        "PostConstruct",
        "PreDestroy",
        "ExceptionHandler",
        "Async",
    }
    assert need <= ann


def test_queries_exist_and_stay_in_the_vocabulary():
    vocab = {"name", "mods", "recv", "type", "import", "root.marker", "scope", "anon", "meta", "cmd"}
    prefixes = (
        "def.",
        "ref.",
        "var.",
        "dyn.",
    )  # dyn.<kind>: a reflected use (rules.json dyn), with the @cmd that filters it
    for g in ("java", "kotlin"):
        d = Path(tg.QUERY_DIR) / g
        want = {
            "defs",
            "refs",
            "types",
            "imports",
            "roots",
            "xlang",
        }  # xlang: the string literals (java also calls and native methods)
        assert {p.stem for p in d.glob("*.scm")} == want
        for p in (q for q in d.glob("*.scm") if q.stem != "xlang"):  # xlang.scm has its own vocabulary (test_graph_xlang)
            body = "\n".join(ln.split(";", 1)[0] for ln in p.read_text(encoding="utf-8").splitlines())
            caps = set(re.findall(r"@([\w.]+)", body))
            assert caps and all(c in vocab or c.startswith(prefixes) for c in caps), (p, caps)
            assert body.count("(") == body.count(")")
            for c in caps:
                if c.startswith("def."):
                    assert c[4:] in md.NODE_KINDS and p.stem == "defs"
                if c.startswith("ref."):
                    assert c[4:] in md.EDGE_KINDS and p.stem == "refs"
                if c.startswith("var."):
                    assert c[4:] in tg.VAR_KINDS and p.stem == "types"


def test_grammar_sha_follows_the_rules(monkeypatch, tmp_path):
    ex = contracts.get_extractor("java")
    base = ex.grammar_sha("java")
    assert base != ex.grammar_sha("kotlin") and re.fullmatch(r"[0-9a-f]{64}", base)
    d = json.loads(Path(lr.DATA).read_text(encoding="utf-8"))
    d["frameworks"]["spring"]["rules"][0]["ann"].append("MyBoot")
    alt = tmp_path / "frameworks.json"
    alt.write_text(json.dumps(d), encoding="utf-8")
    monkeypatch.setattr(lr, "DATA", str(alt))
    assert ex.grammar_sha("java") != base  # a changed rule re-extracts the shards
    assert contracts.get_extractor("shell").grammar_sha("shell") == contracts.get_extractor("shell").grammar_sha(
        "shell"
    )


# ====================================================================== lang_rules
def test_split_mods_ignores_strings_arguments_and_use_site_targets():
    words, anns = lr.split_mods('@GetMapping(value = "/a@b", produces = {"x"}) @org.x.Foo public static')
    assert words == ["public", "static"] and anns == ["GetMapping", "Foo"]
    assert lr.split_mods("@field:Autowired private lateinit") == (["private", "lateinit"], ["Autowired"])
    assert lr.split_mods("") == ([], [])


def test_def_facts_visibility_flags_and_roots():
    def f(kind, mods, qual="A.m", parent="class"):
        return lr.def_facts(
            RL, kind, qual, qual.rpartition(".")[2], mods, 7, parent
        )  # noqa: E731
    assert (
        f("method", "").vis == "package"
        and f("method", "private").vis == "private"
        and f("method", "protected").vis == "package"
    )
    assert f("method", "", parent="iface").vis == "public" and f("method", "private", parent="iface").vis == "private"
    assert (
        f("method", "@Override public").flags == md.F_OVERRIDE and f("method", "abstract public").flags == md.F_ABSTRACT
    )
    r = f("method", "@Scheduled(fixedRate = 1000) public")
    assert [(h.kind, h.target, h.strength, h.trust) for h in r.roots] == [
        ("annotation:spring.scheduled", "A.m", "hard", "convention")
    ]
    assert r.flags & md.F_DECORATED and r.rules == ("spring.scheduled",) and r.anns == ("Scheduled",)
    assert [h.kind for h in f("method", "@Test").roots] == ["test"]  # JUnit: a test root, not a production one
    assert (
        f("class", "@Service").roots[0].kind == "annotation:spring.bean" and not f("method", "@Service").roots
    )  # `on` is respected
    assert not f("method", "@Unknown public").roots and f("method", "@Unknown").flags == 0


def test_main_roots_java_and_kotlin():
    m = lr.def_facts(RL, "method", "App.main", "main", "public static", 3, "class")
    assert sorted((h.kind, h.target) for h in m.roots) == [
        ("main", "App"),
        ("main", "App.main"),
    ]  # the class that hosts main too
    assert not lr.def_facts(RL, "method", "App.main", "main", "public", 3, "class").roots  # not static
    assert not lr.def_facts(RL, "method", "App.run", "run", "public static", 3, "class").roots
    assert [(h.kind, h.target) for h in lr.def_facts(KL, "func", "main", "main", "", 1, "").roots] == [("main", "main")]
    assert not lr.def_facts(KL, "method", "Box.main", "main", "", 1, "class").roots  # Kotlin: top level only


@pytest.mark.parametrize(
    "text,wrap,want",
    [
        ("PriceService", False, "PriceService"),
        ("com.acme.PriceService", False, "com.acme.PriceService"),
        ("List<PriceService>", False, "List"),
        ("List<PriceService>", True, "PriceService"),
        ("Map<String, List<PriceService>>", True, "PriceService"),
        ("Optional<? extends Foo>", True, "Foo"),
        ("Provider<Foo>", True, "Foo"),
        ("Box<Foo>", True, "Box"),
        ("Foo[]", True, "Foo"),
        ("@Nullable Foo", False, "Foo"),
        ("Foo?", False, "Foo"),
        ("List<out Foo>", True, "Foo"),
        ("int", False, ""),
        ("var", False, ""),
        ("", False, ""),
        ("a b c", False, ""),
    ],
)
def test_type_name(text, wrap, want):
    assert lr.type_name(RL, text, wrap) == want


def fnd(v, scope):
    """A find() over a {(scope, name): type} table: the scope chain, innermost out (ts_generic does it over regions and positions)."""

    def find(name):
        sc = scope
        while True:
            if (sc, name) in v:
                return v[(sc, name)]
            if not sc:
                return None
            sc = sc.rpartition(".")[0]

    return find


def test_qualify_receivers():
    v = {("A.m", "svc"): "PriceService", ("A", "repo"): "Repo", ("", "top"): "Top"}
    q = lambda recv, scope="A.m": lr.qualify(RL, fnd(v, scope), recv, "go")  # noqa: E731
    assert (
        q("svc") == "PriceService.go" and q("this.repo") == "Repo.go" and q("repo") == "Repo.go"
    )  # a local, then the class fields
    assert q("repo", "B.m") == "repo.go" and q("top", "B.m") == "Top.go"  # another class does not see A's fields
    assert q("this") == "this.go" and q("super") == "super.go"
    assert q("Util") == "Util.go" and q("a.b.C") == "a.b.C.go"  # a class or a path as written
    assert q("a.b()") == "*.go" and q('"x"') == "*.go" and q("x[0]") == "*.go"  # an expression: a member by name
    assert lr.qualify(RL, lambda n: "", "svc", "go") == "svc.go"  # a primitive or `var` variable: as written


def test_injection_points():
    owner = lr.def_facts(RL, "method", "C.bean", "bean", "@Bean", 1, "class")
    plain = lr.def_facts(RL, "method", "C.plain", "plain", "public", 1, "class")
    wired = lr.def_facts(RL, "method", "C.set", "set", "@Autowired", 1, "class")
    assert lr.injects(RL, "ctor", "", None) and lr.injects(RL, "field", "@Autowired private", None)
    assert lr.injects(RL, "field", "@Inject", None) and lr.injects(RL, "field", '@Resource(name = "x")', None)
    assert not lr.injects(RL, "field", "private final", None) and not lr.injects(RL, "local", "", None)
    assert lr.injects(RL, "param", "", owner) and lr.injects(RL, "param", "", wired)
    assert not lr.injects(RL, "param", "", plain) and not lr.injects(RL, "param", "", None)


def test_parse_import():
    p = lambda rl, t: lr.parse_import(rl, t, 4)  # noqa: E731
    assert p(RL, "import com.acme.Foo;") == md.Import("com.acme.Foo", (), "", 4, 0, "")
    assert p(RL, "import static com.acme.Foo.bar;").module == "com.acme.Foo.bar"
    assert p(RL, "import com.acme.*;") == md.Import("com.acme", ("*",), "", 4, 0, "")
    assert p(KL, "import com.acme.Foo as F") == md.Import("com.acme.Foo", (), "F", 4, 0, "")
    assert p(KL, "import com.acme.*").names == ("*",) and p(RL, "package a;") is None


# ====================================================================== facts of the golden fixture (fake parser)
def defs(f):
    return {d.qual: d for d in f.defs if d.qual}


def refs(f, kind):
    return {(r.name, r.scope) for r in f.refs if r.kind == kind}


def roots(f):
    return {(r.kind, r.target) for r in f.roots}


def test_controller_facts(spring_files):
    f = spring_files["src/main/java/com/acme/shop/OrderController.java"]
    d = defs(f)
    assert {q: (x.kind, x.vis) for q, x in d.items()} == {
        "OrderController": ("class", "public"),
        "OrderController.quote": ("method", "public"),
        "OrderController.label": ("method", "private"),
    }
    assert roots(f) == {
        ("annotation:spring.controller", "OrderController"),
        ("annotation:spring.mapping", "OrderController.quote"),
    }
    assert refs(f, "di") == {("PriceService", "OrderController")}  # constructor injection
    assert ("PriceService.price", "OrderController.quote") in refs(
        f, "call"
    )  # the receiver is a field of type PriceService
    assert {("label", "OrderController.quote"), ("this.label", "OrderController.quote")} <= refs(f, "call")
    assert len(f.imports) == 3 and f.errors == 0


def test_service_and_interface_facts(spring_files):
    std = spring_files["src/main/java/com/acme/shop/StandardPriceService.java"]
    assert ("PriceService", "StandardPriceService") in refs(std, "impl")
    assert (
        defs(std)["StandardPriceService.price"].flags == md.F_OVERRIDE
        and defs(std)["StandardPriceService.unusedMargin"].vis == "private"
    )
    assert roots(std) == {("annotation:spring.bean", "StandardPriceService")}
    iface = spring_files["src/main/java/com/acme/shop/PriceService.java"]
    assert {(q, d.kind) for q, d in defs(iface).items()} == {
        ("PriceService", "iface"),
        ("PriceService.price", "method"),
    }
    assert defs(iface)["PriceService.price"].vis == "public" and not iface.roots


def test_scheduler_listener_config_and_main_facts(spring_files):
    g = lambda n: spring_files[f"src/main/java/com/acme/shop/{n}.java"]  # noqa: E731
    assert roots(g("RefreshJob")) == {
        ("annotation:spring.bean", "RefreshJob"),
        ("annotation:spring.scheduled", "RefreshJob.refresh"),
    }
    assert refs(g("RefreshJob"), "di") == {
        ("PriceService", "RefreshJob")
    }  # field injection (@Autowired), not constructor
    assert roots(g("OrderEvents")) == {
        ("annotation:spring.bean", "OrderEvents"),
        ("annotation:spring.listener", "OrderEvents.onOrder"),
    }
    assert roots(g("ShopConfig")) >= {("annotation:spring.bean-method", "ShopConfig.auditor")}
    assert refs(g("ShopConfig"), "di") == {("PriceService", "ShopConfig.auditor")}  # @Bean method parameter
    assert roots(g("ShopApplication")) == {
        ("annotation:spring.boot-app", "ShopApplication"),
        ("main", "ShopApplication"),
        ("main", "ShopApplication.main"),
    }
    assert refs(g("Auditor"), "di") == {("PriceService", "Auditor")}  # a plain constructor injects too
    assert roots(g("LegacyPriceService")) == set()  # no stereotype: not a bean
    assert roots(g("OrphanMailer")) == {
        ("annotation:spring.bean", "OrphanMailer")
    }  # a bean nobody injects is still a root
    t = spring_files["src/test/java/com/acme/shop/PriceServiceTest.java"]
    assert roots(t) == {("test", "PriceServiceTest.standardAddsOne")}


def test_imports_become_import_facts(spring_files):
    f = spring_files["src/main/java/com/acme/shop/StandardPriceService.java"]
    assert [(i.module, i.names) for i in f.imports] == [("org.springframework.stereotype.Service", ())]


def test_manual_root_marker_and_field_dedupe():
    src = "public class A {\n    // deadcode: root keep\n    public void m() {\n    }\n}\n"
    f = extract("java", "A.java", src)
    assert [(r.kind, r.target) for r in f.roots] == [("manual", "A.m")]


def test_a_type_used_inside_itself_is_no_ref_and_a_wrapper_injects_its_argument():
    src = "public class Node {\n    @Autowired\n    private List<Handler> handlers;\n    private Node next;\n}\n"
    f = extract("java", "Node.java", src)
    assert ("Node", "Node") not in refs(f, "ref") and ("Handler", "Node") in refs(f, "di")
    assert ("Handler", "Node") in refs(f, "ref") and not any(r.name == "Node" and r.kind == "di" for r in f.refs)


# ====================================================================== link and reach: the golden verdicts
def test_golden_verdicts(spring):
    v = verdicts(spring)
    assert spring[0].findings == ()
    dead = {k for k, x in v.items() if x == "dead"}
    assert dead == {
        "LegacyPriceService",
        "StandardPriceService.unusedMargin",
        "Auditor.total",
        "OrphanMailer.send",
    }  # the dead plain class, the dead private method, unused methods (a class with no stereotype is no bean)
    for alive in (
        "OrderController",
        "OrderController.quote",
        "OrderController.label",
        "RefreshJob.refresh",
        "OrderEvents.onOrder",
        "OrderEvents.handle",
        "ShopApplication.main",
        "StandardPriceService.base",
        "StandardPriceService.price",
        "DiscountPriceService.price",
        "PriceService",
        "PriceService.price",
        "Auditor",
        "ShopConfig.auditor",
        "OrphanMailer",
    ):
        assert v[alive] == "alive", alive  # OrphanMailer: an unused @Service is a hard root, never dead
    assert v["PriceServiceTest"] == v["PriceServiceTest.standardAddsOne"] == "test-only"


def test_di_edges_go_to_every_bean_implementing_the_type(spring):
    di = edges(spring, "di")
    for injector in (
        "OrderController",
        "RefreshJob",
        "ShopConfig.auditor",
        "Auditor",
    ):  # constructor, field, @Bean parameter, plain constructor
        assert (injector, "StandardPriceService", "N") in di and (injector, "DiscountPriceService", "N") in di
        assert (injector, "PriceService", "T") in di  # the type itself is a resolution, the two beans are a choice
        assert not any(s == injector and d == "LegacyPriceService" for s, d, _ in di)  # not a bean: no di edge
    assert {d for _, d, _ in di} == {"PriceService", "StandardPriceService", "DiscountPriceService"}


def test_class_hierarchy_edges_interface_to_implementations(spring):
    ov, im = edges(spring, "override"), edges(spring, "impl")
    for impl in ("StandardPriceService", "DiscountPriceService", "LegacyPriceService"):
        assert (
            "PriceService.price",
            f"{impl}.price",
            "T",
        ) in ov  # a live interface method reaches every implementation
        assert (f"{impl}.price", "PriceService.price", "T") in im and (
            impl,
            "PriceService",
            "T",
        ) in im  # same package: T, no import needed


def test_why_alive_of_a_bean_names_its_annotation_root(spring):
    res, an = spring
    key = next(n.key for n in res.graph.nodes if short(n.key) == "OrderEvents.onOrder")
    w = an.why_alive(key)
    assert w.root.kind == "annotation:spring.listener" and (w.root.strength, w.root.trust) == ("hard", "convention")


def test_single_bean_is_a_resolution_not_a_guess():
    files = {
        "a/Greeter.java": extract("java", "a/Greeter.java", "public interface Greeter {\n    String hi();\n}\n"),
        "a/Polite.java": extract(
            "java",
            "a/Polite.java",
            "@Service\npublic class Polite implements Greeter {\n    public String hi() {\n        return x;\n    }\n}\n",
        ),
        "a/Use.java": extract(
            "java", "a/Use.java", "@Component\npublic class Use {\n    @Autowired\n    private Greeter greeter;\n}\n"
        ),
    }
    di = {(short(e.src), short(e.dst), e.tier) for e in dlink.link(files, workers=1).graph.edges if e.kind == "di"}
    assert di == {("Use", "Greeter", "T"), ("Use", "Polite", "T")}


def test_deeper_hierarchy_and_injection_by_the_concrete_class():
    src = {
        "p/Base.java": "public abstract class Base implements Port {\n}\n",
        "p/Port.java": "public interface Port {\n    void run();\n}\n",
        "p/Impl.java": "@Component\npublic class Impl extends Base {\n    public void run() {\n    }\n}\n",
        "p/Plain.java": "public class Plain extends Base {\n}\n",
        "p/User.java": "@Component\npublic class User {\n    @Resource\n    private Port port;\n    @Inject\n    private Impl direct;\n}\n",
    }
    files = {p: extract("java", p, t) for p, t in src.items()}
    di = {(short(e.src), short(e.dst), e.tier) for e in dlink.link(files, workers=1).graph.edges if e.kind == "di"}
    assert ("User", "Impl", "T") in di and ("User", "Port", "T") in di and not any(d == "Plain" for _, d, _ in di)


def test_link_is_deterministic_for_any_order_and_worker_count(spring_files):
    base = dlink.link(spring_files, workers=1).graph.digest()
    shuffled = dict(random.Random(7).sample(sorted(spring_files.items()), len(spring_files)))
    assert dlink.link(shuffled, workers=1).graph.digest() == base
    assert dlink.link(spring_files, workers=2, min_chunk=1).graph.digest() == base


def test_package_scope_resolves_same_directory_types_only():
    a = extract("java", "x/A.java", "public class A {\n    public void m() {\n        B.go();\n    }\n}\n")
    b = extract("java", "x/B.java", "public class B {\n    public static void go() {\n    }\n}\n")
    far = extract("java", "y/B.java", "public class B {\n    public static void go() {\n    }\n}\n")
    es = {
        (e.src.split("#")[-1], e.dst, e.tier)
        for e in dlink.link({"x/A.java": a, "x/B.java": b, "y/B.java": far}, workers=1).graph.edges
        if e.src.endswith("A.m")
    }
    assert ("A.m", "java:x/B.java#B", "T") in es and ("A.m", "java:x/B.java#B.go", "T") in es
    assert not any(d.startswith("java:y/") for _, d, _ in es)  # the other package is not seen without an import


# ====================================================================== fix round 1: scoping, nested definitions, uses, more roots
def jverdicts(src):
    """{short key: verdict} of a small Java project {path: text}."""
    res = dlink.link({p: extract("java", p, t) for p, t in src.items()}, workers=1)
    an = reach.analyze(res.graph)
    return {short(n.key): an.verdict(n.key) for n in res.graph.nodes if n.kind != "file"}


def jedges(src, kind="call"):
    res = dlink.link({p: extract("java", p, t) for p, t in src.items()}, workers=1)
    return {(short(e.src), short(e.dst)) for e in res.graph.edges if e.kind == kind}


VALUES = {
    "p/Num.java": "public class Num {\n    public int value() {\n        return 1;\n    }\n}\n",
    "p/Add.java": "public class Add {\n    public int value() {\n        return 2;\n    }\n}\n",
}


def test_overloads_keep_their_own_parameters():
    """The visitor pattern: two overloads of one name, one parameter name, two types. One qual, two regions."""
    src = {
        **VALUES,
        "p/V.java": "@Component\npublic class V {\n    @GetMapping\n    public int visit(Num n) {\n        return n.value();\n    }\n    @GetMapping\n    public int visit(Add n) {\n        return n.value();\n    }\n}\n",
    }
    assert {("V.visit", "Num.value"), ("V.visit", "Add.value")} <= jedges(src)
    v = jverdicts(src)
    assert v["Num.value"] == v["Add.value"] == "alive"


def test_constructor_parameter_hides_the_field_only_in_the_constructor():
    src = (
        "public class Holder {\n    private Port port;\n    public Holder(Impl port) {\n        port.init();\n    }\n"
        "    public void run() {\n        port.go();\n    }\n}\n"
    )
    f = extract("java", "Holder.java", src)
    assert {("Impl.init", "Holder"), ("Port.go", "Holder.run")} <= refs(f, "call")
    assert not {r for r in refs(f, "call") if r[0] in ("Port.init", "Impl.go")}


def test_a_local_name_reused_in_two_blocks_and_a_field_after_its_use():
    src = (
        "public class W {\n    public void both(boolean f) {\n        if (f) {\n            Num n = make();\n            n.value();\n        }\n"
        "        if (!f) {\n            Add n = make();\n            n.value();\n        }\n        late.go();\n    }\n    private Late late;\n}\n"
    )
    f = extract("java", "W.java", src)
    calls = refs(f, "call")
    assert [c for c in sorted(calls) if c[0] in ("Num.value", "Add.value")] == [
        ("Add.value", "W.both"),
        ("Num.value", "W.both"),
    ]
    assert ("Late.go", "W.both") in calls  # a field is seen from the whole class body, also before its declaration


def test_a_typeless_variable_hides_the_field_of_its_name():
    src = "public class T {\n    private Port port;\n    public void m() {\n        int port = 1;\n        port.go();\n    }\n}\n"
    assert ("port.go", "T.m") in refs(extract("java", "T.java", src), "call")


ANON = (
    "public class Main {\n    public static void main(String[] args) {\n        Runnable r = new Runnable() {\n"
    "            public void run() {\n                Helper.help();\n            }\n        };\n        r.run();\n    }\n}\n"
)


def test_an_anonymous_class_lives_as_long_as_its_method():
    f = extract("java", "Main.java", ANON)
    assert defs(f)["Main.main.run"].kind == "method" and ("run", "Main.main") in refs(f, "ref")
    src = {
        "p/Main.java": ANON,
        "p/Helper.java": "public class Helper {\n    public static void help() {\n    }\n    public static void spare() {\n    }\n}\n",
    }
    v = jverdicts(src)
    assert v["Main.main.run"] == v["Helper.help"] == "alive" and v["Helper.spare"] == "dead"
    assert jverdicts({"p/Main.java": ANON.replace("public static void main", "static void other")})[
        "Main.other.run"
    ] in (
        "dead",
        "dead-cluster",
    )  # no main: no root


def test_constant_and_static_import_uses_keep_classes_alive():
    src = {
        "a/Constants.java": "public class Constants {\n    public static final int MAX = 3;\n}\n",
        "a/Unused.java": "public class Unused {\n    public static final int NONE = 0;\n}\n",
        "b/Limits.java": "public class Limits {\n    public static final int MIN = 1;\n}\n",
        "a/Main.java": "import static b.Limits.MIN;\npublic class Main {\n    public static void main(String[] args) {\n        int x = Constants.MAX;\n        use(MIN);\n    }\n}\n",
    }
    f = extract("java", "a/Main.java", src["a/Main.java"])
    assert ("Constants", "Main.main") in refs(f, "ref") and ("Limits", "Main") in refs(f, "ref")
    v = jverdicts(src)
    assert (
        v["Constants"] == "alive" and v["Limits"] == "weak-alive" and v["Unused"] == "dead"
    )  # a static import links by name: N


def test_static_class_of_an_import():
    sc = lambda t: lr.static_class(RL, t)  # noqa: E731
    assert (
        sc("import static a.b.C.m;") == "C" and sc("import static a.b.C.*;") == "C" and sc("import static C.m;") == "C"
    )
    assert sc("import a.b.C;") == "" and sc("package a;") == "" and lr.static_class(KL, "import a.b.C") == ""


def test_overloads_merge_to_the_widest_visibility():
    for body in (
        "    private void a(int x) {\n    }\n    public void a(String x) {\n    }\n",
        "    public void a(String x) {\n    }\n    private void a(int x) {\n    }\n",
    ):
        assert defs(extract("java", "V.java", f"public class V {{\n{body}}}\n"))["V.a"].vis == "public"


def test_a_method_named_like_a_type_may_use_that_type():
    src = "public class Fac {\n    public Helper Helper() {\n        return new Helper();\n    }\n}\n"
    assert ("Helper", "Fac.Helper") in refs(extract("java", "Fac.java", src), "ref")


def test_setter_injection_and_framework_beans_are_roots():
    src = {
        "p/Greeter.java": "public interface Greeter {\n    String hi();\n}\n",
        "p/Polite.java": "@Service\npublic class Polite implements Greeter {\n    public String hi() {\n        return x;\n    }\n}\n",
        "p/Svc.java": "@Component\npublic class Svc {\n    private Greeter g;\n    @Autowired\n    public void setGreeter(Greeter g) {\n        this.g = g;\n    }\n}\n",
    }
    f = extract("java", "p/Svc.java", src["p/Svc.java"])
    assert ("annotation:spring.inject-method", "Svc.setGreeter") in roots(f) and ("Greeter", "Svc.setGreeter") in refs(
        f, "di"
    )
    assert jverdicts(src)["Svc.setGreeter"] == "alive" and ("Svc.setGreeter", "Polite") in jedges(src, "di")


@pytest.mark.parametrize(
    "kind,ann,want",
    [
        ("method", "@Around", "annotation:spring.advice"),
        ("method", "@Pointcut", "annotation:spring.advice"),
        ("method", "@AfterReturning", "annotation:spring.advice"),
        ("class", "@Aspect", "annotation:spring.aspect"),
        ("class", "@Entity", "annotation:spring.entity"),
        ("class", "@MappedSuperclass", "annotation:spring.entity"),
        ("class", "@ConfigurationProperties", "annotation:spring.properties"),
        ("class", "@Mapper", "annotation:spring.bean"),
        ("class", "@FeignClient", "annotation:spring.bean"),
    ],
)
def test_more_framework_roots(kind, ann, want):
    assert [h.kind for h in lr.def_facts(RL, kind, "A.m", "m", ann + ' ("x") public', 1, "class").roots] == [want]
    assert (want in lr.bean_kinds()) == (want in ("annotation:spring.bean", "annotation:spring.properties"))


def test_method_source_provider_lives_with_the_test():
    mods = '@ParameterizedTest @MethodSource("a", "T#b", "#c", \'d\')'
    assert (
        lr.ann_strings(mods, "MethodSource") == ["a", "T#b", "#c", "d"] and lr.ann_strings("@Foo(1) @Bar", "Foo") == []
    )
    assert lr.def_facts(RL, "method", "T.t", "t", mods, 1, "class").calls == ("a", "c", "d")
    src = {
        "src/test/java/T.java": 'public class T {\n    @ParameterizedTest\n    @MethodSource("provide")\n    void t(int x) {\n    }\n    static Stream<Arguments> provide() {\n        return null;\n    }\n'
        "    static Stream<Arguments> spare() {\n        return null;\n    }\n}\n"
    }
    v = jverdicts(src)
    assert v["T.provide"] == v["T.t"] == "test-only" and ("T.t", "T.provide") in jedges(src)


def test_kotlin_jvmstatic_main_in_an_object_is_a_main_root():
    def f(mods, qual="Tool.main", parent="class"):
        return lr.def_facts(
            KL, "method", qual, "main", mods, 1, parent
        )  # noqa: E731
    assert sorted((h.kind, h.target) for h in f("@JvmStatic").roots) == [("main", "Tool"), ("main", "Tool.main")]
    assert not f("").roots and not f("@Other").roots


# ====================================================================== fix round 2: nested types, accessors, wildcard, source sets
OUTER = (
    "public class Outer {\n    public static class Inner {\n        public void go() {\n        }\n        public static void st() {\n"
    "        }\n        public void spare() {\n        }\n    }\n}\n"
)


def main_of(body, imports=""):
    return imports + "public class Main {\n    public static void main(String[] a) {\n" + body + "    }\n}\n"


@pytest.mark.parametrize(
    "path,imports,body",
    [
        (
            "q/Main.java",
            "import p.Outer.Inner;\n",
            "        Inner i = new Inner();\n        i.go();\n        Inner.st();\n",
        ),
        (
            "q/Main.java",
            "import p.Outer;\n",
            "        Outer.Inner x = new Outer.Inner();\n        x.go();\n        Outer.Inner.st();\n",
        ),
        ("p/Main.java", "", "        Outer.Inner x = new Outer.Inner();\n        x.go();\n        Outer.Inner.st();\n"),
        ("p/Main.java", "", "        Outer.Inner.st();\n        Outer.Inner y = null;\n        y.go();\n"),
    ],
)
def test_a_nested_class_used_from_another_class_keeps_its_members_and_its_outer_class(path, imports, body):
    v = jverdicts({"p/Outer.java": OUTER, path: main_of(body, imports)})
    assert {k: v[k] for k in ("Outer", "Outer.Inner", "Outer.Inner.go", "Outer.Inner.st")} == dict.fromkeys(
        ("Outer", "Outer.Inner", "Outer.Inner.go", "Outer.Inner.st"), "alive"
    )
    assert v["Outer.Inner.spare"] == "dead"  # the members that nothing calls are still found


def test_a_nested_import_is_an_import_edge_and_a_use_of_the_file():
    es = jedges(
        {"p/Outer.java": OUTER, "q/Main.java": main_of("        Inner i = new Inner();\n", "import p.Outer.Inner;\n")},
        "import",
    )
    assert ("java:q/Main.java", "java:p/Outer.java") in es


def test_a_third_nesting_level_is_entered():
    deep = "public class A {\n    public static class B {\n        public static class C {\n            public static void f() {\n            }\n        }\n    }\n}\n"
    v = jverdicts({"p/A.java": deep, "q/Main.java": main_of("        A.B.C.f();\n", "import p.A;\n")})
    assert all(v[k] == "alive" for k in ("A", "A.B", "A.B.C", "A.B.C.f"))


PROPS = (
    "@ConfigurationProperties\npublic class Props {\n    private int a;\n    public int getA() {\n        return a;\n    }\n"
    "    public void setA(int a) {\n        this.a = a;\n    }\n    public boolean isOn() {\n        return true;\n    }\n"
    "    private int getHidden() {\n        return 1;\n    }\n    public int compute() {\n        return 2;\n    }\n"
    "    public int getaway() {\n        return 3;\n    }\n}\n"
)


@pytest.mark.parametrize("ann,rid", [("@ConfigurationProperties", "spring.properties"), ("@Entity", "spring.entity")])
def test_accessors_of_reflection_bound_classes_are_roots(ann, rid):
    src = PROPS.replace("@ConfigurationProperties", ann)
    f = extract("java", "p/Props.java", src)
    assert {t for k, t in roots(f) if k == f"annotation:{rid}.accessor"} == {"Props.getA", "Props.setA", "Props.isOn"}
    v = jverdicts({"p/Props.java": src})
    assert v["Props"] == "alive" and all(v[m] == "alive" for m in ("Props.getA", "Props.setA", "Props.isOn"))
    assert {k for k, x in v.items() if x == "dead"} >= {
        "Props.compute",
        "Props.getHidden",
        "Props.getaway",
    }  # not accessors


def test_accessors_of_an_ordinary_or_stereotype_class_are_not_roots():
    for ann in ("", "@Service\n"):
        v = jverdicts({"p/Props.java": PROPS.replace("@ConfigurationProperties\n", ann)})
        assert v["Props.getA"] == v["Props.setA"] == "dead"
    assert RL.acc_ids == {"spring.properties", "spring.entity", "jackson.type", "micronaut.data"}


def test_a_wildcard_import_resolves_to_t_unless_two_classes_share_the_name():
    helper = "public class Helper {\n    public static void go() {\n    }\n}\n"
    src = {"b/Helper.java": helper, "q/Main.java": main_of("        Helper.go();\n", "import b.*;\n")}
    res = dlink.link({p: extract("java", p, t) for p, t in src.items()}, workers=1)
    assert {e.tier for e in res.graph.edges if e.kind == "call"} == {"T"}
    assert jverdicts(src)["Helper.go"] == "alive"
    src["c/Helper.java"] = helper
    src["q/Main.java"] = main_of("        Helper.go();\n", "import b.*;\nimport c.*;\n")
    res = dlink.link({p: extract("java", p, t) for p, t in src.items()}, workers=1)
    assert {e.tier for e in res.graph.edges if e.kind == "call"} == {"N"}  # two candidates: a guess


def test_the_main_and_test_source_sets_of_a_package_are_one_scope():
    helper = "public class Helper {\n    public static void go() {\n    }\n}\n"
    t = "public class Main {\n    public static void main(String[] a) {\n        Helper.go();\n    }\n}\n"
    far = "public class Helper {\n    public static void go() {\n    }\n}\n"
    v = jverdicts({"src/main/java/b/Helper.java": helper, "src/test/java/b/Main.java": t})
    assert v["Helper"] == v["Helper.go"] == "alive"
    assert lr.pkg_dir("java", "m1/src/main/java/b/H.java") == lr.pkg_dir("java", "m1/src/test/java/b/H.java") == "m1/b"
    assert lr.pkg_dir("java", "src/test/kotlin/b/H.kt") == lr.pkg_dir("java", "src/main/java/b/H.kt")
    assert lr.pkg_dir("java", "x/H.java") == "x" and lr.pkg_dir("java", "m2/src/main/java/b/H.java") != lr.pkg_dir(
        "java", "m1/src/main/java/b/H.java"
    )
    other = jverdicts({"m1/src/main/java/b/Helper.java": far, "m2/src/test/java/b/Main.java": t})
    assert other["Helper"] == "dead"  # another module: a package of the same name, not the same scope


def test_an_inferred_lambda_parameter_hides_the_field_of_its_name():
    src = (
        "public class T {\n    private Port port;\n    public void m(List<Item> items) {\n        items.forEach(port -> port.go());\n"
        "        items.forEach((x, port) -> port.go());\n    }\n}\n"
    )
    f = extract("java", "T.java", src)
    assert ("port.go", "T.m") in refs(f, "call") and ("Port.go", "T.m") not in refs(f, "call")


def test_the_dead_bean_of_the_fixture_is_a_plain_class_and_a_stereotype_class_is_always_alive(spring):
    v = verdicts(spring)
    assert v["LegacyPriceService"] == "dead" and v["OrphanMailer"] == "alive"
    assert [p for p in spring[0].graph.roots if short(p.node).endswith("OrphanMailer")][
        0
    ].kind == "annotation:spring.bean"


# ====================================================================== pass 3: anonymous class bodies are regions of their own
SVC = """public class Svc {
    private static final Comparator<String> CMP = new Comparator<String>() {
        public int compare(String a, String b) {
            return weigh(a) - weigh(b);
        }
    };
    private final Task t = new Task() {
        public void exec() {
            cleanup();
        }
    };
    private static int weigh(String s) {
        return s.length();
    }
    private void cleanup() {
    }
    private void spare() {
    }
    public void go(List<String> l) {
        l.sort(CMP);
        t.exec();
    }
}
"""
TASK = "public interface Task {\n    void exec();\n}\n"


def svc_user(call="        s.go(null);\n"):
    return main_of("        Svc s = new Svc();\n" + call)


def test_an_anonymous_class_in_a_field_initializer_lives_as_long_as_its_class():
    """The counterexample of the review: the anonymous Comparator and Task of fields; Svc is alive, nothing names their methods."""
    f = extract("java", "p/Svc.java", SVC)
    assert {("compare", "Svc"), ("exec", "Svc")} <= refs(f, "ref")  # the class holds its anonymous members
    v = jverdicts({"p/Svc.java": SVC, "p/Task.java": TASK, "p/Main.java": svc_user()})
    assert [v[k] for k in ("Svc", "Svc.compare", "Svc.exec", "Svc.weigh", "Svc.cleanup", "Svc.go")] == ["alive"] * 6
    assert v["Svc.spare"] == "dead"  # a real member that nothing calls is still found


def test_the_anonymous_members_of_a_class_that_is_dead_stay_dead():
    v = jverdicts({"p/Svc.java": SVC, "p/Task.java": TASK, "p/Main.java": main_of("")})
    assert all(v[k] in ("dead", "dead-cluster") for k in ("Svc", "Svc.compare", "Svc.exec", "Svc.weigh", "Svc.cleanup"))


def test_the_anonymous_class_body_is_a_region_of_its_own():
    """The field of an anonymous class is seen from its whole body, and does not leak into the enclosing class."""
    src = (
        "public class Own {\n    private Port port;\n    private final Task t = new Task() {\n        public void exec() {\n"
        "            port.go();\n        }\n        private Other port;\n    };\n    public void m() {\n        port.go();\n    }\n}\n"
    )
    calls = refs(extract("java", "p/Own.java", src), "call")
    assert ("Other.go", "Own.exec") in calls and ("Port.go", "Own.m") in calls and ("Port.go", "Own.exec") not in calls


def test_anonymous_classes_nest():
    src = (
        "public class Deep {\n    public static void main(String[] args) {\n        Runnable r = new Runnable() {\n"
        "            private final Task t = new Task() {\n                public void exec() {\n                    Helper.help();\n"
        "                }\n            };\n            public void run() {\n                t.exec();\n            }\n        };\n"
        "        r.run();\n    }\n}\n"
    )
    helper = "public class Helper {\n    public static void help() {\n    }\n}\n"
    v = jverdicts({"p/Deep.java": src, "p/Helper.java": helper})
    assert v["Deep.main.run"] == v["Deep.main.exec"] == v["Helper.help"] == "alive"


def test_an_anonymous_class_in_an_initializer_block_lives_with_its_class():
    init = (
        "public class Init {\n    static {\n        Runnable r = new Runnable() {\n            public void run() {\n"
        "                Helper.help();\n            }\n        };\n        r.run();\n    }\n    public static void touch() {\n    }\n}\n"
    )
    helper = "public class Helper {\n    public static void help() {\n    }\n}\n"
    v = jverdicts({"p/Init.java": init, "p/Helper.java": helper, "p/Main.java": main_of("        Init.touch();\n")})
    assert v["Init"] == v["Init.run"] == v["Helper.help"] == "alive"


ENUM = (
    "public enum Op {\n    ADD {\n        int apply(int a, int b) {\n            return plus(a, b);\n        }\n    },\n"
    '    SUB("-") {\n        int apply(int a, int b) {\n            return a - b;\n        }\n    };\n'
    "    abstract int apply(int a, int b);\n    private static int plus(int a, int b) {\n        return a + b;\n    }\n}\n"
)


def test_the_body_of_an_enum_constant_lives_with_its_enum():
    v = jverdicts({"p/Op.java": ENUM, "p/Main.java": main_of("        Op o = null;\n        o.apply(1, 2);\n")})
    assert v["Op"] == v["Op.apply"] == v["Op.plus"] == "alive"
    assert jverdicts({"p/Op.java": ENUM, "p/Main.java": main_of("")})["Op.plus"] in ("dead", "dead-cluster")


# ====================================================================== pass 3: lambdas, method references, framework data
REFS = (
    "public class Refs {\n    public void run(List<Item> items) {\n        items.forEach(this::process);\n"
    "        items.forEach(Util::helper);\n        items.stream().map(Mapper::new);\n        items.forEach(it -> it.go());\n    }\n"
    "    private void process(Item i) {\n    }\n    private void spare() {\n    }\n}\n"
)
UTIL = (
    "public class Util {\n    public static void helper(Item i) {\n    }\n    public static void idle() {\n    }\n}\n"
)
MAPPER = "public class Mapper {\n    public Mapper(Item i) {\n    }\n}\n"


def test_method_references_are_uses_of_the_method_and_of_the_type():
    f = extract("java", "p/Refs.java", REFS)
    assert {("this.process", "Refs.run"), ("Util.helper", "Refs.run")} <= refs(f, "ref")
    assert {("Util", "Refs.run"), ("Mapper", "Refs.run")} <= refs(f, "ref") and ("this", "Refs.run") not in refs(
        f, "ref"
    )
    src = {
        "p/Refs.java": REFS,
        "p/Util.java": UTIL,
        "p/Mapper.java": MAPPER,
        "p/Main.java": main_of("        Refs r = new Refs();\n        r.run(null);\n"),
    }
    v = jverdicts(src)
    assert [v[k] for k in ("Refs.run", "Refs.process", "Util.helper", "Util", "Mapper")] == ["alive"] * 5
    assert v["Refs.spare"] == v["Util.idle"] == "dead"  # a method that no reference names is still found


def test_lambdas_and_method_references_of_a_field_initializer_belong_to_the_class():
    src = (
        "public class Init {\n    private final Runnable r = () -> helper();\n    private final Consumer<Item> c = this::accept;\n"
        "    private static void helper() {\n    }\n    private void accept(Item i) {\n    }\n    private void spare() {\n    }\n}\n"
    )
    f = extract("java", "p/Init.java", src)
    assert ("helper", "Init") in refs(f, "call") and ("this.accept", "Init") in refs(f, "ref")
    v = jverdicts({"p/Init.java": src, "p/Main.java": main_of("        Init i = new Init();\n")})
    assert v["Init.helper"] == v["Init.accept"] == "alive" and v["Init.spare"] == "dead"


JACKSON = "@JsonIgnoreProperties\npublic class Dto {\n    private String name;\n    public String getName() {\n        return name;\n    }\n    public int size() {\n        return 1;\n    }\n}\n"


@pytest.mark.parametrize(
    "kind,mods,want",
    [
        ("class", '@Path("/x")', "annotation:jakarta.resource"),
        ("class", "@WebServlet", "annotation:jakarta.resource"),
        ("method", '@GET @Path("/y")', "annotation:jakarta.resource-method"),
        ("class", "@ApplicationScoped", "annotation:jakarta.cdi-bean"),
        ("method", "@Produces", "annotation:jakarta.producer"),
        ("method", "@PrePersist", "annotation:jakarta.callback"),
        ("method", "@AssertTrue", "annotation:jakarta.validation"),
        ("method", "@JsonCreator", "annotation:jackson.member"),
        ("class", "@JsonDeserialize", "annotation:jackson.type"),
        ("class", "@Module", "annotation:dagger.module"),
        ("method", "@Provides", "annotation:dagger.provides"),
        ("method", "@BeforeMethod", "test"),
        ("method", "@DataProvider", "test"),
        ("class", "@Nested", "test"),
        ("method", "@Benchmark", "test"),
        ("method", "@MessageMapping", "annotation:spring.mapping"),
        ("method", "@SqsListener", "annotation:spring.listener"),
        ("method", "@InitBinder", "annotation:spring.handler"),
    ],
)
def test_framework_rules_of_the_data_file(kind, mods, want):
    assert want in {h.kind for h in lr.def_facts(RL, kind, "A.m", "m", mods + " public", 1, "class").roots}


def test_a_data_binding_type_roots_its_accessors():
    f = extract("java", "p/Dto.java", JACKSON)
    assert {t for k, t in roots(f) if k == "annotation:jackson.type.accessor"} == {"Dto.getName"}
    assert jverdicts({"p/Dto.java": JACKSON})["Dto.getName"] == "alive"


def test_kotlin_operator_and_infix_functions_are_roots_in_kotlin_only():
    for word in ("operator", "infix"):
        r = lr.def_facts(KL, "func", "plus", "plus", f"public {word}", 1, "")
        assert [h.kind for h in r.roots] == ["annotation:kotlin.operator"] and r.flags & md.F_DECORATED
    assert not lr.def_facts(KL, "func", "plus", "plus", "public", 1, "").roots
    assert not lr.def_facts(RL, "method", "A.plus", "plus", "operator", 1, "class").roots  # a Java word like any other


SER = (
    "public class Ser {\n    private void writeObject(ObjectOutputStream out) {\n        encode(out);\n    }\n"
    "    private void encode(ObjectOutputStream out) {\n    }\n    private void spare() {\n    }\n}\n"
)


def test_methods_that_the_jdk_calls_itself_are_protocol_members_of_their_class():
    f = extract("java", "p/Ser.java", SER)
    assert defs(f)["Ser.writeObject"].flags & md.F_PROTOCOL and not defs(f)["Ser.spare"].flags & md.F_PROTOCOL
    v = jverdicts({"p/Ser.java": SER, "p/Main.java": main_of("        Ser s = new Ser();\n")})
    assert [v[k] for k in ("Ser", "Ser.writeObject", "Ser.encode")] == ["alive"] * 3 and v["Ser.spare"] == "dead"
    assert (
        lr.get("java").protocol >= {"readObject", "close", "iterator"}
        and not lr.def_facts(RL, "func", "close", "close", "", 1, "").flags & md.F_PROTOCOL
    )  # a top-level function of that name is no member


# ====================================================================== Kotlin (canned matches: the queries are UNVERIFIED)
KT = (FIX / "kotlin" / "Main.kt").read_text(encoding="utf-8")


def kotlin_parsed():
    k = FakeKotlin(KT)
    k.match("defs", def__iface="interface Greeter {\n    fun greet(name: String): String\n}", name=("Greeter", 0))
    k.match("defs", def__func="fun greet(name: String): String", name=("greet", 0))
    k.match(
        "defs",
        def__class='@Service\nclass PoliteGreeter(private val clock: Clock) : Greeter {\n    override fun greet(name: String): String = "Dear " + name\n\n    private fun unused() {}\n}',
        mods="@Service",
        name=("PoliteGreeter", 0),
    )
    k.match(
        "defs",
        def__func='override fun greet(name: String): String = "Dear " + name',
        mods="override",
        name=("greet", 1),
    )
    k.match("defs", def__func="private fun unused() {}", mods="private", name=("unused", 0))
    k.match("defs", def__class="class Clock", name=("Clock", 1))
    k.match("defs", def__func="fun main(args: Array<String>) {\n    println(args.size)\n}", name=("main", 0))
    k.match("types", var__ctor="private val clock: Clock", mods="private val", type=("Clock", 0), name=("clock", 0))
    k.match("refs", ref__inherit="Greeter", name=("Greeter", 2))
    k.match("refs", ref__call="println(args.size)", name=("println", 0))
    k.match("imports", **{"import": "import org.springframework.stereotype.Service"})
    return k


def test_kotlin_facts_mapping():
    tg.set_backend(lambda g, data: kotlin_parsed())
    f = extract("kotlin", "Main.kt", KT)
    d = defs(f)
    assert {q: (x.kind, x.vis) for q, x in d.items()} == {
        "Greeter": ("iface", "public"),
        "Greeter.greet": ("method", "public"),
        "PoliteGreeter": ("class", "public"),
        "PoliteGreeter.greet": ("method", "public"),
        "PoliteGreeter.unused": ("method", "private"),
        "Clock": ("class", "public"),
        "main": ("func", "public"),
    }
    assert d["PoliteGreeter.greet"].flags == md.F_OVERRIDE
    assert roots(f) == {("annotation:spring.bean", "PoliteGreeter"), ("main", "main")}
    assert refs(f, "di") == {("Clock", "PoliteGreeter")} and refs(f, "inherit") == {("Greeter", "PoliteGreeter")}
    assert [(i.module, i.names) for i in f.imports] == [("org.springframework.stereotype.Service", ())]
    assert ("println", "main") in refs(f, "call") and ("this.println", "main") not in refs(
        f, "call"
    )  # main is top level: no implicit this
    files = {"Main.kt": f}
    v = {
        short(n.key): reach.analyze(dlink.link(files, workers=1).graph).verdict(n.key)
        for n in dlink.link(files, workers=1).graph.nodes
        if n.kind != "file"
    }
    assert v["main"] == "alive" and v["PoliteGreeter"] == "alive" and v["PoliteGreeter.unused"] == "dead"


OBJ = "object : Task {\n        fun exec() {\n            cleanup()\n        }\n    }"
KSVC = f"class Svc {{\n    private val t = {OBJ}\n    private fun cleanup() {{\n    }}\n}}"
KMAIN = "fun main() {\n    Svc()\n}"


def kotlin_object_literal(main=True):
    src = KSVC + "\n" + (KMAIN + "\n" if main else "")
    k = FakeKotlin(src).match("defs", def__class=KSVC, name=("Svc", 0)).match("defs", anon=OBJ)
    k.match("defs", def__func="fun exec() {\n            cleanup()\n        }", name=("exec", 0))
    k.match("defs", def__func="private fun cleanup() {\n    }", mods="private", name=("cleanup", 1))
    k.match("refs", ref__call="cleanup()", name=("cleanup", 0))
    if main:
        k.match("defs", def__func=KMAIN, name=("main", 0)).match("refs", ref__call="Svc()", name=("Svc", 1))
    tg.set_backend(lambda g, d: k)
    return {"Main.kt": extract("kotlin", "Main.kt", src)}


def test_a_kotlin_object_literal_in_a_property_lives_as_long_as_its_class():
    v = {short(n.key): x for n, x in _verdicts(dlink.link(kotlin_object_literal(), workers=1))}
    assert v["Svc"] == v["Svc.exec"] == v["Svc.cleanup"] == v["main"] == "alive"
    gone = {short(n.key): x for n, x in _verdicts(dlink.link(kotlin_object_literal(main=False), workers=1))}
    assert gone["Svc.exec"] in ("dead", "dead-cluster")  # the class is not used: neither is its anonymous object


def _verdicts(res):
    an = reach.analyze(res.graph)
    return [(n, an.verdict(n.key)) for n in res.graph.nodes if n.kind != "file"]


EXT = "package a.util\nfun String.shout() {\n}\nclass Helper {\n    fun go() {\n    }\n}\n"
USE = 'import a.util.shout\nimport a.util.Helper\nfun main() {\n    val text = "x"\n    text.shout()\n    Helper().go()\n}\n'


def kotlin_ext_files(imports=True):
    k1 = FakeKotlin(EXT).match("defs", def__func="fun String.shout() {\n}", name=("shout", 0))
    k1.match("defs", def__class="class Helper {\n    fun go() {\n    }\n}", name=("Helper", 0))
    k1.match("defs", def__func="fun go() {\n    }", name=("go", 0))
    k2 = FakeKotlin(USE).match("defs", def__func=USE[USE.index("fun main") :].rstrip(), name=("main", 0))
    if imports:
        k2.match("imports", **{"import": "import a.util.shout"}).match("imports", **{"import": "import a.util.Helper"})
    k2.match("refs", ref__call="text.shout()", name=("shout", 1), recv=("text", 1))
    k2.match("refs", ref__call="Helper()", name=("Helper", 1)).match(
        "refs", ref__call="Helper().go()", name=("go", 0), recv=("Helper()", 0)
    )
    files = {}
    for path, k, text, pkg in (("a/util/Ext.kt", k1, EXT, ""), ("u/Main.kt", k2, USE, "")):
        tg.set_backend(lambda g, d, k=k: k)
        files[path] = extract("kotlin", path, text)
    return files


def test_a_call_with_a_receiver_may_call_a_top_level_extension_function():
    f = kotlin_ext_files()["u/Main.kt"]
    assert {("text.shout", "main"), ("shout", "main")} <= refs(
        f, "call"
    )  # the member of an unknown type, and the extension


def test_java_calls_with_a_receiver_stay_member_calls():
    f = extract("java", "p/A.java", "public class A {\n    public void m(Port p) {\n        p.go();\n    }\n}\n")
    assert ("go", "A.m") not in refs(f, "call") and ("Port.go", "A.m") in refs(f, "call")


def test_kotlin_imports_bind_the_declarations_of_files_that_are_named_otherwise_and_extension_functions_live():
    v = {short(n.key): x for n, x in _verdicts(dlink.link(kotlin_ext_files(), workers=1))}
    assert v["shout"] == v["Helper"] == v["main"] == "alive"  # Ext.kt holds both, the imports name the packages
    assert v["Helper.go"] in ("alive", "weak-alive")
    es = {(short(e.src), short(e.dst), e.tier) for e in dlink.link(kotlin_ext_files(), workers=1).graph.edges}
    assert ("main", "shout", "T") in es and ("main", "Helper", "T") in es


def test_kotlin_same_package_extension_needs_no_import():
    v = {short(n.key): x for n, x in _verdicts(dlink.link(kotlin_ext_files(imports=False), workers=1))}
    assert v["main"] == "alive" and v["shout"] in ("alive", "weak-alive")  # a name match at worst, never dead


CFG = 'val CONFIG = load()\nfun load(): String {\n    return "x"\n}\nclass Svc {\n    fun go() {\n    }\n}\n'


def kotlin_top_level_init(use):
    k1 = FakeKotlin(CFG).match("defs", def__func='fun load(): String {\n    return "x"\n}', name=("load", 1))
    k1.match("defs", def__class="class Svc {\n    fun go() {\n    }\n}", name=("Svc", 0))
    k1.match("defs", def__func="fun go() {\n    }", name=("go", 0)).match("refs", ref__call="load()", name=("load", 0))
    main = "fun main() {\n    " + ("Svc()" if use else "") + "\n}\n"
    k2 = FakeKotlin(main).match("defs", def__func=main.rstrip(), name=("main", 0))
    if use:
        k2.match("refs", ref__call="Svc()", name=("Svc", 0))
    files = {}
    for path, k, text in (("a/Cfg.kt", k1, CFG), ("a/Main.kt", k2, main)):
        tg.set_backend(lambda g, d, k=k: k)
        files[path] = extract("kotlin", path, text)
    return {short(n.key): x for n, x in _verdicts(dlink.link(files, workers=1))}


def test_the_top_level_initializers_of_a_kotlin_file_run_when_any_declaration_of_it_is_used():
    v = kotlin_top_level_init(use=True)
    assert v["load"] == v["Svc"] == v["main"] == "alive"  # `val CONFIG = load()` is no declaration and has no caller
    assert kotlin_top_level_init(use=False)["load"] in ("dead", "dead-cluster")  # the file is not used at all


def test_kotlin_import_alias_binds_the_class():
    """`import a.B as C` then `C.make()`: the call is a call of B.make (canned matches: the mapping and the linker, not the query)."""
    lib, use = "class B {\n    fun make() {}\n}\n", "import a.B as C\nfun main() {\n    C.make()\n}\n"
    k1 = (
        FakeKotlin(lib)
        .match("defs", def__class="class B {\n    fun make() {}\n}", name=("B", 0))
        .match("defs", def__func="fun make() {}", name=("make", 0))
    )
    k2 = (
        FakeKotlin(use)
        .match("defs", def__func="fun main() {\n    C.make()\n}", name=("main", 0))
        .match("imports", **{"import": "import a.B as C"})
    )
    k2.match("refs", ref__call="C.make()", name=("make", 0), recv=("C", 1))
    files = {}
    for path, k, text in (("a/B.kt", k1, lib), ("u/U.kt", k2, use)):
        tg.set_backend(lambda g, d, k=k: k)
        files[path] = extract("kotlin", path, text)
    res = dlink.link(files, workers=1)
    assert ("main", "B.make") in {(short(e.src), short(e.dst)) for e in res.graph.edges if e.kind == "call"}


# ====================================================================== second pass 3: classes that registration files name (ServiceLoader and kin)
PLUGIN = {
    "p/Plugin.java": "public interface Plugin {\n    void run();\n}\n",
    "p/Impl.java": "public class Impl implements Plugin {\n    public void run() {\n        helper();\n    }\n    private void helper() {\n    }\n}\n",
    "p/Other.java": "public class Other implements Plugin {\n    public void run() {\n    }\n}\n",
    "p/Main.java": main_of(
        "        Plugin p = ServiceLoader.load(Plugin.class).iterator().next();\n        p.run();\n"
    ),
}
SERVICES = "src/main/resources/META-INF/services/p.Plugin"


def reg_hints(path, text):
    return [(h.target, h.line) for h in droots.extract_hints(path, text) if h.kind == "classreg"]


def jreg(src, regs):
    """({short key: verdict}, RootSet) of a Java project {path: text} with registration files {path: text}, linked the way
    levels.build links them: the hints become selector roots (graph.roots.resolve), dc.classreg.bind turns them into nodes.
    """
    hints = {p: droots.extract_hints(p, t) for p, t in regs.items()}
    rs = droots.resolve(hints, {p: "100644" for p in (*src, *regs)}, lambda p: None)
    res = dlink.link({p: extract("java", p, t) for p, t in src.items()}, workers=1)
    bound = classreg.bind(res.graph.nodes, rs.roots)
    g = md.canonical(res.graph.nodes, res.graph.edges, (*res.graph.roots, *bound), res.graph.evid)
    an = reach.analyze(g)
    return {short(n.key): an.verdict(n.key) for n in g.nodes if n.kind != "file"}, rs


def test_a_service_provider_that_a_services_file_names_is_not_dead():
    """The counterexample of the review: Impl is only a ServiceLoader provider, nothing in the code names it."""
    v = jverdicts(PLUGIN)
    assert v["Impl"] == v["Other"] == "dead"  # no registration, no root: the control
    v, rs = jreg(PLUGIN, {SERVICES: "# providers\np.Impl\n"})
    assert [(r.kind, r.selector, r.origin) for r in rs.roots] == [("classreg", "class:p.Impl", SERVICES + ":2")]
    assert [v[k] for k in ("Impl", "Impl.run", "Impl.helper", "Plugin")] == ["alive"] * 4
    assert v["Other"] == "dead"  # a class that no file registers is still found


def test_a_services_file_is_read_line_by_line_with_comments():
    text = "\ufeff# the providers\n\np.A   # first\n  p.B\n#p.C\nnot a class!\n"
    assert reg_hints(SERVICES, text) == [("class:p.A", 3), ("class:p.B", 4)]
    assert reg_hints("lib/META-INF/services/x.Y", "q.Z\n") == [("class:q.Z", 1)] and not reg_hints(
        "lib/services/x.Y", "q.Z\n"
    )


MODULE = "module app {\n    // provides p.Plugin with p.Gone;\n    requires java.base;\n    provides p.Plugin\n        with p.Impl, /* p.Gone */ p.Extra;\n    uses p.Plugin;\n}\n"


def test_module_info_provides_with_roots_every_listed_provider():
    assert reg_hints("module-info.java", MODULE) == [("class:p.Extra", 5), ("class:p.Impl", 5)]
    assert not reg_hints("src/Main.java", MODULE)
    src = {**PLUGIN, "p/Extra.java": "public class Extra implements Plugin {\n    public void run() {\n    }\n}\n"}
    v, _ = jreg(src, {"src/main/java/module-info.java": MODULE})
    assert v["Impl"] == v["Extra"] == "alive" and v["Other"] == "dead"


def test_a_provider_method_of_a_module_provider_lives_with_its_class():
    src = "public class Prov {\n    public static Plugin provider() {\n        return null;\n    }\n    public void spare() {\n    }\n}\n"
    v, _ = jreg({"p/Prov.java": src}, {"module-info.java": "module m {\n    provides p.Plugin with p.Prov;\n}\n"})
    assert v["Prov"] == v["Prov.provider"] == "alive" and v["Prov.spare"] == "dead"


def test_spring_factories_and_imports_files_name_classes():
    fac = "# auto-configuration\norg.springframework.boot.autoconfigure.EnableAutoConfiguration=\\\n  p.AutoA,\\\n  p.AutoB\nx.Listener = p.Listener\n"
    assert reg_hints("src/main/resources/META-INF/spring.factories", fac) == [
        ("class:p.AutoA", 2),
        ("class:p.AutoB", 2),
        ("class:p.Listener", 5),
    ]
    imp = "src/main/resources/META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports"
    assert reg_hints(imp, "p.AutoA\n# c\np.AutoB\n") == [("class:p.AutoA", 1), ("class:p.AutoB", 3)]


def test_gradle_plugin_ids_and_build_plugin_annotations_name_entry_points():
    got = reg_hints(
        "src/main/resources/META-INF/gradle-plugins/acme.gradle.properties", "implementation-class=p.AcmePlugin\n"
    )
    assert got == [("class:p.AcmePlugin", 1)]
    def f(kind, ann):
        return {
            h.kind for h in lr.def_facts(RL, kind, "A.m", "m", ann + " public", 1, "class").roots
        }  # noqa: E731
    assert f("class", "@AutoService") == {"annotation:jvm.autoservice"} and f("class", "@Mojo") == {
        "annotation:jvm.build-plugin"
    }
    assert f("method", "@TaskAction") == {"annotation:jvm.build-task"}


WEB_XML = (
    "<web-app>\n  <!-- <servlet-class>p.Gone</servlet-class> -->\n  <servlet>\n    <servlet-name>s</servlet-name>\n"
    "    <servlet-class>p.Hello</servlet-class>\n  </servlet>\n  <filter><filter-class> p.Auth </filter-class></filter>\n"
    "  <listener><listener-class>p.Boot</listener-class></listener>\n</web-app>\n"
)


def test_web_and_persistence_descriptors_name_classes():
    assert reg_hints("src/main/webapp/WEB-INF/web.xml", WEB_XML) == [
        ("class:p.Auth", 7),
        ("class:p.Boot", 8),
        ("class:p.Hello", 5),
    ]
    pu = "<persistence>\n  <persistence-unit name='u'>\n    <class>p.Order</class>\n    <class>p.Item</class>\n  </persistence-unit>\n</persistence>\n"
    assert reg_hints("src/main/resources/META-INF/persistence.xml", pu) == [("class:p.Item", 4), ("class:p.Order", 3)]
    assert not reg_hints("conf/other.xml", WEB_XML)  # the file kind decides, not the tags


BEANS = (
    '<beans xmlns="http://www.springframework.org/schema/beans">\n  <bean id="pool" class="p.Pool" init-method="start" destroy-method=\'stop\'/>\n'
    '  <!-- <bean class="p.Gone"/> -->\n  <bean id="plain" class="p.Plain"/>\n</beans>\n'
)
POOL = "public class Pool {\n    public void start() {\n    }\n    public void stop() {\n    }\n    public void spare() {\n    }\n}\n"


def test_a_spring_xml_bean_is_a_root_with_its_init_and_destroy_methods():
    got = reg_hints("conf/beans.xml", BEANS)
    assert got == [("class:p.Plain", 4), ("class:p.Pool", 2), ("class:p.Pool#start", 2), ("class:p.Pool#stop", 2)]
    assert not reg_hints(
        "pom.xml", '<project>\n  <bean class="p.Pool"/>\n</project>\n'
    )  # no <beans>: another kind of xml
    v, _ = jreg({"p/Pool.java": POOL}, {"conf/beans.xml": BEANS})
    assert v["Pool"] == v["Pool.start"] == v["Pool.stop"] == "alive" and v["Pool.spare"] == "dead"


def test_a_namespace_url_is_no_comment_in_xml_and_a_slash_slash_is_one_in_module_info():
    xml = '<beans>\n  <bean class="p.Pool" xmlns:x="http://example.org/x" init-method="start"/>\n</beans>\n'
    assert reg_hints("conf/beans.xml", xml) == [("class:p.Pool", 2), ("class:p.Pool#start", 2)]
    mod = "module m {\n    provides p.Plugin with p.A; // with p.Gone;\n}\n"
    assert reg_hints("module-info.java", mod) == [("class:p.A", 2)]


def test_android_components_of_the_manifest_are_roots():
    man = (
        '<manifest package="com.acme">\n  <uses-permission android:name="android.permission.INTERNET"/>\n  <application android:name=".App">\n'
        '    <activity android:name=".ui.Main" android:exported="true"/>\n    <service android:name="com.acme.Sync"/>\n  </application>\n</manifest>\n'
    )
    assert reg_hints("app/src/main/AndroidManifest.xml", man) == [
        ("class:App", 3),
        ("class:com.acme.Sync", 5),
        ("class:ui.Main", 4),
    ]
    src = {
        "app/src/main/java/com/acme/ui/Main.java": "public class Main {\n}\n",
        "app/src/main/java/com/acme/Other.java": "public class Other {\n}\n",
    }
    v, _ = jreg(src, {"app/src/main/AndroidManifest.xml": man})
    assert v["Main"] == "alive" and v["Other"] == "dead"


def test_a_registered_name_is_matched_by_package_and_nesting_and_a_library_class_gives_no_root():
    src = {
        "mod/p/Outer.java": "public class Outer {\n    public static class Inner {\n    }\n}\n",
        "a/p/Impl.java": "public class Impl {\n}\n",
        "b/p/Impl.java": "public class Impl {\n}\n",
        "xp/Impl.java": "public class Impl {\n}\n",
        "src/main/java/p/Src.java": "public class Src {\n}\n",
    }
    reg = {SERVICES: "p.Outer$Inner\np.Impl\nq.Library\np.Src\n"}
    v, rs = jreg(src, reg)
    assert (
        v["Outer.Inner"] == v["Src"] == "alive" and v["Outer"] == "dead"
    )  # the root names the nested class, the file is rooted
    assert not rs.findings  # a class of a library is no dangling registration
    res = dlink.link({p: extract("java", p, t) for p, t in src.items()}, workers=1)
    bound = classreg.bind(res.graph.nodes, rs.roots)
    assert sorted(r.node.split("#")[0] for r in bound if "#" not in r.node) == [
        "java:a/p/Impl.java",
        "java:b/p/Impl.java",
        "java:mod/p/Outer.java",
        "java:src/main/java/p/Src.java",
    ]  # both modules' Impl (a guess is alive), not xp.Impl, nothing for q.Library
    assert all(r.selector == "" for r in bound) and classreg.bind(res.graph.nodes, ()) == ()


def test_a_registration_under_test_resources_registers_nothing():
    for path in ("src/test/resources/META-INF/services/p.Plugin", "tests/fixtures/META-INF/services/p.Plugin"):
        hints = {path: droots.extract_hints(path, "p.Impl\n")}
        assert hints[path] and not droots.resolve(hints, {path: "100644"}, lambda p: None).roots


def test_the_class_registry_rules_are_data_and_a_changed_rule_changes_the_shard_version(monkeypatch, tmp_path):
    ex = classreg.ClassRegistryRoots()
    base = ex.ext_ver
    d = json.loads(Path(lr.DATA).read_text(encoding="utf-8"))
    ids = [r["id"] for r in d["class_registries"]["rules"]]
    assert len(set(ids)) == len(ids) and all(
        r["read"] in classreg.READERS and r["files"] and r["why"] for r in d["class_registries"]["rules"]
    )
    assert all(
        ("re" in r) == (r["read"] == "match") and (r["read"] != "xml" or r.get("text") or r.get("attrs"))
        for r in d["class_registries"]["rules"]
    )
    d["class_registries"]["rules"][0]["files"] += "|extra\\.services$"
    alt = tmp_path / "frameworks.json"
    alt.write_text(json.dumps(d), encoding="utf-8")
    monkeypatch.setattr(lr, "DATA", str(alt))
    assert ex.ext_ver != base and ex.matches("conf/extra.services") and not ex.matches("conf/other.txt")


def test_the_class_registry_roots_are_collected_from_a_tree_and_cached_by_blob(tmp_path):
    import subprocess

    from dead_code_checker.graph import store, vcs

    repo = tmp_path / "repo"
    for rel, text in {SERVICES: "p.Impl\n", "p/Impl.java": "public class Impl {\n}\n", "doc.txt": "p.Nope\n"}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    tree = vcs.snapshot(str(repo))
    st = store.Store(str(tmp_path / "db"))
    first = droots.collect(str(repo), tree, st, None, 1)
    assert [(p, [(h.kind, h.target) for h in hs if h.kind == "classreg"]) for p, hs in first.items()] == [
        (SERVICES, [("classreg", "class:p.Impl")])
    ]
    assert droots.collect(str(repo), tree, st, None, 1) == first  # the warm run reads the shard


# ====================================================================== second pass 3: framework data (Micronaut, events, string attributes)
CTRL = (
    '@Controller("/x")\npublic class Api {\n    @Get\n    public String list() {\n        return fmt();\n    }\n    @Post\n    public void add() {\n    }\n'
    "    @Error\n    public String oops() {\n        return null;\n    }\n    private String fmt() {\n        return null;\n    }\n"
    "    public void spare() {\n    }\n}\n"
)


def test_micronaut_routes_filters_and_error_handlers_are_roots():
    v = jverdicts({"p/Api.java": CTRL})
    assert [v[k] for k in ("Api", "Api.list", "Api.add", "Api.oops", "Api.fmt")] == ["alive"] * 5 and v[
        "Api.spare"
    ] == "dead"
    def f(kind, ann):
        return {
            h.kind for h in lr.def_facts(RL, kind, "A.m", "m", ann + " public", 1, "class").roots
        }  # noqa: E731
    for ann in ("@Put", "@Delete", "@Patch", "@Head", "@Options", "@Trace", "@RequestFilter", "@ResponseFilter"):
        assert f("method", ann) == {"annotation:micronaut.route"}
    assert f("class", "@Filter") == {"annotation:micronaut.bean"} and f("class", "@ServerFilter") == {
        "annotation:micronaut.bean"
    }
    assert (
        f("class", "@Introspected") == {"annotation:micronaut.data"} and "annotation:micronaut.bean" in lr.bean_kinds()
    )
    assert f("method", "@get") == set()  # the JAX-RS verbs are upper case, Micronaut's are not: names are exact


BUS = (
    "@Singleton\npublic class Listener {\n    @Subscribe\n    public void onOrder(Order o) {\n        audit(o);\n    }\n"
    "    public void onCancel(@Observes Cancel c) {\n        audit(c);\n    }\n    @Incoming\n    public void feed(String s) {\n    }\n"
    "    private void audit(Object o) {\n    }\n    public void spare(Cancel c) {\n    }\n}\n"
)


def test_event_subscribers_and_cdi_observers_are_roots():
    f = extract("java", "p/Listener.java", BUS)
    assert roots(f) >= {
        ("annotation:events.subscriber", "Listener.onOrder"),
        ("annotation:events.observer", "Listener.onCancel"),
        ("annotation:events.subscriber", "Listener.feed"),
    }
    assert ("annotation:events.observer", "Listener.spare") not in roots(f)  # no annotation on its parameter
    v = jverdicts({"p/Listener.java": BUS})
    assert [v[k] for k in ("Listener.onOrder", "Listener.onCancel", "Listener.feed", "Listener.audit")] == ["alive"] * 4
    assert v["Listener.spare"] == "dead"


def test_a_parameter_annotation_of_a_lambda_or_constructor_roots_nothing():
    src = "public class Ctor {\n    public Ctor(@Observes Cancel c) {\n    }\n    public void m() {\n        run(x -> x);\n    }\n}\n"
    assert not [r for r in roots(extract("java", "p/Ctor.java", src)) if r[0].startswith("annotation:events")]
    mods = lr.param_roots(RL, "@Observes", "A.on", 3)
    assert [(h.kind, h.target, h.line) for h in mods] == [("annotation:events.observer", "A.on", 3)]
    assert lr.param_roots(RL, "@Other final", "A.on", 3) == () and lr.param_roots(RL, "", "A.on", 3) == ()


CONFIG = (
    '@Configuration\npublic class Config {\n    @Bean(name = "poolBean", initMethod = "start", destroyMethod = "stop")\n    public Pool pool() {\n'
    "        return new Pool();\n    }\n}\n"
)


def test_a_bean_names_its_init_and_destroy_methods_as_strings():
    f = extract("java", "p/Config.java", CONFIG)
    assert {("*.start", "Config.pool"), ("*.stop", "Config.pool")} <= refs(f, "call")
    assert not {n for n, _ in refs(f, "call") if "poolBean" in n}  # `name` is no method
    v = jverdicts({"p/Config.java": CONFIG, "p/Pool.java": POOL})
    assert v["Pool.start"] in ("alive", "weak-alive") and v["Pool.stop"] in (
        "alive",
        "weak-alive",
    )  # a member named so: a guess
    assert v["Pool.spare"] == "dead"  # a method that no string names is still found
    assert lr.ann_strings(
        '@Bean(name = "n", initMethod = "m", destroyMethod = "d")', "Bean", ("initMethod", "destroyMethod")
    ) == ["m", "d"]
    assert (
        lr.ann_strings('@Bean("a")', "Bean", ("value",)) == ["a"]
        and lr.ann_strings('@Bean("a")', "Bean", ("name",)) == []
    )
    assert lr.ann_strings('@X(a = {"p", "q"}, b = "r")', "X", ("a",)) == ["p", "q"]
    assert lr.def_facts(RL, "method", "C.b", "b", '@Bean(initMethod = "")', 1, "class").calls == ()


def test_a_junit_condition_method_lives_with_its_test():
    mods = '@Test @EnabledIf("fast") @Tag("x")'
    assert lr.def_facts(RL, "method", "T.t", "t", mods, 1, "class").calls == ("fast",)
    assert lr.def_facts(
        RL, "method", "T.t", "t", '@Test @EnabledIf(value = "fast", disabledReason = "slow")', 1, "class"
    ).calls == ("fast",)
    src = {
        "src/test/java/T.java": 'public class T {\n    @Test\n    @EnabledIf("fast")\n    void t() {\n    }\n    boolean fast() {\n        return true;\n    }\n}\n'
    }
    assert jverdicts(src)["T.fast"] == "test-only"


# ====================================================================== loud failures, never a silent fallback
def test_missing_grammar_is_extractor_missing_and_a_finding(no_tree_sitter):
    tg.set_backend(None)
    for lang, g in (("java", "java"), ("kotlin", "kotlin")):
        with pytest.raises(contracts.ExtractorMissing) as ei:
            contracts.get_extractor(lang).extract(lang, "A.x", "class A {}")
        assert ei.value.lang == lang and "tree-sitter is not installed" in ei.value.reason
        f = tg.missing_finding(lang, f"src/A.{g}")
        assert (f.level, f.code) == ("W", "dc.degraded-extractor") and g in f.msg  # the files are left out of the graph, said once


def test_an_unknown_var_kind_is_an_error_of_the_query():
    class P:
        errors = 0

        def run(self, kind):
            n = FNode(0, 1, (0, 0), (0, 1))
            return [(0, {"var.bogus": [n], "type": [n], "name": [n]})] if kind == "types" else []

    tg.set_backend(lambda g, d: P())
    with pytest.raises(ValueError, match="not a variable kind"):
        extract("java", "A.java", "class A {}")


# ====================================================================== the real grammar (skipped without it)
def _real_grammar(g):
    tsm.reset()
    try:
        tsm.get_parser(g)
    except LookupError as e:
        return str(e)
    return ""


@pytest.mark.parametrize("lang", ["java", "kotlin"])
def test_real_grammar_queries_compile_and_find_the_fixture_defs(lang):
    why = _real_grammar(lang)
    if why:
        pytest.skip(
            f"tree-sitter grammar {lang} is not installed, trusted and verified on this host ({why}): the .scm queries were never run"
        )
    tg.set_backend(None)
    if lang == "java":
        f = extract(
            "java", "OrderController.java", (SPRING / "src/main/java/com/acme/shop/OrderController.java").read_text()
        )
        assert {"OrderController", "OrderController.quote"} <= set(defs(f)) and roots(f) >= {
            ("annotation:spring.mapping", "OrderController.quote")
        }
        assert ("PriceService", "OrderController") in refs(f, "di") and f.errors == 0
    else:
        f = extract("kotlin", "Main.kt", KT)
        assert {"Greeter", "PoliteGreeter", "main"} <= set(defs(f)) and ("main", "main") in roots(f)


KOBJ = (
    "interface Task { fun exec() }\n\nclass Svc {\n    private val t = object : Task {\n        override fun exec() {\n"
    "            cleanup()\n        }\n    }\n\n    private fun cleanup() {\n    }\n}\n"
)


@pytest.mark.parametrize("lang", ["java", "kotlin"])
def test_real_grammar_anonymous_bodies_references_and_parameter_annotations(lang):
    """The constructs of the last two passes, on the real grammar: the @anon captures (a class body of a field initializer, an enum
    constant body, a Kotlin object literal), method references, and the annotated parameter of an observer."""
    why = _real_grammar(lang)
    if why:
        pytest.skip(
            f"tree-sitter grammar {lang} is not installed, trusted and verified on this host ({why}): the @anon, method reference and "
            "parameter captures of the .scm queries were never run; the tests above prove the facts mapping through the fake parser only"
        )
    tg.set_backend(None)
    if lang == "java":
        assert {("compare", "Svc"), ("exec", "Svc")} <= refs(extract("java", "p/Svc.java", SVC), "ref")
        assert ("apply", "Op") in refs(extract("java", "p/Op.java", ENUM), "ref")
        assert {("this.process", "Refs.run"), ("Util.helper", "Refs.run")} <= refs(
            extract("java", "p/Refs.java", REFS), "ref"
        )
        assert ("annotation:events.observer", "Listener.onCancel") in roots(extract("java", "p/Listener.java", BUS))
        assert ("*.start", "Config.pool") in refs(extract("java", "p/Config.java", CONFIG), "call")
    else:
        assert ("exec", "Svc") in refs(extract("kotlin", "Svc.kt", KOBJ), "ref")


# ====================================================================== second pass 4: composed annotations, top-level properties, scripts, loose packages
def kt_project(files):
    """{path: (text, [(capture kind, {capture: needle})])} -> {path: Facts}: Kotlin files read through canned matches."""
    out = {}
    for path, (text, matches) in files.items():
        k = FakeKotlin(text)
        for kind, caps in matches:
            k.match(kind, **caps)
        tg.set_backend(lambda g, d, k=k: k)
        out[path] = extract("kotlin", path, text)
    return out


def verdicts_of(facts, extra=()):
    """{short key: verdict} of linked facts, with extra roots (graph.roots.resolve) merged the way levels.build merges them."""
    res = dlink.link(facts, workers=1)
    g = md.canonical(res.graph.nodes, res.graph.edges, (*res.graph.roots, *extra), res.graph.evid)
    an = reach.analyze(g)
    return {short(n.key): an.verdict(n.key) for n in g.nodes if n.kind != "file"}


DEAD_OR_CLUSTER = ("dead", "dead-cluster")
FACADE = {
    "p/Facade.java": "@Service\npublic @interface Facade {\n}\n",
    "p/UserFacade.java": "@Facade\npublic class UserFacade {\n    public void go() {\n    }\n}\n",
}


def test_a_composed_stereotype_roots_the_class_like_the_annotation_it_carries():
    """The counterexample of the review: @Facade is a project @interface carrying @Service; nothing names UserFacade."""
    f = extract("java", "p/Facade.java", FACADE["p/Facade.java"])
    assert roots(f) == {("annotation:spring.bean.meta", "Facade"), ("annotation:.meta", "Facade")}  # no root of its own
    assert defs(f)["Facade"].kind == "iface"
    assert ("Facade", "UserFacade") in refs(extract("java", "p/UserFacade.java", FACADE["p/UserFacade.java"]), "reg")
    v = jverdicts(FACADE)
    assert (
        v["UserFacade"] == "alive" and v["Facade"] == "alive"
    )  # the annotation type lives as long as a def that uses it
    assert v["UserFacade.go"] in DEAD_OR_CLUSTER  # a bean root roots the class, not its methods
    res = dlink.link({p: extract("java", p, t) for p, t in FACADE.items()}, workers=1)
    assert [(r.kind, r.strength, r.trust) for r in res.graph.roots if r.node.endswith("#UserFacade")] == [
        ("annotation:spring.bean", "hard", "convention")
    ]


def test_an_annotation_type_with_no_rules_and_an_unused_one_root_nothing():
    plain = {**FACADE, "p/Facade.java": "public @interface Facade {\n}\n"}
    assert jverdicts(plain)["UserFacade"] in DEAD_OR_CLUSTER  # @Facade carries no rule: the control
    assert (
        jverdicts({"p/Facade.java": FACADE["p/Facade.java"]})["Facade"] in DEAD_OR_CLUSTER
    )  # nothing uses the annotation type
    assert (
        jverdicts({"p/UserFacade.java": FACADE["p/UserFacade.java"]})["UserFacade"] in DEAD_OR_CLUSTER
    )  # a library @Facade


def test_composed_annotations_chain_and_survive_a_cycle():
    chain = {
        **FACADE,
        "p/UseCase.java": "@Facade\npublic @interface UseCase {\n}\n",
        "p/Order.java": "@UseCase\npublic class Order {\n}\n",
        "p/A.java": "@B\npublic @interface A {\n}\n",
        "p/B.java": "@A\n@Service\npublic @interface B {\n}\n",
        "p/Loop.java": "@A\npublic class Loop {\n}\n",
    }
    v = jverdicts(chain)
    assert v["Order"] == v["UseCase"] == v["Facade"] == "alive"  # @UseCase carries @Facade carries @Service
    assert v["Loop"] == "alive"  # A and B use each other: the closure ends


def test_a_composed_annotation_hands_on_the_method_rules_and_the_bean_kind():
    src = {
        "p/Fetch.java": '@RequestMapping("/x")\npublic @interface Fetch {\n}\n',
        "p/Ctl.java": "public class Ctl {\n    @Fetch\n    public void orders() {\n    }\n    public void other() {\n    }\n}\n",
        "p/Port.java": "public interface Port {\n    void run();\n}\n",
        "p/Adapter.java": "@Facade\npublic class Adapter implements Port {\n    public void run() {\n    }\n}\n",
        "p/Svc.java": "@Component\npublic class Svc {\n    @Autowired\n    private Port port;\n}\n",
        **FACADE,
    }
    v = jverdicts(src)
    assert (
        v["Ctl.orders"] == "alive" and v["Ctl.other"] in DEAD_OR_CLUSTER
    )  # the method side of @RequestMapping, through @Fetch
    assert ("Svc", "Adapter") in jedges(src, "di")  # a composed bean takes part in the injection like @Service
    assert ("Svc", "Adapter") not in jedges({**src, "p/Facade.java": "public @interface Facade {\n}\n"}, "di")


def test_the_own_rules_of_an_annotation_type_are_read_on_every_side_and_a_kotlin_annotation_class_is_one():
    df = lr.def_facts(RL, "iface", "Fx", "Fx", '@Service @RequestMapping("/") @Test @Override', 1, "", None, True)
    assert {h.kind for h in df.roots} == {
        "annotation:spring.bean.meta",
        "annotation:spring.mapping.meta",
        "annotation:junit.test.meta",
        "annotation:.meta",
    } and not df.flags & md.F_DECORATED
    kf = lr.def_facts(KL, "class", "UseCase", "UseCase", "@Service\nannotation", 1, "", None)  # meta_mods of the data
    assert {h.kind for h in kf.roots} == {"annotation:spring.bean.meta", "annotation:.meta"}
    assert "annotation:spring.bean" in {
        h.kind for h in lr.def_facts(KL, "class", "C", "C", "@Service", 1, "", None).roots
    }
    assert (
        (lr.meta_rule("annotation:x.y.meta"), lr.meta_rule("annotation:.meta"), lr.meta_rule("annotation:x.y"))
        == (
            "x.y",
            "",
            None,
        )
        and lr.meta_rule("test") is None
        and lr.rule("spring.bean")["bean"]
        and lr.rule("nope") is None
    )
    assert lr.inherit({"a": ["r1"], "b": ["r2"]}, {"a": ["b"], "b": ["a"], "c": ["a"]}) == {
        "a": ("r1", "r2"),
        "b": ("r1", "r2"),
        "c": ("r1", "r2"),
    }


KFACADE = {
    "p/UseCase.kt": (
        "@Service\nannotation class UseCase\n",
        [
            (
                "defs",
                {
                    "def__class": "@Service\nannotation class UseCase",
                    "mods": "@Service\nannotation",
                    "name": ("UseCase", 0),
                },
            ),
            ("refs", {"ref__reg": "@Service", "name": ("Service", 0)}),
        ],
    ),
    "p/Checkout.kt": (
        "@UseCase\nclass Checkout\n",
        [
            ("defs", {"def__class": "@UseCase\nclass Checkout", "mods": "@UseCase", "name": ("Checkout", 0)}),
            ("refs", {"ref__reg": "@UseCase", "name": ("UseCase", 0)}),
        ],
    ),
}


def test_a_kotlin_composed_stereotype_roots_the_class():
    v = verdicts_of(kt_project(KFACADE))
    assert v["Checkout"] == "alive" and v["UseCase"] == "alive"
    lone = {"p/Checkout.kt": KFACADE["p/Checkout.kt"]}
    assert verdicts_of(kt_project(lone))["Checkout"] in DEAD_OR_CLUSTER  # the annotation is a library one: no rule
    assert roots(kt_project(KFACADE)["p/UseCase.kt"]) == {
        ("annotation:spring.bean.meta", "UseCase"),
        ("annotation:.meta", "UseCase"),
    }


def test_a_qualified_type_inside_its_own_class_is_another_type():
    """`class Order { com.other.Order legacy; }`: the type of that name outside the class is used, not the class itself."""
    own = "public class Order {\n    com.other.Order legacy;\n    Order next;\n}\n"
    src = {
        "a/Order.java": own,
        "com/other/Order.java": "public class Order {\n}\n",
        "a/Main.java": main_of("        new Order();\n"),
    }
    f = extract("java", "a/Order.java", own)
    assert ("com.other.Order", "Order") in refs(f, "ref") and ("Order", "Order") not in refs(
        f, "ref"
    )  # `next` is its own type
    res = dlink.link({p: extract("java", p, t) for p, t in src.items()}, workers=1)
    assert any(
        e.dst == "java:com/other/Order.java#Order" and e.src == "java:a/Order.java#Order" for e in res.graph.edges
    )
    assert reach.analyze(res.graph).verdict("java:com/other/Order.java#Order") in ("alive", "weak-alive")
    plain = {**src, "a/Order.java": "public class Order {\n    Order next;\n}\n"}
    res = dlink.link({p: extract("java", p, t) for p, t in plain.items()}, workers=1)
    assert reach.analyze(res.graph).verdict("java:com/other/Order.java#Order") in DEAD_OR_CLUSTER  # the control


# ---------------------------------------------------------------- Kotlin: top-level properties
CFG_A = 'val SETTINGS = load()\nprivate fun load(): String {\n    return "x"\n}\n'
CFG_A_MATCHES = [
    ("defs", {"def__var": "val SETTINGS = load()", "name": ("SETTINGS", 0)}),
    ("defs", {"def__func": 'private fun load(): String {\n    return "x"\n}', "mods": "private", "name": ("load", 1)}),
    ("refs", {"ref__call": "load()", "name": ("load", 0)}),
]
USE_MAIN = "fun main() {\n    println(SETTINGS)\n}\n"
USE_MATCHES = [
    ("defs", {"def__func": USE_MAIN.rstrip(), "name": ("main", 0)}),
    ("refs", {"ref__call": "println(SETTINGS)", "name": ("println", 0)}),
    ("refs", {"ref__ref": "SETTINGS", "name": ("SETTINGS", 0)}),
]


def test_a_top_level_property_used_from_another_file_keeps_its_file_and_the_helpers_of_its_initializer():
    """The counterexample of the review: A.kt holds `val SETTINGS = load()` and `private fun load()`, B.kt prints SETTINGS."""
    facts = kt_project({"a/A.kt": (CFG_A, CFG_A_MATCHES), "a/B.kt": (USE_MAIN, USE_MATCHES)})
    assert {q: d.kind for q, d in defs(facts["a/A.kt"]).items()} == {"SETTINGS": "var", "load": "func"}
    assert ("load", "SETTINGS") in refs(facts["a/A.kt"], "call")  # the initializer belongs to the property
    v = verdicts_of(facts)
    assert v["SETTINGS"] == v["load"] == v["main"] == "alive"
    unused = kt_project(
        {
            "a/A.kt": (CFG_A, CFG_A_MATCHES),
            "a/B.kt": (
                "fun main() {\n}\n",
                [USE_MATCHES[0][:1] + ({"def__func": "fun main() {\n}", "name": ("main", 0)},)],
            ),
        }
    )
    assert verdicts_of(unused)["load"] in DEAD_OR_CLUSTER  # the file is not used by anything: the control


def test_a_top_level_property_imported_from_another_package_keeps_its_file():
    imp = "import a.SETTINGS\n" + USE_MAIN
    matches = [
        ("defs", {"def__func": USE_MAIN.rstrip(), "name": ("main", 0)}),
        ("imports", {"import": "import a.SETTINGS"}),
        ("refs", {"ref__call": "println(SETTINGS)", "name": ("println", 0)}),
        ("refs", {"ref__ref": "println(SETTINGS)", "name": ("SETTINGS", 1)}),
    ]
    facts = kt_project({"a/A.kt": (CFG_A, CFG_A_MATCHES), "u/B.kt": (imp, matches)})
    res = dlink.link(facts, workers=1)
    assert ("kotlin:u/B.kt#main", "kotlin:a/A.kt#SETTINGS", "T") in {
        (e.src, e.dst, e.tier) for e in res.graph.edges if e.kind == "ref"
    }
    v = verdicts_of(facts)
    assert v["SETTINGS"] == v["load"] == "alive"


def test_a_property_only_file_is_alive_when_its_property_is_used():
    lim = "val LIMIT = 5\n"
    facts = kt_project(
        {
            "c/C.kt": (lim, [("defs", {"def__var": "val LIMIT = 5", "name": ("LIMIT", 0)})]),
            "c/M.kt": (
                "fun main() {\n    println(LIMIT)\n}\n",
                [
                    ("defs", {"def__func": "fun main() {\n    println(LIMIT)\n}", "name": ("main", 0)}),
                    ("refs", {"ref__ref": "LIMIT", "name": ("LIMIT", 0)}),
                ],
            ),
        }
    )
    assert verdicts_of(facts)["LIMIT"] == "alive"


def test_the_initializers_of_a_file_run_with_it_even_when_nothing_names_the_property():
    """`val CONFIG = load()` is a def now, and still runs when any declaration of its file is used (the old counterexample)."""
    cfg = 'val CONFIG = load()\nfun load(): String {\n    return "x"\n}\nclass Svc {\n    fun go() {\n    }\n}\n'
    k = [
        ("defs", {"def__var": "val CONFIG = load()", "name": ("CONFIG", 0)}),
        ("defs", {"def__func": 'fun load(): String {\n    return "x"\n}', "name": ("load", 1)}),
        ("defs", {"def__class": "class Svc {\n    fun go() {\n    }\n}", "name": ("Svc", 0)}),
        ("defs", {"def__func": "fun go() {\n    }", "name": ("go", 0)}),
        ("refs", {"ref__call": "load()", "name": ("load", 0)}),
    ]
    main = "fun main() {\n    Svc()\n}\n"
    mk = [
        ("defs", {"def__func": main.rstrip(), "name": ("main", 0)}),
        ("refs", {"ref__call": "Svc()", "name": ("Svc", 0)}),
    ]
    v = verdicts_of(kt_project({"a/Cfg.kt": (cfg, k), "a/Main.kt": (main, mk)}))
    assert v["CONFIG"] == v["load"] == v["Svc"] == "alive"
    off = kt_project(
        {
            "a/Cfg.kt": (cfg, k),
            "a/Main.kt": ("fun main() {\n}\n", [("defs", {"def__func": "fun main() {\n}", "name": ("main", 0)})]),
        }
    )
    assert verdicts_of(off)["CONFIG"] in DEAD_OR_CLUSTER


def test_only_a_top_level_property_is_a_def():
    src = "val TOP = 1\nval f = { val y = 2 }\nclass Svc {\n    val field = 3\n    fun go() {\n        val local = 4\n    }\n}\n"
    m = [
        ("defs", {"def__var": "val TOP = 1", "name": ("TOP", 0)}),
        ("defs", {"def__var": "val f = { val y = 2 }", "name": ("f", 0)}),
        ("defs", {"def__var": "val y = 2", "name": ("y", 0)}),
        ("defs", {"scope": "{ val y = 2 }"}),
        (
            "defs",
            {
                "def__class": "class Svc {\n    val field = 3\n    fun go() {\n        val local = 4\n    }\n}",
                "name": ("Svc", 0),
            },
        ),
        ("defs", {"def__var": "val field = 3", "name": ("field", 0)}),
        ("defs", {"def__func": "fun go() {\n        val local = 4\n    }", "name": ("go", 0)}),
        ("defs", {"def__var": "val local = 4", "name": ("local", 0)}),
    ]
    f = kt_project({"A.kt": (src, m)})["A.kt"]
    assert {q: d.kind for q, d in defs(f).items()} == {"TOP": "var", "f": "var", "Svc": "class", "Svc.go": "method"}


def test_the_name_of_a_definition_and_the_names_of_an_import_are_no_uses():
    """A catch-all identifier query reads every identifier: the declared name and the `a.B` of an import must not become refs
    (a self-reference would be a loop that makes a dead function a cluster, an unused import would keep its class alive).
    """
    src = "package a\nimport a.B\nfun unused() {\n}\n"
    m = [
        ("defs", {"def__func": "fun unused() {\n}", "name": ("unused", 0)}),
        ("imports", {"import": "import a.B"}),
        ("imports", {"import": "package a"}),
        ("refs", {"ref__ref": "unused", "name": ("unused", 0)}),  # the name node itself
        ("refs", {"ref__ref": "a.B", "name": ("B", 0)}),  # inside the import statement
        ("refs", {"ref__ref": "a", "name": ("a", 0)}),  # inside the package statement
    ]
    f = kt_project({"A.kt": (src, m)})["A.kt"]
    assert not f.refs and [i.module for i in f.imports] == ["a.B"]
    res = dlink.link({"A.kt": f}, workers=1)
    assert not any(
        e.src == e.dst for e in res.graph.edges
    )  # no self-edge: a name node is no use of the definition it names


# ---------------------------------------------------------------- Kotlin scripts
def test_a_kotlin_script_is_an_entry_point_and_so_are_its_helpers():
    """The counterexample of the review: build.gradle.kts holds `fun helper()` and a top-level call helper()."""
    files = {
        "build.gradle.kts": "100644",
        "app/settings.gradle.kts": "100644",
        "tests/x.kts": "100644",
        "src/Main.kt": "100644",
    }
    rs = droots.resolve({}, files, lambda p: None)
    mains = [(r.node, r.strength, r.trust) for r in rs.roots if r.kind == "main"]
    assert mains == [
        ("kotlin:app/settings.gradle.kts", "hard", "convention"),
        ("kotlin:build.gradle.kts", "hard", "convention"),
    ]  # the script under tests/ is a test root, a .kt file is no script
    src = "fun helper() {\n}\nhelper()\n"
    m = [
        ("defs", {"def__func": "fun helper() {\n}", "name": ("helper", 0)}),
        ("refs", {"ref__call": "helper()", "name": ("helper", 1)}),
    ]
    facts = kt_project({"build.gradle.kts": (src, m)})
    assert ("helper", "") in refs(facts["build.gradle.kts"], "call")
    assert verdicts_of(facts, rs.roots)["helper"] == "alive"
    assert verdicts_of(facts)["helper"] in DEAD_OR_CLUSTER  # without the root: what the review saw


def test_script_extensions_are_data_of_the_language():
    lg = dlangs_load()
    assert lg.script_lang("a/build.gradle.kts") == "kotlin" and lg.script_lang("a/Main.kt") is None
    assert lg.script_lang("x.py") is None and lg.script_lang("Dockerfile") is None


def dlangs_load():
    from dead_code_checker.graph import langs as dlangs

    return dlangs.load()


# ---------------------------------------------------------------- loose packages, the Java and Kotlin groups, facades
HELPER = "class Helper {\n    fun go() {\n    }\n}\n"
HELPER_M = [
    ("defs", {"def__class": "class Helper {\n    fun go() {\n    }\n}", "name": ("Helper", 0)}),
    ("defs", {"def__func": "fun go() {\n    }", "name": ("go", 0)}),
]
LOOSE_MAIN = "package app\nimport app.util.Helper\nfun main() {\n    Helper().go()\n}\n"
LOOSE_MAIN_M = [
    ("defs", {"def__func": "fun main() {\n    Helper().go()\n}", "name": ("main", 0)}),
    ("imports", {"import": "import app.util.Helper"}),
    ("refs", {"ref__call": "Helper()", "name": ("Helper", 1)}),
]


def test_a_kotlin_package_need_not_match_its_directory():
    """Flat layout: `package app.util` in src/main/kotlin/Helper.kt, no directory app/util. The import found no directory and was
    bound to "external": the class was dead although Main.kt names it."""
    flat = {
        "src/main/kotlin/Helper.kt": ("package app.util\n" + HELPER, HELPER_M),
        "src/main/kotlin/Main.kt": (LOOSE_MAIN, LOOSE_MAIN_M),
    }
    v = verdicts_of(kt_project(flat))
    assert v["Helper"] == "alive" and v["main"] == "alive"
    res = dlink.link(kt_project(flat), workers=1)
    assert any(
        e.kind == "import" and e.dst == "kotlin:src/main/kotlin/Helper.kt" for e in res.graph.edges
    )  # the import uses its file
    flat["src/main/kotlin/Main.kt"] = (
        "fun main() {\n}\n",
        [("defs", {"def__func": "fun main() {\n}", "name": ("main", 0)})],
    )
    assert verdicts_of(kt_project(flat))["Helper"] in DEAD_OR_CLUSTER  # nothing names it: still found


def test_a_registered_kotlin_class_is_found_by_its_simple_name_when_its_package_is_no_directory():
    k = kt_project(
        {
            "src/main/kotlin/Impl.kt": (
                "package app\nclass Impl\n",
                [("defs", {"def__class": "class Impl", "name": ("Impl", 0)})],
            ),
            "src/main/kotlin/a/Other.kt": (
                "class Other\n",
                [("defs", {"def__class": "class Other", "name": ("Other", 0)})],
            ),
        }
    )
    g = dlink.link(k, workers=1).graph
    rs = (
        md.root("classreg", "META-INF/services/x.S:1", selector="class:app.Impl"),
        md.root("classreg", "x:1", selector="class:q.Other"),
    )
    bound = classreg.bind(g.nodes, rs)
    assert sorted(r.node for r in bound) == [
        "kotlin:src/main/kotlin/Impl.kt",
        "kotlin:src/main/kotlin/Impl.kt#Impl",
        "kotlin:src/main/kotlin/a/Other.kt",
        "kotlin:src/main/kotlin/a/Other.kt#Other",
    ]  # app.Impl: no directory app, found by its name; q.Other: the file is in a/ and the name decides, too
    java = {"src/main/java/Impl.java": extract("java", "src/main/java/Impl.java", "public class Impl {\n}\n")}
    assert not classreg.bind(
        dlink.link(java, workers=1).graph.nodes, rs[:1]
    )  # a Java package is its directory: no guess


def test_java_and_kotlin_see_each_others_declarations():
    ktl = kt_project(
        {
            "a/Greeter.kt": (
                "class Greeter {\n    fun hi() {\n    }\n}\n",
                [
                    ("defs", {"def__class": "class Greeter {\n    fun hi() {\n    }\n}", "name": ("Greeter", 0)}),
                    ("defs", {"def__func": "fun hi() {\n    }", "name": ("hi", 0)}),
                ],
            )
        }
    )
    jmain = main_of("        Greeter g = new Greeter();\n        g.hi();\n", "import a.Greeter;\n")
    tg.set_backend(lambda g, d: FakeJava(d.decode()))
    facts = {**ktl, "Main.java": extract("java", "Main.java", jmain)}
    v = verdicts_of(facts)
    assert v["Greeter"] == v["Greeter.hi"] == "alive"  # a Kotlin class used only from Java
    jlib = {
        "a/Greeter.java": extract(
            "java", "a/Greeter.java", "public class Greeter {\n    public void hi() {\n    }\n}\n"
        )
    }
    kmain = "import a.Greeter\nfun main() {\n    Greeter().hi()\n}\n"
    km = kt_project(
        {
            "u/Main.kt": (
                kmain,
                [
                    ("defs", {"def__func": "fun main() {\n    Greeter().hi()\n}", "name": ("main", 0)}),
                    ("imports", {"import": "import a.Greeter"}),
                    ("refs", {"ref__call": "Greeter()", "name": ("Greeter", 1)}),
                ],
            )
        }
    )
    assert verdicts_of({**jlib, **km})["Greeter"] == "alive"  # a Java class used only from Kotlin


@pytest.mark.parametrize("imported", [True, False])
def test_a_top_level_kotlin_function_called_through_its_file_facade_from_java(imported):
    util = kt_project(
        {
            "a/Util.kt": (
                "fun helper() {\n}\nfun spare() {\n}\n",
                [
                    ("defs", {"def__func": "fun helper() {\n}", "name": ("helper", 0)}),
                    ("defs", {"def__func": "fun spare() {\n}", "name": ("spare", 0)}),
                ],
            )
        }
    )
    jmain = main_of("        UtilKt.helper();\n", "import a.UtilKt;\n" if imported else "")
    tg.set_backend(lambda g, d: FakeJava(d.decode()))
    path = "a/Main.java"  # the same package: UtilKt needs no import
    v = verdicts_of({**util, path: extract("java", path, jmain)})
    assert v["helper"] == "alive"  # Util.kt is UtilKt for Java: the call names the top-level function
    assert v["spare"] in DEAD_OR_CLUSTER  # the call names one function of the file, not all of them


# ---------------------------------------------------------------- registration forms and more rules
def hint_targets(path, text):
    return sorted(t for t, _ in reg_hints(path, text))


LAYOUT = (
    '<?xml version="1.0" encoding="utf-8"?>\n<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"\n'
    '    xmlns:app="http://schemas.android.com/apk/res-auto">\n    <com.acme.CustomView android:id="@+id/v"/>\n'
    '    <fragment android:name="com.acme.HomeFragment"/>\n    <view class="com.acme.Inner"/>\n'
    '    <Button app:layout_behavior="com.acme.Behavior" android:text="@string/ok"/>\n    <!-- <com.acme.Gone/> -->\n</LinearLayout>\n'
)


def test_android_layouts_navigation_graphs_and_preferences_name_classes():
    got = hint_targets("app/src/main/res/layout/main.xml", LAYOUT)
    assert got == sorted(
        f"class:com.acme.{c}" for c in ("CustomView", "HomeFragment", "Inner", "Behavior")
    )  # a comment names none
    nav = '<navigation>\n  <fragment android:id="@+id/h" android:name="com.acme.NavHome"/>\n  <activity android:name="com.acme.Detail"/>\n  <dialog android:name="com.acme.Dlg"/>\n</navigation>\n'
    assert hint_targets("app/src/main/res/navigation/nav.xml", nav) == [
        "class:com.acme.Detail",
        "class:com.acme.Dlg",
        "class:com.acme.NavHome",
    ]
    pref = '<PreferenceScreen><Preference android:fragment="com.acme.Settings"/></PreferenceScreen>\n'
    assert hint_targets("res/xml/prefs.xml", pref) == ["class:com.acme.Settings"]
    bind = '<layout><data><variable name="vm" type="com.acme.Vm"/><import type="com.acme.Fmt"/></data></layout>\n'
    assert hint_targets("res/layout/b.xml", bind) == ["class:com.acme.Fmt", "class:com.acme.Vm"]
    assert not hint_targets("src/main/java/layout/main.xml", LAYOUT) and not hint_targets(
        "docs/layout/main.xml", LAYOUT
    )  # res/ only
    src = {
        "com/acme/CustomView.java": "public class CustomView {\n}\n",
        "com/acme/Unused.java": "public class Unused {\n}\n",
    }
    v, _ = jreg(src, {"app/src/main/res/layout/main.xml": LAYOUT})
    assert v["CustomView"] == "alive" and v["Unused"] == "dead"


def test_android_manifest_names_the_backup_agent_the_parent_activity_and_the_instrumentation():
    man = '<manifest>\n<application android:name=".App" android:backupAgent=".Agent" android:appComponentFactory="com.acme.Factory">\n<activity android:name=".Main" android:parentActivityName=".Home"/>\n</application>\n<instrumentation android:name="com.acme.Runner"/>\n</manifest>\n'
    assert hint_targets("app/src/main/AndroidManifest.xml", man) == sorted(
        f"class:{c}" for c in ("App", "Agent", "com.acme.Factory", "Main", "Home", "com.acme.Runner")
    )


def test_persistence_beans_and_faces_descriptors_name_classes():
    hib = '<hibernate-configuration><session-factory>\n<mapping class="com.acme.User"/>\n<mapping resource="a.hbm.xml"/>\n</session-factory></hibernate-configuration>\n'
    assert hint_targets("src/main/resources/hibernate.cfg.xml", hib) == ["class:com.acme.User"]
    orm = '<entity-mappings>\n<entity class="com.acme.A"/>\n<mapped-superclass class="com.acme.B"/>\n<converter class="com.acme.C"/>\n</entity-mappings>\n'
    assert hint_targets("src/main/resources/META-INF/orm.xml", orm) == [
        "class:com.acme.A",
        "class:com.acme.B",
        "class:com.acme.C",
    ]
    beans = '<beans xmlns="http://xmlns.jcp.org/xml/ns/javaee">\n<alternatives><class>com.acme.Alt</class><stereotype>com.acme.St</stereotype></alternatives>\n<interceptors><class>com.acme.Ic</class></interceptors>\n</beans>\n'
    assert hint_targets("src/main/webapp/WEB-INF/beans.xml", beans) == [
        "class:com.acme.Alt",
        "class:com.acme.Ic",
        "class:com.acme.St",
    ]
    faces = "<faces-config>\n<managed-bean><managed-bean-class>com.acme.Bean</managed-bean-class></managed-bean>\n<converter><converter-class>com.acme.Conv</converter-class></converter>\n</faces-config>\n"
    for path in ("src/main/webapp/WEB-INF/faces-config.xml", "src/main/resources/META-INF/lib.faces-config.xml"):
        assert hint_targets(path, faces) == ["class:com.acme.Bean", "class:com.acme.Conv"]


def test_class_names_in_application_properties_and_yaml_are_registrations_but_ordinary_values_are_not():
    props = (
        "server.port=8080\nspring.application.name=demo\napp.strategy=com.acme.FastStrategy\n# app.old=com.acme.Commented\n"
        "app.list=com.acme.A, com.acme.B\nlogging.level.com.acme.Noisy=DEBUG\nspring.datasource.url=jdbc:mysql://localhost:3306/db\n"
        "app.version=1.0.Final\n"
    )
    want = ["class:com.acme.A", "class:com.acme.B", "class:com.acme.FastStrategy"]
    assert (
        hint_targets("src/main/resources/application.properties", props) == want
    )  # keys and comments are no registration
    yml = "server:\n  port: 8080\napp:\n  strategy: com.acme.Strat\n  list:\n    - com.acme.One\n    - com.acme.Two # two\nspring:\n  datasource:\n    url: jdbc:mysql://localhost:3306/db\n"
    assert hint_targets("src/main/resources/application-prod.yml", yml) == [
        "class:com.acme.One",
        "class:com.acme.Strat",
        "class:com.acme.Two",
    ]
    assert not hint_targets("src/main/resources/messages.properties", props)
    v, _ = jreg(
        {"com/acme/FastStrategy.java": "public class FastStrategy {\n}\n"},
        {"src/main/resources/application.properties": props},
    )
    assert v["FastStrategy"] == "alive"


@pytest.mark.parametrize(
    "ann,rid",
    [
        ("@Converter", "jakarta.converter"),
        ("@FacesConverter", "jakarta.converter"),
        ("@ManagedBean", "jakarta.faces-bean"),
        ("@Keep", "jvm.keep"),
        ("@RegisterForReflection", "jvm.keep"),
        ("@HiltViewModel", "android.hilt"),
        ("@AndroidEntryPoint", "android.hilt"),
    ],
)
def test_more_class_rules_of_the_data(ann, rid):
    src = f"{ann}\npublic class X {{\n}}\n"
    assert ("annotation:" + rid, "X") in roots(extract("java", "p/X.java", src))
    assert jverdicts({"p/X.java": src})["X"] == "alive"
    assert ("annotation:jvm.keep-member", "X.m") in roots(
        extract("java", "p/X.java", "public class X {\n    @Keep\n    void m() {\n    }\n}\n")
    )


def test_the_registry_rules_of_wave_4_are_well_formed():
    d = json.loads(Path(lr.DATA).read_text(encoding="utf-8"))
    for r in d["class_registries"]["rules"]:
        re.compile(r["files"]), re.compile(r.get("name", "")) if r.get("name") else None
        assert r["read"] in classreg.READERS and (
            r["read"] != "dotted" or r.get("name")
        )  # a free-text file needs a class shape
        assert r.get("tags", "qualified") == "qualified"


def test_a_jar_manifest_and_a_gradle_plugin_declaration_name_classes():
    mf = "Manifest-Version: 1.0\nMain-Class: com.acme.Main\nPremain-Class: com.acme.Agent\nCreated-By: x\nClass-Path: a.jar\n"
    assert hint_targets("src/main/resources/META-INF/MANIFEST.MF", mf) == [
        "class:com.acme.Agent",
        "class:com.acme.Main",
    ]
    gr = 'gradlePlugin {\n    plugins {\n        create("x") {\n            id = "acme.x"\n            implementationClass = "com.acme.XPlugin"\n        }\n    }\n}\n'
    assert hint_targets("plugin/build.gradle.kts", gr) == ["class:com.acme.XPlugin"]
    groovy = 'gradlePlugin {\n    plugins {\n        x {\n            implementationClass = "com.acme.XPlugin"\n        }\n    }\n}\n'
    assert hint_targets("plugin/build.gradle", groovy) == ["class:com.acme.XPlugin"] and not hint_targets(
        "plugin/other.gradle", groovy
    )
    agent = (
        "public class Agent {\n    public static void premain(String a) {\n    }\n    public void spare() {\n    }\n}\n"
    )
    v, _ = jreg({"com/acme/Agent.java": agent}, {"src/main/resources/META-INF/MANIFEST.MF": mf})
    assert (
        v["Agent"] == v["Agent.premain"] == "alive" and v["Agent.spare"] in DEAD_OR_CLUSTER
    )  # premain is the class's own


@pytest.mark.parametrize("ann", ["@Preview", "@BindingAdapter", "@TypeConverter"])
def test_methods_that_tools_and_generated_code_call_are_roots(ann):
    f = extract("java", "p/V.java", f"public class V {{\n    {ann}\n    void m() {{\n    }}\n}}\n")
    assert any(k.startswith("annotation:android.") and t == "V.m" for k, t in roots(f))


@pytest.mark.parametrize("lang", ["java", "kotlin"])
def test_real_grammar_annotation_types_annotation_uses_and_top_level_properties(lang):
    """The constructs of second pass 4 on the real grammar: the @meta capture of an @interface, the reg refs of annotation uses, the
    Kotlin property def and plain identifier refs, the import and package statements."""
    why = _real_grammar(lang)
    if why:
        pytest.skip(
            f"tree-sitter grammar {lang} is not installed, trusted and verified on this host ({why}): the @meta, ref.reg, @def.var and "
            "identifier captures of the .scm queries were never run; the tests above prove the facts mapping through the fake parser only"
        )
    tg.set_backend(None)
    if lang == "java":
        f = extract("java", "p/Facade.java", FACADE["p/Facade.java"])
        assert ("annotation:spring.bean.meta", "Facade") in roots(f) and defs(f)["Facade"].kind == "iface"
        assert ("Facade", "UserFacade") in refs(
            extract("java", "p/UserFacade.java", FACADE["p/UserFacade.java"]), "reg"
        )
    else:
        f = extract("kotlin", "a/A.kt", CFG_A)
        assert defs(f)["SETTINGS"].kind == "var" and ("load", "SETTINGS") in refs(f, "call")
        assert ("annotation:spring.bean.meta", "UseCase") in roots(
            extract("kotlin", "p/UseCase.kt", KFACADE["p/UseCase.kt"][0])
        )
        assert ("UseCase", "Checkout") in refs(extract("kotlin", "p/Checkout.kt", KFACADE["p/Checkout.kt"][0]), "reg")
        b = extract("kotlin", "u/B.kt", "package u\nimport a.SETTINGS\n" + USE_MAIN)
        assert ("SETTINGS", "main") in refs(b, "ref") and ("SETTINGS", "") not in refs(b, "ref")


def test_a_kotlin_typealias_keeps_the_type_it_stands_for_alive_through_its_uses():
    """`typealias BoxAlias = Box` in a file nothing else names: a use of BoxAlias elsewhere is a use of Box."""
    alias = "class Box\ntypealias BoxAlias = Box\n"
    m = [
        ("defs", {"def__class": "class Box", "name": ("Box", 0)}),
        ("defs", {"def__class": "typealias BoxAlias = Box", "name": ("BoxAlias", 0)}),
        ("refs", {"ref__ref": "Box", "name": ("Box", 1)}),
    ]
    use = "fun main() {\n    val b: BoxAlias? = null\n}\n"
    um = [
        ("defs", {"def__func": use.rstrip(), "name": ("main", 0)}),
        ("refs", {"ref__ref": "BoxAlias", "name": ("BoxAlias", 0)}),
    ]
    facts = kt_project({"a/Alias.kt": (alias, m), "a/Main.kt": (use, um)})
    assert defs(facts["a/Alias.kt"])["BoxAlias"].kind == "class" and ("Box", "BoxAlias") in refs(
        facts["a/Alias.kt"], "ref"
    )
    v = verdicts_of(facts)
    assert v["BoxAlias"] == v["Box"] == "alive"


# ====================================================================== counterexamples of an earlier re-check (real grammar)
def _link_real(files, lang_of, entry):
    """{key short: verdict} of files {path: source} extracted with the REAL grammar; skips with the reason when it is missing."""
    for lang in set(lang_of.values()):
        why = _real_grammar(lang)
        if why:
            pytest.skip(f"tree-sitter grammar {lang} is not installed, trusted and verified on this host ({why})")
    tg.set_backend(None)
    facts = {p: extract(lang_of[p], p, text) for p, text in files.items()}
    res = dlink.link(facts, {"(roots)": [md.RootHint("main", f"file:{entry}", 1)]}, workers=1)
    an = reach.analyze(res.graph)
    return {n.key.split(":", 1)[1]: an.verdict(n.key) for n in res.graph.nodes if n.kind != "file"}, facts


KT_RECV = "class Foo {\n  fun bar() {}\n  fun spare() {}\n}\n"
KT_IMPLICIT = {
    "extension-function": KT_RECV + "fun Foo.baz() { bar() }\nfun main() { Foo().baz() }\n",
    "apply": KT_RECV + "fun main() { Foo().apply { bar() } }\n",
    "with": KT_RECV + "fun main() {\n  val foo = Foo()\n  with(foo) { bar() }\n}\n",
    "builder": "class Foo {\n  fun bar() {}\n  fun spare() {}\n}\nfun config(b: Foo.() -> Unit) { Foo().b() }\nfun main() { config { bar() } }\n",
    "run-let": KT_RECV + "fun main() { Foo().run { bar() } }\n",
}


@pytest.mark.parametrize("case", sorted(KT_IMPLICIT))
def test_kotlin_a_member_called_through_an_implicit_receiver_is_not_dead(case):
    """Counterexample: `fun Foo.baz() { bar() }` and lambdas with receiver (apply, with, run, a DSL builder) call Foo.bar by its bare name."""
    got, _ = _link_real({"a.kt": KT_IMPLICIT[case]}, {"a.kt": "kotlin"}, "a.kt")
    assert got["a.kt#Foo.bar"] == "weak-alive"  # N: the receiver is not typed
    assert got["a.kt#Foo.spare"] == "dead"  # a member nobody calls stays dead
    assert got["a.kt#Foo"] == "alive"


def test_kotlin_a_bare_call_that_a_top_level_function_answers_still_reaches_it():
    got, _ = _link_real(
        {"a.kt": "fun helper() {}\nfun other() {}\nfun main() { helper() }\n"}, {"a.kt": "kotlin"}, "a.kt"
    )
    assert got["a.kt#helper"] == "alive" and got["a.kt#other"] == "dead"


JAVA_PLUGIN = "package p;\npublic class Plugin {\n  public Plugin() { helper(); }\n  private void helper() {}\n  private void spare() {}\n}\n"
KT_PLUGIN = (
    "package p\nclass Plugin {\n  constructor() { helper() }\n  private fun helper() {}\n  private fun spare() {}\n}\n"
)
JAVA_MAIN = (
    "package p;\npublic class Main {\n  public static void main(String[] a) throws Exception {\n    %s\n  }\n}\n"
)
KT_MAIN = "package p\nfun main() {\n  %s\n}\n"
FOR_NAME = {  # reflection by a string literal: (language, call, class)
    "java-forName": ("java", 'Object o = Class.forName("p.Plugin").getDeclaredConstructor().newInstance();', "Plugin"),
    "java-loadClass": ("java", 'Class<?> c = Main.class.getClassLoader().loadClass("p.Plugin");', "Plugin"),
    "java-findClass": ("java", 'Class<?> c = loader.findClass("p.Plugin");', "Plugin"),
    "kt-forName": ("kotlin", 'val o = Class.forName("p.Plugin").getDeclaredConstructor().newInstance()', "Plugin"),
    "kt-loadClass": ("kotlin", 'val c = loader.loadClass("p.Plugin")', "Plugin"),
}


@pytest.mark.parametrize("case", sorted(FOR_NAME))
def test_a_class_loaded_by_a_string_literal_is_a_dynamic_site_not_a_dead_class(case):
    lang, stmt, cls = FOR_NAME[case]
    ext = "java" if lang == "java" else "kt"
    main, plugin = (JAVA_MAIN, JAVA_PLUGIN) if lang == "java" else (KT_MAIN, KT_PLUGIN)
    files = {f"p/Main.{ext}": main % stmt, f"p/Plugin.{ext}": plugin}
    got, facts = _link_real(files, dict.fromkeys(files, lang), f"p/Main.{ext}")
    assert [d.pattern for f in facts.values() for d in f.dyn] == [f"name:{cls}"]
    assert got[f"p/Plugin.{ext}#Plugin"] == "weak-alive" and got[f"p/Plugin.{ext}#Plugin.helper"] == "weak-alive"
    assert got[f"p/Plugin.{ext}#Plugin.spare"] == "dead"  # only what the class does is alive


def test_reflection_by_a_computed_name_or_a_member_name_and_a_nested_class():
    main = (
        JAVA_MAIN % 'Object a = Class.forName(args[0]); Object b = Class.forName("p.Plugin$Inner"); m.getMethod("run");'
    )
    plugin = "package p;\npublic class Plugin {\n  void spare() {}\n  public static class Inner {\n    public void run() {}\n    void idle() {}\n  }\n}\n"
    got, facts = _link_real(
        {"p/Main.java": main.replace("args[0]", "a[0]"), "p/Plugin.java": plugin},
        dict.fromkeys(("p/Main.java", "p/Plugin.java"), "java"),
        "p/Main.java",
    )
    assert sorted(d.pattern for d in facts["p/Main.java"].dyn) == [
        "name:Inner",
        "name:run",
    ]  # the computed name is no site
    assert got["p/Plugin.java#Plugin.Inner"] == "weak-alive" and got["p/Plugin.java#Plugin.Inner.run"] == "weak-alive"
    assert got["p/Plugin.java#Plugin"] == "weak-alive"  # a nested class loaded by name keeps its outer class
    assert got["p/Plugin.java#Plugin.spare"] == "dead" and got["p/Plugin.java#Plugin.Inner.idle"] == "dead"


def test_a_reflection_call_with_no_literal_argument_makes_no_site():
    main = JAVA_MAIN % "Object o = Class.forName(a[0]);"
    got, facts = _link_real(
        {"p/Main.java": main, "p/Plugin.java": JAVA_PLUGIN},
        dict.fromkeys(("p/Main.java", "p/Plugin.java"), "java"),
        "p/Main.java",
    )
    assert not facts["p/Main.java"].dyn and got["p/Plugin.java#Plugin"] in ("dead", "dead-cluster")
