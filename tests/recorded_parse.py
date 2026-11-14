"""A recorded tree-sitter parse for the ts_generic backend seam (tests only; no grammar needed).

tests/fixtures/<lang>/<case>/parse.json: {"<file>": {"<kind>": [[pattern_index, {"<capture>": "<spec>", ...}], ...]}}
with kind in defs | refs | roots | stub | imports | exports | types | xlang | embed: the matches the queries of dead_code_checker/graph/data/queries/<grammar>/<kind>.scm give for
the file, one entry per match, written by hand from the grammar's node types (no grammar was installed when this was
recorded). check_patterns() ties every recorded match to the query file (the pattern exists, the capture names belong to
it), so a query edit that moves a pattern fails the test instead of silently feeding the mapping something the real
query never gives.

A node is a spec string, located in the source text:
  "text"       the first occurrence ("text#2": the third) inside the span of the FIRST capture of the match (the
               anchor, whose own occurrence is counted over the whole file); a capture that is not inside the anchor, or
               one written "<text", is the nearest occurrence before the anchor (the type of an impl header is outside
               the method); "\\<text" is a text that starts with "<" inside the anchor
  "a…b"        the span from "a" to the first "b" after it (a node that covers several lines)
A capture value may be a list of specs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Set, Tuple

KINDS = ("defs", "refs", "roots", "stub", "imports", "exports", "types", "xlang", "embed")


class FNode(NamedTuple):
    start_byte: int
    end_byte: int
    start_point: tuple
    end_point: tuple
    parent: object = (
        None  # a recorded node is a root: it is no operand of a chain of literals (graph.extract.xlang.concat_chains)
    )


def _split(spec: str) -> Tuple[str, int]:
    m = re.fullmatch(r"(.*?)#(\d+)", spec, re.S)
    return (m.group(1), int(m.group(2))) if m else (spec, 0)


def _find(src: str, text: str, nth: int, lo: int, hi: int) -> int:
    pos = lo - 1
    for _ in range(nth + 1):
        pos = src.find(text, pos + 1, hi)
        if pos < 0:
            raise AssertionError(f"recorded parse: {text!r} (#{nth}) not found in [{lo}, {hi})")
    return pos


class Recorded:
    """The object ts_generic expects from a backend: .errors and .run(kind)."""

    def __init__(self, source: str, matches: Dict[str, list], errors: int = 0):
        self.errors, self.src, self.m = errors, source, matches
        self.rows = [0] + [i + 1 for i, ch in enumerate(source) if ch == "\n"]

    def point(self, pos: int) -> tuple:
        row = max(i for i, o in enumerate(self.rows) if o <= pos)
        return row, pos - self.rows[row]

    def node(self, start: int, end: int) -> FNode:
        # byte offsets: the fixtures are ASCII, so characters are bytes
        return FNode(start, end, self.point(start), self.point(end))

    def locate(self, spec: str, lo: int, hi: int) -> Tuple[int, int]:
        text, nth = _split(spec)
        head, sep, tail = text.partition("…")
        start = _find(self.src, head, nth, lo, hi)
        end = start + len(head)
        if sep:
            end = self.src.index(tail, end) + len(tail)
        return start, end

    def run(self, kind: str):
        out = []
        for pi, caps in self.m.get(kind, ()):
            res, anchor = {}, None
            for name, specs in caps.items():
                nodes = []
                for spec in [specs] if isinstance(specs, str) else specs:
                    if anchor is None:
                        s, e = self.locate(spec, 0, len(self.src))
                        anchor = (s, e)
                    else:
                        literal = spec.startswith("\\<")  # "\\<T>::f": a text that itself starts with "<"
                        before = spec.startswith("<")  # "<Square": the nearest occurrence before the anchor (an impl)
                        text, nth = _split(spec[1:] if before or literal else spec)
                        head = text.partition("…")[0]
                        if not before and self.src.find(head, anchor[0], anchor[1]) >= 0:
                            s, e = self.locate(text + (f"#{nth}" if nth else ""), anchor[0], anchor[1])
                        else:
                            s = self.src.rfind(head, 0, anchor[0])
                            if s < 0:
                                raise AssertionError(f"recorded parse: {spec!r} not found before the match")
                            e = s + len(head)
                    nodes.append(self.node(s, e))
                res[name] = nodes
            out.append((pi, res))
        return out


def scm_pattern_texts(path: Path) -> List[str]:
    """The source of each top-level pattern of a query file, in order (comments dropped)."""
    text = re.sub(r";[^\n]*", "", path.read_text(encoding="utf-8"))
    pats: List[str] = []
    depth = 0
    for ch in text:
        if ch in "([":
            if depth == 0:
                pats.append("")  # a new top-level pattern; what follows its closing bracket (@captures) belongs to it
            depth += 1
        elif ch in ")]":
            depth -= 1
        if pats:
            pats[-1] += ch
    return pats


def scm_patterns(path: Path) -> List[Set[str]]:
    """The capture names of each top-level pattern of a query file, in order (comments dropped)."""
    return [set(re.findall(r"@([A-Za-z_.-]+)", p)) for p in scm_pattern_texts(path)]


def check_patterns(query_dir: Path, recorded: Dict[str, dict]) -> int:
    """Raise AssertionError for a recorded match that does not fit its query file; returns the number checked."""
    n = 0
    for file, by_kind in recorded.items():
        for kind, matches in by_kind.items():
            assert kind in KINDS, (file, kind)
            pats = scm_patterns(query_dir / f"{kind}.scm")
            for pi, caps in matches:
                assert 0 <= pi < len(pats), (file, kind, pi, len(pats))
                assert set(caps) <= pats[pi], (file, kind, pi, sorted(set(caps) - pats[pi]))
                n += 1
    return n
