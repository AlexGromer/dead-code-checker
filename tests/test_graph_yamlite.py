"""graph.extract.yamlite: the indentation YAML reader (no PyYAML), as far as k8s, Helm, CI and compose files need it.

python3 -m pytest -q tests/test_graph_yamlite.py
"""

from dead_code_checker.graph.extract import yamlite as Y


# ====================================================================== yamlite
def test_yamlite_mappings_sequences_flow_and_block_scalars():
    docs, err = Y.parse(
        "a: 1  # c\nb:\n  c: 'x y'\n  d: [p, \"q,r\", {k: v}]\nl:\n- one\n- k: v\n  w: z\nrun: |\n  echo a # not a comment\n  echo b\nz: &anc\n  m: n\nt: !reference [a, b]\n"
    )
    assert err == 0 and len(docs) == 1
    d = docs[0]
    assert Y.scalar(Y.get(d, "a")) == "1" and Y.scalar(Y.get(Y.get(d, "b"), "c")) == "x y"
    flow = Y.items(Y.get(Y.get(d, "b"), "d"))
    assert [Y.scalar(x) for x in flow[:2]] == ["p", "q,r"] and Y.scalar(Y.get(flow[2], "k")) == "v"
    seq = Y.items(Y.get(d, "l"))  # a sequence at the indent of its key, and a `- key: v` item
    assert Y.scalar(seq[0]) == "one" and Y.scalar(Y.get(seq[1], "w")) == "z"
    assert Y.scalar(Y.get(d, "run")) == "echo a # not a comment\necho b"
    assert Y.scalar(Y.get(Y.get(d, "z"), "m")) == "n" and Y.scalar(Y.get(d, "t")) == "!reference [a, b]"


def test_yamlite_lines_documents_and_multiline_flow():
    docs, _ = Y.parse("---\na:\n  b: 1\n---\n# only a comment\n---\nc: [1,\n  2]\nd: x\n")
    assert len(docs) == 2
    a, c = docs
    assert (a.line, a.end, Y.get(a, "a").line, Y.get(a, "a").end) == (2, 3, 2, 3)
    assert [Y.scalar(x) for x in Y.items(Y.get(c, "c"))] == ["1", "2"] and Y.get(c, "d").line == 9


def test_yamlite_errors_are_counted_and_never_raise():
    docs, err = Y.parse("a: 1\nnot a mapping line\nb: 2\n")
    assert err == 1 and [k for k, _ in docs[0].val] == ["a", "b"]
    assert Y.parse("\t- x\n")[1] == 1 and Y.parse("")[0] == [] and Y.parse("just a scalar\n")[0] == []
    deep = "".join("  " * i + "k:\n" for i in range(200)) + "  " * 200 + "v: 1\n"
    assert Y.parse(deep)[0]  # the depth guard, no RecursionError


def test_yamlite_keys_with_colons_quotes_and_duplicates():
    d = Y.parse('"a b": 1\nurl: http://x/y:80\nk: 1\nk: 2\n"on": push\n')[0][0]
    assert [k for k, _ in d.val] == ["a b", "url", "k", "k", "on"] and Y.scalar(Y.get(d, "url")) == "http://x/y:80"


def test_walk_and_scalars_visit_every_node_in_order_and_skip_what_they_are_told():
    d = Y.parse("a:\n  b: 1\n  c:\n    e: 2\nd: z\n")[0][0]
    assert [k for k, _ in Y.walk(d)] == ["a", "b", "c", "e", "d"]
    assert [k for k, _ in Y.walk(d, skip=frozenset({"c"}))] == [
        "a",
        "b",
        "c",
        "d",
    ]  # `c` is seen, what is below it is not
    assert [Y.scalar(n) for n in Y.scalars(Y.parse("l: [x, y]\nm: {k: v}\n")[0][0])] == ["x", "y", "v"]
