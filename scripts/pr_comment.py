#!/usr/bin/env python3
"""Post (or update) one pull-request comment that summarises the new findings of `dead-code-checker graph check`.

    python3 scripts/pr_comment.py --findings dcc.json              # inside GitHub Actions: posts or updates the comment
    python3 scripts/pr_comment.py --findings dcc.json --dry-run    # prints the comment body, calls nothing

Standard library only. The token is read from $GITHUB_TOKEN and never from an argument. The repository, the pull-request
number, the API and server URLs come from the arguments or from the variables of a GitHub Actions run
($GITHUB_REPOSITORY, $GITHUB_EVENT_PATH, $GITHUB_API_URL, $GITHUB_SERVER_URL, $GITHUB_SHA).

One comment per pull request: it carries a hidden marker, and a later run edits it instead of adding another. When there
are no findings and no earlier comment, nothing is posted. A token that may not write (a pull request from a fork gets a
read-only one) is a warning and exit 0: the comment is advisory and must not fail a check. Any other API error exits 1.

Exit: 0 posted, updated, skipped or not allowed; 1 an API or input error; 2 a usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

MARKER = "<!-- dead-code-checker:pr-comment -->"
MAX_BODY = 60000  # the API limit is 65536 characters
MAX_ROWS = 25
MAX_CELL = 240
UA = "dead-code-checker-pr-comment"


class ApiError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"HTTP {status}: {text[:200]}")
        self.status = status


def load_findings(path: str) -> Tuple[List[dict], dict]:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if isinstance(doc, list):  # tolerate a bare list of findings
        return [f for f in doc if isinstance(f, dict)], {}
    if not isinstance(doc, dict) or not isinstance(doc.get("findings", []), list):
        raise ValueError("not a findings document: expected {'findings': [...]}")
    return [f for f in doc.get("findings", []) if isinstance(f, dict)], doc.get("meta") or {}


_SPAN = re.compile(r"(?<!`)`[^`\n]+`(?!`)")  # only a clean single-backtick span keeps its text as it is
_ESC = {ord(c): "\\" + c for c in "\\*_[]~`"}
_ESC.update({ord("&"): "&amp;", ord("<"): "&lt;", ord(">"): "&gt;", ord("|"): "\\|"})


def _short(text: Any) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= MAX_CELL else s[: MAX_CELL - 1] + "…"


def _cell(text: Any) -> str:
    """Text for a table cell. A `code span` keeps its characters (only | is escaped, a table needs that); everything
    outside a span has its Markdown and HTML characters escaped, so a message cannot make a link, a tag or emphasis."""
    s, out, pos = _short(text), [], 0
    for m in _SPAN.finditer(s):
        out.append(s[pos: m.start()].translate(_ESC))
        out.append(m.group().replace("|", "\\|"))
        pos = m.end()
    out.append(s[pos:].translate(_ESC))
    return "".join(out)


def _code(text: Any) -> str:
    """Text that goes inside a code span: no backtick can close it, only | is escaped."""
    return _short(text).replace("`", "").replace("|", "\\|")


def _loc(f: dict, server: str, repo: str, sha: str) -> str:
    path, line = str(f.get("path", "")), int(f.get("line") or 0)
    label = _code(f"{path}:{line}" if line else path)
    if repo and sha and path and not path.startswith(("/", "..")):
        frag = f"#L{line}" if line else ""
        url = f"{server.rstrip('/')}/{repo}/blob/{urllib.parse.quote(sha)}/{urllib.parse.quote(path)}{frag}"
        return f"[`{label}`]({url})"
    return f"`{label}`"


def counts(findings: Sequence[dict]) -> Dict[str, int]:
    out = {"E": 0, "W": 0, "other": 0}
    for f in findings:
        lv = f.get("level")
        out[lv if lv in ("E", "W") else "other"] += 1
    return out


def render(findings: Sequence[dict], *, repo: str = "", sha: str = "", server: str = "https://github.com",
           max_rows: int = MAX_ROWS) -> str:
    n = counts(findings)
    lines = [MARKER, "### dead-code-checker", ""]
    if not findings:
        lines.append("No new dead code in this pull request.")
        return "\n".join(lines) + "\n"
    head = f"{n['E']} error{'s' if n['E'] != 1 else ''}, {n['W']} warning{'s' if n['W'] != 1 else ''}"
    if n["other"]:
        head += f", {n['other']} other"
    lines.append(f"**{head}** in what this pull request adds. Only errors fail the check.")
    lines.append("")
    ordered = sorted(findings, key=lambda f: (f.get("level") != "E", f.get("level") != "W", str(f.get("path", "")),
                                              int(f.get("line") or 0), str(f.get("code", ""))))
    lines += ["| level | where | finding |", "|---|---|---|"]
    shown = ordered[:max_rows]
    for f in shown:
        what = f"`{_code(f.get('code', ''))}` {_cell(f.get('msg', ''))}"
        lines.append(f"| {_cell(f.get('level', ''))} | {_loc(f, server, repo, sha)} | {what} |")
    if len(ordered) > len(shown):
        lines.append("")
        lines.append(f"{len(ordered) - len(shown)} more not shown; the check log and the code scanning alerts list them all.")
    whys = [(f, str(f.get("why", ""))) for f in shown if f.get("level") == "E" and f.get("why")]
    if whys:
        lines += ["", "<details><summary>Why the errors were reported</summary>", ""]
        for f, why in whys[:10]:
            lines.append(f"- `{_code(f.get('code', ''))}` at {_loc(f, server, repo, sha)}: {_cell(why)}")
        lines += ["", "</details>"]
    lines += ["", "Suppress one finding with `# deadcode: ignore` on the definition line, or see the configuration docs."]
    body = "\n".join(lines) + "\n"
    if len(body) > MAX_BODY:
        body = body[: MAX_BODY - 40].rstrip() + "\n\n(comment shortened)\n"
    return body


class Api:
    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token

    def call(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", UA)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - the URL is the Actions API URL
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode("utf-8", "replace")) from None
        except urllib.error.URLError as e:
            raise ApiError(0, str(e.reason)) from None
        return json.loads(raw) if raw else None

    def find_comment(self, repo: str, pr: int) -> Optional[dict]:
        page = 1
        while page <= 50:
            rows = self.call("GET", f"/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}")
            if not rows:
                return None
            for c in rows:
                if MARKER in str(c.get("body", "")):
                    return c
            if len(rows) < 100:
                return None
            page += 1
        return None


def sync(api: Api, repo: str, pr: int, body: str, has_findings: bool) -> str:
    """'created' | 'updated' | 'skipped'."""
    old = api.find_comment(repo, pr)
    if old is not None:
        if str(old.get("body", "")) == body:
            return "skipped"
        api.call("PATCH", f"/repos/{repo}/issues/comments/{old['id']}", {"body": body})
        return "updated"
    if not has_findings:
        return "skipped"
    api.call("POST", f"/repos/{repo}/issues/{pr}/comments", {"body": body})
    return "created"


def pr_number_from_event(path: Optional[str]) -> Optional[int]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            ev = json.load(fh)
    except (OSError, ValueError):
        return None
    n = (ev.get("pull_request") or {}).get("number") or ev.get("number")
    return int(n) if isinstance(n, int) else None


def main(argv: Optional[Sequence[str]] = None, env: Optional[Dict[str, str]] = None) -> int:
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(prog="pr_comment.py", description="Summarise new findings in one pull-request comment.")
    ap.add_argument("--findings", required=True, help="JSON from `dead-code-checker graph check --format json`")
    ap.add_argument("--repo", help="owner/name (default $GITHUB_REPOSITORY)")
    ap.add_argument("--pr", type=int, help="pull-request number (default: from $GITHUB_EVENT_PATH)")
    ap.add_argument("--sha", help="commit the links point at (default $GITHUB_SHA)")
    ap.add_argument("--api-url", help="REST API root (default $GITHUB_API_URL or https://api.github.com)")
    ap.add_argument("--server-url", help="web root for links (default $GITHUB_SERVER_URL or https://github.com)")
    ap.add_argument("--max-rows", type=int, default=MAX_ROWS, help=f"findings listed in the table (default {MAX_ROWS})")
    ap.add_argument("--dry-run", action="store_true", help="print the comment body, call no API")
    a = ap.parse_args(argv)
    try:
        findings, _meta = load_findings(a.findings)
    except (OSError, ValueError) as e:
        print(f"pr_comment: cannot read {a.findings}: {e}", file=sys.stderr)
        return 1
    repo = a.repo or env.get("GITHUB_REPOSITORY", "")
    sha = a.sha or env.get("GITHUB_SHA", "")
    server = a.server_url or env.get("GITHUB_SERVER_URL") or "https://github.com"
    body = render(findings, repo=repo, sha=sha, server=server, max_rows=max(1, a.max_rows))
    if a.dry_run:
        sys.stdout.write(body)
        return 0
    token = env.get("GITHUB_TOKEN", "")
    pr = a.pr or pr_number_from_event(env.get("GITHUB_EVENT_PATH"))
    if not (repo and pr and token):
        missing = [n for n, v in (("repository", repo), ("pull-request number", pr), ("GITHUB_TOKEN", token)) if not v]
        print(f"pr_comment: not a pull-request run, or {', '.join(missing)} missing: nothing posted", file=sys.stderr)
        return 0 if not a.pr else 2
    api = Api(a.api_url or env.get("GITHUB_API_URL") or "https://api.github.com", token)
    try:
        what = sync(api, repo, pr, body, bool(findings))
    except ApiError as e:
        if e.status in (401, 403, 404):  # a fork's read-only token, or a token without pull-requests: write
            print(f"pr_comment: the token may not comment here ({e}); skipped", file=sys.stderr)
            return 0
        print(f"pr_comment: {e}", file=sys.stderr)
        return 1
    print(f"pr_comment: {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
