# MCP registry entry (later, draft)

Not now. dead-code-checker has no MCP server today; an entry that points at a server which does not exist would be wrong.
This file records what the entry would say and what has to exist first.

## Prerequisites

- [ ] A server exists: `dead-code-checker mcp` (or a separate package) over stdio, exposing read-only tools only, for
      example `check_branch`, `check_files`, `explain_finding`. Each tool takes a repository path inside an allowed
      root, writes nothing into it, and returns the same JSON as `graph check --format json`.
- [ ] The server is published as a package (PyPI) so a registry entry can point at an installable artifact.
- [ ] Authentication to the registry follows its documentation on the day (a GitHub-based namespace is the usual way to
      prove ownership of an `io.github.<owner>/<name>` identifier); check the current rules before writing the manifest.

## Entry text

- **Name:** `io.github.{{owner}}/dead-code-checker` (the exact identifier format is defined by the registry; verify).
- **Description:** Answers "can anything reach this code?" for a change or a repository: dead-code findings,
  deterministic and read-only.
- **Install:** `pipx install dead-code-checker`, command `dead-code-checker mcp`.
- **Environment variables:** none required.

## Also consider

Community lists of MCP servers take a pull request with one line; draft it from the entry text when the server exists.
