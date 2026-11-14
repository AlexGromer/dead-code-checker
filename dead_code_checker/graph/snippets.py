"""graph.snippets: the code of the symbols that the viewer page carries.

    collect(root, graph, res, tree, marks, suspects, cfg) -> Snips   text per node key, the page meta, the findings
    find_gitleaks() -> path | None                                  the gitleaks binary: $DEAD_CODE_CHECKER_GITLEAKS, else the system directories,
                                                                    else PATH

A snippet is the body of a symbol (line0..line1) cut to MAX_LINES lines (the last one then says how many lines are left) and every
line to MAX_LINE characters. The text of a file is the one the graph was built from: `tree` (vcs.Tree of the build) says which blob
each file had; a clean file comes from the object store, the others from the work tree (vcs.read_files), and a file whose blob id is
not the one of the tree (changed or gone since the build) is stale: its symbols have no code and the meta counts it. What goes into
a page is decided in this order:
  1. [graph] viewer_code = false: no snippets, nothing is read (the page says "off"); no finding is raised, it was chosen
  2. the byte budget ([graph] viewer_code_bytes, default 6 MB of snippet text): candidates in the priority dead / suspect >
     changed (a mark) > roots > the rest, in node order inside a class; one that does not fit is left out, the state says "budget"
  3. ONE gitleaks run (`gitleaks dir` of the installed binary with the default rules, no config of the repository, `gitleaks:allow`
     comments ignored) over the WHOLE files that hold a chosen snippet, one file each under a neutral name: a finding that touches
     the lines a snippet can show (its first MAX_LINES lines, or the whole body when shorter) replaces the snippet by HIDDEN, also
     when the secret goes on below the cut (a key of 60 lines). No gitleaks, or a run that fails or times out: NO snippets at all and
     W dc.viewer-code-off with the reason and a note on how to turn the fragments on (fail closed: code is never embedded unscanned).
  4. the evidence texts of the edges (graph.evid snippets, a cut of the source line, <= 120 bytes) go through the SAME gitleaks run
     (several to a file, one per line): Snips.ev_ok = the texts that came out clean; the page carries no other one (a hit hides it;
     when no scan ran, ev_ok is empty: no evidence text at all).
`deadline` (a time.monotonic() value; the caller's budget, the caller's): nothing is started after it, the
reading stops between chunks, the gitleaks run gets min(SCAN_TIMEOUT, what is left) and fewer than MIN_LEFT seconds left means no
scan at all: the page then has no code, W dc.viewer-code-off with the reason "budget". Without a deadline the old limits stand.
Files are read in chunks of CHUNK (one git batch each) and cut one thread per file (par.run_units, kind thread); a file that is
not text (a NUL in its head) or bigger than MAX_FILE has no snippets. A page that carries code is local: Snips.info["local"] says so.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from typing import (
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)


from .. import safepath
from . import par, safety, vcs
from .model import Finding

HIDDEN = "hidden: possible secret"
LOCAL = "local page: contains source code, do not publish"
MAX_LINES, MAX_LINE, MAX_FILE, CHUNK = 40, 240, 2 * 1024 * 1024, 256
DEFAULT_BYTES = 6 * 1024 * 1024
SAFE_DIRS = ("/usr/bin", "/usr/local/bin", "/bin")
ENV_GITLEAKS = "DEAD_CODE_CHECKER_GITLEAKS"
NOTE = "install gitleaks (https://github.com/gitleaks/gitleaks) to put code fragments into the pages; without it the pages show names only"
LEAK_EXIT = 99
SCAN_TIMEOUT = 120
MIN_LEFT = 2.0  # seconds of the caller's budget below which no gitleaks run is started
EV_CHUNK = 2000  # evidence texts per scanned file
SKIP_KINDS = ("file", "module")
DEAD = ("dead", "dead-cluster")


class OutOfBudget(RuntimeError):
    """The deadline of the caller passed: no code on the page (reason "budget")."""


class Snips(NamedTuple):
    text: Dict[str, str]  # node key -> snippet text (HIDDEN for one with a finding)
    info: dict  # the page meta "code": state on|off|budget, n, hidden, bytes, budget, reason
    findings: Tuple[Finding, ...]
    ev_ok: FrozenSet[str] = (
        frozenset()
    )  # the edge evidence texts that were scanned clean: no other one goes into a page


def _left(deadline: Optional[float]) -> Optional[float]:
    return None if deadline is None else deadline - time.monotonic()


def _check(deadline: Optional[float]) -> None:
    left = _left(deadline)
    if left is not None and left <= 0:
        raise OutOfBudget("budget")


def off(reason: str, finding: bool = True) -> Snips:
    fs = (
        (
            Finding(
                "W",
                safety.WHERE,
                0,
                "dc.viewer-code-off",
                f"the viewer page carries no code: {reason}; {NOTE}",
                "snippets are embedded only after a gitleaks scan; without one there are none",
            ),
        )
        if finding
        else ()
    )
    return Snips({}, {"state": "off", "reason": reason, "n": 0, "hidden": 0, "bytes": 0}, fs)


def find_gitleaks() -> Optional[str]:
    """The gitleaks binary: $DEAD_CODE_CHECKER_GITLEAKS (an absolute path to an executable), else the system directories, else
    the first one on PATH. None when there is none: the pages then carry no code."""
    exe = "gitleaks.exe" if os.name == "nt" else "gitleaks"
    own = (os.environ.get(ENV_GITLEAKS) or "").strip()
    if own:
        return own if os.path.isabs(own) and os.path.isfile(own) and os.access(own, os.X_OK) else None
    for d in SAFE_DIRS:
        p = os.path.join(d, exe)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return safepath.which(exe) or None


def _clean_env() -> Dict[str, str]:
    """No GITLEAKS_CONFIG*, LD_*, PYTHON* and no PATH of the user: a minimal environment, so that no configuration of the
    surroundings can allow a finding away."""
    home = os.path.expanduser("~")
    return {
        "PATH": os.pathsep.join(SAFE_DIRS),
        "HOME": home,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def cut(lines: Sequence[str], l0: int, l1: int) -> str:
    """Lines l0..l1 (1-based, inclusive) as one text of at most MAX_LINES lines; '' when the range is not in the file."""
    if l0 < 1 or l1 < l0 or l0 > len(lines):
        return ""
    body = [x[:MAX_LINE] + ("…" if len(x) > MAX_LINE else "") for x in lines[l0 - 1 : l1]]
    if len(body) > MAX_LINES:
        body = body[: MAX_LINES - 1] + [f"... ({len(body) - MAX_LINES + 1} more lines)"]
    return "\n".join(body)


def text_lines(data: bytes) -> Optional[List[str]]:
    """The lines of a text file (CRLF as LF), or None when it is bigger than MAX_FILE or binary (a NUL in its first 8 KB)."""
    if len(data) > MAX_FILE or b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")


def read(root: str, tree, paths: Iterable[str]) -> Iterator[Tuple[Dict[str, List[str]], Set[str]]]:
    """Chunks of ({path: lines}, {stale paths}) for the files of `paths` that `tree` knows, as the graph saw them: the content of
    vcs.read_files whose blob id is the one of the tree. A file that is gone or has another id since the build is stale; a file that
    is not text is in neither (it has no snippets)."""
    paths = [p for p in sorted(set(paths)) if p in tree.files]
    for k in range(0, len(paths), CHUNK):
        got, ok, stale = vcs.read_files(root, tree, paths[k : k + CHUNK]), {}, set()
        for p in paths[k : k + CHUNK]:
            want, data = tree.files[p].blob, got.get(p)
            if data is None or vcs.blob_sha(data, "sha256" if len(want) == 64 else "sha1") != want:
                stale.add(p)
            else:
                lines = text_lines(data)
                if lines is not None:
                    ok[p] = lines
        yield ok, stale


def _unit(arg: Tuple[Tuple[Tuple[int, int, int], ...], List[str]]) -> List[Tuple[int, str]]:
    wants, lines = arg
    return [(i, t) for i, l0, l1 in wants for t in [cut(lines, l0, l1)] if t]


def _classes(graph, res, marks, suspects: Iterable[str]) -> List[List[int]]:
    """Node indexes with a body, in the priority classes dead/suspect, changed, roots, rest (node order inside a class)."""
    p, vs = res.prep, res.verdicts()
    sus, rooted = set(suspects), {r.node for r in graph.roots if r.node}
    out: List[List[int]] = [[], [], [], []]
    for i, n in enumerate(graph.nodes):
        if n.kind in SKIP_KINDS or n.line0 < 1 or n.line1 < n.line0:
            continue
        k = p.keys[i]
        out[(0 if vs[k] in DEAD or k in sus else 1 if (marks or {}).get(k) else 2 if k in rooted else 3)].append(i)
    return out


def scan(
    texts: Iterable[str], exe: str, timeout: Optional[float] = None, deadline: Optional[float] = None
) -> List[Tuple[int, int, int]]:
    """ONE gitleaks run over the texts, each written (as it comes) to a file of its own under a neutral name, so that no path rule of
    the default config can allow it. Every finding as (index of the text, first line, last line); nothing is run for no text.
    RuntimeError when the run fails or a leak cannot be placed; OutOfBudget when `deadline` passes before or during the run.
    """
    timeout = SCAN_TIMEOUT if timeout is None else timeout
    with tempfile.TemporaryDirectory(prefix="dc-snip-") as td:
        src, rep = os.path.join(td, "src"), os.path.join(td, "report.json")
        os.mkdir(src)
        n = 0
        for n, t in enumerate(texts, 1):
            _check(deadline)
            with open(os.path.join(src, f"f{n - 1}"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(t)
        if not n:
            return []
        left = _left(deadline)
        capped = left is not None and left < timeout
        if capped:
            if left < MIN_LEFT:
                raise OutOfBudget("budget")
            timeout = left
        cmd = [
            exe,
            "dir",
            src,
            "--no-banner",
            "--no-color",
            "--redact",
            "-l",
            "error",
            "-f",
            "json",
            "-r",
            rep,
            "--exit-code",
            str(LEAK_EXIT),
            "--ignore-gitleaks-allow",
        ]
        try:
            r = safepath.run(cmd, capture_output=True, timeout=timeout, cwd=td, env=_clean_env())
        except subprocess.TimeoutExpired:
            if capped:
                raise OutOfBudget("budget")
            raise RuntimeError(f"gitleaks timed out after {timeout:g}s")
        except OSError as exc:
            raise RuntimeError(f"gitleaks did not start: {exc}")
        if r.returncode not in (0, LEAK_EXIT):
            raise RuntimeError(
                f"gitleaks failed (exit {r.returncode}): {r.stderr.decode('utf-8', 'replace').strip()[:120]}"
            )
        found = []
        if r.returncode == LEAK_EXIT:
            try:
                with open(rep, encoding="utf-8") as fh:
                    found = json.load(fh)
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"the gitleaks report is unreadable: {exc}")
    hits = []
    for f in found:
        name, a = os.path.basename(str(f.get("File", ""))), int(f.get("StartLine", 0))
        if name[:1] == "f" and name[1:].isdigit() and int(name[1:]) < n:
            hits.append((int(name[1:]), a, int(f.get("EndLine", 0)) or a))
    if r.returncode == LEAK_EXIT and not hits:
        raise RuntimeError("gitleaks reported leaks the scan cannot place")
    return hits


def _evidence(graph) -> List[str]:
    """The distinct non-empty evidence texts of the graph, in a fixed order."""
    return sorted({e.snippet for e in getattr(graph, "evid", ()) if e.snippet})


def _ev_chunks(texts: Sequence[str]) -> Iterator[Tuple[str, List[Tuple[str, int, int]]]]:
    """Chunks of EV_CHUNK evidence texts as (file text, [(text, first line, last line)]): one text after another, newline apart."""
    for k in range(0, len(texts), EV_CHUNK):
        spans, line, parts = [], 1, []
        for t in texts[k : k + EV_CHUNK]:
            n = t.count("\n")
            spans.append((t, line, line + n))
            parts.append(t)
            line += n + 1
        yield "\n".join(parts), spans


def collect(
    root: str,
    graph,
    res,
    tree,
    marks=None,
    suspects: Iterable[str] = (),
    cfg=None,
    exe: Optional[str] = None,
    deadline: Optional[float] = None,
) -> Snips:
    """The snippets of a page of `graph` (see the module docstring). `tree` = the vcs.Tree the graph was built from, `cfg` =
    graph.config.Config (viewer_code, viewer_code_bytes), `deadline` = a time.monotonic() value (see the module docstring).
    """
    if cfg is not None and not cfg.viewer_code:
        return off("[graph] viewer_code = false", finding=False)
    budget = int(cfg.viewer_code_bytes) if cfg is not None else DEFAULT_BYTES
    exe = exe or find_gitleaks()
    if not exe:
        return off("gitleaks is not installed")
    left = _left(deadline)
    if left is not None and left < MIN_LEFT:
        return off("budget")
    try:
        return _collect(root, graph, res, tree, marks, suspects, budget, exe, deadline)
    except OutOfBudget:
        return off("budget")


def _collect(root, graph, res, tree, marks, suspects, budget: int, exe: str, deadline: Optional[float]) -> Snips:
    p = res.prep
    chosen: List[Tuple[int, str]] = []
    stale: Set[str] = set()
    used, skipped = 0, False
    for cls in _classes(graph, res, marks, suspects):
        _check(deadline)
        if used >= budget:
            skipped = skipped or bool(cls)
            continue
        by_file: Dict[str, List[Tuple[int, int, int]]] = {}
        for i in cls:
            n = graph.nodes[i]
            by_file.setdefault(n.path, []).append((i, n.line0, n.line1))
        got: List[List[Tuple[int, str]]] = []
        for ok, bad in read(root, tree, by_file):
            _check(deadline)
            stale |= bad
            got += par.run_units(
                [(tuple(by_file[f]), lines) for f, lines in ok.items()],
                _unit,
                key=lambda u: u[0][0][0],
                kind="thread",
            )
        for i, t in sorted(x for r in got for x in r):
            size = len(t.encode("utf-8"))
            if used + size > budget:
                skipped = True
                continue
            used += size
            chosen.append((i, t))
    labels: List[Tuple[str, object]] = (
        []
    )  # what each scanned text is: ("f", path) or ("e", [(text, first line, last line)])
    evidence = _evidence(graph)

    def files() -> Iterator[str]:
        """The whole text of every file that holds a snippet, read again (nothing is kept in between), then the evidence chunks."""
        for ok, bad in read(root, tree, {graph.nodes[i].path for i, _ in chosen}):
            _check(deadline)
            stale.update(bad)
            for f in sorted(ok):
                labels.append(("f", f))
                yield "\n".join(ok[f])
        for text, spans in _ev_chunks(evidence):
            labels.append(("e", spans))
            yield text

    try:
        found = scan(files(), exe, deadline=deadline)
    except OutOfBudget:
        raise
    except RuntimeError as exc:
        return off(str(exc))
    spans: Dict[str, List[Tuple[int, int]]] = {}
    done = {v for k, v in labels if k == "f"}
    ev_bad: Set[str] = set()
    for k, a, b in found:
        kind, v = labels[k]
        if kind == "f":
            spans.setdefault(v, []).append((a, b))
        else:
            ev_bad.update(t for t, x, y in v if a <= y and x <= b)
    text, hidden = {}, 0
    for i, t in chosen:
        n = graph.nodes[i]
        if n.path in stale or n.path not in done:  # changed between the two reads: no code, never unscanned code
            continue
        top = min(n.line1, n.line0 + MAX_LINES - 1)  # the lines of the body that can reach the page
        hit = any(a <= top and n.line0 <= b for a, b in spans.get(n.path, ()))
        text[p.keys[i]], hidden = (HIDDEN if hit else t), hidden + int(hit)
    info = {
        "state": "budget" if skipped else "on",
        "n": len(text),
        "hidden": hidden,
        "stale": len(stale),
        "bytes": used,
        "budget": budget,
        "local": LOCAL,
        "ev_hidden": len(ev_bad),
    }
    return Snips(text, info, (), frozenset(evidence) - ev_bad)
