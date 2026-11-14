"""graph.extract.common: what the extractors share.

text_checker()          dead_code_checker.core, the v1 text checker (py_symbols, the stub detectors), imported lazily and once: the
                        extractors reuse those functions instead of copying them. Importing it costs a fraction of a second in a
                        fresh process, so nothing here pays for it until a file is really extracted.
merge_defs(defs)        one Def per qual (a redefined or conditionally defined name: min line0, max line1, the widest vis (a private overload does not hide a public one), flags or-ed,
                        F_STUB only when every definition is a stub), sorted by (line0, qual); canonical() raises on two
                        nodes with one key, so an extractor must never hand the linker duplicates.
protocol_member(rules, qual, kind, vis, parent, words)  does a rules "protocol" rule hold for a definition (a member that a language, a framework
                        or a serializer reaches without naming it: alive with its type, link.py)? Keys of a rule, all optional and all must hold:
                        re (searched in the qualified name), kinds, vis, parents (kind of the enclosing def), skip_mods (modifier words).
mark_generated(text, defs)     F_GENERATED on the definitions inside a generated block of the text (data/generated.json: `// BEGIN GENERATED` .. `// END GENERATED`), generated_sha() the sha of that table.
generated_path(path), generated_head(text)   a SCHEMA file (generated.json "path_exts": .proto, .graphql, .sql) whose path is a vendored or generated copy ("paths": google/protobuf,
                        vendored/, third-party/; code of any other language is never flagged by them), a text whose first KB says a
                        generator wrote it ("headers"): the definitions of such a file are flagged generated whole (graph.link.path_flags for the path, graph.extract.schema for the head).
module_def(text, stub)         the Def of the file itself: qual "" (node key "lang:path"), kind "module". Its `stub` is the
                        file-level verdict of py_hook_stub_reason / sh_hook_stub_reason ("a registered hook of this file
                        would be an empty stub"); F_STUB stays unset: only the roots layer knows the file is registered.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import threading
from functools import lru_cache
from typing import Dict, Iterable, List, Tuple

from ..model import F_GENERATED, F_STUB, VIS, Def

_LOCK = threading.Lock()


def text_checker():
    """The v1 text checker module (dead_code_checker.core)."""
    with _LOCK:
        return importlib.import_module("dead_code_checker.core")


def merge_defs(defs: Iterable[Def]) -> List[Def]:
    by: Dict[str, Def] = {}
    for d in defs:
        old = by.get(d.qual)
        if old is None:
            by[d.qual] = d
            continue
        flags = (old.flags | d.flags) & ~F_STUB | (old.flags & d.flags & F_STUB)
        by[d.qual] = old._replace(
            line0=min(old.line0, d.line0),
            line1=max(old.line1, d.line1),
            flags=flags,
            vis=max(old.vis, d.vis, key=VIS.index),
            stub=old.stub if old.stub == d.stub else "",
            bh="",  # two bodies under one name (overloads): no single body to compare
            bsz=0,
        )
    return sorted(by.values(), key=lambda d: (d.line0, d.qual))


GENERATED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "generated.json")


@lru_cache(maxsize=1)
def _generated() -> Tuple[tuple, str]:
    with open(GENERATED, "rb") as fh:
        raw = fh.read()
    return tuple((b, e) for b, e in json.loads(raw)["blocks"]), hashlib.sha256(raw).hexdigest()[:12]


@lru_cache(maxsize=1)
def _generated_rx() -> Tuple["re.Pattern", "re.Pattern", Tuple[str, ...]]:
    with open(GENERATED, encoding="utf-8") as fh:
        d = json.load(fh)
    rx = [re.compile("|".join(f"(?:{p})" for p in d[k])) for k in ("paths", "headers")]
    return rx[0], rx[1], tuple(d["path_exts"])


def generated_path(path: str) -> bool:
    """A vendored or generated copy of a schema file by its path (the rules never apply to the code of other languages)."""
    rx, _, exts = _generated_rx()
    return path.lower().endswith(exts) and bool(rx.search(path))


def generated_head(text: str) -> bool:
    return bool(_generated_rx()[1].search(text[:1024]))


def generated_sha() -> str:
    """The sha of data/generated.json: part of the grammar_sha of an extractor that calls mark_generated (an edited table is a new shard key)."""
    return _generated()[1]


def mark_generated(text: str, defs: Iterable[Def]) -> List[Def]:
    """`defs` with F_GENERATED on every definition inside a generated block of `text` (data/generated.json marker pairs)."""
    defs, blocks = list(defs), _generated()[0]
    if not any(b in text for b, _ in blocks):
        return defs
    spans, end, start = [], "", 0
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if end:
            if s.startswith(end):
                spans.append((start, i))
                end = ""
        else:
            end, start = next(((e, i) for b, e in blocks if s.startswith(b)), ("", 0))
    return [
        (
            d._replace(flags=d.flags | F_GENERATED)
            if d.qual and any(a <= d.line0 and d.line1 <= b for a, b in spans)
            else d
        )
        for d in defs
    ]


def module_def(text: str, stub: str = "") -> Def:
    return Def("", "module", 1, max(1, len(text.splitlines())), "public", 0, -1, stub or "")


def protocol_member(rules: dict, qual: str, kind: str, vis: str, parent: str, words: Iterable[str] = ()) -> bool:
    words = set(words)
    return any(
        (not r.get("re") or re.search(r["re"], qual))
        and kind in r.get("kinds", (kind,))
        and vis in r.get("vis", (vis,))
        and parent in r.get("parents", (parent,))
        and not words & set(r.get("skip_mods", ()))
        for r in rules.get("protocol", ())
    )
