"""The python tracer of the runtime-signal mode (graph.rtbox puts this directory on PYTHONPATH inside the box of the
execution target). Stdlib only. It is loaded by the interpreter itself (`sitecustomize`), so every python process of the traced command is
covered, children included, and it records ONE fact: which functions of the project were called.

    DCRT_OUT=<dir>   where the process writes <pid>.json  {"v": 1, "fn": [[path, qualname, first line], ...]}  (written again, atomically,
                     whenever something new was seen, every 2 s and at exit; a process that is killed loses at most 2 s)
    DCRT_ROOT=<dir>  the project copy: only code objects whose file lies below it count; the path is stored relative to it

A function is recorded by its first call (sys.monitoring PY_START on 3.12+, which then switches the event off for that code object, so
a hot function costs nothing after its first call; sys.setprofile before 3.12). Code that is no function (module bodies and class
bodies: they run when a file is imported, which proves nothing about the symbols in it) is not recorded; a lambda, a comprehension
and a nested function are, and the reader matches them to the symbol that contains their first line.

Without both variables, or with a relative DCRT_OUT, it does nothing. It never raises into the traced program. `python -I`, `-S`
and `-E` ignore PYTHONPATH, so a process started like that is not traced (a gap in coverage, never a false hit).
"""

import atexit
import json
import os
import sys
import threading

CO_OPTIMIZED = 0x1  # set for a function, a lambda, a generator and a comprehension; not for a module or a class body


def _setup():
    out, root = os.environ.get("DCRT_OUT", ""), os.environ.get("DCRT_ROOT", "")
    if not (os.path.isabs(out) and os.path.isabs(root) and os.path.isdir(out)):
        return
    root = os.path.realpath(root).rstrip(os.sep) + os.sep
    rels = {}  # file name -> path relative to the project, or None
    hits = {}  # (path, qualname, first line) -> True
    state = {"dirty": False, "stop": threading.Event()}

    def rel_of(name):
        if name not in rels:
            real = os.path.realpath(name) if name and name[:1] != "<" else ""
            rels[name] = real[len(root) :].replace(os.sep, "/") if real.startswith(root) else None
        return rels[name]

    def note(code):
        if not code.co_flags & CO_OPTIMIZED:
            return
        rel = rel_of(code.co_filename)
        if rel is None:
            return
        key = (rel, getattr(code, "co_qualname", code.co_name), code.co_firstlineno)
        if key not in hits:
            hits[key] = True
            state["dirty"] = True

    def flush():
        if not state["dirty"]:
            return
        state["dirty"] = False
        pid = os.getpid()
        tmp, final = os.path.join(out, f".{pid}.tmp"), os.path.join(out, f"{pid}.json")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"v": 1, "fn": [list(k) for k in sorted(hits)]}, fh)
            os.replace(tmp, final)
        except OSError:
            state["dirty"] = True

    def loop():
        while not state["stop"].wait(2.0):
            flush()

    def start_thread():
        state["stop"] = threading.Event()
        threading.Thread(target=loop, name="dc-runtime-flush", daemon=True).start()

    mon = getattr(sys, "monitoring", None)
    if mon is not None:
        tool = next((t for t in (4, 3) if _use(mon, t)), None)
        if tool is None:
            return

        def on_start(code, offset):
            note(code)
            return mon.DISABLE

        mon.register_callback(tool, mon.events.PY_START, on_start)
        mon.set_events(tool, mon.events.PY_START)
    else:
        seen = set()

        def prof(frame, event, arg):
            if event == "call" and frame.f_code not in seen:
                seen.add(frame.f_code)
                note(frame.f_code)

        sys.setprofile(prof)
        threading.setprofile(prof)
    atexit.register(flush)
    start_thread()
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=start_thread)


def _use(mon, tool):
    try:
        mon.use_tool_id(tool, "dc-runtime")
        return True
    except ValueError:  # taken by a debugger, a profiler or coverage.py
        return False


try:
    _setup()
except Exception:  # noqa: BLE001 - a tracer never breaks the program it watches
    pass
