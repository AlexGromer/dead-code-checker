"""graph.extract.batch: extract many files in parallel (the unit is a chunk of files).

extract_many(items, workers=None, chunk=64, have=()) -> (shards, missing)
    items    [(lang, path, blob, text)]; two paths with one (lang, blob) are one unit, the first path in sorted order is
             the one the extractor sees (its result does not depend on the path anyway, graph.contracts)
    have     shard keys already in the store: not extracted again (a warm cache costs no parse)
    shards   [Shard] sorted by key: the same list for 1, 2 or 8 workers (par.run_units orders the chunks by a key)
    missing  {lang: reason} for each language whose extractor cannot run (ExtractorMissing): the caller turns it into a
             finding; nothing is extracted for those languages and nothing falls back.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from .. import par
from ..contracts import ExtractorMissing, get_extractor, make_shard
from ..model import Shard, shard_key


def _unit(chunk) -> list:
    """Worker: [("ok", Shard) | ("missing", lang, reason)] for a chunk of (lang, path, blob, text)."""
    out = []
    for lang, path, blob, text in chunk:
        try:
            out.append(("ok", make_shard(get_extractor(lang), lang, path, blob, text)))
        except ExtractorMissing as exc:
            out.append(("missing", lang, exc.reason))
    return out


def extract_many(
    items: Iterable[Tuple[str, str, str, str]], workers: Optional[int] = None, chunk: int = 64, have: Iterable[str] = ()
) -> Tuple[List[Shard], Dict[str, str]]:
    have, uniq, missing = set(have), {}, {}
    for lang, path, blob, text in sorted(items, key=lambda i: (i[1], i[0], i[2])):
        uniq.setdefault((lang, blob), (lang, path, blob, text))
    todo = []
    for (lang, blob), item in sorted(uniq.items()):
        if lang in missing:
            continue
        try:
            ex = get_extractor(lang)
        except ExtractorMissing as exc:
            missing[lang] = exc.reason
            continue
        if shard_key(blob, lang, ex.ext_ver, ex.grammar_sha(lang)) not in have:
            todo.append(item)
    chunks = [todo[i : i + max(1, chunk)] for i in range(0, len(todo), max(1, chunk))]
    shards: Dict[str, Shard] = {}
    for res in par.run_units(chunks, _unit, jobs=workers, key=lambda c: (c[0][0], c[0][2])):
        for r in res:
            if r[0] == "ok":
                shards[r[1].key] = r[1]
            else:
                missing.setdefault(r[1], r[2])
    return [shards[k] for k in sorted(shards)], dict(sorted(missing.items()))
