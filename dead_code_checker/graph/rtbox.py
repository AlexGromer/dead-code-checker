"""graph.rtbox: the half of the runtime-signal mode that runs ON THE EXECUTION TARGET, the one ssh host the user configured
(`[graph.runtime]`, see graph.rtcfg). ONE stdlib-only file (Python 3.9+): it imports nothing of this package, so the controlling side
ships it inside the delivery and it runs as

    python3 -I rtbox.py --on-target --box <rundir> --sha256 <hex>

from the directory of the delivery, which holds exactly: rtbox.py, runtime.json (the data of graph.rtcfg), tracers/py/sitecustomize.py
(`delivery_files()` names them). `--on-target` is required: the module is the half that runs where project code may run, and a call
without it is refused, so a stray local call never starts a command. The controlling side checks the hashes of the delivery files
before it starts this and never runs it on its own machine.

What it gets, in <rundir> (a private 0700 directory below /tmp, /var/tmp or /dev/shm named rt-<32 hex digits> that the controlling side
made): `in.tar`, a tar of `spec.json` (the run id, the commit, the commands, the timeout, `files`: tracked path -> git blob id) and
`tree/...` (the tracked files at HEAD, nothing else). It
  1. checks the sha256 of in.tar against the one on the command line, the spec against its schema, every member against the file list
     (regular files and confined relative links only, sizes, counts: nothing is extracted outside <rundir>/tree) and every file's
     blob id against its content;
  2. needs a sandbox: a bubblewrap that can build its namespaces, from a root-owned system directory, and prlimit. Without them NOTHING
     runs: the result is status `no-sandbox` and the reason (the controlling side prints it as the one skip line);
  3. runs each command of the spec inside the box: no network (loopback is down too), the file system read-only but the private copy,
     HOME, TMPDIR and the output directory, a fresh /tmp, daemon sockets masked, the environment cleared to PATH, HOME, TMPDIR, LANG and
     what the tracer needs, prlimit from runtime.json (cpu, address space, file size, open files, processes; a language may lift the
     address-space limit and bound its heap by its own env) and the wall clock of the whole box (the process group is killed when it
     ends). The commands run one after the other: they share one writable copy and one wall clock;
  4. puts the tracer in: python: PYTHONPATH=tracers/py (every python process of the command writes its called functions), javascript:
     NODE_V8_COVERAGE, go: `-coverprofile` after `go test` and `go tool cover -func`; another language, or a command that does not suit
     the tracer, is `no tracer for <lang>` with the reason, never a guess;
  4b. a CHECK RUN (a run with a `cwd`: an executing check such as a build or a test of one patched file) is the same box without a tracer:
     the command runs in tree/<cwd> and what comes back of it is its exit code and ONE line, the first error line of its output (cut to
     runtime.json result.note_chars, secret-looking parts masked); the copy of such a delivery is the tracked files at HEAD with the
     patched texts over them;
  5. reads what the tracers wrote as DATA (size caps, schema checks, only paths of the file list), and writes `out/runtime.json` and then
     `out/manifest.json` (run id, commit, sha256 and size of runtime.json): the executed symbols (lang, file, qualname, first line, last
     line, hit count) and, per file, the blob id they were seen in. NOTHING ELSE comes back: no output of the project (but the one error
     line of a check run), no file content.

The readers (read_python, read_v8, read_go) are pure functions of recorded tracer output, so the formats are tested without running
anything. A symbol that is not in the result was not seen running; that is no evidence of death (only a hit is ever used).

The helpers below the "box layer" heading (argv_problem, mask_secrets, first_error, blob ids, link confinement, tool lookup, the
bubblewrap prefix, the process group starter) are small copies of what graph.rtcfg and graph.sandbox/proc/vcs offer: this file cannot
import them on the target, and tests/test_graph_rtbox.py keeps the copies equal to the originals.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tarfile
import time
from urllib.parse import unquote
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

try:
    import pwd
except ImportError:  # not posix: nothing runs there anyway (no bubblewrap)
    pwd = None  # type: ignore[assignment]

HERE = os.path.dirname(os.path.abspath(__file__))
TRACER_DIR = os.path.join(HERE, "tracers", "py")
DELIVERY = ("rtbox.py", "runtime.json", "tracers/py/sitecustomize.py")
SCHEMA = 1
RUN_RE = re.compile(r"^/(?:tmp|var/tmp|dev/shm)/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/rt-[0-9a-f]{32}$")
HEX = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
NAME_RE = re.compile(r"^/(?:[^/\0]+/)*rt-[0-9a-f]{32}$")
RUN_ID = re.compile(r"^[0-9a-f]{32}$")
SPEC_KEYS = ("schema", "run_id", "commit", "target", "runs", "timeout", "files")
RUN_KEYS = ("label", "langs", "argv")
CHECK_KEYS = RUN_KEYS + ("cwd",)  # a check run: no tracer, the command runs in tree/<cwd> ("" = the root)
MAX_RUNS = 8
POST_S = 300  # seconds for a report command (go tool cover) inside what is left of the wall clock
MAX_OUTPUT = 4_000_000
MAX_ARGS, MAX_ARG = 64, 4096
_CACHE: dict = {}


class BoxError(ValueError):
    """The delivery or the spec is not acceptable: nothing ran (exit 2, the message is the last line of stderr)."""


class Run(NamedTuple):
    label: str
    langs: Tuple[str, ...]
    argv: Tuple[str, ...]
    cwd: Optional[str] = None  # None = a traced run; a directory of the tree ("" = root) = a check run


class Spec(NamedTuple):
    run_id: str
    commit: str
    target: str
    runs: Tuple[Run, ...]
    timeout: int
    files: Dict[str, str]


def rt_data() -> dict:
    """runtime.json of the delivery (next to this file), or data/runtime.json of the installed package (cached)."""
    if "d" not in _CACHE:
        for p in (os.path.join(HERE, "runtime.json"), os.path.join(HERE, "data", "runtime.json")):
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    _CACHE["d"] = json.load(fh)
                break
        else:
            raise BoxError("runtime.json is not in the delivery")
    return _CACHE["d"]


def delivery_files() -> Dict[str, str]:
    """The delivery: path inside it -> the file on this machine (the installed package or an unpacked delivery). The controlling side packs
    exactly these, keeps their hashes and starts `python3 -I rtbox.py` from the directory they make."""
    here = os.path.abspath(__file__)
    pkg = os.path.join(HERE, "data", "runtime.json")
    return {
        "rtbox.py": here[:-1] if here.endswith(".pyc") else here,
        "runtime.json": pkg if os.path.isfile(pkg) else os.path.join(HERE, "runtime.json"),
        "tracers/py/sitecustomize.py": os.path.join(TRACER_DIR, "sitecustomize.py"),
    }


# ---------------------------------------------------------------- the box layer (copies, see the module docstring)
def argv_problem(argv) -> str:
    """ "" when `argv` is a command the target may be asked to run (a non-empty list of short strings without NUL or newline)."""
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) and a for a in argv):
        return "the command must be a non-empty list of non-empty strings"
    if len(argv) > MAX_ARGS or any(len(a) > MAX_ARG for a in argv):
        return f"the command has more than {MAX_ARGS} arguments or an argument longer than {MAX_ARG} characters"
    if any("\0" in a or "\n" in a or "\r" in a for a in argv):
        return "the command has a NUL, a newline or a carriage return in an argument"
    return ""


MASK = "***"
_SECRET_HINT = re.compile(
    r"(?i)sk-|AIza|gh[pousr]_|github_pat_|AKIA|glpat-|xox[baprs]-|eyJ|bearer|PRIVATE KEY|://|pass|pwd|secret|token|api[_-]?key|access[_-]?key|authoriz|credential"
)
_SECRET_TOKENS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=\-]{12,}")
_USERINFO = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)")
_KEY_VALUE = re.compile(
    r"(?i)\b([a-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|authorization|credential)[a-z0-9_.-]*)"
    r"(\s*[:=]\s*)(?!\*\*\*)(\"[^\"]*\"|'[^']*'|[^\s,;'\"}]+)"
)


def mask_secrets(text: str) -> str:
    """`text` with the usual secret carriers replaced by ***. Best effort (a filter for what is shown, not a guarantee): the output of a
    check can quote a config line, an environment dump or a URL with a password, and the line goes to the screen, the JSON output and the stored run."""
    text = str(text)
    if not _SECRET_HINT.search(text):
        return text
    for rx in _SECRET_TOKENS:
        text = rx.sub(MASK, text)
    text = _BEARER.sub(lambda m: m.group(1) + MASK, text)
    text = _USERINFO.sub(lambda m: m.group(1) + MASK + m.group(2), text)
    return _KEY_VALUE.sub(lambda m: m.group(1) + m.group(2) + MASK, text)


def first_error(out: str, err: str, strip: str = "", n: int = 300) -> str:
    """The line of a tool's output that says what broke: the first non-empty line that is no header ("# pkg", "FAIL") and carries a
    position or the word error; else the first non-empty one. The directory of the copy (`strip`) is cut off the paths; at most `n` characters.
    """
    lines = [x.strip() for x in (err + "\n" + out).splitlines() if x.strip()]
    if strip:
        lines = [x.replace(strip.rstrip("/") + "/", "") for x in lines]
    good = [x for x in lines if not x.startswith(("#", "FAIL", "ok ", "---", "==="))]
    hit = next((x for x in good if ": " in x or "error" in x.lower()), None) or (good or lines or ["no message"])[0]
    return mask_secrets(hit)[:n]  # masked before the cut: a secret cut in the middle would survive as a prefix



def blob_sha(data: bytes, fmt: str = "sha1") -> str:
    return hashlib.new(fmt, b"blob %d\0" % len(data) + data).hexdigest()


LINK_HOPS = 40  # symlinks followed while one target is resolved (the kernel's own limit is 40): more is a loop


def _link_shape(target: str) -> bool:
    """A link target the delivery may carry: not empty, relative (no NUL, no leading slash or backslash)."""
    return bool(target) and "\0" not in target and not os.path.isabs(target) and not target.startswith(("/", "\\"))


def _inside_tree(path: str, target: str, links: Dict[str, str]) -> bool:
    """True when the symlink `path` -> `target` ends at a place inside the tree. The target is walked component by component from
    the directory of the link and a symlink met on the way (`links`: path -> target, the symlinks that exist) is replaced by its own
    target at that place, so `q/..` climbs from where `q` leads and not from where it stands. `..` above the root, the root itself,
    a bad target on the way and a loop are refused.
    """
    here, todo, hops = path.split("/")[:-1], target.split("/")[::-1], 0
    while todo:
        part = todo.pop()
        if part in ("", "."):
            continue
        if part == "..":
            if not here:
                return False
            here.pop()
            continue
        here.append(part)
        via = links.get("/".join(here))
        if via is not None:
            hops += 1
            if hops > LINK_HOPS or not _link_shape(via):
                return False
            here.pop()
            todo.extend(via.split("/")[::-1])
    return bool(here)


def confine_links(links: Dict[str, str]) -> Dict[str, str]:
    """The symlinks of `links` (path -> target) that stay in the tree, judged against each other: a link that is refused is not there
    for the others, so the rest is judged again until nothing more is refused."""
    cur = {p: t for p, t in sorted(links.items()) if _link_shape(t)}
    while True:
        bad = [p for p, t in cur.items() if not _inside_tree(p, t, cur)]
        if not bad:
            return cur
        cur = {p: t for p, t in cur.items() if p not in bad}


MB = 1 << 20
PRLIMIT = {
    "cpu_s": ("--cpu", 1),
    "as_mb": ("--as", MB),
    "fsize_mb": ("--fsize", MB),
    "nofile": ("--nofile", 1),
    "nproc": ("--nproc", 1),
}
PATH = "/usr/local/bin:/usr/bin:/bin"
SYSTEM_DIRS = ("/usr/bin", "/usr/local/bin", "/bin")  # tools are looked up here and never through PATH
TRUE = "/usr/bin/true"
PROBE_S = 20  # seconds for the bubblewrap probe
UNIX_TABLE = "/proc/net/unix"  # the unix sockets of the host: the last column is the path of a file system one
_FRESH = ("/tmp", "/dev", "/proc")  # directories the box replaces anyway (a fresh tmpfs, devfs, procfs)
_NOT_HOME = ("/", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/var", "/opt", "/sys", "/boot")
_PROBE: Dict[str, str] = {}  # bwrap path -> why it cannot build the namespaces here ("" = it can)


class Tools(NamedTuple):
    prlimit: str  # absolute path, "" = not found in a trusted system directory
    bwrap: str = ""  # absolute path of a trusted bubblewrap that works here, "" = nothing runs
    bwrap_note: str = ""  # why bwrap is not used ("" when it is)


def _trusted(path: str) -> bool:
    """The resolved file and its directory are owned by root and not group or world writable."""
    try:
        real = os.path.realpath(path)
        return all(
            st.st_uid == 0 and not st.st_mode & 0o022
            for st in (os.stat(real), os.stat(os.path.dirname(path)), os.stat(os.path.dirname(real)))
        )
    except OSError:
        return False


def find_tool(name: str, dirs: Sequence[str] = SYSTEM_DIRS) -> str:
    """The absolute path of an executable of a trusted system directory ("" when there is none); PATH is never consulted."""
    for d in dirs:
        c = os.path.join(d, name)
        if os.path.isfile(c) and os.access(c, os.X_OK) and _trusted(c):
            return c
    return ""


def _tail(data: bytes) -> str:
    lines = [x.strip() for x in data.decode("utf-8", "replace").splitlines() if x.strip()]
    return lines[-1][:200] if lines else "no message"


def bwrap_problem(path: str) -> str:
    """Why the bubblewrap at `path` cannot build the namespaces on this host ("" = it can); one probe per path."""
    if path not in _PROBE:
        argv = [path, "--unshare-all", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--", TRUE]
        try:
            r = subprocess.run(argv, capture_output=True, timeout=PROBE_S, stdin=subprocess.DEVNULL, env={"PATH": PATH})
            _PROBE[path] = "" if r.returncode == 0 else _tail(r.stderr)
        except (OSError, subprocess.SubprocessError) as exc:
            _PROBE[path] = str(exc)
    return _PROBE[path]


def detect_tools(dirs: Sequence[str] = SYSTEM_DIRS) -> Tools:
    """prlimit and a working bubblewrap, from trusted system directories only."""
    if os.name != "posix":
        return Tools("", "", "this is not a posix host: there is no bubblewrap")
    prlimit, bwrap = find_tool("prlimit", dirs), find_tool("bwrap", dirs)
    why = bwrap_problem(bwrap) if bwrap else "bwrap is not in a trusted system directory (install bubblewrap)"
    if bwrap and why:
        why = f"bwrap at {bwrap} cannot create namespaces here ({why})"
    return Tools(prlimit, "" if why else bwrap, why)


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def _passwd_home() -> str:
    """The home directory of this uid in the passwd database ("" = a uid without an entry)."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir if pwd is not None else ""
    except KeyError:
        return ""


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:  # gone since the table was read
        return False


def hidden_dirs(environ: Optional[dict] = None) -> List[str]:
    """The directories that the box replaces with an empty tmpfs (after `--ro-bind / /`, before the private binds): /run (the user bus,
    gpg-agent, the docker and podman sockets), /var/run when it is a real directory, the real HOME of this user (passwd and $HOME) and
    $XDG_RUNTIME_DIR. A path that does not exist, that /tmp, /dev or /proc replaces anyway, or that is already inside a hidden one is
    left out."""
    env = os.environ if environ is None else environ
    names = [env.get("HOME") or "", env.get("XDG_RUNTIME_DIR") or "", _passwd_home()]
    out = ["/run"]
    if os.path.isdir("/var/run") and not os.path.islink("/var/run"):
        out.append("/var/run")
    for name in names:
        if not (name and os.path.isabs(name) and os.path.isdir(name)):
            continue
        real = os.path.realpath(name)  # bwrap does not mount onto a symlink
        if real in _NOT_HOME or any(_under(real, d) for d in [*out, *_FRESH]):
            continue
        out = [d for d in out if not _under(d, real)] + [real]  # a parent replaces what it holds
    return sorted(out)


def host_sockets(skip: Sequence[str], table: str = UNIX_TABLE, keep: Sequence[str] = ()) -> List[str]:
    """The file system unix sockets of the host (the path column of /proc/net/unix) outside the `skip` directories, as real paths of
    sockets that exist now: the box covers each with /dev/null, so connect() fails there too. A socket inside a `keep` directory stays
    in even when a `skip` one holds it: the box binds such a directory back from the host, which makes the sockets inside it
    reachable again."""
    try:
        with open(table, encoding="utf-8", errors="replace") as fh:
            rows = fh.read().splitlines()[1:]
    except OSError:
        rows = []
    found = set()
    for row in rows:
        cols = row.split(None, 7)
        if len(cols) < 8 or not cols[7].startswith("/"):  # unnamed, abstract or relative
            continue
        real = os.path.join(os.path.realpath(os.path.dirname(cols[7])), os.path.basename(cols[7]))
        if _is_socket(real) and (any(_under(real, d) for d in keep) or not any(_under(real, d) for d in skip)):
            found.add(real)
    return sorted(found)


def bwrap_prefix(
    bwrap: str, dirs: Dict[str, str], env: dict, ro: Sequence[str], cwd: str, environ: Optional[dict] = None
) -> List[str]:
    """The bubblewrap prefix: nothing of the host is writable but the run's private directories `dirs`, no network, the caller's
    environment cleared and the allowlisted one `env` set (`--tmpfs /tmp` hides what lives there, so the binds come after it). The
    daemon sockets are masked: /run, the real HOME and $XDG_RUNTIME_DIR by an empty tmpfs before the binds, every other socket file
    of the host by /dev/null AFTER all of them (a bind of the tracer's directory re-opens the sockets it holds): a read-only bind does
    not stop connect(). `ro` are the directories bound read-only (the tracer)."""
    a = [bwrap, "--die-with-parent", "--new-session", "--unshare-all", "--clearenv"]
    a += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    hidden = hidden_dirs(environ)
    for d in hidden:
        a += ["--tmpfs", d]
    for d in dirs.values():  # tree, home, tmp, out
        a += ["--bind", d, d]
    for d in ro:
        a += ["--ro-bind", d, d]
    for sock in host_sockets([*hidden, *_FRESH], keep=[*ro, *map(os.path.realpath, ro)]):
        a += ["--ro-bind", "/dev/null", sock]
    for k, v in sorted(env.items()):
        a += ["--setenv", k, v]
    return a + ["--chdir", cwd, "--"]


def run_group(argv: Sequence[str], cwd: str, timeout: float, env: Optional[dict] = None) -> Tuple[int, bytes, bytes]:
    """(rc, stdout, stderr) of a child started in its OWN process group. On a timeout (or any error) the whole group is killed, so no
    grandchild survives; raises subprocess.TimeoutExpired or OSError. A shell is never used."""
    p = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env if env is not None else dict(os.environ),
        start_new_session=True,
    )

    def kill_group() -> None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass

    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException:
        kill_group()
        try:
            p.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - the child is already killed; nothing else to do with it
            pass
        raise
    kill_group()  # stragglers of a finished tool (a daemon a go command left behind)
    return p.returncode, out, err


# ---------------------------------------------------------------- the delivery
def safe_rel(p) -> bool:
    """A path inside the tree: relative, no NUL, no empty, `.` or `..` component, at most 1024 characters."""
    return (
        isinstance(p, str)
        and 0 < len(p) <= 1024
        and "\0" not in p
        and all(x not in ("", ".", "..") for x in p.split("/"))
    )


def check_spec(doc) -> Spec:
    """The typed spec, or BoxError. Every key is mandatory and nothing else is accepted."""
    d = rt_data()
    if not isinstance(doc, dict) or set(doc) != set(SPEC_KEYS):
        raise BoxError("spec.json: not an object with exactly the keys " + ", ".join(SPEC_KEYS))
    if doc["schema"] != SCHEMA:
        raise BoxError(f"spec.json: schema {doc['schema']!r}, this target reads {SCHEMA}")
    if not (isinstance(doc["run_id"], str) and RUN_ID.match(doc["run_id"])):
        raise BoxError("spec.json: run_id must be 32 hex digits")
    if not (isinstance(doc["commit"], str) and HEX.match(doc["commit"])):
        raise BoxError("spec.json: commit must be a git object id in hex")
    if not (isinstance(doc["target"], str) and 0 < len(doc["target"]) <= 120 and doc["target"].isprintable()):
        raise BoxError("spec.json: target must be a short printable label")
    t = doc["timeout"]
    if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= d["limits"]["wall_s"]:
        raise BoxError(f"spec.json: timeout must be 1..{d['limits']['wall_s']} seconds")
    runs = doc["runs"]
    if not isinstance(runs, list) or not 1 <= len(runs) <= MAX_RUNS:
        raise BoxError(f"spec.json: runs must be a list of 1..{MAX_RUNS} commands")
    out: List[Run] = []
    for r in runs:
        if not isinstance(r, dict) or set(r) not in (set(RUN_KEYS), set(CHECK_KEYS)):
            raise BoxError(
                "spec.json: a run must be an object with exactly " + ", ".join(RUN_KEYS) + " (a check run: and cwd)"
            )
        langs, label, cwd = r["langs"], r["label"], r.get("cwd")
        if not (
            isinstance(langs, list)
            and (langs or "cwd" in r)
            and all(isinstance(x, str) and x in d["tracers"] for x in langs)
        ):
            raise BoxError("spec.json: a run names languages that have a tracer in data/runtime.json")
        if "cwd" in r and not (cwd == "" or safe_rel(cwd)):
            raise BoxError("spec.json: the cwd of a check run must be a relative directory of the tree")
        if not (isinstance(label, str) and 0 < len(label) <= 80 and label.isprintable()):
            raise BoxError("spec.json: a run label must be a short printable text")
        why = argv_problem(r["argv"])
        if why:
            raise BoxError(f"spec.json: {why}")
        out.append(Run(label, tuple(langs), tuple(r["argv"]), cwd))
    files = doc["files"]
    if not isinstance(files, dict) or len(files) > d["copy"]["max_files"]:
        raise BoxError("spec.json: files must be an object path -> blob id, within the file cap")
    for p, b in files.items():
        if not (safe_rel(p) and isinstance(b, str) and HEX.match(b)):
            raise BoxError("spec.json: files holds a path that is not safe or a blob id that is not hex")
    return Spec(doc["run_id"], doc["commit"], doc["target"], tuple(out), t, dict(files))


def _blob(data: bytes, blob_id: str) -> str:
    return blob_sha(data, "sha256" if len(blob_id) == 64 else "sha1")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unpack(tar_path: str, tree: str, want_sha: str) -> Spec:
    """Check `tar_path` (sha256, members, spec, blob ids) and extract its tree into `tree` (an existing empty directory). Nothing is
    written outside `tree`; BoxError otherwise, with the tree possibly half-filled (the rundir is removed by the controlling side).
    """
    d = rt_data()
    cap = d["copy"]["max_tree_mb"] << 20
    if not (isinstance(want_sha, str) and re.match(r"^[0-9a-f]{64}$", want_sha)):
        raise BoxError("--sha256 must be 64 hex digits")
    size = os.path.getsize(tar_path)
    if size > cap + (8 << 20):
        raise BoxError(f"in.tar is larger than the copy cap ({d['copy']['max_tree_mb']} MB)")
    if sha256_file(tar_path) != want_sha:
        raise BoxError("in.tar does not match the sha256 of the command: the delivery was cut or changed")
    spec: Optional[Spec] = None
    links: Dict[str, str] = {}
    seen: Dict[str, str] = {}
    total = 0
    with tarfile.open(tar_path, "r:") as tf:
        for m in tf:  # the members come in the order the controlling side wrote them: spec.json first
            if m.name == "spec.json":
                if spec is not None or not m.isreg() or m.size > d["copy"]["spec_bytes"]:
                    raise BoxError("in.tar: spec.json is repeated, not a file or too large")
                spec = check_spec(json.loads(tf.extractfile(m).read().decode("utf-8")))
                continue
            if spec is None:
                raise BoxError("in.tar: spec.json must be the first member")
            name = m.name.rstrip("/")
            if name == "tree" and m.isdir():
                continue
            if not (name.startswith("tree/") and safe_rel(name[5:])):
                raise BoxError(f"in.tar: member {m.name[:80]!r} is not a safe path below tree/")
            rel = name[5:]
            if rel in seen:
                raise BoxError(f"in.tar: {rel[:80]!r} appears twice")
            if m.isdir():
                seen[rel] = "d"
                os.makedirs(os.path.join(tree, rel), mode=0o755, exist_ok=True)
            elif m.issym():
                seen[rel], links[rel] = "l", m.linkname
            elif m.isreg():
                blob = spec.files.get(rel)
                if blob is None:
                    raise BoxError(f"in.tar: {rel[:80]!r} is not in the file list of the spec")
                total += m.size
                if m.size > d["copy"]["max_file_mb"] << 20 or total > cap:
                    raise BoxError("in.tar: a file or the tree is over the copy cap")
                data = tf.extractfile(m).read()
                if _blob(data, blob) != blob:
                    raise BoxError(f"in.tar: the content of {rel[:80]!r} is not the blob the spec names")
                dest = os.path.join(tree, rel)
                os.makedirs(os.path.dirname(dest), mode=0o755, exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(data)
                os.chmod(dest, 0o755 if m.mode & 0o111 else 0o644)
                seen[rel] = "f"
            else:
                raise BoxError(f"in.tar: member {rel[:80]!r} is neither a file, a directory nor a link")
    if spec is None:
        raise BoxError("in.tar has no spec.json")
    missing = sorted(set(spec.files) - {p for p, k in seen.items() if k == "f"})
    if missing:
        raise BoxError(f"in.tar lacks {len(missing)} file(s) of the spec, e.g. {missing[0][:80]!r}")
    kept = confine_links(
        links
    )  # a link that leaves the tree, also through other links, is not a link the controlling side sends
    if len(kept) != len(links):
        raise BoxError(f"in.tar: {len(links) - len(kept)} symbolic link(s) point outside the tree")
    for p, target in kept.items():
        try:
            os.makedirs(os.path.dirname(os.path.join(tree, p)), mode=0o755, exist_ok=True)
            os.symlink(target, os.path.join(tree, p))
        except OSError as e:
            raise BoxError(f"in.tar: the link {p[:80]!r} cannot be made ({e.strerror})")
    return spec


# ---------------------------------------------------------------- the readers of tracer output (pure functions of recorded files)
class Item(NamedTuple):
    lang: str
    file: str
    qual: str
    line: int
    end: int
    hit: int

    def doc(self) -> dict:
        return {
            "lang": self.lang,
            "file": self.file,
            "qual": self.qual,
            "line": self.line,
            "end": self.end,
            "hit": self.hit,
        }


def _read_json(path: str, cap: int):
    try:
        if os.path.getsize(path) > cap:
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _files_in(d: str, prefix: str, suffix: str, limit: int) -> List[str]:
    try:
        names = sorted(n for n in os.listdir(d) if n.startswith(prefix) and n.endswith(suffix))
    except OSError:
        return []
    return [os.path.join(d, n) for n in names[:limit] if os.path.isfile(os.path.join(d, n))]


def _int(v, lo=0, hi=1 << 31) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def read_python(out_dir: str, files: Dict[str, str]) -> Tuple[List[Item], str]:
    """The called functions the python tracer (tracers/py) wrote: one <pid>.json per process; merged, only paths of the file list."""
    r = rt_data()["result"]
    got: Dict[Tuple[str, str, int], int] = {}
    paths = _files_in(out_dir, "", ".json", r["max_trace_files"])
    for p in paths:
        doc = _read_json(p, r["max_trace_bytes"])
        if not (isinstance(doc, dict) and doc.get("v") == 1 and isinstance(doc.get("fn"), list)):
            continue
        for row in doc["fn"]:
            if (
                isinstance(row, list)
                and len(row) == 3
                and row[0] in files
                and isinstance(row[1], str)
                and _int(row[2], 1)
            ):
                got[(row[0], row[1][:200], row[2])] = 1
    note = (
        ""
        if got
        else (
            f"{len(paths)} trace file(s), no function of the project was called"
            if paths
            else "no trace file was written"
        )
    )
    return [Item("python", f, q, ln, 0, h) for (f, q, ln), h in sorted(got.items())], note


def _line_starts(text: str) -> List[int]:
    """UTF-16 offsets of the first character of each line (V8 offsets count UTF-16 code units)."""
    if text.isascii():
        return [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
    starts, pos = [0], 0
    for c in text:
        pos += 2 if ord(c) > 0xFFFF else 1
        if c == "\n":
            starts.append(pos)
    return starts


def _line_of(starts: List[int], off: int) -> int:
    lo, hi = 0, len(starts)
    while lo < hi:
        mid = (lo + hi) // 2
        if starts[mid] <= off:
            lo = mid + 1
        else:
            hi = mid
    return lo  # 1-based


def read_v8(out_dir: str, tree: str, files: Dict[str, str]) -> Tuple[List[Item], str]:
    """The functions V8 ran (NODE_V8_COVERAGE: coverage-<pid>-<time>-<n>.json): a function counts when the count of its first range is above
    zero. A script is read when its url is a file below `tree` that the file list has; the whole-file wrapper and anonymous functions
    that start at offset 0 are not functions of the project. Offsets become lines through the source text on the target.
    """
    r = rt_data()["result"]
    base = os.path.realpath(tree).rstrip("/") + "/"
    hits: Dict[Tuple[str, str, int, int], int] = {}
    starts: Dict[str, Optional[List[int]]] = {}
    paths = _files_in(out_dir, "coverage-", ".json", r["max_trace_files"])
    for p in paths:
        doc = _read_json(p, r["max_trace_bytes"])
        for sc in doc.get("result", []) if isinstance(doc, dict) and isinstance(doc.get("result"), list) else []:
            url = sc.get("url") if isinstance(sc, dict) else None
            if not (isinstance(url, str) and url.startswith("file://")):
                continue
            real = os.path.realpath(unquote(url[7:]))
            rel = real[len(base) :] if real.startswith(base) else ""
            if rel not in files or not isinstance(sc.get("functions"), list):
                continue
            if rel not in starts:
                try:
                    with open(os.path.join(tree, rel), encoding="utf-8", errors="replace") as fh:
                        starts[rel] = _line_starts(fh.read(4 << 20))
                except OSError:
                    starts[rel] = None
            st = starts[rel]
            if st is None:
                continue
            for fn in sc["functions"]:
                rg = fn.get("ranges") if isinstance(fn, dict) else None
                first = rg[0] if isinstance(rg, list) and rg and isinstance(rg[0], dict) else {}
                a, b, n = first.get("startOffset"), first.get("endOffset"), first.get("count")
                name = fn.get("functionName")
                if not (_int(a) and _int(b) and _int(n, 1) and isinstance(name, str)) or (a == 0 and not name):
                    continue
                k = (rel, name[:200], _line_of(st, a), _line_of(st, max(a, b - 1)))
                hits[k] = hits.get(k, 0) + n
    note = (
        ""
        if hits
        else (
            f"{len(paths)} coverage file(s), no function of the project ran"
            if paths
            else "no V8 coverage file was written"
        )
    )
    return [Item("javascript", f, q, a, b, n) for (f, q, a, b), n in sorted(hits.items())], note


_GO_FUNC = re.compile(r"^(?P<path>\S.*?\.go):(?P<line>\d+):\s+(?P<name>\S+)\s+(?P<pct>[0-9.]+)%\s*$")


def go_modules(tree: str, files: Dict[str, str]) -> List[Tuple[str, str]]:
    """[(module path, directory relative to the tree)] of every go.mod of the file list, longest module path first."""
    out = []
    for rel in sorted(files):
        if rel == "go.mod" or rel.endswith("/go.mod"):
            try:
                with open(os.path.join(tree, rel), encoding="utf-8", errors="replace") as fh:
                    m = next(
                        (
                            re.match(r"^module\s+(\S+)", ln)
                            for ln in fh.read(65536).splitlines()
                            if ln.startswith("module")
                        ),
                        None,
                    )
            except OSError:
                continue
            if m:
                out.append((m.group(1).strip('"'), rel[: -len("go.mod")].rstrip("/")))
    return sorted(out, key=lambda t: -len(t[0]))


def read_go(func_text: str, files: Dict[str, str], mods: Sequence[Tuple[str, str]]) -> Tuple[List[Item], str]:
    """The functions `go tool cover -func=<profile>` lists with a percentage above zero (a function that has no statement shows 100%: the
    one place where a hit may be generous; it can only hide a dead claim, never make one). The file name is an import path: the module
    path of the nearest go.mod gives the directory."""
    items, rows = {}, 0
    for ln in func_text.splitlines():
        m = _GO_FUNC.match(ln.strip())
        if not m:
            continue
        rows += 1
        path = m.group("path")
        mod = next((t for t in mods if path.startswith(t[0] + "/")), None)
        if mod is None:
            continue
        rel = "/".join(x for x in (mod[1], path[len(mod[0]) + 1 :]) if x)
        if rel in files and float(m.group("pct")) > 0:
            items[(rel, m.group("name")[:200], int(m.group("line")))] = 1
    note = (
        ""
        if items
        else (
            "the profile lists %d function(s), none of the project ran" % rows
            if rows
            else "no function rows in the cover output"
        )
    )
    return [Item("go", f, q, ln, 0, h) for (f, q, ln), h in sorted(items.items())], note


# ---------------------------------------------------------------- the box
class Plan(NamedTuple):
    argv: List[str]
    env: Dict[str, str]
    notes: Dict[str, str]  # language -> `no tracer for ...` reason (the language is not traced in this run)
    post: Dict[
        str, Callable[[], List[str]]
    ]  # language -> the argv of its report command (run after the main one, in the box)


def inject_go(argv: Sequence[str], profile: str) -> Optional[List[str]]:
    """`go test ...` with the profile flags after `test` (all packages of the module are measured), None when the command is no `go test`
    or already asks for a coverage profile itself."""
    a = list(argv)
    if (
        len(a) < 2
        or os.path.basename(a[0]) != "go"
        or a[1] != "test"
        or any(x.startswith(("-coverprofile", "-test.coverprofile")) for x in a)
    ):
        return None
    return a[:2] + [f"-coverprofile={profile}", "-covermode=set", "-coverpkg=./..."] + a[2:]


def plan(run: Run, dirs: Dict[str, str], d: Optional[dict] = None, tracer_dir: str = TRACER_DIR) -> Plan:
    """The command line, the environment and the report commands of one run. Directories of `dirs` (tree, home, tmp, out) are made here."""
    d = d or rt_data()
    env = {"PATH": PATH, "HOME": dirs["home"], "TMPDIR": dirs["tmp"], "LANG": "C.UTF-8"}
    if run.cwd is not None:  # a check run: the language's environment (offline, no update notice), no tracer
        for lang in run.langs:
            env.update(d["tracers"][lang].get("env") or {})
        return Plan(list(run.argv), env, {}, {})
    argv, notes, post = list(run.argv), {}, {}
    out = dirs["out"]
    for lang in run.langs:
        spec = d["tracers"][lang]
        env.update(spec.get("env") or {})
        sub = os.path.join(out, {"python": "py", "javascript": "v8"}.get(lang, lang))
        if lang == "python":
            os.makedirs(sub, exist_ok=True)
            env.update({"PYTHONPATH": tracer_dir, "DCRT_OUT": sub, "DCRT_ROOT": dirs["tree"]})
        elif lang == "javascript":
            os.makedirs(sub, exist_ok=True)
            env["NODE_V8_COVERAGE"] = sub
        elif lang == "go":
            prof = os.path.join(out, "go.cover")
            new = inject_go(argv, prof)
            if new is None:
                notes[lang] = "no tracer for go: the command is not a `go test` without its own -coverprofile"
            else:
                argv = new
                post[lang] = lambda prof=prof: ["go", "tool", "cover", f"-func={prof}"]
    return Plan(argv, env, notes, post)


def limits_for(run: Run, d: Optional[dict] = None) -> Dict[str, int]:
    """The prlimit values of a run: data limits, and a language's own override (0 lifts the key: only when EVERY language of the run lifts it)."""
    d = d or rt_data()
    lim = {k: v for k, v in d["limits"].items() if k != "wall_s"}
    for key in list(lim):
        over = [d["tracers"][lang]["limits"].get(key) for lang in run.langs]
        if over and all(o == 0 for o in over):
            del lim[key]
    return lim


def sandbox_problem(tools: Tools) -> str:
    """ "" when the target can box a run, else why not (the text of the controlling side's skip line)."""
    if not tools.bwrap:
        return tools.bwrap_note or "bubblewrap is not installed"
    if not tools.prlimit:
        return "prlimit is not in a trusted system directory (util-linux)"
    return ""


class Outcome(NamedTuple):
    rc: Optional[int]
    timed_out: bool
    seconds: float
    why: str
    tail: str = ""  # the first error line of a command that ended with a rc other than 0


def _exec(
    argv: Sequence[str],
    dirs: Dict[str, str],
    env: Dict[str, str],
    lim: Dict[str, int],
    tools,
    starter,
    wall: float,
    ro: Sequence[str],
    cwd: Optional[str] = None,
) -> Tuple[Outcome, bytes]:
    cwd = cwd or dirs["tree"]
    flags = [f"{opt}={lim[k] * mul}" for k, (opt, mul) in PRLIMIT.items() if k in lim]
    full = [*bwrap_prefix(tools.bwrap, dirs, env, ro, cwd), tools.prlimit, *flags, "--", *argv]
    t0 = time.monotonic()
    try:
        rc, so, se = starter(full, cwd, max(1.0, wall), {"PATH": PATH})
    except subprocess.TimeoutExpired:
        return (
            Outcome(
                None, True, round(time.monotonic() - t0, 1), "the time limit expired (the process group was killed)"
            ),
            b"",
        )
    except OSError as e:
        return (
            Outcome(None, False, round(time.monotonic() - t0, 1), f"it could not be started ({e.strerror or e})"),
            b"",
        )
    tail = (
        first_error((so or b"").decode("utf-8", "replace"), (se or b"").decode("utf-8", "replace")) if rc else ""
    )
    return Outcome(rc, False, round(time.monotonic() - t0, 1), "", tail), (so or b"")[:MAX_OUTPUT]


def _note(t: str) -> str:
    return " ".join(str(t).split())[: rt_data()["result"]["note_chars"]]


def _write(path: str, doc) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, sort_keys=True, separators=(",", ":"))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def execute(
    spec: Spec, rundir: str, tools, starter, d: Optional[dict] = None, now: Callable[[], float] = time.monotonic
) -> dict:
    """Run the commands of `spec` in the box and read the tracers: the runtime.json document (see the module docstring). `rundir` holds
    tree/ (extracted) and out/; home/ and tmp/ are made here."""
    d = d or rt_data()
    dirs = {n: os.path.join(rundir, n) for n in ("tree", "home", "tmp", "out")}
    for n in ("home", "tmp", "out"):
        os.makedirs(dirs[n], mode=0o700, exist_ok=True)
    doc: dict = {
        "schema": SCHEMA,
        "run_id": spec.run_id,
        "commit": spec.commit,
        "target": spec.target,
        "status": "ok",
        "notes": [],
        "runs": [],
        "files": {},
        "executed": [],
    }
    end, items = now() + spec.timeout, []
    mods = go_modules(dirs["tree"], spec.files)
    for i, run in enumerate(spec.runs):
        left = end - now()
        rec = {
            "label": run.label,
            "langs": list(run.langs),
            "argv0": os.path.basename(run.argv[0])[:60],
            "rc": None,
            "timed_out": False,
            "seconds": 0.0,
            "tracers": {},
        }
        doc["runs"].append(rec)
        if left < 1:
            rec["why"] = "no time left of the timeout for this command"
            continue
        out = os.path.join(dirs["out"], f"r{i}")
        os.makedirs(out, mode=0o700, exist_ok=True)
        rd = dict(dirs, out=out)
        if (
            run.cwd is not None
        ):  # a check run: the command in its directory, rc and the first error line come back, nothing else
            cwd = os.path.join(dirs["tree"], *run.cwd.split("/")) if run.cwd else dirs["tree"]
            if not os.path.isdir(cwd):
                rec["why"] = "no such directory in the project copy"
                continue
            pl = plan(run, rd, d)
            res, _so = _exec(pl.argv, rd, pl.env, limits_for(run, d), tools, starter, left, [], cwd)
            rec.update(rc=res.rc, timed_out=res.timed_out, seconds=res.seconds)
            if res.why:
                rec["why"] = _note(res.why)
            if res.tail:
                rec["error"] = _note(res.tail)
            continue
        pl = plan(run, rd, d)
        for lang, why in pl.notes.items():
            rec["tracers"][lang] = {"tracer": "", "status": "no-tracer", "symbols": 0, "note": _note(why)}
        ro = [TRACER_DIR] if "python" in run.langs else []
        res, _so = _exec(pl.argv, rd, pl.env, limits_for(run, d), tools, starter, left, ro)
        rec.update(rc=res.rc, timed_out=res.timed_out, seconds=res.seconds)
        if res.why:
            rec["why"] = _note(res.why)
        got: Dict[str, Tuple[List[Item], str]] = {}
        if "python" in run.langs:
            got["python"] = read_python(os.path.join(out, "py"), spec.files)
        if "javascript" in run.langs:
            got["javascript"] = read_v8(os.path.join(out, "v8"), dirs["tree"], spec.files)
        for lang, mk in pl.post.items():
            left = max(1.0, min(POST_S, end - now()))
            pres, text = _exec(mk(), rd, pl.env, limits_for(run, d), tools, starter, left, ro)
            if lang == "go":
                got[lang] = (
                    read_go(text.decode("utf-8", "replace"), spec.files, mods)
                    if pres.rc == 0
                    else ([], f"go tool cover failed ({pres.why or 'rc ' + str(pres.rc)})")
                )

        for lang, (its, note) in got.items():
            rec["tracers"][lang] = {
                "tracer": d["tracers"][lang]["name"],
                "status": "traced" if its else "no-data",
                "symbols": len(its),
                "note": _note(note),
            }
            items += its
    cap = d["result"]["max_symbols"]
    merged: Dict[Tuple[str, str, str, int, int], int] = {}
    for it in items:
        k = (it.lang, it.file, it.qual, it.line, it.end)
        merged[k] = merged.get(k, 0) + it.hit
    rows = [Item(*k, h) for k, h in sorted(merged.items(), key=lambda kv: (kv[0][1], kv[0][3], kv[0][2], kv[0][0]))]
    if len(rows) > cap:
        doc["notes"].append(f"{len(rows)} symbols ran; the first {cap} (by file and line) are returned")
        rows = rows[:cap]
    doc["executed"] = [r.doc() for r in rows]
    doc["files"] = {f: spec.files[f] for f in sorted({r.file for r in rows})}
    return doc


def _rundir(path: str) -> str:
    """The run directory: absolute, named rt-<32 hex>, a real directory (no link) of this user, mode 0700. (main() also requires it
    to lie below /tmp, /var/tmp or /dev/shm, RUN_RE.)"""
    if not NAME_RE.match(path or "") or ".." in path.split("/"):
        raise BoxError("--box must be <tmp dir>/rt-<32 hex digits>")
    try:
        st = os.lstat(path)
    except OSError as e:
        raise BoxError(f"--box {path}: {e.strerror}")
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise BoxError("--box is not a private (0700) directory of this user")
    return path


def box(
    rundir: str, sha256: str, on_target: bool = False, starter=None, tools=None, d: Optional[dict] = None
) -> int:
    """The whole target half: verify, unpack, box, run, read, write out/runtime.json and out/manifest.json. Returns the exit code
    (0 = a result was written, also `no-sandbox`; BoxError = nothing ran). `on_target` must be True: the caller says that this is the
    execution target, where project code may run (a call without it is refused). `starter` and `tools` are for the tests."""
    if on_target is not True:
        raise BoxError("this is the half that runs on the execution target (--on-target); nothing is run on this host")
    d = d or rt_data()
    rd = _rundir(rundir)
    out = os.path.join(rd, "out")
    os.makedirs(out, mode=0o700, exist_ok=True)
    tree = os.path.join(rd, "tree")
    os.makedirs(tree, mode=0o700, exist_ok=True)
    tools = tools or detect_tools()
    spec = unpack(os.path.join(rd, "in.tar"), tree, sha256)
    why = sandbox_problem(tools)
    if why:
        doc = {
            "schema": SCHEMA,
            "run_id": spec.run_id,
            "commit": spec.commit,
            "target": spec.target,
            "status": "no-sandbox",
            "notes": [_note(why)],
            "runs": [],
            "files": {},
            "executed": [],
        }
    else:
        doc = execute(spec, rd, tools, starter or run_group, d)
    _write(os.path.join(out, "runtime.json"), doc)
    p = os.path.join(out, "runtime.json")
    _write(
        os.path.join(out, "manifest.json"),
        {
            "schema": SCHEMA,
            "run_id": spec.run_id,
            "commit": spec.commit,
            "runtime_sha256": sha256_file(p),
            "runtime_bytes": os.path.getsize(p),
        },
    )
    return 0


def _args(argv: Sequence[str]) -> Tuple[bool, str, str]:
    """(on_target, box, sha256) of the command line; BoxError for anything else (no abbreviation, no extra argument)."""
    on, box_dir, sha, i, a = False, "", "", 0, list(argv)
    while i < len(a):
        if a[i] == "--on-target":
            on = True
        elif a[i] in ("--box", "--sha256") and i + 1 < len(a):
            if a[i] == "--box":
                box_dir = a[i + 1]
            else:
                sha = a[i + 1]
            i += 1
        else:
            raise BoxError("usage: rtbox.py --on-target --box <rundir> --sha256 <hex>")
        i += 1
    return on, box_dir, sha


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`python3 -I rtbox.py --on-target --box <rundir> --sha256 <hex>`: the exit code of box(); the reason of a refusal is the last line
    of stderr. Nothing is read from the environment here."""
    try:
        on, rundir, sha = _args(sys.argv[1:] if argv is None else argv)
        if not on:
            raise BoxError("this is the half that runs on the execution target (--on-target); nothing is run on this host")
        if not RUN_RE.match(rundir or ""):
            raise BoxError("--box must lie below /tmp, /var/tmp or /dev/shm")
        return box(rundir, sha, on_target=True)
    except BoxError as e:
        sys.stderr.write(f"dead-code-checker runtime box: {e}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
