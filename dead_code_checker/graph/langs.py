"""graph.langs: file -> language, and the project profile (languages, mode). The tables live in data/langs.json.

Order of the checks in Langs.detect(path, head): exact file name (Dockerfile, Makefile ...; a name that content decides, `nginx.conf` (a
template of a chart is no nginx file), is one of `ambiguous_names`), then the extension (the longest
suffix first, case-insensitive: ".d.ts" is typescript through ".ts"), where an ambiguous extension (".h") is decided by
content markers in `head`, then, for a name without a known extension, the shebang line of `head`, and last the directory
(`in_dir` of langs.json: a file below templates/ that has no language or is plain data is a Helm template). head = the first
bytes of the file (str or bytes) or None; without it only the name decides.

    L = langs.load(); L.detect("a/b.py") -> "python"; L.detect("bin/x", b"#!/usr/bin/env python3\\n") -> "python"
    L.info("kotlin") -> {"role": "code", "family": "kotlin", "grammar": "kotlin", "extractor": "ts_generic", ...}
    L.script_lang("app/build.gradle.kts") -> "kotlin"   (an extension in the script_ext of its language, or a path that its entry_re matches: an entry point, graph.roots)
    langs.profile(paths, head=reader, mode="closed_world") -> Profile(languages, counts, mode)

A lang name is the node-key prefix; `family` is the key the engine router groups languages by.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from functools import lru_cache
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple, Union

from .model import MODES

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "langs.json")
HEAD_BYTES = 4096
_VERSION_TAIL = re.compile(r"[\d.]+$")


def _content_rules(rules: List[dict]) -> List[Tuple[Optional["re.Pattern"], str]]:
    """[(content regex or None = the default, lang)] of an ambiguous extension or name (the first hit wins)."""
    return [(re.compile(r["re"], re.M) if r["re"] else None, r["lang"]) for r in rules]


class Langs:
    def __init__(self, data: dict):
        self.langs: Dict[str, dict] = dict(data["langs"])
        self.query_alias: Dict[str, str] = dict(
            data.get("query_alias", {})
        )  # grammar -> the grammar whose queries it shares
        self.ext: Dict[str, str] = {}
        self.names: Dict[str, str] = {}
        self.shebang: Dict[str, str] = {}
        self.dirs: Dict[str, str] = {}
        for lang in sorted(self.langs):
            spec = self.langs[lang]
            if spec.get("in_dir"):
                self.dirs[spec["in_dir"]] = lang
            for e in spec.get("ext", ()):
                self.ext[e.lower()] = lang
            for n in spec.get("filenames", ()):
                self.names[n] = lang
            for s in spec.get("shebang", ()):
                self.shebang[s] = lang
        self.ambiguous: Dict[str, List[Tuple[Optional["re.Pattern"], str]]] = {
            e.lower(): _content_rules(rules) for e, rules in data.get("ambiguous", {}).items()
        }
        self.ambiguous_names = {n: _content_rules(rules) for n, rules in data.get("ambiguous_names", {}).items()}
        self._exts = sorted(
            {e for e in list(self.ext) + list(self.ambiguous)}, key=lambda e: (-e.count("."), -len(e), e)
        )

    # --- lookups
    def info(self, lang: str) -> dict:
        return self.langs[lang]

    def family(self, lang: str) -> Optional[str]:
        return self.langs[lang].get("family")

    def grammar(self, lang: str) -> Optional[str]:
        return self.langs[lang].get("grammar")

    def extractor(self, lang: str) -> Optional[str]:
        return self.langs[lang].get("extractor")

    def role(self, lang: str) -> str:
        return self.langs[lang].get("role", "data")

    def analyzable(self, lang: str) -> bool:
        """The language is read by an extractor or by the root extractors, i.e. it takes part in the graph."""
        return self.role(lang) in ("code", "config")

    # --- detection
    def shebang_lang(self, head: Union[str, bytes, None]) -> Optional[str]:
        if head is None:
            return None
        line = (head[:256].decode("latin-1") if isinstance(head, bytes) else head[:256]).split("\n", 1)[0]
        if not line.startswith("#!"):
            return None
        toks = line[2:].split()
        if not toks:
            return None
        name = posixpath.basename(toks[0].replace("\\", "/"))
        if name == "env":
            rest = [t for t in toks[1:] if not t.startswith("-") and "=" not in t]
            if not rest:
                return None
            name = posixpath.basename(rest[0])
        return self.shebang.get(name) or self.shebang.get(_VERSION_TAIL.sub("", name))

    def _ext(self, base: str) -> Optional[str]:
        low = base.lower()
        return next((e for e in self._exts if low.endswith(e) and len(low) > len(e)), None)

    def needs_head(self, path: str) -> bool:
        """True when the name alone does not decide: an unknown name (shebang) or an ambiguous extension (markers)."""
        base = posixpath.basename(path.replace("\\", "/"))
        e = self._ext(base)
        return base in self.ambiguous_names or (base not in self.names and (e is None or e in self.ambiguous))

    def script_lang(self, path: str) -> Optional[str]:
        """The language of a file that its tool runs as a script (langs.json script_ext: build.gradle.kts) or that a server or framework runs
        or serves by convention (entry_re: index.php, flake.nix, a .html page), None for any other."""
        lang = self.detect(path)
        if not lang:
            return None
        spec, p = self.langs[lang], path.replace("\\", "/")
        by_ext = self._ext(posixpath.basename(p)) in spec.get("script_ext", ())
        return lang if by_ext or any(re.search(rx, p) for rx in spec.get("entry_re", ())) else None

    def detect(self, path: str, head: Union[str, bytes, None] = None) -> Optional[str]:
        lang = self._detect(path, head)
        if self.dirs and (lang is None or self.role(lang) == "data"):
            lang = next((self.dirs[d] for d in path.replace("\\", "/").split("/")[:-1] if d in self.dirs), lang)
        return lang

    def _detect(self, path: str, head: Union[str, bytes, None]) -> Optional[str]:
        base = posixpath.basename(path.replace("\\", "/"))
        rules = self.ambiguous_names.get(base)
        if rules is None:
            if base in self.names:
                return self.names[base]
            e = self._ext(base)
            if e is None:
                return self.shebang_lang(head)
            rules = self.ambiguous.get(e)
            if rules is None:
                return self.ext[e]
        text = (
            ""
            if head is None
            else (head[:HEAD_BYTES].decode("utf-8", "replace") if isinstance(head, bytes) else head[:HEAD_BYTES])
        )
        return next(lang for rx, lang in rules if rx is None or rx.search(text))


class Profile(NamedTuple):
    languages: Tuple[str, ...]  # analyzable languages present, most files first, then by name
    counts: Dict[str, int]  # lang -> files, every detected language
    mode: str  # closed_world | library


@lru_cache(maxsize=4)
def _load(path: str) -> Langs:
    with open(path, encoding="utf-8") as fh:
        return Langs(json.load(fh))


def load(path: Optional[str] = None) -> Langs:
    return _load(path or DATA)


def detect(path: str, head: Union[str, bytes, None] = None) -> Optional[str]:
    return load().detect(path, head)


def profile(
    paths: Iterable[str],
    head: Optional[Callable[[str], Union[str, bytes, None]]] = None,
    mode: str = "closed_world",
    langs: Optional[Langs] = None,
) -> Profile:
    """The project profile. `head(path)` supplies the first bytes for files whose name alone does not decide (called only
    for those: a name without a known extension, or an ambiguous one); mode comes from the configuration."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}, not {mode!r}")
    lg = langs or load()
    counts: Dict[str, int] = {}
    for p in sorted(set(paths)):
        lang = lg.detect(p, head(p) if head and lg.needs_head(p) else None)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    langs_present = sorted((k for k in counts if lg.analyzable(k)), key=lambda k: (-counts[k], k))
    return Profile(tuple(langs_present), dict(sorted(counts.items())), mode)
