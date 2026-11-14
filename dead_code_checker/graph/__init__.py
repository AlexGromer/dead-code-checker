"""The reachability-graph checker (`dead-code-checker graph ...`): data layout, config, parallel units and the core graph.

The modules are independent of the v1 text checker in `dead_code_checker.core` (which they use for its git helpers and its
`# deadcode: ignore` markers); the two share the config file name.

    paths     where the data of a run lives (resolve, repo_id, find_root)
    config    the `[graph]` table of `.dead-code-checker.toml` and the DEAD_CODE_CHECKER_* variables (load)
    par       ordered, deterministic parallel units (run_units, workers)
    model     nodes, edges, roots, evidence, per-file facts, shards and findings: the value types
    contracts the Extractor and RootExtractor interfaces and their registry: where the language packs plug in
    langs     file -> language and the project profile (data/langs.json)
    store     the SQLite cache of shards, snapshots and the graph
    vcs       the repository: snapshot with blob shas, diffs, merge-base, file content
    roots     entry points that something outside the code runs or loads (systemd, cron, npm, html, docs, ...)
    rootsrc   the same for packaging and build files (pyproject, Cargo, Dockerfile, Makefile, ...)
    classreg  classes that registration files name (ServiceLoader, Spring factories, web.xml)
    link      Facts -> the symbol graph (scopes, imports, tiers); xlink: the cross-language edges
    reach     reachability and verdicts over the graph (alive, weak-alive, test-only, dead, dead-cluster ...)
    verdict   the corroboration rule: a dead claim is a warning only when an index or an engine agrees, else a suspect
    levels    the branch level and the graph build; tree: the call tree of one symbol; report: the output formats
    safety    a failure of the checker is a notice, never a block
    cli       `dead-code-checker graph doctor`
    extract   the extractors of the languages (python, tree-sitter queries as data, config languages, schema languages, PowerShell), their
              shared pieces and the batch driver; ts: the loader of the optional tree-sitter packages
"""
