"""Shared helpers of the graph tests: git fixtures and the toy language "dcref" with its root extractor "dcroots".

The extractors of the real languages arrive with the language packs; the graph core is tested with this tiny synthetic one, which
shows what a real extractor must return.

Language "dcref" (*.dcref), one statement per line, '#' starts a comment:
    def <Qual.name>[ <arity>][ private][ stub:<reason>]   a definition (class when the name starts upper-case, else func)
    ref <name>                                            a call from the last def (module level before any def)
    import <module> [name ...]                            an import
    dyn <pattern>                                         a dynamic site
    root <kind> <qual>                                    a root hint on a def of this file
Root extractor "dcroots" (*.dcroots): "<kind> <selector> [soft]" per line, e.g. "bin file:bin/x".
"""

import json
import subprocess
from pathlib import Path
from typing import List

from dead_code_checker.graph import contracts, langs
from dead_code_checker.graph.model import F_STUB, Def, DynSite, Facts, Import, Ref, RootHint


# ------------------------------------------------------------------ git
def git(cwd, *args, check=True):
    r = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True,
    )  # fmt: skip
    assert r.returncode == 0 or not check, r.stderr
    return r.stdout.strip()


def write(repo, rel, text, mode=None):
    p = Path(repo) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    if mode:
        p.chmod(mode)
    return p


def commit_all(r, msg="change"):
    git(r, "add", "-A")
    git(r, "commit", "-qm", msg)


def new_repo(root, files=None, name="repo", commit=True):
    """A git repository with one commit holding `files` {path: text} (commit=False: the repository has no commit yet)."""
    r = Path(root) / name
    r.mkdir()
    git(r, "init", "-q")
    for rel, text in (files or {}).items():
        write(r, rel, text)
    if commit:
        commit_all(r, "base")
    return r


# ------------------------------------------------------------------ the toy language
class RefExtractor(contracts.Extractor):
    name, version, langs = "ref", 1, ("dcref",)

    def extract(self, lang: str, path: str, text: str) -> Facts:
        defs, refs, imports, roots, dyn, errors, scope = [], [], [], [], [], 0, ""
        for n, raw in enumerate(text.splitlines(), 1):
            toks = raw.split("#", 1)[0].split()
            if not toks:
                continue
            op, args = toks[0], toks[1:]
            if op == "def" and args:
                arity, vis, stub = -1, "public", ""
                for a in args[1:]:
                    if a.isdigit():
                        arity = int(a)
                    elif a == "private":
                        vis = "private"
                    elif a.startswith("stub:"):
                        stub = a[5:]
                scope = args[0]
                kind = "class" if scope[:1].isupper() else ("method" if "." in scope else "func")
                defs.append(Def(scope, kind, n, n, vis, F_STUB if stub else 0, arity, stub))
            elif op == "ref" and args:
                refs.append(Ref(args[0], "call", n, scope))
            elif op == "import" and args:
                imports.append(Import(args[0], tuple(args[1:]), "", n, 0, scope))
            elif op == "dyn" and args:
                dyn.append(DynSite(args[0], n, scope))
            elif op == "root" and len(args) == 2:
                roots.append(RootHint(args[0], args[1], n))
            else:
                errors += 1
        return Facts(lang, tuple(defs), tuple(refs), tuple(imports), tuple(roots), tuple(dyn), errors)


class RefRoots(contracts.RootExtractor):
    name, version = "dcroots", 1

    def matches(self, path: str) -> bool:
        return path.endswith(".dcroots")

    def extract(self, path: str, text: str, cfg) -> List[RootHint]:
        out = []
        for n, raw in enumerate(text.splitlines(), 1):
            toks = raw.split("#", 1)[0].split()
            if len(toks) >= 2:
                out.append(RootHint(toks[0], toks[1], n, "soft" if toks[2:] == ["soft"] else "hard"))
        return out


DCREF_LANG = {"role": "code", "family": None, "grammar": None, "extractor": None, "ext": [".dcref"]}


def install_dcref(monkeypatch, tmp_path):
    """Register the toy extractors and give langs.load() a table that knows the language: the production table plus dcref."""
    table = json.loads(Path(langs.DATA).read_text(encoding="utf-8"))
    table["langs"]["dcref"] = DCREF_LANG
    p = Path(tmp_path) / "langs-with-dcref.json"
    p.write_text(json.dumps(table), encoding="utf-8")
    monkeypatch.setattr(langs, "DATA", str(p))
    monkeypatch.setitem(contracts._EXTRACTORS, "dcref", RefExtractor())  # both are undone at the end of the test
    monkeypatch.setitem(contracts._ROOTS, "dcroots", RefRoots())


def inline_units(monkeypatch) -> None:
    """Run every par.run_units pool of the test in this process (threads): a test that monkeypatches the code under test must not
    depend on the start method of a process pool (a spawned or forkserver worker is a fresh interpreter that does not see the patch)."""
    from dead_code_checker.graph import par

    real = par.run_units

    def run(units, fn, **kw):
        kw["kind"] = "thread"
        return real(units, fn, **kw)

    monkeypatch.setattr(par, "run_units", run)
