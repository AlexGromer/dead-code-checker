"""Test helper: the real tree-sitter grammars of the optional extra, or a reason to skip.

    available(grammar) -> str          "" when graph.ts can give the grammar, else why not (for pytest.skip / skipif)
    reason(grammar) -> str             the same, under the name some tests use
    backend(grammar, data)             a parse the ts_generic seam accepts (tg.set_backend(real_grammar.backend))
    use(*grammars)                     context manager: the seam serves only these grammars, from the real packages

Nothing here downloads: graph.ts asks the language pack only for a grammar that is already on disk.
"""

import contextlib
import sys

from dead_code_checker.graph import ts as tsload
from dead_code_checker.graph.extract import ts_generic as tg


def available(grammar: str) -> str:
    res = tsload.grammar_status(grammar)
    return "" if res.status == "ok" else f"real {grammar} grammar not usable: {res.detail}"


reason = available


def backend(grammar: str, data: bytes):
    parser = tsload.get_parser(grammar)
    language = parser.language
    mod = sys.modules[type(language).__module__]
    return tg._TsParsed(grammar, mod, language, parser.parse(data).root_node)


@contextlib.contextmanager
def use(*grammars):
    def fn(g, data):
        if g not in grammars:
            raise LookupError(f"grammar {g} is not served by the test helper")
        return backend(g, data)

    tg.set_backend(fn)
    try:
        yield
    finally:
        tg.set_backend(None)
