"""graph.rootsrc: the root sources of ordinary projects (packaging, build and container files).

python3 -m pytest -q tests/test_graph_rootsrc.py

The unit tests read the fixtures of tests/fixtures/rootsrc (one file per source, bad/ = the malformed ones); the language
extractors that make a Cargo target or a Gradle main class alive in the graph arrive with the language packs. Nothing is executed.
"""

import importlib
import json
from pathlib import Path

import pytest
from graph_support import new_repo

from dead_code_checker.graph import contracts, model, roots, rootsrc, store, vcs

FIX = Path(__file__).resolve().parent / "fixtures" / "rootsrc"


BAD = ["pyproject.toml", "setup.cfg", "setup.py", "Cargo.toml", "Server.csproj", "pom.xml", "package.json"]
GARBAGE = [
    "",
    "\x00\x01\x02",
    "{" * 500,
    "[" * 500,
    "<a>" * 400,
    "\ufeff",
    "= = =",
    "\n" * 50,
    "a" * 100_000,
    "\xff\xfe",
]


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def hints(path: str, text: str):
    return sorted((h.kind, h.target, h.strength) for h in roots.extract_hints(path, text))


def want(*rows):
    return sorted(rows)


# ================================================================ Python packaging
def test_pyproject_scripts_entry_points_poetry_and_script_files():
    got = hints("pyproject.toml", fx("pyproject.toml"))
    assert got == want(
        ("bin", "mod:demo.cli#main", "hard"),  # [project.scripts]
        ("bin", "mod:demo.tools.x#run", "hard"),  # `pkg.mod:func [extra]`
        ("bin", "mod:demo.gui", "hard"),  # [project.gui-scripts]: a module, no function
        ("bin", "mod:demo.legacy#main", "hard"),  # entry-points.console_scripts installs a command
        ("export", "mod:demo.plug.alpha#Plugin", "hard"),  # any other group: a host loads the plugin
        ("bin", "mod:demo.poet#cli", "hard"),  # [tool.poetry.scripts]
        ("bin", "sibling:scripts/run.sh", "hard"),  # poetry 2: type = "file"
        ("export", "mod:demo.plug.beta#B", "hard"),  # [tool.poetry.plugins.*]
        ("bin", "sibling:scripts/old.sh", "hard"),  # [tool.setuptools] script-files
    )


def test_every_root_has_a_kind_and_the_line_of_its_source():
    for path, name, needle in (
        ("pyproject.toml", "mod:demo.cli#main", "demo.cli:main"),
        ("pyproject.toml", "mod:demo.plug.beta#B", "demo.plug.beta:B"),
        ("pyproject.toml", "sibling:scripts/old.sh", "scripts/old.sh"),
        ("setup.cfg", "mod:demo.two#run", "demo.two:run"),
        ("setup.cfg", "sibling:scripts/b.sh", "scripts"),
    ):
        text = fx(path)
        by = {h.target: h for h in roots.extract_hints(path, text)}
        assert needle in text.split("\n")[by[name].line - 1], (path, name)
        assert by[name].kind in ("bin", "export")


def test_setup_cfg_entry_points_and_scripts():
    assert hints("setup.cfg", fx("setup.cfg")) == want(
        ("bin", "mod:demo.cli#main", "hard"),
        ("bin", "mod:demo.two#run", "hard"),
        ("export", "mod:demo.plug.alpha#Plugin", "hard"),
        ("bin", "sibling:scripts/a.sh", "hard"),
        ("bin", "sibling:scripts/b.sh", "hard"),
    )


def test_setup_py_literal_is_read_and_never_executed(tmp_path):
    marker = tmp_path / "ran"
    text = f"open({str(marker)!r}, 'w').write('x')\n" + fx("setup.py")
    assert hints("setup.py", text) == want(
        ("bin", "mod:demo.cli#main", "hard"),
        ("export", "mod:demo.plug#Alpha", "hard"),
        ("bin", "sibling:scripts/run.sh", "hard"),
    )
    assert not marker.exists()


def test_setup_py_ini_text_and_non_literal_values():
    ini = (
        'from setuptools import setup\nsetup(entry_points="""[console_scripts]\\na = p.m:f\\n[x.y]\\nb = p.n:g\\n""")\n'
    )
    assert hints("setup.py", ini) == want(("bin", "mod:p.m#f", "hard"), ("export", "mod:p.n#g", "hard"))
    dyn = "from setuptools import setup\nsetup(entry_points=load(), scripts=glob('s/*'))\n"
    got = roots.extract_hints("setup.py", dyn)
    assert [h.kind for h in got] == [rootsrc.BAD, rootsrc.BAD]  # a value that is no literal is W, never evaluated
    assert roots.extract_hints("setup.py", "from setuptools import setup\nsetup(name='x')\n") == []


def test_bad_entry_point_value_is_one_bad_hint_the_rest_is_read():
    got = roots.extract_hints("pyproject.toml", '[project.scripts]\nok = "a.b:c"\nbad = 5\nalso = "not a spec!"\n')
    assert sorted(h.kind for h in got) == [rootsrc.BAD, rootsrc.BAD, "bin"]
    assert {h.target for h in got if h.kind == "bin"} == {"mod:a.b#c"}


# ================================================================ Cargo, .NET, JVM
def test_cargo_manifest_and_conventions():
    got = hints("Cargo.toml", fx("Cargo.toml"))
    for row in (
        ("bin", "sibling:src/main.rs", "hard"),
        ("bin", "sibling:src/bin/*.rs", "hard"),
        ("bin", "sibling:src/bin/*/main.rs", "hard"),
        ("bin", "sibling:src/cli/main.rs", "hard"),  # [[bin]] path
        ("bin", "sibling:src/bin/other.rs", "hard"),  # [[bin]] name: where cargo looks
        ("bin", "sibling:examples/quick.rs", "hard"),
        ("bin", "sibling:examples/*.rs", "hard"),
        ("test", "sibling:benches/speed.rs", "soft"),  # benches are test-like
        ("test", "sibling:benches/*.rs", "soft"),
        ("init", "sibling:tools/build.rs", "hard"),  # package.build
        ("export", "sibling:src/core.rs", "hard"),  # [lib] path of a published crate
    ):
        assert row in got
    assert ("export", "sibling:src/lib.rs", "hard") not in got


def test_cargo_library_root_publish_proc_macro_and_workspace():
    pub = lambda extra: hints("Cargo.toml", f'[package]\nname = "x"\n{extra}')  # noqa: E731
    assert ("export", "sibling:src/lib.rs", "hard") in pub("")
    assert ("export", "sibling:src/lib.rs", "hard") not in pub("publish = false\n")
    assert ("export", "sibling:src/lib.rs", "hard") in pub('publish = false\n[lib]\ncrate-type = ["cdylib"]\n')
    assert ("export", "sibling:src/lib.rs", "hard") in pub("publish = false\n[lib]\nproc-macro = true\n")
    assert ("bin", "sibling:src/main.rs", "hard") not in pub("autobins = false\n")
    assert ("init", "sibling:build.rs", "hard") not in pub("build = false\n")
    assert roots.extract_hints("Cargo.toml", '[workspace]\nmembers = ["a", "b"]\n') == []


def test_csproj_exe_web_sdk_and_test_projects():
    assert hints("app/Server.csproj", fx("Server.csproj")) == want(("bin", "sibling:Program.cs", "hard"))
    assert hints("t/Server.Tests.csproj", fx("Server.Tests.csproj")) == want(("test", "sibling:*.cs", "soft"))
    web = '<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup></PropertyGroup></Project>'
    assert hints("w.csproj", web) == want(("bin", "sibling:Program.cs", "hard"))
    lib = '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Library</OutputType></PropertyGroup></Project>'
    assert hints("l.csproj", lib) == []


def test_gradle_and_maven_main_classes():
    assert hints("build.gradle", fx("build.gradle")) == want(
        ("main", "class:com.example.App", "hard"),
        ("main", "class:com.example.Cli", "hard"),
        ("main", "class:com.example.Boot", "hard"),
    )  # the commented-out mainClass is not a root
    assert hints("app/build.gradle.kts", fx("build.gradle.kts")) == want(
        ("main", "class:com.example.KtApp", "hard"), ("main", "class:com.example.KtCli", "hard")
    )
    assert hints("pom.xml", fx("pom.xml")) == want(
        ("main", "class:com.example.Boot", "hard"), ("main", "class:com.example.Exec", "hard")
    )


# ================================================================ Dockerfile, Makefile, justfile, compose
def test_dockerfile_cmd_entrypoint_run_copy_and_workdir():
    got = {(k, t) for k, t, _ in hints("Dockerfile", fx("Dockerfile"))}
    for kind, rel in (
        ("ci-job", "scripts/build.sh"),  # RUN ./scripts/build.sh after `COPY . .` into WORKDIR
        ("ci-job", "scripts/warm.py"),  # RUN --mount=... python scripts/warm.py (flags and continuation)
        ("ci-job", "scripts/heredoc.sh"),  # RUN <<EOF body
        ("ci-job", "scripts/entry.sh"),  # RUN /usr/local/bin/entry.sh: the COPY that put it there
        ("bin", "scripts/entry.sh"),  # ENTRYPOINT ["/usr/local/bin/entry.sh"]
        ("bin", "app/main.py"),  # CMD [...] relative to WORKDIR /app, COPY app/ ./app/
        ("bin", "app/health.py"),  # HEALTHCHECK ... CMD [...]
    ):
        assert (kind, f"sibling:{rel}") in got and (kind, f"file:{rel}") in got, (kind, rel)
    assert not any("tool" in t for _, t in got)  # COPY --from=build: a file of another stage is no repository path
    assert not any(t.endswith("/usr/local/bin/entry.sh") for _, t in got)  # an image path is never a repository path


def test_dockerfile_shift_and_unterminated_heredoc_do_not_swallow_the_rest():
    text = 'FROM x\nRUN python -c "print(1 << 2)"\nRUN cat <<EOF > /etc/x\nCMD ["./run.sh"]\n'
    assert "file:run.sh" in {
        t for _, t, _ in hints("Dockerfile", text)
    }  # neither `<< 2` nor a missing EOF eats the CMD


def test_dockerfile_copy_is_followed_only_when_a_later_instruction_runs_it():
    text = 'FROM x\nCOPY tools/a.sh /srv/a.sh\nCOPY tools/b.sh /srv/b.sh\nCMD ["/srv/a.sh"]\n'
    got = {t for _, t, _ in hints("Dockerfile", text)}
    assert "file:tools/a.sh" in got and "file:tools/b.sh" not in got


def test_makefile_variables_continuations_shell_calls_and_leading_variables():
    got = {t for k, t, _ in hints("Makefile", fx("Makefile")) if t.startswith("file:")}
    assert got == {
        "file:scripts/gen.py",  # @$(PY) $(SCRIPTS)/gen.py: both variables expanded
        "file:scripts/fmt.sh",  # a backslash continuation
        "file:scripts/version.sh",  # $$(...) in a recipe
        "file:tools/lint.py",  # $(UNKNOWN) tools/lint.py: a script path survives an unknown leading variable
        "file:tools/ver.py",  # VER := $(shell python3 tools/ver.py)
    }  # `$(CP) data/in.txt build/in.txt` names no program; `$(MAKE) -C sub all` is no script


def test_justfile_recipes_variables_and_nested_just():
    got = {t for _, t, _ in hints("justfile", fx("justfile")) if t.startswith("file:")}
    assert got == {"file:scripts/build.py", "file:scripts/post.sh", "file:tools/push.sh"}


def test_compose_command_and_entrypoint_of_services():
    text = fx("docker-compose.yml") + "  worker:\n    image: w\n    entrypoint: ./scripts/worker.sh --x\n"
    got = {t for _, t, _ in hints("deploy/docker-compose.override.yml", text)}
    assert got == {"file:scripts/serve.py", "file:scripts/worker.sh"}  # image paths: from the repository root only
    assert not roots.extract_hints("deploy/values.yml", text)  # only compose files


def test_the_files_themselves_are_no_roots():
    for name, text in (("Dockerfile", fx("Dockerfile")), ("Makefile", fx("Makefile")), ("justfile", fx("justfile"))):
        assert not [h for h in roots.extract_hints(name, text) if h.target == "self:"]


@pytest.mark.parametrize(
    "path, key",
    [
        ("a/Dockerfile", "docker"),
        ("Dockerfile.dev", "docker"),
        ("x.dockerfile", "docker"),
        ("Containerfile", "docker"),
        ("Makefile", "make"),
        ("a/GNUmakefile", "make"),
        ("rules.mk", "make"),
        ("justfile", "just"),
        ("Justfile", "just"),
        ("compose.yaml", "compose"),
        ("docker-compose.prod.yml", "compose"),
        ("sub/pyproject.toml", "pyproject"),
        ("setup.cfg", "setup_cfg"),
        ("setup.py", "setup_py"),
        ("crates/x/Cargo.toml", "cargo"),
        ("src/A.csproj", "csproj"),
        ("build.gradle", "gradle"),
        ("a/b.gradle.kts", "gradle"),
        ("pom.xml", "maven"),
    ],
)
def test_which_files_each_extractor_reads(path, key):
    reads = {rx.name for rx in contracts.root_extractors() if rx.matches(path)}
    assert key.replace("_", "-") in reads


@pytest.mark.parametrize("name", BAD)
def test_a_malformed_manifest_is_one_bad_source_hint(name):
    got = roots.extract_hints(name, fx(f"bad/{name}"))
    assert [h.kind for h in got] == [rootsrc.BAD] and got[0].target and got[0].line >= 1


@pytest.mark.parametrize("name", [*BAD, "Dockerfile", "Makefile", "justfile", "build.gradle", "docker-compose.yml"])
def test_no_extractor_raises_on_garbage(name):
    for text in GARBAGE:
        for h in roots.extract_hints(name, text):
            assert h.kind in {*model.ROOT_KINDS, rootsrc.BAD}


def test_an_extractor_bug_is_a_bad_source_too():
    class Boom(rootsrc._Src):
        name, key = "boom", "pyproject"

        def read(self, text):
            raise RuntimeError("bug")

    got = Boom().extract("pyproject.toml", "x", None)
    assert [h.kind for h in got] == [rootsrc.BAD] and "RuntimeError" in got[0].target


def test_resolve_turns_a_bad_source_into_w_bad_root_source_and_no_root():
    hs = {"svc/pyproject.toml": [rootsrc.bad("TOMLDecodeError: broken", 7)]}
    rs = roots.resolve(hs, {"svc/pyproject.toml": "100644"}, lambda p: None)
    assert not rs.roots
    (f,) = rs.findings
    assert (f.level, f.code, f.path, f.line) == ("W", "dc.bad-root-source", "svc/pyproject.toml", 7)
    assert "pyproject.toml" in f.msg and "TOMLDecodeError" in f.msg and f.why


def test_package_json_is_pinned_here_and_a_broken_one_is_bad():
    pj = json.dumps(
        {
            "main": "lib/a.js",
            "bin": {"x": "./bin/x.js"},
            "exports": {".": "./lib/e.js"},
            "scripts": {"s": "node tools/s.js"},
        }
    )
    got = {(h.kind, h.target) for h in roots.extract_hints("package.json", pj)}
    for row in (
        ("export", "sibling:./lib/a.js"),
        ("bin", "sibling:./bin/x.js"),
        ("export", "sibling:./lib/e.js"),
        ("export", "sibling:./tools/s.js"),
    ):
        assert row in got
    assert [h.kind for h in roots.extract_hints("package.json", "{")] == [rootsrc.BAD]


def test_ext_ver_carries_the_data_and_registration_survives_a_reset(monkeypatch):
    rx = next(r for r in contracts.root_extractors() if r.name == "cargo")
    before, real = rx.ext_ver, rootsrc.data()
    monkeypatch.setattr(rootsrc, "data", lambda: {**real, "cargo": {**real["cargo"], "main": ["x"]}})
    assert rx.ext_ver != before and rx.ext_ver.startswith("cargo@1.")
    monkeypatch.undo()
    monkeypatch.setattr(contracts, "_ROOTS", dict(contracts._ROOTS))  # copies: the reset leaves the real registry alone
    monkeypatch.setattr(contracts, "_EXTRACTORS", dict(contracts._EXTRACTORS))
    contracts.reset_registry()
    assert not contracts.root_extractors()
    importlib.reload(roots)
    assert {"pyproject", "cargo", "docker", "make", "just", "compose"} <= {r.name for r in contracts.root_extractors()}


def test_shards_of_the_new_extractors_roundtrip_and_are_deterministic():
    for name, fixture in (("Cargo.toml", "Cargo.toml"), ("Makefile", "Makefile"), ("Dockerfile", "Dockerfile")):
        rx = [r for r in contracts.root_extractors() if r.matches(name)]
        assert rx
        for r in rx:
            a = contracts.make_root_shard(r, name, "b" * 40, fx(fixture))
            b = contracts.make_root_shard(r, name, "b" * 40, fx(fixture))
            assert a == b and a.facts().roots


def test_exec_entry_rule_is_data_driven():
    d = rootsrc.data()["exec_entry"]
    assert rootsrc.exec_entry("tools/x.py", d["mode"], "#!/usr/bin/env python3\n") == (
        d["kind"],
        d["strength"],
        d["trust"],
    )
    assert rootsrc.exec_entry("tools/x.py", "100644", "#!/bin/sh\n") is None
    assert rootsrc.exec_entry("tools/x.py", d["mode"], "print(1)\n") is None
    assert rootsrc.exec_entry("tools/x.py", d["mode"], "") is None
    assert rootsrc.exec_entry("node_modules/p/x.js", d["mode"], "#!/usr/bin/env node\n") is None


def test_collect_is_parallel_deterministic_and_cached_by_blob(tmp_path):
    files = {
        "pyproject.toml": fx("pyproject.toml"),
        "Cargo.toml": fx("Cargo.toml"),
        "Makefile": fx("Makefile"),
        "justfile": fx("justfile"),
        "Dockerfile": fx("Dockerfile"),
        "pom.xml": fx("pom.xml"),
        "app/build.gradle": fx("build.gradle"),
        "bad/setup.py": fx("bad/setup.py"),
        "web/package.json": fx("bad/package.json"),
        "tests/fixtures/pyproject.toml": fx("pyproject.toml"),  # a fixture registers nothing
    }
    r = new_repo(tmp_path, files)
    tree = vcs.snapshot(str(r))
    one, many = roots.collect(str(r), tree, workers=1), roots.collect(str(r), tree, workers=4)
    assert one == many and "tests/fixtures/pyproject.toml" not in one and {"Makefile", "bad/setup.py"} <= set(one)
    with store.Store(str(tmp_path / "g" / "store.sqlite")) as st:
        cold = roots.collect(str(r), tree, st, workers=2)
        n = st.shard_count()
        assert cold == one and roots.collect(str(r), tree, st, workers=2) == one and st.shard_count() == n
    a, b = roots.build(str(r), workers=1), roots.build(str(r), workers=4)
    assert a.roots == b.roots and [f.as_dict() for f in a.findings] == [f.as_dict() for f in b.findings]
    assert [f.code for f in a.findings].count("dc.bad-root-source") == 2
