"""graph.snippets: the code of the symbols on a page, behind a gitleaks scan; without the scan there is no code.

python3 -m pytest -q tests/test_graph_snippets.py

The scan is a stand-in script (tests/gitleaks_stand_in.py) pointed at by $DEAD_CODE_CHECKER_GITLEAKS; one test uses the real gitleaks
when it is installed. The code under test is the toy language dcref (tests/graph_support.py).
"""

import shutil
import sys
from pathlib import Path

import pytest
from graph_support import new_repo, write

from dead_code_checker.graph import config, levels, snippets

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.usefixtures("hermetic_git", "data_root", "dcref"),
]
STAND_IN = str(Path(__file__).with_name("gitleaks_stand_in.py"))
APP = "def helper\ndef run\nref helper\ndef main\nref run\nroot main main\n"
LEAKY = APP.replace("def run\n", "def run # SECRET_TOKEN\n")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    p = tmp_path / "gitleaks"
    p.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(Path(STAND_IN).parent)!r})\nimport gitleaks_stand_in as g\nsys.exit(g.main(sys.argv[1:], sys.argv[0]))\n")
    p.chmod(0o755)
    monkeypatch.setenv(snippets.ENV_GITLEAKS, str(p))
    return str(p)


def build(tmp_path, app):
    repo = new_repo(tmp_path, {"lib/app.dcref": app, "lib/other.dcref": "def spare\n", "run.dcroots": "main file:lib/app.dcref\n"})
    write(repo, "lib/other.dcref", "def spare\n# edited\n")  # a dirty file: its text is read from the work tree
    b, res = levels.analyzed(str(repo))
    return str(repo), b, res


@pytest.fixture
def built(tmp_path):
    return build(tmp_path, APP)


def collect(built, **kw):
    root, b, res = built
    kw.setdefault("cfg", config.load(root))
    return snippets.collect(root, b.graph, res, b.tree, None, (), **kw)


def key(b, qual):
    return next(n.key for n in b.graph.nodes if n.key.endswith("#" + qual))


def test_the_code_of_a_symbol_is_on_the_page_when_the_scan_is_clean(built, fake):
    s = collect(built)
    b = built[1]
    assert s.info["state"] == "on" and s.info["hidden"] == 0 and not s.findings
    assert s.text[key(b, "helper")] == "def helper" and s.text[key(b, "spare")] == "def spare"
    assert s.info["local"] == snippets.LOCAL and s.info["bytes"] == sum(len(t) for t in s.text.values())


def test_a_symbol_with_a_finding_is_replaced_by_the_hidden_marker(tmp_path, fake):
    leaky = build(tmp_path, LEAKY)
    s = collect(leaky)
    b = leaky[1]
    assert s.text[key(b, "run")] == snippets.HIDDEN and s.info["hidden"] == 1
    assert "SECRET_TOKEN" not in "".join(s.text.values())


def test_without_gitleaks_there_is_no_code_and_a_note_says_how_to_get_it(built, monkeypatch):
    monkeypatch.setattr(snippets, "find_gitleaks", lambda: None)
    s = collect(built)
    assert s.text == {} and s.info["state"] == "off" and not s.ev_ok
    (f,) = s.findings
    assert f.code == "dc.viewer-code-off" and f.level == "W" and "gitleaks is not installed" in f.msg and "install gitleaks" in f.msg


def test_a_scan_that_fails_means_no_code_never_unscanned_code(built, fake):
    Path(fake + ".fail").write_text("")
    s = collect(built)
    assert s.text == {} and s.findings[0].code == "dc.viewer-code-off" and "exit 7" in s.findings[0].msg


def test_viewer_code_false_reads_nothing_and_raises_no_finding(built, fake, tmp_path):
    root = built[0]
    write(root, ".dead-code-checker.toml", "[graph]\nviewer_code = false\n")
    s = collect(built, cfg=config.load(root))
    assert s.text == {} and s.findings == () and s.info["state"] == "off"


def test_the_byte_budget_keeps_the_dead_and_says_so(built, fake):
    root = built[0]
    write(root, ".dead-code-checker.toml", "[graph]\nviewer_code_bytes = 12\n")
    s = collect(built, cfg=config.load(root))
    b = built[1]
    assert s.info["state"] == "budget" and s.info["bytes"] <= 12
    assert key(b, "spare") in s.text  # the dead one comes first


def test_a_file_changed_since_the_build_has_no_code(built, fake):
    root = built[0]
    write(root, "lib/other.dcref", "def spare\ndef more\n")  # edited again after the build
    s = collect(built)
    b = built[1]
    assert key(b, "spare") not in s.text and s.info["stale"] >= 1 and key(b, "helper") in s.text


def test_a_scan_that_runs_over_the_deadline_gives_no_code(built, fake):
    import time

    Path(fake + ".sleep").write_text("30")
    s = collect(built, deadline=time.monotonic() + 3)
    assert s.text == {} and s.info["reason"] == "budget"


def test_a_deadline_that_has_passed_gives_no_code(built, fake):
    s = collect(built, deadline=0.0)
    assert s.text == {} and s.info["reason"] == "budget"


def test_cut_and_text_lines():
    lines = [f"l{i}" for i in range(100)]
    t = snippets.cut(lines, 1, 100)
    assert t.count("\n") + 1 == snippets.MAX_LINES and t.endswith("more lines)")
    assert snippets.cut(lines, 0, 3) == "" and snippets.cut(lines, 200, 300) == ""
    assert snippets.text_lines(b"a\r\nb") == ["a", "b"] and snippets.text_lines(b"a\0b") is None


def test_the_variable_names_the_binary_only_when_it_is_an_absolute_executable(tmp_path, monkeypatch):
    monkeypatch.setenv(snippets.ENV_GITLEAKS, "gitleaks")
    assert snippets.find_gitleaks() is None
    monkeypatch.setenv(snippets.ENV_GITLEAKS, str(tmp_path / "nope"))
    assert snippets.find_gitleaks() is None


@pytest.mark.skipif(snippets.find_gitleaks() is None, reason="gitleaks is not installed")
def test_the_real_gitleaks_hides_a_token_and_passes_clean_code(tmp_path):
    token = "gh" + "p_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"
    hits = snippets.scan([f'x = "{token}"\n', "y = 1\n"], snippets.find_gitleaks())
    assert [(i, a) for i, a, _b in hits] == [(0, 1)]
    assert snippets.scan([], snippets.find_gitleaks()) == []
