"""graph.store: the SQLite cache of the graph checker. A cache: it can be deleted at any moment, a cold start is correct, and
a file that is damaged, from another schema version or loose (group/other bits) is dropped and rebuilt with Store.reset_reason
set, so the caller can say so (never silent). Standard library only; ONE writer (the main thread): workers return data,
they never open the store.

    st = Store(layout.store)                             # <data root>/projects/<name>-<hash>/store.sqlite, see graph.paths
    with st.tx(): st.put_shards(shards); st.put_snapshot(tree_id, {path: shard_key})
    st.has_shards(keys) -> set            st.get_shard(key) -> Shard | None (payload_sha verified; a bad row is deleted)
    st.get_facts(keys) -> {key: Facts}    st.get_snapshot(tree_id) -> {path: shard_key}      st.gc(keep_trees) -> shards removed
    st.put_graph(graph) / st.get_graph() -> model.Graph    (replace-all in one transaction; ids follow the canonical order)
    st.put_engine_result(key, tool, payload) / get_engine_result(key) / engine_keys(prefix) / delete_engine_results(keys)
                                                           (the seam of the semantic and engine layers: opaque cached results)
    st.set_meta(k, v) / get_meta(k)

Security: directory 0700, file 0600, journal_mode TRUNCATE (WAL breaks on network and WSL file systems), busy_timeout 5 s;
a directory or file of another user, or a symlink in place of either, is refused (StoreError). Tables are (re)created when
meta.schema_version differs from SCHEMA_VERSION.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import zlib
from itertools import repeat
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set

from .model import Edge, Evidence, Facts, Graph, Node, Root, Shard, canonical, payload_sha

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE shard(key TEXT PRIMARY KEY, blob TEXT NOT NULL, lang TEXT NOT NULL, ext_ver TEXT NOT NULL,
                   facts BLOB NOT NULL, payload_sha TEXT NOT NULL);
CREATE TABLE snapshot(tree TEXT NOT NULL, path TEXT NOT NULL, shard_key TEXT NOT NULL, PRIMARY KEY(tree, path));
CREATE TABLE node(id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, lang TEXT NOT NULL, path TEXT NOT NULL,
                  line0 INT NOT NULL, line1 INT NOT NULL, pkg TEXT NOT NULL, vis TEXT NOT NULL, flags INT NOT NULL);
CREATE TABLE edge(src INT NOT NULL, dst INT NOT NULL, kind TEXT NOT NULL, tier TEXT NOT NULL, site TEXT NOT NULL, ev INT NOT NULL);
CREATE INDEX edge_src ON edge(src);
CREATE INDEX edge_dst ON edge(dst);
CREATE TABLE root(node TEXT NOT NULL, selector TEXT NOT NULL, kind TEXT NOT NULL, origin TEXT NOT NULL, strength TEXT NOT NULL,
                  trust TEXT NOT NULL, stub TEXT NOT NULL);
CREATE TABLE evid(id INTEGER PRIMARY KEY, source TEXT NOT NULL, file TEXT NOT NULL, line INT NOT NULL, snippet TEXT NOT NULL,
                  extractor TEXT NOT NULL);
CREATE TABLE engine_result(key TEXT PRIMARY KEY, tool TEXT NOT NULL, payload BLOB NOT NULL);
"""
_TABLES = ("meta", "shard", "snapshot", "node", "edge", "root", "evid", "engine_result")


class StoreError(Exception):
    pass


def _own(st: os.stat_result, what: str, path: str) -> None:
    if os.name != "nt" and st.st_uid != os.geteuid():
        raise StoreError(f"{what} {path} belongs to uid {st.st_uid}: a foreign cache is refused")


def _secure_dir(d: str) -> None:
    os.makedirs(os.path.dirname(d), mode=0o700, exist_ok=True)
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise StoreError(f"{d} is not a plain directory")
    _own(st, "cache directory", d)
    if os.name != "nt" and st.st_mode & 0o077:
        os.chmod(d, 0o700)


# ---------------------------------------------------------------- store
class Store:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.reset_reason = ""  # set when an unusable cache file was dropped and rebuilt
        _secure_dir(os.path.dirname(self.path))
        self._reason_for_file()
        try:
            self._conn = self._connect()
            self._ensure_schema()
        except sqlite3.DatabaseError as e:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._drop(f"damaged database ({e})")
            self._conn = self._connect()
            self._ensure_schema()

    # --- open / reset
    def _reason_for_file(self) -> None:
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(st.st_mode):
            raise StoreError(f"{self.path} is not a regular file")
        _own(st, "cache file", self.path)
        if os.name != "nt" and st.st_mode & 0o077:
            self._drop("loose file permissions: content cannot be trusted")

    def _drop(self, reason: str) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.path + suffix)
        self.reset_reason = reason

    def _connect(self) -> sqlite3.Connection:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.execute("PRAGMA journal_mode=TRUNCATE")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_schema(self) -> None:
        have = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        ver = None
        if "meta" in have:
            row = self._conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
            ver = row[0] if row else None
        if ver == str(SCHEMA_VERSION) and set(_TABLES) <= have:
            return
        if have and not self.reset_reason:
            self.reset_reason = f"schema version {ver} replaced by {SCHEMA_VERSION}"
        with self.tx():
            for t in sorted(have):
                if t.startswith("sqlite_"):
                    continue
                self._conn.execute(f'DROP TABLE IF EXISTS "{t}"')
            for stmt in filter(None, (s.strip() for s in _DDL.split(";"))):
                self._conn.execute(stmt)
            self._conn.execute("INSERT INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextlib.contextmanager
    def tx(self):
        """One write transaction (BEGIN IMMEDIATE); nested use joins the outer one."""
        if self._conn.in_transaction:
            yield
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def check(self) -> bool:
        """Full integrity check (import and doctor paths; not run on every open)."""
        return self._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    # --- meta
    def set_meta(self, key: str, value: str) -> None:
        with self.tx():
            self._conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    # --- shards
    @staticmethod
    def _shard_row(s: Shard):
        sha = s.sha or payload_sha(s.payload)
        if sha != payload_sha(s.payload):
            raise StoreError(f"shard {s.key[:12]}: payload does not match its sha")
        return (s.key, s.blob, s.lang, s.ext_ver, zlib.compress(s.payload, 6), sha)

    def put_shards(self, shards: Iterable[Shard], *, replace: bool = True) -> int:
        rows = [self._shard_row(s) for s in shards]
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        with self.tx():
            self._conn.executemany(f"{verb} INTO shard VALUES(?,?,?,?,?,?)", rows)
        return len(rows)

    def put_shard(self, shard: Shard) -> None:
        self.put_shards([shard])

    def get_shard(self, key: str) -> Optional[Shard]:
        """The shard, or None when absent or unreadable. A row whose payload no longer matches its sha is deleted: the cache
        heals itself instead of feeding corrupt facts to the linker."""
        row = self._conn.execute(
            "SELECT key,blob,lang,ext_ver,facts,payload_sha FROM shard WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        try:
            payload = zlib.decompress(row[4])
            ok = payload_sha(payload) == row[5]
            if ok:
                Facts.from_payload(payload)
        except (zlib.error, ValueError, KeyError, TypeError):
            ok = False
        if not ok:
            with self.tx():
                self._conn.execute("DELETE FROM shard WHERE key=?", (key,))
            return None
        return Shard(row[0], row[1], row[2], row[3], payload, row[5])

    def get_facts(self, keys: Iterable[str]) -> Dict[str, Facts]:
        """{key: Facts} of the shards that are present and intact (each payload is decoded ONCE; a row whose payload no longer
        matches its sha is deleted like get_shard does). The warm-cache path of the linker: the facts of every file."""
        keys, out, bad = sorted(set(keys)), {}, []
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for k, blob, sha in self._conn.execute(
                f"SELECT key,facts,payload_sha FROM shard WHERE key IN ({marks})", chunk
            ):
                try:
                    payload = zlib.decompress(blob)
                    if payload_sha(payload) != sha:
                        raise ValueError("payload sha")
                    out[k] = Facts.from_payload(payload)
                except (zlib.error, ValueError, KeyError, TypeError):
                    bad.append(k)
        if bad:
            with self.tx():
                self._conn.executemany("DELETE FROM shard WHERE key=?", [(k,) for k in bad])
        return out

    def has_shards(self, keys: Iterable[str]) -> Set[str]:
        keys = sorted(set(keys))
        found: Set[str] = set()
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            q = ",".join("?" * len(chunk))
            found.update(r[0] for r in self._conn.execute(f"SELECT key FROM shard WHERE key IN ({q})", chunk))
        return found

    def shard_count(self) -> int:
        return self._conn.execute("SELECT count(*) FROM shard").fetchone()[0]

    # --- snapshots
    def put_snapshot(self, tree: str, mapping: Mapping[str, str]) -> None:
        with self.tx():
            self._conn.execute("DELETE FROM snapshot WHERE tree=?", (tree,))
            self._conn.executemany(
                "INSERT INTO snapshot VALUES(?,?,?)", [(tree, p, k) for p, k in sorted(mapping.items())]
            )

    def get_snapshot(self, tree: str) -> Dict[str, str]:
        return dict(self._conn.execute("SELECT path, shard_key FROM snapshot WHERE tree=? ORDER BY path", (tree,)))

    def trees(self) -> List[str]:
        return [r[0] for r in self._conn.execute("SELECT DISTINCT tree FROM snapshot ORDER BY tree")]

    def gc(self, keep_trees: Sequence[str]) -> int:
        """Drop the snapshots of other trees and the shards no kept snapshot references; returns the shards removed."""
        keep = sorted(set(keep_trees))
        q = ",".join("?" * len(keep))
        with self.tx():
            self._conn.execute(
                f"DELETE FROM snapshot WHERE tree NOT IN ({q})" if keep else "DELETE FROM snapshot", keep
            )
            cur = self._conn.execute("DELETE FROM shard WHERE key NOT IN (SELECT shard_key FROM snapshot)")
        return cur.rowcount

    # --- graph
    def put_graph(self, graph: Graph, canonical_input: bool = False) -> None:
        """Replace the stored graph. canonical_input: `graph` came out of model.canonical already (a built graph): not sorted again."""
        g = graph if canonical_input else canonical(*graph)
        ids = {n.key: i for i, n in enumerate(g.nodes)}
        for e in g.edges:
            if e.src not in ids or e.dst not in ids:
                raise StoreError(f"edge {e.src} -> {e.dst} names a node that is not in the graph")
        with self.tx():
            for t in ("node", "edge", "root", "evid"):
                self._conn.execute(f"DELETE FROM {t}")
            self._conn.executemany(
                "INSERT INTO node VALUES(?,?,?,?,?,?,?,?,?,?)", [(i,) + tuple(n) for i, n in enumerate(g.nodes)]
            )
            self._conn.executemany(
                "INSERT INTO edge VALUES(?,?,?,?,?,?)",
                [(ids[e.src], ids[e.dst], e.kind, e.tier, e.site, e.ev) for e in g.edges],
            )
            self._conn.executemany("INSERT INTO root VALUES(?,?,?,?,?,?,?)", [tuple(r) for r in g.roots])
            self._conn.executemany(
                "INSERT INTO evid VALUES(?,?,?,?,?,?)", [(i,) + tuple(e) for i, e in enumerate(g.evid)]
            )

    def get_graph(self) -> Graph:
        c = self._conn
        nodes = tuple(
            Node(*r) for r in c.execute("SELECT key,kind,lang,path,line0,line1,pkg,vis,flags FROM node ORDER BY id")
        )
        keys = [n.key for n in nodes]
        rows = c.execute("SELECT src,dst,kind,tier,site,ev FROM edge ORDER BY rowid")
        edges = tuple(
            map(tuple.__new__, repeat(Edge), ((keys[s], keys[d], k, t, site, ev) for s, d, k, t, site, ev in rows))
        )
        roots = tuple(
            Root(*r) for r in c.execute("SELECT node,selector,kind,origin,strength,trust,stub FROM root ORDER BY rowid")
        )
        evid = tuple(Evidence(*r) for r in c.execute("SELECT source,file,line,snippet,extractor FROM evid ORDER BY id"))
        return Graph(nodes, edges, roots, evid)

    # --- engine results
    def put_engine_result(self, key: str, tool: str, payload: bytes) -> None:
        with self.tx():
            self._conn.execute(
                "INSERT OR REPLACE INTO engine_result VALUES(?,?,?)", (key, tool, sqlite3.Binary(payload))
            )

    def get_engine_result(self, key: str) -> Optional[bytes]:
        row = self._conn.execute("SELECT payload FROM engine_result WHERE key=?", (key,)).fetchone()
        return bytes(row[0]) if row else None

    def engine_keys(self, prefix: str) -> List[str]:
        q = "SELECT key FROM engine_result WHERE substr(key,1,?)=? ORDER BY key"
        return [k for (k,) in self._conn.execute(q, (len(prefix), prefix))]

    def delete_engine_results(self, keys: Iterable[str]) -> int:
        """The rows actually deleted (a key that has no row counts for nothing)."""
        with self.tx():
            return max(
                0, self._conn.executemany("DELETE FROM engine_result WHERE key=?", [(k,) for k in keys]).rowcount
            )
