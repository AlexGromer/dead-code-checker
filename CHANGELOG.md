# Changelog

## Unreleased

- Runtime mode: the login belongs to the person who runs the tool. `user` and `port` in a repository's `[graph.runtime]` are ignored (one
  `dc.bad-config` note); the login comes from `--user` / `--port` (also on `graph fix --verify`), from `user@host:port` entries of
  `DEAD_CODE_CHECKER_RUNTIME_HOSTS`, or from your ssh configuration. A line the target or ssh printed is masked before it reaches a message.
- Every program the tool starts by name (`git`, `ssh`, `bwrap`, `unshare`, `dot`, `gitleaks`, `cp`, `go`, `tsc`, the engines) is looked up in the
  absolute entries of `PATH` only and started by its absolute path with a PATH of absolute entries (`dead_code_checker/safepath.py`); a
  relative entry such as `.` can no longer make a planted file in the analysed repository run.
- Publication scan: an extractor version stamp in a page's strings (`link@13.fb5b28c1`) is no longer reported as an e-mail address (it was, whenever
  the hash began with two letters).
- Runtime-signal mode, controlling half (docs/v2.md, "Runtime mode: the controlling half and `graph runtime`"):
  `dead-code-checker graph runtime [--root DIR] [--host HOST] [--classes] [--json]` copies the tracked files at `HEAD` (never the work
  tree; no secret-like name or content, no link out of the tree) to the one ssh host you allowed, runs the project's tests there in the
  box under a tracer and stores the symbols that ran. The ssh form is fixed (`BatchMode`, `StrictHostKeyChecking=yes`, no forwarding, no
  local command; tar over the same channel; four fixed remote commands; a private 0700 directory per run, removed after every started
  run; the tool's own box files are hash-checked on the target). The host of the repository's file needs `DEAD_CODE_CHECKER_RUNTIME_HOSTS`
  or `--host`; this machine is refused; no usable target is one `dc.runtime-skipped` line and nothing runs here. `graph check` (branch
  and repo level) reads the newest stored run: a dead claim about a symbol that ran becomes the information `dc.alive-runtime`, the
  run feeds the confidence of the other claims, a run older than `refresh_days` is still used and `dc.runtime-stale` says so; it never
  contacts the target. `runtime.exec_target()` is the same target for the executing checks of a verified fix.

- Runtime-signal mode, target half and configuration (docs/v2.md, "Runtime mode: the target half"): `[graph.runtime]` in
  `.dead-code-checker.toml` (`enabled`, `host`, `user`, `port`, `command`, `timeout`, `refresh_days`; off by default; a wrong value is a
  `dc.bad-config` warning, a bad host, user or port leaves the mode off), `DEAD_CODE_CHECKER_RUNTIME_HOSTS` (the hosts a repository's
  file may name), a `runtime:` row in `doctor`, `graph/rtcfg.py` and `graph/data/runtime.json`. `graph/rtbox.py` is the one
  standard-library file that runs on the execution target inside a bubblewrap box (no network, read-only file system, fresh `/tmp`,
  `prlimit`, wall clock, output caps; `no sandbox` and no run when the box does not work) with a Python tracer (`graph/tracers/py`),
  V8 coverage for JavaScript and a coverage profile for `go test`; only the set of symbols that ran comes back. Nothing runs project
  code on the machine that runs the checker.

- `dead-code-checker graph fix --dry-run [--scope branch|repo] [--code CODE]... [--path GLOB]... [--verify [--host HOST]] [--min-confidence N] [--json]`
  (`graph/fix.py`, `graph/fixverify.py`, `graph/pycheck.py`, `graph/data/fix.json`): a patch for review that removes the dead Python
  symbols a check has confirmed (an index or engine corroborates them; suspects are never removed), with their decorators and the comment
  lines directly above. It never writes, stages or commits. `--min-confidence N` removes only claims of at least `N`, and the files and
  lists go the surest first. `--verify` applies the patch to a copy of the tree and checks it: non-executing local checks only (Python
  compile, scopes and imports in a bubblewrap box, in-process with a note when there is none; `go vet` offline and `tsc --noEmit` only in
  the box), executing checks (tests, builds) only on a configured remote target (`--host`), else one skip line. A failing patch is
  split by file, then by symbol, with bounded rounds; every symbol gets `verified-local`, `verified-runtime`, `unverified` or `failed`.
  `graph.sandbox` gains `bwrap_prefix` (the strict box) and `check_box`.
- Graph checker, precision rules for false dead-cluster claims (docs/v2.md, "Precision rules for dead-cluster claims"): Ansible
  plugin classes are roots only in a plugin file (a `*_plugins/` or `plugins/` directory, or a file that imports ansible);
  a file that is the same module under another root (same relative path, same facts) keeps its twin's defs alive; Go: the
  exported members of a declared type whose literal value is handed to another package stay used; shell: a command word with a
  literal prefix and an expansion (`"check_$t"`) is the glob `check_*`; JavaScript and TypeScript: a top-level call statement or
  a `require.main` guard makes a file a script, except in an `index.*` file. The linker version is 13 and the Python extractor 6,
  so stored graphs and shards of older versions are rebuilt.

- Numeric confidence (0 to 100) as a second axis of every dead-symbol claim (`graph/confidence.py`, `graph/data/confidence.json`): a
  deterministic sum of the base of the claim, its corroborations, the engine's own percent, hazards (decorated, dynamic, override,
  protocol, dynamic call sites) and gaps of coverage; it changes no level, code, message, exit code or default visibility. Shown in the
  text, JSON and markdown output, in `findings.json`, in SARIF (`properties.confidence`, `rank`) and in the viewer (a badge and a sort).
  `graph check --min-confidence N` leaves the claims under `N` out of the printout.
- `dead-code-checker graph check --scope branch|repo`, `view`, `why`, `tree`, `publish`, `prune`, `pages`: the viewer (one
  self-contained page per run: treemap, graph view with levels of detail, call trees, code fragments behind a gitleaks scan, or names
  only without gitleaks), the run artifacts under the data directory, `--format sarif` (SARIF 2.1.0), the architecture as files
  (`modules.svg`, `.drawio`, call pictures, `index.md`) with a scan before anything is written, an optional local `gh-pages` commit, the
  cleanup of old runs and an overview page. `doctor` also lists gitleaks, Graphviz and bwrap.
- Viewer lenses: a switch over the same run with unused (the existing colouring), duplicates (functions and methods with the same
  normalised body: comments, whitespace, the symbol's own name and local names do not count), architecture (modules in layers by
  dependency, cycles outlined, edges between languages dashed) and health (a score per module with its formula and grades in
  `data/lens.json`). The facts carry a body hash (`Def.bh` / `bsz`; Python by `ast`, the tree-sitter languages by the text of the
  definition node), the chunks ride in the page and in the run's `lens/` folder, `publish` adds `architecture.svg` (names and counts
  only, still scanned). Duplicates need a language that keeps a body hash; the page names the ones it looked at.
- Distribution: a composite GitHub Action (`action.yml`: install, check base to head, SARIF upload, one pull-request
  comment, badge file, fails only on E), `scripts/pr_comment.py`, `scripts/badge.py`, `docs/ci.md`, `docs/methodology.md`.
  The action calls `dead-code-checker graph check --scope branch|repo --format sarif|json`. The SARIF upload step carries
  a placeholder (`PIN_BY_SHA`) that must be replaced by a reviewed commit SHA before a release.
  The upload and the comment never fail the job (the upload is skipped for a pull request from a fork); the verdict is the
  exit code of the checker alone.
- `dead-code-checker agent install --target claude-code|codex|cursor [--dry-run] [--write] [--global --confirm-global]`
  and the hook it installs, `dead-code-checker agent hook`.

- The graph checker never writes into the analysed tree: every engine and indexer job runs in a private copy of the files it
  analyses (a resolved `Cargo.lock`, a `tsbuildinfo`, caches and bytecode stay in the copy), with its output paths translated
  back; Python engines run with `PYTHONDONTWRITEBYTECODE=1`. A test runs a writing stub for each engine of `data/audit.json`.
  A job in a subdirectory gets the whole tree (a Go workspace, `replace ../x`, path dependencies and tsconfig `references`
  reach into its siblings); the small files of `node_modules` and `.terraform` are copied into the job's copy and only the
  large ones are linked; the cost in disk and I/O and the remaining limits are in docs/v2.md.
- `dead-code-checker graph doctor`: the data layout, the `[graph]` configuration and the parallel-units base of the
  graph checker (see docs/v2.md). The v1 commands are unchanged.
- The core of the graph checker as a library (`dead_code_checker.graph`): the model and the store, the roots of an
  application (systemd, cron, npm, html, manifests, packaging and build files, docs), the linker, the reachability pass, the
  corroboration rule for dead claims, the branch level and the call tree.
- The extractors of the graph checker: Python (stdlib `ast`), the languages read through tree-sitter queries kept as data
  (shell, Go, Rust, JavaScript, TypeScript, TSX, Java, Kotlin, C, C++, C#, Lua, Ruby, PHP, Scala, Groovy, Nix, Starlark,
  HTML, Vue, Svelte), the config languages (Kubernetes, Helm, GitHub Actions, GitLab CI, compose, Ansible, Terraform,
  Makefile, justfile, Dockerfile, nginx, jsonnet, HAProxy, keepalived, Grafana Alloy, Redis, LogQL), PowerShell, Jinja2
  templates and the schema languages (SQL, protobuf, GraphQL), plus the cross-language edges between them. The tree-sitter
  ones are an optional extra: `pip install 'dead-code-checker[treesitter]'`; without it those files are one
  `dc.degraded-extractor` warning per language, never a crash. The command line follows.
- A file nested deeper than the recursion limit (a hostile YAML) is one file error; a language pack that cannot list its
  downloaded grammars is treated as having none unless `DEAD_CODE_CHECKER_TS_DOWNLOAD=1`.
- `dead-code-checker lsp`: a language server on stdio (standard library only), diagnostics from `scan --json` on open
  and on save.
- `editors/vscode/`: a VS Code extension (no build step, no npm dependencies); `docs/editors.md`.

## 0.1.0

First release. Deterministic checks only.

- unreferenced-symbol, test-only-symbol, unreferenced-file, stub-function, todo-stub, stub-hook,
  config-key-unread, env-unread, cli-flag-unused.
- Languages: Python, shell, JavaScript, Go, Rust; with `languages` or `scan --foreign` also Java, C/C++, C#, Kotlin,
  Swift, Scala, Dart, PHP, Ruby, Lua, Elixir, Haskell, R and CSS/SCSS/Less selectors.
- `function_level = "E"` makes a dead Python function an error (opt-in).
- `scan --foreign`: hardened audit of a tree that is not yours (no git run in it, no symlinks, no writes, W only);
  `--public-api report|exempt|as-private` decides what an exported, unreferenced symbol is.
- Config: `.dead-code-checker.toml`, table `[deadcode]`.
