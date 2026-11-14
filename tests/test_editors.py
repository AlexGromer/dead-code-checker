"""The VS Code extension: manifest consistency, syntax and the node tests (skipped when node is not installed)."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dead_code_checker import lsp

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "editors" / "vscode"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


# every setting that names a program, its arguments, its environment or its working directory
PROGRAM_SETTINGS = {"deadCodeChecker.path"}
OTHER_SETTINGS = {"deadCodeChecker.enabled"}


def manifest():
    return json.loads((EXT / "package.json").read_text())


def test_why_texts_match_the_language_server():
    assert json.loads((EXT / "why.json").read_text()) == lsp.WHY


def test_manifest_declares_what_the_code_uses():
    m, code = manifest(), (EXT / "extension.js").read_text()
    assert m["main"] == "./extension.js" and (EXT / "extension.js").is_file()
    assert "dependencies" not in m and "devDependencies" not in m and "scripts" not in m  # no npm, no build step
    props = m["contributes"]["configuration"]["properties"]
    for key in props:
        assert key.startswith("deadCodeChecker.")
        name = re.escape(key.split(".", 1)[1])
        reader = r"userValue\(c, " if key in PROGRAM_SETTINGS else r"get\("  # a setting that names a program is read by level
        assert re.search(r'%s"%s"' % (reader, name), code), key
    for cmd in m["contributes"]["commands"]:
        assert 'registerCommand("%s"' % cmd["command"] in code, cmd["command"]
    assert props["deadCodeChecker.path"]["default"] == "dead-code-checker"
    assert m["license"] == "Apache-2.0"


def test_every_setting_is_classified_and_program_settings_are_machine_scoped():
    props = manifest()["contributes"]["configuration"]["properties"]
    assert set(props) == PROGRAM_SETTINGS | OTHER_SETTINGS  # a new setting must be placed in one of the two sets on purpose
    for key in PROGRAM_SETTINGS:
        assert props[key]["scope"] == "machine", key  # never "window", "resource" or "machine-overridable"
    for key in OTHER_SETTINGS:  # settings with a program-like name or a path/command/args/env/cwd meaning would need the machine scope
        assert not re.search(r"path|command|args|env|cwd|exe|server|bin", key.split(".", 1)[1], re.I), key


def test_restricted_configurations_list_the_program_settings():
    caps = manifest()["capabilities"]["untrustedWorkspaces"]
    assert caps["supported"] == "limited" and caps["description"].strip()
    assert set(caps["restrictedConfigurations"]) == PROGRAM_SETTINGS


def test_the_extension_reads_program_settings_from_the_user_level_only_and_needs_trust():
    code = (EXT / "extension.js").read_text()
    for key in PROGRAM_SETTINGS:
        assert 'get("%s"' % key.split(".", 1)[1] not in code  # get() merges the workspace over the user level
    assert "globalValue" in code and "defaultValue" in code
    assert "workspaceValue" not in code and "workspaceFolderValue" not in code
    assert "workspace.isTrusted" in code and "onDidGrantWorkspaceTrust" in code
    assert "isTrusted === true" in code  # unknown counts as untrusted
    assert code.index("allowed()") < code.index("spawn(cli")  # the trust check comes before the process start


def test_the_cli_accepts_the_stdio_flag_clients_pass():
    r = subprocess.run([sys.executable, "-P", "-m", "dead_code_checker", "lsp", "--bogus"], capture_output=True, text=True,
                       cwd=ROOT, timeout=60, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 2  # argparse knows `lsp` and rejects only the unknown flag
    assert "--stdio" in subprocess.run([sys.executable, "-P", "-m", "dead_code_checker", "lsp", "-h"], capture_output=True,
                                       text=True, cwd=ROOT, timeout=60, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}).stdout


@needs_node
@pytest.mark.parametrize("rel", ["extension.js", "map.js", "test/map.test.js", "test/extension.test.js"])
def test_node_syntax(rel):
    r = subprocess.run([NODE, "--check", str(EXT / rel)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


@needs_node
@pytest.mark.parametrize("rel", ["test/map.test.js", "test/extension.test.js"])
def test_node_unit_tests(rel):
    r = subprocess.run([NODE, str(EXT / rel)], capture_output=True, text=True, timeout=300, cwd=EXT)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
