"""dead-code-checker: catch dead code the moment it is written (diff-scoped, deterministic, fast).

It looks at what a commit or a session ADDED and answers one question: can anything reach it? A registered hook that
prints `{}` and never reads its input, a flag nobody reads, a file nobody imports: nothing notices them, because
nothing looks.

Checks (code = `deadcode.<name>`; E blocks, W informs):
    1. unreferenced-symbol / test-only-symbol (W)   a NEW top-level function, class or constant that nothing outside
                                                    its own body mentions (tests count only when the symbol is also
                                                    used outside tests; `tests_count = true` relaxes that).
                                                    Opt-in E for dead Python functions: `function_level = "E"` (and
                                                    `function_level_dirs = [...]` to limit it to those directories);
                                                    classes, constants, test-only symbols and functions of the regex
                                                    languages (js, go, rs, sh) stay W. Default W.
    2. unreferenced-file (E in hooks/ and bin/, W elsewhere)
                                                    a NEW code file that no import, registry (settings.json,
                                                    registry.json, catalog.json), doc, test, cron/systemd unit or
                                                    other file mentions by name.
    3. stub-function / todo-stub (W)                a function whose body is only pass / ... / docstring / return None
                                                    / return {} / raise NotImplementedError / todo!() / TODO.
       stub-hook (E)                                a hook REGISTERED in an agent settings*.json that ignores its
                                                    input and prints a constant or nothing; a stub function that is
                                                    itself registered is E too.
    4. config-key-unread / env-unread (W)           a config key or environment variable the diff adds that no code reads.
    5. cli-flag-unused (W)                          an argparse / click flag defined but never read.

How a symbol is looked up: ONE `git grep -w -F` over the repo for all names at once (tracked + untracked, binary files
skipped), or a stdlib walker outside git. Hits that sit inside the symbol's own definition, in docs (for symbols), in
backups and archives do not count.

Suppress one finding on purpose: `# deadcode: ignore` on the definition line or the line above; a whole file:
`deadcode: ignore-file` in its first 10 lines. Repo config: table [deadcode] of `.dead-code-checker.toml`
(`dead-code-checker defaults` prints the keys). Unknown keys are an error, never ignored.

Entry points:
    check_staged(repo)        -> [Finding]   the index against HEAD (pre-commit)
    check_session(paths)      -> [Finding]   the working tree against HEAD
    scan(paths, root=...)     -> [Finding]   everything under paths counts as new (audits)
        scan(..., foreign=True, languages=[...], public_api="report|exempt|as-private", jobs=N) audits a tree that is
        not yours: every supported language (Java C C++ C# Kotlin Swift Scala Dart PHP in group A, Ruby Lua Elixir
        Haskell R in group B, CSS/SCSS/Less selectors in group C, plus Python, shell, JavaScript, Go and Rust), the
        target's own config file is not policy, git never runs in it (its .git is hostile data: the tree is walked,
        HEAD is read from the repository files), symlinks and vendor/generated files are never read, nothing is
        written inside the tree and no finding is an E. An exported symbol nothing in the tree references is
        deadcode.unreferenced-public-symbol (W) in `report` mode, silent in `exempt`, an ordinary unreferenced-symbol
        in `as-private`. staged and session stay on the five core languages unless [deadcode] languages says more.
    session_hook_output(fs)   -> dict|None   hookSpecificOutput.additionalContext for those findings
CLI: dead-code-checker staged | session PATH... | scan PATH... [--report FILE] [--foreign] [--lang A,B,java]
[--public-api MODE] [--jobs N] | defaults ; exit 1 on any E, silent on pass.

Known limits (by design): names are matched textually, so a dynamic use (getattr with a built string, a plugin loader)
is invisible and needs the ignore marker; a mention in a trailing comment or a string counts as a use (a line that is
only a comment does not); methods are checked for TODO-stubs only; click/argparse are the only CLI frameworks understood;
YAML is read by regex. In the extra languages only top-level declarations are read (no methods), annotation/attribute-
managed types (Spring, ASP.NET, Android, Rails ...) count as used, reflection and framework conventions are invisible,
and a CSS class built at run time needs `deadcode: ignore`. Precision over recall: when in doubt the symbol counts as used.

Stdlib only, Python >= 3.11.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import contextlib
import fnmatch
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from collections import namedtuple
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:  # pragma: no cover - python < 3.11 has no TOML reader
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None


from . import langs as DLANGS
from . import safepath


def _workers(jobs: Optional[int]) -> int:
    """Worker count: --jobs, else $DEAD_CODE_CHECKER_JOBS, else the CPUs (at most 8)."""
    if jobs is None:
        try:
            jobs = int(os.environ.get("DEAD_CODE_CHECKER_JOBS", ""))
        except ValueError:
            jobs = min(os.cpu_count() or 1, 8)
    return max(1, jobs)


def _run_units(units, fn, workers: int):
    """fn over units in a process pool; the results come back in the order of `units`."""
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, units))


CORE_LANGS = ("py", "sh", "js", "go", "rs")
EXTRA_LANG_TAGS = ("java", "c", "cs", "kt", "swift", "scala", "dart", "php", "rb", "lua", "ex", "hs", "r", "css")
LANG_GROUP_TAGS = ("core", "A", "B", "C", "all")
PUBLIC_API_MODES = ("report", "exempt", "as-private")


class _Skipped(dict):
    """reason -> count of the files a scan did not read. The same file is met by the change walker and by the reference
    walker; `note` counts one (reason, file) once, so a symlink or a FIFO shows up as 1, not 2."""

    def __init__(self, init=None):
        super().__init__(init or {})
        self.seen: Set[Tuple[str, str]] = set(getattr(init, "seen", ()))

    def note(self, reason: str, path: str) -> None:
        key = (reason, os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path)))
        if key not in self.seen:
            self.seen.add(key)
            self[reason] = self.get(reason, 0) + 1


def _note(sk: Dict[str, int], reason: str, path: str) -> None:
    if isinstance(sk, _Skipped):
        sk.note(reason, path)
    else:
        sk[reason] = sk.get(reason, 0) + 1


LAST_STATS: dict = {}  # filled by every analysis (per-language counts, skipped reasons); read by --report
CONFIG_FILES = (".dead-code-checker.toml",)
MAX_FILE_BYTES = 1_000_000
MAX_HITS_PER_NAME = 400

DEFAULTS = {
    "enabled": True,
    "tests_count": False,  # a reference from a test counts even when nothing else uses the symbol
    "docs_count": False,  # a mention in a doc counts for symbols (files always count docs)
    "strict_dirs": ["hooks", "bin"],  # an unreferenced NEW file here is E
    "function_level": "W",  # level of unreferenced-symbol for a Python function: "W" (default) or "E"
    "function_level_dirs": [],  # [] = whole repo; else E only inside these directories (repo-relative path prefix)
    "languages": list(CORE_LANGS),  # exact set analysed; add java/c/cs/kt/swift/scala/dart/php/rb/lua/ex/hs/r/css or A|B|C|all
    "exempt_dirs": [
        "tests",
        "test",
        "fixtures",
        "testdata",
        "docs",
        "examples",
        "staging",
        "templates",
        "archive",
        "_archived",
        "vendor",
        "third_party",
        "node_modules",
        "migrations",
    ],
    "ignore": [],  # symbol-name globs never reported
    "ignore_paths": [],  # path globs never looked at
    "config_files": [
        "*config*",
        "*settings*",
        "*flags*",
        "*options*",
        "*defaults*",
        "*features*",
        "*.toml",
        "*.ini",
        "*.properties",
    ],  # files whose NEW keys must be read by code
}
_LIST_KEYS = ("strict_dirs", "exempt_dirs", "ignore", "ignore_paths", "config_files", "function_level_dirs", "languages")
_BOOL_KEYS = ("enabled", "tests_count", "docs_count")

REF_SKIP_GLOBS = (
    "*.bak",
    "*.bak-*",
    "*.bak.*",
    "*.orig",
    "*.rej",
    "*.pre-*",
    "*~",
    "*.swp",
    "*.patch",
    "*.diff",
    "*.log",
    "*.jsonl",
    "*.lock",
    "package-lock.json",
    "*.min.js",
    "*.pyc",
    "*.map",
    "archive/*",
    "*/archive/*",
    "_archived/*",
    "*/_archived/*",
    "staging/*",
    "*/staging/*",
    "node_modules/*",
    "*/node_modules/*",
    ".git/*",
    "*/.git/*",
)
SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    "target",
    ".cache",
    ".idea",
    ".next",
    ".ruff_cache",
}
SECRET_RE = re.compile(
    r"(^|/)(\.env(?!\.(example|sample|template|dist)$)(\..+)?|\.netrc|\.pgpass|id_[a-z0-9]+|.*\.(pem|key|p12|pfx|jks|kdbx|gpg|asc)|\.credentials\.json|"
    r"credentials(\.json)?|\.git-credentials|\.npmrc|\.pypirc|\.ssh|\.gnupg)(/|$)",
    re.I,
)
ENV_EXAMPLE_RE = re.compile(r"(^|/)\.env\.(example|sample|template|dist)$|\.env\.(example|sample)$", re.I)

TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs", "testdata", "fixtures", "e2e"}
TEST_SUFFIXES = (
    "_test.py",
    "_test.go",
    "_test.sh",
    "_spec.rb",
    ".test.js",
    ".test.ts",
    ".spec.js",
    ".spec.ts",
    ".test.mjs",
    ".test.jsx",
    ".test.tsx",
    ".spec.jsx",
    ".spec.tsx",
)
DOC_EXT = {".md", ".rst", ".txt", ".adoc", ".markdown"}
LANG_EXT = {
    ".py": "py",
    ".sh": "sh",
    ".bash": "sh",
    ".zsh": "sh",
    ".js": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".jsx": "js",
    ".ts": "js",
    ".tsx": "js",
    ".go": "go",
    ".rs": "rs",
}
ALL_LANG_EXT = dict(LANG_EXT)
if DLANGS is not None:
    ALL_LANG_EXT.update(DLANGS.LANG_EXT_EXTRA)
CONFIG_EXT = {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".properties"}
CONFIG_SKIP_NAMES = (
    "pyproject.toml",
    "setup.cfg",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "package.json",
    "package-lock.json",
    "tsconfig*.json",
    "jsconfig*.json",
    ".pre-commit-config.yaml",
    "mkdocs.yml",
    "*.lock",
    "settings.json",
    "settings.local.json",
    "managed-settings.json",
    ".mcp.json",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose*.yml",
    "compose*.yaml",
    "tox.ini",
    "pytest.ini",
    "mypy.ini",
    ".flake8",
    "registry.json",
    "catalog.json",
    "hooks.json",
    "plugin.json",
    "marketplace.json",
    "ansible.cfg",
    "managed-settings*.json",
    ".eslintrc*",
    ".prettierrc*",
    "renovate.json",
    "dependabot.yml",
    "*.schema.json",
    "tsconfig.json",
    "deno.json",
    "bunfig.toml",
    "uv.lock",
    "poetry.lock",
    ".golangci.yml",
    ".golangci.yaml",
    ".dead-code-checker.toml",
)
CONFIG_SKIP_DIRS = {
    "tests",
    "test",
    "fixtures",
    "testdata",
    "staging",
    "docs",
    "doc",
    "data",
    "node_modules",
    ".github",
    ".gitlab",
    "schemas",
    "schema",
    "archive",
    "_archived",
    "templates",
    "examples",
    "vendor",
}
ENTRY_NAMES = {
    "__init__.py",
    "__main__.py",
    "conftest.py",
    "setup.py",
    "manage.py",
    "wsgi.py",
    "asgi.py",
    "noxfile.py",
    "fabfile.py",
    "tasks.py",
    "index.js",
    "index.ts",
    "index.mjs",
    "index.tsx",
    "main.go",
    "main.rs",
    "build.rs",
    "mod.rs",
    "lib.rs",
    "gulpfile.js",
}
STD_ENV = {
    "PATH",
    "HOME",
    "USER",
    "SHELL",
    "LANG",
    "TERM",
    "TZ",
    "PWD",
    "OLDPWD",
    "TMPDIR",
    "PYTHONPATH",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
    "NODE_ENV",
    "NODE_PATH",
    "LD_LIBRARY_PATH",
    "EDITOR",
    "PAGER",
    "DISPLAY",
    "HOSTNAME",
    "LOGNAME",
    "VIRTUAL_ENV",
    "GOPATH",
    "GOFLAGS",
    "CGO_ENABLED",
    "IFS",
    "PS1",
    "UID",
    "SHLVL",
    "MANPATH",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "LC_ALL",
    "LC_CTYPE",
    "COLORTERM",
    "NO_COLOR",
    "FORCE_COLOR",
    "CI",
    "DEBIAN_FRONTEND",
    "PIP_NO_CACHE_DIR",
}
REGISTRY_NAMES = {
    "settings.json",
    "settings.local.json",
    "managed-settings.json",
    "registry.json",
    "catalog.json",
    "hooks.json",
    "plugin.json",
}
IGNORE_MARK = "deadcode: ignore"
IGNORE_FILE_MARK = "deadcode: ignore-file"

if DLANGS is not None:
    ENTRY_NAMES |= set(DLANGS.ENTRY_NAMES_EXTRA)
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TODO_RE = re.compile(r"#\s*(TODO|FIXME|XXX|HACK)\b|//\s*(TODO|FIXME|XXX|HACK)\b", re.I)
CONST_RE = re.compile(r"^_?[A-Z][A-Z0-9_]{2,}$")

Hit = namedtuple("Hit", "path line text")


# ------------------------------------------------------------------ findings
class Finding:
    __slots__ = ("level", "path", "line", "code", "msg")

    def __init__(self, level: str, path: str, line: int, code: str, msg: str):
        self.level, self.path, self.line, self.code, self.msg = level, path, line, code, msg

    def render(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        return f"deadcode: {self.level} {loc}: {self.code}: {self.msg}"

    def as_dict(self) -> dict:
        return {"level": self.level, "path": self.path, "line": self.line, "code": self.code, "msg": self.msg}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return self.render()


def _sorted(findings: Iterable[Finding]) -> List[Finding]:
    seen, out = set(), []
    for f in sorted(findings, key=lambda f: (f.level != "E", f.path, f.line, f.code, f.msg)):
        k = (f.level, f.path, f.line, f.code, f.msg)
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


# ------------------------------------------------------------------ small helpers
# a scanned tree may be foreign: its .git/config must not run anything (core.fsmonitor is a command), open a pager or
# fetch; harmless for our own repositories
GIT_SAFE = (
    "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat", "-c", "protocol.allow=never",
)  # fmt: skip


def _git_env(root: str) -> Optional[dict]:
    """Environment for a git run in `root`. When `root` holds its own .git, the work tree is pinned to it: the
    repository's config (core.worktree) is data of the scanned tree and must not move the base to a directory the tree's
    author chose (-c cannot override it, GIT_WORK_TREE can). None = inherit as is (safepath.run gives git a PATH of absolute
    entries either way)."""
    if root and os.path.exists(os.path.join(root, ".git")):
        env = dict(os.environ)
        env["GIT_WORK_TREE"] = os.path.realpath(root)
        return env
    return None


_GIT_DIR_FILES = ("HEAD", "config", "config.worktree", "index", "packed-refs", "commondir", "shallow")
_FOREIGN_GIT = [0]  # >0 while a foreign tree is audited: its .git is hostile data
_GIT_BLOCKED: Dict[str, bool] = {}


@contextlib.contextmanager
def foreign_git(on: bool = True):
    """Within the block git runs against a repository of a foreign tree are guarded by git_blocked()."""
    if not on:
        yield
        return
    _FOREIGN_GIT[0] += 1
    _GIT_BLOCKED.clear()
    try:
        yield
    finally:
        _FOREIGN_GIT[0] -= 1
        _GIT_BLOCKED.clear()


def _git_dir_of(root: str) -> Optional[str]:
    near = nearest_git_root(root)
    if not near:
        return None
    g = os.path.join(near, ".git")
    if os.path.isdir(g):
        return g
    st, data = read_regular(g)  # a gitfile: "gitdir: <path>"
    if st == "ok" and data and data[:8] == b"gitdir: ":
        tgt = data[8:].split(b"\n", 1)[0].decode("utf-8", "replace").strip()
        return tgt if os.path.isabs(tgt) else os.path.normpath(os.path.join(near, tgt))
    return None


_HEX40 = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


def read_head(root: str) -> Optional[str]:
    """The commit id HEAD names, read from the repository files without running git (a foreign repository is hostile
    data). Only regular files are opened (read_regular); a symbolic ref is resolved through one loose ref file or
    packed-refs; None when anything is odd (detached into a bad value, a ref with '..', a FIFO ...)."""
    gd = _git_dir_of(root)
    if not gd:
        return None
    st, data = read_regular(os.path.join(gd, "HEAD"))
    if st != "ok" or not data or len(data) > 4096:
        return None
    ref = data.decode("utf-8", "replace").strip()
    if _HEX40.match(ref):
        return ref
    if not ref.startswith("ref: "):
        return None
    name = ref[5:].strip()
    if not name.startswith("refs/") or ".." in name.split("/") or "\0" in name:
        return None
    st, data = read_regular(os.path.join(gd, *name.split("/")))
    if st == "ok" and data and len(data) < 200:
        v = data.decode("utf-8", "replace").strip()
        return v if _HEX40.match(v) else None
    st, data = read_regular(os.path.join(gd, "packed-refs"))
    if st == "ok" and data and len(data) <= 5_000_000:
        for ln in data.decode("utf-8", "replace").split("\n"):
            parts = ln.split(" ", 1)
            if len(parts) == 2 and parts[1].strip() == name and _HEX40.match(parts[0]):
                return parts[0]
    return None


def git_blocked(root: str) -> bool:
    """True when git must not run in `root`; the caller then does without it (it walks the tree). Two cases.
    A foreign tree (inside foreign_git): its repository is hostile data. git opens more nodes than can be listed
    (info/exclude, objects/info/alternates, every .gitignore and .gitattributes of the tree, the commondir config and
    all [include] targets of it), any of them may be a FIFO, and a git call then stalls for its whole timeout; a config
    may also run foreign configuration. So git never runs there.
    Our own tree: a repository file that is a FIFO, socket or device (git would wait for a writer)."""
    if _FOREIGN_GIT[0] > 0:
        return True
    key = os.path.realpath(root)
    hit = _GIT_BLOCKED.get(key)
    if hit is not None:
        return hit
    bad = False
    gd = _git_dir_of(root)
    if gd:
        for name in _GIT_DIR_FILES:
            try:
                mode = os.stat(os.path.join(gd, name)).st_mode
            except OSError:
                continue
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                bad = True
                break
    _GIT_BLOCKED[key] = bad
    return bad


def _git(root: str, *args: str, inp: Optional[bytes] = None) -> Tuple[int, bytes]:
    if git_blocked(root):
        return 127, b""
    try:
        r = safepath.run(
            ["git", "-C", root, *GIT_SAFE, "-c", "core.quotepath=false", *args],
            input=inp,
            env=_git_env(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, b""


def nearest_git_root(start: str) -> Optional[str]:
    """The closest ancestor (or `start` itself) that holds a .git entry, found without running git."""
    d = os.path.realpath(start)
    if not os.path.isdir(d):
        d = os.path.dirname(d)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def toplevel(start: str, foreign: bool = False) -> Optional[str]:
    """Top of the repository holding `start`. The directory that owns the .git entry wins over what git reports: a
    scanned tree may set core.worktree to anywhere. foreign=True (a tree that is not ours): git never runs, only the
    .git entry search; a tree shaped like a bare repository would make git read its config and [include] targets."""
    if not safepath.which("git"):
        return None
    near = nearest_git_root(start)
    if near:
        return near
    if foreign or _FOREIGN_GIT[0] > 0:
        return None
    d = start if os.path.isdir(start) else os.path.dirname(os.path.abspath(start))
    rc, out = _git(d, "rev-parse", "--show-toplevel")
    return os.path.realpath(out.decode("utf-8", "replace").strip()) if rc == 0 and out.strip() else None


def _under_dir(path: str, d: str) -> bool:
    """`path` (repo-relative, forward slashes) lies inside directory `d` of function_level_dirs: a prefix match on whole
    path components, so "lib" covers lib/x.py and lib/sub/y.py but not other/lib/z.py or libx/w.py."""
    d = d.replace("\\", "/").strip().strip("/")
    while d.startswith("./"):
        d = d[2:].lstrip("/")
    if d == ".":
        return True
    return bool(d) and (path == d or path.startswith(d + "/"))


def _decode(b: bytes) -> Optional[str]:
    if b"\0" in b[:8192] or len(b) > MAX_FILE_BYTES:
        return None
    # a UTF-8 BOM (Windows editors write it) makes ast.parse fail on the first character
    return b.decode("utf-8", "replace").lstrip("\ufeff")


def read_regular(path: str) -> Tuple[str, Optional[bytes]]:
    """('ok', bytes) | ('special', None) | ('big', None) | ('gone', None). Opens non-blocking and checks the opened
    descriptor, so a FIFO or a device (a tarball can carry one, a symlinked directory can lead to one) is never
    read and cannot hang a scan; a file bigger than MAX_FILE_BYTES is not read either."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return "gone", None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "special", None
        if st.st_size > MAX_FILE_BYTES:
            return "big", None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return "ok", fh.read()
    except OSError:
        return "gone", None
    finally:
        os.close(fd)


def _unread(path: str) -> Optional["Changed"]:
    """A code file that cannot be read (binary or over the size limit) still gets a visible notice."""
    if os.path.splitext(path)[1].lower() in LANG_EXT and not SECRET_RE.search(path):
        return Changed(path, "", None, None, True, skipped=f"binary or larger than {MAX_FILE_BYTES // 1000} KB")
    return None


def _parts(path: str) -> List[str]:
    return [p for p in path.replace("\\", "/").split("/") if p]


def is_test(path: str) -> bool:
    parts = _parts(path)
    name = parts[-1].lower() if parts else ""
    if any(p.lower() in TEST_DIRS for p in parts[:-1]):
        return True
    if name.startswith("test_") or name == "conftest.py" or name.endswith(TEST_SUFFIXES):
        return True
    return DLANGS is not None and (
        name.endswith(DLANGS.TEST_SUFFIXES_EXTRA)
        or bool(DLANGS.TEST_CAMEL_RE.search(parts[-1]))
        or (name.startswith("test-") and name.endswith(".r"))  # testthat
    )


def is_doc(path: str) -> bool:
    parts = _parts(path)
    ext = os.path.splitext(parts[-1])[1].lower() if parts else ""
    if ext in DOC_EXT:
        return True
    # a code file under a package that happens to be called doc/ is code, not documentation
    return any(p.lower() in ("docs", "doc") for p in parts[:-1]) and ext not in ALL_LANG_EXT and ext not in CONFIG_EXT


def lang_of(path: str, text: Optional[str] = None, extra: bool = False) -> Optional[str]:
    """extra=True also knows the languages of the langs module (the caller then filters by the enabled set)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in LANG_EXT:
        return LANG_EXT[ext]
    if extra and DLANGS is not None:
        if ext in DLANGS.LANG_EXT_EXTRA:
            return DLANGS.LANG_EXT_EXTRA[ext]
        by_name = DLANGS.lang_of_name(os.path.basename(path))
        if by_name:
            return by_name
    if ext == "" and text:
        first = text.split("\n", 1)[0]
        if first.startswith("#!"):
            if "python" in first:
                return "py"
            if re.search(r"\b(ba|z|da)?sh\b", first):
                return "sh"
            if "node" in first:
                return "js"
            if extra and DLANGS is not None:
                if re.search(r"\bruby\b", first):
                    return "rb"
                if re.search(r"\blua(jit)?\b", first):
                    return "lua"
                if re.search(r"\bRscript\b", first):
                    return "r"
    return None


def _glob_any(path: str, globs: Sequence[str]) -> bool:
    base = os.path.basename(path)
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(base, g) for g in globs)


def is_backup(path: str) -> bool:
    return _glob_any(path.replace("\\", "/"), REF_SKIP_GLOBS)


def has_ignore_file(text: str) -> bool:
    return IGNORE_FILE_MARK in "\n".join(text.split("\n", 10)[:10])


def line_ignored(lines: Sequence[str], line: int) -> bool:
    for ln in (line, line - 1):
        if 1 <= ln <= len(lines) and IGNORE_MARK in lines[ln - 1] and IGNORE_FILE_MARK not in lines[ln - 1]:
            return True
    return False


# ------------------------------------------------------------------ config
def load_config(root: str, use_file: bool = True, foreign: bool = False) -> Tuple[dict, List[Finding]]:
    """use_file=False: the defaults only (a foreign repository's own config file is not our policy).
    The file is read with read_regular (a FIFO cannot hang the run). foreign + use_file (--use-repo-config): a
    symlink or a file whose real path leaves the root is not read."""
    cfg = json.loads(json.dumps(DEFAULTS))
    problems: List[Finding] = []
    raw, where = {}, None
    for rel in CONFIG_FILES if use_file else ():
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            where = rel
            if tomllib is None:
                problems.append(
                    Finding(
                        "W",
                        rel,
                        0,
                        "deadcode.bad-config",
                        "python < 3.11 cannot read TOML: defaults used (upgrade python)",
                    )
                )
                break
            if foreign:
                rp = os.path.realpath(p)
                rr = os.path.realpath(root)
                if os.path.islink(p) or not (rp == rr or rp.startswith(rr + os.sep)):
                    problems.append(
                        Finding("W", rel, 0, "deadcode.bad-config", "not read: a symlink or a path outside the scanned tree")
                    )
                    break
            st, blob = read_regular(p)
            if st != "ok" or blob is None:
                problems.append(Finding("W", rel, 0, "deadcode.bad-config", f"not read: {st} file"))
                break
            try:
                doc = tomllib.loads(blob.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as e:
                problems.append(Finding("E", rel, 0, "deadcode.bad-config", f"cannot read: {str(e)[:120]}"))
                break
            raw = doc.get("deadcode", {})
            if not isinstance(raw, dict):
                problems.append(Finding("E", rel, 0, "deadcode.bad-config", "[deadcode] must be a table"))
                raw = {}
            break
    for k, v in raw.items():
        if k not in DEFAULTS:
            problems.append(
                Finding(
                    "E",
                    where or "",
                    0,
                    "deadcode.bad-config",
                    f"unknown key {k!r} in [deadcode] (known: {', '.join(sorted(DEFAULTS))})",
                )
            )
            continue
        ok = True
        if k in _BOOL_KEYS:
            ok = isinstance(v, bool)
        elif k in _LIST_KEYS:
            ok = isinstance(v, list) and all(isinstance(x, str) for x in v)
        elif k == "function_level":
            ok = v in ("W", "E")
        if ok and k == "languages":
            ok = bool(v) and all(x in CORE_LANGS + EXTRA_LANG_TAGS + LANG_GROUP_TAGS for x in v)
        if ok:
            cfg[k] = v
        else:
            problems.append(Finding("E", where or "", 0, "deadcode.bad-config", f"bad value for {k!r} in [deadcode]"))
    return cfg, problems


# ------------------------------------------------------------------ changes (what is new)
class Changed:
    __slots__ = ("path", "text", "base", "added", "new", "skipped")

    def __init__(
        self,
        path: str,
        text: str,
        base: Optional[str],
        added: Optional[Set[int]],
        new: bool,
        skipped: Optional[str] = None,
    ):
        # added = set of added line numbers, None = every line; base = previous text or None; new = file did not exist
        # skipped = why the file could not be read (binary, too large): reported, never dropped silently
        self.path, self.text, self.base, self.added, self.new = path, text, base, added, new
        self.skipped = skipped

    def touches(self, a: int, b: int) -> bool:
        return self.added is None or not self.added.isdisjoint(range(a, b + 1))


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _parse_added(diff: bytes) -> Dict[str, Set[int]]:
    out: Dict[str, Set[int]] = {}
    cur: Optional[str] = None
    for raw in diff.decode("utf-8", "replace").split("\n"):
        if raw.startswith("+++ "):
            p = raw[4:].split("\t")[0]
            cur = p[2:] if p.startswith("b/") else None
            if cur is not None:
                out.setdefault(cur, set())
        elif raw.startswith("@@") and cur is not None:
            m = _HUNK_RE.match(raw)
            if m:
                start, n = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
                out[cur].update(range(start, start + n))
    return out


def _cat_batch(root: str, specs: Sequence[str]) -> Dict[str, Optional[bytes]]:
    """Blob contents for object names (`:path`, `HEAD:path`, `<sha>`) with ONE git process; missing = None."""
    specs = [x for x in dict.fromkeys(specs) if "\n" not in x]
    if not specs:
        return {}
    if git_blocked(root):
        return {x: None for x in specs}
    try:
        r = safepath.run(
            ["git", "-C", root, *GIT_SAFE, "cat-file", "--batch"],
            input=("\n".join(specs) + "\n").encode("utf-8"),
            env=_git_env(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return {x: None for x in specs}
    out, pos, res = r.stdout, 0, {}
    for spec in specs:
        nl = out.find(b"\n", pos)
        if nl < 0:
            res[spec] = None
            continue
        head = out[pos:nl].decode("utf-8", "replace").split(" ")
        pos = nl + 1
        if len(head) != 3 or not head[2].isdigit():
            res[spec] = None  # "<spec> missing" or "ambiguous"
            continue
        size = int(head[2])
        res[spec] = out[pos : pos + size] if head[1] == "blob" else None
        pos += size + 1
    return res


_NULL_SHA = re.compile(r"^0+$")


def staged_changes(root: str) -> List[Changed]:
    rc, out = _git(root, "diff", "--cached", "--raw", "-z", "-M", "--diff-filter=ACMR", "--no-abbrev")
    if rc != 0 or not out:
        return []
    toks = out.decode("utf-8", "replace").split("\0")
    entries = []  # (status, old path, new path, old sha)
    i = 0
    while i < len(toks) and toks[i].startswith(":"):
        meta = toks[i][1:].split(" ")
        st = meta[4][0] if len(meta) >= 5 and meta[4] else "M"
        if st in "RC":
            entries.append((st, toks[i + 1], toks[i + 2], meta[2]))
            i += 3
        else:
            entries.append((st, toks[i + 1], toks[i + 1], meta[2]))
            i += 2
    _rc, diff = _git(root, "diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "-M", "--diff-filter=ACMR")
    added = _parse_added(diff)
    blobs = _cat_batch(
        root,
        [f":{new}" for _st, _old, new, _sha in entries]
        + [sha for _s, _o, _n, sha in entries if not _NULL_SHA.match(sha)],
    )
    res = []
    for st, _old, new, old_sha in entries:
        raw = blobs.get(f":{new}")
        text = _decode(raw) if raw is not None else None
        if text is None:
            u = _unread(new) if raw is not None else None
            if u is not None:
                res.append(u)
            continue
        base = None
        if not _NULL_SHA.match(old_sha) and st in "MR":
            braw = blobs.get(old_sha)
            base = _decode(braw) if braw is not None else None
        is_new = st in "AC" or (st == "M" and base is None)
        a = added.get(new)
        res.append(Changed(new, text, base, None if (a is None or (is_new and st != "R")) else a, is_new))
    return res


def session_changes(root: str, paths: Sequence[str], in_git: bool = True) -> List[Changed]:
    rels: List[str] = []
    for p in paths:
        ap = os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p))
        if os.path.isfile(ap) and (ap == root or ap.startswith(root + os.sep)):
            rels.append(os.path.relpath(ap, root).replace(os.sep, "/"))
    if not rels:
        return []
    tracked: Set[str] = set()
    added: Dict[str, Set[int]] = {}
    if in_git:
        rc, out = _git(root, "ls-files", "-z", "--", *rels)
        tracked = {x for x in out.decode("utf-8", "replace").split("\0") if x} if rc == 0 else set()
        if tracked:
            _rc, diff = _git(root, "diff", "HEAD", "-U0", "--no-color", "--no-ext-diff", "--", *sorted(tracked))
            added = _parse_added(diff)
    heads = _cat_batch(root, [f"HEAD:{r}" for r in sorted(tracked)]) if tracked else {}
    res = []
    for rel in sorted(set(rels)):
        try:
            with open(os.path.join(root, rel), "rb") as fh:
                text = _decode(fh.read())
        except OSError:
            continue
        if text is None:
            u = _unread(rel)
            if u is not None:
                res.append(u)
            continue
        base = None
        if rel in tracked:
            braw = heads.get(f"HEAD:{rel}")
            base = _decode(braw) if braw is not None else None
        if base is None:
            res.append(Changed(rel, text, None, None, True))
        else:
            res.append(Changed(rel, text, base, added.get(rel, set()), False))
    return res


def all_new_changes(
    root: str,
    paths: Sequence[str],
    skip_dirs: Optional[Set[str]] = None,
    foreign: bool = False,
    skipped: Optional[Dict[str, int]] = None,
) -> List[Changed]:
    """Every readable text file under paths, treated as new (audits). foreign: a symlink, or a file whose real path
    leaves the root, is never read (counted in `skipped`); skip_dirs are pruned (counted as vendor)."""
    res, seen = [], set()
    skip = SKIP_DIRS if skip_dirs is None else skip_dirs
    skipped = skipped if skipped is not None else {}
    for p in paths:
        ap = os.path.realpath(os.path.abspath(p))  # a relative path is relative to the cwd, as in scan_base
        files = []
        if os.path.isfile(ap):
            files = [ap]
        else:
            for d, dirs, names in os.walk(ap):
                kept = sorted(x for x in dirs if x not in skip)
                if foreign:
                    _note_linked_dirs(skipped, d, dirs, ap)
                pruned = sum(1 for x in dirs if x in skip and x not in SKIP_DIRS)
                if pruned:
                    skipped["vendor-dirs"] = skipped.get("vendor-dirs", 0) + pruned
                dirs[:] = kept
                files += [os.path.join(d, n) for n in sorted(names)]
        for f in files:
            if f in seen or SECRET_RE.search(f.replace(os.sep, "/")):
                continue
            seen.add(f)
            if foreign and (os.path.islink(f) or not os.path.realpath(f).startswith(root + os.sep)):
                _note(skipped, "symlink", f)
                continue
            state, data = read_regular(f)
            if state == "special":
                _note(skipped, "special", f)
                continue
            if state == "gone":
                continue
            text = _decode(data) if data is not None else None
            rel = os.path.relpath(f, root).replace(os.sep, "/") if f.startswith(root + os.sep) else f
            if text is None:
                u = _unread(rel)
                if u is not None:
                    res.append(u)
                continue
            res.append(Changed(rel, text, None, None, True))
    return res


def _note_linked_dirs(sk: Dict[str, int], d: str, dirs: Sequence[str], root: str) -> None:
    """A foreign walk never follows a symlinked directory (os.walk does not); count each one so it is not silent:
    'outside-root' when it leads out of the tree, else 'symlink'."""
    for x in dirs:
        p = os.path.join(d, x)
        if os.path.islink(p):
            rp = os.path.realpath(p)
            _note(sk, "outside-root" if not (rp == root or rp.startswith(root + os.sep)) else "symlink", p)


# ------------------------------------------------------------------ reference index
def _name_regex(name: str) -> "re.Pattern[str]":
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")


def _is_word(c: str) -> bool:
    return c.isalnum() or c == "_"


class RefIndex:
    """Where does each name occur? One query for all names.

    On a git toplevel with up to GIT_MAX_NAMES names: `git grep -P` with one whole-word alternation (measured: 300
    names over the 2.6 GB dev checkout 0.3 s; `-F -w -f` with 824 patterns took 158 s and `-E` 2.9 s, so those are not
    used; ~3000 names in chunks took 370 s because of the hits to parse). Otherwise, without git, or when git lacks
    PCRE, each candidate file is read once: the list comes from `git ls-files`
    (tracked + untracked, .gitignore honoured) or os.walk, and a file is searched only for the names whose token
    occurs in it. Both give the same hits.
    """

    GIT_CHUNK = 300
    GIT_MAX_NAMES = 300  # more names mean far more hits to parse; the walker caps hits per file and wins there
    PER_FILE = 3

    def __init__(
        self,
        targets: Sequence[str],
        backend: str = "auto",
        foreign: bool = False,
        jobs: int = 1,
        skip_dirs: Optional[Set[str]] = None,
        skipped: Optional[Dict[str, int]] = None,
    ):
        self.targets = [os.path.realpath(t) for t in targets]
        self.skipped = skipped if skipped is not None else {}  # reasons a candidate file was not read (shared with stats)
        self.backend = backend  # auto | git | walk
        self.foreign, self.jobs = foreign, jobs  # foreign: nothing that resolves outside the target is read
        self.skip_dirs = set(SKIP_DIRS) if skip_dirs is None else set(skip_dirs)

    def find(self, names: Iterable[str], focus: Iterable[str] = (), full: Iterable[str] = ()) -> Dict[str, List[Hit]]:
        """focus: real paths of the changed files (every occurrence recorded there, for definition ranges);
        full: names whose every occurrence is wanted (line text is inspected). Elsewhere a name is recorded at most
        PER_FILE times per file: one live hit is all a reference check needs."""
        names = sorted({n for n in names if n})
        focus_set, full_set = {os.path.realpath(f) for f in focus}, set(full)
        res: Dict[str, List[Hit]] = {n: [] for n in names}
        if not names:
            return res
        simple = {n for n in names if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n)}
        special = {n: _name_regex(n) for n in names if n not in simple}
        counts: Dict[Tuple[str, int], int] = {}

        def add(name: str, path: str, lineno: int, text: str) -> None:
            cat = 0 if is_test(path) else (1 if is_doc(path) else 2)
            k = (name, cat)
            if counts.get(k, 0) >= MAX_HITS_PER_NAME:
                return
            counts[k] = counts.get(k, 0) + 1
            res[name].append(Hit(path, lineno, text[:400]))

        def feed_line(path: str, lineno: int, text: str) -> None:
            if SECRET_RE.search(path.replace(os.sep, "/")):
                return
            found = (set(TOKEN_RE.findall(text)) & simple) | {n for n, rx in special.items() if rx.search(text)}
            for n in found:
                add(n, path, lineno, text)

        for t in self.targets:
            top = os.path.isdir(t) and toplevel(t) == t
            use_git = self.backend == "git" or (self.backend == "auto" and top and len(names) <= self.GIT_MAX_NAMES)
            if use_git and self._git_grep(t, names, feed_line):
                continue
            self._walk(t, top, simple, special, add, focus_set, full_set)
        return res

    @staticmethod
    def _git_grep(root: str, names: Sequence[str], feed) -> bool:
        """`git grep -P` with one alternation per chunk: a whole-word search for many names in one pass over the repo
        (tracked + untracked, binary files skipped). False when git or its PCRE support is missing: the caller walks."""
        if git_blocked(root):
            return False
        for n in range(0, len(names), RefIndex.GIT_CHUNK):
            alt = "|".join(re.escape(x) for x in sorted(names[n : n + RefIndex.GIT_CHUNK], key=len, reverse=True))
            pattern = "(?<![A-Za-z0-9_])(?:" + alt + ")(?![A-Za-z0-9_])"
            try:
                r = safepath.run(
                    ["git", "-C", root, *GIT_SAFE, "grep", "--untracked", "-I", "-n", "-P", "-z", "-e", pattern, "--", ".",
                     ":(exclude,glob)**/node_modules/**", ":(exclude,glob)**/.git/**"],
                    env=_git_env(root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=120,
                )  # fmt: skip
            except (OSError, subprocess.SubprocessError):
                return False
            if r.returncode not in (0, 1):
                return False
            for rec in r.stdout.split(b"\n"):
                parts = rec.split(b"\0", 2)
                if len(parts) != 3 or not parts[1].isdigit():
                    continue
                feed(
                    os.path.join(root, parts[0].decode("utf-8", "replace")),
                    int(parts[1]),
                    parts[2].decode("utf-8", "replace"),
                )
        return True

    def _files(self, target: str, is_git_top: bool) -> List[str]:
        if os.path.isfile(target):
            return [target]
        skip = self.skip_dirs
        files: Optional[List[str]] = None
        if is_git_top:
            rc, out = _git(target, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
            if rc == 0:
                files = sorted(
                    {
                        os.path.join(target, r)
                        for r in out.decode("utf-8", "replace").split("\0")
                        if r and not any(p in skip for p in _parts(r)[:-1])
                    }
                )
        if files is None:
            files = []
            for d, dirs, fnames in os.walk(target):
                if self.foreign:
                    _note_linked_dirs(self.skipped, d, dirs, target)
                dirs[:] = sorted(x for x in dirs if x not in skip and not SECRET_RE.search(x + "/"))
                files += [os.path.join(d, n) for n in sorted(fnames)]
        return self._readable(target, files)

    def _readable(self, target: str, files: List[str]) -> List[str]:
        """Keep the regular files that may be read. Always: a FIFO, device or socket is dropped (counted 'special'),
        because opening it can block forever. Foreign: also a symlink and any file whose real path leaves the
        target (`git ls-files` lists what the tree's own index says, so `evil/ref.rb` can sit behind a symlinked
        `evil`); both are counted ('symlink', 'outside-root')."""
        sk = self.skipped
        ok_dirs: Dict[str, bool] = {}
        keep = []
        for f in files:
            try:
                st = os.lstat(f)
            except OSError:
                continue
            if self.foreign:
                if stat.S_ISLNK(st.st_mode):
                    _note(sk, "symlink", f)
                    continue
                d = os.path.dirname(f)
                if d not in ok_dirs:
                    rd = os.path.realpath(d)
                    ok_dirs[d] = rd == target or rd.startswith(target + os.sep)
                if not ok_dirs[d]:
                    _note(sk, "outside-root", f)
                    continue
            if not stat.S_ISREG(st.st_mode):
                _note(sk, "special", f)
                continue
            keep.append(f)
        return keep

    def _walk(
        self, target: str, is_git_top: bool, simple: Set[str], special: dict, add, focus: Set[str], full: Set[str]
    ) -> None:
        """Read every candidate file once. The per-file work is a pure function (`_file_hits`); with jobs > 1 the
        files go to a process pool in chunks (results ordered by path), and the hits are fed to
        `add` in sorted-path order either way, so jobs=1 and jobs=N give byte-identical results."""
        files = self._files(target, is_git_top)
        n = self.jobs
        if n > 1 and len(files) >= 64:
            size = max(16, -(-len(files) // (n * 4)))
            chunks = [files[i : i + size] for i in range(0, len(files), size)]
            args = (tuple(sorted(simple)), tuple(sorted(special)), tuple(sorted(focus)), tuple(sorted(full)))
            try:
                parts = _run_units([(c, args) for c in chunks], _hits_chunk, n)
            except Exception:  # noqa: BLE001
                parts = [_hits_chunk((c, args)) for c in chunks]
        else:
            args = (tuple(sorted(simple)), tuple(sorted(special)), tuple(sorted(focus)), tuple(sorted(full)))
            parts = [_hits_chunk((files, args))]
        for part in parts:
            for path, hits in part:
                for name, line, text in hits:
                    add(name, path, line, text)


def _hits_chunk(unit):
    """([files], (simple, special, focus, full)) -> [(path, [(name, line, text)])] in file order. Pure."""
    files, (simple_t, special_t, focus_t, full_t) = unit
    simple, focus, full = set(simple_t), set(focus_t), set(full_t)
    special = {n: _name_regex(n) for n in special_t}
    out = []
    for f in files:
        hits = _file_hits(f, simple, special, focus, full)
        if hits:
            out.append((f, hits))
    return out


def _file_hits(f: str, simple: Set[str], special: dict, focus: Set[str], full: Set[str]):
    if SECRET_RE.search(f.replace(os.sep, "/")):
        return []
    state, data = read_regular(f)
    text = _decode(data) if data is not None else None
    if text is None:
        return []
    toks = set(TOKEN_RE.findall(text)) & simple
    spec = [n for n in sorted(special) if n in text]
    if not toks and not spec:
        return []
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    n_text = len(text)
    every = os.path.realpath(f) in focus
    hits: List[Tuple[str, int, str]] = []

    def emit(name: str, off: int) -> None:
        ln = bisect.bisect_right(starts, off)
        end = text.find("\n", off)
        hits.append((name, ln, text[starts[ln - 1] : end if end >= 0 else n_text][:400]))

    for n in sorted(toks):
        pos, L, got = 0, len(n), 0
        limit = None if (every or n in full) else RefIndex.PER_FILE
        while limit is None or got < limit:
            i = text.find(n, pos)
            if i < 0:
                break
            pos = i + L
            if (i == 0 or not _is_word(text[i - 1])) and (pos >= n_text or not _is_word(text[pos])):
                emit(n, i)
                got += 1
    for n in spec:
        for m in special[n].finditer(text):
            emit(n, m.start())
    return hits


_HASH_COMMENT_EXT = {".py", ".sh", ".bash", ".zsh", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".service", ".timer"}
_SLASH_COMMENT_EXT = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".cs"}


if DLANGS is not None:
    _HASH_COMMENT_EXT = _HASH_COMMENT_EXT | set(DLANGS.HASH_COMMENT_EXT)
    _SLASH_COMMENT_EXT = _SLASH_COMMENT_EXT | set(DLANGS.SLASH_COMMENT_EXT)
    _DASH_COMMENT_EXT = set(DLANGS.DASH_COMMENT_EXT)
else:
    _DASH_COMMENT_EXT = set()


def _comment_only(path: str, text: str) -> bool:
    """A line that is nothing but a comment: naming a symbol there is not a use (`# foo is great`)."""
    ext = os.path.splitext(path)[1].lower()
    t = text.lstrip()
    if ext in _HASH_COMMENT_EXT:
        return t.startswith("#") and not t.startswith("#!")
    if ext in _DASH_COMMENT_EXT:
        return t.startswith("--")
    return ext in _SLASH_COMMENT_EXT and t.startswith("//")


def _countable(h: Hit, root_abs: str, extra_skip: Sequence[str] = ()) -> bool:
    rel = os.path.relpath(h.path, root_abs).replace(os.sep, "/") if h.path.startswith(root_abs + os.sep) else h.path
    return not is_backup(rel) and not (extra_skip and _glob_any(rel, extra_skip)) and not _comment_only(h.path, h.text)


# ------------------------------------------------------------------ registries (settings.json hooks, registry.json ...)
class Registry:
    """Strings of settings*.json / registry.json / catalog.json / hooks.json / plugin.json and the hook commands."""

    def __init__(self, root: str, extra_files: Sequence[str] = (), foreign: bool = False):
        self.root = root
        self.skipped: Dict[str, int] = {}
        self.text = ""
        self.hook_cmds: List[str] = []
        files: Set[str] = set()
        rc, out = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        if rc == 0:
            for rel in out.decode("utf-8", "replace").split("\0"):
                if rel and os.path.basename(rel) in REGISTRY_NAMES and rel.count("/") <= 4:
                    files.add(os.path.join(root, rel))
        else:
            for d, dirs, names in os.walk(root):
                dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
                if d[len(root) :].count(os.sep) > 3:
                    dirs[:] = []
                files.update(os.path.join(d, n) for n in names if n in REGISTRY_NAMES)
        files = self._readable(files, foreign)
        for e in extra_files:  # named by the operator (--settings): trusted, but still never a FIFO
            if os.path.isfile(e):
                files.add(os.path.realpath(e))
        strings: List[str] = []
        for f in sorted(files):
            st, raw = read_regular(f)  # non-blocking open + fstat: a FIFO named settings.json cannot hang a scan
            if st != "ok" or raw is None or len(raw) > 5_000_000:
                if st == "special":
                    self.skipped["special"] = self.skipped.get("special", 0) + 1
                continue
            try:
                doc = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            self._collect(doc, strings)
            if isinstance(doc, dict) and isinstance(doc.get("hooks"), dict):
                self._hooks(doc["hooks"])
        self.text = "\n".join(strings)

    def _readable(self, files: Set[str], foreign: bool) -> Set[str]:
        """The registry candidates that may be read. Always: only regular files (a FIFO is counted 'special').
        Foreign: also not a symlink and nothing whose real path leaves the root (same rule as RefIndex._readable),
        so a file outside the tree cannot register hooks and hide a finding."""
        root = os.path.realpath(self.root)
        keep: Set[str] = set()
        ok_dirs: Dict[str, bool] = {}
        for f in files:
            try:
                st = os.lstat(f)
            except OSError:
                continue
            if foreign:
                if stat.S_ISLNK(st.st_mode):
                    self.skipped["symlink"] = self.skipped.get("symlink", 0) + 1
                    continue
                d = os.path.dirname(f)
                if d not in ok_dirs:
                    rd = os.path.realpath(d)
                    ok_dirs[d] = rd == root or rd.startswith(root + os.sep)
                if not ok_dirs[d]:
                    self.skipped["outside-root"] = self.skipped.get("outside-root", 0) + 1
                    continue
            if not (stat.S_ISREG(st.st_mode) or (stat.S_ISLNK(st.st_mode) and not foreign)):
                self.skipped["special"] = self.skipped.get("special", 0) + 1
                continue
            keep.add(f)
        return keep

    @staticmethod
    def _collect(node, out: List[str]) -> None:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, list):
            for x in node:
                Registry._collect(x, out)
        elif isinstance(node, dict):
            for k, v in node.items():
                out.append(str(k))
                Registry._collect(v, out)

    def _hooks(self, hooks: dict) -> None:
        for groups in hooks.values():
            for g in groups if isinstance(groups, list) else []:
                for h in (g.get("hooks", []) if isinstance(g, dict) else []):
                    if isinstance(h, dict) and isinstance(h.get("command"), str):
                        self.hook_cmds.append(h["command"])

    def mentions_file(self, rel: str) -> bool:
        base = os.path.basename(rel)
        return bool(base and re.search(r"(?<![\w.-])" + re.escape(base) + r"(?![\w-])", self.text))

    def mentions_name(self, name: str) -> bool:
        return bool(name and re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", self.text))

    @staticmethod
    def _cmd_tokens(cmd: str) -> List[str]:
        """Tokens of a hook command, POSIX and Windows spellings both (a backslash path survives shlex in neither)."""
        toks: List[str] = []
        try:
            toks.extend(shlex.split(cmd))
        except ValueError:
            toks.extend(cmd.split())
        if "\\" in cmd:  # Windows: C:\Users\x\hooks\h.py, .\hooks\h.py, "%CLAUDE_PROJECT_DIR%\hooks\h.py"
            for t in re.findall(r'"[^"]*"|\'[^\']*\'|\S+', cmd):
                t = t.strip("\"'")
                if "\\" in t:
                    toks.append(t.replace("\\", "/"))
        return toks

    def hook_files(self) -> Set[str]:
        """Real paths of the scripts the hook commands run (best effort; unresolved names kept as suffixes)."""
        out: Set[str] = set()
        home = os.path.expanduser("~")
        for cmd in self.hook_cmds:
            for t in self._cmd_tokens(cmd):
                if not t or t.startswith("-") or "=" in t.split("/")[0]:
                    continue
                s = (
                    t.replace("${CLAUDE_PROJECT_DIR}", self.root)
                    .replace("$CLAUDE_PROJECT_DIR", self.root)
                    .replace("%CLAUDE_PROJECT_DIR%", self.root)
                    .replace("${CLAUDE_PLUGIN_ROOT}", self.root)
                    .replace("$CLAUDE_PLUGIN_ROOT", self.root)
                    .replace("%CLAUDE_PLUGIN_ROOT%", self.root)
                    .replace("${HOME}", home)
                    .replace("$HOME", home)
                    .replace("%USERPROFILE%", home)
                )
                if s.startswith("~"):
                    s = home + s[1:]
                cand = s if os.path.isabs(s) else os.path.join(self.root, s)
                if os.path.isfile(cand):
                    out.add(os.path.realpath(cand))
                elif "/" in s:
                    tail = re.sub(r"^[A-Za-z]:", "", s).lstrip("./").lstrip("/")
                    if os.path.isabs(s) or re.match(r"^[A-Za-z]:", s):
                        # an absolute path from another machine or a Windows drive: only its last two parts can match
                        tail = "/".join(tail.split("/")[-2:])
                    out.add("*/" + tail)
        return out

    def is_hook(self, abs_path: str, hook_files: Set[str]) -> bool:
        rp = os.path.realpath(abs_path)
        return rp in hook_files or any(h.startswith("*/") and rp.endswith(h[1:]) for h in hook_files)


# ------------------------------------------------------------------ python analysis
class Sym:
    __slots__ = ("kind", "name", "start", "end", "node", "decorated", "public")

    def __init__(
        self, kind: str, name: str, start: int, end: int, node=None, decorated: bool = False, public: bool = False
    ):
        self.kind, self.name, self.start, self.end, self.node, self.decorated = kind, name, start, end, node, decorated
        self.public = public


SAFE_DECORATORS = {
    "lru_cache",
    "cache",
    "wraps",
    "dataclass",
    "contextmanager",
    "total_ordering",
    "staticmethod",
    "classmethod",
    "cached_property",
    "unique",
    "final",
}


def _deco_name(d) -> str:
    if isinstance(d, ast.Call):
        d = d.func
    if isinstance(d, ast.Attribute):
        return d.attr
    return d.id if isinstance(d, ast.Name) else ""


def py_parse(text: str):
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):  # MemoryError: "Parser stack overflowed"
        return None


def py_symbols(tree) -> List[Sym]:
    syms = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])
            unsafe = any(_deco_name(d) not in SAFE_DECORATORS for d in n.decorator_list)
            syms.append(
                Sym(
                    "class" if isinstance(n, ast.ClassDef) else "function",
                    n.name,
                    start,
                    getattr(n, "end_lineno", n.lineno),
                    n,
                    unsafe,
                )
            )
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                if isinstance(t, ast.Name) and CONST_RE.match(t.id):
                    syms.append(Sym("constant", t.id, n.lineno, getattr(n, "end_lineno", n.lineno), n))
    return syms


def _is_const(n) -> bool:
    if isinstance(n, ast.Constant):
        return True
    if isinstance(n, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_const(e) for e in n.elts)
    if isinstance(n, ast.Dict):
        return all(k is not None and _is_const(k) for k in n.keys) and all(_is_const(v) for v in n.values)
    if isinstance(n, ast.JoinedStr):
        return not any(isinstance(v, ast.FormattedValue) for v in n.values)
    if isinstance(n, ast.UnaryOp):
        return _is_const(n.operand)
    if isinstance(n, ast.Call) and _dotted(n.func) == "json.dumps":
        return all(_is_const(a) for a in n.args) and all(_is_const(k.value) for k in n.keywords)
    return False


def _dotted(n) -> str:
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        base = _dotted(n.value)
        return f"{base}.{n.attr}" if base else ""
    return ""


def _empty_value(n) -> bool:
    if n is None:
        return True
    if isinstance(n, ast.Constant):
        return n.value is None or n.value in ("", b"")
    if isinstance(n, (ast.Tuple, ast.List, ast.Set)):
        return not n.elts
    if isinstance(n, ast.Dict):
        return not n.keys
    return False


def _is_trivial_stmt(s) -> Optional[str]:
    if isinstance(s, ast.Pass):
        return "pass"
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant):
        return "..." if s.value.value is Ellipsis else "string"
    if isinstance(s, ast.Return) and _empty_value(s.value):
        return "return empty"
    if isinstance(s, ast.Raise) and s.exc is not None:
        e = s.exc.func if isinstance(s.exc, ast.Call) else s.exc
        if _dotted(e) in ("NotImplementedError", "NotImplemented"):
            return "raise NotImplementedError"
    return None


def py_stub_reason(fn) -> Optional[str]:
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
        if not body:
            return "docstring only"
    kinds = [_is_trivial_stmt(s) for s in body]
    if all(kinds):
        return " / ".join(dict.fromkeys(k for k in kinds if k))
    return None


def _fn_lines(lines: Sequence[str], a: int, b: int) -> str:
    return "\n".join(lines[a - 1 : b])


def py_todo_small(fn, src: str) -> bool:
    """A TODO marker in the function and a body of at most 3 statements that are trivial or constant-only."""
    if not TODO_RE.search(src):
        return False
    body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    if len(body) > 3:
        return False
    return all(
        _is_trivial_stmt(s)
        or (isinstance(s, ast.Return) and _is_const(s.value))
        or isinstance(s, ast.Assign)
        and _is_const(s.value)
        for s in body
    )


def _abstractish(cls: ast.ClassDef) -> bool:
    names = {_dotted(b).split(".")[-1] for b in cls.bases} | {_dotted(k.value).split(".")[-1] for k in cls.keywords}
    return bool(names & {"ABC", "Protocol", "ABCMeta"})


# --- python hooks: does a registered hook do anything with its input?
_READ_HELPER_RE = re.compile(r"(stdin|hook_input|read_input|load_input|parse_input|get_input)", re.I)
_OK_CALLS = {
    "print",
    "json.dumps",
    "sys.exit",
    "exit",
    "quit",
    "os._exit",
    "str",
    "Path",
    "pathlib.Path",
    "sys.path.insert",
    "sys.path.append",
    "os.path.dirname",
    "os.path.abspath",
    "os.path.realpath",
    "os.path.join",
    "sys.stdout.flush",
    "sys.stderr.flush",
    "sys.stdout.write",
    "sys.stderr.write",
    "SystemExit",
    "super",
    "len",
    "repr",
    "dict",
    "list",
    "set",
    "tuple",
    "int",
    "bool",
    "isinstance",
}


def py_hook_stub_reason(tree) -> Optional[str]:
    """A registered hook is a stub when its input never reaches its output and it does no other work."""
    local = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    parents: Dict[int, ast.AST] = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parents[id(c)] = n
    loads = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    loads_attr_stdin = False
    work: List[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr == "stdin" and _dotted(n) == "sys.stdin":
            loads_attr_stdin = True
        if not isinstance(n, ast.Call):
            continue
        name = _dotted(n.func)
        if name in local or name.split(".")[-1] in local and name.startswith("self."):
            continue
        is_read = (
            bool(_READ_HELPER_RE.search(name))
            or name in ("input", "os.read")
            or (name == "open" and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == 0)
            or any(_dotted(a) == "sys.stdin" for a in n.args)  # json.load(sys.stdin), yaml.safe_load(sys.stdin)
        )
        if is_read:
            p = parents.get(id(n))
            if isinstance(p, ast.Expr):
                continue  # value thrown away
            if isinstance(p, ast.Assign) and all(isinstance(t, ast.Name) for t in p.targets):
                if any(t.id in loads for t in p.targets):  # type: ignore[union-attr]
                    return None  # the value is used somewhere
                continue
            return None  # the value flows into an expression
        if name in ("print", "sys.stdout.write"):
            to_err = any(k.arg == "file" and _dotted(k.value) == "sys.stderr" for k in n.keywords)
            if not to_err and not all(_is_const(a) for a in n.args):
                return None  # output depends on something
            continue
        if name == "json.dump":
            to_err = len(n.args) > 1 and _dotted(n.args[1]) == "sys.stderr"
            if not to_err and not all(_is_const(a) or _empty_value(a) for a in n.args[:1]):
                return None  # output depends on something
            continue
        if name == "json.dumps" or name in _OK_CALLS:
            continue
        work.append(name or "<call>")
    if loads_attr_stdin:
        # sys.stdin used as an object: fine only when thrown away; treat any other use as input handling
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and _dotted(n) == "sys.stdin":
                p = parents.get(id(n))
                gp = parents.get(id(p)) if p is not None else None
                if not (
                    isinstance(gp, ast.Expr)
                    or (
                        isinstance(p, ast.Call)
                        and isinstance(gp, ast.Assign)
                        and all(isinstance(t, ast.Name) and t.id not in loads for t in gp.targets)
                    )
                    or (
                        isinstance(p, ast.Attribute)
                        and isinstance(gp, ast.Call)
                        and isinstance(parents.get(id(gp)), ast.Expr)
                    )
                ):
                    return None
    if work:
        return None
    return "input is never used and the output is constant"


_SH_STUB_LINE = re.compile(r"^(set\b.*|exit(\s+\d+)?|true|:|return(\s+\d+)?|echo\s[^$`]*|printf\s[^$`]*|cd\b.*)$")
_SH_READS = re.compile(
    r"\b(cat|read|jq|grep|sed|awk|python3?|node|head|tail|tee|xargs|cut|tr)\b|/dev/stdin|\$\(<|\$\{?\d|\$@|\$\*"
)


_SH_DRAIN = re.compile(r"^cat(\s+-)?\s+\d?>\s*/dev/null(\s+2>&1)?(\s*\|\|\s*(true|:))?$")


def sh_hook_stub_reason(text: str) -> Optional[str]:
    stmts = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if len(s) > _STUB_MAX_CHARS:  # a stub line is short; the patterns below never run on a hostile line
            return None
        s = re.sub(r"\s+#.*$", "", s)
        stmts.append(s)
    if not stmts:
        return "empty script"
    for s in stmts:
        if _SH_DRAIN.match(s):
            continue  # stdin is drained into /dev/null: the input is thrown away
        if _SH_READS.search(s) or not _SH_STUB_LINE.match(s):
            return None
    return "never reads its input and prints a constant"


# --- python: CLI flags
def _str_args(call: ast.Call) -> List[str]:
    return [a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def _dest_of(flags: List[str], kws: Dict[str, ast.expr]) -> Optional[str]:
    if "dest" in kws and isinstance(kws["dest"], ast.Constant) and isinstance(kws["dest"].value, str):
        return kws["dest"].value
    longs = [f for f in flags if f.startswith("--")]
    use = longs[0] if longs else (flags[0] if flags else None)
    return use.lstrip("-").replace("-", "_") if use else None


def py_cli_flags(tree) -> List[Tuple[str, str, int, int, str]]:
    """(flag, dest, first line, last line, kind) for add_argument / click.option calls."""
    out = []
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("add_argument", "add_option")
        ):
            flags = [f for f in _str_args(n) if f.startswith("-")]
            kws = {k.arg: k.value for k in n.keywords if k.arg}
            act = kws.get("action")
            if not flags or (isinstance(act, ast.Constant) and act.value in ("help", "version")):
                continue
            dest = _dest_of(flags, kws)
            if dest:
                longs = [f for f in flags if f.startswith("--")]
                out.append(((longs or flags)[0], dest, n.lineno, getattr(n, "end_lineno", n.lineno), "argparse"))
    return out


def py_click_unused(tree) -> List[Tuple[str, str, int]]:
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        for d in fn.decorator_list:
            if isinstance(d, ast.Call) and _deco_name(d) == "option":
                flags = [f for f in _str_args(d) if f.startswith("-")]
                kws = {k.arg: k.value for k in d.keywords if k.arg}
                dest = _dest_of(flags, kws)
                if dest and dest in params and dest not in used and not fn.args.kwarg:
                    longs = [f for f in flags if f.startswith("--")]
                    out.append(((longs or flags)[0], dest, d.lineno))
    return out


def py_local_uses(tree) -> Dict[str, List[int]]:
    """name -> lines where it is read as attribute, string constant or loaded name (built once per file)."""
    uses: Dict[str, List[int]] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute):
            uses.setdefault(n.attr, []).append(n.lineno)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            uses.setdefault(n.value, []).append(n.lineno)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            uses.setdefault(n.id, []).append(n.lineno)
    return uses


def py_has_namespace_dump(tree) -> bool:
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.Call)
            and _dotted(n.func) in ("vars", "dict")
            and n.args
            and isinstance(n.args[0], ast.Name)
            and n.args[0].id in ("args", "ns", "options", "opts", "namespace")
        ):
            return True
        if (
            isinstance(n, ast.Attribute)
            and n.attr == "__dict__"
            and isinstance(n.value, ast.Name)
            and n.value.id in ("args", "ns", "options", "opts", "namespace")
        ):
            return True
    return False


# ------------------------------------------------------------------ generic (sh / js / go / rs) analysis
# Column-0 declarations are short; a longer line (hostile foreign data) is not matched with a regex at all.
MAX_SYM_LINE = 2000

_SYM_RES = {
    "js": [
        (
            "function",
            re.compile(r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?function(?:\s+|\s*\*\s*)([A-Za-z_$][\w$]*)\s*\("),
        ),
        ("class", re.compile(r"^(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
        ("constant", re.compile(r"^(?:export\s+)?const\s+([A-Z][A-Z0-9_]{2,})\s*[=:]")),
        (
            "function",
            re.compile(
                r"^(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?" r"(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
            ),
        ),
    ],
    "go": [
        ("function", re.compile(r"^func\s+([A-Za-z_]\w*)\s*[\(\[]")),
        ("class", re.compile(r"^type\s+([A-Za-z_]\w*)\s+")),
        ("constant", re.compile(r"^(?:const|var)\s+([A-Za-z_]\w*)\s")),
    ],
    "rs": [
        ("function", re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)")),
        ("class", re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|type)\s+([A-Za-z_]\w*)")),
        ("constant", re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:const|static)\s+([A-Z][A-Z0-9_]*)\s*:")),
    ],
    "sh": [
        ("function", re.compile(r"^(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?")),
        ("function", re.compile(r"^function\s+([A-Za-z_][\w-]*)\s*\{?")),
        ("constant", re.compile(r"^(?:readonly\s+|declare\s+-r\s+)?([A-Z][A-Z0-9_]{2,})=")),
    ],
}
_GO_SKIP = re.compile(r"^(main|init|Test\w*|Benchmark\w*|Example\w*|Fuzz\w*)$")
_STUB_MAX_CHARS = 400
_GENERIC_STUB = {
    "js": re.compile(
        r"^(return;?|return (null|undefined|\{\}|\[\]|\"\"|'')\s*;?|throw new Error\(\s*([\"'`])[^\"'`]*"
        r"(not implemented|todo|unimplemented)[^\"'`]*\3\s*\)\s*;?)?$",
        re.I,
    ),
    "go": re.compile(
        r"^(return|return (nil|\"\"|0|false)(, ?(nil|\"\"|0|false))*|panic\([\"`][^\"`]*(todo|not implemented|"
        r"unimplemented)[^\"`]*[\"`]\))?$",
        re.I,
    ),
    "rs": re.compile(
        r"^((todo|unimplemented)!\(.*\);?|panic!\(\"[^\"]*(todo|not implemented)[^\"]*\"\);?|\(\))?$", re.I
    ),
    "sh": re.compile(r"^(:|true|return( 0)?|echo [\"']?(todo|not implemented)[^\n]*)?$", re.I),
}


def _scan_block(text: str, i: int, lang: str) -> int:
    """text[i] == '{'. Index after the matching '}', or -1. Skips strings and comments roughly."""
    depth, n = 0, len(text)
    while i < n:
        c = text[i]
        two = text[i : i + 2]
        if lang == "sh" and c == "#" and (i == 0 or text[i - 1] in " \t\n;"):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if lang != "sh" and two == "//":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if lang != "sh" and two == "/*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in '"`' or (c == "'" and not (lang == "rs" and not re.match(r"'(\\.|[^\\'])'", text[i : i + 4]))):
            q = c
            i += 1
            while i < n and text[i] != q:
                i += 2 if text[i] == "\\" and q != "'" or (text[i] == "\\" and lang != "sh") else 1
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def generic_symbols(lang: str, text: str, pre=None) -> List[Tuple[Sym, str]]:
    """[(Sym, body text)] for column-0 declarations; body is '' for constants and unparsed blocks. The extra
    languages (the langs module) come from `pre` (already extracted) or are extracted here."""
    if DLANGS is not None and lang in DLANGS.EXTRA_LANGS:
        xs = pre if pre is not None else DLANGS.symbols(lang, text)
        return [(Sym(x.kind, x.name, x.start, x.end, None, x.decorated, x.public), x.body) for x in xs]
    out = []
    lines = text.split("\n")
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    for idx, ln in enumerate(lines):
        if len(ln) > MAX_SYM_LINE:
            continue
        for kind, rx in _SYM_RES.get(lang, []):
            m = rx.match(ln)
            if not m:
                continue
            name = m.group(1)
            if lang == "go" and _GO_SKIP.match(name):
                break
            if lang == "rs" and (name == "main" or (idx > 0 and re.search(r"#\[(test|cfg\(test\))\]", lines[idx - 1]))):
                break
            body, end = None, idx + 1
            if kind in ("function", "class"):
                brace = text.find("{", offsets[idx])
                nxt = offsets[idx + 1] if idx + 1 < len(lines) else len(text)
                seg = text[offsets[idx] : brace] if brace >= 0 else ""
                arrow = "=>" in ln
                ok = (
                    brace >= 0
                    and ";" not in seg
                    and (
                        (arrow and re.search(r"=>\s*$", seg))
                        or (not arrow and (lang == "sh" and brace < nxt or lang != "sh" and brace < nxt + 400))
                    )
                )
                if ok and lang in ("go", "rs", "js") and kind == "class":
                    ok = brace < nxt  # a type body starts on the header line
                if ok:
                    close = _scan_block(text, brace, lang)
                    if close > 0:
                        body = text[brace + 1 : close - 1] if kind == "function" else None
                        end = text.count("\n", 0, close) + 1
            out.append((Sym(kind, name, idx + 1, end), body))
            break
    return out


def _strip_block_comments(s: str) -> str:
    """Closed /* ... */ removed left to right; an unclosed /* stays. Linear (the core must work without
    deadcode_langs, so this mirrors deadcode_langs.strip_block_comments instead of importing it)."""
    if "/*" not in s:
        return s
    parts = []
    pos = 0
    while True:
        a = s.find("/*", pos)
        if a < 0:
            break
        b = s.find("*/", a + 2)
        if b < 0:
            break
        parts.append(s[pos:a])
        pos = b + 2
    parts.append(s[pos:])
    return "".join(parts)


def generic_body_stub(lang: str, body: str) -> Optional[str]:
    if body is None:
        return None
    if DLANGS is not None and lang in DLANGS.EXTRA_LANGS:
        return DLANGS.body_stub(lang, body)
    s = body
    if lang == "sh":  # line by line: a regex with ^\s* spans the blank lines of a hostile body and is quadratic
        s = "\n".join("" if ln.lstrip().startswith("#") else ln for ln in s.split("\n"))
    else:
        s = _strip_block_comments(s)
        s = re.sub(r"(?m)//.*$", "", s)
    s = " ".join(s.split()).strip().rstrip(";").strip()
    if len(s) > _STUB_MAX_CHARS:  # a stub is short; the verdict patterns never run on a long (hostile) body
        return None
    rx = _GENERIC_STUB.get(lang)
    if rx and rx.match(s.strip()):
        return "empty body" if not s.strip() else s.strip()[:40]
    return None


# ------------------------------------------------------------------ the analysis
class Task:
    __slots__ = ("kind", "path", "line", "name", "start", "end", "extra")

    def __init__(self, kind, path, line, name, start=0, end=0, extra=None):
        self.kind, self.path, self.line, self.name, self.start, self.end, self.extra = (
            kind,
            path,
            line,
            name,
            start,
            end,
            extra or {},
        )


_KEY_LINE_MAX = 400  # a key sits at the start of a line; a longer tail (minified or hostile) is not matched
_KEY_RES = {
    ".json": re.compile(r'^\s*"([^"]{3,})"\s*:'),
    ".toml": re.compile(r"^\s*(?:\[+\s*([A-Za-z0-9_.-]{3,}?)\s*\]+|([A-Za-z0-9_.-]{3,})\s*=)"),
    ".yaml": re.compile(r"^\s*(?:-\s*)?([A-Za-z_][\w.-]{2,})\s*:(?:\s|$)"),
    ".ini": re.compile(r"^\s*([A-Za-z_][\w.-]{2,})\s*="),
}
_ENV_RES = [
    re.compile(r"^\s*export\s+([A-Z][A-Z0-9_]{2,})="),
    re.compile(r"^\s*ENV\s+([A-Z][A-Z0-9_]{2,})[ =]"),
    re.compile(r"^\s*Environment=\"?([A-Z][A-Z0-9_]{2,})="),
    re.compile(r"os\.environ\[['\"]([A-Z][A-Z0-9_]{2,})['\"]\]\s*=[^=]"),
    re.compile(r"os\.(?:environ\.setdefault|putenv)\(\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]"),
    re.compile(r"process\.env\.([A-Z][A-Z0-9_]{2,})\s*=[^=]"),
]
_ENV_EXAMPLE_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})=")


def _config_kind(path: str, config_files: Sequence[str] = ()) -> Optional[str]:
    base = os.path.basename(path)
    if ENV_EXAMPLE_RE.search(path):
        return "env"
    ext = os.path.splitext(base)[1].lower()
    if ext not in CONFIG_EXT or _glob_any(path, CONFIG_SKIP_NAMES):
        return None
    if config_files and not any(fnmatch.fnmatch(base.lower(), g) for g in config_files):
        return None
    if any(p in CONFIG_SKIP_DIRS for p in _parts(path)[:-1]) or is_test(path):
        return None
    return {
        ".json": ".json",
        ".toml": ".toml",
        ".yaml": ".yaml",
        ".yml": ".yaml",
        ".ini": ".ini",
        ".cfg": ".ini",
        ".conf": ".ini",
        ".properties": ".ini",
    }[ext]


def _keys_of(kind: str, text: str, only: Optional[Set[int]]) -> Dict[str, int]:
    rx = _KEY_RES[kind]
    out: Dict[str, int] = {}
    for i, ln in enumerate(text.split("\n"), 1):
        if only is not None and i not in only:
            continue
        m = rx.match(ln[:_KEY_LINE_MAX])
        if m:
            k = next(g for g in m.groups() if g)
            k = k.split(".")[-1] if kind == ".toml" else k
            out.setdefault(k, i)
    return out


def _core_public(lang: str, line: str, name: str) -> bool:
    """Exported by the language's own rule: Go capitalised, Rust `pub`, JS/TS `export`."""
    if lang == "go":
        return name[:1].isupper()
    if lang == "rs":
        return line.startswith("pub")
    if lang == "js":
        return line.startswith("export")
    return False


def _extract_chunk(units):
    """Process-pool unit: [(path, lang, text)] -> [(path, json)]; pure."""
    return [(path, DLANGS.extract_unit((path, lang, text))) for path, lang, text in units]


class Analysis:
    def __init__(
        self,
        root: str,
        files: List[Changed],
        cfg: dict,
        search: Sequence[str],
        settings: Sequence[str],
        backend: str,
        langs: Optional[Set[str]] = None,
        foreign: bool = False,
        public_api: Optional[str] = None,
        jobs: int = 1,
        skipped: Optional[Dict[str, int]] = None,
    ):
        self.root, self.files, self.cfg = root, files, cfg
        # langs: the enabled language tags; None = the [deadcode] languages of the config (default: the five core)
        self.langs = set(langs) if langs is not None else set(cfg.get("languages", CORE_LANGS))
        self.langs = DLANGS.resolve(self.langs) | (self.langs & set(CORE_LANGS))
        self.use_extra = bool(self.langs & set(EXTRA_LANG_TAGS))
        self.foreign, self.jobs = foreign, jobs
        # public_api None = legacy (staged, session): every unreferenced symbol is reported as private
        self.public_api = public_api
        self.stats: dict = {"languages": {}, "skipped": _Skipped(skipped), "extracted": 0, "jobs": jobs}
        self._pre: Dict[str, list] = {}
        self._generated: Dict[str, bool] = {}  # path -> verdict of the generated-header check
        self.search = list(search) or [root]
        self.settings = settings
        self.backend = backend
        self.findings: List[Finding] = []
        self.tasks: List[Task] = []
        self.registry = Registry(root, settings, foreign=foreign)
        for k, v in self.registry.skipped.items():
            self.stats["skipped"][k] = self.stats["skipped"].get(k, 0) + v
        self.hook_files = self.registry.hook_files()

    # ---- extra languages: read + extract each file once (process pool), results ordered by path
    def _extract_extra(self) -> None:
        todo = []
        for f in self.files:
            if f.skipped or self.skip_reason(f) is not None:
                continue
            lang = lang_of(f.path, f.text, extra=True)
            if lang in EXTRA_LANG_TAGS and lang in self.langs:
                todo.append((f.path, lang, f.text))
        if not todo:
            return
        self.stats["extracted"] = len(todo)
        n = max(1, min(self.jobs, len(todo)))
        if n > 1 and len(todo) >= 32:  # a pool costs more than a few small files
            size = max(1, -(-len(todo) // (n * 4)))
            chunks = [todo[i : i + size] for i in range(0, len(todo), size)]
            try:
                results: List[Tuple[str, str]] = []
                for part in _run_units(chunks, _extract_chunk, n):
                    results += part
            except Exception:  # noqa: BLE001 - a broken pool falls back to the same function, in process
                results = _extract_chunk(todo)
        else:
            results = _extract_chunk(todo)
        for path, blob in results:
            self._pre[path] = DLANGS.from_json(blob)

    # ---- helpers
    def abs(self, rel: str) -> str:
        return rel if os.path.isabs(rel) else os.path.join(self.root, rel)

    def skip_file(self, f: Changed) -> bool:
        return self.skip_reason(f) is not None

    def skip_reason(self, f: Changed) -> Optional[str]:
        if is_backup(f.path) or SECRET_RE.search(f.path) is not None:
            return "backup-or-secret"
        if _glob_any(f.path, self.cfg["ignore_paths"]):
            return "ignored-path"
        if has_ignore_file(f.text):
            return "ignore-file"
        hit = self._generated.get(f.path)
        if hit is None:
            hit = self._generated[f.path] = DLANGS.is_generated(f.text, self.foreign)
        if hit:
            return "generated"
        return None

    def skip_dirs(self) -> Set[str]:
        extra = set(DLANGS.FOREIGN_SKIP_DIRS) if (self.foreign and DLANGS is not None) else set()
        return set(SKIP_DIRS) | extra

    def name_ignored(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, g) for g in self.cfg["ignore"])

    def registered_symbol(self, f: Changed, name: str) -> bool:
        return self.registry.mentions_name(name) or (
            name == "main" and self.registry.is_hook(self.abs(f.path), self.hook_files)
        )

    # ---- per-file extraction
    def run(self) -> List[Finding]:
        if self.use_extra:
            self._extract_extra()
        for f in self.files:
            if f.skipped:
                if _glob_any(f.path, self.cfg["ignore_paths"]):
                    continue
                self.findings.append(
                    Finding(
                        "W",
                        f.path,
                        0,
                        "deadcode.unread",
                        f"not checked: {f.skipped} (split it or add it to [deadcode] ignore_paths)",
                    )
                )
                continue
            why = self.skip_reason(f)
            if why is not None:
                if why == "generated":
                    self.stats["skipped"]["generated"] = self.stats["skipped"].get("generated", 0) + 1
                continue
            lang = lang_of(f.path, f.text, extra=self.use_extra)
            if lang is not None and lang not in self.langs:
                lang = None
            if lang:
                self.stats["languages"][lang] = self.stats["languages"].get(lang, 0) + 1
            if lang == "py":
                self.python(f)
            elif lang:
                self.generic(f, lang)
            self.file_task(f, lang)
            self.env_tasks(f)
            self.config_tasks(f, lang)
        self.resolve()
        LAST_STATS.clear()
        LAST_STATS.update(self.stats)
        return _sorted(self.findings)

    def python(self, f: Changed) -> None:
        tree = py_parse(f.text)
        if tree is None:
            self.findings.append(
                Finding(
                    "W", f.path, 0, "deadcode.unparsed", "not checked: the file does not parse as Python (syntax error)"
                )
            )
            return
        lines = f.text.split("\n")
        base_names: Set[str] = set()
        if f.base is not None:
            bt = py_parse(f.base)
            base_names = {s.name for s in py_symbols(bt)} if bt is not None else set()
        test_file = is_test(f.path)
        registered_file = self.registry.is_hook(self.abs(f.path), self.hook_files)
        hook_stub = None
        if registered_file:
            hook_stub = py_hook_stub_reason(tree)
            if hook_stub and (f.new or f.touches(1, len(lines))):
                self.findings.append(
                    Finding(
                        "E",
                        f.path,
                        1,
                        "deadcode.stub-hook",
                        f"registered hook does nothing: {hook_stub}; implement it or unregister and delete it",
                    )
                )
        for s in py_symbols(tree):
            if line_ignored(lines, s.start) or self.name_ignored(s.name):
                continue
            is_new = f.new or s.name not in base_names
            dunder = s.name.startswith("__") and s.name.endswith("__")
            if s.kind == "function" and not test_file and not hook_stub and f.touches(s.start, s.end):
                reason = py_stub_reason(s.node)
                if reason and not (_deco_name_set(s.node) & {"abstractmethod", "overload"}):
                    reg = self.registered_symbol(f, s.name)
                    self.findings.append(
                        Finding(
                            "E" if reg else "W",
                            f.path,
                            s.start,
                            "deadcode.stub-function",
                            f"{s.name}: body is only {reason}" + (" (registered)" if reg else ""),
                        )
                    )
            if s.kind == "class" and not test_file:
                self.class_todos(f, s, lines)
            if is_new and not dunder and not s.decorated:
                if test_file and (
                    s.name.startswith("test") or s.name.startswith("Test") or s.name.startswith("pytest_")
                ):
                    continue
                self.tasks.append(
                    Task(
                        "sym",
                        f.path,
                        s.start,
                        s.name,
                        s.start,
                        s.end,
                        {"kind": s.kind, "test_def": test_file, "lang": "py"},
                    )
                )
        self.cli_flags(f, tree)

    def class_todos(self, f: Changed, s: Sym, lines: Sequence[str]) -> None:
        if _abstractish(s.node):
            return
        for m in s.node.body:
            if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (m.name.startswith("__") and m.name.endswith("__")) or (
                _deco_name_set(m) & {"abstractmethod", "overload"}
            ):
                continue
            if not f.touches(m.lineno, getattr(m, "end_lineno", m.lineno)):
                continue
            if line_ignored(lines, m.lineno):
                continue
            src = _fn_lines(lines, m.lineno, getattr(m, "end_lineno", m.lineno))
            if py_todo_small(m, src):
                self.findings.append(
                    Finding(
                        "W",
                        f.path,
                        m.lineno,
                        "deadcode.todo-stub",
                        f"{s.name}.{m.name}: TODO marker over a trivial body",
                    )
                )

    def cli_flags(self, f: Changed, tree) -> None:
        if is_test(f.path) or py_has_namespace_dump(tree):
            return
        lines = f.text.split("\n")
        flags = py_cli_flags(tree)
        uses = py_local_uses(tree) if flags else {}
        for flag, dest, a, b, _k in flags:
            if not f.touches(a, b) or line_ignored(lines, a):
                continue
            if any(not (a <= ln <= b) for ln in uses.get(dest, ())):
                continue
            self.tasks.append(Task("cli", f.path, a, dest, a, b, {"flag": flag}))
        for flag, dest, a in py_click_unused(tree):
            if f.touches(a, a) and not line_ignored(lines, a):
                self.findings.append(
                    Finding(
                        "W", f.path, a, "deadcode.cli-flag-unused", f"{flag}: click parameter {dest!r} is never read"
                    )
                )

    def generic(self, f: Changed, lang: str) -> None:
        lines = f.text.split("\n")
        base_names = {s.name for s, _b in generic_symbols(lang, f.base)} if f.base is not None else set()
        xs = DLANGS is not None and lang in DLANGS.EXTRA_LANGS
        test_file = is_test(f.path)
        registered_file = self.registry.is_hook(self.abs(f.path), self.hook_files)
        if lang == "sh" and registered_file:
            r = sh_hook_stub_reason(f.text)
            if r:
                self.findings.append(
                    Finding(
                        "E",
                        f.path,
                        1,
                        "deadcode.stub-hook",
                        f"registered hook does nothing: {r}; implement it or unregister and delete it",
                    )
                )
                return
        for s, body in generic_symbols(lang, f.text, self._pre.get(f.path) if xs else None):
            if line_ignored(lines, s.start) or self.name_ignored(s.name):
                continue
            is_new = f.new or s.name not in base_names
            if s.kind == "function" and not test_file and f.touches(s.start, s.end):
                r = generic_body_stub(lang, body)
                if r is not None:
                    reg = self.registered_symbol(f, s.name)
                    self.findings.append(
                        Finding(
                            "E" if reg else "W",
                            f.path,
                            s.start,
                            "deadcode.stub-function",
                            f"{s.name}: body is only {r}" + (" (registered)" if reg else ""),
                        )
                    )
            if s.decorated:
                continue  # framework-managed (Spring, ASP.NET, Android ...): assumed used
            if is_new and not (test_file and re.match(r"(test|Test)", s.name)):
                public = False
                if self.public_api in ("report", "exempt"):
                    public = s.public if xs else _core_public(lang, lines[s.start - 1], s.name)
                self.tasks.append(
                    Task(
                        "sym",
                        f.path,
                        s.start,
                        s.name,
                        s.start,
                        s.end,
                        {"kind": s.kind, "test_def": test_file, "lang": "generic", "public": public},
                    )
                )

    def env_tasks(self, f: Changed) -> None:
        if is_doc(f.path) or is_test(f.path):
            return
        seen = set()
        for i, ln in enumerate(f.text.split("\n"), 1):
            if f.added is not None and i not in f.added:
                continue
            rxs = _ENV_RES + ([_ENV_EXAMPLE_RE] if ENV_EXAMPLE_RE.search(f.path) else [])
            for rx in rxs:
                m = rx.search(ln)
                if m and m.group(1) not in STD_ENV and m.group(1) not in seen:
                    seen.add(m.group(1))
                    self.tasks.append(Task("env", f.path, i, m.group(1), i, i))

    def file_task(self, f: Changed, lang: Optional[str]) -> None:
        if not f.new or lang is None:
            return
        rel = f.path
        base = os.path.basename(rel)
        parts = _parts(rel)
        if is_test(rel) or is_doc(rel) or base in ENTRY_NAMES or re.search(r"\.config\.\w+$", base):
            return
        if any(p in self.cfg["exempt_dirs"] for p in parts[:-1]):
            return
        strict = any(p in self.cfg["strict_dirs"] for p in parts[:-1])
        together = ("go", "rs") + (tuple(DLANGS.SKIP_FILE_LANGS) if DLANGS is not None else ())
        if lang in together and not strict:
            return  # package members are compiled together (or autoloaded by class name), not imported
        if self.registry.is_hook(self.abs(rel), self.hook_files):
            return  # registered in a hook table: referenced by definition
        stem = os.path.splitext(base)[0]
        by_stem = ("py", "js") + (tuple(DLANGS.STEM_LANGS) if DLANGS is not None else ())
        names = {base} | ({stem} if len(stem) >= 3 and lang in by_stem else set())
        self.tasks.append(Task("file", rel, 1, base, extra={"names": sorted(names), "strict": strict}))

    def config_tasks(self, f: Changed, lang: Optional[str]) -> None:
        kind = _config_kind(f.path, self.cfg["config_files"])
        if kind is None or kind == "env":
            return
        old = _keys_of(kind, f.base, None) if f.base is not None else {}
        for k, line in _keys_of(kind, f.text, f.added).items():
            if k not in old:
                self.tasks.append(Task("cfgkey", f.path, line, k, line, line))

    # ---- resolution (one reference query for everything)
    def resolve(self) -> None:
        if not self.tasks:
            return
        names: Set[str] = set()
        for t in self.tasks:
            if t.kind == "file":
                names.update(t.extra["names"])
            elif t.kind == "cfgkey":
                names.update({t.name, t.name.replace("-", "_")})
            else:
                names.add(t.name)
        full = {t.name for t in self.tasks if t.kind == "cli"}
        focus = [self.abs(f.path) for f in self.files]
        hits = RefIndex(
            self.search,
            self.backend,
            foreign=self.foreign,
            jobs=self.jobs,
            skip_dirs=self.skip_dirs(),
            skipped=self.stats["skipped"],
        ).find(names, focus=focus, full=full)
        for t in self.tasks:
            getattr(self, "_res_" + t.kind)(t, hits)

    def _live(self, h: Hit) -> bool:
        return _countable(h, self.root)

    def _same_def(self, t: Task, h: Hit) -> bool:
        return os.path.realpath(h.path) == os.path.realpath(self.abs(t.path)) and t.start <= h.line <= t.end

    def _res_sym(self, t: Task, hits: Dict[str, List[Hit]]) -> None:
        prod, tests = False, False
        selector = t.extra["kind"] == "selector"
        for h in hits.get(t.name, []):
            if not self._live(h) or self._same_def(t, h):
                continue
            rel = os.path.relpath(h.path, self.root) if h.path.startswith(self.root + os.sep) else h.path
            if is_doc(rel) and not self.cfg["docs_count"]:
                continue
            if selector and DLANGS is not None and os.path.splitext(rel)[1].lower() in DLANGS.STYLE_EXT:
                continue  # a selector that only another selector mentions is still dead
            if is_test(rel) and not t.extra["test_def"]:
                tests = True
                continue
            prod = True
            break
        if prod or (tests and self.cfg["tests_count"]):
            return
        if tests:
            self.findings.append(
                Finding(
                    "W",
                    t.path,
                    t.line,
                    "deadcode.test-only-symbol",
                    f"{t.extra['kind']} {t.name}: referenced only from tests (production code never uses it)",
                )
            )
        elif t.extra.get("public") and self.public_api in ("report", "exempt"):
            if self.public_api == "report":
                self.findings.append(
                    Finding(
                        "W",
                        t.path,
                        t.line,
                        "deadcode.unreferenced-public-symbol",
                        f"{t.extra['kind']} {t.name}: exported, nothing in this tree references it "
                        "(may have outside consumers)",
                    )
                )
        else:
            level = self._fn_level(t)
            self.findings.append(
                Finding(
                    level,
                    t.path,
                    t.line,
                    "deadcode.unreferenced-symbol",
                    f"new {t.extra['kind']} {t.name}: nothing references it"
                    + (" (dead function; wire it in, delete it, or mark `# deadcode: ignore`)" if level == "E" else ""),
                )
            )

    def _fn_level(self, t: Task) -> str:
        """E for a dead Python function when [deadcode] function_level = "E" (and the path is in function_level_dirs
        when that list is set); W for everything else: classes, constants, regex-detected functions."""
        if self.cfg["function_level"] != "E" or t.extra["kind"] != "function" or t.extra.get("lang") != "py":
            return "W"
        dirs = self.cfg["function_level_dirs"]
        if dirs:
            path = t.path.replace("\\", "/").lstrip("/")
            if not any(_under_dir(path, d) for d in dirs):
                return "W"
        return "E"

    def _res_file(self, t: Task, hits: Dict[str, List[Hit]]) -> None:
        me = os.path.realpath(self.abs(t.path))
        for n in t.extra["names"]:
            for h in hits.get(n, []):
                if os.path.realpath(h.path) != me and self._live(h):
                    return
        strict = t.extra["strict"]
        self.findings.append(
            Finding(
                "E" if strict else "W",
                t.path,
                1,
                "deadcode.unreferenced-file",
                f"new file {os.path.basename(t.path)} is not imported, registered, documented, tested or scheduled anywhere"
                + (" (hooks/ and bin/ files must be wired in)" if strict else ""),
            )
        )

    def _res_cfgkey(self, t: Task, hits: Dict[str, List[Hit]]) -> None:
        me = os.path.realpath(self.abs(t.path))
        for n in {t.name, t.name.replace("-", "_")}:
            for h in hits.get(n, []):
                rel = os.path.relpath(h.path, self.root) if h.path.startswith(self.root + os.sep) else h.path
                if os.path.realpath(h.path) == me or not self._live(h) or is_doc(rel) or is_test(rel):
                    continue
                if os.path.splitext(h.path)[1].lower() in CONFIG_EXT:
                    continue
                return
        self.findings.append(
            Finding(
                "W",
                t.path,
                t.line,
                "deadcode.config-key-unread",
                f"config key {t.name!r} is added but no code reads it",
            )
        )

    def _res_env(self, t: Task, hits: Dict[str, List[Hit]]) -> None:
        me = os.path.realpath(self.abs(t.path))
        for h in hits.get(t.name, []):
            rel = os.path.relpath(h.path, self.root) if h.path.startswith(self.root + os.sep) else h.path
            if (
                (os.path.realpath(h.path) == me and h.line == t.line)
                or not self._live(h)
                or is_doc(rel)
                or is_test(rel)
            ):
                continue
            return
        self.findings.append(
            Finding(
                "W",
                t.path,
                t.line,
                "deadcode.env-unread",
                f"environment variable {t.name} is set here but no code reads it",
            )
        )

    def _res_cli(self, t: Task, hits: Dict[str, List[Hit]]) -> None:
        me = os.path.realpath(self.abs(t.path))
        rx = re.compile(r"(\.|['\"])" + re.escape(t.name) + r"\b")
        for h in hits.get(t.name, []):
            if self._same_def(t, h) or not self._live(h):
                continue
            if os.path.realpath(h.path) != me and rx.search(h.text) and not is_doc(h.path):
                return
        self.findings.append(
            Finding(
                "W",
                t.path,
                t.line,
                "deadcode.cli-flag-unused",
                f"{t.extra['flag']}: the parsed value {t.name!r} is never read",
            )
        )


def _deco_name_set(fn) -> Set[str]:
    return {_deco_name(d) for d in getattr(fn, "decorator_list", [])}


# ------------------------------------------------------------------ public API
def _run(
    root: str,
    files: List[Changed],
    search: Sequence[str],
    settings: Sequence[str],
    backend: str,
    languages: Optional[Sequence[str]] = None,
    foreign: bool = False,
    use_repo_config: bool = False,
    public_api: Optional[str] = None,
    jobs: int = 1,
    skipped: Optional[Dict[str, int]] = None,
) -> List[Finding]:
    cfg, problems = load_config(root, use_file=not foreign or use_repo_config, foreign=foreign)
    if foreign:
        if not use_repo_config:
            cfg["strict_dirs"] = []  # unreferenced-file is W in a tree that is not ours
        cfg["ignore_paths"] = list(cfg["ignore_paths"]) + list(DLANGS.FOREIGN_IGNORE_PATHS)
    if not cfg["enabled"]:
        return []
    tags = list(languages) if languages is not None else (["all"] if foreign else list(cfg["languages"]))
    langs: Optional[Set[str]] = DLANGS.resolve(tags)
    an = Analysis(
        root, files, cfg, search, settings, backend,
        langs=langs, foreign=foreign, public_api=public_api, jobs=jobs, skipped=skipped,
    )  # fmt: skip
    out = problems + an.run()
    if foreign:  # an audit of a tree that is not ours never blocks: only a scanner error stays E
        for f in out:
            if f.level == "E" and f.code not in ("deadcode.no-git", "deadcode.not-a-repo", "deadcode.bad-config"):
                f.level = "W"
    return _sorted(out)


def check_staged(
    repo: str = ".",
    search: Sequence[str] = (),
    settings: Sequence[str] = (),
    backend: str = "auto",
) -> List[Finding]:
    """Dead code in what is staged in `repo` (the index against HEAD)."""
    root = toplevel(repo)
    if root is None:
        if not safepath.which("git"):
            return [
                Finding(
                    "E", repo, 0, "deadcode.no-git", "git is not on PATH; install git (apt install git) or fix PATH"
                )
            ]
        return [Finding("E", repo, 0, "deadcode.not-a-repo", "check_staged needs a git repository")]
    files = staged_changes(root)
    if not files:
        return []
    return _run(root, files, search, settings, backend)


def check_session(
    paths: Sequence[str],
    root: Optional[str] = None,
    search: Sequence[str] = (),
    settings: Sequence[str] = (),
    backend: str = "auto",
) -> List[Finding]:
    """Dead code in the files a session touched (working tree against HEAD)."""
    paths = [p for p in paths if p]
    if not paths:
        return []
    first = os.path.abspath(paths[0])
    top = toplevel(root or first)
    base = top or os.path.realpath(root or (first if os.path.isdir(first) else os.path.dirname(first)))
    files = session_changes(base, paths, in_git=top is not None)
    if not files:
        return []
    return _run(base, files, search, settings, backend)


def resolve_paths(paths: Sequence[str], root: Optional[str] = None) -> List[str]:
    """Absolute scan paths: a relative one is relative to --root when it is given, else to the cwd."""
    anchor = os.path.abspath(root) if root else None
    out = []
    for p in paths:
        if not p:
            continue
        out.append(os.path.abspath(p if os.path.isabs(p) or anchor is None else os.path.join(anchor, p)))
    return out


def scan_base(paths: Sequence[str], root: Optional[str] = None, foreign: bool = False) -> str:
    """The directory a scan is rooted at: --root, else the repository top of the first path, else its directory."""
    if root:
        return os.path.realpath(root)
    first = resolve_paths(paths)[0]
    return toplevel(first, foreign) or os.path.realpath(first if os.path.isdir(first) else os.path.dirname(first))


def tree_roots(paths: Sequence[str], root: Optional[str] = None, foreign: bool = False) -> List[str]:
    """Every real directory (symlinks resolved, git config not trusted) a scan of `paths` reads: --root, the scan base,
    and for each path the path itself and its repository top. A --foreign report must land outside all of them."""
    out: List[str] = []
    if root:
        out.append(os.path.realpath(root))
    paths = [p for p in paths if p]
    if paths:
        out.append(scan_base(paths, root, foreign))
    for p in resolve_paths(paths, root):
        rp = os.path.realpath(p)
        out.append(rp)
        top = toplevel(rp, foreign)
        if top:
            out.append(top)
    return list(dict.fromkeys(out))


def scan(
    paths: Sequence[str],
    root: Optional[str] = None,
    search: Sequence[str] = (),
    settings: Sequence[str] = (),
    backend: str = "auto",
    foreign: bool = False,
    languages: Optional[Sequence[str]] = None,
    public_api: str = "report",
    jobs: Optional[int] = None,
    use_repo_config: bool = False,
) -> List[Finding]:
    """Audit: everything under `paths` counts as new.

    foreign=True is for a tree that is not ours: all languages by default, its own config file is not policy
    (unless use_repo_config), unreferenced-file is W, vendor/generated/symlinked files are skipped, git never runs (the tree is walked),
    nothing is written inside it. public_api: report | exempt | as-private (what an exported, unreferenced symbol is)."""
    paths = resolve_paths(paths, root)  # relative to --root when given, else to the cwd; like scan_base and tree_roots
    if not paths:
        return []
    for p in paths:
        if not os.path.exists(p):
            raise ValueError(f"path {p!r} does not exist")
    if public_api not in PUBLIC_API_MODES:
        raise ValueError(f"public_api must be one of {', '.join(PUBLIC_API_MODES)}")
    base = scan_base(paths, root, foreign)
    if foreign:
        # one scan has one base; a path whose real location leaves it would be skipped and misreported as a symlink
        for p in paths:
            rp = os.path.realpath(p)
            if rp != base and not rp.startswith(base + os.sep):
                raise ValueError(f"path {p!r} is outside the scan base {base!r}; scan each tree separately")
    skipped: Dict[str, int] = _Skipped()
    skip = None
    if foreign:
        skip = set(SKIP_DIRS) | set(DLANGS.FOREIGN_SKIP_DIRS)
    n = 1
    if foreign or jobs is not None:
        n = _workers(jobs)
    with foreign_git(foreign):
        return _run(
            base,
            all_new_changes(base, paths, skip_dirs=skip, foreign=foreign, skipped=skipped),
            search,
            settings,
            backend,
            languages=languages,
            foreign=foreign,
            use_repo_config=use_repo_config,
            public_api=public_api,
            jobs=n,
            skipped=skipped,
        )


def session_hook_output(findings: Sequence[Finding], limit: int = 8) -> Optional[dict]:
    """hookSpecificOutput for PostToolUse; None (stay silent) when there is nothing to say."""
    if not findings:
        return None
    lines = [f.render() for f in findings[:limit]]
    if len(findings) > limit:
        lines.append(f"deadcode: ... {len(findings) - limit} more (dead-code-checker session PATH...)")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n".join(lines)}}


def write_report(
    findings: Sequence[Finding], out_path: str, title: str, meta: Sequence[str] = (), stats: Optional[dict] = None
) -> None:
    """Findings carry name, path and line only, never source text, so a report cannot leak a secret from the tree."""
    by_code: Dict[str, List[Finding]] = {}
    for f in findings:
        by_code.setdefault(f.code, []).append(f)
    lines = [f"# {title}", ""]
    lines += [f"- {m}" for m in meta]
    if stats:
        langs = stats.get("languages", {})
        lines.append("- files per language: " + (", ".join(f"{k} {langs[k]}" for k in sorted(langs)) or "none"))
        sk = stats.get("skipped", {})
        lines.append("- skipped: " + (", ".join(f"{k} {sk[k]}" for k in sorted(sk) if sk[k]) or "none"))
        lines.append(
            f"- extraction: {stats.get('extracted', 0)} file(s), jobs {stats.get('jobs', 1)}"
        )
    e = sum(1 for f in findings if f.level == "E")
    lines += [f"- findings: {len(findings)} (E {e}, W {len(findings) - e})", "", "| code | E | W |", "|---|---|---|"]
    for code in sorted(by_code):
        fs = by_code[code]
        lines.append(f"| {code} | {sum(1 for f in fs if f.level == 'E')} | {sum(1 for f in fs if f.level == 'W')} |")
    for code in sorted(by_code, key=lambda c: (all(f.level != "E" for f in by_code[c]), c)):
        lines += ["", f"## {code}", ""]
        for f in by_code[code][:200]:
            lines.append(f"- {f.level} `{f.path}:{f.line}` {f.msg}" if f.line else f"- {f.level} `{f.path}` {f.msg}")
        if len(by_code[code]) > 200:
            lines.append(f"- ... {len(by_code[code]) - 200} more")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ------------------------------------------------------------------ CLI
def _emit(findings: Sequence[Finding], as_json: bool) -> int:
    if as_json:
        if findings:
            print(json.dumps([f.as_dict() for f in findings], indent=1))
    else:
        for f in findings:
            print(f.render())
    return 1 if any(f.level == "E" for f in findings) else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["graph"]:  # the reachability-graph checker has its own parser
        from .graph.cli import main as graph_main

        return graph_main(args[1:])
    if args[:1] == ["agent"]:  # wiring into a coding agent, and the hook it installs
        from .agent import main as agent_main

        return agent_main(args[1:])
    ap = argparse.ArgumentParser(prog="dead-code-checker", description="Dead code in what a commit or session added.",
                                 epilog="`dead-code-checker graph --help`: the reachability-graph checker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help="repository root (default: of the first path / cwd)")
    common.add_argument("--json", action="store_true", help="findings as JSON")
    common.add_argument("--search", action="append", default=[], help="extra directory searched for references")
    common.add_argument(
        "--settings", action="append", default=[], help="extra settings.json whose hooks count as registered"
    )
    sub.add_parser("staged", parents=[common], help="check the staged diff")
    p = sub.add_parser("session", parents=[common], help="check files a session touched")
    p.add_argument("paths", nargs="+")
    p = sub.add_parser("scan", parents=[common], help="audit: everything under PATH counts as new")
    p.add_argument("paths", nargs="+")
    p.add_argument("--report", help="write a markdown report here")
    p.add_argument("--foreign", action="store_true", help="a tree that is not ours: hardened, all languages, W only")
    p.add_argument("--use-repo-config", action="store_true", help="with --foreign: honour the target's .dead-code-checker.toml")
    p.add_argument("--lang", help="comma list of language tags or groups (java,cs,A,B,C,all); default all if --foreign")
    p.add_argument("--public-api", choices=PUBLIC_API_MODES, default="report", help="exported, unreferenced symbols")
    p.add_argument("--jobs", type=int, help="workers (default $DEAD_CODE_CHECKER_JOBS or the CPUs, at most 8)")
    sub.add_parser("defaults", help="print the [deadcode] defaults")
    p = sub.add_parser("lsp", help="language server on stdio (editor diagnostics)")
    p.add_argument("--stdio", action="store_true", help="accepted for clients that always pass it; stdio is the only transport")  # deadcode: ignore
    a = ap.parse_args(args)
    if a.cmd == "lsp":
        from .lsp import serve

        return serve()
    if a.cmd == "defaults":
        print("[deadcode]")
        for k, v in DEFAULTS.items():
            if v is not None:
                print(f"{k} = {json.dumps(v)}")
        return 0
    if a.cmd == "staged":
        fs = check_staged(a.root or ".", search=a.search, settings=a.settings)
        return _emit(fs, a.json)
    if a.cmd == "session":
        fs = check_session(a.paths, root=a.root, search=a.search, settings=a.settings)
        return _emit(fs, a.json)
    langs = [x.strip() for x in a.lang.split(",") if x.strip()] if a.lang else None
    if langs and not all(x in CORE_LANGS + EXTRA_LANG_TAGS + LANG_GROUP_TAGS for x in langs):
        print(f"dead-code-checker: unknown language in --lang {a.lang!r}", file=sys.stderr)
        return 2
    if a.report and a.foreign:
        rep = os.path.realpath(os.path.abspath(a.report))
        if any(rep == d or rep.startswith(d.rstrip(os.sep) + os.sep) for d in tree_roots(a.paths, a.root, a.foreign)):
            print("dead-code-checker: --report must be outside the scanned tree (--foreign writes nothing inside it)", file=sys.stderr)
            return 2
    try:
        fs = scan(
            a.paths, root=a.root, search=a.search, settings=a.settings, foreign=a.foreign,
            languages=langs, public_api=a.public_api, jobs=a.jobs,
            use_repo_config=a.use_repo_config,
        )  # fmt: skip
    except ValueError as e:
        print(f"dead-code-checker: {e}", file=sys.stderr)
        return 2
    if a.report:
        meta = [f"paths: {', '.join(a.paths)}", f"languages: {', '.join(langs) if langs else 'all' if a.foreign else 'config'}"]
        top = toplevel(resolve_paths(a.paths, a.root)[0], a.foreign)
        if top:
            if a.foreign:
                head = read_head(top)  # no git run in a foreign tree: HEAD is read from the repository files
                rc, out = (0, head.encode()) if head else (1, b"")
            else:
                rc, out = _git(top, "rev-parse", "HEAD")
            if rc == 0 and out.strip():
                meta.append(f"target HEAD: {out.decode().strip()}")
        write_report(fs, a.report, "deadcode scan", meta, LAST_STATS)
        e = sum(1 for f in fs if f.level == "E")
        print(f"dead-code-checker: {len(fs)} findings (E {e}) -> {a.report}")
        return 1 if e else 0
    return _emit(fs, a.json)


if __name__ == "__main__":
    sys.exit(main())
