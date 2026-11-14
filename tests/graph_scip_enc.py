"""A SCIP index writer for the tests of graph/sem.py: the protobuf wire format, the other way round.

The committed tests/fixtures/sem/<lang>/index.scip files are the real output of the installed indexers (scip-python,
scip-typescript, scip-go) and pin the reader on what the tools write. A rule of the overlay (coverage 0.99, a `local` symbol, an
index that resolves the same name twice, a hostile file) needs an index no indexer will produce on request, so it is written
here: index([doc(path, [occ(...)], [info(...)])], tool="scip-fake"). The field numbers are those of scip.proto (the
@scip-code/scip bindings of the installed scip-typescript carry them): Index metadata 1, documents 2, external_symbols 3;
Document relative_path 1, occurrences 2, symbols 3, language 4; Occurrence range 1, symbol 2, symbol_roles 3,
single_line_range 8, multi_line_range 9; SymbolInformation symbol 1, relationships 4, kind 5, display_name 6.
"""

DEFINITION, IMPORT, WRITE, READ = 1, 2, 4, 8
STYLES = ("packed", "unpacked", "single", "multi")  # the range encodings occ() can write


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field(num: int, wt: int, payload: bytes) -> bytes:
    return varint(num << 3 | wt) + payload


def num(n: int, value: int) -> bytes:
    return field(n, 0, varint(value))


def blob(n: int, data: bytes) -> bytes:
    return field(n, 2, varint(len(data)) + data)


def text(n: int, value: str) -> bytes:
    return blob(n, value.encode())


def occ(line: int, col: int, symbol: str, roles: int = READ, style: str = "packed") -> bytes:
    """style: packed (the legacy `range`, one length-delimited field), unpacked (one varint field each), single (the typed
    SingleLineRange, line 0 omitted like protobuf does), multi (MultiLineRange ending on the next line)."""
    if style == "packed":
        rng = blob(1, varint(line) + varint(col) + varint(col + 1))
    elif style == "unpacked":
        rng = num(1, line) + num(1, col) + num(1, col + 1)
    elif style == "single":
        rng = blob(8, (num(1, line) if line else b"") + num(2, col) + num(3, col + 1))
    elif style == "multi":
        rng = blob(9, (num(1, line) if line else b"") + num(2, col) + num(3, line + 1) + num(4, 1))
    else:
        raise ValueError(style)
    return rng + text(2, symbol) + (num(3, roles) if roles else b"")


def rel(symbol: str, implementation: bool = True) -> bytes:
    return text(1, symbol) + (num(3, 1) if implementation else num(2, 1))


def info(symbol: str, kind: int = 0, name: str = "", rels=()) -> bytes:
    out = text(1, symbol) + (num(5, kind) if kind else b"") + (text(6, name) if name else b"")
    return out + b"".join(blob(4, r) for r in rels)


def doc(path: str, occs=(), infos=(), language: str = "") -> bytes:
    out = text(1, path) + (text(4, language) if language else b"")
    return out + b"".join(blob(2, o) for o in occs) + b"".join(blob(3, i) for i in infos)


def index(docs=(), tool: str = "scip-fake", version: str = "1.0", root: str = "file:///fixture", external=()) -> bytes:
    meta = blob(2, text(1, tool) + text(2, version)) + text(3, root)
    return blob(1, meta) + b"".join(blob(2, d) for d in docs) + b"".join(blob(3, e) for e in external)


def sym(path: str, descs: str, pkg: str = "fake 1.0") -> str:
    """A global symbol of the fake scheme: sym("m", "Foo#bar().") -> 'scip-fake py fake 1.0 m/Foo#bar().' style."""
    return f"scip-fake py {pkg} {path}/{descs}"
