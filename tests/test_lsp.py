"""The language server: protocol over pipes against a temp repository, and the pure mapping helpers."""

import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dead_code_checker import lsp
from test_deadcode import mkrepo

BASE = {"lib/x_mod.py": "def lonely_one(x):\n    pass\n"}
TIMEOUT = 60


class Client:
    """Drives `dead-code-checker lsp` over its stdin/stdout."""

    def __init__(self, cwd):
        self.p = subprocess.Popen(
            [sys.executable, "-P", "-m", "dead_code_checker", "lsp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=cwd,
        )  # fmt: skip
        self.q: "queue.Queue[dict]" = queue.Queue()
        self.seen = []
        self.next_id = 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        srv = lsp.Server(self.p.stdout, io.BytesIO())
        while True:
            try:
                m = srv.read_message()
            except ValueError:
                return
            if m is None:
                return
            self.q.put(m)

    def send(self, msg):
        body = json.dumps({"jsonrpc": "2.0", **msg}).encode()
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.p.stdin.flush()

    def request(self, method, params=None):
        self.next_id += 1
        self.send({"id": self.next_id, "method": method, "params": params})
        return self.wait(lambda m: m.get("id") == self.next_id)

    def notify(self, method, params=None):
        self.send({"method": method, "params": params})

    def wait(self, pred, timeout=TIMEOUT):
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            self.seen.append(m)
            if pred(m):
                return m
        raise AssertionError(f"timed out; saw {self.seen}")

    def diagnostics_for(self, uri, timeout=TIMEOUT):
        return self.wait(lambda m: m.get("method") == "textDocument/publishDiagnostics" and m["params"]["uri"] == uri,
                         timeout)["params"]["diagnostics"]

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        for f in (self.p.stdout, self.p.stderr):
            f.close()


@pytest.fixture
def repo(tmp_path):
    return mkrepo(tmp_path, BASE)


@pytest.fixture
def client(repo):
    c = Client(repo)
    yield c
    c.close()


def init(c, root, **opts):
    r = c.request("initialize", {"rootUri": lsp.path_to_uri(str(root)), "capabilities": {},
                                 "initializationOptions": {"debounceMs": 20, **opts}})
    c.notify("initialized", {})
    return r


def test_initialize_advertises_save_sync(client, repo):
    r = init(client, repo)
    sync = r["result"]["capabilities"]["textDocumentSync"]
    assert sync["openClose"] is True and sync["save"] == {"includeText": False} and sync["change"] == 0
    assert r["result"]["serverInfo"]["name"] == "dead-code-checker"


def test_open_publishes_diagnostics_with_level_code_and_why(client, repo):
    init(client, repo)
    uri = lsp.path_to_uri(str(repo / "lib" / "x_mod.py"))
    client.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": ""}})
    ds = client.diagnostics_for(uri)
    by_code = {d["code"]: d for d in ds}
    assert {"deadcode.unreferenced-symbol", "deadcode.stub-function"} <= set(by_code)
    d = by_code["deadcode.unreferenced-symbol"]
    assert d["severity"] == 2 and d["source"] == "dead-code-checker"
    assert d["range"]["start"] == {"line": 0, "character": 0}
    assert d["range"]["end"] == {"line": 0, "character": len("def lonely_one(x):")}
    assert "lonely_one" in d["message"] and "deadcode: ignore" in d["message"]  # message + why


def test_save_rescans_and_clears_fixed_files(client, repo):
    init(client, repo)
    f = repo / "lib" / "x_mod.py"
    uri = lsp.path_to_uri(str(f))
    client.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": ""}})
    assert client.diagnostics_for(uri)
    f.write_text("def lonely_one(x):\n    return x + 1\n\nprint(lonely_one(1))\n")
    (repo / "README.md").write_text("# fixture\nlib/x_mod.py\n")
    client.notify("textDocument/didSave", {"textDocument": {"uri": uri}})
    assert client.diagnostics_for(uri) == []  # an empty list clears what was published before


def test_error_level_maps_to_severity_1(tmp_path):
    root = mkrepo(tmp_path, {".dead-code-checker.toml": "[deadcode]\nbogus = 1\n"})
    c = Client(root)
    try:
        init(c, root)
        c.notify("textDocument/didSave", {"textDocument": {"uri": lsp.path_to_uri(str(root / "README.md"))}})
        m = c.wait(lambda m: m.get("method") == "textDocument/publishDiagnostics"
                   and any(d["code"] == "deadcode.bad-config" for d in m["params"]["diagnostics"]))
        d = [d for d in m["params"]["diagnostics"] if d["code"] == "deadcode.bad-config"][0]
        assert d["severity"] == 1
    finally:
        c.close()


def test_root_falls_back_to_the_git_root_of_the_opened_file(tmp_path):
    root = mkrepo(tmp_path, BASE)
    c = Client(root)
    try:
        r = c.request("initialize", {"rootUri": None, "capabilities": {}, "initializationOptions": {"debounceMs": 20}})
        assert "capabilities" in r["result"]
        uri = lsp.path_to_uri(str(root / "lib" / "x_mod.py"))
        c.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": ""}})
        assert c.diagnostics_for(uri)
    finally:
        c.close()


def test_unknown_request_is_method_not_found(client, repo):
    init(client, repo)
    r = client.request("textDocument/hover", {})
    assert r["error"]["code"] == -32601


def test_shutdown_then_exit_is_clean(client, repo):
    init(client, repo)
    assert client.request("shutdown")["result"] is None
    client.notify("exit")
    assert client.p.wait(timeout=20) == 0


def test_exit_without_shutdown_is_exit_1(client, repo):
    init(client, repo)
    client.notify("exit")
    assert client.p.wait(timeout=20) == 1


def test_eof_ends_the_server(client, repo):
    init(client, repo)
    client.p.stdin.close()
    assert client.p.wait(timeout=20) == 1


# ---------------------------------------------------------------- debounce, cancel, failure (in process, fake command)
def serve_in_thread(command):
    """A Server on OS pipes; returns (server, write end for the client, read end of its output)."""
    cr, sw = os.pipe()
    sr, cw = os.pipe()
    srv = lsp.Server(os.fdopen(cr, "rb"), os.fdopen(cw, "wb"), command=command)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    return srv, os.fdopen(sw, "wb"), os.fdopen(sr, "rb"), t


def frame(msg):
    body = json.dumps({"jsonrpc": "2.0", **msg}).encode()
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def test_saves_are_debounced_into_one_run(tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    script = tmp_path / "fake.py"
    script.write_text("import os, sys, uuid\nopen(os.path.join(sys.argv[1], uuid.uuid4().hex), 'w').close()\nprint('[]')\n")
    srv, w, r, t = serve_in_thread(lambda root: [sys.executable, str(script), str(marks)])
    try:
        w.write(frame({"id": 1, "method": "initialize", "params": {"rootUri": lsp.path_to_uri(str(tmp_path)),
                                                                     "initializationOptions": {"debounceMs": 300}}}))
        for _ in range(5):
            w.write(frame({"method": "textDocument/didSave", "params": {"textDocument": {"uri": lsp.path_to_uri(str(tmp_path / "a"))}}}))
        w.flush()
        deadline = time.time() + TIMEOUT
        while time.time() < deadline and not (srv._ran and srv.wait_idle(0.1)):  # scheduling is on the reader thread
            time.sleep(0.05)
        assert len(list(marks.iterdir())) == 1
    finally:
        w.write(frame({"method": "exit"}))
        w.close()
        t.join(20)
        r.close()


def test_a_newer_request_kills_the_run_in_flight(tmp_path):
    started = tmp_path / "started"
    script = tmp_path / "slow.py"
    script.write_text("import sys, time\nopen(sys.argv[1], 'a').write('x')\ntime.sleep(30)\nprint('[]')\n")
    srv = lsp.Server(io.BytesIO(), io.BytesIO(), command=lambda root: [sys.executable, str(script), str(started)])
    srv.root = str(tmp_path)
    srv.debounce = 0
    srv.schedule()
    deadline = time.time() + TIMEOUT
    while time.time() < deadline and not (started.exists() and srv._proc is not None):
        time.sleep(0.05)
    first = srv._proc
    assert first is not None and first.poll() is None
    srv.schedule()
    assert first.wait(timeout=20) != 0  # killed, not finished
    srv.cancel_all()
    assert srv.wait_idle(10)


def test_a_failing_scan_is_logged_not_published(tmp_path):
    out = io.BytesIO()
    srv = lsp.Server(io.BytesIO(), out, command=lambda root: [sys.executable, "-c", "import sys; sys.exit(2)"])
    srv.root = str(tmp_path)
    srv.debounce = 0
    srv.schedule()
    assert srv.wait_idle(TIMEOUT)
    sent = out.getvalue().decode()
    assert "window/logMessage" in sent and "scan failed" in sent and "publishDiagnostics" not in sent


# ---------------------------------------------------------------- what the server runs is never chosen by the workspace
def test_default_command_is_the_checkers_own_interpreter_and_names_no_workspace_program(tmp_path):
    argv = lsp.default_command(str(tmp_path))
    assert argv[:4] == [sys.executable, "-P", "-m", "dead_code_checker"]
    assert argv[4] == "scan" and set(argv[5:]) == {str(tmp_path), "--root", "--json"}


def test_initialization_options_cannot_change_what_runs(tmp_path):
    srv = lsp.Server(io.BytesIO(), io.BytesIO())
    before = srv.command
    srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "rootUri": lsp.path_to_uri(str(tmp_path)),
        "initializationOptions": {"command": ["touch", "x"], "path": "/bin/sh", "args": ["-c", "x"], "env": {"A": "b"}, "debounceMs": 5}}})
    assert srv.command is before and srv.command is lsp.default_command
    assert srv.debounce == 0.005  # the one option it knows still works


def test_a_repository_config_that_names_a_program_runs_nothing(client, repo):
    marker = repo / "planted-ran"
    (repo / ".dead-code-checker.toml").write_text(
        f'command = "touch {marker}"\npath = "{marker}"\n[graph]\ncommand = ["touch", "{marker}"]\nexe = "touch"\nargs = ["{marker}"]\n')
    init(client, repo)
    uri = lsp.path_to_uri(str(repo / "lib" / "x_mod.py"))
    client.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": ""}})
    client.diagnostics_for(uri)  # the scan finished (with or without findings), and nothing was started by name
    assert not marker.exists()


# ---------------------------------------------------------------- pure helpers
def test_parse_findings_shapes():
    assert lsp.parse_findings("") == []
    assert lsp.parse_findings("  \n") == []
    one = {"level": "W", "path": "a.py", "line": 1, "code": "deadcode.x", "msg": "m"}
    assert lsp.parse_findings(json.dumps([one])) == [one]
    assert lsp.parse_findings(json.dumps({"findings": [one]})) == [one]
    assert lsp.parse_findings(json.dumps([one, "junk", {"nopath": 1}])) == [one]
    with pytest.raises(ValueError):
        lsp.parse_findings("not json")
    with pytest.raises(ValueError):
        lsp.parse_findings("3")


def test_to_diagnostics_maps_level_and_file_level_findings(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    fs = [
        {"level": "E", "path": "a.py", "line": 1, "code": "deadcode.stub-hook", "msg": "bad"},
        {"level": "W", "path": "a.py", "line": 0, "code": "deadcode.future-code", "msg": "no line"},
        {"level": "I", "path": "a.py", "line": 99, "code": "deadcode.x", "msg": "past the end"},
    ]
    ds = lsp.to_diagnostics(fs, str(tmp_path))[lsp.path_to_uri(str((tmp_path / "a.py").resolve()))]
    assert [d["severity"] for d in ds] == [1, 2, 3]
    assert ds[1]["range"]["start"]["line"] == 0 and ds[1]["message"] == "no line"  # unknown code: no why
    assert ds[2]["range"]["end"]["character"] == 0
    assert ds[0]["message"].startswith("bad\n")


def test_to_diagnostics_drops_paths_outside_the_root(tmp_path):
    fs = [{"level": "W", "path": "../escape.py", "line": 1, "code": "deadcode.x", "msg": "m"},
          {"level": "W", "path": "/etc/passwd", "line": 1, "code": "deadcode.x", "msg": "m"}]
    assert lsp.to_diagnostics(fs, str(tmp_path)) == {}


def test_uri_round_trip_with_spaces(tmp_path):
    p = str(tmp_path / "a b" / "c.py")
    assert lsp.uri_to_path(lsp.path_to_uri(p)) == p
    with pytest.raises(ValueError):
        lsp.uri_to_path("http://example.com/x")


def test_find_root_prefers_the_git_root(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert lsp.find_root(str(tmp_path / "a" / "b" / "f.py")) == str(tmp_path.resolve())


def test_every_why_code_is_a_documented_finding():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    for code in lsp.WHY:
        assert code in readme, code


def test_protocol_framing_round_trip():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "m", "params": {"t": "да"}}
    srv = lsp.Server(io.BytesIO(frame(msg)), io.BytesIO())
    assert srv.read_message() == msg
    assert srv.read_message() is None
    out = io.BytesIO()
    lsp.Server(io.BytesIO(), out).send({"id": 1, "result": None})
    head, _, body = out.getvalue().partition(b"\r\n\r\n")
    assert head == b"Content-Length: %d" % len(body) and json.loads(body)["id"] == 1
