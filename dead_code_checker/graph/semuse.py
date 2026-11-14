"""graph.semuse: the USE side of the semantic cache. Stdlib and graph modules only.

The cache lives in store.engine_result (no schema change); `graph index` fills it. Two kinds of item, both tied to the files they were
made from by `sem.package_hash(sem.package_files(tree, root, family))`:

    sem:<engine>:<root>:<package_hash>   JSON {"meta": sem.Meta fields, "index": base64 of the SCIP blob}
    eng:<tool>:<input_hash>              adapters.to_payload(matches) of one engine run

    proof(root, graph, tree, store, *, notes=None, paths=None, deadline=None, clock=time.monotonic) -> (graph', verdict.Proof)
        every item whose hash is the hash of its package NOW (an eng: item: and whose engine version is the one the lock names now,
        as semcache.split_cached asks) is applied: the SCIP blobs go through sem.refine (refuted edges leave the graph, so they
        change reach; the returned graph is the refined one), the S-covered paths of the overlays that applied in full and the exact
        engine matches become the Proof (graph.verdict). A stale item (hash differs) is not applied; one W dc.sem-stale names them
        (the indexer has to run again).
        paths     only the items whose package holds one of these paths (the packages of the changed files); a stale item of a package
                  that holds none of them is not named either.
        deadline  `clock()` value after which nothing more is started: the package hashes stop (every 512 files) and a blob whose
                  estimated apply time (data/sem.json apply_bytes_per_s, measured: sem.refine parses 2-3 MB/s) does not fit into what
                  is left is NOT applied. Both are one W dc.sem-budget; the Proof then holds what was read (nothing, when the hashes
                  stopped).
    Nothing here raises: a cache that cannot be read, a damaged item, a refine that fails are W findings appended to `notes` (the
    caller's list) and the graph is returned as it was, with the Proof of what could be read. Never an E.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import tempfile
import time
from functools import lru_cache
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from . import engines as _eng
from . import langs as _langs
from . import report, safety, sem, semcache, verdict
from .model import Finding

SEM, ENG = "sem:", "eng:"
ROLES = {SEM: ("sem",), ENG: ("branch", "audit")}
MAX_TEXT = 4_000_000  # a file larger than this is not read for the refutation text


class Late(Exception):
    """The deadline of the caller passed while the package hashes were being made."""


WHY = {
    "dc.sem-budget": "the semantic cache is applied inside the time of the level: what does not fit is left out, the claims stay as they were",
}
report.register_why(WHY)


def _w(code: str, msg: str) -> Finding:
    return Finding("W", safety.WHERE, 0, code, msg, {**sem.WHY, **WHY}.get(code, ""))


def _registry() -> dict:
    return _eng.load_registry()


def _families(engine: str, roles: Sequence[str], lg) -> List[str]:
    """The language families an engine works on (data/engines.json roles); every family that has manifests when it is unknown."""
    fams = {
        (lg.family(lang) if lang in lg.langs else lang) or lang
        for lang, spec in _registry()["languages"].items()
        for r in roles
        if any(e["engine"] == engine for e in spec.get("roles", {}).get(r, ()))
    }
    return sorted(fams or sem.config()["manifests"])


class _Lg:
    """The langs table with detect() memoised (one package hash after another walks the same tree) and the deadline looked at
    every 512 files: a big tree stops the hashing, it does not overrun the level."""

    def __init__(self, lg, over: Callable[[], bool]):
        det, n = lru_cache(maxsize=None)(lg.detect), [0]

        def detect(path: str):
            n[0] += 1
            if n[0] % 512 == 0 and over():
                raise Late
            return det(path)

        self.langs, self.family, self.detect = lg.langs, lg.family, detect


def _split(key: str, prefix: str) -> Optional[Tuple[str, str, str]]:
    """(engine or tool, root, hash) of a cache key; None when it is not one."""
    name, _, rest = key[len(prefix) :].partition(":")
    root, _, h = rest.rpartition(":") if prefix == SEM else ("", "", rest)
    return (name, root, h) if name and h else None


def _blobs(tree) -> Mapping[str, str]:
    files = getattr(tree, "files", None)
    return tree if files is None else {p: f.blob for p, f in files.items()}


def _current(store, blobs: Mapping[str, str], lg, paths: Optional[Set[str]], over: Callable[[], bool]):
    """-> (sem items [(key, engine, root, family, package paths)], eng keys, stale names). An item is current when its hash is the
    hash of its package in `blobs`; it is wanted, and named when stale, when `paths` is None or the package holds one of them. An eng:
    item is also stale when the engine version in the record of the store is not the one the lock names now (split_cached's rule: a
    new engine may say something else about the same files; an item without a record has an unknown version).
    """
    lg, memo = _Lg(lg, over), {}

    def package(root: str, fam: str):
        if (root, fam) not in memo:
            files = sem.package_files(blobs, root, fam, lg)
            memo[(root, fam)] = (sem.package_hash(files), frozenset(files))
        return memo[(root, fam)]

    def wanted(root: str, fam: str) -> bool:
        return paths is None or not paths.isdisjoint(package(root, fam)[1])

    def roots_of(fams) -> List[str]:  # the engine run of a tree, or of a module of it (a manifest's directory)
        mani = {m for f in fams for m in sem.config()["manifests"].get(f, ())}
        return sorted({""} | {os.path.dirname(p) for p in blobs if os.path.basename(p) in mani})

    live, got, seen, hit = (
        [],
        [],
        set(),
        set(),
    )  # seen: the names of a wanted package with an item, hit: the names with a current one
    for key in store.engine_keys(SEM):
        s = _split(key, SEM)
        if s is None:
            continue
        if over():
            raise Late
        engine, root, h = s
        name, pkg, fams = f"{engine} {root or '.'}", root.strip("/"), _families(engine, ROLES[SEM], lg)
        fam = next((f for f in fams if package(pkg, f)[0] == h), None)
        if fam is not None:
            hit.add(name)
            if wanted(pkg, fam):
                live.append((key, engine, root, fam, package(pkg, fam)[1]))
        elif any(wanted(pkg, f) for f in fams):
            seen.add(name)
    rec, lock, vers = semcache._load(store), _lock(), {}
    for key in store.engine_keys(ENG):
        s = _split(key, ENG)
        if s is None:
            continue
        if over():
            raise Late
        tool, _, h = s
        cells = [(r, f) for f in _families(tool, ROLES[ENG], lg) for r in roots_of([f])]
        same = [(r, f) for r, f in cells if package(r, f)[0] == h]
        if same and (rec.get(key) or {}).get("ver") == vers.setdefault(tool, _version(lock, tool)):
            hit.add(tool)
            got.append(key)
        elif any(wanted(r, f) for r, f in (same or cells)):
            seen.add(tool)
    return live, got, sorted(seen - hit)


def _lock() -> Optional[dict]:
    """The engine lock of this host (what semcache.split_cached is given); None when there is none or it cannot be read."""
    try:
        return _eng.cached_lock()
    except (OSError, ValueError):
        return None


def _version(lock: Optional[dict], tool: str) -> str:
    """The version of the engine as the host has it now (graph.engines.engine_version; "" when it is not found)."""
    return _eng.engine_version(_eng.load_registry(), lock, tool)


def _decode(store, key: str, engine: str, root: str) -> Tuple[sem.Meta, bytes]:
    d = json.loads(store.get_engine_result(key) or b"")
    meta = sem.Meta(*(str(d["meta"][k]) for k in sem.Meta._fields))
    blob = base64.b64decode(d["index"], validate=True)
    if meta.engine != engine or meta.root != root.strip("/") or hashlib.sha256(blob).hexdigest() != meta.index_sha256:
        raise ValueError("the item is not the index its key and stamp name")
    return meta, blob


def _matches(store, keys: Iterable[str], notes: List[Finding]) -> list:
    from . import adapters

    out = []
    for key in keys:
        try:
            out += [
                adapters.Match(n, h, ex, -1) for n, ex, h in adapters.from_payload(store.get_engine_result(key) or b"")
            ]
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            notes.append(_w("dc.sem-index", f"engine result {key} ignored: {type(exc).__name__}: {exc}"))
    return out


def reader(root: str, tree) -> Callable[[str], Optional[str]]:
    from . import vcs

    seen: Dict[str, Optional[str]] = {}

    def text_of(path: str) -> Optional[str]:
        if path not in seen:
            data = None
            if getattr(tree, "files", None) is not None:
                data = vcs.read_files(root, tree, [path]).get(path)
            else:
                with contextlib.suppress(OSError), open(os.path.join(root, *path.split("/")), "rb") as fh:
                    data = fh.read(MAX_TEXT)
            seen[path] = None if data is None or len(data) > MAX_TEXT else data.decode("utf-8", "replace")
        return seen[path]

    return text_of


def _overlay(root, graph, tree, store, live, notes, text_of, workers, left):
    """sem.refine of the current sem items: (graph, covered paths). `left()` = seconds of the level that are left (None: no limit): an
    item whose estimated apply time (its size over data/sem.json apply_bytes_per_s, the smallest first, one worker) does not fit
    into them is not applied, one W dc.sem-budget names it."""
    rate, size, items = float(sem.config()["apply_bytes_per_s"]), 0, []
    for key, engine, r, _fam, _files in live:
        try:
            meta, blob = _decode(store, key, engine, r)
            items.append((key, blob, meta))
        except (ValueError, TypeError, KeyError, AttributeError, binascii.Error) as exc:
            notes.append(_w("dc.sem-index", f"semantic cache item {key} ignored: {type(exc).__name__}: {exc}"))
    paths: List[str] = []
    with tempfile.TemporaryDirectory(prefix="dcc-semuse-") as tmp:
        for i, (key, blob, meta) in enumerate(sorted(items, key=lambda it: (len(it[1]), it[0]))):
            if left is not None and (size + len(blob)) / rate > left():
                why = (
                    f"{len(blob) >> 20} MB would take about {len(blob) / rate:.0f} s, {max(0.0, left()):.0f} s are left"
                )
                notes.append(_w("dc.sem-budget", f"semantic cache item {key} not applied: {why}"))
                continue
            size += len(blob)
            ix = os.path.join(tmp, f"{i}.scip")
            with open(ix, "wb") as fh:
                fh.write(blob)
            with open(sem.meta_path(ix), "w", encoding="utf-8") as fh:
                json.dump({"schema": 1, **meta._asdict()}, fh)
            paths.append(ix)
        r = sem.refine(graph, _blobs(tree), paths, workers=workers, text_of=text_of or reader(root, tree))
    notes += r.findings
    return r.graph, r.covered


def proof(
    root: str,
    graph,
    tree,
    store,
    *,
    notes: Optional[list] = None,
    paths: Optional[Iterable[str]] = None,
    text_of: Optional[Callable[[str], Optional[str]]] = None,
    workers: Optional[int] = None,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
):
    notes = [] if notes is None else notes
    if store is None:
        return graph, verdict.build()
    over = (lambda: False) if deadline is None else (lambda: clock() >= deadline)
    try:
        live, eng, stale = _current(store, _blobs(tree), _langs.load(), None if paths is None else set(paths), over)
        if stale:
            notes.append(
                _w(
                    "dc.sem-stale",
                    f"semantic cache older than the files of its package or than the engine, ignored: {', '.join(stale[:6])}",
                )
            )
        left = None if deadline is None else lambda: deadline - clock()
        graph, covered = (
            _overlay(root, graph, tree, store, live, notes, text_of, workers, left) if live else (graph, frozenset())
        )
        return graph, verdict.build(covered, _matches(store, eng, notes))
    except Late:
        notes.append(_w("dc.sem-budget", "the semantic cache was not read: the time of this level ran out first"))
        return graph, verdict.build()
    except Exception as exc:  # noqa: BLE001 - a cache that cannot be read costs the proof, never the check
        notes.append(Finding(*safety.check_failed("semantic cache", exc)))
        return graph, verdict.build()
