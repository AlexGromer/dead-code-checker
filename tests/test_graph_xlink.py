"""graph.xlink: the link side of the cross-language edges (lit, ffi, env). The facts are built by hand: the extractors that write
them arrive with the language packs.

python3 -m pytest -q tests/test_graph_xlink.py
"""

from dead_code_checker.graph import link as dlink, model as md, xlink
from dead_code_checker.graph.extract import xlang
from dead_code_checker.graph.model import Def, Facts, Ref


def d(qual):
    return Def(qual, "method" if "." in qual else "class" if qual[:1].isupper() else "func", 1, 1)


def facts(lang, defs=(), refs=()):
    return Facts(lang, tuple(defs), tuple(refs))


def cross(files):
    res = dlink.link(files, workers=1)
    return sorted(
        (e.src.split(":", 1)[1], e.dst.split(":", 1)[1], e.kind, e.tier)
        for e in res.graph.edges
        if e.kind in xlink.KINDS
    )


def test_the_kinds_are_edge_kinds_and_the_data_is_the_xlang_table():
    assert set(xlink.KINDS) <= set(md.EDGE_KINDS)
    ffi_groups, ambient, max_writers, schema = xlink.rules()
    assert (
        ffi_groups == xlang.data()["ffi_groups"]
        and "HOME" in ambient
        and max_writers == xlang.data()["env_max_writers"]
        and schema == xlang.data()["schema"]
    )


def test_a_string_that_spells_a_file_is_a_weak_edge_to_exactly_that_file_and_never_to_itself():
    files = {
        "py/app.py": facts(
            "python",
            [d("main")],
            [
                Ref("lib/add.c", "lit", 3, "main"),
                Ref("py/app.py", "lit", 4, "main"),
                Ref("nothere.c", "lit", 5, "main"),
            ],
        ),
        "lib/add.c": facts("c", [d("add")]),
    }
    assert cross(files) == [("py/app.py#main", "lib/add.c", "lit", "N")]
    near = {"a/x.py": facts("python", [d("f")], [Ref("data.c", "lit", 1, "f")]), "a/data.c": facts("c", [d("g")])}
    assert cross(near) == [("a/x.py#f", "a/data.c", "lit", "N")]  # relative to the file first, then to the tree root


def test_a_native_symbol_is_a_strong_edge_for_one_candidate_and_a_weak_one_for_several():
    one = {"py.py": facts("python", [d("m")], [Ref("only", "ffi", 1, "m")]), "n/b.c": facts("c", [d("only")])}
    assert cross(one) == [("py.py#m", "n/b.c#only", "ffi", "T")]
    two = {**one, "n/b.rs": facts("rust", [d("only")])}
    assert cross(two) == [("py.py#m", "n/b.c#only", "ffi", "N"), ("py.py#m", "n/b.rs#only", "ffi", "N")]
    own = {"a.c": facts("c", [d("f")], [Ref("g", "ffi", 1, "f")]), "b.c": facts("c", [d("g")])}
    assert cross(own) == []  # the exporting side of the same group is that language's own call graph


def test_a_jni_name_reaches_the_overloads_with_a_signature():
    files = {
        "a/A.java": facts("java", [d("A"), d("A.go")], [Ref("Java_p_A_go", "ffi", 3, "A.go")]),
        "n/a.c": facts("c", [d("Java_p_A_go__I")]),
        "n/other.c": facts("c", [d("Java_p_B_go__I")]),
    }
    assert cross(files) == [("a/A.java#A.go", "n/a.c#Java_p_A_go__I", "ffi", "T")]


def test_a_variable_that_one_language_reads_reaches_the_code_that_writes_it_in_another():
    files = {
        "py/app.py": facts(
            "python",
            [d("main")],
            [Ref("r:API_KEY", "env", 5, "main"), Ref("r:HOME", "env", 6, "main"), Ref("w:OTHER", "env", 7, "main")],
        ),
        "svc/main.go": facts("go", [d("run")], [Ref("w:API_KEY", "env", 2, "run"), Ref("w:HOME", "env", 3, "run")]),
        "svc/also.py": facts("python", [d("g")], [Ref("w:API_KEY", "env", 2, "g")]),
    }
    assert cross(files) == [
        ("py/app.py#main", "svc/main.go#run", "env", "N")
    ]  # HOME is ambient; the writer in python is the same group


def test_a_variable_that_too_many_places_write_links_nothing():
    n = xlang.data()["env_max_writers"] + 1
    files = {f"w{i}/main.go": facts("go", [d("run")], [Ref("w:X", "env", 1, "run")]) for i in range(n)}
    files["py.py"] = facts("python", [d("m")], [Ref("r:X", "env", 1, "m")])
    assert cross(files) == []
    files.pop("w0/main.go")
    assert len(cross(files)) == n - 1


def test_a_configuration_key_is_an_env_edge_with_its_own_evidence_rule():
    files = {
        "j/App.java": facts("java", [d("App"), d("App.run")], [Ref("r:cfg:db.url", "env", 4, "App.run")]),
        "svc/conf.go": facts("go", [d("load")], [Ref("w:cfg:db.url", "env", 2, "load")]),
    }
    res = dlink.link(files, workers=1)
    e = next(e for e in res.graph.edges if e.kind == "env")
    assert (e.src, e.dst, e.tier) == ("java:j/App.java#App.run", "go:svc/conf.go#load", "N")
    assert res.graph.evid[e.ev].snippet.startswith("cfg")


def test_a_write_is_no_use_and_no_edge_is_made_from_it():
    files = {
        "a.go": facts("go", [d("f")], [Ref("w:X", "env", 1, "f")]),
        "b.py": facts("python", [d("g")], [Ref("w:X", "env", 1, "g")]),
    }
    assert cross(files) == []
