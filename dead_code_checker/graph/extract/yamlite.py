"""graph.extract.yamlite: an indentation YAML reader without PyYAML, enough for k8s manifests, Helm values, GitHub Actions, GitLab CI and compose files.

    docs, errors = parse(text)      docs: [Node] one per `---` document (a document that is not a mapping or a sequence is dropped)
    Node(kind, val, line, end)      kind "m" mapping (val = [(key, Node)], order and duplicate keys kept), "s" sequence
                                    (val = [Node]), "q" scalar (val = str, quotes and a trailing comment removed; a block scalar
                                    keeps its text; a `!tag [..]` value keeps the tag text, a core `!!str` tag is dropped). line = where the node starts (the key
                                    line for a value introduced by a key), end = its last line.
    get(node, key) / scalar(node) / items(node) / walk(node) are the readers the extractors and the root sources use.

Supported: block mappings and sequences (also `key:` followed by a sequence at the same indent, `- key: v` items), flow `[a, b]` and
`{k: v}` (also over several lines), quoted scalars, `|` and `>` block scalars, plain multi-line scalars (the continuation is skipped),
comments, anchors (dropped), aliases and merge keys (kept as plain text, never expanded). Not supported: complex `? ` keys, tags
with structure, `%` directives. A line that cannot be read adds 1 to `errors` and is skipped: the result is always a best effort.
"""

from __future__ import annotations

import re
from typing import Iterator, List, NamedTuple, Optional, Tuple

MAX_DEPTH = 64


class Node(NamedTuple):
    kind: str
    val: object
    line: int
    end: int


_DOC = re.compile(r"^---(\s|$)")
_COLON = re.compile(r":(\s|$)")
_ANCHOR = re.compile(r"^&\S+\s*")
_CORE_TAG = re.compile(
    r"^!![\w-]+\s*"
)  # `!!str x`: a core tag says nothing about the name (`!reference [..]` keeps its tag)
_BLOCK = re.compile(r"^[|>][+\-0-9]*$")


def _strip_comment(s: str) -> str:
    q = ""
    for i, c in enumerate(s):
        if q:
            if c == q:
                q = ""
        elif c in "\"'" and (i == 0 or s[i - 1] in " \t[{,:-"):
            q = c
        elif c == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
    return s.rstrip()


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1].replace("''", "'") if s[0] == "'" else s[1:-1].replace('\\"', '"')
    return s


def _split_key(c: str) -> Optional[Tuple[str, str]]:
    """`key: rest` -> (key, rest); None when the line is not a mapping entry."""
    if not c or c[0] in "[{" or c.startswith("- ") or c == "-":
        return None
    if c[0] in "\"'":
        end = c.find(c[0], 1)
        while end != -1 and c[0] == '"' and c[end - 1] == "\\":
            end = c.find(c[0], end + 1)
        if end == -1 or not _COLON.match(c, end + 1):
            return None
        return c[1:end], c[end + 2 :].strip()
    m = _COLON.search(c)
    if not m or m.start() == 0:
        return None
    return c[: m.start()].strip(), c[m.end() :].strip()


def _balanced(s: str) -> bool:
    depth, q = 0, ""
    for c in s:
        if q:
            q = "" if c == q else q
        elif c in "\"'":
            q = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
    return depth <= 0


def _top_split(s: str) -> List[str]:
    out, depth, q, cur = [], 0, "", []
    for c in s:
        if q:
            q = "" if c == q else q
        elif c in "\"'":
            q = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif c == "," and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(c)
    if "".join(cur).strip():
        out.append("".join(cur))
    return out


def _flow(s: str, line: int, end: int) -> Node:
    s = s.strip()
    if s[:1] == "[" and s[-1:] == "]":
        return Node("s", [_flow(p, line, end) for p in _top_split(s[1:-1])], line, end)
    if s[:1] == "{" and s[-1:] == "}":
        ents = []
        for p in _top_split(s[1:-1]):
            kv = _split_key(p.strip()) or (p.strip(), "")
            ents.append((_unquote(kv[0]), _flow(kv[1], line, end)))
        return Node("m", ents, line, end)
    return Node("q", _unquote(s), line, end)


class _P:
    def __init__(self, rows: list):
        self.rows, self.n, self.errors = rows, len(rows), 0  # a row: [indent, content, line]

    def _end(self, i: int) -> int:
        return self.rows[i - 1][2] if i else 0

    def block(self, i: int, depth: int = 0) -> Tuple[Node, int]:
        ind, c, ln = self.rows[i]
        if depth > MAX_DEPTH:
            return Node("q", "", ln, ln), i + 1
        return self.seq(i, ind, depth) if (c == "-" or c.startswith("- ")) else self.map(i, ind, depth)

    def value(self, rest: str, i: int, ind: int, ln: int, depth: int) -> Tuple[Node, int]:
        """The value after `key:` or `- ` on row i-1 (i = the next row): inline, flow, block scalar or nested block."""
        rest = _strip_comment(rest)
        for rx in (_ANCHOR, _CORE_TAG, _ANCHOR):  # `&a !!str x` and `!!str &a x`
            rest = rx.sub("", rest.strip())
        rest = rest.strip()
        if _BLOCK.match(rest):
            j, text = i, []
            while j < self.n and self.rows[j][0] > ind:
                text.append(self.rows[j][1])
                j += 1
            return Node("q", "\n".join(text), ln, self._end(j) or ln), j
        if rest[:1] in ("[", "{"):
            while not _balanced(rest) and i < self.n:
                rest += " " + _strip_comment(self.rows[i][1])
                i += 1
            return _flow(rest, ln, self._end(i) or ln), i
        if rest:
            while i < self.n and self.rows[i][0] > ind and not _split_key(self.rows[i][1]):  # plain multi-line scalar
                i += 1
            return Node("q", _unquote(rest), ln, ln), i
        if i < self.n and (self.rows[i][0] > ind or (self.rows[i][0] == ind and self.rows[i][1].startswith("- "))):
            node, j = self.block(i, depth + 1)
            return Node(node.kind, node.val, ln, node.end), j
        return Node("q", "", ln, ln), i

    def map(self, i: int, ind: int, depth: int) -> Tuple[Node, int]:
        ents, start = [], self.rows[i][2]
        while i < self.n and self.rows[i][0] == ind:
            c, ln = _strip_comment(self.rows[i][1]), self.rows[i][2]
            kv = _split_key(c)
            if kv is None:
                if c == "-" or c.startswith("- "):
                    break
                self.errors += 1
                i += 1
                continue
            node, i = self.value(kv[1], i + 1, ind, ln, depth)
            ents.append((kv[0], node))
        return Node("m", ents, start, self._end(i)), i

    def seq(self, i: int, ind: int, depth: int) -> Tuple[Node, int]:
        items, start = [], self.rows[i][2]
        while i < self.n and self.rows[i][0] == ind and (self.rows[i][1] == "-" or self.rows[i][1].startswith("- ")):
            c, ln = self.rows[i][1], self.rows[i][2]
            rest = c[1:].lstrip()
            col = ind + (len(c) - len(rest))
            c2 = _strip_comment(rest)
            if c2 and (c2[:2] == "- " or _split_key(c2)) and not _BLOCK.match(c2):
                self.rows[i] = [col, rest, ln]  # `- key: v` and `- - x`: the rest of the line is a block of its own
                node, i = self.block(i, depth + 1)
                items.append(Node(node.kind, node.val, ln, node.end))
            else:
                node, i = self.value(rest, i + 1, ind, ln, depth)
                items.append(node)
        return Node("s", items, start, self._end(i)), i


def parse(text: str) -> Tuple[List[Node], int]:
    text = text[1:] if text[:1] == "\ufeff" else text  # a BOM would glue itself to the first key
    segs: List[list] = [[]]
    bad = 0
    for n, raw in enumerate(text.split("\n"), 1):
        raw = raw.rstrip("\r")
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            bad += 1
        if _DOC.match(raw) or raw.startswith("..."):
            segs.append([])
            rest = raw[3:].strip()
            if raw.startswith("---") and rest and not rest.startswith("#"):
                segs[-1].append([0, rest, n])
            continue
        segs[-1].append([len(raw) - len(raw.lstrip(" ")), raw.strip(), n])
    docs: List[Node] = []
    for rows in segs:
        if not rows:
            continue
        p = _P(rows)
        node, _ = p.block(0)
        bad += p.errors
        if node.kind in ("m", "s") and node.val:
            docs.append(node)
    return docs, bad


# ------------------------------------------------------------------ readers
def get(node: Optional[Node], key: str) -> Optional[Node]:
    if node is not None and node.kind == "m":
        for k, v in node.val:
            if k == key:
                return v
    return None


def scalar(node: Optional[Node]) -> str:
    return node.val if node is not None and node.kind == "q" else ""  # type: ignore[return-value]


def items(node: Optional[Node]) -> List[Node]:
    """The elements of a sequence; a lone scalar or mapping counts as a one-element list, None as none."""
    if node is None:
        return []
    return list(node.val) if node.kind == "s" else [node]  # type: ignore[arg-type]


def walk(node: Node, skip: frozenset = frozenset()) -> Iterator[Tuple[str, Node]]:
    """Every mapping entry below `node` (depth first, document order); the children of a key in `skip` are not entered."""
    if node.kind == "m":
        for k, v in node.val:  # type: ignore[union-attr]
            yield k, v
            if k not in skip:
                yield from walk(v, skip)
    elif node.kind == "s":
        for v in node.val:  # type: ignore[union-attr]
            yield from walk(v, skip)


def scalars(node: Node) -> Iterator[Node]:
    """Every scalar below `node` (and the node itself when it is one)."""
    if node.kind == "q":
        yield node
    else:
        for v in node.val if node.kind == "s" else [v for _, v in node.val]:  # type: ignore[union-attr]
            yield from scalars(v)
