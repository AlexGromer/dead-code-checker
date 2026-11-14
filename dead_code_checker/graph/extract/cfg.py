"""graph.extract.cfg: the config-language extractor, YAML (k8s, Helm, GitHub Actions, GitLab CI) and HCL (Terraform)
Rules are DATA: data/cfg.json (k8s kinds and reference keys, CI keys, Terraform keywords)
and data/queries/hcl/*.scm; this module only maps them to Facts (a function of (lang, text) only).

Names: every def qual is "<tag>:<name>" with the name percent-escaped for . / ~ # : and blanks (_q), so a qual never looks like a
dotted path to the linker. References are `ref` edges by that exact qual (same file: T, any file of the language group: N), config
keys are `reads` edges (link.py: cfg keys by exact qual, and "<prefix>*" = every key below the prefix).

YAML documents are classified by content, one document at a time (a Helm template is first cleaned: {{ }} actions alone on a line
vanish, inline ones become _TPL_; a name that contains _TPL_ is unknown and gets no def and no reference; in a reference position it is a
dynamic read, the DynSite `name:<Kind>:<literal parts as a glob>`, so the literal-named objects that only such a reference reads stay alive):
  k8s        apiVersion + kind (+ List items). def "<Kind>:<name>" (resource). Roots: a workload or any kind that is not consumed
             (Ingress, HPA, bindings, CRD instances, Namespace ...) is a hard k8s-workload root; Service is a soft one; ConfigMap,
             Secret, PersistentVolumeClaim, ServiceAccount, Role, ClusterRole are roots of nothing: no reader = dead, unless the
             name is computed by a template (no def: a soft root of the file, whose reads count) or a controller reads
             it by a label (data k8s.reader_labels: RBAC aggregation, the Grafana sidecar, Argo CD repository secrets). Refs: the keys of
             data/cfg.json "refs" (envFrom, volumes, serviceAccountName, imagePullSecrets, Ingress backends, scaleTargetRef, roleRef ...)
             from the object that holds them, ingress-nginx auth/ssl Secret annotations, any {kind: ConfigMap|Secret, name} mapping (Flux
             valuesFrom), certificateRefs, configMaps/secrets lists. A Service is found through its readers only (selectors are not matched).
             Files: a kustomization (kind Kustomization/Component, or a list under a key of data kustomize.marker_keys without a kind) names the
             files and directories it builds (resources, patches, generator inputs, helm values) as `include` refs, relative to its
             directory; a Flux Kustomization or Argo Application deploys a directory (data k8s.dir_refs): `include` of `dir/**`.
  Actions    `jobs` map. def "job:<id>"; the file reaches every job (a ref from the file), `needs` and the jobs of the file reach
             each other; `uses: ./x.yml` (reusable workflow) and `uses: ./dir` (local action: dir/action.yml|yaml) are `include`
             refs, `run:` is read for `exec` refs twice (_runs): path tokens (data exec_tokens, also behind a $VAR, ${VAR} or ${{ }}
             prefix: `$GITHUB_ACTION_PATH/scripts/x.sh`) and, line by line, the command parser of graph.roots (data frameworks.json
             interpreters, wrappers, launchers, shells): `python build.py`, `node tools/x.js`, `poetry run python gen.py` name the
             script after the interpreter word, `python -m pkg.main` the files a module can be (pkg/main.py, pkg/main/__main__.py ...);
             `with:` inputs of data gha.with_path_keys (docker `file:`) are `include` refs. A workflow with a trigger other than
             workflow_call is a hard `workflow` root of its file; one with only workflow_call lives through its callers.
  GitLab     top-level job maps (script/trigger/extends/stage). def "job:<name>" (hidden .name too); extends, needs, !reference are refs,
             include (string, local:, trigger:include) is an include ref (a wildcard `*`, `**`, `?` or a $VARIABLE in the path matches
             every file it can: link.py path_targets; a variable that is a whole segment is `**`, part of one is `*`), script tokens are
             exec refs (also under `default`). The pipeline file itself is a hard ci-job root by its path (roots.py GitlabRoots, `.gitlab-ci.yml`); any other file
             recognised as GitLab CI (another pipeline path, a template other projects `include: project:`) is a soft one (data gitlab.root).
  values     only in lang "helm-values" (files named values.yaml / values.yml, langs.json; Facts cannot depend on the path, so the
             language carries it: any other plain mapping, docker-compose, Chart.yaml, mkdocs, app config read by a runtime loader,
             has no reader the extractor can see and gets NO defs): a mapping without {{ }}: one cfgkey "values:a.b.c" per leaf (scalar, empty map,
             sequence). Helm templates read them: `.Values.a.b` and `index .Values "a" "b"` give reads "values:a.b" and "values:a.b.*"
             (a whole map used as one value keeps its children; every key above is read too: `foo: {}` is consulted by `.Values.foo.x`), a bare
             `.Values` reads "values:*".
  action.yml `runs`: composite steps (uses, run) as above; main/pre/post and a docker `image: Dockerfile` are exec refs (paths
             relative to the action directory), entrypoint/pre-entrypoint/post-entrypoint paths inside the image, looked up by their file
             name (data gha.container_keys; a Dockerfile COPY or ENTRYPOINT is not read). A published action is rooted by its path
             (roots.py ActionRoots): `uses: org/repo@v1` from another repository is outside the tree. A computed local `uses:`
             (`./.github/actions/${{ matrix.x }}`) is a wildcard include.
  Chart.yaml (Helm), also the requirements.yaml of a v1 chart: a root of its file; the files helm reads next to it without a template
             naming them (data helm.chart_files: values.schema.json, Chart.lock, .helmignore, crds/**, charts/**) are `include` refs; it reads (values) the subtree of every
             dependency (name, alias), `condition` paths, tags, import-values (child and parent paths, `exports.<name>`) and `global`:
             umbrella charts keep the keys the subcharts read. Every chart also reads `global` and the subtree of its OWN name: a
             subchart unpacked in charts/ needs no `dependencies` entry, and the facts cannot see the path (a root chart's key
             named like the chart is kept too: precision, never a false dead).
  Helm       {{ define "n" }} = template def "tpl:n"; include/template "n" = ref from the enclosing define (or the file). A file the
             template reads (`.Files.Get/Glob/Lines/AsConfig "files/a.conf"`, `.Template.BasePath "/cm.yaml"`; data helm.file_refs) is an
             `include` ref named from the chart root: a plain path by its tail, a pattern from one and two directories up. An
             `include` whose name is computed is a dynamic read (DynSite `name:tpl:<glob>`): `(printf "%s.labels" x)` reads every
             define that ends in `.labels`, any other expression every define; a name built from .Template.BasePath is a file.
             Structure: when the gotmpl grammar is installed and the template parses, define / block extents, the definition a reference sits in
             and the expressions come from its tree (data/queries/gotmpl); otherwise (grammar not installed, a half of a template) from a scan of
             the {{ }} actions. Both read the same things: `$.Values.a` and variables made of `.Values`, and `tpl`, which renders a string of the
             values as a template (it may read any value: `values:*`).
  helm-template  a file under a `templates/` directory that is not YAML (NOTES.txt, *.conf, *.json, *.toml ...; langs.json `in_dir`):
             helm renders it, so the {{ }} scan above is all there is (no YAML parse) and the file is a soft `template` root.
             The same holds for any `templates/` directory without a chart beside it (Ansible, cookiecutter): another engine renders
             the file from outside the tree, so it is weak-alive, never dead (a language is a function of the path, not of the tree).
  tools      the build tools that CI lines run name Makefile / justfile recipes (`make test`, `$(MAKE) -C d all`, `just build`: ref
             `recipe:<name>`; graph.roots.build_targets, data build_tools of frameworks.json) and Dockerfile stages (`--target x`, a compose
             `build: {target: x}`: `stage:<name>`); they link to the defs of graph.extract.cfgts through the link group cfg (langs.json). A
             computed word (`make ${{ matrix.target }}`) is a dynamic site `name:recipe:<glob>` that reads every recipe it can name (_Out.build),
             never a skipped word. Shell scripts, docs and Dockerfiles name recipes through xlang, the doc roots and graph.rootsrc.
             An Ansible file (a YAML file that no layer above owns) names task and vars files (include_tasks, import_tasks, import_playbook, include_vars, vars_files), templates and files (template / copy `src`,
             a computed name is a wildcard) and roles (`roles:`, include_role) as `include` refs (data ansible).
  commands   a k8s object, a compose file or a playbook (a YAML file that is no CI file) runs commands too: the scalars and the argv
             lists under the keys of data exec_keys (`command`, `args`, `entrypoint`, `script` ...) are read as shell lines for `exec`
             refs (a k8s ConfigMap/Secret `data` is user data and not entered; a values.yaml is not read).
  Loki       a plain mapping with `ruler.storage.local.directory` (a Loki config; data loki.rule_dirs) names the directory of the rule
             files its ruler reads: `include` of `dir/**`. The rule files themselves (`groups:` with `rules:`) are plain YAML: they get no
             defs, so `unknown` below makes each one a soft root of its file.
  unknown    a YAML or HCL file in which nothing is recognised (no def, no root: docker-compose, mkdocs, dependabot, values-prod,
             .tflint.hcl, terragrunt, *.tftest.hcl ...) is read by a tool outside the tree: a soft root of the file, so its
             top-level references count and it is never `dead` for want of a reader the extractor cannot see.
HCL (tree-sitter through the ts_generic backend seam; a missing grammar is ExtractorMissing, no fallback): top-level blocks give
variable -> tfvar "var:x", locals -> tfvar "local:x" per attribute, output -> var "output:x", data -> resource "data:T:N", resource ->
resource "resource:T:N", module -> resource "module:n", provider -> resource "provider:p". References are read from the attribute
expressions: var.x, local.x, data.T.N, module.n, T.N (a resource type has an underscore); the scope is the enclosing def (a local is
its own scope, so a local used only by an unused local dies with it); a module's own attributes are refs to the variables "var:<arg>" of the
module it calls; a top-level attribute (.tfvars) is a ref to its variable and roots its file (init, hard). Roots: output = soft tf-output (a child module's output
cannot be told from a root module's by the text; the roots layer may harden it by path), resource, module and provider = hard `init`
(an apply has an effect); a top-level import, check, moved, removed or terraform block roots its file (init, hard: its references have
no enclosing def), a top-level block that is not Terraform's (data terraform.native_blocks: packer source/build, nomad job, terragrunt
include ...) roots it softly: another tool reads the file and its references have no enclosing def either. Expressions give `include`
refs for the files they read (file(), templatefile(), fileset() ... of "${path.module}/x", data terraform.file_funcs; a `${var}` in the
path is a wildcard) and `exec` refs for script paths and for what a command line runs (a string with a blank in it, a heredoc or a list
read as one argv: local-exec `command`, an `external` program; _hcl_runs). Names are not scoped by directory: the same var name in two modules
links by name (N, over-approximate).
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
from bisect import bisect_right
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .. import langs as dclangs
from ..contracts import ExtractorMissing, Extractor, register_extractor
from ..model import Def, DynSite, Facts, Finding, Ref, RootHint, qual_name
from ..roots import DATA as ROOTS_DATA, build_targets, command_targets, fw, mod_files
from . import ts_generic as tg
from . import yamlite as Y
from .common import merge_defs, module_def

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cfg.json")
PLACE = "_TPL_"
MAX_LEAVES = 20000
# a Go-template action; a `}}` inside a string is no end of it (the gotmpl tree reads it so, and the scan must agree with the tree)
_ACTION = re.compile(r'\{\{(?:[^"`}]|"(?:[^"\\]|\\.)*"|`[^`]*`|\}(?!\}))*\}\}', re.S)
_INCLUDE = re.compile(r'\b(?:include|template|block)\s+"([^"]+)"')
_VALUES = re.compile(r"\.Values((?:\.[A-Za-z_]\w*)+)")
_VALUES_INDEX = re.compile(r'\bindex\s+\$?\.Values((?:\s+"[^"]+")+)')
_VALUES_BARE = re.compile(r"\.Values(?![\w.])")
_REFERENCE = re.compile(r"!reference\s*\[\s*([^,\]\s]+)")
_ALIAS = re.compile(r"(?:^|[\s\[,])\*([\w-]+)")
_ANCHOR = re.compile(r"^([^\s#:][^:#\n]*):[ \t]*&([\w-]+)", re.M)
_NOT_PATH = re.compile(r"[\s$]|://|^docker:")
_EXPR = re.compile(r"\$?" + PLACE)  # a ${{ }} expression of a workflow (the reader replaced it by _TPL_)
_VAR = re.compile(r"\$\{?\w+\}?")  # a GitLab CI variable inside an include path
_VAR_SEG = re.compile(r"(?<![^/])\$\{?\w+\}?(?![^/])")  # ... that is a whole path segment (`$CI_PROJECT_DIR/ci/a.yml`)
# a part of a path that is computed at run time: ${var.x}, printf's %s
_HOLE = re.compile(r"\$\{[^}]*\}|%[-+# 0]*\d*(?:\.\d+)?[a-zA-Z]")
_TRIGGER = "workflow_call"
# `$GITHUB_ACTION_PATH/`, `${path.module}/`, `$_TPL_/` in front of a script path
_PREFIX = re.compile(r"^\$(?:\{[\w.]+\}|\w+)/")
_CONT = re.compile(r"\\\n")  # a shell line continuation
_HCL_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')
_HEREDOC = re.compile(r"<<-?\s*(\w+)[^\n]*\n(.*?)\n[ \t]*\1[ \t]*(?:\n|$)", re.S)
_CALL = re.compile(r"\binclude\s+(?=[^\s\"])")  # an `include` whose template name is not a string literal
_PRINTF = re.compile(r'\(\s*printf\s+"([^"]*)"')
_TPL = re.compile(r"(?<![\w.$\"'])tpl(?=[\s(])")
GOTMPL = "gotmpl"


@lru_cache(maxsize=1)
def rules() -> dict:
    with open(DATA, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _exec_re():
    return re.compile(rules()["exec_tokens"])


_q = qual_name


def _script(p: str) -> str:
    """The file a command names, relative to where it runs: a variable in front of it (`$GITHUB_ACTION_PATH/x.py`) is dropped."""
    p = _path(_PREFIX.sub("", p, count=1))
    return p if re.search(r"\w", p.replace("*", "")) else ""  # `*/*` (`${a}/${b}`) names nothing


def _runs(text: str, commands: Optional[Iterable[str]] = None) -> List[str]:
    """The files a shell text runs, as paths of `exec` refs. Two readings, joined: the path tokens of data exec_tokens
    (anywhere in `text`) and what the command parser of the roots layer reads in every line of `commands` (default: the text;
    data frameworks.json interpreters, wrappers and shells): the script an interpreter runs (`python build.py`,
    `node tools/x.js`, `ruby gen.rb`) and the files a module name can be (`python -m pkg.main`)."""
    out = {posixpath.normpath(t): t for t in _exec_re().findall(text)}
    for cmd in [text] if commands is None else commands:
        for line in _CONT.sub(" ", cmd).split("\n"):
            for sel in command_targets(line):
                scheme, _, val = sel.partition(":")
                for p in mod_files(val) if scheme == "mod" else [_script(val)] if scheme == "file" else ():
                    if p:
                        out.setdefault(posixpath.normpath(p), p)
    return list(out.values())


def _hcl_runs(expr: str) -> List[str]:
    """The files a Terraform expression runs. A command line is a heredoc, a string with a blank in it (local-exec `command`),
    a string that is a script by its extension (`script = "${path.module}/run.py"`), or a list of strings read as one argv
    (`program = ["python3", "${path.module}/x.py"]`); any other string (a name, a CIDR, an ARN) is no command."""
    strs = [m.group(2) for m in _HEREDOC.finditer(expr)] or _HCL_STR.findall(expr)
    if expr.lstrip().startswith("["):
        return _runs(expr, [shlex.join(strs)])
    return _runs(expr, [t for t in strs if re.search(r"\s", t) or os.path.splitext(t)[1] in fw()["script_ext"]])


def _known(name: str) -> bool:
    return bool(name) and PLACE not in name


def _computed_ref(kind: str, name: str, line: int, scope: str, out: _Out) -> None:
    """A reference whose name a template computes (`name: {{ .Values.cfgName }}`, `{{ include "x" . }}-secret`) reads some object of
    that kind: the DynSite `name:<kind>:<literal parts as a glob>` (`Secret:*-secret`), so the literal-named objects that a chart
    renders and that only such references read stay weak-alive. An empty name or a kind that is not known reads nothing.
    """
    if kind and PLACE not in kind and PLACE in name:
        parts = [_q(x).replace("[", "[[]").replace("*", "[*]").replace("?", "[?]") for x in name.split(PLACE)]
        out.dyn.add(DynSite(f"name:{kind}:{'*'.join(parts)}", line, scope))


def _path(p: str) -> str:
    """A path written in a config value, its computed parts (`${{ }}`, `{{ }}`, `${var}`, `%s`) as `*`; '' when it is no path."""
    p = _HOLE.sub("*", _EXPR.sub("*", p.strip()))
    return "" if _NOT_PATH.search(p) else p


class _Out:
    """The facts of one file while they are collected."""

    def __init__(self):
        self.defs: List[Def] = []
        self.refs: Set[Ref] = set()
        self.roots: List[RootHint] = []
        self.dyn: Set[DynSite] = set()
        self.templ = False  # a Helm template: names and references may be computed
        self.file_root: Optional[Tuple[int, str]] = None  # (line, strength) of the root of the file itself (HCL)

    def d(self, qual: str, kind: str, l0: int, l1: int, vis: str = "package") -> str:
        self.defs.append(Def(qual, kind, l0, max(l0, l1), vis, 0, -1, ""))
        return qual

    def r(self, name: str, kind: str, line: int, scope: str = "") -> None:
        self.refs.add(Ref(name, kind, line, scope))

    def build(self, text: str, line: int, scopes: Iterable[str]) -> None:
        """The recipes and stages that a shell text names (graph.roots.build_targets), from each of `scopes`: a name is a ref, a word with
        a computed part (`make $(SUBS)`, `just {{ t }}`) a dynamic site that reads every recipe it can name, as the Helm layer does.
        """
        for q in build_targets(text):
            for sc in scopes:
                if "*" in q:
                    self.dyn.add(DynSite(f"name:{q}", line, sc))
                else:
                    self.r(q, "ref", line, sc)

    def facts(self, lang: str, text: str, errors: int) -> Facts:
        defs = tuple(merge_defs([module_def(text), *self.defs]))
        return Facts(lang, defs, tuple(sorted(self.refs)), (), tuple(self.roots), tuple(sorted(self.dyn)), errors)

    def unknown(self, kind: str) -> None:
        """Nothing in the file is recognised: a tool outside the tree reads it (see the module docstring)."""
        if not self.defs and not self.roots:
            self.roots.append(RootHint(kind, "", 1, "soft", "convention"))


# ================================================================== YAML
def _clean(text: str) -> str:
    """A Helm template made readable as YAML, line numbers kept."""

    def sub(m):
        a = text.rfind("\n", 0, m.start()) + 1
        b = text.find("\n", m.end())
        b = len(text) if b < 0 else b
        nl = "\n" * m.group().count("\n")
        return nl if not text[a : m.start()].strip() and not text[m.end() : b].strip() else PLACE + nl

    return _ACTION.sub(sub, text)


def _expr(body: str, line: int, scope: str, out: _Out) -> None:
    """What one Go-template expression reads (the text of an action, or of one expression node of the tree): include / template /
    block calls (computed names are dynamic reads), files of the chart, `.Values` paths, and `tpl` (it renders a string of the values
    as a template, which may read any value)."""
    for name in _INCLUDE.findall(body):  # `block "n"` defines and calls n, so it is a ref from the outer scope too
        out.r(f"tpl:{_q(name)}", "ref", line, scope)
    for pat in _computed(body):
        out.dyn.add(DynSite(f"name:{pat}", line, scope))
    for rx in _file_rxs():
        for ref in (r for lit in rx.findall(body) for r in _chart_file(lit)):
            out.r(ref, "include", line, scope)
    paths = [m2.group(1)[1:] for m2 in _VALUES.finditer(body)]
    paths += [".".join(re.findall(r'"([^"]+)"', m2.group(1))) for m2 in _VALUES_INDEX.finditer(body)]
    for p in paths:
        _reads(out, p, line, scope)
    if _VALUES_BARE.search(body) or _TPL.search(body):
        out.r("values:*", "reads", line, scope)


def _helm(text: str, out: _Out) -> None:
    """define / include / template and the .Values reads of a Helm template. The structure comes from the tree of the gotmpl
    grammar when it is installed and the template parses (see _helm_tree), else from a scan of the {{ }} actions."""
    parsed = _gotmpl(text)
    if parsed is not None:
        return _helm_tree(text, parsed, out)
    stack: List[Tuple[str, str]] = []  # (opening keyword, define qual or "")
    spans: Dict[str, List[int]] = {}
    for m in _ACTION.finditer(text):
        body = m.group()[2:-2].strip()
        body = body[1:].strip() if body.startswith("-") else body
        body = body[:-1].strip() if body.endswith("-") else body
        if body.startswith("/*"):
            continue
        line = text.count("\n", 0, m.start()) + 1
        kw = (re.match(r"\w+", body) or re.match(r"", body)).group()
        scope = next((q for _, q in reversed(stack) if q), "")
        if kw in ("define", "block"):
            nm = re.match(r'\w+\s+"([^"]+)"', body)
            qual = f"tpl:{_q(nm.group(1))}" if nm and _known(nm.group(1)) else ""
            if qual:
                spans[qual] = [line, line]
            stack.append((kw, qual))
        elif kw in ("if", "range", "with"):
            stack.append((kw, ""))
        elif kw == "end" and stack:
            _, qual = stack.pop()
            if qual:
                spans[qual][1] = line
        _expr(body, line, scope, out)
    for qual, (l0, l1) in spans.items():
        out.d(qual, "template", l0, l1)


def _gotmpl(text: str):
    """The parse of a Helm template by the gotmpl grammar, None when the grammar is not usable here (not installed: the scan of the
    actions stands in) or the template does not parse (a half of a template that another
    file completes)."""
    try:
        parsed = (tg._backend or tg._real_backend)(GOTMPL, text.encode("utf-8", "replace"))
    except LookupError:
        return None
    return None if parsed.errors else parsed


def _helm_tree(text: str, parsed, out: _Out) -> None:
    """The tree path of _helm: data/queries/gotmpl. A define / block is the node, so its extent and the definition a reference sits in
    are exact (an `end`, a `{{` in a string or a comment cannot confuse them); every outermost expression is read like an action.
    """
    data = text.encode("utf-8", "replace")
    line = lambda n: n.start_point[0] + 1  # noqa: E731
    txt = lambda n: data[n.start_byte : n.end_byte].decode("utf-8", "replace")  # noqa: E731
    scopes: List[Tuple[int, int, str]] = []
    for _, caps in parsed.run("defs"):
        name, node = txt(caps["name"][0])[1:-1], caps["def"][0]
        if _known(name):
            qual = out.d(f"tpl:{_q(name)}", "template", line(node), node.end_point[0] + 1)
            scopes.append((node.start_byte, node.end_byte, qual))
    scopes.sort()

    def scope_of(pos: int, skip: int = -1) -> str:
        return next((q for s, e, q in reversed(scopes) if s <= pos < e and s != skip), "")

    exprs, calls = [], []
    for _, caps in parsed.run("refs"):
        exprs += caps.get("expr", ())
        calls += [(n, caps["name"][0]) for n in caps.get("call", ())]
    for node, name in calls:  # {{ template "n" . }}, and the call that a {{ block "n" . }} also is
        out.r(f"tpl:{_q(txt(name)[1:-1])}", "ref", line(node), scope_of(node.start_byte, node.start_byte))
    last = -1
    for n in sorted(exprs, key=lambda n: (n.start_byte, -n.end_byte)):
        if n.start_byte >= last:  # the outermost expressions only: `$.Values.b` is one read, not also a bare `$.Values`
            last = n.end_byte
            _expr(txt(n), line(n), scope_of(n.start_byte), out)


def _operand(s: str, i: int) -> str:
    """The balanced `( ... )` or the word that starts at s[i]."""
    if s[i] != "(":
        return re.match(r"[^\s)]*", s[i:]).group()  # type: ignore[union-attr]
    depth = 0
    for j in range(i, len(s)):
        depth += (s[j] == "(") - (s[j] == ")")
        if not depth:
            return s[i : j + 1]
    return s[i:]


def _computed(body: str) -> List[str]:
    """The `tpl:` globs of the `include <expr>` calls of a Helm action whose template name is computed: `(printf "%s.labels" x)`
    is `tpl:*%2Elabels`, any other expression `tpl:*` (every define). A name built from .Template.BasePath is a file (see
    data helm.file_refs), not a define."""
    out = []
    for m in _CALL.finditer(body):
        expr = _operand(body, m.end())
        fmt = _PRINTF.match(expr)
        if "Template.BasePath" not in expr:
            out.append("tpl:" + ("*".join(_q(x) for x in _HOLE.split(fmt.group(1))) if fmt else "*"))
    return out


@lru_cache(maxsize=1)
def _file_rxs() -> List["re.Pattern"]:
    return [re.compile(x) for x in rules()["helm"]["file_refs"]]


def _chart_file(lit: str) -> List[str]:
    """The file a template reads (`.Files.Get "files/a.conf"`, `.Template.BasePath "/cm.yaml"`) is named from the chart root, the
    directory above templates/: a plain path is found by its tail, a pattern is tried from one and two levels up."""
    p = _path(lit).lstrip("/")
    return [f"{up}{p}" for up in ("../", "../../")] if "*" in p else [p] if p else []


def _reads(out: _Out, path: str, line: int, scope: str = "") -> None:
    """The reads of `.Values.<path>`: the key, every key below it (a whole map used as one value keeps its children) and every
    key above it (`foo: {}` or `foo: null` is consulted by `.Values.foo.x`)."""
    parts = [_q(x) for x in path.split(".")]
    for i in range(1, len(parts) + 1):
        out.r("values:" + ".".join(parts[:i]), "reads", line, scope)
    out.r("values:" + ".".join(parts) + ".*", "reads", line, scope)


def _exec_refs(node: Y.Node, scope: str, out: _Out, argv: bool = False) -> None:
    """`run:` / `script:` of a node: every scalar below it is a shell text. `argv`: a sequence of scalars is also ONE command
    line (`command: [python, -m, pkg.main]`)."""
    texts = [(s.val, s.line) for s in Y.scalars(node)]
    if argv and node.kind == "s" and all(x.kind == "q" for x in node.val):  # type: ignore[union-attr]
        texts.append((shlex.join(x.val for x in node.val), node.line))  # type: ignore[union-attr]
    for text, line in texts:
        for p in _runs(text):  # type: ignore[arg-type]
            out.r(p, "exec", line, scope)
        out.build(text, line, [scope])  # type: ignore[arg-type]


def _param(node: Optional[Y.Node], names: Iterable[str], bare: bool = False) -> List[str]:
    """The values of the parameters `names` of an Ansible module call: a mapping key, or `name=value` in the free-form string; `bare`:
    a free-form string without `=` is the value itself (`include_tasks: x.yml`)."""
    if node is None:
        return []
    if node.kind == "m":
        return [Y.scalar(Y.get(node, n)) for n in names]
    text = Y.scalar(node).strip()
    if "=" not in text:
        return [text] if bare else []
    return [m.group(1) for n in names for m in re.finditer(rf"(?<!\w){n}=(\S+)", text)]


def _tool_refs(doc: Y.Node, out: _Out) -> None:
    """Files and stages that a YAML file of a build or deploy tool names, outside CI (data ansible): Ansible task files (include_tasks,
    import_playbook, include_vars, vars_files), templates and files (template / copy `src`), roles (`roles:`, include_role) as
    `include` refs, and the Dockerfile stage of a compose `build: {target: x}` as `stage:x`."""
    a, found = rules()["ansible"], []
    for key, node in Y.walk(doc, frozenset(rules()["k8s"]["skip"])):
        k = key.rsplit(".", 1)[-1]  # ansible.builtin.template -> template
        if k in a["path_modules"]:
            found += _param(node, a["path_params"], bare=True)
        elif k in a["src_modules"]:
            found += _param(node, ["src"])
        elif k in a["list_keys"]:
            found += [Y.scalar(n) for n in Y.items(node)]
        elif k in a["role_keys"] or (k == "roles" and node.kind == "s"):
            roles = (
                [_param(n, ["name", "role"], bare=True) for n in Y.items(node)]
                if k == "roles"
                else [_param(node, ["name"])]
            )
            found += [f"roles/{r}/tasks/main.yml" for rs in roles for r in rs if r and PLACE not in r]
        elif k == "build" and node.kind == "m" and _known(Y.scalar(Y.get(node, "target"))):
            out.r(f"stage:{_q(Y.scalar(Y.get(node, 'target')).lower())}", "ref", node.line, "")
        for p in found:
            if _path(p):
                out.r(_path(p), "include", node.line, "")
        found = []


def _exec_keys(doc: Y.Node, scope: str, out: _Out) -> None:
    """The command lines of a document that runs commands without being a CI file (data exec_keys: a Deployment's `command`, a
    compose service's `entrypoint`, an Ansible task's `script` ...); the children of a key in k8s.skip are user data."""
    keys = frozenset(rules()["exec_keys"])
    for key, node in Y.walk(doc, frozenset(rules()["k8s"]["skip"])):
        if key in keys:
            _exec_refs(node, scope, out, argv=True)


def _k8s(doc: Y.Node, out: _Out) -> bool:
    k = rules()["k8s"]
    kind, api, md = Y.scalar(Y.get(doc, "kind")), Y.scalar(Y.get(doc, "apiVersion")), Y.get(doc, "metadata")
    if not (kind and api):
        return False
    if kind == "List":
        for it in Y.items(Y.get(doc, "items")):
            _k8s(it, out)
        return True
    name = Y.scalar(Y.get(md, "name"))
    qual = f"{kind}:{_q(name)}" if _known(name) else ""
    if qual:
        out.d(qual, "resource", doc.line, doc.end)
    labels = Y.get(md, "labels")
    agg = labels is not None and labels.kind == "m" and any(_reader_label().search(x) for x, _ in labels.val)  # type: ignore[union-attr]
    if kind not in k["consumed"]:
        soft = kind in k["soft_roots"]
        strength, trust = ("soft", "convention") if soft else ("hard", "registered")
        out.roots.append(RootHint("k8s-workload", qual, doc.line, strength, trust))
    elif agg or out.templ or not qual:
        # read by a controller (aggregation), by a reference the template computes, or named by the template (no def, the
        # file is the root): not provably dead
        out.roots.append(RootHint("k8s-workload", qual, doc.line, "soft", "convention"))
    if kind in rules()["kustomize"]["kinds"]:
        _kustomize(doc, qual, out)
    _dir_refs(doc, kind, qual, out)
    pats = _patterns()
    ann = Y.get(md, "annotations")  # ingress-nginx names its Secrets in annotations, which the walk below never enters
    for key, node in ann.val if ann is not None and ann.kind == "m" else ():  # type: ignore[union-attr]
        rk, tgt = k["annotation_refs"].get(key.rpartition("/")[2]), Y.scalar(node).rpartition("/")[2]
        if rk and _known(tgt):
            out.r(f"{rk}:{_q(tgt)}", "ref", node.line, qual)
        elif rk:
            _computed_ref(rk, tgt, node.line, qual, out)
    for key, node in Y.walk(doc, frozenset(k["skip"])):
        for m in Y.items(node):  # a {kind: ConfigMap|Secret, name} mapping anywhere (Flux valuesFrom ...)
            mk, tgt = Y.scalar(Y.get(m, "kind")), Y.scalar(Y.get(m, "name"))
            if mk in k["named_kinds"] and _known(tgt):
                out.r(f"{mk}:{_q(tgt)}", "ref", m.line, qual)
            elif mk in k["named_kinds"]:
                _computed_ref(mk, tgt, m.line, qual, out)
        hits = list(k["refs"].get(key, ())) + [p for rx, p in pats if rx.search(key)]
        for rule in hits:
            for m in Y.items(node):
                tgt = Y.scalar(m) if "field" not in rule else Y.scalar(Y.get(m, rule["field"]))
                rk = Y.scalar(Y.get(m, "kind")) if rule["kind"] == "*" else rule["kind"]
                if _known(tgt) and rk:
                    out.r(f"{rk}:{_q(tgt)}", "ref", m.line, qual)
                else:
                    _computed_ref(rk, tgt, m.line, qual, out)
        sel = Y.scalar(Y.get(node, "name")) if node.kind == "m" and Y.get(node, "key") is not None else ""
        if not hits and _known(sel):  # a {name, key} selector of a CRD: a ConfigMap or a Secret, which one is not known
            for rk in k["selector_kinds"]:
                out.r(f"{rk}:{_q(sel)}", "ref", node.line, qual)
        elif not hits:
            for rk in k["selector_kinds"]:
                _computed_ref(rk, sel, node.line, qual, out)
    _exec_keys(doc, qual, out)
    return True


def _kz_file(p: str, line: int, scope: str, out: _Out) -> None:
    """A file or directory a kustomization builds, relative to its directory: a directory is its kustomization file, a remote
    base has no file in the tree (the ref resolves to nothing)."""
    p = _path(p.rpartition("=")[2] if "=" in p and "?" not in p else p).rstrip("/")  # a generator's `key=path`
    if p and p != ".":  # the directory of the kustomization itself names nothing
        out.r(p, "include", line, scope)
        if not os.path.splitext(p)[1]:
            for f in rules()["kustomize"]["files"]:
                out.r(f"{p}/{f}", "include", line, scope)


def _kustomize(doc: Y.Node, scope: str, out: _Out) -> None:
    """The files a kustomization lists (resources, patches, generator inputs, helm values) are read by `kustomize build`."""
    kz = rules()["kustomize"]
    for key in kz["path_keys"]:
        for it in Y.items(Y.get(doc, key)):
            for v in [it] if it.kind == "q" else [x for f in kz["path_fields"] for x in Y.items(Y.get(it, f))]:
                _kz_file(Y.scalar(v), v.line, scope, out)


def _is_kustomize(doc: Y.Node) -> bool:
    """A kustomization without apiVersion and kind: a list under one of its keys (a `resources:` map is a Helm value)."""
    return any(k in rules()["kustomize"]["marker_keys"] and v.kind == "s" for k, v in doc.val)  # type: ignore[union-attr]


def _dig(node: Y.Node, path: List[str]) -> List[Y.Node]:
    """The values at a dotted path below `node`; a sequence on the way is walked."""
    nodes = [node]
    for k in path:
        nodes = [v for n in nodes for it in Y.items(n) for v in [Y.get(it, k)] if v is not None]
    return nodes


def _ruler_dirs(doc: Y.Node, out: _Out) -> None:
    """A Loki config names the directory of the rule files that its ruler reads (a path of a container: found by its tail)."""
    for dotted in rules()["loki"]["rule_dirs"]:
        for n in _dig(doc, dotted.split(".")):
            p = _path(Y.scalar(n)).strip("/")
            if p and p != ".":
                out.r(f"{p}/**", "include", n.line, "")


def _dir_refs(doc: Y.Node, kind: str, scope: str, out: _Out) -> None:
    """A GitOps object (Flux Kustomization, Argo Application) deploys every file of a directory of the tree."""
    for rule in (r for r in rules()["k8s"]["dir_refs"] if r["kind"] == kind):
        for n in _dig(doc, rule["path"].split(".")):
            p = _path(Y.scalar(n)).strip("/")
            if p and p != ".":
                out.r(f"{p}/**", "include", n.line, scope)


@lru_cache(maxsize=1)
def _reader_label() -> "re.Pattern":
    return re.compile(rules()["k8s"]["reader_labels"])


@lru_cache(maxsize=1)
def _patterns() -> List[Tuple["re.Pattern", dict]]:
    return [(re.compile(p["key"]), p) for p in rules()["k8s"]["patterns"]]


def _triggers(on: Optional[Y.Node]) -> List[str]:
    if on is None:
        return []
    if on.kind == "m":
        return [k for k, _ in on.val]  # type: ignore[union-attr]
    return [Y.scalar(x) for x in Y.items(on)]


def _uses(u: str, line: int, scope: str, out: _Out) -> None:
    if not u.startswith("./"):
        return  # a remote action or workflow is external
    u = _EXPR.sub("*", u)  # a computed path matches every file it can
    if u.endswith((".yml", ".yaml")):
        out.r(u, "include", line, scope)
    else:
        for f in ("action.yml", "action.yaml"):
            out.r(f"{u.rstrip('/')}/{f}", "include", line, scope)


def _with_paths(step: Y.Node, scope: str, out: _Out) -> None:
    """`with: {file: ./docker/Dockerfile}` of a step: an input that names a file of the tree."""
    for key in rules()["gha"]["with_path_keys"]:
        v = Y.get(Y.get(step, "with"), key)
        p = _path(Y.scalar(v))
        if p:
            out.r(p, "include", v.line, scope)  # type: ignore[union-attr]


def _gha(doc: Y.Node, jobs: Y.Node, out: _Out) -> None:
    trig = _triggers(Y.get(doc, "on"))
    on = Y.get(doc, "on")
    hard = any(t != _TRIGGER for t in trig)  # workflow_call only: callers may live in other repositories, so soft
    out.roots.append(RootHint("workflow", "", on.line, "hard" if hard else "soft", "registered" if hard else "convention"))  # type: ignore[union-attr]
    for jid, job in jobs.val:  # type: ignore[union-attr]
        if job.kind != "m":
            continue
        q = out.d(f"job:{_q(jid)}", "job", job.line, job.end)
        out.r(q, "ref", job.line, "")
        for n in Y.items(Y.get(job, "needs")):
            if _known(Y.scalar(n)):
                out.r(f"job:{_q(Y.scalar(n))}", "ref", n.line, q)
        _uses(Y.scalar(Y.get(job, "uses")), job.line, q, out)
        for step in Y.items(Y.get(job, "steps")):
            _uses(Y.scalar(Y.get(step, "uses")), step.line, q, out)
            _with_paths(step, q, out)
            run = Y.get(step, "run")
            if run is not None:
                _exec_refs(run, q, out)


def _action(runs: Y.Node, out: _Out) -> None:
    """A GitHub action.yml: composite steps (uses, run) and the entry files (main, pre, post, entrypoint ...) of a node/docker action,
    paths relative to the action directory."""
    gha = rules()["gha"]
    for k in gha["entry_keys"] + gha["container_keys"]:
        node = Y.get(runs, k)
        v = Y.scalar(node).strip()
        if v and not _NOT_PATH.search(v):  # a container path is looked up by its file name
            out.r(posixpath.basename(v) if k in gha["container_keys"] else v, "exec", node.line, "")  # type: ignore
    for step in Y.items(Y.get(runs, "steps")):
        _uses(Y.scalar(Y.get(step, "uses")), step.line, "", out)
        _with_paths(step, "", out)
        if Y.get(step, "run") is not None:
            _exec_refs(Y.get(step, "run"), "", out)  # type: ignore[arg-type]


def _gitlab_jobs(doc: Y.Node) -> List[Tuple[str, Y.Node]]:
    g = rules()["gitlab"]
    return [(k, v) for k, v in doc.val if k not in g["reserved"] and v.kind == "m"]  # type: ignore[union-attr]


def _is_gitlab(doc: Y.Node) -> bool:
    g = rules()["gitlab"]
    return any(k in g["job_keys"] for _, v in _gitlab_jobs(doc) for k, _ in v.val) or any(  # type: ignore[union-attr]
        k in ("stages", "include", "workflow") for k, _ in doc.val  # type: ignore[union-attr]
    )


def _gl_include(node: Optional[Y.Node], line: int, out: _Out) -> None:
    for it in Y.items(node):
        for loc in [it] if it.kind == "q" else Y.items(Y.get(it, "local")):
            p = Y.scalar(loc)
            if p and not p.startswith(("http:", "https:")) and PLACE not in p:
                out.r(_VAR.sub("*", _VAR_SEG.sub("**", p)).lstrip("/"), "include", loc.line or line, "")


def _gitlab(doc: Y.Node, text: str, out: _Out) -> None:
    anchors = {
        m.group(2): m.group(1).strip("'\"") for m in _ANCHOR.finditer(text)
    }  # `.defaults: &defaults` at the top level
    root = rules()["gitlab"]["root"]  # a pipeline under another name, or a template shared through `include: project:`
    out.roots.append(RootHint(root["kind"], "", 1, root["strength"], "convention"))
    _gl_include(Y.get(doc, "include"), 0, out)
    for owner in (doc, Y.get(doc, "default")):  # the scripts every job runs: top level (old syntax) or under `default`
        for k in ("before_script", "after_script"):
            if Y.get(owner, k) is not None:
                _exec_refs(Y.get(owner, k), "", out)  # type: ignore[arg-type]
    for name, job in _gitlab_jobs(doc):
        q = out.d(f"job:{_q(name)}", "job", job.line, job.end)
        if not name.startswith("."):
            out.r(q, "ref", job.line, "")  # a pipeline runs its jobs
        for n in Y.items(Y.get(job, "extends")):
            if _known(Y.scalar(n)):
                out.r(f"job:{_q(Y.scalar(n))}", "ref", n.line, q)
        for n in Y.items(Y.get(job, "needs")):
            tgt = Y.scalar(n) if n.kind == "q" else Y.scalar(Y.get(n, "job"))
            if _known(tgt):
                out.r(f"job:{_q(tgt)}", "ref", n.line, q)
        for s in Y.scalars(job):
            for tgt in _REFERENCE.findall(s.val):  # type: ignore[arg-type]
                out.r(f"job:{_q(tgt)}", "ref", s.line, q)
            for al in _ALIAS.findall(s.val):  # type: ignore[arg-type]  `<<: *defaults`, `script: *steps`
                if al in anchors:
                    out.r(f"job:{_q(anchors[al])}", "ref", s.line, q)
        for k in ("script", "before_script", "after_script"):
            if Y.get(job, k) is not None:
                _exec_refs(Y.get(job, k), q, out)  # type: ignore[arg-type]
        trig = Y.get(job, "trigger")
        if trig is not None and trig.kind == "m":
            _gl_include(Y.get(trig, "include"), trig.line, out)


def _is_chart(doc: Y.Node) -> bool:
    """Chart.yaml, or the requirements.yaml of an apiVersion v1 chart: nothing but `dependencies`, each one named."""
    deps = Y.items(Y.get(doc, "dependencies"))
    if [k for k, _ in doc.val if k != "dependencies"] == [] and deps:  # type: ignore[union-attr]
        return all(d.kind == "m" and Y.get(d, "name") is not None for d in deps)
    return (
        Y.scalar(Y.get(doc, "apiVersion")) in ("v1", "v2")
        and Y.get(doc, "kind") is None
        and Y.get(doc, "name") is not None
        and (Y.get(doc, "version") is not None or Y.get(doc, "dependencies") is not None)
    )


def _chart(doc: Y.Node, out: _Out) -> None:
    """Chart.yaml: helm reads it (a root of its file), and what it names in values is read by subcharts: a dependency's own
    subtree (name or alias), `condition` paths, `tags`, `import-values` (both paths, or exports.<name>), and `global` as soon
    as there is a subchart.
    """
    out.roots.append(RootHint("init", "", doc.line, "hard", "convention"))
    for name in rules()["helm"]["chart_files"]:  # what helm reads next to Chart.yaml without any template naming it
        out.r(name, "include", doc.line)
    deps = [d for d in Y.items(Y.get(doc, "dependencies")) if d.kind == "m"]
    # `global` is shared with every subchart; the values of a subchart are named by its chart name in the parent. A chart
    # unpacked in charts/ needs no `dependencies` entry, and the facts cannot see the path: every chart reads both.
    _reads(out, "global", doc.line)
    if _known(Y.scalar(Y.get(doc, "name"))):
        _reads(out, Y.scalar(Y.get(doc, "name")), doc.line)
    for d in deps:
        for key in ("name", "alias"):
            if _known(Y.scalar(Y.get(d, key))):
                _reads(out, Y.scalar(Y.get(d, key)), d.line)
        for cond in Y.scalar(Y.get(d, "condition")).split(","):
            if cond.strip():
                _reads(out, cond.strip(), d.line)
        for t in Y.items(Y.get(d, "tags")):
            if Y.scalar(t):
                _reads(out, f"tags.{Y.scalar(t)}", t.line)
        for iv in Y.items(Y.get(d, "import-values")):
            for path in filter(None, _import_paths(iv)):
                _reads(out, path, iv.line)


def _import_paths(iv: Y.Node) -> List[str]:
    """The values paths of an `import-values` item: `- data` is the child's exports.data, `{child, parent}` are two paths."""
    name = Y.scalar(iv)
    return [f"exports.{name}"] if name else [Y.scalar(Y.get(iv, k)) for k in ("child", "parent")]


def _values(doc: Y.Node, out: _Out) -> None:
    n = [0]

    def rec(node: Y.Node, path: List[str]) -> None:
        if n[0] >= MAX_LEAVES:
            return
        if node.kind == "m" and node.val:
            for k, v in node.val:  # type: ignore[union-attr]
                rec(v, path + [_q(k)])
        elif path:
            n[0] += 1
            out.d("values:" + ".".join(path), "cfgkey", node.line, node.end)

    if doc.kind == "m":
        rec(doc, [])


def _yaml(text: str, lang: str) -> Facts:
    out, templ = _Out(), "{{" in text
    out.templ = templ
    if templ:
        _helm(text, out)
    docs, errors = Y.parse(_clean(text) if templ else text)
    for doc in docs:
        if doc.kind != "m":
            if lang != "helm-values":
                _exec_keys(doc, "", out)  # an Ansible playbook is a sequence
                _tool_refs(doc, out)
            continue
        if _k8s(doc, out):
            continue
        jobs = Y.get(doc, "jobs")
        if jobs is not None and jobs.kind == "m" and Y.get(doc, "on") is not None:
            _gha(doc, jobs, out)
        elif Y.get(Y.get(doc, "runs"), "using") is not None:
            _action(Y.get(doc, "runs"), out)  # type: ignore[arg-type]
        elif _is_chart(doc):
            _chart(doc, out)
        elif lang != "helm-values" and _is_kustomize(doc):
            _kustomize(doc, "", out)  # no apiVersion and kind: its file is a soft root through `unknown`
        elif _is_gitlab(doc):
            _gitlab(doc, text, out)
        elif lang == "helm-values":
            if not templ:
                _values(doc, out)
        else:
            _exec_keys(doc, "", out)  # docker-compose, Ansible ...: read by a tool outside the tree (`unknown` below)
            _tool_refs(doc, out)
            _ruler_dirs(doc, out)
    out.unknown("template" if templ else "init")
    return out.facts(lang, text, 0 if templ else errors)


def _template(text: str) -> Facts:
    """A non-YAML file under templates/: the {{ }} scan only, a soft root (helm renders every file of the directory)."""
    out = _Out()
    _helm(text, out)
    out.roots.append(RootHint("template", "", 1, "soft", "convention"))
    return out.facts("helm-template", text, 0)


# ================================================================== HCL
_TF_VAR = re.compile(r"(?<![\w.])var\.([A-Za-z_][\w-]*)")
_TF_LOCAL = re.compile(r"(?<![\w.])local\.([A-Za-z_][\w-]*)")
_TF_DATA = re.compile(r"(?<![\w.])data\.([A-Za-z_]\w*)\.([A-Za-z_][\w-]*)")
_TF_MODULE = re.compile(r"(?<![\w.])module\.([A-Za-z_][\w-]*)")
_TF_RES = re.compile(r"(?<![\w.])([a-z][a-z0-9]*_[a-z0-9_]*)\.([A-Za-z_][\w-]*)")


def _tf_refs(expr: str) -> List[str]:
    kw = frozenset(rules()["terraform"]["keywords"])
    out = [f"var:{_q(m.group(1))}" for m in _TF_VAR.finditer(expr)]
    out += [f"local:{_q(m.group(1))}" for m in _TF_LOCAL.finditer(expr)]
    out += [f"data:{_q(m.group(1))}:{_q(m.group(2))}" for m in _TF_DATA.finditer(expr)]
    out += [f"module:{_q(m.group(1))}" for m in _TF_MODULE.finditer(expr)]
    out += [f"resource:{_q(m.group(1))}:{_q(m.group(2))}" for m in _TF_RES.finditer(expr) if m.group(1) not in kw]
    return out


def _file_root(out: _Out, line: int, strength: str = "hard") -> None:
    """The file itself is run by terraform (hard) or read by another HCL tool (soft): its top-level references have no
    enclosing def, they are alive through the file. The root is added once, at the end (a hard one wins)."""
    if out.file_root is None or (strength == "hard" and out.file_root[1] != "hard"):
        out.file_root = (line, strength)


@lru_cache(maxsize=1)
def _tf_file_rx() -> Tuple["re.Pattern", "re.Pattern"]:
    t = rules()["terraform"]
    return re.compile(t["file_funcs"]), re.compile(t["path_prefix"])


def _tf_files(expr: str) -> List[str]:
    """The files a Terraform expression reads, relative to the .tf file: file(), templatefile() ... of `"${path.module}/x"`
    (a `${var.env}` in the path is a wildcard), fileset() of a directory."""
    fn, prefix = _tf_file_rx()
    out = []
    for f, lit in fn.findall(expr):
        p = _path(prefix.sub("", lit, count=1)).rstrip("/")
        if p:
            out.append(f"{p}/**" if f == "fileset" else p)
    return out


def _block_def(t: str, labels: List[str]) -> Optional[Tuple[str, str, Optional[Tuple[str, str, str]]]]:
    """(qual, node kind, root (kind, strength, trust) or None) of a top-level block."""
    n = [_q(x) for x in labels]
    if t == "variable" and n:
        return f"var:{n[0]}", "tfvar", None
    if t == "output" and n:
        return f"output:{n[0]}", "var", ("tf-output", "soft", "convention")
    if t == "data" and len(n) == 2:
        return f"data:{n[0]}:{n[1]}", "resource", None
    if t == "resource" and len(n) == 2:
        return f"resource:{n[0]}:{n[1]}", "resource", ("init", "hard", "convention")
    if t in ("module", "provider") and n:
        return f"{t}:{n[0]}", "resource", ("init", "hard", "convention")
    return None


def _hcl(text: str, data: bytes, parsed) -> Facts:
    line = lambda n: n.start_point[0] + 1  # noqa: E731
    txt = lambda n: data[n.start_byte : n.end_byte].decode("utf-8", "replace")  # noqa: E731
    out = _Out()
    blocks: Dict[Tuple[int, int], dict] = {}
    attrs: List[Tuple[object, object, object, object]] = []  # (kw, block, name, attr node)
    for _, caps in parsed.run("defs"):
        for b in caps.get("block", ()):
            e = blocks.setdefault((b.start_byte, b.end_byte), {"node": b, "ids": {}, "labels": {}})
            for n in caps.get("block.type", ()):
                e["ids"][n.start_byte] = n
            for n in caps.get("block.label", ()):
                e["labels"][n.start_byte] = n
        for kw in caps.get("attr.kw", ()):
            attrs.append((kw, caps["attr.block"][0], caps["attr.name"][0], caps["attr.node"][0]))
    scopes: List[Tuple[int, int, str]] = []
    qual_of: Dict[int, str] = {}  # block start byte -> its qual
    inside = -1  # end byte of the last top-level block: a block that starts before it is nested
    for (s, e), b in sorted(blocks.items(), key=lambda kv: (kv[0][0], -kv[0][1])):
        if s < inside or not b["ids"]:
            continue
        inside = e
        t = txt(b["ids"][min(b["ids"])])
        labels = [txt(b["labels"][k]).strip('"') for k in sorted(b["labels"])]
        bd = _block_def(t, labels)
        if bd is None:
            if t in rules()["terraform"]["effect_blocks"]:  # import, check, moved, removed: terraform acts on them
                _file_root(out, line(b["node"]))
            elif t not in rules()["terraform"]["native_blocks"]:
                _file_root(out, line(b["node"]), "soft")  # a block of another HCL tool (packer source, nomad job ...)
            continue
        qual, kind, root = bd
        n = b["node"]
        out.d(qual, kind, line(n), n.end_point[0] + 1)
        qual_of[s] = qual
        scopes.append((s, e, qual))
        if root:
            out.roots.append(RootHint(root[0], qual, line(n), root[1], root[2]))
    meta = frozenset(rules()["terraform"]["module_meta"])
    for kw, blk, name, node in attrs:
        if kw.start_byte != blk.start_byte:  # the identifier is the block type only when the block starts with it
            continue
        t, nm = txt(kw), txt(name)
        if t == "locals":
            out.d(f"local:{_q(nm)}", "tfvar", line(node), node.end_point[0] + 1)
            scopes.append((node.start_byte, node.end_byte, f"local:{_q(nm)}"))
        elif t == "module" and nm not in meta and blk.start_byte in qual_of:
            out.r(f"var:{_q(nm)}", "ref", line(node), qual_of[blk.start_byte])
    scopes.sort()
    starts = [s[0] for s in scopes]

    def scope_of(pos: int) -> str:
        i = bisect_right(starts, pos) - 1
        while i >= 0:
            if scopes[i][0] <= pos < scopes[i][1]:
                return scopes[i][2]
            i -= 1
        return ""

    for _, caps in parsed.run("refs"):
        for n in caps.get("expr", ()):
            sc, e = scope_of(n.start_byte), txt(n)
            for name in _tf_refs(e):
                out.r(name, "ref", line(n), sc)
            for name in _tf_files(e):
                out.r(name, "include", line(n), sc)
            for name in _hcl_runs(e):  # a local-exec command
                out.r(name, "exec", line(n), sc)
        for n in caps.get("tfvars.name", ()):
            out.r(f"var:{_q(txt(n))}", "ref", line(n), "")
            _file_root(out, line(n))  # a .tfvars file is input of an apply
    if out.file_root:
        out.roots.append(RootHint("init", "", out.file_root[0], out.file_root[1], "convention"))
    out.unknown("init")
    return out.facts("hcl", text, int(parsed.errors))


# ================================================================== extractor
def parse(lang: str, data: bytes):
    """The parse of `data` by the grammar of `lang` through the backend seam of ts_generic (the real one is graph.ts: the installed
    tree-sitter and language pack). ExtractorMissing: no queries for the grammar, or the grammar is not usable (never a fallback).
    """
    g = dclangs.load().grammar(lang)
    if not g or not tg._queries(g):
        raise ExtractorMissing(lang, f"no tree-sitter queries for grammar {g} (graph/data/queries/{g}/)")
    try:
        return (tg._backend or tg._real_backend)(g, data)
    except LookupError as exc:
        raise ExtractorMissing(lang, str(exc)) from exc


def grammar_sha(lang: str, *extra: str) -> str:
    """sha256 of what decides the facts of a tree-sitter config language: the rules (cfg.json, and frameworks.json that the command
    parser reads), the grammar (the installed versions and the queries) and the module `extra` names (the mapping code)."""
    h = hashlib.sha256()
    for path in (DATA, ROOTS_DATA, *extra):
        with open(path, "rb") as fh:
            h.update(fh.read())
    g = dclangs.load().grammar(lang)
    if g:
        h.update(f"{tg.so_sha(g)}|{tg.queries_sha(g)}".encode())
    return h.hexdigest()


class Cfg(Extractor):
    # 7: computed reference names and chart files, the gotmpl tree path of the Helm layer, `tpl` reads every value, Ansible paths,
    # recipes and stages named by CI lines; 8: merged; 9: Loki ruler directories
    name, version, langs = "cfg", 9, ("yaml", "helm-values", "helm-template", "hcl")

    def grammar_sha(self, lang: str) -> str:
        """sha256(cfg.json + frameworks.json (the command parser reads it) | for hcl the grammar and queries sha of ts_generic | for the
        YAML languages the gotmpl grammar of the Helm layer): all of them decide the facts."""
        h = hashlib.sha256()
        for path in (DATA, ROOTS_DATA):
            with open(path, "rb") as fh:
                h.update(fh.read())
        if lang == "hcl":
            h.update(tg.TsGeneric().grammar_sha(lang).encode())
        else:
            h.update(f"{tg.so_sha(GOTMPL)}|{tg.queries_sha(GOTMPL)}".encode())
        return h.hexdigest()

    def probe(self, lang: str, cfg: Optional[dict] = None) -> Optional[Finding]:
        return tg.probe_grammar(lang, cfg)

    def extract(self, lang: str, path: str, text: str) -> Facts:
        text = text[1:] if text[:1] == "\ufeff" else text  # a BOM (Windows editors) is not part of the first key
        try:
            if lang in ("yaml", "helm-values"):
                return _yaml(text, lang)
            if lang == "helm-template":
                return _template(text)
            data = text.encode("utf-8", "replace")
            return _hcl(text, data, parse(lang, data))
        except RecursionError:  # `a: [[[[ ...` thousands deep: one file error, the other files of the run are still read
            return Facts(lang, errors=1)


register_extractor(Cfg())
