"""graph.ts: the tree-sitter loader of the extractors. The two packages it needs are an optional dependency.

    pip install 'dead-code-checker[treesitter]'      # tree-sitter, tree-sitter-language-pack

The python, powershell and yaml extractors, the config readers without a grammar (cfgconf) and the root sources need neither. A
language that is read through a grammar and finds no tree-sitter, or no grammar, is not extracted: the caller gets a finding
that says why (W dc.degraded-extractor, see graph.extract.ts_generic.missing_finding), never a crash and never a guess.

    get_parser(grammar) -> tree_sitter.Parser     LookupError (with the reason) when it cannot be had; one parser per thread
    grammar_status(grammar) -> Res                Res("ok", "") or Res("missing", reason)
    grammar_id() -> str                           the installed versions, part of the shard key of every tree-sitter extractor
    reset()                                       forget what is cached in the process (tests)

Grammars come from tree-sitter-language-pack, by the grammar name of data/langs.json. What this module checks against the sources of
tree_sitter 0.26.1 and tree_sitter_language_pack 1.25.0: `get_language(name)` returns a `tree_sitter.Language`, `has_language(name)`
tells a name the pack does not know, `downloaded_languages()` lists the grammars already on disk and `configure(PackConfig(cache_dir=))`
moves its cache. That pack version fetches a grammar that is not on disk from the network when `get_language` asks for it, so this loader
asks only for a grammar the pack lists as downloaded and treats any other as missing, with the command that fetches it. A pack that has
no `downloaded_languages()` cannot tell what is on disk, so every grammar of it is missing unless DEAD_CODE_CHECKER_TS_DOWNLOAD=1: the
no-network default does not depend on the version of the pack.

    DEAD_CODE_CHECKER_TS_CACHE     the base directory of the pack's cache (`configure(cache_dir=...)`)
    DEAD_CODE_CHECKER_TS_DOWNLOAD  1: let the pack fetch a missing grammar by itself (the one network access of an analysis; off by default)
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import threading
from typing import Dict, NamedTuple, Optional

ENV_CACHE = "DEAD_CODE_CHECKER_TS_CACHE"
ENV_DOWNLOAD = "DEAD_CODE_CHECKER_TS_DOWNLOAD"
EXTRA = "pip install 'dead-code-checker[treesitter]'"
PACKAGES = ("tree-sitter", "tree-sitter-language-pack")  # the distribution names, as pip knows them

_lock = threading.RLock()
_local = threading.local()  # a Parser is not shared between threads
_configured: list = []  # [cache_dir] once the pack has been configured


class Res(NamedTuple):
    status: str  # "ok" | "missing"
    detail: str


def reset() -> None:
    """Forget the cached parsers and the configuration (tests; after an install)."""
    with _lock:
        _configured.clear()
    _local.parsers = {}


def _modules():
    """(tree_sitter, tree_sitter_language_pack), configured; LookupError says which package is not installed."""
    try:
        ts = importlib.import_module("tree_sitter")
    except ImportError as exc:
        raise LookupError(f"tree-sitter is not installed ({EXTRA})") from exc
    try:
        pack = importlib.import_module("tree_sitter_language_pack")
    except ImportError as exc:
        raise LookupError(f"tree-sitter-language-pack is not installed ({EXTRA})") from exc
    if not (hasattr(ts, "Query") and hasattr(ts, "QueryCursor") and hasattr(ts, "Parser")):
        raise LookupError(f"the installed tree-sitter has no Query/QueryCursor (0.25 or newer is required: {EXTRA})")
    cache = os.environ.get(ENV_CACHE)
    with _lock:
        if cache and _configured != [cache] and hasattr(pack, "configure") and hasattr(pack, "PackConfig"):
            try:
                pack.configure(pack.PackConfig(cache_dir=cache))
            except Exception as exc:  # noqa: BLE001 - the pack's own errors: a cache it cannot use is a missing grammar
                raise LookupError(f"tree-sitter-language-pack cannot use the cache {cache}: {exc}") from exc
            _configured[:] = [cache]
    return ts, pack


def _allowed(pack, grammar: str) -> Optional[str]:
    """None when the pack may be asked for `grammar`, else the reason it may not."""
    has = getattr(pack, "has_language", None)
    if has is not None and not has(grammar):
        return f"the grammar {grammar} is not known to tree-sitter-language-pack"
    if os.environ.get(ENV_DOWNLOAD) == "1":
        return None
    listed = getattr(pack, "downloaded_languages", None)
    if listed is None:
        return (
            f"the grammar {grammar} cannot be checked: this tree-sitter-language-pack has no downloaded_languages() and "
            f"get_language would fetch it over the network (upgrade the pack, or set {ENV_DOWNLOAD}=1 to allow that)"
        )
    if grammar not in set(listed()):
        return (
            f"the grammar {grammar} is not on disk; fetch it with python3 -c "
            f"\"import tree_sitter_language_pack as p; p.download(['{grammar}'])\" "
            f"(or set {ENV_DOWNLOAD}=1 to let a run fetch it)"
        )
    return None


def grammar_status(grammar: str) -> Res:
    try:
        _, pack = _modules()
    except LookupError as exc:
        return Res("missing", str(exc))
    why = _allowed(pack, grammar)
    return Res("missing", why) if why else Res("ok", "")


def get_parser(grammar: str):
    """A tree_sitter.Parser of `grammar`, cached per thread. LookupError (with the reason) when tree-sitter, the pack or the grammar
    is not there; a failure is remembered for the thread, so a file after a file does not ask again."""
    cache = getattr(_local, "parsers", None)
    if cache is None:
        cache = _local.parsers = {}
    if grammar in cache:
        if isinstance(cache[grammar], str):
            raise LookupError(cache[grammar])
        return cache[grammar]
    try:
        ts, pack = _modules()
        why = _allowed(pack, grammar)
        if why:
            raise LookupError(why)
        try:
            parser = ts.Parser(pack.get_language(grammar))
        except LookupError:
            raise
        except Exception as exc:  # noqa: BLE001 - LanguageNotFoundError, DownloadError, a library that does not load ...
            raise LookupError(f"the grammar {grammar} cannot be loaded: {exc}") from exc
    except LookupError as exc:
        cache[grammar] = str(exc)
        raise
    cache[grammar] = parser
    return parser


def _version(dist: str) -> str:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return ""


def grammar_id() -> str:
    """The versions of tree-sitter and of the pack ('' when either is not installed): a grammar of another version can give other
    facts, so this is part of the shard key (the extractors put it into grammar_sha)."""
    versions: Dict[str, str] = {d: _version(d) for d in PACKAGES}
    return "" if not all(versions.values()) else "|".join(f"{d}=={v}" for d, v in sorted(versions.items()))
