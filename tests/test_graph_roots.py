"""graph.roots: command parsing, the root extractors (systemd, cron, task xml, gitlab, actions, docs), resolution against a
tree, root-dangling, parallel and cached collection.

python3 -m pytest -q tests/test_graph_roots.py
"""

import json
import shutil

import pytest
from graph_support import git, new_repo

from dead_code_checker.graph import contracts, model, roots, store, vcs


def targets(path, text, kind=None):
    return sorted({h.target for h in roots.extract_hints(path, text) if kind in (None, h.kind)})


# ================================================================ command lines
@pytest.mark.parametrize(
    "cmd, want",
    [
        ("~/bin/x.py 2>/dev/null", ["file:~/bin/x.py"]),
        ("python3 -I $HOME/bin/h.py", ["file:~/bin/h.py"]),
        ('python "%USERPROFILE%\\bin\\h.py"', ["file:~/bin/h.py"]),
        (r"powershell -NoProfile -File C:\Users\x\bin\h.ps1", ["file:C:/Users/x/bin/h.ps1"]),
        ("pwsh -Command 'Get-Date'", []),
        ("python3 -c 'import os'", []),
        ("python3 -m pkg.mod --flag", ["mod:pkg.mod"]),
        ("env FOO=1 nice -n 5 timeout 30 /usr/bin/python3 -u lib/x.py", ["file:lib/x.py"]),
        ("python3 lib/wrapper.py run -- python3 -I bin/audit", ["file:lib/wrapper.py", "file:bin/audit"]),
        ("cd /srv/app && ./bin/run.sh --all; echo done", ["file:bin/run.sh"]),
        ("bash -c 'exec python3 jobs/a.py || true'", ["file:jobs/a.py"]),
        ("bash scripts/b.sh arg", ["file:scripts/b.sh"]),
        ("source lib/env.sh", ["file:lib/env.sh"]),
        ("node --require ./pre.js srv/index.js", ["file:srv/index.js"]),
        (
            "poetry run python tools/gen.py --x",
            ["file:tools/gen.py"],
        ),  # a launcher runs the command after its subcommand
        ("uv run build.py", ["file:build.py"]),
        ("bundle exec ruby gen.rb && pnpm exec node x.js", ["file:gen.rb", "file:x.js"]),
        ("npx tsx src/main.ts; xvfb-run -a python3 ui.py", ["file:src/main.ts", "file:ui.py"]),
        ("deno run -A main.ts; bun run server.ts", ["file:main.ts", "file:server.ts"]),
        ("ts-node -r ./pre.js scripts/seed.ts", ["file:scripts/seed.ts"]),
        ("coverage run tools/cov.py", ["file:tools/cov.py"]),
        (
            "poetry install; yarn build",
            ["cmd:poetry", "cmd:yarn"],
        ),  # no subcommand of a launcher: the tool is the command
        ("mytool --quick >/dev/null 2>&1", ["cmd:mytool"]),
        ("echo hi; test -r /x/y.py", []),
        ("git commit -m x", []),
        ("FOO=bar", []),
        ("", []),
        ("unterminated 'quote bin/z", ["cmd:unterminated"]),
    ],
)
def test_command_targets(cmd, want):
    assert roots.command_targets(cmd) == want


def test_a_shell_wrapper_with_a_guard_names_the_script_it_execs():
    cmd = "/bin/sh -c 'test -r /usr/local/lib/app/guard.py || { echo missing >&2; exit 2; }; exec /usr/bin/python3 -I /usr/local/lib/app/guard.py'"
    assert roots.command_targets(cmd) == ["file:/usr/local/lib/app/guard.py"]


def test_nested_shells_stop_at_depth_3():
    cmd = "x.py"
    for _ in range(6):
        cmd = "sh -c " + json.dumps(cmd)
    assert roots.command_targets(cmd) == []  # never recurse without bound


def test_bare_script_words_are_files_only_when_the_caller_says_so():
    assert roots.command_targets("node server") == ["cmd:server"]
    assert roots.command_targets("node server", bare=True) == ["file:server"]
    assert roots.command_targets("eslint .", bare=True) == ["cmd:eslint"]


@pytest.mark.parametrize(
    "cmd, want",
    [
        ("make test lint", ["recipe:test", "recipe:lint"]),
        ("$(MAKE) -C sub all", ["recipe:all"]),
        ("make -j all", ["recipe:all"]),
        ("make -j 4 all CC=gcc", ["recipe:all"]),
        ("just build", ["recipe:build"]),
        ("make $(SUBS)", ["recipe:*"]),
        ("make a-$(X)", ["recipe:a-*"]),
        ("docker build --target dev .", ["stage:dev"]),
        ("docker build --target=Dev .", ["stage:dev"]),
        ("bash -c 'make x'", ["recipe:x"]),
        ("@make quiet", ["recipe:quiet"]),
        ("echo make test", []),
        ("make", []),
        ("make V=1 -n", []),
        ("", []),
    ],
)
def test_build_targets(cmd, want):
    assert roots.build_targets(cmd) == want


def test_mod_files_names_the_files_a_module_can_be():
    assert roots.mod_files("pkg.main") == ["pkg/main.py", "pkg/main/__main__.py", "pkg/main/__init__.py"]


# ================================================================ extractors
def test_systemd_units_and_timers():
    unit = "[Service]\nExecStart=-/usr/bin/python3 /a/b.py \\\n  --x\nExecStartPre=@/a/pre.sh\nExecStop=/a/stop.sh\n"
    assert targets("u.service", unit) == ["file:/a/b.py", "file:/a/pre.sh", "file:/a/stop.sh"]
    assert {h.kind for h in roots.extract_hints("u.service", unit)} == {"systemd"}
    assert [h.line for h in roots.extract_hints("u.service", unit) if h.target == "file:/a/b.py"] == [2]
    assert [h.target for h in roots.extract_hints("t.timer", "[Timer]\nUnit=job.service\n")] == ["sibling:job.service"]
    assert [h.target for h in roots.extract_hints("t.timer", "[Timer]\nOnCalendar=daily\n")] == [
        "sibling:"
    ]  # same stem
    assert roots.extract_hints("notes.txt", unit) == []


def test_docs_name_commands_in_fences_and_inline_code():
    md = "run `bin/tool --x` and `linter`, not `Edit` or `--flag`.\n\n```bash\n$ lib/a.py go\n# lib/c.py\n```\n```\nplain bin/b.sh\n$ bin/d.sh\n```\n"
    hs = roots.extract_hints("README.md", md)
    assert {h.kind for h in hs} == {"doc-cmd"} and all(h.strength == "soft" and h.trust == "documented" for h in hs)
    assert targets("README.md", md) == ["cmd:linter", "file:bin/d.sh", "file:bin/tool", "file:lib/a.py"]
    assert targets("docs/make.md", "```\nmake test\n```\nuse `just lint` too\n") == [
        "build:recipe:lint",
        "build:recipe:test",
        "cmd:just",
    ]
    assert targets("a.txt", md) == []


def test_cron_and_task_xml():
    cron = "# c\nSHELL=/bin/sh\n*/5 * * * * root /usr/bin/python3 /srv/app/bin/a.py\n@daily /srv/app/bin/b\n0 1 * * * lib/c.sh\n"
    got = targets("cron.d/app", cron, "cron")
    assert "file:/srv/app/bin/a.py" in got and "file:/srv/app/bin/b" in got and "file:lib/c.sh" in got
    xml = "<Task><Actions><Exec><Command>C:\\app\\bin\\x.cmd</Command><Arguments>--a b</Arguments></Exec></Actions></Task>"
    assert targets("task.xml", xml) == ["file:C:/app/bin/x.cmd"]
    assert roots.extract_hints("pom.xml", "<project/>") == []
    assert (
        roots.extract_hints("lib/checkstyle.xml", "<module>\n  Every property, by name\n</module>") == []
    )  # prose is no crontab


GITLAB = "stages: [build]\nbuild:\n  script: make\n"
ACTION = "name: a\nruns:\n  using: node20\n  main: dist/index.js\n"


def test_gitlab_pipeline_is_a_root_by_its_file_name():
    for p in (".gitlab-ci.yml", "sub/.gitlab-ci.yaml"):
        assert [(h.kind, h.target, h.strength) for h in roots.extract_hints(p, GITLAB)] == [("ci-job", "self:", "hard")]
    assert not roots.extract_hints("ci/jobs.yml", GITLAB) and not roots.extract_hints("my.gitlab-ci.yml", GITLAB)
    got = roots.resolve(
        {"sub/.gitlab-ci.yaml": roots.extract_hints("sub/.gitlab-ci.yaml", GITLAB)},
        {"sub/.gitlab-ci.yaml": "100644"},
        lambda p: GITLAB,
    )
    assert not got.findings and [(r.kind, r.strength, r.node) for r in got.roots] == [
        ("ci-job", "hard", "yaml:sub/.gitlab-ci.yaml")
    ]


def test_published_action_is_a_root_hard_at_the_repository_root_soft_below_it():
    def hints(path, text):
        return [(h.kind, h.target, h.strength) for h in roots.extract_hints(path, text)]

    for p in ("action.yml", "action.yaml"):
        assert hints(p, ACTION) == [("export", "self:", "hard")]
    for p in (".github/actions/x/action.yml", "tools/action.yaml"):
        assert hints(p, ACTION) == [("export", "self:", "soft")]
    assert not hints("action.yml", "name: a\ndescription: not an action\n")  # no `runs:`
    assert not hints("my-action.yml", ACTION) and not hints("dir/actions.yml", ACTION)


def test_extractors_are_registered_and_path_independent():
    names = {rx.name for rx in contracts.root_extractors()}
    assert {
        "systemd",
        "cron",
        "task-xml",
        "docs",
        "npm",
        "html",
        "manifest",
        "jstool",
        "sfc",
        "gitlab",
        "classreg",
    } <= names
    assert {"action", "action-nested", "pyproject", "cargo", "docker", "make", "compose"} <= names
    assert (
        not {"settings", "mcp-reg", "mcp-py", "workflow"} & names
    )  # the registrations of one agent setup are not here
    text = "[Service]\nExecStart=/a/b.py\n"
    assert roots.extract_hints("a/x.service", text) == roots.extract_hints(
        "b/c/x.service", text
    )  # the shard is keyed by the blob
    rx = next(x for x in contracts.root_extractors() if x.name == "systemd")
    sh = contracts.make_root_shard(rx, "x.service", "b" * 40, text)
    assert sh.lang == "root:systemd" and sh.facts().roots[0].target == "file:/a/b.py"


# ================================================================ resolve
def mk(files, read_map=None, hints=None, **kw):
    mode = {p: ("100755" if p.startswith("bin/") else "100644") for p in files}
    texts = dict(read_map or {})
    return roots.resolve(hints or {}, mode, lambda p: texts.get(p), **kw)


def mkhint(kind, target, line=1, strength="hard", trust="registered", stub=""):
    return model.RootHint(kind, target, line, strength, trust, stub)


MAPS = [{"from": "/srv/app/", "to": "", "anchored": True}, {"from": "/usr/local/lib/app/", "to": "", "anchored": False}]


def test_resolve_a_registration_by_exact_path_by_map_by_suffix_and_outside_the_repository():
    files = ["jobs/a.py", "guard/guard.py", "staging/home/jobs/b.py", "other/jobs/b.py", "x.py"]
    hints = {
        "unit.service": [
            mkhint("main", "file:jobs/a.py", 3),
            mkhint("main", "file:/usr/local/lib/app/guard/guard.py", 4),
            mkhint("main", "file:~/jobs/b.py", 5),
            mkhint("main", "file:/usr/bin/some-system-tool", 6),
            mkhint("main", "file:/srv/other/bin/tool", 7),
            mkhint("main", "file:{LIB}/guard/guard.py", 8),
        ]
    }
    rs = mk(files, {p: "" for p in files}, hints, maps=MAPS)
    by = {r.origin: [] for r in rs.roots}
    for r in rs.roots:
        by[r.origin].append((r.node, r.trust))
    assert by["unit.service:3"] == [("python:jobs/a.py", "registered")]
    assert by["unit.service:4"] == [("python:guard/guard.py", "registered")]  # an unanchored map still resolves
    assert sorted(by["unit.service:5"]) == [
        ("python:other/jobs/b.py", "convention"),
        ("python:staging/home/jobs/b.py", "convention"),
    ]
    assert by["unit.service:8"] == [("python:guard/guard.py", "convention")]  # placeholder: a suffix match only
    assert "unit.service:6" not in by and "unit.service:7" not in by  # outside the repo: silent
    assert rs.findings == ()


def test_resolve_root_dangling_and_its_negative_cases():
    hints = {
        "run.cfg": [
            mkhint("main", "file:jobs/gone.py", 2),
            mkhint("main", "file:/srv/app/jobs/gone2.py", 3),
            mkhint("main", "file:/usr/local/lib/app/jobs/gone3.py", 4),  # unanchored map: the install layout may differ
            mkhint("main", "file:~/jobs/gone4.py", 5),
            mkhint("main", "file:../up/gone5.py", 6),
        ]
    }
    rs = mk(["jobs/a.py"], hints=hints, maps=MAPS)
    assert [(f.level, f.code, f.line) for f in rs.findings] == [
        ("E", "dc.root-dangling", 2),
        ("E", "dc.root-dangling", 3),
    ]
    f = rs.findings[0]
    assert f.path == "run.cfg" and "jobs/gone.py" in f.msg and f.why and "run.cfg:2" in f.why
    dang = [r for r in rs.roots if r.selector]
    assert sorted(r.selector for r in dang) == ["file:jobs/gone.py", "file:jobs/gone2.py"]
    assert all(r.kind == "main" and not r.node for r in dang)


def test_resolve_a_missing_source_is_a_w_and_registers_nothing_else():
    hints = {"pkg/pyproject.toml": [roots._rs.bad("broken toml", 4)]}
    rs = mk(["pkg/pyproject.toml"], hints=hints)
    assert [(f.level, f.code, f.line, f.path) for f in rs.findings] == [
        ("W", "dc.bad-root-source", 4, "pkg/pyproject.toml")
    ]
    assert "broken toml" in rs.findings[0].msg and rs.findings[0].why and not rs.roots


def test_resolve_systemd_cron_docs_never_dangle_unless_mapped():
    files = ["bin/audit", "lib/wrapper.py", "units/a.service", "units/a.timer", "bin/other"]
    texts = {"bin/other": "#!/bin/sh\n", "bin/audit": "#!/bin/sh\n"}
    hints = {
        "units/a.timer": [mkhint("systemd", "sibling:a.service", 9), mkhint("systemd", "sibling:", 10)],
        "units/a.service": [
            mkhint("systemd", "file:/srv/app/bin/audit", 5),
            mkhint("systemd", "file:/srv/app/bin/removed", 6),
            mkhint("systemd", "file:rel/ignored.sh", 7),
        ],
        "etc/cron.d/x": [mkhint("cron", "file:/srv/app/bin/other", 1), mkhint("cron", "file:/srv/app/bin/gone", 2)],
        "README.md": [mkhint("doc-cmd", "cmd:other", 3, "soft", "documented"), mkhint("doc-cmd", "cmd:deleted", 4, "soft", "documented"),
                      mkhint("doc-cmd", "file:bin/deleted", 5, "soft", "documented")],
    }  # fmt: skip
    rs = mk(files, texts, hints, maps=MAPS)
    assert sorted((f.path, f.line) for f in rs.findings) == [("etc/cron.d/x", 2), ("units/a.service", 6)]
    assert all(f.code == "dc.root-dangling" for f in rs.findings)  # a relative ExecStart and a doc miss are silent
    assert [(r.node, r.strength, r.trust) for r in rs.roots if r.kind == "doc-cmd"] == [
        ("shell:bin/other", "soft", "documented")
    ]
    assert {r.node or r.selector for r in rs.roots if r.kind == "systemd"} == {
        "shell:bin/audit", "systemd:units/a.service", "file:bin/removed"}  # fmt: skip


def test_resolve_timer_without_service_dangles():
    rs = mk(["a/x.timer"], hints={"a/x.timer": [mkhint("systemd", "sibling:", 1)]})
    assert [(f.code, f.path) for f in rs.findings] == [("dc.root-dangling", "a/x.timer")]
    rs = mk(["a/x.timer", "a/x.service"], hints={"a/x.timer": [mkhint("systemd", "sibling:", 1)]})
    assert rs.findings == () and [r.node for r in rs.roots if r.kind == "systemd"] == ["systemd:a/x.service"]


def test_a_timer_whose_service_is_rendered_from_a_template_does_not_dangle():
    hint = {"r/files/x.timer": [mkhint("systemd", "sibling:", 1)]}
    rs = mk(["r/files/x.timer", "r/templates/x.service.j2"], hints=hint)
    got = [(r.node.split(":", 1)[1], r.trust) for r in rs.roots if r.kind == "systemd"]
    assert rs.findings == () and got == [("r/templates/x.service.j2", "convention")]
    rs = mk(["r/files/x.timer", "r/files/x.service.in"], hints=hint)  # next to it first
    got = [r.node.split(":", 1)[1] for r in rs.roots if r.kind == "systemd"]
    assert rs.findings == () and got == ["r/files/x.service.in"]
    rs = mk(["r/files/x.timer", "r/templates/other.service.j2"], hints=hint)
    assert [(f.code, f.path) for f in rs.findings] == [("dc.root-dangling", "r/files/x.timer")]


def test_resolve_an_entry_point_with_a_function_roots_the_function_and_the_module():
    rs = mk(["pkg/cli.py"], {"pkg/cli.py": ""}, {"pyproject.toml": [mkhint("bin", "mod:pkg.cli#main", 5)]})
    assert sorted(r.node for r in rs.roots if r.kind == "bin") == ["python:pkg/cli.py", "python:pkg/cli.py#main"]
    assert rs.findings == ()


def test_resolve_bin_and_test_roots_are_soft_convention():
    files = ["bin/tool", "bin/data.txt", "bin/x.cmd", "bin/sub/deep", "lib/a.py", "tests/test_a.py", "pkg/b_test.go",
             "web/c.test.ts", "tests/data/blob.json", "docs/readme.md"]  # fmt: skip
    mode = {p: ("100755" if p == "bin/tool" else "100644") for p in files}
    texts = {"bin/tool": "#!/usr/bin/env python3\n", "bin/data.txt": "just text", "bin/x.cmd": "@echo off"}
    rs = roots.resolve({}, mode, lambda p: texts.get(p))
    got = {(r.kind, r.node) for r in rs.roots}
    assert {n for k, n in got if k == "bin"} == {"python:bin/tool", "file:bin/x.cmd"}
    # not executable (bin/data.txt, no shebang), not top level (bin/sub/deep), and no bin root for lib/ or tests/
    assert {n for k, n in got if k == "test"} == {
        "python:tests/test_a.py",
        "go:pkg/b_test.go",
        "typescript:web/c.test.ts",
    }
    assert all(r.strength == "soft" and r.trust == "convention" for r in rs.roots if r.kind in ("bin", "test"))
    assert all(r.origin.endswith(":1") for r in rs.roots)


def test_resolve_a_tracked_executable_with_a_shebang_and_a_script_extension_is_an_entry():
    files = {
        "tools/run": "100755",
        "tools/plain": "100755",
        "tests/t": "100755",
        "build.gradle.kts": "100644",
        "lib/a.py": "100644",
    }
    texts = {"tools/run": "#!/usr/bin/env python3\n", "tools/plain": "no shebang", "tests/t": "#!/bin/sh\n"}
    rs = roots.resolve({}, files, lambda p: texts.get(p))
    got = {(r.kind, r.node, r.strength) for r in rs.roots}
    assert ("bin", "python:tools/run", "hard") in got and ("main", "kotlin:build.gradle.kts", "hard") in got
    assert not [r for r in rs.roots if r.node in ("file:tools/plain", "tools/plain") or "lib/a.py" in r.node]


def test_resolve_fixture_dirs_register_nothing():
    hints = {
        "tests/fixtures/x/app.service": [mkhint("systemd", "file:/srv/app/bin/gone")],
        "spec/cron.d/x": [mkhint("cron", "file:bin/z")],
    }
    rs = mk(["a.py"], hints=hints, maps=MAPS)
    assert rs.findings == () and [r for r in rs.roots if r.kind in ("systemd", "cron")] == []


def test_resolve_is_deterministic_and_canonical():
    files = ["jobs/a.py", "jobs/b.py", "bin/x"]
    hints = {
        "run.cfg": [
            mkhint("main", "file:jobs/b.py", 2),
            mkhint("main", "file:jobs/a.py", 1),
            mkhint("main", "file:jobs/gone.py", 3),
        ]
    }
    a = mk(files, {p: "" for p in files}, hints)
    b = mk(files[::-1], {p: "" for p in files}, dict(reversed(list(hints.items()))))
    assert a.roots == b.roots and [f.as_dict() for f in a.findings] == [f.as_dict() for f in b.findings]
    g = model.canonical([], roots=a.roots)
    assert g.roots == a.roots  # already in canonical order: the same digest whatever order the linker feeds them
    assert [f.render() for f in a.findings] == [f.render() for f in model.sort_findings(a.findings)]


def test_class_and_build_selectors_are_kept_as_selector_roots():
    hints = {
        "META-INF/services/x.Y": [mkhint("classreg", "class:p.Impl", 1)],
        "README.md": [mkhint("doc-cmd", "build:recipe:test", 2, "soft", "documented")],
    }
    rs = mk(["META-INF/services/x.Y"], hints=hints)
    assert sorted(r.selector for r in rs.roots) == ["build:recipe:test", "class:p.Impl"] and rs.findings == ()


# ================================================================ collect / build on a real git tree
@pytest.fixture
def repo(tmp_path):
    r = new_repo(
        tmp_path,
        {
            "units/t.service": "[Service]\nExecStart=/usr/bin/python3 /srv/app/bin/run\n",
            "units/gone.service": "[Service]\nExecStart=/usr/bin/python3 jobs/never.py\nExecStop=/srv/app/bin/removed\n",
            "bin/run": "#!/usr/bin/env python3\nprint(1)\n",
            "bin/removed-doc-target": "#!/bin/sh\necho x\n",
            "README.md": "Use `bin/run` or `bin/was-deleted`.\n",
            "tests/test_run.py": "def test_x():\n    assert 1\n",
            "tests/fixtures/s/x.service": "[Service]\nExecStart=jobs/never.py\n",
        },
    )
    (r / "bin" / "run").chmod(0o755)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "modes")
    return r


MAPS_APP = [{"from": "/srv/app/", "to": "", "anchored": True}]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_build_on_a_real_tree(repo):
    rs = roots.build(str(repo), workers=1, maps=MAPS_APP)
    assert [(f.path, f.code, f.line) for f in rs.findings] == [("units/gone.service", "dc.root-dangling", 3)]
    assert all(f.level == "E" and f.why for f in rs.findings)
    by_kind = {}
    for r in rs.roots:
        by_kind.setdefault(r.kind, set()).add(r.node or r.selector)
    assert by_kind["systemd"] == {
        "python:bin/run",
        "file:bin/removed",
    }  # /srv/app/bin/run mapped to the repo, lang from the shebang
    assert by_kind["bin"] == {"python:bin/run", "shell:bin/removed-doc-target"}
    assert by_kind["doc-cmd"] == {"python:bin/run"}  # `bin/was-deleted` names nothing: no root and no finding
    assert by_kind["test"] == {
        "python:tests/test_run.py",
        "systemd:tests/fixtures/s/x.service",
    }  # a test file of any analysed language
    assert not any("never.py" in n for ns in by_kind.values() for n in ns)  # tests/fixtures/ config registers nothing


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_collect_is_identical_for_1_2_and_8_workers(repo):
    results = [roots.build(str(repo), workers=w, maps=MAPS_APP) for w in (1, 2, 8)]
    assert results[0].roots == results[1].roots == results[2].roots
    assert [[f.as_dict() for f in r.findings] for r in results] == [[f.as_dict() for f in results[0].findings]] * 3
    assert results[0].roots and results[0].findings


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_collect_uses_and_fills_the_store(repo, tmp_path):
    tree = vcs.snapshot(str(repo))
    with store.Store(str(tmp_path / "g" / "store.sqlite")) as st:
        cold = roots.collect(str(repo), tree, st, workers=1)
        n = st.shard_count()
        assert n >= 3  # the two units and the readme: one virtual shard per (file, extractor)
        warm = roots.collect(str(repo), tree, st, workers=1)
        assert warm == cold and st.shard_count() == n
        # prove the second run reads the shard instead of extracting: plant a different payload under the unit's key
        rx = next(x for x in contracts.root_extractors() if x.name == "systemd")
        blob = tree.files["units/t.service"].blob
        fake = model.new_shard(
            blob,
            "root:systemd",
            rx.ext_ver,
            model.Facts("root:systemd", roots=(model.RootHint("systemd", "file:bin/planted", 1),)),
        )
        assert fake.key == model.shard_key(blob, "root:systemd", rx.ext_ver)
        st.put_shard(fake)
        assert [h.target for h in roots.collect(str(repo), tree, st, workers=1)["units/t.service"]] == [
            "file:bin/planted"
        ]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_collect_a_changed_file_invalidates_only_its_shard(repo, tmp_path):
    with store.Store(str(tmp_path / "g2" / "store.sqlite")) as st:
        roots.collect(str(repo), vcs.snapshot(str(repo)), st, workers=1)
        n = st.shard_count()
        (repo / "units" / "t.service").write_text("[Service]\nExecStart=/srv/app/bin/removed-doc-target\n")
        got = roots.collect(str(repo), vcs.snapshot(str(repo)), st, workers=1)
        assert [h.target for h in got["units/t.service"]] == ["file:/srv/app/bin/removed-doc-target"]
        assert st.shard_count() == n + 1  # one new shard for the changed blob, the others were reused


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_binary_config_is_skipped(repo):
    (repo / "units" / "t.service").write_bytes(b"\0\1\2 not text")
    rs = roots.build(str(repo), workers=1, maps=MAPS_APP)
    assert not [
        r for r in rs.roots if r.origin.startswith("units/t.service")
    ]  # no crash, no root from the unreadable unit


def test_data_file_is_well_formed():
    d = roots.fw()
    for k in ("dangling", "interpreters", "wrappers", "builtins", "systemd_files", "npm_files", "doc_files"):
        assert d[k]
    assert set(d["dangling"]) <= set(model.ROOT_KINDS)
    assert all(m["from"].endswith("/") for m in d["path_maps"])
    private = ("settings_files", "settings_command_keys", "mcp_files", "gw_inputs_dir", "mcp_py_path", "workflow_files")
    assert not [k for k in private if k in d]  # the registrations of one agent setup are not part of the data
