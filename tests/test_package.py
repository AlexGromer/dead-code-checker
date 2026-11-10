"""The package surface: console entry point, version, pre-commit manifest, config keys, dogfood."""

import re
import subprocess
import sys
from pathlib import Path

import dead_code_checker
from dead_code_checker import core as dc
from test_deadcode import mkrepo, stage

ROOT = Path(__file__).resolve().parent.parent


def cli(*args, cwd=None):
    return subprocess.run([sys.executable, "-P", "-m", "dead_code_checker", *args], capture_output=True, text=True,
                          cwd=cwd, timeout=120)


def test_version_matches_pyproject():
    text = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'(?m)^version = "%s"$' % re.escape(dead_code_checker.__version__), text)


def test_public_api_is_reexported():
    for name in ("check_staged", "check_session", "scan", "Finding", "main"):
        assert hasattr(dead_code_checker, name)


def test_console_script_points_at_main():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'dead-code-checker = "dead_code_checker.core:main"' in text
    assert callable(dc.main)


def test_cli_defaults_prints_only_the_kept_keys():
    r = cli("defaults")
    assert r.returncode == 0 and "[deadcode]" in r.stdout
    assert "function_level" in r.stdout and "tests_count" in r.stdout
    for gone in ("llm", "endpoint", "model", "data_class", "time_budget_s", "request_extra", "max_candidates"):
        assert gone not in r.stdout, gone


def test_cli_staged_exit_codes(tmp_path):
    root = mkrepo(tmp_path)
    stage(root, {"lib/clean.py": "def triple(x):\n    return x * 3\n", "examples/use.py": "from clean import triple\nprint(triple(2))\n"})
    r = cli("staged", "--root", str(root))
    assert r.returncode == 0 and r.stdout == ""
    stage(root, {"hooks/dead_hook.py": "print(1)\n"})
    r = cli("staged", "--root", str(root))
    assert r.returncode == 1 and "deadcode.unreferenced-file" in r.stdout


def test_cli_scan_and_session(tmp_path):
    root = mkrepo(tmp_path, {"lib/x_mod.py": "def lonely_one(x):\n    pass\n"})
    r = cli("scan", str(root / "lib"), "--root", str(root))
    assert "lonely_one" in r.stdout and r.returncode in (0, 1)
    r = cli("session", str(root / "lib" / "x_mod.py"), "--root", str(root))
    assert r.returncode in (0, 1)


def test_cli_has_no_model_flags():
    for flag in ("--llm", "--no-llm", "--no-cache"):
        assert cli("staged", flag).returncode == 2


def test_pre_commit_manifest_has_the_five_keys():
    text = (ROOT / ".pre-commit-hooks.yaml").read_text()
    for key in ("id", "entry", "language", "pass_filenames", "always_run"):
        assert re.search(r"(?m)^\s*(- )?%s:" % key, text), key
    assert re.search(r"(?m)^\s+entry: dead-code-checker staged$", text)
    assert re.search(r"(?m)^\s+pass_filenames: false$", text) and re.search(r"(?m)^\s+always_run: true$", text)


def test_old_model_keys_are_bad_config(tmp_path):
    for key in ('llm = true', 'endpoint = "http://x"', 'model = "m"', 'data_class = "personal"'):
        root = mkrepo(tmp_path, {".dead-code-checker.toml": "[deadcode]\n%s\n" % key}, name="r%d" % abs(hash(key)))
        cfg, probs = dc.load_config(str(root))
        assert any(p.code == "deadcode.bad-config" and p.level == "E" for p in probs), key


def test_config_file_name_is_the_new_one(tmp_path):
    assert dc.CONFIG_FILES == (".dead-code-checker.toml",)
    root = mkrepo(tmp_path, {".claude/format.toml": "[deadcode]\nbogus = 1\n"})
    _cfg, probs = dc.load_config(str(root))
    assert probs == []  # the old name is not read


def test_dogfood_the_package_has_no_error_and_no_unreferenced_symbol():
    fs = dc.scan([str(ROOT / "dead_code_checker")], root=str(ROOT), jobs=1)
    assert not [f for f in fs if f.level == "E"], [f.render() for f in fs if f.level == "E"]
    assert not [f for f in fs if f.code in ("deadcode.unreferenced-symbol", "deadcode.unreferenced-public-symbol")], [
        f.render() for f in fs
    ]


def test_dogfood_through_the_cli():
    r = cli("scan", "dead_code_checker", "--root", str(ROOT))
    assert r.returncode == 0, r.stdout + r.stderr

