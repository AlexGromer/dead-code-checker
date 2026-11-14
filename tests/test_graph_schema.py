"""The schema languages (SQL, protobuf, GraphQL): graph.extract.schema, the SQL scan of graph.extract.xlang, the
annotation rules of graph.extract.lang_rules (JPA, Spring GraphQL, DGS), the link side (graph.xlink: names, globs, generated proto stubs, resolver classes), the
soft root of a migration (graph.roots) and the verdict of an unused object (at most W, the confirmation rule of graph.verdict).

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_schema.py

Every fixture is synthetic text written here (no project content). The real tree-sitter grammars are read through graph.ts; a test that needs one
skips with its reason when tree-sitter or the grammar is not installed. Java and Python are read like in a real run.
"""

import ast
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test

ROOT = Path(__file__).resolve().parent.parent
import real_grammar as unlocked  # noqa: E402
from dead_code_checker.graph import contracts, levels, link, model as md, reach, verdict  # noqa: E402
from dead_code_checker.graph.extract import schema, ts_generic as tg, xlang  # noqa: E402
from dead_code_checker.graph.verdict import Proof  # noqa: E402
from graph_support import commit_all, git, write  # noqa: E402
from dead_code_checker.graph.model import Finding  # noqa: E402

SCHEMA_LANGS = ("sql", "proto", "graphql")


@pytest.fixture
def grammars():
    """The real grammars of the schema languages and Java, or a skip with the reason; the extractors read them through the backend seam."""
    for g in (*SCHEMA_LANGS, "java"):
        why = unlocked.available(g)
        if why:
            pytest.skip(why)
    tg.set_backend(unlocked.backend)
    yield
    tg.set_backend(None)


def facts(lang, text):
    return contracts.get_extractor(lang).extract(lang, "x", text)


def refs(f, kind="schema"):
    return sorted((r.name, r.scope) for r in f.refs if r.kind == kind)


def make(tmp_path, files, exe=("app.py",)):
    """A git repository with `files` ({path: text}); the files in `exe` carry the exec bit, so their shebang makes them a hard root."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    for rel, text in files.items():
        write(r, rel, text, 0o755 if rel in exe else None)
    commit_all(r, "base")
    return r


def analyze(r):
    b = levels.build(str(r), None)
    return b, reach.analyze(b.graph, "closed_world")


def verdicts(b, res, lang=None):
    """{qual: verdict} of the schema objects (the nodes that are no file)."""
    return {
        levels._qual(n.key): res.verdict(n.key)
        for n in b.graph.nodes
        if n.lang == (lang or n.lang) and n.lang in SCHEMA_LANGS and n.kind != "file"
    }


def node_of(b, qual):
    return next(n for n in b.graph.nodes if levels._qual(n.key) == qual)


def findings(r, b, res, proof=Proof()):
    """(level, code, name) of the dead-symbol claims of the whole graph, each settled by the corroboration rule like the branch level does
    (W with a proof that names the node, else a hidden dc.suspect)."""
    out = []
    for n in b.graph.nodes:
        v = res.verdict(n.key)
        if v not in ("dead", "dead-cluster") or not levels.reportable(n, res):
            continue
        code = "dc.dead-cluster" if v == "dead-cluster" else "dc.dead"
        f = Finding("W", n.path, n.line0, code, f"`{levels._qual(n.key)}` is dead")
        out.append(verdict.settle(f, proof.text(n.key, n.path), verdict.claims(n.lang)))
    return sorted((f.level, f.code, f.msg.split("`")[1]) for f in out)


APP = "#!/usr/bin/env python3\nimport dao\n\n\ndef main():\n    return dao.load_orders(None)\n\n\nmain()\n"
DAO = "def load_orders(db):\n" '    return db.run("SELECT o.id FROM orders o JOIN items i ON i.order_id = o.id")\n'
TABLES = """\
CREATE TABLE orders (id int PRIMARY KEY, customer_id int REFERENCES customers(id));
CREATE TABLE customers (id int PRIMARY KEY);
CREATE TABLE items (id int, order_id int);
CREATE TABLE public.audit_log (id int);
CREATE TABLE audit_user (id int);
CREATE TABLE legacy (id int);
CREATE VIEW open_orders AS SELECT * FROM orders;
"""


# ================================================================ the SQL scan (no grammar)
@pytest.mark.parametrize(
    "text,names",
    [
        ("SELECT o.id FROM orders o JOIN items i ON i.order_id = o.id", ["items", "orders"]),
        ("select * from a x, b y where x.id = y.id", ["a", "b"]),
        ("INSERT INTO public.orders (id) VALUES (1)", ["public.orders"]),
        ('UPDATE "Audit"."log" SET a = 1', ["Audit.log"]),
        ("DELETE FROM orders WHERE id IN (SELECT id FROM legacy)", ["legacy", "orders"]),
        ("WITH recent AS (SELECT * FROM events) SELECT * FROM recent JOIN users USING (id)", ["events", "users"]),
        ("SELECT * FROM audit_%s", ["audit_*"]),
        ("SELECT * FROM audit_${kind}", ["audit_*"]),
        ("SELECT * FROM ", ["*"]),
        ("SELECT * FROM {table} WHERE x = 1", ["*"]),
        ("SELECT * FROM t_", ["t_*"]),
        ("CALL refresh_totals(1)", ["refresh_totals"]),
        ("SELECT 'from nowhere' AS x -- from nothing\nFROM real_one /* join ghost */", ["real_one"]),
        ("SELECT updated_at, intro FROM t JOIN joined j ON 1 = 1", ["joined", "t"]),  # a keyword is no start of a word
    ],
)
def test_sql_names_of_a_statement(text, names):
    assert xlang.sql_names(text) == names


def test_sql_names_of_a_schema_file_include_the_ddl_words():
    ddl = "CREATE TABLE a (b int REFERENCES c(id)); ALTER TABLE IF EXISTS ONLY d ADD x int; TRUNCATE TABLE e; CREATE TRIGGER t AFTER INSERT ON a EXECUTE FUNCTION f();"
    assert xlang.sql_names(ddl, ddl=True) == ["c", "d", "e", "f"]
    assert xlang.sql_names(ddl) == ["f"]  # without the ddl words only a call is read
    assert xlang.sql_literal(f'"{ddl}"') == []  # and in code a string that is no DML statement is no query at all


@pytest.mark.parametrize(
    "token,names",
    [
        ('"SELECT * FROM orders"', ["sql:orders"]),
        ('"select id from public.items where id = ?"', ["sql:public.items"]),
        ('"""\n  SELECT 1\n  FROM orders\n"""', ["sql:orders"]),  # a Java text block
        ("`SELECT * FROM ${table}`", ["sql:*"]),  # computed: a glob
        ('"SELECT * FROM audit_"', ["sql:audit_*"]),
        ('"Hello world, we select from the menu"', []),  # prose is no statement
        ('"Update the cache from "', ["sql:the"]),  # starts like one, and the only name is no table of anything
        ('"select from "', []),  # a lower-case sentence that ends in `from` is no computed name
        ('"UPDATE x"', ["sql:x"]),
        ('"CREATE TABLE a (id int)"', []),  # DDL is no DML statement
        (
            '"FROM orders o "',
            ["sql:orders"],
        ),  # the part of a statement that starts with a clause (what a StringBuilder appends)
        ('" LEFT OUTER JOIN items i ON 1 = 1"', ["sql:items"]),
        ('"Choose from the menu"', []),
        ('"Joined the club"', []),
    ],
)
def test_sql_string_literals(token, names):
    assert xlang.sql_literal(token) == names


def test_the_data_is_consistent():
    d = xlang.data()["schema"]
    assert {"sql", "proto", "graphql"} == set(d["groups"]) and "schema" in md.EDGE_KINDS
    assert {fam for fam, f in xlang.data()["families"].items() if f.get("schema_sql")} == {
        "python",
        "go",
        "js",
        "java",
        "kotlin",
        "csharp",
        "rust",
        "cpp",
    }
    q = xlang.data()["families"]  # the node types that join operands are data of the tree-sitter families
    assert {f: set(q[f]["concat"]) for f in q if q[f].get("concat")} == {
        "go": {"binary_expression"},
        "js": {"binary_expression"},
        "java": {"binary_expression"},
        "kotlin": {"additive_expression"},
        "csharp": {"binary_expression"},
        "cpp": {"concatenated_string"},
    }
    assert d["sql"]["fold_max"] > d["sql"]["max"] > d["sql"]["min"] > 0
    for lang in SCHEMA_LANGS:  # the schema languages are data languages that the schema extractor reads
        spec = contracts._langs.load().info(lang)
        assert spec["role"] == "data" and spec["extractor"] == "schema"
    assert verdict.schema_lang("sql") and not verdict.schema_lang("python")


# ================================================================ the extractor
def test_sql_definitions_and_references(grammars):
    f = facts(
        "sql",
        "CREATE TABLE public.orders (id int, c int REFERENCES customers(id));\n"
        'CREATE TABLE IF NOT EXISTS "Audit"."log" (id int);\n'
        "CREATE OR REPLACE VIEW v AS SELECT * FROM orders JOIN items ON 1 = 1;\n"
        "CREATE MATERIALIZED VIEW mv AS SELECT 1;\n"
        "CREATE FUNCTION total() RETURNS int AS $$ SELECT count(*) FROM orders $$ LANGUAGE sql;\n"
        "CREATE PROCEDURE p() LANGUAGE sql AS $$ INSERT INTO log VALUES (1) $$;\n"
        "CREATE INDEX i ON orders (id);\n"
        "ALTER TABLE orders ADD COLUMN note text;\n",
    )
    assert f.errors == 0
    assert [(d.qual, d.kind, d.line0) for d in f.defs if d.qual] == [
        ("table:public%2Eorders", "class", 1),
        ("table:Audit%2Elog", "class", 2),
        ("view:v", "class", 3),
        ("view:mv", "class", 4),
        ("function:total", "func", 5),
        ("procedure:p", "func", 6),
    ]
    assert refs(f) == [
        ("sql:customers", "table:public%2Eorders"),
        ("sql:items", "view:v"),
        ("sql:log", "procedure:p"),
        ("sql:orders", ""),  # the ALTER: from the file (CREATE INDEX is part of the table, it names no use)
        ("sql:orders", "function:total"),
        ("sql:orders", "view:v"),
    ]


def test_proto_definitions_and_references(grammars):
    f = facts(
        "proto",
        'syntax = "proto3";\npackage acme.v1;\nmessage Order {\n  Customer c = 1;\n  repeated acme.v1.Item items = 2;\n  map<string, Tag> tags = 3;\n'
        "  message Inner { int32 x = 1; }\n  enum Kind { A = 0; }\n}\nenum Status { S = 0; }\n"
        "service Shop {\n  rpc Get (GetReq) returns (Order);\n  rpc Watch (stream GetReq) returns (stream Order) {}\n}\n",
    )
    assert f.errors == 0
    assert [(d.qual, d.kind) for d in f.defs if d.qual] == [
        ("message:acme%2Ev1%2EOrder", "class"),
        ("message:acme%2Ev1%2EOrder%2EInner", "class"),
        ("enum:acme%2Ev1%2EOrder%2EKind", "class"),
        ("enum:acme%2Ev1%2EStatus", "class"),
        ("service:acme%2Ev1%2EShop", "class"),
        ("rpc:acme%2Ev1%2EShop%2EGet", "func"),
        ("rpc:acme%2Ev1%2EShop%2EWatch", "func"),
    ]
    got = dict.fromkeys(refs(f))
    order, svc = "message:acme%2Ev1%2EOrder", "service:acme%2Ev1%2EShop"
    for want in (
        ("proto:Customer", order),
        ("proto:acme.v1.Item", order),
        ("proto:Tag", order),
        ("proto:acme.v1.Order.Inner", order),  # a nested type lives with its owner
        ("proto:acme.v1.Shop.Get", svc),  # an rpc with its service
        ("proto:GetReq", "rpc:acme%2Ev1%2EShop%2EGet"),
        ("proto:Order", "rpc:acme%2Ev1%2EShop%2EWatch"),
    ):
        assert want in got, want


def test_graphql_definitions_and_references(grammars):
    f = facts(
        "graphql",
        "schema { query: Root }\ntype Root { books(f: Filter): [Book!]! node: Node }\nextend type Root { more: Int }\ntype Mutation { add(b: BookIn!): Book }\n"
        "type Book implements Node & Titled { id: ID! author: Author }\ninterface Node { id: ID! }\ninput BookIn { t: String }\nenum Genre { A }\n"
        "union Found = Book | Author\nscalar Date\n",
    )
    assert f.errors == 0
    assert sorted((d.qual, d.kind) for d in f.defs if d.qual) == sorted(
        [
            ("type:Root", "class"),
            ("field:Root%2Ebooks", "func"),  # the type of the schema block is a root operation type
            ("field:Root%2Enode", "func"),
            ("field:Root%2Emore", "func"),  # the field of an `extend type` too
            ("type:Mutation", "class"),
            ("field:Mutation%2Eadd", "func"),
            ("type:Book", "class"),
            ("interface:Node", "class"),
            ("input:BookIn", "class"),
            ("enum:Genre", "class"),
            ("union:Found", "class"),
        ]
    )  # no field of Book (not a root type), no scalar
    got = dict.fromkeys(refs(f))
    for want in (
        ("graphql:Filter", "field:Root%2Ebooks"),
        ("graphql:Book", "field:Root%2Ebooks"),
        ("graphql:Node", "field:Root%2Enode"),
        ("graphql:~Node", "type:Book"),  # `implements`: the interface reaches its implementer too
        ("graphql:~Titled", "type:Book"),
        ("graphql:Author", "type:Book"),
        ("graphql:Book", "union:Found"),
        ("graphql:BookIn", "field:Mutation%2Eadd"),
    ):
        assert want in got, want
    assert not [r for r in f.refs if r.name in ("graphql:String", "graphql:ID")]  # a builtin scalar is no object
    assert sorted((r.kind, r.target, r.strength) for r in f.roots) == [
        ("init", "type:Mutation", "soft"),  # a root operation type: the clients call it
        ("init", "type:Root", "soft"),  # the type that the schema block names
    ]


def test_the_schema_block_names_the_root_types(grammars):
    f = facts("graphql", "schema { query: RootQuery }\ntype RootQuery { a: Int }\ntype Query { b: Int }\n")
    assert sorted(r.target for r in f.roots) == [
        "type:Query",
        "type:RootQuery",
    ]  # the defaults stay root types, the block adds one


def test_a_generated_header_flags_every_definition(grammars):
    head = "// Generated by the protocol buffer compiler.  DO NOT EDIT!\n"
    gen = facts("proto", head + 'syntax = "proto3";\nmessage A {}\n')
    own = facts("proto", 'syntax = "proto3";\nmessage A {}\n')
    assert [d.flags & md.F_GENERATED for d in gen.defs if d.qual] == [md.F_GENERATED]
    assert [d.flags & md.F_GENERATED for d in own.defs if d.qual] == [0]
    assert gen.errors == 0 and own.errors == 0


def test_the_facts_are_a_function_of_the_text(grammars):
    text = "CREATE TABLE a (id int);\nCREATE VIEW v AS SELECT * FROM a;\n"
    ex = contracts.get_extractor("sql")
    assert ex.extract("sql", "x/one.sql", text).to_payload() == ex.extract("sql", "y/two.sql", text).to_payload()
    assert ex.grammar_sha("sql") == ex.grammar_sha("sql") and ex.grammar_sha("sql") != ex.grammar_sha("proto")


# ================================================================ code that names tables
JAVA_APP = "package p;\npublic class App {\n    public static void main(String[] a) {\n        new Repo().find();\n        new Dyn().by(a[0]);\n    }\n}\n"
JAVA_REPO = 'package p;\npublic class Repo {\n    String find() {\n        return "SELECT id FROM public.audit_log WHERE id = ?";\n    }\n}\n'
JAVA_DYN = (
    'package p;\npublic class Dyn {\n    String by(String k) {\n        return "SELECT * FROM audit_" + k;\n    }\n}\n'
)


def test_schema_edges(grammars, tmp_path):
    """Code that names a table by a string literal reaches it: a Python and a Java user, a view that reads a table, a foreign key; the table
    that nothing names stays dead."""
    r = make(
        tmp_path,
        {
            "app.py": APP,
            "dao.py": DAO,
            "db/tables.sql": TABLES,
            "src/main/java/p/App.java": JAVA_APP.replace("        new Dyn().by(a[0]);\n", ""),
            "src/main/java/p/Repo.java": JAVA_REPO,
        },
    )
    b, res = analyze(r)
    got = verdicts(b, res)
    assert got["table:orders"] == got["table:items"] == "weak-alive"  # the Python literal (N)
    assert got["table:public%2Eaudit_log"] == "weak-alive"  # the Java literal, a schema-qualified name
    assert got["table:customers"] == "weak-alive"  # the foreign key of a used table
    assert (got["table:legacy"], got["view:open_orders"], got["table:audit_user"]) == ("dead",) * 3
    edges = {(levels._qual(e.src), levels._qual(e.dst), e.tier) for e in b.graph.edges if e.kind == "schema"}
    assert ("main", "table:orders", "N") not in edges and ("load_orders", "table:orders", "N") in edges
    assert ("Repo.find", "table:public%2Eaudit_log", "N") in edges
    assert ("table:orders", "table:customers", "T") in edges and ("view:open_orders", "table:orders", "T") in edges
    assert b.findings == () and b.missing == {} and b.unsupported == ()


def test_a_name_is_matched_by_the_end_of_the_qualified_name_and_ignores_case(grammars, tmp_path):
    r = make(
        tmp_path,
        {
            "app.py": "#!/usr/bin/env python3\nimport dao\n\n\ndef main():\n    return dao.a() + dao.b() + dao.c()\n\n\nmain()\n",
            "dao.py": 'def a():\n    return run("SELECT * FROM ORDERS")\n\n\ndef b():\n    return run("SELECT * FROM public.items")\n\n\n'
            'def c():\n    return run("SELECT * FROM dbo.legacy")\n',
            "db/t.sql": 'CREATE TABLE public.Orders (id int);\nCREATE TABLE items (id int);\nCREATE TABLE "other"."legacy2" (id int);\nCREATE TABLE legacy (id int);\n',
        },
    )
    b, res = analyze(r)
    assert verdicts(b, res) == {
        "table:public%2EOrders": "weak-alive",  # the same name, any case, the schema of the definition written or not
        "table:items": "weak-alive",  # a name qualified more than the definition
        "table:Other%2Elegacy2".replace("Other", "other"): "dead",
        "table:legacy": "weak-alive",
    }


@pytest.mark.parametrize(
    "dao,alive,dead",
    [
        # a name with a computed tail reaches the objects that fit it, and only those
        (
            'def load(db, kind):\n    return db.run(f"SELECT * FROM audit_{kind}")\n',
            {"table:audit_user", "table:public%2Eaudit_log"},
            {"table:legacy"},
        ),
        # a name that is fully computed can be any of them
        (
            'def load(db, name):\n    return db.run("SELECT * FROM " + name)\n',
            {"table:audit_user", "table:public%2Eaudit_log", "table:legacy", "table:orders"},
            set(),
        ),
    ],
)
def test_dynamic_sql_is_never_dead(grammars, tmp_path, dao, alive, dead):
    """A table name built at run time is a dynamic site (tier D): the objects it can name are reached, flagged dynamic and never reported as dead."""
    r = make(
        tmp_path,
        {
            "app.py": "#!/usr/bin/env python3\nimport dao\n\n\ndef main():\n    return dao.load(None, 'x')\n\n\nmain()\n",
            "dao.py": dao,
            "db/t.sql": TABLES,
        },
    )
    b, res = analyze(r)
    got = verdicts(b, res)
    assert {q for q in alive} == {q for q, v in got.items() if v == "weak-alive" and q in alive}
    assert {q for q in dead} == {q for q, v in got.items() if v == "dead" and q in dead}
    assert all(node_of(b, q).flags & md.F_DYNAMIC for q in alive)  # reached by a dynamic edge
    assert {e.tier for e in b.graph.edges if e.kind == "schema" and levels._qual(e.src) == "load"} == {"D"}
    names = {f[2] for f in findings(r, b, res)}
    assert not names & alive  # never a finding for what a dynamic site can name


def test_a_dead_function_does_not_keep_a_table_alive(grammars, tmp_path):
    r = make(tmp_path, {"app.py": APP.replace("dao.load_orders(None)", "1"), "dao.py": DAO, "db/t.sql": TABLES})
    b, res = analyze(r)
    assert set(verdicts(b, res).values()) == {"dead", "dead-cluster"}


JPA = (
    'package p;\n@Entity\n@Table(name = "orders")\nclass OrderRow {\n}\n\n@Entity\nclass Customer {\n}\n\n@Entity\nclass LineItem {\n}\n\n'
    "public class App {\n    public static void main(String[] a) {\n        new OrderRow();\n        new Customer();\n        new LineItem();\n    }\n}\n"
)


def test_jpa_entities_and_tables_are_references(grammars):
    from dead_code_checker.graph.extract import ts_generic as t

    f = t.TsGeneric().extract("java", "A.java", JPA)
    assert sorted((r.name, r.scope) for r in f.refs if r.kind == "schema") == [
        ("sql:Customer", "Customer"),  # an @Entity without @Table: the class name, as written and in snake_case
        ("sql:LineItem", "LineItem"),
        ("sql:customer", "Customer"),
        ("sql:line_item", "LineItem"),
        ("sql:orders", "OrderRow"),  # @Table(name): the table; the class name is not a table then
    ]


def test_jpa_reaches_the_tables_of_the_graph(grammars, tmp_path):
    r = make(
        tmp_path,
        {
            "src/main/java/p/App.java": JPA,
            "db/t.sql": "CREATE TABLE orders (id int);\nCREATE TABLE customer (id int);\nCREATE TABLE line_item (id int);\nCREATE TABLE legacy (id int);\nCREATE TABLE order_row (id int);\n",
        },
        exe=(),
    )
    b, res = analyze(r)
    assert verdicts(b, res) == {
        "table:orders": "weak-alive",
        "table:customer": "weak-alive",
        "table:line_item": "weak-alive",
        "table:legacy": "dead",
        "table:order_row": "dead",  # the entity of that name has a @Table
    }


# ================================================================ unused objects: suspect, W, never E
def test_an_unused_table_is_a_suspect_and_w_only_with_a_proof(grammars, tmp_path):
    r = make(tmp_path, {"app.py": APP, "dao.py": DAO, "db/tables.sql": TABLES})
    b, res = analyze(r)
    got = findings(r, b, res)
    assert [(lv, name) for lv, _, name in got if lv == "W"] == []
    assert {(code, name) for _, code, name in got} == {
        (verdict.SUSPECT, "table:legacy"),  # no engine reads SQL: the claim is not corroborated
        (verdict.SUSPECT, "table:public%2Eaudit_log"),
        (verdict.SUSPECT, "table:audit_user"),
        (verdict.SUSPECT, "view:open_orders"),
    }
    assert {lv for lv, _, _ in got} == {"I"}
    key = node_of(b, "table:legacy").key
    proved = findings(r, b, res, Proof(nodes={key: "test-engine unused-table at db/tables.sql:6"}))
    assert ("W", "dc.dead", "table:legacy") in proved and ("I", verdict.SUSPECT, "table:audit_user") in proved
    assert {lv for lv, _, _ in proved} <= {"W", "I"}  # at most W


def test_the_viewer_marks_the_unused_objects_as_suspects(grammars, tmp_path):
    r = make(tmp_path, {"app.py": APP, "dao.py": DAO, "db/tables.sql": TABLES})
    b, res = analyze(r)
    keys = levels.suspect_keys(b.graph, res, Proof())
    assert {levels._qual(k) for k in keys} == {
        "table:legacy",
        "table:public%2Eaudit_log",
        "table:audit_user",
        "view:open_orders",
    }


def test_schema_never_e(grammars, tmp_path):
    """No language of the schema group can be E: a branch that makes an object dead says W (with a proof that names it) or a suspect
    (without one), and the data table says which languages are schema languages."""
    assert all(verdict.schema_lang(lang) for lang in (*SCHEMA_LANGS, "postgres"))
    assert not verdict.schema_lang("python") and not verdict.schema_lang("java")
    assert "dc.orphaned-by-branch" in verdict.claims("sql") and "dc.orphaned-by-branch" not in verdict.claims("python")
    r = make(tmp_path, {"app.py": APP, "dao.py": DAO, "db/tables.sql": TABLES})
    write(r, "db/tables.sql", TABLES + "CREATE TABLE fresh (id int);\n")
    key = "sql:db/tables.sql#table:fresh"
    proof = Proof(nodes={key: "test-engine unused-table at db/tables.sql:8"})
    assert [(f.code, f.level) for f in levels.branch(str(r), "HEAD").findings] == [(verdict.SUSPECT, "I")] * 1
    assert [(f.code, f.level) for f in levels.branch(str(r), "HEAD", proof=proof).findings] == [
        ("dc.introduced-dead", "W")
    ]


def test_a_table_that_loses_its_last_caller_follows_the_confirmation_rule(grammars, tmp_path):
    r = make(
        tmp_path,
        {
            "app.py": APP,
            "dao.py": DAO,
            "db/tables.sql": "CREATE TABLE orders (id int);\nCREATE TABLE items (id int);\n",
        },
    )
    write(
        r, "dao.py", DAO.replace("orders o JOIN items i ON i.order_id = o.id", "items i")
    )  # orders lost its only caller on this branch
    got = [(f.code, f.level, f.msg) for f in levels.branch(str(r), "HEAD").findings]
    assert [(c, lv) for c, lv, _ in got] == [(verdict.SUSPECT, "I")] and got[0][2].startswith(
        "[dc.orphaned-by-branch] "
    )
    key = "sql:db/tables.sql#table:orders"
    proved = levels.branch(
        str(r), "HEAD", proof=Proof(nodes={key: "test-engine unused-table at db/tables.sql:1"})
    ).findings
    assert [(f.code, f.level) for f in proved] == [("dc.orphaned-by-branch", "W")]


def test_a_python_symbol_that_loses_its_last_caller_is_still_a_w_without_proof(tmp_path):
    """The extension for schema objects leaves every other language alone (the graph's own verdict, no engine needed)."""
    r = make(
        tmp_path,
        {
            "app.py": "#!/usr/bin/env python3\nimport lib\n\n\ndef main():\n    return lib.f()\n\n\nmain()\n",
            "lib.py": "def f():\n    return g() + 1\n\n\ndef g():\n    return 2\n",
        },
    )
    write(r, "lib.py", "def f():\n    return 3\n\n\ndef g():\n    return 2\n")
    assert [(f.code, f.level) for f in levels.branch(str(r), "HEAD").findings] == [("dc.orphaned-by-branch", "W")]


# ================================================================ migrations
MIGRATION = "ALTER TABLE legacy ADD COLUMN note text;\n"


@pytest.mark.parametrize(
    "path,state",
    [
        ("db/migration/V2__add_note.sql", "weak-alive"),  # Flyway runs it: the table it alters is used
        ("src/main/resources/db/changelog/0002.sql", "weak-alive"),
        ("db/migrate/20260101_add_note.sql", "weak-alive"),
        ("docker-entrypoint-initdb.d/02.sql", "weak-alive"),
        ("data.sql", "weak-alive"),  # Spring runs schema.sql and data.sql by name
        ("tools/add_note.sql", "dead"),  # nobody runs it: its ALTER is no use
    ],
)
def test_a_migration_references_what_it_alters(grammars, tmp_path, path, state):
    r = make(
        tmp_path, {"app.py": APP, "dao.py": DAO, "db/tables.sql": "CREATE TABLE legacy (id int);\n", path: MIGRATION}
    )
    b, res = analyze(r)
    assert verdicts(b, res)["table:legacy"] in (
        state,
        "dead-cluster" if state == "dead" else state,
    )  # the unrun ALTER and the table form a cluster
    roots = {x.node for x in b.graph.roots if x.kind == "init"}
    assert (f"sql:{path}" in roots) == (state == "weak-alive")


# ================================================================ proto
PROTO = """\
syntax = "proto3";
package acme.shop;
message Item { string sku = 1; }
message Order { string id = 1; repeated Item items = 2; }
message Unused { int32 x = 1; }
message Direct { int32 y = 1; }
message Bare { Bare2 b = 1; }
message Bare2 { int32 z = 1; }
service Shop { rpc Get (Order) returns (Order); }
service Idle { rpc Ping (Unused) returns (Unused); }
"""
PROTO_APP = (
    "package p;\npublic class App {\n    public static void main(String[] a) {\n        ShopGrpc.newBlockingStub(null);\n"
    "        Direct.newBuilder().build();\n    }\n}\n"
)


def test_proto_stubs_reach_the_service_and_the_messages_used_by_name(grammars, tmp_path):
    r = make(tmp_path, {"src/main/java/p/App.java": PROTO_APP, "proto/shop.proto": PROTO}, exe=())
    b, res = analyze(r)
    got = verdicts(b, res)
    pre = "acme%2Eshop%2E"
    assert got[f"service:{pre}Shop"] == "weak-alive"  # ShopGrpc: the generated class of the service Shop
    assert (
        got[f"rpc:{pre}Shop%2EGet"] == "weak-alive" and got[f"message:{pre}Order"] == "weak-alive"
    )  # its rpc and the types of the rpc
    assert got[f"message:{pre}Item"] == "weak-alive"  # a field of a live message
    assert got[f"message:{pre}Direct"] == "weak-alive"  # a message class used by its name
    assert {
        got[f"{k}:{pre}{n}"]
        for k, n in (
            ("service", "Idle"),
            ("rpc", "Idle%2EPing"),
            ("message", "Unused"),
            ("message", "Bare"),
            ("message", "Bare2"),
        )
    } <= {"dead", "dead-cluster"}
    edges = {(levels._qual(e.src), levels._qual(e.dst), e.tier) for e in b.graph.edges if e.kind == "schema"}
    assert ("App.main", f"service:{pre}Shop", "N") in edges and ("App.main", f"message:{pre}Direct", "N") in edges
    assert (f"service:{pre}Shop", f"rpc:{pre}Shop%2EGet", "T") in edges


def test_a_python_module_of_the_generated_code_reaches_the_file_and_its_services(grammars, tmp_path):
    py = "#!/usr/bin/env python3\nimport shop_pb2\nimport shop_pb2_grpc\n\n\ndef main():\n    shop_pb2.Direct()\n    return shop_pb2_grpc.ShopStub(None)\n\n\nmain()\n"
    r = make(tmp_path, {"app.py": py, "proto/shop.proto": PROTO})
    b, res = analyze(r)
    got = verdicts(b, res)
    pre = "acme%2Eshop%2E"
    assert got[f"message:{pre}Direct"] == "weak-alive" and got[f"service:{pre}Shop"] == "weak-alive"
    assert got[f"service:{pre}Idle"] == "weak-alive"  # shop_pb2_grpc is the module of every service of shop.proto
    assert got[f"message:{pre}Unused"] == "weak-alive"  # reached through the service Idle that its module names
    file_node = next(n for n in b.graph.nodes if n.kind == "file" and n.path == "proto/shop.proto")
    assert res.verdict(file_node.key) == "weak-alive"  # shop_pb2: the module of the file


def test_generated_and_vendored_protos_are_skipped(grammars, tmp_path):
    files = {
        "app.py": APP,
        "dao.py": DAO,
        "proto/real.proto": 'syntax = "proto3";\nmessage Real { int32 a = 1; }\n',
        "gen/api.proto": '// Generated by the protocol buffer compiler.  DO NOT EDIT!\nsyntax = "proto3";\nmessage Gen { int32 a = 1; }\n',
        "third_party/google/protobuf/any.proto": 'syntax = "proto3";\npackage google.protobuf;\nmessage Any { string u = 1; }\n',
        "vendored/other/x.proto": 'syntax = "proto3";\nmessage Vend { int32 a = 1; }\n',
        "api/google/protobuf/timestamp.proto": 'syntax = "proto3";\nmessage Timestamp { int64 s = 1; }\n',
    }
    r = make(tmp_path, files)
    b, res = analyze(r)
    got = verdicts(b, res)
    # a copy that a path gives away (third_party, vendored, google/protobuf) is not in the graph at all; a header in the file flags its objects
    assert set(got) == {"message:Real", "message:Gen"}
    assert {q for q in got if res.excluded(node_of(b, q).key)} == {"message:Gen"}
    assert not {n.path for n in b.graph.nodes} & {
        "third_party/google/protobuf/any.proto",
        "vendored/other/x.proto",
        "api/google/protobuf/timestamp.proto",
    }
    assert not res.excluded(node_of(b, "message:Real").key)
    assert [name for _, _, name in findings(r, b, res)] == ["message:Real"]  # the others are nobody's to remove
    assert (
        link.path_flags("a/third_party/x.proto") & md.F_GENERATED
        and not link.path_flags("proto/real.proto") & md.F_GENERATED
    )


# ================================================================ graphql
GQL = """\
type Query {
  books: [Book!]!
  node(id: ID!): Node
  stale: Int
  fetched: Int
  unserved: Int
}
type Mutation { addBook(input: BookInput!): Book }
type Book implements Node { id: ID! author: Author }
interface Node { id: ID! }
type Author implements Node { id: ID! name: String }
input BookInput { title: String! }
type Unused { x: Int }
"""
GQL_JAVA = """\
package p;
public class App {
    public static void main(String[] a) {
        new BookController().books();
        new BookController().node(null);
        new BookController().author(null);
        new QueryResolver().stale();
        builder.dataFetcher("fetched", env -> null);
    }
}
@Controller
class BookController {
    @QueryMapping
    public Object books() { return null; }
    @QueryMapping("node")
    public Object node(String id) { return null; }
    @SchemaMapping(typeName = "Book", field = "author")
    public Object author(Object b) { return null; }
}
class QueryResolver {
    Object stale() { return null; }
}
"""


def test_graphql_resolvers_reach_the_fields_they_serve(grammars, tmp_path):
    r = make(tmp_path, {"src/main/java/p/App.java": GQL_JAVA, "api/schema.graphqls": GQL}, exe=())
    b, res = analyze(r)
    got = verdicts(b, res)
    alive = {q for q, v in got.items() if v == "weak-alive"}
    assert alive == {
        "type:Query",  # a root operation type: the clients call it
        "type:Mutation",  # so is this one, and no resolver of its fields is needed for it
        "field:Query%2Ebooks",  # @QueryMapping
        "field:Query%2Enode",  # @QueryMapping("node"): the string names the field
        "field:Query%2Estale",  # a member of QueryResolver named like the field
        "field:Query%2Efetched",  # builder.dataFetcher("fetched", ..)
        "type:Book",  # the type of a live field
        "type:Author",  # @SchemaMapping(typeName = "Book", field = "author"): the field of Book that has no node of its own reaches the type
        "interface:Node",  # the return type of a live field
    }, got
    assert {q for q, v in got.items() if v != "weak-alive"} == {
        "field:Query%2Eunserved",
        "field:Mutation%2EaddBook",
        "input:BookInput",
        "type:Unused",
    }
    edges = {(levels._qual(e.src), levels._qual(e.dst), e.tier) for e in b.graph.edges if e.kind == "schema"}
    assert ("BookController.books", "field:Query%2Ebooks", "N") in edges and (
        "QueryResolver.stale",
        "field:Query%2Estale",
        "N",
    ) in edges
    assert ("App.main", "field:Query%2Efetched", "N") in edges and ("BookController.author", "type:Book", "N") in edges


def test_an_interface_reaches_its_implementers(grammars, tmp_path):
    """`Author implements Node` and a live field that returns Node: nothing else names Author, and it is used."""
    java = "package p;\npublic class App {\n    public static void main(String[] a) {\n        new C().node(null);\n    }\n}\nclass C {\n    @QueryMapping\n    Object node(String id) { return null; }\n}\n"
    schema_ = "type Query { node(id: ID!): Node }\ninterface Node { id: ID! }\ntype Author implements Node { id: ID! }\ntype Other { id: ID! }\n"
    r = make(tmp_path, {"src/main/java/p/App.java": java, "s.graphqls": schema_}, exe=())
    b, res = analyze(r)
    got = verdicts(b, res)
    assert (got["type:Author"], got["type:Other"]) == ("weak-alive", "dead")
    assert ("interface:Node", "type:Author", "T") in {
        (levels._qual(e.src), levels._qual(e.dst), e.tier) for e in b.graph.edges if e.kind == "schema"
    }


def test_the_annotation_rules_write_refs_and_no_root(grammars):
    from dead_code_checker.graph.extract import ts_generic as t

    f = t.TsGeneric().extract(
        "java",
        "A.java",
        'class A {\n  @MutationMapping\n  Object add() { return null; }\n  @DgsData(parentType = "Book", field = "title")\n  Object t() { return null; }\n'
        '  @SubscriptionMapping(name = "ticks")\n  Object s() { return null; }\n  @BatchMapping\n  Object author() { return null; }\n}\n',
    )
    assert sorted((r.name, r.scope) for r in f.refs if r.kind == "schema") == [
        ("graphql:Book.title", "A.t"),
        ("graphql:Mutation.add", "A.add"),
        ("graphql:Subscription.ticks", "A.s"),
        ("graphql:author", "A.author"),  # no type: any field of that name
    ]
    assert not [x for x in f.roots if "schema" in x.kind]  # a ref-only rule is no entry point


def test_a_missing_grammar_is_reported_never_skipped(tmp_path, monkeypatch):
    """A schema file whose grammar cannot be used is a warning (dc.degraded-extractor) or a missing extractor, never an empty graph."""
    monkeypatch.setattr(tg, "set_backend", tg.set_backend)

    def broken(grammar, data):
        raise LookupError(f"grammar {grammar} is not locked")

    tg.set_backend(broken)
    try:
        with pytest.raises(contracts.ExtractorMissing, match="not locked"):
            contracts.get_extractor("sql").extract("sql", "a.sql", "CREATE TABLE a (id int);")
    finally:
        tg.set_backend(None)
    assert "schema" in tg.GRAMMAR_EXTRACTORS


def test_kotlin_annotations_write_the_same_refs(grammars):
    """Kotlin has the same annotation rules (JPA, Spring GraphQL); its string literals are not read for SQL yet (no xlang query for Kotlin)."""
    if unlocked.available("kotlin"):
        pytest.skip(unlocked.available("kotlin"))
    f = tg.TsGeneric().extract(
        "kotlin",
        "A.kt",
        'package p\n@Entity\n@Table(name = "orders")\nclass OrderRow\n\n@Entity\nclass LineItem\n\nclass Ctl {\n    @QueryMapping\n    fun books(): Any = 1\n\n'
        '    @SchemaMapping(typeName = "Book", field = "author")\n    fun author(b: Any): Any = 1\n}\n',
    )
    assert sorted((r.name, r.scope) for r in f.refs if r.kind == "schema") == [
        ("graphql:Book.author", "Ctl.author"),
        ("graphql:Query.books", "Ctl.books"),
        ("sql:LineItem", "LineItem"),
        ("sql:line_item", "LineItem"),
        ("sql:orders", "OrderRow"),
    ]


# ================================================================ the size of a schema dump
def _brute_inner(spans, pos):
    """What the nest must answer: the last of the spans sorted by (start, -end) that contains `pos`."""
    return next((q for s, e, q in reversed(sorted(spans, key=lambda sp: (sp[0], -sp[1]))) if s <= pos < e), "")


def _nested_spans(rnd, lo, hi, out, depth=0):
    """Random properly nested and disjoint spans inside [lo, hi), several with the same start or the same end."""
    pos = lo
    while pos < hi - 4 and rnd.random() < 0.85:
        start = pos + rnd.randrange(0, 3)
        end = rnd.randrange(start + 2, min(hi, start + 40) + 1)
        out.append((start, end, f"q{len(out)}"))
        if depth < 4:
            _nested_spans(rnd, start, end, out, depth + 1)
        pos = end + rnd.randrange(0, 2)
    return out


@pytest.mark.parametrize("seed", range(12))
def test_the_nest_finds_the_innermost_span_like_a_scan_does(seed):
    rnd = random.Random(seed)
    spans = _nested_spans(rnd, 0, 400, [])
    rnd.shuffle(spans)  # the nest sorts them itself
    spans += [spans[0][:2] + ("twin",)] if spans else []  # two equal spans: the later one is the innermost
    nest = schema._Nest(spans)
    assert [nest.at(pos) for pos in range(-2, 405)] == [_brute_inner(spans, pos) for pos in range(-2, 405)]
    assert schema._Nest([]).at(5) == ""


def test_the_nest_of_200000_spans_costs_a_sweep_not_a_scan_per_query():
    spans = [(10 * i, 10 * i + 9, f"o{i}") for i in range(100000)] + [
        (10 * i + 2, 10 * i + 5, f"i{i}") for i in range(100000)
    ]
    start = time.process_time()  # the CPU time of the extraction: a loaded host does not make the test flaky
    nest = schema._Nest(spans)
    got = [nest.at(10 * i + 3) for i in range(0, 100000, 7)] + [nest.at(10 * i + 7) for i in range(0, 100000, 7)]
    assert time.process_time() - start < 15  # a scan per query is 2 * 10^10 steps here
    assert got[:2] == ["i0", "i7"] and got[-1] == "o99995" and nest.at(10 * 5 + 9) == ""


def _sql_dump(n):
    return "".join(
        f"CREATE TABLE t{i} (id int REFERENCES t{i - 1}(id));\nINSERT INTO t{i} VALUES (1);\n" for i in range(1, n)
    )


def _proto_dump(n):
    return 'syntax = "proto3";\n' + "".join(f"message M{i} {{ M{i - 1} a = 1; }}\n" for i in range(1, n))


def _graphql_dump(n):
    return (
        "".join(f"type T{i} implements Node {{ a: T{i - 1} }}\n" for i in range(1, n))
        + "interface Node { id: ID }\ntype Query { "
        + " ".join(f"f{i}: T{i}" for i in range(n // 2))
        + " }\n"
    )


@pytest.mark.parametrize(
    "lang, dump, count, defs, budget",
    [
        # a schema dump of about 1 MB (the reader takes files up to MAX_FILE_BYTES) must stay far below the time of a run (a scan per object or per reference took 10 s for 6000 tables and about 30 s for 12000
        ("sql", _sql_dump, 12000, 11999, 12),
        ("proto", _proto_dump, 24000, 23999, 12),
        ("graphql", _graphql_dump, 18000, 18000 + 9000 + 1, 12),
    ],
)
def test_a_big_schema_dump_is_extracted_in_time(grammars, lang, dump, count, defs, budget):
    text = dump(count)
    start = time.process_time()
    f = facts(lang, text)
    took = time.process_time() - start
    assert f.errors == 0 and len([d for d in f.defs if d.qual]) == defs
    assert took < budget, f"{lang}: {len(text)} bytes took {took:.1f} s"
    assert ("graphql:~Node", "type:T5") in refs(f) if lang == "graphql" else True


# ================================================================ a statement written over several literals
@pytest.mark.parametrize(
    "parts,names",
    [
        (["SELECT id, name ", "FROM orders o ", "JOIN items i ON i.order_id = o.id"], ["sql:items", "sql:orders"]),
        (["SELECT *", " FROM legacy"], ["sql:legacy"]),
        (["SELECT ", None, " FROM mid_", "tbl"], ["sql:mid_tbl"]),  # a name written over two literals is one name
        (["SELECT * FROM ", None], ["sql:*"]),  # a computed name: any table
        (["SELECT * FROM ", None, ".orders"], ["sql:*"]),
        (["SELECT * FROM audit_", None], ["sql:audit_*"]),  # a computed tail after an underscore: the tables that fit
        (["SELECT * FROM orders", None], ["sql:orders"]),  # the where clause that is appended, as for a single literal
        ([None, "SELECT x FROM p"], ["sql:p"]),  # holes before the first literal are no part of the statement
        (["Hello ", None, " from here"], []),  # prose is no statement
        (["SELECT 1"], []),
        ([None, None], []),
    ],
)
def test_sql_fold_reads_the_operands_as_one_statement(parts, names):
    assert xlang.sql_fold(parts) == names


JAVA_CHAIN = """\
package p;
class Dao {
    String a() {
        return "SELECT id, name " + // the columns
            "FROM orders o " +
            "JOIN items i ON i.order_id = o.id";
    }
    String b() { return "SELECT *" + " FROM legacy"; }
    String c() { return ("SELECT x " + "FROM par") + " WHERE y"; }
    String d(String t) { return "SELECT * FROM " + t; }
    String e(String k) { return "SELECT * FROM audit_" + k + " WHERE x = 1"; }
    String f(String cols) { return "SELECT " + cols + " FROM mid_" + "tbl"; }
    String g(String w) { return "SELECT * FROM plain" + w; }
    String h() {
        StringBuilder sb = new StringBuilder("SELECT a ");
        sb.append("FROM builder ").append(" LEFT JOIN joined j ON 1 = 1");
        return sb.toString();
    }
    String i() { return "SELECT 1"; }
    int j() { return 1 + 2; }
}
"""


def test_a_java_statement_over_several_literals_names_its_tables(grammars):
    got = refs(facts("java", JAVA_CHAIN))
    assert got == [
        ("sql:*", "Dao.d"),  # a computed name
        ("sql:audit_*", "Dao.e"),
        ("sql:builder", "Dao.h"),  # the parts that a StringBuilder appends
        ("sql:items", "Dao.a"),  # a statement over three lines, a comment between the operands
        ("sql:joined", "Dao.h"),
        ("sql:legacy", "Dao.b"),
        ("sql:mid_tbl", "Dao.f"),
        ("sql:orders", "Dao.a"),
        ("sql:par", "Dao.c"),  # inside parentheses
        ("sql:plain", "Dao.g"),
    ]


def test_the_refs_of_a_chain_do_not_depend_on_the_hash_seed(grammars, tmp_path):
    """The chains of a file are visited in a sorted order: the facts are the same bytes under any PYTHONHASHSEED."""
    src = tmp_path / "Dao.java"
    src.write_text(JAVA_CHAIN)
    code = (
        "import sys, hashlib\n"
        f"sys.path[:0] = [{str(ROOT)!r}, {str(ROOT / 'tests')!r}]\n"
        "import real_grammar as u\nfrom dead_code_checker.graph import contracts\nfrom dead_code_checker.graph.extract import ts_generic as tg\n"
        "tg.set_backend(u.backend)\n"
        f"f = contracts.get_extractor('java').extract('java', 'Dao.java', open({str(src)!r}).read())\n"
        "print(hashlib.sha256(repr(f.to_payload()).encode()).hexdigest())\n"
    )
    out = set()
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        res = subprocess.run([sys.executable, "-P", "-c", code], capture_output=True, text=True, env=env, timeout=120)
        assert res.returncode == 0, res.stderr
        out.add(res.stdout.strip())
    assert len(out) == 1


PY_CHAIN = """\
def a(db):
    return db.run("SELECT a " + "FROM py_two " + "JOIN py_join j ON 1 = 1")


def b(db, cols):
    return db.run(f"SELECT {cols} FROM py_f WHERE x = 1")


def c(db):
    return db.run("SELECT a " + "FROM %s" % "py_pct")


def d(db, t):
    return db.run("SELECT a " + "FROM {}".format(t))


def e(db, t):
    return db.run("SELECT * FROM " + t)


def f(db, k):
    return db.run(f"SELECT * FROM audit_{k}")


def g(db, pre):
    return db.run(pre + "SELECT a FROM py_pre")


def h(db):
    return db.run(("SELECT a " "FROM py_implicit"))


def i(db):
    return db.run("SELECT 1") + 1 + 2
"""


def test_a_python_statement_over_several_operands_names_its_tables():
    assert refs(facts("python", PY_CHAIN)) == [
        ("sql:*", "c"),  # `"FROM %s" % ..` and `"FROM {}".format(..)`: a hole at the name, any table
        ("sql:*", "d"),
        ("sql:*", "e"),
        ("sql:audit_*", "f"),
        ("sql:py_f", "b"),  # an f-string with a hole before FROM
        ("sql:py_implicit", "h"),  # one constant for the parser already
        ("sql:py_join", "a"),
        ("sql:py_pre", "g"),  # a prefix before the statement
        ("sql:py_two", "a"),
    ]


@pytest.mark.parametrize(
    "lang,text,names",
    [
        (
            "javascript",
            'q = "SELECT a " + "FROM js_a" + x;\nr = `SELECT ${c} FROM js_t`;\ns = "SELECT *" + " FROM " + tab;',
            ["sql:*", "sql:js_a", "sql:js_t"],
        ),
        ("go", 'package p\nvar q = "SELECT a " +\n\t"FROM go_a"\n', ["sql:go_a"]),
        ("csharp", 'class A { string F() { return "SELECT a " + "FROM cs_a"; } }', ["sql:cs_a"]),
        ("cpp", 'const char* q = "SELECT a "\n  "FROM cpp_a";\n', ["sql:cpp_a"]),
        (
            "kotlin",
            'val q = "SELECT a " + "FROM kt_a" + x\nval r = """\n  SELECT * FROM kt_raw\n"""\nval s = "SELECT * FROM $t WHERE ${c}"\n',
            ["sql:*", "sql:kt_a", "sql:kt_raw"],
        ),
    ],
)
def test_other_languages_read_a_statement_over_several_literals(lang, text, names):
    why = unlocked.available(lang)
    if why:
        pytest.skip(why)
    tg.set_backend(unlocked.backend)
    try:
        assert sorted({n for n, _ in refs(facts(lang, text))}) == names
    finally:
        tg.set_backend(None)


def test_a_chain_of_thousands_of_literals_is_one_pass(grammars):
    n = 4000
    ops = ['"SELECT a "'] + ['"x "'] * n + ['"FROM long_chain"']
    java = "package p;\nclass D {\n    String q() {\n        return " + " +\n            ".join(ops) + ";\n    }\n}\n"
    start = time.process_time()  # CPU time: a loaded host does not make the test flaky
    got = refs(facts("java", java))
    assert time.process_time() - start < 15
    assert got == [("sql:long_chain", "D.q")]
    tree = ast.parse("x = " + " + ".join(['"SELECT a "'] + ['"x "'] * 1500 + ['"FROM py_long"']) + "\n")
    scan = xlang.PyScan(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            scan.const(node, "")
    assert sorted({r.name for r in scan.finish()}) == ["sql:py_long"]


JAVA_DAO = """\
package p;
public class Dao {
    String orders() {
        return "SELECT id, name " +
            "FROM orders o " +
            "JOIN items i ON i.order_id = o.id";
    }
    String legacy() { return "SELECT *" + " FROM legacy"; }
    String audit(String kind) { return "SELECT * FROM audit_" + kind; }
    String dynamic(String cols, String t) { return "SELECT " + cols + " FROM " + t; }
}
"""
JAVA_MAIN = "package p;\npublic class App {\n    public static void main(String[] a) {\n        Dao d = new Dao();\n        d.orders();\n        d.legacy();\n    }\n}\n"
PY_MAIN = (
    "#!/usr/bin/env python3\nimport dao\n\n\ndef main():\n    return dao.one(None) + dao.two(None, 'x')\n\n\nmain()\n"
)
PY_DAO = (
    "def one(db):\n"
    '    return db.run("SELECT a " + "FROM py_two " + "WHERE x = 1")\n\n\n'
    "def two(db, cols):\n"
    '    return db.run(f"SELECT {cols} FROM py_f")\n'
)
CHAIN_TABLES = (
    "CREATE TABLE orders (id int);\nCREATE TABLE items (id int, order_id int);\nCREATE TABLE legacy (id int);\n"
    "CREATE TABLE py_two (id int);\nCREATE TABLE py_f (id int);\nCREATE TABLE unused_one (id int);\nCREATE TABLE unused_two (id int);\n"
)


def test_a_table_named_over_concatenated_literals_is_alive_and_an_unused_one_is_a_suspect(grammars, tmp_path):
    """The usual style of Java (a statement over several lines) and Python (`+` and an f-string): the table that the statement names is used, the
    one nothing names stays a suspect (W with a proof, never E)."""
    dao = "".join(ln + "\n" for ln in JAVA_DAO.splitlines() if "audit" not in ln and "dynamic" not in ln)
    files = {
        "src/main/java/p/App.java": JAVA_MAIN,
        "src/main/java/p/Dao.java": dao,
        "app.py": PY_MAIN,
        "dao.py": PY_DAO,
        "db/t.sql": CHAIN_TABLES,
    }
    r = make(tmp_path, files)
    b, res = analyze(r)
    got = verdicts(b, res)
    assert {q for q, v in got.items() if v == "weak-alive"} == {
        "table:orders",
        "table:items",
        "table:legacy",
        "table:py_two",
        "table:py_f",
    }
    assert {q for q, v in got.items() if v == "dead"} == {"table:unused_one", "table:unused_two"}
    fs = findings(r, b, res)
    assert {(code, name) for _, code, name in fs} == {
        (verdict.SUSPECT, "table:unused_one"),
        (verdict.SUSPECT, "table:unused_two"),
    }
    assert {lv for lv, _, _ in fs} == {"I"}  # a suspect, never E
    key = node_of(b, "table:unused_one").key
    proved = findings(r, b, res, Proof(nodes={key: "test-engine unused-table at db/t.sql:6"}))
    assert ("W", "dc.dead", "table:unused_one") in proved and {lv for lv, _, _ in proved} <= {"W", "I"}


def test_a_name_built_at_run_time_in_a_chain_makes_no_table_dead(grammars, tmp_path):
    java = JAVA_MAIN.replace("        d.legacy();\n", '        d.dynamic("a", "b");\n')
    r = make(
        tmp_path,
        {"src/main/java/p/App.java": java, "src/main/java/p/Dao.java": JAVA_DAO, "db/t.sql": CHAIN_TABLES},
        exe=(),
    )
    b, res = analyze(r)
    got = verdicts(b, res)
    assert set(got.values()) == {"weak-alive"}  # `FROM " + t`: any table
    assert all(node_of(b, q).flags & md.F_DYNAMIC for q in ("table:unused_one", "table:legacy"))
    assert not {
        name for _, _, name in findings(r, b, res) if name.startswith("table:")
    }  # (the Java methods nobody calls are dead)


def test_a_name_with_a_computed_tail_in_a_chain_reaches_the_tables_that_fit(grammars, tmp_path):
    java = JAVA_MAIN.replace("        d.legacy();\n", '        d.audit("x");\n')
    tables = "CREATE TABLE audit_user (id int);\nCREATE TABLE audit_log (id int);\nCREATE TABLE legacy (id int);\n"
    r = make(
        tmp_path,
        {"src/main/java/p/App.java": java, "src/main/java/p/Dao.java": JAVA_DAO, "db/t.sql": tables},
        exe=(),
    )
    b, res = analyze(r)
    got = verdicts(b, res)
    assert (got["table:audit_user"], got["table:audit_log"]) == ("weak-alive", "weak-alive")
    assert got["table:legacy"] == "dead-cluster"  # only the method that nobody calls names it


def test_a_kotlin_statement_names_its_tables(tmp_path):
    for g in (*SCHEMA_LANGS, "kotlin"):
        why = unlocked.available(g)
        if why:
            pytest.skip(why)
    tg.set_backend(unlocked.backend)
    try:
        kt = (
            'package p\n\nclass Repo {\n    fun orders(): String = "SELECT a " +\n        "FROM orders o " +\n        "JOIN items i ON 1 = 1"\n}\n\n'
            "fun main() {\n    Repo().orders()\n}\n"
        )
        tables = "CREATE TABLE orders (id int);\nCREATE TABLE items (id int);\nCREATE TABLE legacy (id int);\n"
        r = make(tmp_path, {"src/main/kotlin/p/Repo.kt": kt, "db/t.sql": tables}, exe=())
        b, res = analyze(r)
        assert verdicts(b, res) == {"table:orders": "weak-alive", "table:items": "weak-alive", "table:legacy": "dead"}
    finally:
        tg.set_backend(None)


# ================================================================ generated copies are schema files only
def test_the_generated_paths_apply_to_schema_files_and_never_to_other_languages(grammars, tmp_path):
    """First-party code under external/, deps/ or src/google/cloud/ is code of the project: it stays in the graph, so what it calls is used."""
    lib = "def used():\n    return 1\n\n\ndef only_ext():\n    return 2\n\n\ndef only_deps():\n    return 3\n\n\ndef only_cloud():\n    return 4\n\n\ndef unused():\n    return 5\n"
    call = "#!/usr/bin/env python3\nimport lib\n\nlib.{}()\n"
    files = {
        "app.py": "#!/usr/bin/env python3\nimport lib\n\n\ndef main():\n    return lib.used()\n\n\nmain()\n",
        "lib.py": lib,
        "external/tool.py": call.format("only_ext"),
        "deps/tool.py": call.format("only_deps"),
        "src/google/cloud/x.py": call.format("only_cloud"),
        "deps/real.proto": 'syntax = "proto3";\nmessage KeptHere { int32 a = 1; }\n',
    }
    r = make(tmp_path, files, exe=("app.py", "external/tool.py", "deps/tool.py", "src/google/cloud/x.py"))
    b, res = analyze(r)
    assert {n.path for n in b.graph.nodes} >= {
        "external/tool.py",
        "deps/tool.py",
        "src/google/cloud/x.py",
        "deps/real.proto",
    }
    got = {levels._qual(n.key): res.verdict(n.key) for n in b.graph.nodes if n.path == "lib.py" and n.kind != "file"}
    assert {q for q, v in got.items() if v != "dead"} == {"used", "only_ext", "only_deps", "only_cloud"}, got
    assert [name for _, _, name in findings(r, b, res) if name == "unused"] == ["unused"]
    for path in ("external/tool.py", "deps/tool.py", "src/google/cloud/x.py", "a/google/cloud/y.go", "x/vendored/y.py"):
        assert not link.path_flags(path) & md.F_GENERATED, path
    for path in (
        "a/vendored/x.proto",
        "third-party/api.proto",
        "src/google/cloud/x.proto",
        "x/google/protobuf/any.proto",
        "db/vendored/x.sql",
        "g/googleapis/a.graphql",
    ):
        assert link.path_flags(path) & md.F_GENERATED, path
    assert (
        not link.path_flags("deps/real.proto") & md.F_GENERATED
        and not link.path_flags("proto/real.proto") & md.F_GENERATED
    )


# ================================================================ graphql: python resolvers and custom root names
GQL_PY_SCHEMA = (
    "type Query { books: [Book] bookById(id: ID): Book allAuthors: [Author] unserved: Int }\n"
    "type Book { id: ID }\ntype Author { name: String }\ntype Unused { x: Int }\n"
)
GQL_PY_API = (
    "class Query:\n"
    "    def resolve_books(self, info):\n        return []\n\n"
    "    def resolve_book_by_id(self, info, id):\n        return None\n\n"
    "    def get_all_authors(self, info):\n        return []\n"
)
GQL_PY_APP = (
    "#!/usr/bin/env python3\nimport api\n\n\ndef main():\n    q = api.Query()\n"
    "    return q.resolve_books(None) + q.resolve_book_by_id(None, 1) + q.get_all_authors(None)\n\n\nmain()\n"
)


def test_a_python_resolver_named_like_graphene_reaches_the_field(grammars, tmp_path):
    """`resolve_books`, `resolve_book_by_id` and `get_all_authors` serve the fields books, bookById and allAuthors (no underscores, any case)."""
    r = make(tmp_path, {"app.py": GQL_PY_APP, "api.py": GQL_PY_API, "api/schema.graphql": GQL_PY_SCHEMA})
    b, res = analyze(r)
    got = verdicts(b, res)
    assert {q for q, v in got.items() if v == "weak-alive"} == {
        "type:Query",
        "field:Query%2Ebooks",
        "field:Query%2EbookById",
        "field:Query%2EallAuthors",
        "type:Book",
        "type:Author",
    }, got
    assert {q for q, v in got.items() if v != "weak-alive"} == {"field:Query%2Eunserved", "type:Unused"}
    edges = {(levels._qual(e.src), levels._qual(e.dst), e.tier) for e in b.graph.edges if e.kind == "schema"}
    assert ("Query.resolve_books", "field:Query%2Ebooks", "N") in edges
    assert ("Query.resolve_book_by_id", "field:Query%2EbookById", "N") in edges
    assert ("Query.get_all_authors", "field:Query%2EallAuthors", "N") in edges


CUSTOM_ROOT = """\
schema { query: RootQ mutation: RootM }
type RootQ { book: Book  unserved: Int  byIsbn(isbn: String): Book }
type RootM { addBook: Book  removeBook: Int }
type Book { id: ID }
type Unused { x: Int }
"""
CUSTOM_ROOT_JAVA = """\
package p;
public class App {
    public static void main(String[] a) {
        new BookController().book();
        new BookController().add();
        new RootQueryResolver().by_isbn();
    }
}
@Controller
class BookController {
    @QueryMapping
    public Object book() { return null; }
    @MutationMapping("addBook")
    public Object add() { return null; }
}
class RootQueryResolver {
    Object by_isbn() { return null; }
}
"""


def test_the_resolvers_of_the_default_root_names_reach_the_fields_of_a_custom_root(grammars, tmp_path):
    """`schema { query: RootQ }`: @QueryMapping writes `Query.book`, the fields are those of RootQ."""
    f = facts("graphql", CUSTOM_ROOT)
    assert {(r.name, r.scope) for r in f.refs if r.name.startswith("graphql:=")} == {
        ("graphql:=query", "type:RootQ"),
        ("graphql:=mutation", "type:RootM"),
    }
    r = make(tmp_path, {"src/main/java/p/App.java": CUSTOM_ROOT_JAVA, "api/schema.graphqls": CUSTOM_ROOT}, exe=())
    b, res = analyze(r)
    got = verdicts(b, res)
    assert {q for q, v in got.items() if v == "weak-alive"} == {
        "type:RootQ",  # a root: the clients call it
        "type:RootM",
        "field:RootQ%2Ebook",  # @QueryMapping
        "field:RootM%2EaddBook",  # @MutationMapping("addBook")
        "field:RootQ%2EbyIsbn",  # the method of a class named like the operation, by_isbn = byIsbn
        "type:Book",
    }, got
    assert {q for q, v in got.items() if v != "weak-alive"} == {
        "field:RootQ%2Eunserved",
        "field:RootM%2EremoveBook",
        "type:Unused",
    }


def test_a_ref_written_with_the_type_name_of_a_custom_root_still_resolves(grammars, tmp_path):
    java = (
        "package p;\npublic class App {\n    public static void main(String[] a) {\n        new C().book();\n    }\n}\n"
        'class C {\n    @SchemaMapping(typeName = "RootQ", field = "book")\n    Object book() { return null; }\n}\n'
    )
    r = make(tmp_path, {"src/main/java/p/App.java": java, "s.graphqls": CUSTOM_ROOT}, exe=())
    b, res = analyze(r)
    got = verdicts(b, res)
    assert got["field:RootQ%2Ebook"] == "weak-alive" and got["field:RootQ%2Eunserved"] != "weak-alive"
