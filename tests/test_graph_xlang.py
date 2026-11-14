"""The cross-language edges: exec of a script or bin command, path strings, FFI (ctypes/cffi, cgo, JNI, DllImport, Rust extern "C"),
environment variables written in one language and read in another.

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_xlang.py

Facts come from the real extractors (python by ast; the others by tree-sitter queries data/queries/<grammar>/xlang.scm on the real grammars,
which skip with a reason when a grammar is missing), the edges from graph.link, the verdicts from graph.reach. Fixtures are inline texts of mini repos.
"""

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test

ROOT = Path(__file__).resolve().parent.parent
from dead_code_checker.graph import contracts, langs as dclangs, link as dlink, model as md, reach as rc, ts as tsm, xlink  # noqa: E402
from dead_code_checker.graph.extract import pwsh as pw, ts_generic as tg, xlang  # noqa: E402
from dead_code_checker.graph.model import Facts, RootHint  # noqa: E402

QDIR = Path(tg.QUERY_DIR)


@pytest.fixture(autouse=True)
def registered():
    import importlib

    if not contracts._EXTRACTORS.get("go"):
        importlib.reload(tg)
    yield
    tg.set_backend(None)
    drop_pwsh_worker()


def drop_pwsh_worker():
    """A real pwsh worker cached in pw._local.w (a test that parsed a powershell file) outlives this module and answers a later test
    (tests/test_graph_lang_native.py) that expects its own worker seam: close and forget it."""
    w = getattr(pw._local, "w", None)
    if w is not None:
        pw._local.w = None
        w.close()


def have(grammar):
    tsm.reset()
    try:
        tsm.get_parser(grammar)
    except LookupError as e:
        return str(e)
    return ""


def need(*grammars):
    tg.set_backend(None)
    for g in grammars:
        why = have(g)
        if why:
            pytest.skip(f"tree-sitter grammar {g} is not usable on this host ({why})")


def extract(path, text):
    lang = dclangs.detect(path, text[:4096])
    try:
        return contracts.get_extractor(lang).extract(lang, path, text)
    except contracts.ExtractorMissing as exc:  # the test needs a grammar that this host does not have
        pytest.skip(str(exc))


def graph_files(files):
    """The files that have an extractor (a text or markdown file is no node of the graph), roots as `path#qual` or `path`."""
    out = {}
    for p, t in files.items():
        lang = dclangs.detect(p, t[:4096])
        if lang and dclangs.load().extractor(lang):
            out[p] = extract(p, t)
    return out


def xrefs(path, text, kinds=("exec", "lit", "ffi", "env")):
    f = extract(path, text)
    return {(r.kind, r.name, r.scope) for r in f.refs if r.kind in kinds}


def build(files, roots=()):
    """Link a mini repo ({path: text}); roots: paths that are a hard main root (`self:`)."""
    facts = graph_files(files)
    hints = {}
    for r in roots:
        path, _, qual = r.partition("#")
        if qual:  # a root on a definition (the code facts carry it)
            facts[path] = facts[path]._replace(roots=(*facts[path].roots, RootHint("main", qual, 1)))
        else:
            hints[path] = [RootHint("main", "self:", 1)]
    res = dlink.link(facts, hints, workers=1)
    return res, rc.analyze(res.graph)


def edges(res, kind=None):
    return {
        (e.src.split(":", 1)[1], e.dst.split(":", 1)[1], e.kind, e.tier)
        for e in res.graph.edges
        if kind in (None, e.kind)
    }


# ====================================================================== data and vocabulary
def test_data_is_valid_and_the_kinds_are_edge_kinds():
    d = xlang.data()
    assert {"lit", "ffi", "env", "schema"} <= set(md.EDGE_KINDS) and set(d["ffi_groups"]) <= {
        "cpp",
        "rust",
        "go",
        "csharp",
    }
    for fam, f in d["families"].items():
        assert fam in {s.get("family") for s in dclangs.load().langs.values()}, fam
        r = xlang.rules(fam)
        assert {c.role for c in r.calls if not c.role.startswith("schema.")} <= {
            "exec",
            "env.r",
            "env.w",
            "cfg.r",
            "cfg.w",
        }
        assert {c.role[7:] for c in r.calls if c.role.startswith("schema.")} <= set(d["schema"]["groups"])
        assert all(c.join in ("argv", "line") for c in r.calls)
    assert re.compile(d["lit"]["re"]) and re.compile(d["lit"]["ext"]) and re.compile(d["name"])
    assert len(xlang.sha()) == 64


VOCAB = re.compile(r"@(cmd|lit|call|args|env\.r|env\.w|name|obj|ffi\.decl|ffi\.use)\b")


@pytest.mark.parametrize("g", ["bash", "go", "javascript", "typescript", "java", "csharp", "rust", "c", "cpp"])
def test_xlang_queries_use_the_documented_vocabulary(g):
    text = (QDIR / g / "xlang.scm").read_text()
    code = "\n".join(re.sub(r";.*$", "", ln) for ln in text.splitlines())
    assert code.count("(") == code.count(")") and code.count("[") == code.count("]")
    caps = re.findall(r"@[\w.]+", code)
    assert caps and all(VOCAB.fullmatch(c) for c in caps), set(caps)
    assert not re.search(r"#(eq|match|any-of|not-eq)\?", code), "predicates are binding dependent"
    assert "xlang" in tg._queries(g)


def test_the_shard_key_follows_the_rules(monkeypatch):
    base = contracts.get_extractor("python").grammar_sha("python")
    tsb = contracts.get_extractor("go").grammar_sha("go")
    xlang.sha.cache_clear()
    monkeypatch.setattr(xlang, "data", lambda: {**json.loads((QDIR.parent / "xlang.json").read_text()), "name": "^x$"})
    try:
        assert contracts.get_extractor("python").grammar_sha("python") != base
        assert contracts.get_extractor("go").grammar_sha("go") != tsb
    finally:
        xlang.sha.cache_clear()


# ====================================================================== the helpers
def test_literals_and_path_strings():
    assert xlang.literal('"a/b"') == "a/b" and xlang.literal("'x'") == "x" and xlang.literal("`t`") == "t"
    assert (
        xlang.literal('@"C:\\a\\b"') == "C:\\a\\b"
        and xlang.literal('"a\\"b"') == 'a"b'
        and xlang.literal("word") is None
    )
    ok = ["bin/tool", "./scripts/x.sh", "../lib/a.py", "setup.py", "a/b/", "x-y/z_w.json"]
    bad = ["", "ab", "os", "a b/c.py", "http://x/y.py", "${HOME}/x", "bin tool", "x" * 300, "-x.py", "/abs/p"]
    assert all(xlang.path_like(s) for s in ok), [s for s in ok if not xlang.path_like(s)]
    assert not any(xlang.path_like(s) for s in bad), [s for s in bad if xlang.path_like(s)]
    assert xlang.path_like("lib\\a.txt") == "lib/a.txt"


@pytest.mark.parametrize(
    "args,skip,want",
    [
        ('("bin/tool", "x")', 0, ["bin/tool", "x"]),
        ('(["bash", "s.sh"], check=True)', 0, ["bash", "s.sh"]),
        ('(ctx, "a", "b")', 1, ["a", "b"]),
        ('(new String[]{"bash", "-c", "x y"})', 0, ["bash", "-c", "x y"]),
        ("(Arrays.asList(\"a\", 'b'))", 0, ["a", "b"]),
        ('("bin/" + name, "x")', 0, []),
        ('("a", name, "c")', 0, ["a"]),
        ('(cmd, "a")', 0, []),
        ("(`./s.sh ${x} y`)", 0, []),
        ('("a b")', 0, ["a b"]),
    ],
)
def test_lead_reads_the_leading_literals_only(args, skip, want):
    assert xlang.lead(args, skip) == want


def test_lead_substitutes_a_template_for_a_command_line():
    assert xlang.lead("(`./scripts/run.sh ${x} y`)", 0, True) == ["./scripts/run.sh 0 y"]


def test_exec_names_read_interpreters_launchers_and_builtins_like_the_roots_do():
    n = xlang.exec_names
    assert n(["python3", "-u", "scripts/x.py", "a"]) == ["scripts/x.py"]
    assert n(["bin/tool", "--flag"]) == ["bin/tool"] and n(["tool"]) == ["tool"] and n(["echo", "hi"]) == []
    assert n(["bash", "-c", "bin/z && other"]) == ["bin/z", "other"]
    assert n(["./s.sh a b"]) == ["s.sh"] and n(["tool a", "b"], "line") == ["tool"] and n([]) == []
    assert n(["node", "-e", "1"]) == []  # inline code names no file
    assert n(["$HERE/gen.py a"], "line") == ["gen.py"] and n(['"${ROOT}"/bin/x y'], "line") == [
        "bin/x"
    ]  # a variable directory
    assert (
        n(["/usr/bin/git"]) == [] and n(["$cmd a"], "line") == [] and n(["~/bin/x"]) == []
    )  # outside the tree or computed


def test_jni_names_are_mangled():
    assert xlang.jni_name("com.acme", "Calc", "add") == "Java_com_acme_Calc_add"
    assert xlang.jni_name("", "A", "init_x") == "Java_A_init_1x"
    assert xlang.jni_name("p", "Outer.Inner", "m") == "Java_p_Outer_00024Inner_m"


# ====================================================================== python (ast): no grammar needed
PY = """import os, subprocess, ctypes
from cffi import FFI
lib = ctypes.CDLL("./build/libx.so")
other = FFI().dlopen("y")
class K:
    def __init__(self):
        self.h = ctypes.cdll.LoadLibrary("z")
    def go(self):
        return self.h.zed(1)
def main():
    subprocess.run(["bash", "bin/deploy.sh", "x"], check=True)
    subprocess.check_output("scripts/gen.sh a b", shell=True)
    os.system("bin/tool --x")
    os.spawnv(os.P_WAIT, "bin/sp", ["sp"])
    lib.add(1, 2)
    ctypes.CDLL("w").inline(3)
    other.mul(4)
    os.environ["OUT_DIR"] = "x"
    os.environ.setdefault("MODE", "a")
    v = os.environ["IN_DIR"] + os.getenv("HOME") + os.environ.get("USER")
    p = "conf/app.yaml"
    q = "plain text here"
    return v, p, q
"""


def test_python_facts():
    got = xrefs("m.py", PY)
    assert {
        ("exec", "bin/deploy.sh", "main"),
        ("exec", "scripts/gen.sh", "main"),
        ("exec", "bin/tool", "main"),
        ("exec", "bin/sp", "main"),
    } <= got
    assert {("ffi", "add", "main"), ("ffi", "inline", "main"), ("ffi", "mul", "main"), ("ffi", "zed", "K.go")} <= got
    assert {
        ("env", "w:OUT_DIR", "main"),
        ("env", "w:MODE", "main"),
        ("env", "r:IN_DIR", "main"),
        ("env", "r:HOME", "main"),
    } <= got
    assert ("env", "r:USER", "main") in got and ("lit", "conf/app.yaml", "main") in got
    assert ("lit", "./build/libx.so", "") in got
    assert not any(k == "lit" and n in ("bin/deploy.sh", "bin/tool") for k, n, _ in got)  # an exec call names it once
    assert not any(n in ("plain text here", "x") for _, n, _ in got)


def test_python_calls_that_are_not_commands_add_nothing():
    got = xrefs(
        "m.py",
        "import re\ndef f(run, call):\n    run(['a/b.sh'])\n    call('c.sh')\n    re.run('d.sh')\n    x.Popen(['e.sh'])\n",
    )
    assert not [r for r in got if r[0] == "exec"]


def test_python_f_strings_and_computed_arguments():
    got = xrefs(
        "m.py",
        'import subprocess\ndef f(n):\n    subprocess.run(["bin/" + n])\n    subprocess.run([n, "bin/b"])\n    subprocess.run(f"bin/{n}")\n',
    )
    assert not [r for r in got if r[0] == "exec"]


# ====================================================================== the other languages (real grammars)
def test_shell_facts():
    need("bash")
    got = xrefs(
        "a.sh",
        '#!/bin/bash\nexport FOO=1\nBAR=2 bin/tool "a b" ./s.sh\npython3 -u scripts/x.py\ncat lib/data.json\n'
        "echo $BAZ ${QUX} $lower\nf() { tool z; ls -l; }\nnode server.js\n",
    )
    assert {
        ("exec", "bin/tool", ""),
        ("exec", "scripts/x.py", ""),
        ("exec", "tool", "f"),
        ("exec", "server.js", ""),
    } <= got
    assert {("env", "w:FOO", ""), ("env", "w:BAR", ""), ("env", "r:BAZ", ""), ("env", "r:QUX", "")} <= got
    assert ("lit", "./s.sh", "") in got and ("lit", "lib/data.json", "") in got
    assert not [
        r for r in got if r[1] in ("ls", "r:lower")
    ]  # a builtin is no file, a lower-case variable is no environment


def test_go_facts():
    need("go")
    got = xrefs(
        "a.go",
        'package main\nimport "C"\nimport "os/exec"\nfunc f() {\n C.add(1)\n exec.Command("bin/tool", "x")\n'
        ' exec.CommandContext(ctx, "bash", "scripts/y.sh")\n os.Setenv("A", "b")\n os.Getenv("R")\n s := `raw/x.go`\n}\n',
    )
    assert {
        ("ffi", "add", "f"),
        ("exec", "bin/tool", "f"),
        ("exec", "scripts/y.sh", "f"),
        ("env", "w:A", "f"),
        ("env", "r:R", "f"),
    } <= got
    assert ("lit", "raw/x.go", "f") in got
    assert ("ffi", "add", "f") not in xrefs(
        "b.go", "package main\nfunc f() { C.add(1) }\n"
    )  # no import "C": a Go value named C


def test_js_facts():
    need("javascript", "typescript")
    src = (
        'const cp = require("child_process");\nfunction f() {\n cp.spawn("bin/tool", ["a"]);\n cp.exec(`./scripts/z.sh ${x}`);\n'
        ' process.env.FOO = "1";\n const a = process.env.BAR, b = process.env["Z"];\n return "./w.js";\n}\n'
    )
    for p in ("a.js", "a.ts"):
        got = xrefs(p, src)
        assert {
            ("exec", "bin/tool", "f"),
            ("exec", "scripts/z.sh", "f"),
            ("env", "r:BAR", "f"),
            ("env", "r:Z", "f"),
        } <= got
        assert ("env", "w:FOO", "f") in got and ("env", "r:FOO", "f") not in got and ("lit", "./w.js", "f") in got
    assert not [r for r in xrefs("a.js", "function f(o) { return o.env.X + other.env.Y; }") if r[0] == "env"]


def test_java_facts():
    need("java")
    got = xrefs(
        "src/Calc.java",
        "package com.acme;\nclass Calc {\n native int add(int a, int b);\n public static native void init_x();\n int plain() { return 1; }\n"
        ' void run() throws Exception {\n new ProcessBuilder("bin/tool", "x").start();\n Runtime.getRuntime().exec("bin/other a");\n'
        '  String v = System.getenv("HOME"); System.setProperty("k.v", "1"); System.getProperty("a.b");\n }\n}\n',
    )
    assert {("ffi", "Java_com_acme_Calc_add", "Calc.add"), ("ffi", "Java_com_acme_Calc_init_1x", "Calc.init_x")} <= got
    assert {("exec", "bin/tool", "Calc.run"), ("exec", "bin/other", "Calc.run"), ("env", "r:HOME", "Calc.run")} <= got
    assert {("env", "w:cfg:k.v", "Calc.run"), ("env", "r:cfg:a.b", "Calc.run")} <= got
    assert not [r for r in got if r[0] == "ffi" and "plain" in r[1]]


def test_csharp_facts():
    need("csharp")
    got = xrefs(
        "N.cs",
        'class N {\n [DllImport("native", EntryPoint = "add")] static extern int Add(int a, int b);\n [DllImport("native")] static extern int Mul(int a);\n'
        ' [LibraryImport("native")] static partial int Sub(int a);\n int Plain() { return 1; }\n'
        ' [return: MarshalAs(UnmanagedType.Bool)] [DllImport("native")] static extern bool Flag();\n'
        ' void Run() { Process.Start("bin/tool", "a"); var v = Environment.GetEnvironmentVariable("X"); '
        'Environment.SetEnvironmentVariable("Y", "1"); }\n}\n',
    )
    assert {("ffi", "add", "N.Add"), ("ffi", "Mul", "N.Mul"), ("ffi", "Sub", "N.Sub"), ("ffi", "Flag", "N.Flag")} <= got
    assert {("exec", "bin/tool", "N.Run"), ("env", "r:X", "N.Run"), ("env", "w:Y", "N.Run")} <= got
    assert not [r for r in got if r[0] == "ffi" and r[2] == "N.Plain"]


def test_rust_and_c_facts():
    need("rust", "c")
    got = xrefs(
        "a.rs",
        'extern "C" { fn c_add(a: i32, b: i32) -> i32; }\nfn f() {\n std::process::Command::new("bin/tool").arg("x");\n'
        ' let v = std::env::var("HOME"); env::set_var("A", "b");\n}\ntrait T { fn m(&self); }\n',
    )
    assert {("ffi", "c_add", "c_add"), ("exec", "bin/tool", "f"), ("env", "r:HOME", "f"), ("env", "w:A", "f")} <= got
    assert not [r for r in got if r[0] == "ffi" and r[1] == "m"]  # a trait method is no native symbol
    got = xrefs(
        "a.c",
        'int add(int a) { system("bin/tool x"); getenv("HOME"); setenv("A", "1", 1); putenv("B=2"); return a; }\n',
    )
    assert {("exec", "bin/tool", "add"), ("env", "r:HOME", "add"), ("env", "w:A", "add"), ("env", "w:B", "add")} <= got
    need("cpp")
    assert {("exec", "bin/tool", "f"), ("env", "r:HOME", "f")} <= xrefs(
        "a.cpp", 'int f() { system("bin/tool x"); getenv("HOME"); return 0; }\n'
    )


def test_powershell_env_facts():
    refs = xlang.pwsh_refs(
        '$env:FOO = "x"\n# $env:SKIP = 1\nfunction f { $v = $env:BAR; [Environment]::SetEnvironmentVariable("BAZ", "1") }\n',
        lambda p: "f" if p[0] == 3 else "",
    )
    assert {(r.name, r.scope) for r in refs} == {("w:FOO", ""), ("r:BAR", "f"), ("w:BAZ", "f")}


# ====================================================================== linking: the six fixtures
def has(res, src, dst, kind, tier):
    return (src, dst, kind, tier) in edges(res)


def test_python_runs_a_bash_script():
    res, ra = build(
        {
            "tools/run.py": "import subprocess\ndef main():\n    subprocess.run(['bash', 'bin/deploy.sh', 'x'])\nif __name__ == '__main__':\n    main()\n",
            "bin/deploy.sh": "#!/bin/sh\necho deploy\n",
            "bin/unused.sh": "#!/bin/sh\nunused() { :; }\n",  # a script with a statement is a soft entry of its own: this one only defines
        },
        roots=["tools/run.py"],
    )
    assert has(res, "tools/run.py#main", "bin/deploy.sh", "exec", "T")
    assert ra.verdict("shell:bin/deploy.sh") == "alive" and ra.verdict("shell:bin/unused.sh") == "dead"
    why = ra.why_alive("shell:bin/deploy.sh").render()
    assert "exec/T at tools/run.py:3" in why


def test_go_runs_a_python_script():
    need("go")
    res, ra = build(
        {
            "cmd/main.go": 'package main\nimport "os/exec"\nfunc main() {\n exec.Command("python3", "scripts/gen.py", "a").Run()\n}\n',
            "scripts/gen.py": "def gen():\n    return 1\n",
            "scripts/other.py": "def other():\n    return 2\n",
        },
        roots=["cmd/main.go"],
    )
    assert has(res, "cmd/main.go#main", "scripts/gen.py", "exec", "T")
    assert ra.verdict("python:scripts/gen.py") == "alive" and ra.verdict("python:scripts/other.py") == "dead"


def test_node_runs_a_shell_script_and_reads_its_variable():
    need("javascript", "bash")
    res, ra = build(
        {
            "tools/build.js": 'const { execSync } = require("child_process");\nfunction build() {\n execSync("./scripts/pack.sh --fast");\n return process.env.PACK_OUT;\n}\nbuild();\n',
            "scripts/pack.sh": "#!/bin/sh\nexport PACK_OUT=dist\nmake_it() { :; }\n",
        },
        roots=["tools/build.js"],
    )
    assert has(res, "tools/build.js#build", "scripts/pack.sh", "exec", "T")
    assert has(res, "tools/build.js#build", "scripts/pack.sh", "env", "N")
    assert ra.verdict("shell:scripts/pack.sh") == "alive"


def test_java_native_method_reaches_the_jni_c_function():
    need("java", "c")
    res, ra = build(
        {
            "src/Calc.java": "package com.acme;\npublic class Calc {\n public native int add(int a, int b);\n public native void ghost();\n}\n",
            "native/calc.c": "#include <jni.h>\nint Java_com_acme_Calc_add(void *e, void *o, int a, int b) { return a + b; }\nstatic int helper_dead(void) { return 0; }\n",
        },
        roots=["src/Calc.java#Calc.add"],
    )
    assert has(res, "src/Calc.java#Calc.add", "native/calc.c#Java_com_acme_Calc_add", "ffi", "T")
    assert not [e for e in edges(res, "ffi") if e[0].endswith("Calc.ghost")]  # no C symbol of that name
    assert (
        ra.verdict("c:native/calc.c#Java_com_acme_Calc_add") == "alive"
    )  # (a Java_ function is a root of the C data too)
    assert ra.verdict("c:native/calc.c#helper_dead") == "dead"


def test_jni_overloads_are_reached_with_n():
    need("java", "c")
    res, _ = build(
        {
            "A.java": "package p;\nclass A { native int f(int x); }\n",
            "a.c": "int Java_p_A_f__I(void *e, void *o, int x) { return x; }\nint Java_p_A_f__J(void *e, void *o, long x) { return 0; }\n",
        }
    )
    assert {(e[1], e[3]) for e in edges(res, "ffi")} == {("a.c#Java_p_A_f__I", "N"), ("a.c#Java_p_A_f__J", "N")}


def test_csharp_dllimport_reaches_the_c_function():
    need("csharp", "c")
    res, ra = build(
        {
            "App/Native.cs": 'class Native {\n [DllImport("calc", EntryPoint = "calc_add")] public static extern int Add(int a, int b);\n [DllImport("calc")] public static extern int calc_mul(int a);\n}\n',
            "native/calc.c": "int calc_add(int a, int b) { return a + b; }\nint calc_mul(int a) { return a; }\nint calc_dead(void) { return 0; }\n",
        },
        roots=["App/Native.cs#Native.Add", "App/Native.cs#Native.calc_mul"],
    )
    assert has(res, "App/Native.cs#Native.Add", "native/calc.c#calc_add", "ffi", "T")
    assert has(res, "App/Native.cs#Native.calc_mul", "native/calc.c#calc_mul", "ffi", "T")
    assert ra.verdict("c:native/calc.c#calc_dead") == "dead"
    assert "ffi/T at App/Native.cs:2" in ra.why_alive("c:native/calc.c#calc_add").render()


def test_python_ctypes_reaches_the_rust_extern_c_function():
    need("rust")
    res, ra = build(
        {
            "rs/src/lib.rs": '#[no_mangle]\npub extern "C" fn add(a: i32, b: i32) -> i32 { a + b }\npub extern "C" fn unused_sym(a: i32) -> i32 { a }\n',
            "py/use.py": "import ctypes\nlib = ctypes.CDLL('./rs/target/release/libx.so')\ndef main():\n    return lib.add(1, 2)\nif __name__ == '__main__':\n    main()\n",
        },
        roots=["py/use.py"],
    )
    assert has(res, "py/use.py#main", "rs/src/lib.rs#add", "ffi", "T")
    assert ra.verdict("rust:rs/src/lib.rs#add") == "alive"
    assert not [e for e in edges(res, "ffi") if e[1].endswith("unused_sym")]


def test_rust_extern_block_reaches_the_c_function_and_cgo_too():
    need("rust", "c", "go")
    res, _ = build(
        {
            "r/a.rs": 'extern "C" {\n fn c_add(a: i32) -> i32;\n}\n',
            "g/m.go": 'package m\n/*\nint go_add(int a);\n*/\nimport "C"\nfunc F() int { return int(C.go_add(1)) }\n',
            "c/x.c": "int c_add(int a) { return a; }\nint go_add(int a) { return a; }\n",
        }
    )
    assert has(res, "r/a.rs#c_add", "c/x.c#c_add", "ffi", "T")
    assert any(e[0] == "g/m.go#F" and e[1].endswith("go_add") and e[2] == "ffi" for e in edges(res))


def test_an_ambiguous_symbol_is_n_and_a_symbol_of_the_same_language_adds_nothing():
    need("rust", "c")
    res, _ = build(
        {
            "m.py": "import ctypes\nlib = ctypes.CDLL('x')\ndef f():\n    return lib.dup(1) + lib.local(2)\n",
            "a.rs": '#[no_mangle]\npub extern "C" fn dup() {}\n',
            "b.c": "int dup(void) { return 1; }\n",
            "n.py": "def local():\n    return 1\n",
        }
    )
    assert {(e[1], e[3]) for e in edges(res, "ffi")} == {("a.rs#dup", "N"), ("b.c#dup", "N")}


# ====================================================================== environment variables and path strings
def test_env_is_an_n_edge_from_the_reader_to_the_writer_in_another_language():
    need("bash")
    res, ra = build(
        {
            "run.sh": "#!/bin/sh\nexport LOGDIR=/tmp/x\nsetup() { export CACHE=1; }\n",
            "app.py": "import os\ndef main():\n    return os.environ['LOGDIR'], os.getenv('CACHE')\nif __name__ == '__main__':\n    main()\n",
        },
        roots=["app.py"],
    )
    assert has(res, "app.py#main", "run.sh", "env", "N") and has(res, "app.py#main", "run.sh#setup", "env", "N")
    assert (
        ra.verdict("shell:run.sh") == "weak-alive" and ra.verdict("shell:run.sh#setup") == "weak-alive"
    )  # never strong
    assert "env/N at app.py:3" in ra.why_alive("shell:run.sh#setup").render()


def test_env_within_one_language_and_a_write_without_a_reader_add_nothing():
    res, ra = build(
        {
            "a.py": "import os\ndef w():\n    os.environ['X'] = '1'\n",
            "b.py": "import os\ndef r():\n    return os.environ['X']\nif __name__ == '__main__':\n    r()\n",
            "c.py": "import os\ndef w2():\n    os.environ['LONELY'] = '1'\n",
        },
        roots=["b.py"],
    )
    assert not edges(res, "env") and ra.verdict("python:a.py#w") == "dead" and ra.verdict("python:c.py#w2") == "dead"


def test_an_ambient_or_over_wide_variable_links_nothing():
    need("bash")
    reader = "import os\ndef main():\n    return os.environ['%s']\nif __name__ == '__main__':\n    main()\n"
    for name, writers in (
        ("HOME", 1),
        ("MY_VAR", 9),
    ):  # HOME: every process has it; MY_VAR: more writers than env_max_writers
        files = {"app.py": reader % name, **{f"w{i}.sh": f"export {name}=1\n" for i in range(writers)}}
        res, _ = build(files, roots=["app.py"])
        assert not edges(res, "env"), name
    res, _ = build(
        {"app.py": reader % "MY_VAR", **{f"w{i}.sh": "export MY_VAR=1\n" for i in range(8)}}, roots=["app.py"]
    )
    assert len(edges(res, "env")) == 8  # at the limit it still links


def test_a_config_key_written_in_java_is_read_in_go():
    need("java", "go")
    res, _ = build(
        {
            "J.java": 'class J { void w() { System.setProperty("app.mode", "x"); } }\n',
            "m.go": 'package main\nfunc f() { viper.GetString("app.mode") }\nfunc g() { os.Getenv("app.mode") }\n',
        }
    )
    assert edges(res, "env") == {
        ("m.go#f", "J.java#J.w", "env", "N")
    }  # a configuration key and an environment variable never meet


def test_a_path_string_is_an_n_edge_to_the_file_it_names_exactly():
    res, ra = build(
        {
            "app.py": "def main():\n    return 'conf/app.yaml', 'missing/x.yaml', 'app.py', 'tools/helper.py'\nif __name__ == '__main__':\n    main()\n",
            "conf/app.yaml": "k: 1\n",
            "other/conf/app.yaml": "k: 2\n",
            "tools/helper.py": "def h():\n    return 1\n",
        },
        roots=["app.py"],
    )
    assert edges(res, "lit") == {
        ("app.py#main", "conf/app.yaml", "lit", "N"),
        ("app.py#main", "tools/helper.py", "lit", "N"),
    }
    assert ra.verdict("yaml:conf/app.yaml") in (
        "weak-alive",
        "unknown",
        "dead",
    )  # reach classes N as weak; a yaml file may be a cfg node


def test_a_path_string_relative_to_the_file_and_never_to_itself():
    res, _ = build({"d/a.py": "X = 'b.py'\nY = 'd/a.py'\nZ = 'a.py'\n", "d/b.py": "pass\n", "b.py": "pass\n"})
    assert {(e[1], e[3]) for e in edges(res, "lit")} == {("d/b.py", "N"), ("b.py", "N")}


def test_exec_names_a_file_by_a_bare_command_word():
    files = {
        "run.py": "import subprocess\ndef main():\n    subprocess.run("
        + "['mytool', '--x'])\nif __name__ == '__main__':\n    main()\n",  # split: the bootstrap test greps the sources for such a call
        "bin/mytool": "#!/bin/sh\necho hi\n",
    }
    res, ra = build(files, roots=["run.py"])
    assert (
        edges(res, "exec") == {("run.py#main", "bin/mytool", "exec", "T")} and ra.verdict("shell:bin/mytool") == "alive"
    )
    res, _ = build(
        {**files, "tools/mytool": "#!/bin/sh\necho other\n"}, roots=["run.py"]
    )  # a file under bin/ is preferred
    assert edges(res, "exec") == {("run.py#main", "bin/mytool", "exec", "T")}
    res, _ = build(
        {**files, "x/bin/mytool": "#!/bin/sh\necho other\n"}, roots=["run.py"]
    )  # two bin/ candidates: a guess
    assert {e[3] for e in edges(res, "exec")} == {"N"} and len(edges(res, "exec")) == 2


def test_powershell_exec_and_env_link_to_other_languages():
    if not (dclangs.load().extractor("powershell") and __import__("shutil").which("pwsh")):
        pytest.skip("pwsh is not installed on this host")
    res, ra = build(
        {
            "tools/go.ps1": "function Start-It {\n  & ./scripts/y.ps1 1\n  Start-Process bin/tool\n  $env:APP_HOME = 'x'\n}\nStart-It\n",
            "scripts/y.ps1": "Write-Host y\n",
            "bin/tool": "#!/bin/sh\necho t\n",
            "app.py": "import os\ndef main():\n    return os.environ['APP_HOME']\nif __name__ == '__main__':\n    main()\n",
        },
        roots=["tools/go.ps1", "app.py"],
    )
    assert has(res, "tools/go.ps1#start-it", "scripts/y.ps1", "exec", "T") and has(
        res, "tools/go.ps1#start-it", "bin/tool", "exec", "T"
    )
    assert has(res, "app.py#main", "tools/go.ps1#start-it", "env", "N")


# ====================================================================== determinism and safety
def test_the_graph_is_the_same_for_any_worker_count():
    files = {
        "a.py": "import subprocess, os\ndef main():\n    subprocess.run(['bin/t'])\n    os.environ['A'] = 'b'\n    return 'conf/x.yaml'\n",
        "bin/t": "#!/bin/sh\necho $A\n",
        "conf/x.yaml": "k: 1\n",
    }
    facts = graph_files(files)
    digests = {dlink.link(facts, workers=w, min_chunk=1).graph.digest() for w in (1, 2, 4)}
    assert len(digests) == 1


def test_a_bug_in_the_cross_language_pass_costs_that_pass_and_nothing_else(monkeypatch):
    need("go")

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(xlang, "ts_refs", boom)
    f = extract("a.go", "package main\nfunc a() { b() }\nfunc b() {}\n")
    assert f.errors == 1 and {d.qual for d in f.defs} >= {"a", "b"} and any(r.name == "b" for r in f.refs)


def test_a_bug_in_the_python_cross_language_pass_costs_that_pass_and_nothing_else(monkeypatch):
    src = "import os, subprocess\n\n\ndef a():\n    subprocess.run(['bin/t'])\n    os.environ['A'] = 'b'\n    return b()\n\n\ndef b():\n    return 1\n"
    good = extract("a.py", src)
    assert good.errors == 0 and any(r.kind == "exec" for r in good.refs)
    for name in ("call", "const", "attribute", "subscript", "finish"):

        def boom(self, *a, **k):
            raise RuntimeError("bug")

        monkeypatch.setattr(xlang.PyScan, name, boom)
        f = extract("a.py", src)
        assert f.errors == 1, name
        assert {d.qual for d in f.defs} >= {"a", "b"} and any(r.name == "b" and r.kind == "call" for r in f.refs), name
        assert not any(r.kind in ("exec", "lit", "ffi", "env") for r in f.refs), name
        monkeypatch.undo()
    monkeypatch.setattr(xlang, "PyScan", lambda tree: (_ for _ in ()).throw(RuntimeError("bug")))  # the constructor too
    f = extract("a.py", src)
    assert f.errors == 1 and {d.qual for d in f.defs} >= {"a", "b"}


def test_a_bug_in_the_powershell_env_pass_costs_that_pass_and_nothing_else(monkeypatch):
    text = "function A {\n  B\n}\nfunction B {}\n"
    rec = [["func", "A", 1, 0, 3, 1, 0, 1], ["func", "B", 4, 0, 4, 12, 0, 0], ["cmd", "B", 2, 2, 2, 3, "", "", []]]
    pw.set_worker(lambda t: {"errors": 0, "rec": rec})
    try:
        ok = pw.Pwsh().extract("powershell", "a.ps1", text)
        assert ok.errors == 0 and {d.qual for d in ok.defs} >= {"a", "b"}

        def boom(*a, **k):
            raise RuntimeError("bug")

        monkeypatch.setattr(xlang, "pwsh_refs", boom)
        f = pw.Pwsh().extract("powershell", "a.ps1", text)
        assert (
            f.errors == 1
            and {d.qual for d in f.defs} >= {"a", "b"}
            and {r.name for r in f.refs} == {r.name for r in ok.refs}
        )
    finally:
        pw.set_worker(None)


def test_a_cached_pwsh_worker_is_closed_and_forgotten_by_the_module_finalizer():
    class W:
        closed = False

        def close(self):
            W.closed = True

    pw._local.w = W()
    drop_pwsh_worker()
    assert W.closed and getattr(pw._local, "w", None) is None
    drop_pwsh_worker()  # nothing cached: nothing to do


def test_a_query_that_does_not_compile_stays_loud(monkeypatch):
    need("go")
    real = tg._query_text
    monkeypatch.setattr(tg, "_query_text", lambda g, k: "(no_such_node) @lit" if k == "xlang" else real(g, k))
    getattr(tg._local, "queries", {}).clear()
    try:
        with pytest.raises(contracts.ExtractorMissing, match="does not compile"):
            extract("a.go", "package main\nfunc a() {}\n")
    finally:
        getattr(tg._local, "queries", {}).clear()


def test_facts_of_other_kinds_survive_a_roundtrip_with_the_new_edge_kinds():
    f = extract("m.py", PY)
    assert Facts.from_payload(f.to_payload()).to_payload() == f.to_payload()
    assert {"exec", "lit", "ffi", "env"} <= {r.kind for r in f.refs}


def test_xlink_kinds_match_the_model():
    assert set(xlink.KINDS) <= set(md.EDGE_KINDS)
