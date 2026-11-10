"""dead_code_checker.core against fixture repos (real git, real files).

    python3 -m pytest -q tests/test_deadcode.py

Every repo lives under tmp_path; nothing touches the network.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
from dead_code_checker import core as dc  # noqa: E402

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


def sh(root, *args):
    return subprocess.run([*GIT, "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"))
    return p


def mkrepo(tmp_path, base=None, name="repo"):
    root = tmp_path / name
    root.mkdir()
    sh(root, "init", "-q", "-b", "main")
    write(root, "README.md", "# fixture\n")
    for rel, text in (base or {}).items():
        write(root, rel, text)
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", "base")
    return root


def stage(root, files):
    for rel, text in files.items():
        write(root, rel, text)
    sh(root, "add", "-A")


def codes(findings, level=None):
    return sorted((f.code, f.path) for f in findings if level in (None, f.level))


def has(findings, code, path=None, level=None, msg=None):
    return any(
        f.code == code
        and (path is None or f.path == path)
        and (level is None or f.level == level)
        and (msg is None or msg in f.msg)
        for f in findings
    )


def staged(root, **kw):
    return dc.check_staged(str(root), **kw)


# ---------------------------------------------------------------- 1. unreferenced symbols
def test_unreferenced_function_class_constant(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/helpers.py": """
            LIMIT_SECONDS = 30
            UNUSED_LIMIT = 99

            def used_helper(x):
                return x * 2 + 1

            def dead_helper(x):
                return x - 1

            class DeadThing:
                value = 3

            class LiveThing:
                value = 4
            """,
            "lib/consumer.py": """
            from helpers import used_helper, LIMIT_SECONDS, LiveThing
            print(used_helper(LIMIT_SECONDS), LiveThing.value)
            """,
            "docs/notes.md": "see consumer.py and helpers.py\n",
        },
    )
    fs = staged(root)
    dead = {f.msg.split(":")[0].split()[-1] for f in fs if f.code == "deadcode.unreferenced-symbol"}
    assert dead == {"dead_helper", "DeadThing", "UNUSED_LIMIT"}
    assert all(f.level == "W" for f in fs)


def test_recursion_and_own_body_do_not_count(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/walker.py": """
        def countdown(n):
            if n <= 0:
                return 0
            return countdown(n - 1) + 1
        """,
            "lib/other.py": "import walker\nprint(walker)\n",
        },
    )
    fs = staged(root)
    assert has(fs, "deadcode.unreferenced-symbol", "lib/walker.py", msg="countdown")


def test_tests_count_only_with_production_use(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/engine.py": """
            def only_tested(x):
                return x + 100

            def really_used(x):
                return x + 200
            """,
            "lib/runner.py": "from engine import really_used\nprint(really_used(1))\n",
            "tests/test_engine.py": """
            from engine import only_tested, really_used

            def test_a():
                assert only_tested(1) == 101 and really_used(1) == 201
            """,
        },
    )
    fs = staged(root)
    assert has(fs, "deadcode.test-only-symbol", "lib/engine.py", msg="only_tested")
    assert not any("really_used" in f.msg for f in fs)
    # configurable: tests count
    write(root, ".dead-code-checker.toml", "[deadcode]\ntests_count = true\n")
    sh(root, "add", "-A")
    fs = staged(root)
    assert not any(f.code in ("deadcode.test-only-symbol", "deadcode.unreferenced-symbol") for f in fs)


def test_docs_do_not_keep_a_symbol_alive_unless_configured(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/thing.py": "def documented_only(x):\n    return x * 3\n",
            "lib/user.py": "import thing\nprint(thing)\n",
            "docs/api.md": "call documented_only(x)\n",
        },
    )
    assert has(staged(root), "deadcode.unreferenced-symbol", msg="documented_only")
    write(root, ".dead-code-checker.toml", "[deadcode]\ndocs_count = true\n")
    sh(root, "add", "-A")
    assert not has(staged(root), "deadcode.unreferenced-symbol")


def test_only_new_symbols_in_a_modified_file(tmp_path):
    root = mkrepo(
        tmp_path, {"lib/mod.py": "def old_dead(x):\n    return x - 7\n", "lib/use.py": "import mod\nprint(mod)\n"}
    )
    stage(root, {"lib/mod.py": "def old_dead(x):\n    return x - 7\n\ndef new_dead(x):\n    return x - 8\n"})
    fs = staged(root)
    assert has(fs, "deadcode.unreferenced-symbol", msg="new_dead")
    assert not any("old_dead" in f.msg for f in fs)


def test_ignore_markers_and_name_globs(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/plug.py": """
        # deadcode: ignore
        def loaded_by_string(x):
            return x + 11

        def also_loaded(x):  # deadcode: ignore
            return x + 12

        def internal_cb_one(x):
            return x + 13
        """,
            "lib/loader.py": "import plug\nprint(plug)\n",
            ".dead-code-checker.toml": '[deadcode]\nignore = ["internal_cb_*"]\n',
        },
    )
    assert not [f for f in staged(root) if f.code == "deadcode.unreferenced-symbol"]
    stage(root, {"lib/skipme.py": "# deadcode: ignore-file\ndef zzz_dead(x):\n    return x + 14\n"})
    assert not any(f.path == "lib/skipme.py" for f in staged(root))


def test_decorated_dunder_and_test_names_are_not_candidates(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/web.py": """
        import app_registry

        @app_registry.route("/x")
        def handler_registered(x):
            return x * 5

        def __getattr__(name):
            return name * 2
        """,
            "lib/web_user.py": "import web\nprint(web)\n",
            "tests/test_web.py": "def test_something():\n    assert 1 + 1 == 2\n\ndef helper_unused_in_tests():\n    return 5 * 5\n",
        },
    )
    fs = staged(root)
    assert not any("handler_registered" in f.msg or "__getattr__" in f.msg or "test_something" in f.msg for f in fs)
    assert has(fs, "deadcode.unreferenced-symbol", "tests/test_web.py", msg="helper_unused_in_tests")


def test_staged_content_wins_over_worktree_and_unstaged_edit_is_ignored(tmp_path):
    root = mkrepo(tmp_path)
    stage(root, {"lib/partial.py": "def staged_dead(x):\n    return x + 21\n"})
    write(root, "lib/partial.py", "def staged_dead(x):\n    return x + 21\n\ndef unstaged_dead(x):\n    return x\n")
    fs = staged(root)
    assert has(fs, "deadcode.unreferenced-symbol", msg="staged_dead")
    assert not any("unstaged_dead" in f.msg for f in fs)


# ---------------------------------------------------------------- 2. files
def test_unreferenced_new_file_is_e_in_hooks_and_bin_w_elsewhere(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "hooks/orphan_hook.py": "import sys\nprint(sys.argv)\n",
            "bin/orphan-tool": "#!/usr/bin/env python3\nimport sys\nprint(sys.argv)\n",
            "lib/orphan_lib.py": "import sys\nprint(sys.argv)\n",
        },
    )
    fs = staged(root)
    assert has(fs, "deadcode.unreferenced-file", "hooks/orphan_hook.py", level="E")
    assert has(fs, "deadcode.unreferenced-file", "bin/orphan-tool", level="E")
    assert has(fs, "deadcode.unreferenced-file", "lib/orphan_lib.py", level="W")


@pytest.mark.parametrize(
    "where,rel,text",
    [
        ("import", "lib/consumer.py", "import wired_mod\n"),
        (
            "settings",
            ".claude/settings.json",
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/lib/wired_mod.py"'}
                                ]
                            }
                        ]
                    }
                }
            ),
        ),
        ("registry", "registry.json", json.dumps({"backends": {"x": {"argv": ["{store}/src/wired_mod.py"]}}})),
        ("doc", "docs/usage.md", "run wired_mod.py nightly\n"),
        ("test", "tests/test_wired.py", "import wired_mod\n\ndef test_it():\n    assert wired_mod\n"),
        ("systemd", "systemd/job.service", "[Service]\nExecStart=/usr/bin/python3 /srv/lib/wired_mod.py\n"),
    ],
)
def test_file_references_from_everywhere_count(tmp_path, where, rel, text):
    root = mkrepo(tmp_path)
    stage(root, {"lib/wired_mod.py": "import sys\nprint(sys.argv)\n", rel: text})
    assert not has(staged(root), "deadcode.unreferenced-file", "lib/wired_mod.py"), where


def test_exempt_files_and_dirs(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/__init__.py": "",
            "tests/test_new_thing.py": "def test_x():\n    assert True\n",
            "examples/demo_script.py": "print(1)\n",
            "templates/skeleton_tool.py": "print(2)\n",
            "src/main.go": "package main\nfunc main() {}\n",
            "pkg/helper_impl.go": "package pkg\nfunc HelperImpl() int { return 1 }\n",
            "docs/guide.md": "# guide\n",
        },
    )
    assert not has(staged(root), "deadcode.unreferenced-file")


def test_a_backup_or_archive_mention_is_not_a_reference(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/ghost_mod.py": "import sys\nprint(sys.argv)\n",
            "archive/old_notes.py": "import ghost_mod\n",
            "lib/user.py.bak": "import ghost_mod\n",
            "staging/notes.patch": "+ import ghost_mod\n",
        },
    )
    assert has(staged(root), "deadcode.unreferenced-file", "lib/ghost_mod.py")


def test_renamed_file_is_not_new(tmp_path):
    root = mkrepo(tmp_path, {"lib/old_name.py": "import sys\nprint(sys.argv)\n# body body body body body body\n"})
    sh(root, "mv", "lib/old_name.py", "lib/new_name.py")
    assert not has(staged(root), "deadcode.unreferenced-file")


# ---------------------------------------------------------------- 3. stubs
def test_python_stub_bodies(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/stubs.py": '''
        def a_pass(x):
            pass

        def b_ellipsis(x):
            ...

        def c_none(x):
            return None

        def d_dict(x):
            return {}

        def e_notimpl(x):
            raise NotImplementedError("later")

        def f_doc(x):
            """Does a thing."""

        def g_doc_pass(x):
            """Does a thing."""
            pass

        def h_real(x):
            return x * 3 + 1

        def i_list(x):
            return []
        ''',
            "lib/stubs_user.py": """
        from stubs import a_pass, b_ellipsis, c_none, d_dict, e_notimpl, f_doc, g_doc_pass, h_real, i_list
        print(a_pass, b_ellipsis, c_none, d_dict, e_notimpl, f_doc, g_doc_pass, h_real, i_list)
        """,
        },
    )
    fs = [f for f in staged(root) if f.code == "deadcode.stub-function"]
    names = {f.msg.split(":")[0] for f in fs}
    assert names == {"a_pass", "b_ellipsis", "c_none", "d_dict", "e_notimpl", "f_doc", "g_doc_pass", "i_list"}
    assert all(f.level == "W" for f in fs)


def test_stub_in_tests_and_abstract_methods_are_fine(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/base.py": """
        from abc import ABC, abstractmethod

        class Base(ABC):
            @abstractmethod
            def run(self):
                raise NotImplementedError

            def hook(self):
                pass
        """,
            "lib/base_user.py": "import base\nprint(base.Base)\n",
            "tests/test_x.py": "def fake_client(x):\n    pass\n\ndef test_a():\n    assert fake_client(1) is None\n",
        },
    )
    fs = staged(root)
    assert not any(f.code in ("deadcode.stub-function", "deadcode.todo-stub") for f in fs)


def test_method_with_todo_and_trivial_body(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/svc.py": """
        class Service:
            def start(self):
                # TODO: wire the real start
                return None

            def stop(self):
                return 1 + 1
        """,
            "lib/svc_user.py": "import svc\nprint(svc.Service)\n",
        },
    )
    assert has(staged(root), "deadcode.todo-stub", "lib/svc.py", msg="Service.start")


def test_changed_function_that_became_a_stub(tmp_path):
    root = mkrepo(
        tmp_path,
        {
            "lib/job.py": "def run_job(x):\n    return x * 9 + 4\n",
            "lib/job_user.py": "import job\nprint(job.run_job)\n",
        },
    )
    stage(root, {"lib/job.py": "def run_job(x):\n    pass\n"})
    assert has(staged(root), "deadcode.stub-function", "lib/job.py", msg="run_job")


def test_generic_language_stubs(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/tool.sh": '#!/usr/bin/env bash\nnoop_fn() { :; }\nreal_fn() {\n  echo "$1" | tr a b\n}\nnoop_fn\nreal_fn x\n',
            "src/mod.js": "function empty() {}\nfunction todo() { throw new Error('not implemented'); }\nfunction real(a) { return a + 1; }\nempty(); todo(); real(1);\n",
            "pkg/stub.go": 'package pkg\nfunc Todo() { panic("TODO") }\nfunc Real(a int) int { return a + 1 }\nfunc useAll() { Todo(); Real(1) }\n',
            "src/lib.rs": "fn later() { todo!() }\nfn real(a: i32) -> i32 { a + 1 }\nfn use_all() { later(); real(1); }\n",
        },
    )
    fs = [f for f in staged(root) if f.code == "deadcode.stub-function"]
    got = {(f.path, f.msg.split(":")[0]) for f in fs}
    assert ("lib/tool.sh", "noop_fn") in got
    assert ("src/mod.js", "empty") in got and ("src/mod.js", "todo") in got
    assert ("pkg/stub.go", "Todo") in got
    assert ("src/lib.rs", "later") in got
    assert not any(n in ("real_fn", "real", "Real") for _p, n in got)


def test_js_expression_arrow_does_not_swallow_a_later_block(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "src/flow.js": "const countAll = (xs) => xs.filter(x => x.ok).length\nreturn {\n  total: countAll(items),\n}\n"
        },
    )
    assert not any("countAll" in f.msg for f in staged(root))
    syms = {
        s.name: (s.start, s.end)
        for s, _b in dc.generic_symbols(
            "js", "const f = (a) => { return a }\nclass Foo {\n  m() {}\n}\nfunction g(a,\n b) {\n  return 1\n}\n"
        )
    }
    assert syms == {"f": (1, 1), "Foo": (2, 4), "g": (5, 8)}


# ---------------------------------------------------------------- 3b. registered stubs (a hook that approves nothing and reads nothing)
APPROVE_HOOK = """
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from hook_stdin import read_hook_stdin


    def main():
        try:
            data = read_hook_stdin()  # noqa: F841
        except Exception:
            print(json.dumps({}))
            return
        print(json.dumps({}))


    if __name__ == "__main__":
        main()
    """
SETTINGS_PERM = json.dumps(
    {
        "hooks": {
            "PermissionRequest": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 $CLAUDE_PROJECT_DIR/hooks/approve_hook.py 2>/dev/null",
                        }
                    ]
                }
            ]
        }
    }
)


def test_registered_hook_that_ignores_input_and_prints_constant_is_e(tmp_path):
    root = mkrepo(tmp_path)
    stage(root, {"hooks/approve_hook.py": APPROVE_HOOK, ".claude/settings.json": SETTINGS_PERM})
    fs = staged(root)
    assert has(fs, "deadcode.stub-hook", "hooks/approve_hook.py", level="E")
    assert not has(fs, "deadcode.unreferenced-file")  # it is registered; the problem is what it does


def test_registered_hook_that_uses_its_input_is_fine(tmp_path):
    root = mkrepo(tmp_path)
    real = APPROVE_HOOK.replace(
        "        print(json.dumps({}))\n\n\n    if",
        "        print(json.dumps({'tool': data.get('tool_name')}))\n\n\n    if",
    )
    assert "data.get" in real
    stage(root, {"hooks/approve_hook.py": real, ".claude/settings.json": SETTINGS_PERM})
    assert not has(staged(root), "deadcode.stub-hook")


def test_registered_hook_that_does_other_work_is_fine(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "hooks/gc_hook.py": 'import subprocess\nprint("{}")\nsubprocess.run(["true"])\n',
            ".claude/settings.json": SETTINGS_PERM.replace("approve_hook", "gc_hook"),
        },
    )
    assert not has(staged(root), "deadcode.stub-hook")


def test_registered_hook_exit_zero_without_reading(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "hooks/quiet.py": "import sys\nsys.exit(0)\n",
            "hooks/quiet.sh": "#!/usr/bin/env bash\n# nothing\necho '{}'\nexit 0\n",
            ".claude/settings.json": json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {"type": "command", "command": "python3 $CLAUDE_PROJECT_DIR/hooks/quiet.py"},
                                    {"type": "command", "command": "bash $CLAUDE_PROJECT_DIR/hooks/quiet.sh"},
                                ]
                            }
                        ]
                    }
                }
            ),
        },
    )
    fs = staged(root)
    assert has(fs, "deadcode.stub-hook", "hooks/quiet.py", level="E")
    assert has(fs, "deadcode.stub-hook", "hooks/quiet.sh", level="E")


def test_registered_shell_hook_that_reads_stdin_is_fine(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "hooks/real.sh": '#!/usr/bin/env bash\ninput=$(cat)\necho "$input" | jq .tool_name\n',
            ".claude/settings.json": json.dumps(
                {
                    "hooks": {
                        "Stop": [{"hooks": [{"type": "command", "command": "bash $CLAUDE_PROJECT_DIR/hooks/real.sh"}]}]
                    }
                }
            ),
        },
    )
    assert not has(staged(root), "deadcode.stub-hook")


def test_helper_stub_inside_a_working_registered_hook_is_only_w(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "hooks/real_hook.py": "import json\nimport sys\n\n\ndef _approve():\n    return {}\n\n\ndef main():\n"
            '    data = json.load(sys.stdin)\n    print(json.dumps(_approve() if data.get("x") else {"y": data}))\n\n\nmain()\n',
            ".claude/settings.json": SETTINGS_PERM.replace("approve_hook", "real_hook"),
        },
    )
    fs = staged(root)
    assert has(fs, "deadcode.stub-function", level="W", msg="_approve")
    assert not any(f.level == "E" for f in fs)


def test_registered_stub_function_is_e(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/plugins.py": "def on_session_start(ctx):\n    pass\n\ndef on_other(ctx):\n    pass\n",
            "registry.json": json.dumps({"plugins": [{"entry": "plugins:on_session_start"}]}),
            "lib/plugin_user.py": "import plugins\nprint(plugins.on_other)\n",
        },
    )
    fs = staged(root)
    assert has(fs, "deadcode.stub-function", level="E", msg="on_session_start")
    assert has(fs, "deadcode.stub-function", level="W", msg="on_other")


# ---------------------------------------------------------------- 4. config keys and env vars
def test_config_key_added_but_never_read(tmp_path):
    root = mkrepo(
        tmp_path, {"config/app.toml": "[core]\nused_key = 1\n", "lib/app.py": "KEY = 'used_key'\nprint(KEY)\n"}
    )
    stage(
        root,
        {
            "config/app.toml": "[core]\nused_key = 1\nghost_option = 5\nread_option = 6\n[ghost_section]\nx_value = 1\n",
            "lib/app.py": "KEY = 'used_key'\nprint(KEY)\nprint('read_option')\n",
        },
    )
    fs = [f for f in staged(root) if f.code == "deadcode.config-key-unread"]
    names = {f.msg.split("'")[1] for f in fs}
    assert "ghost_option" in names and "ghost_section" in names
    assert "read_option" not in names and "used_key" not in names


def test_json_and_yaml_config_keys_and_skip_lists(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "config/flags.json": '{\n  "ghost_flag": false,\n  "live_flag": true\n}\n',
            "config/svc-config.yaml": "ghost_yaml_key: 1\nlive_yaml: 2\n",
            "deploy/manifest.yaml": "ghost_manifest_key: 1\n",
            "lib/flags.py": "import json\nprint(json.load(open('x')).get('live_flag'), 'live_yaml')\n",
            "tests/fixtures/data.json": '{"fixture_only_key": 1}\n',
            "package.json": '{"scripts_unused_key": 1}\n',
        },
    )
    names = {f.msg.split("'")[1] for f in staged(root) if f.code == "deadcode.config-key-unread"}
    assert names == {"ghost_flag", "ghost_yaml_key"}


def test_code_under_a_package_called_doc_is_code_not_documentation(tmp_path):
    assert not dc.is_doc("notation/doc/engine/fix.py") and dc.is_doc("docs/guide.txt") and dc.is_doc("README.md")
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "pkg/doc/engine.py": "def real_dead(x):\n    return x - 2\n\ndef used_in_file(x):\n    return x + 2\n\nprint(used_in_file(1))\n"
        },
    )
    fs = staged(root)
    assert has(fs, "deadcode.unreferenced-symbol", msg="real_dead") and not any("used_in_file" in f.msg for f in fs)


def test_env_var_defined_but_never_read(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "bin/run.sh": "#!/usr/bin/env bash\nexport GHOST_ENV_VAR=1\nexport LIVE_ENV_VAR=2\nexport PATH=$PATH:/x\n",
            "lib/cfg.py": "import os\nprint(os.environ.get('LIVE_ENV_VAR'))\n",
            ".env.example": "GHOST_EXAMPLE_VAR=1\nLIVE_ENV_VAR=2\n",
            "docs/env.md": "GHOST_ENV_VAR documented\n",
        },
    )
    names = {f.msg.split()[2] for f in staged(root) if f.code == "deadcode.env-unread"}
    assert names == {"GHOST_ENV_VAR", "GHOST_EXAMPLE_VAR"}


# ---------------------------------------------------------------- 5. CLI flags
def test_argparse_flag_defined_but_unused(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "bin/tool.py": """
        import argparse

        def main():
            ap = argparse.ArgumentParser()
            ap.add_argument("--used-flag", action="store_true")
            ap.add_argument("--ghost-flag", action="store_true")
            ap.add_argument("--renamed", dest="custom_dest")
            ap.add_argument("--version", action="version", version="1")
            args = ap.parse_args()
            if args.used_flag:
                print(args.custom_dest)

        main()
        """,
            "docs/tool.md": "bin/tool.py --ghost-flag --used-flag\n",
        },
    )
    fs = [f for f in staged(root) if f.code == "deadcode.cli-flag-unused"]
    assert [f.msg.split(":")[0] for f in fs] == ["--ghost-flag"]


def test_argparse_flag_read_in_another_module_and_vars_dump(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/cli_a.py": """
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--shared-opt")
        ns = ap.parse_args()
        import cli_b
        cli_b.run(ns)
        """,
            "lib/cli_b.py": "def run(ns):\n    return ns.shared_opt\n",
            "lib/cli_c.py": "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('--dumped')\nargs = ap.parse_args()\nprint(vars(args))\n",
        },
    )
    assert not has(staged(root), "deadcode.cli-flag-unused")


def test_click_option_never_read(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {"bin/ctool.py": """
        import click

        @click.command()
        @click.option("--shown")
        @click.option("--ghost")
        def cli(shown, ghost):
            click.echo(shown)

        cli()
        """},
    )
    msgs = [f.msg for f in staged(root) if f.code == "deadcode.cli-flag-unused"]
    assert len(msgs) == 1 and msgs[0].startswith("--ghost")


# ---------------------------------------------------------------- clean input stays silent
def test_clean_commit_is_silent(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/clean.py": "def double(x):\n    return x * 2\n",
            "examples/main_clean.py": "from clean import double\nprint(double(2))\n",
            "tests/test_clean.py": "from clean import double\n\ndef test_d():\n    assert double(2) == 4\n",
        },
    )
    assert staged(root) == []


def test_first_commit_without_head_and_spaces_in_paths(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    sh(root, "init", "-q", "-b", "main")
    stage(root, {"lib/my module.py": "def spaced_dead(x):\n    return x - 6\n", "lib/b.bin": "x"})
    (root / "lib" / "blob.dat").write_bytes(b"\0\1\2" * 100)
    sh(root, "add", "-A")
    fs = staged(root)
    assert has(fs, "deadcode.unreferenced-symbol", "lib/my module.py", msg="spaced_dead")
    assert not any(f.path.endswith(".dat") for f in fs)


def test_cat_batch_reads_blobs_and_reports_missing(tmp_path):
    root = mkrepo(tmp_path, {"lib/a b.py": "x = 1\n"})
    got = dc._cat_batch(str(root), ["HEAD:lib/a b.py", "HEAD:nope.py", ":README.md"])
    assert got["HEAD:lib/a b.py"] == b"x = 1\n" and got["HEAD:nope.py"] is None and got[":README.md"] == b"# fixture\n"


def test_nothing_staged_or_not_a_repo(tmp_path):
    root = mkrepo(tmp_path)
    assert staged(root) == []
    plain = tmp_path / "plain"
    plain.mkdir()
    assert has(dc.check_staged(str(plain)), "deadcode.not-a-repo", level="E")


# ---------------------------------------------------------------- config
def test_bad_config_is_reported_not_ignored(tmp_path):
    root = mkrepo(tmp_path)
    stage(root, {".dead-code-checker.toml": '[deadcode]\nbogus = 1\ntests_count = "yes"\nllm = true\n'})
    fs = staged(root)
    msgs = " ".join(f.msg for f in fs if f.code == "deadcode.bad-config")
    assert "bogus" in msgs and "tests_count" in msgs and "'llm'" in msgs


def test_disabled_in_config(tmp_path):
    root = mkrepo(tmp_path)
    stage(root, {".dead-code-checker.toml": "[deadcode]\nenabled = false\n", "lib/orphan.py": "def lonely(x):\n    pass\n"})
    assert staged(root) == []


# ---------------------------------------------------------------- session helper and scan
def test_check_session_untracked_and_modified(tmp_path):
    root = mkrepo(
        tmp_path,
        {"lib/keep.py": "def keep_me(x):\n    return x * 4\n", "lib/keep_user.py": "import keep\nprint(keep)\n"},
    )
    write(root, "lib/fresh_mod.py", "def fresh_dead(x):\n    return x - 3\n")
    write(root, "lib/keep.py", "def keep_me(x):\n    return x * 4\n\ndef added_dead(x):\n    pass\n")
    fs = dc.check_session([str(root / "lib/fresh_mod.py"), "lib/keep.py"], root=str(root))
    assert has(fs, "deadcode.unreferenced-symbol", "lib/fresh_mod.py", msg="fresh_dead")
    assert has(fs, "deadcode.unreferenced-file", "lib/fresh_mod.py")
    assert has(fs, "deadcode.stub-function", "lib/keep.py", msg="added_dead")
    assert not any("keep_me" in f.msg for f in fs)
    out = dc.session_hook_output(fs)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "fresh_dead" in out["hookSpecificOutput"]["additionalContext"]
    assert dc.session_hook_output([]) is None


def test_check_session_outside_git_treats_files_as_new(tmp_path):
    d = tmp_path / "plain"
    write(d, "tool.py", "def lonely_fn(x):\n    return x - 5\n")
    fs = dc.check_session([str(d / "tool.py")], root=str(d))
    assert has(fs, "deadcode.unreferenced-symbol", msg="lonely_fn")


def test_scan_reports_everything_and_writes_a_report(tmp_path):
    root = mkrepo(tmp_path, {"lib/old.py": "def long_dead(x):\n    return x - 1\n"})
    fs = dc.scan([str(root / "lib")], root=str(root))
    assert has(fs, "deadcode.unreferenced-symbol", msg="long_dead")
    rep = tmp_path / "r.md"
    dc.write_report(fs, str(rep), "t", ["meta"])
    text = rep.read_text()
    assert "deadcode.unreferenced-symbol" in text and "long_dead" in text


# ---------------------------------------------------------------- reference index
def test_backends_agree(tmp_path):
    root = mkrepo(
        tmp_path, {"a/one.py": "def alpha_name(): return 1\nbeta-name = 2\n", "b/two.py": "alpha_name()\n# beta-name\n"}
    )
    names = ["alpha_name", "beta-name", "missing_zz"]
    got = {}
    for be in ("git", "walk"):
        res = dc.RefIndex([str(root)], be).find(names)
        got[be] = {n: sorted((os.path.relpath(h.path, root), h.line) for h in v) for n, v in res.items()}
    assert got["git"] == got["walk"]
    assert got["git"]["alpha_name"] and got["git"]["missing_zz"] == []


def test_walker_fallback_gives_the_same_hits_as_git_grep(tmp_path, monkeypatch):
    root = mkrepo(
        tmp_path,
        {
            "a/one.py": "def alpha_name(): return 1\nbeta-name = 2\n",
            "b/two.py": "alpha_name()\n# beta-name\n",
            "c/three.txt": "xalpha_name alpha_name_2 alpha_name\n",
        },
    )
    write(root, "untracked.py", "alpha_name = 3\n")
    write(root, ".gitignore", "ignored_dir/\n")
    write(root, "ignored_dir/x.py", "alpha_name = 4\n")
    names = ["alpha_name", "beta-name"]
    ref = dc.RefIndex([str(root)], "git").find(names)
    monkeypatch.setattr(dc.RefIndex, "_git_grep", staticmethod(lambda *a, **k: False))  # git without PCRE
    auto = dc.RefIndex([str(root)], "auto").find(names)
    def norm(r):
        return {n: sorted((os.path.relpath(h.path, root), h.line, h.text) for h in v) for n, v in r.items()}

    many = [f"zz_name_{i}" for i in range(450)] + names  # more than one chunk
    monkeypatch.undo()
    assert norm(dc.RefIndex([str(root)], "git").find(many)) == norm(dc.RefIndex([str(root)], "walk").find(many))
    monkeypatch.setattr(dc.RefIndex, "GIT_MAX_NAMES", 3)
    big = dc.RefIndex([str(root)], "auto").find(many)  # over the limit: walker
    assert norm(big) == norm(dc.RefIndex([str(root)], "walk").find(many))
    assert norm(ref) == norm(auto)
    assert not any("ignored_dir" in h.path for h in auto["alpha_name"])
    assert any(h.path.endswith("untracked.py") for h in auto["alpha_name"])


def test_env_example_is_readable_but_dotenv_is_not(tmp_path):
    d = tmp_path / "w"
    write(d, ".env.example", "needle_symbol=1\n")
    write(d, ".env", "needle_symbol=hunter2\n")
    write(d, ".env.local", "needle_symbol=hunter2\n")
    res = dc.RefIndex([str(d)], "walk").find(["needle_symbol"])
    assert [os.path.basename(h.path) for h in res["needle_symbol"]] == [".env.example"]


def test_walker_never_reads_secret_files(tmp_path):
    d = tmp_path / "w"
    write(d, "code.py", "needle_symbol = 1\n")
    write(d, ".env", "needle_symbol=hunter2\n")
    write(d, "id_rsa", "needle_symbol\n")
    write(d, "creds/server.pem", "needle_symbol\n")
    write(d, ".ssh/config", "needle_symbol\n")
    res = dc.RefIndex([str(d)], "walk").find(["needle_symbol"])
    assert [os.path.basename(h.path) for h in res["needle_symbol"]] == ["code.py"]


# ---------------------------------------------------------------- CLI
def test_cli_silent_on_pass_and_exit_codes(tmp_path, capsys):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/clean2.py": "def triple(x):\n    return x * 3\n",
            "examples/clean2_user.py": "from clean2 import triple\nprint(triple(2))\n",
        },
    )
    assert dc.main(["staged", "--root", str(root)]) == 0
    assert capsys.readouterr().out == ""
    stage(root, {"hooks/dead_hook.py": "print(1)\n"})
    assert dc.main(["staged", "--root", str(root)]) == 1
    assert "deadcode.unreferenced-file" in capsys.readouterr().out
    # W only => exit 0 but printed
    sh(root, "rm", "-q", "-f", "hooks/dead_hook.py")
    stage(
        root,
        {"lib/wonly.py": "def just_dead(x):\n    return x + 1\n", "lib/wonly_user.py": "import wonly\nprint(wonly)\n"},
    )
    assert dc.main(["staged", "--root", str(root), "--json"]) == 0
    assert "unreferenced-symbol" in capsys.readouterr().out


def test_cli_defaults_and_scan_report(tmp_path, capsys):
    assert dc.main(["defaults"]) == 0
    assert "tests_count" in capsys.readouterr().out
    root = mkrepo(tmp_path, {"lib/x_mod.py": "def lonely_one(x):\n    pass\n"})
    rep = tmp_path / "rep.md"
    rc = dc.main(["scan", str(root / "lib"), "--root", str(root), "--report", str(rep)])
    assert rc in (0, 1) and rep.exists() and "lonely_one" in rep.read_text()


def test_module_runs_as_a_script(tmp_path):
    root = mkrepo(tmp_path)
    stage(root, {"bin/lone-tool": "#!/usr/bin/env bash\necho hi\n"})
    r = subprocess.run(
        [sys.executable, "-P", "-m", "dead_code_checker", "staged", "--root", str(root)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1 and "unreferenced-file" in r.stdout


# ---------------------------------------------------------------- 13. adversarial review : bypasses, Windows, loud failure
def _hook_repo(tmp_path, body, cmd, name="hk", fname="hooks/h.py"):
    root = mkrepo(tmp_path, name=name)
    settings = json.dumps({"hooks": {"PermissionRequest": [{"hooks": [{"type": "command", "command": cmd}]}]}})
    stage(root, {fname: body, ".claude/settings.json": settings})
    return root


@pytest.mark.parametrize(
    "body",
    [
        "import json, sys\njson.load(sys.stdin)\njson.dump({}, sys.stdout)\n",
        "import json, sys\ndata = json.load(sys.stdin)\nprint('{}')\n",
        "import sys\nsys.stdout.write('{}')\n",
    ],
)
def test_stub_hook_spellings_are_caught(tmp_path, body):
    root = _hook_repo(tmp_path, body, "python3 hooks/h.py")
    assert has(staged(root), "deadcode.stub-hook", "hooks/h.py", level="E")


def test_hook_that_uses_its_input_is_not_a_stub_even_via_json_load(tmp_path):
    body = "import json, sys\nd = json.load(sys.stdin)\nprint(json.dumps({'decision': d.get('tool_name')}))\n"
    root = _hook_repo(tmp_path, body, "python3 hooks/h.py")
    assert not has(staged(root), "deadcode.stub-hook")


@pytest.mark.parametrize(
    "cmd",
    [
        "python C:\\Users\\a\\proj\\hooks\\h.py",
        "python .\\hooks\\h.py",
        'python "%CLAUDE_PROJECT_DIR%\\hooks\\h.py"',
        "python3 /somewhere/else/entirely/hooks/h.py --x",
    ],
)
def test_hook_commands_in_windows_and_foreign_spellings_still_register(tmp_path, cmd):
    root = _hook_repo(tmp_path, "import sys\nsys.stdin.read()\nprint('{}')\n", cmd)
    assert has(staged(root), "deadcode.stub-hook", "hooks/h.py", level="E")


@pytest.mark.parametrize("line", ["cat >/dev/null", "cat > /dev/null 2>&1", "cat - >/dev/null || true"])
def test_shell_hook_that_drains_stdin_and_prints_a_constant_is_a_stub(tmp_path, line):
    root = _hook_repo(tmp_path, f"#!/bin/sh\n{line}\necho '{{}}'\n", "bash hooks/h.sh", fname="hooks/h.sh")
    assert has(staged(root), "deadcode.stub-hook", "hooks/h.sh", level="E")


def test_shell_hook_that_filters_stdin_is_not_a_stub(tmp_path):
    root = _hook_repo(
        tmp_path, "#!/bin/sh\njq -r .tool_name | grep -q Bash && echo deny\n", "bash hooks/h.sh", fname="hooks/h.sh"
    )
    assert not has(staged(root), "deadcode.stub-hook")


def test_utf8_bom_does_not_blind_the_python_checks(tmp_path):
    root = mkrepo(tmp_path)
    (root / "lib").mkdir()
    (root / "lib/s.py").write_bytes(b"\xef\xbb\xbfdef todo_fn():\n    pass\n")
    sh(root, "add", "-A")
    fs = staged(root)
    assert has(fs, "deadcode.stub-function", "lib/s.py") and not has(fs, "deadcode.unparsed")


def test_file_that_cannot_be_parsed_or_read_is_reported_not_skipped(tmp_path):
    root = mkrepo(tmp_path)
    stage(root, {"lib/syn.py": "def (:\n", "lib/big.py": "x = 1\n" * 400000})
    (root / "lib/bin.py").write_bytes(b"def f():\n    return \x00\x01")
    sh(root, "add", "-A")
    fs = staged(root)
    assert has(fs, "deadcode.unparsed", "lib/syn.py", level="W")
    assert has(fs, "deadcode.unread", "lib/big.py", level="W") and has(fs, "deadcode.unread", "lib/bin.py", level="W")
    write(root, ".dead-code-checker.toml", '[deadcode]\nignore_paths = ["lib/big.py"]\n')
    sh(root, "add", "-A")
    assert not has(staged(root), "deadcode.unread", "lib/big.py")


def test_name_in_a_pure_comment_line_is_not_a_use(tmp_path):
    root = mkrepo(tmp_path)
    stage(
        root,
        {
            "lib/a.py": "def dead_one():\n    return 42\n",
            "lib/b.py": "# dead_one is great\nprint(1)\n",
            "lib/c.py": "def live_one():\n    return 1\n",
            "lib/d.py": "x = 1  # live_one\nfrom c import live_one\nlive_one()\n",
        },
    )
    fs = staged(root)
    assert has(fs, "deadcode.unreferenced-symbol", "lib/a.py", msg="dead_one")
    assert not has(fs, "deadcode.unreferenced-symbol", "lib/c.py")


def test_missing_git_is_a_loud_error_with_a_fix(tmp_path, monkeypatch):
    root = mkrepo(tmp_path)
    stage(root, {"lib/z.py": "def lonely():\n    pass\n"})
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    fs = dc.check_staged(str(root))
    assert has(fs, "deadcode.no-git", level="E") and "install git" in fs[0].msg


# ---------------------------------------------------------------- function_level (dead function as E, opt-in)
FN_E = '[deadcode]\nfunction_level = "E"\n'
FN_SRC = "def dead_fn_zq(x):\n    return x - 7\n"


def _fn_codes(fs, level):
    return {f.msg.split(":")[0].split()[-1] for f in fs if f.code == "deadcode.unreferenced-symbol" and f.level == level}


def test_function_level_default_is_w(tmp_path):
    root = mkrepo(tmp_path)
    stage(root, {"lib/mod.py": FN_SRC})
    fs = staged(root)
    assert _fn_codes(fs, "W") == {"dead_fn_zq"} and not [f for f in fs if f.level == "E"]


def test_function_level_e_makes_a_dead_function_an_error(tmp_path):
    root = mkrepo(tmp_path, {".dead-code-checker.toml": FN_E})
    stage(root, {"lib/mod.py": FN_SRC})
    fs = staged(root)
    assert has(fs, "deadcode.unreferenced-symbol", "lib/mod.py", level="E", msg="dead function")


def test_function_level_e_keeps_class_and_constant_at_w(tmp_path):
    root = mkrepo(tmp_path, {".dead-code-checker.toml": FN_E})
    stage(root, {"lib/mod.py": "class DeadKlassQ:\n    v = 1\n\nDEAD_CONST_Q = 5\n"})
    fs = staged(root)
    assert _fn_codes(fs, "W") == {"DeadKlassQ", "DEAD_CONST_Q"} and not [f for f in fs if f.level == "E"]


def test_function_level_e_keeps_test_only_symbol_at_w(tmp_path):
    root = mkrepo(tmp_path, {".dead-code-checker.toml": FN_E})
    stage(
        root,
        {
            "lib/mod.py": FN_SRC,
            "tests/test_mod.py": "from mod import dead_fn_zq\n\ndef test_x():\n    assert dead_fn_zq(8) == 1\n",
        },
    )
    fs = staged(root)
    assert has(fs, "deadcode.test-only-symbol", level="W") and not [f for f in fs if f.level == "E"]


def test_function_level_dirs_limit_the_error_to_those_dirs(tmp_path):
    cfg = '[deadcode]\nfunction_level = "E"\nfunction_level_dirs = ["lib"]\n'
    root = mkrepo(tmp_path, {".dead-code-checker.toml": cfg})
    stage(
        root,
        {
            "lib/a.py": "def dead_in_lib_q(x):\n    return x\n",
            "tools/b.py": "def dead_in_tools_q(x):\n    return x\n",
        },
    )
    fs = staged(root)
    assert has(fs, "deadcode.unreferenced-symbol", "lib/a.py", level="E")
    assert has(fs, "deadcode.unreferenced-symbol", "tools/b.py", level="W")


def test_function_level_dirs_match_the_path_prefix_not_any_component(tmp_path):
    cfg = '[deadcode]\nfunction_level = "E"\nfunction_level_dirs = ["lib", "src/core/"]\n'
    root = mkrepo(tmp_path, {".dead-code-checker.toml": cfg})
    stage(
        root,
        {
            "lib/sub/a.py": "def dead_in_lib_sub_q(x):\n    return x\n",
            "other/lib/m.py": "def dead_in_other_lib_q(x):\n    return x\n",
            "tests/lib/x.py": "def dead_in_tests_lib_q(x):\n    return x\n",
            "src/core/deep/c.py": "def dead_in_core_q(x):\n    return x\n",
            "src/other/d.py": "def dead_in_src_other_q(x):\n    return x\n",
            "libx/e.py": "def dead_in_libx_q(x):\n    return x\n",
        },
    )
    fs = staged(root)
    assert has(fs, "deadcode.unreferenced-symbol", "lib/sub/a.py", level="E")
    assert has(fs, "deadcode.unreferenced-symbol", "src/core/deep/c.py", level="E")
    for rel in ("other/lib/m.py", "tests/lib/x.py", "src/other/d.py", "libx/e.py"):
        assert has(fs, "deadcode.unreferenced-symbol", rel, level="W"), rel


def test_function_level_e_does_not_touch_regex_languages(tmp_path):
    root = mkrepo(tmp_path, {".dead-code-checker.toml": FN_E})
    stage(root, {"lib/util.js": "function deadJsFnQ(a) {\n  return a * 3 + 1;\n}\n"})
    fs = staged(root)
    assert has(fs, "deadcode.unreferenced-symbol", "lib/util.js", level="W")
    assert not [f for f in fs if f.code == "deadcode.unreferenced-symbol" and f.level == "E"]


def test_function_level_ignore_marker_and_bad_values(tmp_path):
    root = mkrepo(tmp_path, {".dead-code-checker.toml": FN_E})
    stage(root, {"lib/mod.py": "def quiet_fn_q(x):  # deadcode: ignore\n    return x - 7\n"})
    assert not [f for f in staged(root) if f.code == "deadcode.unreferenced-symbol"]
    cfg, probs = dc.load_config(str(root))
    assert cfg["function_level"] == "E" and not probs
    for body in ('function_level = "X"', 'function_level_dirs = "lib"', "function_level = 1"):
        r2 = mkrepo(tmp_path, {".dead-code-checker.toml": f"[deadcode]\n{body}\n"}, name="bad" + str(abs(hash(body))))
        cfg, probs = dc.load_config(str(r2))
        assert [p for p in probs if p.code == "deadcode.bad-config" and p.level == "E"], body
        assert cfg["function_level"] == "W" and cfg["function_level_dirs"] == []


def test_a_python_file_too_deeply_nested_to_parse_counts_as_unparsed():
    # ast.parse raises MemoryError ("Parser stack overflowed") here; a hostile foreign file must not abort the audit.
    assert dc.py_parse("x = " + "-" * 200000 + "1\n") is None
