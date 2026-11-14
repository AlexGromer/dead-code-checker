# Dead Code Checker for VS Code

Shows the findings of [dead-code-checker](https://github.com/AlexGromer/dead-code-checker) in the Problems panel and
as squiggles: unreferenced symbols and files, stubs, unread config keys, unused CLI flags. An error finding is an
error, a warning finding is a warning; the diagnostic code is the finding code and the message ends with one line on
what to do about it.

## Requirements

The `dead-code-checker` command (Python 3.11 or newer):

```bash
pipx install git+https://github.com/AlexGromer/dead-code-checker
```

## How it works

The extension runs `dead-code-checker scan <workspace folder> --json` when the folder opens and after each save
(debounced; a run still going when a newer one is wanted is stopped), and turns the JSON into diagnostics. The scan
looks at the files on disk, so findings update on save, not on every keystroke. Nothing is sent anywhere.

It shells out to the CLI instead of running the language server (`dead-code-checker lsp`) on purpose: no protocol
client in the extension, nothing to restart if a server dies, and no dependency on npm packages. Other editors use the
language server, see `docs/editors.md` in the repository.

## Settings

| setting | default | meaning |
|---|---|---|
| `deadCodeChecker.enabled` | `true` | scan on open and on save; off clears the diagnostics |
| `deadCodeChecker.path` | `dead-code-checker` | the command: a name on PATH or an absolute path; machine-scoped, read from user settings only (a workspace cannot set it) |

Commands: `Dead Code Checker: Scan workspace`, `Dead Code Checker: Clear diagnostics`.

Project options (languages, public API handling, `function_level`) come from `.dead-code-checker.toml` in the
repository, as for the command line. To silence a deliberate dynamic use, put `deadcode: ignore` on the line.

## Develop

No build step and no npm dependencies. From the repository root:

```bash
node --check editors/vscode/extension.js
node editors/vscode/test/map.test.js
node editors/vscode/test/extension.test.js
```

Package and publish (needs network and a Marketplace publisher account; see `docs/editors.md`).
