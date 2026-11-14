"""scripts/pr_comment.py against a fake GitHub API, scripts/badge.py, and action.yml."""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr_comment = load("pr_comment")
badge = load("badge")

FINDINGS = [
    {"level": "W", "path": "pkg/b.py", "line": 7, "code": "dc.dead", "msg": "`old` has no caller | pipe"},
    {"level": "E", "path": "pkg/a.py", "line": 3, "code": "dc.introduced-dead", "msg": "`new` is unreachable",
     "why": "no root reaches it"},
]


class FakeApi:
    """The three endpoints the script uses, on a local port, with a failure switch."""

    def __init__(self):
        self.comments = []  # {"id", "body"}
        self.calls = []
        self.fail = None  # status to answer every request with
        self.auth = set()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                outer.calls.append((method, self.path, body))
                outer.auth.add(self.headers.get("Authorization"))
                if outer.fail:
                    return self._send(outer.fail, {"message": "nope"})
                m = re.fullmatch(r"/repos/o/r/issues/(\d+)/comments(\?.*)?", self.path)
                if m and method == "GET":
                    q = dict(p.split("=") for p in (m.group(2) or "?").lstrip("?").split("&") if p)
                    page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
                    return self._send(200, outer.comments[(page - 1) * per: page * per])
                if m and method == "POST":
                    c = {"id": 100 + len(outer.comments), "body": body["body"]}
                    outer.comments.append(c)
                    return self._send(201, c)
                m = re.fullmatch(r"/repos/o/r/issues/comments/(\d+)", self.path)
                if m and method == "PATCH":
                    for c in outer.comments:
                        if c["id"] == int(m.group(1)):
                            c["body"] = body["body"]
                            return self._send(200, c)
                self._send(404, {"message": "not found"})

            def _send(self, code, doc):
                raw = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

            def do_PATCH(self):
                self._go("PATCH")

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def api():
    a = FakeApi()
    yield a
    a.close()


def run(api, tmp_path, findings, *extra, env=None):
    f = tmp_path / "f.json"
    f.write_text(json.dumps({"findings": findings, "meta": {}}))
    e = {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "o/r", "GITHUB_SHA": "abc123", "GITHUB_API_URL": api.url}
    e.update(env or {})
    return pr_comment.main(["--findings", str(f), "--pr", "5", *extra], env=e)


def test_render_orders_errors_first_and_escapes():
    body = pr_comment.render(FINDINGS, repo="o/r", sha="abc", server="https://github.com")
    assert body.startswith(pr_comment.MARKER)
    assert "1 error, 1 warning" in body
    assert body.index("pkg/a.py") < body.index("pkg/b.py")
    assert "https://github.com/o/r/blob/abc/pkg/a.py#L3" in body
    assert "\\|" in body and "no root reaches it" in body


def test_render_empty_and_truncation():
    assert "No new dead code" in pr_comment.render([])
    many = [{"level": "W", "path": f"f{i}.py", "line": i + 1, "code": "dc.dead", "msg": "x"} for i in range(40)]
    body = pr_comment.render(many, max_rows=10)
    assert body.count("| W |") == 10 and "30 more not shown" in body
    huge = [{"level": "E", "path": "p.py", "line": 1, "code": "c", "msg": "m" * 5000, "why": "w" * 5000}] * 400
    assert len(pr_comment.render(huge, max_rows=400)) <= pr_comment.MAX_BODY


def test_creates_then_updates_one_comment(api, tmp_path):
    assert run(api, tmp_path, FINDINGS) == 0
    assert len(api.comments) == 1 and pr_comment.MARKER in api.comments[0]["body"]
    assert run(api, tmp_path, FINDINGS[:1]) == 0
    assert len(api.comments) == 1 and "0 errors, 1 warning" in api.comments[0]["body"]
    assert [c[0] for c in api.calls if c[0] != "GET"] == ["POST", "PATCH"]
    assert api.auth == {"Bearer tok"}


def test_identical_body_is_not_patched_again(api, tmp_path):
    run(api, tmp_path, FINDINGS)
    run(api, tmp_path, FINDINGS)
    assert [c[0] for c in api.calls if c[0] != "GET"] == ["POST"]


def test_clean_run_posts_nothing_but_updates_an_old_comment(api, tmp_path):
    assert run(api, tmp_path, []) == 0
    assert api.comments == []
    run(api, tmp_path, FINDINGS)
    run(api, tmp_path, [])
    assert "No new dead code" in api.comments[0]["body"]


def test_paths_are_quoted_and_messages_cannot_make_markup():
    f = {"level": "W", "path": "a b/(c).py", "line": 4, "code": "dc.dead",
         "msg": "[x](http://evil.invalid) *b* `keep<this>|` <img src=x> & more"}
    body = pr_comment.render([f], repo="o/r", sha="abc", server="https://github.com")
    assert "https://github.com/o/r/blob/abc/a%20b/%28c%29.py#L4" in body
    assert "\\[x\\](http://evil.invalid)" in body and "\\*b\\*" in body
    assert "`keep<this>\\|`" in body  # a code span keeps its text, a table only needs the pipe escaped
    assert "<img" not in body and "&lt;img src=x&gt; &amp; more" in body
    assert pr_comment._cell("``a``") == "\\`\\`a\\`\\`"  # only a clean single-backtick span is a span


def test_foreign_comments_are_left_alone(api, tmp_path):
    api.comments.append({"id": 1, "body": "unrelated"})
    run(api, tmp_path, FINDINGS)
    assert api.comments[0]["body"] == "unrelated" and len(api.comments) == 2


def test_finds_the_comment_on_a_later_page(api, tmp_path):
    api.comments = [{"id": i, "body": "x"} for i in range(1, 130)]
    api.comments.append({"id": 999, "body": pr_comment.MARKER + "\nold"})
    run(api, tmp_path, FINDINGS)
    assert api.comments[-1]["body"].startswith(pr_comment.MARKER) and "pkg/a.py" in api.comments[-1]["body"]
    assert len(api.comments) == 130


@pytest.mark.parametrize("status,rc", [(403, 0), (401, 0), (404, 0), (500, 1)])
def test_api_failures(api, tmp_path, status, rc, capsys):
    api.fail = status
    assert run(api, tmp_path, FINDINGS) == rc
    assert "pr_comment" in capsys.readouterr().err


def test_not_a_pull_request_run_is_quiet_exit_0(tmp_path, capsys):
    f = tmp_path / "f.json"
    f.write_text(json.dumps({"findings": FINDINGS}))
    assert pr_comment.main(["--findings", str(f)], env={"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}) == 0
    assert "nothing posted" in capsys.readouterr().err


def test_pr_number_from_event_file(api, tmp_path):
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"pull_request": {"number": 5}}))
    f = tmp_path / "f.json"
    f.write_text(json.dumps({"findings": FINDINGS}))
    env = {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "GITHUB_API_URL": api.url, "GITHUB_EVENT_PATH": str(ev)}
    assert pr_comment.main(["--findings", str(f)], env=env) == 0
    assert len(api.comments) == 1


def test_dry_run_calls_nothing_and_needs_no_token(api, tmp_path, capsys):
    f = tmp_path / "f.json"
    f.write_text(json.dumps({"findings": FINDINGS}))
    assert pr_comment.main(["--findings", str(f), "--dry-run"], env={}) == 0
    assert pr_comment.MARKER in capsys.readouterr().out and api.calls == []


def test_bad_input_file(tmp_path):
    f = tmp_path / "f.json"
    f.write_text("{nope")
    assert pr_comment.main(["--findings", str(f), "--dry-run"], env={}) == 1


def test_badge_levels(tmp_path, capsys):
    cases = [([], "none", "brightgreen"), ([{"level": "W"}], "1 warning", "yellow"),
             ([{"level": "W"}, {"level": "E"}, {"level": "E"}], "2 errors", "red")]
    for fs, msg, color in cases:
        src, out = tmp_path / "f.json", tmp_path / "b.json"
        src.write_text(json.dumps({"findings": fs}))
        assert badge.main(["--findings", str(src), "--out", str(out)]) == 0
        doc = json.loads(out.read_text())
        assert doc == {"schemaVersion": 1, "label": "dead code", "message": msg, "color": color}
    lines = capsys.readouterr().out.split()
    assert lines[-2:] == ["errors=2", "warnings=1"]


def test_badge_unreadable_input(tmp_path):
    assert badge.main(["--findings", str(tmp_path / "missing.json"), "--out", str(tmp_path / "b.json")]) == 1


def test_action_yaml_shape():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load((ROOT / "action.yml").read_text())
    assert doc["runs"]["using"] == "composite"
    steps = doc["runs"]["steps"]
    assert all("run" in s or "uses" in s for s in steps)
    for out in doc["outputs"].values():
        assert out["value"].startswith("${{ steps.")
    for s in steps:  # inputs reach shell code through env, never interpolated into the script text
        assert "${{" not in s.get("run", ""), s["name"]
        if "run" in s:
            assert s["shell"] == "bash"


def test_action_upload_step_is_the_only_unpinned_placeholder():
    text = (ROOT / "action.yml").read_text()
    uses = re.findall(r"(?m)^\s*uses:\s*(\S+)", text)
    assert uses == ["github/codeql-action/upload-sarif@PIN_BY_SHA"]
    assert "PIN BEFORE RELEASE" in text
    assert "PIN_BY_SHA" in (ROOT / "docs" / "ci.md").read_text()


# --- action.yml: the run steps are executed as the runner would, with a fake checker --------------------------------------

yaml = pytest.importorskip("yaml")
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is needed to run the steps of the action")

BASE_SHA = "1" * 40
FAKE_CHECKER = """#!/usr/bin/env bash
echo "$@" >> "$FAKE_LOG"
fmt=
while [ $# -gt 0 ]; do [ "$1" = --format ] && fmt=$2; shift; done
if [ "$fmt" = json ]; then [ -f "$FAKE_JSON" ] && cat "$FAKE_JSON"; exit "${FAKE_RC:-0}"; fi
if [ "$fmt" = sarif ]; then echo '{"version": "2.1.0"}'; exit "${FAKE_SARIF_RC:-0}"; fi
exit 2
"""
TOKEN = re.compile(r"'[^']*'|[A-Za-z_][\w.\-]*\(\)|[A-Za-z_][\w.\-]*|\|\||&&|==|!=|[()]")


def _expr(src, ctx):
    """The small part of the Actions expression language that action.yml uses: || && == != ( ), 'strings', true, false,
    null, always() and dotted lookups in the context."""
    toks = TOKEN.findall(src)
    assert "".join(toks) == "".join(src.split()), src
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def take():
        pos[0] += 1
        return toks[pos[0] - 1]

    def look(path):
        v = ctx
        for k in path.split("."):
            v = v.get(k) if isinstance(v, dict) else None
        return v

    def atom():
        tk = take()
        if tk == "(":
            v = either()
            assert take() == ")"
            return v
        if tk[0] == "'":
            return tk[1:-1]
        if tk == "always()":
            return True
        return {"true": True, "false": False, "null": None}[tk] if tk in ("true", "false", "null") else look(tk)

    def compare():
        v = atom()
        if peek() in ("==", "!="):
            op = take()
            w = atom()
            v, w = ("" if v is None and isinstance(w, str) else v), ("" if w is None and isinstance(v, str) else w)
            return (v == w) if op == "==" else (v != w)  # null against a string compares as an empty string
        return v

    def both():
        v = compare()
        while peek() == "&&":
            take()
            w = compare()
            v = w if v else v
        return v

    def either():
        v = both()
        while peek() == "||":
            take()
            w = both()
            v = v if v else w
        return v

    out = either()
    assert pos[0] == len(toks), src
    return out


def _sub(text, ctx):
    def one(m):
        v = _expr(m.group(1).strip(), ctx)
        return "" if v is None else str(v).lower() if isinstance(v, bool) else str(v)

    return re.sub(r"\$\{\{(.*?)\}\}", one, str(text))


class Run:
    """The composite action run step by step: `if` honoured, a failed step skips the plain ones after it, the steps
    that `uses:` another action are not run but get the outcome the test chooses."""

    def __init__(self, tmp_path):
        tmp_path.mkdir(exist_ok=True)
        self.tmp = tmp_path
        self.doc = yaml.safe_load((ROOT / "action.yml").read_text())
        self.log = tmp_path / "checker.log"
        self.skip = {"Install dead-code-checker", "Comment on the pull request"}

    def go(self, *, findings=None, rc=0, sarif_rc=0, inputs=None, pr=True, fork=False, upload_outcome="success"):
        bin_ = self.tmp / "bin"
        bin_.mkdir(exist_ok=True)
        (bin_ / "dead-code-checker").write_text(FAKE_CHECKER)
        (bin_ / "dead-code-checker").chmod(0o755)
        j = self.tmp / "fake.json"
        if findings is not None:
            j.write_text(findings if isinstance(findings, str) else json.dumps({"findings": findings}))
        work = self.tmp / "work"
        work.mkdir(exist_ok=True)
        gh_out, summary = self.tmp / "gh_out", self.tmp / "summary"
        gh_out.write_text("")
        summary.write_text("")
        event = ({"pull_request": {"base": {"sha": BASE_SHA}, "head": {"repo": {"fork": fork}}}} if pr
                 else {"before": BASE_SHA})
        ctx = {"inputs": {k: v["default"] for k, v in self.doc["inputs"].items()},
               "github": {"event": event, "event_name": "pull_request" if pr else "push"}, "steps": {}}
        ctx["inputs"].update(inputs or {})
        ctx["inputs"]["working-directory"] = str(work)
        env0 = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "RUNNER_TEMP": str(self.tmp),
                "FAKE_LOG": str(self.log), "GITHUB_OUTPUT": str(gh_out), "GITHUB_STEP_SUMMARY": str(summary),
                "GITHUB_ACTION_PATH": str(ROOT), "FAKE_JSON": str(j), "FAKE_RC": str(rc), "FAKE_SARIF_RC": str(sarif_rc),
                "PYTHONDONTWRITEBYTECODE": "1"}
        self.ran, self.logs, failed = [], "", False
        for i, s in enumerate(self.doc["runs"]["steps"]):
            name, sid = s["name"], s.get("id", f"step{i}")
            cond = s.get("if")
            if cond is None:
                go = not failed
            else:
                go = bool(_expr(str(cond).strip().removeprefix("${{").removesuffix("}}").strip(), ctx))
            if not go:
                ctx["steps"][sid] = {"outcome": "skipped", "outputs": {}}
                continue
            self.ran.append(name)
            if "uses" in s:
                ctx["steps"][sid] = {"outcome": upload_outcome, "outputs": {}}
                if upload_outcome == "failure" and not s.get("continue-on-error"):
                    failed = True
                continue
            if name in self.skip:
                ctx["steps"][sid] = {"outcome": "success", "outputs": {}}
                continue
            env = {**env0, **{k: _sub(v, ctx) for k, v in s.get("env", {}).items()}}
            cwd = _sub(s.get("working-directory", str(work)), ctx)  # a step without one runs in the workspace
            before = gh_out.read_text()
            r = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", s["run"]], cwd=cwd,
                               env=env, capture_output=True, text=True, timeout=60)
            self.logs += r.stdout + r.stderr
            outs = dict(ln.split("=", 1) for ln in gh_out.read_text()[len(before):].splitlines() if "=" in ln)
            ctx["steps"][sid] = {"outcome": "success" if r.returncode == 0 else "failure", "outputs": outs}
            if r.returncode and not s.get("continue-on-error"):
                failed = True
        self.ok = not failed
        self.out = ctx["steps"]
        return self

    def checker_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


E = {"level": "E", "path": "a.py", "line": 1, "code": "dc.introduced-dead", "msg": "`f` is unreachable"}
W = {"level": "W", "path": "b.py", "line": 2, "code": "dc.dead", "msg": "`g` has no caller"}


@needs_bash
@pytest.mark.parametrize("findings,rc,ok", [([], 0, True), ([W, W], 0, True), ([E], 1, False), ([W, E], 1, False)])
def test_job_fails_only_when_the_checker_reports_an_error(tmp_path, findings, rc, ok):
    r = Run(tmp_path).go(findings=findings, rc=rc)
    assert r.ok is ok
    assert r.out["summary"]["outputs"]["errors"] == str(sum(f["level"] == "E" for f in findings))
    assert r.out["summary"]["outputs"]["warnings"] == str(sum(f["level"] == "W" for f in findings))
    assert ("::error::dead-code-checker found" in r.logs) is (not ok)
    assert r.out["check"]["outcome"] == "success"  # the check step records, the last step decides
    assert "Upload SARIF" in r.ran  # an E does not stop the report from reaching code scanning


@needs_bash
@pytest.mark.parametrize("findings", [None, "{not json"])
def test_a_broken_run_is_never_a_silent_pass(tmp_path, findings):
    r = Run(tmp_path / "a").go(findings=findings, rc=2)
    assert not r.ok and "failed to run (exit 2)" in r.logs
    r = Run(tmp_path / "b").go(findings=findings, rc=1)  # a crash that happens to exit 1
    assert not r.ok and "an unknown number of error(s)" in r.logs


@needs_bash
def test_a_refused_upload_is_a_warning_and_does_not_change_the_result(tmp_path):
    clean = Run(tmp_path / "a").go(findings=[W], rc=0, upload_outcome="failure")
    assert clean.ok and "::warning::the SARIF upload was refused" in clean.logs
    assert clean.out["upload"]["outcome"] == "failure"
    bad = Run(tmp_path / "b").go(findings=[E], rc=1, upload_outcome="failure")
    assert not bad.ok and "found 1 error(s)" in bad.logs  # the verdict still follows the exit code


@needs_bash
def test_a_pull_request_from_a_fork_skips_the_upload_and_still_gates(tmp_path):
    r = Run(tmp_path / "a").go(findings=[W], rc=0, fork=True)
    assert r.ok and "Upload SARIF" not in r.ran and "::notice::the SARIF upload is skipped" in r.logs
    assert not any("--format sarif" in c for c in r.checker_calls())  # nothing will upload it, so it is not made
    assert "sarif-file" not in r.out["check"]["outputs"]
    bad = Run(tmp_path / "b").go(findings=[E], rc=1, fork=True)
    assert not bad.ok


@needs_bash
def test_upload_off_makes_no_sarif_run_and_a_failed_sarif_run_is_only_a_warning(tmp_path):
    r = Run(tmp_path / "a").go(findings=[], rc=0, inputs={"upload-sarif": "false"})
    assert r.ok and "Upload SARIF" not in r.ran
    assert len(r.checker_calls()) == 1 and "--format json" in r.checker_calls()[0]
    s = Run(tmp_path / "b").go(findings=[], rc=0, sarif_rc=2)
    assert s.ok and "produced no SARIF report (exit 2)" in s.logs and "Upload SARIF" not in s.ran


@needs_bash
def test_the_base_and_scope_reach_the_checker(tmp_path):
    r = Run(tmp_path / "a").go(findings=[], rc=0)
    assert r.checker_calls() and all(f"graph check --scope branch --base {BASE_SHA} --format" in c
                                     for c in r.checker_calls())
    r = Run(tmp_path / "b").go(findings=[], rc=0, pr=False, inputs={"scope": "repo"})
    assert r.checker_calls() and all(c.startswith("graph check --scope repo --format") for c in r.checker_calls())


def test_upload_step_cannot_fail_the_job():
    steps = yaml.safe_load((ROOT / "action.yml").read_text())["runs"]["steps"]
    up = next(s for s in steps if s.get("uses", "").startswith("github/codeql-action/upload-sarif@"))
    assert up["continue-on-error"] is True and up["id"] == "upload"
    assert "head.repo.fork != true" in up["if"]
    # the job ends in one place: the last step alone reads the exit code, nothing after the upload can fail on its own
    assert steps[-1]["name"] == "Fail on errors" and "RC" in steps[-1]["env"]
    after = steps[steps.index(up) + 1:-1]
    assert all(s.get("continue-on-error") or s["name"] == "Say what happened to the upload" for s in after)
