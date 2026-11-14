"""graph.engines: the engine layer (registry, lock, resolution, doctor, plan). Standard library only, nothing here touches the network.

    registry   data/engines.json: language -> engines by role (branch audit sem), per engine the executable name per OS, the version
               command, the licence class, the install hint per OS and the network-free environment.
    lock       an optional file of sha256 pins, given as `--lock FILE` (or $DEAD_CODE_CHECKER_ENGINES_LOCK):

                   {"schema": 1, "platforms": {"linux-x86_64": {"engines": {"vulture": {"sha256": "<hex>", "version": "2.14"}}}}}

               `dead-code-checker graph lock` writes one from the engines found now. With a lock, an engine runs only when its file
               has the pinned sha256: a different file is `sha-mismatch`, an engine the lock does not list is `untrusted`. Without a
               lock the engines are used as found and `doctor` says so once. A lock that is not valid JSON, has no schema 1 or has a
               malformed pin is an error (ValueError), never "no lock". With a lock nothing is started before its bytes are checked
               (the `-h` probe that tells the Go `deadcode` from the PyPI one only sees pinned files). The lock covers the engines of
               the registry; `go` (the Go cache check), `git`, `bwrap` and `unshare` are looked up on PATH and run unpinned.
    where      the engines directory (`--engines-dir DIR`, or $DEAD_CODE_CHECKER_ENGINES_DIR; several directories are separated like
               PATH) is searched first, then PATH. A repository's own configuration can not choose a path, argv or env of an engine.
    status     find_engine -> Res(path, status, detail), status in ok | missing | untrusted | sha-mismatch | degraded
               (degraded: the engine has no build for this OS; it is a report line, never a failure).
    doctor     one row per engine applicable to the host OS and the languages, with the install hint of the OS.
    plan       the engines of one role for the languages in scope, each resolved once: usable now, missing, degraded or waived.

An engine that builds the project (needs_build) is run through graph.sandbox: bubblewrap when it is installed, else unsandboxed
with a note. Nothing in this module runs one.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
from collections import namedtuple
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from .. import safepath
from . import proc

HERE = os.path.dirname(os.path.realpath(__file__))
REGISTRY_PATH = os.path.join(HERE, "data", "engines.json")
ENV_LOCK = "DEAD_CODE_CHECKER_ENGINES_LOCK"
ENV_DIR = "DEAD_CODE_CHECKER_ENGINES_DIR"
FAILING = ("missing", "untrusted", "sha-mismatch")
ROLES = ("branch", "audit", "sem")
CLASSES = ("permissive", "process-only")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
Res = namedtuple("Res", "path status detail")  # path is None unless status == ok


# ------------------------------------------------------------------ host
def host_os() -> str:
    return {"nt": "windows"}.get(os.name) or ("darwin" if platform.system() == "Darwin" else "linux")


def platform_key(os_name: Optional[str] = None) -> str:
    m = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(m, m or "unknown")
    return f"{os_name or host_os()}-{arch}"


class Setup(NamedTuple):
    """Where the engines are looked for and which lock pins them (both optional)."""

    lock: str = ""
    dirs: Tuple[str, ...] = ()
    path: bool = True  # PATH is searched after the directories


_SETUP: List[Optional[Setup]] = [None]


def configure(lock: Optional[str] = None, engines_dir: Optional[str] = None, path: bool = True) -> Setup:
    """Fix the setup of this process (the command line does it once). A value that is None falls back to the environment."""
    s = Setup(lock or "", tuple(d for d in (engines_dir or "").split(os.pathsep) if d), path)
    _SETUP[0] = s
    return s


def reset() -> None:
    _SETUP[0] = None


def current() -> Setup:
    """The configured setup, else the one the environment gives: $DEAD_CODE_CHECKER_ENGINES_LOCK / _DIR."""
    s = _SETUP[0]
    if s is not None:
        return Setup(
            s.lock or os.environ.get(ENV_LOCK, ""),
            s.dirs or tuple(d for d in os.environ.get(ENV_DIR, "").split(os.pathsep) if d),
            s.path,
        )
    return Setup(os.environ.get(ENV_LOCK, ""), tuple(d for d in os.environ.get(ENV_DIR, "").split(os.pathsep) if d))


path_dirs = safepath.path_dirs  # the absolute entries of PATH: a relative one is the working directory's, which can be the analysed repository
safe_path = safepath.safe_path  # the PATH an engine or a tool is started with


def search_dirs(setup: Optional[Setup] = None) -> List[str]:
    """The directories an engine is looked for in, in order: the engines directories (made absolute: the user gave them, relative to where
    the user stands), then the absolute entries of PATH (when the setup allows it)."""
    s = setup or current()
    out = [os.path.abspath(d) for d in s.dirs if d]
    if s.path:
        out += path_dirs()
    seen: Set[str] = set()
    return [d for d in out if not (d in seen or seen.add(d))]


# ------------------------------------------------------------------ registry
_REG: Dict[str, dict] = {}


def load_registry(path: Optional[str] = None) -> dict:
    """The registry (cached per path). ValueError lists every structural problem."""
    p = path or REGISTRY_PATH
    if p not in _REG:
        with open(p, encoding="utf-8") as fh:
            reg = json.load(fh)
        bad = validate_registry(reg)
        if bad:
            raise ValueError("engines registry %s: %s" % (p, "; ".join(bad)))
        _REG[p] = reg
    return _REG[p]


def validate_registry(reg: dict) -> List[str]:
    bad: List[str] = []
    if reg.get("schema") != 1:
        bad.append("schema must be 1")
    engines = reg.get("engines") or {}
    seen: Dict[str, str] = {}
    for name, e in sorted(engines.items()):
        for n in (name, *(e.get("aliases") or [])):
            if n in seen:
                bad.append(f"name {n} used by {seen[n]} and {name}")
            seen[n] = name
        if e.get("class") not in CLASSES:
            bad.append(f"{name}: class must be one of {CLASSES}")
        if e.get("needs_net") is not False:
            bad.append(f"{name}: needs_net must be false (engines never use the network at run time)")
        for osn in reg.get("oses", ()):
            if osn not in (e.get("bin") or {}) or osn not in (e.get("install") or {}):
                bad.append(f"{name}: bin and install must name {osn}")
        for osn, ins in (e.get("install") or {}).items():
            if ins.get("method") != "degraded" and not ins.get("cmd"):
                bad.append(f"{name}: install.{osn} needs a cmd or method degraded")
            if (e.get("bin") or {}).get(osn) is None and ins.get("method") != "degraded":
                bad.append(f"{name}: no {osn} executable but install.{osn} is not degraded")
    for lang, spec in (reg.get("languages") or {}).items():
        for role, items in (spec.get("roles") or {}).items():
            if role not in ROLES:
                bad.append(f"{lang}: unknown role {role}")
            for it in items:
                if it["engine"] not in engines:
                    bad.append(f"{lang}.{role}: unknown engine {it['engine']}")
    return bad


def exe_names(reg: dict, name: str, os_name: Optional[str] = None) -> Tuple[str, ...]:
    e = (reg.get("engines") or {}).get(name)
    exe = ((e or {}).get("bin") or {}).get(os_name or host_os())
    if isinstance(exe, str):
        return (exe,)
    return tuple(exe or ())


def degraded_reason(reg: dict, name: str, os_name: Optional[str] = None) -> Optional[str]:
    """Why the engine does not exist for the OS, or None when it does (or when the registry does not know it)."""
    e = (reg.get("engines") or {}).get(name)
    if e is None:
        return None
    ins = (e.get("install") or {}).get(os_name or host_os()) or {}
    if ins.get("method") == "degraded" or not exe_names(reg, name, os_name):
        return ins.get("reason") or "no build for this OS"
    return None


def not_run_here(name: str, reason: str) -> str:
    """The text of the W dc.engine-incomplete for an engine that is degraded on this OS."""
    return f"{name}: not run here ({reason})"


def install_line(reg: dict, name: str, os_name: Optional[str] = None) -> str:
    """The install hint of the engine for the OS (a hint in words or one command; nothing here runs it)."""
    e = (reg.get("engines") or {}).get(name)
    if e is None:
        return f"{name}: not in the engines registry"
    why = degraded_reason(reg, name, os_name)
    if why:
        return f"degraded: {why}"
    return e["install"][os_name or host_os()]["cmd"]


def lang_engines(reg: dict, lang: str, role: str) -> List[Tuple[str, bool]]:
    return [
        (i["engine"], bool(i.get("required")))
        for i in ((reg["languages"].get(lang) or {}).get("roles") or {}).get(role, [])
    ]


def project_files(root: str, limit: int = 50000) -> List[str]:
    """Tracked files of the project (git), else a bounded walk; relative posix paths."""
    tracked = None
    try:
        r = safepath.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        if r.returncode == 0:
            tracked = [p for p in r.stdout.decode("utf-8", "replace").split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        tracked = None  # no git or no repo: the bounded walk below
    if tracked is not None:
        return tracked[:limit]
    out: List[str] = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in (".git", "node_modules", "vendor", "target", "__pycache__"))
        for f in sorted(fns):
            out.append(os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/"))
            if len(out) >= limit:
                return out
    return out


def project_langs(reg: dict, files: Iterable[str]) -> Set[str]:
    """The registry languages present in the file list (by extension or marker file); "*" (language-neutral engines) always."""
    files = list(files)
    exts = {os.path.splitext(f)[1].lower() for f in files}
    bases = {os.path.basename(f) for f in files}
    langs = {"*"}
    for lang, spec in reg["languages"].items():
        if lang != "*" and (exts & set(spec.get("ext") or ()) or bases & set(spec.get("markers") or ())):
            langs.add(lang)
    return langs


def needs_met(needs: Sequence[str], files: Iterable[str]) -> bool:
    """Every marker of `needs` (a glob on the base name) exists in the file list."""
    bases = {os.path.basename(f) for f in files}
    return all(any(fnmatch.fnmatch(b, n) for b in bases) for n in needs)


# ------------------------------------------------------------------ lock
def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def check_lock(lock: object, where: str) -> dict:
    """The lock, or ValueError naming the first problem (a pin that is not 64 hex digits makes the whole lock unusable)."""
    if not isinstance(lock, dict) or lock.get("schema") != 1:
        raise ValueError(f"lock {where}: schema must be 1")
    plats = lock.get("platforms")
    if not isinstance(plats, dict):
        raise ValueError(f"lock {where}: platforms must be an object")
    for plat, body in plats.items():
        engines = (body or {}).get("engines") if isinstance(body, dict) else None
        if not isinstance(engines, dict):
            raise ValueError(f"lock {where}: platforms.{plat}.engines must be an object")
        for name, pin in engines.items():
            if not isinstance(pin, dict) or not SHA_RE.match(str(pin.get("sha256") or "")):
                raise ValueError(f"lock {where}: {plat}/{name}: sha256 must be 64 hex digits")
    return lock


def load_lock(path: Optional[str] = None) -> Optional[dict]:
    """The lock of `path` (default: the configured one), None when none is configured. ValueError when the file is missing, not
    JSON or malformed: a lock that was asked for and cannot be read must not turn into "no lock"."""
    p = path or current().lock
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            lock = json.load(fh)
    except OSError as exc:
        raise ValueError(f"lock {p} cannot be read: {exc.strerror or exc}")
    except ValueError as exc:
        raise ValueError(f"lock {p} is not valid JSON: {exc}")
    return check_lock(lock, p)


_LOCKS: Dict[str, tuple] = {}


def cached_lock() -> Optional[dict]:
    """load_lock() for the configured path, re-read only when the file changes."""
    p = current().lock
    if not p:
        return None
    try:
        st = os.stat(p)
    except OSError as exc:
        raise ValueError(f"lock {p} cannot be read: {exc.strerror or exc}")
    key = (st.st_mtime_ns, st.st_size)
    if _LOCKS.get(p, (None,))[0] != key:
        _LOCKS[p] = (key, load_lock(p))
    return _LOCKS[p][1]


def lock_engines(lock: Optional[dict], plat: Optional[str] = None) -> dict:
    return (((lock or {}).get("platforms") or {}).get(plat or platform_key()) or {}).get("engines") or {}


def lock_entry(lock: Optional[dict], name: str, plat: Optional[str] = None) -> Optional[dict]:
    engines = lock_engines(lock, plat)
    if name in engines:
        return engines[name]
    for n, e in engines.items():  # an alias of a registered engine is locked under its own name
        if e.get("alias_of") == name:
            return e
    return None


def make_lock(rows: Dict[str, dict], plat: Optional[str] = None, base: Optional[dict] = None) -> dict:
    """A lock with the pins `rows` ({engine: {"sha256": ..., "version": ...}}) for this platform; the other platforms of `base` stay."""
    plats = {k: v for k, v in ((base or {}).get("platforms") or {}).items()}
    plats[plat or platform_key()] = {"engines": {n: dict(sorted(r.items())) for n, r in sorted(rows.items())}}
    return {"schema": 1, "platforms": dict(sorted(plats.items()))}


def write_lock(path: str, lock: dict) -> None:
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".lock-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(lock, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ------------------------------------------------------------------ sha256
_SHA_MEMO: Dict[tuple, str] = {}


def sha256_file(path: str) -> str:
    """sha256 of the file's bytes (symlinks followed); memoised per process by (path, inode, mtime, size)."""
    rp = os.path.realpath(path)
    st = os.stat(rp)
    key = (rp, st.st_ino, st.st_mtime_ns, st.st_size)
    if key not in _SHA_MEMO:
        h = hashlib.sha256()
        with open(rp, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        _SHA_MEMO[key] = h.hexdigest()
    return _SHA_MEMO[key]


def tree_sha256(path: str) -> str:
    """sha256 of the sorted (relpath, sha256) list of a file or directory tree (symlinks inside are not followed)."""
    if os.path.isfile(path):
        return sha256_file(path)
    rows = []
    for dp, dns, fns in os.walk(path):
        dns.sort()
        for f in sorted(fns):
            p = os.path.join(dp, f)
            if os.path.islink(p):
                rows.append((os.path.relpath(p, path).replace(os.sep, "/"), "L:" + os.readlink(p)))
            else:
                rows.append((os.path.relpath(p, path).replace(os.sep, "/"), sha256_file(p)))
    return hashlib.sha256(canonical(rows).encode()).hexdigest()


# ------------------------------------------------------------------ resolution
def _is_exe(p: str) -> bool:
    return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)


def named_candidates(
    names: Sequence[str], dirs: Sequence[str], os_name: Optional[str] = None
) -> List[Tuple[str, str]]:
    """(executable name, path) of the executables of `names` found in `dirs`, in order (names outer, directories inner). Nothing is
    started here: a file is looked at, not run. A relative directory is skipped: it would be the working directory's, which can be the
    analysed repository (search_dirs gives absolute ones only)."""
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    win = (os_name or host_os()) == "windows"
    for exe in names:
        variants = [exe] if not win or os.path.splitext(exe)[1] else [exe + x for x in (".exe", ".cmd", ".bat")]
        for d in dirs:
            if not os.path.isabs(d):
                continue
            for v in variants:
                c = os.path.join(d, v)
                if _is_exe(c) and c not in seen:
                    seen.add(c)
                    out.append((exe, c))
    return out


def candidates(
    names: Sequence[str],
    dirs: Sequence[str],
    accept: Optional[Callable[[str, str], bool]] = None,
    os_name: Optional[str] = None,
) -> List[str]:
    """The executables of `names` found in `dirs`, in order; accept(exe_name, path) may veto one. accept may start the file (the
    Go check of `deadcode` does), so a caller with a lock does not pass it here: it hashes first (see find_engine)."""
    return [c for exe, c in named_candidates(names, dirs, os_name) if accept is None or accept(exe, c)]


def resolve(
    names: Sequence[str],
    dirs: Sequence[str],
    accept: Optional[Callable[[str, str], bool]] = None,
    os_name: Optional[str] = None,
) -> Optional[str]:
    """The first executable of `names` in `dirs` (None when there is none)."""
    found = candidates(names, dirs, accept, os_name)
    return found[0] if found else None


def find_engine(
    reg: dict,
    lock: Optional[dict],
    name: str,
    *,
    os_name: Optional[str] = None,
    dirs: Optional[Sequence[str]] = None,
    names: Optional[Sequence[str]] = None,
    accept: Optional[Callable[[str, str], bool]] = None,
    verify: bool = True,
) -> Res:
    """Where the engine is and in which state (see the module docstring). `names` overrides the registry's executable names;
    verify=False skips the lock."""
    os_name = os_name or host_os()
    why = degraded_reason(reg, name, os_name)
    if why:
        return Res(None, "degraded", why)
    where = list(search_dirs() if dirs is None else dirs)
    spec = (reg.get("engines") or {}).get(name) or {}
    if spec.get("kind") == "module":  # a directory (a PowerShell module): looked up by name in the module directories
        dirs_found = [os.path.join(x, name) for x in where if os.path.isdir(os.path.join(x, name))]
        if not dirs_found:
            return Res(None, "missing", f"{name} module not found in the engines directories")
        if lock is None or not verify:
            return Res(dirs_found[0], "ok", "")
        pin = lock_entry(lock, name)  # a module is pinned by the hash of its whole tree (tree_sha256), like a binary by its bytes
        if pin is None:
            return Res(None, "untrusted", f"{dirs_found[0]} is not listed in the lock")
        want = str(pin["sha256"])
        for d in dirs_found:
            try:
                if tree_sha256(d) == want:
                    return Res(d, "ok", "")
            except OSError:
                continue
        try:
            got = tree_sha256(dirs_found[0])
        except OSError as exc:
            return Res(None, "untrusted", f"{dirs_found[0]} cannot be read: {exc.strerror or exc}")
        return Res(None, "sha-mismatch", f"{dirs_found[0]}: tree sha256 {got[:12]} differs from the lock's {want[:12]}")
    pairs = named_candidates(names or exe_names(reg, name, os_name) or (name,), where, os_name)
    gone = "not found in the engines directories or on PATH" if current().dirs else "not found on PATH"
    if lock is None or not verify:
        found = [c for exe, c in pairs if accept is None or accept(exe, c)]
        return Res(found[0], "ok", "") if found else Res(None, "missing", gone)
    # With a lock nothing is started before its bytes are checked: `accept` may run the file (the Go check of `deadcode` runs
    # `-h`), so it only sees the files that have the pinned sha256, and a file the lock does not name is never run.
    if not pairs:
        return Res(None, "missing", gone)
    pin = lock_entry(lock, name)
    if pin is None:
        return Res(None, "untrusted", f"{pairs[0][1]} is not listed in the lock")
    want = str(pin["sha256"])
    pinned: List[Tuple[str, str]] = []
    for exe, c in pairs:  # every copy with the pinned bytes qualifies: a copy earlier on PATH does not hide the pinned one
        try:
            if sha256_file(c) == want:
                pinned.append((exe, c))
        except OSError:
            continue
    for exe, c in pinned:
        if accept is None or accept(exe, c):
            return Res(c, "ok", "")
    if pinned:
        return Res(None, "missing", f"{pinned[0][1]} has the pinned bytes but is not the tool this engine stands for")
    try:
        got = sha256_file(pairs[0][1])
    except OSError as exc:
        return Res(None, "untrusted", f"{pairs[0][1]} cannot be read: {exc.strerror or exc}")
    return Res(None, "sha-mismatch", f"{pairs[0][1]}: sha256 {got[:12]} differs from the lock's {want[:12]}")


# ------------------------------------------------------------------ version
def parse_version(spec: dict, text: str) -> Optional[str]:
    rx = spec.get("version_re") or r"(\d+\.\d+(?:\.\d+)?\S*)"
    m = re.search(rx, text)
    if not m:
        return None
    return next((g for g in m.groups() if g), m.group(0)) if m.groups() else m.group(0)


PASS_ENV = (
    "RUSTUP_TOOLCHAIN",
    "JAVA_HOME",
    "SYSTEMROOT",
)  # variables an engine needs to find its toolchain: passed on as they are


def host_env() -> Dict[str, str]:
    """What an engine is given of the real environment: PATH, the variables of PASS_ENV, and RUSTUP_HOME (the real one, else
    ~/.rustup when it exists: a rustup proxy cannot find its toolchain from the private HOME an engine runs with). Nothing else:
    not HOME, not the proxy and cache variables of the session."""
    env = {"PATH": safe_path()}
    env.update({k: os.environ[k] for k in PASS_ENV if k in os.environ})
    rh = os.environ.get("RUSTUP_HOME") or os.path.join(os.path.expanduser("~"), ".rustup")
    if os.path.isdir(rh):
        env["RUSTUP_HOME"] = rh
    return env


_VERSIONS: Dict[tuple, str] = {}


def engine_version(reg: dict, lock: Optional[dict], name: str, path: Optional[str] = None) -> str:
    """The version that stamps what an engine produced (the semantic cache, the corroboration text): the lock's `version` when the
    lock pins one, else what the engine says for its version command, else `sha256:<12 digits>` of its file (so that a changed
    binary is a changed engine even when it does not tell its version). `path` is where the engine is (looked up when None); "" when
    there is no such engine. Memoised per (path, size, mtime)."""
    pin = lock_entry(lock, name)
    if pin and pin.get("version"):
        return str(pin["version"])
    if path is None:
        path = find_engine(reg, lock, name, accept=accept_for(name)).path
    if not path:
        return ""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    key = (path, st.st_size, st.st_mtime_ns)
    if key not in _VERSIONS:
        spec = (reg.get("engines") or {}).get(name) or {}
        ver = None if spec.get("kind") == "module" else probe_version(spec, path)
        _VERSIONS[key] = ver or "sha256:" + sha256_file(path)[:12]
    return _VERSIONS[key]


def netless_prefix() -> List[str]:
    return proc.netless_prefix()


def probe_version(spec: dict, path: str, timeout: float = 30.0) -> Optional[str]:
    """Run the engine's version_cmd in its own process group, network-less when the host allows it, with a private empty HOME and
    the registry's env; None if there is no command, it fails, or nothing matches."""
    cmd = spec.get("version_cmd")
    if not cmd:
        return None
    with tempfile.TemporaryDirectory(prefix="dcc-ver-") as home:
        env = {**host_env(), "HOME": home, "TMPDIR": home, **(spec.get("env") or {})}
        try:
            rc, out, err = proc.run_group(
                netless_prefix() + [path if c == "{bin}" else c for c in cmd], home, timeout, env
            )
        except (OSError, subprocess.SubprocessError):
            return None
    return parse_version(spec, (out + err).decode("utf-8", "replace")[:20000])


# ------------------------------------------------------------------ waive
def waived_by(cfg, engine: str) -> bool:
    """True when the configuration (graph.config.Config) waives dc.engine-incomplete for this engine, with a reason and a date."""
    try:
        return bool(cfg is not None and cfg.is_waived("dc.engine-incomplete", None, engine))
    except AttributeError:
        return False


# ------------------------------------------------------------------ doctor
def applicable(reg: dict, langs: Optional[Set[str]] = None) -> Dict[str, dict]:
    """engine -> {"langs": set, "roles": set, "required": bool} over the languages (None = all)."""
    out: Dict[str, dict] = {}
    for lang, spec in sorted(reg["languages"].items()):
        if langs is not None and lang not in langs:
            continue
        for role in ROLES:
            for name, req in lang_engines(reg, lang, role):
                row = out.setdefault(name, {"langs": set(), "roles": set(), "required": False})
                row["langs"].add(lang)
                row["roles"].add(role)
                row["required"] = row["required"] or req
    return out


def doctor(
    reg: dict,
    lock: Optional[dict],
    *,
    langs: Optional[Set[str]] = None,
    files: Optional[Sequence[str]] = None,
    os_name: Optional[str] = None,
    dirs: Optional[Sequence[str]] = None,
    verify: bool = True,
    versions: bool = False,
    accept: Optional[Callable[[str, str], bool]] = None,
) -> List[dict]:
    """One row per applicable engine: engine, status, langs, roles, path, detail, install, pinned, version. `files` (the project's
    file list) drops engines whose `needs` markers are absent: not applicable is not missing."""
    os_name = os_name or host_os()
    rows = []
    for name, info in sorted(applicable(reg, langs).items()):
        spec = reg["engines"][name]
        if files is not None and spec.get("needs") and not needs_met(spec["needs"], files):
            continue
        res = find_engine(
            reg,
            lock,
            name,
            os_name=os_name,
            dirs=dirs,
            verify=verify,
            accept=accept_for(name) if accept is None else accept,
        )
        row = {
            "engine": name,
            "status": res.status,
            "langs": sorted(info["langs"]),
            "roles": sorted(info["roles"]),
            "path": res.path,
            "detail": res.detail,
            "install": "" if res.status == "ok" else install_line(reg, name, os_name),
            "pinned": None if lock is None else lock_entry(lock, name) is not None,
        }
        if versions and res.path:
            row["version"] = probe_version(spec, res.path)
        rows.append(row)
    return sorted(rows, key=lambda r: (r["status"] == "ok", r["engine"]))


_GO_HELP: Dict[str, bool] = {}


def names_go(exe: str) -> bool:
    """A binary named `deadcode` counts as the Go tool only when its -h output says Go (the PyPI `deadcode` is another tool)."""
    if exe not in _GO_HELP:
        ok = False
        try:
            r = subprocess.run(
                [exe, "-h"], capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
                env={"PATH": safe_path(), "GOTOOLCHAIN": "local", "GOPROXY": "off"},
            )  # fmt: skip
            ok = bool(re.search(rb"\bGo\b|golang", (r.stdout + r.stderr)[:20000]))
        except (OSError, subprocess.SubprocessError):
            ok = False
        _GO_HELP[exe] = ok
    return _GO_HELP[exe]


def accept_for(name: str) -> Optional[Callable[[str, str], bool]]:
    """The veto of the registry's names for an engine: the bare `deadcode` is go-deadcode only when it says Go."""
    if name == "go-deadcode":
        return lambda exe, path: exe != "deadcode" or names_go(path)
    return None


Unit = namedtuple("Unit", "engine langs required path weight needs_build")
# units: [Unit]; degraded and missing: [{engine, langs, detail, install}]
Plan = namedtuple("Plan", "units degraded missing")


def plan(
    reg: dict,
    lock: Optional[dict],
    role: str,
    *,
    langs: Optional[Set[str]] = None,
    files: Optional[Sequence[str]] = None,
    os_name: Optional[str] = None,
    dirs: Optional[Sequence[str]] = None,
    cfg=None,
    verify: bool = True,
) -> Plan:
    """The engines of ONE role for the languages in scope (None = all), each resolved once. units: usable now. missing: applicable
    but not found, not in the lock or not matching it (with the install hint). degraded: no build for this OS, or waived in the
    configuration with a reason and a date. An engine whose `needs` markers are not in `files` is not applicable: it is in none of
    the three lists (not needed is not missing)."""
    os_name = os_name or host_os()
    want: Dict[str, dict] = {}
    for lang in sorted(langs if langs is not None else reg["languages"]):
        for name, req in lang_engines(reg, lang, role):
            row = want.setdefault(name, {"langs": set(), "required": False})
            row["langs"].add(lang)
            row["required"] = row["required"] or req
    units, degraded, missing = [], [], []
    for name, info in sorted(want.items()):
        spec = reg["engines"][name]
        if files is not None and spec.get("needs") and not needs_met(spec["needs"], files):
            continue
        row = {
            "engine": name,
            "langs": sorted(info["langs"]),
            "detail": "",
            "install": install_line(reg, name, os_name),
        }
        if waived_by(cfg, name):
            degraded.append(dict(row, detail="waived in the configuration with a reason and a date", install=""))
            continue
        res = find_engine(reg, lock, name, os_name=os_name, dirs=dirs, verify=verify, accept=accept_for(name))
        if res.status == "ok":
            units.append(
                Unit(
                    name,
                    tuple(row["langs"]),
                    info["required"],
                    res.path,
                    int(spec.get("weight", 1)),
                    bool(spec.get("needs_build")),
                )
            )
        else:
            (degraded if res.status == "degraded" else missing).append(dict(row, detail=f"{res.status}: {res.detail}"))
    return Plan(units, degraded, missing)


def exit_code(rows: Sequence[dict], strict: bool = False) -> int:
    """With `strict`: 2 when an engine of the rows is missing, untrusted or does not match the lock; else 0 (degraded never fails)."""
    return 2 if strict and any(r["status"] in FAILING for r in rows) else 0


def lock_rows(
    reg: dict, dirs: Optional[Sequence[str]] = None, os_name: Optional[str] = None, versions: bool = True
) -> Dict[str, dict]:
    """{engine: {"sha256", "version"}} of every engine of the registry that is found now (no lock applied): what `graph lock` pins."""
    rows: Dict[str, dict] = {}
    for name in sorted(reg["engines"]):
        res = find_engine(reg, None, name, os_name=os_name, dirs=dirs, accept=accept_for(name))
        if res.status != "ok" or not res.path:
            continue
        spec = reg["engines"][name]
        ent = {"sha256": tree_sha256(res.path)}
        ver = probe_version(spec, res.path) if versions and spec.get("kind") != "module" else None
        if ver:
            ent["version"] = ver
        rows[name] = ent
    return rows
