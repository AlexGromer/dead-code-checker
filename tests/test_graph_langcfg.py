"""The config and infrastructure languages: the grammars of the language table, the "known language, no graph"
group (json toml ini properties xml proto graphql sql postgres promql ssh_config caddy css cmake), make / just / dockerfile
(recipes and stages), the template layers (gotmpl in the Helm layer, jinja2 and Ansible), nginx and jsonnet (include / import
edges), terraform on the hcl layer.

Tests that need a REAL grammar parse through tests/real_grammar.py (graph.ts) and skip with the reason when tree-sitter or the
grammar is not installed. Everything else (language table, YAML side of Ansible and CI, the fallback of the Helm layer, no-fallback
on a missing grammar) needs no grammar. Expected facts and verdicts of the fixtures: tests/fixtures/langcfg/expected/<case>.json
(regenerate with REGEN_FIXTURES=1 on the WHOLE file after a deliberate change and READ the diff).

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_langcfg.py
"""

import hashlib
import importlib
import importlib.util
import json
import os
import shutil
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test

ROOT = Path(__file__).resolve().parent.parent
import real_grammar as unlocked  # noqa: E402
from dead_code_checker.graph import (  # noqa: E402
    classreg,
    contracts,
    langs as dclangs,
    levels,
    link as dlink,
    model as md,
    reach as rc,
    roots as rt,
    ts as tsm,
)
from dead_code_checker.graph.extract import cfg, ts_generic as tg  # noqa: E402


FIX = ROOT / "tests" / "fixtures" / "langcfg"
CFG_FIX = ROOT / "tests" / "fixtures" / "cfg"
NEW_GRAMMARS = (
    "terraform json jsonnet promql gotmpl jinja2 nginx caddy dockerfile ini sql postgres make just cmake starlark xml properties "
    "groovy toml proto graphql ssh_config lua nix ruby php scala html css vue svelte"
).split()
NO_GRAPH = "json toml ini properties xml postgres promql ssh_config caddy css cmake".split()
SCHEMA = "proto graphql sql".split()  # data languages that the schema extractor reads (tests/test_graph_schema.py)


RECORDED = (
    FIX / "recorded.json"
)  # {"<grammar>:<sha256 of the source>": {"e": errors, "m": {kind: [[pattern, {capture: [[sb, eb, sr, sc, er, ec]]}]]}}}
REGEN = bool(os.environ.get("REGEN_FIXTURES"))
_recorded: dict = {}


class Node(NamedTuple):
    start_byte: int
    end_byte: int
    start_point: tuple
    end_point: tuple


class Replay:
    """A recorded parse: what ts_generic's backend seam needs (.errors and .run(kind))."""

    def __init__(self, rec):
        self.errors, self.rec = rec["e"], rec["m"]

    def run(self, kind):
        return [(pi, {c: [self.node(n) for n in ns] for c, ns in caps.items()}) for pi, caps in self.rec.get(kind, ())]

    @staticmethod
    def node(n):
        return Node(n[0], n[1], tuple(n[2:4]), tuple(n[4:6]))


def flat(n):
    return [n.start_byte, n.end_byte, *n.start_point, *n.end_point]


def snapshot(p):
    """The record of a real parse: the matches of every query kind, as plain numbers."""
    m = {}
    for kind in tg.QUERY_KINDS:
        got = [[pi, {c: [flat(n) for n in ns] for c, ns in sorted(caps.items())}] for pi, caps in p.run(kind)]
        if got:
            m[kind] = got
    return {"e": int(p.errors), "m": m}


def table():
    if not _recorded and RECORDED.exists() and not REGEN:
        _recorded.update(json.loads(RECORDED.read_text()))
    return _recorded


def parse_backend(grammar, data):
    """The real grammar when it is usable (the record is checked against it, or written on REGEN_FIXTURES=1), else the record."""
    key = f"{grammar}:{hashlib.sha256(data).hexdigest()[:24]}"
    why = unlocked.available(grammar)
    if why:
        if key not in table():
            pytest.skip(f"{why}; no recorded parse of this input either")
        return Replay(table()[key])
    p = unlocked.backend(grammar, data)
    snap = snapshot(p)
    if REGEN:
        _recorded[key] = snap
    else:
        assert (
            table().get(key) == snap
        ), f"the recorded parse of a {grammar} input is missing or stale (REGEN_FIXTURES=1)"
    return p


@pytest.fixture(autouse=True)
def roots_registered():
    """other test files call contracts.reset_registry(): in one pytest process the root extractors of graph.roots must
    be registered again (importing graph.roots registered them once, at the first import)."""
    if "settings" not in {r.name for r in contracts.root_extractors()}:
        importlib.reload(rt)


@pytest.fixture(scope="session", autouse=True)
def write_recording():
    yield
    if REGEN and _recorded:
        RECORDED.write_text(json.dumps(_recorded, sort_keys=True, separators=(",", ":")) + "\n")  # a whole-file run


@pytest.fixture
def grammar():
    """grammar(*names): parse with the real grammars, or replay the recorded parses when they are not usable on this host."""

    def use(*names):
        tg.set_backend(parse_backend)

    yield use
    tg.set_backend(None)


def extract(lang, text, path="x"):
    return contracts.get_extractor(lang).extract(lang, path, text)


def defs(f):
    return {d.qual: d.kind for d in f.defs if d.qual}


def refs(f):
    return {(r.name, r.kind, r.scope) for r in f.refs}


def roots(f):
    return {(r.kind, r.target, r.strength) for r in f.roots}


# ================================================================ the case driver: facts and verdicts of a fixture directory
def run_case(name, drop=()):
    """{"facts": {path: summary}, "verdicts": {node: verdict}} of the fixture tree FIX/name (the roots layer included)."""
    base, lg = FIX / name, dclangs.load()
    facts, hints, summary = {}, {}, {}
    for f in sorted(base.rglob("*")):
        rel = f.relative_to(base).as_posix()
        if not f.is_file() or rel in drop:
            continue
        text = f.read_text()
        lang = lg.detect(rel, text[:4096].encode())
        if lang and lg.role(lang) != "data" and lg.info(lang).get("extractor"):
            facts[rel] = contracts.get_extractor(lang).extract(lang, rel, text)
            summary[rel] = {
                "defs": sorted([d.qual, d.kind, d.line0, d.line1] for d in facts[rel].defs if d.qual),
                "refs": sorted([r.name, r.kind, r.scope] for r in facts[rel].refs),
                "dyn": sorted([d.pattern, d.scope] for d in facts[rel].dyn),
                "roots": sorted([r.kind, r.target, r.strength] for r in facts[rel].roots),
                "errors": facts[rel].errors,
            }
        else:
            facts[rel] = md.Facts(lang or "file")
        hints[rel] = rt.extract_hints(rel, text)
    res = dlink.link(facts, hints, workers=1)
    assert not res.findings, res.findings
    return {"facts": summary, "verdicts": dict(sorted(rc.analyze(res.graph).verdicts().items()))}


def expect(name, got):
    path = FIX / "expected" / f"{name}.json"
    if os.environ.get("REGEN_FIXTURES"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    assert got == json.loads(path.read_text()), f"{path} differs (REGEN_FIXTURES=1 rewrites it)"


def dead(got):
    return sorted(k for k, v in got["verdicts"].items() if v in levels.DEAD)


# ================================================================ registry, lock, language table (no grammar needed)
def test_every_grammar_of_a_tree_sitter_extractor_has_queries_and_the_new_grammars_are_all_known():
    lg = dclangs.load()
    grams = set()
    for lang, spec in lg.langs.items():
        if spec.get("extractor") in tg.GRAMMAR_EXTRACTORS and spec.get("grammar") and not spec.get("native"):
            assert tg._queries(spec["grammar"]), lang  # queries exist (a query_alias grammar uses those of the named one)
            grams.add(spec["grammar"])
    assert {"terraform", "gotmpl"} <= {g for g in NEW_GRAMMARS}
    for g in ("jsonnet", "jinja2", "nginx", "dockerfile", "make", "just", "groovy", "lua", "nix", "ruby", "php", "scala", "html", "vue", "svelte", "sql", "proto", "graphql"):
        assert g in grams, g


@pytest.mark.parametrize("lang", NO_GRAPH)
def test_known_languages_without_a_graph_have_role_family_and_no_extractor(lang):
    lg = dclangs.load()
    spec = lg.info(lang)
    assert spec["role"] == "data" and spec["family"] and spec["grammar"] and not spec["extractor"]
    assert not lg.analyzable(lang)  # profile and the graph leave them out: no dc.unsupported-language


def test_files_of_the_no_graph_languages_are_detected_and_cause_no_finding(tmp_path, monkeypatch):
    lg, seen = dclangs.load(), {}
    for f in sorted((FIX / "data").iterdir()):
        seen[f.name] = lg.detect(f.name)
    assert seen == {
        "Caddyfile": "caddy",
        "CMakeLists.txt": "cmake",
        "a.css": "css",
        "a.ini": "ini",
        "a.json": "json",
        "a.xml": "xml",
        "app.properties": "properties",
        "m.proto": "proto",
        "pyproject.toml": "toml",
        "q.pgsql": "postgres",
        "q.sql": "sql",
        "r.promql": "promql",
        "s.graphql": "graphql",
        "ssh_config": "ssh_config",
    }
    monkeypatch.setenv("DEAD_CODE_CHECKER_HOME", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    for f in (FIX / "data").iterdir():
        if lg.detect(f.name) not in SCHEMA:  # the schema languages have a graph now
            (repo / f.name).write_text(f.read_text())
    b = levels.build(str(repo), None)  # a plain directory is a tree too (vcs.snapshot walks it)
    assert b.unsupported == () and b.missing == {} and b.findings == () and b.langs == {}


def test_conf_is_nginx_only_when_it_reads_like_nginx():
    lg = dclangs.load()
    assert lg.detect("a/nginx.conf") == "nginx" and lg.detect("site.conf", "server {\n  listen 80;\n}\n") == "nginx"
    assert lg.needs_head("a/nginx.conf") and lg.detect("a/nginx.conf", "user x;\n") == "nginx"  # the name decides
    assert lg.detect("x.conf", "events { worker_connections 1; }\n") == "nginx"
    assert lg.detect("y.conf", "[main]\nk=v\n") == "ini" and lg.detect("z.conf") == "ini"
    assert (
        lg.detect("t.conf", "server {\n  listen {{ .Values.port }};\n}\n") == "ini"
    )  # a template is data (or a Helm template)
    assert lg.detect("charts/a/templates/n.conf", "k=v\n") == "helm-template"


def test_a_helm_template_named_nginx_conf_stays_a_helm_template():
    """The name nginx.conf is no nginx file when it holds a template: below templates/ helm renders it (its .Values reads count)."""
    lg, body = dclangs.load(), "listen {{ .Values.port }};\n"
    assert lg.detect("chart/templates/nginx.conf", body) == "helm-template"
    assert lg.detect("chart/templates/site.conf", body) == "helm-template"
    assert lg.detect("conf/nginx.conf", body) == "ini"  # outside templates/: plain data, as before
    assert ("values:port", "reads", "") in refs(extract("helm-template", body, "chart/templates/nginx.conf"))


def test_languages_of_the_config_layers():
    lg = dclangs.load()
    names = {
        "Makefile": "make",
        "GNUmakefile": "make",
        "a.mk": "make",
        "justfile": "just",
        "x.just": "just",
        "Dockerfile": "dockerfile",
        "a.jsonnet": "jsonnet",
        "a.libsonnet": "libsonnet",
        "t.j2": "jinja2",
        "a.yml.j2": "jinja2",
        "main.tf": "hcl",
    }
    assert {k: lg.detect(k) for k in names} == names
    for lang in ("make", "just", "dockerfile", "yaml", "helm-values", "helm-template", "shell"):
        assert lg.info(lang)["link_group"] == "cfg"  # `make test` of a CI line or a script reaches the recipe `test`
    assert (
        lg.info("terraform" if "terraform" in lg.langs else "hcl")["grammar"] == "hcl"
        and lg.query_alias["terraform"] == "hcl"
    )


# ================================================================ no fallback: a grammar that is not usable is ExtractorMissing
@pytest.mark.parametrize(
    "lang,text",
    [
        ("make", "a:\n"),
        ("just", "a:\n"),
        ("dockerfile", "FROM x\n"),
        ("nginx", "user x;\n"),
        ("jsonnet", "{}"),
        ("libsonnet", "{}"),
        ("jinja2", "{% include 'a' %}"),
    ],
)
def test_a_grammar_that_is_not_usable_is_extractor_missing_never_a_fallback(lang, text):
    def refuse(grammar, data):
        raise LookupError(f"grammar {grammar} is missing")

    tg.set_backend(refuse)
    try:
        with pytest.raises(contracts.ExtractorMissing) as exc:
            extract(lang, text)
    finally:
        tg.set_backend(None)
    assert "is missing" in exc.value.reason


def test_the_probe_reports_a_missing_grammar_as_a_warning(monkeypatch):
    monkeypatch.setattr(tsm, "grammar_status", lambda g: tsm.Res("missing", f"the grammar {g} is not on disk"))
    for lang in ("make", "just", "dockerfile", "nginx", "jsonnet", "libsonnet", "jinja2"):
        f = tg.missing_finding(lang, "x")
        assert f is not None and (f.level, f.code) == ("W", "dc.degraded-extractor"), lang
        assert contracts.get_extractor(lang).probe(lang).code == "dc.degraded-extractor", lang
    assert tg.missing_finding("json", "x") is None  # a language without an extractor is not probed
    assert tg.missing_finding("yaml", "x") is None  # read without a grammar


def test_the_shard_key_depends_on_the_grammar_and_the_mapping_code():
    shas = {
        lang: contracts.get_extractor(lang).grammar_sha(lang)
        for lang in ("make", "just", "dockerfile", "nginx", "jsonnet", "jinja2", "yaml", "hcl")
    }
    assert len(set(shas.values())) == len(shas) and all(len(v) == 64 for v in shas.values())
    assert contracts.get_extractor("make").grammar_sha("make") == shas["make"]


# ================================================================ CI and shell lines name recipes and stages (no grammar needed)
@pytest.mark.parametrize(
    "line,want",
    [
        ("make test lint && echo done", ["recipe:test", "recipe:lint"]),
        ("  $(MAKE) -C sub all CC=gcc", ["recipe:all"]),
        ("sudo just build", ["recipe:build"]),
        ("cd x; make -j4 -f a.mk deploy | tee log", ["recipe:deploy"]),
        ("make --file=b.mk check", ["recipe:check"]),
        ("echo make is nice", []),
        ("pip install make", []),
        ("make", []),
        ("docker build --target Dev -t x .", ["stage:dev"]),
        ("docker buildx build --target=release .", ["stage:release"]),
        ("cargo build --target x86_64-unknown-linux-gnu", []),  # only an image builder names a stage
        ("make -j all", ["recipe:all"]),  # -j takes a value only when a number follows
        ("make -j 4 all", ["recipe:all"]),
        ("make -d all", ["recipe:all"]),  # make -d is the debug flag, just -d the working directory
        ("just -d sub all", ["recipe:all"]),
        ("CC=gcc make all", ["recipe:all"]),
        ("@make x", ["recipe:x"]),
        ("bash -c 'make hc && make hd'", ["recipe:hc", "recipe:hd"]),
        ("make $(MAKEFLAGS) all", ["recipe:all"]),  # an option variable is no recipe
        ("make $(SUBS)", ["recipe:*"]),  # a computed word is a glob: the caller makes a dynamic site of it
        ("make a-$(X) $@ $1", ["recipe:a-*", "recipe:*"]),
        ("echo hi\nmake x\n  make -C d \\\n  y", ["recipe:x", "recipe:y"]),  # a script: one command line per line
        ("just {{ target }}", ["recipe:*"]),
        ("make ${{ matrix.target }}", ["recipe:*"]),
        ("make _TPL_", ["recipe:*"]),
        ("docker build --target $STAGE .", ["stage:*"]),
        ("make a.b/c", ["recipe:a%2Eb%2Fc"]),  # the name is escaped like the quals of the extractors
    ],
)
def test_build_tool_lines_name_recipes_and_stages(line, want):
    assert rt.build_targets(line) == want


def test_a_ci_line_reaches_the_recipe_and_a_compose_target_the_stage():
    wf = "on: push\njobs:\n  b:\n    steps:\n      - run: make -C sub package test\n      - run: docker build --target test .\n"
    f = extract("yaml", wf)
    assert {
        ("recipe:package", "ref", "job:b"),
        ("recipe:test", "ref", "job:b"),
        ("stage:test", "ref", "job:b"),
    } <= refs(f)
    c = extract("yaml", "services:\n  t:\n    build:\n      context: .\n      target: Test\n")
    assert ("stage:test", "ref", "") in refs(c)


# ================================================================ Ansible (the YAML side needs no grammar)
def test_ansible_tasks_templates_and_roles_are_path_edges():
    f = extract("yaml", (FIX / "ansible" / "site.yml").read_text(), "site.yml")
    inc = {n for n, k, _ in refs(f) if k == "include"}
    assert inc == {
        "vars/main.yml",
        "tasks/extra.yml",
        "app.conf.j2",
        "files/motd",
        "other.yml",
        "roles/common/tasks/main.yml",
        "roles/web/tasks/main.yml",
    }
    o = extract("yaml", (FIX / "ansible" / "other.yml").read_text())
    assert {n for n, k, _ in refs(o) if k == "include"} == {"roles/web/tasks/main.yml"}
    t = extract("yaml", (FIX / "ansible" / "roles/web/tasks/main.yml").read_text())
    assert {n for n, k, _ in refs(t) if k == "include"} == {"*.conf.j2"}  # a computed name matches every file it can


def test_ansible_keys_are_read_only_in_a_file_no_other_layer_owns():
    k8s = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\ndata:\n  include: x.yml\n  template: {src: y.j2}\n"
    gha = "on: push\njobs:\n  a:\n    steps:\n      - include_tasks: z.yml\n"
    for text in (k8s, gha):
        assert not {n for n, k, _ in refs(extract("yaml", text)) if k == "include"}


# ================================================================ make
def test_make_recipes_prerequisites_includes_and_roots(grammar):
    grammar("make")
    f = extract("make", (FIX / "make" / "Makefile").read_text(), "Makefile")
    assert f.errors == 0
    assert set(defs(f)) == {
        f"recipe:{n}" for n in "all build gen test lint docs deploy package cleanup-old out".split()
    } | {"recipe:cleanup-old"}
    assert set(defs(f).values()) == {
        "job"
    }  # a recipe is never reported by the diff checks (kinds func / method / class)
    r = refs(f)
    assert ("recipe:gen", "ref", "recipe:build") in r and (
        "recipe:out",
        "ref",
        "recipe:build",
    ) in r  # normal and order-only
    assert ("recipe:check", "ref", "recipe:build") in r  # $(MAKE) -C sub check
    assert ("recipe:package", "ref", "recipe:deploy") in r
    assert {"config.mk", "local/*.mk"} == {n for n, k, _ in r if k == "include"}
    assert not any(
        n.startswith("recipe:.") or "%" in n for n, _, _ in r
    )  # .PHONY lists and pattern rules are no references
    assert {t for _, t, _ in roots(f)} == {
        "recipe:all",
        "recipe:test",
        "recipe:lint",
        "recipe:docs",
        "recipe:build",
    }  # goal, phony, conventional


def test_make_special_targets_pattern_rules_and_variables_are_no_recipes(grammar):
    grammar("make")
    f = extract(
        "make", ".SUFFIXES: .c .o\n%.o: %.c\n\t$(CC) $<\n$(BIN): main.o\n\t$(LD)\nreal: other\n\t@echo\nother:\n"
    )
    assert set(defs(f)) == {"recipe:real", "recipe:other"}
    assert ("recipe:other", "ref", "recipe:real") in refs(f)


def test_make_goal_variable_wins_over_the_first_target(grammar):
    grammar("make")
    f = extract("make", ".DEFAULT_GOAL := app\nfirst:\n\t@echo\napp:\n\t@echo\n")
    assert {t for _, t, _ in roots(f)} == {"recipe:app"}


def test_make_conditional_blocks_and_recipe_lines_keep_their_scope(grammar):
    grammar("make")
    f = extract("make", "a:\n\t$(MAKE) b\nifeq ($(X),1)\nt1:\n\t$(MAKE) c\nendif\n")
    r = refs(f)
    assert ("recipe:b", "ref", "recipe:a") in r and ("recipe:c", "ref", "recipe:t1") in r


def test_make_fixture_facts_and_verdicts(grammar):
    grammar("make")
    got = run_case("make")
    expect("make", got)
    assert dead(got) == [
        "make:Makefile#recipe:cleanup-old"
    ]  # package, gen, out are prerequisites; deploy is named by a CI line
    without_ci = run_case("make", drop={".github/workflows/ci.yml"})
    assert dead(without_ci) == [
        "make:Makefile#recipe:cleanup-old",
        "make:Makefile#recipe:deploy",
        "make:Makefile#recipe:package",
    ]


def test_a_makefile_with_variables_only_is_read_from_outside(grammar):
    grammar("make")
    f = extract("make", "CC := gcc\ninclude other.mk\n")
    assert roots(f) == {("init", "", "soft")} and ("other.mk", "include", "") in refs(f)


# ================================================================ just
def test_just_recipes_dependencies_aliases_and_imports(grammar):
    grammar("just")
    f = extract("just", (FIX / "just" / "justfile").read_text(), "justfile")
    assert f.errors == 0 and set(defs(f)) == {
        f"recipe:{n}" for n in "default build test hidden-helper _prepare _unused".split()
    }
    r = refs(f)
    assert ("recipe:_prepare", "ref", "recipe:build") in r and (
        "recipe:build",
        "ref",
        "recipe:test",
    ) in r  # `(build "y")`
    assert ("recipe:test", "ref", "recipe:build") in r  # `just test` in a recipe line
    assert ("recipe:build", "ref", "") in r  # alias b := build
    assert {"common.just", "ops", "ops/justfile", "ops.just"} <= {n for n, k, _ in r if k == "include"}
    assert {t for _, t, _ in roots(f)} == {
        "recipe:default",
        "recipe:build",
        "recipe:test",
    }  # public: no `_`, not [private]


def test_just_fixture_verdicts(grammar):
    grammar("just")
    got = run_case("just")
    expect("just", got)
    assert dead(got) == ["just:justfile#recipe:_unused", "just:justfile#recipe:hidden-helper"]


# ================================================================ dockerfile
def test_dockerfile_stages_and_their_references(grammar):
    grammar("dockerfile")
    f = extract("dockerfile", (FIX / "docker" / "Dockerfile").read_text(), "Dockerfile")
    assert f.errors == 0 and defs(f) == {
        "stage:builder": "resource",
        "stage:test": "resource",
        "stage:unused-tools": "resource",
        "stage:3": "resource",
    }
    r = refs(f)
    assert ("stage:builder", "ref", "stage:test") in r  # FROM builder AS test (the alias `Builder` is case-insensitive)
    assert ("stage:builder", "ref", "stage:unused-tools") in r  # COPY --from=0 (the index of a stage)
    assert ("stage:builder", "ref", "stage:3") in r  # COPY --from=builder
    assert ("stage:3", "ref", "") in r  # docker build builds the last stage
    assert not any(
        n in ("stage:vendor", "stage:assets") for n, _, _ in r
    )  # images / contexts that are no stage of this file
    assert roots(f) == set()  # an unreferenced Dockerfile is dead: what it RUNS is dc.rootsrc


def test_dockerfile_fixture_verdicts(grammar):
    grammar("dockerfile")
    got = run_case("docker")
    expect("docker", got)
    assert dead(got) == ["dockerfile:Dockerfile#stage:unused-tools", "file:Dockerfile.old"]
    alone = run_case("docker", drop={"docker-compose.yml"})
    assert set(dead(alone)) >= {"dockerfile:Dockerfile", "dockerfile:Dockerfile#stage:3"}  # nothing builds it


# ================================================================ templates: gotmpl in the Helm layer
HELM = ("templates/_helpers.tpl", "templates/configmap.yaml")


def helm_facts(rel):
    return extract("yaml", (FIX / "helm" / rel).read_text(), rel)


def no_grammar(grammar, data):
    raise LookupError(f"grammar {grammar} is not locked")


def test_the_helm_layer_reads_the_gotmpl_tree_and_agrees_with_the_action_scan(grammar):
    tg.set_backend(no_grammar)
    plain = {rel: helm_facts(rel) for rel in HELM}  # no grammar: the scan of the {{ }} actions
    grammar("gotmpl")
    for rel in HELM:
        assert helm_facts(rel) == plain[rel], rel


def test_the_helm_tree_path_is_taken_when_the_grammar_parses_and_not_otherwise(grammar, monkeypatch):
    grammar("gotmpl")
    used, tree = [], cfg._helm_tree
    monkeypatch.setattr(cfg, "_helm_tree", lambda *a: (used.append(1), tree(*a))[1])
    helm_facts("templates/_helpers.tpl")
    assert used == [1]
    used.clear()
    broken = '{{ define "a" }}{{ include "b" . }}'  # never closed: the scan stands in
    f = extract("yaml", broken)
    assert not used and ("tpl:b", "ref", "tpl:a") in refs(f) and "tpl:a" in defs(f)


def test_helm_nesting_scopes_and_spans_from_the_tree(grammar):
    grammar("gotmpl")
    f = helm_facts("templates/_helpers.tpl")
    d = {d.qual: (d.line0, d.line1) for d in f.defs if d.qual}
    assert d == {"tpl:app%2Ename": (4, 6), "tpl:app%2Elabels": (8, 15), "tpl:app%2Eunused": (17, 19)}
    assert ("tpl:app%2Ename", "ref", "tpl:app%2Elabels") in refs(f)  # inside a define, a with and a range
    assert not any(q.startswith("tpl:ghost") for q in defs(f))  # `{{ define "ghost" }}` in a comment defines nothing


def test_helm_block_is_a_definition_and_a_call_from_the_outer_scope(grammar):
    grammar("gotmpl")
    f = extract("yaml", '{{ define "outer" }}{{ block "inner" . }}x{{ end }}{{ template "t" . }}{{ end }}\n')
    assert {"tpl:outer", "tpl:inner"} <= set(defs(f))
    r = refs(f)
    assert ("tpl:inner", "ref", "tpl:outer") in r and ("tpl:t", "ref", "tpl:outer") in r


@pytest.mark.parametrize("use_tree", [False, True])
def test_helm_tpl_may_read_any_value_and_variables_keep_their_paths(grammar, use_tree):
    if use_tree:
        grammar("gotmpl")
    else:
        tg.set_backend(no_grammar)
    f = helm_facts("templates/configmap.yaml")
    r = refs(f)
    assert ("values:*", "reads", "") in r  # tpl .Values.motd: the rendered string may read any value
    assert {("values:settings", "reads", ""), ("values:debug.enabled", "reads", "")} <= r
    plain = extract("yaml", "a: {{ .Values.a.b }}\n")
    assert ("values:*", "reads", "") not in refs(plain) and ("values:a.b", "reads", "") in refs(plain)


def test_helm_fixture_verdicts(grammar):
    grammar("gotmpl")
    got = run_case("helm")
    expect("helm", got)
    assert dead(got) == [
        "yaml:templates/_helpers.tpl#tpl:app%2Eunused"
    ]  # tpl reads every value: values:neverRead stays alive


# ================================================================ jinja2
def test_jinja2_macros_calls_imports_and_paths(grammar):
    grammar("jinja2")
    f = extract("jinja2", (FIX / "ansible" / "templates" / "app.conf.j2").read_text(), "app.conf.j2")
    assert f.errors <= 1 and defs(f) == {"macro:row": "template", "macro:unused_macro": "template"}
    r = refs(f)
    assert {n for n, k, _ in r if k == "include"} == {"base.j2", "macros.j2", "part1.j2", "part2.j2"}
    assert ("macro:cell", "ref", "macro:row") in r  # m.cell(x) inside the macro row, `m` is a module alias
    assert ("macro:row", "ref", "") in r and ("macro:field", "ref", "") in r  # `field as f`: the macro field
    assert roots(f) == {("template", "", "soft")}  # another engine renders it


def test_jinja2_comments_and_builtin_calls_are_no_references(grammar):
    grammar("jinja2")
    f = extract(
        "jinja2",
        '{# {{ ghost() }} #}{% for i in range(3) %}{{ loop.cycle("a") }}{% endfor %}{% call(x) box() %}{% endcall %}\n',
    )
    assert {n for n, _, _ in refs(f)} == {"macro:box"}


def test_ansible_fixture_facts_and_verdicts(grammar):
    grammar("jinja2")
    got = run_case("ansible")
    expect("ansible", got)
    assert dead(got) == ["jinja2:templates/app.conf.j2#macro:unused_macro", "jinja2:templates/macros.j2#macro:lonely"]


# ================================================================ nginx and jsonnet
def test_nginx_include_and_code_file_directives_only(grammar):
    grammar("nginx")
    f = extract("nginx", (FIX / "nginx" / "nginx.conf").read_text(), "nginx.conf")
    inc = {n for n, k, _ in refs(f) if k == "include"}
    assert inc == {"mime.types", "conf.d/*.conf", "modules/ngx_http_js_module.so", "snippets/proxy.conf"}
    assert not any(
        x in " ".join(inc) for x in ("/var/www", "/etc/ssl", "/srv/static")
    )  # root, ssl_certificate, alias are no code
    assert roots(f) == {("init", "", "soft")}  # the server reads the file


def test_nginx_fixture_verdicts(grammar):
    grammar("nginx")
    got = run_case("nginx")
    expect("nginx", got)
    assert dead(got) == []


def test_jsonnet_imports_and_the_root_of_a_program_not_of_a_library(grammar):
    grammar("jsonnet")
    main = extract("jsonnet", (FIX / "jsonnet" / "main.jsonnet").read_text(), "main.jsonnet")
    assert {n for n, k, _ in refs(main) if k == "include"} == {"lib/util.libsonnet", "notes.txt", "data.json"}
    assert roots(main) == {("init", "", "soft")}
    lib = extract("libsonnet", "local inner = import 'inner.libsonnet';\n{ x: inner.y }\n")
    assert {n for n, k, _ in refs(lib) if k == "include"} == {"inner.libsonnet"} and roots(lib) == set()


def test_jsonnet_fixture_verdicts(grammar):
    grammar("jsonnet")
    got = run_case("jsonnet")
    expect("jsonnet", got)
    assert dead(got) == ["libsonnet:lib/orphan.libsonnet"]


# ================================================================ terraform = the hcl layer
def test_terraform_grammar_gives_the_facts_of_the_hcl_grammar_and_the_hcl_queries_compile_on_it(grammar):
    grammar("hcl", "terraform")
    for f in sorted((CFG_FIX / "tf").iterdir()):
        text, out = f.read_text(), {}
        for g in ("hcl", "terraform"):
            tg.set_backend(lambda gr, data, g=g: parse_backend(g, data))
            out[g] = extract("hcl", text, f.name)
        assert out["hcl"] == out["terraform"] and out["hcl"].errors == 0, f.name
        assert defs(out["hcl"]) or f.suffix == ".tfvars", f.name


# ================================================================ who names a recipe: docs, scripts, Dockerfiles, computed words
def dyn(f):
    return {(d.pattern, d.scope) for d in f.dyn}


def test_make_computed_words_are_dynamic_sites_not_skipped_words(grammar):
    grammar("make")
    f = extract("make", "all: build $(SUBS) a-$(X) $(wildcard *.c) %.o\n\t$(MAKE) $(T) $(MAKEFLAGS)\nbuild:\n\t@echo\n")
    assert dyn(f) == {
        ("name:recipe:*", "recipe:all"),
        ("name:recipe:a-*", "recipe:all"),
    }  # not $(wildcard), %.o, $(MAKEFLAGS)
    assert ("recipe:build", "ref", "recipe:all") in refs(f)
    assert dyn(extract("make", "$(BIN): $(OBJS)\n\t@echo\n")) == {
        ("name:recipe:*", "")
    }  # the rule has no named target: the file
    assert not extract("make", ".SUFFIXES: $(X)\n.PHONY: $(Y) z\nz:\n\t@echo\n").dyn  # a special target reads no recipe


def test_just_recipe_line_with_an_interpolation_is_a_dynamic_site(grammar):
    grammar("just")
    f = extract("just", "default:\n    just {{ target }}\n    @make -C x all\n")
    assert dyn(f) == {("name:recipe:*", "recipe:default")} and ("recipe:all", "ref", "recipe:default") in refs(f)


def test_a_shell_script_names_recipes_and_a_computed_one_is_a_dynamic_site(grammar):
    grammar("bash")
    f = extract("shell", "#!/bin/sh\nbuild() {\n  make -C sub via-sh\n}\nmake $T\nbash -c 'just lint'\n", "x.sh")
    assert {("recipe:via-sh", "ref", "build"), ("recipe:lint", "ref", "")} <= refs(f)
    assert dyn(f) == {("name:recipe:*", "")}
    assert not any(
        n.startswith("recipe:") for n, _, _ in refs(extract("shell", "#!/bin/sh\necho make is fun\n", "y.sh"))
    )


def test_docs_and_dockerfiles_root_the_recipes_they_name():
    md_text = "Build: `make build`, `just -d sub test`, `make a-$(X)`.\n\n```sh\n$ make -j4 a b\nmake $(T)\n```\n"
    md_text += "\n```\nmake plain\nls -l bin/not-a-command-in-a-plain-fence\n```\n"
    got = {h.target for h in rt.extract_hints("README.md", md_text) if h.target.startswith(classreg.BUILD_SCHEME)}
    assert got == {f"build:recipe:{n}" for n in ("build", "test", "a", "b", "plain")}  # a computed word is no root
    assert not [
        h for h in rt.extract_hints("README.md", md_text) if "not-a-command" in h.target
    ]  # a plain fence: recipes only
    assert {h.kind for h in rt.extract_hints("README.md", md_text) if h.target.startswith("build:")} == {"doc-cmd"}
    docker = 'FROM x\nRUN make a && \\\n  make b\nCMD ["make", "serve"]\nHEALTHCHECK CMD make hc\nRUN <<EOF\nmake inside\nEOF\n'
    hints = rt.extract_hints("Dockerfile", docker)
    built = [h for h in hints if h.target.startswith("build:")]
    assert {h.target for h in built} == {f"build:recipe:{n}" for n in ("a", "b", "serve", "hc", "inside")}
    assert {(h.strength, h.trust) for h in built} == {("soft", "convention")}


def test_a_build_selector_root_is_bound_to_the_nodes_of_that_qual_and_a_miss_is_silent():
    one = md.Facts("make", defs=(md.Def("recipe:x", "job", 1, 2, "package", 0, -1, ""),))
    hints = {"README.md": [md.RootHint("doc-cmd", f"build:recipe:{n}", 1, "soft", "documented") for n in ("x", "none")]}
    rs = rt.resolve(hints, {"Makefile": "100644", "README.md": "100644"}, lambda p: None)
    assert sorted(r.selector for r in rs.roots) == ["build:recipe:none", "build:recipe:x"] and not rs.findings
    graph = dlink.link({"Makefile": one}, workers=1).graph
    bound = classreg.bind(graph.nodes, rs.roots)  # what levels.build does after the link
    assert {(r.node, r.strength, r.kind) for r in bound} == {  # the recipe and its file, as a root of a definition does
        ("make:Makefile#recipe:x", "soft", "doc-cmd"),
        ("make:Makefile", "soft", "doc-cmd"),
    }
    direct = dlink.link({"Makefile": one}, hints, workers=1)  # the hints path of the linker agrees
    assert direct.findings == () and {r.node for r in direct.graph.roots} == {r.node for r in bound}


def test_recipes_named_by_docs_scripts_and_dockerfiles_are_not_dead(grammar):
    grammar("make", "bash", "dockerfile")
    got = run_case("refs")
    expect("refs", got)
    assert [k for k in dead(got) if not k.startswith("markdown:")] == [
        "make:Makefile#recipe:orphan"
    ]  # (the case gives a README a node)
    v = got["verdicts"]
    assert v["make:Makefile#recipe:via-doc"] == "doc-only" and v["make:Makefile#recipe:via-sh"] == "weak-alive"
    assert v["make:Makefile#recipe:via-docker"] == "weak-alive" and v["make:Makefile#recipe:via-fence"] == "doc-only"
    no_script = run_case("refs", drop={"scripts/ci.sh"})  # without the script that CI runs
    assert "make:Makefile#recipe:via-sh" in dead(no_script)


def test_levels_build_roots_the_recipes_of_docs_and_dockerfiles_through_the_selectors(grammar, tmp_path, monkeypatch):
    grammar("make", "bash", "dockerfile")
    monkeypatch.setenv("DEAD_CODE_CHECKER_HOME", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    shutil.copytree(FIX / "refs", repo)
    b = levels.build(str(repo), None)
    verdicts = rc.analyze(b.graph).verdicts()
    assert sorted(k for k, v in verdicts.items() if v in levels.DEAD) == ["make:Makefile#recipe:orphan"]
    assert b.findings == () and b.missing == {}  # `make nothing-like-it` of the README is no dangling root


def test_computed_recipe_words_keep_recipes_alive_in_a_makefile_and_a_justfile(grammar):
    grammar("make", "just")
    for case in ("dyn_make", "dyn_just"):
        got = run_case(case)
        expect(case, got)
        assert dead(got) == [], case


def test_the_shard_keys_cover_the_helpers_and_the_data_the_facts_are_made_of(tmp_path, monkeypatch):
    before = {lang: contracts.get_extractor(lang).grammar_sha(lang) for lang in ("make", "just", "nginx", "jinja2")}
    alt = tmp_path / "cfg.py"  # cfgts and tpl call cfg._Out, cfg._path, cfg._q ...: an edit of cfg.py is a new key
    alt.write_text(Path(cfg.__file__).read_text() + "\n# edited\n")
    monkeypatch.setattr(cfg, "__file__", str(alt))
    for lang, sha in before.items():
        assert contracts.get_extractor(lang).grammar_sha(lang) != sha, lang
    monkeypatch.undo()
    xl, fw = tg.xl, rt.fw()  # the build tool data decides the facts of a shell script (xlang) and of every command line
    sha, saved = xl.sha(), fw["build_tools"]
    fw["build_tools"] = {**saved, "stage_tools": [*saved["stage_tools"], "kaniko"]}
    xl.sha.cache_clear()
    try:
        assert xl.sha() != sha
    finally:
        fw["build_tools"] = saved
        xl.sha.cache_clear()
    assert xl.sha() == sha


# ================================================================ a .j2 file below a chart's templates/ keeps its Helm reads
def test_a_j2_template_of_a_chart_keeps_its_values_reads(grammar):
    grammar("jinja2")
    f = extract("jinja2", "listen {{ .Values.port }};\n{% include 'a.j2' %}\nmotd: {{ tpl .Values.motd . }}\n")
    r = refs(f)
    assert {("values:port", "reads", ""), ("values:*", "reads", ""), ("a.j2", "include", "")} <= r
    assert not any(
        k == "reads" for _, k, _ in refs(extract("jinja2", "{{ port }} {{ user.name }}\n"))
    )  # Jinja2: no reads


# ================================================================ Helm: the action scan and the gotmpl tree read the same facts
TRICKY = [
    '{{ define "a" }}{{ "}} {{ end" }}{{ include "b" . }}{{ end }}{{ define "b" }}x{{ end }}\n',  # `}}` and `{{ end` in a string
    '{{ define "a" }}{{ `}} {{ define "ghost" }}` }}{{ template "b" . }}{{ end }}{{ define "b" }}{{ end }}\n',  # a raw string
    '{{/* {{ define "ghost" }} */}}{{ define "a" -}}\n{{- .Values.x -}}\n{{- end }}\n',  # a comment, trim markers
    '{{ define "a" }}{{ if .Values.x }}{{ range .Values.l }}{{ include "b" . }}{{ end }}{{ end }}{{ end }}{{ define "b" }}{{ end }}\n',
    '{{ define "a" }}{{ with .Values.m }}{{ printf "%s" "}}" }}{{ include "b" . }}{{ end }}{{ end }}{{ define "b" }}{{ end }}\n',
]


@pytest.mark.parametrize("text", TRICKY)
def test_the_action_scan_agrees_with_the_gotmpl_tree_on_strings_and_comments(grammar, text):
    tg.set_backend(no_grammar)
    plain = extract("yaml", text, "templates/x.tpl")
    grammar("gotmpl")
    assert (
        extract("yaml", text, "templates/x.tpl") == plain
    )  # the facts do not depend on whether the grammar is locked here
