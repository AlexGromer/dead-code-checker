"""graph.vcs: the repository as the graph checker sees it: the file list with blob shas, diffs, merge-base, content.

    snapshot(root, want=None, workers=None) -> Tree      every analysable regular file with its git-style blob sha
    Tree.files[path] -> FileRef(path, mode, blob, dirty, tracked);  Tree.id (sha256 of the sorted path/mode/blob list: the
        snapshot key, the same in git and no-git mode);  Tree.git;  Tree.skipped[path] = reason (symlink, submodule, big ...)
    read_files(root, tree, paths) -> {path: bytes}       clean tracked files through ONE `git cat-file --batch`, dirty and
        untracked ones from the work tree (read_regular: no FIFO, no symlink)
    ls_files / status / diff_name_status / changed(root, base) / merge_base / default_base / head_commit
    blob_sha(data, fmt) = sha1("blob <len>\\0" + data): what `git hash-object` gives for an unfiltered file

Clean tracked files take their blob sha from `git ls-files -s -z` without reading the file; a file `git status` calls modified
or untracked is hashed from the work tree (parallel through par.run_units, threads); an unmerged path counts as dirty.
Without a git repository (or when core.git_blocked says git must not run: a foreign tree) the work tree is walked in sorted
order and everything is hashed: the same Tree comes out, only `git` is False. All git runs go through core._git (safe flags,
pinned work tree, timeout). A path is `/`-separated relative to the root; no mtime is ever used as a key.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import threading
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

from .. import core, safepath
from . import par

SKIP_DIRS = (".git", ".hg", ".svn")
GITLINK, SYMLINK = "160000", "120000"


def _run(root: str, *args: str, inp: Optional[bytes] = None) -> Tuple[int, bytes]:
    return core._git(root, *args, inp=inp)


def _ref_ok(ref: str) -> str:
    if not ref or ref.startswith("-") or "\0" in ref or "\n" in ref:
        raise ValueError(f"unsafe git ref {ref!r}")
    return ref


def _paths(raw: bytes) -> List[str]:
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]


def git_ok(root: str) -> bool:
    """git can and may run in `root`: a repository is there and core.git_blocked does not forbid it."""
    return bool(core.nearest_git_root(root)) and not core.git_blocked(root)


def head_commit(root: str) -> Optional[str]:
    """HEAD's commit id, read from the repository files without running git (core.read_head)."""
    return core.read_head(root)


# ---------------------------------------------------------------- hashing
def blob_sha(data: bytes, fmt: str = "sha1") -> str:
    return hashlib.new(fmt, b"blob %d\0" % len(data) + data).hexdigest()


def object_format(root: str) -> str:
    rc, out = _run(root, "rev-parse", "--show-object-format")
    return "sha256" if rc == 0 and out.strip() == b"sha256" else "sha1"


# ---------------------------------------------------------------- git queries
class Entry(NamedTuple):
    path: str
    mode: str
    blob: str
    stage: int


class Change(NamedTuple):
    status: str  # A M D T U R C, "?" for untracked (changed())
    path: str
    old: str = ""  # the previous path of a rename or copy


def ls_files(root: str) -> List[Entry]:
    """`git ls-files -s -z`: [mode blob stage]<TAB>path; sorted by (path, stage)."""
    rc, out = _run(root, "ls-files", "-s", "-z")
    if rc != 0:
        return []
    res = []
    for rec in out.split(b"\0"):
        meta, tab, path = rec.partition(b"\t")
        if not tab:
            continue
        mode, blob, stage = meta.decode().split()
        res.append(Entry(path.decode("utf-8", "surrogateescape"), mode, blob, int(stage)))
    return sorted(res, key=lambda e: (e.path, e.stage))


def status(root: str) -> Tuple[Set[str], Set[str], Set[str]]:
    """(modified, untracked, deleted) against the INDEX: `git status --porcelain=v1 -z -uall --no-renames`. A path staged and
    otherwise unchanged is clean (its index blob is what ls_files gives); unmerged paths are modified."""
    rc, out = _run(root, "status", "--porcelain=v1", "-z", "-uall", "--no-renames", "--ignore-submodules=all")
    mod, new, gone = set(), set(), set()
    if rc != 0:
        return mod, new, gone
    for rec in out.split(b"\0"):
        if len(rec) < 4:
            continue
        xy, path = rec[:2].decode(), rec[3:].decode("utf-8", "surrogateescape")
        if xy == "??":
            new.add(path)
        elif xy != "!!" and (xy[1] != " " or "U" in xy or xy in ("AA", "DD")):
            (gone if xy[1] == "D" else mod).add(path)
    return mod, new, gone


def diff_name_status(root: str, base: str, head: Optional[str] = None, *, cached: bool = False) -> List[Change]:
    """`git diff --name-status -z -M base [head]`; without head the work tree is compared (tracked files only), with
    cached the index. Sorted by path."""
    args = ["diff", "--name-status", "-z", "-M", "--no-ext-diff", "--no-textconv"]
    if cached:
        args.append("--cached")
    args.append(_ref_ok(base))
    if head:
        args.append(_ref_ok(head))
    rc, out = _run(root, *args, "--")
    if rc != 0:
        return []
    toks, res, i = _paths(out), [], 0
    while i < len(toks):
        st = toks[i]
        if st[:1] in "RC" and i + 2 < len(toks):
            res.append(Change(st[0], toks[i + 2], toks[i + 1]))
            i += 3
        elif i + 1 < len(toks):
            res.append(Change(st[0], toks[i + 1]))
            i += 2
        else:
            break
    return sorted(res, key=lambda c: (c.path, c.status))


def changed(root: str, base: str) -> List[Change]:
    """What a branch run needs: the tracked changes against `base` plus the untracked files ("?")."""
    res = diff_name_status(root, base)
    seen = {c.path for c in res}
    return sorted(
        res + [Change("?", p) for p in sorted(status(root)[1]) if p not in seen], key=lambda c: (c.path, c.status)
    )


def base_blobs(root: str, base: str, paths: Iterable[str]) -> Dict[str, str]:
    """{path: blob sha} of `paths` as they are in the commit `base` (`git ls-tree -r -z`); a path not there is absent."""
    want = sorted(set(paths))
    if not want:
        return {}
    rc, out = _run(root, "ls-tree", "-r", "-z", _ref_ok(base), "--", *want)
    res: Dict[str, str] = {}
    for rec in out.split(b"\0") if rc == 0 else ():
        meta, tab, path = rec.partition(b"\t")
        if tab and meta.split()[1:2] == [b"blob"]:
            res[path.decode("utf-8", "surrogateescape")] = meta.split()[2].decode()
    return res


def added_lines(root: str, base: str) -> Dict[str, Set[int]]:
    """{path: added line numbers} of the work tree against `base` (`git diff -U0`): what a branch run calls its hunks."""
    rc, out = _run(root, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "-M", _ref_ok(base))
    return core._parse_added(out) if rc == 0 else {}


def merge_base(root: str, a: str, b: str = "HEAD") -> Optional[str]:
    rc, out = _run(root, "merge-base", _ref_ok(a), _ref_ok(b))
    return out.decode().strip() or None if rc == 0 else None


def _verify(root: str, ref: str) -> bool:
    return _run(root, "rev-parse", "--verify", "--quiet", _ref_ok(ref) + "^{commit}")[0] == 0


def default_base(root: str, ref: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """(merge-base commit, ref used) for a branch check: `ref` when given, else the upstream, origin/HEAD, origin/main|master,
    main|master. None when none exists (no repository, a fresh one without any of them)."""
    for cand in (
        [ref] if ref else ["@{upstream}", "refs/remotes/origin/HEAD", "origin/main", "origin/master", "main", "master"]
    ):
        if _verify(root, cand):
            mb = merge_base(root, cand)
            if mb:
                return mb, cand
    return None


# ---------------------------------------------------------------- one cat-file --batch
def cat_batch(root: str, shas: Iterable[str], max_bytes: Optional[int] = None) -> Dict[str, Optional[bytes]]:
    """Contents of blobs through ONE `git cat-file --batch` process; None = missing, or larger than max_bytes (default
    core.MAX_FILE_BYTES). {} when git may not run here."""
    limit = core.MAX_FILE_BYTES if max_bytes is None else max_bytes
    want = sorted(set(shas))
    if not want or core.git_blocked(root):
        return {}
    out: Dict[str, Optional[bytes]] = {}
    try:
        p = safepath.popen(
            ["git", "-C", root, *core.GIT_SAFE, "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=core._git_env(root),
        )
    except OSError:
        return {}
    timer = threading.Timer(60, p.kill)
    timer.start()
    try:
        for sha in want:
            p.stdin.write(sha.encode() + b"\n")
            p.stdin.flush()
            hdr = p.stdout.readline().split()
            if len(hdr) == 2 and hdr[1] == b"missing":
                out[sha] = None
                continue
            if len(hdr) != 3 or hdr[1] != b"blob":
                out[sha] = None
                if len(hdr) == 3:  # some other object type: skip its body
                    p.stdout.read(int(hdr[2]) + 1)
                    continue
                break
            size = int(hdr[2])
            data = p.stdout.read(size)
            p.stdout.read(1)
            out[sha] = data if size <= limit else None
    except (OSError, ValueError):
        pass
    finally:
        timer.cancel()
        for s in (p.stdin, p.stdout):
            try:
                s.close()
            except OSError:
                pass
        p.kill()
        p.wait()
    return out


# ---------------------------------------------------------------- the tree
class FileRef(NamedTuple):
    path: str
    mode: str  # "100644" / "100755"
    blob: str
    dirty: bool  # blob was hashed from the work tree, not taken from the index
    tracked: bool


class Tree(NamedTuple):
    files: Dict[str, FileRef]
    id: str
    git: bool
    skipped: Dict[str, str]


def tree_id(files: Dict[str, FileRef]) -> str:
    h = hashlib.sha256()
    for p in sorted(files):
        f = files[p]
        h.update(f"{p}\0{f.mode}\0{f.blob}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _mode(st_mode: int) -> str:
    """git's rule for a regular file (ce_mode_from_stat): executable iff the user x bit is set."""
    return "100755" if st_mode & stat.S_IXUSR else "100644"


def _hash_unit(args: Tuple[str, str, str]) -> Tuple[str, str, str]:
    """(root, path, fmt) -> (path, blob, mode-or-skip-reason); a reason starts with '!'."""
    root, path, fmt = args
    full = os.path.join(root, *path.split("/"))
    st, data = core.read_regular(full)
    if st != "ok":
        return path, "", "!" + st
    try:
        mode = _mode(os.lstat(full).st_mode)
    except OSError:
        return path, "", "!gone"
    return path, blob_sha(data or b"", fmt), mode


def _walk(root: str) -> List[str]:
    """The regular files under `root`, sorted ("/"-separated relative paths); no symlink is followed and none is listed, and
    nothing below `.git`, `.hg` or `.svn` is."""
    out = []
    for dp, dns, fns in os.walk(root, followlinks=False):
        rel = os.path.relpath(dp, root)
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not os.path.islink(os.path.join(dp, d)))
        for fn in sorted(fns):
            if not os.path.islink(os.path.join(dp, fn)):
                out.append(fn if rel == "." else f"{rel}/{fn}".replace(os.sep, "/"))
    return sorted(out)


def snapshot(root: str, want: Optional[Callable[[str], bool]] = None, workers: Optional[int] = None) -> Tree:
    """The tree of `root` as a Tree. `want(path) -> bool` limits the files (default all) so that only analysable files are
    hashed or read; the Tree.id covers exactly the wanted files."""
    keep = want or (lambda p: True)
    git = git_ok(root)
    files: Dict[str, FileRef] = {}
    skipped: Dict[str, str] = {}
    todo: List[str] = []  # to hash from the work tree
    fmt, tracked = "sha1", set()
    if git:
        fmt = object_format(root)
        mod, new, gone = status(root)
        entries = ls_files(root)
        tracked = {e.path for e in entries}
        for e in entries:
            if e.mode in (GITLINK, SYMLINK):
                skipped[e.path] = "submodule" if e.mode == GITLINK else "symlink"
            elif keep(e.path) and e.path not in gone:
                if e.stage == 0 and e.path not in mod:
                    files[e.path] = FileRef(e.path, e.mode, e.blob, False, True)
                elif not todo or todo[-1] != e.path:
                    todo.append(e.path)
        todo += [p for p in sorted(new) if keep(p)]
    else:
        todo = [p for p in _walk(root) if keep(p)]
    units = [(root, p, fmt) for p in todo]
    for path, blob, mode in par.run_units(units, _hash_unit, jobs=workers, kind="thread", key=lambda u: u[1]):
        if mode.startswith("!"):
            skipped[path] = mode[1:]
        else:
            files[path] = FileRef(path, mode, blob, True, path in tracked)
    return Tree(dict(sorted(files.items())), tree_id(files), git, dict(sorted(skipped.items())))


def read_files(root: str, tree: Tree, paths: Iterable[str]) -> Dict[str, bytes]:
    """Content of `paths` (those not in the tree or unreadable are absent): clean files from the object store in one
    cat-file batch, the others (dirty, untracked, no-git mode, or not served by git) from the work tree."""
    out: Dict[str, bytes] = {}
    by_blob: Dict[str, List[str]] = {}
    disk: List[str] = []
    for p in sorted(set(paths)):
        f = tree.files.get(p)
        if f is not None:
            (disk if f.dirty or not tree.git else by_blob.setdefault(f.blob, [])).append(p)
    got = cat_batch(root, by_blob) if by_blob else {}
    for blob, ps in by_blob.items():
        if got.get(blob) is None:
            disk += ps
        else:
            out.update({p: got[blob] for p in ps})
    read = core.read_regular
    for p in sorted(disk):
        st, data = read(os.path.join(root, *p.split("/")))
        if st == "ok" and data is not None:
            out[p] = data
    return dict(sorted(out.items()))
