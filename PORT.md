# Port notes

Ported by hand from a private working copy at sha efa06ac. Nothing re-syncs this tree.

Deliberate deletions:

- the optional model layer (the step that asked a local model about suspicious function bodies) and every config key,
  flag and message that belonged to it;
- the on-disk extraction cache and the shared CPU-slot scheduler; extraction and reference search use a plain process
  pool (`--jobs`, `$DEAD_CODE_CHECKER_JOBS`);
- host-specific config file names and harness wording; the config file is `.dead-code-checker.toml`.
