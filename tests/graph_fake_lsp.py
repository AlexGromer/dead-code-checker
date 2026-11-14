"""A scripted language server for the tests of graph/sem.py (stdio JSON-RPC, Content-Length framing, stdlib only).

    python3 dc_fake_lsp.py script.json          the working directory is the workspace root (the client starts it there)

script.json (all keys optional)
    caps          the server capabilities of the initialize answer (default: references only)
    progress      "finish" (create a progress token, begin, end: indexing announced and done; the default), "none" (never
                  announce anything), "stuck" (begin and never end)
    refs, calls   {"<path>:<line>:<character>": [[path, line], ...]}: the answer of textDocument/references, and of
                  prepareCallHierarchy + incomingCalls, for a position; a path that starts with "/" is used as it is
                  (outside the workspace), any other is under the workspace root; an unknown position gets []
    hang          true: textDocument/references is never answered
    errors        {"<method>": "message"}: that request is answered with a JSON-RPC error
    raw           {"<method>": value}: that request is answered with this JSON value as its result, whatever its shape
    frames        [text, ...]: sent as they are, as frames, right after `initialized` (malformed messages)
    ignore_shutdown  true: shutdown and exit are not answered (the server stays up until it is killed)
    child         file: the server starts a child process (same process group) and writes its pid to the file
    flood         N: after `initialized` the server sends N notifications of 10 kB each
    sleep_after_init  true: after `initialized` the server sleeps and reads nothing more
    crash_after   exit(3) right after answering this many queries
    log           file: one JSON line per message received ({"m": method, "id": id}) and per reply the client gave to a
                  request of the server ({"reply_to": method, "result": ...})
"""

import json
import os
import sys
import time
from urllib.parse import unquote, urlparse

SCRIPT = json.load(open(sys.argv[1]))
ROOT = os.getcwd()
LOG = SCRIPT.get("log")
CAPS = SCRIPT.get("caps", {"referencesProvider": True})
OUT = sys.stdout.buffer
IN = sys.stdin.buffer
asked = {}  # id of a request of ours -> its method


def log(row):
    if LOG:
        with open(LOG, "a") as fh:
            fh.write(json.dumps(row) + "\n")


def send(msg):
    body = json.dumps({"jsonrpc": "2.0", **msg}).encode()
    OUT.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    OUT.flush()


def send_raw(text):
    body = text.encode()
    OUT.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    OUT.flush()


def read():
    n = None
    for line in iter(IN.readline, b"\r\n"):
        if not line:
            return None
        k, _, v = line.partition(b":")
        if k.strip().lower() == b"content-length":
            n = int(v)
    return json.loads(IN.read(n))


def uri(path):
    return "file://" + (path if path.startswith("/") else os.path.join(ROOT, path))


def location(path, line):
    return {"uri": uri(path), "range": {"start": {"line": line, "character": 0}, "end": {"line": line, "character": 1}}}


def caller(path, line):
    loc = location(path, line)
    return {"from": {"uri": loc["uri"], "range": loc["range"]}, "fromRanges": [loc["range"]]}


def key(params):
    rel = os.path.relpath(unquote(urlparse(params["textDocument"]["uri"]).path), ROOT)
    return f"{rel}:{params['position']['line']}:{params['position']['character']}"


def announce():
    mode = SCRIPT.get("progress", "finish")
    if mode == "none":
        return
    asked[900] = "window/workDoneProgress/create"
    send({"id": 900, "method": "window/workDoneProgress/create", "params": {"token": "idx"}})
    asked[901] = "workspace/configuration"
    send({"id": 901, "method": "workspace/configuration", "params": {"items": [{"section": "a"}, {"section": "b"}]}})
    send({"method": "$/progress", "params": {"token": "idx", "value": {"kind": "begin", "title": "indexing"}}})
    if mode == "finish":
        send({"method": "$/progress", "params": {"token": "idx", "value": {"kind": "end"}}})


def main():
    queries = 0
    if SCRIPT.get("child"):
        import subprocess

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        open(SCRIPT["child"], "w").write(str(child.pid))
    while True:
        msg = read()
        if msg is None:
            return
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None:  # the client's answer to a request of ours
            log({"reply_to": asked.get(mid), "result": msg.get("result")})
            continue
        log({"m": method, "id": mid, "opts": params.get("initializationOptions")})
        if method in SCRIPT.get("errors", {}):
            send({"id": mid, "error": {"code": -32603, "message": SCRIPT["errors"][method]}})
        elif method in SCRIPT.get("raw", {}):
            send({"id": mid, "result": SCRIPT["raw"][method]})
        elif method in ("shutdown", "exit") and SCRIPT.get("ignore_shutdown"):
            continue
        elif method == "initialize":
            send({"id": mid, "result": {"capabilities": CAPS, "serverInfo": {"name": "fake-lsp"}}})
        elif method == "initialized":
            announce()
            for frame in SCRIPT.get("frames", []):
                send_raw(frame)
            for _ in range(SCRIPT.get("flood", 0)):
                send({"method": "window/logMessage", "params": {"type": 4, "message": "x" * 10_000}})
            if SCRIPT.get("sleep_after_init"):
                time.sleep(120)
        elif method == "shutdown":
            send({"id": mid, "result": None})
        elif method == "exit":
            return
        elif method == "textDocument/references":
            if SCRIPT.get("hang"):
                continue
            send({"id": mid, "result": [location(p, ln) for p, ln in SCRIPT.get("refs", {}).get(key(params), [])]})
            queries += 1
        elif method == "textDocument/prepareCallHierarchy":
            item = {"name": "x", "kind": 12, "uri": params["textDocument"]["uri"], "range": {}, "selectionRange": {}}
            item["data"] = key(params)
            send({"id": mid, "result": [item]})
        elif method == "callHierarchy/incomingCalls":
            calls = SCRIPT.get("calls", {}).get(params["item"]["data"], [])
            send({"id": mid, "result": [caller(p, ln) for p, ln in calls]})
            queries += 1
        elif mid is not None:
            send({"id": mid, "result": None})
        if SCRIPT.get("crash_after") is not None and queries >= SCRIPT["crash_after"]:
            os._exit(3)


main()
