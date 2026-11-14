"""graph.ts: the loader of the optional tree-sitter dependency, tested against fake bindings (no grammar needed), plus one test with
the real packages that skips with its reason when they are not installed.

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_ts.py
"""

import importlib.metadata
import sys
import threading

import pytest

from dead_code_checker.graph import ts as tsload


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv(tsload.ENV_CACHE, raising=False)
    monkeypatch.delenv(tsload.ENV_DOWNLOAD, raising=False)
    tsload.reset()
    yield
    monkeypatch.undo()
    tsload.reset()


class Language:
    def __init__(self, name):
        self.name = name


def fake_bindings(monkeypatch, listed=("go", "bash"), known=("go", "bash", "rust"), query_api=True, pack_api=True):
    """Fake `tree_sitter` and `tree_sitter_language_pack` modules; returns the list of calls the loader made."""
    calls = []
    ts = type(sys)("tree_sitter")

    class Parser:
        def __init__(self, language):
            self.language = language
            calls.append(("Parser", language.name))

    ts.Parser = Parser
    if query_api:
        ts.Query = ts.QueryCursor = object
    pack = type(sys)("tree_sitter_language_pack")
    pack.get_language = lambda name: calls.append(("get_language", name)) or Language(name)
    pack.has_language = lambda name: name in known
    if pack_api:
        pack.downloaded_languages = lambda: list(listed)
        pack.PackConfig = lambda cache_dir=None: ("config", cache_dir)
        pack.configure = lambda cfg: calls.append(("configure", cfg))
    monkeypatch.setitem(sys.modules, "tree_sitter", ts)
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", pack)
    return calls


def test_without_tree_sitter_a_grammar_is_missing_with_the_install_command(no_tree_sitter):
    res = tsload.grammar_status("go")
    assert res.status == "missing" and "tree-sitter is not installed" in res.detail and "[treesitter]" in res.detail
    with pytest.raises(LookupError, match="tree-sitter is not installed"):
        tsload.get_parser("go")


def test_without_the_language_pack_the_pack_is_named(monkeypatch):
    fake_bindings(monkeypatch)
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)
    with pytest.raises(LookupError, match="tree-sitter-language-pack is not installed"):
        tsload.get_parser("go")


def test_a_downloaded_known_grammar_gives_a_parser_that_is_cached_per_thread(monkeypatch):
    calls = fake_bindings(monkeypatch)
    p = tsload.get_parser("go")
    assert p.language.name == "go" and tsload.get_parser("go") is p
    assert calls.count(("get_language", "go")) == 1  # asked once
    other = []
    t = threading.Thread(target=lambda: other.append(tsload.get_parser("go")))
    t.start()
    t.join()
    assert other[0] is not p  # a Parser is not shared between threads
    assert tsload.grammar_status("go") == ("ok", "")


def test_a_grammar_that_is_not_on_disk_is_missing_and_never_fetched_unless_asked(monkeypatch):
    calls = fake_bindings(monkeypatch)
    res = tsload.grammar_status("rust")
    assert res.status == "missing" and "not on disk" in res.detail and "p.download(['rust'])" in res.detail
    assert tsload.ENV_DOWNLOAD in res.detail
    with pytest.raises(LookupError, match="not on disk"):
        tsload.get_parser("rust")
    assert ("get_language", "rust") not in calls  # the pack, which would fetch it, was not asked
    monkeypatch.setenv(tsload.ENV_DOWNLOAD, "1")
    tsload.reset()
    assert tsload.get_parser("rust").language.name == "rust"
    assert ("get_language", "rust") in calls


def test_a_grammar_the_pack_does_not_know_is_missing(monkeypatch):
    fake_bindings(monkeypatch)
    res = tsload.grammar_status("cobol")
    assert res.status == "missing" and "not known to tree-sitter-language-pack" in res.detail


def test_a_failure_is_remembered_for_the_thread_and_a_pack_error_is_a_lookup_error(monkeypatch):
    calls = fake_bindings(monkeypatch)
    sys.modules["tree_sitter_language_pack"].get_language = lambda name: calls.append(("get", name)) or 1 / 0
    for _ in range(2):
        with pytest.raises(LookupError, match="cannot be loaded"):
            tsload.get_parser("go")
    assert calls.count(("get", "go")) == 1


def test_an_old_binding_without_the_query_api_is_refused_with_the_version_hint(monkeypatch):
    fake_bindings(monkeypatch, query_api=False)
    with pytest.raises(LookupError, match="0.25 or newer"):
        tsload.get_parser("go")


def test_a_pack_without_downloaded_languages_is_never_asked_unless_the_download_is_allowed(monkeypatch):
    monkeypatch.delenv(tsload.ENV_DOWNLOAD, raising=False)
    calls = fake_bindings(monkeypatch, pack_api=False)
    res = tsload.grammar_status("bash")
    assert res.status == "missing" and "downloaded_languages" in res.detail and tsload.ENV_DOWNLOAD in res.detail
    with pytest.raises(LookupError, match="downloaded_languages"):
        tsload.get_parser("bash")
    assert not [c for c in calls if c[0] in ("get_language", "Parser")]  # no fetch was possible
    tsload.reset()
    monkeypatch.setenv(tsload.ENV_DOWNLOAD, "1")
    assert tsload.grammar_status("bash").status == "ok"
    assert tsload.get_parser("bash").language.name == "bash" and ("get_language", "bash") in calls


def test_the_cache_directory_variable_configures_the_pack_once(monkeypatch, tmp_path):
    calls = fake_bindings(monkeypatch)
    monkeypatch.setenv(tsload.ENV_CACHE, str(tmp_path))
    tsload.get_parser("go")
    tsload.grammar_status("bash")
    assert [c for c in calls if c[0] == "configure"] == [("configure", ("config", str(tmp_path)))]


def test_the_grammar_id_is_the_two_versions_or_empty(monkeypatch):
    versions = {"tree-sitter": "0.26.1", "tree-sitter-language-pack": "1.25.0"}
    monkeypatch.setattr(importlib.metadata, "version", lambda d: versions[d])
    assert tsload.grammar_id() == "tree-sitter==0.26.1|tree-sitter-language-pack==1.25.0"
    versions["tree-sitter"] = ""
    assert tsload.grammar_id() == ""

    def gone(d):
        raise importlib.metadata.PackageNotFoundError(d)

    monkeypatch.setattr(importlib.metadata, "version", gone)
    assert tsload.grammar_id() == ""


def _real():
    try:
        tsload.get_parser("bash")
        return ""
    except LookupError as exc:
        return str(exc)


@pytest.mark.skipif(bool(_real()), reason=f"tree-sitter and the bash grammar are not usable here: {_real()}")
def test_the_real_packages_parse_and_the_queries_of_the_data_compile():
    """With the extra installed and the grammar on disk: a parser works, and every query file of data/queries compiles on its
    grammar (a pattern the grammar cannot have is a loud ExtractorMissing at run time, so it is caught here)."""
    import os

    import tree_sitter

    from dead_code_checker.graph.extract import ts_generic as tg

    root = tsload.get_parser("bash").parse(b"f() { :; }\n").root_node
    assert not root.has_error and root.type == "program"
    checked = 0
    for g in sorted(
        os.listdir(tg.QUERY_DIR)
    ):  # one directory per grammar (tsx and terraform share those of typescript and hcl)
        if tsload.grammar_status(g).status != "ok":
            continue  # a grammar that is not on disk is not a failure of the queries
        language = tsload.get_parser(g).language
        for kind in tg.QUERY_KINDS:
            text = tg._query_text(g, kind)
            if text is not None:
                tree_sitter.Query(language, text)
                checked += 1
    assert checked > 0
