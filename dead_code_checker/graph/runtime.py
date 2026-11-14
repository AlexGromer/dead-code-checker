"""graph.runtime: the runtime-signal mode, the controlling half: `dead-code-checker graph runtime [--root DIR] [--host HOST] [--classes] [--json]`.

The tests (or a configured command) of a project run UNDER A TRACER on ONE separate execution target, an ssh host the user names, inside
a bubblewrap box there (graph.rtbox), and only the set of symbols that ran comes back. The effect is one direction only: a symbol that ran
can never be reported dead or dead-cluster by `graph check` (it becomes `dc.alive-runtime`, level I, with the run named); a symbol that did
not run adds nothing (coverage is not evidence of death), and the confirmation rule and the budgets of graph.verdict are untouched.

THE TRUST MODEL. Whatever EXECUTES project code (tests, build scripts, coverage and trace runs) runs only on the target, inside its box;
results come back as data. THIS MODULE HAS NO LOCAL EXECUTION PATH: the only processes started on this machine are git (reading) and ssh
(graph.rttransport). The target is the `host` of `[graph.runtime]` in `.dead-code-checker.toml`, but a repository does not choose where its own
code runs: the host is used only when the user also allows it ($DEAD_CODE_CHECKER_RUNTIME_HOSTS names it) or gives it on the command line
(`--host`, which skips the list; the host still has to be a valid ssh destination). A host that is this machine (loopback, its own host name,
FQDN or any of its addresses) is refused. Without a usable target the step is SKIPPED with exactly ONE line (rtcfg.skip_line, level I, code
dc.runtime-skipped) and nothing runs here. The ssh host key has to be in the user's known_hosts already; a new key is never accepted.

    run(root, rt, host=None, ...) -> Outcome       the whole round trip: target, project copy, put, run, pull, checks, stored run
    target_for(rt, environ, host, make, user, port) -> (transport | None, why)    the rules above (nothing is contacted)
    exec_target(root, cfg=None, environ=None, host=None, make=None, user=None, port=None) -> (ExecTarget | None, why)
                                                   THE execution target as graph.fixverify sees it (name, unavailable(), run(job)): an executing check
                                                   of a fix is a CHECK RUN of the same delivery (the tracked files at HEAD with the patched texts over
                                                   them, one command per package directory, the exit code and one error line back)
    pack(root, commit, runs, timeout, target, run_id, edits=None) -> Package
                                                   tar of spec.json, the tool's own rtbox.py and tracer, and the tracked files AT HEAD (git ls-tree: exact
                                                   blob bytes, never the work tree; no symlink out of the tree, no secret-like name or content)
    exchange(tr, rid, pkg, commit, wait_s) -> (doc | None, "skip" | "fail" | "", line)    put, run, pull, cleanup, the checks of parse_delivery
    parse_delivery(tar, sent, run_id, commit) -> (doc, why)   manifest, sha256, size caps, then the strict schema of the result against what WE
                                                   sent (paths, blob ids): nothing from the target is believed beyond that
    store_run / load_latest / list_runs            <project data dir>/runtime/<utc>-<head8>/{runtime.json,report.md}; the newest keep_runs are kept (the
                                                   directory is no run directory of graph.artifacts: pages and prune ignore it)
    alive(nodes, doc, blob_of, label)              node key -> note for the symbols of the graph that ran: a function, method or class whose span holds
                                                   the first line of an executed function of the SAME file, when that file has the blob id it had in the
                                                   run (a file that changed since is not used: its lines moved)
    ran_map(nodes, doc, blob_of)                   node key -> True (ran) | False (its file was in the run and it did not run): the `runtime` input of
                                                   graph.confidence, a second axis that never changes a verdict
    alive_finding(node, why, alive)                the I finding that replaces a dead claim (also for the members of a dead cluster one of which ran)
    view(root, built, rt, ...) -> View             what the newest stored run says about a graph: `graph check` reads it, never contacts the target
    replace_claims(found, graph, res, alive)       the findings of a check with the dead claims about symbols that ran replaced

Codes: dc.alive-runtime (I), dc.runtime-skipped (I: no target, no tracer, not a repository), dc.runtime-failed (W: the target answered and the
command or the delivery failed), dc.runtime-stale (I: the stored run is older than refresh_days; it is still used). They never block.
"""

from __future__ import annotations

import bisect
import calendar
import hashlib
import io
import json
import os
import shutil
import tarfile
import time
import uuid
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from .. import safepath
from . import par, paths, rtbox, rtcfg, rttransport, safety, vcs, verdict
from .model import Finding, split_key

SKIPPED, FAILED, ALIVE, STALE = "dc.runtime-skipped", "dc.runtime-failed", "dc.alive-runtime", "dc.runtime-stale"
KINDS = ("func", "method", "class")  # the nodes a hit is matched to (the kinds the dead-symbol claims are about)
DEAD_CLAIMS = ("dc.dead", "dc.dead-cluster", "dc.introduced-dead", "dc.orphaned-by-branch")
LINK_HOPS = 40


class PackError(Exception):
    """The project copy cannot be made or must not leave this host: the reason is the one line of the step."""


# ---------------------------------------------------------------- the target
def make_transport(target: rttransport.Target, environ: Optional[dict] = None):
    """The transport of a target (the seam of the tests: pass `make=` to run(), target_for() and exec_target())."""
    d = rtcfg.data()
    return rttransport.SshTransport(target, d["timeouts"], d["result"]["max_bytes"])


def _login(h: str, env, user: Optional[str], port: Optional[int]) -> Tuple[str, int, str]:
    """(user, port, why) for the host `h`: the user's side only, never the repository's file. A value of the command line (--user, --port)
    wins over the user's entry `[user@]host[:port]` of $DEAD_CODE_CHECKER_RUNTIME_HOSTS; "" and 0 leave the choice to the user's ssh
    configuration."""
    entry = rtcfg.allowed_logins(env).get(h.lower(), rtcfg.Login())
    u, p = (user or entry.user), (port or entry.port)
    if user:
        why = rtcfg.user_problem(user)
        if why:
            return "", 0, f"--user is not usable: {why}"
    if port and not (isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535):
        return "", 0, "--port must be an integer 1..65535"
    return u, p, ""


def target_for(
    rt: rtcfg.RuntimeCfg,
    environ: Optional[dict] = None,
    host: Optional[str] = None,
    make: Optional[Callable] = None,
    user: Optional[str] = None,
    port: Optional[int] = None,
):
    """(transport, "") for a target the rules allow, else (None, why). Reads the configuration and the environment only: nothing is contacted
    (resolve() adds the reachability check; ExecTarget asks it when a job is due). The user and the port of the login come from `user` and
    `port` (--user, --port) or the user's entry in $DEAD_CODE_CHECKER_RUNTIME_HOSTS, never from the repository's file."""
    env = os.environ if environ is None else environ
    try:
        named = (host or "").strip()
        h = named or rt.host
        if not h:
            return None, "no host: name one in [graph.runtime] host or pass --host HOST"
        why = rtcfg.host_problem(h)
        if why:
            return None, f"the host is not usable: {why}"
        here = rttransport.work_machine(h)
        if here:  # first: no list and no command line makes this machine a target
            return None, f"{here}; code runs only on a separate machine, never on this one"
        if not named and h.lower() not in rtcfg.hosts_allowed(
            env
        ):  # the file of a repository never chooses where its code runs
            return None, (
                f"the host {h} is named by the repository's config file but you have not allowed it: add it to "
                f"${rtcfg.ENV_HOSTS} (comma-separated) or pass --host {h}"
            )
        u, p, why = _login(h, env, user, port)
        if why:
            return None, why
        if make is None and not safepath.which("ssh"):
            return None, "ssh is missing on this machine"
        return (make or make_transport)(rttransport.Target(h, u, p), env), ""
    except Exception as e:  # noqa: BLE001 - a broken configuration is a skip line, never a traceback
        return None, f"{type(e).__name__}: {str(e)[:120]}"


def resolve(rt: rtcfg.RuntimeCfg, environ=None, host=None, make=None, user=None, port=None):
    """(transport, "") for a usable and reachable target, else (None, why)."""
    t, why = target_for(rt, environ, host, make, user, port)
    if t is None:
        return None, why
    try:
        ok, why = t.reachable()
        return (t, "") if ok else (None, f"{t.label} unreachable: {why}")
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:120]}"


# ---------------------------------------------------------------- the project copy
class Package(NamedTuple):
    data: bytes  # what is sent: one tar of in.tar (the delivery of the run) and box/ (the tool's own half that runs on the target)
    sha256: str  # of in.tar, the one the target checks
    files: Dict[str, str]  # path -> blob id
    notes: Tuple[str, ...]
    langs: Dict[str, int]  # code language -> files
    tool: Dict[
        str, str
    ] = {}  # name -> sha256 of each file of the tool's own half (rttransport.TOOL_NAMES), checked on the target before it starts


def _link_shape(target: str) -> bool:
    """A target that can be sent: not empty, relative (no NUL, no leading slash or backslash)."""
    return bool(target) and "\0" not in target and not os.path.isabs(target) and not target.startswith(("/", "\\"))


def _inside_tree(path: str, target: str, links: Dict[str, str]) -> bool:
    """True when the symlink `path` -> `target` ends at a place inside the tree. The target is walked component by component from the directory
    of the link and a symlink met on the way is replaced by its own target at that place. `..` above the root, the root itself, a bad target
    on the way and a loop are refused."""
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
    """The symlinks of `links` (path -> target) that stay in the tree, judged against each other: a refused link is not there for the others,
    so the rest is judged again until nothing more is refused."""
    cur = {p: t for p, t in sorted(links.items()) if _link_shape(t)}
    while True:
        bad = [p for p, t in cur.items() if not _inside_tree(p, t, cur)]
        if not bad:
            return cur
        cur = {p: t for p, t in cur.items() if p not in bad}


def _tree(root: str, commit: str) -> Tuple[List[Tuple[str, str, int, bool]], Dict[str, str]]:
    """([(path, blob, size, executable)] of the regular files, {path: blob} of the links) of `git ls-tree -r` at `commit`; submodules are left out."""
    rc, out = vcs._run(root, "ls-tree", "-r", "-l", "-z", "--full-tree", commit)
    if rc != 0:
        raise PackError("git ls-tree failed: not a repository with that commit")
    files, links = [], {}
    for rec in out.split(b"\0"):
        meta, tab, path = rec.partition(b"\t")
        if not tab:
            continue
        mode, typ, blob, size = meta.decode().split()
        p = path.decode("utf-8", "surrogateescape")
        if typ != "blob" or not rtbox.safe_rel(p) or not size.isdigit():
            continue
        if mode in ("100644", "100755"):
            files.append((p, blob, int(size), mode == "100755"))
        elif mode == "120000":
            links[p] = blob
    return files, links


def detect_langs(paths_: Sequence[str]) -> Dict[str, int]:
    """code language -> number of tracked files (data/langs.json by name; no content is read)."""
    from . import langs as dl

    table, out = dl.load(), {}
    for p in paths_:
        lang = table.detect(p)
        if lang and (table.info(lang) or {}).get("role") == "code":
            out[lang] = out.get(lang, 0) + 1
    return dict(sorted(out.items()))


def plan_runs(rt: rtcfg.RuntimeCfg, present: Dict[str, int]) -> Tuple[List[dict], List[str]]:
    """([run of the spec], [why a language is not traced]) for the languages in the tree. With a configured command ONE run covers every
    language that has a tracer; without it each such language runs its default command (data/runtime.json), when it has one."""
    d = rtcfg.data()["tracers"]
    have = [lang for lang in d if lang in present]
    notes, runs = [], []
    if rt.command:
        if have:
            runs.append({"label": "configured command", "langs": have, "argv": list(rt.command)})
    else:
        for lang in have:
            if d[lang]["default_command"]:
                runs.append({"label": f"default {lang}", "langs": [lang], "argv": list(d[lang]["default_command"])})
            else:
                notes.append(f"no command for {lang}: set command in [graph.runtime]")
    other = sorted(lang for lang in present if lang not in d)
    if other:
        notes.append("no tracer for " + ", ".join(other))
    return runs, notes


def _scan_batch(batch):
    """One unit of the content scan: [(path, bytes)] -> ["path:rule"]."""
    return [f"{p}:{r}" for p, data in batch for r in sorted(rttransport.scan_content(data))]


def _batches(items: List[Tuple[str, bytes]], budget: int = 8 << 20) -> List[list]:
    out, cur, size = [], [], 0
    for p, data in items:
        cur.append((p, data))
        size += len(data)
        if size >= budget:
            out.append(cur)
            cur, size = [], 0
    return out + ([cur] if cur else [])


def tool_files() -> Dict[str, bytes]:
    """The tool's OWN half that runs on the target (graph.rtbox.delivery_files: the box, its data and the tracer), as it is on this machine:
    what the target runs is never read from anything else."""
    out = {}
    for name, path in sorted(rtbox.delivery_files().items()):
        try:
            with open(path, "rb") as fh:
                out[name] = fh.read()
        except OSError as e:
            raise PackError(f"the tool's own {name} cannot be read ({e.strerror})") from e
    return out


def pack(
    root: str,
    commit: str,
    runs: Sequence[dict],
    timeout: int,
    target: str,
    run_id: str,
    edits: Optional[Dict[str, str]] = None,
) -> Package:
    """The delivery: a tar of `spec.json` (first) and `tree/<path>` of every tracked file at `commit`, taken from git's object store (the blob bytes, never the work tree, so an edit or a secret that was not committed cannot
    go). PackError (the one line of the step) for a commit that does not exist, a copy over the caps, a secret-like name or content.
    `edits` ({path: the whole text}: the patched files of a check run) replace those tracked files in the copy, with their own blob ids; a path
    that is not in the copy is a PackError. They pass the same secret checks. What is sent is that tar as `in.tar` next to the tool's own
    half (rtbox.py, its data, the tracer: `box/...`), whose sha256 go along (Package.tool)."""
    d = rtcfg.data()["copy"]
    files, links = _tree(root, commit)
    cap, big = d["max_file_mb"] << 20, [f for f in files if f[2] > d["max_file_mb"] << 20]
    keep = [f for f in files if f[2] <= cap]
    if not keep or len(keep) > d["max_files"] or sum(f[2] for f in keep) > d["max_tree_mb"] << 20:
        raise PackError(f"the project copy is empty or over the caps ({d['max_files']} files, {d['max_tree_mb']} MB)")
    names = rttransport.secret_name_hits([f[0] for f in keep])
    if names:
        raise PackError(
            f"{len(names)} tracked file name(s) look like secrets ({', '.join(names[:3])}); nothing was sent"
        )
    blobs = vcs.cat_batch(root, {f[1] for f in keep} | set(links.values()), max_bytes=cap)
    if any(blobs.get(f[1]) is None for f in keep):
        raise PackError("a blob of the commit could not be read; nothing was sent")
    if edits:
        have = {f[0]: f for f in keep}
        miss = sorted(p for p in edits if p not in have)
        if miss:
            raise PackError(f"an edited file is not in the project copy ({', '.join(miss[:3])})")
        for p, text in sorted(edits.items()):
            raw = text.encode("utf-8")
            nb = vcs.blob_sha(raw, "sha256" if len(have[p][1]) == 64 else "sha1")
            blobs[nb] = raw
            keep[keep.index(have[p])] = (p, nb, len(raw), have[p][3])
    hits = [
        h
        for part in par.run_units(_batches([(f[0], blobs[f[1]]) for f in keep]), _scan_batch, kind="process")
        for h in part
    ]
    if hits:
        raise PackError(
            f"the secret checks stop the copy ({len(hits)}: {', '.join(sorted(hits)[:3])}); nothing was sent"
        )
    lk = {p: blobs[b].decode("utf-8", "surrogateescape") for p, b in links.items() if blobs.get(b) is not None}
    kept_links = confine_links(lk)
    tool = tool_files()
    spec = {
        "schema": rtbox.SCHEMA,
        "run_id": run_id,
        "commit": commit,
        "target": target,
        "runs": [dict(r) for r in runs],
        "timeout": timeout,
        "files": {f[0]: f[1] for f in keep},
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        raw = json.dumps(spec, sort_keys=True).encode()
        tf.addfile(_info("spec.json", len(raw), 0o644), io.BytesIO(raw))
        for p, b, _size, x in sorted(keep):
            tf.addfile(_info("tree/" + p, len(blobs[b]), 0o755 if x else 0o644), io.BytesIO(blobs[b]))
        for p, target_ in sorted(kept_links.items()):
            ti = _info("tree/" + p, 0, 0o777)
            ti.type, ti.linkname = tarfile.SYMTYPE, target_
            tf.addfile(ti)
    inner = buf.getvalue()
    wire = io.BytesIO()
    with tarfile.open(fileobj=wire, mode="w:") as tf:
        tf.addfile(_info("in.tar", len(inner), 0o600), io.BytesIO(inner))
        for n, b in sorted(tool.items()):
            tf.addfile(_info("box/" + n, len(b), 0o644), io.BytesIO(b))
    notes = []
    if big:
        notes.append(f"{len(big)} file(s) over {d['max_file_mb']} MB are not in the copy")
    if len(kept_links) != len(links):
        notes.append(f"{len(links) - len(kept_links)} symbolic link(s) leave the tree and are not in the copy")
    return Package(
        wire.getvalue(),
        hashlib.sha256(inner).hexdigest(),
        spec["files"],
        tuple(notes),
        detect_langs([f[0] for f in keep]),
        {n: hashlib.sha256(b).hexdigest() for n, b in sorted(tool.items())},
    )


def _info(name: str, size: int, mode: int) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = size, mode, 0, 0, 0, "", ""
    return ti


# ---------------------------------------------------------------- what comes back
DOC_KEYS = ("schema", "run_id", "commit", "target", "status", "notes", "runs", "files", "executed")
ROW_KEYS = ("lang", "file", "qual", "line", "end", "hit")
RUN_OPT = ("label", "langs", "argv0", "rc", "timed_out", "seconds", "tracers", "why", "error")


def _txt(v, n: int) -> str:
    """Text of the target (or of a label) as one short line; what looks like a secret is masked (rtcfg.mask_secrets) BEFORE the cut."""
    return rtcfg.mask_secrets(" ".join(str(v).split()))[:n]


def _clean_run(r: dict, chars: int) -> dict:
    """One run record of the target as plain, cut text and numbers: its words are shown to the user, never trusted."""
    num = lambda v: v if isinstance(v, int) and not isinstance(v, bool) else None  # noqa: E731
    out = {k: _txt(r[k], 120) for k in ("label", "argv0", "why") if k in r}
    if "error" in r:  # the first error line of a check run (graph.fixverify judges by it)
        out["error"] = _txt(r["error"], chars)
    out["rc"], out["timed_out"] = num(r.get("rc")), r.get("timed_out") is True
    out["seconds"] = (
        float(r["seconds"])
        if isinstance(r.get("seconds"), (int, float)) and not isinstance(r["seconds"], bool)
        else 0.0
    )
    out["langs"] = [_txt(x, 20) for x in (r.get("langs") if isinstance(r.get("langs"), list) else [])[:8]]
    tr = r.get("tracers") if isinstance(r.get("tracers"), dict) else {}
    out["tracers"] = {
        _txt(k, 20): {
            "tracer": _txt(v.get("tracer", ""), 40),
            "status": _txt(v.get("status", ""), 20),
            "symbols": num(v.get("symbols")) or 0,
            "note": _txt(v.get("note", ""), chars),
        }
        for k, v in list(tr.items())[:8]
        if isinstance(v, dict)
    }
    return out


def check_result(doc, sent: Dict[str, str], run_id: str, commit: str) -> str:
    """ "" when `doc` is a runtime.json of THIS run in the strict shape (and cleans it in place), else why not. Every path and blob id has to be
    one that was sent; every row has the exact keys and integer fields."""
    d = rtcfg.data()
    if not isinstance(doc, dict) or set(doc) != set(DOC_KEYS):
        return "runtime.json is not an object with exactly the expected keys"
    if doc["schema"] != rtbox.SCHEMA or doc["run_id"] != run_id or doc["commit"] != commit:
        return "runtime.json is of another schema, run or commit"
    if doc["status"] not in ("ok", "no-sandbox") or not isinstance(doc["target"], str):
        return "runtime.json has a status or target that is not known"
    if not (
        isinstance(doc["notes"], list) and len(doc["notes"]) <= 20 and all(isinstance(x, str) for x in doc["notes"])
    ):
        return "runtime.json: notes must be a short list of text"
    doc["notes"] = [_txt(x, d["result"]["note_chars"]) for x in doc["notes"]]
    if not (
        isinstance(doc["runs"], list)
        and len(doc["runs"]) <= rtbox.MAX_RUNS
        and all(isinstance(r, dict) and set(r) <= set(RUN_OPT) for r in doc["runs"])
    ):
        return "runtime.json: runs are not what the spec asked for"
    doc["runs"] = [_clean_run(r, d["result"]["note_chars"]) for r in doc["runs"]]
    if not (isinstance(doc["files"], dict) and all(sent.get(p) == b for p, b in doc["files"].items())):
        return "runtime.json names a file or a blob id that was not sent"
    rows = doc["executed"]
    if not isinstance(rows, list) or len(rows) > d["result"]["max_symbols"]:
        return "runtime.json: executed is not a list within the symbol cap"
    for r in rows:
        if not (
            isinstance(r, dict)
            and set(r) == set(ROW_KEYS)
            and r["lang"] in d["tracers"]
            and r["file"] in doc["files"]
            and isinstance(r["qual"], str)
        ):
            return "runtime.json: an executed row is not in the exact shape or names a file it has no blob id for"
        if (
            not all(isinstance(r[k], int) and not isinstance(r[k], bool) for k in ("line", "end", "hit"))
            or r["line"] < 1
            or r["end"] < 0
            or r["hit"] < 1
        ):
            return "runtime.json: an executed row has a line or count that is not a valid number"
    return ""


def parse_delivery(tar_bytes: bytes, sent: Dict[str, str], run_id: str, commit: str) -> Tuple[Optional[dict], str]:
    """(doc, "") for a delivery that passes every check, else (None, why): exactly manifest.json and runtime.json as plain files, the size
    caps, the manifest of this run with the sha256 and size of runtime.json, then check_result()."""
    cap = rtcfg.data()["result"]["max_bytes"]
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
            ms = tf.getmembers()
            if sorted(m.name for m in ms) != sorted(rttransport.DELIVERY) or not all(
                m.isreg() and m.size <= cap for m in ms
            ):
                return (
                    None,
                    "the delivery is not exactly manifest.json and runtime.json as plain files within the size cap",
                )
            raw = {m.name: tf.extractfile(m).read() for m in ms}
        man = json.loads(raw["manifest.json"].decode("utf-8"))
        doc = json.loads(raw["runtime.json"].decode("utf-8"))
    except (tarfile.TarError, ValueError, OSError):
        return None, "the delivery cannot be read"
    keys = {"schema", "run_id", "commit", "runtime_sha256", "runtime_bytes"}
    if not (
        isinstance(man, dict)
        and set(man) == keys
        and man["schema"] == rtbox.SCHEMA
        and man["run_id"] == run_id
        and man["commit"] == commit
    ):
        return None, "manifest.json is not the one of this run"
    if man["runtime_sha256"] != hashlib.sha256(raw["runtime.json"]).hexdigest() or man["runtime_bytes"] != len(
        raw["runtime.json"]
    ):
        return None, "runtime.json does not match the manifest (sha256 or size)"
    why = check_result(doc, sent, run_id, commit)
    return (None, why) if why else (doc, "")


# ---------------------------------------------------------------- stored runs
class Stored(NamedTuple):
    name: str
    dir: str
    ts: float
    doc: dict


def runs_dir(root: str, environ: Optional[dict] = None, layout: Optional[paths.Layout] = None) -> str:
    """<project data dir>/runtime (graph.paths: the same project directory as the store and the runs). paths.DataDirError when there is none."""
    lay = layout or paths.resolve(paths.find_root(root), environ=environ)
    return os.path.join(str(lay.project), "runtime")


def _stamp_ts(stamp: str) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(stamp, "%Y%m%dT%H%M%SZ")))
    except ValueError:
        return None


def _run_name(name: str) -> Optional[float]:
    """The time (UTC seconds) of a run directory name `<utc>-<head8>[-<n>]`, None for any other name."""
    stamp, _, rest = name.partition("-")
    head, _, n = rest.partition("-")
    if not (head and (head == "nohead" or all(c in "0123456789abcdef" for c in head) and len(head) <= 8)) or (
        n and not n.isdigit()
    ):
        return None
    return _stamp_ts(stamp) if len(stamp) == 16 else None


def list_runs(
    root: str, environ: Optional[dict] = None, layout: Optional[paths.Layout] = None
) -> List[Tuple[str, str, float]]:
    """[(name, path, utc seconds)] of the stored runs, oldest first; a name that is no run name and a link are not runs. [] when there is
    no data directory."""
    try:
        base = runs_dir(root, environ, layout)
        names = os.listdir(base)
    except (OSError, paths.DataDirError):
        return []
    out = []
    for n in names:
        ts, p = _run_name(n), os.path.join(base, n)
        if ts is not None and os.path.isdir(p) and not os.path.islink(p):
            out.append((n, p, ts))
    return sorted(out, key=lambda t: (t[2], t[0]))


def _private_dir(d: str) -> None:
    os.makedirs(d, mode=0o700, exist_ok=True)
    if os.path.islink(d) or not os.path.isdir(d):
        raise OSError(f"{d} is not a plain directory")


def store_run(
    root: str,
    doc: dict,
    target: str,
    head: str,
    now: Optional[float] = None,
    environ: Optional[dict] = None,
    layout: Optional[paths.Layout] = None,
) -> str:
    """Write the (validated) result as a stored run and drop the oldest beyond keep_runs; the directory, or "" when it cannot be written."""
    now = time.time() if now is None else now
    stem = "%s-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)), (head or "nohead")[:8])
    try:
        base = runs_dir(root, environ, layout)
        _private_dir(base)
        for i in range(100):
            path = os.path.join(base, stem if i == 0 else f"{stem}-{i}")
            try:
                os.mkdir(path, 0o700)
                break
            except FileExistsError:
                continue
        else:
            return ""
        body = dict(doc, stored={"target": target, "utc": stem[:16]})
        _put(os.path.join(path, "runtime.json"), json.dumps(body, sort_keys=True, indent=1))
        _put(os.path.join(path, "report.md"), report_md(body))
        for _n, p, _ts in list_runs(root, environ, layout)[: -rtcfg.data()["keep_runs"]]:
            shutil.rmtree(p, ignore_errors=True)
        return path
    except (OSError, paths.DataDirError):
        return ""


def _put(path: str, text: str) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_latest(root: str, environ: Optional[dict] = None, layout: Optional[paths.Layout] = None) -> Optional[Stored]:
    """The newest stored run that reads back as a result of the strict shape (the blob ids are checked against the run's own file list)."""
    for name, path, ts in reversed(list_runs(root, environ, layout)):
        try:
            if os.path.getsize(os.path.join(path, "runtime.json")) > rtcfg.data()["result"]["max_bytes"] * 2:
                continue
            with open(os.path.join(path, "runtime.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("commit"), str) and isinstance(doc.get("run_id"), str):
            body = {k: doc.get(k) for k in DOC_KEYS}
            if not check_result(body, dict(body.get("files") or {}), doc["run_id"], doc["commit"]):
                return Stored(name, path, ts, dict(body, stored=doc.get("stored") or {}))
    return None


def report_md(doc: dict) -> str:
    """The text of report.md of a stored run: the counts and the tracers, never a symbol list (runtime.json has it)."""
    rows = doc.get("executed") or []
    out = [
        "# Runtime run",
        "",
        f"- target: {doc.get('target')}",
        f"- commit: {str(doc.get('commit'))[:12]}",
        f"- status: {doc.get('status')}",
        f"- symbols that ran: {len(rows)} in {len(doc.get('files') or {})} file(s)",
    ]
    for r in doc.get("runs") or []:
        tr = ", ".join(
            f"{k}: {v.get('status')} ({v.get('symbols', 0)})" for k, v in sorted((r.get("tracers") or {}).items())
        )
        out.append(
            f"- run `{r.get('label')}` ({r.get('argv0')}): rc {r.get('rc')}, {r.get('seconds')} s{', timed out' if r.get('timed_out') else ''}; {tr}"
        )
    out += [f"- note: {n}" for n in doc.get("notes") or []]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- the effect on the verdicts
def alive(nodes, doc: dict, blob_of: Callable[[str], Optional[str]], label: str = "") -> Tuple[Dict[str, str], int]:
    """({node key: note}, files left out). A node of KINDS is alive when an executed function of its file starts on a line inside its span
    [line0, line1] (a lambda, a nested function or a method marks the symbols that contain it: they ran too), and only for a file whose
    blob id is the one of the run (`blob_of(path)`): a file that changed since is left out and counted, its lines moved."""
    by: Dict[str, List[int]] = {}
    for r in doc.get("executed") or []:
        by.setdefault(r["file"], []).append(r["line"])
    fresh = {p for p, b in (doc.get("files") or {}).items() if blob_of(p) == b}
    for ls in by.values():
        ls.sort()
    out: Dict[str, str] = {}
    note = f"run {label}" if label else "a stored runtime run"
    for n in nodes:
        ls = by.get(n.path) if n.kind in KINDS and n.path in fresh else None
        if ls:
            i = bisect.bisect_left(ls, n.line0)
            if i < len(ls) and ls[i] <= max(n.line1, n.line0):
                out[n.key] = note
    return out, len(doc.get("files") or {}) - len(fresh)


def ran_map(nodes, doc: dict, blob_of: Callable[[str], Optional[str]]) -> Dict[str, bool]:
    """{node key: True | False} for the symbols of the graph a stored run says something about, the `runtime` input of graph.confidence.annotate:
    True = the symbol ran (see alive); False = its file is one of the run (a symbol of it ran) and unchanged since, and this symbol did not.
    A symbol of a file the run never touched, or of a changed one, is not in the map: no result, no change."""
    ran, _stale = alive(nodes, doc, blob_of)
    fresh = {p for p, b in (doc.get("files") or {}).items() if blob_of(p) == b}
    return {n.key: n.key in ran for n in nodes if n.kind in KINDS and (n.key in ran or n.path in fresh)}


def _qual(key: str) -> str:
    return split_key(key)[2]


def alive_finding(n, why, alive_map: Dict[str, str]) -> Optional[Finding]:
    """The I finding that stands for a dead claim about `n` when `n` ran at runtime, or when it is in a dead cluster (why.cluster) a member of
    which ran: such a cluster has an entry the graph does not see, so no member is reported dead. None when nothing ran."""
    if n.key in alive_map:
        msg = f"`{_qual(n.key)}` looked dead in the graph but ran at runtime ({alive_map[n.key]}); it is not reported dead"
    else:
        members = tuple(getattr(why, "cluster", ()) or ())
        hit = next((k for k in members if k in alive_map), None)
        if hit is None:
            return None
        msg = (
            f"`{_qual(n.key)}` is in a dead cluster of {len(members)} symbols one of which, `{_qual(hit)}`, ran at runtime "
            f"({alive_map[hit]}); the cluster is not reported dead"
        )
    return Finding("I", n.path, n.line0, ALIVE, msg)


def replace_claims(found: Sequence[Finding], graph, res, alive_map: Dict[str, str]) -> List[Finding]:
    """`found` with every dead claim (a dead symbol, a dead cluster, an orphan of the branch, a suspect that stands for one) about a symbol that
    ran, or about a member of a dead cluster one of which ran, replaced by its I dc.alive-runtime. Nothing else changes; no map, no change."""
    if not alive_map:
        return list(found)
    at: Dict[Tuple[str, int], object] = {}
    for n in graph.nodes:
        if n.kind in KINDS:
            at.setdefault((n.path, n.line0), n)
    out = []
    for f in found:
        n = at.get((f.path, f.line)) if verdict.claim(f) in DEAD_CLAIMS else None
        rep = alive_finding(n, res.why_dead(n.key), alive_map) if n is not None else None
        out.append(rep if rep is not None else f)
    return out


class View(NamedTuple):
    """What the newest stored run says about the graph of a check: `alive` for replace_claims, `ran` for graph.confidence, the notes."""

    alive: Dict[str, str] = {}
    ran: Dict[str, bool] = {}
    notes: Tuple[str, ...] = ()
    findings: Tuple[Finding, ...] = ()
    run: str = ""


def view(
    root: str,
    built,
    rt: rtcfg.RuntimeCfg,
    environ: Optional[dict] = None,
    layout: Optional[paths.Layout] = None,
    now: Optional[float] = None,
) -> View:
    """What the newest stored runtime run of the project says about `built` (a levels.Built). Reads files only: the target is never contacted
    and no run is started. No stored run, no change (View()). A run older than refresh_days is still used and ONE I dc.runtime-stale says so;
    a file changed since the run is left out and counted."""
    st = load_latest(root, environ, layout)
    if st is None:
        return View()
    files = built.tree.files
    blob_of = lambda p: files[p].blob if p in files else None  # noqa: E731
    amap, left = alive(built.graph.nodes, st.doc, blob_of, st.name)
    note = "runtime: %d symbol(s) of the graph ran in %s on %s%s" % (
        len(amap),
        st.name,
        (st.doc.get("stored") or {}).get("target") or st.doc["target"],
        f"; {left} file(s) changed since, their hits are not used" if left else "",
    )
    fs: List[Finding] = []
    age = ((time.time() if now is None else now) - st.ts) / 86400
    if age > rt.refresh():
        fs.append(
            Finding(
                "I",
                safety.WHERE,
                0,
                STALE,
                f"the stored runtime run {st.name} is {int(age)} days old (refresh_days = {rt.refresh()}); it is still used. "
                "`dead-code-checker graph runtime` runs it again",
            )
        )
    return View(amap, ran_map(built.graph.nodes, st.doc, blob_of), (note,), tuple(fs), st.name)


# ---------------------------------------------------------------- the round trip
class Outcome(NamedTuple):
    status: str  # ok | skipped | failed | off
    line: str  # the one line to print (the step's only output unless it ran)
    level: str  # I | W
    dir: str = ""  # the stored run
    doc: Optional[dict] = None
    notes: Tuple[str, ...] = ()

    def finding(self) -> Optional[Finding]:
        if self.status in ("ok", "off"):
            return None
        return Finding(self.level, safety.WHERE, 0, SKIPPED if self.status == "skipped" else FAILED, self.line)


def _skip(why: str) -> Outcome:
    return Outcome("skipped", rtcfg.skip_line(rtcfg.mask_secrets(why)), "I")


def _fail(line: str) -> Outcome:
    """The one line of a failed step. What the target or ssh said is masked (rtcfg.mask_secrets) before it is cut to length."""
    return Outcome("failed", "runtime mode: " + rtcfg.mask_secrets(" ".join(line.split()))[:300], "W")


def run(
    root: str,
    rt: rtcfg.RuntimeCfg,
    host: Optional[str] = None,
    environ: Optional[dict] = None,
    now: Optional[float] = None,
    make: Optional[Callable] = None,
    run_id: Optional[str] = None,
    layout: Optional[paths.Layout] = None,
    user: Optional[str] = None,
    port: Optional[int] = None,
) -> Outcome:
    """One runtime run of the repository at `root` (its HEAD) on the target; see the module docstring. Never raises. Nothing is run on this
    machine: the only processes started here are git (reading) and ssh."""
    env = os.environ if environ is None else environ
    try:
        if not (rt.enabled or host):
            return Outcome(
                "off", "runtime mode: off ([graph.runtime] enabled = true opts this project in; or pass --host)", "I"
            )
        tr, why = resolve(rt, env, host, make, user, port)
        if tr is None:
            return _skip(why)
        commit = vcs.head_commit(root)
        if not commit:
            return _skip("the repository has no commit to copy")
        try:
            present = detect_langs([f[0] for f in _tree(root, commit)[0]])
        except PackError as e:
            return _skip(str(e))
        runs, notes = plan_runs(rt, present)
        if not runs:
            why = "; ".join(notes) or "no language of the tree has a tracer"
            return Outcome("skipped", rtcfg.skip_line(why), "I", notes=tuple(notes))
        rid = run_id or uuid.uuid4().hex
        try:
            pkg = pack(root, commit, runs, rt.wall_s(), tr.label, rid)
        except PackError as e:
            return _fail(f"not run: {e}")
        return _transfer(root, rt, tr, rid, commit, pkg, notes, now, env, layout)
    except Exception as e:  # noqa: BLE001 - the step never takes anything else down
        return _fail(f"internal error: {type(e).__name__}: {str(e)[:160]}")


def exchange(tr, rid: str, pkg: Package, commit: str, wait_s: float) -> Tuple[Optional[dict], str, str]:
    """put, run, pull, cleanup (after every started run), then the checks of parse_delivery: (doc, "", "") or (None, "skip" | "fail", the one
    line). "skip" = no transport (nothing ran, the target is not there), "fail" = the target was reached and the run or its delivery went
    wrong. The one round trip of the runtime mode and of the check runs of graph.fixverify."""
    try:
        rc, last = tr.put(rid, pkg.data)
        last = rtcfg.mask_secrets(str(last))  # a line of the target: masked before it reaches any message
        if rc != 0:
            if rc == 255:
                return None, "skip", f"{tr.label} did not take the project copy ({last})"
            return None, "fail", f"the project copy could not be put on {tr.label} (rc {rc}: {last})"
        rc, last = tr.run(rid, pkg.sha256, wait_s, pkg.tool)
        last = rtcfg.mask_secrets(str(last))
        if rc == 255:
            return None, "skip", f"no transport to {tr.label} ({last})"
        if rc != 0:
            return None, "fail", f"the box on {tr.label} ended with rc {rc}: {last}"
        data, why = tr.pull(rid)
        if data is None:
            return None, "fail", f"the result could not be pulled from {tr.label}: {rtcfg.mask_secrets(str(why))}"
        doc, why = parse_delivery(data, pkg.files, rid, commit)
        if doc is None:
            return None, "fail", f"the result of {tr.label} was rejected: {why}"
        return doc, "", ""
    finally:
        tr.cleanup(rid)


def _transfer(root, rt, tr, rid, commit, pkg: Package, notes, now, env, layout) -> Outcome:
    """The exchange, then the stored run."""
    doc, kind, line = exchange(tr, rid, pkg, commit, rt.wall_s() + rtcfg.data()["timeouts"]["setup_s"])
    if doc is None:
        return _skip(line) if kind == "skip" else _fail(line)
    if doc["status"] == "no-sandbox":
        return _skip(f"{tr.label} has no sandbox: {(doc['notes'] or ['no reason given'])[0]}")
    tracers = [(lang, v) for r in doc["runs"] for lang, v in sorted(r["tracers"].items())]
    allnotes = tuple([*pkg.notes, *notes, *doc["notes"], *(f"{lang}: {v['note']}" for lang, v in tracers if v["note"])])
    rcs = [r["rc"] for r in doc["runs"]]
    if not doc["executed"] and rcs and all(x != 0 for x in rcs):
        why = ", ".join(str(x) for x in rcs)
        return _fail(f"the command failed on {tr.label} (rc {why}) and no symbol was recorded")
    path = store_run(root, doc, tr.label, commit, now, env, layout)
    seen = "; ".join(f"{lang}: {v['status']} {v['symbols']}" for lang, v in tracers) or "no tracer data"
    line = "runtime mode: %d symbol(s) in %d file(s) ran on %s (%s); %s%s" % (
        len(doc["executed"]),
        len(doc["files"]),
        tr.label,
        seen,
        f"stored {path}" if path else "the run could not be stored",
        ("; " + "; ".join(allnotes)) if allnotes else "",
    )
    return Outcome("ok", line, "I", path, doc, allnotes)


# ---------------------------------------------------------------- the execution target of graph.fixverify
class ExecTarget:
    """THE execution target as graph.fixverify sees it (its Target interface: `name`, `unavailable()`, `run(job)`), over the transport of the
    runtime mode: the allowed host, the known host key, the one fixed command and the bubblewrap box of graph.rtbox. An executing check of a
    verified fix (tests, build scripts, mvn, gradle, npm) is a CHECK RUN of the same delivery: the tracked files at HEAD with the patched texts
    over them (pack(edits=)), the command once per package directory of the job, and back come the exit code and the first error line of the
    check. Nothing is ever run on this machine; a job that cannot reach the target is a result with ran=False and the reason, never an exception."""

    def __init__(self, tr, environ: Optional[dict] = None):
        self.tr, self.env = tr, os.environ if environ is None else environ
        self.name = getattr(tr, "name", None) or getattr(tr, "label", "target")

    def unavailable(self) -> str:
        """ "" when a job can go now, else one line: why not (the reachability check of the target)."""
        try:
            ok, why = self.tr.reachable()
        except Exception as e:  # noqa: BLE001
            return f"{self.name}: {type(e).__name__}: {str(e)[:120]}"
        return "" if ok else f"{self.name} unreachable: {why}"

    def run(self, job):
        from .fixverify import RuntimeResult

        try:
            return self._job(job)
        except Exception as e:  # noqa: BLE001 - a failed check run is a message, never a traceback
            return RuntimeResult(False, None, f"internal error: {type(e).__name__}: {str(e)[:160]}")

    def _job(self, job):
        from .fixverify import RuntimeResult

        no = lambda why: RuntimeResult(False, None, why)  # noqa: E731
        tr, d = self.tr, rtcfg.data()
        commit = vcs.head_commit(job.root)
        if not commit:
            return no("the repository has no commit to copy")
        why = rtcfg.argv_problem(list(job.argv))
        if why or not job.dirs or len(job.dirs) > rtbox.MAX_RUNS:
            return no(why or f"a check runs in 1..{rtbox.MAX_RUNS} package directories, not {len(job.dirs)}")
        label = _txt(job.check, 80) or "check"
        langs = [job.lang] if job.lang in d["tracers"] else []
        runs = [{"label": label, "langs": langs, "argv": list(job.argv), "cwd": x} for x in job.dirs]
        wall = max(1, min(int(job.timeout_s), d["limits"]["wall_s"]))
        rid = uuid.uuid4().hex
        try:
            pkg = pack(job.root, commit, runs, wall, tr.label, rid, edits=dict(job.edits))
        except PackError as e:
            return no(f"not run: {e}")
        doc, _kind, line = exchange(tr, rid, pkg, commit, wall + d["timeouts"]["setup_s"])
        if doc is None:
            return no(line)
        if doc["status"] == "no-sandbox":
            return no(f"{tr.label} has no sandbox: {(doc['notes'] or ['no reason given'])[0]}")
        rs = doc["runs"]
        if len(rs) != len(runs):
            return no(f"{tr.label} answered for {len(rs)} of {len(runs)} commands")
        for r in rs:
            if r["timed_out"]:
                return no(f"{label} did not finish in {wall} s on {tr.label}")
            if r["rc"] is None:
                return no(r.get("why") or f"{label} did not run on {tr.label}")
        bad = next((r for r in rs if r["rc"] != 0), None)
        return RuntimeResult(True, 0, "") if bad is None else RuntimeResult(True, bad["rc"], bad.get("error", ""))


def exec_target(
    root: str,
    cfg: Optional[rtcfg.RuntimeCfg] = None,
    environ: Optional[dict] = None,
    host: Optional[str] = None,
    make: Optional[Callable] = None,
    user: Optional[str] = None,
    port: Optional[int] = None,
):
    """(ExecTarget, "") when the executing checks of a fix may go to a target, else (None, why). `cfg` is the [graph.runtime] table (loaded from
    `root` when not given); the mode has to be enabled there, or the host given by the caller (`--host`). The same rules as the runtime command
    (target_for): the allow-list, this machine refused. Nothing is contacted here."""
    env = os.environ if environ is None else environ
    try:
        if cfg is None:
            from . import config

            cfg = config.load(root).runtime
        if not (cfg.enabled or host):
            return None, "runtime mode is off ([graph.runtime] enabled = true opts this project in)"
        tr, why = target_for(cfg, env, host, make, user, port)
        return (ExecTarget(tr, env), "") if tr is not None else (None, why)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:120]}"


# ---------------------------------------------------------------- the command line
def run_cli(a, root: Optional[str], layout: Optional[paths.Layout] = None) -> int:
    """`dead-code-checker graph runtime`: 0 for a run, a skip and a failed run alike (it never blocks), 2 for a usage error or a project
    that has not opted in. --classes prints data/runtime.json "classes" and needs no target."""
    import sys

    if getattr(a, "classes", False):
        if getattr(a, "json", False):
            print(json.dumps(rtcfg.classes(), indent=1, sort_keys=True))
            return 0
        for lang, c in sorted(rtcfg.classes().items()):
            print(f"{lang}: executing (target only): {c['executing']}; reading (may run here): {c['reading']}")
        return 0
    if root is None:
        print("dead-code-checker graph runtime: not inside a git repository (use --root)", file=sys.stderr)
        return 2
    from . import config

    cfg = config.load(root)
    out = run(
        root, cfg.runtime, host=getattr(a, "host", None), layout=layout, user=getattr(a, "user", None), port=getattr(a, "port", None)
    )
    if getattr(a, "json", False):
        doc = {
            "status": out.status,
            "line": out.line,
            "level": out.level,
            "dir": out.dir,
            "symbols": len((out.doc or {}).get("executed") or []),
            "notes": list(out.notes),
        }
        print(json.dumps(doc, indent=1, sort_keys=True))
    else:
        print(out.line)
    return 2 if out.status == "off" else 0
