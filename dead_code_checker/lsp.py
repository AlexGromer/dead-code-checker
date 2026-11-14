"""A minimal Language Server for dead-code-checker: JSON-RPC over stdio, standard library only.

The server runs `dead-code-checker scan ROOT --json` on the workspace and publishes one diagnostic per finding.
It never reads the editor buffer: findings describe the files on disk, so a scan is started on save (and once on the
first open), not on every keystroke. Runs are debounced, and a run that is still going when a newer one is wanted is
killed. Start it with `dead-code-checker lsp`.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from . import safepath

SOURCE = "dead-code-checker"
DEFAULT_DEBOUNCE_MS = 400
SCAN_TIMEOUT_S = 600

# LSP DiagnosticSeverity
ERROR, WARNING, INFORMATION = 1, 2, 3

# one sentence on what to do, keyed by the code without its `deadcode.` prefix
WHY = {
    "unreferenced-symbol": "Nothing else in the repository mentions this name. Remove it, or use it; "
    "`deadcode: ignore` on the line marks a dynamic use.",
    "unreferenced-public-symbol": "An exported symbol nothing in the tree references; it may be API for other "
    "projects. See --public-api.",
    "test-only-symbol": "Only tests use this symbol; production code never calls it.",
    "unreferenced-file": "No import, registry, document, test or unit file mentions this file.",
    "stub-function": "The body does nothing (pass, ..., return None, NotImplementedError).",
    "todo-stub": "The body is only a TODO.",
    "stub-hook": "A registered hook ignores its input and prints a constant.",
    "config-key-unread": "No code reads this config key.",
    "env-unread": "No code reads this environment variable.",
    "cli-flag-unused": "The flag is defined and never read.",
    "bad-config": "Fix .dead-code-checker.toml: an unknown key or a bad value.",
}


def severity_of(level: str) -> int:
    return ERROR if level == "E" else WARNING if level == "W" else INFORMATION


def why_of(code: str) -> str:
    return WHY.get(code.removeprefix("deadcode."), "")


def uri_to_path(uri: str) -> str:
    u = urlparse(uri)
    if u.scheme != "file":
        raise ValueError(f"not a file URI: {uri}")
    return url2pathname(unquote(u.path))


def path_to_uri(path: str) -> str:
    return Path(path).as_uri()


def find_root(start: str) -> str:
    """The nearest directory at or above `start` holding a .git; `start` itself (or its directory) when none does."""
    p = Path(start).resolve()
    here = p if p.is_dir() else p.parent
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return str(d)
    return str(here)


def parse_findings(text: str) -> List[dict]:
    """Findings from the CLI's --json output. Empty output means no findings; a list, or an object with a
    `findings` list, is accepted."""
    text = text.strip()
    if not text:
        return []
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("findings", [])
    if not isinstance(data, list):
        raise ValueError("unexpected JSON shape")
    return [f for f in data if isinstance(f, dict) and isinstance(f.get("path"), str)]


def _line_length(path: str, line: int, cache: Dict[str, List[str]]) -> int:
    if path not in cache:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                cache[path] = fh.read().splitlines()
        except OSError:
            cache[path] = []
    lines = cache[path]
    return len(lines[line - 1].rstrip()) if 0 < line <= len(lines) else 0


def to_diagnostics(findings: Sequence[dict], root: str) -> Dict[str, List[dict]]:
    """{file URI: [LSP Diagnostic]}. A finding whose path leaves `root` is dropped."""
    out: Dict[str, List[dict]] = {}
    cache: Dict[str, List[str]] = {}
    base = os.path.realpath(root)
    for f in findings:
        full = os.path.normpath(os.path.join(base, f["path"]))
        if full != base and not full.startswith(base.rstrip(os.sep) + os.sep):
            continue
        line = f.get("line") if isinstance(f.get("line"), int) else 0
        row = max(line - 1, 0)
        code = str(f.get("code", ""))
        message = str(f.get("msg", ""))
        why = why_of(code)
        out.setdefault(path_to_uri(full), []).append(
            {
                "range": {
                    "start": {"line": row, "character": 0},
                    "end": {"line": row, "character": _line_length(full, line, cache)},
                },
                "severity": severity_of(str(f.get("level", ""))),
                "code": code,
                "source": SOURCE,
                "message": f"{message}\n{why}" if why else message,
            }
        )
    return out


def default_command(root: str) -> List[str]:
    return [sys.executable, "-P", "-m", "dead_code_checker", "scan", root, "--root", root, "--json"]


def default_env() -> Dict[str, str]:
    """-P keeps the scanned directory off sys.path; the package itself stays importable when run from a source tree."""
    env = dict(os.environ)
    env["PATH"] = safepath.safe_path()  # the scanner is started with the absolute entries of PATH only
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


class Server:
    """One LSP session. `command` maps a root to the argv that prints findings as JSON (replaceable in tests)."""

    def __init__(self, rfile, wfile, command: Callable[[str], List[str]] = default_command):
        self.rfile, self.wfile, self.command = rfile, wfile, command
        self.root: Optional[str] = None
        self.debounce = DEFAULT_DEBOUNCE_MS / 1000
        self.shutdown_requested = False
        self.exit_code: Optional[int] = None
        self._wlock = threading.Lock()
        self._lock = threading.Lock()  # guards _gen, _timer, _proc, _published, _ran
        self._gen = 0
        self._timer: Optional[threading.Timer] = None
        self._proc: Optional[subprocess.Popen] = None
        self._published: set = set()
        self._ran = False
        self._idle = threading.Event()
        self._idle.set()

    # ------------------------------------------------------------ transport
    def read_message(self) -> Optional[dict]:
        length = None
        while True:
            line = self.rfile.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                if length is None:
                    continue
                break
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())
        body = self.rfile.read(length)
        if len(body) < length:
            return None
        return json.loads(body.decode("utf-8"))

    def send(self, msg: dict) -> None:
        body = json.dumps({"jsonrpc": "2.0", **msg}, ensure_ascii=False).encode("utf-8")
        with self._wlock:
            self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            self.wfile.flush()

    def log(self, text: str, level: int = 4) -> None:
        self.send({"method": "window/logMessage", "params": {"type": level, "message": text}})

    # ------------------------------------------------------------ main loop
    def serve(self) -> int:
        while self.exit_code is None:
            try:
                msg = self.read_message()
            except (ValueError, json.JSONDecodeError):
                self.send({"id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if msg is None:  # the client went away
                self.cancel_all()
                return 0 if self.shutdown_requested else 1
            self.handle(msg)
        self.cancel_all()
        return self.exit_code

    def handle(self, msg: dict) -> None:
        method, mid = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}
        if method is None:  # a response to a request of ours: none are sent
            return
        if method == "exit":
            self.exit_code = 0 if self.shutdown_requested else 1
            return
        if method == "initialize":
            self.send({"id": mid, "result": self.initialize(params)})
        elif method == "shutdown":
            self.shutdown_requested = True
            self.cancel_all()
            self.send({"id": mid, "result": None})
        elif method == "initialized":
            pass
        elif method == "textDocument/didOpen":
            self.on_open(params)
        elif method == "textDocument/didSave":
            self.on_save(params)
        elif mid is not None:
            code = -32600 if self.shutdown_requested else -32601
            self.send({"id": mid, "error": {"code": code, "message": f"unsupported: {method}"}})
        # any other notification ($/cancelRequest, didChange, didClose, ...) is ignored

    def initialize(self, params: dict) -> dict:
        folders = params.get("workspaceFolders") or []
        uri = (folders[0].get("uri") if folders else None) or params.get("rootUri")
        try:
            if uri:
                self.root = uri_to_path(uri)
            elif params.get("rootPath"):
                self.root = params["rootPath"]
        except ValueError:
            self.root = None
        opts = params.get("initializationOptions") or {}
        ms = opts.get("debounceMs")
        if isinstance(ms, (int, float)) and not isinstance(ms, bool) and 0 <= ms <= 60000:
            self.debounce = ms / 1000
        return {
            "capabilities": {"textDocumentSync": {"openClose": True, "change": 0, "save": {"includeText": False}}},
            "serverInfo": {"name": SOURCE},
        }

    # ------------------------------------------------------------ triggers
    def _doc_path(self, params: dict) -> Optional[str]:
        try:
            return uri_to_path(params["textDocument"]["uri"])
        except (KeyError, TypeError, ValueError):
            return None

    def _ensure_root(self, doc: Optional[str]) -> bool:
        if self.root is None and doc:
            self.root = find_root(doc)
        return self.root is not None

    def on_open(self, params: dict) -> None:
        if self._ensure_root(self._doc_path(params)) and not self._ran:
            self.schedule()

    def on_save(self, params: dict) -> None:
        if self._ensure_root(self._doc_path(params)):
            self.schedule()

    # ------------------------------------------------------------ runs
    def schedule(self) -> None:
        """Debounce: the newest request wins; a run in flight is killed."""
        with self._lock:
            self._gen += 1
            gen = self._gen
            if self._timer:
                self._timer.cancel()
            self._kill_locked()
            self._idle.clear()
            self._ran = True
            self._timer = threading.Timer(self.debounce, self._run, args=(gen,))
            self._timer.daemon = True
            self._timer.start()

    def _kill_locked(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()

    def cancel_all(self) -> None:
        with self._lock:
            self._gen += 1
            if self._timer:
                self._timer.cancel()
            self._kill_locked()
        self._idle.set()

    def wait_idle(self, timeout: float) -> bool:
        return self._idle.wait(timeout)

    def _run(self, gen: int) -> None:
        root = self.root
        try:
            with self._lock:
                if gen != self._gen:
                    return
                proc = subprocess.Popen(
                    self.command(root), cwd=root, env=default_env(), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )  # fmt: skip
                self._proc = proc
            try:
                out, err = proc.communicate(timeout=SCAN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                self.log(f"scan timed out after {SCAN_TIMEOUT_S}s", 1)
                return
            with self._lock:
                if gen != self._gen:  # killed or superseded: its output is stale
                    return
            if proc.returncode not in (0, 1):
                self.log(f"scan failed (exit {proc.returncode}): {err.decode('utf-8', 'replace')[-500:]}", 1)
                return
            self.publish(to_diagnostics(parse_findings(out.decode("utf-8", "replace")), root))
        except (OSError, ValueError) as e:
            self.log(f"scan could not run: {e}", 1)
        finally:
            with self._lock:
                if gen == self._gen:
                    self._idle.set()

    def publish(self, by_uri: Dict[str, List[dict]]) -> None:
        with self._lock:
            stale = self._published - set(by_uri)
            self._published = set(by_uri)
        for uri in sorted(stale):
            self.send({"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": []}})
        for uri in sorted(by_uri):
            self.send({"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": by_uri[uri]}})


def serve() -> int:
    """Entry point of `dead-code-checker lsp`: stdio, until `exit` or EOF."""
    return Server(sys.stdin.buffer, sys.stdout.buffer).serve()
