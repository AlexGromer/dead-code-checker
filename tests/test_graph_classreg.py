"""graph.classreg: classes that registration files name (ServiceLoader, module-info, Spring factories, web.xml) and how they
become roots of the class nodes of the linked graph; graph.extract.lang_rules: the annotation rules as data.

python3 -m pytest -q tests/test_graph_classreg.py
"""

import json

import pytest

from dead_code_checker.graph import classreg, contracts, langs, model as md, roots
from dead_code_checker.graph.extract import lang_rules as lr

SERVICES = "src/main/resources/META-INF/services/p.Plugin"


def reg_hints(path, text):
    return [(h.target, h.line) for h in roots.extract_hints(path, text) if h.kind == "classreg"]


# ================================================================ the readers
def test_a_services_file_is_read_line_by_line_with_comments():
    text = "﻿# the providers\n\np.A   # first\n  p.B\n#p.C\nnot a class!\n"
    assert reg_hints(SERVICES, text) == [("class:p.A", 3), ("class:p.B", 4)]
    assert reg_hints("lib/META-INF/services/x.Y", "q.Z\n") == [("class:q.Z", 1)] and not reg_hints(
        "lib/services/x.Y", "q.Z\n"
    )


def test_module_info_provides_with_names_every_provider_and_skips_comments():
    text = "module app {\n    // provides p.Plugin with p.Gone;\n    requires java.base;\n    provides p.Plugin\n        with p.Impl, /* p.Gone */ p.Extra;\n    uses p.Plugin;\n}\n"
    assert reg_hints("module-info.java", text) == [("class:p.Extra", 5), ("class:p.Impl", 5)]
    assert not reg_hints("src/Main.java", text)


def test_spring_factories_and_web_xml_name_classes():
    fac = "# auto-configuration\nx.EnableAutoConfiguration=\\\n  p.AutoA,\\\n  p.AutoB\nx.Listener = p.Listener\n"
    assert reg_hints("META-INF/spring.factories", fac) == [
        ("class:p.AutoA", 2),
        ("class:p.AutoB", 2),
        ("class:p.Listener", 5),
    ]
    web = "<web-app>\n<servlet><servlet-class>p.Servlet</servlet-class></servlet>\n<!-- <servlet-class>p.Gone</servlet-class> -->\n</web-app>"
    assert reg_hints("WEB-INF/web.xml", web) == [("class:p.Servlet", 2)]


def test_a_bean_xml_names_its_init_method_with_the_class():
    xml = '<beans>\n<bean class="p.Svc" init-method="start" destroy-method="stop"/>\n</beans>'
    got = {t for t, _ in reg_hints("src/main/resources/beans.xml", xml)} | {
        t for t, _ in reg_hints("WEB-INF/applicationContext.xml", xml)
    }
    assert got <= {"class:p.Svc", "class:p.Svc#start", "class:p.Svc#stop"}


def test_the_rules_are_data_and_a_changed_rule_changes_the_shard_version(monkeypatch):
    rx = next(r for r in contracts.root_extractors() if r.name == "classreg")
    d = json.loads(open(lr.DATA, encoding="utf-8").read())
    assert all(r["read"] in classreg.READERS and r["files"] and r["why"] for r in d["class_registries"]["rules"])
    assert rx.ext_ver.startswith("classreg@1.") and rx.matches(SERVICES) and not rx.matches("src/Main.java")
    before, real = rx.ext_ver, classreg._load(lr.DATA)
    monkeypatch.setattr(classreg, "_load", lambda path: (real[0], "changed1"))  # another rule table: another hash
    assert rx.ext_ver != before and rx.ext_ver.endswith(".changed1")


# ================================================================ binding to the nodes of the graph
def _node(qual, kind="class", lang="java", path="p/Impl.java"):
    return md.node(md.node_key(lang, path, qual), kind, line0=1)


def _classes(lang="java"):
    return [
        _node("Impl"),
        _node("Impl.run", "method"),
        _node("Other", path="q/Other.java"),
        _node("Impl", lang=lang, path="v/Impl.java"),
    ]


def test_a_class_selector_becomes_a_root_of_the_class_and_its_file():
    sel = md.root("classreg", "r:1", selector="class:p.Impl")
    main = md.root("main", "m:1", node="java:p/Impl.java")
    got = classreg.bind(_classes(), [sel, main])
    assert main in got and sel not in got
    assert sorted((r.node, r.origin) for r in got if r.kind == "classreg") == [
        ("java:p/Impl.java", "r:1"),
        ("java:p/Impl.java#Impl", "r:1"),
    ]
    # a package is its directory: v/Impl.java is the class v.Impl, not p.Impl


def test_a_method_selector_roots_the_methods_of_that_name_and_a_library_class_gives_no_root():
    got = classreg.bind(_classes(), [md.root("classreg", "r:2", selector="class:p.Impl#run")])
    assert sorted(r.node for r in got) == ["java:p/Impl.java", "java:p/Impl.java#Impl.run"]
    assert classreg.bind(_classes(), [md.root("classreg", "r:3", selector="class:lib.Gone")]) == ()
    assert classreg.bind(_classes(), [md.root("classreg", "r:4", selector="class:p.Impl#nomethod")]) == ()
    assert classreg.bind([], ()) == ()


def test_a_nested_class_and_a_dollar_name_match_the_package_and_nesting():
    nodes = [_node("Outer", path="p/Outer.java"), _node("Outer.Inner", path="p/Outer.java")]
    got = classreg.bind(nodes, [md.root("classreg", "r:5", selector="class:p.Outer.Inner")])
    assert "java:p/Outer.java#Outer.Inner" in {r.node for r in got}


def test_the_main_and_test_source_sets_of_a_package_are_one_scope():
    nodes = [_node("Impl", path="app/src/main/java/p/Impl.java")]
    got = classreg.bind(nodes, [md.root("classreg", "r:6", selector="class:p.Impl")])
    assert "java:app/src/main/java/p/Impl.java#Impl" in {r.node for r in got}


def test_a_kotlin_class_is_found_by_its_simple_name_when_its_package_is_no_directory():
    nodes = [_node("Impl", lang="kotlin", path="src/Impl.kt")]
    assert langs.load().langs["kotlin"].get("package_loose")
    got = classreg.bind(nodes, [md.root("classreg", "r:7", selector="class:some.where.Impl")])
    assert "kotlin:src/Impl.kt#Impl" in {r.node for r in got}
    assert (
        classreg.bind([_node("Impl")], [md.root("classreg", "r:8", selector="class:some.where.Impl")]) == ()
    )  # java: strict


def test_a_build_selector_roots_the_nodes_of_that_qual_and_none_is_no_root_and_no_finding():
    nodes = [
        md.node(md.node_key("make", "Makefile", "recipe:test"), "job", line0=1),
        md.node(md.node_key("make", "sub/Makefile", "recipe:test"), "job", line0=1),
    ]
    got = classreg.bind(
        nodes, [md.root("doc-cmd", "README.md:3", selector="build:recipe:test", strength="soft", trust="documented")]
    )
    assert sorted(r.node for r in got) == [
        "make:Makefile",
        "make:Makefile#recipe:test",
        "make:sub/Makefile",
        "make:sub/Makefile#recipe:test",
    ]
    assert all(r.kind == "doc-cmd" and r.strength == "soft" for r in got)
    assert classreg.bind(nodes, [md.root("doc-cmd", "README.md:4", selector="build:recipe:gone")]) == ()


# ================================================================ lang_rules: the data half
def test_pkg_dir_strips_the_source_set_part_of_a_path():
    assert (
        lr.pkg_dir("java", "app/src/main/java/p/X.java") == "app/p" == lr.pkg_dir("java", "app/src/test/java/p/X.java")
    )
    assert lr.pkg_dir("kotlin", "src/test/kotlin/q/Y.kt") == lr.pkg_dir("kotlin", "src/main/kotlin/q/Z.kt")
    assert lr.pkg_dir("python", "app/src/main/java/p/X.py") == "app/src/main/java/p"  # a language with no such rule
    assert lr.pkg_dir("java", "p/X.java") == "p"


def test_get_returns_the_rules_of_a_language_that_has_some():
    rl = lr.get("java")
    assert rl is not None and rl.src_root is not None and rl.lang == "java" and "readObject" in rl.protocol
    assert lr.get("python") is None and lr.get("kotlin").meta_mods


def test_rules_by_id_and_the_bean_kinds():
    r = lr.rule("spring.boot-app")
    assert r is not None and r["on"] == "class" and lr.rule("no.such.rule") is None
    kinds = lr.bean_kinds()
    assert kinds and all(k.startswith("annotation:") for k in kinds) and "annotation:micronaut.bean" in kinds


def test_meta_rule_names_the_rule_of_an_annotation_type_hint():
    assert lr.meta_rule("annotation:spring.service.meta") == "spring.service"
    assert (
        lr.meta_rule("annotation:.meta") == "" and lr.meta_rule("annotation:x") is None and lr.meta_rule("main") is None
    )
    assert lr.SIDE["iface"] == "class" and lr.SIDE["func"] == "method"


def test_inherit_closes_the_rule_ids_over_a_chain_of_annotation_types_and_survives_a_cycle():
    assert lr.inherit({"A": ["r1"], "B": ["r2"]}, {"C": ["A"], "A": ["B"], "B": ["A"]}) == {
        "A": ("r1", "r2"), "B": ("r1", "r2"), "C": ("r1", "r2")}  # fmt: skip
    assert lr.inherit({"S": ["service"]}, {"Facade": ["S"], "UseCase": ["Facade"]}) == {
        "S": ("service",), "Facade": ("service",), "UseCase": ("service",)}  # fmt: skip
    assert lr.inherit({}, {}) == {}


@pytest.mark.parametrize("lang", ["java", "kotlin"])
def test_every_framework_a_language_names_exists_and_has_rules(lang):
    d = json.loads(open(lr.DATA, encoding="utf-8").read())
    for fwk in d["lang_rules"][lang]["frameworks"]:
        rules = d["frameworks"][fwk]["rules"]
        assert rules and all(r["id"] and r["on"] in ("class", "method", "param") and r["why"] for r in rules), fwk
