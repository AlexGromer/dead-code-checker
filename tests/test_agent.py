"""dead-code-checker agent install / hook."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dead_code_checker import agent

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    a = agent.build_parser().parse_args(["install", *argv])
    rc = agent.install(a, out, err)
    return rc, out.getvalue(), err.getvalue()


def tree(p):
    return sorted(str(x.relative_to(p)) for x in p.rglob("*") if x.is_file())


@pytest.mark.parametrize("target", agent.TARGETS)
def test_default_prints_and_writes_nothing(tmp_path, home, target):
    proj = tmp_path / "proj"
    proj.mkdir()
    rc, out, _ = run("--target", target, "--root", str(proj))
    assert rc == 0 and "nothing was written" in out
    assert tree(proj) == [] and tree(home) == []


@pytest.mark.parametrize("target", agent.TARGETS)
def test_dry_run_with_write_writes_nothing(tmp_path, home, target):
    proj = tmp_path / "proj"
    proj.mkdir()
    rc, out, _ = run("--target", target, "--root", str(proj), "--write", "--dry-run")
    assert rc == 0 and "would write" in out
    assert tree(proj) == [] and tree(home) == []


def test_claude_code_write_creates_settings_with_the_hook(tmp_path, home):
    rc, out, _ = run("--target", "claude-code", "--root", str(tmp_path), "--write")
    assert rc == 0
    doc = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    entry = doc["hooks"]["PostToolUse"][0]
    assert entry["matcher"] == "Edit|Write|MultiEdit"
    assert entry["hooks"] == [{"type": "command", "command": "dead-code-checker agent hook"}]
    assert tree(home) == []


def test_claude_code_merge_keeps_other_settings_and_is_idempotent(tmp_path, home):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"model": "x", "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": []}],
                                                      "Stop": [{"hooks": []}]}}))
    run("--target", "claude-code", "--root", str(tmp_path), "--write")
    first = p.read_text()
    rc, out, _ = run("--target", "claude-code", "--root", str(tmp_path), "--write")
    assert "already up to date" in out and p.read_text() == first
    doc = json.loads(first)
    assert doc["model"] == "x" and "Stop" in doc["hooks"] and len(doc["hooks"]["PostToolUse"]) == 2
    assert not list(p.parent.glob("*.tmp"))


def test_invalid_settings_json_is_refused_untouched(tmp_path, home):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir()
    p.write_text("{ not json")
    rc, _, err = run("--target", "claude-code", "--root", str(tmp_path), "--write")
    assert rc == 2 and "not valid JSON" in err and p.read_text() == "{ not json"


def test_codex_block_is_appended_then_replaced(tmp_path, home):
    p = tmp_path / "AGENTS.md"
    p.write_text("# Mine\n\nKeep this.\n")
    run("--target", "codex", "--root", str(tmp_path), "--write")
    text = p.read_text()
    assert text.startswith("# Mine\n\nKeep this.\n") and text.count(agent.BEGIN) == 1
    p.write_text(text.replace("Do not add code", "OLD WORDING"))
    run("--target", "codex", "--root", str(tmp_path), "--write")
    again = p.read_text()
    assert again.count(agent.BEGIN) == 1 and "OLD WORDING" not in again and again.startswith("# Mine")


def test_cursor_rule_and_refusal_to_overwrite_a_foreign_file(tmp_path, home):
    run("--target", "cursor", "--root", str(tmp_path), "--write")
    f = tmp_path / ".cursor" / "rules" / "dead-code-checker.mdc"
    assert f.read_text().startswith("---\n") and "alwaysApply: true" in f.read_text()
    f.write_text("my own rule\n")
    rc, _, err = run("--target", "cursor", "--root", str(tmp_path), "--write")
    assert rc == 2 and f.read_text() == "my own rule\n"


def test_global_needs_confirmation_and_only_then_touches_home(tmp_path, home):
    rc, _, err = run("--target", "claude-code", "--global", "--write")
    assert rc == 2 and "--confirm-global" in err and tree(home) == []
    rc, out, _ = run("--target", "claude-code", "--global")  # print only: fine without confirmation
    assert rc == 0 and tree(home) == []
    rc, out, _ = run("--target", "claude-code", "--global", "--write", "--dry-run", "--confirm-global")
    assert rc == 0 and tree(home) == []
    rc, out, _ = run("--target", "claude-code", "--global", "--write", "--confirm-global")
    assert rc == 0 and tree(home) == [".claude/settings.json"]


def test_cursor_has_no_global_file_and_confirm_needs_global(tmp_path, home):
    assert run("--target", "cursor", "--global", "--write", "--confirm-global")[0] == 2
    assert run("--target", "codex", "--root", str(tmp_path), "--confirm-global")[0] == 2
    assert tree(home) == []


def test_project_root_equal_to_home_is_refused(home):
    rc, _, err = run("--target", "codex", "--root", str(home), "--write")
    assert rc == 2 and tree(home) == []


def _env():
    return {**os.environ, "PYTHONPATH": str(ROOT), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def hook(event, cwd, raw=None):
    return subprocess.run([sys.executable, "-P", "-m", "dead_code_checker", "agent", "hook"],
                          input=json.dumps(event) if raw is None else raw, capture_output=True, text=True, cwd=cwd,
                          timeout=120, env=_env())


def test_hook_ignores_garbage_and_missing_files(tmp_path):
    assert hook(None, tmp_path, raw="not json").returncode == 0
    assert hook({"tool_input": {"file_path": str(tmp_path / "nope.py")}}, tmp_path).returncode == 0
    assert hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, tmp_path).returncode == 0


def test_hook_blocks_on_an_error_finding_only(tmp_path):
    env = _env()
    ident = ["-c", "user.name=t", "-c", "user.email=t@localhost"]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
    (tmp_path / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, env=env)
    subprocess.run(["git", *ident, "commit", "-qm", "init"], cwd=tmp_path, check=True, env=env)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    dead = hooks / "unused_tool.py"  # a new file in hooks/ that nothing mentions: unreferenced-file is E there
    dead.write_text("def helper():\n    return 1\n")
    r = hook({"tool_input": {"file_path": str(dead)}}, tmp_path)
    assert r.returncode == 2 and "unreferenced-file" in r.stderr
    warn = tmp_path / "lib.py"  # a W finding is silent
    warn.write_text("def lonely():\n    return 2\n")
    assert hook({"tool_input": {"file_path": str(warn)}}, tmp_path).returncode == 0


def test_core_dispatches_agent():
    r = subprocess.run([sys.executable, "-P", "-m", "dead_code_checker", "agent", "install", "--target", "codex"],
                       capture_output=True, text=True, cwd=ROOT, env=_env())
    assert r.returncode == 0 and agent.BEGIN in r.stdout
