"""graph.semcache: the semantic cache. What an indexer or an engine computed for a package is kept in
the store (table engine_result, no schema change) under the hash of the files it was computed from, so a level uses it only while the
files are the same.

    sem:<engine>:<root>:<package_hash>   {"meta": sem.Meta fields, "index": base64 of the SCIP blob}   (JSON)
    eng:<tool>:<input_hash>              adapters.to_payload(matches)
    package_hash = input_hash = sem.package_hash(sem.package_files(tree, root, family)); `root` is the package root below the
    repository ("" = the root), the family is the one of the engine in the registry (family_of); an eng: item is made of the
    whole repository (root "") because the matches of a tool are those of all its jobs.

The record of what the cache holds lives in the store's meta (key `semcache`): {key: {"t": epoch, "fam": family, "ver": engine
version}}. It is what the garbage collection and the lookups read, so no payload is opened to ask "which package, how old". A hit
refreshes "t" (the cache of an unchanged package never ages out).

    put_index(store, files, engine, root, data, ver)      stamp (sem.meta_for) and store; SemError for an index above data/sem.json
    put_matches(store, files, tool, matches, ver)         the matches of a tool that ran in full
    family_of(engine, files=, root=)                      the family of a package: of several (java and kotlin) the one with files there
    get_index(store, key) -> (Meta, bytes) | None         None for a missing row, a damaged payload or a blob that is not the stamped one
    get_matches(store, key) -> [(node, exact, Hit)] | None
    split_cached(store, files, tasks, reuse, lock)        the jobs that still have to run, and a row for each one the cache holds (an index
                                                          that is damaged is not "held": its job runs and writes it again)
    overlay(store, graph, files, text_of) -> Refined      sem.refine over the cached indexes whose hash is the current one; a package
                                                          that still exists and has only older ones is W dc.sem-stale (one that is
                                                          gone, deleted or renamed, is not: its entries age out)
    gc(store, now) -> rows removed                        the newest KEEP hashes per (engine, root) and per tool, nothing older than
                                                          MAX_AGE_S, no row without a record and no record without a row
    keep_matches(store, files, eng, matches)              store what the tools of a gather ran in full, then gc: every caller that
                                                          writes eng: rows collects them in the same call
Every read-modify-write of the record, and the write of an item with its record, is one store transaction, so two processes on one
store do not lose each other's entries. Every time stamp takes `now` (the clock of the caller; None = the host clock).
Nothing here is a gate: a damaged item is a miss, never an error for the caller.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import tempfile
import time
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from . import adapters, engines as dce, langs as _langs, safety, sem
from .model import Finding

SEM, ENG, RECORD = "sem:", "eng:", "semcache"
KEEP = 3  # hashes kept per (engine, root) and per tool
MAX_AGE_S = 30 * 86400.0


def family_of(
    engine: str, reg: Optional[dict] = None, files: Optional[Mapping[str, str]] = None, root: str = ""
) -> str:
    """The language family an engine of the registry works on (a language with it in the role audit or sem); "" for an engine of every
    language (ast-grep, dot): it has no package, so nothing of it is cached. An engine of several families (cs: java and kotlin) gets
    the one that has files below `root` of `files`, else the first: a package is hashed over its own family, not over the first one.
    """
    fams = [
        fam
        for fam, spec in (reg or dce.load_registry())["languages"].items()
        if fam != "*"
        and any(e["engine"] == engine for r in ("audit", "sem") for e in (spec.get("roles") or {}).get(r, ()))
    ]
    if len(fams) > 1 and files is not None:
        lg, pre = _langs.load(), root.strip("/")
        pre = pre + "/" if pre else ""
        have = {lg.family(la) for p in files if p.startswith(pre) and (la := lg.detect(p))}
        fams.sort(key=lambda f: f not in have)  # stable: the families with files first, the registry order inside each
    return fams[0] if fams else ""


def _present(files: Mapping[str, str], root: str, fam: str) -> bool:
    """Whether `files` have a source file of the family `fam` below `root`: a package that was deleted or renamed has none."""
    lg, pre = _langs.load(), root.strip("/")
    pre = pre + "/" if pre else ""
    return any(p.startswith(pre) and (la := lg.detect(p)) and lg.family(la) == fam for p in files)


def blobs(tree) -> Dict[str, str]:
    """{path: blob} of a vcs.Tree (or of a mapping that already is one)."""
    files = getattr(tree, "files", tree)
    return {p: getattr(f, "blob", f) for p, f in files.items()}


def sem_key(engine: str, root: str, phash: str) -> str:
    return f"{SEM}{engine}:{root.strip('/')}:{phash}"


def eng_key(tool: str, ihash: str) -> str:
    return f"{ENG}{tool}:{ihash}"


def _split(key: str) -> Tuple[str, str, str]:
    """(kind, name, root) of a key; the root of an eng: key is ""."""
    kind, _, rest = key.partition(":")
    name, _, rest = rest.partition(":")
    return kind, name, rest.rpartition(":")[0] if kind == "sem" else ""


def _hash(files: Mapping[str, str], root: str, fam: str, memo: Dict[Tuple[str, str], str]) -> str:
    k = (root.strip("/"), fam)
    if k not in memo:
        memo[k] = sem.package_hash(sem.package_files(files, root, fam))
    return memo[k]


# ---------------------------------------------------------------- the record
def _load(store) -> Dict[str, dict]:
    """The record. An entry that is not one ({"t": number, "fam": string}) is left out alone: its row is then "a row without a record"
    for the next gc and goes, the cache of every other package stays. A record that is no JSON object is an empty one.
    """
    try:
        d = json.loads(store.get_meta(RECORD) or "{}")
    except ValueError:
        return {}
    if not isinstance(d, dict):
        return {}
    return {
        k: v
        for k, v in d.items()
        if isinstance(v, dict) and isinstance(v.get("t"), (int, float)) and isinstance(v.get("fam"), str)
    }


def _save(store, rec: Mapping[str, dict]) -> None:
    store.set_meta(RECORD, json.dumps(rec, sort_keys=True, separators=(",", ":")))


def _touch(store, keys: Mapping[str, Tuple[str, str]], now: Optional[float] = None) -> None:
    """Record {key: (family, version)} as seen now (one transaction: the record is read and written under the write lock)."""
    now = time.time() if now is None else now
    with store.tx():
        rec = _load(store)
        for k, (fam, ver) in keys.items():
            rec[k] = {"t": now, "fam": fam, "ver": ver}
        _save(store, rec)


# ---------------------------------------------------------------- put / get
def put_index(
    store, files: Mapping[str, str], engine: str, root: str, data: bytes, ver: str = "", now: Optional[float] = None
) -> Optional[str]:
    """The key an index was stored under, None for an engine without a family. sem.SemError for an index that sem would not read."""
    fam = family_of(engine, files=files, root=root)
    if not fam:
        return None
    if len(data) > sem.config()["max_index_bytes"]:
        raise sem.SemError(f"{engine} index of {root or '.'} is {len(data)} bytes: above the limit of data/sem.json")
    m = sem.meta_for(data, files, root, fam, engine)
    body = {"meta": m._asdict(), "index": base64.b64encode(data).decode("ascii")}
    key = sem_key(engine, m.root, m.input_hash)
    with store.tx():  # the row and its record together: a gc of another process never sees a row without a record
        store.put_engine_result(key, engine, json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
        _touch(store, {key: (fam, ver)}, now)
    return key


def get_index(store, key: str) -> Optional[Tuple[sem.Meta, bytes]]:
    raw = store.get_engine_result(key)
    try:
        body = json.loads(raw) if raw else None
        m = sem.Meta(*(str(body["meta"][k]) for k in sem.Meta._fields))
        data = base64.b64decode(body["index"], validate=True)
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None
    return (m, data) if hashlib.sha256(data).hexdigest() == m.index_sha256 else None


def put_matches(
    store, files: Mapping[str, str], tool: str, matches: Iterable, ver: str = "", now: Optional[float] = None
) -> Optional[str]:
    fam = family_of(tool, files=files)
    if not fam:
        return None
    key = eng_key(tool, _hash(files, "", fam, {}))
    with store.tx():
        store.put_engine_result(key, tool, adapters.to_payload(matches))
        _touch(store, {key: (fam, ver)}, now)
    return key


def get_matches(store, key: str) -> Optional[list]:
    raw = store.get_engine_result(key)
    try:
        return adapters.from_payload(raw) if raw is not None else None
    except (ValueError, KeyError, TypeError, IndexError):
        return None


# ---------------------------------------------------------------- incremental runs
def split_cached(
    store, files: Mapping[str, str], tasks: Sequence, reuse: Sequence[str], lock, now: Optional[float] = None
) -> Tuple[list, List[dict]]:
    """(tasks to run, rows of the cached ones). A job is cached when its item is in the store with the engine version the lock names
    now: an indexer job (reuse has "index") by sem:<engine>:<rel>:<hash of its package>, an engine job ("eng") by the eng: item of its
    tool. An index that cannot be read back (get_index) is not cached: its job runs and writes it again. A hit refreshes the record.
    A task that is no job (an extra layer) always runs. "eng" in `reuse` only SKIPS a tool's job: its cached matches are not applied
    here (get_matches reads them), so that job has no Run for adapters.reconcile. That is for a caller that fills the cache (`graph
    index`); a run that must reconcile engine output with the graph never sets it.
    """
    rec, have, memo = _load(store), set(store.engine_keys(SEM)) | set(store.engine_keys(ENG)), {}
    run, rows, seen, reg = [], [], {}, dce.load_registry()
    for t in tasks:
        j = t.job
        wants = j is not None and ("index" if j.index else "eng") in reuse
        fam = family_of(j.engine, files=files, root=j.rel if j.index else "") if wants else ""
        if not fam:
            run.append(t)
            continue
        h = _hash(files, j.rel if j.index else "", fam, memo)
        key = sem_key(j.engine, j.rel, h) if j.index else eng_key(j.engine, h)
        ver = dce.engine_version(reg, lock, j.engine, j.argv[0])
        if key in have and (rec.get(key) or {}).get("ver") == ver and (not j.index or get_index(store, key)):
            seen[key] = (fam, ver)
            rows.append(
                {"engine": j.engine, "label": j.label, "status": "cached", "detail": f"files unchanged ({h[:12]})"}
            )
        else:
            run.append(t)
    if seen:
        _touch(store, seen, now)
    return run, rows


# ---------------------------------------------------------------- the overlay
def overlay(
    store, graph, files: Mapping[str, str], text_of: Optional[Callable[[str], Optional[str]]] = None
) -> sem.Refined:
    """sem.refine over the cached indexes of the current files. Reading a damaged item is a miss; a package with only older indexes
    is one W dc.sem-stale (there is nothing to apply, and the graph's own verdicts stand for it)."""
    rec, memo = _load(store), {}
    fresh: List[str] = []
    seen: Dict[Tuple[str, str], bool] = {}
    fams: Dict[Tuple[str, str], str] = {}
    for key in sorted(k for k in rec if k.startswith(SEM)):
        _, engine, root = _split(key)
        ok = key.rpartition(":")[2] == _hash(files, root, rec[key]["fam"], memo)
        seen[(engine, root)] = seen.get((engine, root), False) or ok
        fams[(engine, root)] = rec[key]["fam"]
        if ok:
            fresh.append(key)
    notes = [
        Finding(
            "W",
            safety.WHERE,
            0,
            "dc.sem-stale",
            f"no cached {engine} index matches the files of {root or '.'} now: the graph's own verdicts stand there (run `dead-code-checker graph index`)",
        )
        for (engine, root), ok in sorted(seen.items())
        if not ok and _present(files, root, fams[(engine, root)])  # a package that is gone has nothing to be stale for
    ]
    with tempfile.TemporaryDirectory(prefix="dcc-sem-") as tmp:
        paths = []
        for key in fresh:  # <engine>/<root>/index.scip: the name a W shows, the same every night
            got = get_index(store, key)
            if got is None:
                notes.append(
                    Finding("W", safety.WHERE, 0, "dc.sem-index", f"cached index {key[:60]} is damaged: ignored")
                )
                continue
            path = os.path.join(tmp, got[0].engine, got[0].root, "index.scip")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(got[1])
            sem.write_meta(path, got[0])
            paths.append(path)
        out = sem.refine(graph, files, paths, text_of=text_of) if paths else sem.Refined(graph, (), (), frozenset())
        pre = tmp + os.sep
        for f in out.findings:  # the temporary directory is no part of what a W says
            f.msg = f.msg.replace(pre, "")
        found = out.findings
        reports = tuple(r._replace(index=r.index.replace(pre, "")) for r in out.reports)
    return out._replace(findings=tuple(notes) + found, reports=reports)


def keep_matches(store, files: Mapping[str, str], eng, matches: Iterable, now: Optional[float] = None) -> int:
    """Store the matches of every tool that ran in full in `eng` (jobs.Engines): a tool with a failed job, a bad exit code or no run
    keeps what the cache had. Then the garbage collection, so no caller that writes eng: rows leaves them to grow. Returns the number
    of tools stored."""
    by: Dict[str, list] = {}
    for m in matches:
        by.setdefault(m.hit.tool, []).append(m)
    ver = {r.tool: r.ver for r in eng.runs}
    bad = set(eng.down) | {r.tool for r in eng.runs if not adapters.rc_ok(r.tool, r.rc)}
    tools = sorted(set(ver) - bad)
    for t in tools:
        put_matches(store, files, t, by.get(t, []), ver[t], now)
    gc(store, now)
    return len(tools)


# ---------------------------------------------------------------- garbage collection
def gc(store, now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    with store.tx():
        rec, rows = _load(store), set(store.engine_keys(SEM)) | set(store.engine_keys(ENG))
        groups: Dict[Tuple[str, str, str], List[str]] = {}
        for k in rec:
            groups.setdefault(_split(k), []).append(k)
        drop: Set[str] = {k for k in rec if now - rec[k]["t"] > MAX_AGE_S}
        for ks in groups.values():
            drop.update(sorted(ks, key=lambda k: (-rec[k]["t"], k))[KEEP:])
        gone = (rows - set(rec)) | (drop & rows)
        _save(store, {k: v for k, v in rec.items() if k not in drop and k in rows})
        return store.delete_engine_results(sorted(gone))
