"""graph.extract.cfg and yamlite: YAML (k8s, Helm, GitHub Actions, GitLab CI) and HCL.

YAML runs for real (no grammar needed). HCL is tested through the backend seam of ts_generic with a line-based stand-in for
tree-sitter-hcl that answers data/queries/hcl in its capture vocabulary; the test with the real grammar skips with its reason when
tree-sitter or the grammar is not installed.

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_cfg.py
"""

import re
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test

ROOT = Path(__file__).resolve().parent.parent
from dead_code_checker.graph import contracts, link as dlink, model as md, reach as rc, roots as rt, ts as tsm  # noqa: E402
from dead_code_checker.graph.extract import batch, cfg, ts_generic as tg, yamlite as Y  # noqa: E402
from graph_support import inline_units  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "cfg"
LANG = {".yaml": "yaml", ".yml": "yaml", ".tpl": "yaml", ".tf": "hcl", ".tfvars": "hcl"}


def read(rel):
    return (FIX / rel).read_text()


def facts(rel_or_text, lang="yaml", path="x.yaml", literal=False):
    text = rel_or_text if literal else read(rel_or_text)
    if not literal and lang == "yaml":
        lang = cfg.dclangs.detect(rel_or_text) or lang  # values.yaml is the lang "helm-values"
    return contracts.get_extractor(lang).extract(lang, path, text)


def defs(f):
    return {d.qual: d for d in f.defs if d.qual}


def refs(f):
    return {(r.name, r.kind, r.scope) for r in f.refs}


def roots(f):
    return {(r.kind, r.target, r.strength) for r in f.roots}


# ====================================================================== yamlite
def test_yamlite_mappings_sequences_flow_and_block_scalars():
    docs, err = Y.parse(
        "a: 1  # c\nb:\n  c: 'x y'\n  d: [p, \"q,r\", {k: v}]\nl:\n- one\n- k: v\n  w: z\nrun: |\n  echo a # not a comment\n  echo b\nz: &anc\n  m: n\nt: !reference [a, b]\n"
    )
    assert err == 0 and len(docs) == 1
    d = docs[0]
    assert Y.scalar(Y.get(d, "a")) == "1" and Y.scalar(Y.get(Y.get(d, "b"), "c")) == "x y"
    flow = Y.items(Y.get(Y.get(d, "b"), "d"))
    assert [Y.scalar(x) for x in flow[:2]] == ["p", "q,r"] and Y.scalar(Y.get(flow[2], "k")) == "v"
    seq = Y.items(Y.get(d, "l"))  # a sequence at the indent of its key, and a `- key: v` item
    assert Y.scalar(seq[0]) == "one" and Y.scalar(Y.get(seq[1], "w")) == "z"
    assert Y.scalar(Y.get(d, "run")) == "echo a # not a comment\necho b"
    assert Y.scalar(Y.get(Y.get(d, "z"), "m")) == "n" and Y.scalar(Y.get(d, "t")) == "!reference [a, b]"


def test_yamlite_lines_documents_and_multiline_flow():
    docs, _ = Y.parse("---\na:\n  b: 1\n---\n# only a comment\n---\nc: [1,\n  2]\nd: x\n")
    assert len(docs) == 2
    a, c = docs
    assert (a.line, a.end, Y.get(a, "a").line, Y.get(a, "a").end) == (2, 3, 2, 3)
    assert [Y.scalar(x) for x in Y.items(Y.get(c, "c"))] == ["1", "2"] and Y.get(c, "d").line == 9


def test_yamlite_errors_are_counted_and_never_raise():
    docs, err = Y.parse("a: 1\nnot a mapping line\nb: 2\n")
    assert err == 1 and [k for k, _ in docs[0].val] == ["a", "b"]
    assert Y.parse("\t- x\n")[1] == 1 and Y.parse("")[0] == [] and Y.parse("just a scalar\n")[0] == []
    deep = "".join("  " * i + "k:\n" for i in range(200)) + "  " * 200 + "v: 1\n"
    assert Y.parse(deep)[0]  # the depth guard, no RecursionError


def test_a_deeply_nested_hostile_file_is_one_file_error_not_a_crash():
    hostile = "a: " + "[" * 2000 + "]" * 2000 + "\n"  # nested flow sequences far beyond the recursion limit
    with pytest.raises(RecursionError):
        Y.parse(hostile)  # the reader itself is a plain recursive function: the extractor is the guard
    for lang in ("yaml", "helm-values"):
        f = cfg.Cfg().extract(lang, "x.yaml", hostile)
        assert f.errors == 1 and not f.defs and not f.refs


def test_yamlite_keys_with_colons_quotes_and_duplicates():
    d = Y.parse('"a b": 1\nurl: http://x/y:80\nk: 1\nk: 2\n"on": push\n')[0][0]
    assert [k for k, _ in d.val] == ["a b", "url", "k", "k", "on"] and Y.scalar(Y.get(d, "url")) == "http://x/y:80"


# ====================================================================== k8s
def test_k8s_facts_defs_refs_roots():
    f = facts("k8s/web.yaml")
    assert set(defs(f)) == {"Deployment:web", "Service:web-svc", "Ingress:web"} and f.errors == 0
    assert defs(f)["Deployment:web"].line0 == 1 and defs(f)["Deployment:web"].kind == "resource"
    r = refs(f)
    for name in (
        "ServiceAccount:web-sa",
        "Secret:regcred",
        "ConfigMap:app-cfg",
        "Secret:api-token",
        "Secret:tls-cert",
        "PersistentVolumeClaim:web-data",
    ):
        assert (name, "ref", "Deployment:web") in r
    assert ("Service:web-svc", "ref", "Ingress:web") in r
    assert roots(f) == {
        ("k8s-workload", "Deployment:web", "hard"),
        ("k8s-workload", "Service:web-svc", "soft"),
        ("k8s-workload", "Ingress:web", "hard"),
    }


def test_k8s_consumed_kinds_are_no_roots_and_data_keys_are_not_references():
    f = facts("k8s/config.yaml")
    assert roots(f) == set() and refs(f) == set()  # `data: {secret: ...}` is user data, not a Secret reference
    assert {"ConfigMap:orphan-cfg", "Secret:orphan-secret", "ServiceAccount:orphan-sa"} <= set(defs(f))


def test_k8s_list_kinds_rbac_hpa_and_odd_names():
    f = facts(
        "apiVersion: v1\nkind: List\nitems:\n  - apiVersion: rbac.authorization.k8s.io/v1\n    kind: RoleBinding\n    metadata: {name: rb}\n"
        "    roleRef: {kind: Role, name: r.1}\n    subjects:\n      - {kind: ServiceAccount, name: sa}\n"
        "  - apiVersion: autoscaling/v2\n    kind: HorizontalPodAutoscaler\n    metadata: {name: h}\n    spec:\n      scaleTargetRef: {kind: Deployment, name: web}\n",
        literal=True,
    )
    assert set(defs(f)) == {"RoleBinding:rb", "HorizontalPodAutoscaler:h"}
    assert {"Role:r%2E1", "ServiceAccount:sa"} <= {n for n, _, s in refs(f) if s == "RoleBinding:rb"}
    assert ("Deployment:web", "ref", "HorizontalPodAutoscaler:h") in refs(f)


def test_k8s_not_a_manifest_is_not_k8s():
    f = facts("apiVersion: v1\nreplicas: 2\n", lang="helm-values", literal=True)  # no kind: a plain mapping (values)
    assert not f.roots and set(defs(f)) == {"values:apiVersion", "values:replicas"}


# ====================================================================== helm
def test_helm_template_facts():
    f = facts("helm/templates/deployment.yaml")
    assert not defs(f) and roots(f) == {("k8s-workload", "", "hard")}  # a templated name: no def, the file is the root
    r = refs(f)
    assert ("tpl:chart%2Elabels", "ref", "") in r and ("tpl:chart%2Efullname", "ref", "") in r
    for p in ("replicaCount", "image.repository", "image.tag", "resources"):
        assert (f"values:{p}", "reads", "") in r and (f"values:{p}.*", "reads", "") in r
    assert ("ConfigMap:app-cfg", "ref", "") in r and f.errors == 0  # {{ }} lines do not break the manifest


def test_helm_helpers_define_include_and_scopes():
    f = facts("helm/templates/_helpers.tpl", path="_helpers.tpl")
    assert set(defs(f)) == {"tpl:chart%2Ename", "tpl:chart%2Elabels", "tpl:chart%2Eunused"}
    assert defs(f)["tpl:chart%2Elabels"].line0 == 4 and defs(f)["tpl:chart%2Elabels"].line1 == 6
    assert ("tpl:chart%2Ename", "ref", "tpl:chart%2Elabels") in refs(f)  # include inside a define: from that define
    assert ("values:nameOverride", "reads", "tpl:chart%2Ename") in refs(f)


def test_helm_values_forms_index_bare_and_comments():
    f = facts(
        '{{- /* .Values.nope */ -}}\na: {{ index .Values "x" "y-z" }}\nb: {{ toYaml .Values | indent 2 }}\n',
        literal=True,
    )
    r = {n for n, k, _ in refs(f) if k == "reads"}
    assert (
        "values:x.y%2Dz" not in r
        and {"values:x.y-z", "values:x.y-z.*", "values:*"} <= {x.replace("%2D", "-") for x in r} | r
    )
    assert not any("nope" in n for n in r)


def test_helm_values_file_leaves():
    f = facts("helm/values.yaml")
    assert set(defs(f)) == {
        "values:replicaCount",
        "values:image.repository",
        "values:image.tag",
        "values:image.pullPolicy",
        "values:resources.limits.cpu",
        "values:resources.limits.memory",
        "values:env",
        "values:unusedKey",
        "values:unused.nested",
    }
    assert all(d.kind == "cfgkey" for d in defs(f).values()) and not f.refs and not f.roots


# ====================================================================== CI
def test_gha_facts():
    f = facts(".github/workflows/ci.yml")
    assert set(defs(f)) == {"job:build", "job:test", "job:lint"} and defs(f)["job:test"].kind == "job"
    r = refs(f)
    assert {("job:build", "ref", "job:test"), ("job:build", "ref", "job:lint")} <= r
    assert ("./.github/workflows/reuse.yml", "include", "job:test") in r
    assert ("./.github/actions/setup/action.yml", "include", "job:build") in r
    assert ("./scripts/build.sh", "exec", "job:build") in r and ("bin/lint", "exec", "job:lint") in r
    assert not any(n.startswith("actions/") or "checkout" in n for n, _, _ in r)  # a remote action is external
    assert all(("job:" + j, "ref", "") in r for j in ("build", "test", "lint")) and roots(f) == {
        ("workflow", "", "hard")
    }


def test_gha_workflow_call_only_is_a_soft_root():
    soft = {("workflow", "", "soft")}  # callers may live in other repositories: soft, never dead
    assert roots(facts(".github/workflows/reuse.yml")) == soft
    assert roots(facts(".github/workflows/orphan.yml")) == soft  # `on: workflow_call` as a scalar
    f = facts("on: [workflow_call, push]\njobs:\n  a:\n    steps: []\n", literal=True)
    assert roots(f) == {("workflow", "", "hard")}


def test_gitlab_facts():
    f = facts("ci/.gitlab-ci.yml", path=".gitlab-ci.yml")
    assert set(defs(f)) == {"job:%2Ebase", "job:%2Eunused-template", "job:build", "job:test"}
    r = refs(f)
    assert ("ci/jobs.yml", "include", "") in r and not any(k == "include" and "other" in n for n, k, _ in r)
    assert ("job:%2Ebase", "ref", "job:build") in r and ("job:build", "ref", "job:test") in r
    assert ("job:%2Ebase", "ref", "job:test") in r  # !reference [.base, before_script]
    assert ("./scripts/prepare.sh", "exec", "job:%2Ebase") in r
    assert ("job:build", "ref", "") in r and ("job:%2Ebase", "ref", "") not in r  # hidden jobs are not run
    # the pipeline file is a hard root by its path (roots layer); the content alone gives a soft one (another name, a shared template)
    assert roots(f) == {("ci-job", "", "soft")}


def test_gitlab_trigger_include_and_needs_forms():
    f = facts(
        "child:\n  trigger:\n    include: ci/child.yml\nb:\n  script: [x]\n  needs: [a, {job: c}]\n", literal=True
    )
    r = refs(f)
    assert ("ci/child.yml", "include", "") in r and {("job:a", "ref", "job:b"), ("job:c", "ref", "job:b")} <= r


def test_plain_yaml_has_no_defs_only_helm_values_has():
    text = "services:\n  web:\n    image: x\n"
    assert not [d for d in facts(text, literal=True).defs if d.kind == "cfgkey"]
    assert set(defs(facts(text, lang="helm-values", literal=True))) - {""} == {"values:services.web.image"}
    for name in ("docker-compose.yml", "action.yml", "Chart.yaml", "mkdocs.yml", "values-prod.yaml"):
        assert cfg.dclangs.detect("d/" + name) == "yaml"
    assert cfg.dclangs.detect("chart/values.yaml") == cfg.dclangs.detect("values.yml") == "helm-values"
    # end to end: a compose file is not dead keys
    res = dlink.link({"docker-compose.yml": facts(text, literal=True)}, {}, workers=1)
    assert not [n for n in res.graph.nodes if n.kind == "cfgkey"]


# ====================================================================== determinism and shards
def test_extraction_is_a_function_of_the_text_and_survives_the_shard_roundtrip():
    ex = contracts.get_extractor("yaml")
    text = read("k8s/web.yaml")
    assert ex.extract("yaml", "a/b.yaml", text).to_payload() == ex.extract("yaml", "c.yml", text).to_payload()
    s = contracts.make_shard(ex, "yaml", "a.yaml", "b" * 40, text)
    assert s.ext_ver == "cfg@9" and s.facts().to_payload() == ex.extract("yaml", "z.yaml", text).to_payload()
    assert ex.grammar_sha("yaml") != ex.grammar_sha("hcl") and len(ex.grammar_sha("yaml")) == 64


def test_registration_and_missing_finding():
    assert contracts.get_extractor("yaml").name == "cfg" and contracts.get_extractor("hcl").name == "cfg"
    assert tg.missing_finding("yaml", "a.yaml") is None  # yamlite needs no grammar
    assert tg.missing_finding("python", "a.py") is None
    for ext in (".yaml", ".yml", ".tpl"):
        assert contracts.get_extractor(LANG[ext]) and cfg.dclangs.detect("x" + ext) == "yaml"


def test_batch_extract_is_the_same_for_1_and_2_workers():
    items = [
        ("yaml", p, "%040x" % i, read(p))
        for i, p in enumerate(
            ["k8s/web.yaml", "k8s/config.yaml", "helm/values.yaml", ".github/workflows/ci.yml", "ci/jobs.yml"]
        )
    ]
    one, miss1 = batch.extract_many(items, workers=1)
    two, miss2 = batch.extract_many(items, workers=2, chunk=2)
    assert not miss1 and not miss2 and [s.key for s in one] == [s.key for s in two]
    assert [s.facts().to_payload() for s in one] == [s.facts().to_payload() for s in two]


# ====================================================================== link: `reads` prefix
def test_reads_prefix_name_reaches_every_key_below_it():
    d = lambda q: md.Def(q, "cfgkey", 1, 1, "package", 0, -1, "")  # noqa: E731
    files = {
        "v.yaml": md.Facts("yaml", defs=(d("values:a.b"), d("values:a.c"), d("values:ab"), d("values:z"))),
        "t.yaml": md.Facts("yaml", refs=(md.Ref("values:a.*", "reads", 3, ""), md.Ref("values:z", "reads", 4, ""))),
    }
    res = dlink.link(files, {"t.yaml": [md.RootHint("manual", "file:t.yaml", 1)]}, workers=1)
    reads = {(e.dst.split("#")[1]) for e in res.graph.edges if e.kind == "reads"}
    assert reads == {"values:a.b", "values:a.c", "values:z"}  # "values:ab" is not below "values:a."


# ====================================================================== end to end: link + reach
def _load(rels, hints=None):
    files = {}
    for rel in rels:
        lang = cfg.dclangs.detect(rel) or LANG[Path(rel).suffix]
        files[rel] = contracts.get_extractor(lang).extract(lang, rel, read(rel))
    hs = {p: [md.RootHint(k, t, 1, s, tr) for k, t, s, tr in v] for p, v in (hints or {}).items()}
    res = dlink.link(files, hs, workers=1)
    assert not res.findings, res.findings
    raw = rc.analyze(res.graph).verdicts()
    # a cross-file name match is an N edge: reached only through N edges is weak-alive, which still is not dead
    return res, {k: "alive" if x == "weak-alive" else x for k, x in raw.items()}, raw


def _by_qual(verdicts):
    return {k.split("#", 1)[1] if "#" in k else k: v for k, v in verdicts.items()}


def test_k8s_end_to_end_a_configmap_without_a_consumer_is_dead():
    res, v, _ = _load(["k8s/web.yaml", "k8s/config.yaml"])
    q = _by_qual(v)
    assert (
        q["ConfigMap:app-cfg"] == "alive" and q["Secret:tls-cert"] == "alive" and q["ServiceAccount:web-sa"] == "alive"
    )
    assert q["Secret:api-token"] == q["Secret:regcred"] == q["PersistentVolumeClaim:web-data"] == "alive"
    assert q["ConfigMap:orphan-cfg"] == q["Secret:orphan-secret"] == q["ServiceAccount:orphan-sa"] == "dead"
    assert q["Deployment:web"] == q["Ingress:web"] == "alive" and q["Service:web-svc"] == "alive"  # read by the Ingress


def test_helm_end_to_end_unused_values_and_templates():
    _, v, _ = _load(["helm/values.yaml", "helm/templates/deployment.yaml", "helm/templates/_helpers.tpl"])
    q = _by_qual(v)
    alive = [
        "values:replicaCount",
        "values:image.repository",
        "values:image.tag",
        "values:resources.limits.cpu",
        "values:resources.limits.memory",
        "tpl:chart%2Elabels",
        "tpl:chart%2Ename",
    ]
    dead = ["values:unusedKey", "values:unused.nested", "values:image.pullPolicy", "values:env", "tpl:chart%2Eunused"]
    assert [q[k] for k in alive] == ["alive"] * len(alive)
    assert [q[k] for k in dead] == ["dead"] * len(dead)


def test_gha_end_to_end_reusable_workflows_and_actions():
    files = [
        "/.github/workflows/ci.yml",
        "/.github/workflows/reuse.yml",
        "/.github/workflows/orphan.yml",
        "/.github/actions/setup/action.yml",
    ]
    _, v, raw = _load([f[1:] for f in files])
    short = {k.split(":", 1)[1]: x for k, x in v.items() if "#" not in k}
    assert short[".github/workflows/ci.yml"] == "alive" and short[".github/workflows/reuse.yml"] == "alive"
    assert short[".github/actions/setup/action.yml"] == "alive"  # uses: ./.github/actions/setup
    rshort = {k.split(":", 1)[1]: x for k, x in raw.items() if "#" not in k}
    assert rshort[".github/workflows/orphan.yml"] == "weak-alive"  # workflow_call only: a soft root (external callers)
    q = _by_qual(v)
    assert q["job:inner"] == "alive" and q["job:test"] == "alive"


def test_gitlab_end_to_end_extends_reference_include():
    """The pipeline file is rooted by the roots layer (GitlabRoots), nothing is injected by hand."""
    paths = ["ci/.gitlab-ci.yml", "ci/jobs.yml"]
    files = {p: contracts.get_extractor("yaml").extract("yaml", p, read(p)) for p in paths}
    hints = {p: rt.extract_hints(p, read(p)) for p in paths}
    assert [h.kind for h in hints["ci/.gitlab-ci.yml"]] == ["ci-job"] and not hints["ci/jobs.yml"]
    res = dlink.link(files, hints, workers=1)
    assert not res.findings, res.findings
    v = {k: "alive" if x == "weak-alive" else x for k, x in rc.analyze(res.graph).verdicts().items()}
    q = _by_qual(v)
    assert q["job:build"] == q["job:test"] == q["job:deploy"] == q["job:%2Ebase"] == q["job:%2Eshared"] == "alive"
    assert q["job:%2Eunused-template"].startswith("dead")
    assert v["yaml:ci/jobs.yml"] == "alive"  # include: local


def test_gitlab_pipeline_root_resolves_in_the_roots_layer():
    text = read("ci/.gitlab-ci.yml")
    hs = {"sub/.gitlab-ci.yaml": rt.extract_hints("sub/.gitlab-ci.yaml", text)}
    got = rt.resolve(hs, {"sub/.gitlab-ci.yaml": "100644"}, lambda p: text)
    assert not got.findings and [(r.kind, r.strength, r.node) for r in got.roots] == [
        ("ci-job", "hard", "yaml:sub/.gitlab-ci.yaml")
    ]
    assert not rt.extract_hints("ci/jobs.yml", text) and not rt.extract_hints(
        "my.gitlab-ci.yml", text
    )  # a fragment is no root


# ====================================================================== HCL through a fake parser
class N(NamedTuple):
    start_byte: int
    end_byte: int
    start_point: tuple
    end_point: tuple


class FakeHcl:
    """A line-based stand-in for tree-sitter-hcl in the capture vocabulary of data/queries/hcl: `type "l1" "l2" {` opens a block
    (also `{}` on one line), `name = expr` is an attribute (the value may continue over lines until its brackets balance), a lone
    `}` closes. Blocks are reported in the same shape as the real queries: one match per type identifier and one per label.
    """

    HEAD = re.compile(r'^(\s*)(\w+)((?:\s+"[^"]*")*)\s*\{(.*)$')
    ATTR = re.compile(r"^(\s*)(\w+)\s*=\s*(.*)$")

    def __init__(self, data: bytes, errors: int = 0):
        self.errors, self.m = errors, {"defs": [], "refs": []}
        lines = data.decode().split("\n")
        offs, pos = [], 0
        for ln in lines:
            offs.append(pos)
            pos += len(ln) + 1
        self.offs = offs
        stack = []  # open blocks: [start line, start col, node-less info]
        i = 0
        while i < len(lines):
            ln = lines[i]
            h, a = self.HEAD.match(ln), self.ATTR.match(ln)
            if h and not a:
                stack.append(
                    dict(row=i, col=len(h.group(1)), ident=h.group(2), labels=h.start(3), text=h.group(3), attrs=[])
                )
                if h.group(4).strip().startswith("}"):
                    self.close(stack, i, ln.rindex("}") + 1)
            elif a:
                j, expr = i, a.group(3)
                while expr.count("[") + expr.count("{") > expr.count("]") + expr.count("}") and j + 1 < len(lines):
                    j += 1
                    expr += "\n" + lines[j]
                col = len(a.group(1))
                attr = self.node(i, col, j, len(lines[j]))
                name = self.node(i, col, i, col + len(a.group(2)))
                ecol = ln.index("=") + 1 + (len(ln[ln.index("=") + 1 :]) - len(ln[ln.index("=") + 1 :].lstrip()))
                value = self.node(i, ecol, j, len(lines[j]))
                if stack:
                    stack[-1]["attrs"].append((name, attr))
                    self.m["refs"].append((0, {"expr": [value]}))
                else:
                    self.m["refs"].append((1, {"tfvars.name": [name]}))
                i = j
            elif ln.strip() == "}" and stack:
                self.close(stack, i, ln.index("}") + 1)
            i += 1

    def node(self, r0, c0, r1, c1):
        return N(self.offs[r0] + c0, self.offs[r1] + c1, (r0, c0), (r1, c1))

    def close(self, stack, row, col):
        b = stack.pop()
        blk = self.node(b["row"], b["col"], row, col)
        ident = self.node(b["row"], b["col"], b["row"], b["col"] + len(b["ident"]))
        self.m["defs"].append((0, {"block": [blk], "block.type": [ident]}))
        for lm in re.finditer(r'"[^"]*"', b["text"]):
            lab = self.node(b["row"], b["labels"] + lm.start(), b["row"], b["labels"] + lm.end())
            self.m["defs"].append((1, {"block": [blk], "block.label": [lab]}))
        for name, attr in b["attrs"]:
            self.m["defs"].append(
                (2, {"attr.kw": [ident], "attr.block": [blk], "attr.name": [name], "attr.node": [attr]})
            )

    def run(self, kind):
        return self.m.get(kind, [])


@pytest.fixture
def fake_hcl():
    tg.set_backend(lambda grammar, data: FakeHcl(data))
    yield
    tg.set_backend(None)


def test_hcl_defs_refs_roots(fake_hcl):
    f = facts("tf/main.tf", "hcl", "main.tf")
    d = defs(f)
    assert set(d) == {
        "local:name",
        "local:tagmap",
        "local:dead",
        "data:aws_ami:base",
        "data:aws_region:unused",
        "resource:aws_instance:web",
        "module:net",
    }
    assert d["local:name"].kind == "tfvar" and d["resource:aws_instance:web"].kind == "resource"
    assert (d["resource:aws_instance:web"].line0, d["resource:aws_instance:web"].line1) == (
        13,
        19,
    )  # the lifecycle block is nested
    r = refs(f)
    assert {
        ("var:prefix", "ref", "local:name"),
        ("local:name", "ref", "local:tagmap"),
        ("var:unused_in_local", "ref", "local:dead"),
        ("data:aws_ami:base", "ref", "resource:aws_instance:web"),
        ("local:tagmap", "ref", "resource:aws_instance:web"),
        ("var:cidr", "ref", "module:net"),
    } <= r
    assert not any(n == "var:version" for n, _, _ in r) and not any(s == "locals" for _, _, s in r)
    assert roots(f) == {("init", "resource:aws_instance:web", "hard"), ("init", "module:net", "hard")}


def test_hcl_variables_outputs_and_tfvars(fake_hcl):
    f = facts("tf/variables.tf", "hcl", "variables.tf")
    assert (
        set(defs(f)) == {"var:prefix", "var:cidr", "var:unused_in_local", "var:never_used", "var:from_tfvars"}
        and not f.roots
    )
    o = facts("tf/outputs.tf", "hcl", "outputs.tf")
    assert set(defs(o)) == {"output:ip", "output:net"} and defs(o)["output:ip"].kind == "var"
    assert refs(o) == {("resource:aws_instance:web", "ref", "output:ip"), ("module:net", "ref", "output:net")}
    assert roots(o) == {("tf-output", "output:ip", "soft"), ("tf-output", "output:net", "soft")}
    t = facts("tf/prod.tfvars", "hcl", "prod.tfvars")
    assert refs(t) == {("var:from_tfvars", "ref", "")} and roots(t) == {("init", "", "hard")} and not defs(t)


def test_hcl_reference_forms_and_interpolation(fake_hcl):
    f = facts(
        'output "o" {\n  value = "${var.a}-${data.x_y.z.id}/${module.m.out}"\n  other = [for s in var.list : aws_s3_bucket.b[s].id]\n}\n',
        "hcl",
        literal=True,
    )
    assert refs(f) == {
        ("var:a", "ref", "output:o"),
        ("data:x_y:z", "ref", "output:o"),
        ("module:m", "ref", "output:o"),
        ("var:list", "ref", "output:o"),
        ("resource:aws_s3_bucket:b", "ref", "output:o"),
    }


def test_hcl_end_to_end(fake_hcl):
    _, v, raw = _load(["tf/main.tf", "tf/variables.tf", "tf/outputs.tf", "tf/prod.tfvars"])
    q = _by_qual(v)
    for k in (
        "var:prefix",
        "var:cidr",
        "var:from_tfvars",
        "local:name",
        "local:tagmap",
        "data:aws_ami:base",
        "resource:aws_instance:web",
        "module:net",
    ):
        assert q[k] == "alive", k
    assert _by_qual(raw)["output:ip"] == _by_qual(raw)["output:net"] == "weak-alive"  # soft roots
    for k in ("var:never_used", "local:dead", "var:unused_in_local", "data:aws_region:unused"):
        assert q[k].startswith("dead"), k  # a variable read only by a dead local dies with it


def test_hcl_without_a_grammar_is_extractor_missing_not_a_fallback(monkeypatch):
    tg.set_backend(None)
    inline_units(monkeypatch)  # the patch below must reach the extraction, which runs in a pool
    monkeypatch.setattr(tsm, "get_parser", lambda g: (_ for _ in ()).throw(LookupError(f"grammar {g} is missing: x")))
    with pytest.raises(contracts.ExtractorMissing, match="grammar hcl is missing"):
        contracts.get_extractor("hcl").extract("hcl", "a.tf", 'variable "a" {}\n')
    assert batch.extract_many([("hcl", "a.tf", "c" * 40, "x")])[1].keys() == {"hcl"}


def test_hcl_parse_errors_are_counted_and_the_facts_kept():
    tg.set_backend(lambda g, data: FakeHcl(data, errors=1))
    try:
        f = facts('variable "a" {}\n', "hcl", literal=True)
    finally:
        tg.set_backend(None)
    assert f.errors == 1 and "var:a" in defs(f)


def test_hcl_queries_exist_and_use_the_documented_vocabulary():
    d = ROOT / "dead_code_checker" / "graph" / "data" / "queries" / "hcl"
    text = (d / "defs.scm").read_text() + (d / "refs.scm").read_text()
    caps = set(re.findall(r"@([\w.]+)", text))
    assert caps == {
        "block",
        "block.type",
        "block.label",
        "attr.kw",
        "attr.name",
        "attr.node",
        "attr.block",
        "expr",
        "tfvars.name",
    }
    assert "hcl" in cfg.dclangs.load().langs and cfg.dclangs.load().grammar("hcl") == "hcl"


def _real_grammar():
    try:
        tsm.get_parser("hcl")
        return ""
    except LookupError as exc:
        return str(exc)


@pytest.mark.skipif(
    bool(_real_grammar()), reason=f"tree-sitter hcl grammar is not installed on this host: {_real_grammar()}"
)
def test_hcl_with_the_real_grammar():
    tg.set_backend(None)
    f = facts(read("tf/main.tf") + read("tf/variables.tf") + read("tf/outputs.tf"), "hcl", literal=True)
    assert {"var:prefix", "output:ip", "resource:aws_instance:web", "local:name"} <= set(defs(f)) and f.errors == 0
    assert ("var:prefix", "ref", "local:name") in refs(f)


# ====================================================================== fix round 1: false dead
def _verdicts(files, rooted=()):
    fs = {
        p: contracts.get_extractor(cfg.dclangs.detect(p) or "yaml").extract(cfg.dclangs.detect(p) or "yaml", p, t)
        for p, t in files.items()
    }
    hs = {p: [md.RootHint("ci-job", "file:" + p, 1, "hard", "registered")] for p in rooted}
    res = dlink.link(fs, hs, workers=1)
    assert not res.findings, res.findings
    return _by_qual({k: x for k, x in rc.analyze(res.graph).verdicts().items()})


def _k8s_dead(*docs):
    v = _verdicts({"m.yaml": "\n---\n".join(docs)})
    return {k for k, x in v.items() if x.startswith("dead") and ":" in k}


DEPLOY = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  template:\n    spec:\n"


def test_k8s_ingress_tls_and_crd_secret_selectors_keep_the_secret_alive():
    sec = lambda n: f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: {n}\n"  # noqa: E731
    ing = "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: i\nspec:\n  tls:\n    - hosts: [a]\n      secretName: web-tls\n"
    mon = (
        "apiVersion: monitoring.coreos.com/v1\nkind: ServiceMonitor\nmetadata:\n  name: m\nspec:\n  endpoints:\n"
        "    - basicAuth:\n        username:\n          name: mon-auth\n          key: user\n"
    )
    csi = (
        DEPLOY
        + "      volumes:\n        - name: v\n          csi:\n            nodePublishSecretRef:\n              name: csi-sec\n"
    )
    crd = "apiVersion: x.io/v1\nkind: Thing\nmetadata:\n  name: t\nspec:\n  credentialsSecretName: crd-sec\n"
    names = ("web-tls", "mon-auth", "csi-sec", "crd-sec", "lonely")
    dead = _k8s_dead(ing, mon, csi, crd, *[sec(n) for n in names])
    assert dead == {"Secret:lonely"}  # only the unreferenced one


def test_k8s_aggregated_clusterrole_and_templated_references_are_not_dead():
    agg = (
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: agg\n  labels:\n"
        "    rbac.authorization.k8s.io/aggregate-to-view: 'true'\n"
    )
    plain = "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: plain\n"
    assert _k8s_dead(agg, plain) == {"ClusterRole:plain"}
    # Helm template: the reader's name is computed, so the literal-named consumed kinds are not provable dead
    tpl = DEPLOY + "      serviceAccountName: {{ .Values.sa }}\n"
    sa = "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: lit\n"
    cm = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Release.Name }}-cfg\n"
    assert _k8s_dead(tpl, sa, cm) == set()
    assert _k8s_dead(DEPLOY.replace("web", "w2"), sa) == {"ServiceAccount:lit"}  # without {{ }} it stays provable


def test_gitlab_anchor_and_merge_key_keep_the_hidden_job_alive():
    ci = ".defaults: &defaults\n  image: x\n.lst: &lst\n  script: [a]\n.unused: &unused\n  image: y\nbuild:\n  <<: *defaults\n  stage: b\n"
    ci += "test:\n  <<: [*lst]\n  stage: t\n"
    f = facts(ci, literal=True)
    r = refs(f)
    assert ("job:%2Edefaults", "ref", "job:build") in r and ("job:%2Elst", "ref", "job:test") in r
    assert not any(n == "job:%2Eunused" for n, _, _ in r)
    v = _verdicts({".gitlab-ci.yml": ci}, [".gitlab-ci.yml"])  # the pipeline file is a root by its path
    assert v["job:%2Edefaults"] == "alive" and v["job:%2Elst"] == "alive" and v["job:%2Eunused"].startswith("dead")


def test_composite_action_steps_are_include_and_exec_refs():
    act = (
        "name: a\nruns:\n  using: composite\n  steps:\n    - uses: ./.github/actions/other\n"
        "    - run: bash .github/scripts/x.sh\n      shell: bash\n"
    )
    f = facts(act, literal=True)
    assert ("./.github/actions/other/action.yml", "include", "") in refs(f) and (
        ".github/scripts/x.sh",
        "exec",
        "",
    ) in refs(f)
    assert not [d for d in f.defs if d.kind == "cfgkey"]  # an action's own keys are no config keys
    node = facts("runs:\n  using: node20\n  main: dist/index.js\n", literal=True)
    assert not [d for d in node.defs if d.kind == "cfgkey"]


def test_exec_tokens_find_scripts_run_by_an_interpreter_from_any_path():
    for cmd, tok in (
        ("bash .github/scripts/x.sh", ".github/scripts/x.sh"),
        ("sh hack/build.sh", "hack/build.sh"),
        ("bash deploy.sh", "deploy.sh"),
        ("python3 tools/gen.py --x", "tools/gen.py"),
        ("./run.sh", "./run.sh"),
        ("curl https://example.com/a.sh", None),
    ):
        f = facts(f"on: push\njobs:\n  a:\n    steps:\n      - run: {cmd}\n", literal=True)
        got = {n for n, k, _ in refs(f) if k == "exec"}
        g = facts(f"build:\n  script:\n    - {cmd}\n", literal=True)
        gl = {n for n, k, _ in refs(g) if k == "exec"}
        assert got == gl == ({tok} if tok else set()), (cmd, got, gl)


# ====================================================================== fix round 2
def _full(files):
    """link + reach with the hints of the roots layer; returns ({qual or file key: verdict}, raw). A file of another
    language (a script, a Dockerfile) is a bare file node."""
    fs, hs = {}, {}
    for p, t in files.items():
        lang = cfg.dclangs.detect(p) or "yaml"
        fs[p] = (
            contracts.get_extractor(lang).extract(lang, p, t) if lang in (*cfg.Cfg.langs, "jinja2") else md.Facts(lang)
        )
        hs[p] = rt.extract_hints(p, t)
    res = dlink.link(fs, hs, workers=1)
    assert not res.findings, res.findings
    raw = rc.analyze(res.graph).verdicts()
    return _by_qual(raw), raw


def test_k8s_ingress_annotation_gateway_flux_prometheus_and_sa_secrets_are_read():
    sec = lambda n: f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: {n}\n"  # noqa: E731
    cm = lambda n: f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {n}\n"  # noqa: E731
    ing = (
        "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: i\n  annotations:\n"
        "    nginx.ingress.kubernetes.io/auth-secret: basic-auth\n"
        "    nginx.ingress.kubernetes.io/auth-tls-secret: team/client-ca\n"
        '    nginx.ingress.kubernetes.io/proxy-ssl-secret: "up-tls"\n    other/note: lonely-note\n'
    )
    gw = (
        "apiVersion: gateway.networking.k8s.io/v1\nkind: Gateway\nmetadata:\n  name: g\nspec:\n  listeners:\n"
        "    - name: https\n      tls:\n        certificateRefs:\n          - kind: Secret\n            name: gw-cert\n"
    )
    hr = (
        "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\nmetadata:\n  name: h\nspec:\n  valuesFrom:\n"
        "    - kind: ConfigMap\n      name: hr-values\n    - kind: Secret\n      name: hr-secret\n      valuesKey: x\n"
    )
    prom = (
        "apiVersion: monitoring.coreos.com/v1\nkind: Prometheus\nmetadata:\n  name: p\nspec:\n  configMaps:\n"
        "    - prom-extra\n  secrets: [prom-secret]\n"
    )
    sa = "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: sa\nsecrets:\n  - name: sa-token\n"
    used = ("basic-auth", "client-ca", "up-tls", "gw-cert", "hr-secret", "prom-secret", "sa-token")
    dead = _k8s_dead(
        ing,
        gw,
        hr,
        prom,
        sa,
        DEPLOY.replace("web", "w") + "      serviceAccountName: sa\n",
        cm("hr-values"),
        cm("prom-extra"),
        cm("lonely-note"),
        *[sec(n) for n in used],
        sec("nobody"),
    )
    assert dead == {"ConfigMap:lonely-note", "Secret:nobody"}


def test_helm_chart_yaml_keeps_dependency_condition_tags_and_global_values():
    chart = (
        "apiVersion: v2\nname: umb\nversion: 1.0.0\ndependencies:\n  - name: postgresql\n    version: 1.2.3\n"
        "    condition: postgresql.enabled\n  - name: redis\n    alias: cache\n    tags: [front]\n"
        "    import-values:\n      - child: data\n        parent: redisData\n"
    )
    values = (
        "postgresql:\n  enabled: true\n  auth:\n    password: x\ncache:\n  size: 1\nglobal:\n  region: eu\n"
        "tags:\n  front: true\n  back: false\nredisData:\n  a: 1\nown: 2\nunread: 3\n"
    )
    tpl = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\ndata:\n  a: {{ .Values.own }}\n"
    v, raw = _full({"umb/Chart.yaml": chart, "umb/values.yaml": values, "umb/templates/c.yaml": tpl})
    live = [
        "values:postgresql.enabled",
        "values:postgresql.auth.password",
        "values:cache.size",
        "values:global.region",
        "values:tags.front",
        "values:redisData.a",
        "values:own",
    ]
    assert all(v[k] in ("alive", "weak-alive") for k in live), [(k, v[k]) for k in live]
    assert v["values:unread"].startswith("dead") and v["values:tags.back"].startswith("dead")
    assert v["yaml:umb/Chart.yaml"] == "alive" and raw["helm-values:umb/values.yaml"] != "dead"
    assert roots(facts(chart, literal=True)) == {("init", "", "hard")}


def test_helm_values_without_templates_beside_them_are_read_from_outside():
    vals = "image:\n  tag: v1\nreplicas: 2\n"
    v, raw = _full({"deploy/argo/values.yaml": vals})  # an override file for an upstream chart
    assert {raw[k] for k in raw if k.startswith("helm-values:deploy/argo/values.yaml#")} == {"weak-alive"} and raw[
        "helm-values:deploy/argo/values.yaml"
    ] == "weak-alive"
    # the same keys with a templates/ directory beside the file stay checkable
    tpl = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\ndata:\n  a: {{ .Values.replicas }}\n"
    v2, raw2 = _full({"chart/values.yaml": vals, "chart/templates/c.yaml": tpl})
    assert v2["values:replicas"] == "weak-alive" and v2["values:image.tag"].startswith("dead")
    # a templates/ directory elsewhere does not count, and the root of the tree works too
    v3, _ = _full({"values.yaml": vals, "other/templates/c.yaml": tpl})
    assert v3["values:image.tag"] == v3["values:replicas"] == "weak-alive"
    v4, _ = _full({"values.yaml": vals, "templates/c.yaml": tpl})
    assert v4["values:image.tag"].startswith("dead")


def test_a_config_file_lives_while_one_of_its_entries_does():
    v, raw = _full(
        {
            "a.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: lone\n",
            "b.yaml": DEPLOY + "      serviceAccountName: sa\n",
            "c.yaml": "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: sa\n",
        }
    )
    assert (
        raw["yaml:b.yaml"] == "alive" and raw["yaml:c.yaml"] == "weak-alive"
    )  # c.yaml holds only an entry read by name
    assert v["ConfigMap:lone"].startswith("dead") and raw["yaml:a.yaml"].startswith("dead")  # all its entries are dead
    w, rw = _full(
        {k: read(k) for k in ("helm/values.yaml", "helm/templates/deployment.yaml", "helm/templates/_helpers.tpl")}
    )
    assert rw["helm-values:helm/values.yaml"] != "dead" and rw["yaml:helm/templates/_helpers.tpl"] in (
        "alive",
        "weak-alive",
    )


def test_utf8_bom_does_not_hide_the_first_key():
    dep = "\ufeff" + DEPLOY + "      serviceAccountName: sa\n"
    f = facts(dep, literal=True)
    assert "Deployment:web" in defs(f) and ("ServiceAccount:sa", "ref", "Deployment:web") in refs(f)
    docs, _ = Y.parse("\ufeffa: 1\n")
    assert [k for k, _ in docs[0].val] == ["a"]
    h = facts("\ufeffnot_a_chart: 1\n", literal=True)
    assert not h.defs or all(d.kind == "module" for d in h.defs)


def test_core_yaml_tags_on_names_are_dropped():
    f = facts("apiVersion: v1\nkind: Secret\nmetadata:\n  name: !!str s1\n", literal=True)
    assert "Secret:s1" in defs(f)
    docs, _ = Y.parse("a: !!str &x 'v'\nb: !reference [x, y]\nc: &y !!int 5\n")
    assert [Y.scalar(Y.get(docs[0], k)) for k in "abc"] == ["v", "!reference [x, y]", "5"]


def test_action_entry_files_are_exec_refs():
    node = facts(
        "runs:\n  using: node20\n  pre: setup.js\n  main: dist/index.js\n  post: ./dist/cleanup.js\n", literal=True
    )
    assert {n for n, k, _ in refs(node) if k == "exec"} == {"setup.js", "dist/index.js", "./dist/cleanup.js"}
    dock = facts(
        "runs:\n  using: docker\n  image: Dockerfile\n  pre-entrypoint: pre.sh\n  entrypoint: entrypoint.sh\n  post-entrypoint: ${{ x }}\n",
        literal=True,
    )
    assert {n for n, k, _ in refs(dock) if k == "exec"} == {"Dockerfile", "pre.sh", "entrypoint.sh"}
    # `image: Dockerfile` is a file of the action, a ${{ }} expression is no path
    files = {
        ".github/actions/a/action.yml": "runs:\n  using: node20\n  main: dist/index.js\n",
        ".github/actions/a/dist/index.js": "x\n",
    }
    fs = {
        p: contracts.get_extractor(cfg.dclangs.detect(p) or "yaml").extract(cfg.dclangs.detect(p) or "yaml", p, t)
        for p, t in files.items()
        if p.endswith(".yml")
    }
    assert ("dist/index.js", "exec", "") in refs(fs[".github/actions/a/action.yml"])


def test_hcl_import_check_moved_blocks_root_their_file(fake_hcl):
    f = facts('variable "id" {}\nimport {\n  to = aws_s3_bucket.b\n  id = var.id\n}\n', "hcl", literal=True)
    assert ("var:id", "ref", "") in refs(f) and roots(f) == {("init", "", "hard")}
    assert not roots(facts('variable "id" {}\n', "hcl", literal=True))
    # the file is alive through its root, so the variable the import reads is too
    v, _ = _full(
        {"imp.tf": 'variable "id" {}\nvariable "idle" {}\nimport {\n  to = aws_s3_bucket.b\n  id = var.id\n}\n'}
    )
    assert v["var:id"] == "weak-alive" or v["var:id"] == "alive"
    assert v["var:idle"].startswith("dead")


# ====================================================================== pass 3
def _extract(path, text):
    """The facts of a file of the tree: a config file through its extractor, any other language is a bare file node."""
    lang = cfg.dclangs.detect(path) or "yaml"
    return contracts.get_extractor(lang).extract(lang, path, text) if lang in cfg.Cfg.langs else md.Facts(lang)


def _edges(files, kind):
    """{(source qual or file, target file path)} of the edges of a kind between files."""
    fs = {p: _extract(p, t) for p, t in files.items()}
    res = dlink.link(fs, {}, workers=1)
    return {
        (e.src.split(":", 1)[1], e.dst.split(":", 1)[1]) for e in res.graph.edges if e.kind == kind and "#" not in e.dst
    }


def _hints(path, text):
    return [(h.kind, h.target, h.strength) for h in rt.extract_hints(path, text)]


ACTION = "name: a\nruns:\n  using: composite\n  steps:\n    - run: bash scripts/x.sh\n      shell: bash\n"


def test_published_action_is_a_root_by_its_path_hard_at_the_root_soft_below_it():
    for p in ("action.yml", "action.yaml"):
        assert _hints(p, ACTION) == [("export", "self:", "hard")]
    for p in (".github/actions/x/action.yml", "tools/action.yaml"):
        assert _hints(p, ACTION) == [("export", "self:", "soft")]
    assert not _hints("action.yml", "name: a\ndescription: not an action\n")  # no `runs:`
    assert not _hints("my-action.yml", ACTION) and not _hints("dir/actions.yml", ACTION)


def test_a_published_action_and_its_entry_files_are_not_dead_without_a_local_caller():
    node = "name: a\nruns:\n  using: node20\n  main: dist/index.js\n"
    dock = "name: d\nruns:\n  using: docker\n  image: Dockerfile\n"
    _, raw = _full(
        {
            "action.yml": node,
            "dist/index.js": "x\n",
            ".github/actions/d/action.yml": dock,
            ".github/actions/d/Dockerfile": "FROM x\n",
            "other.yml": "a: 1\n",
        }
    )
    assert raw["yaml:action.yml"] == raw["javascript:dist/index.js"] == "alive"  # the repository is the action
    assert raw["yaml:.github/actions/d/action.yml"] == raw["dockerfile:.github/actions/d/Dockerfile"] == "weak-alive"


def test_root_hints_are_the_same_for_every_path_so_a_shard_keyed_by_the_blob_is_right():
    ci = "stages: [a]\nbuild:\n  script: [x]\n"
    for name, p1, p2, text in (
        ("gitlab", ".gitlab-ci.yml", "a/.gitlab-ci.yaml", ci),
        ("action", "action.yml", "action.yaml", ACTION),
        ("action-nested", "x/action.yml", "y/z/action.yml", ACTION),
    ):
        rx = next(x for x in contracts.root_extractors() if x.name == name)
        one = contracts.make_root_shard(rx, p1, "b" * 40, text).facts().roots
        assert one and one == contracts.make_root_shard(rx, p2, "b" * 40, text).facts().roots, name
    hs = rt.extract_hints(
        ".gitlab-ci.yml", ci
    )  # two copies of one pipeline file: each is rooted, whichever shard served it
    modes = {"a/.gitlab-ci.yml": "100644", "b/.gitlab-ci.yml": "100644"}
    got = rt.resolve({p: hs for p in modes}, modes, lambda p: ci)
    assert not got.findings and sorted(r.node for r in got.roots) == ["yaml:a/.gitlab-ci.yml", "yaml:b/.gitlab-ci.yml"]


def test_path_wildcards_star_stays_in_a_segment_double_star_spans_directories():
    m = lambda pat, p: bool(dlink._wild_rx(pat).match(p))  # noqa: E731
    assert m("ci/*.yml", "ci/a.yml") and not m("ci/*.yml", "ci/d/a.yml") and not m("ci/*.yml", "ci/a.yaml")
    assert m("ci/**/*.yml", "ci/a.yml") and m("ci/**/*.yml", "ci/d/e/a.yml") and not m("ci/**/*.yml", "x/ci/a.yml")
    assert m("a?.yml", "ab.yml") and not m("a?.yml", "a.yml") and not m("a?.yml", "a/.yml")
    assert m("a.b/*.yml", "a.b/c.yml") and not m("a.b/*.yml", "axb/c.yml")  # the rest is literal


CI_GLOB = "stages: [a]\ninclude:\n  - local: '/ci/*.yml'\n"


def test_gitlab_include_with_a_wildcard_reaches_every_file_it_matches():
    assert ("ci/*.yml", "include", "") in refs(facts(CI_GLOB, literal=True))
    job = lambda n: f"{n}:\n  stage: a\n  script: [echo]\n"  # noqa: E731
    files = {
        ".gitlab-ci.yml": CI_GLOB,
        "ci/one.yml": job("job1"),
        "ci/two.yml": job("job2"),
        "ci/deep/three.yml": job("job3"),
        "ci/notes.yaml": job("job4"),
    }
    # `*` is one segment and `.yaml` is not `.yml`: only the two files directly in ci/ are included
    assert _edges(files, "include") == {(".gitlab-ci.yml", "ci/one.yml"), (".gitlab-ci.yml", "ci/two.yml")}
    v, _ = _full(files)
    assert v["job:job1"] == v["job:job2"] == "alive"  # the verified counterexample: job1 was dead-cluster
    # relative to the including file's directory
    w = {"sub/.gitlab-ci.yml": CI_GLOB, "sub/ci/one.yml": job("job1")}
    assert _edges(w, "include") == {("sub/.gitlab-ci.yml", "sub/ci/one.yml")} and _full(w)[0]["job:job1"] == "alive"


def test_gitlab_include_forms_double_star_variable_list_and_trigger():
    ci = (
        "include:\n  - local: 'ci/**/*.yml'\n  - local: '/env/$ENV.yml'\n  - '/plain/p?.yml'\n  - local: [/multi/*.yml]\n"
        "  - remote: 'https://x/*.yml'\n  - template: Jobs/SAST.gitlab-ci.yml\nbuild:\n  script: [x]\n"
        "down:\n  trigger:\n    include: child/*.yml\n"
    )
    f = facts(ci, literal=True)
    assert {n for n, k, _ in refs(f) if k == "include"} == {
        "ci/**/*.yml",
        "env/*.yml",
        "plain/p?.yml",
        "multi/*.yml",
        "child/*.yml",
    }
    job = lambda n: f"{n}:\n  script: [echo]\n"  # noqa: E731
    files = {
        ".gitlab-ci.yml": ci,
        "ci/deep/a.yml": job("deep_job"),
        "env/prod.yml": job("env_job"),
        "plain/p1.yml": job("p_job"),
        "multi/m.yml": job("m_job"),
        "child/c.yml": job("child_job"),
        "other/o.yml": job("other_job"),
    }
    got = _edges(files, "include")
    assert got == {
        (".gitlab-ci.yml", f) for f in ("ci/deep/a.yml", "env/prod.yml", "plain/p1.yml", "multi/m.yml", "child/c.yml")
    }


def test_gha_computed_local_action_path_is_a_wildcard_include():
    wf = "on: push\njobs:\n  a:\n    steps:\n      - uses: ./.github/actions/${{ matrix.x }}\n"
    assert ("./.github/actions/*/action.yml", "include", "job:a") in refs(facts(wf, literal=True))
    act = "runs:\n  using: composite\n  steps: []\n"
    got = _edges(
        {".github/workflows/w.yml": wf, ".github/actions/a/action.yml": act, ".github/actions/b/action.yml": act},
        "include",
    )
    assert got == {(".github/workflows/w.yml#job:a", f".github/actions/{x}/action.yml") for x in "ab"}


def test_files_under_templates_are_helm_templates_whatever_their_extension():
    detect = cfg.dclangs.detect
    for p in (
        "c/templates/NOTES.txt",
        "c/templates/app.conf",
        "c/templates/dash.json",
        "c/templates/x.toml",
        "c/templates/sub/y.ini",
        "templates/NOTES.txt",
        "c/templates/README.md",
    ):
        assert detect(p) == "helm-template", p
    assert (
        detect("c/templates/a.yaml") == detect("c/templates/_h.tpl") == "yaml"
        and detect("c/templates/run.sh") == "shell"
    )
    assert (
        detect("docs/NOTES.txt") is None
        and detect("c/templatesx/app.conf") == "ini"
        and detect("a/templates.txt") is None
    )


def test_helm_template_facts_of_a_file_that_is_not_yaml():
    text = '{{- if .Values.ingress.enabled }}\nhost: {{ .Values.ingress.host }} <<< not: yaml [\n{{ include "c.name" . }}\n{{- end }}\n'
    f = facts(text, "helm-template", "NOTES.txt", literal=True)
    r = refs(f)
    assert ("values:ingress.enabled", "reads", "") in r and ("values:ingress.host.*", "reads", "") in r
    assert (
        ("tpl:c%2Ename", "ref", "") in r
        and roots(f) == {("template", "", "soft")}
        and f.errors == 0
        and f.lang == "helm-template"
    )


def test_values_read_only_by_notes_conf_json_or_a_templated_configmap_are_alive():
    chart = {
        "c/values.yaml": "a: 1\nb: 2\nc: 3\nd: 4\nunread: 5\n",
        "c/templates/NOTES.txt": "{{ .Values.a }}\n",
        "c/templates/app.conf": "x={{ .Values.b }}\n",
        "c/templates/dash.json": '{"d": {{ .Values.d | toJson }}}\n',
        "c/templates/cm.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Release.Name }}-cfg\ndata:\n  k: {{ .Values.c }}\n",
    }
    v, _ = _full(chart)
    assert [v[f"values:{k}"] for k in "abcd"] == ["weak-alive"] * 4  # the verified counterexample: all four were dead
    assert v["values:unread"].startswith("dead")  # a chart with a NOTES.txt keeps its dead-key detection
    only_cm = {k: x for k, x in chart.items() if k in ("c/values.yaml", "c/templates/cm.yaml")}
    assert (
        _full(only_cm)[0]["values:c"] == "weak-alive"
    )  # a ConfigMap named by the template: no def, the file is the root


def test_a_config_file_in_which_nothing_is_recognised_is_read_from_outside():
    for text in (
        "services:\n  web:\n    image: x\n",
        "version: 2\nupdates: []\n",
        "- hosts: all\n  tasks: []\n",
        "{}\n",
    ):
        assert roots(facts(text, literal=True)) == {("init", "", "soft")}, text
    assert roots(facts("a: {{ .Release.Name }}\n", "helm-values", literal=True)) == {("template", "", "soft")}
    assert not roots(
        facts("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n", literal=True)
    )  # an entry: judged by it
    _, raw = _full({"docker-compose.yml": "services: {}\n", "values-prod.yaml": "a: 1\n", "ci/lonely.yml": "k: v\n"})
    assert set(raw.values()) == {"weak-alive"}


def test_k8s_pvc_data_source_and_service_account_mappings_keep_the_object_alive():
    def pvc(n, extra=""):
        return (f"apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: {n}\n{extra}")  # noqa: E731
    dep = DEPLOY + "      volumes:\n        - name: v\n          persistentVolumeClaim:\n            claimName: clone\n"
    clone = pvc("clone", "spec:\n  dataSource:\n    kind: PersistentVolumeClaim\n    name: src\n")
    assert _k8s_dead(dep, clone, pvc("src"), pvc("lonely")) == {"PersistentVolumeClaim:lonely"}


def test_hcl_terraform_block_roots_its_file_and_an_unknown_file_is_read_from_outside(fake_hcl):
    f = facts('terraform {\n  required_version = ">= 1.5"\n}\n', "hcl", literal=True)
    assert roots(f) == {("init", "", "hard")} and not defs(f)
    for text in (
        'plugin "aws" {\n  enabled = true\n}\n',
        'run "plan" {\n  command = plan\n}\n',
    ):  # .tflint.hcl, *.tftest.hcl
        assert roots(facts(text, "hcl", literal=True)) == {("init", "", "soft")}, text
    assert not roots(facts('variable "v" {}\n', "hcl", literal=True))  # an entry: judged by it
    v, _ = _full({"versions.tf": 'terraform {\n  required_version = ">= 1"\n}\nvariable "idle" {}\n'})
    assert (
        v["var:idle"].startswith("dead") and _full({"versions.tf": "terraform {\n}\n"})[1]["hcl:versions.tf"] == "alive"
    )


def test_helm_requirements_yaml_and_string_import_values_keep_the_subchart_keys():
    req = "dependencies:\n  - name: redis\n    repository: https://x\n    condition: redis.enabled\n    import-values:\n      - data\n"
    f = facts(req, literal=True)
    assert roots(f) == {("init", "", "hard")} and ("values:redis.enabled", "reads", "") in refs(f)
    assert ("values:exports.data", "reads", "") in refs(f) and ("values:redis", "reads", "") in refs(f)
    assert roots(facts("dependencies:\n  - numpy\n  - pandas\n", literal=True)) == {("init", "", "soft")}  # a conda env
    chart = "apiVersion: v2\nname: u\nversion: 1\ndependencies:\n  - name: db\n    import-values:\n      - child: default.data\n        parent: dbData\n"
    sub = {
        "u/charts/db/values.yaml": "default:\n  data: {x: 1}\n",
        "u/templates/n.txt": "{{ .Values.own }}\n",
        "u/charts/db/templates/n.txt": "{{ .Values.z }}\n",
    }
    v, _ = _full({"u/Chart.yaml": chart, "u/values.yaml": "dbData: {a: 1}\nown: 1\nunread: 2\n", **sub})
    assert v["values:dbData.a"] == v["values:own"] == "weak-alive" and v["values:unread"].startswith("dead")
    assert v["values:default.data.x"] == "weak-alive"  # the child path the parent imports
    w, _ = _full({"u/Chart.yaml": chart.replace("default.data", "other"), "u/values.yaml": "own: 1\n", **sub})
    assert w["values:default.data.x"].startswith("dead")  # not imported: a dead key of the subchart


# ====================================================================== second pass
ALIVE = ("alive", "weak-alive")


def _exec_names(text):
    return {n for n, k, _ in refs(facts(text, literal=True)) if k == "exec"}


def test_scripts_run_through_a_path_variable_are_exec_refs():
    act = (
        "name: a\nruns:\n  using: composite\n  steps:\n"
        "    - run: ${{ github.action_path }}/scripts/x.sh\n      shell: bash\n"
        "    - run: $GITHUB_ACTION_PATH/scripts/y.sh\n      shell: bash\n"
        '    - run: bash "${GITHUB_ACTION_PATH}/scripts/z.sh" arg\n      shell: bash\n'
    )
    wf = "on: push\njobs:\n  a:\n    steps:\n      - run: $GITHUB_WORKSPACE/scripts/v.sh\n"
    ci = "build:\n  script:\n    - $CI_PROJECT_DIR/scripts/g.sh\n    - ${CI_PROJECT_DIR}/ci/h.sh --x\n"
    assert _exec_names(act) == {"scripts/x.sh", "scripts/y.sh", "scripts/z.sh"}
    assert _exec_names(wf) == {"scripts/v.sh"} and _exec_names(ci) == {"scripts/g.sh", "ci/h.sh"}
    for noise in (
        "echo $HOME/notes.txt",
        'echo "$A/b and ${{ x }}/c"',
        "ls ${DIR}/data",
    ):  # a variable alone is no script
        assert _exec_names(f"on: push\njobs:\n  a:\n    steps:\n      - run: {noise}\n") == set(), noise
    files = {"action.yml": act, ".github/workflows/w.yml": wf, ".gitlab-ci.yml": ci, "scripts/unused.sh": "echo\n"}
    files.update({f"scripts/{n}.sh": "echo\n" for n in "xyzvg"} | {"ci/h.sh": "echo\n"})
    _, raw = _full(files)  # the verified counterexample: x, y, z, v were all dead
    assert all(raw[f"shell:scripts/{n}.sh"] in ALIVE for n in "xyzvg") and raw["shell:ci/h.sh"] in ALIVE
    assert raw["shell:scripts/unused.sh"] == "dead"


def test_helm_files_a_template_reads_are_include_refs_from_the_chart_root():
    tpl = (
        'data:\n  a: {{ .Files.Get "files/a.conf" | quote }}\n  {{- (.Files.Glob "dash/*.json").AsConfig | nindent 2 }}\n'
        '  b: {{ .Files.Get (printf "files/%s.conf" .Values.n) }}\n'
        '  c: {{ include (print $.Template.BasePath "/extra.yaml") . | sha256sum }}\n  d: {{ .Files.Get .Values.dyn }}\n'
    )
    inc = {n for n, k, _ in refs(facts(tpl, "helm-template", "x", literal=True)) if k == "include"}
    assert inc == {
        "files/a.conf",
        "extra.yaml",
        "../dash/*.json",
        "../../dash/*.json",
        "../files/*.conf",
        "../../files/*.conf",
    }
    cm = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Release.Name }}\n" + tpl
    chart = {
        "c/templates/cm.yaml": cm,
        "c/templates/extra.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n",
    }
    chart |= {"c/files/a.conf": "a\n", "c/files/b.conf": "b\n", "c/dash/d.json": "{}\n", "c/other/n.conf": "n\n"}
    literal = {
        **chart,
        "c/templates/cm.yaml": cm.replace("{{ .Release.Name }}", "cfg"),
    }  # a name nothing reads: a template renders
    assert _full(literal)[1]["ini:c/files/a.conf"] in ALIVE
    _, raw = _full(chart)  # the verified counterexample: chart/files/a.conf was dead
    assert all(raw[k] in ALIVE for k in ("ini:c/files/a.conf", "ini:c/files/b.conf", "json:c/dash/d.json"))
    assert raw["ini:c/other/n.conf"] == "dead"  # a file nothing reads stays dead
    glob = {
        "c/templates/NOTES.txt": '{{ range $p, $_ := .Files.Glob "docs/**" }}{{ end }}\n',
        "c/docs/a/b.json": "{}\n",
    }
    assert _full(glob)[1]["json:c/docs/a/b.json"] in ALIVE  # a template that is not YAML reads files too


KZ = (
    "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n  - cm2.yaml\n  - ../base\n"
    "  - https://example.com/x.yaml\npatches:\n  - path: p1.yaml\n    target: {kind: Deployment}\n"
    "  - patch: |-\n      - op: add\n        path: /spec\npatchesStrategicMerge:\n  - p2.yaml\n"
    "configMapGenerator:\n  - name: g\n    files:\n      - app.conf\n      - key=conf/k.conf\n    envs: [g.env]\n"
    "helmCharts:\n  - name: c\n    repo: https://x\n    valuesFile: hv.yaml\n"
)


def test_kustomization_lists_the_files_it_builds_as_include_refs():
    inc = {n for n, k, _ in refs(facts(KZ, literal=True)) if k == "include"}
    assert inc == {
        "cm2.yaml",
        "../base",
        "../base/kustomization.yaml",
        "../base/kustomization.yml",
        "../base/Kustomization",
        "p1.yaml",
        "p2.yaml",
        "app.conf",
        "conf/k.conf",
        "g.env",
        "hv.yaml",
    }
    bare = "resources:\n  - cm2.yaml\nnamePrefix: x-\n"  # without apiVersion and kind
    f = facts(bare, literal=True)
    assert {n for n, k, _ in refs(f) if k == "include"} == {"cm2.yaml"} and roots(f) == {("init", "", "soft")}
    assert not [r for r in facts("resources:\n  - .\n  - ./\n", literal=True).refs if r.kind == "include"]
    values = "resources:\n  limits: {cpu: 1}\n"  # a Helm value named like a kustomize key
    assert not [r for r in facts(values, "helm-values", literal=True).refs]
    assert not [r for r in facts(values, literal=True).refs if r.kind == "include"]


def test_a_file_listed_in_a_kustomization_is_not_dead():
    cm = lambda n: f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {n}\n"  # noqa: E731
    dep = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"
    files = {
        "k/kustomization.yaml": KZ,
        "k/cm2.yaml": cm("cm2"),
        "k/p1.yaml": dep,
        "k/app.conf": "a=1\n",
        "k/stray.yaml": cm("stray"),
        "base/kustomization.yaml": "resources:\n  - d.yaml\n",
        "base/d.yaml": dep.replace("web", "d"),
    }
    assert _edges(files, "include") == {
        ("k/kustomization.yaml", "k/cm2.yaml"),
        ("k/kustomization.yaml", "base/kustomization.yaml"),
        ("k/kustomization.yaml", "k/p1.yaml"),
        ("k/kustomization.yaml", "k/app.conf"),
        ("base/kustomization.yaml", "base/d.yaml"),
    }
    v, raw = _full(files)  # the verified counterexample: cm2.yaml was dead-cluster
    assert raw["yaml:k/cm2.yaml"] in ALIVE and raw["ini:k/app.conf"] in ALIVE and raw["yaml:base/d.yaml"] == "alive"
    assert raw["yaml:k/stray.yaml"] == "dead-cluster" and v["ConfigMap:stray"].startswith("dead")
    assert v["ConfigMap:cm2"].startswith("dead")  # an object that nothing reads is still judged by its readers


def test_flux_and_argo_deploy_a_directory_of_the_tree():
    flux = (
        "apiVersion: kustomize.toolkit.fluxcd.io/v1\nkind: Kustomization\nmetadata:\n  name: apps\nspec:\n  path: ./clusters/prod\n"
        "  sourceRef: {kind: GitRepository, name: x}\n"
    )
    argo = (
        "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: a\nspec:\n  source:\n    repoURL: x\n"
        "    path: deploy/app\n  sources:\n    - path: deploy/b\n    - chart: c\n"
    )
    assert ("./clusters/prod/**", "include", "Kustomization:apps") in refs(facts(flux, literal=True))
    assert {n for n, k, _ in refs(facts(argo, literal=True)) if k == "include"} == {"deploy/app/**", "deploy/b/**"}
    cm = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {}\n"
    files = {
        "gitops/flux.yaml": flux,
        "clusters/prod/cm.yaml": cm.format("prod"),
        "clusters/dev/cm.yaml": cm.format("dev"),
    }
    _, raw = _full(files)
    assert raw["yaml:clusters/prod/cm.yaml"] in ALIVE and raw["yaml:clusters/dev/cm.yaml"] == "dead-cluster"


def test_a_gitlab_file_under_another_name_is_not_dead_for_want_of_an_includer():
    pipeline = "stages: [a]\nbuild:\n  stage: a\n  script: [make]\n.hidden:\n  script: [x]\n"
    shared = "deploy:\n  stage: a\n  script: [kubectl apply]\n"  # a template other projects pull in: include: project:
    v, raw = _full({"ci/pipeline.yml": pipeline, "templates/deploy.yml": shared})
    assert raw["yaml:ci/pipeline.yml"] in ALIVE and raw["yaml:templates/deploy.yml"] in ALIVE  # both were dead-cluster
    assert v["job:build"] in ALIVE and v["job:deploy"] in ALIVE and v["job:%2Ehidden"].startswith("dead")
    assert roots(facts(pipeline, literal=True)) == {("ci-job", "", "soft")}
    _, raw2 = _full({".gitlab-ci.yml": pipeline})  # the pipeline by its name keeps its hard root next to the soft one
    assert raw2["yaml:.gitlab-ci.yml"] == "alive"


def test_gitlab_include_path_that_starts_with_a_variable_matches_from_the_root():
    ci = "include:\n  - local: $CI_PROJECT_DIR/ci/one.yml\n  - local: '/env/${ENV}/x.yml'\n  - local: /env/$ENV.yml\n  - local: ci/a-$B.yml\n"
    inc = {n for n, k, _ in refs(facts(ci, literal=True)) if k == "include"}
    assert inc == {
        "**/ci/one.yml",
        "env/**/x.yml",
        "env/*.yml",
        "ci/a-*.yml",
    }  # a whole segment is `**`, a part of one is `*`
    job = "j:\n  script: [x]\n"
    files = {
        ".gitlab-ci.yml": ci,
        "ci/one.yml": job,
        "env/prod/x.yml": job,
        "env/stg.yml": job,
        "ci/a-1.yml": job,
        "ci/other.yml": job,
    }
    got = {dst for _, dst in _edges(files, "include")}  # the verified counterexample: ci/one.yml was dead-cluster
    assert got == {"ci/one.yml", "env/prod/x.yml", "env/stg.yml", "ci/a-1.yml"}
    assert {d for _, d in _edges({".gitlab-ci.yml": ci, "x/ci/one.yml": job, "env/x.yml": job}, "include")} >= {
        "x/ci/one.yml"
    }


PACKER = 'variable "ami" {}\nvariable "idle" {}\nsource "amazon-ebs" "x" {\n  ami_name = var.ami\n}\nbuild {\n  sources = ["source.amazon-ebs.x"]\n}\n'
NOMAD = 'variable "img" {}\njob "web" {\n  image = var.img\n}\n'


def test_hcl_of_another_tool_roots_its_file_softly_and_terraform_blocks_alone_do_not(fake_hcl):
    for text in (PACKER, NOMAD):
        assert roots(facts(text, "hcl", literal=True)) == {("init", "", "soft")}, text
    assert not roots(
        facts('variable "v" {}\nlocals {\n  a = var.v\n}\n', "hcl", literal=True)
    )  # Terraform: judged by entries
    both = facts(PACKER + 'terraform {\n  required_version = ">= 1"\n}\n', "hcl", literal=True)
    assert roots(both) == {("init", "", "hard")}  # the order of the blocks does not matter
    assert roots(facts("terraform {\n}\n" + PACKER, "hcl", literal=True)) == {("init", "", "hard")}
    v, _ = _full({"build.pkr.hcl": PACKER})  # the verified counterexample: the file and `ami` were dead-cluster
    assert v["var:ami"] in ALIVE and v["var:idle"].startswith("dead")
    w, _ = _full({"web.nomad.hcl": NOMAD})
    assert w["var:img"] in ALIVE


TF_FILES = (
    'resource "aws_instance" "w" {\n  user_data = templatefile("${path.module}/templates/boot.sh.tpl", { a = 1 })\n'
    '  policy = file("${path.module}/../policy.json")\n  rules = fileset("${path.module}/rules", "*.json")\n'
    '  extra = file("${path.module}/files/${var.env}.json")\n  provisioner "local-exec" {\n'
    '    command = "${path.module}/scripts/run.sh ${self.id}"\n  }\n}\n'
)


def test_terraform_file_functions_and_local_exec_are_refs_to_files_of_the_tree(fake_hcl):
    r = refs(facts(TF_FILES, "hcl", literal=True))
    inc = {n for n, k, _ in r if k == "include"}
    assert inc == {"templates/boot.sh.tpl", "../policy.json", "rules/**", "files/*.json"}
    assert ("scripts/run.sh", "exec", "resource:aws_instance:w") in r
    f = facts('output "o" {\n  value = file(var.p)\n}\n', "hcl", literal=True)  # not a literal: nothing to resolve
    assert not [x for x in refs(f) if x[1] in ("include", "exec")]
    files = {
        "m/main.tf": TF_FILES,
        "policy.json": "{}\n",
        "m/rules/a.json": "{}\n",
        "m/files/prod.json": "{}\n",
        "m/scripts/run.sh": "echo\n",
        "m/rules/sub/deep.json": "{}\n",
        "m/other.json": "{}\n",
    }
    _, raw = _full(files)
    for k in (
        "json:policy.json",
        "json:m/rules/a.json",
        "json:m/files/prod.json",
        "shell:m/scripts/run.sh",
        "json:m/rules/sub/deep.json",
    ):
        assert raw[k] in ALIVE, k
    assert raw["json:m/other.json"] == "dead"


def test_workflow_step_inputs_that_name_a_file_are_include_refs():
    wf = (
        "on: push\njobs:\n  a:\n    steps:\n      - uses: docker/build-push-action@v5\n        with:\n          file: ./docker/Dockerfile\n"
        "          context: .\n          config-file: ${{ github.workspace }}/.lint.yml\n          tags: x\n"
    )
    inc = {n for n, k, _ in refs(facts(wf, literal=True)) if k == "include"}
    assert inc == {"./docker/Dockerfile", "*/.lint.yml"}
    act = (
        "runs:\n  using: composite\n  steps:\n    - uses: x/y@v1\n      with:\n        dockerfile: docker/Dockerfile\n"
    )
    assert ("docker/Dockerfile", "include", "") in refs(facts(act, literal=True))
    _, raw = _full({".github/workflows/w.yml": wf, "docker/Dockerfile": "FROM x\n", "other/Dockerfile": "FROM y\n"})
    assert raw["dockerfile:docker/Dockerfile"] in ALIVE and raw["dockerfile:other/Dockerfile"] == "dead"


def test_a_configmap_or_secret_a_controller_reads_by_label_is_not_dead():
    def obj(kind, n, lab):
        return (f"apiVersion: v1\nkind: {kind}\nmetadata:\n  name: {n}\n  labels:\n    {lab}\n")  # noqa: E731
    dead = _k8s_dead(
        obj("ConfigMap", "dash", 'grafana_dashboard: "1"'),
        obj("Secret", "repo", "argocd.argoproj.io/secret-type: repository"),
        obj("ConfigMap", "plain", "app: x"),
        obj("ConfigMap", "near", "grafana_dashboards_x: y"),  # the key is matched whole
    )
    assert dead == {"ConfigMap:plain", "ConfigMap:near"}


# ====================================================================== second pass, round 2
MOD = {"pkg/main.py", "pkg/main/__main__.py", "pkg/main/__init__.py"}
TF_EXEC = 'resource "null_resource" "r" {\n  provisioner "local-exec" {\n    command = "%s"\n  }\n}\n'
CONTEXT = {
    "gha": lambda c: f"on: push\njobs:\n  a:\n    steps:\n      - run: {c}\n",
    "gitlab": lambda c: f"build:\n  script:\n    - {c}\n",
    "action": lambda c: f"runs:\n  using: composite\n  steps:\n    - run: {c}\n      shell: bash\n",
    "terraform": lambda c: TF_EXEC % c,
}
INTERPRETERS = (
    ("python build.py", {"build.py"}),
    ("python manage.py migrate", {"manage.py"}),
    ("python3 -u -W ignore tools/gen.py --x", {"tools/gen.py"}),
    ("node src/x.js", {"src/x.js"}),
    ("node dist/server.js", {"dist/server.js"}),
    ("ruby gen.rb", {"gen.rb"}),
    ("perl x.pl", {"x.pl"}),
    ("python -m pkg.main", MOD),
    ("python3 -m pkg.main --x", MOD),
    ("FOO=1 sudo -E python build.py && node tools.js", {"build.py", "tools.js"}),
    ("cd sub && ruby gen.rb; perl x.pl | tee log", {"gen.rb", "x.pl"}),
    ("sh -c 'node tools.js'", {"tools.js"}),
    ("poetry run python build.py && uv run gen.py", {"build.py", "gen.py"}),  # a launcher (data frameworks.json)
    ("npx tsx src/x.ts; bundle exec ruby gen.rb; deno run -A main.ts", {"src/x.ts", "gen.rb", "main.ts"}),
    ("python -c 'print(1)'", set()),  # inline code names no file
    ("node -e 'x()'", set()),
    ("echo python build.py", set()),  # an argument of echo is no command
    ("git log -- build.py", set()),
)


def _runs_in(ctx, cmd):
    lang = "hcl" if ctx == "terraform" else "yaml"
    return {n for n, k, _ in refs(facts(CONTEXT[ctx](cmd), lang, literal=True)) if k == "exec"}


@pytest.mark.parametrize("ctx", sorted(CONTEXT))
def test_the_script_or_module_an_interpreter_runs_is_an_exec_ref(ctx, fake_hcl):
    """The verified counterexample: `python build.py`, `node tools.js`, `ruby gen.rb`, `perl x.pl`, `python -m pkg.main` gave no
    exec ref in a workflow, a GitLab job, a composite action and a Terraform local-exec."""
    for cmd, want in INTERPRETERS:
        assert _runs_in(ctx, cmd) == want, (ctx, cmd)


def test_a_variable_in_front_of_the_script_an_interpreter_runs_is_dropped(fake_hcl):
    for ctx, var in (
        ("action", "${GITHUB_ACTION_PATH}"),
        ("gha", "$GITHUB_WORKSPACE"),
        ("gitlab", "${CI_PROJECT_DIR}"),
    ):
        assert _runs_in(ctx, f"python {var}/gen.py") == {"gen.py"}, ctx
        assert _runs_in(ctx, f"node {var}/tools/x.js --y") == {"tools/x.js"}, ctx
    assert _runs_in("action", "ruby ${{ github.action_path }}/scripts/gen.rb") == {"scripts/gen.rb"}
    assert _runs_in("gha", "python scripts/${{ matrix.n }}.py") == {"scripts/*.py"}  # a computed name is a wildcard
    assert _runs_in("terraform", "python3 ${path.module}/foo.py") == {"foo.py"}
    assert _runs_in("terraform", "node ${path.module}/js/${var.n}.js ${self.id}") == {"js/*.js"}
    for noise in ("python ${{ matrix.script }}", "python $SCRIPT", "python ${DIR}/${NAME}", "node $A/$B"):
        assert _runs_in("gha", noise) == set(), noise  # nothing literal to look up: never a wildcard of every file
    assert _runs_in("terraform", "arn:aws:s3:::${var.b}/${var.k}") == set()


def test_a_shell_text_is_read_line_by_line_with_its_continuations():
    text = (
        "on: push\njobs:\n  a:\n    steps:\n      - run: |\n          set -e\n          # python skipped.py\n"
        "          if [ -f x ]; then python y.py; fi\n          for f in a b; do\n            ruby $f.rb\n          done\n"
        "          python \\\n            long.py --x\n          node a.js\n          perl b.pl\n"
    )
    assert _exec_names(text) == {"y.py", "long.py", "a.js", "b.pl"}


def test_a_command_list_is_one_argv_and_a_terraform_list_too(fake_hcl):
    ex = _exec_names("services:\n  web:\n    command: [python, -m, pkg.main]\n    entrypoint: ['node', 'tools/x.js']\n")
    assert ex == MOD | {"tools/x.js"}
    tf = 'data "external" "g" {\n  program = ["python3", "${path.module}/gen.py"]\n}\n'
    assert {n for n, k, _ in refs(facts(tf, "hcl", literal=True)) if k == "exec"} == {"gen.py"}
    assert cfg._hcl_runs("<<-EOT\n  cd ${path.module}\n  python3 ${path.module}/scripts/gen.py\nEOT\n") == [
        "scripts/gen.py"
    ]


def test_scripts_run_by_an_interpreter_are_not_dead(fake_hcl):
    """The verified counterexamples through link + reach with the hints of the roots layer: the script was `dead`."""
    cases = {
        "gha": (".github/workflows/ci.yml", "python build.py", {"build.py": "python"}),
        "gha-node": (".github/workflows/ci.yml", "node tools.js", {"tools.js": "javascript"}),
        "gitlab": (".gitlab-ci.yml", "python build.py", {"build.py": "python"}),
        "action": ("action.yml", "python ${GITHUB_ACTION_PATH}/gen.py", {"gen.py": "python"}),
        "terraform": ("main.tf", "python3 ${path.module}/foo.py", {"foo.py": "python"}),
        "shell": (".github/workflows/ci.yml", "bash x.sh && python3 a/b.py", {"x.sh": "shell", "a/b.py": "python"}),
        "module": (".github/workflows/ci.yml", "python -m pkg.main", {"pkg/main.py": "python"}),
    }
    for name, (path, cmd, scripts) in cases.items():
        lang = "hcl" if path.endswith(".tf") else "yaml"
        text = CONTEXT[
            (
                "gha"
                if "workflows" in path
                else "gitlab" if "gitlab" in path else "action" if lang == "yaml" else "terraform"
            )
        ](cmd)
        files = {path: text, "unused.py": "print('x')\n", **{p: "print('hi')\n" for p in scripts}}
        _, raw = _full(files)
        for p, language in scripts.items():
            assert raw[f"{language}:{p}"] in ALIVE, (name, p)
        assert raw["python:unused.py"] == "dead", name


def _deploy(body):
    return f"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: d\nspec:\n  replicas: {body}\n"


def test_a_values_leaf_read_one_level_deeper_is_alive():
    """The verified counterexample: `foo: {}` read as `.Values.foo.x` gave values:foo `dead`."""
    r = refs(facts("a: {{ .Values.foo.bar.x }}\n", "helm-template", "x", literal=True))
    assert {n for n, k, _ in r if k == "reads"} == {
        "values:foo",
        "values:foo.bar",
        "values:foo.bar.x",
        "values:foo.bar.x.*",
    }
    tpl = _deploy("{{ .Values.foo.x }}{{ .Values.bar.y.z }}{{ .Values.baz.q }}{{ .Values.nest.a.deep }}")
    chart = {
        "c/values.yaml": "foo: {}\nbar: null\nbaz: []\nnest:\n  a:\n  b: 1\nunused: 1\nunused_map:\n  k: 1\n",
        "c/templates/d.yaml": tpl,
    }
    v, _ = _full(chart)
    assert all(v[f"values:{k}"] in ALIVE for k in ("foo", "bar", "baz", "nest.a")), v
    assert v["values:nest.b"].startswith("dead") and v["values:unused"].startswith("dead")  # a sibling is not read
    assert v["values:unused_map.k"].startswith("dead")


HELPERS = (
    '{{- define "a.labels" }}x{{ end }}\n{{- define "b.labels" }}y{{ end }}\n{{- define "a.unused" }}z{{ end }}\n'
    '{{- define "a.name" }}n{{ end }}\n'
)


def _tpl_verdicts(body):
    v, _ = _full({"c/templates/_helpers.tpl": HELPERS, "c/templates/d.yaml": _deploy(body)})
    return {n: v[f"tpl:{n}"] for n in ("a%2Elabels", "b%2Elabels", "a%2Eunused", "a%2Ename")}


def test_a_template_included_by_a_computed_name_is_read():
    """The verified counterexample: `include (printf "%s.labels" .Chart.Name) .` left every define `dead`."""
    got = _tpl_verdicts('{{ include (printf "%s.labels" .Chart.Name) . }}')
    assert [got[n] in ALIVE for n in ("a%2Elabels", "b%2Elabels")] == [True, True]  # the printf format is the glob
    assert got["a%2Eunused"].startswith("dead") and got["a%2Ename"].startswith("dead")
    assert all(x in ALIVE for x in _tpl_verdicts("{{ include $name . }}").values())  # nothing is known: every define
    assert all(x in ALIVE for x in _tpl_verdicts("{{ include (print .Values.kind) . | nindent 2 }}").values())
    assert all(x in ALIVE for x in _tpl_verdicts('{{ include (printf "%s" (include "x" .)) . }}').values())
    # the checksum idiom names a FILE of the chart (.Files refs): no define is read by it
    sha = '{{ include (print $.Template.BasePath "/cm.yaml") . | sha256sum }}'
    assert all(x.startswith("dead") for x in _tpl_verdicts(sha).values())
    assert _tpl_verdicts('{{ include "a.name" . }}')["a%2Eunused"] not in ALIVE  # a literal name still names one define
    f = facts('{{ include (printf "%s.l-%d" . 1) . }}', "helm-template", "x", literal=True)
    assert [d.pattern for d in f.dyn] == ["name:tpl:*%2El-*"]


def test_a_subchart_in_charts_without_a_dependencies_entry_reads_the_parent_values():
    """The verified counterexample: the parent values `sub.x` came out dead-cluster."""
    chart = {
        "c/Chart.yaml": "apiVersion: v2\nname: parent\nversion: 1.0.0\n",
        "c/values.yaml": "sub:\n  x: 1\nglobal:\n  g: 2\nother:\n  y: 3\nghost:\n  z: 4\n",
        "c/templates/d.yaml": _deploy("{{ .Values.other.y }}"),
        "c/charts/sub/Chart.yaml": "apiVersion: v2\nname: sub\nversion: 0.1.0\n",
        "c/charts/sub/values.yaml": "x: 5\n",
        "c/charts/sub/templates/d.yaml": _deploy("{{ .Values.x }}{{ .Values.global.g }}"),
    }
    v, _ = _full(chart)
    assert v["values:sub.x"] in ALIVE and v["values:global.g"] in ALIVE and v["values:other.y"] in ALIVE
    assert v["values:ghost.z"].startswith("dead")  # the parent's own unread key stays dead
    f = facts("apiVersion: v2\nname: sub\nversion: 1\n", "yaml", "Chart.yaml", literal=True)
    assert {("values:sub.*", "reads", ""), ("values:global.*", "reads", "")} <= refs(f)
    assert not [
        r for r in refs(facts("dependencies:\n  - name: x\n    version: 1\n", literal=True)) if "parent" in r[0]
    ]


def test_container_paths_of_a_docker_action_are_looked_up_by_their_file_name():
    """The verified counterexample: `entrypoint: /entrypoint.sh` resolved to nothing, so entrypoint.sh was dead."""
    act = (
        "name: d\nruns:\n  using: docker\n  image: Dockerfile\n  entrypoint: /entrypoint.sh\n"
        "  pre-entrypoint: /usr/local/bin/pre.sh\n  post-entrypoint: post.sh\n"
    )
    assert {n for n, k, _ in refs(facts(act, literal=True)) if k == "exec"} == {
        "Dockerfile",
        "entrypoint.sh",
        "pre.sh",
        "post.sh",
    }
    files = {"action.yml": act, "Dockerfile": "FROM x\n", "entrypoint.sh": "echo\n", "scripts/pre.sh": "echo\n"}
    _, raw = _full({**files, "post.sh": "echo\n", "other.sh": "echo\n"})
    assert all(raw[f"shell:{p}"] in ALIVE for p in ("entrypoint.sh", "scripts/pre.sh", "post.sh"))
    assert raw["shell:other.sh"] == "dead"
    node = "runs:\n  using: node20\n  main: dist/index.js\n  post: ./dist/cleanup.js\n"
    assert {n for n, k, _ in refs(facts(node, literal=True)) if k == "exec"} == {"dist/index.js", "./dist/cleanup.js"}


def test_commands_of_a_k8s_object_a_compose_file_or_a_playbook_are_exec_refs():
    dep = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: d\nspec:\n  template:\n    spec:\n      containers:\n"
        "        - name: c\n          command: [python, tools/job.py]\n          args: ['--x']\n"
    )
    cm = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\ndata:\n  command: python tools/data.py\n"
    compose = "services:\n  web:\n    command: python manage.py runserver\n    entrypoint: [node, tools/x.js]\n"
    play = "- hosts: all\n  tasks:\n    - command: ruby gen.rb\n    - script: files/y.sh\n"
    assert ("tools/job.py", "exec", "Deployment:d") in refs(facts(dep, literal=True))
    assert "tools/data.py" not in _exec_names(cm)  # the data of a ConfigMap is not entered
    assert _exec_names(compose) == {"manage.py", "tools/x.js"} and _exec_names(play) == {"gen.rb", "files/y.sh"}
    assert _exec_names(
        "a:\n  command: python x.py\n",
    ) == {"x.py"}
    assert not [
        r for r in refs(facts("command: python x.py\n", "helm-values", "values.yaml", literal=True)) if r[1] == "exec"
    ]
    files = {
        "k.yaml": dep,
        "docker-compose.yml": compose,
        "play.yml": play,
        "tools/job.py": "print(1)\n",
        "manage.py": "print(1)\n",
        "tools/x.js": "x\n",
        "files/y.sh": "echo\n",
        "tools/unused.py": "print(1)\n",
    }
    _, raw = _full(files)
    assert all(
        raw[k] in ALIVE
        for k in ("python:tools/job.py", "python:manage.py", "javascript:tools/x.js", "shell:files/y.sh")
    )
    assert raw["python:tools/unused.py"] == "dead"


def test_a_templates_directory_without_a_chart_stays_a_soft_root():
    """langs.json in_dir `templates` claims any such directory (Ansible, cookiecutter ...): the file is read by an engine that
    is outside the tree, so it is weak-alive, never dead. A chart is told apart only by the tree, which a language is not.
    """
    files = {
        "roles/web/templates/nginx.conf.j2": "server { listen {{ port }}; }\n",
        "roles/web/templates/motd.cfg": "banner={{ port }}\n",
        "roles/web/templates/a.toml": "x=1\n",
    }
    tg.set_backend(
        lambda grammar, data: NoMatch()
    )  # a .j2 file is jinja2 (graph.extract.tpl): its soft root needs no match
    try:
        _, raw = _full(files)
    finally:
        tg.set_backend(None)
    assert set(raw.values()) == {"weak-alive"}, raw


class NoMatch:
    """A parse in which no query matches (the template of the test above holds no statement)."""

    errors = 0

    def run(self, kind):
        return []


def test_the_scripts_every_gitlab_job_runs_are_exec_refs_under_default_too():
    ci = "default:\n  before_script:\n    - python setup.py\n  after_script: [node clean.js]\nbuild:\n  script: [x]\n"
    assert _exec_names(ci) == {"setup.py", "clean.js"}
    assert _exec_names("before_script:\n  - ruby pre.rb\nbuild:\n  script: [x]\n") == {"pre.rb"}


def test_commands_of_other_yaml_tools_pre_commit_travis_circleci_taskfile(fake_hcl):
    pre = "repos:\n  - repo: local\n    hooks:\n      - id: chk\n        entry: python tools/check.py\n        language: system\n"
    travis = "language: python\ninstall: pip install -r r.txt\nscript:\n  - python -m pkg.main\n  - node ci/run.js\n"
    circle = "jobs:\n  b:\n    steps:\n      - run: ruby gen.rb\n      - run:\n          name: n\n          command: perl x.pl\n"
    task = "version: '3'\ntasks:\n  b:\n    cmds:\n      - python build.py\n      - cmd: node t.js\n"
    assert _exec_names(pre) == {"tools/check.py"} and _exec_names(travis) == MOD | {"ci/run.js"}
    assert _exec_names(circle) == {"gen.rb", "x.pl"} and _exec_names(task) == {"build.py", "t.js"}
    tf = 'resource "null_resource" "r" {\n  provisioner "remote-exec" {\n    script = "${path.module}/run.py"\n  }\n'
    tf += '  tags = { Name = "web/${var.env}", log = "logs/${var.x}" }\n  cidr = "10.0.0.0/8"\n}\n'
    got = {n for n, k, _ in refs(facts(tf, "hcl", literal=True)) if k == "exec"}
    assert got == {
        "run.py"
    }  # a string that is no command or script (a name, a key, a CIDR) is no ref, a wildcard least of all


def test_the_cache_key_covers_the_data_of_the_command_parser(tmp_path, monkeypatch):
    """The exec refs depend on frameworks.json (interpreters, launchers): a changed file must not be served from a shard."""
    ex = contracts.get_extractor("yaml")
    before = ex.grammar_sha("yaml")
    alt = tmp_path / "frameworks.json"
    alt.write_text(Path(cfg.ROOTS_DATA).read_text() + " ")
    monkeypatch.setattr(cfg, "ROOTS_DATA", str(alt))
    assert ex.grammar_sha("yaml") != before


# ====================================================================== counterexamples of an earlier re-check
def _chart_full(files):
    """link + reach as _full, but a file with no config language is a bare file node (nothing reads it by itself)."""
    fs, hs = {}, {}
    for p, t in files.items():
        lang = cfg.dclangs.detect(p)
        fs[p] = contracts.get_extractor(lang).extract(lang, p, t) if lang in cfg.Cfg.langs else md.Facts(lang or "text")
        hs[p] = rt.extract_hints(p, t)
    res = dlink.link(fs, hs, workers=1)
    assert not res.findings, res.findings
    # a def by its qual, a file by its path; a name that is read across files is an N edge: weak-alive, which still is not dead
    return {
        k.split("#", 1)[1] if "#" in k else k.split(":", 1)[1]: x for k, x in rc.analyze(res.graph).verdicts().items()
    }, fs


H_CHART = "apiVersion: v2\nname: c\nversion: 0.1.0\n"
H_OBJ = "apiVersion: v1\nkind: %s\nmetadata:\n  name: %s\n"
H_DEPLOY = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  template:\n    spec:\n      serviceAccountName: %s\n"
    "      containers:\n        - name: web\n          image: x\n          envFrom:\n            - configMapRef:\n                name: %s\n"
    "            - secretRef:\n                name: %s\n"
)
H_REFS = {  # (serviceAccountName, configMapRef name, secretRef name) as the template writes them
    "values": ("{{ .Values.sa }}", "{{ .Values.cfgName }}", '{{ include "x" . }}-secret'),
    "release": ("{{ .Release.Name }}-sa", "{{ .Release.Name }}-config", "{{ .Release.Name }}-secret"),
    "default": (
        '{{ .Values.sa | default "app-sa" }}',
        '{{ default "app-config" .Values.cfg }}',
        '{{ .Values.existingSecret | default "app-secret" }}',
    ),
}


@pytest.mark.parametrize("case", sorted(H_REFS))
def test_helm_literal_named_objects_read_by_a_templated_reference_are_not_dead(case):
    """Counterexample: a ConfigMap, a Secret and a ServiceAccount with literal names, read by envFrom / serviceAccountName whose
    name the template computes, were dead-cluster with their files."""
    sa, cmn, sec = H_REFS[case]
    chart = {
        "c/Chart.yaml": H_CHART,
        "c/templates/cm.yaml": H_OBJ % ("ConfigMap", "app-config"),
        "c/templates/sec.yaml": H_OBJ % ("Secret", "app-secret"),
        "c/templates/sa.yaml": H_OBJ % ("ServiceAccount", "app-sa"),
        "c/templates/deploy.yaml": H_DEPLOY % (sa, cmn, sec),
    }
    v, fs = _chart_full(chart)
    dyn = sorted(d.pattern for d in fs["c/templates/deploy.yaml"].dyn)
    assert dyn[0].startswith("name:ConfigMap:") and any(d.startswith("name:Secret:") for d in dyn)
    assert v["ConfigMap:app-config"] == v["Secret:app-secret"] == v["ServiceAccount:app-sa"] == "weak-alive"
    assert v["c/templates/cm.yaml"] == v["c/templates/sec.yaml"] == v["c/templates/sa.yaml"] == "weak-alive"
    assert v["Deployment:web"] == "alive"


def test_helm_a_computed_reference_reads_only_the_objects_its_literal_parts_allow():
    chart = {
        "c/Chart.yaml": H_CHART,
        "c/templates/deploy.yaml": H_DEPLOY % ("web-sa", "web-config", "{{ .Release.Name }}-secret"),
        "c/templates/ok.yaml": H_OBJ % ("Secret", "rel-secret"),
        "c/templates/other-kind.yaml": H_OBJ % ("ConfigMap", "rel-secret"),
        "c/templates/other-name.yaml": H_OBJ % ("Secret", "rel-token"),
        "c/templates/sa.yaml": H_OBJ % ("ServiceAccount", "web-sa"),
        "c/templates/cm.yaml": H_OBJ % ("ConfigMap", "web-config"),
    }
    v, _ = _chart_full(chart)
    assert v["Secret:rel-secret"] == v["ServiceAccount:web-sa"] == v["ConfigMap:web-config"] == "weak-alive"
    assert v["ConfigMap:rel-secret"].startswith("dead")  # the kind is part of the pattern
    assert v["Secret:rel-token"].startswith("dead")  # and so are the literal parts of the name


def test_k8s_a_name_with_a_dot_is_escaped_the_way_its_def_is_in_a_computed_reference_glob():
    out = cfg._Out()
    cfg._computed_ref("Secret", f"a.b{cfg.PLACE}-x{cfg.PLACE}", 3, "S:q", out)
    assert [d.pattern for d in out.dyn] == ["name:Secret:a%2Eb*-x*"]
    cfg._computed_ref("Secret", "plain", 3, "", out)
    cfg._computed_ref("", cfg.PLACE, 3, "", out)
    cfg._computed_ref(f"Sec{cfg.PLACE}", cfg.PLACE, 3, "", out)
    assert len(out.dyn) == 1  # a known name is a plain ref, no kind or a computed kind reads nothing


CHART_OWNED = {
    "c/values.schema.json": '{"type": "object"}\n',
    "c/Chart.lock": "dependencies: []\ndigest: x\n",
    "c/.helmignore": "*.swp\n",
    "c/crds/foo.yaml": "apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata:\n  name: foos.x.io\n",
    "c/charts/sub-1.0.0.tgz": "binary",
}


def test_helm_files_a_chart_owns_and_helm_reads_without_a_template_are_not_dead():
    """Counterexample: values.schema.json (json), Chart.lock and .helmignore (no language) were dead beside Chart.yaml."""
    chart = {
        "c/Chart.yaml": H_CHART,
        "c/values.yaml": "a: 1\n",
        "c/templates/cm.yaml": H_OBJ % ("ConfigMap", "x"),
        **CHART_OWNED,
    }
    v, fs = _chart_full(chart)
    assert {r.name for r in fs["c/Chart.yaml"].refs if r.kind == "include"} == set(
        cfg.rules()["helm"]["chart_files"]
    )  # data: helm.chart_files
    for p in ("c/values.schema.json", "c/Chart.lock", "c/.helmignore", "c/charts/sub-1.0.0.tgz"):
        assert v[p] == "alive", p
    stray = {**chart, "c/notes.txt": "x\n", "d/Chart.lock": "x\n"}  # beside no Chart.yaml
    v, _ = _chart_full({k: x for k, x in stray.items() if k != "c/Chart.yaml"})
    assert v["c/Chart.lock"] == "dead" and v["d/Chart.lock"] == "dead" and v["c/notes.txt"] == "dead"  # the control
