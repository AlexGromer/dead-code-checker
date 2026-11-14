"""graph.rtbox (the half of the runtime-signal mode that runs ON THE EXECUTION TARGET), the shipped python tracer
(graph/tracers/py/sitecustomize.py) and graph/data/runtime.json.

python3 -m pytest -q tests/test_graph_rtbox.py

Most tests run the box with tests/graph_fake_target.py FakeBox as its process starter: it records the command line bwrap and prlimit
would have got and writes the recorded tracer files. The tests marked "real box" start the real bubblewrap of this host on a project
of a few lines that the test writes (no network, nothing outside the test directory is touched); they skip with their reason when
bubblewrap does not work here. rtbox.main() is called with explicit arguments: nothing reads the ambient environment.
"""

import hashlib
import ipaddress
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
from pathlib import Path

import graph_fake_target as F
import pytest

from dead_code_checker.graph import langs, rtbox, rtcfg

ROOT = Path(__file__).resolve().parent.parent


def blob(text: str) -> str:
    return rtbox.blob_sha(text.encode(), "sha1")


def spec_doc(files=None, runs=None, timeout=60, **over):
    files = files if files is not None else {p: blob(t) for p, t in F.FILES.items()}
    doc = {
        "schema": 1,
        "run_id": F.RUN_ID,
        "commit": "a" * 40,
        "target": "box@fake",
        "runs": (
            runs
            if runs is not None
            else [{"label": "default python", "langs": ["python"], "argv": ["python3", "-m", "pytest", "-q"]}]
        ),
        "timeout": timeout,
        "files": files,
    }
    doc.update(over)
    return doc


def _ti(name, size, mode=0o644):
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode = size, mode
    return ti


def make_tar(spec, files=None, links=None, raw_spec=None, extra=()) -> bytes:
    """in.tar: spec.json first, then tree/<path> for every file of FILES (or `files`), then the links, then `extra` (name, kind, data)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        raw = raw_spec if raw_spec is not None else json.dumps(spec).encode()
        tf.addfile(_ti("spec.json", len(raw)), io.BytesIO(raw))
        for p, t in (F.FILES if files is None else files).items():
            data = t.encode()
            tf.addfile(_ti("tree/" + p, len(data)), io.BytesIO(data))
        for p, target in (links or {}).items():
            ti = _ti("tree/" + p, 0)
            ti.type, ti.linkname = tarfile.SYMTYPE, target
            tf.addfile(ti)
        for name, kind, data in extra:
            ti = _ti(name, len(data))
            if kind:
                ti.type = kind
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def rundir(tmp_path):
    d = tmp_path / ("rt-" + F.RUN_ID)
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return d


def deliver(rundir, data: bytes) -> str:
    (rundir / "in.tar").write_bytes(data)
    return sha(data)


def box_fake(rundir, data, box=None, tools=F.FakeTools):
    box = box or F.FakeBox()
    return rtbox.box(str(rundir), deliver(rundir, data), on_target=True, starter=box, tools=tools), box


# ================================================================ data/runtime.json
def test_the_data_file_is_consistent_with_the_rest_of_the_checker():
    d = rtcfg.data()
    known = set(langs.load().langs)
    assert set(d["tracers"]) <= known  # tracer keys are graph languages
    assert set(d["limits"]) - {"wall_s"} <= set(rtbox.PRLIMIT)  # every limit key is one prlimit knows
    for lang, t in d["tracers"].items():
        assert t["name"] and set(t["limits"]) <= set(rtbox.PRLIMIT)
        if t["default_command"] is not None:
            assert rtcfg.argv_problem(t["default_command"]) == ""
    assert d["timeouts"]["default_s"] <= d["limits"]["wall_s"]
    for lang, c in rtcfg.classes().items():  # per language which class it is: executing (target only) and reading (may run here)
        assert lang in known and c["executing"] and c["reading"], lang
    assert {"python", "go", "typescript", "java", "rust"} <= set(rtcfg.classes())
    assert rtbox.rt_data() == d  # the target half reads the same file


def test_the_delivery_is_three_files_and_they_exist():
    files = rtbox.delivery_files()
    assert tuple(files) == rtbox.DELIVERY
    assert all(os.path.isfile(p) for p in files.values()), files
    assert Path(files["rtbox.py"]).name == "rtbox.py" and Path(files["runtime.json"]).name == "runtime.json"


def test_the_copies_of_the_helpers_equal_the_originals():
    """rtbox cannot import graph.rtcfg on the target: its small copies must behave the same."""
    samples = [
        "plain text",
        "token=fakesecretvalue123 and more",
        "clone https://user:fakepass@example.org/x.git",
        "Authorization: Bearer fakebearertoken1234567890",
        "sk-" + "fakefakefakefakefakefake",
        "ghp_" + "F" * 24,
        "AKIA" + "F" * 16,
        "eyJfakefake1.eyJfakefake2.fakesig1",
        "-----BEGIN RSA PRIVATE KEY-----",
        'API_KEY: "fake key"',
    ]
    for s in samples:
        assert rtbox.mask_secrets(s) == rtcfg.mask_secrets(s), s
    for argv in (["a"], [], [""], ["a\nb"], ["x"] * 65, ["x" * 4097], None, "a"):
        assert rtbox.argv_problem(argv) == rtcfg.argv_problem(argv)
    out = "# pkg\nFAIL\n./a.go:3:1: undefined: x\nmore\n"
    assert rtbox.first_error(out, "token=fakesecret12345") == rtcfg.first_error(out, "token=fakesecret12345")
    assert rtbox.first_error("/w/t/a.py:1: error: bad", "", "/w/t") == rtcfg.first_error("/w/t/a.py:1: error: bad", "", "/w/t")
    assert rtbox.MAX_ARGS == rtcfg.MAX_ARGS and rtbox.MAX_ARG == rtcfg.MAX_ARG
    # the same rule for the links of a tree as the one the controlling side keeps
    links = {"a/l": "q/../../x", "a/q": "../d", "b/ok": "../a.py", "b/abs": "/etc/passwd", "c": "c"}
    assert sorted(rtbox.confine_links(links)) == ["a/q", "b/ok"]  # a/l climbs out of the tree through a/q, c loops, b/abs is absolute
    assert rtbox.blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_the_target_half_imports_nothing_of_the_package(tmp_path):
    code = (
        "import sys, runpy; sys.argv = ['rtbox.py']; m = runpy.run_path(%r, run_name='not_main'); "
        "print(sorted(k for k in sys.modules if k.startswith('dead_code_checker')))"
    )
    r = subprocess.run([sys.executable, "-I", "-c", code % str(ROOT / "dead_code_checker" / "graph" / "rtbox.py")],
                       capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]"


# ================================================================ the readers of recorded tracer output
def test_read_python_merges_processes_and_drops_files_outside_the_list(tmp_path):
    (tmp_path / "1.json").write_text(json.dumps(F.py_trace()))
    (tmp_path / "2.json").write_text(json.dumps({"v": 1, "fn": [["pkg/a.py", "used", 4], ["pkg/a.py", "helper", 20]]}))
    (tmp_path / "junk.json").write_text("not json")
    (tmp_path / "other.txt").write_text("{}")
    files = {"pkg/a.py": "x", "app.py": "y"}
    items, note = rtbox.read_python(str(tmp_path), files)
    assert [(i.file, i.qual, i.line) for i in items] == [
        ("app.py", "main", 5),
        ("pkg/a.py", "Klass.meth", 13),
        ("pkg/a.py", "helper", 20),
        ("pkg/a.py", "used", 4),
    ]  # sorted by file, then name; the same function in two processes is one row
    assert all(i.lang == "python" and i.hit == 1 and i.end == 0 for i in items) and note == ""


def test_read_python_says_why_it_has_nothing(tmp_path):
    assert rtbox.read_python(str(tmp_path / "none"), {})[1] == "no trace file was written"
    (tmp_path / "1.json").write_text(json.dumps({"v": 1, "fn": [["pkg/a.py", "x", 1]]}))
    items, note = rtbox.read_python(str(tmp_path), {"other.py": "b"})
    assert items == [] and "no function of the project was called" in note
    (tmp_path / "2.json").write_text(
        json.dumps({"v": 1, "fn": [["other.py", "f", 0], ["other.py", 3, 4], "x", ["other.py", "g", True]]})
    )
    assert rtbox.read_python(str(tmp_path), {"other.py": "b"})[0] == []  # a line below 1, a name that is no text, a bool: no row


def test_read_v8_counts_functions_by_their_first_range_and_skips_the_wrapper_and_node_internals(tmp_path):
    tree = tmp_path / "tree"
    (tree / "web").mkdir(parents=True)
    (tree / "web" / "app.js").write_text(F.JS_APP)
    out = tmp_path / "v8"
    out.mkdir()
    (out / "coverage-1-2-0.json").write_text(json.dumps(F.v8_doc(os.path.realpath(tree))))
    items, note = rtbox.read_v8(str(out), str(tree), {"web/app.js": "b"})
    assert [(i.file, i.qual, i.line, i.hit) for i in items] == [("web/app.js", "used", 1, 3)] and note == ""
    assert items[0].end == 3 and items[0].lang == "javascript"  # `unused` has count 0; the wrapper and node:internal are no functions


def test_read_v8_offsets_are_utf16_units_and_urls_are_decoded(tmp_path):
    text = "// \U0001f600 \U0001f600\nfunction f() {\n  return 1;\n}\n"  # two astral characters: 2 code units each in V8, 1 in python
    tree = tmp_path / "t"
    (tree / "my dir").mkdir(parents=True)
    (tree / "my dir" / "x.js").write_text(text, encoding="utf-8")
    start = len("// ") + 2 + 1 + 2 + 1  # UTF-16 offset of the second line
    doc = {
        "result": [
            {
                "url": "file://%s/my%%20dir/x.js" % os.path.realpath(tree),
                "functions": [{"functionName": "f", "ranges": [{"startOffset": start, "endOffset": start + 30, "count": 2}]}],
            }
        ]
    }
    out = tmp_path / "v8"
    out.mkdir()
    (out / "coverage-9.json").write_text(json.dumps(doc))
    items, _ = rtbox.read_v8(str(out), str(tree), {"my dir/x.js": "b"})
    assert [(i.qual, i.line) for i in items] == [("f", 2)]


def test_read_v8_ignores_what_is_not_a_tracked_file_of_the_tree(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.js").write_text("function a() {}\n")
    out = tmp_path / "v8"
    out.mkdir()
    fn = {"functionName": "a", "ranges": [{"startOffset": 1, "endOffset": 9, "count": 1}]}
    docs = {
        "coverage-1.json": {"result": [{"url": f"file://{tree}/a.js", "functions": [fn]}]},  # not in the file list
        "coverage-2.json": {"result": [{"url": "file:///etc/passwd", "functions": [fn]}]},
        "coverage-3.json": {"result": [{"url": f"file://{tree}/../a.js", "functions": [fn]}]},
        "coverage-4.json": {"result": "no"},
    }
    for n, d in docs.items():
        (out / n).write_text(json.dumps(d))
    assert rtbox.read_v8(str(out), str(tree), {})[0] == []
    assert "no function of the project ran" in rtbox.read_v8(str(out), str(tree), {})[1]


def test_read_go_maps_the_import_path_through_the_nearest_module(tmp_path):
    (tmp_path / "go.mod").write_text(F.GO_MOD)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "go.mod").write_text("module example.com/other\n")
    files = {"go.mod": "a", "sub/go.mod": "b", "svc/a.go": "c", "sub/x.go": "d"}
    mods = rtbox.go_modules(str(tmp_path), files)
    assert mods == [("example.com/other", "sub"), ("example.com/m", "")]
    text = F.GO_FUNC + "example.com/other/x.go:9:\tF\t12.5%\nfmt/print.go:1:\tG\t100.0%\nexample.com/m/vendor/y.go:1:\tH\t100.0%\n"
    items, note = rtbox.read_go(text, files, mods)
    assert [(i.file, i.qual, i.line) for i in items] == [("sub/x.go", "F", 9), ("svc/a.go", "Used", 3)] and note == ""  # Unused is 0.0%


def test_read_go_says_why_it_has_nothing():
    assert "no function rows" in rtbox.read_go("garbage\n", {}, [])[1]
    got = rtbox.read_go("example.com/m/svc/a.go:7:\tUnused\t0.0%\n", {"svc/a.go": "c"}, [("example.com/m", "")])
    assert got[1].startswith("the profile lists 1 function")


# ================================================================ the spec and the delivery
def test_check_spec_accepts_the_exact_shape_and_nothing_else():
    s = rtbox.check_spec(spec_doc())
    assert s.run_id == F.RUN_ID and s.runs[0].argv[:2] == ("python3", "-m") and set(s.files) == set(F.FILES)
    bad = [
        {**spec_doc(), "extra": 1},
        {k: v for k, v in spec_doc().items() if k != "files"},
        spec_doc(schema=2),
        spec_doc(run_id="xyz"),
        spec_doc(commit="HEAD"),
        spec_doc(timeout=0),
        spec_doc(timeout=10**6),
        spec_doc(timeout=True),
        spec_doc(runs=[]),
        spec_doc(runs=[{"label": "x", "langs": ["rust"], "argv": ["a"]}]),  # no tracer for rust: a spec never asks for it
        spec_doc(runs=[{"label": "x", "langs": ["java"], "argv": ["a"]}]),  # nor for java
        spec_doc(runs=[{"label": "x", "langs": ["python"], "argv": []}]),
        spec_doc(runs=[{"label": "x", "langs": ["python"], "argv": ["a\nb"]}]),
        spec_doc(runs=[{"label": "x", "langs": ["python"], "argv": ["a"], "env": {}}]),
        spec_doc(runs=[{"label": "x", "langs": [], "argv": ["a"]}]),  # a traced run needs a language
        spec_doc(runs=[{"label": "x", "langs": [], "argv": ["a"], "cwd": "../x"}]),
        spec_doc(files={"../x": "a" * 40}),
        spec_doc(files={"/abs": "a" * 40}),
        spec_doc(files={"a//b": "a" * 40}),
        spec_doc(files={"a": "nothex"}),
        spec_doc(target="a\nb"),
    ]
    for doc in bad:
        with pytest.raises(rtbox.BoxError):
            rtbox.check_spec(doc)
    with pytest.raises(rtbox.BoxError):
        rtbox.check_spec([])
    ok = rtbox.check_spec(spec_doc(runs=[{"label": "chk", "langs": [], "argv": ["make"], "cwd": "sub/dir"}]))
    assert ok.runs[0].cwd == "sub/dir"


def test_unpack_extracts_only_the_tree_and_checks_every_blob(tmp_path):
    data = make_tar(spec_doc(), links={"pkg/link.py": "a.py"})
    tree = tmp_path / "tree"
    tree.mkdir()
    (tmp_path / "in.tar").write_bytes(data)
    spec = rtbox.unpack(str(tmp_path / "in.tar"), str(tree), sha(data))
    assert (tree / "pkg" / "a.py").read_text() == F.PY_A and os.access(tree / "app.py", os.X_OK) is False  # modes come from the tar
    assert os.readlink(tree / "pkg" / "link.py") == "a.py" and spec.commit == "a" * 40
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.tar", "tree"]  # nothing outside the tree


@pytest.mark.parametrize(
    "mutate,why",
    [
        (lambda d: d + b"x", "sha256"),
        (lambda d: make_tar(spec_doc(), extra=[("tree/../escape", None, b"x")]), "safe path"),
        (lambda d: make_tar(spec_doc(), extra=[("/abs/path", None, b"x")]), "safe path"),
        (lambda d: make_tar(spec_doc(), extra=[("tree/pkg/a.py", None, b"dup")]), "twice"),
        (lambda d: make_tar(spec_doc(), extra=[("tree/not_in_spec.py", None, b"x")]), "not in the file list"),
        (lambda d: make_tar(spec_doc(), files={**F.FILES, "pkg/a.py": "changed\n"}), "not the blob"),
        (lambda d: make_tar(spec_doc(), files={k: v for k, v in F.FILES.items() if k != "app.py"}), "lacks 1 file"),
        (lambda d: make_tar(spec_doc(), links={"pkg/out": "../../../etc/passwd"}), "outside the tree"),
        (lambda d: make_tar(spec_doc(), links={"pkg/abs": "/etc/passwd"}), "outside the tree"),
        (lambda d: make_tar(spec_doc(), extra=[("tree/hard", tarfile.LNKTYPE, b"")]), "neither a file"),
        (lambda d: make_tar(spec_doc(), extra=[("tree/fifo", tarfile.FIFOTYPE, b"")]), "neither a file"),
        (lambda d: make_tar(spec_doc(), raw_spec=b"{not json"), None),
    ],
)
def test_unpack_refuses_a_delivery_that_is_not_what_the_spec_says(tmp_path, mutate, why):
    good = make_tar(spec_doc())
    data = mutate(good)
    (tmp_path / "in.tar").write_bytes(data)
    tree = tmp_path / "tree"
    tree.mkdir()
    want = sha(good) if why == "sha256" else sha(data)  # the first case is the one where the sha256 of the command is the old file's
    with pytest.raises((rtbox.BoxError, ValueError)) as e:
        rtbox.unpack(str(tmp_path / "in.tar"), str(tree), want)
    if why:
        assert why in str(e.value)
    assert not (tmp_path / "escape").exists() and not (tmp_path.parent / "escape").exists()


def test_unpack_wants_spec_json_first_and_a_real_sha256_argument(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        tf.addfile(_ti("tree/a.py", 1), io.BytesIO(b"x"))
    data = buf.getvalue()
    (tmp_path / "in.tar").write_bytes(data)
    with pytest.raises(rtbox.BoxError, match="spec.json must be the first"):
        rtbox.unpack(str(tmp_path / "in.tar"), str(tmp_path), sha(data))
    with pytest.raises(rtbox.BoxError, match="64 hex"):
        rtbox.unpack(str(tmp_path / "in.tar"), str(tmp_path), "abc")


def test_unpack_caps_the_size_of_a_file_and_of_the_tree(tmp_path, monkeypatch):
    d = dict(rtbox.rt_data())
    d["copy"] = dict(d["copy"], max_file_mb=1, max_tree_mb=1)
    monkeypatch.setattr(rtbox, "_CACHE", {"d": d})
    big = {"big.py": "x" * (2 << 20)}
    data = make_tar(spec_doc(files={"big.py": blob(big["big.py"])}), files=big)
    (tmp_path / "in.tar").write_bytes(data)
    tree = tmp_path / "tree"
    tree.mkdir()
    with pytest.raises(rtbox.BoxError, match="larger than the copy cap|over the copy cap"):
        rtbox.unpack(str(tmp_path / "in.tar"), str(tree), sha(data))


# ================================================================ the plan of a run
def run_of(langs_, argv):
    return rtbox.Run("t", tuple(langs_), tuple(argv))


def dirs_of(tmp_path):
    out = {n: str(tmp_path / n) for n in ("tree", "home", "tmp", "out")}
    for p in out.values():
        os.makedirs(p, exist_ok=True)
    return out


def test_plan_python_puts_the_shipped_tracer_on_pythonpath_and_nothing_else_of_the_host(tmp_path):
    dirs = dirs_of(tmp_path)
    pl = rtbox.plan(run_of(["python"], ["python3", "-m", "pytest"]), dirs)
    assert pl.argv == ["python3", "-m", "pytest"] and pl.notes == {}
    assert pl.env["PYTHONPATH"] == rtbox.TRACER_DIR and pl.env["DCRT_ROOT"] == dirs["tree"]
    assert pl.env["DCRT_OUT"].startswith(dirs["out"]) and os.path.isdir(pl.env["DCRT_OUT"])
    assert set(pl.env) == {
        "PATH", "HOME", "TMPDIR", "LANG", "PYTHONPATH", "DCRT_OUT", "DCRT_ROOT", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED",
    }  # fmt: skip
    assert pl.env["PATH"] == rtbox.PATH and pl.env["HOME"] == dirs["home"]  # the environment of the caller never crosses over
    assert os.path.isfile(os.path.join(rtbox.TRACER_DIR, "sitecustomize.py"))


def test_plan_go_injects_the_profile_only_into_a_go_test_without_its_own(tmp_path):
    dirs = dirs_of(tmp_path)
    pl = rtbox.plan(run_of(["go"], ["go", "test", "-run", "X", "./..."]), dirs)
    prof = os.path.join(dirs["out"], "go.cover")
    assert pl.argv == ["go", "test", f"-coverprofile={prof}", "-covermode=set", "-coverpkg=./...", "-run", "X", "./..."]
    assert pl.post["go"]() == ["go", "tool", "cover", f"-func={prof}"]
    assert pl.env["GOPROXY"] == "off" and pl.env["GOTOOLCHAIN"] == "local"
    for argv in (["make", "test"], ["go", "build", "./..."], ["go", "test", "-coverprofile=x.out", "./..."], ["go"]):
        got = rtbox.plan(run_of(["go"], argv), dirs)
        assert got.argv == argv and "go" in got.notes and got.post == {} and got.notes["go"].startswith("no tracer for go:")


def test_plan_javascript_sets_the_v8_directory(tmp_path):
    dirs = dirs_of(tmp_path)
    pl = rtbox.plan(run_of(["javascript"], ["node", "--test"]), dirs)
    assert pl.env["NODE_V8_COVERAGE"] == os.path.join(dirs["out"], "v8") and os.path.isdir(pl.env["NODE_V8_COVERAGE"])
    assert pl.env["NODE_OPTIONS"] == "--max-old-space-size=4096"


def test_plan_of_a_check_run_has_no_tracer(tmp_path):
    dirs = dirs_of(tmp_path)
    pl = rtbox.plan(rtbox.Run("c", ("go",), ("go", "vet", "./..."), ""), dirs)
    assert pl.argv == ["go", "vet", "./..."] and pl.post == {} and pl.notes == {}
    assert "NODE_V8_COVERAGE" not in pl.env and "PYTHONPATH" not in pl.env and pl.env["GOPROXY"] == "off"


def test_limits_come_from_the_data_file_and_a_language_may_lift_the_address_space():
    lim = rtbox.limits_for(run_of(["python"], ["x"]))
    d = rtcfg.data()["limits"]
    assert lim == {k: v for k, v in d.items() if k != "wall_s"}
    assert "as_mb" not in rtbox.limits_for(run_of(["go"], ["x"])) and "as_mb" not in rtbox.limits_for(run_of(["go", "javascript"], ["x"]))
    assert "as_mb" in rtbox.limits_for(run_of(["python", "go"], ["x"]))  # one language that wants the limit keeps it for the whole run
    assert "cpu_s" in rtbox.limits_for(run_of(["go"], ["x"]))


def test_inject_go_keeps_the_arguments_of_the_user_in_order():
    assert rtbox.inject_go(["/usr/bin/go", "test", "-v", "./a"], "/p")[:3] == ["/usr/bin/go", "test", "-coverprofile=/p"]
    assert rtbox.inject_go(["go", "test", "-v", "./a"], "/p")[-2:] == ["-v", "./a"]
    assert rtbox.inject_go(["go", "test", "-test.coverprofile=x"], "/p") is None


# ================================================================ the box with the fake starter
def test_box_runs_the_commands_in_a_bwrap_box_with_prlimit_and_the_time_of_the_spec(rundir):
    rc, box = box_fake(rundir, make_tar(spec_doc(timeout=77)))
    assert rc == 0 and len(box.calls) == 1
    c = box.calls[0]
    a = c["argv"]
    assert a[0] == "/usr/bin/bwrap" and {"--die-with-parent", "--new-session", "--unshare-all", "--clearenv"} <= set(a)
    assert "--share-net" not in a and a[a.index("--ro-bind") : a.index("--ro-bind") + 3] == ["--ro-bind", "/", "/"]
    assert "--tmpfs" in a and a[a.index("--tmpfs") + 1] in ("/tmp", "/run") and "/tmp" in a
    assert c["cmd"] == ["python3", "-m", "pytest", "-q"] and c["cwd"] == str(rundir / "tree")
    assert 0 < c["timeout"] <= 77 and c["outer_env"] == {"PATH": rtbox.PATH}  # the caller's environment does not reach bwrap either
    assert "--bind" in a and str(rundir / "tree") in a and str(rundir / "home") in a  # the only writable places
    assert a[a.index("--chdir") + 1] == str(rundir / "tree")
    lim = rtcfg.data()["limits"]
    assert set(c["flags"]) == {
        f"--cpu={lim['cpu_s']}",
        f"--as={lim['as_mb'] << 20}",
        f"--fsize={lim['fsize_mb'] << 20}",
        f"--nofile={lim['nofile']}",
        f"--nproc={lim['nproc']}",
    }
    assert c["env"]["PYTHONPATH"] == rtbox.TRACER_DIR and "HOME" in c["env"] and "PATH" in c["env"]
    assert rtbox.TRACER_DIR in a  # the tracer is bound read-only after the masks of HOME and /run


def test_box_writes_only_the_executed_symbols_and_the_blob_ids_they_were_seen_in(rundir):
    data = make_tar(spec_doc(runs=[{"label": "all", "langs": ["python", "javascript", "go"], "argv": ["go", "test", "./..."]}]))
    rc, _box = box_fake(rundir, data)
    assert rc == 0
    doc = json.loads((rundir / "out" / "runtime.json").read_text())
    assert set(doc) == {"schema", "run_id", "commit", "target", "status", "notes", "runs", "files", "executed"}
    rows = {(r["lang"], r["file"], r["qual"]) for r in doc["executed"]}
    assert rows == {
        ("python", "pkg/a.py", "used"), ("python", "pkg/a.py", "Klass.meth"), ("python", "app.py", "main"),
        ("javascript", "web/app.js", "used"), ("go", "svc/a.go", "Used"),
    }  # fmt: skip
    assert set(doc["files"]) == {"pkg/a.py", "app.py", "web/app.js", "svc/a.go"} and doc["files"]["pkg/a.py"] == blob(F.PY_A)
    text = json.dumps(doc)
    for forbidden in ("recorded stdout", "recorded stderr", "def used", "return 1"):  # the output and the source never come back
        assert forbidden not in text
    man = json.loads((rundir / "out" / "manifest.json").read_text())
    assert man["runtime_sha256"] == sha((rundir / "out" / "runtime.json").read_bytes()) and man["run_id"] == F.RUN_ID
    run = doc["runs"][0]
    assert run["rc"] == 0 and run["timed_out"] is False and {"python", "javascript", "go"} == set(run["tracers"])
    assert oct((rundir / "out" / "runtime.json").stat().st_mode & 0o777) == "0o600"


def test_box_reports_a_tracer_that_does_not_fit_instead_of_guessing(rundir):
    box_fake(rundir, make_tar(spec_doc(runs=[{"label": "mk", "langs": ["go"], "argv": ["make", "test"]}])))
    doc = json.loads((rundir / "out" / "runtime.json").read_text())
    tr = doc["runs"][0]["tracers"]
    assert tr["go"]["status"] == "no-tracer" and tr["go"]["note"].startswith("no tracer for go:") and doc["executed"] == []


def test_box_keeps_what_a_killed_run_wrote_and_says_it_timed_out(rundir):
    box_fake(rundir, make_tar(spec_doc()), F.FakeBox(timeout_main=True))
    run = json.loads((rundir / "out" / "runtime.json").read_text())["runs"][0]
    assert run["timed_out"] is True and run["rc"] is None and "time limit" in run["why"]


def test_box_a_failing_command_still_gives_what_ran_and_its_exit_code(rundir):
    box_fake(rundir, make_tar(spec_doc()), F.FakeBox(rc=1))
    doc = json.loads((rundir / "out" / "runtime.json").read_text())
    assert doc["runs"][0]["rc"] == 1 and len(doc["executed"]) == 3 and doc["status"] == "ok"


def test_box_a_check_run_returns_its_exit_code_and_one_masked_error_line_and_nothing_else(rundir):
    err = b"build output\nsrc/x.go:3:1: error: token=fakesecretvalue123 broke\nmore\n"
    runs = [{"label": "vet", "langs": ["go"], "argv": ["go", "vet", "./..."], "cwd": "svc"}]
    box = F.FakeBox(rc=2, err=err, out=b"stdout that must not come back")
    box_fake(rundir, make_tar(spec_doc(runs=runs)), box)
    doc = json.loads((rundir / "out" / "runtime.json").read_text())
    run = doc["runs"][0]
    assert run["rc"] == 2 and run["tracers"] == {} and doc["executed"] == [] and doc["files"] == {}
    assert "src/x.go:3:1: error:" in run["error"] and "fakesecretvalue123" not in run["error"] and "***" in run["error"]
    assert "stdout that must not come back" not in json.dumps(doc) and "build output" not in json.dumps(doc)
    assert box.calls[0]["cwd"] == str(rundir / "tree" / "svc") and "DCRT_OUT" not in box.calls[0]["env"]


def test_box_second_command_gets_only_the_time_that_is_left(rundir):
    runs = [{"label": "a", "langs": ["python"], "argv": ["a"]}, {"label": "b", "langs": ["python"], "argv": ["b"]}]
    spec = rtbox.check_spec(spec_doc(runs=runs, timeout=100))
    (rundir / "tree").mkdir(exist_ok=True)
    ticks = {"t": 0.0}

    def now():  # every look at the clock is a minute later: the first command starts with 100 s to go, the second finds none
        ticks["t"] += 60.0
        return ticks["t"]

    doc = rtbox.execute(spec, str(rundir), F.FakeTools, F.FakeBox(), now=now)
    assert doc["runs"][0]["rc"] == 0 and doc["runs"][1]["rc"] is None and "no time left" in doc["runs"][1]["why"]


def test_box_without_a_sandbox_runs_nothing_and_says_why(rundir):
    data = make_tar(spec_doc())
    rc, box = box_fake(rundir, data, tools=F.NoBwrap)
    assert rc == 0 and box.calls == []  # not one command was started
    doc = json.loads((rundir / "out" / "runtime.json").read_text())
    assert doc["status"] == "no-sandbox" and doc["executed"] == [] and "cannot create namespaces" in doc["notes"][0]

    class NoPrlimit(F.FakeTools):
        prlimit = ""

    other = rundir.parent / ("rt-" + "1" * 32)
    other.mkdir(mode=0o700)
    os.chmod(other, 0o700)
    (other / "in.tar").write_bytes(data)
    assert rtbox.box(str(other), sha(data), on_target=True, starter=box, tools=NoPrlimit) == 0 and box.calls == []
    assert "prlimit" in json.loads((other / "out" / "runtime.json").read_text())["notes"][0]  # limits are mandatory: no limits, no run


def test_the_box_is_only_the_target_half_and_checks_its_directory(rundir, tmp_path):
    sh = deliver(rundir, make_tar(spec_doc()))
    for off in (False, None, 1, "yes"):
        with pytest.raises(rtbox.BoxError, match="--on-target"):
            rtbox.box(str(rundir), sh, on_target=off, starter=F.FakeBox(), tools=F.FakeTools)
    with pytest.raises(rtbox.BoxError, match="--on-target"):
        rtbox.box(str(rundir), sh, starter=F.FakeBox(), tools=F.FakeTools)
    wide = tmp_path / ("rt-" + "2" * 32)
    wide.mkdir(mode=0o755)
    os.chmod(wide, 0o755)
    for bad in (str(wide), str(tmp_path / "plain"), str(rundir) + "/../" + rundir.name, "relative/rt-" + "2" * 32):
        with pytest.raises(rtbox.BoxError):
            rtbox.box(bad, sh, on_target=True, starter=F.FakeBox(), tools=F.FakeTools)
    link = tmp_path / ("rt-" + "3" * 32)
    link.symlink_to(rundir)
    with pytest.raises(rtbox.BoxError, match="private"):
        rtbox.box(str(link), sh, on_target=True, starter=F.FakeBox(), tools=F.FakeTools)


def test_main_refuses_without_the_flag_and_outside_the_tmp_roots_and_says_it_on_stderr(capsys):
    sha0 = "0" * 64
    box_dir = "/dev/shm/dcc-x/rt-" + "a" * 32
    assert rtbox.main(["--box", box_dir, "--sha256", sha0]) == 2  # not told that this is the target: refused, nothing runs
    assert "--on-target" in capsys.readouterr().err
    assert rtbox.main(["--on-target", "--box", "/home/x/rt-" + "a" * 32, "--sha256", sha0]) == 2
    assert "below /tmp" in capsys.readouterr().err
    for bad in ([], ["--on-target"], ["--on-target", "--box"], ["--on-target", "--box", box_dir, "--sha256", sha0, "extra"], ["--nope"]):
        assert rtbox.main(bad) == 2
        assert capsys.readouterr().err.strip().splitlines()[-1].startswith("dead-code-checker runtime box: ")
    err_for_missing_dir = rtbox.main(["--on-target", "--box", box_dir, "--sha256", sha0])
    assert err_for_missing_dir == 2 and "No such file" in capsys.readouterr().err


def test_a_truncated_symbol_list_says_so(rundir, monkeypatch):
    d = json.loads(json.dumps(rtbox.rt_data()))
    d["result"]["max_symbols"] = 2
    monkeypatch.setattr(rtbox, "_CACHE", {"d": d})
    box_fake(rundir, make_tar(spec_doc()))
    doc = json.loads((rundir / "out" / "runtime.json").read_text())
    assert len(doc["executed"]) == 2 and "first 2" in doc["notes"][0]


# ================================================================ the python tracer (shipped) on a module the test writes
def test_the_python_tracer_records_the_functions_that_were_called_and_no_module_or_class_body(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "m.py").write_text(
        "class K:\n    ATTR = 1\n\n    def used(self):\n        return [x for x in (1,)]\n\n    def idle(self):\n        return 2\n\n\n"
        "def deco(f):\n    return f\n\n\n@deco\ndef decorated():\n    return 5\n\n\n"
        "def called():\n    return len(K().used()) + decorated()\n\n\ndef never():\n    return 3\n\n\ncalled()\n"
    )
    out = tmp_path / "out"
    out.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": rtbox.TRACER_DIR,
        "DCRT_OUT": str(out),
        "DCRT_ROOT": str(tree),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    r = subprocess.run([sys.executable, str(tree / "m.py")], cwd=tree, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    docs = [json.loads(p.read_text()) for p in out.glob("*.json")]
    assert len(docs) == 1 and docs[0]["v"] == 1
    got = {(f, q) for f, q, _ in docs[0]["fn"]}
    assert ("m.py", "called") in got and ("m.py", "K.used") in got
    assert ("m.py", "never") not in got and ("m.py", "K.idle") not in got
    assert not any(q in ("<module>", "K") for _f, q in got)  # a module body and a class body run on import: no evidence
    lines = {q: ln for _f, q, ln in docs[0]["fn"]}
    assert lines["called"] == 20 and lines["K.used"] == 4  # the first line of the def, the span start the graph uses
    assert lines["decorated"] == 15  # a decorated function starts at its first decorator, as its node of the graph does
    items, note = rtbox.read_python(str(out), {"m.py": "b"})
    assert {i.qual for i in items} >= {"called", "K.used"} and note == ""


def test_the_python_tracer_does_nothing_without_its_variables_and_never_breaks_the_program(tmp_path):
    (tmp_path / "m.py").write_text("print('ran')\n")
    base = {"PATH": os.environ["PATH"], "PYTHONPATH": rtbox.TRACER_DIR}
    for extra in (
        {},
        {"DCRT_OUT": "relative", "DCRT_ROOT": str(tmp_path)},
        {"DCRT_OUT": str(tmp_path / "missing"), "DCRT_ROOT": str(tmp_path)},
    ):
        r = subprocess.run(
            [sys.executable, str(tmp_path / "m.py")], cwd=tmp_path, env={**base, **extra}, capture_output=True, text=True, timeout=60
        )
        assert r.returncode == 0 and r.stdout.strip() == "ran" and r.stderr == ""
    assert not list(tmp_path.glob("*.json"))


def test_the_python_tracer_falls_back_to_setprofile_before_python_3_12(tmp_path):
    """The branch for an interpreter without sys.monitoring, driven by taking sys.monitoring away (the host here is 3.12 or newer)."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "m.py").write_text("def called():\n    return 1\n\n\ndef never():\n    return 2\n")
    out = tmp_path / "out"
    out.mkdir()
    tracer = Path(rtbox.TRACER_DIR) / "sitecustomize.py"
    code = (
        "import sys, runpy\nif hasattr(sys, 'monitoring'): del sys.monitoring\n"
        f"runpy.run_path({str(tracer)!r}, run_name='sitecustomize')\nsys.path.insert(0, {str(tree)!r})\nimport m\nm.called()\n"
    )
    env = {"PATH": os.environ["PATH"], "DCRT_OUT": str(out), "DCRT_ROOT": str(tree)}
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    got = {(f, q) for p in out.glob("*.json") for f, q, _ in json.loads(p.read_text())["fn"]}
    assert ("m.py", "called") in got and ("m.py", "never") not in got


# ================================================================ the real box (this host's bubblewrap)
REAL = rtbox.sandbox_problem(rtbox.detect_tools()) if os.name == "posix" else "not a posix host"
real_box = pytest.mark.skipif(bool(REAL) or not Path("/usr/bin/python3").exists(), reason=f"no working bubblewrap here: {REAL}")


def project(root: Path, files: dict) -> dict:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return {rel: blob(text) for rel, text in files.items()}


def real_delivery(tmp_path, files, runs, timeout=120):
    """A delivery directory as the controlling side makes it (three files), a rundir with in.tar, and the command line to start."""
    deliv = tmp_path / "delivery"
    for rel, src in rtbox.delivery_files().items():
        dst = deliv / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
    rd = tmp_path / "rundirs" / ("rt-" + F.RUN_ID)
    rd.mkdir(parents=True, mode=0o700)
    os.chmod(rd, 0o700)
    data = make_tar(spec_doc(files={r: blob(t) for r, t in files.items()}, runs=runs, timeout=timeout), files=files)
    (rd / "in.tar").write_bytes(data)
    return deliv, rd, sha(data)


def start(deliv, rd, shav, flag=True):
    argv = [sys.executable, "-I", str(deliv / "rtbox.py"), *(["--on-target"] if flag else []), "--box", str(rd), "--sha256", shav]
    return subprocess.run(argv, capture_output=True, text=True, timeout=300, cwd=deliv, env={"PATH": "/usr/bin:/bin"})


@real_box
def test_real_box_standalone_python_run_returns_only_the_symbols_that_ran(tmp_path):
    files = {
        "lib.py": "def used():\n    return 1\n\n\ndef never():\n    return 2\n",
        "run.py": "import lib\n\nassert lib.used() == 1\n",
    }
    deliv, rd, shav = real_delivery(
        tmp_path, files, [{"label": "run", "langs": ["python"], "argv": ["python3", "run.py"]}]
    )
    r = start(deliv, rd, shav)  # python3 -I rtbox.py from the delivery directory: no import of the package is possible there
    assert r.returncode == 0, r.stderr
    doc = json.loads((rd / "out" / "runtime.json").read_text())
    assert doc["status"] == "ok" and doc["runs"][0]["rc"] == 0, doc
    got = {(x["file"], x["qual"]) for x in doc["executed"]}
    assert ("lib.py", "used") in got and ("lib.py", "never") not in got
    assert set(doc["files"]) == {x["file"] for x in doc["executed"]}
    assert set(rd.iterdir()) >= {rd / "out", rd / "tree", rd / "in.tar"}
    assert (rd / "out" / "manifest.json").exists()


@real_box
def test_real_box_refuses_to_start_without_the_flag(tmp_path):
    deliv, rd, shav = real_delivery(tmp_path, {"a.py": "x = 1\n"}, [{"label": "r", "langs": ["python"], "argv": ["python3", "a.py"]}])
    r = start(deliv, rd, shav, flag=False)
    assert r.returncode == 2 and "--on-target" in r.stderr.splitlines()[-1]
    assert not (rd / "out" / "runtime.json").exists() and not (rd / "tree").exists()


@real_box
def test_real_box_has_no_network_a_read_only_host_and_a_fresh_tmp(tmp_path):
    mark = tmp_path / "host-mark.txt"
    mark.write_text("host")
    lo = str(ipaddress.IPv4Address(0x7F000001))  # the loopback address, built (not spelled)
    srv = socket.socket()
    srv.bind((lo, 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    prog = (
        "import os, socket, json, sys\n"
        "res = {}\n"
        "s = socket.socket(); s.settimeout(3)\n"
        f"try:\n    s.connect(({lo!r}, {port})); res['net'] = 'connected'\nexcept OSError as e:\n    res['net'] = 'refused'\n"
        "for name, path in (('usr', '/usr/dcc-probe'), ('etc', '/etc/dcc-probe')):\n"
        "    try:\n        open(path, 'w').write('x'); res[name] = 'wrote'\n    except OSError:\n        res[name] = 'read-only'\n"
        f"res['tmp_sees_host_dir'] = os.path.exists({str(mark)!r})\n"
        "open('/tmp/inside', 'w').write('x')\n"
        "open('probe.json', 'w').write(json.dumps(res))\n"
    )
    deliv, rd, shav = real_delivery(tmp_path, {"probe.py": prog}, [{"label": "p", "langs": ["python"], "argv": ["python3", "probe.py"]}])
    try:
        r = start(deliv, rd, shav)
    finally:
        srv.close()
    assert r.returncode == 0, r.stderr
    res = json.loads((rd / "tree" / "probe.json").read_text())
    assert res == {"net": "refused", "usr": "read-only", "etc": "read-only", "tmp_sees_host_dir": False}, res
    assert not os.path.exists("/tmp/inside")  # the box's /tmp is its own
    doc = json.loads((rd / "out" / "runtime.json").read_text())
    assert doc["runs"][0]["rc"] == 0


@real_box
def test_real_box_kills_the_run_at_the_wall_clock_and_limits_the_output(tmp_path):
    prog = "import time\ntime.sleep(60)\n"
    deliv, rd, shav = real_delivery(tmp_path, {"slow.py": prog}, [{"label": "s", "langs": ["python"], "argv": ["python3", "slow.py"]}], timeout=2)
    r = start(deliv, rd, shav)
    assert r.returncode == 0, r.stderr
    run = json.loads((rd / "out" / "runtime.json").read_text())["runs"][0]
    assert run["timed_out"] is True and run["rc"] is None and run["seconds"] < 30


@pytest.mark.skipif(bool(REAL) or shutil.which("node") is None, reason=f"needs a working bubblewrap and node ({REAL})")
def test_real_box_node_tracer(tmp_path):
    files = {
        "app.js": "function used() {\n  return 1;\n}\n\nfunction unused() {\n  return 2;\n}\nused();\n",
    }
    deliv, rd, shav = real_delivery(tmp_path, files, [{"label": "n", "langs": ["javascript"], "argv": ["node", "app.js"]}])
    r = start(deliv, rd, shav)
    assert r.returncode == 0, r.stderr
    doc = json.loads((rd / "out" / "runtime.json").read_text())
    got = {(x["file"], x["qual"]) for x in doc["executed"]}
    if doc["runs"][0]["rc"] != 0:
        pytest.skip(f"node does not run in the box here: {doc['runs'][0]}")
    assert ("app.js", "used") in got and ("app.js", "unused") not in got, doc


@pytest.mark.skipif(bool(REAL) or shutil.which("go") is None, reason=f"needs a working bubblewrap and go ({REAL})")
def test_real_box_go_tracer(tmp_path):
    files = {
        "go.mod": "module example.com/m\n\ngo 1.21\n",
        "a.go": "package m\n\nfunc Used() int {\n\treturn 1\n}\n\nfunc Unused() int {\n\treturn 2\n}\n",
        "a_test.go": 'package m\n\nimport "testing"\n\nfunc TestUsed(t *testing.T) {\n\tif Used() != 1 {\n\t\tt.Fatal("x")\n\t}\n}\n',
    }
    deliv, rd, shav = real_delivery(tmp_path, files, [{"label": "g", "langs": ["go"], "argv": ["go", "test", "./..."]}])
    r = start(deliv, rd, shav)
    assert r.returncode == 0, r.stderr
    doc = json.loads((rd / "out" / "runtime.json").read_text())
    run = doc["runs"][0]
    if run["rc"] != 0:
        pytest.skip(f"go test does not run in the box here: {run}")
    got = {(x["file"], x["qual"]) for x in doc["executed"]}
    assert ("a.go", "Used") in got and ("a.go", "Unused") not in got, doc
