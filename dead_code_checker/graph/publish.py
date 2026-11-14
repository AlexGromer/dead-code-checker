"""graph.publish: `dead-code-checker graph publish`, the architecture of a project as files that may be committed and shown.

    graph publish [--root R] [--out DIR] [--gh-pages] [--dry-run] [--deny-file F]       run(a, layout, steps) -> exit code
    render(graph, res, tree, suspects, name, out_rel) -> {path: text}           the files, nothing written
    scan(files, deny=()) -> [reason]                                             [] = clean; a reason names a file, never its text
    input_digest(graph, tree, out_rel) -> hex                                    what the published files were made from
    stale(root, sink) -> [Finding]                                               the branch-level W dc.publish-stale (cheap, no render)

The files go to --out (default data/publish.json "out" = docs/architecture; inside the repository) and hold names, counts and
relations only, never source code, a snippet or an evidence text:
  modules.svg    the module overview (viz.modules_diagram, the built-in emitter: the same tree gives the same bytes whether Graphviz is
                 installed or not; a box links nowhere outside the picture)
  modules.drawio draw.io XML, uncompressed, made from the SAME layout (viz.module_grid): one mxCell per module with its name and counts,
                 one frame per top-level directory, one edge per module pair with the call count; diagrams.net opens it as it is
  architecture.svg   the architecture lens of the viewer (graph.lens.arch_svg): the modules in layers by dependency, cycles outlined, edges
                 between languages dashed; module names and node counts only (no file or symbol names, no code); absent when the graph has no module
  calls/NNN-name.svg   what an entry point reaches: the hard and soft production roots (first data/publish.json calls.max of them, the
                 rest is counted), `depth` levels forward, at most `nodes` nodes; names and verdict words only
  index.md       overview: counts, languages, the two tables (modules, module edges) and the links to the pictures
  .dc-digest     JSON: the digest of the inputs (input_digest) and the sha256 of every file above, for the stale check; files named in an
                 earlier .dc-digest that are not written any more are removed, nothing else in the directory is touched
A write or a removal never follows a symlink out of the directory: --out itself must not be a symlink and the real path of every file
written or removed (and of its temporary file) must stay under the real path of --out, else exit 2 with the file named and NOTHING
written or removed (check_out; a symlink that no path goes through is left alone).
Output is deterministic (no clock, sorted everywhere): the same tree gives the same bytes.

Scan before writing (a finding is exit 1 and NOTHING is written, not even the other files): everything is rendered in memory, then
checked by (1) gitleaks (graph.snippets.scan: default rules, no repository config, neutral file names), (2) the built-in marker scan
(`MARKERS` of data/publish.json: private-key blocks, well-known token shapes, `password = value` assignments, e-mail addresses, IPv4
addresses, absolute local paths of a user's machine, and every line of the --deny-file: one word or regular expression per line, case
insensitive, for the names a project must never publish), (3) nothing else: no gitleaks is a finding too, nothing is published
unscanned. The findings are printed as `file[:lines]: reason`, never the matched text.

--gh-pages adds the interactive page (viz.make_page: no code fragments, no evidence texts, no findings; the page meta says code off)
to the LOCAL branch gh-pages: a temporary git worktree, one commit with index.html (signed when the repository signs, hooks run, never
--no-verify), the worktree removed. A page whose bytes are already on the branch makes no commit. NOTHING is pushed and no remote is
touched; the command prints the push line. The page is scanned like the files (its data is deflated, so the scan reads the strings of
its pack: names, files, meta). The branch must not be checked out in another worktree (git refuses; the message says so).
--dry-run renders and scans, prints what would be written, writes and commits nothing.

stale(): called by the branch level for a project that has published (its default directory has a .dc-digest): ONE W
dc.publish-stale when the digest of the inputs (the blobs of the files that have a node in the graph, outside the directory, plus the
version of this module) differs from the stored one, or when a published file is missing or changed. It never renders; a branch run
that built no graph says so (I dc.publish-unchecked). The W never blocks: only `graph publish` repairs it.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .. import safepath
from . import lens as dclens
from . import levels, safety, snippets, viz
from .model import Finding, split_key

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "publish.json")
VERSION = 2  # part of the input digest: bump it when the published files change shape
WHERE = safety.WHERE
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def settings() -> dict:
    with open(DATA, encoding="utf-8") as fh:
        return json.load(fh)


def _md(text: str) -> str:
    """A name inside a markdown table cell or a link text: no control characters, no backtick, no pipe."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", text).replace("`", "'").replace("|", "/")


def _key_name(key: str) -> str:
    _lang, path, qual, _arity, _h8 = split_key(key)
    return qual or path.rpartition("/")[2] or path


# ---------------------------------------------------------------- the pictures
def _entries(res) -> List[Tuple[int, str, str]]:
    """(node index, kind, origin) of the production entry points, hard roots first, then by key: one row per node."""
    p, seen, out = res.prep, set(), []
    for rank, cls in enumerate(("hard", "soft")):
        for v, ri in p.seeds[cls]:
            if v not in seen:
                seen.add(v)
                r = p.graph.roots[ri]
                out.append((rank, p.keys[v], v, r.kind, r.origin))
    return [(v, kind, origin) for _rank, _key, v, kind, origin in sorted(out)]


def _forward(prep, v: int, depth: int, cap: int) -> Tuple[Set[int], bool]:
    """The nodes `depth` levels forward of node v, level by level in index order, at most `cap`; True when the cap cut some."""
    seen, level, cut = {v}, [v], False
    for _ in range(depth):
        nxt = sorted({prep.dst[j] for u in level for j in range(prep.off[u], prep.off[u + 1])} - seen)
        if len(nxt) > cap - len(seen):
            nxt, cut = nxt[: max(0, cap - len(seen))], True
        seen.update(nxt)
        level = nxt
        if not level:
            break
    return seen, cut


_FRAME = "rounded=1;html=0;fillColor=none;dashed=1;strokeColor=#999999;fontColor=#555555;fontSize=9;align=left;verticalAlign=bottom;verticalLabelPosition=top;"
_REL = '<mxGeometry relative="1" as="geometry"/>'


def _geo(x: float, y: float, w: float, h: float) -> str:
    return f'<mxGeometry x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" as="geometry"/>'


def _cell(cid: str, value: str, style: str, geo: str, kind: str = 'vertex="1"') -> str:
    v = html.escape(value, quote=True).replace("\n", "&#xa;")
    return f'<mxCell id="{cid}" value="{v}" style="{style}" {kind} parent="1">{geo}</mxCell>'


def drawio(d: viz.Diagram) -> str:
    """draw.io XML (uncompressed mxfile) of the module overview, on the boxes of viz.module_grid: a frame per top-level directory, a cell
    per module (name and counts, the colour of the SVG), an edge per module pair (the call count as its label)."""
    xy, cw, width, height, frames = viz.module_grid(d, {n.id: viz._box_w(n) for n in d.nodes})
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    cells += [_cell(f"g{i}", viz._clip(g, 40), _FRAME, _geo(x, y, w, h)) for i, (g, x, y, w, h) in enumerate(frames)]
    for n in d.nodes:
        fill, ink = viz._paint(n)
        style = f"rounded=1;html=0;whiteSpace=wrap;fillColor={fill};fontColor={ink};strokeColor=#333333;fontSize=11;"
        text = f"{viz._clip(n.label)}\n{viz._clip(n.sub)}"
        cells.append(_cell(n.id, text, style, _geo(*xy[n.id], cw, viz.BOX_H)))
    for i, e in enumerate(d.edges):
        if e.a != e.b:
            dash = "dashed=1;" if e.tier in ("N", "D") else ""
            width_ = 1 + (e.n > 9) + (e.n > 99)
            style = f"endArrow=classic;html=0;strokeColor=#555555;strokeWidth={width_};{dash}fontSize=8;"
            kind = f'edge="1" source="{e.a}" target="{e.b}"'
            cells.append(_cell(f"e{i}", str(e.n) if e.n > 1 else "", style, _REL, kind))
    return (
        '<mxfile host="dead-code-checker" version="1"><diagram id="modules" name="modules"><mxGraphModel dx="0" dy="0" grid="1" '
        'gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="0" pageScale="1" '
        f'pageWidth="{width:.0f}" pageHeight="{height:.0f}" math="0" shadow="0"><root>'
        + "".join(cells)
        + "</root></mxGraphModel></diagram></mxfile>\n"
    )


def input_digest(graph, tree, out_rel: str) -> str:
    """sha256 of what a publication is made from: the version of this module and, for every file that has a node in the graph and is not
    inside the output directory, its path and blob. Another file (a README, the directory itself) does not make it stale.
    """
    skip = out_rel.strip("/") + "/"
    h = hashlib.sha256(f"dc.publish/{VERSION}\n".encode())
    for path in sorted({n.path for n in graph.nodes}):
        ref = tree.files.get(path)
        if ref is not None and not path.startswith(skip):
            h.update(f"{path}\0{ref.blob}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _slug(name: str) -> str:
    return _UNSAFE.sub("-", name).strip("-.")[:32].lower() or "entry"


def render(graph, res, tree, suspects, name: str, out_rel: str) -> Dict[str, str]:
    """The files of a publication as {path relative to the output directory: text}, without .dc-digest (see the module docstring)."""
    st = settings()
    vd = {**res.verdicts(), **dict.fromkeys(suspects, "suspect")}
    mods = viz.modules_diagram(graph, res, suspects=suspects, page="")
    files = {"modules.svg": viz.emit_svg(mods), "modules.drawio": drawio(mods)}
    layered = dclens.arch_svg(dclens.collect(graph, res, None, suspects))  # the graph alone: no facts, no code
    if layered:
        files["architecture.svg"] = layered
    cfg, pic = st["calls"], []
    ents = _entries(res)
    for i, (v, kind, origin) in enumerate(ents[: cfg["max"]], 1):
        idxs, cut = _forward(res.prep, v, cfg["depth"], cfg["nodes"])
        key = res.prep.keys[v]
        label = _key_name(key)
        d = viz.diagram(
            graph,
            res,
            idxs,
            {key: viz.MARK_TOUCHED},
            f"calls from {label}",
            "",
            max_nodes=len(idxs) + 1,
            suspects=suspects,
        )
        rel = f"calls/{i:03d}-{_slug(label)}.svg"
        files[rel] = viz.emit_svg(d)
        pic.append((rel, label, kind, origin, len(idxs), cut))
    files["index.md"] = _index(
        graph, vd, mods, pic, len(ents), input_digest(graph, tree, out_rel), name, st, "architecture.svg" in files
    )
    return dict(sorted(files.items()))


def _index(
    graph, vd, mods: viz.Diagram, pic, n_entries: int, digest: str, name: str, st: dict, layered: bool = False
) -> str:
    nodes = [n for n in graph.nodes]
    files, syms = Counter(n.lang for n in nodes if n.kind == "file"), Counter(n.lang for n in nodes if n.kind != "file")
    verdicts = Counter(vd[n.key] for n in nodes)
    cfg = st["calls"]
    lines = [
        f"# Architecture of {_md(name)}",
        "",
        "Made by `dead-code-checker graph publish` from the call graph of the repository: names, counts and relations only, no source code. "
        "Do not edit it by hand; run the command again.",
        "",
        f"Input digest: `{digest[:12]}`",
        "",
        "## At a glance",
        "",
        f"- {sum(files.values())} files, {len(nodes)} nodes, {len(graph.edges)} edges, {len(graph.roots)} registered roots",
        "- verdicts: " + ", ".join(f"{v} {c}" for v, c in sorted(verdicts.items())),
        "",
        "## Languages",
        "",
        "| language | files | symbols |",
        "| --- | ---: | ---: |",
        *[f"| {_md(lang)} | {files[lang]} | {syms[lang]} |" for lang in sorted(set(files) | set(syms))],
        "",
        "## Diagrams",
        "",
        "- [Module overview](modules.svg), the same as [draw.io](modules.drawio) (open it with diagrams.net)",
        *(
            [
                "- [Layered architecture](architecture.svg): modules by dependency layer, cycles outlined, edges between languages dashed"
            ]
            if layered
            else []
        ),
        "",
        f"## Modules ({len(mods.nodes)})",
        "",
        "| module | nodes, dead, suspect |",
        "| --- | --- |",
        *[f"| `{_md(n.label)}` | {_md(n.sub)} |" for n in mods.nodes[: st["modules_rows"]]],
    ]
    if len(mods.nodes) > st["modules_rows"]:
        lines.append(f"\n{len(mods.nodes) - st['modules_rows']} more modules are in the pictures.")
    by = {n.id: n.label for n in mods.nodes}
    top = sorted(mods.edges, key=lambda e: (-e.n, by[e.a], by[e.b]))[: st["edge_rows"]]
    lines += ["", "## Calls between modules", "", "| from | to | references |", "| --- | --- | ---: |"]
    lines += [f"| `{_md(by[e.a])}` | `{_md(by[e.b])}` | {e.n} |" for e in top]
    lines += ["", f"## Entry points ({n_entries})", ""]
    lines += [
        f"- [`{_md(label)}`]({rel}) ({_md(kind)}, {_md(origin)}): {n} nodes within {cfg['depth']} levels"
        + (", cut" if cut else "")
        for rel, label, kind, origin, n, cut in pic
    ]
    if n_entries > len(pic):
        lines.append(f"\n{n_entries - len(pic)} more entry points are not drawn (at most {cfg['max']}).")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- the scan
def _markers(deny: Sequence[str]) -> List[Tuple[str, "re.Pattern[str]"]]:
    """(reason, pattern) of the built-in markers (data/publish.json) and of the user's deny list."""
    st = settings()
    out = [(m["reason"], re.compile(m["pattern"], re.I if m.get("icase") else 0)) for m in st["markers"]]
    roots = "|".join(re.escape(r) for r in st["abs_roots"])
    out.append(("an absolute local path", re.compile(rf"(?<![\w.~-])/(?:{roots})/")))
    out.append(("an absolute local path", re.compile(r"(?<![\w])[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]")))
    for w in deny:
        try:
            out.append(("a word of the deny list", re.compile(w, re.I)))
        except re.error:
            out.append(("a word of the deny list", re.compile(re.escape(w), re.I)))
    return out


def read_deny(path: str) -> List[str]:
    """The deny list of a file: one word or regular expression per line, blank lines and `#` lines skipped. OSError when unreadable."""
    with open(path, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh.read().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def marker_scan(files: Dict[str, str], deny: Sequence[str] = ()) -> List[str]:
    """The built-in scan: `name:line: reason` for every marker in every file (one entry per marker and file, the first line)."""
    out, marks = [], _markers(deny)
    for n in sorted(files):
        lines = files[n].split("\n")
        for reason, pat in marks:
            for i, ln in enumerate(lines, 1):
                if pat.search(ln):
                    out.append(f"{n}:{i}: {reason} (the text is not shown)")
                    break
    return out


def scan(files: Dict[str, str], deny: Sequence[str] = ()) -> List[str]:
    """Why `files` may not be published ([] = clean): `name[:lines]: reason`, never the text itself. Every check runs, so one answer
    lists everything."""
    names = sorted(files)
    out = marker_scan(files, deny)
    exe = snippets.find_gitleaks()
    if exe is None:
        out.append(f"(scan): gitleaks is not installed; the scan is not optional ({snippets.NOTE})")
    else:
        try:
            out += [
                f"{names[i]}:{a}-{b}: gitleaks finding (the value is not shown)"
                for i, a, b in snippets.scan([files[n] for n in names], exe)
            ]
        except RuntimeError as exc:
            out.append(f"(scan): gitleaks did not finish ({exc}); nothing is published unscanned")
    return out


# ---------------------------------------------------------------- writing
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_digest(out: str) -> Optional[dict]:
    try:
        with open(os.path.join(out, settings()["digest_file"]), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    ok = isinstance(doc, dict) and isinstance(doc.get("input"), str) and isinstance(doc.get("files"), dict)
    return doc if ok else None


def _safe_rel(rel: str) -> bool:
    return (
        bool(rel)
        and not os.path.isabs(rel)
        and "\\" not in rel
        and all(x not in ("", ".", "..") for x in rel.split("/"))
    )


class Unsafe(Exception):
    """A path of the publication leads out of the output directory through a symlink (or the directory is one): nothing was touched."""


def _tmp(path: str) -> str:
    return f"{path}.tmp{os.getpid()}"


def _inside(real_out: str, path: str) -> bool:
    real = os.path.realpath(path)
    return real == real_out or real.startswith(real_out + os.sep)


def _gone(out: str, files: Dict[str, str]) -> List[str]:
    """The files the earlier .dc-digest names that this publication no longer makes (only names that stay below the directory)."""
    old = _read_digest(out)
    return sorted(r for r in (old or {}).get("files", {}) if r not in files and _safe_rel(r))


def check_out(out: str, files: Dict[str, str], gone: Sequence[str]) -> None:
    """Raise Unsafe unless `out` is not a symlink and every path the publication writes (with its temporary file) or removes resolves
    under realpath(out): a write or a delete never follows a link out of the directory. A foreign symlink that no path goes through
    is left alone."""
    if os.path.islink(out):
        raise Unsafe("the output directory is a symlink")
    real_out = os.path.realpath(out)
    plan = [(rel, "written") for rel in [*files, settings()["digest_file"]]] + [(rel, "removed") for rel in gone]
    for rel, verb in plan:
        path = os.path.join(out, *rel.split("/"))
        if not (_inside(real_out, path) and _inside(real_out, _tmp(path))):
            raise Unsafe(f"{rel} would be {verb} through a symlink that leads outside the output directory")


def _put(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = _tmp(path)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def write(out: str, files: Dict[str, str], digest: str) -> List[str]:
    """Write `files` and .dc-digest under `out`; returns the stale files (named by the earlier .dc-digest) that were removed.
    Raises Unsafe, before anything is written or removed, when a path would go through a symlink out of `out` (see check_out).
    """
    gone = _gone(out, files)
    check_out(out, files, gone)
    for rel, text in files.items():
        _put(os.path.join(out, *rel.split("/")), text)
    for rel in gone:
        Path(out, *rel.split("/")).unlink(missing_ok=True)
    doc = {"v": VERSION, "input": digest, "files": {r: _sha(t) for r, t in files.items()}}
    _put(os.path.join(out, settings()["digest_file"]), json.dumps(doc, indent=1, sort_keys=True) + "\n")
    return gone


# ---------------------------------------------------------------- the stale check of the branch level
def stale(root: str, sink: dict) -> List[Finding]:
    """The branch-level W for a project that has published (its default directory has a digest file); [] for one that has not.
    Reads a few small files, renders nothing."""
    rel = settings()["out"]
    out = os.path.join(root, *rel.split("/"))
    if not os.path.isfile(os.path.join(out, settings()["digest_file"])):
        return []  # never published here: nothing to be stale
    graph, tree = sink.get("graph"), sink.get("tree")
    if graph is None or tree is None:
        return [
            Finding(
                "I",
                WHERE,
                0,
                "dc.publish-unchecked",
                "the published architecture exists, but this run built no graph, so it was not compared",
            )
        ]
    doc = _read_digest(out)
    if doc is None:
        why = f"{rel}/{settings()['digest_file']} cannot be read"
    elif doc["input"] != input_digest(graph, tree, rel):
        why = "the code it was made from has changed"
    else:
        bad = [
            r
            for r, s in sorted(doc["files"].items())
            if not _safe_rel(r) or _file_sha(os.path.join(out, *r.split("/"))) != s
        ]
        why = f"{bad[0]} is missing or was edited" if bad else ""
    if not why:
        return []
    msg = f"the published architecture in {rel} is out of date: {why}"
    return [
        Finding(
            "W",
            rel,
            0,
            "dc.publish-stale",
            msg,
            "this project has published its architecture; `dead-code-checker graph publish` writes the files again, `git add` them yourself; this never blocks anything",
        )
    ]


def _file_sha(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:  # universal newlines: a checkout with CRLF (Windows) is the same text
            return _sha(fh.read())
    except (OSError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------- gh-pages
def _git(cwd: str, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX")}
    r = safepath.run(["git", "-C", cwd, *args], capture_output=True, text=True, env=env, timeout=300)
    if r.returncode:
        last = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["no message"]
        raise RuntimeError(f"git {args[0]} failed: {last[0]}"[:300])
    return r.stdout.strip()


def commit_page(root: str, page: str) -> Tuple[str, str]:
    """index.html of the page in ONE commit on the local branch gh-pages (created as an orphan when it is not there): (status, commit)
    with status "committed" or "unchanged". RuntimeError when git refuses (a hook, the signature, the branch in use); the branch is
    then as it was. Nothing is pushed."""
    branch = settings()["branch"]
    have = (
        safepath.run(
            ["git", "-C", root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], capture_output=True
        ).returncode
        == 0
    )
    with tempfile.TemporaryDirectory(prefix="dc-gh-pages-") as td:
        wt = os.path.join(td, "wt")
        try:
            if have:
                _git(root, "worktree", "add", "-q", wt, branch)
            else:
                _git(root, "worktree", "add", "-q", "--no-checkout", "--detach", wt)
                _git(wt, "checkout", "-q", "--orphan", branch)
                _git(wt, "read-tree", "--empty")  # the orphan starts with the tree of HEAD staged
            _put(os.path.join(wt, "index.html"), page)
            _git(wt, "add", "index.html")
            if safepath.run(["git", "-C", wt, "diff", "--cached", "--quiet"]).returncode == 0 and have:
                return "unchanged", _git(wt, "rev-parse", "HEAD")
            _git(wt, "commit", "-q", "-m", "dead-code-checker graph publish: architecture page")
            return "committed", _git(wt, "rev-parse", "HEAD")
        finally:
            safepath.run(["git", "-C", root, "worktree", "remove", "--force", wt], capture_output=True)
            safepath.run(["git", "-C", root, "worktree", "prune"], capture_output=True)


def page_texts(pack) -> str:
    """Everything a pack says in words (the page itself is deflated and base64, a scan would read noise): strings, files, meta."""
    h = pack.head
    return "\n".join([*h["strings"], *h["files"], *h["langs"], json.dumps(h["meta"], sort_keys=True)]) + "\n"


def _remote(root: str) -> str:
    try:
        names = _git(root, "remote").split()
    except RuntimeError:
        names = []
    return (
        f"git push {names[0] if 'origin' not in names else 'origin'} gh-pages"
        if names
        else "git remote add origin URL && git push -u origin gh-pages"
    )


# ---------------------------------------------------------------- the command
def _out_dir(root: str, out: Optional[str]) -> Optional[Tuple[str, str]]:
    """(absolute directory, path relative to the root) of --out; None when it is not strictly inside the repository or is in .git."""
    top = os.path.realpath(root)
    path = os.path.realpath(os.path.join(top, out or settings()["out"]))
    rel = os.path.relpath(path, top).replace(os.sep, "/")
    if rel in (".", "") or rel.startswith("..") or os.path.isabs(rel) or rel.split("/")[0] == ".git":
        return None
    return path, rel


def _out_is_link(root: str, out: Optional[str]) -> bool:
    """True when --out itself (the last component, also written with a trailing slash or a dot) is a symlink."""
    return os.path.islink(os.path.normpath(os.path.join(os.path.realpath(root), out or settings()["out"])))


def _refusal(exc: Unsafe) -> str:
    return f"dead-code-checker graph publish: refused, {exc}; nothing was written or removed"


def run(a, root: str, steps: Optional[levels.Steps] = None, layout=None) -> int:
    """`graph publish`: exit 0 written (or dry run clean), 1 a finding of the scan or a failed commit, 2 usage / an output path that
    goes through a symlink. `a` has out, gh_pages, dry_run and deny_file; `root` is the repository."""
    P = "dead-code-checker graph publish:"
    if _out_is_link(root, a.out):
        print(f"{P} --out is a symlink; publish into a real directory: {a.out or settings()['out']}", file=sys.stderr)
        return 2
    where = _out_dir(root, a.out)
    if where is None:
        print(f"{P} --out must be a directory inside the repository (not the root, not .git): {a.out}", file=sys.stderr)
        return 2
    deny: List[str] = []
    if getattr(a, "deny_file", None):
        try:
            deny = read_deny(a.deny_file)
        except OSError as exc:
            print(f"{P} cannot read --deny-file {a.deny_file}: {exc}", file=sys.stderr)
            return 2
    out, rel = where
    notes: list = []
    b, res, proof = levels.analyzed_proof(root, notes, steps, layout)
    for f in notes:
        print(f.render(), file=sys.stderr)
    if not b.graph.nodes:
        print(f"{P} the project has no code graph: nothing to publish", file=sys.stderr)
        return 2
    suspects = levels.suspect_keys(b.graph, res, proof)
    name = os.path.basename(os.path.realpath(root))
    files = render(b.graph, res, b.tree, suspects, name, rel)
    page, checked = "", dict(files)
    if a.gh_pages:
        meta = {
            "scope": "repo",
            "files": sum(
                n.kind == "file" for n in b.graph.nodes
            ),  # not the tree: the files of an earlier publication must not change the page
            "nodes": len(b.graph.nodes),
            "edges": len(b.graph.edges),
            "roots": len(b.graph.roots),
            "verdicts": res.counts(),
        }
        pg = viz.make_page(
            b.graph,
            res,
            meta=meta,
            title=f"architecture of {name}",
            suspects=suspects,
            snippets=snippets.off("publication pages carry no code", finding=False),
        )
        if not pg.ok:
            print(f"{P} the page is over the size limit even when cut; --gh-pages did nothing", file=sys.stderr)
            return 1
        page = pg.html
        checked.update({"gh-pages/index.html": page, "gh-pages/index.html.strings": page_texts(pg.pack)})
    found = scan(checked, deny)
    if found:
        print(f"{P} {len(found)} finding(s) in the output; nothing was written:", file=sys.stderr)
        for line in found:
            print(f"  {line}", file=sys.stderr)
        return 1
    try:
        check_out(out, files, _gone(out, files))
    except Unsafe as exc:
        print(_refusal(exc), file=sys.stderr)
        return 2
    if a.dry_run:
        print(f"{P} dry run, the scan is clean; would write to {rel}:")
        for n, t in files.items():
            print(f"  {rel}/{n} ({len(t.encode('utf-8'))} bytes)")
        if page:
            print(
                f"  gh-pages:index.html ({len(page.encode('utf-8'))} bytes) in one commit of the local branch {settings()['branch']}"
            )
        return 0
    try:
        gone = write(out, files, input_digest(b.graph, b.tree, rel))
    except Unsafe as exc:
        print(_refusal(exc), file=sys.stderr)
        return 2
    print(f"{P} wrote {len(files)} files to {rel} ({len(gone)} stale removed); nothing is committed, `git add {rel}` is yours")
    if page:
        try:
            status, sha = commit_page(root, page)
        except RuntimeError as exc:
            print(f"{P} the files are written, the page is not committed ({exc})", file=sys.stderr)
            return 1
        print(f"{P} {settings()['branch']} {status} ({sha[:12]}); nothing was pushed. To publish it yourself:\n  {_remote(root)}")
    return 0
