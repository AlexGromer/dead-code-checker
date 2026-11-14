"""graph.contracts: the abstract interfaces that extractors and root extractors implement, and their registry.

Extractor (one per language family; modules in graph/extract/, named by data/langs.json)
    class MyEx(Extractor):
        name, version, langs = "py", 1, ("python",)
        def extract(self, lang, path, text) -> Facts: ...
    Contract: the Facts are a function of (lang, text) only. `path` is for diagnostics, never for the result, because a shard
    is keyed by the blob sha and shared by every path with that content; path-derived attributes (test, generated, vendor)
    are added by link and roots. A syntax error is Facts.errors > 0 plus as many facts as could be read, never an exception;
    an extractor that cannot run (missing grammar) raises ExtractorMissing: the caller reports it, nothing falls back
    silently. The output is sorted by Facts.to_payload, so the order of emission does not matter, but extraction must not
    depend on a dict or set order, a clock or the process either. Bump `version` whenever the output can change; a
    tree-sitter extractor returns the grammar's so_sha256 from grammar_sha(lang) (it is part of the shard key). `supports`
    says whether this extractor has rules for the language yet, `probe` whether it can run now (None = yes, else the finding).

RootExtractor (extract(path, text, cfg) -> [RootHint])
    class MyRoots(RootExtractor):
        name, version = "systemd", 1
        def matches(self, path) -> bool: ...        # cheap, by name only
        def extract(self, path, text, cfg) -> List[RootHint]: ...
    Cached as a VIRTUAL shard (lang "root:<name>") keyed by the blob sha, so a changed unit file invalidates only the roots.
    Like an extractor it reads only `text` (cfg carries path maps and the like and is part of nobody's key: a result must
    not depend on it beyond what the caller passes again) and executes nothing.

    register_extractor(ex) / get_extractor(lang) / defined(lang);  register_roots(rx) / root_extractors()
    make_shard(ex, lang, path, blob, text) / make_root_shard(rx, path, blob, text, cfg)  -> model.Shard (ready for the store)

The registry is the seam where extractors plug in: a module under graph/extract/ that calls register_extractor at import is
found through the `extractor` name of its language in data/langs.json; a test or a plugin may register one directly.
"""

from __future__ import annotations

import abc
import importlib
import importlib.util
from typing import Dict, List, Optional, Tuple

from . import langs as _langs
from .model import Facts, Finding, RootHint, Shard, new_shard


class ExtractorMissing(Exception):
    """No usable extractor for a language (module absent, grammar not installed ...): the caller reports `reason`."""

    def __init__(self, lang: str, reason: str):
        super().__init__(f"{lang}: {reason}")
        self.lang, self.reason = lang, reason


class Extractor(abc.ABC):
    name: str = ""
    version: int = 1
    langs: Tuple[str, ...] = ()

    @property
    def ext_ver(self) -> str:
        return f"{self.name}@{self.version}"

    def grammar_sha(self, lang: str) -> str:
        """so_sha256 of the grammar in use ('' for extractors without one); part of the shard key."""
        return ""

    def supports(self, lang: str) -> bool:
        """False while this extractor has no rules for `lang` (the language is then reported as not supported yet)."""
        return True

    def probe(self, lang: str, cfg: Optional[dict] = None) -> Optional[Finding]:
        """None when the extractor can run for `lang` now, else the finding that says why not (a grammar that is not installed)."""
        return None

    @abc.abstractmethod
    def extract(self, lang: str, path: str, text: str) -> Facts:
        """Facts of one file; see the module docstring for the contract."""


class RootExtractor(abc.ABC):
    name: str = ""
    version: int = 1

    @property
    def ext_ver(self) -> str:
        return f"{self.name}@{self.version}"

    @abc.abstractmethod
    def matches(self, path: str) -> bool:
        """Cheap name-based test: does this extractor read `path`?"""

    @abc.abstractmethod
    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        """Root hints of one file."""


_EXTRACTORS: Dict[str, Extractor] = {}
_ROOTS: Dict[str, RootExtractor] = {}


def register_extractor(ex: Extractor) -> Extractor:
    if not ex.name or not ex.langs:
        raise ValueError("an extractor needs a name and langs")
    for lang in ex.langs:
        _EXTRACTORS[lang] = ex
    return ex


def register_roots(rx: RootExtractor) -> RootExtractor:
    if not rx.name:
        raise ValueError("a root extractor needs a name")
    _ROOTS[rx.name] = rx
    return rx


def reset_registry() -> None:
    """Tests: forget every registered extractor and root extractor."""
    _EXTRACTORS.clear()
    _ROOTS.clear()


def _module_of(lang: str, lg: "_langs.Langs") -> str:
    mod = lg.extractor(lang) if lang in lg.langs else None
    return f"{__package__}.extract.{mod}" if mod else ""


def defined(lang: str, langs: Optional["_langs.Langs"] = None) -> bool:
    """Is there an extractor for `lang` at all: one registered, or the module that data/langs.json names exists. A language
    that has none is not supported yet (a graph without it), which is a different thing from an extractor that cannot run.
    """
    if lang in _EXTRACTORS:
        return True
    name = _module_of(lang, langs or _langs.load())
    return bool(name) and importlib.util.find_spec(name) is not None


def get_extractor(lang: str, langs: Optional["_langs.Langs"] = None) -> Extractor:
    """The extractor of `lang`: registered, else the module data/langs.json names is imported (it registers itself).
    ExtractorMissing says why there is none; there is no regex fallback here."""
    ex = _EXTRACTORS.get(lang)
    if ex is not None:
        return ex
    lg = langs or _langs.load()
    if lang not in lg.langs:
        raise ExtractorMissing(lang, "unknown language")
    name = _module_of(lang, lg)
    if not name:
        raise ExtractorMissing(lang, "no extractor is defined for this language (read by the root extractors only)")
    try:
        m = importlib.import_module(name)
        if lang not in _EXTRACTORS:  # imported before a reset_registry(): run its registration again
            importlib.reload(m)
    except ImportError as e:
        raise ExtractorMissing(lang, f"extractor module {name} cannot be imported: {e}") from e
    ex = _EXTRACTORS.get(lang)
    if ex is None:
        raise ExtractorMissing(lang, f"module {name} did not register an extractor for {lang}")
    return ex


def root_extractors() -> List[RootExtractor]:
    """Registered root extractors, sorted by name (deterministic order)."""
    return [_ROOTS[k] for k in sorted(_ROOTS)]


def make_shard(ex: Extractor, lang: str, path: str, blob: str, text: str) -> Shard:
    try:
        facts = ex.extract(lang, path, text)
    except RecursionError:  # a hostile deeply nested file is one file error (the contract: never an exception), not a lost run
        facts = Facts(lang, errors=1)
    return new_shard(blob, lang, ex.ext_ver, facts, ex.grammar_sha(lang))


def make_root_shard(rx: RootExtractor, path: str, blob: str, text: str, cfg=None) -> Shard:
    hints = tuple(rx.extract(path, text, cfg))
    return new_shard(blob, f"root:{rx.name}", rx.ext_ver, Facts(f"root:{rx.name}", roots=hints), "")
