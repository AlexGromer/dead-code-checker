# Port notes

Ported by hand from the previous internal version at sha efa06ac. Nothing re-syncs this tree.

Deliberate deletions:

- the optional model layer (the step that asked a local model about suspicious function bodies) and every config key,
  flag and message that belonged to it;
- the on-disk extraction cache and the shared CPU-slot scheduler; extraction and reference search use a plain process
  pool (`--jobs`, `$DEAD_CODE_CHECKER_JOBS`);
- config file names and wording that belonged to one particular environment; the config file is `.dead-code-checker.toml`.

## v2: the reachability-graph checker (`dead-code-checker graph ...`)

Ported by hand, step by step, from the previous internal version at sha 52d12f5. The v1 text checker (`core.py`,
`langs.py`) is untouched except for one line of dispatch in `core.main`: the first argument `graph` hands the rest to
`dead_code_checker.graph.cli`. Both live in one package and one console script, so `pip install` keeps working and the
pre-commit hook keeps calling v1.

**Layout: a `graph` subpackage, not `v2`.** A version label stops meaning anything once v1 is retired; `graph` says what
the code is (a reachability graph over symbols) and matches the subcommand. `pyproject.toml` lists the subpackage
explicitly; a test fails when a package directory is missing from that list.

Step 1 (skeleton and seams), what is ported:

- `graph/paths.py`: the data layout (below). New; the previous version kept its data under one shared directory of the machine.
- `graph/config.py`: the keys of the previous config module that survive the cut list: `mode`, `enabled` with
  `disabled_reason` / `disabled_until` (a disable needs a reason and a date, and expires), `budget` (`commit_s`,
  `branch_s`), `waive` (by path glob or by engine, with reason and date), `viewer_code`, `viewer_code_bytes`, and the
  rule that a repository cannot name a path, argv, env, cmd, command, exe or binary. Renamed: the table is `[graph]`
  (v1 keeps `[deadcode]`, so neither parser meets the other's keys); the finding codes keep the `dc.` prefix.
  Added: the DEAD_CODE_CHECKER_* variables, which override the file.
- `graph/par.py`: `run_units` and `workers` on `concurrent.futures`. The ordering contract is kept (results in unit
  order, one executor path for any worker count, first failure in order re-raises); the shared CPU-slot scheduler and
  the content-hash cache that the previous version had are not.
- `graph/cli.py`: `dead-code-checker graph doctor`, which prints what a run would use and writes nothing.

Data layout (decided, implemented in `paths.py`): everything a run keeps lives in the tool's own directory,
`<tool dir>/data/projects/<name>-<hash>/` (runs, `store.sqlite`, `ledger.jsonl`) and `<tool dir>/data/index.html`, where
`<hash>` is the first 8 hex digits of the sha256 of the repository's real path. The tool dir is the project directory
of a clone, an unpacked release or an editable install. Not writable, not a checkout (pip into site-packages) or inside
the analysed repository: `$DEAD_CODE_CHECKER_HOME`, else `$XDG_STATE_HOME/dead-code-checker` (`%LOCALAPPDATA%` on
Windows), with one warning line. Two deliberate readings: an explicit `$DEAD_CODE_CHECKER_HOME` wins over the tool
dir (an explicit choice is not a fallback) and is an error when unusable rather than silently ignored; `--out DIR` moves
the run artifacts only and may not be inside the analysed repository. Nothing is written into the analysed repository.

Deliberately not ported (and why):

- editor and session hooks (on an edit, at the end of a session, at start): they serve one particular editor workflow, not a library;
- remote execution of a whole-tree run on another machine, its transfer format and the remote entry point;
- the engines lock and the settings file that only an administrator can write: a public tool has no such
  trust root. Where the previous version read the lock from there, the public one takes an explicit `--lock FILE` flag (the engines
  step, below);
- the scheduled audit, promotion and shadow-E ledger kinds that served one internal workflow (the ledger itself stays);
- the comment/model layer, and the coupling to the wrapper that ran the text checker on staged files;
- the config keys `edit_ms` and `stop_s` of the budget (they belonged to the hooks above) and `languages` for now (it arrives
  with the language packs, in the `[graph]` table).

## v2 step 2: the core graph

Ported by hand from the previous internal version at sha 52d12f5: the model, the store, the linker and the reachability pass, the
roots, the corroboration rule, the branch level and the call tree. They are a library for now: `graph doctor` is still the only
command (the extractors, the engines, the viewer and the full command line are the next step). What is in:

- `graph/model.py` (value types, `Finding`), `contracts.py` (the Extractor and RootExtractor interfaces and the registry),
  `langs.py` (file -> language), `store.py` (SQLite cache), `vcs.py` (snapshot, diffs, content), `safety.py` (new, small: a
  failure of the checker is a notice);
- `roots.py`, `rootsrc.py`, `classreg.py`, `link.py`, `xlink.py`, `reach.py`;
- `verdict.py`, `report.py`, `levels.py`, `tree.py`;
- `extract/`: `batch.py` (parallel extraction driver), `yamlite.py`, and the data halves of `lang_rules.py` and `xlang.py`;
- `data/`: `langs.json`, `rules.json`, `xlang.json`, `frameworks.json`, `rootsrc.json`, `verdict.json`.

Deliberate changes (renames and rewrites):

- The disabled finding of the configuration is `dc.disabled` (the previous version had `deadcode.disabled`, a code of the text
  checker's namespace); `dc.disabled-expired` is its twin.
- A configuration file that is a symlink, or whose real path is outside the repository, is never read (W `dc.bad-config`), whatever
  the tree: the previous version applied that rule only to trees it did not trust. `config.load` has no `foreign` argument any more
  for the same reason.
- Every finding of the graph renders with the prefix `dead-code-checker:` (like the configuration problems), the path of a
  finding about the run is `(dead-code-checker)`, the module names in docs and data say `graph.<module>`; the finding codes
  keep the `dc.` prefix.
- The langs.json key for the edge from a member to the type that holds it is `holder_edge` (the previous version used another
  name), with `holders()` and the evidence rule `holder` in the linker.
- `Shard` has no `origin` (it served the remote execution); the store takes the path of `graph.paths.Layout.store`, so the
  location code is gone (see below). The table of engine results stays with `put/get/engine_keys/delete_engine_results`: the
  semantic and engine layers keep their results there.
- `contracts.defined(lang)` and `Extractor.supports(lang)` / `Extractor.probe(lang, cfg)` are new: a language with no
  extractor is `dc.unsupported-language`, an extractor that exists and cannot run is `dc.engine-missing` (the previous version
  decided that by looking for a module and a query directory of one particular extractor).
- `levels.Steps(semantic=..., prove=...)` is new: the two places where the semantic layer and the engines plug into the branch
  level. The previous version called them directly; without them every dead claim of a diff is a `dc.suspect`.
- `levels.open_store(root, layout=None)` resolves the data directory with `graph.paths`; a directory that cannot be used is
  `W dc.store-unavailable`, as before. `levels.persist(store, built)` lost its `root` argument.
- `verdict.Proof` has two fields (`covered`, `nodes`) and `text(key, path)`; the rows of the text checker's native layer and
  the `(path, line)` map are not part of it. `verdict.SEVERITY` (the order of the verdicts, worst first) lives here; the
  previous version took it from the viewer.
- `report.DEFAULT_WHY` is rewritten for a public reader and `report.register_why(table)` lets a layer add its codes; the scope
  `audit` of the markdown report is `repo`.
- `levels.Run` keeps the scope in its codes (`dc.branch-budget`, `dc.budget`); the whole-repository views are
  `levels.analyzed_proof`, `analyzed` and `why`.
- Dead-symbol findings are always W (or the hidden `dc.suspect`): the promotion of a language to E by a precision gate is not
  part of this port, so `data/promoted.json` is not either.
- `roots.command_targets`, `build_targets` and friends lost the `store` argument; `frameworks.json` has an empty `path_maps`
  (the mechanism stays: `resolve(..., maps=...)`).
- `tomllib` is imported unconditionally (Python >= 3.11).

Deliberately not ported (and why):

- the commit level of `levels` (`commit`, `commit_findings`, the text-checker findings merged into it, the cold-graph
  shortcut `dc.graph-cold`): it is the coupling to the text checker's run on staged files; the branch level and the whole-repository
  views are what a standalone tool needs. It can be rebuilt over `_check` and `Delta` later;
- the docs-only mode (`docs.py`, the `docs_only` / `outside_profile` rules of `safety.py`, `data/safety.json`): it arrives with
  the `languages` key of the configuration;
- the host rules of `safety.py` (kill switch from an administrator's settings file, the allow-list of environment variables,
  host provisioning and engine-lock checks, `finalize`, `guard`): a public tool has no trust root; the engines step decides what
  a missing engine means;
- the native-engine and language-server steps inside `levels` (`_native_rows`, `_lsp`, `_prove_dead` and the scratch copy of the tree they ran on) and the
  call of the semantic cache (`semantic_proof`): they are the engines step, and `Steps` is where they plug in;
- the root extractors of one particular tool setup: `settings` (hooks in a coding assistant's settings files), `mcp-reg` and
  `mcp-py` (tool-server registries and their Python handlers), `workflow` (workflow scripts), their data keys, the `store:` selector, the project
  variable stripping of command lines, the root kinds `hook` and `mcp-tool`, the `winlaunch` directory, the hook stub
  check (`entry_stub`, E `dc.stub-entry`) that only they produced;
- `store`: the location code (`store_path`, `data_dir`, `current_user`), the pointer by root (`remember_root`,
  `recall_root`), the read-only open for the editor hook (`open_ro`), `import_shards` and `ImportRejected` (the delivery of a remote execution),
  `drop_engine_results` (the audit's);
- `vcs`: `repo_id` and `normalize_remote` (the id of a repository is `graph.paths.repo_id`), the wrapper `foreign_git` (use
  `core.foreign_git`), the expected-tree functions that check a sync to a machine without `.git`;
- `verdict`: the `legacy`, `native` and `shadow_e` tables, the v1 claim `deadcode.unreferenced-symbol` in `codes`, the
  `(path, line)` proofs;
- `report`: `one_line` and `from_message` (the one-line formats of the staged-files wrapper and the editor hook), the explanations
  of the codes that belong to cut features;
- `tree`: the SVG picture (`svg`, `--out`, `keys_of`): it arrives with the viewer;
- `extract/common.py` (shared by the extractors: arrives with them), `extract/ref.py` (the toy language: it is now
  `tests/graph_support.py`), `lang_rules.py` functions that read the modifiers of a definition and `xlang.py` beyond `data()`;
- tests: the viewer tests of the previous version, the tests that need a language extractor (they are rewritten
  with the toy language `dcref`), the root tests that read snapshots of one setup's configuration files (rewritten with small
  synthetic units, manifests and pages), the self-host acceptance runs, and the checks of the staged-files wrapper of the text checker.
  `tests/fixtures/rootsrc` is copied with two lines changed (a Cargo key and a justfile variable name).

The analysed tree is never written: `jobs._run_job` gives every job a private copy of the files it analyses (`_private_tree`; the
whole tree for a repo and a marker job, so that a go.work, a `replace ../x`, a Cargo path dependency or a tsconfig `references` finds
its sibling directories, the named files for a files job), mirrors `node_modules` and `.terraform` (small files copied, files over
128 KiB linked, copies where links cannot be made), runs Python engines with `PYTHONDONTWRITEBYTECODE=1` and translates the paths of
the output back. The previous version relied on its sandbox for the build engines only. `test_graph_jobs` runs a stub that writes
into its working directory and rewrites a dependency file in place for every engine and indexer of `data/audit.json` and compares
every byte of the tree before and after; two tests run a Go workspace (one with the real `go list` when the tool is installed).
Known limits, also in `docs/v2.md`: the disk and the I/O of a run are those of the tree times the jobs that run at once, there is no
size limit, a large dependency file that an engine rewrites in place reaches the real one when no sandbox is used, and the
dependencies of a sibling directory are not mirrored.

Seams the next step plugs into: `contracts.register_extractor` and the `extractor` module names in `data/langs.json`
(`graph/extract/<module>.py`), `Extractor.supports` / `probe`, `levels.Steps`, `report.register_why`, `verdict.build(matches=...)`,
`store.put_engine_result`, and the `# deadcode: ignore` markers that `levels` honours through `core`.

## v2 step 3: the extractors

Ported by hand from the previous internal version at sha e7407b70 (the one with the schema languages and the config readers
without a grammar). In: `graph/extract/` `py`, `ts_generic`, `ts_lang`, `lang_rules` and `xlang` (the
full modules; step 2 had their data halves), `cfg`, `cfgts`, `cfgconf`, `tpl`, `schema`, `pwsh`, `common`; `graph/ts.py` (the
loader); the data `cfg.json`, `cfgconf.json`, `generated.json`, `pwsh/` and `queries/` (32 grammars; scrubbed of the comments
that spoke of one host); tests with synthetic fixtures (`tests/fixtures/`: cfg, go, rust, js, jvm, langcfg, langcode, native).

Deliberate changes (renames and rewrites):

- **The loader is new.** The previous version read grammar libraries from one protected cache through ctypes, checked each against a
  pinned sha256 and refused anything it had not verified. A tool you install yourself has no such trust root, so `graph/ts.py` imports the
  `tree-sitter` and `tree-sitter-language-pack` packages (optional extra `[treesitter]`), asks the pack only for a grammar it
  lists as downloaded (the pack's own downloader is behind `DEAD_CODE_CHECKER_TS_DOWNLOAD=1`; a pack that cannot list what is on disk
  counts as having nothing there), and the shard key carries the two
  package versions where the previous one carried the sha of the library.
- **A grammar or the whole extra missing is `W dc.degraded-extractor`** (the files of that language stay out of the graph, the
  message says what to install), where the previous version made it `E dc.engine-missing` for a required grammar. Every extractor
  that reads a grammar implements `Extractor.probe`, and `ts_generic` implements `supports` (queries exist).
- **pwsh** is `$DEAD_CODE_CHECKER_PWSH` or `PATH`, where the previous version resolved it through an engine registry and its lock;
  nothing about the worker changed (it parses, never runs) and it has no sandbox.
- `common.deadcode()` is `common.text_checker()` and returns `dead_code_checker.core`; the extractors reuse its `py_symbols`, stub
  detectors and decorator tables as before. `extract/ref.py` is `tests/graph_support.py` (step 2). `batch.shard_key_of` is not ported
  (nothing calls it).
- **A fix found against the real grammars.** The TypeScript queries matched a decorated member as a method with a `decorator:` field;
  in the TypeScript and TSX grammars the decorators are siblings of the method in the class body, so the pattern matched nothing
  there (a NestJS or Angular handler looked undecorated) and the TSX grammar refused the whole file with "Impossible pattern". The
  pattern is now `(class_body (decorator)+ @deco . (method_definition ...))` for TypeScript and TSX; JavaScript keeps its own form.
  Every query file of the 32 grammars now compiles on tree-sitter 0.26.1 / tree-sitter-language-pack 1.25.0 (a test does it when the
  packages and the grammars are there), and the "UNVERIFIED" / "NOT RUN" remarks of some query files, true while no grammar was
  installed, are gone.
- Schema languages: `langs.json` gives `sql`, `proto` and `graphql` the extractor `schema`; the edge kind
  `schema` is in `model.EDGE_KINDS`; `xlink` resolves `<group>:<name>` references, generated stubs and resolver classes; `link` flags
  vendored schema files by path and emits the stub, resolver and implementer edges; a migration or an init script is a soft `init` root
  (`frameworks.json` `schema_run_files`); `verdict.schema_lang` / `verdict.claims` put the claims of a schema language (and the
  `dc.orphaned-by-branch` of the branch level) under the corroboration rule, so an unused table is a hidden `dc.suspect` until
  something vouches for it; `levels` reads data languages that have an extractor. `data/verdict.json` `schema` is that table (it
  replaces a format number that nothing read). These are changes to step 2 modules, made here because the extractor is useless without
  them.
- **Generated-block markers are neutral data.** `data/generated.json` `blocks` named the begin and end lines that one internal template
  generator writes; the shipped pairs are now `// BEGIN GENERATED` .. `// END GENERATED` and `// <generated>` .. `// </generated>`
  (any pair can be added to the file), and the text of the file and the test that reads the pairs say so.
- **A hostile file is one file error.** `yamlite` is a plain recursive reader: `a: ` followed by a few thousand nested `[` raised a
  `RecursionError` that took the whole extract stage (and so the graph check of the repository) down with it. `cfg.Cfg.extract`
  now answers `Facts(errors=1)` for such a file, and `contracts.make_shard` does the same for any extractor, as the contract of
  `Extractor.extract` says (a bad input is `errors > 0`, never an exception).
- **A pack without `downloaded_languages()` is missing, not asked.** The first loader asked such a pack directly, and `get_language`
  of a pack may fetch a grammar from the network; the no-network default must not depend on the version of the pack, so without the
  list a grammar is missing unless `DEAD_CODE_CHECKER_TS_DOWNLOAD=1`.
- **Tests do not depend on each other's order.** `test_python_extractor_is_registered_by_the_langs_data` forgets the registry, so
  it uses the fixture `registry_restored` (`tests/conftest.py`), which puts the same extractor and root extractor objects back.
- The scrubbing of data: `/opt` example paths in synthetic config texts are `/srv`, the comments in the queries name `graph.extract.*`,
  and `core.generic_body_stub` where they named the text checker.

Deliberately not ported (and why):

- the engines registry and lock, the fetch/lock installer steps and their tests, the sandbox, and the audit, promotion and metrics
  modules that the schema and language tests reached for (the dead-symbol findings of the whole graph are a small helper of
  `tests/test_graph_schema.py`); the cache helpers of the test suite are `tests/real_grammar.py`, which asks `graph.ts`;
- the root extractors and tests of one agent setup (workflow scripts, hook and MCP registries) that sat next to the language tests;
- tests that read the engine registry: a language table test checks the same facts against `langs.json` and the queries directory instead.

Local verification of the real-grammar paths, done once with tree-sitter 0.26.1 and tree-sitter-language-pack 1.25.0 and all 32
grammars on disk: every query file compiled on its grammar, one sample of every language extracted, and the whole suite of the new
files passing (the tests that need a grammar skip with their reason when the extra is not installed).
The review round that followed changed no query and no grammar path; it ran on a host without the extra, where the grammar tests skip
(194 of them), so run `python3 -m pytest -q -p no:cacheprovider tests/test_graph_*.py` once where `[treesitter]` is installed to
re-confirm the TypeScript and TSX decorator query and the compile check of the 32 query sets.

## v2 step 3: the viewer, the commands, publication and SARIF

Ported by hand from the previous internal version (the same sources as step 2, plus the later changes to the viewer, the page
cleanup and the publication of the architecture). What is in:

- `graph/viz.py`, `layout.py`, `viewer/` (`shell.html`, `viewer.css`, `viewer.js`, `pages.html`): the data pack, the one-file page
  with the treemap, the graph view with levels of detail, the call trees and the code fragments, the SVG pictures;
- `graph/snippets.py`: the code fragments, behind a gitleaks scan;
- `graph/artifacts.py`, `pages.py`, `data/pages.json`: the run directories, the cleanup, the overview page;
- `graph/publish.py`, `data/publish.json`: the architecture as files, the scan, the local `gh-pages` commit;
- `graph/sarif.py` (new): SARIF 2.1.0 for `check --format sarif`;
- `graph/repo.py` (new, small): the repo level of `check`;
- `graph/cli.py`: `check`, `view`, `why`, `tree`, `publish`, `prune`, `pages`, and the tools rows of `doctor`;
- `graph/tree.py`: the SVG picture (`svg`, `--svg`, `--out`), which step 2 left out.

Deliberate changes (renames and rewrites):

- Run artifacts live in the data layout of `paths.py`: `<project>/runs/<utc>-<scope>-<head8>/`, `latest` in `runs/`, the overview
  page at `<data root>/index.html`, `repo.json` and `summaries.json` in the project directory, the layout cache in
  `<project>/layout-cache/` (passed down as `layout_dir`; None means no cache). `--out DIR` makes `DIR` the directory that holds the
  run directories; such runs are neither cleaned up nor listed. `viz.run_dir` and the data-directory lookup of the previous version
  are gone; `artifacts.new_dir(layout, root, scope)` is the one place that names a run directory.
- `artifacts.publish(layout, root, scope, fs, meta, sink, ...)` has no `report_only`, `environ` or `hook` arguments (they served the
  end-of-session hook); the flag that made a cut page level I is `quiet`. Housekeeping runs after a run stored in the default place.
- The scope `audit` and everything that read it are not ported (the scheduled audit, its trend file and statistics): the overview page
  has one column for the last branch run and one for the last repo run, and no trend. `prune` keeps the last N runs of each scope in
  (`branch`, `repo`). The report dirs of the audit and the second run base are gone.
- Code fragments: the configuration comes from `graph.config.Config` (`viewer_code`, `viewer_code_bytes`); the gitleaks binary is
  `$DEAD_CODE_CHECKER_GITLEAKS`, else the system directories, else `PATH` (the previous version looked in three fixed directories
  only). The scan keeps its minimal environment and `--ignore-gitleaks-allow`. Without gitleaks there are no fragments and the
  warning `dc.viewer-code-off` says how to get them; the scan uses `par.run_units` (threads) instead of the shared CPU scheduler.
- Pictures: Graphviz is `$DEAD_CODE_CHECKER_DOT` or `PATH` (the previous version resolved it through an engine registry and a lock);
  the run is without network and with a read-only file system when `bwrap` is installed and works, and plain otherwise (bwrap is
  optional, `doctor` says which). The finding texts name `apt`, `brew` and the Graphviz download page.
- Publication: there is no opt-in key; the command is the opt-in. The branch check adds `dc.publish-stale` only when the default
  directory already has a `.dc-digest`. The scan that used a script of the previous version's configuration linter is a built-in
  marker scan (`data/publish.json` `markers`: key blocks, token shapes, credential assignments, e-mail and IPv4 addresses, absolute
  paths of a user's machine) plus a `--deny-file` of words and regular expressions of the user's own; gitleaks stays mandatory.
- `check --scope repo` is new: the previous version's whole-tree command was the audit with engines on another machine. Here it is
  the branch level with every file counted as added (`graph/repo.py`); findings keep the branch codes and an error about the tree
  is a warning, like a branch check with no base.
- `--format text|json|md|sarif` with `--json` and `--md` as short forms; `--jobs` for `check` and `view`; a bad `--jobs` is a usage
  error. The finding code prefix stays `dc.`; messages and the pages say `dead-code-checker`.
- `viewer.js`: the root kinds `hook` and `mcp-tool` are gone from the glyph rule; the user-visible strings say
  `dead-code-checker`; nothing else changed (the file is otherwise the previous one, reviewed line by line for forbidden calls).
- Tests: rewritten with the toy language `dcref` and stand-ins for gitleaks and Graphviz (`tests/gitleaks_stand_in.py`, a fake
  `dot`); the node tests of the viewer (layout equals treemap, the why chain, refusal of a foreign file) run when node is installed;
  the browser tests of the previous version (a real chromium) are not ported.

Deliberately not ported (and why):

- the hooks that ran on an editor's edits and at the end of a session, and the project-init step: one editor workflow;
- remote execution on another machine and the audit with its statistics (`audit --stats`), `metrics`, `promote`, `ledger`, `fix`,
  `index`, `cache go|jvm`: they need the engines or belong to the previous version's workflow; the engines step decides what comes
  back;
- the engine-registry lookup of `dot` and the engine lock; `store.data_dir` and the per-user data directory of the previous version.

Seams left for the next step: `cli._steps(a, root)` returns the `levels.Steps` (the semantic layer and the engines; `--lock FILE`
belongs there), and `levels.Steps` is passed to `check`, `view`, `why`, `tree` and `publish` through it. The engines step fills it
(see below).

## v2 step 3: the engines and the semantic layer

Ported by hand from the previous internal version at sha e3de68c, module by module: the engine registry and its resolution, the
output parsers, the jobs, the Go module cache, the semantic layer (SCIP, language servers) and its cache, and the code that plugs
them into the branch level. New commands: `graph lock`, `graph index`, `graph cache go`; `graph doctor` lists the engines. What is in:

- `graph/engines.py` (registry, lock, resolution, `doctor`, `plan`, versions), `graph/proc.py` (`run_group`, `netless_prefix`),
  `graph/sandbox.py`, `graph/adapters.py`, `graph/jobs.py`, `graph/gocache.py`, `graph/sem.py`, `graph/semcache.py`,
  `graph/semuse.py`, `graph/semindex.py`, `graph/steps.py`;
- `data/engines.json`, `data/audit.json`, `data/adapters.json`, `data/sem.json`; `tests/fixtures/engines` (recordings of the real
  engines) and `tests/fixtures/sem` (the indexes the real indexers wrote; the project root each index records is rewritten to a
  neutral `file:///repo/...` by re-encoding the one string, so that no path of the machine that made them is committed, and
  `test_no_fixture_and_no_data_file_carries_a_host_path` reads every byte of the fixtures and of the package to keep it so);
- tests: `test_graph_engines`, `_adapters`, `_jobs`, `_gocache`, `_sandbox`, `_sem`, `_semcache`, `_semuse`, `_steps`, with
  `graph_fake_engines.py` (stub engines: shell scripts that print what the real tool printed), `graph_scip_enc.py` and
  `graph_fake_lsp.py`.

The trust model is new. The previous version trusted an engine only from a root-owned directory, checked the sha256 against a lock
that only an administrator could write, and read its switches from a settings file with the same property. A public tool has no
such root, so: an engine is looked for in `--engines-dir` / `$DEAD_CODE_CHECKER_ENGINES_DIR`, then on `PATH`; with `--lock FILE` /
`$DEAD_CODE_CHECKER_ENGINES_LOCK` (sha256 pins, per platform; `graph lock` writes one) an engine runs only with the pinned bytes
(`sha-mismatch`), and an engine the lock does not list is `untrusted`; without a lock the engines are used as found and `doctor`
says so. The ownership checks (posix owner and mode, the Windows ACL reader), the sealed deployment copy of the lock, the check
that the code itself runs from a locked tree, and the switches of the administrator's settings file are gone. The sandbox is
optional: `sandbox.detect()` uses bubblewrap when it works, else `unshare -rn`, else nothing, and says so in one warning
`dc.sandbox-weak`; the previous version refused to run a build engine without a network-less namespace.
A lock pins bytes only: a PowerShell module (a directory) is pinned by the hash of its tree and checked like a binary, the
check happens when the engine is looked for and the engine is started later by its path (a replaced file in between is not
caught), and an engine that does not build the project (knip, tsc, tflint, staticcheck, go-deadcode, ruff, ...) runs with the
repository's files as its working directory (a private copy of them, so nothing it writes reaches the tree) outside the sandbox, so
it reads the project's own configuration. `doctor` and
`docs/v2.md` say so; the previous version had the sealed directory and the administrator's ownership checks to close this.
With a lock no file is started before its bytes are checked: the one probe that runs a candidate to tell what it is (the bare
`deadcode` is the Go tool only when its `-h` says Go, the PyPI package of that name is another tool) sees only the files that have
the pinned sha256 (`engines.find_engine` hashes first; a stub on `PATH` that the lock does not pin is never run, and a pinned
file that does not say Go is reported as missing). `graph lock` and a run without a lock still start such a file once with `-h`
in a bare environment, because there is nothing to compare it with. The lock covers the engines of the registry only. The host
tools that are not in it are looked up on `PATH` and run unpinned: `go` (`gocache.py` runs `go list` and `go mod edit` offline in
a scratch copy of a `go.mod` and `go.sum`, with `GOPROXY=off`; the fill command only prints `go mod download`), `git` (`vcs.py`,
`core.py`), and `bwrap` and `unshare` (`sandbox.py`, `proc.py`: the sandbox and the network-less prefix). Keep `PATH` to
directories you trust.

Deliberate changes (renames and rewrites):

- Registry: `data/engines.json` lists every engine with an install hint per OS (`linux`, `darwin`, `windows`), written as generic
  text (`apt install ...`, `pipx install ...`, `go install ...@latest`, `npm install -g ...`, `cargo install --locked ...`, or a
  sentence to download a release build from the project's releases page and check its checksum). The previous version's hints
  carried the version numbers and download addresses of its own deployment; none is kept, because none can be checked from the
  repository (a test rejects an address or a version number in a hint; `graph lock` pins the bytes that were reviewed). The
  commands of the previous version that wrote into a root-owned tree are not there. `degraded` now means only "no build of this engine for this OS" (it also meant "runs in the remote audit"). The `required`
  flags, the `gate`, `edit` and `naming` roles and the tree-sitter grammar table are gone (`roles`: `branch`, `audit`, `sem`; the
  engines of the other roles are listed in `audit`); `doctor` exits 2 for a missing engine only with `--strict`.
- A missing, unpinned or degraded engine is a warning (`dc.engine-incomplete`), never `dc.engine-missing` (that code stays for an
  extractor that cannot run). `engines.plan(..., cfg=)` reads a waive of `dc.engine-incomplete` by engine from the configuration.
- `engines.host_env`: an engine is given PATH, `RUSTUP_TOOLCHAIN`, `JAVA_HOME`, `SYSTEMROOT` and `RUSTUP_HOME` (the real one, else
  `~/.rustup` when it exists) and a private empty HOME; the previous version built the whole environment from fixed directories.
- `engines.engine_version`: the version that stamps the cache and the corroboration text is the lock's, else the engine's own
  answer, else a digest of its file (the previous version had the lock's version only).
- `jobs.Settings`: `eng_dir`/`sys_dirs` became `dirs`; new `roles`, `langs`, `base`, `go_cache`; `graph index` and `graph doctor` take `--engines-dir`, `--no-path`, `--lock`,
  `--go-cache`, and `index` takes `--no-sandbox`; `deep`, `max_deep`, `report_dir`
  and the audit config's `offline_caches` for Go (kept for nuget and cargo) are gone. `weight` is carried but no CPU-slot scheduler
  uses it: the pool is `par.run_units` with threads. A build engine or an indexer runs through `sandbox.wrap` (the registry's
  `sandbox.scratch_env` gives it a private build directory) instead of `unshare -rn` plus prlimit limits. The engine plan has the
  roles of the settings (`branch` for the diff, `audit` and `sem` for `graph index`).
- `data/audit.json`: entries added for `ruff`, `staticcheck` and `cargo` (their formats were in `adapters.json`, but the previous
  version ran them from the text checker's native layer); `detekt` runs with its own default configuration (the previous one used a
  rule file of the staged wrapper); `sootup` is gone.
- `gocache`: the directory is `--go-cache`, else `$DEAD_CODE_CHECKER_GO_CACHE`, else `<data root>/caches/gomod` (`paths.py`); the
  host sandbox configuration file is gone; "usable" means "exists and is not empty" (no ownership rule); `--fill` prints plain
  commands without `sudo` or `chown`; `go` is looked up with the engine resolver.
- `adapters`: `ledger_path` is gone (callers pass `layout.ledger`); the `sootup` format and the `reached` alive-claim are gone, the
  `alive_claims` mechanism stays (empty).
- `semcache.gc` no longer touches the `scip:` keys of the first cache format; `semuse.proof` has no `light` mode and no `native`
  rows (those served the edit hook and the text checker's native layer); `RoCache`, `index_tree` are gone; the roles of an `eng:`
  item are `branch` and `audit`.
- `semindex`: `graph index` without the remote delivery (its entry point, its acceptance step and the worktree it made) and without
  the seams of the audit; the ledger
  is the project's `ledger.jsonl`.
- `steps.py` is new and replaces `levels._native_rows`, `_lsp`, `_private_copy` and `_prove_dead` of the previous version. The
  native step no longer runs the text checker's per-file tools on added lines: it plans the `branch` role through `jobs.gather`,
  reads the output with the adapters, and only an exact engine match that `data/verdict.json` accepts corroborates. What the
  engines said in a branch run is kept in the semantic cache (`semcache.keep_matches`), so the next run over the same files and the
  same engine version does not ask them again. The language servers are switched off by `Opts(lsp=False)` or `$DEAD_CODE_CHECKER_LSP=off` (the previous switch lived in the administrator's
  settings file).
- Finding codes keep their names; every one that is about the run has the path `(dead-code-checker)`; the explanations of
  `dc.engine-incomplete`, `dc.engine-output`, `dc.engine-disagree`, `dc.engine-unwired`, `dc.sandbox-weak` and the `dc.sem-*` codes
  are registered with `report.register_why`.
- Python 3.11 or later, as before: `tomllib` is imported unconditionally elsewhere.

Deliberately not ported (and why):

- the text checker's native layer (`deadcode_native`: ruff, shellcheck, rustc, staticcheck and go-deadcode run on the staged text
  of the added lines, with its result cache, its legacy engine names and its `[deadcode]` keys) and its detekt side module: they
  serve the commit level and the staged wrapper, which are not part of this tool;
- single-file `rustc` and `cargo clippy`: `rustc` stays in the registry and in `doctor` (the branch role), but what runs for Rust is
  `cargo check` through the sandbox; clippy served the push hook of the previous version;
- the JVM call-graph engine (SootUp), its Java driver, `jvmcache`, `jvmrun`, its registry rows and adapter, and the coursier
  indexer (`cs`): they need a build of the project and a pinned set of jars that the public tool cannot assume; they can be added
  as another engine entry without touching the rest;
- the nightly audit and the remote execution of the engines (`audit`, `transfer`, the delivery format and its entry point), the
  engines installer and its tests (`install/engines`, the lock writer, the adopt and verify commands), the shadow ledger, the
  promotion of a language to E (`promote`), the metrics and the tool recorder (`record.py`: the recordings stay, the script that made
  them needs the previous layout);
- the hooks (`hook_edit`, `hook_stop`) and everything that reads the previous version's settings files;
- tests: `test_dc_engines_install`, `test_dc_sandbox`, `test_dc_jvm`, `test_dc_sootup`, `test_dc_audit`, `test_deadcode_native`
  (about what is not ported), and the parts of `test_dc_semuse` that go through the text checker, the hooks or the real Python
  extractor; the graphs of the `py` and `union/py` fixtures are written down as the facts an extractor gives (as the `ts` and `go`
  ones already were), and the branch-level tests of the engines and servers run on the toy language of `tests/graph_support.py`
  registered as `python`.

Seams the next step plugs into: `steps.default(Opts(...))` for `levels.branch(..., steps=...)` and `levels.analyzed_proof`;
`engines.configure(lock, engines_dir, path)` for a command line that takes `--lock`, `--engines-dir` and `--no-path`;
`jobs.Settings` / `jobs.gather` for a command that runs the engines itself; `sandbox.set_mode`.

Wiring of the engines into the command line (the two lines of work meet here): `cli._steps` returns `steps.default(...)`;
`check`, `view`, `why`, `tree` and `publish` take `--engines-dir`, `--lock`, `--no-path` and `--go-cache`, and `main` fixes the engine
setup once, so a lock that cannot be read is a usage error (exit 2) and never a `dc.check-failed`. `doctor` prints the tool rows
(gitleaks, Graphviz, bwrap) and the engine rows. The text report counts only `dc.suspect` as suspects; the other notes of level I (a
language server that did not answer) are counted as `info`, as the run line of `artifacts` already did.

## v2 step 4: precision, confidence, lenses, the runtime mode and fix

Ported by hand from the previous internal version at sha f4f4227e (its second release), in five units that were merged here: the
precision rules, the numeric confidence, the viewer lenses, the runtime-signal mode (two halves) and `fix --dry-run --verify`. Each unit
has its own section below; what the units share:

- **One trust model for anything that runs project code.** This tool never runs the analysed project's code on the machine it runs on.
  The local checks of `fix --verify` only compile, type-check or read; the checks that execute project code (tests, build scripts,
  package scripts) and the runtime mode go to ONE ssh host you configure in `[graph.runtime]` and allow (`DEAD_CODE_CHECKER_RUNTIME_HOSTS`
  or `--host`), into a bubblewrap box there; this machine is refused as that host; without a target there is exactly one skip line. The
  previous version's own bookkeeping of named machines (their flag files, host-key pins, slots and a mirrored copy of the tool) is not
  ported: it is replaced by the config table, the allow-list and a delivery that carries the tool's own box files.
- **The seams of the units are closed:** `graph.fixverify` takes the mask, the first-error line and the skip line from `graph.rtcfg`,
  `graph.runtime.exec_target` is the one target that `graph.fix` resolves (lazily), the stored runtime run feeds `dc.alive-runtime` and
  the `runtime` input of `graph.confidence`, and `test_graph_exectarget.py` runs a whole `fix --verify` over the fake channel with the
  real `graph.fixverify` types.
- **The previous steps' "not ported" lists are superseded** for `fix`, the runtime mode, confidence and the lenses; the hooks, the audit
  and its statistics, the promotion of a language to E and the remote whole-tree execution stay out.
- **A fix to the publication scan found while merging:** the e-mail marker of `data/publish.json` read an extractor version stamp
  that rides in a page's strings (`link@13.fb5b28c1`) as `user@host.tld` whenever the hash began with two letters (about one repository
  in seven). The marker now needs the domain's first label not to be all digits and the top-level domain not to run into more letters or
  digits; a test pins the stamps and a real address.
- **The login is the user's.** `user` and `port` of a repository's `[graph.runtime]` are known keys that are ignored with one note; the
  login comes from `--user` / `--port`, the `user@host:port` entries of `DEAD_CODE_CHECKER_RUNTIME_HOSTS` or the user's ssh configuration
  (`rtcfg.allowed_logins`, `runtime.target_for(..., user, port)`).
- **Relative PATH entries are never searched** (`dead_code_checker/safepath.py`: `path_dirs`, `safe_path`, `which`, `argv`, `run`,
  `popen`): every program the tool starts by its bare name (`git` in the text checker, the graph checker and the publication, `ssh`,
  `bwrap`, `unshare`, `dot`, `gitleaks`, `cp`, `go`, `tsc`, the engines) is found through the absolute directories of PATH only and is
  started by its absolute path with a PATH of absolute entries. A program that is not there is a `FileNotFoundError`; the bare name is never
  left to the operating system. `test_graph_safepath.py` plants every one of them in a repository with `.` on PATH. The language-server
  loop of `lsp.py` only passes a clean PATH on. The target half (`rtbox.py`) runs on another machine with its own fixed PATH.
- **Target lines are masked at the source:** what ssh or the target printed (`rttransport` answers, `runtime.exchange`, the one line of a
  failed or skipped step) goes through `rtcfg.mask_secrets` before it is cut.
- **Verified on this host:** every `tests/test_graph_*.py` plus `test_package.py` and `test_deadcode.py` pass; the tests that need a real
  tree-sitter grammar skip (the extra is not installed here), so the query files of the precision rules and the body hash through a real
  grammar were not run; no real ssh connection was made (the channel is a function over a directory plus argv checks).

## v2 step 4: precision rules

In: the five rules that remove false dead-cluster claims, as one change of `graph/link.py`, `graph/extract/py.py`,
`graph/extract/ts_generic.py` and the data files; the rules and their limits are in docs/v2.md ("Precision rules for dead-cluster
claims").
- Ansible plugin roots (`py_class_roots` with its `scope` in `data/frameworks.json`, extractor `py` version 6, `link._plugin_scope`);
- copy twins (`copy_twins` in `data/langs.json`, `link._copy_twins`, the edge rule `copytwin`);
- Go `members:T` for a literal handed to an imported package (`queries/go/refs.scm`, new `queries/go/rules.json`, the `cmd_alias` key of
  `dyn` in `ts_generic`), exported members of a declared type only;
- shell command names with a literal prefix (`queries/bash/refs.scm`, `dyn.name` in `queries/bash/rules.json`);
- JavaScript, TypeScript and TSX script statements (`roots.scm` of the javascript and typescript queries, a new `rules.json` in each of
  the three grammar directories), the exception for an `index.*` file in the linker, and the statements of an inline script block of a
  page, which `ts_generic` drops.

Tests: `tests/test_graph_precluster.py` (the rules on synthetic repositories, each with its control, plus three linker tests on
hand-written facts that need no grammar), the pinned vocabularies, the `py@6` shard key and the linker version in `test_graph_extract`,
`test_graph_go_rust` and `test_graph_jsts`; the recorded parses of `tests/fixtures/langcfg/recorded.json` and
`tests/fixtures/langcode/web/parse.json` carry the new matches (recorded with real grammars, copied as they are).

Deliberate changes: the linker version prefix goes from 11 to 13 in one step (the public tree never had 12), which invalidates the
stored graphs and the reach cache; the Python class-by-name roots are filtered in the linker by the path and the imports of the file (so
the shard of a file stays a function of its text alone); data strings and comments name the modules `graph.link` and `graph.extract.*`.
Not ported: nothing of the precision rules. The other parts of the same source commits (audit, fix verification, project directory
lookups, runtime configuration, the graph server) are not rules of this kind and belong to other steps or are not part of this tool.

Not verified on this host: no tree-sitter grammar is installed, so the query files (`bash/refs.scm`, `go/refs.scm`, the two `roots.scm`)
and the `rules.json` files that read their captures were copied exactly from the source, where they were run against the real
grammars, and the grammar tests of the new test file (shell, JavaScript, TypeScript, Go) are skipped here. Everything that runs without
a grammar (the Python extractor, the linker rules, the data and the vocabularies) was run.

## v2 step 4: confidence

The numeric confidence (0..100) of a dead-symbol claim, a second axis next to level and code. Ported by hand from the previous
internal version (the module, its data file and the change that wired it into the outputs).

In:

- `graph/confidence.py` and `graph/data/confidence.json` (package data is covered by the existing `data/*.json` glob):
  `compute` (the pure formula), `annotate` (never raises, keeps a number a finding already has), `sources_of`, `gap_langs`, `below`.
- `model.Finding` gets `confidence` (None) and `conf_note` ("") with the same defaults, so every old constructor call and every old
  tuple of findings stays valid; `as_dict` adds `confidence` and `confidence_note` only when a number is set.
- `verdict.Proof` gets a third field `confs` (default empty): the engine's own percent per node, filled by `build` for an exact
  hit on a symbol claim, merged by `both`. It corroborates nothing.
- `levels._check` annotates the findings at the end (both the branch level and the repo level run through it; `repo.py` only says
  so in its docstring); `report` (text, json, md, `floored`), `sarif` (`properties.confidence`, `confidenceNote`, `rank`),
  `viz` (docstring: the pack carries the findings as they are), the viewer (`shell.html`, `viewer.css`, `viewer.js`: badge and sort),
  `cli` (`--min-confidence N`, validated 0..100, a bad value is a usage error with exit 2, in `--help` and the module docstring).
- `--min-confidence` is a display filter applied after the artifacts of the run are written, so `findings.json`, `report.md`,
  `sarif.json` and the pages keep every finding. `meta["min_confidence"]` is set only when N is above 0, so the JSON of a run without
  the flag is byte-identical to what it was.
- Tests: `tests/test_graph_confidence.py` (the formula, monotony, determinism, the facts of a graph, the outputs, end to end on the
  toy language) and additions to `test_graph_sarif.py`, `test_graph_verdict.py`, `test_graph_levels.py`, `test_graph_viz.py` (the pack,
  the script under node, and, when chromium is installed, the badge and the sort in a real browser), `test_graph_cli_full.py`.

Deliberate changes:

- the `native` source (rows of the text checker's native layer) and its weight, the three v1 claim codes (`deadcode.*`) and their
  bases are not in the data file: this tree has neither. `sources_of(proof, key, path)` has no `line` argument for the same reason.
- the list of compiler tools (`cargo`, `roslyn`, `rustc`) moved from the `shadow_e` table of `verdict.json` (not ported) into
  `confidence.json` as `compilers`.
- the module imports its neighbours with plain relative imports (no fallback to plain modules).
- the previous version also annotated the findings of its nightly audit and of the commit level: neither exists here.

Seams:

- `confidence.annotate(fs, graph, proof, gaps, runtime=None)`: `runtime` is a map node key (or path) to "ran" from a runtime run. The
  branch level passes None (a comment at the call says so); wiring the result of a runtime run into it is the one place to change.

Deliberately not ported: the confidence column of the graph tool server, the ordering and floor of the `fix` command (there is no
`fix` command here), and the hook, audit and server parts of the previous test file (`tests/test_graph_confidence.py` runs on the toy
language with synthetic findings; the fix and server tests were dropped).

Not verified on this host: nothing in this change reads a grammar; the viewer was checked under node and in the installed chromium,
other browsers were not.

## v2 step 4: viewer lenses

Ported by hand from the previous internal version (the commit that added the lenses; nothing after it touched them). What is in:

- `graph/lens.py` (the lens data of a run: duplicate groups, the module graph with strongly connected components and layers, the
  health score, the inline blob, the files of `lens/`, the architecture SVG), `graph/extract/bodyhash.py` (the normalised body hash,
  Python by `ast`, the other languages by the text of the definition node), `graph/data/lens.json` (every knob, the health formula
  and the grades; package data is already covered by `data/*.json`);
- `model.Def` has `bh` / `bsz` with defaults, so an old shard (rows of 8 columns) and a hand-built tuple stay valid;
  `extract/common.merge_defs` drops the hash of a name defined twice; `extract/py.py` and `extract/ts_generic.py` compute it (`py@6`,
  `ts_generic@5`), and the data sha and the sha of `bodyhash.py` are part of `grammar_sha`, so a change of the hash or its data
  leaves no stale shard;
- `levels.facts_getter` and `sink["facts"]`: the per-file facts reach the pages after the run, through a fresh read-only store when the
  store of the run is already closed; `artifacts.publish` collects the lenses and hands them to `viz.write_run(..., lens=)`;
  `viz.make_page` carries the blob in `<script id="dclens">` and `meta.lens` (the summary); `write_run` writes `lens/` (a failed
  write is W `dc.viewer-lens-off`); a run directory goes as a whole, so `prune` needs no rule of its own for `lens/`;
- `publish`: `architecture.svg` (module names and node counts only) is rendered from the graph alone, scanned like every other file,
  and linked from `index.md`; `VERSION` of the input digest is 2, so a published directory is reported stale once;
- the viewer: `shell.html` (the switch, the lens panel, the `@@LENS@@` slot), `viewer.css`, `viewer.js` (the DOM-free lens logic above
  `boot()`, the painters inside it, the route `#lens=NAME`). Reviewed line by line: no `innerHTML`, `eval`, `fetch` or request of any
  kind, names reach the DOM through `textContent` only; the one `http://` string is the SVG namespace identifier (the page test
  counts it once and strips it before the "no network" check).

Deliberate differences from the source:

- Python 3.11: the SVG writer builds its attributes before the f-string (a backslash inside an f-string expression is a syntax error
  before 3.12).
- `graph view` also hands the facts to the pages (the source's `view` did not, so its duplicates lens was always off); the warning text of
  an off duplicates lens no longer names a command or an editor hook, since a run without facts is now only a run whose shards could
  not be read.
- `viewer.js`: opening a lens while the page is in the graph or ego view first returns to the treemap (the lens panel replaces the
  treemap stage, the graph view has its own layout). The source panel of the previous viewer (`srcview`) is not ported, so a
  duplicates row whose symbol is not in the page says so in the lens message instead of opening a source file.
- The confidence badge and column of the previous viewer are another port's: this one did not touch those lines of `viewer.js` and
  `shell.html`, expect a textual merge there. Two comments of `viewer.js` that named a source path of the previous version were reworded.
- Tests: `tests/test_graph_lens.py` covers the body hash (Python and text), the facts payload with and without the new fields, the
  keys of definitions against the linker's, the groups and their thresholds, the graph algorithms (also 20 000 modules, no recursion),
  the caps, the blob and the folder files, the SVG, the wiring on a small git repository through the real Python extractor
  (`levels.branch` + `artifacts.publish`, `check` twice, `view`, a lens that fails, a write that fails), and, when node is installed,
  the viewer's lens logic and a stub-DOM boot of every lens. The headless-browser pictures of the previous version (one screenshot per
  lens) are not ported. The small changes to `test_graph_extract.py` (extractor versions, the shard key, a shell body hash through the
  fake parser), `test_graph_publish.py` (`architecture.svg`) and `test_graph_viz.py` (the SVG namespace) go with them.

Not verified on this host: the body hash of the tree-sitter languages through a real grammar (tree-sitter is not installed; the shell
test runs through the fake parser, the text normaliser has its own tests), and the lens panels in a real browser (only node with a stub
DOM).


## v2 step 4: the runtime mode, target half

In: `graph/rtcfg.py` (the `[graph.runtime]` table and its data, `graph/data/runtime.json`), the hook in `graph/config.py`
(`Config.runtime`, in `as_dict()`, problems as `W dc.bad-config`; the field has a default so existing positional constructions stay valid)
and the `runtime:` row of `doctor` (text, plus `config.runtime_host_allowed` in `--json`), `graph/rtbox.py` (the target half),
`graph/tracers/py/sitecustomize.py` (the Python tracer, package data `tracers/py/*.py`), `tests/test_graph_rtcfg.py`,
`tests/test_graph_rtbox.py`, `tests/graph_fake_target.py`.

Deliberate differences from the source:

- One target, named by the user: the table has `host` (the login is the user's, never the file's) instead of a choice between two named machine kinds. The source's
  target names, the entry file of its second kind, the key and known-hosts settings of its target description, the per-user permission
  flag files, the slot wait (`slot_wait_s`, CPU slots) and the mirror of the target's product directory are cut; nothing in the public
  tree names them. The skip line is now `runtime mode: no execution target (<why>)` (`rtcfg.SKIP`).
- The public trust model replaces the source's gate: a host named by the repository's file is used only when the user allows it
  (`DEAD_CODE_CHECKER_RUNTIME_HOSTS`, or the host on the command line, handled by the controlling half). `rtcfg.host_problem` is strict
  (non-empty, at most 253 characters, letters, digits and `. - _ : [ ]`, no leading `-`); a bad host leaves the mode off, and `user` and
  `port` of the file are ignored with one note. There is no `local` target and no special case for loopback in `rtcfg`: refusing this machine is the
  controlling half's check.
- `rtbox.py` is standalone (standard library only, Python 3.9+; checked with `ast.parse(feature_version=(3, 9))`) and works as
  `python3 -I rtbox.py --on-target --box DIR --sha256 HEX` from a delivery directory that holds `rtbox.py`, `runtime.json` and
  `tracers/py/sitecustomize.py` (`rtbox.delivery_files()` names the three). The source imported the checker's sandbox, vcs and engines
  modules and read its data from a product directory; the few functions it needed (bubblewrap prefix with the masks of `/run`, the real
  home and other unix sockets, the bubblewrap probe, trusted-directory tool lookup, process-group start, link confinement, blob id,
  `argv_problem`, `mask_secrets`, `first_error`) are small copies inside it, and a test keeps the copies equal to the originals. The data
  is read from `runtime.json` beside the file (or `data/runtime.json` in the package); the accessor is `rtbox.rt_data()`.
- The call is refused unless `--on-target` is given (replaces the source's environment variable check; `box(..., on_target=True)` for
  callers). `main(argv)` reads nothing from the environment. The process starter argument of `box`/`execute` (a fake in the tests) is
  called `starter`.
- The Java tracer (JaCoCo agent and CLI jars at fixed machine paths) is dropped, with its reader and the `java` tracer entry: the
  jar paths cannot be given by the user through the configuration, so a spec naming `java` is refused and Java is one `no tracer for java`
  line. The `classes` table keeps Java as an executing language without a tracer.
- Tool lookup on the target uses root-owned system directories (`/usr/bin`, `/usr/local/bin`, `/bin`) only; PATH is never consulted
  for bubblewrap and prlimit.
- Runs inside one delivery stay sequential: they share one writable project copy and one wall clock (`par.run_units` is for independent
  units; these are not).
- Tests: the source's java reader and plan tests are dropped; new ones cover the delivery files, the standalone import, the parity of the
  copies, a check run, the refusal without `--on-target`, and, on a working bubblewrap, real boxes (no network, read-only host, fresh
  `/tmp`, wall clock, a standalone `-I` start, the Node and Go tracers). The ssh transport and the fake transport of the source tests
  belong to the controlling half.

Not verified on this host: nothing was run over ssh (the half that does is another port); no Python 3.9 interpreter is installed
(syntax checked only); the V8 and Go tracers ran for real here (node and go are installed, both traced a trivial project in the box) but
not with a real test framework (`npm test`, a multi-module Go repository); a bubblewrap that the kernel of a target refuses is covered by
the fake tools only.

## v2 step 4: the runtime mode, controlling half

In: `graph/runtime.py` (the round trip, the project copy, the strict reading of the answer, stored runs, `alive`, `ran_map`,
`alive_finding`, `ExecTarget`, `exec_target`, the `graph runtime` command), `graph/rttransport.py` (the ssh channel and its four remote
command forms, the this-machine check, the secret rules of the copy), `graph runtime` in `graph/cli.py`, the stored-run reading in
`levels._check` (so the branch level and the repo level both have it), `tests/test_graph_runtime.py`, `tests/test_graph_exectarget.py`,
`tests/test_graph_rtwire.py` (ported from the source tests of the same names), `tests/graph_rt_target.py` (the fake channel, the repository
builder, the stand-ins of the job and result types of `graph.fixverify`).

Deliberate differences from the source:

- One target, an ssh host the user names, replaces the two named machine kinds. The transport is written here (`SshTransport`) on the system
  `ssh` with `-o BatchMode=yes -o StrictHostKeyChecking=yes -o ClearAllForwardings=yes -o ForwardAgent=no -o ForwardX11=no -o
  PermitLocalCommand=no` and a few more (no multiplexing, no `RemoteCommand`, no tty); the user's own ssh configuration is kept for aliases
  and keys (the source ignored it and pinned its own key and known-hosts files). A host key that is not in the user's `known_hosts` is a skip
  line, never accepted. The source's remote slots, its per-tree locks, its CPU-slot wrapper, its health cache, its mirrored product
  directory and its environment allowlist file are cut: the run wrapper (ends the process group when the channel closes) stays, without
  locks; `BusyError`, `slot()` and `max_remote_slots` are gone.
- The delivery is changed: the target does not run a product tree, it runs `python3 -I rtbox.py --on-target` from `box/` of the run
  directory, with `rtbox.py`, `runtime.json` and the tracer taken from this package (`rtbox.delivery_files()`). What is put is one tar
  (`in.tar` plus those three files); the put command extracts exactly those four names; the run command checks the sha256 of the three
  tool files against the ones sent before it starts the box. `in.tar` itself is as in the source (`spec.json`, `tree/...`) and its sha256
  is on the command line.
- The trust rules are new (docs/v2.md): the host of the repository's file needs `DEAD_CODE_CHECKER_RUNTIME_HOSTS` or `--host`; this machine
  is refused before the list is read; `--host` skips only the list. The this-machine logic (names, `/etc/hosts`, interface addresses
  through `ipaddress`) is the source's. The skip line is `runtime mode: no execution target (why)`.
- The secret scan of the copy is a regex scanner of the same rules as the source's fallback scanner (private keys, cloud and platform
  tokens, quoted and unquoted assignments, archives opened to three levels), run in parallel over batches of files with `par.run_units`;
  the source also ran gitleaks when installed and read per-project allow lists from its own settings: both are cut. Names that end in
  `.example`, `.sample`, `.template`, `.dist` or `.tpl` are not secret by name (their content is scanned). There is no allow list for a
  name that really matches: such a file stops the copy.
- `graph check` reads the newest stored run whenever there is one; the source required the project's opt-in for that. No run, no change.
  The replacement of dead claims (`runtime.replace_claims`) works on the findings of the diff stage of `levels._check`, so it covers the
  branch level and the repo level alike; the source did it while it built the findings of its audit. A stale run is one level I
  `dc.runtime-stale` finding (new; the source had a nightly refresh instead). `meta["runtime"]` says which run was used.
- Cut, not ported: the audit step and the nightly refresh (`audit_layer`, `audit_alive`, `audit_view` and their tests), the two target
  names and their per-machine permission files, the doctor row of the old targets, the host guard of the audit, the repo-scope reading of the stored run
  by the fix command (it belongs to the fix port) and the end-to-end tests of the verified fix over the one target (they belong to the unit
  that ports `graph.fixverify`; `ExecTarget` here is tested with a stand-in job and result of the same fields).
- Not changed: the suspect list of the viewer (`levels.suspect_keys`) is computed from the graph, so a symbol that ran still appears there;
  only the findings of the check are replaced.

Public names the fix-verify side calls: `runtime.ExecTarget` (`name`, `unavailable()`, `run(job)`; `run` returns
`fixverify.RuntimeResult(ran, rc, error line)` and imports it lazily) and `runtime.exec_target(root, cfg=None, environ=None, host=None,
make=None) -> (ExecTarget | None, why)`; the job needs `root, lang, check, argv, edits, timeout_s, dirs`.

Not verified on this host: nothing was run over a real ssh connection (the transport is tested with fake `run` and `popen` functions
and the argument vectors; the put, pull and cleanup forms, the hash check and the wrapper were run through a local POSIX shell and
interpreter on their own); no target with a bubblewrap was reached, so the delivery was only exercised against `rtbox.box()` with its
fake box; the wording of ssh's refusals (unknown or changed host key, refused login) differs between versions and is matched by a few
regular expressions only; the option `RemoteCommand=none` was checked against `ssh -G` of the installed client (10.0) only.

## v2 step 4: fix --dry-run --verify

In: `graph/fix.py` (the patch: spans checked against the source, decorators and attached comments, tidy blank lines, manual list, `--code`,
`--path`, `--min-confidence`, the confidence order, `--json`), `graph/fixverify.py` (the copy, the two classes of check, the bounded search,
the statuses), `graph/pycheck.py` (the non-executing Python check, as it was), `graph/data/fix.json`, `sandbox.bwrap_prefix` with
`hidden_dirs` and `host_sockets` (the strict box; the existing API is unchanged) and `sandbox.check_box()`, the `fix` subcommand of
`graph/cli.py`, `tests/test_graph_fix.py` and `tests/test_graph_fixverify.py`. The `--verify` rules ported with it: a star import that
reads a removed name fails the removal (followed through a chain of re-exports), the Python check reads the pristine copy plus the edits
as an overlay (no clone per check), symlinks that are absolute or leave the tree are not copied, the path separator counts in the
inside-the-tree test, secrets in a check's first error line are masked, what the search has not proven is dropped.

Deliberate changes:

- Trust model. Local checks are non-executing and listed in code (`NON_EXECUTING`); `go vet` and `tsc` run only in the bubblewrap box
  (without one: `unverified`, never run), only the Python check has an in-process fallback (it parses and compiles, nothing runs), with a
  note in the report. Executing checks go only to a remote target. The tool is looked up like an engine (`--engines-dir`, then PATH),
  not in a fixed list of system directories; the directory of the tool, and the prefix above a `bin` directory, are bound read-only into
  the box (the box hides the real home), except where that would cover a writable directory.
- The Python check starts the interpreter that runs the checker (`sys.executable`), not a `python3` looked up on PATH.
- The strict box probe is its own (`check_box`): the exact prefix with `--unshare-all` is tried once per bubblewrap, because the probe of
  `sandbox.detect()` (the engines' box) only unshares the network.
- Seams. The execution target is resolved in ONE function, `fix._exec_target(root, host)`, which imports `graph.runtime` lazily and
  answers `(None, "runtime mode is not available in this build")` when the module is missing; `--host` is passed through. `fixverify.Target`,
  `RuntimeJob` and `RuntimeResult` keep the shape of the source (`run(job)`, `unavailable()`). `fixverify.mask_secrets`, `first_error`
  and `skip_line` are small local copies of what the runtime half keeps in `graph.rtcfg`; when that module is merged, those three
  bodies become calls to it. The skip line reads `runtime mode: no execution target (<why>)`.
- `fix --scope repo` reads the repo level of `check` (`graph.repo`), not stored whole-tree audit results: the same findings and codes as
  `check --scope repo`; a claim that is a suspect is not removable and is counted in the output.
- `fix` takes the engine options and `--jobs` like `check` and hands the semantic and engine steps to the level, so a claim is
  corroborated exactly as in `check`.
- The pool is `graph.par.run_units` (threads); no CPU-slot scheduler, so a local check takes no slot. `--json` also has `"confidence"`
  per row of `verify`.
- Cut: the machine-specific execution target of the source and its flags, the hook and audit parts of the source command, and the wording of a private setup.

Tests: ported with synthetic Python repositories; the executing-check tests use a fake target; the toolchain checks use stub `go` and
`tsc` shell scripts on a directory the test puts first in the engines search path, run in the real box (they skip with a reason when the
host has no working bubblewrap). The source tests of that machine-specific target are dropped (the target is another port's).

Not verified on this host: `go vet` and `tsc` against the real tools (only stubs ran), the box on a host with another bubblewrap, the
real runtime target (`graph.runtime` is not in this branch; a fake target stands in), Windows (no box there: every toolchain check is
`unverified`, the Python check runs in-process).

