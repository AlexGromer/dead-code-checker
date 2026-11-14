"""dead-code-checker agent: wire the checker into a coding agent, and the hook the wiring calls.

    dead-code-checker agent install --target claude-code|codex|cursor [--dry-run] [--write] [--root DIR]
                                    [--global --confirm-global]
    dead-code-checker agent hook                    # a Claude Code PostToolUse hook: reads the event from stdin

`install` prints the snippet and the file it belongs in, and writes nothing. `--write` writes it into the PROJECT
(`--root`, default the current directory). `--dry-run` prints exactly what a write would do and still writes nothing, with
or without `--write`. The user's home is touched only with `--global --confirm-global --write` together.

    target       project file                              what is written
    claude-code  .claude/settings.json                     a PostToolUse hook on Edit|Write|MultiEdit (merged into the JSON)
    codex        AGENTS.md                                 a marked block of instructions
    cursor       .cursor/rules/dead-code-checker.mdc       an always-applied rule

Codex and Cursor get instructions, not a hook: the agent is told to run `dead-code-checker session FILE...` after it edits
code. The hard gate stays the pre-commit hook. A written block carries markers, so running install again replaces it
instead of adding a second copy. Other settings in an existing file are kept; a settings file that is not valid JSON is
refused, never overwritten.

`hook` exits 0 when the edited file has no error finding, and 2 with the findings on stderr when it has (Claude Code feeds
standard error back to the model). Warnings are not shown. A payload it cannot read, or a file the checker has no
opinion on, is exit 0: an agent hook must never be what stops a session.

Exit codes of install: 0 done or printed, 2 a usage error or a refusal.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

TARGETS = ("claude-code", "codex", "cursor")
HOOK_COMMAND = "dead-code-checker agent hook"
BEGIN = "<!-- dead-code-checker:begin -->"
END = "<!-- dead-code-checker:end -->"

INSTRUCTIONS = f"""{BEGIN}
## Dead code

After you add or change code, run `dead-code-checker session FILE...` on the files you touched and fix every `E` finding
before you say the work is done. A `W` is information. Do not add code that nothing calls, and do not leave a stub
(`pass`, `todo!()`, `raise NotImplementedError`) behind. `# deadcode: ignore` on the definition line suppresses one
finding; use it only with a reason in the same comment.
{END}
"""

CURSOR_RULE = (
    "---\n"
    "description: Do not leave dead code behind; check touched files with dead-code-checker\n"
    "alwaysApply: true\n"
    "---\n\n" + INSTRUCTIONS
)


class Refusal(Exception):
    pass


def hook_entry() -> Dict[str, Any]:
    return {"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": HOOK_COMMAND}]}


def _plan_claude(existing: Optional[str]) -> str:
    doc: Dict[str, Any] = {}
    if existing is not None and existing.strip():
        try:
            doc = json.loads(existing)
        except ValueError as e:
            raise Refusal(f"the settings file is not valid JSON ({e}); fix it or merge the snippet by hand") from None
        if not isinstance(doc, dict):
            raise Refusal("the settings file is not a JSON object")
    hooks = doc.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise Refusal('"hooks" in the settings file is not an object')
    post = hooks.setdefault("PostToolUse", [])
    if not isinstance(post, list):
        raise Refusal('"hooks.PostToolUse" in the settings file is not a list')
    already = any(
        isinstance(m, dict) and any(isinstance(h, dict) and h.get("command") == HOOK_COMMAND for h in m.get("hooks", []))
        for m in post
    )
    if not already:
        post.append(hook_entry())
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _plan_block(existing: Optional[str], block: str) -> str:
    if existing is None or not existing.strip():
        return block
    i, j = existing.find(BEGIN), existing.find(END)
    if i != -1 and j > i:
        return existing[:i] + block.rstrip("\n") + existing[j + len(END):]
    return existing.rstrip("\n") + "\n\n" + block


def project_file(target: str, root: Path) -> Path:
    return root / {"claude-code": ".claude/settings.json", "codex": "AGENTS.md",
                   "cursor": ".cursor/rules/dead-code-checker.mdc"}[target]


def global_file(target: str, home: Path) -> Path:
    if target == "cursor":
        raise Refusal("Cursor keeps user rules in its settings, not in a file; use the project rule")
    return home / {"claude-code": ".claude/settings.json", "codex": ".codex/AGENTS.md"}[target]


def plan(target: str, path: Path) -> Tuple[str, str]:
    """(snippet to show, full new text of the file)."""
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    if target == "claude-code":
        snippet = json.dumps({"hooks": {"PostToolUse": [hook_entry()]}}, indent=2) + "\n"
        return snippet, _plan_claude(existing)
    if target == "codex":
        return INSTRUCTIONS, _plan_block(existing, INSTRUCTIONS)
    if existing is not None and BEGIN not in existing and existing.strip():
        raise Refusal(f"{path} exists and is not written by this command; merge the rule by hand")
    return CURSOR_RULE, CURSOR_RULE


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):  # best-effort cleanup; the original error is re-raised
            os.unlink(tmp)
        raise


def install(a: argparse.Namespace, out=None, err=None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    home = Path.home()
    root = Path(a.root or ".").resolve()
    try:
        if a.glob:
            if a.write and not a.confirm_global:
                raise Refusal("--global --write changes your home directory; add --confirm-global to say you mean it")
            path = global_file(a.target, home)
        else:
            if a.confirm_global:
                raise Refusal("--confirm-global only goes with --global")
            if root == home.resolve() and a.write:
                raise Refusal("the project root is your home directory; use --global --confirm-global, or --root DIR")
            path = project_file(a.target, root)
        snippet, new_text = plan(a.target, path)
    except Refusal as e:
        print(f"dead-code-checker: {e}", file=err)
        return 2
    old_text = path.read_text(encoding="utf-8") if path.is_file() else None
    scope = "global" if a.glob else "project"
    if not a.write and not a.dry_run:
        print(f"# {a.target} ({scope}): add this to {path}", file=out)
        out.write(snippet)
        print("# nothing was written; --write puts it there, --dry-run shows the result", file=out)
        return 0
    if old_text == new_text:
        print(f"{path}: already up to date", file=out)
        return 0
    if a.dry_run or not a.write:
        print(f"# would write {path} ({'new file' if old_text is None else 'changed'}); nothing was written:", file=out)
        out.write(new_text)
        return 0
    _atomic_write(path, new_text)
    print(f"wrote {path}", file=out)
    return 0


def _paths_of(event: Any) -> List[str]:
    ti = event.get("tool_input") if isinstance(event, dict) else None
    if not isinstance(ti, dict):
        return []
    return [str(ti[k]) for k in ("file_path", "path", "notebook_path") if isinstance(ti.get(k), str) and ti[k]]


def hook(stdin=None, err=None) -> int:
    stdin, err = stdin or sys.stdin, err or sys.stderr
    try:
        event = json.loads(stdin.read() or "{}")
    except ValueError:
        return 0
    files = [p for p in _paths_of(event) if os.path.isfile(p)]
    if not files:
        return 0
    from .core import check_session

    try:
        errors = [f for f in check_session(files) if f.level == "E"]
    except Exception as e:  # noqa: BLE001 - a hook never stops the session because the checker failed
        print(f"dead-code-checker: skipped ({type(e).__name__})", file=err)
        return 0
    if not errors:
        return 0
    for f in errors:
        print(f.render(), file=err)
    return 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dead-code-checker agent", description="Wire the checker into a coding agent.")
    sub = ap.add_subparsers(dest="agent_cmd", required=True)
    p = sub.add_parser("install", help="print (default) or write the hook or instructions for an agent")
    p.add_argument("--target", required=True, choices=TARGETS)
    p.add_argument("--write", action="store_true", help="write into the project config (default: only print)")
    p.add_argument("--dry-run", action="store_true", help="show what a write would produce; write nothing")
    p.add_argument("--root", help="project directory (default: the current directory)")
    p.add_argument("--global", dest="glob", action="store_true", help="the user's home config instead of the project")
    p.add_argument("--confirm-global", action="store_true", help="required with --global --write")
    sub.add_parser("hook", help="PostToolUse hook for Claude Code (event JSON on stdin)")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    return hook() if a.agent_cmd == "hook" else install(a)
